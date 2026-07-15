// Turn-based combat with enemy intents and the strike-timing minigame.
import { ENEMIES, TECHNIQUES, LINEAGES, ITEMS, OVERCOMES } from './data.js';
import { S, derived, addItem } from './state.js';
import { ri, chance, esc, el, sleep, pickWeighted, clamp } from './util.js';
import { openModal } from './modal.js';
import { strikeBar } from './minigames.js';

const INTENT_TEXT = {
  hit: 'is about to strike', heavy: 'gathers itself for a HEAVY blow', guard: 'takes a guarded stance',
  drain: 'reaches for your Emberlight', dot: 'kindles a lingering burn', stun: 'weaves a binding art',
  dodgeup: 'blurs, hard to pin down',
};

function elMult(a, b) {
  if (!a || !b) return 1;
  if (OVERCOMES[a] === b) return 1.3;
  if (OVERCOMES[b] === a) return 0.8;
  return 1;
}

// Resolves 'win' | 'lose' | 'flee'. Accepts an enemy id or a full enemy object.
export function startCombat(enemyId, opts = {}) {
  return new Promise(async (resolve) => {
    const def = typeof enemyId === 'string' ? ENEMIES[enemyId] : enemyId;
    const d = derived();
    const e = { id: typeof enemyId === 'string' ? enemyId : null, name: def.name, el: def.el, hp: def.hp, hpMax: def.hp, atk: def.atk,
      poise: Math.round(def.hp * 0.45), poiseMax: Math.round(def.hp * 0.45), staggered: 0, phased: false,
      guard: 0, dodgeUp: 0, dot: null, stun: 0, intent: null };
    const p = { shield: 0, dodge: 0, counter: 0, halve: 0, defend: false, buffMult: 1, buffTurns: 0,
      critAll: 0, stun: 0, dot: null, cds: {} };
    if (S.chr.lineage === 'smoke') { p.dodge = 1; p.counter = 0; }
    const m = openModal({ title: '', wide: true, locked: true });
    let over = false;
    const logLines = [];
    const log = (t, cls = '') => { logLines.push({ t, cls }); if (logLines.length > 30) logLines.shift(); render(); };

    const pickIntent = () => { e.intent = pickWeighted(def.intents, (i) => i.w); };
    pickIntent();

    const deck = [...new Set(['fistform', ...S.chr.deck])]
      .filter((t) => !TECHNIQUES[t].fx?.passiveMeditate);

    const pills = () => Object.entries(S.chr.inventory)
      .filter(([id]) => ITEMS[id]?.kind === 'pill' && (ITEMS[id].use?.hpPct || ITEMS[id].use?.qiPct))
      .map(([id, q]) => ({ id, q }));

    function render(busy = false) {
      const dd = derived();
      const hpP = clamp(S.chr.hp / dd.hpMax, 0, 1), qiP = clamp(S.chr.qi / dd.qiMax, 0, 1);
      const ehpP = clamp(e.hp / e.hpMax, 0, 1);
      m.body.innerHTML = `
      <div class="combat">
        <div class="combat-foes">
          <div class="combatant">
            <div class="c-name">${esc(S.chr.name)}</div>
            <div class="bar"><div class="bar-fill bar-hp" style="width:${hpP * 100}%"></div><span>${S.chr.hp}/${dd.hpMax}</span></div>
            <div class="bar"><div class="bar-fill bar-qi" style="width:${qiP * 100}%"></div><span>${S.chr.qi}/${dd.qiMax}</span></div>
            <div class="c-tags">${p.shield ? `<span class="tag">shield ${p.shield}</span>` : ''}${p.dodge ? '<span class="tag">veiled</span>' : ''}${p.halve ? `<span class="tag">mantled ×${p.halve}</span>` : ''}${p.buffTurns ? `<span class="tag">forged ${p.buffTurns}</span>` : ''}${p.dot ? `<span class="tag tag-bad">burning ${p.dot.turns}</span>` : ''}${p.stun ? '<span class="tag tag-bad">bound</span>' : ''}</div>
          </div>
          <div class="combat-vs">对</div>
          <div class="combatant">
            <div class="c-name">${esc(e.name)} ${def.boss ? '<span class="tag tag-boss">apex</span>' : ''}${def.rival ? '<span class="tag tag-boss">rival</span>' : ''}</div>
            <div class="bar"><div class="bar-fill bar-ehp" style="width:${ehpP * 100}%"></div><span>${Math.max(0, e.hp)}/${e.hpMax}</span></div>
            <div class="bar bar-thin"><div class="bar-fill bar-poise" style="width:${clamp(e.poise / e.poiseMax, 0, 1) * 100}%"></div><span>poise</span></div>
            <div class="c-intent">${e.staggered ? '⟡ STAGGERED — its guard is in pieces.' : e.stun ? '🕸 Bound — it will lose its turn.' : `${({ hit: '⚔', heavy: '💥', guard: '🛡', drain: '🌀', dot: '🔥', stun: '🕸', dodgeup: '💨' })[e.intent.t] || ''} It ${INTENT_TEXT[e.intent.t]}.`}</div>
            <div class="c-tags">${e.guard ? '<span class="tag">guarding</span>' : ''}${e.dodgeUp ? '<span class="tag">blurred</span>' : ''}${e.dot ? `<span class="tag tag-bad">burning ${e.dot.turns}</span>` : ''}${e.staggered ? '<span class="tag tag-boss">staggered</span>' : ''}</div>
          </div>
        </div>
        <div class="combat-log">${logLines.map((l) => `<div class="cl ${l.cls}">${l.t}</div>`).join('')}</div>
        <div class="combat-strike" id="combat-strike"></div>
        <div class="combat-actions">${busy ? '<div class="mg-hint">…</div>' : `
          ${deck.map((tid) => {
            const t = TECHNIQUES[tid];
            const cd = p.cds[tid] || 0;
            const cost = Math.round(t.qi * dd.qiCostMult);
            const no = S.chr.qi < cost || cd > 0;
            return `<button class="btn btn-tech ${no ? 'is-disabled' : ''}" data-tech="${tid}" ${no ? 'disabled' : ''}>
              <b>${t.name}</b><small>${cost ? `${cost} qi` : 'free'}${cd ? ` · ready in ${cd}` : ''}</small></button>`;
          }).join('')}
          <button class="btn" data-cact="defend"><b>Defend</b><small>halve next blow, +qi</small></button>
          ${pills().map((x) => `<button class="btn" data-cact="pill" data-pill="${x.id}"><b>${ITEMS[x.id].name}</b><small>×${x.q}</small></button>`).join('')}
          <button class="btn btn-ghost" data-cact="flee"><b>${opts.spar ? 'Yield' : 'Flee'}</b></button>`}
        </div>
      </div>`;
      const logBox = m.body.querySelector('.combat-log');
      logBox.scrollTop = logBox.scrollHeight;
      if (!busy) {
        m.body.querySelectorAll('[data-tech]').forEach((b) => b.addEventListener('click', () => playerTurn(b.dataset.tech)));
        m.body.querySelectorAll('[data-cact]').forEach((b) => b.addEventListener('click', () => {
          if (b.dataset.cact === 'defend') playerDefend();
          else if (b.dataset.cact === 'pill') playerPill(b.dataset.pill);
          else if (b.dataset.cact === 'flee') playerFlee();
        }));
      }
    }

    const dealToEnemy = (raw, elName, poiseMult = 1) => {
      let dmg = raw * elMult(elName, e.el);
      if (e.guard) dmg *= 0.5;
      if (e.staggered) dmg *= 1.5;
      dmg = Math.max(1, Math.round(dmg * (0.85 + Math.random() * 0.3)));
      if (e.dodgeUp && chance(0.5) && !e.staggered) { e.dodgeUp = 0; log(`${esc(e.name)} blurs aside — your blow parts smoke.`, 'cl-dim'); return 0; }
      e.hp -= dmg;
      document.dispatchEvent(new CustomEvent('emberline:sfx', { detail: 'hit' }));
      // poise: heavy pressure breaks guards
      if (!e.staggered) {
        e.poise -= Math.round(dmg * poiseMult);
        if (e.poise <= 0) {
          e.staggered = 2;
          log(`<b>⟡ ${esc(e.name)}'s guard SHATTERS.</b> It reels, wide open — strike now.`, 'cl-crit');
        }
      }
      // boss phases: at half health, the fight changes
      if (def.boss && !e.phased && e.hp <= e.hpMax / 2 && e.hp > 0) {
        e.phased = true;
        e.poise = e.poiseMax;
        e.staggered = 0;
        def.intents.forEach((it) => { if (it.t === 'heavy') it.w += 2; });
        log(`<b>${esc(e.name)} stops holding back.</b> Something older and angrier looks out of it now.`, 'cl-bad');
      }
      return dmg;
    };
    const dealToPlayer = (raw) => {
      let dmg = raw * ({ ember: 0.9, ash: 1.0, iron: 1.12 }[S.world.difficulty] || 1);
      if (p.defend) dmg *= 0.5;
      if (p.halve > 0) { dmg *= 0.5; p.halve--; }
      dmg = Math.max(1, Math.round(dmg * (0.85 + Math.random() * 0.3)));
      if (p.shield > 0) {
        const absorbed = Math.min(p.shield, dmg);
        p.shield -= absorbed; dmg -= absorbed;
        if (absorbed) log(`Your ward absorbs ${absorbed}.`, 'cl-dim');
      }
      if (dmg > 0) { S.chr.hp -= dmg; document.dispatchEvent(new CustomEvent('emberline:sfx', { detail: 'hurt' })); }
      return dmg;
    };

    async function playerTurn(tid) {
      if (over) return;
      const t = TECHNIQUES[tid];
      const qiCost = Math.round(t.qi * derived().qiCostMult);
      if (S.chr.qi < qiCost || (p.cds[tid] || 0) > 0) return;
      S.chr.qi -= qiCost;
      if (t.cd) p.cds[tid] = t.cd + 1;
      render(true);
      const dd = derived();
      const fx = t.fx || {};
      if (t.kind === 'strike') {
        // the hand remembers: techniques sharpen with use
        S.chr.techUses[tid] = (S.chr.techUses[tid] || 0) + 1;
        if (S.chr.techUses[tid] === 25) log(`<b>✦ ${t.name} has entered your bones.</b> Twenty-five real uses; +15% power from here on.`, 'cl-crit');
        const evolved = (S.chr.techUses[tid] || 0) >= 25 ? 1.15 : 1;
        const critW = 12 + (S.chr.lineage === 'glass' ? 8 : 0);
        const sres = await strikeBar({ host: m.body.querySelector('#combat-strike'), critPct: critW });
        const hits = fx.hits || 1;
        let total = 0, crit = false;
        // desperation: the Turning Palm returns what was taken
        const desper = fx.desperation ? 1 + (1 - S.chr.hp / dd.hpMax) * 1.5 : 1;
        for (let i = 0; i < hits; i++) {
          let critChance = (dd.crit + (fx.critBonus || 0) + p.critAll) / 100;
          // reactions: a burning foe is a readable foe
          let reactMult = 1;
          if (e.dot && t.el === 'smoke') { e.dodgeUp = 0; reactMult = 1.15; }
          if (e.dot && t.el === 'glass') critChance += 0.3;
          const isCrit = sres === 'crit' || chance(critChance);
          crit = crit || isCrit;
          let raw = dd.atk * t.pow * dd.dmgMult * p.buffMult * evolved * desper * reactMult * (isCrit ? 1.8 : 1) * (sres === 'weak' ? 0.7 : 1);
          total += dealToEnemy(raw, t.el, fx.poise || 1);
        }
        const reacted = e.dot && (t.el === 'smoke' || t.el === 'glass');
        log(`${t.name}${hits > 1 ? ` ×${hits}` : ''} hits for <b>${total}</b>${crit ? ' — a telling blow!' : ''}${reacted ? ' <i>(the burn betrays its guard)</i>' : ''}${sres === 'weak' ? ' (off-balance)' : ''}`, crit ? 'cl-crit' : '');
        if (fx.dot && total > 0) { e.dot = { dmg: Math.max(2, Math.round(dd.atk * fx.dot.dmg)), turns: fx.dot.turns }; log(`${esc(e.name)} catches fire.`, 'cl-good'); }
        if (fx.stun && total > 0 && chance(0.5)) { e.stun = 1; log(`${t.name}'s weight staggers ${esc(e.name)} — it loses its footing!`, 'cl-good'); }
      } else if (t.kind === 'guard') {
        if (fx.shieldBody) { p.shield += dd.body * fx.shieldBody; log(`${t.name}: a ward of ${dd.body * fx.shieldBody} rises around you.`, 'cl-good'); }
        if (fx.dodge) { p.dodge += fx.dodge; p.counter = fx.counter || 0; log(`${t.name}: you fade from the pattern of the fight.`, 'cl-good'); }
        if (fx.halve) { p.halve += fx.halve; log(`${t.name}: evening wraps you close.`, 'cl-good'); }
      } else if (t.kind === 'heal') {
        const amt = Math.round(dd.hpMax * (fx.healPct || 0.3));
        S.chr.hp = Math.min(dd.hpMax, S.chr.hp + amt);
        log(`${t.name} knits ${amt} health back.`, 'cl-good');
      } else { // utility
        if (fx.stun) { e.stun = fx.stun; log(`${t.name}: ${esc(e.name)} is bound fast!`, 'cl-good'); }
        if (fx.buff) { p.buffMult = fx.buff.mult; p.buffTurns = fx.buff.turns; log(`${t.name}: your marrow glows with forging heat.`, 'cl-good'); }
        if (fx.critAll) { p.critAll += fx.critAll; log(`${t.name}: you see the hollow places in its guard.`, 'cl-good'); }
        if (fx.dodge) { p.dodge += fx.dodge; p.counter = fx.counter || 0; log(`${t.name}: you burn distance itself.`, 'cl-good'); }
      }
      await afterPlayer();
    }

    function playerDefend() {
      if (over) return;
      p.defend = true;
      const dd = derived();
      S.chr.qi = Math.min(dd.qiMax, S.chr.qi + Math.round(dd.qiMax * 0.12));
      log('You set your stance and breathe — qi returns.', 'cl-dim');
      afterPlayer();
    }
    function playerPill(id) {
      if (over) return;
      const use = ITEMS[id].use; const dd = derived();
      addItem(id, -1);
      if (use.hpPct) S.chr.hp = Math.min(dd.hpMax, S.chr.hp + Math.round(dd.hpMax * use.hpPct));
      if (use.qiPct) S.chr.qi = Math.min(dd.qiMax, S.chr.qi + Math.round(dd.qiMax * use.qiPct));
      log(`You swallow a ${ITEMS[id].name}.`, 'cl-good');
      afterPlayer();
    }
    function playerFlee() {
      if (over) return;
      const dd = derived();
      const ok = opts.spar || S.chr.lineage === 'smoke' || S.chr.dao === 'void' || chance(0.5 + dd.fate * 0.02);
      if (ok) { end('flee'); }
      else { log('You turn to run — it cuts off your escape!', 'cl-bad'); enemyTurn().then(() => { if (!over) render(); }); }
    }

    async function afterPlayer() {
      // tick enemy dot
      if (e.dot) { e.hp -= e.dot.dmg; log(`The burn gnaws ${esc(e.name)} for ${e.dot.dmg}.`, 'cl-dim'); if (--e.dot.turns <= 0) e.dot = null; }
      if (e.hp <= 0) return end('win');
      render(true);
      await sleep(650);
      await enemyTurn();
      if (over) return;
      // cooldowns & buff decay at start of player turn
      for (const k of Object.keys(p.cds)) p.cds[k] = Math.max(0, p.cds[k] - 1);
      if (p.buffTurns > 0 && --p.buffTurns === 0) p.buffMult = 1;
      if (p.dot) { S.chr.hp -= p.dot.dmg; log(`Your burns sear you for ${p.dot.dmg}.`, 'cl-bad'); if (--p.dot.turns <= 0) p.dot = null; }
      if (S.chr.hp <= 0) return end('lose');
      if (p.stun > 0) { p.stun--; log('You are bound — the turn slips past you.', 'cl-bad'); render(true); await sleep(650); await enemyTurn(); if (over) return; }
      render();
    }

    async function enemyTurn() {
      if (over) return;
      if (e.staggered > 0) {
        e.staggered--;
        if (e.staggered === 0) { e.poise = e.poiseMax; log(`${esc(e.name)} finds its footing again.`, 'cl-dim'); }
        else log(`${esc(e.name)} reels, guard in pieces — the turn is yours entirely.`, 'cl-good');
        pickIntent(); return;
      }
      if (e.stun > 0) { e.stun--; log(`${esc(e.name)} strains against its bindings.`, 'cl-dim'); pickIntent(); return; }
      const it = e.intent;
      const dd = derived();
      if (it.t === 'hit' || it.t === 'heavy') {
        if (p.dodge > 0) {
          p.dodge--;
          log(`${esc(e.name)} strikes — and finds only ash where you stood.`, 'cl-good');
          if (p.counter) {
            const c = Math.max(1, Math.round(dd.atk * p.counter));
            e.hp -= c; log(`Your counter lands for <b>${c}</b>.`, 'cl-crit');
            if (e.hp <= 0) return end('win');
          }
        } else {
          const dmg = dealToPlayer(e.atk * (it.mult || 1));
          log(`${esc(e.name)} ${it.t === 'heavy' ? 'crashes into you' : 'strikes'} for <b>${dmg}</b>.`, 'cl-bad');
        }
      } else if (it.t === 'guard') { e.guard = 1; log(`${esc(e.name)} hunkers behind its guard.`, 'cl-dim'); }
      else if (it.t === 'dodgeup') { e.dodgeUp = 1; log(`${esc(e.name)} becomes hard to look at directly.`, 'cl-dim'); }
      else if (it.t === 'drain') {
        const amt = Math.min(S.chr.qi, 8 + (def.tier || 1) * 5);
        S.chr.qi -= amt; e.hp = Math.min(e.hpMax, e.hp + amt);
        log(`${esc(e.name)} drinks ${amt} of your Emberlight.`, 'cl-bad');
      } else if (it.t === 'dot') {
        p.dot = { dmg: Math.max(2, Math.round(e.atk * 0.35)), turns: 3 };
        log(`${esc(e.name)} sets a lingering burn into your robes.`, 'cl-bad');
      } else if (it.t === 'stun') {
        if (chance(0.6)) { p.stun = 1; log(`${esc(e.name)}'s binding art snares you!`, 'cl-bad'); }
        else log(`${esc(e.name)}'s binding art misses.`, 'cl-dim');
      }
      if (e.guard && it.t !== 'guard') e.guard = 0;
      p.defend = false;
      if (S.chr.hp <= 0) return end('lose');
      pickIntent();
    }

    async function end(result) {
      if (over) return; over = true;
      if (result === 'win' && !opts.spar) {
        const stones = ri(def.stones[0], def.stones[1]);
        S.chr.stones += stones;
        const cult = Math.round((4 + (def.tier || 0) * 7) * derived().insightMult);
        S.chr.cult += cult;
        const drops = [];
        for (const [iid, pr] of Object.entries(def.loot || {})) {
          if (chance(pr)) { addItem(iid, 1); drops.push(ITEMS[iid].name); }
        }
        log(`<b>${esc(e.name)} falls.</b> +${stones} stones, +${cult} cultivation${drops.length ? `, loot: ${drops.join(', ')}` : ''}.`, 'cl-crit');
        if (def.boss) { if (e.id) S.meta.bossesSlain.push(e.id); S.chr.deeds.push(`Slew ${def.name}`); }
        if (def.rival) S.chr.deeds.push(`Defeated ${def.name}`);
        render(true);
        await sleep(1400);
      } else if (result === 'win' && opts.spar) {
        log('<b>The bout is yours.</b> Your senior nods, freshly bruised and delighted.', 'cl-crit');
        render(true); await sleep(1000);
      } else if (result === 'flee') {
        log(opts.spar ? 'You yield the bout.' : 'You break away and run until the world goes quiet.', 'cl-dim');
        render(true); await sleep(800);
      } else if (result === 'lose') {
        S.chr.hp = 0;
        log('<b>Darkness takes the field.</b>', 'cl-bad');
        render(true); await sleep(1200);
      }
      m.close();
      resolve(result);
    }

    log(opts.intro || `${esc(def.name)} — ${esc(def.desc)}`, 'cl-dim');
    render();
  });
}

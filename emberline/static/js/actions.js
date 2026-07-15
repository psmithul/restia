// The gameplay engine: actions, events, seasons, sect, alchemy, breakthroughs, succession.
import {
  REGIONS, REALMS, ITEMS, TECHNIQUES, EVENTS, JOBS, MISSIONS, RECIPES, LINEAGES,
  SECT_RANKS, PERKS, QUESTS, SEASONS, ACTION_DEFS, ENEMIES,
  WEATHER, NPCS, ASPECTS, FLAWS, VOWS, WOUNDS, DREAMS, RIVALS, NEWS_TEMPLATES,
  DOMAINS, ROAD_EVENTS, AUCTION_POOL, STORY_BEATS,
  INSIGHTS, DAOS, NPC_ARCS, DISCOVERIES,
} from './data.js';
import {
  S, derived, persist, addItem, hasItems, atPeak, stageCost, realmLabel, sectRank,
  createCharacter, AP_PER_SEASON, perkLevel, healHp, healQi,
} from './state.js';
import { ri, pick, chance, pickWeighted, rollStat, esc, clamp } from './util.js';
import { openModal } from './modal.js';
import { breathing, pillfire, ashfall, forageGrid } from './minigames.js';
import { startCombat } from './combat.js';

export const region = () => REGIONS.find((r) => r.id === S.world.region);

export function log(text, cls = '') {
  S.log.push({ t: text, cls, when: `${dateShort()}` });
  if (S.log.length > 150) S.log.shift();
}
const dateShort = () => `Y${S.world.year} ${SEASONS[S.world.season].slice(0, 3)}`;

export function touch() {
  persist();
  document.dispatchEvent(new CustomEvent('emberline:refresh'));
  checkStory();
}

// The Chronicle: when a life crosses a threshold, the novel writes its next chapter.
let storyShowing = false;
export function checkStory() {
  if (!S || storyShowing) return;
  const seen = new Set(S.meta.chronicle.map((c) => c.id));
  const beat = STORY_BEATS.find((b) => !seen.has(b.id) && b.when(S));
  if (!beat) return;
  const entry = { id: beat.id, book: beat.book, title: beat.title, text: beat.text(S), when: dateShort(), gen: S.meta.generation };
  S.meta.chronicle.push(entry);
  storyShowing = true;
  persist();
  document.dispatchEvent(new CustomEvent('emberline:story', { detail: entry }));
}
export function storyClosed() { storyShowing = false; checkStory(); }

function spendAP(n) {
  if (S.world.ap < n) { toast('Not enough time left this season. Rest by ending the season.'); return false; }
  S.world.ap -= n;
  return true;
}
export const toast = (t) => document.dispatchEvent(new CustomEvent('emberline:toast', { detail: t }));
export const sfxE = (name) => document.dispatchEvent(new CustomEvent('emberline:sfx', { detail: name }));

// ---------------- Insights: the world teaches; the mat only collects ----------------
export function awardInsight(id) {
  if (!INSIGHTS[id] || S.chr.insights.includes(id)) return;
  S.chr.insights.push(id);
  S.chr.cult += 15;
  sfxE('chime');
  log(`<b>✧ Insight: ${INSIGHTS[id].name}.</b> <i>${INSIGHTS[id].desc}</i> (+15 cultivation; all cultivation now +${S.chr.insights.length * 6}%${S.chr.insights.length % 3 === 0 ? '; tribulations endure +1 more bolt' : ''})`, 'l-story');
}

// ---------------- karma & the Wheel ----------------
const OMENS_GOOD = [
  '<i>A stray dog falls in beside you for a mile, escorting.</i>',
  '<i>Incense smoke bends toward you at the shrine, as if leaning in.</i>',
  '<i>A crow drops a copper button at your feet and waits, as though settling a debt.</i>',
];
const OMENS_BAD = [
  '<i>Three crows follow you at rooftop height, keeping accounts.</i>',
  '<i>Your reflection in the rain barrel is a half-breath slow.</i>',
  '<i>The shrine lamp gutters when you pass. Granny Ash pretends not to notice.</i>',
];
export function addKarma(n) {
  if (!n) return;
  if (S.chr.dao === 'wheel') n *= 2;
  S.chr.karma += n;
  if (n <= -2) log(pick(OMENS_BAD), 'l-dim');
  else if (n >= 2) log(pick(OMENS_GOOD), 'l-dim');
}
export function karmaWord() {
  const k = S.chr.karma;
  if (k >= 10) return 'serene';
  if (k >= 3) return 'favorable';
  if (k > -3) return 'turning';
  if (k > -10) return 'troubled';
  return 'grinding';
}
export function pushNews(text) {
  S.world.news.unshift({ text, when: `Y${S.world.year} ${SEASONS[S.world.season].slice(0, 3)}` });
  if (S.world.news.length > 8) S.world.news.pop();
}
export function weatherNow() {
  return WEATHER[S.world.season]?.find((x) => x.id === S.world.weather) || null;
}
export function rollWeather() {
  const w = pick(WEATHER[S.world.season]);
  S.world.weather = w.id;
  log(`<i>${w.name} — ${w.desc}</i>`, 'l-dim');
  return w;
}
function addWound(id) {
  if (S.chr.wounds.includes(id)) return;
  S.chr.wounds.push(id);
  log(`<b>Wound taken: ${WOUNDS[id].name}.</b> ${WOUNDS[id].desc} <i>(It will linger until treated.)</i>`, 'l-bad');
}
export function treatWound(id) {
  const i = S.chr.wounds.indexOf(id);
  if (i >= 0) { S.chr.wounds.splice(i, 1); log(`<i>The ${WOUNDS[id].name.toLowerCase()} finally mends.</i>`, 'l-gain'); }
}

// ---------------- outcome DSL ----------------
export function reqMet(req) {
  if (!req) return true;
  if (req.stones && S.chr.stones < req.stones) return false;
  if (req.items && !hasItems(req.items)) return false;
  if (req.flag && !S.chr.flags[req.flag]) return false;
  if (req.notFlag && S.chr.flags[req.notFlag]) return false;
  if (req.flagMin && (S.chr.flags[req.flagMin[0]] || 0) < req.flagMin[1]) return false;
  if (req.legacyFlag && !S.meta.legacyFlags?.[req.legacyFlag]) return false;
  if (req.stat) for (const [k, v] of Object.entries(req.stat)) if (derived()[k] < v) return false;
  return true;
}

export async function resolveOut(out) {
  if (!out) return;
  const d = derived();
  if (out.roll) {
    const r = rollStat(d[out.roll.stat] + d.checkBonus, out.roll.dc, d.fate);
    log(`<i>(${out.roll.stat} check: ${r.roll} vs ${r.target} — ${r.ok ? 'success' : 'failure'})</i>`, 'l-dim');
    await resolveOut(r.ok ? out.roll.win : out.roll.lose);
    return;
  }
  if (out.sadhu) { await sadhuAudience(); return; }
  if (out.fight) {
    const res = await startCombat(out.fight);
    if (res === 'win') {
      if (out.winFlag) S.chr.flags[out.winFlag] = true;
      if (out.winLog) log(out.winLog, 'l-story');
      if (out.winStones) { S.chr.stones += out.winStones; log(`<b>+${out.winStones} stones.</b>`, 'l-gain'); }
      if (out.winFlag === 'rival_settled') S.chr.deeds.push('Settled the rivalry with Yan Shuo');
    } else if (res === 'lose') {
      if (out.loseLog) log(out.loseLog, 'l-story');
      await handleDefeat(!!out.deadly, out.fight);
    } else if (out.loseLog && res === 'flee') { /* fled: nothing */ }
    touch();
    return;
  }
  if (out.karma) addKarma(out.karma);
  if (out.insight) awardInsight(out.insight);
  if (out.revealKarma) log(`<b>The scales read: ${S.chr.karma}.</b> The Wheel's hum around you is <b>${karmaWord()}</b>.`, 'l-story');
  if (out.dreamScar) addWound('fevered_blood');
  if (out.stones) S.chr.stones = Math.max(0, S.chr.stones + out.stones);
  if (out.items) for (const [id, q] of Object.entries(out.items)) addItem(id, q);
  if (out.cult) S.chr.cult += out.cult;
  if (out.hp) S.chr.hp = clamp(S.chr.hp + out.hp, 1, d.hpMax);
  if (out.hpPct) healHp(out.hpPct);
  if (out.qiPct) healQi(out.qiPct);
  if (out.fate) { S.chr.stats.fate += out.fate; S.chr.deeds.push('Earned the hearth gods’ favor'); }
  if (out.fateSmall && chance(0.4)) { S.chr.stats.fate += 1; log('<i>Small luck sticks to your heels. (+1 Fate)</i>', 'l-gain'); }
  if (out.contrib) { S.chr.sect.contrib += out.contrib; S.chr.sect.totalContrib += out.contrib; }
  if (out.tech && !S.chr.techs.includes(out.tech)) {
    S.chr.techs.push(out.tech);
    if (S.chr.deck.length < 4 && !TECHNIQUES[out.tech].fx?.passiveMeditate) S.chr.deck.push(out.tech);
    log(`<b>Technique learned: ${TECHNIQUES[out.tech].name}.</b>`, 'l-gain');
  }
  if (out.flag) S.chr.flags[out.flag] = true;
  if (out.flagInc) S.chr.flags[out.flagInc] = (S.chr.flags[out.flagInc] || 0) + 1;
  if (out.log) log(out.log, 'l-story');
  touch();
}

async function handleDefeat(deadly, enemyId) {
  if (deadly) {
    log(`<b>${esc(ENEMIES[enemyId]?.name || 'The foe')} does not grant mercy. Your story ends here — but the Hearthline endures.</b>`, 'l-bad');
    await die('slain');
    return;
  }
  const d = derived();
  const lost = Math.floor(S.chr.stones * 0.25);
  S.chr.stones -= lost;
  S.chr.hp = Math.max(1, Math.round(d.hpMax * 0.1));
  S.chr.injured = 1;
  log(`You wake where you fell, lighter by ${lost} stones, heavier by one lesson. You'll be nursing bruises next season.`, 'l-bad');
  awardInsight('in_near_death');
  if (chance(0.4)) addWound(pick(['cracked_rib', 'torn_meridian', 'ash_blind']));
  touch();
}

// ---------------- region actions ----------------
export async function doRegionAction(kind) {
  const r = region();
  const def = ACTION_DEFS[kind];
  const vow = S.chr.vows.find((v) => VOWS[v.id].forbids.includes(kind));
  if (vow) { toast(`Your ${VOWS[vow.id].name} forbids this for ${vow.seasonsLeft} more season${vow.seasonsLeft === 1 ? '' : 's'}. (Break it at the Austerities mat, if you must.)`); return; }
  switch (kind) {
    case 'talk': return actTalk(r, def);
    case 'austerities': return actAusterities(def);
    case 'auction': return actAuction(def);
    case 'arena': return actArena(def);
    case 'ziggurat_heart': return actZigguratHeart(def);
    case 'tide_court': return actTideCourt(def);
    case 'raise': return actRaiseHeir(def);
    case 'forge': return actForge(def);
    case 'challenge': return actRankDuel(def);
  }
  if (kind.startsWith('disc_')) return actDiscovery(kind.slice(5));
  switch (kind) {
    case 'meditate': return actMeditate(r, def);
    case 'rest': return actRest(def);
    case 'jobs': return actJobs(def);
    case 'wander': return actWander(r, def);
    case 'forage': return actForage(r, def);
    case 'hunt': return actHunt(r, def);
    case 'mine': return actMine(def);
    case 'listings': return actListings(def);
    case 'alchemy': return actAlchemy(def);
    case 'spar': return actSpar(def);
    case 'expedition': return actExpedition(def);
    case 'temple_depths': return actTempleDepths(def);
    case 'crater_heart': return actCraterHeart(def);
    case 'market': case 'shrine': case 'missions': case 'library': case 'store':
      if (kind === 'shrine' && S.meta.generation === 1 && !S.chr.flags.tut_shrine) {
        S.chr.flags.tut_shrine = true; S.chr.stones += 10;
        log('<i>Granny Ash hands you the shrine broom before you can speak. You sweep. She nods, satisfied, and pays you a keeper’s coin. (+10 stones)</i>', 'l-dim');
      }
      document.dispatchEvent(new CustomEvent('emberline:open', { detail: kind }));
      return;
  }
}

async function actMeditate(r, def, bonusMult = 1) {
  if (!spendAP(def.ap)) return;
  const m = openModal({ title: 'Meditation', locked: true });
  const res = await breathing({ host: m.body });
  m.close();
  const d = derived();
  const wx = weatherNow();
  const asp = S.chr.aspect ? ASPECTS[S.chr.aspect].fx : {};
  let mult = d.meditate + (wx?.fx.medBonus || 0);
  if (asp.veinMed && (r.id === 'mines' || r.id === 'crater')) mult += asp.veinMed;
  if (S.chr.flags.lamplit && S.world.season === 3) mult += 0.1;
  if (S.chr.flags.kept_flame) mult += 0.2;
  const dim = [1, 0.55, 0.3, 0.15][Math.min(S.world.medCount, 3)];
  S.world.medCount += 1;
  const gain = Math.round((10 + d.spirit * 2.2) * r.qiMult * mult * res.mult * dim * d.insightMult * bonusMult);
  if (S.meta.generation === 1 && !S.chr.flags.tut_meditate) { S.chr.flags.tut_meditate = true; S.chr.stones += 10; log('<i>Granny Ash, passing: "One good breath. That’s the whole art; the rest is repetition." She presses a coin into your hand for listening. (+10 stones)</i>', 'l-dim'); }
  S.chr.cult += gain;
  healQi(0.35);
  const flavor = res.perfects >= 6 ? 'Your breath and the world’s breath become one rhythm.'
    : res.mult >= 1.2 ? 'The ember veins answer you willingly.'
    : res.mult >= 0.8 ? 'A steady session; the Emberlight comes in threads.'
    : 'Your thoughts scatter like startled birds. Still, something is gathered.';
  log(`${flavor} <b>+${gain} cultivation.</b>${res.auto ? ' <i>(quiet sitting)</i>' : ''}${dim < 1 ? ' <i>(the veins run thin from your drinking — try again next season, or cultivate through deeds)</i>' : ''}${S.chr.hungry ? ' <i>(hunger gnaws at your focus)</i>' : ''}`, 'l-gain');
  maybeStageHint();
  touch();
}

// ---------------- people ----------------
function relTier(rel) { return Math.min(4, Math.floor(rel / 2)); }
async function actTalk(r, def) {
  const locals = Object.entries(NPCS).filter(([, n]) => n.region === r.id);
  if (!locals.length) { toast('No one here knows you well enough to sit with.'); return; }
  const pickNpc = () => new Promise((resolve) => {
    if (locals.length === 1) return resolve(locals[0]);
    const m = openModal({ title: 'Whose fire do you join?' });
    m.body.innerHTML = `<div class="event-choices">${locals.map(([id, n]) =>
      `<button class="btn btn-choice" data-npc="${id}"><b>${n.name}</b><small>${n.title} · kinship ${S.chr.rel[id] || 0}</small></button>`).join('')}</div>`;
    m.body.querySelectorAll('[data-npc]').forEach((b) => b.addEventListener('click', () => {
      m.close(); resolve(locals.find(([id]) => id === b.dataset.npc));
    }));
  });
  const picked = await pickNpc();
  if (!picked) return;
  if (!spendAP(def.ap)) return;
  const [id, npc] = picked;
  const rel = S.chr.rel[id] || 0;
  const line = npc.lines[Math.min(relTier(rel), npc.lines.length - 1)];
  S.chr.rel[id] = rel + 1;
  log(`<b>${npc.name}</b> — ${line}`, 'l-story');
  // personal arcs: kinship opens doors that talk alone cannot
  const arcStep = (NPC_ARCS[id] || []).find((st) => S.chr.rel[id] >= st.at && !S.chr.flags[`arc_${id}_${st.at}`]);
  if (arcStep) {
    S.chr.flags[`arc_${id}_${arcStep.at}`] = true;
    const am = openModal({ title: `${npc.name} — a door opens`, locked: true });
    am.body.innerHTML = `<p class="event-text">${arcStep.text}</p><div class="event-choices"><button class="btn btn-choice" data-ok>Carry it with you</button></div>`;
    await new Promise((res) => am.body.querySelector('[data-ok]').addEventListener('click', () => { am.close(); res(); }));
    await resolveOut(arcStep.out);
  }
  if (S.chr.rel[id] === 6) awardInsight('in_kinship');
  if (chance(0.5) && S.world.news.length) log(`<i>Talk drifts to the wider world: "${S.world.news[0].text}"</i>`, 'l-dim');
  if ((S.chr.inventory[npc.gift] || 0) > 0 && S.chr.rel[id] < 8) {
    const give = await confirmModal(`A gift for ${npc.name}?`,
      `You are carrying ${ITEMS[npc.gift].name} — exactly the sort of thing ${npc.name} treasures. Offer it? <i>(deepens kinship considerably)</i>`);
    if (give) {
      addItem(npc.gift, -1);
      S.chr.rel[id] += 2;
      log(`${npc.name} turns the ${ITEMS[npc.gift].name} over twice, says nothing elaborate, and finds it a place of honor. <i>(kinship deepens)</i>`, 'l-gain');
    }
  }
  if (S.chr.rel[id] === 6) log(`<b>${npc.name} counts you as kin now.</b> <i>${npc.perk}</i>`, 'l-story');
  // Granny treats a wound for close kin, once a season
  if (id === 'granny_ash' && S.chr.rel[id] >= 6 && S.chr.wounds.length && !S.chr.flags.granny_heal) {
    S.chr.flags.granny_heal = true;
    treatWound(S.chr.wounds[0]);
    log('Granny Ash sets, salves, and lectures. The wound closes; the lecture does not.', 'l-gain');
  }
  touch();
}

// ---------------- austerities (tapas) ----------------
async function actAusterities() {
  const m = openModal({ title: 'The Austerities Mat', wide: true });
  const render = () => {
    m.body.innerHTML = `<p class="event-text">Power taken from the world is rent. Power taken from yourself is owned.
      A vow, once sworn before the Wheel, binds until kept — or broken, and the Wheel remembers breakage.</p>
      ${S.chr.vows.map((v) => `<div class="inv-row"><span><b>${VOWS[v.id].name}</b> — ${v.seasonsLeft} season${v.seasonsLeft === 1 ? '' : 's'} remain</span>
        <button class="btn btn-small btn-ghost" data-break="${v.id}">Break it</button></div>`).join('')}
      <div class="event-choices">${Object.entries(VOWS).filter(([id]) => !S.chr.vows.some((v) => v.id === id)).map(([id, v]) =>
        `<button class="btn btn-choice" data-vow="${id}"><b>${v.name}</b> <small>(${v.seasons} season${v.seasons === 1 ? '' : 's'})</small><small>${v.desc}</small></button>`).join('')}</div>`;
    m.body.querySelectorAll('[data-vow]').forEach((b) => b.addEventListener('click', () => {
      const id = b.dataset.vow;
      S.chr.vows.push({ id, seasonsLeft: VOWS[id].seasons });
      log(`<b>You swear the ${VOWS[id].name}</b> before the Wheel, the hearth, and your own two ears. ${VOWS[id].seasons} season${VOWS[id].seasons === 1 ? '' : 's'} of iron.`, 'l-story');
      m.close(); touch();
    }));
    m.body.querySelectorAll('[data-break]').forEach((b) => b.addEventListener('click', async () => {
      const id = b.dataset.break;
      const ok = await confirmModal('Break the vow?', `The Wheel keeps a ledger, and broken vows are written in red. Break the ${VOWS[id].name}?`);
      if (!ok) return;
      S.chr.vows = S.chr.vows.filter((v) => v.id !== id);
      addKarma(VOWS[id].breakPenalty.karma);
      log(`<b>The ${VOWS[id].name} breaks.</b> Something regards you, disappointed, from every reflective surface for a day.`, 'l-bad');
      render(); touch();
    }));
  };
  render();
}

// ---------------- the heir, the forge, the gate, the found places ----------------
async function actRaiseHeir(def) {
  if (S.chr.age < 25) { toast('The cradle is still empty — heirs come of the middle years. (Age 25+)'); return; }
  const key = `raised_${seasonSeed()}`;
  if (S.chr.flags[key]) { toast('The child is asleep. Even dynasties nap.'); return; }
  const hb = S.meta.heirBonus;
  const spent = hb.body + hb.mind + hb.spirit + hb.fate + hb.insights;
  if (spent >= 5) { toast('The heir is as ready as raising can make them. The rest is theirs to earn.'); return; }
  const m = openModal({ title: 'The Cradle-Side Hours', wide: true });
  m.body.innerHTML = `<p class="event-text">An hour by the hearth with the child who will carry the fire. What you give now, they keep forever.
    <span class="dim">(${spent}/5 lessons given this dynasty)</span></p>
    <div class="event-choices">
      <button class="btn btn-choice" data-r="body"><b>Wrestling and river-crossing</b><small>The heir will inherit +1 Body.</small></button>
      <button class="btn btn-choice" data-r="mind"><b>Letters and ledger-craft</b><small>The heir will inherit +1 Mind.</small></button>
      <button class="btn btn-choice" data-r="spirit"><b>The breathing games</b><small>The heir will inherit +1 Spirit.</small></button>
      <button class="btn btn-choice" data-r="fate"><b>Tales of the Wheel, told right</b><small>The heir will inherit +1 Fate.</small></button>
      ${(hb.insights < 2 && S.chr.insights.length) ? '<button class="btn btn-choice" data-r="insights"><b>Take them along, once</b><small>The heir inherits one of your insights whole.</small></button>' : ''}
    </div>`;
  m.body.querySelectorAll('[data-r]').forEach((b) => b.addEventListener('click', () => {
    m.close();
    if (!spendAP(def.ap)) return;
    S.chr.flags[key] = true;
    S.meta.heirBonus[b.dataset.r] += 1;
    const lines = {
      body: 'The child pins your wrist with both hands and refuses, on principle, to let go. Good.',
      mind: 'The child corrects your arithmetic. You were wrong on purpose. Probably.',
      spirit: 'The child holds one breath longer than you did at that age. You tell them. They hold two.',
      fate: 'You tell the Wheel-tales the way Granny told them: debts, slopes, and small dogs. The child’s luck listens.',
      insights: 'You take the child along, once, and watch them see the world crack open the way it once did for you.',
    };
    sfxE('chime');
    log(`<b>The cradle-side hour.</b> ${lines[b.dataset.r]} <i>(the heir will remember)</i>`, 'l-story');
    touch();
  }));
}

async function actForge(def) {
  const wid = S.chr.equip.weapon;
  if (!wid) { toast('Bring a weapon to the anvil. The forge does not upgrade opinions.'); return; }
  const tier = S.chr.forgeTier || 0;
  if (tier >= 5) { toast('The blade is at the metal’s limit. Past this point you’d be forging the wielder.'); return; }
  const smithKin = (S.meta.family || []).some((k) => k.vocation === 'smith') && S.world.region === 'ashfen';
  const cost = Math.round(60 * (tier + 1) * (smithKin ? 0.8 : 1));
  const needsCore = tier >= 3;
  const m = openModal({ title: 'The Anvil' });
  m.body.innerHTML = `<p class="event-text">${S.world.region === 'ashfen' ? 'Old Bo turns your weapon over twice, listening to it.' : 'A horde smith with forearms like anchor chain sets your weapon on the anvil.'}
    <b>${ITEMS[wid].name}</b> — forged to tier ${tier}${tier ? ` (+${tier} Strike)` : ''}.
    Next folding: <b>${cost} stones</b>, 1 Star-Iron${needsCore ? ', 1 Beast Core' : ''}.</p>
    <div class="event-choices">
      <button class="btn btn-choice" data-forge><b>Fold the metal — tier ${tier + 1}</b><small>+1 Strike, permanent, carried by the heirloom.</small></button>
    </div>`;
  m.body.querySelector('[data-forge]').addEventListener('click', () => {
    if (S.chr.stones < cost || (S.chr.inventory.star_iron || 0) < 1 || (needsCore && (S.chr.inventory.beast_core || 0) < 1)) {
      toast('The forge is honest: no materials, no folding.'); return;
    }
    m.close();
    if (!spendAP(def.ap)) return;
    S.chr.stones -= cost;
    addItem('star_iron', -1);
    if (needsCore) addItem('beast_core', -1);
    S.chr.forgeTier = tier + 1;
    sfxE('hit');
    log(`<b>The metal folds and remembers.</b> ${ITEMS[wid].name} rings at a new pitch — forged to tier ${tier + 1} (+${tier + 1} Strike).`, 'l-gain');
    touch();
  });
}

async function actRankDuel(def) {
  const key = `duel_y${S.world.year}`;
  if (S.chr.flags[key]) { toast('One challenge a year. The Order calls it decorum; the healers call it scheduling.'); return; }
  const ok = await confirmModal('Challenge for Rank',
    'Under Order law any disciple may challenge for standing once a year: a formal duel before the mission board, witnessed, scored, remembered. Victory is worth 120 contribution and a rung of respect. Challenge?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  S.chr.flags[key] = true;
  const duelist = { name: 'Senior of the Kindled Path', tier: Math.min(5, S.chr.realm + 1), el: 'cinder',
    hp: 50 + S.chr.realm * 85, atk: 8 + S.chr.realm * 7, stones: [0, 0], spar: true,
    desc: 'A senior with rank to defend and a reputation for defending it.',
    loot: {}, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 1.8 }, { t: 'guard', w: 1 } ] };
  const res = await startCombat(duelist, { spar: true });
  const d = derived();
  S.chr.hp = Math.max(S.chr.hp, Math.round(d.hpMax * 0.5));
  if (res === 'win') {
    S.chr.sect.contrib += 120; S.chr.sect.totalContrib += 120;
    log('<b>The duel is yours, formally witnessed.</b> +120 contribution, and the mission board clerk starts using your honorific unprompted.', 'l-gain');
  } else {
    S.chr.cult += 10;
    log('The senior wins on points and buys the tea after, which is how the Order says "again next year." (+10 cultivation)', 'l-dim');
  }
  touch();
}

async function actDiscovery(id) {
  const disc = DISCOVERIES.find((d) => d.id === id);
  if (!disc) return;
  const key = `disc_${id}_${seasonSeed()}`;
  const r = region();
  if (id === 'warm_vein' || id === 'star_pool') {
    return actMeditate(r, { ap: 1 }, 1.25);
  }
  if (S.chr.flags[key]) { toast('Once a season. Even found places have their courtesies.'); return; }
  S.chr.flags[key] = true;
  if (id === 'hunters_cache') {
    addItem(pick(['graypine_sap', 'emberdew_moss', 'marrowroot']), 1);
    S.chr.stones += ri(5, 15);
    log('<b>The Hunters’ Cache</b> yields the woods’ tithe: herbs, a little coin, and the pleasant weight of an old custom kept.', 'l-gain');
  } else if (id === 'herons_shrine') {
    addKarma(1);
    if (chance(0.4)) { S.chr.stats.fate += 0; addItem('lotus_heart', 1); log('<b>The Heron’s Shrine</b> accepts your offering. A lotus heart floats to the islet’s edge, precisely where you will find it.', 'l-gain'); }
    else log('<b>The Heron’s Shrine</b> accepts your offering. The marsh’s luck leans your way like a reed in wind.', 'l-gain');
  } else if (id === 'gate_stone') {
    healHp(0.25); S.chr.cult += 8;
    log('<b>The Held Gate.</b> You set your hands in the worn grip-marks and borrow, for a moment, the shape of holding. (+health, +8 cultivation)', 'l-gain');
  } else if (id === 'glass_garden') {
    if (!spendAP(1)) { delete S.chr.flags[key]; return; }
    addItem('glassreed', 2);
    if (chance(0.35)) addItem('suncap', 1);
    log('<b>The Glass Garden</b> rings as you harvest — acres of fused flowers, and your pack the richer for the song.', 'l-gain');
  }
  touch();
}

// ---------------- the sadhu ----------------
async function sadhuAudience() {
  const k = S.chr.karma;
  const m = openModal({ title: 'Tea with the Wanderer', locked: true });
  if (k >= 5) {
    m.body.innerHTML = `<p class="event-text">He pours tea that steams in spirals. "The Wheel speaks well of you — rarer than you'd think; it mostly complains.
      A boon, then. The old kind. Choose what deepens."</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-boon="body"><b>Bones of the mountain</b> <small>+1 Body</small></button>
        <button class="btn btn-choice" data-boon="mind"><b>The lamp behind the eyes</b> <small>+1 Mind</small></button>
        <button class="btn btn-choice" data-boon="spirit"><b>The deeper breath</b> <small>+1 Spirit</small></button>
      </div>`;
    m.body.querySelectorAll('[data-boon]').forEach((b) => b.addEventListener('click', () => {
      const stat = b.dataset.boon;
      S.chr.stats[stat] += 1;
      S.chr.deeds.push('Received a sadhu’s boon');
      log(`<b>The boon settles in like it had always lived there. +1 ${stat[0].toUpperCase() + stat.slice(1)}.</b> When you look up from your cup, the mat is empty and still warm.`, 'l-story');
      m.close(); touch();
    }));
  } else if (k > -5) {
    m.body.innerHTML = `<p class="event-text">He pours tea and watches you drink. "Neither burden nor blessing yet. A ledger still being written. Good. Most people finish theirs without noticing they held the pen." He teaches you a breathing correction so small and so important you nearly weep.</p>
      <div class="event-choices"><button class="btn btn-choice" data-done>Bow your thanks</button></div>`;
    m.body.querySelector('[data-done]').addEventListener('click', () => {
      S.chr.cult += 20; addKarma(1);
      log('<b>+20 cultivation.</b> The wanderer’s correction hums in your breath for days.', 'l-gain');
      m.close(); touch();
    });
  } else {
    m.body.innerHTML = `<p class="event-text">He pours one cup, and does not offer it. "You've been billing the world," he says, "and the collection notice is being drafted. Here is mercy: pay some of it now, to me, and I will carry it where it lightens."</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-pay><b>Give him a third of your stones</b> <small>the Wheel eases</small></button>
        <button class="btn btn-choice btn-ghost" data-refuse>Refuse</button>
      </div>`;
    m.body.querySelector('[data-pay]').addEventListener('click', () => {
      const paid = Math.floor(S.chr.stones / 3);
      S.chr.stones -= paid;
      S.chr.karma += 5;
      log(`<b>You hand over ${paid} stones.</b> He weighs the purse without looking at it. "Cheaper than the Wheel’s own rates." The air feels genuinely lighter.`, 'l-story');
      m.close(); touch();
    });
    m.body.querySelector('[data-refuse]').addEventListener('click', () => {
      log('"As you like." He drinks the single cup himself, slowly, watching you the whole time. It is the most frightening thing anyone has done to you this year.', 'l-bad');
      m.close(); touch();
    });
  }
}

function maybeStageHint() {
  if (S.chr.cult >= stageCost() && !atPeak()) log('<i>Your dantian brims — you can advance a stage from your character panel.</i>', 'l-dim');
  else if (S.chr.cult >= stageCost() && atPeak() && S.chr.realm < 5) log('<i>You stand at the Peak. The next step is a tribulation — steel yourself and break through.</i>', 'l-dim');
}

async function actRest(def) {
  if (!spendAP(def.ap)) return;
  const flaw = S.chr.flaw ? FLAWS[S.chr.flaw].fx : {};
  healHp(flaw.restPenalty ? 0.65 * (1 - flaw.restPenalty) : 0.65);
  healQi(1);
  if (S.chr.injured > 0) { S.chr.injured = 0; log('You rest properly. The bruises close; the limp fades.', 'l-gain'); }
  else log(`Hot food, a real bed, and a night ${flaw.restPenalty ? 'of moon-riddled half-sleep' : 'without portents'}. Health and Emberlight recovered.`, 'l-gain');
  if (chance(0.3)) await dreamTrial();
  touch();
}

// ---------------- dreams of the Wheel ----------------
async function dreamTrial() {
  const pool = DREAMS.filter((dr) => (dr.minGen || 1) <= S.meta.generation && !S.chr.flags[`dr_${dr.id}`]);
  if (!pool.length) return;
  const dr = pick(pool);
  S.chr.flags[`dr_${dr.id}`] = true;
  const doubled = S.chr.aspect === 'dream_walker' || S.chr.dao === 'wheel';
  awardInsight('in_deep_dream');
  return new Promise((resolve) => {
    const m = openModal({ title: 'A Dream of the Wheel', locked: true });
    m.body.innerHTML = `<p class="event-text"><i>Sleep takes you somewhere with furniture older than the sky.</i><br><br>${dr.text}</p>
      <div class="event-choices"></div>`;
    const box = m.body.querySelector('.event-choices');
    dr.choices.forEach((c) => {
      const b = document.createElement('button');
      b.className = 'btn btn-choice';
      b.textContent = c.label;
      b.addEventListener('click', async () => {
        m.close();
        const out = { ...c.out };
        if (doubled) { if (out.cult) out.cult *= 2; if (out.stones) out.stones *= 2; }
        await resolveOut(out);
        if (doubled) log('<i>(Dream-Walker: what you carried back weighs double.)</i>', 'l-dim');
        resolve();
      });
      box.appendChild(b);
    });
  });
}

function actJobs(def) {
  if (!spendAP(def.ap)) return;
  const j = pick(JOBS);
  const pay = ri(j.stones[0], j.stones[1]);
  S.chr.stones += pay;
  log(`${j.label}. ${j.line} <b>+${pay} stones.</b>`, 'l-gain');
  touch();
}

async function actWander(r, def) {
  if (!spendAP(def.ap)) return;
  // found places: the world rewards walking it
  const undisc = DISCOVERIES.find((d) => d.region === r.id && !S.world.discovered.includes(d.id));
  if (undisc && chance(undisc.chance)) {
    S.world.discovered.push(undisc.id);
    sfxE('chime');
    log(`<b>✦ Discovered: ${undisc.name}.</b> ${undisc.found} <i>(a new place opens in ${r.name}, permanently)</i>`, 'l-story');
    touch();
    return;
  }
  const pool = EVENTS.filter((e) =>
    e.regions.includes(r.id)
    && (e.minRealm || 0) <= S.chr.realm
    && (e.minGen || 1) <= S.meta.generation
    && (!e.once || !S.chr.flags[`ev_${e.id}`])
    && reqMet(e.req));
  if (!pool.length || chance(0.2)) {
    log(pick(r.flavor), 'l-story');
    if (chance(0.5)) { const c = ri(2, 8); S.chr.stones += c; log(`<i>You find ${c} stones someone’s pouch gave up on.</i>`, 'l-dim'); }
    touch();
    return;
  }
  const ev = pickWeighted(pool, (e) => e.w || 1);
  if (ev.once) S.chr.flags[`ev_${ev.id}`] = true;
  await showEvent(ev);
}

function showEvent(ev) {
  return new Promise((resolve) => {
    const m = openModal({ title: 'An Encounter', locked: true });
    m.body.innerHTML = `<p class="event-text">${ev.text}</p><div class="event-choices"></div>`;
    const box = m.body.querySelector('.event-choices');
    ev.choices.forEach((c) => {
      const ok = reqMet(c.req);
      const b = document.createElement('button');
      b.className = `btn btn-choice ${ok ? '' : 'is-disabled'}`;
      b.disabled = !ok;
      b.innerHTML = `${esc(c.label)}${!ok ? ' <small>(beyond your means)</small>' : ''}`;
      b.addEventListener('click', async () => {
        m.close();
        await resolveOut(c.out);
        resolve();
      });
      box.appendChild(b);
    });
  });
}

async function actForage(r, def) {
  if (!spendAP(def.ap)) return;
  const m = openModal({ title: 'Foraging', locked: true });
  const res = await forageGrid({ host: m.body, herbCount: 5, hazardCount: 3, reveals: 8 });
  m.close();
  let n = res.herbs + (S.chr.lineage === 'tinder' && res.herbs > 0 ? 1 : 0)
    + (weatherNow()?.fx.forageBonus && res.herbs > 0 ? 1 : 0);
  const found = {};
  for (let i = 0; i < n; i++) {
    const h = pickWeighted(r.herbs, (id) => Math.min(8, 90 / ITEMS[id].price));
    found[h] = (found[h] || 0) + 1;
    addItem(h, 1);
  }
  const names = Object.entries(found).map(([id, q]) => `${ITEMS[id].name}×${q}`).join(', ');
  if (S.meta.generation === 1 && !S.chr.flags.tut_forage) { S.chr.flags.tut_forage = true; S.chr.stones += 10; log('<i>Granny Ash inspects your gathering on your return: "Something green. Good. The vale feeds its own." (+10 stones)</i>', 'l-dim'); }
  if (n) log(`You come back with ${names}.${S.chr.lineage === 'tinder' ? ' <i>(Greenflame finds one more.)</i>' : ''}`, 'l-gain');
  else log('The ground keeps its secrets today.', 'l-dim');
  if (res.hazards > 0 && S.chr.aspect === 'iron_boned' && chance(0.5)) {
    log('<i>Iron-Boned: the den snaps shut on your forearm and audibly regrets it.</i>', 'l-dim');
    res.hazards = 0;
  }
  if (res.hazards > 0) {
    if (chance(0.45 + (S.chr.flaw === 'loud_fate' ? 0.12 : 0))) {
      log('One of the dens was occupied — and awake.', 'l-bad');
      const foe = pick(r.enemies);
      const cres = await startCombat(foe);
      if (cres === 'lose') await handleDefeat(false, foe);
    } else {
      const dmg = res.hazards * 8;
      S.chr.hp = Math.max(1, S.chr.hp - dmg);
      log(`Teeth, briefly. You escape with scratches. <b>−${dmg} health.</b>`, 'l-bad');
      if (chance(0.25)) addWound(pick(['cracked_rib', 'ash_blind']));
    }
  }
  touch();
}

async function actHunt(r, def) {
  if (!spendAP(def.ap)) return;
  const foe = pick(r.enemies);
  const res = await startCombat(foe);
  if (res === 'win') {
    log(`You bring down a ${ENEMIES[foe].name} in ${r.name}.`, 'l-gain');
    awardInsight('in_first_blood');
    if (chance(0.45)) { addItem('provisions', 1); log('<i>You dress the carcass properly. +1 Provisions — the beast feeds you twice.</i>', 'l-dim'); }
    const mis = S.chr.sect.mission;
    if (mis && mis.kind === 'hunt' && mis.enemy === foe && !mis.done) {
      mis.done = true;
      log(`<i>Mission complete: ${mis.name}. Report to the board.</i>`, 'l-gain');
    }
  } else if (res === 'lose') await handleDefeat(false, foe);
  touch();
}

async function actMine(def) {
  if (!spendAP(def.ap)) return;
  const stones = ri(10, 26);
  S.chr.stones += stones;
  let extra = '';
  if (chance(0.6)) { addItem('vein_ore', 1); extra += ', a lump of Vein Ore'; }
  if (chance(0.15)) { addItem('veinbloom', 1); extra += ', a Veinbloom'; }
  log(`A day in the warm dark. <b>+${stones} stones</b>${extra}.`, 'l-gain');
  if (chance(0.25 + (weatherNow()?.fx.mineHazard || 0))) {
    if (chance(0.5)) {
      log('Your lamp gutters — bandits work this seam too.', 'l-bad');
      const res = await startCombat('vein_bandit');
      if (res === 'lose') await handleDefeat(false, 'vein_bandit');
    } else {
      const dmg = S.chr.aspect === 'iron_boned' ? ri(4, 7) : ri(8, 14);
      S.chr.hp = Math.max(1, S.chr.hp - dmg);
      log(`A rockfall clips you on the way out. <b>−${dmg} health.</b>`, 'l-bad');
      if (chance(0.2) && S.chr.aspect !== 'iron_boned') addWound('cracked_rib');
    }
  }
  touch();
}

async function actListings(def) {
  if (!spendAP(def.ap)) return;
  const jobs = [
    { name: 'Caravan guard to the dune road', fight: 'glasswing_mantis', pay: 70 },
    { name: 'Collect a debt from a "retired" bandit', fight: 'vein_bandit', pay: 90 },
    { name: 'Pit fight, one round, no blades', fight: 'cinderfang_alpha', pay: 140 },
  ];
  const j = pick(jobs);
  log(`Listing taken: <b>${j.name}</b>.`, 'l-story');
  const res = await startCombat(j.fight);
  if (res === 'win') { S.chr.stones += j.pay; log(`The client pays without haggling — a Market miracle. <b>+${j.pay} stones.</b>`, 'l-gain'); }
  else if (res === 'lose') await handleDefeat(false, j.fight);
  else log('You abandon the job. The board keeps your deposit and your pride.', 'l-dim');
  touch();
}

async function actSpar(def) {
  if (!spendAP(def.ap)) return;
  const res = await startCombat('spar_partner', { spar: true });
  const d = derived();
  if (res === 'win') {
    const cult = ri(12, 20); const contrib = 15;
    S.chr.cult += cult; S.chr.sect.contrib += contrib; S.chr.sect.totalContrib += contrib;
    S.chr.hp = Math.max(S.chr.hp, Math.round(d.hpMax * 0.5));
    log(`Wooden swords, real lessons. <b>+${cult} cultivation, +${contrib} contribution.</b>`, 'l-gain');
  } else {
    const cult = ri(6, 10);
    S.chr.cult += cult;
    S.chr.hp = Math.max(S.chr.hp, Math.round(d.hpMax * 0.5));
    log(`Your senior wins, then spends an hour showing you exactly how. <b>+${cult} cultivation.</b>`, 'l-gain');
  }
  touch();
}

const EXP_LOOT = {
  glasswaste: { line: 'The deep waste pays for its bruises', items: { glasswing: [1, 2] }, extra: { suncap: 0.5 } },
  serpents_spine: { line: 'The high passes pay in thunder-herbs', items: { stormbud: [1, 2] }, extra: { suncap: 0.5, beast_core: 0.4 } },
  bone_orchard: { line: 'The old graves give up their goods, reluctantly', items: { beast_core: [1, 2] }, extra: { duskclear_pill: 0.4, marrowroot: 0.6 } },
  sunken_star: { line: 'The drowned sun pays its divers', items: { star_iron: [1, 1] }, extra: { brinebloom: 0.8, suncap: 0.3 } },
  glass_sea: { line: 'The mirror-field yields eight-hundred-year-old spoils', items: { star_iron: [1, 2] }, extra: { veinbloom: 0.6, dawnpetal: 0.12 } },
};

const DELVE_NODES = {
  fight: { icon: '⚔', name: 'Spoor', hint: 'fresh tracks — something ordinary and hungry' },
  elite: { icon: '☠', name: 'A Wrong Silence', hint: 'the birds have opinions about this direction' },
  shrine: { icon: '☲', name: 'Old Stones', hint: 'a wayside shrine still warm with someone’s prayer' },
  event: { icon: '？', name: 'Strange Light', hint: 'something worth a closer look, probably' },
  cache: { icon: '◆', name: 'Marked Ground', hint: 'a scavenger’s mark: goods below' },
};

async function actExpedition(def) {
  if (!spendAP(def.ap)) return;
  const r = region();
  const cfg = EXP_LOOT[r.id] || EXP_LOOT.glasswaste;
  const companion = Object.entries(NPCS).find(([id, n]) => n.region === r.id && (S.chr.rel[id] || 0) >= 6)
    || (r.id === 'sunken_star' && (S.chr.rel.captain_rema || 0) >= 6 ? ['captain_rema', NPCS.captain_rema] : null);
  log(`${r.labels?.expedition ? `<b>${r.labels.expedition.label}.</b>` : '<b>You rope up and go deep.</b>'}${companion ? ` ${NPCS[companion[0]].name} comes with you — kin walk in front, by custom, taking turns.` : ''}`, 'l-story');
  // three forks into the deep, then the payoff
  for (let step = 0; step < 3; step++) {
    const kinds = ['fight', 'elite', 'shrine', 'event', 'cache'].sort(() => Math.random() - 0.5).slice(0, 2);
    const choice = await new Promise((resolve) => {
      const m = openModal({ title: `The Deep Path — fork ${step + 1} of 3`, locked: true });
      m.body.innerHTML = `<div class="event-choices">${kinds.map((k) => `
        <button class="btn btn-choice" data-node="${k}"><b>${DELVE_NODES[k].icon} ${DELVE_NODES[k].name}</b><small>${DELVE_NODES[k].hint}</small></button>`).join('')}</div>`;
      m.body.querySelectorAll('[data-node]').forEach((b) => b.addEventListener('click', () => { m.close(); resolve(b.dataset.node); }));
    });
    if (choice === 'fight' || choice === 'elite') {
      let foe = pick(r.enemies);
      if (choice === 'elite') {
        const base = ENEMIES[foe];
        foe = { ...base, name: `Elder ${base.name}`, hp: Math.round(base.hp * 1.5), atk: Math.round(base.atk * 1.3),
          stones: [base.stones[0] * 2, base.stones[1] * 2], loot: { ...base.loot, beast_core: 1.0 } };
      }
      const res = await startCombat(foe);
      if (res !== 'win') {
        if (res === 'lose') await handleDefeat(false, typeof foe === 'string' ? foe : null);
        else log('You cut the expedition short and follow your own tracks home.', 'l-dim');
        touch();
        return;
      }
      if (companion && step < 2) { healHp(0.25); log(`<i>${NPCS[companion[0]].name} binds your cuts with the efficiency of someone who has done this often, for people they intend to keep.</i>`, 'l-dim'); }
    } else if (choice === 'shrine') {
      healHp(0.3); healQi(0.5); S.chr.cult += 15;
      log('<b>☲ Old stones, older warmth.</b> You rest at the wayside shrine and leave it a little warmer than you found it. (+health, +qi, +15 cultivation)', 'l-gain');
    } else if (choice === 'cache') {
      const extras = Object.keys(cfg.extra);
      addItem(pick(extras), 1); addItem(pick(extras), 1);
      S.chr.stones += ri(20, 50);
      sfxE('coin');
      log('<b>◆ The mark was honest.</b> Goods below: gleanings and coin, cached by someone who never came back for them.', 'l-gain');
    } else if (choice === 'event') {
      await actWander(r, { ap: 0 });
    }
  }
  const stones = ri(60, 140) + (r.minRealm >= 3 ? 80 : 0);
  S.chr.stones += stones;
  const got = [];
  for (const [id, range] of Object.entries(cfg.items)) {
    const q = ri(range[0], range[1]);
    if (q > 0) { addItem(id, q); got.push(`${ITEMS[id].name}×${q}`); }
  }
  for (const [id, p] of Object.entries(cfg.extra)) {
    if (chance(p)) { addItem(id, 1); got.push(ITEMS[id].name); }
  }
  if (r.id === 'sunken_star' && (S.chr.rel.captain_rema || 0) >= 6) {
    addItem('star_iron', 1); got.push('Star-Iron (Rema’s crews)');
  }
  log(`${cfg.line}: <b>+${stones} stones</b>${got.length ? `, ${got.join(', ')}` : ''}.`, 'l-gain');
  if (chance(0.6)) await actWander(r, { ap: 0 });
  touch();
}

async function actTempleDepths(def) {
  if (S.chr.flags.abbot_slain) { toast('The depths are quiet now. Only dust prays here.'); return; }
  const ok = await confirmModal('Descend the Depths',
    'Below the prayer halls something keeps the temple hollow. This fight is to the death — <b>if you fall, your story ends and your heir continues the Hearthline.</b> Descend?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  log('The stairs go down past where stairs should stop believing in themselves.', 'l-story');
  const g = await startCombat('hollow_acolyte');
  if (g !== 'win') { if (g === 'lose') await handleDefeat(false, 'hollow_acolyte'); touch(); return; }
  const res = await startCombat('hollow_abbot');
  if (res === 'win') {
    S.chr.flags.abbot_slain = true;
    awardInsight('in_apex');
    log('<b>The Hollow Abbot unravels into borrowed silence.</b> The bells above ring once, by themselves, and stop forever.', 'l-story');
  } else if (res === 'lose') await handleDefeat(true, 'hollow_abbot');
  touch();
}

async function actCraterHeart(def) {
  const frags = S.chr.inventory.hymn_fragment || 0;
  if (S.chr.flags.hymn_learned && S.chr.realm === 5 && atPeak()) return attemptAscension(def);
  if (frags >= 3 && !S.chr.flags.hymn_learned) {
    addItem('hymn_fragment', -3);
    S.chr.flags.hymn_learned = true;
    if (!S.chr.techs.includes('tenthfire_hymn')) S.chr.techs.push('tenthfire_hymn');
    S.chr.deeds.push('Reassembled the Tenthfire Hymn');
    log('<b>Three fragments align. The Tenthfire Hymn is whole again</b> — nine falling notes, and a tenth that rises. You know, now, how a dawn is kindled.', 'l-story');
    touch();
    return;
  }
  const nextBoss = ['crater_choir', 'warden_ninth'].find((b) => !S.chr.flags[`frag_${b}`]);
  if (!nextBoss) {
    if (!S.chr.flags.frag_ember_guardian) { toast('Two verses sing in your pack; the third is held by the Ember Guardian atop the Obsidian Ziggurat, far east on the Burning Steppe.'); return; }
    toast('The heart of the crater waits for you to be ready: Sunforging Peak, with the Hymn learned.');
    return;
  }
  const ok = await confirmModal('The Crater’s Heart',
    `A guardian of the old sky holds the next Hymn Fragment: <b>${ENEMIES[nextBoss].name}</b>. This fight is to the death — <b>defeat means your heir continues the Hearthline.</b> Approach?`);
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  const res = await startCombat(nextBoss);
  if (res === 'win') {
    S.chr.flags[`frag_${nextBoss}`] = true;
    awardInsight('in_apex');
    log('<b>A Hymn Fragment rises from the fallen guardian</b>, singing faintly, glad to be found.', 'l-story');
  } else if (res === 'lose') await handleDefeat(true, nextBoss);
  touch();
}

async function attemptAscension(def) {
  const ok = await confirmModal('Kindle the Tenth Dawn',
    'You stand at Sunforging Peak with the Hymn on your lips. The sky remembers what happened to the last nine suns, and it will object with everything it has. <b>This is the final tribulation. Failure is death.</b> Sing?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  log('You walk to the crater’s heart and begin the Tenthfire Hymn. The first note falls. The sky inhales.', 'l-story');
  const m = openModal({ title: 'The Final Tribulation', locked: true });
  const res = await ashfall({ host: m.body, realm: 6, grace: S.chr.grace + (S.chr.karma >= 10 ? 1 : 0), slow: S.chr.flags.slow_trib, charcoal: S.chr.lineage === 'charcoal', extraWaves: S.chr.flaw === 'heaven_marked' ? 2 : 0 });
  m.close();
  S.chr.grace = 0; S.chr.flags.slow_trib = false;
  if (res.survived) {
    sfxE('gong');
    await endingChoice();
  } else {
    log(`The sky wins the argument. ${res.taken} bolts found you; the Hymn scatters from your lips.`, 'l-bad');
    await die('the final tribulation');
  }
  touch();
}

// ---------------- the last choice under the iron sky ----------------
async function endingChoice() {
  return new Promise((resolve) => {
    const m = openModal({ title: 'The Tenth Note', locked: true, wide: true });
    const dark = S.chr.karma <= -10;
    m.body.innerHTML = `<p class="event-text">The last bolt falls and misses, and the sky — for the first time in a thousand years — <i>waits</i>.
      The tenth note stands in your throat, whole, patient, yours. A dawn is about to exist, and in this one held breath, you choose what kind.</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-end="kindle"><b>Kindle the Tenth Dawn</b><small>Rise as the Dawnbearer: a new sun, with your family’s fire at its heart.</small></button>
        <button class="btn btn-choice" data-end="share"><b>Open your hands</b><small>Pour the dawn into every hearth and vein under the sky. No sun, no Dawnbearer — one endless spring, for everyone.</small></button>
        <button class="btn btn-choice" data-end="refuse"><b>Swallow the note and walk home</b><small>Not a dawn that costs a singer. Keep the tenth flame banked in your line, for a generation that needs it more.</small></button>
        ${dark ? '<button class="btn btn-choice" data-end="usurp"><b class="bad">Take it</b><small>The Wheel owes you nothing and you owe it less. Stand a sun in the sky with a debt-collector’s face.</small></button>' : ''}
      </div>`;
    m.body.querySelectorAll('[data-end]').forEach((b) => b.addEventListener('click', () => {
      const choice = b.dataset.end;
      m.close();
      S.meta.ending = choice;
      const d0 = derived();
      if (choice === 'refuse') {
        S.chr.flags.kept_flame = true;
        addKarma(5);
        S.chr.deeds.push('Refused the Tenth Dawn and kept the flame');
        log('<b>You swallow the tenth note.</b> The sky exhales, cheated and, somehow, relieved. You walk down from the crater carrying an unsung dawn, banked behind your sternum like the shrine flame — kept, not spent. <i>(The Kept Flame: your line’s meditation burns +20% brighter, forever.)</i>', 'l-story');
      } else {
        S.chr.realm = 6; S.chr.stage = 0; S.chr.cult = 0; S.ended = true;
        const d = derived(); S.chr.hp = d.hpMax; S.chr.qi = d.qiMax;
        if (choice === 'kindle') {
          S.chr.deeds.push('Kindled the Tenth Dawn');
          log('<b>The tenth note rises. And rises. And RISES.</b> Light that owes nothing to the dead suns pours out of you and over the world. Dawn — a true dawn — breaks for the first time in a thousand years.', 'l-story');
          document.dispatchEvent(new CustomEvent('emberline:ascended'));
        } else if (choice === 'share') {
          S.chr.deeds.push('Opened the dawn to every hearth');
          S.meta.hearthflame += 100;
          log('<b>You open your hands.</b> The dawn leaves you like water finding its level — into the veins, into ten thousand shrines, into every banked hearth from the vale to the Glass Sea. No sun rises. Everything, everywhere, warms. <i>(+100 Hearthflame: the family shrine drinks first.)</i>', 'l-story');
        } else if (choice === 'usurp') {
          S.chr.deeds.push('Raised the Black Sunrise');
          log('<b>You take it.</b> The tenth note does not rise; it seizes. A sun stands up over the eastern rim already owned, black-gold and punctual, and the whole world checks its ledgers with a shiver it cannot name.', 'l-story');
        }
      }
      touch(); resolve();
    }));
  });
}

// ---------------- alchemy ----------------
async function actAlchemy(def) {
  const known = RECIPES.filter((r) => derived().mind >= r.minMind);
  const m = openModal({ title: 'The Furnace', wide: true });
  m.body.innerHTML = `<div class="mg-hint">Choose a recipe. Herbs are consumed; your flame decides the yield.</div>
    <div class="recipe-list">${RECIPES.map((r) => {
      const it = ITEMS[r.id];
      const knownR = derived().mind >= r.minMind;
      const have = hasItems(r.needs);
      const needsTxt = Object.entries(r.needs).map(([id, q]) => `${ITEMS[id].name}×${q} <i>(${S.chr.inventory[id] || 0})</i>`).join(', ');
      return `<button class="btn btn-recipe ${knownR && have ? '' : 'is-disabled'}" data-recipe="${r.id}" ${knownR && have ? '' : 'disabled'}>
        <b>${it.name}</b><small>${knownR ? needsTxt : `requires Mind ${r.minMind}`}</small><small class="dim">${it.desc}</small></button>`;
    }).join('')}</div>`;
  m.body.querySelectorAll('[data-recipe]').forEach((b) => b.addEventListener('click', async () => {
    const r = RECIPES.find((x) => x.id === b.dataset.recipe);
    m.close();
    if (!spendAP(def.ap)) return;
    for (const [id, q] of Object.entries(r.needs)) addItem(id, -q);
    const mm = openModal({ title: `Refining: ${ITEMS[r.id].name}`, locked: true });
    const res = await pillfire({ host: mm.body, diff: r.diff });
    mm.close();
    let score = res.score + (S.chr.lineage === 'tinder' ? 0.08 : 0) + (S.chr.dao === 'furnace' ? 0.08 : 0);
    if (score < 0.35) {
      if (chance(0.5)) log('The furnace coughs. You are now the owner of a small, expensive piece of slag.', 'l-bad');
      else { addItem(r.id, 1); log(`A <b>Cracked ${ITEMS[r.id].name}</b> rolls out — ugly, but it’ll work.`, 'l-gain'); }
    } else if (score < 0.6) { addItem(r.id, 1); log(`A <b>Standard ${ITEMS[r.id].name}</b>, honest work.`, 'l-gain'); }
    else if (score < 0.85) {
      addItem(r.id, 1);
      const bonus = chance(0.3);
      if (bonus) addItem(r.id, 1);
      log(`A <b>Refined ${ITEMS[r.id].name}</b>${bonus ? ' — and the dregs yield a second!' : ''}. Purity ${Math.round(score * 100)}%.`, 'l-gain');
    } else { addItem(r.id, 2); awardInsight('in_immaculate'); log(`<b>Immaculate!</b> The furnace sings and yields <b>two ${ITEMS[r.id].name}s</b>. Purity ${Math.round(score * 100)}%.`, 'l-gain'); }
    touch();
  }));
}

// ---------------- market / sect ----------------
export function marketPrice(id, selling = false) {
  const it = ITEMS[id];
  const cat = S.world.prices[it.kind] !== undefined ? it.kind : 'herb';
  let p = it.price * (S.world.prices[cat] || 1);
  if (selling) return Math.max(1, Math.round(p * 0.5));
  p *= derived().priceMult;
  if (it.slot === 'weapon' && (S.chr.rel.old_bo || 0) >= 6 && S.world.region === 'ashfen') p *= 0.8;
  if ((S.meta.family || []).some((k) => k.vocation === 'merchant')) p *= 0.92;
  if (it.slot === 'weapon' && S.world.region === 'ashfen' && (S.meta.family || []).some((k) => k.vocation === 'smith')) p *= 0.8;
  return Math.max(1, Math.round(p));
}
export function buyItem(id) {
  const price = marketPrice(id);
  if (S.chr.stones < price) { toast('Not enough spirit stones.'); return; }
  S.chr.stones -= price;
  addItem(id, 1);
  sfxE('coin');
  if (S.meta.generation === 1 && !S.chr.flags.tut_market) { S.chr.flags.tut_market = true; S.chr.stones += 10; log('<i>Granny Ash, from across the square: "Haggle worse! It builds character!" She flips you a coin for trying. (+10 stones)</i>', 'l-dim'); }
  log(`Bought ${ITEMS[id].name} for ${price} stones.`, 'l-dim');
  touch();
}
export function sellItem(id) {
  if (!(S.chr.inventory[id] > 0)) return;
  const price = marketPrice(id, true);
  addItem(id, -1);
  S.chr.stones += price;
  log(`Sold ${ITEMS[id].name} for ${price} stones.`, 'l-dim');
  touch();
}

export async function joinSectTrial() {
  const ok = await confirmModal('The Ten Thousand Steps',
    'At the gate, an elder gestures at another aspirant: "Two of you. One robe. The Order values many things, but it counts in victories." Fight for your place?');
  if (!ok) return;
  const res = await startCombat('sect_aspirant');
  if (res === 'win') {
    S.chr.sect.joined = true;
    S.chr.sect.contrib += 25; S.chr.sect.totalContrib += 25;
    S.chr.deeds.push('Joined the Order of the Kindled Path');
    log('<b>You are given the ash-gray robe of an Outer Disciple.</b> It fits like a future.', 'l-story');
  } else if (res === 'lose') {
    S.chr.hp = Math.max(1, Math.round(derived().hpMax * 0.2));
    log('The other aspirant takes the robe. The elder shrugs: "Mountains stay put. Come back stronger."', 'l-bad');
  }
  touch();
}

export function takeMission(id) {
  const t = MISSIONS.find((x) => x.id === id);
  if (t.kind === 'donate') {
    if (S.chr.stones < -t.stones) { toast('Not enough stones to tithe.'); return; }
    S.chr.stones += t.stones;
    S.chr.sect.contrib += t.contrib; S.chr.sect.totalContrib += t.contrib;
    log(`You tithe ${-t.stones} stones. <b>+${t.contrib} contribution.</b>`, 'l-gain');
  } else {
    S.chr.sect.mission = { ...t, done: false };
    log(`Mission taken: <b>${t.name}</b>.`, 'l-dim');
  }
  touch();
}
export function turnInMission() {
  const mis = S.chr.sect.mission;
  if (!mis) return;
  if (mis.kind === 'gather') {
    if (!hasItems({ [mis.item]: mis.qty })) { toast(`You still need ${mis.qty}× ${ITEMS[mis.item].name}.`); return; }
    addItem(mis.item, -mis.qty);
  } else if (mis.kind === 'hunt' && !mis.done) { toast('The quarry still breathes. Hunt it in its home region.'); return; }
  const lotusBonus = (S.chr.rel.sister_lotus || 0) >= 6 ? Math.round(mis.contrib * 0.2) : 0;
  S.chr.sect.contrib += mis.contrib + lotusBonus; S.chr.sect.totalContrib += mis.contrib + lotusBonus;
  S.chr.stones += mis.stones;
  S.chr.sect.mission = null;
  log(`Mission complete. <b>+${mis.contrib + lotusBonus} contribution, +${mis.stones} stones.</b>${lotusBonus ? ' <i>(Sister Iron Lotus signs the chit generously — kinship has rates.)</i>' : ''}`, 'l-gain');
  touch();
}
export function learnFromLibrary(tech, cost) {
  if (S.chr.sect.contrib < cost) { toast('Not enough contribution.'); return; }
  S.chr.sect.contrib -= cost;
  S.chr.techs.push(tech);
  if (S.chr.deck.length < 4) S.chr.deck.push(tech);
  log(`<b>Technique learned: ${TECHNIQUES[tech].name}.</b> The librarian stamps your palm with cinnabar.`, 'l-gain');
  touch();
}
export function buyFromSectStore(item, cost) {
  if (S.chr.sect.contrib < cost) { toast('Not enough contribution.'); return; }
  S.chr.sect.contrib -= cost;
  addItem(item, 1);
  log(`Exchanged ${cost} contribution for ${ITEMS[item].name}.`, 'l-dim');
  touch();
}

// ---------------- inventory ----------------
export function useInventoryItem(id) {
  const it = ITEMS[id];
  if (!it || !(S.chr.inventory[id] > 0)) return;
  if (it.kind === 'manual') {
    if (S.chr.techs.includes(it.tech)) { toast('You already know this technique.'); return; }
    const req = TECHNIQUES[it.tech].req;
    if (req?.realm && S.chr.realm < req.realm) { toast(`Requires the ${REALMS[req.realm].name} realm.`); return; }
    addItem(id, -1);
    S.chr.techs.push(it.tech);
    if (S.chr.deck.length < 4) S.chr.deck.push(it.tech);
    log(`<b>Technique learned: ${TECHNIQUES[it.tech].name}.</b>`, 'l-gain');
  } else if (it.kind === 'pill') {
    const u = it.use;
    const pm = derived().pillMult;
    addItem(id, -1);
    if (u.hpPct) {
      healHp(u.hpPct * pm);
      log(`The ${it.name} cools through you. Health restored.${pm > 1 ? ' <i>(the Furnace Dao wastes nothing)</i>' : ''}`, 'l-gain');
      if (S.chr.wounds.length) treatWound(S.chr.wounds[0]);
    }
    if (u.qiPct) { healQi(u.qiPct * pm); log(`The ${it.name} blooms into Emberlight.`, 'l-gain'); }
    if (u.stat) { S.chr.stats[u.stat] += 1; log(`<b>+1 ${u.stat[0].toUpperCase() + u.stat.slice(1)}.</b> The ${it.name} does its permanent work.`, 'l-gain'); }
    if (u.cult) { S.chr.cult += Math.round(u.cult * pm); log(`Crude but effective: <b>+${Math.round(u.cult * pm)} cultivation.</b>`, 'l-gain'); }
    if (u.grace) { S.chr.grace += u.grace; if (u.slow) S.chr.flags.slow_trib = true; log(`Your skin takes on a slate sheen. The next tribulation will find you harder to hurt. <i>(+${u.grace} bolt endured)</i>`, 'l-gain'); }
  } else if (it.kind === 'artifact') {
    const slot = it.slot;
    const old = S.chr.equip[slot];
    S.chr.equip[slot] = id;
    addItem(id, -1);
    if (old) addItem(old, 1);
    log(`Equipped <b>${it.name}</b>.`, 'l-dim');
  } else { toast(it.desc); return; }
  touch();
}
export function unequip(slot) {
  const id = S.chr.equip[slot];
  if (!id) return;
  S.chr.equip[slot] = null;
  addItem(id, 1);
  touch();
}
export function toggleDeck(tech) {
  const i = S.chr.deck.indexOf(tech);
  if (i >= 0) S.chr.deck.splice(i, 1);
  else {
    if (S.chr.deck.length >= 4) { toast('Your combat deck holds four techniques. Remove one first.'); return; }
    if (TECHNIQUES[tech].fx?.passiveMeditate) { toast('That scripture works on its own; it needs no deck slot.'); return; }
    S.chr.deck.push(tech);
  }
  touch();
}

// ---------------- advancement ----------------
export function stageUp() {
  if (atPeak()) { toast('You are at the Peak — only a tribulation can carry you further.'); return; }
  const cost = stageCost();
  if (S.chr.cult < cost) { toast('Your dantian is not yet full.'); return; }
  S.chr.cult -= cost;
  S.chr.stage += 1;
  S.chr.stats.spirit += 1;
  if (S.chr.stage % 2 === 0) S.chr.stats.body += 1;
  const d = derived();
  S.chr.hp = d.hpMax; S.chr.qi = d.qiMax;
  log(`<b>${realmLabel(S.chr)}.</b> The flame behind your ribs burns a shade brighter. (+1 Spirit${S.chr.stage % 2 === 0 ? ', +1 Body' : ''})`, 'l-gain');
  touch();
}

export async function attemptBreakthrough() {
  if (!atPeak()) { toast('Reach the Peak of your realm before challenging the heavens.'); return; }
  if (S.chr.realm >= 5) { toast('Beyond Sunforging lies only the Tenth Dawn — seek the Ninth Crater’s heart.'); return; }
  const cost = stageCost();
  if (S.chr.cult < cost) { toast('Gather more cultivation before challenging the heavens.'); return; }
  const next = REALMS[S.chr.realm + 1];
  const risky = S.chr.realm >= 3;
  const karmaGrace = S.chr.karma >= 10 ? 1 : 0;
  const flaw = S.chr.flaw ? FLAWS[S.chr.flaw].fx : {};
  const ok = await confirmModal(`Breakthrough: ${next.name}`,
    `The heavens tax every ascent. Endure the Ashfall and rise to <b>${next.name}</b> (lifespan ${next.lifespan} years).${S.chr.grace ? ` Pill wards will let you endure ${S.chr.grace} extra bolt(s).` : ''}${karmaGrace ? ' <b>The Wheel is serene around you — the heavens will hesitate once on your behalf.</b>' : ''}${flaw.extraWaves ? ' <b>You are Heaven-Marked: the sky will spend extra bolts on you.</b>' : ''}${risky ? ' <b>At this height, a failed tribulation can kill.</b>' : ' Failure costs cultivation and leaves wounds.'} Begin?`);
  if (!ok) return;
  S.chr.cult -= cost;
  const m = openModal({ title: `Tribulation: ${next.name}`, locked: true });
  const res = await ashfall({
    host: m.body, realm: S.chr.realm + 1, grace: S.chr.grace + karmaGrace + derived().insightGrace,
    slow: !!S.chr.flags.slow_trib, charcoal: S.chr.lineage === 'charcoal',
    extraWaves: flaw.extraWaves || 0,
  });
  m.close();
  S.chr.grace = 0; S.chr.flags.slow_trib = false;
  if (res.survived) {
    S.chr.realm += 1; S.chr.stage = 0; S.chr.cult = 0;
    const d = derived();
    S.chr.hp = d.hpMax; S.chr.qi = d.qiMax;
    S.chr.deeds.push(`Rose to ${REALMS[S.chr.realm].name}`);
    sfxE('gong');
    log(`<b>The last bolt falls, and you are still standing. ${REALMS[S.chr.realm].name}.</b> ${REALMS[S.chr.realm].desc}${res.auto ? ' <i>(endured in stillness)</i>' : ''}`, 'l-story');
    awardInsight('in_sky_survived');
    if (S.chr.realm === 1 && !S.chr.aspect) await awakening();
    if (S.chr.realm === 2 && !S.chr.dao) await daoChoice();
  } else {
    if (risky && chance(0.25)) {
      log('The heavens press their advantage past the point of lesson, into verdict.', 'l-bad');
      await die('a failed tribulation');
      touch();
      return;
    }
    const d = derived();
    S.chr.hp = Math.max(1, Math.round(d.hpMax * 0.25));
    S.chr.cult = Math.round(cost * 0.5);
    S.chr.injured = 1;
    log(`<b>The tribulation throws you back.</b> ${res.taken} bolts found flesh. Half your gathered cultivation scatters; your meridians will ache for a season.`, 'l-bad');
  }
  touch();
}

// ---------------- the Dao: what you cultivate toward ----------------
export async function daoChoice() {
  return new Promise((resolve) => {
    const m = openModal({ title: 'The Fork in the Fire', locked: true, wide: true });
    m.body.innerHTML = `<p class="event-text">Kindling: a true flame lives behind your sternum now, and a flame must burn <i>toward</i> something.
      The old texts call it choosing a Dao — the shape your whole cultivation grows into. It is chosen once, and it is chosen forever.</p>
      <div class="event-choices">${Object.entries(DAOS).map(([id, d]) => `
        <button class="btn btn-choice" data-dao="${id}"><b><span class="gold">${d.glyph}</span> ${d.name}</b><small>${d.desc}</small></button>`).join('')}</div>`;
    m.body.querySelectorAll('[data-dao]').forEach((b) => b.addEventListener('click', () => {
      const id = b.dataset.dao;
      S.chr.dao = id;
      const t = DAOS[id].tech;
      if (t && !S.chr.techs.includes(t)) {
        S.chr.techs.push(t);
        if (S.chr.deck.length < 4) S.chr.deck.push(t);
      }
      S.chr.deeds.push(`Chose ${DAOS[id].name}`);
      sfxE('gong');
      log(`<b>${DAOS[id].glyph} You set your feet on ${DAOS[id].name}.</b> ${DAOS[id].desc}`, 'l-story');
      m.close(); touch(); resolve();
    }));
  });
}

// ---------------- awakening: Aspect and Flaw ----------------
async function awakening() {
  const pool = Object.keys(ASPECTS).sort(() => Math.random() - 0.5).slice(0, 2);
  const flawId = pick(Object.keys(FLAWS));
  return new Promise((resolve) => {
    const m = openModal({ title: 'The Awakening', locked: true, wide: true });
    m.body.innerHTML = `<p class="event-text">The first true breath of Emberlight goes through you like a census-taker, opening every door.
      Something old in your blood stands up and introduces itself. <b>An Aspect wakes — but the Wheel balances every ledger, and a Flaw wakes with it.</b></p>
      <h4>Your blood offers two gifts. Take one.</h4>
      <div class="event-choices">${pool.map((id) => `<button class="btn btn-choice" data-asp="${id}"><b>${ASPECTS[id].name}</b><small>${ASPECTS[id].desc}</small></button>`).join('')}</div>`;
    m.body.querySelectorAll('[data-asp]').forEach((b) => b.addEventListener('click', () => {
      S.chr.aspect = b.dataset.asp;
      S.chr.flaw = flawId;
      log(`<b>Aspect awakened: ${ASPECTS[S.chr.aspect].name}.</b> ${ASPECTS[S.chr.aspect].desc}`, 'l-story');
      log(`<b>…and the Wheel’s price: ${FLAWS[flawId].name}.</b> ${FLAWS[flawId].desc}`, 'l-bad');
      S.chr.deeds.push(`Awakened the ${ASPECTS[S.chr.aspect].name} aspect`);
      m.close(); touch(); resolve();
    }));
  });
}

// ---------------- seasons & mortality ----------------
export async function endSeason() {
  const flaw = S.chr.flaw ? FLAWS[S.chr.flaw].fx : {};
  const wxOld = weatherNow();

  // --- vows tick ---
  const fasting = S.chr.vows.some((v) => v.id === 'fast');
  for (const v of [...S.chr.vows]) {
    v.seasonsLeft -= 1;
    const def = VOWS[v.id];
    if (def.hpCost) S.chr.hp = Math.max(1, S.chr.hp - Math.round(derived().hpMax * def.hpCost));
    if (v.seasonsLeft <= 0) {
      S.chr.vows = S.chr.vows.filter((x) => x !== v);
      const r = def.reward;
      if (r.cult) S.chr.cult += r.cult;
      if (r.karma) addKarma(r.karma);
      if (r.stat) S.chr.stats[r.stat] += 1;
      log(`<b>Vow kept: ${def.name}.</b> ${r.log}`, 'l-story');
      S.chr.deeds.push(`Kept the ${def.name}`);
      awardInsight('in_vow_kept');
    }
  }

  // --- the table: provisions, hunger, debts ---
  if (!fasting) {
    let need = flaw.doubleFood ? 2 : 1;
    if (wxOld?.fx.hungerUp) need += 1;
    const have = S.chr.inventory.provisions || 0;
    const eaten = Math.min(have, need);
    if (eaten > 0) addItem('provisions', -eaten);
    if (eaten < need) {
      S.chr.hungry = true;
      S.chr.hp = Math.max(1, S.chr.hp - Math.round(derived().hpMax * 0.15));
      log(`<b>The larder runs empty${flaw.doubleFood ? ' — and your hollow appetite eats first' : ''}.</b> You boil bark, tighten your belt, and think about food during meditation, which defeats the purpose. <i>(Hungry: cultivation suffers until you eat)</i>`, 'l-bad');
    } else {
      if (S.chr.hungry) log('<i>Fed again, properly. Your focus stops wandering to soup.</i>', 'l-dim');
      S.chr.hungry = false;
      if (need > 1) log(`<i>${need} provisions eaten this season${wxOld?.fx.hungerUp ? ' — the iron cold is a second mouth' : ''}.</i>`, 'l-dim');
    }
  } else {
    log('<i>The Fast of Embers: your body eats its own warmth and asks the flame to make up the difference.</i>', 'l-dim');
  }
  if (flaw.tax) {
    S.chr.stones = Math.max(0, S.chr.stones - flaw.tax);
    log(`<i>The Bleeding Ledger: ${flaw.tax} stones gone from your purse by morning. The receipt, as always, is written in a dead language.</i>`, 'l-dim');
  }

  // --- turn the season ---
  S.world.season += 1;
  S.world.medCount = 0;
  S.chr.flags.granny_heal = false;
  let newYear = false;
  if (S.world.season > 3) {
    S.world.season = 0;
    S.world.year += 1;
    S.chr.age += 1;
    newYear = true;
  }
  S.world.ap = AP_PER_SEASON - (S.chr.injured > 0 ? 1 : 0);
  if (S.chr.injured > 0) { S.chr.injured -= 1; log('<i>Your bruises slow the season’s work. (−1 action)</i>', 'l-dim'); }
  if (S.world.season === 0) S.chr.flags.lamplit = false;
  healQi(0.25);
  if (S.chr.sect.joined) {
    const rk = SECT_RANKS[sectRank(S.chr)];
    S.chr.stones += rk.stipend;
    log(`<i>Sect stipend: +${rk.stipend} stones (${rk.name}).</i>`, 'l-dim');
  }

  // --- the household provides ---
  const fam = S.meta.family || [];
  for (const kin of fam) {
    if (kin.vocation === 'herbalist') {
      const h = pick(['emberdew_moss', 'graypine_sap', 'duskpetal']);
      addItem(h, 1);
      log(`<i>${esc(kin.name)} leaves ${ITEMS[h].name} on your sill, labeled in a hand that brooks no argument.</i>`, 'l-dim');
    }
    if (kin.vocation === 'hunter' && S.world.season % 2 === 0) {
      addItem('provisions', 1);
      log(`<i>${esc(kin.name)} comes back smelling of pine and blood; the larder grows. (+1 Provisions)</i>`, 'l-dim');
    }
    if (kin.vocation === 'disciple' && S.chr.sect.joined) {
      S.chr.sect.contrib += 8; S.chr.sect.totalContrib += 8;
      log(`<i>A letter from ${esc(kin.name)} on the mountain vouches for your family. (+8 contribution)</i>`, 'l-dim');
    }
    if (kin.vocation === 'keeper' && newYear) {
      addKarma(1);
      log(`<i>${esc(kin.name)} sweeps the shrine at dusk, all year; the ash never settles on your name.</i>`, 'l-dim');
    }
  }

  // --- the estate provides ---
  if ((S.meta.estate?.garden || 0) > 0) {
    const herb = pick(['emberdew_moss', 'graypine_sap', 'duskpetal', 'marrowroot'].slice(0, 1 + S.meta.estate.garden));
    addItem(herb, 1);
    log(`<i>The family garden yields: ${ITEMS[herb].name}. The rows your line planted keep planting back.</i>`, 'l-dim');
  }

  // --- aspect upkeep ---
  if (S.chr.aspect === 'ash_blooded' && S.chr.wounds.length) {
    const wid = S.chr.wounds[0];
    treatWound(wid);
    log('<i>Ash-Blooded: the wound closes on its own, gray and warm, like banked coals settling.</i>', 'l-dim');
  }

  // --- mortality ---
  const d = derived();
  const left = d.lifespan - S.chr.age;
  if (left <= 5 && left > 0 && S.world.season === 0) log(`<b>Your hands have started to look like your ${S.meta.generation > 1 ? 'ancestors’' : 'parents’'}.</b> Perhaps ${left} year${left === 1 ? '' : 's'} remain. A breakthrough would buy decades; the Hearth Shrine would secure the line.`, 'l-bad');
  if (left <= 0) {
    log('<b>One evening, the flame behind your ribs banks itself low, and you understand.</b> You set your affairs in order and sit down by the hearth a final time.', 'l-story');
    await die('old age');
    return;
  }

  // --- new season arrives ---
  log(`— ${SEASONS[S.world.season]}, Year ${S.world.year}. ${pick(region().flavor)}`, 'l-season');
  const wx = rollWeather();
  if (wx.fx.woundAche && S.chr.wounds.length) log('<i>The frost finds every old wound and rings each one like a bell.</i>', 'l-dim');
  if (wx.fx.omen) log(`<i>The crows watch you specifically, you feel. The Wheel’s hum is <b>${karmaWord()}</b>.</i>`, 'l-dim');

  // --- world tick: rivals, prices, news ---
  if (newYear) advanceRivals();
  driftPrices();
  if (chance(0.4)) pushNews(pick(chance(0.5) ? NEWS_TEMPLATES.sect : NEWS_TEMPLATES.world));

  // --- festival of lamps (winter) ---
  if (S.world.season === 3) await festivalOfLamps();

  // --- dreams come at season's turning ---
  if (chance(0.2)) await dreamTrial();

  touch();
}

function advanceRivals() {
  for (const rv of S.world.rivals) {
    const def = RIVALS.find((r) => r.id === rv.id);
    if (!def || rv.dead || rv.realm >= 5) continue;
    rv.cult += Math.round(def.talent * ri(22, 42));
    const need = 100 + rv.realm * 130;
    if (rv.cult >= need) {
      rv.cult = 0;
      // at height, the sky collects its tax from them too
      if (rv.realm >= 3 && chance(0.12)) {
        rv.dead = true;
        pushNews(`Black banners in the teahouses: ${def.name}, ${def.epithet}, fell to the ${REALMS[rv.realm + 1].name} tribulation. The Roll of Names grows one line shorter, and everyone on it grows one degree quieter.`);
        log(`<i>Word comes with the year's first caravan: ${def.name} is dead — the sky refused them. You knew that name. Everyone did.</i>`, 'l-bad');
        continue;
      }
      rv.realm += 1;
      pushNews(NEWS_TEMPLATES.rival_up(def, REALMS[rv.realm].name));
      log(`<i>${NEWS_TEMPLATES.rival_up(def, REALMS[rv.realm].name)}</i>`, 'l-dim');
    }
  }
}

// A rival, rendered in flesh for the arena floor or a dark road.
export function rivalAsEnemy(rv) {
  const def = RIVALS.find((r) => r.id === rv.id);
  const els = { yan_shuo: 'glass', bo_yun: 'charcoal', brother_cinder: 'cinder', widow_ash: 'smoke' };
  return {
    name: `${def.name}, ${def.epithet}`, tier: Math.min(5, rv.realm + 1), el: els[rv.id] || 'cinder',
    hp: 60 + rv.realm * 95, atk: 8 + rv.realm * 8, stones: [0, 0], rival: true,
    desc: 'A name from the Roll, standing in front of you with everything the teahouses said and a few things they missed.',
    loot: {},
    intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 1.9 }, { t: 'guard', w: 1 }, { t: 'dodgeup', w: 1 } ],
  };
}
export function livingRivalNear(range = 1) {
  return S.world.rivals.find((rv) => !rv.dead && Math.abs(rv.realm - S.chr.realm) <= range);
}

function driftPrices() {
  const wx = weatherNow();
  for (const cat of Object.keys(S.world.prices)) {
    let f = 0.94 + Math.random() * 0.14;
    if (wx?.fx.priceUp === cat) f += 0.12;
    if (wx?.fx.priceDown === cat) f -= 0.12;
    const before = S.world.prices[cat];
    S.world.prices[cat] = Math.min(1.4, Math.max(0.7, before * f));
    const moved = S.world.prices[cat] - before;
    if (moved > 0.08) pushNews(NEWS_TEMPLATES.price_up(cat));
    if (moved < -0.08) pushNews(NEWS_TEMPLATES.price_down(cat));
  }
}

async function festivalOfLamps() {
  return new Promise((resolve) => {
    const m = openModal({ title: 'The Night of Nine Lamps', locked: true });
    m.body.innerHTML = `<p class="event-text">Midwinter. Across the vale, every household sets out nine lamps — one for each fallen sun — and one small unnumbered flame for their own dead. The dark between houses fills with walking lights.
      ${S.meta.ancestors.length ? `Your line keeps ${S.meta.ancestors.length} name${S.meta.ancestors.length === 1 ? '' : 's'} worth lighting.` : 'Your line’s lamps are still few, and all still ahead of you.'}</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-light><b>Light the lamps and keep the vigil</b> <small>5 stones of oil — the ancestors lean close</small></button>
        <button class="btn btn-choice btn-ghost" data-skip>Let the night pass unlit</button>
      </div>`;
    m.body.querySelector('[data-light]').addEventListener('click', () => {
      S.chr.stones = Math.max(0, S.chr.stones - 5);
      S.meta.hearthflame += 1;
      addKarma(1);
      S.chr.flags.lamplit = true;
      log('<b>Nine lamps, and one.</b> You sit the vigil out. Around midnight the flames all lean the same direction, toward no wind you can feel. <i>(+1 Hearthflame; the Wheel notes the courtesy; your breath comes easier all winter)</i>', 'l-story');
      m.close(); resolve();
    });
    m.body.querySelector('[data-skip]').addEventListener('click', () => {
      log('The vale glitters with other people’s lamps. Your windows stay dark. It is only one night, you tell yourself, several times.', 'l-dim');
      m.close(); resolve();
    });
  });
}

// ---------------- Hearthline: death & succession ----------------
export function legacyEarned() {
  const c = S.chr;
  const realmHf = [0, 10, 30, 60, 110, 180, 320][c.realm];
  let hf = realmHf + c.stage * 5 + c.deeds.length * 8;
  if (c.dao === 'hearth') hf = Math.round(hf * 1.5);
  hf += (S.meta.estate?.shrine || 0) * 4;
  if ((S.meta.family || []).some((k) => k.vocation === 'keeper')) hf += 4;
  return hf;
}

export async function die(cause) {
  const earn = legacyEarned();
  S.meta.hearthflame += earn;
  S.meta.ancestors.push({
    name: S.chr.name, lineage: S.chr.lineage, realm: realmLabel(S.chr), age: S.chr.age,
    cause, deeds: [...S.chr.deeds], sect: S.chr.sect.joined, gen: S.meta.generation,
    karma: S.chr.karma,
  });
  // what the fire keeps: deeds echo down the generations
  for (const k of ['saved_village', 'naga_friend', 'rival_settled', 'ghost_letter_done', 'abbot_slain', 'order_debt', 'family_physician']) {
    if (S.chr.flags[k]) S.meta.legacyFlags[k] = true;
  }
  if (cause === 'slain' || cause === 'a failed tribulation' || cause === 'the final tribulation') S.meta.legacyFlags.died_fighting = true;
  S.pendingSuccession = { cause, earn };
  persist(true);
  document.dispatchEvent(new CustomEvent('emberline:succession'));
}

export async function passTorch() {
  const ok = await confirmModal('Pass the Torch',
    'Step back from the path, raise your heir by the family hearth, and let the Hearthline continue. Your realm, deeds, and estate become their inheritance. Do it?');
  if (!ok) return;
  log('<b>You bank your flame and turn to the cradle.</b> There are many ways to tend a fire. This is one.', 'l-story');
  await die('passed the torch');
}

export function estateForKit() {
  const inv = S.chr.inventory;
  const withEquipped = { ...inv };
  for (const slot of ['weapon', 'charm']) {
    const id = S.chr.equip[slot];
    if (id) withEquipped[id] = (withEquipped[id] || 0) + 1;
  }
  const of = (kind) => Object.entries(withEquipped).filter(([id]) => ITEMS[id]?.kind === kind);
  return {
    artifacts: of('artifact'), manuals: of('manual'),
    pills: of('pill'), stones: Math.floor(S.chr.stones * 0.3),
  };
}

export const ESTATE_TRACKS = {
  shrine: { name: 'The Family Shrine', desc: 'Gilded cradle, deeper ash-bed. Each tier: +4 Hearthflame at every succession.' },
  garden: { name: 'The Herb Garden', desc: 'Rows your line plants and replants. Each tier: better herbs arrive each season, free.' },
  library: { name: 'The Hearth Library', desc: 'Copied scriptures, annotated by every generation. Each tier: heirs begin with +20 cultivation.' },
};
export function buyEstate(track) {
  const tier = S.meta.estate[track] || 0;
  if (tier >= 3) { toast('The estate can hold no more of that. Some limits are architectural.'); return; }
  const cost = 150 * (tier + 1);
  if (S.chr.stones < cost) { toast(`The masons want ${cost} stones. Masons are like that.`); return; }
  S.chr.stones -= cost;
  S.meta.estate[track] = tier + 1;
  sfxE('coin');
  log(`<b>The estate grows: ${ESTATE_TRACKS[track].name}, tier ${tier + 1}.</b> Stone outlives flesh; this will serve every life the line has left.`, 'l-gain');
  touch();
}

export function buyPerk(id) {
  const p = PERKS.find((x) => x.id === id);
  const lvl = perkLevel(S.meta, id);
  if (lvl >= p.tiers.length) return;
  const cost = p.tiers[lvl].cost;
  if (S.meta.hearthflame < cost) { toast('Not enough Hearthflame.'); return; }
  S.meta.hearthflame -= cost;
  S.meta.perks[id] = lvl + 1;
  log(`<b>Bloodline deepened: ${p.name} ${lvl + 1 > 1 ? 'tier ' + (lvl + 1) : ''}.</b>`, 'l-gain');
  touch();
}

export function completeSuccession({ name, lineage, kit }) {
  const gen = S.meta.generation + 1;
  S.meta.generation = gen;
  const chr = createCharacter({ name, lineage, origin: 'hearthborn', meta: S.meta, kit });
  const a = S.meta.ancestors[S.meta.ancestors.length - 1];
  chr.karma = Math.round((a?.karma || 0) / 2);
  if (chr.karma >= 10) { chr.stats.fate += 1; }
  else if (chr.karma <= -10) { chr.stats.fate = Math.max(1, chr.stats.fate - 1); }
  // the cradle-side hours, kept
  const hb = S.meta.heirBonus || {};
  for (const k of ['body', 'mind', 'spirit', 'fate']) chr.stats[k] += hb[k] || 0;
  let inherit = (hb.insights || 0) + (S.chr?.dao === 'hearth' ? 1 : 0);
  const pool = (S.chr?.insights || []).slice();
  while (inherit-- > 0 && pool.length) chr.insights.push(pool.splice(Math.floor(Math.random() * pool.length), 1)[0]);
  chr.cult += (S.meta.estate?.library || 0) * 20;
  if (S.chr?.flags?.kept_flame) chr.flags.kept_flame = true;
  chr.forgeTier = S.chr?.forgeTier || 0;
  S.meta.heirBonus = { body: 0, mind: 0, spirit: 0, fate: 0, insights: 0 };
  S.chr = chr;
  S.world.year += 10;
  S.world.season = 0;
  S.world.ap = AP_PER_SEASON;
  S.world.region = 'ashfen';
  S.pendingSuccession = null;
  S.ended = false;
  const d = derived();
  chr.hp = d.hpMax; chr.qi = d.qiMax;
  log(`<b>Generation ${gen}.</b> Ten years pass. ${esc(name)} of the Hearthline comes of age beneath the family flame, inheriting what the ancestors kept and what they dared. The world remembers your name — prove it should.`, 'l-story');
  awardInsight('in_grief');
  if (chr.karma >= 10) log('<i>The Wheel remembers the line’s merit: this child was born under kind stars. (+1 Fate, karmic echo inherited)</i>', 'l-dim');
  else if (chr.karma <= -10) log('<i>The Wheel remembers the line’s debts: this child was born in a hard year, under circling crows. (−1 Fate, karmic echo inherited)</i>', 'l-dim');
  else if (chr.karma) log('<i>A faint karmic echo follows the bloodline, as the Wheel turns the account forward.</i>', 'l-dim');
  touch();
}

export function questStates() {
  return QUESTS.map((q) => ({ ...q, isDone: q.done(S) }));
}

// ---------------- travel & the wide world ----------------
export const domainOf = (r) => DOMAINS.find((d) => d.id === (r.domain || 'vale'));

export async function travelTo(id) {
  const r = REGIONS.find((x) => x.id === id);
  const from = region();
  const dom = domainOf(r);
  if (S.chr.realm < dom.minRealm) { toast(`${dom.name} lies beyond you — the roads there would eat ${REALMS[S.chr.realm].title} alive. Reach ${REALMS[dom.minRealm].name} first.`); return; }
  if (S.chr.realm < r.minRealm) { toast(`${r.name} would eat ${REALMS[S.chr.realm].title} alive. Reach ${REALMS[r.minRealm].name} first.`); return; }
  if (S.world.region === id) return;
  if (from.domain !== r.domain) {
    if (S.world.ap < 1) { toast('Crossing between domains eats the better part of a season. No time left — end the season first.'); return; }
    S.world.ap -= 1;
    log(`<b>You take the long roads out of ${domainOf(from).name}</b> — passes, ferries, way-inns, and the particular loneliness of maps. <i>(journey: −1 action)</i>`, 'l-story');
    awardInsight('in_far_road');
    const grudged = S.world.rivals.find((rv) => rv.grudge && !rv.dead);
    if (grudged && chance(0.25)) {
      const def = RIVALS.find((x) => x.id === grudged.id);
      log(`<b>The road narrows, and ${def.name} is standing in it.</b> "You cost me something," they say. "Roads are where accounts get settled."`, 'l-bad');
      const res = await startCombat(rivalAsEnemy(grudged));
      if (res === 'win') {
        grudged.grudge = false;
        awardInsight('in_rivals_eyes');
        pushNews(`Road-gossip: ${def.name} sought a reckoning on the long roads and was sent home with a settled account and a new respect.`);
        log(`${def.name} picks themselves out of the road-dust and, remarkably, laughs. "Paid in full." The grudge is done; something cleaner takes its place.`, 'l-story');
      } else if (res === 'lose') { grudged.grudge = false; await handleDefeat(false, null); }
    } else if (chance(0.5)) await showEvent(pick(ROAD_EVENTS));
  }
  S.world.region = id;
  if (!S.world.visited.includes(id)) S.world.visited.push(id);
  log(`<b>${r.name}</b> — ${dom.name}. ${r.desc}`, 'l-story');
  touch();
}

// ---------------- the Grand Auction ----------------
function seasonSeed() { return S.world.year * 4 + S.world.season; }
function seededPick(seed, n, len) { return ((seed * 31 + n * 17) % len + len) % len; }

async function actAuction(def) {
  const key = `auction_${seasonSeed()}`;
  if (S.chr.flags[key]) { toast('The Grand Auction’s bell rings but once a season. Come back with the new moon.'); return; }
  if (!spendAP(def.ap)) return;
  S.chr.flags[key] = true;
  const seed = seasonSeed();
  const lots = [];
  for (let i = 0; lots.length < 3 && i < 12; i++) {
    const cand = AUCTION_POOL[seededPick(seed, i, AUCTION_POOL.length)];
    if (!lots.includes(cand)) lots.push(cand);
  }
  const feeFree = (S.chr.rel.nine_fingers || 0) >= 6;
  const m = openModal({ title: 'The Grand Auction of Riverport', wide: true, locked: true });
  sfxE('bell');
  const bidders = ['a veiled sword-bride', 'the Salt Consortium’s clerk', 'an old man with young hands', 'a sect steward with a war budget', 'somebody’s extremely calm butler'];
  const liveRival = livingRivalNear(2);
  const rivalDef = liveRival ? RIVALS.find((x) => x.id === liveRival.id) : null;
  let lotIdx = 0;

  const showLot = () => {
    if (lotIdx >= lots.length) {
      m.body.innerHTML = `<p class="event-text">The bell rings thrice. Porters collect the lots; the crowd disperses into gossip and regret, the auction’s true products.</p>
        <div class="event-choices"><button class="btn btn-choice" data-done>Step out into Riverport</button></div>`;
      m.body.querySelector('[data-done]').addEventListener('click', () => { m.close(); touch(); });
      return;
    }
    const lot = lots[lotIdx];
    const it = ITEMS[lot.item];
    const isRivalLot = rivalDef && lotIdx === 1;
    const rival = isRivalLot ? rivalDef.name : pick(bidders);
    let bid = Math.round(lot.base * (0.75 + ((seed + lotIdx * 7) % 10) / 20));
    let rivalCap = Math.round(lot.base * (1.15 + ((seed + lotIdx * 3) % 10) / 12));
    let yourBid = false;
    const render = (line) => {
      const next = Math.max(bid + Math.ceil(bid * 0.1), bid + 5);
      const afford = next + (feeFree ? 0 : Math.ceil(next * 0.05));
      m.body.innerHTML = `
        <p class="event-text"><b>Lot ${lotIdx + 1} of ${lots.length}: ${esc(it.name)}</b><br><small class="dim">${esc(it.desc)}</small></p>
        <p class="event-text">${line}<br>Current bid: <b class="gold">${fmtNum(bid)} ◈</b> ${yourBid ? '<b>(yours)</b>' : `(${rival})`} · your purse: ${fmtNum(S.chr.stones)} ◈${feeFree ? ' · <i>house fee waived</i>' : ' · house fee 5%'}</p>
        <div class="event-choices">
          <button class="btn btn-choice ${S.chr.stones >= afford ? '' : 'is-disabled'}" data-raise ${S.chr.stones >= afford ? '' : 'disabled'}><b>Raise to ${fmtNum(next)} ◈</b></button>
          <button class="btn btn-choice btn-ghost" data-pass>${yourBid ? 'Hold — let the hammer fall' : 'Let it go'}</button>
        </div>`;
      m.body.querySelector('[data-raise]')?.addEventListener('click', () => {
        bid = next; yourBid = true;
        if (bid < rivalCap && chance(0.65)) {
          bid = Math.min(rivalCap, bid + Math.ceil(bid * 0.08));
          yourBid = false;
          render(`${rival} lifts a paddle without looking up.`);
        } else {
          render(`${rival} folds their hands. The room looks at you.`);
        }
      });
      m.body.querySelector('[data-pass]').addEventListener('click', () => {
        if (yourBid) {
          const fee = feeFree ? 0 : Math.ceil(bid * 0.05);
          S.chr.stones -= bid + fee;
          addItem(lot.item, 1);
          sfxE('coin');
          awardInsight('in_hammer');
          if (isRivalLot) { liveRival.grudge = true; pushNews(`${rivalDef.name} was outbid at the Grand Auction — publicly, by name — and left before the hammer's echo died. The teahouses are insufferable about it.`); }
          log(`<b>Hammer falls: ${it.name} is yours</b> for ${fmtNum(bid)} stones${fee ? ` (+${fee} house fee)` : ''}. ${rival} exits with dignity${isRivalLot ? ', visibly rebudgeting a grudge' : ' and a grudge'}.`, 'l-gain');
        } else {
          log(`<i>Lot ${lotIdx + 1} (${it.name}) goes to ${rival}. The hammer’s echo sounds briefly like your name.</i>`, 'l-dim');
        }
        lotIdx += 1;
        showLot();
      });
    };
    render('The auctioneer’s gavel hovers. Nine-Fingers reads the room like weather.');
  };
  log('<b>The noon bell.</b> Riverport’s Grand Auction convenes under a roof of ships’ ribs and chandeliers of salvage glass.', 'l-story');
  showLot();
}
const fmtNum = (n) => Number(n).toLocaleString('en-US');

// ---------------- the Scarred Arena ----------------
async function actArena(def) {
  const key = `arena_y${S.world.year}`;
  if (S.chr.flags[key]) { toast('The tournament is done for the year. The horde spends the off-season inventing new respect to withhold.'); return; }
  const ok = await confirmModal('The Scarred Arena',
    'Three bouts, drawn lots, no substitutions: a Horde Champion, then an Asura Remnant off its leash, then — the horde’s idea of an honor — a captured Sunspawn. Losses end at the healers’ tent, not the grave. Victors are remembered by the Banner itself. Enter?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  S.chr.flags[key] = true;
  const arenaRival = livingRivalNear(1);
  const bracket = ['horde_champion', arenaRival ? rivalAsEnemy(arenaRival) : 'asura_remnant', 'sunspawn'];
  const healPct = (S.chr.rel.khans_shadow || 0) >= 6 ? 0.5 : 0.3;
  log(`<b>The Scarred Arena.</b> Ten thousand asura-blooded voices, and all of them can see you. The lots are drawn${arenaRival ? ` — and the second name on the board is ${RIVALS.find((x) => x.id === arenaRival.id).name}` : ''}.`, 'l-story');
  for (let i = 0; i < bracket.length; i++) {
    const res = await startCombat(bracket[i], { spar: false });
    if (res === 'win' && i === 1 && arenaRival) { arenaRival.grudge = true; awardInsight('in_rivals_eyes'); pushNews(`The Scarred Arena's second bout is already a teahouse ballad: two names from the Roll, one still standing at the end.`); }
    if (res !== 'win') {
      const d = derived();
      S.chr.hp = Math.max(S.chr.hp, Math.round(d.hpMax * 0.2));
      const consolation = 60 * (i);
      if (consolation) { S.chr.stones += consolation; }
      log(`Carried to the healers’ tent on a shield — the horde’s courtesy for bout ${i + 1}. ${consolation ? `Blood-pay: <b>+${consolation} stones.</b>` : 'No pay for a first-bout fall; the steppe is honest that way.'}`, 'l-bad');
      touch();
      return;
    }
    if (i < bracket.length - 1) {
      const d = derived();
      S.chr.hp = Math.min(d.hpMax, S.chr.hp + Math.round(d.hpMax * healPct));
      healQi(0.4);
      log(`<i>Between bouts: ${healPct > 0.3 ? 'the Banner’s own healers attend you — the Shadow’s word carries' : 'a horde cutman splashes you with something that burns twice'}. Bout ${i + 2} is called.</i>`, 'l-dim');
    }
  }
  S.chr.stones += 500;
  S.chr.cult += 60;
  awardInsight('in_arena_dust');
  S.chr.deeds.push('Won the Scarred Arena tournament');
  pushNews(`The teahouses have a new argument: an outlander took the Scarred Arena’s crown. The Banner did not object, which is the loudest thing a banner can do.`);
  if (!S.chr.flags.arena_standard) {
    S.chr.flags.arena_standard = true;
    addItem('khan_standard', 1);
    log('<b>Champion of the Scarred Arena.</b> +500 stones, +60 cultivation — and the Shadow drapes the Khan’s Lesser Standard over your shoulders. The silence from the Khan’s tent is, you are assured, thunderous approval.', 'l-story');
  } else {
    log('<b>Champion again.</b> +500 stones, +60 cultivation. The horde begins, grudgingly, to name children after you.', 'l-story');
  }
  touch();
}

// ---------------- domain bosses ----------------
async function actZigguratHeart(def) {
  if (S.chr.flags.frag_ember_guardian) { toast('The Ziggurat stands quiet. Its verse of the Hymn is already yours.'); return; }
  const ok = await confirmModal('Climb the Ziggurat',
    'The Ember Guardian circles the summit: armor of slag, heart of grief, and a verse of the Tenthfire Hymn held for eight hundred years. This fight is to the death — <b>defeat means your heir continues the Hearthline.</b> Climb?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  log('You climb steps sized for something with a longer stride than regret. At the summit, the air itself stands at attention.', 'l-story');
  const res = await startCombat('ember_guardian');
  if (res === 'win') {
    S.chr.flags.frag_ember_guardian = true;
    awardInsight('in_apex');
    log('<b>A Hymn Fragment rises from the Guardian’s cooling armor</b>, singing faintly — a verse that has waited centuries for a throat.', 'l-story');
  } else if (res === 'lose') await handleDefeat(true, 'ember_guardian');
  touch();
}

async function actTideCourt(def) {
  if (S.chr.flags.tide_king_slain) { toast('The great ribcage stands empty. The tide comes and goes on its own recognizance now.'); return; }
  const ok = await confirmModal('The Drowned Court',
    'The Tide-Hollowed King holds court in the largest ribcage on the coast, and his petitions are settled in blood. His harpoon and his killing stroke would pass to whoever unseats him. This fight is to the death — <b>defeat means your heir continues the Hearthline.</b> Petition?');
  if (!ok) return;
  if (!spendAP(def.ap)) return;
  log('You walk the rib-cathedral at low tide. The King rises from his throne of ballast stones, crowned in barnacle and patience.', 'l-story');
  const res = await startCombat('tide_king');
  if (res === 'win') {
    S.chr.flags.tide_king_slain = true;
    awardInsight('in_apex');
    S.chr.deeds.push('Unseated the Tide-Hollowed King');
    pushNews('Tide-law is rewritten on the Drowned Coast: the Hollowed King’s court stands empty, and the harbor-mothers light lamps for whoever managed it.');
    log('<b>The King comes apart into spent tide and old grief.</b> His court of wraiths bows — to you, or to the ending — and disperses like fog given permission.', 'l-story');
  } else if (res === 'lose') await handleDefeat(true, 'tide_king');
  touch();
}

// ---------------- shared confirm ----------------
export function confirmModal(title, html) {
  return new Promise((resolve) => {
    const m = openModal({ title, locked: true });
    m.body.innerHTML = `<p class="event-text">${html}</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-yes>Proceed</button>
        <button class="btn btn-ghost" data-no>Not yet</button>
      </div>`;
    m.body.querySelector('[data-yes]').addEventListener('click', () => { m.close(); resolve(true); });
    m.body.querySelector('[data-no]').addEventListener('click', () => { m.close(); resolve(false); });
  });
}

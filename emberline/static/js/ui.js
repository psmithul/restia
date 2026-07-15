// Render layer: screens, panels, side tabs, shops, shrine, succession.
import {
  REGIONS, REALMS, LINEAGES, ORIGINS, ITEMS, TECHNIQUES, PERKS, MISSIONS,
  SECT_RANKS, SECT_STORE, LIBRARY, ACTION_DEFS, STAGE_NAMES,
  ASPECTS, FLAWS, WOUNDS, VOWS, RIVALS, DOMAINS, INSIGHTS, DAOS, DISCOVERIES,
  VOCATIONS, DIFFICULTIES, NAME_POOL,
} from './data.js';
import { isMuted, setMuted } from './sound.js';
import {
  S, setState, derived, stageCost, atPeak, realmLabel, dateLabel, sectRank,
  newGame, listSaves, loadSave, deleteSave, persist, perkLevel,
} from './state.js';
import * as A from './actions.js';
import { esc, el, fmt, pick } from './util.js';
import { openModal } from './modal.js';

const $ = (sel) => document.querySelector(sel);
const show = (id) => {
  document.querySelectorAll('.screen').forEach((s) => s.classList.add('hidden'));
  $(id).classList.remove('hidden');
};

// ---------------- title ----------------
export async function showTitle() {
  show('#screen-title');
  spawnEmbers();
  const box = $('#title-saves');
  box.innerHTML = '<div class="dim">Reading the family registers…</div>';
  const saves = await listSaves();
  box.innerHTML = saves.length ? '<h3>Continue a Hearthline</h3>' : '';
  saves.forEach((s) => {
    const row = el(`<div class="save-row">
      <button class="save-load"><b>${esc(s.name)}</b><small>Gen ${s.generation} · ${esc(s.realm)} · Year ${s.year}</small></button>
      <button class="save-del btn-ghost" title="Let this line end" aria-label="delete save">✕</button>
    </div>`);
    row.querySelector('.save-load').addEventListener('click', async () => {
      const st = await loadSave(s.slot);
      if (!st) return toastMsg('That register has crumbled to ash.');
      setState(st);
      startGame();
    });
    row.querySelector('.save-del').addEventListener('click', async () => {
      if (!confirm(`Erase the Hearthline of ${s.name}? This cannot be undone.`)) return;
      await deleteSave(s.slot);
      showTitle();
    });
    box.appendChild(row);
  });
}

function spawnEmbers() {
  const host = $('.embers');
  if (host.childElementCount) return;
  for (let i = 0; i < 26; i++) {
    const e = document.createElement('i');
    e.style.left = `${Math.random() * 100}%`;
    e.style.animationDuration = `${6 + Math.random() * 10}s`;
    e.style.animationDelay = `${-Math.random() * 12}s`;
    e.style.opacity = 0.3 + Math.random() * 0.5;
    const s = 2 + Math.random() * 4;
    e.style.width = e.style.height = `${s}px`;
    host.appendChild(e);
  }
}

// ---------------- creation: fated, or architect ----------------
let createPick = { lineage: 'cinder', origin: 'orphan' };
let createMode = null; // null | 'fated' | 'architect'
let architect = null;

export function showCreate() {
  show('#screen-create');
  const card = $('.create-card');
  if (createMode === null) renderModeChoice(card);
  else if (createMode === 'fated') renderFated(card);
  else renderArchitect(card);
}

function renderModeChoice(card) {
  card.innerHTML = `
    <h2>The First of the Line</h2>
    <p class="dim" style="margin-bottom:14px">Every Hearthline begins one of two ways: cast onto the Wheel, or drawn by your own hand.</p>
    <div class="event-choices">
      <button class="btn btn-choice" data-mode="fated"><b>⚏ Play Fated</b>
        <small>The Wheel decides everything — who you are, where you wake, what you carry. Choose only how hard the year of your birth was. The honest way.</small></button>
      <button class="btn btn-choice" data-mode="architect"><b>✦ Play as Architect (cheats)</b>
        <small>Draw the life yourself: stats, wealth, starting realm, aspect and flaw, and a family of your own design whose vocations feed the household. The indulgent way. The Chronicle will know.</small></button>
      <button class="btn btn-ghost" data-mode="back">Back</button>
    </div>`;
  card.querySelectorAll('[data-mode]').forEach((b) => b.addEventListener('click', () => {
    const m = b.dataset.mode;
    if (m === 'back') return showTitle();
    createMode = m;
    if (m === 'architect') architect = { name: '', stats: { body: 3, mind: 3, spirit: 3, fate: 3 }, stones: 30, realm: 0, aspect: '', flaw: '', family: [] };
    showCreate();
  }));
}

// ---- fated: the Wheel casts you ----
function renderFated(card) {
  card.innerHTML = `
    <h2>⚏ Fated</h2>
    <label class="field"><span>A name, if you insist (leave blank and the Wheel names you)</span>
      <input id="fated-name" maxlength="24" autocomplete="off" placeholder="…"></label>
    <h3>How hard was the year of your birth?</h3>
    <div class="event-choices">
      ${Object.entries(DIFFICULTIES).map(([id, d]) => `
        <button class="btn btn-choice" data-diff="${id}"><b><span class="gold">${d.glyph}</span> ${d.name}</b><small>${d.desc}</small></button>`).join('')}
      <button class="btn btn-ghost" data-diff="back">Back</button>
    </div>`;
  card.querySelectorAll('[data-diff]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.diff === 'back') { createMode = null; return showCreate(); }
    const diffId = b.dataset.diff;
    const fx = DIFFICULTIES[diffId].fx;
    const name = $('#fated-name').value.trim() || pick(NAME_POOL);
    const lineage = pick(Object.keys(LINEAGES));
    const origin = pick(Object.keys(ORIGINS).filter((o) => o !== 'hearthborn'));
    const region = pick(fx.regions);
    beginNewGame({
      name, lineage, origin,
      custom: {
        stones: fx.stones, provisions: fx.provisions, region,
        season: fx.season ?? 0, hungry: !!fx.hungry, wound: !!fx.wound,
        difficulty: diffId,
      },
      fatedFx: fx, diffId,
    });
  }));
}

// ---- architect: draw the life yourself ----
function renderArchitect(card) {
  const a = architect;
  const statRow = (k) => `<div class="stat-stepper"><span>${k[0].toUpperCase() + k.slice(1)}</span>
    <button class="btn btn-small" data-stat="${k}" data-d="-1">−</button><b>${a.stats[k]}</b>
    <button class="btn btn-small" data-stat="${k}" data-d="1">+</button></div>`;
  card.innerHTML = `
    <h2>✦ The Architect’s Table <small class="dim">— cheats, honestly kept</small></h2>
    <label class="field"><span>Name</span><input id="arch-name" maxlength="24" value="${esc(a.name)}" placeholder="e.g. Wei Ashdown"></label>
    <h3>Ash Lineage</h3>
    <div id="create-lineages" class="card-row">${Object.entries(LINEAGES).map(([id, l]) => `
      <button class="pick-card ${createPick.lineage === id ? 'is-picked' : ''}" data-lin="${id}">
        <span class="pick-glyph el-${l.el}">${l.glyph}</span><b>${l.name}</b><small class="gold">${l.passive}</small></button>`).join('')}</div>
    <h3>Origin</h3>
    <div id="create-origins" class="card-row">${Object.entries(ORIGINS).filter(([id]) => id !== 'hearthborn').map(([id, o]) => `
      <button class="pick-card ${createPick.origin === id ? 'is-picked' : ''}" data-org="${id}">
        <b>${o.name}</b><small>${o.desc}</small></button>`).join('')}</div>
    <h3>The base of you <small class="dim">— final starting stats (lineage/origin bonuses add on top)</small></h3>
    <div class="stepper-row">${['body', 'mind', 'spirit', 'fate'].map(statRow).join('')}</div>
    <div class="stepper-row" style="margin-top:8px">
      <div class="stat-stepper"><span>Stones</span>
        <button class="btn btn-small" data-money="-100">−</button><b>${fmt(a.stones)}</b>
        <button class="btn btn-small" data-money="100">+</button></div>
      <div class="stat-stepper"><span>Realm</span>
        <button class="btn btn-small" data-realm="-1">−</button><b>${REALMS[a.realm].name}</b>
        <button class="btn btn-small" data-realm="1">+</button></div>
    </div>
    <h3>Aspect & Flaw <small class="dim">— or leave them to wake on their own</small></h3>
    <div class="stepper-row">
      <select id="arch-aspect" class="arch-select">
        <option value="">Aspect: let it wake at Sparkgathering</option>
        ${Object.entries(ASPECTS).map(([id, x]) => `<option value="${id}" ${a.aspect === id ? 'selected' : ''}>Aspect: ${x.name}</option>`).join('')}
      </select>
      <select id="arch-flaw" class="arch-select">
        <option value="">Flaw: the Wheel decides at awakening</option>
        <option value="none" ${a.flaw === 'none' ? 'selected' : ''}>Flaw: none — the Wheel looks away</option>
        ${Object.entries(FLAWS).map(([id, x]) => `<option value="${id}" ${a.flaw === id ? 'selected' : ''}>Flaw: ${x.name}</option>`).join('')}
      </select>
    </div>
    <h3>The household <small class="dim">— up to three kin; their vocations feed the life, every season</small></h3>
    ${a.family.map((kin, i) => `<div class="kin-row">
      <input class="kin-name" data-kin="${i}" maxlength="20" value="${esc(kin.name)}" placeholder="their name">
      <select class="kin-voc arch-select" data-kinv="${i}">${Object.entries(VOCATIONS).map(([id, v]) => `<option value="${id}" ${kin.vocation === id ? 'selected' : ''}>${v.name}</option>`).join('')}</select>
      <button class="btn btn-small btn-ghost" data-kinx="${i}">✕</button>
      <small class="dim kin-desc">${VOCATIONS[a.family[i].vocation].desc}</small>
    </div>`).join('')}
    ${a.family.length < 3 ? '<button class="btn btn-small" id="arch-addkin">+ Add kin</button>' : ''}
    <div class="create-actions">
      <button class="btn btn-ghost" id="arch-back">Back</button>
      <button class="btn btn-primary" id="arch-go">Light the first spark</button>
    </div>`;
  const keep = () => { a.name = $('#arch-name').value; a.aspect = $('#arch-aspect').value; a.flaw = $('#arch-flaw').value;
    card.querySelectorAll('.kin-name').forEach((inp) => { a.family[Number(inp.dataset.kin)].name = inp.value; });
    card.querySelectorAll('.kin-voc').forEach((sel) => { a.family[Number(sel.dataset.kinv)].vocation = sel.value; }); };
  card.querySelectorAll('[data-lin]').forEach((b) => b.addEventListener('click', () => { keep(); createPick.lineage = b.dataset.lin; showCreate(); }));
  card.querySelectorAll('[data-org]').forEach((b) => b.addEventListener('click', () => { keep(); createPick.origin = b.dataset.org; showCreate(); }));
  card.querySelectorAll('[data-stat]').forEach((b) => b.addEventListener('click', () => {
    keep(); const k = b.dataset.stat;
    a.stats[k] = Math.max(1, Math.min(15, a.stats[k] + Number(b.dataset.d))); showCreate();
  }));
  card.querySelectorAll('[data-money]').forEach((b) => b.addEventListener('click', () => { keep(); a.stones = Math.max(0, Math.min(99900, a.stones + Number(b.dataset.money))); showCreate(); }));
  card.querySelectorAll('[data-realm]').forEach((b) => b.addEventListener('click', () => { keep(); a.realm = Math.max(0, Math.min(4, a.realm + Number(b.dataset.realm))); showCreate(); }));
  card.querySelectorAll('[data-kinx]').forEach((b) => b.addEventListener('click', () => { keep(); a.family.splice(Number(b.dataset.kinx), 1); showCreate(); }));
  $('#arch-addkin')?.addEventListener('click', () => { keep(); a.family.push({ name: '', vocation: 'herbalist' }); showCreate(); });
  card.querySelectorAll('.kin-voc').forEach((sel) => sel.addEventListener('change', () => { keep(); showCreate(); }));
  $('#arch-back').addEventListener('click', () => { createMode = null; showCreate(); });
  $('#arch-go').addEventListener('click', () => {
    keep();
    const name = a.name.trim() || 'Wei Ashdown';
    const family = a.family.filter((k) => k.name.trim()).map((k, i) => ({ name: k.name.trim(), vocation: k.vocation }));
    beginNewGame({
      name, lineage: createPick.lineage, origin: createPick.origin,
      custom: {
        stats: { ...a.stats }, stones: a.stones, realm: a.realm,
        aspect: a.aspect || undefined, flaw: a.flaw === '' ? undefined : (a.flaw === 'none' ? null : a.flaw),
        family, cheated: true, difficulty: 'ash',
      },
    });
  });
}

function beginNewGame({ name, lineage, origin, custom, fatedFx, diffId }) {
  const slot = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'hearth'}-${Date.now().toString(36).slice(-4)}`;
  const s = newGame({ slot, name, lineage, origin, custom });
  setState(s);
  if (fatedFx?.fate) s.chr.stats.fate += fatedFx.fate;
  const r = REGIONS.find((x) => x.id === s.world.region);
  if (custom?.cheated) {
    A.log(`<b>${esc(name)}</b> — drawn at the Architect’s Table: the ${LINEAGES[lineage].name}, ${ORIGINS[origin].name.toLowerCase()}, ${REALMS[s.chr.realm].name}-born, with ${fmt(s.chr.stones)} stones and ${s.meta.family.length ? `a household of ${s.meta.family.length}` : 'no kin but ambition'}. The Wheel watches this arrangement with one raised eyebrow. <i>It counts everything eventually.</i>`, 'l-story');
  } else {
    A.log(`<b>${esc(name)}</b> — cast by the Wheel into ${r.name}, born to the ${LINEAGES[lineage].name}, ${ORIGINS[origin].name.toLowerCase()}, in ${DIFFICULTIES[diffId].name.toLowerCase()}. ${DIFFICULTIES[diffId].fx.hungry ? 'The larder is nearly bare and winter is here. Live anyway.' : 'What you have is what your family had. Begin.'} <i>Meditate to feel the Emberlight. Wander to meet the world.</i>`, 'l-story');
  }
  persist(true);
  createMode = null;
  startGame();
}

// ---------------- main game ----------------
export function startGame() {
  if (!A.weatherNow()) A.rollWeather();
  show('#screen-game');
  renderAll();
  if (S.pendingSuccession) showSuccession();
  else {
    A.checkStory();
    if (S.chr.realm >= 2 && !S.chr.dao) A.daoChoice();
  }
}

export function renderAll() {
  if (!S) return;
  renderTopbar(); renderChar(); renderRegion(); renderSide(); renderLog();
}

function renderTopbar() {
  $('#date-label').textContent = dateLabel(S.world) + ` · Gen ${S.meta.generation}`;
  $('#ap-pips').innerHTML = 'Actions ' + Array.from({ length: 3 }, (_, i) =>
    `<span class="pip ${i < S.world.ap ? 'pip-on' : ''}"></span>`).join('');
}

function bar(cls, cur, max, label) {
  const p = max ? Math.min(100, (cur / max) * 100) : 0;
  return `<div class="bar" title="${label}"><div class="bar-fill ${cls}" style="width:${p}%"></div><span>${label}: ${fmt(Math.floor(cur))}/${fmt(max)}</span></div>`;
}

function renderChar() {
  const c = S.chr, d = derived();
  const lin = LINEAGES[c.lineage];
  const cost = stageCost();
  const peak = atPeak();
  const lifeLeft = d.lifespan - c.age;
  const canAdvance = c.cult >= cost && !peak;
  const canBreak = c.cult >= cost && peak && c.realm < 5;
  $('#char-panel').innerHTML = `
    <div class="char-head">
      <span class="char-glyph el-${lin.el}">${lin.glyph}</span>
      <div><div class="char-name">${esc(c.name)}</div>
      <div class="char-realm">${realmLabel(c)}</div></div>
    </div>
    ${bar('bar-hp', c.hp, d.hpMax, 'Health')}
    ${bar('bar-qi', c.qi, d.qiMax, 'Emberlight')}
    ${bar('bar-cult', Math.min(c.cult, cost || 1), cost || 1, 'Cultivation')}
    ${c.cult > cost && cost ? `<div class="dim tiny">…and ${fmt(c.cult - cost)} banked beyond the bottleneck</div>` : ''}
    <div class="stat-grid">
      <span title="Muscle, blood, damage, health">Body <b>${d.body}</b></span>
      <span title="Wits, alchemy, checks">Mind <b>${d.mind}</b></span>
      <span title="Qi capacity and meditation">Spirit <b>${d.spirit}</b></span>
      <span title="Luck bends toward you">Fate <b>${d.fate}</b></span>
      <span title="Attack rating">Strike <b>${d.atk}</b></span>
      <span title="Critical chance">Crit <b>${Math.round(d.crit)}%</b></span>
    </div>
    <div class="char-rows">
      <div>Age <b>${c.age}</b> <span class="dim">of ~${d.lifespan}${lifeLeft <= 8 ? ' ⚠' : ''}</span></div>
      <div>Stones <b class="gold">${fmt(c.stones)}</b> · Provisions <b>${c.inventory.provisions || 0}</b></div>
      <div class="dim">The Wheel’s hum: <b class="${c.karma >= 3 ? 'gold' : c.karma <= -3 ? 'bad' : 'dim'}">${A.karmaWord()}</b>${c.aspect === 'karmic_sight' ? ` <span class="dim">(${c.karma})</span>` : ''}</div>
      ${c.sect.joined ? `<div>${SECT_RANKS[sectRank(c)].name} <b>${fmt(c.sect.contrib)}</b> <span class="dim">contrib</span></div>` : ''}
      ${c.grace ? `<div class="dim">Tribulation ward: +${c.grace} bolt</div>` : ''}
      ${c.injured ? '<div class="bad">Bruised — slower next season</div>' : ''}
    </div>
    <div class="chip-row">
      ${c.dao ? `<span class="chip chip-gold" title="${esc(DAOS[c.dao].desc)}">${DAOS[c.dao].glyph} ${DAOS[c.dao].name}</span>` : ''}
      ${c.insights?.length ? `<span class="chip chip-gold" title="Insights: understanding earned from the world. Each speeds all cultivation +6%; every third endures +1 tribulation bolt. See the Paths tab.">✧ ${c.insights.length} insight${c.insights.length === 1 ? '' : 's'} · +${c.insights.length * 6}%</span>` : ''}
      ${c.hungry ? '<span class="chip chip-bad" title="No provisions last season — cultivation suffers until fed">Hungry</span>' : ''}
      ${c.aspect ? `<span class="chip chip-gold" title="${esc(ASPECTS[c.aspect].desc)}">${ASPECTS[c.aspect].name}</span>` : ''}
      ${c.flaw ? `<span class="chip chip-bad" title="${esc(FLAWS[c.flaw].desc)}">${FLAWS[c.flaw].name}</span>` : ''}
      ${(c.wounds || []).map((w) => `<span class="chip chip-bad" title="${esc(WOUNDS[w].desc)} Cured by a Duskclear Pill or a healer.">${WOUNDS[w].name}</span>`).join('')}
      ${(c.vows || []).map((v) => `<span class="chip chip-vow" title="${esc(VOWS[v.id].desc)}">${VOWS[v.id].name} · ${v.seasonsLeft}</span>`).join('')}
    </div>
    <div class="equip-rows">
      ${['weapon', 'charm'].map((slot) => {
        const id = c.equip[slot];
        return `<button class="equip-slot" data-unequip="${slot}" title="${id ? 'click to unequip' : 'empty ' + slot + ' slot'}">
          <small>${slot}</small><b>${id ? esc(ITEMS[id].name) : '—'}</b></button>`;
      }).join('')}
    </div>
    ${canAdvance ? `<button class="btn btn-primary btn-block" id="btn-stageup">Advance to ${STAGE_NAMES[c.stage + 1]} ${REALMS[c.realm].name}</button>` : ''}
    ${canBreak ? `<button class="btn btn-primary btn-block btn-pulse" id="btn-breakthrough">⚡ Face the ${REALMS[c.realm + 1].name} Tribulation</button>` : ''}
    ${peak && c.realm === 5 ? '<div class="gold tiny">Only the Tenth Dawn remains. Seek the Ninth Crater’s heart.</div>' : ''}
  `;
  $('#btn-stageup')?.addEventListener('click', () => A.stageUp());
  $('#btn-breakthrough')?.addEventListener('click', () => A.attemptBreakthrough());
  document.querySelectorAll('[data-unequip]').forEach((b) => b.addEventListener('click', () => A.unequip(b.dataset.unequip)));
}

function renderRegion() {
  const r = A.region();
  const dom = A.domainOf(r);
  $('#region-bar').innerHTML =
    `<button class="region-chip region-chip-map" data-openmap title="The known world — four domains and the roads between">🗺 ${dom.name}</button>` +
    REGIONS.filter((x) => (x.domain || 'vale') === dom.id).map((x) => {
      const locked = S.chr.realm < x.minRealm;
      return `<button class="region-chip ${x.id === r.id ? 'is-here' : ''} ${locked ? 'is-locked' : ''}"
        data-region="${x.id}" ${x.id === r.id ? 'aria-current="true"' : ''} title="${locked ? `Requires ${REALMS[x.minRealm].name}` : x.desc}">
        ${locked ? '🔒 ' : ''}${x.name}</button>`;
    }).join('');
  const wx = A.weatherNow();
  const mood = wx ? (['glasswind', 'ashrain', 'palefog'].includes(wx.id) ? 'storm' : ['ironcold', 'firstfrost', 'ashsnow'].includes(wx.id) ? 'cold' : 'calm') : 'calm';
  document.dispatchEvent(new CustomEvent('emberline:weather', { detail: mood }));
  $('#region-desc').innerHTML = `<b>${r.name}</b> <span class="dim">· ${dom.name} · Emberlight density ×${r.qiMult}</span>${wx ? ` <span class="gold">· ${wx.name}</span>` : ''}<br><span class="dim">${r.desc}</span>`;
  const discovered = DISCOVERIES.filter((d) => d.region === r.id && S.world.discovered.includes(d.id));
  $('#action-grid').innerHTML = r.actions.map((kind) => {
    const a = r.labels?.[kind] ? { ...ACTION_DEFS[kind], ...r.labels[kind] } : ACTION_DEFS[kind];
    let extra = '';
    if (kind === 'temple_depths' && S.chr.flags.abbot_slain) extra = ' is-disabled';
    return `<button class="action-card${extra}" data-act="${kind}">
      <b>${a.label}</b><small>${a.desc}</small>
      <span class="ap-cost">${a.ap ? '●'.repeat(a.ap) : 'free'}</span></button>`;
  }).join('') + discovered.map((d) => `<button class="action-card action-found" data-act="disc_${d.id}">
      <b>✦ ${d.action.label}</b><small>${d.action.desc}</small>
      <span class="ap-cost">${d.action.ap ? '●'.repeat(d.action.ap) : 'free'}</span></button>`).join('')
  + (r.id === 'sect' && !S.chr.sect.joined
    ? `<button class="action-card action-join" data-act="__join"><b>Seek Admission</b><small>Climb the ten thousand steps and take the entry trial.</small><span class="ap-cost">●</span></button>` : '');
  $('[data-openmap]')?.addEventListener('click', openWorldMap);
  document.querySelectorAll('[data-region]').forEach((b) => b.addEventListener('click', () => A.travelTo(b.dataset.region)));
  document.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.act === '__join') return A.joinSectTrial();
    if (b.dataset.act === 'missions' || b.dataset.act === 'library' || b.dataset.act === 'store') {
      if (!S.chr.sect.joined) return toastMsg('Only disciples may use the sect halls. Seek admission first.');
    }
    A.doRegionAction(b.dataset.act);
  }));
}

function renderLog() {
  const box = $('#log');
  box.innerHTML = S.log.slice(-60).map((l) =>
    `<div class="log-line ${l.cls || ''}"><span class="log-when">${l.when}</span>${l.t}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}

// ---------------- side panel ----------------
let sideTab = 'inventory';
const TABS = [['inventory', 'Pack'], ['techniques', 'Arts'], ['quests', 'Paths'], ['world', 'World'], ['dynasty', 'Hearth']];
function renderSide() {
  $('#side-tabs').innerHTML = TABS.map(([id, label]) =>
    `<button class="side-tab ${sideTab === id ? 'is-active' : ''}" data-tab="${id}">${label}</button>`).join('');
  document.querySelectorAll('[data-tab]').forEach((b) => b.addEventListener('click', () => { sideTab = b.dataset.tab; renderSide(); }));
  const box = $('#side-content');
  if (sideTab === 'inventory') {
    const entries = Object.entries(S.chr.inventory);
    box.innerHTML = entries.length ? entries.map(([id, q]) => {
      const it = ITEMS[id];
      const verb = it.kind === 'artifact' ? 'Equip' : it.kind === 'manual' ? 'Study' : it.kind === 'pill' ? 'Take' : null;
      return `<div class="inv-row" title="${esc(it.desc)}">
        <span><b>${esc(it.name)}</b> <span class="dim">×${q}</span><br><small class="dim">${it.kind}</small></span>
        ${verb ? `<button class="btn btn-small" data-use="${id}">${verb}</button>` : ''}
      </div>`;
    }).join('') : '<div class="dim pad">Empty pockets, light footsteps.</div>';
    box.querySelectorAll('[data-use]').forEach((b) => b.addEventListener('click', () => A.useInventoryItem(b.dataset.use)));
  } else if (sideTab === 'techniques') {
    box.innerHTML = `<div class="dim pad tiny">Your combat deck holds 4 arts. Tap to add or remove.</div>` +
      S.chr.techs.filter((t) => t !== 'fistform').map((tid) => {
        const t = TECHNIQUES[tid];
        const inDeck = S.chr.deck.includes(tid);
        const passive = t.fx?.passiveMeditate;
        return `<button class="tech-row ${inDeck ? 'is-decked' : ''}" data-decktoggle="${tid}" ${passive ? 'disabled' : ''}>
          <b>${t.name}</b> ${t.el ? `<span class="el-dot el-${t.el}"></span>` : ''} ${passive ? '<small class="gold">passive</small>' : inDeck ? '<small class="gold">in deck</small>' : ''}
          <br><small class="dim">${t.desc}</small>
          <small>${t.qi ? `${t.qi} qi` : 'free'}${t.cd ? ` · ${t.cd}t cooldown` : ''}</small></button>`;
      }).join('');
    box.querySelectorAll('[data-decktoggle]').forEach((b) => b.addEventListener('click', () => A.toggleDeck(b.dataset.decktoggle)));
  } else if (sideTab === 'quests') {
    const mis = S.chr.sect.mission;
    box.innerHTML = (mis ? `<div class="quest-row"><b>Sect Mission: ${esc(mis.name)}</b><br>
      <small class="dim">${mis.kind === 'hunt' ? (mis.done ? 'Quarry slain — report to the board.' : 'Hunt it where it lives.') : `Deliver at the board.`}</small></div>` : '') +
      A.questStates().filter((q) => !(q.tutorial && (q.isDone || S.meta.generation > 1))).map((q) => `<div class="quest-row ${q.isDone ? 'is-done' : ''}">
        <b>${q.isDone ? '✓ ' : ''}${q.name}</b><br><small class="dim">${q.desc}</small></div>`).join('') +
      `<div class="pad"><b>✧ Insights</b> <small class="dim">${S.chr.insights.length} held · all cultivation +${S.chr.insights.length * 6}% · +${Math.floor(S.chr.insights.length / 3)} tribulation bolt${Math.floor(S.chr.insights.length / 3) === 1 ? '' : 's'} endured</small></div>` +
      S.chr.insights.map((id) => `<div class="quest-row"><b class="gold">✧ ${INSIGHTS[id].name}</b><br><small class="dim">${INSIGHTS[id].desc}</small></div>`).join('') +
      `<div class="pad tiny dim">Still unlearned: ${Object.entries(INSIGHTS).filter(([id]) => !S.chr.insights.includes(id)).map(([, i]) => i.hint).slice(0, 4).join(' · ')}${Object.keys(INSIGHTS).length - S.chr.insights.length > 4 ? ' · …' : ''}</div>`;
  } else if (sideTab === 'world') {
    const wx = A.weatherNow();
    const you = { name: `${S.chr.name} (you)`, realm: S.chr.realm, epithet: LINEAGES[S.chr.lineage].name };
    const roll = [
      ...S.world.rivals.map((rv) => ({ ...RIVALS.find((r) => r.id === rv.id), realm: rv.realm, dead: rv.dead, grudge: rv.grudge })),
      you,
      ...(window.__ghosts || []).map((g) => ({ name: `${g.name} ✧`, realm: g.realm, epithet: `of another hearth (Gen ${g.generation})` })),
    ].sort((a, b) => b.realm - a.realm || (b.name.includes('(you)') ? 1 : -1));
    box.innerHTML = `
      ${wx ? `<div class="pad"><b>${wx.name}</b><br><small class="dim">${wx.desc}</small></div>` : ''}
      <div class="pad tiny dim">The Wheel’s hum around you: <b>${A.karmaWord()}</b></div>
      <div class="pad"><b>The Roll of Names</b><br><small class="dim">Those the teahouses argue about. They cultivate whether you watch or not.</small></div>
      ${roll.map((r, i) => `<div class="inv-row" ${r.dead ? 'style="opacity:.45"' : ''}><span>${i + 1}. <b>${esc(r.name)}</b>${r.grudge ? ' <span class="chip chip-bad" title="They hold a grudge against you — the roads between domains are less safe">grudge</span>' : ''}<br><small class="dim">${esc(r.epithet || '')}</small></span>
        <small class="${r.name.includes('(you)') ? 'gold' : 'dim'}">${r.dead ? '† fallen' : REALMS[r.realm].name}</small></div>`).join('')}
      <div class="pad"><b>Word on the roads</b></div>
      ${S.world.news.length ? S.world.news.map((n) => `<div class="quest-row"><small class="dim">${n.when}</small><br><small>${esc(n.text)}</small></div>`).join('')
        : '<div class="pad dim tiny">The roads are quiet. It never lasts.</div>'}`;
  } else if (sideTab === 'dynasty') {
    const m = S.meta;
    box.innerHTML = `
      <div class="pad"><b class="gold">☲ Hearthflame: ${fmt(m.hearthflame)}</b><br>
      <small class="dim">Generation ${m.generation} · earned by deeds, spent at the Hearth Shrine on the bloodline.</small></div>
      ${Object.entries(m.perks).length ? '<div class="pad tiny"><b>Bloodline:</b> ' + Object.entries(m.perks).map(([id, lv]) =>
        `${PERKS.find((p) => p.id === id).name}${lv > 1 ? ' ' + 'ⅠⅡⅢ'[lv - 1] : ''}`).join(' · ') + '</div>' : ''}
      ${m.chronicle?.length ? '<div class="pad"><b>The Chronicle</b><br><small class="dim">The story so far, as the world will tell it. Tap a chapter to reread.</small></div>' +
        m.chronicle.map((c, i) => `<button class="tech-row" data-chron="${i}"><small class="dim">${esc(c.book)}</small><br><b>${esc(c.title)}</b></button>`).join('') : ''}
      ${m.family?.length ? '<div class="pad"><b>The Household</b></div>' + m.family.map((k) => `
        <div class="inv-row"><span><b>${esc(k.name)}</b><br><small class="dim">${VOCATIONS[k.vocation].name} — ${VOCATIONS[k.vocation].desc}</small></span></div>`).join('') : ''}
      ${m.cheated ? '<div class="pad tiny dim">✦ This line was drawn at the Architect’s Table. The Wheel counts everything eventually.</div>' : ''}
      ${m.ancestors.length ? '<div class="pad tiny dim">The honored dead:</div>' + m.ancestors.map((a) => `
        <div class="ancestor-row"><b>${esc(a.name)}</b> <small class="dim">Gen ${a.gen}</small><br>
        <small>${esc(a.realm)}, died at ${a.age} — ${esc(a.cause)}</small>
        ${a.deeds.length ? `<br><small class="dim">${a.deeds.map(esc).join(' · ')}</small>` : ''}</div>`).join('')
        : '<div class="pad dim tiny">No ancestors yet. May that stay true a long while.</div>'}
      <div class="pad"><button class="btn btn-ghost" id="btn-torch">Pass the Torch…</button>
      ${m.chronicle?.length ? '<button class="btn btn-ghost" id="btn-export">Export the Chronicle</button>' : ''}</div>`;
    $('#btn-export')?.addEventListener('click', exportChronicle);
    $('#btn-torch')?.addEventListener('click', () => A.passTorch());
    box.querySelectorAll('[data-chron]').forEach((b) => b.addEventListener('click', () =>
      showStoryPage(S.meta.chronicle[Number(b.dataset.chron)], true)));
  }
}

// ---------------- the world map ----------------
function openWorldMap() {
  const m = openModal({ title: 'The Known World', wide: true });
  const here = A.region();
  const nodes = REGIONS.map((r) => {
    const dom = A.domainOf(r);
    const domLocked = S.chr.realm < dom.minRealm;
    const locked = domLocked || S.chr.realm < r.minRealm;
    const isHere = r.id === here.id;
    const unseen = !locked && !S.world.visited.includes(r.id);
    return `<g class="map-node ${locked ? 'is-locked' : ''} ${isHere ? 'is-here' : ''}" data-mapgo="${r.id}" transform="translate(${r.map.x},${r.map.y})">
      ${isHere ? '<circle r="13" class="map-here-ring"/>' : ''}
      <circle r="7" class="map-dot ${unseen ? 'map-dot-unseen' : ''}"/>
      ${locked ? '<text y="4" class="map-lock">🔒</text>' : ''}
      <text y="22" class="map-name">${unseen ? '— unvisited —' : esc(r.name)}</text>
    </g>`;
  }).join('');
  const lands = DOMAINS.map((d) => {
    const locked = S.chr.realm < d.minRealm;
    return `<g class="map-domain ${locked ? 'is-locked' : ''}">
      <path d="${d.path}" class="map-land"/>
      <text x="${d.label.x}" y="${d.label.y}" class="map-domain-name">${esc(d.name).toUpperCase()}</text>
      ${locked ? `<text x="${d.label.x}" y="${d.label.y + 17}" class="map-domain-req">sealed until ${esc(REALMS[d.minRealm].name)}</text>` : ''}
    </g>`;
  }).join('');
  m.body.innerHTML = `
    <div class="mg-hint">Four domains under a sunless sky. Travel within a domain is a walk; crossing between domains is a journey (−1 action, and the roads have opinions). Locked lands demand higher realms.</div>
    <svg viewBox="0 0 1000 620" class="world-map" role="img" aria-label="Map of the known world">
      <defs>
        <radialGradient id="mapglow" cx="50%" cy="45%"><stop offset="0%" stop-color="rgba(229,138,58,0.10)"/><stop offset="100%" stop-color="rgba(0,0,0,0)"/></radialGradient>
      </defs>
      <rect x="0" y="0" width="1000" height="620" class="map-sea"/>
      <rect x="0" y="0" width="1000" height="620" fill="url(#mapglow)"/>
      ${lands}
      <g class="map-roads">
        <path d="M330,330 Q380,330 430,330" class="map-road"/>
        <path d="M620,400 Q650,450 660,480" class="map-road"/>
        <path d="M600,180 Q650,160 680,145" class="map-road"/>
      </g>
      ${nodes}
      <text x="500" y="608" class="map-caption">— the world is wider than any one life; that is what heirs are for —</text>
    </svg>`;
  m.body.querySelectorAll('[data-mapgo]').forEach((g) => g.addEventListener('click', async () => {
    const id = g.dataset.mapgo;
    m.close();
    await A.travelTo(id);
  }));
}

// ---------------- shops & halls ----------------
function openPanel(kind) {
  if (kind === 'market') return openMarket();
  if (kind === 'shrine') return openShrine();
  if (kind === 'missions') return openMissions();
  if (kind === 'library') return openLibrary();
  if (kind === 'store') return openSectStore();
}

function openMarket() {
  const r = A.region();
  const m = openModal({ title: `${r.name} — Market`, wide: true });
  const shenStock = r.id === 'cinder_market' && (S.chr.rel.merchant_shen || 0) >= 6 ? (r.privateStock || []) : [];
  const priceTag = (id) => {
    const it = ITEMS[id];
    const cat = S.world.prices[it.kind] !== undefined ? it.kind : null;
    const drift = cat ? S.world.prices[cat] : 1;
    const arrow = drift > 1.08 ? ' ▲' : drift < 0.92 ? ' ▼' : '';
    return `${A.marketPrice(id)} ◈${arrow}`;
  };
  const render = () => {
    m.body.innerHTML = `<div class="market-cols">
      <div><h4>For sale <small class="dim">(you hold ${fmt(S.chr.stones)} stones · prices drift with the seasons)</small></h4>
        ${(r.market || []).map((id) => {
          const it = ITEMS[id];
          const can = S.chr.stones >= A.marketPrice(id);
          return `<div class="inv-row" title="${esc(it.desc)}"><span><b>${esc(it.name)}</b><br><small class="dim">${it.desc}</small></span>
            <button class="btn btn-small ${can ? '' : 'is-disabled'}" data-buy="${id}" ${can ? '' : 'disabled'}>${priceTag(id)}</button></div>`;
        }).join('')}
        ${shenStock.length ? `<h4 class="gold">Shen’s back room <small class="dim">(for kin only)</small></h4>` + shenStock.map((id) => {
          const it = ITEMS[id];
          const can = S.chr.stones >= A.marketPrice(id);
          return `<div class="inv-row" title="${esc(it.desc)}"><span><b>${esc(it.name)}</b><br><small class="dim">${it.desc}</small></span>
            <button class="btn btn-small ${can ? '' : 'is-disabled'}" data-buy="${id}" ${can ? '' : 'disabled'}>${priceTag(id)}</button></div>`;
        }).join('') : ''}</div>
      <div><h4>Your goods <small class="dim">(sell at half value)</small></h4>
        ${Object.entries(S.chr.inventory).filter(([id]) => ITEMS[id].price > 0).map(([id, q]) => {
          const it = ITEMS[id];
          return `<div class="inv-row"><span><b>${esc(it.name)}</b> <span class="dim">×${q}</span></span>
            <button class="btn btn-small" data-sell="${id}">+${A.marketPrice(id, true)} ◈</button></div>`;
        }).join('') || '<div class="dim">Nothing worth a merchant’s squint.</div>'}</div>
    </div>`;
    m.body.querySelectorAll('[data-buy]').forEach((b) => b.addEventListener('click', () => { A.buyItem(b.dataset.buy); render(); }));
    m.body.querySelectorAll('[data-sell]').forEach((b) => b.addEventListener('click', () => { A.sellItem(b.dataset.sell); render(); }));
  };
  render();
}

function openShrine() {
  const m = openModal({ title: 'The Hearth Shrine', wide: true });
  const render = () => {
    m.body.innerHTML = `
      <p class="event-text">The family flame burns in its iron cradle, fed by every generation since the sky fell.
      <b class="gold">☲ ${fmt(S.meta.hearthflame)} Hearthflame</b> — spend it to deepen the bloodline for this life and every life after.</p>
      <div class="perk-grid">${PERKS.map((p) => {
        const lvl = perkLevel(S.meta, p.id);
        const maxed = lvl >= p.tiers.length;
        const cost = maxed ? null : p.tiers[lvl].cost;
        const can = !maxed && S.meta.hearthflame >= cost;
        return `<div class="perk-card ${lvl ? 'is-owned' : ''}">
          <b>${p.name}</b> ${lvl ? `<span class="gold">${'ⅠⅡⅢ'[lvl - 1]}</span>` : ''}<br><small class="dim">${p.desc}</small><br>
          ${maxed ? '<small class="gold">bloodline saturated</small>'
            : `<button class="btn btn-small ${can ? '' : 'is-disabled'}" data-perk="${p.id}" ${can ? '' : 'disabled'}>Deepen — ☲${cost}</button>`}
        </div>`;
      }).join('')}</div>
      <h4>The Estate <small class="dim">(stone outlives flesh — bought once, kept by every generation)</small></h4>
      <div class="perk-grid">${Object.entries(A.ESTATE_TRACKS).map(([track, t]) => {
        const tier = S.meta.estate[track] || 0;
        const cost = 150 * (tier + 1);
        return `<div class="perk-card ${tier ? 'is-owned' : ''}"><b>${t.name}</b> ${tier ? `<span class="gold">${'ⅠⅡⅢ'[tier - 1]}</span>` : ''}<br>
          <small class="dim">${t.desc}</small><br>
          ${tier >= 3 ? '<small class="gold">complete</small>' : `<button class="btn btn-small ${S.chr.stones >= cost ? '' : 'is-disabled'}" data-estate="${track}" ${S.chr.stones >= cost ? '' : 'disabled'}>Build — ${cost} ◈</button>`}</div>`;
      }).join('')}</div>`;
    m.body.querySelectorAll('[data-perk]').forEach((b) => b.addEventListener('click', () => { A.buyPerk(b.dataset.perk); render(); }));
    m.body.querySelectorAll('[data-estate]').forEach((b) => b.addEventListener('click', () => { A.buyEstate(b.dataset.estate); render(); }));
  };
  render();
}

function openMissions() {
  const m = openModal({ title: 'Mission Board — Order of the Kindled Path', wide: true });
  const render = () => {
    const mis = S.chr.sect.mission;
    const avail = MISSIONS.filter((t) => t.minRealm <= S.chr.realm);
    m.body.innerHTML = `
      ${mis ? `<div class="inv-row"><span><b>Underway: ${esc(mis.name)}</b><br><small class="dim">${mis.kind === 'hunt' ? (mis.done ? 'Complete — collect your due.' : 'Hunt your quarry where it lives.') : 'Deliver the goods here.'}</small></span>
        <button class="btn btn-small" data-turnin>Turn in</button></div><hr>` : ''}
      <h4>Postings <small class="dim">(contribution ${fmt(S.chr.sect.contrib)})</small></h4>
      ${avail.map((t) => `<div class="inv-row"><span><b>${esc(t.name)}</b><br>
        <small class="dim">${t.contrib} contribution${t.stones > 0 ? `, ${t.stones} stones` : ''}</small></span>
        <button class="btn btn-small" data-take="${t.id}" ${mis && t.kind !== 'donate' ? 'disabled' : ''}>${t.kind === 'donate' ? 'Tithe' : 'Take'}</button></div>`).join('')}`;
    m.body.querySelector('[data-turnin]')?.addEventListener('click', () => { A.turnInMission(); render(); });
    m.body.querySelectorAll('[data-take]').forEach((b) => b.addEventListener('click', () => { A.takeMission(b.dataset.take); render(); }));
  };
  render();
}

function openLibrary() {
  const rank = sectRank(S.chr);
  const m = openModal({ title: 'Scripture Library', wide: true });
  const render = () => {
    m.body.innerHTML = `<div class="dim tiny pad">Rank: ${SECT_RANKS[rank].name} · contribution ${fmt(S.chr.sect.contrib)}</div>` +
      LIBRARY.map((e) => {
        const t = TECHNIQUES[e.tech];
        const known = S.chr.techs.includes(e.tech);
        const rankOk = rank >= e.rank;
        const realmOk = !t.req?.realm || S.chr.realm >= t.req.realm;
        const can = !known && rankOk && realmOk && S.chr.sect.contrib >= e.contrib;
        return `<div class="inv-row"><span><b>${t.name}</b> ${t.el ? `<span class="el-dot el-${t.el}"></span>` : ''}<br><small class="dim">${t.desc}</small></span>
          ${known ? '<small class="gold">known</small>' : !rankOk ? `<small class="dim">needs ${SECT_RANKS[e.rank].name}</small>` : !realmOk ? `<small class="dim">needs ${REALMS[t.req.realm].name}</small>`
            : `<button class="btn btn-small ${can ? '' : 'is-disabled'}" data-learn="${e.tech}" data-cost="${e.contrib}" ${can ? '' : 'disabled'}>${e.contrib} contrib</button>`}
        </div>`;
      }).join('');
    m.body.querySelectorAll('[data-learn]').forEach((b) => b.addEventListener('click', () => {
      A.learnFromLibrary(b.dataset.learn, Number(b.dataset.cost)); render();
    }));
  };
  render();
}

function openSectStore() {
  const rank = sectRank(S.chr);
  const m = openModal({ title: 'Sect Store', wide: true });
  const render = () => {
    m.body.innerHTML = `<div class="dim tiny pad">Rank: ${SECT_RANKS[rank].name} · contribution ${fmt(S.chr.sect.contrib)}</div>` +
      SECT_STORE.map((e) => {
        const it = ITEMS[e.item];
        const rankOk = rank >= (e.rank || 0);
        const can = rankOk && S.chr.sect.contrib >= e.contrib;
        return `<div class="inv-row" title="${esc(it.desc)}"><span><b>${esc(it.name)}</b><br><small class="dim">${it.desc}</small></span>
          ${!rankOk ? `<small class="dim">needs ${SECT_RANKS[e.rank].name}</small>`
            : `<button class="btn btn-small ${can ? '' : 'is-disabled'}" data-sbuy="${e.item}" data-cost="${e.contrib}" ${can ? '' : 'disabled'}>${e.contrib} contrib</button>`}</div>`;
      }).join('');
    m.body.querySelectorAll('[data-sbuy]').forEach((b) => b.addEventListener('click', () => {
      A.buyFromSectStore(b.dataset.sbuy, Number(b.dataset.cost)); render();
    }));
  };
  render();
}

// ---------------- the Chronicle: story pages ----------------
function showStoryPage(entry, reread = false) {
  const m = openModal({ wide: true, locked: !reread });
  m.body.innerHTML = `
    <div class="story-page">
      <div class="story-book">${esc(entry.book)}</div>
      <div class="story-orn">✦ &nbsp;卷&nbsp; ✦</div>
      <h2 class="story-title">${esc(entry.title)}</h2>
      <div class="story-text">${entry.text}</div>
      <div class="event-choices"><button class="btn btn-choice" data-close>${reread ? 'Close the Chronicle' : 'Turn the page'}</button></div>
    </div>`;
  m.body.querySelector('[data-close]').addEventListener('click', () => {
    m.close();
    if (!reread) A.storyClosed();
  });
}

// ---------------- succession ----------------
function showSuccession() {
  const ps = S.pendingSuccession;
  const a = S.meta.ancestors[S.meta.ancestors.length - 1];
  const estate = A.estateForKit();
  const kit = { items: {}, stones: estate.stones };
  let heir = { name: '', lineage: a.lineage };
  const m = openModal({ title: 'The Hearthline Continues', wide: true, locked: true });
  let step = 0;

  const counts = () => {
    let art = 0, man = 0, pills = 0;
    for (const [id, q] of Object.entries(kit.items)) {
      const k = ITEMS[id].kind;
      if (k === 'artifact') art += q; if (k === 'manual') man += q; if (k === 'pill') pills += q;
    }
    return { art, man, pills };
  };

  const render = () => {
    if (step === 0) {
      m.body.innerHTML = `
        <p class="event-text"><b>${esc(a.name)}</b> — ${esc(a.realm)}, ${esc(String(a.age))} years, ${esc(a.cause)}.<br><br>
        The village turns out in gray. The shrine keeper speaks the old words: <i>"What burned, warmed. What warmed, endures."</i>
        ${a.deeds.length ? `<br><br>Deeds remembered: ${a.deeds.map(esc).join(' · ')}.` : ''}<br><br>
        <b class="gold">☲ +${ps.earn} Hearthflame</b> flows into the family shrine (total ${fmt(S.meta.hearthflame)}).</p>
        <div class="event-choices"><button class="btn btn-choice" data-next>Tend the flame</button></div>`;
      m.body.querySelector('[data-next]').addEventListener('click', () => { step = 1; render(); });
    } else if (step === 1) {
      m.body.innerHTML = `<p class="event-text">Before the heir comes of age, the bloodline can be deepened.
        <b class="gold">☲ ${fmt(S.meta.hearthflame)} Hearthflame</b></p>
        <div class="perk-grid">${PERKS.map((p) => {
          const lvl = perkLevel(S.meta, p.id);
          const maxed = lvl >= p.tiers.length;
          const cost = maxed ? null : p.tiers[lvl].cost;
          const can = !maxed && S.meta.hearthflame >= cost;
          return `<div class="perk-card ${lvl ? 'is-owned' : ''}"><b>${p.name}</b> ${lvl ? `<span class="gold">${'ⅠⅡⅢ'[lvl - 1]}</span>` : ''}<br>
            <small class="dim">${p.desc}</small><br>
            ${maxed ? '<small class="gold">saturated</small>' : `<button class="btn btn-small ${can ? '' : 'is-disabled'}" data-perk="${p.id}" ${can ? '' : 'disabled'}>☲${cost}</button>`}</div>`;
        }).join('')}</div>
        <div class="event-choices"><button class="btn btn-choice" data-next>Assemble the Progeny Kit</button></div>`;
      m.body.querySelectorAll('[data-perk]').forEach((b) => b.addEventListener('click', () => { A.buyPerk(b.dataset.perk); render(); }));
      m.body.querySelector('[data-next]').addEventListener('click', () => { step = 2; render(); });
    } else if (step === 2) {
      const c = counts();
      const row = (id, q, kind, max, cur) => {
        const inKit = kit.items[id] || 0;
        const canAdd = inKit < q && cur < max;
        return `<div class="inv-row"><span><b>${esc(ITEMS[id].name)}</b> <span class="dim">×${q}</span>${inKit ? ` <span class="gold">→ ${inKit} packed</span>` : ''}</span>
          <span>${inKit ? `<button class="btn btn-small" data-kit-sub="${id}">−</button>` : ''}
          <button class="btn btn-small ${canAdd ? '' : 'is-disabled'}" data-kit-add="${id}" ${canAdd ? '' : 'disabled'}>+</button></span></div>`;
      };
      m.body.innerHTML = `
        <p class="event-text"><b>The Progeny Kit.</b> Time is a tax collector: of the estate, an heir may keep
        <b>one artifact</b>, <b>one manual</b>, and <b>three pills</b> — plus <b class="gold">${fmt(kit.stones)} stones</b> (a third survives probate, creditors, and cousins).</p>
        <h4>Artifacts <small class="dim">${c.art}/1</small></h4>${estate.artifacts.map(([id, q]) => row(id, q, 'artifact', 1, c.art)).join('') || '<div class="dim tiny pad">None owned.</div>'}
        <h4>Manuals <small class="dim">${c.man}/1</small></h4>${estate.manuals.map(([id, q]) => row(id, q, 'manual', 1, c.man)).join('') || '<div class="dim tiny pad">None owned.</div>'}
        <h4>Pills <small class="dim">${c.pills}/3</small></h4>${estate.pills.map(([id, q]) => row(id, q, 'pill', 3, c.pills)).join('') || '<div class="dim tiny pad">None owned.</div>'}
        <div class="event-choices"><button class="btn btn-choice" data-next>Seal the kit</button></div>`;
      m.body.querySelectorAll('[data-kit-add]').forEach((b) => b.addEventListener('click', () => {
        kit.items[b.dataset.kitAdd] = (kit.items[b.dataset.kitAdd] || 0) + 1; render();
      }));
      m.body.querySelectorAll('[data-kit-sub]').forEach((b) => b.addEventListener('click', () => {
        kit.items[b.dataset.kitSub] -= 1; if (kit.items[b.dataset.kitSub] <= 0) delete kit.items[b.dataset.kitSub]; render();
      }));
      m.body.querySelector('[data-next]').addEventListener('click', () => { step = 3; render(); });
    } else {
      m.body.innerHTML = `
        <p class="event-text">Ten years pass. A child of the Hearthline comes of age beside the family flame.</p>
        <label class="field"><span>Heir's name</span><input id="heir-name" maxlength="24" placeholder="e.g. ${esc(a.name.split(' ')[0])} the Younger" value="${esc(heir.name)}"></label>
        <h4>Their lineage wakes as…</h4>
        <div class="card-row">${Object.entries(LINEAGES).map(([id, l]) => `
          <button class="pick-card ${heir.lineage === id ? 'is-picked' : ''}" data-heirlin="${id}">
            <span class="pick-glyph el-${l.el}">${l.glyph}</span><b>${l.name}</b><small class="gold">${l.passive}</small></button>`).join('')}</div>
        <div class="event-choices"><button class="btn btn-choice" data-done>Take up the ember</button></div>`;
      m.body.querySelectorAll('[data-heirlin]').forEach((b) => b.addEventListener('click', () => {
        heir.lineage = b.dataset.heirlin; heir.name = m.body.querySelector('#heir-name').value; render();
      }));
      m.body.querySelector('[data-done]').addEventListener('click', () => {
        const name = m.body.querySelector('#heir-name').value.trim() || `${a.name.split(' ')[0]} the Younger`;
        m.close();
        A.completeSuccession({ name, lineage: heir.lineage, kit });
        startGame();
      });
    }
  };
  render();
}

function showAscension() {
  const m = openModal({ title: '', wide: true, locked: true });
  const gens = S.meta.generation;
  m.body.innerHTML = `
    <div class="ascend">
      <div class="ascend-sun"></div>
      <h2>THE TENTH DAWN</h2>
      <p class="event-text">After a thousand years of ash, morning.<br><br>
      <b>${esc(S.chr.name)}</b> hangs a new sun in the sky — not a fallen god's ember, but a fire the Hearthline built
      out of ${gens} generation${gens > 1 ? 's' : ''}: ${S.meta.ancestors.length ? S.meta.ancestors.map((x) => esc(x.name)).join(', ') + ', and finally ' : ''}${esc(S.chr.name)}.<br><br>
      Below, in Ashfen Hollow, the shrine keeper banks the family flame — gently, out of respect —
      because for the first time in living memory, nobody needs it for the warmth.</p>
      <div class="event-choices">
        <button class="btn btn-choice" data-keep>Keep walking the world as the Dawnbearer</button>
        <button class="btn btn-ghost" data-title>Return to the title</button>
      </div>
    </div>`;
  m.body.querySelector('[data-keep]').addEventListener('click', () => { m.close(); renderAll(); });
  m.body.querySelector('[data-title]').addEventListener('click', () => { m.close(); showTitle(); });
}

// ---------------- the Chronicle, bound and exported ----------------
function exportChronicle() {
  const m = S.meta;
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>The Hearthline Chronicle</title>
<style>body{background:#0e0c0a;color:#ddd2bf;font-family:Georgia,serif;max-width:640px;margin:0 auto;padding:60px 24px;line-height:1.8}
h1{letter-spacing:.2em;text-align:center;color:#e8e0d4}h2{color:#e2b25a;font-size:14px;letter-spacing:.3em;text-transform:uppercase;text-align:center;margin-top:70px}
h3{text-align:center;font-size:24px;margin:8px 0 30px}.orn{text-align:center;color:#4d4237;letter-spacing:.4em}
.anc{border-left:2px solid #302921;margin:14px 0;padding:8px 16px;color:#94897c}b{color:#e8e0d4}
.fin{text-align:center;color:#55493b;font-style:italic;margin-top:80px}</style></head><body>
<h1>THE HEARTHLINE CHRONICLE</h1>
<p class="orn">✦ 卷 ✦</p>
<p style="text-align:center;color:#94897c">${m.generation} generation${m.generation > 1 ? 's' : ''} under the iron sky</p>
${m.chronicle.map((c) => `<h2>${c.book}</h2><h3>${c.title}</h3><div>${c.text}</div>`).join('')}
<h2>The Honored Dead</h2>
${m.ancestors.map((a) => `<div class="anc"><b>${a.name}</b> — Generation ${a.gen}<br>${a.realm}, died at ${a.age} — ${a.cause}${a.deeds?.length ? `<br><small>${a.deeds.join(' · ')}</small>` : ''}</div>`).join('') || '<p class="anc">None yet. May that stay true a long while.</p>'}
<p class="fin">— what burned, warmed; what warmed, endures —</p>
</body></html>`;
  const blob = new Blob([html], { type: 'text/html' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'hearthline-chronicle.html';
  a.click();
  URL.revokeObjectURL(a.href);
  toastMsg('The Chronicle is bound: hearthline-chronicle.html');
}

// ---------------- settings ----------------
function openSettings() {
  const m = openModal({ title: 'Settings' });
  const render = () => {
    const quiet = localStorage.getItem('emberline:quiet') === '1';
    const still = localStorage.getItem('emberline:still') === '1';
    m.body.innerHTML = `<div class="event-choices">
      <button class="btn btn-choice" data-s="sound"><b>${isMuted() ? '🔇 Sound: off' : '🔊 Sound: on'}</b><small>Synthesized wind, gongs, bells, and small joys.</small></button>
      <button class="btn btn-choice" data-s="quiet"><b>${quiet ? '✓ ' : ''}Quiet minigames: ${quiet ? 'on' : 'off'}</b><small>Every minigame resolves automatically at a fair average. For the contemplative and the busy.</small></button>
      <button class="btn btn-choice" data-s="still"><b>${still ? '✓ ' : ''}Still sky: ${still ? 'on' : 'off'}</b><small>Freeze the starfield and ember drift. (takes effect on reload)</small></button>
    </div>`;
    m.body.querySelectorAll('[data-s]').forEach((b) => b.addEventListener('click', () => {
      const k = b.dataset.s;
      if (k === 'sound') setMuted(!isMuted());
      if (k === 'quiet') localStorage.setItem('emberline:quiet', quiet ? '0' : '1');
      if (k === 'still') localStorage.setItem('emberline:still', still ? '0' : '1');
      render();
    }));
  };
  render();
}

// ---------------- keyboard shortcuts ----------------
function onShortcut(e) {
  if (e.target.matches('input, textarea') || document.querySelector('.modal-backdrop')) return;
  if (!S || $('#screen-game').classList.contains('hidden')) return;
  if (e.key >= '1' && e.key <= '9') {
    const cards = [...document.querySelectorAll('#action-grid .action-card:not(.is-disabled)')];
    const card = cards[Number(e.key) - 1];
    if (card) { e.preventDefault(); card.click(); }
  } else if (e.key === 'm' || e.key === 'M') { e.preventDefault(); openWorldMap(); }
  else if (e.key === 'e' || e.key === 'E') { e.preventDefault(); A.endSeason(); }
}

// ---------------- toasts & wiring ----------------
export function toastMsg(t) {
  const n = el(`<div class="toast">${esc(t)}</div>`);
  $('#toast-root').appendChild(n);
  setTimeout(() => n.classList.add('is-out'), 2600);
  setTimeout(() => n.remove(), 3100);
}

export function wire() {
  $('#btn-new').addEventListener('click', () => { createMode = null; showCreate(); });
  $('#btn-endseason').addEventListener('click', () => A.endSeason());
  $('#btn-menu').addEventListener('click', () => { persist(true); showTitle(); });
  $('#btn-settings').addEventListener('click', () => openSettings());
  document.addEventListener('keydown', onShortcut);
  document.addEventListener('click', (e) => { if (e.target.closest('.action-card, .btn')) A.sfxE('click'); });
  document.addEventListener('emberline:refresh', renderAll);
  document.addEventListener('emberline:toast', (e) => toastMsg(e.detail));
  document.addEventListener('emberline:open', (e) => openPanel(e.detail));
  document.addEventListener('emberline:succession', showSuccession);
  document.addEventListener('emberline:ascended', showAscension);
  document.addEventListener('emberline:story', (e) => showStoryPage(e.detail, false));
  document.addEventListener('emberline:saved', (e) => {
    const dot = $('#save-dot');
    dot.classList.remove('is-local', 'is-flash');
    if (e.detail.where === 'local') dot.classList.add('is-local');
    void dot.offsetWidth;
    dot.classList.add('is-flash');
    dot.title = e.detail.where === 'local' ? 'saved locally (server unreachable)' : 'saved to server';
  });
}

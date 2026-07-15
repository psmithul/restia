// Game state: creation, derived numbers, persistence.
import { REALMS, LINEAGES, ORIGINS, ITEMS, PERKS, SECT_RANKS, SEASONS, STAGE_NAMES, ASPECTS, FLAWS, WOUNDS, VOWS, RIVALS, DAOS } from './data.js';
import { clamp, pick } from './util.js';

export let S = null; // the single live game state

export function setState(s) { S = s; }

export const AP_PER_SEASON = 3;

export function perkLevel(meta, id) { return meta.perks[id] || 0; }

export function newMeta() {
  return { generation: 1, hearthflame: 0, perks: {}, ancestors: [], bossesSlain: [] };
}

export function createCharacter({ name, lineage, origin, meta, kit }) {
  const lin = LINEAGES[lineage];
  const org = ORIGINS[origin];
  const stats = { body: 3, mind: 3, spirit: 3, fate: 3 };
  for (const k of Object.keys(stats)) {
    stats[k] += (lin.stats[k] || 0) + (org.stats[k] || 0);
    stats[k] += ({ body: perkLevel(meta, 'oxblood_frame'), mind: perkLevel(meta, 'lantern_mind'),
      spirit: perkLevel(meta, 'ashborn_veins'), fate: perkLevel(meta, 'red_thread') })[k] || 0;
  }
  const chr = {
    name, lineage, origin,
    realm: perkLevel(meta, 'old_hearth') ? 1 : 0,
    stage: 0, cult: 0,
    stats, age: 16,
    hp: 1, qi: 0, // fixed up below
    stones: org.stones + (kit?.stones || 0),
    inventory: { ...(org.items || {}) },
    equip: { weapon: null, charm: null },
    techs: ['fistform', lin.tech],
    deck: [lin.tech],
    sect: { joined: false, contrib: 0, totalContrib: 0, mission: null },
    flags: {}, injured: 0, grace: 0, deeds: [],
    karma: 0, aspect: null, flaw: null, vows: [], wounds: [], rel: {}, hungry: false,
    insights: [], dao: null, techUses: {}, forgeTier: 0,
  };
  chr.inventory.provisions = (chr.inventory.provisions || 0) + 2;
  if (perkLevel(meta, 'family_name') && meta.ancestors.some((a) => a.sect)) {
    chr.sect.joined = true; chr.sect.contrib = 100; chr.sect.totalContrib = 100;
    chr.flags.family_name_used = true;
  }
  for (const [id, q] of Object.entries(kit?.items || {})) {
    chr.inventory[id] = (chr.inventory[id] || 0) + q;
  }
  return chr;
}

export function newGame({ slot, name, lineage, origin, meta, kit, world, custom }) {
  meta = meta || newMeta();
  const chr = createCharacter({ name, lineage, origin, meta, kit });
  const w = world || { year: 1, season: 0, ap: AP_PER_SEASON, region: 'ashfen' };
  w.ap = AP_PER_SEASON;
  const s = { v: 2, slot, meta, chr, world: w, log: [], ended: false };
  migrate(s);
  // architect (cheats) and fated (difficulty) overrides
  if (custom) {
    if (custom.stats) Object.assign(chr.stats, custom.stats);
    if (custom.stones !== undefined) chr.stones = custom.stones;
    if (custom.realm !== undefined) { chr.realm = custom.realm; chr.stage = 0; }
    if (custom.aspect) chr.aspect = custom.aspect;
    if (custom.flaw !== undefined) chr.flaw = custom.flaw;
    if (custom.provisions !== undefined) chr.inventory.provisions = custom.provisions;
    if (custom.hungry) chr.hungry = true;
    if (custom.wound) chr.wounds.push('cracked_rib');
    if (custom.region) { w.region = custom.region; w.visited = [custom.region]; }
    if (custom.season !== undefined) w.season = custom.season;
    if (custom.family) meta.family = custom.family;
    if (custom.cheated) meta.cheated = true;
    if (custom.difficulty) w.difficulty = custom.difficulty;
  }
  const d = derived(s);
  chr.hp = d.hpMax; chr.qi = d.qiMax;
  return s;
}

// Fill in Living-World fields on old saves (and fresh worlds).
export function migrate(s) {
  const c = s.chr, w = s.world;
  c.karma ??= 0; c.aspect ??= null; c.flaw ??= null;
  c.vows ??= []; c.wounds ??= []; c.rel ??= {}; c.hungry ??= false;
  if (c.inventory.provisions === undefined && s.v !== 2) c.inventory.provisions = 2;
  w.weather ??= null;
  w.prices ??= { herb: 1, pill: 1, artifact: 1, manual: 1, material: 1 };
  w.news ??= [];
  w.medCount ??= 0;
  w.rivals ??= RIVALS.map((r) => ({ id: r.id, realm: Math.min(1, Math.max(0, s.chr.realm)), stage: 0, cult: 0 }));
  s.meta.chronicle ??= [];
  c.insights ??= []; c.dao ??= null; c.techUses ??= {}; c.forgeTier ??= 0;
  s.meta.legacyFlags ??= {};
  s.meta.estate ??= { shrine: 0, garden: 0, library: 0 };
  s.meta.heirBonus ??= { body: 0, mind: 0, spirit: 0, fate: 0, insights: 0 };
  s.meta.ending ??= null;
  s.meta.family ??= [];
  w.visited ??= [w.region];
  w.discovered ??= [];
  w.difficulty ??= 'ash';
  s.v = 2;
  return s;
}

export function derived(s = S) {
  const c = s.chr; const m = s.meta;
  const w = c.equip.weapon ? ITEMS[c.equip.weapon] : null;
  const ch = c.equip.charm ? ITEMS[c.equip.charm] : null;
  const eq = (k) => (w?.[k] || 0) + (ch?.[k] || 0);
  const asp = c.aspect ? ASPECTS[c.aspect].fx : {};
  const flaw = c.flaw ? FLAWS[c.flaw].fx : {};
  const woundFx = (k) => (c.wounds || []).reduce((t, id) => t + (WOUNDS[id]?.fx[k] || 0), 0);
  const body = Math.max(1, c.stats.body + eq('body') + (asp.body || 0) + woundFx('body'));
  const spirit = Math.max(1, c.stats.spirit + eq('spirit') + woundFx('spirit'));
  const fate = Math.max(1, c.stats.fate + eq('fate'));
  const mind = Math.max(1, c.stats.mind + eq('mind') + woundFx('mind'));
  const dao = c.dao ? DAOS[c.dao].fx : {};
  let hpMax = Math.round((30 + body * 6 + c.realm * 24) * (c.lineage === 'charcoal' ? 1.25 : 1));
  const qiMax = 20 + spirit * 8 + c.realm * 36;
  const atk = Math.round(3 + body * 1.5 + c.realm * 4.5 + eq('atk') + (c.forgeTier || 0));
  const crit = 10 + fate * 1.5 + eq('crit') + (c.lineage === 'glass' ? 20 : 0) + (asp.crit || 0) + woundFx('crit') + (dao.crit || 0);
  let meditate = 1 + eq('meditate') + perkLevel(m, 'kiln_lungs') * 0.15;
  if (c.techs.includes('ninefold_breath')) meditate += 0.3;
  if ((c.vows || []).some((v) => v.id === 'silence')) meditate += 0.35;
  if (c.hungry) meditate = Math.max(0.2, meditate - 0.25);
  const priceMult = perkLevel(m, 'ember_frugality') ? 0.85 : 1;
  const lifespan = REALMS[c.realm].lifespan + perkLevel(m, 'long_wick') * 10;
  const insightMult = 1 + (c.insights?.length || 0) * 0.06;
  return { hpMax, qiMax, atk, crit, dodge: 5 + eq('dodge') + (dao.dodge || 0), meditate, priceMult, lifespan,
    body, mind, spirit, fate,
    dmgMult: (c.lineage === 'cinder' ? 1.15 : 1) * (1 + (dao.dmg || 0)),
    qiCostMult: flaw.qiCost || 1, checkBonus: asp.checkBonus || 0,
    insightMult, insightGrace: Math.floor((c.insights?.length || 0) / 3),
    pillMult: 1 + (dao.pill || 0) };
}

export function stageCost(s = S) { return REALMS[s.chr.realm].stageCost; }
export function atPeak(s = S) { return s.chr.stage >= REALMS[s.chr.realm].stages - 1; }
export function realmLabel(c) {
  const r = REALMS[c.realm];
  return r.stages > 1 ? `${r.name} · ${STAGE_NAMES[c.stage]}` : r.name;
}
export function dateLabel(w) { return `Year ${w.year}, ${SEASONS[w.season]}`; }

export function sectRank(c) {
  let r = 0;
  SECT_RANKS.forEach((rk, i) => { if (c.sect.totalContrib >= rk.at) r = i; });
  return r;
}

export function healHp(pct) { const d = derived(); S.chr.hp = clamp(S.chr.hp + Math.round(d.hpMax * pct), 0, d.hpMax); }
export function healQi(pct) { const d = derived(); S.chr.qi = clamp(S.chr.qi + Math.round(d.qiMax * pct), 0, d.qiMax); }

export function addItem(id, q = 1) {
  S.chr.inventory[id] = (S.chr.inventory[id] || 0) + q;
  if (S.chr.inventory[id] <= 0) delete S.chr.inventory[id];
}
export function hasItems(need) {
  return Object.entries(need).every(([id, q]) => (S.chr.inventory[id] || 0) >= q);
}

// ---------- persistence ----------
let saveTimer = null;
export function persist(immediate = false) {
  if (!S || !S.slot) return;
  clearTimeout(saveTimer);
  const run = async () => {
    const payload = {
      summary: { name: S.chr.name, generation: S.meta.generation, realm: realmLabel(S.chr), year: S.world.year },
      state: S,
    };
    try {
      const res = await fetch(`/api/saves/${S.slot}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`save http ${res.status}`);
      document.dispatchEvent(new CustomEvent('emberline:saved', { detail: { where: 'server' } }));
    } catch (e) {
      try { localStorage.setItem(`emberline:${S.slot}`, JSON.stringify(payload)); } catch {}
      document.dispatchEvent(new CustomEvent('emberline:saved', { detail: { where: 'local' } }));
    }
  };
  if (immediate) run(); else saveTimer = setTimeout(run, 400);
}

export async function listSaves() {
  try {
    const res = await fetch('/api/saves');
    if (res.ok) return await res.json();
  } catch {}
  // offline fallback: scan localStorage
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k?.startsWith('emberline:')) {
      try {
        const p = JSON.parse(localStorage.getItem(k));
        out.push({ slot: k.slice(10), ...p.summary, updatedAt: 0 });
      } catch {}
    }
  }
  return out;
}

export async function loadSave(slot) {
  try {
    const res = await fetch(`/api/saves/${slot}`);
    if (res.ok) { const p = await res.json(); return migrate(p.state); }
  } catch {}
  const raw = localStorage.getItem(`emberline:${slot}`);
  return raw ? migrate(JSON.parse(raw).state) : null;
}

export async function deleteSave(slot) {
  try { await fetch(`/api/saves/${slot}`, { method: 'DELETE' }); } catch {}
  localStorage.removeItem(`emberline:${slot}`);
}

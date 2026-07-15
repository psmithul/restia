// Balance simulator: models Emberline's growth math headlessly and prints
// pacing tables. Run:  node emberline/tools/simulate.mjs
// It mirrors the formulas in state.js/actions.js (kept in sync by hand —
// if pacing feels off in play, adjust here first, then port).

const REALMS = [
  { name: 'Mortal', stageCost: 30, stages: 1, lifespan: 60 },
  { name: 'Sparkgathering', stageCost: 70, stages: 4, lifespan: 85 },
  { name: 'Kindling', stageCost: 170, stages: 4, lifespan: 115 },
  { name: 'Emberheart', stageCost: 420, stages: 4, lifespan: 160 },
  { name: 'Blazebound', stageCost: 1400, stages: 4, lifespan: 220 },
  { name: 'Sunforging', stageCost: 3600, stages: 4, lifespan: 320 },
];

function simulate({ label, medPerSeason = 2, qiMult = 1.0, insightsPerYear = 0.8, huntsPerSeason = 1, avgTier = 1 }) {
  let realm = 0, stage = 0, cult = 0, spirit = 3, insights = 0;
  let seasons = 0, age = 16;
  const milestones = [];
  while (realm < 5 && age < 320) {
    seasons++;
    if (seasons % 4 === 0) { age++; insights += insightsPerYear; }
    const insightMult = 1 + Math.min(insights, 14) * 0.06;
    // meditation with diminishing returns [1, .55] for two sits
    const dims = [1, 0.55, 0.3];
    for (let i = 0; i < medPerSeason; i++) {
      cult += (10 + spirit * 2.2) * qiMult * 1.0 * (dims[i] ?? 0.15) * insightMult;
    }
    // combat cultivation
    cult += huntsPerSeason * (4 + avgTier * 7) * insightMult;
    const cost = REALMS[realm].stageCost;
    while (cult >= cost && !(realm === 5)) {
      if (stage >= REALMS[realm].stages - 1) {
        // tribulation: assume success (grace from insights helps)
        cult = 0; realm++; stage = 0; spirit += 1;
        milestones.push({ realm: REALMS[realm]?.name || 'Dawnbearer-ready', age, seasons });
        break;
      } else {
        cult -= cost; stage++; spirit += 1;
      }
    }
  }
  return { label, milestones, endAge: age };
}

const scenarios = [
  simulate({ label: 'Hermit (meditate only, no insights)', medPerSeason: 3, insightsPerYear: 0, huntsPerSeason: 0 }),
  simulate({ label: 'Hermit in good veins (x2 qi)', medPerSeason: 3, qiMult: 2.0, insightsPerYear: 0, huntsPerSeason: 0 }),
  simulate({ label: 'Worldly (2 sits + hunts + ~1 insight/yr)', medPerSeason: 2, qiMult: 1.4, insightsPerYear: 1.0, huntsPerSeason: 1, avgTier: 2 }),
  simulate({ label: 'Adventurer (1 sit, rich life, insights)', medPerSeason: 1, qiMult: 1.8, insightsPerYear: 1.4, huntsPerSeason: 2, avgTier: 3 }),
];

console.log('\nEMBERLINE pacing simulation — age at each realm breakthrough\n');
const realms = ['Sparkgathering', 'Kindling', 'Emberheart', 'Blazebound', 'Sunforging'];
const pad = (s, n) => String(s).padEnd(n);
console.log(pad('scenario', 44) + realms.map((r) => pad(r.slice(0, 10), 12)).join(''));
for (const sc of scenarios) {
  const row = realms.map((r) => {
    const m = sc.milestones.find((x) => x.realm === r);
    return pad(m ? `age ${m.age}` : '—', 12);
  }).join('');
  console.log(pad(sc.label, 44) + row);
}
console.log('\nLifespans: Mortal 60 · Spark 85 · Kindling 115 · Emberheart 160 · Blazebound 220 · Sunforging 320');
console.log('Reading: every scenario should clear Sunforging before its lifespan; the worldly paths should be decisively faster than hermiting.\n');

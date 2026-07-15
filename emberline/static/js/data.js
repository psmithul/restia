// EMBERLINE — world content. Pure data; the engine interprets it.

export const ELEMENTS = ['cinder', 'glass', 'tinder', 'charcoal', 'smoke'];
// Overcoming pentagon: each element overcomes the next (1.3x), is overcome by previous (0.8x)
export const OVERCOMES = { cinder: 'glass', glass: 'tinder', tinder: 'charcoal', charcoal: 'smoke', smoke: 'cinder' };

export const LINEAGES = {
  cinder: {
    name: 'Cinder Lineage', el: 'cinder', glyph: '火',
    tagline: 'Descendants of those who swallowed sparks of the falling suns.',
    passive: 'Flareheart — techniques deal +15% damage.',
    stats: { body: 0, mind: 0, spirit: 2, fate: 0 },
    tech: 'emberpalm',
  },
  smoke: {
    name: 'Smoke Lineage', el: 'smoke', glyph: '煙',
    tagline: 'Born of the ash-fog that hid the fleeing tribes.',
    passive: 'Veilstep — dodge the first blow of every battle; fleeing always succeeds.',
    stats: { body: 0, mind: 0, spirit: 0, fate: 2 },
    tech: 'ashveil',
  },
  charcoal: {
    name: 'Charcoal Lineage', el: 'charcoal', glyph: '炭',
    tagline: 'Slow-burning folk, tempered like wood turned to coal.',
    passive: 'Slowburn — +25% max health; tribulation lightning falls slower for you.',
    stats: { body: 2, mind: 0, spirit: 0, fate: 0 },
    tech: 'cindershield',
  },
  glass: {
    name: 'Glass Lineage', el: 'glass', glyph: '琉',
    tagline: 'Children of the fused sands where the Fifth Sun struck.',
    passive: 'Edgelight — +20% critical chance and a wider perfect-strike window.',
    stats: { body: 1, mind: 1, spirit: 0, fate: 0 },
    tech: 'glassdraw',
  },
  tinder: {
    name: 'Tinder Lineage', el: 'tinder', glyph: '薪',
    tagline: 'Green-thumbed keepers of the last living groves.',
    passive: 'Greenflame — better pill grades, and one extra herb from every foraging trip.',
    stats: { body: 0, mind: 2, spirit: 0, fate: 0 },
    tech: 'thornspark',
  },
};

export const ORIGINS = {
  orphan: {
    name: 'Orphan of Ashfen', desc: 'Raised by the whole village and by no one. You learned to read luck like weather.',
    stats: { fate: 1 }, stones: 30, items: {},
  },
  noble: {
    name: 'Fallen Noble', desc: 'Your house burned with the old capital. Its last coin purse survived.',
    stats: { mind: 1 }, stones: 260, items: {},
  },
  smith: {
    name: "Smith's Child", desc: 'You grew up at the bellows. The forge left iron in your arms and a parting gift.',
    stats: { body: 1 }, stones: 60, items: { ironbrand_knife: 1 },
  },
  foundling: {
    name: 'Temple Foundling', desc: 'The Hollow Temple monks taught you letters, silence, and where the medicine drawer was.',
    stats: { mind: 1 }, stones: 40, items: { duskclear_pill: 2, emberdew_moss: 2 },
  },
  hearthborn: {
    name: 'Child of the Hearth', desc: 'Born beneath the family flame, heir to everything your line clawed from the ash.',
    stats: {}, stones: 20, items: {},
  },
};

export const REALMS = [
  { name: 'Mortal', title: 'an unawakened mortal', stages: 1, stageCost: 30, lifespan: 60,
    desc: 'Ash in your lungs, dreams of warmth. The ember veins are silent to you.' },
  { name: 'Sparkgathering', title: 'a Sparkgatherer', stages: 4, stageCost: 70, lifespan: 85,
    desc: 'You can feel Emberlight now — motes of the fallen suns drifting in stone and sap.' },
  { name: 'Kindling', title: 'a Kindling cultivator', stages: 4, stageCost: 170, lifespan: 115,
    desc: 'A true flame lives behind your sternum. Cold no longer touches you.' },
  { name: 'Emberheart', title: 'an Emberheart', stages: 4, stageCost: 420, lifespan: 160,
    desc: 'Your heart has become a coal that will not die. Beasts lower their eyes first.' },
  { name: 'Blazebound', title: 'one of the Blazebound', stages: 4, stageCost: 1400, lifespan: 220,
    desc: 'Light bleeds from your old scars. Cities know your name before you arrive.' },
  { name: 'Sunforging', title: 'a Sunforger', stages: 4, stageCost: 3600, lifespan: 320,
    desc: 'You are smelting a sun of your own. The sky watches you with one narrowed eye.' },
  { name: 'Dawnbearer', title: 'the Dawnbearer', stages: 1, stageCost: 0, lifespan: 9999,
    desc: 'The Tenth Dawn. Your light is your own, and it will not fall.' },
];

export const STAGE_NAMES = ['Early', 'Middle', 'Late', 'Peak'];

// kind: strike | guard | utility | heal ; fx interpreted by combat engine
export const TECHNIQUES = {
  fistform:    { name: 'Fistform', el: null, kind: 'strike', qi: 0, pow: 1.0, cd: 0,
    desc: 'Plain knuckles and intent. Costs nothing.', fx: {} },
  emberpalm:   { name: 'Emberpalm', el: 'cinder', kind: 'strike', qi: 6, pow: 1.5, cd: 0,
    desc: 'A palm that leaves a glowing handprint.', fx: {} },
  ashveil:     { name: 'Ashveil', el: 'smoke', kind: 'guard', qi: 7, pow: 0, cd: 2,
    desc: 'Become gray haze. Dodge the next blow entirely, then counter for light damage.', fx: { dodge: 1, counter: 0.6 } },
  cindershield:{ name: 'Cindershield', el: 'charcoal', kind: 'guard', qi: 8, pow: 0, cd: 2,
    desc: 'A slab of compacted char absorbs harm equal to thrice your Body.', fx: { shieldBody: 3 } },
  glassdraw:   { name: 'Glassdraw', el: 'glass', kind: 'strike', qi: 7, pow: 1.3, cd: 0,
    desc: 'A drawing cut of crystallized light. Critical strikes come easily.', fx: { critBonus: 25 } },
  thornspark:  { name: 'Thornspark', el: 'tinder', kind: 'strike', qi: 7, pow: 0.8, cd: 0,
    desc: 'Burrowing seed-embers gnaw at the foe for three turns.', fx: { dot: { dmg: 0.5, turns: 3 } } },
  smokebind:   { name: 'Smokebind', el: 'smoke', kind: 'utility', qi: 12, pow: 0, cd: 3, req: { realm: 1 },
    desc: 'Ropes of solid smoke. The enemy loses its next turn.', fx: { stun: 1 } },
  kindled_step:{ name: 'Kindled Step', el: 'cinder', kind: 'utility', qi: 9, pow: 0, cd: 3, req: { realm: 1 },
    desc: 'Burn distance itself. Dodge the next blow and strike back hard.', fx: { dodge: 1, counter: 1.0 } },
  sunspoke_lance:{ name: 'Sunspoke Lance', el: 'cinder', kind: 'strike', qi: 16, pow: 2.3, cd: 1, req: { realm: 2 },
    desc: 'A spear of dawnlight hurled from the shoulder of memory.', fx: {} },
  glass_rain:  { name: 'Glass Rain', el: 'glass', kind: 'strike', qi: 15, pow: 0.75, cd: 1, req: { realm: 2 },
    desc: 'Three arcs of razored light fall as one.', fx: { hits: 3 } },
  charwall:    { name: 'Charwall', el: 'charcoal', kind: 'guard', qi: 14, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'A rampart of dead forests. Absorbs harm equal to six times your Body.', fx: { shieldBody: 6 } },
  verdant_surge:{ name: 'Verdant Surge', el: 'tinder', kind: 'heal', qi: 15, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'Green fire knits flesh: recover a third of your health.', fx: { healPct: 0.34 } },
  ninefold_breath:{ name: 'Ninefold Breath', el: null, kind: 'utility', qi: 0, pow: 0, cd: 0, req: { realm: 1 },
    desc: 'The beggar-immortal’s breathing scripture. Meditation yields +30% Emberlight. (Passive)', fx: { passiveMeditate: 0.3 } },
  boneforge:   { name: 'Boneforge', el: 'charcoal', kind: 'utility', qi: 13, pow: 0, cd: 4, req: { realm: 2 },
    desc: 'Heat your marrow to forging temperature: +50% damage for three turns.', fx: { buff: { mult: 1.5, turns: 3 } } },
  duskmantle:  { name: 'Duskmantle', el: 'smoke', kind: 'guard', qi: 13, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'Wrap yourself in evening. The next two blows are halved.', fx: { halve: 2 } },
  emberstorm:  { name: 'Emberstorm', el: 'cinder', kind: 'strike', qi: 26, pow: 3.1, cd: 2, req: { realm: 3 },
    desc: 'The field becomes a furnace. Everything burns.', fx: { dot: { dmg: 0.4, turns: 2 } } },
  hollow_sight:{ name: 'Hollow Sight', el: null, kind: 'utility', qi: 10, pow: 0, cd: 4, req: { realm: 3 },
    desc: 'See the empty spaces in all things: +30% crit for the rest of the fight.', fx: { critAll: 30 } },
  dawnlance:   { name: 'Dawnlance', el: 'cinder', kind: 'strike', qi: 40, pow: 4.4, cd: 2, req: { realm: 4 },
    desc: 'A fragment of true sunrise, thrown like a javelin.', fx: {} },
  tenthfire_hymn:{ name: 'Tenthfire Hymn', el: 'cinder', kind: 'strike', qi: 60, pow: 5.5, cd: 3, req: { realm: 5 },
    desc: 'The forbidden verse that calls a new sun. Needed to kindle the Tenth Dawn.', fx: { dot: { dmg: 0.6, turns: 3 } } },
  tidebreak:    { name: 'Tidebreak', el: 'smoke', kind: 'strike', qi: 30, pow: 2.8, cd: 1, req: { realm: 3 },
    desc: 'The drowned court’s stroke: the weight of forty fathoms, delivered sideways.', fx: { stun: 1 } },
  verse_of_edges:{ name: 'Verse of Edges', el: 'glass', kind: 'strike', qi: 14, pow: 1.6, cd: 0, req: { realm: 2 },
    desc: 'The Sword Dao’s first line: everything has a seam.', fx: { critBonus: 20, poise: 2 } },
  kiln_heart_breath:{ name: 'Kiln-Heart Breath', el: 'cinder', kind: 'heal', qi: 12, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'The Furnace Dao tempers flesh like ore: recover a quarter of your health and burn off one lingering wound over time.', fx: { healPct: 0.25 } },
  turning_palm: { name: 'Turning Palm', el: null, kind: 'strike', qi: 13, pow: 1.2, cd: 2, req: { realm: 2 },
    desc: 'The Wheel Dao returns what was given: strikes harder the lower your health.', fx: { desperation: true } },
  emberkeeper_ward:{ name: 'Emberkeeper’s Ward', el: 'charcoal', kind: 'guard', qi: 12, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'The Hearth Dao shelters: a ward of five times your Body, warm as a kept flame.', fx: { shieldBody: 5 } },
  hollow_step:  { name: 'Hollow Step', el: 'smoke', kind: 'utility', qi: 10, pow: 0, cd: 3, req: { realm: 2 },
    desc: 'The Void Dao’s address: be absent when it matters, and answer from behind.', fx: { dodge: 1, counter: 1.2 } },
  asura_roar:   { name: 'Asura’s Roar', el: 'charcoal', kind: 'strike', qi: 38, pow: 3.6, cd: 2, req: { realm: 4 },
    desc: 'A war-shout with mass. The asura host used it to answer falling suns.', fx: {} },
};

export const PILL_GRADES = [
  { name: 'Cracked', mult: 0.6 }, { name: 'Standard', mult: 1.0 },
  { name: 'Refined', mult: 1.5 }, { name: 'Immaculate', mult: 2.2 },
];

// kind: herb | pill | material | artifact | manual | key
export const ITEMS = {
  emberdew_moss: { name: 'Emberdew Moss', kind: 'herb', price: 12, desc: 'Moss that drinks vein-light and weeps warm dew.' },
  graypine_sap:  { name: 'Graypine Sap', kind: 'herb', price: 8, desc: 'Bitter amber sap; binds other essences together.' },
  duskpetal:     { name: 'Duskpetal', kind: 'herb', price: 15, desc: 'Blooms only in the hour the suns used to set.' },
  glassreed:     { name: 'Glassreed', kind: 'herb', price: 20, desc: 'A translucent reed that rings faintly in wind.' },
  marrowroot:    { name: 'Marrowroot', kind: 'herb', price: 24, desc: 'A root shaped uncomfortably like a femur.' },
  suncap:        { name: 'Suncap Mushroom', kind: 'herb', price: 70, desc: 'Grows one cap per year over buried ember shards. Rare.' },
  veinbloom:     { name: 'Veinbloom', kind: 'herb', price: 90, desc: 'A metal flower that grows only inside ember veins.' },
  dawnpetal:     { name: 'Dawnpetal', kind: 'herb', price: 600, desc: 'Petal of a flower that remembers the sky before the fall. Vanishingly rare.' },
  emberdew_pill: { name: 'Emberdew Pill', kind: 'pill', price: 45, use: { qiPct: 0.5 }, desc: 'Restores half your Emberlight.' },
  duskclear_pill:{ name: 'Duskclear Pill', kind: 'pill', price: 40, use: { hpPct: 0.45 }, desc: 'Cools wounds shut. Restores health.' },
  marrowfire_pill:{ name: 'Marrowfire Pill', kind: 'pill', price: 220, use: { stat: 'body' }, desc: 'Permanently hardens the body. +1 Body.' },
  clearmind_pill:{ name: 'Clearmind Pill', kind: 'pill', price: 220, use: { stat: 'mind' }, desc: 'Burns fog from thought. +1 Mind.' },
  stoneveil_pill:{ name: 'Stoneveil Pill', kind: 'pill', price: 120, use: { grace: 1 }, desc: 'Skin like slate for one tribulation: endure one extra bolt.' },
  cindergrit_pill:{ name: 'Cindergrit Pill', kind: 'pill', price: 90, use: { cult: 40 }, desc: 'Compressed cultivation. Crude, effective, frowned upon.' },
  dawnpetal_elixir:{ name: 'Dawnpetal Elixir', kind: 'pill', price: 1500, use: { grace: 2, slow: true }, desc: 'The breakthrough elixir of legends: lightning falls slower, and you endure two extra bolts.' },
  ironbrand_knife:{ name: 'Ironbrand Knife', kind: 'artifact', slot: 'weapon', price: 80, atk: 2, desc: 'A smith’s parting gift. Honest iron.' },
  graypine_staff:{ name: 'Graypine Staff', kind: 'artifact', slot: 'weapon', price: 160, atk: 1, spirit: 1, desc: 'Cut from a tree that survived two sunfalls.' },
  emberglass_saber:{ name: 'Emberglass Saber', kind: 'artifact', slot: 'weapon', price: 620, atk: 5, crit: 8, desc: 'Fused sand from the Fifth Sun’s grave, ground to an edge.' },
  kilnheart_gauntlet:{ name: 'Kilnheart Gauntlet', kind: 'artifact', slot: 'weapon', price: 1400, atk: 8, body: 1, desc: 'Still warm from a forge that no longer exists.' },
  dawnshard_blade:{ name: 'Dawnshard Blade', kind: 'artifact', slot: 'weapon', price: 4000, atk: 14, crit: 10, desc: 'A splinter of the Ninth Sun’s heart, sheathed in oaths.' },
  sootcloak:     { name: 'Sootcloak', kind: 'artifact', slot: 'charm', price: 240, dodge: 10, desc: 'A cloak that forgets it was ever seen.' },
  veinstone_pendant:{ name: 'Veinstone Pendant', kind: 'artifact', slot: 'charm', price: 420, meditate: 0.2, desc: 'A chip of living ember vein. Hums during meditation: +20% Emberlight gained.' },
  hearth_ring:   { name: 'Hearth Ring', kind: 'artifact', slot: 'charm', price: 800, fate: 2, desc: 'Every generation of your line has worn it. It remembers them all.' },
  ashwraith_lantern:{ name: 'Ashwraith Lantern', kind: 'artifact', slot: 'charm', price: 2200, crit: 12, spirit: 2, desc: 'Something lives in it. It pays rent in luck.' },
  manual_smokebind:{ name: 'Manual: Smokebind', kind: 'manual', price: 300, tech: 'smokebind', desc: 'Rope-smoke binding arts, third revision.' },
  manual_sunspoke:{ name: 'Manual: Sunspoke Lance', kind: 'manual', price: 700, tech: 'sunspoke_lance', desc: 'Spear scripture of the Kindled Path.' },
  manual_glass_rain:{ name: 'Manual: Glass Rain', kind: 'manual', price: 700, tech: 'glass_rain', desc: 'Triple-arc sword diagrams etched on slides of glass.' },
  manual_charwall:{ name: 'Manual: Charwall', kind: 'manual', price: 650, tech: 'charwall', desc: 'Defensive earthworks for the body.' },
  manual_verdant:{ name: 'Manual: Verdant Surge', kind: 'manual', price: 750, tech: 'verdant_surge', desc: 'Green-fire mending, transcribed by temple healers.' },
  manual_boneforge:{ name: 'Manual: Boneforge', kind: 'manual', price: 800, tech: 'boneforge', desc: 'Marrow-tempering methods. Margin notes warn of the smell.' },
  manual_duskmantle:{ name: 'Manual: Duskmantle', kind: 'manual', price: 800, tech: 'duskmantle', desc: 'Evening-cloth defensive arts.' },
  manual_emberstorm:{ name: 'Manual: Emberstorm', kind: 'manual', price: 2000, tech: 'emberstorm', desc: 'Furnace-field arts. The pages are scorched.' },
  manual_hollow_sight:{ name: 'Manual: Hollow Sight', kind: 'manual', price: 1600, tech: 'hollow_sight', desc: 'See what isn’t. Recovered from the Hollow Temple.' },
  manual_dawnlance:{ name: 'Manual: Dawnlance', kind: 'manual', price: 5000, tech: 'dawnlance', desc: 'Written in ink that glows at dawn.' },
  vein_ore:      { name: 'Vein Ore', kind: 'material', price: 30, desc: 'Ember-veined stone. Smiths and sects pay well for it.' },
  beast_core:    { name: 'Beast Core', kind: 'material', price: 55, desc: 'The hard knot of Emberlight inside a spirit beast.' },
  glasswing:     { name: 'Glasswing', kind: 'material', price: 40, desc: 'A mantis wing like a pane of smoked glass.' },
  hymn_fragment: { name: 'Hymn Fragment', kind: 'key', price: 0, desc: 'A verse of the Tenthfire Hymn, humming with heat. Three complete it.' },
  provisions:    { name: 'Provisions', kind: 'food', price: 12, desc: 'A season’s worth of grain, dried meat, and pickles that could survive a tribulation.' },
  stormbud:      { name: 'Stormbud', kind: 'herb', price: 45, desc: 'A flower that blooms only in thunder. Handle with dry gloves and low expectations.' },
  lotus_heart:   { name: 'White Lotus Heart', kind: 'herb', price: 60, desc: 'The cool center of a century lotus. Tastes like forgiven debts.' },
  brinebloom:    { name: 'Brinebloom', kind: 'herb', price: 80, desc: 'Grows on drowned masts, fed by sunken light. Weeps saltwater at dawn.' },
  star_iron:     { name: 'Star-Iron', kind: 'material', price: 120, desc: 'Metal from the drowned sun’s mantle. Cold to the eye, warm to the palm.' },
  stormheart_pill:{ name: 'Stormheart Pill', kind: 'pill', price: 260, use: { stat: 'spirit' }, desc: 'Bottled thunder for the meridians. +1 Spirit, permanently.' },
  deepwater_pill:{ name: 'Deepwater Pill', kind: 'pill', price: 150, use: { hpPct: 0.5, qiPct: 0.5 }, desc: 'The patience of the deep sea, swallowed. Restores half of everything.' },
  tidebreaker_harpoon:{ name: 'Tidebreaker Harpoon', kind: 'artifact', slot: 'weapon', price: 2600, atk: 10, crit: 6, desc: 'Forged for a war against the sea. The sea lost, narrowly.' },
  star_iron_blade:{ name: 'Star-Iron Blade', kind: 'artifact', slot: 'weapon', price: 3200, atk: 12, spirit: 1, desc: 'A sword quenched in drowned sunlight. It hums at dawn and dusk.' },
  khan_standard: { name: 'The Khan’s Lesser Standard', kind: 'artifact', slot: 'charm', price: 3000, body: 2, fate: 1, desc: 'A banner-scrap of the Banner That Does Not Cool. Carrying it is a rank.' },
  manual_tidebreak:{ name: 'Manual: Tidebreak', kind: 'manual', price: 2400, tech: 'tidebreak', desc: 'The drowned court’s killing stroke, transcribed on sharkskin.' },
  manual_asura_roar:{ name: 'Manual: Asura’s Roar', kind: 'manual', price: 2400, tech: 'asura_roar', desc: 'Four-armed forms adapted, grudgingly, for the two-armed.' },
};

export const RECIPES = [
  { id: 'emberdew_pill', needs: { emberdew_moss: 2, graypine_sap: 1 }, minMind: 2, diff: 0 },
  { id: 'duskclear_pill', needs: { duskpetal: 2, graypine_sap: 1 }, minMind: 2, diff: 0 },
  { id: 'cindergrit_pill', needs: { emberdew_moss: 1, marrowroot: 1 }, minMind: 3, diff: 1 },
  { id: 'stoneveil_pill', needs: { glassreed: 1, marrowroot: 1, graypine_sap: 1 }, minMind: 4, diff: 1 },
  { id: 'marrowfire_pill', needs: { marrowroot: 2, suncap: 1 }, minMind: 5, diff: 2 },
  { id: 'clearmind_pill', needs: { glassreed: 2, duskpetal: 1 }, minMind: 5, diff: 2 },
  { id: 'dawnpetal_elixir', needs: { dawnpetal: 1, suncap: 1, emberdew_moss: 2 }, minMind: 7, diff: 3 },
  { id: 'stormheart_pill', needs: { stormbud: 2, suncap: 1 }, minMind: 6, diff: 2 },
  { id: 'deepwater_pill', needs: { brinebloom: 1, lotus_heart: 1 }, minMind: 5, diff: 1 },
];

export const ENEMIES = {
  ashen_hare:   { name: 'Ashen Hare', tier: 0, el: 'smoke', hp: 18, atk: 4, stones: [4, 10], desc: 'Fast, gray, and inexplicably furious.',
    loot: { graypine_sap: 0.5 }, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'dodgeup', w: 1 } ] },
  sootback_boar:{ name: 'Sootback Boar', tier: 0, el: 'charcoal', hp: 30, atk: 5, stones: [8, 16], desc: 'A boar that rolled in a dead campfire and made it a lifestyle.',
    loot: { marrowroot: 0.35 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 1, mult: 1.8 } ] },
  graypine_wolf:{ name: 'Graypine Wolf', tier: 1, el: 'smoke', hp: 46, atk: 8, stones: [14, 30], desc: 'Its howl sounds like wind through dead branches.',
    loot: { beast_core: 0.3, graypine_sap: 0.4 }, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'heavy', w: 1, mult: 1.7 } ] },
  vein_bandit:  { name: 'Vein Bandit', tier: 1, el: 'glass', hp: 52, atk: 9, stones: [30, 60], desc: 'Cast out of three sects, welcome in none, armed anyway.',
    loot: { emberdew_pill: 0.25 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'guard', w: 1 }, { t: 'heavy', w: 1, mult: 1.6 } ] },
  glasswing_mantis:{ name: 'Glasswing Mantis', tier: 1, el: 'glass', hp: 40, atk: 11, stones: [18, 36], desc: 'You hear it refract before you see it strike.',
    loot: { glasswing: 0.6 }, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'heavy', w: 2, mult: 1.5 } ] },
  hollow_acolyte:{ name: 'Hollow Acolyte', tier: 2, el: 'smoke', hp: 80, atk: 13, stones: [40, 80], desc: 'A monk whose insides were traded for silence.',
    loot: { duskclear_pill: 0.3, beast_core: 0.3 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'drain', w: 2 }, { t: 'heavy', w: 1, mult: 1.8 } ] },
  cinderfang_alpha:{ name: 'Cinderfang Alpha', tier: 2, el: 'cinder', hp: 95, atk: 15, stones: [50, 100], desc: 'The pack follows the one whose bite cauterizes.',
    loot: { beast_core: 0.7 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 1.7 }, { t: 'dot', w: 1 } ] },
  glasswaste_stalker:{ name: 'Glasswaste Stalker', tier: 2, el: 'glass', hp: 85, atk: 17, stones: [55, 110], desc: 'Six legs, no shadow, patience of sand.',
    loot: { glasswing: 0.5, beast_core: 0.4 }, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'heavy', w: 2, mult: 1.9 } ] },
  molten_serpent:{ name: 'Molten Serpent', tier: 3, el: 'cinder', hp: 160, atk: 22, stones: [90, 180], desc: 'A river of slag that decided to have opinions.',
    loot: { beast_core: 0.8, suncap: 0.3 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'dot', w: 2 }, { t: 'heavy', w: 2, mult: 2.0 } ] },
  storm_eyed_roc:{ name: 'Storm-Eyed Roc', tier: 3, el: 'smoke', hp: 150, atk: 25, stones: [100, 200], desc: 'Weather happens where it chooses to look.',
    loot: { beast_core: 0.8, veinbloom: 0.3 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'dodgeup', w: 1 }, { t: 'heavy', w: 2, mult: 2.1 } ] },
  hollow_abbot: { name: 'The Hollow Abbot', tier: 3, el: 'smoke', hp: 260, atk: 26, stones: [400, 600], boss: true, deadly: true,
    desc: 'What remains when a holy man gives everything away, including the giving.',
    loot: { manual_hollow_sight: 1.0, suncap: 1.0 }, intents: [ { t: 'drain', w: 2 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.2 }, { t: 'stun', w: 1 } ] },
  ashwraith_lord:{ name: 'Ashwraith Lord', tier: 4, el: 'smoke', hp: 340, atk: 34, stones: [250, 450], desc: 'A cultivator who failed the Blazebound tribulation and kept going anyway.',
    loot: { ashwraith_lantern: 0.25, beast_core: 1.0 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'drain', w: 2 }, { t: 'heavy', w: 2, mult: 2.0 } ] },
  ember_guardian:{ name: 'Ember Guardian', tier: 4, el: 'charcoal', hp: 480, atk: 38, stones: [800, 1200], boss: true, deadly: true,
    desc: 'The Ninth Crater’s sleepless sentinel: armor of slag, heart of grief.',
    loot: { hymn_fragment: 1.0, kilnheart_gauntlet: 0.5 }, intents: [ { t: 'guard', w: 1 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.3 }, { t: 'stun', w: 1 } ] },
  crater_choir:  { name: 'The Crater Choir', tier: 5, el: 'cinder', hp: 520, atk: 44, stones: [900, 1400], boss: true, deadly: true,
    desc: 'Nine voices singing the memory of nine suns. They do not want a tenth.',
    loot: { hymn_fragment: 1.0, suncap: 1.0 }, intents: [ { t: 'dot', w: 2 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.4 } ] },
  warden_ninth: { name: 'Warden of the Ninth', tier: 5, el: 'cinder', hp: 700, atk: 52, stones: [2000, 3000], boss: true, deadly: true,
    desc: 'The archer’s last arrow, given armor and a grudge. It guards the grave of the sky.',
    loot: { hymn_fragment: 1.0, dawnshard_blade: 1.0 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.5 }, { t: 'stun', w: 1 }, { t: 'dot', w: 1 } ] },
  yan_shuo_1:   { name: 'Yan Shuo, Sect Prodigy', tier: 1, el: 'glass', hp: 60, atk: 10, stones: [0, 0], rival: true,
    desc: 'Your rival. Better funded, better dressed, worse tempered.',
    loot: {}, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 1.6 }, { t: 'guard', w: 1 } ] },
  yan_shuo_2:   { name: 'Yan Shuo, Inner Blade', tier: 3, el: 'glass', hp: 190, atk: 26, stones: [0, 0], rival: true,
    desc: 'He has been waiting for this rematch. He made a list of your weaknesses.',
    loot: {}, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.0 }, { t: 'guard', w: 1 }, { t: 'dot', w: 1 } ] },
  yan_shuo_3:   { name: 'Yan Shuo, Glass Saint', tier: 5, el: 'glass', hp: 560, atk: 46, stones: [0, 0], rival: true, deadly: true,
    desc: 'At the end of every path you walked, he was walking too.',
    loot: { manual_dawnlance: 1.0 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.3 }, { t: 'stun', w: 1 }, { t: 'guard', w: 1 } ] },
  spine_drake:  { name: 'Spine Drake', tier: 3, el: 'glass', hp: 175, atk: 24, stones: [110, 190], desc: 'A ridge that opened one eye. Storm-light runs under its scales like thought.',
    loot: { beast_core: 0.9, stormbud: 0.5 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.0 }, { t: 'dodgeup', w: 1 } ] },
  cliff_ape:    { name: 'Grey Cliff Ape', tier: 3, el: 'charcoal', hp: 150, atk: 23, stones: [90, 160], desc: 'Broad as a gate, patient as gravity. Throws boulders recreationally.',
    loot: { beast_core: 0.6, marrowroot: 0.5 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 1.9 }, { t: 'guard', w: 1 } ] },
  bone_husk:    { name: 'Orchard Husk', tier: 3, el: 'smoke', hp: 145, atk: 22, stones: [80, 150], desc: 'A soldier of a war that ended before your dynasty began. It has not been told.',
    loot: { beast_core: 0.5, duskclear_pill: 0.3 }, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'drain', w: 1 }, { t: 'heavy', w: 1, mult: 1.8 } ] },
  grave_eel:    { name: 'Grave Eel', tier: 4, el: 'smoke', hp: 290, atk: 33, stones: [160, 280], desc: 'Long as a barge, born in a leviathan’s ribcage, homesick for the abyss.',
    loot: { beast_core: 1.0, brinebloom: 0.5 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.1 }, { t: 'dot', w: 1 } ] },
  salt_wraith:  { name: 'Salt Wraith', tier: 4, el: 'smoke', hp: 310, atk: 36, stones: [180, 300], desc: 'What the sea keeps of drowned cultivators: the thirst, the grudge, the technique.',
    loot: { beast_core: 0.8, star_iron: 0.4 }, intents: [ { t: 'drain', w: 2 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.0 }, { t: 'stun', w: 1 } ] },
  horde_champion:{ name: 'Horde Champion', tier: 4, el: 'charcoal', hp: 360, atk: 38, stones: [200, 340], desc: 'Asura-blooded, arena-forged, polite in the way of people who have nothing to prove and prove it anyway.',
    loot: { marrowfire_pill: 0.3, star_iron: 0.4 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.2 }, { t: 'guard', w: 1 } ] },
  asura_remnant:{ name: 'Asura Remnant', tier: 5, el: 'cinder', hp: 500, atk: 46, stones: [300, 500], desc: 'Four arms, one purpose, eight hundred years past its discharge papers.',
    loot: { beast_core: 1.0, star_iron: 0.7 }, intents: [ { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.3 }, { t: 'dot', w: 1 }, { t: 'guard', w: 1 } ] },
  sunspawn:     { name: 'Sunspawn', tier: 5, el: 'cinder', hp: 440, atk: 44, stones: [280, 460], desc: 'A droplet of the fallen Eighth Sun that grew a will and a temperature.',
    loot: { suncap: 0.8, veinbloom: 0.6 }, intents: [ { t: 'dot', w: 2 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 2, mult: 2.2 } ] },
  tide_king:    { name: 'The Tide-Hollowed King', tier: 5, el: 'smoke', hp: 640, atk: 48, stones: [1200, 1800], boss: true, deadly: true,
    desc: 'A drowned emperor holding court in the largest ribcage on the coast. The tide comes and goes at his pleasure, mostly out of pity.',
    loot: { tidebreaker_harpoon: 1.0, manual_tidebreak: 1.0 }, intents: [ { t: 'drain', w: 2 }, { t: 'hit', w: 2, mult: 1 }, { t: 'heavy', w: 3, mult: 2.4 }, { t: 'stun', w: 1 } ] },
  sect_aspirant:{ name: 'Sect Trial Aspirant', tier: 1, el: 'charcoal', hp: 44, atk: 8, stones: [0, 0],
    desc: 'Another hopeful at the sect gates. Only one of you passes today.',
    loot: {}, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'guard', w: 1 }, { t: 'heavy', w: 1, mult: 1.5 } ] },
  spar_partner: { name: 'Sparring Senior', tier: 2, el: 'charcoal', hp: 90, atk: 12, spar: true, stones: [0, 0],
    desc: 'A senior disciple with wooden weapons and no mercy.',
    loot: {}, intents: [ { t: 'hit', w: 3, mult: 1 }, { t: 'heavy', w: 1, mult: 1.5 }, { t: 'guard', w: 1 } ] },
};

export const REGIONS = [
  { id: 'ashfen', name: 'Ashfen Hollow', minRealm: 0, qiMult: 0.6, domain: 'vale', map: { x: 150, y: 300 },
    desc: 'Your village: forty roofs huddled around the Hearth Shrine, under a sky the color of old iron.',
    flavor: ['Smoke rises from supper fires. Somewhere a dog argues with a goose.',
      'The shrine keeper sweeps ash from the steps, as her mother did, as hers did.',
      'Children play "tribulation" in the square, throwing pebbles as lightning.'],
    actions: ['meditate', 'rest', 'market', 'jobs', 'talk', 'wander', 'alchemy', 'forge', 'raise', 'shrine', 'austerities'],
    market: ['provisions', 'graypine_sap', 'emberdew_moss', 'duskclear_pill', 'ironbrand_knife', 'manual_smokebind'],
    enemies: ['ashen_hare'], herbs: ['graypine_sap', 'emberdew_moss', 'duskpetal'] },
  { id: 'greypine', name: 'Greypine Wilds', minRealm: 0, qiMult: 1.0, domain: 'vale', map: { x: 245, y: 225 },
    desc: 'A forest of ash-silver pines. The deeper you go, the warmer the ground.',
    flavor: ['Needles fall soundlessly. Something with too many eyes watches, decides against it.',
      'You cross a stream running warm — an ember vein passes beneath.',
      'Hunters’ marks on the bark: three cuts, meaning "turn back after dark."'],
    actions: ['meditate', 'forage', 'hunt', 'wander'],
    enemies: ['ashen_hare', 'sootback_boar', 'graypine_wolf'],
    herbs: ['graypine_sap', 'emberdew_moss', 'duskpetal', 'marrowroot'] },
  { id: 'mines', name: 'Ember Vein Mines', minRealm: 1, qiMult: 1.7, domain: 'vale', map: { x: 165, y: 185 },
    desc: 'Tunnels chasing rivers of buried sunlight. Miners nod at cultivators, warily.',
    flavor: ['The walls pulse faintly, like a sleeping animal.',
      'A foreman sells maps of "safe" tunnels. The quotation marks are audible.',
      'Deep below, something knocks four times. Miners knock back three. Never four.'],
    actions: ['meditate', 'mine', 'hunt', 'wander'],
    enemies: ['vein_bandit', 'glasswing_mantis', 'graypine_wolf'],
    herbs: ['veinbloom', 'glassreed', 'marrowroot'] },
  { id: 'cinder_market', name: 'The Cinder Market', minRealm: 1, qiMult: 0.8, domain: 'vale', map: { x: 285, y: 330 },
    desc: 'A city built in the shell of a fallen sun-fragment. Everything is for sale, including directions.',
    flavor: ['An auctioneer sells a "genuine dawn feather." It is a chicken feather painted gold.',
      'Pill smoke, spice smoke, incense smoke, actual smoke: the four winds of the Market.',
      'A fortune teller grabs your wrist, pales, and refunds you double.'],
    actions: ['market', 'listings', 'talk', 'wander', 'alchemy', 'rest'],
    market: ['provisions', 'emberdew_moss', 'duskpetal', 'glassreed', 'marrowroot', 'suncap', 'emberdew_pill', 'duskclear_pill', 'stoneveil_pill', 'cindergrit_pill', 'graypine_staff', 'emberglass_saber', 'sootcloak', 'veinstone_pendant', 'hearth_ring', 'manual_sunspoke', 'manual_glass_rain', 'manual_charwall', 'manual_verdant', 'manual_boneforge', 'manual_duskmantle'],
    privateStock: ['ashwraith_lantern', 'kilnheart_gauntlet', 'dawnpetal_elixir', 'manual_hollow_sight'],
    enemies: ['vein_bandit'], herbs: [] },
  { id: 'sect', name: 'Order of the Kindled Path', minRealm: 1, qiMult: 1.4, domain: 'vale', map: { x: 255, y: 118 },
    desc: 'Terraced halls climbing a mountain of banked coals. The Order keeps the Third Ember burning.',
    flavor: ['Disciples carry braziers up ten thousand steps. Nobody remembers why. Nobody stops.',
      'The mission board creaks under new postings and old grudges.',
      'An elder sleeps mid-lecture. His students take notes on his snoring, just in case.'],
    actions: ['meditate', 'missions', 'library', 'store', 'spar', 'challenge', 'talk', 'wander', 'austerities'],
    enemies: ['sect_aspirant'], herbs: [] },
  { id: 'glasswaste', name: 'The Glasswaste', minRealm: 2, qiMult: 2.1, domain: 'vale', map: { x: 320, y: 425 },
    desc: 'A desert of fused sand where the Fifth Sun struck. Dunes of smoked glass sing at noon.',
    flavor: ['Your reflection walks beside you in the dune-face. It is a poor conversationalist.',
      'Glass storms on the horizon: a curtain of glitter that flays.',
      'Bones of a caravan, annealed into the ground, pointing the way they never went.'],
    actions: ['meditate', 'forage', 'hunt', 'expedition', 'wander'],
    enemies: ['glasswaste_stalker', 'glasswing_mantis', 'cinderfang_alpha'],
    herbs: ['glassreed', 'suncap', 'duskpetal'] },
  { id: 'temple', name: 'The Hollow Temple', minRealm: 3, qiMult: 2.4, domain: 'vale', map: { x: 130, y: 430 },
    desc: 'A monastery carved into a cliff, abandoned mid-prayer. The bells still ring. Nobody rings them.',
    flavor: ['Prayer wheels spin against the wind’s direction.',
      'The refectory table is set for sixty. The dust is set for sixty, too.',
      'You find your own name in the guest ledger, in handwriting you almost recognize.'],
    actions: ['meditate', 'hunt', 'wander', 'temple_depths'],
    enemies: ['hollow_acolyte', 'storm_eyed_roc', 'molten_serpent'],
    herbs: ['duskpetal', 'suncap'] },
  { id: 'crater', name: 'The Ninth Crater', minRealm: 4, qiMult: 3.2, domain: 'vale', map: { x: 80, y: 105 },
    desc: 'The grave of the last fallen sun. Emberlight rises like reversed snow. This is where dawns go to be born, or fail.',
    flavor: ['The air tastes of copper and morning.',
      'Nine standing stones circle the crater rim. One has your family’s hearth-mark on it, centuries old.',
      'At the crater’s heart, something breathes with the slow patience of geology.'],
    actions: ['meditate', 'hunt', 'wander', 'crater_heart'],
    enemies: ['ashwraith_lord', 'molten_serpent', 'storm_eyed_roc'],
    herbs: ['dawnpetal', 'suncap', 'veinbloom'] },

  // ---- Domain: The Sunward Marches (realm 2+) ----
  { id: 'riverport', name: 'Riverport', minRealm: 2, qiMult: 0.9, domain: 'marches', map: { x: 480, y: 330 },
    desc: 'A city strung across three rivers on chains and bravado. Everything that floats ends up here, and most things float if priced correctly.',
    flavor: ['Barge crews sing tally-songs; the cargo answers in clinks and, once, a knock.',
      'The Grand Auction bell rings noon. Half the city adjusts its plans.',
      'A bridge tollkeeper waves cultivators through — bad experiences with the last three.'],
    actions: ['market', 'auction', 'listings', 'talk', 'wander', 'alchemy', 'rest'],
    market: ['provisions', 'emberdew_moss', 'duskpetal', 'glassreed', 'marrowroot', 'stormbud', 'emberdew_pill', 'duskclear_pill', 'stoneveil_pill', 'emberglass_saber', 'sootcloak', 'manual_glass_rain', 'manual_boneforge'],
    enemies: ['vein_bandit'], herbs: [] },
  { id: 'serpents_spine', name: 'The Serpent’s Spine', minRealm: 2, qiMult: 2.3, domain: 'marches', map: { x: 565, y: 175 },
    desc: 'A mountain range like a skeleton that lay down and never got up. Storm-qi pools in the vertebrae passes.',
    flavor: ['Thunder rolls up from below the cliffs. The mountains file it away.',
      'Prayer flags on the high passes, each one signed by someone who made it back.',
      'A drake-shadow slides across three ridgelines without hurrying.'],
    actions: ['meditate', 'forage', 'hunt', 'expedition', 'wander'],
    labels: { expedition: { label: 'Climb the High Passes', desc: 'Rope up for the storm-touched peaks: several fights, storm-herbs, drake territory.' } },
    enemies: ['spine_drake', 'cliff_ape', 'storm_eyed_roc'],
    herbs: ['stormbud', 'glassreed', 'suncap'] },
  { id: 'lotus_marsh', name: 'The Lotus Marsh', minRealm: 2, qiMult: 1.8, domain: 'marches', map: { x: 470, y: 455 },
    desc: 'Ten thousand mu of black water and white lotus. The marsh remembers being a lake; the lake remembers being holy.',
    flavor: ['Lotus lanterns drift past with prayers tucked in them. Some prayers are addressed to you. Unsettling.',
      'A heron regards you with the condescension of the truly ancient.',
      'Bubbles rise in a line, pause politely, and continue.'],
    actions: ['meditate', 'forage', 'hunt', 'wander'],
    enemies: ['grave_eel', 'glasswing_mantis', 'hollow_acolyte'],
    herbs: ['lotus_heart', 'duskpetal', 'marrowroot'] },
  { id: 'bone_orchard', name: 'The Bone Orchard', minRealm: 3, qiMult: 2.5, domain: 'marches', map: { x: 610, y: 395 },
    desc: 'A battlefield so old the graves grew into trees. The fruit is best left unpicked, and the harvest walks at dusk.',
    flavor: ['Wind through the bone-trees: a sound like a choir clearing its throat forever.',
      'Someone has been leaving fresh chrysanthemums. The bouquets are always exactly seven.',
      'You pass an unbroken sword planted in the soil. It is warm.'],
    actions: ['meditate', 'hunt', 'expedition', 'wander'],
    labels: { expedition: { label: 'Delve the Old Graves', desc: 'Go down among the roots and regiments: several fights, grave-goods, and worse.' } },
    enemies: ['bone_husk', 'hollow_acolyte', 'ashwraith_lord'],
    herbs: ['duskpetal', 'marrowroot'] },

  // ---- Domain: The Drowned Coast (realm 3+) ----
  { id: 'saltglass', name: 'Saltglass Harbor', minRealm: 3, qiMult: 1.0, domain: 'coast', map: { x: 700, y: 495 },
    desc: 'A port built from shipwrecks, ruled by harbor-mothers and tide-law. When the Fourth Sun drowned, its light kept burning on the seafloor — and divers have been rich and mad ever since.',
    flavor: ['The tide goes out and leaves the streets paved in brief silver.',
      'Divers argue depth-pay in a language that is half hand-signs, half scars.',
      'A harbor-mother blesses a new hull with salt, blood, and paperwork.'],
    actions: ['market', 'listings', 'talk', 'wander', 'rest', 'alchemy'],
    market: ['provisions', 'brinebloom', 'duskclear_pill', 'emberdew_pill', 'stoneveil_pill', 'star_iron', 'tidebreaker_harpoon', 'manual_duskmantle', 'manual_verdant'],
    enemies: ['vein_bandit'], herbs: [] },
  { id: 'sunken_star', name: 'The Sunken Star', minRealm: 3, qiMult: 2.8, domain: 'coast', map: { x: 820, y: 545 },
    desc: 'Where the Fourth Sun lies drowned. Its glow rises through forty fathoms like dawn seen from inside a closed eye.',
    flavor: ['The water is warm here, year-round, and tastes faintly of morning.',
      'Down in the glow, whales sing to the drowned sun. It hums back, off-key.',
      'A diver surfaces weeping. "It’s STILL TRYING TO RISE," she says, and laughs, and cannot stop.'],
    actions: ['meditate', 'expedition', 'wander'],
    labels: { expedition: { label: 'Dive the Drowned Sun', desc: 'Rope, weight-stones, and nerve: dive the glow for star-iron and worse company.' } },
    enemies: ['grave_eel', 'salt_wraith', 'molten_serpent'],
    herbs: ['brinebloom'] },
  { id: 'leviathan_graves', name: 'The Leviathan Graves', minRealm: 3, qiMult: 2.6, domain: 'coast', map: { x: 885, y: 425 },
    desc: 'A coast of ribs. The great deep-things beach themselves here to die, and have since before the suns fell. Their bones remember the abyss.',
    flavor: ['Each ribcage is a cathedral with the congregation long dispersed.',
      'Salt-wraiths drift between the bones like ushers who never accepted the funeral ended.',
      'Something two bays over exhales. Every bird on the coast leaves.'],
    actions: ['meditate', 'hunt', 'wander', 'tide_court'],
    enemies: ['salt_wraith', 'grave_eel', 'storm_eyed_roc'],
    herbs: ['brinebloom', 'suncap'] },

  // ---- Domain: The Burning Steppe (realm 4+) ----
  { id: 'horde_camps', name: 'The Iron Horde Camps', minRealm: 4, qiMult: 1.5, domain: 'steppe', map: { x: 700, y: 140 },
    desc: 'A moving nation of asura-blooded riders under the Banner That Does Not Cool. They respect exactly one thing, and hold a tournament for it every year.',
    flavor: ['Ten thousand tents, and every guy-rope humming in the steppe wind.',
      'Children here duel with heated iron rods, laughing. You were never that young.',
      'The Khan’s banner is visible from everywhere in camp. That is the point of the Khan.'],
    actions: ['market', 'arena', 'forge', 'talk', 'wander', 'rest'],
    market: ['provisions', 'marrowfire_pill', 'cindergrit_pill', 'kilnheart_gauntlet', 'khan_standard', 'manual_asura_roar', 'star_iron'],
    enemies: ['horde_champion'], herbs: [] },
  { id: 'ziggurat', name: 'The Obsidian Ziggurat', minRealm: 4, qiMult: 3.0, domain: 'steppe', map: { x: 835, y: 85 },
    desc: 'A stepped black mountain no one built, in a steppe with no other stones. The Ember Guardian circles its summit, keeping a verse of the Hymn from the world.',
    flavor: ['The ziggurat’s steps are sized for something with a longer stride than regret.',
      'Heat-shimmer crowns the summit even under snow.',
      'Horde riders detour around its shadow. The shadow does not always match the sun.'],
    actions: ['meditate', 'hunt', 'wander', 'ziggurat_heart'],
    enemies: ['asura_remnant', 'ashwraith_lord', 'molten_serpent'],
    herbs: ['suncap', 'veinbloom'] },
  { id: 'glass_sea', name: 'The Glass Sea of Asura', minRealm: 4, qiMult: 3.4, domain: 'steppe', map: { x: 905, y: 220 },
    desc: 'Where the asura host made its last stand against the falling Eighth Sun, and the land became a mirror. Their remnants still patrol a war that ended eight centuries ago.',
    flavor: ['Your reflection underfoot wears antique armor and does not apologize.',
      'Sunspawn drift like slow meteors looking for an address.',
      'At the horizon, the glass curls up into a frozen wave, mid-break, forever.'],
    actions: ['meditate', 'hunt', 'expedition', 'wander'],
    labels: { expedition: { label: 'March the Mirror', desc: 'Cross the old battlefield in force: remnant patrols, sunspawn, and eight-hundred-year-old loot.' } },
    enemies: ['asura_remnant', 'sunspawn', 'salt_wraith'],
    herbs: ['veinbloom', 'dawnpetal'] },
];

export const DOMAINS = [
  { id: 'vale', name: 'The Cindered Vale', minRealm: 0,
    desc: 'The old heartland: villages, veins, and the grave of the Ninth Sun.',
    blurb: 'Home. Small only until you leave it.',
    path: 'M40,80 Q20,250 80,380 Q120,500 260,480 Q380,460 370,330 Q400,180 300,80 Q160,20 40,80 Z',
    label: { x: 190, y: 62 } },
  { id: 'marches', name: 'The Sunward Marches', minRealm: 2,
    desc: 'River kingdoms, auction fortunes, storm mountains, and the graves of old wars.',
    blurb: 'Where the Vale’s roads go when they grow up.',
    path: 'M400,140 Q380,300 430,490 Q520,545 645,465 Q685,340 665,220 Q625,120 500,110 Q440,110 400,140 Z',
    label: { x: 530, y: 95 } },
  { id: 'coast', name: 'The Drowned Coast', minRealm: 3,
    desc: 'Tide-law harbors and the seafloor grave of the Fourth Sun, still burning.',
    blurb: 'One sun refused to go out. It waits, forty fathoms down.',
    path: 'M645,485 Q670,580 800,592 Q925,585 955,485 Q945,405 860,392 Q735,380 645,485 Z',
    label: { x: 800, y: 375 } },
  { id: 'steppe', name: 'The Burning Steppe', minRealm: 4,
    desc: 'The asura horde’s grasslands, the Glass Sea, and the Ziggurat no one built.',
    blurb: 'The horde respects one thing. Bring it.',
    path: 'M650,60 Q640,200 720,245 Q820,285 930,245 Q985,150 945,70 Q820,18 650,60 Z',
    label: { x: 800, y: 40 } },
];

// Road events: crossing between domains is a journey, and journeys collect stories.
export const ROAD_EVENTS = [
  { text: 'A toll-rope across the road, and four men with the bored menace of professionals. "Road tax. Cultivator rate."',
    choices: [
      { label: 'Pay 15 stones', req: { stones: 15 }, out: { stones: -15, log: 'You pay. The eldest bows with genuine courtesy — brigandage with standards.' } },
      { label: 'Refuse', out: { fight: 'vein_bandit', winLog: 'The toll-rope makes an excellent trophy. The road applauds silently.', loseLog: 'They take the toll and interest.' } },
    ] },
  { text: 'A pilgrim caravan walking the Wheel-road, lamps lit at midday. An old pilgrim offers to bless your journey for nothing but your name.',
    choices: [
      { label: 'Give your true name', out: { karma: 2, log: 'She speaks it once to the road ahead, as introduction. The miles after feel downhill both ways.' } },
      { label: 'Give a false name', out: { karma: -1, log: 'She blesses the false name warmly. Somewhere, a stranger’s luck improves.' } },
    ] },
  { text: 'A river crossing where the bridge has washed out. A ferryman with a raft eyes your build. "Help me pole against the current and ride free."',
    choices: [
      { label: 'Pole the raft (Body check)', out: { roll: { stat: 'body', dc: 6, win: { cult: 10, log: 'You arrive with burning shoulders and a ferryman’s respect, which is currency in three provinces.' }, lose: { hp: -8, log: 'The current wins on points. You arrive soaked, alive, and heavier by one lesson about rivers.' } } } },
      { label: 'Pay 8 stones', req: { stones: 8 }, out: { stones: -8, log: 'You ride dry. The ferryman poles like the river owes him money.' } },
    ] },
  { text: 'A wandering trader with a laden mule and no escort, delighted to see a cultivator. "Walk with me a mile and pick anything — traveler’s discount."',
    choices: [
      { label: 'Buy provisions cheap (6 stones)', req: { stones: 6 }, out: { stones: -6, items: { provisions: 1 }, log: 'Grain, salt-plums, and gossip at a mile-rate. The mule approves of you.' } },
      { label: 'Buy herbs cheap (10 stones)', req: { stones: 10 }, out: { stones: -10, items: { emberdew_moss: 1, graypine_sap: 1 }, log: 'Fresh-cut and honestly weighed, which on this road counts as a miracle.' } },
      { label: 'Walk on', out: { log: 'The trader waves anyway. The mule does not.' } },
    ] },
];

export const AUCTION_POOL = [
  { item: 'dawnpetal', base: 550 },
  { item: 'dawnpetal_elixir', base: 1300 },
  { item: 'ashwraith_lantern', base: 1900 },
  { item: 'kilnheart_gauntlet', base: 1200 },
  { item: 'star_iron_blade', base: 2700 },
  { item: 'hearth_ring', base: 700 },
  { item: 'manual_emberstorm', base: 1700 },
  { item: 'manual_hollow_sight', base: 1400 },
  { item: 'manual_asura_roar', base: 2200 },
  { item: 'suncap', base: 60 },
  { item: 'veinbloom', base: 75 },
  { item: 'marrowfire_pill', base: 190 },
];

export const JOBS = [
  { label: 'Haul charcoal for the kiln houses', stones: [8, 16], line: 'Your shoulders ache. Your purse does not.' },
  { label: 'Copy scripture for the shrine keeper', stones: [10, 20], line: 'Your hand cramps around the brush, but the keeper pays in worn coin and warm soup.' },
  { label: 'Stand night watch on the palisade', stones: [12, 22], line: 'Nothing comes out of the dark. Being paid for nothing is underrated.' },
];

// Mission templates for the sect board; engine instantiates them.
export const MISSIONS = [
  { id: 'hunt_t1', name: 'Cull the Greypine packs', kind: 'hunt', enemy: 'graypine_wolf', contrib: 40, stones: 30, minRealm: 1 },
  { id: 'hunt_t2', name: 'Break the vein-bandit camp', kind: 'hunt', enemy: 'vein_bandit', contrib: 55, stones: 50, minRealm: 1 },
  { id: 'gather_moss', name: 'Deliver 3 Emberdew Moss', kind: 'gather', item: 'emberdew_moss', qty: 3, contrib: 35, stones: 20, minRealm: 1 },
  { id: 'gather_reed', name: 'Deliver 2 Glassreed', kind: 'gather', item: 'glassreed', qty: 2, contrib: 45, stones: 30, minRealm: 1 },
  { id: 'hunt_t3', name: 'Put down the Cinderfang Alpha', kind: 'hunt', enemy: 'cinderfang_alpha', contrib: 90, stones: 90, minRealm: 2 },
  { id: 'gather_vein', name: 'Deliver 2 Veinbloom', kind: 'gather', item: 'veinbloom', qty: 2, contrib: 80, stones: 70, minRealm: 2 },
  { id: 'hunt_t4', name: 'Slay the Molten Serpent', kind: 'hunt', enemy: 'molten_serpent', contrib: 160, stones: 180, minRealm: 3 },
  { id: 'donate', name: 'Tithe 100 spirit stones', kind: 'donate', stones: -100, contrib: 50, minRealm: 1 },
];

export const SECT_RANKS = [
  { name: 'Outer Disciple', at: 0, stipend: 10 },
  { name: 'Inner Disciple', at: 300, stipend: 25 },
  { name: 'Core Disciple', at: 900, stipend: 60 },
  { name: 'Elder of the Kindled Path', at: 2000, stipend: 150 },
];

export const SECT_STORE = [
  { item: 'emberdew_pill', contrib: 30 }, { item: 'duskclear_pill', contrib: 28 },
  { item: 'stoneveil_pill', contrib: 70 }, { item: 'manual_sunspoke', contrib: 260, rank: 1 },
  { item: 'manual_verdant', contrib: 280, rank: 1 }, { item: 'marrowfire_pill', contrib: 150, rank: 1 },
  { item: 'clearmind_pill', contrib: 150, rank: 1 }, { item: 'veinstone_pendant', contrib: 220, rank: 1 },
  { item: 'manual_emberstorm', contrib: 700, rank: 2 }, { item: 'kilnheart_gauntlet', contrib: 900, rank: 2 },
  { item: 'dawnpetal_elixir', contrib: 1400, rank: 3 },
];

export const LIBRARY = [
  { tech: 'kindled_step', contrib: 120, rank: 0 },
  { tech: 'smokebind', contrib: 140, rank: 0 },
  { tech: 'charwall', contrib: 240, rank: 1 },
  { tech: 'glass_rain', contrib: 260, rank: 1 },
  { tech: 'boneforge', contrib: 300, rank: 1 },
  { tech: 'duskmantle', contrib: 300, rank: 1 },
  { tech: 'emberstorm', contrib: 800, rank: 2 },
  { tech: 'dawnlance', contrib: 1800, rank: 3 },
];

// Hearthline bloodline perks. apply: interpreted at character creation / relevant checks.
export const PERKS = [
  { id: 'ashborn_veins', name: 'Ashborn Veins', stat: 'spirit',
    tiers: [{ cost: 10 }, { cost: 25 }, { cost: 50 }], desc: 'The family’s channels run hot. +1 Spirit per tier.' },
  { id: 'oxblood_frame', name: 'Oxblood Frame', stat: 'body',
    tiers: [{ cost: 10 }, { cost: 25 }, { cost: 50 }], desc: 'Broad backs, stubborn bones. +1 Body per tier.' },
  { id: 'lantern_mind', name: 'Lantern Mind', stat: 'mind',
    tiers: [{ cost: 10 }, { cost: 25 }, { cost: 50 }], desc: 'The family keeps its wits lit. +1 Mind per tier.' },
  { id: 'red_thread', name: 'Red Thread of Fate', stat: 'fate',
    tiers: [{ cost: 15 }, { cost: 35 }], desc: 'Luck runs in the blood. +1 Fate per tier.' },
  { id: 'old_hearth', name: 'Old Hearth’s Warmth', tiers: [{ cost: 60 }],
    desc: 'Heirs awaken in the cradle: begin at Sparkgathering, Early stage.' },
  { id: 'family_name', name: 'The Family Name', tiers: [{ cost: 30 }],
    desc: 'The Order remembers your line: heirs of members start as Outer Disciples with 100 contribution.' },
  { id: 'ember_frugality', name: 'Ember Frugality', tiers: [{ cost: 20 }],
    desc: 'Haggling is a bloodline art. All market prices -15%.' },
  { id: 'long_wick', name: 'Long Wick', tiers: [{ cost: 40 }],
    desc: 'The family flame burns slow: +10 years of lifespan at every realm.' },
  { id: 'kiln_lungs', name: 'Kiln Lungs', tiers: [{ cost: 25 }],
    desc: 'Breath like bellows: meditation yields +15% Emberlight.' },
];

export const QUESTS = [
  { id: 't_sweep', name: 'Granny’s Errand: the Shrine', desc: 'Visit the Hearth Shrine. Sweep first, questions after.', tutorial: true, done: s => !!s.chr.flags.tut_shrine },
  { id: 't_sit', name: 'Granny’s Errand: One Good Breath', desc: 'Meditate once. The veins answer politeness.', tutorial: true, done: s => !!s.chr.flags.tut_meditate },
  { id: 't_green', name: 'Granny’s Errand: Something Green', desc: 'Forage the Greypine Wilds for anything that grows.', tutorial: true, done: s => !!s.chr.flags.tut_forage },
  { id: 't_coin', name: 'Granny’s Errand: Honest Coin', desc: 'Buy or sell something at a market. Haggle badly; it builds character.', tutorial: true, done: s => !!s.chr.flags.tut_market },
  { id: 'q_awaken', name: 'Feel the Emberlight', desc: 'Meditate until the ember veins answer: reach Sparkgathering.', done: s => s.chr.realm >= 1 },
  { id: 'q_sect', name: 'The Ten Thousand Steps', desc: 'Pass the trial of the Order of the Kindled Path.', done: s => s.chr.sect.joined },
  { id: 'q_beggar', name: 'The Beggar’s Riddle', desc: 'The scarred beggar hums nine notes. Find all three of his lessons by wandering.', done: s => (s.chr.flags.beggar || 0) >= 3 },
  { id: 'q_rival', name: 'Glass and Ember', desc: 'Settle things with Yan Shuo, however many lifetimes it takes.', done: s => s.chr.flags.rival_settled },
  { id: 'q_temple', name: 'What the Abbot Kept', desc: 'Descend into the Hollow Temple’s depths and face what remains.', done: s => s.chr.flags.abbot_slain },
  { id: 'q_hymn', name: 'The Tenthfire Hymn', desc: 'Gather three Hymn Fragments: the Crater Choir and the Warden in the Ninth Crater, and the Ember Guardian atop the Obsidian Ziggurat of the Burning Steppe.', done: s => s.chr.flags.hymn_learned },
  { id: 'q_dawn', name: 'Kindle the Tenth Dawn', desc: 'At Sunforging Peak, with the Hymn on your lips, ascend at the Ninth Crater’s heart.', done: s => s.chr.realm >= 6 },
];

// Wander events. w = weight; once = one time per generation; minRealm gates.
// out/req use the outcome DSL in actions.js
export const EVENTS = [
  {
    id: 'beggar_1', regions: ['ashfen', 'greypine'], w: 3, once: true,
    text: 'A beggar with lightning-scars down one arm sits by the road, humming nine falling notes. "Spare a coin," he says, "or spare an hour. One of them pays better."',
    choices: [
      { label: 'Give him 20 stones', req: { stones: 20 }, out: { stones: -20, flagInc: 'beggar', log: 'He bites the coin, laughs, and teaches you the first falling note. Something in your breathing shifts forever.', cult: 15 } },
      { label: 'Sit with him for an hour', out: { flagInc: 'beggar', log: 'He talks about the sky before the fall — nine suns like ripe fruit. When he hums, your qi hums back.', cult: 15 } },
      { label: 'Walk on', out: { log: 'You walk on. The humming follows you for a mile, then gives up.' } },
    ],
  },
  {
    id: 'beggar_2', regions: ['cinder_market', 'mines'], w: 3, once: true, req: { flagMin: ['beggar', 1] },
    text: 'The same beggar, impossibly, sits outside a pill shop arguing with a fortune teller about the weather in ten years. He waves you over. "Second lesson. Free. That should worry you."',
    choices: [
      { label: 'Listen', out: { flagInc: 'beggar', log: 'He teaches you to breathe on the exhale of the world. Your meditation will never be the same.', cult: 30 } },
      { label: 'Ask who he really is', out: { flagInc: 'beggar', log: '"A man who missed his tribulation appointment," he says, "and got a better offer." He teaches you anyway.', cult: 30 } },
    ],
  },
  {
    id: 'beggar_3', regions: ['glasswaste', 'temple', 'crater'], w: 3, once: true, req: { flagMin: ['beggar', 2] },
    text: 'At the edge of nowhere, the beggar sits on a dune of glass, roasting a yam over a flame that casts no light. "Third lesson. Then I stop following you around. It’s getting embarrassing."',
    choices: [
      { label: 'Receive the Ninefold Breath', out: { flagInc: 'beggar', tech: 'ninefold_breath', log: 'The full scripture settles into your lungs like a landlord. Meditation gains +30% forever. When you look up, there is only a yam, still warm, and no beggar.' } },
    ],
  },
  {
    id: 'rival_intro', regions: ['sect', 'cinder_market'], w: 3, once: true, minRealm: 1,
    text: 'A young cultivator in glass-trimmed robes blocks your path. "Yan Shuo," he says, as if the name were a title. "They say you’re promising. I collect promising people’s losses."',
    choices: [
      { label: 'Accept the duel', out: { fight: 'yan_shuo_1', winFlag: 'rival1', winLog: 'Yan Shuo stares up from the dust, recalculating his entire cosmology. "Again," he says. "Someday." He means it.', loseLog: 'He leaves you your dignity, mostly. "Train," he says, not unkindly, which is worse.' } },
      { label: 'Decline politely', out: { log: '"Sensible," he says, disappointed, and leaves. You feel his attention remain, like a hook.' } },
    ],
  },
  {
    id: 'rival_2', regions: ['glasswaste', 'temple'], w: 3, once: true, minRealm: 3, req: { flag: 'rival1' },
    text: 'Yan Shuo again — taller, colder, an Inner Blade of his sect now. "I made a list of your weaknesses," he says, unrolling an actual scroll. "It’s shorter than it was. Fix that for me."',
    choices: [
      { label: 'Duel him', out: { fight: 'yan_shuo_2', winFlag: 'rival2', winLog: 'He laughs from the ground — the first honest sound you’ve heard him make. "Good. GOOD. The Crater, then. When we’re both worth it."', loseLog: 'He helps you up, which costs him visible effort. "Item four on the list," he says. "Footwork."' } },
      { label: 'Not today', out: { log: 'He rolls up the scroll. "I’ll wait. I’m very good at it. Item one on YOUR list: you hesitate."' } },
    ],
  },
  {
    id: 'rival_3', regions: ['crater'], w: 4, once: true, minRealm: 5, req: { flag: 'rival2' },
    text: 'On the crater rim, under rising ember-snow, Yan Shuo waits in white. "Glass Saint, they call me now. One of us kindles a dawn. The other one watches. I came to settle which."',
    choices: [
      { label: 'The final duel', out: { fight: 'yan_shuo_3', winFlag: 'rival_settled', winLog: 'Yan Shuo kneels among nine standing stones, bleeding light. He presses his dawnlance manual into your hands. "Watching," he says, "is also a path." He stays to see you rise.', loseLog: 'You wake three days later, bandaged with expensive silk. A note: "Not like that. Try again. — Y.S."' } },
      { label: 'Withdraw for now', out: { log: 'He nods once. The ember-snow settles on his shoulders and does not melt.' } },
    ],
  },
  {
    id: 'withering_cough', regions: ['ashfen'], w: 2, once: true,
    text: 'The Withering Cough sweeps Ashfen Hollow. The shrine keeper, gray-lipped, asks any cultivator to help — the sick need Duskclear Pills, or a miracle.',
    choices: [
      { label: 'Donate 2 Duskclear Pills', req: { items: { duskclear_pill: 2 } }, out: { items: { duskclear_pill: -2 }, flag: 'saved_village', fate: 1, karma: 3, log: 'The fevers break within days. The village will not forget. (+1 Fate — the hearth gods keep accounts.)' } },
      { label: 'Nurse the sick yourself (Mind check)', out: { roll: { stat: 'mind', dc: 6, win: { flag: 'saved_village', fate: 1, karma: 3, log: 'Weeks of broth, boiled linen, and stubbornness. The village pulls through. (+1 Fate.)' }, lose: { hp: -15, karma: 1, log: 'You catch the cough yourself and spend a bitter month sweating it out.' } } } },
      { label: 'Keep your distance', out: { karma: -2, log: 'Cultivators outlive villages. That’s the arithmetic. It keeps you up anyway.' } },
    ],
  },
  {
    id: 'merchant_scam', regions: ['cinder_market'], w: 4,
    text: 'A silk-voiced merchant offers a "Dawnpetal, freshly picked, family emergency price" — 200 stones. It is either the deal of the century or a dyed chrysanthemum.',
    choices: [
      { label: 'Inspect it closely (Mind check)', out: { roll: { stat: 'mind', dc: 7, win: { log: 'Chrysanthemum. Dyed with ember-ink, which is at least artisanal fraud. The merchant vanishes into the crowd, applauding you sarcastically.' }, lose: { stones: -200, items: { duskpetal: 1 }, log: 'You pay. Up close, later, it is a very ordinary Duskpetal wearing makeup. Tuition, you decide, for a course in humility.' } } } },
      { label: 'Buy it instantly', req: { stones: 200 }, out: { roll: { stat: 'fate', dc: 9, win: { stones: -200, items: { dawnpetal: 1 }, log: 'It is REAL. The merchant genuinely needed the money. The whole market holds its breath.' }, lose: { stones: -200, items: { duskpetal: 1 }, log: 'A dyed Duskpetal. The merchant is already three provinces away, telling this story.' } } } },
      { label: 'Walk away', out: { log: 'Rule one of the Cinder Market: emergencies are wholesale.' } },
    ],
  },
  {
    id: 'vein_surge', regions: ['mines', 'greypine'], w: 4, minRealm: 1,
    text: 'The ground shivers — an ember vein surges beneath you, light bleeding up through the soil like a sunrise trying to escape.',
    choices: [
      { label: 'Sit and absorb it', out: { cult: 35, log: 'You drink the surge like a held breath. Cultivation floods in.' } },
      { label: 'Harvest crystallizing motes (Fate check)', out: { roll: { stat: 'fate', dc: 7, win: { items: { veinbloom: 1, vein_ore: 2 }, log: 'Motes harden mid-air into Veinbloom and ore. The mountain’s gift, or its distraction.' }, lose: { hp: -10, log: 'The surge snaps shut like a jaw. You yank scorched fingers back.' } } } },
    ],
  },
  {
    id: 'lost_child', regions: ['greypine'], w: 3,
    text: 'A charcoal-burner’s child sits under a graypine, lost, refusing to cry with visible effort.',
    choices: [
      { label: 'Carry them home', out: { stones: 15, fateSmall: true, karma: 1, log: 'The burner insists on paying in coin and a blessing. The blessing feels heavier.' } },
      { label: 'Teach them to find south, then walk together', out: { stones: 15, karma: 1, log: 'They walk you home, technically. The burner laughs like a landslide and pays you anyway.' } },
    ],
  },
  {
    id: 'glass_storm', regions: ['glasswaste'], w: 4,
    text: 'The horizon glitters — a glass storm, minutes out, wide as a weather system and exactly as negotiable.',
    choices: [
      { label: 'Shelter under a dune shelf (Body check)', out: { roll: { stat: 'body', dc: 7, win: { items: { glasswing: 2 }, log: 'You hold the shelf steady while the world sands itself smooth. After, the storm’s leavings glitter for the taking.' }, lose: { hp: -20, log: 'The shelf collapses. You emerge sanded, glittering, and educated.' } } } },
      { label: 'Outrun it', out: { roll: { stat: 'fate', dc: 8, win: { log: 'You crest the last dune as the storm closes like an eye behind you. Exhilarating. Unrepeatable.' }, lose: { hp: -14, log: 'You do not outrun weather. You are now weather-adjacent and bleeding.' } } } },
    ],
  },
  {
    id: 'temple_ledger', regions: ['temple'], w: 3, once: true,
    text: 'In the temple guest ledger, one line glows faintly: your family’s hearth-mark, an entry dated two hundred years ago. "Came asking about the Hymn. Sent to the Crater. May the ash forgive us."',
    choices: [
      { label: 'Tear out the page and keep it', out: { flag: 'ancestor_ledger', cult: 25, log: 'An ancestor walked this path before you and the world kept the receipt. Your resolve hardens into cultivation.' } },
      { label: 'Leave it, add your own name', out: { flag: 'ancestor_ledger', fateSmall: true, log: 'You sign beneath your ancestor. The ink dries instantly, like the ledger was thirsty.' } },
    ],
  },
  {
    id: 'pill_smoke_dream', regions: ['ashfen', 'cinder_market'], w: 2, minRealm: 1,
    text: 'Falling asleep near an alchemist’s chimney, you dream in recipes: your grandmother’s hands folding herbs like laundry.',
    choices: [
      { label: 'Chase the dream', out: { cult: 12, log: 'You wake with warm hands and a steadier flame-sense.' } },
    ],
  },
  {
    id: 'miners_knock', regions: ['mines'], w: 3, once: true, minRealm: 1,
    text: 'From the sealed shaft, four knocks. The miners have all gone quiet, looking at you, the cultivator. Four knocks. Never four.',
    choices: [
      { label: 'Knock back three, firmly', out: { log: 'Silence. Then — three knocks, softer, almost polite. Whatever it is, it respects the protocol. The miners exhale and buy you drinks for a week.', stones: 25 } },
      { label: 'Open the shaft', out: { fight: 'hollow_acolyte', winLog: 'It was a Hollow Acolyte, walled in decades ago and very patient. Was. The miners seal the shaft behind you with fresh mortar and old prayers.', loseLog: 'It moves like smoke through a keyhole. The miners drag you out and seal the shaft with everything including their lunch benches.' } },
      { label: 'Walk away quietly', out: { log: 'Some doors are load-bearing. You leave this one alone.' } },
    ],
  },
  {
    id: 'roc_shadow', regions: ['temple', 'crater'], w: 3, minRealm: 3,
    text: 'A shadow the size of a field crosses you — the Storm-Eyed Roc, wheeling, deciding whether you are scenery or protein.',
    choices: [
      { label: 'Stand your ground and flare your qi', out: { roll: { stat: 'spirit', dc: 9, win: { cult: 30, log: 'The Roc reads your flame, files you under "inadvisable," and thermals away. Facing it down tempers your spirit.' }, lose: { fight: 'storm_eyed_roc', loseLog: 'The Roc disagrees with your assessment of yourself.', winLog: 'The Roc disagrees, loses the argument, and leaves feathers.' } } } },
      { label: 'Take cover', out: { log: 'You become scenery. The shadow passes. Scenery survives; it’s a whole strategy.' } },
    ],
  },
  {
    id: 'hearth_dream', regions: ['ashfen'], w: 2, minGen: 2,
    text: 'At the family shrine, incense smoke bends into a familiar silhouette — your predecessor, or the shape memory gives them. They nod at what you’ve become.',
    choices: [
      { label: 'Sit with them awhile', out: { cult: 20, qiPct: 1, log: 'No words. The smoke straightens. Your qi feels swept clean, like a room made ready for guests.' } },
    ],
  },
  {
    id: 'stray_dog', regions: ['ashfen', 'cinder_market', 'greypine'], w: 3,
    text: 'A soot-colored dog adopts you at high speed, executing the sit of a professional.',
    choices: [
      { label: 'Share your rations', out: { fateSmall: true, karma: 1, log: 'The dog escorts you for a day, ferociously guarding you from leaves. Small luck sticks to your heels afterward.' } },
      { label: 'Shoo it off', out: { karma: -1, log: 'The dog leaves with the dignity of an emperor in exile. You feel briefly, correctly, judged.' } },
    ],
  },
  {
    id: 'crater_stone', regions: ['crater'], w: 3, once: true,
    text: 'Nine standing stones ring the crater. On the eighth, under centuries of ash: your family’s hearth-mark, and a handhold worn smooth by one ancestor’s repeated climbing.',
    choices: [
      { label: 'Climb where they climbed', out: { cult: 60, flag: 'crater_stone', log: 'Your hand fits the groove exactly. Generations of intent flow up your arm like heat from a banked fire.' } },
    ],
  },
  {
    id: 'auction_whisper', regions: ['cinder_market'], w: 2, minRealm: 2,
    text: 'A back-alley auctioneer whispers: "Suncap, wild-grown, one previous owner who no longer needs anything." 120 stones, no questions.',
    choices: [
      { label: 'Buy it', req: { stones: 120 }, out: { stones: -120, items: { suncap: 1 }, log: 'The Suncap is real and excellent. You elect not to think about the previous owner.' } },
      { label: 'Ask questions anyway', out: { log: 'The auctioneer evaporates. In the Market, questions are a form of payment nobody accepts.' } },
    ],
  },
  {
    id: 'sect_politics', regions: ['sect'], w: 3, minRealm: 1,
    text: 'Two elders argue over brazier placement with the intensity of border warfare. Both suddenly turn to you: "Disciple! Adjudicate."',
    choices: [
      { label: 'Side with Elder Ash (Mind check)', out: { roll: { stat: 'mind', dc: 6, win: { contrib: 30, log: 'You cite draft codes, wind direction, and precedent. Elder Ash beams. Contribution flows.' }, lose: { log: 'Your argument collapses under cross-examination. Both elders unite in disappointment — the only unity they’ve shown in years. Arguably a service.' } } } },
      { label: 'Propose moving both braziers', out: { contrib: 15, log: 'Compromise: everyone equally unhappy, you marginally rewarded. The sect way.' } },
    ],
  },
  {
    id: 'old_soldier', regions: ['greypine', 'mines'], w: 2,
    text: 'A retired sect soldier chops wood with a technique far too good for firewood. He notices you noticing.',
    choices: [
      { label: 'Ask for pointers', out: { roll: { stat: 'fate', dc: 5, win: { cult: 25, log: '"Elbows," he says, and adjusts yours one inch. The inch is worth a season of training.' }, lose: { log: '"Buy the wood or move along." You buy some wood. It is good wood.', stones: -5 } } } },
      { label: 'Just watch and learn', out: { cult: 10, log: 'You memorize the rhythm of it. Even his rest strokes teach.' } },
    ],
  },
  {
    id: 'naga_offering', regions: ['mines'], w: 3, once: true, minRealm: 1,
    text: 'In the deepest gallery, a pool that shouldn’t hold water holds water. Coils move beneath it — patient, ancient, scaled in vein-light. The miners leave milk and silver here, on the old advice.',
    choices: [
      { label: 'Leave milk and 20 stones', req: { stones: 20 }, out: { stones: -20, flag: 'naga_friend', karma: 2, insight: 'in_old_courtesy', log: 'The offerings sink without ripple. The coils turn once, approving, and the whole vein seems to breathe easier. The miners will speak of the day the cultivator kept the old courtesy.' } },
      { label: 'Demand the naga’s treasure', out: { roll: { stat: 'body', dc: 9, win: { items: { veinbloom: 2, suncap: 1 }, karma: -3, flag: 'naga_wronged', log: 'You wrestle a king of the under-waters for an armful of its garden. You win. Its eyes, disappearing, promise a later chapter.' }, lose: { hp: -25, karma: -2, flag: 'naga_wronged', log: 'The water stands up. When you wake on the gallery floor, your purse is lighter and the pool is gone — moved houses, offended.' , stones: -30 } } } },
      { label: 'Bow and withdraw', out: { karma: 1, log: 'Some doors are neighbors, not doors. You leave the pool its privacy.' } },
    ],
  },
  {
    id: 'naga_boon', regions: ['mines'], w: 3, once: true, minRealm: 1, req: { flag: 'naga_friend' },
    text: 'The impossible pool again — risen to meet you. On a flat stone at its rim: a Veinbloom wet with under-water, arranged. A gift has been prepared. Reciprocity, among old things, is law.',
    choices: [
      { label: 'Accept with both hands', out: { items: { veinbloom: 1 }, cult: 25, log: 'As your fingers close on the flower, the naga’s regard passes through you like warm current — a season of cultivation in a single breath. Keep the old courtesies. They keep back.' } },
    ],
  },
  {
    id: 'wandering_sadhu', regions: ['greypine', 'glasswaste', 'crater'], w: 2, once: true, minRealm: 1,
    text: 'A wanderer sits by the path on a mat of woven ash, hair bound in a knot that has outlasted empires. He is not meditating. He is listening — to you, apparently, from a great way off. "Sit," he says. "The Wheel and I take tea sometimes. It mentions you."',
    choices: [
      { label: 'Sit with him', out: { sadhu: true } },
      { label: 'Make an excuse', out: { log: '"Another turning, then," he says, unoffended, and returns to listening to something enormous and slow.' } },
    ],
  },
  {
    id: 'asura_pit', regions: ['cinder_market'], w: 2, minRealm: 2,
    text: 'Below the Market, torchlight and a ring of shouting faces: a pit champion with asura blood — four arms of slag-dark muscle, grin full of grave-markers — is offering triple stakes to any cultivator with a spine.',
    choices: [
      { label: 'Fight the asura-blooded champion', out: { fight: 'cinderfang_alpha', winLog: 'You stand over four twitching arms while the pit loses its collective mind. The bookmakers pay triple, weeping.', loseLog: 'You wake being fanned by strangers who bet on you anyway, out of sentiment.', winStones: 200 } },
      { label: 'Bet on him instead', req: { stones: 50 }, out: { roll: { stat: 'fate', dc: 6, win: { stones: 60, log: 'The champion wins in two blows. Your purse grows fat on someone else’s bruises.' }, lose: { stones: -50, karma: -1, log: 'A farm girl with a quarterstaff drops the champion in front of everyone. Your stake evaporates amid scenes of unbearable joy.' } } } },
      { label: 'Walk on', out: { log: 'The Market roars behind you like a beast fed nightly.' } },
    ],
  },
  {
    id: 'gu_broker', regions: ['cinder_market'], w: 2, minRealm: 1,
    text: 'A broker with too many rings sets a sealed gourd on the table between you. Something inside shifts its weight. "Contents guaranteed alive," he says. "Nature of contents: the buyer’s adventure. Forty stones, no returns, and I’d open it outdoors."',
    choices: [
      { label: 'Buy the gourd and open it outdoors', req: { stones: 40 }, out: { roll: { stat: 'fate', dc: 7, win: { stones: -40, items: { beast_core: 2, veinbloom: 1 }, log: 'A vein-beetle the size of a fist, asleep on a Veinbloom it has hoarded. Beetles of this kind are worth their weight in cores. The broker, watching from a distance, mouths "no returns" sadly.' }, lose: { stones: -40, karma: -1, hp: -12, log: 'Something with opinions and pincers. By the time you subdue it, the broker is gone and your dignity is disputed territory.' } } } },
      { label: 'Report him to the market wardens', out: { karma: 1, stones: 10, log: 'The wardens confiscate the gourd with tongs and pay you an informant’s fee. From inside the gourd: applause? Best not to wonder.' } },
      { label: 'Decline', out: { log: '"Your loss," says the broker, to you, and also to the gourd, reassuringly.' } },
    ],
  },
  {
    id: 'sect_defector', regions: ['greypine', 'mines'], w: 2, once: true, minRealm: 1,
    text: 'A man in torn inner-disciple robes flags you down from a ditch, one arm bound, eyes going to every shadow. "They’ll say I stole the technique. I wrote half of it. Shelter me one night and my notes are yours — or drag me back and be paid in merit. Choose fast."',
    choices: [
      { label: 'Shelter him for the night', out: { karma: 2, tech: 'smokebind', log: 'He sleeps like the hunted, wakes before dawn, and leaves his notebook weighted under a stone: rope-smoke bindings, annotated by a bitter genius. You never learn how his story ends. That is probably the price.' } },
      { label: 'Turn him in to the Order', out: { contrib: 60, karma: -3, log: 'The pursuers pay in contribution chits and do not thank you. His eyes, as they bind him, do the arithmetic of your soul out loud, silently.' } },
      { label: 'Give him rations and look away', req: { items: { provisions: 1 } }, out: { items: { provisions: -1 }, karma: 1, log: 'You leave food on a stone and study some fascinating clouds. When you look back, ditch and man are empty. Cheap, as mercies go.' } },
    ],
  },
  {
    id: 'ash_ghost', regions: ['temple', 'glasswaste', 'crater'], w: 2, once: true, minRealm: 2,
    text: 'At dusk, a figure of packed gray ash stands at a respectful distance, holding something flat and pale. It does not breathe, because it cannot. It offers the letter with the terrible politeness of someone who has been waiting a very long time to ask one favor.',
    choices: [
      { label: 'Take the letter', out: { flag: 'ghost_letter', karma: 1, log: 'The letter is addressed, in beautiful old script, to a family in Ashfen Hollow. The figure bows and comes apart on the wind, its errand finally someone else’s. The paper is cold and does not warm.' } },
      { label: 'Back away', out: { log: 'The figure lowers the letter and turns to face the horizon again. It has patience, and nothing else left.' } },
    ],
  },
  {
    id: 'ghost_delivery', regions: ['ashfen'], w: 4, once: true, req: { flag: 'ghost_letter' },
    text: 'The address from the ash-figure’s letter is a leaning house by the palisade. An old woman answers, sees the handwriting, and sits down where she stands, on the doorstep, all at once.',
    choices: [
      { label: 'Stay while she reads it', out: { karma: 3, stones: 20, flag: 'ghost_letter_done', log: '"My grandfather," she says. "He went to the waste for medicine money when my mother was small." She reads it four times. She pays you the letter’s postage, sixty years late, from a jar kept for exactly this, and you cannot refuse it without insulting three generations.' } },
    ],
  },
  {
    id: 'starving_family', regions: ['ashfen', 'greypine'], w: 2, minRealm: 0,
    text: 'A charcoal-burner’s family at the road’s edge, moving somewhere with everything they own on one handcart. The youngest is chewing bark, methodically, the way children do when they’ve decided not to complain.',
    choices: [
      { label: 'Give them a season’s provisions', req: { items: { provisions: 1 } }, out: { items: { provisions: -1 }, karma: 2, fateSmall: true, log: 'The father tries to refuse and fails on the second try. The mother notes your face aloud, so the children will remember it. You eat lighter this season and somehow do not mind.' } },
      { label: 'Give a few stones', req: { stones: 10 }, out: { stones: -10, karma: 1, log: 'Coin is thinner than food but spends wider. The cart creaks on with slightly better prospects.' } },
      { label: 'Pass by', out: { karma: -1, log: 'The road is long and everyone on it is somebody’s story. You tell yourself yours needs the provisions more, and walk to the rhythm of the child chewing bark.' } },
    ],
  },
  {
    id: 'riverport_grifter', regions: ['riverport'], w: 3,
    text: 'A woman with an honest face and dishonest dice invites you to a game of Nine Bones on an upturned barrel. A small crowd of previous customers watches with the specific silence of tuition already paid.',
    choices: [
      { label: 'Play a round (Mind check)', req: { stones: 20 }, out: { roll: { stat: 'mind', dc: 8, win: { stones: 35, log: 'You track the weighted bone through four switches and name it without touching. She pays out laughing and offers you a job. You decline; the crowd files the moment away.' }, lose: { stones: -20, log: 'The bones land the way she needs them to, five times running. An education, purchased retail.' } } } },
      { label: 'Watch the hands, not the bones', out: { cult: 8, log: 'Sleight is footwork for fingers. You memorize three switches and a false shuffle — technique is technique.' } },
      { label: 'Walk on', out: { log: 'Behind you the dice rattle like small, profitable thunder.' } },
    ],
  },
  {
    id: 'marsh_lantern', regions: ['lotus_marsh'], w: 3, once: true,
    text: 'At dusk a lotus lantern drifts against the current to bump your ankle, insistent as a cat. The prayer inside is written in a child’s hand: "for the cultivator who is coming. the marsh said so."',
    choices: [
      { label: 'Follow the lantern upstream', out: { roll: { stat: 'fate', dc: 6, win: { items: { lotus_heart: 2 }, karma: 1, log: 'The lantern leads you to a stand of century lotus and a fishing family who dreamed of you twice. They will not let you leave without the hearts, or without soup.' }, lose: { hp: -10, log: 'The lantern leads you into black water and hip-deep mud with a sense of humor. When you finally look up, it has gone on without you.' } } } },
      { label: 'Relight it and send it on', out: { karma: 1, log: 'You feed the little flame and push it back into the current. Somewhere upstream, someone’s prayer arrives slightly warmer.' } },
    ],
  },
  {
    id: 'steppe_hospitality', regions: ['horde_camps', 'glass_sea'], w: 3,
    text: 'A horde outrider shares your fire without asking — steppe custom — and produces salt, tea, and a silence that is somehow companionable. Eventually: "You fight?" It is not a challenge. It is a census question.',
    choices: [
      { label: '"I fight."', out: { roll: { stat: 'body', dc: 7, win: { cult: 15, karma: 1, log: 'You wrestle by firelight, steppe rules: first shoulder to the grass. You win narrowly, and are immediately adopted into three drinking songs.' }, lose: { cult: 8, log: 'First shoulder to the grass is yours. The outrider helps you up with genuine warmth: losing honestly counts out here.' } } } },
      { label: '"I cultivate."', out: { cult: 10, log: '"Same thing," the outrider says, "slower." You trade breathing methods until the fire dies; theirs smells of horse and works better than it should.' } },
    ],
  },
  // ---- Ancestral echoes: the world remembers your dead ----
  {
    id: 'echo_healer', regions: ['ashfen'], w: 4, once: true, minGen: 2, req: { legacyFlag: 'saved_village' },
    text: (n => n)( 'A young physician stands at your door with a lacquered case and a rehearsed speech that collapses on the second sentence. "My grandmother lived because of your— because your family— " She starts over. "During the Withering Cough. Your line nursed mine. We remember. We have always meant to—" She opens the case: needles, salves, a folio of her own annotations. "House calls. Forever. That’s the debt."'),
    choices: [
      { label: 'Accept the old debt', out: { hpPct: 1, karma: 1, flag: 'family_physician', log: 'She treats your aches, your scars, and one thing you hadn’t mentioned to anyone. The line’s mercy, come home with interest. <i>(A physician owes your family house calls — healing will find you easier in the vale.)</i>' } },
      { label: '"The debt was paid by surviving."', out: { karma: 2, log: 'She argues. You lose, magnificently. She treats you anyway and leaves the folio "as a loan, indefinitely." The vale keeps its accounts in exactly this coin.', hpPct: 1 } },
    ],
  },
  {
    id: 'echo_naga', regions: ['mines'], w: 4, once: true, minGen: 2, req: { legacyFlag: 'naga_friend' },
    text: (n => n)('The impossible pool has moved to meet you again — but this time the coils rise above the water, and the great head regards you with recognition that spans your whole bloodline. It remembers milk and silver. It remembers who kept the old courtesy when keeping it cost. It has been waiting, with the patience of under-rivers, for the line to send someone new.'),
    choices: [
      { label: 'Bow as your ancestor bowed', out: { insight: 'in_old_courtesy', items: { veinbloom: 2 }, karma: 2, log: 'The naga inclines its crowned head — an equal’s greeting, dynasty to dynasty. Veinblooms surface like gifts at a wedding. The old courtesies, it seems, compound.' } },
    ],
  },
  {
    id: 'echo_rival', regions: ['sect', 'cinder_market'], w: 4, once: true, minGen: 2, req: { legacyFlag: 'rival_settled' },
    text: (n => n)('A young cultivator in glass-trimmed robes plants herself in your path, chin high, terrified and hiding it well. "My teacher was Yan Shuo," she says. "He spoke of your ancestor until the day he sat down facing the dawn and did not get up. He said watching is also a path. I am his watching. Show me what he saw."'),
    choices: [
      { label: 'Spar with her, honestly', out: { fight: 'sect_aspirant', winLog: 'She picks herself up glowing like a struck match. "AGAIN," she says — and there it is, the old rivalry, reborn with the polarity flipped. Your houses are bound, apparently, in every generation. There are worse fates.', loseLog: 'She wins, is horrified, apologizes for eleven straight sentences, then asks — hopefully — if you’ll want revenge. Houses bound in every generation. There are worse fates.' } },
      { label: 'Tell her what he was like', out: { cult: 20, karma: 1, log: 'You talk until the lamps burn down: the duels, the lists of weaknesses, the manual pressed into dying hands. She writes none of it down. She will remember every word. Watching is also a path.' } },
    ],
  },
  {
    id: 'echo_slain', regions: ['ashfen'], w: 3, once: true, minGen: 2, req: { legacyFlag: 'died_fighting' },
    text: (n => n)('The shrine keeper finds you at your predecessor’s tablet. "They went out fighting," she says. "The vale remembers it differently than the family does, you know. To you it’s grief. To every child here it’s the reason they sleep well: someone from that house is always standing between us and the dark." She sets a fresh lamp at the tablet. "No pressure," she adds, dishonestly.'),
    choices: [
      { label: 'Sit with the tablet awhile', out: { insight: 'in_grief', cult: 30, log: 'The lamp burns. The name stays. Something in you sets, like a bone healing straight, exactly where it broke.' } },
    ],
  },
  // ---- The guttering of the Third Ember: a sect crisis ----
  {
    id: 'ember_gutters', regions: ['sect'], w: 5, once: true, minRealm: 2,
    text: (n => n)('Alarm-gongs, at the wrong hour. Disciples run past with braziers, faces gray. Sister Iron Lotus grabs your shoulder: "The Third Ember is guttering. Six centuries of tending and it’s guttering — something in the deep veins is drinking its draft. The elders are sealing the summit. We need cultivators in the vents. Now."'),
    choices: [
      { label: 'Go down into the vents', out: { fight: 'hollow_acolyte', winFlag: 'ember_crisis', winLog: 'In the vent-dark you find them: hollow things, mouths to the vein like leeches, drinking the Ember’s draft. You burn them out. Above you, faintly, six hundred years of flame steadies — but these were strays. Something sent them. The elders will want to hear all of it.', loseLog: 'The vent-things overwhelm you; disciples drag you out by the ankles. The Ember still gutters. This is not over.' } },
      { label: 'This is not your fire', out: { karma: -2, log: 'You walk down the ten thousand steps against a river of running disciples. Whatever the Order is to you, tonight it learned exactly what you are to it.' } },
    ],
  },
  {
    id: 'ember_restored', regions: ['sect'], w: 5, once: true, req: { flag: 'ember_crisis' },
    text: (n => n)('The elders receive you beneath the steadied Third Ember — six centuries of flame, breathing easy again because you went into the dark for it. The eldest, awake for once, studies you for a long moment. "The Order pays its debts," he says. "Choose: our scriptures, our stores, or our word."'),
    choices: [
      { label: 'The scriptures', out: { tech: 'boneforge', insight: 'in_vow_kept', log: 'A technique of the inner halls, and — rarer — the librarian’s respect.' } },
      { label: 'The stores', out: { contrib: 300, stones: 200, log: 'Contribution enough to shop the inner shelves, and a purse besides. The quartermaster salutes you, which he does for no one.' } },
      { label: 'The word', out: { karma: 4, flag: 'order_debt', log: 'The Order owes your line a favor, formally, in the ledger that elders sign. Whole sects have been founded on less. <i>(The Order’s debt is written — it will matter.)</i>' } },
    ],
  },
  {
    id: 'ashsnow_night', regions: ['ashfen', 'greypine', 'mines', 'cinder_market', 'sect', 'glasswaste', 'temple', 'crater', 'riverport', 'serpents_spine', 'lotus_marsh', 'bone_orchard', 'saltglass', 'sunken_star', 'leviathan_graves', 'horde_camps', 'ziggurat', 'glass_sea'], w: 1,
    text: 'Ash-snow falls all night — soft gray flakes that were forest, or city, or someone, once. The whole world goes quiet under it.',
    choices: [
      { label: 'Meditate in the falling ash', out: { cult: 22, log: 'Grief and Emberlight, it turns out, condense at the same temperature.' } },
      { label: 'Sleep through it', out: { hpPct: 1, log: 'You sleep like the dead and wake like the living. Full health.' } },
    ],
  },
];

export const SEASONS = ['Spring', 'Summer', 'Autumn', 'Winter'];

// ---------------- The Living World ----------------

// Weather rolls each season; small mechanical bite, big texture.
export const WEATHER = {
  0: [ // Spring
    { id: 'ashrain', name: 'Ash-rain', desc: 'Warm rain the color of slate. The mines weep; tunnels grow treacherous.', fx: { mineHazard: 0.15 } },
    { id: 'greenwind', name: 'Green wind', desc: 'A wind that remembers forests. Herbs push up early.', fx: { forageBonus: 1 } },
    { id: 'palefog', name: 'Pale fog', desc: 'Fog thick enough to lean on. Travelers vanish politely into it.', fx: { ambush: 0.1 } },
  ],
  1: [ // Summer
    { id: 'searing', name: 'Searing haze', desc: 'The buried embers breathe through the soil. Meditation comes easier; tempers do not.', fx: { medBonus: 0.15 } },
    { id: 'glasswind', name: 'Glass wind', desc: 'Glittering wind off the waste. The dune roads close; glass goods grow scarce.', fx: { priceUp: 'material' } },
    { id: 'stillheat', name: 'Dead-still heat', desc: 'Not a leaf moves. Beasts lie up in shade, irritable and hungry.', fx: { beastAtk: 1.1 } },
  ],
  2: [ // Autumn
    { id: 'harvestglow', name: 'Harvest glow', desc: 'The veins run bright before winter. Markets groan with goods.', fx: { priceDown: 'herb' } },
    { id: 'crowsky', name: 'Crow-dark sky', desc: 'Crows gather on every roofline, auditing the living.', fx: { omen: true } },
    { id: 'firstfrost', name: 'First frost', desc: 'Frost licks the ember-veins into fog. Cold seeps into old wounds.', fx: { woundAche: true } },
  ],
  3: [ // Winter
    { id: 'ashsnow', name: 'Ash-snow', desc: 'Gray snow that was forest, or city, or someone, once.', fx: { medBonus: 0.1 } },
    { id: 'ironcold', name: 'Iron cold', desc: 'Cold that rings like a struck anvil. Provisions vanish twice as fast.', fx: { hungerUp: true } },
    { id: 'lampglow', name: 'Lamp-glow calm', desc: 'Still, clear nights. Every window holds a flame against the dark.', fx: {} },
  ],
};

// Named people. rel 0-5; milestones unlock perks. Gifts they favor deepen ties fast.
export const NPCS = {
  granny_ash: {
    name: 'Granny Ash', title: 'shrine keeper of Ashfen', region: 'ashfen', gift: 'duskpetal',
    desc: 'She has buried three generations of your family and swept the shrine for all of them.',
    perk: 'At close kinship she treats one wound each season, free, scolding included.',
    lines: [
      '"Sweep first, questions after." She hands you the shrine broom like a sword.',
      '"Your people were stubborn. Good stubborn, mostly. The Wheel keeps score either way."',
      '"Karma isn’t a purse, child. It’s a slope. You’re always walking one way or the other."',
      '"When the lamps gutter, feed them. When your heart gutters — same medicine."',
      '"I knew your line when it was one scared orphan with a spark. Look at it now. Look at you."',
    ],
  },
  old_bo: {
    name: 'Old Bo', title: 'the village smith', region: 'ashfen', gift: 'vein_ore',
    desc: 'Forearms like anchor chain. He talks to iron more gently than to people.',
    perk: 'At close kinship his weapon prices drop by a fifth, "family rate."',
    lines: [
      '"Buy something or pump the bellows. Standing’s free everywhere else."',
      '"Vein ore’s honest. It doesn’t pretend to be anything but heavy."',
      '"My daughter Bo Yun went east to the Order. Sends letters. Short ones. Like her mother."',
      '"A blade’s a promise you make your own hand. Keep it clean."',
      '"You swing like your grandmother. That’s a compliment, mind."',
    ],
  },
  sister_lotus: {
    name: 'Sister Iron Lotus', title: 'senior disciple of the Kindled Path', region: 'sect', gift: 'suncap',
    desc: 'She once held the mountain gate alone for three days. She teaches footwork now, and mercy, in that order.',
    perk: 'At close kinship the mission board pays you a fifth more contribution.',
    lines: [
      '"Stand straighter. The mountain is watching and it gossips."',
      '"The Order keeps the Third Ember. The Third Ember keeps us. Circles, disciple. Everything true is a circle."',
      '"Yan Shuo asked after you. He made it sound like he wasn’t asking. He was asking."',
      '"Strength that doesn’t shelter something is just weather with a name."',
      '"When I go to the Crater at last, I’d be glad if it were your generation holding the gate."',
    ],
  },
  nine_fingers: {
    name: 'Nine-Fingers', title: 'auction master of Riverport', region: 'riverport', gift: 'star_iron',
    desc: 'Lost the tenth finger verifying a cursed lot. Considers it tuition. Reads a room the way scholars read scripture.',
    perk: 'At close kinship the house fee vanishes from your winning bids — "family rate, and I never had family."',
    lines: [
      '"Bid with your head. The paddle is connected to it, in theory."',
      '"Everything here was someone’s treasure. Auctions are just grief with a bell."',
      '"The trick isn’t knowing what a thing is worth. It’s knowing what the person behind you thinks it’s worth."',
      '"I once sold a sealed box to three different buyers. Long story. Short trial."',
      '"You bid like my old master. He died rich, which is the only way he’d have accepted."',
    ],
  },
  captain_rema: {
    name: 'Captain Rema', title: 'harbor-mother of Saltglass', region: 'saltglass', gift: 'beast_core',
    desc: 'Runs the deepest dive crews on the coast. Half her hair went white in the drowned sun’s glow; she keeps it as a credential.',
    perk: 'At close kinship her crews haul an extra ingot of star-iron from every dive you join.',
    lines: [
      '"Depth-pay is triple. Grief-pay is included. Sign here."',
      '"The Fourth Sun isn’t dead down there. It’s waiting. Waiting’s worse."',
      '"Tide-law: what the sea gives back belongs to who it’s given to. Even the sea keeps accounts."',
      '"I’ve buried forty divers and married two. Similar paperwork, honestly."',
      '"You’ve got deep-water nerves, landborn. If the Wheel ever spits you out, there’s a berth here."',
    ],
  },
  khans_shadow: {
    name: 'The Khan’s Shadow', title: 'left hand of the Banner', region: 'horde_camps', gift: 'beast_core',
    desc: 'Nobody has heard the Khan speak in nine years. Nobody has needed to. The Shadow translates silence into policy.',
    perk: 'At close kinship the arena’s healers tend you between bouts — the Banner protects its own.',
    lines: [
      '"The Khan sees you." (The Khan is three miles away.) "The Khan sees everything on the steppe. It’s a whole thing."',
      '"We measure worth in the arena because everything else can be inherited."',
      '"The Ziggurat was here before the grass. The grass is very old. Draw your own conclusions."',
      '"Asura blood isn’t rage. It’s clarity about what rage is for."',
      '"The Banner remembers its friends. You would not believe how literal that is."',
    ],
  },
  merchant_shen: {
    name: 'Merchant Shen', title: 'broker of the Cinder Market', region: 'cinder_market', gift: 'glasswing',
    desc: 'Smiles like a ledger balancing. Knows what everything costs, including the question.',
    perk: 'At close kinship he opens his private stock — rarities the open stalls never see.',
    lines: [
      '"First rule: everything’s for sale. Second rule: never say the first rule aloud. You didn’t hear either."',
      '"Prices drift like weather. I merely sell umbrellas."',
      '"A naga in the deep mines, they say. Milk and silver, they say. Sellers of milk and silver say it loudest."',
      '"You have the look of someone acquiring a reputation. Those are expensive to keep and worse to lose."',
      '"For you? The back room. Mind the idol — it bites appraisers."',
    ],
  },
};

// Aspects: awakened at the first breakthrough. Choose one of two drawn.
export const ASPECTS = {
  ember_eyed:   { name: 'Ember-Eyed', desc: 'Your pupils hold a mote of the old suns. +8% critical chance; you read killing intent a heartbeat early.', fx: { crit: 8 } },
  ash_blooded:  { name: 'Ash-Blooded', desc: 'Your blood runs warm and gray. One lingering wound knits itself closed at each season’s end.', fx: { autoHealWound: true } },
  dream_walker: { name: 'Dream-Walker', desc: 'You walk your dreams shod. Rewards drawn from past-life dreams are doubled.', fx: { dreamDouble: true } },
  iron_boned:   { name: 'Iron-Boned', desc: 'Your bones took the tempering meant for a temple bell. +1 Body; foraging dens and rockfalls barely scratch you.', fx: { body: 1, hazardHalf: true } },
  vein_whisperer:{ name: 'Vein-Whisperer', desc: 'The buried light knows your name. +20% Emberlight from meditation in mines and crater.', fx: { veinMed: 0.2 } },
  karmic_sight: { name: 'Karmic Sight', desc: 'You see the Wheel’s ledger plainly — your karma is revealed, and fate checks tilt slightly your way.', fx: { seeKarma: true, checkBonus: 1 } },
};

// Flaws: the Wheel's price. One is assigned, unchosen, at awakening.
export const FLAWS = {
  hollow_appetite:{ name: 'Hollow Appetite', desc: 'Something in you eats first. You consume double provisions each season.', fx: { doubleFood: true } },
  bleeding_ledger:{ name: 'Bleeding Ledger', desc: 'A debt from a life you don’t remember. 8 stones vanish from your purse each season, collected.', fx: { tax: 8 } },
  moonstruck:   { name: 'Moonstruck Sleep', desc: 'The moon keeps reading over your shoulder. Rest restores a third less.', fx: { restPenalty: 0.33 } },
  heaven_marked:{ name: 'Heaven-Marked', desc: 'The sky remembers your face from somewhere, and it isn’t fond. Tribulations hurl two extra bolts.', fx: { extraWaves: 2 } },
  thin_meridians:{ name: 'Thin Meridians', desc: 'Your channels are silk where others have rope. Techniques cost 15% more Emberlight.', fx: { qiCost: 1.15 } },
  loud_fate:    { name: 'Loud Fate', desc: 'Your destiny is audible. Trouble finds you: ambushes and dens stir more often.', fx: { ambush: 0.12 } },
};

// Vows (vrata): tapas — austerity traded for power. Break them and the Wheel notices.
export const VOWS = {
  silence: { name: 'Vow of Silence', seasons: 2, desc: 'Speak to no one, buy nothing, sell nothing. The inner voice grows deafening: +35% meditation while held.',
    forbids: ['talk', 'market', 'listings'], reward: { log: 'The vow completes. Words return like birds to a field — and your breath is deeper for their absence.' , karma: 2 }, breakPenalty: { karma: -5 } },
  fast:    { name: 'Fast of Embers', seasons: 1, desc: 'Take no food for a season, feeding the inner flame instead. Painful, purifying: +25 cultivation and merit on completion.',
    forbids: [], noFood: true, hpCost: 0.15, reward: { cult: 25, karma: 3, log: 'The fast ends. You are lighter by a season’s meals and heavier by something the scales can’t weigh.' }, breakPenalty: { karma: -4 } },
  vigil:   { name: 'Iron Vigil', seasons: 2, desc: 'Do not rest. Sit the nights out with a straight spine. The body, insulted, hardens: +1 Body on completion.',
    forbids: ['rest'], reward: { stat: 'body', log: 'The vigil ends at dawn on the last day. Your spine has opinions now, all of them iron.' }, breakPenalty: { karma: -4 } },
};

// Lingering wounds — combat and hazards leave marks that stay until treated.
export const WOUNDS = {
  cracked_rib:  { name: 'Cracked Rib', desc: 'Every breath files a complaint.', fx: { body: -1 } },
  torn_meridian:{ name: 'Torn Meridian', desc: 'Emberlight leaks where it should flow.', fx: { spirit: -1 } },
  ash_blind:    { name: 'Ash-Blind Eye', desc: 'One eye full of gray weather.', fx: { crit: -10 } },
  fevered_blood:{ name: 'Fevered Blood', desc: 'Thoughts arrive slightly boiled.', fx: { mind: -1 } },
};

// Past-life dreams: the Wheel showing its old turnings. Trigger on rest and season's end.
export const DREAMS = [
  { id: 'drowned_king', text: 'You dream you are a king in a drowned era, standing on a palace roof as the water climbs the stairs, calm as a clerk. Your court waits for your last decree.',
    choices: [
      { label: 'Decree that the archives be saved before the gold', out: { roll: { stat: 'mind', dc: 6, win: { cult: 18, karma: 2, log: 'The scribes row away with the kingdom’s memory. You wake with borrowed clarity — some of a dead king’s learning stays.' }, lose: { dreamScar: true, log: 'The water takes archives and gold alike. You wake with the taste of ink and salt.' } } } },
      { label: 'Save the gold', out: { stones: 15, karma: -1, log: 'You wake clutching real coins that were not under your pillow last night. Best not to ask.' } },
    ] },
  { id: 'black_river', text: 'A black river, and a ferryman who is only a hat and two patient hands. He does not ask for coin. He asks what you weigh.',
    choices: [
      { label: 'Answer honestly', out: { revealKarma: true, log: 'The ferryman weighs you with a look and tells you, precisely, the balance of your account with the world. You wake knowing it.' } },
      { label: 'Refuse the scales', out: { cult: 8, log: '"Later, then," says the hat. The river carries you back to morning.' } },
    ] },
  { id: 'churning', text: 'Gods and asuras grip a serpent like a rope and churn a sea of milk-white fire. Someone hands you a length of the serpent. It is warm, and it is your family’s hearth-line, generations long.',
    choices: [
      { label: 'Pull with the gods', out: { roll: { stat: 'body', dc: 7, win: { cult: 20, karma: 1, log: 'The sea yields a drop of something older than sunlight. You swallow it before anyone objects. You wake stronger-channeled.' }, lose: { dreamScar: true, log: 'The serpent-rope burns your palms. You wake gripping your own blanket like a lifeline.' } } } },
      { label: 'Pull with the asuras', out: { roll: { stat: 'body', dc: 7, win: { cult: 20, karma: -2, log: 'The asuras cheer you like a brother. Something potent and unsanctioned splashes your lips. You wake stronger, and slightly ashamed.' }, lose: { dreamScar: true, log: 'The gods notice which side you chose. The dream ends abruptly, like a door.' } } } },
    ] },
  { id: 'lamp_extinguisher', text: 'A figure moves down an endless corridor of lamps, pinching each flame out with wet fingers. It has not seen you. Each lamp, you understand, is a year of someone’s life.',
    choices: [
      { label: 'Relight the lamps behind it', out: { roll: { stat: 'fate', dc: 8, win: { karma: 3, log: 'You cup sparks back into a dozen wicks before the figure turns. Somewhere, strangers wake from fevers. You wake with clean hands.' }, lose: { dreamScar: true, log: 'Its wet fingers close on your ember too — briefly. You wake gasping, whole, but cold in one chamber of your heart.' } } } },
      { label: 'Follow it in silence, learning', out: { cult: 15, karma: -1, log: 'You watch how a life is put out: gently, like correcting an error. Terrible knowledge, efficiently gained.' } },
    ] },
  { id: 'past_wheel', minGen: 2, text: 'You dream a supper table you have never sat at, yet you know every knot in its wood. Your predecessor ladles soup, unsurprised to see you. "The Wheel seats us together, once each turning."',
    choices: [
      { label: 'Ask what they regret', out: { cult: 15, log: '"Hoarding," they say at once. "Stones, pills, apologies. Spend everything before the Wheel does." You wake resolved.' } },
      { label: 'Just eat together', out: { hpPct: 1, qiPct: 1, log: 'The soup tastes of a kitchen you were born too late for. You wake completely rested, completely whole.' } },
    ] },
  { id: 'garuda_shadow', text: 'You dream from above — riding, or perhaps being, an eagle the size of a province. Below, the ember-veins spell a word in a language of light. One more wingbeat and you could read it.',
    choices: [
      { label: 'That wingbeat', out: { roll: { stat: 'spirit', dc: 8, win: { cult: 25, log: 'The word is a name. Yours, or the world’s — in the dream they are briefly the same. You wake with your channels ringing.' }, lose: { dreamScar: true, log: 'The height notices you noticing. You wake mid-fall, knuckles white.' } } } },
      { label: 'Land before you learn too much', out: { qiPct: 0.5, log: 'Wisdom is sometimes a refused staircase. You wake calm, Emberlight pooled and quiet.' } },
    ] },
];

// Rivals: the Roll of Names. They cultivate whether you watch or not.
export const RIVALS = [
  { id: 'yan_shuo', name: 'Yan Shuo', talent: 1.35, epithet: 'the Glass Prodigy' },
  { id: 'bo_yun', name: 'Bo Yun', talent: 1.0, epithet: 'the Smith’s Daughter' },
  { id: 'brother_cinder', name: 'Brother Cinder', talent: 1.15, epithet: 'of the Kindled Path' },
  { id: 'widow_ash', name: 'The Ash Widow', talent: 0.9, epithet: 'who buried three husbands and one sect' },
];

export const NEWS_TEMPLATES = {
  rival_up: (r, realm) => `Word travels: ${r.name}, ${r.epithet}, has broken through to ${realm}. Teahouses argue about the details.`,
  price_up: (cat) => `Traders mutter: ${cat} prices climb — caravans thin on the roads.`,
  price_down: (cat) => `A glut in the market: ${cat} goods go cheap. Merchant Shen looks personally offended.`,
  sect: [
    'The Order of the Kindled Path posts new bounties; the mission board creaks.',
    'An elder of the Order enters seclusion. His students water his plants with ceremony.',
    'Rumor: the Order turned away a prince this week. The prince is telling everyone it was mutual.',
  ],
  world: [
    'A vein-tremor in the deep mines. Miners refuse the fourth shaft again, knocking three times at each crossing.',
    'Pilgrims pass through, walking the old Wheel-road to the Crater rim. They accept water, refuse questions.',
    'A star fell past the northern ridge. By law, falling light belongs to whoever reaches it — three parties already left.',
    'The shrine lamps burned blue for a night. Granny Ash called it "the ancestors clearing their throats."',
  ],
};

export const FOOD_ID = 'provisions';

// ---------------- Insights: understanding earned by living ----------------
// Each insight held speeds all cultivation (+6% each) and every third grants
// +1 bolt endured at tribulations. They come only from the world, never the mat.
export const INSIGHTS = {
  in_first_blood:   { name: 'First Blood', desc: 'The gap between practicing a strike and needing one.', hint: 'Win your first real fight.' },
  in_apex:          { name: 'The Shape of an Apex', desc: 'You watched something ancient fight with everything it had. Now you know what everything looks like.', hint: 'Slay any apex beast.' },
  in_deep_dream:    { name: 'What the Wheel Showed', desc: 'A truth carried back across the border of sleep.', hint: 'Prevail in a dream of the Wheel.' },
  in_vow_kept:      { name: 'The Taste of Iron', desc: 'Power taken from yourself is owned.', hint: 'Keep any vow to its end.' },
  in_sky_survived:  { name: 'The Sky’s Grammar', desc: 'Lightning has rules. You have read them at close range.', hint: 'Survive a tribulation.' },
  in_kinship:       { name: 'A Second Fire', desc: 'Warmth that isn’t yours will still warm you. This is somehow not a metaphor.', hint: 'Become close kin to anyone.' },
  in_far_road:      { name: 'The Size of the World', desc: 'You have stood where your map ends and kept walking.', hint: 'Leave your home domain.' },
  in_immaculate:    { name: 'The Furnace’s Consent', desc: 'Perfection isn’t forced from the fire. It is negotiated.', hint: 'Refine an Immaculate pill.' },
  in_near_death:    { name: 'The Width of a Hair', desc: 'You have seen exactly how little was left. It was enough. Remember that.', hint: 'Survive a defeat.' },
  in_old_courtesy:  { name: 'The Old Courtesies', desc: 'Some powers predate cultivation. They keep accounts in milk and silver.', hint: 'Honor something ancient.' },
  in_hammer:        { name: 'The Hammer’s Argument', desc: 'Won at auction: everything is worth what someone will not let it go for.', hint: 'Win a bidding war.' },
  in_arena_dust:    { name: 'Arena Dust', desc: 'Ten thousand people watched you refuse to fall.', hint: 'Win the Scarred Arena.' },
  in_grief:         { name: 'What the Fire Keeps', desc: 'Grief, in your family, is a form of fuel.', hint: 'Inherit the Hearthline.' },
  in_rivals_eyes:   { name: 'A Rival’s Eyes', desc: 'Someone strong has studied you and come back anyway. You must be worth studying.', hint: 'Defeat a named rival.' },
};

// ---------------- Dao paths: what you cultivate toward ----------------
export const DAOS = {
  sword:   { name: 'The Sword Dao', glyph: '劍', desc: 'The world is a knot; you are the edge. +12% technique damage, +6% crit, and the Verse of Edges.',
    fx: { dmg: 0.12, crit: 6 }, tech: 'verse_of_edges' },
  furnace: { name: 'The Furnace Dao', glyph: '鼎', desc: 'All things refine. Pills act 25% stronger, your grades climb a tier easier, and the furnace teaches the Kiln-Heart Breath.',
    fx: { pill: 0.25, grade: 0.08 }, tech: 'kiln_heart_breath' },
  wheel:   { name: 'The Wheel Dao', glyph: '輪', desc: 'Karma is a current; you learn to row. Karmic gains doubled, dream rewards +50%, and the Turning Palm.',
    fx: { karma: 2, dream: 0.5 }, tech: 'turning_palm' },
  hearth:  { name: 'The Hearth Dao', glyph: '灶', desc: 'The line is the weapon. +50% Hearthflame earned, heirs inherit an extra insight, and the Emberkeeper’s Ward.',
    fx: { hearthflame: 0.5, heirInsight: 1 }, tech: 'emberkeeper_ward' },
  void:    { name: 'The Void Dao', glyph: '虛', desc: 'Absence is a place; you have an address there. +12% dodge, fleeing always works, and the Hollow Step.',
    fx: { dodge: 12, flee: true }, tech: 'hollow_step' },
};

export const NPC_ARCS = {
  granny_ash: [
    { at: 4, text: 'Granny Ash finally lets you into the shrine’s back room: a wall of tablets, one per keeper, going back past legibility. "Mine goes there," she says, pointing at a gap. "Yours — well. Your family doesn’t get tablets. You get the whole vale for a tablet. Heavier, if you ask me." She teaches you the keeper’s sweeping-form, which is, you realize halfway through, a breathing art older than the Order.', out: { cult: 25, insight: 'in_old_courtesy' } },
    { at: 8, text: 'You find Granny Ash on the shrine steps at dusk, looking her age for once. "I buried your predecessor," she says. "And the one before. I’d like, just once, to bury nobody. Do an old woman a favor: come home from wherever you’re going." She presses a plain clay lamp into your hands. It never quite goes out, you will find, no matter the wind.', out: { items: { hearth_ring: 1 }, log: '<b>Granny’s clay lamp</b> — she gives you the Hearth Ring her own line kept. "It remembers everyone," she says. "Now it can remember you."' } },
    { at: 12, text: 'On the night of the lamps, Granny Ash names you shrine-kin before the whole village — an office that hasn’t been granted in four generations. "When the Tenth Dawn comes," she says, loud enough for the ancestors, "this vale will say: we kept that fire. Every one of us. Now sweep. Shrine-kin sweep."', out: { karma: 5, cult: 40 } },
  ],
  sister_lotus: [
    { at: 4, text: 'Sister Iron Lotus takes you to the gate she held alone for three days. The stone still bears the marks. "Everyone asks how," she says. "Wrong question. The question is what I was holding it FOR. Answer that about yourself and your footwork will fix itself." Infuriatingly, she is right.', out: { cult: 25 } },
    { at: 8, text: 'Lotus shows you a letter, worn soft with rereading: her acceptance into the Order, signed by an elder long ascended. "I was a charcoal-burner’s daughter. The Path doesn’t care where the wood came from — only that it burns. You burn well, disciple. I have said so where it matters." Your next rank will come easier; more importantly, so will the one after.', out: { contrib: 120 } },
    { at: 12, text: 'On the ten-thousandth step, Lotus stops. "When I go to the Crater at last, I want it to be your generation holding the gate. I have told the elders so. Formally." She hands you her own brazier — the one she has carried up this mountain every day for thirty years. It is heavier than it looks. Everything she owns is.', out: { tech: 'charwall', karma: 2, log: '<b>Lotus’s brazier</b> — carrying it up the steps once teaches you more about the Charwall than any manual.' } },
  ],
  captain_rema: [
    { at: 4, text: 'Rema takes you out on the evening tide to the buoy line, where the drowned sun’s glow comes up through forty fathoms like a hearth seen through smoked glass. "Every diver wants to touch it once," she says. "The ones I keep are the ones who can want it and not do it." She watches you want it and not do it. You pass.', out: { cult: 25 } },
    { at: 8, text: 'A storm takes a dive-crew’s line and Rema goes in after them herself, and you go in after her, because apparently that is who you are now. Between you, everyone comes up. On the dock, soaked and furious and alive, she laughs for the first time in your hearing. "Landborn," she says, "you’ll do."', out: { insight: 'in_near_death', items: { star_iron: 2 } } },
    { at: 12, text: 'Rema has a berth built for you on her own boat — a plank with your name burned in, which under tide-law makes you crew, which under tide-law makes you family. "The sea gives back what it owes," she says. "Eventually. To somebody. Sail with me and be there when it does."', out: { items: { tidebreaker_harpoon: 1 }, log: '<b>Crew of the harbor-mother’s own boat.</b> The Tidebreaker Harpoon is yours by tide-law: salvage, honestly witnessed.' } },
  ],
  khans_shadow: [
    { at: 4, text: 'The Shadow lets you stand watch with them outside the Khan’s tent — a silence with someone in it, which on the steppe is intimacy. Near dawn they say: "The Khan asked your name." That is all. On the Burning Steppe, that is a great deal.', out: { cult: 25 } },
    { at: 8, text: 'A challenge circle forms around some insult you didn’t see. The Shadow steps in beside you, uninvited, and the circle dissolves like salt in rain. "The Banner remembers its friends," they say. "You are watched for, now. Try to make it inconvenient for us. We enjoy that."', out: { insight: 'in_kinship' } },
    { at: 12, text: 'The Khan’s tent opens for you. Inside: quiet, warmth, and a man older than the stories, who looks at you for a long time and nods once. The Shadow, outside, translates the nod: "He says the grass will remember your name. He does not say that. He has not said that in nine years." ', out: { items: { khan_standard: 1 }, karma: 3, log: '<b>The Khan’s nod.</b> A second Lesser Standard is folded into your hands — one for your line, distinct from any arena prize.' } },
  ],
  merchant_shen: [
    { at: 4, text: 'Shen shows you his private ledger — not the numbers, the margins: tiny notations of who was desperate, who was lying, who wept. "Everything here was someone’s treasure," he says. "A broker who forgets that becomes a thief with paperwork. I forget nothing." It is the closest thing to a confession you will get from him.', out: { cult: 20 } },
    { at: 8, text: 'A ruined family comes to sell their shrine lamp — the last thing they own that matters. You watch Shen pay four times its worth without letting them see the arithmetic, then sell it back to them for one copper "pending recovery of the market." He catches you watching. "You saw nothing," he says. "My rates depend on it."', out: { karma: 2, insight: 'in_hammer' } },
    { at: 12, text: 'Shen offers you a partnership — not in goods; in information. "You walk through every domain under this sky. I sit in the middle of every rumor. Between us we could know almost everything worth knowing." He is right, and hereafter, the news reaches you before it reaches the roads.', out: { log: '<b>Partner of the back room.</b> Shen’s pigeons find you anywhere: from now on, word reaches you a season early.', flag: 'shen_network' } },
  ],
  nine_fingers: [
    { at: 4, text: 'Nine-Fingers teaches you to read an auction room: the man touching his ear is at his limit, the woman studying the ceiling has orders not to lose, the calm butler is the one to fear. "The lot is never the game," he says. "The room is the game."', out: { cult: 20 } },
    { at: 8, text: 'He shows you the tenth finger — kept, mummified, in a lacquer box, wearing a ring you don’t look at twice on purpose. "Tuition," he says. "The lesson: verify everything, and price your own flesh honestly, because someone else already has." He gifts you the ring after all. You look at it now. It’s star-iron.', out: { items: { star_iron: 1 }, insight: 'in_hammer' } },
    { at: 12, text: '"Family rate," he announces at the next auction, publicly, tapping the bell — and the whole room recalculates you: someone the house itself has adopted. Grief with a bell, he called this place once. He never mentions what he lost to it. He looks at you sometimes as if you were the refund.', out: { log: '<b>The house’s own.</b> Nine-Fingers waives your fees forever and holds the good lots back for your eyes first.' } },
  ],
  old_bo: [
    { at: 4, text: 'Old Bo lets you work the bellows on a real commission — a plow, which disappoints you until you watch him treat it with more care than any sword. "Swords feed nobody," he says. "Remember what the iron is FOR and it forgives your mistakes."', out: { cult: 20 } },
    { at: 8, text: 'Bo shows you a letter from his daughter Bo Yun — short, like her mother’s. It says the Order has posted her to the border. He reads it four times while you pretend to study the forge. "Take a look at her name on the Roll when you’re out there," he says gruffly. "Tell me if the teahouses say it right."', out: { insight: 'in_kinship' } },
    { at: 12, text: 'Bo forges you a blade over three nights without being asked, quenches it in the warm stream where the ember-vein runs, and hands it over with the bill: one copper. "Family rate. Original meaning." You will bury this man someday, you realize, or he you; the vale keeps its accounts in exactly this coin.', out: { items: { emberglass_saber: 1 }, karma: 2 } },
  ],
};

// ---------------- Region discoveries: the world remembers being explored ----------------
export const DISCOVERIES = [
  { id: 'hunters_cache', region: 'greypine', name: 'The Hunters’ Cache', chance: 0.2,
    found: 'Behind three cuts in the bark — turn back after dark — a hollow trunk: the old hunters’ cache, restocked by custom for whoever keeps the woods.',
    action: { label: 'The Hunters’ Cache', desc: 'Take the woods’ tithe; leave something when you can. (once a season)', ap: 0 } },
  { id: 'warm_vein', region: 'mines', name: 'The Warm Vein', chance: 0.2,
    found: 'A side-gallery the maps mark as collapsed. It isn’t. The vein here runs shallow and warm as a sleeping animal — miners keep it secret for naps and prayers.',
    action: { label: 'The Warm Vein', desc: 'Meditate against living ember-light. (+25% here)', ap: 1 } },
  { id: 'herons_shrine', region: 'lotus_marsh', name: 'The Heron’s Shrine', chance: 0.25,
    found: 'An islet with a shrine no sect keeps: a heron carved in blackwood, older than the marsh’s name. Offerings still appear. Something still accepts them.',
    action: { label: 'The Heron’s Shrine', desc: 'Leave an offering; the marsh’s luck leans your way. (once a season)', ap: 0 } },
  { id: 'gate_stone', region: 'sect', name: 'The Held Gate', chance: 0.25,
    found: 'The gate Sister Lotus held. The stone remembers: grip-marks worn in granite. Disciples touch them for courage before trials. Now you know where they are.',
    action: { label: 'The Held Gate', desc: 'Set your hands where hers were. Steel before a challenge. (once a season)', ap: 0 } },
  { id: 'glass_garden', region: 'glasswaste', name: 'The Glass Garden', chance: 0.2,
    found: 'A dell where the storm-winds cross: sand fused into flowers, acres of them, ringing faintly. Glassreed grows here like weeds in a graveyard.',
    action: { label: 'The Glass Garden', desc: 'Harvest the singing flowers. (once a season)', ap: 1 } },
  { id: 'star_pool', region: 'sunken_star', name: 'The Star Pool', chance: 0.25,
    found: 'A tide-pool the drowned sun reaches through some vein of the seafloor: knee-deep water, forty fathoms of light. Divers call it the Shallows of the Deep and tell no one.',
    action: { label: 'The Star Pool', desc: 'Meditate in drowned dawnlight. (+25% here)', ap: 1 } },
];

// ---------------- Branching endings ----------------
export const ENDINGS = {
  kindle: { name: 'The Tenth Dawn', key: 'the_dawn' },
  share:  { name: 'The Shared Dawn', key: 'the_shared_dawn' },
  refuse: { name: 'The Kept Flame', key: 'the_kept_flame' },
  usurp:  { name: 'The Black Sunrise', key: 'the_black_sunrise' },
};

// ---------------- The household: family vocations with real effects ----------------
export const VOCATIONS = {
  smith:    { name: 'Smith', desc: 'Iron in the family. Weapon prices and forge-foldings cost a fifth less in the vale.', line: 'hammers something honest before dawn; the sound is a kind of clock' },
  herbalist:{ name: 'Herbalist', desc: 'Someone keeps the drying racks full: +1 herb arrives each season.', line: 'leaves bundles on your sill, labeled in a hand that brooks no argument' },
  hunter:   { name: 'Hunter', desc: 'The pot is never empty: +1 provisions every second season.', line: 'comes back smelling of pine and blood, and the larder grows' },
  disciple: { name: 'Sect Disciple', desc: 'Letters home carry weight: +8 contribution each season once you join the Order.', line: 'writes short letters from the mountain; the honorifics keep changing' },
  merchant: { name: 'Merchant', desc: 'The family knows what things cost: all market prices −8%.', line: 'audits your purchases at supper, wincing theatrically' },
  keeper:   { name: 'Shrine-keeper', desc: 'The lamps are never dark: +1 karma each year, +4 Hearthflame at every succession.', line: 'sweeps the shrine at dusk; the ash never settles on your name' },
};

export const NAME_POOL = [
  'Wei Ashdown', 'Han of the Hollow', 'Suo Lin', 'Old Cinder’s Daughter', 'Bram Veinborn', 'Yu the Younger',
  'Marrow Chen', 'Sable Ashfen', 'Petal-of-Dusk', 'Iron Mei', 'Kestrel Bo', 'Ash-Between-Hills',
];

export const DIFFICULTIES = {
  ember: { name: 'An Ember Year', glyph: '燭',
    desc: 'The Wheel is kind, this turning. A gentle village birth, a full larder, a little luck sewn into your swaddling. Foes strike softer.',
    fx: { stones: 90, provisions: 3, fate: 1, enemyAtk: 0.9, regions: ['ashfen'] } },
  ash:   { name: 'An Ash Year', glyph: '灰',
    desc: 'The honest Wheel: you are born where you are born, with what your family had. The world as it is.',
    fx: { stones: 30, provisions: 2, enemyAtk: 1.0, regions: ['ashfen', 'greypine'] } },
  iron:  { name: 'An Iron Year', glyph: '鐵',
    desc: 'Born in a famine winter, in a hard place, with an old injury and ten stones to your name. Foes hit harder. The chronicle of anyone who survives this is worth reading.',
    fx: { stones: 10, provisions: 1, enemyAtk: 1.12, hungry: true, season: 3, wound: true, regions: ['ashfen', 'greypine'] } },
};

// ---------------- The Chronicle: story beats ----------------
// The game writes itself as a novel. Each beat fires once per Hearthline,
// is shown as a book page, and is kept forever in the Chronicle.
export const STORY_BEATS = [
  {
    id: 'prologue', book: 'Book One — ASH', title: 'The Hollow Between Hills',
    when: (s) => s.meta.generation === 1,
    text: (s) => `They say that when the Ninth Sun fell, it took eleven days to die, and that the people of the vale stood on their roofs for all eleven, watching the light come down like a slow wound opening across the sky. They say the sound arrived three days after the light. They say a great deal, in Ashfen Hollow, because talk is free and firewood is not.
<br><br>A thousand years later, the sky is a lid of iron, warmth is a thing you inherit or steal, and the buried light of dead suns runs through the world's stone like marrow through bone. Those who can feel it — draw it, refine it, burn it in the lamp of the self — are called cultivators, and they live as long as their fire and their luck hold out.
<br><br>You are ${esc_(s.chr.name)}, born under forty roofs and one shrine, to a line that owns a hearth, a name, and nothing else. The flame in the family shrine has never once gone out. Neither, the shrine keeper likes to say, has your family's stubbornness. This is the story of how one of those outlives the other.`,
  },
  {
    id: 'first_spark', book: 'Book One — ASH', title: 'The First Spark',
    when: (s) => s.chr.realm >= 1,
    text: (s) => `There is a moment — every cultivator remembers theirs — when the world stops being scenery. The stone under your feet becomes a vein. The cold air becomes a debt. The light behind your ribs, which you had always taken for something like a mood, turns over in its sleep and opens one eye.
<br><br>Sparkgathering, the realms-books call it, dryly, the way books describe drowning as 'immersion.' What it is: the sky noticed you. Whatever fell with the nine suns, whatever survives of them in the deep places — some grain of it has decided your body makes an acceptable lamp.
<br><br>${esc_(s.chr.name)} of Ashfen Hollow. Lamp-bearer. The villagers will start leaving the first portion for you at festivals now, and locking their doors a little more thoughtfully. Both are correct.`,
  },
  {
    id: 'the_robe', book: 'Book One — ASH', title: 'The Ash-Gray Robe',
    when: (s) => s.chr.sect.joined,
    text: () => `The Order of the Kindled Path keeps the Third Ember: one of the nine hearts of the fallen suns, banked in a mountain, tended in shifts that have not been broken in six centuries. Disciples carry braziers up ten thousand steps because a flame carried is a flame understood. That is the whole doctrine. Everything else is footnotes and sword forms.
<br><br>The robe they give you is gray, coarse, and smells faintly of every disciple who wore it before. This is intentional. "You are a link now," the elder says, in the tone of someone handing over something heavy. "Links do not get to be interesting. Links get to hold."
<br><br>That night, from the outer dormitories, you watch the brazier-line climb the dark mountain like a procession of patient stars, and you understand that you have joined something that intends to outlive the sky.`,
  },
  {
    id: 'wide_world', book: 'Book Two — ROADS', title: 'The Wide World',
    when: (s) => { const r = REGIONS.find((x) => x.id === s.world.region); return (r?.domain || 'vale') !== 'vale'; },
    text: (s) => `Every map of the vale ends the same way: the roads run east off the parchment, and some long-dead cartographer has written, in small honest letters, "more world."
<br><br>Now you are in the more world. Behind you, the whole of the Cindered Vale — every village, vein, and grudge you have ever known — is becoming a smudge of smoke on the western sky, and it is astonishing, actually offensive, how small it looks. A day's walk out here belongs to no one you've heard of. The teahouses argue about names you don't know, realms you can't see the top of, wars that your village never learned had started, let alone ended.
<br><br>${esc_(s.chr.name)} walks east under the iron sky, carrying a hearth-name from a hollow between hills. The world is about to find out what that's worth. So are you.`,
  },
  {
    id: 'deep_water', book: 'Book Two — ROADS', title: 'Salt and Drowned Light',
    when: (s) => { const r = REGIONS.find((x) => x.id === s.world.region); return r?.domain === 'coast'; },
    text: () => `The Fourth Sun refused to die on land. It came down burning over the eastern sea and the sea closed over it, and a thousand years later it is still down there, forty fathoms deep, glowing like a coal cupped in two black hands.
<br><br>The coast that grew up around that drowned dawn is not like other places. Tide-law instead of kings. Harbor-mothers instead of magistrates. Divers who come up rich, or strange, or not at all, and lamps in every window not for the dark but for the dead, who are numerous and, by all accounts, sociable.
<br><br>At night the sea to the east glows faintly, patiently, like a door left open for someone. The locals do not look at it. You will learn not to look at it. That is lesson one, and it is free. Everything else on this coast has a price in salt.`,
  },
  {
    id: 'iron_grass', book: 'Book Two — ROADS', title: 'The Grass That Remembers War',
    when: (s) => { const r = REGIONS.find((x) => x.id === s.world.region); return r?.domain === 'steppe'; },
    text: () => `When the Eighth Sun fell, the asura host rode out to meet it. This is not a legend; legends have variants. This account has none: eight hundred years ago, the fiercest people under the dying sky looked up at a falling god and decided, unanimously, that it should have to go through them.
<br><br>It did. The place where it happened is a sea of glass a hundred li across, and the horde that survives is the horde that was too young, too old, or too stubborn to die that day — and their descendants, who inherited the stubbornness in its purified form.
<br><br>They will not respect your realm. They will not respect your name. There is exactly one currency on the Burning Steppe, and it is spent in an arena of scarred stone under the open sky. The Banner That Does Not Cool is always watching. Bring what you have.`,
  },
  {
    id: 'the_heir', book: 'Book Three — THE WHEEL', title: 'What the Fire Keeps',
    when: (s) => s.meta.generation >= 2,
    text: (s) => `Here is what the Hearthline knows that the sects, with their immortality pills and their thousand-year elders, have never quite believed: a life is not the unit. The fire is the unit.
<br><br>A cultivator dies — of the sky's lightning, of a foe's patience, of the simple honest arithmetic of years — and the sects write "ended." But in Ashfen Hollow the shrine keeper banks the coals, and an heir stands up in the ash-smell of the old life, wearing its deeds like a coat cut down to fit, and the fire does not go out. It has not gone out in a thousand years. Grief, in your family, is a form of fuel.
<br><br>${esc_(s.chr.name)}, generation ${s.meta.generation} of the line. Everything your predecessors clawed from the world is in your two hands. The Wheel turns; the flame passes; the debt of the Tenth Dawn comes down the years to you. Spend it all. That's what it's for.`,
  },
  {
    id: 'the_hymn', book: 'Book Four — DAWN', title: 'Nine Notes Falling, One Rising',
    when: (s) => !!s.chr.flags.hymn_learned,
    text: () => `The Tenthfire Hymn was not written. It was transcribed — from the sound the sky made, say the oldest sources, in the moment before the First Sun was loosed from its place. Nine notes falling. And then a tenth note, which does not fall, which the transcribers heard and did not understand and wrote down anyway, faithfully, the way you'd write down a word in a language you pray someone will someday read.
<br><br>You have all of it now. The fragments align in your mind like a spine straightening after long stooping, and you understand — suddenly, completely, the way you understand heat by burning — why every power under the iron sky spent a thousand years keeping these verses apart.
<br><br>The Hymn is not a weapon. It is an argument. It argues that the sky is wrong, that the dark is a clerical error, that dawn is not a memory but a debt in arrears. And it is singable. By you. Now.`,
  },
  {
    id: 'the_threshold', book: 'Book Four — DAWN', title: 'Sunforger',
    when: (s) => s.chr.realm >= 5,
    text: (s) => `There are cultivators who are strong the way storms are strong, and the world makes room for them and closes behind them and forgets. And then there is what you have become, which the realms-books name Sunforging, and describe, uncharacteristically, in a whisper: one who has stopped borrowing light, and begun to make it.
<br><br>You feel it constantly now — a second heartbeat behind the sternum, hot and rhythmic and unborrowed, the first entirely new light under this sky in a thousand years. It keeps you awake. It keeps the birds near you awake. Elders of sects you have never visited know your name, ${esc_(s.chr.name)}; some of them pray it stays far away, and some of them pray it doesn't, and both are a kind of respect.
<br><br>The sky has noticed too. Of course it has. The Wheel's hum around you sounds, lately, like held breath. There is exactly one step left, and everyone — the Order, the horde, the drowned court, the dead — is watching to see if the hollow-born line takes it.`,
  },
  {
    id: 'the_shared_dawn', book: 'Book Four — DAWN', title: 'The Shared Dawn',
    when: (s) => s.meta.ending === 'share',
    text: (s) => `You could have kept it. Every account agrees on this — the Hymn was yours, the light was yours, the sky itself had signed the deed. And ${esc_(s.chr.name)} of the Hearthline stood at the crater's heart with a whole dawn cupped in two hands, and did the one thing no power under the iron sky had thought to plan for.
<br><br>Opened them.
<br><br>The light went out of you like water finding its level — into the ember-veins, into the banked coals of ten thousand shrines, into every hearth from the vale to the Glass Sea. No second sun rose that day. Instead, everything warmed: two degrees, everywhere, forever. The Withering ended not with a sunrise but with a season — one long spring that has not stopped. They do not name a Dawnbearer in the annals. They name a thousand villages that stopped burying their winters. The Hearthline's fire is smaller than it might have been, and in every window, and this — the shrine keeper says, sweeping, smiling — was obviously always the point.`,
  },
  {
    id: 'the_kept_flame', book: 'Book Four — DAWN', title: 'The Kept Flame',
    when: (s) => s.meta.ending === 'refuse',
    text: (s) => `The sky waited. The Hymn was sung to its final falling note, the tenth note rose in ${esc_(s.chr.name)}'s throat like a sunrise asking permission — and the answer, weighed in the oldest scales the Hearthline owns, was: not this. Not a sun that costs a singer. Not a dawn with a name on it.
<br><br>You walked home. The accounts dwell on this: down from the crater rim, through the standing stones, west along the long roads, home — carrying an unsung note the way your family has always carried fire. It is still there, banked behind your sternum, patient as the shrine flame. The world stayed dim, and stayed whole, and the vale lights its lamps at dusk same as ever.
<br><br>But children in Ashfen Hollow are told, when the ninth lamp gutters: somewhere in the line that keeps the shrine, there is a tenth flame already lit, waiting for a generation that needs it more than we do. Sleep now. The fire is kept.`,
  },
  {
    id: 'the_black_sunrise', book: 'Book Four — DAWN', title: 'The Black Sunrise',
    when: (s) => s.meta.ending === 'usurp',
    text: (s) => `The teahouses do not tell this one aloud. It is written in margins, in ciphers, in the worried italics of sect archivists: that when ${esc_(s.chr.name)} of the Hearthline sang the Hymn at the crater's heart, the tenth note did not rise so much as TAKE — that the new sun stood up over the eastern rim already owned, a dawn with a debt-collector's face.
<br><br>It gives light. No one disputes the light. Crops grow; the Withering is over; the ember-veins run fat. And every dusk, when the black-gold disc goes down, the whole world checks its ledgers with a shiver it cannot name, because warmth on loan is still warmth, and the interest is not due yet, and the Wheel — which doubles what it is given, always, in both directions — is turning somewhere overhead with the patience of an old, old creditor.
<br><br>In Ashfen Hollow the shrine flame burns black at the root now. The keeper sweeps as always. Some mornings she finds the ash already swept, and does not ask by whom.`,
  },
  {
    id: 'the_dawn', book: 'Book Four — DAWN', title: 'The Tenth Dawn',
    when: (s) => !!s.ended && (s.meta.ending === 'kindle' || !s.meta.ending),
    text: (s) => `Afterwards, the accounts disagree about everything except the color. Not ember-orange, they all say. Not the banked red of the veins or the gold of shrine lamps. Morning-colored: that impossible clean white-gold that only the very oldest paintings remember, laid across the world like a hand on a fevered forehead.
<br><br>In Ashfen Hollow, the shrine keeper stood in the doorway of the shrine her family kept for a thousand years, and watched true dawn cross the forty roofs, and then — gently, out of respect, with a hand that shook only a little — banked the family flame low. Not out. It will never go out; some habits are load-bearing. Just low. For the first time in living memory, nobody needed it for the warmth.
<br><br>${esc_(s.chr.name)}, of the Hearthline, hollow-born: the Dawnbearer. The sky is wearing your family's fire now. The teahouses will argue about the details for ten thousand years, and every version, every single one, begins in a small cold village with a flame that would not go out.`,
  },
];
// tiny local escape for story interpolation (data.js has no imports)
function esc_(x) { return String(x ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'); }


export const ACTION_DEFS = {
  meditate:   { label: 'Meditate', ap: 1, desc: 'Draw Emberlight from the region’s veins. (Breathing minigame)' },
  rest:       { label: 'Rest', ap: 1, desc: 'Recover health and Emberlight at an inn or your own bed.' },
  market:     { label: 'Market', ap: 0, desc: 'Buy and sell goods.' },
  jobs:       { label: 'Odd Jobs', ap: 1, desc: 'Honest mortal work for honest mortal coin.' },
  wander:     { label: 'Wander', ap: 1, desc: 'Walk without a destination. The world notices.' },
  alchemy:    { label: 'Alchemy', ap: 1, desc: 'Refine herbs into pills over a furnace. (Pillfire minigame)' },
  shrine:     { label: 'Hearth Shrine', ap: 0, desc: 'Tend the family flame: bloodline perks and the honored dead.' },
  forage:     { label: 'Forage', ap: 1, desc: 'Search for herbs. (Foraging minigame)' },
  hunt:       { label: 'Hunt', ap: 1, desc: 'Seek out a spirit beast and fight it.' },
  mine:       { label: 'Mine the Veins', ap: 1, desc: 'Dig for ore and stones. Occasionally the mine digs back.' },
  listings:   { label: 'Job Listings', ap: 1, desc: 'Freelance work off the Market board: rougher, better paid.' },
  missions:   { label: 'Mission Board', ap: 0, desc: 'Take and turn in sect missions for contribution.' },
  library:    { label: 'Scripture Library', ap: 0, desc: 'Exchange contribution for techniques.' },
  store:      { label: 'Sect Store', ap: 0, desc: 'Exchange contribution for pills and treasures.' },
  spar:       { label: 'Spar', ap: 1, desc: 'Practice bouts with a senior. No stakes, real lessons.' },
  expedition: { label: 'Deep Expedition', ap: 2, desc: 'Push deep into the waste: several fights, better rewards.' },
  temple_depths:{ label: 'Descend the Depths', ap: 2, desc: 'Face what remains of the Abbot. (Boss — defeat can be fatal)' },
  crater_heart:{ label: 'The Crater’s Heart', ap: 2, desc: 'Where dawns are born. Guardians, fragments, and the end of the path.' },
  talk:       { label: 'Talk', ap: 1, desc: 'Sit with someone who knows things. Ties deepen; rumors surface.' },
  austerities:{ label: 'Austerities', ap: 0, desc: 'Swear a vow of tapas: silence, fasting, or vigil. Power for a price.' },
  auction:    { label: 'Grand Auction', ap: 1, desc: 'Riverport’s bell rings noon: rare lots, deep pockets, and bidding wars.' },
  arena:      { label: 'The Scarred Arena', ap: 2, desc: 'The horde’s yearly tournament: three bouts, no substitutions, real prizes.' },
  ziggurat_heart:{ label: 'Climb the Ziggurat', ap: 2, desc: 'Face the Ember Guardian for its verse of the Hymn. (Boss — defeat can be fatal)' },
  tide_court: { label: 'The Drowned Court', ap: 2, desc: 'Seek audience with the Tide-Hollowed King. He accepts petitions in blood. (Boss — deadly)' },
  raise:      { label: 'Raise the Heir', ap: 1, desc: 'An hour by the cradle. What you give now, the next life keeps. (age 25+)' },
  forge:      { label: 'The Forge', ap: 1, desc: 'Fold star-iron into your weapon. Each tier is permanent — and heirlooms remember.' },
  challenge:  { label: 'Challenge for Rank', ap: 1, desc: 'A witnessed duel for standing. Once a year; the healers insist.' },
};

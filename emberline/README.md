# EMBERLINE — Ashes of the Ninth Sun

A full-stack cultivation life-sim. Nine suns were shot from the sky a thousand
years ago; the world grew cold around their buried embers. You refine
**Emberlight** from the wreckage, climb the realms, and race your own lifespan
to kindle a **Tenth Dawn**. When your cultivator dies, retires, or falls, the
**Hearthline** continues: you assemble a *Progeny Kit* of heirlooms and play on
as your heir, in the same persistent world.

Self-contained — nothing outside this folder except a `launch.json` entry.

## Run

```bash
# from the repo root (reuses the repo venv's fastapi/uvicorn)
.venv/bin/python emberline/server.py
# then open http://127.0.0.1:7777
```

Saves are JSON files in `emberline/saves/` (`EMBERLINE_SAVE_DIR` overrides;
`EMBERLINE_PORT` overrides the port). If the server is unreachable the client
falls back to `localStorage` and the save dot turns gold.

## The world in one breath

| Realm | Lifespan | You are… |
|---|---|---|
| Mortal | 60 | ash in your lungs, dreams of warmth |
| Sparkgathering | 85 | able to feel Emberlight |
| Kindling | 115 | a true flame behind the sternum |
| Emberheart | 160 | a heart that will not die |
| Blazebound | 220 | light bleeding from old scars |
| Sunforging | 320 | smelting a sun of your own |
| **Dawnbearer** | — | **the Tenth Dawn (victory)** |

Time passes in seasons (3 actions each). Every 4 seasons you age a year.
Realms extend your lifespan; the Hearth Shrine spends **Hearthflame** on
permanent bloodline perks for every future generation.

## The Great World

Four domains under a sunless sky, on a real clickable map (the 🗺 chip):

| Domain | Opens at | What's there |
|---|---|---|
| The Cindered Vale | Mortal | the original eight regions, the Order, the Ninth Crater |
| The Sunward Marches | Kindling | Riverport's **Grand Auction** (bidding wars vs rival buyers), the Serpent's Spine, the Lotus Marsh, the Bone Orchard |
| The Drowned Coast | Emberheart | Saltglass Harbor, dives to the **Sunken Star** (star-iron), the Leviathan Graves and the **Tide-Hollowed King** |
| The Burning Steppe | Blazebound | the Iron Horde's yearly **Scarred Arena** tournament, the Glass Sea, and the **Obsidian Ziggurat** |

Travel within a domain is free; crossing between domains costs an action and
rolls road events (tolls, pilgrims, ferrymen, wandering traders). The three
Tenthfire Hymn fragments are scattered across the world — the Crater Choir and
the Warden in the Vale, the Ember Guardian atop the Ziggurat — so ascension
demands the whole map. Ten new enemies, star-iron artifacts, storm/lotus/brine
herbs with new pill recipes, and three new NPCs (Nine-Fingers, Captain Rema,
the Khan's Shadow) whose kinship pays in auction fees, dive hauls, and arena
healers.

## The Dao Revamp

- **Insights** — understanding earned only from living (first blood, apex kills,
  dreams, vows kept, kinship, far roads, near deaths, the hammer, the arena…).
  Each speeds *all* cultivation +6%; every third endures +1 tribulation bolt.
  The Paths tab lists what you hold and hints at what you haven't learned.
- **Dao paths** — at Kindling, choose forever: Sword, Furnace, Wheel, Hearth,
  or Void. Each reshapes your numbers, teaches a unique technique, and colors
  the life (karma doubled, pills amplified, heirs enriched…).
- **Rivals act** — they bid against you at auction *by name*, meet you in the
  arena bracket, hold grudges that ambush the long roads, die to their own
  tribulations, and it all reaches the news.
- **NPC arcs** — every named character has a three-step personal story that
  kinship unlocks, ending in real gifts; close kin join your expeditions.
- **Ancestral echoes** — the world remembers your dead: the healer's
  grandchild, the naga's dynasty-courtesy, Yan Shuo's student, the tablet of
  the one who died fighting.
- **Deeper combat** — poise bars and guard-shattering staggers, elemental
  reactions off burning foes, boss phase-shifts at half health, techniques that
  evolve after 25 real uses.
- **Delves** — expeditions are now three forks deep: spoor, wrong silences,
  wayside shrines, marked caches, strange lights.
- **Discoveries** — wandering finds permanent places (the Hunters' Cache, the
  Warm Vein, the Star Pool…), each adding a real action to its region.
- **Sect life** — yearly rank-duels, and the guttering of the Third Ember.
- **The estate & the heir** — build shrine/garden/library with stones that
  persist across generations; spend cradle-side hours shaping your heir's
  stats and even bequeathing whole insights.
- **The forge** — fold star-iron into your weapon, five tiers, heirloom-kept.
- **Four endings** — kindle the Dawn, share it into every hearth, refuse it
  and keep the flame, or (karma-black) raise the Black Sunrise.
- **Sound** — synthesized wind that follows the weather, gongs, bells, chimes,
  thunder; a ⚙ settings panel (mute, quiet minigames, still sky), keyboard
  shortcuts (1–9 actions, M map, E end season), map fog of war, Chronicle
  export to a standalone HTML novel, JSON content packs (`content/*.json`),
  ghost hearthlines from other saves on your server, and a pacing simulator
  (`node tools/simulate.mjs`).

## The Chronicle

The game writes itself as a novel. At life's thresholds — the first spark, the
ash-gray robe, the first road out of the Vale, the first heir, the Hymn, the
Dawn — a chapter page turns: **Book One — ASH: "The Hollow Between Hills"**,
and so on through four books. Every chapter is kept in the Hearth tab's
Chronicle, rereadable forever; a dynasty finishes the game with its own
webnovel. Above it all, a slow starfield burns in the sunless sky, and every
so often a light falls — by law, falling light belongs to whoever reaches it.

## The Living World

The world runs whether you watch it or not — inspired by *Reverend Insanity*'s
resource-driven ruthlessness, *Shadow Slave*'s Aspects and dream-trials, and
Hindu cosmology's karma and samsara:

- **Provisions & hunger** — every season eats. Run out and cultivation suffers
  while your body burns itself. Hunts can be dressed for meat; markets sell grain.
- **Weather** — each season rolls weather with real teeth: glass winds close
  roads and move prices, iron cold doubles appetite, green winds swell foraging.
- **A drifting economy** — prices move each season (▲▼ in the market) and
  weather or gluts push them further. Buy low, sell into scarcity.
- **The Roll of Names** — rival cultivators advance realms on their own clock.
  Check the World tab; the teahouses keep score.
- **Named people** — Granny Ash, Old Bo, Sister Iron Lotus, Merchant Shen.
  Sit with them, bring the right gifts, and kinship pays: free wound-treatment,
  smith's rates, mission bonuses, a back room of rarities.
- **Karma & the Wheel** — choices tilt a hidden ledger. Omens follow you; the
  heavens hesitate for the virtuous at tribulations; a karmic echo passes to
  your heir (samsara — blessed or crow-marked at birth).
- **Aspects & Flaws** — your first breakthrough wakes a unique gift *and* an
  unchosen price: Heaven-Marked draws extra lightning, the Bleeding Ledger
  collects nightly, Hollow Appetite eats double.
- **Dreams of the Wheel** — sleep sometimes opens past-life trials: drowned
  kings, the churning of the milk-fire sea, the lamp-extinguisher.
- **Vows (tapas)** — swear silence, fasting, or vigil at the Austerities Mat.
  Kept vows pay in cultivation, karma, and flesh; broken ones are written in red.
- **Lingering wounds** — defeats and dens leave named injuries (Cracked Rib,
  Torn Meridian…) that debuff until treated by pills or a healer.
- **The Night of Nine Lamps** — every midwinter, light lamps for the fallen
  suns and your own dead. The ancestors lean close.
- **Vein exhaustion** — meditating repeatedly in one season drains the local
  veins; real growth comes from deeds, dreams, vows, and the world.

## Systems

- **Five Ash Lineages** (spirit roots) with distinct passives: Cinder, Smoke,
  Charcoal, Glass, Tinder — an elemental pentagon that matters in combat.
- **Five minigames**: breathing-rhythm meditation, furnace-control alchemy,
  lane-dodging tribulations, minesweeper-style foraging, and a strike-timing
  bar inside combat. Every minigame has a "Resolve quietly" fallback
  (also auto-used under `prefers-reduced-motion`).
- **Turn-based combat** with telegraphed enemy intents, shields, dots, stuns,
  crits, counters, and pill-chugging.
- **Eight regions** gated by realm, each with its own actions, herbs, beasts,
  and wander-event deck (a rival arc, a hidden master, plagues, scams, storms…).
- **The Order of the Kindled Path**: entry trial, mission board, contribution
  ranks, scripture library, sect store, sparring.
- **Alchemy** with graded outcomes (Cracked → Immaculate) and permanent
  stat pills; **markets** with haggling perks; artifacts in weapon/charm slots.
- **Hearthline succession**: eulogy → spend Hearthflame on bloodline perks →
  assemble the Progeny Kit (1 artifact, 1 manual, 3 pills, ⅓ of the stones) →
  name the heir and pick their lineage. Ancestors are remembered by deed.
- **Endgame**: reassemble the Tenthfire Hymn from three crater guardians,
  reach Sunforging Peak, and survive the final tribulation to ascend.

## Layout

```
server.py            FastAPI: static hosting + /api/saves CRUD (atomic writes)
static/index.html    screens: title / creation / game
static/css/game.css  ember-and-ash theme
static/js/
  data.js            all content: realms, techniques, items, enemies, regions, events, perks
  state.js           state shape, derived stats, persistence (server + localStorage fallback)
  actions.js         gameplay engine: actions, events DSL, seasons, sect, succession
  combat.js          intent-telegraph turn combat
  minigames.js       the five minigames (promise-based, self-cleaning)
  ui.js              render layer: panels, shops, shrine, succession, ascension
  modal.js, util.js  helpers
  main.js            boot
```

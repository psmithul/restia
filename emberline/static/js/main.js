// Boot.
import { wire, showTitle } from './ui.js';
import { initSky } from './sky.js';
import { initSound } from './sound.js';
import { EVENTS } from './data.js';

initSky();
initSound();
wire();
showTitle();

// Optional content packs: extra events dropped into emberline/content/*.json.
fetch('/api/content').then((r) => r.ok ? r.json() : null).then((pack) => {
  if (pack?.events?.length) {
    EVENTS.push(...pack.events);
    console.info(`[emberline] loaded ${pack.events.length} content-pack event(s)`);
  }
}).catch(() => {});

// Ghosts: other hearthlines on this server, shown on the Roll of Names.
document.addEventListener('emberline:refresh', () => {
  if (window.__ghostsFetched) return;
  window.__ghostsFetched = true;
  import('./state.js').then(({ S }) => {
    if (!S?.slot) return;
    fetch(`/api/ghosts/${S.slot}`).then((r) => r.ok ? r.json() : []).then((g) => { window.__ghosts = g; }).catch(() => {});
  });
});

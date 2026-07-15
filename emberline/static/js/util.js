// Small helpers shared by the whole game.

export const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
export const ri = (lo, hi) => lo + Math.floor(Math.random() * (hi - lo + 1));
export const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
export const chance = (p) => Math.random() < p;

export function pickWeighted(arr, wOf) {
  const total = arr.reduce((s, x) => s + wOf(x), 0);
  let r = Math.random() * total;
  for (const x of arr) { r -= wOf(x); if (r <= 0) return x; }
  return arr[arr.length - 1];
}

// Stat check: stat + d10 vs dc + d4. Fate nudges every roll a little.
export function rollStat(statVal, dc, fate = 0) {
  const roll = statVal + ri(1, 10) + Math.floor(fate / 3);
  const target = dc + ri(1, 4);
  return { ok: roll >= target, roll, target };
}

export const esc = (s) => String(s ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

export const el = (html) => {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export const fmt = (n) => Number(n).toLocaleString('en-US');

export const prefersReducedMotion = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

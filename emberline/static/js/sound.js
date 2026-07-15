// A synthesized soundscape — no assets, all WebAudio.
// Wind that follows the weather, a gong for breakthroughs, a bell for the
// auction, thunder for tribulations, small chimes for small joys.

let ctx = null;
let master = null;
let windGain = null;
let started = false;
let muted = localStorage.getItem('emberline:muted') === '1';

export function isMuted() { return muted; }
export function setMuted(m) {
  muted = m;
  localStorage.setItem('emberline:muted', m ? '1' : '0');
  if (master) master.gain.setTargetAtTime(m ? 0 : 0.9, ctx.currentTime, 0.1);
}

function ensure() {
  if (started || muted) return started;
  try {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    master = ctx.createGain();
    master.gain.value = muted ? 0 : 0.9;
    master.connect(ctx.destination);
    startWind();
    started = true;
  } catch { /* no audio, no problem */ }
  return started;
}

// ---- ambient wind: filtered noise, slow swells ----
function startWind() {
  const len = ctx.sampleRate * 4;
  const buf = ctx.createBuffer(1, len, ctx.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
  const src = ctx.createBufferSource();
  src.buffer = buf; src.loop = true;
  const filt = ctx.createBiquadFilter();
  filt.type = 'lowpass'; filt.frequency.value = 320; filt.Q.value = 0.6;
  windGain = ctx.createGain();
  windGain.gain.value = 0.035;
  src.connect(filt).connect(windGain).connect(master);
  src.start();
  // slow swells
  const lfo = ctx.createOscillator();
  const lfoGain = ctx.createGain();
  lfo.frequency.value = 0.05; lfoGain.gain.value = 0.018;
  lfo.connect(lfoGain).connect(windGain.gain);
  lfo.start();
}

export function setWindMood(mood) {
  // mood: 'calm' | 'storm' | 'cold'
  if (!windGain) return;
  const g = mood === 'storm' ? 0.07 : mood === 'cold' ? 0.05 : 0.035;
  windGain.gain.setTargetAtTime(g, ctx.currentTime, 2);
}

// ---- one-shots ----
function tone({ f = 440, t = 0.4, type = 'sine', g = 0.2, sweep = 0, delay = 0 }) {
  if (!ensure()) return;
  const o = ctx.createOscillator();
  const gn = ctx.createGain();
  const t0 = ctx.currentTime + delay;
  o.type = type; o.frequency.setValueAtTime(f, t0);
  if (sweep) o.frequency.exponentialRampToValueAtTime(Math.max(30, f + sweep), t0 + t);
  gn.gain.setValueAtTime(0, t0);
  gn.gain.linearRampToValueAtTime(g, t0 + 0.01);
  gn.gain.exponentialRampToValueAtTime(0.0001, t0 + t);
  o.connect(gn).connect(master);
  o.start(t0); o.stop(t0 + t + 0.05);
}

export const sfx = {
  click:  () => tone({ f: 720, t: 0.05, type: 'triangle', g: 0.06 }),
  chime:  () => { tone({ f: 1174, t: 0.5, g: 0.08 }); tone({ f: 1568, t: 0.7, g: 0.06, delay: 0.09 }); },
  gong:   () => { tone({ f: 98, t: 2.8, type: 'sine', g: 0.3 }); tone({ f: 196.5, t: 2.2, g: 0.12, delay: 0.02 }); tone({ f: 293, t: 1.4, g: 0.05, delay: 0.03 }); },
  bell:   () => { tone({ f: 660, t: 1.4, g: 0.15 }); tone({ f: 1320, t: 1.0, g: 0.05, delay: 0.01 }); },
  thunder:() => { tone({ f: 70, t: 1.2, type: 'sawtooth', g: 0.22, sweep: -40 }); tone({ f: 120, t: 0.5, type: 'square', g: 0.06, sweep: -80, delay: 0.05 }); },
  hit:    () => tone({ f: 160, t: 0.12, type: 'square', g: 0.09, sweep: -60 }),
  hurt:   () => tone({ f: 110, t: 0.18, type: 'sawtooth', g: 0.1, sweep: -50 }),
  coin:   () => { tone({ f: 1046, t: 0.08, type: 'triangle', g: 0.08 }); tone({ f: 1318, t: 0.12, type: 'triangle', g: 0.07, delay: 0.06 }); },
  page:   () => tone({ f: 300, t: 0.16, type: 'triangle', g: 0.05, sweep: 140 }),
};

export function initSound() {
  // Audio may only start on a user gesture.
  const kick = () => { ensure(); document.removeEventListener('pointerdown', kick); document.removeEventListener('keydown', kick); };
  document.addEventListener('pointerdown', kick);
  document.addEventListener('keydown', kick);
  document.addEventListener('emberline:sfx', (e) => { if (sfx[e.detail]) sfx[e.detail](); });
  document.addEventListener('emberline:story', () => sfx.page());
  document.addEventListener('emberline:weather', (e) => setWindMood(e.detail));
}

export const playSfx = (name) => document.dispatchEvent(new CustomEvent('emberline:sfx', { detail: name }));

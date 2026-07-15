// Five minigames. Each renders into `host`, returns a Promise, cleans up after itself.
// Every game offers "Resolve quietly" (auto) for accessibility / reduced motion.
import { el, clamp, prefersReducedMotion } from './util.js';

const css = (name, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

// Quiet mode: settings or OS preference can auto-resolve every minigame.
const quiet = () => prefersReducedMotion() || localStorage.getItem('emberline:quiet') === '1';

function frame(host, title, hint) {
  host.innerHTML = '';
  const root = el(`<div class="mg">
    <div class="mg-head"><span class="mg-title">${title}</span>
      <button class="btn btn-ghost mg-auto" type="button">Resolve quietly</button></div>
    <div class="mg-hint">${hint}</div>
    <div class="mg-body"></div>
    <div class="mg-status" aria-live="polite"></div>
  </div>`);
  host.appendChild(root);
  return { root, body: root.querySelector('.mg-body'), status: root.querySelector('.mg-status'),
    autoBtn: root.querySelector('.mg-auto') };
}

// ---------- 1. Breathing (meditation) ----------
export function breathing({ host, beats = 8 }) {
  return new Promise((resolve) => {
    const f = frame(host, 'Ninefold Breathing', 'Press SPACE (or click) when the closing ring meets the ember. Perfect timing draws more Emberlight.');
    const cv = el('<canvas width="320" height="300" class="mg-canvas"></canvas>');
    f.body.appendChild(cv);
    const ctx = cv.getContext('2d');
    const CX = 160, CY = 150, TARGET = 56;
    let beat = 0, score = 0, perfects = 0, ringR = 150, t0 = performance.now(), done = false;
    const PERIOD = 1350;
    let flash = null; // {text, color, until}

    const finish = (auto = false) => {
      if (done) return; done = true;
      cleanup();
      const mult = auto ? 1.0 : clamp(0.4 + (score / (beats * 2)) * 1.6, 0.4, 2.0);
      resolve({ mult, perfects, auto });
    };
    const judge = () => {
      if (done || beat >= beats) return;
      const diff = Math.abs(ringR - TARGET);
      let label, color;
      if (diff < 9) { score += 2; perfects++; label = 'Perfect'; color = css('--gold', '#e2b25a'); }
      else if (diff < 22) { score += 1; label = 'Good'; color = css('--ok', '#7fae6b'); }
      else { label = 'Scattered'; color = css('--dim', '#8a8078'); }
      flash = { text: label, color, until: performance.now() + 500 };
      nextBeat();
    };
    const nextBeat = () => {
      beat++; t0 = performance.now();
      if (beat >= beats) setTimeout(() => finish(false), 550);
    };
    const onKey = (e) => { if (e.code === 'Space') { e.preventDefault(); judge(); } };
    const onClick = () => judge();
    const cleanup = () => {
      document.removeEventListener('keydown', onKey);
      cv.removeEventListener('pointerdown', onClick);
    };
    document.addEventListener('keydown', onKey);
    cv.addEventListener('pointerdown', onClick);
    f.autoBtn.addEventListener('click', () => finish(true));
    if (quiet()) { finish(true); return; }

    (function draw(now) {
      if (done) return;
      const p = ((now - t0) % PERIOD) / PERIOD;
      if (now - t0 >= PERIOD && beat < beats) { flash = { text: 'Missed breath', color: css('--dim', '#8a8078'), until: now + 400 }; nextBeat(); }
      ringR = 150 - p * 130;
      ctx.clearRect(0, 0, 320, 300);
      // ember core
      const glow = ctx.createRadialGradient(CX, CY, 4, CX, CY, TARGET);
      glow.addColorStop(0, css('--ember', '#e58a3a')); glow.addColorStop(1, 'rgba(229,138,58,0.05)');
      ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(CX, CY, TARGET, 0, 7); ctx.fill();
      ctx.strokeStyle = css('--gold', '#e2b25a'); ctx.setLineDash([4, 6]); ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(CX, CY, TARGET, 0, 7); ctx.stroke(); ctx.setLineDash([]);
      // shrinking ring
      ctx.strokeStyle = css('--fg', '#e8e0d4'); ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(CX, CY, Math.max(ringR, 6), 0, 7); ctx.stroke();
      if (flash && now < flash.until) {
        ctx.fillStyle = flash.color; ctx.font = '600 20px Georgia, serif'; ctx.textAlign = 'center';
        ctx.fillText(flash.text, CX, 34);
      }
      f.status.textContent = `Breath ${Math.min(beat + 1, beats)} of ${beats}`;
      requestAnimationFrame(draw);
    })(performance.now());
  });
}

// ---------- 2. Pillfire (alchemy) ----------
export function pillfire({ host, diff = 0 }) {
  return new Promise((resolve) => {
    const f = frame(host, 'Pillfire Control', 'HOLD Space (or press the furnace) to stoke; release to let it cool. Keep the needle inside the drifting golden band.');
    const cv = el('<canvas width="460" height="220" class="mg-canvas"></canvas>');
    const btn = el('<button class="btn btn-hold" type="button">Stoke the furnace (hold)</button>');
    f.body.append(cv, btn);
    const ctx = cv.getContext('2d');
    const DURATION = 16000;
    let temp = 40, holding = false, inBand = 0, total = 0, done = false, start = performance.now(), last = start;
    const bandW = Math.max(11, 22 - diff * 3);

    const bandCenter = (t) => 50 + Math.sin(t / 2600) * (22 + diff * 5) + Math.sin(t / 900 + 2) * (6 + diff * 2);
    const finish = (auto = false) => {
      if (done) return; done = true; cleanup();
      resolve({ score: auto ? 0.55 : (total ? inBand / total : 0), auto });
    };
    const down = (e) => { if (e.code === 'Space' || e.type === 'pointerdown') { e.preventDefault(); holding = true; } };
    const up = (e) => { if (e.code === 'Space' || e.type === 'pointerup' || e.type === 'pointerleave') holding = false; };
    const cleanup = () => {
      document.removeEventListener('keydown', down); document.removeEventListener('keyup', up);
      btn.removeEventListener('pointerdown', down); btn.removeEventListener('pointerup', up);
      btn.removeEventListener('pointerleave', up);
    };
    document.addEventListener('keydown', down); document.addEventListener('keyup', up);
    btn.addEventListener('pointerdown', down); btn.addEventListener('pointerup', up);
    btn.addEventListener('pointerleave', up);
    f.autoBtn.addEventListener('click', () => finish(true));
    if (quiet()) { finish(true); return; }

    (function draw(now) {
      if (done) return;
      const dt = Math.min((now - last) / 1000, 0.05); last = now;
      const t = now - start;
      temp = clamp(temp + (holding ? 44 : -26) * dt, 0, 100);
      const c = bandCenter(t);
      total++; if (Math.abs(temp - c) <= bandW / 2) inBand++;
      ctx.clearRect(0, 0, 460, 220);
      // gauge track
      ctx.fillStyle = 'rgba(255,255,255,0.06)'; ctx.fillRect(30, 90, 400, 26);
      // band
      const bx = 30 + ((c - bandW / 2) / 100) * 400;
      ctx.fillStyle = 'rgba(226,178,90,0.35)'; ctx.fillRect(bx, 84, (bandW / 100) * 400, 38);
      ctx.strokeStyle = css('--gold', '#e2b25a'); ctx.strokeRect(bx, 84, (bandW / 100) * 400, 38);
      // needle
      const nx = 30 + (temp / 100) * 400;
      ctx.fillStyle = css('--ember', '#e58a3a');
      ctx.beginPath(); ctx.moveTo(nx, 78); ctx.lineTo(nx - 7, 62); ctx.lineTo(nx + 7, 62); ctx.closePath(); ctx.fill();
      ctx.fillRect(nx - 1.5, 78, 3, 50);
      // flame meter + progress
      ctx.fillStyle = css('--dim', '#8a8078'); ctx.font = '13px system-ui'; ctx.textAlign = 'left';
      ctx.fillText(holding ? 'STOKING' : 'cooling', 30, 40);
      const prog = clamp(t / DURATION, 0, 1);
      ctx.fillStyle = 'rgba(255,255,255,0.06)'; ctx.fillRect(30, 170, 400, 8);
      ctx.fillStyle = css('--ember', '#e58a3a'); ctx.fillRect(30, 170, 400 * prog, 8);
      f.status.textContent = `Purity: ${total ? Math.round((inBand / total) * 100) : 0}%`;
      if (t >= DURATION) { finish(false); return; }
      requestAnimationFrame(draw);
    })(performance.now());
  });
}

// ---------- 3. Ashfall (tribulation) ----------
export function ashfall({ host, realm = 1, grace = 0, slow = false, charcoal = false, extraWaves = 0 }) {
  return new Promise((resolve) => {
    const allowed = 2 + grace;
    const waves = 7 + realm * 2 + extraWaves;
    const f = frame(host, 'Tribulation: the Ashfall',
      `Lightning telegraphs a lane, then strikes. Move with ← → (or click a lane). Endure ${waves} bolts; more than ${allowed} hits and the tribulation wins.`);
    const cv = el('<canvas width="440" height="300" class="mg-canvas"></canvas>');
    f.body.appendChild(cv);
    const ctx = cv.getContext('2d');
    const LANES = 3, LW = 440 / LANES;
    let lane = 1, taken = 0, wave = 0, done = false;
    let telegraph = (900 - realm * 75) * (slow ? 1.35 : 1) * (charcoal ? 1.18 : 1);
    telegraph = Math.max(telegraph, 320);
    let bolt = null; // {lane, at, strikeAt, struckAt}
    let flashUntil = 0, hitFlash = false;

    const finish = (auto = false) => {
      if (done) return; done = true; cleanup();
      if (auto) { const t = 2 + Math.floor(realm / 3); resolve({ survived: t <= allowed, taken: t, waves, auto }); }
      else resolve({ survived: taken <= allowed, taken, waves, auto });
    };
    const move = (d) => { lane = clamp(lane + d, 0, LANES - 1); };
    const onKey = (e) => {
      if (e.key === 'ArrowLeft') { e.preventDefault(); move(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); move(1); }
    };
    const onClick = (e) => {
      const r = cv.getBoundingClientRect();
      lane = clamp(Math.floor(((e.clientX - r.left) / r.width) * LANES), 0, LANES - 1);
    };
    const cleanup = () => { document.removeEventListener('keydown', onKey); cv.removeEventListener('pointerdown', onClick); };
    document.addEventListener('keydown', onKey);
    cv.addEventListener('pointerdown', onClick);
    f.autoBtn.addEventListener('click', () => finish(true));
    if (quiet()) { finish(true); return; }

    const spawn = (now) => {
      wave++;
      bolt = { lane: Math.floor(Math.random() * LANES), at: now, strikeAt: now + telegraph };
      telegraph = Math.max(300, telegraph * 0.96);
    };
    (function draw(now) {
      if (done) return;
      if (!bolt) spawn(now);
      ctx.clearRect(0, 0, 440, 300);
      // sky + lanes
      ctx.fillStyle = 'rgba(255,255,255,0.04)';
      for (let i = 0; i < LANES; i++) if (i % 2) ctx.fillRect(i * LW, 0, LW, 300);
      // telegraph / strike
      if (bolt) {
        const cxL = bolt.lane * LW;
        if (now < bolt.strikeAt) {
          const p = (now - bolt.at) / (bolt.strikeAt - bolt.at);
          ctx.fillStyle = `rgba(226,178,90,${0.10 + p * 0.25})`;
          ctx.fillRect(cxL, 0, LW, 300);
        } else if (!bolt.struckAt) {
          bolt.struckAt = now;
          if (bolt.lane === lane) { taken++; hitFlash = true; }
          flashUntil = now + 220;
        }
        if (bolt.struckAt && now < flashUntil) {
          ctx.fillStyle = hitFlash ? 'rgba(212,80,60,0.75)' : 'rgba(240,235,220,0.85)';
          ctx.fillRect(cxL + LW * 0.3, 0, LW * 0.4, 300);
        } else if (bolt.struckAt) {
          bolt = null; hitFlash = false;
          if (taken > allowed) { finish(false); return; }
          if (wave >= waves) { finish(false); return; }
        }
      }
      // player
      const px = lane * LW + LW / 2;
      ctx.fillStyle = css('--ember', '#e58a3a');
      ctx.beginPath(); ctx.arc(px, 262, 14, 0, 7); ctx.fill();
      ctx.fillStyle = css('--fg', '#e8e0d4'); ctx.font = '12px system-ui'; ctx.textAlign = 'center';
      ctx.fillText('you', px, 292);
      f.status.textContent = `Bolt ${Math.min(wave, waves)}/${waves} · struck ${taken}/${allowed} times`;
      requestAnimationFrame(draw);
    })(performance.now());
  });
}

// ---------- 4. Foraging grid ----------
export function forageGrid({ host, herbCount = 5, hazardCount = 3, reveals = 8 }) {
  return new Promise((resolve) => {
    const f = frame(host, 'Foraging', `Turn over ${reveals} patches of ground. Numbers count herbs hiding in adjacent patches. Disturb a beast den and you'll regret it.`);
    const N = 5;
    const cells = Array.from({ length: N * N }, (_, i) => ({ i, herb: false, hazard: false, open: false }));
    const shuffled = [...cells].sort(() => Math.random() - 0.5);
    shuffled.slice(0, herbCount).forEach((c) => (c.herb = true));
    shuffled.slice(herbCount, herbCount + hazardCount).forEach((c) => (c.hazard = true));
    const adj = (i) => {
      const r = Math.floor(i / N), c = i % N, out = [];
      for (let dr = -1; dr <= 1; dr++) for (let dc = -1; dc <= 1; dc++) {
        if (!dr && !dc) continue;
        const rr = r + dr, cc = c + dc;
        if (rr >= 0 && rr < N && cc >= 0 && cc < N) out.push(cells[rr * N + cc]);
      }
      return out;
    };
    let left = reveals, herbs = 0, hazards = 0, done = false;
    const grid = el(`<div class="forage-grid" role="grid"></div>`);
    const leave = el('<button class="btn" type="button">Leave with your findings</button>');
    f.body.append(grid, leave);
    const btns = cells.map((c) => {
      const b = el(`<button class="forage-cell" type="button" aria-label="patch ${c.i + 1}"></button>`);
      b.addEventListener('click', () => reveal(c, b));
      grid.appendChild(b);
      return b;
    });
    const finish = (auto = false) => {
      if (done) return; done = true;
      if (auto) resolve({ herbs: Math.ceil(herbCount * 0.5), hazards: 0, auto });
      else resolve({ herbs, hazards, auto });
    };
    function reveal(c, b) {
      if (done || c.open || left <= 0) return;
      c.open = true; left--;
      if (c.herb) { herbs++; b.classList.add('is-herb'); b.textContent = '❋'; }
      else if (c.hazard) { hazards++; b.classList.add('is-hazard'); b.textContent = '✸'; }
      else { const n = adj(c.i).filter((x) => x.herb).length; b.classList.add('is-open'); b.textContent = n || ''; }
      f.status.textContent = `${left} looks left · ${herbs} herbs · ${hazards} dens disturbed`;
      if (left <= 0) setTimeout(() => finish(false), 650);
    }
    f.status.textContent = `${left} looks left`;
    leave.addEventListener('click', () => finish(false));
    f.autoBtn.addEventListener('click', () => finish(true));
  });
}

// ---------- 5. Strike timing (combat) ----------
export function strikeBar({ host, critPct = 12 }) {
  return new Promise((resolve) => {
    const wrap = el(`<div class="strikebar-wrap"><div class="mg-hint">Strike true — stop the spark in the golden heart of the bar.</div>
      <canvas width="340" height="56" class="mg-canvas strikebar"></canvas></div>`);
    host.innerHTML = ''; host.appendChild(wrap);
    const cv = wrap.querySelector('canvas'); const ctx = cv.getContext('2d');
    const W = 340, critW = (critPct / 100) * W, hitW = W * 0.3;
    let x = 0, dir = 1, passes = 0, done = false;
    const finish = (res) => { if (done) return; done = true; cleanup(); resolve(res); };
    const judge = () => {
      const c = W / 2;
      if (Math.abs(x - c) <= critW / 2) finish('crit');
      else if (Math.abs(x - c) <= critW / 2 + hitW / 2) finish('hit');
      else finish('weak');
    };
    const onKey = (e) => { if (e.code === 'Space') { e.preventDefault(); judge(); } };
    const cleanup = () => { document.removeEventListener('keydown', onKey); };
    document.addEventListener('keydown', onKey);
    cv.addEventListener('pointerdown', judge);
    if (quiet()) { finish('hit'); return; }
    (function draw() {
      if (done) return;
      x += dir * 4.2;
      if (x >= W) { x = W; dir = -1; passes++; }
      if (x <= 0) { x = 0; dir = 1; passes++; }
      if (passes >= 4) { finish('hit'); return; }
      ctx.clearRect(0, 0, W, 56);
      ctx.fillStyle = 'rgba(255,255,255,0.07)'; ctx.fillRect(0, 18, W, 20);
      ctx.fillStyle = 'rgba(232,224,212,0.18)'; ctx.fillRect(W / 2 - critW / 2 - hitW / 2, 18, critW + hitW, 20);
      ctx.fillStyle = css('--gold', '#e2b25a'); ctx.fillRect(W / 2 - critW / 2, 14, critW, 28);
      ctx.fillStyle = css('--ember', '#e58a3a'); ctx.beginPath(); ctx.arc(x, 28, 9, 0, 7); ctx.fill();
      requestAnimationFrame(draw);
    })();
  });
}

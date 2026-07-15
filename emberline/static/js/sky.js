// The sky where nine suns used to hang: a slow starfield with the occasional
// falling light. ("By law, falling light belongs to whoever reaches it.")
import { prefersReducedMotion } from './util.js';

export function initSky() {
  const cv = document.getElementById('skyfield');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  let W = 0, H = 0, stars = [];
  const meteors = [];

  const seed = () => {
    W = cv.width = window.innerWidth;
    H = cv.height = window.innerHeight;
    const n = Math.round((W * H) / 11000);
    stars = Array.from({ length: n }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      r: 0.4 + Math.random() * 1.1,
      warm: Math.random() < 0.22,
      tw: Math.random() * Math.PI * 2,
      sp: 0.3 + Math.random() * 0.9,
    }));
  };
  window.addEventListener('resize', seed);
  seed();

  const drawStatic = () => {
    ctx.clearRect(0, 0, W, H);
    for (const s of stars) {
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = s.warm ? '#d9a441' : '#cfc8bc';
      ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = 1;
  };

  if (prefersReducedMotion() || localStorage.getItem('emberline:still') === '1') { drawStatic(); return; }

  let nextMeteor = performance.now() + 6000 + Math.random() * 12000;
  let last = 0;
  const frame = (now) => {
    // ~30fps is plenty for a sky
    if (now - last > 33) {
      last = now;
      ctx.clearRect(0, 0, W, H);
      for (const s of stars) {
        s.tw += 0.02 * s.sp;
        const a = 0.25 + Math.abs(Math.sin(s.tw)) * 0.55;
        ctx.globalAlpha = a;
        ctx.fillStyle = s.warm ? '#d9a441' : '#cfc8bc';
        ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 7); ctx.fill();
      }
      if (now > nextMeteor) {
        nextMeteor = now + 9000 + Math.random() * 16000;
        const x = Math.random() * W * 0.8 + W * 0.1;
        meteors.push({ x, y: -20, vx: 2.2 + Math.random() * 2, vy: 3.2 + Math.random() * 2, life: 1 });
      }
      for (let i = meteors.length - 1; i >= 0; i--) {
        const mt = meteors[i];
        mt.x += mt.vx; mt.y += mt.vy; mt.life -= 0.008;
        if (mt.life <= 0 || mt.y > H + 30) { meteors.splice(i, 1); continue; }
        const grad = ctx.createLinearGradient(mt.x, mt.y, mt.x - mt.vx * 14, mt.y - mt.vy * 14);
        grad.addColorStop(0, `rgba(245,215,142,${0.85 * mt.life})`);
        grad.addColorStop(1, 'rgba(245,215,142,0)');
        ctx.strokeStyle = grad;
        ctx.lineWidth = 1.6;
        ctx.globalAlpha = 1;
        ctx.beginPath();
        ctx.moveTo(mt.x, mt.y);
        ctx.lineTo(mt.x - mt.vx * 14, mt.y - mt.vy * 14);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

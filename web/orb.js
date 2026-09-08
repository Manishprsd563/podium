// Podium's orb: a podium-spotlight — a soft central disc ringed by breathing
// arcs. Reused at two sizes: the large gate orb and the ~44px bubble orb.
// createOrb(canvas) owns its own rAF loop, pausing it whenever the canvas is
// not visible (gate closed, tab hidden) so an idle orb burns no CPU.

const TEAL = [79, 209, 197]; // --accent / --listening
const AMBER = [242, 184, 75]; // --speaking
const DIM = [110, 126, 142]; // muted state, used while connecting

function mix(a, b, t) {
  return [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t));
}
function rgba(c, a) {
  return `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${Math.max(0, Math.min(1, a))})`;
}

export function createOrb(canvas) {
  const ctx = canvas.getContext("2d");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let state = "connecting"; // connecting | listening | speaking | user
  let levels = { agent: 0, user: 0 };
  let dpr = Math.max(1, window.devicePixelRatio || 1);
  let w = 0;
  let h = 0;
  let visible = true;
  let destroyed = false;
  let raf = null;
  let rotation = 0;
  const t0 = performance.now();

  function resize() {
    dpr = Math.max(1, window.devicePixelRatio || 1);
    const rect = canvas.getBoundingClientRect();
    w = Math.max(1, Math.round(rect.width || canvas.width || 1));
    h = Math.max(1, Math.round(rect.height || canvas.height || 1));
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (reduceMotion) draw(performance.now());
  }

  function draw(now) {
    const elapsed = (now - t0) / 1000;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const base = Math.min(w, h) * 0.3;

    if (!reduceMotion) {
      const spin = state === "listening" ? 0.15 : state === "connecting" ? 0.05 : 0.35;
      rotation += 0.01 * spin;
    }

    let color = TEAL;
    let discRadius = base;
    let glowAlpha = 0.4;
    let breathe = reduceMotion ? 0.5 : Math.sin(elapsed * (state === "connecting" ? 1.1 : 0.6)) * 0.5 + 0.5;

    if (state === "connecting") {
      color = DIM;
      discRadius = base * (0.9 + 0.06 * breathe);
      glowAlpha = 0.18 + 0.14 * breathe;
    } else if (state === "speaking") {
      color = AMBER;
      discRadius = base * (1 + 0.26 * levels.agent);
      glowAlpha = 0.48 + 0.35 * levels.agent;
    } else if (state === "user") {
      color = TEAL;
      discRadius = base * (1 + 0.08 * levels.user);
      glowAlpha = 0.4 + 0.22 * levels.user;
    } else {
      color = TEAL;
      discRadius = base * (0.98 + 0.03 * breathe);
      glowAlpha = 0.32 + 0.12 * breathe;
    }

    // outer glow
    const grad = ctx.createRadialGradient(cx, cy, discRadius * 0.15, cx, cy, discRadius * 1.7);
    grad.addColorStop(0, rgba(color, glowAlpha));
    grad.addColorStop(1, rgba(color, 0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, discRadius * 1.7, 0, Math.PI * 2);
    ctx.fill();

    // central disc (the spotlight)
    ctx.beginPath();
    ctx.fillStyle = rgba(color, 0.92);
    ctx.arc(cx, cy, discRadius, 0, Math.PI * 2);
    ctx.fill();

    // concentric ring arcs
    const ringCount = 4;
    const lineWidth = Math.max(1, base * 0.05);
    for (let i = 1; i <= ringCount; i++) {
      const spread = i / ringCount;
      let ringRadius = base * (1.2 + spread * 0.5);
      let alpha = 0.16 * (1 - spread * 0.6);
      let ringColor = color;

      if (!reduceMotion && state === "speaking") {
        // rings ripple outward from the disc, faster/brighter with amplitude
        const speed = 0.55 + levels.agent * 1.6;
        const phase = (elapsed * speed + spread) % 1;
        ringRadius = base * (1.05 + phase * (0.65 + levels.agent * 0.55));
        alpha = (1 - phase) * (0.16 + levels.agent * 0.34);
        ringColor = mix(TEAL, AMBER, 1);
      } else if (!reduceMotion && state === "user") {
        // a ring pulses inward from the edge toward the disc
        const speed = 0.7 + levels.user * 1.6;
        const phase = (elapsed * speed + spread) % 1;
        ringRadius = base * (1.85 - phase * (0.65 + levels.user * 0.55));
        alpha = phase * (0.16 + levels.user * 0.34);
        ringColor = TEAL;
      } else if (state === "connecting") {
        alpha *= 0.5 + 0.3 * breathe;
      }

      if (alpha <= 0.004) continue;
      ctx.beginPath();
      ctx.strokeStyle = rgba(ringColor, alpha);
      ctx.lineWidth = lineWidth;
      const span = Math.PI * 1.4;
      const start = rotation + i * (Math.PI / 2.3);
      ctx.arc(cx, cy, Math.max(1, ringRadius), start, start + span);
      ctx.stroke();
    }
  }

  function loop(now) {
    raf = null;
    if (destroyed) return;
    draw(now);
    if (visible && !document.hidden) {
      raf = requestAnimationFrame(loop);
    }
  }
  function ensureLoop() {
    if (destroyed || reduceMotion || raf != null) return;
    if (!visible || document.hidden) return;
    raf = requestAnimationFrame(loop);
  }
  function stopLoop() {
    if (raf != null) {
      cancelAnimationFrame(raf);
      raf = null;
    }
  }

  const io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) visible = entry.isIntersecting && entry.intersectionRatio > 0;
      if (visible) {
        resize();
        ensureLoop();
      } else {
        stopLoop();
      }
    },
    { threshold: 0.01 },
  );
  io.observe(canvas);

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);

  function onVisibilityChange() {
    if (document.hidden) stopLoop();
    else ensureLoop();
  }
  document.addEventListener("visibilitychange", onVisibilityChange);

  resize();
  if (reduceMotion) {
    draw(performance.now());
  } else {
    ensureLoop();
  }

  return {
    setState(next) {
      if (state === next) return;
      state = next;
      if (reduceMotion) draw(performance.now());
    },
    setLevels(next) {
      if (next.agent != null) levels.agent = next.agent;
      if (next.user != null) levels.user = next.user;
      if (reduceMotion) draw(performance.now());
    },
    destroy() {
      destroyed = true;
      stopLoop();
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    },
  };
}

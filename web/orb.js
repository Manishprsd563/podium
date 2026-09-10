// Podium's orb: a WebGL shader blob (icosphere + simplex-noise displacement +
// fresnel rim/subsurface glow + additive halo + orbiting loading ring),
// rendered with a transparent clear colour onto the page background.
// createOrb(canvas) owns a single render loop hooked into gsap.ticker (so it
// shares the same clock as every other GSAP animation on the page), pausing
// whenever the canvas is off-screen or the tab is hidden. Falls back to a
// lightweight 2D-canvas orb with the same API when WebGL is unavailable.
//
// export function createOrb(canvas) -> { setState(name), setLevel(v), pulse(), destroy() }

import * as THREE from "three";
import { gsap } from "gsap";

// ---------------------------------------------------------------------------
// State -> uniform / colour table. Every field here is what setState() tweens
// toward over ~600ms; the state name itself is picked every frame by app.js's
// computeOrbState(), never by this file:
//   connecting / reconnecting -- not joined yet, or LiveKit is retrying (grey)
//   idle       -- connected, floor open, nobody audible (teal -> violet)
//   speaking   -- the agent's smoothed output level is above SPEAK_THRESHOLD
//   listening  -- the user's smoothed mic level is above SPEAK_THRESHOLD (violet -> teal)
//   micLive    -- the push-to-talk floor is the user's (green)
//   waiting    -- state.attention === "waiting" (amber, also breathes -- see startBreathe)
//   thinking   -- state.attention === "thinking", a line is being synthesized (grey)
//   disconnected -- was connected and dropped (red)
// `ring` (0|1) is the orbiting loading indicator, shown only while the coach is
// preparing something (connecting/reconnecting/thinking). `haloBase` is the halo's
// resting opacity; in `speaking` it also gains a live contribution from the smoothed
// agent level (see ampBase/haloBase handling in the render loop). `level` itself
// (setLevel(), 0..1) is app.js's smoothed RMS mic/output amplitude for whichever side
// currently holds the floor -- it drives noise shimmer and halo brightness every frame,
// independent of which state is active.
// ---------------------------------------------------------------------------
const STATES = {
  connecting: { colorA: "#8a94a6", colorB: "#5b6472", amp: 0.05, speed: 0.4, ring: 1, opacity: 0.75, haloBase: 0.1, rim: 1.6 },
  reconnecting: { colorA: "#8a94a6", colorB: "#5b6472", amp: 0.05, speed: 0.4, ring: 1, opacity: 0.75, haloBase: 0.1, rim: 1.6 },
  idle: { colorA: "#5ee1d0", colorB: "#7b8cff", amp: 0.12, speed: 0.6, ring: 0, opacity: 1, haloBase: 0.16, rim: 1.8 },
  speaking: { colorA: "#5ee1d0", colorB: "#7b8cff", amp: 0.12, speed: 1.4, ring: 0, opacity: 1, haloBase: 0.16, rim: 2.2 },
  listening: { colorA: "#8f7bff", colorB: "#5ee1d0", amp: 0.18, speed: 0.9, ring: 0, opacity: 1, haloBase: 0.2, rim: 2.0 },
  micLive: { colorA: "#3ddc84", colorB: "#1f9d5a", amp: 0.2, speed: 1.0, ring: 0, opacity: 1, haloBase: 0.22, rim: 2.1 },
  waiting: { colorA: "#f5c451", colorB: "#ffdf8a", amp: 0.1, speed: 0.5, ring: 0, opacity: 1, haloBase: 0.14, rim: 1.7 },
  thinking: { colorA: "#8a94a6", colorB: "#b6c0cf", amp: 0.08, speed: 0.5, ring: 1, opacity: 1, haloBase: 0.12, rim: 1.6 },
  disconnected: { colorA: "#ff5c5c", colorB: "#7a1f1f", amp: 0, speed: 0, ring: 0, opacity: 0.9, haloBase: 0.06, rim: 1.2 },
};
const DEFAULT_STATE = "connecting";
const TWEEN_DUR = 0.6;
const TWEEN_EASE = "power2.out";

// Ashima Arts / Stefan Gustavson 3D simplex noise (MIT). Standard webgl-noise
// implementation, unmodified apart from formatting.
const SNOISE_GLSL = `
vec3 mod289(vec3 x){return x - floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x - floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159 - 0.85373472095314 * r;}

float snoise(vec3 v){
  const vec2 C = vec2(1.0/6.0, 1.0/3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);

  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);

  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);

  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;

  i = mod289(i);
  vec4 p = permute(permute(permute(
            i.z + vec4(0.0, i1.z, i2.z, 1.0))
          + i.y + vec4(0.0, i1.y, i2.y, 1.0))
          + i.x + vec4(0.0, i1.x, i2.x, 1.0));

  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;

  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);

  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);

  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);

  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);

  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));

  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;

  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);

  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x;
  p1 *= norm.y;
  p2 *= norm.z;
  p3 *= norm.w;

  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}
`;

const BLOB_VERT = `
uniform float uTime;
uniform float uAmp;
uniform float uFreq;
uniform float uSpeed;
uniform float uLevel;
uniform float uPulse;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying float vDisp;

${SNOISE_GLSL}

void main() {
  vec3 p = position;
  float t = uTime * uSpeed;
  float n1 = snoise(p * uFreq + vec3(0.0, 0.0, t));
  float n2 = snoise(p * uFreq * 2.1 + vec3(t * 0.6, 11.3, 4.7)) * 0.5;
  float disp = n1 * 0.7 + n2 * 0.3;

  float shimmer = snoise(p * uFreq * 4.2 + vec3(t * 1.3, 3.1, 7.7)) * uLevel * 0.12;
  float ripple = sin(length(p) * 9.0 - uTime * 5.5) * uPulse * 0.14;

  vec3 displaced = p + normal * (disp * uAmp + shimmer + ripple);

  vDisp = disp;
  vNormal = normalize(normalMatrix * normal);
  vec4 worldPos = modelMatrix * vec4(displaced, 1.0);
  vWorldPos = worldPos.xyz;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(displaced, 1.0);
}
`;

const BLOB_FRAG = `
uniform vec3 uColorA;
uniform vec3 uColorB;
uniform float uRim;
uniform float uOpacity;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying float vDisp;

void main() {
  vec3 n = normalize(vNormal);
  vec3 viewDir = normalize(cameraPosition - vWorldPos);
  float ndv = clamp(dot(n, viewDir), 0.0, 1.0);
  float fresnel = pow(1.0 - ndv, 2.2);

  float mixT = clamp(vDisp * 0.5 + 0.5, 0.0, 1.0);
  vec3 base = mix(uColorA, uColorB, mixT);

  vec3 lightDir = normalize(vec3(0.4, 0.6, 0.8));
  float lambert = clamp(dot(n, lightDir), 0.0, 1.0);
  vec3 highlight = mix(base, vec3(1.0), 0.35) * pow(lambert, 2.0) * 0.5;

  vec3 rimColor = mix(uColorB, vec3(1.0), 0.5);
  vec3 color = base + highlight + rimColor * fresnel * uRim * 0.5;

  float alpha = uOpacity * clamp(0.55 + fresnel * 0.5 + lambert * 0.15, 0.0, 1.0);
  gl_FragColor = vec4(color, alpha);
}
`;

const HALO_VERT = `
varying vec3 vNormal;
varying vec3 vWorldPos;
void main() {
  vNormal = normalize(normalMatrix * normal);
  vec4 worldPos = modelMatrix * vec4(position, 1.0);
  vWorldPos = worldPos.xyz;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const HALO_FRAG = `
uniform vec3 uColor;
uniform float uOpacity;
varying vec3 vNormal;
varying vec3 vWorldPos;
void main() {
  vec3 viewDir = normalize(cameraPosition - vWorldPos);
  float fres = pow(1.0 - clamp(abs(dot(viewDir, normalize(vNormal))), 0.0, 1.0), 2.6);
  gl_FragColor = vec4(uColor, fres * uOpacity);
}
`;

const RING_VERT = `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

// Torus UV: v (uv.y) sweeps around the main loop, u (uv.x) sweeps the tube
// cross-section -- so dashes run along uv.y and the soft edge fades along uv.x.
const RING_FRAG = `
uniform vec3 uColor;
uniform float uOpacity;
uniform float uTime;
varying vec2 vUv;
void main() {
  float dashes = 16.0;
  float t = fract(vUv.y * dashes - uTime * 0.5);
  float dash = smoothstep(0.0, 0.06, t) - smoothstep(0.5, 0.58, t);
  float edge = smoothstep(0.0, 0.2, vUv.x) * smoothstep(1.0, 0.8, vUv.x);
  float alpha = dash * edge * uOpacity;
  if (alpha <= 0.001) discard;
  gl_FragColor = vec4(uColor, alpha);
}
`;

function clamp01(v) {
  return Math.max(0, Math.min(1, v));
}

function colorTween(colorObj, hex, duration, ease) {
  const target = new THREE.Color(hex);
  gsap.to(colorObj, { r: target.r, g: target.g, b: target.b, duration, ease, overwrite: true });
}

// ---------------------------------------------------------------------------
// WebGL implementation
// ---------------------------------------------------------------------------
function createWebglOrb(canvas, reduceMotion) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    alpha: true,
    antialias: true,
    powerPreference: "high-performance",
  });
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();

  const BLOB_RADIUS = 1;
  const HALO_RADIUS = 1.42;
  const RING_RADIUS = 1.75;

  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10);
  camera.position.set(0, 0, 4);
  camera.lookAt(0, 0, 0);

  // -- blob -------------------------------------------------------------
  const geometry = new THREE.IcosahedronGeometry(BLOB_RADIUS, 5);
  const uniforms = {
    uTime: { value: 0 },
    uAmp: { value: STATES.idle.amp },
    uFreq: { value: 1.7 },
    uSpeed: { value: STATES.idle.speed },
    uLevel: { value: 0 },
    uPulse: { value: 0 },
    uColorA: { value: new THREE.Color(STATES.idle.colorA) },
    uColorB: { value: new THREE.Color(STATES.idle.colorB) },
    uRim: { value: STATES.idle.rim },
    uOpacity: { value: STATES.idle.opacity },
  };
  const material = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: BLOB_VERT,
    fragmentShader: BLOB_FRAG,
    transparent: true,
  });
  const blob = new THREE.Mesh(geometry, material);
  blob.renderOrder = 0;
  scene.add(blob);

  // -- halo (additive atmosphere glow, back-face fresnel) ----------------
  const haloGeometry = new THREE.IcosahedronGeometry(HALO_RADIUS, 3);
  const haloUniforms = {
    uColor: { value: new THREE.Color(STATES.idle.colorA) },
    uOpacity: { value: STATES.idle.haloBase },
  };
  const haloMaterial = new THREE.ShaderMaterial({
    uniforms: haloUniforms,
    vertexShader: HALO_VERT,
    fragmentShader: HALO_FRAG,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.BackSide,
  });
  const halo = new THREE.Mesh(haloGeometry, haloMaterial);
  halo.renderOrder = 2;
  scene.add(halo);

  // -- orbiting loading ring ---------------------------------------------
  const ringGeometry = new THREE.TorusGeometry(RING_RADIUS, 0.012, 8, 128);
  const ringUniforms = {
    uColor: { value: new THREE.Color(STATES.connecting.colorA) },
    uOpacity: { value: 0 },
    uTime: { value: 0 },
  };
  const ringMaterial = new THREE.ShaderMaterial({
    uniforms: ringUniforms,
    vertexShader: RING_VERT,
    fragmentShader: RING_FRAG,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
  });
  const ring = new THREE.Mesh(ringGeometry, ringMaterial);
  ring.rotation.x = Math.PI * 0.42;
  ring.rotation.y = Math.PI * 0.08;
  ring.renderOrder = 1;
  scene.add(ring);

  // -- mutable render-loop state ------------------------------------------
  let state = DEFAULT_STATE;
  let destroyed = false;
  let visible = true;
  let levelTarget = 0;
  let smoothedLevel = 0;
  const ampScale = reduceMotion ? 0.3 : 1;
  const ringSpeed = reduceMotion ? 0.12 : 0.5;
  const ampBase = { v: STATES.idle.amp };
  const haloBase = { v: STATES.idle.haloBase };
  let breatheTween = null;

  // Idle "breathing" scale pulse, run only while `state === "waiting"` (the coach is
  // static -- no shimmer speed to read as "alive") so the orb still visibly breathes.
  function stopBreathe() {
    if (breatheTween) {
      breatheTween.kill();
      breatheTween = null;
      gsap.to(blob.scale, { x: 1, y: 1, z: 1, duration: 0.4, ease: TWEEN_EASE, overwrite: true });
    }
  }
  function startBreathe() {
    if (reduceMotion || breatheTween) return;
    breatheTween = gsap.to(blob.scale, {
      x: 1.06,
      y: 1.06,
      z: 1.06,
      duration: 0.8,
      ease: "sine.inOut",
      yoyo: true,
      repeat: -1,
    });
  }

  // Tweens every uniform/colour toward `next`'s STATES config over TWEEN_DUR; called on
  // construction and on every setState(). Only `waiting` also runs the breathing loop.
  function applyState(next) {
    const cfg = STATES[next] || STATES.idle;
    gsap.to(ampBase, { v: cfg.amp * ampScale, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    gsap.to(haloBase, { v: cfg.haloBase, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    gsap.to(uniforms.uSpeed, { value: cfg.speed, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    gsap.to(uniforms.uRim, { value: cfg.rim, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    gsap.to(uniforms.uOpacity, { value: cfg.opacity, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    gsap.to(ringUniforms.uOpacity, { value: cfg.ring ? 0.9 : 0, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    colorTween(uniforms.uColorA.value, cfg.colorA, TWEEN_DUR, TWEEN_EASE);
    colorTween(uniforms.uColorB.value, cfg.colorB, TWEEN_DUR, TWEEN_EASE);
    colorTween(haloUniforms.uColor.value, cfg.colorA, TWEEN_DUR, TWEEN_EASE);
    colorTween(ringUniforms.uColor.value, cfg.colorA, TWEEN_DUR, TWEEN_EASE);
    if (next === "waiting") startBreathe();
    else stopBreathe();
  }

  applyState(state);

  // -- sizing ---------------------------------------------------------------
  let dpr = Math.min(window.devicePixelRatio || 1, 2);

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width || canvas.clientWidth || 1));
    const h = Math.max(1, Math.round(rect.height || canvas.clientHeight || 1));
    renderer.setPixelRatio(dpr);
    renderer.setSize(w, h, false);

    const aspect = w / h;
    const halfMin = HALO_RADIUS / 0.7; // blob+halo fills ~70% of the smaller box dimension
    if (aspect >= 1) {
      camera.top = halfMin;
      camera.bottom = -halfMin;
      camera.left = -halfMin * aspect;
      camera.right = halfMin * aspect;
    } else {
      camera.left = -halfMin;
      camera.right = halfMin;
      camera.top = halfMin / aspect;
      camera.bottom = -halfMin / aspect;
    }
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);
  }

  // -- render loop (shares gsap's clock) -------------------------------------
  // Per-frame update, driven by gsap.ticker so the orb shares the exact clock every other
  // GSAP tween in the app uses. `smoothedLevel` snaps up fast and decays slowly (asymmetric
  // rate) so a level spike reads clearly without flickering; it only tracks level while
  // speaking/micLive holds the floor, per app.js's computeOrbState().
  function onTick(_time, deltaMs) {
    if (destroyed || !visible || (typeof document !== "undefined" && document.hidden)) return;
    const dt = Math.min((deltaMs || 16.7) / 1000, 0.1);

    if (state !== "disconnected") {
      uniforms.uTime.value += dt;
      ringUniforms.uTime.value += dt;
      ring.rotation.z += dt * ringSpeed;
    }

    const target = (state === "speaking" || state === "micLive") ? levelTarget : 0;
    const rate = target > smoothedLevel ? 0.35 : 0.08;
    smoothedLevel += (target - smoothedLevel) * rate;

    uniforms.uAmp.value = ampBase.v + smoothedLevel * 0.55;
    uniforms.uLevel.value = smoothedLevel;
    haloUniforms.uOpacity.value = haloBase.v + smoothedLevel * 0.7;
    halo.scale.setScalar(1 + smoothedLevel * 0.35);

    renderer.render(scene, camera);
  }
  gsap.ticker.add(onTick);

  // -- visibility gating (IntersectionObserver + tab visibility) ------------
  const io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) visible = entry.isIntersecting && entry.intersectionRatio > 0;
      if (visible) resize();
    },
    { threshold: 0.01 },
  );
  io.observe(canvas);

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);

  function onVisibilityChange() {
    if (!document.hidden) resize();
  }
  document.addEventListener("visibilitychange", onVisibilityChange);

  // -- WebGL context loss ---------------------------------------------------
  // three.js does not pause draws on its own: `onTick` would keep calling
  // renderer.render() into a dead context every frame (driver reset, GPU switch, tab
  // woken from sleep) until the page reloads. Stop the ticker while the context is lost
  // and resume it once restored; three.js's own restore handler re-creates the lost GL
  // objects from geometry/material state that's still in JS memory.
  function onContextLost(event) {
    event.preventDefault(); // required for "webglcontextrestored" to ever fire
    gsap.ticker.remove(onTick);
  }
  function onContextRestored() {
    gsap.ticker.add(onTick);
    resize();
  }
  canvas.addEventListener("webglcontextlost", onContextLost, false);
  canvas.addEventListener("webglcontextrestored", onContextRestored, false);

  resize();

  return {
    setState(next) {
      if (!STATES[next] || next === state) return;
      state = next;
      applyState(next);
    },
    setLevel(v) {
      levelTarget = clamp01(v);
    },
    pulse() {
      const peak = reduceMotion ? 0.3 : 1;
      gsap.killTweensOf(uniforms.uPulse);
      gsap
        .timeline({ overwrite: true })
        .to(uniforms.uPulse, { value: peak, duration: 0.35, ease: "power2.out" })
        .to(uniforms.uPulse, { value: 0, duration: 0.55, ease: "power2.in" });
    },
    destroy() {
      destroyed = true;
      gsap.ticker.remove(onTick);
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      canvas.removeEventListener("webglcontextrestored", onContextRestored);
      stopBreathe();
      gsap.killTweensOf([
        uniforms.uColorA.value,
        uniforms.uColorB.value,
        uniforms.uSpeed,
        uniforms.uRim,
        uniforms.uOpacity,
        uniforms.uPulse,
        haloUniforms.uColor.value,
        haloUniforms.uOpacity,
        ringUniforms.uColor.value,
        ringUniforms.uOpacity,
        ampBase,
        haloBase,
        blob.scale,
      ]);
      geometry.dispose();
      material.dispose();
      haloGeometry.dispose();
      haloMaterial.dispose();
      ringGeometry.dispose();
      ringMaterial.dispose();
      renderer.dispose();
    },
  };
}

// ---------------------------------------------------------------------------
// 2D canvas fallback (same API), used when WebGLRenderer construction throws.
// ---------------------------------------------------------------------------
function createFallbackOrb(canvas, reduceMotion) {
  const ctx = canvas.getContext("2d");
  let dpr = Math.max(1, window.devicePixelRatio || 1);
  let w = 0;
  let h = 0;
  let visible = true;
  let destroyed = false;
  let state = DEFAULT_STATE;
  let levelTarget = 0;
  let smoothedLevel = 0;
  let ringAngle = 0;
  const t0 = performance.now();

  const colorA = new THREE.Color(STATES.idle.colorA);
  const colorB = new THREE.Color(STATES.idle.colorB);
  const params = { scale: 1, ring: 0 };
  let breatheTween = null;

  function stopBreathe() {
    if (breatheTween) {
      breatheTween.kill();
      breatheTween = null;
      gsap.to(params, { scale: 1, duration: 0.4, overwrite: true });
    }
  }
  function startBreathe() {
    if (reduceMotion || breatheTween) return;
    breatheTween = gsap.to(params, { scale: 1.06, duration: 0.8, ease: "sine.inOut", yoyo: true, repeat: -1 });
  }

  function applyState(next) {
    const cfg = STATES[next] || STATES.idle;
    colorTween(colorA, cfg.colorA, TWEEN_DUR, TWEEN_EASE);
    colorTween(colorB, cfg.colorB, TWEEN_DUR, TWEEN_EASE);
    gsap.to(params, { ring: cfg.ring ? 1 : 0, duration: TWEEN_DUR, ease: TWEEN_EASE, overwrite: true });
    if (next === "waiting") startBreathe();
    else stopBreathe();
  }
  applyState(state);

  function rgba(c, a) {
    return `rgba(${Math.round(c.r * 255)}, ${Math.round(c.g * 255)}, ${Math.round(c.b * 255)}, ${Math.max(0, Math.min(1, a))})`;
  }

  function resize() {
    dpr = Math.max(1, window.devicePixelRatio || 1);
    const rect = canvas.getBoundingClientRect();
    w = Math.max(1, Math.round(rect.width || canvas.width || 1));
    h = Math.max(1, Math.round(rect.height || canvas.height || 1));
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  // Approximates the WebGL blob's fresnel/shimmer look with a radial gradient plus a
  // sine "breathing" term in place of noise displacement -- close enough at a glance,
  // far cheaper on a GPU-less fallback path. 2D canvas contexts aren't lost the way
  // WebGL contexts are, so this path needs no contextlost/restored handling.
  function draw() {
    const elapsed = (performance.now() - t0) / 1000;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const base = Math.min(w, h) * 0.3;

    const target = (state === "speaking" || state === "micLive") ? levelTarget : 0;
    const rate = target > smoothedLevel ? 0.35 : 0.08;
    smoothedLevel += (target - smoothedLevel) * rate;

    const cfg = STATES[state] || STATES.idle;
    const ampScale = reduceMotion ? 0.3 : 1;
    const breathe = Math.sin(elapsed * (cfg.speed || 0.3) * 2) * 0.5 + 0.5;
    const radius = base * params.scale * (1 + (cfg.amp * ampScale + smoothedLevel * 0.55) * 0.6 * breathe);

    const grad = ctx.createRadialGradient(cx, cy, radius * 0.1, cx, cy, radius * 1.8);
    grad.addColorStop(0, rgba(colorA, 0.9));
    grad.addColorStop(0.55, rgba(colorB, 0.4 + smoothedLevel * 0.3));
    grad.addColorStop(1, rgba(colorB, 0));
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 1.8, 0, Math.PI * 2);
    ctx.fill();

    ctx.beginPath();
    ctx.fillStyle = rgba(colorA, cfg.opacity);
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();

    if (params.ring > 0.01) {
      ringAngle += (reduceMotion ? 0.008 : 0.03) * cfg.speed;
      ctx.beginPath();
      ctx.strokeStyle = rgba(colorA, 0.55 * params.ring);
      ctx.lineWidth = Math.max(1, base * 0.045);
      ctx.arc(cx, cy, radius * 1.55, ringAngle, ringAngle + Math.PI * 1.3);
      ctx.stroke();
    }
  }

  function onTick() {
    if (destroyed || !visible || document.hidden) return;
    draw();
  }
  gsap.ticker.add(onTick);

  const io = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) visible = entry.isIntersecting && entry.intersectionRatio > 0;
      if (visible) resize();
    },
    { threshold: 0.01 },
  );
  io.observe(canvas);

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);

  function onVisibilityChange() {
    if (!document.hidden) resize();
  }
  document.addEventListener("visibilitychange", onVisibilityChange);

  resize();

  return {
    setState(next) {
      if (!STATES[next] || next === state) return;
      state = next;
      applyState(next);
    },
    setLevel(v) {
      levelTarget = clamp01(v);
    },
    pulse() {
      gsap.killTweensOf(params);
      gsap.to(params, { scale: 1.12, duration: 0.35, ease: "power2.out", yoyo: true, repeat: 1 });
    },
    destroy() {
      destroyed = true;
      gsap.ticker.remove(onTick);
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      stopBreathe();
      gsap.killTweensOf([colorA, colorB, params]);
    },
  };
}

export function createOrb(canvas) {
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  try {
    return createWebglOrb(canvas, reduceMotion);
  } catch (err) {
    console.warn("[orb] WebGL unavailable, falling back to 2D canvas orb", err);
    return createFallbackOrb(canvas, reduceMotion);
  }
}

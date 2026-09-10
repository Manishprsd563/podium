// Podium orb — deterministic Three.js port of web/orb.js for HyperFrames renders.
//
// web/orb.js drives the orb from gsap.ticker (a free-running RAF loop) and animates state
// transitions with gsap.to() color/scalar tweens that accumulate across frames. Neither is
// compatible with a seekable renderer: HyperFrames samples frames out of order / in parallel,
// so every visual has to be a pure function of an explicit time value, not of "how many frames
// have played so far". This module keeps orb.js's shaders, geometry, and STATES palette
// byte-for-byte, but replaces the ticker + tween machinery with a single render(time, state,
// level) call the composition's own GSAP timeline drives via onUpdate — no RAF, no ticker, no
// internal clock accumulation, no per-call history. State cuts are instantaneous (matching how
// every composition in this project already swaps orb CSS classes with a hard tl.set(), not a
// crossfade), and the audio-reactive amplitude comes entirely from the `level` argument the
// caller computes from its own known VO timing.
//
// window.THREE must be assigned before mount() is called; this file only reads it lazily
// inside mount(), so it is safe to load before the THREE module script that sets it.

(function () {
  "use strict";

  // ---------------------------------------------------------------------------
  // State -> uniform / colour table. Verbatim copy of web/orb.js STATES.
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

  // Ashima Arts / Stefan Gustavson 3D simplex noise (MIT). Verbatim copy of web/orb.js.
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

  // Analytic replica of web/orb.js's pulse() one-shot tween (0.35s power2.out
  // rise, 0.55s power2.in fall) — evaluated purely from elapsed time so a
  // one-shot "pulse" accent stays a pure function of the render time, with no
  // stored tween/call-order state.
  function pulseEnvelope(elapsed) {
    const UP = 0.35;
    const DOWN = 0.55;
    if (elapsed < 0) return 0;
    if (elapsed <= UP) {
      const p = elapsed / UP;
      return 1 - (1 - p) * (1 - p); // power2.out, 0 -> 1
    }
    const d = elapsed - UP;
    if (d >= DOWN) return 0;
    const p = d / DOWN;
    return 1 - p * p; // power2.in, 1 -> 0
  }

  // Analytic replica of the "waiting" breathe loop (gsap yoyo scale 1 -> 1.06,
  // 0.8s each way, sine.inOut, infinite) as a deterministic triangle-into-sine
  // wave of period 1.6s.
  function breatheScale(time) {
    const period = 1.6;
    const phase = ((time % period) + period) % period;
    const half = phase < period / 2 ? phase / (period / 2) : 2 - phase / (period / 2);
    const eased = (1 - Math.cos(Math.PI * half)) / 2; // sine.inOut
    return 1 + 0.06 * eased;
  }

  // 2D-canvas fallback mirroring web/orb.js's createCanvasOrb: same STATES palette,
  // same render(time, state, level) contract, used only when WebGL is unavailable.
  function mount2d(canvas, opts) {
    const ctx = canvas.getContext("2d");
    const pulses = Array.isArray(opts.pulses) ? opts.pulses.slice() : [];
    let destroyed = false;
    function resize(width, height) {
      canvas.width = Math.max(1, Math.round(width || canvas.width || 1));
      canvas.height = Math.max(1, Math.round(height || canvas.height || 1));
    }
    function render(time, state, level) {
      if (destroyed || !ctx) return;
      const t = typeof time === "number" && isFinite(time) ? time : 0;
      const cfg = STATES[state] || STATES[DEFAULT_STATE];
      const active = state === "speaking" || state === "micLive" ? clamp01(level || 0) : 0;
      let pulse = 0;
      for (let i = 0; i < pulses.length; i++) pulse = Math.max(pulse, pulseEnvelope(t - pulses[i]));
      const w = canvas.width, h = canvas.height, cx = w / 2, cy = h / 2;
      const r = Math.min(w, h) * 0.35 * (1 + active * 0.12 + pulse * 0.06) * (state === "waiting" ? breatheScale(t) : 1);
      ctx.clearRect(0, 0, w, h);
      ctx.globalAlpha = cfg.haloBase + active * 0.7;
      const halo = ctx.createRadialGradient(cx, cy, r * 0.8, cx, cy, r * 1.42);
      halo.addColorStop(0, cfg.colorA);
      halo.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(cx, cy, r * 1.42, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = cfg.opacity;
      const g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r);
      g.addColorStop(0, cfg.colorA);
      g.addColorStop(1, cfg.colorB);
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();
      if (cfg.ring) {
        ctx.globalAlpha = 0.9;
        ctx.strokeStyle = cfg.colorA;
        ctx.lineWidth = Math.max(1, r * 0.012);
        ctx.beginPath(); ctx.arc(cx, cy, r * 1.75, t * 0.5, t * 0.5 + Math.PI * 1.2); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    resize(opts.width, opts.height);
    return { render, resize, destroy() { destroyed = true; } };
  }

  // Public entry: defers all WebGL work (context + shader compile, slow under
  // software rendering) to the first render() call so page load never blocks on it.
  function mount(canvas, options) {
    const opts = options || {};
    if (window.PODIUM_ORB_FORCE_2D) return mount2d(canvas, opts);
    let impl = null;
    let size = { w: opts.width, h: opts.height };
    function ensure() {
      if (!impl) {
        impl = mountGL(canvas, Object.assign({}, opts, { width: size.w, height: size.h }));
      }
      return impl;
    }
    return {
      render(time, state, level) { ensure().render(time, state, level); },
      resize(w, h) { size = { w, h }; if (impl) impl.resize(w, h); },
      destroy() { if (impl) impl.destroy(); },
    };
  }

  function mountGL(canvas, options) {
    const opts = options || {};
    const THREE = window.THREE;
    if (!THREE) {
      throw new Error("PodiumOrb.mount: window.THREE must be assigned before mount() is called");
    }

    const reduceMotion = !!opts.reduceMotion;
    const ampScale = reduceMotion ? 0.3 : 1;
    const ringSpeed = reduceMotion ? 0.12 : 0.5;
    const pulses = Array.isArray(opts.pulses) ? opts.pulses.slice() : [];

    let renderer;
    try {
      renderer = new THREE.WebGLRenderer({
        canvas,
        alpha: true,
        antialias: true,
        powerPreference: "high-performance",
      });
    } catch (err) {
      console.warn("PodiumOrb: WebGL unavailable, using 2D fallback", err);
      return mount2d(canvas, opts);
    }
    renderer.setClearColor(0x000000, 0);
    // Renders are captured frame-by-frame at a fixed resolution, so pixel
    // ratio is pinned to 1 rather than read from devicePixelRatio.
    renderer.setPixelRatio(1);

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

    let destroyed = false;
    let lastColorState = null;

    function applyStateColors(name) {
      if (name === lastColorState) return;
      const cfg = STATES[name] || STATES.idle;
      uniforms.uColorA.value.set(cfg.colorA);
      uniforms.uColorB.value.set(cfg.colorB);
      haloUniforms.uColor.value.set(cfg.colorA);
      ringUniforms.uColor.value.set(cfg.colorA);
      lastColorState = name;
    }

    function resize(width, height) {
      const w = Math.max(1, Math.round(width || canvas.width || 1));
      const h = Math.max(1, Math.round(height || canvas.height || 1));
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
    }

    function render(time, state, level) {
      if (destroyed) return;
      const t = typeof time === "number" && isFinite(time) ? time : 0;
      const lvl = clamp01(level || 0);
      const stateName = STATES[state] ? state : DEFAULT_STATE;
      const cfg = STATES[stateName];

      applyStateColors(stateName);

      uniforms.uTime.value = t;
      uniforms.uSpeed.value = cfg.speed;
      uniforms.uRim.value = cfg.rim;
      uniforms.uOpacity.value = cfg.opacity;

      // Matches web/orb.js: the smoothed agent level only contributes amplitude
      // while the orb is actually speaking/mic-live, zero in every other state.
      const activeLevel = stateName === "speaking" || stateName === "micLive" ? lvl : 0;
      uniforms.uAmp.value = cfg.amp * ampScale + activeLevel * 0.55;
      uniforms.uLevel.value = activeLevel;

      let pulse = 0;
      for (let i = 0; i < pulses.length; i++) {
        const p = pulseEnvelope(t - pulses[i]);
        if (p > pulse) pulse = p;
      }
      uniforms.uPulse.value = pulse;

      haloUniforms.uOpacity.value = cfg.haloBase + activeLevel * 0.7;
      halo.scale.setScalar(1 + activeLevel * 0.35);

      ringUniforms.uTime.value = t;
      ringUniforms.uOpacity.value = cfg.ring ? 0.9 : 0;
      ring.rotation.z = t * ringSpeed;

      blob.scale.setScalar(stateName === "waiting" ? breatheScale(t) : 1);

      renderer.render(scene, camera);
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      geometry.dispose();
      material.dispose();
      haloGeometry.dispose();
      haloMaterial.dispose();
      ringGeometry.dispose();
      ringMaterial.dispose();
      renderer.dispose();
    }

    resize(opts.width, opts.height);

    return { render, resize, destroy };
  }

  window.PodiumOrb = { mount, STATES };
})();

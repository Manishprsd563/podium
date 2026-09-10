# Podium pitch video — UI kit

`tokens.css` (custom properties only) + `components.css` (static classes,
zero `@keyframes`/`animation:`/`transition:`) rebuild the real Podium
product look — `web/style.css`, `web/dashboard.css`, `web/orb.js` — as a
class-based kit for a 1920x1080 HyperFrames canvas. Scenes link both files:

```html
<link rel="stylesheet" href="../ui/tokens.css">
<link rel="stylesheet" href="../ui/components.css">
```

All motion is scene-owned GSAP: tween `transform`/`opacity` directly, or
tween the CSS custom properties listed under each component (e.g.
`gsap.to(bar, { "--v": 4, duration: 1 })`). No component declares its own
transition or animation, so every value change is instantly seek-safe.

Every colour in `components.css` is a `var()` — literal hex only exists in
`tokens.css`.

## Tokens (`tokens.css`)

Surfaces: `--bg`, `--bg-rgb`, `--surface`, `--surface-rgb`, `--surface-2`, `--surface-2-rgb`.
Ink: `--ink`, `--ink-rgb`, `--ink-2`, `--ink-3`.
Hairlines: `--hair`, `--hair-2`.
Accents: `--teal` / `--teal-rgb`, `--peri` / `--peri-rgb`, `--violet` / `--violet-rgb`,
`--amber` / `--amber-rgb`, `--red` / `--red-rgb`, `--green` / `--green-rgb`,
`--mic` / `--mic-rgb`, `--think` / `--think-rgb`, `--pd-you` (= `--red`), `--pd-coach` (= `--green`).
Orb duotones (`orb.js` `STATES`): `--orb-idle-a/-b`, `--orb-listen-a/-b`, `--orb-mic-a/-b`,
`--orb-wait-a/-b`, `--orb-think-a/-b`, `--orb-connect-a/-b`, `--orb-off-a/-b`, each with a
`-rgb` sibling (e.g. `--orb-idle-a-rgb`) for alpha compositing in gradients.
Type: `--font`, `--font-mono`, `--ls-tight`, `--ls-normal`, `--ls-wide`, `--ls-wider`.
Shape/elevation: `--radius`, `--radius-sm`, `--radius-pill`, `--shadow-card`,
`--shadow-glow-teal`, `--shadow-glow-amber`, `--shadow-glow-red`, `--shadow-glow-green`.
Reference timing (informational only, never applied as `transition`): `--ease`, `--dur`, `--dur-slow`.
Layout: `--statusbar-h`.

## Components (`components.css`)

### `.pd-statusbar`
Bottom status bar, exact segment order from `web/app.js renderStatusbar()` /
SCRIPT.md's badge text. Children: `.pd-statusbar-seg` (plain segment),
`.pd-statusbar-seg.is-badge` (provider segment — teal text + teal underline,
this is the "rebuilt at full resolution, teal underline" punch-in in S11),
`.pd-statusbar-dot` (small separator dot), `.pd-level`/`.pd-level-fill`
(mic meter — set `--lvl` 0..1 on the fill, it scales via `scaleX`).

```html
<div class="pd-statusbar">
  <span class="pd-statusbar-seg">connected</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">agent present</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg is-badge">rime · mistv3 · thunder</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">listening</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">present</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">deck_ack</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">mic live · hold space to talk</span>
  <span class="pd-statusbar-dot"></span>
  <span class="pd-statusbar-seg">podium-fqamkqva</span>
  <div class="pd-level" style="--lvl:.6"><div class="pd-level-fill"></div></div>
</div>
```

CSS vars: `--lvl` (0..1, on `.pd-level-fill`).

### `.pd-orb`
3-layer glow rebuild of `orb.js`'s WebGL blob: `.pd-orb-halo` (huge blurred
outer bloom), `.pd-orb-core` (main gradient sphere, scales with `--pulse`),
`.pd-orb-rim` (fresnel-style rim highlight, `mix-blend-mode: screen`). State
modifier classes swap the duotone: `.is-idle` / `.is-speaking` (teal→periwinkle),
`.is-listening` (violet→teal), `.is-mic-live` (green), `.is-waiting` (amber),
`.is-thinking` (grey, dotted pulse), `.is-connecting` (grey), `.is-disconnected` (red).

```html
<div class="pd-orb is-idle" style="--orb-size:220px">
  <div class="pd-orb-halo"></div>
  <div class="pd-orb-core"></div>
  <div class="pd-orb-rim"></div>
</div>
```

CSS vars: `--orb-size` (px, on `.pd-orb`), `--pulse` (0..~1, breathing/pulse
scale on the core), `--glow` (0..~1, halo + core shadow intensity).

### `.pd-caption`
```html
<p class="pd-caption-label">Maya</p>
<p class="pd-caption is-maya is-interim">so, like, smart cities are&hellip;</p>
```
Modifiers: `.is-interim` (grey, streaming) / `.is-final` (white, settled);
speaker colour `.is-narrator` (ink), `.is-maya` (violet), `.is-coach` (teal).
Apply the speaker class only once a line is final — interim always reads grey.

### `.pd-chips` / `.pd-chip`
```html
<div class="pd-chips">
  <button class="pd-chip is-on">Novice</button>
  <button class="pd-chip">Speaker</button>
  <button class="pd-chip">Presenter</button>
  <button class="pd-chip">TEDx</button>
</div>
```
`.is-on` = selected chip (teal border/glow), rebuilt from `#choices .choice`.

### `.pd-slide`
```html
<div class="pd-slide">
  <span class="pd-slide-counter">1 / 3</span>
  <h2 class="pd-slide-title">What is a smart city</h2>
  <ul class="pd-slide-bullets">
    <li>Networked sensors and shared data</li>
    <li>Coordinated traffic, energy, and safety systems</li>
    <li>Residents as both users and data sources</li>
  </ul>
  <p class="pd-slide-hint">Deck generated from your voice · or upload your own PDF</p>
</div>
```
Rebuilt from `#view-present` (`#present-title`/`#present-bullets`/`#present-counter`).

### `.pd-clock`
```html
<span class="pd-clock is-warn">0:42</span>
```
Rebuilt from `#present-clock` / `#present-clock.warn`.

### `.pd-wave`
```html
<div class="pd-wave"><canvas class="pd-wave-canvas"></canvas></div>
```
Empty shell sized for a scene-drawn waveform (canvas or SVG path); no
canvas logic lives in the kit.

### `.pd-meter`
```html
<div class="pd-meter">
  <span class="pd-meter-label">wpm</span>
  <span class="pd-meter-value"><span class="pd-meter-num">173</span><span class="pd-meter-unit">wpm</span></span>
  <span class="pd-meter-band is-fast">fast</span>
</div>
```
Band modifiers: `.is-fast` (amber), `.is-good` (green), `.is-slow` (periwinkle).

### `.pd-scorecard` / `.pd-bar`
```html
<div class="pd-scorecard">
  <div class="pd-bar">
    <span class="pd-bar-label">Delivery</span>
    <div class="pd-bar-track"><div class="pd-bar-fill" style="--v:2"></div></div>
    <span class="pd-bar-value"><span class="pd-bar-num">2</span><span class="pd-bar-max">/5</span>
      <span class="pd-bar-delta">+2</span></span>
  </div>
</div>
```
Rebuilt from `web/dashboard.css` `.pd-bars`/`.pd-bar` (renamed container).
CSS vars: `--v` (0..5, on `.pd-bar-fill`, drives track width). `.pd-bar-delta.is-flat`
= `±0` (ink-faint instead of teal).

### `.pd-metrics` / `.pd-metric`
```html
<p class="pd-metrics">
  <span class="pd-metric">151.8 wpm</span><span class="pd-metric-sep">·</span>
  <span class="pd-metric">10 pauses</span><span class="pd-metric-sep">·</span>
  <span class="pd-metric">longest 3.28 s</span>
</p>
```
Rebuilt from `.pd-metrics-strip`/`.pd-metric`/`.pd-metric-sep`.

### `.pd-imp` / `.pd-line`
```html
<div class="pd-imp">
  <p class="pd-imp-eyebrow">Improvement 1</p>
  <div class="pd-line is-you">
    <span class="pd-line-label">you said</span>
    <p class="pd-line-body">so, like, smart cities are, um, basically&hellip;</p>
  </div>
  <div class="pd-line is-say">
    <span class="pd-line-label">say it like this</span>
    <p class="pd-line-body">Smart cities connect sensors, traffic, and power.</p>
  </div>
  <div class="pd-line is-paced">
    <span class="pd-line-label">paced</span>
    <p class="pd-line-body">Smart cities connect sensors<span class="pd-pause-glyph">‖</span>traffic, and power.</p>
  </div>
</div>
```
Rebuilt from `.pd-said`/`.pd-stage-line`/`.pd-pause-glyph`. `.is-you` = red
(`--pd-you`), `.is-say` = green (`--pd-coach`), `.is-paced` = ink body with an
inline teal `.pd-pause-glyph` marking the held gap.

### `.pd-drill`
```html
<div class="pd-drill">
  <p class="pd-drill-word">services</p>
  <div class="pd-drill-ring is-warn" style="--pct:69">
    <span class="pd-drill-pct">69%</span>
  </div>
</div>
```
CSS vars: `--pct` (0..100, on `.pd-drill-ring`, sweeps the `conic-gradient`).
State modifiers set `--drill-color`: `.is-ok` (teal), `.is-warn` (amber),
`.is-fail` (red). The ring's `::before` punches the donut hole using
`var(--bg)` — place the ring over the canvas background, or override
`--bg` locally if nesting inside a lighter surface.

### `.pd-tiles` / `.pd-tile`
```html
<div class="pd-tiles">
  <div class="pd-tile is-lit">Pacing</div>
  <div class="pd-tile">Hook</div>
  <!-- ... 10 total -->
</div>
```
`.pd-tiles` is a 5-column grid shell for the 10 skill tiles (S09).
`.is-lit` = teal border/fill/glow (the skill this session touched).

### `.pd-ladder`
```html
<div class="pd-ladder" style="--pos:0">
  <div class="pd-ladder-line"></div>
  <div class="pd-ladder-marker">Maya</div>
  <div class="pd-ladder-notch is-active"><span class="pd-ladder-dot"></span><span class="pd-ladder-label">Novice</span></div>
  <div class="pd-ladder-notch"><span class="pd-ladder-dot"></span><span class="pd-ladder-label">Speaker</span></div>
  <div class="pd-ladder-notch"><span class="pd-ladder-dot"></span><span class="pd-ladder-label">Presenter</span></div>
  <div class="pd-ladder-notch"><span class="pd-ladder-dot"></span><span class="pd-ladder-label">TEDx</span></div>
</div>
```
CSS vars: `--pos` (0..3, on `.pd-ladder`, positions `.pd-ladder-marker` above
the matching notch). `.pd-ladder-notch.is-active`/`.is-done` light the dot + label.

### `.pd-sparkline`
```html
<div class="pd-sparkline"><svg viewBox="0 0 400 96" preserveAspectRatio="none">…</svg></div>
```
Empty shell (hairline baseline) sized for a scene-injected SVG polyline or
canvas trend line (S10 delta sparkline).

### `.pd-statement` / `.pd-statement-sub`
```html
<p class="pd-statement">No form. No setup. Just talk.</p>
<p class="pd-statement-sub">live coach voice over WebSocket · contrast clips over REST</p>
```
Large callout typography used for every scene's closing line.

### `.pd-evidence-chip`
```html
<span class="pd-evidence-chip">3 / 3 within ±450 ms · RIME_EVIDENCE.md claim 1</span>
```
Small mono citation pill for corner-of-frame evidence text.

### `.pd-card`
```html
<div class="pd-card">…</div>
```
Generic dark surface panel (radius + hairline + `--shadow-card`) other
components sit inside, e.g. the S11 provider card.

### `.pd-eyebrow`
```html
<p class="pd-eyebrow">Session s_20260909_160535</p>
```
Shared small-caps label utility used inside `.pd-imp`/`.pd-card`/free text.

## `preview.html`
Static 1920x1080 page instantiating every component above with sample copy
pulled from `SCRIPT.md`'s real/in-story numbers, for visual QA before any
scene links these files.

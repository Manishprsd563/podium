// Podium dashboard — cinematic post-presentation views (scores, improvement, drill, wrap).
// export function createDashboard(root, { send, gsap }) -> { update(state), destroy() }
// Renders inside `root`; the active view is `state.view`, which app.js's deriveView()
// derives from the §5 `phase`/`coach` data-channel messages (CONTRACTS.md). Each view
// factory below names the message(s) that drive it and the CONTRACTS.md section that
// defines its payload. No cards, hairlines only, all state via colour + motion.

const SKILL_IDS = [
  "hook", "structure", "pacing", "pausing", "fillers",
  "vocal-variety", "storytelling", "slide-connection", "closing", "articulation",
];

const RAIL_STAGES = [
  { key: "v0", label: "You" },
  { key: "v2", label: "Cleaner" },
  { key: "v3", label: "With pauses" },
  { key: "alt", label: "Another opening" },
  { key: "prompt", label: "Your turn" },
];

function titleCase(s) {
  return String(s || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Tweens a plain number from `from` to `to`, writing the rounded/formatted string into
// `el.textContent` every frame -- used for every animated score/percentage readout so
// bars and drill verdicts count up instead of snapping.
function countTo(gsap, el, from, to, { duration = 0.8, delay = 0, decimals = 0, ease = "power2.out", suffix = "" } = {}) {
  const obj = { v: Number(from) || 0 };
  return gsap.to(obj, {
    v: Number(to) || 0,
    duration,
    delay,
    ease,
    onUpdate: () => {
      el.textContent = (decimals === 0 ? String(Math.round(obj.v)) : obj.v.toFixed(decimals)) + suffix;
    },
  });
}

// Builds one row per key (label / track+fill+ghost / number+delta[+sparkline]) for the
// scores view and the wrap view's compact recap. `key` is a §3 judgment.scores key (an
// LLM-authored JSON object key) -- built with createElement/textContent rather than
// innerHTML so a hallucinated or adversarial key can never inject markup.
function buildScoreBars(container, keys, { compact = false } = {}) {
  container.className = "pd-bars" + (compact ? " pd-bars--compact" : "");
  container.innerHTML = "";
  const rows = {};
  keys.forEach((key) => {
    const row = document.createElement("div");
    row.className = "pd-bar";
    row.dataset.key = key;

    const label = document.createElement("div");
    label.className = "pd-bar-label";
    label.textContent = titleCase(key);
    row.appendChild(label);

    const track = document.createElement("div");
    track.className = "pd-bar-track";
    const ghost = document.createElement("div");
    ghost.className = "pd-bar-ghost";
    const fill = document.createElement("div");
    fill.className = "pd-bar-fill";
    track.appendChild(ghost);
    track.appendChild(fill);
    row.appendChild(track);

    const value = document.createElement("div");
    value.className = "pd-bar-value";
    const num = document.createElement("span");
    num.className = "pd-bar-num";
    num.textContent = "0";
    const max = document.createElement("span");
    max.className = "pd-bar-max";
    max.textContent = " / 5";
    const delta = document.createElement("span");
    delta.className = "pd-bar-delta";
    value.appendChild(num);
    value.appendChild(max);
    value.appendChild(delta);
    row.appendChild(value);

    let spark = null;
    if (compact) {
      spark = document.createElement("div");
      spark.className = "pd-bar-spark";
      row.appendChild(spark);
    }

    container.appendChild(row);
    rows[key] = { root: row, fill, ghost, num, delta, spark };
  });
  return rows;
}

// Renders a tiny inline SVG line+dot of the last N judgment revisions for one score key
// (wrap view only) -- cheap enough to build by hand, no charting library.
function buildSparkline(history, key) {
  const w = 56, h = 16, pad = 2;
  const vals = history.map((entry) => (entry.scores ? entry.scores[key] : null)).filter((v) => v != null);
  const n = vals.length;
  const stepX = n > 1 ? (w - 2 * pad) / (n - 1) : 0;
  const points = vals.map((v, i) => {
    const x = pad + i * stepX;
    const y = h - pad - ((v - 1) / 4) * (h - 2 * pad);
    return [x, y];
  });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("width", String(w));
  svg.setAttribute("height", String(h));
  svg.classList.add("pd-spark-svg");
  const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  poly.setAttribute("points", points.map((p) => p.map((n2) => n2.toFixed(1)).join(",")).join(" "));
  poly.setAttribute("fill", "none");
  poly.setAttribute("stroke", "var(--ink-faint)");
  poly.setAttribute("stroke-width", "1.5");
  svg.appendChild(poly);
  if (points.length) {
    const [lx, ly] = points[points.length - 1];
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", lx.toFixed(1));
    dot.setAttribute("cy", ly.toFixed(1));
    dot.setAttribute("r", "1.8");
    dot.setAttribute("fill", "var(--accent)");
    svg.appendChild(dot);
  }
  return svg;
}

// ---------------------------------------------------------------------------
// scores view -- driven by the §5 `coach` message with stage summary|ask_proceed;
// renders §3 judgment.scores/improvements and §2 metrics.
// ---------------------------------------------------------------------------
function createScoresView(el, { gsap, isReduced }) {
  function dur(n) { return isReduced() ? 0 : n; }

  const col = document.createElement("div");
  col.className = "pd-col";
  el.appendChild(col);

  const barsEl = document.createElement("div");
  col.appendChild(barsEl);

  const metricsEl = document.createElement("div");
  metricsEl.className = "pd-metrics-strip";
  col.appendChild(metricsEl);

  let rows = null;
  let keys = [];
  const metricSpans = {};
  let lastFocusSig = "";
  let lastScoresSig = null;

  function buildMetricsStrip(state) {
    metricsEl.innerHTML = "";
    Object.keys(metricSpans).forEach((k) => delete metricSpans[k]);
    const m = state.metrics;
    if (!m) return;
    const items = [];
    if (typeof m.wpm === "number") items.push(["wpm", `${Math.round(m.wpm)} wpm`]);
    if (m.fillers) items.push(["fillers", `${m.fillers.count} filler${m.fillers.count === 1 ? "" : "s"}`]);
    if (m.pauses) items.push(["pauses", `longest pause ${m.pauses.longest_s.toFixed(1)} s`]);
    if (m.time_budget) items.push(["time", `${Math.round(m.time_budget.used_s)} / ${Math.round(m.time_budget.budget_s)} s`]);
    if (m.pronunciation) items.push(["intelligibility", `intelligibility ${Math.round(m.pronunciation.intelligibility * 100)} %`]);
    items.forEach(([key, text], i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.className = "pd-metric-sep";
        sep.textContent = "\u00b7";
        metricsEl.appendChild(sep);
      }
      const span = document.createElement("span");
      span.className = "pd-metric";
      span.dataset.key = key;
      span.textContent = text;
      metricsEl.appendChild(span);
      metricSpans[key] = span;
    });
  }

  // Rebuilds the bar rows from §3 judgment.scores whenever the signature changes
  // (mount, or a new/superseded judgment). `judgmentPrevious.scores` (§5 `judgment.previous`)
  // draws the ghost tick and +/- delta so a re-judged score reads as a change, not a reset.
  function buildBars(state) {
    const judgment = state.judgment;
    if (!judgment || !judgment.scores) return;
    keys = Object.keys(judgment.scores);
    gsap.killTweensOf(barsEl.querySelectorAll("*"));
    rows = buildScoreBars(barsEl, keys, { compact: false });
    const prevScores = state.judgmentPrevious && state.judgmentPrevious.scores;
    keys.forEach((key, i) => {
      const score = judgment.scores[key];
      const pct = (score / 5) * 100;
      const row = rows[key];
      const prevScore = prevScores ? prevScores[key] : undefined;
      if (prevScore != null) {
        row.ghost.style.left = `${(prevScore / 5) * 100}%`;
        row.ghost.style.opacity = "1";
        const delta = score - prevScore;
        row.delta.textContent = delta === 0 ? "" : delta > 0 ? `+${delta}` : `${delta}`;
        row.delta.style.color = delta >= 0 ? "var(--good)" : "var(--warn)";
      } else {
        row.ghost.style.opacity = "0";
        row.delta.textContent = "";
      }
      gsap.set(row.fill, { width: prevScore != null ? `${(prevScore / 5) * 100}%` : "0%" });
      gsap.to(row.fill, { width: `${pct}%`, duration: dur(1.1), ease: "expo.out", delay: dur(i * 0.12) });
      countTo(gsap, row.num, prevScore != null ? prevScore : 0, score, { duration: dur(1.1), delay: dur(i * 0.12) });
      gsap.fromTo(row.root, { autoAlpha: 0, y: 14 }, { autoAlpha: 1, y: 0, duration: dur(0.6), delay: dur(i * 0.12), ease: "power2.out" });
    });
    buildMetricsStrip(state);
    gsap.fromTo(metricsEl, { autoAlpha: 0, y: 6 }, { autoAlpha: 1, y: 0, duration: dur(0.5), delay: dur(keys.length * 0.12 + 0.15) });
  }

  // Highlights/dims the bar or metric named by the §5 `focus` message, sent in sync with
  // the coach's spoken line so the UI points at whichever number is being talked about.
  function applyFocus(state) {
    const focus = state.focus;
    const target = focus && focus.target;
    const key = focus && focus.key;
    const sig = `${target || ""}:${key || ""}`;
    if (sig === lastFocusSig) return;
    lastFocusSig = sig;
    if (rows) {
      keys.forEach((k) => {
        const row = rows[k];
        const isFocused = target === "score" && k === key;
        const dim = target === "score" && !isFocused;
        gsap.to(row.root, { scale: isFocused ? 1.04 : 1, opacity: dim ? 0.45 : 1, duration: dur(0.35), ease: "power2.out" });
        gsap.to(row.fill, { opacity: isFocused ? 1 : dim ? 0.5 : 0.85, duration: dur(0.35) });
      });
    }
    Object.entries(metricSpans).forEach(([k, span]) => {
      const isFocused = target === "metric" && k === key;
      const dim = target === "metric" && !isFocused;
      gsap.to(span, { color: isFocused ? "var(--accent)" : "var(--ink-soft)", opacity: dim ? 0.5 : 1, duration: dur(0.3) });
    });
  }

  return {
    mount(state) {
      buildBars(state);
      lastScoresSig = JSON.stringify(state.judgment && state.judgment.scores);
      applyFocus(state);
    },
    update(state) {
      const sig = JSON.stringify(state.judgment && state.judgment.scores) + "|" + JSON.stringify(state.judgmentPrevious && state.judgmentPrevious.scores);
      if (sig !== lastScoresSig) {
        lastScoresSig = sig;
        buildBars(state);
      } else if (state.metrics && !metricsEl.children.length) {
        buildMetricsStrip(state);
      }
      applyFocus(state);
    },
    destroy() {
      gsap.killTweensOf(col.querySelectorAll("*"));
    },
  };
}

// ---------------------------------------------------------------------------
// improvement view -- driven by the §5 `coach` message with stage item|practice|verdict,
// stepped by §5 `feedback` messages; verdict numbers are §7 practice-verdict output
// (analysis/practice.py) -- the coach LLM may only phrase them, never compute them.
// ---------------------------------------------------------------------------
// Normalises a token for the added-word diff: lowercase, punctuation stripped.
function normalizeWord(w) {
  return w.toLowerCase().replace(/[^a-z0-9']/g, "");
}

function quoteWordSet(text) {
  const set = new Set();
  String(text || "")
    .split(/\s+/)
    .forEach((w) => {
      const n = normalizeWord(w);
      if (n) set.add(n);
    });
  return set;
}

function formatPause(ms) {
  const secs = (Number(ms) || 0) / 1000;
  return `\u00b7 pause ${secs.toFixed(1).replace(/\.0$/, "")}s \u00b7`;
}

// Renders pause-marked text (`<600>` glyphs) into `container`, underlining any
// word absent from `quoteText` so the delta between the original line and the
// coached one is legible at a glance. Cheap normalised-token diff, no library.
function renderMarkupBody(container, markup, quoteText) {
  const known = quoteWordSet(quoteText);
  String(markup || "")
    .split(/(<\d+>)/g)
    .forEach((part) => {
      const pause = part.match(/^<(\d+)>$/);
      if (pause) {
        const glyph = document.createElement("span");
        glyph.className = "pd-pause-glyph";
        glyph.textContent = formatPause(pause[1]);
        container.appendChild(glyph);
        return;
      }
      part.split(/(\s+)/).forEach((chunk) => {
        if (!chunk) return;
        if (/^\s+$/.test(chunk)) {
          container.appendChild(document.createTextNode(chunk));
          return;
        }
        const norm = normalizeWord(chunk);
        if (norm && known.size && !known.has(norm)) {
          const added = document.createElement("span");
          added.className = "pd-added";
          added.textContent = chunk;
          container.appendChild(added);
        } else {
          container.appendChild(document.createTextNode(chunk));
        }
      });
    });
}

// Builds one "who said it" transcript line: red ink + "you said" for the
// presenter's own recording, green ink + "say it like this" for the coach.
// `missing` renders a faint neutral line instead -- the v0 clip could not be
// cut, and the coach's voice must never stand in for the user's own words.
function buildSaidLine(role, { eyebrow, missing, build } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "pd-said " + (missing ? "pd-said--missing" : role === "user" ? "pd-said--you" : "pd-said--coach");
  if (eyebrow && !missing) {
    const eb = document.createElement("div");
    eb.className = "pd-said-eyebrow";
    eb.textContent = eyebrow;
    wrap.appendChild(eb);
  }
  const body = document.createElement("div");
  body.className = "pd-said-body";
  wrap.appendChild(body);
  if (build) build(body);
  return wrap;
}

function createImprovementView(el, { gsap, isReduced }) {
  function dur(n) { return isReduced() ? 0 : n; }

  const col = document.createElement("div");
  col.className = "pd-col";
  el.appendChild(col);

  const eyebrow = document.createElement("div");
  eyebrow.className = "pd-eyebrow";
  col.appendChild(eyebrow);

  const quoteWrap = document.createElement("div");
  quoteWrap.className = "pd-quote-wrap";
  const quote = document.createElement("blockquote");
  quote.className = "pd-quote";
  quoteWrap.appendChild(quote);
  col.appendChild(quoteWrap);

  const rail = document.createElement("div");
  rail.className = "pd-rail";
  col.appendChild(rail);

  const stageBody = document.createElement("div");
  stageBody.className = "pd-stage-body";
  col.appendChild(stageBody);

  let railNodes = {};
  let currentItemId = null;
  let lastStage = null;
  let lastRole = null;
  let lastAttentionWaiting = null;
  let waitPulse = null;
  let glowTween = null;
  let clipIndicator = null;

  // Rebuilds the v0->v2->v3->alt->prompt stage rail; a trailing "Verdict" node is only
  // added once this improvement (or a past attempt at it) actually has a §7 verdict, so
  // items nobody has practiced yet don't show a step that can never light up.
  function buildRail(hasVerdict) {
    rail.innerHTML = "";
    railNodes = {};
    const line = document.createElement("div");
    line.className = "pd-rail-line";
    rail.appendChild(line);
    const stages = hasVerdict ? RAIL_STAGES.concat([{ key: "verdict", label: "Verdict" }]) : RAIL_STAGES;
    stages.forEach((s) => {
      const node = document.createElement("div");
      node.className = "pd-rail-node";
      node.dataset.stage = s.key;
      node.innerHTML = '<span class="pd-rail-dot"></span><span class="pd-rail-label">' + s.label + "</span>";
      rail.appendChild(node);
      railNodes[s.key] = node;
    });
  }

  // Resolves "the improvement currently on screen": `state.feedback.itemId` while a §5
  // `feedback` step is playing, else `state.coach.improvementId` (e.g. right after `item`
  // arrives and before the first `feedback` for it lands).
  function findImprovement(state) {
    const id = (state.feedback && state.feedback.itemId) || (state.coach && state.coach.improvementId);
    const list = state.judgment && state.judgment.improvements;
    if (!id || !list) return null;
    return list.find((it) => it.id === id) || null;
  }

  // Renders the §7 verdict facts plus, if this improvement was attempted before, a faint
  // "previously on point N%" ghost line pulled from `state.coachHistory` so repeat practice
  // reads as progress rather than a fresh score with no context.
  function renderVerdict(state, imp) {
    const v = state.coach && state.coach.verdict;
    const wrap = document.createElement("div");
    wrap.className = "pd-verdict";
    if (v) {
      const facts = document.createElement("div");
      facts.className = "pd-verdict-facts";
      const f1 = document.createElement("div");
      f1.className = "pd-fact";
      f1.textContent = `fillers ${v.original_fillers} \u2192 ${v.fillers.count}`;
      const f2 = document.createElement("div");
      f2.className = "pd-fact";
      f2.textContent = v.pauses.landed ? "pause landed" : "no pause yet";
      const f3 = document.createElement("div");
      f3.className = "pd-fact";
      f3.textContent = v.wpm != null ? `${Math.round(v.wpm)} wpm \u00b7 ${v.pace_band}` : v.pace_band;
      facts.appendChild(f1);
      facts.appendChild(f2);
      facts.appendChild(f3);
      wrap.appendChild(facts);
      const onPoint = document.createElement("div");
      onPoint.className = "pd-onpoint";
      onPoint.textContent = `on point ${Math.round(v.on_point * 100)} %`;
      wrap.appendChild(onPoint);
      if (v.wins && v.wins.length) {
        const wins = document.createElement("div");
        wins.className = "pd-wins";
        wins.textContent = v.wins.join(" \u00b7 ");
        wrap.appendChild(wins);
      }
    }
    const history = imp && state.coachHistory && state.coachHistory[imp.id];
    if (history && history.length) {
      const prevEntry = history[history.length - (v ? 2 : 1)];
      if (prevEntry && prevEntry.verdict) {
        const ghost = document.createElement("div");
        ghost.className = "pd-verdict-ghost";
        ghost.textContent = `previously on point ${Math.round(prevEntry.verdict.on_point * 100)} %`;
        wrap.appendChild(ghost);
      }
    }
    stageBody.appendChild(wrap);
  }

  // Dispatches on `fb.stage` (§5 `feedback.stage`) to render this step's line: `role`
  // decides red "you said" (the user's own v0 clip) vs green "say it like this" (every
  // coach-voiced stage); `verdict` hands off to renderVerdict above.
  function renderStageText(state, imp) {
    stageBody.innerHTML = "";
    const fb = state.feedback || {};
    const stage = fb.stage;
    if (stage === "verdict") {
      renderVerdict(state, imp);
      return;
    }
    if (!imp) return;
    const role = fb.role || "coach";
    if (stage === "v0") {
      if (role === "user") {
        if (fb.available === false) {
          stageBody.appendChild(
            buildSaidLine("user", { missing: true, build: (b) => { b.textContent = "your clip wasn't clean enough to cut"; } })
          );
        } else {
          stageBody.appendChild(
            buildSaidLine("user", { eyebrow: "you said", build: (b) => { b.textContent = fb.text || imp.quote; } })
          );
        }
      } else {
        stageBody.appendChild(buildSaidLine("coach", { build: (b) => { b.textContent = fb.text || imp.issue; } }));
      }
    } else if (stage === "v2") {
      stageBody.appendChild(
        buildSaidLine("coach", { eyebrow: "say it like this", build: (b) => { b.textContent = fb.text || imp.v2_text; } })
      );
    } else if (stage === "v3") {
      stageBody.appendChild(
        buildSaidLine("coach", {
          eyebrow: "say it like this",
          build: (b) => renderMarkupBody(b, fb.markup || imp.v3_markup, imp.quote),
        })
      );
    } else if (stage === "alt") {
      stageBody.appendChild(
        buildSaidLine("coach", { eyebrow: "say it like this", build: (b) => { b.textContent = fb.text || imp.alternative; } })
      );
    } else if (stage === "prompt") {
      const p = document.createElement("div");
      p.className = "pd-stage-line pd-stage-line--prompt";
      p.textContent = "Say it your way";
      const pulse = document.createElement("span");
      pulse.className = "pd-listen-pulse";
      pulse.innerHTML = "<span></span><span></span><span></span>";
      p.appendChild(pulse);
      stageBody.appendChild(p);
      const target = fb.markup || imp.v3_markup || imp.v2_text;
      stageBody.appendChild(
        buildSaidLine("coach", { eyebrow: "say it like this", build: (b) => renderMarkupBody(b, target, imp.quote) })
      );
    }
  }

  // Marks the active/done rail nodes and pulses the active dot's glow -- but only while
  // `state.attention !== "waiting"`; during the user's own turn the glow holds static so
  // it doesn't visually compete with the "your turn" cue.
  function updateRailStates(state, imp) {
    const stage = state.feedback && state.feedback.stage;
    const activeKey = ["v0", "v2", "v3", "alt", "prompt", "verdict"].includes(stage) ? stage : null;
    const played = (imp && state.feedbackPlayed && state.feedbackPlayed[imp.id]) || new Set();
    Object.entries(railNodes).forEach(([key, node]) => {
      const isActive = key === activeKey;
      const isDone = !isActive && played.has(key);
      node.classList.toggle("is-active", isActive);
      node.classList.toggle("is-done", isDone);
      gsap.to(node, { scale: isActive ? 1.08 : 1, opacity: isActive ? 1 : isDone ? 0.6 : 0.35, duration: dur(0.3), ease: "power2.out" });
    });
    if (glowTween) {
      glowTween.kill();
      glowTween = null;
    }
    gsap.set(Object.values(railNodes), { "--glow": 0 });
    Object.values(railNodes).forEach((node) => node.classList.remove("pd-glow-static"));
    const activeNode = activeKey && railNodes[activeKey];
    if (activeNode) {
      if (state.attention !== "waiting" && !isReduced()) {
        activeNode.classList.remove("pd-glow-static");
        glowTween = gsap.to(activeNode, { "--glow": 1, duration: 0.9, ease: "sine.inOut", repeat: -1, yoyo: true });
      } else {
        activeNode.classList.add("pd-glow-static");
      }
    }
  }

  // Toggles the quote's amber "waiting on you" underline glow from `state.attention`,
  // guarded so it only restarts the tween on an actual waiting<->not-waiting transition.
  function updateWaiting(state) {
    const waiting = state.attention === "waiting";
    if (waiting === lastAttentionWaiting) return;
    lastAttentionWaiting = waiting;
    quoteWrap.classList.toggle("is-waiting", waiting);
    if (waitPulse) {
      waitPulse.kill();
      waitPulse = null;
    }
    if (waiting && !isReduced()) {
      waitPulse = gsap.to(quoteWrap, { "--wait-glow": 1, duration: 1.1, ease: "sine.inOut", repeat: -1, yoyo: true });
    } else {
      gsap.set(quoteWrap, { "--wait-glow": 0 });
    }
  }

  // Shows the small "audio is playing" bars only while the user's own v0 clip is the one
  // being heard, or at `verdict` (the practice attempt just recorded) -- never while a
  // coach-voiced stage plays, so the indicator can't be mistaken for the presenter's voice.
  function renderClipIndicator(state, imp) {
    const fb = state.feedback || {};
    const clip = imp && state.clips && state.clips[imp.id];
    const show = !!(clip && ((fb.role === "user" && clip.v0) || fb.stage === "verdict"));
    if (show) {
      if (!clipIndicator) {
        clipIndicator = document.createElement("div");
        clipIndicator.className = "pd-clip-indicator";
        clipIndicator.innerHTML = "<span></span><span></span><span></span>";
        col.appendChild(clipIndicator);
        if (!isReduced()) {
          gsap.to(clipIndicator.querySelectorAll("span"), { scaleY: 1.7, duration: 0.35, ease: "sine.inOut", stagger: { each: 0.12, repeat: -1, yoyo: true } });
        }
      }
    } else if (clipIndicator) {
      gsap.killTweensOf(clipIndicator.querySelectorAll("span"));
      clipIndicator.remove();
      clipIndicator = null;
    }
  }

  // `animateQuote` crossfades the whole block on an item change; `animateStage` crossfades
  // only `stageBody` when the item is the same but the stage advanced (v0 -> v2 -> ...),
  // so moving through one improvement's steps doesn't re-slide the quote and rail each time.
  function renderAll(state, { animateQuote, animateStage }) {
    const imp = findImprovement(state);
    const coach = state.coach || {};
    const eyebrowParts = [];
    if (coach.index && coach.total) eyebrowParts.push(`Improvement ${coach.index} of ${coach.total}`);
    if (imp && imp.skill) eyebrowParts.push(titleCase(imp.skill));
    if (imp && imp.slide != null) eyebrowParts.push(`Slide ${imp.slide}`);
    eyebrow.textContent = eyebrowParts.join(" \u00b7 ");

    const hasVerdict = !!(state.coach && state.coach.verdict) || !!(imp && state.coachHistory && state.coachHistory[imp.id] && state.coachHistory[imp.id].length);
    if (!railNodes.v0 || (hasVerdict && !railNodes.verdict)) buildRail(hasVerdict);

    const applyContent = () => {
      quote.textContent = imp ? imp.quote : "";
      renderStageText(state, imp);
      updateRailStates(state, imp);
      renderClipIndicator(state, imp);
    };

    if (animateQuote && !isReduced()) {
      gsap.to(quote, {
        autoAlpha: 0, y: -16, duration: 0.22, ease: "power2.in",
        onComplete: () => {
          applyContent();
          gsap.fromTo(quote, { autoAlpha: 0, y: 16 }, { autoAlpha: 1, y: 0, duration: 0.32, ease: "power2.out" });
        },
      });
    } else if (animateStage && !isReduced()) {
      gsap.to(stageBody, {
        autoAlpha: 0, y: -10, duration: 0.16, ease: "power2.in",
        onComplete: () => {
          applyContent();
          gsap.fromTo(stageBody, { autoAlpha: 0, y: 10 }, { autoAlpha: 1, y: 0, duration: 0.28, ease: "power2.out" });
        },
      });
    } else {
      applyContent();
      if (animateQuote) gsap.set(quote, { autoAlpha: 1, y: 0 });
    }
    updateWaiting(state);
  }

  return {
    mount(state) {
      const imp = findImprovement(state);
      currentItemId = imp && imp.id;
      lastStage = state.feedback && state.feedback.stage;
      lastRole = (state.feedback && state.feedback.role) || null;
      renderAll(state, { animateQuote: false });
      gsap.fromTo(col.children, { autoAlpha: 0, y: 16 }, { autoAlpha: 1, y: 0, duration: dur(0.5), stagger: dur(0.08), ease: "power2.out" });
    },
    update(state) {
      const imp = findImprovement(state);
      const itemId = imp && imp.id;
      const fb = state.feedback || {};
      const stage = fb.stage;
      const role = fb.role || null;
      const itemChanged = itemId !== currentItemId;
      const stageChanged = stage !== lastStage;
      const roleChanged = role !== lastRole;
      if (!itemChanged && !stageChanged && !roleChanged) {
        updateRailStates(state, imp);
        updateWaiting(state);
        return;
      }
      currentItemId = itemId;
      lastStage = stage;
      lastRole = role;
      renderAll(state, { animateQuote: itemChanged, animateStage: !itemChanged });
    },
    destroy() {
      if (glowTween) glowTween.kill();
      if (waitPulse) waitPulse.kill();
      gsap.killTweensOf(col.querySelectorAll("*"));
    },
  };
}

// ---------------------------------------------------------------------------
// drill view -- driven by the §5 `phase` message (name: "drill") and stepped by §5
// `drill` messages; drill words are §2 metrics.pronunciation.low_confidence picks.
// ---------------------------------------------------------------------------
const DRILL_STAGE_LABELS = { you: "how it came through", model: "the coach's version", prompt: "say it", verdict: "before / after" };

function createDrillView(el, { gsap, isReduced }) {
  function dur(n) { return isReduced() ? 0 : n; }

  const col = document.createElement("div");
  col.className = "pd-col pd-drill-col";
  el.appendChild(col);

  const wordEl = document.createElement("div");
  wordEl.className = "pd-drill-word";
  col.appendChild(wordEl);

  const stageEl = document.createElement("div");
  stageEl.className = "pd-drill-stage";
  col.appendChild(stageEl);

  const verdictEl = document.createElement("div");
  verdictEl.className = "pd-drill-verdict";
  col.appendChild(verdictEl);

  let currentWord = null;
  let currentStage = null;
  let pulseTween = null;

  // Renders the before/after confidence pair from the drill's §5 `conf_before`/`conf_after`;
  // the after number is coloured good/warn depending on whether the repeat actually improved.
  function renderVerdict(state) {
    verdictEl.innerHTML = "";
    const d = state.drill;
    if (!d || d.stage !== "verdict") {
      verdictEl.classList.remove("is-visible");
      return;
    }
    verdictEl.classList.add("is-visible");
    const before = Math.round((d.confBefore || 0) * 100);
    const after = Math.round((d.confAfter || 0) * 100);
    const beforeSpan = document.createElement("span");
    beforeSpan.className = "pd-drill-before";
    beforeSpan.textContent = `${before} %`;
    const arrow = document.createElement("span");
    arrow.className = "pd-drill-arrow";
    arrow.textContent = "\u2192";
    const afterSpan = document.createElement("span");
    afterSpan.className = "pd-drill-after";
    afterSpan.style.color = after >= before ? "var(--good)" : "var(--warn)";
    verdictEl.appendChild(beforeSpan);
    verdictEl.appendChild(arrow);
    verdictEl.appendChild(afterSpan);
    countTo(gsap, afterSpan, before, after, { duration: dur(1.0), ease: "power2.out", suffix: " %" });
  }

  // `waiting` is only true at stage "prompt" (the user's turn to say the word); that's the
  // one moment `el` gets the amber wait-glow pulse other views drive from `state.attention`.
  function renderStage(state) {
    const stage = state.drill && state.drill.stage;
    stageEl.textContent = DRILL_STAGE_LABELS[stage] || "";
    stageEl.classList.toggle("pd-drill-stage--you", stage === "you");
    stageEl.classList.toggle("pd-drill-stage--model", stage === "model");
    const waiting = stage === "prompt";
    el.classList.toggle("is-waiting", !!waiting);
    if (pulseTween) {
      pulseTween.kill();
      pulseTween = null;
    }
    if (waiting && !isReduced()) {
      pulseTween = gsap.to(el, { "--wait-glow": 1, duration: 1.1, ease: "sine.inOut", repeat: -1, yoyo: true });
    } else {
      gsap.set(el, { "--wait-glow": 0 });
    }
  }

  // `animateWord` crossfades the big word only when the drill target word itself changes;
  // a stage advance on the same word is handled by update()'s own fade below instead.
  function renderAll(state, { animateWord }) {
    const word = state.drill && state.drill.word;
    const applyContent = () => {
      wordEl.textContent = word || "";
      renderStage(state);
      renderVerdict(state);
    };
    if (animateWord && !isReduced()) {
      gsap.to(wordEl, {
        autoAlpha: 0, y: -20, duration: 0.2, ease: "power2.in",
        onComplete: () => {
          applyContent();
          gsap.fromTo(wordEl, { autoAlpha: 0, y: 20 }, { autoAlpha: 1, y: 0, duration: 0.35, ease: "power2.out" });
        },
      });
    } else {
      applyContent();
      if (animateWord) gsap.set(wordEl, { autoAlpha: 1, y: 0 });
    }
  }

  return {
    mount(state) {
      currentWord = state.drill && state.drill.word;
      currentStage = state.drill && state.drill.stage;
      renderAll(state, { animateWord: false });
      gsap.fromTo(col.children, { autoAlpha: 0, y: 20 }, { autoAlpha: 1, y: 0, duration: dur(0.6), stagger: dur(0.1), ease: "power2.out" });
    },
    update(state) {
      const word = state.drill && state.drill.word;
      const stage = state.drill && state.drill.stage;
      if (word === currentWord && stage === currentStage) return;
      const wordChanged = word !== currentWord;
      currentWord = word;
      currentStage = stage;
      if (wordChanged) {
        renderAll(state, { animateWord: true });
      } else {
        gsap.to(stageEl, {
          autoAlpha: 0, duration: 0.15,
          onComplete: () => {
            renderStage(state);
            renderVerdict(state);
            gsap.to(stageEl, { autoAlpha: 1, duration: 0.2 });
          },
        });
      }
    },
    destroy() {
      if (pulseTween) pulseTween.kill();
      gsap.killTweensOf(col.querySelectorAll("*"));
    },
  };
}

// ---------------------------------------------------------------------------
// wrap view -- driven by the §5 `phase` message (name: "report") or `coach.stage: "wrap"`;
// renders §3 judgment.scores (compact bars + history sparkline), §8 progress
// (SessionGraph.to_message), and the coach's wrap `options`.
// ---------------------------------------------------------------------------
function createWrapView(el, { gsap, isReduced }) {
  function dur(n) { return isReduced() ? 0 : n; }

  const col = document.createElement("div");
  col.className = "pd-col";
  el.appendChild(col);

  const barsEl = document.createElement("div");
  col.appendChild(barsEl);

  const progressEl = document.createElement("div");
  progressEl.className = "pd-wrap-progress";
  col.appendChild(progressEl);

  const skillsEl = document.createElement("div");
  skillsEl.className = "pd-wrap-skills";
  col.appendChild(skillsEl);

  const optionsEl = document.createElement("div");
  optionsEl.className = "pd-wrap-options";
  col.appendChild(optionsEl);

  let lastScoresSig = null;
  let lastSkillsSig = null;
  let lastProgressSig = null;
  let lastOptionsSig = null;

  // Each builder below is idempotent and cheap to skip: mount() always calls all four,
  // update() re-runs only the one whose signature (JSON.stringify of its slice of state)
  // actually changed, so an unrelated field update doesn't retrigger every tween.
  function buildBars(state) {
    const judgment = state.judgment;
    if (!judgment || !judgment.scores) {
      barsEl.innerHTML = "";
      return;
    }
    const keys = Object.keys(judgment.scores);
    gsap.killTweensOf(barsEl.querySelectorAll("*"));
    const rows = buildScoreBars(barsEl, keys, { compact: true });
    const prevScores = state.judgmentPrevious && state.judgmentPrevious.scores;
    const history = state.judgmentHistory || [];
    keys.forEach((key, i) => {
      const score = judgment.scores[key];
      const pct = (score / 5) * 100;
      const row = rows[key];
      const prevScore = prevScores ? prevScores[key] : undefined;
      if (prevScore != null) {
        row.ghost.style.left = `${(prevScore / 5) * 100}%`;
        row.ghost.style.opacity = "1";
        const delta = score - prevScore;
        row.delta.textContent = delta === 0 ? "" : delta > 0 ? `+${delta}` : `${delta}`;
        row.delta.style.color = delta >= 0 ? "var(--good)" : "var(--warn)";
      } else {
        row.ghost.style.opacity = "0";
        row.delta.textContent = "";
      }
      gsap.set(row.fill, { width: "0%" });
      gsap.to(row.fill, { width: `${pct}%`, duration: dur(0.9), ease: "expo.out", delay: dur(i * 0.08) });
      countTo(gsap, row.num, 0, score, { duration: dur(0.9), delay: dur(i * 0.08) });
      if (history.length > 1 && row.spark) {
        row.spark.appendChild(buildSparkline(history, key));
      }
    });
  }

  function buildSkills(state) {
    skillsEl.innerHTML = "";
    const skills = (state.progress && state.progress.skills) || {};
    SKILL_IDS.forEach((id) => {
      const mastery = (skills[id] && skills[id].mastery) || 0;
      const tick = document.createElement("div");
      tick.className = "pd-skill-tick";
      tick.title = `${titleCase(id)}: ${Math.round(mastery * 100)} %`;
      const bar = document.createElement("div");
      bar.className = "pd-skill-tick-fill";
      tick.appendChild(bar);
      skillsEl.appendChild(tick);
      gsap.set(bar, { scaleY: 0 });
      gsap.to(bar, { scaleY: Math.max(mastery, 0.04), duration: dur(0.7), ease: "power2.out" });
    });
  }

  function renderProgress(state) {
    const p = state.progress;
    if (!p) {
      progressEl.textContent = "";
      return;
    }
    const next = p.nextFocus ? String(p.nextFocus).replace(/-/g, " ") : null;
    progressEl.textContent = next ? `${p.level} \u2192 next: ${next}` : p.level || "";
  }

  function renderOptions(state) {
    const opts = state.coach && state.coach.options;
    if (opts && opts.length) {
      optionsEl.textContent = "Try the talk again, practice more, or start a new one";
      optionsEl.classList.add("is-visible");
    } else {
      optionsEl.textContent = "";
      optionsEl.classList.remove("is-visible");
    }
  }

  return {
    mount(state) {
      buildBars(state);
      buildSkills(state);
      renderProgress(state);
      renderOptions(state);
      lastScoresSig = JSON.stringify(state.judgment && state.judgment.scores) + "|" + (state.judgmentHistory ? state.judgmentHistory.length : 0);
      lastSkillsSig = JSON.stringify(state.progress && state.progress.skills);
      lastProgressSig = state.progress ? state.progress.level + "|" + state.progress.nextFocus : null;
      lastOptionsSig = state.coach && state.coach.options ? state.coach.options.length : 0;
      gsap.fromTo(col.children, { autoAlpha: 0, y: 18 }, { autoAlpha: 1, y: 0, duration: dur(0.6), stagger: dur(0.1), ease: "power2.out" });
    },
    update(state) {
      const scoresSig = JSON.stringify(state.judgment && state.judgment.scores) + "|" + (state.judgmentHistory ? state.judgmentHistory.length : 0);
      if (scoresSig !== lastScoresSig) {
        lastScoresSig = scoresSig;
        buildBars(state);
      }
      const skillsSig = JSON.stringify(state.progress && state.progress.skills);
      if (skillsSig !== lastSkillsSig) {
        lastSkillsSig = skillsSig;
        buildSkills(state);
      }
      const progressSig = state.progress ? state.progress.level + "|" + state.progress.nextFocus : null;
      if (progressSig !== lastProgressSig) {
        lastProgressSig = progressSig;
        renderProgress(state);
      }
      const optionsSig = state.coach && state.coach.options ? state.coach.options.length : 0;
      if (optionsSig !== lastOptionsSig) {
        lastOptionsSig = optionsSig;
        renderOptions(state);
      }
    },
    destroy() {
      gsap.killTweensOf(col.querySelectorAll("*"));
    },
  };
}

const FACTORIES = {
  scores: createScoresView,
  improvement: createImprovementView,
  drill: createDrillView,
  wrap: createWrapView,
};

export function createDashboard(root, { send, gsap }) {
  root.classList.add("dash-root");

  let reduced = false;
  const mm = gsap.matchMedia();
  mm.add("(prefers-reduced-motion: reduce)", () => {
    reduced = true;
    return () => {
      reduced = false;
    };
  });
  const isReduced = () => reduced;

  let currentView = null;
  let controller = null;
  let viewEl = null;

  // Crossfades the outgoing view out (skipped under reduced motion) before destroying its
  // controller/removing its element, so a view switch never yanks a still-animating node
  // out of the DOM mid-tween.
  function switchTo(view, state) {
    const oldController = controller;
    const oldEl = viewEl;
    controller = null;
    viewEl = null;
    currentView = view;

    const finishOld = () => {
      if (oldController) oldController.destroy();
      if (oldEl && oldEl.parentNode) oldEl.parentNode.removeChild(oldEl);
    };
    if (oldEl) {
      if (isReduced()) {
        finishOld();
      } else {
        gsap.to(oldEl, { autoAlpha: 0, y: -12, duration: 0.22, ease: "power2.in", onComplete: finishOld });
      }
    }

    const factory = view && FACTORIES[view];
    if (!factory) return;
    const el = document.createElement("div");
    el.className = `pd-view pd-view--${view}`;
    root.appendChild(el);
    viewEl = el;
    controller = factory(el, { send, gsap, isReduced });
    controller.mount(state);
  }

  function update(state) {
    const view = state && state.view;
    const knownView = view && FACTORIES[view] ? view : null;
    if (knownView !== currentView) {
      switchTo(knownView, state);
      return;
    }
    if (controller) controller.update(state);
  }

  function destroy() {
    if (controller) controller.destroy();
    if (viewEl && viewEl.parentNode) viewEl.parentNode.removeChild(viewEl);
    controller = null;
    viewEl = null;
    currentView = null;
    mm.revert();
    root.classList.remove("dash-root");
  }

  return { update, destroy };
}

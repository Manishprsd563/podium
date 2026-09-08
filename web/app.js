// Podium web client. No build step: plain ES module, deps from CDN.
import { Room, RoomEvent } from "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.esm.mjs";
const PDFJS_URL = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.min.mjs";
const PDFJS_WORKER_URL = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.min.mjs";
// A fresh room per page load: an agent job is dispatched per room, and a job's
// AgentSession closes when its participant leaves, so reusing one room name
// would rejoin a dead session ("AgentSession isn't running"). `?room=` overrides.
const ROOM_NAME = new URLSearchParams(location.search).get("room")
  || "podium-" + Math.random().toString(36).slice(2, 10);
const state = {
  room: null,
  connected: false,
  micEnabled: false,
  phase: "setup",
  budgetS: 60,
  deck: null,
  pendingDeckSlides: null,
  currentSlide: 1,
  remainingS: null,
  transcript: new Map(), // segId -> {speaker, text, final}
  metrics: null,
  judgment: null,
  clips: {}, // improvement_id -> {v1,v2,v3: url}
  provider: null,
  activeImprovementId: null,
  agentSpeaking: false,
  localSpeaking: false,
};
const el = {};
function q(id) { return document.getElementById(id); }
function cacheEls() {
  [
    "conn-badge", "mic-badge", "agent-badge", "provider-badge", "error-banner",
    "view-setup", "view-stage", "view-report",
    "setup-form", "setup-topic", "setup-level", "setup-duration",
    "upload-input", "upload-status", "upload-error",
    "setup-deck-preview", "deck-preview-list",
    "stage-status", "stage-prep", "prep-countdown", "prep-ready-btn",
    "stage-present", "present-timer", "slide-title", "slide-bullets",
    "slide-next-btn", "present-done-btn", "transcript-strip",
    "scorecard", "judgment-summary", "metrics-grid", "metrics-perslide",
    "rerecord-slide", "rerecord-btn", "improvement-cards",
  ].forEach((id) => { el[toCamel(id)] = q(id); });
}
function toCamel(id) {
  return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

// ---------------------------------------------------------------- connect
async function connect() {
  const identity = "presenter-" + Math.random().toString(36).slice(2, 8);
  setConnBadge("connecting\u2026", "muted");
  let room;
  try {
    const res = await fetch(`/token?room=${encodeURIComponent(ROOM_NAME)}&identity=${encodeURIComponent(identity)}`);
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || `token request failed (${res.status})`);
    room = new Room({ adaptiveStream: true, dynacast: true });
    wireRoom(room, identity);
    await room.connect(payload.url, payload.token);
  } catch (err) {
    setConnBadge("offline", "muted");
    showError(`Could not connect: ${err.message || err}`);
    return;
  }
  state.room = room;
  state.connected = true;
  setConnBadge("connected", "on");
  render();
  try {
    // Podium measures the presenter's loudness variance as a delivery metric, so
    // browser auto-gain and noise suppression must stay off: they flatten exactly
    // the dynamics we score, and they attenuate steady speech by ~25 dB. Echo
    // cancellation stays on because the coach speaks the timer cue mid-talk.
    await room.localParticipant.setMicrophoneEnabled(true, {
      autoGainControl: false,
      noiseSuppression: false,
      echoCancellation: true,
    });
    state.micEnabled = true;
  } catch (err) {
    showError(`Connected, but microphone access failed: ${err.message || err}. Grant mic permission and reload to present.`);
  }
  render();
}
function wireRoom(room, identity) {
  room.registerTextStreamHandler("lk.transcription", async (reader, participantInfo) => {
    try {
      const text = await reader.readAll();
      const info = reader.info;
      const segId = (info.attributes && info.attributes["lk.segment_id"]) || info.id;
      const isFinal = !!(info.attributes && info.attributes["lk.transcription_final"] === "true");
      upsertTranscriptLine(segId, (participantInfo && participantInfo.identity) || "agent", text, isFinal);
      render();
    } catch (err) {
      console.error("transcription stream error", err);
    }
  });
  room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
    if (topic !== "podium") return;
    let msg;
    try {
      msg = JSON.parse(new TextDecoder().decode(payload));
    } catch (err) {
      console.error("bad podium payload", err);
      return;
    }
    handleMessage(msg);
  });
  room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
    state.agentSpeaking = speakers.some((p) => p.identity !== identity);
    state.localSpeaking = speakers.some((p) => p.identity === identity);
    render();
  });
  room.on(RoomEvent.Disconnected, () => {
    state.connected = false;
    setConnBadge("disconnected", "muted");
  });
}
function sendMessage(msg) {
  if (!state.room) {
    showError("Not connected yet \u2014 message not sent.");
    return;
  }
  const data = new TextEncoder().encode(JSON.stringify(msg));
  state.room.localParticipant.publishData(data, { reliable: true, topic: "podium" });
}
function upsertTranscriptLine(segId, speaker, text, isFinal) {
  state.transcript.set(segId, { speaker, text, final: isFinal });
  if (state.transcript.size > 40) {
    const firstKey = state.transcript.keys().next().value;
    state.transcript.delete(firstKey);
  }
}

// ---------------------------------------------------------- message router
function handleMessage(msg) {
  switch (msg.type) {
    case "phase":
      state.phase = msg.name;
      if (msg.budget_s != null) state.budgetS = msg.budget_s;
      break;
    case "deck":
      state.deck = msg.deck;
      state.currentSlide = (msg.deck && msg.deck.slides && msg.deck.slides[0] && msg.deck.slides[0].index) || 1;
      break;
    case "slide":
      state.currentSlide = msg.slide;
      break;
    case "timer":
      state.remainingS = msg.remaining_s;
      break;
    case "metrics":
      state.metrics = msg.metrics;
      break;
    case "judgment":
      state.judgment = msg.judgment;
      break;
    case "feedback":
      state.activeImprovementId = (msg.item && msg.item.id) || null;
      state.agentSpeaking = true;
      break;
    case "clip":
      if (!state.clips[msg.improvement_id]) state.clips[msg.improvement_id] = {};
      state.clips[msg.improvement_id][msg.variant] = msg.url;
      state.agentSpeaking = true;
      break;
    case "provider":
      state.provider = { name: msg.name, model: msg.model, speaker: msg.speaker };
      break;
    case "error":
      showError(msg.message || "Unknown agent error");
      return;
    default:
      console.warn("unhandled podium message", msg);
      return;
  }
  render();
}

// ------------------------------------------------------------------ render
function viewForPhase(phase) {
  if (phase === "prep" || phase === "present") return "stage";
  if (phase === "analyze" || phase === "coach" || phase === "drill" || phase === "report") return "report";
  return "setup";
}
function showView(name) {
  el.viewSetup.classList.toggle("hidden", name !== "setup");
  el.viewStage.classList.toggle("hidden", name !== "stage");
  el.viewReport.classList.toggle("hidden", name !== "report");
}
function render() {
  showView(viewForPhase(state.phase));
  renderTopbar();
  if (state.phase === "setup") renderSetup();
  else if (state.phase === "prep" || state.phase === "present") renderStage();
  else renderReport();
}
function renderTopbar() {
  el.micBadge.classList.toggle("hidden", !state.micEnabled);
  el.micBadge.classList.toggle("on", state.localSpeaking);
  el.agentBadge.classList.toggle("hidden", !state.connected);
  el.agentBadge.textContent = state.agentSpeaking ? "\ud83d\udd0a coach speaking" : "\ud83c\udfa7 listening";
  el.agentBadge.classList.toggle("speaking", state.agentSpeaking);
  el.agentBadge.classList.toggle("on", !state.agentSpeaking);
  if (state.provider) {
    el.providerBadge.classList.remove("hidden");
    el.providerBadge.textContent = `${state.provider.name} \u00b7 ${state.provider.model} \u00b7 ${state.provider.speaker}`;
  }
}
function setConnBadge(text, cls) {
  el.connBadge.className = "badge " + cls;
  el.connBadge.textContent = text;
}
function showError(text) {
  el.errorBanner.textContent = text;
  el.errorBanner.classList.remove("hidden");
}

// -------------------------------------------------------------- Setup view
function renderSetup() {
  const slides = state.pendingDeckSlides || (state.deck && state.deck.slides);
  if (slides && slides.length) {
    el.setupDeckPreview.classList.remove("hidden");
    renderDeckPreview(slides);
  } else {
    el.setupDeckPreview.classList.add("hidden");
  }
}
function renderDeckPreview(slides) {
  el.deckPreviewList.innerHTML = "";
  slides.forEach((s) => {
    const row = document.createElement("div");
    row.className = "deck-slide";
    const title = document.createElement("strong");
    title.textContent = `${s.index}. ${s.title}`;
    row.appendChild(title);
    if (s.bullets && s.bullets.length) {
      const ul = document.createElement("ul");
      s.bullets.forEach((b) => {
        const li = document.createElement("li");
        li.textContent = b;
        ul.appendChild(li);
      });
      row.appendChild(ul);
    }
    el.deckPreviewList.appendChild(row);
  });
}
async function handlePdfUpload(file) {
  el.uploadError.classList.add("hidden");
  el.uploadStatus.textContent = "Parsing\u2026";
  try {
    const pdfjsLib = await import(PDFJS_URL);
    pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
    const buf = await file.arrayBuffer();
    const doc = await pdfjsLib.getDocument({ data: buf }).promise;
    const slides = [];
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent();
      const lines = groupTextIntoLines(content.items);
      const [title, ...rest] = lines;
      slides.push({ index: i, title: title || `Slide ${i}`, bullets: rest.slice(0, 8) });
    }
    if (!slides.length) throw new Error("No pages found in PDF");
    state.pendingDeckSlides = slides;
    render();
    sendMessage({ type: "deck_upload", slides });
    el.uploadStatus.textContent = `Sent ${slides.length} slide(s) parsed from PDF.`;
  } catch (err) {
    el.uploadStatus.textContent = "";
    el.uploadError.textContent = `PDF upload failed: ${err.message || err} \u2014 "Generate slides" above still works.`;
    el.uploadError.classList.remove("hidden");
  }
}
function groupTextIntoLines(items) {
  const rows = [];
  const tolerance = 2;
  for (const item of items) {
    const y = item.transform[5];
    let row = rows.find((r) => Math.abs(r.y - y) <= tolerance);
    if (!row) { row = { y, parts: [] }; rows.push(row); }
    row.parts.push(item.str);
  }
  rows.sort((a, b) => b.y - a.y);
  return rows.map((r) => r.parts.join(" ").replace(/\s+/g, " ").trim()).filter(Boolean);
}

// -------------------------------------------------------------- Stage view
function currentSlideObj() {
  const slides = (state.deck && state.deck.slides) || [];
  return slides.find((s) => s.index === state.currentSlide) || slides[state.currentSlide - 1] || null;
}
function formatClock(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}
function renderStage() {
  const isPrep = state.phase === "prep";
  el.stagePrep.classList.toggle("hidden", !isPrep);
  el.stagePresent.classList.toggle("hidden", isPrep);
  el.stageStatus.textContent = state.agentSpeaking ? "coach speaking" : "listening";
  el.stageStatus.classList.toggle("speaking", state.agentSpeaking);
  el.stageStatus.classList.toggle("listening", !state.agentSpeaking);
  if (isPrep) {
    el.prepCountdown.textContent = state.remainingS != null ? String(Math.max(0, Math.ceil(state.remainingS))) : "\u2014";
    return;
  }
  el.presentTimer.textContent = state.remainingS != null ? formatClock(state.remainingS) : "\u2014";
  const slide = currentSlideObj();
  el.slideTitle.textContent = slide ? slide.title : "\u2014";
  el.slideBullets.innerHTML = "";
  ((slide && slide.bullets) || []).forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    el.slideBullets.appendChild(li);
  });
  renderTranscript();
}
function renderTranscript() {
  el.transcriptStrip.innerHTML = "";
  const lines = Array.from(state.transcript.values()).slice(-8);
  lines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "line" + (line.final ? "" : " partial");
    div.textContent = `${line.speaker}: ${line.text}`;
    el.transcriptStrip.appendChild(div);
  });
  el.transcriptStrip.scrollTop = el.transcriptStrip.scrollHeight;
}

// ------------------------------------------------------------- Report view
const SCORE_MAX = 5; // rubric scale per skills/judge/*.md examples in GATE0.md
function renderReport() {
  renderScorecard();
  renderMetrics();
  renderRerecordSlideOptions();
  renderImprovementCards();
}
function renderScorecard() {
  el.scorecard.innerHTML = "";
  const scores = (state.judgment && state.judgment.scores) || null;
  el.judgmentSummary.textContent = (state.judgment && state.judgment.summary) || "";
  if (!scores) {
    el.scorecard.innerHTML = '<p class="hint">Waiting for the judge\u2026</p>';
    return;
  }
  Object.entries(scores).forEach(([label, value]) => {
    const row = document.createElement("div");
    row.className = "score-row";
    const l = document.createElement("div");
    l.className = "score-label";
    l.textContent = label.replace(/_/g, " ");
    const track = document.createElement("div");
    track.className = "score-bar-track";
    const fill = document.createElement("div");
    fill.className = "score-bar-fill";
    fill.style.width = `${Math.max(0, Math.min(100, (value / SCORE_MAX) * 100))}%`;
    track.appendChild(fill);
    const v = document.createElement("div");
    v.className = "score-value";
    v.textContent = `${value}/${SCORE_MAX}`;
    row.append(l, track, v);
    el.scorecard.appendChild(row);
  });
}
function metricTile(label, value) {
  const tile = document.createElement("div");
  tile.className = "metric-tile";
  const v = document.createElement("div");
  v.className = "metric-value";
  v.textContent = value;
  const l = document.createElement("div");
  l.className = "metric-label";
  l.textContent = label;
  tile.append(v, l);
  return tile;
}
function renderMetrics() {
  el.metricsGrid.innerHTML = "";
  el.metricsPerslide.innerHTML = "";
  const m = state.metrics;
  if (!m) {
    el.metricsGrid.innerHTML = '<p class="hint">Waiting for metrics\u2026</p>';
    return;
  }
  el.metricsGrid.append(
    metricTile("words / min", m.wpm != null ? m.wpm.toFixed(0) : "\u2014"),
    metricTile("fillers", m.fillers ? `${m.fillers.count} (${m.fillers.per_min.toFixed(1)}/min)` : "\u2014"),
    metricTile("longest pause", m.pauses ? `${m.pauses.longest_s.toFixed(1)}s` : "\u2014"),
    metricTile(
      "time used / budget",
      m.time_budget ? `${m.time_budget.used_s.toFixed(0)}s / ${m.time_budget.budget_s}s` : "\u2014"
    )
  );
  if (m.time_budget && m.time_budget.over_s > 0) {
    el.metricsGrid.append(metricTile("over budget", `+${m.time_budget.over_s.toFixed(1)}s`));
  }
  if (m.time_budget && m.time_budget.per_slide && m.time_budget.per_slide.length) {
    const parts = m.time_budget.per_slide.map((row) => `slide ${row.slide}: ${row.used_s.toFixed(0)}s (fair ${row.fair_share_s.toFixed(0)}s)`);
    el.metricsPerslide.textContent = "Per slide: " + parts.join(" \u00b7 ");
  }
}
function renderRerecordSlideOptions() {
  const slides = (state.deck && state.deck.slides) || [];
  const existing = new Set(Array.from(el.rerecordSlide.options).map((o) => o.value));
  const wanted = new Set(slides.map((s) => String(s.index)));
  if (existing.size !== wanted.size || [...wanted].some((v) => !existing.has(v))) {
    el.rerecordSlide.innerHTML = "";
    slides.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = String(s.index);
      opt.textContent = `Slide ${s.index}: ${s.title}`;
      el.rerecordSlide.appendChild(opt);
    });
  }
  if (state.currentSlide) el.rerecordSlide.value = String(state.currentSlide);
}
const CLIP_LABELS = { v1: "You said", v2: "Cleaner", v3: "With pauses" };
function renderImprovementCards() {
  el.improvementCards.innerHTML = "";
  const improvements = (state.judgment && state.judgment.improvements) || [];
  if (!improvements.length) {
    el.improvementCards.innerHTML = '<p class="hint">Waiting for the judge\u2026</p>';
    return;
  }
  improvements.forEach((imp) => {
    const card = document.createElement("div");
    card.className = "improvement-card" + (state.activeImprovementId === imp.id ? " active" : "");
    const quote = document.createElement("p");
    quote.className = "improvement-quote";
    quote.textContent = `\u201c${imp.quote}\u201d`;
    const issue = document.createElement("p");
    issue.className = "improvement-issue";
    issue.textContent = imp.issue;
    card.append(quote, issue);
    const clips = state.clips[imp.id] || {};
    ["v1", "v2", "v3"].forEach((variant) => {
      const row = document.createElement("div");
      row.className = "clip-row" + (clips[variant] ? "" : " missing");
      const label = document.createElement("div");
      label.className = "clip-label";
      label.textContent = CLIP_LABELS[variant];
      row.appendChild(label);
      if (clips[variant]) {
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.src = clips[variant];
        row.appendChild(audio);
      }
      card.appendChild(row);
    });
    const cmds = document.createElement("div");
    cmds.className = "command-row";
    ["again", "slower", "why", "skip"].forEach((name) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "secondary";
      btn.textContent = name;
      btn.addEventListener("click", () => sendMessage({ type: "command", name }));
      cmds.appendChild(btn);
    });
    card.appendChild(cmds);
    el.improvementCards.appendChild(card);
  });
}

// -------------------------------------------------------------- wiring
function wireEvents() {
  el.setupForm.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage({
      type: "setup",
      topic: el.setupTopic.value.trim(),
      level: el.setupLevel.value,
      budget_s: Number(el.setupDuration.value),
    });
  });
  el.uploadInput.addEventListener("change", () => {
    const file = el.uploadInput.files[0];
    if (file) handlePdfUpload(file);
  });
  el.prepReadyBtn.addEventListener("click", () => sendMessage({ type: "ready" }));
  el.slideNextBtn.addEventListener("click", () => sendMessage({ type: "slide_next" }));
  el.presentDoneBtn.addEventListener("click", () => sendMessage({ type: "present_end" }));
  el.rerecordBtn.addEventListener("click", () => sendMessage({ type: "rerecord", slide: Number(el.rerecordSlide.value) }));
  window.addEventListener("keydown", (e) => {
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (state.phase !== "present") return;
    if (e.code === "Space") {
      e.preventDefault();
      sendMessage({ type: "slide_next" });
    } else if (e.code === "Enter") {
      e.preventDefault();
      sendMessage({ type: "present_end" });
    }
  });
}
function init() {
  cacheEls();
  wireEvents();
  render();
  connect();
  // Test/debug hook: lets an external driver call the router directly.
  window.podiumDebug = { state, handleMessage, render };
}
init();
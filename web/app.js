// Podium web client. No build step: plain ES module, deps from CDN.
import { Room, RoomEvent, Track } from "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.esm.mjs";
import { createOrb } from "./orb.js";
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
  lastAgentLine: null, // {text, final}
  lastUserLine: null, // {text, final}
  metrics: null,
  judgment: null,
  clips: {}, // improvement_id -> {v1,v2,v3,attempt<N>: url}
  provider: null,
  activeImprovementId: null,
  agentSpeaking: false,
  localSpeaking: false,
  coach: null, // {stage, options, improvementId, index, total, attempt, verdict}
  coachHistory: {}, // improvement_id -> [{attempt, verdict}]
  countdownValue: null,
  countdownTimer: null,
  gateOpen: true,
  agentPresent: false,
  audioCtx: null,
  analysers: {}, // agent|user -> {analyser, data}
  orbGate: null,
  orbBubble: null,
};
const el = {};
function q(id) { return document.getElementById(id); }
// The coach is named after the active Rime speaker (the agent does the same),
// so a speaker change never leaves a stale name on screen.
function coachName() {
  const s = state.provider && state.provider.speaker;
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Coach";
}
function cacheEls() {
  [
    "conn-badge", "mic-badge", "agent-badge", "provider-badge", "error-banner",
    "astra-indicator", "astra-line", "user-line",
    "gate", "gate-orb", "gate-status", "gate-start-btn", "unmute-pill",
    "view-setup", "view-stage", "view-coach", "view-report",
    "setup-form", "setup-topic", "setup-level", "setup-duration",
    "upload-input", "upload-status", "upload-error",
    "setup-deck-preview", "deck-preview-list",
    "stage-status", "stage-prep", "prep-countdown", "prep-ready-btn",
    "stage-present", "present-timer", "slide-title", "slide-bullets",
    "slide-next-btn", "present-done-btn", "transcript-strip",
    "scorecard", "judgment-summary", "metrics-grid", "metrics-perslide",
    "coach-chat-log", "coach-card", "coach-options", "coach-status",
    "rerecord-slide", "rerecord-btn", "improvement-cards", "new-talk-btn",
    "countdown-overlay", "countdown-number",
  ].forEach((id) => { el[toCamel(id)] = q(id); });
}
function toCamel(id) {
  return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
function stripPauseMarkup(text) {
  return (text || "").replace(/<\d+>/g, "");
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
    renderGate();
    return;
  }
  state.room = room;
  state.connected = true;
  setConnBadge("connected", "on");
  // A remote participant already in the room when we attach listeners won't
  // fire ParticipantConnected retroactively, so check directly.
  if (room.remoteParticipants && room.remoteParticipants.size > 0) markAgentPresent();
  render();
  renderGate();
  // Microphone enablement now waits for the gate's start button: it is the
  // same user gesture that unlocks audio playback (room.startAudio()), and
  // both must happen together or the coach can't be heard, or heard back.
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
  room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
    if (track.kind === Track.Kind.Audio && participant.identity !== identity) {
      // livekit-client does not auto-play subscribed audio: without attach()
      // the coach is silent in every browser, gesture or not. The element is
      // what room.startAudio() unblocks, and Chrome only feeds a remote track
      // into Web Audio while a media element is playing it.
      const audioEl = track.attach();
      audioEl.id = "coach-audio";
      audioEl.setAttribute("aria-hidden", "true");
      document.body.appendChild(audioEl);
      attachAnalyser(track.mediaStreamTrack, "agent");
    }
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => {
    if (track.kind === Track.Kind.Audio) track.detach().forEach((el) => el.remove());
  });
  room.on(RoomEvent.ParticipantConnected, () => markAgentPresent());
  room.on(RoomEvent.AudioPlaybackStatusChanged, () => updateUnmutePill());
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
  if (state.transcript.size > 60) {
    const firstKey = state.transcript.keys().next().value;
    state.transcript.delete(firstKey);
  }
  const isAstra = !speaker.startsWith("presenter-");
  if (isAstra) {
    state.lastAgentLine = { text, final: isFinal };
  } else {
    state.lastUserLine = { text, final: isFinal };
  }
}

// ------------------------------------------------------------- start gate
// Chrome blocks remote audio until the page has had a user gesture. The gate
// holds the greeting-less UI until the start button click both unlocks audio
// playback and enables the mic, then tells the agent it's safe to speak.
function markAgentPresent() {
  if (state.agentPresent) return;
  state.agentPresent = true;
  renderGate();
}
function renderGate() {
  if (!el.gate) return;
  const ready = state.connected && state.agentPresent;
  el.gateStatus.textContent = ready ? "Your coach is ready" : "Connecting your coach\u2026";
  if (state.gateOpen) el.gateStartBtn.disabled = !ready;
  if (state.orbGate) state.orbGate.setState(ready ? "listening" : "connecting");
}
function closeGate() {
  state.gateOpen = false;
  el.gate.classList.add("closing");
  setTimeout(() => el.gate.classList.add("hidden"), 420);
  updateUnmutePill();
}
function updateUnmutePill() {
  if (!el.unmutePill) return;
  if (!state.room || state.gateOpen) {
    el.unmutePill.classList.add("hidden");
    return;
  }
  el.unmutePill.classList.toggle("hidden", !!state.room.canPlaybackAudio);
}
async function startSession() {
  el.gateStartBtn.disabled = true;
  el.gateStatus.textContent = "Starting your coach\u2026";
  try {
    await state.room.startAudio();
  } catch (err) {
    el.gateStatus.textContent = "Tap Start session to enable audio.";
    el.gateStartBtn.disabled = false;
    return;
  }
  if (!state.room.canPlaybackAudio) {
    el.gateStatus.textContent = "Tap Start session to enable audio.";
    el.gateStartBtn.disabled = false;
    return;
  }
  ensureAudioContext();
  try {
    // Same constraints as before: load-bearing for the loudness metric.
    await state.room.localParticipant.setMicrophoneEnabled(true, {
      autoGainControl: false,
      noiseSuppression: false,
      echoCancellation: true,
    });
    state.micEnabled = true;
    attachLocalMicAnalyser();
  } catch (err) {
    showError(`Connected, but microphone access failed: ${err.message || err}. Grant mic permission and reload to present.`);
  }
  sendMessage({ type: "client_ready" });
  closeGate();
  render();
}

// ---------------------------------------------------------------- audio levels
function ensureAudioContext() {
  if (!state.audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audioCtx = new Ctx();
  }
  if (state.audioCtx.state === "suspended") state.audioCtx.resume();
  return state.audioCtx;
}
function attachAnalyser(mediaStreamTrack, key) {
  if (!mediaStreamTrack) return;
  const ctx = ensureAudioContext();
  const stream = new MediaStream([mediaStreamTrack]);
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  // Analysis only: never routed to ctx.destination, so no double audio.
  source.connect(analyser);
  state.analysers[key] = { analyser, data: new Uint8Array(analyser.frequencyBinCount) };
}
function attachLocalMicAnalyser() {
  const pub = state.room && state.room.localParticipant.getTrackPublication(Track.Source.Microphone);
  const track = pub && pub.track;
  if (track && track.mediaStreamTrack) attachAnalyser(track.mediaStreamTrack, "user");
}
function sampleLevel(entry) {
  if (!entry) return 0;
  const { analyser, data } = entry;
  analyser.getByteTimeDomainData(data);
  let sumSquares = 0;
  for (let i = 0; i < data.length; i++) {
    const v = (data[i] - 128) / 128;
    sumSquares += v * v;
  }
  return Math.min(1, Math.sqrt(sumSquares / data.length) * 4);
}
const SPEAK_THRESHOLD = 0.06;
const smoothedLevels = { agent: 0, user: 0 };
function levelLoop() {
  smoothedLevels.agent += (sampleLevel(state.analysers.agent) - smoothedLevels.agent) * 0.3;
  smoothedLevels.user += (sampleLevel(state.analysers.user) - smoothedLevels.user) * 0.3;
  let orbState;
  if (state.gateOpen) {
    orbState = state.connected && state.agentPresent ? "listening" : "connecting";
  } else if (!state.connected) {
    orbState = "connecting";
  } else {
    const agentActive = smoothedLevels.agent > SPEAK_THRESHOLD || state.agentSpeaking;
    const userActive = !agentActive && (smoothedLevels.user > SPEAK_THRESHOLD || state.localSpeaking);
    orbState = agentActive ? "speaking" : userActive ? "user" : "listening";
  }
  if (state.orbGate) {
    state.orbGate.setState(orbState);
    state.orbGate.setLevels(smoothedLevels);
  }
  if (state.orbBubble) {
    state.orbBubble.setState(orbState);
    state.orbBubble.setLevels(smoothedLevels);
  }
  requestAnimationFrame(levelLoop);
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
    case "coach":
      state.coach = {
        stage: msg.stage,
        options: msg.options || [],
        improvementId: msg.improvement_id != null ? msg.improvement_id : null,
        index: msg.index != null ? msg.index : null,
        total: msg.total != null ? msg.total : null,
        attempt: msg.attempt != null ? msg.attempt : null,
        verdict: msg.verdict || null,
      };
      state.activeImprovementId = state.coach.improvementId;
      if (msg.stage === "verdict" && msg.improvement_id && msg.verdict) {
        const hist = state.coachHistory[msg.improvement_id] || (state.coachHistory[msg.improvement_id] = []);
        if (!hist.some((h) => h.attempt === msg.attempt)) {
          hist.push({ attempt: msg.attempt, verdict: msg.verdict });
        }
      }
      break;
    case "countdown":
      startCountdown(msg.seconds);
      break;
    case "provider":
      state.provider = { name: msg.name, model: msg.model, speaker: msg.speaker };
      markAgentPresent();
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

// ------------------------------------------------------------ countdown overlay
function clearCountdownTimer() {
  if (state.countdownTimer) {
    clearInterval(state.countdownTimer);
    state.countdownTimer = null;
  }
}
function startCountdown(seconds) {
  clearCountdownTimer();
  state.countdownValue = seconds;
  el.countdownOverlay.classList.remove("hidden");
  el.countdownNumber.textContent = String(seconds);
  let n = seconds;
  state.countdownTimer = setInterval(() => {
    n -= 1;
    if (n > 0) {
      el.countdownNumber.textContent = String(n);
    } else if (n === 0) {
      el.countdownNumber.textContent = "Go!";
    } else {
      clearCountdownTimer();
      el.countdownOverlay.classList.add("hidden");
    }
  }, 900);
}

// ------------------------------------------------------------------ render
function viewForPhase(phase) {
  if (phase === "prep" || phase === "present") return "stage";
  if (phase === "analyze" || phase === "coach" || phase === "drill") return "coach";
  if (phase === "report") return "report";
  return "setup";
}
function showView(name) {
  el.viewSetup.classList.toggle("hidden", name !== "setup");
  el.viewStage.classList.toggle("hidden", name !== "stage");
  el.viewCoach.classList.toggle("hidden", name !== "coach");
  el.viewReport.classList.toggle("hidden", name !== "report");
  document.body.classList.toggle("wide-view", name === "coach");
}
function render() {
  showView(viewForPhase(state.phase));
  renderTopbar();
  renderAstraBubble();
  if (state.phase === "setup") renderSetup();
  else if (state.phase === "prep" || state.phase === "present") renderStage();
  else if (state.phase === "analyze" || state.phase === "coach" || state.phase === "drill") renderCoachView();
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
function renderAstraBubble() {
  const agent = state.lastAgentLine;
  if (agent) {
    el.astraLine.textContent = stripPauseMarkup(agent.text);
    el.astraLine.classList.toggle("partial", !agent.final);
  }
  const user = state.lastUserLine;
  el.userLine.classList.toggle("hidden", !user);
  if (user) {
    el.userLine.textContent = user.text;
    el.userLine.classList.toggle("partial", !user.final);
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

// -------------------------------------------------------------- Coach view
function renderCoachView() {
  renderScorecard();
  renderMetrics();
  renderCoachChatLog();
  renderCoachCard();
  renderCoachOptions();
  renderCoachStatus();
}
function renderCoachChatLog() {
  el.coachChatLog.innerHTML = "";
  const lines = Array.from(state.transcript.values()).slice(-60);
  lines.forEach((line) => {
    const isAstra = !line.speaker.startsWith("presenter-");
    const div = document.createElement("div");
    div.className = "chat-line " + (isAstra ? "astra" : "you") + (line.final ? "" : " partial");
    const who = document.createElement("span");
    who.className = "chat-who";
    who.textContent = isAstra ? coachName() : "You";
    const txt = document.createElement("span");
    txt.className = "chat-text";
    txt.textContent = stripPauseMarkup(line.text);
    div.append(who, txt);
    el.coachChatLog.appendChild(div);
  });
  el.coachChatLog.scrollTop = el.coachChatLog.scrollHeight;
}
function labeledLine(label, text) {
  const p = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = label + ": ";
  p.appendChild(strong);
  p.appendChild(document.createTextNode(text));
  return p;
}
function renderVerdictChips(v) {
  const wrap = document.createElement("div");
  wrap.className = "verdict-chip-row";
  const originalFillers = v.original_fillers != null ? v.original_fillers : "\u2014";
  const fillerCount = v.fillers ? v.fillers.count : "\u2014";
  const chips = [
    `fillers ${originalFillers} \u2192 ${fillerCount}`,
    v.pauses && v.pauses.landed ? "pause landed" : "no pause",
    `pace: ${v.pace_band || "unknown"}`,
    `on point ${v.on_point != null ? Math.round(v.on_point * 100) + "%" : "\u2014"}`,
  ];
  chips.forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "verdict-chip";
    chip.textContent = text;
    wrap.appendChild(chip);
  });
  if (v.wins && v.wins.length) {
    const wins = document.createElement("div");
    wins.className = "verdict-wins";
    wins.textContent = "Wins: " + v.wins.join(", ");
    wrap.appendChild(wins);
  }
  return wrap;
}
function renderCoachCard() {
  el.coachCard.innerHTML = "";
  const coach = state.coach;
  if (!coach || !coach.improvementId) {
    el.coachCard.classList.add("hidden");
    return;
  }
  const imp = ((state.judgment && state.judgment.improvements) || []).find((i) => i.id === coach.improvementId);
  if (!imp) {
    el.coachCard.classList.add("hidden");
    return;
  }
  el.coachCard.classList.remove("hidden");
  const head = document.createElement("div");
  head.className = "coach-card-head";
  head.textContent = coach.total ? `Improvement ${coach.index} of ${coach.total}` : "Improvement";
  const quote = document.createElement("p");
  quote.className = "improvement-quote";
  quote.textContent = `\u201c${imp.quote}\u201d`;
  const issue = document.createElement("p");
  issue.className = "improvement-issue";
  issue.textContent = imp.issue;
  el.coachCard.append(head, quote, issue, labeledLine("Cleaner", stripPauseMarkup(imp.v2_text || "")));
  if (imp.alternative) {
    el.coachCard.appendChild(labeledLine("Alternative", imp.alternative));
  }
  if (coach.stage === "verdict" && coach.verdict) {
    el.coachCard.appendChild(renderVerdictChips(coach.verdict));
  }
  const hist = state.coachHistory[coach.improvementId] || [];
  if (hist.length) {
    const box = document.createElement("div");
    box.className = "coach-history";
    hist.forEach((h) => {
      const row = document.createElement("div");
      row.className = "coach-history-row";
      const onPoint = h.verdict.on_point != null ? Math.round(h.verdict.on_point * 100) + "% on point" : "";
      const wins = h.verdict.wins && h.verdict.wins.length ? h.verdict.wins.join(", ") : onPoint;
      row.textContent = `Attempt ${h.attempt}: ${wins || "recorded"}`;
      box.appendChild(row);
    });
    el.coachCard.appendChild(box);
  }
}
const PRIMARY_COMMANDS = new Set(["proceed", "next", "practice"]);
function renderCoachOptions() {
  el.coachOptions.innerHTML = "";
  const options = (state.coach && state.coach.options) || [];
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    if (!PRIMARY_COMMANDS.has(opt.name)) btn.className = "secondary";
    btn.textContent = opt.label;
    btn.addEventListener("click", () => sendMessage({ type: "command", name: opt.name }));
    el.coachOptions.appendChild(btn);
  });
}
function renderCoachStatus() {
  const stage = state.coach && state.coach.stage;
  if (stage === "summary") {
    el.coachStatus.textContent = `${coachName()} is summarising\u2026`;
    return;
  }
  el.coachStatus.textContent = state.agentSpeaking ? `${coachName()} is speaking` : "listening \u2014 say it or tap an option";
}

// ------------------------------------------------------------- Report view
const SCORE_MAX = 5; // rubric scale per skills/judge/*.md examples in GATE0.md
function renderReport() {
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
    const attemptKeys = Object.keys(clips)
      .filter((k) => /^attempt\d+$/.test(k))
      .sort((a, b) => Number(a.slice(7)) - Number(b.slice(7)));
    attemptKeys.forEach((key) => {
      const n = key.slice(7);
      const row = document.createElement("div");
      row.className = "clip-row";
      const label = document.createElement("div");
      label.className = "clip-label";
      label.textContent = `Your take ${n}`;
      row.appendChild(label);
      const audio = document.createElement("audio");
      audio.controls = true;
      audio.src = clips[key];
      row.appendChild(audio);
      card.appendChild(row);
    });
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
  el.newTalkBtn.addEventListener("click", () => location.reload());
  el.gateStartBtn.addEventListener("click", () => startSession());
  el.unmutePill.addEventListener("click", async () => {
    try {
      await state.room.startAudio();
    } catch (err) {
      // Leave the pill up; the user can try again.
    }
    updateUnmutePill();
  });
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
  state.orbGate = createOrb(el.gateOrb);
  state.orbBubble = createOrb(el.astraIndicator);
  wireEvents();
  render();
  connect();
  requestAnimationFrame(levelLoop);
  // Test/debug hook: lets an external driver call the router directly.
  window.podiumDebug = { state, handleMessage, render, levels: smoothedLevels };
}
init();

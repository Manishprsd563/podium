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

// A stable per-browser id so cross-session progress (§8 Progress) can be
// attributed to the same person without an account system.
function generateUuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
function getClientId() {
  let id;
  try {
    id = localStorage.getItem("podium_client_id");
  } catch (err) {
    id = null;
  }
  if (!id) {
    id = generateUuid();
    try {
      localStorage.setItem("podium_client_id", id);
    } catch (err) {
      // localStorage unavailable (private mode etc.) - keep the in-memory id.
    }
  }
  return id;
}
const CLIENT_ID = getClientId();

const state = {
  room: null,
  connected: false,
  micEnabled: false,
  phase: "setup",
  budgetS: 60,
  deck: null,
  pendingDeckSlides: null,
  currentSlide: 1,
  remainingS: null, // prep countdown, agent-driven via `timer`
  presentStartedAt: null, // present clock, client-driven from `phase.budget_s`
  presentTimerId: null,
  transcript: new Map(), // segId -> {speaker, text, final}
  lastAgentLine: null, // {text, final}
  lastUserLine: null, // {text, final}
  metrics: null,
  judgment: null,
  clips: {}, // improvement_id -> {v0,v1,v2,v3,attempt<N>: url}
  provider: null,
  activeImprovementId: null,
  agentSpeaking: false,
  localSpeaking: false,
  coach: null, // {stage, options, improvementId, index, total, attempt, verdict}
  coachHistory: {}, // improvement_id -> [{attempt, verdict}]
  feedback: null, // {itemId, stage, text} - latest §5 `feedback` message
  feedbackPlayed: {}, // improvement_id -> Set of stages already cued
  drill: null, // {word, stage, confBefore, confAfter, url} - latest §5 `drill` message
  progress: null, // {level, levels, skills, nextFocus} - latest §5 `progress` message
  countdown: null, // {id, uiStatus, clips, clipIndex, abortController, errorKind, errorMessage, completedSent}
  beginPending: false, // true from Begin-now/Enter until phase leaves prep or the attempt fails
  gateOpen: true,
  agentPresent: false,
  audioCtx: null,
  analysers: {}, // agent|user -> {analyser, data}
  orbGate: null,
  orbBubble: null,
  orbCountdown: null,
  clientId: CLIENT_ID,
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
    "conn-badge", "mic-badge", "agent-badge", "provider-badge", "level-badge", "error-banner",
    "astra-indicator", "astra-line", "user-line",
    "gate", "gate-orb", "gate-status", "gate-start-btn", "unmute-pill",
    "view-setup", "view-stage", "view-coach",
    "setup-form", "setup-topic", "setup-level", "setup-duration",
    "upload-input", "upload-status", "upload-error",
    "setup-deck-preview", "deck-preview-list",
    "stage-status", "stage-prep", "prep-countdown", "prep-ready-btn",
    "stage-present", "present-timer", "slide-title", "slide-bullets",
    "slide-next-btn", "present-done-btn", "transcript-strip",
    "scorecard", "judgment-summary", "metrics-grid", "metrics-perslide",
    "coach-chat-log", "coach-card", "drill-card", "coach-transcript-strip",
    "coach-options", "coach-status",
    "progress-panel", "level-track", "skill-rows", "next-focus",
    "rerecord-slide", "rerecord-btn", "improvement-cards",
    "countdown-overlay", "countdown-loading", "countdown-orb", "countdown-loading-title",
    "countdown-playing", "countdown-number", "countdown-status",
    "countdown-error", "countdown-error-message", "countdown-retry-btn",
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
    resetCountdownState();
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

// ------------------------------------------------------------- present clock
// The agent only sends `timer` during prep; during present the client owns
// the clock, seeded from `phase.budget_s`, so a dropped/late `timer` message
// can never freeze the on-screen time.
function stopPresentClock() {
  if (state.presentTimerId) {
    clearInterval(state.presentTimerId);
    state.presentTimerId = null;
  }
  state.presentStartedAt = null;
}
function startPresentClock(budgetS) {
  stopPresentClock();
  if (budgetS != null) state.budgetS = budgetS;
  state.presentStartedAt = Date.now();
  state.presentTimerId = setInterval(render, 250);
}

// ---------------------------------------------------------- message router
function handleMessage(msg) {
  switch (msg.type) {
    case "phase": {
      const enteringPresent = msg.name === "present";
      const wasPrep = state.phase === "prep";
      state.phase = msg.name;
      if (msg.budget_s != null) state.budgetS = msg.budget_s;
      if (enteringPresent) startPresentClock(state.budgetS);
      else stopPresentClock();
      // The countdown overlay is only ever torn down by leaving prep (into
      // present on success, or elsewhere as a defensive cleanup) -- never by
      // a timeout -- so visuals can't outrun the audio that gates them.
      if (wasPrep && msg.name !== "prep") resetCountdownState();
      if (msg.name !== "prep") state.beginPending = false;
      break;
    }
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
    case "feedback": {
      const itemId = (msg.item && msg.item.id) || null;
      const stage = (msg.item && msg.item.stage) || null;
      const prev = state.feedback;
      if (itemId && (!prev || prev.itemId !== itemId)) {
        state.feedbackPlayed[itemId] = new Set();
      } else if (prev && itemId && prev.itemId === itemId && prev.stage && prev.stage !== stage && prev.stage !== "resume") {
        (state.feedbackPlayed[itemId] || (state.feedbackPlayed[itemId] = new Set())).add(prev.stage);
      }
      state.feedback = { itemId, stage, text: (msg.item && msg.item.text) || null };
      state.activeImprovementId = itemId;
      state.agentSpeaking = true;
      break;
    }
    case "clip":
      if (!state.clips[msg.improvement_id]) state.clips[msg.improvement_id] = {};
      state.clips[msg.improvement_id][msg.variant] = msg.url;
      state.agentSpeaking = true;
      break;
    case "drill":
      state.drill = {
        word: msg.word,
        stage: msg.stage,
        confBefore: msg.conf_before,
        confAfter: msg.conf_after,
        url: msg.url || null,
      };
      break;
    case "progress":
      state.progress = {
        clientId: msg.client_id || null,
        level: msg.level || null,
        levels: msg.levels || [],
        skills: msg.skills || {},
        nextFocus: msg.next_focus || null,
      };
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
      handleCountdownMessage(msg);
      break;
    case "provider":
      state.provider = { name: msg.name, model: msg.model, speaker: msg.speaker };
      markAgentPresent();
      break;
    case "error":
      showError(msg.message || "Unknown agent error");
      return;
    case "reset":
      handleReset();
      break;
    default:
      console.warn("unhandled podium message", msg);
      return;
  }
  render();
}

// ------------------------------------------------------------ countdown overlay
// The countdown is four short server-rendered clips (Three/Two/One/Begin)
// played back-to-back on ONE shared media element. The overlay label only
// ever changes on that element's own `playing`/`ended` events -- never on a
// local timer -- so the UI can't finish before the audio the user actually
// hears. `countdown_complete` fires exactly once, after the last `ended`.
const COUNTDOWN_AUDIO = new Audio();
COUNTDOWN_AUDIO.preload = "auto";

function agentAudioEl() {
  return document.getElementById("coach-audio");
}
function muteAgentAudio() {
  const a = agentAudioEl();
  if (a) a.muted = true;
}
function restoreAgentAudio() {
  const a = agentAudioEl();
  if (a) a.muted = false;
}
function renderCountdownOverlay() {
  const cur = state.countdown;
  if (!cur) {
    el.countdownOverlay.classList.add("hidden");
    return;
  }
  el.countdownOverlay.classList.remove("hidden");
  const showLoading = cur.uiStatus === "loading" || cur.uiStatus === "buffering";
  const showPlaying = cur.uiStatus === "playing";
  const showStarting = cur.uiStatus === "starting";
  const showError = cur.uiStatus === "error";
  el.countdownLoading.classList.toggle("hidden", !showLoading);
  el.countdownPlaying.classList.toggle("hidden", !showPlaying);
  el.countdownStatus.classList.toggle("hidden", !showStarting);
  el.countdownError.classList.toggle("hidden", !showError);
  if (showLoading) {
    el.countdownLoadingTitle.textContent = cur.uiStatus === "loading" ? "Preparing your countdown\u2026" : "Loading\u2026";
    if (state.orbCountdown) state.orbCountdown.setState("connecting");
  }
  if (showPlaying) {
    const clip = cur.clips[cur.clipIndex];
    el.countdownNumber.textContent = clip ? clip.label : "";
  }
  if (showStarting) {
    el.countdownStatus.textContent = "Starting microphone\u2026";
  }
  if (showError) {
    el.countdownErrorMessage.textContent = cur.errorMessage || "Something went wrong.";
    el.countdownRetryBtn.textContent = cur.errorKind === "autoplay" ? "Resume audio" : "Try again";
  }
}
// Cleans up the shared audio element, its listeners (via AbortController),
// and the mute it placed on the agent's track. Called on supersede, reset,
// disconnect, and leaving prep -- the only ways the overlay ever closes.
function resetCountdownState() {
  const cur = state.countdown;
  if (cur && cur.abortController) cur.abortController.abort();
  try { COUNTDOWN_AUDIO.pause(); } catch (err) { /* ignore */ }
  COUNTDOWN_AUDIO.removeAttribute("src");
  try { COUNTDOWN_AUDIO.load(); } catch (err) { /* ignore */ }
  state.countdown = null;
  restoreAgentAudio();
  renderCountdownOverlay();
}
function failCountdown(cur, kind, message) {
  if (state.countdown !== cur) return;
  try { COUNTDOWN_AUDIO.pause(); } catch (err) { /* ignore */ }
  cur.uiStatus = "error";
  cur.errorKind = kind;
  cur.errorMessage = message;
  // Autoplay refusal is not an agent-visible failure: the same clip just
  // needs a user gesture, so no countdown_failed and no Begin re-arm.
  if (kind !== "autoplay") {
    if (kind === "media") sendMessage({ type: "countdown_failed", id: cur.id });
    state.beginPending = false;
  }
  restoreAgentAudio();
  renderCountdownOverlay();
}
function finishCountdown(cur) {
  if (state.countdown !== cur) return;
  if (!cur.completedSent) {
    cur.completedSent = true;
    sendMessage({ type: "countdown_complete", id: cur.id });
  }
  cur.uiStatus = "starting";
  restoreAgentAudio();
  renderCountdownOverlay();
}
function attachClipListeners(cur, index, signal) {
  const isCurrent = () => state.countdown === cur && cur.clipIndex === index;
  COUNTDOWN_AUDIO.addEventListener("playing", () => {
    if (!isCurrent()) return;
    cur.uiStatus = "playing";
    renderCountdownOverlay();
  }, { signal });
  const onBuffering = () => {
    if (!isCurrent()) return;
    cur.uiStatus = "buffering";
    renderCountdownOverlay();
  };
  COUNTDOWN_AUDIO.addEventListener("waiting", onBuffering, { signal });
  COUNTDOWN_AUDIO.addEventListener("stalled", onBuffering, { signal });
  COUNTDOWN_AUDIO.addEventListener("ended", () => {
    if (!isCurrent()) return;
    if (index + 1 < cur.clips.length) playCountdownClip(cur, index + 1);
    else finishCountdown(cur);
  }, { signal });
  COUNTDOWN_AUDIO.addEventListener("error", () => {
    if (!isCurrent()) return;
    failCountdown(cur, "media", "Countdown audio failed to load.");
  }, { signal });
}
function playCountdownClip(cur, index) {
  if (state.countdown !== cur) return;
  cur.clipIndex = index;
  cur.uiStatus = "buffering"; // waiting for this clip's own `playing` event
  const clip = cur.clips[index];
  attachClipListeners(cur, index, cur.abortController.signal);
  COUNTDOWN_AUDIO.pause();
  COUNTDOWN_AUDIO.src = clip.url;
  renderCountdownOverlay();
  const playPromise = COUNTDOWN_AUDIO.play();
  if (playPromise && playPromise.catch) {
    playPromise.catch((err) => {
      if (state.countdown !== cur || cur.clipIndex !== index) return;
      if (err && err.name === "NotAllowedError") {
        failCountdown(cur, "autoplay", "Tap to resume the countdown.");
      } else {
        failCountdown(cur, "media", "Countdown audio failed to play.");
      }
    });
  }
}
function retryAutoplay(cur) {
  if (state.countdown !== cur || cur.uiStatus !== "error" || cur.errorKind !== "autoplay") return;
  cur.uiStatus = "buffering";
  cur.errorKind = null;
  cur.errorMessage = null;
  renderCountdownOverlay();
  const playPromise = COUNTDOWN_AUDIO.play();
  if (playPromise && playPromise.catch) {
    playPromise.catch((err) => {
      if (state.countdown !== cur) return;
      failCountdown(cur, err && err.name === "NotAllowedError" ? "autoplay" : "media", "Tap to resume the countdown.");
    });
  }
}
// Requests a fresh countdown attempt: the Begin button, prep Enter key, and
// the error overlay's "Try again" all funnel through here so a pending
// request can never be issued twice.
function requestBegin() {
  if (state.beginPending) return;
  state.beginPending = true;
  renderStage();
  sendMessage({ type: "ready" });
}
function handleCountdownMessage(msg) {
  const { id, status } = msg;
  if (!id) return;
  const cur = state.countdown;
  if (status === "loading") {
    if (cur && cur.id === id) return; // duplicate loading for the same id
    if (cur) resetCountdownState(); // a newer id supersedes whatever was in flight
    muteAgentAudio();
    state.countdown = {
      id,
      uiStatus: "loading",
      clips: null,
      clipIndex: -1,
      abortController: new AbortController(),
      errorKind: null,
      errorMessage: null,
      completedSent: false,
    };
    renderCountdownOverlay();
    return;
  }
  if (!cur || cur.id !== id) return; // stale/superseded id: ignore
  if (cur.completedSent) return; // already acked; late same-id messages are stale
  if (status === "ready") {
    if (cur.uiStatus !== "loading") return; // duplicate ready: already started, never replay
    const clips = Array.isArray(msg.clips) ? msg.clips : [];
    if (!clips.length) {
      failCountdown(cur, "media", "No countdown audio received.");
      return;
    }
    cur.clips = clips;
    playCountdownClip(cur, 0);
    return;
  }
  if (status === "error") {
    failCountdown(cur, "server", msg.message || "Could not prepare the countdown.");
    return;
  }
}
function handleReset() {
  resetCountdownState();
  stopPresentClock();
  state.deck = null;
  state.pendingDeckSlides = null;
  state.currentSlide = 1;
  state.remainingS = null;
  state.transcript = new Map();
  state.lastAgentLine = null;
  state.lastUserLine = null;
  state.metrics = null;
  state.judgment = null;
  state.clips = {};
  state.activeImprovementId = null;
  state.coach = null;
  state.coachHistory = {};
  state.feedback = null;
  state.feedbackPlayed = {};
  state.drill = null;
  state.beginPending = false;
  // Preserved on purpose: room/connected, micEnabled, provider, progress,
  // clientId, agentSpeaking/localSpeaking -- new_talk keeps the session live.
  if (el.errorBanner) {
    el.errorBanner.textContent = "";
    el.errorBanner.classList.add("hidden");
  }
  if (el.setupTopic) el.setupTopic.value = "";
  if (el.setupDeckPreview) el.setupDeckPreview.classList.add("hidden");
  if (el.deckPreviewList) el.deckPreviewList.innerHTML = "";
  if (el.uploadInput) el.uploadInput.value = "";
  if (el.uploadStatus) el.uploadStatus.textContent = "";
  if (el.uploadError) {
    el.uploadError.textContent = "";
    el.uploadError.classList.add("hidden");
  }
}

// ------------------------------------------------------------------ render
function viewForPhase(phase) {
  if (phase === "prep" || phase === "present") return "stage";
  if (phase === "analyze" || phase === "coach" || phase === "drill" || phase === "report") return "coach";
  return "setup";
}
function showView(name) {
  el.viewSetup.classList.toggle("hidden", name !== "setup");
  el.viewStage.classList.toggle("hidden", name !== "stage");
  el.viewCoach.classList.toggle("hidden", name !== "coach");
  document.body.classList.toggle("wide-view", name === "coach");
}
function render() {
  showView(viewForPhase(state.phase));
  renderTopbar();
  renderAstraBubble();
  if (state.phase === "setup") renderSetup();
  else if (state.phase === "prep" || state.phase === "present") renderStage();
  else renderCoachView(); // analyze | coach | drill | report -- one continuation dashboard
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
  renderLevelBadge();
}
function renderLevelBadge() {
  if (!el.levelBadge) return;
  if (state.progress && state.progress.level) {
    el.levelBadge.classList.remove("hidden");
    el.levelBadge.classList.add("level");
    el.levelBadge.textContent = state.progress.level;
  } else {
    el.levelBadge.classList.add("hidden");
  }
}
function renderAstraBubble() {
  const agent = state.lastAgentLine;
  // A cleared transcript (new-talk reset) falls back to the placeholder so a
  // stale line from the previous talk never survives a reset.
  el.astraLine.textContent = agent ? stripPauseMarkup(agent.text) : "Your coach is connecting\u2026";
  el.astraLine.classList.toggle("partial", !!agent && !agent.final);
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
    sendMessage({ type: "deck_upload", slides, client_id: state.clientId });
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
function renderPresentTimer() {
  if (state.presentStartedAt == null) {
    el.presentTimer.textContent = state.remainingS != null ? formatClock(state.remainingS) : "\u2014";
    el.presentTimer.classList.remove("over");
    return;
  }
  const elapsed = (Date.now() - state.presentStartedAt) / 1000;
  const remaining = state.budgetS - elapsed;
  if (remaining >= 0) {
    el.presentTimer.textContent = formatClock(remaining);
    el.presentTimer.classList.remove("over");
  } else {
    el.presentTimer.textContent = "+" + formatClock(-remaining);
    el.presentTimer.classList.add("over");
  }
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
    el.prepReadyBtn.disabled = state.beginPending;
    return;
  }
  renderPresentTimer();
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
    const who = line.speaker.startsWith("presenter-") ? "You" : coachName();
    div.textContent = `${who}: ${stripPauseMarkup(line.text)}`;
    el.transcriptStrip.appendChild(div);
  });
  el.transcriptStrip.scrollTop = el.transcriptStrip.scrollHeight;
}

// --------------------------------------------------------- Pronunciation
// The user's own spoken words, reconstructed from the live transcription
// stream (the only place the client sees the presentation's text). Words
// that Deepgram's per-word confidence flagged as low are underlined.
function userTranscriptText() {
  return Array.from(state.transcript.values())
    .filter((l) => l.final && l.speaker && l.speaker.startsWith("presenter-"))
    .map((l) => l.text)
    .join(" ");
}
function normalizeWord(tok) {
  return (tok || "").toLowerCase().replace(/[^a-z0-9']/g, "");
}
function renderPronunciationTranscript(container) {
  if (!container) return;
  container.innerHTML = "";
  const text = userTranscriptText();
  if (!text) {
    container.innerHTML = '<span class="hint">Waiting for your transcript\u2026</span>';
    return;
  }
  const low = (state.metrics && state.metrics.pronunciation && state.metrics.pronunciation.low_confidence) || [];
  const lowMap = new Map();
  low.forEach((w) => {
    const key = normalizeWord(w.w);
    if (key) lowMap.set(key, w.conf);
  });
  const tokens = text.split(/(\s+)/);
  tokens.forEach((tok) => {
    if (tok === "" ) return;
    if (/^\s+$/.test(tok)) {
      container.appendChild(document.createTextNode(tok));
      return;
    }
    const key = normalizeWord(tok);
    if (key && lowMap.has(key)) {
      const span = document.createElement("span");
      span.className = "low-conf-word";
      span.textContent = tok;
      span.title = `heard with ${Math.round(lowMap.get(key) * 100)}% confidence`;
      container.appendChild(span);
    } else {
      container.appendChild(document.createTextNode(tok));
    }
  });
}

// -------------------------------------------------------------- Coach view
function renderCoachView() {
  renderScorecard();
  renderMetrics();
  renderCoachChatLog();
  renderCoachCard();
  renderDrillCard();
  renderPronunciationTranscript(el.coachTranscriptStrip);
  renderCoachOptions();
  renderCoachStatus();
  renderRerecordSlideOptions();
  renderImprovementCards();
  renderProgressPanel();
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
// The four clip stages the coach cycles through for every improvement, plus
// a fifth "Your turn" pill for the practice prompt. Order matters: it is the
// order clips are recorded/rendered in (§1) and the order the coach plays them.
const FEEDBACK_PILLS = [
  { stage: "v0", label: "You" },
  { stage: "v2", label: "Cleaner" },
  { stage: "v3", label: "With pauses" },
  { stage: "alt", label: "Another opening" },
];
function renderFeedbackPills(container, coach) {
  container.innerHTML = "";
  const fb = state.feedback;
  const activeStage = fb && fb.itemId === coach.improvementId ? fb.stage : null;
  const played = state.feedbackPlayed[coach.improvementId] || new Set();
  FEEDBACK_PILLS.forEach(({ stage, label }) => {
    const pill = document.createElement("div");
    pill.className = "feedback-pill";
    if (activeStage === stage) pill.classList.add("active");
    if (played.has(stage)) pill.classList.add("played");
    const text = document.createElement("span");
    text.textContent = label;
    pill.appendChild(text);
    if (played.has(stage)) {
      const check = document.createElement("span");
      check.className = "pill-check";
      check.textContent = "\u2713";
      pill.appendChild(check);
    }
    container.appendChild(pill);
  });
  const promptPill = document.createElement("div");
  promptPill.className = "feedback-pill prompt-pill";
  if (activeStage === "prompt") promptPill.classList.add("active");
  promptPill.textContent = "Your turn";
  container.appendChild(promptPill);
  if (activeStage === "resume") {
    const flash = document.createElement("div");
    flash.className = "feedback-resume-flash";
    flash.textContent = "resuming\u2026";
    container.appendChild(flash);
  }
}
function renderCoachStepper(container, coach) {
  container.innerHTML = "";
  const total = coach.total || 3;
  for (let i = 1; i <= total; i++) {
    const dot = document.createElement("div");
    dot.className = "step-dot" + (i === coach.index ? " active" : i < coach.index ? " done" : "");
    dot.textContent = String(i);
    container.appendChild(dot);
  }
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

  const stepper = document.createElement("div");
  stepper.className = "coach-stepper";
  renderCoachStepper(stepper, coach);

  const slideIndex = imp.slide != null ? imp.slide : state.currentSlide;
  const slides = (state.deck && state.deck.slides) || [];
  const slideObj = slides.find((s) => s.index === slideIndex) || null;
  const slideCard = document.createElement("div");
  slideCard.className = "slide-card coach-slide-card";
  const slideTitle = document.createElement("div");
  slideTitle.className = "slide-title";
  slideTitle.textContent = slideObj ? slideObj.title : (slideIndex != null ? `Slide ${slideIndex}` : "\u2014");
  const slideBullets = document.createElement("ul");
  slideBullets.className = "slide-bullets";
  ((slideObj && slideObj.bullets) || []).forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    slideBullets.appendChild(li);
  });
  slideCard.append(slideTitle, slideBullets);

  const quote = document.createElement("p");
  quote.className = "improvement-quote highlighted";
  quote.textContent = `\u201c${imp.quote}\u201d`;

  const isIntro = state.feedback && state.feedback.itemId === coach.improvementId && state.feedback.stage === "intro";
  const issue = document.createElement("p");
  issue.className = "improvement-issue" + (isIntro ? " active" : "");
  issue.textContent = imp.issue;

  const pillRow = document.createElement("div");
  pillRow.className = "feedback-pill-row";
  renderFeedbackPills(pillRow, coach);

  el.coachCard.append(head, stepper, slideCard, quote, issue, pillRow);

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
const DRILL_STAGE_LABELS = {
  you: "how it came through",
  model: "coach's version",
  prompt: "say it",
  verdict: "before / after",
};
function renderDrillCard() {
  if (!el.drillCard) return;
  const d = state.drill;
  if (!d || state.phase !== "drill") {
    el.drillCard.classList.add("hidden");
    return;
  }
  el.drillCard.classList.remove("hidden");
  el.drillCard.innerHTML = "";
  const head = document.createElement("div");
  head.className = "coach-card-head";
  head.textContent = `Drill: \u201c${d.word}\u201d`;
  const stageLine = document.createElement("p");
  stageLine.textContent = DRILL_STAGE_LABELS[d.stage] || d.stage;
  el.drillCard.append(head, stageLine);
  if (d.stage === "verdict") {
    const p = document.createElement("p");
    const before = d.confBefore != null ? Math.round(d.confBefore * 100) + "%" : "\u2014";
    const after = d.confAfter != null ? Math.round(d.confAfter * 100) + "%" : "\u2014";
    p.textContent = `${before} \u2192 ${after}`;
    el.drillCard.appendChild(p);
  }
  if (d.url) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = d.url;
    el.drillCard.appendChild(audio);
  }
}
const PRIMARY_COMMANDS = new Set(["proceed", "next", "practice", "more"]);
function doRerecord() {
  const slide = Number(el.rerecordSlide.value);
  if (!slide) return;
  sendMessage({ type: "rerecord", slide });
}
function renderCoachOptions() {
  el.coachOptions.innerHTML = "";
  const options = (state.coach && state.coach.options) || [];
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    if (!PRIMARY_COMMANDS.has(opt.name)) btn.className = "secondary";
    btn.textContent = opt.label;
    // wrap's "rerecord" option reuses the Recordings slide selector instead
    // of a bare command -- the agent needs the actual slide number.
    if (opt.name === "rerecord") btn.addEventListener("click", () => doRerecord());
    else btn.addEventListener("click", () => sendMessage({ type: "command", name: opt.name }));
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

// ------------------------------------------------------ Recordings & path
const SCORE_MAX = 5; // rubric scale per skills/judge/*.md examples in GATE0.md
// The TEDx skill ladder (§9 curriculum), in the fixed order the path panel
// always shows all ten in, whether or not the judge has trained them yet.
const SKILL_IDS = [
  "hook", "structure", "pacing", "pausing", "fillers",
  "vocal-variety", "storytelling", "slide-connection", "closing", "articulation",
];
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
  if (m.pronunciation && m.pronunciation.intelligibility != null) {
    el.metricsGrid.append(metricTile("intelligibility", `${Math.round(m.pronunciation.intelligibility * 100)}%`));
  }
  if (m.time_budget && m.time_budget.over_s > 0) {
    el.metricsGrid.append(metricTile("over budget", `+${m.time_budget.over_s.toFixed(1)}s`));
  }
  if (m.time_budget && m.time_budget.per_slide && m.time_budget.per_slide.length) {
    const parts = m.time_budget.per_slide.map((row) => `slide ${row.slide}: ${row.used_s.toFixed(0)}s (fair ${row.fair_share_s.toFixed(0)}s)`);
    el.metricsPerslide.textContent = "Per slide: " + parts.join(" \u00b7 ");
  }
}
function renderProgressPanel() {
  if (!el.progressPanel) return;
  const p = state.progress;
  el.progressPanel.classList.toggle("hidden", !p);
  if (!p) return;
  el.levelTrack.innerHTML = "";
  (p.levels || []).forEach((lvl) => {
    const chip = document.createElement("div");
    chip.className = "level-chip" + (lvl === p.level ? " current" : "");
    chip.textContent = lvl;
    el.levelTrack.appendChild(chip);
  });
  el.skillRows.innerHTML = "";
  SKILL_IDS.forEach((id) => {
    const s = (p.skills && p.skills[id]) || { mastery: 0 };
    const row = document.createElement("div");
    row.className = "skill-row" + (id === p.nextFocus ? " focus" : "");
    const label = document.createElement("div");
    label.className = "skill-label";
    label.textContent = id.replace(/-/g, " ");
    const track = document.createElement("div");
    track.className = "skill-bar-track";
    const fill = document.createElement("div");
    fill.className = "skill-bar-fill";
    fill.style.width = `${Math.round(Math.max(0, Math.min(1, s.mastery || 0)) * 100)}%`;
    track.appendChild(fill);
    row.append(label, track);
    el.skillRows.appendChild(row);
  });
  el.nextFocus.textContent = p.nextFocus ? `Next up: ${p.nextFocus.replace(/-/g, " ")}` : "";
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
const CLIP_LABELS = { v0: "You (recording)", v1: "You said", v2: "Cleaner", v3: "With pauses" };
const CLIP_VARIANT_ORDER = ["v0", "v1", "v2", "v3"];
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
    CLIP_VARIANT_ORDER.forEach((variant) => {
      // v0/v2/v3 always render a row (pending clips show "(rendering…)");
      // v1 is a legacy/optional variant, shown only if it exists.
      if (variant === "v1" && !clips.v1) return;
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
      client_id: state.clientId,
    });
  });
  el.uploadInput.addEventListener("change", () => {
    const file = el.uploadInput.files[0];
    if (file) handlePdfUpload(file);
  });
  el.prepReadyBtn.addEventListener("click", () => requestBegin());
  el.slideNextBtn.addEventListener("click", () => sendMessage({ type: "slide_next" }));
  el.presentDoneBtn.addEventListener("click", () => sendMessage({ type: "present_end" }));
  el.rerecordBtn.addEventListener("click", () => doRerecord());
  el.countdownRetryBtn.addEventListener("click", () => {
    const cur = state.countdown;
    if (!cur || cur.uiStatus !== "error") return;
    if (cur.errorKind === "autoplay") retryAutoplay(cur);
    else requestBegin();
  });
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
    if (state.phase === "prep" && e.code === "Enter") {
      e.preventDefault();
      requestBegin();
      return;
    }
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
  state.orbCountdown = createOrb(el.countdownOrb);
  wireEvents();
  render();
  connect();
  requestAnimationFrame(levelLoop);
  // Test/debug hook: lets an external driver call the router directly.
  window.podiumDebug = { state, handleMessage, render, levels: smoothedLevels };
}
init();

// Podium web client. No build step: plain ES module, deps from the import
// map (CDN). Voice-first shell: orb + caption + one slot view at a time.
import { Room, RoomEvent, Track } from "livekit-client";
import { gsap } from "gsap";
import Lenis from "lenis";
import { createOrb } from "./orb.js";
import { createDashboard } from "./dashboard.js";

const PDFJS_URL = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.min.mjs";
const PDFJS_WORKER_URL = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.min.mjs";

// A fresh room per page load: an agent job is dispatched per room, and a
// job's AgentSession closes when its participant leaves, so reusing one room
// name would rejoin a dead session ("AgentSession isn't running"). `?room=`
// overrides.
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

// state shape per contract §C. Additional fields beyond the documented ones
// are internal bookkeeping app.js needs; dashboard.js only reads the
// documented fields.
const state = {
  connected: false,
  reconnecting: false,
  agentPresent: false,
  audioUnlocked: false,
  micEnabled: false,
  phase: "setup",
  attention: "listening",
  agentSpeaking: false,
  userSpeaking: false,
  view: "none",
  layout: "center",
  focus: null,
  deck: null,
  currentSlide: 1,
  budgetS: 60,
  remainingS: null,
  presentStartedAt: null,
  metrics: null,
  judgment: null,
  judgmentPrevious: null,
  judgmentHistory: [],
  clips: {},
  coach: null,
  coachHistory: {},
  feedback: null,
  feedbackPlayed: {},
  drill: null,
  progress: null,
  provider: null,
  caption: null, // {segId, text, final}
  userLine: null, // {text, final}
  countdown: null, // {id, uiStatus, clips, clipIndex, abortController, errorKind, errorMessage, completedSent}
  turn: { seq: -1, owner: "agent", expect: null, allow: [], hint: "" },
  setup: { topic: null, level: null, budget_s: null, source: null, missing: [] },
  floor: "agent", // "agent" | "user" -- who currently holds the microphone (push-to-talk)
  floorLatched: false,

  // internal only, not part of the documented contract shape
  room: null,
  everConnected: false,
  wakeCompleted: false,
  pendingDeckSlides: null,
  presentTimerId: null,
  beginPending: false,
  transcript: new Map(),
  clientId: CLIENT_ID,
  audioCtx: null,
  analysers: {},
  orb: null,
};

const el = {};
function q(id) { return document.getElementById(id); }
function toCamel(id) {
  return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}
function stripPauseMarkup(text) {
  return (text || "").replace(/<\d+>/g, "");
}
function cacheEls() {
  [
    "stage", "hero", "orb-wrap", "orb", "caption-col", "caption", "user-line", "wake",
    "slot",
    "view-setup", "setup-form", "setup-topic", "setup-level", "setup-length",
    "setup-upload-btn", "setup-upload-input", "setup-status", "setup-submit", "setup-hint",
    "view-prep", "prep-number",
    "view-countdown", "countdown-ring", "countdown-number", "countdown-status",
    "countdown-error", "countdown-error-message", "countdown-retry",
    "view-present", "present-clock", "present-title", "present-bullets",
    "present-counter", "present-prev", "present-next", "present-done",
    "view-dashboard",
    "choices",
    "unmute", "toast",
    "sb-conn", "sb-agent", "sb-provider", "sb-attention", "sb-phase",
    "sb-stage", "sb-mic", "sb-room", "sb-level", "sb-level-fill",
  ].forEach((id) => { el[toCamel(id)] = q(id); });
}

// -------------------------------------------------------------- motion
let reducedMotion = false;
let mm;
function setupMatchMedia() {
  mm = gsap.matchMedia();
  mm.add("(prefers-reduced-motion: reduce)", () => {
    reducedMotion = true;
    return () => { reducedMotion = false; };
  });
}
function dur(seconds) { return reducedMotion ? 0 : seconds; }

let dash = null;

// ---------------------------------------------------------------- connect
async function connect() {
  const identity = "presenter-" + Math.random().toString(36).slice(2, 8);
  let room;
  try {
    const res = await fetch(`/token?room=${encodeURIComponent(ROOM_NAME)}&identity=${encodeURIComponent(identity)}`);
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || `token request failed (${res.status})`);
    room = new Room({ adaptiveStream: true, dynacast: true });
    wireRoom(room, identity);
    await room.connect(payload.url, payload.token);
  } catch (err) {
    state.connected = false;
    showToast(`Could not connect: ${err.message || err}`);
    render();
    return;
  }
  state.room = room;
  state.connected = true;
  state.everConnected = true;
  // A remote participant already in the room when we attach listeners won't
  // fire ParticipantConnected retroactively, so check directly.
  if (room.remoteParticipants && room.remoteParticipants.size > 0) markAgentPresent();
  render();
  maybeAutoWake();
}
function wireRoom(room, identity) {
  room.registerTextStreamHandler("lk.transcription", async (reader, participantInfo) => {
    // Chunks arrive synchronised with the agent's audio playout, so the
    // caption is built word by word as the coach speaks, not at segment end.
    try {
      const info = reader.info;
      const segId = (info.attributes && info.attributes["lk.segment_id"]) || info.id;
      const speaker = (participantInfo && participantInfo.identity) || "agent";
      let text = "";
      for await (const chunk of reader) {
        text += chunk;
        upsertTranscriptLine(segId, speaker, text, false);
        render();
      }
      const isFinal = !!(reader.info.attributes && reader.info.attributes["lk.transcription_final"] === "true");
      upsertTranscriptLine(segId, speaker, text, isFinal);
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
    state.userSpeaking = speakers.some((p) => p.identity === identity);
    render();
  });
  room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
    if (track.kind === Track.Kind.Audio && participant.identity !== identity) {
      // livekit-client does not auto-play subscribed audio: without attach()
      // the coach is silent in every browser, gesture or not. The element is
      // what room.startAudio() unblocks, and Chrome only feeds a remote
      // track into Web Audio while a media element is playing it.
      // A second subscription without an intervening unsubscribe (seen on
      // reconnect) must not leave two elements playing the same track
      // slightly offset -- drop the old one first.
      const existing = document.getElementById("coach-audio");
      if (existing) existing.remove();
      const audioEl = track.attach();
      audioEl.id = "coach-audio";
      audioEl.setAttribute("aria-hidden", "true");
      audioEl.muted = agentMuteReasons.size > 0;
      document.body.appendChild(audioEl);
      attachAnalyser(track.mediaStreamTrack, "agent");
    }
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => {
    if (track.kind === Track.Kind.Audio) track.detach().forEach((node) => node.remove());
  });
  room.on(RoomEvent.ParticipantConnected, () => markAgentPresent());
  room.on(RoomEvent.AudioPlaybackStatusChanged, () => updateUnmutePill());
  room.on(RoomEvent.Reconnecting, () => {
    state.reconnecting = true;
    render();
  });
  room.on(RoomEvent.Reconnected, () => {
    state.reconnecting = false;
    render();
  });
  room.on(RoomEvent.Disconnected, () => {
    state.connected = false;
    state.reconnecting = false;
    state.agentPresent = false;
    if (state.coach) state.coach.options = [];
    resetCountdownState();
    render();
  });
}
// v4 input gate (CONTRACTS.md §5): the agent is authoritative on `turn`, but
// the client must not even attempt a gated message -- these four are the
// exception, sent regardless of who currently holds the floor.
const UNGATED = new Set(["client_ready", "countdown_complete", "countdown_failed", "mic"]);
function sendMessage(msg) {
  if (!UNGATED.has(msg.type) && (state.turn.owner !== "user" || !state.turn.allow.includes(msg.type))) {
    console.debug("turn gate: refusing to send", msg.type, state.turn);
    return false;
  }
  if (!state.room) {
    showToast("Not connected yet \u2014 message not sent.");
    return false;
  }
  const data = new TextEncoder().encode(JSON.stringify(msg));
  state.room.localParticipant.publishData(data, { reliable: true, topic: "podium" });
  return true;
}
// Wrapper for direct user gestures (buttons, form submit, keyboard shortcuts):
// surfaces the one refusal reason the user can act on -- the floor isn't
// theirs yet -- without double-toasting the "not connected" case, which
// sendMessage already reports itself.
function sendUserAction(msg) {
  const ok = sendMessage(msg);
  if (!ok && state.room) showToast("Podium is still talking \u2014 one sec.");
  return ok;
}
function upsertTranscriptLine(segId, speaker, text, isFinal) {
  state.transcript.set(segId, { speaker, text, final: isFinal });
  if (state.transcript.size > 60) {
    const firstKey = state.transcript.keys().next().value;
    state.transcript.delete(firstKey);
  }
  const isAgent = !speaker.startsWith("presenter-");
  if (isAgent) {
    state.caption = { segId, text, final: isFinal };
  } else {
    state.userLine = { text, final: isFinal };
  }
}

// ------------------------------------------------------------- wake / gate
// Chrome blocks remote audio until the page has had a user gesture. We try
// to unlock automatically as soon as the agent is present (a page load is
// not itself a gesture, so this often fails silently); if it does, #wake
// asks for the first tap/keypress anywhere, which is a real gesture.
let wakeInFlight = false;
async function attemptWake() {
  if (wakeInFlight || !state.room || !state.connected) return;
  wakeInFlight = true;
  if (!state.audioUnlocked) {
    try {
      await state.room.startAudio();
    } catch (err) {
      wakeInFlight = false;
      showWake();
      return;
    }
    if (!state.room.canPlaybackAudio) {
      wakeInFlight = false;
      showWake();
      return;
    }
    state.audioUnlocked = true;
    hideWake();
    render();
  }
  if (state.wakeCompleted || !state.agentPresent) {
    wakeInFlight = false;
    return;
  }
  state.wakeCompleted = true;
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
    // Closed by default outside push-to-talk/present: this is the one-time
    // mic acquisition (constraints load-bearing for the loudness metric);
    // muting the published track here doesn't touch the publication or the
    // analyser, both of which stay alive for setFloor to flip later. A Space
    // gesture can open the floor while permission is still resolving, so
    // preserve that requested state once the track appears.
    const pub = getMicPublication();
    if (pub && pub.track) {
      if (state.phase === "present" || state.floor === "user") pub.track.unmute();
      else pub.track.mute();
    }
  } catch (err) {
    showToast(`Connected, but microphone access failed: ${err.message || err}. Grant mic permission and reload to present.`);
  }
  sendMessage({ type: "client_ready" });
  wakeInFlight = false;
  render();
}
function maybeAutoWake() {
  if (state.audioUnlocked || !state.connected || !state.agentPresent) return;
  attemptWake();
}
function markAgentPresent() {
  if (state.agentPresent) return;
  state.agentPresent = true;
  render();
  maybeAutoWake();
}
function showWake() {
  el.wake.classList.remove("hidden");
  gsap.fromTo(el.wake, { autoAlpha: 0 }, { autoAlpha: 1, duration: dur(0.4) });
}
function hideWake() {
  if (el.wake.classList.contains("hidden")) return;
  gsap.to(el.wake, { autoAlpha: 0, duration: dur(0.3), onComplete: () => el.wake.classList.add("hidden") });
}
function updateUnmutePill() {
  if (!state.room) {
    el.unmute.classList.add("hidden");
    return;
  }
  const blocked = state.audioUnlocked && !state.room.canPlaybackAudio;
  el.unmute.classList.toggle("hidden", !blocked);
}
function globalPointerHandler() {
  if (state.everConnected && !state.connected) {
    location.reload();
    return;
  }
  attemptWake();
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
  const pub = getMicPublication();
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
let lastOrbState = null;
function computeOrbState() {
  if (!state.connected) return state.everConnected ? "disconnected" : "connecting";
  if (state.reconnecting) return "reconnecting";
  if (!(state.connected && state.agentPresent && state.audioUnlocked)) return "connecting";
  if (state.floor === "user") return "micLive";
  const agentActive = smoothedLevels.agent > SPEAK_THRESHOLD || state.agentSpeaking;
  if (agentActive) return "speaking";
  const userActive = smoothedLevels.user > SPEAK_THRESHOLD || state.userSpeaking;
  if (userActive) return "listening";
  if (state.attention === "waiting") return "waiting";
  if (state.attention === "thinking") return "thinking";
  return "idle";
}
function levelLoop() {
  smoothedLevels.agent += (sampleLevel(state.analysers.agent) - smoothedLevels.agent) * 0.3;
  smoothedLevels.user += (sampleLevel(state.analysers.user) - smoothedLevels.user) * 0.3;
  if (state.orb) {
    state.orb.setLevel(state.floor === "user" ? smoothedLevels.user : smoothedLevels.agent);
    const next = computeOrbState();
    if (next !== lastOrbState) {
      state.orb.setState(next);
      lastOrbState = next;
    }
  }
  renderLevelMeter();
  requestAnimationFrame(levelLoop);
}
function renderLevelMeter() {
  if (el.sbLevelFill) el.sbLevelFill.style.transform = `scaleX(${Math.max(0, Math.min(1, smoothedLevels.agent))})`;
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
    case "attention":
      state.attention = msg.state;
      break;
    case "turn": {
      if (typeof msg.seq !== "number" || msg.seq <= state.turn.seq) return;
      state.turn = { seq: msg.seq, owner: msg.owner, expect: msg.expect || null, allow: msg.allow || [], hint: msg.hint || "" };
      break;
    }
    case "setup_state": {
      state.setup = {
        topic: msg.topic != null ? msg.topic : null,
        level: msg.level != null ? msg.level : null,
        budget_s: msg.budget_s != null ? msg.budget_s : null,
        source: msg.source || null,
        missing: msg.missing || [],
      };
      const topicFocused = document.activeElement === el.setupTopic;
      if (state.setup.topic && !topicFocused && (!el.setupTopic.value || state.setup.source === "voice")) {
        el.setupTopic.value = state.setup.topic;
      }
      if (state.setup.level != null) el.setupLevel.value = state.setup.level;
      if (state.setup.budget_s != null) el.setupLength.value = String(state.setup.budget_s);
      break;
    }
    case "focus":
      state.focus = msg.target ? { target: msg.target, key: msg.key || null } : null;
      if (state.orb) state.orb.pulse();
      break;
    case "phase": {
      const enteringPresent = msg.name === "present";
      const leavingPresent = state.phase === "present" && msg.name !== "present";
      const hadCountdown = !!state.countdown;
      state.phase = msg.name;
      if (msg.budget_s != null) state.budgetS = msg.budget_s;
      if (enteringPresent || msg.name === "analyze") {
        // The floor is the presenter's during present, and the coach's after:
        // neither the prep line nor the last transcript fragment should linger.
        if (enteringPresent) state.caption = null;
        state.userLine = null;
      }
      // Coach options belong to the stage that offered them: presenting (or
      // preparing to) with the previous item's strip still on screen would
      // invite a command the agent no longer accepts.
      if (state.coach && (enteringPresent || msg.name === "prep" || msg.name === "analyze")) state.coach.options = [];
      if (enteringPresent) {
        startPresentClock(state.budgetS);
        setPresentMicForced(true);
      } else {
        stopPresentClock();
        if (leavingPresent) setPresentMicForced(false);
      }
      // The countdown is only ever torn down by a phase change (into present
      // on success, or elsewhere as a defensive cleanup) -- never by a
      // timeout -- so visuals can't outrun the audio that gates them. A
      // re-record from the dashboard runs the countdown in `report`, so this
      // is keyed on the countdown existing, not on leaving prep.
      if (hadCountdown && msg.name !== "prep") resetCountdownState();
      if (msg.name !== "prep") state.beginPending = false;
      break;
    }
    case "deck":
      state.deck = msg.deck;
      state.pendingDeckSlides = null;
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
    case "judgment": {
      state.judgment = msg.judgment;
      state.judgmentPrevious = msg.previous || null;
      const rev = msg.revision;
      if (rev != null && !state.judgmentHistory.some((h) => h.revision === rev)) {
        state.judgmentHistory.push({ revision: rev, scores: msg.judgment && msg.judgment.scores });
      }
      break;
    }
    case "feedback": {
      const itemId = (msg.item && msg.item.id) || null;
      const stage = (msg.item && msg.item.stage) || null;
      const prev = state.feedback;
      if (itemId && (!prev || prev.itemId !== itemId)) {
        state.feedbackPlayed[itemId] = new Set();
      } else if (prev && itemId && prev.itemId === itemId && prev.stage && prev.stage !== stage && prev.stage !== "resume") {
        (state.feedbackPlayed[itemId] || (state.feedbackPlayed[itemId] = new Set())).add(prev.stage);
      }
      const role = (msg.item && msg.item.role) || "coach";
      const markup = (msg.item && msg.item.markup) || null;
      const available = msg.item && msg.item.available != null ? msg.item.available : true;
      state.feedback = { itemId, stage, text: (msg.item && msg.item.text) || null, role, markup, available };
      state.agentSpeaking = true;
      if (state.orb) state.orb.pulse();
      break;
    }
    case "clip":
      if (!state.clips[msg.improvement_id]) state.clips[msg.improvement_id] = {};
      state.clips[msg.improvement_id][msg.variant] = msg.url;
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
    case "coach": {
      state.coach = {
        stage: msg.stage,
        options: msg.options || [],
        improvementId: msg.improvement_id != null ? msg.improvement_id : null,
        index: msg.index != null ? msg.index : null,
        total: msg.total != null ? msg.total : null,
        attempt: msg.attempt != null ? msg.attempt : null,
        verdict: msg.verdict || null,
      };
      if (msg.stage === "verdict" && msg.improvement_id && msg.verdict) {
        const hist = state.coachHistory[msg.improvement_id] || (state.coachHistory[msg.improvement_id] = []);
        if (!hist.some((h) => h.attempt === msg.attempt)) {
          hist.push({ attempt: msg.attempt, verdict: msg.verdict });
        }
      }
      break;
    }
    case "countdown":
      handleCountdownMessage(msg);
      break;
    case "provider":
      state.provider = { name: msg.name, model: msg.model, speaker: msg.speaker };
      markAgentPresent();
      break;
    case "error":
      showToast(msg.message || "Unknown agent error");
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

// ------------------------------------------------------------ countdown machine
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
// Two independent reasons can want the coach's audio silent at once (the
// countdown overlay and the user holding the push-to-talk floor); a Set
// means either one's restore can't clobber the other's still-active mute.
const agentMuteReasons = new Set();
function muteAgentAudio(reason) {
  agentMuteReasons.add(reason);
  const a = agentAudioEl();
  if (a) a.muted = true;
}
function restoreAgentAudio(reason) {
  agentMuteReasons.delete(reason);
  if (agentMuteReasons.size > 0) return;
  const a = agentAudioEl();
  if (a) a.muted = false;
}

// ------------------------------------------------------------- push-to-talk
// Outside `present` the mic stays published but muted; the floor opens only
// while the user holds Space, or has double-tapped it to latch (see
// wireEvents' keydown/keyup). `present` forces the floor open for the whole
// take through setPresentMicForced instead -- no mic message, no coach-audio
// mute, since the agent already knows the phase and its own cues must stay
// audible over the presenter.
function getMicPublication() {
  return state.room && state.room.localParticipant.getTrackPublication(Track.Source.Microphone);
}
function setFloor(open, opts) {
  const nextFloor = open ? "user" : "agent";
  const changed = state.floor !== nextFloor;
  const pub = getMicPublication();
  if (changed && pub && pub.track) {
    if (open) pub.track.unmute();
    else pub.track.mute();
  }
  if (changed) sendMessage({ type: "mic", open });
  state.floor = nextFloor;
  state.floorLatched = open ? !!(opts && opts.latched) : false;
  if (open) muteAgentAudio("floor");
  else restoreAgentAudio("floor");
  render();
}
function setPresentMicForced(on) {
  const pub = getMicPublication();
  if (pub && pub.track) {
    if (on) pub.track.unmute();
    else pub.track.mute();
  }
  // Present cues must remain audible even if a latched floor happened to be
  // open before the countdown completed.
  restoreAgentAudio("floor");
  state.floor = on ? "user" : "agent";
  state.floorLatched = false;
}
function renderCountdownView() {
  const cur = state.countdown;
  if (!cur) return;
  const showPlaying = cur.uiStatus === "playing";
  const showError = cur.uiStatus === "error";
  const showRing = !showPlaying && !showError;
  el.countdownRing.classList.toggle("hidden", !showRing);
  el.countdownNumber.classList.toggle("hidden", !showPlaying);
  el.countdownStatus.classList.toggle("hidden", !showRing);
  el.countdownError.classList.toggle("hidden", !showError);
  if (showPlaying) {
    const clip = cur.clips && cur.clips[cur.clipIndex];
    el.countdownNumber.textContent = clip ? clip.label : "";
  }
  if (cur.uiStatus === "loading") el.countdownStatus.textContent = "Preparing your countdown\u2026";
  else if (cur.uiStatus === "buffering") el.countdownStatus.textContent = "Loading\u2026";
  else if (cur.uiStatus === "starting") el.countdownStatus.textContent = "Starting microphone\u2026";
  else el.countdownStatus.textContent = "";
  if (showError) {
    el.countdownErrorMessage.textContent = cur.errorMessage || "Something went wrong.";
    el.countdownRetry.textContent = cur.errorKind === "autoplay" ? "Resume audio" : "Try again";
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
  restoreAgentAudio("countdown");
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
  restoreAgentAudio("countdown");
  renderCountdownView();
}
function finishCountdown(cur) {
  if (state.countdown !== cur) return;
  if (!cur.completedSent) {
    cur.completedSent = true;
    sendMessage({ type: "countdown_complete", id: cur.id });
  }
  cur.uiStatus = "starting";
  restoreAgentAudio("countdown");
  renderCountdownView();
}
function attachClipListeners(cur, index, signal) {
  const isCurrent = () => state.countdown === cur && cur.clipIndex === index;
  COUNTDOWN_AUDIO.addEventListener("playing", () => {
    if (!isCurrent()) return;
    cur.uiStatus = "playing";
    renderCountdownView();
  }, { signal });
  const onBuffering = () => {
    if (!isCurrent()) return;
    cur.uiStatus = "buffering";
    renderCountdownView();
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
  renderCountdownView();
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
  renderCountdownView();
  const playPromise = COUNTDOWN_AUDIO.play();
  if (playPromise && playPromise.catch) {
    playPromise.catch((err) => {
      if (state.countdown !== cur) return;
      failCountdown(cur, err && err.name === "NotAllowedError" ? "autoplay" : "media", "Tap to resume the countdown.");
    });
  }
}
// Requests a fresh countdown attempt: prep's Enter key and the error
// overlay's "Try again" both funnel through here so a pending request can
// never be issued twice.
function requestBegin() {
  if (state.beginPending) return;
  if (!sendUserAction({ type: "ready" })) return;
  state.beginPending = true;
  render();
}
function handleCountdownMessage(msg) {
  const { id, status } = msg;
  if (!id) return;
  const cur = state.countdown;
  if (status === "loading") {
    if (cur && cur.id === id) return; // duplicate loading for the same id
    if (cur) resetCountdownState(); // a newer id supersedes whatever was in flight
    muteAgentAudio("countdown");
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
  state.caption = null;
  state.userLine = null;
  state.metrics = null;
  state.judgment = null;
  state.judgmentPrevious = null;
  state.judgmentHistory = [];
  state.clips = {};
  state.coach = null;
  state.coachHistory = {};
  state.feedback = null;
  state.feedbackPlayed = {};
  state.drill = null;
  state.beginPending = false;
  state.focus = null;
  state.setup = { topic: null, level: null, budget_s: null, source: null, missing: [] };
  // Preserved on purpose: room/connected, micEnabled, provider, progress,
  // clientId, audioUnlocked, agentSpeaking/userSpeaking -- new_talk keeps
  // the session live.
  hideToast();
  if (el.setupTopic) el.setupTopic.value = "";
  if (el.setupStatus) el.setupStatus.textContent = "";
}

// ------------------------------------------------------------------- toast
let toastTimer = null;
function showToast(text) {
  el.toast.textContent = text;
  el.toast.classList.remove("hidden");
  gsap.fromTo(el.toast, { autoAlpha: 0, y: 8 }, { autoAlpha: 1, y: 0, duration: dur(0.3) });
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, 6000);
}
function hideToast() {
  clearTimeout(toastTimer);
  if (el.toast.classList.contains("hidden")) return;
  gsap.to(el.toast, { autoAlpha: 0, y: 8, duration: dur(0.25), onComplete: () => el.toast.classList.add("hidden") });
}

// ------------------------------------------------------------------ view derivation (contract §D)
function deriveView() {
  if (state.everConnected && !state.connected) return "none"; // orb alone, red, centred
  if (state.countdown) return "countdown"; // prep, or a re-record from the dashboard
  if (state.phase === "prep") return "prep";
  if (state.phase === "present") return "present";
  // The gate, not `attention`, decides whether the form belongs on screen:
  // `turn.expect` keeps naming the slot being collected even while the coach
  // is mid-ask (owner:"agent"), so the form stays put for the whole setup
  // conversation instead of flickering out every time Podium speaks. It
  // disappears again once every slot is filled and the deck is generating.
  if (state.phase === "setup") {
    const collecting = ["topic", "level", "length"].includes(state.turn.expect);
    return collecting || state.attention === "waiting" ? "setup" : "none";
  }
  if (state.phase === "analyze") return "none";
  if (state.phase === "drill") return "drill";
  if (state.phase === "report") return "wrap";
  if (state.phase === "coach") {
    const stage = state.coach && state.coach.stage;
    if (stage === "item" || stage === "practice" || stage === "verdict") return "improvement";
    if (stage === "wrap") return "wrap";
    // summary / ask_proceed, or a stage left over from before the judgment arrived
    return state.judgment ? "scores" : "none";
  }
  return "none";
}
function slotSectionFor(view) {
  if (view === "setup") return "setup";
  if (view === "prep") return "prep";
  if (view === "countdown") return "countdown";
  if (view === "present") return "present";
  if (view === "scores" || view === "improvement" || view === "drill" || view === "wrap") return "dashboard";
  return "none";
}

// ------------------------------------------------------------------ render
function render() {
  const view = deriveView();
  const layout = view === "none" ? "center" : "top";
  state.view = view;
  state.layout = layout;

  renderStatusbar();
  updateUnmutePill();
  renderCaption();
  renderChoices();
  renderPrepView();
  renderCountdownView();
  renderPresentView();
  renderSetupState();
  renderSetupHint();
  applyGate();

  applyHeroLayout(layout);
  transitionSlot(slotSectionFor(view));

  if (dash) dash.update(state);
}

// ------------------------------------------------------------- input gate
// Single source of truth for which controls are inert while owner:"agent"
// (CONTRACTS.md §5 Input gate v4). Runs after renderChoices() so freshly
// rebuilt choice buttons are covered by the same pass.
function applyGate() {
  const gated = state.turn.owner !== "user";
  el.stage.dataset.turn = state.turn.owner;
  if (el.setupSubmit) el.setupSubmit.disabled = gated;
  if (el.setupUploadBtn) el.setupUploadBtn.disabled = gated;
  updatePresentSteppers();
  if (el.presentDone) el.presentDone.disabled = gated;
  el.choices.querySelectorAll(".choice").forEach((btn) => {
    btn.disabled = gated;
    btn.setAttribute("aria-disabled", String(gated));
  });
}

// Two independent conditions disable the slide steppers -- the turn gate and
// the ends of the deck -- and both `applyGate` and `renderPresentView` can fire
// on their own, so they are resolved in one place instead of overwriting each
// other's `disabled` flag.
function updatePresentSteppers() {
  const gated = state.turn.owner !== "user";
  const total = ((state.deck && state.deck.slides) || []).length;
  if (el.presentPrev) el.presentPrev.disabled = gated || state.currentSlide <= 1;
  if (el.presentNext) el.presentNext.disabled = gated || (total > 0 && state.currentSlide >= total);
}

function micStatusText() {
  if (!state.micEnabled) return "mic off";
  if (state.floorLatched) return "mic latched";
  if (state.floor === "user") return "mic live \u00b7 hold space";
  return "mic muted \u00b7 hold space to talk";
}

function renderStatusbar() {
  el.sbConn.textContent = state.connected ? (state.reconnecting ? "reconnecting" : "connected") : "disconnected";
  el.sbAgent.textContent = state.agentPresent ? "agent present" : "agent absent";
  el.sbProvider.textContent = state.provider ? `${state.provider.name} \u00b7 ${state.provider.model} \u00b7 ${state.provider.speaker}` : "";
  el.sbAttention.textContent = state.agentSpeaking ? "speaking" : state.attention;
  el.sbPhase.textContent = state.phase;
  el.sbStage.textContent = (state.coach && state.coach.stage) || "";
  el.sbMic.textContent = micStatusText();
  el.sbRoom.textContent = ROOM_NAME;
}

// ------------------------------------------------------------------ caption / hero
let lastCaptionSegId = null;
let captionShown = ""; // text currently laid out as .word spans
function renderCaption() {
  if (state.everConnected && !state.connected) {
    el.caption.textContent = "Connection lost. Tap to reconnect.";
    el.caption.classList.remove("partial");
    el.userLine.classList.add("hidden");
    captionShown = "";
    return;
  }
  const agent = state.caption;
  if (!agent) {
    el.caption.textContent = "";
    lastCaptionSegId = null;
    captionShown = "";
  } else {
    const text = stripPauseMarkup(agent.text);
    el.caption.classList.toggle("partial", !agent.final);
    const sameSegment = agent.segId === lastCaptionSegId;
    lastCaptionSegId = agent.segId;
    if (sameSegment && text.startsWith(captionShown)) {
      revealCaption(text.slice(captionShown.length));
    } else {
      el.caption.textContent = "";
      captionShown = "";
      revealCaption(text);
    }
    captionShown = text;
  }
  const user = state.userLine;
  el.userLine.classList.toggle("hidden", !user);
  if (user) {
    el.userLine.textContent = user.text;
    el.userLine.classList.toggle("partial", !user.final);
  }
}
// Appends `text` to the caption as word spans and fades them in; called with
// only the newly streamed tail so words already on screen never re-animate.
function revealCaption(text) {
  const words = text.split(/(\s+)/).filter((w) => w !== "");
  const spans = [];
  for (const w of words) {
    if (/^\s+$/.test(w)) {
      el.caption.appendChild(document.createTextNode(" "));
      continue;
    }
    const span = document.createElement("span");
    span.className = "word";
    span.textContent = w;
    el.caption.appendChild(span);
    spans.push(span);
  }
  if (!spans.length) return;
  gsap.fromTo(spans, { autoAlpha: 0, y: 8 }, { autoAlpha: 1, y: 0, duration: dur(0.32), ease: "power2.out", stagger: dur(0.028) });
}

let prevLayout = "center";
function applyHeroLayout(layout) {
  const hasCaption = !!state.caption || (state.everConnected && !state.connected);
  const wantsRow = layout === "top" || hasCaption;
  const isRow = el.hero.classList.contains("has-caption");
  const layoutChanged = layout !== prevLayout;
  const rowChanged = wantsRow !== isRow;
  if (!layoutChanged && !rowChanged) return;

  const firstOrb = el.orbWrap.getBoundingClientRect();
  const firstCap = el.captionCol.getBoundingClientRect();
  el.stage.dataset.layout = layout;
  el.hero.classList.toggle("has-caption", wantsRow);
  // force reflow so the "last" rects reflect the new layout
  void el.hero.offsetWidth;
  const lastOrb = el.orbWrap.getBoundingClientRect();
  const lastCap = el.captionCol.getBoundingClientRect();

  const d = dur(0.9);
  const tl = gsap.timeline();
  tl.fromTo(el.orbWrap, {
    x: firstOrb.left - lastOrb.left,
    y: firstOrb.top - lastOrb.top,
    scale: rowChanged && wantsRow ? 0.92 : 1,
  }, { x: 0, y: 0, scale: 1, duration: d, ease: "expo.out" }, 0);
  if (lastCap.width) {
    tl.fromTo(el.captionCol, {
      x: firstCap.width ? firstCap.left - lastCap.left : 20,
      autoAlpha: firstCap.width ? 1 : 0,
    }, { x: 0, autoAlpha: 1, duration: d, ease: "expo.out" }, 0.1);
  }
  prevLayout = layout;
}

// ------------------------------------------------------------------ slot transitions
const SECTION_EL = {
  setup: () => el.viewSetup,
  prep: () => el.viewPrep,
  countdown: () => el.viewCountdown,
  present: () => el.viewPresent,
  dashboard: () => el.viewDashboard,
};
let activeSection = "none";
function transitionSlot(nextSection) {
  if (nextSection === activeSection) return;
  const outEl = SECTION_EL[activeSection] && SECTION_EL[activeSection]();
  const inEl = SECTION_EL[nextSection] && SECTION_EL[nextSection]();
  // A control that keeps focus inside a hidden view would swallow the
  // Enter/Space shortcuts of the next one.
  if (outEl && document.activeElement && outEl.contains(document.activeElement)) document.activeElement.blur();
  const d = dur(0.5);
  const tl = gsap.timeline({
    onComplete: () => { if (outEl && outEl !== inEl) outEl.classList.add("view-hidden"); },
  });
  if (outEl) {
    tl.to(outEl, { autoAlpha: 0, y: -16, duration: d, ease: "power2.in" }, 0);
  }
  if (inEl) {
    inEl.classList.remove("view-hidden");
    gsap.set(inEl, { autoAlpha: 0, y: 24 });
    tl.to(inEl, { autoAlpha: 1, y: 0, duration: d, ease: "power2.out" }, outEl ? "-=0.15" : 0);
    const kids = Array.from(inEl.children);
    if (kids.length) {
      tl.fromTo(kids, { autoAlpha: 0, y: 16 }, { autoAlpha: 1, y: 0, duration: d, ease: "power2.out", stagger: dur(0.06) }, "<");
    }
  }
  activeSection = nextSection;
}

// -------------------------------------------------------------- choices strip
// The agent sends every option it accepts by voice; a minimal UI shows only
// each stage's primary actions (CONTRACTS.md §5 "options may exceed what a
// minimal UI should show") -- everything else stays voice-only.
const CHOICE_PRIORITY = {
  item: ["practice", "again", "next"],
  practice: ["again", "skip", "next"],
  verdict: ["next", "practice", "finish"],
  ask_proceed: ["proceed", "later"],
  wrap: ["more", "rerecord", "new_talk"],
};
function primaryChoices(stage, options) {
  const order = CHOICE_PRIORITY[stage];
  if (!order) return options.slice(0, 3);
  const byName = new Map(options.map((o) => [o.name, o]));
  const picked = [];
  for (const name of order) {
    if (byName.has(name)) picked.push(byName.get(name));
    if (picked.length === 3) break;
  }
  return picked;
}
function renderChoices() {
  const stage = state.coach && state.coach.stage;
  const options = primaryChoices(stage, (state.coach && state.coach.options) || []);
  el.choices.classList.toggle("hidden", options.length === 0);
  el.choices.innerHTML = "";
  if (!options.length) return;
  const prefix = document.createElement("span");
  prefix.className = "choices-prefix";
  prefix.textContent = "say";
  el.choices.appendChild(prefix);
  options.forEach((opt, i) => {
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "choices-sep";
      sep.textContent = "\u00b7";
      el.choices.appendChild(sep);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "choice";
    btn.textContent = `\u201c${opt.label}\u201d`;
    btn.addEventListener("click", () => {
      if (opt.name === "rerecord") sendUserAction({ type: "rerecord", slide: null });
      else sendUserAction({ type: "command", name: opt.name });
    });
    el.choices.appendChild(btn);
  });
}

// -------------------------------------------------------------- Setup view

function renderSetupState() {
  const missing = new Set((state.setup && state.setup.missing) || []);
  if (el.setupTopic) el.setupTopic.classList.toggle("is-missing", missing.has("topic"));
  const levelLabel = el.setupLevel && el.setupLevel.closest("label");
  if (levelLabel) levelLabel.classList.toggle("is-missing", missing.has("level"));
  const lengthLabel = el.setupLength && el.setupLength.closest("label");
  if (lengthLabel) lengthLabel.classList.toggle("is-missing", missing.has("budget_s"));
}
// The one line the setup view shows for "what do I say now", driven live by
// the turn gate so it always agrees with which controls are actually
// enabled -- fixes the static "say ready or press Enter"-style copy that
// never changed even after a spoken topic had already autofilled the form.
function renderSetupHint() {
  if (!el.setupHint) return;
  const turn = state.turn;
  // While the coach holds the floor the invitation would be a lie: the
  // controls it names are disabled. The agent's own `hint` is kept for the
  // moment the floor comes back.
  if (turn.owner === "agent") {
    el.setupHint.textContent = state.attention === "thinking"
      ? "Building your slides\u2026"
      : "Podium is speaking\u2026";
    return;
  }
  let text = turn.hint;
  if (!text && turn.expect === "topic") {
    text = "Your turn \u2014 say a topic to generate slides, or upload your own deck.";
  } else if (!text && turn.expect === "level") {
    text = "Your turn \u2014 what level are you presenting at?";
  } else if (!text && turn.expect === "length") {
    text = "Your turn \u2014 how long should the talk be?";
  }
  el.setupHint.textContent = text ? "Your turn \u2014 " + text.replace(/^your turn\s*[\u2014-]\s*/i, "") : "";
}
async function handlePdfUpload(file) {
  if (state.turn.owner !== "user" || !state.turn.allow.includes("deck_upload")) {
    showToast("Podium is still talking \u2014 one sec.");
    return;
  }
  el.setupStatus.textContent = "Parsing\u2026";
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
    if (sendUserAction({ type: "deck_upload", slides, client_id: state.clientId })) {
      el.setupStatus.textContent = `Sent ${slides.length} slide(s) parsed from PDF.`;
    }
  } catch (err) {
    el.setupStatus.textContent = `PDF upload failed: ${err.message || err} \u2014 typing a topic above still works.`;
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

// -------------------------------------------------------------- Prep view
function renderPrepView() {
  if (state.phase !== "prep") return;
  el.prepNumber.textContent = state.remainingS != null ? String(Math.max(0, Math.ceil(state.remainingS))) : "\u2014";
}

// -------------------------------------------------------------- Present view
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
function renderPresentView() {
  if (state.phase !== "present") return;
  const slide = currentSlideObj();
  el.presentTitle.textContent = slide ? slide.title : "\u2014";
  el.presentBullets.innerHTML = "";
  ((slide && slide.bullets) || []).forEach((b) => {
    const li = document.createElement("li");
    li.textContent = b;
    el.presentBullets.appendChild(li);
  });
  const slides = (state.deck && state.deck.slides) || [];
  el.presentCounter.textContent = slides.length ? `${state.currentSlide} / ${slides.length}` : "";
  // The steppers mirror the agent's own bounds check, so the presenter can see
  // where the deck ends instead of pressing a control that does nothing.
  updatePresentSteppers();
  if (state.presentStartedAt == null) {
    el.presentClock.textContent = state.remainingS != null ? formatClock(state.remainingS) : "\u2014";
    el.presentClock.classList.remove("warn");
  } else {
    const elapsed = (Date.now() - state.presentStartedAt) / 1000;
    const remaining = state.budgetS - elapsed;
    if (remaining >= 0) {
      el.presentClock.textContent = formatClock(remaining);
      el.presentClock.classList.remove("warn");
    } else {
      el.presentClock.textContent = "+" + formatClock(-remaining);
      el.presentClock.classList.add("warn");
    }
  }
}

// -------------------------------------------------------------- wiring
let lastSpaceDownAt = 0;
function wireEvents() {
  el.setupForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const topic = el.setupTopic.value.trim();
    if (!topic) {
      el.setupTopic.focus();
      el.setupStatus.textContent = "Say or type a topic first.";
      return;
    }
    const ok = sendUserAction({
      type: "setup",
      topic,
      level: el.setupLevel.value,
      budget_s: Number(el.setupLength.value),
      client_id: state.clientId,
    });
    if (ok) {
      el.setupStatus.textContent = "Sent. Preparing your reference slides\u2026";
      el.setupTopic.blur();
    }
  });
  el.setupUploadBtn.addEventListener("click", () => el.setupUploadInput.click());
  el.setupUploadInput.addEventListener("change", () => {
    const file = el.setupUploadInput.files[0];
    if (file) handlePdfUpload(file);
  });
  window.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file && file.type === "application/pdf") handlePdfUpload(file);
  });
  el.presentPrev.addEventListener("click", () => {
    el.presentPrev.blur();
    sendUserAction({ type: "slide_prev" });
  });
  el.presentNext.addEventListener("click", () => {
    el.presentNext.blur();
    sendUserAction({ type: "slide_next" });
  });
  el.presentDone.addEventListener("click", () => {
    el.presentDone.blur();
    sendUserAction({ type: "present_end" });
  });
  el.countdownRetry.addEventListener("click", () => {
    const cur = state.countdown;
    if (!cur || cur.uiStatus !== "error") return;
    if (cur.errorKind === "autoplay") retryAutoplay(cur);
    else requestBegin();
  });
  el.unmute.addEventListener("click", async () => {
    try {
      await state.room.startAudio();
    } catch (err) {
      // Leave the pill up; the user can try again.
    }
    updateUnmutePill();
  });
  window.addEventListener("pointerdown", globalPointerHandler);
  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    attemptWake();
    if (e.code === "Escape") {
      if (state.floorLatched) setFloor(false);
      return;
    }
    const tag = document.activeElement && document.activeElement.tagName;
    // A focused button already fires its own click on Enter/Space; letting
    // this handler also act would double-send (e.g. clicking #present-next
    // then hitting Enter on it should not also fire slide_next).
    if (tag === "BUTTON" || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (state.phase === "prep" && e.code === "Enter") {
      e.preventDefault();
      requestBegin();
      return;
    }
    if (state.phase === "present") {
      if (e.code === "ArrowRight") {
        e.preventDefault();
        sendUserAction({ type: "slide_next" });
      } else if (e.code === "ArrowLeft") {
        e.preventDefault();
        sendUserAction({ type: "slide_prev" });
      } else if (e.code === "Enter") {
        e.preventDefault();
        sendUserAction({ type: "present_end" });
      }
      return;
    }
    // Push-to-talk: hold Space to open the floor; a second press within
    // 350ms of the last one latches it open (CONTRACTS.md §5). Inert during
    // `present`, where the mic is already forced live for the whole take.
    if (e.code === "Space") {
      e.preventDefault();
      const now = performance.now();
      const isDoubleTap = now - lastSpaceDownAt < 350;
      lastSpaceDownAt = now;
      // Latched and tapped once: the natural read is "stop talking now".
      if (state.floorLatched && !isDoubleTap) {
        setFloor(false);
        return;
      }
      if (isDoubleTap) state.floorLatched = !state.floorLatched;
      setFloor(true, { latched: state.floorLatched });
    }
  });
  window.addEventListener("keyup", (e) => {
    if (e.code !== "Space") return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === "BUTTON" || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (state.phase === "present") return;
    if (!state.floorLatched) setFloor(false);
  });
}

function init() {
  cacheEls();
  setupMatchMedia();

  const lenis = new Lenis({ autoRaf: false });
  gsap.ticker.add((time) => lenis.raf(time * 1000));

  state.orb = createOrb(el.orb);
  dash = createDashboard(el.viewDashboard, { send: sendMessage, gsap });

  wireEvents();
  render();
  connect();
  requestAnimationFrame(levelLoop);

  // Test/debug hook: lets an external driver call the router directly.
  window.podiumDebug = { state, handleMessage, render, levels: smoothedLevels };
}
init();

"""Podium's voice session: setup -> prep -> present -> analyze -> coach -> drill -> report.

Phase transitions, the presentation-mode turn policy, the revision fence, and the
coach's contrastive playback queue all live here. See CONTRACTS.md for the frozen
shapes this module speaks on the wire and writes to disk.

The coaching half (setup through report) is a voice-first conversation: Thunder (the
capitalised Rime speaker name) greets, acknowledges, summarises, and judges practice
attempts by *speaking*, not by stepping through a silent queue. Every number he
says comes from `analysis/metrics.py`, the judge LLM's strict-JSON judgment, or
`analysis/practice.py` -- the coach LLM only phrases numbers it is handed, via
`_speak_llm`, and every phrasing call has a scripted, non-LLM fallback so a model
hiccup never leaves the user without a reply.

v2 adds: the user's own recording played back as the item's first beat (V0, cut by
`analysis/slice.py`), real per-word pronunciation confidence from a Deepgram REST
re-transcription (`analysis/pronunciation.py`), a temporal `SessionGraph` (§8) that
both drives the coach LLM's steering context and is persisted for the UI, and a
skill-word drill driven by the judge's `drill_words` instead of raw deck terms.

`build_agent_and_session()` is the seam that makes this testable without a live
LiveKit room: it returns a real `AgentSession` + `PodiumOrchestrator` pair. Pass
`ctx=None` and the orchestrator never touches `ctx.room` -- a caller can instead set
`session.input.audio` to a custom `io.AudioInput` (see `livekit.agents.voice.io`)
and drive phases directly via `orchestrator.on_client({...})`, exercising the real
manual-turn-detection, fencing, and timeline-logging code with no network involved.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    RunContext,
    StopResponse,
    TurnHandlingOptions,
    function_tool,
)
from livekit.agents.llm import ChatContext
from livekit.agents.utils.audio import audio_frames_from_file
from livekit.plugins import deepgram, rime, silero

import agent.graph as graph_mod
import analysis.deck as deck_mod
import analysis.judge as judge_mod
import analysis.metrics as metrics_mod
import analysis.practice as practice_mod
import analysis.pronunciation as pronunciation_mod
import analysis.render as render_mod
import analysis.slice as slice_mod
from agent.llm_config import COACH_MODEL, JUDGE_MODEL, coach_llm
from agent.store import Store, WavWriter

load_dotenv()
log = logging.getLogger("podium.session_agent")

RIME_MODEL = os.environ.get("RIME_MODEL", "mistv3")
RIME_SPEAKER = os.environ.get("RIME_SPEAKER", "thunder")
RIME_LANG = "eng"
SAMPLE_RATE = 24000
PREP_DURATION_S = 60  # PLAN.md §4.2: "countdown (default 60 s)"; not in the frozen
                       # client->agent "setup" shape, so it isn't client-configurable.

# The persona's spoken name -- the Rime speaker name, capitalised.
AGENT_NAME = RIME_SPEAKER.capitalize()

# Below this, treat the recording as "nothing heard" rather than a bad talk.
MIN_WORDS_TO_JUDGE = 12

# How long an item's practice capture is allowed to sit idle before a "still
# there?" nudge, and how many nudges before giving up and moving on.
PRACTICE_WAIT_S = 25.0
ASK_PROCEED_WAIT_S = 20.0
MAX_NUDGES_PER_WAIT = 2
# How long start() waits for the client's start-button gesture before greeting
# anyway (a headless harness never sends client_ready).
CLIENT_READY_FALLBACK_S = 120.0

# v2: how many low-confidence words the drill works through, and how long it
# waits for a spoken answer before scoring "not recognised".
DRILL_MAX_WORDS = 3
DRILL_ANSWER_TIMEOUT_S = 8.0
# How long an unrecognised barge-in mid-trio waits for the default LLM reply to
# finish speaking before giving up and resuming anyway (see _resolve_interruption).
RESUME_REPLY_TIMEOUT_S = 12.0

_CURRICULUM_DIR = Path(__file__).resolve().parent.parent / "skills" / "curriculum"

COACH_INSTRUCTIONS = (
    f"You are {AGENT_NAME}, a warm, upbeat presentation coach who talks like a sharp friend "
    "in the user's corner, not a formal assistant -- energetic, encouraging, a coach who gets "
    "you fired up but stays precise about the numbers. Speak in at most two short sentences "
    "(about 30 words) unless you are asked to summarise, in which case a few sentences are "
    "fine. Never use markdown, bullet points, numbers-as-lists, or emojis -- everything you "
    "say is spoken aloud. Light, kind humour is welcome; never be sarcastic or dismissive "
    "about the user's ability. If someone asks about something unrelated to rehearsing their "
    "talk, answer in at most one playful sentence and then steer back to the current step. "
    "Call presentation_metrics for pace, filler, or pause questions, and explain_rubric when "
    "asked why something was flagged, or recap when asked what has been covered so far; never "
    "invent a number that did not come from a tool or the context you were given. Call "
    "generate_slides when the user names a presentation topic during setup, start_presenting "
    "when they say they are ready during prep, and choose to confirm one of the options "
    "currently on offer when they phrase it in their own words."
)

GREETING_TEXT = (
    f"Hi, I'm {AGENT_NAME}, your Podium coach. I'll train you to make your presentation "
    "better. To begin, type or tell me a topic and I'll build the slides, or upload your "
    "own deck."
)
COUNTDOWN_SPEECH = "Three. <700> Two. <700> One. <500> Go!"

_PRESENT_END_RE = re.compile(r"\b(i'?m\s+done|that'?s\s+it|i'?m\s+finished)\b", re.I)
_NEXT_SLIDE_RE = re.compile(r"\bnext\s+slide\b", re.I)
_PAUSE_MARKUP_RE = re.compile(r"<\d+>")
_READY_RE = re.compile(r"\b(ready|let'?s go|begin|start|go ahead|i'?m good)\b", re.I)

# CONTRACTS.md §5 option names -> a short voice/regex match. Only names currently
# offered (`PodiumOrchestrator._offered`) are ever matched against these.
_INTENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "proceed": re.compile(r"\b(yes|yeah|yep|sure|ok(?:ay)?|let'?s (?:do it|go)|go ahead)\b", re.I),
    "later": re.compile(r"\b(no|not now|later|maybe later|skip (?:them|this|it)|straight to the report)\b", re.I),
    "original": re.compile(r"\b(original|what i said|my version)\b", re.I),
    "cleaner": re.compile(r"\b(clean(?:er)?|fixed|better version)\b", re.I),
    "pauses": re.compile(r"\b(pause[sd]?|paced|with pauses)\b", re.I),
    "alternative": re.compile(r"\b(alternative|another way|different way|other way)\b", re.I),
    "again": re.compile(r"\b(again|repeat|once more|one more time|play (?:it|that) again)\b", re.I),
    "slower": re.compile(r"\b(slower|slow down)\b", re.I),
    "why": re.compile(r"\b(why|reason|explain)\b", re.I),
    "practice": re.compile(r"\b(my turn|let me try|i'?ll try|i will try|ready to try)\b", re.I),
    "next": re.compile(r"\b(next|move on|next one|got it,? next)\b", re.I),
    "skip": re.compile(r"\bskip\b", re.I),
    "finish": re.compile(r"\b(finish|that'?s enough|wrap(?: it)? up|stop coaching|i'?m done coaching)\b", re.I),
}

# Short, plain-language phrasing of each option name, used in nudges and in the
# per-turn steering context handed to the coach LLM.
_OPTION_LABELS: dict[str, str] = {
    "proceed": "say let's do it", "later": "say maybe later",
    "original": "hear what you said", "cleaner": "hear the cleaner version",
    "pauses": "hear it with pauses", "alternative": "hear another way to open it",
    "again": "hear it again", "slower": "hear it slower", "why": "ask why",
    "practice": "say it yourself", "next": "move to the next one",
    "skip": "skip this one", "finish": "finish up",
}

_NUDGE_FALLBACK: dict[str, str] = {
    "setup": "Still there? Tell me a topic whenever you're ready, or upload your own deck.",
    "prep": "Still with me? No rush -- say ready when you are, or just start talking about your topic.",
    "coach": "Still with me? No rush -- pick one whenever you're ready.",
    "drill": "Still there? Just say the word whenever you're ready.",
}

_ITEM_OPTIONS = [
    {"name": "original", "label": "What I said"},
    {"name": "cleaner", "label": "Cleaner"},
    {"name": "pauses", "label": "With pauses"},
    {"name": "alternative", "label": "Another opening"},
    {"name": "again", "label": "Play all again"},
    {"name": "slower", "label": "Slower"},
    {"name": "why", "label": "Why?"},
    {"name": "practice", "label": "My turn"},
    {"name": "next", "label": "Next"},
    {"name": "skip", "label": "Skip"},
    {"name": "finish", "label": "Finish"},
]

_PRACTICE_PROMPTS = (
    "Your turn -- say it your way, in one breath.",
    "Now you: same point, your words.",
    "Last one -- give it a try in your own words.",
)

# Not a real option: signals the practice loop that `_handle_attempt` already
# stopped capture, spoke the verdict, and just wants a fresh capture started.
_ATTEMPT_HANDLED = "_attempt_handled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_pause_markup(text: str) -> str:
    return _PAUSE_MARKUP_RE.sub(" ", text).strip()


def _match_intent(text: str, offered: list[str]) -> str | None:
    """Deterministic-first voice command match: only names currently offered can
    match, word-boundary, case-insensitive, longest match wins. A long utterance
    (more than ~8 words) is treated as a genuine remark for the LLM, never a
    command, even if it happens to contain a command word somewhere in it."""
    if len(text.split()) > 8:
        return None
    best_name: str | None = None
    best_len = -1
    for name in offered:
        pattern = _INTENT_PATTERNS.get(name)
        if pattern is None:
            continue
        m = pattern.search(text)
        if m and (m.end() - m.start()) > best_len:
            best_name, best_len = name, m.end() - m.start()
    return best_name


def _levenshtein_le1(a: str, b: str) -> bool:
    """True iff the edit distance between `a` and `b` (case-insensitive) is <= 1."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return abs(la - lb) <= 1
    if abs(la - lb) > 1:
        return False
    i = j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if la == lb:
            i += 1
            j += 1
        elif la > lb:
            i += 1
        else:
            j += 1
    edits += (la - i) + (lb - j)
    return edits <= 1


def _close_enough(heard: str, term: str) -> bool:
    """Used by the drill: does the recognised text plausibly contain `term`,
    allowing one edit (this checks intelligibility to a recogniser, not
    phonetic correctness -- the spoken framing says so explicitly)."""
    if _levenshtein_le1(heard.strip(), term):
        return True
    tokens = re.findall(r"[A-Za-z']+", heard)
    return any(_levenshtein_le1(tok, term) for tok in tokens)


def _trim_wav_to_words(path: Path, words: list[dict[str, Any]]) -> None:
    """In-place-rewrite `path` to `[first_word.start-0.3, last_word.end+0.3]`,
    24 kHz mono int16. No-op if the window would be degenerate."""
    with wave.open(str(path), "rb") as r:
        n_frames = r.getnframes()
        sr = r.getframerate()
        raw = r.readframes(n_frames)
    samples = np.frombuffer(raw, dtype=np.int16)
    if sr <= 0 or len(samples) == 0:
        return
    start_s = max(0.0, words[0]["start"] - 0.3)
    end_s = min(len(samples) / sr, words[-1]["end"] + 0.3)
    i0 = int(start_s * sr)
    i1 = max(i0 + 1, int(end_s * sr))
    trimmed = samples[i0:i1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(trimmed.tobytes())


def _curriculum_section(skill_id: str, heading: str) -> str | None:
    """CONTRACTS.md §9: pull the plain text under a `## <heading>` from
    `skills/curriculum/<skill_id>.md`. None if the file or heading is missing --
    callers fall back to the judge rubric text so a coaching session never
    blocks on the curriculum files landing."""
    path = _CURRICULUM_DIR / f"{skill_id}.md"
    if not path.exists():
        return None
    capture = False
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if capture:
                break
            capture = line[3:].strip().lower() == heading.lower()
            continue
        if capture and line.strip():
            out.append(line.strip())
    return " ".join(out) if out else None


def _skill_title(skill_id: str | None) -> str:
    """CONTRACTS.md §9: the `# <Title>` heading of a curriculum file, or a
    readable fallback derived from the id when the file doesn't exist yet."""
    if not skill_id:
        return "delivery"
    path = _CURRICULUM_DIR / f"{skill_id}.md"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return skill_id.replace("-", " ").title()


@dataclass
class ImprovementItem:
    data: dict[str, Any]
    revision: int
    heard: bool = False
    slower: bool = False
    clips: dict[str, dict[str, Any]] | None = None
    # v2: which stage of the intro/trio walkthrough comes next -- 0 = intro,
    # 1..3 = trio slots 0/1/2 (see PodiumOrchestrator._trio_slot), 4 = done.
    # Tracked on the item (not local state) so an interrupted item resumes at
    # exactly this stage, never restarting from the intro.
    stage_idx: int = 0
    # Which trio slots ("v0"/"v1", "v2", "v3") have played through uninterrupted
    # at least once. feedback_heard only fires once all three are present.
    completed: set[str] = field(default_factory=set)


class PodiumAgent(Agent):
    """Thin LLM-facing shell. Deterministic control flow lives on the orchestrator;
    this class only owns the persona, the function tools, and steering the default
    LLM turn with `route_utterance` / `phase_context` before it replies."""

    def __init__(self, orchestrator: "PodiumOrchestrator") -> None:
        super().__init__(instructions=COACH_INSTRUCTIONS)
        self._orch = orchestrator

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        text = (getattr(new_message, "text_content", None) or "").strip()
        orch = self._orch
        if text and orch.route_utterance(text):
            raise StopResponse()
        orch.note_unconsumed_utterance()
        if text:
            orch.record_utterance(text)
        # `turn_ctx` here is a temporary copy made fresh for this turn only (the
        # framework never merges edits back into Agent.chat_ctx), so this never
        # accumulates across turns -- no pruning needed.
        turn_ctx.add_message(role="system", content=orch.phase_context())

    @function_tool()
    async def generate_slides(self, context: RunContext, topic: str, level: str = "intermediate",
                               budget_s: int = 60) -> str:
        """Generate a slide deck for the user's presentation topic and move into
        the prep window once it is ready. Setup phase only.

        Args:
            topic: the presentation topic the user described.
            level: difficulty/level for the generated slides; "beginner",
                "intermediate", or "advanced".
            budget_s: how many seconds their talk should target; default 60.
        """
        orch = self._orch
        if orch.phase != "setup":
            return "We already have a deck ready -- no need to generate another one right now."
        asyncio.create_task(orch._handle_setup({"topic": topic, "level": level, "budget_s": budget_s}))
        return f"Generating slides on {topic}, give me ten seconds."

    @function_tool()
    async def start_presenting(self, context: RunContext) -> str:
        """Begin the countdown and presentation right away. Prep phase only."""
        orch = self._orch
        if orch.phase != "prep":
            return "We're not in the prep window right now."
        orch._start_present_flow()
        return "Starting the countdown now."

    @function_tool()
    async def choose(self, context: RunContext, option: str) -> str:
        """Confirm one of the options currently offered to the user, when they
        express it in natural language rather than an exact keyword.

        Args:
            option: one of the option names currently offered (e.g. "proceed",
                "next", "skip").
        """
        orch = self._orch
        option = option.strip().lower()
        if option not in orch._offered:
            choices = ", ".join(orch._offered) or "none right now"
            return f"That's not one of the choices right now. Options: {choices}."
        orch._deliver_intent(option, "llm")
        return "Got it."

    @function_tool()
    async def presentation_metrics(self, context: RunContext) -> dict[str, Any]:
        """Return the current revision's computed delivery metrics (pace, fillers,
        pauses, time budget) so you can answer a question like "how was my pacing"
        without waiting for the full judgment to finish.
        """
        return self._orch.current_metrics_summary()

    @function_tool()
    async def explain_rubric(self, context: RunContext, improvement_id: str) -> str:
        """Explain why a specific improvement was flagged, citing its rubric source.

        Args:
            improvement_id: id of the improvement to explain, e.g. "imp_1".
        """
        return self._orch.explain_rubric(improvement_id)

    @function_tool()
    async def recap(self, context: RunContext) -> str:
        """Summarise what has been covered in the session so far -- call this when
        the user asks what they've done, where they left off, or how it's going
        overall."""
        return self._orch.graph.recap_text()


class PodiumOrchestrator:
    """Owns phase state, the presentation-mode turn policy, the revision fence, and
    the data-channel protocol. One instance per live session."""

    def __init__(self, session: AgentSession, store: Store, *, ctx: agents.JobContext | None = None) -> None:
        self.session = session
        self.store = store
        self.ctx = ctx
        self.agent: PodiumAgent | None = None  # set by build_agent_and_session right after construction

        self.phase = "setup"
        self.deck: dict[str, Any] | None = None
        self.budget_s: int = 60
        self.current_slide: int = 1

        self.wav_writer: WavWriter | None = None
        self._word_stt = deepgram.STT(model="nova-3", language="en", filler_words=True,
                                       punctuate=True, sample_rate=SAMPLE_RATE)
        self._word_stream: Any = None
        self._word_stream_task: asyncio.Task | None = None

        self.active_revision: int | None = None
        self.words_buffer: list[dict[str, Any]] = []
        self.transcript_parts: list[str] = []
        self.slide_events: list[dict[str, Any]] = []
        self._present_started_mono: float = 0.0
        self._present_end_event: asyncio.Event | None = None
        self._present_timer_task: asyncio.Task | None = None
        self._pending_supersede: int | None = None

        self._prep_task: asyncio.Task | None = None
        self._flow_task: asyncio.Task | None = None
        self._post_analyze_task: asyncio.Task | None = None

        self.current_metrics: dict[str, Any] | None = None
        self.current_judgment: dict[str, Any] | None = None
        self.improvement_queue: list[ImprovementItem] = []
        self.drill_terms: list[str] = []
        self.drill_words: list[dict[str, Any]] = []
        self._verdicts: list[dict[str, Any]] = []
        self._pending_command: str | None = None
        self._drill_answer_future: asyncio.Future[str] | None = None
        self._last_user_speech_start_mono: float | None = None

        # -- v2: temporal graph + cross-session progress --------------------
        self.graph = graph_mod.SessionGraph(store.t0_mono)
        self.client_id: str | None = None
        self.progress: graph_mod.Progress | None = None
        self._utterance_n = 0

        # -- voice-first conversation state --------------------------------
        self._audio_subscribed = asyncio.Event()
        self._client_ready = asyncio.Event()
        self._offered: list[str] = []
        self._intent_future: asyncio.Future[str] | None = None
        self._expect: str | None = None  # "proceed" | "menu" | "drill" | None
        self._nudges_this_wait: int = 0
        self._current_item: ImprovementItem | None = None
        self._attempt_n: int = 0
        self._attempt_task: asyncio.Task | None = None

        # v2: true while an item's intro/trio is playing (as opposed to the
        # practice loop) -- gates the unrecognised-barge-in resume path.
        self._trio_active: bool = False
        # v2: armed by _resolve_interruption's unrecognised-utterance branch,
        # resolved by the speech_created listener with the default LLM reply's
        # own SpeechHandle so the trio can wait for it before resuming.
        self._await_default_reply_future: asyncio.Future[Any] | None = None
        # v2: True for the duration of our own explicit session.generate_reply()
        # calls (in _speak_llm) so the speech_created listener never mistakes
        # them for the framework's automatic post-turn reply.
        self._suppress_auto_reply_capture: bool = False

        # practice-attempt raw-audio capture -- separate from the
        # presentation-phase wav_writer/_word_stream above.
        self._capture_writer: WavWriter | None = None
        self._capture_stream: Any = None
        self._capture_task: asyncio.Task | None = None
        self._capture_words: list[dict[str, Any]] = []
        self._capture_active = False

    # -- wiring -----------------------------------------------------

    def wire_session_events(self) -> None:
        @self.session.on("user_input_transcribed")
        def _on_transcribed(ev: agents.UserInputTranscribedEvent) -> None:
            self.store.log("user_transcript", text=ev.transcript, final=ev.is_final)
            if not ev.is_final or self.phase != "present":
                return
            self.transcript_parts.append(ev.transcript)
            low = ev.transcript.lower()
            if _PRESENT_END_RE.search(low):
                self._trigger_present_end("voice")
            elif _NEXT_SLIDE_RE.search(low):
                self._advance_slide()

        @self.session.on("user_state_changed")
        def _on_user_state(ev: agents.UserStateChangedEvent) -> None:
            if ev.new_state == "speaking" and ev.old_state != "speaking":
                self.store.log("user_speech_start")
                self._last_user_speech_start_mono = time.monotonic()
            elif ev.old_state == "speaking" and ev.new_state != "speaking":
                self.store.log("user_speech_end")
            if (ev.new_state == "away" and self.phase in ("setup", "prep", "coach", "drill")
                    and self._is_awaiting_input() and self._nudges_this_wait < MAX_NUDGES_PER_WAIT):
                self._nudges_this_wait += 1
                asyncio.create_task(self._nudge(reason="idle"))

        @self.session.on("speech_created")
        def _on_speech_created(ev: Any) -> None:
            # Every generate_reply()-triggered speech (ours and the framework's
            # own automatic post-turn reply) surfaces here with source ==
            # "generate_reply"; _suppress_auto_reply_capture filters out our own
            # explicit _speak_llm calls so only the automatic reply is captured.
            if ev.source != "generate_reply" or self._suppress_auto_reply_capture:
                return
            fut = self._await_default_reply_future
            if fut is not None and not fut.done():
                self._await_default_reply_future = None
                fut.set_result(ev.speech_handle)

    def wire_room(self) -> None:
        """Only meaningful with a live room: raw-audio capture for the presentation
        WAV/word-timed transcript, practice-attempt capture, and the podium data
        channel. A fixture harness driving this orchestrator with `ctx=None` skips
        this entirely."""
        assert self.ctx is not None
        room = self.ctx.room

        @room.on("track_subscribed")
        def _on_track_subscribed(track: rtc.Track, *_: Any) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                self._audio_subscribed.set()
                asyncio.create_task(self._consume_raw_audio(track))

        @room.on("data_received")
        def _on_data(data: rtc.DataPacket) -> None:
            if data.topic != "podium":
                return
            try:
                msg = json.loads(bytes(data.data).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                log.warning("bad podium data packet")
                return
            self.on_client(msg)

    async def start(self) -> None:
        self.store.set_config(
            rime_model=RIME_MODEL, rime_speaker=RIME_SPEAKER, rime_lang=RIME_LANG,
            sample_rate=SAMPLE_RATE, transport="websocket /ws3", stt="deepgram/nova-3",
            coach_llm=COACH_MODEL, judge_llm=JUDGE_MODEL, provider="rime",
        )
        self.store.log("provider", name="rime", model=RIME_MODEL, speaker=RIME_SPEAKER)
        self.send({"type": "provider", "name": "rime", "model": RIME_MODEL, "speaker": RIME_SPEAKER})

        self._set_phase("setup")
        if self.ctx is not None:
            # Browsers block remote audio until the page has had a user gesture,
            # so the client sends client_ready from its start button; greeting
            # before that would be spoken into a muted tab. A headless harness
            # that never sends it still gets greeted after the fallback.
            try:
                await asyncio.wait_for(self._client_ready.wait(), timeout=CLIENT_READY_FALLBACK_S)
            except asyncio.TimeoutError:
                self.store.log("client_ready_timeout")
            try:
                await asyncio.wait_for(self._audio_subscribed.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
        if self.phase != "setup":
            return  # a client (or harness) already moved on without the greeting
        self._send_coach("greeting")
        self._say(GREETING_TEXT, kind="ack")

    # -- data channel -----------------------------------------------------

    def send(self, msg: dict[str, Any]) -> None:
        if self.ctx is None:
            log.debug("send (no room): %s", msg)
            return
        asyncio.create_task(self._publish(msg))

    async def _publish(self, msg: dict[str, Any]) -> None:
        assert self.ctx is not None
        try:
            await self.ctx.room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"), reliable=True, topic="podium"
            )
        except Exception as exc:
            # The presenter can close the tab mid-session; a dead engine must not
            # surface as an unhandled task exception on every queued message.
            log.debug("dropping %s message, room unavailable: %s", msg.get("type"), exc)

    def on_client(self, msg: dict[str, Any]) -> None:
        """The real dispatcher for every client->agent message (CONTRACTS §5). Plain,
        room-independent: callable directly by a fixture harness."""
        asyncio.create_task(self._dispatch_client(msg))

    async def _dispatch_client(self, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        try:
            if t == "setup" and self.phase == "setup":
                await self._handle_setup(msg)
            elif t == "deck_upload" and self.phase == "setup":
                await self._handle_deck_upload(msg)
            elif t == "ready" and self.phase == "prep":
                self._start_present_flow()
            elif t == "slide_next":
                self._advance_slide()
            elif t == "present_end" and self.phase == "present":
                self._trigger_present_end("client")
            elif t == "rerecord":
                await self._handle_rerecord(msg)
            elif t == "command":
                await self._handle_command(str(msg.get("name", "")))
            elif t == "client_ready":
                self._client_ready.set()
            else:
                log.info("ignoring client msg type=%s in phase=%s", t, self.phase)
        except Exception:
            log.exception("error handling client msg: %s", msg)
            self.send({"type": "error", "message": f"failed to handle {t}"})

    # -- speech helpers -----------------------------------------------------

    def _say(self, text: str, *, kind: str, allow_interruptions: bool = True,
              add_to_chat_ctx: bool = True, audio: Any = None) -> Any:
        kwargs: dict[str, Any] = {"allow_interruptions": allow_interruptions, "add_to_chat_ctx": add_to_chat_ctx}
        if audio is not None:
            kwargs["audio"] = audio
        handle = self.session.say(text, **kwargs)
        start_mono = time.monotonic()
        self.store.log("agent_speech_start", speech_id=handle.id, text=text[:200], kind=kind)
        asyncio.create_task(self._track_speech(handle, start_mono))
        return handle

    async def _say_and_wait(self, text: str, *, kind: str) -> Any:
        handle = self._say(text, kind=kind)
        await handle.wait_for_playout()
        return handle

    async def _track_speech(self, handle: Any, start_mono: float) -> None:
        await handle.wait_for_playout()
        played_s = round(time.monotonic() - start_mono, 3)
        self.store.log("agent_speech_end", speech_id=handle.id, interrupted=handle.interrupted, played_s=played_s)
        if handle.interrupted:
            onset = self._last_user_speech_start_mono
            if onset is not None and onset >= start_mono:
                latency_ms = max(0, round((time.monotonic() - onset) * 1000))
                self.store.log("interrupt", speech_id=handle.id, latency_ms=latency_ms)
            else:
                # Cut by a button/command or the flow itself: there is no voice
                # onset to measure from, so never report a number.
                self.store.log("interrupt", speech_id=handle.id, latency_ms=None, source="client")

    async def _speak_llm(self, instructions: str, fallback: str, *, kind: str) -> Any:
        """Let the coach LLM phrase `instructions` (which must carry every fact it
        is allowed to use). Falls back to a scripted `fallback` line, spoken via
        `_say`, if the LLM call errored, produced no assistant reply, or was cut
        off before it could finish.

        The request runs in a single-turn context (persona + request) rather than
        the live conversation: with the whole presentation transcript sitting in
        history as a user turn, the model answered *that* and ignored the facts."""
        rule = " Use only the numbers given here; do not invent any. Reply with the spoken line only."
        ctx = ChatContext.empty()
        ctx.add_message(role="system", content=COACH_INSTRUCTIONS)
        ctx.add_message(role="user", content=instructions + rule)
        self._suppress_auto_reply_capture = True
        try:
            handle = self.session.generate_reply(chat_ctx=ctx, tool_choice="none")
        finally:
            self._suppress_auto_reply_capture = False
        start_mono = time.monotonic()
        self.store.log("agent_speech_start", speech_id=handle.id, text=instructions[:200], kind=kind)
        await self._track_speech(handle, start_mono)
        spoken = " ".join(
            (getattr(item, "text_content", None) or "")
            for item in handle.chat_items if getattr(item, "role", None) == "assistant"
        ).strip()
        if spoken:
            self.store.log("agent_speech_text", speech_id=handle.id, text=spoken[:400])
        if handle.interrupted:
            return handle  # the user cut in deliberately; never re-speak it
        if handle.exception() is not None or not spoken:
            return await self._say_and_wait(fallback, kind=kind)
        return handle

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self._nudges_this_wait = 0
        self.store.log("phase", name=phase)
        self.send({"type": "phase", "name": phase, "budget_s": self.budget_s})
        self.store.set_graph(self.graph.to_json())

    def _send_coach(self, stage: str, *, options: list[dict[str, str]] | None = None, **extra: Any) -> None:
        options = options if options is not None else []
        self._offered = [o["name"] for o in options]
        log_fields = {k: v for k, v in extra.items() if k in ("improvement_id", "attempt")}
        self.store.log("coach_stage", stage=stage, **log_fields)
        self.send({"type": "coach", "stage": stage, "options": options, **extra})

    def _send_feedback_stage(self, improvement_id: str, stage: str, *, text: str | None = None) -> None:
        """v2: CONTRACTS §5 `feedback` message -- cues the client exactly which
        step of an item (intro/v0/v2/v3/alt/prompt/verdict/resume) is playing."""
        self.store.log("feedback_stage", improvement_id=improvement_id, stage=stage)
        item: dict[str, Any] = {"id": improvement_id, "stage": stage}
        if text is not None:
            item["text"] = text
        self.send({"type": "feedback", "item": item})

    def _send_drill(self, word: str, stage: str, *, conf_before: float | None = None,
                     conf_after: float | None = None, url: str | None = None) -> None:
        """v2: CONTRACTS §5 `drill` message."""
        self.store.log("drill", word=word, stage=stage, conf_before=conf_before, conf_after=conf_after)
        msg: dict[str, Any] = {"type": "drill", "word": word, "stage": stage}
        if conf_before is not None:
            msg["conf_before"] = conf_before
        if conf_after is not None:
            msg["conf_after"] = conf_after
        if url is not None:
            msg["url"] = url
        self.send(msg)

    # -- v2 graph helpers -----------------------------------------------------

    def record_utterance(self, text: str) -> None:
        """A user remark that reached the LLM (route_utterance did not consume
        it): recorded as an `utterance` node linked to whatever the coach is
        currently focused on, per CONTRACTS §8."""
        self._utterance_n += 1
        key = self.graph.add("utterance", str(self._utterance_n), text=text[:300])
        focus = self.graph.focus()
        if focus:
            self.graph.link(key, "about", focus)

    def _graph_add_improvement(self, item: ImprovementItem) -> None:
        imp = item.data
        key = self.graph.add("improvement", imp["id"], issue=imp.get("issue"), quote=imp.get("quote"),
                              skill=imp.get("skill"), slide=imp.get("slide"))
        if imp.get("slide") is not None:
            slide_key = self.graph.add("slide", imp["slide"])
            self.graph.link(key, "targets", slide_key)
        skill_id = imp.get("skill")
        if skill_id:
            skill_key = self.graph.add("skill", skill_id, title=_skill_title(skill_id))
            self.graph.link(key, "trains", skill_key)

    def _mark_played(self, improvement_id: str, variant: str) -> None:
        key = f"improvement:{improvement_id}"
        node = self.graph.nodes.get(key)
        played = list((node["props"].get("played") if node else None) or [])
        if variant not in played:
            played.append(variant)
        self.graph.mark(key, played=played)

    def _why_text(self, imp: dict[str, Any]) -> str:
        """CONTRACTS §9: the coach's "why" answer reads the curriculum's
        "Why it matters" section when the improvement's skill has one landed;
        falls back to the judge rubric text otherwise."""
        skill_id = imp.get("skill")
        if skill_id:
            section = _curriculum_section(skill_id, "Why it matters")
            if section:
                return f"{imp['issue']} {section}"[:400]
        return f"{imp['issue']} {judge_mod.rubric_text(imp['rubric_ref'])}"[:400]

    def _maybe_attach_progress(self, msg: dict[str, Any]) -> None:
        client_id = msg.get("client_id")
        if client_id and self.progress is None:
            self.client_id = str(client_id)
            self.progress = graph_mod.Progress(self.client_id)
            self.graph.attach_progress(self.progress)

    # -- intent routing -----------------------------------------------------

    def route_utterance(self, text: str) -> bool:
        """Deterministic-first: True means the utterance was consumed and the
        caller must raise StopResponse (no default LLM reply)."""
        if self.phase == "drill":
            self.deliver_drill_answer(text)
            return True
        if self.phase == "prep" and len(text.split()) <= 8 and _READY_RE.search(text):
            self._start_present_flow()
            return True
        if self._expect in ("proceed", "menu"):
            name = _match_intent(text, self._offered)
            if name is not None:
                self._deliver_intent(name, "regex")
                return True
            if (self._expect == "menu" and self._capture_active and self._current_item is not None
                    and practice_mod.looks_like_attempt(text, self._current_item.data)):
                self._attempt_task = asyncio.create_task(self._handle_attempt(text))
                return True
        return False

    def note_unconsumed_utterance(self) -> None:
        """The LLM is about to answer a remark made during a practice wait: the
        remark's audio is already in the capture, so discard it and start clean."""
        if self._expect == "menu" and self._capture_active and self._current_item is not None:
            asyncio.create_task(self._restart_capture())

    async def _restart_capture(self) -> None:
        item, n = self._current_item, self._attempt_n
        await self._stop_capture()
        if item is not None and self._current_item is item and self._expect == "menu":
            await self._start_capture(item, n)

    def _deliver_intent(self, name: str, source: str) -> None:
        self.store.log("voice_intent", name=name, source=source)
        if name not in self._offered:
            return
        if self._intent_future is not None and not self._intent_future.done():
            self._intent_future.set_result(name)
            # A button press while Thunder is still talking (verdict, prompt,
            # question) should cut him off; a voice intent already did via VAD.
            self.session.interrupt()
            return
        # Thunder is mid-speech (announcing an item / playing the trio) -- mirror
        # the old trigger_voice_command barge-in path.
        self._pending_command = name
        self.session.interrupt()

    async def _await_intent(self, timeout_s: float) -> str | None:
        """Wait for the next resolved intent, nudging up to MAX_NUDGES_PER_WAIT
        times on silence before giving up (caller applies its own default)."""
        self._nudges_this_wait = 0
        while True:
            # A command that arrived while Thunder was mid-speech parked itself
            # in _pending_command (see _deliver_intent); honour it before waiting.
            parked, self._pending_command = self._pending_command, None
            if parked in self._offered:
                return parked
            self._intent_future = asyncio.get_event_loop().create_future()
            try:
                return await asyncio.wait_for(self._intent_future, timeout=timeout_s)
            except asyncio.TimeoutError:
                if self._nudges_this_wait >= MAX_NUDGES_PER_WAIT:
                    return None
                self._nudges_this_wait += 1
                await self._nudge(reason="timeout")
            finally:
                self._intent_future = None

    def _is_awaiting_input(self) -> bool:
        if self.phase in ("setup", "prep"):
            return True
        if self.phase == "coach":
            return self._intent_future is not None
        if self.phase == "drill":
            return self._drill_answer_future is not None
        return False

    async def _nudge(self, *, reason: str) -> None:
        stage = self.phase
        self.store.log("nudge", reason=reason, stage=stage)
        if self._offered:
            hint = " or ".join(_OPTION_LABELS.get(n, n) for n in self._offered)
        elif stage == "setup":
            hint = "tell me a topic, or upload your own deck"
        elif stage == "prep":
            hint = "say ready when you want to begin"
        else:
            hint = "just tell me what's on your mind"
        instructions = (
            f"The user has gone quiet during {stage}. In one short, warm, lightly funny line, "
            f"check in on them and remind them they can {hint}. Keep it under twenty words."
        )
        fallback = _NUDGE_FALLBACK.get(stage, "Still with me? Take your time.")
        handle = await self._speak_llm(instructions, fallback, kind="ack")
        self.graph.add("coach_line", handle.id, line_kind="nudge", reason=reason, stage=stage)

    def phase_context(self) -> str:
        """A short paragraph handed to the coach LLM as steering for the current
        turn: phase, deck, what the user can do right now, the active
        improvement's facts, the metrics summary when available, and the v2
        SessionGraph's own steering text (focus, plays/heard, attempts,
        recent remarks, remaining items, skills touched)."""
        parts = [f"Current phase: {self.phase}."]
        if self.deck:
            titles = ", ".join(s.get("title", "") for s in self.deck.get("slides", []))
            parts.append(f"Topic: {self.deck.get('topic')}. Slides: {titles}.")
        if self._offered:
            labels = ", ".join(f"{n} ({_OPTION_LABELS.get(n, n)})" for n in self._offered)
            parts.append(f"Right now the user can: {labels}.")
        elif self.phase == "setup":
            parts.append("The user can tell you a topic to generate slides for, or upload their own deck.")
        elif self.phase == "prep":
            parts.append("The user can say ready to begin, or keep preparing.")
        item = self._current_item
        if item is not None:
            d = item.data
            parts.append(
                f"Current improvement: issue={d.get('issue')!r} quote={d.get('quote')!r} "
                f"cleaner={d.get('v2_text')!r} alternative={d.get('alternative')!r}."
            )
        if self.current_metrics:
            parts.append(f"Metrics summary: {self.current_metrics_summary()}.")
        return " ".join(parts) + " " + self.graph.context_text()

    # -- setup -----------------------------------------------------

    async def _handle_setup(self, msg: dict[str, Any]) -> None:
        self._maybe_attach_progress(msg)
        topic = str(msg["topic"])
        level = str(msg.get("level", "intermediate"))
        budget_s = int(msg["budget_s"])
        deck = await deck_mod.generate_deck(topic=topic, level=level, budget_s=budget_s)
        await self._apply_deck(deck)

    async def _handle_deck_upload(self, msg: dict[str, Any]) -> None:
        self._maybe_attach_progress(msg)
        # CONTRACTS §5's deck_upload carries only `slides` -- no budget_s/topic/level.
        # Interpreted here: accept optional overrides if a client sends them, else fall
        # back to a sensible default budget and a topic derived from the first slide.
        slides = msg["slides"]
        budget_s = int(msg.get("budget_s", 90))
        deck = deck_mod.normalise_deck(slides, budget_s, topic=str(msg.get("topic", "")))
        if not deck.get("topic"):
            deck["topic"] = (slides[0].get("title") if slides else None) or "your uploaded deck"
        await self._apply_deck(deck)

    async def _apply_deck(self, deck: dict[str, Any]) -> None:
        self.deck = deck
        self.budget_s = int(deck["budget_s"])
        self.store.set_deck(deck)
        for s in deck.get("slides", []):
            self.graph.add("slide", s["index"], title=s.get("title"), terms=s.get("terms"))
        self.send({"type": "deck", "deck": deck})
        topic = deck.get("topic") or "your topic"
        self._send_coach("deck_ack")
        instructions = (
            f"Acknowledge in one warm sentence that they chose to talk about {topic}, "
            "mentioning one specific, accurate detail about it, then tell them they can take "
            "up to a minute to prepare or say ready to begin right away. Under 35 words total."
        )
        fallback = f"So you've chosen {topic}, nice. Take a minute to prepare, or say ready to begin right away."
        await self._speak_llm(instructions, fallback, kind="ack")
        self._enter_prep()

    # -- prep -----------------------------------------------------

    def _enter_prep(self) -> None:
        self._set_phase("prep")
        if self._prep_task and not self._prep_task.done():
            self._prep_task.cancel()
        self._prep_task = asyncio.create_task(self._run_prep_countdown())

    async def _run_prep_countdown(self) -> None:
        remaining = PREP_DURATION_S
        try:
            while remaining > 0:
                self.send({"type": "timer", "remaining_s": remaining})
                await asyncio.sleep(1)
                remaining -= 1
            self.send({"type": "timer", "remaining_s": 0})
        except asyncio.CancelledError:
            return
        self._start_present_flow()

    # -- present -----------------------------------------------------

    def _start_present_flow(self, *, scope: dict[str, Any] | str = "full", rerecord: bool = False,
                             slide: int | None = None) -> None:
        if self._prep_task and not self._prep_task.done():
            self._prep_task.cancel()
        if self._flow_task and not self._flow_task.done():
            self._flow_task.cancel()
        self._flow_task = asyncio.create_task(self._run_flow(scope=scope, rerecord=rerecord, slide=slide))

    async def _run_flow(self, *, scope: dict[str, Any] | str, rerecord: bool, slide: int | None) -> None:
        try:
            self.send({"type": "countdown", "seconds": 3})
            cue = self._say(COUNTDOWN_SPEECH, kind="cue", allow_interruptions=False, add_to_chat_ctx=False)
            await cue.wait_for_playout()
            revision = await self._enter_present(scope=scope, rerecord=rerecord, slide=slide)
            wav_path, text, words = await self._finish_present(revision)
            await self._enter_analyze(revision, wav_path, text, words)
        except asyncio.CancelledError:
            self._cleanup_present_recording()
            raise
        except Exception:
            log.exception("presentation flow crashed")
            self.send({"type": "error", "message": "internal error during presentation"})

    async def _enter_present(self, *, scope: dict[str, Any] | str, rerecord: bool, slide: int | None) -> int:
        self.session.update_options(turn_detection="manual")
        rev = self.store.new_revision(scope)
        revision = rev["revision"]
        self.graph.add("revision", revision, scope=scope)
        if self._pending_supersede is not None:
            self.graph.link(f"revision:{revision}", "supersedes", f"revision:{self._pending_supersede}")
            self._pending_supersede = None
        self.active_revision = revision
        self.words_buffer = []
        self.transcript_parts = []
        self.slide_events = []
        self._present_end_event = asyncio.Event()
        self._present_started_mono = time.monotonic()

        if not rerecord:
            self.current_slide = 1
        elif slide is not None:
            self.current_slide = slide
        self._record_slide_event(self.current_slide)

        self.wav_writer = WavWriter(self.store.wav_path(revision), sample_rate=SAMPLE_RATE)
        self._word_stream = self._word_stt.stream()
        self._word_stream_task = asyncio.create_task(self._consume_word_stream(self._word_stream))

        self._set_phase("present")
        self._present_timer_task = asyncio.create_task(self._run_present_timers())

        await self._present_end_event.wait()

        if self._present_timer_task and not self._present_timer_task.done():
            self._present_timer_task.cancel()
        self.session.update_options(turn_detection="vad")
        await self.session.commit_user_turn(skip_reply=True)
        return revision

    async def _finish_present(self, revision: int) -> tuple[Path, str, list[dict[str, Any]]]:
        writer, self.wav_writer = self.wav_writer, None
        stream, self._word_stream = self._word_stream, None
        if stream is not None:
            await stream.aclose()
        if self._word_stream_task:
            try:
                await asyncio.wait_for(self._word_stream_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._word_stream_task.cancel()

        wav_path = writer.finalize() if writer else self.store.wav_path(revision)
        text = " ".join(self.transcript_parts).strip()
        words = list(self.words_buffer)
        self.store.update_revision(revision, ended_at=_now_iso(),
                                    transcript={"text": text, "words": words},
                                    slide_events=list(self.slide_events))
        return wav_path, text, words

    def _cleanup_present_recording(self) -> None:
        if self.wav_writer is not None:
            try:
                self.wav_writer.finalize()
            except Exception:
                log.exception("failed to finalize abandoned wav")
            self.wav_writer = None
        if self._present_timer_task and not self._present_timer_task.done():
            self._present_timer_task.cancel()
        if self._word_stream_task and not self._word_stream_task.done():
            self._word_stream_task.cancel()
        self._word_stream = None

    async def _run_present_timers(self) -> None:
        cue_delay = max(0.0, self.budget_s - 30)
        hard_limit = self.budget_s * 1.5
        cue_task = asyncio.create_task(self._present_cue(cue_delay))
        try:
            await asyncio.sleep(hard_limit)
            self._trigger_present_end("hard_limit")
        except asyncio.CancelledError:
            pass
        finally:
            if not cue_task.done():
                cue_task.cancel()

    async def _present_cue(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # allow_interruptions=True + add_to_chat_ctx=False + manual turn mode still
        # active: the cue plays over the user without taking their turn away.
        self._say("Thirty seconds.", kind="cue", allow_interruptions=True, add_to_chat_ctx=False)

    def _trigger_present_end(self, reason: str) -> None:
        if self.phase != "present" or self._present_end_event is None:
            return
        if not self._present_end_event.is_set():
            log.info("present_end (%s)", reason)
            self._present_end_event.set()

    def _record_slide_event(self, slide: int) -> None:
        at_s = round(time.monotonic() - self._present_started_mono, 3)
        self.slide_events.append({"slide": slide, "at_s": at_s})
        self.store.log("slide", slide=slide)
        self.send({"type": "slide", "slide": slide})

    def _advance_slide(self) -> None:
        if self.phase != "present" or not self.deck:
            return
        total = len(self.deck.get("slides", []))
        if total and self.current_slide >= total:
            return
        self.current_slide += 1
        self._record_slide_event(self.current_slide)

    # -- raw audio / word-timed transcript -----------------------------------------------------

    async def _consume_raw_audio(self, track: rtc.Track) -> None:
        audio_stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for event in audio_stream:
                frame = event.frame
                if self.wav_writer is not None:
                    self.wav_writer.write_frame(frame)
                if self._word_stream is not None:
                    self._word_stream.push_frame(frame)
                if self._capture_writer is not None:
                    self._capture_writer.write_frame(frame)
                if self._capture_stream is not None:
                    self._capture_stream.push_frame(frame)
        finally:
            await audio_stream.aclose()

    async def _consume_word_stream(self, stream: Any) -> None:
        from livekit.agents.stt import SpeechEventType

        try:
            async for ev in stream:
                if ev.type != SpeechEventType.FINAL_TRANSCRIPT or not ev.alternatives:
                    continue
                alt = ev.alternatives[0]
                # TimedString carries only (text, start_time, end_time) -- no
                # per-word confidence field (docs/livekit-agents.md §5). We fall
                # back to the alternative-level SpeechData.confidence for every
                # word in it; coarser than true per-word confidence but it's the
                # only value the framework exposes. v2's analyze phase upgrades
                # this to real per-word confidence via a Deepgram REST call.
                conf = round(float(alt.confidence), 3)
                for w in (alt.words or []):
                    self.words_buffer.append({
                        "w": str(w), "start": round(float(w.start_time), 3),
                        "end": round(float(w.end_time), 3), "conf": conf,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("word stream consumer crashed")

    # -- practice-attempt raw-audio capture -----------------------------------------------------

    async def _start_capture_named(self, name: str) -> None:
        """v2: endpointing widens to 0.9s only while a capture is open (drill
        answers and practice attempts both go through here) -- restored to 0.5s
        in _stop_capture, instead of for the whole coach phase."""
        path = self.store.clips_dir / f"{name}.wav"
        self._capture_writer = WavWriter(path, sample_rate=SAMPLE_RATE)
        self._capture_words = []
        self._capture_stream = self._word_stt.stream()
        self._capture_task = asyncio.create_task(self._consume_capture_stream(self._capture_stream))
        self._capture_active = True
        self.session.update_options(endpointing_opts={"min_delay": 0.9})

    async def _start_capture(self, item: ImprovementItem, n: int) -> None:
        await self._start_capture_named(f"{item.data['id']}_attempt{n}")

    async def _consume_capture_stream(self, stream: Any) -> None:
        from livekit.agents.stt import SpeechEventType

        try:
            async for ev in stream:
                if ev.type != SpeechEventType.FINAL_TRANSCRIPT or not ev.alternatives:
                    continue
                alt = ev.alternatives[0]
                conf = round(float(alt.confidence), 3)
                for w in (alt.words or []):
                    self._capture_words.append({
                        "w": str(w), "start": round(float(w.start_time), 3),
                        "end": round(float(w.end_time), 3), "conf": conf,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("practice capture stream crashed")

    async def _stop_capture(self) -> tuple[Path | None, list[dict[str, Any]]]:
        self._capture_active = False
        self.session.update_options(endpointing_opts={"min_delay": 0.5})
        writer, self._capture_writer = self._capture_writer, None
        stream, self._capture_stream = self._capture_stream, None
        if stream is not None:
            await stream.aclose()
        task, self._capture_task = self._capture_task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        words = list(self._capture_words)
        self._capture_words = []
        if writer is None:
            return None, words
        wav_path = writer.finalize()
        if not words:
            # Nothing was said into this capture (a menu command or the item
            # moving on): keep the session directory free of silent takes.
            wav_path.unlink(missing_ok=True)
            return None, words
        _trim_wav_to_words(wav_path, words)
        return wav_path, words

    # -- analyze -----------------------------------------------------

    async def _enter_analyze(self, revision: int, wav_path: Path, text: str, words: list[dict[str, Any]]) -> None:
        self._set_phase("analyze")
        self._say("Got it, give me a moment.", kind="ack")

        rev_data = self.store.get_revision(revision)
        deck_terms = [t for s in (self.deck or {}).get("slides", []) for t in s.get("terms", [])]

        # v2: a Deepgram REST re-transcription gives real per-word confidence
        # (the live STT stream only exposes segment-level confidence). This runs
        # right after the ack, non-blocking relative to it (_say doesn't await
        # playout), and its output -- when it succeeds -- replaces `words` for
        # both metrics and the judge, per CONTRACTS §2/§3.
        pron_block: dict[str, Any] | None = None
        try:
            rest_words = await asyncio.to_thread(pronunciation_mod.transcribe_rest, str(wav_path))
            pron_block = pronunciation_mod.intelligibility(rest_words, deck_terms, source="deepgram_rest")
            words = rest_words
        except pronunciation_mod.PronunciationError as exc:
            self.store.log("tool_error", tool="pronunciation", error=str(exc)[:200])
            # Fall through with the original stream words; metrics.compute()
            # builds its own source="stream" pronunciation block when none is given.

        metrics = metrics_mod.compute(words=words, audio_path=str(wav_path), deck=self.deck or {},
                                       slide_events=rev_data["slide_events"], budget_s=self.budget_s,
                                       pronunciation=pron_block)
        self.current_metrics = metrics
        self.store.update_revision(revision, metrics=metrics, transcript={"text": text, "words": words})
        self.send({"type": "metrics", "revision": revision, "metrics": metrics})

        # Nothing was captured: scoring an empty rehearsal would invent feedback and
        # burn a judge call, so say so plainly and let the user run it again.
        if metrics["words"] < MIN_WORDS_TO_JUDGE:
            self.store.log("no_speech", revision=revision, words=metrics["words"])
            self._say("I did not hear your presentation. Check your microphone, then say "
                      "you're ready and start again.", kind="feedback")
            self._set_phase("prep")
            self.send({"type": "phase", "name": "prep", "budget_s": self.budget_s})
            return

        self._post_analyze_task = asyncio.create_task(self._judge_then_coach(revision, text, words, metrics))

    async def _judge_then_coach(self, revision: int, text: str, words: list[dict[str, Any]],
                                 metrics: dict[str, Any]) -> None:
        start = time.monotonic()
        self.store.log("tool_start", tool="judge", revision=revision)
        try:
            judgment = await judge_mod.judge(deck=self.deck or {}, slide=None, transcript_text=text,
                                              words=words, metrics=metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("judge failed for revision %s", revision)
            self.store.log("tool_end", tool="judge", revision=revision, ms=round((time.monotonic() - start) * 1000))
            self.send({"type": "error", "message": "judging failed"})
            return
        ms = round((time.monotonic() - start) * 1000)
        self.store.log("tool_end", tool="judge", revision=revision, ms=ms)

        if revision != self.store.current_revision:
            self.store.log("stale_dropped", tool="judge", revision=revision, current_revision=self.store.current_revision)
            return

        self.current_judgment = judgment
        self.store.update_revision(revision, judgment=judgment)
        self.send({"type": "judgment", "revision": revision, "judgment": judgment})
        await self._enter_coach(revision, judgment)

    def current_metrics_summary(self) -> dict[str, Any]:
        m = self.current_metrics
        if not m:
            return {"status": "not_available_yet"}
        return {
            "wpm": m.get("wpm"),
            "duration_s": m.get("duration_s"),
            "filler_count": m.get("fillers", {}).get("count"),
            "filler_per_min": m.get("fillers", {}).get("per_min"),
            "pause_count": m.get("pauses", {}).get("count"),
            "longest_pause_s": m.get("pauses", {}).get("longest_s"),
            "time_over_budget_s": m.get("time_budget", {}).get("over_s"),
        }

    def explain_rubric(self, improvement_id: str) -> str:
        if not self.current_judgment:
            return "I don't have a judgment ready yet."
        imp = next((i for i in self.current_judgment.get("improvements", []) if i.get("id") == improvement_id), None)
        if not imp:
            return f"I don't have an improvement called {improvement_id}."
        return self._why_text(imp)

    # -- coach -----------------------------------------------------

    async def _enter_coach(self, revision: int, judgment: dict[str, Any]) -> None:
        self._set_phase("coach")
        try:
            await self._coach_summary_and_items(revision, judgment)
        finally:
            await self._leave_coach()
        await self._enter_drill(revision)

    def _cancel_attempt_task(self) -> None:
        task, self._attempt_task = self._attempt_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _leave_coach(self) -> None:
        self._cancel_attempt_task()
        await self._stop_capture()
        self._expect = None
        self._offered = []
        self._intent_future = None
        self._current_item = None

    async def _prerender_all_items(self, revision: int) -> None:
        """v2: pre-render every improvement's clips (V1/V2/V3 + the V0 slice)
        concurrently while the summary line is being spoken, so no render gap
        opens once the conversation reaches the item. `_render_clips` itself
        fences on the current revision, so a stale batch is silently dropped."""
        items = list(self.improvement_queue)
        if not items:
            return
        results = await asyncio.gather(*(self._render_clips(item) for item in items), return_exceptions=True)
        for item, clips in zip(items, results):
            if isinstance(clips, BaseException):
                log.exception("prerender failed for %s", item.data.get("id"), exc_info=clips)
                continue
            if clips is not None and item.clips is None:
                item.clips = clips

    async def _coach_summary_and_items(self, revision: int, judgment: dict[str, Any]) -> None:
        self._send_coach("summary")

        self.improvement_queue = [ImprovementItem(data=imp, revision=revision)
                                   for imp in judgment.get("improvements", [])]
        self.drill_terms = list(judgment.get("terms_to_drill", []))
        self.drill_words = list(judgment.get("drill_words", []))
        for item in self.improvement_queue:
            self._graph_add_improvement(item)
        if self.improvement_queue:
            asyncio.create_task(self._prerender_all_items(revision))

        m = self.current_metrics or {}
        scores = judgment.get("scores", {})
        facts = (
            f"summary={judgment.get('summary', '')!r} scores={scores} wpm={m.get('wpm')} "
            f"filler_count={m.get('fillers', {}).get('count')} filler_per_min={m.get('fillers', {}).get('per_min')} "
            f"longest_pause_s={m.get('pauses', {}).get('longest_s')} "
            f"time_used_s={m.get('time_budget', {}).get('used_s')} budget_s={self.budget_s}"
        )
        instructions = (
            "Give a warm two to three sentence summary of how the talk went, using only these "
            f"facts: {facts}. Mention at least one specific number naturally, the way a friend would."
        )
        fallback = judgment.get("summary", "") or "Nice work out there."
        await self._speak_llm(instructions, fallback, kind="feedback")

        if not self.improvement_queue:
            return

        n = len(self.improvement_queue)
        self._send_coach("ask_proceed", options=[
            {"name": "proceed", "label": "Let's do it"},
            {"name": "later", "label": "Maybe later"},
        ])
        self._expect = "proceed"
        self._say(f"Want to work through the {n} improvements together?", kind="feedback")
        intent = await self._await_intent(ASK_PROCEED_WAIT_S)
        if intent is None or intent == "proceed":
            await self._run_items_loop()
        else:
            self._say("No problem, we'll save those for later.", kind="feedback")

    async def _run_items_loop(self) -> None:
        idx = 0
        while idx < len(self.improvement_queue):
            item = self.improvement_queue[idx]
            if item.revision != self.store.current_revision:
                self.store.log("stale_dropped", tool="coach_queue", revision=item.revision,
                                current_revision=self.store.current_revision)
                idx += 1
                continue

            outcome = await self._deliver_improvement_flow(item, idx)
            if outcome in ("next", "skip"):
                idx += 1
            elif outcome == "finish":
                break

    # -- item trio (v2: V0 -> V2 -> V3, resumable) -----------------------------------------------------

    def _trio_slot(self, item: ImprovementItem, slot: int) -> tuple[str, str]:
        """Slot 0/1/2 -> (variant key into item.clips, spoken label). Slot 0 is
        the user's own recording (v0) when slicing succeeded, else the Rime
        re-render (v1) -- either way it is announced as feedback stage "v0"
        (CONTRACTS §5 only defines that one stage for the first beat)."""
        if slot == 0:
            variant = "v0" if item.clips and "v0" in item.clips else "v1"
            return variant, "Here's you:"
        if slot == 1:
            return "v2", "Here's it cleaner:"
        return "v3", "And with a pause before the key idea:"

    async def _play_variant_v2(self, item: ImprovementItem, variant: str, label: str) -> bool:
        imp_id = item.data["id"]
        stage_name = "v0" if variant in ("v0", "v1") else variant
        self._send_feedback_stage(imp_id, stage_name, text=label)
        handle = self._say(label, kind="feedback")
        await handle.wait_for_playout()
        if handle.interrupted:
            return False
        clip = (item.clips or {}).get(variant)
        if clip is None:
            return True  # nothing to play (e.g. v0 failed and v1 also missing) -- don't get stuck
        frames = audio_frames_from_file(clip["path"], sample_rate=SAMPLE_RATE, num_channels=1)
        clip_handle = self._say(_strip_pause_markup(clip["text"]), kind="clip", audio=frames, add_to_chat_ctx=False)
        await clip_handle.wait_for_playout()
        ok = not clip_handle.interrupted
        if ok:
            self._mark_played(imp_id, variant)
        return ok

    async def _play_trio_v2(self, item: ImprovementItem) -> bool:
        for slot in range(3):
            variant, label = self._trio_slot(item, slot)
            if not await self._play_variant_v2(item, variant, label):
                return False
        return True

    async def _play_stage(self, item: ImprovementItem, idx: int, total: int) -> str | None:
        """Plays exactly one stage of `item.stage_idx` (0 = intro, 1..3 = trio
        slots). Returns None and advances the stage on success, or the outcome
        from `_resolve_interruption` when cut off."""
        imp = item.data
        stage = item.stage_idx
        if stage == 0:
            self._send_feedback_stage(imp["id"], "intro", text=imp.get("issue"))
            skill_title = _skill_title(imp.get("skill"))
            slide = imp.get("slide")
            slide_part = f" on slide {slide}" if slide else ""
            line = f"Improvement {idx + 1} of {total} \u2014 {skill_title}{slide_part}: {imp['issue']}"
            handle = self._say(line, kind="feedback")
            await handle.wait_for_playout()
            ok = not handle.interrupted
        else:
            variant, label = self._trio_slot(item, stage - 1)
            ok = await self._play_variant_v2(item, variant, label)
        if ok:
            if stage >= 1:
                slot_variant, _ = self._trio_slot(item, stage - 1)
                item.completed.add(slot_variant)
            item.stage_idx += 1
            return None
        return await self._resolve_interruption(item)

    async def _wait_for_default_reply(self, timeout: float = RESUME_REPLY_TIMEOUT_S) -> None:
        """Armed by _resolve_interruption right before it returns "resume":
        waits for the framework's automatic post-turn reply (captured via the
        speech_created listener) to finish playing, so "Back to it" never talks
        over the LLM's answer to the user's remark."""
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._await_default_reply_future = fut
        try:
            handle = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._await_default_reply_future = None
            return
        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _resolve_interruption(self, item: ImprovementItem) -> str:
        """Recognised commands (skip/next/finish/slower/why/again/...) behave as
        before v2: skip/next/finish exit the item, everything else forces a full
        restart from the intro. An utterance that matched no command means the
        default LLM reply is already in flight (route_utterance returned False);
        v2 waits for it, cues "resume", and picks the trio back up exactly where
        it was cut off -- never from the intro."""
        cmd, self._pending_command = self._pending_command, None
        if cmd in ("skip", "next", "finish"):
            return cmd
        if cmd == "slower":
            item.clips = None
            item.slower = True
            item.stage_idx = 0
            item.completed.clear()
            return "restart"
        if cmd == "why":
            h = self._say(self._why_text(item.data)[:180], kind="answer")
            await h.wait_for_playout()
            return "restart"
        if cmd is not None:
            # "again", or any other recognised command mid-trio: full restart, as today.
            item.stage_idx = 0
            item.completed.clear()
            return "restart"
        await self._wait_for_default_reply()
        self._send_feedback_stage(item.data["id"], "resume")
        await self._say_and_wait("Back to it \u2014", kind="feedback")
        return "resume"

    async def _deliver_improvement_flow(self, item: ImprovementItem, idx: int) -> str:
        imp = item.data
        total = len(self.improvement_queue)
        self._current_item = item
        self.graph.set_focus(f"improvement:{imp['id']}")
        self._expect = "menu"
        self._send_coach("item", options=_ITEM_OPTIONS, improvement_id=imp["id"], index=idx + 1, total=total)

        self._trio_active = True
        try:
            while item.stage_idx < 4:
                if item.clips is None:
                    clips = await self._render_clips(item)
                    if clips is None:
                        return "skip"  # fenced: revision went stale mid-render, already logged
                    item.clips = clips
                outcome = await self._play_stage(item, idx, total)
                if outcome in ("restart", "resume"):
                    continue
                if outcome is not None:
                    return outcome
        finally:
            self._trio_active = False

        if {"v0", "v1"} & item.completed and "v2" in item.completed and "v3" in item.completed:
            if not item.heard:
                item.heard = True
                self.store.log("feedback_heard", improvement_id=imp["id"])
                self.graph.mark(f"improvement:{imp['id']}", heard=True)

        return await self._practice_loop(item, idx, total)

    async def _render_clips(self, item: ImprovementItem) -> dict[str, dict[str, Any]] | None:
        revision = item.revision
        rev_data = self.store.get_revision(revision)
        rev_wav = self.store.wav_path(revision)
        words = rev_data["transcript"]["words"]
        start = time.monotonic()
        self.store.log("tool_start", tool="render", revision=revision)
        clips, v0 = await asyncio.gather(
            render_mod.render_variants(item.data, self.store.clips_dir, slower=item.slower,
                                        model=RIME_MODEL, speaker=RIME_SPEAKER),
            asyncio.to_thread(slice_mod.slice_for_improvement, rev_wav, item.data, words, self.store.clips_dir),
        )
        ms = round((time.monotonic() - start) * 1000)
        self.store.log("tool_end", tool="render", revision=revision, ms=ms)

        if revision != self.store.current_revision:
            self.store.log("stale_dropped", tool="render", revision=revision, current_revision=self.store.current_revision)
            return None

        if v0 is not None:
            clips = {**clips, "v0": v0}

        imp_id = item.data["id"]
        for variant, info in clips.items():
            self.send({
                "type": "clip", "improvement_id": imp_id, "variant": variant,
                "url": f"/sessions/{self.store.session_id}/clips/{Path(info['path']).name}",
            })
            clip_key = self.graph.add("clip", f"{imp_id}:{variant}", variant=variant, duration_s=info.get("duration_s"))
            self.graph.link(clip_key, "renders", f"improvement:{imp_id}")
        return clips

    async def _handle_command(self, name: str) -> None:
        self._deliver_intent(name, "button")

    # -- practice loop -----------------------------------------------------

    async def _practice_loop(self, item: ImprovementItem, idx: int, total: int) -> str:
        imp = item.data
        prompt = _PRACTICE_PROMPTS[min(idx, len(_PRACTICE_PROMPTS) - 1)]
        self._send_feedback_stage(imp["id"], "prompt", text=prompt)
        self._say(prompt, kind="feedback")
        self._send_coach("practice", options=_ITEM_OPTIONS, improvement_id=imp["id"], index=idx + 1, total=total)
        self._expect = "menu"
        self._attempt_n = 1
        await self._start_capture(item, self._attempt_n)

        while True:
            intent = await self._await_intent(PRACTICE_WAIT_S)
            if intent is None:
                intent = "next"
            if intent in ("next", "skip", "finish"):
                self._cancel_attempt_task()
                await self._stop_capture()
                return intent
            if intent != _ATTEMPT_HANDLED:
                await self._handle_menu_intent(item, intent)
                if self._pending_command is not None:
                    # A command landed mid-replay and interrupted it; the next
                    # _await_intent picks it up before opening a fresh capture.
                    continue
            self._attempt_n += 1
            await self._start_capture(item, self._attempt_n)

    async def _handle_menu_intent(self, item: ImprovementItem, intent: str) -> None:
        imp = item.data
        await self._stop_capture()
        if intent == "original":
            variant, label = self._trio_slot(item, 0)
            await self._play_variant_v2(item, variant, label)
        elif intent == "cleaner":
            await self._play_variant_v2(item, "v2", "cleaner")
        elif intent == "pauses":
            await self._play_variant_v2(item, "v3", "with pauses")
        elif intent == "alternative":
            self._send_feedback_stage(imp["id"], "alt", text=imp.get("alternative", ""))
            await self._say_and_wait(f"Another way in: {imp.get('alternative', '')}", kind="feedback")
        elif intent == "again":
            await self._play_trio_v2(item)
        elif intent == "slower":
            item.clips = None
            item.slower = True
            clips = await self._render_clips(item)
            if clips is not None:
                item.clips = clips
                await self._play_trio_v2(item)
        elif intent == "why":
            await self._say_and_wait(self._why_text(imp)[:180], kind="answer")
        elif intent == "practice":
            await self._say_and_wait("Go ahead, I'm listening.", kind="feedback")

    async def _handle_attempt(self, text: str) -> None:
        item = self._current_item
        if item is None:
            return
        n = self._attempt_n
        wav_path, words = await self._stop_capture()
        metrics = None
        if words and wav_path is not None:
            metrics = metrics_mod.compute(words=words, audio_path=str(wav_path), deck=self.deck or {},
                                           slide_events=[], budget_s=self.budget_s)
        verdict = practice_mod.evaluate_attempt(item.data, attempt=n, text=text, words=words, metrics=metrics)
        imp_id = item.data["id"]
        self.store.log("practice_attempt", improvement_id=imp_id, attempt=n,
                        fillers=verdict["fillers"]["count"], longest_pause_s=verdict["pauses"]["longest_s"],
                        wpm=verdict["wpm"], on_point=verdict["on_point"])
        attempt_key = self.graph.add("attempt", f"{imp_id}#{n}", text=text[:300], wpm=verdict.get("wpm"))
        self.graph.link(attempt_key, "practices", f"improvement:{imp_id}")
        verdict_key = self.graph.add("verdict", f"{imp_id}#{n}", **verdict)
        self.graph.link(verdict_key, "scores", attempt_key)
        self._verdicts.append({**verdict, "improvement_id": imp_id})
        if wav_path is not None:
            self.send({"type": "clip", "improvement_id": imp_id, "variant": f"attempt{n}",
                       "url": f"/sessions/{self.store.session_id}/clips/{wav_path.name}"})

        idx = next(i for i, x in enumerate(self.improvement_queue) if x is item)
        total = len(self.improvement_queue)
        self._send_feedback_stage(imp_id, "verdict")
        self._send_coach("verdict", options=_ITEM_OPTIONS, improvement_id=imp_id,
                          index=idx + 1, total=total, attempt=n, verdict=verdict)
        # Let the practice loop open the next take's capture now, so a user who
        # goes straight into another attempt while the verdict is still being
        # spoken is recorded rather than routed to the LLM.
        if self._intent_future is not None and not self._intent_future.done():
            self._intent_future.set_result(_ATTEMPT_HANDLED)

        facts = (
            f"attempt={n} words={verdict['words']} wpm={verdict['wpm']} "
            f"fillers_before={verdict['original_fillers']} fillers_after={verdict['fillers']['count']} "
            f"pause_landed={verdict['pauses']['landed']} longest_pause_s={verdict['pauses']['longest_s']} "
            f"on_point={verdict['on_point']} pace_band={verdict['pace_band']} "
            f"wins={verdict['wins']} next_focus={verdict['next_focus']}"
        )
        instructions = (
            "The user just repeated the point themselves. Phrase these numbers as a friend "
            "would: mention fillers before versus after, whether the pause landed, and their "
            f"pace, using only these facts: {facts}. End with a short encouraging line."
        )
        fallback = practice_mod.scripted_verdict(verdict, item.data)
        handle = await self._speak_llm(instructions, fallback, kind="feedback")
        if not handle.interrupted and self._current_item is item:
            self._say("Again, or next?", kind="feedback")

    # -- drill -----------------------------------------------------

    async def _enter_drill(self, revision: int) -> None:
        drill_words = list(self.drill_words)[:DRILL_MAX_WORDS]
        # The raw deck-term drill is only a fallback for when per-word confidence
        # was unavailable; if REST confidence says the take was clear, there is
        # nothing to drill and saying so is the honest outcome.
        pron = (self.current_metrics or {}).get("pronunciation") or {}
        terms = [] if pron.get("source") == "deepgram_rest" else list(self.drill_terms)
        if not drill_words and not terms:
            await self._enter_report()
            return
        self._set_phase("drill")
        self._send_coach("drill")
        if drill_words:
            rev_wav = self.store.wav_path(revision)
            for word in drill_words:
                await self._drill_word(word, rev_wav)
        else:
            for term in terms:
                await self._drill_term(term)
        await self._enter_report()

    async def _drill_word(self, word: dict[str, Any], rev_wav: Path) -> None:
        """v2: a low-confidence word from judgment["drill_words"] -- play the
        user's own recording of it, say it clearly, capture their retry, and
        compare recogniser confidence before/after. Framed as intelligibility
        (how a recogniser understood it), never as an accent judgement."""
        w = str(word.get("w", ""))
        conf_before = word.get("conf")
        self.graph.set_focus(f"drill:{w}")
        drill_key = self.graph.add("drill", w, conf_before=conf_before)
        self.graph.link(drill_key, "addresses", "skill:articulation")

        you_clip: dict[str, Any] | None = None
        try:
            you_clip = await asyncio.to_thread(slice_mod.slice_word, rev_wav, word, self.store.clips_dir, f"drill_{w}_you")
        except Exception:
            log.exception("drill slice_word failed for %r", w)

        you_url = (f"/sessions/{self.store.session_id}/clips/{Path(you_clip['path']).name}" if you_clip else None)
        self._send_drill(w, "you", url=you_url)
        label_handle = await self._say_and_wait("Quick check -- here's a word you said:", kind="feedback")
        if not label_handle.interrupted and you_clip is not None:
            frames = audio_frames_from_file(you_clip["path"], sample_rate=SAMPLE_RATE, num_channels=1)
            clip_handle = self._say(w, kind="clip", audio=frames, add_to_chat_ctx=False)
            await clip_handle.wait_for_playout()

        self._send_drill(w, "model")
        await self._say_and_wait(
            f"Here it is again: {w}. This checks whether a recogniser understands it, not your accent.",
            kind="feedback",
        )

        # Open the capture before the prompt: people answer over the end of a
        # question, and a word said during "say it for me" must still count.
        await self._start_capture_named(f"drill_{w}_answer")
        self._expect = "drill"
        self._send_drill(w, "prompt")
        await self._say_and_wait(f"Your turn -- say '{w}' for me.", kind="feedback")
        heard_text = await self._await_drill_answer(DRILL_ANSWER_TIMEOUT_S)
        wav_path, capture_words = await self._stop_capture()

        conf_after: float | None = None
        if wav_path is not None:
            try:
                rest_words = await asyncio.to_thread(pronunciation_mod.transcribe_rest, str(wav_path))
                match = next((rw for rw in rest_words if _close_enough(rw["w"], w)), None)
                if match is not None:
                    conf_after = match["conf"]
            except pronunciation_mod.PronunciationError as exc:
                self.store.log("tool_error", tool="pronunciation", error=str(exc)[:200])
            if conf_after is None:
                match = next((cw for cw in capture_words if _close_enough(cw["w"], w)), None)
                if match is not None:
                    conf_after = match.get("conf")

        heard_ok = (heard_text is not None and _close_enough(heard_text, w)) or (
            conf_after is not None and conf_after >= pronunciation_mod.LOW_CONF_THRESHOLD)
        self._send_drill(w, "verdict", conf_before=conf_before, conf_after=conf_after)
        self.graph.mark(drill_key, conf_after=conf_after, heard_ok=heard_ok)
        if heard_ok and conf_before is not None and conf_after is not None:
            line = (f"Nice -- the recogniser understood '{w}' at {round(conf_after * 100)} percent that time, "
                    f"up from {round(conf_before * 100)} percent.")
        elif heard_ok:
            line = f"Good, that came through clearly as {w}."
        else:
            line = f"Recognisers still stumble on {w}; worth flagging to your audience."
        await self._say_and_wait(line, kind="feedback")

    async def _drill_term(self, term: str) -> None:
        """Fallback drill used only when the judge returned no drill_words (e.g.
        a very short or very clean take): the pre-v2 raw-deck-term check."""
        await self._say_and_wait(
            f"Quick check: say the word '{term}'. This checks whether a recogniser understands "
            f"it, not your pronunciation.", kind="feedback",
        )
        for attempt in range(2):
            heard = await self._await_drill_answer(timeout=8.0)
            if heard is not None and _close_enough(heard, term):
                await self._say_and_wait(f"Good, that came through clearly as {term}.", kind="feedback")
                return
            if attempt == 0:
                await self._say_and_wait("Let's try that once more.", kind="feedback")
        await self._say_and_wait(f"Recognisers still stumble on {term}; worth flagging to your audience.",
                                  kind="feedback")

    async def _await_drill_answer(self, timeout: float) -> str | None:
        self._expect = "drill"
        self._drill_answer_future = asyncio.get_event_loop().create_future()
        try:
            return await asyncio.wait_for(self._drill_answer_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._drill_answer_future = None
            self._expect = None

    def deliver_drill_answer(self, text: str) -> None:
        if self._drill_answer_future is not None and not self._drill_answer_future.done():
            self._drill_answer_future.set_result(text)

    # -- report -----------------------------------------------------

    async def _enter_report(self) -> None:
        self._set_phase("report")
        self._send_coach("wrap")
        heard = sum(1 for i in self.improvement_queue if i.heard)
        total = len(self.improvement_queue)
        self._say(f"That's a wrap. We worked through {heard} of {total} improvements together.", kind="feedback")

        if self.progress is not None and self.current_judgment is not None:
            self.progress.record_session(self.current_judgment.get("scores", {}),
                                          self.current_judgment.get("improvements", []),
                                          list(self._verdicts))
            msg = self.progress.to_message()
            self.store.log("progress", level=msg["level"], next_focus=msg["next_focus"])
            self.send(msg)
            level, next_focus = msg["level"], msg["next_focus"]
            instructions = (
                f"Tell the user in one warm sentence that they're now at the {level} level overall, "
                f"and that next time you'll focus on {next_focus}. Use only these facts."
            )
            fallback = f"You're now at the {level} level overall -- next time, let's focus on {next_focus}."
            await self._speak_llm(instructions, fallback, kind="feedback")

        self._say("Re-record any slide whenever you want another pass, or start a brand new talk. "
                  "Good luck out there.", kind="feedback")
        self.store.set_graph(self.graph.to_json())

    # -- rerecord -----------------------------------------------------

    async def _handle_rerecord(self, msg: dict[str, Any]) -> None:
        if self.deck is None:
            self.send({"type": "error", "message": "rerecord requires an active deck"})
            return
        slide = msg.get("slide")
        if self._post_analyze_task and not self._post_analyze_task.done():
            self._post_analyze_task.cancel()
        old_revision = self.store.current_revision
        if old_revision:
            self.store.supersede(old_revision)
            self._pending_supersede = old_revision
        self.session.interrupt()
        self._start_present_flow(scope={"slide": slide} if slide is not None else "full",
                                  rerecord=True, slide=slide)


def build_agent_and_session(store: Store, *, ctx: agents.JobContext | None = None,
                             ) -> tuple[AgentSession, PodiumAgent, PodiumOrchestrator]:
    """Construct the real coach session (frozen STT/LLM/TTS/VAD per GATE0.md) and its
    orchestrator. `ctx` is optional: a live room wires raw-audio capture and the podium
    data channel via `orchestrator.wire_room()`; without it, drive the same orchestrator
    through `orchestrator.on_client(...)` with `session.input.audio` set directly."""
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="en", filler_words=True, punctuate=True),
        llm=coach_llm(),
        tts=rime.TTS(model=RIME_MODEL, speaker=RIME_SPEAKER, lang=RIME_LANG,
                     sample_rate=SAMPLE_RATE, use_websocket=True,
                     pause_between_brackets=(RIME_MODEL != "coda")),
        # min_speech_duration 0.05 -> 0.03: the VAD's own onset delay is the floor
        # under interruption.min_duration, so it has to be smaller than it.
        vad=silero.VAD.load(min_speech_duration=0.03, min_silence_duration=0.55),
        # Barge-in speed is a judged property, and the SDK default
        # interruption.min_duration of 0.5 s alone put measured stop latency near
        # one second (evidence/e2/results.json part_c). 0.12 s of voiced audio is
        # still far above a click or a breath, and false_interruption_timeout keeps
        # the coach resuming when a short noise turns out not to be speech.
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            interruption={
                "mode": "vad",
                "min_duration": 0.12,
                "discard_audio_if_uninterruptible": True,
                "resume_false_interruption": True,
                "false_interruption_timeout": 1.5,
            },
            # v2: start LLM inference on the interim transcript instead of
            # waiting for end-of-turn, shaving reply latency off every turn.
            # (This is also livekit-agents 1.8.0's own default; set explicitly
            # so the choice is visible here rather than relying on the SDK
            # default surviving a future upgrade.)
            preemptive_generation={"enabled": True},
        ),
        use_tts_aligned_transcript=True,
    )
    orchestrator = PodiumOrchestrator(session, store, ctx=ctx)
    agent = PodiumAgent(orchestrator)
    orchestrator.agent = agent
    orchestrator.wire_session_events()
    return session, agent, orchestrator


server = agents.AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext) -> None:
    store = Store()
    session, agent, orchestrator = build_agent_and_session(store, ctx=ctx)
    orchestrator.wire_room()
    # LiveKit Cloud dispatches jobs with enable_recording=True, which makes the
    # SDK buffer the whole session's audio in memory for upload (a multi-hour
    # idle tab reached a 2.8 GiB allocation failure). Podium writes its own
    # per-revision WAVs, so the server-side recording is pure cost.
    await session.start(room=ctx.room, agent=agent, record=False)
    await orchestrator.start()


if __name__ == "__main__":
    agents.cli.run_app(server)

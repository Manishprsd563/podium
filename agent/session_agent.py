"""Podium's voice session: setup -> prep -> present -> analyze -> coach -> drill -> report.

Phase transitions, the presentation-mode turn policy, the revision fence, and the
coach's contrastive playback queue all live here. See CONTRACTS.md for the frozen
shapes this module speaks on the wire and writes to disk.

`build_agent_and_session()` is the seam that makes this testable without a live
LiveKit room: it returns a real `AgentSession` + `PodiumOrchestrator` pair. Pass
`ctx=None` and the orchestrator never touches `ctx.room` — a caller can instead set
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from livekit.agents.utils.audio import audio_frames_from_file
from livekit.plugins import deepgram, rime, silero

import analysis.deck as deck_mod
import analysis.judge as judge_mod
import analysis.metrics as metrics_mod
import analysis.render as render_mod
from agent.llm_config import COACH_MODEL, JUDGE_MODEL, coach_llm
from agent.store import Store, WavWriter

load_dotenv()
log = logging.getLogger("podium.session_agent")

RIME_MODEL = os.environ.get("RIME_MODEL", "mistv3")
RIME_SPEAKER = os.environ.get("RIME_SPEAKER", "astra")
RIME_LANG = "eng"
SAMPLE_RATE = 24000
PREP_DURATION_S = 60  # PLAN.md §4.2: "countdown (default 60 s)"; not in the frozen
                       # client->agent "setup" shape, so it isn't client-configurable.

# Below this, treat the recording as "nothing heard" rather than a bad talk.
MIN_WORDS_TO_JUDGE = 12

COACH_INSTRUCTIONS = (
    "You are Podium, a warm, direct presentation coach speaking through Rime. "
    "Keep every reply to one or two short sentences, at most 25 words total. "
    "Never use markdown, bullet points, numbers, or lists -- you are speaking aloud. "
    "Call presentation_metrics to answer questions about pace, fillers, or pauses "
    "before the full judgment is ready. Call explain_rubric when asked why something "
    "was flagged. Be encouraging but concrete: name the specific thing, not vague praise."
)

_PRESENT_END_RE = re.compile(r"\b(i'?m\s+done|that'?s\s+it|i'?m\s+finished)\b", re.I)
_NEXT_SLIDE_RE = re.compile(r"\bnext\s+slide\b", re.I)
_PAUSE_MARKUP_RE = re.compile(r"<\d+>")
_COMMAND_PATTERNS: dict[str, re.Pattern[str]] = {
    "skip": re.compile(r"^\s*skip\.?\s*$", re.I),
    "again": re.compile(r"^\s*(again|repeat that)\.?\s*$", re.I),
    "slower": re.compile(r"^\s*(slower|slow (it |that )?down)\.?\s*$", re.I),
    "why": re.compile(r"^\s*why\.?\??\s*$", re.I),
}
_VARIANT_LABELS = (("v1", "you said"), ("v2", "cleaner"), ("v3", "with pauses"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _match_command(text: str) -> str | None:
    for name, pattern in _COMMAND_PATTERNS.items():
        if pattern.match(text.strip()):
            return name
    return None


def _strip_pause_markup(text: str) -> str:
    return _PAUSE_MARKUP_RE.sub(" ", text).strip()


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


@dataclass
class ImprovementItem:
    data: dict[str, Any]
    revision: int
    heard: bool = False
    slower: bool = False
    clips: dict[str, dict[str, Any]] | None = None


class PodiumAgent(Agent):
    """Thin LLM-facing shell. Deterministic control flow lives on the orchestrator;
    this class only owns the persona, the two function tools, and intercepting
    coach-phase commands / drill answers before the framework's default LLM reply."""

    def __init__(self, orchestrator: "PodiumOrchestrator") -> None:
        super().__init__(instructions=COACH_INSTRUCTIONS)
        self._orch = orchestrator

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        text = (getattr(new_message, "text_content", None) or "").strip()
        if not text:
            return
        orch = self._orch
        if orch.phase == "drill":
            orch.deliver_drill_answer(text)
            raise StopResponse()
        if orch.phase == "coach":
            cmd = _match_command(text)
            if cmd:
                orch.trigger_voice_command(cmd)
                raise StopResponse()
        # otherwise: fall through to the default LLM reply (may call presentation_metrics
        # / explain_rubric below for a genuine question, e.g. "how was my pacing?")

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

        self._prep_task: asyncio.Task | None = None
        self._flow_task: asyncio.Task | None = None
        self._post_analyze_task: asyncio.Task | None = None

        self.current_metrics: dict[str, Any] | None = None
        self.current_judgment: dict[str, Any] | None = None
        self.improvement_queue: list[ImprovementItem] = []
        self.drill_terms: list[str] = []
        self._pending_command: str | None = None
        self._drill_answer_future: asyncio.Future[str] | None = None
        self._last_user_speech_start_mono: float | None = None

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

    def wire_room(self) -> None:
        """Only meaningful with a live room: raw-audio capture for the presentation
        WAV/word-timed transcript, and the podium data channel. A fixture harness
        driving this orchestrator with `ctx=None` skips this entirely."""
        assert self.ctx is not None
        room = self.ctx.room

        @room.on("track_subscribed")
        def _on_track_subscribed(track: rtc.Track, *_: Any) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
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
            else:
                log.info("ignoring client msg type=%s in phase=%s", t, self.phase)
        except Exception:
            log.exception("error handling client msg: %s", msg)
            self.send({"type": "error", "message": f"failed to handle {t}"})

    # -- speech helper -----------------------------------------------------

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

    async def _track_speech(self, handle: Any, start_mono: float) -> None:
        await handle.wait_for_playout()
        played_s = round(time.monotonic() - start_mono, 3)
        self.store.log("agent_speech_end", speech_id=handle.id, interrupted=handle.interrupted, played_s=played_s)
        if handle.interrupted:
            onset = self._last_user_speech_start_mono or start_mono
            latency_ms = max(0, round((time.monotonic() - onset) * 1000))
            self.store.log("interrupt", speech_id=handle.id, latency_ms=latency_ms)

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self.store.log("phase", name=phase)
        self.send({"type": "phase", "name": phase, "budget_s": self.budget_s})

    # -- setup -----------------------------------------------------

    async def _handle_setup(self, msg: dict[str, Any]) -> None:
        topic = str(msg["topic"])
        level = str(msg.get("level", "intermediate"))
        budget_s = int(msg["budget_s"])
        deck = await deck_mod.generate_deck(topic=topic, level=level, budget_s=budget_s)
        await self._apply_deck(deck)

    async def _handle_deck_upload(self, msg: dict[str, Any]) -> None:
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
        self.send({"type": "deck", "deck": deck})
        topic = deck.get("topic") or "your topic"
        self._say(f"Let's talk about {topic}. You have {PREP_DURATION_S} seconds to prepare, then you're on.",
                   kind="ack")
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
                # only value the framework exposes.
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

    # -- analyze -----------------------------------------------------

    async def _enter_analyze(self, revision: int, wav_path: Path, text: str, words: list[dict[str, Any]]) -> None:
        self._set_phase("analyze")
        self._say("Got it, give me a moment.", kind="ack")

        rev_data = self.store.get_revision(revision)
        metrics = metrics_mod.compute(words=words, audio_path=str(wav_path), deck=self.deck or {},
                                       slide_events=rev_data["slide_events"], budget_s=self.budget_s)
        self.current_metrics = metrics
        self.store.update_revision(revision, metrics=metrics)
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
        return f"{imp['issue']} {judge_mod.rubric_text(imp['rubric_ref'])}"[:400]

    # -- coach -----------------------------------------------------

    async def _enter_coach(self, revision: int, judgment: dict[str, Any]) -> None:
        self._set_phase("coach")
        self._say(judgment.get("summary", ""), kind="feedback")

        self.improvement_queue = [ImprovementItem(data=imp, revision=revision)
                                   for imp in judgment.get("improvements", [])]
        self.drill_terms = list(judgment.get("terms_to_drill", []))
        await self._run_coach_queue()

    async def _run_coach_queue(self) -> None:
        idx = 0
        while idx < len(self.improvement_queue):
            item = self.improvement_queue[idx]
            if item.revision != self.store.current_revision:
                self.store.log("stale_dropped", tool="coach_queue", revision=item.revision,
                                current_revision=self.store.current_revision)
                idx += 1
                continue

            outcome = await self._deliver_improvement(item)
            if outcome == "heard":
                item.heard = True
                self.store.log("feedback_heard", improvement_id=item.data["id"])
                idx += 1
            elif outcome == "skip":
                idx += 1
            # "retry": re-offer the same item, idx unchanged

        await self._enter_drill()

    async def _deliver_improvement(self, item: ImprovementItem) -> str:
        imp = item.data
        handle = self._say(f"Here's one thing to work on: {imp['issue']}", kind="feedback")
        await handle.wait_for_playout()
        if handle.interrupted:
            return await self._resolve_interruption(item)

        if item.clips is None:
            clips = await self._render_clips(item)
            if clips is None:
                return "skip"  # fenced: revision went stale mid-render, already logged
            item.clips = clips

        for variant, label in _VARIANT_LABELS:
            ok = await self._play_variant(item, variant, label)
            if not ok:
                return await self._resolve_interruption(item)
        return "heard"

    async def _resolve_interruption(self, item: ImprovementItem) -> str:
        cmd, self._pending_command = self._pending_command, None
        if cmd == "skip":
            return "skip"
        if cmd == "slower":
            item.clips = None
            item.slower = True
            return "retry"
        if cmd == "why":
            h = self._say(judge_mod.rubric_text(item.data["rubric_ref"])[:180], kind="answer")
            await h.wait_for_playout()
            return "retry"
        return "retry"  # "again", or a plain barge-in with no recognised command

    async def _render_clips(self, item: ImprovementItem) -> dict[str, dict[str, Any]] | None:
        revision = item.revision
        start = time.monotonic()
        self.store.log("tool_start", tool="render", revision=revision)
        clips = await render_mod.render_variants(item.data, self.store.clips_dir, slower=item.slower,
                                                   model=RIME_MODEL, speaker=RIME_SPEAKER)
        ms = round((time.monotonic() - start) * 1000)
        self.store.log("tool_end", tool="render", revision=revision, ms=ms)

        if revision != self.store.current_revision:
            self.store.log("stale_dropped", tool="render", revision=revision, current_revision=self.store.current_revision)
            return None

        for variant, info in clips.items():
            self.send({
                "type": "clip", "improvement_id": item.data["id"], "variant": variant,
                "url": f"/sessions/{self.store.session_id}/clips/{Path(info['path']).name}",
            })
        return clips

    async def _play_variant(self, item: ImprovementItem, variant: str, label: str) -> bool:
        handle = self._say(label, kind="feedback")
        await handle.wait_for_playout()
        if handle.interrupted:
            return False
        clip = item.clips[variant]  # type: ignore[index]
        frames = audio_frames_from_file(clip["path"], sample_rate=SAMPLE_RATE, num_channels=1)
        clip_handle = self._say(_strip_pause_markup(clip["text"]), kind="clip", audio=frames, add_to_chat_ctx=False)
        await clip_handle.wait_for_playout()
        return not clip_handle.interrupted

    def trigger_voice_command(self, cmd: str) -> None:
        self._pending_command = cmd
        self.session.interrupt()

    async def _handle_command(self, name: str) -> None:
        if self.phase not in ("coach", "drill"):
            return
        if name not in ("skip", "again", "slower", "why"):
            self.send({"type": "error", "message": f"unknown command: {name}"})
            return
        self._pending_command = name
        self.session.interrupt()

    # -- drill -----------------------------------------------------

    async def _enter_drill(self) -> None:
        if not self.drill_terms:
            await self._enter_report()
            return
        self._set_phase("drill")
        for term in self.drill_terms:
            await self._drill_term(term)
        await self._enter_report()

    async def _drill_term(self, term: str) -> None:
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

    async def _say_and_wait(self, text: str, *, kind: str) -> Any:
        handle = self._say(text, kind=kind)
        await handle.wait_for_playout()
        return handle

    async def _await_drill_answer(self, timeout: float) -> str | None:
        self._drill_answer_future = asyncio.get_event_loop().create_future()
        try:
            return await asyncio.wait_for(self._drill_answer_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._drill_answer_future = None

    def deliver_drill_answer(self, text: str) -> None:
        if self._drill_answer_future is not None and not self._drill_answer_future.done():
            self._drill_answer_future.set_result(text)

    # -- report -----------------------------------------------------

    async def _enter_report(self) -> None:
        self._set_phase("report")
        heard = sum(1 for i in self.improvement_queue if i.heard)
        total = len(self.improvement_queue)
        self._say(f"That's a wrap. We worked through {heard} of {total} improvements together.", kind="feedback")
        self._say("Re-record any slide whenever you want another pass. Good luck out there.", kind="feedback")

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
    await session.start(room=ctx.room, agent=agent)
    await orchestrator.start()


if __name__ == "__main__":
    agents.cli.run_app(server)

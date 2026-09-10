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
import uuid
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

# The persona's spoken name -- fixed to "Podium" regardless of which Rime
# speaker id is configured; the `provider` message still reports the real
# speaker (see CONTRACTS.md §5) for the status bar.
AGENT_NAME = "Podium"

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
    f"Hi, I'm {AGENT_NAME}, your voice-first presentation coach. We'll rehearse your talk "
    "out loud, I'll listen, measure your delivery, and coach you back in your own words. "
    "To start, tell me your topic or upload your slides."
)

_PRESENT_END_RE = re.compile(r"\b(i'?m\s+done|that'?s\s+it|i'?m\s+finished)\b", re.I)
_NEXT_SLIDE_RE = re.compile(r"\bnext\s+slide\b", re.I)
_PREV_SLIDE_RE = re.compile(r"\b(previous\s+slide|last\s+slide|go\s+back\s+a\s+slide|back\s+one\s+slide)\b", re.I)
_PAUSE_MARKUP_RE = re.compile(r"<\d+>")
_READY_RE = re.compile(r"\b(ready|let'?s go|begin|start|go ahead|i'?m good)\b", re.I)
_NOT_READY_RE = re.compile(r"\b(?:not|never)\s+(?:\w+\s+){0,3}ready\b|n'?t\s+(?:\w+\s+){0,3}ready\b", re.I)

# v4: a spoken utterance during setup's topic-wait is treated as the topic
# itself unless it looks like a question/meta remark (route_utterance) --
# these two never require an offered command to be recognised.
_SETUP_QUESTION_RE = re.compile(
    r"^\s*(what|how|why|who|when|which|can you|could you|do you|tell me|help)\b|\?\s*$", re.I)
# Lead-ins are stripped iteratively (a real answer stacks them: "let's talk
# about, um, my topic is neural nets"), longest alternative first so
# "let's talk about" wins over the bare "about".
_SETUP_LEADIN_RE = re.compile(
    r"^\s*(?:okay|ok|so|um|uh|well|hey)?[,\s]*"
    r"(i want to talk about|i'?d like to talk about|i'?d like to present|"
    r"i want to present|i'?m presenting(?: on| about)?|i'?ll be talking about|"
    r"i'?m going to talk about|my talk is (?:on|about)|my topic is|"
    r"let'?s talk about|let us talk about|let'?s do|"
    r"generate (?:me )?slides (?:on|about|for)|make (?:me )?slides (?:on|about|for)|"
    r"(?:a )?presentation (?:on|about)|talk about|about)(?:\s+|$)", re.I)
# How long a too-short spoken topic is held before it is accepted on its own,
# and the fragments that are never a topic by themselves.
_TOPIC_MERGE_S = 2.5
_TOPIC_JUNK_RE = re.compile(
    r"^(let'?s|let us|so|um+|uh+|okay|ok|well|hey|hi|hello|and|but|i|it|the|a|an|my|"
    r"talk|about|topic|slides|please|yes|no|yeah|hmm|thanks)$", re.I)

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
    "more": re.compile(r"\b(more practice|again please|do (?:it|that|them) again|one more (?:round|time)|keep practicing)\b", re.I),
    "rerecord": re.compile(
        r"\b(try (?:it |that |the (?:talk|presentation|whole (?:thing|talk)) )?again|"
        r"re-?record|redo (?:a |that |the )?(?:slide|talk|presentation)|do it again)\b", re.I),
    "new_talk": re.compile(
        r"\b(new (?:talk|topic|presentation)|start over|different topic|"
        r"upload (?:a |another |my |the |own )?(?:new |own )?(?:deck|slides|pdf)|"
        r"no|nah|i'?m done(?!\s*coaching)|that'?s enough|i'?ve had enough|stop)\b", re.I),
    # v4 setup slot-filling (CONTRACTS §5 input gate) -- option names are the
    # command names in the `command` client message, matched the same
    # deterministic-first way as every other offered option.
    "level_beginner": re.compile(r"\b(beginner|basic|simple|easy|intro(?:ductory)?)\b", re.I),
    "level_intermediate": re.compile(r"\b(intermediate|medium|normal|standard)\b", re.I),
    "level_advanced": re.compile(r"\b(advanced|expert|hard|deep)\b", re.I),
    "len_60": re.compile(r"\b(60|sixty|one minute|a minute)\b", re.I),
    "len_90": re.compile(r"\b(90|ninety|minute and a half|a minute and a half)\b", re.I),
    "len_120": re.compile(r"\b(120|two minutes|one twenty|a hundred (?:and )?twenty)\b", re.I),
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
    "more": "practice more", "rerecord": "try the whole talk again", "new_talk": "start a new topic",
    "level_beginner": "say beginner", "level_intermediate": "say intermediate", "level_advanced": "say advanced",
    "len_60": "say sixty seconds", "len_90": "say ninety seconds", "len_120": "say two minutes",
}

# Setup slot answer option name -> the value stored on the orchestrator's
# `_setup` dict (CONTRACTS §5 `setup_state`).
_LEVEL_VALUES: dict[str, str] = {
    "level_beginner": "beginner", "level_intermediate": "intermediate", "level_advanced": "advanced",
}
_LENGTH_VALUES: dict[str, int] = {"len_60": 60, "len_90": 90, "len_120": 120}

_NUDGE_FALLBACK: dict[str, str] = {
    "setup": "Still there? Tell me a topic whenever you're ready, or upload your own deck.",
    "prep": "Still with me? No rush -- say ready when you are, or just start talking about your topic.",
    "coach": "Still with me? No rush -- pick one whenever you're ready.",
    "drill": "Still there? Just say the word whenever you're ready.",
    "report": "Take your time -- practice more, try the whole talk again, or start something new whenever you're ready.",
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

_WRAP_OPTIONS = [
    {"name": "more", "label": "More practice"},
    {"name": "rerecord", "label": "Try the whole talk again"},
    {"name": "new_talk", "label": "New topic / upload slides"},
]

_PRACTICE_PROMPTS = (
    "Your turn -- say it your way, in one breath.",
    "Now you: same point, your words.",
    "Last one -- give it a try in your own words.",
)

# Not a real option: signals the practice loop that `_handle_attempt` already
# stopped capture, spoke the verdict, and just wants a fresh capture started.
_ATTEMPT_HANDLED = "_attempt_handled"

# CONTRACTS §5 input gate (v4): these client->agent message types are never
# gated by `turn.allow` -- the countdown handshake and push-to-talk floor
# must work regardless of whose turn the agent thinks it is.
_ALWAYS_ALLOWED_TYPES = frozenset({"client_ready", "countdown_complete", "countdown_failed", "mic"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_pause_markup(text: str) -> str:
    return _PAUSE_MARKUP_RE.sub(" ", text).strip()


def _is_ready_phrase(text: str) -> bool:
    """True iff `text` contains a ready-to-begin phrase and is not a negation of
    it (e.g. "not ready", "I'm not quite ready" must never trigger the
    countdown -- CONTRACTS §5's ready/countdown handshake is opt-in only)."""
    if _NOT_READY_RE.search(text):
        return False
    return bool(_READY_RE.search(text))


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


def _extract_topic(text: str) -> str | None:
    """Strips a spoken topic of its conversational lead-in and trailing
    punctuation for deterministic setup capture (`route_utterance`); None if
    nothing usable remains (CONTRACTS §5 input gate v4)."""
    stripped = text.strip()
    for _ in range(3):
        peeled = _SETUP_LEADIN_RE.sub("", stripped, count=1).strip()
        if peeled == stripped or len(peeled) < 2:
            break
        stripped = peeled
    stripped = stripped.strip(" .!,;:\"'\u2014-")
    return stripped if len(stripped) >= 2 else None


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


_MARKDOWN_RE = re.compile(r"[*_`#]+")


def _strip_markdown(text: str) -> str:
    """Plain-text a coach LLM reply for TTS: strips markdown emphasis/heading
    markers the persona is told never to use but sometimes slips in anyway."""
    return re.sub(r"\s{2,}", " ", _MARKDOWN_RE.sub("", text or "")).strip()


_SCORE_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z_ -]{1,30}?)\s*[:\-]\s*(.+)$")
_SCORE_TITLES: dict[str, str] = {
    "delivery": "Delivery", "clarity": "Clarity", "structure": "Structure",
    "slide_connection": "Slide connection", "pronunciation": "Pronunciation",
}


def _build_score_lines(raw: str, scores: dict[str, int]) -> dict[str, str]:
    """Parse the coach LLM's `key: line` per-score narration (one short spoken
    line per CONTRACTS.md §3 score key) into `{key: line}`. Any key the LLM
    didn't return in valid `key: line` form -- a malformed reply, a missing
    category, empty output -- falls back to a scripted `"<Title>: <score> out
    of 5."` line, so the score walkthrough always has all N lines."""
    parsed: dict[str, str] = {}
    for raw_line in (raw or "").splitlines():
        line = raw_line.strip().lstrip("-*\u2022 ").strip()
        if not line:
            continue
        m = _SCORE_LINE_RE.match(line)
        if not m:
            continue
        key = re.sub(r"[\s-]+", "_", m.group(1).strip().lower())
        text = _strip_markdown(m.group(2)).strip()
        if key in scores and key not in parsed and text:
            parsed[key] = text
    out: dict[str, str] = {}
    for key, score in scores.items():
        if key in parsed:
            out[key] = parsed[key]
        else:
            title = _SCORE_TITLES.get(key, key.replace("_", " ").title())
            out[key] = f"{title}: {score} out of 5."
    return out


async def _frame_iter(frames: list[rtc.AudioFrame]) -> Any:
    for f in frames:
        yield f


@dataclass
class PreparedLine:
    """A spoken line whose audio has already been synthesized (or, on a TTS
    failure, an empty-frames line that falls back to the session's own live
    TTS -- see `PodiumOrchestrator._play`). `from_llm` gates whether `_play`
    logs an `agent_speech_text` timeline event, mirroring `_speak_llm`."""
    text: str
    frames: list[rtc.AudioFrame] = field(default_factory=list)
    from_llm: bool = False


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
    # Which trio slots ("v0", "v2", "v3") have played through uninterrupted
    # at least once. feedback_heard only fires once all three are present.
    completed: set[str] = field(default_factory=set)
    # Practice attempts already recorded for this item, across every pass
    # (including "more" wrap reuse) -- the next attempt's clip filename number
    # so a repeat pass never overwrites an earlier take.
    attempt_count: int = 0
    # v2: cached PreparedLine for the intro + the three trio slot labels
    # (see PodiumOrchestrator._prepare_item_lines) -- text never changes on a
    # "slower"/"again" clip re-render, so this is computed once per item and
    # reused; only `clips` gets reset by those.
    spoken: dict[str, "PreparedLine"] | None = None


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
    async def generate_slides(self, context: RunContext, topic: str) -> str:
        """Record the user's presentation topic during setup, when they name one
        in a turn the deterministic router did not already capture on its own
        (for example, combined with a question). Setup phase only -- level and
        length are asked separately by the orchestrator, never by this tool.

        Args:
            topic: the presentation topic the user described.
        """
        orch = self._orch
        if orch.phase != "setup" or orch.deck is not None:
            return "We already have a deck ready -- no need to generate another one right now."
        orch._fill_setup_slot("topic", topic, "llm")
        asyncio.create_task(orch._run_setup_flow("llm"))
        return (f"Topic recorded: {topic}. I am handling the level and length questions myself "
                "-- acknowledge in at most one short sentence and ask nothing.")

    @function_tool()
    async def start_presenting(self, context: RunContext) -> str:
        """Begin the countdown and presentation right away. Prep phase only."""
        orch = self._orch
        if orch.phase != "prep":
            return "We're not in the prep window right now."
        if orch._countdown_pending:
            return "The countdown's already starting -- one sec."
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

    def __init__(self, session: AgentSession, store: Store, *, ctx: agents.JobContext | None = None,
                 tts_rest: Any = None) -> None:
        self.session = session
        self.store = store
        self.ctx = ctx
        self.agent: PodiumAgent | None = None  # set by build_agent_and_session right after construction
        # REST-mode Rime TTS instance (chunked, non-streaming) used to pre-synthesize
        # a line's audio before its matching UI message is sent -- see `_prepare`.
        self._tts_rest = tts_rest

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

        # -- v2: countdown handshake (CONTRACTS §5/§6) + topic-reset fencing ---
        self._countdown_cache: dict[str, dict[str, Any]] | None = None
        self._countdown_task: asyncio.Task | None = None  # prewarm render, started at prep entry
        self._countdown_id: str | None = None              # id of the in-flight countdown, else None
        self._countdown_pending: bool = False
        self._countdown_future: asyncio.Future[bool] | None = None
        self._countdown_ready = asyncio.Event()  # set once the in-flight countdown's clips are ready
        # Bumped by new_talk so a judge/render/attempt callback still in flight
        # for the old topic fences itself even before a new revision exists to
        # fence on (store.current_revision alone doesn't move until the next
        # presentation starts).
        self._epoch: int = 0
        self._progress_recorded_for: set[int] = set()  # revisions already credited (wrap "more" safety)
        # -- speech-preparation layer (contract §B: message gated behind synthesized audio) --
        self._attention: str = "listening"
        self._line_cache: dict[str, PreparedLine] = {}
        self._drill_announced: bool = False
        self._setup_wait_task: asyncio.Task[None] | None = None
        # Per-talk judgment history ({"revision","scores"} per revision, arrival
        # order); previous = the entry before the one just appended, or None for
        # the talk's first judgment. Reset by new_talk (a new talk starts clean).
        self._judgment_history: list[dict[str, Any]] = []

        # -- v4: input gate (CONTRACTS §5 `turn`) ---------------------------
        self._turn_seq: int = 0
        self._turn_last: tuple[str, str | None, tuple[str, ...], str] | None = None
        # How many nested spoken lines are currently in flight -- see
        # `_track_speech`, the single place every `_say`/`_speak_llm` call
        # funnels through, including fire-and-forget `_play` sites.
        self._speaking_depth: int = 0
        # True while deterministic setup slot-filling is awaiting
        # `deck_mod.generate_deck` -- a busy (owner="agent") window with no
        # active speech to key `_speaking_depth` off of.
        self._setup_generating: bool = False

        # -- v4: push-to-talk floor (CONTRACTS §5 `mic`) ---------------------
        self._floor_user: bool = False
        self._floor_open = asyncio.Event()
        self._floor_open.set()  # floor starts free

        # -- v4: deterministic setup slot-filling (CONTRACTS §5) -------------
        self._setup: dict[str, Any] = {"topic": None, "level": None, "budget_s": None}
        self._setup_task: asyncio.Task | None = None
        self._setup_source: str = "voice"
        # A spoken topic can arrive split across two STT turns; `_capture_topic`
        # holds the short first half here rather than committing it.
        self._topic_fragment: str = ""
        self._topic_fragment_mono: float = 0.0
        self._topic_flush_task: asyncio.Task | None = None
        self._setup_task_kickoff: asyncio.Task | None = None

        # -- v4: slide-advance debounce (issue: slides self-advancing) -------
        self._last_advance_mono: float = 0.0
        self._last_advance_transcript: tuple[str, float] = ("", 0.0)

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
                return
            spoken = (self._advance_slide if _NEXT_SLIDE_RE.search(low)
                      else self._rewind_slide if _PREV_SLIDE_RE.search(low) else None)
            if spoken is None:
                return
            # Deepgram can deliver the same final transcript twice; a repeat of
            # the identical text within 3 s is the STT echoing, not the
            # presenter asking for another slide.
            now = time.monotonic()
            last_text, last_mono = self._last_advance_transcript
            if ev.transcript == last_text and now - last_mono < 3.0:
                self.store.log("gate_blocked", type="slide_voice", owner="user", expect="present")
                return
            self._last_advance_transcript = (ev.transcript, now)
            spoken()

        @self.session.on("user_state_changed")
        def _on_user_state(ev: agents.UserStateChangedEvent) -> None:
            if ev.new_state == "speaking" and ev.old_state != "speaking":
                self.store.log("user_speech_start")
                self._last_user_speech_start_mono = time.monotonic()
            elif ev.old_state == "speaking" and ev.new_state != "speaking":
                self.store.log("user_speech_end")
            if (ev.new_state == "away" and not self._countdown_pending and not self._floor_user
                    and self.phase in ("setup", "prep", "coach", "drill", "report")
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

        @room.on("disconnected")
        def _on_disconnected(*_: Any) -> None:
            # A closed tab/lost connection must not leave a countdown ack wait,
            # a render/judge call, or a prep timer running forever in the
            # background -- cancel every pending flow.
            asyncio.create_task(self._cancel_stale_tasks(exclude_current=False))

    async def start(self) -> None:
        self.store.set_config(
            rime_model=RIME_MODEL, rime_speaker=RIME_SPEAKER, rime_lang=RIME_LANG,
            sample_rate=SAMPLE_RATE, transport="websocket /ws3", stt="deepgram/nova-3",
            coach_llm=COACH_MODEL, judge_llm=JUDGE_MODEL, provider="rime",
        )
        self.store.log("provider", name="rime", model=RIME_MODEL, speaker=RIME_SPEAKER)
        self.send({"type": "provider", "name": "rime", "model": RIME_MODEL, "speaker": RIME_SPEAKER})
        self._refresh_turn(force=True)

        self._set_phase("setup")
        self._set_attention("thinking")
        greeting_task = asyncio.create_task(self._prepare(GREETING_TEXT))
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
            greeting_task.cancel()
            return  # a client (or harness) already moved on without the greeting
        greeting = await greeting_task
        handle = await self._play(greeting, kind="ack", before=lambda: self._send_coach("greeting"))
        self._setup_wait_task = asyncio.create_task(self._await_setup_input(handle))

    async def _await_setup_input(self, handle: Any) -> None:
        """Flip to `attention: waiting` only once the setup prompt has actually
        been heard -- the client reveals the topic/deck form on that message,
        and revealing it mid-sentence undercuts the voice-first pacing. A prompt
        cut short by an early topic/deck never flips (the flow already moved)."""
        try:
            await handle.wait_for_playout()
        except Exception:
            return
        if self.phase == "setup" and self.deck is None and not handle.interrupted:
            self._set_attention("waiting")

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
        if t not in _ALWAYS_ALLOWED_TYPES:
            owner, expect, allow, _hint = self._gate_state()
            if t not in allow:
                self.store.log("gate_blocked", type=t, owner=owner, expect=expect)
                self._refresh_turn(force=True)
                return
        try:
            if t == "setup" and self.phase == "setup":
                await self._handle_setup(msg)
            elif t == "deck_upload" and self.phase == "setup":
                await self._handle_deck_upload(msg)
            elif t == "ready" and self.phase == "prep":
                if self._countdown_pending:
                    # A duplicate "ready" while the countdown handshake is
                    # already in flight is ignored, never cancel-and-restart.
                    self.store.log("countdown_ready_ignored", id=self._countdown_id)
                else:
                    self._start_present_flow()
            elif t == "countdown_complete":
                self._handle_countdown_ack(str(msg.get("id", "")), True)
            elif t == "countdown_failed":
                self._handle_countdown_ack(str(msg.get("id", "")), False)
            elif t == "slide_next" and self.phase == "present":
                self._advance_slide()
            elif t == "slide_prev" and self.phase == "present":
                self._rewind_slide()
            elif t == "present_end" and self.phase == "present":
                self._trigger_present_end("client")
            elif t == "rerecord":
                await self._handle_rerecord(msg)
            elif t == "command":
                await self._handle_command(str(msg.get("name", "")))
            elif t == "client_ready":
                self._client_ready.set()
            elif t == "mic":
                self._handle_mic(bool(msg.get("open", False)))
            else:
                log.info("ignoring client msg type=%s in phase=%s", t, self.phase)
        except Exception:
            log.exception("error handling client msg: %s", msg)
            self.send({"type": "error", "message": f"failed to handle {t}"})
        finally:
            self._refresh_turn()

    def _handle_mic(self, open_: bool) -> None:
        """CONTRACTS §5 push-to-talk floor. Interrupting on open must never raise
        when nothing is currently speaking -- the mic can open at any time."""
        self._floor_user = open_
        if open_:
            self._floor_open.clear()
            try:
                self.session.interrupt()
            except Exception:
                log.debug("session.interrupt() on mic open was a no-op")
        else:
            self._floor_open.set()
        self.store.log("mic", open=open_)
        self._refresh_turn()

    def _handle_countdown_ack(self, cid: str, ok: bool) -> None:
        """CONTRACTS §5/§6: an ack only matters for the currently in-flight
        countdown id -- a duplicate or a stale id (e.g. left over from before a
        rerecord/new_talk moved on) is dropped and never starts a recording."""
        if not self._countdown_pending or cid != self._countdown_id:
            self.store.log("countdown_stale_ack", id=cid, ok=ok, current=self._countdown_id)
            return
        fut = self._countdown_future
        if fut is not None and not fut.done():
            fut.set_result(ok)

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
        await self._await_floor()
        handle = self._say(text, kind=kind)
        await handle.wait_for_playout()
        return handle

    async def _track_speech(self, handle: Any, start_mono: float) -> None:
        self._speaking_depth += 1
        self._refresh_turn()
        try:
            await handle.wait_for_playout()
        finally:
            self._speaking_depth -= 1
            self._refresh_turn()
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

    async def _await_floor(self) -> None:
        """CONTRACTS §5 push-to-talk: while the user holds the floor (`mic`
        `open:true`), the agent starts no new line -- waits for `_floor_open`
        (set on `mic` `open:false`) before proceeding. Gives up after 30 s so a
        client that forgets to release the floor can never permanently mute
        the coach."""
        if not self._floor_user:
            return
        try:
            await asyncio.wait_for(self._floor_open.wait(), timeout=30)
        except asyncio.TimeoutError:
            self.store.log("tool_error", tool="floor_wait", error="mic held open past 30s, proceeding anyway")

    async def _speak_llm(self, instructions: str, fallback: str, *, kind: str) -> Any:
        """Let the coach LLM phrase `instructions` (which must carry every fact it
        is allowed to use). Falls back to a scripted `fallback` line, spoken via
        `_say`, if the LLM call errored, produced no assistant reply, or was cut
        off before it could finish.

        The request runs in a single-turn context (persona + request) rather than
        the live conversation: with the whole presentation transcript sitting in
        history as a user turn, the model answered *that* and ignored the facts."""
        await self._await_floor()
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

    # -- speech preparation (contract §B: gate a UI message behind its audio) --

    def _set_attention(self, state: str) -> None:
        """CONTRACTS §5 `attention`. Deduplicates repeats -- only a genuine
        state change reaches the wire and the timeline."""
        if self._attention == state:
            return
        self._attention = state
        self.store.log("attention", state=state)
        self.send({"type": "attention", "state": state})
        self._refresh_turn()

    async def _prepare(self, text: str) -> PreparedLine:
        """Synthesizes `text` via the REST-mode Rime instance ahead of time, so
        `_play` can hand `session.say` already-decoded frames instead of
        waiting on TTS after the matching UI message goes out. A synthesis
        failure degrades to an empty-frames line: `_play` then falls back to
        the session's own (streaming) TTS rather than losing the line."""
        if self._tts_rest is None:
            return PreparedLine(text, [])
        try:
            frames = [ev.frame async for ev in self._tts_rest.synthesize(text)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.log("tool_error", tool="tts_prepare", error=str(exc)[:200])
            return PreparedLine(text, [])
        return PreparedLine(text, frames)

    async def _prepare_many(self, texts: list[str]) -> list[PreparedLine]:
        return list(await asyncio.gather(*(self._prepare(t) for t in texts)))

    async def _prepare_cached(self, text: str) -> PreparedLine:
        """Memoized `_prepare`, for the handful of fixed strings spoken many
        times a session (practice prompts, "again or next", drill framing)."""
        cached = self._line_cache.get(text)
        if cached is not None:
            return cached
        line = await self._prepare(text)
        self._line_cache[text] = line
        return line

    async def _llm_text(self, instructions: str, *, timeout: float = 12.0) -> str:
        """One-shot coach-LLM call returning raw text (no TTS, no speaking) --
        the building block `_prepare_llm` and the score narration use to get a
        phrased line *before* deciding what to say and to whom to announce it."""
        rule = (" Use only the numbers given here; do not invent any. Reply with "
                 "the spoken line(s) only, no preamble, no markdown.")
        ctx = ChatContext.empty()
        ctx.add_message(role="system", content=COACH_INSTRUCTIONS)
        ctx.add_message(role="user", content=instructions + rule)
        llm = coach_llm()
        try:
            async def _collect() -> str:
                chunks: list[str] = []
                async with llm.chat(chat_ctx=ctx) as stream:
                    async for chunk in stream:
                        if chunk.delta and chunk.delta.content:
                            chunks.append(chunk.delta.content)
                return "".join(chunks)
            return await asyncio.wait_for(_collect(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.log("tool_error", tool="llm_prepare", error=str(exc)[:200])
            return ""
        finally:
            await llm.aclose()

    async def _prepare_llm(self, instructions: str, fallback: str) -> PreparedLine:
        """Like `_speak_llm` but returns the prepared line instead of speaking
        it, so the caller can send the matching UI message first. Falls back to
        a `_prepare`d `fallback` on an empty/failed LLM call, exactly like
        `_speak_llm` falls back to `_say_and_wait(fallback, ...)`."""
        text = _strip_markdown(await self._llm_text(instructions))
        if not text:
            line = await self._prepare(fallback)
            line.from_llm = False
            return line
        line = await self._prepare(text)
        line.from_llm = True
        return line

    async def _play(self, line: PreparedLine, *, kind: str, before: Any = None,
                     allow_interruptions: bool = True, add_to_chat_ctx: bool = True) -> Any:
        """Plays an already-`_prepare`d line. `before`, if given, runs
        synchronously immediately before `session.say` -- this is where a
        caller sends the message(s) whose audio is now ready (CONTRACTS §5B).
        Push-to-talk (CONTRACTS §5): waits for the floor to free before
        `before`/`session.say` run at all, so neither a UI message nor audio
        ever starts while the user is mid-sentence -- covers every caller,
        including fire-and-forget ones, since they all funnel through here."""
        await self._await_floor()
        if before is not None:
            before()
        audio = _frame_iter(line.frames) if line.frames else None
        handle = self._say(line.text, kind=kind, allow_interruptions=allow_interruptions,
                            add_to_chat_ctx=add_to_chat_ctx, audio=audio)
        if line.from_llm:
            self.store.log("agent_speech_text", speech_id=handle.id, text=line.text[:400])
        return handle

    async def _play_and_wait(self, line: PreparedLine, *, kind: str, before: Any = None,
                              allow_interruptions: bool = True, add_to_chat_ctx: bool = True) -> Any:
        handle = await self._play(line, kind=kind, before=before, allow_interruptions=allow_interruptions,
                                   add_to_chat_ctx=add_to_chat_ctx)
        await handle.wait_for_playout()
        return handle

    def _set_phase(self, phase: str, *, defer_send: bool = False) -> None:
        """`defer_send=True` updates local state/log/graph but withholds the
        `phase` wire message -- used only for phase in (coach, report), which
        CONTRACTS §5B gates behind their first spoken line; the caller sends it
        itself via `_send_phase()` inside that line's `before=` callback."""
        self.phase = phase
        self._nudges_this_wait = 0
        self.store.log("phase", name=phase)
        if not defer_send:
            self._send_phase()
        self.store.set_graph(self.graph.to_json())
        self._refresh_turn()

    def _send_phase(self) -> None:
        self.send({"type": "phase", "name": self.phase, "budget_s": self.budget_s})

    def _send_coach(self, stage: str, *, options: list[dict[str, str]] | None = None, **extra: Any) -> None:
        options = options if options is not None else []
        self._offered = [o["name"] for o in options]
        log_fields = {k: v for k, v in extra.items() if k in ("improvement_id", "attempt")}
        self.store.log("coach_stage", stage=stage, **log_fields)
        self.send({"type": "coach", "stage": stage, "options": options, **extra})
        self._refresh_turn()

    def _send_feedback_stage(self, improvement_id: str, stage: str, *, text: str | None = None,
                              role: str = "coach", markup: str | None = None, available: bool = True) -> None:
        """v2/v4: CONTRACTS §5 `feedback` message -- cues the client exactly which
        step of an item (intro/v0/v2/v3/alt/prompt/verdict/resume) is playing.
        `role` distinguishes the presenter's OWN recorded clip ("user", shown
        red) from every line the agent speaks ("coach", shown green); `markup`
        carries pause-marked text (v3/prompt) for the client to display;
        `available` is only ever false for the v0 beat when the slice failed."""
        self.store.log("feedback_stage", improvement_id=improvement_id, stage=stage)
        item: dict[str, Any] = {"id": improvement_id, "stage": stage, "role": role,
                                 "markup": markup, "available": available}
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

    # -- input gate (CONTRACTS §5 `turn`, v4) --------------------------------

    def _logical_wait(self) -> tuple[str | None, list[str], str]:
        """Pure function of phase/`_expect`/`_capture_active` -> (expect,
        allow-if-the-floor-is-the-user's, hint) -- independent of whether the
        agent is currently busy speaking/rendering/counting down/analyzing.
        `_gate_state` keeps this as the "current" expectation while busy
        (forcing `allow` to `[]`), per CONTRACTS §5's allow table."""
        if self.phase == "prep":
            return "ready", ["ready", "command"], "say ready when you want to begin, or take a moment to prepare"
        if self.phase == "setup" and self.deck is None:
            if self._expect == "level":
                return "level", ["command", "setup", "deck_upload"], "say beginner, intermediate, or advanced"
            if self._expect == "length":
                return "length", ["command", "setup", "deck_upload"], "say sixty, ninety, or two minutes"
            return ("topic", ["setup", "deck_upload", "command"],
                    "say a topic to generate slides, or upload your own deck")
        if self.phase == "drill":
            return "drill", ["command"], "say the word when you're ready"
        if self.phase in ("coach", "report"):
            if self.phase == "coach" and self._capture_active:
                return "attempt", ["command"], "say it your way, or pick an option"
            return "choice", ["command", "rerecord"], "pick one, or say what you'd like"
        return None, [], "one moment"

    def _gate_state(self) -> tuple[str, str | None, list[str], str]:
        """CONTRACTS §5 input gate (v4): `(owner, expect, allow, hint)`. During
        `present` the floor is always the user's -- push-to-talk/present's
        continuous mic means a brief agent cue must never lock the controls.
        Everywhere else, `owner` is "agent" (allow `[]`) whenever the agent is
        speaking, rendering the setup deck, mid-countdown, or analyzing, and
        "user" only in the specific windows the allow table recognises as
        actually waiting on the user (an armed intent/drill future, or the
        phase's own no-future waiting states)."""
        if self.phase == "present":
            return ("user", "present", ["slide_next", "slide_prev", "present_end"],
                    "say \u201cnext slide\u201d, \u201cprevious slide\u201d or \u201cI'm done\u201d, or use the on-screen controls")
        expect, allow, hint = self._logical_wait()
        # `attention == "thinking"` is exactly CONTRACTS §5's "LLM/TTS prep in
        # progress, agent silent": the answer to the user's last turn is still
        # being built, so the floor is not theirs yet even though nothing is
        # audible. Without it the gate opens in the silent gap before the
        # greeting/ask plays, which is the window the user could jump.
        busy = (self._speaking_depth > 0 or self._countdown_pending
                or self._setup_generating or self.phase == "analyze"
                or self._attention == "thinking")
        waiting_for_input = not busy and (
            self.phase == "prep"
            # Setup only waits on the user in two windows: before a topic
            # exists, and while a level/length ask is outstanding. With every
            # slot filled the flow is working, so the floor stays the agent's
            # even in the gap between its "on it" line and the deck render.
            or (self.phase == "setup" and self.deck is None
                and (self._setup.get("topic") is None or self._expect in ("level", "length")))
            or (self.phase == "drill" and self._drill_answer_future is not None)
            or (self.phase in ("coach", "report") and self._intent_future is not None)
        )
        if waiting_for_input:
            return "user", expect, allow, hint
        return "agent", expect, [], hint

    def _refresh_turn(self, *, force: bool = False) -> None:
        """Recomputes `_gate_state()` and, on a genuine change (or `force`),
        bumps `seq` and sends the new `turn` -- called on every state
        transition that can move the gate (see call sites) so a resend is
        never missed; idempotent otherwise, so calling it liberally is safe."""
        owner, expect, allow, hint = self._gate_state()
        state = (owner, expect, tuple(allow), hint)
        if not force and state == self._turn_last:
            return
        self._turn_last = state
        self._turn_seq += 1
        self.store.log("turn", owner=owner, expect=expect, seq=self._turn_seq)
        self.send({"type": "turn", "seq": self._turn_seq, "owner": owner, "expect": expect,
                   "allow": allow, "hint": hint})

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
        if self._countdown_pending:
            # Nothing spoken during the countdown handshake gets a reply, a
            # nudge, or routed anywhere -- the browser owns this window.
            return True
        if self.phase == "drill":
            self.deliver_drill_answer(text)
            return True
        if self.phase == "prep" and len(text.split()) <= 8 and _is_ready_phrase(text):
            self._start_present_flow()
            return True
        if self._expect in ("proceed", "menu", "wrap"):
            name = _match_intent(text, self._offered)
            if name is not None:
                self._deliver_intent(name, "regex")
                return True
            if (self._expect == "menu" and self._capture_active and self._current_item is not None
                    and practice_mod.looks_like_attempt(text, self._current_item.data)):
                self._attempt_task = asyncio.create_task(self._handle_attempt(text))
                return True
        if self._expect in ("level", "length"):
            # v4: setup slot answers -- same deterministic offered-name match
            # as every other choice; unrecognised falls through to the LLM.
            name = _match_intent(text, self._offered)
            if name is not None:
                self._deliver_intent(name, "regex")
                return True
        if (self.phase == "setup" and self.deck is None and self._expect in (None, "topic")
                and _match_intent(text, self._offered) is None
                and not _is_ready_phrase(text) and not _SETUP_QUESTION_RE.search(text)):
            # v4: deterministic topic capture (CONTRACTS §5 input gate) -- a
            # plain remark during the topic-wait IS the topic; the LLM never
            # gets a setup turn for it, so it can never ask its own improvised
            # level/length question out of order with the deterministic flow.
            return self._capture_topic(text)
        return False

    def _capture_topic(self, text: str) -> bool:
        """Deterministic topic capture. STT splits a spoken sentence into more
        than one final turn often enough to matter ("Let's" / "talk about how
        attention works"), and committing the first fragment as the topic is
        exactly the "it never picked up my topic" failure. A fragment that is
        still shorter than two words is held for `_TOPIC_MERGE_S` and merged
        with whatever comes next; if nothing does, `_flush_topic_fragment`
        accepts it anyway unless it is pure lead-in/filler. Always returns
        True: a fragment must not reach the LLM, or it would answer it."""
        raw = text.strip()
        if self._topic_fragment and time.monotonic() - self._topic_fragment_mono < _TOPIC_MERGE_S:
            raw = f"{self._topic_fragment} {raw}"
        if self._topic_flush_task is not None and not self._topic_flush_task.done():
            self._topic_flush_task.cancel()
            self._topic_flush_task = None
        topic = _extract_topic(raw)
        if topic is not None and len(topic.split()) >= 2:
            self._topic_fragment = ""
            self._commit_topic(topic)
            return True
        self._topic_fragment = raw
        self._topic_fragment_mono = time.monotonic()
        self._topic_flush_task = asyncio.create_task(self._flush_topic_fragment(raw))
        return True

    def _commit_topic(self, topic: str) -> None:
        self._fill_setup_slot("topic", topic, "voice")
        self._setup_task_kickoff = asyncio.create_task(self._run_setup_flow("voice"))

    async def _flush_topic_fragment(self, raw: str) -> None:
        """A one-word topic ("transformers") is legitimate, so a held fragment
        is committed after `_TOPIC_MERGE_S` unless it is only a lead-in or a
        filler word, in which case it is dropped and the coach keeps waiting."""
        try:
            await asyncio.sleep(_TOPIC_MERGE_S)
        except asyncio.CancelledError:
            return
        if self._topic_fragment != raw or self.phase != "setup" or self.deck is not None:
            return
        self._topic_fragment = ""
        topic = _extract_topic(raw)
        if topic and len(topic) >= 3 and not _TOPIC_JUNK_RE.match(topic):
            self._commit_topic(topic)
        else:
            self.store.log("setup_slot", slot="topic", value=None, source="voice")

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
        self._set_attention("waiting")
        try:
            while True:
                # A command that arrived while Thunder was mid-speech parked itself
                # in _pending_command (see _deliver_intent); honour it before waiting.
                parked, self._pending_command = self._pending_command, None
                if parked in self._offered:
                    return parked
                self._intent_future = asyncio.get_event_loop().create_future()
                self._refresh_turn()
                try:
                    return await asyncio.wait_for(self._intent_future, timeout=timeout_s)
                except asyncio.TimeoutError:
                    if self._nudges_this_wait >= MAX_NUDGES_PER_WAIT:
                        return None
                    self._nudges_this_wait += 1
                    await self._nudge(reason="timeout")
                finally:
                    self._intent_future = None
                    self._refresh_turn()
        finally:
            self._set_attention("listening")

    def _is_awaiting_input(self) -> bool:
        if self._countdown_pending:
            return False
        if self.phase in ("setup", "prep"):
            return True
        if self.phase in ("coach", "report"):
            return self._intent_future is not None
        if self.phase == "drill":
            return self._drill_answer_future is not None
        return False

    async def _nudge(self, *, reason: str) -> None:
        if self._floor_user:
            return  # push-to-talk: the user is mid-sentence, not idle -- never talk over them
        stage = self.phase
        self.store.log("nudge", reason=reason, stage=stage)
        if self._offered:
            hint = " or ".join(_OPTION_LABELS.get(n, n) for n in self._offered)
        elif stage == "setup" and self._expect == "level":
            hint = "say beginner, intermediate, or advanced"
        elif stage == "setup" and self._expect == "length":
            hint = "say sixty, ninety, or two minutes"
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
        if stage == "setup" and self._expect in ("level", "length"):
            fallback = f"Still there? {hint[0].upper()}{hint[1:]}?"
        else:
            fallback = _NUDGE_FALLBACK.get(stage, "Still with me? Take your time.")
        self._set_attention("thinking")
        handle = await self._speak_llm(instructions, fallback, kind="ack")
        self._set_attention("waiting")
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
        elif self.phase == "setup" and self._expect == "level":
            parts.append("The user can say beginner, intermediate, or advanced.")
        elif self.phase == "setup" and self._expect == "length":
            parts.append("The user can say sixty, ninety, or two minutes.")
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

    def _fill_setup_slot(self, name: str, value: Any, source: str) -> None:
        """Records a newly-known setup slot (CONTRACTS §5 `setup_slot` /
        `setup_state`) -- idempotent: repeating an already-known value never
        logs a duplicate slot-filled event or resends state."""
        if self._setup.get(name) == value:
            return
        self._setup[name] = value
        self.store.log("setup_slot", slot=name, value=value, source=source)
        self._send_setup_state(source)

    def _send_setup_state(self, source: str) -> None:
        missing = [k for k in ("topic", "level", "budget_s") if self._setup.get(k) is None]
        self.send({"type": "setup_state", "topic": self._setup["topic"], "level": self._setup["level"],
                   "budget_s": self._setup["budget_s"], "source": source, "missing": missing})

    async def _handle_setup(self, msg: dict[str, Any]) -> None:
        """Client form submission: fills every slot at once (the form only
        submits once complete) and hands off to the same deterministic flow
        voice slot-filling uses, so the deck is always generated from exactly
        one place (CONTRACTS §5 input gate)."""
        self._maybe_attach_progress(msg)
        topic = str(msg.get("topic", "")).strip()
        if not topic:
            return  # the client also validates; an empty submission is a no-op
        self._fill_setup_slot("topic", topic, "client")
        self._fill_setup_slot("level", str(msg.get("level", "intermediate")), "client")
        self._fill_setup_slot("budget_s", int(msg.get("budget_s", 60)), "client")
        await self._run_setup_flow("client")

    async def _handle_deck_upload(self, msg: dict[str, Any]) -> None:
        self._maybe_attach_progress(msg)
        if self._setup_task is not None and not self._setup_task.done():
            # A deck upload always wins over an in-flight voice/LLM ask.
            self._setup_task.cancel()
        self._set_attention("thinking")
        # CONTRACTS §5's deck_upload carries only `slides` -- no budget_s/topic/level.
        # Interpreted here: accept optional overrides if a client sends them, else fall
        # back to a sensible default budget and a topic derived from the first slide.
        slides = msg["slides"]
        budget_s = int(msg.get("budget_s", 90))
        deck = deck_mod.normalise_deck(slides, budget_s, topic=str(msg.get("topic", "")))
        if not deck.get("topic"):
            deck["topic"] = (slides[0].get("title") if slides else None) or "your uploaded deck"
        self._fill_setup_slot("topic", deck["topic"], "client")
        self._fill_setup_slot("budget_s", budget_s, "client")
        self._send_setup_state("client")
        if not (self.phase == "setup" and self.deck is None):
            log.debug("deck_upload dropped: phase=%s deck_set=%s", self.phase, self.deck is not None)
            return
        await self._apply_deck(deck)

    async def _run_setup_flow(self, source: str) -> None:
        """Single-flight entry point: a second call while the flow is already
        running (e.g. the LLM tool firing right after voice already started
        it) just refreshes state instead of racing a second ask/generate."""
        if self._setup_task is not None and not self._setup_task.done():
            self._send_setup_state(source)
            return
        self._setup_task = asyncio.create_task(self._run_setup_flow_body(source))

    async def _run_setup_flow_body(self, source: str) -> None:
        self._setup_source = source
        self._send_setup_state(source)
        if self._setup["topic"] is None:
            return  # nothing to ask yet -- the topic itself is what starts this flow
        if source == "llm":
            # The tool call's own return value triggers the framework's
            # automatic post-tool reply; never let the level ask race it.
            await self._wait_for_default_reply()
        if not (self.phase == "setup" and self.deck is None):
            log.debug("setup_flow dropped: phase=%s deck_set=%s", self.phase, self.deck is not None)
            return
        if self._setup["level"] is None:
            ok = await self._ask_setup_slot(
                "level", "level", "setup_level",
                [{"name": "level_beginner", "label": "Beginner"},
                 {"name": "level_intermediate", "label": "Intermediate"},
                 {"name": "level_advanced", "label": "Advanced"}],
                question="What level should the talk be pitched at -- beginner, intermediate, or advanced?",
                default_value="intermediate", default_line="I'll go with intermediate.",
            )
            if not ok:
                return
        if self._setup["budget_s"] is None:
            ok = await self._ask_setup_slot(
                "budget_s", "length", "setup_length",
                [{"name": "len_60", "label": "60 seconds"},
                 {"name": "len_90", "label": "90 seconds"},
                 {"name": "len_120", "label": "2 minutes"}],
                question="How long should your talk run -- sixty, ninety, or a hundred twenty seconds?",
                default_value=60, default_line="I'll go with sixty seconds.",
            )
            if not ok:
                return
        self._setup_generating = True
        self._refresh_turn()
        try:
            deck = await deck_mod.generate_deck(topic=self._setup["topic"], level=self._setup["level"],
                                                 budget_s=self._setup["budget_s"])
        finally:
            self._setup_generating = False
            self._refresh_turn()
        if not (self.phase == "setup" and self.deck is None):
            log.debug("setup_flow dropped after generate: phase=%s deck_set=%s", self.phase, self.deck is not None)
            return
        await self._apply_deck(deck)

    async def _ask_setup_slot(self, slot: str, expect: str, stage: str, options: list[dict[str, str]], *,
                               question: str, default_value: Any, default_line: str) -> bool:
        """Asks one setup slot (CONTRACTS §5 `coach.stage` `setup_level`/
        `setup_length`), deterministically maps the voice/button answer to the
        slot's stored value, and falls back to a sensible default -- spoken --
        on timeout. Returns False when the caller must abort because the
        phase moved on (a race) while this awaited."""
        line = await self._prepare(question)
        self._expect = expect
        await self._play(line, kind="ack", before=lambda: self._send_coach(stage, options=options))
        intent = await self._await_intent(ASK_PROCEED_WAIT_S)
        if not (self.phase == "setup" and self.deck is None):
            return False
        value_map = _LEVEL_VALUES if slot == "level" else _LENGTH_VALUES
        if intent is None or intent not in value_map:
            default = await self._prepare_cached(default_line)
            await self._play_and_wait(default, kind="ack")
            value = default_value
        else:
            value = value_map[intent]
        self._expect = None
        self._fill_setup_slot(slot, value, self._setup_source)
        return self.phase == "setup" and self.deck is None

    async def _apply_deck(self, deck: dict[str, Any]) -> None:
        self.deck = deck
        self.budget_s = int(deck["budget_s"])
        self.store.set_deck(deck)
        for s in deck.get("slides", []):
            self.graph.add("slide", s["index"], title=s.get("title"), terms=s.get("terms"))
        self.send({"type": "deck", "deck": deck})
        topic = deck.get("topic") or "your topic"
        self._set_attention("thinking")
        instructions = (
            f"Acknowledge in one warm sentence that they chose to talk about {topic}, "
            "mentioning one specific, accurate detail about it, then tell them they can take "
            "up to a minute to prepare or say ready to begin right away. Under 35 words total."
        )
        fallback = f"So you've chosen {topic}, nice. Take a minute to prepare, or say ready to begin right away."
        line = await self._prepare_llm(instructions, fallback)
        await self._play_and_wait(line, kind="ack", before=lambda: self._send_coach("deck_ack"))
        self._enter_prep()

    # -- prep -----------------------------------------------------

    def _enter_prep(self) -> None:
        self._set_phase("prep")
        self._set_attention("waiting")
        if self._prep_task and not self._prep_task.done():
            self._prep_task.cancel()
        self._prep_task = asyncio.create_task(self._run_prep_countdown())
        self._prewarm_countdown()

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

    # -- countdown handshake (CONTRACTS §5/§6) -----------------------------------------------------

    def _prewarm_countdown(self) -> None:
        """Kicks off the four-clip render as soon as prep starts so the ready
        handshake usually has nothing left to wait on. Tracked on
        self._countdown_task -- never a stray untracked task -- and guarded so
        a second prep entry (e.g. recovering from a countdown error) never
        renders twice; the same clips are reused for every rerecord."""
        if self._countdown_cache is not None:
            return
        if self._countdown_task is not None and not self._countdown_task.done():
            return
        self._countdown_task = asyncio.create_task(self._render_countdown_cache())

    async def _render_countdown_cache(self) -> None:
        try:
            clips = await render_mod.render_countdown(self.store.clips_dir, model=RIME_MODEL, speaker=RIME_SPEAKER)
        except asyncio.CancelledError:
            # Only this render was cancelled (disconnect/new_talk tear-down):
            # complete quietly so an awaiter never mistakes someone else's
            # cancellation for its own.
            return
        except Exception:
            log.exception("countdown prewarm render failed")
            return
        self._countdown_cache = clips

    async def _ensure_countdown_clips(self) -> dict[str, dict[str, Any]] | None:
        if self._countdown_cache is not None:
            return self._countdown_cache
        task = self._countdown_task
        if task is not None and not task.done():
            await task
            if self._countdown_cache is not None:
                return self._countdown_cache
        try:
            clips = await render_mod.render_countdown(self.store.clips_dir, model=RIME_MODEL, speaker=RIME_SPEAKER)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("countdown render failed")
            return None
        self._countdown_cache = clips
        return clips

    def _countdown_clip_url(self, info: dict[str, Any]) -> str:
        return f"/sessions/{self.store.session_id}/clips/{Path(info['path']).name}"

    async def _interrupt_and_drain(self) -> None:
        """Stops and drains whatever Thunder is saying before the countdown
        handshake claims the audio pipeline -- never touches the deck or any
        other state, only the speech queue."""
        try:
            await self.session.interrupt(force=True)
        except Exception:
            log.exception("interrupt(force=True) failed ahead of countdown")

    async def _run_countdown(self) -> bool:
        """CONTRACTS §5/§6 countdown handshake. Stages loading->ready with four
        same-voice clips (cached after the first render, reused on rerecord),
        then blocks -- no timeout, no local timer -- until the client's
        completion ack for THIS id. Returns True to proceed into
        _enter_present; False means the caller must not start recording
        (render error, an actual client-reported playback failure, or we were
        cancelled out from under -- disconnect, a rerecord, or new_talk)."""
        cid = uuid.uuid4().hex
        self._countdown_id = cid
        self._countdown_pending = True
        self._refresh_turn()
        self._countdown_ready.clear()
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._countdown_future = fut
        ok = False
        self._set_attention("thinking")
        try:
            await self._interrupt_and_drain()

            self.store.log("countdown", id=cid, status="loading")
            self.send({"type": "countdown", "id": cid, "status": "loading"})

            clips = await self._ensure_countdown_clips()
            if clips is None:
                self.store.log("countdown", id=cid, status="error")
                self.send({"type": "countdown", "id": cid, "status": "error",
                           "message": "could not prepare the countdown"})
            else:
                clip_list = [{"label": label, "url": self._countdown_clip_url(info)} for label, info in clips.items()]
                self.store.log("countdown", id=cid, status="ready")
                self.send({"type": "countdown", "id": cid, "status": "ready", "clips": clip_list})
                self._countdown_ready.set()
                ok = await fut
        finally:
            if self._countdown_id == cid:
                self._countdown_id = None
                self._countdown_pending = False
            self._countdown_future = None
            self._refresh_turn()
        if not ok:
            # A stray final transcript from during the countdown wait (someone
            # talking over it, a "not ready" that got this far anyway) must
            # never bleed into the next thing said or the presentation itself.
            self.session.clear_user_turn()
            self._enter_prep()
        return ok

    async def _cancel_stale_tasks(self, *, exclude_current: bool = True) -> None:
        """Cancels every background flow (countdown render, present/analyze
        flow, post-analyze judge/coach/drill/report chain, prep timer, a
        practice-attempt capture) -- used on disconnect and by new_talk. Never
        cancels the task calling this (new_talk itself usually runs inside the
        very post-analyze/report chain being torn down)."""
        current = asyncio.current_task() if exclude_current else None
        candidates = (self._countdown_task, self._flow_task, self._post_analyze_task,
                      self._prep_task, self._attempt_task)
        fut = self._countdown_future
        if fut is not None and not fut.done():
            fut.cancel()
        tasks = [t for t in candidates if t is not None and not t.done() and t is not current]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- present -----------------------------------------------------

    def _start_present_flow(self, *, scope: dict[str, Any] | str = "full",
                             slide: int | None = None) -> None:
        if self._prep_task and not self._prep_task.done():
            self._prep_task.cancel()
        if self._flow_task and not self._flow_task.done():
            self._flow_task.cancel()
        self._flow_task = asyncio.create_task(self._run_flow(scope=scope, slide=slide))

    async def _run_flow(self, *, scope: dict[str, Any] | str, slide: int | None) -> None:
        try:
            if not await self._run_countdown():
                return
            revision = await self._enter_present(scope=scope, slide=slide)
            wav_path, text, words = await self._finish_present(revision)
            await self._enter_analyze(revision, wav_path, text, words)
        except asyncio.CancelledError:
            self._cleanup_present_recording()
            self._countdown_pending = False
            self._refresh_turn()
            raise
        except Exception:
            log.exception("presentation flow crashed")
            self.send({"type": "error", "message": "internal error during presentation"})

    async def _enter_present(self, *, scope: dict[str, Any] | str, slide: int | None) -> int:
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

        # Every recording opens on the first slide of its own scope. A
        # slide-scoped rerecord starts on that slide; a full-talk rerecord
        # starts at slide 1 -- carrying `current_slide` over from the previous
        # revision would open the retake on whatever slide the last run ended on.
        self.current_slide = slide if slide is not None else 1
        self._record_slide_event(self.current_slide)

        self.wav_writer = WavWriter(self.store.wav_path(revision), sample_rate=SAMPLE_RATE)
        self._word_stream = self._word_stt.stream()
        self._word_stream_task = asyncio.create_task(self._consume_word_stream(self._word_stream))

        self._set_phase("present")
        self._set_attention("listening")
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
        """Append to the revision's slide track and mirror it to the client.

        `slide_events` feeds `analysis/metrics.py`'s per-slide pacing, so every
        change of `current_slide` -- forwards, backwards, or the opening slide
        of a revision -- must go through here."""
        at_s = round(time.monotonic() - self._present_started_mono, 3)
        self.slide_events.append({"slide": slide, "at_s": at_s})
        self.store.log("slide", slide=slide)
        self.send({"type": "slide", "slide": slide})

    def _step_slide(self, delta: int, *, msg_type: str) -> None:
        """Move the deck one slide in either direction during a presentation.

        Both directions share one debounce window: two triggers landing
        together (the on-screen control plus the spoken "next slide", or a
        duplicated final transcript) must never skip a slide. Requests past
        either end of the deck are dropped silently -- the presenter is simply
        already on the first or last slide."""
        if self.phase != "present" or not self.deck:
            return
        now = time.monotonic()
        if now - self._last_advance_mono < 0.4:
            self.store.log("gate_blocked", type=msg_type, owner="user", expect="present")
            return
        total = len(self.deck.get("slides", []))
        target = self.current_slide + delta
        if target < 1 or (total and target > total):
            return
        self._last_advance_mono = now
        self.current_slide = target
        self._record_slide_event(self.current_slide)

    def _advance_slide(self) -> None:
        self._step_slide(1, msg_type="slide_next")

    def _rewind_slide(self) -> None:
        self._step_slide(-1, msg_type="slide_prev")

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
        self._refresh_turn()

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
        self._refresh_turn()
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
        self._set_attention("thinking")
        bridge = await self._prepare("Got it. Give me a moment while I go through your delivery.")
        await self._play(bridge, kind="ack")

        rev_data = self.store.get_revision(revision)
        deck_terms = [t for s in (self.deck or {}).get("slides", []) for t in s.get("terms", [])]

        # v2: a Deepgram REST re-transcription gives real per-word confidence
        # (the live STT stream only exposes segment-level confidence). This runs
        # right after the ack, non-blocking relative to it (_play doesn't await
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
            line = await self._prepare("I did not hear your presentation. Check your microphone, then say "
                                        "you're ready and start again.")
            await self._play(line, kind="feedback")
            # Back to prep through the normal path: `_enter_prep` restarts the
            # auto-start countdown and re-arms the countdown prerender, which a
            # hand-rolled phase message would leave stale -- the session would
            # then sit in prep until the user explicitly said "ready".
            self._enter_prep()
            return

        self._post_analyze_task = asyncio.create_task(self._judge_then_coach(revision, text, words, metrics))

    async def _judge_then_coach(self, revision: int, text: str, words: list[dict[str, Any]],
                                 metrics: dict[str, Any]) -> None:
        epoch = self._epoch
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

        if epoch != self._epoch or revision != self.store.current_revision:
            # Fenced: either a rerecord opened a newer revision, or new_talk
            # reset the topic before any new revision exists (the epoch covers
            # that window, store.current_revision alone does not).
            self.store.log("stale_dropped", tool="judge", revision=revision,
                            current_revision=self.store.current_revision,
                            epoch=epoch, current_epoch=self._epoch)
            return

        self.current_judgment = judgment
        self.store.update_revision(revision, judgment=judgment)
        # CONTRACTS §5 judgment.previous: the most recent superseded judgment of
        # THIS talk (reset by new_talk), not looked up on the wire -- sent right
        # before the intro line plays, not here (rule §B).
        previous = self._judgment_history[-1] if self._judgment_history else None
        self._judgment_history.append({"revision": revision, "scores": judgment.get("scores", {})})
        await self._enter_coach(revision, judgment, previous)

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

    async def _enter_coach(self, revision: int, judgment: dict[str, Any], previous: dict[str, Any] | None) -> None:
        self._set_phase("coach", defer_send=True)
        try:
            await self._coach_summary_and_items(revision, judgment, previous)
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
        self._refresh_turn()
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

    async def _coach_summary_and_items(self, revision: int, judgment: dict[str, Any],
                                        previous: dict[str, Any] | None) -> None:
        self.improvement_queue = [ImprovementItem(data=imp, revision=revision)
                                   for imp in judgment.get("improvements", [])]
        self.drill_terms = list(judgment.get("terms_to_drill", []))
        self.drill_words = list(judgment.get("drill_words", []))
        for item in self.improvement_queue:
            self._graph_add_improvement(item)
        if self.improvement_queue:
            asyncio.create_task(self._prerender_all_items(revision))

        self._set_attention("thinking")
        m = self.current_metrics or {}
        scores = judgment.get("scores", {})
        delta_fact = ""
        if previous is not None:
            prev_scores = previous.get("scores", {})
            deltas = [f"{k} went from {prev_scores.get(k)} to {v}" for k, v in scores.items()
                      if prev_scores.get(k) is not None and prev_scores.get(k) != v]
            if deltas:
                delta_fact = " Compared with the last attempt on this talk: " + "; ".join(deltas) + "."
        facts = (
            f"summary={judgment.get('summary', '')!r} scores={scores} wpm={m.get('wpm')} "
            f"filler_count={m.get('fillers', {}).get('count')} filler_per_min={m.get('fillers', {}).get('per_min')} "
            f"longest_pause_s={m.get('pauses', {}).get('longest_s')} "
            f"time_used_s={m.get('time_budget', {}).get('used_s')} budget_s={self.budget_s}"
        ) + delta_fact

        intro_instructions = (
            "Give a one-sentence overall impression of how the talk went, at most twenty five "
            f"words, using only these facts: {facts}."
        )
        intro_fallback = judgment.get("summary", "") or "Nice work out there."
        score_keys = list(scores.keys())
        scores_instructions = (
            f"Write exactly {len(score_keys)} short spoken lines, one per score category, in this "
            f"order: {', '.join(score_keys)}. Each line starts with the category name then a colon, "
            "for example \"delivery: you paced it well and only rushed the close.\" At most eighteen "
            f"words per line, using only these facts: {facts}. Nothing else -- no preamble, no "
            "closing line, exactly one line per category."
        )
        intro_line, scores_raw = await asyncio.gather(
            self._prepare_llm(intro_instructions, intro_fallback),
            self._llm_text(scores_instructions),
        )
        score_texts = _build_score_lines(scores_raw, scores)
        score_prepared_list = await self._prepare_many([score_texts[k] for k in score_keys])
        score_lines = dict(zip(score_keys, score_prepared_list))
        self._set_attention("listening")

        def _send_judgment() -> None:
            self._send_phase()
            self.send({"type": "judgment", "revision": revision, "judgment": judgment, "previous": previous})
            self._send_coach("summary")

        await self._play_and_wait(intro_line, kind="feedback", before=_send_judgment)
        for key in score_keys:
            await self._play_and_wait(
                score_lines[key], kind="feedback",
                before=(lambda k=key: self.send({"type": "focus", "target": "score", "key": k})),
            )
        if score_keys:
            self.send({"type": "focus", "target": None})

        if not self.improvement_queue:
            return

        n = len(self.improvement_queue)
        proceed_line = await self._prepare(f"Want to work through the {n} improvements together?")
        self._expect = "proceed"
        await self._play(proceed_line, kind="feedback", before=lambda: self._send_coach("ask_proceed", options=[
            {"name": "proceed", "label": "Let's do it"},
            {"name": "later", "label": "Maybe later"},
        ]))
        intent = await self._await_intent(ASK_PROCEED_WAIT_S)
        if intent is None or intent == "proceed":
            await self._run_items_loop()
        else:
            line = await self._prepare_cached("No problem, we'll save those for later.")
            await self._play(line, kind="feedback")

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

    async def _prepare_item_lines(self, item: ImprovementItem, idx: int, total: int) -> dict[str, PreparedLine]:
        """The intro line plus the three trio slot labels -- prepared together,
        concurrently with `_render_clips`, and cached on `item.spoken` (their
        text never changes on a "slower"/"again" clip re-render)."""
        imp = item.data
        skill_title = _skill_title(imp.get("skill"))
        slide = imp.get("slide")
        slide_part = f" on slide {slide}" if slide else ""
        intro_text = f"Improvement {idx + 1} of {total} \u2014 {skill_title}{slide_part}: {imp['issue']}"
        texts = [intro_text, "Here's you:", "Here's it cleaner:", "And with a pause before the key idea:"]
        intro, slot0, slot1, slot2 = await self._prepare_many(texts)
        return {"intro": intro, "slot0": slot0, "slot1": slot1, "slot2": slot2}

    def _trio_slot(self, item: ImprovementItem, slot: int) -> tuple[str, PreparedLine]:
        """Slot 0/1/2 -> (variant key into item.clips, prepared label). Slot 0
        is ALWAYS "v0" -- the user's own recording. When slicing failed there
        is no v1 substitution (CONTRACTS §5: the agent never re-synthesizes the
        presenter's words in its own voice); `_play_variant_v2` handles the
        missing clip honestly instead."""
        spoken = item.spoken or {}
        if slot == 0:
            return "v0", spoken.get("slot0") or PreparedLine("Here's you:")
        if slot == 1:
            return "v2", spoken.get("slot1") or PreparedLine("Here's it cleaner:")
        return "v3", spoken.get("slot2") or PreparedLine("And with a pause before the key idea:")

    async def _play_variant_v2(self, item: ImprovementItem, variant: str, label: PreparedLine) -> bool:
        imp_id = item.data["id"]
        imp = item.data
        clip = (item.clips or {}).get(variant)
        if variant == "v0" and clip is None:
            # No v1 fallback: the presenter's words are never re-synthesized in
            # the agent's own voice (CONTRACTS §5). One short honest line, then
            # the walkthrough moves straight on to the cleaner beat.
            self._send_feedback_stage(
                imp_id, "v0", text=None, role="user", available=False)
            honest = await self._prepare_cached(
                "I couldn't cut that line out of your recording cleanly, so here's the cleaner version.")
            await self._play_and_wait(honest, kind="ack")
            return True
        handle = await self._play_and_wait(
            label, kind="feedback",
            before=lambda: self._send_feedback_stage(
                imp_id, variant, text=label.text, markup=imp.get("v3_markup") if variant == "v3" else None),
        )
        if handle.interrupted:
            return False
        if clip is None:
            return True  # nothing to play (e.g. a re-render came back empty) -- don't get stuck
        frames = audio_frames_from_file(clip["path"], sample_rate=SAMPLE_RATE, num_channels=1)
        if variant == "v0":
            # The user's own clip: the client's red transcript beat, sent the
            # moment before the recording's frames start (CONTRACTS §5).
            self._send_feedback_stage(imp_id, "v0", text=clip["text"], role="user", available=True)
        await self._await_floor()
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
            spoken = item.spoken or {}
            intro_line = spoken.get("intro") or PreparedLine(
                f"Improvement {idx + 1} of {total} \u2014 {_skill_title(imp.get('skill'))}: {imp['issue']}")

            def _before() -> None:
                self._send_coach("item", options=_ITEM_OPTIONS, improvement_id=imp["id"], index=idx + 1, total=total)
                self._send_feedback_stage(imp["id"], "intro", text=imp.get("issue"))

            handle = await self._play_and_wait(intro_line, kind="feedback", before=_before)
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
            await self._await_floor()
            h = self._say(self._why_text(item.data)[:180], kind="answer")
            await h.wait_for_playout()
            return "restart"
        if cmd is not None:
            # "again", or any other recognised command mid-trio: full restart, as today.
            item.stage_idx = 0
            item.completed.clear()
            return "restart"
        await self._wait_for_default_reply()
        resume_line = await self._prepare_cached("Back to it \u2014")
        await self._play_and_wait(resume_line, kind="feedback",
                                   before=lambda: self._send_feedback_stage(item.data["id"], "resume"))
        return "resume"

    async def _deliver_improvement_flow(self, item: ImprovementItem, idx: int) -> str:
        imp = item.data
        total = len(self.improvement_queue)
        self._current_item = item
        self.graph.set_focus(f"improvement:{imp['id']}")
        self._expect = "menu"
        # Recognised immediately so a barge-in during the (usually already-warm)
        # clip/label prep below still routes -- the "item" coach message itself
        # is sent later, gated behind the intro line's audio (see `_play_stage`).
        self._offered = [o["name"] for o in _ITEM_OPTIONS]

        self._trio_active = True
        try:
            while item.stage_idx < 4:
                needs_labels = item.spoken is None
                needs_clips = item.clips is None
                if needs_labels or needs_clips:
                    self._set_attention("thinking")
                    aws: dict[str, Any] = {}
                    if needs_labels:
                        aws["spoken"] = self._prepare_item_lines(item, idx, total)
                    if needs_clips:
                        aws["clips"] = self._render_clips(item)
                    results = dict(zip(aws.keys(), await asyncio.gather(*aws.values())))
                    if needs_labels:
                        item.spoken = results["spoken"]
                    if needs_clips:
                        clips = results["clips"]
                        if clips is None:
                            return "skip"  # fenced: revision went stale mid-render, already logged
                        item.clips = clips
                    self._set_attention("listening")
                outcome = await self._play_stage(item, idx, total)
                if outcome in ("restart", "resume"):
                    continue
                if outcome is not None:
                    return outcome
        finally:
            self._trio_active = False

        if "v0" in item.completed and "v2" in item.completed and "v3" in item.completed:
            if not item.heard:
                item.heard = True
                self.store.log("feedback_heard", improvement_id=imp["id"])
                self.graph.mark(f"improvement:{imp['id']}", heard=True)

        return await self._practice_loop(item, idx, total)

    async def _render_clips(self, item: ImprovementItem) -> dict[str, dict[str, Any]] | None:
        revision = item.revision
        epoch = self._epoch
        rev_data = self.store.get_revision(revision)
        rev_wav = self.store.wav_path(revision)
        words = rev_data["transcript"]["words"]
        start = time.monotonic()
        self.store.log("tool_start", tool="render", revision=revision)
        clips, v0 = await asyncio.gather(
            render_mod.render_variants(item.data, self.store.clips_dir, slower=item.slower,
                                        model=RIME_MODEL, speaker=RIME_SPEAKER,
                                        variants=("v2", "v3")),
            asyncio.to_thread(slice_mod.slice_for_improvement, rev_wav, item.data, words, self.store.clips_dir),
        )
        ms = round((time.monotonic() - start) * 1000)
        self.store.log("tool_end", tool="render", revision=revision, ms=ms)

        if epoch != self._epoch or revision != self.store.current_revision:
            self.store.log("stale_dropped", tool="render", revision=revision,
                            current_revision=self.store.current_revision,
                            epoch=epoch, current_epoch=self._epoch)
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
        prompt_text = _PRACTICE_PROMPTS[min(idx, len(_PRACTICE_PROMPTS) - 1)]
        self._set_attention("thinking")
        prompt_line = await self._prepare_cached(prompt_text)
        self._expect = "menu"

        def _before() -> None:
            self._send_feedback_stage(imp["id"], "prompt", text=prompt_text, markup=imp.get("v3_markup"))
            self._send_coach("practice", options=_ITEM_OPTIONS, improvement_id=imp["id"], index=idx + 1, total=total)

        await self._play(prompt_line, kind="feedback", before=_before)
        # Resume numbering after every attempt this item has ever opened (wrap
        # "more" reuses the same items) so a repeat pass never overwrites an
        # earlier take's clip file.
        self._attempt_n = item.attempt_count + 1
        await self._start_capture(item, self._attempt_n)
        item.attempt_count = max(item.attempt_count, self._attempt_n)

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
            item.attempt_count = max(item.attempt_count, self._attempt_n)

    async def _handle_menu_intent(self, item: ImprovementItem, intent: str) -> None:
        imp = item.data
        await self._stop_capture()
        if intent == "original":
            variant, label = self._trio_slot(item, 0)
            await self._play_variant_v2(item, variant, label)
        elif intent == "cleaner":
            _, label = self._trio_slot(item, 1)
            await self._play_variant_v2(item, "v2", label)
        elif intent == "pauses":
            _, label = self._trio_slot(item, 2)
            await self._play_variant_v2(item, "v3", label)
        elif intent == "alternative":
            text = f"Another way in: {imp.get('alternative', '')}"
            line = await self._prepare(text)
            await self._play_and_wait(line, kind="feedback",
                                       before=lambda: self._send_feedback_stage(imp["id"], "alt", text=text))
        elif intent == "again":
            await self._play_trio_v2(item)
        elif intent == "slower":
            item.clips = None
            item.slower = True
            self._set_attention("thinking")
            clips = await self._render_clips(item)
            self._set_attention("listening")
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
        # Capture both fences before the first await: new_talk resets
        # _attempt_n/_current_item and bumps _epoch while _stop_capture yields.
        n = self._attempt_n
        epoch = self._epoch
        wav_path, words = await self._stop_capture()
        if epoch != self._epoch or item.revision != self.store.current_revision:
            self.store.log("stale_dropped", tool="attempt", revision=item.revision,
                            current_revision=self.store.current_revision,
                            epoch=epoch, current_epoch=self._epoch)
            return
        metrics = None
        if words and wav_path is not None:
            metrics = metrics_mod.compute(words=words, audio_path=str(wav_path), deck=self.deck or {},
                                           slide_events=[], budget_s=self.budget_s)
        imp_id = item.data["id"]
        verdict = practice_mod.evaluate_attempt(item.data, attempt=n, text=text, words=words, metrics=metrics)
        self.store.log("practice_attempt", improvement_id=imp_id, attempt=n,
                        fillers=verdict["fillers"]["count"], longest_pause_s=verdict["pauses"]["longest_s"],
                        wpm=verdict["wpm"], on_point=verdict["on_point"])
        attempt_key = self.graph.add("attempt", f"{imp_id}#{n}", text=text[:300], wpm=verdict.get("wpm"))
        self.graph.link(attempt_key, "practices", f"improvement:{imp_id}")
        verdict_key = self.graph.add("verdict", f"{imp_id}#{n}", **verdict)
        self.graph.link(verdict_key, "scores", attempt_key)
        self._verdicts.append({**verdict, "improvement_id": imp_id})

        idx = next(i for i, x in enumerate(self.improvement_queue) if x is item)
        total = len(self.improvement_queue)
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
        self._set_attention("thinking")
        verdict_line = await self._prepare_llm(instructions, fallback)
        self._set_attention("listening")

        def _before() -> None:
            self._send_feedback_stage(imp_id, "verdict")
            self._send_coach("verdict", options=_ITEM_OPTIONS, improvement_id=imp_id,
                              index=idx + 1, total=total, attempt=n, verdict=verdict)
            if wav_path is not None:
                self.send({"type": "clip", "improvement_id": imp_id, "variant": f"attempt{n}",
                           "url": f"/sessions/{self.store.session_id}/clips/{wav_path.name}"})
            # Let the practice loop open the next take's capture now, so a user who
            # goes straight into another attempt while the verdict is still being
            # spoken is recorded rather than routed to the LLM.
            if self._intent_future is not None and not self._intent_future.done():
                self._intent_future.set_result(_ATTEMPT_HANDLED)

        handle = await self._play_and_wait(verdict_line, kind="feedback", before=_before)
        if not handle.interrupted and self._current_item is item:
            line = await self._prepare_cached("Again, or next?")
            await self._play(line, kind="feedback")

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
        self._drill_announced = False
        if drill_words:
            rev_wav = self.store.wav_path(revision)
            for word in drill_words:
                await self._drill_word(word, rev_wav)
        else:
            for term in terms:
                await self._drill_term(term)
        await self._enter_report()

    def _announce_drill_phase(self) -> None:
        """CONTRACTS §5 `coach.stage=drill` -- sent once, gated behind the first
        drill line's audio rather than at phase entry (rule §B)."""
        if not self._drill_announced:
            self._drill_announced = True
            self._send_coach("drill")

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

        # Every fixed line for this word is known up-front (unlike the verdict
        # line below, none of these depend on the capture's outcome) -- prepare
        # them, and the user's own recording slice, all concurrently.
        self._set_attention("thinking")
        results = await asyncio.gather(
            self._prepare_cached("Quick check \u2014 here's a word you said:"),
            self._prepare(f"Here it is again: {w}. This checks whether a recogniser understands it, not your accent."),
            self._prepare(f"Your turn \u2014 say '{w}' for me."),
            asyncio.to_thread(slice_mod.slice_word, rev_wav, word, self.store.clips_dir, f"drill_{w}_you"),
            return_exceptions=True,
        )
        label_line, model_line, prompt_line, you_clip_result = results
        if isinstance(label_line, BaseException):
            label_line = PreparedLine("Quick check \u2014 here's a word you said:")
        if isinstance(model_line, BaseException):
            model_line = PreparedLine(f"Here it is again: {w}.")
        if isinstance(prompt_line, BaseException):
            prompt_line = PreparedLine(f"Your turn \u2014 say '{w}' for me.")
        you_clip: dict[str, Any] | None = None
        if isinstance(you_clip_result, BaseException):
            log.exception("drill slice_word failed for %r", w, exc_info=you_clip_result)
        else:
            you_clip = you_clip_result
        self._set_attention("listening")

        you_url = (f"/sessions/{self.store.session_id}/clips/{Path(you_clip['path']).name}" if you_clip else None)

        def _before_you() -> None:
            self._announce_drill_phase()
            self._send_drill(w, "you", url=you_url)

        label_handle = await self._play_and_wait(label_line, kind="feedback", before=_before_you)
        if not label_handle.interrupted and you_clip is not None:
            frames = audio_frames_from_file(you_clip["path"], sample_rate=SAMPLE_RATE, num_channels=1)
            await self._await_floor()
            clip_handle = self._say(w, kind="clip", audio=frames, add_to_chat_ctx=False)
            await clip_handle.wait_for_playout()

        await self._play_and_wait(model_line, kind="feedback", before=lambda: self._send_drill(w, "model"))

        # Open the capture before the prompt: people answer over the end of a
        # question, and a word said during "say it for me" must still count.
        await self._start_capture_named(f"drill_{w}_answer")
        self._expect = "drill"
        await self._play_and_wait(prompt_line, kind="feedback", before=lambda: self._send_drill(w, "prompt"))
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
        self.graph.mark(drill_key, conf_after=conf_after, heard_ok=heard_ok)
        if heard_ok and conf_before is not None and conf_after is not None:
            text = (f"Nice -- the recogniser understood '{w}' at {round(conf_after * 100)} percent that time, "
                    f"up from {round(conf_before * 100)} percent.")
        elif heard_ok:
            text = f"Good, that came through clearly as {w}."
        else:
            text = f"Recognisers still stumble on {w}; worth flagging to your audience."
        self._set_attention("thinking")
        verdict_line = await self._prepare(text)
        self._set_attention("listening")
        await self._play_and_wait(
            verdict_line, kind="feedback",
            before=lambda: self._send_drill(w, "verdict", conf_before=conf_before, conf_after=conf_after),
        )

    async def _drill_term(self, term: str) -> None:
        """Fallback drill used only when the judge returned no drill_words (e.g.
        a very short or very clean take): the pre-v2 raw-deck-term check."""
        self._set_attention("thinking")
        prompt_line = await self._prepare(
            f"Quick check: say the word '{term}'. This checks whether a recogniser understands "
            f"it, not your pronunciation.")
        self._set_attention("listening")
        await self._play_and_wait(prompt_line, kind="feedback", before=self._announce_drill_phase)
        for attempt in range(2):
            heard = await self._await_drill_answer(timeout=8.0)
            if heard is not None and _close_enough(heard, term):
                line = await self._prepare_cached(f"Good, that came through clearly as {term}.")
                await self._play_and_wait(line, kind="feedback")
                return
            if attempt == 0:
                line = await self._prepare_cached("Let's try that once more.")
                await self._play_and_wait(line, kind="feedback")
        line = await self._prepare(f"Recognisers still stumble on {term}; worth flagging to your audience.")
        await self._play_and_wait(line, kind="feedback")

    async def _await_drill_answer(self, timeout: float) -> str | None:
        self._expect = "drill"
        self._drill_answer_future = asyncio.get_event_loop().create_future()
        self._refresh_turn()
        self._set_attention("waiting")
        try:
            return await asyncio.wait_for(self._drill_answer_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._drill_answer_future = None
            self._expect = None
            self._set_attention("listening")
            self._refresh_turn()

    def deliver_drill_answer(self, text: str) -> None:
        if self._drill_answer_future is not None and not self._drill_answer_future.done():
            self._drill_answer_future.set_result(text)

    # -- report -----------------------------------------------------

    async def _enter_report(self) -> None:
        """The wrap state on the SAME dashboard (no separate report page). The
        summary/progress speech runs exactly once per revision; after that this
        loop waits indefinitely for a wrap choice -- silence just keeps the
        dashboard waiting, it never re-runs the wrap speech and never auto-picks
        more/new_talk. "more" re-enters the items loop directly (no recursive
        _enter_coach/_enter_report nesting); rerecord/new_talk hand off to their
        own flows and end this loop."""
        self._set_phase("report", defer_send=True)
        await self._speak_wrap_summary()
        self.store.set_graph(self.graph.to_json())
        while True:
            self._expect = "wrap"
            intent = await self._await_wrap_intent()
            self._expect = None
            if intent == "new_talk":
                await self._handle_new_talk()
                return
            if intent == "rerecord":
                # Whole-talk rerecord: the wrap's primary CTA is "try it again",
                # not a specific slide -- _handle_rerecord({}) leaves scope="full".
                await self._handle_rerecord({})
                return
            # "more": replay the existing improvements -- no new judgment, no
            # duplicate heard marks, no extra progress credit.
            self._resume_items_for_more()
            await self._run_items_loop()

    async def _speak_wrap_summary(self) -> None:
        heard = sum(1 for i in self.improvement_queue if i.heard)
        total = len(self.improvement_queue)
        self._set_attention("thinking")
        opening = await self._prepare(f"That's a wrap. We worked through {heard} of {total} improvements together.")
        self._set_attention("listening")

        def _before_wrap() -> None:
            self._send_phase()
            self._send_coach("wrap", options=_WRAP_OPTIONS)

        await self._play_and_wait(opening, kind="feedback", before=_before_wrap)

        # Cross-session progress is recorded at most once per presentation
        # revision: a wrap "more" loop must never credit the same talk twice.
        revision = self.store.current_revision
        if (self.progress is not None and self.current_judgment is not None
                and revision and revision not in self._progress_recorded_for):
            self._progress_recorded_for.add(revision)
            self.progress.record_session(self.current_judgment.get("scores", {}),
                                          self.current_judgment.get("improvements", []),
                                          list(self._verdicts))
            msg = self.progress.to_message()
            level, next_focus = msg["level"], msg["next_focus"]
            instructions = (
                f"Tell the user in one warm sentence that they're now at the {level} level overall, "
                f"and that next time you'll focus on {next_focus}. Use only these facts."
            )
            fallback = f"You're now at the {level} level overall -- next time, let's focus on {next_focus}."
            self._set_attention("thinking")
            try:
                progress_line = await self._prepare_llm(instructions, fallback)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("wrap progress line failed")
                progress_line = await self._prepare(fallback)
            self._set_attention("listening")

            def _before_progress() -> None:
                self.store.log("progress", level=msg["level"], next_focus=msg["next_focus"])
                self.send(msg)

            await self._play_and_wait(progress_line, kind="feedback", before=_before_progress)

        closing = await self._prepare_cached(
            "Try the whole talk again, go through these once more, or start something new \u2014 your call.")
        await self._play_and_wait(closing, kind="feedback")

    async def _await_wrap_intent(self) -> str | None:
        """Waits for a wrap choice. Unlike _await_intent there is no
        timeout-and-default: one brief check-in nudge fires when the option
        prompts go quiet, then the wait continues indefinitely -- silence never
        picks a continuation, and the wrap speech is never re-run."""
        self._nudges_this_wait = 0
        self._set_attention("waiting")
        try:
            while True:
                parked, self._pending_command = self._pending_command, None
                if parked in self._offered:
                    return parked
                self._intent_future = asyncio.get_event_loop().create_future()
                self._refresh_turn()
                try:
                    timeout = PRACTICE_WAIT_S if self._nudges_this_wait == 0 else None
                    return await asyncio.wait_for(self._intent_future, timeout=timeout)
                except asyncio.TimeoutError:
                    self._nudges_this_wait += 1
                    await self._nudge(reason="timeout")
                finally:
                    self._intent_future = None
                    self._refresh_turn()
        finally:
            self._set_attention("listening")

    def _resume_items_for_more(self) -> None:
        """Replays the current judgment's improvements from the intro. Playback
        progress resets; heard marks, verdicts, attempt numbering, and clip files
        are preserved -- no new judgment, no duplicate heard/progress credit."""
        for item in self.improvement_queue:
            item.stage_idx = 0
            item.completed.clear()

    async def _handle_new_talk(self) -> None:
        """CONTRACTS §5 wrap `new_talk`: reset to the setup phase on the same
        connection. Cancels and awaits every old flow (never the task calling
        this), bumps the epoch so stale judge/render/attempt callbacks fence
        themselves even before the next revision exists, and clears the topic's
        deck/metrics/judgment/words/clips/graph focus/turn context. Browser
        identity, connection, microphone, provider, and cross-session progress
        are preserved; old recordings on disk are never touched."""
        self._epoch += 1
        await self._cancel_stale_tasks()
        self.session.interrupt()

        self.deck = None
        self.budget_s = 60
        self.current_slide = 1
        self.active_revision = None
        self.words_buffer = []
        self.transcript_parts = []
        self.slide_events = []
        self._pending_supersede = None
        self.current_metrics = None
        self.current_judgment = None
        self.improvement_queue = []
        self.drill_terms = []
        self.drill_words = []
        self._verdicts = []
        self._pending_command = None
        self._attempt_n = 0
        self._current_item = None
        self._expect = None
        self._offered = []
        self._setup = {"topic": None, "level": None, "budget_s": None}
        self._setup_task = None
        self._setup_source = "voice"
        self._progress_recorded_for = set()
        self._judgment_history = []
        # The countdown clips are rendered once per live session in the fixed
        # process voice (RIME_MODEL/RIME_SPEAKER are env constants) -- new_talk
        # reuses them like any rerecord does.

        self.graph = graph_mod.SessionGraph(self.store.t0_mono)
        self._utterance_n = 0
        if self.progress is not None:
            self.graph.attach_progress(self.progress)

        self.session.clear_user_turn()
        self.store.set_deck(None)
        self.send({"type": "reset"})
        self._refresh_turn(force=True)
        self._set_phase("setup")
        offer_line = await self._prepare_cached(
            "Ready for a new one? Tell me a topic and I'll build the slides, or upload your own deck.")
        handle = await self._play(offer_line, kind="ack")
        self._setup_wait_task = asyncio.create_task(self._await_setup_input(handle))
        self.store.set_graph(self.graph.to_json())

    # -- rerecord -----------------------------------------------------

    async def _handle_rerecord(self, msg: dict[str, Any]) -> None:
        if self.deck is None:
            self.send({"type": "error", "message": "rerecord requires an active deck"})
            return
        slide = msg.get("slide")
        if slide is not None:
            try:
                slide = int(slide)
            except (TypeError, ValueError):
                slide = None
            total = len(self.deck.get("slides", []))
            if slide is None or slide < 1 or (total and slide > total):
                # Out-of-scope rerecord: reject rather than record a slide that
                # does not exist (CONTRACTS §5 -- the rerecord message carries an
                # explicit slide and the dashboard is responsible for sending a
                # valid one; a bad one must never open a revision).
                self.store.log("rerecord_rejected", slide=msg.get("slide"), total=total)
                self.send({"type": "error", "message": f"no slide {msg.get('slide')!r} to rerecord"})
                return
        # Fence every in-flight callback now, the same way new_talk does. The
        # revision counter alone is not enough: no new revision exists until
        # `_enter_present` runs, which is on the far side of a multi-second
        # countdown handshake (render + client ack), so a leftover prerender or
        # judge callback for the superseded revision would still pass the
        # `revision == store.current_revision` check and push stale clips to
        # the client.
        self._epoch += 1
        if self._post_analyze_task and not self._post_analyze_task.done():
            self._post_analyze_task.cancel()
        old_revision = self.store.current_revision
        if old_revision:
            self.store.supersede(old_revision)
            self._pending_supersede = old_revision
        self.session.interrupt()
        self._start_present_flow(scope={"slide": slide} if slide is not None else "full", slide=slide)


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
        # Barge-in speed is a judged property, so interruption.min_duration stays
        # well below the SDK default of 0.5 s (which put measured stop latency
        # near one second, evidence/e2/results.json part_c). It moves up to 0.3
        # now that push-to-talk (CONTRACTS §5 mic) mutes the microphone unless
        # the user is deliberately holding the floor -- a stray noise during
        # speech is far rarer, so the extra barge-in latency costs nothing.
        # resume_false_interruption stays armed but false_interruption_timeout
        # is None (disables the pause/resume recovery path entirely): in
        # livekit-agents 1.8.0 that path pauses playout, then -- at resume --
        # DISCARDS up to ~200 ms of already-queued audio
        # (voice/room_io/_output.py clears _playback_enabled and drops the next
        # forwarded frame) before picking up mid-word, which produced the
        # audible hard splice heard as "harsh noise in between" mid-sentence.
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            interruption={
                "mode": "vad",
                "min_duration": 0.3,
                # False on purpose: even a line the flow marks uninterruptible
                # must not silence the STT path. The SDK's discard substitutes
                # silence into the recognition stream
                # (voice/agent_activity.py push_audio), which loses the user's
                # answer outright instead of merely delaying it.
                "discard_audio_if_uninterruptible": False,
                "resume_false_interruption": True,
                "false_interruption_timeout": None,
            },
            # v2: start LLM inference on the interim transcript instead of
            # waiting for end-of-turn, shaving reply latency off every turn.
            # (This is also livekit-agents 1.8.0's own default; set explicitly
            # so the choice is visible here rather than relying on the SDK
            # default surviving a future upgrade.)
            preemptive_generation={"enabled": True},
        ),
        # aec_warmup_duration=0: the SDK default (3 s) re-arms on agent speech
        # and feeds SILENCE to STT for that whole window
        # (voice/agent_activity.py push_audio -> silence_frame_like), so an
        # answer given in the first three seconds after a question is never
        # transcribed -- measured directly: "Beginner, please." spoken 1.3 s
        # into the level ask produced user_speech_start/end with no transcript
        # at all, and the flow then timed out and defaulted. Echo protection is
        # already covered without it: the browser publishes with
        # echoCancellation on, and push-to-talk (CONTRACTS §5 mic) keeps the
        # microphone muted unless the user is deliberately holding the floor.
        aec_warmup_duration=0.0,
        use_tts_aligned_transcript=True,
    )
    # A second, REST-mode (chunked, non-streaming) Rime instance: the speech-
    # preparation layer (`PodiumOrchestrator._prepare`) synthesizes a line's
    # audio ahead of its matching UI message on this instance, independent of
    # the WS-streaming `tts` above that `session.say`'s live TTS still uses.
    tts_rest = rime.TTS(model=RIME_MODEL, speaker=RIME_SPEAKER, lang=RIME_LANG,
                         sample_rate=SAMPLE_RATE, use_websocket=False, pause_between_brackets=True)
    orchestrator = PodiumOrchestrator(session, store, ctx=ctx, tts_rest=tts_rest)
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

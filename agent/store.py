"""Session persistence: `session.json`, `timeline.jsonl`, and presentation WAV writing.

CONTRACTS.md §1/§4 is authoritative for shapes and the timeline event vocabulary.
This module owns the single source of truth for "which revision is current" (the
re-record fence) so `session_agent.py` never has to reimplement it.
"""
from __future__ import annotations

import json
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np

SESSIONS_ROOT = Path("sessions")

_VARIANT_LABEL = {"v1": "asdelivered", "v2": "cleaned", "v3": "paced"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """One instance per live session. Not process-shared; a single agent job owns it."""

    def __init__(self, session_id: str | None = None, root: Path = SESSIONS_ROOT) -> None:
        """Create the session directory and write an empty `session.json` (no
        revisions yet) immediately, so a crash before the first real write still
        leaves a session directory a client/evidence script can find."""
        self.session_id = session_id or f"s_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.dir = root / self.session_id
        self.clips_dir = self.dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._lock = Lock()
        self._session_path = self.dir / "session.json"
        self._timeline_path = self.dir / "timeline.jsonl"
        self._revision_counter = 0

        self.session: dict[str, Any] = {
            "session_id": self.session_id,
            "created_at": _now_iso(),
            "config": {},
            "deck": None,
            "revisions": [],
        }
        self.save()

    # -- session.json -----------------------------------------------------

    def save(self) -> None:
        """Whole-file rewrite; cheap at this session size, avoids a partial-write format."""
        with self._lock:
            tmp = self._session_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.session, indent=2), encoding="utf-8")
            tmp.replace(self._session_path)

    def set_config(self, **cfg: Any) -> None:
        """Merge into `session.json["config"]` (CONTRACTS.md §1) and persist."""
        self.session["config"].update(cfg)
        self.save()

    def set_deck(self, deck: dict[str, Any]) -> None:
        """Replace `session.json["deck"]` (§1) and persist; `None` clears it (new_talk)."""
        self.session["deck"] = deck
        self.save()

    def set_graph(self, graph: dict[str, Any]) -> None:
        """Replace `session.json["graph"]` with `SessionGraph.to_json()` (§8) and persist."""
        self.session["graph"] = graph
        self.save()

    # -- timeline.jsonl -----------------------------------------------------

    def log(self, ev: str, **fields: Any) -> None:
        """Append one `{"t", "ev", ...fields}` object to `timeline.jsonl` (§4).
        Append-only: callers never rewrite or reorder prior lines, so the file
        stays a faithful record even if the process crashes mid-session."""
        entry = {"t": round(time.monotonic() - self._t0, 3), "ev": ev, **fields}
        line = json.dumps(entry)
        with self._lock:
            with self._timeline_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    # -- revision fence -----------------------------------------------------
    # `current_revision` is the highest revision number ever created, independent
    # of `superseded`. A judge/render task tagged with an older number is stale
    # the instant a newer revision opens, whether or not the old one was later
    # explicitly superseded — that is exactly the property session_agent needs
    # to fence a pending judgment/render against a re-record.

    @property
    def current_revision(self) -> int:
        """The fence value: the highest revision number ever created (see the
        block comment above for why `superseded` doesn't affect this)."""
        return self._revision_counter

    @property
    def t0_mono(self) -> float:
        """The `time.monotonic()` origin `timeline.jsonl` timestamps are relative
        to -- v2's `SessionGraph` is built against the same origin so its `t`
        values line up with the timeline."""
        return self._t0

    def new_revision(self, scope: dict[str, Any] | str = "full") -> dict[str, Any]:
        """Open the next revision (§1 `revisions[]` entry), bump the fence, and log
        a `"revision"` timeline event. `scope` is `"full"` or `{"slide": n}` for a
        single-slide rerecord; callers must not reuse a revision number once this
        returns, since `current_revision` has already moved past it."""
        self._revision_counter += 1
        n = self._revision_counter
        rev = {
            "revision": n,
            "scope": scope,
            "audio": f"presentation_r{n}.wav",
            "started_at": _now_iso(),
            "ended_at": None,
            "transcript": {"text": "", "words": []},
            "slide_events": [],
            "metrics": None,
            "judgment": None,
            "superseded": False,
        }
        self.session["revisions"].append(rev)
        self.save()
        self.log("revision", revision=n, scope=scope)
        return rev

    def supersede(self, revision: int) -> None:
        """Flag `revision` as superseded (a rerecord replaced it). Does not move
        `current_revision` -- a caller that wants the fence to also advance must
        separately call `new_revision`. Raises `KeyError` if `revision` is unknown."""
        for rev in self.session["revisions"]:
            if rev["revision"] == revision:
                rev["superseded"] = True
                break
        else:
            raise KeyError(f"no such revision: {revision}")
        self.save()

    def update_revision(self, revision: int, **fields: Any) -> dict[str, Any]:
        """Merge `fields` into the named revision's dict (e.g. `metrics`,
        `judgment`, `transcript`) and persist. Raises `KeyError` if `revision`
        is unknown."""
        for rev in self.session["revisions"]:
            if rev["revision"] == revision:
                rev.update(fields)
                self.save()
                return rev
        raise KeyError(f"no such revision: {revision}")

    def get_revision(self, revision: int) -> dict[str, Any]:
        """Look up a revision dict by number. Raises `KeyError` if unknown."""
        for rev in self.session["revisions"]:
            if rev["revision"] == revision:
                return rev
        raise KeyError(f"no such revision: {revision}")

    # -- paths -----------------------------------------------------

    def wav_path(self, revision: int) -> Path:
        """Where that revision's full-talk recording lives (§1 `presentation_r<N>.wav`)."""
        return self.dir / f"presentation_r{revision}.wav"

    def clip_path(self, improvement_id: str, variant: str) -> Path:
        """Where a practice/contrast clip for `improvement_id` lives; `variant`
        is one of `_VARIANT_LABEL`'s keys (§1 `clips/` naming)."""
        return self.clips_dir / f"{improvement_id}_{variant}_{_VARIANT_LABEL[variant]}.wav"


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample int16 mono `samples` from `src_rate` to `dst_rate`.

    Plain linear interpolation, no anti-aliasing filter — a proper polyphase
    resampler would be more correct but pulls in scipy for a path that, per the
    shipped config (GATE0.md), should rarely trigger: the room's audio input is
    already expected at 24 kHz. This exists so a differently-configured client
    still produces a playable, roughly-in-tune WAV instead of a crash or noise.
    """
    if src_rate == dst_rate or samples.size == 0:
        return samples
    duration = samples.shape[0] / src_rate
    dst_n = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0, duration, num=samples.shape[0], endpoint=False)
    dst_x = np.linspace(0, duration, num=dst_n, endpoint=False)
    resampled = np.interp(dst_x, src_x, samples.astype(np.float64))
    return resampled.astype(np.int16)


class WavWriter:
    """Accumulates 16-bit PCM from `rtc.AudioFrame`s and writes a 24 kHz mono WAV.

    Frames arriving at a different sample rate or channel count than the target
    are downmixed (channel average) and linearly resampled on the way in; see
    `_resample_linear` for the explicit tradeoff. The written file's actual rate
    is always `self.sample_rate` (default 24000, matching CONTRACTS §1) — a
    session recorded at a different capture rate is documented as normalised,
    not carried through as a mismatched rate.
    """

    def __init__(self, path: Path, sample_rate: int = 24000) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self._chunks: list[np.ndarray] = []

    def write_frame(self, frame: Any) -> None:
        """`frame`: a `livekit.rtc.AudioFrame` (or any object exposing `.data`,
        `.sample_rate`, `.num_channels`)."""
        samples = np.frombuffer(bytes(frame.data), dtype=np.int16)
        channels = getattr(frame, "num_channels", 1)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
        src_rate = getattr(frame, "sample_rate", self.sample_rate)
        if src_rate != self.sample_rate:
            samples = _resample_linear(samples, src_rate, self.sample_rate)
        self._chunks.append(samples)

    @property
    def duration_s(self) -> float:
        total = sum(c.shape[0] for c in self._chunks)
        return total / self.sample_rate

    def finalize(self) -> Path:
        data = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.int16)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(data.tobytes())
        return self.path

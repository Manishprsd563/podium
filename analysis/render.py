"""The contrastive-clip renderer: V1 (as-delivered) / V2 (cleaned) / V3 (paced).

Same Rime model and speaker for all three so the only variable is wording and
pacing -- this is Podium's core coaching mechanism. Pure REST calls; never
touches the room.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import urllib.request
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

RIME_TTS_URL = "https://users.rime.ai/v1/rime-tts"
RATE = 24000
MAX_CHARS = 1000

VARIANT_NAMES = {"v1": "asdelivered", "v2": "cleaned", "v3": "paced"}

# Countdown clips (CONTRACTS.md §5/§6): four short same-voice numerals rendered
# once per live session and played back entirely by the browser -- the server
# never speaks a duplicate countdown. Order matters: it's the order the client
# receives and plays the clips in.
COUNTDOWN_TEXT = (("3", "Three."), ("2", "Two."), ("1", "One."), ("Begin", "Begin!"))

# GATE0.md: requesting timeScaleFactor=1.3 measured a 1.67x actual duration
# ratio on mistv3 (not 1.3x) -- the shipped "slower" feature calibrates
# against measured duration rather than trusting the nominal factor.
SLOWER_INITIAL_FACTOR = 1.13
SLOWER_TARGET_RATIO = 1.30
SLOWER_ACCEPTABLE_RANGE = (1.15, 1.45)


def _rime_key() -> str:
    load_dotenv()
    return os.environ["RIME_API_KEY"]


def _chunk_text(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Split at sentence boundaries so no single Rime request exceeds the 1000-char cap."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    chunks: list[str] = []
    cur = ""
    for s in sentences or [text]:
        candidate = f"{cur} {s}".strip() if cur else s
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    out: list[str] = []
    for c in chunks:
        while len(c) > limit:
            out.append(c[:limit])
            c = c[limit:]
        if c:
            out.append(c)
    return out or [""]


def _http_synth(text: str, model: str, speaker: str, sampling_rate: int, time_scale: float | None) -> bytes:
    body: dict[str, object] = {
        "text": text,
        "speaker": speaker,
        "modelId": model,
        "lang": "en",
        "samplingRate": sampling_rate,
        "pauseBetweenBrackets": True,
    }
    if time_scale is not None:
        body["timeScaleFactor"] = time_scale
    req = urllib.request.Request(
        RIME_TTS_URL, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {_rime_key()}", "Content-Type": "application/json", "Accept": "audio/wav"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _pcm_samples(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Trust the byte length of the `data` chunk, not the RIFF frame-count header
    (Rime streams a placeholder count) -- see evidence/e1_pause_control.py::_samples."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    data_idx = wav_bytes.find(b"data")
    body = wav_bytes[data_idx + 8:] if data_idx != -1 else wav_bytes[44:]
    usable = len(body) // (width * nch) * width * nch
    x = np.frombuffer(body[:usable], dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    return x, sr


def gaps(wav_bytes: bytes, *, thresh_db: float = -35.0, min_ms: int = 200) -> list[dict]:
    """Measured internal silence spans, so evidence scripts can assert a pause actually rendered."""
    x, sr = _pcm_samples(wav_bytes)
    if len(x) == 0:
        return []
    hop = max(int(sr * 0.01), 1)
    n = len(x) // hop
    if n == 0:
        return []
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    quiet = 20 * np.log10(rms) < thresh_db
    out, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if start > 0 and (i - start) * 10 >= min_ms:
                out.append({"at_s": round(start / 100, 2), "ms": (i - start) * 10})
            start = None
    if start is not None and start > 0 and (n - start) * 10 >= min_ms:
        out.append({"at_s": round(start / 100, 2), "ms": (n - start) * 10})
    return out


def _synth_pcm(text: str, model: str, speaker: str, sampling_rate: int, time_scale: float | None = None) -> tuple[np.ndarray, int]:
    chunks = _chunk_text(text)
    parts: list[np.ndarray] = []
    sr = sampling_rate
    for chunk in chunks:
        wav_bytes = _http_synth(chunk, model, speaker, sampling_rate, time_scale)
        samples, sr = _pcm_samples(wav_bytes)
        parts.append(samples)
    return (np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)), sr


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    pcm16 = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


def _insert_pauses(text: str, pauses: list[dict]) -> str:
    """Reinsert the user's own measured gaps into the verbatim quote as `<NNN>` markup.

    `pauses`: [{"after_word": <0-indexed position in `text.split(" ")>, "ms": <int>}].
    """
    if not pauses:
        return text
    words = text.split(" ")
    if not words:
        return text
    for p in sorted(pauses, key=lambda p: p["after_word"], reverse=True):
        idx = max(0, min(len(words) - 1, int(p["after_word"])))
        ms = max(1, int(p["ms"]))
        words[idx] = f"{words[idx]} <{ms}>"
    return " ".join(words)


def _calibrated_slower_render(text: str, model: str, speaker: str, sampling_rate: int) -> tuple[np.ndarray, int, float]:
    """One correction step: render at a baseline guess, measure the ratio against
    an unscaled render, and if it's outside the acceptable band, back-solve a
    corrected factor from the observed (non-linear) relationship and re-render once."""
    baseline, base_sr = _synth_pcm(text, model, speaker, sampling_rate, time_scale=None)
    baseline_dur = len(baseline) / base_sr if base_sr else 0.0

    factor = SLOWER_INITIAL_FACTOR
    samples, sr = _synth_pcm(text, model, speaker, sampling_rate, time_scale=factor)
    dur = len(samples) / sr if sr else 0.0
    ratio = dur / baseline_dur if baseline_dur > 0 else 1.0

    lo, hi = SLOWER_ACCEPTABLE_RANGE
    if baseline_dur > 0 and not (lo <= ratio <= hi):
        achieved_slowdown = ratio - 1.0
        requested_slowdown = factor - 1.0
        if achieved_slowdown > 1e-6:
            k = achieved_slowdown / requested_slowdown
            new_factor = max(0.4, min(2.5, 1.0 + (SLOWER_TARGET_RATIO - 1.0) / k))
            samples, sr = _synth_pcm(text, model, speaker, sampling_rate, time_scale=new_factor)
            factor = new_factor
    return samples, sr, factor


async def render_variants(improvement: dict, out_dir: Path, *, slower: bool = False, model: str = "mistv3", speaker: str = "summit") -> dict[str, dict]:
    """CONTRACTS.md §1 clips/. `improvement` needs "id", "quote", "v2_text",
    "v3_markup" (CONTRACTS §3 shape); optional "pauses" for V1 reinsertion."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    imp_id = improvement["id"]
    v1_text = _insert_pauses(improvement["quote"], improvement.get("pauses") or [])
    v2_text = improvement["v2_text"]
    v3_text = improvement["v3_markup"]

    def _do() -> dict[str, dict]:
        if slower:
            v1_samples, sr, factor = _calibrated_slower_render(v1_text, model, speaker, RATE)
            v2_samples, _ = _synth_pcm(v2_text, model, speaker, RATE, time_scale=factor)
            v3_samples, _ = _synth_pcm(v3_text, model, speaker, RATE, time_scale=factor)
        else:
            v1_samples, sr = _synth_pcm(v1_text, model, speaker, RATE)
            v2_samples, _ = _synth_pcm(v2_text, model, speaker, RATE)
            v3_samples, _ = _synth_pcm(v3_text, model, speaker, RATE)

        results: dict[str, dict] = {}
        for tag, samples, text in (("v1", v1_samples, v1_text), ("v2", v2_samples, v2_text), ("v3", v3_samples, v3_text)):
            path = out_dir / f"{imp_id}_{tag}_{VARIANT_NAMES[tag]}.wav"
            _write_wav(path, samples, sr)
            results[tag] = {
                "path": str(path),
                "duration_s": round(len(samples) / sr, 3) if sr else 0.0,
                "text": text,
                "gaps": gaps(path.read_bytes()),
            }
        return results

    return await asyncio.to_thread(_do)


async def render_countdown(out_dir: Path, *, model: str = "mistv3", speaker: str = "summit") -> dict[str, dict]:
    """CONTRACTS.md §5/§6: renders "Three."/"Two."/"One."/"Begin!" as four separate
    same-voice clips, keyed by label ("3", "2", "1", "Begin") in playback order.
    The browser owns countdown *timing* (waits for each clip's media-element
    `playing`/`ended`); this only has to produce the audio once. Callers (e.g.
    `session_agent.PodiumOrchestrator`) cache the result for the life of a
    session and reuse it across rerecord -- this function itself is stateless
    and always renders fresh, same as `render_variants`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _do() -> dict[str, dict]:
        results: dict[str, dict] = {}
        for label, text in COUNTDOWN_TEXT:
            samples, sr = _synth_pcm(text, model, speaker, RATE)
            path = out_dir / f"countdown_{label.lower()}.wav"
            _write_wav(path, samples, sr)
            results[label] = {
                "path": str(path),
                "duration_s": round(len(samples) / sr, 3) if sr else 0.0,
                "text": text,
            }
        return results

    return await asyncio.to_thread(_do)

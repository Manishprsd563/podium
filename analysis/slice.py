"""Cut the user's own recording by word span. CONTRACTS.md §5 clip v0 / §6.

Pure, I/O-free beyond reading/writing WAV files. Never touches the room.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np


def _normalize_word(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def _wav_samples(audio_path: str | Path) -> tuple[np.ndarray, int]:
    """Read mono float32 samples in [-1, 1] and the sample rate from a WAV file.

    Same byte-length-of-`data`-chunk pattern as analysis/metrics.py and
    analysis/render.py -- streamed WAVs can carry a placeholder frame count.
    """
    raw = Path(audio_path).read_bytes()
    with wave.open(io.BytesIO(raw)) as w:
        sr, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    data_idx = raw.find(b"data")
    body = raw[data_idx + 8:] if data_idx != -1 else raw[44:]
    usable = len(body) // (width * nch) * width * nch
    x = np.frombuffer(body[:usable], dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    return x, sr


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    pcm16 = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


def span_for_quote(quote: str, words: list[dict]) -> tuple[float, float] | None:
    """Locate `quote`'s word sequence in `words`, tolerant of small STT drift.

    Normalises tokens like `metrics._normalize_word` and allows at most one
    mismatched word per six words of quote (rounded down, minimum 0 for very
    short quotes). Returns (first_word.start, last_word.end) of the best
    (fewest-mismatch) match. If even that fails -- ordinary drift in the
    middle of a long quote -- a final fallback matches the quote's first and
    last 3 tokens independently and takes the longest span whose token count
    is within +-40% of the quote's. Returns None only when nothing matches."""
    quote_tokens = [_normalize_word(t) for t in quote.split() if _normalize_word(t)]
    n = len(quote_tokens)
    if n == 0 or not words:
        return None
    word_tokens = [_normalize_word(w["w"]) for w in words]
    allowed = n // 6
    best: tuple[int, int] | None = None  # (mismatches, start_idx, end_idx)
    for start in range(len(word_tokens) - n + 1):
        window = word_tokens[start:start + n]
        mismatches = sum(1 for a, b in zip(window, quote_tokens) if a != b)
        if mismatches <= allowed and (best is None or mismatches < best[0]):
            best = (mismatches, start, start + n - 1)
            if mismatches == 0:
                break
    if best is None:
        span = _anchor_span(quote_tokens, word_tokens, n)
        if span is None:
            return None
        best = (0, span[0], span[1])
    _, start, end = best
    return float(words[start]["start"]), float(words[end]["end"])


def _anchor_span(quote_tokens: list[str], word_tokens: list[str], n: int) -> tuple[int, int] | None:
    """Final fallback for `span_for_quote`: match the quote's first and last 3
    normalised tokens as independent exact anchors, then pick the longest
    span between them whose token count stays within +-40% of the quote's --
    ordinary STT drift in the middle of a long quote must still yield a span.
    Pure: returns None when even the anchors do not line up."""
    k = min(3, n)
    head, tail = quote_tokens[:k], quote_tokens[n - k:]
    head_starts = [i for i in range(len(word_tokens) - k + 1)
                   if word_tokens[i:i + k] == head]
    tail_ends = [i for i in range(k - 1, len(word_tokens))
                 if word_tokens[i - k + 1:i + 1] == tail]
    if not head_starts or not tail_ends:
        return None
    lo, hi = round(n * 0.6), round(n * 1.4)
    best: tuple[int, int] | None = None
    for hs in head_starts:
        for te in tail_ends:
            span_n = te - hs + 1
            if span_n >= lo and span_n <= hi and (best is None or span_n > best[1] - best[0]):
                best = (hs, te)
    if best is None:
        return None
    start, end = best
    return start, end


def slice_wav(src: Path, start: float, end: float, out: Path, pad_s: float = 0.25, fade_ms: int = 15) -> dict:
    """Cut `src` from `start` to `end` (with clamped padding and short linear
    fades) into `out`. Returns {"path", "duration_s", "start", "end"} where
    start/end are the padded, clamped bounds actually used."""
    x, sr = _wav_samples(src)
    duration = len(x) / sr if sr else 0.0
    s = max(0.0, start - pad_s)
    e = min(duration, end + pad_s)
    if e <= s:
        e = min(duration, s + 0.01)
    i0, i1 = int(round(s * sr)), int(round(e * sr))
    seg = x[i0:i1].copy()
    fade_n = min(int(sr * fade_ms / 1000), len(seg) // 2)
    if fade_n > 0:
        ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        seg[:fade_n] *= ramp
        seg[-fade_n:] *= ramp[::-1]
    out = Path(out)
    _write_wav(out, seg, sr)
    return {"path": str(out), "duration_s": round(len(seg) / sr, 3) if sr else 0.0, "start": s, "end": e}


def slice_for_improvement(rev_wav: str | Path, improvement: dict, words: list[dict], out_dir: str | Path) -> dict | None:
    """CONTRACTS.md §5 clip v0. Returns the slice dict with variant="v0" and
    text=improvement["quote"], or None if no usable span exists."""
    quote = str(improvement.get("quote", ""))
    span = span_for_quote(quote, words)
    if span is None:
        raw_span = improvement.get("span") or {}
        try:
            start, end = float(raw_span.get("start", 0.0)), float(raw_span.get("end", 0.0))
        except (TypeError, ValueError):
            start = end = 0.0
        if end > start:
            x, sr = _wav_samples(rev_wav)
            duration = len(x) / sr if sr else 0.0
            if 0.0 <= start < end <= duration:
                span = (start, end)
    if span is None:
        return None
    out = Path(out_dir) / f"{improvement['id']}_v0_you.wav"
    info = slice_wav(Path(rev_wav), span[0], span[1], out)
    info["variant"] = "v0"
    info["text"] = quote
    return info


def slice_word(rev_wav: str | Path, word: dict, out_dir: str | Path, name: str) -> dict:
    """Cut a single drill word out of the user's own recording."""
    out = Path(out_dir) / f"{name}.wav"
    return slice_wav(Path(rev_wav), float(word["start"]), float(word["end"]), out, pad_s=0.15)

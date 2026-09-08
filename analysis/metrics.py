"""Compute CONTRACTS.md §2 metrics from a Deepgram word list and a WAV file.

Pure and I/O-free beyond reading the WAV; never calls an LLM, never touches
the room. Numbers here are the ground truth the judge prompt is told not to
re-estimate.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np

# CONTRACTS.md §2 filler set: "um, uh, mm, hmm, mhm, er, ah, like, you know,
# basically, actually, sort of, kind of, i mean, right -- the first six are
# counted as vocal fillers, the rest as verbal crutches, reported separately."
VOCAL_FILLERS = ["um", "uh", "mm", "hmm", "mhm", "er"]
VERBAL_CRUTCHES = ["ah", "like", "you know", "basically", "actually", "sort of", "kind of", "i mean", "right"]

PAUSE_MIN_S = 0.35
DEAD_AIR_S = 2.0
RHETORICAL_RANGE = (0.35, 1.2)
LOUDNESS_FLOOR_DBFS = -50.0


def _wav_samples(audio_path: str | Path) -> tuple[np.ndarray, int]:
    """Read mono float32 samples in [-1, 1] and the sample rate from a WAV file.

    Rime (and Podium's own writer) can leave a placeholder frame count in the
    RIFF header on streamed audio, so trust the byte length of the `data`
    chunk rather than `wave.getnframes()` -- see
    evidence/e1_pause_control.py::_samples for the same pattern.
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


def _normalize_word(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def _normalize_phrase(p: str) -> str:
    return " ".join(_normalize_word(tok) for tok in p.split())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _scan_phrases(words: list[dict], phrase_set: list[str]) -> dict:
    """Find non-overlapping occurrences of single- or multi-word phrases, longest first."""
    singles = {_normalize_word(p) for p in phrase_set if " " not in p}
    phrases = {_normalize_phrase(p) for p in phrase_set if " " in p}
    max_len = max((len(p.split()) for p in phrases), default=1)
    norm = [_normalize_word(w["w"]) for w in words]
    used: set[int] = set()
    items = []
    for n in range(max_len, 0, -1):
        target_set = phrases if n > 1 else singles
        for i in range(len(words) - n + 1):
            if any(k in used for k in range(i, i + n)):
                continue
            window = " ".join(norm[i:i + n])
            if window in target_set:
                items.append({"w": " ".join(w["w"] for w in words[i:i + n]), "start": round(words[i]["start"], 2)})
                used.update(range(i, i + n))
    items.sort(key=lambda it: it["start"])
    return {"count": len(items), "items": items}


def _pauses(words: list[dict]) -> tuple[dict, list[dict]]:
    gaps = []
    for a, b in zip(words, words[1:]):
        dur = b["start"] - a["end"]
        if dur >= PAUSE_MIN_S:
            gaps.append({"start": round(a["end"], 3), "dur": round(dur, 3)})
    dead_air = [g for g in gaps if g["dur"] >= DEAD_AIR_S]
    rhetorical = [g for g in gaps if RHETORICAL_RANGE[0] <= g["dur"] <= RHETORICAL_RANGE[1]]
    durs = [g["dur"] for g in gaps]
    return {
        "count": len(gaps),
        "longest_s": round(max(durs), 3) if durs else 0.0,
        "mean_s": round(sum(durs) / len(durs), 3) if durs else 0.0,
        "dead_air": dead_air,
        "rhetorical": rhetorical,
    }, dead_air


def _wpm_overall(words: list[dict], dead_air: list[dict]) -> float:
    if not words:
        return 0.0
    span = words[-1]["end"] - words[0]["start"]
    speaking_s = max(span - sum(g["dur"] for g in dead_air), 1e-6)
    return round(len(words) / (speaking_s / 60.0), 1)


def _slide_spans(slide_events: list[dict], duration_s: float) -> list[dict]:
    ordered = sorted(slide_events, key=lambda e: e["at_s"])
    spans = []
    for i, ev in enumerate(ordered):
        start = ev["at_s"]
        end = ordered[i + 1]["at_s"] if i + 1 < len(ordered) else duration_s
        spans.append({"slide": ev["slide"], "start": start, "end": max(end, start)})
    return spans


def _wpm_by_slide(words: list[dict], spans: list[dict]) -> list[dict]:
    out = []
    for span in spans:
        in_slide = [w for w in words if span["start"] <= w["start"] < span["end"]]
        seconds = span["end"] - span["start"]
        wpm = round(len(in_slide) / (seconds / 60.0), 1) if seconds > 0 else 0.0
        out.append({"slide": span["slide"], "wpm": wpm, "seconds": round(seconds, 1)})
    return out


def _time_budget(spans: list[dict], duration_s: float, budget_s: float) -> dict:
    fair_share = budget_s / len(spans) if spans else float(budget_s)
    per_slide = [
        {"slide": s["slide"], "used_s": round(s["end"] - s["start"], 1), "fair_share_s": round(fair_share, 1)}
        for s in spans
    ]
    return {
        "budget_s": budget_s,
        "used_s": round(duration_s, 1),
        "over_s": round(duration_s - budget_s, 1),
        "per_slide": per_slide,
    }


def _loudness(x: np.ndarray, sr: int) -> dict:
    if len(x) == 0 or sr <= 0:
        return {"mean_dbfs": -100.0, "variance_db": 0.0}
    frame = sr  # one-second frames
    frames = [x] if len(x) < frame else list(x[: (len(x) // frame) * frame].reshape(-1, frame))
    voiced = []
    for f in frames:
        rms = float(np.sqrt(np.mean(f.astype(np.float64) ** 2) + 1e-12))
        dbfs = 20 * np.log10(rms + 1e-12)
        if dbfs >= LOUDNESS_FLOOR_DBFS:
            voiced.append(dbfs)
    if not voiced:
        return {"mean_dbfs": -100.0, "variance_db": 0.0}
    arr = np.array(voiced)
    return {"mean_dbfs": round(float(arr.mean()), 1), "variance_db": round(float(arr.std()), 1)}


def _deck_terms(deck: dict) -> list[str]:
    terms: list[str] = []
    for slide in deck.get("slides", []):
        for t in slide.get("terms", []):
            if t not in terms:
                terms.append(t)
    return terms


def _low_confidence_terms(terms: list[str], words: list[dict]) -> list[dict]:
    norm_words = [_normalize_word(w["w"]) for w in words]
    out = []
    for term in terms:
        term_tokens = [_normalize_word(t) for t in term.replace("-", " ").split() if _normalize_word(t)]
        if not term_tokens:
            continue
        n = len(term_tokens)
        target = "".join(term_tokens)
        best: tuple[int, str, float, float] | None = None
        for i in range(len(words) - n + 1):
            window_tokens = norm_words[i:i + n]
            if not all(window_tokens):
                continue
            candidate = "".join(window_tokens)
            dist = _levenshtein(target, candidate)
            if best is None or dist < best[0]:
                heard = " ".join(w["w"] for w in words[i:i + n])
                conf = sum(w.get("conf", 1.0) for w in words[i:i + n]) / n
                best = (dist, heard, conf, words[i]["start"])
        if best is None:
            continue
        dist, heard, conf, start = best
        near_miss_threshold = max(1, len(target) // 3)
        if dist == 0:
            if conf < 0.6:
                out.append({"term": term, "heard": heard, "conf": round(conf, 2), "start": round(start, 2)})
        elif dist <= near_miss_threshold:
            out.append({"term": term, "heard": heard, "conf": round(conf, 2), "start": round(start, 2)})
    return out


def compute(words: list[dict], audio_path: str | Path, deck: dict, slide_events: list[dict], budget_s: float) -> dict:
    """CONTRACTS.md §2. `words` = Deepgram-shaped [{"w","start","end","conf"}, ...]."""
    x, sr = _wav_samples(audio_path)
    duration_s = round(len(x) / sr, 2) if sr else 0.0
    words_sorted = sorted(words, key=lambda w: w["start"])
    pauses, dead_air = _pauses(words_sorted)
    fillers = _scan_phrases(words_sorted, VOCAL_FILLERS)
    crutches = _scan_phrases(words_sorted, VERBAL_CRUTCHES)
    minutes = duration_s / 60.0
    fillers["per_min"] = round(fillers["count"] / minutes, 2) if minutes else 0.0
    crutches["per_min"] = round(crutches["count"] / minutes, 2) if minutes else 0.0
    spans = _slide_spans(slide_events, duration_s)
    return {
        "duration_s": duration_s,
        "words": len(words_sorted),
        "wpm": _wpm_overall(words_sorted, dead_air),
        "wpm_by_slide": _wpm_by_slide(words_sorted, spans),
        "fillers": fillers,
        "crutches": crutches,
        "pauses": pauses,
        "loudness": _loudness(x, sr),
        "time_budget": _time_budget(spans, duration_s, budget_s),
        "low_confidence_terms": _low_confidence_terms(_deck_terms(deck), words_sorted),
    }

"""Compute CONTRACTS.md §7 practice verdicts from a user's spoken attempt.

Pure and I/O-free; never calls an LLM, never touches the room. The coach LLM
may only *phrase* these numbers (see ``scripted_verdict`` for the deterministic
fallback phrasing), never change them.
"""
from __future__ import annotations

import re

from analysis.metrics import (
    PAUSE_MIN_S,
    VERBAL_CRUTCHES,
    VOCAL_FILLERS,
    _normalize_phrase,
    _normalize_word,
    _scan_phrases,
)

# Every individual word that appears anywhere in the CONTRACTS.md §2 filler
# set (single-word entries as-is, multi-word entries split into their words),
# used to drop filler words from both sides of a token-overlap comparison.
_FILLER_TOKENS = {
    _normalize_word(tok)
    for phrase in (VOCAL_FILLERS + VERBAL_CRUTCHES)
    for tok in phrase.split()
}

_PAUSE_MARKUP_RE = re.compile(r"<\d+>")


def _content_tokens(text: str) -> set[str]:
    """Normalised, filler-stripped token set of `text`, for Jaccard
    overlap in `token_overlap`."""
    tokens: set[str] = set()
    for raw in (text or "").split():
        norm = _normalize_word(raw)
        if not norm or norm in _FILLER_TOKENS:
            continue
        tokens.add(norm)
    return tokens


def _content_word_count(text: str) -> int:
    """Count of `text`'s words after dropping filler/crutch tokens -- the
    "3 or more content words" gate `looks_like_attempt` uses to rule out
    one-word interjections."""
    count = 0
    for raw in (text or "").split():
        norm = _normalize_word(raw)
        if norm and norm not in _FILLER_TOKENS:
            count += 1
    return count


def token_overlap(a: str, b: str) -> float:
    """Jaccard overlap of normalised, filler-stripped tokens in ``a`` and ``b``."""
    ta = _content_tokens(a)
    tb = _content_tokens(b)
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def looks_like_attempt(text: str, improvement: dict) -> bool:
    """§7: a practice-wait utterance is a repetition, not a question, when it
    has >= 3 content words and overlaps the improvement's original wording."""
    if _content_word_count(text) < 3:
        return False
    combined = " ".join(
        [
            improvement.get("quote", "") or "",
            improvement.get("v2_text", "") or "",
            improvement.get("alternative", "") or "",
        ]
    )
    return token_overlap(text, combined) >= 0.25


def _text_phrase_matches(norm_tokens: list[str], raw_tokens: list[str], phrase_set: list[str]) -> list[tuple[int, str]]:
    """Same non-overlapping, longest-first phrase scan as metrics._scan_phrases,
    but over plain (untimed) text tokens instead of Deepgram word dicts."""
    singles = {_normalize_word(p) for p in phrase_set if " " not in p}
    phrases = {_normalize_phrase(p) for p in phrase_set if " " in p}
    max_len = max((len(p.split()) for p in phrases), default=1)
    used: set[int] = set()
    matches: list[tuple[int, str]] = []
    for n in range(max_len, 0, -1):
        target_set = phrases if n > 1 else singles
        for i in range(len(norm_tokens) - n + 1):
            if any(k in used for k in range(i, i + n)):
                continue
            window = " ".join(norm_tokens[i:i + n])
            if window in target_set:
                matches.append((i, " ".join(raw_tokens[i:i + n])))
                used.update(range(i, i + n))
    matches.sort(key=lambda m: m[0])
    return matches


def _fillers_from_text(text: str) -> dict:
    """Filler/crutch count and phrases found in plain (untimed) `text`,
    used when no word timestamps arrived for the attempt."""
    raw = (text or "").split()
    norm = [_normalize_word(t) for t in raw]
    vocal = _text_phrase_matches(norm, raw, VOCAL_FILLERS)
    crutch = _text_phrase_matches(norm, raw, VERBAL_CRUTCHES)
    combined = sorted(vocal + crutch, key=lambda m: m[0])
    return {"count": len(combined), "items": [phrase for _, phrase in combined]}


def _fillers_from_words(words: list[dict]) -> dict:
    """Filler/crutch count and phrases found in timestamped `words`, the
    preferred source over `_fillers_from_text` when available."""
    ws = sorted(words, key=lambda w: w["start"])
    vocal = _scan_phrases(ws, VOCAL_FILLERS)
    crutch = _scan_phrases(ws, VERBAL_CRUTCHES)
    combined = sorted(vocal["items"] + crutch["items"], key=lambda it: it["start"])
    return {"count": vocal["count"] + crutch["count"], "items": [it["w"] for it in combined]}


def _pauses_from_words(words: list[dict]) -> dict:
    """Pause stats for one practice attempt: gaps >= PAUSE_MIN_S (0.35 s)
    between consecutive words, `longest_s` the largest of them, and
    `landed` true once that longest gap reaches 0.4 s -- CONTRACTS.md §7's
    threshold for "the pause actually landed" (deliberately stricter than
    the 0.35 s floor that counts a gap as a pause at all)."""
    if not words:
        return {"count": 0, "longest_s": 0.0, "landed": False}
    ws = sorted(words, key=lambda w: w["start"])
    durs = [b["start"] - a["end"] for a, b in zip(ws, ws[1:]) if b["start"] - a["end"] >= PAUSE_MIN_S]
    if not durs:
        return {"count": 0, "longest_s": 0.0, "landed": False}
    longest = round(max(durs), 2)
    return {"count": len(durs), "longest_s": longest, "landed": longest >= 0.4}


def _word_count(text: str, words: list[dict]) -> int:
    """Word count for the verdict: timestamped `words` when available,
    else a plain whitespace split of `text`."""
    if words:
        return len(words)
    return len((text or "").split())


def evaluate_attempt(improvement: dict, *, attempt: int, text: str, words: list[dict], metrics: dict | None) -> dict:
    """CONTRACTS.md §7. Compares the spoken ``text``/``words`` attempt with the
    improvement's original ``quote``, using only computed numbers."""
    fillers = _fillers_from_words(words) if words else _fillers_from_text(text)
    original_fillers = _fillers_from_text(improvement.get("quote", "") or "")["count"]
    pauses = _pauses_from_words(words)

    wpm = metrics.get("wpm") if metrics else None
    if wpm is None:
        pace_band = "unknown"
    elif wpm > 170:
        pace_band = "fast"
    elif wpm < 110:
        pace_band = "slow"
    else:
        pace_band = "good"

    quote = improvement.get("quote", "") or ""
    v2_text = improvement.get("v2_text", "") or ""
    on_point = round(token_overlap(text, f"{quote} {v2_text}"), 2)

    wins: list[str] = []
    if fillers["count"] == 0:
        wins.append("no fillers")
    elif fillers["count"] < original_fillers:
        wins.append("fewer fillers")
    if pauses["landed"]:
        wins.append("pause landed")
    if pace_band == "good":
        wins.append("good pace")
    if on_point >= 0.5:
        wins.append("on the point")

    next_focus = None
    if fillers["count"] > 0 and fillers["count"] >= original_fillers:
        next_focus = "fillers"
    elif not pauses["landed"] and _PAUSE_MARKUP_RE.search(improvement.get("v3_markup", "") or ""):
        next_focus = "pause"
    elif pace_band in ("fast", "slow"):
        next_focus = "pace"

    return {
        "attempt": attempt,
        "text": text,
        "words": _word_count(text, words),
        "wpm": wpm,
        "fillers": fillers,
        "original_fillers": original_fillers,
        "pauses": pauses,
        "on_point": on_point,
        "pace_band": pace_band,
        "wins": wins,
        "next_focus": next_focus,
    }


def scripted_verdict(verdict: dict, improvement: dict) -> str:
    """One or two spoken sentences, phrasing only numbers already in ``verdict``.

    Used as the fallback when the coach LLM fails to phrase the verdict itself.
    """
    fillers = verdict["fillers"]["count"]
    original = verdict["original_fillers"]
    pauses = verdict["pauses"]
    wpm = verdict["wpm"]
    band = verdict["pace_band"]
    on_point = verdict["on_point"]

    clauses: list[str] = []
    if fillers == 0 and original == 0:
        clauses.append("no filler words that time")
    elif fillers == 0:
        clauses.append(f"zero fillers that time, down from {original}")
    elif original and fillers < original:
        word = "filler" if fillers == 1 else "fillers"
        clauses.append(f"just {fillers} {word} that time, down from {original}")
    elif original and fillers >= original:
        word = "filler" if fillers == 1 else "fillers"
        clauses.append(f"still {fillers} {word} in there, same as before")
    else:
        word = "filler" if fillers == 1 else "fillers"
        clauses.append(f"{fillers} {word} crept in that time")

    v3_markup = improvement.get("v3_markup", "") or ""
    expects_pause = bool(_PAUSE_MARKUP_RE.search(v3_markup))
    if pauses["landed"]:
        clauses.append("the pause landed")
    elif expects_pause:
        clauses.append("the pause did not quite land")

    sentence1 = ", and ".join(clauses) if len(clauses) > 1 else clauses[0]
    sentence1 = sentence1[0].upper() + sentence1[1:] + "."

    sentence2 = None
    if band == "good":
        if on_point >= 0.5:
            sentence2 = "Pace was right in the pocket and you stayed right on the point."
        else:
            sentence2 = "Pace was right in the pocket."
    elif band == "fast":
        sentence2 = f"Pace was a touch fast at {wpm} words a minute, so ease off a little on the next one."
    elif band == "slow":
        sentence2 = f"Pace dragged a bit at {wpm} words a minute, so pick it up slightly next time."
    elif on_point < 0.5:
        sentence2 = "Try to stay a little closer to the point next time."

    if sentence2:
        return f"{sentence1} {sentence2}"
    return sentence1

"""Deepgram REST per-word confidence and the CONTRACTS.md §2 `pronunciation`
block. Described to the user as intelligibility -- how reliably a recogniser
understood the words -- never as an accent judgement.

Pure beyond the REST call itself. Never touches the room.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

DEEPGRAM_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-3&language=en&filler_words=true&punctuate=true"
)

# CONTRACTS.md §2 filler set (kept identical to analysis/metrics.py's list).
FILLER_SET = {
    "um", "uh", "mm", "hmm", "mhm", "er", "ah", "like", "you know",
    "basically", "actually", "sort of", "kind of", "i mean", "right",
}

LOW_CONF_THRESHOLD = 0.6


class PronunciationError(RuntimeError):
    """Raised when Deepgram REST transcription fails or returns an unusable shape."""


def _normalize_word(w: str) -> str:
    return "".join(ch for ch in w.lower() if ch.isalnum())


def _api_key() -> str:
    """The Deepgram REST key, with a message that names the fix rather than a
    bare KeyError from inside a post-take transcription."""
    load_dotenv()
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY missing -- copy .env.example to .env and fill it in")
    return key


def transcribe_rest(wav_path: str | Path, *, timeout_s: int = 60) -> list[dict]:
    """POST the WAV to Deepgram nova-3 REST and return Deepgram-shaped words:
    [{"w","start","end","conf"}, ...] with real per-word confidence.

    Raises PronunciationError on any HTTP or shape failure -- never swallows.
    """
    data = Path(wav_path).read_bytes()
    req = urllib.request.Request(DEEPGRAM_URL, data=data, method="POST")
    req.add_header("Authorization", f"Token {_api_key()}")
    req.add_header("Content-Type", "audio/wav")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read()
    except urllib.error.URLError as e:
        raise PronunciationError(f"deepgram REST request failed: {e}") from e
    try:
        obj = json.loads(body)
        words = obj["results"]["channels"][0]["alternatives"][0]["words"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise PronunciationError(f"deepgram REST response malformed: {e}") from e
    out = []
    for w in words:
        try:
            out.append({
                "w": str(w["word"]),
                "start": float(w["start"]),
                "end": float(w["end"]),
                "conf": float(w.get("confidence", 0.0)),
            })
        except (KeyError, TypeError, ValueError) as e:
            raise PronunciationError(f"deepgram REST word malformed: {e}") from e
    return out


# Function words are never worth a drill: a low-confidence "the" says nothing
# about the speaker, only about co-articulation.
FUNCTION_WORDS = frozenset("""
the and that this these those with for from into onto but not are was were has have had
how what when where which who whom why can could would should will shall may might
its our your their his her they them then than there here about over under again
""".split())

def intelligibility(words: list[dict], deck_terms: list[str], source: str = "deepgram_rest") -> dict:
    """CONTRACTS.md §2 `pronunciation` block. `deck_terms` is accepted for
    interface parity with the rest of the pronunciation pipeline; the block
    itself is derived purely from per-word confidence."""
    del deck_terms
    n = len(words)
    below = [w for w in words if float(w.get("conf", 1.0)) < LOW_CONF_THRESHOLD]
    intel = round(1.0 - (len(below) / n), 3) if n else 1.0
    candidates = [
        w for w in words
        if _normalize_word(str(w.get("w", ""))) not in FILLER_SET
        and _normalize_word(str(w.get("w", ""))) not in FUNCTION_WORDS
        and len(_normalize_word(str(w.get("w", "")))) >= 3
    ]
    low_sorted = sorted(candidates, key=lambda w: float(w.get("conf", 1.0)))[:12]
    low_confidence = [
        {"w": w["w"], "start": w["start"], "end": w["end"], "conf": w["conf"]}
        for w in low_sorted
    ]
    return {
        "intelligibility": intel,
        "words_below_0_6": len(below),
        "low_confidence": low_confidence,
        "source": source,
    }


# A word only earns a drill when the recogniser was genuinely unsure of it; a
# perfectly clear take must produce no drill rather than drilling its best words.
DRILL_CONF_THRESHOLD = 0.85


def pick_drill_words(pron_block: dict | None, deck_terms: list[str], max_n: int = 3) -> list[dict]:
    """Deck terms first, then lowest confidence, from `pron_block["low_confidence"]`,
    restricted to words below DRILL_CONF_THRESHOLD."""
    if not pron_block:
        return []
    low = [w for w in (pron_block.get("low_confidence") or [])
           if float(w.get("conf", 1.0)) < DRILL_CONF_THRESHOLD]
    deck_norm = {_normalize_word(t) for t in deck_terms}
    deck_matches = [w for w in low if _normalize_word(str(w.get("w", ""))) in deck_norm]
    deck_ids = {id(w) for w in deck_matches}
    rest = [w for w in low if id(w) not in deck_ids]
    return (deck_matches + rest)[:max_n]

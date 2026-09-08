"""Shared Rime/Deepgram/WAV helpers for the evidence scripts (E2, E3, run_all).

Not a CONTRACTS-owned module — internal to evidence/, kept separate from
e1_pause_control.py (finished, untouched) purely to avoid duplicating the
same REST/WAV plumbing across e2 and e3.
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()

RIME_KEY = os.environ["RIME_API_KEY"]
DG_KEY = os.environ.get("DEEPGRAM_API_KEY", "")

RIME_MODEL = "mistv3"
RIME_SPEAKER = "astra"
RIME_LANG = "eng"
SAMPLE_RATE = 24000


def rime_rest_synth(
    text: str,
    *,
    model: str = RIME_MODEL,
    speaker: str = RIME_SPEAKER,
    lang: str = RIME_LANG,
    sample_rate: int = SAMPLE_RATE,
    pause_between_brackets: bool = True,
) -> bytes:
    """POST /v1/rime-tts, return WAV bytes. Same shipped config as GATE0.md."""
    body = {
        "text": text, "speaker": speaker, "modelId": model, "lang": lang,
        "samplingRate": sample_rate, "pauseBetweenBrackets": pause_between_brackets,
    }
    req = urllib.request.Request(
        "https://users.rime.ai/v1/rime-tts", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {RIME_KEY}", "Content-Type": "application/json",
                 "Accept": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def wav_bytes_to_int16(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Rime streams WAV with a placeholder frame count; trust the byte length (see E1)."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    if width != 2:
        raise ValueError(f"expected 16-bit PCM, got {width * 8}-bit")
    raw = wav_bytes[44:]
    usable = len(raw) // (width * nch) * width * nch
    return np.frombuffer(raw[:usable], dtype=np.int16), sr


def wav_bytes_to_float(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    x, sr = wav_bytes_to_int16(wav_bytes)
    return x.astype(np.float32) / 32768.0, sr


def write_wav_int16(path: str | Path, samples: np.ndarray, sr: int, *, num_channels: int = 1) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(num_channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.ascontiguousarray(samples, dtype=np.int16).tobytes())


def read_wav_int16(path: str | Path) -> tuple[np.ndarray, int]:
    with open(path, "rb") as f:
        return wav_bytes_to_int16(f.read())


def gaps(samples_float: np.ndarray, sr: int, *, thresh_db: float = -40.0, min_ms: int = 200) -> list[dict]:
    """Silence-gap detector on RMS-per-10ms-frame, identical method to E1."""
    hop = int(sr * 0.01)
    n = len(samples_float) // hop
    if n == 0:
        return []
    rms = np.sqrt((samples_float[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
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


def deepgram_transcribe(wav_bytes: bytes, *, filler_words: bool = True) -> dict:
    """Prerecorded Deepgram nova-3, same knobs the live agent ships (CONTRACTS/GATE0)."""
    if not DG_KEY:
        return {"transcript": "(no DEEPGRAM_API_KEY)", "words": []}
    url = (
        "https://api.deepgram.com/v1/listen?model=nova-3&language=en&punctuate=true"
        f"&filler_words={'true' if filler_words else 'false'}&numerals=false"
    )
    req = urllib.request.Request(
        url, data=wav_bytes, method="POST",
        headers={"Authorization": f"Token {DG_KEY}", "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.load(r)
    alt = payload["results"]["channels"][0]["alternatives"][0]
    words = [
        {"w": w.get("punctuated_word", w["word"]), "start": w["start"], "end": w["end"],
         "conf": w.get("confidence", 0.0)}
        for w in alt.get("words", [])
    ]
    return {"transcript": alt["transcript"], "words": words}


def duration_s(samples: np.ndarray, sr: int) -> float:
    return round(len(samples) / sr, 3)

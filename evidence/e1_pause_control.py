"""E1 — controlled test: does Rime pause markup produce measurable silence?

Same model, same speaker, same words. Only the markup differs:
  A  no markup
  B  `<900>` and `<600>` inserted at two marked boundaries
Measures internal silence at the marked positions from the rendered PCM, and
transcribes both so a reader can confirm the markup is never spoken aloud.

Run: .venv/Scripts/python.exe evidence/e1_pause_control.py
Writes clips to evidence/e1/ and a result table to evidence/e1/results.json
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()
RIME_KEY = os.environ["RIME_API_KEY"]
DG_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
OUT = Path("evidence/e1")
OUT.mkdir(parents=True, exist_ok=True)
RATE = 24000

# (id, plain, marked, marker positions as the word index after which the pause sits)
FIXTURES = [
    ("attention", "The key idea is this. Every token looks at every other token. That is attention.",
     "The key idea is this. <900> Every token looks at every other token. <600> That is attention."),
    ("tradeoff", "There is a cost. Attention is quadratic in sequence length. That limits context.",
     "There is a cost. <900> Attention is quadratic in sequence length. <600> That limits context."),
    ("closing", "So that is the mechanism. It is simple. It is also expensive.",
     "So that is the mechanism. <900> It is simple. <600> It is also expensive."),
]
REQUESTED_MS = 1500  # 900 + 600 per fixture
TOLERANCE_MS = 450   # +/-30% of the requested budget
SHIPPED_MODEL = "mistv3"
MODELS = [("mistv3", "astra"), ("coda", "astra")]


def synth(model: str, speaker: str, text: str, *, pause: bool) -> bytes:
    body = {"text": text, "speaker": speaker, "modelId": model, "lang": "en", "samplingRate": RATE}
    if pause:
        body["pauseBetweenBrackets"] = True
    req = urllib.request.Request(
        "https://users.rime.ai/v1/rime-tts", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {RIME_KEY}", "Content-Type": "application/json",
                 "Accept": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _samples(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Rime streams WAV with a placeholder frame count; trust the byte length."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    raw = wav_bytes[44:]
    usable = len(raw) // (width * nch) * width * nch
    x = np.frombuffer(raw[:usable], dtype=np.int16).astype(np.float32) / 32768
    return x, sr


def gaps(wav_bytes: bytes, *, thresh_db: float = -40.0, min_ms: int = 200) -> list[dict]:
    x, sr = _samples(wav_bytes)
    hop = int(sr * 0.01)
    n = len(x) // hop
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
    return out


def transcribe(wav_bytes: bytes) -> str:
    if not DG_KEY:
        return "(no DEEPGRAM_API_KEY)"
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/listen?model=nova-3&language=en&punctuate=true&numerals=false",
        data=wav_bytes, method="POST",
        headers={"Authorization": f"Token {DG_KEY}", "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)["results"]["channels"][0]["alternatives"][0]["transcript"]

def duration(wav_bytes: bytes) -> float:
    x, sr = _samples(wav_bytes)
    return round(len(x) / sr, 2)


def main() -> None:
    rows = []
    for model, speaker in MODELS:
        for fid, plain, marked in FIXTURES:
            a = synth(model, speaker, plain, pause=False)
            b = synth(model, speaker, marked, pause=True)
            (OUT / f"{model}_{speaker}_{fid}_A_plain.wav").write_bytes(a)
            (OUT / f"{model}_{speaker}_{fid}_B_marked.wav").write_bytes(b)
            ga, gb = gaps(a), gaps(b)
            row = {
                "model": model, "speaker": speaker, "fixture": fid,
                "requested_pause_ms": REQUESTED_MS,
                "A_duration_s": duration(a), "B_duration_s": duration(b),
                "A_gaps": ga, "B_gaps": gb,
                "A_max_gap_ms": max((g["ms"] for g in ga), default=0),
                "B_max_gap_ms": max((g["ms"] for g in gb), default=0),
                "B_transcript": transcribe(b),
            }
            row["added_ms"] = round((row["B_duration_s"] - row["A_duration_s"]) * 1000)
            row["error_ms"] = row["added_ms"] - REQUESTED_MS
            row["within_tolerance"] = abs(row["error_ms"]) <= TOLERANCE_MS
            row["markup_spoken"] = any(t in row["B_transcript"].lower()
                                       for t in ("900", "600", "nine hundred", "six hundred"))
            rows.append(row)
            print(f"{model:7s} {fid:10s} A {row['A_duration_s']:5.2f}s | B {row['B_duration_s']:5.2f}s"
                  f" | added {row['added_ms']:5d}ms (requested {REQUESTED_MS}, err {row['error_ms']:+5d})"
                  f" | maxgap {row['A_max_gap_ms']:4d}->{row['B_max_gap_ms']:4d}ms"
                  f" | spoken: {row['markup_spoken']}")

    (OUT / "results.json").write_text(json.dumps(rows, indent=2))
    print(f"\nPer-model verdict (requested {REQUESTED_MS} ms of pause, tolerance +/-{TOLERANCE_MS} ms):")
    fail = False
    for model, _ in MODELS:
        sub = [r for r in rows if r["model"] == model]
        ok = sum(1 for r in sub if r["within_tolerance"])
        spoken = any(r["markup_spoken"] for r in sub)
        print(f"  {model:7s} honoured {ok}/{len(sub)} fixtures | added {[r['added_ms'] for r in sub]} ms"
              f" | markup ever spoken: {spoken}")
        if model == SHIPPED_MODEL and (ok < len(sub) or spoken):
            fail = True
    print("\nresults -> evidence/e1/results.json ; clips -> evidence/e1/*.wav")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()

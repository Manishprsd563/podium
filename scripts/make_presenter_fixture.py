"""Build a realistic presenter audio fixture for the end-to-end smoke test.

Rendered with Rime so the content is known exactly, with deliberate 2.5 s
thinking pauses spliced in - those are what break a default VAD turn policy.
Written at 48 kHz mono because Chrome's --use-file-for-fake-audio-capture
expects that rate.

Run: .venv/Scripts/python.exe scripts/make_presenter_fixture.py
Writes evidence/smoke/presenter_48k.wav
Needs: RIME_API_KEY (see .env.example)
"""
from __future__ import annotations

import json
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("RIME_API_KEY", "")
if not KEY:
    raise SystemExit("RIME_API_KEY missing -- copy .env.example to .env and fill it in")
OUT = Path("evidence/smoke")
OUT.mkdir(parents=True, exist_ok=True)

LINES = [
    ("Okay. So today I want to explain self attention.", 0.6),
    ("The key idea is that every token looks at every other token in the sequence.", 0.6),
    ("Um, so each token produces a query, a key, and a value.", 2.5),
    ("And, uh, the dot product of the query and the key basically gives you the attention weight.", 0.6),
    ("The cost is quadratic in sequence length, so it gets expensive for long inputs.", 2.5),
    ("That is, that is basically the whole mechanism. I'm done.", 0.4),
]


def synth(text: str) -> np.ndarray:
    body = {"text": text, "speaker": "astra", "modelId": "mistv3", "lang": "en", "samplingRate": 24000}
    req = urllib.request.Request(
        "https://users.rime.ai/v1/rime-tts", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
                 "Accept": "audio/wav"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()[44:]
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)


def main() -> None:
    parts: list[np.ndarray] = []
    for text, gap_s in LINES:
        parts.append(synth(text))
        parts.append(np.zeros(int(24000 * gap_s), dtype=np.int16))
    sig = np.concatenate(parts)
    up = np.interp(np.arange(len(sig) * 2) / 2, np.arange(len(sig)), sig).astype(np.int16)
    path = OUT / "presenter_48k.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(up.tobytes())
    print(f"wrote {path} {len(up)/48000:.2f}s (24k source {len(sig)/24000:.2f}s)")


if __name__ == "__main__":
    main()

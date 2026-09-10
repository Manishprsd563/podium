"""Decisive model test: does the pause markup become silence, or spoken digits?

`<900>` renders as a pause only on the Mist family; Coda has no SSML and may
read the number aloud. Transcribing each rendered clip is the only honest check.
Run after rime_probe.py: .venv/Scripts/python.exe scripts/pause_verify.py
Reads evidence/probe/*_pause.wav and *_slow.wav (written by rime_probe.py).
Needs: DEEPGRAM_API_KEY (see .env.example)
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("DEEPGRAM_API_KEY", "")
if not KEY:
    raise SystemExit("DEEPGRAM_API_KEY missing -- copy .env.example to .env and fill it in")
URL = "https://api.deepgram.com/v1/listen?model=nova-3&language=en&punctuate=true&numerals=false"
DIGITS = re.compile(r"\b(nine hundred|six hundred|900|600|nine zero zero|six zero zero)\b", re.I)


def transcribe(path: Path) -> str:
    req = urllib.request.Request(URL, data=path.read_bytes(), method="POST",
                                 headers={"Authorization": f"Token {KEY}", "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["results"]["channels"][0]["alternatives"][0]["transcript"]


if __name__ == "__main__":
    for clip in sorted(Path("evidence/probe").glob("*_pause.wav")):
        text = transcribe(clip)
        leak = DIGITS.search(text)
        print(f"{clip.stem:28s} {'LEAKS DIGITS' if leak else 'clean       '} | {text}")
    print("\nAlso check the slow renders were not distorted:")
    for clip in sorted(Path("evidence/probe").glob("*_slow.wav")):
        print(f"{clip.stem:28s} | {transcribe(clip)}")

"""Gate 0: probe the exact Rime configuration Podium will ship.

Renders fixed sentences on Coda and Mist v3 over REST (WAV), measures duration,
first-byte latency and silence gaps (to verify `<750>` pause markup renders),
and checks word timestamps over /ws3. Clips go to evidence/probe/.

Run:  .venv/Scripts/python.exe scripts/rime_probe.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ.get("RIME_API_KEY", "")
if not KEY or KEY.startswith("your_"):
    sys.exit("RIME_API_KEY missing in .env")

REST = "https://users.rime.ai/v1/rime-tts"
WS3 = "wss://users-ws.rime.ai/ws3"
OUT = Path("evidence/probe")
OUT.mkdir(parents=True, exist_ok=True)
RATE = 24000

CASES = [
    ("plain", "The attention mechanism lets every token look at every other token in the sequence."),
    ("fillers", "So, um, the attention mechanism, uh, it basically lets every token, like, look at the other tokens."),
    ("pause", "The key idea is this. <900> Every token looks at every other token. <600> That is attention."),
    ("terms", "Kubernetes. Nginx. Idempotency. Dijkstra. PostgreSQL. Byzantine fault tolerance."),
    ("slow", "The attention mechanism lets every token look at every other token in the sequence."),
]
MODELS = [("coda", "astra"), ("mistv3", "astra"), ("mistv3", "cove")]


def synth(model: str, speaker: str, text: str, *, pause: bool, scale: float) -> tuple[bytes, float]:
    body = {"text": text, "speaker": speaker, "modelId": model, "lang": "en",
            "samplingRate": RATE, "timeScaleFactor": scale}
    if pause:
        body["pauseBetweenBrackets"] = True
    req = urllib.request.Request(
        REST, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "Accept": "audio/wav"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        first = r.read(1)
        ttfb = time.perf_counter() - t0
        return first + r.read(), ttfb


def pcm(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    return data, sr


def silences(x: np.ndarray, sr: int, thresh_db: float = -40, min_ms: int = 250) -> list[tuple[float, float]]:
    """Internal silence gaps (start_s, dur_s) longer than min_ms, ignoring leading/trailing."""
    win = int(sr * 0.02)
    n = len(x) // win
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    quiet = db < thresh_db
    gaps, start = [], None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            gaps.append((start, i)); start = None
    out = []
    for a, b in gaps:
        if a == 0:
            continue
        dur = (b - a) * 0.02
        if dur * 1000 >= min_ms:
            out.append((round(a * 0.02, 2), round(dur, 2)))
    return out


async def ws3_timestamps(model: str, speaker: str, text: str) -> dict:
    import websockets
    url = f"{WS3}?speaker={speaker}&modelId={model}&lang=en&audioFormat=pcm&samplingRate={RATE}&segment=never"
    hdr = {"Authorization": f"Bearer {KEY}"}
    try:
        ws = await websockets.connect(url, additional_headers=hdr)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=hdr)
    t0 = time.perf_counter()
    words, first_chunk, nbytes = None, None, 0
    async with ws:
        await ws.send(json.dumps({"text": text, "contextId": "probe"}))
        await ws.send(json.dumps({"operation": "eos"}))
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            if t == "chunk":
                if first_chunk is None:
                    first_chunk = time.perf_counter() - t0
                nbytes += len(base64.b64decode(m["data"]))
            elif t == "timestamps":
                words = m["word_timestamps"]
            elif t == "done":
                break
            elif t == "error":
                return {"error": m.get("message")}
    return {"ttfa_s": round(first_chunk or -1, 3), "audio_s": round(nbytes / 2 / RATE, 2),
            "words": (words or {}).get("words"), "starts": (words or {}).get("start")}


def main() -> None:
    rows = []
    for model, speaker in MODELS:
        for name, text in CASES:
            scale = 1.3 if name == "slow" else 1.0
            try:
                wav, ttfb = synth(model, speaker, text, pause=(name == "pause"), scale=scale)
            except urllib.error.HTTPError as e:
                rows.append((model, speaker, name, f"HTTP {e.code}: {e.read()[:120]!r}"))
                continue
            path = OUT / f"{model}_{speaker}_{name}.wav"
            path.write_bytes(wav)
            x, sr = pcm(wav)
            rows.append((model, speaker, name,
                         f"{len(x)/sr:5.2f}s ttfb={ttfb*1000:4.0f}ms gaps={silences(x, sr)}"))
    print("\nREST renders (evidence/probe/*.wav):")
    for r in rows:
        print("  ", *r)

    print("\n/ws3 word timestamps:")
    for model, speaker in MODELS[:2]:
        res = asyncio.run(ws3_timestamps(model, speaker, CASES[1][1]))
        print("  ", model, speaker, json.dumps(res)[:400])

    print("\nInterpretation:")
    print("  pause case: expect >=2 gaps of ~0.9s and ~0.6s on mistv3; expect none (or literal reading) on coda.")
    print("  slow case: expect ~1.3x the plain duration if timeScaleFactor>1 = slower.")
    print("  fillers case: word timestamps should list 'um'/'uh' if Rime voices them.")
    print("  Listen to the clips before choosing the shipped model/speaker.")


if __name__ == "__main__":
    main()

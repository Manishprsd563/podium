"""Gate 0: Deepgram keeps fillers with word timestamps; LLM endpoint returns strict JSON.

Feeds the Rime-rendered filler clip (from rime_probe.py) to Deepgram prerecorded
with filler_words=true, then asks the LLM for a JSON object. The LLM half
(llm()) targets the opencode-go gateway via agent.llm_config.API_KEY/BASE_URL/
MODEL, which GATE0.md records as REJECTED in favor of LiveKit Inference and
which agent/llm_config.py no longer exports; it now prints a clear message
instead of crashing. deepgram() is unaffected and still exercises the shipped
Deepgram config.

Run after rime_probe.py:  .venv/Scripts/python.exe scripts/stt_llm_probe.py
Writes: nothing (stdout only). Needs: DEEPGRAM_API_KEY (see .env.example).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def deepgram() -> None:
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key or key.startswith("your_"):
        print("DEEPGRAM_API_KEY missing"); return
    clip = next(Path("evidence/probe").glob("mistv3_*_fillers.wav"), None) \
        or next(Path("evidence/probe").glob("*_fillers.wav"), None)
    if not clip:
        print("no fillers clip; run rime_probe.py first"); return
    url = "https://api.deepgram.com/v1/listen?model=nova-3&language=en&filler_words=true&punctuate=true"
    req = urllib.request.Request(url, data=clip.read_bytes(), method="POST",
                                 headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    alt = res["results"]["channels"][0]["alternatives"][0]
    words = alt["words"]
    fillers = [w for w in words if w["word"].lower().strip(",.") in {"um", "uh", "mm", "mhm"}]
    print(f"Deepgram ({time.perf_counter()-t0:.1f}s) on {clip.name}")
    print("  transcript:", alt["transcript"])
    print("  confidence:", round(alt["confidence"], 3), "| words:", len(words))
    print("  fillers found:", [(w["word"], w["start"], w["end"]) for w in fillers])
    print("  PASS" if fillers else "  FAIL: no fillers in transcript")


def llm() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from agent.llm_config import API_KEY, BASE_URL, MODEL, extra_headers
    except ImportError:
        print("agent.llm_config no longer exports API_KEY/BASE_URL/MODEL/extra_headers -- "
              "this probe targeted the opencode-go gateway, which GATE0.md records as "
              "rejected in favor of LiveKit Inference. See scripts/inference_probe.py "
              "and scripts/judge_json_probe.py for the current LLM checks.")
        return

    if not API_KEY or API_KEY.startswith("your_"):
        print("OPENAI_API_KEY missing"); return
    body = {
        "model": MODEL, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Reply with a JSON object only. No prose, no code fences."},
            {"role": "user", "content": 'Return {"scores": {"pace": int 1-5, "clarity": int 1-5}, '
                                        '"improvement": {"quote": str, "v2_text": str}} for a presenter who said: '
                                        '"So, um, the attention mechanism, uh, it basically lets every token, like, look at the other tokens."'},
        ],
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", **extra_headers()})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"LLM HTTP {e.code}: {e.read()[:300]!r}"); return
    content = res["choices"][0]["message"]["content"]
    print(f"LLM {MODEL} @ {BASE_URL} ({time.perf_counter()-t0:.1f}s)")
    try:
        obj = json.loads(content)
        print("  JSON OK:", json.dumps(obj)[:300])
    except json.JSONDecodeError:
        print("  FAIL: not JSON ->", content[:300])


if __name__ == "__main__":
    deepgram()
    print()
    llm()

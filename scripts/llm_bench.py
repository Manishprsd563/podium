"""Pick the LLM for Podium: measure first-token and full-response latency per model.

Two workloads: a short coach turn (latency-critical, streamed) and a judge call
(strict JSON, quality-critical). Targets the opencode-go OpenAI-compatible
gateway (`agent.llm_config.API_KEY`/`BASE_URL`/`extra_headers`), the path
GATE0.md records as REJECTED in favor of LiveKit Inference -- see
scripts/inference_probe.py and scripts/judge_json_probe.py for the LLM
benchmarks of the shipped path. agent/llm_config.py no longer exports those
names, so this script now exits with a clear message instead of an
ImportError; kept for reference in case the gateway comparison is redone.

Run: .venv/Scripts/python.exe scripts/llm_bench.py
Writes: nothing (stdout only). Needs: OPENAI_API_KEY plus a restored
opencode-go BASE_URL/extra_headers in agent/llm_config.py (currently absent).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from agent.llm_config import API_KEY, BASE_URL, extra_headers  # noqa: E402
except ImportError:
    API_KEY = BASE_URL = None  # noqa: E402
    extra_headers = None  # noqa: E402

CANDIDATES = [
    "glm-5.3-flash", "glm-5.3", "kimi-k2.7-code", "minimax-m2.5",
    "gpt-5.4-nano", "gpt-5.4-mini", "gemini-3-flash", "claude-haiku-4.5",
]

COACH = [
    {"role": "system", "content": "You are a presentation coach. Reply in at most two short spoken sentences. No lists."},
    {"role": "user", "content": "How was my pacing? I spoke 178 words per minute with 6 filler words in 60 seconds."},
]
JUDGE = [
    {"role": "system", "content": "Reply with a JSON object only."},
    {"role": "user", "content": 'Return {"scores":{"pace":int,"clarity":int,"structure":int},'
                                '"improvements":[{"quote":str,"issue":str,"v2_text":str}]} '
                                'judging: "So, um, the attention mechanism, uh, it basically lets every token, '
                                'like, look at the other tokens. And yeah, that is, that is basically it."'},
]


def post(model: str, messages: list, *, stream: bool, json_mode: bool, max_tokens: int):
    """Streaming path tracks first-reasoning-token and first-content-token separately:
    reasoning-heavy models can emit a long reasoning_content burst before any visible
    content, so ttft must be measured from content, not from the first token of any kind."""
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if stream:
        body["stream"] = True
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", **extra_headers()})
    t0 = time.perf_counter()
    first_any, first_content, text = None, None, []
    with urllib.request.urlopen(req, timeout=120) as r:
        if not stream:
            data = json.load(r)
            el = time.perf_counter() - t0
            msg = data["choices"][0]["message"]
            return el, el, msg.get("content") or ""
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                choices = json.loads(payload).get("choices") or []
            except json.JSONDecodeError:
                continue
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if (delta.get("reasoning_content") or delta.get("reasoning")) and first_any is None:
                first_any = time.perf_counter() - t0
            chunk = delta.get("content")
            if chunk:
                now = time.perf_counter() - t0
                first_any = first_any if first_any is not None else now
                first_content = first_content if first_content is not None else now
                text.append(chunk)
    return (first_content if first_content is not None else -1.0), time.perf_counter() - t0, "".join(text)


def bench(model: str) -> None:
    try:
        ttft, total, out = post(model, COACH, stream=True, json_mode=False, max_tokens=80)
        coach = f"ttft={ttft:5.2f}s total={total:5.2f}s | {out.strip()[:70]!r}"
    except urllib.error.HTTPError as e:
        print(f"{model:20s} coach HTTP {e.code} {e.read()[:80]!r}"); return
    except Exception as e:
        print(f"{model:20s} coach ERR {type(e).__name__}: {e}"); return
    try:
        _, total, out = post(model, JUDGE, stream=False, json_mode=True, max_tokens=500)
        ok = "JSON-OK" if _valid(out) else f"JSON-BAD {out[:60]!r}"
        judge = f"total={total:5.2f}s {ok}"
    except urllib.error.HTTPError as e:
        judge = f"HTTP {e.code} {e.read()[:60]!r}"
    except Exception as e:
        judge = f"ERR {type(e).__name__}"
    print(f"{model:20s} coach {coach}\n{'':20s} judge {judge}")


def _valid(text: str) -> bool:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(obj.get("scores"), dict) and isinstance(obj.get("improvements"), list)


if __name__ == "__main__":
    if BASE_URL is None:
        raise SystemExit(
            "agent.llm_config no longer exports API_KEY/BASE_URL/extra_headers -- "
            "this probe targeted the opencode-go gateway, which GATE0.md records as "
            "rejected in favor of LiveKit Inference. See scripts/inference_probe.py "
            "and scripts/judge_json_probe.py for the current LLM benchmarks."
        )
    models = sys.argv[1:] or CANDIDATES
    for m in models:
        bench(m)

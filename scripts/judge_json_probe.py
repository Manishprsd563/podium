"""Judge reliability on LiveKit Inference: strict JSON, N runs, measured latency.

Podium's judge must return a parseable scorecard every time. This runs the real
judge-shaped prompt repeatedly and reports parse rate and latency.
Run: .venv/Scripts/python.exe scripts/judge_json_probe.py [runs]
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time

from dotenv import load_dotenv
from livekit.agents import inference
from livekit.agents.llm import ChatContext

load_dotenv()

MODELS = ["openai/gpt-5.4-mini", "google/gemma-4-31b-it", "openai/gpt-4.1-nano"]

SYSTEM = (
    "You judge a rehearsed presentation. Reply with ONE JSON object and nothing else.\n"
    "Schema: {\"scores\":{\"delivery\":1-5,\"clarity\":1-5,\"structure\":1-5,\"slide_connection\":1-5},"
    "\"summary\":str,\"improvements\":[{\"quote\":str,\"issue\":str,\"rubric_ref\":str,"
    "\"v2_text\":str,\"alternative\":str}],\"terms_to_drill\":[str]}\n"
    "Exactly 3 improvements. quote must be verbatim from the transcript."
)
USER = (
    "SLIDE 1 — 'Self-attention': every token attends to every other token; O(n^2) cost; "
    "queries, keys, values.\n"
    "METRICS: 178 wpm, 6 fillers in 62s, longest pause 3.4s, 41s on slide 1 of 60s budget.\n"
    "TRANSCRIPT: \"So, um, the attention mechanism, uh, it basically lets every token, like, look at "
    "the other tokens. And yeah, that is, that is basically it. The cost is quadratic which is, um, "
    "n squared, so it gets expensive. Queries keys values, those are the three things.\""
)


def parse(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    ok = (isinstance(obj.get("scores"), dict)
          and len(obj.get("improvements") or []) == 3
          and all({"quote", "issue", "v2_text"} <= set(i) for i in obj["improvements"]))
    return obj if ok else None


async def run(model: str, runs: int) -> None:
    llm = inference.LLM(model=model)
    good, times = 0, []
    for _ in range(runs):
        ctx = ChatContext.empty()
        ctx.add_message(role="system", content=SYSTEM)
        ctx.add_message(role="user", content=USER)
        t0 = time.perf_counter()
        out = []
        try:
            async with llm.chat(chat_ctx=ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        out.append(chunk.delta.content)
        except Exception as e:
            print(f"  {model} ERR {type(e).__name__}: {str(e)[:100]}")
            continue
        times.append(time.perf_counter() - t0)
        obj = parse("".join(out))
        if obj:
            good += 1
            last = obj
    await llm.aclose()
    avg = sum(times) / len(times) if times else -1
    print(f"{model:24s} parsed {good}/{runs}  avg={avg:5.2f}s  max={max(times, default=-1):5.2f}s")
    if good:
        print("   sample:", json.dumps(last["improvements"][0])[:220])


async def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for m in MODELS:
        await run(m, runs)


if __name__ == "__main__":
    asyncio.run(main())

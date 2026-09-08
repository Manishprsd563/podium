"""Check whether LiveKit Inference can serve the LLM with the existing LiveKit keys.

The opencode-go gateway proved slow and non-deterministic (reasoning-only
responses, intermittently empty content). Inference needs no extra credential.
Run: .venv/Scripts/python.exe scripts/inference_probe.py
"""
from __future__ import annotations

import asyncio
import time

from dotenv import load_dotenv
from livekit.agents import inference
from livekit.agents.llm import ChatContext

load_dotenv()

CANDIDATES = ["openai/gpt-4.1-mini", "openai/gpt-5.4-mini", "openai/gpt-4.1-nano",
              "google/gemma-4-31b-it", "openai/chat-latest"]

PROMPT = ("You are a presentation coach. Reply in at most two short spoken sentences. No lists. "
          "How was my pacing? I spoke 178 words per minute with 6 filler words in 60 seconds.")


async def probe(model: str) -> None:
    llm = inference.LLM(model=model)
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content=PROMPT)
    t0 = time.perf_counter()
    first, out = None, []
    try:
        async with llm.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                delta = chunk.delta.content if chunk.delta else None
                if delta:
                    if first is None:
                        first = time.perf_counter() - t0
                    out.append(delta)
        print(f"{model:24s} ttft={first or -1:5.2f}s total={time.perf_counter()-t0:5.2f}s "
              f"| {''.join(out).strip()[:80]!r}")
    except Exception as e:
        print(f"{model:24s} ERR {type(e).__name__}: {str(e)[:140]}")
    finally:
        await llm.aclose()


async def main() -> None:
    for m in CANDIDATES:
        await probe(m)


if __name__ == "__main__":
    asyncio.run(main())

"""Which LLM serves which role, and why.

Gate 0 measured both available paths (see GATE0.md):

  opencode-go gateway   coach 6.7 s TTFT, judge 12.7 s, JSON 0/3, needs
                        Cloudflare-specific headers -> rejected
  LiveKit Inference     coach 0.67 s TTFT (gemma), judge 3.2 s, JSON 3/3,
                        authenticated by the LiveKit key we already hold

So Podium uses LiveKit Inference for both roles: a fast model for spoken coach
turns, a stronger one for the JSON scorecard.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

COACH_MODEL = os.environ.get("COACH_MODEL", "google/gemma-4-31b-it")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "openai/gpt-5.4-mini")


def coach_llm():
    """Latency-critical conversational turns."""
    from livekit.agents import inference

    return inference.LLM(model=COACH_MODEL)


def judge_llm():
    """Quality-critical strict-JSON scoring; not on the speech path."""
    from livekit.agents import inference

    return inference.LLM(model=JUDGE_MODEL)

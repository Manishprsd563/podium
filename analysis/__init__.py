"""Podium's pure analysis layer.

Everything in this package is importable and callable without a LiveKit room
or transport: `metrics.py` and `render.py` do no LiveKit imports at all;
`judge.py` and `deck.py` only reach into `agent.llm_config`, which itself
only imports `livekit.agents.inference` lazily inside function bodies. No
module here may call `session.say` or otherwise touch a room -- both
`agent/session_agent.py` and `evidence/*.py` call these functions directly.
"""
from __future__ import annotations

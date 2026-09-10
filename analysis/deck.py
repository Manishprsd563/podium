"""Generate or normalise a deck. CONTRACTS.md §1 `deck` shape.

`generate_deck` calls the judge-quality LLM (latency doesn't matter here, the
deck is built once before the timer starts). `normalise_deck` is pure and
handles both an uploaded deck and generate_deck's own LLM output.
"""
from __future__ import annotations

import json
import re

MAX_SLIDES = 6

_TECH_TOKENS = {
    "api", "gpu", "cpu", "llm", "json", "http", "https", "sql", "css", "html",
    "ai", "ml", "gan", "cnn", "rnn", "tcp", "ip", "url", "ui", "ux", "nlp",
}

_DECK_SYSTEM = (
    "You write a short teaching deck for a rehearsal coaching tool. Reply "
    "with ONE JSON object and nothing else -- no prose, no markdown fences.\n"
    'Shape: {"slides":[{"title":"...", "bullets":["...","..."], "terms":["..."]}, ...]}\n'
    "Exactly 3 slides. Each slide: a short title (<=6 words), 2-4 short "
    "bullets (plain spoken phrases, no trailing punctuation), and 2-5 terms "
    "-- the domain vocabulary the presenter should be able to pronounce and "
    "define, drawn from words that actually appear in the bullets or are "
    "essential jargon for the topic."
)


class DeckError(RuntimeError):
    """Raised when the deck LLM never returns parseable slides."""


def _parse(raw: str) -> dict | None:
    """Best-effort JSON parse of an LLM response: strips ``` fences, then
    falls back to the first {...} span if the whole string doesn't parse
    as-is. None if nothing usable is found."""
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None


async def _call_llm(system: str, user: str) -> str:
    """Stream one deck-generation completion via
    `agent.llm_config.judge_llm()` and return the concatenated text."""
    from agent.llm_config import judge_llm
    from livekit.agents.llm import ChatContext

    llm = judge_llm()
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content=system)
    ctx.add_message(role="user", content=user)
    chunks: list[str] = []
    try:
        async with llm.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    chunks.append(chunk.delta.content)
    finally:
        await llm.aclose()
    return "".join(chunks)


def _derive_terms(bullets: list[str]) -> list[str]:
    """Capitalised words (not sentence-initial) and known-technical tokens, in first-seen order."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for bullet in bullets:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9+_.\-]*", bullet)
        for i, tok in enumerate(tokens):
            lower = tok.lower()
            is_acronym = tok.isupper() and len(tok) > 1
            is_capitalized = i > 0 and tok[0].isupper() and not tok.isupper()
            is_technical = lower in _TECH_TOKENS
            if not (is_acronym or is_capitalized or is_technical):
                continue
            if lower not in seen_lower:
                seen.append(tok)
                seen_lower.add(lower)
    return seen[:6]


def normalise_deck(slides: list[dict], budget_s: int, *, source: str = "upload", topic: str = "") -> dict:
    """CONTRACTS.md §1 deck shape from a client-supplied or LLM-supplied slide list."""
    out_slides = []
    for i, s in enumerate(list(slides)[:MAX_SLIDES], start=1):
        title = str(s.get("title") or f"Slide {i}").strip()
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()]
        raw_terms = s.get("terms")
        terms = [str(t).strip() for t in raw_terms if str(t).strip()] if raw_terms else _derive_terms(bullets)
        out_slides.append({
            "index": i,
            "title": title,
            "bullets": bullets,
            "terms": terms,
            "notes": str(s.get("notes", "")),
        })
    return {
        "source": source,
        "topic": topic or (out_slides[0]["title"] if out_slides else ""),
        "budget_s": budget_s,
        "slides": out_slides,
    }


async def generate_deck(topic: str, level: str, budget_s: int) -> dict:
    """CONTRACTS.md §1 deck, source="generated". 3 slides via the judge-quality LLM."""
    user = f"Topic: {topic}\nAudience level: {level}\nTotal presentation time budget: {budget_s} seconds."
    raw = await _call_llm(_DECK_SYSTEM, user)
    obj = _parse(raw)
    if obj is None or not obj.get("slides"):
        retry_user = user + "\n\nReturn ONLY the JSON object. No explanation, no markdown fences."
        raw = await _call_llm(_DECK_SYSTEM, retry_user)
        obj = _parse(raw)
    if obj is None or not obj.get("slides"):
        raise DeckError(f"deck LLM did not return parseable slides after retry: {raw[:200]!r}")
    return normalise_deck(obj["slides"][:3], budget_s, source="generated", topic=topic)

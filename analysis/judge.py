"""LLM judgment against the skill-file rubric. CONTRACTS.md §3.

Pure-ish: takes deck + metrics + transcript, returns strict validated JSON.
Never touches the room; only reaches into `agent.llm_config` for the model.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills" / "judge"
RUBRIC_FILES = ["delivery.md", "clarity.md", "structure.md", "slide-connection.md", "pronunciation.md", "feedback-style.md"]

_RUBRIC_CACHE: dict[str, str] = {}

_VALID_PAUSE = re.compile(r"<\d+>")
_ANY_TAG = re.compile(r"<[^>]*>")

SCHEMA_INSTRUCTIONS = (
    "Return exactly this JSON shape and nothing else:\n"
    '{"scores":{"delivery":1-5,"clarity":1-5,"structure":1-5,"slide_connection":1-5},'
    '"summary":"one or two spoken sentences, under 40 words",'
    '"improvements":[{"quote":"verbatim span copied from TRANSCRIPT",'
    '"span":{"start":0.0,"end":0.0},"issue":"one spoken sentence naming the problem",'
    '"rubric_ref":"file.md#anchor","v2_text":"cleaned wording, <=25 words",'
    '"v3_markup":"same point with <NNN> pause markup, at most 3 markers",'
    '"alternative":"a different way to open the same idea"}],'
    '"terms_to_drill":["term", ...]}\n'
    "Exactly 3 improvements. Every quote MUST be copied verbatim, "
    "character-for-character, from TRANSCRIPT -- never paraphrase it."
)


class JudgeError(RuntimeError):
    """Raised when the judge LLM never returns parseable JSON."""


def _load_rubric(name: str) -> str:
    if name not in _RUBRIC_CACHE:
        _RUBRIC_CACHE[name] = (SKILLS_DIR / name).read_text(encoding="utf-8")
    return _RUBRIC_CACHE[name]


def _full_rubric() -> str:
    return "\n\n".join(f"# {name}\n{_load_rubric(name)}" for name in RUBRIC_FILES)


def _slugify(heading: str) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", s).strip("-")


def _extract_section(markdown: str, anchor: str) -> str | None:
    lines = markdown.splitlines()
    headings = [(i, line, len(line) - len(line.lstrip("#"))) for i, line in enumerate(lines) if line.lstrip().startswith("#")]
    for idx, (line_no, line, level) in enumerate(headings):
        if _slugify(line.lstrip("#").strip()) != anchor:
            continue
        end = len(lines)
        for next_line_no, _next_line, next_level in headings[idx + 1:]:
            if next_level <= level:
                end = next_line_no
                break
        return "\n".join(lines[line_no:end]).strip()
    return None


def rubric_text(ref: str) -> str:
    """Resolve a `rubric_ref` like "delivery.md#filler-rate" to its section text.

    Used by the agent's `explain_rubric` tool. Missing/whole-file refs fall
    back to the entire file.
    """
    name, _, anchor = ref.partition("#")
    text = _load_rubric(name)
    if not anchor:
        return text
    return _extract_section(text, anchor) or text


def _build_prompt(deck: dict, slide: dict | None, transcript_text: str, metrics: dict) -> tuple[str, str]:
    system = (
        "You are Podium's presentation judge. Score the rehearsal strictly "
        "against the rubric below. Output ONE JSON object and nothing else "
        "-- no prose, no markdown fences.\n\n" + _full_rubric() + "\n\n" + SCHEMA_INSTRUCTIONS
    )
    if slide:
        bullets = "; ".join(slide.get("bullets", []))
        slide_desc = f"slide {slide.get('index')} -- '{slide.get('title')}': {bullets}"
    else:
        slide_desc = "whole presentation (all slides)"
    deck_desc = f"topic '{deck.get('topic', '')}', budget {deck.get('budget_s', '?')}s"
    user = (
        f"DECK: {deck_desc}\n"
        f"CURRENT SLIDE: {slide_desc}\n"
        f"METRICS (already computed, do not re-estimate): {json.dumps(metrics)}\n"
        f'TRANSCRIPT: "{transcript_text}"'
    )
    return system, user


def _parse(raw: str) -> dict | None:
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


async def _call_llm(system: str, user: str, model: str | None) -> str:
    from livekit.agents.llm import ChatContext

    if model:
        from livekit.agents import inference

        llm = inference.LLM(model=model)
    else:
        from agent.llm_config import judge_llm

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


def _clamp_score(v: object) -> int:
    try:
        n = int(round(float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 3
    return max(1, min(5, n))


def _last_punct_idx(s: str) -> int:
    return max((s.rfind(c) for c in ".!?,"), default=-1)


def _first_punct_idx(s: str) -> int:
    idxs = [i for i in (s.find(c) for c in ".!?,") if i != -1]
    return min(idxs) if idxs else -1


def _relocate_markers(text: str) -> str:
    """Move each `<NNN>` marker to immediately follow the nearest sentence or
    clause punctuation. Measured fact: Mist v3 only renders a pause reliably
    right after `.`, `!`, `?`, or `,` -- a marker stranded mid-clause with no
    adjacent punctuation renders as little to no silence (see GATE0.md).
    "Verify, do not trust": rather than hope the judge LLM places pauses
    where they render, relocate them to a position known to work."""
    segments = _VALID_PAUSE.split(text)
    markers = _VALID_PAUSE.findall(text)
    if not markers:
        return text
    out: list[str] = [segments[0]]
    for idx, marker in enumerate(markers):
        following = segments[idx + 1] if idx + 1 < len(segments) else ""
        before = out[-1]
        if before.rstrip() and before.rstrip()[-1] in ".!?,":
            out[-1] = before.rstrip() + " " + marker
            out.append(following)
            continue
        k = _last_punct_idx(before)
        if k != -1:
            head, tail = before[:k + 1], before[k + 1:]
            out[-1] = head.rstrip() + " " + marker
            out.append(tail + following)
            continue
        j = _first_punct_idx(following)
        if j != -1:
            head, tail = following[:j + 1], following[j + 1:]
            out[-1] = (before + head).rstrip() + " " + marker
            out.append(tail)
        else:
            out[-1] = before + following  # no punctuation anywhere -- drop this marker
    return "".join(out)


def _sanitize_v3_markup(text: str) -> str:
    cleaned = _ANY_TAG.sub(lambda m: m.group(0) if _VALID_PAUSE.fullmatch(m.group(0)) else "", text)
    count = 0

    def _cap(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return m.group(0) if count <= 3 else ""

    cleaned = _VALID_PAUSE.sub(_cap, cleaned)
    cleaned = _relocate_markers(cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _strip_markup(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _ANY_TAG.sub("", text)).strip()


def _validate(obj: dict, transcript_text: str) -> dict:
    scores_in = obj.get("scores") or {}
    scores = {k: _clamp_score(scores_in.get(k, 3)) for k in ("delivery", "clarity", "structure", "slide_connection")}

    haystack = transcript_text.lower()
    kept = [imp for imp in (obj.get("improvements") or []) if str(imp.get("quote", "")).lower() and str(imp.get("quote", "")).lower() in haystack]

    improvements = []
    for i, imp in enumerate(kept, start=1):
        span = imp.get("span") or {}
        try:
            start, end = float(span.get("start", 0.0)), float(span.get("end", 0.0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        improvements.append({
            "id": f"imp_{i}",
            "quote": str(imp["quote"]),
            "span": {"start": start, "end": end},
            "issue": str(imp.get("issue", "")).strip(),
            "rubric_ref": str(imp.get("rubric_ref", "")).strip(),
            "v2_text": _strip_markup(str(imp.get("v2_text", ""))),
            "v3_markup": _sanitize_v3_markup(str(imp.get("v3_markup", ""))),
            "alternative": str(imp.get("alternative", "")).strip(),
        })

    return {
        "scores": scores,
        "summary": str(obj.get("summary", "")).strip(),
        "improvements": improvements,
        "terms_to_drill": [str(t) for t in (obj.get("terms_to_drill") or [])],
    }


async def judge(deck: dict, slide: dict | None, transcript_text: str, words: list[dict], metrics: dict, *, model: str | None = None) -> dict:
    """CONTRACTS.md §3. `words` are unused directly (metrics already derives
    from them) but kept in the signature so callers can pass span-consistency
    context in future without breaking the call site."""
    del words
    system, user = _build_prompt(deck, slide, transcript_text, metrics)
    raw = await _call_llm(system, user, model)
    obj = _parse(raw)
    if obj is None:
        retry_user = user + "\n\nReturn ONLY the JSON object. No explanation, no markdown fences."
        raw = await _call_llm(system, retry_user, model)
        obj = _parse(raw)
    if obj is None:
        raise JudgeError(f"judge LLM did not return parseable JSON after retry: {raw[:200]!r}")
    return _validate(obj, transcript_text)

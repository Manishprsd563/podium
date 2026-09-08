"""Acceptance smoke test for the analysis/* slice.

Exercises metrics.compute, judge.judge, render.render_variants and
deck.generate_deck against a real Rime-rendered fixture clip, transcribed
live with the shipped Deepgram config so the word timings/confidences are
real, not fabricated.

Run: .venv/Scripts/python.exe scripts/analysis_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from analysis import deck as deck_mod
from analysis import judge as judge_mod
from analysis import metrics as metrics_mod
from analysis import render as render_mod

load_dotenv()

FIXTURE_WAV = Path("evidence/e1/mistv3_astra_attention_A_plain.wav")
OUT_DIR = Path("scripts/_smoke_out/clips")


def transcribe_words(wav_path: Path) -> tuple[str, list[dict]]:
    """Real Deepgram nova-3 transcript + word timings/confidences for the fixture clip,
    using the exact shipped STT config (filler_words, punctuate)."""
    key = os.environ["DEEPGRAM_API_KEY"]
    url = (
        "https://api.deepgram.com/v1/listen?model=nova-3&language=en"
        "&punctuate=true&filler_words=true&numerals=false"
    )
    req = urllib.request.Request(
        url, data=wav_path.read_bytes(), method="POST",
        headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        payload = json.load(r)
    alt = payload["results"]["channels"][0]["alternatives"][0]
    text = alt["transcript"]
    words = [{"w": w["word"], "start": w["start"], "end": w["end"], "conf": w["confidence"]} for w in alt["words"]]
    return text, words


def build_fixture_deck(budget_s: int) -> dict:
    slides = [{
        "title": "Attention",
        "bullets": ["every token looks at every other token", "this is the core mechanism"],
        # "attension" is a deliberate near-miss to exercise the phonetic-match path;
        # "quadratic" never appears in this clip and should NOT be falsely flagged.
        "terms": ["token", "attension", "quadratic"],
        "notes": "",
    }]
    return deck_mod.normalise_deck(slides, budget_s, source="generated", topic="self-attention")


async def main() -> None:
    print("=== 1. fixture transcript + deck ===")
    transcript_text, words = transcribe_words(FIXTURE_WAV)
    print("transcript:", transcript_text)
    print("words:", json.dumps(words, indent=2))
    deck = build_fixture_deck(budget_s=60)
    print("deck:", json.dumps(deck, indent=2))

    print("\n=== 2. metrics.compute ===")
    slide_events = [{"slide": 1, "at_s": 0.0}]
    metrics = metrics_mod.compute(words, FIXTURE_WAV, deck, slide_events, budget_s=60)
    print(json.dumps(metrics, indent=2))

    print("\n=== 3. judge.judge ===")
    judgment = await judge_mod.judge(deck, deck["slides"][0], transcript_text, words, metrics)
    print(json.dumps(judgment, indent=2))

    expected_top = {"scores", "summary", "improvements", "terms_to_drill"}
    assert set(judgment.keys()) == expected_top, f"unexpected top-level keys: {judgment.keys()}"
    expected_scores = {"delivery", "clarity", "structure", "slide_connection"}
    assert set(judgment["scores"].keys()) == expected_scores, f"unexpected score keys: {judgment['scores'].keys()}"
    expected_imp_keys = {"id", "quote", "span", "issue", "rubric_ref", "v2_text", "v3_markup", "alternative"}
    for imp in judgment["improvements"]:
        assert set(imp.keys()) == expected_imp_keys, f"unexpected improvement keys: {imp.keys()}"
        assert imp["quote"].lower() in transcript_text.lower(), f"quote not a real substring: {imp['quote']!r}"
    print(f"OK: {len(judgment['improvements'])} verified improvement(s), all quotes are real substrings.")

    print("\n=== 4. render.render_variants ===")
    # 4a. Integration proof: render the judge's actual first (or synthesized
    # fallback) improvement end-to-end.
    if judgment["improvements"]:
        improvement = judgment["improvements"][0]
    else:
        improvement = {
            "id": "imp_1",
            "quote": "Every token looks at every other token.",
            "span": {"start": 0.0, "end": 0.0},
            "issue": "the point lands flat with no pause for emphasis",
            "rubric_ref": "delivery.md#pause-taxonomy",
            "v2_text": "Every token looks at every other token in the sequence.",
            "v3_markup": "Every token looks at every other token in the sequence. <500>",
            "alternative": "Each token attends to every other token in the sequence.",
        }
        print("(no judge improvement survived verification on this short clip; using a synthetic fallback)")

    result = await render_mod.render_variants(improvement, OUT_DIR)
    for tag in ("v1", "v2", "v3"):
        r = result[tag]
        print(f"{tag}: path={r['path']} duration_s={r['duration_s']} gaps={r['gaps']}")
    for tag in ("v1", "v2", "v3"):
        assert result[tag]["duration_s"] > 0, f"{tag} clip has zero duration"
    print("OK: judge->render integration produced three real, non-empty clips.")

    # 4b. Deterministic contrast fixture, matching PLAN.md's own acceptance
    # methodology of controlled fixture sentences (not live LLM wording) for
    # the exact-millisecond claim: single clause with no internal punctuation
    # so V2 has no natural mid-sentence pause to confound the measurement,
    # and V3 adds one explicit end-of-sentence pause marker.
    contrast_improvement = {
        "id": "imp_contrast",
        "quote": "Every token can weigh all the others and decide what matters most.",
        "span": {"start": 0.0, "end": 0.0},
        "issue": "the point lands flat with no pause for emphasis",
        "rubric_ref": "delivery.md#pause-taxonomy",
        "v2_text": "Every token can weigh all the others and decide what matters most.",
        "v3_markup": "Every token can weigh all the others and decide what matters most. <800>",
        "alternative": "Each token weighs the others and picks out what matters most.",
    }
    contrast = await render_mod.render_variants(contrast_improvement, OUT_DIR)
    for tag in ("v2", "v3"):
        r = contrast[tag]
        print(f"contrast {tag}: path={r['path']} duration_s={r['duration_s']} gaps={r['gaps']}")

    v3_long_gaps = [g for g in contrast["v3"]["gaps"] if g["ms"] >= 400]
    v2_long_gaps = [g for g in contrast["v2"]["gaps"] if g["ms"] >= 400]
    assert v3_long_gaps, f"expected V3 to contain a measured gap >= 400ms, got {contrast['v3']['gaps']}"
    assert not v2_long_gaps, f"expected V2 to have no gap >= 400ms, got {contrast['v2']['gaps']}"
    print(f"OK: V3 has a >=400ms gap ({v3_long_gaps}) that V2 does not.")

    print("\n=== 5. deck.generate_deck ===")
    generated = await deck_mod.generate_deck("self-attention", "intermediate", 60)
    print(json.dumps(generated, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

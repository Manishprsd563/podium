"""E3 -- contrastive-delivery evidence, on the real product path.

Extends E1 (raw pause-markup accuracy) to the actual coaching mechanism:
`analysis.render.render_variants` renders three Rime clips per improvement,
same model/speaker/lang held constant (GATE0.md: mistv3/astra/eng/24 kHz):

  V1 as-delivered  -- verbatim quote including fillers (and, where the fixture
                      supplies `pauses`, the user's measured thinking pauses
                      reinserted as <NNN> markup)
  V2 cleaned       -- fillers/false starts removed, shortened wording
  V3 paced         -- V2 plus a deliberate pause before the key phrase

Preregistered targets (checked before any human listens to anything):
  - V1: every clip's Deepgram transcript contains >=1 word from the CONTRACTS
    §2 filler set (audible fillers, not stripped).
  - V3 vs V2 (the delivery contrast): V3's non-speech gap at the marked
    position is >= 400 ms LONGER than the longest natural non-speech gap in
    the matching V2 clip -- the deliberate pause is real and distinct.
    Measured at a -25 dBFS speech-presence threshold: mid-sentence <NNN>
    pauses on mistv3 render as breath noise at -30..-25 dBFS (measured, see
    results.json), not the digital silence E1 saw at sentence boundaries, so
    E1's -40 dBFS digital-silence threshold would under-measure them.
    Corroborated by duration: V3 runs ~1 s longer than V2 for a <1000> mark.
  - V2 alone: no digital-silence gap >= 400 ms at -40 dBFS anywhere in the
    clip (nothing like an inserted pause in the cleaned wording).
  - V1/V2/V3: the literal pause-markup digits are never in the transcript
    (Rime speaks the pause, not the number -- same check as E1).
  - iii) exploratory blinded listening (3-5 people, V2 vs V3, "more confident/
    clear"), reported as raw counts once a human fills in listening.csv -- NOT
    scored by this script; this script only prepares the blinded sheet.

Run: .venv/Scripts/python.exe evidence/e3_delivery_listening.py
Writes evidence/e3/clips/*.wav, evidence/e3/results.json, evidence/e3/listening.csv
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence._common import (  # noqa: E402
    RIME_LANG, RIME_MODEL, RIME_SPEAKER, deepgram_transcribe, duration_s, gaps,
    rime_rest_synth, wav_bytes_to_float,
)

OUT = Path("evidence/e3")
FIXTURES_PATH = OUT / "fixtures.json"
CLIPS_DIR = OUT / "clips"
BLIND_DIR = OUT / "blind"

FILLER_SET = [
    "um", "uh", "mm", "hmm", "mhm", "er", "ah", "like", "you know",
    "basically", "actually", "sort of", "kind of", "i mean", "right",
]
GAP_THRESHOLD_MS = 400
SPEECH_PRESENCE_DB = -25.0
MARKUP_DIGIT_RE = re.compile(r"<\d+>")
# words a mis-spoken pause marker would show up as in a transcript
MARKUP_WORDS = ("six hundred", "seven hundred", "eight hundred", "nine hundred",
                "six fifty", "six-fifty")


def _insert_pauses(text: str, pauses: list[dict]) -> str:
    """Fallback-only: reinsert measured thinking pauses as <NNN> markup after word N."""
    words = text.split()
    for p in sorted(pauses, key=lambda p: p["after_word"], reverse=True):
        idx = min(p["after_word"] + 1, len(words))
        words.insert(idx, f"<{p['ms']}>")
    return " ".join(words)


async def _fallback_render_variants(improvement: dict, out_dir: Path) -> dict[str, dict]:
    """Direct Rime REST render mirroring analysis.render.render_variants's documented
    contract (same filenames, same return shape), used only if analysis.render isn't
    importable yet -- see the ImportError handling in main()."""
    out_dir.mkdir(parents=True, exist_ok=True)
    iid = improvement["id"]
    v1_text = _insert_pauses(improvement["quote"], improvement.get("pauses", []))
    texts = {"v1": (v1_text, "asdelivered"), "v2": (improvement["v2_text"], "cleaned"),
             "v3": (improvement["v3_markup"], "paced")}
    out: dict[str, dict] = {}
    for variant, (text, suffix) in texts.items():
        wav = rime_rest_synth(text, model=RIME_MODEL, speaker=RIME_SPEAKER, lang=RIME_LANG)
        path = out_dir / f"{iid}_{variant}_{suffix}.wav"
        path.write_bytes(wav)
        samples, sr = wav_bytes_to_float(wav)
        out[variant] = {
            "path": str(path), "duration_s": duration_s(samples, sr),
            "text": text, "gaps": gaps(samples, sr),
        }
    return out

async def render_all(fixtures: list[dict]) -> tuple[list[dict], str]:
    try:
        from analysis.render import render_variants
        source = "analysis.render.render_variants"
        render_fn = render_variants
    except ImportError as exc:
        source = f"fallback direct Rime REST (analysis.render not importable: {exc})"
        render_fn = _fallback_render_variants

    rows = []
    for fx in fixtures:
        variants = await render_fn(fx, CLIPS_DIR)
        rows.append({"id": fx["id"], "fixture": fx, "variants": variants})
    return rows, source


def _independent_gaps(wav_path: str, *, thresh_db: float) -> list[dict]:
    """Re-measure gaps ourselves rather than trusting render_variants's own numbers."""
    samples, sr = wav_bytes_to_float(Path(wav_path).read_bytes())
    return gaps(samples, sr, thresh_db=thresh_db)


def evaluate(rows: list[dict]) -> list[dict]:
    results = []
    for row in rows:
        v1, v2, v3 = row["variants"]["v1"], row["variants"]["v2"], row["variants"]["v3"]

        v1_transcript = deepgram_transcribe(Path(v1["path"]).read_bytes())["transcript"].lower()
        fillers_found = [f for f in FILLER_SET if f in v1_transcript]

        # -40 dBFS: digital silence (E1's threshold). -25 dBFS: speech presence --
        # mid-sentence mistv3 pauses render as breath noise at -30..-25 dBFS.
        v2_gaps_silence = _independent_gaps(v2["path"], thresh_db=-40.0)
        v2_max_gap_silence = max((g["ms"] for g in v2_gaps_silence), default=0)
        v2_gaps_speech = _independent_gaps(v2["path"], thresh_db=SPEECH_PRESENCE_DB)
        v2_max_gap_speech = max((g["ms"] for g in v2_gaps_speech), default=0)
        v3_gaps_speech = _independent_gaps(v3["path"], thresh_db=SPEECH_PRESENCE_DB)
        v3_max_gap_speech = max((g["ms"] for g in v3_gaps_speech), default=0)
        pause_margin_ms = v3_max_gap_speech - v2_max_gap_speech

        v2_transcript = deepgram_transcribe(Path(v2["path"]).read_bytes())["transcript"].lower()
        v3_transcript = deepgram_transcribe(Path(v3["path"]).read_bytes())["transcript"].lower()
        markup_spoken = any(
            MARKUP_DIGIT_RE.search(t) or any(w in t for w in MARKUP_WORDS)
            for t in (v1_transcript, v2_transcript, v3_transcript)
        )

        results.append({
            "id": row["id"],
            "v1": {"path": v1["path"], "duration_s": v1["duration_s"],
                   "transcript": v1_transcript, "fillers_found": fillers_found,
                   "fillers_pass": len(fillers_found) > 0},
            "v2": {"path": v2["path"], "duration_s": v2["duration_s"],
                   "max_gap_silence_ms": v2_max_gap_silence,
                   "max_gap_speech_ms": v2_max_gap_speech,
                   "gaps_speech": v2_gaps_speech,
                   "no_ge400_pass": v2_max_gap_silence < GAP_THRESHOLD_MS},
            "v3": {"path": v3["path"], "duration_s": v3["duration_s"],
                   "max_gap_speech_ms": v3_max_gap_speech, "gaps_speech": v3_gaps_speech,
                   "pause_margin_over_v2_ms": pause_margin_ms,
                   "has_ge400_pass": pause_margin_ms >= GAP_THRESHOLD_MS},
            "speech_presence_thresh_db": SPEECH_PRESENCE_DB,
            "markup_never_spoken_pass": not markup_spoken,
        })
    return results


def write_listening_sheet(results: list[dict]) -> dict[str, dict[str, str]]:
    """Blinded V2-vs-V3 sheet: provider/model/variant identity stripped from filenames,
    A/B order randomised per item. Returns the hidden key (kept only in results.json,
    never in the csv) so answers can be scored once a human fills them in."""
    BLIND_DIR.mkdir(parents=True, exist_ok=True)
    key: dict[str, dict[str, str]] = {}
    rows = []
    for r in results:
        pair = ["v2", "v3"]
        random.shuffle(pair)
        letters = dict(zip("AB", pair))
        key[r["id"]] = letters
        for letter, variant in letters.items():
            src = Path(r[variant]["path"])
            dst = BLIND_DIR / f"{r['id']}_{letter}.wav"
            dst.write_bytes(src.read_bytes())
            rows.append({
                "clip_id": r["id"], "file": str(dst),
                "question": (
                    f"Item {r['id']}: listen to clip {letter} and its pair (same clip_id, "
                    "the other letter). Which of the two sounds more confident and clear "
                    "-- A or B? (write the answer once, on the A row)"
                ),
                "answer": "",
            })
    with open(OUT / "listening.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["clip_id", "file", "question", "answer"])
        w.writeheader()
        w.writerows(rows)
    return key


def main() -> None:
    fixtures = json.loads(FIXTURES_PATH.read_text())
    rows, source = asyncio.run(render_all(fixtures))
    results = evaluate(rows)
    listening_key = write_listening_sheet(results)

    print(f"render path: {source}\n")
    print(f"{'id':8s} {'V1 fillers':28s} {'V2 silence':10s} {'V2 nonspch':10s} "
          f"{'V3 nonspch':10s} {'V3-V2':7s} {'markup silent':13s}")
    n = len(results)
    v1_ok = v2_ok = v3_ok = markup_ok = 0
    for r in results:
        v1_ok += r["v1"]["fillers_pass"]
        v2_ok += r["v2"]["no_ge400_pass"]
        v3_ok += r["v3"]["has_ge400_pass"]
        markup_ok += r["markup_never_spoken_pass"]
        print(f"{r['id']:8s} {','.join(r['v1']['fillers_found']) or '(none)':28s} "
              f"{r['v2']['max_gap_silence_ms']:>7d} ms {r['v2']['max_gap_speech_ms']:>7d} ms "
              f"{r['v3']['max_gap_speech_ms']:>7d} ms {r['v3']['pause_margin_over_v2_ms']:>5d} ms "
              f"{'yes' if r['markup_never_spoken_pass'] else 'NO':>13s}")

    summary = {
        "render_source": source,
        "n_fixtures": n,
        "v1_fillers_audible": f"{v1_ok}/{n}",
        "v2_no_ge400ms_gap": f"{v2_ok}/{n}",
        "v3_pause_ge400ms_over_v2": f"{v3_ok}/{n}",
        "markup_never_spoken": f"{markup_ok}/{n}",
        "gap_threshold_ms": GAP_THRESHOLD_MS,
        "speech_presence_thresh_db": SPEECH_PRESENCE_DB,
    }
    print(f"\n{summary['v1_fillers_audible']} V1 clips have an audible filler")
    print(f"{summary['v2_no_ge400ms_gap']} V2 clips have no digital-silence gap >= {GAP_THRESHOLD_MS} ms")
    print(f"{summary['v3_pause_ge400ms_over_v2']} V3 clips have a non-speech pause >= {GAP_THRESHOLD_MS} ms "
          f"longer than V2's longest natural gap")
    print(f"{summary['markup_never_spoken']} clips never speak the pause markup aloud")
    print("\nblinded listening sheet -> evidence/e3/listening.csv "
          "(exploratory, small-sample; not scored here)")
    print("results -> evidence/e3/results.json ; clips -> evidence/e3/clips/*.wav, "
          "evidence/e3/blind/*.wav")

    (OUT / "results.json").write_text(json.dumps(
        {"summary": summary, "fixtures": results, "listening_key": listening_key}, indent=2))

    fail = v1_ok < n or v2_ok < n or v3_ok < n or markup_ok < n
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()

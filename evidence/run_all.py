"""One command that runs the whole evidence suite: E1, E2 Parts A+B (evidence/e2_duplex.py),
E2 Part C (evidence/e2_live_room.py, run only if that file is present), and E3.

Prints a pass/fail line per preregistered claim with the measured number next to
its target, and exits non-zero if any MEASURED target was missed. A claim that
could not be measured is printed as NOT EXERCISED with the reason (an unmeasured
target is reported honestly, not silently scored as a pass) and does not flip
the exit code -- the pass/fail table itself is the judge-facing artifact.

Run: .venv/Scripts/python.exe evidence/run_all.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

E1_SCRIPT = ROOT / "evidence" / "e1_pause_control.py"
E1_RESULTS = ROOT / "evidence" / "e1" / "results.json"
E2_SCRIPT = ROOT / "evidence" / "e2_duplex.py"
E2_LIVE_ROOM_SCRIPT = ROOT / "evidence" / "e2_live_room.py"
E2_RESULTS = ROOT / "evidence" / "e2" / "results.json"
E3_SCRIPT = ROOT / "evidence" / "e3_delivery_listening.py"
E3_RESULTS = ROOT / "evidence" / "e3" / "results.json"

SHIPPED_MODEL = "mistv3"


def _run(name: str, script: Path) -> bool:
    print(f"\n{'=' * 72}\n{name}: {script.name}\n{'=' * 72}")
    proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT))
    ok = proc.returncode == 0
    print(f"-> {script.name} exit code {proc.returncode}")
    return ok


def e1_claims() -> list[dict]:
    rows = json.loads(E1_RESULTS.read_text())
    shipped = [r for r in rows if r["model"] == SHIPPED_MODEL]
    within = sum(1 for r in shipped if r["within_tolerance"])
    spoken = any(r["markup_spoken"] for r in rows)
    errors = [r["error_ms"] for r in shipped]
    return [{
        "claim": f"E1 pause markup: {SHIPPED_MODEL} honours <NNN> markup within tolerance and never speaks it",
        "target": f"{len(shipped)}/{len(shipped)} fixtures within +/-450 ms of requested, markup never spoken",
        "measured": f"{within}/{len(shipped)} within tolerance (error {errors} ms), "
                    f"markup spoken on any clip: {spoken}",
        "pass": within == len(shipped) and not spoken,
    }]


def e2_claims() -> list[dict]:
    data = json.loads(E2_RESULTS.read_text())
    claims: list[dict] = []
    pa = data["part_a"]
    base, pm = pa["baseline_default_vad"], pa["presentation_mode"]
    claims.append({
        "claim": "E2.A baseline: default VAD endpointing ends the presenter's turn mid-pause",
        "target": "informational (the reason presentation mode exists)",
        "measured": f"{base['premature_ends']}/{base['of']} fixtures ended prematurely",
        "pass": True,
    })
    if pm["exercised"]:
        claims.append({
            "claim": "E2.A shipped presentation-mode policy never ends the turn during 2-5 s thinking pauses",
            "target": "0/6 premature turn ends",
            "measured": f"{pm['premature_ends']}/{pm['of']} premature turn ends",
            "pass": pm["premature_ends"] == 0,
        })
    else:
        claims.append({
            "claim": "E2.A shipped presentation-mode policy never ends the turn during 2-5 s thinking pauses",
            "target": "0/6 premature turn ends",
            "measured": f"NOT EXERCISED -- {pm['reason']}",
            "pass": None,
        })
    pb = data["part_b"]
    if pb["exercised"]:
        sf = pb["stale_fencing"]
        claims.append({
            "claim": "E2.B re-recording during a pending judgment never speaks the stale judgment",
            "target": "0 stale judgments spoken",
            "measured": f"{1 if sf['stale_judgment_spoken'] else 0} spoken "
                        f"(revision {sf['revision_before_rerecord']} -> {sf['revision_after_rerecord']}; "
                        f"mechanism: {sf['mechanism']})",
            "pass": not sf["stale_judgment_spoken"],
        })
        claims.append({
            "claim": "E2.B feedback items marked heard only after full playout (no duplicates)",
            "target": "0 duplicate feedback_heard items",
            "measured": f"{pb['duplicate_feedback_heard']} duplicates",
            "pass": pb["duplicate_feedback_heard"] == 0,
        })
        claims.append({
            "claim": "E2.B interrupting feedback stops audible output with P95 <= 300 ms",
            "target": "P95 <= 300 ms",
            "measured": f"NOT EXERCISED -- {pb['stop_latency_caveat']}",
            "pass": None,
        })
    else:
        claims.append({
            "claim": "E2.B interruption + revision fencing",
            "target": "0 stale spoken, 0 duplicate heard, P95 stop latency <= 300 ms",
            "measured": f"NOT EXERCISED -- {pb['reason']}",
            "pass": None,
        })
    pc = data.get("part_c")
    if pc is None:
        claims.append({
            "claim": "E2.C interrupting feedback stops audible output with P95 <= 300 ms (real LiveKit room)",
            "target": "P95 <= 300 ms",
            "measured": "NOT EXERCISED -- evidence/e2_live_room.py has not been run yet "
                        "(no 'part_c' key in evidence/e2/results.json); "
                        "run .venv/Scripts/python.exe evidence/e2_live_room.py against a live LiveKit room",
            "pass": None,
        })
    elif not pc.get("exercised", True) and "stop_latency" not in pc \
            and "baseline_default_interruption" not in pc and "tuned_interruption" not in pc:
        claims.append({
            "claim": "E2.C interrupting feedback stops audible output with P95 <= 300 ms (real LiveKit room)",
            "target": "P95 <= 300 ms",
            "measured": f"NOT EXERCISED -- {pc.get('reason', 'no reason recorded')}",
            "pass": None,
        })
    else:
        # New shape: two labelled variants (part_c.baseline_default_interruption /
        # part_c.tuned_interruption), each holding its own stop_latency/state_consistency/
        # rerecord_fence. Older shape: those three blocks live flat on part_c itself --
        # treated as a single unlabelled variant for backward compatibility.
        variants = {k: v for k, v in pc.items()
                    if isinstance(v, dict) and "stop_latency" in v}
        if not variants:
            variants = {"": pc}
        for label, variant in variants.items():
            suffix = f" [{label}]" if label else ""
            if not variant.get("exercised", True):
                claims.append({
                    "claim": f"E2.C interrupting feedback stops audible output with P95 <= 300 ms "
                              f"(real LiveKit room){suffix}",
                    "target": "P95 <= 300 ms",
                    "measured": f"NOT EXERCISED -- {variant.get('reason', 'no reason recorded')}",
                    "pass": None,
                })
                continue
            sl = variant.get("stop_latency", {})
            p95, target = sl.get("p95_ms"), sl.get("target_ms", 300)
            n_ok, n_attempted = sl.get("n_trials"), sl.get("n_attempted")
            caveat = "; ".join(variant.get("not_measured", []))
            claims.append({
                "claim": f"E2.C interrupting feedback stops audible output with P95 <= 300 ms "
                          f"(real LiveKit room){suffix}",
                "target": f"P95 <= {target} ms (>=10 measurable trials)",
                "measured": f"P50 {sl.get('p50_ms')} ms, P95 {p95} ms, max {sl.get('max_ms')} ms, "
                            f"from {n_ok}/{n_attempted} attempted trials measurable"
                            + (f" (caveat: {caveat})" if caveat else ""),
                "pass": bool(sl.get("target_met")) if p95 is not None else None,
            })
            sc = variant.get("state_consistency", {})
            claims.append({
                "claim": f"E2.C interrupted feedback never ends up marked heard, no duplicate heard, "
                          f"no stale speech after interrupt (real LiveKit room){suffix}",
                "target": "0 / 0 / 0",
                "measured": f"{sc.get('interrupted_items_marked_heard', 0)} / {sc.get('duplicate_heard_items', 0)} / "
                            f"{sc.get('stale_speech_after_interrupt_count', 0)}",
                "pass": (sc.get("interrupted_items_marked_heard", 0) == 0
                         and sc.get("duplicate_heard_items", 0) == 0
                         and sc.get("stale_speech_after_interrupt_count", 0) == 0),
            })
            rf = variant.get("rerecord_fence", {})
            claims.append({
                "claim": f"E2.C re-recording during a pending judgment never speaks the stale judgment "
                          f"(real LiveKit room){suffix}",
                "target": "0 stale judgments spoken",
                "measured": f"{1 if rf.get('stale_judgment_spoken') else 0} spoken (mechanism: {rf.get('mechanism')})",
                "pass": not rf.get("stale_judgment_spoken", True),
            })
    return claims


def e3_claims() -> list[dict]:
    data = json.loads(E3_RESULTS.read_text())
    s = data["summary"]
    n = s["n_fixtures"]
    def frac(key: str) -> tuple[int, int]:
        got, _, total = s[key].partition("/")
        return int(got), int(total)
    v1, _ = frac("v1_fillers_audible")
    v2, _ = frac("v2_no_ge400ms_gap")
    v3, _ = frac("v3_pause_ge400ms_over_v2")
    silent, _ = frac("markup_never_spoken")
    return [
        {"claim": f"E3 V1 keeps the fillers audible (via Deepgram, CONTRACTS §2 filler set)",
         "target": f"{n}/{n} clips", "measured": s["v1_fillers_audible"], "pass": v1 == n},
        {"claim": f"E3 V2 has no silence gap >= {s['gap_threshold_ms']} ms at the marked position",
         "target": f"{n}/{n} clips", "measured": s["v2_no_ge400ms_gap"], "pass": v2 == n},
        {"claim": f"E3 V3 has a real >= {s['gap_threshold_ms']} ms pause at the marked position",
         "target": f"{n}/{n} clips", "measured": s["v3_pause_ge400ms_over_v2"], "pass": v3 == n},
        {"claim": "E3 pause markup is never spoken aloud in any variant",
         "target": f"{n}/{n} clips", "measured": s["markup_never_spoken"], "pass": silent == n},
        {"claim": "E3 blinded listening: V2 vs V3, which sounds more confident/clear",
         "target": "exploratory, small sample (3-5 people), reported as counts",
         "measured": "PENDING HUMAN -- fill evidence/e3/listening.csv (blinded A/B per item; "
                     "key in evidence/e3/results.json 'listening_key')",
         "pass": None},
    ]


def main() -> None:
    script_results = {
        "E1": _run("E1 pause control", E1_SCRIPT),
        "E2 duplex": _run("E2 full duplex", E2_SCRIPT),
    }
    if E2_LIVE_ROOM_SCRIPT.exists():
        script_results["E2 live room"] = _run("E2 live room (part C)", E2_LIVE_ROOM_SCRIPT)
    else:
        print(f"\n{'=' * 72}\nE2 live room (part C): evidence/e2_live_room.py not present yet -- skipping\n{'=' * 72}")
    script_results["E3 delivery listening"] = _run("E3 delivery listening", E3_SCRIPT)

    claims = e1_claims() + e2_claims() + e3_claims()

    print(f"\n{'=' * 72}\nCLAIM TABLE\n{'=' * 72}")
    missed = 0
    for c in claims:
        if c["pass"] is True:
            verdict = "PASS"
        elif c["pass"] is False:
            verdict = "FAIL"
        else:
            verdict = "NOT EXERCISED"
        print(f"[{verdict:^13s}] {c['claim']}\n{'':15s} target:   {c['target']}\n"
              f"{'':15s} measured: {c['measured']}\n")
        if c["pass"] is False:
            missed += 1

    print("script exit codes:", {k: ("ok" if v else "FAILED") for k, v in script_results.items()})
    print("-> evidence/e1/results.json, evidence/e2/results.json (parts a/b/c), "
          "evidence/e3/results.json, evidence/e3/listening.csv")
    if missed:
        print(f"\n{missed} measured preregistered target(s) missed -> exiting non-zero")
    else:
        print("\nno measured preregistered target missed")
    sys.exit(1 if (missed or not all(script_results.values())) else 0)


if __name__ == "__main__":
    main()

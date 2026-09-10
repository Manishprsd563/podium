"""Assemble videos/podium-pitch/index.html from the twelve scene sub-compositions.

- Scene order and transition type are fixed in SCENES below; scene durations are read from each
  scene file's root `data-duration`, so retiming a scene never requires touching the root.
- Consecutive scenes overlap by the transition length; the root GSAP timeline animates the host
  slots (opacity / scale / clip-path) plus a teal accent wipe block and a flash overlay.
- The music bed (public/music/bed_film.wav: bed.wav time-stretched and EQ'd offline with ffmpeg)
  is placed once at root level with a volume automation lane that ducks under every VO clip.
  Speech windows are computed from the scenes' own `<audio src="public/vo/…">` placements and the
  VO manifest, so the duck always follows the actual voice placement.

Run from the project directory or repo root:  python tools/assemble.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
COMP = PROJECT / "compositions"
MANIFEST = json.loads((PROJECT / "public" / "vo" / "manifest.json").read_text(encoding="utf-8"))

# (scene id, transition INTO the next scene)
SCENES = [
    ("s01-cold-open", "zoom"),
    ("s02-meet-maya", "slice"),
    ("s03-deck", "zoom"),
    ("s04-session-one", "wipe"),
    ("s05-measured", "zoom"),
    ("s06-contrast", "wipe"),
    ("s07-inside-the-call", "slice"),
    ("s08-drill-failure", "zoom"),
    ("s09-ten-skills", "slice"),
    ("s10-delta", "wipe"),
    ("s11-provider", "flash"),
    ("s12-close", None),
]
OVERLAP = {"zoom": 0.5, "slice": 0.45, "wipe": 0.4, "flash": 0.25}

DUCK_DB = -9.0        # bed level under speech
BED_DB = 0.0          # bed level in the clear (bed.wav is already -24 LUFS)
ATTACK = 0.18
RELEASE = 0.6
MERGE_GAP = 0.6       # speech windows closer than this stay ducked

DUR_RE = re.compile(r'data-composition-id="(?P<id>[^"]+)"[^>]*?data-duration="(?P<dur>[0-9.]+)"', re.S)
VO_RE = re.compile(r'<audio\b(?P<attrs>[^>]*?src="public/vo/(?P<vo>[a-z]\d{2})\.wav"[^>]*)>', re.S)
START_RE = re.compile(r'data-start="(?P<t>[0-9.]+)"')


def db(v: float) -> float:
    return 10 ** (v / 20)


def scene_info(sid: str) -> tuple[float, list[tuple[float, float]]]:
    html = (COMP / f"{sid}.html").read_text(encoding="utf-8")
    m = DUR_RE.search(html)
    if not m or m.group("id") != sid:
        raise SystemExit(f"{sid}: root data-composition-id/data-duration not found or mismatched")
    dur = float(m.group("dur"))
    speech: list[tuple[float, float]] = []
    for a in VO_RE.finditer(html):
        vo = a.group("vo")
        s = START_RE.search(a.group("attrs"))
        if not s:
            raise SystemExit(f"{sid}: VO {vo} has no data-start")
        t0 = float(s.group("t"))
        speech.append((t0, t0 + MANIFEST[vo]["seconds"]))
    return dur, speech


def merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(windows):
        if out and a - out[-1][1] < MERGE_GAP:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def duck_lane(windows: list[tuple[float, float]], total: float) -> list[dict]:
    pts: list[dict] = [{"t": 0, "v": 0}, {"t": 1.2, "v": db(BED_DB)}]
    hi, lo = db(BED_DB), db(DUCK_DB)
    for a, b in windows:
        pts.append({"t": round(max(1.2, a - ATTACK), 3), "v": hi})
        pts.append({"t": round(a, 3), "v": lo})
        pts.append({"t": round(b + 0.1, 3), "v": lo})
        pts.append({"t": round(b + 0.1 + RELEASE, 3), "v": hi, "curve": 0.3})
    pts.append({"t": round(total - 2.5, 3), "v": hi})
    pts.append({"t": round(total, 3), "v": 0})
    # keep monotonic time
    cleaned = []
    for p in pts:
        if cleaned and p["t"] <= cleaned[-1]["t"]:
            p["t"] = cleaned[-1]["t"] + 0.01
        cleaned.append(p)
    assert len(cleaned) <= 512, len(cleaned)
    return cleaned


def main() -> None:
    starts: list[float] = []
    durs: list[float] = []
    speech_all: list[tuple[float, float]] = []
    t = 0.0
    for sid, trans in SCENES:
        dur, speech = scene_info(sid)
        starts.append(round(t, 3))
        durs.append(dur)
        speech_all += [(t + a, t + b) for a, b in speech]
        t += dur - (OVERLAP[trans] if trans else 0.0)
    total = round(starts[-1] + durs[-1], 3)
    windows = merge(speech_all)

    hosts = []
    for (sid, _), st, du in zip(SCENES, starts, durs):
        hosts.append(
            f'      <div id="el-{sid[:3]}" class="clip host" data-composition-id="{sid}"\n'
            f'           data-composition-src="compositions/{sid}.html" data-start="{st}" data-duration="{du}"\n'
            f'           data-track-index="1" data-width="1920" data-height="1080"></div>'
        )

    boundaries = []
    for i, (sid, trans) in enumerate(SCENES[:-1]):
        nxt = SCENES[i + 1][0]
        boundaries.append({"kind": trans, "out": f"#el-{sid[:3]}", "in": f"#el-{nxt[:3]}", "t": starts[i + 1], "d": OVERLAP[trans]})

    # No runtime data-fx-chain anywhere: HyperFrames' offline fx renderer attenuates
    # low-level passages by 10-30 dB (measured on m01: quiet syllables dropped ~25 dB
    # with only a highpass + peaking node), which reads as chopped speech. The bed's
    # room EQ + time-stretch are baked into public/music/bed_film.wav with ffmpeg
    # instead; voices stay untouched.
    automation = {"version": 1, "lanes": [{"target": "volume", "points": duck_lane(windows, total)}]}

    def attr(obj: dict) -> str:
        return json.dumps(obj, separators=(",", ":")).replace("&", "&amp;").replace('"', "&quot;")

    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="ui/vendor/gsap.min.js"></script>
    <script src="ui/vendor/three.min.js"></script>
    <script src="ui/podium-orb.js"></script>
    <link rel="stylesheet" href="ui/tokens.css" />
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ width: 1920px; height: 1080px; overflow: hidden; background: var(--bg, #0b0d12); }}
      body {{ font-family: var(--font, system-ui, sans-serif); }}
      #root {{ position: relative; width: 1920px; height: 1080px; overflow: hidden; background: var(--bg, #0b0d12); }}
      .host {{ position: absolute; inset: 0; transform-origin: 50% 50%; will-change: transform, opacity, clip-path; }}
      #wipe-block {{ position: absolute; top: 0; left: -1920px; width: 1920px; height: 1080px; background: var(--bg, #0b0d12); z-index: 50; }}
      #wipe-line {{ position: absolute; top: 0; right: -2px; width: 4px; height: 1080px; background: var(--teal, #4fd1c5); opacity: 0; box-shadow: 0 0 24px var(--teal, #4fd1c5); }}
      #flash {{ position: absolute; inset: 0; background: var(--ink, #fff); opacity: 0; z-index: 60; }}
      #grain {{ position: absolute; inset: 0; z-index: 70; pointer-events: none; opacity: 0.05; mix-blend-mode: overlay;
        background-image: radial-gradient(rgba(255,255,255,0.35) 0.6px, transparent 0.7px); background-size: 3px 3px; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="root" data-width="1920" data-height="1080" data-start="0" data-duration="{total}">
{chr(10).join(hosts)}


      <div id="wipe-block" data-track-index="49"><div id="wipe-line"></div></div>
      <div id="flash" data-track-index="51"></div>
      <div id="grain" data-track-index="52"></div>

      <hf-audio-group id="voice" data-label="Voices" data-volume="1"></hf-audio-group>
      <hf-audio-group id="sfx" data-label="SFX" data-volume="0.9"></hf-audio-group>

      <audio id="bed" src="public/music/bed_film.wav" data-start="0" data-duration="{total}" data-track-index="10"
        data-audio-group="music" data-volume="1"
        data-automation="{attr(automation)}"></audio>
    </div>

    <script>
      window.__timelines = window.__timelines || {{}};
      var tl = gsap.timeline({{ paused: true }});
      var B = {json.dumps(boundaries)};

      B.forEach(function (b) {{
        var t = b.t, d = b.d;
        if (b.kind === "zoom") {{
          tl.fromTo(b.out, {{ opacity: 1, scale: 1 }}, {{ opacity: 0, scale: 1.06, duration: d, ease: "power2.in" }}, t);
          tl.fromTo(b.in, {{ opacity: 0, scale: 0.965 }}, {{ opacity: 1, scale: 1, duration: d, ease: "power2.out" }}, t);
        }} else if (b.kind === "slice") {{
          tl.set(b.in, {{ opacity: 1, clipPath: "inset(0 0 0 100%)" }}, t - 0.001);
          tl.to(b.in, {{ clipPath: "inset(0 0 0 0%)", duration: d, ease: "power3.inOut" }}, t);
          tl.to(b.out, {{ x: -140, opacity: 0.6, duration: d, ease: "power3.inOut" }}, t);
          tl.set(b.out, {{ opacity: 0 }}, t + d);
        }} else if (b.kind === "wipe") {{
          tl.set("#wipe-block", {{ x: 0 }}, t - 0.001);
          tl.set("#wipe-line", {{ opacity: 0 }}, t - 0.001);
          tl.to("#wipe-block", {{ x: 1920, duration: d / 2, ease: "power3.inOut" }}, t);
          tl.to("#wipe-line", {{ opacity: 1, duration: 0.08, ease: "power2.out" }}, t);
          tl.set(b.out, {{ opacity: 0 }}, t + d / 2);
          tl.set(b.in, {{ opacity: 1 }}, t + d / 2);
          tl.to("#wipe-line", {{ opacity: 0, duration: 0.08, ease: "power1.in" }}, t + d / 2);
          tl.to("#wipe-block", {{ x: 3840, duration: d / 2, ease: "power3.inOut" }}, t + d / 2);
        }} else if (b.kind === "flash") {{
          tl.fromTo("#flash", {{ opacity: 0 }}, {{ opacity: 0.85, duration: d * 0.35, ease: "power2.in" }}, t);
          tl.set(b.out, {{ opacity: 0 }}, t + d * 0.35);
          tl.set(b.in, {{ opacity: 1 }}, t + d * 0.35);
          tl.to("#flash", {{ opacity: 0, duration: d * 0.65, ease: "power2.out" }}, t + d * 0.35);
        }}
      }});

      window.__timelines["root"] = tl;
    </script>
  </body>
</html>
"""
    (PROJECT / "index.html").write_text(html, encoding="utf-8")
    print(f"total {total}s; scenes:")
    for (sid, trans), st, du in zip(SCENES, starts, durs):
        print(f"  {sid:<22} start {st:8.3f}  dur {du:6.2f}  -> {trans}")
    print(f"speech windows: {len(windows)}; duck points: {len(automation['lanes'][0]['points'])}")


if __name__ == "__main__":
    main()

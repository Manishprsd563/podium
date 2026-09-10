"""Emit docs/img/architecture.svg: the module-wiring graph from docs/README.md.

Same nodes and edges as the mermaid block in docs/README.md ("The same graph
as exact module wiring"), drawn in the podium-header.svg theme so the README
image carries the full detail instead of a simplified sketch.

    python scripts/architecture_diagram.py
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img" / "architecture.svg"

W, H = 1680, 960

LAVENDER = "#c4b5fd"  # Podium's own code (browser, orchestrator)
MINT = "#7dd3c0"  # LiveKit Cloud, LiveKit Inference, Deepgram
CORAL = "#f5a997"  # Rime
GOLD = "#f3d17c"  # analysis/*, pure Python
INK = "#f5f5f7"
BG = "#000000"
PANEL = "#0b0b0d"  # opaque fills that sit on the background (pills, cylinder cap)
SANS = "'Poppins','Century Gothic','Futura',Avenir,'Segoe UI',ui-sans-serif,system-ui,sans-serif"
UI = "'Segoe UI',ui-sans-serif,system-ui,sans-serif"
MONO = "'Cascadia Code','JetBrains Mono',Consolas,ui-monospace,monospace"

# 24x24 line icons, drawn with the node colour.
ICONS = {
    "window": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/><path d="M6 6.5h.01M9 6.5h.01"/>',
    "layers": '<path d="M12 3 21 8l-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 16 9 5 9-5"/>',
    "orb": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>',
    "bars": '<path d="M4 20V13M10 20V6M16 20V10M22 20H2"/>',
    "graph": '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="m10.8 7.2-4.6 9.6M13.2 7.2l4.6 9.6M7.5 19h9"/>',
    "wave": '<path d="M3 12h2M7 8v8M11 4v16M15 7v10M19 10v4M21 12h.5"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "speaker": '<path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16 9.5a3.5 3.5 0 0 1 0 5"/><path d="M18.5 7a7 7 0 0 1 0 10"/>',
    "speaker-clock": '<path d="M3 9v6h4l4 3.5v-13L7 9H3z"/><circle cx="17" cy="15" r="4.5"/><path d="M17 12.5V15l1.8 1.2"/>',
    "gauge": '<path d="M4 17a8 8 0 1 1 16 0"/><path d="m12 17 4-6"/><circle cx="12" cy="17" r="1.5"/>',
    "scale": '<path d="M12 3v18M6 21h12M4 7h16"/><path d="M7 7l-3 7a3 3 0 0 0 6 0L7 7zM17 7l-3 7a3 3 0 0 0 6 0l-3-7z"/>',
    "film": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 5v14M16 5v14M3 10h5M3 14h5M16 10h5M16 14h5"/>',
    "doc-speaker": '<path d="M13 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9l-6-6z"/><path d="M13 3v6h6"/><path d="M8 13v3h2l2.5 2v-7L10 13H8z"/><path d="M15 14a2 2 0 0 1 0 3"/>',
}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rgba(hex_color: str, a: float) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{a})"


class Svg:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.headers: list[str] = []  # group titles, emitted last so edges pass behind them

    def add(self, s: str) -> None:
        self.parts.append(s)

    # ---- containers ---------------------------------------------------------------
    def group(self, x: int, y: int, w: int, h: int, color: str, title: str, sub: str | None = None) -> None:
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{color}" fill-opacity="0.035" '
            f'stroke="{color}" stroke-opacity="0.45" stroke-width="1.2" stroke-dasharray="6 5"/>'
        )
        halo = f'paint-order="stroke" stroke="{BG}" stroke-width="7" stroke-linejoin="round"'
        self.headers.append(
            f'<text x="{x + 20}" y="{y + 28}" font-family="{UI}" font-size="12" font-weight="600" '
            f'letter-spacing="3.5" fill="{color}" {halo}>{esc(title)}</text>'
        )
        if sub:
            self.headers.append(
                f'<text x="{x + 20}" y="{y + 46}" font-family="{MONO}" font-size="11.5" '
                f'fill="{INK}" fill-opacity="0.6" {halo}>{esc(sub)}</text>'
            )

    def flush_headers(self) -> None:
        self.parts.extend(self.headers)
        self.headers.clear()

    # ---- nodes --------------------------------------------------------------------
    def node(self, x: int, y: int, w: int, h: int, color: str, icon: str, title: str, lines: list[str], mono_title: bool = True) -> None:
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="11" fill="{color}" fill-opacity="0.09" '
            f'stroke="{color}" stroke-width="1.5" filter="url(#node-shadow)"/>'
        )
        self.add(f'<rect x="{x}" y="{y + 12}" width="3" height="{h - 24}" rx="1.5" fill="{color}"/>')
        n = 1 + len(lines)
        line_h = 17
        top = y + h / 2 - (14 + (n - 1) * line_h) / 2 + 12
        ix, iy = x + 18, y + h / 2 - 14
        self.add(f'<g transform="translate({ix} {iy}) scale(1.17)" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICONS[icon]}</g>')
        tx = x + 60
        fam = MONO if mono_title else UI
        self.add(f'<text x="{tx}" y="{top:.1f}" font-family="{fam}" font-size="13.5" font-weight="600" fill="{INK}">{esc(title)}</text>')
        for i, ln in enumerate(lines, 1):
            self.add(f'<text x="{tx}" y="{top + i * line_h:.1f}" font-family="{UI}" font-size="12" fill="{INK}" fill-opacity="0.72">{esc(ln)}</text>')

    def hub(self, x: int, y: int, w: int, h: int, color: str, title: str, lines: list[str]) -> None:
        """Large centred node for the orchestrator."""
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{color}" fill-opacity="0.11" '
            f'stroke="{color}" stroke-width="2" filter="url(#node-shadow)"/>'
        )
        cx = x + w / 2
        self.add(f'<circle cx="{cx}" cy="{y + 62}" r="30" fill="{color}" fill-opacity="0.12" stroke="{color}" stroke-opacity="0.5"/>')
        self.add(f'<g transform="translate({cx - 18} {y + 44}) scale(1.5)" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{ICONS["graph"]}</g>')
        self.add(f'<text x="{cx}" y="{y + 126}" text-anchor="middle" font-family="{SANS}" font-size="17" font-weight="700" fill="{INK}">{esc(title)}</text>')
        for i, ln in enumerate(lines):
            self.add(f'<text x="{cx}" y="{y + 150 + i * 18}" text-anchor="middle" font-family="{UI}" font-size="12.5" fill="{INK}" fill-opacity="0.75">{esc(ln)}</text>')

    def cylinder(self, cx: int, top: int, w: int, h: int, color: str, title: str, sub: str) -> None:
        ry = 12
        x = cx - w / 2
        self.add(
            f'<path d="M{x} {top + ry} v{h - 2 * ry} a{w / 2} {ry} 0 0 0 {w} 0 v-{h - 2 * ry}" '
            f'fill="{color}" fill-opacity="0.09" stroke="{color}" stroke-width="1.5" filter="url(#node-shadow)"/>'
        )
        self.add(f'<ellipse cx="{cx}" cy="{top + ry}" rx="{w / 2}" ry="{ry}" fill="{PANEL}" stroke="{color}" stroke-width="1.5"/>')
        self.add(f'<text x="{cx}" y="{top + h / 2 + 4}" text-anchor="middle" font-family="{SANS}" font-size="16" font-weight="700" fill="{INK}">{esc(title)}</text>')
        self.add(f'<text x="{cx}" y="{top + h / 2 + 22}" text-anchor="middle" font-family="{UI}" font-size="11.5" fill="{INK}" fill-opacity="0.65">{esc(sub)}</text>')

    # ---- edges --------------------------------------------------------------------
    def edge(self, pts: list[tuple[float, float]], color: str, *, both: bool = False, dashed: bool = False, label: str | None = None, at: tuple[float, float] | None = None) -> None:
        d = "M" + " L".join(f"{x} {y}" for x, y in pts)
        m = f'marker-end="url(#arrow-{color[1:]})"'
        if both:
            m += f' marker-start="url(#arrow-{color[1:]}-rev)"'
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" stroke-opacity="0.9"{dash} {m}/>')
        if label:
            self.pill(*(at or ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2)), label, color)

    def pill(self, lx: float, ly: float, label: str, color: str) -> None:
        pw = len(label) * 7 + 20
        self.add(
            f'<rect x="{lx - pw / 2:.1f}" y="{ly - 9}" width="{pw:.1f}" height="18" rx="9" fill="{PANEL}" '
            f'stroke="{color}" stroke-opacity="0.55" stroke-width="1"/>'
        )
        self.add(
            f'<text x="{lx}" y="{ly + 4}" text-anchor="middle" font-family="{MONO}" font-size="11" '
            f'fill="{color}">{esc(label)}</text>'
        )


def build() -> str:
    s = Svg()

    # ---- defs + background (same recipe as podium-header.svg) --------------------------
    markers = []
    for c in (LAVENDER, MINT, CORAL, GOLD, INK):
        k = c[1:]
        markers.append(
            f'<marker id="arrow-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
            f'<path d="M0 0.5 L10 5 L0 9.5 z" fill="{c}"/></marker>'
            f'<marker id="arrow-{k}-rev" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="8" markerHeight="8" orient="auto">'
            f'<path d="M10 0.5 L0 5 L10 9.5 z" fill="{c}"/></marker>'
        )
    s.add(
        "<defs>"
        f'<radialGradient id="glow-lavender" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="{LAVENDER}" stop-opacity="0.09"/><stop offset="100%" stop-color="{LAVENDER}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="glow-mint" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="{MINT}" stop-opacity="0.07"/><stop offset="100%" stop-color="{MINT}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="glow-coral" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="{CORAL}" stop-opacity="0.06"/><stop offset="100%" stop-color="{CORAL}" stop-opacity="0"/></radialGradient>'
        '<filter id="soft-blur" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="34"/></filter>'
        '<filter id="node-shadow" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#000" flood-opacity="0.6"/></filter>'
        + "".join(markers)
        + "</defs>"
    )
    s.add(f'<rect width="{W}" height="{H}" fill="{BG}"/>')
    s.add(
        '<g filter="url(#soft-blur)">'
        '<ellipse cx="260" cy="440" rx="320" ry="300" fill="url(#glow-lavender)"/>'
        '<ellipse cx="900" cy="330" rx="420" ry="260" fill="url(#glow-mint)"/>'
        '<ellipse cx="1420" cy="720" rx="360" ry="220" fill="url(#glow-coral)"/>'
        "</g>"
    )
    # ---- groups ----------------------------------------------------------------------
    s.group(40, 120, 280, 660, LAVENDER, "BROWSER", "web/  ·  no build step")
    s.group(390, 255, 210, 220, MINT, "LIVEKIT CLOUD", "India South")
    s.group(620, 100, 640, 560, INK, "AGENT PROCESS", "agent/session_agent.py")
    s.group(730, 690, 660, 170, GOLD, "ANALYSIS")

    # ---- nodes -----------------------------------------------------------------------
    # Browser
    s.node(65, 185, 230, 84, LAVENDER, "window", "index.html", ["import map, statusbar, slot"])
    s.node(65, 325, 230, 96, LAVENDER, "layers", "app.js", ["state + view router,", "LiveKit client"])
    s.node(65, 495, 190, 84, LAVENDER, "orb", "orb.js", ["Three.js orb"])
    s.node(65, 655, 230, 96, LAVENDER, "bars", "dashboard.js", ["scores / improvement /", "drill / wrap"])
    # LiveKit Cloud
    s.cylinder(495, 320, 120, 100, MINT, "Room", "WebRTC SFU")
    # Agent process
    s.hub(650, 200, 220, 230, LAVENDER, "PodiumOrchestrator", ["phases, turn policy,", "revision fence"])
    s.node(635, 470, 215, 90, CORAL, "speaker", "Rime mistv3/thunder", ["WebSocket /ws3,", "live speech"], mono_title=False)
    s.node(990, 130, 250, 75, MINT, "wave", "Deepgram nova-3", ["STT: words + timestamps"], mono_title=False)
    s.node(990, 240, 250, 75, MINT, "chip", "LiveKit Inference", ["coach: gemma-4-31b-it"], mono_title=False)
    s.node(990, 355, 250, 90, CORAL, "speaker-clock", "Rime mistv3/thunder", ["REST, pre-synthesizes each", "line ahead of its UI message"], mono_title=False)
    s.node(990, 540, 250, 75, MINT, "chip", "LiveKit Inference", ["judge: gpt-5.4-mini"], mono_title=False)
    # Analysis
    s.node(760, 740, 160, 100, GOLD, "gauge", "metrics.py", ["wpm, pauses,", "fillers, loudness"])
    s.node(960, 740, 200, 100, GOLD, "scale", "judge.py", ["+ skills/judge/*.md"])
    s.node(1195, 740, 170, 100, GOLD, "film", "render.py", ["V1 / V2 / V3 clips"])
    # Standalone Rime REST
    s.node(1405, 740, 225, 100, CORAL, "doc-speaker", "Rime mistv3/thunder", ["REST /v1/rime-tts,", "offline clips"], mono_title=False)

    # ---- edges -----------------------------------------------------------------------
    # Browser internals
    s.edge([(180, 269), (180, 325)], LAVENDER)
    s.edge([(160, 421), (160, 495)], LAVENDER)
    s.edge([(275, 421), (275, 655)], LAVENDER)
    # App <-- WebRTC audio + data channel --> Room
    s.edge([(295, 373), (435, 373)], MINT, both=True, label="WebRTC audio", at=(365, 348))
    s.pill(365, 398, "+ data channel", MINT)
    # Room <-- WebRTC --> Orch
    s.edge([(555, 373), (650, 373)], MINT, both=True, label="WebRTC", at=(602, 348))
    # Orch --> STT --> Orch  (elbow up to the top service)
    s.edge([(870, 230), (930, 230), (930, 167), (990, 167)], MINT, both=True)
    # Orch --> LLM1 --> Orch
    s.edge([(870, 277), (990, 277)], MINT, both=True)
    # Orch --> TTS_PREP -->|frames| Orch
    s.edge([(870, 400), (990, 400)], CORAL, both=True, label="frames", at=(930, 424))
    # Orch --> TTS_WS --> Room
    s.edge([(737, 430), (737, 470)], CORAL)
    s.edge([(635, 515), (495, 515), (495, 420)], CORAL, label="live speech · 24 kHz", at=(575, 540))
    # Orch -->|analyze| Metrics --> Judge
    s.edge([(855, 430), (855, 740)], GOLD, label="analyze", at=(855, 610))
    s.edge([(920, 790), (960, 790)], GOLD)
    # Judge --> LLM2 --> Judge
    s.edge([(1050, 740), (1050, 615)], MINT)
    s.edge([(1100, 615), (1100, 740)], MINT)
    # Judge -->|v3_markup| Render --> TTS_REST
    s.edge([(1160, 790), (1195, 790)], GOLD, label="v3_markup", at=(1178, 815))
    s.edge([(1365, 790), (1405, 790)], CORAL)
    # Render -->|clip paths| Orch   (loops over the top, into the orchestrator)
    s.edge([(1290, 740), (1290, 68), (850, 68), (850, 200)], GOLD, label="clip paths", at=(1070, 68))
    # TTS_REST -.pre-rendered clips.-> Orch
    s.edge([(1517, 740), (1517, 45), (820, 45), (820, 200)], CORAL, dashed=True, label="pre-rendered clips", at=(1200, 45))
    s.flush_headers()

    # ---- legend (centred) ------------------------------------------------------------
    ly = 912
    items = [(LAVENDER, "Podium code"), (MINT, "LiveKit  ·  Deepgram"), (CORAL, "Rime"), (GOLD, "analysis, offline")]
    lines = [("live call path", False), ("offline, ahead of the call", True)]
    widths = [22 + len(t) * 6.6 + 34 for _, t in items] + [46 + len(t) * 6.6 + 34 for t, _ in lines]
    x = (W - sum(widths) + 34) / 2
    for (c, txt), w in zip(items, widths):
        s.add(f'<rect x="{x:.1f}" y="{ly - 7}" width="14" height="14" rx="4" fill="{c}" fill-opacity="0.18" stroke="{c}" stroke-width="1.4"/>')
        s.add(f'<text x="{x + 22:.1f}" y="{ly + 4}" font-family="{UI}" font-size="12" fill="{INK}" fill-opacity="0.78">{esc(txt)}</text>')
        x += w
    for (txt, dashed), w in zip(lines, widths[len(items):]):
        dash = ' stroke-dasharray="7 6"' if dashed else ""
        s.add(f'<path d="M{x:.1f} {ly}h36" stroke="{INK}" stroke-width="1.8" stroke-opacity="0.8"{dash} marker-end="url(#arrow-f5f5f7)"/>')
        s.add(f'<text x="{x + 46:.1f}" y="{ly + 4}" font-family="{UI}" font-size="12" fill="{INK}" fill-opacity="0.78">{esc(txt)}</text>')
        x += w

    body = "\n  ".join(s.parts)
    return (
        f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="arch-title arch-desc">\n'
        "  <title id=\"arch-title\">Podium architecture: module wiring</title>\n"
        "  <desc id=\"arch-desc\">Browser (index.html, app.js, orb.js, dashboard.js) talks WebRTC audio and a data channel to a LiveKit Cloud room; "
        "the agent process's PodiumOrchestrator joins the same room and drives Deepgram nova-3 STT, LiveKit Inference coach and judge models, "
        "Rime over WebSocket for live speech and over REST to pre-synthesize lines; the pure-Python analysis lane (metrics.py, judge.py, render.py) "
        "produces V1/V2/V3 contrast clips through Rime REST, which loop back to the orchestrator as clip paths and pre-rendered clips.</desc>\n"
        f"  {body}\n</svg>\n"
    )


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(Path.cwd())}")

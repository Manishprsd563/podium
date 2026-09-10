"""CONTRACTS.md §8: `SessionGraph`, the temporal coaching graph, and cross-session
`Progress`.

Pure Python (stdlib + json only). No LiveKit import — `session_agent.py` writes to
the graph at every step and reads the LLM's steering context back from it, and
evidence scripts must be able to build/inspect a graph without a room. Nothing
here calls `session.say` or touches the room.

Node keys are ``f"{kind}:{id}"`` (e.g. ``"slide:3"``, ``"improvement:imp_1"``,
``"attempt:imp_1#2"``). Edges are directed: ``link(src, rel, dst)`` means
``src -rel-> dst`` using the same ``"kind:id"`` strings, per the §8 edge table
(e.g. ``verdict -scores-> attempt`` means the verdict node is `src`, the attempt
node is `dst`).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# -- SessionGraph -------------------------------------------------------


def _attempt_number(node_id: str) -> int:
    """`attempt`/`verdict` node ids are `"<imp_id>#<n>"`; extract `n`."""
    if "#" not in node_id:
        return 0
    try:
        return int(node_id.rsplit("#", 1)[1])
    except ValueError:
        return 0


def _owning_improvement(node_id: str) -> str:
    """`attempt`/`verdict` node ids are `"<imp_id>#<n>"`; extract `imp_id`."""
    return node_id.split("#", 1)[0]


def _truncate_sentence(text: str, max_chars: int) -> str:
    """Cut `text` to `max_chars`, backing up to the last sentence boundary
    (`". "`) inside the window so the LLM never gets a mid-word/mid-clause tail."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind(". ")
    if boundary == -1:
        boundary = cut.rfind(".")
    if boundary > 0:
        return cut[: boundary + 1]
    return cut.rstrip() + "\u2026"


class SessionGraph:
    """One in-process graph per session. No external store."""

    def __init__(self, t0_mono: float) -> None:
        self._t0 = t0_mono
        # insertion-ordered: iteration order is add() call order, which callers
        # use as the natural chronological/curriculum order (e.g. improvement
        # ids in judge-returned order) for "remaining items" and "last 3".
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self._focus: str | None = None
        self._progress: Progress | None = None

    def _now(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    # -- writes -----------------------------------------------------

    def add(self, kind: str, id: str, **props: Any) -> str:
        """Upsert a node. First call sets `t`; later calls merge `props` and
        bump `updated_t`, leaving the original `t` alone (pass `t=` explicitly
        to override either)."""
        key = f"{kind}:{id}"
        t = props.pop("t", None)
        stamp = t if t is not None else self._now()
        node = self.nodes.get(key)
        if node is None:
            self.nodes[key] = {"kind": kind, "id": id, "t": stamp, "updated_t": stamp, "props": dict(props)}
        else:
            node["props"].update(props)
            node["updated_t"] = stamp
        return key

    def link(self, src: str, rel: str, dst: str, **props: Any) -> None:
        """Add an edge, deduping on identical `(src, rel, dst)` — a repeat call
        merges `props` into the existing edge instead of appending a duplicate."""
        t = props.pop("t", None)
        for edge in self.edges:
            if edge["src"] == src and edge["rel"] == rel and edge["dst"] == dst:
                edge["props"].update(props)
                return
        self.edges.append({"src": src, "rel": rel, "dst": dst, "t": t if t is not None else self._now(), "props": dict(props)})

    def set_focus(self, node_key: str | None) -> None:
        """Point the coach's "what are we working on" context at `node_key`
        (an `add()`-returned key), or clear it with `None`."""
        self._focus = node_key

    def focus(self) -> str | None:
        """The currently focused node key, or `None`."""
        return self._focus

    def mark(self, node_key: str, **props: Any) -> None:
        """Shortcut for updating props on an already-added node, e.g.
        `mark("improvement:imp_1", heard=True, played=["v0", "v2"])`."""
        node = self.nodes.get(node_key)
        if node is None:
            raise KeyError(f"no such node: {node_key}")
        node["props"].update(props)
        node["updated_t"] = self._now()

    def attach_progress(self, progress: "Progress") -> None:
        """Optional. Once attached, `context_text()` adds a "skills touched"
        section reporting mastery for every skill any improvement in this
        session trains."""
        self._progress = progress

    # -- reads -----------------------------------------------------

    def _edges_by_rel(self, rel: str) -> list[dict[str, Any]]:
        return [e for e in self.edges if e["rel"] == rel]

    def _verdict_props_for(self, attempt_key: str) -> dict[str, Any] | None:
        for e in self._edges_by_rel("scores"):
            if e["dst"] == attempt_key:
                vnode = self.nodes.get(e["src"])
                if vnode is not None:
                    return vnode["props"]
        return None

    def _about_target(self, utterance_key: str) -> str | None:
        for e in self._edges_by_rel("about"):
            if e["src"] == utterance_key:
                return e["dst"]
        return None

    def _improvement_nodes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(k, n) for k, n in self.nodes.items() if n["kind"] == "improvement"]

    def _focus_section(self) -> str:
        focus_key = self._focus
        node = self.nodes.get(focus_key) if focus_key else None
        if node is None:
            return "Nothing is focused yet."
        props = node["props"]
        if node["kind"] == "improvement":
            bits = [f"Working on {node['id']}: {props.get('issue', 'no issue recorded yet')}."]
            if props.get("skill"):
                bits.append(f"Skill: {props['skill']}.")
            if props.get("slide") is not None:
                bits.append(f"Slide: {props['slide']}.")
            if props.get("quote"):
                bits.append(f'Quote: "{props["quote"]}".')
            played = props.get("played") or []
            bits.append(f"Played: {', '.join(played) if played else 'nothing yet'}.")
            bits.append(f"Heard: {'yes' if props.get('heard') else 'no'}.")
            return " ".join(bits)
        prop_bits = ", ".join(f"{k}={v}" for k, v in props.items())
        return f"Focused on {focus_key}" + (f" ({prop_bits})" if prop_bits else "") + "."

    def _attempts_section(self) -> str:
        """All attempts recorded so far (not only ones on the current focus) so
        the coach can reference earlier practice even after moving on."""
        attempt_items = [(k, n) for k, n in self.nodes.items() if n["kind"] == "attempt"]
        if not attempt_items:
            return ""
        attempt_items.sort(key=lambda kv: (_owning_improvement(kv[1]["id"]), _attempt_number(kv[1]["id"])))
        sentences = []
        for akey, anode in attempt_items:
            imp_id = _owning_improvement(anode["id"])
            n = _attempt_number(anode["id"])
            verdict = self._verdict_props_for(akey)
            if verdict is None:
                sentences.append(f"{imp_id} attempt {n}: not yet scored.")
                continue
            before = verdict.get("original_fillers", "?")
            after = (verdict.get("fillers") or {}).get("count", "?")
            landed = (verdict.get("pauses") or {}).get("landed")
            landed_txt = "yes" if landed else ("no" if landed is not None else "unknown")
            pace = verdict.get("pace_band", "unknown")
            wins = verdict.get("wins") or []
            wins_txt = ", ".join(wins) if wins else "none yet"
            sentences.append(
                f"{imp_id} attempt {n}: fillers {before}\u2192{after}, pause landed: {landed_txt}, "
                f"pace: {pace}, wins: {wins_txt}."
            )
        return "Attempts so far: " + " ".join(sentences)

    def _utterances_section(self) -> str:
        utter_items = [(k, n) for k, n in self.nodes.items() if n["kind"] == "utterance"]
        if not utter_items:
            return ""
        bits = []
        for key, node in utter_items[-3:]:
            about = self._about_target(key)
            text = node["props"].get("text", "")
            bits.append(f'"{text}" (about {about})' if about else f'"{text}"')
        return "Recent remarks: " + "; ".join(bits) + "."

    def _remaining_section(self) -> str:
        imp_nodes = self._improvement_nodes()
        if not imp_nodes:
            return ""
        remaining = [n["id"] for _, n in imp_nodes if not n["props"].get("heard") and not n["props"].get("skipped")]
        if not remaining:
            return "All improvements have been covered."
        return "Items remaining: " + ", ".join(remaining) + "."

    def _skills_section(self) -> str:
        if self._progress is None:
            return ""
        skills_seen = {n["props"].get("skill") for _, n in self._improvement_nodes() if n["props"].get("skill")}
        if not skills_seen:
            return ""
        bits = []
        for sid in sorted(skills_seen):
            info = self._progress.skills.get(sid)
            if info is not None:
                bits.append(f"{sid} (mastery {info['mastery']:.2f})")
        if not bits:
            return ""
        return "Skills touched: " + ", ".join(bits) + "."

    def context_text(self, max_chars: int = 1200) -> str:
        """Compact plain-English steering block for the coach LLM, in order:
        header; focus; attempts+verdicts; last 3 utterances; remaining items;
        skills touched (only if `attach_progress` was called)."""
        parts = ["Session so far:", self._focus_section()]
        for section in (self._attempts_section(), self._utterances_section(), self._remaining_section(), self._skills_section()):
            if section:
                parts.append(section)
        return _truncate_sentence(" ".join(parts), max_chars)

    def _best_verdict(self) -> dict[str, Any] | None:
        verdicts = [n["props"] for n in self.nodes.values() if n["kind"] == "verdict"]
        if not verdicts:
            return None
        return max(verdicts, key=lambda v: (len(v.get("wins") or []), v.get("on_point", 0)))

    def recap_text(self) -> str:
        """2-4 spoken sentences: progress count, practice count, best verdict
        wins, what remains."""
        imp_nodes = self._improvement_nodes()
        total = len(imp_nodes)
        done = sum(1 for _, n in imp_nodes if n["props"].get("heard"))
        attempt_nodes = [n for n in self.nodes.values() if n["kind"] == "attempt"]
        practiced_ids = {e["dst"] for e in self._edges_by_rel("practices")}

        sentences: list[str] = []
        if total:
            sentences.append(f"We've gotten through {done} of {total} improvements so far.")
        else:
            sentences.append("We haven't started on any improvements yet.")

        if attempt_nodes:
            sentences.append(
                f"You practiced {len(practiced_ids)} of them out loud, {len(attempt_nodes)} attempts in all."
            )

        best = self._best_verdict()
        if best is not None:
            wins_txt = ", ".join(best.get("wins") or [])
            if wins_txt:
                sentences.append(f"Best moment so far: {wins_txt}.")

        remaining = [n["id"] for _, n in imp_nodes if not n["props"].get("heard") and not n["props"].get("skipped")]
        if remaining:
            sentences.append("Still to go: " + ", ".join(remaining) + ".")
        elif total:
            sentences.append("That's everything on the list.")

        return " ".join(sentences[:4])

    # -- persistence -----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Snapshot for `session.json["graph"]` (§8). Defensive-copies `props`
        dicts so later graph mutation cannot leak into an already-persisted
        snapshot."""
        return {
            "t0_mono": self._t0,
            "nodes": {k: {**v, "props": dict(v["props"])} for k, v in self.nodes.items()},
            "edges": [dict(e, props=dict(e["props"])) for e in self.edges],
            "focus": self._focus,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "SessionGraph":
        """Inverse of `to_json`; reconstructs a graph anchored to the same
        `t0_mono` origin so restored `t` values still line up with the timeline."""
        graph = cls(d.get("t0_mono", 0.0))
        graph.nodes = {k: {**v, "props": dict(v.get("props", {}))} for k, v in d.get("nodes", {}).items()}
        graph.edges = [dict(e, props=dict(e.get("props", {}))) for e in d.get("edges", [])]
        graph._focus = d.get("focus")
        return graph


# -- Progress -------------------------------------------------------

# CONTRACTS.md §9 curriculum ladder.
SKILLS = [
    "hook", "structure", "pacing", "pausing", "fillers",
    "vocal-variety", "storytelling", "slide-connection", "closing", "articulation",
]

# CONTRACTS.md §3 judge score categories -> curriculum skills each one trains.
_CATEGORY_SKILLS = {
    "delivery": ["pacing", "pausing", "fillers", "vocal-variety"],
    "clarity": ["articulation", "structure"],
    "structure": ["structure", "hook", "closing", "storytelling"],
    "slide_connection": ["slide-connection"],
    "pronunciation": ["articulation"],
}

# Inverse: skill -> score categories that feed its mastery EMA. `structure` and
# `articulation` are trained by two categories each and get the mean of both.
_SKILL_CATEGORIES: dict[str, list[str]] = {}
for _cat, _skills in _CATEGORY_SKILLS.items():
    for _skill in _skills:
        _SKILL_CATEGORIES.setdefault(_skill, []).append(_cat)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Progress:
    """Cross-session mastery, persisted at `sessions/progress/<client_id>.json`."""

    LEVELS = ["Novice", "Speaker", "Presenter", "Keynote", "TEDx-ready"]
    _ALPHA = 0.4
    _BONUS = 0.05

    def __init__(self, client_id: str, root: Path = Path("sessions/progress")) -> None:
        self.client_id = client_id
        self._root = root
        self._path = root / f"{client_id}.json"
        self.skills: dict[str, dict[str, Any]] = {sid: {"mastery": 0.0, "sessions": 0, "last_focus": None} for sid in SKILLS}
        self.total_sessions = 0
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        stored = data.get("skills", {})
        for sid in SKILLS:
            if sid in stored:
                self.skills[sid].update(stored[sid])
        self.total_sessions = data.get("total_sessions", 0)

    def _save(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "client_id": self.client_id,
            "skills": self.skills,
            "total_sessions": self.total_sessions,
            "updated_at": _today(),
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def record_session(self, scores: dict[str, Any], improvements: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> None:
        """`scores` is the §3 judgment.scores dict. `improvements` is the §3
        judgment.improvements list (each needs `id` and `skill`). `verdicts` is
        a list of §7 verdict dicts, each augmented by the caller with an
        `improvement_id` key naming which improvement the attempt practiced —
        that's how a >=2-win attempt's +0.05 bonus is attributed to a skill;
        verdicts without that key only count toward the score-driven EMA via
        their improvement's skill, not the bonus."""
        touched: set[str] = set()

        for skill, categories in _SKILL_CATEGORIES.items():
            present = [c for c in categories if c in scores]
            if not present:
                continue
            avg_scaled = sum(scores[c] / 5.0 for c in present) / len(present)
            old = self.skills[skill]["mastery"]
            self.skills[skill]["mastery"] = self._ALPHA * avg_scaled + (1 - self._ALPHA) * old
            touched.add(skill)

        best_wins_by_improvement: dict[str, int] = {}
        for v in verdicts:
            imp_id = v.get("improvement_id")
            if not imp_id:
                continue
            wins = len(v.get("wins") or [])
            best_wins_by_improvement[imp_id] = max(best_wins_by_improvement.get(imp_id, 0), wins)

        for imp in improvements:
            skill = imp.get("skill")
            if skill not in self.skills:
                continue
            touched.add(skill)
            if best_wins_by_improvement.get(imp.get("id"), 0) >= 2:
                self.skills[skill]["mastery"] = min(1.0, self.skills[skill]["mastery"] + self._BONUS)

        today = _today()
        for skill in touched:
            self.skills[skill]["sessions"] += 1
            self.skills[skill]["last_focus"] = today

        self.total_sessions += 1
        self._save()

    def level(self) -> str:
        """§8 curriculum level label, derived from the mean mastery across all
        skills (not weighted by session count)."""
        values = [info["mastery"] for info in self.skills.values()]
        mean = sum(values) / len(values) if values else 0.0
        if mean < 0.3:
            return "Novice"
        if mean < 0.5:
            return "Speaker"
        if mean < 0.7:
            return "Presenter"
        if mean < 0.85:
            return "Keynote"
        return "TEDx-ready"

    def next_focus(self) -> str | None:
        """The skill to recommend next: lowest mastery, ties broken toward the
        skill practiced in fewer sessions."""
        if not self.skills:
            return None
        ordered = sorted(SKILLS, key=lambda s: (self.skills[s]["mastery"], self.skills[s]["sessions"]))
        return ordered[0]

    def to_message(self) -> dict[str, Any]:
        """CONTRACTS.md §5 `progress` message."""
        return {
            "type": "progress",
            "client_id": self.client_id,
            "level": self.level(),
            "levels": list(self.LEVELS),
            "skills": {sid: dict(info) for sid, info in self.skills.items()},
            "next_focus": self.next_focus(),
        }

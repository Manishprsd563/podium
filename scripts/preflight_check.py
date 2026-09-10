#!/usr/bin/env python
"""Local secret/config-hygiene preflight for the Podium submission package.

The hackathon organizers said they would provide a preflight script; it was
never added to this repo, so this is the local stand-in referenced by
RIME_EVIDENCE.md / SUBMISSION.md. Stdlib only; no network unless --online.

Usage:
    python scripts/preflight_check.py [TARGET_DIR] [--repo-root ROOT] [--online]

TARGET_DIR defaults to submission/podium under the repo root; it is the
directory that gets scanned for leaked secrets and inspected for the shipped
Rime configuration. --repo-root (default: this script's grandparent dir) is
used to check .env's gitignore status and, with --online, to read
RIME_API_KEY out of the real (uncommitted) .env.

Exit code is non-zero if any check in (a)/(b)/(c) finds a problem.
Writes: nothing (stdout only). Needs: RIME_API_KEY in .env, but only when
--online is passed; the default (offline) checks need no env vars.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent

TEXT_EXTS = {
    ".py", ".md", ".txt", ".json", ".js", ".html", ".css", ".yml", ".yaml",
    ".toml", ".cfg", ".ini", ".sh", ".ps1", ".gitignore", ".jsonl", "",
}

SECRET_NEAR_WORD_RE = re.compile(
    r"(?i)(key|secret|token)\W{0,12}[A-Za-z0-9_-]{32,}|[A-Za-z0-9_-]{32,}\W{0,12}(?i:key|secret|token)"
)
APIN_RE = re.compile(r"APIn[A-Za-z0-9]+")
SK_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
LIVEKIT_URL_RE = re.compile(r"wss://([a-z0-9-]+)\.livekit\.cloud")


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTS or path.name in {".env.example", ".gitignore"}:
        return True
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(2048)
        if b"\x00" in chunk:
            return False
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def check_env_gitignored(repo_root: Path, target: Path) -> list[str]:
    problems = []
    gitignore = repo_root / ".gitignore"
    gitignore_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    ignored = any(
        line.strip() in (".env", "/.env", ".env*") or line.strip() == ".env/"
        for line in gitignore_text.splitlines()
    )
    if not ignored:
        problems.append(f"(a) .env is not listed in {gitignore} — it must be gitignored")

    for env_file in target.rglob(".env"):
        problems.append(f"(a) found a real .env file inside the submission package: {env_file}")

    return problems


PLACEHOLDER_MARKERS = ("your_", "your-project")


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic for check_env_example_placeholders: a real credential looks like a
    long random alphanumeric string or a known key prefix, a placeholder does not."""
    if any(m in value for m in PLACEHOLDER_MARKERS):
        return False
    if SK_RE.match(value) or APIN_RE.match(value) or value.startswith("rime_"):
        return True
    if len(value) >= 24 and value.isalnum() and re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value):
        return True
    return False


def check_env_example_placeholders(env_example: Path) -> list[str]:
    """(b) .env.example must ship placeholder values only -- if a real key ever got
    pasted in here it would be committed straight into the public repo."""
    problems = []
    if not env_example.exists():
        problems.append(f"(b) {env_example} does not exist")
        return problems
    for lineno, line in enumerate(env_example.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if not value:
            continue
        if _looks_like_real_secret(value):
            problems.append(f"(b) {env_example}:{lineno}: {key} looks like a real credential, not a placeholder")
    return problems


def check_secret_scan(target: Path, real_livekit_project: str | None) -> list[str]:
    """(c) Line-by-line scan of every text file in the packaged target for anything
    that looks like a leaked key/secret/token, or the real (non-placeholder) LiveKit
    project URL specifically (a bare "wss://your-project..." placeholder is fine)."""
    problems = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or not _is_text_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(target)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if SECRET_NEAR_WORD_RE.search(line):
                problems.append(f"(c) {rel}:{lineno}: token resembling a key/secret/token")
            if APIN_RE.search(line):
                problems.append(f"(c) {rel}:{lineno}: matches APIn... key pattern")
            if SK_RE.search(line):
                problems.append(f"(c) {rel}:{lineno}: matches sk-... key pattern")
            for m in LIVEKIT_URL_RE.finditer(line):
                project = m.group(1)
                if project == "your-project":
                    continue
                if real_livekit_project and project == real_livekit_project:
                    problems.append(f"(c) {rel}:{lineno}: leaks the real LiveKit project URL ({m.group(0)})")
                elif not real_livekit_project:
                    problems.append(f"(c) {rel}:{lineno}: non-placeholder LiveKit URL ({m.group(0)})")
    return problems


def read_real_livekit_project(repo_root: Path) -> str | None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("LIVEKIT_URL="):
            m = LIVEKIT_URL_RE.search(line)
            if m:
                return m.group(1)
    return None


def read_env_value(repo_root: Path, key: str) -> str | None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def print_shipped_config(target: Path) -> dict:
    """Regex-scrape the shipped Rime config straight out of the packaged source
    (not .env) so the printed report reflects what the code actually ships, not
    what a local .env happens to be set to."""
    cfg: dict[str, str] = {}
    session_agent = target / "agent" / "session_agent.py"
    render_py = target / "analysis" / "render.py"

    if session_agent.exists():
        text = session_agent.read_text(encoding="utf-8")
        for key, pat in (
            ("RIME_MODEL", r'RIME_MODEL\s*=\s*os\.environ\.get\("RIME_MODEL",\s*"([^"]+)"\)'),
            ("RIME_SPEAKER", r'RIME_SPEAKER\s*=\s*os\.environ\.get\("RIME_SPEAKER",\s*"([^"]+)"\)'),
            ("RIME_LANG", r'RIME_LANG\s*=\s*"([^"]+)"'),
            ("SAMPLE_RATE", r"SAMPLE_RATE\s*=\s*(\d+)"),
        ):
            m = re.search(pat, text)
            if m:
                cfg[key] = m.group(1)
        m = re.search(r'use_websocket=True', text)
        if m:
            cfg["live_transport"] = "WebSocket (rime.TTS use_websocket=True)"
        m = re.search(r'"/ws3"|ws3', text)
        if m:
            cfg["ws_endpoint"] = "/ws3"

    if render_py.exists():
        text = render_py.read_text(encoding="utf-8")
        m = re.search(r'RIME_TTS_URL\s*=\s*"([^"]+)"', text)
        if m:
            cfg["rest_endpoint"] = m.group(1)
        m = re.search(r"RATE\s*=\s*(\d+)", text)
        if m:
            cfg.setdefault("SAMPLE_RATE", m.group(1))

    print("Shipped Rime configuration (read from agent/session_agent.py + analysis/render.py):")
    for k, v in cfg.items():
        print(f"  {k} = {v}")
    if not cfg:
        print("  (could not locate agent/session_agent.py or analysis/render.py under target)")
    return cfg


def online_voice_check(repo_root: Path, cfg: dict) -> list[str]:
    """(d) --online only: confirms the shipped RIME_SPEAKER still exists in Rime's
    live voice catalog, so a renamed/retired speaker fails loudly before submission."""
    problems: list[str] = []
    api_key = read_env_value(repo_root, "RIME_API_KEY")
    if not api_key:
        problems.append("(d) --online requested but RIME_API_KEY is not set in .env")
        return problems
    req = urllib.request.Request(
        "https://users.rime.ai/data/voices/all-v2.json",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"(d) online voice check failed: {exc}")
        return problems
    speaker = cfg.get("RIME_SPEAKER", "thunder")
    if speaker not in body:
        problems.append(f"(d) speaker '{speaker}' not found in /data/voices/all-v2.json response")
    else:
        print(f"(d) online check OK: speaker '{speaker}' present in /data/voices/all-v2.json")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default=None, help="directory to scan (default: <repo-root>/submission/podium)")
    ap.add_argument("--repo-root", type=str, default=str(REPO_ROOT_DEFAULT))
    ap.add_argument("--online", action="store_true")
    args = ap.parse_args()

    repo_root = Path(args.repo_root).resolve()
    target = Path(args.target).resolve() if args.target else (repo_root / "submission" / "podium")

    print(f"repo root : {repo_root}")
    print(f"target    : {target}")
    print("Note: the hackathon organizers' own preflight script was not available in this")
    print("repo at submission time; this script is the local stand-in.")
    print()

    if not target.exists():
        print(f"ERROR: target directory does not exist: {target}", file=sys.stderr)
        return 2

    problems: list[str] = []
    problems += check_env_gitignored(repo_root, target)
    problems += check_env_example_placeholders(target / ".env.example")
    real_project = read_real_livekit_project(repo_root)
    problems += check_secret_scan(target, real_project)

    print()
    cfg = print_shipped_config(target)
    print()

    if args.online:
        problems += online_voice_check(repo_root, cfg)

    print()
    if problems:
        print(f"PREFLIGHT FAILED — {len(problems)} finding(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("PREFLIGHT OK — no findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

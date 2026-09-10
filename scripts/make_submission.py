#!/usr/bin/env python
"""Build a submit-ready copy of the repo under submission/podium/, plus an
optional trimmed demo recording under submission/demo/, then zip the whole
submission/ directory into submission.zip.

Stdlib only. Usage:
    python scripts/make_submission.py [--no-zip] [--demo PATH]

Run from anywhere; paths are resolved relative to the repo root (the parent
directory of this scripts/ folder).
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBMISSION_DIR = REPO_ROOT / "submission"
PODIUM_DIR = SUBMISSION_DIR / "podium"
ZIP_PATH = REPO_ROOT / "submission.zip"

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# Directory basenames that are never descended into, wherever they occur.
EXCLUDE_DIR_NAMES = {
    ".venv",
    "node_modules",
    "__pycache__",
    "sessions",
    "submission",
    "video",
    "videos",
    ".git",
}

# Repo-root-relative directories (posix-style) that are skipped entirely.
EXCLUDE_RELATIVE_DIRS = {
    "demo",
    # The README's embedded copy of the pitch film. The package already carries
    # the full-quality cut at submission/demo/, so shipping the web-sized copy
    # inside the source tree too would just double ~13 MB.
    "docs/demo",
    "evidence/probe",
    "evidence/smoke",
}

# Repo-root-relative files that are never copied.
EXCLUDE_RELATIVE_FILES = {
    ".env",
    "submission.zip",
    "Rime PS.pdf",
    "RIME_HACKATHON_RESEARCH.md",
}

EXCLUDE_FILE_GLOBS = ("*.pyc",)


def _rel_posix(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _should_skip_dir(rel_posix: str, name: str) -> bool:
    if name in EXCLUDE_DIR_NAMES:
        return True
    if rel_posix in EXCLUDE_RELATIVE_DIRS:
        return True
    return False


def _should_skip_file(rel_path: Path, size: int) -> bool:
    """evidence/ is exempt from the size cap because its WAV clips are the cited
    proof (RIME_EVIDENCE.md); anything else over the cap is an accidental large asset."""
    rel_posix = rel_path.as_posix()
    if rel_posix in EXCLUDE_RELATIVE_FILES:
        return True
    name = rel_path.name
    for pat in EXCLUDE_FILE_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True
    if size > MAX_FILE_BYTES and not rel_posix.startswith("evidence/"):
        return True
    return False


def copy_tree(dest: Path) -> tuple[int, int]:
    """Copy the repo working tree into dest, honouring the exclusion rules.
    Returns (files_copied, files_skipped_for_size)."""
    copied = 0
    skipped_size = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirpath_p = Path(dirpath)
        rel_dir = "" if dirpath_p == REPO_ROOT else _rel_posix(dirpath_p)

        # Prune excluded subdirectories in place so os.walk never descends.
        pruned = []
        for d in dirnames:
            child_rel = f"{rel_dir}/{d}" if rel_dir else d
            if _should_skip_dir(child_rel, d):
                continue
            pruned.append(d)
        dirnames[:] = pruned

        for fname in filenames:
            src = dirpath_p / fname
            rel_path = src.relative_to(REPO_ROOT)
            try:
                size = src.stat().st_size
            except OSError:
                continue
            if _should_skip_file(rel_path, size):
                if size > MAX_FILE_BYTES:
                    skipped_size += 1
                continue
            dst = dest / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    return copied, skipped_size


def zip_submission() -> int:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    store_exts = {".mp4", ".wav"}
    with zipfile.ZipFile(ZIP_PATH, "w") as zf:
        for dirpath, _dirnames, filenames in os.walk(SUBMISSION_DIR):
            dirpath_p = Path(dirpath)
            for fname in filenames:
                src = dirpath_p / fname
                arcname = Path("submission") / src.relative_to(SUBMISSION_DIR)
                method = zipfile.ZIP_STORED if src.suffix.lower() in store_exts else zipfile.ZIP_DEFLATED
                zf.write(src, arcname.as_posix(), compress_type=method)
    return ZIP_PATH.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true", help="skip building submission.zip")
    ap.add_argument("--demo", type=str, default=None, help="path to a demo video to place under submission/demo/")
    args = ap.parse_args()

    if SUBMISSION_DIR.exists():
        shutil.rmtree(SUBMISSION_DIR)
    PODIUM_DIR.mkdir(parents=True, exist_ok=True)

    copied, skipped_size = copy_tree(PODIUM_DIR)
    print(f"copied {copied} files into {PODIUM_DIR}")
    if skipped_size:
        print(f"skipped {skipped_size} file(s) over 50 MB (outside evidence/)")

    # Explicitly (re)write .env.example verbatim, in case any future exclusion
    # rule would otherwise touch it.
    env_example_src = REPO_ROOT / ".env.example"
    if env_example_src.exists():
        shutil.copy2(env_example_src, PODIUM_DIR / ".env.example")
        print("wrote podium/.env.example")
    else:
        print("WARNING: repo .env.example not found", file=sys.stderr)

    if args.demo:
        demo_src = Path(args.demo)
        if not demo_src.is_absolute():
            demo_src = (REPO_ROOT / demo_src).resolve()
        if not demo_src.exists():
            print(f"ERROR: --demo path does not exist: {demo_src}", file=sys.stderr)
            return 1
        demo_dir = SUBMISSION_DIR / "demo"
        demo_dir.mkdir(parents=True, exist_ok=True)
        dst = demo_dir / demo_src.name
        shutil.copy2(demo_src, dst)
        print(f"copied demo video to {dst} ({dst.stat().st_size / 1e6:.1f} MB)")

    # Configuration-hygiene gate: run the preflight against the packaged tree and keep its output.
    preflight = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "preflight_check.py"), str(PODIUM_DIR)],
        capture_output=True, text=True,
    )
    (SUBMISSION_DIR / "PREFLIGHT.txt").write_text(preflight.stdout + preflight.stderr, encoding="utf-8")
    print(f"preflight exit {preflight.returncode} -> submission/PREFLIGHT.txt")
    if preflight.returncode != 0:
        print("ERROR: preflight found issues; fix them before submitting", file=sys.stderr)
        return preflight.returncode

    template = REPO_ROOT / "scripts" / "SUBMISSION_TEMPLATE.md"
    if template.exists():
        demo_line = f"`demo/{Path(args.demo).name}`" if args.demo else "_not bundled — add with `--demo <path>`_"
        (SUBMISSION_DIR / "SUBMISSION.md").write_text(
            template.read_text(encoding="utf-8").replace("{{DEMO}}", demo_line), encoding="utf-8"
        )
        print("wrote submission/SUBMISSION.md")

    if not args.no_zip:
        size = zip_submission()
        print(f"wrote {ZIP_PATH} ({size / 1e6:.1f} MB)")
    else:
        print("skipped zip (--no-zip)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

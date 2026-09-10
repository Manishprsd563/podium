"""Synthesize the Podium pitch video narration (n01..n13, m01..m08, c01..c12) with Rime.

Parses `videos/podium-pitch/SCRIPT.md` for lines shaped `- <id>: "text"` where
`<id>` matches `^[nmc]\\d{2}$`, resolves the speaker from the id prefix per
SCRIPT.md's cast table (`n` -> astra / NARRATOR, `m` -> willow / MAYA,
`c` -> thunder / COACH), synthesizes each with Rime REST (`mistv3`, 24 kHz
mono WAV, `pauseBetweenBrackets: true` so `<NNN>` markers render as silence),
trims excess *leading/trailing* silence only (internal pauses such as
`<2400>`/`<1500>`/`<1200>` are a deliberate part of the performance and are
never touched), and writes:

  - `public/vo/<id>.wav`      -- 24 kHz mono PCM16
  - `public/vo/<id>.txt`      -- sidecar: "<speaker>\\n<text>" last synthesized
  - `public/vo/manifest.json` -- {"<id>": {"speaker","seconds","text"}, ..., "_total_seconds": N}

Re-run is cheap: a line is skipped (no network call) when `<id>.wav` and
`<id>.txt` already exist and the sidecar's speaker+text match the current
script. Changing the speaker for an id therefore forces a re-synthesis even
if the text is unchanged.

Duration is measured from the WAV `data` chunk byte length, not
`wave.getnframes()` -- Rime can leave a placeholder frame count in the RIFF
header on streamed audio (same pattern as analysis/metrics.py::_wav_samples
and evidence/e1_pause_control.py::_samples).

If Rime rejects a speaker outright (as opposed to rejecting `lang=eng`, which
falls back to `lang=en`), this script reports the error and stops -- it never
silently substitutes a different speaker.

Run:  .venv/Scripts/python.exe videos/podium-pitch/tools/narrate.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np
import urllib.error
import urllib.request

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
PROJECT = Path(__file__).resolve().parents[1]
SCRIPT_MD = PROJECT / "SCRIPT.md"
VO_DIR = PROJECT / "public" / "vo"

load_dotenv(ROOT / ".env")
KEY = os.environ.get("RIME_API_KEY", "")
if not KEY or KEY.startswith("your_"):
    sys.exit("RIME_API_KEY missing in .env")

REST = "https://users.rime.ai/v1/rime-tts"
MODEL_ID = "mistv3"
RATE = 24000

# id prefix -> speaker, per SCRIPT.md "## Cast".
SPEAKER_BY_PREFIX = {
    "n": "astra",   # NARRATOR
    "m": "willow",  # MAYA
    "c": "thunder", # COACH
}

VO_LINE_RE = re.compile(r'^-\s+([nmc]\d{2}):\s*"(.*)"\s*$')

# Scene -> ordered VO ids, per SCRIPT.md "## Scenes and lines" (for reporting only).
SCENE_LINES = {
    "S01": ["n01", "n02"],
    "S02": ["m01", "c01", "n03"],
    "S03": ["n04"],
    "S04": ["m02", "m03", "n05"],
    "S05": ["c02", "n06"],
    "S06": ["c03", "m04", "c04", "c05", "c06", "c07", "n07"],
    "S07": ["n08"],
    "S08": ["c08", "m05", "c09", "m06", "c10", "m07", "c11", "n09"],
    "S09": ["n10"],
    "S10": ["m08", "c12", "n11"],
    "S11": ["n12"],
    "S12": ["n13"],
}

# Lines with a deliberate internal pause we want to confirm rendered. All
# three place the `<NNN>` marker mid-clause (not at a sentence/clause
# boundary), which RIME_EVIDENCE.md (claim 2 / E3) documents as rendering
# as low-level breath noise around -30..-25 dBFS on mistv3, not digital
# silence -- a strict digital-silence threshold under-measures or misses it.
INTERNAL_PAUSE_IDS = {"m03", "c07", "m08"}

# Silence trim tuning (matches SCRIPT.md contract + rime_probe.py conventions).
TRIM_THRESH_DB = -45.0
TRIM_MIN_MS = 250.0
TRIM_PAD_MS = 120.0

# Speech-presence threshold for measuring *mid-clause* pauses, per
# RIME_EVIDENCE.md claim 2 / E3 ("E3 therefore measures non-speech gaps at
# a -25 dBFS speech-presence threshold instead"). Reported alongside the
# strict -45 dBFS digital-silence figure so a pause that rendered as breath
# noise (not true silence) is still honestly confirmed rather than missed.
INTERNAL_PAUSE_SPEECH_PRESENCE_DB = -25.0


def parse_script(path: Path) -> list[tuple[str, str]]:
    """Return [(id, text)] for every `- <id>: "..."` line, in file order."""
    lines: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = VO_LINE_RE.match(raw.strip())
        if m:
            lines.append((m.group(1), m.group(2)))
    return lines


def speaker_for(vid: str) -> str:
    prefix = vid[0]
    speaker = SPEAKER_BY_PREFIX.get(prefix)
    if speaker is None:
        sys.exit(f"{vid}: no speaker mapping for prefix {prefix!r} (expected one of {sorted(SPEAKER_BY_PREFIX)})")
    return speaker


def synth(text: str, speaker: str, *, lang: str = "eng") -> bytes:
    body = {
        "text": text,
        "speaker": speaker,
        "modelId": MODEL_ID,
        "lang": lang,
        "samplingRate": RATE,
        "pauseBetweenBrackets": True,
    }
    req = urllib.request.Request(
        REST,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def synth_with_fallback(text: str, speaker: str) -> tuple[bytes, str]:
    """Try lang=eng first (per SCRIPT.md); fall back to lang=en if rejected.

    Never falls back to a different speaker: a speaker-level rejection stops
    the whole run so it can be reported and fixed deliberately."""
    try:
        return synth(text, speaker, lang="eng"), "eng"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  lang=eng rejected ({e.code}): {body[:200]!r} -- retrying lang=en")
        try:
            return synth(text, speaker, lang="en"), "en"
        except urllib.error.HTTPError as e2:
            body2 = e2.read().decode(errors="replace")
            sys.exit(
                f"Rime REST rejected speaker={speaker!r} for text {text[:60]!r}: "
                f"HTTP {e2.code} {body2[:300]!r}"
            )


def wav_data_chunk(raw: bytes) -> tuple[bytes, int, int, int]:
    """Return (pcm16_bytes, sample_rate, n_channels, sample_width) trusting
    the `data` chunk's byte length, not the RIFF header frame count."""
    with wave.open(io.BytesIO(raw)) as w:
        sr, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    idx = raw.find(b"data")
    body = raw[idx + 8:] if idx != -1 else raw[44:]
    usable = len(body) // (width * nch) * width * nch
    return body[:usable], sr, nch, width


def to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def dbfs_windows(x: np.ndarray, sr: int, win_ms: float = 20.0) -> tuple[np.ndarray, int]:
    win = max(1, int(sr * win_ms / 1000))
    n = len(x) // win
    if n == 0:
        return np.array([-120.0]), win
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-12), win


def trim_silence(pcm: bytes, sr: int) -> bytes:
    """Trim leading/trailing silence longer than TRIM_MIN_MS at
    TRIM_THRESH_DB, keeping a TRIM_PAD_MS head/tail pad. No-op if the clip
    has no long silence at either edge. Internal silence (between the first
    and last loud window) is never touched -- deliberate `<NNN>` pauses in
    the middle of a line must survive untrimmed."""
    x = to_float(pcm)
    if len(x) == 0:
        return pcm
    db, win = dbfs_windows(x, sr)
    loud = db > TRIM_THRESH_DB
    if not loud.any():
        return pcm
    first_loud = int(np.argmax(loud))
    last_loud = int(len(loud) - 1 - np.argmax(loud[::-1]))
    lead_ms = first_loud * win / sr * 1000
    tail_ms = (len(loud) - 1 - last_loud) * win / sr * 1000
    pad = int(sr * TRIM_PAD_MS / 1000)
    start = max(0, first_loud * win - pad) if lead_ms > TRIM_MIN_MS else 0
    end_sample = len(x) if tail_ms <= TRIM_MIN_MS else min(len(x), (last_loud + 1) * win + pad)
    if start == 0 and end_sample == len(x):
        return pcm
    trimmed = x[start:end_sample]
    ints = np.clip(trimmed * 32768.0, -32768, 32767).astype(np.int16)
    return ints.tobytes()


GATE_THRESH_DB = -30.0   # mistv3 renders pauses as breath noise around -31 dBFS
GATE_MIN_MS = 200.0      # only real pauses, never the micro-gaps between words
GATE_RAMP_MS = 30.0


def gate_internal_pauses(pcm: bytes, sr: int) -> bytes:
    """Fade the breath-noise floor inside pauses >= GATE_MIN_MS down to digital
    silence, with a GATE_RAMP_MS ramp at both edges so nothing clicks. Pause
    *lengths* are untouched -- only the noise inside them goes. Leading/trailing
    silence is left to trim_silence."""
    x = to_float(pcm).copy()
    if len(x) == 0:
        return pcm
    db, win = dbfs_windows(x, sr)
    quiet = db < GATE_THRESH_DB
    min_win = int(GATE_MIN_MS / 1000 * sr / win)
    ramp = int(sr * GATE_RAMP_MS / 1000)
    i = 0
    while i < len(quiet):
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < len(quiet) and quiet[j]:
            j += 1
        if j - i >= min_win and i > 0 and j < len(quiet):
            a, b = i * win, j * win
            gain = np.zeros(b - a, dtype=np.float32)
            r = min(ramp, (b - a) // 2)
            gain[:r] = np.linspace(1.0, 0.0, r, dtype=np.float32)
            gain[-r:] = np.linspace(0.0, 1.0, r, dtype=np.float32)
            x[a:b] *= gain
        i = j
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16).tobytes()


def longest_internal_silence_ms(pcm: bytes, sr: int, thresh_db: float = TRIM_THRESH_DB) -> float:
    """Longest run of quiet (< thresh_db) windows strictly between the first
    and last loud window -- i.e. a pause *inside* the line, not the leading/
    trailing silence that `trim_silence` already removes."""
    x = to_float(pcm)
    if len(x) == 0:
        return 0.0
    db, win = dbfs_windows(x, sr)
    loud = db > thresh_db
    if not loud.any():
        return 0.0
    first_loud = int(np.argmax(loud))
    last_loud = int(len(loud) - 1 - np.argmax(loud[::-1]))
    if last_loud <= first_loud + 1:
        return 0.0
    quiet = ~loud[first_loud + 1:last_loud]
    best = cur = 0
    for v in quiet:
        if v:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best * win / sr * 1000.0


def write_wav(path: Path, pcm: bytes, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


def peak_dbfs(pcm: bytes) -> float:
    x = to_float(pcm)
    if len(x) == 0:
        return -120.0
    peak = float(np.max(np.abs(x)))
    return 20 * np.log10(peak + 1e-12)


def expected_range(text: str) -> tuple[float, float]:
    """Plausible duration range: words / 2.2 wps +/- 60%, plus pause-marker ms.

    Pause markers (`<NNN>`) contribute real silence on top of speech, so
    their ms are folded into `base` before the +/-60% band is applied. For a
    line dominated by a marker (e.g. m03's `<2400>` against ~12 words) the
    lower bound stays comfortably below the guaranteed marker silence alone
    (0.4 * 2.4s = 0.96s < 2.4s) and the upper bound is generous (1.6x + 1s),
    so a legitimate long internal pause is never rejected by this check.
    """
    words = len(re.findall(r"[A-Za-z']+", text))
    pause_ms = sum(int(m) for m in re.findall(r"<(\d+)>", text))
    base = words / 2.2 + pause_ms / 1000.0
    return base * 0.4, base * 1.6 + 1.0


def synth_line(vid: str, text: str, speaker: str) -> tuple[bytes, int]:
    """Synthesize, trim, and sanity-check one line; re-synth once on failure."""
    lo, hi = expected_range(text)
    for attempt in range(2):
        raw = synth_with_fallback(text, speaker)[0]
        pcm, sr, nch, width = wav_data_chunk(raw)
        assert sr == RATE and nch == 1 and width == 2, f"{vid}: unexpected wav format sr={sr} nch={nch} width={width}"
        pcm = trim_silence(pcm, sr)
        dur = len(pcm) / (2 * sr)
        peak = peak_dbfs(pcm)
        ok_dur = lo <= dur <= hi
        ok_peak = peak > -20.0
        if ok_dur and ok_peak:
            return pcm, sr
        print(f"  [{vid}] attempt {attempt + 1} failed check: dur={dur:.2f}s (expect {lo:.2f}-{hi:.2f}) peak={peak:.1f}dBFS -- retrying")
    return pcm, sr  # last attempt, report as-is


def main() -> None:
    VO_DIR.mkdir(parents=True, exist_ok=True)
    lines = parse_script(SCRIPT_MD)
    if not lines:
        sys.exit("no `- <id>: \"...\"` lines found in SCRIPT.md")

    manifest: dict[str, dict] = {}
    total = 0.0
    durations: list[tuple[str, float]] = []
    internal_pauses: dict[str, tuple[float, float]] = {}

    for vid, text in lines:
        speaker = speaker_for(vid)
        wav_path = VO_DIR / f"{vid}.wav"
        txt_path = VO_DIR / f"{vid}.txt"
        sidecar = f"{speaker}\n{text}"
        needs_synth = True
        if wav_path.exists() and txt_path.exists():
            if txt_path.read_text(encoding="utf-8") == sidecar:
                needs_synth = False

        if needs_synth:
            print(f"synthesizing {vid} [{speaker}]: {text[:70]!r}...")
            pcm, sr = synth_line(vid, text, speaker)
            write_wav(wav_path, gate_internal_pauses(pcm, sr), sr)
            txt_path.write_text(sidecar, encoding="utf-8")
            time.sleep(0.2)  # be polite to the API
        else:
            print(f"skipping {vid} (unchanged)")

        raw = wav_path.read_bytes()
        pcm, sr, nch, width = wav_data_chunk(raw)
        dur = round(len(pcm) / (width * nch * sr), 3)
        durations.append((vid, dur))
        total += dur
        manifest[vid] = {
            "speaker": speaker,
            "seconds": dur,
            "text": text,
        }

        if vid in INTERNAL_PAUSE_IDS:
            gap_silence_ms = longest_internal_silence_ms(pcm, sr, TRIM_THRESH_DB)
            gap_presence_ms = longest_internal_silence_ms(pcm, sr, INTERNAL_PAUSE_SPEECH_PRESENCE_DB)
            internal_pauses[vid] = (gap_silence_ms, gap_presence_ms)
            print(
                f"  [{vid}] longest internal gap: {gap_silence_ms:.0f} ms digital-silence (<{TRIM_THRESH_DB:.0f} dBFS), "
                f"{gap_presence_ms:.0f} ms speech-presence (<{INTERNAL_PAUSE_SPEECH_PRESENCE_DB:.0f} dBFS)"
            )

    manifest["_total_seconds"] = round(total, 3)
    (VO_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    durations.sort(key=lambda t: -t[1])
    print("\n--- narration durations (longest first) ---")
    for vid, dur in durations:
        print(f"  {vid}: {dur:.2f}s")

    dur_by_id = dict(durations)
    print("\n--- per-scene VO seconds ---")
    for scene, ids in SCENE_LINES.items():
        scene_total = sum(dur_by_id[i] for i in ids)
        print(f"  {scene}: {scene_total:.2f}s ({'+'.join(ids)})")

    print(f"\nTOTAL narration duration: {total:.2f}s (cap 250s)")
    if total > 250:
        print("OVER BUDGET -- longest lines above; SCRIPT.md trim needed (not done here).")

    if internal_pauses:
        print("\n--- measured internal pauses (deliberate <NNN> silences) ---")
        print("    digital-silence = strict -45 dBFS gate; speech-presence = -25 dBFS gate")
        print("    (mid-clause markers render as breath noise, not digital silence -- RIME_EVIDENCE.md claim 2/E3)")
        for vid, (gap_silence_ms, gap_presence_ms) in internal_pauses.items():
            print(f"  {vid}: {gap_silence_ms:.0f} ms digital-silence / {gap_presence_ms:.0f} ms speech-presence")


if __name__ == "__main__":
    main()

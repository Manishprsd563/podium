"""Generate the Podium pitch video music bed (public/music/bed.wav).

Cinematic-tech underscore, 96 BPM, D minor. Pure numpy synthesis (+ stdlib
`wave`/`struct` for I/O), ffmpeg only for the final two-pass loudness pass.

Sections (exact boundaries, per SCRIPT.md § Asset contract):
  0.00s   -  14.00s   sparse intro       (pad + soft sub only)
  14.00s  - 150.00s   pulse @ 96 BPM     (+ kick, hats, arpeggio)
  150.00s - 200.00s   lift               (+ octave pad, shimmer, denser hats)
  200.00s - 250.00s   resolve            (drums drop, pad+arp decay, tail)

Run:
    python tools/make_bed.py
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
MUSIC_DIR = PROJECT / "public" / "music"
RAW_PATH = MUSIC_DIR / "_bed_raw.wav"
OUT_PATH = MUSIC_DIR / "bed.wav"

SR = 48000
DURATION = 250.0
N = int(round(DURATION * SR))
T = np.arange(N, dtype=np.float64) / SR  # time axis, seconds

BPM = 96.0
BEAT = 60.0 / BPM       # 0.625 s
BAR = BEAT * 4.0        # 2.5 s
CHORD_DUR = BAR * 2.0   # 5 s (2 bars)

RNG = np.random.default_rng(20260909)

# --------------------------------------------------------------------------
# Music theory: D minor progression Dm - Bb - F - C, 2 bars each.
# Triads as MIDI note numbers (root, third, fifth).
# --------------------------------------------------------------------------
CHORDS = {
    "Dm": (50, 53, 57),  # D3 F3 A3
    "Bb": (46, 50, 53),  # Bb2 D3 F3
    "F":  (53, 57, 60),  # F3 A3 C4
    "C":  (60, 64, 67),  # C4 E4 G4
}
PROG = ["Dm", "Bb", "F", "C"]


def midi_freq(m: float) -> float:
    return 440.0 * 2.0 ** ((m - 69.0) / 12.0)


def build_segments() -> list[tuple[float, float, str]]:
    """Chord timeline for the whole track, bar-aligned, ending on a long Dm resolve."""
    segs: list[tuple[float, float, str]] = []
    t = 0.0
    i = 0
    while t < 200.0 - 1e-6:
        segs.append((t, t + CHORD_DUR, PROG[i % 4]))
        t += CHORD_DUR
        i += 1
    segs.append((200.0, 205.0, "F"))
    segs.append((205.0, 210.0, "C"))
    segs.append((210.0, 250.0, "Dm"))
    return segs


SEGMENTS = build_segments()


def chord_at(t: float) -> str:
    for (a, b, c) in SEGMENTS:
        if a - 1e-9 <= t < b + 1e-9:
            return c
    return SEGMENTS[-1][2]


def build_freq_track(note_of_chord, glide: float = 0.15) -> np.ndarray:
    """Piecewise-constant frequency track (Hz) over the chord timeline, with a
    linear glide of `glide` seconds centred on every chord boundary so the
    oscillator phase (and hence the audio) never jumps -> no clicks."""
    freq = np.empty(N, dtype=np.float64)
    boundaries: list[tuple[int, float, float]] = []
    prev_f = None
    for i, (t0, t1, chord) in enumerate(SEGMENTS):
        s0 = int(round(t0 * SR))
        s1 = int(round(t1 * SR))
        s1 = min(s1, N)
        if s0 >= N:
            break
        f = midi_freq(note_of_chord(chord))
        if i > 0 and prev_f is not None:
            boundaries.append((s0, prev_f, f))
        freq[s0:s1] = f
        prev_f = f
    glide_n = max(2, int(glide * SR))
    half = glide_n // 2
    for (b, oldf, newf) in boundaries:
        lo = max(0, b - half)
        hi = min(N, b + half)
        if hi > lo:
            freq[lo:hi] = np.linspace(oldf, newf, hi - lo)
    return freq


def osc_from_freq(freq: np.ndarray, detune_cents: float = 0.0,
                   saw_mix: float = 0.25) -> np.ndarray:
    """Triangle/saw blended oscillator, phase-continuous (cumulative sum of a
    smoothly-varying frequency track -> no discontinuities at chord changes)."""
    f = freq * (2.0 ** (detune_cents / 1200.0))
    phase = 2.0 * np.pi * np.cumsum(f) / SR
    tri = (2.0 / np.pi) * np.arcsin(np.sin(phase))
    saw = np.mod(phase / np.pi, 2.0) - 1.0
    sig = (1.0 - saw_mix) * tri + saw_mix * saw
    return sig.astype(np.float32)


# --------------------------------------------------------------------------
# Envelope helpers
# --------------------------------------------------------------------------
def env_interp(knots_t, knots_v) -> np.ndarray:
    return np.interp(T, knots_t, knots_v).astype(np.float32)


def note_env(n: int, attack: int, decay_tau: float) -> np.ndarray:
    """Fast attack, exponential decay. Always >= ~5 ms attack -> no clicks."""
    e = np.empty(n, dtype=np.float32)
    a = max(1, attack)
    e[:a] = np.linspace(0.0, 1.0, a)
    tail = n - a
    if tail > 0:
        idx = np.arange(tail)
        e[a:] = np.exp(-idx / (decay_tau * SR))
    return e


# --------------------------------------------------------------------------
# Time-varying (STFT) low-pass filter: no per-sample IIR recursion needed, so
# it is numerically stable for any sweep speed and fully vectorised per frame.
# --------------------------------------------------------------------------
def stft_variable_lowpass(x: np.ndarray, cutoff_fn, frame: int = 4096,
                           hop: int = 2048) -> np.ndarray:
    n = len(x)
    window = np.hanning(frame).astype(np.float64)
    out = np.zeros(n + frame, dtype=np.float64)
    norm = np.zeros(n + frame, dtype=np.float64)
    freqs = np.fft.rfftfreq(frame, d=1.0 / SR)
    pos = 0
    while pos < n:
        seg = x[pos:pos + frame]
        if len(seg) < frame:
            seg = np.pad(seg, (0, frame - len(seg)))
        spec = np.fft.rfft(seg * window)
        t_center = (pos + frame / 2.0) / SR
        cutoff = cutoff_fn(t_center)
        lo = cutoff * 0.7
        hi = cutoff * 1.35
        mask = np.ones_like(freqs)
        mask[freqs >= hi] = 0.0
        trans = (freqs > lo) & (freqs < hi)
        xr = (freqs[trans] - lo) / (hi - lo)
        mask[trans] = 0.5 * (1.0 + np.cos(np.pi * xr))
        spec *= mask
        seg_out = np.fft.irfft(spec, n=frame)
        out[pos:pos + frame] += seg_out * window
        norm[pos:pos + frame] += window * window
        pos += hop
    norm[norm < 1e-9] = 1.0
    return (out / norm)[:n].astype(np.float32)


def static_bandpass(x: np.ndarray, lo_hz: float, hi_hz: float) -> np.ndarray:
    n = len(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, d=1.0 / SR)
    mask = np.zeros_like(freqs)
    lo_w = lo_hz * 0.7
    hi_w = hi_hz * 1.3
    mask[(freqs >= lo_hz) & (freqs <= hi_hz)] = 1.0
    t_lo = (freqs > lo_w) & (freqs < lo_hz)
    mask[t_lo] = 0.5 * (1 - np.cos(np.pi * (freqs[t_lo] - lo_w) / (lo_hz - lo_w)))
    t_hi = (freqs > hi_hz) & (freqs < hi_w)
    mask[t_hi] = 0.5 * (1 + np.cos(np.pi * (freqs[t_hi] - hi_hz) / (hi_w - hi_hz)))
    spec *= mask
    return np.fft.irfft(spec, n=n).astype(np.float32)


def hat_burst(n: int, cutoff_hi: float = 9000.0, cutoff_lo: float = 5500.0) -> np.ndarray:
    noise = RNG.standard_normal(n).astype(np.float64)
    filt = static_bandpass(noise, cutoff_lo, cutoff_hi)
    env = np.exp(-np.arange(n) / (0.012 * SR)).astype(np.float32)
    return filt * env


# --------------------------------------------------------------------------
# Stereo width helper: short L/R delay (Haas) applied to a mono layer.
# --------------------------------------------------------------------------
def widen(mono: np.ndarray, delay_ms: float = 9.0):
    d = int(SR * delay_ms / 1000.0)
    left = mono
    right = np.concatenate([np.zeros(d, dtype=mono.dtype), mono[:-d] if d > 0 else mono])
    return left, right


def add_at(buf: np.ndarray, start_sample: int, chunk: np.ndarray) -> None:
    n = len(chunk)
    s = start_sample
    e = s + n
    if e <= 0 or s >= len(buf):
        return
    cs = max(0, -s)
    ce = n - max(0, e - len(buf))
    if ce <= cs:
        return
    buf[s + cs:s + ce] += chunk[cs:ce]


# --------------------------------------------------------------------------
# Main synthesis
# --------------------------------------------------------------------------
def synth() -> np.ndarray:
    outL = np.zeros(N, dtype=np.float32)
    outR = np.zeros(N, dtype=np.float32)

    print("building chord frequency tracks...")
    root_freq = build_freq_track(lambda c: CHORDS[c][0], glide=0.15)
    third_freq = build_freq_track(lambda c: CHORDS[c][1], glide=0.15)
    fifth_freq = build_freq_track(lambda c: CHORDS[c][2], glide=0.15)
    octave_freq = build_freq_track(lambda c: CHORDS[c][0] + 12, glide=0.15)
    sub_freq = root_freq / 2.0

    # --- Pad: 4 detuned triangle/saw voices (root, third, fifth, root-dup) ---
    print("synthesizing pad voices...")
    pad = (
        0.85 * osc_from_freq(root_freq, detune_cents=-6, saw_mix=0.22) +
        0.75 * osc_from_freq(third_freq, detune_cents=+4, saw_mix=0.22) +
        0.70 * osc_from_freq(fifth_freq, detune_cents=-3, saw_mix=0.20) +
        0.55 * osc_from_freq(root_freq, detune_cents=+9, saw_mix=0.28)
    )
    # soft "breath" dip right at each chord boundary (extra smoothing safety,
    # well beyond the >=30ms fade requirement)
    breath = np.ones(N, dtype=np.float32)
    for (b, _, _) in [(int(round(t0 * SR)), None, None) for (t0, _, _) in SEGMENTS[1:]]:
        w = int(0.18 * SR)
        lo, hi = max(0, b - w), min(N, b + w)
        if hi > lo:
            local_t = np.linspace(-1, 1, hi - lo)
            breath[lo:hi] *= (1.0 - 0.10 * np.exp(-(local_t ** 2) * 6.0))
    pad *= breath

    # overall pad presence/level shaping across the arrangement
    pad_level = env_interp(
        [0, 2, 14, 150, 200, 210, 244, 250],
        [0.0, 0.55, 0.70, 0.95, 1.00, 0.85, 0.45, 0.05],
    )
    pad *= pad_level

    # octave-up pad, lift section only (150-200s), faded in/out
    print("synthesizing octave-up lift pad...")
    oct_pad = osc_from_freq(octave_freq, detune_cents=+6, saw_mix=0.18) * 0.35
    oct_env = env_interp([0, 148.5, 150.5, 198.5, 200.5, 250], [0, 0, 1, 1, 0, 0])
    oct_pad *= oct_env

    pad_mono = (pad + oct_pad).astype(np.float32)

    # slow low-pass sweep on the pad (section-shaped base + slow LFO)
    print("applying swept low-pass to pad (STFT, may take a little while)...")
    base_knots_t = [0, 14, 100, 150, 200, 210, 250]
    base_knots_v = [480, 650, 1500, 1900, 2100, 1100, 480]

    def pad_cutoff_L(t):
        base = float(np.interp(t, base_knots_t, base_knots_v))
        lfo = 420.0 * np.sin(2 * np.pi * t / 37.0)
        return max(160.0, base + lfo)

    def pad_cutoff_R(t):
        base = float(np.interp(t, base_knots_t, base_knots_v))
        lfo = 420.0 * np.sin(2 * np.pi * t / 37.0 + 0.9)
        return max(160.0, base + lfo)

    padL_raw, padR_raw = widen(pad_mono, delay_ms=9.0)
    padL = stft_variable_lowpass(padL_raw, pad_cutoff_L)
    padR = stft_variable_lowpass(padR_raw, pad_cutoff_R)
    outL += padL
    outR += padR
    del pad, oct_pad, pad_mono, padL_raw, padR_raw, padL, padR

    # --- Sub bass: root/2, sidechain-style pump synced to the kick grid ---
    print("synthesizing sub bass...")
    sub = osc_from_freq(sub_freq, detune_cents=0.0, saw_mix=0.05) * 0.9
    # pure sine-ish sub: blend down the saw/tri edge further via low-pass (static)
    sub = static_bandpass(sub, 28.0, 220.0)
    sub_level = env_interp([0, 6, 8, 200, 215, 250], [0.15, 0.15, 0.55, 0.55, 0.0, 0.0])
    pump_depth = env_interp([0, 12, 14, 198, 200.6], [0.0, 0.0, 0.55, 0.55, 0.0])
    t_mod = np.mod(T - 14.0, BEAT)
    pump_env = 1.0 - pump_depth * np.exp(-t_mod / 0.14).astype(np.float32)
    sub = sub * sub_level * pump_env
    subL, subR = widen(sub, delay_ms=3.0)
    outL += subL
    outR += subR
    del sub, subL, subR

    # --- Kick: pitched sine drop 60->40Hz, ~120ms, on every beat 14-200s ---
    print("synthesizing kick drum...")
    kick_len = int(0.15 * SR)
    sweep_n = int(0.12 * SR)
    kfreq = np.empty(kick_len, dtype=np.float64)
    kfreq[:sweep_n] = np.linspace(60.0, 40.0, sweep_n)
    kfreq[sweep_n:] = 40.0
    kphase = 2 * np.pi * np.cumsum(kfreq) / SR
    kick_wave = np.sin(kphase)
    kick_amp = note_env(kick_len, attack=int(0.003 * SR), decay_tau=0.055)
    kick_template = (kick_wave * kick_amp).astype(np.float32) * 0.95

    kickL = np.zeros(N, dtype=np.float32)
    kickR = np.zeros(N, dtype=np.float32)
    t = 14.0
    while t < 200.0:
        s = int(round(t * SR))
        add_at(kickL, s, kick_template)
        add_at(kickR, s, kick_template)
        t += BEAT
    outL += kickL
    outR += kickR
    del kickL, kickR

    # --- Hi-hats: filtered noise ticks on offbeats from 14s, denser in lift ---
    print("synthesizing hi-hats...")
    hatL = np.zeros(N, dtype=np.float32)
    hatR = np.zeros(N, dtype=np.float32)
    hat_len = int(0.05 * SR)
    t = 14.0
    while t < 200.0:
        off = t + BEAT * 0.5
        if off < 200.0:
            s = int(round(off * SR))
            burst = hat_burst(hat_len) * 0.5
            pan = 0.5 + 0.5 * np.sin(off * 1.7)
            add_at(hatL, s, burst * (1.0 - 0.15 * pan))
            add_at(hatR, s, burst * (1.0 - 0.15 * (1 - pan)))
        t += BEAT
    # extra 16th-note density during the lift section
    t = 150.0
    while t < 200.0:
        for frac in (0.25, 0.75):
            pos = t + BEAT * frac
            if 150.0 <= pos < 200.0:
                s = int(round(pos * SR))
                burst = hat_burst(hat_len, cutoff_hi=11000, cutoff_lo=6500) * 0.28
                add_at(hatL, s, burst)
                add_at(hatR, s, burst)
        t += BEAT
    outL += hatL
    outR += hatR
    del hatL, hatR

    # --- Shimmer: bandpassed noise swells every 2 bars during the lift ---
    print("synthesizing shimmer...")
    lift_start_s = int(150.0 * SR)
    lift_end_s = int(200.0 * SR)
    lift_len = lift_end_s - lift_start_s
    noise = RNG.standard_normal(lift_len).astype(np.float64)
    shimmer_bp = static_bandpass(noise, 3200.0, 9000.0)
    swell_env = np.zeros(lift_len, dtype=np.float32)
    swell_period = CHORD_DUR * 2.0  # every 2 bars = 5s (matches chord length here)
    tt = np.arange(lift_len) / SR
    phase = np.mod(tt, swell_period) / swell_period
    swell_env = (0.5 - 0.5 * np.cos(2 * np.pi * phase)) ** 2
    shimmer = shimmer_bp * swell_env * 0.22
    shimL, shimR = widen(shimmer, delay_ms=14.0)
    add_at(outL, lift_start_s, shimL)
    add_at(outR, lift_start_s, shimR)
    del noise, shimmer_bp, shimmer, shimL, shimR

    # --- Plucked arpeggio: sine+triangle, 1/8 notes over chord tones, from 14s ---
    print("synthesizing arpeggio...")
    arpL = np.zeros(N, dtype=np.float32)
    arpR = np.zeros(N, dtype=np.float32)
    eighth = BEAT / 2.0  # 0.3125 s
    presence = env_interp([0, 14, 14.5, 208, 240, 250], [0, 0, 1, 1, 0, 0])
    pattern = [0, 1, 2, 3, 2, 1]
    note_i = 0
    t = 14.0
    note_len = int(0.30 * SR)
    while t < 244.0:
        chord = chord_at(t)
        root, third, fifth = CHORDS[chord]
        tones = [root + 12, third + 12, fifth + 12, root + 24]
        m = tones[pattern[note_i % len(pattern)]]
        f = midi_freq(m)
        vel = 0.5 * (0.85 + 0.3 * RNG.random())
        s = int(round(t * SR))
        pres = float(np.interp(t, [0, 14, 14.5, 208, 240, 250], [0, 0, 1, 1, 0, 0]))
        if pres > 0.001:
            nn = min(note_len, N - s) if s < N else 0
            if nn > 0:
                idx = np.arange(nn)
                ph = 2 * np.pi * f * idx / SR
                sig = 0.6 * np.sin(ph) + 0.4 * ((2 / np.pi) * np.arcsin(np.sin(ph)))
                env = note_env(nn, attack=int(0.006 * SR), decay_tau=0.09)
                chunk = (sig * env * vel * pres).astype(np.float32)
                pan = 0.5 + 0.35 * np.sin(note_i * 0.9)
                add_at(arpL, s, chunk * (1.0 - 0.3 * pan))
                add_at(arpR, s, chunk * (1.0 - 0.3 * (1 - pan)))
        note_i += 1
        t += eighth
    outL += arpL
    outR += arpR
    del arpL, arpR

    # --- Master shaping + soft-clip limiter ---
    print("mastering...")
    master = env_interp(
        [0, 2, 14, 150, 200, 210, 244, 250],
        [0.0, 0.7, 0.85, 1.0, 1.0, 0.9, 0.55, 0.0],
    )
    outL *= master
    outR *= master
    # final hard fade to true zero over the last 40ms as an absolute safety net
    tail_n = int(0.04 * SR)
    fade = np.linspace(1.0, 0.0, tail_n).astype(np.float32)
    outL[-tail_n:] *= fade
    outR[-tail_n:] *= fade

    drive = 1.35
    outL = np.tanh(outL * drive)
    outR = np.tanh(outR * drive)

    stereo = np.stack([outL, outR], axis=1).astype(np.float32)
    peak = float(np.max(np.abs(stereo)))
    print(f"raw mix peak = {peak:.3f}")
    return stereo


# --------------------------------------------------------------------------
# WAV I/O (stdlib only; `wave` module cannot write IEEE-float, so a minimal
# RIFF/WAVE writer is hand-rolled for the float32 intermediate file).
# --------------------------------------------------------------------------
def write_wav_float32(path: Path, data: np.ndarray, sr: int) -> None:
    data = np.ascontiguousarray(data.astype("<f4"))
    n, ch = data.shape
    bits = 32
    byte_rate = sr * ch * bits // 8
    block_align = ch * bits // 8
    data_bytes = data.tobytes()
    fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, ch, sr, byte_rate, block_align, bits)
    fact_chunk = struct.pack("<4sII", b"fact", 4, n)
    data_header = struct.pack("<4sI", b"data", len(data_bytes))
    riff_size = 4 + len(fmt_chunk) + len(fact_chunk) + len(data_header) + len(data_bytes)
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        f.write(fmt_chunk)
        f.write(fact_chunk)
        f.write(data_header)
        f.write(data_bytes)


def loudnorm_two_pass(src: Path, dst: Path, sr: int = 48000) -> dict:
    cmd1 = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
        "-af", "loudnorm=I=-24:TP=-2:LRA=11:print_format=json",
        "-f", "null", "-",
    ]
    p1 = subprocess.run(cmd1, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p1.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm pass1 failed:\n" + p1.stderr[-3000:])
    stats = json.loads(m.group(0))
    af = (
        f"loudnorm=I=-24:TP=-2:LRA=11:"
        f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:linear=true:print_format=summary"
    )
    cmd2 = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(src),
        "-af", af, "-ar", str(sr), "-ac", "2", "-c:a", "pcm_s16le", str(dst),
    ]
    p2 = subprocess.run(cmd2, capture_output=True, text=True)
    if p2.returncode != 0:
        raise RuntimeError("loudnorm pass2 failed:\n" + p2.stderr[-3000:])
    return stats


def rms_table(path: Path, block_s: float = 10.0) -> str:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    x = x.reshape(-1, ch).mean(axis=1)
    block = int(block_s * sr)
    lines = [f"{'t(s)':>8}  {'RMS dBFS':>9}"]
    for i in range(0, len(x), block):
        seg = x[i:i + block]
        if len(seg) == 0:
            continue
        rms = np.sqrt(np.mean(seg ** 2) + 1e-12)
        db = 20 * np.log10(rms + 1e-12)
        lines.append(f"{i / sr:8.1f}  {db:9.1f}")
    return "\n".join(lines)


def main() -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    stereo = synth()
    print(f"writing raw float wav -> {RAW_PATH}")
    write_wav_float32(RAW_PATH, stereo, SR)

    print("running ffmpeg two-pass loudnorm...")
    stats = loudnorm_two_pass(RAW_PATH, OUT_PATH, SR)
    print("loudnorm measured:", json.dumps(stats, indent=2))

    with wave.open(str(OUT_PATH), "rb") as w:
        dur = w.getnframes() / w.getframerate()
        print(f"final bed.wav: {dur:.3f}s, {w.getframerate()}Hz, {w.getnchannels()}ch, "
              f"{w.getsampwidth() * 8}bit")

    print("\nRMS per 10s block:")
    print(rms_table(OUT_PATH))

    RAW_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

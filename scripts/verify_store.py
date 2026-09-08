"""Throwaway acceptance check for agent/store.py — not part of the shipped app.

Verifies: Store produces a valid session.json and a timeline.jsonl whose event
names match CONTRACTS §4, and WavWriter writes a playable 24 kHz mono WAV from
synthetic frames (including a resampled 16 kHz frame).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.store import Store, WavWriter  # noqa: E402

CONTRACT_EVENTS = {
    "phase", "revision", "slide", "user_speech_start", "user_speech_end",
    "user_transcript", "agent_speech_start", "agent_speech_end", "interrupt",
    "tool_start", "tool_end", "stale_dropped", "feedback_heard", "provider",
}


@dataclasses.dataclass
class FakeFrame:
    data: bytes
    sample_rate: int
    num_channels: int = 1


def main() -> None:
    test_root = Path("sessions_test_tmp")
    if test_root.exists():
        shutil.rmtree(test_root)

    store = Store(session_id="s_verify_test", root=test_root)
    store.set_config(rime_model="mistv3", rime_speaker="astra", sample_rate=24000)
    store.set_deck({"source": "generated", "topic": "self-attention", "budget_s": 60, "slides": []})

    store.log("provider", name="rime", model="mistv3", speaker="astra")
    store.log("phase", name="setup")
    rev = store.new_revision("full")
    store.log("phase", name="present")
    store.log("slide", slide=1)
    store.log("user_speech_start")
    store.log("user_transcript", text="so today", final=False)
    store.log("user_speech_end")
    store.log("agent_speech_start", speech_id="sp1", text="thirty seconds", kind="cue")
    store.log("agent_speech_end", speech_id="sp1", interrupted=False, played_s=1.2)
    store.log("interrupt", speech_id="sp2", latency_ms=180)
    store.log("tool_start", tool="judge", revision=rev["revision"], ms=0)
    store.log("tool_end", tool="judge", revision=rev["revision"], ms=3190)
    store.log("stale_dropped", tool="render", revision=rev["revision"], current_revision=store.current_revision)
    store.log("feedback_heard", improvement_id="imp_1")

    store.update_revision(rev["revision"], ended_at="2026-09-07T18:31:12Z",
                           transcript={"text": "so today", "words": [{"w": "so", "start": 0.0, "end": 0.2, "conf": 0.98}]})

    # -- session.json checks --
    session = json.loads((test_root / "s_verify_test" / "session.json").read_text())
    assert session["session_id"] == "s_verify_test"
    assert session["deck"]["topic"] == "self-attention"
    assert session["revisions"][0]["revision"] == 1
    assert session["revisions"][0]["transcript"]["words"][0]["w"] == "so"
    print("session.json: OK ->", json.dumps(session, indent=2)[:300], "...")

    # -- timeline.jsonl checks --
    lines = (test_root / "s_verify_test" / "timeline.jsonl").read_text().strip().splitlines()
    seen_events = set()
    for line in lines:
        obj = json.loads(line)
        assert "t" in obj and isinstance(obj["t"], float)
        assert "ev" in obj
        seen_events.add(obj["ev"])
    unknown = seen_events - CONTRACT_EVENTS
    assert not unknown, f"events not in CONTRACTS §4: {unknown}"
    print(f"timeline.jsonl: OK -> {len(lines)} lines, events used: {sorted(seen_events)}")

    # revision fence
    store.new_revision({"slide": 2})
    store.supersede(1)
    assert store.current_revision == 2
    assert store.get_revision(1)["superseded"] is True
    print("revision fence: OK -> current_revision =", store.current_revision, "rev1.superseded =", store.get_revision(1)["superseded"])

    # -- WavWriter checks --
    wav_path = store.wav_path(1)
    writer = WavWriter(wav_path, sample_rate=24000)
    # 0.5s of a 440Hz tone at native 24kHz
    t = np.linspace(0, 0.5, int(24000 * 0.5), endpoint=False)
    tone_24k = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    writer.write_frame(FakeFrame(data=tone_24k.tobytes(), sample_rate=24000, num_channels=1))
    # 0.3s of a 220Hz tone at 16kHz -> exercises the resampling path
    t16 = np.linspace(0, 0.3, int(16000 * 0.3), endpoint=False)
    tone_16k = (np.sin(2 * np.pi * 220 * t16) * 6000).astype(np.int16)
    writer.write_frame(FakeFrame(data=tone_16k.tobytes(), sample_rate=16000, num_channels=1))
    # stereo frame at 24kHz -> exercises the downmix path
    stereo = np.column_stack([tone_24k[:1000], -tone_24k[:1000]]).astype(np.int16)
    writer.write_frame(FakeFrame(data=stereo.tobytes(), sample_rate=24000, num_channels=2))

    expected_duration = writer.duration_s
    out_path = writer.finalize()

    with wave.open(str(out_path), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        n_frames = w.getnframes()
        pcm = w.readframes(n_frames)
        samples = np.frombuffer(pcm, dtype=np.int16)
        assert samples.size == n_frames
        assert np.abs(samples).max() > 1000, "WAV looks silent"
    actual_duration = n_frames / 24000
    print(f"WavWriter: OK -> {out_path}, {n_frames} frames @ 24kHz = {actual_duration:.3f}s "
          f"(writer.duration_s reported {expected_duration:.3f}s), peak amplitude {int(np.abs(samples).max())}")

    shutil.rmtree(test_root)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()

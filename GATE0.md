# Gate 0 — measured results and the configuration Podium ships

Run on 2026-09-07 from Windows 11 / Python 3.12.10 / `livekit-agents 1.8.0`.
Every number below came from the scripts in this repo, not from documentation.
Reproduce with: `.venv/Scripts/python.exe scripts/rime_probe.py`,
`scripts/stt_llm_probe.py`, `scripts/inference_probe.py`,
`scripts/judge_json_probe.py`, `evidence/e1_pause_control.py`.

## Decision summary

| Component | Shipped choice | Why (measured) |
|---|---|---|
| TTS | Rime **`mistv3`**, speaker `astra`, `lang=eng`, 24 kHz, WebSocket `/ws3`, `pause_between_brackets=true` | Only Mist v3 honours pause markup: 3/3 fixtures within −100…+30 ms of a requested 1500 ms. Coda: 0/3. Markup never spoken aloud on either. |
| STT | Deepgram **`nova-3`**, `filler_words=true`, `punctuate=true` | Returns "um"/"uh" with word timestamps and 0.998 confidence on a Rime-rendered filler clip; Whisper-class STT would strip them and blind the core metric. |
| Coach LLM | LiveKit Inference **`google/gemma-4-31b-it`** | 0.67 s to first token, 0.95 s total on a coach-shaped turn. |
| Judge LLM | LiveKit Inference **`openai/gpt-5.4-mini`** | 3/3 valid scorecard JSON, avg 3.19 s, max 3.59 s. |
| Transport | LiveKit Cloud, region **India South** | Worker registered against `wss://your-project.livekit.cloud` (redacted; see `.env.example`); no self-hosting needed. |

## E1 — pause markup is real, measured, and never spoken

Same model, same speaker, same words; only `<900>` and `<600>` markup differs.
`evidence/e1_pause_control.py` → `evidence/e1/results.json` + 12 WAV clips.

| Model | Fixture | A (plain) | B (marked) | Added | Error vs 1500 ms | Markup spoken? |
|---|---|---|---|---|---|---|
| mistv3 | attention | 5.32 s | 6.72 s | 1400 ms | −100 ms | no |
| mistv3 | tradeoff | 5.61 s | 7.14 s | 1530 ms | +30 ms | no |
| mistv3 | closing | 4.54 s | 6.03 s | 1490 ms | −10 ms | no |
| coda | attention | 5.20 s | 5.36 s | 160 ms | −1340 ms | no |
| coda | tradeoff | 6.56 s | 6.80 s | 240 ms | −1260 ms | no |
| coda | closing | 4.56 s | 6.80 s | 2240 ms | +740 ms | no |

Mist v3 honoured 3/3 within ±450 ms tolerance; Coda 0/3 — its variation is
generation noise, not markup. This makes the V2-vs-V3 delivery contrast (the
product's core coaching mechanism) measurable rather than asserted.

Limitations: 3 fixtures, one speaker, one sampling rate, silence threshold
−40 dBFS over 10 ms frames, measured on REST WAV renders rather than the live
WebSocket path. The listening comparison is still to be run.

## Other measurements taken

- **Rime first-byte latency (REST, India → US West):** 872–999 ms across 15 renders. `/ws3` first audio chunk: **0.61 s (mistv3)**, 0.71 s (coda) — the WebSocket path is the one to ship.
- **`/ws3` word timestamps:** present for both models, per-word start/end in seconds, and they include the filler tokens ("um" at 0.354 s, "uh" at 3.009 s on mistv3) — usable to align "what the listener actually heard".
- **`timeScaleFactor=1.3`:** coda 4.32 s → 5.30 s (1.23×); mistv3 4.57 s → 7.61 s (1.67×). Mist v3 over-slows; the "say it slower" feature must be calibrated against measured duration, not the nominal factor.
- **Deepgram round trip** on a 6.7 s clip: 2.0–2.3 s prerecorded; transcript matched the rendered text including both fillers.

## Rejected: the opencode-go LLM gateway

`https://opencode.ai/zen/go/v1` needs a Cloudflare-friendly `User-Agent` plus an
`x-opencode-session` header (otherwise `403 error code: 1010`, then
`400 MissingSessionID`). Even once reachable it measured
`glm-5.3-flash` 6.73 s to first token, 12.74 s for the judge call, and returned
empty content or invalid JSON on 3 of 6 attempts across candidate models.
LiveKit Inference needs no extra credential and was 10× faster, so Podium uses
it for both LLM roles and the gateway is not part of the shipped path.

## Open items

- Choose the coach speaker by listening to `evidence/probe/*.wav` (`astra` vs `cove`); `astra` is the default until then.
- Calibrate `timeScaleFactor` for the "slower" control against measured duration.
- Organizer preflight script has not been run — still needed before submission.

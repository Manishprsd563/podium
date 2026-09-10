# Podium — DataForge x Rime hackathon submission

Podium is a voice-native presentation coach: you rehearse a timed talk against slides out
loud; it listens, measures your delivery by code, and coaches you back **by contrast** — your
own sentence, then a cleaned version, then the cleaned version with a deliberate Rime pause,
all in the same Rime voice (`mistv3` / `thunder`).

```
submission/
  SUBMISSION.md   <- this file
  PREFLIGHT.txt   <- output of scripts/preflight_check.py run against podium/ (exit 0 = clean)
  podium/         <- the full inspectable source tree (secrets, recordings, virtualenv stripped)
  demo/           <- the demo video
```

## Where each organizer requirement lives

| Requirement | Location |
|---|---|
| Demo (≤ 4–5 min: user + problem S01–S02, end-to-end flow S02–S05, hard voice problem S06, technical showcase S07, stress/failure case S08, result S10, active provider S07 + S11) | {{DEMO}} — 4:18, a directed pitch around the real Podium UI: the actual Three.js orb in its live states, muted captures of the real product inside browser frames, three Rime voices; real numbers are cited on screen, in-story numbers are labelled "Maya · session 1 / 2" (see `podium/README.md` § Demo video) |
| Working code | `podium/` — run instructions in `podium/README.md` § Running it on Windows |
| README: setup, architecture, third-party services, limitations, failure behaviour, exact Rime model / speaker / language / endpoint / audio format / transport | `podium/README.md` (§ Third-party services and exact configuration) |
| Evidence: hard voice claim, acceptance test, procedure, result, limitations, repeatable command | `podium/RIME_EVIDENCE.md`; rerun with `.venv\Scripts\python.exe evidence\run_all.py` |
| Configuration hygiene: env example with placeholders only; secret / Rime-config preflight | `podium/.env.example`; `podium/scripts/preflight_check.py` (saved output: `PREFLIGHT.txt`) |

## Rime configuration in one line

Model `mistv3` · speaker `thunder` · `lang=eng` · live speech over WebSocket
`wss://users-ws.rime.ai/ws3` (PCM, 24 kHz, `pause_between_brackets=true`) · pre-synthesis and
contrastive clips over REST `POST https://users.rime.ai/v1/rime-tts` (`audio/wav`, 24 kHz) ·
transport LiveKit Cloud (India South) · STT Deepgram `nova-3`.

## Notes for judges

- The organizer-provided preflight script was not available to us; `scripts/preflight_check.py`
  is the local stand-in (placeholders-only `.env.example`, secret scan, shipped Rime config,
  optional `--online` speaker check against Rime's catalog).
- `RIME_EVIDENCE.md` reports one target as **missed** (barge-in stop latency P95 1.4 s vs a
  0.3 s target). It is left in deliberately; the evidence suite exits non-zero on it.
- All three voices in the demo (narrator `astra`, presenter `willow`, coach `thunder`) are Rime `mistv3`; the coach voice is the shipped product voice.

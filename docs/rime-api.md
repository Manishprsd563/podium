# Rime TTS API — Verified Facts (researched 2026-09-07)

All facts fetched live from official primary sources: docs.rime.ai (official docs markdown exports) and the public live catalog JSON at users.rime.ai. Every claim carries its URL. Anything not directly confirmed is marked **[UNVERIFIED]** with what was tried.

Hosts (https://docs.rime.ai/docs/api-cheat-sheet):
- REST: `https://users.rime.ai` (NOT `api.rime.ai` — returns 404 for TTS)
- WebSocket: `wss://users-ws.rime.ai`
- Text normalization: `https://optimize.rime.ai`
- No npm/PyPI SDK published; similarly named registry packages are unrelated third parties.

## 1. Model IDs
Source: https://docs.rime.ai/docs/models

| Model | `modelId` string | Released | Recommended for |
|---|---|---|---|
| Coda | `coda` | May 2026 | **Default for most new apps.** Flagship; highest voice-quality scores in Rime's human evals; sub-100ms model latency (on-prem GPU engine); 9 languages; word timestamps |
| Mist v3 | `mistv3` | Mar 2026 | Lowest time-to-first-audio (~37ms P50 in Rime's benchmark); custom pauses. **No inline pronunciation control** |
| Mist v2 | `mistv2` | Feb 2025 | Inline pronunciation control (`phonemizeBetweenBrackets`) — Mist v2/v1 only; also `noTextNormalization` |
| Mist v1 (legacy) | `mist` | Apr 2023 | Legacy |
| Arcana (legacy) | `arcana` | — | Not in the current models doc, but appears as a key in the live catalog JSON with a large English voice list |

Key behaviors (https://docs.rime.ai/docs/models):
- "Requests that omit `modelId`, or send a value the API does not recognize, are served by **Mist v3**. Set `modelId` explicitly on every request; Coda is never served by default."
- Mist v1 deprecated.

English language support (https://docs.rime.ai/docs/voices#languages, as of Aug 2026):
- Coda: English ✅ (162 of its 253 voices are English)
- Mist v3: English ✅ (plus fr, de, es only)
- Mist v2: English ✅ (plus fr, de, es)

Snippet (https://docs.rime.ai/docs/voices):
```json
{ "speaker": "astra", "text": "Hello from Rime!", "modelId": "coda", "lang": "en" }
```

## 2. Live catalog endpoint
Sources: https://docs.rime.ai/docs/api-cheat-sheet, https://docs.rime.ai/api-reference/data/voices-v2, https://docs.rime.ai/api-reference/data/voice-details

- Voice names by model+language: `GET https://users.rime.ai/data/voices/all-v2.json`
- Full metadata (gender, age, dialect, flagship flag…): `GET https://users.rime.ai/data/voices/voice_details.json`
- **Both are public — no API key required** (cheat sheet: "Both voice endpoints are public; no API key required.").

Verified live on 2026-09-07 (fetched all-v2.json). Concrete current English speakers:
- **Coda** (`coda.eng`): `astra`, `luna`, `masonry`, `celeste`, `albion`, plus `lyra`, `clementine`, `eyre`, `bancroft`, `beatty`, `godfrey`, `vespera`, `parapet`, `cupola`, `lawton`, …
- **Mist v3** (`mistv3.eng`): `astra`, `luna`, `cove`, `peak`, `pearl`, plus `river`, `ember`, `willow`, …
- Docs' starter table (https://docs.rime.ai/docs/voices): `astra` (Coda+Mistv3), `luna` (Coda+Mistv3), `celeste` (Coda), `masonry` (Coda), `albion` (Coda), `cove` (Mist v3).
- Live catalog also contains legacy keys `mist` and `arcana` (`arcana.eng` includes `astra`, `bond`, `albion`, …).

## 3. REST synthesis endpoint
Sources: https://docs.rime.ai/docs/api-cheat-sheet, https://docs.rime.ai/api-reference/coda/http, https://docs.rime.ai/api-reference/mistv3/http

`POST https://users.rime.ai/v1/rime-tts`

Headers:
- `Authorization: Bearer YOUR_API_KEY`
- `Content-Type: application/json`
- `Accept:` controls output — `audio/mpeg` (MP3), `audio/wav` (WAV), `audio/L16` (headerless PCM), `audio/PCMU` (G.711 μ-law), `audio/webm;codecs=opus`, `audio/ogg;codecs=opus`. Deprecated aliases still accepted: `audio/mp3`, `audio/pcm`, `audio/x-mulaw` (https://docs.rime.ai/api-reference/coda/http).

JSON body (Coda/Mist v3 HTTP reference):
```json
{
  "text": "Hello from Rime!",
  "speaker": "astra",
  "modelId": "coda",
  "lang": "en",
  "samplingRate": 24000,
  "timeScaleFactor": 1.0
}
```
- `lang` default `en`; BCP-47 (en, es, fr, pt, de, ja, ar, hi, it); legacy ISO 639-2 (eng, spa, fra, por, ger, jpn, ara, hin, ita) remain accepted.
- Mist v3 additionally: `pauseBetweenBrackets` (bool), `inlineSpeedAlpha` (string). Mist v2 additionally: `speedAlpha`, `phonemizeBetweenBrackets`, `noTextNormalization`, `audioFormat`, `samplingRate` (4000–44100, default 22050).

cURL:
```bash
curl -X POST https://users.rime.ai/v1/rime-tts \
  -H 'Authorization: Bearer $RIME_API_KEY' \
  -H 'Content-Type: application/json' \
  -H 'Accept: audio/mpeg' \
  --output hello.mp3 \
  -d '{"text": "Hello from Rime.", "speaker": "astra", "modelId": "coda", "lang": "en"}'
```

**Word timestamps over REST: NOT available.** https://docs.rime.ai/docs/streaming transport table: HTTP streaming — Word-level timestamps ❌; WebSocket (JSON) ✅. The `Accept: application/json` non-streaming JSON envelope (base64 audio) exists for **Mist v2 / Mist v1 only** (https://docs.rime.ai/api-reference/mistv2/json-mp3; errors page: "Only Mist v1 and Mist v2 have a JSON representation; every other model returns audio only"). A `wordTimestamps` field inside that REST JSON response is **[UNVERIFIED]** — not shown on the json-mp3/json-wav reference pages I read; use WebSocket `/ws3` for word timestamps.

SSE: `Accept: text/event-stream` on the same endpoint — **Mist v2 only** (https://docs.rime.ai/docs/streaming).

## 4. WebSocket endpoints
Source: https://docs.rime.ai/docs/websockets

| | `/ws3` (recommended) | `/ws2` (legacy) | `/ws` (binary) |
|---|---|---|---|
| URL | `wss://users-ws.rime.ai/ws3` | `wss://users-ws.rime.ai/ws2` | `wss://users-ws.rime.ai/ws` |
| Format | JSON | JSON | Raw binary audio |
| Coda | ✅ | ❌ | ✅ |
| Mist v3 | ✅ | ❌ | ✅ |
| Mist v2/v1 | ✅ | ✅ | ✅ |
| Word timestamps | ✅ | ✅ | ❌ |
| Context IDs | ✅ | ✅ | ❌ |

Auth: `Authorization: Bearer YOUR_API_KEY` as a **connection header** (browser `WebSocket` cannot set headers → needs server-side bridge/proxy). All synthesis args in the **query string**: `?speaker=astra&modelId=coda&audioFormat=mp3&lang=en&samplingRate=24000&segment=bySentence`.

Send (JSON endpoints):
```json
{ "text": "Hello.", "contextId": "turn-001" }
{ "operation": "flush" }
{ "operation": "clear" }
{ "operation": "eos" }
```
- `flush`: synthesize buffer now (use with segment=never). `clear`: discard buffered text on interruption (does NOT cancel in-flight synthesis; stop playback client-side, close connection for hard stop). `eos`: synthesize remainder, emit done, close.
- On `/ws` (binary): send plain text and tokens `<FLUSH>` / `<EOS>` instead (https://docs.rime.ai/docs/websockets-segment).

Receive (`/ws3`):
```typescript
{ type: "chunk", data: "<base64 audio>", contextId: string|null }
{ type: "timestamps", word_timestamps: { words: string[], start: number[], end: number[] }, contextId: string|null }
{ type: "done", contextId: string|null }        // /ws2 additionally carries done: true
{ type: "error", message: string }              // server keeps the connection open
```
- Timestamps: seconds, index-aligned arrays, from start of current synthesis.
- **Timestamps only when `lang` is `en`/`eng` or `es`/`spa` (or omitted)** — other languages get chunk/done with no timestamps and no error (https://docs.rime.ai/api-reference/coda/websockets-json).

`segment` query param (https://docs.rime.ai/docs/websockets-segment): `bySentence` (default; heuristic — unsafe with "Dr.", "2.5ml"), `immediate`, `never` (recommended for production agents; requires explicit flush). Available on all three WS endpoints; legacy `immediate=true` query equivalent.

**contextId semantics** (https://docs.rime.ai/docs/websockets): Rime does not maintain multiple simultaneous context IDs — the chunk event carries the most recent contextId at the time the audio was requested; a contextId persists across subsequent messages that don't provide one.

Buffering: JSON WebSockets buffer input up to `.`, `?`, `!` before first synthesis.

## 5. Custom pauses, spell(), phonemes
Sources: https://docs.rime.ai/docs/custom-pauses, https://docs.rime.ai/docs/spell, https://docs.rime.ai/docs/custom-pronunciation

**Custom pauses** — syntax `<750>` (milliseconds inside angle brackets). Supported on **Mist, Mist v2, Mist v3** (NOT Coda). Requires `pauseBetweenBrackets: true`. No documented max duration; the number is the pause in ms. Example:
```json
{ "text": "wait. <750> are you actually serious.", "speaker": "cove", "modelId": "mistv3", "pauseBetweenBrackets": true }
```
Coda: **no SSML, no `<break>`, no inline tags** (https://docs.rime.ai/docs/prompting: "Coda does not accept SSML. Do not use <break>, <emotion>, or other inline tags. The only supported inline function is spell()."). Punctuation is the only prosody tool on Coda.

**`spell()`** — Mist-family feature (Coda passes it through unprocessed). `spell(ABC123XYZ)`, `spell(rf543dc2)`, `spell(help@rime.ai)`; reads symbols `@ _ - .`; on Mist v3 groups in threes/pairs with naturalistic pauses. Not for standard phone numbers or real uppercase words.

**Inline phonemes** — `{k1Ast0xm}` in Rime's phonetic alphabet (IPA-inspired; https://docs.rime.ai/platform/rime-phonetic-alphabet), requires `phonemizeBetweenBrackets: true`. Supported on **Mist v1, Mist v2, and English Mist v3** per https://docs.rime.ai/docs/custom-pronunciation — but https://docs.rime.ai/docs/models says phonemizeBetweenBrackets works on Mist v2/v1 only and is "not supported on Mist v3 or Coda". The two official pages conflict on English Mist v3 — treat as **[UNVERIFIED/disputed]**; if inline phonemes are load-bearing, use `mistv2`. Helper endpoints: `POST https://users.rime.ai/oov` (dictionary coverage) and `/phonemize` (audio → phonetic string), auth required.

## 6. Playback speed
Sources: https://docs.rime.ai/docs/speed, https://docs.rime.ai/api-reference/coda/http

| Scope | Models | Parameter | Faster | Slower |
|---|---|---|---|---|
| Whole response | Coda, Mist v3 | `timeScaleFactor` | below 1.0 | above 1.0 |
| Whole response (compat) | Coda, Mist v3 | `speedAlpha` | above 1.0 | below 1.0 |
| Whole response | Mist v2 | `speedAlpha` | **below 1.0** | **above 1.0** (legacy, inverted) |
| Per-word `[word]` | Mist v2, Mist v3 | `inlineSpeedAlpha` (string, comma list) | below 1.0 | above 1.0 |

- `timeScaleFactor` range **0.4–2.5**, default 1.0; out-of-range values are **clamped without an error** (https://docs.rime.ai/api-reference/coda/http).
- `timeScaleFactor` works over HTTP and as a query param on /ws, /ws2, /ws3.
- `inlineSpeedAlpha` is Mist-family only; Coda does not support it. On Mist v3, `speedAlpha` and `inlineSpeedAlpha` go in **opposite directions**.
- `speedAlpha` numeric range for Mist v2: **[UNVERIFIED]** — docs give direction and default 1.0 but no min/max.

## 7. Regional endpoints
Source: https://docs.rime.ai/docs/regional-endpoints

HTTP:
| Endpoint | Region |
|---|---|
| `https://users.rime.ai` (default alias for users-west) | US West (us-west-2) |
| `https://users-west.rime.ai` | US West (us-west-2) |
| `https://users-east.rime.ai` | US East (us-east-1) |

WebSocket (pattern applies to /ws, /ws2, /ws3):
| Endpoint | Region |
|---|---|
| `wss://users-ws.rime.ai/ws3` | US West (us-west-2) |
| `wss://users-east-ws.rime.ai/ws3` | US East (us-east-1) |

- Only two regions (US West / US East). Same-region RTT typically 1–10 ms; coast-to-coast ~60 ms floor; >90 ms between US metros = bad route.
- **India recommendation: [UNVERIFIED]** — no guidance for India/ap-south found. Tried: grep for `India|ap-south|Asia` across https://docs.rime.ai/docs/introduction.md, /docs/regional-endpoints.md, /docs/latency.md, /docs/changelog.md — no matches. Only published rule: pick the region closest to your application server.

## 8. Prompting / "writing for the ear"
Source: https://docs.rime.ai/docs/prompting (focuses on Coda; grammar normalizer + spell() processing are Mist-family)

Key concrete rules:
- **No SSML.** No `<break>`, `<emotion>`, any inline markup — read literally. Only inline directive: `spell(...)`.
- **Put disfluencies in the text**: write "um", "uh", "so", "yeah", "well" where a person would genuinely hesitate; sprinkle, never stack (two "um"s in a row sounds like a bug).
- **Punctuation = prosody**: comma = short pause + slight rise; period = falling pitch; `?` = rising intonation; `...` = hesitant/trailing pause (sparingly); `!` = excitement (only when warranted); semicolon between comma and period.
- **Sentence length**: under 25 words (ideally under 15); long sentences without commas sound breathless.
- **Show, don't tell**: replace adjectives ("friendly") with observable speech patterns ("starts sentences with 'yeah'"); give imitable examples. Example pair: Bad "I can certainly assist you with that inquiry." → Good "Yeah, I can help with that. One sec."
- **Normalize cleanly**: pass natively-handled formats through unchanged ($124.50, 04/21/2026, 7:05 PM, (213) 555-9274, 5kg, 95%); pre-expand only listed patterns (04/21→"April 21st"; 07/2025; 3pm→"3:00pm"; 1990s; Q1 2025; €900K→"900 thousand euros"; 10,000,000→"10M"). Verify with `POST https://optimize.rime.ai/textnorm`.
- **spell() for IDs**: confirmation codes, SKUs, vanity phone letters; NOT for standard phone numbers or real uppercase words; avoid dashes in numeric IDs (unnatural pauses) — use spaces or spell().
- **Drop-in system prompt**: full text on the same page under "Drop-in system prompt" (Parts 1–4: sound like a person / normalize cleanly / spell() for IDs / invariants — apply rules silently, never invent/drop/reorder information).

## 9. Audio formats and sample rates
Sources: https://docs.rime.ai/api-reference/coda/http, https://docs.rime.ai/docs/api-cheat-sheet, https://docs.rime.ai/docs/streaming

HTTP (`Accept`): `audio/mpeg` (MP3), `audio/wav` (16-bit LE PCM RIFF), `audio/L16` (headerless PCM), `audio/PCMU` (μ-law), `audio/webm;codecs=opus`, `audio/ogg;codecs=opus`. Deprecated aliases accepted: `audio/mp3`, `audio/pcm`, `audio/x-mulaw`.

WebSocket (`audioFormat` query): `wav`, `mp3`/`mpeg`, `ogg`, `webm`, `pcm`/`l16`, `mulaw`/`pcmu`. Unrecognized falls back to `wav` (Coda ws3 ref). `/ws2` (Mist v2): `mp3`, `mulaw`, `pcm` only.

`samplingRate`: default **24000** (Coda/Mist v3 HTTP + ws3); cheat-sheet range `8000`–`96000` ("Values above 24000 are upsampling"); common: 8000, 16000, 22050, 24000, 44100. Mist v2: 4000–44100, **default 22050**.

Telephony: μ-law native — `Accept: audio/PCMU` / `audioFormat=mulaw` + `samplingRate: 8000`, no transcoding step (https://docs.rime.ai/docs/streaming).

## 10. Rate limits / max text length / contextId
Sources: https://docs.rime.ai/docs/errors, https://docs.rime.ai/docs/api-cheat-sheet

- **Max text: 1,000 characters per request** — HTTP `400` `text is too long`; same limit in dashboard UI and per-endpoint references; split at sentence boundaries. After WS connect, same violation = close code `1011` with reason usually starting `400: text is too long`.
- **Rate limits: no published numbers.** `429` "Currently at websocket limit" at the WS upgrade — back off, retry the upgrade after a delay, reuse connections rather than opening per utterance. Connection limit and synthesis concurrency both unpublished; limits apply per account (team members share).
- Billing is **per character synthesized**; retries billed again — for long utterances, synthesize in sentence-aligned chunks and resume from the last chunk received.
- `contextId`: optional string on each text message; echoed on chunk/timestamps/done; only the most recent is active at request time; persists until replaced. Useful for correlating audio to turns and mapping interruption playback position.
- Other errors worth knowing: 401 variants include `invalid subscription` (billing issue masquerading as auth); 406 when `Accept` names a type the model can't produce (JSON representation = Mist v1/v2 only); retry only 500/502; no response carries a request ID (log UTC timestamps).

## Bonus: official LiveKit integration page
https://docs.rime.ai/docs/livekit and https://docs.rime.ai/docs/quickstart-livekit — Rime maintains official LiveKit Agents plugin docs (plugin setup + streaming configuration). The Python plugin package/parameter names should be confirmed from github.com/livekit/agents (rime plugin source) by the LiveKit scout.
# Podium — frozen cross-slice contracts

Every slice codes against this file. Do not change a shape here without saying so
in `hub`; siblings are compiling against it concurrently.

## 1. Session directory

```
sessions/<session_id>/
  session.json        # everything below, written incrementally
  presentation.wav    # 24 kHz mono s16le, the user's speech (per revision: presentation_r<N>.wav)
  timeline.jsonl      # one JSON object per line, append-only
  clips/
    <improvement_id>_v1_asdelivered.wav
    <improvement_id>_v2_cleaned.wav
    <improvement_id>_v3_paced.wav
```

`session.json`:

```jsonc
{
  "session_id": "s_20260907_183012",
  "created_at": "2026-09-07T18:30:12Z",
  "config": { "rime_model": "mistv3", "rime_speaker": "astra", "rime_lang": "eng",
              "sample_rate": 24000, "transport": "websocket /ws3",
              "stt": "deepgram/nova-3", "coach_llm": "google/gemma-4-31b-it",
              "judge_llm": "openai/gpt-5.4-mini", "provider": "rime" },
  "deck": {
    "source": "generated" | "upload",
    "topic": "self-attention",
    "budget_s": 60,
    "slides": [
      { "index": 1, "title": "Self-attention",
        "bullets": ["every token attends to every other token", "O(n^2) cost"],
        "terms": ["self-attention", "quadratic"], "notes": "" }
    ]
  },
  "revisions": [
    { "revision": 1, "scope": "full" | {"slide": 2},
      "audio": "presentation_r1.wav",
      "started_at": "...", "ended_at": "...",
      "transcript": { "text": "...",
                      "words": [{"w":"so","start":0.31,"end":0.44,"conf":0.99}] },
      "slide_events": [{"slide":1,"at_s":0.0},{"slide":2,"at_s":41.2}],
      "metrics": { /* §2 */ },
      "judgment": { /* §3 */ },
      "superseded": false }
  ]
}
```

## 2. Metrics (computed by code, never by the LLM)

```jsonc
{
  "duration_s": 62.4,
  "words": 184,
  "wpm": 177.0,
  "wpm_by_slide": [{"slide":1,"wpm":181.2,"seconds":41.2}],
  "fillers": { "count": 6, "per_min": 5.8,
               "items": [{"w":"um","start":3.2},{"w":"uh","start":11.9}] },
  "pauses": { "count": 9, "longest_s": 3.4, "mean_s": 0.8,
              "dead_air": [{"start":21.0,"dur":3.4}],   // >= 2.0 s
              "rhetorical": [{"start":8.1,"dur":0.6}] },  // 0.35-1.2 s
  "loudness": { "mean_dbfs": -24.1, "variance_db": 5.2 },
  "time_budget": { "budget_s": 60, "used_s": 62.4, "over_s": 2.4,
                   "per_slide": [{"slide":1,"used_s":41.2,"fair_share_s":30.0}] },
  "low_confidence_terms": [{"term":"Dijkstra","heard":"dexter","conf":0.42,"start":33.1}]
}
```

Definitions frozen so numbers mean one thing everywhere:
- **filler set**: `um, uh, mm, hmm, mhm, er, ah, like, you know, basically, actually, sort of, kind of, i mean, right` — the first six are counted as *vocal* fillers, the rest as *verbal crutches*, reported separately.
- **pause**: a gap between consecutive Deepgram word end/start. `>= 0.35 s` counts; `>= 2.0 s` is dead air.
- **wpm**: words / speaking time, where speaking time excludes gaps `>= 2.0 s`.
- **low-confidence term**: a deck term whose best matching transcript word has `conf < 0.6`, or which never appears while a phonetically near word does.

## 3. Judgment (strict JSON from the judge LLM)

```jsonc
{
  "scores": { "delivery": 3, "clarity": 2, "structure": 4, "slide_connection": 3 },
  "summary": "One or two spoken sentences, under 40 words.",
  "improvements": [
    { "id": "imp_1",
      "quote": "verbatim span from the transcript",
      "span": { "start": 3.2, "end": 9.7 },
      "issue": "one spoken sentence naming the problem",
      "rubric_ref": "delivery.md#filler-rate",
      "v2_text": "the same point, cleaned wording, <= 25 words",
      "v3_markup": "the same point with <700> pause markup and emphasis",
      "alternative": "a different way to open the same idea" }
  ],
  "terms_to_drill": ["Dijkstra"]
}
```

Rules: exactly 3 improvements; `quote` must be a verbatim substring of the
transcript (code verifies and drops any that is not); `v3_markup` may use only
`<NNN>` pause markup (Mist v3), never SSML.

## 4. Timeline events (`timeline.jsonl`)

One object per line: `{"t": 12.847, "ev": "...", ...}` where `t` is seconds since
session start (monotonic).

| `ev` | Extra fields | Meaning |
|---|---|---|
| `phase` | `name` (`setup`\|`prep`\|`present`\|`analyze`\|`coach`\|`drill`\|`report`) | phase entered |
| `revision` | `revision`, `scope` | a new presentation revision opened |
| `slide` | `slide` | slide shown |
| `user_speech_start` / `user_speech_end` | — | VAD boundary |
| `user_transcript` | `text`, `final` | STT result |
| `agent_speech_start` | `speech_id`, `text`, `kind` (`ack`\|`cue`\|`feedback`\|`clip`\|`answer`) | Rime output began |
| `agent_speech_end` | `speech_id`, `interrupted`, `played_s` | playout finished or was cut |
| `interrupt` | `speech_id`, `latency_ms` | user interruption onset → audio stop |
| `tool_start` / `tool_end` | `tool`, `revision`, `ms` | judge or render call |
| `stale_dropped` | `tool`, `revision`, `current_revision` | a result was fenced and never spoken |
| `feedback_heard` | `improvement_id` | item marked heard after full playout |
| `provider` | `name`, `model`, `speaker` | active speech provider |

## 5. Data channel messages

JSON over LiveKit data packets, `topic="podium"`, reliable.

Agent → client:
```jsonc
{"type":"phase","name":"present","budget_s":60}
{"type":"deck","deck":{...}}                       // §1 deck
{"type":"slide","slide":2}
{"type":"timer","remaining_s":30}
{"type":"metrics","revision":1,"metrics":{...}}    // §2
{"type":"judgment","revision":1,"judgment":{...}}  // §3
{"type":"feedback","item":{"id":"imp_1","stage":"v2","text":"..."}}
{"type":"clip","improvement_id":"imp_1","variant":"v3","url":"/clips/imp_1_v3_paced.wav"}
{"type":"provider","name":"rime","model":"mistv3","speaker":"astra"}
{"type":"error","message":"..."}
```

Client → agent:
```jsonc
{"type":"setup","topic":"self-attention","level":"intermediate","budget_s":60}
{"type":"deck_upload","slides":[{"index":1,"title":"...","bullets":["..."]}]}
{"type":"ready"}            // prep finished, start presenting
{"type":"slide_next"}
{"type":"present_end"}
{"type":"rerecord","slide":2}
{"type":"command","name":"skip"|"again"|"slower"|"why"}
```

## 6. Module boundaries

| Path | Owns | Imports from siblings |
|---|---|---|
| `agent/session_agent.py` | phases, turn policy, speech queue, revision fence, timeline | `analysis.*`, `agent.llm_config`, `agent.store` |
| `agent/store.py` | session dir, `session.json`, `timeline.jsonl`, WAV writing | — |
| `analysis/metrics.py` | §2 from words + audio | — |
| `analysis/judge.py` | §3 from deck + metrics + transcript, loads `skills/judge/*.md` | `agent.llm_config` |
| `analysis/render.py` | V1/V2/V3 clips via Rime REST, returns paths + durations | — |
| `analysis/deck.py` | generate a deck (§1) from a topic; normalise an uploaded one | `agent.llm_config` |
| `web/` | UI only; speaks §5 | — |
| `evidence/` | acceptance tests; may import `analysis.*` | — |

`analysis/*` is pure and importable without LiveKit so evidence scripts can run it
directly. Nothing in `analysis/` may call `session.say` or touch the room.

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
    <improvement_id>_v0_you.wav        # the presenter's OWN recording, sliced
    <improvement_id>_v2_cleaned.wav
    <improvement_id>_v3_paced.wav
    <improvement_id>_v1_asdelivered.wav   # evidence pipeline only (E3); never
                                          # rendered or played in a live session
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
  "low_confidence_terms": [{"term":"Dijkstra","heard":"dexter","conf":0.42,"start":33.1}],
  "pronunciation": {                                   // v2: from per-word Deepgram confidence
    "intelligibility": 0.87,                           // 1 - (words with conf < 0.6) / words
    "words_below_0_6": 12,
    "low_confidence": [{"w":"heads","start":41.2,"end":41.6,"conf":0.28}],  // sorted by conf, <= 12
    "source": "deepgram_rest" | "stream"               // stream = segment-level conf only (fallback)
  }
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
  "scores": { "delivery": 3, "clarity": 2, "structure": 4, "slide_connection": 3, "pronunciation": 3 },
  "summary": "One or two spoken sentences, under 40 words.",
  "improvements": [
    { "id": "imp_1",
      "quote": "verbatim span from the transcript",
      "span": { "start": 3.2, "end": 9.7 },
      "issue": "one spoken sentence naming the problem",
      "rubric_ref": "delivery.md#filler-rate",
      "v2_text": "the same point, cleaned wording, <= 25 words",
      "v3_markup": "the same point with <700> pause markup and emphasis",
      "alternative": "a different way to open the same idea",
      "skill": "pacing",          // v2: a skills/curriculum/<skill>.md id (see §9)
      "slide": 2 }                // v2: slide the quote was spoken on, or null
  ],
  "terms_to_drill": ["Dijkstra"],
  "drill_words": [{"w":"heads","start":41.2,"end":41.6,"conf":0.28}]   // v2: <= 3 from metrics.pronunciation.low_confidence, deck terms first
}
```

`span` is derived by code from the transcript words (first/last word of `quote`), never
trusted from the LLM. `pronunciation` is an **intelligibility** score — how reliably a
recogniser understood the words — and is described to the user as such, never as an
accent judgement.

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
| `interrupt` | `speech_id`, `latency_ms`, `source?` | user interruption onset → audio stop; `latency_ms` is `null` with `source: "client"` when the cut came from a button/flow, not a voice onset |
| `tool_start` / `tool_end` | `tool`, `revision`, `ms` | judge or render call |
| `stale_dropped` | `tool`, `revision`, `current_revision` | a result was fenced and never spoken |
| `feedback_heard` | `improvement_id` | item marked heard after full playout |
| `provider` | `name`, `model`, `speaker` | active speech provider |
| `coach_stage` | `stage`, `improvement_id?`, `attempt?` | conversational coach moved to a stage (§5 `coach` message) |
| `voice_intent` | `name`, `source` (`regex`\|`llm`\|`button`) | a user choice was recognised |
| `practice_attempt` | `improvement_id`, `attempt`, `fillers`, `longest_pause_s`, `wpm`, `on_point` | the user repeated a point; §7 verdict computed |
| `nudge` | `reason` (`idle`\|`timeout`), `stage` | the coach checked in on a silent user |
| `no_speech` | `revision`, `words` | rehearsal too short to judge; returned to prep |
| `agent_speech_text` | `speech_id`, `text` | what an LLM-phrased line actually said (its `agent_speech_start.text` holds the instructions) |
| `feedback_stage` | `improvement_id`, `stage` | v2: an item's `feedback` message stage was sent (mirrors §5 `feedback.item.stage`) |
| `tool_error` | `tool`, `error` | v2: a non-fatal tool call failed and a fallback was used |
| `drill` | `word`, `stage`, `conf_before?`, `conf_after?` | v2: a drill step for a low-confidence word (mirrors §5 `drill`) |
| `progress` | `level`, `next_focus` | v2: cross-session progress was recorded and spoken at report |
| `attention` | `state` (`listening`\|`waiting`\|`thinking`) | v3: UI attention/orb state changed (§5 `attention`); sent at every change, client may receive repeats |
| `turn` | `owner` (`agent`\|`user`), `expect`, `seq` | v4: input-gate ownership changed (§5 `turn`) |
| `gate_blocked` | `type`, `owner`, `expect` | v4: a client→agent message arrived outside the current gate and was dropped |
| `mic` | `open` | v4: the client's push-to-talk floor opened/closed |
| `setup_slot` | `slot` (`topic`\|`level`\|`budget_s`), `value`, `source` (`voice`\|`client`) | v4: a setup slot was filled |


## 5. Data channel messages

JSON over LiveKit data packets, `topic="podium"`, reliable.

Every UI-affecting message (`coach`, `feedback`, `judgment`, `metrics`, `drill`, `progress`,
`phase` into `coach`/`report`, `focus`) that has a matching spoken line is sent **only once
that line's audio is already synthesized**, immediately followed by `session.say(text,
audio=<frames>)`. While the line is being prepared the agent sends `attention: thinking`.
Consequence for the client: when such a message arrives, audio starts within ~0.4 s (measured
0.25 s median over a full live session), so the client may reveal the matching UI component
immediately. `attention: waiting` for the setup form is the one deliberate exception: it is sent
only after the greeting/new-talk prompt has finished playing out, so the form appears when the
coach stops speaking rather than mid-sentence.

Agent → client:
```jsonc
{"type":"phase","name":"present","budget_s":60}
{"type":"deck","deck":{...}}                       // §1 deck
{"type":"slide","slide":2}
{"type":"timer","remaining_s":30}                   // prep only; during present the client runs its own clock from phase.budget_s
{"type":"attention","state":"listening"|"waiting"|"thinking"}
                                                    // v3: listening = default (mic open, nothing specific expected);
                                                    // waiting = the coach asked something and is waiting on the user
                                                    // (setup form / topic, prep "ready", ask_proceed, practice attempt,
                                                    // menu choice, drill answer, wrap choice) -- client shows the "your
                                                    // turn" orb; thinking = analysis/judge/LLM/TTS prep in progress,
                                                    // agent silent -- client shows the loading orb.
{"type":"focus","target":"score"|"metric"|null,"key":"delivery"}
                                                    // v3: sent immediately before the speech that talks about that
                                                    // element starts, so the UI highlights it in sync with the voice.
                                                    // target="score": key ∈ judgment.scores keys. target="metric": key ∈
                                                    // wpm|fillers|pauses|time|intelligibility. target=null clears it.
{"type":"metrics","revision":1,"metrics":{...}}    // §2
{"type":"judgment","revision":1,"judgment":{...},"previous":{"revision":0,"scores":{...}}|null}
                                                    // §3 judgment. previous = scores of the most recent superseded
                                                    // judgment of the SAME talk (null for the talk's first judgment,
                                                    // and after `new_talk`); the client renders per-bar deltas from it.
{"type":"feedback","item":{"id":"imp_1","stage":"v0","text":"...","role":"user","markup":null,"available":true}}
                                                    // v2: sent at every step of an item so the UI can cue it. stage ∈
                                                    // intro | v0 (the user's OWN recording slice) | v2 | v3 | alt | prompt | verdict | resume
                                                    // v4: `role` is who is about to be heard -- "user" for the v0 beat
                                                    // (the presenter's own recording) and "coach" for every line the
                                                    // agent speaks; the client colours the transcript red for "user"
                                                    // and green for "coach". `markup` carries the pause-marked text
                                                    // (`<600>` glyphs) for the v3/prompt beats so the user can read what
                                                    // to say, else null. `available` is false only when the v0 slice
                                                    // could not be cut -- the agent then SKIPS the beat and never
                                                    // re-synthesizes the user's own words in its own voice.
{"type":"clip","improvement_id":"imp_1","variant":"v3","url":"/clips/imp_1_v3_paced.wav"}
                                                    // variant is v0 (the presenter's own recording slice) | v2 | v3 |
                                                    // attempt<N>. v1 (the coach re-speaking the user's verbatim words)
                                                    // is never rendered or sent in a live session -- the v0 beat plays
                                                    // the user's own audio, or is skipped.
{"type":"drill","word":"heads","stage":"you"|"model"|"prompt"|"verdict","conf_before":0.28,"conf_after":0.91,"url":"/sessions/<id>/clips/drill_heads_you.wav"}
{"type":"progress","client_id":"...","level":"Speaker","levels":["Novice","Speaker","Presenter","Keynote","TEDx-ready"],
 "skills":{"pacing":{"mastery":0.4,"sessions":2,"last_focus":"2026-09-08"}}, "next_focus":"pausing"}   // §8
{"type":"countdown","id":"<unique-token>","status":"loading"}
{"type":"countdown","id":"<same-token>","status":"ready","clips":[{"label":"3","url":"/sessions/<id>/clips/countdown_3.wav"},{"label":"2","url":"..."},{"label":"1","url":"..."},{"label":"Begin","url":"..."}]}
{"type":"countdown","id":"<same-token>","status":"error","message":"..."}
{"type":"turn","seq":7,"owner":"agent"|"user","expect":null|"topic"|"level"|"length"|"ready"|"choice"|"attempt"|"drill"|"present",
 "allow":["setup","deck_upload","command"],"hint":"say a topic, or upload your own deck"}
                                                    // v4: THE input gate. `owner:"agent"` means the agent is speaking or
                                                    // working and the client must refuse every interactive control;
                                                    // `owner:"user"` means the floor is the user's and exactly the
                                                    // client→agent message types in `allow` are accepted. `seq` is
                                                    // monotonic per session: a client must ignore a lower `seq`.
                                                    // `hint` is the one short line the client shows as the "what to say
                                                    // now" affordance. Sent on every ownership/expectation change.
{"type":"setup_state","topic":"self-attention"|null,"level":"beginner"|null,"budget_s":60|null,
 "source":"voice"|"client","missing":["level","budget_s"]}
                                                    // v4: every setup slot the agent currently holds, echoed the moment
                                                    // it is filled -- from voice or from the form. The client mirrors
                                                    // these into the setup form (autofilling a spoken topic) and shows
                                                    // `missing` as what the agent is still asking for.
{"type":"reset"}                                   // clear current talk UI; keep room, microphone, provider and user progress
{"type":"coach","stage":"...","options":[{"name":"proceed","label":"Let's do it"}],
 "improvement_id":"imp_1","index":1,"total":3,"attempt":2,"verdict":{...}}   // §7
{"type":"provider","name":"rime","model":"mistv3","speaker":"astra"}
{"type":"error","message":"..."}
```

`coach.stage` ∈ `greeting` | `deck_ack` | `setup_level` | `setup_length` | `summary` |
`ask_proceed` | `item` | `practice` | `verdict` | `drill` | `wrap`.
`options` is the exact set of buttons the client should show right now (may be empty);
`improvement_id`/`index`/`total` accompany `item`/`practice`/`verdict`; `attempt` and
`verdict` accompany `verdict`. Clients must tolerate unknown stages.

Speech name: the coach introduces itself as **Podium** ("Hi, I'm Podium, your voice-first
presentation coach..."), regardless of the configured Rime speaker. `provider` keeps reporting
the real Rime speaker (e.g. `thunder`) for the status bar -- it never changes to "Podium".

Option names (client sends `{"type":"command","name":<name>}`; the agent accepts the
same names by voice): `proceed` (yes, start improvements) · `later` (skip them, go to
the wrap-up) · `original` (play V0, the user's own recording -- V1 is NEVER substituted;
if the slice is unavailable the agent says so) · `cleaner` (play V2) · `pauses` (play V3) ·
`alternative` (speak the alternative opening) · `again` (replay V0+V2+V3) · `slower`
(re-render slower, replay) · `why` (rubric reason) · `practice` (prompt the user to
say it) · `next` (done with this item) · `skip` (move on without marking heard) ·
`finish` (leave the current improvement loop). At `coach.stage=wrap`, the dashboard
offers `more` (practice the existing improvements again), `rerecord` (try the whole talk
again -- `{"type":"rerecord"}` with no `slide` re-records the full deck; a client-initiated
rerecord may still target one `slide`), and `new_talk` (return to the topic/PDF form on the
same connection; natural declines at wrap -- "no", "I'm done", "that's enough", "stop" --
are also recognised as `new_talk`). The internal phase `report` remains a dashboard
continuation state, not a separate report page. Silence does not choose a continuation.
Repeated practice does not create extra progress credit for the same presentation revision.
Commands outside the currently offered options are ignored. `options` may exceed what a
minimal UI should show: the client renders at most **three** buttons for a stage (the
stage's primary actions) and leaves the rest voice-only.

### Input gate (v4)

`turn` is authoritative and enforced on BOTH sides. The agent drops any client→agent
message whose `type` is not in the current `allow` set (logging `gate_blocked` and
re-sending the current `turn`), and the client must not send one. Four types are never
gated: `client_ready`, `countdown_complete`, `countdown_failed`, `mic`. Consequences the
client must implement: no phase can be advanced while `owner:"agent"` -- the setup form,
the PDF upload, the choice strip, the prep "ready" key, and the present controls are all
inert until the agent hands the floor back. A `command` is additionally constrained to
`coach.options`, exactly as before.

Setup is a deterministic slot-filling flow, never an LLM improvisation: the agent fills
`topic`, then `level`, then `budget_s`, echoing `setup_state` and re-issuing `turn` at
each step, and only generates the deck once all three are known. A spoken topic fills the
form; the level and length asks are offered as `coach.options` so voice and buttons take
the same path. The deck is generated inside that flow (`owner:"agent"`, `allow:[]`), so
no UI message and no spoken line can ever arrive out of order with it.

Push-to-talk: the client holds the microphone muted outside `present` and publishes it
only while the user holds the floor (hold Space, or double-tap Space to latch). While the
floor is the user's the client sends `{"type":"mic","open":true}`; the agent stops
speaking immediately and starts no new line until `{"type":"mic","open":false}`. During
`present` the microphone is live for the whole take -- that recording is the deliverable.

Slide navigation during `present`: the deck moves one slide per request, by control
(`←`/`→`, the two on-screen steppers) or by voice ("next slide", "previous slide"). Both
directions share one 400 ms debounce, so a control press and the spoken phrase landing
together move one slide, not two; a repeated identical final transcript within 3 s is
treated as the recogniser echoing. A step past either end of the deck is dropped and the
client disables that stepper. Every recording opens on the first slide of its own scope:
slide 1 for a full take, the requested slide for a slide-scoped rerecord -- the previous
revision's position is never carried over. Each change emits `{"type":"slide","slide":n}`
and a `slide` timeline event, which is what `analysis/metrics.py` reads for per-slide pace.


Client → agent:
```jsonc
{"type":"setup","topic":"self-attention","level":"intermediate","budget_s":60,"client_id":"<uuid from localStorage>"}
{"type":"deck_upload","slides":[{"index":1,"title":"...","bullets":["..."]}],"client_id":"<uuid>"}
{"type":"client_ready"}     // sent from the start button's click handler once room.startAudio() succeeded;
                            // the agent holds the greeting until it arrives (120 s fallback)
{"type":"ready"}            // begin audio preparation, NOT presentation recording
{"type":"countdown_complete","id":"<matching-token>"} // only after all four local audio clips emitted ended
{"type":"countdown_failed","id":"<matching-token>"}   // actual playback failure; never start a presentation as fallback
{"type":"slide_next"}       // one step per message; both directions share a 400 ms debounce
{"type":"slide_prev"}       // steps back; a request past either end of the deck is dropped
{"type":"present_end"}
{"type":"rerecord","slide":2}   // omit `slide` (or send null) for a whole-talk retake
{"type":"command","name":"proceed"|"later"|"original"|"cleaner"|"pauses"|"alternative"|"again"|"slower"|"why"|"practice"|"next"|"skip"|"finish"|"more"|"rerecord"|"new_talk"
                        |"level_beginner"|"level_intermediate"|"level_advanced"|"len_60"|"len_90"|"len_120"}
                            // the level_*/len_* names are the setup slot answers (§5 input gate)
{"type":"mic","open":true}  // v4: push-to-talk floor; never gated
```

Countdown audio is rendered once per live session in the active Rime voice and played
by the browser, not queued again on the agent speech stream. Each numeral is shown on
its media element `playing` event; only `ended` advances to the next clip. Buffering or
blocked playback keeps the loading overlay visible. `phase:present` is sent only after
the matching completion acknowledgement and hides the overlay. Duplicate or stale
acknowledgements cannot start another recording. A failure returns to preparation.

## 7. Practice verdict (computed by `analysis/practice.py`, never by the LLM)

After the user repeats an improvement's point, code compares the attempt with the
original span. The coach LLM may only *phrase* these numbers, never change them.

```jsonc
{
  "attempt": 2,
  "text": "what the recogniser heard",
  "words": 14,
  "wpm": 142.0,                       // null when no word timings arrived
  "fillers": {"count": 0, "items": ["um"]},
  "original_fillers": 2,              // fillers in the original quote (§2 filler set)
  "pauses": {"count": 1, "longest_s": 0.72, "landed": true},  // landed: any pause >= 0.4 s
  "on_point": 0.71,                   // token overlap with quote ∪ v2_text, 0..1
  "pace_band": "good" | "fast" | "slow" | "unknown",   // good = 110..170 wpm
  "wins": ["no fillers", "pause landed"],
  "next_focus": "pace" | "fillers" | "pause" | null
}
```

`looks_like_attempt(text, improvement)` decides whether a user utterance during a
practice wait is a repetition (≥ 3 words and `on_point` ≥ 0.25) or a question for the
LLM. Attempt audio is saved as `clips/<improvement_id>_attempt<N>.wav`.

## 6. Module boundaries

| Path | Owns | Imports from siblings |
|---|---|---|
| `agent/session_agent.py` | phases, turn policy, speech queue, revision fence, timeline | `analysis.*`, `agent.llm_config`, `agent.store` |
| `agent/store.py` | session dir, `session.json`, `timeline.jsonl`, WAV writing | — |
| `analysis/metrics.py` | §2 from words + audio | — |
| `analysis/judge.py` | §3 from deck + metrics + transcript, loads `skills/judge/*.md` | `agent.llm_config` |
| `analysis/render.py` | V1/V2/V3 clips via Rime REST, returns paths + durations | — |
| `analysis/practice.py` | §7 verdict from an attempt's words/metrics vs. the improvement; `looks_like_attempt` | `analysis.metrics` |
| `analysis/slice.py` | v2: cut the user's own recording by word span (`slice_wav`, `span_for_quote`) | — |
| `analysis/pronunciation.py` | v2: Deepgram REST per-word confidence (`transcribe_rest`) and the §2 `pronunciation` block (`intelligibility`) | — |
| `agent/graph.py` | v2: `SessionGraph` temporal coaching graph (§8) + cross-session `Progress` | — |
| `skills/curriculum/*.md` | v2: the TEDx skill ladder the judge and coach cite (§9) | — |
| `analysis/deck.py` | generate a deck (§1) from a topic; normalise an uploaded one | `agent.llm_config` |
| `web/` | UI only; speaks §5 | — |
| `evidence/` | acceptance tests; may import `analysis.*` | — |

`analysis/*` is pure and importable without LiveKit so evidence scripts can run it
directly. Nothing in `analysis/` may call `session.say` or touch the room.

## 8. SessionGraph — the temporal coaching graph (`agent/graph.py`)

One in-process graph per session (no external store; the constraint "Rime, Deepgram,
LiveKit only" stands). Every node and edge carries `t` (seconds since session start).
`session_agent.py` writes to it at every step and reads the LLM's steering context
from it, so what the coach says is always derived from the same structure the UI shows.

Nodes `(kind, id, props)`: `slide:<n>`, `revision:<n>`, `improvement:<imp_id>`,
`clip:<imp_id>:<variant>`, `attempt:<imp_id>#<n>`, `verdict:<imp_id>#<n>`,
`utterance:<n>` (a user remark/question), `coach_line:<speech_id>`, `skill:<id>`,
`drill:<word>`.
Edges `(src, rel, dst, t)`: `improvement -targets-> slide`, `improvement -trains-> skill`,
`clip -renders-> improvement`, `attempt -practices-> improvement`, `verdict -scores-> attempt`,
`utterance -about-> improvement|slide`, `coach_line -regarding-> improvement|attempt|utterance`,
`drill -addresses-> improvement|skill`, `revision -supersedes-> revision`.

```python
class SessionGraph:
    def __init__(self, t0_mono: float): ...
    def add(self, kind: str, id: str, **props) -> str            # node key "kind:id"; upsert
    def link(self, src: str, rel: str, dst: str, **props) -> None
    def set_focus(self, node_key: str | None) -> None            # what the coach is on right now
    def focus(self) -> str | None
    def mark(self, node_key: str, **props) -> None               # e.g. heard=True, played=["v0","v2"]
    def context_text(self, max_chars: int = 1200) -> str         # LLM steering: focus, what's played/heard,
                                                                  # attempts+verdicts, last 3 utterances, remaining items
    def recap_text(self) -> str                                  # "what have we done so far" for the recap tool
    def to_json(self) -> dict                                    # persisted under session.json["graph"]

class Progress:                                                  # sessions/progress/<client_id>.json
    LEVELS = ["Novice", "Speaker", "Presenter", "Keynote", "TEDx-ready"]
    def __init__(self, client_id: str, root: Path = Path("sessions/progress")): ...
    def record_session(self, scores: dict, improvements: list[dict], verdicts: list[dict]) -> None
    def level(self) -> str                                       # from mean mastery across skills
    def next_focus(self) -> str | None                           # weakest skill with the fewest sessions
    def to_message(self) -> dict                                 # the §5 progress message
```

`mastery` per skill is a number in 0..1: an exponential moving average of the judge's
score for the skill's rubric category (scaled 1..5 → 0..1) and of practice-verdict wins
on improvements that train the skill. Levels are mean mastery bands: `<0.3` Novice,
`<0.5` Speaker, `<0.7` Presenter, `<0.85` Keynote, else TEDx-ready.

## 9. Curriculum (`skills/curriculum/<id>.md`)

The ladder from novice to TEDx: `hook`, `structure`, `pacing`, `pausing`, `fillers`,
`vocal-variety`, `storytelling`, `slide-connection`, `closing`, `articulation`. Each file
has the same headings so code can cite it: `# <Title>`, `## Why it matters`,
`## What good looks like`, `## Novice → TEDx` (four one-line levels), `## Drill` (one
30-second exercise), `## Judge maps to` (the `skills/judge/*.md#anchor` the score comes
from). The judge's `improvement.skill` must be one of these ids; the coach's `why`
answer reads `## Why it matters`, and the report's path view reads `## Novice → TEDx`.

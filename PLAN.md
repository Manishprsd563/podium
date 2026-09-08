# Podium — a voice coach that lets you hear the better version of your own presentation

Working name; rename freely. Everything below is a proposal, not built or measured.

## 1. One-paragraph pitch

A student, founder or engineer rehearsing a talk alone gets no useful feedback: a text tool cannot hear the four-second pause, the "um, so, basically", the rushed key sentence, and it cannot show what a better delivery *sounds* like. Podium listens to a timed presentation against real slides (generated on the spot to test comprehension, or uploaded), judges it against explicit rubric skill files, and then coaches by contrast: it plays your own sentence back, then the same words cleaned up, then the cleaned words delivered with deliberate pauses and emphasis — all in one Rime voice, held constant, so the only thing that changes is the delivery. You can interrupt the coach, ask for it slower, drill a mispronounced term, and re-record a single slide.

## 2. Rubric mapping (why this can score)

| Criterion | How Podium answers it |
|---|---|
| Problem & necessity of voice (25%) | The input is speech, the judged object is speech, and the corrective feedback is speech. Remove speech and nothing remains. |
| Hard voice engineering (25%) | Two voice failure modes solved under realistic conditions (see §3): (a) contrastive controlled delivery with measured audio evidence; (b) a presentation-mode turn policy: the agent must listen to a 2-minute monologue full of thinking pauses without taking the floor, still deliver a timed cue mid-speech, stay responsive during analysis, and fence stale analysis after interruption/re-record. |
| Rime integration (20%) | Rime speaks every coach turn *and* is the demonstration instrument: three text variants per improvement rendered with fixed model/voice; word timestamps used to align "what was actually heard"; playback speed control for "say it slower". |
| Evidence (20%) | Committed fixtures + one command per claim: rendered clips with measured pause/rate deltas; injected-delay duplex test with a machine-readable session timeline; small blinded listening test labelled exploratory. |
| Demo clarity (10%) | Fixed 4.5-min storyboard (§9): user, problem, normal flow, stress case, measurement, provider badge on screen. |

Prior-art distance: the catalog has "Continuum — Interview Practice" (Q&A with memory). Podium is monologue rehearsal graded against slides, with acoustic metrics computed from audio and contrastive Rime playback. State this in the README.

## 3. The hard voice claims (define before building)

**Primary claim — audible, controlled delivery.** For each top improvement, Podium renders with the same Rime model and speaker:
- V1 *as delivered*: verbatim transcript including fillers, false starts and the measured pauses re-inserted as explicit pause markup;
- V2 *cleaned wording*: fillers and false starts removed, sentence shortened per writing-for-the-ear;
- V3 *paced delivery*: V2 plus pause markup before the key phrase and emphasis wording.

Acceptance test (preregistered): over 12 fixture sentences, (i) V1 clips contain the fillers audibly (Rime word timestamps show them) and the inserted pauses measure within ±150 ms of the requested duration via silence detection on the rendered PCM; (ii) V3 has ≥ 1 measured pause ≥ 400 ms at the marked position and V2 has none there; (iii) exploratory blinded listening with 3–5 people: which of V2/V3 sounds more confident/clear, reported as counts, not a score. Model, speaker, speed logged per clip. If the pause syntax is unsupported on the chosen model, that is reported and the claim is reduced to V1/V2 wording contrast.

**Secondary claim — presentation-mode full duplex.** (a) On 6 pause-heavy fixture recordings (pauses 2–5 s), the coach never ends the presenter's turn before the explicit end signal (target 0 premature turn ends; the default VAD endpointing is run on the same recordings as the baseline and its premature ends are reported). (b) With a 5 s delay injected into the judge tool: interrupting feedback stops audible Rime output with P95 ≤ 300 ms from labelled interruption onset; re-recording a slide during a pending judgment never results in the stale judgment being spoken (10 runs, target 0); feedback items are marked "heard" only after client playout completes.

## 4. Product flow

1. **Setup** (voice or click): "Quiz me on transformers, intermediate, 90 seconds" → LLM generates a 3-slide deck as JSON, rendered as HTML. Or upload a PDF (export PPT → PDF; parsed and rendered in-browser with pdf.js, page text extracted per slide). PPTX is out of scope; say so.
2. **Prep**: countdown (default 60 s), slides visible, coach silent. "I'm ready" or timer ends.
3. **Present**: presentation-mode turn policy (manual turn end). User advances slides by "next slide" or key; slide-change timestamps recorded. At T−30 s a short Rime cue ("thirty seconds") plays *while the user keeps talking* — the agent does not take the floor. Ends on "that's it / I'm done", key, or hard limit.
4. **Analyze**: within ~1 s the coach acknowledges ("Got it, give me a moment"). Acoustic metrics are computed locally and immediately (pace, pauses, fillers, loudness); the rubric judge (LLM + skill files) runs as an async tool. While it runs the user can ask "how was my pacing?" and get an answer from the metrics already available — genuine continuity during tool work. Results are bound to a presentation revision id.
5. **Coach**: spoken scorecard (≤ 4 sentences), then a queue of top-3 improvements. Each item: what you said (your own audio excerpt) → V2 → V3, each ≤ 2 sentences of framing. Interruptible: "skip", "again", "slower" (Rime speed control), "why". Items marked heard only on playout completion; interrupted items are re-offered.
6. **Drill** (if the judge flagged domain terms with low ASR confidence): coach says the term, user repeats, ASR match ≤ 2 tries. Labelled as an intelligibility proxy, not phonetic assessment.
7. **Report**: on-screen scorecard per slide, metrics, clips (yours / V1 / V2 / V3), "re-record slide N" (new revision, old analysis fenced), session saved to `sessions/<id>/`. Progress view across sessions: filler rate and pace trend (flat JSON, no DB).

Stretch (only if the build window runs to 12 Sept): one audience question after the talk, judged on the answer; a second coach voice for the "audience".

## 5. Judging via skill files

`skills/judge/*.md`, loaded verbatim into the judge prompt; code, not the LLM, computes numbers.

- `delivery.md` — pace bands (wpm), pause taxonomy (thinking pause vs rhetorical pause vs dead air), filler rate bands, loudness variance; how to cite the metric.
- `clarity.md` — short sentences, define terms before use, intuitive framing/analogy first, signposting.
- `structure.md` — hook, roadmap, transitions, close; time allocation per slide.
- `slide-connection.md` — covers the slide's points, does not read it verbatim, references what is on screen, stays within time.
- `pronunciation.md` — domain terms from slide text, low-confidence/misrecognized detection, honest limits.
- `feedback-style.md` — writing-for-the-ear rules for the spoken feedback: quote verbatim → name the issue → corrected → alternative; ≤ 2 sentences each; no lists read aloud.

Judge output is a strict JSON schema: per-slide scores, overall, `improvements[≤3]` each with `{quote, span, issue, rubric_ref, v2_text, v3_markup, alternative}`, `terms_to_drill[]`.

## 6. Architecture (minimal)

```
Browser (Vite, vanilla TS)                 Agent (Python, livekit-agents)
  mic/playback via livekit-client   <->    LiveKit room (Cloud free tier or local livekit-server)
  slides: generated HTML | pdf.js          STT: Deepgram nova-3 (filler_words, word ts, confidence)
  timers, live transcript                  LLM: OpenAI-compatible endpoint (to decide)
  provider badge (model/speaker)           TTS: official Rime plugin (coach voice)
  report + clip player                     render.py: direct Rime API for contrastive clips (saved WAV + ts)
  data-channel messages  <->               metrics.py: wpm, pauses, fillers, RMS loudness
                                           judge.py: skills/*.md + JSON schema
                                           session store: sessions/<id>/{audio.wav, transcript.json,
                                             slides.json, metrics.json, judgment.json, clips/, timeline.jsonl}
evidence/: fixtures + runner scripts (one command per claim)
```

Why LiveKit Agents: the brief recommends it, it gives WebRTC transport, barge-in, and manual turn detection out of the box, so effort goes into the coaching and evidence rather than audio plumbing. Why Deepgram: `filler_words=true` keeps "um/uh" in the transcript with timestamps and confidence; Whisper-class STT strips fillers, which would blind the core metric.

Explicitly **not** built: DB, auth, multi-language, PPTX parsing, pitch/prosody ML, mid-sentence real-time correction, mobile, telephony, vector store.

### Cross-slice contracts (frozen before fan-out)
- `session.json` schema (above) and `timeline.jsonl` event vocabulary: `phase`, `slide`, `user_speech_start/end`, `agent_speech_start/end/interrupted`, `tool_start/end`, `revision`, `provider`.
- Data-channel messages, agent→client: `phase`, `slide.show`, `timer`, `transcript.partial`, `metrics`, `judgment`, `feedback.item`, `provider`. Client→agent: `slides.upload`, `present.end`, `slide.next`, `rerecord {slide}`.
- Rime config comes from one `rime_config.py` (model id, speaker, language, endpoint, format, speed) and is echoed in the provider badge and README.

## 7. Gate 0 — verify before anything else (≈2 h)

1. Organizer Rime key + preflight script present and passing.
2. Rime capability probe (`scripts/rime_probe.py`): for Coda and Mist v3 with one live-catalog English speaker each — render a plain sentence, a sentence with pause markup, a sentence with fillers, three domain terms, and one slowed render. Record: whether pauses render (measured), speed behaviour, word timestamps available on the path used, first-audio latency, audio format. Pick the model per outcome (pause support decides). Save clips in `evidence/probe/`.
3. LiveKit hello-world: browser joins room, agent speaks one Rime sentence, barge-in works, manual turn mode confirmed.
4. Deepgram streaming with `filler_words=true` returns "um" with timestamps.

Any failure here changes the plan; nothing else starts before this is green.

## 8. Work breakdown (parallel after Gate 0)

| Slice | Owner scope | Deliverable |
|---|---|---|
| A. Agent core | phases state machine, presentation-mode turn policy, T−30 cue, ack + async judge + revision fence, interruptible feedback queue with heard-tracking, drill loop, timeline logging | `agent/` runnable with a stub judge |
| B. Analysis | `metrics.py`, `judge.py` + `skills/judge/*.md` + JSON schema, `render.py` contrastive clips with timestamps, slide generation prompt | unit-runnable on a saved session |
| C. Web | Vite app: setup, slide viewer (generated + pdf.js upload), timers, transcript, provider badge, feedback/report view, clip player, progress view | connects to a room, handles all messages |
| D. Evidence & submission | fixtures, `evidence/*.py` runners, `RIME_EVIDENCE.md`, `README.md`, `.env.example`, demo script and recording | one command per claim |

Serialization only where required: D's duplex runner needs A; B's render needs Gate 0's chosen model.

## 9. Demo storyboard (4:30)

- 0:00–0:30 — the user and the problem: rehearsing alone; text feedback can't hear pauses or show a better delivery.
- 0:30–1:30 — normal flow: topic → generated slides → 45 s prep → 60 s talk with visible thinking pauses (coach stays silent) → T−30 cue while speaking → "I'm done".
- 1:30–2:45 — instant ack; "how was my pacing?" answered during judging; scorecard; contrastive playback of one improvement (you → cleaned → paced). Provider badge visible: Rime model/speaker.
- 2:45–3:30 — stress: interrupt mid-feedback ("wait, play the paced one slower") — audio stops, replays slower; re-record slide 2 while old judgment is pending — stale result never spoken; timeline shown.
- 3:30–4:15 — evidence: measured pause/rate deltas per variant, the fixture command, listener counts, limitations.
- 4:15–4:30 — the contribution in one sentence.

## 10. Timeline

If the package is due **8 Sept 23:59 IST** (~32 h): Gate 0 → A+B+C in parallel (~12 h) → integrate (~4 h) → evidence E1 + duplex run + docs (~6 h) → record demo (~2 h) → buffer. Cut list in order: progress view, drill loop, PDF upload (keep generated slides), V1 rendering (keep V2/V3).

If the build runs to **12 Sept**: same core first, then the cut list restored, listener test, stretch Q&A.

## 11. Decisions needed before Gate 0

1. Which deadline applies: 8 Sept package or 12 Sept presentation?
2. Approve installs: Python — `livekit-agents`, `livekit-plugins-rime`, `livekit-plugins-deepgram`, `livekit-plugins-openai`, `livekit-plugins-silero`, `numpy`, `python-dotenv`; JS — `vite`, `livekit-client`, `pdfjs-dist`. LiveKit server: Cloud free tier (account) or local `livekit-server` binary.
3. STT/LLM providers and keys: Deepgram (recommended) and which OpenAI-compatible LLM (fast cloud model preferred for the judge; local Ollama is a fallback, not a demo path).
4. Rime credentials: organizer key and preflight script available?

## 12. Coaching v2 — from a scripted queue to a coach that keeps context (post-testing)

Human testing of v1 surfaced five problems, each with a concrete root cause found in
the code rather than assumed:

| Reported | Root cause | v2 answer |
|---|---|---|
| Presentation clock frozen | The agent only sends `timer` during prep; `present` never did, so the client showed the last prep value | Client-side clock from `phase.budget_s`, overtime shown in amber |
| Replies arrive late | LLM starts only after end-of-turn; endpointing was held at 0.9 s for the whole coach phase; clips were rendered *inside* the conversation | `preemptive_generation` (LLM starts on the interim transcript); 0.9 s endpointing only while a practice take is being captured; all clips pre-rendered concurrently while the summary is spoken |
| Can't tell an "improvement" from small talk | No `feedback` stage message was ever sent, so the UI could not cue what was playing; a barge-in restarted the item from its intro | Signposted items ("Improvement 2 of 3 — pausing, slide 2"), the user's **own recording** played first, a stage cue row in the UI (You · Cleaner · With pauses · Your turn), and resume-from-the-interrupted-step |
| Coach loses the thread | The LLM was steered with a flat string rebuilt per turn | `SessionGraph` (§CONTRACTS 8): a timestamped in-process graph of slides, improvements, clips, attempts, verdicts, utterances and skills; the LLM's context and the UI derive from the same structure; a `recap` tool answers "what have we done" |
| Voice is dull | `summit` is polished but flat | `thunder` — Rime's high-energy flagship, pause markup verified |

Two new capabilities ride on the same pass:

- **Pronunciation as intelligibility.** Deepgram's REST endpoint returns real per-word
  confidence (the live stream does not). After each rehearsal the recording is
  re-transcribed once (~2 s), giving an intelligibility score, the words a recogniser
  struggled with, and a drill that plays the user's own word, the coach's model, and
  measures the confidence again. Described to the user as "how reliably a recogniser
  understood you" — never as an accent judgement, which we cannot measure honestly.
- **A curriculum, not a checklist.** Ten skill files (`skills/curriculum/`) define the
  ladder from novice to TEDx: hook, structure, pacing, pausing, fillers, vocal variety,
  storytelling, slide connection, closing, articulation. Every improvement is tagged
  with the skill it trains; a per-user `Progress` record (keyed by a browser-local id)
  tracks mastery across sessions and names the next focus. Levels: Novice → Speaker →
  Presenter → Keynote → TEDx-ready.

What deliberately did *not* change: the presentation-mode turn policy, the revision
fence, the rule that code computes every number, the three-service stack, and the
measured (and still missed) barge-in stop latency, which is a transport property this
pass does not claim to fix.

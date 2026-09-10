# Podium pitch video v3 — actual product UI, three voices, twelve scenes

Canvas 1920x1080 @ 30 fps. Target ≈ 258 s. Real Podium UI capture is shown muted inside a designed browser surface; narration, coach, music and SFX remain film-controlled so recorded-demo audio can never overlap them. The exact Three.js shader orb from `web/orb.js` is adapted to deterministic frame seeking and shown in its real product states. HTML/CSS explanatory overlays are derived from `web/style.css`, `web/dashboard.css`, `web/index.html`, and `web/app.js`. Every number is either (a) a real measurement from the repo (README.md, RIME_EVIDENCE.md, `sessions/s_20260909_160535/session.json`) or (b) an in-story value for the fictional presenter “Maya”, labelled below. Never present (b) as evidence.

## Cast (all Rime `mistv3`, `lang=eng`, 24 kHz WAV via REST `https://users.rime.ai/v1/rime-tts`, `pauseBetweenBrackets: true`)

| role | id prefix | speaker | direction |
|---|---|---|---|
| NARRATOR — the film's voice | `n` | `astra` | confident, warm, advertising cadence; short sentences |
| MAYA — first-time presenter | `m` | `willow` | nervous beginner: fillers, run-ons, rushes; later confident |
| COACH — Podium itself | `c` | `thunder` | calm, specific, kind; the shipped product voice |

`<NNN>` = Rime pause marker in ms (max 3 per line, right after punctuation). Written text of
fillers ("um", "uh", "so, like") is spoken literally — that is the point of Maya's early lines.

## Asset contract

- VO: `public/vo/<id>.wav` (24 kHz mono PCM16, silence-trimmed, ≤120 ms pad) + `public/vo/manifest.json`
  `{ "<id>": { "speaker": "...", "seconds": 4.21, "text": "..." }, ... }`
- SFX: `public/sfx/*.mp3` (bundled media-use library, already copied). Names used below:
  `riser`, `impact-bass-1`, `impact-bass-2`, `whoosh`, `whoosh-short`, `whoosh-cinematic`, `pop`,
  `click`, `click-soft`, `key-press`, `typing`, `ping`, `notification`, `chime`, `sparkle`,
  `error`, `glitch-1`, `glitch-2`, `glitch-3`.
- Music: `public/music/bed.wav` (≥ 250 s, 48 kHz stereo, −24 LUFS integrated, sectioned:
  0–14 s sparse intro · 14–150 s pulse at 96 BPM · 150–200 s lift (added layer) · 200–end resolve
  and tail). Narration is carved into it (`data-fx-carve` against the `voice` audio group).
- Scenes: `compositions/sNN-<slug>.html`, each a self-contained sub-composition sized 1920x1080
  with `data-duration` equal to the scene length in the manifest; the root `index.html` places them.
- Design tokens: `ui/tokens.css` (extracted from `web/style.css` + `web/dashboard.css`) and
  `ui/components.css` (statusbar, hero/orb, caption, chips, slide card, clock, scorecard,
  improvement card, drill ring, skill tile, sparkline). Scenes link both; nothing re-declares colours.

## Real numbers (usable as evidence, cite the source on screen where it matters)

- Demo session `s_20260909_160535`: 149 words, 151.8 wpm; per slide 114.4 / 147.1 / 128.3 wpm;
  10 pauses, longest 3.28 s, mean 1.08 s; loudness −18.1 dBFS mean, 7.7 dB variance;
  intelligibility 0.913, 13 words below 0.6 confidence.
  Judge scores (0–5): delivery 2 · clarity 2 · structure 2 · slide connection 3 · pronunciation 4.
  Improvement 1 verdict: 173.3 wpm "fast" → 142.9 wpm "good · pause landed" (0.42 s).
  Drills: services 69 % → 96 % · trade 39 % → 0 % (honest failure) · good 42 % → 98 %.
- RIME_EVIDENCE.md claim 1: `<1500>` pause → 1480 / 1620 / 1580 ms (−20 / +120 / +80 ms), 3/3
  within ±450 ms, digits never spoken; coda 0/3.
- Claim 2: 6/6 V1 clips carry the filler, 6/6 V3 clips have a ≥400 ms deliberate pause, 6/6 never speak markup.
- Claim 3: presentation-mode turn policy 0/6 premature turn ends vs default VAD 6/6.
- Claim 4: barge-in stop latency P50 682 ms · P95 1400 ms · target ≤300 ms · **not met**, published.
- Stack: LiveKit Cloud (India South) · Deepgram `nova-3` STT · SessionGraph (turn-gated) ·
  coach LLM `google/gemma-4-31b-it` · judge LLM `openai/gpt-5.4-mini` · Rime `mistv3`/`thunder`
  live over WebSocket `wss://users-ws.rime.ai/ws3` (PCM 24 kHz, `pause_between_brackets=true`),
  REST `https://users.rime.ai/v1/rime-tts` for pre-synthesis and V1/V2/V3 contrast clips.
- Curriculum: 10 skills — Hook · Structure · Pacing · Pausing · Fillers · Vocal Variety ·
  Storytelling · Slide Connection · Closing · Articulation; ladder Novice → Speaker → Presenter → TEDx.
- Statusbar badge text: `connected · agent present · rime · mistv3 · thunder · listening · present · deck_ack · mic live · hold space to talk · podium-fqamkqva`. The film-level statusbar appears only inside the rebuilt browser window (S02) and as the subject of S11; every other scene relies on the statusbar visible in the real captures.

## In-story numbers (Maya — label on screen as "Maya · session 1 / session 2", never as evidence)

- Session 1 scorecard = the real demo scorecard (2 · 2 · 2 · 3 · 4), 173 wpm on the key line, 4 fillers/min.
- Session 2 scorecard: 4 · 4 · 3 · 4 · 4, 143 wpm on the key line, 0 fillers/min, pause landed.
  Deltas shown: +2 +2 +1 +1 ±0 · −30 wpm · −4 fillers/min.

## Scenes and lines

Scene lengths are targets; the Integrator retimes to real VO durations (VO drives the cut; leave
≥ 0.4 s of air after the last line of each scene). SFX cues are listed with the visual they hit.

### S01 — Cold open (0:00–0:13)
Black. A single hairline draws across centre. Orb (rebuilt from `orb.js` look: teal→periwinkle
glow, slow breathing) blooms behind it. Words cut in hard, one per beat, kinetic type:
"Every great talk" / "started as a nervous first draft." Then wordmark **PODIUM** with a
letter-spacing settle and the sub "the presentation coach that listens".
- SFX: `riser` from 0 s → `impact-bass-1` at the wordmark; `click-soft` on each word cut.
- n01: "Every great talk started as a nervous first draft. <500> Most people rehearse that draft alone, in silence. Nobody listens. Nobody tells them what to fix."
- n02: "Podium listens."

### S02 — Meet Maya (0:13–0:34)
The real Podium hero: orb centre, caption column, statusbar at the bottom with the live badge.
Maya's words appear as the live caption (typewriter, word-by-word with Deepgram-style interim
grey → final white). Coach answers; caption swaps to the coach colour. Choice chips
(`#choices` style) stagger in: Novice · Speaker · Presenter · TEDx, then 3 min · 5 min · 8 min;
"Novice" and "5 min" light up as she says them. Statement, right column: **"No form. No setup. Just talk."**
- SFX: `pop` on each chip; `click` when a chip selects; `notification` when the coach starts speaking.
- m01: "Um, hi. So... I have to give a talk next week? About smart cities. It's like five minutes, and I've, uh, never really done this before."
- c01: "Smart cities. Good topic. <300> Let's keep it at Novice level, five minutes. I'll write you three slides. You'll get sixty seconds to prep before you present."
- n03: "There is no form to fill. You say what you want to talk about, <200> Podium picks up the level and the length from your own words, and writes the slides."

### S03 — The deck materialises (0:34–0:46)
Three slide cards (real slide styling: dark card, title, three bullets, page indicator `1 / 3`)
fly in with a 3D perspective fan, then the first snaps flat to centre. Titles: "What is a smart
city" / "Key city systems" / "Benefits and tradeoffs". Chip under the deck: "Deck generated from
your voice · or upload your own PDF". Prep countdown ring runs 60 → 57 in the corner.
- SFX: `whoosh-short` ×3 on the fan, `sparkle` when the first slide settles, `click-soft` on the countdown ticks.
- n04: "Three slides, generated from a sentence. <300> Or bring your own deck as a PDF. Then a sixty second prep, and the clock starts."

### S04 — Session 1: Maya presents (0:46–1:14)
Presentation layout: the real `c06_present` capture in a browser frame, caption column on the
left with the spoken line. No meter, clock, or waveform overlays — the capture carries them and
the scorecard in S05 carries the numbers. When the presenter stops to think a single mono line
`turn policy · holding` with a thin teal hairline marks the 2.4 s in which Podium does not
interrupt.
- SFX: `key-press` on each filler tick; `ping` when the meter enters "fast"; `chime` (low) when the hold badge resolves.
- m02: "So, um, a smart city is basically, like, a city that uses data to, uh, make daily life better? It connects transport and utilities and, um, services, and it, it aims for faster, cleaner decisions."
- m03: "And the, um... <2400> the key systems are transport, energy, and public services."
- n05: "You rush. You fill the gaps. And when you stop to think, <300> Podium stays quiet. A presentation-mode turn policy, measured at zero premature interruptions <200> where default voice detection cut in every single time."

### S05 — Measured, not guessed (1:14–1:36)
Split-caption layout (S05/S06/S10 share it): the real `c09_scores` capture large in a browser
frame on the left (cursor, one gentle zoom on the bars), the spoken line as a caption on the
right — coach in teal, narrator in ink, one caption at a time. No rebuilt bars or metric strips.
Statement: **"Every number comes from code. The coach only phrases it."** Footnote:
"session s_20260909_160535 · computed from audio + word timestamps".
- SFX: `typing` under the metrics count-up; `pop` on each bar landing.
- c02: "Here's your scorecard. <300> Pace one fifty-two words a minute, a little fast on slide two. Ten pauses, the longest over three seconds. <300> Let's fix three things."
- n06: "Every number is computed by code, from the audio and the word timestamps. <300> Never estimated by a language model. The coach only says it out loud."

### S06 — Coaching by contrast: the hard voice problem (1:36–2:14)
Split-caption layout: the real improvement-flow capture (`c11`/`c12`) on the left; on the right
each played line as a caption — "you said" with fillers in red, "say it like this" in green, the
paced line with an inline 1.5 s pause glyph filling during the real gap (`<1500>` → measured
1480 ms). Statement: **"Your sentence. Cleaned. Then paced. Same voice."** Footnote:
"3 / 3 within ±450 ms · digits never spoken · RIME_EVIDENCE.md claim 1".
- SFX: `click` at the start of each of the three plays; `whoosh-short` on the gap highlight.
- c03: "Here's what you said."
- m04: "Smart cities matter because, um, they can basically make daily life faster, cleaner and, uh, easier."
- c04: "Same line, cleaned."
- c05: "Smart cities matter because they can make daily life faster, cleaner, and easier."
- c06: "Now with one pause, before the part that matters."
- c07: "Smart cities matter <1500> because they can make daily life faster, cleaner, and easier."
- n07: "This is the hard part. Podium plays your own sentence back. <300> Then the same line, cleaned. Then the cleaned line with a deliberate pause before the key phrase, in the same Rime voice. <300> The words barely change. Only the delivery does."

### S07 — Inside the call (2:14–2:36) · technical showcase
Minimal schematic: six nodes on one baseline, each a hand-drawn line icon + name + mono detail,
joined by hairlines with a single travelling packet dot: **mic** → **LiveKit Cloud** (India South)
→ **Deepgram nova-3** (STT, word timestamps) → **SessionGraph** (turn-gated · coach
`gemma-4-31b-it` · judge `gpt-5.4-mini`) → **Rime mistv3 · thunder** (`wss://users-ws.rime.ai/ws3`
· 24 kHz PCM) → **speaker**. A second hairline lane from SessionGraph to **Rime REST**
(`/v1/rime-tts` · WAV · V1/V2/V3). Icons draw on as the narration reaches them. Statement:
**"One duplex call. Two Rime roles."** Sub: "live coach voice over WebSocket · contrast clips over REST".
- SFX: `glitch-2` softened at the schematic reveal; `ping` as each node lights; `typing` on the endpoint labels.
- n08: "Under the hood it is one duplex call. Your microphone streams through LiveKit into Deepgram for words and timestamps. <300> A turn-gated session graph decides when the coach may speak. <300> And Rime speaks it, live, over a WebSocket at twenty-four kilohertz, <200> while the same model renders the contrast clips over REST."

### S08 — Drill, and an honest failure (2:36–3:02)
Drill UI: the word **services** large, a confidence ring at 69 % (amber). Sequence: Maya says
it → coach says it → Maya again; the ring sweeps to 96 % (teal) with a count-up. Then **trade**:
39 % → the ring drains to 0 % and turns red; the coach flags it instead of hiding it. Card slides
in: "Barge-in stop latency · P95 1.4 s · target 0.3 s · published as a miss · RIME_EVIDENCE.md claim 4".
Statement: **"When it doesn't work, Podium says so."**
- SFX: `click-soft` on each take; `chime` at 96 %; `error` (short, low) at 0 %; `whoosh` for the card.
- c08: "Let's drill one word. Say services."
- m05: "Serv-ices."
- c09: "Services."
- m06: "Services."
- c10: "Ninety-six percent. That landed. <300> Now, trade."
- m07: "Trade."
- c11: "That one didn't land, and I won't pretend it did. <300> It stays flagged for next time."
- n09: "Words the recognizer struggled with become a drill. <300> And when a take fails, the failure is shown, not hidden. <300> Same honesty in our evidence: interruption stop latency is one point four seconds at the ninety-fifth percentile, <200> against a three hundred millisecond target. Published as a miss."

### S09 — Ten skills, one ladder (3:02–3:18)
Grid of 10 skill tiles (dashboard tile styling) staggering in from a 3D tilt: Hook · Structure ·
Pacing · Pausing · Fillers · Vocal Variety · Storytelling · Slide Connection · Closing ·
Articulation. Maya's three improvements light their tiles (Pacing, Fillers, Pausing). Ladder
strip under it: Novice → Speaker → Presenter → TEDx with the marker moving one notch.
Statement: **"10 crafted skills. One ladder to TEDx."**
- SFX: `pop` per tile, `sparkle` when the three light up, `whoosh-short` on the ladder marker.
- n10: "Ten crafted speaking skills, each with a four-level ladder from Novice to TEDx. <300> Every improvement is tagged with the skill it trains, <200> and your mastery carries across sessions."

### S10 — Session 2: the delta (3:18–3:44)
Split-caption layout: the real `c17`/`c18` capture on the left (one zoom on the compact bars of
the wrap screen); on the right the session-2 line with its 1.2 s pause glyph, the coach's verdict,
then the narrator and the statement **"Same talk. One week later. Watch the delta."** with the
footnote "maya · session 1 → session 2 · 2·2·2·3·4 → 4·4·3·4·4" and a single thin sparkline.
No rebuilt scorecard bars or meter cards.
- SFX: `whoosh-cinematic` on the split, `pop` per delta, `sparkle` on the sparkline peak.
- m08: "Smart cities matter, <1200> because they can make daily life faster, cleaner, and easier."
- c12: "One forty-three words a minute. Good. <300> And the pause landed."
- n11: "Then you give the talk again. <300> The next scorecard shows the delta. The sparkline shows the climb. <300> Not a feeling of improvement. A measured one."

### S11 — Provider card (3:44–3:56)
Punch into the statusbar badge `rime · mistv3 · thunder` (rebuilt at full resolution, teal
underline), then the provider card: **Speech provider** — Rime · mistv3 · thunder · eng ·
WebSocket `wss://users-ws.rime.ai/ws3` · 24 kHz PCM · REST `/v1/rime-tts` · LiveKit Cloud
(India South) · STT Deepgram nova-3. Small line: "this narration: Rime mistv3 · astra / willow / thunder".
- SFX: `click` on the punch-in, `notification` on the card.
- n12: "Every voice you heard, <200> the coach, the presenter, and this narration, <200> is Rime. Mist v3, streaming over WebSocket, twenty-four kilohertz, through LiveKit. <300> The provider badge never leaves the screen."

### S12 — Close (3:56–4:05)
Orb collapses to a point, wordmark **PODIUM** slams in, tagline types under it:
"Rehearse out loud. Get coached in your own words." Hairline URL-less footer:
"DataForge × Rime hackathon · 2026".
- SFX: `impact-bass-2` on the wordmark, `chime` on the tagline, music resolves.
- n13: "Podium. <400> Rehearse out loud. <300> Get coached in your own words."

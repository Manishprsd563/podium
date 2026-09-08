# Delivery rubric

Score `delivery` 1-5 using ONLY the metrics object provided in the prompt.
Numbers are computed by code from timestamped speech-to-text output -- do not
re-estimate wpm, filler counts, or pause lengths from the transcript text
yourself. Cite the metric you used in `rubric_ref` as `delivery.md#<anchor>`.

## Pace bands (wpm)
- `< 110 wpm`: too slow -- sounds under-rehearsed or padded with dead air.
- `110-160 wpm`: conversational -- the target band, no penalty.
- `160-185 wpm`: brisk -- acceptable if pauses are still present at key points.
- `> 185 wpm`: rushed -- listeners lose the thread; flag as an improvement if
  it co-occurs with few rhetorical pauses.

## Filler rate
`fillers.per_min` (vocal: um, uh, mm, hmm, mhm, er):
- `< 2/min`: clean.
- `2-5/min`: normal, no penalty alone.
- `> 5/min`: score delivery no higher than 3; propose an improvement citing
  the worst-clustered `fillers.items` entries.

## Verbal-crutch rate
`crutches.per_min` (like, you know, basically, actually, sort of, kind of, i
mean, right, ah):
- `< 3/min`: fine.
- `3-6/min`: minor, mention only if delivery is already weak elsewhere.
- `> 6/min`: score delivery no higher than 3.

## Pause taxonomy
Read from `pauses`:
- `0.35-1.2 s` (`pauses.rhetorical`): GOOD when it sits right before a key
  point -- reward, do not flag as an improvement.
- `1.2-2.0 s` (a pause counted in `pauses` but absent from both
  `rhetorical` and `dead_air`): hesitation -- neutral, mention only if
  frequent (3+ occurrences).
- `>= 2.0 s` (`pauses.dead_air`): BAD -- the presenter lost their place. Any
  entry here is a strong candidate for an improvement.

## Monotony
`loudness.variance_db`:
- `< 2 dB`: monotone -- flag as an improvement (pair with a `v3_markup`
  emphasis suggestion via pause placement, since Podium cannot add pitch).
- `>= 2 dB`: normal variation, no penalty.

Score 5 only if pace is in-band, filler and crutch rates are both in the
clean band, there is no dead air, and loudness variance is normal. Deduct
one point per violated band, floor at 1.

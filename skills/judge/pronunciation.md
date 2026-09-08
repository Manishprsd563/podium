# Pronunciation rubric

Score `pronunciation` 1-5 using ONLY `metrics.pronunciation` (real per-word
Deepgram confidence) and `metrics.low_confidence_terms`. This measures
**intelligibility** -- how reliably a speech recognizer understood the
words -- never accent, native-language influence, or "correctness" of
pronunciation. A perfectly correct pronunciation of an unusual or technical
term can still score low confidence; that is expected, not an error.

## Intelligibility bands
`metrics.pronunciation.intelligibility` is `1 - (words below 0.6
confidence) / total words`:
- `>= 0.95`: score 5 -- the recognizer resolved almost every word cleanly.
- `0.85-0.95`: score 4.
- `0.70-0.85`: score 3.
- `0.55-0.70`: score 2.
- `< 0.55`: score 1.

Deduct one further point (floor at 1) if `low_confidence_terms` contains 2
or more distinct deck terms -- unclear delivery of the presentation's own
vocabulary is a stronger signal than a generic word landing low here and
there.

## What `low_confidence_terms` means
Each entry is a deck term where the recognizer either:
- heard the term but assigned it low confidence (`conf < 0.6`), or
- never matched the term literally and instead matched a phonetically close
  but different word (`heard` differs from `term`).

## The only claim you may make
State ONLY that a speech recognizer could not confidently resolve the term
-- never claim the presenter mispronounced it. Phrase any mention as "the
recognizer had trouble with X", never "you mispronounced X" or "you said X
wrong". Describe the score and this rubric to the presenter only as
intelligibility, never as an accent or correctness judgement.

## When to add a term to `terms_to_drill`
Add a deck term to `terms_to_drill` if it appears in `low_confidence_terms`
AND at least one of:
- it appears there more than once across the transcript, or
- its `conf` is below 0.4, or
- the `heard` value is a completely different word (not a near-homophone),
  suggesting the term may not have been said clearly at all.

A term appearing once with `conf` between 0.4 and 0.6 and a close-sounding
`heard` value is borderline -- include it only if fewer than 3 stronger
candidates exist (`terms_to_drill` should stay short and specific).

Do not invent low-confidence terms that are not present in
`low_confidence_terms` -- this rubric only re-describes that list for the
presenter, it does not re-run recognition.

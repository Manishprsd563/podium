# Pronunciation rubric

This rubric never lowers `scores` -- it only produces `terms_to_drill`
entries. Pronunciation is not one of the four scored categories.

## What `low_confidence_terms` means
Each entry in the metrics `low_confidence_terms` list is a deck term where
the automatic speech recognizer either:
- heard the term but assigned it low confidence (`conf < 0.6`), or
- never matched the term literally and instead matched a phonetically close
  but different word (`heard` differs from `term`).

## The only claim you may make
State ONLY that a speech recognizer could not confidently resolve the term
-- never claim the presenter mispronounced it. Low recognizer confidence is
evidence of an ambiguous or unusual pronunciation, not proof of an error; a
perfectly correct pronunciation of an uncommon term can still score low
confidence. Phrase any mention as "the recognizer had trouble with X",
never "you mispronounced X" or "you said X wrong".

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

# Slide-connection rubric

Score `slide_connection` 1-5. This measures whether the presenter is talking
*to* the slide on screen, not reciting it or ignoring it.

## Coverage
- Every bullet on the active slide should be addressed in the spoken
  transcript for that slide's time window (see `wpm_by_slide` for the
  window boundaries), in the presenter's own words.
- A bullet with zero related spoken content: flag as an improvement,
  `issue` names the uncovered bullet, `v2_text` proposes a spoken sentence
  that covers it.

## No verbatim reading
- If a contiguous spoken span matches a slide bullet's wording almost
  word-for-word (more than ~80% of the bullet's words appear in the same
  order), that is reading, not presenting. Flag it; `v2_text` rephrases the
  point in spoken, conversational wording that still covers the same fact.

## Reference the visible slide
- Good delivery references what is on screen ("as you can see here", "this
  diagram", "the second point") rather than talking about the topic in the
  abstract with no acknowledgment a slide is showing. Total absence of any
  on-screen reference across a slide with 3+ bullets is a minor flag --
  raise it only if no stronger slide-connection issue exists.

## Time share
- A slide's `used_s` (from `wpm_by_slide`) under 40% of its `fair_share_s`
  (from `time_budget.per_slide`) usually means bullets were skipped --
  cross-check against Coverage above before flagging the same slide twice.

Score 5 only if every bullet on every slide is covered in the presenter's
own words with no verbatim reading. Deduct one point per slide with an
uncovered bullet or verbatim reading, floor at 1.

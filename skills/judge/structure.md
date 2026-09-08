# Structure rubric

Score `structure` 1-5 from the TRANSCRIPT text and `time_budget`.

## Required parts
A rehearsed talk should contain, in this order:
1. **Hook** -- a first sentence that states why the topic matters or poses
   the question it answers, not just the topic name.
2. **Roadmap** -- a sentence naming what will be covered, e.g. "I'll cover
   X, then Y."
3. **Transitions** -- an explicit verbal signal at each slide or topic
   change ("now", "next", "moving to", "so that brings us to").
4. **Close** -- a final sentence that restates the takeaway, not a
   trail-off.

## Decision rule
- All 4 parts present: no structural improvement needed from this rubric.
- A part missing: flag it as ONE improvement (choose the single most
  damaging missing part if several are missing -- a missing close outranks a
  missing roadmap). `issue` names exactly which part is missing.
  - Missing hook: `v2_text` proposes an opening line for the actual topic.
  - Missing roadmap: `v2_text` proposes a one-sentence roadmap.
  - Missing transitions: cite the specific slide change with no verbal
    signal; `v2_text` proposes the transition phrase for that spot.
  - Missing close: `v2_text` proposes a one-sentence takeaway.

## Time allocation
Compare `time_budget.per_slide[*].used_s` against `fair_share_s`:
- Any slide over `1.5x` its fair share while another slide is under `0.5x`
  its fair share: flag as an improvement citing
  `structure.md#time-allocation` -- the talk is unbalanced, not just
  over or under overall.
- `time_budget.over_s` greater than 15% of `budget_s`: mention it in
  `summary`, not as a separate improvement, unless no other structural
  issue is stronger.

Score 5 only if all 4 parts are present and no slide deviates from its fair
share by more than 50%. Deduct one point per missing part (max 3) and one
more for a severe time imbalance, floor at 1.

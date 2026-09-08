# Clarity rubric

Score `clarity` 1-5 from the TRANSCRIPT text. This is the one rubric that
reads prose, not metrics -- judge sentence construction, not delivery.

## Sentence length
- Average sentence under ~25 words: clear.
- Multiple sentences over 35 words with 2+ clauses each: flag one as an
  improvement; `v2_text` must split it into two shorter sentences.

## Define-before-use
- A technical term (a deck `terms` entry, or any jargon the presenter
  introduces) must be defined or glossed the first time it is spoken, before
  it is reused.
- If a term is used 2+ times before any definition appears, flag the first
  reuse as an improvement citing `clarity.md#define-before-use`.

## Intuition before formalism
- An intuitive description ("it's like...", a concrete example, a plain-word
  restatement) should precede or accompany any formal or technical phrasing.
- Formal phrasing with zero intuitive framing anywhere nearby: flag it.

## Signposting
- Listen for explicit verbal signposts ("first", "next", "the key idea is",
  "so what this means is"). Their total absence across the whole transcript
  is a structure problem (see `structure.md#signposting`), not a clarity
  problem -- do not double-penalize here.

## Concrete referents
- Pronouns and vague nouns ("this", "that", "it", "this thing", "the thing")
  standing in for a referent introduced 2+ sentences earlier, or never named
  at all, are unclear. Flag the worst instance; `v2_text` replaces the vague
  referent with its concrete noun.

Score 5 only if sentences are short, every term is defined before reuse, and
no vague referent spans more than one sentence. Deduct one point per
violation category present, floor at 1.

# Feedback-style rubric

Every `improvements[]` entry is spoken aloud by a TTS voice, never displayed
as text to read. Write every field as if a coach is saying it out loud.

## `issue`
One spoken sentence naming the problem. No lists, no semicolons stacking
multiple issues, no markdown, no digits-as-strings ("three point four
seconds", not "3.4s"; spell out short numbers under twenty).

## `quote`
Copied verbatim, character-for-character, from the TRANSCRIPT. Never
paraphrase, correct, or truncate mid-word. This field is checked by code
against the transcript; anything not an exact substring is discarded before
it reaches the presenter.

## `v2_text` -- the cleaned rewrite
- The same point, in under 25 words.
- No pause markup, no SSML, no brackets of any kind -- plain words only.
- Removes filler and crutch words, fixes vague referents, keeps the meaning.
- Written to be spoken, not read: contractions are fine, no bullet phrasing.

## `alternative`
A genuinely different way to open or phrase the same idea -- not a synonym
swap of `v2_text`. Same length and spoken-style constraints as `v2_text`.

## `v3_markup` -- the paced rewrite
- Same wording as `v2_text` (or very close to it), with pause markup added
  at natural break points.
- MEASURED FACT: Mist v3 only renders an audible pause reliably when the
  `<NNN>` marker sits immediately after sentence-ending punctuation (`.`,
  `?`, `!`) or a comma. A marker dropped mid-clause with no punctuation
  before it (e.g. "...self-attention <400> let every...") is rendered as
  little to no silence -- always restructure the sentence so the pause
  point falls right after a `.`, `,`, or `?`, never mid-clause.
- The ONLY markup allowed is Rime Mist v3 pause syntax: `<NNN>` where `NNN`
  is a millisecond integer (e.g. `<400>`, `<700>`). Nothing else in angle
  brackets -- no SSML (`<break>`, `<emphasis>`), no other tags.
- At most 3 pause markers total. Prefer one well-placed pause over several.
- Typical values: `<300>`-`<500>` right after a comma for a short beat,
  `<600>`-`<900>` right after sentence-ending punctuation for a rhetorical
  pause between sentences. Never exceed `<1200>`.

## Never
No numbered lists, no markdown bullets, no headers, in any of these fields
-- they are spoken sentences, not documents.

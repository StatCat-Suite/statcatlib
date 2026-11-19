# Style review: label-lookup.md (revision 1)

Lens: style (statcatlib CLAUDE.md "Writing style"). Line numbers refer to
`docs/design/label-lookup.md`.

No blockers. Two major issues, nineteen minor.

## Conventional-commit syntax

1. **Major. Lines 362, 365, 367, 370, 372, 379, 381, 383, 386.**
   `feat!(labels)`, `feat!(scopes)`, `feat!(store)`, `feat!(query)`,
   `fix!(store)`, `feat!(search)` are invalid. Conventional Commits 1.0.0
   writes `<type>[(scope)][!]: <description>`, so the `!` goes after the scope.
   git-cliff would not read these headers as breaking, and pre-1.0 the minor
   bump depends on it. Rewrite as `feat(labels)!`, `feat(scopes)!`,
   `feat(store)!`, `feat(query)!`, `fix(store)!`, `feat(search)!`. D6 removes a
   public filter, so line 372 may fit `feat(store)!` or `refactor(store)!` better.

## Titles and headings

2. **Minor. Lines 358, 377.** "Part A: the lookup API" and "Part B: search"
   put a colon after a sequence label. Write "Part A, the lookup API" and
   "Part B, search", as line 40 does.
3. **Minor. Line 1.** "required tags" is vague. Rewrite: "Label lookup: return
   LocalisedString, require `lang`, add language pairs to search".

## Prose

4. **Major (colon reveal). Line 298.** "The index stops inventing English: a
   dataset is indexed under ...". Rewrite: "A dataset is indexed under the
   union of the tags its title, description, keywords, themes and dimension
   names hold (`store.py:3636`), and a code row under its code label's tags
   (`store.py:3812`). The index adds no `en` tag of its own."
5. **Minor. Line 118.** Negative form. Rewrite: "Every query uses only the
   tags the caller passes and the tags the stored map holds."
6. **Minor. Line 121.** Not a sentence. Rewrite: "Meet the acceptance
   criteria of GH-91 and of the suite design's Stages 4–5."
7. **Minor. Lines 6–7.** "No code is written yet." Rewrite: "Code starts
   after the EGM review round (D12)."
8. **Minor. Line 65.** "without the audit table and without msgspec."
   Rewrite: "The audit table and the msgspec dependency remain open."
9. **Minor. Line 137.** "decision ... decided" repeats itself. Rewrite: "The
   project owner made these decisions on 2026-11-18." Or delete the line.
10. **Minor. Lines 142–143.** "the gaps ride on the result" is figurative.
    Rewrite: "each result has a `gaps` attribute."
11. **Minor. Lines 158–159.** The D4 rationale is hard to parse. Rewrite:
    "Passing `lang=` to each terminal gives one way to supply a tag. A setter
    with no default would make every terminal raise until the setter runs,
    and would add a second way."
12. **Minor. Lines 183–185.** Rewrite in positive form: "statcatlib's writers
    serialise labels through `as_dict()`, and the CLI prints no labels, so only
    the JSON-order test needs msgspec."
13. **Minor. Line 211.** "No `lang` has a default." Rewrite: "Every `lang`
    parameter is required."
14. **Minor. Lines 106–107.** The list reads ambiguously, and "invented" is
    figurative (it also appears at lines 119 and 381–382). Rewrite: "Slice 4
    also covers three items: decision 9's last two sites (line 102), the
    hard-coded `{"en"}` search language (`store.py:3636, 3812`), and decision
    11's search side."
15. **Minor. Line 130.** "decision 2" could mean the RFC's decision 2. Write
    "D2's columns".
16. **Minor. Line 92.** "Open; 33". Write "Open; 33 sites".
17. **Minor. Line 90.** "Done in effect" hedges. Write "Done".
18. **Minor. Line 9.** "its comment" is vague. Name the comment and link it.
19. **Minor. Line 346.** "The hub session" is jargon. Write "A follow-up PR in
    the StatCat Suite hub applies these".
20. **Minor. Lines 353–354.** Fragment. Rewrite: "Once 0.21.0 is released,
    mark Stages 4 and 5 as built in statcatlib 0.21.0."
21. **Minor. Lines 452–453.** "already wrong" is subjective. Write "already
    fails at 0.20, because ...".

## Compliant

The document has no rhetorical questions, no hook clauses, no "not X, but Y"
contrasts, and no question headings. The colons at lines 217, 253 and 279
introduce detail.

# Structural review: label-lookup.md (revision 1)

Lens: structural, against the feature-spec standard and the sibling designs
`language-tags.md`, `inventory-history-stage-2.md` and `release-job.md`.

Verdict: not ready. 3 blockers, 8 major, 7 minor. The heading levels nest
correctly and the Markdown is well formed.

## Blockers

- **B1. No "Alternatives considered" section.** The hub
  (`localised-string.md:388-417`) and the siblings (`release-job.md:1233`,
  `inventory-history-stage-2.md:1825`) have one. D1, D2, D4, D5, D7 and D9 make
  new choices, and only D4 names a rejected option, inline. **Fix:** add the
  section before Work order, with one bullet per rejected option tagged with its
  decision.
- **B2. No "Risks and mitigations" section.** The design states none of these
  risks:
  - every product needs a re-index before `Product.view` opens it;
  - behaviour changes silently: `None` in `Labels`, spatial facet keys, and
    the meaning of dataset `Hit.lang`;
  - `MissingTranslationError` is a `KeyError`, and the hub rejected exactly
    that for `InvalidTagError` (hub D7) because `except KeyError` and
    `Mapping.get` swallow it;
  - the cost of the `CASE`/`COALESCE` SQL over long tag tuples;
  - a stale pairs fingerprint;
  - statcat-serve stays pinned at 0.18.

  **Fix:** add the section, giving each risk a mitigation that names a test or
  a Work order step.
- **B3. No acceptance map.** Goal 6 cites the acceptance criteria of GH-91 and
  hub Stages 4–5 but lists none. The gaps:
  - Hub Stage 5's "ingest refuses an empty `lang`" is "Done in effect", with
    no test and no amendment.
  - The hub requires `tests/unit/test_language_tags.py`
    (`localised-string.md:581-583`). This design uses the flat
    `tests/test_language_tags.py` and proposes no hub amendment for it.
  - GH-91's items are not linked to tests.

  **Fix:** add an "Acceptance map" table with columns for criterion, step and
  test. Add hub amendments for the test path and for the empty-`lang`
  criterion.

## Major

- **M1. Decision numbers clash.** Line 340's "(D7)" and "(D8)" mean hub
  decisions. The amendment bullets "D9:" and "D4:" are hub numbers. **Fix:**
  use local prefixes such as L1–L14 or E-numbers, and write "hub Dn" and "RFC
  decision n" (see `inventory-history-stage-2.md:49-59`).
- **M2. Process items are listed as decisions.** D10, D11, D12 and D14 repeat
  the Status line, the Overview, Work order step 2 and the hub amendments.
  **Fix:** move D12 to Status and D14 to a "Land order" note, and state D10
  and D11 once each.
- **M3. Decisions are duplicated in the API tables.** D1–D9 each repeat a
  table row, and `Langs` is defined twice. **Fix:** keep the reason in one
  place and cite it from the other.
- **M4. Tests do not cover every Goal.** No test covers:
  - search raising `TypeError` for a sequence;
  - `get`, `agent_labels`, `concept_labels`, `spatial_labels`, `code_map`
    and `SchemeScope.labels` returning tags;
  - `label` and `get` actually raising;
  - D8's optional description returning `None`;
  - `RowList` and `ValueList` gaps from `values` and `sort_by`;
  - the removal of D6's agent filter and of the English-first fallback;
  - step 11, which stops indexing an invented `en`;
  - D9's `ConceptHit` reading its columns;
  - `preference()` and `language_counts()`.

  **Fix:** map each Goal to its tests and add the missing ones.
- **M5. Goal 1 and the enumeration test contradict the API tables.**
  `select`, `scalars` and `sort_by` keep `lang: Langs | None`. **Fix:**
  narrow Goal 1 and list these exceptions in `_calls`.
- **M6. No Handoff section, and "Downstream impact" comes last.** **Fix:**
  rename it Handoff and place it after Cross-repo contract. Give the hub one
  item covering the amendments and the hub-doc fixes, and mark statcat-serve
  and sdmxlib-rest "no action".
- **M7. Migration sits under Stored formats but covers caller code too.**
  **Fix:** promote it to "Breaking changes and migration", with "Stored data"
  and "Caller code" subsections.
- **M8. The design does not name its drift from the suite design.** Line
  numbers have moved, "about 27" defaults is now 33, and `lance.py:942` is
  gone. **Fix:** add a "Drift from the suite design" subsection that lists
  each hub claim and its amendment.

## Minor

- m1 Add "Open questions: none", and say where each RFC open item is settled.
- m2 Add an Errors table with columns for error, raised by and condition.
- m3 Add a one-paragraph Problem section.
- m4 Rename Work order "Implementation plan", with Tests, Docs and Size as
  peers.
- m5 Link the suite design by its published site URL, as the siblings do.
- m6 Fill the empty "Before" cells of `preference` and `language_counts` with
  "none".
- m7 D13 has no section of its own. Add "Ingest (Part A)".

# Comprehension review: label-lookup.md (revision 1)

Lens: comprehension, read as a newcomer. Context documents:
`docs/design/language-tags.md` (the RFC) and the hub's
`docs/design/localised-string.md`.

Verdict: needs revision. 3 blockers, 13 major, 13 minor.

## Blockers

- **B1. New public types and Label lookups.** The design gives no `Gap`
  `entity` or `field` value for any call. For `batch_labels`, `entity` could be
  the code ID or the URN. It could be the pair or the URN for the
  `CodelistsScope` variant. For `agent_labels` it is presumably the agent ID.
  `spatial_labels` and D5's keyless row have no candidate. For `code_map`,
  dimension and code are both candidates. For `QueryBuilder.rows`, a row may
  have no ID column. The design does not say whether `held` can be empty or
  whether `langs` is normalised. **Fix:** add a table of call, `entity`,
  `field` and example `Gap` for every `Labels`, `RowList` and `ValueList` call
  and for `facets("spatial")`, and state that `langs` is the `as_langs` tuple.
- **B2. Search, D7, Stored formats.** The design does not define a search row's
  tag or a dataset's "best-scoring row". A document can mix tags, such as an
  `en` description with an `en-t-fr` title. The reader cannot tell whether a
  dataset has one row per tag, or what `CodeHit.lang` is when the code label
  and the dimension label differ in tag. **Fix:** add a "Search rows"
  subsection with the row rule per hit type: one row per stored tag, the
  document built from that tag's text, and the row's `lang` equal to that tag.
  Include an open.canada.ca example.
- **B3. Throughout.** Three numbering schemes collide: "decisions 4 to 12" is
  the RFC, D1–D14 is this design, and "hub D13" is the hub. The Cross-repo
  contract's unprefixed "(D7)" and "(D8)" mean hub D7 and D8. The amendment
  bullets "D9:" and "D4:" are hub numbers. Non-goals' "decision 2" is
  ambiguous. **Fix:** prefix every outside reference ("RFC decision 11", "hub
  D7"), and define the three schemes once under Terms.

## Major

- **M1. Overview.** The RFC is named before it is linked. "RFC finding 10, 11,
  13" are not glossed. **Fix:** link it at first use and add a half-sentence
  gloss for each cited item.
- **M2. Issue line, State today.** "Stage N" and "slice N" are undefined. The
  hub notes that Stage 5 is narrower than slice 4 (hub lines 605–607), and the
  design relies on this without stating it. **Fix:** add Terms entries for both.
- **M3. Terms.** MAP, `und` and `zxx` are undefined. **Fix:** define MAP as
  DuckDB `MAP(VARCHAR, VARCHAR)`, noting the 26 multilingual columns with
  `extras` excluded. Define `und` as untagged CKAN text and `zxx` as an
  identifier stored as text.
- **M4. Conventions vs Projections.** "No `lang` has a default" contradicts
  `select`, `scalars` and `sort_by` taking `lang: Langs | None`. **Fix:** state
  the exception, the error raised, and when it is raised.
- **M5. Conventions.** The design does not state whether de-duplication
  happens after normalisation, or whether empty text answers. `pick` and
  `COALESCE` both stop at `''`, but the State-today row about `{"en": ""}` does
  not say what the new API returns. **Fix:** normalise, then de-duplicate; a
  held `''` answers and is not a gap.
- **M6. New public types.** "SQL order for whole-table maps" is undefined and
  nondeterministic without an `ORDER BY`. **Fix:** name the calls and give an
  explicit key order.
- **M7. Label lookups.** `CodesScope.get` and `ConceptsScope.get` return
  `Hit`s. The design does not say what `lang`, `document` and
  `dimension_label` mean outside search, or which label raises. **Fix:** give
  the field values for `get`.
- **M8. Label lookups.** `CodelistScope.label` changes from `-> str | None` to
  "unknown codelist raises `LookupError`". If it returns `None` today, this is
  an unlisted break. **Fix:** give today's behaviour per call and add any
  change to Migration.
- **M9. Projections.** `row()` and `value()` drop gaps, and `value()`'s `None`
  means two things. The `scalars` container is not stated. **Fix:** decide and
  state each. Give `scalars -> ValueList`.
- **M10. Search.** The design does not say what `search(lang="en-t-fr")`
  returns: every `en`-table hit, or only `en-t-fr` rows. It does not say what
  a tag with no table returns. **Fix:** state both.
- **M11. Stored formats vs Tests.** The pairs shapes disagree: `{alternate:
  primary}` in one place and `{"en": ["en-t-fr"]}` in the other. The
  fingerprint's input is undefined, and GH-3 is not glossed. **Fix:** use one
  shape and define the fingerprint.
- **M12. Migration.** "Carried vectors", working copy, seal and release are
  undefined. The design never says plainly that every released edition is
  unreadable under 0.21.0 until it is rebuilt. **Fix:** define the terms and
  state that consequence.
- **M13. Section order.** State today comes first and uses terms before they
  are defined. Conventions follows Decisions, although the decisions rely on
  it. **Fix:** order the sections Overview, Terms, Goals and Non-goals,
  Conventions, Decisions, API. Move State today to an appendix.

## Minor

- m1 "enriched" is undefined. Say "the label-joined rows".
- m2 "terminal", "scope" and "tables" are jargon. Define them.
- m3 "audit hook", EGM, `just ci` and "Phase 6" need a gloss or a link.
- m4 "GH-88's contract" is undefined.
- m5 The design has no `MissingTranslationError` message example for a tuple,
  and does not say how the subclass calls `MissingTagError`'s constructor.
- m6 `KeyError` is overloaded: an unknown ID and `MissingTranslationError`
  are both `KeyError`s.
- m7 `preference(lang)` is positional, against the keyword-only convention.
  "Tag order" is ambiguous. The result for an alternate or an unknown tag is
  not given.
- m8 The `language_counts.texts` column is not defined.
- m9 `has_spatial` does not say whether it compares by equality or
  containment, or when it consults the concept.
- m10 D13 and its test give no input tag. Use `{"en": ""}`, and reconcile it
  with the State-today row.
- m11 The `_map_to_istring` non-goal is opaque. Say what the cost is.
- m12 "needs no macro" needs the RFC's reason: read-only connections.
- m13 `type Langs = …` needs Python 3.12. Confirm `requires-python`.

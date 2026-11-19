# Implementation-readiness review: label-lookup.md (revision 1)

Lens: implementation readiness. Base: statcatlib `2e5055a`; sdmxlib `istring.py`
and `tests/unit/test_language_tags.py` read in the shared checkout. The GH-91
issue body was not read, because no network was available.

Verdict: not ready. 1 blocker, 10 major, 5 minor, 1 nit.

## What `just ci` runs

`just ci` runs `check`, the tests and the docs build (`justfile:66,118`;
`pyrefly.toml:47`):

- `check`: the index guard, ruff check and format, pydoclint, the prose
  check, the `Any` ratchet, and pyrefly over `src` and `tests`;
- the tests: `pytest -m unit`, `-m integration` and `tests/stores`;
- `mkdocs build --strict`.

No doctest or doc-example test exists, so docs that lag the code do not break
the gate. pyrefly does type-check every test caller, including
`test_live_sdmx.py`, and pydoclint requires the `Raises:` sections to match.
No `index.json` fixture is checked in.
`tests/test_index_version_guard.py` hard-codes version 3 at lines 63 and 112.

## Blocker

**1. Migration: the upgrade path does not exist.** `WorkingCopy._start` calls
`_Store._over(..., search_dir=...)` (`working_copy.py:1179-1188`), and that
call runs `_refuse_unreadable_index` (`store.py:2908`).
`tests/test_index_version_guard.py:105-120`
(`test_a_working_copy_carrying_a_refused_index_is_refused`) asserts this
refusal. **Fix:** in `_over(writable=True)`, accept a carried v3 index for
`carried_vectors()` only, or drop v3 `search/` and re-embed. Name the change
to that test and to the lines with the version literals.

## Major

**2. Work order: commit 5 depends on commit 13's types.** `get()` must return
tagged labels in commit 5, but the `CodeHit` and `ConceptHit` fields change in
commit 13 (`store.py:1289-1317`, built at `2453-2464` and `2576-2583`).
**Fix:** move the field types into commit 5, or move both `get` changes into
Part B. State what `document` holds for a `get()` result.

**3. Tests: the enumeration test contradicts the API.**

- (a) `select`, `scalars` and `sort_by` keep `lang=None`
  (`store.py:2072, 2091, 2009`).
- (b) `LanceIndex._for` swallows `InvalidTagError` and returns `None`
  (`lance.py:1206-1211, 1505-1507`), so `fr_CA` gives `[]`.
- (c) A walk over the public API would find `Hit`, `Gap` and `preference`,
  and would miss `_FilteredCodelistsScope` and `MapCondition`/`MapFilter`.
- (d) The test can pass only after commit 10, although GH-88 closes with
  Part A.

**Fix:** give the module filter, the exemptions and the hand-written cases,
make `_for` raise, and keep a temporary allow-list for the search calls that
is removed in commit 10.

**4. Work order: tests are not assigned to commits.** With pyrefly over
`tests/`, each commit must update every caller, for example
`test_shared_connection_idle.py:62`, which calls `.label("en")` positionally.
**Fix:** list the test files each commit updates and adds.

**5. Conventions: the `Gap` contents are undefined per call.** The design
gives no rule for `entity` and `field` per API. It does not say whether a
missing optional column is a gap, or whether `row()` and `value()` raise or
stay silent. RFC decision 8 lists facet labels as producing gaps, but
`facets()` returns `dict[str, dict[str, int]]`, which has no room for them.
**Fix:** add a table of API, `entity`, `field` and gap condition, and name
where facet gaps go.

**6. Projections: the SQL helper is underspecified.** One `params` list
cannot serve two expressions used in different clauses. Positional `?`
parameters cannot mix with `$name` (`store.py:2441`). `CodelistScope.label`
reads `sdmx.localized_text` rows (`scopes.py:895-897`), not a MAP. **Fix:**
give the exact signature, returning text SQL with its params and lang SQL
with its params, and specify a separate pick for `localized_text`.

**7. Stored formats: the pairs fingerprint and the stale check are
unspecified.** `check_bundle` no longer exists (`docs/upgrading.md:553`); the
check is `bundle.check_catalog` and `_check_search` (`bundle.py:223-253, 285`).
The design leaves these open:

- the input bytes the fingerprint hashes;
- whether `index.json` records the pairs when there are none;
- which method warns;
- `check_catalog_no_index` skips `_check_search`;
- how `LanceIndex` receives the pairs, given that `_by_language` is a
  staticmethod (`lance.py:1294`).

**Fix:** specify each item.

**8. Commit 12: "`_table_name` calls `tag_alias`" breaks region tags.**
`_table_name` receives keys that are already aliases (`lance.py:1297, 1317,
1374`). `tag_alias("fr_ca")` normalises its input, and `normalize_tag` refuses
`_`. **Fix:** group by the normalised tag and alias it once, inside
`_table_name`.

**9. Commit 10: dataset hits from the best row need a new return shape.**
`bm25_search` returns `[(identifier, score)]`, deduplicated by ID
(`lance.py:1477, 1518`), so the row's `lang` is lost. **Fix:** return
`(id, score, lang)` and keep the `lang` of the highest score. Apply the same
change to the vector and hybrid paths.

**10. `language_counts` is underspecified.** `schema.py` has 27 MAP columns,
and the count of 26 holds only with `dataset.extras` excluded
(`schema.py:122`). The design does not give:

- the column list;
- the `table` and `column` format;
- the column for `sdmx.localized_text`;
- the row order;
- the return type.

It also excludes `sdmx.concept.name`, although `get` reads it
(`store.py:2558`). **Fix:** list the columns or define a constant for them.

**11. New public types: module locations are missing.** The design gives no
module and no `Labels` constructor. sdmxlib's
`MissingTagError.__init__(langs, held, /)` passes those two values to
`KeyError` and overrides `__str__` (`istring.py:120-131`). A subclass with a
four-argument `__init__` must call `super().__init__(langs, held)` and
override `__str__`. It will not unpickle unless it handles `args` or defines
`__reduce__`. **Fix:** name the modules and constructors, and state the
pickling rule.

## Minor

- 12 The unknown-entity rule contradicts itself: `CodelistScope.label`
  returns `None` today (`scopes.py:904-906`), and the table says it raises
  `LookupError`.
- 13 Several tag-tuple details are open:
  - whether `CodelistsScope.filter` matches any tag or the first held one;
  - `preference(lang)` is positional, and its result for an alternate and
    its "tag order" are undefined;
  - `Labels` has no `ORDER BY`;
  - the design does not say what `batch_labels` does with duplicate IDs.
- 14 Commit 12 writes a v4 index with a `text` column, and commit 13 renames
  the column without a version bump. Say this, or merge the rename into 12.
- 15 About 18 test files are affected:
  - `test_scopes`, `test_filters`, `test_query_builder`, `test_store`;
  - `test_edition_view`, `test_hybrid_search`, `test_code_search`,
    `test_code_search_extensions`;
  - `test_codelist_policy_and_concepts`, `test_reference_metadata_search`,
    `test_structural_queries`;
  - `test_sources_sdmx`, `test_sources_ckan`, `test_lang_normalisation`,
    `test_shared_connection_idle`;
  - `test_index_version_guard`, `test_live_sdmx`, `test_embedding_reuse`.

  The work also includes `payload.text` to `.document`, row keys, and version
  literals.
- 16 Two acceptance-map differences from the hub: the hub names
  `tests/unit/test_language_tags.py`, and the design adds no test for the
  empty-`lang` refusal at ingest.

## Nit

- 17 pydoclint requires `Raises:` sections in commits 4 to 6.

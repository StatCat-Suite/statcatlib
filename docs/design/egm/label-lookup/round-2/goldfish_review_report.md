# Goldfish Review Report, round 2

**Target:** `docs/design/label-lookup.md` revision 2 (`feat/gh-91-localised-string`
at `7279c05`; lines 1–849), read against statcatlib `2e5055a` (v0.20.0).

**Lenses:** `egm-codebase-details`, `egm-design`,
`egm-implementation-readiness`, one instance each. Per-lens reports:
`codebase-details.md`, `design.md`, `implementation-readiness.md` in this
directory.

**Scope:** what revision 2 added or changed: LL-D15, LL-D5's SQL-only hash
key, Gaps, Search rows, SQL helpers, `UnknownCodeError`, the 15-commit plan,
and the review-response table. Round-1 findings the table resolves and owner
decisions LL-D1 to LL-D15 are not re-raised; findings below state
consequences or factual errors in them.

**Limits:** sdmxlib was checked at 0.88.0 only; reviewers had no git access
for v0.89.0. No sdmxlib claim changed between rounds, and none of the
findings depends on one. DuckDB `to_json` behaviour was assessed from
knowledge, not by running it. The synthesizer confirmed RD-M1
(`working_copy.py:773-786, 1606-1614`), RD-M2's stale `_version`
(`lance.py:1086, 1092`; no write in `_write_manifest`) and RD-M6
(`store.py:2399-2421`) against the source.

**Counts:** 0 Blocker, 12 Major, 19 Minor.

**Verdict:** ready after the listed fixes. The design's structure and
decisions hold. The majors are spec gaps and plan errors that each have a
concrete fix. After the fixes, a short check of the LL-D15, LL-D5 and
Search rows text is enough; another full round is not needed.

## Prioritized Findings (Blockers -> Major -> Nits)

### RD-M1. LL-D15: a resumed working copy seals a version 3 index - Severity: Major
* **Reviewer Lens:** `design`, `codebase-details`, `implementation-readiness`
* **Affected Scope:** LL-D15 bullet 2 (lines 258–259); Stored formats (line 504); commit 11
* **Discrepancy Description**: the seal keeps the index when
  `_meta.index_built_from` equals `_tables_fingerprint` and `index.json`
  exists (`working_copy.py:1606-1614`). `_start` clears `index_built_from`
  only for a new working copy (`working_copy.py:773-786`). A working copy
  indexed under 0.20, resumed under 0.21 and sealed with no table change
  keeps its v3 index, and `_search_section` records `index_version: 3`
  (`bundle.py:110`). "The seal always rebuilds it as version 4" is false.
* **Rationale & Recommendation**: add `store._lance.version ==
  _MANIFEST_VERSION` to the seal's `current` condition and state it under
  LL-D15. Add a commit 11 test: resume a v3-indexed working copy with an
  unchanged fingerprint, seal, and check that the edition records 4.

### RD-M2. LL-D15: the search guard has no defined home, version source or ordering - Severity: Major
* **Reviewer Lens:** `design`, `implementation-readiness`, `codebase-details`
* **Affected Scope:** LL-D15 bullet 3 (lines 260–262); Errors (line 304); commit 11
* **Discrepancy Description**: today one predicate,
  `LanceIndex.unreadable_version` (`lance.py:1122-1144`), serves
  `_refuse_unreadable_index` (`store.py:242`, called at 2908) and
  `_check_search` (`bundle.py:241`). Every reader LL-D15 names goes through
  one of these two (table in `codebase-details.md`), so accepting `{3, 4}`
  there covers the open path in one place. The search-only rule is not placed:
  * `IncompatibleCatalogError` is in `store.py:167`, and `lance.py` may not
    import `store` (`lance.py:198-199`), so `_for` cannot raise it.
  * `LanceIndex._version` is set only in `__init__` (`lance.py:1086, 1092`);
    `_write_manifest` (`lance.py:1160-1202`) never updates it. After
    `build_index` over a carried v3 index the object still reports 3
    (`store.py:4539-4609`), and a freshly built index reports `None`.
  * Entry points return empty early when `_lance is None`
    (`store.py:2365, 2497, 2629, 2724, 6062, 6424`) or when no row passes
    the SQL filter (6431); a guard placed after these returns gives an empty
    result on v3.
  * A missed path fails silently: v4 code reads `document` from a v3 table,
    and `_LanguageIndex.bm25` / `.vector` swallow every exception
    (`lance.py:1021-1028, 1042-1049`).
* **Rationale & Recommendation**: keep one version table in `lance.py`
  (`_MANIFEST_VERSION = 4`, `_READABLE_VERSIONS = {3, 4}`, searchable =
  4). `_write_manifest` sets `self._version`. Choose one guard: in `_for`,
  raising an error that is `IncompatibleCatalogError` (moved to a leaf
  module and re-exported), or one `_Store` helper called at every entry point
  before the early returns. State the v3 behaviour of `table_for`,
  `table_for_key`, `describe`, `count_rows`, `index_layout`,
  `check_index_compatible`, and of search without lancedb. Name the code
  sites (`lance.py:1140`, `store.py:2908`, `bundle.py:241`,
  `working_copy.py:1607-1612`) and state that `embargo`, `release`,
  `upload`, `admin`, `catalog`, `diagnostics` and `cli` do not change. Add
  one parametrised test over every search entry point on a v3 fixture,
  including a filtered `QueryBuilder.search` that matches nothing, and a test
  that search succeeds after rebuilding over a carried v3 index.

### RD-M3. LL-D15: no real version 3 fixture, a wrong plan for `test_index_version_guard`, and four untested readers - Severity: Major
* **Reviewer Lens:** `implementation-readiness`, `codebase-details`
* **Affected Scope:** commit 11 (line 675); Review response B1 (line 807)
* **Discrepancy Description**: today's tests fake a version by rewriting
  `index.json` (`tests/test_index_version_guard.py:32-39`). That cannot
  serve `check_catalog`, which checks the manifest digests and the section's
  `index_version` (`bundle.py:250-252`), and it never produces a `text`-column
  table, so the carry claim goes untested.
  `test_a_working_copy_carrying_a_refused_index_is_refused`
  (lines 105–120) rewrites to version **2** (line 112), which LL-D15 still
  refuses; once the version is 4 the replace matches nothing and the test
  fails. The plan says it "becomes a test that it opens", which is wrong.
  Line 63 asserts `== 3`; line 69 calls `search` without `lang` and breaks
  at commit 9, where the file is not listed. Of LL-D15's six readers,
  `EmbargoReader.view`, the fold's `_reference_check`
  (`release.py:1174-1180`), upload `_verify` (`upload.py:231-236`) and
  `verify_catalog` (`diagnostics.py:315-316`) have no test.
* **Rationale & Recommendation**: specify how a v3 edition is built: seal
  under `monkeypatch.setattr(lance, "_MANIFEST_VERSION", 3)` with a v3 table
  schema helper, or a fixture catalog sealed by 0.20.0. Keep the existing
  test as a refusal test (update the literal) and add a separate "v3 opens"
  test. List the file under commit 9. Add one test per named reader.

### RD-M4. LL-D5: the canonical JSON depends on DuckDB internals and session settings - Severity: Major
* **Reviewer Lens:** `design`, `implementation-readiness`
* **Affected Scope:** LL-D5 (lines 186–198); SQL helpers (line 413); commit 7
* **Discrepancy Description**: "exactly as DuckDB writes
  `to_json(list_sort(map_entries(label)))`" leaves open:
  * sort direction and NULL order, which `list_sort` takes from the
    `default_order` and `default_null_order` session settings when not
    passed;
  * escaping of control characters, `/`, U+2028/2029 and astral characters,
    set by the bundled yyjson flags and not a documented DuckDB contract, so
    a DuckDB upgrade can change keys;
  * a map entry with a NULL value.

  The pinned fixture ("accents and a quote") covers none of these, nor
  prefix-ordered tags (`en`, `en-t-fr`, `fr`, `fr-ca`). Downstream callers
  store facet keys (line 545), so a change breaks them without an error.
  Struct key order (`key` then `value`) is fixed by `map_entries` and is not
  at risk.
* **Rationale & Recommendation**: define the key independently of a JSON
  writer, then implement it in SQL. Either (a) a Python reference,
  `json.dumps([{"key": k, "value": v} for k, v in sorted(m.items()) if v is not None], ensure_ascii=False, separators=(",", ":"))`
  UTF-8 encoded with code-point order, or (b) an escape-free length-prefixed
  form (`<tag>=<octet_length(text)>:<text>` per entry, in byte order of
  tag). Pass `'ASC'` explicitly to `list_sort`. Give the pinned fixture map
  and expected hex as literals. Test the SQL against the reference over a
  corpus with the characters above.

### RD-M5. LL-D5: keys split coverages that used to merge, change between editions, and collapse empty labels - Severity: Major
* **Reviewer Lens:** `design`, `codebase-details`, `implementation-readiness`
* **Affected Scope:** LL-D5 (lines 196–197); Gaps (line 395); Breaking changes (lines 543–545); Risks (line 652)
* **Discrepancy Description**: "Two coverages with the same label map share
  a key, as they shared an English key before" is wrong on the facts. The
  old key `COALESCE(uri, label['en'], label['und'])` (`store.py:1206`)
  merged coverages by English text; under the hash `{en: "Ontario"}` and
  `{en: "Ontario", fr: "Ontario"}` get two keys and `facets("spatial")`
  splits one displayed label into two counts. The key also changes whenever
  a later edition adds or edits any translation. "Every coverage has a key"
  (line 395) is false: a coverage with no `uri` and a NULL label
  (`schema.py:225-235`) has none, and CKAN stores an all-empty spatial dict
  as `{}` (`sources/ckan.py:384-401, 511-513`; `store.py:734-738`), which
  hashes to one key shared by every such coverage, with no label. Today
  those coverages have a NULL key and are dropped.
* **Rationale & Recommendation**: correct both sentences. Define the key as
  NULL when `label` is NULL or `cardinality(label) = 0`, and keep `WHERE key
  IS NOT NULL`. List the split and the per-edition instability under the
  silent behaviour changes and in `docs/upgrading.md`. Add tests: two
  coverages that differ only in `fr` give two keys; an empty map has no key.
  LL-D5 itself stays as decided.

### RD-M6. Search rows: deduplicating codes by `code_urn` collapses hits across datasets - Severity: Major
* **Reviewer Lens:** `design`, `codebase-details`, `implementation-readiness`
* **Affected Scope:** Search rows, Duplicates (lines 470–478); commit 9
* **Discrepancy Description**: a code row's `id` is the dataset identifier,
  one row per (dataset, dimension, code, tag) (`store.py:3665-3667,
  3846-3855`), so "two rows ... with one `id`" already holds before pairs.
  bm25 and vector `CodesScope.find` return one hit per row today
  (`store.py:2408-2419`); only hybrid merges, by `code_urn` plus `lang`
  (`store.py:2399-2406`). Deduplicating `bm25_search_rows` / `search_rows`
  by `code_urn` keeps one dataset per code, so `dataset_ids=` filters and
  per-dataset UIs lose hits. In commit 9, before pairs exist, this collapse
  is the dedup's only effect.
* **Rationale & Recommendation**: deduplicate by the row identity minus the
  tag: code `(id, dimension_id, code_urn or code_id)`, concept
  `concept_urn`, reference metadata `(id, attribute_path, chunk)` or `id`.
  Move the dedup into commit 11 with its test: one code used by two datasets
  under `en` and `en-t-fr` gives two hits. If cross-dataset collapse is
  wanted, list it as a silent behaviour change.

### RD-M7. Search rows: deduplicating after the LanceDB limit shortens pages and miscounts `total` - Severity: Major
* **Reviewer Lens:** `design`, `implementation-readiness`
* **Affected Scope:** Search rows, Duplicates; `CodesScope.find`, `ConceptsScope.find`
* **Discrepancy Description**: `bm25_search` and `search` oversample before
  `_dedupe_by_id` (`lance.py:1509, 1626`); `bm25_search_rows` and
  `search_rows` pass `limit` straight to LanceDB (`lance.py:1572-1585,
  1662-1673`), with `limit + offset` from the scopes (`store.py:2377`).
  Removing paired rows afterwards returns fewer than `limit` hits while more
  exist; `total = len(hits)` (`store.py:2421`) is then wrong and `offset`
  paging skips entities.
* **Rationale & Recommendation**: oversample the `*_rows` methods by at
  least 1 + the primary's alternate count, apply `limit` after
  deduplication, document `total`, and add a paging test over paired rows.

### RD-M8. SQL helpers: `rank` in `arg_min` is undefined, and source-first order breaks the first-held-tag rule - Severity: Major
* **Reviewer Lens:** `design`
* **Affected Scope:** SQL helpers (lines 419–422); Conventions (line 139); Gaps (line 389)
* **Discrepancy Description**: "a URI key takes its text from the concept
  first, then from its coverages" with `lang=("fr", "en")`, a concept
  holding only `en` and a coverage holding `fr`, answers `en`; Conventions
  says the first held tag answers. Today's single-tag
  `COALESCE(c.label[?], ds.label[?])` (`store.py:6214`) never met this case.
  `{text, lang}` is not DuckDB struct syntax, and NULL texts are not
  filtered.
* **Rationale & Recommendation**: define `rank = (tag position, 0 for the
  concept, 1 for a coverage)`, or state source-first as an exception to
  Conventions. Write the expression in DuckDB syntax (`struct_pack` or
  `{'text': …, 'lang': …}`) with `WHERE text IS NOT NULL`. Test the
  `("fr", "en")` case.

### RD-M9. `UnknownCodeError` cannot describe a `CodelistsScope.batch_labels` failure - Severity: Major
* **Reviewer Lens:** `design`
* **Affected Scope:** New public types (line 279); Errors (line 303); Label lookups (line 316); Conventions (line 152)
* **Discrepancy Description**: `UnknownCodeError(codelist_urn, codes)`
  names one codelist. `CodelistsScope.batch_labels(pairs)` spans many
  (`scopes.py:755-804`), so "naming every unknown code ID" cannot hold
  there.
* **Rationale & Recommendation**: make the payload
  `missing: tuple[tuple[str, str], ...]` (codelist URN, code ID); drop
  `codelist_urn` or set it only when one codelist is involved; update
  `__str__`, `__reduce__` and the commit 5 tests.

### RD-M10. Hybrid: two rules for which row's `lang` is kept, and `_rrf_rank`'s tag has no consumer - Severity: Major
* **Reviewer Lens:** `design`
* **Affected Scope:** Search rows, Dataset hits (lines 479–481); Search table (line 436)
* **Discrepancy Description**: dataset hits keep the row from the ranking
  that places the dataset higher, BM25 on a tie. Code, concept and
  reference-metadata hits go through `_rrf_merge_rows`, which keeps the BM25
  row whenever one exists (`setdefault`, `store.py:1819-1831`).
  `_rrf_rank`'s only caller, `_execute_search` (`store.py:6444-6454`),
  builds a `_QueryResult` of identifiers and scores (`store.py:1250-1260`),
  so the new tag is dropped.
* **Rationale & Recommendation**: state one rule for every hit kind and
  apply it in `_rrf_merge_rows`. Say where `QueryBuilder.search` and
  `hybrid_search` surface the tag, or leave `_rrf_rank` unchanged and drop
  its row from the table. Test both modes.

### RD-M11. The "Tests updated" column misses files and misplaces two - Severity: Major
* **Reviewer Lens:** `codebase-details`, `implementation-readiness`
* **Affected Scope:** Implementation plan table (lines 663–679); Review response M16
* **Discrepancy Description**: with pyrefly over `tests/`, a commit that
  omits a broken test file fails `just ci`.
  * Commit 6 lists `test_structural_queries` (only `code_map`,
    `tests/test_structural_queries.py:153-188`, changed in commit 7) and
    `test_codelist_policy_and_concepts` (no commit 6 API).
  * Commit 7 misses `test_structural_queries`.
  * Commit 8 misses `test_lang_normalisation.py:45-51` and
    `test_hybrid_search.py:432-435` (`LocalisedString` does not equal `str`).
  * Commit 9 misses `test_store.py:170-208`, `test_sources_sdmx.py:294-312`,
    `test_codelist_policy_and_concepts.py:259, 314, 327, 339, 350, 362`,
    `test_sources_ckan.py:687`, `test_query_builder.py:24`,
    `test_index_version_guard.py:69`.
  * Commit 11 misses `test_hybrid_search` (42 `SearchRow` dicts keyed
    `"text"`; `add_bulk(rows)` without the new required `pairs=`;
    `TestSearchRowMatchesSchema` at 2109-2154), `test_code_search_extensions`
    (37 reads of `row["text"]`), `test_codelist_policy_and_concepts.py:268`.
* **Rationale & Recommendation**: regenerate the column per commit from a
  grep of each changed name, using the lists above, and recount "about 22
  files".

### RD-M12. The migration loop fails on real catalogs - Severity: Major
* **Reviewer Lens:** `codebase-details`
* **Affected Scope:** Breaking changes and migration, "Re-index each product" (lines 527–539)
* **Discrepancy Description**: `working_copy()` defaults to
  `lifecycle="revisable"` (`catalog.py:1183`), and `_start` raises
  `InventoryRuleError(rule="lifecycle")` for a `final` product
  (`working_copy.py:1145-1150`). `catalog.products()` lists only products
  with a live edition (`catalog.py:891-894`), so a product whose newest
  edition is embargoed is skipped and stays v3. The prose says "seal and
  release", but the loop has no release step for an embargoed catalog. An
  existing working copy raises `WorkingCopyExistsError`.
* **Rationale & Recommendation**: pass each product's recorded lifecycle,
  iterate every product the head records, handle an existing working copy,
  and add the release step. Or point to `statcat build-index` plus seal
  (`cli/__init__.py:300-455`), which takes `--lifecycle`.

### RD-m1. Commit 9 makes `_for` raise one commit before commit 10 fixes `_check_language` - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`, `codebase-details`
* **Affected Scope:** commits 9–10
* **Discrepancy Description**: `_check_language` calls `table_for` with
  alias keys such as `fr_ca` (`bundle.py:258-280`); `normalize_tag` refuses
  `_`; `_for` swallows it today (`lance.py:1206-1211`). Between commits 9 and
  10, `check_catalog` raises for any edition with a region or alternate tag.
  No test covers this, so CI stays green, and the single merge means the
  state is never released.
* **Rationale & Recommendation**: swap commits 9 and 10, or move "`_for`
  raises" into commit 10.

### RD-m2. `_lang_sql.py` location creates an import cycle; `SPATIAL_KEY_SQL` is fixed to one alias - Severity: Nit
* **Reviewer Lens:** `codebase-details`, `implementation-readiness`, `design`
* **Affected Scope:** SQL helpers (lines 399–413)
* **Discrepancy Description**: `query.py` imports only sdmxlib and attrs
  (`query.py:35-40`); importing `statcatlib.store._lang_sql` runs
  `store/__init__.py` (`store/__init__.py:3-40`), and `store.py:57` imports
  `statcatlib.query`. `has_spatial_key` compiles inside `ExistsFilter` with
  generated aliases (`query.py:928-932`); facets use `ds`
  (`store.py:6160, 6807`). `labels.RowList` needs `Row` from `store/row.py`.
* **Rationale & Recommendation**: put the module at `statcatlib/_lang_sql.py`
  (or require function-level imports), state that `labels` and `_lang_sql`
  import nothing from `store.store` or `store.scopes`, and make the key SQL
  `spatial_key_sql(alias: str) -> str`.

### RD-m3. Enumeration test: discovery rules open, private defaults uncovered - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** `tests/test_language_tags.py` (lines 687–700); Acceptance map (line 721)
* **Discrepancy Description**: the callable discovery rules, the shape of
  `_calls`, the fixture and the no-lancedb behaviour are open. The test
  cannot see the 11 private defaults (line 785), such as `search_lang="en"`
  (`store.py:1890, 6260, 6507`) and `_SpatialFilter.lang` (`query.py:923`).
* **Rationale & Recommendation**: spell out the discovery rules, and check
  that the collected set equals the keys of `_calls`. Add an AST check that
  no `lang`, `langs` or `*_lang` parameter or attrs field has a string
  default.

### RD-m4. Hit field renames are not assigned to a commit - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** Search table (lines 429–431); commits 9 and 11
* **Discrepancy Description**: `get()` builds hits with `document` in
  commit 9, so `CodeHit` and `ConceptHit` rename there, reading the `text`
  column until commit 11. `ReferenceMetadataHit.text` has no commit. In
  commit 9, `ConceptHit.name` and `description` can only come from splitting
  `text` (`store.py:1420-1424`).
* **Rationale & Recommendation**: assign all three renames to commit 9 and
  state the interim rule.

### RD-m5. Commit 10: `_langs` and `index.json` keys must stay aliases - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** Search rows, Tables (lines 458–463); commit 10
* **Discrepancy Description**: table names are unchanged (`lance.py:218`),
  so v3 editions and fixtures still open. `_langs` and `languages` are keyed
  by alias (`lance.py:1096-1099, 1193`). Keying them by tag makes `add()`
  recreate tables (`lance.py:1311-1318`) and changes the `index.json` keys,
  against line 501.
* **Rationale & Recommendation**: state that `_by_language` returns
  alias → (tag, rows), so `_langs` stays keyed by alias.

### RD-m6. Commit 5 adds `_lang_sql.py` with no caller, and its `!` marks no breaking change - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** commit 5
* **Discrepancy Description**: CI passes (`justfile:118` runs no coverage),
  but nothing calls `pick_map` or `localized_map` until commit 6, or
  `SPATIAL_KEY_SQL` until commit 7. Commit 5 only adds names, so its `!`
  marks no breaking change.
* **Rationale & Recommendation**: add each helper with its first caller,
  and drop the `!`.

### RD-m7. No reference page for `statcatlib.labels` - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** Docs (lines 731–745)
* **Discrepancy Description**: the docs build stays green before commit 15.
  The prose mentions at `docs/api/store.md:732, 1029, 1037, 1085` do not
  fail `--strict`. Every public module has a reference page, and the new
  module has none.
* **Rationale & Recommendation**: add `docs/reference/labels.md` with
  `::: statcatlib.labels` and a nav entry.

### RD-m8. Commit 11 bundles format-independent row-tag rules - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** commit 11
* **Discrepancy Description**: these rules do not depend on the format and
  could land in a commit of their own: no invented `en`
  (`store.py:3635-3636`), code rows tagged from the code label
  (`store.py:3810-3812`), and `zxx` excluded.
* **Rationale & Recommendation**: split them into a commit before commit 11.

### RD-m9. `describe()`, `index_layout` and `languages` list alternates on a v3 index - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Stored formats (line 501)
* **Discrepancy Description**: line 501 says these list primary tags only.
  On a v3 index they list the alternates too (`lance.py:1248-1268`,
  `store.py:3018-3020`).
* **Rationale & Recommendation**: limit line 501's claim to version 4.

### RD-m10. `add_bulk` with no rows writes a v4 manifest over carried v3 tables - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** LL-D15 bullet 2
* **Discrepancy Description**: `add_bulk` with no rows writes the manifest
  with the carried tables (`lance.py:1362-1368`). Until `drop_carried` runs,
  the manifest says v4 over tables that hold `text`, and `wc.view()` search
  returns empty with no error.
* **Rationale & Recommendation**: drop the carried v3 tables before writing
  the manifest, or write no manifest.

### RD-m11. `LanceIndex.add()` ignores language pairs - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Search table (line 437)
* **Discrepancy Description**: only `add_bulk` gains `pairs=`. `add()`
  (`lance.py:1300-1322`) would create an `en_t_fr` table in a v4 index.
* **Rationale & Recommendation**: route `add()` through the
  `language_pairs` in `index.json`.

### RD-m12. Unknown codelist and no-`sdmx`-schema cases are underspecified - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Errors (line 303); Label lookups (line 314)
* **Discrepancy Description**: `label` returns `None` for an unknown
  codelist, while `batch_labels` raises. Telling an unknown codelist from one
  with no name rows needs an `sdmx.artefact` lookup, which the current query
  lacks (`scopes.py:895-897`). `CodelistScope.rows` with no `sdmx` schema is
  unspecified.
* **Rationale & Recommendation**: add one sentence per case, and add the
  existence check.

### RD-m13. `ConceptsScope.get` picks name and description separately - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Label lookups (line 328); `get()` hits (lines 349–352)
* **Discrepancy Description**: with a tag tuple, the name and the
  description of one hit can answer in different tags.
* **Rationale & Recommendation**: state that each field is picked
  separately and that `.lang` tells them apart.

### RD-m14. Producers on 0.20 cannot start a working copy after a v4 seal - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Breaking changes, Readers first (lines 518–521)
* **Discrepancy Description**: 0.20 refuses a version 4 index when it opens
  a working copy (`store.py:2906-2908`). "Readers first" covers readers only.
* **Rationale & Recommendation**: add to Migration that every producer of a
  product upgrades before or with its re-index.

### RD-m15. Code rows lose dataset context across a pair - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** Search rows, Tags and Text (lines 445–455)
* **Discrepancy Description**: a code held under `en` in a dataset titled
  only under `en-t-fr` gets a document with an empty title
  (`store.py:3817, 3823`).
* **Rationale & Recommendation**: fill the title and dimension context from
  the primary's alternates in `preference` order, or state the loss.

### RD-m16. "0.20 refuses a version 4 index" and "check its layout" are too broad - Severity: Nit
* **Reviewer Lens:** `codebase-details`
* **Affected Scope:** LL-D15 bullet 1; Readers first (line 518)
* **Discrepancy Description**: on local disk, `Product.view` and
  `EmbargoReader` run only the version guard (`store.py:2908`); the layout is
  checked by `check_catalog` (`bundle.py:253-263`). Without lancedb, upload
  and the fold use `check_catalog_no_index` (`bundle.py:323-341`), which
  never reads `index.json`. The rollout order is still safe.
* **Rationale & Recommendation**: narrow both sentences.

### RD-m17. `_refuse_unreadable_index`'s message is not updated - Severity: Nit
* **Reviewer Lens:** `design`
* **Affected Scope:** LL-D15; commit 11
* **Discrepancy Description**: the message tells users to delete `search/`
  (`store.py:245-249`). Under LL-D15 the fix is to re-index.
* **Rationale & Recommendation**: name the message change in commit 11.

### RD-m18. Wrong citation for `carried_vectors` - Severity: Nit
* **Reviewer Lens:** `codebase-details`
* **Affected Scope:** Stored formats, `text_sha256` row (line 503)
* **Discrepancy Description**: `lance.py:880-896` is
  `_LanguageIndex.hashed_vectors`; `carried_vectors` is at
  `lance.py:1274-1289`. The claim itself holds.
* **Rationale & Recommendation**: cite both locations.

### RD-m19. No fixture or expected value for the pinned hash - Severity: Nit
* **Reviewer Lens:** `implementation-readiness`
* **Affected Scope:** LL-D5 (lines 193–194); commit 7
* **Discrepancy Description**: the test can be written only once the
  fixture map and the expected value are fixed. RD-M4 covers the spec.
* **Rationale & Recommendation**: give the literal map, for example
  `{"fr": "Québec \"Nord\"", "en": "Quebec"}`, and the expected hex. Also
  compute the value from the reference in the test.

## LL-D15 coverage summary

Every reader of `search/index.json` and every caller of the guard was
traced. `Product.view`, `EmbargoReader.view`, `WorkingCopy._start` and
`statcat build-index` go through `store.py:2908`. verify-catalog
(`diagnostics.py:316`), the release fold (`release.py:1180`), upload
(`upload.py:236`), blob `check_on_open` (`catalog.py:289-296`) and
`publish_check` go through `bundle.py:241`. LL-D15's bullets cover all of
them. Readers that do not check the version (`lance.py:1090-1099, 1257`,
`bundle.py:104-110`, `working_copy.py:1610`) need no change beyond RD-M1.
`admin.py`, `gc.py`, `blob.py` and `cli/` read no index. Carried vectors
read only `text_sha256` and `embedding` (`lance.py:880-896`), so a v3 table
carries safely. "Only search raises" can be built without a second code
path that drifts, provided RD-M2's version table, guard placement and
`_version` update are specified.

## Review-response verification (round-1 blockers and majors)

| Finding | Status | Gap |
|---|---|---|
| B1 | Partly | the seal keeps a resumed v3 index (RD-M1); the plan for `test_index_version_guard` is wrong (RD-M3); the refusal message is unchanged (RD-m17) |
| M1, M3, M4, M7, M10, M11, M13, M14, M17, M18, M19 | Present as claimed | M7's test details are in RD-m3; M10's consequences are in RD-M4 and RD-M5 |
| M2 | Present | the rename commit is unassigned (RD-m4) |
| M5 | Partly | wrong dedup key for codes (RD-M6); no oversampling (RD-M7) |
| M6 | Partly | split hybrid rule; the tag from `_rrf_rank` has no consumer (RD-M10) |
| M8 | Present | the commit order opens RD-m1 |
| M9 | Partly | `add()` ignores pairs (RD-m11) |
| M12 | Partly | `rank` is undefined (RD-M8) |
| M15 | Partly | `UnknownCodeError` has the wrong shape for pairs (RD-M9); schema cases (RD-m12) |
| M16 | Partly | test lists incomplete (RD-M11) |

No round-1 blocker or major is claimed and entirely missing. Seven are
partly applied: B1, M5, M6, M9, M12, M15 and M16.

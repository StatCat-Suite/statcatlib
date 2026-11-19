# Design lens, round 2: label-lookup.md revision 2

**Target:** `docs/design/label-lookup.md` at `7279c05`, against statcatlib
`2e5055a`. Lens: `egm-design`. Counts: 0 blocker, 9 major, 9 minor.

## Major

### MJ1. LL-D15: a resumed working copy seals a version 3 index under 0.21
- **Section:** LL-D15 bullet 2 (lines 258–259); Stored formats (line 504); commit 11.
- **Evidence:** seal step 3 keeps the index when `_meta.index_built_from`
  equals `_tables_fingerprint` and `index.json` exists
  (`working_copy.py:1606-1614`); `_start` clears `index_built_from` only for a
  new working copy, and `resume=True` keeps it (`working_copy.py:773-786`). A
  working copy indexed under 0.20, resumed under 0.21 and sealed with no table
  change keeps its v3 index and records `index_version: 3`.
- **Fix:** make "current" in the seal require the index version to equal the
  written version; state it under LL-D15; add a commit 11 test that resumes a
  v3-indexed working copy and checks that the seal writes v4.

### MJ2. LL-D15: two guards that can drift, and a miss that fails silently
- **Section:** LL-D15 (lines 251–262); Errors (line 304); commit 11.
- **Evidence:** today one predicate, `unreadable_version` (`lance.py:1140`),
  serves `_refuse_unreadable_index` (`store.py:242`) and `_check_search`
  (`bundle.py:241`). Revision 2 adds a "readable but not searchable" rule
  without saying where it lives. Search entry points (`store.py:2365, 2497,
  2629, 2724, 6062`, `_execute_search` 6424, `_rrf_rank` 3555) each return
  empty early when `store._lance is None`, and `_execute_search` also returns
  early when no row passes the SQL filter (6431); a guard after those returns
  gives an empty result on v3. A missed path reads `document` from a v3 table
  holding `text`, and `_LanguageIndex.bm25` / `.vector` swallow every
  exception (`lance.py:1027, 1048`), so search returns `[]` with no error.
  `_for` cannot raise `IncompatibleCatalogError` directly: the class is in
  `store.py:167` and `lance.py` must not import `store` (`lance.py:198-199`).
- **Fix:** one version table in `lance.py` (`_MANIFEST_VERSION = 4`,
  `_READABLE_VERSIONS = {3, 4}`, `_SEARCHABLE_VERSION = 4`); `_for()` is the
  one search guard and raises an error that is, or is translated once to,
  `IncompatibleCatalogError` (move the class to a leaf module, re-exported).
  State the v3 behaviour of `table_for`, `table_for_key`, `describe`,
  `count_rows`, and of search without lancedb. One parametrised test calls
  every search entry point on a v3 fixture, including a filtered
  `QueryBuilder.search` that matches nothing.

### MJ3. LL-D5: the canonical form depends on DuckDB internals and settings
- **Section:** LL-D5 (lines 186–198); SQL helpers (line 413); commit 7.
- **Evidence:** `list_sort` without an explicit order takes `default_order`
  and `default_null_order` from session settings. `to_json` escaping
  (control characters, `/`, U+2028/2029, astral characters) follows the
  bundled yyjson flags, which DuckDB does not document as a contract. A map
  entry with a NULL value is unspecified. The pinned fixture ("accents and a
  quote") exercises none of these, nor several tags sorted by prefix (`en`,
  `en-t-fr`, `fr`, `fr-ca`). Downstream callers store facet keys (line 545).
- **Fix:** define the key independently of a JSON writer, then implement it
  in SQL. Either (a) a Python reference,
  `sha256(json.dumps([{"key": k, "value": v} for k, v in sorted(m.items()) if v is not None], ensure_ascii=False, separators=(",", ":")).encode())`
  with code-point order, or (b) a length-prefixed form with no escaping
  (`<tag>=<octet_length(text)>:<text>` per entry in byte order of tag). Pass
  `'ASC'` to `list_sort` (or `string_agg(... ORDER BY key)`); define NULL and
  empty labels; test SQL against the reference over a corpus with the
  characters above, besides the pinned hash.

### MJ4. LL-D5: the shared-key claim is wrong, and keys change between editions
- **Section:** LL-D5 (lines 196–197); Breaking changes (lines 543–545); Risks (line 652).
- **Evidence:** the old key `COALESCE(uri, label['en'], label['und'])`
  (`store.py:1206`) merged coverages with the same English text whatever
  their other tags. Under the hash, `{en: "Ontario"}` and `{en: "Ontario",
  fr: "Ontario"}` get two keys, so `facets("spatial")` splits one displayed
  label into two counts. A key also changes when a later edition adds or
  edits any translation; the English key survived adding `fr`.
- **Fix:** correct the sentence; list both effects under the silent behaviour
  changes and in `docs/upgrading.md`; add a test of two coverages that differ
  only in `fr`. This states LL-D5's consequences and does not reopen it.

### MJ5. Search rows: deduplicating codes by `code_urn` collapses datasets
- **Section:** Search rows, Duplicates (lines 470–478).
- **Evidence:** a code row is one per (dataset, dimension, code, tag), `id`
  the dataset identifier (`store.py:3796-3865`). BM25 and vector mode return
  every row today (`store.py:2408-2419`), so a code used by five datasets
  gives five `CodeHit`s; only hybrid keys by `code_urn` (2399-2406). The
  design's key keeps one dataset per code in `bm25_search_rows` and
  `search_rows`.
- **Fix:** deduplicate by the row identity minus the tag: code
  `(id, dimension_id, code_urn or code_id)`, concept `concept_urn`,
  reference metadata `(id, attribute_path, chunk)`. Test one code in two
  datasets under `en` and `en-t-fr`: two hits.

### MJ6. Search rows: deduplication after the LanceDB limit shrinks pages
- **Section:** Search rows, Duplicates; `CodesScope.find`, `ConceptsScope.find`.
- **Evidence:** `bm25_search` and `search` oversample before `_dedupe_by_id`
  (`lance.py:1509, 1626`); `bm25_search_rows` and `search_rows` pass `limit`
  straight to LanceDB (`lance.py:1572-1585, 1662-1673`), and scopes pass
  `limit + offset` (`store.py:2377`). Removing paired duplicates afterwards
  returns fewer than `limit` hits, `total = len(hits)` (2421) is wrong, and
  `offset` paging skips entities.
- **Fix:** oversample the `*_rows` methods by at least 1 + the primary's
  alternate count, apply `limit` after deduplication, document `total`, and
  add a paging test over paired rows.

### MJ7. SQL helpers: `rank` in `arg_min` is undefined and source-major order breaks the first-held-tag rule
- **Section:** SQL helpers (lines 419–422); Conventions (line 139); Gaps (line 389).
- **Evidence:** "concept first, then coverages" with `lang=("fr", "en")`, a
  concept holding only `en` and a coverage holding `fr` answers `en`;
  Conventions says the first held tag answers. Today's single-tag
  `COALESCE(c.label[?], ds.label[?])` (`store.py:6214`) never met the case.
  `{text, lang}` is not DuckDB struct syntax; NULL texts must be filtered.
- **Fix:** define `rank = (tag position, 0 for concept / 1 for coverage)`,
  or state source-major as an exception; write real DuckDB syntax
  (`struct_pack` or `{'text': …, 'lang': …}`) with `WHERE text IS NOT NULL`;
  test the `("fr", "en")` case.

### MJ8. `UnknownCodeError` cannot describe a `CodelistsScope.batch_labels` failure
- **Section:** New public types (line 279); Errors (line 303); Label lookups (line 316).
- **Evidence:** `UnknownCodeError(codelist_urn, codes)` names one codelist;
  `CodelistsScope.batch_labels(pairs)` spans many (`scopes.py:755-804`), so
  "naming every unknown code ID" (line 152) is impossible there.
- **Fix:** payload `missing: tuple[tuple[str, str], ...]` (codelist URN, code
  ID); drop `codelist_urn` or set it only when one codelist is involved;
  update `__str__`, `__reduce__` and commit 5 tests.

### MJ9. Hybrid: two rules for which row's `lang` is kept, and `_rrf_rank`'s tag has no consumer
- **Section:** Search rows, Dataset hits (lines 479–481); Search table (line 436).
- **Evidence:** datasets keep the higher-ranked ranking's row, BM25 on a tie;
  codes, concepts and reference metadata go through `_rrf_merge_rows`, whose
  representative is always the BM25 row when one exists (`setdefault`,
  `store.py:1819-1831`). `_rrf_rank`'s only caller, `_execute_search`
  (6444-6454), builds a `_QueryResult` of identifiers and scores (1250-1260),
  so the new tag is dropped.
- **Fix:** one rule for all hit kinds, applied in `_rrf_merge_rows` too; say
  where `QueryBuilder.search` / `hybrid_search` surface the tag, or leave
  `_rrf_rank` unchanged and drop its table row. Test both modes.

## Minor

- **mn1. "Every coverage has a key" is false** (line 395). A CKAN empty-dict
  item stores `SpatialCoverage(label=None)` with no `uri`
  (`sources/ckan.py:511-513`), and bbox-only coverages are accepted
  (`schema.py:225-235`); the hash of NULL is NULL, and every empty map hashes
  `[]` to one shared key. Fix: such a coverage has no key and no facet; keep
  `WHERE key IS NOT NULL`.
- **mn2. `SPATIAL_KEY_SQL` is fixed to one alias** (line 413). Facets use
  `ds` (`store.py:6160, 6807`); `has_spatial_key` compiles in `ExistsFilter`
  with generated aliases (`query.py:928-932`). Fix:
  `spatial_key_sql(alias: str) -> str`; note that a `sha256:` filter hashes
  every `dataset_spatial` row per query.
- **mn3. `index_layout`, `describe()`, `languages` on a v3 index** list
  alternates (`lance.py:1248-1268`, `store.py:3018-3020`); line 501 says
  primary tags only. Fix: scope the claim to version 4.
- **mn4. `add_bulk` with no rows writes a v4 manifest over carried v3 tables**
  (`lance.py:1362-1368`); `wc.view()` search then returns empty silently.
  Fix: drop carried v3 tables first, or write no manifest.
- **mn5. `LanceIndex.add()` ignores pairs**; only `add_bulk` gains `pairs=`
  (line 437), and `add()` (`lance.py:1300-1322`) would create an `en_t_fr`
  table in a v4 index. Fix: route through `index.json`'s `language_pairs`.
- **mn6. Unknown codelist and no-`sdmx`-schema cases** (lines 303, 314):
  `label` returns `None`, `batch_labels` raises; telling "unknown codelist"
  from "no name rows" needs an `sdmx.artefact` lookup the query lacks
  (`scopes.py:895-897`); `CodelistScope.rows` without `sdmx` is unspecified.
  Fix: one sentence per case and an existence check.
- **mn7. `ConceptsScope.get` picks name and description separately**, so
  they can answer in different tags. Fix: say so.
- **mn8. Producers on 0.20 after the first v4 seal** cannot start a working
  copy (`store.py:2906-2908`). Fix: add "upgrade every producer of a product
  before or with its re-index" to Migration.
- **mn9. Code rows lose dataset context across a pair:** a code held under
  `en` in a dataset titled only under `en-t-fr` gets an empty title
  (`store.py:3817, 3823`). Fix: fill context from the primary's alternates,
  or state the loss.

## Review-response check

| Finding | Status |
|---|---|
| B1 | Partly: resumed working copy seals v3 (MJ1); `_refuse_unreadable_index`'s message telling users to delete `search/` (`store.py:245-249`) is not named for change |
| M5 | Partly: wrong dedup key for codes, no oversampling (MJ5, MJ6) |
| M6 | Partly: split hybrid rule; `_rrf_rank` tag unused (MJ9) |
| M9 | Partly: `add()` ignores pairs (mn5) |
| M10 | Applied; consequences unstated (MJ3, MJ4, mn1) |
| M12 | Partly: `rank` undefined (MJ7) |
| M15 | Partly: `UnknownCodeError` shape (MJ8); schema case (mn6) |
| other blockers and majors | Reflected as claimed |

# Codebase-details lens, round 2: label-lookup.md revision 2

**Target:** `docs/design/label-lookup.md` at `7279c05`, against statcatlib
`2e5055a`. Lens: `egm-codebase-details`. Counts: 0 blocker, 4 major, 5
minor, 2 nit. sdmxlib was checked at 0.88.0 only (`pyproject.toml:40`,
`msgspec>=0.22.0`, agrees with LL-D10); v0.89.0 was not readable without git.
Paths are relative to `src/statcatlib/` or `tests/` at `2e5055a`.

## Readers of `search/index.json` and callers of the version guard

The version check is one predicate, `LanceIndex.unreadable_version`
(`store/lance.py:1122-1144`, against `_MANIFEST_VERSION` at `lance.py:74`),
with two callers.

| Site | Reached from | LL-D15 bullet |
|---|---|---|
| `store/store.py:242` `_refuse_unreadable_index`, called at `store.py:2908` in `_Store._over` | `Product.view` and `EmbargoReader.view` via `catalog._open_view` (`catalog.py:378-386`, `embargo.py:215`); `WorkingCopy._start` (`working_copy.py:1179-1188`), also used by `statcat build-index` (`cli/__init__.py:282-286, 455`) | 1 and 2 |
| `bundle.py:241` `_check_search` in `check_catalog` | verify-catalog (`diagnostics.py:316`); release fold `_reference_check` (`release.py:1180`); upload `_verify` (`upload.py:236`); `check_on_open` over blob (`catalog.py:289-296`); `working_copy.publish_check` | 1 |

Readers that do not check the version: `LanceIndex.__init__` manifest parse
(`lance.py:1090-1099`), `describe` (`lance.py:1257`), `_search_section`
copying `version` into the `statcat` section (`bundle.py:104-110`), the
seal's existence check (`working_copy.py:1610`). `check_catalog_no_index`
(`bundle.py:323-341`) never reads `index.json`. `admin.py`, `gc.py` and
`blob.py` have no index reader (`admin.py:251` is an unrelated
`IncompatibleCatalogError`). Every reader is covered by LL-D15.

**One guard is feasible:** accept `{3, 4}` in the predicate at
`lance.py:1140` (both callers follow), and put one search-time check in
`LanceIndex._for` (`lance.py:1206`), which covers `bm25_search`,
`bm25_search_rows`, `search`, `search_rows` and `table_for`. Three points
need stating: `IncompatibleCatalogError` lives in `store.py:167` and
`lance.py` may not import `store` (`lance.py:198-199`); `_check_language`
calls `table_for` (`bundle.py:279`), so it must switch to commit 10's
`table_for_key` before commit 11's guard lands; and without lancedb the
`_Store` entry points return empty before reaching `_for` (`store.py:2365,
2497, 2629, 2724, 6062, 6424`). The guard is required: `_LanguageIndex.bm25`
and `.vector` swallow every exception (`lance.py:1021-1028, 1042-1049`).

**Carry and reuse:** `carried_vectors` (`lance.py:1274-1289`) calls
`hashed_vectors` (`lance.py:880-896`), which selects only `text_sha256` and
`embedding`. `add_bulk` builds fresh tables (`lance.py:1389-1394`); `_Store`
never calls `upsert`/`add`; `_tables_fingerprint` hashes DuckDB tables only
(`working_copy.py:816-847`). A v3 `text` column breaks no carry path.

## Major

### M1. Commit 11's plan for `test_index_version_guard` is wrong
- **Section:** Implementation plan, row 11; Review response B1.
- **Evidence:** `test_a_working_copy_carrying_a_refused_index_is_refused`
  (`tests/test_index_version_guard.py:105-120`) rewrites the index to version
  2 (line 112), which LL-D15 still refuses. Once the version is 4, line 112's
  replace matches nothing and the test fails. Line 63 asserts `== 3`; line 69
  calls `view.search(...)` with no `lang`, which breaks at commit 9, where the
  file is not listed.
- **Fix:** keep that test as a refusal test with the literal updated; add a
  separate test that a working copy carrying a real v3 index opens (a fixture
  table with `text` and no concept columns); list the file under commit 9.

### M2. "Tests updated" leaves out files that break and lists two in the wrong commit
- **Section:** Implementation plan table.
- **Evidence:**
  - Commit 6 lists `test_structural_queries` (only calls `code_map`,
    `tests/test_structural_queries.py:153-188`, changed in commit 7) and
    `test_codelist_policy_and_concepts` (no commit 6 API).
  - Commit 8 misses `test_lang_normalisation.py:51` (`scalars(...) ==
    ["Population totale"]`) and `test_hybrid_search.py:432-435`; a
    `LocalisedString` does not equal a `str`.
  - Commit 9 misses `test_store.py:170-208`, `test_sources_sdmx.py:294-312`,
    `test_codelist_policy_and_concepts.py:259, 314, 327, 339, 350, 362`,
    `test_sources_ckan.py:687`, `test_query_builder.py:24`,
    `test_index_version_guard.py:69`.
  - Commit 11 misses `test_hybrid_search` (42 `SearchRow` dicts keyed
    `"text"`, every `idx.add_bulk(rows)` without the new required `pairs=`,
    `TestSearchRowMatchesSchema` at 2109-2154),
    `test_code_search_extensions` (37 reads of `row["text"]`, e.g. 187-226,
    302), `test_codelist_policy_and_concepts.py:268`.
- **Fix:** 6: `test_scopes`, `test_shared_connection_idle`,
  `test_lang_normalisation`, `test_live_sdmx`. 7: add
  `test_structural_queries`. 8: add `test_lang_normalisation`,
  `test_hybrid_search`. 9: add `test_store`, `test_sources_sdmx`,
  `test_codelist_policy_and_concepts`, `test_sources_ckan`,
  `test_query_builder`, `test_index_version_guard`. 11: add
  `test_hybrid_search`, `test_code_search_extensions`,
  `test_codelist_policy_and_concepts`. Recount "about 22 files".

### M3. The migration loop fails on real catalogs
- **Section:** Breaking changes and migration, "Re-index each product".
- **Evidence:** `working_copy()` defaults to `lifecycle="revisable"`
  (`catalog.py:1183`), and `WorkingCopy._start` raises
  `InventoryRuleError(rule="lifecycle")` for a `final` product
  (`working_copy.py:1145-1150`). `catalog.products()` lists only products
  with a live edition (`catalog.py:891-894`), so a product whose newest
  edition is embargoed is skipped and stays v3. With embargo on, the seal
  records the edition as embargoed and the loop has no release step. An
  existing working copy raises `WorkingCopyExistsError`.
- **Fix:** pass each product's recorded lifecycle; iterate every product the
  head records; handle an existing working copy; add the release step; or
  point to `statcat build-index` plus seal (`cli/__init__.py:300-455`).

### M4. Entity deduplication changes `CodesScope.find` results unstated
- **Section:** Search rows, Duplicates.
- **Evidence:** a code row's `id` is the dataset identifier (`store.py:3846`),
  so all code rows of a dataset already share an `id`. bm25 and vector modes
  return one hit per row today (`lance.py:1572-1585`, `store.py:2408-2419`);
  a code in three datasets gives three hits. Only hybrid merges, and its key
  includes `lang` (`store.py:2399-2406`). Deduplicating by `code_urn`
  collapses hits across datasets and changes `limit` and `total`.
- **Fix:** key codes by `(id, code_urn)`; or, if `code_urn` alone is meant,
  list it as a silent behaviour change and test one code in two datasets.

## Minor

- **Mi1. The seal can keep a v3 index.** The keep condition
  (`working_copy.py:1606-1614`) ignores the version, and a resumed working
  copy keeps `index_built_from` (`working_copy.py:784-786`), so
  `index_version: 3` is sealed (`bundle.py:110`). Fix: require
  `store._lance.version == 4` (`lance.py:1146-1149`); test a resumed working
  copy.
- **Mi2. `store/_lang_sql.py` and `query.py` form an import cycle.**
  `query.py` imports only `sdmxlib` and `attrs` (`query.py:35-40`); importing
  `statcatlib.store._lang_sql` runs `store/__init__.py` (`store/__init__.py:3-40`),
  and `store.py:57` imports `statcatlib.query`. Fix: place the module at
  `statcatlib/_lang_sql.py`, or require function-level imports in `query.py`.
  No Python code needs the hash key outside SQL: `facets` and `with_facets`
  use `_SPATIAL_FACET_KEY` (`store.py:1206, 6158-6164, 6806-6811`), and
  `spatial_labels` (`store.py:6195-6237`) and `_SpatialFilter`
  (`query.py:913-978`) are SQL.
- **Mi3. "Every coverage has a key" is false.** CKAN stores an all-empty
  spatial dict as `{}` (`sources/ckan.py:384-401, 511-513`; `store.py:734-738,
  5084`); the hash gives every such coverage one key with no label, where
  today's key is NULL and they are dropped. Fix: key NULL when `label` is
  NULL or `cardinality(label) = 0`; correct the Gaps sentence; add the case
  to the pinned test.
- **Mi4. LL-D15 does not name its code sites:** `lance.py:1140`,
  `store.py:2908` / `bundle.py:241`, `working_copy.py:1607-1612`. Fix: name
  them.
- **Mi5. No v3 fixture is specified.** Today's guard tests only rewrite
  `index.json` (`tests/test_index_version_guard.py:32-39`). Fix: a helper
  that writes a v3 table with v3 columns, or a checked-in v3 edition.

## Nit

- **N1.** Bullet 1 says each reader checks the layout; locally,
  `Product.view` and `EmbargoReader` run only the version guard
  (`store.py:2908`). Without lancedb, upload and the fold use
  `check_catalog_no_index` (`bundle.py:323-341`), so "0.20 refuses a version
  4 index" is too broad there.
- **N2.** Stored formats cites `lance.py:880-896` for `carried_vectors`;
  those lines are `hashed_vectors`; `carried_vectors` is `lance.py:1274-1289`.

## Citations confirmed

`store.py:3636, 3812, 3846, 3936, 3768-3773, 7075-7084, 1057, 6346-6350,
2452, 2572, 2574, 1343, 1401, 1437, 1502, 2781, 4042, 734-738`;
`lance.py:74, 209-219, 918-927, 1206-1211, 1297`; `bundle.py:68`;
`schema.py:51, 353-360` (table at 356-360); `working_copy.py:785, 816-847,
1605-1618`; `scopes.py:1166`; `sources/sdmx.py:153, 163-168`;
`sources/ckan.py:511-513`. The 37 `lang` defaults match (26 public, 11
private).

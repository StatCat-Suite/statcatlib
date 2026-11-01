Status: resolved (2026-11-01). RR1–RR7 and nits N1–N8 applied after the re-audit, as the owner chose; no further audit.

# Stage 9 re-audit report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md),
  DetailedRange (lines 880–1526), after the two resolution rounds
* **Reviewers:** `egm-implementation-readiness` (RA) and `egm-codebase-details`
  (RC), both fresh-context Goldfish, checked against statcatlib at 833005d and
  sdmxlib 0.80.0
* **Date:** 2026-11-01
* **Note:** this was the single re-audit the review allows.

**Verdict: not ready.** The re-audit found 2 Blockers, 5 Majors and about
8 Nits once duplicates were merged. All of them are in S1's test helper,
S1's test list, and API names; the design is unaffected.

## Fix verification (RA)

- **Landed in full:**
  - RB1 and RB3;
  - Issues 5, 6, 7, 8, 9, 10, 13 and 15–22;
  - R1–R4, R5 (through its test), R7, R8 and R11.
- **Partly landed:**
  - RB2: the text is there, but the steps cannot run. See RR1 and RR2.
  - Issue 11: see RR3.
  - Issue 12: see RR4.
  - Issue 14: see N1.
  - R9: see N2.

## Elephant notes

I checked these three findings against the code:

- **RR1 is confirmed.** `from_connection` sets `_bundle_dir = None` and
  `_lance = None` (`store.py:3201,3210`). `_init_lance` then asserts
  `_bundle_dir` (`store.py:3232`).
- **RR2 is confirmed.** `test_bundle_manifest.py` has 21 references to
  `publish_bundle`, `check_bundle` or `data_path=Embedded`.
- **RR4 is confirmed.** `docs/reference/bundle.md:3` links to
  `../api/bundles.md`.

## Blockers

### RR1. S1's `synthetic_catalog` cannot index, describe or seal (RA-B1, RC-1, RC-2)

The plan's lines 989–1001 do not work against the code, for three reasons:

- `build_index` asserts `_bundle_dir`, which a store opened with
  `from_connection` does not set.
- `catalog_section(cat)` needs the store's Lance index and its `search/`
  path. A store opened with `from_connection` has neither.
- The function returns only `cat`. `cat.close()` does not close the
  connection, so the caller has no handle on `reg`. sdmxlib requires
  closing `reg` before `seal` (`sdmxlib/testing.py:196-199`).

**Fix:**

- `from_connection` gains a private `search_dir=` argument.
- `synthetic_catalog` returns an attrs record `SyntheticCatalog(cat, reg,
  edition_dir)`.
- The docs state the order: close `cat`, then close `reg`, then seal.

### RR2. Most of `test_bundle_manifest.py`, and the kept guard tests, still break in S1 (RA-B2, RC-5)

These tests use code that S1 deletes:

- in `test_bundle_manifest.py`:
  - the `_published` fixture (lines 36–46);
  - TestPublish (61–92);
  - TestCatalogChecks (108–178);
  - TestCLI (180–199);
  - TestIndexAnnotations (216–275);
  - the imports of `sdmxlib.bundle` and `synthetic_bundle` (13, 23).
- `test_index_version_guard.py` (TestReadable and TestGuard), which opens
  `sc.Catalog(bundle)` on the old layout.
- `test_shared_connection_idle.py`, which needs an index and so breaks
  through RR1.

**Fix:** decide each class's fate.

- **Delete:** TestPublish, TestCatalogChecks, TestCLI and
  TestIndexAnnotations.
- **Keep:**
  - TestAnnotationFingerprint (277–323);
  - the overlay tests (325–344), re-imported from `sdmxlib.edition`.
- **Move to S2:** the guard tests in `test_index_version_guard.py`, which
  are rewritten there anyway (Issue 11).
- **S1:** point `test_shared_connection_idle.py` at RR1's record.

## Majors

- **RR3. No replacement for tests that run raw SQL through `.connection`
  (RA-M1).**
  - **Problem:** about 15 sites use it, in `test_sources_sdmx.py`,
    `test_ingest_data.py`, `test_skos.py`, `test_store.py`,
    `test_catalog_folder.py` and `test_sdmx_schema_guard.py`. Separately,
    the persistence tests reopen a store, and `test_schema.py` calls
    `create_schema(c)` without a `catalog_name`.
  - **Fix:**
    - add a test-only `view._cursor()`;
    - default `catalog_name=None` (no `USE`);
    - mark the persistence tests "Rewrite: seal, then `sealed_view`".
- **RR4. Dangling link in `docs/reference/bundle.md:3` (RA-M2, RC-6).**
  - **Problem:** the page is `::: statcatlib.bundle`, so "reduce" does
    nothing to it, and line 3 still links to `api/bundles.md`.
  - **Fix:** remove the link sentence on line 3.
- **RR5. Item 8 misdescribes `__init__.py` (RC-3).**
  - **Problem:** `overlay_affects_search`, `catalog_section`,
    `check_catalog` and `DataPolicy` are not imported there.
  - **Fix:** drop those four names from the item.
- **RR6. Item 5 gives no replacement import (RC-4, RA-N3).**
  - **Fix:** re-import `Finding`, `Manifest`, `MANIFEST_NAME`, `Overlay` and
    `OverlayEntry` from `sdmxlib.edition`.
- **RR7. `"lifecycle"` is not one of sdmxlib's rules (RC-7).**
  - **Problem:** `record` relabels the lifecycle instead of refusing it
    (`_rules.py:48, 216-222`).
  - **Fix:** `InventoryRuleError.rule: Rule | Literal["lifecycle"]`, with an
    optional cause. statcatlib refuses the change itself in
    `working_copy()`.

## Nits

- **N1.** Assign `tests/test_live_cuj.py` to S2's Create list.
- **N2.** `test_cli_verify_doctor.py` should cover the `.tmp` report.
- **N3.** Item 6 should also replace `Producer`, `synthetic_bundle` and the
  `Manifest` return type in `testing.py`.
- **N4.** Item 19 should restate `catalog_section(store: _Store)` for S2.
- **N5.** RFC map D13/D16: add the `test_public_api.py` assertion to item
  24.
- **N6. Line references to correct:**
  - `store.py`: the `data_path` check is 3050–3052, not 3049–3053, because
    the in-memory branch survives S1. 3116–3154 is `_colocated_data_path`
    itself, so drop the duplicate.
  - `docs/api/store.md`: use 22–25 and 84–89.
  - `docs/tutorial.md`: use 25–26.
  - `test_index_version_guard.py`: 111–128.
  - `test_catalog_folder.py`: 93–131.
- **N7.** S1 should put an "until S2" banner on `docs/bundle-format.md` and
  update `src/statcatlib/EGM.md` (51, 117, 141) and `cli/EGM.md:106`.
- **N8.** `ConflictError` is `sdmxlib.inventory.conformance.ConflictError`.
  Use it from there, or define `InventoryConflictError` in statcatlib for
  `try_commit`.

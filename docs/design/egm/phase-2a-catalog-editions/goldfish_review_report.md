Status: resolved (2026-11-01). Owner decisions: ZM1, ZM2, ZB1 option (b), ZM3 adopted (P3 revised, P5–P8). Issues 2–22 accepted; 23–24 declined.

# Goldfish Annotated Review Report

**Target:** `docs/design/phase-2a-catalog-editions.md` (DetailedRange, lines
679–1054; consistency edits reach the Technical Plan where noted)

**Findings source:** [implementation_readiness_report.md](implementation_readiness_report.md).
The open owner decisions come from
[intermediate_zoom_out_report.md](intermediate_zoom_out_report.md), because
they decide the scope of several items below.

**Counts:** 4 ESCALATE, 0 CONTEST, 19 PROPOSE FIX (grouped), 2 DECLINE.

## Prioritized Findings & AI Suggestions

### [Issue 1] [`egm-zoom-out`, Stage 8] Severity: High. Four owner decisions decide which S2–S4 items exist

*   **Affected Scope:** [Lines 683–691](../../phase-2a-catalog-editions.md)
    (Slices), and items 12–34.
*   **Discrepancy Description:** Stage 8 traced four groups of findings to
    choices the owner made. Each group's fix reverses or narrows one of
    those choices:
    *   **ZM1, P3.** Move the description and `describe` out of S2. `create`
        writes only `catalog.json`, and the first seal writes version 1. S2
        pins 0.80.0.
    *   **ZM2, RFC decisions 28 and 32.** Defer `correct` and `reinstate`.
    *   **ZB1, RFC decision 32.** Either defer prune to Phase 4, or keep a
        narrow laptop prune (one `older_than` window; retire; delete
        tombstoned directories; delete only guarded `data/` files).
    *   **ZM3.** Split the store by delegation. Embedding reuse and the
        batched seal move to an S2b slice.
*   **Rationale:** Each one reverses or narrows a decision the owner made,
    so only the owner can settle it.
*   ***AI Suggestion:*** ESCALATE
*   ***Justification:*** These reverse P3, RFC decision 32 and the GH-96
    scope, which the owner set. The evidence for each recommendation is in
    the Stage 8 report and holds up: the closed `EventKind` Literal, the
    shared `data/`, and `_rules.py:536-538`. The choice still belongs to the
    owner. Issues 2–23 below assume the Stage 8 recommendation; if the owner
    decides otherwise, the affected items keep their current text.
*   ***Proposed Edit:*** None until the owner decides. Then:
    *   rewrite the Slices table as S1 / S2 / S2b / S3 / S4;
    *   delete or narrow items 27, 29, 31 and 33 to match;
    *   update P3 and add a P6 row to the Decisions table.

### [Issue 2] [`egm-codebase-details`, `egm-implementation-readiness`] Severity: High. RB1: about 35 query sites still use `connection`

*   **Affected Scope:** [Lines 840–853](../../phase-2a-catalog-editions.md)
    (item 16), and [lines 595–605](../../phase-2a-catalog-editions.md)
    (Alternative B).
*   **Discrepancy Description:** `scopes.py` runs `self._store.connection` 27
    times. `store.py` uses it at 7167, 7192, 7219, 7251 and 3839. Those
    queries run outside the per-cursor `USE`, so under an `EditionEngine`
    they read the wrong catalog. Several modules (`store/__init__.py`,
    `query.py`, `cli`, `sources`, `lance.py`) refer to `Catalog` by name.
*   **Rationale:** Item 16 changes only two lines that set `search_path`.
    These query sites are missing from the plan.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Verified: there are 27 matches in `scopes.py`.
*   ***Proposed Edit:***
    *   Add item 16a, `src/statcatlib/store/scopes.py` (Modify). Route every
        `self._store.connection` call through `self._store._cursor()`. That
        is a private accessor that returns the per-thread cursor, and the
        cursor has already run `USE "<catalog_name>".statcat`.
    *   In item 16, route `store.py` 3839, 7167, 7192, 7219 and 7251 the same
        way.
    *   Add item 16b for `store/__init__.py`, `query.py`, `cli/__init__.py`,
        `sources/*.py` and `lance.py`. Each one changes its references to
        `Catalog` (the query class) to `EditionView` or `WorkingCopy`.
    *   Rewrite Alternative B's cost from "four changed lines" to "the four
        `SET search_path` sites plus about 35 direct `connection` uses, all
        routed through one cursor accessor".
    *   Extend `test_edition_view.py` so it runs `codelists`, `batch_labels`
        and `code_map` with two editions attached.

### [Issue 3] [`egm-implementation-readiness`, `egm-codebase-details`] Severity: High. RB2: S1 cannot leave `just ci` green

*   **Affected Scope:** [Lines 738–783](../../phase-2a-catalog-editions.md)
    (items 6, 7 and 10).
*   **Discrepancy Description:**
    *   `synthetic_edition(seal=False)` writes `<edition_id>/metadata.duckdb`,
        but the S1 Catalog opens `meta.duckdb`.
    *   `catalog_section` opens with `data_path=Embedded()`, which S1
        deletes.
    *   Tests import `publish_bundle`, `check_bundle` and `data_path=`.
*   **Rationale:** The S1 gate fails as written.
*   ***AI Suggestion:*** PROPOSE FIX, taking option (b), which keeps S1
    self-contained.
*   ***Justification:*** `Catalog.from_connection` survives S1
    (`store.py:3157`). The builder's connection holds the sdmx and statcat
    schemas, which is all `synthetic_catalog` and `catalog_section` read.
    Option (a) would leave S1 without a fixture that several test files use.
*   ***Proposed Edit:***
    *   **Item 7.** `synthetic_catalog(path, shape, *, seed=0, index=True)
        -> Catalog`:
        1. Call `reg = synthetic_edition(path, shape, seed=seed, seal=False)`.
        2. Call `cat = Catalog.from_connection(reg.connection)`.
        3. Run `prepare_sdmx` and `put`, then `build_index` into
           `<edition_dir>/search/` when `index=True`.
        4. Return `cat`, open. The caller closes it.
    *   **Item 6.** `catalog_section(cat: Catalog) -> dict` takes an open
        Catalog in place of a path.
    *   **Item 10** gains a disposition list:
        *   **Delete:**
            *   `test_catalog_folder.py`'s four `data_path` tests (lines
                93–129);
            *   `test_statcat_schema_guard.py` 149–159 and
                `test_index_version_guard.py` 113–125 (the
                `publish_bundle`/`check_bundle` cases);
            *   the `open_bundle` tests in `test_bundle_manifest.py` (98,
                103).
        *   **Keep:** the `overlay_affects_search` tests (336–344).
        *   **Rewrite** `test_shared_connection_idle.py` to open the
            `synthetic_catalog` result directly.
        *   **New test:** `check_catalog` gets one against a sealed
            `synthetic_edition` whose manifest carries
            `extensions={"statcat": catalog_section(cat)}`, sealed after
            `cat.close()`.

### [Issue 4] [`egm-codebase-details`, `egm-implementation-readiness`] Severity: High. RB3: the export edits break `import statcatlib`

*   **Affected Scope:** [Lines 705–765](../../phase-2a-catalog-editions.md)
    (items 2, 6 and 9).
*   **Discrepancy Description:** Item 9 removes names that are not exported,
    and misses five that are: `BundleInfo`, `inspect_bundle`,
    `normalize_lake_data_path`, `open_bundle` and `read_current_data_path`.
    It also misses the import at `store.py:38` and the trim of
    `bundle.__all__`.
*   **Rationale:** As written, the package fails to import.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Verified at `__init__.py:52,185`.
*   ***Proposed Edit:***
    *   **Item 9:** remove `BundleDataPathError`, `DataPolicy`, `Embedded`,
        `Colocated`, `At`, `Auto`, `BundleInfo`, `inspect_bundle`,
        `normalize_lake_data_path`, `open_bundle` and
        `read_current_data_path` from imports and `__all__`. Keep
        `overlay_affects_search`, `catalog_section` and `check_catalog`.
    *   **Item 6:** trim `bundle.__all__` (74–93) to the kept names. Delete
        the dead `_LAKE_FILENAME` and `_REMOTE_SCHEMES` (96–102), the
        data-path module docstring (1–38) and the `sdmxlib.bundle` imports
        (53–64). Change the finding texts at 850 and 856 so they no longer
        name `meta.duckdb` or `publish_bundle`.
    *   **Item 2:** remove `Embedded`, `normalize_lake_data_path` and
        `read_current_data_path` from the `bundle` import at `store.py:38`.

### [Issue 5] [`egm-codebase-details`, `egm-implementation-readiness`] Severity: Medium. The S1 lake ATTACH is unspecified

*   **Affected Scope:** [Lines 709–715](../../phase-2a-catalog-editions.md)
    (item 2).
*   **Discrepancy Description:** Item 2 deletes the helpers that build the
    ATTACH statement (`store.py:2965-2975`), and the `data_path` branch at
    3049–3053 is left in place.
*   **Rationale:** An implementer would have to decide what the ATTACH
    statement becomes.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The folder mode in 0.11 already defaults to a
    co-located `parquet/`.
*   ***Proposed Edit:***
    *   In item 2, `open()` attaches
        `ducklake:<dir>/lake.ducklake AS lake (DATA_PATH '<dir>/parquet/')`,
        and adds `READ_ONLY` when `read_only=True`.
    *   `azure_credentials` stays in S1 and is removed in S2.
    *   Delete the `data_path` branch at 3049–3053, and the `data_path`
        documentation in `docs/api/store.md:88`.

### [Issue 6] [`egm-codebase-details`] Severity: Medium. `_contact_id` raises on sdmxlib 0.80, and the assertion counts are wrong

*   **Affected Scope:** [Lines 709 and 771–776](../../phase-2a-catalog-editions.md),
    plus the upgrade table at line 560.
*   **Discrepancy Description:**
    *   `store.py:1186` calls `json.dumps` on `LocalisedString` values.
    *   The `.get("xx") ==` grep finds 59 hits in 11 files, not 69 in 12.
*   **Rationale:** Every contact that has only a name raises `TypeError`.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Verified at `store.py:1186`.
*   ***Proposed Edit:***
    *   **Item 2:** `_contact_id` uses
        `sorted(contact.name.as_dict().items())`.
        `test_agent_contact_ids.py` confirms the ids are unchanged.
    *   **Item 10:** replace the count with this procedure: run the suite
        with `SDMXLIB_ISTRING_AUDIT=1` and `SDMXLIB_ISTRING_AUDIT_OUT=<file>`
        (`istring.py:559-579`), then fix every site it reports.
        `test_codelist_policy_and_concepts.py` and `test_hybrid_search.py`
        join the list.
    *   **Upgrade table (line 560):** cite `store.py:744` and `:1186`.

### [Issue 7] [`egm-codebase-details`, `egm-implementation-readiness`] Severity: Medium. `lang=` normalisation is incomplete and names no function

*   **Affected Scope:** [Lines 709–722](../../phase-2a-catalog-editions.md)
    (items 2 and 3).
*   **Discrepancy Description:**
    *   The plan normalises 5 sites. There are 42 public `lang` entry points:
        27 in `store.py`, 10 in `scopes.py` and 5 in `query.py`.
    *   The plan names no normalising function.
    *   It does not say what happens to an invalid tag.
*   **Rationale:** A public API behaviour is left to the implementer.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** `sdmxlib.normalize_tag` and `tag_alias` are public
    exports.
*   ***Proposed Edit:***
    *   **Items 2 and 3:** every public method, `.lang()` setter and filter
        constructor that takes `lang` calls `sdmxlib.normalize_tag(lang)`
        once on entry. An invalid tag raises `sdmxlib.InvalidTagError`.
    *   **`test_lang_normalisation.py`** covers both cases:
        *   `"fr-CA"` finds the label stored under `fr-ca`;
        *   `"fr_CA!"` raises `InvalidTagError`.
    *   **Item 18:** `lance._language_slug` is replaced by
        `sdmxlib.tag_alias`.

### [Issue 8] [`egm-codebase-details`] Severity: Medium. The CKAN fix contradicts language-tags decision 2

*   **Affected Scope:** [Lines 724–730](../../phase-2a-catalog-editions.md)
    (item 4).
*   **Discrepancy Description:**
    *   Decision 2 repairs `fr_CA` to `fr-ca`, where the plan drops it.
    *   A duplicate tag pair such as `EN`/`en` raises `ValueError`, which
        the plan does not handle.
*   **Rationale:** The fix conflicts with an approved design and misses a
    second abort path.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `docs/design/language-tags.md`, decision 2, and
    `istring.py:224-226`.
*   ***Proposed Edit:***
    *   Replace item 4 with: "`_lang_map` (lines 333–339) applies
        language-tags decision 2:
        1. strip the tag and replace `_` with `-`;
        2. call `normalize_tag`;
        3. drop a tag that raises `InvalidTagError`, counting it per source
           and logging a WARNING;
        4. on a duplicate after normalisation, keep the first text and log a
           WARNING.

        Lines 358 and 393 read through `_lang_map` and need no change."
    *   `test_sources_ckan.py` gains three cases: `fr_CA`, an invalid tag,
        and an `EN`/`en` pair.

### [Issue 9] [`egm-codebase-details`] Severity: Medium. Item 5 claims a change that does not exist, and `registry.data` is not an API

*   **Affected Scope:** [Lines 732–736, 831–833 and 888–891](../../phase-2a-catalog-editions.md)
    (items 5, 15 and 21), and the Technical Plan at lines 396 and 410–413.
*   **Discrepancy Description:**
    *   `seal` raises the out-of-codelist `ManifestError`, before the
        journal entry. Line 311 does not raise it.
    *   `LocalRegistry` has no public `data` attribute.
*   **Rationale:** Item 5 describes a non-change. Items 15 and 21 point at an
    API that does not exist.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `_write.py:552,605-619` and
    `registry.py:1616,2853`.
*   ***Proposed Edit:***
    *   **Delete item 5.**
    *   **Items 15 and 21:** WorkingCopy exposes `registry`, and
        `sources/sdmx.py:311` calls `wc.registry.write_observations`.
    *   **Technical Plan, after step 5, add:** "If `sdmxlib.edition.seal`
        raises `ManifestError`, it does so before writing the journal entry.
        The WorkingCopy can be resumed with `working_copy(resume=True)`."

### [Issue 10] [`egm-codebase-details`] Severity: Medium. The read path names the wrong sdmxlib calls, and the schema checks are misdescribed

*   **Affected Scope:** [Lines 815–858](../../phase-2a-catalog-editions.md)
    (items 14, 16 and 17), and the Technical Plan at lines 351 and 431–447.
*   **Discrepancy Description:**
    *   `EditionEngine.attach` returns only a prefix. `LocalRegistry.edition`
        returns the `EditionRegistry`, which also verifies the manifest's
        schema.
    *   `_refuse_sdmx_schema_rebuild` does not query `information_schema`.
    *   Five `information_schema` queries that need scoping are not listed:
        `store.py` lines 1739, 4084, 4251, 4344 and 4659.
*   **Rationale:** An implementer following the plan would write the wrong
    calls.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `registry.py:1452-1539,1496-1500` and
    `engine.py:115-142`.
*   ***Proposed Edit:***
    *   `Product.view()` and item 14 call
        `LocalRegistry.edition(edition_dir, engine=self._engine)`.
    *   A WorkingCopy takes `catalog_name` from its build registry.
    *   On the EditionView path, drop `_refuse_sdmx_schema_rebuild`, because
        sdmxlib already refuses.
    *   The `table_catalog` filter list becomes `schema.py`'s
        `schema_exists` and `get_schema_version`, `bundle.py:846`, and
        `store.py` lines 1739, 4084, 4251, 4344 and 4659.

### [Issue 11] [`egm-implementation-readiness`, `egm-codebase-details`] Severity: Medium. The test migration has no per-file dispositions or fixture shape

*   **Affected Scope:** [Lines 926–944](../../phase-2a-catalog-editions.md)
    (item 25).
*   **Discrepancy Description:**
    *   The `store` fixture serves 162 tests that write and query through
        one object.
    *   The guard and thread tests depend on constructors that S2 removes.
    *   `conftest.py`'s helpers take a `sc.Catalog`.
    *   `test_index_version_guard.py:62` conflicts with item 18.
*   **Rationale:** An implementer would have to decide how each file
    migrates.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The WorkingCopy and its view share one connection
    (Technical Plan, WorkingCopy), so one view taken from `wc` sees later
    writes.
*   ***Proposed Edit:*** Item 25's Modify block becomes:
    *   **`conftest.py` fixtures:**
        *   `wc` is a WorkingCopy in a `temporary_catalog`.
        *   `view` is `wc.view()`, taken once. It sees later writes because
            it shares the connection.
        *   `sealed_view` is `cat.product(p).view()` after `wc.seal()`, for
            read-path tests.
        *   `store` is kept as an alias that returns an object with both
            faces: `wc`'s write methods and `view`'s query methods. That
            keeps the 162 tests unchanged.
        *   `require_index` and `search_table` take an `EditionView`.
    *   **A disposition table:**

        | File | Disposition |
        |---|---|
        | `test_sdmx_schema_guard.py` | Rewrite: the guard runs in `LocalRegistry.edition` (`StoreSchemaError`) |
        | `test_statcat_schema_guard.py` | Rewrite: the guard runs at `Product.view()` |
        | `test_index_version_guard.py` | Rewrite: v3 is read, v2 is refused, and the v1 case is deleted along with the legacy read path |
        | `test_thread_safety.py` | Rewrite: over `sealed_view` |
        | `test_shared_connection_idle.py` | Rewrite: over `sealed_view` |
        | `test_catalog_folder.py`, `test_read_only.py` | Rewrite: around `create` and `open` |
        | 28 others | Swap `Catalog(...)` for the fixtures, using the self-contained file list |

### [Issue 12] [`egm-implementation-readiness`, `egm-codebase-details`] Severity: Medium. Dangling links break the strict docs build

*   **Affected Scope:** [Lines 785–793, 946–964 and 994–999](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   `bundle-format.md:355` links to the page that S1 deletes.
    *   `search.md:196` links to the page that S2 renames.
    *   The checks page has no path.
    *   Deleting `docs/reference/bundle.md` leaves public symbols with no
        reference page.
*   **Rationale:** `mkdocs build --strict` fails mid-slice, and that build
    is part of `just ci`.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Both links are cited by line number.
*   ***Proposed Edit:***
    *   **Item 11:**
        *   Remove the link at `bundle-format.md:355`.
        *   Keep `docs/reference/bundle.md`, reduced to `catalog_section`,
            `check_catalog`, `overlay_affects_search` and `STATCAT_SECTION`.
        *   Update the `data_path` text in `api/store.md` and the `parquet/`
            text in `tutorial.md`.
    *   **Item 26:**
        *   Fix the link at `search.md:196` to
            `catalog-format.md#4-the-search-index`.
        *   Create `docs/reference/checks.md`, with its nav line under
            Reference.

### [Issue 13] [`egm-implementation-readiness`] Severity: Medium. The RFC acceptance tests are only partly mapped, and no markers are stated

*   **Affected Scope:** [Lines 686–691 and 914–1022](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   These RFC rows have no mapping: D5/D18, D12, D13, D16, D22 and the
        D14 enum count.
    *   "The restore test" and "D32's checks" are undefined.
    *   Exit code 2 is untested.
    *   No test file states its `pytestmark`.
*   **Rationale:** The gate selects tests by marker, so an unmarked test file
    never runs (GH-8).
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** CLAUDE.md, "`just ci` is the gate".
*   ***Proposed Edit:*** Add a subsection, "RFC acceptance test map", before
    the Verification Plan:

    | RFC row | Slice | Test file | Disposition |
    |---|---|---|---|
    | D1 | S1/S2 | `just ci` | — |
    | D2, D3, D15, D19 | S2 | `test_catalog_model.py` | — |
    | D4, D7, D17 | S3 | `test_catalog_admin.py` | — |
    | D5/D18 | S2 | `docs/catalog-format.md` and a `check-prose`-style grep test | — |
    | D6 | S2 | `test_concurrent_seal.py` | — |
    | D8, D11, §4 | S2 | `test_inventory_writer.py` | — |
    | D9 | S2 | `test_edition_check.py` | — |
    | D10 | S2 | `test_legacy_layout.py` | — |
    | D12 | — | — | N/A: process |
    | D13, D16 | S2 | `test_public_api.py` (no `Catalog` attribute returns an operational object) | — |
    | D14 | S3 | `test_catalog_admin.py` (`len(get_args(State)) == 4`) | — |
    | D20 | S3 | `test_catalog_admin.py` | — |
    | D21 | S2b | `test_embedding_reuse.py` | — |
    | D22 | — | — | N/A: sdmxlib |
    | §6 | S2 | `test_seal_crash.py` | — |
    | §10 | S2 | `test_edition_view.py` | — |

    Also:
    *   Delete "restore test" and "D32's checks" from the Slices table.
    *   Add a `verify-catalog` exit-2 case to `test_cli_verify_doctor.py`.
    *   Mark `test_concurrent_seal.py` and `test_seal_crash.py`
        `pytestmark = pytest.mark.integration`, and every other new file
        `unit`.

### [Issue 14] [`egm-implementation-readiness`] Severity: Medium. The Verification Plan cannot be run as written

*   **Affected Scope:** [Lines 1037–1050](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   No live test for CUJ 1 exists.
    *   The plan claims branch coverage without `branch = true`.
    *   The `check-any` ratchet fails when the `Any` count drops.
*   **Rationale:** The steps cannot be executed as stated.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `count_any.py:235-242` and `pyproject.toml:93-94`.
*   ***Proposed Edit:***
    *   Add `tests/test_live_cuj.py` (marker `live`) for CUJ 1.
    *   Add CUJ 2 as an integration test in `test_catalog_admin.py`.
    *   Drop the branch-coverage claim, so the plan reads "the crash tests
        cover every seal step in `tests/test_seal_crash.py`".
    *   In each slice, add a step: "run `uv run python tools/count_any.py
        --write` and commit `tools/any_baseline.txt` when the count
        changes."

### [Issue 15] [`egm-implementation-readiness`] Severity: Medium. The release, rollback and hotfix plan does not fit the release recipes

*   **Affected Scope:** [Lines 693–695](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   The release recipes merge to `main` only.
    *   The tag is `0.11.0`, not `0.11`.
    *   The plan gives no versions and no rollback.
*   **Rationale:** Step 3 cannot be followed as stated.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See the justfile, lines 155–218.
*   ***Proposed Edit:*** Replace the paragraph with:
    *   "Each slice is a feature branch and a PR, merged with a merge commit
        after `just ci` passes. Breaking commits use `feat!:` or `fix!:`.
    *   S2 releases as 0.12.0, and S2b, S3 and S4 each release as a minor
        version.
    *   **Rollback:** revert the merge commit. Users pin the previous minor.
        0.11 cannot read 0.12 catalogs, so a downgrade means re-ingesting.
    *   **Hotfix:** while S1 sits unreleased on main, the release recipes
        cannot cut a patch. An urgent fix is a manual branch from tag
        `0.11.0` with a manually built release."

### [Issue 16] [`egm-style`] Severity: Medium. Seventeen instructions can be read two ways (STY9 M1–M17)

*   **Affected Scope:** [Lines 679–1050](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   Line numbers go stale between slices.
    *   P5's release status is unclear.
    *   The version range is wrong.
    *   Verbs such as "map", "move", "switch", "update" and "covers" are
        vague.
    *   Error classes and the export list are unnamed.
    *   Items 15 and 16 duplicate one move.
    *   `conf_status` gives no severity.
    *   The prune CLI uses placeholders.
    *   Some statements use a tense that hides their status.
*   **Rationale:** An implementer could carry out any of these two ways.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Each finding quotes the text and gives a rewrite.
*   ***Proposed Edit:*** Apply the STY9 rewrites as given, with three
    adjustments:
    *   **M1:** add "Line numbers refer to commit 833005d; find each site by
        its symbol" under Slices.
    *   **M11:** the export list is `InventoryContentionError`,
        `InventoryRuleError`, `NonFastForwardError`,
        `WorkingCopyExistsError`, `WorkingCopyClosedError`,
        `SealCheckError` and `IncompatibleCatalogError` (existing). This
        follows Stage 8 ZM4's single wrapper.
    *   **M13:** every finding from `conf_status` refuses the seal (ZM4).
        M17 depends on Issue 1.

### [Issue 17] [`egm-codebase-details`] Severity: Low. Reuse the sdmxlib.inventory helpers, and correct the `build` arguments (CB-N4, CB-N5)

*   **Affected Scope:** [Lines 803–811](../../phase-2a-catalog-editions.md)
    (item 13), and [line 384](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:**
    *   Item 13 re-derives helpers that sdmxlib.inventory already exports.
    *   The `Producer` is unnamed.
    *   `build(previous=)` takes an edition id, not a directory.
*   **Rationale:** Some of this is duplicated code, and some is wrong.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `_rules.py:452-471` and `registry.py:2497`.
*   ***Proposed Edit:***
    *   **Item 13:** the writer uses `encode`, `leaf_ref`, `root_path`,
        `leaf_path`, `CatalogDocument`, `Current`, `CATALOG_NAME`,
        `CURRENT_NAME` and `decode_current`. The producer is
        `Producer(name="statcatlib", version=statcatlib.__version__)`.
    *   **Line 384:** `build(product_dir, previous=<head edition_id>)`.

### [Issue 18] [`egm-codebase-details`] Severity: Low. `add_bulk` returns early when a search table has no rows (CB-N3)

*   **Affected Scope:** [Lines 863–870](../../phase-2a-catalog-editions.md)
    (item 18).
*   **Discrepancy Description:** `add_bulk` already drops the old tables.
    When an edition has no rows, it returns early and leaves the carried
    tables in place.
*   **Rationale:** An edition with no search rows would keep the previous
    edition's tables.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** See `lance.py:1324-1359`.
*   ***Proposed Edit:*** Replace "`discard_orphans` drops the previous
    edition's tables" with "`add_bulk` already drops the superseded tables
    after its commit. For an edition with no rows, seal calls
    `discard_orphans` explicitly so the carried tables do not survive."

### [Issue 19] [`egm-implementation-readiness`] Severity: Low. IR nits

*   **Affected Scope:** items 22, 23, 26 and 14; the Slices table.
*   **Discrepancy Description:**
    *   The CLI needs a `catalog.json` check because of P4.
    *   The S2 `synthetic_catalog` signature is missing.
    *   The `__getattr__` list is missing.
    *   CLAUDE.md and root `EGM.md` need updating in S1.
    *   S4's dependency is not stated.
*   **Rationale:** Small gaps that an implementer would otherwise fill by
    guessing.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** These are cheap fixes.
*   ***Proposed Edit:***
    *   **Item 23:** the CLI refuses a path with no `catalog.json` before
        calling `create(exist_ok=True)`.
    *   **Item 22:** `synthetic_catalog(path, shape, *, product_id="synthetic",
        seed=0, index=True) -> Edition`.
    *   **Item 14:** `__getattr__` covers every public attribute of
        `EditionView`.
    *   **Item 11:** add the CLAUDE.md sdmxlib cap line, the Bundle contract
        paragraph and root `EGM.md`.
    *   **Slices:** add "`verify-catalog` and `doctor` depend on S2 only."

### [Issue 20] [`egm-codebase-details`, `egm-style`] Severity: Low. The test-file lists are not self-contained (CB-N1, STY9 M12)

*   **Affected Scope:** [Lines 931–944](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:** The lists name 34 files, including some
    that do not call `Catalog(`.
*   **Rationale:** An implementer cannot tell which files to change.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The reviewers verified the count.
*   ***Proposed Edit:*** Folded into Issue 11's disposition table: a
    self-contained list, excluding `test_model.py` and
    `test_cli_build_index.py`.

### [Issue 21] [`egm-codebase-details`] Severity: Low. Cleanup items left unlisted (CB-N6)

*   **Affected Scope:** items 6 and 26.
*   **Discrepancy Description:** The plan does not list several cleanups,
    and leaves `CHANGELOG.md` unlisted.
*   **Rationale:** None beyond completeness.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** The bundle cleanup is folded into Issue 4.
    `CHANGELOG.md` is written by the release flow (`release-2-changelog`),
    not by a slice PR.
*   ***Proposed Edit:*** Add one line under Slices: "CHANGELOG entries come
    from `just release-2-changelog` at each release; slice PRs do not edit
    `CHANGELOG.md`."

### [Issue 22] [`egm-style`] Severity: Low. Mood, notation and Scope lines (STY9 N1–N16)

*   **Affected Scope:** DetailedRange.
*   **Discrepancy Description:**
    *   Mixed imperative and descriptive mood.
    *   Unnamed § sources.
    *   Backtick consistency.
    *   Missing Scope lines.
*   **Rationale:** Consistency.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** These edits apply while the items above are
    rewritten anyway.
*   ***Proposed Edit:*** Apply the STY9 nit rewrites. Use the imperative
    throughout, write "RFC §n" and "spec §n", and add Scope lines to every
    item.

### [Issue 23] [`egm-implementation-readiness`] Severity: Low. Item 5 has nothing to map

*   **Affected Scope:** [Lines 732–736](../../phase-2a-catalog-editions.md).
*   **Discrepancy Description:** Duplicate of Issue 9.
*   **Rationale:** —
*   ***AI Suggestion:*** DECLINE
*   ***Justification:*** Issue 9 deletes item 5.
*   ***Proposed Edit:*** None.

### [Issue 24] [`egm-style`] Severity: Low. Stale line numbers (STY9 M1)

*   **Affected Scope:** DetailedRange.
*   **Discrepancy Description:** Line numbers shift after S1.
*   **Rationale:** —
*   ***AI Suggestion:*** DECLINE as a separate edit.
*   ***Justification:*** Issue 16 already covers it with the commit-anchor
    line. Converting every reference to a symbol name adds churn for no
    reader benefit.
*   ***Proposed Edit:*** None beyond Issue 16.

### [Issues 25–27] Owner-scoped items that depend on Issue 1

*   **AI Suggestion:** ESCALATE (bundled with Issue 1)
*   **Items:**
    *   **25 (STY9 M15, M17; IR S4 dependency):** the CatalogAdmin and
        prune item texts. They are rewritten only if correct, reinstate and
        prune stay.
    *   **26 (CB-M7 for `describe`):** depends on ZM1.
    *   **27 (IR-M7 S2b version):** depends on ZM3.

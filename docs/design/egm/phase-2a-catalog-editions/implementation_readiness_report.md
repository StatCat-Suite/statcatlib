# Stage 9: Implementation Readiness report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md),
  DetailedRange (lines 679–1054)
* **Reviewers:** three fresh-context Goldfish reviewers:
  - `egm-implementation-readiness` (IR);
  - `egm-codebase-details` (CB), checking against the statcatlib working tree
    and sdmxlib 0.80.0;
  - `egm-style` (STY9), against the CLAUDE.md writing rules.
* **Standard:** EGM Implementation Readiness Standard
* **Date:** 2026-11-01
* **Excluded:** anything already in the [Stage 8 report](intermediate_zoom_out_report.md)

**Verdict: not ready.** The plan has 3 Blockers after deduplication, about 25
Majors, and about 29 Nits.

The architecture holds up. The plan text does not, for three reasons:

- S1 cannot leave `just ci` green.
- S2 misses the roughly 35 query sites that use the `connection` property.
- Many items name the wrong symbols, list the wrong exports, or leave out
  the test that should cover them.

## Elephant notes

The Elephant spot-checked three findings against the code:

- **CB-B1 is confirmed.** `scopes.py` uses `self._store.connection` 27
  times.
- **CB-M3 is confirmed.** `store.py:1186` runs
  `json.dumps(sorted(contact.name.items()))`. In sdmxlib 0.80 the values of
  `items()` are `LocalisedString` objects.
- **CB-M1 is confirmed.** `__init__.py:52,185` exports `open_bundle`, which
  item 6 deletes. Item 9 does not remove that export.

Two pairs of findings merge:

- IR-B1 and CB-B2 report the same S1 test-helper break, so they are merged
  as RB2.
- IR-M2 and CB-B1 report the same `connection` gap, so they are merged as
  RB1.

## Blockers

### RB1. S2 deletes `connection`, but about 35 query sites still use it

- **Sources:** CB-B1 and IR-M2.
- **Plan lines:** items 3 and 16 (717–722, 840–853); the claims at 293,
  436–440 and 602–603.
- **Code:**
  - `scopes.py` uses `self._store.connection` 27 times (lines 462–1419).
  - `store.py` uses `self.connection.execute` at 7167, 7192, 7219 and 7251,
    and `CHECKPOINT` at 3839.
  - `store/__init__.py:20` re-exports `Catalog`.
  - `Catalog` is referenced by name in:
    - `scopes.py`, 12 times;
    - `query.py`, 3 times;
    - `cli`, 14 times;
    - `sources`;
    - `lance.py`.
- **Problem:** Under an `EditionEngine`, these sites run on the shared base
  connection, outside the per-cursor `USE`, so they read the wrong catalog
  or fail. Two of the plan's claims are false: Alternative B's "four changed
  lines" and "every scope stays as it is".
- **Fix:**
  - Add `scopes.py`, `query.py`, `store/__init__.py` and `bundle.py` to S2.
  - Route every one of these sites through a private cursor accessor that
    runs `USE`.
  - Extend `test_edition_view.py` to call a scope, `batch_labels` and
    `code_map` with two editions attached.

### RB2. S1 cannot leave `just ci` green

- **Sources:** IR-B1, CB-B2, CB-M9 and IR-M1 (in part).
- **Plan lines:** items 2, 6, 7 and 10.
- **Code:**
  - `synthetic_edition(seal=False)` returns a writable `LocalRegistry` over
    `<path>/<edition_id>/metadata.duckdb` (sdmxlib `testing.py:181-235`).
    The S1 folder `Catalog` opens `<path>/meta.duckdb` instead
    (`store.py:2966, 3060-3071`).
  - `catalog_section` opens
    `Catalog(root, read_only=True, data_path=Embedded())`
    (`bundle.py:664`), and S1 deletes both arguments.
  - These tests use code that S1 deletes:
    - `test_catalog_folder.py:93-129` (four `data_path` tests);
    - `test_statcat_schema_guard.py:149-159` and
      `test_index_version_guard.py:113-125` (`publish_bundle` and
      `check_bundle`);
    - `test_shared_connection_idle.py:36,42`;
    - `test_bundle_manifest.py:98,103,336-344` (`open_bundle` and
      `overlay_affects_search`).
- **Fix:**
  1. Choose between:
     - (a) removing `synthetic_catalog` and `catalog_section` in S1 and
       rebuilding them over the `WorkingCopy` in S2; or
     - (b) wrapping the builder with
       `Catalog.from_connection(builder.connection)` and having
       `catalog_section` take an open `Catalog`.
  2. List what happens to each affected test in S1.

### RB3. S1's export and import edits break `import statcatlib`

- **Sources:** CB-M1 and IR-M1.
- **Plan lines:** items 2, 6 and 9 (705–715, 738–746, 761–765).
- **Code:**
  - `publish_bundle` and `check_bundle` are not exported.
  - These names are exported, and item 6 deletes them:
    - `BundleInfo`;
    - `inspect_bundle`;
    - `normalize_lake_data_path`;
    - `open_bundle`;
    - `read_current_data_path`.

    The export sites are `__init__.py:44-53` and `__all__` at lines 109–188.
  - `store.py:38` imports `Embedded`, `normalize_lake_data_path` and
    `read_current_data_path` from `bundle`.
  - `bundle.__all__` (74–93) lists `DataPolicy` and the other deleted names.
- **Fix:**
  - List every symbol removed from `bundle.__all__` and from
    `statcatlib/__init__.py`.
  - Add the `store.py:38` import to item 2.
  - Say whether `overlay_affects_search` stays.

## Major: S1

- **IR-M1 and CB-M2: the replacement lake ATTACH is not specified.**
  - `store.py:2965-2975` builds the ATTACH through the deleted helpers.
  - The `data_path` branch at 3049–3053 remains.
  - The fate of `azure_credentials` is not stated.
  - **Fix:** write out the ATTACH statement, for example
    `(DATA_PATH '<dir>/parquet/'[, READ_ONLY])`.
- **CB-M3: `_contact_id` raises `TypeError` on 0.80.**
  - `store.py:1186` serialises `LocalisedString` values with `json.dumps`.
  - **Fix:** use `sorted(contact.name.as_dict().items())`.
  - The plan's test counts do not reproduce: `.get("xx") ==` matches 59
    times in 11 files, and a broader match finds 67 in 13 files.
  - **Fix for the counts:** find the sites with sdmxlib's
    `SDMXLIB_ISTRING_AUDIT=1` audit (`istring.py:559-579`).
- **CB-M4 and IR-M8: `lang=` normalisation is incomplete.**
  - The parameters are 27 in `store.py`, 10 in `scopes.py` (including the
    `.lang()` setters) and 5 in `query.py`.
  - **Fix:**
    - Name `sdmxlib.normalize_tag`.
    - Normalise once at each public entry point.
    - State that an invalid tag raises `InvalidTagError`, and test it.
    - Use `sdmxlib.tag_alias` in place of `lance._language_slug` for the
      table names.
- **CB-M5: the CKAN fix conflicts with `language-tags.md` decision 2.**
  - Decision 2 says: strip, map `_` to `-`, then `normalize_tag`, and log
    refusals per source.
  - The plan misses `ValueError` on duplicate tags such as `EN`/`en`
    (`istring.py:224-226`).
  - **Fix:** apply decision 2. Keep the first text on a duplicate and log a
    WARNING. Lines 358 and 393 then need no change.
- **CB-M8: the claim about item 5 is wrong.**
  - `sdmx.py:311` raises nothing new in S1.
  - The out-of-codelist refusal comes from `seal`
    (`_write.py:552, 605-619`), as a `ManifestError` raised before the
    journal is written.
  - `LocalRegistry` has no public `data`. It has `write_observations`
    (`registry.py:2853`).
  - **Fix:**
    - Remove the claim.
    - Item 21 uses `registry.write_observations`.
    - The S2 seal states that after a `ManifestError` the WorkingCopy can
      be resumed and no journal entry exists.

## Major: S2

- **CB-M6: the scoped checks are misdescribed.**
  - `_refuse_sdmx_schema_rebuild` has no `information_schema` query.
  - For sealed editions, `LocalRegistry.edition` already refuses schema
    drift (`registry.py:1496-1500`).
  - Five `information_schema` queries are unscoped and not listed:
    `store.py:1739`, 4084, 4251, 4344 and 4659.
  - **Fix:** drop the sdmx guard on the `EditionView` path, and scope those
    five queries.
- **CB-M7: use `LocalRegistry.edition(edition_dir, engine=)` instead of
  `EditionEngine.attach`.**
  - `attach` returns only a prefix string.
  - `edition()` checks the manifest, wires `__tables`, and returns the
    `EditionRegistry` that provides `catalog_name`.
  - For a WorkingCopy, take `catalog_name` from the build registry instead
    of hard-coding `metadata`.
- **IR-M3 and CB-M10: the test migration is not a fixture swap.**
  - The guard, thread-safety and idle-connection tests depend on
    `Catalog(path).open()`, `read_only`, `from_connection` and
    `.connection`.
  - `conftest.py`'s helpers take `sc.Catalog` and read `_lance`.
  - The `store` fixture serves 162 tests that write and query one object.
  - `test_index_version_guard.py:62` ("version 1 is still read") conflicts
    with item 18.
  - **Fix:**
    - Add a table giving each test file's fate.
    - Define the fixture shape, for example `view` taken once from `wc`
      over a shared connection, with a sealed-view fixture as well.
    - State where the schema guards now run.
    - Say whether the legacy index read path is deleted.

## Major: across slices

- **IR-M4 and CB-M11: the strict docs build breaks.**
  - `docs/bundle-format.md:355` links the `api/bundles.md` page that S1
    deletes.
  - `docs/search.md:196` links the `bundle-format.md` page that S2 renames.
  - The checks page has no path and no nav entry.
  - Deleting `docs/reference/bundle.md` leaves the public `bundle.py`
    symbols undocumented.
  - **Fix:**
    - List the link edits for each slice.
    - Give the page a path, `docs/reference/checks.md`.
    - Keep a `bundle` reference page.
- **IR-M5: the mapping from RFC acceptance tests is partial.**
  - D5/D18, D12, D13, D16, D22 and D14's enum-count test are not mapped.
  - The restore test and "D32's checks" are undefined.
  - Exit code 2 is untested.
  - No new test file states its marker.
  - **Fix:** add a table mapping each RFC row to a test file and test
    name, writing "N/A in 2a" with a reason where a row does not apply.
    State the `pytestmark` of each new file.
- **IR-M6: the verification plan cannot be run.**
  - No CUJ 1 live test exists.
  - "Branch coverage" needs `branch = true`, and `just coverage` is outside
    `just ci`.
  - `check-any` fails when the count drops (`count_any.py:235-242`), so
    deleting write paths forces a baseline update.
  - **Fix:**
    - Add `tests/test_live_cuj.py`.
    - Either drop the branch-coverage claim or set `branch = true`.
    - Add a `tools/any_baseline.txt` update step to each slice.
- **IR-M7: the release, rollback and hotfix plan does not fit the release
  recipes.**
  - The recipes merge to `main` only.
  - The tag is `0.11.0`.
  - **Fix:**
    - State each slice's version, for example S2 = 0.12.0.
    - Mark breaking commits `feat!:` or `fix!:`.
    - Rollback: revert the merge, and users pin the previous minor.
    - State that a hotfix needs manual steps while S1 is unreleased.

## Major: wording (STY9)

These sentences can be read two ways, so an engineer could carry them out
differently:

- **M1:** line numbers go stale between slices. State that they refer to
  commit 833005d, or name functions instead.
- **M2:** P5's release status is unclear. Write "S1 ships first in the S2
  release."
- **M3:** "map 0.73 to 0.80" uses the wrong version range, which should be
  0.72 to 0.80.
- **M4:** the `data_path=` owner and the stamp-repair range are unclear.
- **M5:** "read MAP values as plain strings" names no sites and no target
  type.
- **M6:** the CKAN "same applies" has no clear referent.
- **M7:** "move `check_catalog`" should be "change its signature".
- **M8:** "the rename" and "schema 2.52" are unclear.
- **M9:** items 15 and 16 both say to move the write methods, and neither
  lists them.
- **M10:** "guided errors" does not state their content.
- **M11:** error classes are left unnamed. "The new errors" needs an
  explicit export list.
- **M12:** "32 files" refers to the wrong list, and two files appear under
  two instructions.
- **M13:** `conf_status` does not state the severity of its findings.
- **M14:** the docs verbs "covers", "add a page" and "update" are vague, and
  "the risk" alludes to something unstated.
- **M15:** "each a single commit" includes the read-only `editions`.
- **M16:** "the laptop clock caveat" alludes to something unstated, and
  "done at finalization" has the wrong tense.
- **M17:** prune's CLI placeholders and test wording are unclear.

## Nits

- **IR:**
  - item 5 has nothing to map;
  - the `build-index` CLI must check for `catalog.json` because of P4;
  - S2's `synthetic_catalog` signature is unstated;
  - the `__getattr__` name list is missing;
  - the CLAUDE.md cap and root `EGM.md` must update in S1;
  - S4 waits on S2 only, except for prune;
  - make the test-file list self-contained.
- **CB:**
  - **N1:** the 32-file count is right, but the lists name 34 files.
  - **N2:** `_istring_to_map` is at 744.
  - **N3:** `add_bulk` already drops old tables, and returns early on
    empty rows.
  - **N4:** reuse `encode`, `leaf_ref`, `root_path`, `leaf_path`,
    `CatalogDocument`, `Current` and the name constants, and name the
    `Producer`.
  - **N5:** `build(previous=)` takes an edition id, not a directory.
  - **N6:** dead constants, the module docstring, finding texts naming
    `meta.duckdb`, and `CHANGELOG.md` are unlisted.
- **STY9:**
  - **N1–N16:**
    - mixed imperative and descriptive mood;
    - unnamed sources for §-numbers;
    - placeholders;
    - backtick consistency;
    - "edition_id hex" against "hyphens removed";
    - "statcat section 3" against "format 3";
    - a vague `pytestmark`;
    - a "Build" heading with no build step.
  - Items 12 and 17–34 have no Scope lines.

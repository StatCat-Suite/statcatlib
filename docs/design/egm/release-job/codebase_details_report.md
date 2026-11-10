# Codebase details report, instance A: statcatlib claims

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`. Code citations are statcatlib at `3306996`. Instance B's report on sdmxlib, spec, plan
and issue claims is `codebase_details_sdmxlib_report.md`.

**Verdict.** No Blocker. Almost every claim the design makes about today's statcatlib code holds: each
cited line range, each symbol, and the `content_fingerprint` and `canonical_form` grep. The weak
points are in what the design proposes. Five Major findings:

1. D6's leaf-first records remove the manifest check `ManifestArtefacts` runs on every incoming
   entry, for `CatalogAdmin.release` and `rollback` on disk as well as on blob.
2. Four existing tests and one API page assert the Phase 4 refusals, and no Part lists them.
3. Every new test file but one lacks a Create mark.
4. The retirement move leaves behind the prune code it should share.
5. The error contract of `ManifestArtefacts(Fetch)` is unspecified, and two callers depend on it.

The Minor findings are reuse opportunities, signature gaps, test tiers that differ from the files'
markers, and docs and nav entries the plan does not list.

## What I read

- The persona `agents/codebase_details.md` of the EGM plugin, and the target in full.
- `CLAUDE.md`, `src/statcatlib/EGM.md`, `src/statcatlib/store/EGM.md`, `src/statcatlib/cli/EGM.md`.
- In full: `store/inventory.py`, `store/cross_product.py`, `admin.py`, `upload.py`.
- In large part: `store/working_copy.py` (1-420, 500-660, 820-1070, 1454-1850), `store/blob.py`
  (160-851), `bundle.py` (1-410, 555-574).
- In part: `catalog.py` (590-740), `diagnostics.py` (366-390), `cli/__init__.py` (615-775),
  `bench/corpus.py` (120-200), `tests/stores/_stores.py` (265-415), `tools/count_any.py`
  (195-244), `mkdocs.yml` (90-132), `docs/catalog-format.md` (85-338), `docs/api/store.md`
  (350-375).
- Tests: `tests/test_blob_storage.py` (700-748), `tests/test_blob_open.py` (150-189),
  `tests/test_inventory_writer.py` (56-76, 265-305), `tests/test_public_api.py` (176-255), and a
  listing of every file under `tests/`, `docs/api/`, `docs/reference/` and `tools/`.
- The Phase 4 design's D4, D5, D7, W3, W6, W11, F13 and F14.
- sdmxlib: the scratchpad copies of `_rules.py` (140-540), `_routing.py` and `_check.py`, and the
  installed `sdmxlib-0.87.0` in the project's virtual environment, by grep.

## Limits

- Nothing was run, and there was no git.
- The installed sdmxlib's `inventory/` modules were not opened; the scratchpad copies stood in.
- Spec and plan section claims are instance B's.
- Whether sdmxlib accepts a `release` event with no leaf change (D5's record-only version) was not
  checked.

## Blocker

None.

## Major

### CB-1. Leaf-first records drop the manifest check on incoming entries

**Sites:** design:470-474, 485-489, 666-668 (D11); `store/cross_product.py:157-175`, `:275-276`;
`admin.py:296-330`.

**Failure:** Today every incoming entry is read through `ManifestArtefacts`, which checks that the
manifest exists and hashes it against the Inventory before parsing (`cross_product.py:170`, "if
digest != manifest_sha256: ... raise ManifestError"). D6's `_records` returns `entry.artefacts`
whenever it is not `None`, and the incoming entry is read the same way (`_Rules._read` calls the
reader, `cross_product.py:275-276`). After Part 1 a filled entry no longer has its manifest checked
by `CatalogAdmin.release` or `rollback`, on disk or on blob. Only the release job's D4 step adds the
check back (design:405-407). The Goal at design:113-114 then holds for the job and fails for manual
releases. The cost paragraph counts "one manifest GET for each incoming entry" (design:488-489),
which under `_records` a filled incoming entry does not make. The compiler confirmed the check site.

**Fix:** Choose one behaviour and state it. Either `_records` reads only non-incoming entries from
the leaf, and `refusals` still reads or hashes each incoming entry's manifest; or D11 adds D4's GET
and SHA-256 comparison to `CatalogAdmin._commit_product` for `release` and `rollback`. Align the
cost paragraph, and add a test that a release whose manifest was removed or altered is refused.

### CB-2. Tests and pages asserting the Phase 4 refusals are missing from Parts 1 and 2

**Sites:** design:891-912 (Part 1), 914-931 (Part 2); `tests/test_blob_storage.py:719-735`,
`tests/test_blob_open.py:170-172`, `docs/api/store.md:364-366`, `CLAUDE.md:374-376`.

**Failure:** Part 1 removes `_no_manifest_reader`, and two unit tests expect its error,
`test_an_incoming_entry_with_no_artefacts_raises_naming_phase_5_and_reads_no_leaf` and
`test_an_embargoed_entry_is_incoming_too` (`pytest.raises(NotImplementedError, match="Phase 5")`).
Part 2 makes `CatalogAdmin.open` accept a `BlobLocation`, and
`test_an_admin_refuses_a_blob_location` asserts a `TypeError`. `docs/api/store.md:364-366` says
`CatalogAdmin` runs on local catalogs only and an incoming blob `commit` raises
`NotImplementedError`. None appears in the plan, and each PR fails `just ci` until they change.

**Fix:** List `tests/test_blob_storage.py` (Modify) in Part 1, replacing the two tests with "an
incoming entry reads its manifest through the fetch". List `tests/test_blob_open.py` (Modify) in
Part 2. Add `docs/api/store.md` to both Parts' Docs lines. Widen the rule at design:886-888 ("where
it names a new module or command") to cover statements a Part makes false.

### CB-3. Test files are not marked Create or Modify

**Sites:** the test tables at design:905-1012.

**Failure:** Only `tests/test_fold_lifecycle.py` carries "(Create)". These new files have no mark:
`tests/stores/test_admin_blob.py`, `tests/test_jobs_config.py`, `tests/test_cli_admin.py`,
`tests/stores/test_release_job.py`, `tests/test_release_fold.py`, `tests/test_release_step.py`,
`tests/test_release_job.py`, `tests/test_access.py`, `tests/test_cli_release.py`,
`tests/stores/test_gc.py`, `tests/test_gc.py`, `tests/stores/test_embargo_reader.py`,
`tests/test_embargo_reader.py`, `tests/stores/test_expiry_blob.py`, `tests/test_history_lease.py`.
Each new file needs a `pytestmark` (CLAUDE.md, GH-8).

**Fix:** Mark every test file Create or Modify, with its `pytestmark`.

### CB-4. The retirement move leaves the prune change builder and its fallback behind

**Sites:** design:595-602 (D9), 786; `admin.py:157-187`, `:640-666`, `:668-682`.

**Failure:** D9 moves only `_retirable`. The job also needs the code that turns the plan into one
`retire` version (`retire` per product, the head's tombstones plus the new ones, the
`_NothingToRetireError` no-op), which is the `change` closure in `CatalogAdmin._prune_retire`
(`admin.py:646-660`). `CatalogAdmin._cited` keeps every structure edition when a manifest cannot be
read (`except ManifestError: log.warning("prune keeps every structure edition: ...")`), and D9 calls
`cross_product.cited_structures` directly with no such fallback, so an unfilled entry with a missing
manifest stops the run. The signature differs in three places: D9 `(full, cutoff, *, kept)`,
Symbols `(full, cutoff, *, kept, skipped=())`, and today `(full, cutoff, skipped, kept)`
positionally, with two callers at `admin.py:642` and `:647`.

**Fix:** Move the change builder and the cited-set fallback with `_retirable`, for example
`retire_change(head, cutoff, *, kept)` and `kept_structures(full, artefacts)` in
`store/inventory.py`. Use one signature and list both callers.

### CB-5. The error contract of the fetch-backed `ManifestArtefacts` is unspecified

**Sites:** design:475-479, 781, 896-899; `store/cross_product.py:133-178`, `admin.py:674-682`,
`diagnostics.py:377-382`, `bench/corpus.py:164`, `:170`.

**Failure:** The design says only that a `Fetch` "reads `products/<p>/<e>/manifest.json` through it
and decodes with `decode_manifest`".

- A `Fetch` returns `None` for a missing file, and the design does not say that `None` and a digest
  mismatch still raise `ManifestError` as today (`cross_product.py:162-175`).
- `BlobInventoryStorage.read` raises `BlobStoreError`, and both callers that tolerate an unreadable
  manifest catch `ManifestError` only (`admin.py:676`, `diagnostics.py:381`).
- `decode_manifest` takes `(raw, source)`, and `source` needs a value.
- "`InventoryWriter` over any storage defaults to `ManifestArtefacts(storage.read)`" contradicts
  "a `Path` keeps today's local read". If the local default switches, `bench/corpus.py:170`
  (`self._root / ...`) breaks, and the bench also reads `self._cache` (line 164).

**Fix:** A `None` fetch or a mismatch raises `ManifestError` naming the path. `BlobStoreError` is
wrapped as `ManifestError`, or both callers catch it. `source` is `products/<p>/<e>/manifest.json`.
`LocalInventoryStorage` keeps `ManifestArtefacts(root)` and `BlobInventoryStorage` gets
`ManifestArtefacts(storage.read)`. `_root` and `_cache` both stay.

## Minor

### CB-6. The seal's rule-skip test reads other manifests directly

**Sites:** design:119, 908; `store/working_copy.py:1661-1666`.

**Failure:** `WorkingCopy._may_be_refused` calls `self._writer.artefacts(other, entry.edition_id,
entry.manifest_sha256)` for every other live data entry, bypassing `_records`. On a catalog with no
active structure edition a seal still opens another product's manifest when its entry is filled.
The Goal at design:119 and the Part 1 test row miss this caller.

**Fix:** Route `_may_be_refused` through the leaf records, for example a public
`cross_product.records(product_id, entry, artefacts)`, and add it to Part 1's Modify line.

### CB-7. More read sites change in `cross_product` than the design counts

**Sites:** design:470-473, 493.

**Failure:** `refusals` reads through `_homes` (`cross_product.py:248-255`) and `_Rules._read`
(`:275-276`, also used by `_no_home_copies`). `stale_editions` and `homeless_partials` call `_homes`
and the reader directly (`:460-463`, `:523-527`), and `cited_structures` reads at two sites (`:552`,
`:562`).

**Fix:** Name the six read sites, or say `_homes` and `_Rules._read` take `_records`. Correct "Four
functions change".

### CB-8. `commit` has no `now`, and the no-op paths need the existing sentinel pattern

**Sites:** design:394-404 (D4), 337-338 (D2), 445-447 (D5); `store/inventory.py:713-764`, `:667`;
`admin.py:125`; `store/working_copy.py:641`, `:1848`.

**Failure:** `commit(change, *, kind, reason)` takes no `now=`, `Change` has no time field, and
`try_commit` calls `self._storage.now()` itself (line 667), a second anonymous `HEAD` on blob. So
"`now` ... passed to `release_due` and to the root" (design:404) does not fit the signature. D4's
sketch returns `Change(leaves={}, extensions=...)` when held, and a returned `Change` always writes
a version, so "when it equals the head's, it writes nothing" needs an exception the caller catches.
The code has three: `_NothingToRetireError`, `_NothingToRepairError`, `_RecordedAlreadyError`. The
sketch's `reason=f"... {now:...}"` also uses `now` outside the function that defines it.

**Fix:** Add `now` to `Change` and pass it through `commit` to `try_commit`, listed in Part 3's
Modify line, or drop "and to the root". Write the held and unchanged paths with a sentinel
exception.

### CB-9. The fold re-specifies checks that existing helpers implement

**Sites:** design:310-319, 321-326, 345-347, 405-407, 624-627; `store/blob.py:626-661`, `:664-697`;
`upload.py:126-148`, `:249-254`; `store/working_copy.py:204-207`, `:848-888`.

**Failure:** The design describes in prose what these helpers already do:
`_decode_checked(raw, product_id, edition_id, source)` (decode and name check, D2's `manifest` row);
`_refuse_unsafe_ids` (ids from `pending/` LIST names; D2 checks only the edition id);
`_read_remote_manifest` (D4's and GC step 4's GET and SHA-256 check); `upload._lookup` and
`upload._store_path` (the `verify_remote` adapter, normalising `../data/`); `edition_kind(manifest)`
("the section's" kind); and `precheck_refusals(writer, full, planned)` with `Planned(...,
read=None)`, the drop loop D2 and D4 describe again.

**Fix:** Name these helpers in D2, D4 and D10, have the fold and release step call
`precheck_refusals`, and move the shared `upload` helpers, for example to `store/blob.py`.

### CB-10. The registration order is cited to the wrong decision

**Sites:** design:310.

**Failure:** "Two keys in D7's order" points at the jobs configuration. The order is fixed by
`upload._pending_document` (`upload.py:249-254`) and `docs/catalog-format.md` §1.5.

**Fix:** Cite §1.5 and `_pending_document`.

### CB-11. Test tiers in the plan differ from the files' markers

**Sites:** design:907, 909, 910, 1012.

**Failure:** `pytestmark` is per file, so a test runs at its file's tier.

| File | Plan says | File marker |
|---|---|---|
| `tests/test_inventory_routing.py` | unit | `pytest.mark.integration` (line 31) |
| `tests/test_cli_verify_doctor.py` | integration | `pytest.mark.unit` (line 31) |
| `tests/test_catalog_admin.py` | unit | `pytest.mark.integration` (line 39) |

The row "A seal's cross-product rules ... read no manifest" sits in
`tests/test_cross_product_rules.py`, whose scope is refusals over built leaves; seals live in
`tests/test_cross_product.py`.

**Fix:** Correct the tier column, or name files whose marker matches.

### CB-12. `CatalogAdmin` over blob needs changes the plan omits

**Sites:** design:666-672, 916-918; `admin.py:248-249`, `:325`, `:650`; `store/inventory.py:336-338`,
`:402-404`.

**Failure:** `__repr__` returns `f"<CatalogAdmin {self._writer.root}>"`, and `InventoryWriter.root`
raises `TypeError` on a non-local storage. `LocalInventoryStorage.now` is already
`datetime.now(UTC)`, so replacing `datetime.now(UTC)` at `admin.py:325` and `:650` with
`storage.now()` covers both stores with no branch. `advance_current` must return `None` "when
`CURRENT` was current", and `InventoryStorage.advance_current(root, data) -> None` reports nothing.

**Fix:** Make `__repr__` use `storage.location`, replace the two clock calls, and state how
`advance_current` learns whether it moved `CURRENT`, listing any protocol change.

### CB-13. Docs pages and nav entries the plan does not list

**Sites:** design:922-923, 941-942, 968, 987; `mkdocs.yml:103-122`; `docs/api/admin.md`;
`docs/reference/`.

**Failure:** `CatalogAdmin` has its own page, `docs/api/admin.md`, which Part 2 does not touch. Only
`docs/api/jobs.md` gets a nav entry, and `mkdocs build --strict` fails on pages missing from the nav
(CLAUDE.md:786-788). Each public module has a `docs/reference/*.md` page, and `jobs`, `release`,
`access`, `gc` and `embargo` get none.

**Fix:** Add `docs/api/admin.md` (Modify) to Part 2, give each new page its nav entry, and list
reference pages for the new modules.

### CB-14. `record_uploaded`'s default lifecycle makes the new rule unreachable

**Sites:** design:381-382, 902; `tests/stores/_stores.py:378-412`.

**Failure:** `record_uploaded(..., *, lifecycle: Lifecycle = "revisable", ...)`. If `lifecycle=` is
"kept as an override", every caller omitting it overrides `fold_lifecycle` with `"revisable"`.

**Fix:** Default to `None`, meaning "use `fold_lifecycle`".

### CB-15. Test infrastructure for the new blob routes

**Sites:** design:919, 794, 997-998; `tests/test_public_api.py:199-222`;
`tests/stores/_stores.py:267-342`, `:370-375`; `store/blob.py:371-378`.

**Failure:** `_ObjectOps.list` must load `obstore` through `_load_obstore`, and
`test_every_route_names_the_missing_extra` would need a `list` case. `RecordingOps` and `ReadingOps`
override `read`, `head`, `put` and `delete` only, so LIST calls go unrecorded.
`AzureBlobRest._send(path)` hard-codes `HEAD`, and `KeyedRest._send(path)` overrides that
signature, so Part 6's `.delete`, `.lease` and `.put` need a general `_send`.

**Fix:** Add a `list` route case to `tests/test_public_api.py` (Modify), `list` overrides to the two
ops classes, and the `_send` and `KeyedRest` change to Part 6.

### CB-16. On a 2.x catalog Part 3 can never commit

**Sites:** design:271 (step 1), 880-884; `store/inventory.py:570-575`.

**Failure:** `check_writable` decodes `catalog.json` with `decoder="stage-1-writer"`, so every commit
on an opted-in catalog raises `InventoryWriteRefusedError` until GH-125 stage 2 replaces the writer.
Part 3's step 1 has nothing to protect, and the run stops at the first commit. The compiler
confirmed the decoder call at `inventory.py:572`.

**Fix:** State Part 3's behaviour on a 2.x catalog ("stops at step 0 with
`InventoryWriteRefusedError`, exit 1"), and move step 1 to Part 6.

### CB-17. Environment reads and messages in the new commands

**Sites:** design:269, 688, 722, 795; `src/statcatlib/EGM.md:386-388`; `cli/__init__.py:635-694`;
`src/statcatlib/cli/EGM.md:83-84`.

**Failure:** `KubernetesPodVersions` lives in `release.py`, a library module, and an in-cluster
client reads `KUBERNETES_SERVICE_HOST` and the service-account token, against the invariant "A
library call never takes a credential from the environment". The CLI helpers D1 and D11 reuse
hard-code upload: `_catalog_uri` says "upload supports Azure only", `_azure_account` says "upload
writes with its own credential", and `_azure_location` sets the one-hour `_UPLOAD_TIMEOUT`.
`cli/EGM.md` says `upload` is "the only code in statcatlib that reads the environment for a store".

**Fix:** Build `KubernetesPodVersions` in the CLI and pass it to `ReleaseJob(pods=)`. Generalise the
helpers' messages and timeout, list them in Part 2's Modify line, and update the CLI EGM sentence.

### CB-18. The bench meter's meaning changes after Part 1

**Sites:** design:477, 891-903; `bench/corpus.py:126-176`; `bench/corpus-results.md:71`, `:159`.

**Failure:** `install_meter` counts manifest reads by wrapping `ManifestArtefacts.__call__`. After
Part 1 a filled entry reads none, so `corpus-results.md` no longer describes the code.

**Fix:** Re-run `bench/corpus.py` in Part 1, or mark the results as pre-Part 1.

### CB-19. Exporting `EmbargoReader` must keep the core import free of the blob extra

**Sites:** design:703, 986; `tests/test_public_api.py:179-188`.

**Failure:** `test_core_imports_without_the_blob_extra` re-imports `statcatlib` with `obstore` set to
`None`. An exported `embargo.py` that imports blob code at module level fails it.

**Fix:** State that `embargo.py` imports `store/blob.py`'s routes inside functions, as `catalog.py`
does.

## Claims verified

| Claim | Design line | Verdict |
|---|---|---|
| A blob `commit` with an incoming entry raises `NotImplementedError`, `store/inventory.py:411-417` | 36, 95-96 | Correct (raised through `_refuse`, 706-708) |
| `recorded_routing` fills `artefacts` for structure editions only, `store/working_copy.py:337` | 97 | Correct |
| `CatalogAdmin` runs on local disk only (Phase 4 W11) | 100 | Correct (`admin.py:223-246`) |
| `_ObjectOps` has no LIST and no conditional delete | 102 | Correct |
| A grep for `content_fingerprint` and `canonical_form` under `src/statcatlib` finds nothing | 104-106 | Correct |
| No code lists role assignments, reads pod versions or holds a lease | 107 | Correct |
| `record` requires `lifecycle`; the manifest has none | 91 | Correct |
| `working_copy` refuses a lifecycle change, `store/working_copy.py:999-1004` | 133-134, 366 | Correct |
| Storage clock through `BlobInventoryStorage.now()`, 30 s bound | 79, 269, 763 | Correct |
| `_refuse_unsafe_files` in `store/blob.py` | 187, 313 | Correct (634-645) |
| Registration is "two keys in D7's order" | 310 | Wrong reference (CB-10) |
| `verify_remote` through `AzureBlobRest.head`, as upload's step 5 | 315 | Correct |
| `check_edition(..., extensions={"statcat": _statcat_check()})` | 319 | Correct (`blob.py:412-414`) |
| `pull_edition` and `structure_editions` exist with those roles | 321-325 | Correct |
| `precheck_refusals`, `store/working_copy.py:848-890` | 347 | Correct (848-888) |
| The seal can pass `lifecycle=` to `catalog_section` | 363-366, 894 | Correct (one caller, `working_copy.py:1732`) |
| `_section_findings` ignores unread keys, `bundle.py:353-377` | 367-368 | Correct (335-377) |
| `record_uploaded` has `lifecycle=` | 381-382 | Correct (default `"revisable"`, CB-14) |
| `now` passed to `release_due` and to the root | 404 | Partly (CB-8) |
| `commit` re-applies `change` to each head, `store/inventory.py:713-760` | 415 | Correct (713-764) |
| Later versions carry the section unchanged, `store/inventory.py:269` | 448 | Correct |
| `ROOT_SECTION_FORMAT` is `"3"`; §1.2 tells readers to ignore unknown keys | 451-452 | Correct |
| `recorded_routing(fields, kind)` at `store/working_copy.py:322-337` | 466-467 | Correct |
| The seal's provisional entry has no `artefacts` | 473-474 | Correct |
| `bench/corpus.py:170` reads `ManifestArtefacts._root` | 477 | Correct (164 also reads `_cache`) |
| `verify-catalog` compares filled `artefacts` with the manifest | 480-481 | Correct |
| "Four functions change" | 493 | Partly (CB-7) |
| One manifest GET for each incoming entry | 488-489 | Wrong under D6's rule (CB-1) |
| `admin._retirable` at `admin.py:157-187` | 597-598 | Correct (`(full, cutoff, skipped, kept)`) |
| `CatalogAdmin.open(path)`; `EditionEntry` fields; method names for `statcat admin` | 666, 680-685, 698 | Correct |
| `current_is_older` exists | 670-672 | Correct (`inventory.py:307-312`) |
| `decode_manifest`, `edition_files`, `CANONICAL_FORM` in sdmxlib 0.87 | 313-314, 476, 625 | Correct (installed 0.87.0) |
| `tools/count_any.py --write`; `docs/catalog-format.md` §1.2, §3, §3.1, §3.3 | 888, 903, 942 | Correct |
| `docs/api/blob.md` exists; the four new pages do not | 922-987 | Correct (nav, CB-13) |
| Modify test files exist; Create files do not | 907-1012 | Correct (marks, CB-3; tiers, CB-11) |

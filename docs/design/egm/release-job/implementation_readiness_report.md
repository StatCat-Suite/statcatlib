# Implementation readiness report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, read against statcatlib `3306996`. Line
references are `design:N`. **Standard:** `implementation-readiness-standard.md` of the EGM plugin.

## Verdict

The design is not ready under the Zero-Decision Rule: 0 Blockers, 12 Majors and 12 Minors. The
architecture, the decision tables and most of the rule logic can be built as written. The gaps sit in
five places: test seams, output schemas, CLI wiring, a few undecided behaviours, and test rows whose
tier is wrong or that cannot pass as written.

- **Part 1** is closest to ready. It needs tier corrections, a list of the existing tests it breaks,
  and the `record_uploaded` override rule.
- **Part 2** needs a clock and actor seam on `CatalogAdmin.open`, and an owner for D11's per-commit
  lease sentence.
- **Parts 3 and 4** need the most: injection points, a rule for commits that change nothing, schemas
  for the report, the deletion log and the events, the CLI wiring, the exact reference-check call,
  and a fix for the refusal record when step 4 is skipped.
- **Part 5** needs a way to build an `EditionView`.
- **Part 6** depends on interface names that GH-125's unwritten local design will fix.

Every finding can be fixed in the design text, and none reopens an owner decision.

## What I read

- The persona, the standard, and the target in full.
- statcatlib `CLAUDE.md` and the `EGM.md` files for `src/statcatlib`, `store`, `cli` and `tools`.
- Code: `store/inventory.py` and `store/cross_product.py` in full; `store/working_copy.py`
  (195-500, 1555-1630), `store/blob.py` (160-660), `admin.py` (1-260 and `prune`'s commit),
  `upload.py` (120-354); `cli/__init__.py` by search.
- Tests: `tests/stores/_stores.py` in full; `pytestmark` in every test file; the tests that assert
  today's Phase 5 refusals.
- Configuration: `justfile`, `mkdocs.yml`, `pyproject.toml`, `ruff.toml`, `cliff.toml`, and the
  `docs/catalog-format.md` headings.
- The Phase 4 round 1 implementation readiness report.
- sdmxlib copies: `_rules.py` (140-540), `_routing.py`, `_check.py` (85-245), the GH-52 and GH-125
  texts in full; the plan (330-377) and spec (940-1029) in part.
- The hub's `inventory-history-log.md`, lines 330-540 and a search of the file.

## Limits

- Nothing was run, and there was no git.
- Not read: the sdmxlib#381, GH-134 and suite tracker texts, `_format.py`, `_reader.py`, the Phase 4
  design itself and its round 2 reports.
- What Azurite supports (blob leases, conditional delete), what `obstore.sign` can sign, and how ARM's
  `atScope()` behaves are general knowledge, unverified.

## Blocker

None.

## Major

### IR-1. Clock, actor and storage seams for the job objects

**Sites:** design:789, 791, 666-668, 785, 696, 854-855; rows 949, 954, 973-979; `store/blob.py:559-564`
(`now()` on a store with no `config`, such as `MemoryStore`, raises `InventoryWriteRefusedError`),
`:602-611` (no `TokenIdentity` and no `actor=` makes `actor()` raise); `tests/stores/conftest.py:47`,
`tests/stores/test_blob_writer.py:53` (every stores test passes `clock=` and `actor=`).

**Failure:** Each job object builds its own `BlobInventoryStorage` from a `BlobLocation`, and none
takes a clock, an actor or a storage. Over `MemoryStore` every unit row of Parts 3 and 4 stops at the
first `now()`. Over Azurite the store holds an account key, so `actor()` refuses for `ReleaseJob` and
`collect_garbage`. Row 954 (clock ahead) cannot be set up on `MemoryStore`, since `now()` refuses
before it compares clocks. GC's grace and window rows (973, 974, 979) need files older than 24 h or 35
d, and every Azurite or `MemoryStore` object is minutes old, so row 974 passes only because the grace
period protects the files. Each implementer would add a different private parameter.

**Fix:** Give each constructor a storage seam, for example `storage: BlobInventoryStorage | None =
None` on `ReleaseJob` and `collect_garbage`, and `clock=None` on `CatalogAdmin.open` beside `actor=`.
State that GC takes `now` from `storage.now()`. For row 954, state the setup: an `AzureStore`-shaped
config with `pytest-httpx` answering the anonymous `HEAD` with an old `Date`, as
`tests/test_blob_storage.py` does. For the GC stores rows, state a clock passed to GC, or short
windows such as `upload_grace = "2s"` with a sleep.

### IR-2. CLI wiring of the job dependencies, and the environment rule

**Sites:** design:269, 563, 722, 789, 938, 967, 282-286; `src/statcatlib/EGM.md:386-388`;
`cli/EGM.md:83-84`; `cli/__init__.py:671-684`.

**Failure:** The design does not say which layer builds `AzureBlobRest`, `AzureReaderListing` and
`KubernetesPodVersions`, or which credential object the listing takes (`DefaultAzureCredential`, the
`TokenIdentity`, or a token callable per scope). `KubernetesPodVersions` needs the service-account
token, the CA bundle and `KUBERNETES_SERVICE_HOST`, and reading them in `release.py` breaks the
library rule. "No API" has no detection rule (an absent variable, a refused connection, or a 403).
`statcat gc` has no exit-code table, so a failed deletion, a forecast deviation and an unregistered
edition have no code. Part 4 has no CLI test row, and Part 3's (962) does not say what it patches.

**Fix:** State that the CLI builds all three objects and passes them in, and name each one's
credential. Give the in-cluster detection rule and its module. Add a `statcat gc` exit-code table, a
`tests/test_cli_gc.py` (unit), and the patch targets for both commands' tests.

### IR-3. Output contracts with no schema

**Sites:** design:260, 276 (`--report FILE`, "as JSON"), 789, 791 (`ReleaseReport`, `GcReport`),
640-647 (deletion log, dry-run plan, `--forecast FILE`), 736-737 (event line), 282-286 (`REFUSED`,
`SKIPPED`), 545 (`jobs.config`).

**Failure:** Phase 8, the runbook and the next GC run will parse the report, the deletion-log line,
the plan `--forecast` reads back, and each D14 event, and the design gives no fields. All go to
standard output with the human summary and the `REFUSED` lines, so a dry run's output cannot feed
`--forecast` without a parser that skips the other lines.

**Fix:** Add a table (field, type, meaning) for `ReleaseReport` and its JSON, `GcReport`, the
deletion-log line (path, action `planned`, `deleted` or `failed`, reason, product, edition, error),
the plan file, and each event's payload. State which stream carries what, or give GC a `--plan FILE`
output that `--forecast` reads.

### IR-4. Symbols used with no module, signature, returns or raises

**Sites:** Symbols table, design:776-795; design:397 (`record_ext`), 559-560 (`ReaderReport`,
`AccessResult`), 774 (`ReleaseRefusal`, "a value"), 718-724 (`AzureHistoryLease`'s constructor,
`check()`, context manager use, the renewal thread's error path), 720 and 794 (`AzureBlobRest.delete`,
`.lease`, `.put`), 722 and 795 (`PodVersions`, no method), 726-729 (the blob expiry storage, no name),
787 (`_ObjectOps.list(prefix) -> Iterator[ObjectMeta]`; `obstore.list` yields batches, with a prefix
relative to the store's own), 324 (a blob version of `structure_editions`, which takes a local
`catalog_root` today), 623 (GC decodes registrations "as the fold decodes it", though Part 4 may land
before Part 3).

**Failure:** The implementer invents each. If Part 4 lands first, the registration decoder has no
module to live in.

**Fix:** Complete the Symbols table. Put the registration decoder and the manifest checks in a module
`release.py` and `gc.py` both import, built in whichever Part lands first.

### IR-5. A step with nothing to write still writes a version

**Sites:** design:278-280, 393-402, 445-447, 601; `store/inventory.py:742-762`; `admin.py:125-126`,
`:646-666` (`prune` raises `_NothingToRetireError` inside the callable).

**Failure:** On a held run, D4's sketch returns `Change(leaves={}, extensions=record_ext(...))`, and
`commit` writes that as a version even when the record equals the head's. After a lost race the fold,
release and retire steps can each find nothing to do. The design does not say how a callable tells
`commit` to write nothing, so the idempotency gating tests (rows 956, 977) depend on an unmade choice.

**Fix:** Either a private sentinel exception per step, caught around `commit`, as `prune` does; or
`InventoryWriter.commit` writes nothing when the callable returns `None`, with a docstring, a line in
`store/EGM.md` and a row in `tests/test_inventory_writer.py`.

### IR-6. Fold refusals are lost when step 4 does not run

**Sites:** design:115, 273, 445, 580-583, 279.

**Failure:** When the access check fails, the fold still refuses registrations, and only step 4 writes
the record, so nothing persists those refusals. The same happens when step 4 raises.

**Fix:** Decide who writes the record when step 4 is skipped: a record-only commit after step 3 or 5,
or the fold's `seal` version carrying the fold refusals. Add a row to `tests/test_release_step.py`
for a failed access check with a fold refusal.

### IR-7. The fold's reference-check call is left open

**Sites:** design:319, 321-331, 349-350; plan:363; sdmxlib `_check.py:203`, `:213-218`;
`upload.py:229-238`; `catalog.py:1015-1016`.

**Failure:** The design gives `remote=` and `lookup=` with no values and says the check needs no
`StoreSecret`. With `remote=`, the registry opens through `LocalRegistry.edition(root, remote=,
engine=)`, so the implementer must decide whether to pass `remote=`, its form, and whether opening it
needs an engine with the store's secret. Upload passes `lookup=` and no `remote=`. Whether the check
runs again inside the `commit` callable when a lost race changes the other structure products'
editions is also undecided; "reads of the first pass are cached by path" covers reads only.

**Fix:** Write the call as code with each argument's value, name the helper that lists and pulls the
structure editions on blob, and state the retry rule. If `remote=` is dropped, state how plan:363 is
met.

### IR-8. Test rows with the wrong tier, or that cannot pass as written

**Sites:** design:907, 909 (`tests/test_inventory_routing.py` marked unit; the file is integration,
`:31`), 910 (`tests/test_cli_verify_doctor.py` marked integration; it is unit, `:31`), 1012
(`tests/test_catalog_admin.py` marked unit; it is integration, `:39`); 907 (`record_uploaded` in a
non-stores file, though it uploads through `KeyedRest` to Azurite, `tests/stores/_stores.py:378-388`);
1006, 1008, 1011 (`AzureBlobRest.lease`, `.delete`, `.put` on Azurite, where `AzureBlobRest` sends
only bearer tokens and `KeyedRest` overrides only `_send(path)` for `HEAD`,
`tests/stores/_stores.py:370-375`, `store/blob.py:371-378`); 939-940 (the fake listing in
`tests/stores/_stores.py`, which no unit test imports).

**Failure:** A test file carries one mark, so a misplaced row never runs in the named tier or forces
an unlisted file. Part 1 also breaks `tests/test_blob_storage.py:727` and `:734` (they assert
`NotImplementedError` naming Phase 5) and `tests/test_inventory_routing.py:113`, `:133`, `:160` (they
assert a data entry's `artefacts` is `None`), and lists neither. The lease and delete rows need a
signing seam per method, which the design does not define.

**Fix:** Correct the tiers or name new files with their marks. Move the `record_uploaded` assertion to
`tests/stores/test_upload_store.py`. Widen the seam to `_send(method, path, headers)` and give
`KeyedRest` a signed URL per method. List the tests Part 1 rewrites. Put the fake listing in
`tests/conftest.py` or a support module.

### IR-9. The access listing and the new REST calls have no unit test

**Sites:** design:563-578, 588, 794, 854-855, 1004-1012, 1042-1043; `justfile:93-94` (`just coverage`
runs unit and integration only); `pyproject.toml` (`fail_under = 85`); `tests/test_azure_blob_rest.py`
(the `pytest-httpx` pattern).

**Failure:** Every test passes a fake listing, so none of its logic runs before the first real
release: the ARM and Graph requests, the `dataActions` wildcard and `notDataActions` rule, group
expansion, `allowSharedKeyAccess`, paging (`nextLink`, `@odata.nextLink`). The design gives no
`api-version` for the three ARM calls and no paging rule. `AzureBlobRest.lease`, `.delete` and `.put`
run only in the stores tier, which `just coverage` excludes. This code decides whether embargoed data
is exposed.

**Fix:** Add `tests/test_access_listing.py` (unit, `pytest-httpx`) with a row per rule, extend
`tests/test_azure_blob_rest.py` to the three methods, state the `api-version` values and the paging
rule, and state which new modules `just coverage` reaches.

### IR-10. `EmbargoReader` has no way to build an `EditionView`

**Sites:** design:693-709; `store/EGM.md:54-55` (`EditionView` is built only by `Product.view` and
`WorkingCopy.view`, and direct construction raises `TypeError`); `catalog.py:1018-1027`.

**Failure:** "A blob `view` runs `Product.view`'s steps" leaves three choices: extract those steps
into a shared function, build a private `Catalog`, or widen `EditionView`'s guard. Also unstated: what
`open` does with a `BlobLocation` lacking `cache_dir` or `secrets`, whether `EmbargoReader` is a
context manager, what `view` raises for an unknown product or edition, and whether `close` closes the
views it returned.

**Fix:** Name the extracted function and its signature, and add the refusals and lifecycle methods
to D12 and the Errors table.

### IR-11. Part 6 waits on names another design has not fixed, and has no server reachability test

**Sites:** design:716-729; hub `inventory-history-log.md:496-498` (step 1's ranged GET of a root's
first 1 KiB, absent from the listed interface), `:461-462`; design:673-674 against 719; GH-125
acceptance ("Expiry never deletes a leaf a retained root names, checked by an independent
reachability computation compared with the deletion log").

**Failure:** The expiry storage's methods, the `exclusive()` and `check()` contract, and the engine's
call order belong to GH-125's local design, which does not exist, so Part 6's signatures cannot be
fixed. The Part 2 implementer cannot tell whether to build D11's lease sentence. Part 6 adds
server-only code that decides which leaves are deleted (the leaf-set cache, `If-Match` deletes), and
no row checks reachability on blob.

**Fix:** Mark Part 6 provisional and plan a revision once GH-125's design merges. Move D11's lease
sentence to D13 and Part 6. Add rows to `tests/stores/test_expiry_blob.py` for an admin commit waiting
on a held lease, and an independent reachability check against the deletion log after a cached run.

### IR-12. A refused registration has no operator remedy

**Sites:** design:637-639, 151, 634-636, 282-286, 442, 752.

**Failure:** A registration refused for `files`, `manifest`, `chain`, `final` or `lifecycle` stays in
`pending/` until the tombstone window, 400 days in the example. Every release run exits 3 and keeps
the refusal in the record, and the orchestrator's "missed or failed" alert fires on every run. What
`--discard` does on a registered or a recorded edition is unstated.

**Fix:** Decide the remedy, for example `--discard p/e` on a registration the Inventory does not
record deletes the registration, then the directory with the manifest first, and on a recorded
edition refuses, naming `statcat admin cancel`. Add both cases to row 980.

## Minor

### IR-13. The handoff table and the land order disagree on what gates what

**Sites:** design:177, 497-546, 920, 379, 893, 182-183, 889.

**Failure:** Row 1 gates Parts 3 and 4 on Q1 to Q8, while Q3 decides D7 (Part 2) and Q7 decides
`fold_lifecycle` (Part 1). "Parts release in order" contradicts Part 4 landing before Part 3.

**Fix:** Gate Part 1 on Q7 and Part 2 on Q3, or split row 1 per question. Rewrite design:889's first
clause.

### IR-14. Part 3 on an opted-in catalog before Part 6

**Sites:** design:271, 260, 881; `store/inventory.py:570-575`.

**Failure:** Part 3 has no `AzureHistoryLease`, and every commit on a 2.x catalog raises
`InventoryWriteRefusedError`. The design does not say whether Part 3 refuses a 2.x catalog at step 0,
or whether it accepts `--state-dir` before Part 6 uses it.

**Fix:** State Part 3's behaviour on a 2.x catalog (refuse at step 0, exit 1, naming Part 6), and the
Part that adds `--state-dir`.

### IR-15. Docs pages, nav, reference pages, exports, `upgrading.md` and CHANGELOG

**Sites:** design:922-923, 941-942, 968, 987, 1002; `mkdocs.yml:103-122`; `CLAUDE.md:786-788`;
`docs/api/admin.md`; `src/statcatlib/EGM.md:5-12`; `cliff.toml:59-63`.

**Failure:** Only `docs/api/jobs.md` gets a nav entry, and the strict build fails without the others.
No `docs/reference/` page is named for the five new modules. Part 2 puts blob administration in
`docs/api/blob.md`, while the `CatalogAdmin` guide is `docs/api/admin.md`. The exports are open
(`ReleaseJob`, `ReleaseReport`, `collect_garbage`, `GcReport`, `JobsConfig`, `JobsConfigError`,
`LeaseLostError`). No Part names its `docs/upgrading.md` entry. D3's message names statcatlib 0.17,
which holds only if Part 1 ships as a `feat` commit released as 0.17.0, and no commit type is stated.

**Fix:** List nav entries, reference pages and exports per Part, move the blob administration text to
`docs/api/admin.md`, and give each Part's `upgrading.md` heading and commit type.

### IR-16. Jobs configuration details

**Sites:** design:499-546, 310, 517-524; spec:952-953.

**Failure:** Unstated: whether `[access]` and `[expiry]` are required, which `[expiry]` keys are
required with `enabled = false`, whether GC refuses a file lacking them, and whether `not_before` is a
string or TOML's native datetime. The example allowlist names three jobs, and a real deployment where
the pods, search pods, preview and pre-materialisation hold blob read fails every check. "D7's order"
should cite `upload._pending_document` and `docs/catalog-format.md` §1.5.

**Fix:** State the required keys per section and the accepted instant types, give an allowlist that
matches §10.1, and correct the reference.

### IR-17. The `Any` ratchet instruction allows growth

**Sites:** design:887-889; `tools/EGM.md:52-56`; `justfile:43-49`.

**Failure:** `tomllib.load` and the decoded ARM, Graph and Kubernetes JSON return `dict[str, Any]`,
and the instruction lets each Part raise the baseline.

**Fix:** State that `src/` gains no new `Any`; decode into typed values, and run `--write` only when
the count falls.

### IR-18. Missing test rows for listed behaviour

**Sites:** Parts 3 to 6 tables; plan:358, 360; GH-125 acceptance.

**Failure:** No row covers: the fold recording `artefacts`; a fold refusal from the cross-product
rules; the D14 event names and `jobs.config`; GC's dry run deleting nothing, the deletion log's lines,
and GC touching nothing under `inventory/` (a refusing store like `RecordingOps` would check it);
`_ObjectOps.list` over `MemoryStore` and Azurite; `EmbargoReader.view` of a `live` entry and
`refresh`; opt-in over a `BlobLocation` on Azurite; the 30-minute cap; the hook row 1008 needs between
step 6's final check and the delete.

**Fix:** Add each row with its file and tier.

### IR-19. Gaps in the acceptance map, and one wrong file

**Sites:** design:1027, 1033-1034; GH-52 acceptance.

**Failure:** The allowlist item cites `tests/test_access.py`, while row 955 asserts the stop in
`tests/test_release_job.py`. GH-52's own items are not mapped: the 404-from-every-pod item moved to
Phase 6 on 2026-11-02, and "a data edition whose partial gained a code is refused live until its
structure edition is live" is close to row 952. The GH-125 rows cite whole tables.

**Fix:** Cite `tests/test_release_job.py`, map GH-52's items to rows or their new phase, and map each
GH-125 item to one row.

### IR-20. Gaps in the error table

**Sites:** design:757-774; `store/inventory.py:479`, `:734-736`; `store/blob.py:761`; design:769.

**Failure:** In the fold and the release step, the rules read every unfilled entry's manifest, and one
missing manifest raises `ManifestError` out of the step with no stated exit code or scope. Also
missing: a structure edition that fails to pull for the reference check, `InventoryWriteRefusedError`
for a 2.x catalog, `InventoryCommitUnknownError`, GC's class for a missing root, `LeaseLostError`'s
base class. The table gives the non-Azure `TypeError` exit 2, which D1 reserves for usage errors.

**Fix:** Add a row for each and reconcile exit 2.

### IR-21. The override rule for `record_uploaded`

**Sites:** design:381-382, 902; `tests/stores/_stores.py:383`.

**Failure:** With a default of `"revisable"` the helper always overrides, so `fold_lifecycle` never
runs. With `None`, a stores fixture whose new product's edition has no section `lifecycle` is refused.

**Fix:** State `lifecycle: Lifecycle | None = None`, `None` meaning `fold_lifecycle`, and which callers
pass a value.

### IR-22. The 200-table fixture

**Sites:** design:939-940, 946, 1041; `testing.py` (`codr_product(..., tables=1)`, `codr_corpus`).

**Failure:** "200 tables" may mean 200 one-table products or fewer grouped ones. Whether the structure
products fold in the same fold or are recorded first is unstated, as are the fixture's run time in
the pre-push gate and what "records the request counts" asserts and where.

**Fix:** Give the fixture's signature, product and table counts, structure handling, and where the
counts go and what they are checked against.

### IR-23. Which leaf each fold check reads within one product

**Sites:** design:308-319, 332-343.

**Failure:** For two registrations where the second is built on the first, the `chain` check may read
the head's leaf (refusing the second) or the leaf after the first is recorded (accepting it).
`pending-fork` must also tell two new registrations on one `previous` from one beside an embargoed
entry, which goes to `supersede_pending`.

**Fix:** State the leaf each check reads and the order of checks within a product.

### IR-24. Small items in D8, D13 and the extras

**Sites:** design:566-578, 722, 1042; `pyproject.toml:23-37`; `cli/EGM.md:124-125`.

**Failure:** Gate 2's "below 0.85" needs a version parser, and `packaging` is no dependency; a pod
with no `podIP` has no outcome. Which `transitiveMembers` types count, and the wildcard rule's case
and segment semantics, are unstated. So are the extra each command needs and whether the new modules
import `obstore` inside functions only.

**Fix:** Name the version comparison and the no-IP outcome, give the member-type and wildcard rules,
and name each command's extra and the lazy-import rule.

## Gating test coverage

| Plan item | Design test row | Verdict |
|---|---|---|
| A 200-table synthetic release becomes visible in one Inventory version | design:946, stores | Row exists; fixture undefined (IR-22); two runs need a clock seam (IR-1) |
| The fold runs `check_edition(at="receive", remote=, structures=)` on a no-bindings edition | design:947, stores | Row exists; call arguments undecided (IR-7) |
| An interrupted release leaves every entry `embargoed`; a re-run releases them | design:948, stores | Row exists; crash injection point unstated |
| The fold refuses two pending editions on one live edition and one older than the tombstone window | design:949, unit | Row exists; age case needs a clock seam (IR-1) |
| A stale automatic release after embargo was turned on is refused | design:950, unit | Covered |
| A non-fast-forward edition is refused, naming both, and the rest goes live | design:951, unit | Covered |
| A data edition whose structure edition is not due stays `embargoed` | design:952, unit | Covered |
| A new edition of a `final` product with a different `content_fingerprint` is refused | design:953, unit | Covered |
| A release run with its clock ahead is refused | design:954, unit | Cannot pass over `MemoryStore` as specified (IR-1) |
| A principal outside the allowlist stops the release | design:955, unit | Covered; the map cites the wrong file (IR-19) |
| GC deletes exactly the unreferenced files; keeps a racing upload's and a rolled-back-to edition's | design:972-973, stores | Rows exist; grace and window need a seam (IR-1) |
| GC keeps a `pending/` edition's files and fails closed | design:974-975 | Rows exist; row 974 passes for the wrong reason without a seam (IR-1) |
| GC refuses to start with backup retention longer than the GC window | design:976, 930, unit | Covered |
| `advance-current` moves a lagging `CURRENT` forward and never back | design:927, stores | Row exists; `CatalogAdmin.open` lacks a clock seam (IR-1) |
| A re-run of each job changes nothing | design:956, 977, stores | Rows exist; depend on the no-op rule (IR-5) |
| Plan bullet: GC has a dry run and logs every deletion | none | Missing (IR-18) |
| Plan bullet: GC never touches `inventory/` | none | Missing (IR-18) |
| Plan bullet: the jobs log each §11.6 alert as a structured event | none | Missing (IR-3, IR-18) |
| Plan bullet: the sweep of sealed but unrecorded editions | design:980, unit | Covered |
| GH-125 part 4: every entry carries `artefacts`; the rules open no filled entry's manifest | design:907-909 | Tier mismatch, `record_uploaded` misplaced, fold missing (IR-8, IR-18) |
| GH-125: `verify-catalog` detects differing `artefacts` | design:910 | Tier mismatch (IR-8) |
| GH-125 part 3: a second release run fails to take the lease | design:1006, stores | Needs a signing seam on Azurite (IR-8) |
| GH-125 part 3: a failed renewal stops expiry | design:1007, unit | Covered, subject to IR-11 |
| GH-125 part 3: a leaf rewritten after the final check survives the `If-Match` delete | design:1008, stores | Needs the seam and a hook (IR-8, IR-18) |
| GH-125 part 3: the pod check skips expiry and the release still runs | design:1009, unit | Covered, subject to IR-2 and IR-11 |
| GH-125: expiry never deletes a leaf a retained root names, on the server | none | Missing (IR-11) |
| GH-52: an embargoed dataflow returns 404 from every pod | none | Moved to Phase 6 on 2026-11-02, unrecorded (IR-19) |
| GH-52: a data edition whose partial gained a code is refused live until its structure edition is live | design:952 | Close; not in the map (IR-19) |

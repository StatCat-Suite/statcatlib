# Implementation readiness, round 2: catalog on blob storage, revision 3

**Target:** `/Users/gabrielgellner/dev/statcatlib/docs/design/catalog-on-blob-storage.md` ("design"), all 1150 lines.
**Standards read:** `/Users/gabrielgellner/.claude/plugins/cache/egm/egm/0.1.0/agents/implementation_readiness.md` and `/Users/gabrielgellner/.claude/plugins/cache/egm/egm/0.1.0/resources/document_standards/implementation-readiness-standard.md`.
**Round 1 report read:** `/Users/gabrielgellner/dev/statcatlib/docs/design/egm/catalog-on-blob-storage/review_report.md`.

**Result: 0 Blockers, 3 Majors, 14 Minors.** The `NotImplementedError` does not stop any test row from running. The three Majors are all small fixes to the text: the blob endpoint, the CLI test seam and the lost-race row. Nothing was run; every finding comes from reading.

"Read" below means I read the cited code. sdmxlib paths are the working tree at `docs/phase-4-restated`. That tree's `pyproject.toml:3` and the top of `CHANGELOG.md` both say 0.85.0, and `edition/_check.py` has no `references` argument. This fits #371 being unmerged. I cannot confirm that its `src/` equals `11f18e3f`.

## Majors

### IR-1. `AzureBlobRest` has no way to find its URL, so the production path stops at the digest read
- **Sites:** design:698-704, 723-724, 467; `/Users/gabrielgellner/.cache/uv/archive-v0/CrCci5PwDHbOclfm/obstore/_store/_azure.pyi:354-355, 440-451`.
- **Gap in D7:** it says `head(path)` sends `Get Blob Properties`, but never says how the URL is built from account, endpoint, container and prefix.
- **Gap in D4:** it says `now()` "takes the endpoint from `store.config`", yet CUJ 2's store (design:204) sets `account_name` and no endpoint, so a default must be derived. Nothing states that derivation.
- **Failure scenario:**
  - The CLI calls `AzureStore.from_url(AZ_URL, credential_provider=..., client_options=...)` and leaves the account to `AZURE_STORAGE_ACCOUNT_NAME`.
  - The stub says constructors check the environment. It documents `.config` only as "the underlying Azure config parameters".
  - If environment values are not reflected in `.config`, which I could not check, `store.config["account_name"]` fails or is absent. `upload_edition` then fails at step 5, after every file is sent and before registration.
  - The `pytest-httpx` row (design:1042) builds its own store with an explicit `account_name`, so it passes. Checklist item 6 asks only whether `from_url` "resolves as D7 says", and D7 does not say.
- **Checked:** the stub and the design by reading. Rust behaviour unverified.
- **Fix:**
  - Have the CLI read the variable and pass `account_name=` to `from_url`.
  - Define one resolver in Part 1, used by both `now()` and `AzureBlobRest`: `config["endpoint"]`, else `https://<account>.blob.core.windows.net`.
  - Take container and prefix from `store.config`, `store.prefix` or `location.uri`, and name which.
  - State the `S3Store` default, and that `MemoryStore` with no `clock=` refuses.
  - Add a row asserting the exact URL with a prefix.

### IR-2. The CLI rows have no seam and leave the production wiring untested
- **Sites:** design:1044, 723, 728; `src/statcatlib/cli/EGM.md:26-27`.
- **What goes wrong:** `success`, `already registered` and `findings exit 1` are listed as unit `CliRunner` rows, but nothing says what replaces the store.
  - The command builds a real `DefaultAzureCredential`, and its output line needs `oid`. That fetches a token, which is network I/O in a tier defined as having none (`pyproject.toml:83`).
  - The implementer has to invent a patch point. The natural one replaces `upload_edition` whole.
- **Failure scenario:** the CLI passes a bare `AzureCredentialProvider` without `TokenIdentity`, or drops the one-hour timeout. Every row passes, and production raises D7's `ValueError`. Only the manual checklist would show it.
- **Also missing:** no row covers the output line, which D7 relies on to expose an unintended identity (design:728, 733, 936).
- **Checked:** by reading.
- **Fix:**
  - Name the seam: patch `upload_edition` and the credential class, and capture the `BlobLocation` passed.
  - Assert `isinstance(location.store.credential_provider, TokenIdentity)`, `location.store.client_options`, `location.store.prefix` and `location.uri`.
  - Assert that the `oid` is printed.

### IR-3. The lost-race row can pass without testing the acceptance item
- **Sites:** design:1000, 956, 1134; `src/statcatlib/store/inventory.py:595-613`; `tests/test_inventory_history.py:208-239, 254`; `src/sdmxlib/inventory/conformance.py:570-586`.
- **It can run:** `InventoryWriter(storage, artefacts=lambda _p, _e, _s: {})` is already the pattern at `test_inventory_history.py:254`, and `_refuse` calls the injected reader.
- **The problem:** "two writers on one base" and Land order's "a `commit` with no incoming entry or an injected `artefacts=`" allow three tests, and only one proves "recomputes `changes` and `previous_root_sha256`" through the writer's loop:
  - **Two `try_commit` calls:** the loser raises, and the test retries by hand. This repeats `check_a_lost_race_rebuilds_its_history_fields`, which `run_history` already runs, and never enters `commit`'s loop on blob.
  - **`commit` with no incoming entry, such as `Change(leaves={}, embargo=True)`:** `changes` is empty for winner and loser, so the `changes` half proves nothing. A no-incoming change with a changed leaf needs an embargoed or superseded setup first.
  - **`commit` with `artefacts=` and a race injected as `_race_once` does:** this is the local test's shape, and the only one that exercises the loop.
- **Checked:** by reading.
- **Fix:**
  - Rewrite the row: "`commit` on a writer with `artefacts=lambda *_: {}` for a new product, with a second writer winning inside the first `try_commit`; the loser's `previous_root_sha256` hashes the winner's stored bytes and its `changes` name only its product."
  - Note that `_race_once` lives in a test module and needs a copy or a move.
  - Delete the "no incoming entry" option from design:956.

## Minors

### Test rows and the fixture path

**IR-4. The `record_uploaded` path is complete, but its writer and `now` have no stated source.** Sites: design:764, 769-791; `sdmxlib/inventory/_rules.py:178-189`.
- **Your question:** yes, it can run against Azurite with `AzureStore` and no `TokenIdentity`. `_upload(..., rest=)` bypasses both `upload_edition` refusals, provided the `AzureBlobRest` constructor does not itself require a `TokenIdentity`. D7 places that refusal in `upload_edition`, so this holds.
- **Actor:** the actor is given only for `BlobBackend` (design:764). `record_uploaded` takes a `writer` built by a fixture that no text names. Without `actor=`, the commit raises `InventoryWriteRefusedError`. Without `clock=`, the real `HEAD` runs, which is the lagging-VM failure D8 avoids for conformance. Parts 2, 3 and 4 all need this writer.
- **`record` arguments:** the source table omits `now` (required), `leaf` and `product_id`. With `writer.now()` gone, `now` has no source.
- **Fix:** add a `writer` fixture to `tests/stores/conftest.py` with a fixed actor and clock. Add rows for `now` (`datetime.now(UTC)`), `leaf` and `product_id` (the head's leaf and the manifest).

**IR-5. A literal port writes a leaf before it refuses for a missing actor.** Sites: `inventory.py:532-540`; design:443, 1010.
- `try_commit` writes leaves at line 534 and reads the actor at line 540. D3 says the refusal comes "before anything is written", D1 does not say to move the read, and the row asserts only that it refuses.
- **Fix:** resolve `storage.actor()` before the leaf loop, and assert in the row that nothing was written.

**IR-6. The second-upload row contradicts step 4.** Sites: design:669, 679, 1033.
- Step 4 is an unconditional `put(mode="create")`, so a rerun sends the manifest bytes and gets a refusal. A recording `_ObjectOps` sees that `put`, so a test of "sends no file again" fails as written.
- **Fix:** either `head` first in step 4, or define "sent" as an accepted create and have the row assert on `UploadReport`. State how step 4 gets the stored bytes it compares.

**IR-7. The `[blob]`-only view case has no runnable row.** Sites: design:423-424, 575-576, 1020, 1043; `src/statcatlib/bundle.py:228-237`.
- Part 2's row says "for upload and view", but a blob `view` does not exist until Part 3, and Part 3 has no such row.
- **Failure scenario:** Part 3 passes plain `check_catalog`. On a `[blob]`-only install, `_check_search` now yields a finding, and every view of a product with `search/` raises `IncompatibleCatalogError`.
- D2's chooser function also has no name, and Part 2's `store/blob.py` entry and the Symbols table omit it.
- **Fix:** name the function, list it in Part 2, and add a Part 3 row.

### D7 and the command line

**IR-8. The CLI's exit-code table does not cover every error it can meet.** Sites: design:729, 815, 1044; `src/statcatlib/cli/__init__.py:26`.
- `ManifestError` (unsealed directory) and `ImportError` (missing extra) are in neither listed class, and "store or credential fault" names no exception types. They reach Typer as uncaught tracebacks.
- The "missing extra" row gives no code. A base install fails earlier, at `import typer`, with no hint naming an extra, and the dev environment cannot exercise that case.
- **Fix:**
  - List the classes caught for exit 1.
  - Give `ImportError` a code.
  - Say that a no-extras install keeps today's import failure (`cli/EGM.md:16-17`).
  - Check that an uncaught fault cannot print a token, given the design's "A token never reaches a log" (design:905). I did not verify Typer's default for printing locals.

**IR-9. `AzureBlobRest.head` and `create`'s "transport error" are undefined (M20 residue).** Sites: design:356-361, 803, 1042; `obstore/exceptions/__init__.pyi:12-54`.
- The Errors table has no row for a 401, 403 or 5xx on `head`. An implementer could return `None`, so a missing role reports as "missing from the store", or could let `httpx.HTTPStatusError` escape.
- `obstore` has no transport exception class, only `GenericError` beside the specific ones. So the design does not say which `BlobStoreError` causes trigger `create`'s re-read and which lead to `InventoryCommitUnknownError`. `PermissionDeniedError` is the clear case to exclude.
- **Fix:** add a table row for `head`, and name the classes for `create`.

**IR-10. `TokenIdentity.token()` may go stale on a long upload.** Sites: design:436, 701, 723.
- D3 does not say whether `token()` calls the provider or returns the last token seen. If it returns the last token, a long upload can reach step 5 with an expired token and fail after sending everything.
- **Fix:** state that `token()` calls the provider on each use. `AzureCredentialProvider.__call__` asks the credential every time (`obstore/auth/azure.py:86-96`).

### Handoff and order

**IR-11. Row 2 disagrees with the Land order.** Sites: design:167, 172, 174, 176, 966, 968, 974.
- Row 2 ("Part 1, then Part 4", gates Part 2) makes Part 4 a gate on Part 2. The Parts table has Part 2 waiting only on Part 1, and design:974 says Part 4 "may land before".
- If Part 2 lands first, Part 4 cannot land on 0.85, which W13 requires. Part 4 needs nothing from #371, so this is a wording conflict, but the implementer must choose.
- Rows 4 and 6 have smaller order problems. Row 4 says a change made inside Part 2's PR gates Part 2. Row 6 sits after row 5 in a table headed "In order", though it needs only Part 4.
- Part 1 waiting on row 1 and gating Part 2 is consistent.
- **Fix:** split row 2, and say that Part 4 is on whatever cap is current.

**IR-12. "S16 alone" is not something statcatlib can guarantee.** Sites: design:12, 173, 174, 287, 1018; `pyproject.toml:30-32`.
- The design's own header places `origin/main` "after 0.85.0", so a release cut from main carries whatever else has landed. The pin comment says a minor bump may break.
- The design does not say who absorbs a break, or whether a 0.85.x patch release counts, in which case the floor rises and the cap does not. "Raise the cap" gives no pin text.
- I could not read the commits after 0.85.0, so I cannot say whether this is real today. W12 is settled, so this finding asks only for a fallback.
- **Fix:** add one sentence: Part 2's PR sets the exact pin and fixes any break from other contents of the release.

**IR-13. No row owns the spec text for S16.** Sites: design:171, 173, 178; sdmxlib spec `docs/design/sharded-bundle-catalog.md:272, 283`; plan `docs/design/sharded-bundle-catalog-plan.md:328, 363`.
- At `02d3af2a` the plan records #371 and the fold's check. The spec does not:
  - §4.6 says `references` runs at `receive`.
  - §4.7 says any stamp for the same manifest means `open`, which the design's deferred-stamp rule contradicts.
- Row 1 lists other items, and row 3 reads as code.
- **Fix:** add the spec change to row 3 or row 7, or confirm #371 carries it. I need the issue text to know which.

**IR-14. The "Read against" and plan-status text is stale at `02d3af2a`.** Sites: design:13, 24, 184-186; plan:326, 334.
- Lines 185-186 say the plan still needs `verify_remote` and the moved scope. The plan already has both.
- The contents of row 1 are all present on the branch: S2 (spec:370), S3 (spec:569), S7 (spec:937) and W4 (spec:525).
- Row 1 still stands, because the branch has not merged.
- **Fix:** update the commit and delete the plan half of lines 185-186. GH-98's half stays out of scope, as you said.

### What moved out

**IR-15. `CatalogAdmin` on blob has no owner, and S9 and S11 have no definition in this revision.** Sites: design:65, 126, 180, 391, 845-848, 954.
- "A later phase" names none, and Moved out's list of what GH-130 takes omits `CatalogAdmin.open(BlobLocation)`.
- Yet line 954 names `correct` and `reinstate` as its callers, and line 391 puts GH-125's opt-in refusal in `CatalogAdmin`. So GH-125 or GH-130 needs it.
- A blob `correct` would also depend on Phase 5's reader if it commits incoming entries, and the design does not state this.
- S9 and S11 are cited at lines 180 and 847, but their definitions exist only in revision 2.
- **Fix:** add to Moved out: `CatalogAdmin.open(BlobLocation)` with `writer.now()`, `prune`'s refusal and `repr`, an owner, and a pointer to S9 and S11 at `e7eba47`.

**IR-16. A second stale "Phase 4" promise was missed.** Sites: `src/statcatlib/store/working_copy.py:1002`; design:873-874, 990.
- A runtime error says changing a lifecycle "is not supported before Phase 4", and Phase 4 does not build it. It is the same kind of promise as `admin.py:364-366`, which Part 1 fixes.
- A grep of `tests/` and `docs/` found no use of the wording, so the change is safe.
- **Fix:** add it to Part 1.

### Part 4 (m22 residue)

**IR-17. `bench/inventory_read.py` cannot import `tests.stores._stores` when run as written.** Sites: design:1095-1096, 1148; `bench/corpus.py:29-31, 59-63`.
- `uv run python bench/inventory_read.py` puts `bench/` on `sys.path`, and the package builds only `statcatlib`. So I expect `ModuleNotFoundError`. `bench/corpus.py` imports only `statcatlib`, so nothing has tried this yet.
- `bench/__init__.py` and `tests/__init__.py` both exist.
- **Fix:** use `uv run python -m bench.inventory_read` from the repository root.

**IR-18. `inventory_corpus` omits values the determinism row needs.** Sites: design:1086-1094, 1118; `_rules.py:178-189`; `sdmxlib/inventory/EGM.md:113`.
- Not given: `kind`, `lifecycle`, `schema_version`, the edition id form, and `now`.
- `LiveInventory` holds only entries at its schema version, so the wrong `schema_version` makes the read count nothing.
- "Determinism by seed" fails if `now` comes from the clock, since `state_changed_at` is in the leaf bytes.
- **Fix:** state these values, with `now` fixed and the root's time still taken from the writer. `corpus_embargoed` does exist (`testing.py:556`).

## Per-area verdict

**1. Test rows against the `NotImplementedError`: all can run, with one row needing a rewrite (IR-3).**

| Rows | Writer path | Needs `commit` with an incoming entry |
|---|---|---|
| Part 1: conformance, kill, 2.x | `try_commit` | No |
| Part 1: 8 MB root, 5 MiB + 1 leaf, `CURRENT`, create, clock | `storage` or `_ObjectOps` directly, or `try_commit` | No |
| Part 1: lost race | `commit` | Yes, with `artefacts=` (IR-3) |
| Part 1: `NotImplementedError` row | `commit` | Yes, deliberately with no `artefacts=` |
| Part 2 and 3: `record_uploaded`, including embargo and tombstone setups | `try_commit`, with `try_commit(embargo=...)` and `retired=` for setup | No |
| Part 4 | `try_commit` | No |

- **Conformance:** I walked all 8 `CHECKS` and all 5 `HISTORY_CHECKS` against D8's `BlobBackend`.
  - Every path is `try_commit` or backend hooks. The `correct` and `reinstate` kinds are plain `try_commit` kinds using sdmxlib's rule functions, with no `CatalogAdmin` involved.
  - An explicit `now=` bypasses the clock, the 2.x check raises an `InventoryError` subclass, and `restore_to` with `ops.delete` and `commit_interrupted` writing the leaf alone both satisfy their checks.
  - No findings on conformance.
- **`NotImplementedError` placement:** D1 does not say where it is raised. A default reader that raises works, because `refusals` reads no manifest without an incoming entry (`cross_product.py:395-397`), and it raises before any write. I do not count this as a finding.

**2. D7: the path is not yet complete (IR-1).**
- `typer` and `rich` in `blob` is sound. The `statcat` entry point is always installed, `cli/__init__.py` imports neither `lancedb` nor an encoder at module level, and the two extras and the dev group agree on versions.
- The file list is complete: extra, CLI wiring, `cli/EGM.md`, exports and docs.
- The CLI rows do not yet test what is stated (IR-2, IR-8).

**3. Handoff and Land order: every dependency has an owner, and the order works.**
- The sdmxlib versions hold for each part, since Parts 1 and 4 use nothing from #371.
- Rows 8 to 10 are sound. Plan:363 backs row 8, plan:326 backs row 10, and `_check.py:262` confirms the `remote=` refusal row 8 cites.
- Row 6 matches the acceptance map.
- Findings are text-level only: IR-11 to IR-14.

**4. Moved-out work: nothing in Phase 4 still needs it, apart from IR-15 and IR-16.** Clean checks:
- Symbols table.
- `writer.now()`, apart from IR-4's `now`.
- S6 and `decode_manifest`, which exists at `storage/_manifest.py:471`.
- `bench/corpus.py:170`, which still works since `ManifestArtefacts(root)` is unchanged.
- `check_writable`.
- `admin.py:364-366`, which Part 1 now owns.
- The D6 embargo section, which does not use `CatalogAdmin`.
- Risk and NFR rows.
- Plan:322-346 against the Parts: four parts, the rig and all eight gating tests match.
- The moved-out findings list: from their round 1 entries, B2, M1, M2, M4, M10, M13, M16, M26, M34 to M36 and m1, m6 to m9, m19 and m24 all concern `correct` or `reinstate`. I had no revision 2 text to compare them against.

**5. Round 1 closure: everything is closed except residue in three.**
- **B4:** the credential, grammar and rows are done. IR-1 and IR-2 remain, and IR-8 partly.
- **M20:** the table exists. IR-9 remains.
- **m22:** Azurite, test file and PR owner are done. IR-17 and IR-18 remain.

## Could not verify

- Whether `AzureStore.config` reflects values read from the environment or from `from_url` (IR-1). Only the stub is on disk, not the Rust source.
- Whether `store.credential_provider` returns the same Python object. D3 step 1 and D7's `ValueError` depend on it, and `isinstance` fails otherwise. The `AzureBlobRest` unit row would catch a difference.
- Whether `obstore.sign` honours a store's prefix. The test subclass (F8) runs under the uuid prefixes D8 gives conformance stores. It is a small fix in a test if it does not.
- Whether `import azure.identity.aio`, run at `obstore/auth/azure.py:12`, succeeds on a `[blob]`-only install without `aiohttp`. I could not find `azure-identity` on disk to read. A failure would break every `statcat upload` on that install, and the dev environment could hide it.
- Typer's default for printing locals in uncaught tracebacks (IR-8).
- Whether `filterwarnings = ["error"]` trips on `obstore`, `azure-identity` or the tier's subprocess use.
- Whether sdmxlib `origin/main` holds source commits after 0.85.0 (IR-12).

**Issue text I would need:**
- **sdmxlib#371:** whether it includes spec text, the exact stamp rule, and the release vehicle.
- **statcatlib#52:** whether it carries `ManifestArtefacts(fetch)`, handoff row 8 and S6.
- **statcatlib#130:** whether it takes `CatalogAdmin.open(BlobLocation)`.
- **statcatlib#98:** I took its acceptance text from the design's map.

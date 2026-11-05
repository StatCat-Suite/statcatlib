# EGM review report, round 2: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 3, branch
`docs/design-catalog-on-blob-storage` at `1f2deea`. The review covers six named areas and leaves
the rest of the document alone.

**Read against:** statcatlib working tree at `1f2deea` (code equal to `origin/main` `974ea27`, by
the brief). sdmxlib working tree at `docs/phase-4-restated` `02d3af2a`, for spec, plan and code.
The refs on disk confirm both commits and that sdmxlib `origin/main` is `11f18e3f`.

**Reviewers:** design critic (`DES`), codebase details (`CB`), implementation readiness (`IR`). Their
full reports sit beside this file. The owner's decisions W1 to W23 were treated as settled.

**Counts after deduplication:** 0 Blockers, 4 Major, 20 Minor, from 34 raw findings. `IR`'s report
says 14 Minors and lists 15; this report counts 15.

**Verdict.** The design can be implemented once the 4 Majors are fixed, and each Major is a small
edit to the text. No reviewer found a broken embargo or data guarantee. Conformance runs through
`try_commit`, and the `NotImplementedError` stops no test row from running. `statcat upload` has a
defined path from the shell to the registration, except for how `AzureBlobRest` finds its URL (M2).

**Evidence limits, which apply to every finding.**

- Nothing was run. Each finding comes from reading.
- No reviewer and not the compiler could run `gh` or `git`. So StatCat-Suite/statcatlib#98, #130, #52
  and StatCat-Suite/sdmxlib#371 **were not read**. Area 2 reviewed #371's stamp rule only as design:591-593
  states it, and some of m4 may already be answered in the issue.
- Nobody confirmed that the sdmxlib working tree's `src/` equals `11f18e3f`. Its `pyproject.toml` and
  changelog say 0.85.0, and `check_edition` has no `references` argument, which fits.

**Compiler's spot checks, by reading.** `sdmxlib/edition/__init__.py:91,149` (M1);
`statcatlib/catalog.py:942-947` (M1); `_store/_azure.pyi:354-355,387-404,440-445` (M2);
`store/inventory.py:532-540` (m10), `:595-613` (M4); `tests/test_inventory_history.py:208`,
`tests/test_catalog_admin.py:71` (M4); spec lines 272 and 283 (m4); `store/working_copy.py:1002` (m20).
Each supports its finding. No severity was changed from what the reviewers reported.

Line references are `design:N` at `1f2deea`.

## Blockers

None.

## Major

### M1. Part 3 needs a bytes-to-`Manifest` decoder, which W12 and S6 defer to Phase 5

**Reviewers:** `DES-1`, `CB-1`. **Area:** 2 and 4. **Checked by reading.**
**Sites:** design:161, 173, 287, 580, 840, 1076; `sdmxlib/edition/__init__.py:91,149`;
`sdmxlib/storage/_manifest.py:442,471,479-498`; `statcatlib/catalog.py:942-947`.

**Failure.** `Product.editions` returns `manifest.previous` (`catalog.py:947`), and D6 says it reads the
manifest "by a GET and a SHA-256 check, with no pull". Row 1076 tests that it pulls nothing. On an uncached
edition it holds bytes and no directory. `read_manifest` takes a directory, `decode_manifest` is exported
from neither `sdmxlib.edition` nor `sdmxlib.storage`, and statcatlib imports no private sdmxlib module today.
The Part 3 implementer must pick among a private import, a temporary directory, and a hand parse. A hand parse
skips `decode_manifest`'s format-version refusal. `pull_edition(..., manifest_bytes, dest)` has the same need,
and can write the bytes into its staged directory first.

This is reportable under W12 because S6 says Phase 5 is the first caller, and the code shows Part 3 is. W12 can
still stand: the temporary-directory route uses only public API.

**Fix, the owner picks one.**

1. Keep W12. State in D6 that `pull_edition` writes the bytes into the staged directory and calls `read_manifest`,
   and that `editions` does the same in a temporary directory under `cache_dir`.
2. Add the `decode_manifest` export to the #371 release, which Parts 2 and 3 already wait on. The release order
   does not change, and this edits W12's "only" and handoff row 3's "S16 alone".

### M2. `AzureBlobRest` and `now()` have no defined way to find the blob URL

**Reviewers:** `IR-1`, `CB-2`, `DES-7`. **Area:** 3. **Checked by reading the stubs and the design; the runtime
content of `store.config` is unverified.**
**Sites:** design:204, 467, 698-704, 723-724, 837; `_store/_azure.pyi:354-355,387-404,440-445`.

**Failure.** D7's store is `AzureStore.from_url(AZ_URL, credential_provider=..., client_options=...)`, with the
account left to `AZURE_STORAGE_ACCOUNT_NAME`. `AzureBlobRest(location)` must build
`https://<account>.blob.core.windows.net/<container>/<prefix>/<path>`, and the design states no derivation. D4
says only that `now()` takes the endpoint from `store.config`, and CUJ 2's store sets no endpoint. The stub says
constructors check the environment and describes `.config` only as "the underlying Azure config parameters". If
`.config` omits a value read from the environment, every production `statcat upload` fails at step 5's `HEAD`, after
every file is sent and before registration. The `pytest-httpx` row (design:1042) and the Azurite subclass use a store
with explicit config, so both pass. Checklist item 6 asks whether `from_url` "resolves as D7 says", and D7 does not say.

**Fix.** Have the CLI read the variable and pass `account_name=` to `from_url` (the stub accepts it as a keyword). Define
one resolver in Part 1 for `now()` and `AzureBlobRest`: `config["endpoint"]`, else `https://<account>.blob.core.windows.net`.
Name where container and prefix come from. State `S3Store`'s default, and that `MemoryStore` with no `clock=` refuses. Add
a unit row asserting the exact URL with a prefix, on a store built the way the CLI builds it with only the variable set.

### M3. The CLI rows have no seam and leave the production wiring untested

**Reviewer:** `IR-2`. **Area:** 3. **Checked by reading.**
**Sites:** design:723, 728, 733, 936, 1044; `src/statcatlib/cli/EGM.md:26-27`; `pyproject.toml:83`.

**Failure.** Row 1044 lists `success`, `already registered` and `findings exit 1` as unit `CliRunner` rows, and nothing
says what replaces the store. The command builds a real `DefaultAzureCredential`, and its output line needs `oid`, which
fetches a token. That is network I/O in a tier defined as having none. The natural patch point replaces `upload_edition` whole.
Then a CLI that passes a bare `AzureCredentialProvider` without `TokenIdentity`, or drops the one-hour timeout, passes every
row and raises D7's `ValueError` in production. No row covers the output line that D7 relies on to expose an unintended identity.

**Fix.** Name the seam: patch `upload_edition` and the credential class, and capture the `BlobLocation` passed. Assert
`isinstance(location.store.credential_provider, TokenIdentity)`, `client_options`, `prefix` and `location.uri`, and that the `oid`
is printed. The first assertion depends on the provider property returning the same object (unverified), which this row would show.

### M4. The lost-race row can pass without testing the acceptance item

**Reviewer:** `IR-3`. **Area:** 1 and 5. **Checked by reading.**
**Sites:** design:956, 1000, 1134; `store/inventory.py:595-613`; `tests/test_inventory_history.py:208-239,254`;
`tests/test_catalog_admin.py:71`; sdmxlib `inventory/conformance.py:570-586`.

**Failure.** The row can run: `InventoryWriter(storage, artefacts=lambda _p, _e, _s: {})` is the pattern at
`test_inventory_history.py:254`. But design:1000 and design:956 allow three tests, and only one proves the item "a lost race
recomputes `changes` and `previous_root_sha256`" through the writer's loop.

- Two `try_commit` calls: the loser raises and the test retries by hand. This repeats a check `run_history` already runs, and never
  enters `commit`'s loop on blob.
- `commit` with no incoming entry, such as `Change(leaves={}, embargo=True)`: `changes` is empty for winner and loser, so that half
  proves nothing.
- `commit` with `artefacts=` and a race injected as `_race_once` does: the only one that exercises the loop.

**Fix.** Rewrite row 1000: "`commit` on a writer with `artefacts=lambda *_: {}` for a new product, with a second writer winning inside
the first `try_commit`; the loser's `previous_root_sha256` hashes the winner's stored bytes and its `changes` name only its product."
Note that `_race_once` lives in two test modules and needs a copy or a move. Delete the "no incoming entry" option from design:956.

## Minor

| # | Reviewers | Sites | Finding | Proposed fix |
|---|---|---|---|---|
| m1 | `DES-2` | design:367-368, 810; `inventory.py:596-598`; `cross_product.py:214,219-235,395-399` | The `NotImplementedError` works but its site is unnamed. Raising on non-empty `planned.leaves` refuses no-incoming commits, and a raising `Artefacts` stub alone arrives after `_planned`'s `full.leaves()`, up to 7,000 GETs | One sentence: the default blob `artefacts` raises, and `_refuse` checks `cross_product.incoming` first |
| m2 | `DES-3` | design:322, 357-359; `inventory.py:402-408` | Two writers with identical root bytes in one `Date` second both succeed. `write_catalog_json` run twice returns success against its `FileExistsError` contract. State is correct and conformance is unaffected | State that twin roots count as one commit; have `write_catalog_json` refuse equal bytes on blob |
| m3 | `DES-4`, `IR-9` | design:356-361, 753, 803-806, 1042; `obstore/exceptions/__init__.pyi:12-54` | "Transport error" is undefined once `_ObjectOps` wraps faults, so a definite 403 reports as an unknown outcome. `AzureBlobRest.head` on 401, 403 or 5xx has no row, and could report a missing role as a missing file | Name the `obstore` classes that count as unknown, re-enter read-and-compare on retry, and add a `head` row |
| m4 | `DES-5`, `IR-13` | design:171, 173, 591-593, 1074; `_check.py:305-350,755-759`; spec:272, 283 | #371's rule as stated leaves open: how a stamp records deferral, `references=False` with bindings, with `structures=`, at `publish` and `open`, and an older reader. Its "later full check" has no Phase 4 caller, and on external references always finds. Spec §4.6 and §4.7 contradict it, and no handoff row owns that text. #371 was not read and may answer some | Put the five answers in #371, and add §4.6 and §4.7 to a handoff row |
| m5 | `DES-6` | design:570-573; `edition/_copy.py:92-97`; `_check.py:371-379,399-405` | `pull_edition` states no fsync, and the stamp is durable. After a power loss a torn file can pass `open`, which checks sizes only. `copy_edition` fsyncs for this reason | Fsync each file and directory before the rename |
| m6 | `DES-7` | design:574, 578; `registry.py:1540-1542` | D6 does not say that `remote=` is `<uri>products/<p>/<e>/` | One line |
| m7 | `DES-8`, `CB-3`, `IR-4` | design:764, 779-791, 954; `_rules.py:178-196` | `record_uploaded` works, and bypasses `upload_edition`'s refusals through `_upload`. Its table omits `leaf`, `product_id` and `now`, `writer.now()` does not exist, and no fixture gives its writer an actor and clock. M7's remainder | Add three rows and a `writer` fixture in `tests/stores/conftest.py` |
| m8 | `CB-4`, `IR-18` | design:1086-1094, 1118; `_rules.py:256`; `_reader.py:325` | `inventory_corpus` omits `kind`, `lifecycle`, `schema_version`, the id form and `now`. A clock `now` fails "determinism by seed", and a wrong `schema_version` makes the read count nothing | State the values, with fixed `now` and `SCHEMA_VERSION` |
| m9 | `CB-5`, `IR-17` | design:1095-1096, 1148; sdmxlib `bench/tier3/bench_tier3.py:16,41` | `uv run python bench/inventory_read.py` cannot import `tests.stores._stores` | `uv run python -m bench.inventory_read` |
| m10 | `CB-6`, `IR-5`, `DES` note | design:443, 1010; `inventory.py:532-540` | `try_commit` writes leaves at `:534` and reads the actor at `:540`, so a literal port writes before it refuses | Resolve the actor before the loop, and assert no write in the row |
| m11 | `CB-7`, `IR-10` | design:436, 701, 723; `obstore/auth/azure.py:86-96` | `token()` freshness is unspecified. A last-seen token can expire across a long Put and fail step 5 with a 401 | `token()` calls the provider on each use |
| m12 | `CB-8` | design:663-678, 837-840; `_check.py:61`; `_copy.py:106-143`; `_manifest.py:67` | `lookup(EditionFile)` has no adapter over `head(path)`, and steps 2 and 3 name no path mapping. `data_path` is `../data/`, so a naive join gives `products/p/e/../data/x`. M18 and m13 remainder | Add the adapter to Symbols, and state the normalisation or `edition_files` |
| m13 | `IR-6` | design:669, 679, 1033 | Step 4 is an unconditional create, so a recording `_ObjectOps` sees a manifest `put` on a rerun, and row 1033's "sends no file again" fails | `head` first, or define "sent" as an accepted create; state how step 4 reads stored bytes |
| m14 | `IR-7` | design:423-424, 575-576, 1043; `bundle.py:228-237` | Part 2's row covers "upload and view", but a blob `view` arrives in Part 3, which has no such row. D2's chooser has no name, and it is in no file list | Name it, list it in Part 2, add a Part 3 row |
| m15 | `IR-8` | design:729, 815, 1044; `cli/__init__.py:26` | `ManifestError` and `ImportError` are in no exit class, and "store or credential fault" names no types, so they reach Typer as tracebacks. Typer's printing of locals against "a token never reaches a log" is unverified | List the caught classes, give `ImportError` a code, check Typer's default |
| m16 | `IR-11` | design:167, 172, 174, 176, 966, 974 | Handoff row 2 makes Part 4 gate Part 2, and the Land order does not. If Part 2 lands first, Part 4 cannot land on 0.85 as W13 says. Rows 4 and 6 also sit oddly in "in order" | Split row 2, and put Part 4 on the current cap |
| m17 | `IR-12` | design:12, 173, 174, 1018; `pyproject.toml:30-32` | "S16 alone" is outside statcatlib's control, since a release from main carries whatever landed after 0.85.0. Nothing says who absorbs a break or what the pin text is | One sentence: Part 2's PR sets the exact pin and fixes any break |
| m18 | `IR-14` | design:13, 24, 184-186; plan:326, 334 | "Read against" names `011312c3`, and the branch is at `02d3af2a`. Lines 185-186 say the plan still needs edits it now has | Update the commit, and delete the plan half |
| m19 | `IR-15` | design:65, 126, 180, 391, 845-848, 954 | `CatalogAdmin` on blob has "a later phase" and no owner, and Moved out omits it, though line 954 names `correct` and `reinstate` as its callers. S9 and S11 are defined only in revision 2 | Add it to Moved out with an owner, and point to S9 and S11 at `e7eba47` |
| m20 | `IR-16` | `store/working_copy.py:1002`; design:873-874, 990 | A runtime error says a lifecycle change "is not supported before Phase 4", and Phase 4 does not build it. Same kind as `admin.py:364-366` | Add it to Part 1 |

## Results by area

| Area | Result |
|---|---|
| 1. D1 | Sound. Conformance: `WriterBackend` calls only `try_commit`, and `IR` and `CB` each walked the `CHECKS` and `HISTORY_CHECKS` against D8's copy and found nothing. All Part 1 and Part 3 rows can run under the `NotImplementedError`; only the lost-race row and the error's own row use `commit` with an incoming entry. M4, m1, m2, m3, m10 |
| 2. D6 | Sound as traced: crash mid-pull, crash before the stamp, cached mismatch, rollback to a cached edition, `_Store._over` with no writer, `refresh`, and `remote=` with `engine=`. `check_catalog` and `check_catalog_no_index` fit `Check`. M1, m4, m5, m6 |
| 3. D7 | Every step's API exists with the signature named, apart from M2 and m12. Step 5 covers what it claims. The file list is complete. `typer` and `rich` in `blob` is sound. M2, M3, m11, m13, m14, m15 |
| 4. Handoff and Land order | Every dependency has an owner, and the order works. Parts 1, 2 and 4 hold on their named sdmxlib versions. Part 3 needs one thing beyond #371, or the workaround (M1). m4, m16, m17, m18 |
| 5. Moved-out work | Nothing in Phase 4 still needs it, apart from M4's reference at design:956 and the two wording items m19 and m20 |
| 6. Round 1 closure | See below |
| Embargo and data guarantees | No break found. `pending/` is written last, after all three verify steps. Upload reads nothing under `catalog.json`, `inventory/` or `CURRENT`. `view` only GETs editions from `LiveInventory`, and leaves stay in memory |

## Round 1 items still open

| Item | State |
|---|---|
| B1 | Closed for upload, `view` and `record_uploaded`, given #371. A default-argument caller is still refused on a valid structure edition (m4) |
| B3 | Closed. Text residue in m4 and m16 |
| B4 | Open in part: M2 and M3 |
| M7 | Open in part: m7 |
| M18, m13 | Open in part: m12 |
| M20 | Open in part: m3 |
| m22 | Open in part: m8, m9 |

**Closed, by a reviewer's reading:** M3, M5 as S12, M6, M8, M9, M11, M12, M14 to M17, M19, M21, M23, M24, M27 to M30.

**Closed, by the compiler's reading of the design only, since no reviewer was asked:** M22 (D7 and D8 exist), M25 (design:11-24 pins the commits, with m18's staleness), M31 (design:53-66), M32 (design:79-96), M33 (the Terms table's qualified terms; not traced through every use), M37 (the "Built" column at design:383-391).

**Moved to GH-130, per design:850-852, and not reassessed:** M1, M2, M4, M10, M13, M16, M26, M34 to M36. `IR` found that each of these concerns `correct` or `reinstate`.

## Could not verify

- The text of statcatlib#98, #130, #52 and sdmxlib#371, and so: #371's exact stamp rule and release vehicle, whether #52 carries `ManifestArtefacts(fetch)`, handoff row 8 and S6, and whether #130 takes `CatalogAdmin.open(BlobLocation)`.
- That sdmxlib's `src/` equals `11f18e3f`, and whether `origin/main` holds source commits after 0.85.0 (m17).
- What `AzureStore.config` holds for values from the environment or `from_url` (M2).
- That `store.credential_provider` returns the same Python object. D3 step 1 and D7's `ValueError` depend on it.
- That `import azure.identity.aio`, at `obstore/auth/azure.py:12`, succeeds on a `[blob]` install without `aiohttp`. If it does not, `statcat upload` fails on that install, and the dev environment could hide it. This silent dependency is worth adding to the checklist.
- That `obstore.sign` honours a store's `prefix`, which the Azurite `AzureBlobRest` subclass needs.
- `obstore`'s credential caching and its handling of `..` path segments.
- That DuckDB reads `az://` over Azurite with this design's secret.
- Typer's default for printing locals, and whether `filterwarnings = ["error"]` trips on `obstore`, `azure-identity` or the tier's subprocesses.
- Whether a statcatlib seal passes `tiers=` overrides that place an edition file in the `remote` tier.

# Codebase details review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** the EGM feature-spec standard.
**Reviewer:** `egm-codebase-details`.

**Counts as reported:** 2 Blockers, 9 Majors, 16 Minors.

## Scope and limits

- The design was read whole, and it meets the standard's form: alternatives, risks and goals are all present.
- Nothing was executed. Every "run" means the code was traced by reading it.
- Paths are the checkouts named in the brief. Their HEAD commits were not separately confirmed.
- Spec: §4.1, §4.6, §4.7, §6.1 to §6.8, §7.1, §10, §11 were read, and §6.9 only spot-checked.
- Hub design: the lease section and the decisions table.
- Line numbers are at the on-disk checkouts.
- Blocker here means the design as written fails or misleads implementers about current code.

## Findings

### CB-1. Blocker. `check_on_open`, upload and `_verify_edition` run with no `structures=`

**Target:** design and sdmxlib code. D6 step 4, Upload step 5 and D7's `_verify_edition` run `receive` or `publish` with no `structures=`.

**Evidence:** sdmxlib `src/sdmxlib/edition/_check.py:293` passes `None if manifest.bindings else structures`. `_check.py:755-759`: with `structures=None`, every unresolved reference is a finding. `check_on_open` has no `structures` parameter (`_check.py:305-313`). statcatlib `src/statcatlib/store/working_copy.py:1128-1131` says a reference into another structure product stays open. Spec §6.5 item 9 gives the same example.

**Failure scenarios:** a live concept-scheme structure product references a codelist in another structure product. The first blob `view()` gets a `references` finding and raises `IncompatibleCatalogError`, permanently. `statcat upload` of that edition raises `UploadCheckError` and registers nothing, and upload may not read the Inventory (§10.1), so it cannot find the other structure editions. `correct` or `reinstate` of it raises `EditionCheckError`. Data partials also go unchecked in `_verify_edition`.

**Fix:** add a contract item: sdmxlib gets `check_on_open(structures=)`, or a way to defer the references check at `receive`. Upload skips references for an edition with no bindings, with the check deferred to the fold. Local `_verify_edition` passes structures as `diagnostics._structures_for` does (`diagnostics.py:260-283`). State how the blob `_verify_edition` gets structure editions.

### CB-2. Blocker as reported. `reinstate`'s call unpacks a tuple with `**`

**Target:** design line 762: `reinstate(..., **recorded_routing(routing_fields(manifest), leaf.kind))`. **Evidence:** `recorded_routing` returns a 3-tuple `(dataflows, pull_bytes, artefacts)` (`working_copy.py:322-337`, used at `:376` and `:1786`). `**` on a tuple raises `TypeError`. **Failure scenario:** `reinstate` written as designed fails on every call. **Fix:** unpack the tuple and pass `artefacts=, dataflows=, pull_bytes=`.

### CB-3. Major. `statcatlib[blob]` is not enough for a remote `view()`

**Target:** design (D2, D6). The extra is `obstore`, `typer` and `rich`, without `lancedb`. **Evidence:** D6 step 4 passes `extensions=`, which means `check_catalog`. `src/statcatlib/bundle.py:229-232` guards only the module import, and `lance.py` imports `lancedb` lazily, so that import succeeds. `bundle.py:237` calls `LanceIndex(search_dir)`, and `lance.py:1080` runs `import lancedb` in `__init__`. `check_edition` catches only `duckdb.Error` (`_check.py:226-233`). **Failure scenario:** on a `[blob]`-only install, any edition with `search/` raises an unhandled `ModuleNotFoundError` from `view()`. This is an existing statcatlib bug the design newly exposes. **Fix:** have `_check_search` catch the error at the constructor as well, and decide whether `blob` needs `lancedb` or the check is skipped. Add a test with `lancedb` absent.

### CB-4. Major. The prefetch estimate covers manifests only

**Target:** design (D1, D5, NFR). **Evidence:** `refusals` calls `_planned`, which calls `full.leaves()` (`cross_product.py:214`). `leaves()` is 7,000 sequential GETs through `_Chain._leaf` (sdmxlib `inventory/_reader.py:256-269`, `302-304`). The reader has no batch path. `read_many` appears only as a name in Part 1 (design 885), and leaf prefetch only as a Part 5 bench case (1010). **Failure scenario:** a cold `release` at 7,000 products takes about 71 s at 10 ms a GET, 70 s of it reading leaves. **Fix:** specify production leaf prefetch, such as a memoizing `Fetch` filled by `read_many` from `root.products`, or a sdmxlib batch API. Otherwise correct the estimate.

### CB-5. Major. `record_uploaded` and the fold have no source for `lifecycle`

**Target:** design and spec. **Evidence:** `record` requires `lifecycle` (`_rules.py:183`). `Manifest` has no lifecycle field (sdmxlib `src/sdmxlib/storage/_manifest.py:402-422`). `seal(lifecycle=)` only drives lake settling (`edition/_write.py:347`, `553`). `record_uploaded` has no such argument, and `pending/` holds only `edition_id` and `release_at` (§11.2 step 1). **Failure scenario:** the first upload of a `final` product is recorded with a guessed lifecycle. A later `working_copy(lifecycle="final")` raises `rule="lifecycle"` (`working_copy.py:999-1004`). The Phase 5 fold has the same gap. **Fix:** add a `lifecycle=` argument to `record_uploaded`, and add a spec question on where the fold gets lifecycle (the manifest's `producer_fields`, the `statcat` section, or the `pending/` JSON).

### CB-6. Major. `_verify_edition` on blob pulls into a cache that may be absent, and caches embargoed files

**Target:** design (D6, D7). **Evidence:** `cache_dir` is required only by `Catalog.open` (design 538), and `CatalogAdmin.open(BlobLocation)` has no such check. D7 step 6 verifies restored entries, and a release after `from_seq` returns its entry to `embargoed` (747). **Failure scenarios:** `CatalogAdmin.reinstate` with `cache_dir=None` has no destination. `correct` writes an embargoed edition's `metadata.duckdb` into the operator's cache, against "the cache holds nothing embargoed" (613) and outside the §10.1 boundary. **Fix:** pull into a temporary directory, delete it after the check, and state that. `reinstate` then needs no `cache_dir`.

### CB-7. Major. `statcat upload` has no credential source

**Target:** design (CUJ 1, Upload, D2). **Evidence:** `AzureBlobRest` needs a `TokenIdentity` or an `account_key`, otherwise upload refuses (design 390-395). `azure-identity` "stays the caller's dependency" (376). `obstore/auth/azure.py:11-12` imports `azure.identity` at module load, and `obstore`'s METADATA has no such dependency. **Failure scenario:** the production CLI path either raises `ImportError` or refuses. **Fix:** state the CLI's credential source, for example an `azure` extra with `DefaultAzureCredential` wrapped in `TokenIdentity`, and the environment variables it reads. `AzureStore` constructors read environment variables themselves (`_store/_azure.pyi:354`).

### CB-8. Major. "Every local caller and test is unchanged" is false

**Target:** design (D1 line 303; Part 1 file list). **Evidence, for code the design moves to `store/storage.py`:** `tests/test_seal_crash.py:86,91` patch `InventoryWriter._write_leaf` and `_advance_current`. `tests/test_inventory_writer.py:118` patches `inventory_module.create_exclusive`. `tests/test_inventory_writer.py:165` calls `writer._advance_current`. `tests/test_inventory_writer.py:303-305` patch `inventory_module.getpass` and `socket` and call `_actor()`. `bench/corpus.py:170` reads `ManifestArtefacts._root`, and `ManifestArtefacts(fetch)` removes that attribute. **Failure scenario:** Part 1's PR fails `just ci` on the crash-point tests, and `bench/corpus.py` raises `AttributeError` when rerun. **Fix:** list these files as Modify, or keep thin delegating methods on the writer. Drop the claim.

### CB-9. Major. No API gives an operator `if_seq` or a candidate `from_seq`

**Target:** design (CUJ 3). **Evidence:** "`CatalogAdmin.editions` shows the head at 42 and version 39 good", and `EditionEntry` has no `seq` (`admin.py:67-89`). `CatalogAdmin` exposes `embargo`, `products` and `editions` for reading, and no version listing. **Failure scenario:** reaching for `_writer.head()` is the only route, and `doctor` is path-only. **Fix:** add `CatalogAdmin.head_seq` and a `versions()` read, using `event` and `changes` from retained roots, to D7 and Part 4.

### CB-10. Major. D7 omits entries withdrawn after `from_seq`

**Target:** design (D7), bears on O4. **Evidence:** step 3 restores `from_seq`'s leaf, and only entries the head tombstones are removed. An entry embargoed at `from_seq` and cancelled later (`cancel`, `_rules.py:350-362`) returns as `embargoed` with its old `release_at`. `release_due` then releases it when due (`_rules.py:313-347`). A rolled-back edition returns as `live`, against §6.4: "`withdrawn` ... never routed again automatically". **Failure scenario:** an operator cancels E at 41 for a confidentiality problem, then corrects from 39 for another product's fault. E is embargoed again and goes out at its old time. The rules and step 6 pass, because the files are fine. **Fix:** refuse, keep `withdrawn` from the head, or at least report it and test it. The owner decides, alongside O4.

### CB-11. Major. S8 is stale, and spec line 519 contradicts option 1

**Target:** design (S8) and spec. **Evidence:** at the sdmxlib checkout, `docs/design/sharded-bundle-catalog.md:427` already says "`retire` or `correct`", and `:519` already has an `any state | tombstone | correct(...)` row. The new row says "any state", while D7 refuses when a dropped entry is live in the restored leaf and O4's recommended option keeps embargoed entries. **Fix:** restate S8 against this commit, and have the spec row follow O4's outcome: "any state but embargoed" under option 1.

### CB-12. Minor. Upload step 5's check has no `extensions=`

**Target:** design. The call `check_edition(edition_dir, at="receive", lookup=lookup)` has no `extensions=`, so no `statcat` check runs (`_check.py:302`). `verify_remote` over all tiers repeats the data-file findings of `check_edition`'s `remote` step (`_check.py:202-205`). **Scenario:** a bad `statcat` section uploads and registers, and data-file faults are reported twice. **Fix:** pass `extensions={STATCAT_SECTION: check_catalog}`, and limit `verify_remote` to `root: edition` files.

### CB-13. Minor. Code reuse the design misses

**Target:** design.

- `working_copy.publish_check` (`:253-300`) already does the local check and raises `EditionCheckError`, with the partials guard. `_verify_edition` restates a weaker form.
- `_verify_edition` shares a name with `diagnostics._verify_edition` (`:299`).
- `publish` with default `fingerprints` recomputes fingerprints (`_check.py:148-150`: 774 s against 46 s on 2.0 GB). "Both cost the edition's size" understates this.
- `EditionCheckError`'s message says "the publish check of the sealed edition ... stays on disk unrecorded ... start a new working copy" (`working_copy.py:188-192`), which is false for `correct`, `reinstate` and a `receive` check.
- `sdmxlib.edition.edition_files(manifest, tiers=)` (`edition/_copy.py:106-143`) lists exactly what upload steps 2 and 3 and `pull_edition` need, data first, manifest left out.
- `edition_kind` and `manifest_sha256` (`working_copy.py:199-207`) serve `record_uploaded`.
- `root_certificate` (`_store/_client.pyi:113`) with `trustme`'s CA replaces `allow_invalid_certificates`.

### CB-14. Minor. `advance_current` omits the epoch comparison

**Target:** design (D1). The local rule compares epochs: `held.restore_epoch == root.restore_epoch and held.seq >= root.seq` (`store/inventory.py:388`), and treats a malformed `CURRENT` as absent (`:386`). **Scenario:** after a recorded restore, a higher-`seq` `CURRENT` from the old epoch is never replaced, so every reader falls back on every refresh. **Fix:** keep both conditions, and overwrite a malformed `CURRENT` using its ETag.

### CB-15. Minor. The long-lived reader keeps a stale head after `restore_to`

**Target:** design (D1). `_Chain.refresh` keeps a held root when the found `seq` is lower in the same epoch (`_reader.py:178-179`). `_stored_root` relies on a fresh read (`inventory.py:439-445`). `skipped` and `_leaves` only grow (`_reader.py:124-125`). **Scenario:** after an epoch-keeping `restore_to`, allowed on 1.0 catalogs (§6.1), a long-lived writer keeps a stale head and raises `InventoryConflictError` on each race until `InventoryContentionError`, while a new process works. A shared instance also mutates a `full` that callers still hold. **Fix:** on `InventoryConflictError`, rebuild the reader and reseed its leaf cache.

### CB-16. Minor. The `record` row omits two conditions

**Target:** design. `record` refuses a tombstoned edition only if the caller passes `retired=` (`_rules.py:191`, `247`, default `()`). `record` on an empty leaf with embargo off succeeds only if `previous` is `None` or names a live edition at another schema (`_rules.py:166-174`). **Scenario:** `record_uploaded` omitting `retired=` records a dropped edition. Line 701's seal works only because `_start` gives `previous=None` (`working_copy.py:1012-1015`). **Fix:** state both conditions in the row.

### CB-17. Minor. The second `correct` has a precondition, and S9's spelling collides

**Target:** design (line 700, S9). Step 4 raises `rule="tombstone"` when the head tombstones a live entry of the restored leaf, on any product. **Scenario:** `correct` dropped E of p and G of q, both live at 42. After reinstating only E, `correct(from_seq=42)` refuses on q. Part 4's single-product test will not see this. `CatalogAdmin.rollback(if_live=None)` already means "the live edition when the call starts" (`admin.py:369-374`), so S9's "expects none live" needs a different spelling at that layer. **Fix:** state the precondition that every such entry must be reinstated first, and rename S9's argument.

### CB-18. Minor. Rule literals and the None head

**Target:** design (D7 steps 1 and 2; Part 4 file list). `InventoryRuleError.rule` is typed `Rule | "lifecycle" | CrossRule` (`inventory.py:140`, `145`), so `"if-seq"` and `"from-seq"` fail pyrefly. `commit` can pass `full=None` (`inventory.py:596-597`), and `full.seq` then raises `AttributeError`. **Fix:** add `store/inventory.py` to Part 4, and define the no-version refusal.

### CB-19. Minor. Commit counts

**Target:** design (D1, NFR). `commit` calls `full()`, which calls `has_version()` first (`inventory.py:358`). Through `storage.read` that is a further `CURRENT` GET, so a commit is 10 reads, not 9. `_commit_product` and `correct` take a `writer.now()` and `try_commit` takes another, because `commit` does not pass `now` (`inventory.py:600-609`). That is 2 `Date` HEADs, which D4 admits and D1's count omits. `retired_at` and `created_at` then come from different requests. **Fix:** correct the count, or add `now` to `Change` or `commit`.

### CB-20. Minor. `InventoryWriter.root` and `verify-catalog` on blob

**Target:** design (lines 352-353). `CatalogAdmin.__repr__` reads `root` too (`admin.py:249`). Refusal messages name `statcat verify-catalog` (`catalog.py:910`, `cross_product.py:173`), a command that takes only a path (`diagnostics.py:353`). **Scenario:** `repr()` raises in a debugger or log line, and blob operators are told to run a command that cannot run. **Fix:** use `storage.location`, and state how `verify-catalog` applies to blob in Phase 4.

### CB-21. Minor. Endpoint and TLS plumbing

**Target:** design (D2, D4). `now()` "takes the endpoint from `store.config`". CUJ 2's store sets only `account_name`, and `endpoint` is an optional override (`_store/_azure.pyi:155`), so the endpoint must be derived. Whether `config` fills it was not verified. `AzureBlobRest` and `now()` use `httpx`, while `allow_invalid_certificates` reaches only `obstore`, and `BlobLocation` has no TLS setting. sdmxlib's `start()` waits on an `http://` base (`tests/stores/_stores.py:103-123`). **Fix:** specify endpoint derivation and an injected `httpx` client or CA.

### CB-22. Minor. Credential provider shapes

**Target:** design (D3). A credential provider may return a coroutine, an access key or a SAS (`_store/_azure.pyi:316`, `348`). "Decodes the JWT payload" holds only for a sync `AzureBearerToken`. **Scenario:** an async provider with `TokenIdentity` fails on the coroutine. **Fix:** state the supported shapes and behaviour for the others.

### CB-23. Minor. D5 and Part 4 details

**Target:** design. D5 says the rules read blob manifests "only" in `release`, `rollback` and `correct`, yet `record_uploaded` commits through them (818-819). Part 4 says each test "also runs once over Azurite", and `doctor`, `prune` and "a new seal" cannot run on blob. `prefetch` is called in `refusals`, whose reader is typed `Artefacts` and may be `StoreArtefacts` (`inventory.py:322-338`), a plain callable with no `prefetch`. `to_read`'s stated conditions omit an incoming embargoed data entry, which `_Rules.data` reads (`cross_product.py:313`); D5 notes such a gap costs only one sequential GET.

### CB-24. Minor. §6.4's `(sealed)` row

**Target:** spec. §6.4's row `(sealed) | embargoed | ... the same fast-forward check` (`sharded-bundle-catalog.md:510`) contradicts `record`, which checks only when embargo is off (`_rules.py:229`, `264`), and the spec's own line 522. **Fix:** fix it with S1.

### CB-25. Minor. Extensions at `open`

**Target:** spec and design. §4.6 says extensions at `open` are "section only" (`:277`), while `check_catalog` runs everything at every profile (`bundle.py:294-295`, `303`). **Scenario:** D6's "later ones `open`" means each `view()` on a pulled edition also runs dataset counts and opens the LanceDB index, and the caching table omits this. **Fix:** align the spec or the code, and record the cost.

### CB-26. Minor. `_remote.py`'s docstring

**Target:** sdmxlib code. `edition/_remote.py:7-8` says Azure keeps `Content-MD5` "when the uploader sets it". F7 says the service computes it for a single-shot Put. **Fix:** update the docstring once real Azure confirms.

### CB-27. Minor. "Naming the withdrawn base"

**Target:** design (line 753). A dropped `previous` is tombstoned. `_fast_forward` says "which is not recorded" when no edition is live (`_rules.py:168-173`), or names the live edition (`admin.py:421-425`). The test row's `NonFastForwardError` is correct on both paths (`inventory.py:159`). **Fix:** reword.

## O4 facts (no recommendation)

- `Tombstone` holds only `product_id`, `edition_id` and `retired_at` (`_format.py:178-189`).
- `reinstate` writes `superseded`, with `release_at` unset, and requires a matching tombstone or raises `rule="tombstone"`. It takes no `structure_source`, so any is lost (`_rules.py:506-524`).
- `rollback` has no `release_at` check. `_moved` always sets `release_at=None`. It requires a live edition at the schema, or raises `if-live` (`_rules.py:127-140`, `413-436`).
- `release` checks due only if not explicit, then fast-forward. `CatalogAdmin.release` always passes `explicit=True` (`admin.py:415`), so option 1's "goes through when due" applies only to Phase 5's `release_due`. A manual release ignores `release_at`.
- `cancel` needs `embargoed` and moves it to `withdrawn`, recorded with `event.kind` `withdraw` (`_rules.py:350-362`, `admin.py:445-451`).
- `record` with embargo on does no fast-forward check. With embargo off it refuses a `previous` that is not live, including a tombstoned one (`_rules.py:155-175`, `264-267`). So option 1's "its release's fast-forward check refuses" holds.
- A kept entry with `previous=None` and no live edition at its schema still passes `_fast_forward`.
- An embargoed entry recorded before `from_seq` and released later returns to `embargoed` with `release_at` kept. See CB-10 for cancelled entries.

## `obstore` facts

Source: the wheel at `/Users/gabrielgellner/.cache/uv/archive-v0/CrCci5PwDHbOclfm/` (`obstore` 0.12.1 stubs, Python files and METADATA). It is in no `.venv`. No checkout's `.venv` holds `obstore`, and no Rust source exists on disk, so `src` citations could not be checked.

| F | Status | Source, and what remains unverified |
|---|---|---|
| Header | Verified | METADATA: 0.12.1, `>=3.11`; only `typing-extensions` for Python below 3.13. Wheel size and upload date unverified |
| F1 | Partly verified | `_put.pyi:45-49`, `exceptions:35`. Headers and statuses not verified |
| F2 | Partly verified | `_put.pyi:14-52`, `74-96`, `_get.pyi:207`. `LocalStore`'s `NotImplementedError` and the run not verified |
| F3 | Partly verified | `_list.pyi:10-40`, `_put.pyi:74-96`. Absence of `Date` from responses not verified beyond these stubs |
| F4 | Not independently verified | |
| F5 | Partly verified | `_aws.pyi:60`, and `:113` allows `str`. The `crc32c` config error is run-only |
| F6 | Partly verified | `_attributes.pyi:46`, `_client.pyi:74`. Azurite's responses not verified |
| F7 | Not independently verified | |
| F8 | Partly verified | `_sign.pyi:7-18`, `23-29` allows HEAD. The `sp=` behaviour not verified |
| F9 | Not independently verified | |
| F10 | Partly verified | `_put.pyi:107`, `116-120`, `155`. Byte boundary not verified |
| F11 | Verified at stub level | `_put.pyi:152`, `155`. The `Path` run not verified |
| F12 | Not independently verified | |
| F13 | Partly verified | Defaults in `_retry.pyi:79`, `96`. Retry semantics are src-only |
| F14 | Verified at stub level | `_delete.pyi:5`, `_put.pyi:98-109`. No lease stub among the listed files |
| F15 | Verified | `_azure.pyi:331-349`, `auth/azure.py:11-12` |
| F16 | Not independently verified | |
| F17 | Not independently verified | Consistent with the 14,484-byte manifest in `bench/corpus-results.md:35-36` |
| F18 | Not independently verified | |
| F19 | Not independently verified | `allow_invalid_certificates` exists: `_client.pyi:45` |
| F20 to F22 | Not independently verified | |

None is contradicted. Stub facts that support the design: `AzureStore.prefix`, `config` and `credential_provider` getters (`_azure.pyi:441-450`) and `account_key` in the config (`:28`).

## Claims verified correct

**"What `sdmxlib.inventory` accepts":** `next_root`, `validate_root` and `decode_root`, tombstones and empty leaves (`_format.py:813-832`, `_rules.py:747-771`); `retire` (`_rules.py:450-452`); `change_list`, `_marks`, `_check_item`, and `leaf_sha256.to` for an empty leaf (`_rules.py:586-667`, `_format.py:860-876`); `decode_leaf` and `validate_leaf` with `editions: ()` (`_format.py:879-907`); `LiveInventory.head`, `products` and `FullInventory.products` (`_reader.py:290-292`, `345-360`); `_named` and `settle_journal` clearing an entry, for the product's own entries (`working_copy.py:311-319`, `670-672`); `reinstate` on an empty or restored leaf; `rollback` rows, including `if_live=None`.

**Conformance:** `check_a_correction...` at `conformance.py:546-567`, including `("live", None)`; `commit_interrupted`, `commit_at`, `run_history`, `stage_2`; statcatlib's backend maps to `try_commit` (`tests/test_inventory_conformance.py:109-147`); the `restore_*` methods exist for a blob backend to implement.

**sdmxlib APIs:** `check_edition` signature, profiles and lookup; S4 at `_check.py:382-405` and S5 at `:202-205`; `check_on_open` and `LocalRegistry.edition` signatures; `decode_manifest` not exported, `read_manifest` takes a directory, `routing_fields`; `verify_remote`, and `RemoteFile`'s base64 digests.

**statcatlib symbols:** `local_fetch`, `create_exclusive`, `_stage`, `_fsync_dir`, `try_commit`, `Change` (all five fields; no new field needed), `commit`; `_commit_product`, `admin.py:325`, `:650`; `next_created_at` (`_rules.py:534-557`), `check_writable`, `_planned`, `ManifestArtefacts`, `_Rules`; `_Store._over`, `_refuse_legacy_layout`, `lance.py`'s lazy import; `doctor`, and the `cli` imports needing `[search]`; `store/inventory.py:237-243`, `300-311` and `catalog.py:594-620`.

**Tooling:** `ci` recipe, `coverage` selection, `fail_under = 85`, pre-push comment; `search` extra's contents and `dev` group; `test_public_api.py` and `test_cross_product_rules.py` exist.

**Bench numbers:** 6,300 manifests and 91 MB, 63 s, 0.3 of 16.7 MB, 1 to 4 ms, five calls a seal. "510 bytes a leaf" is in spec §6.3 (`:432`), and the design's Part 5 does not attribute it to the bench. 8 MB root: 6.5 + 1.6 at `:432`. 14 roots and the `ceil` arithmetic.

**Spec references that hold:** §4.1, §4.6, §4.7, §6.1, §6.2 (actor, S2 against §10.1), §6.3 (`to: null`, S11), §6.4 (30 s, S1); §6.5 items 4, 6, 7, 8, §6.6 items 2 to 4 and the leaf rule; §7.1, whose "readiness budget" is named at §4.0 `:103` and §7.3 `:831`, with no number for an Inventory read; §10.1, §11.2, §11.3, §11.4, §11.5; §6.9 only as far as spot-checked.

**Hub design:** H10, H11, H21, H23, H24; lease section (`:453-477`), including the carried-forward leaf and internal commit under lock. H12 fits D1's stage-2 table, and the design does not cite it.

**Test rig:** emulators pinned by digest, free port, skip in `conftest.py:32-36`, 20 s at `sdmxlib/justfile:232`, SharedKey signer size; W5's docstring warning (`local/object_store.py:91-93`); `doctor` tombstone handling (`diagnostics.py:468-476`); `prune` windows (`admin.py:751`).

**Phase 2a findings:** DES-M6's four defects (`critic_report.md:118-126`), N13 (`comprehension_report.md:282`), ZM2, and P6 (`phase-2a-catalog-editions.md:273`).

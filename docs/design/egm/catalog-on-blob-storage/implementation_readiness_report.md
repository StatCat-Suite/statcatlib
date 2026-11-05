# Implementation readiness review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** the EGM implementation-readiness standard.
**Reviewer:** `egm-implementation-readiness`.

**Verdict as reported:** not ready under the Zero-Decision Rule. 2 Blockers, 13 Majors, 9
Minors. The architecture and the D7 rule logic can be built. The gaps are sdmxlib ownership
and order, the production path of `statcat upload`, test seams, undefined symbols and
undefined errors. Part 1 is closest to ready. Parts 2 and 4 need the most fixes.

## Coverage and limits

- Read in full: the target, 98-live.md, `CLAUDE.md`, the statcatlib `EGM.md` files (root, statcatlib, store, cli, tools), `inventory.py`, `admin.py`, the justfile, `pyproject.toml`, `pyrefly.toml`, `count_any.py`, `tests/test_inventory_conformance.py`, sdmxlib `_remote.py`, `_reader.py` and `tests/stores/`.
- Read in the parts that matter: `catalog.py`, `cross_product.py`, `conformance.py`, `_check.py`, `_rules.py`, `registry.py`, the spec (§6.3 change-item notes, §6.4, §6.5, the start of §6.6, §11.2), the sdmxlib plan's Phase 4, the hub `CLAUDE.md` and the hub design's contract and staging sections.
- DES-M6, N13 and ZM2 were located by search only and not re-audited.
- Working trees were read as they stand, and commit hashes were not confirmed.
- `obstore` is not on disk anywhere under `~/dev`, so F1 to F22 are not independently verified and are not treated as wrong. Three further `obstore` accessors the design relies on are stated in no F row: reading back `store.credential_provider`, `store.client_options` and the endpoint in `store.config`.

## Blockers

### IR-1. sdmxlib's work has no owner, no issue and no consistent release order

**Target:** design and plan. **Citations:** design:141, 150, 881, 889, 671, 154-155, 961; sdmxlib plan:322-344 (no S6, S10 or S11); 98-live.md:26; hub `CLAUDE.md:68`; spec:427, 519.

**Failure scenario:**

- The Part 1 implementer cannot tell whether to wait for an sdmxlib release, use `decode_manifest` privately, or build the temp-directory fallback.
- If S6 ships with S10 as line 671 suggests, Part 1 waits on the S10 rule. Its content depends on open O4, although Part 4 is meant to land last.
- Part 4's plan entry lists only the S10 rule and its conformance check, and leaves out S8's and S11's spec changes. GH-98 and the hub both require contract changes to go back first. S1, S2, S3, S7 and S9 appear in no Part.
- No sdmxlib issue exists to pick any of this up.
- S8 is stale. At `011312c3` spec:427 already says "`retire` or `correct`" and spec:519 already has the `correct` row.
- The leaf rule's §6.6 text sits on an unmerged branch, and no Part lists its merge as a precondition.

**Fix:** add a "Preconditions and order" table: item, repository, issue, spec or code, the sdmxlib release that carries it, the Part it gates. Separate S6 from S10. Choose one S6 path for Part 1 (recommended: the export ships in a small sdmxlib release before Part 1, with the fallback removed). State that S10 waits for O4. Close S8 against `011312c3`. Add S6, S10 and S11 to the sdmxlib plan's Phase 4.

### IR-2. `statcat upload` cannot be built for its production journey without invented decisions

**Target:** design. **Citations:** design:161-162, 376-377, 390-395, 415-416, 775, 923; `cli/EGM.md:87-89` (commands stay thin).

**Failure scenario:** a console command has no caller to supply a credential provider, and the `blob` extra contains no `azure-identity`. Upload's lookup refuses without a `TokenIdentity` or an `account_key`. So a publish job on workload identity reaches only the refusal, unless the implementer adds a dependency the design excludes or invents a credential scheme. The environment variable names, how URL maps to `AzureStore`, how `--release-at TIME` is parsed, whether a naive time is refused, and the exit codes are all undefined. Part 2's table has no CLI test.

**Fix:** specify the variable names, the URL grammar, the credential source (for example, CLI-only use of `obstore.auth.azure.AzureCredentialProvider` with `azure-identity` in the extra, or a documented dotted-path provider), the time format, the exit codes and CLI tests.

## Majors

### IR-3. There is no injection point for the fakes the tests require

**Target:** design. **Citations:** design:305, 335-336, 452, 517-526, 772, 909, 616-617, 928, 931; 98-live.md:103.

**Failure scenario:** every `tests/test_blob_storage.py` test over `MemoryStore` reaches `now()`, which has no endpoint and no way to receive a clock. "Fake `_ObjectOps`" has nowhere to be passed. GH-98's recording store for upload cannot be installed through the given signature, and D6's refusing store has no stated form. Upload is not stated to use `_ObjectOps`, and `_ObjectOps`'s methods are never listed. "A file lost after step 2" (line 931) needs a hook between steps. Each implementer invents a different private parameter.

**Fix:** give exact constructors, for example `BlobInventoryStorage(location, *, ops=None, clock=None)` and a private `_upload(..., ops=)`. List `_ObjectOps`'s methods. State that upload, `pull_edition` and the writer all use it.

### IR-4. Several tests assert HTTP-level facts `_ObjectOps` cannot see, and no recording proxy is specified

**Target:** design. **Citations:** design:906, 838, 936, 861, 951-952, 210-211, 892-895; sdmxlib `tests/stores/_stores.py` (none there).

**Failure scenario:** a wrapper over `obstore.put` sees arguments, not requests or headers. An implementer either weakens the assertion or builds an unplanned proxy. Part 1's table has no row for the `x-ms-blob-type` assertion or for F10.

**Fix:** add a recording HTTP proxy to `tests/stores/_stores.py`, with its interface, in Part 1. Name which rows use it and which use `_ObjectOps`. Add the missing rows.

### IR-5. Some Part 4 rows cannot pass as specified

**Target:** design. **Citations:** design:966-967, 983, 984, 985, 110, 111-112, 979, 99.

**Failure scenario:** rows 983 to 985 cannot run over Azurite in Phase 4: a seal, `doctor` and `prune` have no blob path. Row 979's skip has no code to call, because the fold is Phase 5, and a local catalog has no `pending/`. An implementer writes a tautology or stops.

**Fix:** mark each row local, blob or both. Reduce row 979 to what exists (`record` refuses a dropped edition, and the helper at `working_copy.py:315-319` reports recorded or tombstoned), and move the fold skip to GH-52's acceptance.

### IR-6. The Acceptance map and the Part tables do not agree on test files

**Target:** design. **Citations:** design:1035-1042, 926-936, 949-957, 969-988, 878; `CLAUDE.md:737-739` (every file needs `pytestmark`).

**Failure scenario:** four filenames appear only in the map, with no directory. A file at `tests/test_correct.py` marked `integration` never runs over Azurite. If it is also marked `stores`, `just test-stores` will not collect it, because that recipe is restricted to `tests/stores`. A shared parametrised body with two backends needs a helper nobody specified. Which rows belong to `test_correct.py` and which to `test_reinstate.py` is left open (rows 981, 983, 988). Each of the 14 GH-98 items has a row or a named artefact. The traceability is what fails.

**Fix:** add a File column to each table, using full paths. Define Part 4's layout, for example `tests/test_correct.py` (integration) and `tests/stores/test_correct_blob.py` over a shared `tests/support` scenario module.

### IR-7. `_verify_edition` as written refuses valid structure editions

**Target:** design. **Citations:** design:711-715 (`check_edition(at="publish", extensions=...)` with no `structures=`), 651-653, 759-760; sdmxlib `edition/_check.py:293`, 755-759 (with `structures is None`, every unresolved reference is a finding); statcatlib `testing.py:278-285` (CODR's concept scheme references a codelist by `core_representation`); spec:549 (item 9 names this exact case); `diagnostics.py:260-283`, 316 (`verify_catalog` passes `_structures_for`).

**Failure scenario:** `reinstate` or `correct` on a structure edition whose references resolve in another structure product raises `EditionCheckError`, though the edition is valid. Part 4's "`artefacts` on a structure entry" row (line 987) hits this if it uses CODR's concept product. A data edition's `partial_of` also goes unchecked, which makes this check weaker than `verify_catalog`'s. On blob, the `structures=` editions must also be local, so they must be pulled too.

**Fix:** specify `structures=` for both paths. Reuse `_structures_for` locally, and state which editions are pulled on blob and at what cost.

### IR-8. `_verify_edition`'s blob path has undefined inputs and an unlisted dependency

**Target:** design. **Citations:** design:525, 538, 711-715, 918, 943, 266, 963, F5.

**Failure scenario:** `CatalogAdmin.open(BlobLocation(cache_dir=None))` is legal, and then `correct` needs a pull directory. The behaviour could be a crash, a refusal or a temp directory, and nothing says which. On S3, no lookup can read a digest (F5), so whether `correct` and `reinstate` refuse or fall back to size-only checks is undecided. That choice sets how strong "every listed file verifies" is. An engine with `secrets` for the admin object is unstated. Part 4 silently depends on Part 3's `pull_edition` and Part 2's `_lookup`, and on an extension choice (`check_catalog`) the blob text does not state.

**Fix:** define admin behaviour with no `cache_dir` and on S3. Move the lookup to `store/blob.py` as public-to-the-package. State Part 4's dependency on Parts 2 and 3.

### IR-9. `InventoryRuleError(rule="if-seq")` and `rule="from-seq"` do not fit the error class

**Target:** design. **Citations:** design:630-633; statcatlib `store/inventory.py:140-151` (`rule` is `Rule | "lifecycle" | CrossRule`, `product: str` is required keyword-only); sdmxlib `inventory/_rules.py:62`.

**Failure scenario:** `correct` is catalog-wide and has no product, so `product=` must be invented. The two new literals fail pyrefly until the union changes, and this change is in no Part's file list. The closure's `full is None` case is unstated, though `commit` passes None (`inventory.py:596-597`).

**Fix:** state the new `Rule | Literal[...]` union, whether `product` becomes `str | None` (and update `EGM.md`), and the None-head behaviour.

### IR-10. Named symbols have no signature or behaviour

**Target:** design.

- `read_many`: design:885, its only mention.
- `pull_edition`: design:943. Its behaviour is in D6 steps 2 and 3, with no signature, return, error type or stated split from `Product.view`.
- `_lookup`: design:918. Only the name is given. `EditionFile` to store path is unstated, including `root: data` through `manifest.data_path` (`../data/` style, sdmxlib `_remote.py:94`), and so is the cache (line 792).
- `MANIFEST_READERS`: design:494. No file named. `cross_product.py` "imports only sdmxlib" (`store/EGM.md:181`), so `prefetch`'s thread pool and `read_many` in `blob.py` conflict with that invariant unless a direction is stated.
- `prefetch`: `InventoryWriter.refusals` takes any `Artefacts` callable (`inventory.py:322-338`), including `StoreArtefacts`, which has no `prefetch`. How local "keeps its sequential path" (line 494) is selected is unstated.
- `TokenIdentity`: how `AzureBlobRest` and `actor()` get `oid` and the token is unstated, including when no token has been fetched yet, since `oid` exists only after the provider has been called.
- `verify_remote(..., tiers=all)`: line 790 passes Python's builtin. It should be `("required", "lazy", "remote")`.

Verified as existing and adequate: `verify_remote`, `RemoteFile`, `check_on_open` (`sdmxlib.edition` exports), `MAX_RACES` (`inventory.py:99`), `corpus_embargoed` (`testing.py:556`), `recorded_routing`, `routing_fields`.

**Fix:** add a symbol table: module, signature, returns, raises.

### IR-11. The rules' cold cost on blob omits the leaf reads, and the leaf prefetch the design measures has no implementation

**Target:** design. **Citations:** statcatlib `store/cross_product.py:214`, 398 (`_planned` calls `full.leaves()` whenever an entry is incoming); sdmxlib `inventory/_reader.py:256-269`, 302-304 (leaves are read one at a time); design:496-499, 503, 587, 848-849, 1010, 885.

**Failure scenario:** a cold `release`, `rollback` or `correct` at 7,000 products takes about 70 s in sequential leaf GETs before any manifest. `to_read(full, leaves)` needs the same leaves, so D5's claimed gain from 63 s to 1 s does not reach the operator. `correct` step 3 also reads `from_seq`'s leaves. Part 5 measures a prefetch no Part builds. `read_many` may be meant for this, but nothing says so, and sdmxlib's reader has no hook for it.

**Fix:** specify a leaf prefetch, for example a caching `Fetch` that `read_many` fills from `root.products` before `full.leaves()`, and name its callers. Or state that writers pay 70 s until GH-125 and correct D5's table and the NFR.

### IR-12. The writer's long-lived reader is underspecified and conflicts with the reader floor

**Target:** design. **Citations:** design:331-335, 286-297, 886-887; sdmxlib `_reader.py:178-180` (floor), 146-181 (`refresh` mutates); `conformance.py:108`, 139-141; statcatlib `inventory.py:353-360`, 439-445, 595-615; `admin.py:576-587`.

**Failure scenario:** after a same-epoch storage restore (`restore_to`), a kept `FullInventory` holds the lost N forever. `_stored_root` raises `InventoryConflictError`, `commit` re-reads the same N, and it ends in `InventoryContentionError` after `MAX_RACES`. The conformance suite does not catch this, because no check calls `head` after `restore_to`. `full()` also now returns one shared mutable object to callers that assumed a snapshot, such as `prune`'s `full` and the `commit` loop.

**Fix:** add a Protocol member or a constructor flag. State that `full()` returns the shared reader and whether callers may hold it. Decide how a writer recovers when `_stored_root` finds its held root gone, for example by building a fresh reader on conflict. Have the test Backend's `head` use a fresh reader.

### IR-13. The conformance `Backend` for the blob writer is specified by one clause

**Target:** design. **Citations:** design:902-903; statcatlib `tests/test_inventory_conformance.py:76-197`; sdmxlib `conformance.py:104-145`, 468-502.

**Already works:** `commit`, `commit_at`, `head` map onto `try_commit` through `now=` and `built_from=`, so `_T0` bypasses `storage.now()`. `commit_interrupted` writing the leaf alone satisfies `conformance.py:232-244`, and `check_a_writer_without_stage_2...` works through `put`.

**Left open:** whether to factor a shared base or copy the adapter; how each factory call gets an empty catalog (prefix per backend); `restore_to`'s delete (`obstore`, or an unlisted `_ObjectOps.delete`); the declared `actor`; how `reader()` builds its `Fetch`; whether the plain `commit` path uses the real anonymous `HEAD` against the emulator. With the real `HEAD`, the 30 s bound (design:458) compares host time to container time, so a VM clock lagging by more than 30 s after sleep would fail every commit in the pre-push gate. Not independently verified on colima.

**Fix:** name a `tests/stores/_backend.py` with the factoring, the prefix rule, the actor value and the clock choice, and how the tier reports skew.

### IR-14. The D1 seam is incomplete where code will be written

**Target:** design. **Citations:** design:300-303, 311, 328-330, 352-353; statcatlib `store/inventory.py:300`, 376-393 (epoch test at 388, malformed `CURRENT` treated as absent at 386), 611, 614; `admin.py:249`; `catalog.py:537`, 625, 693.

**Failure scenarios:**

- `InventoryWriter`'s new signature is never written, and neither is how `ManifestArtefacts(fetch)` is defaulted.
- `root` "raises" on blob, but `CatalogAdmin.__repr__` reads it, so `repr(admin)` raises. `Catalog._root` is a `Path` used across repr and error messages, and the `_Opening` shape for blob is unstated.
- `advance_current(current, data)` does not say whether `data` is the root's bytes or `CURRENT`'s.
- Blob's rule "stopping when `held.seq >= root.seq`" drops the local rule's `restore_epoch` equality, so a new-epoch root with a lower `seq` never updates a stale higher `CURRENT`, and the two backends differ.
- A malformed `CURRENT` on blob is unaddressed.
- Whether the WARNING for a failed advance is caught in the storage or in `try_commit` decides whether local behaviour changes.

**Fix:** write the signatures. Keep the epoch test in one rule. State the catch location and use `storage.location` for messages.

### IR-15. Error types are undefined across the new surface

**Target:** design. **Citations:** design:787, 781-782, 395, 800, 796, 917-918, 111-112, 891, 352-353, 323-327, 467.

**Failure scenario:** each implementer picks `ValueError`, `RuntimeError` or a new class per site. Tests assert the class chosen, and raw `obstore` exceptions escape through `commit`, `Catalog.open` and `view()` undocumented. Fetch errors have no stated mapping (`bytes | None` covers absence only).

**Fix:** add an error table: site, class, base, attributes, what is exported, and the `obstore` exceptions treated as transport faults and how they surface.

### IR-16. The OAuth rig and the bearer test lack required details

**Target:** design. **Citations:** design:405-411, 933, 385-391; sdmxlib `tests/stores/_stores.py:53-60`, 114-130 (`start` polls plain HTTP and waits 120 s, `Running.base` is `http://`), F19.

**Failure scenario:** `AzureBlobRest(location)` holds its own `httpx` client against a self-signed TLS container. `allow_invalid_certificates` is `obstore`'s option, and the design states no way to pass it to `httpx`, so row 933 cannot pass without an invention. The readiness poll fails on a TLS-only port. Unspecified: Azurite's full command line, the audience and issuer values to mint, the container creation route under OAuth, and the `x-ms-version` value. `TokenIdentity` first runs over an emulator here, in Part 2, and Part 1's writer runs only with a declared actor, which is fine as an order and should be stated.

**Fix:** give the exact command, mint values, readiness probe and a certificate-trust parameter on `AzureBlobRest` (derive `verify` from `client_options` or take `verify=`).

## Minors

### IR-17. The public surface is not listed

**Target:** design. **Citations:** design:169-176, 884-885, 917-918; statcatlib `EGM.md:5-12`; `tests/test_public_api.py:152-181`. `statcatlib/__init__.py`, `store/EGM.md` and `docs/reference/` nav (strict mkdocs) are in no Part's list, so the implementer decides which of `BlobLocation`, `TokenIdentity`, `upload_edition`, `UploadReport`, `UploadCheckError` are exported. **Fix:** list exports per Part, and the nav pages.

### IR-18. Coverage effects are stated too narrowly

**Target:** design. **Citations:** design:852-854; justfile:93-94, 111 (`coverage` is not in `ci`); `pyproject.toml:94-97`; design:926-936, 949-957. `upload.py`, `AzureBlobRest`, `pull_edition`, `catalog.py`'s blob branches, the blob branch of `_verify_edition`, `record_uploaded` and `inventory_corpus` run only in `stores`, which `just coverage` excludes. The next `just coverage` could fall under the floor. The drop was not computed. Nothing in `just ci` catches it. **Fix:** state per module what is unit-tested, or add `stores` to the coverage recipe.

### IR-19. The `Any`-ratchet "care" has no instruction

**Target:** design. **Citations:** design:381; `tools/count_any.py:224-242`; `tools/EGM.md:41`, 52-54 (baseline 7, two-way); `pyrefly.toml:60-63`, 105-106; `lance.py:45-51`. `json.loads` on the JWT payload for `oid`, and untyped credential callbacks, trigger pyrefly's unknown rules in `src`. The natural `dict[str, Any]` raises the count above the baseline and fails `check-any`. **Fix:** state: no new `Any`; imports under `TYPE_CHECKING` as `lance.py` does; decode claims to a typed struct (msgspec), or guard with `isinstance`. State how the absent-extra test works (for example, a subprocess with `sys.modules["obstore"] = None`).

### IR-20. Listed changes have no test row

**Target:** design. **Citations:** design:888-891 against 900-913; 940-942 against 949-957. No row for `CatalogAdmin.open(BlobLocation)`, `CatalogAdmin.create` (including duplicate create), `prune` refusing a blob catalog, the `ManifestArtefacts(fetch)` SHA check or `prefetch`, or `catalog_id`, `description` or `refresh` on blob. `Product.editions` "through the pull" (line 942) has no D6 text and would download an edition's required and lazy files just to return `previous` (`catalog.py:942`). **Fix:** add the rows, and define `Product.editions` as a manifest-only read.

### IR-21. `record_uploaded` is underspecified

**Target:** design. **Citations:** design:810-823; statcatlib `store/working_copy.py:382`, 587-590; sdmxlib `inventory/_rules.py:178-196`, 233. `record` requires `lifecycle`, `retired=` and `kind`, with no stated source. The `embargoed` parameter can contradict the root's `embargo`, which the seal path follows, so `embargoed=False` on an embargo-on catalog writes `live`. Data products need their structure homes uploaded first, or item 4 refuses them. **Fix:** state each `record` argument's source, whether `embargoed` may differ from the root, and the upload order.

### IR-22. The `pending/` document has no byte format

**Target:** design and spec. **Citations:** design:793; spec:969. Key order, the `release_at` format and whether `null` is written are undefined, though Phase 5 will read this document and `catalog-format.md` must describe it. The reviewer saw no sdmxlib type for it, and its search was truncated at the result limit. **Fix:** define the JSON and who encodes it. If it is a contract, add it to the spec.

### IR-23. Part 5 leaves small choices open

**Target:** design. **Citations:** design:994-1006, 1019-1025. `dataflows`, `pull_bytes` and `artefacts` values are unspecified beyond target sizes. The 50-product `stores` test has no file. `bench/inventory_read.py`'s Azurite source is unstated. "An sdmxlib PR, or a subagent" names no owner, and the acceptance item ends in an owner decision with no stated trigger. **Fix:** state the generator values, the test file, how the bench starts Azurite, and who opens the sdmxlib PR.

### IR-24. Small text defects

**Target:** design.

- design:897 says "GH-98's `LocalStore`". 98-live.md:46 now says `MemoryStore`, so drop the sentence.
- design:789 omits `extensions={"statcat": check_catalog}`, though the text says it checks the `statcat` section. Without it, that check does not run (sdmxlib `_check.py:302`). Add it.
- design:870's "Parts 1 to 4 can release separately" holds only in order. Part 3's fixtures need Part 2, Part 4's blob path needs Parts 2 and 3, and Parts 1 and 4 need sdmxlib releases. Say so.

## Checked, no finding

- GH-98's 14 acceptance items each have a row or artefact. The gap is file mapping (IR-6).
- The Part 1 `to_read` test needs no Part 2: it is pure over `test_cross_product_rules.py`'s built leaves, and a recording `Artefacts` works.
- `Change` and `try_commit` already carry `retired`, `built_from` and `now`.
- `correct` and `reinstate` are event kinds exercised by `conformance.py:406`, 652.
- sdmxlib's `reinstate` signature matches design:761.
- The stamp path matches `check_on_open`'s layout.
- `EditionEngine(secrets=)` and `LocalRegistry.edition(remote=)` exist as D6 uses them.
- W4's 30 s is at spec:526.
- S10's signature appears at design:639 and the plan lists it under Part 4. Its one gap is the export from `sdmxlib.inventory`, which S10's table row implies.
- `Product.view`, `Product.editions`, `Catalog.refresh`, `description` and `catalog_id` are listed under Part 3, the right Part.
- `decode_manifest` is correctly unexported today: `sdmxlib/storage/__init__.py` does not export it, and `_routing.py:128` and `registry.py:1573` use it internally. Its second `path` argument is for messages (`_routing.py:127`), and D5 could say so.

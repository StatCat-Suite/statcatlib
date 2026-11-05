# Codebase details review, round 2: catalog-on-blob-storage.md, revision 3

**Counts:** 0 Blocker, 2 Major, 6 Minor. Nothing was run; every finding comes from reading the code, and CB-2 and CB-7 say which parts are unverified.

**Source read:**
- sdmxlib: the working tree at `/Users/gabrielgellner/dev/sdmxlib`. `pyproject.toml` says 0.85.0 and the top `CHANGELOG.md` entry is 0.85.0. `check_edition` and `check_on_open` have no `references` argument yet, which fits #371 being unmerged. Nothing looked newer than the design describes, but I cannot confirm `src/` equals `11f18e3f`.
- statcatlib: the working tree at `/Users/gabrielgellner/dev/statcatlib`.
- obstore: the stubs in `~/.cache/uv/archive-v0/CrCci5PwDHbOclfm/`.

Design line numbers below are in `/Users/gabrielgellner/dev/statcatlib/docs/design/catalog-on-blob-storage.md`.

## Major

### CB-1. Part 3 needs a bytes-to-`Manifest` decoder, which the design defers to Phase 5
- **Sites:**
  - Design:580 (`Product.editions`), design:840 (`pull_edition(..., manifest_bytes, dest)`).
  - Design:161 (S6), design:173 ("S16 alone"), design:287 (W12), design:966-967.
  - `sdmxlib/storage/_manifest.py:442,471`; `sdmxlib/edition/__init__.py:76-92`; `statcatlib/catalog.py:942-947`.
- **Evidence (read):**
  - `decode_manifest` is exported from neither `sdmxlib.edition` nor `sdmxlib.storage`. Only `_routing.py` and `local/registry.py` import it, from the private module.
  - `read_manifest` takes a directory.
  - statcatlib has no import from a private sdmxlib module today.
  - S6 says Phase 5's `ManifestArtefacts(fetch)` is the first caller, but Part 3 needs the decoder first.
- **Failure:**
  - `Product.editions` must return `manifest.previous` from a GET "with no pull". `LiveEdition` has no `previous`, and `LiveInventory` hides the leaf entry.
  - `pull_edition` receives bytes, yet it must read `files` and each sha256.
  - On the #371 release, a Part 3 implementer has three choices: import `sdmxlib.storage._manifest`, write the bytes to a temporary directory and call `read_manifest`, or parse the JSON by hand. W12 and the handoff table settle none of them.
- **Why this is reportable under W12:** the code shows the deferral's premise is wrong. It does not make Part 3 impossible, because the temporary-directory route uses only public API.
- **Fix, either of:**
  - Add the `decode_manifest` export to the #371 release, which reopens W12's "only".
  - State in D6 that `pull_edition` writes the bytes into the staged directory and calls `read_manifest` on it, and that `editions` does the same under `cache_dir`.

### CB-2. `AzureBlobRest` has no defined way to find its account and container on the production path
- **Sites:**
  - Design:467, 698-701, 723-724, 837.
  - `_store/_azure.pyi:354-355,387-404,440-445`.
- **Evidence (read):**
  - D7's store is `AzureStore.from_url(AZ_URL, credential_provider=..., client_options=...)`. The account comes only from the environment, and it is not passed as an argument.
  - The stub promises `prefix` from the URL path and environment reading in constructors.
  - `config` is described only as "the underlying Azure config parameters".
  - `AzureBlobRest(location)` must build `https://<account>.blob.core.windows.net/<container>/<prefix>/<path>`, and no URL derivation is written anywhere. D4 says only that `now()` takes the endpoint from `store.config`.
- **Failure:**
  - If `config` omits values taken from the environment, every production `statcat upload` fails at step 5's bearer `HEAD`, after all files have been sent. From memory of the Rust wrapper I believe it does omit them, but there is no Rust source on disk, so this is unverified.
  - Neither test tier sees this. `pytest-httpx` rows and the Azurite subclass use a store built with explicit config.
  - Only checklist items 3, 5 and 6 would catch it, and item 6 asks about `from_url`, not `AzureBlobRest`.
- **Fix:**
  - Have the CLI read `AZURE_STORAGE_ACCOUNT_NAME` and pass `account_name=` to `from_url`, and parse the container from `AZ_URL`.
  - State `AzureBlobRest`'s URL derivation in D7.
  - Add a unit row where the store comes from the CLI's own construction with only the environment variable set.

## Minor

### CB-3. `record_uploaded`'s table omits required `record` arguments
- **Sites:** design:779-791; `sdmxlib/inventory/_rules.py:178-196`; `statcatlib/store/working_copy.py:1784,1798`.
- **Evidence (read):** `record` also requires `leaf` (positional), `product_id` and `now`, and the table lists none of them. The table's other names and sources all hold.
- **Failure:** the `leaf` needs a `FullInventory` read, not the bare `head` the snippet shows. `InventoryWriter` has no public `now`, and design:954 mentions a `writer.now()` that does not exist. Round 1's M7 asked for each argument to be stated.
- **Fix:** add three rows: `leaf` from `writer.full()`, `product_id` from the manifest, and `now` as the seal's `datetime.now(UTC)` or the storage clock.

### CB-4. `inventory_corpus`'s values are incomplete, and its determinism row cannot pass as specified
- **Sites:** design:1086-1092, 1118; `sdmxlib/inventory/_rules.py:256`; `sdmxlib/inventory/_reader.py:325`.
- **Evidence (read):**
  - `edition_id`, `schema_version`, `lifecycle`, `kind` and `now` have no stated value.
  - `state_changed_at` comes from `now` and is in the leaf bytes, and the signature has no fixed time.
- **Failure:**
  - Row 1118's "determinism by seed" fails on leaf bytes if `now` comes from a clock.
  - A hard-coded `schema_version` that later differs from `SCHEMA_VERSION` makes `LiveInventory.editions()` return nothing, while the read counts still look right. `conformance.SCHEMA_VERSION` equals it today only by being the same literal.
- **Fix:** state the five values, with a fixed `now` derived from the seed and `sdmxlib.storage.schema.SCHEMA_VERSION`. `record` itself can build every entry, and no leaf rule refuses these values.

### CB-5. `bench/inventory_read.py` cannot import `tests.stores._stores` the way the design runs it
- **Sites:** design:1095-1096, 1148; `/Users/gabrielgellner/dev/sdmxlib/bench/tier3/bench_tier3.py:16,41`; `statcatlib/bench/corpus.py:29`.
- **Evidence (read):**
  - `uv run python bench/inventory_read.py` puts `bench/` on `sys.path[0]`, not the repository root, and `tests` is not installed, so the import raises `ModuleNotFoundError`. `tests/__init__.py` does exist.
  - sdmxlib's bench that does this import runs as `uv run python -m bench.tier3.bench_tier3`.
  - statcatlib's `bench/corpus.py` imports nothing from `tests`, so it sets no precedent.
- **Fix:** use `uv run python -m bench.inventory_read`.

### CB-6. "Before anything is written" for the actor refusal does not hold at the current call site
- **Sites:** design:443; `statcatlib/store/inventory.py:532-540`.
- **Evidence (read):** `try_commit` writes leaves at `:534` and evaluates `Event(actor=_actor(), ...)` at `:540`.
- **Failure:** a direct swap to `storage.actor()` writes the leaves, then raises `InventoryWriteRefusedError`. Row 1010 ("none refuses") does not assert that no leaf was written.
- **Fix:** D1 resolves the actor beside `check_writable`, and the row asserts no write.

### CB-7. `TokenIdentity.token()`'s freshness is unspecified
- **Sites:** design:436, 723, 833; `_store/_azure.pyi:290-314`.
- **Failure:** if `token()` returns the last token `__call__` saw, a Put of up to one hour can leave step 5's `HEAD` with an expired token and a 401 after everything was sent.
- **Evidence:** I read the stubs. I did not verify `obstore`'s credential caching behaviour.
- **Fix:** `token()` calls the provider each time, since `azure-identity` caches internally, or it checks `expires_at`. Add this to row 1042 or row 1010.

### CB-8. Remainder of round 1's M18 and m13: the lookup and the store paths are undefined
- **Sites:** design:663-678, 837-840; `sdmxlib/edition/_check.py:61`; `sdmxlib/edition/_copy.py:106-143`; `sdmxlib/storage/_manifest.py:67`.
- **Evidence (read):**
  - `verify_remote` and `check_edition` call `lookup(EditionFile)`, whereas `AzureBlobRest.head` takes a path.
  - The adapter is not in the Symbols table, although M18 listed `_lookup`.
  - D7's steps 2 and 3 name no path mapping, and they do not use `edition_files`.
  - `data_path` is `../data/`. A naive join gives `products/p/e/../data/x`, not `products/p/data/x`, the path D6's `data_uri` reads from. On my reading of the object store's path rules, the `..` form is refused (not run).
- **Fix:**
  - Add the adapter to Symbols.
  - State that paths are `products/<product>/` plus `edition_files`' result, or the same normalisation.

## Verdict by area

**1. D7's APIs: exist as named, except CB-2 and CB-8.**
- `read_manifest` raises `ManifestError` on an unsealed directory.
- `edition_kind` is at `working_copy.py:204`.
- `edition_files(manifest, *, tiers=)` is correct.
- `check_edition(at="receive", lookup=, extensions=)` works on a local sealed directory.
  - It hashes the edition's files and skips `data/` in `_files` (`_check.py:192,391`).
  - It runs `verify_remote` over `root: data` only (`:202-204`).
  - It then runs lake, bindings, references, tags and extensions.
- Step 5's claim holds, with one precision: the lake and bindings read the local `data/`, and the store's `data/` is checked by size and MD5. The publish job's disk must therefore hold the data files, which a local seal has.
- "The manifest limited to root: edition" can be built with `msgspec.structs.replace`, as `_check.py:203` does.
- `tiers=`, `Lookup` and `RemoteFile(size, digests)` match. An `md5` key in base64 compares against the manifest.
- `AzureStore.from_url` accepts `credential_provider` and `client_options={"timeout": "1h"}`, and duration strings are documented.
- `AzureCredentialProvider(DefaultAzureCredential())` is synchronous and returns `{"token", "expires_at"}`.
- The URL path becomes `prefix`.
- `store.credential_provider` exists.
- The Typer app and `_say` fit.
- One note, not a finding: `FAIL <file>: <detail>` matches `VerifyFinding`, but `str(sdmxlib Finding)` is `[check] subject: detail`, so the CLI must format explicitly.

**2. D6's code path: clean, except CB-1.**
- `remote=` and `engine=` are accepted together at `receive` and `open` (`_check.py:339-347,207`).
- Stamps are at `<stamps>/<product_id>/<edition_id>`, which matches the cache layout.
- `LocalRegistry.edition(path, remote=, engine=, manifest_sha256=)` exists, and `data_uri` resolves `../data/` against `remote`.
- `EditionEngine(secrets=)` and `sl.StoreSecret` exist.
- `engine.attach` counts holders, so `check_on_open` beside an open view is safe.
- `check_catalog` satisfies `Check`, and a `check_catalog_no_index` with the same signature will too.
- `_check.py:262` refuses `remote=` at `publish`, so handoff row 8 is accurate.
- `_Store._over` and `_init_lance` fit a pulled directory.
- `LiveInventory.head` raises `KeyError`, which `_head` already handles.

**3. D1's facts: clean, except CB-6.**
- Every named line and symbol is right.
- The test patches stay valid with delegates in `store/inventory.py`: `create_exclusive` is called with `(Path, data)`, and `_after` is generic.
- `InventoryWriter.root` is also read at `catalog.py:329,336,365,375,753,755`, `tests/conftest.py:107` and `tests/test_catalog_admin.py:303`, beyond `WorkingCopy` and `CatalogAdmin`. All of these are local, and `Catalog.seal` on blob stops at `_require_writer`, so this is not a finding.
- No test or bench reads `InventoryWriter._root`. `bench/corpus.py:170` reads `ManifestArtefacts._root`, which stays.
- `next_created_at`, `MAX_RACES = 8` and `try_commit(kind=, reason=)` all match.
- **Conformance:** `WriterBackend` calls only `try_commit`, never `InventoryWriter.commit`, so no `ManifestArtefacts` read happens. I walked `CHECKS` and `HISTORY_CHECKS` against D8's copy and found no blocker. That includes `commit_interrupted` writing the leaf alone, `restore_to` using a delete, and the opted-in refusal through `check_writable`.

**4. `record_uploaded` and `inventory_corpus`: signatures hold, tables incomplete (CB-3, CB-4).**
- `recorded_routing(routing_fields(manifest), kind)` exists and returns `dataflows, pull_bytes, artefacts`.
- `corpus_embargoed` exists at `testing.py:556`.
- A data entry may carry `artefacts`.

**5. Version claims: hold for Parts 1, 2 and 4. Part 3 needs one thing beyond #371 (CB-1).**
- The cap is `sdmxlib>=0.85,<0.86`.
- `run`, `run_history`, `HISTORY_CHECKS`, `next_root`, the routing fields and `decoder="stage-1-writer"` are in the tree at 0.85.0, and statcatlib's existing tests already import or use them.

**6. Round 1 closures:**
- Closed: B1 as far as code shows, M8, M9, M12, m1, m12, m15.
- Partly closed: M18 and m13, only for the lookup and path mapping (CB-8).

## Could not verify
- #371's text, the behaviour of `references=False`, and its stamp rule.
- That `AzureStore.config` omits or includes values from the environment (CB-2), and that `store.credential_provider` returns the same Python object, which D3's `TokenIdentity` lookup depends on.
- That `obstore.sign` honours a store's `prefix`, which the Azurite `AzureBlobRest` subclass needs. A failure there would show loudly in the tier.
- That importing `azure.identity.aio` at module load (`auth/azure.py:12`) succeeds without `aiohttp`. If it does not, `statcat upload` fails on a `[blob]` install that has only `azure-identity`.
- `obstore`'s credential caching (CB-7), and its refusal of `..` path segments (CB-8).
- That DuckDB reads `az://` over Azurite with this design's secret. sdmxlib's `tests/stores/_stores.py` has the connection-string secret, but I did not trace `root()`.
- That `src/` equals `11f18e3f`, and that `statcatlib` installs exactly 0.85 here. I found no installed `.venv` metadata.

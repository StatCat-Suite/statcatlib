# statcatlib.cli

## Overview

`statcatlib.cli` is the `statcat` command, a Typer app installed by
`[project.scripts] statcat = "statcatlib.cli:main"` in `pyproject.toml`. It
holds one module, `__init__.py`. Each command parses options, validates the
catalog directory, and calls one library function. The work itself lives in
`statcatlib.WorkingCopy.build_index` (`../store/working_copy.py`), which
delegates to the private `_Store.build_index` (`../store/store.py`), and in
`statcatlib.diagnostics` (`../diagnostics.py`) for `verify-catalog` and
`doctor`, in `statcatlib.CatalogAdmin.prune` (`../admin.py`) for `prune`, in
`statcatlib.upload_edition` (`../upload.py`) for `upload`, in
`statcatlib.CatalogAdmin` for `advance-current` and the `admin` group
(`CatalogAdmin.create` for `admin create`), in
`statcatlib.gc.collect_garbage` (`../gc.py`) for `gc`, and in
`statcatlib.release.ReleaseJob` (`../release.py`) for `release`.

The CLI needs the `search` extra (`uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'`), which supplies `typer`, `rich`,
`lancedb` and `sentence-transformers`. Importing the module without `typer`
and `rich` fails. `upload` needs the `blob` extra, which supplies `typer`, `rich`,
`obstore` and `azure-identity`, and so do `gc` and `release`, and `advance-current`
and `admin` over an `az://` catalog. `build-index` is the only command that needs `search`.

The console script is `statcat`, with eight commands, `build-index`,
`verify-catalog`, `doctor`, `prune`, `upload`, `advance-current`, `gc` and `release`, and the
`admin` group (`admin_app`, added with `app.add_typer`): `create`, `editions`,
`release`, `cancel`, `rollback` and `embargo`.

## Interface

Exported names (`__all__`):

- `app` — the root `typer.Typer` (`name="statcat"`, `no_args_is_help=True`,
  `add_completion=False`, `pretty_exceptions_show_locals=False`, so a printed frame
  never shows a token). Tests drive it with `typer.testing.CliRunner`.
- `admin_app` — the `admin` group's `typer.Typer`, added to `app` with
  `add_typer(admin_app, name="admin")`.
- `main()` — console-script entry point; calls `app()`.

Commands:

| Command | Function | Calls | Exit 1 when |
| --- | --- | --- | --- |
| `statcat build-index CATALOG PRODUCT` | `build_index` | `sc.Catalog.create(catalog, exist_ok=True).product(product).working_copy(resume=True).build_index(...)` | `catalog.json` is missing, the product has no unsealed edition, or `sentence_transformers` cannot be imported |
| `statcat verify-catalog PATH` | `verify_catalog` | `diagnostics.verify_catalog(path)` | any finding, each printed as `FAIL <file>: <detail>`; exit 2 on `CatalogUnreadableError`. Each stale data edition prints as `STALE <StaleEdition>` and leaves the exit code alone |
| `statcat doctor PATH` | `doctor` | `diagnostics.doctor(path)` | the report holds a warning (topic `stale data editions`: a stale embargoed data edition is one; a stale live edition is `info` and leaves the exit code alone; topic `partials with no structure home`: each item is one) |
| `statcat prune PATH --older-than DAYS --reason TEXT [--apply]` | `prune` | `sc.CatalogAdmin.open(path).prune(older_than=timedelta(days=DAYS), reason=TEXT, dry_run=not apply)` | `catalog.json` is missing, the catalog cannot be opened, the retire commit is refused, or a deletion failed (each printed as `FAIL <path>: <error>`) |
| `statcat advance-current URL` | `advance_current` | `CatalogAdmin.open(target).advance_current()` | the catalog cannot be read or `CURRENT` cannot be written (`ERROR <message>`); exit 2 as `upload` for an `az://` usage error |
| `statcat admin create AZ_URL` | `admin_create` | `CatalogAdmin.create(location).catalog_id`, printed alone on standard output; the line `Created the catalog <uri> with catalog_id <id>.` goes to standard error | the prefix holds `catalog.json` (`FileExistsError`, naming its `catalog_id`), it holds an object under `inventory/`, `products/` or `pending/` and no `catalog.json` (`IncompatibleCatalogError`), or an `InventoryError`, `BlobStoreError` or `ClientAuthenticationError`; each as `ERROR <message>` on standard error, with nothing written. Exit 2 for a directory (the message names `Catalog.create`), as `upload` for a usage error, `s3://` or a refused variable, and for a store other than `AzureStore` (`_require_azure`) |
| `statcat admin editions URL PRODUCT` | `admin_editions` | `CatalogAdmin.open(target).editions(product)`, one line per entry | the catalog cannot be read, or records no such product |
| `statcat admin release URL PRODUCT EDITION --reason TEXT` | `admin_release` | `.release(product, edition, reason=)` | a refusal (`InventoryRuleError`, `NonFastForwardError`, `CrossProductRuleError`, `ManifestError`) or a store, clock or credential failure, each as `ERROR <message>`; exit 2 on a missing or blank `--reason` |
| `statcat admin cancel URL PRODUCT EDITION --reason TEXT` | `admin_cancel` | `.cancel(product, edition, reason=)` | as `release` |
| `statcat admin rollback URL PRODUCT [--to EDITION] [--if-live EDITION] --reason TEXT` | `admin_rollback` | `.rollback(product, to=, if_live=, reason=)` | as `release` |
| `statcat admin embargo URL on\|off --reason TEXT` | `admin_embargo` | `.set_embargo(on=, reason=)` | as `release`, and the setting the catalog has already (`ValueError`) |
| `statcat gc AZ_URL --config FILE [--dry-run] [--forecast FILE] [--discard PRODUCT/EDITION ...]` | `gc` | `load_jobs_config(config)`, then `collect_garbage(location, jobs.retention, storage=BlobInventoryStorage(location), dry_run=, forecast=_forecast(file), discard=)` | a `JobsConfigError` (before any request; stdout empty), a `GcStoppedError`, `InventoryError` (the storage clock included), `BlobStoreError` or `ClientAuthenticationError` (`step.failed` on stdout, `ERROR <message>` on stderr), or a failed deletion (`FAIL <path>: <error>`); exit 2 as `upload` for a usage error, `s3://` or a refused variable, and for an unreadable or non-JSON `--forecast`, a `--discard` that is no `<product>/<edition>` or names a recorded edition (`ValueError`), or a store other than `AzureStore` (`_require_azure`) |
| `statcat release AZ_URL --config FILE [--dry-run]` | `release` | `load_jobs_config(config)`, then `ReleaseJob(location, jobs, rest=AzureBlobRest(location), listing=AzureReaderListing(access.storage_account, access.container, arm_token=, scopes=access.scopes), storage=BlobInventoryStorage(location)).run(dry_run=)` | a `JobsConfigError`, or a configuration with no `[access]` (before any request; stdout empty); an `[access]` for another container or account, or a bad resource id (`ValueError`, `ERROR <config>: <message>`); a run that stopped: `FileNotFoundError`, `InventoryError` (an opted-in catalog and the storage clock included), `BlobStoreError` or `ClientAuthenticationError` (`step.failed` with `detail.step` `start` on stdout, `ERROR <message>` on stderr); or a step in `ReleaseReport.failed` (`ERROR <step>: <error>`). Refusals, a held release and a release skipped by the access check leave exit 0. Exit 2 as `upload` for a usage error, `s3://` or a refused variable, and for a store other than `AzureStore` (`_require_azure`) |
| `statcat upload EDITION_DIR AZ_URL [--release-at TIME]` | `upload` | `upload_edition(edition_dir, location, release_at=)` | a finding, each printed as `FAIL <file>: <detail>`, or a `ManifestError`, `BlobStoreError` or `ClientAuthenticationError`, printed as `ERROR <message>`; exit 2 on a usage error, a bad `AZ_URL`, `s3://`, no `AZURE_STORAGE_ACCOUNT_NAME`, a refused `obstore` variable set, even empty, in any spelling `obstore` reads (`_REFUSED_VARIABLES`, matched by `_refused_variables`: `AZURE_` then any case), a naive time, a missing `blob` extra, or an environment value `obstore` cannot parse, such as `AZURE_DISABLE_TAGGING=junk` (`GenericError` from the store build, before any request) |

`build-index` options and their `WorkingCopy.build_index` keyword:

| Option | Default | Keyword |
| --- | --- | --- |
| `--lifecycle` | `revisable` | `working_copy(lifecycle=)` when the working copy is resumed |
| `--model` | `intfloat/multilingual-e5-base` (`_DEFAULT_MODEL`) | `model_name`, and the encoder that `_load_encoder` loads |
| `--document-prefix`, `--query-prefix` | from `ASYMMETRIC_MODEL_PREFIXES`, else `""` | `document_prefix`, `query_prefix` |
| `--batch-size` | 256 | sets env `STATCAT_EMBED_BATCH_SIZE` |
| `--device` | `auto` (`auto` passes `None` to SentenceTransformers) | encoder device |
| `--include-codes/--no-include-codes` | on | `include_codes` |
| `--embed-codes/--no-embed-codes` | off | `embed_codes`; turns `include_codes` on |
| `--max-codes-per-codelist` | 500 | `max_codes_per_codelist` |
| `--codelist-policy URN[,URN...]=mode` (repeatable) | none | `codelist_policies` (`sc.CodelistIndexPolicy`) |
| `--reference-metadata-blend PATH[:max_chars]` (repeatable) | none | `reference_metadata_blends` (`sc.ReferenceMetadataBlend`) |
| `--include-reference-metadata/--no-...` | off | `include_reference_metadata` |
| `--embed-reference-metadata/--no-...` | on | `embed_reference_metadata` |
| `--reference-metadataflow URN` (repeatable) | all sets | `reference_metadataflow_urns` (empty list becomes `None`) |

Private helpers that tests and later commands rely on:

- `_main()` — the empty `@app.callback()`. It keeps Typer in multi-command
  mode so that `build-index` parses as a command name.
- `_require_catalog(catalog)` — exits 1 unless the directory holds
  `catalog.json`, naming `Catalog.create`. It creates nothing.
- `_resume(catalog, product)` — opens the catalog with
  `Catalog.create(catalog, exist_ok=True)` and returns it with the product's
  resumed `WorkingCopy`. A product with no unsealed edition closes the
  catalog and exits 1, naming `working_copy()`.
- `_say(line, style=None)` — prints *line* escaped, unhighlighted and with
  `soft_wrap=True`, so a path in a finding is never split or styled.
- `console` — the shared `rich.console.Console`; `log` — the
  `statcatlib.cli` logger (currently unused).
- `_usage(message)` — prints it and exits 2. `_release_time(raw)`, `_catalog_uri(raw, *,
  command="upload")` and `_azure_account(command="upload")` parse the inputs, each through
  `_usage`, *command* naming the command in the message. `_refused_variables()` lists the
  environment's names that `obstore` would parse as a refused key, as spelled.
  `_azure_location(uri, account, *, command="upload", timeout=_UPLOAD_TIMEOUT)` builds the
  store with `obstore.store.from_url`, `TokenIdentity(AzureCredentialProvider(DefaultAzureCredential()))`
  and *timeout* (one hour for `upload`, `_ADMIN_TIMEOUT` five minutes for `advance-current`,
  `admin`, `gc` and `release`; Phase 5 D15's generalised helper). It returns the location of
  `_azure_clients(uri, account, *, command, timeout)`, which holds the four `blob` imports and
  returns the location with the `DefaultAzureCredential` its store asks for tokens; `release`
  asks that credential for its `access.ARM_SCOPE` token. `_azure_clients`
  turns obstore's `GenericError` from that build into `_usage`, with the text from
  `_store_build_message(exc)`: obstore's first line, and the `AZURE_DISABLE_TAGGING` spelling the
  environment sets, since obstore 0.12.1's message omits the variable. `_auth_error()` returns
  azure-core's `ClientAuthenticationError`, imported on the `blob` extra. The CLI is the only code in
  statcatlib that reads the environment for a store.
- `_admin_call(url, command, action)` — opens `CatalogAdmin` over *url* (a scheme makes it
  `az://`, through `_catalog_uri`, `_azure_account` and `_azure_location`; anything else is a
  directory) and runs *action*; `_ADMIN_ERRORS`, and the credential error on `az://`, print as
  `ERROR <message>` and exit 1. `_reason(reason)` exits 2 on a blank `--reason`. `_Switch` is
  `embargo`'s `on`/`off`. `admin create` builds its own location, with `_catalog_uri`,
  `_azure_account`, `_azure_location` at `_ADMIN_TIMEOUT` and `_require_azure`, and prints through
  `_say` (the id) and `_tell` (the human line and each `ERROR`).
- `gc`'s helpers: `err_console` (a `Console(stderr=True)`) and `_tell(line, style=None)`
  print the human lines on standard error. `_events(name)` attaches `_EventLines` to
  the logger *name* (INFO, no propagation) while the block runs, which prints each
  record's `jobs._EVENT_ATTRIBUTE` object as one compact JSON line on standard
  output and ignores any other record. `_forecast(path)` reads the `gc.planned`
  lines' paths, `_require_azure(location, command)` exits 2 for a store other than
  `obstore.store.AzureStore` (through `store.blob._load_obstore`, no new import),
  `_event(...)` builds `jobs.config` and `step.failed`, and `_gc_summary(report)`
  prints `FAILED-CLOSED`, `UNREGISTERED`, `DEVIATION`, `FAIL` and the summary.
- `release`'s helpers: it shares `_tell`, `_events` (on `_RELEASE_LOGGER`,
  `statcatlib.release`), `_event` and `_require_azure` with `gc`.
  `_release_summary(report, names)` prints `REFUSED <product>/<edition> <rule or ->:
  <reason>`, `OUTSIDER`, `UNCHECKED`, `UNUSED` (with the allowlist entry's name from
  *names*), `SKIPPED <step>: <reason>`, `ERROR <step>: <error>` and the summary. Tests
  replace the module names `AzureReaderListing` and `AzureBlobRest` with fakes, so no
  test of the command builds a client that could reach Azure.
- `_parse_codelist_policy(raw)` — accepts only mode `fts_only`
  (`_ACCEPTED_CODELIST_MODES`); raises `typer.BadParameter` otherwise.
- `_parse_reference_metadata_blend(raw)` — `max_chars` must be a positive
  integer; raises `typer.BadParameter` otherwise.
- `_resolve_prefixes(model, document_prefix, query_prefix)` — an explicit
  option wins over the table in `statcatlib.store.store.ASYMMETRIC_MODEL_PREFIXES`.
- `_load_encoder(model_name, device)` — returns an `EmbeddingModel`
  (the Protocol in `../store/store.py`).
- `_hush_encoder_logs()` — lowers the `httpx`, `huggingface_hub`,
  `transformers` and `sentence_transformers` loggers, adds the
  `.*unauthenticated.*` warnings filter, and calls
  `transformers.logging.set_verbosity_error()` and
  `disable_progress_bar()`.

## Invariants

- Commands stay thin. Validation and index building belong to
  `WorkingCopy.build_index`, so the Python API and the CLI behave the same
  way.
- `build-index`'s `CATALOG` argument has `exists=True, file_okay=False`, so
  Typer rejects a missing path or a non-directory before the command runs.
  Beyond that, `_require_catalog` checks for `catalog.json` before
  `Catalog.create(exist_ok=True)` runs, so a mistyped path never becomes a
  new catalog.
- `build-index` indexes a working copy, the product's unsealed edition, and
  leaves it unsealed. A sealed edition is never written (see the "Edition
  contract" paragraph of [`CLAUDE.md`](../../../CLAUDE.md)).
- `WorkingCopy.seal` rebuilds the index with the arguments of the last
  `build_index` call on the same `WorkingCopy` object. The CLI's call ends
  with its process, so a seal from a working copy resumed later rebuilds with
  the default arguments and the encoder passed to
  `working_copy(embedding_model=)`.
- `build-index` resumes with `--lifecycle` (`revisable` by default). A
  product recorded with another lifecycle exits 1, naming the option.
- `build-index` passes `model_name` to `WorkingCopy.build_index` on every
  call, so `search/embeddings.json` records the model and its prefixes.
- Imports of `sentence_transformers` and `transformers`
  stay inside the functions that use them, so `statcat --help` loads no
  encoder.
- Imports of `azure.identity`, `azure.core` and `obstore` stay inside the functions
  that use them, with `# noqa: PLC0415`, so `statcat --help` needs no `blob` extra.
- Every install hint names `uv add` first and `pip install` second.

## Side Effects

- `verify-catalog` and `doctor` write nothing. `prune` without `--apply`
  writes nothing; with `--apply` it writes one Inventory version (kind
  `retire`) and deletes edition directories and `data/` files.
- `admin editions` writes nothing. `admin create` writes `catalog.json` alone, with one
  create-only Put, after one read of `catalog.json` and one listing each of `inventory/`,
  `products/` and `pending/`, stopped at the first object; it reads no storage clock and asks
  for no token of its own. Each other `admin` command writes one
  Inventory version, and `advance-current` writes `CURRENT` alone, or nothing.
- `gc` writes nothing to the catalog. It deletes under `products/` and `pending/`
  unless `--dry-run`. Its standard output holds one JSON line per event and nothing
  else (`jobs.config`, the deletion log, the alerts, `step.failed`); its human lines
  and summary go to standard error.
- `release` writes up to three Inventory versions (kinds `seal`, `release`, `retire`)
  and `CURRENT`, and nothing under `products/` or `pending/`; with `--dry-run` it
  writes nothing. It sends requests to the store, one anonymous `HEAD` per step for the
  storage clock, a `HEAD` per file of each edition it folds, and the role assignment
  and role definition reads to Azure Resource Manager. It writes a temporary directory
  for the fold's reference check and deletes it. Its standard output holds one JSON
  line per event and nothing else; its human lines and summary go to standard error.

- `build-index` writes the working copy's `search/` directory: the
  edition's tables, `search/index.json`, and `search/embeddings.json`. It
  opens the catalog with `Catalog.create(catalog, exist_ok=True)`, which
  writes nothing on an existing catalog, and resumes the working copy, which
  settles the product's seal journal. After a successful build it closes the
  working copy (left unsealed) and the catalog.
- `build-index` sets `os.environ["STATCAT_EMBED_BATCH_SIZE"]` for the
  process, calls `logging.basicConfig(level=INFO)`, and calls
  `_hush_encoder_logs()`. That adds a process-wide `warnings.filterwarnings`
  entry for `.*unauthenticated.*`, sets `transformers` verbosity to error and
  disables its progress bars.
- `_load_encoder` first loads the model with `local_files_only=True`. On
  `OSError` it loads again with network access, which can download the
  model from Hugging Face.
- All commands but `gc` and `release` print to stdout; `admin create` prints the `catalog_id`
  there and its human line and errors on standard error. A command failure exits 1 (the cases in the
  table above). Usage errors exit 2 through Click: a `CATALOG` path that is
  missing or not a directory, `--batch-size` or `--max-codes-per-codelist`
  below 1 (`min=1`), `prune` without `--older-than` or `--reason` or with
  `--older-than` below 1, and a `typer.BadParameter` from the option parsers.

## Verification

- Run: `just ci` (the project gate).
- Unit tier only: `uv run pytest -m unit`.
- CLI tests: `uv run pytest -m unit tests/test_cli_build_index.py`; `advance-current` and
  `admin`: `tests/test_cli_admin.py`; `admin create`: `tests/test_cli_admin_create.py`, and
  `tests/stores/test_admin_create.py` on Azurite; `gc`: `tests/test_cli_gc.py`; `release`:
  `tests/test_cli_release.py`.

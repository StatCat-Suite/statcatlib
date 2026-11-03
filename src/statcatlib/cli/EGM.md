# statcatlib.cli

## Overview

`statcatlib.cli` is the `statcat` command, a Typer app installed by
`[project.scripts] statcat = "statcatlib.cli:main"` in `pyproject.toml`. It
holds one module, `__init__.py`. Each command parses options, validates the
catalog directory, and calls one library function. The work itself lives in
`statcatlib.WorkingCopy.build_index` (`../store/working_copy.py`), which
delegates to the private `_Store.build_index` (`../store/store.py`), and in
`statcatlib.diagnostics` (`../diagnostics.py`) for `verify-catalog` and
`doctor`, and in `statcatlib.CatalogAdmin.prune` (`../admin.py`) for `prune`.

The CLI needs the `search` extra (`uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'`), which supplies `typer`, `rich`,
`lancedb` and `sentence-transformers`. Importing the module without `typer`
and `rich` fails.

The console script is `statcat`, with four commands: `build-index`,
`verify-catalog`, `doctor` and `prune`.

## Interface

Exported names (`__all__`):

- `app` — the root `typer.Typer` (`name="statcat"`, `no_args_is_help=True`,
  `add_completion=False`). Tests drive it with `typer.testing.CliRunner`.
- `main()` — console-script entry point; calls `app()`.

Commands:

| Command | Function | Calls | Exit 1 when |
| --- | --- | --- | --- |
| `statcat build-index CATALOG PRODUCT` | `build_index` | `sc.Catalog.create(catalog, exist_ok=True).product(product).working_copy(resume=True).build_index(...)` | `catalog.json` is missing, the product has no unsealed edition, or `sentence_transformers` cannot be imported |
| `statcat verify-catalog PATH` | `verify_catalog` | `diagnostics.verify_catalog(path)` | any finding, each printed as `FAIL <file>: <detail>`; exit 2 on `CatalogUnreadableError`. Each stale data edition prints as `STALE <StaleEdition>` and leaves the exit code alone |
| `statcat doctor PATH` | `doctor` | `diagnostics.doctor(path)` | the report holds a warning (topic `stale data editions`: a stale embargoed data edition is one; a stale live edition is `info` and leaves the exit code alone; topic `partials with no structure home`: each item is one) |
| `statcat prune PATH --older-than DAYS --reason TEXT [--apply]` | `prune` | `sc.CatalogAdmin.open(path).prune(older_than=timedelta(days=DAYS), reason=TEXT, dry_run=not apply)` | `catalog.json` is missing, the catalog cannot be opened, the retire commit is refused, or a deletion failed (each printed as `FAIL <path>: <error>`) |

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
- Every install hint names `uv add` first and `pip install` second.

## Side Effects

- `verify-catalog` and `doctor` write nothing. `prune` without `--apply`
  writes nothing; with `--apply` it writes one Inventory version (kind
  `retire`) and deletes edition directories and `data/` files.

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
- All commands print to stdout. A command failure exits 1 (the cases in the
  table above). Usage errors exit 2 through Click: a `CATALOG` path that is
  missing or not a directory, `--batch-size` or `--max-codes-per-codelist`
  below 1 (`min=1`), `prune` without `--older-than` or `--reason` or with
  `--older-than` below 1, and a `typer.BadParameter` from the option parsers.

## Verification

- Run: `just ci` (the project gate).
- Unit tier only: `uv run pytest -m unit`.
- CLI tests: `uv run pytest -m unit tests/test_cli_build_index.py`.

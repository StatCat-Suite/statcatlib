# statcatlib.cli

## Overview

`statcatlib.cli` is the `statcat` command, a Typer app installed by
`[project.scripts] statcat = "statcatlib.cli:main"` in `pyproject.toml`. It
holds one module, `__init__.py`. Each command parses options, validates the
bundle folder, and calls one library function. The work itself lives in
`statcatlib.Catalog.build_index` (`../store/store.py`) and in
`statcatlib.bundle` (`../bundle.py`).

The CLI needs the `search` extra (`uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'`), which supplies `typer`, `rich`,
`lancedb` and `sentence-transformers`. Importing the module without `typer`
and `rich` fails.

Three docstrings are out of date. The module docstring and `main()`'s
docstring call the console script `statcatlib`, and the module docstring and
`_main()`'s docstring say `build-index` is the only command. The script is
`statcat`, and the app has five commands.

## Interface

Exported names (`__all__`):

- `app` — the root `typer.Typer` (`name="statcat"`, `no_args_is_help=True`,
  `add_completion=False`). Tests drive it with `typer.testing.CliRunner`.
- `main()` — console-script entry point; calls `app()`.

Commands:

| Command | Function | Calls | Exit 1 when |
| --- | --- | --- | --- |
| `statcat build-index BUNDLE` | `build_index` | `sc.Catalog(bundle).open().build_index(...)` | `meta.duckdb` is missing, or `sentence_transformers` cannot be imported |
| `statcat bundle rebind BUNDLE [--to URI] [--scratch-dir DIR] [--force]` | `bundle_rebind` | `statcatlib.bundle.normalize_lake_data_path` | the bundle check fails, or `BundleDataPathError` is raised |
| `statcat bundle explain BUNDLE` | `bundle_explain` | `statcatlib.bundle.inspect_bundle` | the bundle check fails, or the verdict starts with `ambiguous` and no target or write is decided |
| `statcat bundle check BUNDLE [--references/--no-references]` | `bundle_check` | `statcatlib.bundle.check_bundle` | the report has any finding |
| `statcat bundle attach-cmd BUNDLE [--data-path P] [--read-only]` | `bundle_attach_cmd` | none; prints a DuckLake `ATTACH` statement | the bundle check fails |

`build-index` options and their `Catalog.build_index` keyword:

| Option | Default | Keyword |
| --- | --- | --- |
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

- `bundle_app` — the `bundle` sub-Typer, registered with
  `app.add_typer(bundle_app, name="bundle")`.
- `_main()` — the empty `@app.callback()`. It keeps Typer in multi-command
  mode so that `build-index` parses as a command name.
- `console` — the shared `rich.console.Console`; `log` — the
  `statcatlib.cli` logger (currently unused).
- `_require_bundle(bundle) -> Path` — exits 1 unless the folder holds both
  `meta.duckdb` and `lake.ducklake`; returns the path it was given.
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

- Commands stay thin. Validation, data-path policy, manifest checks and index
  building belong to `statcatlib.bundle` and `Catalog.build_index`, so the
  Python API and the CLI behave the same way.
- Every command's `BUNDLE` argument has `exists=True, file_okay=False`, so
  Typer rejects a missing path or a non-directory before the command runs.
  Beyond that, `bundle rebind`, `explain` and `attach-cmd` require
  `meta.duckdb` and `lake.ducklake` through `_require_bundle`. `build-index`
  checks only `meta.duckdb`, with its own inline copy of the check.
  `bundle check` leaves the contents to `check_bundle`.
- `bundle explain` and `bundle check` are read-only. `explain` takes no write
  lock.
- `bundle rebind` is idempotent. A bundle that already points at the target
  completes with no writes and exits 0.
- `bundle rebind` refuses to rewrite a remote `data_path` to a local one
  unless `--force` is given. The refusal comes from
  `normalize_lake_data_path` as `BundleDataPathError`.
- `--to` and `--data-path` default to `<bundle>/parquet/`, with a trailing
  slash.
- `attach-cmd` writes through `typer.echo` on one unwrapped line so the
  output can be piped into the `duckdb` CLI. All other output goes through
  the shared `rich.console.Console`.
- `bundle check` prints each finding with `markup=False`, because a
  finding's `[check]` prefix is plain text.
- Run `build-index` before `publish_bundle`. A later write to the bundle
  fails the manifest's files check (see the "Bundle contract" paragraph of
  [`CLAUDE.md`](../../../CLAUDE.md)).
- `build-index` passes `model_name` to `Catalog.build_index` on every call.
  Without it, `Catalog.build_index` writes no `search/embeddings.json`, the
  next `open()` restores neither prefix, and `_warn_about_prefixes` warns.
- Imports of `sentence_transformers`, `transformers` and `statcatlib.bundle`
  stay inside the functions that use them, so `statcat --help` loads no
  encoder.
- Every install hint names `uv add` first and `pip install` second.

## Side Effects

- `build-index` writes the bundle's `search/` folder: new generation tables,
  `search/index.json`, and `search/embeddings.json`.
  It opens the catalog with `Catalog(...).open()` and does not close it.
- `build-index` sets `os.environ["STATCAT_EMBED_BATCH_SIZE"]` for the
  process, calls `logging.basicConfig(level=INFO)`, and calls
  `_hush_encoder_logs()`. That adds a process-wide `warnings.filterwarnings`
  entry for `.*unauthenticated.*`, sets `transformers` verbosity to error and
  disables its progress bars.
- `_load_encoder` first loads the model with `local_files_only=True`. On
  `OSError` it loads again with network access, which can download the
  model from Hugging Face.
- `bundle rebind` rewrites `data_path` in `lake.ducklake`. When the lake file
  is on a read-only mount and `--scratch-dir` is given, it creates that
  directory (with parents), copies the lake file into it and rewrites the
  copy.
- `bundle check` with `--references` (the default) runs the costly check
  that every structural reference resolves in the bundle.
- All commands print to stdout. A command failure exits 1 (the cases in the
  table above). Usage errors exit 2 through Click: a `BUNDLE` path that is
  missing or not a directory, `--batch-size` or `--max-codes-per-codelist`
  below 1 (`min=1`), and a `typer.BadParameter` from the option parsers.

## Verification

- Run: `just ci` (the project gate).
- Unit tier only: `uv run pytest -m unit`.
- CLI tests: `uv run pytest -m unit tests/test_cli_build_index.py tests/test_bundle.py tests/test_bundle_manifest.py`.

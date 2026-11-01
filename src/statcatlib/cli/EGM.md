# statcatlib.cli

## Overview

`statcatlib.cli` is the `statcat` command, a Typer app installed by
`[project.scripts] statcat = "statcatlib.cli:main"` in `pyproject.toml`. It
holds one module, `__init__.py`. Each command parses options, validates the
catalog folder, and calls one library function. The work itself lives in
`statcatlib.Catalog.build_index` (`../store/store.py`).

The CLI needs the `search` extra (`uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'`), which supplies `typer`, `rich`,
`lancedb` and `sentence-transformers`. Importing the module without `typer`
and `rich` fails.

Three docstrings are out of date. The module docstring and `main()`'s
docstring call the console script `statcatlib`, and the module docstring and
`_main()`'s docstring say `build-index` is the only command. The script is
`statcat`. Since Phase 2a S1 removed the `bundle` group, `build-index` is
again the only command.

## Interface

Exported names (`__all__`):

- `app` — the root `typer.Typer` (`name="statcat"`, `no_args_is_help=True`,
  `add_completion=False`). Tests drive it with `typer.testing.CliRunner`.
- `main()` — console-script entry point; calls `app()`.

Commands:

| Command | Function | Calls | Exit 1 when |
| --- | --- | --- | --- |
| `statcat build-index BUNDLE` | `build_index` | `sc.Catalog(bundle).open().build_index(...)` | `meta.duckdb` is missing, or `sentence_transformers` cannot be imported |

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

- `_main()` — the empty `@app.callback()`. It keeps Typer in multi-command
  mode so that `build-index` parses as a command name.
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
  `Catalog.build_index`, so the Python API and the CLI behave the same way.
- `build-index`'s `BUNDLE` argument has `exists=True, file_okay=False`, so
  Typer rejects a missing path or a non-directory before the command runs.
  Beyond that, it checks that the folder holds `meta.duckdb`.
- Run `build-index` before an edition is sealed. A later write fails the
  manifest's files check (see the "Edition contract" paragraph of
  [`CLAUDE.md`](../../../CLAUDE.md)).
- `build-index` passes `model_name` to `Catalog.build_index` on every call.
  Without it, `Catalog.build_index` writes no `search/embeddings.json`, the
  next `open()` restores neither prefix, and `_warn_about_prefixes` warns.
- Imports of `sentence_transformers` and `transformers`
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
- All commands print to stdout. A command failure exits 1 (the cases in the
  table above). Usage errors exit 2 through Click: a `BUNDLE` path that is
  missing or not a directory, `--batch-size` or `--max-codes-per-codelist`
  below 1 (`min=1`), and a `typer.BadParameter` from the option parsers.

## Verification

- Run: `just ci` (the project gate).
- Unit tier only: `uv run pytest -m unit`.
- CLI tests: `uv run pytest -m unit tests/test_cli_build_index.py`.

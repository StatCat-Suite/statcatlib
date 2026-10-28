# statcatlib Blueprint

## System Overview

statcatlib builds queryable open data catalog engines for DCAT-AP,
StatDCAT-AP, CKAN, SDMX and PROV-O catalogs. Ingest materializes the catalog
into one DuckDB database with two schema owners: statcatlib's `statcat` star
schema and sdmxlib's `sdmx` structural tables. SQL JOINs over URN foreign keys
traverse the graph, and LanceDB (`statcatlib[search]`) adds per-language BM25
and vector search. A published catalog ships as a portable bundle with a
sha256 manifest.

`README.md` is the user guide and PyPI description; `CLAUDE.md` records the
design decisions in full. This file indexes the code contracts.

## Subdirectories & System Boundaries

- `src`: [`./src/EGM.md`](./src/EGM.md) — src layout holding the one package.
  - `src/statcatlib`: [`./src/statcatlib/EGM.md`](./src/statcatlib/EGM.md) —
    public API re-exports, `bundle.py`, `query.py`, `expr.py`, `urn.py`,
    `testing.py`.
  - `src/statcatlib/model`: [`./src/statcatlib/model/EGM.md`](./src/statcatlib/model/EGM.md)
    — attrs domain model with no I/O.
  - `src/statcatlib/store`: [`./src/statcatlib/store/EGM.md`](./src/statcatlib/store/EGM.md)
    — DuckDB-backed `Catalog`, `QueryBuilder`, scopes, schema DDL, LanceDB
    index.
  - `src/statcatlib/sources`: [`./src/statcatlib/sources/EGM.md`](./src/statcatlib/sources/EGM.md)
    — SDMX and CKAN ingest connectors.
  - `src/statcatlib/cli`: [`./src/statcatlib/cli/EGM.md`](./src/statcatlib/cli/EGM.md)
    — the `statcat` command.
- `tools`: [`./tools/EGM.md`](./tools/EGM.md) — gate scripts for docstrings,
  package indexes, prose and the `Any` ratchet.
- `tests`, `docs`, `design`: test suite, mkdocs site, and historical design
  specs. They hold no production code and carry no contract.

## Global Invariants

- statcatlib depends on sdmxlib in one direction. `model/` imports only
  `InternationalString`; sdmxlib's SDMX model types never appear in a domain
  model field.
- statcatlib copies no SDMX structure. `AggregateDataset.sdmx_dataflow_urn`
  links into `sdmx.dataflow`, and structural filters JOIN through it.
- All SQL binds values as parameters; f-strings build only clause structure.
  Caller-supplied keyword keys reach SQL only through a code-owned key to
  column map (GH-71).
- `Catalog.open()` and `from_connection()` raise `IncompatibleCatalogError`
  when the stored `statcat._meta` version differs from this build's, and when
  the stored `sdmx._meta` version differs while `sdmx.artefact` holds rows. An
  absent or empty `sdmx` schema passes through for sdmxlib to rebuild.
  Bumping either version means rebuilding every catalog.
- `Catalog.open()` refuses a search manifest version above
  `lance._MANIFEST_VERSION`. A stale index in a readable format warns and keeps
  serving; nothing on the open or search path rebuilds it (GH-3).
- `publish_bundle` runs after `build_index()`. Any later write to the bundle
  fails the manifest's file check.
- Every test file sets `pytestmark`; `just ci` selects tests by marker.

## Global Side Effects

- One DuckDB file per catalog folder, shared by the `statcat` and `sdmx`
  schemas; DuckLake holds observations under `parquet/`.
- `{catalog}/search/` holds generation-suffixed LanceDB tables and the
  `index.json` manifest, replaced atomically on rebuild.
- `bundle.manifest.json` is written by `publish_bundle`.
- Network access comes from SDMX registries (through sdmxlib) and the CKAN
  Action API (`httpx`), and from embedding model downloads when vector search
  is enabled.

## System Verification

- Run: `just ci` (index guard, lint, format, docstrings, prose, `Any` ratchet,
  pyrefly, unit and integration tests, strict docs build).

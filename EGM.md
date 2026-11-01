# statcatlib Blueprint

## System Overview

statcatlib builds queryable open data catalog engines for DCAT-AP,
StatDCAT-AP, CKAN, SDMX and PROV-O catalogs. A catalog is a directory of
products, each a chain of immutable sdmxlib editions recorded in an
Inventory. Ingest writes into a product's working copy, one edition's
DuckDB database with two schema owners: statcatlib's `statcat` star schema
and sdmxlib's `sdmx` structural tables. SQL JOINs over URN foreign keys
traverse the graph, and LanceDB (`statcatlib[search]`) adds per-language BM25
and vector search. A seal writes the edition's manifest of file digests and
records the edition in the Inventory; readers query a product's live
edition through an `EditionView`.

`README.md` is the user guide and PyPI description; `CLAUDE.md` records the
design decisions in full. This file indexes the code contracts.

## Subdirectories & System Boundaries

- `src`: [`./src/EGM.md`](./src/EGM.md) — src layout holding the one package.
  - `src/statcatlib`: [`./src/statcatlib/EGM.md`](./src/statcatlib/EGM.md) —
    public API re-exports, `catalog.py`, `checks.py`, `bundle.py`,
    `query.py`, `expr.py`, `urn.py`, `testing.py`.
  - `src/statcatlib/model`: [`./src/statcatlib/model/EGM.md`](./src/statcatlib/model/EGM.md)
    — attrs domain model with no I/O.
  - `src/statcatlib/store`: [`./src/statcatlib/store/EGM.md`](./src/statcatlib/store/EGM.md)
    — the private `_Store`, `EditionView`, `WorkingCopy`, the Inventory
    writer, `QueryBuilder`, scopes, schema DDL, LanceDB index.
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
- `Catalog.open` and `Catalog.create` refuse a legacy layout (a root-level
  `meta.duckdb`, a `bundle.manifest.json`, or files and no `catalog.json`)
  with `IncompatibleCatalogError` before writing anything.
- `Product.view` and `Product.working_copy` raise `IncompatibleCatalogError`
  when the edition's `statcat._meta` version differs from this build's, and
  when its search manifest records a version other than
  `lance._MANIFEST_VERSION` (3). sdmxlib refuses another `sdmx` store schema
  with `sdmxlib.StoreSchemaError`. Bumping either schema version means
  rebuilding every catalog. A stale index in a readable format warns and
  keeps serving; nothing on the view or search path rebuilds it (GH-3).
- `store/inventory.py`'s `InventoryWriter` is the only code that writes
  `catalog.json`, `inventory/` and `CURRENT`.
- A product has one unsealed edition at a time. `WorkingCopy.seal` rebuilds
  the search index, computes the `statcat` section with
  `bundle.catalog_section`, seals with `sdmxlib.edition.seal`, and records
  the edition. Nothing writes to a sealed edition: its store refuses every
  write method, and a later write would fail the manifest's files check.
- Every writer settles the product's seal journal (the journal rule,
  `store/working_copy.py`) when a working copy starts or resumes and around
  each seal, so an edition a crash left sealed and unrecorded gets recorded.
- Every test file sets `pytestmark`; `just ci` selects tests by marker.

## Global Side Effects

- `Catalog.create` writes `catalog.json`; each seal writes a leaf, an
  Inventory root under `inventory/` and `CURRENT`.
- One `metadata.duckdb` per edition under `products/<product>/<edition_id>/`,
  shared by the `statcat` and `sdmx` schemas; sdmxlib's DuckLake holds
  observations, its data files shared by the product's editions under
  `products/<product>/data/`.
- `<edition_dir>/search/` holds LanceDB tables named by edition id and the
  `index.json` manifest, replaced atomically on rebuild.
- `sdmxlib.edition.seal` writes the journal entry under
  `products/<product>/journal/` and the edition's `manifest.json`;
  statcatlib clears the journal entry once the edition is recorded.
- Network access comes from SDMX registries (through sdmxlib) and the CKAN
  Action API (`httpx`), and from embedding model downloads when vector search
  is enabled.

## System Verification

- Run: `just ci` (index guard, lint, format, docstrings, prose, `Any` ratchet,
  pyrefly, unit and integration tests, strict docs build).

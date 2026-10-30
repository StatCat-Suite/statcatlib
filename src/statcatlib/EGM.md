# statcatlib

## Overview

`statcatlib` is the package root. `__init__.py` re-exports the public API of
`model`, `sources` and `store`, `NotLoadedError` and `is_unloaded` from the
store's sentinel module, and part of `bundle` and `expr` (listed below). It
exposes `query` and `urn` as submodules (`sc.query`, `sc.urn`). The
top-level modules hold the parts that sit across subpackages: portable bundles
(`bundle.py`), composable query filters (`query.py`), traversal expressions
(`expr.py`), SDMX URN builders (`urn.py`) and synthetic test bundles
(`testing.py`).

## Interface

### Subpackages

- `model/`: [`./model/EGM.md`](./model/EGM.md) — attrs domain model (datasets,
  agents, distributions, SKOS concepts); imports only `InternationalString`
  from sdmxlib.
- `store/`: [`./store/EGM.md`](./store/EGM.md) — `Catalog`, `QueryBuilder`,
  scopes, DDL, LanceDB search index.
- `sources/`: [`./sources/EGM.md`](./sources/EGM.md) — SDMX and CKAN ingest
  connectors.
- `cli/`: [`./cli/EGM.md`](./cli/EGM.md) — the `statcat` Typer command.

### `bundle.py`

Re-exported from `statcatlib`:

- `open_bundle(bundle_uri, *, data=Auto(), access_mode=...,
  azure_credentials=None)`: opens a bundle (the `meta.duckdb` +
  `lake.ducklake` + `parquet/` triple) and resolves the DuckLake `data_path`
  stamp under the `data` policy.
- `Embedded`, `Colocated`, `At`, `Auto`: data-path policies. `Embedded` honours
  the stamp and never writes. `Colocated` points the stamp at
  `<bundle>/parquet/`. `At(path, *, persist=False)` attaches with
  `OVERRIDE_DATA_PATH` for that open only, and rewrites the stamp only with
  `persist=True` on a writable open. `Auto` chooses from the stamp.
- `inspect_bundle(source) -> BundleInfo`: read-only diagnosis of the stamp
  (`target`, `will_write`, `verdict`).
- `read_current_data_path(source)`, `normalize_lake_data_path(lake_path,
  target_data_path, *, scratch_dir=None, force=False) -> Path`: read and
  rewrite the stamp.
- `BundleDataPathError(RuntimeError)`: an unresolvable stamp or a refused
  remote-to-local rewrite.

Imported from `statcatlib.bundle`:

- `DataPolicy`: the union type of the four policies.
- `publish_bundle(path, *, bundle_id, producer, product=None,
  extensions=None, provenance=None) -> Manifest`: writes sdmxlib's
  `bundle.manifest.json` with the `statcat` section.
- `catalog_section(path)`: builds the `statcat` manifest section.
- `check_catalog(root, manifest, reg)`, `check_bundle(path, references=True) ->
  Report`: sdmxlib's bundle checks plus the catalog checks.
- `overlay_affects_search(overlay, types)`: lists overlay entries that change
  search results.
- `STATCAT_SECTION = "statcat"`, `STATCAT_SECTION_FORMAT = 2`.

### `query.py`

- Filter constructors: `has_code`, `descendants_of`, `within`,
  `has_dimensions`, `distribution`, `has_service`, `publishes`, `serves`,
  `has_spatial`, `has_spatial_uri`, `has_keyword`, `title_contains`,
  `description_contains`. `has_spatial` also matches a region's concept
  label; `has_spatial_uri` filters on a spatial facet key that is a URI.
- Filter nodes with `to_sql(ctx) -> (fragment, params)`: `MapFilter`,
  `ExistsFilter`, `SdmxCodelistFilter`, `SdmxCodeFilter`,
  `SdmxDimensionFilter`, `PositionalFilter`.
- Data holders compiled by other nodes: `FieldFilter` (`field`, `op`,
  `value`), `MapCondition` (inside `ExistsFilter`), `CodeDescent` (inside
  `SdmxCodeFilter`).
- Outside `query.__all__`, used by `store/`: `CompileContext` (join state
  for filter compilation, subclassed as `_QueryContext` in `store/store.py`)
  and `label_norm_sql(expr)` (SQL that normalizes labels: accents,
  punctuation, whitespace).

### `expr.py`

- `each(...)` and the expression nodes used by scope `.enrich()`,
  `.filter()` and `.sort_by()`. `statcatlib` re-exports `each`, `Expr`,
  `TraversalExpr`, `AliasedExpr`, `CountExpr` and `ExistsExpr`; `SumExpr`,
  `IdsExpr`, `DistinctExpr`, `FilteredExpr` and `CountDistinctExpr` are
  imported from `statcatlib.expr`.

### `urn.py`

- `codelist`, `dataflow`, `data_structure`, `concept_scheme(agency, id,
  version="1.0")` and `parse(urn)`: wrappers over sdmxlib's `SdmxUrn`.

### `testing.py`

- `synthetic_catalog(path, shape, *, seed=0, index=True,
  bundle_id="synthetic") -> Manifest`: builds a reproducible published statcat
  bundle.

## Invariants

- Filters bind every value as a `?` parameter (e.g. `d.{col}[?] = ?` with
  params `[key, value]`). `CompileContext.join_params` keeps insertion order.
- Column names in the SQL text come from code. `distribution(**kwargs)` and
  `has_service(**kwargs)` map keys through `_DISTRIBUTION_KWARG_COLS` and
  `_SERVICE_KWARG_COLS` and raise `ValueError` on any other key (GH-71).
  `publishes` and `serves` map theirs through `_DATASET_KWARG_COLS` and drop
  unknown keys.
- Positional filters are ANDed. The keyword arguments of one `distribution()`
  call must match the same distribution row; two calls require two matching
  rows.
- statcatlib owns `CompileContext`. sdmxlib receives a `SqlRelation`
  (`sl.q.hierarchy_applicability`) and shares no compile state.
- Hierarchy descent reads the `sdmx.hierarchy_applicability` relation through
  two `LEFT JOIN`s on `dataflow_urn`: an existence relation and an unnested
  pairs relation. Where no HierarchyAssociation applies, it falls back to the
  codelist's parent chain in `sdmx.code.ancestor_ids`; that fallback is the one
  hierarchy rule statcatlib owns. Descent is strict unless `include_self=True`.
- `publish_bundle` runs last, after `Catalog.build_index()`. The manifest
  records each file's sha256, so a later write fails `check_bundle`.
  Republishing mints a new token. `publish_bundle` raises `ValueError` when
  `extensions` already holds a `statcat` section or when the search index
  predates per-language tables.
- `open_bundle` passes no `DATA_PATH` to `ATTACH` when it honours the stamp.
  `Auto` rewrites only a local stamp that does not resolve, and only when
  `<bundle>/parquet` exists or the lake holds no data files; otherwise it
  raises `BundleDataPathError`. A remote-to-local rewrite needs `force=True`.
- `normalize_lake_data_path` reads first: a matching stamp takes no lock and
  issues no `UPDATE`.
- A `read_only` open never writes the stamp, so parallel read-only workers on
  an unmoved bundle are safe.
- A stale stamp fails at the first observation read, after `ATTACH` succeeds.
  `inspect_bundle` detects it in advance.
- `Expr` nodes are frozen attrs objects and run no SQL themselves.

## Side Effects

- Stamp rewrites (`normalize_lake_data_path`, `Colocated`, `At` with
  `persist=True`, `Auto` on a moved bundle) take the lake write lock and `UPDATE`
  `ducklake_metadata.data_path`.
- On a read-only mount with `scratch_dir`, the lake file is copied into
  `scratch_dir` and the copy is rewritten.
- `publish_bundle` writes `bundle.manifest.json`.
- `synthetic_catalog` writes a full bundle: sdmxlib's synthetic bundle, the
  catalog, the search index when `index=True`, and the manifest.
- `read_current_data_path` and `inspect_bundle` open read-only DuckDB
  connections.
- These modules make no network calls; remote schemes (`s3`, `gs`, `http`, …)
  are classified as strings only.

## Verification

- Run: `just ci` (unit tier alone: `uv run pytest -m unit`).

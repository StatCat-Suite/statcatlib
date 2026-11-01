# statcatlib

## Overview

`statcatlib` is the package root. `__init__.py` re-exports the public API of
`model`, `sources` and `store`, `NotLoadedError` and `is_unloaded` from the
store's sentinel module, and part of `expr` (listed below). It
exposes `query` and `urn` as submodules (`sc.query`, `sc.urn`). The
top-level modules hold the parts that sit across subpackages: the `statcat`
section of an sdmxlib edition manifest (`bundle.py`), composable query filters
(`query.py`), traversal expressions (`expr.py`), SDMX URN builders (`urn.py`)
and synthetic test editions (`testing.py`).

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

Imported from `statcatlib.bundle`; nothing here is re-exported from
`statcatlib`:

- `catalog_section(cat) -> dict`: builds the `statcat` manifest section from an
  open `Catalog`, for `sdmxlib.edition.seal(..., extensions={"statcat": ...})`.
- `check_catalog(root, manifest, reg, profile) -> list[Finding]`: the
  `statcat` check, with sdmxlib 0.80's `Check` signature, run as an extension
  of `sdmxlib.edition.check_edition`.
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

- `synthetic_catalog(path, shape, *, seed=0, index=True) -> SyntheticCatalog`:
  builds a reproducible unsealed statcat edition over
  `sdmxlib.testing.synthetic_edition(..., seal=False)`. The record holds the
  open catalog (`cat`), its builder registry (`reg`) and `edition_dir`. The
  caller closes `cat`, then `reg`, and then seals.

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
- Seal an edition last, after `Catalog.build_index()` and `catalog_section`,
  with the catalog and its registry closed. The manifest records each file's
  digests, so a later write fails `check_edition`'s files check.
- `MapCondition`, `MapFilter` and `has_spatial` pass their language tag
  through `sdmxlib.normalize_tag`.
- `Expr` nodes are frozen attrs objects and run no SQL themselves.

## Side Effects

- `synthetic_catalog` writes an unsealed edition: sdmxlib's synthetic edition,
  the catalog, and the search index when `index=True`. It writes no manifest.
- These modules make no network calls.

## Verification

- Run: `just ci` (unit tier alone: `uv run pytest -m unit`).

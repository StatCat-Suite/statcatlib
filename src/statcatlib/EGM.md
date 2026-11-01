# statcatlib

## Overview

`statcatlib` is the package root. `__init__.py` re-exports the public API of
`model`, `sources` and `store`; `Catalog`, `Product`, `Edition` and
`NoLiveEditionError` from `catalog.py`; the errors `InventoryConflictError`,
`InventoryContentionError`, `InventoryRuleError` and `NonFastForwardError`
from `store/inventory.py`, and `WorkingCopyExistsError`,
`WorkingCopyClosedError`, `SealCheckError` and `JournalError` from
`store/working_copy.py`; `NotLoadedError` and `is_unloaded` from the store's
sentinel module; and part of `expr` (listed below). It exposes `checks`,
`query` and `urn` as submodules (`sc.checks`, `sc.query`, `sc.urn`). The
top-level modules hold the parts that sit across subpackages: the catalog of
products (`catalog.py`), seal checks (`checks.py`), the `statcat` section of
an sdmxlib edition manifest (`bundle.py`), composable query filters
(`query.py`), traversal expressions (`expr.py`), SDMX URN builders (`urn.py`)
and test catalogs (`testing.py`).

## Interface

### Subpackages

- `model/`: [`./model/EGM.md`](./model/EGM.md) — attrs domain model (datasets,
  agents, distributions, SKOS concepts); imports only `InternationalString`
  from sdmxlib.
- `store/`: [`./store/EGM.md`](./store/EGM.md) — the private `_Store`,
  `EditionView`, `WorkingCopy` and the seal, the Inventory writer,
  `QueryBuilder`, scopes, DDL, LanceDB search index.
- `sources/`: [`./sources/EGM.md`](./sources/EGM.md) — SDMX and CKAN ingest
  connectors.
- `cli/`: [`./cli/EGM.md`](./cli/EGM.md) — the `statcat` Typer command.

### `catalog.py`

- `Catalog`: a directory of products. `Catalog(...)` raises `TypeError`
  naming the two constructors and `testing.temporary_catalog`.
  - `Catalog.create(path, *, exist_ok=False)`: on a missing or empty
    directory, probes `os.link` and writes `catalog.json` (a UUIDv7
    `catalog_id`, `restore_epoch` 0) and nothing else; the first seal writes
    Inventory version 1. On an existing catalog it raises `FileExistsError`,
    or with `exist_ok=True` reopens it for writing and writes nothing.
  - `Catalog.open(path)`: read-only. Raises `FileNotFoundError` when *path*
    has no `catalog.json`. A catalog with no Inventory version has no
    products.
  - `writable`, `catalog_id`, `refresh()`, `products()` (the products with a
    live edition, sorted), `product(product_id) -> Product` (`ValueError`
    unless the id matches `^[A-Za-z0-9][A-Za-z0-9._-]*$`), `close()` and the
    context manager.
  - `search` and `facets` raise `NotImplementedError` naming
    `product(id).view()`. `__getattr__` raises `AttributeError` naming
    `cat.product(id).view().<name>` for every name in
    `store.store.VIEW_ATTRIBUTES`.
- `Product`: `product_id`; `view(*, embedding_model=None, query_prefix="")
  -> EditionView`; `editions() -> list[Edition]` (the live head, with the
  `previous` its manifest records; empty without one); `working_copy(*,
  lifecycle="revisable", resume=False, embedding_model=None,
  document_prefix="", query_prefix="") -> WorkingCopy`; `rollback()`, which
  raises `AttributeError` naming `CatalogAdmin.rollback` (Phase 2a S3).
  `Catalog.product` is the only constructor.
- `NoLiveEditionError` (`LookupError`): `view()` on a product with no live
  edition.
- `Edition` is re-exported from `store/working_copy.py`.

### `checks.py`

- `SealCheck`: the type `Callable[[EditionView], Iterable[Finding]]`, passed
  to `WorkingCopy.seal(checks=[...])`.
- `conf_status(attribute="CONF_STATUS", public=("F",)) -> SealCheck`: for
  each dataflow whose DSD has *attribute* and that has a `DataflowBinding`,
  reads `view.registry.obs(flow)` and yields one `Finding` (check name
  `CONF_STATUS_CHECK = "conf_status"`) per observation whose value is set
  and outside *public*.

### `bundle.py`

The module keeps its 0.11 name and describes editions. Imported from
`statcatlib.bundle`; nothing here is re-exported from `statcatlib`:

- `catalog_section(store: _Store) -> dict`: builds the `statcat` manifest
  section (format 3: `format`, `kind`, `statcatlib_version`, `datasets`,
  `search`) from a working copy's open store. `WorkingCopy.seal` passes it to
  `sdmxlib.edition.seal(..., extensions={"statcat": ...})`. It raises
  `ValueError` when `search/` holds files and no `index.json`.
- `check_catalog(root, manifest, reg, profile) -> list[Finding]`: the
  `statcat` check, with sdmxlib 0.80's `Check` signature, run as an extension
  of `sdmxlib.edition.check_edition`. It refuses a section format other than
  3, a `kind` outside `PRODUCT_KINDS`, and a `structure` edition whose
  manifest binds a dataflow.
- `overlay_affects_search(overlay, types=None)`: lists overlay entries that
  change search results.
- `STATCAT_SECTION = "statcat"`, `STATCAT_SECTION_FORMAT = 3`,
  `PRODUCT_KINDS = ("data", "structure")`. Phase 2a seals `data` only.

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

- `temporary_catalog()`: a context manager yielding a writable `Catalog`
  from `Catalog.create` in a temporary directory, deleted when the block
  ends. It replaces 0.11's `Catalog(":memory:")`.
- `synthetic_catalog(path, shape, *, product_id="synthetic", seed=0) ->
  Edition`: creates a catalog at *path* with one sealed, live edition. It
  builds `sdmxlib.testing.synthetic_edition(..., seal=False)` into the
  product's directory, resumes it as a working copy, stores one
  `AggregateDataset` per dataflow (`prepare_sdmx(wc.registry)`), and seals.
  The seal builds the search index when `statcatlib[search]` is installed.

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
- `WorkingCopy.seal` computes `catalog_section` after it rebuilds the search
  index and before it closes the store and registry; it then seals. The
  manifest records each file's digests, so a later write fails
  `check_edition`'s files check.
- `Catalog.open` writes nothing. No `Catalog` or `Product` method takes or
  returns a path inside the catalog.
- `Product.view` hashes the live edition's `manifest.json` and raises
  `IncompatibleCatalogError` when it differs from the `manifest_sha256` the
  Inventory records. Every view a `Catalog` returns shares one
  `sdmxlib.EditionEngine`, and `Catalog.close` closes them all.
- A seal check only reads: `seal` runs every check against `wc.view()`
  before writing anything, and any finding raises `SealCheckError`.
- `MapCondition`, `MapFilter` and `has_spatial` pass their language tag
  through `sdmxlib.normalize_tag`.
- `Expr` nodes are frozen attrs objects and run no SQL themselves.

## Side Effects

- `Catalog.create` creates the directory and `catalog.json`, logs the path
  at INFO, and writes and removes a `.statcat-link-probe.*` file and its hard
  link.
- `Product.view` attaches the edition to the catalog's engine
  (`LocalRegistry.edition`). `Product.working_copy` writes as
  `store/EGM.md` describes.
- `synthetic_catalog` writes a whole catalog: `catalog.json`, sdmxlib's
  synthetic edition, the catalog rows, the search index when lancedb is
  installed, the manifest and Inventory version 1. `temporary_catalog`
  deletes its directory on exit.
- These modules make no network calls.

## Verification

- Run: `just ci` (unit tier alone: `uv run pytest -m unit`).

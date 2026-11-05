# statcatlib

[![PyPI](https://img.shields.io/pypi/v/statcatlib)](https://pypi.org/project/statcatlib/)
[![Python](https://img.shields.io/pypi/pyversions/statcatlib)](https://pypi.org/project/statcatlib/)
[![License](https://img.shields.io/pypi/l/statcatlib)](LICENSE)
[![Docs](https://github.com/StatCat-Suite/statcatlib/actions/workflows/docs.yml/badge.svg)](https://statcat-suite.github.io/statcatlib/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> **Note:** This library has been written extensively with AI assistance
> (Claude Code). Users who are not comfortable with AI-generated code should
> take that into account before adopting it.

**statcatlib** is a Python library for building, managing, and querying
statistical metadata catalogs.

**Documentation:** <https://statcat-suite.github.io/statcatlib/>

Inspired by DCAT and the broader landscape of metadata standards (SDMX, DDI),
statcatlib provides a general-purpose engine for catalog storage, discovery, and
retrieval — designed to be embedded in ETL pipelines, data platforms, and
analytical tooling rather than used as a standalone application. It pairs
naturally with domain-specific libraries like
[sdmxlib](https://github.com/StatCat-Suite/sdmxlib) for standards-aware
workflows.

```python
import statcatlib as sc
```

## What it does

statcatlib materializes remote catalog and structural metadata into a local
catalog and exposes a fluent Python API for discovery queries — filtering by
publisher, theme, dimension, code, free text, or provenance lineage.

A catalog is a directory of products. Each product is a chain of immutable
editions, and each edition holds its datasets, its SDMX structures, its
observations and its search index in one DuckDB database and its DuckLake.

**The catalog is a materialized graph.** Ingest pulls the full structural model
into the edition's database, SQL JOINs are the graph traversal, and a query
makes no network calls.

## Installation

Requires Python 3.14+. Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv add statcatlib
# or: pip install statcatlib
```

Published on PyPI as `statcatlib` (formerly `pinaxlib`, imported as `pinax`).

statcatlib is pre-1.0: until 1.0, a minor release may break the API.

## Quick start

```python
import statcatlib as sc
import statcatlib.query as q
import sdmxlib as sl


def german_datasets(view: sc.EditionView) -> sc.SearchResult[sc.AggregateDataset]:
    return (
        view.query(sc.AggregateDataset)
        .filter(q.has_code("geo", "DE"))
        .with_facets("themes", "frequency")
        .execute()
    )


# Create the catalog and start the product's working copy, its one unsealed edition
with sc.Catalog.create("my_catalog") as cat:
    wc = cat.product("estat").working_copy()
    with sl.RestRegistry(sl.Provider.ESTAT) as reg:
        reg.get(sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP"), resolve=True)
        sc.ingest_sdmx(wc, reg.registry)

    # wc.view() reads the working copy, writes included
    print(german_datasets(wc.view()).total, "datasets found")

    # Seal it into an immutable edition, with its search index, and record it live
    wc.seal()

# Open the catalog read-only and query the product's live edition, with no network calls
with sc.Catalog.open("my_catalog") as cat:
    results = german_datasets(cat.product("estat").view())
    print(results.total, "datasets found")
    print(results.facets["themes"])
```

`Catalog.create(path)` creates a catalog, and `Catalog.create(path, exist_ok=True)`
reopens an existing one for writing. `cat.product(id).working_copy()` starts the
product's one unsealed edition, a `WorkingCopy`. Write to it with `put`, the
`ingest_*` functions and `wc.registry`, then `seal()` it, or `discard()` it.
`Catalog.open(path)` opens a catalog read-only, and `cat.product(id).view()`
returns an `EditionView` of the product's live edition, which every query below
runs on. A view stays on its edition. After `cat.refresh()`, call
`product.view()` again to read a newer one. `statcatlib.testing.temporary_catalog()`
creates a throwaway catalog for tests and scripts. The on-disk layout is
specified in [docs/catalog-format.md](docs/catalog-format.md).

## Five dataset kinds

statcatlib uses a discriminated union of dataset types aligned with DCAT-AP and
StatDCAT-AP:

```python
# Generic datasets (CKAN, open portals)
sc.OpenDataset(identifier="co2-2024", title=i18n("CO2 Emissions 2024"), ...)

# Statistical tables with SDMX structure (dimensions, codelists)
sc.AggregateDataset(identifier="ESTAT:UNE_RT_M(1.0)", ..., sdmx_dataflow_urn="urn:...")

# Survey microdata with variable-level metadata
sc.MicrodataDataset(identifier="lfs-2023", ..., variables=[...])

# Spatial datasets with bounding box and CRS
sc.GeospatialDataset(identifier="boundaries-2024", ..., crs="EPSG:4326")

# Articles, reports, and analytical publications
sc.PublicationDataset(identifier="pub-71-607-x", ..., doi="10.25318/...", authors=[...])
```

All five share the same write and query API. Use `sc.BaseDataset` as the bound
for generic code; `sc.Dataset` is a type alias for the full union.

## Source connectors

### SDMX (Eurostat, OECD, BIS, ABS, ...)

```python
import sdmxlib as sl
import statcatlib as sc

with sc.Catalog.create("my_catalog", exist_ok=True) as cat:
    with cat.product("estat").working_copy() as wc:
        with sl.RestRegistry(sl.Provider.ESTAT) as reg:
            for df_id in ["NAMA_10_GDP", "UNE_RT_M", "PRIC_HPI_IDX"]:
                reg.get(sl.Dataflow.urn_for(agency="ESTAT", id=df_id), resolve=True)
            sc.ingest_sdmx(wc, reg.registry)

            # Write observations to the edition's DuckLake and bind them to the dataflow
            sc.ingest_data(wc, reg, "ESTAT:NAMA_10_GDP(latest)", measure_dim="na_item")
        wc.seal()

# Read them back through the edition's sdmxlib registry
with sc.Catalog.open("my_catalog") as cat:
    view = cat.product("estat").view()
    ds = view.get(sc.AggregateDataset, "ESTAT:NAMA_10_GDP(1.0)")
    obs = view.registry.obs(sl.Dataflow.parse_urn(ds.sdmx_dataflow_urn))
    wide = obs.pivot(on=ds.measure_dim)   # a polars.DataFrame
```

### CKAN (open.canada.ca, data.gov, ...)

```python
with sc.Catalog.create("my_catalog", exist_ok=True) as cat:
    with cat.product("open-canada").working_copy() as wc:
        sc.ingest_ckan(wc, "https://open.canada.ca/data", organization="statcan-statcan", limit=500)
        wc.seal()
```

## Scope-based graph traversal

statcatlib exposes a lazy, scope-based API for navigating the catalog graph.
Navigation builds scope objects without executing SQL; only terminal methods
(`.collect()`, `.count()`) hit the database.

```python
import statcatlib as sc

cat = sc.Catalog.open("my_catalog")
view = cat.product("estat").view()

# Navigate themes — no SQL until .collect()
concepts = view.themes["statcan"].collect()          # ItemList[Concept]
concept = view.themes["statcan"]["13"].collect()     # Concept

# Cross-entity navigation — .datasets returns a lazy QueryBuilder
datasets = view.themes["statcan"]["13"].datasets.collect()

# Enrich with sub-traversal expressions (like Polars' pl.col())
view.themes["statcan"].enrich(
    n=sc.each("datasets").count(),
    has_data=sc.each("datasets").exists(),
).collect()

# Codelist navigation — sc.urn builds URN strings for you
codes = view.codelist(sc.urn.codelist("SDMX", "CL_GEO")).collect()    # ItemList[Code]
code = view.codelist(sc.urn.codelist("SDMX", "CL_GEO"))["ON"].collect()  # Code

# CodelistsScope — parallel to ThemesScope, supports enrich
# Enriched output includes labels resolved via sdmx.localized_text
view.codelists.lang("en").enrich(n=sc.each("datasets").count()).collect()
# → [{"urn": "...", "label": "Geography", "n": 5}, ...]

# Filter codelists by label text (SQL-level, case-insensitive)
view.codelists.filter(text_contains="geo").enrich(n=sc.each("datasets").count()).collect()

view.codelist(urn).label("en")   # quick name lookup

# Enriched per-code output includes code name labels
view.codelist(urn).lang("en").enrich(n=sc.each("datasets").count()).collect()
# → [{"code_id": "ON", "label": "Ontario", "n": 5}, ...]

# Batch label resolution — single SQL query for many codes
view.codelist(geo_urn).batch_labels(["CA", "US", "DE"])
# → {"CA": "Canada", "US": "United States", "DE": "Germany"}

# Across multiple codelists at once
view.codelists.batch_labels([(geo_urn, "CA"), (freq_urn, "A")], lang="en")
# → {(geo_urn, "CA"): "Canada", (freq_urn, "A"): "Annual"}

# Dimension traversal
dims = view.dimensions(ds).collect()              # ItemList[DimensionInfo]
codelist = view.dimensions(ds)["GEO"].codelist    # CodelistScope (lazy)
```

Scope classes: `ConceptSchemesScope`, `ConceptSchemeScope`, `ThemesScope`,
`SchemeScope`, `ConceptScope`, `CodelistsScope`, `CodelistScope`, `CodeScope`,
`DimensionsScope`, `DimensionScope`.

Expression types: `sc.each("edge")` creates a context-free sub-traversal
expression. Reusable across `.enrich()`, `.filter()`, and `.sort_by()`.

## Query API

### Structured filters

```python
import statcatlib.query as q

# Field filters (keyword arguments)
view.query(sc.AggregateDataset).filter(publisher="ESTAT", status="current").all()

# Composable filter objects
view.query(sc.AggregateDataset).filter(
    q.has_code("geo", "DE"),          # datasets with GEO=DE in their codelist
    q.has_dimensions(["geo", "freq"]), # datasets with both GEO and FREQ dimensions
).all()

# Hierarchy descent — any geography *under* Canada, strict (CA itself excluded).
# The applicable Hierarchy is resolved per dataflow; add include_self=True to
# match the root as well.
view.query(sc.AggregateDataset).filter(q.within("REF_AREA", "CA")).all()

# Distribution and service filters
view.query(sc.OpenDataset).filter(
    q.distribution(format="CSV"),
    q.has_service(endpoint_url="https://..."),
).all()
```

### Cross-entity queries

```python
# Agents that publish datasets with code GEO=CA
view.query(sc.Agent).filter(
    q.publishes(q.has_code("geo", "CA"), kind="aggregate")
).all()

# Data services serving aggregate datasets
view.query(sc.DataService).filter(
    q.serves(kind="aggregate")
).all()
```

### MAP column filters

```python
# Spatial coverage filter
view.query(sc.BaseDataset).filter(q.has_spatial("Canada")).all()

# Keyword filter
view.query(sc.BaseDataset).filter(q.has_keyword("employment")).all()

# Title/description search (case-insensitive ILIKE)
view.query(sc.BaseDataset).filter(q.title_contains("GDP")).all()
view.query(sc.BaseDataset).filter(q.description_contains("quarterly")).all()

# Sort by multilingual title
view.query(sc.BaseDataset).sort_by("title", lang="en").all()
view.query(sc.BaseDataset).sort_by("title", lang="en", desc=True).all()
```

### Selective relationship loading

By default, querying a list of datasets loads all relationships (~17 queries).
Use `.include()` to declare exactly which relationships to batch-load — the rest
are set to an `UNLOADED` sentinel:

```python
# Load publisher, themes and keywords only: 3 queries instead of ~17
results = (
    view.query(sc.BaseDataset)
    .filter(status="published")
    .include("publisher", "themes", "keywords")
    .sort_by("issued", desc=True)
    .limit(20)
    .all()
)

# Explicit full hydration shows the cost at the call site
results = view.query(sc.BaseDataset).filter(...).full().all()

# get() always loads everything — no include() needed
ds = view.get(sc.AggregateDataset, "ESTAT:NAMA_10_GDP(1.0)")
```

Unloaded fields raise `sc.NotLoadedError` on access. Use `sc.is_unloaded(value)`
to check before accessing:

```python
ds = view.query(sc.BaseDataset).include("publisher").first()
ds.publisher.name    # OK
ds.themes[0]         # raises NotLoadedError: 'themes' was not loaded

if not sc.is_unloaded(ds.themes):
    print(ds.themes)
```

Valid relationship names: `publisher`, `contact_point`, `frequency`, `licence`,
`themes`, `subject`, `dataset_type`, `keywords`, `spatial_coverage`,
`distributions`, `conforms_to`, `quality_annotations`, `provenance`,
`dimension_names`, `variables`, `feature_types`, `authors`.

### Lightweight projections

When you need just a few columns (e.g. autocomplete), projection modifiers
bypass full object reconstruction — a single SQL query:

```python
# Row projection — .select() + .rows() returns Row objects (dict subclass)
rows = (
    view.query(sc.BaseDataset)
    .filter(q.title_contains("GDP"))
    .sort_by("title", lang="en")
    .limit(3)
    .select("identifier", "title", lang="en")
    .rows()
)
# → [Row({"identifier": "GDP", "title": "GDP Growth"}), ...]
rows[0].identifier   # attribute-style access
rows[0]["title"]     # dict-style access — both work

# Flat value projection — .scalars() + .values() returns bare values
ids = view.query(sc.BaseDataset).filter(status="current").scalars("identifier").values()
# → ["EXR", "M1", "UNEMP", ...]

# Existence check — no object reconstruction
if view.query(sc.AggregateDataset).filter(q.has_code("geo", "DE")).exists():
    ...
```

### Full-text search and facets

```python
# BM25 search across titles, descriptions, keywords, themes, and dimensions
results = view.search("unemployment", limit=20)

# Combined search and filter
results = (
    view.query(sc.AggregateDataset)
    .filter(publisher="ESTAT")
    .search("labour force")
    .with_facets("themes", "frequency")
    .execute()
)
print(results.facets["themes"])   # {"Labour": 18, "Economy": 6, ...}

# Aggregation counts
counts = view.facets("publisher", "themes", "frequency", "status")
```

### Lineage and provenance (PROV-O)

```python
# Record derivation relationships between datasets, in a working copy
wc.add_lineage(
    "lfs-microdata",
    "14100287",
    "aggregated_from",
    activity_type="aggregation",
    activity_label="LFS monthly tabulation",
    confidence="asserted",
)

# Transitive upstream/downstream traversal on a view — returns QueryBuilder for chaining
ancestors = view.dataset("14100287").upstream(depth=5).collect()
dependents = view.dataset("CL_GEO").downstream(relationship="uses_classification").collect()

# Chain additional filters after traversal
current = view.dataset("14100287").upstream().filter(status="current").collect()

# Inspect lineage records
rows = view.dataset("14100287").lineage_records(role="target", relationship="aggregated_from")
```

## Architecture

Each edition holds three layers in one DuckDB database and its DuckLake:

```
Discovery layer  — statcat schema     (dataset, agent, concept_scheme, concept, distribution, lineage)
Structural layer — sdmxlib tables     (dataflows, dsd_components, codes, codelists)
Observation layer — DuckLake tables  (actual time-series data)
```

statcatlib owns the discovery layer. sdmxlib owns the structural layer. A working
copy writes both into its edition's database, and queries JOIN freely across
both. `ingest_data()` writes each dataflow's observations to a DuckLake table
through `wc.registry.write_observations` and registers an sdmxlib
`DataflowBinding`, through which `view.registry.obs(flow)` reads them.
[docs/catalog-format.md](docs/catalog-format.md) specifies the files of a
catalog and its editions.

## Development

Run once per clone — installs a pre-push hook that runs the full gate, since
hosted CI only builds the docs site:

```bash
just hooks-install
```

```bash
just ci                # full gate: index guard + lint + format + docstrings + prose + Any ratchet + pyrefly + unit and integration tests + stores tier + strict docs build
just test              # unit tests
just test-integration  # integration tests (no network)
just test-stores       # the blob Inventory writer against Azurite (needs a container runtime)
just test-live         # live tests against real SDMX endpoints
just lint              # ruff check
just typecheck         # pyrefly
just check-indexes     # only public PyPI is named anywhere in the repo
just docs              # local docs server
```

`uv.lock` is not committed: statcatlib is a library, so consumers resolve against
`pyproject.toml`, and a lockfile would record whichever index resolved it.
`just check-indexes` fails the build if a non-public package index is named in
any tracked file.

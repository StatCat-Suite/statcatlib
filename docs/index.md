# statcatlib

statcatlib is a Python library for building queryable open data catalog engines.
It is designed for statistical agencies, open data portals, and research
institutions working with DCAT-AP, StatDCAT-AP, CKAN, SDMX, and PROV-O
conventions.

## The discovery problem

Statistical agencies and open data portals publish datasets through a mix of
APIs: SDMX REST endpoints, CKAN portals, institutional REST services. Finding
the right dataset requires knowing which agency publishes it, what dimensions
it covers, what geographic codes are available, whether it is current, and
where it fits in the production chain. statcatlib materializes that metadata
from one or more sources into a local database and answers these questions
with queries against it.

## The three layers

statcatlib keeps discovery, structure and observation data in three separate
layers.

```mermaid
graph LR
    subgraph Discovery["Discovery layer — Catalog (DuckDB)"]
        DS[datasets]
        AG[agents]
        TH[themes]
        DI[distributions]
        LN[lineage]
    end

    subgraph Structure["Structural layer — sdmxlib tables (same DuckDB)"]
        DF[dataflows]
        DSD[dsd_components]
        CL[codes / codelists]
    end

    subgraph Observation["Observation layer — DuckLake tables"]
        OBS[observation data]
    end

    DS -->|sdmx_dataflow_urn| DF
    DF -->|dsd_urn| DSD
    DSD -->|codelist_urn| CL
    DF -->|DataflowBinding| OBS
    LN -->|source_id / target_id| DS

    style Discovery  stroke:#4a6cf7,stroke-width:2px
    style Structure  stroke:#ea4335,stroke-width:2px
    style Observation stroke:#34a853,stroke-width:2px
```

**Discovery layer** — the catalog star schema owned by statcatlib. Queries for
"datasets published by ECB with a GEO dimension" or "all datasets derived from
LFS microdata" run here.

**Structural layer** — SDMX artifacts (`dataflow`, `dsd_component`, `codelist`,
`code`) owned by sdmxlib, ingested into the same DuckDB connection in the `sdmx` schema. Structural
filters (`has_code`, `has_dimensions`) JOIN directly into these tables.

**Observation layer** — the actual numbers. `ingest_data()` writes a dataflow's
observations to a DuckLake table, one row per observation, and registers an
sdmxlib `DataflowBinding` for it. `store.registry.obs(flow)` reads them back as a
Polars frame, long or pivoted.

## The materialized graph

The design is conceptually equivalent to a local RDF graph store, but implemented
in SQL:

| RDF concept | statcatlib equivalent |
|---|---|
| URI | SDMX URN (e.g. `urn:sdmx:...Dataflow=ESTAT:NAMA_10_GDP(1.0)`) |
| Graph federation | Ingest: copy remote artifacts into local DuckDB |
| SPARQL joins | SQL JOINs across catalog and sdmxlib tables |
| Named graph | `kind` column on `dataset` (`open`, `aggregate`, `microdata`, `geospatial`, `publication`) |
| prov:wasDerivedFrom | `lineage.source_id → lineage.target_id` |

statcatlib materializes the full graph on ingest, so a query traverses it with
local SQL JOINs and calls no endpoint.

## Architecture

```mermaid
graph LR
    subgraph statcatlib
        direction TB
        MODEL["model/\nBaseDataset + 5 variants\nAgent, Distribution, …"]
        STORE["store/\nCatalog\nQueryBuilder, SearchResult"]
        QUERY["query.py\nhas_code(), publishes()\nserves(), distribution()"]
        SOURCES["sources/\ningest_sdmx()\ningest_ckan()"]
    end

    subgraph sdmxlib
        SLMODEL["model\nDataflow, DataStructure\nCodelist, Code"]
        SLSTORAGE["LocalRegistry\nsdmx schema\ningest()"]
        SLREST["RestRegistry\nfetch from SDMX REST"]
    end

    DUCKDB[("DuckDB\ncatalog folder")]
    POLARS["Polars DataFrame\nobs data"]

    SLREST -->|fetch| SLMODEL
    SOURCES -->|uses| SLMODEL
    SOURCES -->|calls| SLSTORAGE
    SOURCES -->|calls| STORE
    SLSTORAGE -->|writes| DUCKDB
    STORE -->|reads & writes| DUCKDB
    MODEL -->|attrs classes| STORE
    QUERY -->|ExistsFilter SQL| STORE
```

## Five dataset kinds

statcatlib uses a discriminated union aligned with DCAT-AP and StatDCAT-AP:

```mermaid
classDiagram
    class BaseDataset {
        +str identifier
        +InternationalString title
        +InternationalString description
        +Agent publisher
        +list~Concept~ themes
        +Frequency frequency
        +list~Distribution~ distributions
        +TemporalCoverage temporal_coverage
        +list~SpatialCoverage~ spatial_coverage
        +str status
        +str access_rights
    }

    class OpenDataset {
        +kind = "open"
    }

    class AggregateDataset {
        +kind = "aggregate"
        +list~InternationalString~ dimension_names
        +int num_series
        +str sdmx_dataflow_urn
        +str measure_dim
    }

    class MicrodataDataset {
        +kind = "microdata"
        +list~Variable~ variables
        +int sample_size
        +str universe
        +str collection_method
    }

    class GeospatialDataset {
        +kind = "geospatial"
        +str crs
        +str spatial_resolution
        +list~str~ feature_types
    }

    class PublicationDataset {
        +kind = "publication"
        +list~str~ authors
        +str doi
        +str isbn
        +str publication_type
    }

    BaseDataset <|-- OpenDataset
    BaseDataset <|-- AggregateDataset
    BaseDataset <|-- MicrodataDataset
    BaseDataset <|-- GeospatialDataset
    BaseDataset <|-- PublicationDataset
```

| Kind | DCAT-AP alignment | Typical source |
|---|---|---|
| `open` | `dcat:Dataset` | CKAN, open portals, manual curation |
| `aggregate` | `stat:StatDataset` | SDMX REST registries |
| `microdata` | `dcat:Dataset` + DDI | Survey microdata registries |
| `geospatial` | `dcat:Dataset` + GeoDCAT | Spatial data portals |
| `publication` | `dcat:Dataset` + DCMI | Publication and article portals |

## Database schema

```mermaid
erDiagram
    dataset {
        varchar identifier PK
        varchar kind
        map title
        map description
        varchar publisher_id FK
        varchar frequency_id FK
        varchar sdmx_dataflow_urn FK
    }
    agent {
        varchar id PK
        map name
    }
    concept_scheme {
        varchar uri PK
        map label
    }
    concept {
        varchar uri PK
        varchar in_scheme FK
        varchar notation
        map label
    }
    dataset_theme {
        varchar dataset_id FK
        varchar concept_uri FK
    }
    lineage {
        varchar id PK
        varchar source_id FK
        varchar target_id FK
        varchar relationship
        varchar confidence
        varchar activity_type
        varchar agent_id
    }
    distribution {
        varchar id PK
        varchar dataset_id FK
        varchar access_url
        varchar media_type
    }

    concept_scheme ||--o{ concept : in_scheme
    dataset ||--o{ dataset_theme : ""
    dataset_theme }o--|| concept : concept_uri
    dataset ||--o{ distribution : ""
    dataset }o--|| agent : publisher
    dataset ||--o{ lineage : source_id
    dataset ||--o{ lineage : target_id
```

!!! note "Schema ownership"
    `dataset`, `agent`, `concept_scheme`, `concept`, `distribution`, `lineage` and
    related bridge tables are in the `statcat` schema, owned by statcatlib. `dataflow`, `dsd_component`,
    `code`, `codelist` are in the `sdmx` schema, owned by sdmxlib. Both live in the same DuckDB
    database and are queried together via cross-schema SQL JOINs. `has_code()` and `has_dimensions()`
    JOIN through the `sdmx` schema via `sdmx_dataflow_urn`.

## Query surface

`Catalog` exposes four operations:

| Operation | Returns | Generic? | Use for |
|---|---|---|---|
| `get(cls, id)` | `T \| None` | Yes | Direct lookup by identifier |
| `query(cls)` | `QueryBuilder[T]` | Yes | Structured filtering and sorting |
| `search(text)` | `SearchResult[BaseDataset]` | No | Full-text BM25 across all datasets |
| `facets(*fields)` | `dict[str, dict[str, int]]` | No | Aggregation counts for UI facets |

`QueryBuilder` supports chaining:

```python
import statcatlib.query as q

results = (
    store.query(sc.AggregateDataset)
    .filter(
        q.has_code("geo", "DE"),
        q.has_dimensions(["geo", "freq"]),
    )
    .filter(publisher="ESTAT")
    .search("exchange rate")
    .include("publisher", "themes")   # load only these relationships
    .sort_by("modified", desc=True)
    .limit(20)
    .with_facets("themes", "frequency")
    .execute()   # → SearchResult[AggregateDataset]
)
```

Cross-entity queries reach through the dataset graph:

```python
# Agents that publish datasets with GEO=CA
store.query(sc.Agent).filter(
    q.publishes(q.has_code("geo", "CA"))
).all()

# Services serving aggregate datasets
store.query(sc.DataService).filter(q.serves(kind="aggregate")).all()
```

Lineage traversal follows PROV-O semantics:

```python
# All upstream sources, transitively
store.dataset("gdp-chained-dollars").upstream(depth=5)

# Impact analysis: datasets that depend on this classification
store.dataset("CL_GEO").downstream(relationship="uses_classification")
```

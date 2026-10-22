# statcatlib — Catalog Engine Design Specification

> **Historical.** This is the original design document, kept for the reasoning behind it. The code has moved on: names, tables and APIs here may no longer exist. CLAUDE.md and the docs site describe the current design.

## DCAT-Inspired Metadata Catalog on DuckDB + Parquet + LanceDB

*A clean-room redesign of the statcatlib catalog engine, built to interlock
with sdmxlib's `sdmx` schema and leverage DuckDB + Parquet + Polars
+ LanceDB as a modern persistence, compute, and discovery stack.*

---

## Table of Contents

1. [Principles](#1-principles)
2. [Architecture](#2-architecture)
3. [The Three-Layer Stack](#3-the-three-layer-stack)
4. [Two-Tier Operation](#4-two-tier-operation)
5. [Schema: `catalog` Namespace](#5-schema-catalog-namespace)
6. [Layer 1 — Dataset Spine](#6-layer-1--dataset-spine)
7. [Layer 2 — Dimension Tables](#7-layer-2--dimension-tables)
8. [Layer 3 — Bridge Tables](#8-layer-3--bridge-tables)
9. [Layer 4 — SDMX Structural Link](#9-layer-4--sdmx-structural-link)
10. [Layer 5 — Lineage & Provenance](#10-layer-5--lineage--provenance)
11. [Layer 6 — Search (LanceDB)](#11-layer-6--search-lancedb)
12. [The Parquet Data Layer](#12-the-parquet-data-layer)
13. [Cross-Schema Queries](#13-cross-schema-queries)
14. [The Scope API](#14-the-scope-api)
15. [Write Path](#15-write-path)
16. [Read Path](#16-read-path)
17. [Connection Contract](#17-connection-contract)
18. [Folder Layout](#18-folder-layout)
19. [Migration from Current Design](#19-migration-from-current-design)

---

## 1. Principles

1. **DCAT-AP is the conceptual model, not the schema.** The DCAT
   vocabulary defines what a catalog *means*. The DuckDB schema
   defines how it's stored. We map the concepts cleanly but don't
   replicate RDF triplisms in SQL.

2. **No blobs.** No `_document VARCHAR`. Every field is a typed
   column or a MAP. Object reconstruction comes from SQL rows,
   not JSON deserialization.

3. **Two schemas, one database.** statcatlib owns `catalog.*`, sdmxlib
   owns `sdmx.*`. Cross-schema joins connect statistical datasets
   to their structural metadata. Each component manages its own
   cursor and search_path.

4. **MAP columns for multilingual text.** All `InternationalString`
   fields use `MAP(VARCHAR, VARCHAR)` — no upfront language config,
   no sparse `title_en` / `title_fr` columns, any language
   queryable at any time.

5. **Parquet is the data layer.** Observation data, survey
   microdata, and tabular downloads live in Parquet files. DuckDB
   can query them directly via `read_parquet()`. The catalog
   stores metadata *about* these files, not the data itself.

6. **Polars is the compute layer.** Data access returns Polars
   LazyFrames. Schema generation, validation, and label mapping
   use the sdmxlib Polars bridge.

7. **DuckDB handles structure, LanceDB handles search.** DuckDB
   answers "which datasets match these structural criteria" —
   filters, facets, code membership, graph traversal. LanceDB
   answers "which datasets are most relevant to this query text" —
   BM25 keyword ranking, semantic vector search, hybrid fusion,
   reranking. Clean separation, no duplication. No DuckDB FTS
   extension.

8. **Search is optional (`statcatlib[search]`).** The catalog works
   fully for programmatic access without LanceDB — structured
   queries, scope navigation, faceted filtering, code-based
   discovery. Install `statcatlib[search]` to enable keyword search,
   semantic search, and hybrid discovery for interactive UIs.

9. **The scope API is the navigation surface.** Users navigate the
   catalog graph through lazy scope objects that compile to SQL
   on terminal calls. No eager loading, no N+1 queries.

10. **Denormalize for discovery, normalize for truth.** Bridge
    tables like `dataset_code` pre-index "which datasets use this
    code" for fast faceted search. The source of truth is the SDMX
    structural graph in the `sdmx` schema. Bridges are derived at
    ingest time and can be rebuilt.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                        User Code                                │
│  store = CatalogStore("my_catalog")                            │
│  store.query(AggregateDataset).filter(has_code("GEO","CA"))    │
│  store.dimensions(ds)["GEO"].codelist.collect()                │
│  store.search("GDP quarterly")          # requires statcatlib[search]│
└───────────────────────┬────────────────────────────────────────┘
                        │
┌───────────────────────▼────────────────────────────────────────┐
│                    CatalogStore                                  │
│  QueryBuilder → SQL compilation → SearchResult                  │
│  Scopes → lazy graph navigation → terminal SQL                  │
│  put() / ingest_sdmx() → write path                            │
└────────┬──────────────────────────┬────────────────────────────┘
         │                          │
┌────────▼────────┐         ┌───────▼────────┐
│  catalog schema │         │  sdmx schema   │
│  (DuckDB)       │         │  (DuckDB)      │
│                 │         │                │
│  dataset        │◄───────►│  artefact      │
│  distribution   │  cross  │  dataflow      │
│  agent          │  schema │  dsd_component │
│  theme          │  JOINs  │  code          │
│  dataset_code   │         │  artefact_ref  │
│  lineage        │         │  ...           │
└────────┬────────┘         └────────────────┘
         │
┌────────▼────────┐         ┌────────────────┐
│  Parquet files  │         │  LanceDB       │
│  (observations) │         │  (search)      │
│  DuckDB scans   │         │  statcatlib[search] │
│  Polars reads   │         │  optional      │
└─────────────────┘         └────────────────┘
```

---

## 3. The Three-Layer Stack

| Layer | Technology | What it stores | Access pattern |
|-------|-----------|----------------|----------------|
| **Metadata** | DuckDB (`catalog` + `sdmx` schemas) | Dataset records, agents, themes, structural metadata, relationships | SQL, scope API, faceted filtering |
| **Data** | Parquet files | Observations, survey records, tabular data | Polars LazyFrame, DuckDB `read_parquet()` |
| **Search** | LanceDB (optional) | BM25 inverted index + embedding vectors | Keyword search, semantic search, hybrid search with reranking |

All three layers are files in a single catalog folder. The folder
is portable, versionable, and shareable.

---

## 4. Two-Tier Operation

### Base mode (no extra dependencies)

The catalog works for structured access without LanceDB:

- `store.query(AggregateDataset).filter(publisher="StatCan").collect()`
- `store.themes["statcan"]["13"].datasets.collect()`
- `store.dimensions(ds)["GEO"].codelist.collect()`
- `store.query(...).filter(has_code("GEO", "CA")).with_facets("theme")`
- `store.code_map(dataset_id, "en")`
- `store.scan(dataset_id)` → Polars LazyFrame

All structural queries, scope navigation, faceted filtering, code-based
discovery, and data access work through DuckDB alone.

### Search mode (`statcatlib[search]`)

Install `pip install statcatlib[search]` to add LanceDB + an embedding model:

- `store.search("GDP quarterly")` → keyword search (BM25)
- `store.semantic_search("economic growth indicators")` → vector ANN
- `store.hybrid_search("GDP quarterly")` → BM25 + vector with reranking
- `store.query(...).search("GDP").filter(publisher="OECD").execute()`

The `CatalogStore` detects whether LanceDB is importable. Search methods
raise `RuntimeError("Install statcatlib[search] for text search")` when
called without it. The `QueryBuilder.search()` and `.hybrid_search()`
methods are available but fail gracefully.

### Dependency specification

```toml
[project]
dependencies = [
    "sdmxlib",
    "duckdb",
    "attrs",
    "polars",
]

[project.optional-dependencies]
search = [
    "lancedb>=0.15",
    "sentence-transformers",  # or any embedding provider
]
```

---

## 5. Schema: `catalog` Namespace

```sql
CREATE SCHEMA IF NOT EXISTS catalog;
SET search_path = 'catalog';

CREATE TABLE _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO _meta VALUES ('schema_version', '1.0');
```

All statcatlib tables live in the `catalog` schema. Cross-references to
sdmxlib use `sdmx.` qualified names. The `CatalogStore` creates a
cursor with `search_path = 'catalog'` so internal SQL stays clean.

---

## 6. Layer 1 — Dataset Spine

One table for all dataset variants. The `kind` column discriminates.
MAP columns for all multilingual text — no language configuration,
no generated columns needed (search is handled by LanceDB, not
DuckDB FTS).

```sql
CREATE TABLE dataset (
    identifier      TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,        -- 'open' | 'aggregate' | 'microdata'
                                          -- | 'geospatial' | 'publication'

    -- Multilingual metadata (MAP — no language config needed)
    title           MAP(VARCHAR, VARCHAR) NOT NULL,
    description     MAP(VARCHAR, VARCHAR) NOT NULL,
    version_notes   MAP(VARCHAR, VARCHAR),

    -- Foreign keys to dimension tables
    publisher_id    TEXT,                 -- FK → agent.id
    contact_id      TEXT,                 -- FK → contact.id
    frequency_id    TEXT,                 -- FK → frequency.id
    licence_id      TEXT,                 -- FK → licence.id

    -- Temporal coverage (inline — one per dataset)
    temporal_start  TIMESTAMP,
    temporal_end    TIMESTAMP,

    -- Core scalar fields
    status          TEXT,
    access_rights   TEXT,
    issued          TIMESTAMP,
    modified        TIMESTAMP,
    landing_page    TEXT,
    version         TEXT,
    language        TEXT[],              -- BCP-47 codes

    -- Aggregate (StatDCAT-AP) fields
    num_series          INTEGER,
    sdmx_dataflow_urn   TEXT,            -- FK → sdmx.dataflow.urn
    measure_dim         TEXT,

    -- Microdata fields
    sample_size     INTEGER,
    universe        TEXT,
    collection_method TEXT,
    anonymization   TEXT,
    survey_id       TEXT,

    -- Geospatial fields
    crs             TEXT,
    spatial_resolution TEXT,
    scale           TEXT,

    -- Publication fields
    doi             TEXT,
    isbn            TEXT,
    publication_type TEXT,
    pages           INTEGER,
    series_title    MAP(VARCHAR, VARCHAR),

    -- Extensibility
    extras          MAP(VARCHAR, VARCHAR)
);

CREATE INDEX idx_ds_kind ON dataset(kind);
CREATE INDEX idx_ds_publisher ON dataset(publisher_id);
CREATE INDEX idx_ds_sdmx ON dataset(sdmx_dataflow_urn);
CREATE INDEX idx_ds_issued ON dataset(issued);
```

**Why one table, not five?** The variants share 80% of their fields.
Splitting into five tables means five-way UNIONs for cross-variant
queries. One table with NULL variant fields is simpler and DuckDB
compresses NULLs to near-zero in columnar storage.

**Why no generated columns for FTS?** Search is handled entirely by
LanceDB. The DuckDB dataset table is optimised for relational queries
and faceted filtering, not text search. This keeps the schema clean —
no `_fts_title_en` columns, no FTS extension, no index rebuilds.

---

## 7. Layer 2 — Dimension Tables

Small lookup tables for entities referenced by datasets.

```sql
CREATE TABLE agent (
    id        TEXT PRIMARY KEY,
    name      MAP(VARCHAR, VARCHAR),
    type      TEXT,                    -- 'organization' | 'person'
    homepage  TEXT
);

CREATE TABLE contact (
    id      TEXT PRIMARY KEY,
    name    MAP(VARCHAR, VARCHAR),
    email   TEXT,
    phone   TEXT,
    url     TEXT
);

CREATE TABLE frequency (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR)
);

CREATE TABLE licence (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR),
    uri     TEXT
);

CREATE TABLE theme (
    id      TEXT NOT NULL,
    scheme  TEXT NOT NULL DEFAULT 'default',
    label   MAP(VARCHAR, VARCHAR),
    parent_id TEXT,                   -- for hierarchical classifications
    position  INT,
    PRIMARY KEY (id, scheme)
);

CREATE INDEX idx_theme_scheme ON theme(scheme);

CREATE TABLE standard (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR),
    uri     TEXT
);
```

---

## 8. Layer 3 — Bridge Tables

Many-to-many relationships and multi-valued fields.

```sql
-- Dataset ↔ Theme (many-to-many)
CREATE TABLE dataset_theme (
    dataset_id    TEXT NOT NULL,
    theme_id      TEXT NOT NULL,
    theme_scheme  TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY (dataset_id, theme_id, theme_scheme)
);
CREATE INDEX idx_dt_theme ON dataset_theme(theme_id, theme_scheme);

-- Dataset keywords (multilingual, multi-valued)
CREATE TABLE dataset_keyword (
    dataset_id  TEXT NOT NULL,
    keyword     MAP(VARCHAR, VARCHAR),
    ordinal     INT NOT NULL,
    PRIMARY KEY (dataset_id, ordinal)
);

-- Dataset spatial coverage (multi-valued)
CREATE TABLE dataset_spatial (
    dataset_id TEXT NOT NULL,
    label      MAP(VARCHAR, VARCHAR),
    uri        TEXT,
    bbox_west  DOUBLE,
    bbox_south DOUBLE,
    bbox_east  DOUBLE,
    bbox_north DOUBLE,
    ordinal    INT NOT NULL,
    PRIMARY KEY (dataset_id, ordinal)
);

-- Dataset ↔ Standard (conformsTo, many-to-many)
CREATE TABLE dataset_standard (
    dataset_id  TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    PRIMARY KEY (dataset_id, standard_id)
);

-- Distributions (one-to-many from dataset)
CREATE TABLE distribution (
    id           TEXT PRIMARY KEY,
    dataset_id   TEXT NOT NULL,
    title        MAP(VARCHAR, VARCHAR),
    description  MAP(VARCHAR, VARCHAR),
    access_url   TEXT,
    download_url TEXT,
    media_type   TEXT,
    format       TEXT,
    byte_size    BIGINT,
    issued       TIMESTAMP,
    modified     TIMESTAMP,
    licence_id   TEXT,
    service_id   TEXT,
    distribution_type TEXT
);
CREATE INDEX idx_dist_dataset ON distribution(dataset_id);
CREATE INDEX idx_dist_type ON distribution(media_type);

-- Data services
CREATE TABLE data_service (
    id           TEXT PRIMARY KEY,
    title        MAP(VARCHAR, VARCHAR),
    description  MAP(VARCHAR, VARCHAR),
    endpoint_url TEXT
);

-- Dataset ↔ DataService (serves relationship)
CREATE TABLE service_dataset (
    service_id  TEXT NOT NULL,
    dataset_id  TEXT NOT NULL,
    PRIMARY KEY (service_id, dataset_id)
);

-- Quality annotations (multi-valued per dataset)
CREATE TABLE quality_annotation (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    dimension  TEXT,
    label      MAP(VARCHAR, VARCHAR),
    body       MAP(VARCHAR, VARCHAR),
    PRIMARY KEY (dataset_id, ordinal)
);

-- Provenance statements (multi-valued per dataset)
CREATE TABLE provenance (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    label      MAP(VARCHAR, VARCHAR),
    uri        TEXT,
    PRIMARY KEY (dataset_id, ordinal)
);

-- Microdata variables (for MicrodataDataset)
CREATE TABLE variable (
    dataset_id        TEXT NOT NULL,
    variable_id       TEXT NOT NULL,
    label             MAP(VARCHAR, VARCHAR),
    description       MAP(VARCHAR, VARCHAR),
    value_type        TEXT,
    classification_id TEXT,
    universe          TEXT,
    question_text     TEXT,
    valid_range       TEXT,
    derivation        TEXT,
    PRIMARY KEY (dataset_id, variable_id)
);

-- Geospatial feature types (for GeospatialDataset)
CREATE TABLE feature_type (
    dataset_id TEXT NOT NULL,
    type_name  TEXT NOT NULL,
    PRIMARY KEY (dataset_id, type_name)
);

-- Publication authors (for PublicationDataset)
CREATE TABLE author (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    name       TEXT NOT NULL,
    PRIMARY KEY (dataset_id, ordinal)
);

-- Aggregate dimension names (for AggregateDataset)
CREATE TABLE dimension_name (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    label      MAP(VARCHAR, VARCHAR),
    PRIMARY KEY (dataset_id, ordinal)
);
```

---

## 9. Layer 4 — SDMX Structural Link

Denormalized indexes derived from the SDMX graph at ingest time.
They exist for query performance — the source of truth is `sdmx.*`.

```sql
-- Pre-indexed code membership for faceted search
-- "Find datasets that have code CA in dimension GEO"
CREATE TABLE dataset_code (
    dataset_id    TEXT NOT NULL,
    dimension_id  TEXT NOT NULL,
    codelist_urn  TEXT NOT NULL,
    code_id       TEXT NOT NULL,
    PRIMARY KEY (dataset_id, dimension_id, codelist_urn, code_id)
);
CREATE INDEX idx_dc_code ON dataset_code(code_id);
CREATE INDEX idx_dc_codelist ON dataset_code(codelist_urn);
CREATE INDEX idx_dc_dim_code ON dataset_code(dimension_id, code_id);

-- Pre-indexed codelist dependencies
-- "Find datasets that use codelist CL_GEO"
CREATE TABLE codelist_dependency (
    dataset_id   TEXT NOT NULL,
    codelist_urn TEXT NOT NULL,
    dimension_id TEXT NOT NULL,
    PRIMARY KEY (dataset_id, codelist_urn, dimension_id)
);
CREATE INDEX idx_cd_codelist ON codelist_dependency(codelist_urn);
```

### Why denormalize when sdmx.artefact_ref exists?

The faceted search hot path ("find datasets containing code CA in
GEO") is a single EXISTS probe against `dataset_code`:

```sql
SELECT d.identifier FROM catalog.dataset d
WHERE EXISTS (
    SELECT 1 FROM catalog.dataset_code dc
    WHERE dc.dataset_id = d.identifier
      AND dc.dimension_id = 'GEO'
      AND dc.code_id = 'CA'
)
```

The normalized 5-hop join through `sdmx.*` is authoritative but
slower. The bridges are rebuilt from the structural graph at ingest.

---

## 10. Layer 5 — Lineage & Provenance

```sql
CREATE TABLE lineage (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,      -- upstream dataset
    target_id      TEXT NOT NULL,      -- downstream dataset
    relationship   TEXT NOT NULL,      -- 'derived_from' | 'revision_of' |
                                       -- 'aggregation_of' | 'subset_of'
    activity_label TEXT,
    activity_type  TEXT,
    agent_id       TEXT,
    timestamp      TIMESTAMP,
    confidence     TEXT,               -- 'asserted' | 'inferred' | 'extracted'
    notes          TEXT,
    UNIQUE (source_id, target_id, relationship)
);

CREATE INDEX idx_lineage_source ON lineage(source_id);
CREATE INDEX idx_lineage_target ON lineage(target_id);
```

---

## 11. Layer 6 — Search (LanceDB)

### Architecture

All text search — keyword, semantic, and hybrid — lives in LanceDB.
DuckDB has no FTS extension, no generated columns for search, no
search indexes. This is a clean separation:

- **DuckDB:** relational queries, faceted filtering, graph traversal,
  cross-schema joins, code-based discovery
- **LanceDB:** text relevance ranking — BM25 keyword matching,
  vector similarity, hybrid fusion with reranking

The boundary is identifier lists. LanceDB returns ranked
`[identifier]` arrays; DuckDB fetches full records and applies
relational filters on top.

### LanceDB table schema

One LanceDB table holds all searchable text per dataset per language:

```python
# LanceDB table: "datasets"
# Columns:
#   id:         str          — dataset identifier (FK → catalog.dataset)
#   lang:       str          — language code ("en", "fr", ...)
#   text:       str          — concatenated searchable text
#   embedding:  vector[dim]  — embedding vector (when model is configured)
```

The `text` column is built at ingest time:
`"{title}. {description} {keywords}"` — all in the target language.

### Index creation

```python
import lancedb

db = lancedb.connect(catalog_path / "search")
table = db.create_table("datasets", data=records, mode="overwrite")

# BM25 full-text index (Tantivy-based, native in Lance)
table.create_fts_index("text")

# Vector index (IVF_PQ for larger catalogs, flat for small ones)
if len(records) > 1000:
    table.create_index("embedding", index_type="IVF_PQ")
```

### Search operations

**Keyword search (BM25):**

```python
results = table.search("GDP quarterly", query_type="fts").limit(50).to_list()
identifiers = [r["id"] for r in results]
```

**Semantic search (vector ANN):**

```python
query_vec = model.encode("economic growth indicators")
results = table.search(query_vec).limit(50).to_list()
identifiers = [r["id"] for r in results]
```

**Hybrid search (BM25 + vector with reranking):**

```python
results = (
    table.search("GDP quarterly", query_type="hybrid")
    .limit(50)
    .to_list()
)
identifiers = [r["id"] for r in results]
```

LanceDB's native hybrid search handles:
- Running both BM25 and vector queries
- Merging ranked lists via RRF (Reciprocal Rank Fusion)
- Optional reranking via linear combination, Cohere, or ColBERT

No manual RRF implementation in Python — LanceDB does this internally.

### Integration with QueryBuilder

```python
class QueryBuilder:
    def search(self, text: str) -> QueryBuilder:
        """Add keyword search ranking (requires statcatlib[search])."""
        self._search_text = text
        self._search_type = "fts"
        return self

    def hybrid_search(self, text: str) -> QueryBuilder:
        """Add hybrid BM25 + semantic ranking (requires statcatlib[search])."""
        self._search_text = text
        self._search_type = "hybrid"
        return self

    def execute(self) -> SearchResult:
        if self._search_text:
            # 1. LanceDB returns ranked identifiers
            ranked_ids = self._store._lance_search(
                self._search_text, self._search_type
            )
            # 2. DuckDB fetches + filters those identifiers
            return self._store._execute_filtered(
                ranked_ids, self._filters, self._facet_fields
            )
        else:
            # Pure relational query — no LanceDB involved
            return self._store._execute_relational(
                self._filters, self._sort, self._facet_fields
            )
```

The flow is: LanceDB ranks → DuckDB filters and fetches. Relational
filters (publisher, theme, code membership) apply as SQL WHERE
clauses on the DuckDB side, not as LanceDB metadata filters.

### Faceted search (DuckDB only)

Facets are always SQL aggregations — they don't involve LanceDB:

```sql
-- Theme facet
SELECT t.id, t.label['en'] AS label, COUNT(*) AS n
FROM catalog.dataset_theme dt
JOIN catalog.theme t ON t.id = dt.theme_id AND t.scheme = dt.theme_scheme
WHERE dt.dataset_id IN (:ranked_ids)   -- from LanceDB, or all if no search
GROUP BY t.id, t.label
ORDER BY n DESC;

-- Publisher facet
SELECT a.id, a.name['en'] AS name, COUNT(*) AS n
FROM catalog.dataset d
JOIN catalog.agent a ON a.id = d.publisher_id
WHERE d.identifier IN (:ranked_ids)
GROUP BY a.id, a.name
ORDER BY n DESC;

-- Code facet (crosses into sdmx schema)
SELECT dc.code_id, c.name['en'] AS label, COUNT(DISTINCT dc.dataset_id) AS n
FROM catalog.dataset_code dc
JOIN sdmx.code c ON c.code_id = dc.code_id
    AND c.codelist_urn = dc.codelist_urn
WHERE dc.dimension_id = :dim
  AND dc.dataset_id IN (:ranked_ids)
GROUP BY dc.code_id, c.name
ORDER BY n DESC;
```

When there's no active search, `:ranked_ids` is replaced with a
full scan (no WHERE filter on identifiers).

### Write-time indexing

When `put()` is called and LanceDB is available, the search index
is updated:

```python
def _index_for_search(self, dataset: BaseDataset) -> None:
    if self._lance is None:
        return  # search not installed — skip silently

    rows = []
    for lang in dataset.title.langs:
        title = dataset.title.get(lang, "")
        desc = dataset.description.get(lang, "")
        kws = " ".join(kw.get(lang, "") for kw in dataset.keywords)
        text = f"{title}. {desc} {kws}".strip()
        if not text:
            continue
        row = {"id": dataset.identifier, "lang": lang, "text": text}
        if self._embedding_model is not None:
            row["embedding"] = self._embedding_model.encode(text).tolist()
        rows.append(row)

    if rows:
        self._lance_table.add(rows)
```

For bulk ingestion, `bulk_load()` defers LanceDB indexing:

```python
@contextmanager
def bulk_load(self):
    self._lance_deferred = True
    try:
        yield self
    finally:
        self._lance_deferred = False
        self._rebuild_lance_indexes()
```

### Degraded mode (no LanceDB)

When `statcatlib[search]` is not installed:

```python
class CatalogStore:
    def __init__(self, ...):
        self._lance = None
        try:
            import lancedb
            self._lance = self._init_lance()
        except ImportError:
            pass  # search features unavailable

    def search(self, text, **kwargs):
        if self._lance is None:
            raise RuntimeError(
                "Text search requires statcatlib[search]. "
                "Install with: pip install statcatlib[search]"
            )
        return self._lance_search(text, "fts", **kwargs)
```

---

## 12. The Parquet Data Layer

Observation data lives in Parquet files under `<catalog>/data/`.
The catalog knows about them through `distribution` rows with
`media_type = 'application/parquet'`.

### DuckDB integration

```sql
-- Join catalog metadata with observation data
SELECT d.identifier, d.title['en'] AS title, obs.*
FROM catalog.dataset d
JOIN catalog.distribution dist
    ON dist.dataset_id = d.identifier
    AND dist.media_type = 'application/parquet'
CROSS JOIN read_parquet(dist.download_url) obs
WHERE d.identifier = :dataset_id
LIMIT 100;
```

### Polars integration

```python
def scan(self, dataset_id: str) -> pl.LazyFrame:
    """Return a Polars LazyFrame for the dataset's Parquet file."""
    dist = self._parquet_distribution(dataset_id)
    return pl.scan_parquet(dist.download_url)
```

---

## 13. Cross-Schema Queries

### Dimension facet builder

```sql
SELECT
    dc.component_id AS dimension,
    dc.position,
    con.name['en'] AS dimension_label,
    c.code_id,
    c.name['en'] AS code_label
FROM catalog.dataset d
JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
    AND dc.component_type = 'Dimension'
LEFT JOIN sdmx.concept con ON con.concept_urn = dc.concept_urn
LEFT JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn
WHERE d.identifier = :dataset_id
ORDER BY dc.position, c.position;
```

### Code map for a dataset

```sql
SELECT dc.component_id, c.code_id, c.name[:lang] AS label
FROM catalog.dataset d
JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn
WHERE d.identifier = :dataset_id
  AND dc.component_type = 'Dimension'
ORDER BY dc.position, c.position;
```

### Constraint-aware facets

```sql
SELECT rkv.value AS code_id, c.name['en'] AS label
FROM catalog.dataset d
JOIN sdmx.artefact_ref ar ON ar.target_urn = d.sdmx_dataflow_urn
    AND ar.role = 'attachment_dataflow'
JOIN sdmx.content_constraint cc ON cc.urn = ar.source_urn
JOIN sdmx.cube_region cr ON cr.constraint_urn = cc.urn
    AND cr.is_included = TRUE
JOIN sdmx.region_key_value rkv ON rkv.constraint_urn = cr.constraint_urn
    AND rkv.region_idx = cr.region_idx
    AND rkv.component_id = 'FREQ'
JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
    AND dc.component_id = 'FREQ'
JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn
    AND c.code_id = rkv.value
WHERE d.identifier = :dataset_id;
```

---

## 14. The Scope API

Lazy graph navigation objects that compile to SQL on terminal calls.

```python
# Theme navigation
store.themes["statcan"].collect()                    # all themes
store.themes["statcan"]["13"].datasets.collect()     # datasets in theme 13
store.themes["statcan"].enrich(
    n=each("datasets").count()
).collect()                                          # themes with counts

# Dimension navigation (crosses into sdmx schema)
store.dimensions(ds).collect()                       # all dimensions
store.dimensions(ds)["GEO"].codelist.collect()       # GEO's codelist
store.dimensions(ds)["GEO"].codelist["CA"].datasets  # datasets with CA

# Code-based filtering (DuckDB only — no search needed)
store.query(AggregateDataset).filter(
    has_code("GEO", "CA"),
    publisher="StatCan"
).with_facets("theme", "frequency").execute()

# Text search (requires statcatlib[search])
store.query(AggregateDataset).search("GDP quarterly").execute()
store.query(AggregateDataset).hybrid_search("economic output").execute()

# Combined: search + structural filters + facets
store.query(AggregateDataset) \
    .hybrid_search("GDP") \
    .filter(has_code("GEO", "CA"), publisher="OECD") \
    .with_facets("theme") \
    .execute()

# Lineage traversal
store.lineage(ds).upstream().collect()
store.lineage(ds).downstream().collect()
```

---

## 15. Write Path

```python
def put(self, dataset: BaseDataset, *, codes=None):
    # 1. Upsert dimension records (agent, contact, frequency, licence)
    agent_id = self._upsert_agent(dataset.publisher)
    ...

    # 2. Upsert dataset spine row (attrs → SQL, no JSON)
    self._upsert_dataset(dataset, agent_id, ...)

    # 3. Write bridge rows (themes, keywords, distributions, etc.)
    self._write_bridges(dataset)

    # 4. Write SDMX code index (if provided)
    if codes is not None:
        self._write_code_index(dataset.identifier, codes)

    # 5. Update LanceDB search index (if available, unless deferred)
    self._index_for_search(dataset)
```

### SDMX bulk ingestion

```python
def ingest_sdmx(store: CatalogStore, registry: Registry):
    # 1. Write structural metadata to sdmx schema
    local = store.local_registry()
    local.ingest(registry)

    # 2. Create catalog datasets from dataflows
    with store.bulk_load():
        for flow in registry.dataflows():
            dsd = flow.structure()
            ds, codes = dataset_from_dataflow(flow, dsd)
            store.put(ds, codes=codes)
    # LanceDB indexes rebuilt once on bulk_load() exit
```

---

## 16. Read Path

Object reconstruction from SQL rows — no JSON blobs:

```python
def get(self, cls, identifier):
    row = self._cursor.execute("""
        SELECT identifier, kind, title, description,
               publisher_id, contact_id, frequency_id, licence_id,
               temporal_start, temporal_end, ...
        FROM dataset WHERE identifier = ?
    """, [identifier]).fetchone()

    if row is None:
        return None

    publisher = self._read_agent(row.publisher_id)
    themes = self._read_themes(identifier)
    keywords = self._read_keywords(identifier)
    distributions = self._read_distributions(identifier)
    ...

    return cls(
        identifier=row.identifier,
        title=InternationalString.of(row.title),  # MAP → InternationalString
        description=InternationalString.of(row.description),
        publisher=publisher,
        themes=themes,
        distributions=distributions,
        ...
    )
```

---

## 17. Connection Contract

```python
class CatalogStore:
    def __init__(self, path_or_connection, ...):
        if isinstance(path_or_connection, duckdb.DuckDBPyConnection):
            self._conn = path_or_connection
            self._cursor = self._conn.cursor()
            self._owns_connection = False
        else:
            self._conn = duckdb.connect(str(path / "catalog.duckdb"))
            self._cursor = self._conn.cursor()
            self._owns_connection = True

        self._cursor.execute("CREATE SCHEMA IF NOT EXISTS catalog")
        self._cursor.execute("SET search_path = 'catalog'")
        self._create_tables()

        # Optional search layer
        self._lance = None
        try:
            import lancedb
            self._lance = self._init_lance()
        except ImportError:
            pass

    def local_registry(self) -> sdmxlib.LocalRegistry:
        """sdmxlib LocalRegistry sharing the database connection.

        The LocalRegistry creates its own cursor with
        search_path='sdmx'. Both operate on the same file.
        """
        return sdmxlib.LocalRegistry(self._conn)
```

---

## 18. Folder Layout

```
my_catalog/
├── catalog.duckdb              # DuckDB database file
│   ├── catalog schema          #   statcatlib tables
│   └── sdmx schema            #   sdmxlib tables
├── data/                       # Parquet observation files
│   ├── AGENCY/
│   │   └── FLOW_VERSION.parquet
│   └── ...
└── search/                     # LanceDB search index (optional)
    └── datasets.lance/         #   BM25 index + vectors
```

The entire folder is self-contained. Copy it to share a catalog.
Parquet files are referenced by relative paths in
`distribution.download_url`. The `search/` directory is only
present when `statcatlib[search]` is installed and search has been
built.

---

## 19. Migration from Current Design

| Current | New |
|---------|-----|
| All tables in `main` schema | statcatlib in `catalog`, sdmxlib in `sdmx` |
| `_document VARCHAR` on datasets | Dropped — full relational decomposition |
| `title_en`, `label_en` flat columns | `MAP(VARCHAR, VARCHAR)` columns |
| `cattrs` for JSON serialize/deserialize | Direct attrs → SQL decomposition |
| DuckDB FTS extension + generated columns | Dropped — search in LanceDB |
| Manual RRF hybrid scoring in Python | LanceDB native hybrid search |
| `_sdmx_schema.create_tables(self._conn)` | `sdmxlib.LocalRegistry(self._conn)` |
| Direct SQL against sdmxlib table names | `sdmx.` qualified cross-schema joins |
| `label_{lang}` on sdmxlib codes | `sdmx.code.name[:lang]` MAP lookup |
| FTS rebuild on every `put()` | LanceDB index updated incrementally |
| LanceDB optional, DuckDB FTS fallback | LanceDB optional, no FTS fallback |

### Migration strategy

Drop and rebuild. The catalog is a derived artifact — repopulate from
SDMX registries, CKAN endpoints, or other sources.

---

## Implementation Order

### Phase 1: Schema and namespace

`catalog` schema DDL, `_meta` version tracking. Connection contract
with cursor isolation. Verify cross-schema JOINs work with sdmxlib's
`sdmx` schema.

### Phase 2: Writers (no blobs)

Direct attrs → SQL for `put()`. MAP column handling for
`InternationalString`. Bridge table population. Code index writing.
No FTS rebuilds.

### Phase 3: Readers (no blobs)

SQL → attrs reconstruction for `get()`. QueryBuilder compilation
with MAP-aware filtering. Facet computation via DuckDB aggregations.

### Phase 4: Scope API

Theme, dimension, codelist, code scopes with cross-schema SQL.
Expression compilation for `.enrich()`.

### Phase 5: SDMX ingestion

`ingest_sdmx()` routing through `LocalRegistry`. Bridge table
derivation from SDMX structural graph. Code index population.

### Phase 6: Search (`statcatlib[search]`)

LanceDB integration: table creation, FTS indexing, vector indexing,
hybrid search. Graceful degradation when not installed. `bulk_load()`
deferred indexing.

### Phase 7: Parquet integration

`scan()` returning Polars LazyFrame. `ingest_data()` writing wide
Parquet. DuckDB `read_parquet()` for SQL-level data access.

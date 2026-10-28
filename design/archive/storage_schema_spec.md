# sdmxlib Storage Layer — Design Specification

> **Historical.** This is the original design document, kept for the reasoning behind it. The code has moved on: names, tables and APIs here may no longer exist. The current schema is sdmxlib's to document.

## Pure-SQL DuckDB Schema for the SDMX Information Model

*Replaces the current hybrid blob+projection storage layer with a fully
relational decomposition. No JSON blobs. Every field queryable. Designed
for the statcatlib catalog stack.*

---

## Table of Contents

1. [Principles](#1-principles)
2. [Architecture Overview](#2-architecture-overview)
3. [Schema Layers](#3-schema-layers)
4. [Layer 1 — The Catalogue Spine](#4-layer-1--the-catalogue-spine)
5. [Layer 2 — Multilingual Text](#5-layer-2--multilingual-text)
6. [Layer 3 — Annotations](#6-layer-3--annotations)
7. [Layer 4 — The Reference Graph](#7-layer-4--the-reference-graph)
8. [Layer 5 — Item Tables (Columnar)](#8-layer-5--item-tables-columnar)
9. [Layer 6 — Snowflake Arms (Structure Tables)](#9-layer-6--snowflake-arms-structure-tables)
10. [Layer 7 — Type Extension Tables](#10-layer-7--type-extension-tables)
11. [Layer 8 — Constraint Tables](#11-layer-8--constraint-tables)
12. [Layer 9 — Mapping Tables](#12-layer-9--mapping-tables)
13. [Hierarchy Strategy](#13-hierarchy-strategy)
14. [Key Queries](#14-key-queries)
15. [Write Path](#15-write-path)
16. [Read Path — Object Reconstruction](#16-read-path--object-reconstruction)
17. [Migration from Current Schema](#17-migration-from-current-schema)
18. [Schema Diagram](#18-schema-diagram)
19. [Design Decisions](#19-design-decisions-all-settled)

---

## 1. Principles

1. **No blobs.** Every piece of information is stored in typed columns.
   No `_document VARCHAR` column. No JSON escape hatches. The schema
   *is* the information model.

2. **The schema mirrors the IM, not the XML.** Table and column names
   come from the SDMX Information Model, not from XML element names.
   No `DimensionList` table, no `LocalRepresentation` column.

3. **Any language, any time.** Item tables use `MAP(VARCHAR, VARCHAR)`
   columns for multilingual labels — no upfront language config, no
   DDL migration, no joins on the hot path. Artefact-level text uses
   a `localized_text` table (low volume, indexed).

4. **Columnar where it matters.** Item tables (codes, concepts,
   hierarchy nodes) are flat and wide — optimised for DuckDB's
   columnar engine. These are where real analytical volume lives.

5. **Snowflake, not star.** Type-specific structure tables
   (`dsd_component`, `constraint_key_value`, `codelist_map_entry`)
   extend the central catalogue spine. They're the "arms" that carry
   detail too structured for the spine but too important to flatten.

6. **References are first-class data.** An `artefact_ref` table
   captures every Ref edge in the IM as a queryable row. Dependency
   traversal, impact analysis ("what uses this codelist?"), and
   `resolve()` are SQL operations — no object graph walking needed.

7. **Round-trip fidelity.** Domain object → SQL → domain object must
   be lossless for every field on every artefact type the domain model
   supports. If the domain model has it, the schema stores it.

8. **Single write path.** One function per artefact type writes to all
   relevant tables in a transaction. Derived tables (`artefact_ref`,
   hierarchy precomputation) are populated at write time, never
   maintained independently.

9. **statcatlib-ready.** The schema lives in a DuckDB `sdmx` schema
   (namespace) within a shared database file. Other statcatlib components
   own their own schemas (`catalog`, `quality`, etc.) in the same
   file. Cross-component queries use fully qualified names
   (`sdmx.artefact`, `catalog.dataset`). The `LocalRegistry` accepts
   external connections and creates a cursor with
   `search_path = 'sdmx'` so that its internal SQL stays clean.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    LocalRegistry                             │
│  get(urn) → domain object    add(artefact) → write tables   │
│  code_map(urn, lang) → dict  resolve_deps(urn) → set[urn]   │
└────────────────────┬────────────────────┬───────────────────┘
                     │                    │
              ┌──────▼──────┐     ┌───────▼──────┐
              │   readers   │     │   writers    │
              │  SQL → attrs│     │  attrs → SQL │
              └──────┬──────┘     └───────┬──────┘
                     │                    │
              ┌──────▼────────────────────▼──────┐
              │       DuckDB (statcat.duckdb)       │
              │                                   │
              │  ┌─ sdmx schema ───────────────┐  │
              │  │  artefact                   │  │
              │  │  localized_text             │  │
              │  │  artefact_ref               │  │
              │  │  code, concept, ...         │  │
              │  │  dsd_component, ...         │  │
              │  │  dataflow, provision, ...   │  │
              │  └─────────────────────────────┘  │
              │                                   │
              │  ┌─ catalog schema ────────────┐  │
              │  │  dataset, source, ...       │  │
              │  └─────────────────────────────┘  │
              └───────────────────────────────────┘
```

The `to_dict()`/`from_dict()` serialisation layer is **not** in the
storage read/write path. Writers decompose attrs objects directly into
SQL rows. Readers assemble attrs objects directly from SQL rows. JSON
serialisation remains available for wire formats (SDMX-JSON) and file
export, but the database never stores JSON.

---

## 3. Schema Layers

The schema has nine layers, each serving a distinct purpose:

| Layer | Tables | Purpose | Volume |
|-------|--------|---------|--------|
| 1. Spine | `artefact` | Universal catalogue; every maintainable artefact gets one row | Small (thousands) |
| 2. Text | `localized_text` | Artefact-level multilingual strings (names, descriptions) | Small (thousands) |
| 3. Annotations | `annotation`, `annotation_text` | Annotation metadata and multilingual text | Small–medium |
| 4. Graph | `artefact_ref` | Every Ref edge as a queryable row | Medium |
| 5. Items | `code`, `concept`, `category`, `agency`, `data_provider` | Flat item tables with MAP columns for labels; columnar analytics | **Large** (tens of thousands per codelist) |
| 6. Arms | `dsd_component`, `group_dimension` | DSD structural decomposition | Small |
| 7. Extensions | `dataflow`, `provision_agreement`, `categorisation` | Type-specific fields for leaf artefact types | Small |
| 8. Constraints | `content_constraint`, `cube_region`, `region_key_value` | Constraint decomposition | Medium–large |
| 9. Mappings | `structure_set`, `codelist_map`, `code_map_entry` | Registry-provided code mappings | Medium |

---

## 4. Layer 1 — The Catalogue Spine

Every maintainable artefact gets exactly one row here. This is the
universal index for the entire registry.

All tables live in the `sdmx` schema. The DDL below omits the schema
qualifier because `LocalRegistry` sets `search_path = 'sdmx'` on its
cursor. External consumers use `sdmx.artefact`, `sdmx.code`, etc.

```sql
-- Schema creation (once per database file)
CREATE SCHEMA IF NOT EXISTS sdmx;
SET search_path = 'sdmx';

-- Schema version tracking
CREATE TABLE _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO _meta VALUES ('schema_version', '2.0');

-- The catalogue spine
CREATE TABLE artefact (
    urn             TEXT PRIMARY KEY,
    artefact_type   TEXT NOT NULL,   -- 'Codelist', 'DataStructure', ...
    agency_id       TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    version         TEXT NOT NULL DEFAULT '1.0',
    is_final        BOOLEAN NOT NULL DEFAULT FALSE,
    is_external_ref BOOLEAN NOT NULL DEFAULT FALSE,
    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP,
    source_registry TEXT,            -- which endpoint this was fetched from
    fetched_at      TIMESTAMP
);

CREATE INDEX idx_artefact_type ON artefact(artefact_type);
CREATE INDEX idx_artefact_agency_id ON artefact(agency_id, resource_id);
CREATE UNIQUE INDEX idx_artefact_identity
    ON artefact(artefact_type, agency_id, resource_id, version);
```

**`artefact_type` values** correspond 1:1 with domain model classes:

`Codelist`, `ConceptScheme`, `DataStructure`, `Dataflow`,
`CategoryScheme`, `Categorisation`, `ProvisionAgreement`,
`AgencyScheme`, `DataProviderScheme`, `ContentConstraint`,
`StructureSet`, `Hierarchy`

**Why a single table, not per-type tables?**

Cross-cutting queries ("list everything from agency X", "count
artefacts by type", "does this URN exist?") are natural on one table.
Type-specific detail goes in extension tables joined by URN.

---

## 5. Layer 2 — Multilingual Text (Hybrid Design)

Multilingual text uses two strategies, matched to access patterns:

**MAP columns on item tables** (codes, concepts, categories, agencies,
hierarchy nodes). These are the high-volume tables where columnar
performance matters. A `MAP(VARCHAR, VARCHAR)` column stores labels
directly on the row — no join needed, no upfront language config,
and DuckDB vectorises `map['en']` lookups efficiently.

**`localized_text` table for artefact-level metadata** (names and
descriptions of DSDs, Dataflows, Codelists themselves, annotation
text, contact info fields). This is low-volume — a few thousand
artefacts, not hundreds of thousands of items. The join cost is
negligible because the table is small and lookups are indexed.

```sql
CREATE TABLE localized_text (
    owner_urn   TEXT NOT NULL,      -- URN of owning artefact
    field       TEXT NOT NULL,      -- 'name', 'description', 'department', ...
    lang        TEXT NOT NULL,
    text        TEXT NOT NULL,
    PRIMARY KEY (owner_urn, field, lang)
);

CREATE INDEX idx_lt_owner ON localized_text(owner_urn);
```

**Why not one table for everything?** The hot path is `code_map()` —
fetching `{code_id: label}` for a codelist. With `localized_text`
for codes, that's a hash-join against a 2-million-row table (across
a full registry). With a MAP column on the `code` table, it's a
single-table columnar scan:

```sql
-- MAP approach: single-table, no join, vectorised
SELECT code_id, name['en'] AS label
FROM code
WHERE codelist_urn = :urn
ORDER BY position;
```

This is measurably faster for the query that runs most often.

**Why not flat columns (`label_en`, `label_fr`)?** You must declare
languages at schema creation time. `code_map(urn, "de")` silently
returns empty if German wasn't configured. MAP stores whatever
languages exist — no config, no DDL migration, no sparse columns.

### Text search escape hatch

MAP columns aren't indexable by key. For full-registry label search
("find codes matching 'Canada'"), `name['en'] ILIKE '%Canada%'`
requires a sequential scan. DuckDB's columnar engine handles this
in ~50–100ms across 500k codes, which is acceptable for a search
box.

If this becomes a bottleneck for a specific language, add a generated
column:

```sql
ALTER TABLE code ADD COLUMN name_en TEXT
    GENERATED ALWAYS AS (name['en']);
CREATE INDEX idx_code_name_en ON code(name_en);
```

This gives indexed lookup for the one language you search most, while
the MAP retains all languages. Add generated columns later, only when
profiling shows the need — they're a non-destructive optimisation.

### What localized_text covers

Only artefact-level and annotation text:

- `artefact.urn` → `name`, `description`
- `annotation.id` (cast to text) → `text` (annotation body)
- `agency_contact.id` (cast to text) → `name`, `department`, `role`

The table is small (order of thousands of rows) and indexed on
`owner_urn`. Joins against it are point lookups, not scans.

---

## 6. Layer 3 — Annotations

Annotations are common across many artefact types and have their own
sub-structure (id, title, type, url, multilingual text).

```sql
CREATE TABLE annotation (
    id              BIGINT PRIMARY KEY,     -- auto-increment surrogate
    owner_urn       TEXT NOT NULL,           -- URN of annotated entity
    ordinal         INT NOT NULL,            -- preserve ordering
    annotation_id   TEXT,                    -- SDMX annotation @id (optional)
    title           TEXT,
    annotation_type TEXT,
    url             TEXT
);

CREATE SEQUENCE annotation_id_seq;

CREATE INDEX idx_ann_owner ON annotation(owner_urn);
CREATE INDEX idx_ann_type ON annotation(annotation_type);
CREATE INDEX idx_ann_type_owner ON annotation(annotation_type, owner_urn);
```

**Owner URN** is the URN of whatever is annotated — an artefact URN
for artefact-level annotations, or an item URN (code_urn, concept_urn,
etc.) for item-level annotations. Items carry their own annotations
via the MAP approach for labels, but annotation metadata (type, url,
structured text) needs its own table.

Annotation text (which is itself `InternationalString`) uses a
dedicated child table rather than `localized_text`, keeping the
annotation text model self-contained:

```sql
CREATE TABLE annotation_text (
    annotation_id   BIGINT NOT NULL,        -- FK → annotation.id
    lang            TEXT NOT NULL,
    text            TEXT NOT NULL,
    PRIMARY KEY (annotation_id, lang)
);
```

This avoids synthetic URNs for annotation ownership and keeps the
`localized_text` table scoped to artefact names/descriptions only.

Query: "find all artefacts with annotation type DEPRECATED":

```sql
SELECT a.owner_urn
FROM annotation a
WHERE a.annotation_type = 'DEPRECATED';
```

---

## 7. Layer 4 — The Reference Graph

Every `Ref[T]` in the domain model produces a row here. This is the
backbone for `resolve()`, impact analysis, and dependency traversal.

```sql
CREATE TABLE artefact_ref (
    source_urn   TEXT NOT NULL,     -- artefact containing the reference
    target_urn   TEXT NOT NULL,     -- artefact being referenced
    role         TEXT NOT NULL,     -- semantic role of the reference
    ordinal      INT,              -- for ordered reference collections
    PRIMARY KEY (source_urn, target_urn, role)
);

CREATE INDEX idx_ref_target ON artefact_ref(target_urn);
CREATE INDEX idx_ref_role ON artefact_ref(role);
```

**`role` values** (non-exhaustive, one per Ref field in the IM):

| Source type | Role | Target type |
|-------------|------|-------------|
| Dimension | `enumeration` | Codelist |
| Dimension | `concept_identity` | Concept (item URN) |
| Attribute | `enumeration` | Codelist |
| Attribute | `concept_identity` | Concept (item URN) |
| Measure | `concept_identity` | Concept (item URN) |
| Dataflow | `structure` | DataStructure |
| ProvisionAgreement | `dataflow` | Dataflow |
| ProvisionAgreement | `data_provider` | DataProvider (item URN) |
| ContentConstraint | `attachment_dataflow` | Dataflow |
| ContentConstraint | `attachment_dsd` | DataStructure |
| ContentConstraint | `attachment_provision` | ProvisionAgreement |
| Categorisation | `source` | Dataflow (or any artefact) |
| Categorisation | `target` | Category (item URN) |
| CodelistMap | `source_codelist` | Codelist |
| CodelistMap | `target_codelist` | Codelist |
| HierarchicalCode | `code` | Code (item URN) |

### Transitive dependency resolution

```sql
WITH RECURSIVE deps AS (
    SELECT target_urn, role, 1 AS depth
    FROM artefact_ref
    WHERE source_urn = :root_urn

    UNION ALL

    SELECT r.target_urn, r.role, d.depth + 1
    FROM artefact_ref r
    JOIN deps d ON r.source_urn = d.target_urn
    WHERE d.depth < 20
)
SELECT DISTINCT target_urn FROM deps;
```

This is `resolve()` as a query. No object graph walking.

### Reverse dependency (impact analysis)

"What uses this codelist?"

```sql
SELECT source_urn, role
FROM artefact_ref
WHERE target_urn = :codelist_urn;
```

This is impossible in the current schema without deserialising every
artefact.

---

## 8. Layer 5 — Item Tables (Columnar)

These are the large flat tables that justify DuckDB. Each item scheme
type gets its own table with `MAP(VARCHAR, VARCHAR)` columns for
multilingual labels — no join to `localized_text`, no upfront
language config.

Items within a scheme are identified by a **synthetic item URN** that
follows the SDMX URN spec:

`urn:sdmx:org.sdmx.infomodel.codelist.Code=SDMX:CL_FREQ(1.0).A`

This URN serves as the primary key and as the `owner_urn` for
annotations on that item.

### code

```sql
CREATE TABLE code (
    code_urn        TEXT PRIMARY KEY,   -- full item URN
    codelist_urn    TEXT NOT NULL,      -- FK → artefact.urn
    code_id         TEXT NOT NULL,
    parent_id       TEXT,              -- NULL for roots
    position        INT,               -- original ordering

    -- Multilingual labels (MAP — no join needed)
    name            MAP(VARCHAR, VARCHAR),  -- {'en': 'Annual', 'fr': 'Annuel'}
    description     MAP(VARCHAR, VARCHAR),

    -- Hierarchy precomputation (derived from parent_id at write time)
    depth           SMALLINT NOT NULL DEFAULT 0,
    ancestor_ids    TEXT[],            -- root-first materialized path

    UNIQUE (codelist_urn, code_id)
);

CREATE INDEX idx_code_codelist ON code(codelist_urn);
CREATE INDEX idx_code_parent ON code(codelist_urn, parent_id);
```

**code_map** — the hot path — is a single-table scan:

```sql
SELECT code_id, name[:lang] AS label
FROM code
WHERE codelist_urn = :urn
ORDER BY position;
```

**Hierarchy precomputation** — see [Section 13](#13-hierarchy-strategy).

### concept

```sql
CREATE TABLE concept (
    concept_urn     TEXT PRIMARY KEY,   -- full item URN
    scheme_urn      TEXT NOT NULL,      -- FK → artefact.urn
    concept_id      TEXT NOT NULL,
    parent_id       TEXT,
    position        INT,

    -- Multilingual labels
    name            MAP(VARCHAR, VARCHAR),
    description     MAP(VARCHAR, VARCHAR),

    -- Core representation (from Concept.core_representation)
    repr_kind       TEXT,              -- 'enumeration' | 'facet' | NULL
    repr_codelist_urn TEXT,            -- if enumeration
    repr_text_type  TEXT,              -- if facet
    repr_min_length INT,
    repr_max_length INT,

    UNIQUE (scheme_urn, concept_id)
);

CREATE INDEX idx_concept_scheme ON concept(scheme_urn);
```

### category

```sql
CREATE TABLE category (
    category_urn    TEXT PRIMARY KEY,
    scheme_urn      TEXT NOT NULL,
    category_id     TEXT NOT NULL,
    parent_id       TEXT,
    position        INT,

    name            MAP(VARCHAR, VARCHAR),
    description     MAP(VARCHAR, VARCHAR),

    depth           SMALLINT NOT NULL DEFAULT 0,
    ancestor_ids    TEXT[],

    UNIQUE (scheme_urn, category_id)
);

CREATE INDEX idx_category_scheme ON category(scheme_urn);
```

### agency

```sql
CREATE TABLE agency (
    agency_urn      TEXT PRIMARY KEY,
    scheme_urn      TEXT NOT NULL,
    agency_id       TEXT NOT NULL,
    parent_id       TEXT,             -- agencies can be nested

    name            MAP(VARCHAR, VARCHAR),
    description     MAP(VARCHAR, VARCHAR),

    UNIQUE (scheme_urn, agency_id)
);

CREATE INDEX idx_agency_scheme ON agency(scheme_urn);
```

Contact information for agencies:

```sql
CREATE TABLE agency_contact (
    id              BIGINT PRIMARY KEY,
    agency_urn      TEXT NOT NULL,
    ordinal         INT NOT NULL,

    -- Contact text fields as MAPs (low volume, same pattern as items)
    contact_name    MAP(VARCHAR, VARCHAR),
    department      MAP(VARCHAR, VARCHAR),
    role            MAP(VARCHAR, VARCHAR),

    -- Non-text fields stored directly
    phones          TEXT[],
    uris            TEXT[],
    emails          TEXT[]
);

CREATE SEQUENCE agency_contact_id_seq;
CREATE INDEX idx_contact_agency ON agency_contact(agency_urn);
```

### data_provider

```sql
CREATE TABLE data_provider (
    provider_urn    TEXT PRIMARY KEY,
    scheme_urn      TEXT NOT NULL,
    provider_id     TEXT NOT NULL,

    name            MAP(VARCHAR, VARCHAR),
    description     MAP(VARCHAR, VARCHAR),

    UNIQUE (scheme_urn, provider_id)
);

CREATE INDEX idx_provider_scheme ON data_provider(scheme_urn);
```

---

## 9. Layer 6 — Snowflake Arms (Structure Tables)

These capture the internal structure of complex artefacts. They're
the snowflake "arms" extending from the spine.

### dsd_component

```sql
CREATE TABLE dsd_component (
    dsd_urn          TEXT NOT NULL,    -- FK → artefact.urn
    component_id     TEXT NOT NULL,
    component_type   TEXT NOT NULL,    -- 'Dimension' | 'TimeDimension' |
                                       -- 'Attribute' | 'Measure'
    position         INT,             -- for dimensions

    -- Concept identity
    concept_urn      TEXT,            -- item URN → concept table

    -- Representation
    repr_kind        TEXT,            -- 'enumeration' | 'facet' | NULL
    codelist_urn     TEXT,            -- if enumeration → artefact.urn

    -- Facet fields (NULL when repr_kind ≠ 'facet')
    text_type        TEXT,
    min_length       INT,
    max_length       INT,
    min_value        DOUBLE,
    max_value        DOUBLE,
    decimals         SMALLINT,
    pattern          TEXT,
    is_sequence      BOOLEAN,
    start_value      DOUBLE,
    end_value        DOUBLE,

    -- Attribute-specific
    usage_status     TEXT,            -- 'Mandatory' | 'Conditional'
    attachment_level TEXT,            -- 'DataSet' | 'Series' |
                                      -- 'Observation' | 'Group'
    attachment_group TEXT,            -- group id if attachment_level = 'Group'

    -- Measure-specific
    measure_usage    TEXT,            -- v3 Measure.usage

    PRIMARY KEY (dsd_urn, component_id)
);

CREATE INDEX idx_dsd_comp_codelist ON dsd_component(codelist_urn);
```

### group_definition / group_dimension

```sql
CREATE TABLE dsd_group (
    dsd_urn     TEXT NOT NULL,
    group_id    TEXT NOT NULL,
    PRIMARY KEY (dsd_urn, group_id)
);

CREATE TABLE group_dimension (
    dsd_urn      TEXT NOT NULL,
    group_id     TEXT NOT NULL,
    dimension_id TEXT NOT NULL,
    PRIMARY KEY (dsd_urn, group_id, dimension_id)
);
```

---

## 10. Layer 7 — Type Extension Tables

Leaf artefact types that have fields beyond what the spine carries.

### dataflow

```sql
CREATE TABLE dataflow (
    urn         TEXT PRIMARY KEY,    -- FK → artefact.urn
    dsd_urn     TEXT NOT NULL        -- FK → artefact.urn
);
```

### provision_agreement

```sql
CREATE TABLE provision_agreement (
    urn              TEXT PRIMARY KEY,
    dataflow_urn     TEXT NOT NULL,
    data_provider_urn TEXT NOT NULL   -- item URN → data_provider
);
```

### categorisation

```sql
CREATE TABLE categorisation (
    urn             TEXT PRIMARY KEY,
    source_urn      TEXT NOT NULL,    -- what is categorised (usually Dataflow)
    target_urn      TEXT NOT NULL     -- category item URN
);
```

### hierarchy (SDMX 3.0 Hierarchy artefact)

```sql
CREATE TABLE hierarchy_meta (
    urn             TEXT PRIMARY KEY,
    -- hierarchy-level metadata beyond the spine
    -- (operator, leveled fields, etc. as needed)
    is_leveled      BOOLEAN DEFAULT FALSE
);

CREATE TABLE hierarchy_node (
    node_urn        TEXT PRIMARY KEY, -- synthetic item URN
    hierarchy_urn   TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    parent_node_id  TEXT,
    code_urn        TEXT,             -- optional Ref to a Code item

    name            MAP(VARCHAR, VARCHAR),
    description     MAP(VARCHAR, VARCHAR),

    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP,
    depth           SMALLINT NOT NULL DEFAULT 0,
    ancestor_ids    TEXT[],

    UNIQUE (hierarchy_urn, node_id)
);

CREATE INDEX idx_hnode_hierarchy ON hierarchy_node(hierarchy_urn);
CREATE INDEX idx_hnode_parent ON hierarchy_node(hierarchy_urn, parent_node_id);
```

---

## 11. Layer 8 — Constraint Tables

Constraints are analytically significant — cube regions can have
thousands of key-value pairs. They deserve columnar treatment.

```sql
CREATE TABLE content_constraint (
    urn              TEXT PRIMARY KEY,
    constraint_type  TEXT NOT NULL DEFAULT 'Allowed',  -- 'Allowed' | 'Actual'
    -- attachments stored in artefact_ref with roles:
    --   'attachment_dataflow', 'attachment_dsd', 'attachment_provision'
);

CREATE TABLE cube_region (
    constraint_urn  TEXT NOT NULL,
    region_idx      INT NOT NULL,
    is_included     BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (constraint_urn, region_idx)
);

CREATE TABLE region_key_value (
    constraint_urn  TEXT NOT NULL,
    region_idx      INT NOT NULL,
    component_id    TEXT NOT NULL,
    value           TEXT NOT NULL,
    PRIMARY KEY (constraint_urn, region_idx, component_id, value)
);

CREATE INDEX idx_rkv_component ON region_key_value(component_id);
```

This flat structure lets DuckDB answer "what values are allowed for
FREQ in this constraint?" with a single scan:

```sql
SELECT value
FROM region_key_value
WHERE constraint_urn = ? AND component_id = 'FREQ';
```

And "is this observation key allowed?" with:

```sql
SELECT cr.is_included
FROM cube_region cr
WHERE cr.constraint_urn = ?
  AND NOT EXISTS (
    SELECT 1 FROM region_key_value rkv
    WHERE rkv.constraint_urn = cr.constraint_urn
      AND rkv.region_idx = cr.region_idx
      AND rkv.component_id = ?  -- for each dimension
      AND rkv.value != ?         -- check against the key
  );
```

---

## 12. Layer 9 — Mapping Tables

StructureSets with CodelistMaps — registry-provided code-level
mappings.

```sql
CREATE TABLE structure_set (
    urn             TEXT PRIMARY KEY
    -- name/description via localized_text
);

CREATE TABLE codelist_map (
    id                  TEXT NOT NULL,
    structure_set_urn   TEXT NOT NULL,
    source_codelist_urn TEXT NOT NULL,
    target_codelist_urn TEXT NOT NULL,
    PRIMARY KEY (structure_set_urn, id)
);

CREATE TABLE code_map_entry (
    structure_set_urn TEXT NOT NULL,
    map_id            TEXT NOT NULL,
    source_code_id    TEXT NOT NULL,
    target_code_id    TEXT NOT NULL,
    PRIMARY KEY (structure_set_urn, map_id, source_code_id, target_code_id)
);

CREATE INDEX idx_cme_source ON code_map_entry(source_code_id);
```

Translation query — "map this source code to the target codelist":

```sql
SELECT target_code_id
FROM code_map_entry
WHERE structure_set_urn = ?
  AND map_id = ?
  AND source_code_id = ?;
```

---

## 13. Hierarchy Strategy

Codelists and category schemes can have deep hierarchies (NAICS has
5 levels, geographic classifications have more). Three representations
coexist, each serving a different query:

### 13.1 Adjacency list: `parent_id`

The source of truth. This is what the SDMX IM defines: each item
optionally points at its parent. Round-tripping is lossless.

### 13.2 Depth: `depth`

Precomputed at write time. `depth = 0` for roots, `1` for children
of roots, etc. Enables level-based filtering:

```sql
-- All NAICS subsectors (level 1)
SELECT code_id FROM code
WHERE codelist_urn = :naics AND depth = 1;
```

### 13.3 Materialised path: `ancestor_ids`

A `TEXT[]` array of ancestor code IDs, root-first. Precomputed at
write time.

```
code_id='111110'  ancestor_ids=['11', '111', '1111', '11111']
code_id='11111'   ancestor_ids=['11', '111', '1111']
code_id='1111'    ancestor_ids=['11', '111']
code_id='111'     ancestor_ids=['11']
code_id='11'      ancestor_ids=[]
```

Enables descendant queries without recursion:

```sql
-- Everything under Agriculture (sector 11)
SELECT code_id FROM code
WHERE codelist_urn = :naics
  AND list_contains(ancestor_ids, '11');
```

DuckDB scans `TEXT[]` columns efficiently and `list_contains` compiles
to vectorised operations. For a 20k-row codelist this is microseconds.

### 13.4 Rebuild invariant

`parent_id` is the truth. `depth` and `ancestor_ids` are derived.
The writer computes them in a single pass after inserting all codes
for a codelist. The invariants are:

- `depth = len(ancestor_ids)`
- `ancestor_ids[-1] = parent_id` (when `parent_id` is not NULL)
- `ancestor_ids[0]` is always a root code

### 13.5 Why not closure tables or nested sets?

**Closure tables** (`ancestor, descendant` pairs) are standard in
row-oriented databases. In DuckDB they'd work but add a separate
table and a JOIN. The `ancestor_ids` array-per-row approach is the
columnar equivalent — it denormalises the closure into the row, which
DuckDB's columnar storage handles naturally.

**Nested sets** are terrible for writes — any insertion rewrites half
the tree.

---

## 14. Key Queries

### 14.1 Catalogue browsing

```sql
-- All DSDs from StatCan
SELECT a.urn, a.resource_id, a.version, lt.text AS name_en
FROM artefact a
LEFT JOIN localized_text lt
  ON lt.owner_urn = a.urn
  AND lt.field = 'name'
  AND lt.lang = 'en'
WHERE a.artefact_type = 'DataStructure'
  AND a.agency_id = 'StatCan';
```

### 14.2 code_map (the hot path)

```sql
-- Single-table scan, no join, vectorised MAP lookup
SELECT code_id, name[:lang] AS label
FROM code
WHERE codelist_urn = :codelist_urn
ORDER BY position;
```

### 14.3 dsd_code_map (single query, no N+1)

```sql
SELECT dc.component_id, c.code_id, c.name[:lang] AS label
FROM dsd_component dc
JOIN code c ON dc.codelist_urn = c.codelist_urn
WHERE dc.dsd_urn = :dsd_urn
  AND dc.codelist_urn IS NOT NULL
ORDER BY dc.component_id, c.position;
```

### 14.4 DSD → its dimensions → their codelists

```sql
SELECT dc.component_id, dc.position,
       a.resource_id AS codelist_id,
       lt.text AS codelist_name
FROM dsd_component dc
JOIN artefact a ON dc.codelist_urn = a.urn
LEFT JOIN localized_text lt
  ON lt.owner_urn = a.urn
  AND lt.field = 'name'
  AND lt.lang = 'en'
WHERE dc.dsd_urn = :dsd_urn
  AND dc.component_type = 'Dimension'
ORDER BY dc.position;
```

### 14.5 Transitive resolve (dependency tree)

```sql
WITH RECURSIVE deps AS (
    SELECT target_urn, role, 1 AS depth
    FROM artefact_ref
    WHERE source_urn = :root_urn

    UNION ALL

    SELECT r.target_urn, r.role, d.depth + 1
    FROM artefact_ref r
    JOIN deps d ON r.source_urn = d.target_urn
    WHERE d.depth < 20
)
SELECT DISTINCT target_urn FROM deps;
```

### 14.6 Reverse dependency (impact analysis)

```sql
-- What DSDs use this codelist?
SELECT DISTINCT a.urn, a.resource_id
FROM artefact_ref r
JOIN artefact a ON r.source_urn = a.urn
WHERE r.target_urn = :codelist_urn
  AND r.role = 'enumeration';
```

### 14.7 Codelist coverage analysis (harmonisation)

```sql
-- Overlap between two codelists after normalisation
SELECT a.code_id
FROM code a
JOIN code b ON UPPER(a.code_id) = UPPER(b.code_id)
WHERE a.codelist_urn = :urn_a
  AND b.codelist_urn = :urn_b;
```

### 14.8 Constraint check

```sql
-- What values are allowed for FREQ in a constraint?
SELECT rkv.value
FROM region_key_value rkv
JOIN cube_region cr
  ON rkv.constraint_urn = cr.constraint_urn
  AND rkv.region_idx = cr.region_idx
WHERE rkv.constraint_urn = :constraint_urn
  AND rkv.component_id = 'FREQ'
  AND cr.is_included = TRUE;
```

### 14.9 Hierarchy descendant query

```sql
-- All codes under NAICS sector 11 (Agriculture)
SELECT code_id, depth
FROM code
WHERE codelist_urn = :naics_urn
  AND list_contains(ancestor_ids, '11')
ORDER BY depth, code_id;
```

---

## 15. Write Path

### 15.1 Discipline

All writes go through a single entry point per artefact type. The
function:

1. Begins a transaction
2. Deletes any existing rows for this URN across all tables
3. Inserts the spine row into `artefact`
4. Inserts `localized_text` rows for artefact name, description
5. Inserts `annotation` + `annotation_text` rows
6. Inserts type-specific rows (extension tables, item tables with MAP columns)
7. Derives and inserts `artefact_ref` rows
8. Commits

### 15.2 Ref extraction

At write time, every `Ref[T]` on the artefact is traversed and a
corresponding `artefact_ref` row is inserted. This is a mechanical
walk of the attrs fields — the same fields `_resolve_refs()` currently
walks, but writing instead of reading.

```python
def _extract_refs(artefact: Any, source_urn: str) -> list[tuple[str, str, str]]:
    """Walk attrs fields, extracting (source_urn, target_urn, role) tuples."""
    refs = []
    for field in attrs.fields(type(artefact)):
        value = getattr(artefact, field.name)
        if isinstance(value, Ref):
            refs.append((source_urn, str(value.urn), field.name))
        elif isinstance(value, ItemList):
            for item in value:
                if attrs.has(type(item)):
                    refs.extend(_extract_refs(item, source_urn))
    return refs
```

### 15.3 Hierarchy precomputation

After inserting all codes for a codelist, compute `depth` and
`ancestor_ids` in Python:

```python
def _compute_hierarchy(codes: list[Code]) -> dict[str, tuple[int, list[str]]]:
    """Compute depth and ancestor_ids for each code."""
    by_id = {c.id: c for c in codes}
    result: dict[str, tuple[int, list[str]]] = {}

    def walk(code_id: str) -> tuple[int, list[str]]:
        if code_id in result:
            return result[code_id]
        code = by_id[code_id]
        if code.parent_id is None:
            result[code_id] = (0, [])
        else:
            parent_depth, parent_ancestors = walk(code.parent_id)
            result[code_id] = (
                parent_depth + 1,
                parent_ancestors + [code.parent_id],
            )
        return result[code_id]

    for code_id in by_id:
        walk(code_id)
    return result
```

Then UPDATE the `code` table rows with the computed values, or
include them in the initial INSERT.

### 15.4 Bulk insert strategy

For item tables with potentially thousands of rows (codes, concepts,
hierarchy nodes, region_key_values), use DuckDB's bulk insert:

```python
# Build all rows in Python, then insert in one statement
conn.executemany(
    "INSERT INTO code (code_urn, codelist_urn, code_id, ...) VALUES (?, ?, ?, ...)",
    rows
)
```

Or for even better performance, use `conn.execute("INSERT INTO code SELECT * FROM df")`
with a Polars/PyArrow DataFrame for very large codelists.

---

## 16. Read Path — Object Reconstruction

### 16.1 Principle

Object reconstruction assembles an attrs domain object from SQL rows.
No JSON parsing. Each artefact type has a dedicated reader function
that issues the necessary queries and constructs the object.

### 16.2 LazyCodelist (unchanged concept, new backing)

`LazyCodelist` continues to exist as the default return type for
codelist gets. Its internal implementation changes from
`label_{lang}` column reads to `localized_text` JOINs, but the
external API is identical.

### 16.3 Ref resolution strategy

When reconstructing a DSD, its component `Ref[Codelist]` fields need
targets. Two approaches:

**Option A: Resolve on demand (current approach, adapted)**

`get(dsd_urn)` reconstructs the DSD from `dsd_component` rows,
creating `Ref.unresolved(codelist_urn)` for each. Then
`_resolve_refs()` walks the object and resolves each Ref from DuckDB.
For codelists, this creates `LazyCodelist` objects.

This is what the current code does. The difference: reconstruction
comes from relational tables, not JSON blobs.

**Option B: Pre-resolve from artefact_ref**

Use `artefact_ref` to discover all dependencies, batch-fetch their
metadata, and pre-build resolved Refs before assembling the DSD.

```sql
SELECT target_urn, role
FROM artefact_ref
WHERE source_urn = :dsd_urn;
```

This tells us exactly which codelists and concepts the DSD needs.
We can batch-construct `LazyCodelist` objects for all of them in one
pass, then wire them into the DSD during construction.

**Recommendation: Option A for now, Option B as optimisation.**

Option A is simpler to implement and mirrors the current code
structure. Option B is the performance path for DSDs with many
components, but it's an optimisation that can wait.

### 16.4 get_artefact dispatch

```python
def get_artefact(conn, urn: str, *, eager: bool = False) -> Any | None:
    row = conn.execute(
        "SELECT artefact_type FROM artefact WHERE urn = ?", [urn]
    ).fetchone()
    if row is None:
        return None

    artefact_type = row[0]
    match artefact_type:
        case "Codelist":
            return _read_codelist(conn, urn, eager=eager)
        case "DataStructure":
            return _read_data_structure(conn, urn)
        case "Dataflow":
            return _read_dataflow(conn, urn)
        case "ConceptScheme":
            return _read_concept_scheme(conn, urn)
        case "ContentConstraint":
            return _read_constraint(conn, urn)
        # ... etc
        case _:
            return _read_generic(conn, urn, artefact_type)
```

Each `_read_*` function issues the queries it needs, constructs the
attrs object, and returns it.

### 16.5 Example: _read_data_structure

```python
def _read_data_structure(conn, urn: str) -> DataStructure:
    # 1. Spine
    meta = _read_spine(conn, urn)  # agency, id, version, is_final, ...
    name = _read_istring(conn, urn, 'name')
    desc = _read_istring(conn, urn, 'description')
    anns = _read_annotations(conn, urn)

    # 2. Components
    comp_rows = conn.execute("""
        SELECT component_id, component_type, position, concept_urn,
               repr_kind, codelist_urn, text_type, min_length, max_length,
               min_value, max_value, decimals, pattern, is_sequence,
               start_value, end_value,
               usage_status, attachment_level, attachment_group,
               measure_usage
        FROM dsd_component
        WHERE dsd_urn = ?
        ORDER BY position
    """, [urn]).fetchall()

    dimensions, time_dim, attributes, measures = [], None, [], []
    for row in comp_rows:
        comp = _build_component(conn, urn, row)
        match comp:
            case Dimension(): dimensions.append(comp)
            case TimeDimension(): time_dim = comp
            case Attribute(): attributes.append(comp)
            case Measure(): measures.append(comp)

    # 3. Groups
    groups = _read_groups(conn, urn)

    return DataStructure(
        maintainer=meta.agency_id,
        id=meta.resource_id,
        version=meta.version,
        name=name,
        description=desc,
        is_final=meta.is_final,
        annotations=anns,
        dimensions=dimensions,
        time_dimension=time_dim,
        attributes=attributes,
        measures=measures,
        groups=groups,
    )
```

### 16.6 Shared helpers

```python
def _read_istring(conn, owner_urn, field) -> InternationalString:
    """Read an InternationalString from localized_text (artefact-level)."""
    rows = conn.execute("""
        SELECT lang, text FROM localized_text
        WHERE owner_urn = ? AND field = ?
    """, [owner_urn, field]).fetchall()
    return InternationalString.of({lang: text for lang, text in rows})


def _map_to_istring(m: dict[str, str] | None) -> InternationalString:
    """Convert a DuckDB MAP result to InternationalString (item-level)."""
    if not m:
        return InternationalString()
    return InternationalString.of(m)


def _read_annotations(conn, owner_urn) -> Annotations:
    """Read annotations and their text for any annotated entity."""
    rows = conn.execute("""
        SELECT a.id, a.annotation_id, a.title, a.annotation_type, a.url
        FROM annotation a
        WHERE a.owner_urn = ?
        ORDER BY a.ordinal
    """, [owner_urn]).fetchall()
    result = []
    for row in rows:
        text_rows = conn.execute("""
            SELECT lang, text FROM annotation_text
            WHERE annotation_id = ?
        """, [row[0]]).fetchall()
        text = InternationalString.of({l: t for l, t in text_rows})
        result.append(Annotation(
            id=row[1], title=row[2], type=row[3], url=row[4], text=text
        ))
    return Annotations(result)
```

---

## 17. Migration from Current Schema

### 17.1 What changes

| Current | New |
|---------|-----|
| `artefacts._document VARCHAR` (JSON blob) | Removed entirely |
| `codelists` table (written, never read) | Removed — spine + code table sufficient |
| `data_structures` table (written, never read) | Removed — spine + dsd_component sufficient |
| `dataflows` table | Kept, simplified (spine + extension) |
| `concept_schemes` table (written, never read) | Removed — spine + concept table sufficient |
| `name_{lang}`, `label_{lang}` columns | → `MAP(VARCHAR, VARCHAR)` on item tables; `localized_text` for artefacts |
| `_annotations VARCHAR` (JSON) | → `annotation` + `annotation_text` tables |
| `_meta.languages` configuration | Removed — `sdmx._meta` stores `schema_version` only |
| No `artefact_ref` table | Added |
| No hierarchy precomputation | `depth` + `ancestor_ids` added |
| `code_map_for_dsd` N+1 queries | Single JOIN query |
| `_resolve_refs` walks object graph | Reads `artefact_ref` table |
| ContentConstraint as JSON blob | Full relational decomposition |
| StructureSet as JSON blob | Full relational decomposition |

### 17.2 Migration strategy

Since this is a persistent cache (not a primary data store), the
migration is: **drop and rebuild.**

```python
def migrate(cursor: duckdb.DuckDBPyConnection) -> None:
    """Drop old schema, create new schema."""
    cursor.execute("DROP SCHEMA IF EXISTS sdmx CASCADE")
    create_schema(cursor)
```

Users repopulate from remote registries. No data migration needed.
The `LocalRegistry` detects schema version from `sdmx._meta` and
auto-migrates on open. If the `sdmx` schema doesn't exist or
`_meta.schema_version` is missing/outdated, it rebuilds.

---

## 18. Schema Diagram

```
                        ┌──────────────┐
                        │  artefact    │ ← Layer 1: Catalogue spine
                        │  (all types) │
                        └──────┬───────┘
                               │
          ┌────────────────────┼────────────────────────┐
          │                    │                         │
   ┌──────▼──────┐    ┌───────▼───────┐         ┌──────▼──────┐
   │ localized_  │    │ artefact_ref  │         │ annotation  │
   │ text        │    │ (graph edges) │         │             │
   │ (all text)  │    └───────────────┘         └─────────────┘
   └─────────────┘         Layer 4                 Layer 3
      Layer 2
                               │
          ┌─────────┬──────────┼──────────┬────────────┐
          │         │          │          │            │
   ┌──────▼──┐ ┌───▼───┐ ┌───▼────┐ ┌───▼───┐ ┌─────▼──────┐
   │  code   │ │concept│ │category│ │agency │ │data_provider│
   │ (flat,  │ │       │ │        │ │       │ │            │
   │ large)  │ └───────┘ └────────┘ └───────┘ └────────────┘
   └─────────┘
      Layer 5: Item tables (columnar)

          ┌──────────────────┬──────────────────────┐
          │                  │                      │
   ┌──────▼───────┐  ┌──────▼───────┐     ┌───────▼────────┐
   │dsd_component │  │ dsd_group /  │     │group_dimension │
   │              │  │              │     │                │
   └──────────────┘  └──────────────┘     └────────────────┘
      Layer 6: Snowflake arms

   ┌──────────┐ ┌──────────────────┐ ┌──────────────┐
   │ dataflow │ │provision_agreement│ │categorisation│
   └──────────┘ └──────────────────┘ └──────────────┘
      Layer 7: Type extensions

   ┌───────────────────┐ ┌─────────────┐ ┌─────────────────┐
   │content_constraint │ │ cube_region │ │region_key_value │
   └───────────────────┘ └─────────────┘ └─────────────────┘
      Layer 8: Constraints

   ┌───────────────┐ ┌──────────────┐ ┌────────────────┐
   │ structure_set │ │ codelist_map │ │code_map_entry  │
   └───────────────┘ └──────────────┘ └────────────────┘
      Layer 9: Mappings
```

---

## 19. Design Decisions (all settled)

### 19.1 Item URN as primary key (SETTLED)

**Decision:** Use full SDMX item URNs as primary keys on all item
tables. Generated at write time via `SdmxUrn.make()`.

Example: `urn:sdmx:org.sdmx.infomodel.codelist.Code=SDMX:CL_FREQ(1.0).A`

The shared prefix compresses to nearly nothing under DuckDB's
dictionary encoding. The URN serves as PK, annotation `owner_urn`,
and `artefact_ref` target — one identifier for all purposes.

### 19.2 localized_text performance (SETTLED)

**Decision:** The hybrid design resolves this. `localized_text` only
holds artefact-level text (a few thousand rows). The hot-path
`code_map()` uses MAP columns on the `code` table — single-table
scan, no join. Text search across all codes uses MAP with optional
generated columns for indexing specific languages when profiling
shows the need.

### 19.3 Namespace and connection contract (SETTLED)

**Decision:** Use DuckDB schemas. sdmxlib owns the `sdmx` schema.

**Connection contract for statcatlib integration:**

The DuckDB file is owned by statcatlib, not sdmxlib. sdmxlib is a tenant.
When `LocalRegistry` receives a shared connection, it:

1. Creates the `sdmx` schema if it doesn't exist
2. Creates a **cursor** from the connection (not modifying the
   connection's own search_path)
3. Sets `search_path = 'sdmx'` on the cursor
4. Uses the cursor for all internal operations

```python
class LocalRegistry:
    def __init__(self, path_or_connection, ...):
        if isinstance(path_or_connection, duckdb.DuckDBPyConnection):
            self._conn = path_or_connection
            self._cursor = self._conn.cursor()
            self._owns_connection = False
        else:
            self._conn = duckdb.connect(str(path_or_connection))
            self._cursor = self._conn.cursor()
            self._owns_connection = True

        self._cursor.execute("CREATE SCHEMA IF NOT EXISTS sdmx")
        self._cursor.execute("SET search_path = 'sdmx'")
        # All subsequent SQL on self._cursor resolves to sdmx.*
```

This means:

- sdmxlib's internal SQL stays clean — `SELECT * FROM artefact`,
  not `SELECT * FROM sdmx.artefact`
- The shared connection's default search_path is untouched — other
  components are unaffected
- Other statcatlib components read sdmxlib data via `sdmx.artefact`,
  `sdmx.code`, etc.
- `DROP SCHEMA sdmx CASCADE` cleanly removes all sdmxlib tables
- `SHOW TABLES IN sdmx` lists sdmxlib's tables without noise
  from other components

**Cross-component query example:**

```sql
-- Catalog component enriching its datasets with SDMX metadata
SELECT
    cat.dataset_id,
    cat.last_updated,
    sa.resource_id AS dataflow_id,
    lt.text AS dataflow_name
FROM catalog.dataset cat
JOIN sdmx.artefact sa ON cat.dataflow_urn = sa.urn
LEFT JOIN sdmx.localized_text lt
  ON lt.owner_urn = sa.urn
  AND lt.owner_kind = 'artefact'
  AND lt.field = 'name'
  AND lt.lang = 'en';
```

### 19.4 Observation data boundary (SETTLED)

Observation data stays in Parquet — that's settled. The boundary is:
the DuckDB schema stores structural metadata only. The `Dataset`
domain object (Polars LazyFrame + DSD) is never persisted in DuckDB.

The connection point is the DSD URN: observation Parquet files are
associated with a DSD URN that resolves against the LocalRegistry.

### 19.5 Versioning and schema evolution (SETTLED)

The schema will evolve as the domain model grows (new artefact types,
new fields). Strategy:

- `sdmx._meta` stores `schema_version` (e.g. `'2.0'`)
- On open, `LocalRegistry` checks `sdmx._meta.schema_version`
  against the expected version compiled into the library
- If mismatch: `DROP SCHEMA sdmx CASCADE` and rebuild — this is a
  cache, not primary storage
- Future: incremental migrations for non-destructive changes
  (adding columns, new tables)

---

## Implementation Order

### Phase 1: Schema definition

`storage/schema.py` — `CREATE SCHEMA IF NOT EXISTS sdmx`, DDL for all
tables, indexes, sequences within the `sdmx` schema. `_meta` table
with `schema_version`. Single `create_schema(conn)` entry point that
creates a cursor, sets `search_path = 'sdmx'`, and returns the cursor.

### Phase 2: Writers

`storage/writers.py` — one `put_*` function per artefact type.
`_extract_refs()` for artefact_ref population. Hierarchy precomputation.
Bulk insert for items.

### Phase 3: Readers

`storage/readers.py` — one `_read_*` function per artefact type.
`_read_istring()`, `_read_annotations()` shared helpers. `code_map()`
and `dsd_code_map()` as optimised SQL queries.

### Phase 4: LazyCodelist adaptation

`storage/lazy.py` — adapt `LazyItemList` and `LazyCodelist` to use
`localized_text` JOINs instead of flat language columns.

### Phase 5: LocalRegistry adaptation

`local/_registry.py` — remove `_document` dependency from `get()`,
`to_registry()`, `_resolve_refs()`. Use new readers throughout.

### Phase 6: Drop old schema

Remove old `main.*` tables from legacy installations. Add schema
version detection to `LocalRegistry.__init__` — if old tables exist
in `main` without a `sdmx` schema, auto-migrate.

### Phase 7: Performance

Profile `code_map()` with real data. Add covering indexes or summary
tables as needed. Batch `ingest_registry()` into a single transaction.

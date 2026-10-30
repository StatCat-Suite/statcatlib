"""DuckDB star schema for the Catalog — ``statcat`` namespace.

Single entry point: ``create_schema(conn)`` creates all catalog tables and
returns a cursor with ``search_path = 'statcat'``.  Safe to call on an
existing database — every DDL statement uses ``IF NOT EXISTS``.

Multilingual columns
--------------------
All multilingual text fields use ``MAP(VARCHAR, VARCHAR)`` — no upfront
language configuration needed.  Any language is queryable at any time
via ``element_at(title, 'en')`` or ``title['en']``.

Table groups
------------
Spine       dataset
Dimension   agent, contact, frequency, licence, standard,
            concept_scheme, concept
Bridge      dataset_theme, dataset_subject, dataset_keyword,
            dataset_spatial, dataset_standard, distribution,
            data_service, service_dataset, quality_annotation,
            provenance, variable, feature_type, author,
            dimension_name, concept_relation,
            concept_scheme_top_concept, catalog_theme_taxonomy
Lineage     lineage
Languages   language_alternate
Meta        _meta

SKOS identity model
-------------------
``concept.uri`` is the storage PK used for external reference resolution.
The domain ``Concept`` type identifies concepts by ``notation`` within their
scheme (Pythonic, mirrors sdmxlib Code/Codelist pattern). URI is reconstructed
from ``{scheme.uri}/{concept.notation}`` at serialization time.

``concept_relation.relation`` is VARCHAR (not an enum) so future XKOS
relation types (``xkos:generalizes``, ``xkos:isPartOf``, etc.) can be
added without schema changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb

#: The ``statcat`` schema this build writes and reads, recorded in ``statcat._meta``.
#: `Catalog.open` refuses a catalog stamped with another (GH-39), so bumping it means
#: rebuilding every catalog.
SCHEMA_VERSION = "2.2"

# ── DDL ────────────────────────────────────────────────────────────────

_SCHEMA_DDL = """\
CREATE SCHEMA IF NOT EXISTS statcat;

-- ── Meta ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── Layer 1: Dataset spine ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dataset (
    identifier      TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,

    -- Multilingual metadata
    title           MAP(VARCHAR, VARCHAR) NOT NULL,
    description     MAP(VARCHAR, VARCHAR) NOT NULL,
    version_notes   MAP(VARCHAR, VARCHAR),

    -- Foreign keys to dimension tables
    publisher_id    TEXT,
    creator_id      TEXT,
    contact_id      TEXT,
    frequency_id    TEXT,
    licence_id      TEXT,

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
    language        TEXT[],

    -- Aggregate (StatDCAT-AP) fields
    num_series          INTEGER,
    sdmx_dataflow_urn   TEXT,
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

    -- SKOS concept citations (dcterms:type, URI ref to a Concept)
    dataset_type    TEXT,

    -- Extensibility
    extras          MAP(VARCHAR, VARCHAR)
);

CREATE INDEX IF NOT EXISTS idx_ds_kind ON dataset(kind);
CREATE INDEX IF NOT EXISTS idx_ds_publisher ON dataset(publisher_id);
CREATE INDEX IF NOT EXISTS idx_ds_creator ON dataset(creator_id);
CREATE INDEX IF NOT EXISTS idx_ds_sdmx ON dataset(sdmx_dataflow_urn);
CREATE INDEX IF NOT EXISTS idx_ds_issued ON dataset(issued);

-- ── Layer 2: Dimension tables ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent (
    id        TEXT PRIMARY KEY,
    name      MAP(VARCHAR, VARCHAR),
    type      TEXT,
    homepage  TEXT
);

CREATE TABLE IF NOT EXISTS contact (
    id      TEXT PRIMARY KEY,
    name    MAP(VARCHAR, VARCHAR),
    email   TEXT,
    phone   TEXT,
    url     TEXT
);

CREATE TABLE IF NOT EXISTS frequency (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR)
);

CREATE TABLE IF NOT EXISTS licence (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR),
    uri     TEXT
);

CREATE TABLE IF NOT EXISTS standard (
    id      TEXT PRIMARY KEY,
    label   MAP(VARCHAR, VARCHAR),
    uri     TEXT
);

-- ── SKOS concept schemes and concepts ────────────────────────────────
-- concept.uri is the storage PK for external resolution.
-- Domain Concept uses notation as identity within its scheme.
CREATE TABLE IF NOT EXISTS concept_scheme (
    uri        VARCHAR PRIMARY KEY,
    label      MAP(VARCHAR, VARCHAR) NOT NULL,
    definition MAP(VARCHAR, VARCHAR)
);

CREATE TABLE IF NOT EXISTS concept (
    uri        VARCHAR PRIMARY KEY,
    in_scheme  VARCHAR NOT NULL,
    notation   VARCHAR NOT NULL,
    label      MAP(VARCHAR, VARCHAR) NOT NULL,
    alt_label  MAP(VARCHAR, VARCHAR),
    definition MAP(VARCHAR, VARCHAR)
);

CREATE INDEX IF NOT EXISTS idx_concept_in_scheme ON concept(in_scheme);
CREATE UNIQUE INDEX IF NOT EXISTS idx_concept_notation ON concept(in_scheme, notation);

-- Concept hierarchy and associative relations.
-- relation is VARCHAR (not enum) to support future XKOS types without schema changes.
CREATE TABLE IF NOT EXISTS concept_relation (
    source_uri VARCHAR NOT NULL,
    target_uri VARCHAR NOT NULL,
    relation   VARCHAR NOT NULL,
    PRIMARY KEY (source_uri, target_uri, relation)
);

-- Top-level concepts for a scheme (stored as notations, not URIs).
CREATE TABLE IF NOT EXISTS concept_scheme_top_concept (
    scheme_uri VARCHAR NOT NULL,
    notation   VARCHAR NOT NULL,
    PRIMARY KEY (scheme_uri, notation)
);

-- ── Layer 3: Bridge tables ────────────────────────────────────────────
-- Dataset → theme citations (concept URI only — labels resolved via LEFT JOIN)
CREATE TABLE IF NOT EXISTS dataset_theme (
    dataset_id  TEXT NOT NULL,
    concept_uri TEXT NOT NULL,
    PRIMARY KEY (dataset_id, concept_uri)
);

CREATE INDEX IF NOT EXISTS idx_dt_concept ON dataset_theme(concept_uri);

-- Dataset → subject citations
CREATE TABLE IF NOT EXISTS dataset_subject (
    dataset_id  TEXT NOT NULL,
    concept_uri TEXT NOT NULL,
    PRIMARY KEY (dataset_id, concept_uri)
);

CREATE TABLE IF NOT EXISTS dataset_keyword (
    dataset_id  TEXT NOT NULL,
    keyword     MAP(VARCHAR, VARCHAR),
    ordinal     INT NOT NULL,
    PRIMARY KEY (dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS dataset_spatial (
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

CREATE TABLE IF NOT EXISTS dataset_standard (
    dataset_id  TEXT NOT NULL,
    standard_id TEXT NOT NULL,
    PRIMARY KEY (dataset_id, standard_id)
);

CREATE TABLE IF NOT EXISTS distribution (
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

CREATE INDEX IF NOT EXISTS idx_dist_dataset ON distribution(dataset_id);
CREATE INDEX IF NOT EXISTS idx_dist_type ON distribution(media_type);

CREATE TABLE IF NOT EXISTS data_service (
    id           TEXT PRIMARY KEY,
    title        MAP(VARCHAR, VARCHAR),
    description  MAP(VARCHAR, VARCHAR),
    endpoint_url TEXT
);

CREATE TABLE IF NOT EXISTS service_dataset (
    service_id  TEXT NOT NULL,
    dataset_id  TEXT NOT NULL,
    PRIMARY KEY (service_id, dataset_id)
);

CREATE TABLE IF NOT EXISTS quality_annotation (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    dimension  TEXT,
    label      MAP(VARCHAR, VARCHAR),
    body       MAP(VARCHAR, VARCHAR),
    PRIMARY KEY (dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS provenance (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    label      MAP(VARCHAR, VARCHAR),
    uri        TEXT,
    PRIMARY KEY (dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS variable (
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

CREATE TABLE IF NOT EXISTS feature_type (
    dataset_id TEXT NOT NULL,
    type_name  TEXT NOT NULL,
    PRIMARY KEY (dataset_id, type_name)
);

CREATE TABLE IF NOT EXISTS author (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    name       TEXT NOT NULL,
    PRIMARY KEY (dataset_id, ordinal)
);

CREATE TABLE IF NOT EXISTS dimension_name (
    dataset_id TEXT NOT NULL,
    ordinal    INT NOT NULL,
    label      MAP(VARCHAR, VARCHAR),
    PRIMARY KEY (dataset_id, ordinal)
);

-- Catalog → theme taxonomy (dcat:themeTaxonomy — scheme-level declaration)
CREATE TABLE IF NOT EXISTS catalog_theme_taxonomy (
    catalog_id VARCHAR NOT NULL,
    scheme_uri VARCHAR NOT NULL,
    PRIMARY KEY (catalog_id, scheme_uri)
);

-- ── Layer 4: Lineage & Provenance ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS lineage (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    relationship   TEXT NOT NULL,
    activity_label TEXT,
    activity_type  TEXT,
    agent_id       TEXT,
    timestamp      TIMESTAMP,
    confidence     TEXT,
    notes          TEXT,
    UNIQUE (source_id, target_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_lineage_source ON lineage(source_id);
CREATE INDEX IF NOT EXISTS idx_lineage_target ON lineage(target_id);

-- ── Languages ─────────────────────────────────────────────────────────
-- Language tags declared as alternates of a primary tag, such as
-- `en-t-fr` (English translated from French, RFC 6497) for `en`. Recorded
-- as a source declares them. statcatlib infers no pairs (GH-84).
CREATE TABLE IF NOT EXISTS language_alternate (
    alternate_tag TEXT PRIMARY KEY,
    primary_tag   TEXT NOT NULL,
    source        TEXT NOT NULL
);
"""


def create_schema(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Create the ``statcat`` schema and all tables in *conn*.

    Safe to call on an existing database — every ``CREATE TABLE`` uses
    ``IF NOT EXISTS``.

    Returns a **cursor** with ``search_path = 'statcat'`` so that
    subsequent SQL can use unqualified table names.
    """
    cursor = conn.cursor()
    cursor.execute("CREATE SCHEMA IF NOT EXISTS statcat")
    cursor.execute("SET search_path = 'statcat'")

    for raw_stmt in _SCHEMA_DDL.split(";"):
        if clean := raw_stmt.strip():
            cursor.execute(clean)
    # Stamped once, when the schema is created; a later open never restamps it.
    cursor.execute(
        "INSERT OR IGNORE INTO _meta VALUES ('schema_version', $version)",
        {"version": SCHEMA_VERSION},
    )

    return cursor


def open_schema_readonly(conn: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Open the ``statcat`` schema on a read-only connection.

    No DDL is issued — the schema must already exist (built by a prior
    read-write open). Returns a cursor with ``search_path = 'statcat'``.
    Raises ``RuntimeError`` if the schema is missing.
    """
    if not schema_exists(conn):
        msg = "read_only=True requires an existing statcat schema; none found in this DuckDB."
        raise RuntimeError(msg)
    cursor = conn.cursor()
    cursor.execute("SET search_path = 'statcat'")
    return cursor


def first_row[T](rows: list[T]) -> T | None:
    """Return the first of *rows*, or None when there are none.

    Pass it ``conn.execute(...).fetchall()``. DuckDB 2 counts a result as
    finished only after a fetch returns no rows, so ``fetchone()`` on a
    one-row result leaves it pending, and the relation API (``conn.sql``,
    ``conn.table``) then refuses the connection with
    ``ResourceInUseException`` (GH-43). ``fetchall()`` finishes the result.
    Queries on the catalog's shared connection read single rows this way.

    Args:
        rows: The fetched rows.

    Returns:
        The first row, or None.
    """
    return rows[0] if rows else None


def schema_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    """Return True if the statcat schema exists and has the _meta table."""
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'statcat' AND table_name = '_meta'"
        ).fetchall()
        row = first_row(rows)
    except Exception:  # noqa: BLE001
        return False
    else:
        return row is not None


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> str | None:
    """Return the schema version from ``statcat._meta``, or None."""
    try:
        rows = conn.execute("SELECT value FROM statcat._meta WHERE key = 'schema_version'").fetchall()
        row = first_row(rows)
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None

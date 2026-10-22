"""Tests for statcatlib.store.schema — catalog namespace and table creation."""

import duckdb
import pytest

from statcatlib.store import schema as s

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    s.create_schema(c)
    return c


# ---------------------------------------------------------------------------
# Table existence
# ---------------------------------------------------------------------------


class TestTablesExist:
    _EXPECTED: frozenset[str] = frozenset(
        {
            "dataset",
            "agent",
            "contact",
            "frequency",
            "licence",
            "concept_scheme",
            "concept",
            "concept_relation",
            "concept_scheme_top_concept",
            "standard",
            "data_service",
            "dataset_theme",
            "dataset_subject",
            "dataset_keyword",
            "dataset_spatial",
            "catalog_theme_taxonomy",
            "distribution",
            "lineage",
            "dimension_name",
            "quality_annotation",
            "provenance",
            "variable",
            "feature_type",
            "author",
            "service_dataset",
            "dataset_standard",
            "_meta",
        }
    )

    def test_all_tables_exist(self, conn: duckdb.DuckDBPyConnection) -> None:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'"
        ).fetchall()
        tables = {r[0] for r in rows}
        assert tables >= self._EXPECTED

    def test_dataset_has_kind_column(self, conn: duckdb.DuckDBPyConnection) -> None:
        cursor = conn.cursor()
        cursor.execute("SET search_path = 'statcat'")
        cols = {r[0] for r in cursor.execute("DESCRIBE dataset").fetchall()}
        assert "kind" in cols

    def test_dataset_has_no_document_column(self, conn: duckdb.DuckDBPyConnection) -> None:
        cursor = conn.cursor()
        cursor.execute("SET search_path = 'statcat'")
        cols = {r[0] for r in cursor.execute("DESCRIBE dataset").fetchall()}
        assert "_document" not in cols

    def test_dataset_has_map_columns(self, conn: duckdb.DuckDBPyConnection) -> None:
        cursor = conn.cursor()
        cursor.execute("SET search_path = 'statcat'")
        rows = cursor.execute("DESCRIBE dataset").fetchall()
        col_types = {r[0]: r[1] for r in rows}
        assert "MAP(VARCHAR, VARCHAR)" in col_types.get("title", "")
        assert "MAP(VARCHAR, VARCHAR)" in col_types.get("description", "")

    def test_bridge_tables_have_dataset_id(self, conn: duckdb.DuckDBPyConnection) -> None:
        cursor = conn.cursor()
        cursor.execute("SET search_path = 'statcat'")
        for table in ("dataset_theme", "dataset_keyword", "dataset_spatial", "dimension_name"):
            cols = {r[0] for r in cursor.execute(f"DESCRIBE {table}").fetchall()}
            assert "dataset_id" in cols, f"{table} missing dataset_id"

    def test_lineage_columns(self, conn: duckdb.DuckDBPyConnection) -> None:
        cursor = conn.cursor()
        cursor.execute("SET search_path = 'statcat'")
        cols = {r[0] for r in cursor.execute("DESCRIBE lineage").fetchall()}
        assert {"id", "source_id", "target_id", "relationship", "activity_type", "confidence"} <= cols


# ---------------------------------------------------------------------------
# Schema namespace
# ---------------------------------------------------------------------------


class TestSchemaNamespace:
    def test_catalog_schema_created(self, conn: duckdb.DuckDBPyConnection) -> None:
        assert s.schema_exists(conn)

    def test_schema_version_stored(self, conn: duckdb.DuckDBPyConnection) -> None:
        version = s.get_schema_version(conn)
        assert version is not None
        assert version == "2.0"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotent:
    def test_create_schema_twice(self) -> None:
        c = duckdb.connect(":memory:")
        s.create_schema(c)
        s.create_schema(c)  # must not raise
        rows = c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'").fetchall()
        assert len(rows) > 0

"""The open-time guard against sdmxlib dropping a catalog's SDMX structure.

sdmxlib migrates its store by dropping the ``sdmx`` schema and rebuilding it
empty. For a catalog that is data loss: ``statcat.dataset`` would survive,
linking by URN into tables that hold nothing, and structural queries would
answer empty instead of failing. A catalog built by statcatlib 0.8.0 (sdmxlib
schema 2.19) opened read-write under sdmxlib 0.70 (schema 2.48) did exactly
that, silently.

A stale version is simulated by rewriting ``sdmx._meta``, which is the only
thing sdmxlib reads to decide.
"""

from pathlib import Path

import duckdb
import pytest
import sdmxlib as sl
from sdmxlib import InternationalString
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
import statcatlib.query as q
from tests.conftest import concept_of

pytestmark = pytest.mark.unit

_STALE = "2.19"


def _registry() -> InMemoryRegistry:
    cl = sl.Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=InternationalString(en="Frequency"),
        codes=[sl.Code(id="A", name=InternationalString(en="Annual"))],
    )
    dsd = sl.DataStructure(
        id="DSD_GUARD",
        maintainer="TEST",
        name=InternationalString(en="Guard DSD"),
        dimensions=[sl.Dimension(id="FREQ", concept=concept_of("FREQ"), representation=cl)],
    )
    flow = sl.Dataflow(id="DF_GUARD", maintainer="TEST", name=InternationalString(en="Guard flow"), structure=dsd)
    registry = InMemoryRegistry()
    for artefact in (cl, dsd, flow):
        registry.add(artefact)
    return registry


def _set_sdmx_version(meta: Path, version: str) -> None:
    with duckdb.connect(str(meta)) as conn:
        conn.execute(
            """
            UPDATE sdmx._meta SET value = $version WHERE key = 'schema_version'
            """,
            {"version": version},
        )


def _artefact_count(meta: Path) -> int:
    with duckdb.connect(str(meta), read_only=True) as conn:
        row = conn.execute("""
            SELECT count(*) FROM sdmx.artefact
        """).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def stale_catalog(tmp_path: Path) -> Path:
    """A folder catalog holding SDMX structure under another sdmxlib schema."""
    path = tmp_path / "cat"
    with sc.Catalog(path) as cat:
        sc.ingest_sdmx(cat, _registry())
    _set_sdmx_version(path / "meta.duckdb", _STALE)
    return path


class TestStaleSchemaRefused:
    def test_read_write_open_raises(self, stale_catalog: Path) -> None:
        with pytest.raises(sc.IncompatibleCatalogError, match=rf"schema {_STALE}.*schema {SCHEMA_VERSION}"):
            sc.Catalog(stale_catalog).open()

    def test_structure_survives_the_refusal(self, stale_catalog: Path) -> None:
        before = _artefact_count(stale_catalog / "meta.duckdb")
        assert before > 0
        with pytest.raises(sc.IncompatibleCatalogError):
            sc.Catalog(stale_catalog).open()
        assert _artefact_count(stale_catalog / "meta.duckdb") == before

    def test_refusal_releases_the_connection(self, stale_catalog: Path) -> None:
        cat = sc.Catalog(stale_catalog)
        with pytest.raises(sc.IncompatibleCatalogError):
            cat.open()
        # Unopened again, not half-open: querying says so rather than
        # running against a connection nothing will close.
        with pytest.raises(RuntimeError):
            cat.query(sc.StatisticalDataset).collect()

    def test_read_only_open_raises_ours_not_sdmxlibs(self, stale_catalog: Path) -> None:
        """sdmxlib's own refusal advises reopening read-write, which is the loss."""
        with pytest.raises(sc.IncompatibleCatalogError):
            sc.Catalog(stale_catalog, read_only=True).open()

    def test_from_connection_raises_and_leaves_the_connection_open(self, stale_catalog: Path) -> None:
        with duckdb.connect(str(stale_catalog / "meta.duckdb")) as conn:
            with pytest.raises(sc.IncompatibleCatalogError):
                sc.Catalog.from_connection(conn)
            row = conn.execute("""
                SELECT count(*) FROM sdmx.artefact
            """).fetchone()
            assert row is not None
            assert row[0] > 0


class TestNothingToLose:
    def test_stale_but_empty_sdmx_schema_is_rebuilt(self, tmp_path: Path) -> None:
        """A catalog with no SDMX structure loses nothing to the rebuild."""
        path = tmp_path / "cat"
        with sc.Catalog(path):
            pass
        _set_sdmx_version(path / "meta.duckdb", _STALE)
        with sc.Catalog(path) as cat:
            row = cat.connection.execute("""
                SELECT value FROM sdmx._meta WHERE key = 'schema_version'
            """).fetchone()
        assert row == (SCHEMA_VERSION,)

    def test_current_schema_opens_and_answers(self, tmp_path: Path) -> None:
        path = tmp_path / "cat"
        with sc.Catalog(path) as cat:
            sc.ingest_sdmx(cat, _registry())
        with sc.Catalog(path) as cat:
            result = cat.query(sc.StatisticalDataset).filter(q.has_code("FREQ", "A")).execute()
        assert result.total == 1

"""The open-time guard against a catalog whose ``statcat`` schema is another version (GH-39).

``create_schema`` runs ``CREATE TABLE IF NOT EXISTS`` for every table, so an
older catalog opened by a newer statcatlib gains its missing tables and keeps
its existing ones as they were. A column added since then surfaces as a
`duckdb.BinderException` on the first query that reads it, with nothing naming
the cause. The guard refuses the open instead, before any DDL runs.

Another version is simulated by rewriting ``statcat._meta``, which is the only
thing the guard reads.
"""

from pathlib import Path

import duckdb
import pytest
from sdmxlib.bundle import Producer
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.bundle import check_bundle, publish_bundle
from statcatlib.store import schema
from statcatlib.store.store import IncompatibleCatalogError
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_OTHER = "1.0"


def _set_statcat_version(meta: Path, version: str | None) -> None:
    with duckdb.connect(str(meta)) as conn:
        if version is None:
            conn.execute("DELETE FROM statcat._meta WHERE key = 'schema_version'")
        else:
            conn.execute(
                "UPDATE statcat._meta SET value = $version WHERE key = 'schema_version'",
                {"version": version},
            )


def _recorded(meta: Path) -> str | None:
    with duckdb.connect(str(meta), read_only=True) as conn:
        return schema.get_schema_version(conn)


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "cat"
    with sc.Catalog(path) as cat:
        cat.put(
            sc.OpenDataset(
                identifier="d1", title=sc.InternationalString(en="One"), description=sc.InternationalString(en="One")
            )
        )
    return path


@pytest.fixture
def other_catalog(catalog: Path) -> Path:
    _set_statcat_version(catalog / "meta.duckdb", _OTHER)
    return catalog


class TestStamp:
    def test_a_new_catalog_records_this_builds_version(self, catalog: Path) -> None:
        assert _recorded(catalog / "meta.duckdb") == schema.SCHEMA_VERSION

    def test_the_stamp_comes_from_the_constant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bumping `SCHEMA_VERSION` alone must stamp new catalogs with it, or the guard refuses them."""
        monkeypatch.setattr(schema, "SCHEMA_VERSION", "9.9")
        with sc.Catalog(tmp_path / "cat"):
            pass
        assert _recorded(tmp_path / "cat" / "meta.duckdb") == "9.9"

    def test_reopening_never_restamps(self, other_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stamp of another version stays as written; only the guard reads it."""
        monkeypatch.setattr(schema, "SCHEMA_VERSION", _OTHER)
        with sc.Catalog(other_catalog):
            pass
        assert _recorded(other_catalog / "meta.duckdb") == _OTHER


class TestGuard:
    def test_current_schema_opens_and_answers(self, catalog: Path) -> None:
        with sc.Catalog(catalog) as cat:
            assert cat.get(sc.OpenDataset, "d1") is not None

    def test_read_write_open_raises(self, other_catalog: Path) -> None:
        expected = rf"version {_OTHER}, and this build .* {schema.SCHEMA_VERSION}"
        with pytest.raises(IncompatibleCatalogError, match=expected):
            sc.Catalog(other_catalog).open()

    def test_read_only_open_raises(self, other_catalog: Path) -> None:
        with pytest.raises(IncompatibleCatalogError, match=_OTHER):
            sc.Catalog(other_catalog, read_only=True).open()

    def test_an_unrecorded_version_is_refused(self, catalog: Path) -> None:
        _set_statcat_version(catalog / "meta.duckdb", None)
        with pytest.raises(IncompatibleCatalogError, match="version unrecorded"):
            sc.Catalog(catalog).open()

    def test_refused_before_any_ddl(self, other_catalog: Path) -> None:
        """A table the older catalog lacks is still absent after the refusal."""
        with duckdb.connect(str(other_catalog / "meta.duckdb")) as conn:
            conn.execute("DROP TABLE statcat.author")
        with pytest.raises(IncompatibleCatalogError):
            sc.Catalog(other_catalog).open()
        with duckdb.connect(str(other_catalog / "meta.duckdb"), read_only=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'"
                ).fetchall()
            }
        assert "author" not in tables

    def test_refusal_releases_the_connection(self, other_catalog: Path) -> None:
        with pytest.raises(IncompatibleCatalogError):
            sc.Catalog(other_catalog).open()
        with duckdb.connect(str(other_catalog / "meta.duckdb")) as conn:
            assert conn.execute("SELECT count(*) FROM statcat.dataset").fetchone() == (1,)

    def test_from_connection_raises_and_leaves_the_connection_open(self, other_catalog: Path) -> None:
        conn = duckdb.connect(str(other_catalog / "meta.duckdb"))
        try:
            with pytest.raises(IncompatibleCatalogError, match=_OTHER):
                sc.Catalog.from_connection(conn)
            assert conn.execute("SELECT count(*) FROM statcat.dataset").fetchone() == (1,)
        finally:
            conn.close()

    def test_a_connection_with_no_statcat_schema_is_created(self) -> None:
        conn = duckdb.connect()
        try:
            sc.Catalog.from_connection(conn)
            assert schema.get_schema_version(conn) == schema.SCHEMA_VERSION
        finally:
            conn.close()


@pytest.fixture
def unindexed_bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    synthetic_catalog(path, Shape(dimensions=(Dim(codes=4),), observations=10, languages=("en",)), index=False)
    return path


def test_publish_bundle_refuses_another_version(unindexed_bundle: Path) -> None:
    """Publishing opens the catalog to describe it, so it meets the guard too."""
    _set_statcat_version(unindexed_bundle / "meta.duckdb", _OTHER)
    with pytest.raises(IncompatibleCatalogError, match=_OTHER):
        publish_bundle(unindexed_bundle, bundle_id="synthetic", producer=Producer(name="tests", version="0"))


def test_check_bundle_reports_another_version(unindexed_bundle: Path) -> None:
    """The stamp changed after publishing, so sdmxlib's ``files`` check fails too; only statcat's is asserted."""
    _set_statcat_version(unindexed_bundle / "meta.duckdb", _OTHER)
    findings = [str(f) for f in check_bundle(unindexed_bundle, references=False).findings]
    detail = f"statcat schema version {_OTHER}, not the one this build reads: rebuild the catalog"
    assert f"[statcat] statcat._meta: {detail}" in findings
    assert [f for f in findings if f.startswith("[statcat]")] == [f"[statcat] statcat._meta: {detail}"]

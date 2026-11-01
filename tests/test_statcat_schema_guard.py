"""The guard against an edition whose ``statcat`` schema is another version (GH-39).

``create_schema`` runs ``CREATE TABLE IF NOT EXISTS`` for every table, so an
older store opened by a newer statcatlib would gain its missing tables and
keep its existing ones as they were. A column added since then would surface
as a `duckdb.BinderException` on the first query that reads it, with nothing
naming the cause. The guard refuses instead, at both places an edition's store
is opened: `Product.view` over a sealed edition, and `Product.working_copy`
over the edition it builds on.

Another version is simulated by rewriting ``statcat._meta`` in the sealed
edition's ``metadata.duckdb``, which is the only thing the guard reads. The
view's manifest check covers ``manifest.json`` alone, so the rewrite reaches
the guard.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

import statcatlib as sc
from statcatlib.store import schema
from statcatlib.store.store import IncompatibleCatalogError

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


def _seal_one(root: Path) -> Path:
    """Seal an edition holding dataset ``d1`` in a new catalog at *root*; return its ``metadata.duckdb``."""
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put(
            sc.OpenDataset(
                identifier="d1", title=sc.InternationalString(en="One"), description=sc.InternationalString(en="One")
            )
        )
        edition = wc.seal()
    return root / "products" / "p" / edition.edition_id / "metadata.duckdb"


@pytest.fixture
def meta(tmp_path: Path) -> Path:
    return _seal_one(tmp_path / "cat")


@pytest.fixture
def other(meta: Path) -> Path:
    _set_statcat_version(meta, _OTHER)
    return meta


@pytest.fixture
def catalog(tmp_path: Path, meta: Path) -> Iterator[sc.Catalog]:
    del meta
    with sc.Catalog.create(tmp_path / "cat", exist_ok=True) as cat:
        yield cat


def _editions(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "cat" / "products" / "p").iterdir() if p.name not in {"data", "journal"})


class TestStamp:
    def test_a_new_edition_records_this_builds_version(self, meta: Path) -> None:
        assert _recorded(meta) == schema.SCHEMA_VERSION

    def test_the_stamp_comes_from_the_constant(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bumping `SCHEMA_VERSION` alone must stamp new editions with it, or the guard refuses them."""
        monkeypatch.setattr(schema, "SCHEMA_VERSION", "9.9")
        assert _recorded(_seal_one(tmp_path / "cat")) == "9.9"

    def test_building_on_an_edition_never_restamps(
        self, tmp_path: Path, other: Path, catalog: sc.Catalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stamp of another version is carried as written; only the guard reads it."""
        del other
        monkeypatch.setattr(schema, "SCHEMA_VERSION", _OTHER)
        edition = catalog.product("p").working_copy().seal()
        assert _recorded(tmp_path / "cat" / "products" / "p" / edition.edition_id / "metadata.duckdb") == _OTHER


class TestGuard:
    def test_current_schema_opens_and_answers(self, catalog: sc.Catalog) -> None:
        assert catalog.product("p").view().get(sc.OpenDataset, "d1") is not None

    def test_a_view_of_another_version_raises(self, other: Path, catalog: sc.Catalog) -> None:
        del other
        expected = rf"version {_OTHER}, and this build .* {schema.SCHEMA_VERSION}"
        with pytest.raises(IncompatibleCatalogError, match=expected):
            catalog.product("p").view()

    def test_an_unrecorded_version_is_refused(self, meta: Path, catalog: sc.Catalog) -> None:
        _set_statcat_version(meta, None)
        with pytest.raises(IncompatibleCatalogError, match="version unrecorded"):
            catalog.product("p").view()

    def test_a_working_copy_on_another_version_raises(self, tmp_path: Path, other: Path, catalog: sc.Catalog) -> None:
        """The edition the refused working copy started is removed, so the next one can start."""
        del other
        before = _editions(tmp_path)
        with pytest.raises(IncompatibleCatalogError, match=_OTHER):
            catalog.product("p").working_copy()
        assert _editions(tmp_path) == before

    def test_refused_before_any_ddl(self, tmp_path: Path, other: Path, catalog: sc.Catalog) -> None:
        """A table the older edition lacks is not created, and no edition is left behind."""
        with duckdb.connect(str(other)) as conn:
            conn.execute("DROP TABLE statcat.author")
        with pytest.raises(IncompatibleCatalogError):
            catalog.product("p").working_copy()
        with duckdb.connect(str(other), read_only=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'"
                ).fetchall()
            }
        assert "author" not in tables
        assert len(_editions(tmp_path)) == 1

    def test_refusal_releases_the_edition(self, other: Path, catalog: sc.Catalog) -> None:
        with pytest.raises(IncompatibleCatalogError):
            catalog.product("p").view()
        engine = catalog._engine
        assert engine is not None
        assert engine.attached == ()
        with duckdb.connect(str(other)) as conn:
            assert conn.execute("SELECT count(*) FROM statcat.dataset").fetchone() == (1,)

    def test_a_first_working_copy_creates_the_schema(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "new") as cat:
            wc = cat.product("p").working_copy()
            assert schema.get_schema_version(wc.view()._cursor()) == schema.SCHEMA_VERSION
            wc.close()

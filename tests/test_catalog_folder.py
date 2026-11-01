"""The catalog directory: what `Catalog.create` writes, and what a seal adds (Phase 2a).

`Catalog.create` writes ``catalog.json`` and nothing else; the first seal
writes the Inventory and the product's directory. The layout is the sharded
catalog spec's §4.1.
"""

import uuid
from pathlib import Path

import polars as pl
import pytest

import statcatlib as sc
from statcatlib.testing import temporary_catalog

pytestmark = pytest.mark.unit


def _dataset(identifier: str = "test-ds") -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=sc.InternationalString(en="Test"),
        description=sc.InternationalString(en="Test dataset"),
    )


class TestCreate:
    def test_create_writes_only_catalog_json(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat"):
            pass
        assert sorted(p.name for p in (tmp_path / "cat").iterdir()) == ["catalog.json"]

    def test_create_makes_missing_parents(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "a" / "b" / "cat"):
            pass
        assert (tmp_path / "a" / "b" / "cat" / "catalog.json").is_file()

    def test_create_refuses_an_existing_catalog(self, tmp_path: Path) -> None:
        sc.Catalog.create(tmp_path / "cat").close()
        with pytest.raises(FileExistsError, match="exist_ok=True"):
            sc.Catalog.create(tmp_path / "cat")

    def test_exist_ok_reopens_for_writing_and_writes_nothing(self, tmp_path: Path) -> None:
        sc.Catalog.create(tmp_path / "cat").close()
        before = (tmp_path / "cat" / "catalog.json").read_bytes()
        with sc.Catalog.create(tmp_path / "cat", exist_ok=True) as cat:
            assert cat.writable
        assert (tmp_path / "cat" / "catalog.json").read_bytes() == before
        assert sorted(p.name for p in (tmp_path / "cat").iterdir()) == ["catalog.json"]

    def test_create_logs_the_path_it_created(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("INFO", logger="statcatlib.catalog"), sc.Catalog.create(tmp_path / "cat"):
            pass
        assert str(tmp_path / "cat") in caplog.text

    def test_a_catalog_id_is_a_uuid7(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat") as cat:
            assert uuid.UUID(cat.catalog_id).version == 7


class TestSealLayout:
    def test_a_seal_writes_the_inventory_and_the_edition(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat") as cat:
            wc = cat.product("p").working_copy()
            wc.put(_dataset())
            edition = wc.seal()
        root = tmp_path / "cat"
        assert (root / "CURRENT").is_file()
        assert (root / "inventory" / "000000000001.json").is_file()
        assert len(list((root / "inventory" / "leaves").iterdir())) == 1
        assert (root / "products" / "p" / edition.edition_id / "manifest.json").is_file()
        assert (root / "products" / "p" / edition.edition_id / "metadata.duckdb").is_file()

    def test_persists_across_reopens(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat") as cat:
            wc = cat.product("p").working_copy()
            wc.put(_dataset())
            wc.seal()
        with sc.Catalog.open(tmp_path / "cat") as cat:
            assert cat.product("p").view().get(sc.BaseDataset, "test-ds") is not None

    def test_write_observations_through_the_editions_lake(self, tmp_path: Path) -> None:
        """``wc.registry.write_observations`` materialises a table in the edition's lake."""
        with sc.Catalog.create(tmp_path / "cat") as cat:
            wc = cat.product("p").working_copy()
            df = pl.DataFrame({"REF_AREA": ["CA", "US"], "OBS_VALUE": [1.0, 2.0]})
            wc.registry.write_observations(schema="t", table="obs", source=df)
            rows = wc.registry.connection.execute("SELECT COUNT(*) FROM lake.t.obs").fetchall()
            assert rows == [(2,)]
            wc.close()


class TestRemovedConstructors:
    @pytest.mark.parametrize("args", [(), (":memory:",), ("some/path",)])
    def test_catalog_constructor_names_the_new_calls(self, args: tuple[str, ...]) -> None:
        with pytest.raises(TypeError, match=r"Catalog\.open.*Catalog\.create.*temporary_catalog"):
            sc.Catalog(*args)

    def test_temporary_catalog_is_writable_and_removed(self) -> None:
        with temporary_catalog() as cat:
            wc = cat.product("p").working_copy()
            wc.put(_dataset())
            wc.seal()
            cat.refresh()
            assert cat.product("p").view().get(sc.BaseDataset, "test-ds") is not None
            root = cat._root
        assert not root.exists()

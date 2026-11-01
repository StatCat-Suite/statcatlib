"""Unit tests for Catalog folder mode + Path-style construction."""

from pathlib import Path

import polars as pl
import pytest
import sdmxlib as sl

import statcatlib as sc

pytestmark = pytest.mark.unit


class TestPathStyleOpen:
    """``Catalog(path).open()`` — Path-style, idempotent.

    Construction is cheap; open() creates the folder layout if it doesn't
    exist and re-attaches if it does. There's no separate strict-create
    or strict-open shape — one Path-style entry point is used
    everywhere.
    """

    def test_creates_meta_duckdb(self, tmp_path: Path) -> None:
        with sc.Catalog(tmp_path / "cat").open():
            pass
        assert (tmp_path / "cat" / "meta.duckdb").exists()

    def test_does_not_create_parquet_subdir(self, tmp_path: Path) -> None:
        """An empty ``parquet/`` is a lie about the layout.

        DuckLake creates its own DATA_PATH directory on first write, so
        making one here bought nothing — and it destroyed a real signal:
        beside a split bundle, an empty local ``parquet/`` makes an
        unresolvable layout look co-located.
        """
        with sc.Catalog(tmp_path / "cat").open():
            pass
        assert (tmp_path / "cat").is_dir()
        assert not (tmp_path / "cat" / "parquet").exists()

    def test_bundle_dir_property(self, tmp_path: Path) -> None:
        with sc.Catalog(tmp_path / "cat").open() as store:
            assert store.bundle_dir == tmp_path / "cat"

    def test_data_property_is_data_store(self, tmp_path: Path) -> None:
        with sc.Catalog(tmp_path / "cat").open() as store:
            assert isinstance(store.data, sl.DuckDBDataStore)

    def test_idempotent_reopen(self, tmp_path: Path) -> None:
        with sc.Catalog(tmp_path / "cat").open():
            pass
        with sc.Catalog(tmp_path / "cat").open() as store:
            assert store.bundle_dir is not None

    def test_with_block_implicit_open(self, tmp_path: Path) -> None:
        """``with Catalog(p):`` opens implicitly via __enter__."""
        with sc.Catalog(tmp_path / "cat") as store:
            assert store.bundle_dir is not None

    def test_opens_without_a_parquet_dir(self, tmp_path: Path) -> None:
        """``parquet/`` is not the sentinel — its absence must not block an open."""
        with sc.Catalog(tmp_path / "cat").open():
            pass
        assert not (tmp_path / "cat" / "parquet").exists()
        with sc.Catalog(tmp_path / "cat").open() as store:
            assert store.bundle_dir is not None

    def test_persists_across_reopens(self, tmp_path: Path) -> None:
        """Catalog content survives close + reopen against the same path."""
        cat = tmp_path / "cat"
        with sc.Catalog(cat).open() as store:
            store.put(
                sc.OpenDataset(
                    identifier="test-ds",
                    title=sc.InternationalString(en="Test"),
                    description=sc.InternationalString(en="Test dataset"),
                )
            )
        with sc.Catalog(cat).open() as store:
            assert store.get(sc.BaseDataset, "test-ds") is not None

    def test_write_observations_through_lake(self, tmp_path: Path) -> None:
        """``cat.data.write_observations`` materialises a table in the lake."""
        with sc.Catalog(tmp_path / "cat").open() as store:
            df = pl.DataFrame({"REF_AREA": ["CA", "US"], "OBS_VALUE": [1.0, 2.0]})
            store.data.write_observations(schema="t", table="obs", source=df)
            rows = store.connection.execute("SELECT COUNT(*) FROM lake.t.obs").fetchone()
            assert rows is not None
            assert rows[0] == 2


class TestMemoryMode:
    def test_bundle_dir_is_none(self) -> None:
        with sc.Catalog().open() as store:
            assert store.bundle_dir is None

    def test_explicit_memory_string(self) -> None:
        with sc.Catalog(":memory:").open() as store:
            assert store.bundle_dir is None

    def test_data_property_available(self) -> None:
        """Even in-memory catalogs expose ``.data`` — it just has no lake."""
        with sc.Catalog().open() as store:
            assert isinstance(store.data, sl.DuckDBDataStore)


class TestCheapConstruction:
    """``Catalog(uri)`` doesn't open the connection until ``.open()``."""

    def test_construction_does_no_io(self, tmp_path: Path) -> None:
        # Constructing against a path doesn't create the folder
        cat_ref = sc.Catalog(tmp_path / "cat")
        assert not (tmp_path / "cat").exists()
        # Methods that need the connection raise a clear error before open
        with pytest.raises(RuntimeError, match="not open"):
            _ = cat_ref.connection

    def test_uri_property_available_pre_open(self, tmp_path: Path) -> None:
        cat_ref = sc.Catalog(tmp_path / "cat")
        assert cat_ref.uri == tmp_path / "cat"

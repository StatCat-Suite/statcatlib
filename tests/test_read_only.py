"""`Catalog.open` is read-only: its views answer, and nothing it returns writes (Phase 2a, P4).

A sealed edition opens through ``LocalRegistry.edition``, with DuckDB's
``read_only``, so any number of processes may read it at once. A view has no
write methods, and a catalog from `Catalog.open` starts no working copy.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _dataset(identifier: str, title: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=_i(title),
        description=_i(f"{title} dataset"),
        publisher=sc.Agent(id="PUB", name=_i("Pub")),
        status="current",
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A catalog whose product ``p`` has a sealed edition holding DS-1 and DS-2."""
    path = tmp_path / "cat"
    with sc.Catalog.create(path) as cat:
        wc = cat.product("p").working_copy()
        wc.put([_dataset("DS-1", "First"), _dataset("DS-2", "Second")])
        wc.seal()
    return path


@pytest.fixture
def view(root: Path) -> Iterator[sc.EditionView]:
    with sc.Catalog.open(root) as cat:
        yield cat.product("p").view()


class TestReadOnlyOpen:
    def test_query_works(self, view: sc.EditionView) -> None:
        ds = view.get(sc.OpenDataset, "DS-1")
        assert ds is not None
        assert ds.identifier == "DS-1"
        assert {d.identifier for d in view.query(sc.OpenDataset).execute().items} == {"DS-1", "DS-2"}

    def test_no_exclusive_lock(self, root: Path, view: sc.EditionView) -> None:
        """Two catalogs and a raw read-only connection read the same edition at once."""
        with sc.Catalog.open(root) as other:
            assert other.product("p").view().get(sc.OpenDataset, "DS-2") is not None
        assert view.get(sc.OpenDataset, "DS-1") is not None
        (meta,) = (root / "products" / "p").glob("*/metadata.duckdb")
        with duckdb.connect(str(meta), read_only=True) as second:
            assert second.execute("SELECT count(*) FROM statcat.dataset").fetchall() == [(2,)]

    def test_open_refuses_a_directory_that_is_not_a_catalog(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"Catalog\.create"):
            sc.Catalog.open(tmp_path / "nope")


class TestWriteGuards:
    def test_a_view_has_no_write_methods(self, view: sc.EditionView) -> None:
        for name in ("put", "put_concept", "add_lineage", "add_language_alternates", "bulk_load", "build_index"):
            assert not hasattr(view, name), name

    def test_the_sealed_store_refuses_writes(self, view: sc.EditionView) -> None:
        store = view._store
        with pytest.raises(RuntimeError, match="sealed edition is never written"):
            store.put(_dataset("DS-NEW", "New"))
        with pytest.raises(RuntimeError, match="sealed edition is never written"), store.bulk_load():
            pass
        with pytest.raises(RuntimeError, match="sealed edition is never written"):
            store.build_index()
        with pytest.raises(RuntimeError, match="sealed edition is never written"):
            store.add_lineage("DS-1", "DS-2", "derived_from")

    def test_an_opened_catalog_starts_no_working_copy(self, root: Path) -> None:
        with sc.Catalog.open(root) as cat, pytest.raises(RuntimeError, match=r"Catalog\.create\(.*exist_ok=True\)"):
            cat.product("p").working_copy()

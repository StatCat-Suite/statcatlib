"""The guard against a search index whose manifest version this build does not read (GH-40).

`_MANIFEST_VERSION` is bumped only when an existing field of ``index.json``
changes meaning, so an index at another version would be served wrongly.
Phase 2a moved it to version 3, naming tables by edition id: a sealed
edition's view refuses any other version, and so does a working copy that
carries such an index from the edition it is built on.

Another version is simulated by rewriting ``search/index.json`` in the sealed
edition, which is the only thing the guard reads. The view's manifest check
covers ``manifest.json`` alone, so the rewrite reaches the guard.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.store import lance
from statcatlib.store.lance import LanceIndex
from statcatlib.store.store import IncompatibleCatalogError
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_NEWER = lance._MANIFEST_VERSION + 1


def _set_index_version(search_dir: Path, version: object) -> None:
    path = search_dir / "index.json"
    manifest = json.loads(path.read_text())
    if version is None:
        del manifest["version"]
    else:
        manifest["version"] = version
    path.write_text(json.dumps(manifest))


@pytest.fixture
def edition(tmp_path: Path) -> sc.Edition:
    pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
    shape = Shape(dimensions=(Dim(codes=4),), observations=10, languages=("en",))
    return synthetic_catalog(tmp_path / "catalog", shape)


@pytest.fixture
def search_dir(tmp_path: Path, edition: sc.Edition) -> Path:
    return tmp_path / "catalog" / "products" / edition.product_id / edition.edition_id / "search"


@pytest.fixture
def catalog(tmp_path: Path, edition: sc.Edition) -> Iterator[sc.Catalog]:
    del edition
    with sc.Catalog.open(tmp_path / "catalog") as cat:
        yield cat


class TestReadable:
    def test_a_sealed_index_records_this_builds_version(self, search_dir: Path) -> None:
        assert LanceIndex(search_dir).version == lance._MANIFEST_VERSION == 3
        assert LanceIndex.unreadable_version(search_dir) is None

    def test_this_builds_version_opens_and_searches(self, catalog: sc.Catalog) -> None:
        view = catalog.product("synthetic").view()
        assert view.index_layout
        assert view.search("synthetic", kinds=("dataset",)).total >= 0

    def test_no_index_has_nothing_to_check(self, tmp_path: Path) -> None:
        assert LanceIndex.unreadable_version(tmp_path / "search") is None


class TestGuard:
    def test_a_newer_version_is_refused(self, catalog: sc.Catalog, search_dir: Path) -> None:
        _set_index_version(search_dir, _NEWER)
        expected = rf"index.json records version {_NEWER}, written by statcatlib .*build_index\(\)"
        with pytest.raises(IncompatibleCatalogError, match=expected):
            catalog.product("synthetic").view()

    def test_version_2_is_refused(self, catalog: sc.Catalog, search_dir: Path) -> None:
        _set_index_version(search_dir, 2)
        with pytest.raises(IncompatibleCatalogError, match=r"index\.json records version 2,"):
            catalog.product("synthetic").view()

    @pytest.mark.parametrize(
        ("version", "recorded"), [(None, "no version"), (True, "version True"), ("3", "version 3")]
    )
    def test_a_missing_or_malformed_version_is_refused(
        self, catalog: sc.Catalog, search_dir: Path, version: object, recorded: str
    ) -> None:
        _set_index_version(search_dir, version)
        with pytest.raises(IncompatibleCatalogError, match=f"index.json records {recorded},"):
            catalog.product("synthetic").view()

    def test_a_refused_view_detaches_its_edition(self, catalog: sc.Catalog, search_dir: Path) -> None:
        _set_index_version(search_dir, 2)
        with pytest.raises(IncompatibleCatalogError):
            catalog.product("synthetic").view()
        engine = catalog._engine
        assert engine is not None
        assert engine.attached == ()

    def test_a_working_copy_carrying_a_refused_index_is_refused(self, tmp_path: Path, search_dir: Path) -> None:
        """The working copy's carried index is checked too, and the edition it started is removed.

        The version is rewritten in place, keeping the file's size, which is
        what sdmxlib checks when it carries the file into the new edition.
        """
        path = search_dir / "index.json"
        path.write_text(path.read_text().replace('"version": 3', '"version": 2'))
        product_dir = tmp_path / "catalog" / "products" / "synthetic"
        before = sorted(p.name for p in product_dir.iterdir())
        with (
            sc.Catalog.create(tmp_path / "catalog", exist_ok=True) as cat,
            pytest.raises(IncompatibleCatalogError, match="version 2"),
        ):
            cat.product("synthetic").working_copy()
        assert sorted(p.name for p in product_dir.iterdir()) == before

"""`CatalogAdmin.create`: a new catalog in object storage, its refusals, and the first version's embargo (GH-159).

Every store is a `MemoryStore`, and no test reads a storage clock or a credential.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, override

import pytest
from obstore.store import MemoryStore
from sdmxlib.inventory import CATALOG_FORMAT, CATALOG_NAME, decode_catalog, decode_root, root_path

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, _ObjectOps
from statcatlib.store.inventory import InventoryWriter
from tests._gc import RecordingOps
from tests._release import URI, ReleaseCatalog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from obstore import ObjectMeta

pytestmark = pytest.mark.unit


def _paths(store: MemoryStore) -> list[str]:
    """Every object the store holds."""
    return sorted(meta["path"] for meta in _ObjectOps(store).list(""))


def _stored(store: MemoryStore, path: str) -> bytes:
    got = _ObjectOps(store).read(path)
    assert got is not None, path
    return got[0]


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def location(store: MemoryStore) -> BlobLocation:
    return BlobLocation(URI, store, ops=RecordingOps(store))


def _calls(location: BlobLocation) -> list[tuple[str, str]]:
    assert isinstance(location.ops, RecordingOps)
    return location.ops.calls


class TestCreate:
    def test_it_writes_catalog_json_alone(self, location: BlobLocation, store: MemoryStore) -> None:
        admin = CatalogAdmin.create(location)

        assert _paths(store) == [CATALOG_NAME]
        document = decode_catalog(_stored(store, CATALOG_NAME))
        assert (document.format_version, document.restore_epoch) == (CATALOG_FORMAT, 0)
        assert document.catalog_id == admin.catalog_id
        assert uuid.UUID(admin.catalog_id).version == 7

    def test_it_reads_no_clock_and_no_identity(self, location: BlobLocation) -> None:
        # A `MemoryStore` has no storage clock and no token, so a read of either raises.
        admin = CatalogAdmin.create(location)

        assert _calls(location) == [
            ("read", CATALOG_NAME),
            ("list", "inventory"),
            ("list", "products"),
            ("list", "pending"),
            # `write_catalog_json` reads the name once more before its create-only Put.
            ("read", CATALOG_NAME),
            ("put", CATALOG_NAME),
        ]
        assert admin.products() == []

    def test_the_new_catalog_opens(self, location: BlobLocation) -> None:
        created = CatalogAdmin.create(location)

        assert CatalogAdmin.open(location).catalog_id == created.catalog_id

    def test_two_catalogs_get_two_ids(self) -> None:
        first = CatalogAdmin.create(BlobLocation(URI, MemoryStore()))
        second = CatalogAdmin.create(BlobLocation(URI, MemoryStore()))

        assert first.catalog_id != second.catalog_id

    def test_a_path_raises_type_error_naming_catalog_create(self, tmp_path: Path) -> None:
        path: Any = tmp_path

        with pytest.raises(TypeError, match=r"Catalog\.create"):
            CatalogAdmin.create(path)

        assert list(tmp_path.iterdir()) == []


class TestAnExistingCatalog:
    def test_a_second_create_names_the_catalog_id_and_writes_nothing(
        self, location: BlobLocation, store: MemoryStore
    ) -> None:
        first = CatalogAdmin.create(location)
        before = _stored(store, CATALOG_NAME)
        _calls(location).clear()

        with pytest.raises(FileExistsError, match=first.catalog_id):
            CatalogAdmin.create(location)

        assert _calls(location) == [("read", CATALOG_NAME)]
        assert _stored(store, CATALOG_NAME) == before
        assert _paths(store) == [CATALOG_NAME]

    def test_a_catalog_json_that_cannot_be_read_is_kept(self, location: BlobLocation, store: MemoryStore) -> None:
        _ObjectOps(store).put(CATALOG_NAME, b"not json")

        with pytest.raises(FileExistsError, match="cannot be read"):
            CatalogAdmin.create(location)

        assert _stored(store, CATALOG_NAME) == b"not json"

    def test_a_create_that_loses_the_race_names_the_winner(self, store: MemoryStore) -> None:
        class Racing(_ObjectOps):
            """Another create lands between this one's read of ``catalog.json`` and its Put."""

            winner: str | None = None

            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                if self.winner is None:
                    self.winner = CatalogAdmin.create(BlobLocation(URI, store)).catalog_id
                return super().put(path, data, **kwargs)

        ops = Racing(store)

        with pytest.raises(FileExistsError) as raised:
            CatalogAdmin.create(BlobLocation(URI, store, ops=ops))

        assert ops.winner is not None
        assert ops.winner in str(raised.value)
        assert decode_catalog(_stored(store, CATALOG_NAME)).catalog_id == ops.winner


class TestALostIdentity:
    @pytest.mark.parametrize(
        "path",
        [
            "inventory/roots/00000000000000000001.json",
            "products/p/01a18e00-0000-7000-8000-000000000001/manifest.json",
            "pending/p/01a18e00-0000-7000-8000-000000000001.json",
        ],
        ids=["inventory", "products", "pending"],
    )
    def test_an_object_under_a_catalog_prefix_is_refused(
        self, location: BlobLocation, store: MemoryStore, path: str
    ) -> None:
        _ObjectOps(store).put(path, b"x")

        with pytest.raises(sc.IncompatibleCatalogError, match=path) as raised:
            CatalogAdmin.create(location)

        assert "catalog_id is lost" in str(raised.value)
        assert _calls(location)[-1] == ("list", path.partition("/")[0])
        assert _paths(store) == [path]

    def test_an_object_beside_the_prefixes_is_no_refusal(self, location: BlobLocation, store: MemoryStore) -> None:
        # `obstore` lists by whole path segments, so `products-old/` is outside `products/`.
        _ObjectOps(store).put("products-old/p/manifest.json", b"x")

        CatalogAdmin.create(location)

        assert _paths(store) == [CATALOG_NAME, "products-old/p/manifest.json"]

    def test_the_listing_stops_at_the_first_object(self, store: MemoryStore) -> None:
        class Counting(_ObjectOps):
            """Counts the objects a listing was asked for."""

            taken = 0

            @override
            def list(self, prefix: str) -> Iterator[ObjectMeta]:
                for meta in super().list(prefix):
                    self.taken += 1
                    yield meta

        plain = _ObjectOps(store)
        for n in range(5):
            plain.put(f"products/p/data/{n}.parquet", b"x")
        ops = Counting(store)

        with pytest.raises(sc.IncompatibleCatalogError):
            CatalogAdmin.create(BlobLocation(URI, store, ops=ops))

        assert ops.taken == 1


class TestTheFirstVersionsEmbargo:
    """A new catalog holds no embargo setting, and its first version records ``embargo: false`` (design Q1)."""

    def test_the_embargo_is_off_before_the_first_version(self, location: BlobLocation) -> None:
        assert CatalogAdmin.create(location).embargo is False

    def test_the_first_fold_writes_embargo_false_and_the_release_is_not_held(self) -> None:
        cat = ReleaseCatalog()
        cat.delete(CATALOG_NAME)
        CatalogAdmin.create(BlobLocation(URI, cat.store))
        edition = cat.upload("p", release_at=cat.now)

        report = cat.run()

        assert report.failed == {}
        assert report.folded == (f"p/{edition}",)
        assert report.released == (f"p/{edition}",)
        assert "release" not in report.skipped
        first = decode_root(cat.read(root_path(1)))
        assert (first.event.kind, first.embargo) == ("seal", False)
        assert cat.full().embargo is False
        assert cat.states("p") == {edition: "live"}

    def test_an_embargo_set_before_the_first_fold_holds_the_release(self) -> None:
        cat = ReleaseCatalog()
        cat.delete(CATALOG_NAME)
        created = CatalogAdmin.create(BlobLocation(URI, cat.store), actor="operator", clock=lambda: cat.now)
        assert created.set_embargo(on=True, reason="jdoe: hold the first release") == 1
        edition = cat.upload("p", release_at=cat.now)

        report = cat.run()

        assert report.released == ()
        assert "release" in report.skipped
        assert cat.states("p") == {edition: "embargoed"}


def test_write_catalog_json_never_replaces_a_document() -> None:
    """The library call under `CatalogAdmin.create` is create-only on the store, whatever its read saw."""
    store = MemoryStore()
    held = InventoryWriter(BlobInventoryStorage(BlobLocation(URI, store))).write_catalog_json()

    class Blind(_ObjectOps):
        """Reads ``catalog.json`` as absent once, as a writer that read before the other's Put."""

        blind = True

        @override
        def read(self, path: str) -> tuple[bytes, str | None] | None:
            if path == CATALOG_NAME and self.blind:
                self.blind = False
                return None
            return super().read(path)

    late = InventoryWriter(
        BlobInventoryStorage(BlobLocation(URI, store, ops=Blind(store)), clock=lambda: datetime.now(UTC))
    )

    with pytest.raises(FileExistsError, match="holds other bytes"):
        late.write_catalog_json()

    assert decode_catalog(_stored(store, CATALOG_NAME)) == held

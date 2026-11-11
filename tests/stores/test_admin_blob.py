"""`CatalogAdmin` over a blob catalog on Azurite (Phase 5 D11): its changes, and ``advance-current``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import attrs
import msgspec
import pytest
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    Leaf,
    decode_catalog,
    decode_current,
    encode,
    record,
    root_path,
    sha256_hex,
)
from sdmxlib.inventory.conformance import SCHEMA_VERSION

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.model import InternationalString
from statcatlib.store.blob import BlobLocation
from statcatlib.store.inventory import InventoryWriter
from tests.conftest import sealed_data_edition
from tests.stores import _stores

if TYPE_CHECKING:
    from pathlib import Path

    from sdmxlib.inventory import Root

pytestmark = pytest.mark.stores

_RELEASE = datetime(2099, 1, 1, tzinfo=UTC)


def _leaf(product: str, n: int) -> Leaf:
    return record(
        None,
        product_id=product,
        kind="data",
        lifecycle="revisable",
        edition_id=f"01a18e00-0000-7000-8000-{n:012d}",
        previous=None,
        manifest_sha256="0" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=False,
        now=datetime.now(UTC),
    )


def _current(location: BlobLocation) -> Current:
    got = location.ops.read(CURRENT_NAME)
    assert got is not None
    return decode_current(got[0])


def _point_current(location: BlobLocation, seq: int, raw_root: bytes, *, epoch: int = 0) -> None:
    """Write ``CURRENT`` naming version *seq*, whose stored bytes are *raw_root*, as a lagging writer leaves it."""
    current = Current(seq=seq, root_sha256=sha256_hex(raw_root), restore_epoch=epoch)
    location.ops.put(CURRENT_NAME, encode(current))


def _stored(location: BlobLocation, seq: int) -> bytes:
    got = location.ops.read(root_path(seq))
    assert got is not None
    return got[0]


def _admin(location: BlobLocation) -> CatalogAdmin:
    return CatalogAdmin.open(location, actor="release-job", clock=lambda: datetime.now(UTC))


class TestAdvanceCurrent:
    def test_a_lagging_current_moves_forward_to_the_head(self, writer: InventoryWriter, location: BlobLocation) -> None:
        one = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="1")
        two = writer.try_commit(one, {"b": _leaf("b", 2)}, kind="seal", reason="2")
        writer.try_commit(two, {"c": _leaf("c", 3)}, kind="seal", reason="3")
        _point_current(location, 1, _stored(location, 1))

        assert _admin(location).advance_current() == 3
        assert _current(location) == Current(seq=3, root_sha256=sha256_hex(_stored(location, 3)), restore_epoch=0)

    def test_a_current_current_is_left_alone(self, writer: InventoryWriter, location: BlobLocation) -> None:
        writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="1")
        before = location.ops.read(CURRENT_NAME)

        assert _admin(location).advance_current() is None
        assert location.ops.read(CURRENT_NAME) == before

    def test_it_never_moves_current_back_past_a_racing_writer(
        self, writer: InventoryWriter, location: BlobLocation, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        one = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="1")
        two = writer.try_commit(one, {"b": _leaf("b", 2)}, kind="seal", reason="2")
        _point_current(location, 1, _stored(location, 1))
        admin = _admin(location)
        storage = admin._writer.storage
        original = storage.advance_current

        def raced(root: Root, data: bytes) -> None:
            # Another writer commits version 3 and moves CURRENT to it before this move lands.
            writer.try_commit(two, {"c": _leaf("c", 3)}, kind="seal", reason="3")
            original(root, data)

        monkeypatch.setattr(storage, "advance_current", raced)

        assert admin.advance_current() == 3
        assert _current(location).seq == 3

    def test_it_crosses_a_restore_epoch(self, writer: InventoryWriter, location: BlobLocation) -> None:
        one = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="1")
        two = writer.try_commit(one, {"b": _leaf("b", 2)}, kind="seal", reason="2")
        writer.try_commit(two, {"c": _leaf("c", 3)}, kind="seal", reason="3")
        old_three = _stored(location, 3)
        # Restore to version 1: versions 2 and 3 go, and catalog.json's restore_epoch becomes 1.
        location.ops.delete(root_path(3))
        location.ops.delete(root_path(2))
        stored = location.ops.read(CATALOG_NAME)
        assert stored is not None
        location.ops.put(CATALOG_NAME, encode(msgspec.structs.replace(decode_catalog(stored[0]), restore_epoch=1)))
        restored = writer.try_commit(one, {"d": _leaf("d", 4)}, kind="seal", reason="after the restore")
        assert (restored.seq, restored.restore_epoch) == (2, 1)
        # CURRENT still names the discarded version 3 of epoch 0: a higher seq, in an older epoch.
        _point_current(location, 3, old_three, epoch=0)

        assert _admin(location).advance_current() == 2
        assert _current(location) == Current(seq=2, root_sha256=sha256_hex(_stored(location, 2)), restore_epoch=1)


@attrs.frozen
class _Placed:
    """Product ``a`` live at ``first`` with ``second`` embargoed on it, and product ``b`` embargoed."""

    first: str
    second: str
    other: str


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("admin") / "catalog"
    first = sealed_data_edition(root, product_id="a")
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("a").working_copy()
        wc.put(sc.OpenDataset(identifier="d2", title=InternationalString(en="Two"), description=InternationalString()))
        second = wc.seal()
    other = sealed_data_edition(root, product_id="b")
    return first, first.parent / second.edition_id, other


@pytest.fixture
def placed(writer: InventoryWriter, location: BlobLocation, sealed: tuple[Path, Path, Path]) -> _Placed:
    first, second, other = sealed
    live = _stores.record_uploaded(writer, location, first)
    writer.try_commit(writer.head(), {}, embargo=True, kind="embargo-change", reason="embargo on")
    held = _stores.record_uploaded(writer, location, second, release_at=_RELEASE)
    withheld = _stores.record_uploaded(writer, location, other, release_at=_RELEASE)
    return _Placed(live.edition_id, held.edition_id, withheld.edition_id)


class _Clock:
    """The storage clock a test gives `CatalogAdmin`: each reading a minute after the last, every reading kept."""

    def __init__(self) -> None:
        self.readings: list[datetime] = []
        self._start = datetime.now(UTC)

    def __call__(self) -> datetime:
        self.readings.append(self._start + timedelta(minutes=len(self.readings) + 1))
        return self.readings[-1]


class TestChanges:
    def test_each_change_writes_one_version_with_the_given_actor_and_the_storage_clock(
        self, writer: InventoryWriter, location: BlobLocation, placed: _Placed
    ) -> None:
        clock = _Clock()
        admin = CatalogAdmin.open(location, actor="release-job", clock=clock)
        start = writer.head()
        assert start is not None
        changes = [
            ("release", lambda: admin.release("a", placed.second, reason="jdoe: due")),
            ("withdraw", lambda: admin.cancel("b", placed.other, reason="jdoe: wrong base")),
            ("rollback", lambda: admin.rollback("a", reason="jdoe: bad figures")),
            ("embargo-change", lambda: admin.set_embargo(on=False, reason="jdoe: releases resume")),
        ]

        for n, (kind, change) in enumerate(changes, start=1):
            readings = len(clock.readings)
            seq = change()
            root = writer.head()
            assert root is not None
            assert (seq, root.seq) == (start.seq + n, start.seq + n)
            assert (root.event.kind, root.event.actor) == (kind, "release-job")
            assert len(clock.readings) == readings + 1, "one storage clock reading per change"
            assert root.created_at == clock.readings[-1]

        full = writer.full()
        assert full is not None
        states = {e.edition_id: (e.state, e.state_changed_at) for e in full.entries("a")}
        assert states[placed.first] == ("live", clock.readings[2])
        assert states[placed.second] == ("withdrawn", clock.readings[2])
        assert full.entries("b")[0].state == "withdrawn"
        assert full.entries("b")[0].state_changed_at == clock.readings[1]
        assert full.embargo is False

    def test_editions_and_products_read_the_blob_head(self, location: BlobLocation, placed: _Placed) -> None:
        admin = _admin(location)

        assert admin.products() == ["a", "b"]
        assert admin.embargo is True
        assert {e.edition_id: e.state for e in admin.editions("a")} == {
            placed.first: "live",
            placed.second: "embargoed",
        }
        assert repr(admin) == f"<CatalogAdmin {location.uri}>"

    def test_prune_is_refused(self, location: BlobLocation, placed: _Placed) -> None:
        with pytest.raises(TypeError, match="statcat gc"):
            _admin(location).prune(older_than=timedelta(days=1), reason="r")


class TestList:
    def test_it_lists_under_the_stores_prefix_by_path_from_the_catalog_root(self, location: BlobLocation) -> None:
        for path in ("pending/a/1.json", "pending/a/2.json", "pending/ab/1.json", "products/a/manifest.json"):
            location.ops.put(path, b"{}")

        found = sorted(meta["path"] for meta in location.ops.list("pending/a"))

        assert found == ["pending/a/1.json", "pending/a/2.json"]

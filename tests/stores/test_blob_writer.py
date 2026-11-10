"""The blob Inventory writer on a real store: its conditional headers (GH-98) and its manifest reader (GH-52)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, override

import msgspec
import pytest
from obstore.exceptions import AlreadyExistsError, PreconditionError
from sdmxlib.edition import MANIFEST_NAME, ManifestError, read_manifest
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    OPTED_IN_CATALOG_FORMAT,
    CatalogHistory,
    Leaf,
    decode_catalog,
    decode_current,
    encode,
    leaf_ref,
    record,
    root_path,
    sha256_hex,
)
from sdmxlib.inventory.conformance import SCHEMA_VERSION

from statcatlib import InventoryWriteRefusedError
from statcatlib.store.blob import MAX_CLOCK_SKEW, BlobInventoryStorage, BlobLocation, _ObjectOps
from statcatlib.store.inventory import Change, InventoryWriter
from statcatlib.upload import _upload
from tests.conftest import sealed_data_edition
from tests.stores import _stores

if TYPE_CHECKING:
    from obstore.store import ObjectStore

pytestmark = pytest.mark.stores

_FIVE_MIB = 5 * 1024 * 1024


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


def _storage(location: BlobLocation, actor: str = "tests") -> BlobInventoryStorage:
    return BlobInventoryStorage(location, actor=actor, clock=lambda: datetime.now(UTC))


class TestLostRace:
    def test_the_loser_recomputes_its_history_fields_from_the_new_head(
        self, location: BlobLocation, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loser = InventoryWriter(_storage(location, "loser"), artefacts=lambda *_: {})
        loser.write_catalog_json()
        winner = InventoryWriter(_storage(location, "winner"))
        first = loser.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        _stores.race_once(
            monkeypatch,
            loser,
            lambda: winner.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="winner"),
        )
        root = loser.commit(lambda _: Change(leaves={"c": _leaf("c", 3)}), kind="seal", reason="loser")

        assert root.seq == 3
        stored = loser.storage.read(root_path(2))
        assert stored is not None
        assert root.previous_root_sha256 == sha256_hex(stored)
        changes = root.changes
        assert not isinstance(changes, msgspec.UnsetType)
        assert [c.product_id for c in changes] == ["c"]
        assert set(root.products) == {"a", "b", "c"}


class _Reads(_ObjectOps):
    """Records the path of every read."""

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self.reads: list[str] = []

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self.reads.append(path)
        return super().read(path)


class TestManifestReader:
    """A blob writer's default reader fetches each incoming manifest through the storage (Phase 5 D6)."""

    @pytest.fixture
    def reads(self, azurite: _stores.Running) -> tuple[InventoryWriter, BlobLocation, _Reads]:
        plain = _stores.location(azurite, uuid.uuid4().hex)
        ops = _Reads(plain.store)
        location = BlobLocation(plain.uri, plain.store, ops=ops)
        writer = InventoryWriter(_storage(location))
        writer.write_catalog_json()
        return writer, location, ops

    @staticmethod
    def _entering(edition_dir: Path) -> tuple[str, Leaf]:
        manifest = read_manifest(edition_dir)
        leaf = record(
            None,
            product_id=manifest.product_id,
            kind="data",
            lifecycle="revisable",
            edition_id=manifest.edition_id,
            previous=None,
            manifest_sha256=sha256_hex((edition_dir / MANIFEST_NAME).read_bytes()),
            schema_version=manifest.schema_version,
            embargo=False,
            now=datetime.now(UTC),
        )
        return manifest.product_id, leaf

    def test_a_commit_with_an_incoming_entry_reads_its_manifest_and_commits(
        self, reads: tuple[InventoryWriter, BlobLocation, _Reads], tmp_path: Path
    ) -> None:
        writer, location, ops = reads
        edition_dir = sealed_data_edition(tmp_path / "catalog")
        with _stores.KeyedRest(location) as rest:
            _upload(edition_dir, location, rest=rest)
        product, leaf = self._entering(edition_dir)
        ops.reads.clear()

        root = writer.commit(lambda _: Change(leaves={product: leaf}), kind="seal", reason="incoming")

        assert root.seq == 1
        assert f"products/{product}/{edition_dir.name}/{MANIFEST_NAME}" in ops.reads

    def test_an_absent_manifest_raises_manifest_error_and_writes_nothing(
        self, reads: tuple[InventoryWriter, BlobLocation, _Reads], tmp_path: Path
    ) -> None:
        writer, _, _ = reads
        edition_dir = sealed_data_edition(tmp_path / "catalog")
        product, leaf = self._entering(edition_dir)

        with pytest.raises(ManifestError, match="is absent"):
            writer.commit(lambda _: Change(leaves={product: leaf}), kind="seal", reason="incoming")

        assert not writer.has_version()


class TestOptedInCatalog:
    def test_a_2x_catalog_is_refused_and_named(self, writer: InventoryWriter, location: BlobLocation) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        stored = writer.storage.read(CATALOG_NAME)
        assert stored is not None
        opted_in = msgspec.structs.replace(
            decode_catalog(stored), format_version=OPTED_IN_CATALOG_FORMAT, history=CatalogHistory(since_seq=1)
        )
        location.ops.put(CATALOG_NAME, encode(opted_in))

        with pytest.raises(InventoryWriteRefusedError, match=OPTED_IN_CATALOG_FORMAT.replace(".", r"\.")):
            writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="refused")

        assert writer.storage.read(root_path(2)) is None
        assert writer.storage.read(leaf_ref(_leaf("b", 2))[1].leaf) is None


class TestSingleShot:
    def test_an_8_mb_root_is_one_put_with_the_create_headers(self, azurite: _stores.Running) -> None:
        with _stores.recording_proxy(azurite) as proxy:
            location = _stores.location(azurite, uuid.uuid4().hex, endpoint=proxy.endpoint)
            writer = InventoryWriter(_storage(location))
            writer.write_catalog_json()
            reason = "x" * (8 * 1024 * 1024)

            root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason=reason)

            stored = writer.storage.read(root_path(root.seq))
            assert stored is not None
            assert len(stored) > 8 * 1024 * 1024
            puts = proxy.puts(root_path(root.seq))
            assert len(puts) == 1
            assert puts[0].headers["if-none-match"] == "*"
            assert puts[0].headers["x-ms-blob-type"] == "BlockBlob"
            assert puts[0].size == len(stored)

    def test_a_big_leaf_is_one_put(self, azurite: _stores.Running) -> None:
        with _stores.recording_proxy(azurite) as proxy:
            location = _stores.location(azurite, uuid.uuid4().hex, endpoint=proxy.endpoint)
            storage = _storage(location)
            data = b"x" * (_FIVE_MIB + 1)

            storage.write_leaf("inventory/leaves/big.json", data)

            puts = proxy.puts("big.json")
            assert len(puts) == 1
            assert puts[0].headers["x-ms-blob-type"] == "BlockBlob"
            assert "if-none-match" not in puts[0].headers
            assert storage.read("inventory/leaves/big.json") == data

    def test_every_commit_write_is_a_put_blob(self, azurite: _stores.Running) -> None:
        with _stores.recording_proxy(azurite) as proxy:
            location = _stores.location(azurite, uuid.uuid4().hex, endpoint=proxy.endpoint)
            writer = InventoryWriter(_storage(location))
            writer.write_catalog_json()
            writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

            writes = [r for r in proxy.requests if r.method == "PUT"]
            assert len(writes) == 4  # catalog.json, the leaf, the root and CURRENT
            assert all(r.headers.get("x-ms-blob-type") == "BlockBlob" for r in writes)
            assert not [r for r in writes if "comp=" in r.path]


class TestConditions:
    def test_a_second_create_raises_and_the_object_keeps_its_bytes(self, location: BlobLocation) -> None:
        location.ops.put("inventory/x.json", b"first", mode="create")
        with pytest.raises(AlreadyExistsError):
            location.ops.put("inventory/x.json", b"second", mode="create")
        got = location.ops.read("inventory/x.json")
        assert got is not None
        assert got[0] == b"first"

    def test_a_stale_e_tag_raises_on_current(self, writer: InventoryWriter, location: BlobLocation) -> None:
        writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        got = location.ops.read(CURRENT_NAME)
        assert got is not None
        raw, e_tag = got
        assert e_tag is not None
        location.ops.put(CURRENT_NAME, raw, mode={"e_tag": e_tag})

        with pytest.raises(PreconditionError):
            location.ops.put(CURRENT_NAME, raw, mode={"e_tag": e_tag})

    def test_current_moves_only_forward(self, writer: InventoryWriter) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        second = writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="second")
        stored = writer.storage.read(root_path(1))
        assert stored is not None

        writer.storage.advance_current(first, stored)

        held = writer.storage.read(CURRENT_NAME)
        assert held is not None
        assert decode_current(held).seq == second.seq


class TestStorageClock:
    def test_the_real_head_gives_a_date_within_the_bound(self, location: BlobLocation) -> None:
        storage = BlobInventoryStorage(location, actor="tests")
        stamp = storage.now()
        difference = abs(datetime.now(UTC) - stamp)
        assert difference <= MAX_CLOCK_SKEW, f"Azurite's Date is {difference} from this machine's clock"

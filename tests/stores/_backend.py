"""sdmxlib's conformance suite over `BlobInventoryStorage`.

`BlobBackend` is `tests/test_inventory_conformance.py`'s `WriterBackend` over a
blob location, so the local adapter stays untouched. The fault and restore
hooks live here only, and write through `_ObjectOps`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import msgspec
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    EventKind,
    FullInventory,
    InventoryError,
    Leaf,
    Root,
    Tombstone,
    decode_catalog,
    decode_root,
    encode,
    leaf_ref,
    root_path,
    sha256_hex,
)
from sdmxlib.inventory.conformance import ConflictError, Reader, Snapshot

from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryConflictError, InventoryWriter


def _now() -> datetime:
    """This machine's clock, so a lagging container VM fails no commit; one test runs the real ``HEAD``."""
    return datetime.now(UTC)


def _snapshot(full: FullInventory) -> Snapshot:
    return Snapshot(
        seq=full.seq,
        restore_epoch=full.restore_epoch,
        embargo=full.embargo,
        leaves=dict(full.leaves()),
        retired=full.retired,
    )


class _Reader:
    def __init__(self, storage: BlobInventoryStorage) -> None:
        self._full = FullInventory(storage.read)

    def refresh(self) -> Snapshot:
        return _snapshot(self._full.refresh())


class BlobBackend:
    """The conformance suite's view of `InventoryWriter` over object storage."""

    #: statcatlib's writer takes no catalog lock yet (GH-125 part 2).
    stage_2 = False

    def __init__(self, location: BlobLocation) -> None:
        self.ops = location.ops
        self.storage = BlobInventoryStorage(location, actor="conformance", clock=_now)
        self.writer = InventoryWriter(self.storage)
        self.writer.write_catalog_json()

    def head(self) -> Snapshot | None:
        try:
            full = self.writer.full()
        except InventoryError:
            return None
        return None if full is None else _snapshot(full)

    def _stored(self, path: str) -> bytes:
        stored = self.storage.read(path)
        assert stored is not None, path
        return stored

    def _root(self, seq: int) -> Root:
        return decode_root(self._stored(root_path(seq)))

    def commit(
        self,
        base: Snapshot | None,
        leaves: Mapping[str, Leaf],
        *,
        retired: Sequence[Tombstone] | None = None,
        embargo: bool | None = None,
        kind: EventKind = "seal",
    ) -> Snapshot:
        return self._try_commit(base, leaves, retired=retired, embargo=embargo, kind=kind)

    def commit_at(
        self,
        base: Snapshot | None,
        leaves: Mapping[str, Leaf],
        *,
        now: datetime,
        retired: Sequence[Tombstone] | None = None,
        kind: EventKind = "seal",
        built_from: int | None = None,
    ) -> Snapshot:
        return self._try_commit(base, leaves, retired=retired, kind=kind, now=now, built_from=built_from)

    def _try_commit(
        self,
        base: Snapshot | None,
        leaves: Mapping[str, Leaf],
        *,
        retired: Sequence[Tombstone] | None = None,
        embargo: bool | None = None,
        kind: EventKind = "seal",
        now: datetime | None = None,
        built_from: int | None = None,
    ) -> Snapshot:
        try:
            self.writer.try_commit(
                None if base is None else self._root(base.seq),
                leaves,
                retired=retired,
                embargo=embargo,
                built_from=built_from,
                now=now,
                kind=kind,
                reason="conformance",
            )
        except InventoryConflictError as exc:
            raise ConflictError(str(exc)) from exc
        head = self.head()
        assert head is not None
        return head

    def fetch(self, path: str) -> bytes | None:
        return self.storage.read(path)

    def put(self, path: str, data: bytes) -> None:
        self.ops.put(path, data)

    def commit_interrupted(self, base: Snapshot | None, leaves: Mapping[str, Leaf]) -> None:
        """Write the leaves alone, as a writer killed between its leaf and its root leaves them."""
        del base
        for leaf in leaves.values():
            data, ref = leaf_ref(leaf)
            self.storage.write_leaf(ref.leaf, data)

    def reader(self) -> Reader:
        return _Reader(self.storage)

    def _set_current(self, seq: int, epoch: int) -> None:
        sha = sha256_hex(self._stored(root_path(seq)))
        self.ops.put(CURRENT_NAME, encode(Current(seq=seq, root_sha256=sha, restore_epoch=epoch)))

    def hint_behind(self) -> None:
        head = self.head()
        if head is None or head.seq < 2:
            return
        self._set_current(head.seq - 1, head.restore_epoch)

    def restore_to(self, seq: int) -> None:
        later = seq + 1
        while self.ops.head(root_path(later)) is not None:
            self.ops.delete(root_path(later))
            later += 1
        self._set_current(seq, self._root(seq).restore_epoch)

    def restore_with_new_epoch(self, seq: int) -> None:
        """Lose the versions after *seq*, bump ``catalog.json``'s epoch, and record the restore."""
        self.restore_to(seq)
        catalog = decode_catalog(self._stored(CATALOG_NAME))
        bumped = msgspec.structs.replace(catalog, restore_epoch=catalog.restore_epoch + 1)
        self.ops.put(CATALOG_NAME, encode(bumped))
        self.writer.try_commit(self._root(seq), {}, kind="restore", reason="conformance restore")

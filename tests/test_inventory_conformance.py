"""statcatlib's Inventory writer passes sdmxlib's conformance suite (spec §6.6, RFC decision 31).

The suite drives a `Backend`. This adapter maps it onto `InventoryWriter`:
``commit`` is `InventoryWriter.try_commit`, and its `InventoryConflictError` is
the suite's `ConflictError`. The fault and restore hooks live here only: they
do what a killed writer, a lagging ``CURRENT`` and a storage restore leave on
disk, which no statcatlib code does.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import msgspec
import pytest
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    Event,
    EventKind,
    FullInventory,
    InventoryError,
    Leaf,
    Root,
    Tombstone,
    decode_catalog,
    decode_root,
    encode,
    local_fetch,
    next_root,
    root_path,
    sha256_hex,
)
from sdmxlib.inventory.conformance import CHECKS, Backend, ConflictError, Reader, Snapshot, run

from statcatlib.store.inventory import InventoryConflictError, InventoryWriter, producer

pytestmark = pytest.mark.integration


def _snapshot(full: FullInventory) -> Snapshot:
    return Snapshot(
        seq=full.seq,
        restore_epoch=full.restore_epoch,
        embargo=full.embargo,
        leaves=dict(full.leaves()),
        retired=full.retired,
    )


class _Reader:
    def __init__(self, root: Path) -> None:
        self._full = FullInventory(local_fetch(root))

    def refresh(self) -> Snapshot:
        return _snapshot(self._full.refresh())


class WriterBackend:
    """The conformance suite's view of `InventoryWriter` over a local directory."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.root = root
        self.writer = InventoryWriter(root)
        self.writer.write_catalog_json()

    def head(self) -> Snapshot | None:
        try:
            full = self.writer.full()
        except InventoryError:
            return None
        return None if full is None else _snapshot(full)

    def _root(self, seq: int) -> Root:
        return decode_root((self.root / root_path(seq)).read_bytes())

    def commit(
        self,
        base: Snapshot | None,
        leaves: Mapping[str, Leaf],
        *,
        retired: Sequence[Tombstone] | None = None,
        embargo: bool | None = None,
        kind: EventKind = "seal",
    ) -> Snapshot:
        try:
            self.writer.try_commit(
                None if base is None else self._root(base.seq),
                leaves,
                retired=retired,
                embargo=embargo,
                kind=kind,
                reason="conformance",
            )
        except InventoryConflictError as exc:
            raise ConflictError(str(exc)) from exc
        head = self.head()
        assert head is not None
        return head

    def commit_interrupted(self, base: Snapshot | None, leaves: Mapping[str, Leaf]) -> None:
        """Stage half of the next root, as a writer killed before its link leaves it."""
        del leaves
        previous = None if base is None else self._root(base.seq)
        root = next_root(
            previous,
            products=dict(previous.products) if previous is not None else {},
            event=Event(actor="killed", reason="conformance", kind="seal"),
            producer=producer(),
            now=datetime.now(UTC),
        )
        data = encode(root)
        target = self.root / root_path(root.seq)
        staged = target.with_name(f".{target.name}.{os.getpid()}.killed.tmp")
        staged.write_bytes(data[: len(data) // 2])

    def reader(self) -> Reader:
        return _Reader(self.root)

    def _set_current(self, seq: int, epoch: int) -> None:
        sha = sha256_hex((self.root / root_path(seq)).read_bytes())
        (self.root / CURRENT_NAME).write_bytes(encode(Current(seq=seq, root_sha256=sha, restore_epoch=epoch)))

    def hint_behind(self) -> None:
        head = self.head()
        if head is None or head.seq < 2:
            return
        self._set_current(head.seq - 1, head.restore_epoch)

    def restore_to(self, seq: int) -> None:
        later = seq + 1
        while (self.root / root_path(later)).exists():
            (self.root / root_path(later)).unlink()
            later += 1
        self._set_current(seq, self._root(seq).restore_epoch)

    def restore_with_new_epoch(self, seq: int) -> None:
        """Lose the versions after *seq*, bump ``catalog.json``'s epoch, and record the restore."""
        self.restore_to(seq)
        catalog = decode_catalog((self.root / CATALOG_NAME).read_bytes())
        bumped = msgspec.structs.replace(catalog, restore_epoch=catalog.restore_epoch + 1)
        (self.root / CATALOG_NAME).write_bytes(encode(bumped))
        self.writer.try_commit(self._root(seq), {}, kind="restore", reason="conformance restore")


@pytest.mark.parametrize("check", CHECKS, ids=[c.__name__.removeprefix("check_") for c in CHECKS])
def test_the_writer_conforms(check: Callable[[Callable[[], Backend]], None], tmp_path: Path) -> None:
    counter = itertools.count()
    check(lambda: WriterBackend(tmp_path / f"catalog-{next(counter)}"))


def test_run_passes_every_check(tmp_path: Path) -> None:
    counter = itertools.count()
    assert run(lambda: WriterBackend(tmp_path / f"c{next(counter)}")) == [c.__name__ for c in CHECKS]

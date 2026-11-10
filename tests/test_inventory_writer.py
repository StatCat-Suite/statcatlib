"""The Inventory writer: exclusive create, ``CURRENT``, and what each commit writes (RFC D8, D11, §4).

`InventoryWriter` commits Inventory versions on local disk. These tests drive
it with sdmxlib's pure rules (`sdmxlib.inventory.record`) and read it back
with sdmxlib's reader, so each document it writes is checked against the
format sdmxlib owns.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sdmxlib.storage
from sdmxlib.edition import ManifestError
from sdmxlib.inventory import (
    CURRENT_NAME,
    Current,
    FullInventory,
    InventoryError,
    Leaf,
    PartialRecord,
    Root,
    decode_current,
    decode_leaf,
    decode_root,
    encode,
    local_fetch,
    record,
    root_path,
    schema,
    sha256_hex,
)
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.store import inventory as inventory_module
from statcatlib.store.inventory import (
    MAX_RACES,
    Change,
    InventoryConflictError,
    InventoryContentionError,
    InventoryWriter,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _edition(n: int) -> str:
    return f"01a18e00-0000-7000-8000-{n:012d}"


def _leaf(leaf: Leaf | None, product: str, n: int, previous: str | None = None) -> Leaf:
    return record(
        leaf,
        product_id=product,
        kind="data",
        lifecycle="revisable",
        edition_id=_edition(n),
        previous=previous,
        manifest_sha256="0" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=False,
        now=_NOW,
    )


def _nothing(*_args: str) -> dict[str, PartialRecord]:
    """An artefacts reader for leaves with no edition on disk: every edition holds nothing."""
    return {}


@pytest.fixture
def writer(tmp_path: Path) -> InventoryWriter:
    """A writer over a new catalog whose leaves name editions with no manifest on disk."""
    root = tmp_path / "cat"
    root.mkdir()
    w = InventoryWriter(root, artefacts=_nothing)
    w.write_catalog_json()
    return w


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class TestExclusiveCreate:
    def test_catalog_json_is_created_once(self, writer: InventoryWriter) -> None:
        with pytest.raises(FileExistsError):
            writer.write_catalog_json()

    def test_a_second_root_at_one_seq_loses(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        with pytest.raises(InventoryConflictError, match="version 1"):
            writer.try_commit(None, {"b": _leaf(None, "b", 2)}, kind="seal", reason="stale")
        head = writer.head()
        assert head is not None
        assert list(head.products) == ["a"]

    def test_no_staged_file_is_left(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        assert not [name for name in _files(writer.root) if name.endswith(".tmp")]

    def test_each_document_is_created_by_sdmxlib(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """catalog.json, each leaf and each root go through `sdmxlib.storage.create_exclusive` (GH-115)."""
        created: list[str] = []
        real = inventory_module.create_exclusive

        def spy(path: Path, data: bytes) -> None:
            created.append(path.relative_to(writer.root).as_posix())
            real(path, data)

        monkeypatch.setattr(inventory_module, "create_exclusive", spy)
        (writer.root / "catalog.json").unlink()
        writer.write_catalog_json()
        root = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        assert real is sdmxlib.storage.create_exclusive
        assert created == ["catalog.json", root.products["a"].leaf, root_path(1)]

    def test_a_kill_before_the_link_leaves_no_root(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A writer killed between staging and linking leaves the previous head readable and writable."""
        first = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")

        class Killed(BaseException):
            pass

        real_link = os.link

        def killed(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            if Path(dst).name == Path(root_path(2)).name:
                raise Killed
            real_link(src, dst)

        monkeypatch.setattr(os, "link", killed)
        with pytest.raises(Killed):
            writer.try_commit(first, {"b": _leaf(None, "b", 2)}, kind="seal", reason="killed")
        monkeypatch.setattr(os, "link", real_link)
        assert not (writer.root / root_path(2)).exists()
        head = writer.head()
        assert head is not None
        assert head.seq == 1
        after = writer.try_commit(head, {"b": _leaf(None, "b", 2)}, kind="seal", reason="retry")
        assert after.seq == 2


class TestCurrent:
    def test_current_names_the_newest_root(self, writer: InventoryWriter) -> None:
        root = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        current = decode_current((writer.root / CURRENT_NAME).read_bytes())
        assert current.seq == root.seq == 1
        assert current.root_sha256 == sha256_hex((writer.root / root_path(1)).read_bytes())

    def test_current_only_moves_forward(self, writer: InventoryWriter) -> None:
        first = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        writer.try_commit(first, {"b": _leaf(None, "b", 2)}, kind="seal", reason="second")
        held = (writer.root / CURRENT_NAME).read_bytes()
        # A slow writer finishing version 1's CURRENT update after version 2's.
        writer._advance_current(first, (writer.root / root_path(1)).read_bytes())
        assert (writer.root / CURRENT_NAME).read_bytes() == held

    def test_a_lagging_current_is_found_by_probing(self, writer: InventoryWriter) -> None:
        """RFC §4: two writers can leave CURRENT one version behind; a reader probes forward."""
        first = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        stale = (writer.root / CURRENT_NAME).read_bytes()
        writer.try_commit(first, {"b": _leaf(None, "b", 2)}, kind="seal", reason="second")
        (writer.root / CURRENT_NAME).write_bytes(stale)
        full = writer.full()
        assert full is not None
        assert full.seq == 2
        assert full.products() == ["a", "b"]


class TestWrites:
    def test_one_product_change_writes_one_leaf_and_one_root(self, writer: InventoryWriter) -> None:
        """RFC D11: the other products' leaves are reused by hash."""
        first = writer.try_commit(
            None, {"a": _leaf(None, "a", 1), "b": _leaf(None, "b", 2)}, kind="seal", reason="first"
        )
        before = _files(writer.root)
        second = writer.try_commit(
            first, {"a": _leaf(_leaf(None, "a", 1), "a", 3, _edition(1))}, kind="seal", reason="second"
        )
        added = _files(writer.root) - before
        assert sorted(n.rsplit("/", 1)[0] for n in added) == ["inventory", "inventory/leaves"]
        assert second.products["b"] == first.products["b"]
        assert second.products["a"] != first.products["a"]

    def test_every_document_validates(self, writer: InventoryWriter) -> None:
        """RFC D8: every root and leaf written decodes and validates with sdmxlib."""
        first = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        writer.try_commit(first, {"a": _leaf(_leaf(None, "a", 1), "a", 2, _edition(1))}, kind="seal", reason="2")
        roots = sorted((writer.root / "inventory").glob("*.json"))
        leaves = sorted((writer.root / "inventory" / "leaves").glob("*.json"))
        assert len(roots) == 2
        assert len(leaves) == 2
        for path in roots:
            assert isinstance(decode_root(path.read_bytes()), Root)
        for path in leaves:
            assert sha256_hex(path.read_bytes()) == path.stem
            assert isinstance(decode_leaf(path.read_bytes()), Leaf)
        for name in ("root", "leaf", "catalog", "current"):
            assert json.loads(schema(name))  # sdmxlib ships each document's schema

    def test_the_event_names_the_actor_from_the_process(self, writer: InventoryWriter) -> None:
        root = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="because")
        assert root.event.reason == "because"
        assert root.event.kind == "seal"
        assert "@" in root.event.actor
        assert root.producer.name == "statcatlib"
        assert root.restore_epoch == 0
        assert root.embargo is False


class TestFormatVersions:
    def _rewrite_root(self, writer: InventoryWriter, **changes: object) -> None:
        path = writer.root / root_path(1)
        document = json.loads(path.read_bytes())
        document.update(changes)
        path.write_bytes(json.dumps(document).encode())
        current = decode_current((writer.root / CURRENT_NAME).read_bytes())
        (writer.root / CURRENT_NAME).write_bytes(
            encode(type(current)(seq=1, root_sha256=sha256_hex(path.read_bytes()), restore_epoch=0))
        )

    def test_a_1_1_root_with_unknown_fields_is_read(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        self._rewrite_root(writer, format_version="1.1", added_in_1_1={"x": 1})
        full = writer.full()
        assert full is not None
        assert full.products() == ["a"]

    def test_a_2_0_root_is_refused(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        self._rewrite_root(writer, format_version="2.0")
        with pytest.raises(InventoryError, match=r"format 2\.0"):
            FullInventory(local_fetch(writer.root)).refresh()


class TestCommit:
    def test_commit_reapplies_the_change_after_a_lost_race(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        heads: list[int | None] = []
        real = writer.try_commit
        raced = False

        def change(full: FullInventory | None) -> Change:
            nonlocal raced
            heads.append(None if full is None else full.seq)
            if not raced:
                raced = True
                # Another writer lands version 1 between the read and the create.
                real(None, {"other": _leaf(None, "other", 9)}, kind="seal", reason="other")
            return Change(leaves={"a": _leaf(None, "a", 1)})

        root = writer.commit(change, kind="seal", reason="mine")
        assert heads == [None, 1]
        assert root.seq == 2
        assert sorted(root.products) == ["a", "other"]

    def test_commit_gives_up_after_eight_lost_races(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def always_lose(*args: object, **kwargs: object) -> Root:
            nonlocal calls
            calls += 1
            raise InventoryConflictError

        monkeypatch.setattr(writer, "try_commit", always_lose)
        with pytest.raises(InventoryContentionError, match=f"{MAX_RACES} commits"):
            writer.commit(lambda _: Change(leaves={}), kind="seal", reason="x")
        assert calls == MAX_RACES == 8

    def test_an_incoming_entry_whose_manifest_is_missing_raises_manifest_error(self, tmp_path: Path) -> None:
        root = tmp_path / "cat"
        root.mkdir()
        writer = InventoryWriter(root)
        writer.write_catalog_json()

        with pytest.raises(ManifestError, match="manifest cannot be read"):
            writer.commit(lambda _: Change(leaves={"a": _leaf(None, "a", 1)}), kind="seal", reason="x")
        assert not writer.has_version(), "nothing is written"

    def test_a_change_with_no_incoming_entry_reads_no_manifest(self, tmp_path: Path) -> None:
        root = tmp_path / "cat"
        root.mkdir()
        writer = InventoryWriter(root)
        writer.write_catalog_json()

        root_written = writer.commit(lambda _: Change(leaves={}, embargo=True), kind="embargo-change", reason="x")
        assert root_written.embargo

    def test_the_actor_comes_from_the_os(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(inventory_module.getpass, "getuser", lambda: "ana")
        monkeypatch.setattr(inventory_module.socket, "gethostname", lambda: "laptop")
        assert inventory_module._actor() == "ana@laptop"


class TestNothingToWrite:
    def test_a_none_change_writes_nothing_and_returns_none(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        before = _files(writer.root)

        assert writer.commit(lambda _: None, kind="retire", reason="nothing retires") is None
        assert _files(writer.root) == before

    def test_a_none_change_before_the_first_version_writes_nothing(self, writer: InventoryWriter) -> None:
        assert writer.commit(lambda _: None, kind="release", reason="nothing is due") is None
        assert not writer.has_version()

    def test_a_change_that_has_nothing_left_after_a_lost_race_writes_nothing(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = writer.try_commit
        heads: list[int | None] = []

        def change(full: FullInventory | None) -> Change | None:
            heads.append(None if full is None else full.seq)
            if full is None:
                # Another writer records the same product first; the replanned change finds nothing to do.
                real(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="other")
                return Change(leaves={"a": _leaf(None, "a", 1)})
            return None

        assert writer.commit(change, kind="seal", reason="mine") is None
        assert heads == [None, 1]
        head = writer.head()
        assert head is not None
        assert head.event.reason == "other"


class TestChangeNow:
    def test_the_changes_time_is_the_roots(self, writer: InventoryWriter) -> None:
        when = datetime(2026, 11, 10, 13, 30, 5, 120000, tzinfo=UTC)

        root = writer.commit(lambda _: Change(leaves={"a": _leaf(None, "a", 1)}, now=when), kind="seal", reason="x")

        assert root.created_at == when
        assert decode_root((writer.root / root_path(1)).read_bytes()).created_at == when

    def test_without_one_the_storage_clock_is_read(
        self, writer: InventoryWriter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        when = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        monkeypatch.setattr(writer.storage, "now", lambda: when)

        root = writer.commit(lambda _: Change(leaves={"a": _leaf(None, "a", 1)}), kind="seal", reason="x")

        assert root.created_at == when


def _lag_current(writer: InventoryWriter, seq: int) -> None:
    """Point ``CURRENT`` back at version *seq*, as a writer that crashed after its root leaves it."""
    data = (writer.root / root_path(seq)).read_bytes()
    current = Current(seq=seq, root_sha256=sha256_hex(data), restore_epoch=0)
    (writer.root / CURRENT_NAME).write_bytes(encode(current))


class TestAdvanceCurrent:
    def test_it_moves_a_lagging_current_to_the_head(self, writer: InventoryWriter) -> None:
        one = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="1")
        writer.try_commit(one, {"b": _leaf(None, "b", 2)}, kind="seal", reason="2")
        _lag_current(writer, 1)

        assert writer.advance_current() == 2
        assert decode_current((writer.root / CURRENT_NAME).read_bytes()).seq == 2

    def test_a_current_current_is_left_alone(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="1")
        before = (writer.root / CURRENT_NAME).read_bytes()

        assert writer.advance_current() is None
        assert (writer.root / CURRENT_NAME).read_bytes() == before

    def test_a_missing_current_is_written(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="1")
        (writer.root / CURRENT_NAME).unlink()

        assert writer.advance_current() == 1
        assert decode_current((writer.root / CURRENT_NAME).read_bytes()).seq == 1

    def test_a_catalog_with_no_version_has_nothing_to_advance(self, writer: InventoryWriter) -> None:
        assert writer.advance_current() is None
        assert not (writer.root / CURRENT_NAME).exists()

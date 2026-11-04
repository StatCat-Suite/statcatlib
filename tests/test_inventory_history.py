"""The history fields every root carries (Inventory 1.2, GH-125 part 1).

Each root links the stored bytes of root ``seq - 1`` and lists its changes
against that root and its leaves. These tests drive `InventoryWriter` and the
seal, and check the documents with sdmxlib's `change_list`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import msgspec
import pytest
from msgspec import UnsetType
from sdmxlib.inventory import (
    CATALOG_NAME,
    OPTED_IN_CATALOG_FORMAT,
    CatalogHistory,
    ChangeItem,
    Event,
    FullInventory,
    Leaf,
    Root,
    change_list,
    decode_catalog,
    decode_leaf,
    decode_root,
    encode,
    leaf_ref,
    local_fetch,
    next_root,
    record,
    root_path,
    sha256_hex,
)
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
from statcatlib import CatalogAdmin
from statcatlib.store.inventory import Change, InventoryWriter, producer

pytestmark = pytest.mark.integration

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


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=sc.InternationalString(en=identifier), description=sc.InternationalString(en="d")
    )


def _seal(root: Path, product: str) -> sc.Edition:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(product).working_copy()
        wc.put([_dataset(f"{product}-1")])
        return wc.seal()


def _stored(root: Path, seq: int) -> tuple[Root, bytes]:
    raw = (root / root_path(seq)).read_bytes()
    return decode_root(raw), raw


def _leaves(root: Path, of: Root) -> dict[str, Leaf]:
    return {p: decode_leaf((root / ref.leaf).read_bytes()) for p, ref in of.products.items()}


def _changes(root: Root) -> tuple[ChangeItem, ...]:
    assert not isinstance(root.changes, UnsetType)
    return root.changes


def _assert_chain(root: Path, seq: int) -> Root:
    """Root *seq*, after checking it links root ``seq - 1``'s stored bytes and diffs against its leaves."""
    current, _ = _stored(root, seq)
    assert current.has_history
    base, raw = _stored(root, seq - 1)
    assert current.previous_root_sha256 == sha256_hex(raw)
    assert current.changes == change_list(
        base,
        _leaves(root, base),
        products=current.products,
        leaves=_leaves(root, current),
        retired=current.retired,
    )
    assert current.created_at > base.created_at
    return current


def _opt_in(root: Path) -> None:
    catalog = decode_catalog((root / CATALOG_NAME).read_bytes())
    opted_in = msgspec.structs.replace(
        catalog, format_version=OPTED_IN_CATALOG_FORMAT, history=CatalogHistory(since_seq=1)
    )
    (root / CATALOG_NAME).write_bytes(encode(opted_in))


@pytest.fixture
def writer(tmp_path: Path) -> InventoryWriter:
    made = InventoryWriter(tmp_path / "catalog")
    made.root.mkdir()
    made.write_catalog_json()
    return made


class TestEveryRoot:
    def test_the_first_root_has_no_previous_and_lists_its_product(self, writer: InventoryWriter) -> None:
        root = writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        assert root.has_history
        assert root.previous_root_sha256 is None
        (item,) = _changes(root)
        assert item.product_id == "a"
        assert item.leaf_sha256.from_ is None

    def test_a_root_with_no_product_change_lists_no_changes(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        root = writer.commit(lambda _: Change(leaves={}, embargo=True), kind="embargo-change", reason="on")
        assert root.changes == ()
        _assert_chain(writer.root, 2)

    def test_a_seal_writes_both_fields_and_an_admin_change_too(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        _seal(root, "a")
        seq = CatalogAdmin.open(root).rollback("a", reason="the new figures are wrong")
        _assert_chain(root, 2)
        current = _assert_chain(root, seq)
        assert sorted((e.from_, e.to) for item in _changes(current) for e in item.entries) == [
            ("live", "withdrawn"),
            ("superseded", "live"),
        ]

    def test_a_catalog_written_before_the_history_fields_continues(self, writer: InventoryWriter) -> None:
        """A 1.0 root and leaf with no history fields are a base like any other, and the chain starts after them."""
        leaf = msgspec.structs.replace(_leaf(None, "a", 1), format_version="1.0")
        data, ref = leaf_ref(leaf)
        (writer.root / ref.leaf).parent.mkdir(parents=True)
        (writer.root / ref.leaf).write_bytes(data)
        old = next_root(
            None,
            products={"a": ref},
            event=Event(actor="old", reason="old", kind="seal"),
            producer=producer(),
            now=_NOW,
        )
        old = msgspec.structs.replace(old, format_version="1.0")
        assert not old.has_history
        raw = encode(old)
        (writer.root / root_path(1)).parent.mkdir(parents=True, exist_ok=True)
        (writer.root / root_path(1)).write_bytes(raw)
        full = writer.full()
        assert full is not None
        root = writer.try_commit(
            full.root, {"a": _leaf(leaf, "a", 2, _edition(1))}, kind="seal", reason="after the upgrade"
        )
        assert root.previous_root_sha256 == sha256_hex(raw)
        assert [c.product_id for c in _changes(root)] == ["a"]


class TestCorrection:
    def test_a_correction_links_and_diffs_against_the_version_before_it(self, tmp_path: Path) -> None:
        writer = InventoryWriter(tmp_path, artefacts=lambda _product, _edition, _sha: {})
        writer.write_catalog_json()
        first = _leaf(None, "a", 1)
        writer.try_commit(None, {"a": first}, kind="seal", reason="1")
        head = writer.head()
        writer.try_commit(head, {"a": _leaf(first, "a", 2, _edition(1))}, kind="seal", reason="2")
        writer.commit(lambda _: Change(leaves={"b": _leaf(None, "b", 3)}), kind="seal", reason="3")

        root = writer.commit(lambda _: Change(leaves={"a": first}, built_from=1), kind="correct", reason="4")

        assert root.seq == 4
        assert root.previous_inventory == 1
        current = _assert_chain(writer.root, 4)
        (item,) = _changes(current)
        third, _ = _stored(writer.root, 3)
        assert item.product_id == "a"
        assert item.leaf_sha256.from_ == third.products["a"].leaf_sha256
        assert {e.edition_id: (e.from_, e.to) for e in item.entries} == {
            _edition(1): ("superseded", "live"),
            _edition(2): ("live", None),
        }


def _race_once(monkeypatch: pytest.MonkeyPatch, writer: InventoryWriter, competing: Callable[[], object]) -> None:
    """Run *competing* inside *writer*'s first `try_commit`, before it writes, so it wins the race."""
    original = writer.try_commit
    calls: list[int] = []

    def raced(*args: Any, **kwargs: Any) -> Root:
        if not calls:
            calls.append(1)
            competing()
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "try_commit", raced)


class TestLostRace:
    def test_a_seal_that_loses_a_race_recomputes_both_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("b").working_copy()
            wc.put([_dataset("b-1")])
            _race_once(monkeypatch, wc._writer, lambda: _seal(root, "c"))
            wc.seal()

        winner = _assert_chain(root, 2)
        loser = _assert_chain(root, 3)
        assert [c.product_id for c in _changes(winner)] == ["c"]
        assert [c.product_id for c in _changes(loser)] == ["b"]
        full = FullInventory(local_fetch(root)).refresh()
        assert full.products() == ["a", "b", "c"]


class TestFastBase:
    def _fast_catalog(self, root: Path) -> datetime:
        """A catalog whose newest version was created 10 minutes ahead of this machine's clock."""
        _seal(root, "a")
        ahead = datetime.now(UTC) + timedelta(minutes=10)
        writer = InventoryWriter(root)
        writer.try_commit(writer.head(), {}, now=ahead, kind="correct", reason="a fast clock wrote this")
        return ahead

    def test_a_commit_names_the_base_version_time(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        ahead = self._fast_catalog(root)
        writer = InventoryWriter(root, artefacts=lambda _product, _edition, _sha: {})
        before = sorted(p.name for p in (root / "inventory").rglob("*.json"))
        with pytest.raises(sc.InventoryWriteRefusedError, match=ahead.isoformat()[:19]):
            writer.commit(lambda _: Change(leaves={"b": _leaf(None, "b", 2)}), kind="seal", reason="refused")
        assert sorted(p.name for p in (root / "inventory").rglob("*.json")) == before, "it wrote nothing"

    def test_a_clock_behind_within_the_limit_steps_past_the_base(self, tmp_path: Path) -> None:
        writer = InventoryWriter(tmp_path, artefacts=lambda _product, _edition, _sha: {})
        writer.write_catalog_json()
        ahead = datetime.now(UTC) + timedelta(minutes=3)
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, now=ahead, kind="seal", reason="fast")
        root = writer.commit(lambda _: Change(leaves={"b": _leaf(None, "b", 2)}), kind="seal", reason="slow")
        assert root.created_at == ahead + timedelta(milliseconds=1)

    def test_a_seal_is_refused_before_it_writes_and_stays_open(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("a").working_copy()
            wc.put([_dataset("a-2")])
            ahead = datetime.now(UTC) + timedelta(minutes=10)
            writer = InventoryWriter(root)
            writer.try_commit(writer.head(), {}, now=ahead, kind="correct", reason="a fast clock wrote this")
            with pytest.raises(sc.InventoryWriteRefusedError, match=ahead.isoformat()[:19]):
                wc.seal()
            assert not (root / "products" / "a" / wc.edition_id / "manifest.json").exists()
            wc.discard()

    def test_a_batch_reports_the_refusal(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("b").working_copy()
            wc.put([_dataset("b-1")])
            ahead = datetime.now(UTC) + timedelta(minutes=10)
            writer = InventoryWriter(root)
            writer.try_commit(writer.head(), {}, now=ahead, kind="correct", reason="a fast clock wrote this")
            outcomes = cat.seal([wc])
            assert isinstance(outcomes.refused["b"], sc.InventoryWriteRefusedError)
            wc.discard()


class TestOptedInCatalog:
    def test_a_commit_is_refused(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf(None, "a", 1)}, kind="seal", reason="first")
        _opt_in(writer.root)
        with pytest.raises(sc.InventoryWriteRefusedError, match="opted in to expiry"):
            writer.try_commit(writer.head(), {"b": _leaf(None, "b", 2)}, kind="seal", reason="refused")

    def test_a_reader_still_opens_it(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        _opt_in(root)
        (root / "inventory" / "HISTORY").write_text("")
        with sc.Catalog.open(root) as cat:
            assert cat.products() == ["a"]
        assert CatalogAdmin.open(root).products() == ["a"]

    def test_a_seal_is_refused_before_it_writes(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        _seal(root, "a")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("a").working_copy()
            wc.put([_dataset("a-2")])
            _opt_in(root)
            with pytest.raises(sc.InventoryWriteRefusedError, match="opted in to expiry"):
                wc.seal()
            assert not (root / "products" / "a" / wc.edition_id / "manifest.json").exists()
            wc.discard()

    def test_a_refusal_at_the_record_keeps_the_journal_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opted in after the pre-check: the edition stays journalled for a later working copy."""
        root = tmp_path / "catalog"
        _seal(root, "a")
        document = (root / CATALOG_NAME).read_bytes()
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("a").working_copy()
            wc.put([_dataset("a-2")])
            edition_id = wc.edition_id
            real = wc._seal_edition

            def opting_in() -> Any:
                sealed = real()
                _opt_in(root)
                return sealed

            monkeypatch.setattr(wc, "_seal_edition", opting_in)
            with pytest.raises(sc.InventoryWriteRefusedError, match="stays unrecorded"):
                wc.seal()
        assert (root / "products" / "a" / "journal" / f"{edition_id}.json").is_file()
        (root / CATALOG_NAME).write_bytes(document)
        _seal(root, "b")
        full = FullInventory(local_fetch(root)).refresh()
        assert edition_id in {e.edition_id for e in full.entries("a")}, "the repair records it once the catalog allows"

"""`inventory_corpus` over `MemoryStore`: entry states, sizes and determinism by seed (GH-98, Phase 4 Part 4)."""

from __future__ import annotations

import importlib
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import obstore
import pytest
from obstore.store import MemoryStore
from sdmxlib.inventory import CURRENT_NAME, FullInventory, LiveInventory, local_fetch
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryWriter
from statcatlib.testing import CorpusGroup, corpus_embargoed, corpus_product_id, inventory_corpus

pytestmark = pytest.mark.unit

_UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-8[0-9a-f]{3}-[0-9a-f]{12}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _writer(store: MemoryStore) -> InventoryWriter:
    return InventoryWriter(BlobInventoryStorage(BlobLocation("memory://corpus/", store), actor="test"))


def _fetch(store: MemoryStore):
    def fetch(path: str) -> bytes | None:
        try:
            return bytes(obstore.get(store, path).bytes())
        except FileNotFoundError:
            return None

    return fetch


def _objects(store: MemoryStore) -> dict[str, bytes]:
    paths = [meta["path"] for chunk in obstore.list(store) for meta in chunk]
    return {path: bytes(obstore.get(store, path).bytes()) for path in paths}


def _built(products: int, **kwargs) -> MemoryStore:
    store = MemoryStore()
    inventory_corpus(_writer(store), products, **kwargs)
    return store


def _leaf_sizes(objects: dict[str, bytes]) -> list[int]:
    return [len(raw) for path, raw in objects.items() if path.startswith("inventory/leaves/")]


def test_the_defaults_write_14_roots_and_7000_leaves_with_one_put_each() -> None:
    objects = _objects(_built(7000))

    roots = [p for p in objects if p.startswith("inventory/") and not p.startswith("inventory/leaves/")]
    assert len(roots) == 14
    assert len(_leaf_sizes(objects)) == 7000
    assert {"catalog.json", CURRENT_NAME} <= objects.keys()
    assert len(objects) == 7016


def test_a_commit_per_batch_rounds_up_the_last_commit() -> None:
    root = inventory_corpus(_writer(MemoryStore()), 25, batch=10)

    assert root.seq == 3
    assert len(root.products) == 25


def test_the_last_root_records_its_commits_change_list() -> None:
    root = inventory_corpus(_writer(MemoryStore()), 25, batch=10)

    assert isinstance(root.changes, tuple)
    assert len(root.changes) == 5
    assert root.changes[-1].product_id == corpus_product_id(CorpusGroup(25), 24)


def test_every_entry_holds_the_listed_values() -> None:
    store = _built(40)

    full = FullInventory(_fetch(store)).refresh()

    assert full.products() == [corpus_product_id(CorpusGroup(40), n) for n in range(40)]
    for product_id, leaf in full.leaves().items():
        assert (leaf.kind, leaf.lifecycle, len(leaf.editions)) == ("data", "revisable", 1)
        entry = leaf.editions[0]
        assert _UUID7.fullmatch(entry.edition_id)
        assert _HEX64.fullmatch(entry.manifest_sha256)
        assert entry.previous is None
        assert entry.schema_version == SCHEMA_VERSION
        assert entry.pull_bytes == 13_131_776
        assert isinstance(entry.dataflows, tuple)
        assert len(entry.dataflows) == 1
        assert product_id in entry.dataflows[0]
        assert entry.artefacts is None


def test_the_share_corpus_embargoed_picks_is_embargoed_with_a_future_release() -> None:
    store = _built(200)

    full = FullInventory(_fetch(store)).refresh()
    states = {p: leaf.editions[0] for p, leaf in full.leaves().items()}

    embargoed = {p for p in states if corpus_embargoed(p)}
    assert 0 < len(embargoed) < 200
    assert {p for p, e in states.items() if e.state == "embargoed"} == embargoed
    assert {p for p, e in states.items() if e.state == "live"} == set(states) - embargoed
    for product_id, entry in states.items():
        if product_id in embargoed:
            assert entry.release_at is not None
            assert entry.release_at > datetime.now(UTC)
        else:
            assert entry.release_at is None
    assert full.embargo is False, "each entry takes its state from the corpus; the root's setting stays off"


def test_live_inventory_counts_the_live_entries_at_sdmxlibs_schema() -> None:
    store = _built(200)

    live = LiveInventory(_fetch(store)).refresh().editions()

    assert set(live) == {p for p in FullInventory(_fetch(store)).refresh().products() if not corpus_embargoed(p)}
    edition = next(iter(live.values()))
    assert edition.pull_bytes == 13_131_776
    assert edition.dataflows is not None


def test_the_embargoed_share_runs_from_none_to_all() -> None:
    assert len(LiveInventory(_fetch(_built(30, embargoed=0))).refresh().editions()) == 30
    assert len(LiveInventory(_fetch(_built(30, embargoed=1))).refresh().editions()) == 0


def test_artefacts_give_ten_records_of_64_hex_fingerprints() -> None:
    store = _built(5, artefacts=True)

    for leaf in FullInventory(_fetch(store)).refresh().leaves().values():
        artefacts = leaf.editions[0].artefacts
        assert artefacts is not None
        assert len(artefacts) == 10
        assert all(_HEX64.fullmatch(r.fingerprint) for r in artefacts.values())


def test_artefact_records_grow_a_leaf_to_the_phase_5_size() -> None:
    plain = _leaf_sizes(_objects(_built(100)))
    with_artefacts = _leaf_sizes(_objects(_built(100, artefacts=True)))

    assert 500 < sum(plain) / 100 < 800
    assert 2300 < sum(with_artefacts) / 100 < 2800


def test_a_seed_writes_the_same_bytes() -> None:
    assert _objects(_built(30, batch=7, seed=3)) == _objects(_built(30, batch=7, seed=3))
    assert _objects(_built(30, batch=7, seed=3, artefacts=True)) == _objects(
        _built(30, batch=7, seed=3, artefacts=True)
    )


def test_another_seed_writes_other_bytes() -> None:
    first, second = _objects(_built(30, seed=1)), _objects(_built(30, seed=2))

    assert first["catalog.json"] != second["catalog.json"]
    assert {p for p in first if "leaves/" in p}.isdisjoint(p for p in second if "leaves/" in p)


def test_commits_step_by_one_millisecond_from_a_fixed_past_time() -> None:
    store = _built(25, batch=10)

    root = FullInventory(_fetch(store)).refresh().root

    assert root.created_at == datetime(2026, 1, 1, 0, 0, 0, 2000, tzinfo=UTC)


def test_the_corpus_writes_through_a_local_writer_too(tmp_path: Path) -> None:
    root = inventory_corpus(InventoryWriter(tmp_path), 5, batch=2)

    assert root.seq == 3
    assert len(FullInventory(local_fetch(tmp_path)).refresh().products()) == 5


def test_a_catalog_with_a_catalog_json_is_refused() -> None:
    store = _built(1)

    with pytest.raises(FileExistsError, match=r"catalog\.json"):
        inventory_corpus(_writer(store), 1)


@pytest.mark.parametrize(
    ("products", "batch", "embargoed", "match"),
    [
        (0, 500, 0.1, "at least 1 product"),
        (1, 0, 0.1, "at least 1 product"),
        (1, 500, 1.5, "from 0 to 1"),
        (1, 500, -0.1, "from 0 to 1"),
    ],
)
def test_bad_arguments_raise_before_anything_is_written(
    products: int, batch: int, embargoed: float, match: str
) -> None:
    store = MemoryStore()

    with pytest.raises(ValueError, match=match):
        inventory_corpus(_writer(store), products, batch=batch, embargoed=embargoed)

    assert _objects(store) == {}


def test_the_testing_module_imports_without_the_blob_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in sys.modules if n == "statcatlib" or n.startswith("statcatlib.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "obstore", None)
    monkeypatch.setitem(sys.modules, "obstore.exceptions", None)

    assert importlib.import_module("statcatlib.testing").inventory_corpus is not None

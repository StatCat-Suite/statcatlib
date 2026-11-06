"""The Inventory read's GETs at 50 products: only `CURRENT`, roots and leaves (GH-98 part 5, Phase 4 Part 4).

`bench/inventory_read.py` measures 7,000 products by hand, outside `just ci`.
These tests run its functions at 50, so a change to the read's cost or to the
script fails the gate. The corpus has 5 roots, and a cold read of all of it
makes 53 GETs: `CURRENT`, the newest root, its absent successor, and 50 leaves.
"""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

import attrs
import pytest
from sdmxlib.inventory import CURRENT_NAME, FullInventory, LiveInventory, root_path

from statcatlib.store.blob import BlobInventoryStorage, _ObjectOps
from statcatlib.store.inventory import InventoryWriter
from statcatlib.testing import CorpusGroup, corpus_embargoed, corpus_product_id, inventory_corpus
from tests.stores import _stores

pytestmark = pytest.mark.stores

_PRODUCTS = 50
_BATCH = 10
_ROOTS = 5
_COLD = _PRODUCTS + 3
_VIEWS = (LiveInventory, FullInventory)


def _bench(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    return importlib.import_module("bench.inventory_read")


def _ids() -> list[str]:
    return [corpus_product_id(CorpusGroup(_PRODUCTS), n) for n in range(_PRODUCTS)]


def _built(azurite: _stores.Running, *, endpoint: str | None = None):
    location = _stores.location(azurite, uuid.uuid4().hex, endpoint=endpoint)
    writer = InventoryWriter(BlobInventoryStorage(location, actor="test"))
    inventory_corpus(writer, _PRODUCTS, batch=_BATCH)
    return location


@pytest.mark.parametrize("view", _VIEWS)
def test_a_cold_read_touches_only_current_the_newest_root_its_probe_and_each_leaf(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch, view: type[LiveInventory | FullInventory]
) -> None:
    bench = _bench(monkeypatch)
    location = _built(azurite)
    fetch = bench.RecordingFetch(_ObjectOps(location.store))

    bench.cold_sequential(view, fetch)

    paths = [path for path, _ in fetch.gets]
    assert len(paths) == _COLD
    assert paths[:3] == [CURRENT_NAME, root_path(_ROOTS), root_path(_ROOTS + 1)]
    assert all(path.startswith("inventory/leaves/") for path in paths[3:])
    assert len(set(paths[3:])) == _PRODUCTS
    assert [path for path, size in fetch.gets if size is None] == [root_path(_ROOTS + 1)]


def test_the_requests_on_the_wire_are_the_recording_fetchs(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = _bench(monkeypatch)
    with _stores.recording_proxy(azurite) as proxy:
        location = _built(azurite, endpoint=proxy.endpoint)
        proxy.requests.clear()
        fetch = bench.RecordingFetch(_ObjectOps(location.store))

        bench.cold_sequential(LiveInventory, fetch)

        assert [request.method for request in proxy.requests] == ["GET"] * _COLD
        prefix = f"/{_stores.ACCOUNT}/{_stores.CONTAINER}/{location.uri.split('/')[3]}/"
        assert [request.path.removeprefix(prefix) for request in proxy.requests] == [p for p, _ in fetch.gets]


@pytest.mark.parametrize("view", _VIEWS)
def test_the_prefilled_read_makes_the_same_gets_as_the_sequential_one(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch, view: type[LiveInventory | FullInventory]
) -> None:
    bench = _bench(monkeypatch)
    location = _built(azurite)
    fetch = bench.RecordingFetch(_ObjectOps(location.store))

    served_s = bench.cold_prefilled(view, fetch, threads=8)

    paths = [path for path, _ in fetch.gets]
    assert served_s > 0
    assert len(paths) == _COLD
    assert paths[:2] == [CURRENT_NAME, root_path(_ROOTS)]
    assert paths[-1] == root_path(_ROOTS + 1), "the reader's one GET is its probe"


@pytest.mark.parametrize("view", _VIEWS)
def test_a_cold_read_of_one_product_makes_four_gets(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch, view: type[LiveInventory | FullInventory]
) -> None:
    bench = _bench(monkeypatch)
    location = _built(azurite)
    fetch = bench.RecordingFetch(_ObjectOps(location.store))
    product_id = next(p for p in _ids() if not corpus_embargoed(p))

    bench.cold_one(view, fetch, product_id)

    paths = [path for path, _ in fetch.gets]
    assert paths[:3] == [CURRENT_NAME, root_path(_ROOTS), root_path(_ROOTS + 1)]
    assert len(paths) == 4
    assert paths[3].startswith("inventory/leaves/")


def test_a_warm_reader_reads_only_each_version_and_its_changed_leaves(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = _bench(monkeypatch)
    location = _built(azurite)
    writer = InventoryWriter(BlobInventoryStorage(location, actor="test"))
    ops = _ObjectOps(location.store)
    live_fetch, full_fetch = bench.RecordingFetch(ops), bench.RecordingFetch(ops)
    live, full = LiveInventory(live_fetch), FullInventory(full_fetch)
    live.refresh().editions()
    full.refresh().leaves()
    live_ids = [p for p in _ids() if not corpus_embargoed(p)]

    seq = _ROOTS
    for size in (1, 10):
        live_fetch.take()
        full_fetch.take()
        bench.next_version(writer, full, live_ids[:size], seed=0, reason="test")
        seq += 1
        live.refresh().editions()
        full.refresh().leaves()

        for fetch in (live_fetch, full_fetch):
            paths = [path for path, _ in fetch.gets]
            assert paths[:3] == [CURRENT_NAME, root_path(seq), root_path(seq + 1)]
            assert len(paths) == 3 + size
            assert all(path.startswith("inventory/leaves/") for path in paths[3:])
        assert {live_ids[0]} <= set(live.editions())


def test_the_bench_measures_every_case_and_reports_them(
    azurite: _stores.Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = _bench(monkeypatch)
    location = _stores.location(azurite, uuid.uuid4().hex)

    result = bench.measure_shape(
        location, products=_PRODUCTS, artefacts=True, batch=_BATCH, repeats=2, version_sizes=(1, 10), threads=8
    )

    assert result.live + result.embargoed == _PRODUCTS
    assert result.build.puts == {"catalog.json": 1, "leaf": _PRODUCTS, "root": _ROOTS, CURRENT_NAME: _ROOTS}
    assert result.build.reads["catalog.json"] == _ROOTS + 1
    assert len(result.build.root_bytes) == _ROOTS
    by_case = {(r.view, r.case): r for r in result.readings}
    for view in ("LiveInventory", "FullInventory"):
        cold = by_case[view, "cold"]
        assert cold.gets == _COLD
        assert cold.absent == 1
        assert cold.by_document == {CURRENT_NAME: 1, "root": 2, "leaf": _PRODUCTS}
        assert len(cold.wall_s) == 2
        assert cold.sequential_estimate_s == pytest.approx(0.53)
        assert cold.concurrent_estimate_s == pytest.approx(0.01)
        prefill = by_case[view, "cold, 8-thread leaf prefill"]
        assert prefill.gets == _COLD
        assert len(prefill.served_s) == 2
        assert by_case[view, "one product, cold"].gets == 4
        assert by_case[view, "refresh after 1 changed"].gets == 4
        assert by_case[view, "refresh after 10 changed"].gets == 13
    assert [cost.puts["leaf"] for cost in result.versions] == [1, 10, 1, 10]
    assert attrs.asdict(result)["shape"] == "artefacts"
    lines = bench.report([result])
    assert any("| artefacts | LiveInventory | cold | 53 |" in line for line in lines)
    assert any("| artefacts | FullInventory | refresh after 10 changed | 13 |" in line for line in lines)

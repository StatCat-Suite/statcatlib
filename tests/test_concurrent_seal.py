"""Two processes at once: sealing different products, and reading one edition (RFC D6, GH-46).

Each process is a fresh interpreter (``spawn``), so nothing is shared but the
catalog directory. The seals race for the next Inventory
version; the loser re-reads the head and records its product on top
(`InventoryWriter.commit`). The readers open one sealed edition read-only at
the same time.
"""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from sdmxlib.inventory import FullInventory, local_fetch

import statcatlib as sc

pytestmark = pytest.mark.integration


def _seal(root: str, product: str, start: float) -> str:
    """In a child process: seal one dataset into *product*, the seal starting at *start*."""
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(product).working_copy()
        wc.put(
            sc.OpenDataset(
                identifier=product,
                title=sc.InternationalString(en=product),
                description=sc.InternationalString(en=product),
            )
        )
        time.sleep(max(0.0, start - time.time()))
        return wc.seal().edition_id


def _read(root: str, start: float) -> int:
    """In a child process: hold product ``p``'s view open and read it, from *start* on."""
    with sc.Catalog.open(root) as cat:
        view = cat.product("p").view()
        time.sleep(max(0.0, start - time.time()))
        for _ in range(20):
            assert view.get(sc.OpenDataset, "p") is not None
        return view.query(sc.BaseDataset).count()


def _pool() -> ProcessPoolExecutor:
    """Two fresh interpreters, sharing nothing but the catalog directory."""
    return ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn"))


def _start_time() -> float:
    """A moment both processes reach after their setup, so their critical steps overlap."""
    return time.time() + 8


def test_two_processes_sealing_different_products_both_end_live(tmp_path: Path) -> None:
    """RFC D6."""
    root = tmp_path / "cat"
    sc.Catalog.create(root).close()
    start = _start_time()
    with _pool() as pool:
        futures = {product: pool.submit(_seal, str(root), product, start) for product in ("a", "b")}
        sealed = {product: future.result(timeout=180) for product, future in futures.items()}

    full = FullInventory(local_fetch(root)).refresh()
    assert full.seq == 2
    assert full.products() == ["a", "b"]
    for product, edition_id in sealed.items():
        (entry,) = full.entries(product)
        assert (entry.edition_id, entry.state) == (edition_id, "live")
    with sc.Catalog.open(root) as cat:
        assert cat.products() == ["a", "b"]


def test_two_processes_read_one_sealed_edition_at_once(tmp_path: Path) -> None:
    """GH-46: a sealed edition opens read-only, so two processes hold it open together."""
    root = tmp_path / "cat"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy()
        wc.put(
            sc.OpenDataset(
                identifier="p", title=sc.InternationalString(en="p"), description=sc.InternationalString(en="p")
            )
        )
        wc.seal()
    start = _start_time()
    with _pool() as pool:
        readers = [pool.submit(_read, str(root), start) for _ in range(2)]
        assert [reader.result(timeout=180) for reader in readers] == [1, 1]

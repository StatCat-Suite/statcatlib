"""Concurrent query safety on one sealed edition's view.

Regression: the catalog used to share one DuckDB cursor across all
callers, so concurrent ``execute()`` calls from threads (e.g. FastAPI's
threadpool dispatching ``def`` route handlers) corrupted the cursor's
pending-result state and raised
``InvalidInputException: Attempting to execute an unsuccessful or closed
pending query result``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from sdmxlib import InternationalString as I18n

import statcatlib as sc

pytestmark = pytest.mark.unit


def _sealed_view(catalog: sc.Catalog, n: int = 20) -> sc.EditionView:
    wc = catalog.product("p").working_copy()
    for i in range(n):
        wc.put(
            sc.OpenDataset(
                identifier=f"ds-{i:03d}",
                title=I18n(en=f"Dataset {i}"),
                description=I18n(en=f"Description {i}"),
            )
        )
    wc.seal()
    catalog.refresh()
    return catalog.product("p").view()


def test_concurrent_queries_do_not_share_cursor(catalog: sc.Catalog) -> None:
    """Many threads issuing queries on one sealed view must not corrupt cursor state."""
    view = _sealed_view(catalog)

    def worker(i: int) -> int:
        # Mix of query shapes that all go through the store's `_cursor()`: get
        # and query terminals. Each thread runs many iterations so concurrent
        # execute() calls overlap.
        for _ in range(25):
            assert view.get(sc.BaseDataset, f"ds-{i % 20:03d}") is not None
            results = view.query(sc.BaseDataset).limit(5).collect()
            assert len(results) == 5
        return i

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(worker, i) for i in range(32)]
        for f in as_completed(futures):
            f.result()  # re-raises any exception from the worker


def test_per_thread_cursor_is_distinct(catalog: sc.Catalog) -> None:
    """Two threads asking a view for its cursor get different cursor objects."""
    view = _sealed_view(catalog, n=1)

    cursors: dict[str, object] = {}
    # Without the barrier the pool may run both tasks on one idle worker,
    # which correctly yields the same cursor and fails the assertion.
    barrier = threading.Barrier(2)

    def grab(name: str) -> None:
        barrier.wait(timeout=10)
        cursors[name] = view._cursor()

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(grab, ["a", "b"]))

    assert cursors["a"] is not cursors["b"]

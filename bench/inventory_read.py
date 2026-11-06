"""Measure the Inventory read at 7,000 products, over Azurite (GH-98, Phase 4 Part 4).

The corpus is `statcatlib.testing.inventory_corpus`: Inventory documents alone,
with no edition behind them. The script starts Azurite with
`tests.stores._stores.start`, builds the corpus twice, without artefact records
and with them, and counts each read through a recording `Fetch`:

- a cold `LiveInventory.refresh()` then `editions()`, and a cold
  `FullInventory.refresh()` then `leaves()`, in sequence;
- the same read with the leaves fetched first by a pool of 64 threads into a
  dictionary, which then serves the reader. Only this script does this;
- a cold read of one product: `LiveInventory.head`, `FullInventory.leaf`;
- a warm reader's `refresh()` and full read after a version that changes one
  product, and after one that changes 200.

Each read records its GETs, its bytes and its wall time. Azurite's wall time
excludes real storage latency, so the report also gives the estimates of
`bench/inventory-read-results.md`: ``GETs x 10 ms`` in sequence and
``ceil(GETs / 64) x 10 ms`` concurrent.

Usage, from the repository root, with a container runtime running:

    uv run python -m bench.inventory_read
    uv run python -m bench.inventory_read --json results.jsonl

``just ci`` does not run the full measurement. `tests/stores/test_inventory_read_counts.py`
runs this module's functions at 50 products.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import attrs
from attrs import asdict, define, field
from msgspec import UnsetType
from sdmxlib.inventory import (
    CREATED_AT_STEP,
    CURRENT_NAME,
    FullInventory,
    Leaf,
    LiveInventory,
    Root,
    decode_current,
    decode_root,
    record,
    root_path,
)
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.store.blob import BlobInventoryStorage, _ObjectOps
from statcatlib.store.inventory import InventoryWriter
from statcatlib.testing import (
    CorpusGroup,
    _sha256,
    _uuid7_form,
    corpus_embargoed,
    corpus_product_id,
    inventory_corpus,
)
from tests.stores import _stores

if TYPE_CHECKING:
    from collections.abc import Sequence

    from obstore import ObjectMeta
    from obstore.store import ObjectStore
    from sdmxlib.inventory import Fetch

    from statcatlib.store.blob import BlobLocation

#: The threads of the prefill case, and of the concurrent estimate.
THREADS = 64

#: The assumed latency of one storage request, in seconds, from GH-98.
ESTIMATE_S = 0.010

#: The versions `measure_shape` writes and measures a warm reader after, by the products each changes.
VERSION_SIZES = (1, 200)

#: The actor the corpus records, since Azurite's key carries no identity.
ACTOR = "bench-inventory-read"

#: The two readers.
VIEWS: dict[str, type[LiveInventory | FullInventory]] = {"LiveInventory": LiveInventory, "FullInventory": FullInventory}


def _write(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def read_all(reader: LiveInventory | FullInventory) -> None:
    """Read every product's leaf, as *reader*'s own full read does."""
    if isinstance(reader, LiveInventory):
        reader.editions()
    else:
        reader.leaves()


def read_one(reader: LiveInventory | FullInventory, product_id: str) -> None:
    """Read one product's leaf, as *reader*'s own one-product read does."""
    if isinstance(reader, LiveInventory):
        reader.head(product_id)
    else:
        reader.leaf(product_id)


# -- recording ---------------------------------------------------------------


@define
class RecordingFetch:
    """A `Fetch` over a store that records each GET: its path, and its size or ``None`` when absent.

    A pool of threads calls it, and `list.append` is atomic.
    """

    ops: _ObjectOps
    gets: list[tuple[str, int | None]] = field(factory=list)

    def __call__(self, path: str) -> bytes | None:
        """The bytes at *path*, or ``None``, after recording the GET."""
        got = self.ops.read(path)
        raw = None if got is None else got[0]
        self.gets.append((path, None if raw is None else len(raw)))
        return raw

    def take(self) -> list[tuple[str, int | None]]:
        """The GETs recorded so far, and start again from none."""
        taken, self.gets = self.gets, []
        return taken


class WriterOps(_ObjectOps):
    """Counts the writer's store calls by document, to set its reads and writes beside the design's."""

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self.calls: list[tuple[str, str, int]] = []

    def take(self) -> list[tuple[str, str, int]]:
        """The calls recorded so far, as ``(call, path, bytes)``, and start again from none."""
        taken, self.calls = self.calls, []
        return taken

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        got = super().read(path)
        self.calls.append(("read", path, 0 if got is None else len(got[0])))
        return got

    @override
    def head(self, path: str) -> ObjectMeta | None:
        self.calls.append(("head", path, 0))
        return super().head(path)

    @override
    def put(self, path: str, data: bytes, **kwargs: Any) -> str | None:
        self.calls.append(("put", path, len(data)))
        return super().put(path, data, **kwargs)


def document(path: str) -> str:
    """The kind of Inventory document at *path*: ``leaf``, ``root``, or the name itself."""
    if path.startswith("inventory/leaves/"):
        return "leaf"
    if path.startswith("inventory/"):
        return "root"
    return path


@define
class Reading:
    """One case's measurement. The counts are the same on each repeat, and the wall times are one per repeat."""

    shape: str
    view: str
    case: str
    gets: int
    absent: int
    by_document: dict[str, int]
    read_bytes: int
    wall_s: list[float] = field(factory=list)
    served_s: list[float] = field(factory=list)

    @property
    def sequential_estimate_s(self) -> float:
        """An estimate: ``GETs x 10 ms``."""
        return self.gets * ESTIMATE_S

    @property
    def concurrent_estimate_s(self) -> float:
        """An estimate: ``ceil(GETs / 64) x 10 ms``."""
        return math.ceil(self.gets / THREADS) * ESTIMATE_S


def _reading(shape: str, view: str, case: str, gets: list[tuple[str, int | None]], wall_s: float) -> Reading:
    by_document: dict[str, int] = {}
    for path, _ in gets:
        kind = document(path)
        by_document[kind] = by_document.get(kind, 0) + 1
    return Reading(
        shape=shape,
        view=view,
        case=case,
        gets=len(gets),
        absent=sum(1 for _, size in gets if size is None),
        by_document=by_document,
        read_bytes=sum(size or 0 for _, size in gets),
        wall_s=[wall_s],
    )


def _add_repeat(readings: dict[tuple[str, str], Reading], found: Reading, served_s: float | None = None) -> None:
    """Keep *found* under its view and case, or add its wall time to the repeats, refusing a changed count."""
    held = readings.setdefault((found.view, found.case), attrs.evolve(found, wall_s=[]))
    if (held.gets, held.absent, held.by_document) != (found.gets, found.absent, found.by_document):
        msg = f"{found.view} {found.case}: the GETs changed between repeats: {held} and {found}"
        raise RuntimeError(msg)
    held.wall_s.extend(found.wall_s)
    if served_s is not None:
        held.served_s.append(served_s)


# -- the cases ---------------------------------------------------------------


def cold_sequential(view: type[LiveInventory | FullInventory], fetch: Fetch) -> None:
    """A new reader's `refresh`, then its read of every product."""
    read_all(view(fetch).refresh())


def cold_prefilled(view: type[LiveInventory | FullInventory], fetch: Fetch, *, threads: int = THREADS) -> float:
    """A new reader served from a dictionary a pool of threads filled, leaves first.

    ``CURRENT`` and its root go into the dictionary too, so the reader's only
    other GET is its probe of the next version.

    Args:
        view: The reader's class.
        fetch: The recording fetch.
        threads: The pool's size.

    Returns:
        The seconds the reader took once the dictionary was full.

    Raises:
        RuntimeError: The catalog has no ``CURRENT`` or no root at its ``seq``.
    """
    current = fetch(CURRENT_NAME)
    if current is None:
        msg = f"the corpus has no {CURRENT_NAME}"
        raise RuntimeError(msg)
    seq = decode_current(current).seq
    root = fetch(root_path(seq))
    if root is None:
        msg = f"the corpus has no root at {seq}"
        raise RuntimeError(msg)
    paths = [ref.leaf for ref in decode_root(root).products.values()]
    filled: dict[str, bytes | None] = {CURRENT_NAME: current, root_path(seq): root}
    with ThreadPoolExecutor(threads) as pool:
        filled.update(zip(paths, pool.map(fetch, paths), strict=True))

    def serve(path: str) -> bytes | None:
        return filled[path] if path in filled else fetch(path)

    start = time.perf_counter()
    read_all(view(serve).refresh())
    return time.perf_counter() - start


def cold_one(view: type[LiveInventory | FullInventory], fetch: Fetch, product_id: str) -> None:
    """A new reader's read of one product."""
    read_one(view(fetch), product_id)


def next_version(
    writer: InventoryWriter, full: FullInventory, products: Sequence[str], *, seed: int, reason: str
) -> Root:
    """Write the version after *full*'s, giving each of *products* a new live edition built on its live one.

    Each entry copies the routing fields and artefact records of the one it
    supersedes. The leaf then holds the new entry and the superseded one.

    Args:
        writer: The corpus's writer.
        full: A reader holding the newest version, whose leaves give each product's next.
        products: The products to change, each with a live edition.
        seed: The corpus seed.
        reason: The version's ``event.reason``.

    Returns:
        The root written.

    Raises:
        RuntimeError: A live entry has no routing fields, which `inventory_corpus` always writes.
    """
    base = full.root
    now = base.created_at + CREATED_AT_STEP
    leaves: dict[str, Leaf] = {}
    for product_id in products:
        leaf = full.leaf(product_id)
        live = next(entry for entry in leaf.editions if entry.state == "live")
        if isinstance(live.dataflows, UnsetType) or isinstance(live.pull_bytes, UnsetType):
            msg = f"{product_id}'s live entry has no routing fields"
            raise RuntimeError(msg)
        edition_id = _uuid7_form(_sha256(seed, product_id, base.seq))
        leaves[product_id] = record(
            leaf,
            product_id=product_id,
            kind=leaf.kind,
            lifecycle=leaf.lifecycle,
            edition_id=edition_id,
            previous=live.edition_id,
            manifest_sha256=_sha256(product_id, edition_id),
            schema_version=SCHEMA_VERSION,
            embargo=False,
            now=now,
            artefacts=live.artefacts,
            dataflows=live.dataflows,
            pull_bytes=live.pull_bytes,
        )
    return writer.try_commit(base, leaves, now=now, kind="seal", reason=reason)


# -- one shape ---------------------------------------------------------------


@define
class WriteCost:
    """What a stretch of writing cost, as `WriterOps` saw it."""

    name: str
    wall_s: float
    reads: dict[str, int]
    puts: dict[str, int]
    put_bytes: dict[str, int]
    root_bytes: list[int]

    @property
    def requests(self) -> int:
        """Every store call."""
        return sum(self.reads.values()) + sum(self.puts.values())

    @property
    def estimate_s(self) -> float:
        """An estimate: every store call in sequence at 10 ms."""
        return self.requests * ESTIMATE_S


def _write_cost(name: str, calls: list[tuple[str, str, int]], wall_s: float) -> WriteCost:
    reads: dict[str, int] = {}
    puts: dict[str, int] = {}
    put_bytes: dict[str, int] = {}
    for call, path, size in calls:
        kind = document(path)
        if call == "put":
            puts[kind] = puts.get(kind, 0) + 1
            put_bytes[kind] = put_bytes.get(kind, 0) + size
        else:
            reads[kind] = reads.get(kind, 0) + 1
    root_bytes = [size for call, path, size in calls if call == "put" and document(path) == "root"]
    return WriteCost(name, wall_s, reads, puts, put_bytes, root_bytes)


@define
class ShapeResult:
    """Everything measured on one corpus."""

    shape: str
    products: int
    live: int
    embargoed: int
    build: WriteCost
    versions: list[WriteCost]
    readings: list[Reading]


def measure_shape(
    location: BlobLocation,
    *,
    products: int,
    artefacts: bool,
    batch: int = 500,
    embargoed: float = 0.1,
    seed: int = 0,
    repeats: int = 3,
    version_sizes: Sequence[int] = VERSION_SIZES,
    threads: int = THREADS,
) -> ShapeResult:
    """Build one corpus at *location* and measure every case on it.

    Args:
        location: An empty catalog location.
        products: The corpus's products.
        artefacts: Give each entry artefact records.
        batch: The products of each corpus commit.
        embargoed: The share embargoed.
        seed: The corpus seed.
        repeats: How many times each case runs. A warm case writes a new version each time.
        version_sizes: The products each measured version changes.
        threads: The prefill pool's size.

    Returns:
        The build's cost, each version's cost, and each case's reading.
    """
    shape = "artefacts" if artefacts else "plain"
    writer_ops = WriterOps(location.store)
    writer = InventoryWriter(BlobInventoryStorage(attrs.evolve(location, ops=writer_ops), actor=ACTOR))
    start = time.perf_counter()
    inventory_corpus(writer, products, embargoed=embargoed, artefacts=artefacts, batch=batch, seed=seed)
    build = _write_cost(f"{products} products, batch {batch}", writer_ops.take(), time.perf_counter() - start)

    group = CorpusGroup(products)
    ids = [corpus_product_id(group, index) for index in range(products)]
    live_ids = [p for p in ids if not corpus_embargoed(p, seed=seed, embargoed=embargoed)]
    ops = _ObjectOps(location.store)
    readings: dict[tuple[str, str], Reading] = {}

    for _ in range(repeats):
        for name, view in VIEWS.items():
            fetch = RecordingFetch(ops)
            start = time.perf_counter()
            cold_sequential(view, fetch)
            _add_repeat(readings, _reading(shape, name, "cold", fetch.take(), time.perf_counter() - start))

            fetch = RecordingFetch(ops)
            start = time.perf_counter()
            served_s = cold_prefilled(view, fetch, threads=threads)
            wall_s = time.perf_counter() - start
            _add_repeat(
                readings, _reading(shape, name, f"cold, {threads}-thread leaf prefill", fetch.take(), wall_s), served_s
            )

            fetch = RecordingFetch(ops)
            start = time.perf_counter()
            cold_one(view, fetch, live_ids[0])
            _add_repeat(readings, _reading(shape, name, "one product, cold", fetch.take(), time.perf_counter() - start))

    live_fetch, full_fetch = RecordingFetch(ops), RecordingFetch(ops)
    live, full = LiveInventory(live_fetch), FullInventory(full_fetch)
    warm = {"LiveInventory": (live, live_fetch), "FullInventory": (full, full_fetch)}
    for reader, fetch in warm.values():
        read_all(reader.refresh())
        fetch.take()

    versions: list[WriteCost] = []
    changed = 0
    for _ in range(repeats):
        for size in version_sizes:
            products_now = [live_ids[(changed + n) % len(live_ids)] for n in range(size)]
            changed += size
            start = time.perf_counter()
            next_version(writer, full, products_now, seed=seed, reason=f"bench: change {size} products")
            versions.append(_write_cost(f"one version of {size}", writer_ops.take(), time.perf_counter() - start))
            for name, (reader, fetch) in warm.items():
                start = time.perf_counter()
                read_all(reader.refresh())
                wall_s = time.perf_counter() - start
                _add_repeat(readings, _reading(shape, name, f"refresh after {size} changed", fetch.take(), wall_s))

    return ShapeResult(
        shape=shape,
        products=products,
        live=len(live_ids),
        embargoed=products - len(live_ids),
        build=build,
        versions=versions,
        readings=list(readings.values()),
    )


# -- report ------------------------------------------------------------------


def _median_range(values: Sequence[float]) -> str:
    return f"{statistics.median(values):.3f} ({min(values):.3f} to {max(values):.3f})"


def report(results: Sequence[ShapeResult]) -> list[str]:
    """The Markdown tables of *results*: the writes, then the reads.

    Args:
        results: One result per corpus.

    Returns:
        The lines.
    """
    lines = [
        (
            "| Shape | Write | Runs | Reads | Leaf Puts | Root Puts | `CURRENT` Puts | Other Puts | Leaf bytes | "
            "Last root bytes | Root bytes, all | Wall (s), median | Estimate (s) |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        by_name: dict[str, list[WriteCost]] = {}
        for cost in [result.build, *result.versions]:
            by_name.setdefault(cost.name, []).append(cost)
        for name, costs in by_name.items():
            cost = costs[0]
            other = sum(n for kind, n in cost.puts.items() if kind not in {"leaf", "root", CURRENT_NAME})
            wall_s = statistics.median(c.wall_s for c in costs)
            lines.append(
                f"| {result.shape} | {name} | {len(costs)} | {sum(cost.reads.values())} | {cost.puts.get('leaf', 0)} | "
                f"{cost.puts.get('root', 0)} | {cost.puts.get(CURRENT_NAME, 0)} | {other} | "
                f"{cost.put_bytes.get('leaf', 0)} | {cost.root_bytes[-1] if cost.root_bytes else 0} | "
                f"{sum(cost.root_bytes)} | {wall_s:.2f} | {cost.estimate_s:.2f} |"
            )
    lines += [
        "",
        (
            "| Shape | Reader | Case | GETs | `CURRENT` | Roots | Leaves | Absent | Bytes | Wall (s), median (range) | "
            "Reader served (s), median | Sequential estimate (s) | Concurrent estimate (s) |"
        ),
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        for reading in result.readings:
            served = f"{statistics.median(reading.served_s):.3f}" if reading.served_s else ""
            lines.append(
                f"| {reading.shape} | {reading.view} | {reading.case} | {reading.gets} | "
                f"{reading.by_document.get(CURRENT_NAME, 0)} | {reading.by_document.get('root', 0)} | "
                f"{reading.by_document.get('leaf', 0)} | {reading.absent} | {reading.read_bytes} | "
                f"{_median_range(reading.wall_s)} | {served} | {reading.sequential_estimate_s:.2f} | "
                f"{reading.concurrent_estimate_s:.2f} |"
            )
    return lines


# -- command line --------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure the Inventory read over Azurite.")
    parser.add_argument("--products", type=int, default=7000)
    parser.add_argument("--batch", type=int, default=500, help="products a corpus commit")
    parser.add_argument("--embargoed", type=float, default=0.1, help="the share embargoed")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=3, help="runs of each case")
    parser.add_argument("--json", type=Path, help="also write each corpus's result to this file, one a line")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Start Azurite, measure both corpus shapes, print the tables, and remove the container.

    Args:
        argv: The arguments; the process's when ``None``.

    Raises:
        SystemExit: No container can run here.
    """
    args = _parser().parse_args(argv)
    missing = _stores.runtime_missing()
    if missing is not None:
        raise SystemExit(missing)
    started = time.perf_counter()
    running = _stores.start()
    try:
        results = [
            measure_shape(
                _stores.location(running, f"inventory-read-{uuid.uuid4().hex}"),
                products=args.products,
                artefacts=artefacts,
                batch=args.batch,
                embargoed=args.embargoed,
                seed=args.seed,
                repeats=args.repeats,
            )
            for artefacts in (False, True)
        ]
    finally:
        _stores.stop(running)
    for line in report(results):
        _write(line)
    _write(f"\nThe run took {time.perf_counter() - started:.0f} s, Azurite's start included.")
    _write(f"The load averages at the end: {os.getloadavg()}.")
    if args.json is not None:
        with args.json.open("w") as handle:
            for result in results:
                handle.write(json.dumps(asdict(result)) + "\n")


if __name__ == "__main__":
    main()

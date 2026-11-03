"""Build the synthetic CODR corpus and measure the cross-product rules as it grows (GH-124, Phase 4).

The corpus is `statcatlib.testing.codr_corpus`: one-table CODR data products
cut from the three structure products, about one in ten sealed under embargo.
At each sample size the script seals more products and records, for each:

- the wall time spent in `statcatlib.store.cross_product.refusals` (the
  cross-product rules, run by the seal's pre-check and by its Inventory
  commit);
- the manifests the rules' reader (`cross_product.ManifestArtefacts`) read
  from disk, their total size, and the reader's calls, cache hits included;
- the wall time of `InventoryWriter.full` (the Inventory head reads);
- the wall time of `WorkingCopy.seal`, and of the whole product
  (`codr_product`: fill, index and seal).

A **warm** seal runs in the building process, whose Inventory writer has
cached every manifest read so far. A **cold** seal runs in a new Python
process (``multiprocessing``'s ``spawn``), as a producer's one-off seal or a
release job would. The script measures by wrapping those four functions in
each process (`install_meter`); the library holds no measuring hook.

At the size reached it also records a `Catalog.seal` of a small batch (warm,
then cold) and `CatalogAdmin.release` of embargoed editions (the first in a
new process reads every manifest; the second, through the same
`CatalogAdmin`, reads its warm cache).

Usage, from the repository root:

    uv run python bench/corpus.py build --root ~/statcat-bench/corpus
    uv run python bench/corpus.py mixed --root ~/statcat-bench/corpus
    uv run python bench/corpus.py report --root ~/statcat-bench/corpus

``build`` grows the corpus to ``--n`` one-table products, sampling at each of
``--samples``. It stops before a sample whose projected disk use passes
``--max-gb`` or whose projected build time passes ``--max-hours``, or during
a segment that passes either, and then measures at the size reached. A second
``build`` resumes the corpus. ``mixed`` adds multi-table products. Every
record is appended to ``<root>/bench.jsonl``, and ``report`` prints the
Markdown table and the fitted growth rate from it.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from attrs import asdict, define
from sdmxlib.edition import MANIFEST_NAME

from statcatlib import Catalog, CatalogAdmin
from statcatlib.store import cross_product
from statcatlib.store.inventory import InventoryWriter
from statcatlib.store.working_copy import WorkingCopy
from statcatlib.testing import CodrShape, CorpusGroup, codr_corpus, codr_product, codr_working_copy, corpus_product_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sdmxlib.inventory import PartialRecord

#: The shapes ``--shape`` names: the CODR default, and the small one the test uses.
SHAPES: dict[str, CodrShape] = {
    "default": CodrShape(),
    "small": CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20, 10), observations=200),
}

#: The file each record is appended to, under the corpus root.
RESULTS_NAME = "bench.jsonl"

#: The tables of each product the samples grow.
ONE_TABLE = 1

#: How many products the build checks its disk and time limits after.
CHECK_EVERY = 50

#: The fewest sample sizes a growth rate is fitted to.
MIN_FIT_POINTS = 2

#: One line of ``bench.jsonl``.
type Record = dict[str, Any]


def _write(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# -- the meter ---------------------------------------------------------------


@define
class Meter:
    """What one measured operation cost, summed over the calls it made."""

    rules_s: float = 0.0
    rules_calls: int = 0
    reads: int = 0
    read_bytes: int = 0
    read_s: float = 0.0
    lookups: int = 0
    inventory_s: float = 0.0
    inventory_calls: int = 0
    seal_s: float = 0.0


METER = Meter()


def reset_meter() -> Meter:
    """Return the meter's totals and start it again from zero."""
    taken = Meter(**asdict(METER))
    for name, value in asdict(Meter()).items():
        setattr(METER, name, value)
    return taken


def install_meter() -> None:
    """Wrap the functions the bench measures, in this process.

    `InventoryWriter.refusals` calls ``cross_product.refusals`` through the
    module, so replacing the module's function times every run of the rules.
    """
    refusals = cross_product.refusals
    full = InventoryWriter.full
    seal = WorkingCopy.seal
    read = cross_product.ManifestArtefacts.__call__

    def timed_refusals(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return refusals(*args, **kwargs)
        finally:
            METER.rules_s += time.perf_counter() - start
            METER.rules_calls += 1

    def timed_full(self: InventoryWriter) -> Any:
        start = time.perf_counter()
        try:
            return full(self)
        finally:
            METER.inventory_s += time.perf_counter() - start
            METER.inventory_calls += 1

    def timed_seal(self: WorkingCopy, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return seal(self, *args, **kwargs)
        finally:
            METER.seal_s += time.perf_counter() - start

    def counted_read(
        self: cross_product.ManifestArtefacts, product_id: str, edition_id: str, manifest_sha256: str
    ) -> Mapping[str, PartialRecord]:
        METER.lookups += 1
        if (edition_id, manifest_sha256) in self._cache:
            return read(self, product_id, edition_id, manifest_sha256)
        start = time.perf_counter()
        found = read(self, product_id, edition_id, manifest_sha256)
        METER.read_s += time.perf_counter() - start
        METER.reads += 1
        METER.read_bytes += (self._root / "products" / product_id / edition_id / MANIFEST_NAME).stat().st_size
        return found

    cross_product.refusals = timed_refusals
    InventoryWriter.full = timed_full
    WorkingCopy.seal = timed_seal
    cross_product.ManifestArtefacts.__call__ = counted_read


# -- the corpus --------------------------------------------------------------


def census(root: Path) -> dict[str, int]:
    """The data products the newest Inventory version records: all, with a live edition, with an embargoed one."""
    full = InventoryWriter(root).full()
    counts = {"data": 0, "live": 0, "embargoed": 0}
    if full is None:
        return counts
    for product_id in full.products():
        leaf = full.leaf(product_id)
        if leaf.kind != "data":
            continue
        counts["data"] += 1
        states = {entry.state for entry in leaf.editions}
        counts["live"] += "live" in states
        counts["embargoed"] += "embargoed" in states
    return counts


def embargoed_products(root: Path) -> list[str]:
    """The data products with an embargoed edition in the newest Inventory version, sorted."""
    full = InventoryWriter(root).full()
    if full is None:
        return []
    return [
        product_id
        for product_id in full.products()
        if full.leaf(product_id).kind == "data" and any(e.state == "embargoed" for e in full.leaf(product_id).editions)
    ]


def disk_bytes(root: Path) -> int:
    """The bytes the files under *root* take on disk (``st_blocks``, as ``du`` counts them)."""
    total = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_blocks * 512
    return total


def append(root: Path, record: Record) -> None:
    """Append *record* to the results file, with the one-minute load average and a timestamp, and print it."""
    record = {**record, "load1": round(os.getloadavg()[0], 1), "at": round(time.time())}
    line = json.dumps(record)
    with (root / RESULTS_NAME).open("a") as handle:
        handle.write(line + "\n")
    _write(line)


def next_ids(root: Path, start: int, count: int) -> list[str]:
    """*count* one-table product ids, from index *start* on, that the Inventory does not record yet."""
    recorded = set(CatalogAdmin.open(root).products())
    found: list[str] = []
    index = start
    while len(found) < count:
        product_id = corpus_product_id(CorpusGroup(index + 1, ONE_TABLE), index)
        if product_id not in recorded:
            found.append(product_id)
        index += 1
    return found


def in_new_process[**P](fn: Callable[P, Record], *args: P.args, **kwargs: P.kwargs) -> Record:
    """Run *fn* in a new Python process and return its record, with ``process_s``, the process's whole wall time."""
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        record = pool.submit(fn, *args, **kwargs).result()
    return {**record, "process_s": time.perf_counter() - start}


class StopBuildError(Exception):
    """The build passed its disk or time limit."""


class LimitCheck:
    """`codr_corpus`'s *on_sealed*: raise `StopBuildError` once the build passes a limit."""

    def __init__(self, root: Path, *, started: float, max_bytes: float, max_s: float) -> None:
        """Check the corpus at *root* against *max_bytes* on disk and *max_s* since *started*."""
        self._root = root
        self._started = started
        self._max_bytes = max_bytes
        self._max_s = max_s
        self._sealed = 0

    def __call__(self, _product_id: str, _edition: object) -> None:
        """Check the limits every `CHECK_EVERY` products."""
        self._sealed += 1
        if self._sealed % CHECK_EVERY:
            return
        if time.perf_counter() - self._started > self._max_s or disk_bytes(self._root) > self._max_bytes:
            raise StopBuildError


# -- measuring ---------------------------------------------------------------


def measure_seal(cat: Catalog, product_id: str, *, seed: int, shape: CodrShape) -> Record:
    """Seal *product_id* in *cat*, with the embargo off, and return what it cost."""
    reset_meter()
    start = time.perf_counter()
    codr_product(cat, product_id, seed=seed, shape=shape)
    product_s = time.perf_counter() - start
    return {"product": product_id, "product_s": product_s, **asdict(reset_meter())}


def measure_batch(cat: Catalog, product_ids: list[str], *, seed: int, shape: CodrShape) -> Record:
    """Fill a working copy for each of *product_ids*, seal them in one `Catalog.seal`, and return what the seal cost."""
    working = [codr_working_copy(cat, product_id, seed=seed, shape=shape) for product_id in product_ids]
    reset_meter()
    start = time.perf_counter()
    outcomes = cat.seal(working)
    batch_s = time.perf_counter() - start
    if outcomes.refused:
        msg = f"the batch was refused: {outcomes.refused}"
        raise RuntimeError(msg)
    return {"products": product_ids, "batch_s": batch_s, **asdict(reset_meter())}


def measure_release(admin: CatalogAdmin, product_id: str) -> Record:
    """Release *product_id*'s embargoed edition with `CatalogAdmin.release`, and return what it cost."""
    edition = next(e for e in admin.editions(product_id) if e.state == "embargoed")
    reset_meter()
    start = time.perf_counter()
    admin.release(product_id, edition.edition_id, reason="bench: release one embargoed edition")
    release_s = time.perf_counter() - start
    return {"product": product_id, "release_s": release_s, **asdict(reset_meter())}


def cold_seal(root: Path, product_id: str, seed: int, shape: CodrShape) -> Record:
    """`measure_seal` in this process, which a new process runs: open the catalog, seal one product."""
    install_meter()
    with Catalog.create(root, exist_ok=True) as cat:
        return measure_seal(cat, product_id, seed=seed, shape=shape)


def cold_batch(root: Path, product_ids: list[str], seed: int, shape: CodrShape) -> Record:
    """`measure_batch` in this process, which a new process runs."""
    install_meter()
    with Catalog.create(root, exist_ok=True) as cat:
        return measure_batch(cat, product_ids, seed=seed, shape=shape)


def cold_releases(root: Path, product_ids: list[str]) -> Record:
    """Release each product's embargoed edition through one `CatalogAdmin`, in this process.

    A new process runs it: the first release reads every manifest, and the
    next ones read the admin's warm cache.
    """
    install_meter()
    admin = CatalogAdmin.open(root)
    releases = [
        {"kind": "release-warm" if n else "release-cold", **measure_release(admin, product_id)}
        for n, product_id in enumerate(product_ids)
    ]
    return {"releases": releases}


def sample(root: Path, cat: Catalog, args: argparse.Namespace) -> None:
    """Record ``--repeats`` warm seals, then as many cold seals, at the corpus's size."""
    counts = census(root)
    ids = next_ids(root, counts["data"], 2 * args.repeats)
    for product_id in ids[: args.repeats]:
        append(root, {"kind": "warm", **counts, **measure_seal(cat, product_id, seed=args.seed, shape=args.shape)})
    for product_id in ids[args.repeats :]:
        record = in_new_process(cold_seal, root, product_id, args.seed, args.shape)
        append(root, {"kind": "cold", **counts, **record})


def final(root: Path, cat: Catalog, args: argparse.Namespace) -> None:
    """Record a batch seal (warm, then cold) and two releases (cold, then warm) at the size reached."""
    counts = census(root)
    ids = next_ids(root, counts["data"], 2 * args.batch)
    warm = measure_batch(cat, ids[: args.batch], seed=args.seed, shape=args.shape)
    append(root, {"kind": "batch-warm", **counts, **warm})
    cold = in_new_process(cold_batch, root, ids[args.batch :], args.seed, args.shape)
    append(root, {"kind": "batch-cold", **counts, **cold})
    embargoed = embargoed_products(root)[:2]
    if embargoed:
        counts = census(root)
        for record in in_new_process(cold_releases, root, embargoed)["releases"]:
            append(root, {**counts, **record})


def build(args: argparse.Namespace) -> None:
    """Grow the corpus, sampling as it grows, then measure the batch and the releases at the size reached."""
    root: Path = args.root
    install_meter()
    started = time.perf_counter()
    max_bytes = args.max_gb * 1e9
    max_s = args.max_hours * 3600
    with Catalog.create(root, exist_ok=True) as cat:
        append(root, {"kind": "start", **census(root), "disk_bytes": disk_bytes(root)})
        per_product_s: float | None = None
        for target in sorted(s for s in args.samples if s <= args.n):
            done = census(root)["data"]
            if target <= done:
                continue
            used = disk_bytes(root)
            if done and used / done * target > max_bytes:
                append(root, {"kind": "stop", "reason": "disk", "data": done, "target": target, "disk_bytes": used})
                break
            if per_product_s is not None and time.perf_counter() - started + per_product_s * (target - done) > max_s:
                append(root, {"kind": "stop", "reason": "time", "data": done, "target": target})
                break
            segment = time.perf_counter()
            check = LimitCheck(root, started=started, max_bytes=max_bytes, max_s=max_s)
            group = CorpusGroup(target, ONE_TABLE)
            try:
                codr_corpus(cat, [group], seed=args.seed, shape=args.shape, embargoed=args.embargoed, on_sealed=check)
            except StopBuildError:
                append(root, {"kind": "stop", "reason": "limit", **census(root), "disk_bytes": disk_bytes(root)})
                sample(root, cat, args)
                break
            counts = census(root)
            segment_s = time.perf_counter() - segment
            per_product_s = segment_s / max(counts["data"] - done, 1)
            record = {"kind": "build", **counts, "segment_s": segment_s, "elapsed_s": time.perf_counter() - started}
            append(root, {**record, "disk_bytes": disk_bytes(root)})
            sample(root, cat, args)
        final(root, cat, args)
    end = {"kind": "end", **census(root), "elapsed_s": time.perf_counter() - started}
    append(root, {**end, "disk_bytes": disk_bytes(root)})


def mixed(args: argparse.Namespace) -> None:
    """Add the multi-table products ``--groups`` names, and record each group's build time."""
    root: Path = args.root
    with Catalog.create(root, exist_ok=True) as cat:
        for spec in args.groups:
            count, tables = (int(part) for part in spec.split("x"))
            start = time.perf_counter()
            group = CorpusGroup(count, tables)
            sealed = codr_corpus(cat, [group], seed=args.seed, shape=args.shape, embargoed=args.embargoed)
            record = {
                "kind": "mixed",
                "products": len(sealed),
                "tables": tables,
                "group_s": time.perf_counter() - start,
            }
            append(root, {**record, **census(root), "disk_bytes": disk_bytes(root)})


# -- report ------------------------------------------------------------------


def fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Least squares of y on x, and of log y on log x.

    Args:
        points: ``(x, y)`` pairs, x and y positive.

    Returns:
        The slope, the intercept, and the log-log exponent.
    """
    xs, ys = zip(*points, strict=True)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x in xs)
    lx, ly = [math.log(x) for x in xs], [math.log(y) for y in ys]
    mlx, mly = statistics.fmean(lx), statistics.fmean(ly)
    exponent = sum((a - mlx) * (b - mly) for a, b in zip(lx, ly, strict=True)) / sum((a - mlx) ** 2 for a in lx)
    return slope, my - slope * mx, exponent


def _median(rows: list[Record], key: str) -> float:
    return statistics.median(float(row[key]) for row in rows)


#: The report's columns: heading, kind, field, scale, format.
COLUMNS: tuple[tuple[str, str, str, float, str], ...] = (
    ("Warm rules (ms)", "warm", "rules_s", 1000, ".0f"),
    ("Warm reads", "warm", "reads", 1, ".0f"),
    ("Warm read (KiB)", "warm", "read_bytes", 1 / 1024, ".0f"),
    ("Warm seal (s)", "warm", "seal_s", 1, ".2f"),
    ("Cold rules (ms)", "cold", "rules_s", 1000, ".0f"),
    ("Cold reads", "cold", "reads", 1, ".0f"),
    ("Cold read (MiB)", "cold", "read_bytes", 1 / 2**20, ".1f"),
    ("Cold seal (s)", "cold", "seal_s", 1, ".2f"),
    ("Cold Inventory reads (s)", "cold", "inventory_s", 1, ".2f"),
    ("Cold process (s)", "cold", "process_s", 1, ".1f"),
)


def table(records: list[Record]) -> tuple[list[str], dict[str, list[tuple[float, float]]]]:
    """The Markdown table of medians per sample size, and each kind's ``(live products, rules seconds)`` points."""
    by_size: dict[int, dict[str, list[Record]]] = {}
    for record in records:
        if record["kind"] in ("warm", "cold"):
            by_size.setdefault(int(record["data"]), {}).setdefault(str(record["kind"]), []).append(record)
    lines = [
        "| Data products | Live | Embargoed | " + " | ".join(c[0] for c in COLUMNS) + " |",
        "|" + "---:|" * (3 + len(COLUMNS)),
    ]
    points: dict[str, list[tuple[float, float]]] = {"warm": [], "cold": []}
    for size in sorted(by_size):
        kinds = by_size[size]
        first = next(iter(kinds.values()))[0]
        cells = [str(size), str(first["live"]), str(first["embargoed"])]
        for _, kind, key, scale, spec in COLUMNS:
            rows = kinds.get(kind, [])
            cells.append(format(_median(rows, key) * scale, spec) if rows and key in rows[0] else "")
        for kind, rows in kinds.items():
            points[kind].append((float(first["live"]), _median(rows, "rules_s")))
        lines.append("| " + " | ".join(cells) + " |")
    return lines, points


def report(args: argparse.Namespace) -> None:
    """Print the per-size table, the fitted growth of the rules' time, and the other records."""
    records = [json.loads(line) for line in (args.root / RESULTS_NAME).read_text().splitlines() if line]
    lines, points = table(records)
    for line in lines:
        _write(line)
    for kind, found in points.items():
        if len(found) >= MIN_FIT_POINTS:
            slope, intercept, exponent = fit(found)
            _write(
                f"\n{kind} rules time = {intercept * 1000:.1f} ms + {slope * 1e6:.1f} us per live data product; "
                f"log-log exponent {exponent:.2f}"
            )
    _write("")
    for record in records:
        if record["kind"] not in ("warm", "cold"):
            _write(json.dumps(record))


# -- command line --------------------------------------------------------------


def _ints(text: str) -> list[int]:
    return [int(part) for part in text.split(",")]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the synthetic CODR corpus and measure the cross-product rules.")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "build": "grow the corpus of one-table products, sampling as it grows",
        "mixed": "add multi-table products, groups given as <products>x<tables>",
        "report": "print the results table from bench.jsonl",
    }
    made = {}
    for name, text in commands.items():
        one = sub.add_parser(name, help=text)
        one.add_argument("--root", type=lambda s: Path(s).expanduser().absolute(), required=True)
        one.add_argument("--seed", type=int, default=0)
        one.add_argument("--shape", choices=sorted(SHAPES), default="default")
        one.add_argument("--embargoed", type=float, default=0.1, help="the share sealed under embargo")
        made[name] = one
    made["build"].add_argument("--n", type=int, default=7000, help="the one-table products to grow to")
    made["build"].add_argument("--samples", type=_ints, default=[100, 500, 1000, 2000, 4000, 7000])
    made["build"].add_argument("--repeats", type=int, default=3, help="warm and cold seals per sample")
    made["build"].add_argument("--batch", type=int, default=5, help="products in the measured Catalog.seal")
    made["build"].add_argument("--max-gb", type=float, default=50.0)
    made["build"].add_argument("--max-hours", type=float, default=8.0)
    made["mixed"].add_argument("--groups", nargs="+", default=["13x5", "13x10", "12x15", "12x20"])
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the command *argv* names.

    Args:
        argv: The arguments; the process's when ``None``.
    """
    args = _parser().parse_args(argv)
    args.shape = SHAPES[args.shape]
    {"build": build, "mixed": mixed, "report": report}[args.command](args)


if __name__ == "__main__":
    main()

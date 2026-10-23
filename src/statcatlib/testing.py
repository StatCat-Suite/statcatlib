"""Synthetic statcat bundles, for tests and benchmarks at any shape (GH-3).

`sdmxlib.testing.synthetic_bundle` builds a conforming sdmxlib bundle from a
`sdmxlib.testing.Shape` and a seed. `synthetic_catalog` makes it a statcat
bundle: a catalog dataset per dataflow, a search index over it, and a
manifest with the ``statcat`` section, so it passes
`statcatlib.bundle.check_bundle`. The same seed builds the same structures and
observations, so a failure reproduces.

Example:
    from sdmxlib.testing import Dim, Shape

    from statcatlib.testing import synthetic_catalog

    shape = Shape(dimensions=(Dim(codes=5000, depth=4), Dim(codes=12)), observations=100_000, languages=("en", "fr"))
    synthetic_catalog("out/stress", shape, seed=7)
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from sdmxlib.bundle import Producer
from sdmxlib.testing import synthetic_bundle

from statcatlib.bundle import Embedded, publish_bundle
from statcatlib.sources.sdmx import prepare_sdmx
from statcatlib.store import Catalog

if TYPE_CHECKING:
    from sdmxlib.bundle import Manifest
    from sdmxlib.testing import Shape

__all__ = ["synthetic_catalog"]


def synthetic_catalog(
    path: str | Path,
    shape: Shape,
    *,
    seed: int = 0,
    index: bool = True,
    bundle_id: str = "synthetic",
) -> Manifest:
    """Build a published statcat bundle of the given shape at *path*.

    The structures and observations are `sdmxlib.testing.synthetic_bundle`'s.
    Each dataflow becomes an `AggregateDataset`, the search index is built
    unless *index* is false (it needs ``statcatlib[search]``), and the bundle
    is published last with `statcatlib.bundle.publish_bundle`.

    Args:
        path: The bundle directory to create. It must not hold a bundle already.
        shape: Codelist sizes, hierarchy depth, observation count and languages.
        seed: What makes the bundle reproducible.
        index: Whether to build the search index.
        bundle_id: The manifest's ``bundle_id``.

    Returns:
        The manifest written.
    """
    root = Path(path)
    synthetic_bundle(root, shape, seed=seed)
    with Catalog(root, data_path=Embedded()) as cat:
        with cat.bulk_load():
            cat.put(prepare_sdmx(cat.registry))
        if index:
            cat.build_index()
    producer = Producer(name="statcatlib.testing", version=metadata.version("statcatlib"))
    return publish_bundle(root, bundle_id=bundle_id, producer=producer, product={"seed": seed})

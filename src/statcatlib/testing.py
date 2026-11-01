"""Throwaway catalogs and synthetic statcat editions, for tests and benchmarks (GH-3).

`temporary_catalog` creates a catalog in a temporary directory, which replaces
statcatlib 0.11's ``Catalog(":memory:")``. `synthetic_catalog` builds a sealed
edition of any shape: `sdmxlib.testing.synthetic_edition` builds a conforming
sdmxlib edition from a `sdmxlib.testing.Shape` and a seed, and statcatlib adds
the catalog, a dataset per dataflow and a search index over it. The same seed
builds the same structures and observations, so a failure reproduces.

Example:
    from sdmxlib.testing import Dim, Shape

    from statcatlib import Catalog
    from statcatlib.testing import synthetic_catalog

    shape = Shape(dimensions=(Dim(codes=5000, depth=4), Dim(codes=12)), observations=100_000, languages=("en", "fr"))
    edition = synthetic_catalog("out/stress", shape, seed=7)
    with Catalog.open("out/stress") as cat:
        view = cat.product(edition.product_id).view()
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from sdmxlib.testing import synthetic_edition

from statcatlib.catalog import Catalog
from statcatlib.sources.sdmx import prepare_sdmx

if TYPE_CHECKING:
    from collections.abc import Generator

    from sdmxlib.testing import Shape

    from statcatlib.store.working_copy import Edition

__all__ = ["synthetic_catalog", "temporary_catalog"]


@contextlib.contextmanager
def temporary_catalog() -> Generator[Catalog]:
    """A writable catalog in a temporary directory, deleted when the block ends.

    Yields:
        The catalog, from `Catalog.create`.
    """
    with tempfile.TemporaryDirectory(prefix="statcat-") as tmp, Catalog.create(Path(tmp) / "catalog") as cat:
        yield cat


def synthetic_catalog(path: str | Path, shape: Shape, *, product_id: str = "synthetic", seed: int = 0) -> Edition:
    """Create a catalog at *path* holding one sealed synthetic edition of the given shape.

    The structures and observations are `sdmxlib.testing.synthetic_edition`'s,
    built straight into the product's unsealed edition, which a working copy
    then resumes. Each dataflow becomes an `AggregateDataset`. The seal builds
    the search index when ``statcatlib[search]`` is installed, and records the
    edition live.

    Args:
        path: The catalog directory to create. It must not exist or must be empty.
        shape: Codelist sizes, hierarchy depth, observation count and languages.
        product_id: The product to build.
        seed: What makes the edition reproducible.

    Returns:
        The sealed edition.
    """
    with Catalog.create(path) as cat:
        product_dir = Path(path).absolute() / "products" / product_id
        synthetic_edition(product_dir, shape, seed=seed, seal=False).close()
        wc = cat.product(product_id).working_copy(resume=True)
        with wc.bulk_load():
            wc.put(prepare_sdmx(wc.registry))
        return wc.seal()

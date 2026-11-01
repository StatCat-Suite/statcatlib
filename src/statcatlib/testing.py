"""Synthetic statcat editions, for tests and benchmarks at any shape (GH-3).

`sdmxlib.testing.synthetic_edition` builds a conforming sdmxlib edition from a
`sdmxlib.testing.Shape` and a seed. `synthetic_catalog` stops it before the
seal and adds the catalog: a catalog dataset per dataflow and a search index
over it. The same seed builds the same structures and observations, so a
failure reproduces.

The caller seals the edition. Close the catalog, then the registry, and only
then call `sdmxlib.edition.seal` on ``edition_dir``.

Example:
    from sdmxlib.edition import Producer, seal
    from sdmxlib.testing import Dim, Shape

    from statcatlib.bundle import catalog_section
    from statcatlib.testing import synthetic_catalog

    shape = Shape(dimensions=(Dim(codes=5000, depth=4), Dim(codes=12)), observations=100_000, languages=("en", "fr"))
    rec = synthetic_catalog("out/stress", shape, seed=7)
    section = catalog_section(rec.cat)
    rec.cat.close()
    rec.reg.close()
    seal(rec.edition_dir, product_id="stress", producer=Producer(name="bench", version="1"),
         extensions={"statcat": section})
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from attrs import frozen
from sdmxlib.testing import synthetic_edition

from statcatlib.sources.sdmx import prepare_sdmx
from statcatlib.store import Catalog

if TYPE_CHECKING:
    from pathlib import Path

    from sdmxlib import LocalRegistry
    from sdmxlib.testing import Shape

__all__ = ["SyntheticCatalog", "synthetic_catalog"]


@frozen
class SyntheticCatalog:
    """An unsealed synthetic edition holding a catalog, as `synthetic_catalog` returns it.

    Attributes:
        cat: The catalog over the edition's store, open.
        reg: The edition's builder registry, open and writable.
        edition_dir: The unsealed edition's directory.
    """

    cat: Catalog
    reg: LocalRegistry
    edition_dir: Path


def synthetic_catalog(path: str | Path, shape: Shape, *, seed: int = 0, index: bool = True) -> SyntheticCatalog:
    """Build an unsealed statcat edition of the given shape at *path*.

    The structures and observations are `sdmxlib.testing.synthetic_edition`'s.
    Each dataflow becomes an `AggregateDataset`, and the search index is built
    in ``<edition_dir>/search/`` unless *index* is false (it needs
    ``statcatlib[search]``). The catalog and the registry are returned open.
    The caller closes ``cat``, then ``reg``, and only then may seal
    ``edition_dir`` with `sdmxlib.edition.seal`.

    Args:
        path: The product directory to create. It must not exist or must be empty.
        shape: Codelist sizes, hierarchy depth, observation count and languages.
        seed: What makes the edition reproducible.
        index: Whether to build the search index.

    Returns:
        The open catalog, its registry and the edition directory.
    """
    reg = synthetic_edition(path, shape, seed=seed, seal=False)
    edition_dir = reg.edition_dir
    cat = Catalog.from_connection(reg.connection, search_dir=edition_dir / "search")
    with cat.bulk_load():
        cat.put(prepare_sdmx(cat.registry))
    if index:
        cat.build_index()
    return SyntheticCatalog(cat=cat, reg=reg, edition_dir=edition_dir)

"""Throwaway catalogs and synthetic statcat editions, for tests and benchmarks (GH-3, GH-51).

`temporary_catalog` creates a catalog in a temporary directory, which replaces
statcatlib 0.11's ``Catalog(":memory:")``. `synthetic_catalog` builds a sealed
edition of any shape: `sdmxlib.testing.synthetic_edition` builds a conforming
sdmxlib edition from a `sdmxlib.testing.Shape` and a seed, and statcatlib adds
the catalog, a dataset per dataflow and a search index over it. The same seed
builds the same structures and observations, so a failure reproduces.

`codr_structures` and `codr_product` build a catalog shaped like Statistics
Canada's CODR tables: three structure products (`STRUCTURE_PRODUCTS`) holding
the full codelists, the geography hierarchy and the concept scheme, and data
products of one or more tables whose codelists are cut to partials. A
`CodrShape` sets the sizes, and one seed gives one set of artefact
fingerprints, so products generated in separate calls match the structure
products.

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
import hashlib
import importlib.util
import math
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import sdmxlib as sl
from attrs import field, frozen
from sdmxlib.testing import synthetic_edition

from statcatlib.admin import CatalogAdmin
from statcatlib.catalog import Catalog
from statcatlib.sources.sdmx import ingest_dataflows, prepare_sdmx
from statcatlib.store.store import CodelistIndexPolicy
from statcatlib.store.working_copy import WorkingCopyExistsError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from sdmxlib.testing import Shape

    from statcatlib.store.working_copy import Edition, WorkingCopy

__all__ = [
    "CODR_AGENCY",
    "STRUCTURE_PRODUCTS",
    "CodrShape",
    "CorpusGroup",
    "codr_corpus",
    "codr_product",
    "codr_structures",
    "codr_working_copy",
    "corpus_embargoed",
    "corpus_product_id",
    "synthetic_catalog",
    "temporary_catalog",
]

#: The three structure products `codr_structures` builds, split by change rate (design decision O1).
STRUCTURE_PRODUCTS: tuple[str, ...] = ("structure-geo", "structure-classifications", "structure-concepts")

#: The maintenance agency of every generated artefact but the status codelist.
CODR_AGENCY = "CODR"

#: The agency of the observation status codelist: an SDMX-agency codelist, as CODR tables use.
_SDMX_AGENCY = "SDMX"

#: Every generated name is in these languages.
_LANGUAGES = ("en", "fr")

#: The share of the geography's leaves one table uses, with their ancestors.
_GEOGRAPHY_SHARE = 0.05

#: The share of each classification's codes one table uses.
_CLASSIFICATION_SHARE = 0.25

#: The units of measure `CL_UNIT` holds, and how many one table uses.
_UNITS = ("NUMBER", "PERCENT", "DOLLARS", "RATE", "INDEX")
_UNITS_PER_TABLE = 2

#: The observation statuses `CL_OBS_STATUS` holds, and how many one table uses.
_STATUSES = ("A", "B", "E", "F", "M")
_STATUSES_PER_TABLE = 2

#: The reference periods a table's observations span.
_YEARS = tuple(str(year) for year in range(2015, 2025))

#: How many levels the first two classifications have; the others are flat.
_CLASSIFICATION_DEPTH = 3

_MASK = (1 << 64) - 1


@frozen
class CodrShape:
    """The sizes of a generated CODR catalog.

    Attributes:
        geography_codes: The codes of ``CL_GEO``.
        geography_depth: Its levels, from the country down.
        classification_codes: The codes of each classification codelist,
            ``CL_CLASS_1`` onward; one dimension per entry. The first two have
            three levels, the others one.
        observations: The rows of each table.
    """

    geography_codes: int = 6000
    geography_depth: int = 5
    classification_codes: tuple[int, ...] = field(default=(900, 500, 300), converter=tuple)
    observations: int = 2000


#: `codr_structures` and `codr_product`'s default shape.
_DEFAULT_SHAPE = CodrShape()


class _Draws:
    """A seeded stream of integers (SplitMix64), the same on every platform and Python release."""

    def __init__(self, seed: int, stream: str) -> None:
        digest = hashlib.sha256(f"{seed}/{stream}".encode()).digest()
        self._state = int.from_bytes(digest[:8], "big")

    def below(self, n: int) -> int:
        """An integer in ``[0, n)``."""
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return (z ^ (z >> 31)) % n

    def sample(self, items: Sequence[str], k: int) -> list[str]:
        """*k* distinct items of *items*, in a seeded order."""
        pool = list(items)
        for i in range(min(k, len(pool))):
            j = i + self.below(len(pool) - i)
            pool[i], pool[j] = pool[j], pool[i]
        return pool[:k]


@contextlib.contextmanager
def temporary_catalog() -> Generator[Catalog]:
    """A writable catalog in a temporary directory, deleted when the block ends.

    Yields:
        The catalog, from `Catalog.create`.
    """
    with tempfile.TemporaryDirectory(prefix="statcat-") as tmp, Catalog.create(Path(tmp) / "catalog") as cat:
        yield cat


def _names(text: str) -> dict[str, str]:
    return {lang: f"{text} {lang}" for lang in _LANGUAGES}


def _parents(count: int, depth: int, draws: _Draws) -> list[int | None]:
    """Each item's parent by index: trees of at most *depth* levels; a parent's index is below its children's."""
    if depth <= 1:
        return [None] * count
    fanout = max(2, round(count ** (1 / (depth - 1))))
    low, high = max(1, fanout // 2), fanout + fanout // 2
    parents: list[int | None] = []
    while len(parents) < count:
        parents.append(None)
        level = [len(parents) - 1]
        for _ in range(1, depth):
            below: list[int] = []
            for parent in level:
                for _ in range(low + draws.below(high - low + 1)):
                    if len(parents) >= count:
                        break
                    parents.append(parent)
                    below.append(len(parents) - 1)
            level = below
            if not level or len(parents) >= count:
                break
    return parents


def _codelist(
    codelist_id: str, prefix: str, count: int, depth: int, draws: _Draws, *, agency: str = CODR_AGENCY
) -> sl.Codelist:
    width = len(str(max(count - 1, 0)))
    ids = [f"{prefix}{i:0{width}d}" for i in range(count)]
    parents = _parents(count, depth, draws)
    return sl.Codelist(
        id=codelist_id,
        maintainer=agency,
        version="1.0",
        name=_names(codelist_id),
        codes=[
            sl.Code(id=code_id, name=_names(code_id), parent_id=None if parent is None else ids[parent])
            for code_id, parent in zip(ids, parents, strict=True)
        ],
    )


def _flat(codelist_id: str, ids: Sequence[str], *, agency: str = CODR_AGENCY) -> sl.Codelist:
    return sl.Codelist(
        id=codelist_id,
        maintainer=agency,
        version="1.0",
        name=_names(codelist_id),
        codes=[sl.Code(id=code_id, name=_names(code_id)) for code_id in ids],
    )


def _hierarchy(codelist: sl.Codelist) -> sl.Hierarchy:
    """``HCL_GEO``: one node per code of *codelist*, placed under the code's parent."""
    children: dict[str, list[str]] = {}
    for code in codelist.codes:
        if code.parent_id is not None:
            children.setdefault(code.parent_id, []).append(code.id)
    built: dict[str, sl.HierarchicalCode] = {}
    for code in reversed(list(codelist.codes)):
        below = [built[child] for child in children.get(code.id, ())]
        built[code.id] = sl.HierarchicalCode(id=code.id, code=codelist.ref_to(code.id), children=below)
    return sl.Hierarchy(
        id="HCL_GEO",
        maintainer=CODR_AGENCY,
        version="1.0",
        name=_names("HCL_GEO"),
        tree=[built[code.id] for code in codelist.codes if code.parent_id is None],
    )


@frozen
class _Structures:
    """The generated structures, by the structure product that holds them."""

    geography: sl.Codelist
    hierarchy: sl.Hierarchy
    classifications: tuple[sl.Codelist, ...]
    unit: sl.Codelist
    status: sl.Codelist
    concepts: sl.ConceptScheme

    def by_product(self) -> dict[str, list[sl.MaintainableArtefact]]:
        """The artefacts of each structure product."""
        return {
            STRUCTURE_PRODUCTS[0]: [self.geography, self.hierarchy],
            STRUCTURE_PRODUCTS[1]: [*self.classifications, self.unit, self.status],
            STRUCTURE_PRODUCTS[2]: [self.concepts],
        }


def _structures(seed: int, shape: CodrShape) -> _Structures:
    """The structures, from *seed* and *shape* alone."""
    draws = _Draws(seed, "structures")
    geography = _codelist("CL_GEO", "G", shape.geography_codes, shape.geography_depth, draws)
    classifications = tuple(
        _codelist(f"CL_CLASS_{n}", f"C{n}_", count, _CLASSIFICATION_DEPTH if n <= 2 else 1, draws)
        for n, count in enumerate(shape.classification_codes, start=1)
    )
    unit = _flat("CL_UNIT", _UNITS)
    status = _flat("CL_OBS_STATUS", _STATUSES, agency=_SDMX_AGENCY)
    coded = [
        ("GEO", geography),
        *((f"CLASS_{n}", cl) for n, cl in enumerate(classifications, start=1)),
        ("UNIT_MEASURE", unit),
        ("OBS_STATUS", status),
    ]
    concepts = sl.ConceptScheme(
        id="CS_CODR",
        maintainer=CODR_AGENCY,
        version="1.0",
        name=_names("CS_CODR"),
        concepts=[
            *(
                sl.Concept(id=concept_id, name=_names(concept_id), core_representation=sl.Ref.to(codelist))
                for concept_id, codelist in coded
            ),
            sl.Concept(id="TIME_PERIOD", name=_names("TIME_PERIOD")),
            sl.Concept(id="OBS_VALUE", name=_names("OBS_VALUE")),
        ],
    )
    return _Structures(
        geography=geography,
        hierarchy=_hierarchy(geography),
        classifications=classifications,
        unit=unit,
        status=status,
        concepts=concepts,
    )


def _sdmx_id(text: str) -> str:
    """*text* as an SDMX id: each character outside letters, digits, ``_`` and ``-`` becomes ``_``."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def _table(product_id: str, n: int, structures: _Structures) -> tuple[sl.DataStructure, sl.Dataflow]:
    """Table *n* of *product_id*: its data structure and dataflow."""
    cs = structures.concepts
    name = f"{_sdmx_id(product_id)}_{n}"
    dsd = sl.DataStructure(
        id=f"DSD_{name}",
        maintainer=CODR_AGENCY,
        version="1.0",
        name=_names(f"DSD_{name}"),
        dimensions=[
            sl.Dimension(id="GEO", concept=cs.ref_to("GEO"), representation=sl.Ref.to(structures.geography)),
            *(
                sl.Dimension(id=f"CLASS_{k}", concept=cs.ref_to(f"CLASS_{k}"), representation=sl.Ref.to(cl))
                for k, cl in enumerate(structures.classifications, start=1)
            ),
        ],
        time_dimension=sl.TimeDimension(id="TIME_PERIOD", concept=cs.ref_to("TIME_PERIOD")),
        measures=[sl.Measure(id="OBS_VALUE", concept=cs.ref_to("OBS_VALUE"), representation=sl.Facet(type="Double"))],
        attributes=[
            sl.Attribute(
                id=attribute,
                concept=cs.ref_to(attribute),
                representation=sl.Ref.to(codelist),
                attachment=sl.AttachmentLevel.OBSERVATION,
            )
            for attribute, codelist in (("UNIT_MEASURE", structures.unit), ("OBS_STATUS", structures.status))
        ],
    )
    flow = sl.Dataflow(
        id=f"DF_{name}", maintainer=CODR_AGENCY, version="1.0", name=_names(f"DF_{name}"), structure=sl.Ref.to(dsd)
    )
    return dsd, flow


def _with_ancestors(codelist: sl.Codelist, ids: Sequence[str]) -> list[str]:
    parent = {code.id: code.parent_id for code in codelist.codes}
    found: set[str] = set()
    for code_id in ids:
        current: str | None = code_id
        while current is not None and current not in found:
            found.add(current)
            current = parent[current]
    return sorted(found)


def _observations(product_id: str, n: int, seed: int, shape: CodrShape, structures: _Structures) -> pa.Table:
    """Table *n*'s rows: distinct keys over a seeded share of each codelist's codes.

    A shape whose table codes hold fewer keys than it asks rows raises
    `ValueError`.
    """
    draws = _Draws(seed, f"{product_id}/{n}")
    geography = structures.geography
    parents = {code.parent_id for code in geography.codes}
    leaves = [code.id for code in geography.codes if code.id not in parents]
    geo = _with_ancestors(geography, draws.sample(leaves, max(1, round(_GEOGRAPHY_SHARE * len(leaves)))))
    classes = [
        draws.sample([code.id for code in cl.codes], max(1, round(_CLASSIFICATION_SHARE * len(cl.codes))))
        for cl in structures.classifications
    ]
    units = draws.sample(_UNITS, _UNITS_PER_TABLE)
    statuses = draws.sample(_STATUSES, _STATUSES_PER_TABLE)
    axes = [geo, *classes, list(_YEARS)]
    space = math.prod(len(axis) for axis in axes)
    if shape.observations > space:
        msg = f"{shape.observations} observations do not fit the {space} keys a table of this shape uses"
        raise ValueError(msg)
    keys: set[tuple[str, ...]] = set()
    while len(keys) < shape.observations:
        keys.add(tuple(axis[draws.below(len(axis))] for axis in axes))
    rows = sorted(keys)
    columns: dict[str, list[str] | list[float]] = {"GEO": [row[0] for row in rows]}
    for k in range(1, len(classes) + 1):
        columns[f"CLASS_{k}"] = [row[k] for row in rows]
    columns["TIME_PERIOD"] = [row[-1] for row in rows]
    columns["OBS_VALUE"] = [draws.below(1_000_000) / 10 for _ in rows]
    columns["UNIT_MEASURE"] = [units[draws.below(len(units))] for _ in rows]
    columns["OBS_STATUS"] = [statuses[draws.below(len(statuses))] for _ in rows]
    return pa.table(columns)


@contextlib.contextmanager
def _scratch(artefacts: Sequence[sl.MaintainableArtefact]) -> Generator[sl.LocalRegistry]:
    """A scratch registry holding *artefacts*, in a temporary directory deleted when the block ends."""
    with tempfile.TemporaryDirectory(prefix="statcat-codr-") as tmp, sl.LocalRegistry.build(Path(tmp)) as scratch:
        scratch.add_all(list(artefacts))
        yield scratch


def codr_structures(catalog: Catalog, *, seed: int = 0, shape: CodrShape = _DEFAULT_SHAPE) -> dict[str, Edition]:
    """Seal the three CODR structure products into *catalog*, in one `Catalog.seal`.

    The artefacts depend on *seed* and *shape* alone: agency ``CODR``,
    version ``1.0``, names ``"<id> en"`` and ``"<id> fr"``, no validity dates
    and no annotations. ``structure-geo`` holds ``CL_GEO`` and ``HCL_GEO``, a
    hierarchy over all its codes. ``structure-classifications`` holds
    ``CL_CLASS_1`` onward, ``CL_UNIT`` and the SDMX-agency ``CL_OBS_STATUS``,
    so every codelist a table cuts has a structure home.
    ``structure-concepts`` holds ``CS_CODR``, whose coded concepts name those
    codelists, so its publish check resolves references in the other two.

    Args:
        catalog: A writable catalog.
        seed: Chooses the codelists' trees.
        shape: The sizes.

    Returns:
        Each structure product's new edition.

    Raises:
        RuntimeError: `Catalog.seal` refused a structure product; the message
            names each refusal, and the first is the cause.
    """
    structures = _structures(seed, shape)
    with _scratch([a for artefacts in structures.by_product().values() for a in artefacts]) as scratch:
        batch = []
        for product_id, artefacts in structures.by_product().items():
            wc = catalog.product(product_id).working_copy(kind="structure")
            wc.fill_structures(scratch, artefacts=[a.urn for a in artefacts])
            batch.append(wc)
        outcomes = catalog.seal(batch)
    if outcomes.refused:
        listed = "; ".join(f"{product_id}: {exc}" for product_id, exc in outcomes.refused.items())
        msg = f"the CODR structure products were refused: {listed}"
        raise RuntimeError(msg) from next(iter(outcomes.refused.values()))
    return dict(outcomes.sealed)


def codr_working_copy(
    catalog: Catalog,
    product_id: str,
    *,
    tables: int = 1,
    seed: int = 0,
    shape: CodrShape = _DEFAULT_SHAPE,
) -> WorkingCopy:
    """Fill a CODR-shaped data product's working copy of *tables* tables, with its codelists cut to partials.

    `codr_product` seals what this returns; a caller sealing several products
    in one `Catalog.seal` takes the working copies from here. It seals the
    three structure products first (`codr_structures`) unless all three are
    live. Each table ``n`` gets a data structure, ``DSD_<product_id>_<n>``
    (dimensions ``GEO`` and ``CLASS_1`` onward, then ``TIME_PERIOD``;
    attributes ``UNIT_MEASURE`` and ``OBS_STATUS``; measure ``OBS_VALUE``), a
    dataflow ``DF_<product_id>_<n>``, and ``shape.observations`` rows over a
    share of each codelist's codes: 5 % of the geography's leaves with their
    ancestors, a quarter of each classification, two units and two statuses.
    `ingest_dataflows` fills the product's working copy with
    ``partials=True``, so the seal records each partial's ``partial_of``. No
    hierarchy association names a table, so ``CL_GEO`` is cut. When lancedb
    is installed, the search index takes the geography's codes with
    ``CodelistIndexPolicy`` in ``fts_only`` mode.

    The structures depend on *seed* alone, and the rows on *seed* and
    *product_id*. Generate every product of a catalog with the seed and the
    shape its structure products were generated with; another seed gives
    other fingerprints, whose partials the publish check refuses.

    Args:
        catalog: A writable catalog.
        product_id: The data product.
        tables: The tables, one dataflow each.
        seed: Chooses the structures and the rows.
        shape: The sizes.

    Returns:
        The filled working copy, unsealed.
    """
    catalog.refresh()
    if not set(STRUCTURE_PRODUCTS) <= set(catalog.products()):
        codr_structures(catalog, seed=seed, shape=shape)
    structures = _structures(seed, shape)
    built = [_table(product_id, n, structures) for n in range(1, tables + 1)]
    held = [a for artefacts in structures.by_product().values() for a in artefacts]
    with _scratch([*held, *(a for pair in built for a in pair)]) as scratch:
        wc = catalog.product(product_id).working_copy()
        flows = [flow.urn for _, flow in built]
        rows = {
            str(flow.urn): _observations(product_id, n, seed, shape, structures) for n, (_, flow) in enumerate(built, 1)
        }
        ingest_dataflows(wc, scratch, flows, observations=rows, partials=True)
    if importlib.util.find_spec("lancedb") is not None:
        # The geography's codes go in the search index whatever their number,
        # without embeddings, as a CODR producer indexes them.
        wc.build_index(codelist_policies=[CodelistIndexPolicy(urns=(str(structures.geography.urn),))])
    return wc


def codr_product(
    catalog: Catalog,
    product_id: str,
    *,
    tables: int = 1,
    seed: int = 0,
    shape: CodrShape = _DEFAULT_SHAPE,
) -> Edition:
    """Seal a CODR-shaped data product of *tables* tables into *catalog*, with its codelists cut to partials.

    `codr_working_copy` fills the working copy, and this seals it.

    Args:
        catalog: A writable catalog.
        product_id: The data product.
        tables: The tables, one dataflow each.
        seed: Chooses the structures and the rows.
        shape: The sizes.

    Returns:
        The sealed edition.
    """
    return codr_working_copy(catalog, product_id, tables=tables, seed=seed, shape=shape).seal()


@frozen
class CorpusGroup:
    """One group of a `codr_corpus`: *products* data products of *tables* tables each.

    Attributes:
        products: The data products in the group.
        tables: The tables of each, one dataflow each.
    """

    products: int
    tables: int = 1

    def __attrs_post_init__(self) -> None:
        """Refuse a negative product count and a product of no table.

        Raises:
            ValueError: *products* is negative or *tables* is below 1.
        """
        if self.products < 0 or self.tables < 1:
            msg = f"a corpus group holds zero or more products of one or more tables, not {self!r}"
            raise ValueError(msg)


def corpus_product_id(group: CorpusGroup, index: int) -> str:
    """The id of the *index*-th product of *group*: ``codr-<tables>t-<index, five digits>``.

    Args:
        group: The group.
        index: The product's place in the group, from 0.

    Returns:
        The product id.
    """
    return f"codr-{group.tables}t-{index:05d}"


def corpus_embargoed(product_id: str, *, seed: int = 0, embargoed: float = 0.1) -> bool:
    """Whether `codr_corpus` seals *product_id* under embargo: a seeded draw below the share *embargoed*.

    Args:
        product_id: The product.
        seed: The corpus seed.
        embargoed: The share of products sealed under embargo.

    Returns:
        Whether the product seals under embargo.
    """
    return _Draws(seed, f"embargo/{product_id}").below(1_000_000) < round(embargoed * 1_000_000)


def _corpus_product(catalog: Catalog, product_id: str, *, tables: int, seed: int, shape: CodrShape) -> Edition:
    """`codr_product`, after discarding an unsealed working copy a crash left."""
    try:
        return codr_product(catalog, product_id, tables=tables, seed=seed, shape=shape)
    except WorkingCopyExistsError:
        catalog.product(product_id).working_copy(resume=True).discard()
        return codr_product(catalog, product_id, tables=tables, seed=seed, shape=shape)


def codr_corpus(
    catalog: Catalog,
    groups: Sequence[CorpusGroup] = (CorpusGroup(7000),),
    *,
    seed: int = 0,
    shape: CodrShape = _DEFAULT_SHAPE,
    embargoed: float = 0.1,
    on_sealed: Callable[[str, Edition], None] | None = None,
) -> list[str]:
    """Seal a corpus of CODR data products into *catalog*, with the structure products they are cut from.

    Each group's products are named by `corpus_product_id` and built by
    `codr_product` with *seed* and *shape*, so every product cuts its
    codelists from the same structure editions, and each product's rows come
    from its id. A product the Inventory records already, in any state, is
    skipped, so a call with larger groups grows a corpus a smaller call
    built, and a call after a crash resumes it, discarding the unsealed
    working copy the crash left. A share *embargoed* of the products, chosen
    by `corpus_embargoed`, seals with the catalog's embargo on, at the cost
    of two ``embargo-change`` versions each, so the corpus holds live and
    embargoed data entries. The embargo is off when the call returns.

    Args:
        catalog: A writable catalog.
        groups: The groups, sealed in order.
        seed: Chooses the structures and every product's rows.
        shape: The sizes.
        embargoed: The share of products sealed under embargo, from 0 to 1.
        on_sealed: Called with each product id and edition after its seal.

    Returns:
        The products this call sealed, in order.

    Raises:
        ValueError: *embargoed* is outside 0 to 1.
    """
    if not 0 <= embargoed <= 1:
        msg = f"embargoed is a share from 0 to 1, not {embargoed}"
        raise ValueError(msg)
    admin = CatalogAdmin.open(catalog._root)
    if admin.embargo:
        admin.set_embargo(on=False, reason="codr_corpus: seal the corpus with the embargo off")
    recorded = set(admin.products())
    sealed: list[str] = []
    for group in groups:
        for index in range(group.products):
            product_id = corpus_product_id(group, index)
            if product_id in recorded:
                continue
            under_embargo = corpus_embargoed(product_id, seed=seed, embargoed=embargoed)
            if under_embargo:
                admin.set_embargo(on=True, reason=f"codr_corpus: seal {product_id} under embargo")
            try:
                edition = _corpus_product(catalog, product_id, tables=group.tables, seed=seed, shape=shape)
            finally:
                if under_embargo:
                    admin.set_embargo(on=False, reason=f"codr_corpus: {product_id} sealed")
            sealed.append(product_id)
            if on_sealed is not None:
                on_sealed(product_id, edition)
    return sealed


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

"""Shared fixtures for statcatlib tests."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import weakref
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sdmxlib import InternationalString, Ref
from sdmxlib.model.concept import Concept
from sdmxlib.model.urn import SdmxUrn

import statcatlib as sc

if TYPE_CHECKING:
    import duckdb
    import lancedb.table

    from statcatlib.store.lance import LanceIndex


def untyped(value: object) -> Any:
    """Erase a value's static type on the way to a runtime guard.

    Some tests exist to prove that passing the wrong type *fails* — that a
    `ClassVar` cannot be assigned, that `put()` rejects a non-string. A type
    checker is right to object to those call sites, so the intent is stated
    here once rather than as a suppression at each of them.
    """
    return value


class ScratchStore(sc.WorkingCopy, sc.EditionView):
    """A working copy that also answers its view's queries: one object, as 0.11's ``Catalog`` was.

    For the tests that write and read through one name (``store.put(...)``
    then ``store.query(...)``), so they stay as they were. It is a
    `WorkingCopy`, so ingest accepts it, and its reads are its own view's,
    which sees every write at once. Built from a working copy by
    `scratch_store`; it reaches private state on purpose.
    """

    @classmethod
    def _from(cls, wc: sc.WorkingCopy) -> ScratchStore:
        scratch = object.__new__(cls)
        scratch.__dict__.update(wc.__dict__)
        return scratch


#: Every `ScratchStore` opened, so `_close_scratch_stores` can close those a test leaves open.
_OPENED: list[ScratchStore] = []


@pytest.fixture(autouse=True)
def _close_scratch_stores() -> Iterator[None]:
    """Close the scratch stores a test opened and left open, when it ends.

    Each holds a DuckDB database, and each database its own thread pool, so
    stores left to the garbage collector exhaust the process's threads over a
    full run. Stores a module-scoped fixture opened are opened before this
    fixture starts, and stay open for the module.
    """
    start = len(_OPENED)
    yield
    for scratch in _OPENED[start:]:
        scratch.close()
    del _OPENED[start:]


def open_scratch(path: Path | None = None, product: str = "p", **options: Any) -> ScratchStore:
    """A `ScratchStore` over a new catalog at *path*, or in a temporary directory.

    The temporary directory is deleted when the store is garbage collected.
    *options* go to `Product.working_copy`.
    """
    owned = path is None
    root = Path(tempfile.mkdtemp(prefix="statcat-test-")) / "catalog" if path is None else path
    cat = sc.Catalog.create(root, exist_ok=True)
    scratch = ScratchStore._from(cat.product(product).working_copy(**options))
    _OPENED.append(scratch)
    weakref.finalize(scratch, cat.close)
    if owned:
        weakref.finalize(scratch, shutil.rmtree, root.parent, ignore_errors=True)
    return scratch


@contextlib.contextmanager
def scratch_store(path: Path | None = None, product: str = "p", **options: Any) -> Generator[ScratchStore]:
    """`open_scratch` as a context manager, which closes the working copy at the end."""
    scratch = open_scratch(path, product, **options)
    try:
        yield scratch
    finally:
        scratch.close()


def seal_and_view(store: sc.WorkingCopy) -> sc.EditionView:
    """Seal *store* and return a view of the edition it became, through a catalog opened read-only."""
    root = store._writer.root
    edition = store.seal()
    cat = sc.Catalog.open(root)
    view = cat.product(edition.product_id).view()
    weakref.finalize(view, cat.close)
    return view


def cursor_of(store: sc.EditionView) -> duckdb.DuckDBPyConnection:
    """The store's cursor on its edition, for raw SQL in a test (private on purpose)."""
    return store._cursor()


def require_index(store: sc.EditionView) -> LanceIndex:
    """The view's search index, narrowed from ``LanceIndex | None``.

    The store's ``_lance`` is `None` until an index is built, and a test that
    reaches for it has already built one. Narrowing here rather than with an
    `assert` at each of the call sites keeps the reason in one place — and
    gives a failure that names the cause instead of an `AttributeError` on
    `None`.
    """
    index = store._store._lance
    assert index is not None, "store has no search index — call build_index() first"
    return index


def search_table(store: sc.EditionView, lang: str = "en") -> lancedb.table.Table:
    """One language's Lance table, straight from a store.

    `require_index` then `require_table`; both narrowings are failed
    assumptions for a test that has already built an index.
    """
    return require_table(require_index(store), lang)


def require_table(index: LanceIndex, lang: str = "en") -> lancedb.table.Table:
    """One language's Lance table, narrowed from ``Table | None``.

    `LanceIndex.table_for` returns `None` for a language with no table, which
    for a test is a failed assumption rather than a branch to handle.
    """
    table = index.table_for(lang)
    assert table is not None, f"no search table for {lang!r}"
    return table


def concept_of(component_id: str) -> Ref[Concept]:
    """A concept identity for a fixture's DSD component or MSD attribute.

    sdmxlib 0.68 refuses a component without one as it is built. The
    reference is left unresolved and its scheme is never stored, so
    `sdmx.concept` holds no row for it and a dimension's name still falls
    back to its id, as it did before a concept was required.
    """
    urn = SdmxUrn.make(
        Concept,
        package="conceptscheme",
        artefact_class="Concept",
        agency="TEST",
        id="CS_TEST",
        version="1.0",
        item_id=component_id,
    )
    return Ref[Concept].unresolved(urn)


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _theme(name: str) -> sc.ConceptRef:
    """Build a ConceptRef for a test theme using a synthetic URI."""
    slug = name.lower().replace(" ", "_")
    return sc.ConceptRef(uri=f"urn:test:theme:{slug}", label=_i(name))


def _ds(
    identifier: str,
    title: str,
    description: str,
    publisher: str,
    theme_ids: list[str] | None = None,
    frequency: str | None = None,
    status: str | None = None,
) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=_i(title),
        description=_i(description),
        publisher=sc.Agent(name=_i(publisher), id=publisher),
        themes=[_theme(t) for t in (theme_ids or [])],
        frequency=sc.Frequency(id=frequency) if frequency else None,
        status=status,
    )


def _stat_ds(
    identifier: str,
    title: str,
    description: str,
    publisher: str,
    theme_ids: list[str] | None = None,
    frequency: str | None = None,
    status: str | None = None,
    num_series: int | None = None,
) -> sc.AggregateDataset:
    return sc.AggregateDataset(
        identifier=identifier,
        title=_i(title),
        description=_i(description),
        publisher=sc.Agent(name=_i(publisher), id=publisher),
        themes=[_theme(t) for t in (theme_ids or [])],
        frequency=sc.Frequency(id=frequency) if frequency else None,
        status=status,
        num_series=num_series,
    )


#: Declared as `sc.Dataset`, the union of the concrete kinds, because that is
#: what `put()` accepts. `list[sc.BaseDataset]` was wrong the moment the
#: signature narrowed: every object here is concrete, but the
#: annotation promised a base class `put()` does not take.
FIXTURE_DATASETS: list[sc.Dataset] = [
    _stat_ds(
        "EXR",
        "Exchange Rate EUR/USD",
        "Daily exchange rates published by ECB",
        "ECB",
        ["Economy", "Finance"],
        "DAILY",
        "current",
        5,
    ),
    _stat_ds("M1", "Money Supply M1", "Monthly money supply aggregates", "ECB", ["Economy"], "MONTHLY", "current", 12),
    _stat_ds(
        "UNEMP",
        "Unemployment Rate",
        "Quarterly unemployment by member state",
        "ESTAT",
        ["Labour"],
        "QUARTERLY",
        "current",
        80,
    ),
    _stat_ds(
        "GDP",
        "GDP Growth",
        "Annual GDP growth rate for EU member states",
        "ESTAT",
        ["Economy"],
        "ANNUAL",
        "current",
        30,
    ),
    _ds("CO2", "CO2 Emissions", "Annual CO2 emissions by country", "OECD", ["Environment"], "ANNUAL", "current"),
    _ds(
        "LFS",
        "Labour Force Survey",
        "Monthly labour force participation survey",
        "StatCan",
        ["Labour"],
        "MONTHLY",
        "current",
    ),
    _stat_ds("BOND", "Bond Yields", "Daily government bond yields", "ECB", ["Finance"], "DAILY", "current", 20),
    _ds("TRADE", "Trade Balance", "Quarterly trade balance statistics", "OECD", ["Economy"], "QUARTERLY", "current"),
    _ds(
        "ENERGY",
        "Energy Consumption",
        "Annual energy consumption by sector",
        "ESTAT",
        ["Environment"],
        "ANNUAL",
        "current",
    ),
    _stat_ds(
        "GDP_PROV",
        "GDP by Province",
        "Quarterly GDP broken down by Canadian province",
        "StatCan",
        ["Economy"],
        "QUARTERLY",
        "current",
        13,
    ),
    _ds("LEND", "Bank Lending Rates", "Monthly bank lending rates", "ECB", ["Finance"], "MONTHLY", "archived"),
    _stat_ds(
        "EMP_SEC",
        "Employment by Sector",
        "Monthly employment statistics by economic sector",
        "ESTAT",
        ["Labour"],
        "MONTHLY",
        "archived",
        50,
    ),
]


@pytest.fixture(scope="module")
def indexed_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sc.EditionView]:
    """The fixture datasets, sealed, as a view of the sealed edition: the seal builds the BM25 index.

    Module-scoped: the seal's index build is the expensive part, and the
    tests only read.
    """
    pytest.importorskip("lancedb", reason="search needs statcatlib[search]")
    path = tmp_path_factory.mktemp("indexed") / "catalog"
    with sc.Catalog.create(path) as catalog:
        wc = catalog.product("p").working_copy()
        wc.put(FIXTURE_DATASETS)
        wc.seal()
        catalog.refresh()
        yield catalog.product("p").view()


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[sc.Catalog]:
    """A writable catalog, created empty in the test's temporary directory."""
    with sc.Catalog.create(tmp_path / "catalog") as cat:
        yield cat


@pytest.fixture
def wc(catalog: sc.Catalog) -> Iterator[sc.WorkingCopy]:
    """A working copy of product ``p`` in `catalog`, closed at the end of the test."""
    working_copy = catalog.product("p").working_copy()
    yield working_copy
    working_copy.close()


@pytest.fixture
def view(wc: sc.WorkingCopy) -> sc.EditionView:
    """`wc`'s view, taken once: it sees later writes, since it shares the working copy's store."""
    return wc.view()


@pytest.fixture
def sealed_view(catalog: sc.Catalog, wc: sc.WorkingCopy) -> sc.EditionView:
    """The fixture datasets, sealed from `wc`, as ``catalog.product("p").view()``."""
    wc.put(FIXTURE_DATASETS)
    wc.seal()
    catalog.refresh()
    return catalog.product("p").view()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ScratchStore]:
    """The fixture datasets in a `ScratchStore`: a working copy that also answers its view's queries."""
    with scratch_store(tmp_path / "catalog") as s:
        s.put(FIXTURE_DATASETS)
        yield s

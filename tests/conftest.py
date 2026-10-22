"""Shared fixtures for statcatlib tests."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

if TYPE_CHECKING:
    import lancedb.table

    from statcatlib.store.lance import LanceIndex


def untyped(value: object) -> Any:
    """Erase a value's static type on the way to a runtime guard.

    Some tests exist to prove that passing the wrong type *fails* — that a
    `ClassVar` cannot be assigned, that `put()` rejects a non-string. A type
    checker is right to object to those call sites, so the intent is stated
    here once rather than as a suppression at each of them (#116).
    """
    return value


def require_index(store: sc.Catalog) -> LanceIndex:
    """The store's search index, narrowed from ``LanceIndex | None``.

    `Catalog._lance` is `None` until `build_index` runs, and a test that
    reaches for it has already built one. Narrowing here rather than with an
    `assert` at each of the call sites keeps the reason in one place — and
    gives a failure that names the cause instead of an `AttributeError` on
    `None` (#116).
    """
    index = store._lance
    assert index is not None, "store has no search index — call build_index() first"
    return index


def search_table(store: sc.Catalog, lang: str = "en") -> lancedb.table.Table:
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
#: signature narrowed (!82): every object here is concrete, but the
#: annotation promised a base class `put()` does not take (#116).
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
def indexed_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sc.Catalog]:
    """The fixture catalog, folder-backed and with a BM25 index built.

    Separate from `store` because `search()` needs somewhere to put
    ``search/``; an in-memory catalog has nowhere. That is why the search
    tests were skipped rather than failing — removing the marker alone would
    not have made them pass (#117).

    Module-scoped: `build_index` is the expensive part, and the tests only
    read.
    """
    pytest.importorskip("lancedb", reason="search needs statcatlib[search]")
    path = tmp_path_factory.mktemp("indexed") / "catalog"
    with sc.Catalog(path) as catalog:
        catalog.put(FIXTURE_DATASETS)
        catalog.build_index()
        yield catalog


@pytest.fixture
def store() -> sc.Catalog:
    s = sc.Catalog().open()
    s.put(FIXTURE_DATASETS)
    return s

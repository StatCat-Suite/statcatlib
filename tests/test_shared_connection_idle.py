"""statcatlib leaves no unfinished result on the catalog's shared connection (GH-43).

DuckDB 2 counts a result as finished only after a fetch returns no rows, so
``execute(...).fetchone()`` on a one-row result leaves it pending. The relation
API (``conn.sql``, ``conn.table``) then refuses the connection with
``ResourceInUseException``. ``conn.execute`` replaces the pending result, which
is why each test probes with ``sql``.

Opening an indexed catalog was the reported case: the annotation fingerprint
check runs only when the index recorded annotations.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.store.schema import get_schema_version, schema_exists
from statcatlib.store.store import SEARCH_ANNOTATION_TYPES, annotation_fingerprint
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SYNTHETIC:CL_DIM_1(1.0)"


def _assert_idle(cat: sc.Catalog) -> None:
    assert cat.connection.sql("SELECT 1").fetchall() == [(1,)]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("idle") / "bundle"
    synthetic_catalog(path, Shape(dimensions=(Dim(codes=4),), observations=10, languages=("en",)))
    return path


@pytest.fixture
def cat(bundle: Path) -> Iterator[sc.Catalog]:
    with sc.Catalog(bundle, read_only=True) as opened:
        yield opened


def test_open_of_an_indexed_catalog(cat: sc.Catalog) -> None:
    assert cat.index_annotations is not None
    _assert_idle(cat)


def _dataset(cat: sc.Catalog) -> sc.AggregateDataset:
    ds = cat.query(sc.AggregateDataset).first()
    assert ds is not None
    return ds


_OPERATIONS: dict[str, Callable[[sc.Catalog], object]] = {
    "annotation_fingerprint": lambda cat: annotation_fingerprint(cat.connection, SEARCH_ANNOTATION_TYPES),
    "schema_exists": lambda cat: schema_exists(cat.connection),
    "get_schema_version": lambda cat: get_schema_version(cat.connection),
    "get_structure": lambda cat: cat.get_structure(_dataset(cat)),
    "codelist_for": lambda cat: cat.codelist_for(_dataset(cat).identifier, "DIM_1"),
    "dimension_collect": lambda cat: cat.dimensions(_dataset(cat))["DIM_1"].collect(),
    "codelists_len": lambda cat: len(cat.codelists),
    "codelist_label": lambda cat: cat.codelists[_CODELIST].label("en"),
    "code_dataset_count": lambda cat: cat.codelists[_CODELIST]["D1_0"].dataset_count(),
    "themes_len": lambda cat: len(cat.themes),
}


@pytest.mark.parametrize("operation", _OPERATIONS.values(), ids=_OPERATIONS.keys())
def test_single_row_reads(cat: sc.Catalog, operation: Callable[[sc.Catalog], object]) -> None:
    cat.connection.execute("SELECT 1").fetchall()  # clears whatever open() left, so only *operation* is tested
    operation(cat)
    _assert_idle(cat)

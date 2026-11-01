"""statcatlib leaves no unfinished result on a view's cursor (GH-43).

DuckDB 2 counts a result as finished only after a fetch returns no rows, so
``execute(...).fetchone()`` on a one-row result leaves it pending. The relation
API (``conn.sql``, ``conn.table``) then refuses the connection with
``ResourceInUseException``. ``conn.execute`` replaces the pending result, which
is why each test probes with ``sql``.

Opening an indexed edition was the reported case: the annotation fingerprint
check runs on the view's cursor when `Product.view` opens an index that
recorded annotations.
"""

from collections.abc import Callable, Iterator

import pytest
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.store.schema import get_schema_version, schema_exists
from statcatlib.store.store import SEARCH_ANNOTATION_TYPES, annotation_fingerprint
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SYNTHETIC:CL_DIM_1(1.0)"


def _assert_idle(view: sc.EditionView) -> None:
    assert view._cursor().sql("SELECT 1").fetchall() == [(1,)]


@pytest.fixture(scope="module")
def view(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sc.EditionView]:
    pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
    path = tmp_path_factory.mktemp("idle") / "catalog"
    edition = synthetic_catalog(path, Shape(dimensions=(Dim(codes=4),), observations=10, languages=("en",)))
    with sc.Catalog.open(path) as cat:
        yield cat.product(edition.product_id).view()


def test_open_of_an_indexed_edition(view: sc.EditionView) -> None:
    """`Product.view` ran the annotation fingerprint check on the view's cursor, and left it idle."""
    assert view.index_annotations is not None
    _assert_idle(view)


def _dataset(view: sc.EditionView) -> sc.AggregateDataset:
    ds = view.query(sc.AggregateDataset).first()
    assert ds is not None
    return ds


_OPERATIONS: dict[str, Callable[[sc.EditionView], object]] = {
    "annotation_fingerprint": lambda view: annotation_fingerprint(view._cursor(), SEARCH_ANNOTATION_TYPES),
    "schema_exists": lambda view: schema_exists(view._cursor()),
    "get_schema_version": lambda view: get_schema_version(view._cursor()),
    "get_structure": lambda view: view.get_structure(_dataset(view)),
    "codelist_for": lambda view: view.codelist_for(_dataset(view).identifier, "DIM_1"),
    "dimension_collect": lambda view: view.dimensions(_dataset(view))["DIM_1"].collect(),
    "codelists_len": lambda view: len(view.codelists),
    "codelist_label": lambda view: view.codelists[_CODELIST].label("en"),
    "code_dataset_count": lambda view: view.codelists[_CODELIST]["D1_0"].dataset_count(),
    "themes_len": lambda view: len(view.themes),
}


@pytest.mark.parametrize("operation", _OPERATIONS.values(), ids=_OPERATIONS.keys())
def test_single_row_reads(view: sc.EditionView, operation: Callable[[sc.EditionView], object]) -> None:
    view._cursor().execute("SELECT 1").fetchall()  # clears what came before, so only *operation* is tested
    operation(view)
    _assert_idle(view)

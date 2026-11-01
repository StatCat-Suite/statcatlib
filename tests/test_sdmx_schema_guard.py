"""The guard against an edition stored under another sdmxlib store schema.

sdmxlib migrates a cache by dropping its ``sdmx`` schema and rebuilding it
empty. For an edition that would be data loss: ``statcat.dataset`` would link
by URN into tables that hold nothing. sdmxlib refuses instead, with
`sdmxlib.StoreSchemaError`: `LocalRegistry.edition` checks the manifest's
``schema_version`` before it opens the store, which is `Product.view`'s path,
and `LocalRegistry.build` checks the copied store, which is
`Product.working_copy`'s. statcatlib adds no guard of its own on either path.

Another store schema is simulated by patching the version the running sdmxlib
reads, which leaves the edition's files as they were written.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
import sdmxlib as sl
from sdmxlib import InternationalString
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
import statcatlib.query as q
from tests.conftest import concept_of

pytestmark = pytest.mark.unit

_NEWER = "9.99"


def _registry() -> InMemoryRegistry:
    cl = sl.Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=InternationalString(en="Frequency"),
        codes=[sl.Code(id="A", name=InternationalString(en="Annual"))],
    )
    dsd = sl.DataStructure(
        id="DSD_GUARD",
        maintainer="TEST",
        name=InternationalString(en="Guard DSD"),
        dimensions=[sl.Dimension(id="FREQ", concept=concept_of("FREQ"), representation=cl)],
    )
    flow = sl.Dataflow(id="DF_GUARD", maintainer="TEST", name=InternationalString(en="Guard flow"), structure=dsd)
    registry = InMemoryRegistry()
    for artefact in (cl, dsd, flow):
        registry.add(artefact)
    return registry


def _artefact_count(meta: Path) -> int:
    with duckdb.connect(str(meta), read_only=True) as conn:
        row = conn.execute("SELECT count(*) FROM sdmx.artefact").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def meta(tmp_path: Path) -> Path:
    """The ``metadata.duckdb`` of a sealed edition holding SDMX structure."""
    with sc.Catalog.create(tmp_path / "cat") as cat:
        wc = cat.product("p").working_copy()
        sc.ingest_sdmx(wc, _registry())
        edition = wc.seal()
    return tmp_path / "cat" / "products" / "p" / edition.edition_id / "metadata.duckdb"


@pytest.fixture
def catalog(tmp_path: Path, meta: Path) -> Iterator[sc.Catalog]:
    del meta
    with sc.Catalog.create(tmp_path / "cat", exist_ok=True) as cat:
        yield cat


@pytest.fixture
def newer_sdmxlib(monkeypatch: pytest.MonkeyPatch, meta: Path) -> None:
    """The running sdmxlib reads another store schema than *meta*'s edition was sealed at."""
    del meta
    monkeypatch.setattr("sdmxlib.local.registry.SCHEMA_VERSION", _NEWER)


@pytest.mark.usefixtures("newer_sdmxlib")
class TestAnotherSchemaRefused:
    def test_a_view_raises_sdmxlibs_error(self, catalog: sc.Catalog) -> None:
        with pytest.raises(sl.StoreSchemaError, match=rf"store schema {SCHEMA_VERSION}.*schema {_NEWER}"):
            catalog.product("p").view()

    def test_the_structure_survives_a_refused_view(self, meta: Path, catalog: sc.Catalog) -> None:
        before = _artefact_count(meta)
        assert before > 0
        with pytest.raises(sl.StoreSchemaError):
            catalog.product("p").view()
        assert _artefact_count(meta) == before

    def test_a_working_copy_raises_and_leaves_no_edition(self, tmp_path: Path, meta: Path, catalog: sc.Catalog) -> None:
        product = tmp_path / "cat" / "products" / "p"
        before = sorted(p.name for p in product.iterdir())
        with pytest.raises(sl.StoreSchemaError):
            catalog.product("p").working_copy()
        assert sorted(p.name for p in product.iterdir()) == before
        assert _artefact_count(meta) > 0


class TestCurrentSchema:
    def test_a_sealed_edition_answers_structural_queries(self, catalog: sc.Catalog) -> None:
        result = catalog.product("p").view().query(sc.StatisticalDataset).filter(q.has_code("FREQ", "A")).execute()
        assert result.total == 1

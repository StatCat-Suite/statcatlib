"""Live integration tests against real SDMX REST endpoints.

These tests make real HTTP requests and are slow. They validate that the
ingest → query cycle works at realistic scale with real structural metadata.

Run with: uv run pytest -m live
"""

from collections.abc import Iterator

import pytest
import sdmxlib as sl

import statcatlib as sc
import statcatlib.query as q

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def estat_store() -> sc.Catalog:
    """Fetch NAMA_10_GDP from Eurostat and ingest into a catalog store.

    Fetches the dataflow + DSD + codelists (GEO, FREQ, UNIT, NA_ITEM).
    Uses module scope so the HTTP round-trip only happens once per test run.
    """
    store = sc.Catalog().open()
    with sl.RestRegistry(sl.Provider.ESTAT) as reg:
        # resolve=True follows every reference, pulling DSD + all codelists
        # in one request and populating reg.registry automatically
        urn = sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP")
        reg.get(urn, resolve=True)
        sc.ingest_sdmx(store, reg.registry)
    return store


@pytest.fixture(scope="module")
def estat_search_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sc.Catalog]:
    """Same ESTAT ingest, but folder-mode with a BM25 index built over it.

    Separate from `estat_store` because `search()` needs a folder — an
    in-memory catalog has nowhere to put ``search/``. That is why the test
    below was skipped rather than failing: removing the marker alone would
    not have made it pass (#115).

    The point of ingesting live rather than reusing a fixture catalog is
    that every other search test builds its documents itself. Nothing else
    checks that text arriving from a real registry reaches the index.
    """
    lancedb = pytest.importorskip("lancedb", reason="search needs statcatlib[search]")
    assert lancedb is not None
    path = tmp_path_factory.mktemp("estat_search") / "catalog"
    with sc.Catalog(path) as store:
        with sl.RestRegistry(sl.Provider.ESTAT) as reg:
            urn = sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP")
            reg.get(urn, resolve=True)
            sc.ingest_sdmx(store, reg.registry)
        store.build_index()
        yield store


class TestEstatIngest:
    def test_dataflow_ingested(self, estat_store: sc.Catalog):
        ds = estat_store.get(sc.StatisticalDataset, "ESTAT:NAMA_10_GDP(latest)")
        if ds is None:
            # version may differ — check by query
            results = estat_store.query(sc.StatisticalDataset).all()
            assert len(results) >= 1
            ds = results[0]
        assert isinstance(ds, sc.StatisticalDataset)

    def test_publisher_is_estat(self, estat_store: sc.Catalog):
        results = estat_store.query(sc.StatisticalDataset).all()
        assert len(results) >= 1
        ds = results[0]
        assert ds.publisher is not None
        assert ds.publisher.id == "ESTAT"

    def test_dimension_names_populated(self, estat_store: sc.Catalog):
        results = estat_store.query(sc.StatisticalDataset).all()
        ds = results[0]
        assert len(ds.dimension_names) >= 1

    def test_geo_codes_queryable(self, estat_store: sc.Catalog):
        # DE (Germany) is in the GEO codelist for NAMA_10_GDP
        result = estat_store.query(sc.StatisticalDataset).filter(has_code=("geo", "DE")).execute()
        assert result.total >= 1

    def test_has_geo_dimension(self, estat_store: sc.Catalog):
        result = estat_store.query(sc.StatisticalDataset).filter(q.has_dimensions(["geo"])).execute()
        assert result.total >= 1

    def test_facets_publisher(self, estat_store: sc.Catalog):
        counts = estat_store.facets("publisher")
        assert "ESTAT" in counts.get("publisher", {})

    def test_sdmxlib_codes_table_populated(self, estat_store: sc.Catalog):
        row = estat_store.connection.execute("SELECT COUNT(*) FROM sdmx.code").fetchone()
        assert row is not None
        assert row[0] > 0


class TestEstatSearch:
    """Search over live-ingested metadata, which no other test covers."""

    def test_fts_search(self, estat_search_store: sc.Catalog):
        result = estat_search_store.search("GDP")
        assert result.total >= 1

    def test_index_layout_records_every_language(self, estat_search_store: sc.Catalog):
        """A build that writes no table returns zero hits *silently* (#115).

        `index_layout` is what distinguishes "indexed, no match" from
        "never indexed", so it is asserted separately from the hit count
        rather than inferred from it.
        """
        layout = estat_search_store.index_layout
        assert layout, "build_index() wrote no per-language table"
        assert all(entry["rows"] > 0 for entry in layout.values())
        # Generation-suffixed, so a crashed rebuild leaves the previous
        # index serving rather than half-swapped.
        assert all(entry["table"].startswith(f"search_{lang}__") for lang, entry in layout.items())

    def test_each_language_gets_its_own_stemmer(self, estat_search_store: sc.Catalog):
        """ESTAT publishes de/en/fr, and each must be stemmed as itself.

        The defect this guards is silent: pooled into one English-stemmed
        table, French and German still return hits, just worse ones. Only a
        live multilingual ingest exercises it — fixture catalogs choose
        their own languages.
        """
        layout = estat_search_store.index_layout
        assert "en" in layout, f"no English index built from ESTAT; got {sorted(layout)}"
        assert layout["en"]["stemmer"] == "English"
        expected = {"de": "German", "fr": "French"}
        for lang, stemmer in expected.items():
            if lang in layout:
                assert layout[lang]["stemmer"] == stemmer

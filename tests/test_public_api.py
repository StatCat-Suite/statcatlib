"""Smoke tests for the public statcatlib API surface.

Verifies that everything a user needs is importable from ``import statcatlib as sc``
and that ``statcatlib.query`` is accessible as the filter DSL module.
"""

from __future__ import annotations

import pytest

from tests.conftest import concept_of

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import smoke test — everything reachable from top-level
# ---------------------------------------------------------------------------


class TestImports:
    def test_import_statcatlib(self) -> None:
        import statcatlib  # noqa: F401

    def test_import_as_cl(self) -> None:
        import statcatlib as sc  # noqa: F401

    def test_query_submodule_accessible(self) -> None:
        import statcatlib as sc

        assert hasattr(sc, "query")
        assert callable(sc.query.has_code)

    def test_valid_include_rels_is_public(self) -> None:
        """Documented as the list of valid `.include()` names, so it must be reachable (#113)."""
        import statcatlib as sc

        assert "VALID_INCLUDE_RELS" in sc.__all__
        assert "publisher" in sc.VALID_INCLUDE_RELS
        # every name it advertises is accepted by .include()
        with sc.Catalog() as store:
            store.query(sc.BaseDataset).include(*sc.VALID_INCLUDE_RELS)
        assert callable(sc.query.has_dimensions)
        assert callable(sc.query.distribution)
        assert callable(sc.query.has_service)


# ---------------------------------------------------------------------------
# Domain model types
# ---------------------------------------------------------------------------


class TestDomainModelExports:
    def test_base_dataset(self) -> None:
        import statcatlib as sc

        assert sc.BaseDataset is not None

    def test_dataset_union(self) -> None:
        import statcatlib as sc

        assert sc.Dataset is not None

    def test_dataset_variants(self) -> None:
        import statcatlib as sc

        assert sc.AggregateDataset is not None
        assert sc.OpenDataset is not None
        assert sc.MicrodataDataset is not None
        assert sc.GeospatialDataset is not None
        assert sc.PublicationDataset is not None

    def test_statistical_dataset_alias(self) -> None:
        """StatisticalDataset backward-compat alias must point to AggregateDataset."""
        import statcatlib as sc

        assert sc.StatisticalDataset is sc.AggregateDataset

    def test_variable(self) -> None:
        import statcatlib as sc

        assert sc.Variable is not None

    def test_agents(self) -> None:
        import statcatlib as sc

        assert sc.Agent is not None
        assert sc.ContactPoint is not None

    def test_concept(self) -> None:
        import statcatlib as sc

        assert sc.Concept is not None

    def test_temporal(self) -> None:
        import statcatlib as sc

        assert sc.TemporalCoverage is not None
        assert sc.Frequency is not None

    def test_spatial(self) -> None:
        import statcatlib as sc

        assert sc.SpatialCoverage is not None

    def test_licence(self) -> None:
        import statcatlib as sc

        assert sc.LicenceDocument is not None

    def test_quality(self) -> None:
        import statcatlib as sc

        assert sc.QualityAnnotation is not None
        assert sc.ProvenanceStatement is not None

    def test_classification(self) -> None:
        import statcatlib as sc

        assert sc.Standard is not None

    def test_distribution(self) -> None:
        import statcatlib as sc

        assert sc.Distribution is not None
        assert sc.DataService is not None

    def test_catalog(self) -> None:
        import statcatlib as sc

        assert sc.Catalog is not None
        assert sc.CatalogRecord is not None

    def test_international_string(self) -> None:
        import statcatlib as sc

        assert sc.InternationalString is not None
        s = sc.InternationalString(en="hello", fr="bonjour")
        assert s.get("en") == "hello"
        assert s.get("fr") == "bonjour"


# ---------------------------------------------------------------------------
# Store exports
# ---------------------------------------------------------------------------


class TestStoreExports:
    def test_catalog_store(self) -> None:
        import statcatlib as sc

        assert sc.Catalog is not None

    def test_query_builder(self) -> None:
        import statcatlib as sc

        assert sc.QueryBuilder is not None

    def test_search_result(self) -> None:
        import statcatlib as sc

        assert sc.SearchResult is not None


# ---------------------------------------------------------------------------
# Source function exports
# ---------------------------------------------------------------------------


class TestSourceExports:
    def test_ingest_sdmx(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_sdmx)

    def test_ingest_sdmx_registry(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_sdmx_registry)

    def test_dataset_from_dataflow(self) -> None:
        import statcatlib as sc

        assert callable(sc.dataset_from_dataflow)

    def test_ingest_ckan(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_ckan)

    def test_ingest_data(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_data)


# ---------------------------------------------------------------------------
# Constructibility smoke test
# ---------------------------------------------------------------------------


class TestConstruct:
    def test_open_dataset(self) -> None:
        import statcatlib as sc

        ds = sc.OpenDataset(
            identifier="test-1",
            title=sc.InternationalString(en="Test"),
            description=sc.InternationalString(en="Desc"),
        )
        assert ds.identifier == "test-1"
        assert ds.kind == "open"

    def test_aggregate_dataset(self) -> None:
        import statcatlib as sc

        ds = sc.AggregateDataset(
            identifier="test-2",
            title=sc.InternationalString(en="Test"),
            description=sc.InternationalString(en="Desc"),
        )
        assert ds.identifier == "test-2"
        assert ds.kind == "aggregate"

    def test_catalog_store_round_trip(self) -> None:
        import statcatlib as sc

        store = sc.Catalog().open()
        ds = sc.OpenDataset(
            identifier="rt-1",
            title=sc.InternationalString(en="Round Trip"),
            description=sc.InternationalString(en="Desc"),
        )
        store.put(ds)
        retrieved = store.get(sc.OpenDataset, "rt-1")
        assert retrieved is not None
        assert retrieved.identifier == "rt-1"

    def test_query_filter_end_to_end(self) -> None:
        from sdmxlib.model.codelist import Code, Codelist
        from sdmxlib.model.dataflow import Dataflow
        from sdmxlib.model.datastructure import DataStructure, Dimension
        from sdmxlib.model.in_memory_registry import InMemoryRegistry
        from sdmxlib.model.istring import InternationalString as IStr

        import statcatlib as sc
        import statcatlib.query as q

        cl = Codelist(
            id="CL_GEO",
            maintainer="TEST",
            name=IStr(en="Geography"),
            codes=[Code(id="CA", name=IStr(en="Canada")), Code(id="US", name=IStr(en="USA"))],
        )
        dsd = DataStructure(
            id="DSD_FILTER",
            maintainer="TEST",
            name=IStr(en="Filter DSD"),
            dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl)],
        )
        flow = Dataflow(
            id="DF_FILTER",
            maintainer="TEST",
            name=IStr(en="Filterable"),
            description=IStr(en="Desc"),
            structure=dsd,
        )
        registry = InMemoryRegistry()
        for obj in [cl, dsd, flow]:
            registry.add(obj)

        store = sc.Catalog().open()
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.AggregateDataset).filter(q.has_code("GEO", "CA")).execute()
        assert result.total == 1

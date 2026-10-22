"""Unit tests for dataset_from_dataflow() — the SDMX bridge function.

These tests exercise the pure Python extraction of AggregateDataset
from in-memory sdmxlib objects, without any DuckDB round-trip.
"""

from __future__ import annotations

import pytest
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString

import statcatlib as sc
import statcatlib.query as q
from statcatlib.sources.sdmx import dataset_from_dataflow

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _make_minimal() -> tuple[Dataflow, DataStructure]:
    """Minimal dataflow with two dimensions, each backed by a codelist."""
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[Code(id="CA", name=_i("Canada")), Code(id="US", name=_i("United States"))],
    )
    freq_cl = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency", "Fréquence"),
        codes=[Code(id="A", name=_i("Annual")), Code(id="M", name=_i("Monthly"))],
    )
    dsd = DataStructure(
        id="DSD_TEST",
        maintainer="TEST",
        name=_i("Test DSD"),
        dimensions=[
            Dimension(id="GEO", representation=geo_cl),
            Dimension(id="FREQ", representation=freq_cl),
        ],
    )
    flow = Dataflow(
        id="DF_TEST",
        maintainer="TEST",
        name=_i("Test Dataflow", "Flux de test"),
        description=_i("Test description", "Description de test"),
        structure=dsd,
    )
    return flow, dsd


# ---------------------------------------------------------------------------
# dataset_from_dataflow — AggregateDataset construction
# ---------------------------------------------------------------------------


class TestDatasetFromDataflow:
    def test_returns_aggregate_dataset(self) -> None:
        flow, dsd = _make_minimal()
        result = dataset_from_dataflow(flow, dsd)
        assert isinstance(result, sc.AggregateDataset)

    def test_identifier_format(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.identifier == "TEST:DF_TEST(1.0)"

    def test_title_bilingual(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.title.get("en") == "Test Dataflow"
        assert ds.title.get("fr") == "Flux de test"

    def test_description_populated(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.description is not None
        assert ds.description.get("en") == "Test description"

    def test_sdmx_dataflow_urn_set(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.sdmx_dataflow_urn is not None
        assert "DF_TEST" in ds.sdmx_dataflow_urn

    def test_publisher_from_agency(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.publisher is not None
        assert ds.publisher.id == "TEST"

    def test_publisher_override(self) -> None:
        flow, dsd = _make_minimal()
        override = sc.Agent(id="OVERRIDE", name=_i("Override Agency"))
        ds = dataset_from_dataflow(flow, dsd, publisher=override)
        assert ds.publisher is not None
        assert ds.publisher.id == "OVERRIDE"

    def test_themes_default_empty(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.themes == []

    def test_themes_override(self) -> None:
        flow, dsd = _make_minimal()
        theme = sc.ConceptRef(uri="urn:test:themes/ECONOMY", label=_i("Economy"))
        ds = dataset_from_dataflow(flow, dsd, themes=[theme])
        assert len(ds.themes) == 1
        assert ds.themes[0].uri == "urn:test:themes/ECONOMY"


# ---------------------------------------------------------------------------
# dataset_from_dataflow — dimension_names extraction
# ---------------------------------------------------------------------------


class TestDimensionNames:
    def test_dimension_count(self) -> None:
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        assert len(ds.dimension_names) == 2

    def test_dimension_ids_as_fallback(self) -> None:
        """Without resolved concept refs, dimension IDs are used as labels."""
        flow, dsd = _make_minimal()
        ds = dataset_from_dataflow(flow, dsd)
        dim_ids = {d.get("en") for d in ds.dimension_names}
        assert "GEO" in dim_ids
        assert "FREQ" in dim_ids

    def test_empty_dsd_gives_no_dimensions(self) -> None:
        dsd = DataStructure(id="DSD_EMPTY", maintainer="TEST", name=_i("Empty"), dimensions=[])
        flow = Dataflow(id="DF_EMPTY", maintainer="TEST", name=_i("Empty Flow"), structure=dsd)
        ds = dataset_from_dataflow(flow, dsd)
        assert ds.dimension_names == []


# ---------------------------------------------------------------------------
# ingest_sdmx — bulk ingestion integration
# ---------------------------------------------------------------------------


class TestIngestSdmxBridge:
    def _make_registry(self) -> tuple[InMemoryRegistry, str]:
        geo_cl = Codelist(
            id="CL_GEO",
            maintainer="TEST",
            name=_i("Geography"),
            codes=[Code(id="CA", name=_i("Canada")), Code(id="US", name=_i("United States"))],
        )
        dsd = DataStructure(
            id="DSD_TEST",
            maintainer="TEST",
            name=_i("Test DSD"),
            dimensions=[Dimension(id="GEO", representation=geo_cl)],
        )
        flow = Dataflow(
            id="DF_TEST",
            maintainer="TEST",
            name=_i("Test Dataflow"),
            description=_i("Desc"),
            structure=dsd,
        )
        registry = InMemoryRegistry()
        registry.add(geo_cl)
        registry.add(dsd)
        registry.add(flow)
        return registry, flow.id

    def test_ingest_sdmx_returns_list(self) -> None:
        store = sc.Catalog().open()
        registry, _ = self._make_registry()
        result = sc.ingest_sdmx(store, registry)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_ingest_sdmx_all_aggregate(self) -> None:
        store = sc.Catalog().open()
        registry, _ = self._make_registry()
        result = sc.ingest_sdmx(store, registry)
        assert all(isinstance(ds, sc.AggregateDataset) for ds in result)

    def test_ingest_sdmx_codes_queryable(self) -> None:
        """Codes are accessible via SDMX structural tables after ingest."""
        store = sc.Catalog().open()
        registry, _ = self._make_registry()
        sc.ingest_sdmx(store, registry)

        # CA and US should be queryable via has_code (SDMX JOIN path)
        result_ca = store.query(sc.AggregateDataset).filter(q.has_code("GEO", "CA")).execute()
        result_us = store.query(sc.AggregateDataset).filter(q.has_code("GEO", "US")).execute()
        assert result_ca.total == 1
        assert result_us.total == 1
        assert result_ca.items[0].identifier == "TEST:DF_TEST(1.0)"

    def test_ingest_sdmx_registry_publisher_override(self) -> None:
        store = sc.Catalog().open()
        registry, _ = self._make_registry()
        publisher = sc.Agent(id="CUSTOM", name=_i("Custom Publisher"))
        result = sc.ingest_sdmx_registry(store, registry, publisher=publisher)
        assert result[0].publisher is not None
        assert result[0].publisher.id == "CUSTOM"

    def test_ingest_sdmx_registry_themes_override(self) -> None:
        store = sc.Catalog().open()
        registry, _ = self._make_registry()
        theme = sc.ConceptRef(uri="urn:test:themes/ECONOMY", label=_i("Economy"))
        result = sc.ingest_sdmx_registry(store, registry, themes=[theme])
        assert any(t.uri == "urn:test:themes/ECONOMY" for t in result[0].themes)

    def test_prepare_sdmx_returns_datasets(self) -> None:
        """prepare_sdmx() returns AggregateDataset objects without storing."""
        registry, _ = self._make_registry()
        datasets = sc.prepare_sdmx(registry)
        assert len(datasets) == 1
        assert isinstance(datasets[0], sc.AggregateDataset)

    def test_prepare_sdmx_with_overrides(self) -> None:
        """prepare_sdmx() accepts publisher and themes overrides."""
        registry, _ = self._make_registry()
        publisher = sc.Agent(id="CUSTOM", name=_i("Custom"))
        theme = sc.ConceptRef(uri="urn:test:themes/ECON", label=_i("Economy"))
        datasets = sc.prepare_sdmx(registry, publisher=publisher, themes=[theme])
        ds = datasets[0]
        assert ds.publisher is not None
        assert ds.publisher.id == "CUSTOM"
        assert any(t.uri == "urn:test:themes/ECON" for t in ds.themes)

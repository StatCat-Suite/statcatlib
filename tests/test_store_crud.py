"""Tests for Catalog CRUD — one test class per dataset variant.

Exercises put/get round-trip fidelity, type narrowing, overwrite behaviour,
and put list semantics for every concrete variant in the hierarchy.
"""

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


# ---------------------------------------------------------------------------
# Helpers — one minimal instance of each variant
# ---------------------------------------------------------------------------


def _open() -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier="OPEN-001",
        title=_i("Open Dataset", "Ensemble de données ouvert"),
        description=_i("A generic open dataset"),
        publisher=sc.Agent(id="PUB", name=_i("Publisher")),
        status="current",
    )


def _aggregate() -> sc.AggregateDataset:
    return sc.AggregateDataset(
        identifier="AGG-001",
        title=_i("Aggregate Dataset", "Ensemble agrégé"),
        description=_i("Statistical aggregate data"),
        publisher=sc.Agent(id="STAT", name=_i("Statistics Bureau")),
        frequency=sc.Frequency(id="QUARTERLY"),
        num_series=42,
        sdmx_dataflow_urn="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=TEST:DF(1.0)",
        dimension_names=[_i("Geography", "Géographie"), _i("Time", "Temps")],
        status="current",
    )


def _microdata() -> sc.MicrodataDataset:
    return sc.MicrodataDataset(
        identifier="MICRO-001",
        title=_i("Microdata Survey"),
        description=_i("Individual-level survey records"),
        publisher=sc.Agent(id="SURVEY", name=_i("Survey Agency")),
        variables=[
            sc.Variable(id="AGE", label=_i("Age of respondent")),
            sc.Variable(id="INCOME", label=_i("Annual income")),
        ],
        sample_size=10_000,
        universe="Canadians aged 15+",
    )


def _geospatial() -> sc.GeospatialDataset:
    return sc.GeospatialDataset(
        identifier="GEO-001",
        title=_i("Geospatial Dataset"),
        description=_i("Spatial features for Canada"),
        publisher=sc.Agent(id="GEO_PUB", name=_i("Mapping Agency")),
        crs="EPSG:4326",
        spatial_resolution="1:50000",
        feature_types=["polygon", "point"],
    )


def _publication() -> sc.PublicationDataset:
    return sc.PublicationDataset(
        identifier="PUB-001",
        title=_i("Research Publication"),
        description=_i("A peer-reviewed research article"),
        publisher=sc.Agent(id="JOURNAL", name=_i("Science Journal")),
        authors=["Smith, J.", "Doe, A."],
        doi="10.1234/test.2024.001",
    )


# ---------------------------------------------------------------------------
# OpenDataset
# ---------------------------------------------------------------------------


class TestOpenDataset:
    def test_put_and_get(self) -> None:
        s = sc.Catalog().open()
        s.put(_open())
        ds = s.get(sc.OpenDataset, "OPEN-001")
        assert ds is not None
        assert isinstance(ds, sc.OpenDataset)
        assert ds.identifier == "OPEN-001"

    def test_get_as_base(self) -> None:
        s = sc.Catalog().open()
        s.put(_open())
        ds = s.get(sc.BaseDataset, "OPEN-001")
        assert ds is not None
        assert isinstance(ds, sc.OpenDataset)

    def test_get_as_wrong_type_returns_none(self) -> None:
        s = sc.Catalog().open()
        s.put(_open())
        assert s.get(sc.AggregateDataset, "OPEN-001") is None

    def test_roundtrip_fields(self) -> None:
        s = sc.Catalog().open()
        orig = _open()
        s.put(orig)
        ds = s.get(sc.OpenDataset, "OPEN-001")
        assert ds is not None
        assert ds.title.get("en") == "Open Dataset"
        assert ds.title.get("fr") == "Ensemble de données ouvert"
        assert ds.publisher is not None
        assert ds.publisher.id == "PUB"
        assert ds.status == "current"

    def test_overwrite(self) -> None:
        s = sc.Catalog().open()
        s.put(_open())
        updated = sc.OpenDataset(
            identifier="OPEN-001",
            title=_i("Updated Title"),
            description=_i("Updated description"),
            status="archived",
        )
        s.put(updated)
        ds = s.get(sc.OpenDataset, "OPEN-001")
        assert ds is not None
        assert ds.title.get("en") == "Updated Title"
        assert ds.status == "archived"


# ---------------------------------------------------------------------------
# AggregateDataset
# ---------------------------------------------------------------------------


class TestAggregateDataset:
    def test_put_and_get(self) -> None:
        s = sc.Catalog().open()
        s.put(_aggregate())
        ds = s.get(sc.AggregateDataset, "AGG-001")
        assert ds is not None
        assert isinstance(ds, sc.AggregateDataset)

    def test_roundtrip_stat_fields(self) -> None:
        s = sc.Catalog().open()
        s.put(_aggregate())
        ds = s.get(sc.AggregateDataset, "AGG-001")
        assert ds is not None
        assert ds.num_series == 42
        assert ds.sdmx_dataflow_urn == "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=TEST:DF(1.0)"
        assert len(ds.dimension_names) == 2
        assert ds.frequency is not None
        assert ds.frequency.id == "QUARTERLY"

    def test_get_as_base(self) -> None:
        s = sc.Catalog().open()
        s.put(_aggregate())
        ds = s.get(sc.BaseDataset, "AGG-001")
        assert ds is not None
        assert isinstance(ds, sc.AggregateDataset)

    def test_statistical_dataset_alias(self) -> None:
        """StatisticalDataset is a backward-compat alias for AggregateDataset."""
        s = sc.Catalog().open()
        s.put(_aggregate())
        ds = s.get(sc.StatisticalDataset, "AGG-001")
        assert ds is not None
        assert isinstance(ds, sc.AggregateDataset)


# ---------------------------------------------------------------------------
# MicrodataDataset
# ---------------------------------------------------------------------------


class TestMicrodataDataset:
    def test_put_and_get(self) -> None:
        s = sc.Catalog().open()
        s.put(_microdata())
        ds = s.get(sc.MicrodataDataset, "MICRO-001")
        assert ds is not None
        assert isinstance(ds, sc.MicrodataDataset)

    def test_roundtrip_variables(self) -> None:
        s = sc.Catalog().open()
        s.put(_microdata())
        ds = s.get(sc.MicrodataDataset, "MICRO-001")
        assert ds is not None
        assert len(ds.variables) == 2
        var_ids = {v.id for v in ds.variables}
        assert "AGE" in var_ids
        assert "INCOME" in var_ids

    def test_roundtrip_microdata_fields(self) -> None:
        s = sc.Catalog().open()
        s.put(_microdata())
        ds = s.get(sc.MicrodataDataset, "MICRO-001")
        assert ds is not None
        assert ds.sample_size == 10_000
        assert ds.universe == "Canadians aged 15+"

    def test_get_as_wrong_type_returns_none(self) -> None:
        s = sc.Catalog().open()
        s.put(_microdata())
        assert s.get(sc.OpenDataset, "MICRO-001") is None


# ---------------------------------------------------------------------------
# GeospatialDataset
# ---------------------------------------------------------------------------


class TestGeospatialDataset:
    def test_put_and_get(self) -> None:
        s = sc.Catalog().open()
        s.put(_geospatial())
        ds = s.get(sc.GeospatialDataset, "GEO-001")
        assert ds is not None
        assert isinstance(ds, sc.GeospatialDataset)

    def test_roundtrip_geo_fields(self) -> None:
        s = sc.Catalog().open()
        s.put(_geospatial())
        ds = s.get(sc.GeospatialDataset, "GEO-001")
        assert ds is not None
        assert ds.crs == "EPSG:4326"
        assert ds.spatial_resolution == "1:50000"
        assert "polygon" in ds.feature_types

    def test_get_as_wrong_type_returns_none(self) -> None:
        s = sc.Catalog().open()
        s.put(_geospatial())
        assert s.get(sc.AggregateDataset, "GEO-001") is None


# ---------------------------------------------------------------------------
# PublicationDataset
# ---------------------------------------------------------------------------


class TestPublicationDataset:
    def test_put_and_get(self) -> None:
        s = sc.Catalog().open()
        s.put(_publication())
        ds = s.get(sc.PublicationDataset, "PUB-001")
        assert ds is not None
        assert isinstance(ds, sc.PublicationDataset)

    def test_roundtrip_publication_fields(self) -> None:
        s = sc.Catalog().open()
        s.put(_publication())
        ds = s.get(sc.PublicationDataset, "PUB-001")
        assert ds is not None
        assert ds.doi == "10.1234/test.2024.001"
        assert "Smith, J." in ds.authors

    def test_get_as_wrong_type_returns_none(self) -> None:
        s = sc.Catalog().open()
        s.put(_publication())
        assert s.get(sc.MicrodataDataset, "PUB-001") is None


# ---------------------------------------------------------------------------
# put(list) — mixed variants
# ---------------------------------------------------------------------------


class TestPutList:
    def test_put_list_mixed_variants(self) -> None:
        s = sc.Catalog().open()
        datasets: list[sc.Dataset] = [_open(), _aggregate(), _microdata(), _geospatial(), _publication()]
        s.put(datasets)
        assert s.get(sc.OpenDataset, "OPEN-001") is not None
        assert s.get(sc.AggregateDataset, "AGG-001") is not None
        assert s.get(sc.MicrodataDataset, "MICRO-001") is not None
        assert s.get(sc.GeospatialDataset, "GEO-001") is not None
        assert s.get(sc.PublicationDataset, "PUB-001") is not None

    def test_put_list_count(self) -> None:
        s = sc.Catalog().open()
        s.put([_open(), _aggregate(), _microdata()])
        result = s.query(sc.BaseDataset).execute()
        assert result.total == 3

    def test_put_list_idempotent(self) -> None:
        s = sc.Catalog().open()
        datasets: list[sc.Dataset] = [_open(), _aggregate()]
        s.put(datasets)
        s.put(datasets)
        result = s.query(sc.BaseDataset).execute()
        assert result.total == 2

    def test_put_list_returns_none_for_missing(self) -> None:
        s = sc.Catalog().open()
        s.put([_open()])
        assert s.get(sc.BaseDataset, "DOES-NOT-EXIST") is None


# ---------------------------------------------------------------------------
# get() — BaseDataset accepts any variant
# ---------------------------------------------------------------------------


class TestGetBase:
    def test_get_base_returns_open(self) -> None:
        s = sc.Catalog().open()
        s.put(_open())
        ds = s.get(sc.BaseDataset, "OPEN-001")
        assert isinstance(ds, sc.OpenDataset)

    def test_get_base_returns_aggregate(self) -> None:
        s = sc.Catalog().open()
        s.put(_aggregate())
        ds = s.get(sc.BaseDataset, "AGG-001")
        assert isinstance(ds, sc.AggregateDataset)

    def test_get_missing_returns_none(self) -> None:
        s = sc.Catalog().open()
        assert s.get(sc.BaseDataset, "MISSING") is None

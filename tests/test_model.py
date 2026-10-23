"""Tests for the statcatlib domain model."""

from datetime import UTC, datetime

import attrs
import pytest
from sdmxlib import InternationalString

import statcatlib as sc
from tests.conftest import untyped

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


class TestConcept:
    def test_construction(self):
        c = sc.Concept(notation="ECONOMY", label=_i("Economy"))
        assert c.notation == "ECONOMY"
        assert c.label.get("en") == "Economy"
        assert c.broader == ()
        assert c.narrower == ()

    def test_with_alt_label_and_definition(self):
        c = sc.Concept(notation="T1", label=_i("Theme 1"), definition=_i("A theme"))
        assert c.definition is not None
        assert c.definition.get("en") == "A theme"

    def test_evolve(self):
        c = sc.Concept(notation="A", label=_i("A"))
        c2 = attrs.evolve(c, notation="B")
        assert c2.notation == "B"
        assert c.notation == "A"


class TestConceptRef:
    def test_construction(self):
        ref = sc.ConceptRef(uri="https://example.com/concepts/economy")
        assert ref.uri == "https://example.com/concepts/economy"
        assert ref.label == InternationalString()
        assert ref.notation is None

    def test_equality_by_uri(self):
        ref1 = sc.ConceptRef(uri="https://example.com/c/1", label=_i("Economy"))
        ref2 = sc.ConceptRef(uri="https://example.com/c/1")
        assert ref1 == ref2

    def test_with_hints(self):
        ref = sc.ConceptRef(uri="https://example.com/c/1", notation="ECO", scheme_uri="https://example.com/c")
        assert ref.notation == "ECO"
        assert ref.scheme_uri == "https://example.com/c"


class TestAgent:
    def test_construction(self):
        a = sc.Agent(name=_i("ECB"), id="ECB")
        assert a.name.get("en") == "ECB"
        assert a.id == "ECB"
        assert a.type is None

    def test_defaults(self):
        a = sc.Agent(name=_i("Someone"))
        assert a.id is None
        assert a.homepage is None


class TestOpenDataset:
    def test_construction(self):
        ds = sc.OpenDataset(
            identifier="DS001",
            title=_i("Test Dataset"),
            description=_i("A test dataset"),
        )
        assert ds.identifier == "DS001"
        assert ds.title.get("en") == "Test Dataset"
        assert ds.kind == "open"
        assert ds.publisher is None
        assert ds.keywords == []
        assert ds.themes == []

    def test_is_base_dataset(self):
        ds = sc.OpenDataset(identifier="DS001", title=_i("T"), description=_i("D"))
        assert isinstance(ds, sc.BaseDataset)

    def test_with_full_fields(self):
        ds = sc.OpenDataset(
            identifier="DS002",
            title=_i("Full Dataset"),
            description=_i("Has all fields"),
            publisher=sc.Agent(name=_i("ECB")),
            themes=[sc.ConceptRef(uri="https://example.com/themes/economy", label=_i("Economy"))],
            frequency=sc.Frequency(id="MONTHLY"),
            status="current",
            language=["en", "fr"],
        )
        assert ds.publisher is not None
        assert ds.publisher.name.get("en") == "ECB"
        assert len(ds.themes) == 1
        assert ds.frequency is not None
        assert ds.frequency.id == "MONTHLY"
        assert ds.status == "current"
        assert "en" in ds.language

    def test_evolve(self):
        ds = sc.OpenDataset(identifier="A", title=_i("A"), description=_i("desc"))
        ds2 = attrs.evolve(ds, status="archived")
        assert ds2.status == "archived"
        assert ds.status is None

    def test_mutable_list_defaults_are_independent(self):
        ds1 = sc.OpenDataset(identifier="A", title=_i("A"), description=_i("d"))
        ds2 = sc.OpenDataset(identifier="B", title=_i("B"), description=_i("d"))
        ds1.keywords.append(_i("test"))
        assert ds2.keywords == []


class TestAggregateDataset:
    def test_is_base_dataset(self):
        ds = sc.AggregateDataset(
            identifier="STAT001",
            title=_i("Statistical Dataset"),
            description=_i("A stat dataset"),
        )
        assert isinstance(ds, sc.BaseDataset)
        assert ds.kind == "aggregate"

    def test_stat_fields(self):
        urn = "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ESTAT:DF_NAMQ_10_GDP(1.0)"
        ds = sc.AggregateDataset(
            identifier="STAT002",
            title=_i("GDP"),
            description=_i("GDP data"),
            dimension_names=[_i("Reference Area"), _i("Time Period")],
            num_series=30,
            sdmx_dataflow_urn=urn,
        )
        assert len(ds.dimension_names) == 2
        assert ds.dimension_names[0].get("en") == "Reference Area"
        assert ds.num_series == 30
        assert ds.sdmx_dataflow_urn == urn

    def test_statistical_dataset_alias(self):
        ds = sc.StatisticalDataset(identifier="S", title=_i("S"), description=_i("d"))
        assert isinstance(ds, sc.AggregateDataset)
        assert ds.kind == "aggregate"

    def test_isinstance_discrimination(self):
        plain = sc.OpenDataset(identifier="P", title=_i("P"), description=_i("d"))
        stat = sc.AggregateDataset(identifier="S", title=_i("S"), description=_i("d"))

        assert isinstance(plain, sc.BaseDataset)
        assert not isinstance(plain, sc.AggregateDataset)
        assert isinstance(stat, sc.BaseDataset)
        assert isinstance(stat, sc.AggregateDataset)


class TestVariantKinds:
    def test_open_kind(self):
        ds = sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D"))
        assert ds.kind == "open"

    def test_aggregate_kind(self):
        ds = sc.AggregateDataset(identifier="A", title=_i("T"), description=_i("D"))
        assert ds.kind == "aggregate"

    def test_microdata_kind(self):
        ds = sc.MicrodataDataset(identifier="M", title=_i("T"), description=_i("D"))
        assert ds.kind == "microdata"
        assert ds.variables == []

    def test_geospatial_kind(self):
        ds = sc.GeospatialDataset(identifier="G", title=_i("T"), description=_i("D"))
        assert ds.kind == "geospatial"
        assert ds.feature_types == []

    def test_publication_kind(self):
        ds = sc.PublicationDataset(identifier="P", title=_i("T"), description=_i("D"))
        assert ds.kind == "publication"
        assert ds.authors == []


class TestKindIsAClassVar:
    """`kind` is derived from the class, not stored on the instance."""

    def test_readable_on_the_class_without_an_instance(self):
        assert sc.AggregateDataset.kind == "aggregate"

    def test_is_not_an_attrs_field(self):
        assert "kind" not in {f.name for f in attrs.fields(sc.OpenDataset)}
        ds = sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D"))
        assert "kind" not in attrs.asdict(ds)

    def test_cannot_be_passed_to_the_constructor(self):
        with pytest.raises(TypeError, match="unexpected keyword argument 'kind'"):
            sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D"), **untyped({"kind": "aggregate"}))

    def test_cannot_be_reassigned(self):
        ds = sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D"))
        with pytest.raises(AttributeError, match="read-only"):
            # Through `untyped`: assigning to a `ClassVar` is exactly what
            # this asserts fails, so the checker is right to reject the
            # direct form and the intent is stated instead of suppressed.
            untyped(ds).kind = "aggregate"

    def test_survives_evolve(self):
        ds = sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D"))
        assert attrs.evolve(ds, version="2").kind == "open"

    def test_base_dataset_has_no_kind(self):
        assert not hasattr(sc.BaseDataset, "kind")

    def test_dataset_kinds_covers_every_variant(self):
        from statcatlib.model.dataset import DATASET_KINDS

        assert set(DATASET_KINDS) == {"open", "aggregate", "microdata", "geospatial", "publication"}

    def test_pattern_matching(self):
        datasets: list[sc.BaseDataset] = [
            sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D")),
            sc.AggregateDataset(identifier="A", title=_i("T"), description=_i("D")),
            sc.MicrodataDataset(identifier="M", title=_i("T"), description=_i("D")),
        ]
        kinds = []
        for ds in datasets:
            match ds:
                case sc.OpenDataset():
                    kinds.append("open")
                case sc.AggregateDataset():
                    kinds.append("aggregate")
                case sc.MicrodataDataset():
                    kinds.append("microdata")
                case _:
                    kinds.append("other")
        assert kinds == ["open", "aggregate", "microdata"]

    def test_all_base_dataset(self):
        variants: list[sc.BaseDataset] = [
            sc.OpenDataset(identifier="O", title=_i("T"), description=_i("D")),
            sc.AggregateDataset(identifier="A", title=_i("T"), description=_i("D")),
            sc.MicrodataDataset(identifier="M", title=_i("T"), description=_i("D")),
            sc.GeospatialDataset(identifier="G", title=_i("T"), description=_i("D")),
            sc.PublicationDataset(identifier="P", title=_i("T"), description=_i("D")),
        ]
        assert all(isinstance(v, sc.BaseDataset) for v in variants)


class TestVariable:
    def test_construction(self):
        v = sc.Variable(id="AGE", label=_i("Age"))
        assert v.id == "AGE"
        assert v.label.get("en") == "Age"
        assert v.value_type is None

    def test_microdata_with_variables(self):
        ds = sc.MicrodataDataset(
            identifier="M1",
            title=_i("Survey"),
            description=_i("A survey"),
            variables=[sc.Variable(id="SEX", label=_i("Sex"))],
        )
        assert len(ds.variables) == 1
        assert ds.variables[0].id == "SEX"


class TestDistribution:
    def test_construction(self):
        d = sc.Distribution(id="DIST001", access_url="http://example.com/data.csv")
        assert d.id == "DIST001"
        assert d.access_url == "http://example.com/data.csv"
        assert d.media_type is None

    def test_with_service(self):
        svc = sc.DataService(
            id="SVC001",
            title=_i("SDMX REST API"),
            endpoint_url="https://api.example.com/sdmx",
        )
        dist = sc.Distribution(id="D1", access_service=svc)
        assert dist.access_service is not None
        assert dist.access_service.endpoint_url == "https://api.example.com/sdmx"


class TestCatalogDescription:
    def test_construction(self):
        cat = sc.CatalogDescription(
            identifier="CAT001",
            title=_i("Open Data Portal"),
            description=_i("A catalog of open datasets"),
            publisher=sc.Agent(name=_i("ECB")),
        )
        assert cat.identifier == "CAT001"
        assert len(cat.datasets) == 0
        assert len(cat.services) == 0

    def test_catalog_record(self):
        ds = sc.OpenDataset(identifier="DS1", title=_i("DS"), description=_i("d"))
        rec = sc.CatalogRecord(identifier="REC1", primary_topic=ds)
        assert rec.primary_topic is ds
        assert rec.listing_date is None


class TestTemporalSpatial:
    def test_temporal_coverage(self):
        tc = sc.TemporalCoverage(
            start=datetime(2020, 1, 1, tzinfo=UTC),
            end=datetime(2023, 12, 31, tzinfo=UTC),
        )
        assert tc.start is not None
        assert tc.start.year == 2020

    def test_spatial_coverage(self):
        cov = sc.SpatialCoverage(
            label=_i("European Union"),
            bbox=(-25.0, 34.0, 45.0, 72.0),
        )
        assert cov.bbox is not None
        assert cov.bbox[0] == pytest.approx(-25.0)

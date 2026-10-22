"""Integration tests for Catalog structural traversal methods.

These tests ingest SDMX artefacts into the shared DuckDB tables, then verify
that the traversal methods on Catalog correctly join across sdmxlib's
tables (dataflows, dsd_components, codelists, codes).
"""

from __future__ import annotations

import pytest
import sdmxlib
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _make_two_flow_registry() -> tuple[InMemoryRegistry, sc.Catalog]:
    """Two dataflows sharing a GEO codelist, different FREQ codelists.

    DF_EXR uses CL_GEO + CL_FREQ_A
    DF_GDP uses CL_GEO + CL_FREQ_Q
    """
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[
            Code(id="CA", name=_i("Canada", "Canada")),
            Code(id="US", name=_i("United States", "États-Unis")),
            Code(id="DE", name=_i("Germany", "Allemagne")),
        ],
    )
    freq_a = Codelist(
        id="CL_FREQ_A",
        maintainer="TEST",
        name=_i("Annual Frequency"),
        codes=[Code(id="A", name=_i("Annual", "Annuel"))],
    )
    freq_q = Codelist(
        id="CL_FREQ_Q",
        maintainer="TEST",
        name=_i("Quarterly Frequency"),
        codes=[Code(id="Q", name=_i("Quarterly", "Trimestriel"))],
    )
    dsd1 = DataStructure(
        id="DSD_EXR",
        maintainer="TEST",
        name=_i("Exchange Rate DSD"),
        dimensions=[
            Dimension(id="GEO", representation=geo_cl),
            Dimension(id="FREQ", representation=freq_a),
        ],
    )
    dsd2 = DataStructure(
        id="DSD_GDP",
        maintainer="TEST",
        name=_i("GDP DSD"),
        dimensions=[
            Dimension(id="GEO", representation=geo_cl),
            Dimension(id="FREQ", representation=freq_q),
        ],
    )
    flow1 = Dataflow(id="DF_EXR", maintainer="TEST", name=_i("Exchange Rate"), structure=dsd1)
    flow2 = Dataflow(id="DF_GDP", maintainer="TEST", name=_i("GDP"), structure=dsd2)

    registry = InMemoryRegistry()
    for obj in [geo_cl, freq_a, freq_q, dsd1, dsd2, flow1, flow2]:
        registry.add(obj)

    store = sc.Catalog().open()
    sc.ingest_sdmx(store, registry)
    return registry, store


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestLocalRegistry:
    def test_returns_local_registry(self) -> None:
        store = sc.Catalog().open()
        reg = store.registry
        assert isinstance(reg, sdmxlib.LocalRegistry)

    def test_shares_connection(self) -> None:
        """LocalRegistry should see artefacts ingested via the store."""
        _, store = _make_two_flow_registry()
        reg = store.registry
        # CL_GEO should be visible via LocalRegistry
        cl_urn = sdmxlib.SdmxUrn.parse("urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)")
        result = reg.get(cl_urn, eager=True)
        assert result is not None
        assert isinstance(result, sdmxlib.Codelist)

    def test_raises_before_open(self) -> None:
        cat = sc.Catalog()  # cheap, not opened
        with pytest.raises(RuntimeError, match="not open"):
            _ = cat.registry

    def test_same_instance_per_open(self) -> None:
        """Composition: one Registry per open(), not rebuilt per access."""
        store = sc.Catalog().open()
        assert store.registry is store.registry

    def test_writes_via_registry_visible_to_statcatlib(self) -> None:
        """sl.LocalRegistry mutations land in the same DuckDB file statcatlib queries."""
        store = sc.Catalog().open()
        # Direct mutation via the composed registry — statcatlib should see it
        # because both schemas live on one connection (Topology B).
        cl = sdmxlib.Codelist(
            id="CL_DIRECT",
            maintainer="TEST",
            version="1.0",
            name=sdmxlib.InternationalString(en="Direct"),
            codes=[
                sdmxlib.Code(id="A", name=sdmxlib.InternationalString(en="Alpha")),
                sdmxlib.Code(id="B", name=sdmxlib.InternationalString(en="Bravo")),
            ],
        )
        store.registry.add(cl)
        cl_urn = sdmxlib.SdmxUrn.parse("urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_DIRECT(1.0)")
        result = store.registry.get(cl_urn, eager=True)
        assert result is not None
        assert isinstance(result, sdmxlib.Codelist)


# ---------------------------------------------------------------------------
# code_map
# ---------------------------------------------------------------------------


class TestCodeMap:
    def test_returns_dict_of_dicts(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.code_map("TEST:DF_EXR(1.0)")
        assert isinstance(result, dict)
        assert "GEO" in result
        assert "FREQ" in result

    def test_geo_codes_present(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.code_map("TEST:DF_EXR(1.0)")
        assert set(result["GEO"].keys()) == {"CA", "US", "DE"}

    def test_labels_populated(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.code_map("TEST:DF_EXR(1.0)")
        assert result["GEO"]["CA"] == "Canada"

    def test_fr_labels(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.code_map("TEST:DF_EXR(1.0)", lang="fr")
        assert result["GEO"]["CA"] == "Canada"
        assert result["GEO"]["US"] == "États-Unis"

    def test_missing_dataset_returns_empty(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.code_map("NONEXISTENT:DATASET(1.0)")
        assert result == {}

    def test_non_sdmx_dataset_returns_empty(self) -> None:
        store = sc.Catalog().open()
        store.put(
            sc.OpenDataset(
                identifier="plain-1",
                title=_i("Plain"),
                description=_i("No SDMX link"),
            )
        )
        result = store.code_map("plain-1")
        assert result == {}


# ---------------------------------------------------------------------------
# codelist_for
# ---------------------------------------------------------------------------


class TestCodelistFor:
    def test_returns_codelist(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.codelist_for("TEST:DF_EXR(1.0)", "GEO")
        assert result is not None
        assert isinstance(result, sdmxlib.Codelist)

    def test_codelist_has_correct_codes(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.codelist_for("TEST:DF_EXR(1.0)", "GEO")
        assert result is not None
        code_ids = {c.id for c in result.codes}
        assert code_ids == {"CA", "US", "DE"}

    def test_wrong_dimension_returns_none(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.codelist_for("TEST:DF_EXR(1.0)", "NONEXISTENT")
        assert result is None

    def test_nonexistent_dataset_returns_none(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.codelist_for("NONEXISTENT", "GEO")
        assert result is None


# ---------------------------------------------------------------------------
# get_structure
# ---------------------------------------------------------------------------


class TestGetStructure:
    def test_returns_data_structure(self) -> None:
        _, store = _make_two_flow_registry()
        ds = store.get(sc.AggregateDataset, "TEST:DF_EXR(1.0)")
        assert ds is not None
        result = store.get_structure(ds)
        assert result is not None
        assert isinstance(result, DataStructure)

    def test_structure_has_correct_dimensions(self) -> None:
        _, store = _make_two_flow_registry()
        ds = store.get(sc.AggregateDataset, "TEST:DF_EXR(1.0)")
        assert ds is not None
        dsd = store.get_structure(ds)
        assert dsd is not None
        dim_ids = [d.id for d in dsd.dimensions]
        assert "GEO" in dim_ids
        assert "FREQ" in dim_ids

    def test_no_urn_returns_none(self) -> None:
        store = sc.Catalog().open()
        ds = sc.AggregateDataset(
            identifier="no-sdmx",
            title=_i("No SDMX"),
            description=_i("Desc"),
        )
        result = store.get_structure(ds)
        assert result is None


# ---------------------------------------------------------------------------
# codelists[urn].datasets
# ---------------------------------------------------------------------------


class TestCodelistDatasets:
    def test_shared_codelist_finds_both(self) -> None:
        """Both DF_EXR and DF_GDP use CL_GEO — both should be returned."""
        _, store = _make_two_flow_registry()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        result = store.codelists[geo_urn].datasets.execute().items
        ids = {ds.identifier for ds in result}
        assert "TEST:DF_EXR(1.0)" in ids
        assert "TEST:DF_GDP(1.0)" in ids

    def test_exclusive_codelist_finds_one(self) -> None:
        """CL_FREQ_A is only used by DF_EXR."""
        _, store = _make_two_flow_registry()
        freq_a_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_FREQ_A(1.0)"
        result = store.codelists[freq_a_urn].datasets.execute().items
        assert len(result) == 1
        assert result[0].identifier == "TEST:DF_EXR(1.0)"

    def test_nonexistent_codelist_returns_empty(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.codelists["urn:nonexistent"].datasets.execute().items
        assert result == []


# ---------------------------------------------------------------------------
# dataset().related("codelist")
# ---------------------------------------------------------------------------


class TestDatasetRelated:
    def test_related_via_shared_geo(self) -> None:
        """DF_EXR and DF_GDP both use CL_GEO — each should find the other."""
        _, store = _make_two_flow_registry()
        result = store.dataset("TEST:DF_EXR(1.0)").related("codelist").collect()
        ids = {ds.identifier for ds in result}
        assert "TEST:DF_GDP(1.0)" in ids

    def test_self_excluded(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.dataset("TEST:DF_EXR(1.0)").related("codelist").collect()
        ids = {ds.identifier for ds in result}
        assert "TEST:DF_EXR(1.0)" not in ids

    def test_isolated_dataset_returns_empty(self) -> None:
        """A dataset with a unique codelist has no related datasets."""
        unique_cl = Codelist(
            id="CL_UNIQUE",
            maintainer="TEST",
            name=_i("Unique"),
            codes=[Code(id="X", name=_i("X"))],
        )
        dsd = DataStructure(
            id="DSD_UNIQUE",
            maintainer="TEST",
            name=_i("Unique DSD"),
            dimensions=[Dimension(id="DIM", representation=unique_cl)],
        )
        flow = Dataflow(id="DF_UNIQUE", maintainer="TEST", name=_i("Unique Flow"), structure=dsd)
        registry = InMemoryRegistry()
        for obj in [unique_cl, dsd, flow]:
            registry.add(obj)
        store = sc.Catalog().open()
        sc.ingest_sdmx(store, registry)
        result = store.dataset("TEST:DF_UNIQUE(1.0)").related("codelist").collect()
        assert result == []

    def test_nonexistent_dataset_returns_empty(self) -> None:
        _, store = _make_two_flow_registry()
        result = store.dataset("NONEXISTENT").related("codelist").collect()
        assert result == []

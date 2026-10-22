"""Tests for statcatlib.urn convenience URN builders."""

import pytest

import statcatlib as sc

pytestmark = pytest.mark.unit


class TestCodelist:
    def test_basic(self):
        result = sc.urn.codelist("SDMX", "CL_GEO", "1.0")
        assert result == "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SDMX:CL_GEO(1.0)"

    def test_default_version(self):
        result = sc.urn.codelist("ESTAT", "CL_FREQ")
        assert result == "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=ESTAT:CL_FREQ(1.0)"

    def test_custom_version(self):
        result = sc.urn.codelist("BIS", "CL_BIS_UNIT", "2.3")
        assert "(2.3)" in result


class TestDataflow:
    def test_basic(self):
        result = sc.urn.dataflow("ESTAT", "NAMA_10_GDP", "1.0")
        assert result == "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ESTAT:NAMA_10_GDP(1.0)"

    def test_default_version(self):
        result = sc.urn.dataflow("ECB", "EXR")
        assert "ECB:EXR(1.0)" in result


class TestDataStructure:
    def test_basic(self):
        result = sc.urn.data_structure("ESTAT", "DSD_NAMA", "1.0")
        assert result == "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=ESTAT:DSD_NAMA(1.0)"


class TestConceptScheme:
    def test_basic(self):
        result = sc.urn.concept_scheme("SDMX", "CS_FREQ", "1.0")
        assert result == "urn:sdmx:org.sdmx.infomodel.conceptscheme.ConceptScheme=SDMX:CS_FREQ(1.0)"


class TestParse:
    def test_roundtrip_codelist(self):
        urn_str = sc.urn.codelist("SDMX", "CL_GEO", "1.0")
        parsed = sc.urn.parse(urn_str)
        assert parsed.agency == "SDMX"
        assert parsed.id == "CL_GEO"
        assert parsed.version == "1.0"
        assert parsed.package == "codelist"
        assert parsed.artefact_class == "Codelist"
        assert str(parsed) == urn_str

    def test_roundtrip_dataflow(self):
        urn_str = sc.urn.dataflow("ESTAT", "NAMA_10_GDP", "2.0")
        parsed = sc.urn.parse(urn_str)
        assert parsed.agency == "ESTAT"
        assert parsed.id == "NAMA_10_GDP"
        assert parsed.version == "2.0"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid SDMX URN"):
            sc.urn.parse("not-a-urn")


class TestExport:
    def test_urn_module_accessible(self):
        assert hasattr(sc, "urn")
        assert hasattr(sc.urn, "codelist")
        assert hasattr(sc.urn, "dataflow")
        assert hasattr(sc.urn, "data_structure")
        assert hasattr(sc.urn, "concept_scheme")
        assert hasattr(sc.urn, "parse")

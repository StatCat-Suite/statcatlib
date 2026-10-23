"""Reference-metadata as a first-class searchable entity kind.

Covers the Phase-B search path: ``build_index(include_reference_metadata=
True)`` chunks sectioned SDMX MetadataSets into ``reference_metadata`` rows,
``cat.reference_metadata.find`` retrieves them (optionally scoped to an
artefact's metadata), and they fold into the cross-kind ``cat.search`` RRF.
Also the structured ``cat.metadata_for`` / ``cat.metadata_report`` path.
"""

from __future__ import annotations

import pathlib

import pytest
import sdmxlib as sl

from statcatlib.store.store import Catalog
from tests.conftest import concept_of

_DQ_TEXT = (
    "The national non-response rate for income was 2.0 percent. As a result of "
    "edit and imputation, aggregate total income increased 3.0 percent. Tax record "
    "linkage reached 90.0 percent of the population aged 15 and older."
)

pytestmark = pytest.mark.integration


def _topic_urn(item_id: str) -> sl.SdmxUrn[sl.Code]:
    return sl.SdmxUrn.make(
        sl.Code,
        package="codelist",
        artefact_class="Code",
        agency="STC",
        id="CL_TOPIC",
        version="1.0",
        item_id=item_id,
    )


def _catalog_with_refmeta(path: pathlib.Path) -> tuple[Catalog, sl.SdmxUrn[sl.Code]]:
    cat = Catalog(path / "cat").open()
    msd = sl.MetadataStructure(
        id="MSD_RG",
        maintainer="STC",
        version="1.0",
        name={"en": "RG"},
        attributes=[
            sl.MetadataAttribute(
                id="DATA_QUALITY_TOPIC",
                concept=concept_of("DATA_QUALITY_TOPIC"),
                representation=sl.Facet(type=sl.DataType.XHTML),
            ),
            sl.MetadataAttribute(
                id="CATALOGUE_ID", concept=concept_of("CATALOGUE_ID"), representation=sl.Facet(type=sl.DataType.String)
            ),
        ],
    )
    mdf = sl.Metadataflow(id="MDF_RG", maintainer="STC", version="1.0", name={"en": "RG"}, structure=msd)
    code = _topic_urn("111")
    ms = sl.MetadataSet(
        id="MS_RG_004",
        maintainer="STC",
        version="1.0",
        structure=mdf,
        name={"en": "Income Reference Guide"},
        targets=[code],
        attributes=[
            sl.ReportedAttribute(id="DATA_QUALITY_TOPIC", value={"en": _DQ_TEXT}),
            sl.ReportedAttribute(id="CATALOGUE_ID", value="98-500-X2021004"),
        ],
    )
    cat.registry.add_all([msd, mdf, ms])
    return cat, code


def _indexed_catalog(path: pathlib.Path) -> tuple[Catalog, sl.SdmxUrn[sl.Code]]:
    cat, code = _catalog_with_refmeta(path)
    cat.build_index(include_reference_metadata=True)  # bm25-only, no embedding model
    return cat, code


# ── indexing ────────────────────────────────────────────────────────────


def test_collect_skips_scalar_provenance(tmp_path: pathlib.Path) -> None:
    cat, _ = _catalog_with_refmeta(tmp_path)
    rows = cat._collect_reference_metadata_texts()
    paths = {r["attribute_path"] for r in rows}
    assert "DATA_QUALITY_TOPIC" in paths  # i18n prose indexed
    assert "CATALOGUE_ID" not in paths  # scalar id skipped


def test_collect_row_shape(tmp_path: pathlib.Path) -> None:
    cat, _ = _catalog_with_refmeta(tmp_path)
    row = next(r for r in cat._collect_reference_metadata_texts() if r["lang"] == "en")
    assert row["entity_type"] == "reference_metadata"
    assert row["section_title"] == "Data Quality Topic"
    assert (row["set_urn"] or "").endswith("MS_RG_004(1.0)")
    assert row["text"].startswith("Data Quality Topic | ")


def test_metadataflow_filter(tmp_path: pathlib.Path) -> None:
    cat, _ = _catalog_with_refmeta(tmp_path)
    mdf_urn = "urn:sdmx:org.sdmx.infomodel.metadatastructure.Metadataflow=STC:MDF_RG(1.0)"
    assert cat._collect_reference_metadata_texts(metadataflow_urns=[mdf_urn])
    other = "urn:sdmx:org.sdmx.infomodel.metadatastructure.Metadataflow=STC:OTHER(1.0)"
    assert cat._collect_reference_metadata_texts(metadataflow_urns=[other]) == []


def test_xhtml_section_is_down_converted_to_markdown(tmp_path: pathlib.Path) -> None:
    # A section stored as XHTML (text_type=XHTML) is indexed as markdown —
    # no tags in the search text, tables recovered as markdown.
    cat = Catalog(tmp_path / "cat").open()
    msd = sl.MetadataStructure(
        id="MSD_X",
        maintainer="STC",
        version="1.0",
        name={"en": "X"},
        attributes=[
            sl.MetadataAttribute(
                id="DATA_QUALITY_TOPIC",
                concept=concept_of("DATA_QUALITY_TOPIC"),
                representation=sl.Facet(type=sl.DataType.XHTML),
            )
        ],
    )
    mdf = sl.Metadataflow(id="MDF_X", maintainer="STC", version="1.0", name={"en": "X"}, structure=msd)
    xhtml = (
        "<p>Non-response was 2.0 percent.</p>"
        "<table><caption>Table 1 Rates</caption>"
        "<tr><th>Geo</th><th>Rate</th></tr><tr><td>Canada</td><td>3.5</td></tr></table>"
    )
    ms = sl.MetadataSet(
        id="MS_X",
        maintainer="STC",
        version="1.0",
        structure=mdf,
        name={"en": "X"},
        targets=[_topic_urn("111")],
        attributes=[sl.ReportedAttribute(id="DATA_QUALITY_TOPIC", value={"en": xhtml})],
    )
    cat.registry.add_all([msd, mdf, ms])

    row = next(r for r in cat._collect_reference_metadata_texts() if r["lang"] == "en")
    assert "<table>" not in row["text"]  # tags gone
    assert "<p>" not in row["text"]
    assert "| Geo | Rate |" in row["text"]  # table recovered as markdown
    assert "**Table 1 Rates**" in row["text"]
    assert "Non-response was 2.0 percent." in row["text"]


# ── search ──────────────────────────────────────────────────────────────


def test_find_returns_section(tmp_path: pathlib.Path) -> None:
    cat, _ = _indexed_catalog(tmp_path)
    hits = cat.reference_metadata.find("non-response rate income imputation", limit=5).items
    assert hits, "expected a reference_metadata hit"
    top = hits[0]
    assert top.kind == "reference_metadata"
    assert top.payload.attribute_path == "DATA_QUALITY_TOPIC"
    assert top.payload.set_urn.endswith("MS_RG_004(1.0)")


def test_find_scoped_to_target(tmp_path: pathlib.Path) -> None:
    cat, code = _indexed_catalog(tmp_path)
    assert cat.reference_metadata.find("income imputation", target_urn=str(code)).items
    # A code with no attached metadata yields nothing.
    other = str(_topic_urn("999"))
    assert cat.reference_metadata.find("income imputation", target_urn=other).items == []


def test_search_includes_reference_metadata_only_when_requested(tmp_path: pathlib.Path) -> None:
    cat, _ = _indexed_catalog(tmp_path)
    default = cat.search("non-response rate income")
    assert all(h.kind != "reference_metadata" for h in default.items)
    with_refmeta = cat.search(
        "non-response rate income",
        kinds=("dataset", "concept", "code", "reference_metadata"),
    )
    assert any(h.kind == "reference_metadata" for h in with_refmeta.items)


# ── structured (URN) path ───────────────────────────────────────────────


def test_metadata_for_resolves_target(tmp_path: pathlib.Path) -> None:
    cat, code = _catalog_with_refmeta(tmp_path)
    sets = cat.metadata_for(code)
    assert [s.id for s in sets] == ["MS_RG_004"]


def test_metadata_report_is_typed(tmp_path: pathlib.Path) -> None:
    cat, _ = _catalog_with_refmeta(tmp_path)
    set_urn = "urn:sdmx:org.sdmx.infomodel.metadatastructure.MetadataSet=STC:MS_RG_004(1.0)"
    sections = {s.attribute_id: s for s in cat.metadata_report(set_urn)}
    assert sections["DATA_QUALITY_TOPIC"].text_type == "XHTML"
    assert sections["CATALOGUE_ID"].value == "98-500-X2021004"


def test_metadata_targets_closes_the_loop(tmp_path: pathlib.Path) -> None:
    # A search hit's set_urn resolves back to the artefacts it describes.
    cat, code = _indexed_catalog(tmp_path)
    hit = cat.reference_metadata.find("income imputation").items[0]
    targets = cat.metadata_targets(hit.payload.set_urn)
    assert str(code) in targets

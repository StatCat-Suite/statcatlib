"""Search takes one tag, and every hit carries the stored tag of its matched text."""

from __future__ import annotations

from pathlib import Path

import pytest
import sdmxlib
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.concept import Concept, ConceptScheme
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry

import statcatlib as sc
from statcatlib.store.store import _rrf_merge_rows
from tests.conftest import ScratchStore, concept_of, cursor_of, open_scratch

pytestmark = pytest.mark.unit

pytest.importorskip("lancedb", reason="search needs statcatlib[search]")


@pytest.fixture
def store(tmp_path: Path) -> ScratchStore:
    s = open_scratch(tmp_path / "catalog")
    s.put(
        sc.OpenDataset(
            identifier="rain",
            title=sc.InternationalString.of({"en": "Rainfall", "fr": "Précipitations"}),
            description=sc.InternationalString.of({"en": "Rainfall totals", "fr": "Totaux"}),
        )
    )
    s.build_index()
    return s


def test_a_dataset_hit_carries_the_tag_of_its_row(store: ScratchStore) -> None:
    [hit] = store.datasets.find("Précipitations", lang="fr").items
    assert hit.lang == "fr"
    [hit] = store.datasets.find("rainfall", lang="EN").items
    assert hit.lang == "en"


def test_a_query_builder_search_reports_each_items_tag(store: ScratchStore) -> None:
    result = store.query(sc.OpenDataset).search("Précipitations", lang="fr").execute()
    assert result.langs == {"rain": "fr"}


def test_search_takes_one_tag(store: ScratchStore) -> None:
    with pytest.raises(TypeError):
        store.datasets.find("rainfall", lang=("en", "fr"))  # pyrefly: ignore[bad-argument-type]
    with pytest.raises(sdmxlib.InvalidTagError):
        store.datasets.find("rainfall", lang="en_CA")
    with pytest.raises(sdmxlib.InvalidTagError):
        store.query(sc.OpenDataset).search("rainfall", lang="en_CA")


def test_a_tag_with_no_table_finds_nothing(store: ScratchStore) -> None:
    assert store.datasets.find("rainfall", lang="de").items == []


def test_hybrid_keeps_the_row_of_the_ranking_that_places_it_higher() -> None:
    def row(lang: str) -> dict[str, object]:
        return {"id": "x", "code_urn": "u", "lang": lang}

    def key(r: object) -> str:
        return "u"

    # Ranked first by vector search, third by BM25: the vector row wins.
    bm25 = [{"id": "a", "code_urn": "a", "lang": "en"}, {"id": "b", "code_urn": "b", "lang": "en"}, row("en")]
    merged = _rrf_merge_rows(bm25, [row("en-t-fr")], rrf_k=60, key=lambda r: str(r.get("code_urn")))  # pyrefly: ignore[bad-argument-type]
    assert next(r for r, _, _ in merged if r["code_urn"] == "u")["lang"] == "en-t-fr"
    # A tie goes to BM25.
    merged = _rrf_merge_rows([row("en")], [row("en-t-fr")], rrf_k=60, key=key)  # pyrefly: ignore[bad-argument-type]
    assert merged[0][0]["lang"] == "en"


def _sdmx_store(tmp_path: Path) -> ScratchStore:
    s = open_scratch(tmp_path / "catalog")
    s.registry.add(
        Codelist(
            id="CL_GEO",
            maintainer="TEST",
            name=sc.InternationalString(en="Geography"),
            codes=[Code(id="CA", name=sc.InternationalString(fr="Canada (fr)"))],
        )
    )
    s.registry.add(
        ConceptScheme(
            id="CS",
            maintainer="TEST",
            name=sc.InternationalString(en="Concepts"),
            concepts=[Concept(id="GDP", name=sc.InternationalString(en="GDP", fr="PIB"))],
        )
    )
    return s


def test_code_get_picks_the_requested_tag_with_no_english_first(tmp_path: Path) -> None:
    s = _sdmx_store(tmp_path)
    (urn,) = cursor_of(s).execute("SELECT code_urn FROM sdmx.code").fetchone() or ("",)
    hit = s.codes.get(urn, lang=("de", "fr"))
    assert hit is not None
    assert hit.code_label == sc.LocalisedString("Canada (fr)", "fr")
    with pytest.raises(sc.MissingTranslationError) as caught:
        s.codes.get(urn, lang="en")
    assert caught.value.held == ("fr",)
    assert s.codes.get("urn:none", lang="en") is None


def test_concept_get_returns_none_for_a_missing_description(tmp_path: Path) -> None:
    s = _sdmx_store(tmp_path)
    (urn,) = cursor_of(s).execute("SELECT concept_urn FROM sdmx.concept").fetchone() or ("",)
    hit = s.concepts.get(urn, lang="fr")
    assert hit is not None
    assert hit.name == sc.LocalisedString("PIB", "fr")
    assert hit.description is None
    with pytest.raises(sc.MissingTranslationError):
        s.concepts.get(urn, lang="de")


def test_a_dataset_is_indexed_under_the_tags_it_holds_and_no_other(tmp_path: Path) -> None:
    s = open_scratch(tmp_path / "catalog")
    s.put(
        sc.OpenDataset(
            identifier="kw",
            title=sc.InternationalString(),
            description=sc.InternationalString(),
            keywords=[sc.InternationalString.of({"fr": "pluie", "zxx": "RAIN_01"})],
        )
    )
    s.build_index()
    assert set(s.index_layout) == {"fr"}
    assert s.datasets.find("pluie", lang="fr").items[0].lang == "fr"


def test_a_code_row_exists_for_each_tag_its_label_holds(tmp_path: Path) -> None:
    """A code named only in French gets a French row, though the dataflow is titled only in English."""
    geo = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=sc.InternationalString(en="Geography"),
        codes=[Code(id="CA", name=sc.InternationalString(fr="Canada (fr)"))],
    )
    dsd = DataStructure(
        id="DSD",
        maintainer="TEST",
        name=sc.InternationalString(en="D"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=geo)],
    )
    flow = Dataflow(id="DF", maintainer="TEST", name=sc.InternationalString(en="Flow"), structure=dsd)
    registry = InMemoryRegistry()
    for obj in (geo, dsd, flow):
        registry.add(obj)
    s = open_scratch(tmp_path / "catalog")
    sc.ingest_sdmx(s, registry)
    s.build_index()
    [hit] = s.codes.find("Canada", lang="fr").items
    assert hit.payload.code_label == sc.LocalisedString("Canada (fr)", "fr")
    assert s.codes.find("Canada", lang="en").items == []

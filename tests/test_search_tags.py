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


def _paired_store(tmp_path: Path) -> ScratchStore:
    s = open_scratch(tmp_path / "catalog")
    s.add_language_alternates({"en": ["en-t-fr"]}, source="test")
    s.put(
        sc.OpenDataset(
            identifier="ckan",
            title=sc.InternationalString.of({"en-t-fr": "Population estimates", "fr": "Estimations de la population"}),
            description=sc.InternationalString.of({"en": "Annual counts", "fr": "Comptes annuels"}),
        )
    )
    s.build_index()
    return s


def test_an_alternate_tag_is_indexed_in_its_primary_table(tmp_path: Path) -> None:
    s = _paired_store(tmp_path)
    assert set(s.index_layout) == {"en", "fr"}
    [hit] = s.datasets.find("population", lang="en").items
    assert hit.lang == "en-t-fr"
    [hit] = s.datasets.find("annual", lang="en").items
    assert hit.lang == "en"
    # An alternate reads its primary's table.
    assert [h.lang for h in s.datasets.find("population", lang="en-t-fr").items] == ["en-t-fr"]


def test_the_index_records_its_pairs(tmp_path: Path) -> None:
    s = _paired_store(tmp_path)
    index = s._store._lance
    assert index is not None
    assert index.language_pairs == {"en-t-fr": "en"}
    assert index.version == 4


def test_a_code_held_under_a_pair_gives_one_hit_per_dataset(tmp_path: Path) -> None:
    geo = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=sc.InternationalString(en="Geography"),
        codes=[Code(id="CA", name=sc.InternationalString.of({"en": "Canada", "en-t-fr": "Canada"}))],
    )
    dsd = DataStructure(
        id="DSD",
        maintainer="TEST",
        name=sc.InternationalString(en="D"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=geo)],
    )
    registry = InMemoryRegistry()
    for obj in (
        geo,
        dsd,
        Dataflow(id="DF1", maintainer="TEST", name=sc.InternationalString(en="One"), structure=dsd),
        Dataflow(id="DF2", maintainer="TEST", name=sc.InternationalString(en="Two"), structure=dsd),
    ):
        registry.add(obj)
    s = open_scratch(tmp_path / "catalog")
    s.add_language_alternates({"en": ["en-t-fr"]}, source="test")
    sc.ingest_sdmx(s, registry)
    s.build_index()
    hits = s.codes.find("Canada", lang="en").items
    assert sorted(h.payload.dataset_id for h in hits) == ["TEST:DF1(1.0)", "TEST:DF2(1.0)"]
    # Each code has two rows in the table; a page of one is still full, and a
    # page of two holds both datasets.
    assert len(s.codes.find("Canada", lang="en", limit=1).items) == 1
    page = s.codes.find("Canada", lang="en", limit=2).items
    assert {h.payload.dataset_id for h in page} == {"TEST:DF1(1.0)", "TEST:DF2(1.0)"}


def test_add_routes_an_alternate_to_its_primary_table(tmp_path: Path) -> None:
    s = _paired_store(tmp_path)
    index = s._store._lance
    assert index is not None
    row = {"id": "extra", "entity_type": "open", "lang": "en-t-fr", "document": "glacier melt"}
    index.add([row])
    assert set(index.describe()) == {"en", "fr"}
    assert [i for i, _, tag in index.bm25_search("glacier", lang="en")] == ["extra"]


def test_catalog_admin_reports_the_recorded_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "cat"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("f").working_copy(lifecycle="final")
        wc.put(
            sc.OpenDataset(identifier="d", title=sc.InternationalString(en="D"), description=sc.InternationalString())
        )
        wc.seal()
    admin = sc.CatalogAdmin.open(root)
    assert admin.lifecycle("f") == "final"
    assert admin.lifecycle("nothing") is None

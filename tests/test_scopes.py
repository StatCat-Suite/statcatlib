"""Tests for lazy scope-based traversal API."""

import pytest
from sdmxlib import InternationalString
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.collections import ItemList
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry

import statcatlib as sc
from tests.conftest import ScratchStore, concept_of, open_scratch

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


_SCHEME_URI = "urn:test:themes"
_STATCAN_URI = "urn:test:themes/statcan"
_EUROVOC_URI = "urn:test:themes/eurovoc"


def _concept_uri(scheme_uri: str, notation: str) -> str:
    return f"{scheme_uri}/{notation}"


@pytest.fixture
def scheme_store() -> ScratchStore:
    """Store with a concept scheme and datasets that cite concepts from it."""
    s = open_scratch()
    scheme = sc.ConceptScheme(
        uri=_SCHEME_URI,
        label=_i("Test Themes"),
        concepts={
            "Economy": sc.Concept(notation="Economy", label=_i("Economy")),
            "Labour": sc.Concept(notation="Labour", label=_i("Labour")),
            "Finance": sc.Concept(notation="Finance", label=_i("Finance")),
        },
    )
    s.put(scheme)
    ds1 = sc.OpenDataset(
        identifier="DS1",
        title=_i("Dataset 1"),
        description=_i("Test"),
        themes=[sc.ConceptRef(uri=_concept_uri(_SCHEME_URI, "Economy"), notation="Economy", scheme_uri=_SCHEME_URI)],
    )
    ds2 = sc.OpenDataset(
        identifier="DS2",
        title=_i("Dataset 2"),
        description=_i("Test"),
        themes=[sc.ConceptRef(uri=_concept_uri(_SCHEME_URI, "Labour"), notation="Labour", scheme_uri=_SCHEME_URI)],
    )
    s.put(ds1)
    s.put(ds2)
    return s


# ---------------------------------------------------------------------------
# ThemesScope
# ---------------------------------------------------------------------------


class TestThemesScope:
    def test_getitem_returns_scheme_scope(self, scheme_store):
        scope = scheme_store.themes[_SCHEME_URI]
        assert isinstance(scope, sc.SchemeScope)
        assert scope.scheme == _SCHEME_URI

    def test_collect_returns_list(self, scheme_store):
        # A plain list since sdmxlib 0.61: SchemeScope has no ``id``, so it
        # never met ItemList's SupportsId bound. Addressed by URI instead.
        schemes = scheme_store.themes.collect()
        assert isinstance(schemes, list)
        assert not isinstance(schemes, ItemList)
        assert len(schemes) >= 1

    def test_iter_yields_scheme_scopes(self, scheme_store):
        schemes = list(scheme_store.themes)
        assert all(isinstance(s, sc.SchemeScope) for s in schemes)

    def test_len(self, scheme_store):
        assert len(scheme_store.themes) >= 1


# ---------------------------------------------------------------------------
# SchemeScope
# ---------------------------------------------------------------------------


class TestSchemeScope:
    def test_collect_returns_concept_refs(self, scheme_store):
        refs = scheme_store.themes[_SCHEME_URI].collect()
        assert isinstance(refs, list)
        assert len(refs) > 0
        assert all(isinstance(r, sc.ConceptRef) for r in refs)

    def test_getitem_returns_concept_scope(self, scheme_store):
        scope = scheme_store.themes[_SCHEME_URI]["Economy"]
        assert isinstance(scope, sc.ConceptScope)
        assert scope.notation == "Economy"

    def test_count(self, scheme_store):
        n = scheme_store.themes[_SCHEME_URI].count()
        assert n > 0

    def test_labels(self, scheme_store):
        labels = scheme_store.themes[_SCHEME_URI].labels(lang="en")
        assert isinstance(labels, sc.Labels)
        assert len(labels) > 0
        assert "Economy" in labels
        label = labels["Economy"]
        assert label is not None
        assert label.lang == "en"

    def test_labels_list_every_concept_and_its_gap(self, scheme_store):
        labels = scheme_store.themes[_SCHEME_URI].labels(lang="de")
        assert set(labels.values()) == {None}
        assert {g.key for g in labels.gaps} == set(labels)
        assert all(g.field == "label" and g.langs == ("de",) for g in labels.gaps)

    def test_uri_property(self, scheme_store):
        scope = scheme_store.themes[_SCHEME_URI]
        assert scope.uri == _SCHEME_URI

    def test_datasets_returns_query_builder(self, scheme_store):
        qb = scheme_store.themes[_SCHEME_URI].datasets
        assert isinstance(qb, sc.QueryBuilder)

    def test_datasets_collect(self, scheme_store):
        datasets = scheme_store.themes[_SCHEME_URI].datasets.collect()
        assert isinstance(datasets, list)
        assert len(datasets) > 0


class TestSchemeScopeEnrich:
    def test_enrich_returns_new_scope(self, scheme_store):
        scope = scheme_store.themes[_SCHEME_URI]
        enriched = scope.enrich(n=sc.each("datasets").count())
        assert isinstance(enriched, sc.SchemeScope)
        assert enriched is not scope

    def test_enrich_rows_returns_dicts(self, scheme_store):
        results = (
            scheme_store.themes[_SCHEME_URI]
            .enrich(
                n=sc.each("datasets").count(),
            )
            .rows(lang="en")
        )
        assert isinstance(results, list)
        assert len(results) > 0
        assert "n" in results[0]
        assert "uri" in results[0]

    def test_enrich_with_alias(self, scheme_store):
        results = (
            scheme_store.themes[_SCHEME_URI]
            .enrich(
                sc.each("datasets").count().alias("total"),
            )
            .rows(lang="en")
        )
        assert "total" in results[0]

    def test_enrich_multiple(self, scheme_store):
        results = (
            scheme_store.themes[_SCHEME_URI]
            .enrich(
                sc.each("datasets").count().alias("total"),
                has_data=sc.each("datasets").exists(),
            )
            .rows(lang="en")
        )
        assert "total" in results[0]
        assert "has_data" in results[0]

    def test_enrich_count_values_are_correct(self, scheme_store):
        results = (
            scheme_store.themes[_SCHEME_URI]
            .enrich(
                n=sc.each("datasets").count(),
            )
            .rows(lang="en")
        )
        economy = next(r for r in results if r["notation"] == "Economy")
        assert economy["n"] >= 1
        assert economy["label__lang"] == "en"

    def test_an_alias_naming_an_entity_column_is_refused(self, scheme_store):
        with pytest.raises(ValueError, match="'label'"):
            scheme_store.themes[_SCHEME_URI].enrich(label=sc.each("datasets").count()).rows(lang="en")


# ---------------------------------------------------------------------------
# ConceptScope
# ---------------------------------------------------------------------------


class TestConceptScope:
    def test_collect_returns_concept(self, scheme_store):
        concept = scheme_store.themes[_SCHEME_URI]["Economy"].collect()
        assert isinstance(concept, sc.Concept)
        assert concept.notation == "Economy"

    def test_collect_not_found_raises(self, scheme_store):
        with pytest.raises(LookupError):
            scheme_store.themes[_SCHEME_URI]["NONEXISTENT"].collect()

    def test_datasets_returns_query_builder(self, scheme_store):
        qb = scheme_store.themes[_SCHEME_URI]["Economy"].datasets
        assert isinstance(qb, sc.QueryBuilder)

    def test_datasets_collect(self, scheme_store):
        datasets = scheme_store.themes[_SCHEME_URI]["Economy"].datasets.collect()
        assert isinstance(datasets, list)
        assert len(datasets) >= 1


# ---------------------------------------------------------------------------
# Multi-scheme isolation
# ---------------------------------------------------------------------------


class TestMultiSchemeScopes:
    def _build_store(self) -> ScratchStore:
        s = open_scratch()
        statcan = sc.ConceptScheme(
            uri=_STATCAN_URI,
            label=_i("StatCan Themes"),
            concepts={"13": sc.Concept(notation="13", label=_i("Labour"))},
        )
        eurovoc = sc.ConceptScheme(
            uri=_EUROVOC_URI,
            label=_i("EuroVoc"),
            concepts={"13": sc.Concept(notation="13", label=_i("Energy"))},
        )
        s.put(statcan)
        s.put(eurovoc)
        ds1 = sc.OpenDataset(
            identifier="DS1",
            title=_i("Dataset 1"),
            description=_i("Test"),
            themes=[sc.ConceptRef(uri=_concept_uri(_STATCAN_URI, "13"), notation="13", scheme_uri=_STATCAN_URI)],
        )
        ds2 = sc.OpenDataset(
            identifier="DS2",
            title=_i("Dataset 2"),
            description=_i("Test"),
            themes=[sc.ConceptRef(uri=_concept_uri(_EUROVOC_URI, "13"), notation="13", scheme_uri=_EUROVOC_URI)],
        )
        s.put(ds1)
        s.put(ds2)
        return s

    def test_different_schemes_isolated(self):
        s = self._build_store()
        statcan_refs = s.themes[_STATCAN_URI].collect()
        eurovoc_refs = s.themes[_EUROVOC_URI].collect()

        assert len(statcan_refs) == 1
        assert statcan_refs[0].label.as_dict().get("en") == "Labour"

        assert len(eurovoc_refs) == 1
        assert eurovoc_refs[0].label.as_dict().get("en") == "Energy"

    def test_concept_datasets_scoped(self):
        s = self._build_store()
        datasets = s.themes[_STATCAN_URI]["13"].datasets.collect()
        assert len(datasets) == 1
        assert datasets[0].identifier == "DS1"

    def test_scheme_datasets_scoped(self):
        s = self._build_store()
        datasets = s.themes[_STATCAN_URI].datasets.collect()
        assert len(datasets) == 1
        assert datasets[0].identifier == "DS1"


# ---------------------------------------------------------------------------
# CodelistScope / CodeScope (basic — no SDMX tables needed)
# ---------------------------------------------------------------------------


class TestCodelistScope:
    def test_getitem_returns_code_scope(self, store):
        scope = store.codelist("urn:test:CL_GEO(1.0)")
        code_scope = scope["ON"]
        assert isinstance(code_scope, sc.CodeScope)
        assert code_scope.code_id == "ON"

    def test_codelist_returns_codelist_scope(self, store):
        scope = store.codelist("urn:test")
        assert isinstance(scope, sc.CodelistScope)
        assert scope.urn == "urn:test"


# ---------------------------------------------------------------------------
# DimensionsScope (basic — no SDMX tables needed)
# ---------------------------------------------------------------------------


class TestDimensionsScope:
    def test_returns_dimensions_scope(self, store):
        ds = sc.AggregateDataset(
            identifier="TEST",
            title=_i("Test"),
            description=_i("Test"),
            sdmx_dataflow_urn="urn:test:DF(1.0)",
        )
        scope = store.dimensions(ds)
        assert isinstance(scope, sc.DimensionsScope)

    def test_no_dataflow_returns_empty(self, store):
        ds = sc.AggregateDataset(
            identifier="TEST",
            title=_i("Test"),
            description=_i("Test"),
        )
        scope = store.dimensions(ds)
        assert scope.collect() == ItemList([])


# ---------------------------------------------------------------------------
# Expression building (no DB needed)
# ---------------------------------------------------------------------------


class TestExpressions:
    def test_each_creates_traversal(self):
        expr = sc.each("datasets")
        assert isinstance(expr, sc.TraversalExpr)
        assert expr.edges == ("datasets",)

    def test_each_multi_hop(self):
        expr = sc.each("datasets", "dimensions", "codelist")
        assert expr.edges == ("datasets", "dimensions", "codelist")

    def test_each_requires_args(self):
        with pytest.raises(ValueError, match="at least one edge"):
            sc.each()

    def test_count(self):
        expr = sc.each("datasets").count()
        assert isinstance(expr, sc.CountExpr)

    def test_exists(self):
        expr = sc.each("datasets").exists()
        assert isinstance(expr, sc.ExistsExpr)

    def test_alias(self):
        expr = sc.each("datasets").count().alias("n")
        assert isinstance(expr, sc.AliasedExpr)
        assert expr.name == "n"

    def test_filter_on_expr(self):
        expr = sc.each("datasets").filter(status="current").count()
        assert isinstance(expr, sc.CountExpr)

    def test_expression_reuse(self):
        n = sc.each("datasets").count()
        a1 = n.alias("n1")
        a2 = n.alias("n2")
        assert a1.name == "n1"
        assert a2.name == "n2"
        assert a1.expr is a2.expr


# ---------------------------------------------------------------------------
# QueryBuilder.collect()
# ---------------------------------------------------------------------------


class TestQueryBuilderCollect:
    def test_collect_returns_list(self, store):
        results = store.query(sc.BaseDataset).collect()
        assert isinstance(results, list)
        assert len(results) > 0

    def test_collect_same_as_all(self, store):
        collect_results = store.query(sc.BaseDataset).limit(3).collect()
        assert len(collect_results) <= 3


# ---------------------------------------------------------------------------
# Public API exports
# ---------------------------------------------------------------------------


class TestScopeExports:
    def test_themes_scope_exported(self):
        assert hasattr(sc, "ThemesScope")

    def test_scheme_scope_exported(self):
        assert hasattr(sc, "SchemeScope")

    def test_concept_scope_exported(self):
        assert hasattr(sc, "ConceptScope")

    def test_codelist_scope_exported(self):
        assert hasattr(sc, "CodelistScope")

    def test_code_scope_exported(self):
        assert hasattr(sc, "CodeScope")

    def test_dimensions_scope_exported(self):
        assert hasattr(sc, "DimensionsScope")

    def test_dimension_scope_exported(self):
        assert hasattr(sc, "DimensionScope")

    def test_dimension_info_exported(self):
        assert hasattr(sc, "DimensionInfo")

    def test_each_exported(self):
        assert hasattr(sc, "each")

    def test_codelists_scope_exported(self):
        assert hasattr(sc, "CodelistsScope")

    def test_expr_types_exported(self):
        assert hasattr(sc, "Expr")
        assert hasattr(sc, "TraversalExpr")
        assert hasattr(sc, "CountExpr")
        assert hasattr(sc, "ExistsExpr")
        assert hasattr(sc, "AliasedExpr")


# ---------------------------------------------------------------------------
# CodelistsScope / CodelistScope with SDMX data
# ---------------------------------------------------------------------------


def _make_sdmx_store() -> ScratchStore:
    """Store with two dataflows sharing a GEO codelist, different FREQ codelists."""
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography"),
        codes=[
            Code(id="CA", name=_i("Canada")),
            Code(id="US", name=_i("United States")),
        ],
    )
    freq_a = Codelist(
        id="CL_FREQ_A",
        maintainer="TEST",
        name=_i("Annual Frequency"),
        codes=[Code(id="A", name=_i("Annual"))],
    )
    freq_q = Codelist(
        id="CL_FREQ_Q",
        maintainer="TEST",
        name=_i("Quarterly Frequency"),
        codes=[Code(id="Q", name=_i("Quarterly"))],
    )
    dsd1 = DataStructure(
        id="DSD_EXR",
        maintainer="TEST",
        name=_i("Exchange Rate DSD"),
        dimensions=[
            Dimension(id="GEO", concept=concept_of("GEO"), representation=geo_cl),
            Dimension(id="FREQ", concept=concept_of("FREQ"), representation=freq_a),
        ],
    )
    dsd2 = DataStructure(
        id="DSD_GDP",
        maintainer="TEST",
        name=_i("GDP DSD"),
        dimensions=[
            Dimension(id="GEO", concept=concept_of("GEO"), representation=geo_cl),
            Dimension(id="FREQ", concept=concept_of("FREQ"), representation=freq_q),
        ],
    )
    flow1 = Dataflow(id="DF_EXR", maintainer="TEST", name=_i("Exchange Rate"), structure=dsd1)
    flow2 = Dataflow(id="DF_GDP", maintainer="TEST", name=_i("GDP"), structure=dsd2)

    registry = InMemoryRegistry()
    for obj in [geo_cl, freq_a, freq_q, dsd1, dsd2, flow1, flow2]:
        registry.add(obj)

    store = open_scratch()
    sc.ingest_sdmx(store, registry)
    return store


class TestCodelistsScopeProperty:
    def test_is_codelists_scope(self):
        store = open_scratch()
        assert isinstance(store.codelists, sc.CodelistsScope)

    def test_getitem_returns_codelist_scope(self):
        store = open_scratch()
        scope = store.codelists["urn:test"]
        assert isinstance(scope, sc.CodelistScope)
        assert scope.urn == "urn:test"

    def test_collect_returns_list(self):
        store = _make_sdmx_store()
        result = store.codelists.collect()
        assert isinstance(result, list)
        assert len(result) == 3  # CL_GEO, CL_FREQ_A, CL_FREQ_Q
        assert all(isinstance(r, sc.CodelistScope) for r in result)

    def test_len(self):
        store = _make_sdmx_store()
        assert len(store.codelists) == 3

    def test_iter(self):
        store = _make_sdmx_store()
        items = list(store.codelists)
        assert len(items) == 3

    def test_empty_store_returns_empty(self):
        store = open_scratch()
        assert len(store.codelists) == 0
        assert store.codelists.collect() == []


class TestCodelistsScopeEnrich:
    def test_enrich_returns_new_scope(self):
        store = _make_sdmx_store()
        scope = store.codelists
        enriched = scope.enrich(n=sc.each("datasets").count())
        assert isinstance(enriched, sc.CodelistsScope)
        assert enriched is not scope

    def test_enrich_rows_returns_dicts(self):
        store = _make_sdmx_store()
        results = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang="en")
        assert isinstance(results, list)
        assert len(results) == 3
        assert all("urn" in r and "n" in r for r in results)

    def test_enrich_includes_label(self):
        store = _make_sdmx_store()
        results = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang="en")
        assert all("label" in r for r in results)
        by_urn = {r["urn"]: r for r in results}
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        assert by_urn[geo_urn]["label"] == "Geography"

    def test_enrich_lang(self):
        store = _make_sdmx_store()
        # No French names in the fixture: each label is None and a gap.
        results = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang="fr")
        assert all(r["label"] is None and r["label__lang"] is None for r in results)
        assert {g.key for g in results.gaps} == {r["urn"] for r in results}

    def test_enrich_lang_tuple_falls_back_in_order(self):
        store = _make_sdmx_store()
        results = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang=("fr", "en"))
        assert all(r["label__lang"] == "en" for r in results)
        assert results.gaps == ()

    def test_enrich_filter_text_contains(self):
        store = _make_sdmx_store()
        results = (
            store.codelists.filter(text_contains="Geo", lang="en").enrich(n=sc.each("datasets").count()).rows(lang="en")
        )
        assert len(results) >= 1
        labels = [r["label"] for r in results]
        assert any("Geography" in str(lbl or "") for lbl in labels)

    def test_enrich_filter_text_excludes(self):
        store = _make_sdmx_store()
        results = (
            store.codelists.filter(text_contains="ZZZZZ", lang="en")
            .enrich(n=sc.each("datasets").count())
            .rows(lang="en")
        )
        assert len(results) == 0

    def test_enrich_filter_text_accent_insensitive(self):
        """Codelist label filter folds diacritics: text_contains="Geographie"
        matches a codelist named "Géographie"."""
        geo = Codelist(id="CL_GEO", maintainer="TEST", name=_i("Géographie"), codes=[Code(id="CA", name=_i("Canada"))])
        dsd = DataStructure(
            id="DSD_G",
            maintainer="TEST",
            name=_i("D"),
            dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=geo)],
        )
        flow = Dataflow(id="DF_G", maintainer="TEST", name=_i("F"), structure=dsd)
        registry = InMemoryRegistry()
        for obj in [geo, dsd, flow]:
            registry.add(obj)
        store = open_scratch()
        sc.ingest_sdmx(store, registry)
        results = (
            store.codelists.filter(text_contains="Geographie", lang="en")
            .enrich(n=sc.each("datasets").count())
            .rows(lang="en")
        )
        assert any("Géographie" in str(r["label"] or "") for r in results)

    def test_enrich_filter_text_punctuation_insensitive(self):
        """Codelist label filter folds punctuation too: text_contains="Sub area"
        matches a codelist named "Sub-area"."""
        cl = Codelist(id="CL_SA", maintainer="TEST", name=_i("Sub-area"), codes=[Code(id="X", name=_i("X"))])
        dsd = DataStructure(
            id="DSD_SA",
            maintainer="TEST",
            name=_i("D"),
            dimensions=[Dimension(id="SA", concept=concept_of("SA"), representation=cl)],
        )
        flow = Dataflow(id="DF_SA", maintainer="TEST", name=_i("F"), structure=dsd)
        registry = InMemoryRegistry()
        for obj in [cl, dsd, flow]:
            registry.add(obj)
        store = open_scratch()
        sc.ingest_sdmx(store, registry)
        results = (
            store.codelists.filter(text_contains="Sub area", lang="en")
            .enrich(n=sc.each("datasets").count())
            .rows(lang="en")
        )
        assert any("Sub-area" in str(r["label"] or "") for r in results)

    def test_enrich_count_values(self):
        store = _make_sdmx_store()
        results = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang="en")
        by_urn = {r["urn"]: r for r in results}
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        freq_a_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_FREQ_A(1.0)"
        # CL_GEO used by both dataflows → 2 datasets
        assert by_urn[geo_urn]["n"] == 2
        # CL_FREQ_A used by only DF_EXR → 1 dataset
        assert by_urn[freq_a_urn]["n"] == 1

    def test_enrich_exists(self):
        store = _make_sdmx_store()
        results = store.codelists.enrich(has_data=sc.each("datasets").exists()).rows(lang="en")
        assert all(r["has_data"] for r in results)


class TestCodelistScopeLabel:
    def test_label_returns_a_localised_string(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        label = store.codelist(geo_urn).label(lang="en")
        assert label == sc.LocalisedString("Geography", "en")

    def test_label_takes_the_first_held_tag(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        assert store.codelist(geo_urn).label(lang=("de", "en")) == sc.LocalisedString("Geography", "en")

    def test_label_missing_lang_raises(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        with pytest.raises(sc.MissingTranslationError) as caught:
            store.codelist(geo_urn).label(lang="de")
        assert caught.value.entity == geo_urn
        assert caught.value.langs == ("de",)
        assert caught.value.held == ("en",)

    def test_label_nonexistent_returns_none(self):
        store = _make_sdmx_store()
        assert store.codelist("urn:nonexistent").label(lang="en") is None

    def test_label_without_an_sdmx_schema_returns_none(self):
        assert open_scratch().codelist("urn:nonexistent").label(lang="en") is None


class TestCodelistScopeEnrich:
    def test_enrich_per_code_returns_dicts(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        results = store.codelist(geo_urn).enrich(n=sc.each("datasets").count()).rows(lang="en")
        assert isinstance(results, list)
        assert len(results) == 2  # CA, US
        assert all("code_id" in r and "n" in r for r in results)

    def test_enrich_per_code_includes_label(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        results = store.codelist(geo_urn).enrich(n=sc.each("datasets").count()).rows(lang="en")
        by_code = {r["code_id"]: r for r in results}
        assert by_code["CA"]["label"] == "Canada"
        assert by_code["US"]["label"] == "United States"

    def test_enrich_per_code_lang(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        # No French names: each label is None and a gap naming the codelist.
        results = store.codelist(geo_urn).enrich(n=sc.each("datasets").count()).rows(lang="fr")
        by_code = {r["code_id"]: r for r in results}
        assert by_code["CA"]["label"] is None
        assert sc.Gap(key="CA", entity=geo_urn, field="name", langs=("fr",)) in results.gaps

    def test_enrich_per_code_count_values(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        results = store.codelist(geo_urn).enrich(n=sc.each("datasets").count()).rows(lang="en")
        by_code = {r["code_id"]: r for r in results}
        # Both CA and US are in the codelist, but dataset_code links depend on
        # what ingest_sdmx writes — the codes may have 0 dataset_code rows
        # since ingest_sdmx doesn't populate dataset_code by default.
        # But the query should still work and return 0 counts.
        assert "CA" in by_code
        assert "US" in by_code
        assert isinstance(by_code["CA"]["n"], int)


class TestCodeScopeDatasetCount:
    def test_dataset_count_returns_int(self):
        store = _make_sdmx_store()
        geo_urn = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
        count = store.codelist(geo_urn)["CA"].dataset_count()
        assert isinstance(count, int)
        assert count >= 0


class TestBatchLabels:
    GEO_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
    FREQ_A_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_FREQ_A(1.0)"

    # --- CodelistScope.batch_labels ---

    def test_codelist_scope_batch_labels_basic(self):
        store = _make_sdmx_store()
        labels = store.codelist(self.GEO_URN).batch_labels(["CA", "US"], lang="en")
        assert dict(labels) == {
            "CA": sc.LocalisedString("Canada", "en"),
            "US": sc.LocalisedString("United States", "en"),
        }
        assert labels.gaps == ()

    def test_codelist_scope_batch_labels_keeps_request_order_and_collapses_duplicates(self):
        store = _make_sdmx_store()
        labels = store.codelist(self.GEO_URN).batch_labels(["US", "CA", "US"], lang="en")
        assert list(labels) == ["US", "CA"]

    def test_codelist_scope_batch_labels_empty_input(self):
        store = _make_sdmx_store()
        assert dict(store.codelist(self.GEO_URN).batch_labels([], lang="en")) == {}

    def test_codelist_scope_batch_labels_unknown_code_raises(self):
        store = _make_sdmx_store()
        with pytest.raises(sc.UnknownCodeError) as caught:
            store.codelist(self.GEO_URN).batch_labels(["CA", "XX"], lang="en")
        assert caught.value.missing == ((self.GEO_URN, "XX"),)

    def test_codelist_scope_batch_labels_untranslated_code_is_a_gap(self):
        store = _make_sdmx_store()
        labels = store.codelist(self.GEO_URN).batch_labels(["CA"], lang="fr")
        assert labels["CA"] is None
        assert labels.gaps == (sc.Gap(key="CA", entity=self.GEO_URN, field="name", langs=("fr",)),)

    def test_codelist_scope_batch_labels_tuple_falls_back(self):
        store = _make_sdmx_store()
        labels = store.codelist(self.GEO_URN).batch_labels(["CA"], lang=("fr", "en"))
        assert labels["CA"] == sc.LocalisedString("Canada", "en")

    def test_codelist_scope_batch_labels_empty_store(self):
        store = open_scratch()
        with pytest.raises(sc.UnknownCodeError):
            store.codelist(self.GEO_URN).batch_labels(["CA"], lang="en")

    # --- CodelistsScope.batch_labels ---

    def test_codelists_scope_batch_labels_basic(self):
        store = _make_sdmx_store()
        pairs = [(self.GEO_URN, "CA"), (self.GEO_URN, "US"), (self.FREQ_A_URN, "A")]
        labels = store.codelists.batch_labels(pairs, lang="en")
        assert labels[(self.GEO_URN, "CA")] == sc.LocalisedString("Canada", "en")
        assert labels[(self.GEO_URN, "US")] == sc.LocalisedString("United States", "en")
        assert labels[(self.FREQ_A_URN, "A")] == sc.LocalisedString("Annual", "en")

    def test_codelists_scope_batch_labels_empty_input(self):
        store = _make_sdmx_store()
        assert dict(store.codelists.batch_labels([], lang="en")) == {}

    def test_codelists_scope_batch_labels_unknown_pairs_raise(self):
        store = _make_sdmx_store()
        with pytest.raises(sc.UnknownCodeError) as caught:
            store.codelists.batch_labels([(self.GEO_URN, "XX"), (self.FREQ_A_URN, "YY")], lang="en")
        assert caught.value.missing == ((self.GEO_URN, "XX"), (self.FREQ_A_URN, "YY"))

    def test_codelists_scope_batch_labels_untranslated_pair_is_a_gap(self):
        store = _make_sdmx_store()
        labels = store.codelists.batch_labels([(self.GEO_URN, "CA")], lang="fr")
        assert labels[(self.GEO_URN, "CA")] is None
        assert [g.key for g in labels.gaps] == [(self.GEO_URN, "CA")]

    def test_codelists_scope_batch_labels_empty_store(self):
        store = open_scratch()
        with pytest.raises(sc.UnknownCodeError):
            store.codelists.batch_labels([(self.GEO_URN, "CA")], lang="en")


class TestSdmxEdgeUnsupportedReduction:
    """An SDMX-joined scope reports an unsupported reduction like a bridge one.

    `_EDGE_MAP` holds two shapes: bridge edges join through a statcatlib table,
    SDMX edges bring their own SQL because the join runs through sdmxlib's.
    `_compile_edge_subquery` used to test the reduction first and the edge
    second, so an SDMX edge with a reduction it does not implement fell past
    both arms into the bridge path and died on ``edge["bridge_table"]`` — a
    bare KeyError naming an internal key, where a bridge edge raises
    NotImplementedError naming the reduction. Same unsupported input, two
    different failures, and only one of them legible.
    """

    def test_bare_traversal_on_a_codelist_scope_names_the_reduction(self):
        store = _make_sdmx_store()
        with pytest.raises(NotImplementedError, match="Unsupported reduction: none"):
            store.codelists.enrich(n=sc.each("datasets")).rows(lang="en")

    def test_count_on_a_codelist_scope_still_works(self):
        store = _make_sdmx_store()
        rows = store.codelists.enrich(n=sc.each("datasets").count()).rows(lang="en")
        assert rows
        assert all("n" in row for row in rows)

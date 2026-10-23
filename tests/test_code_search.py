"""Integration tests for code-level search indexing.

Verifies that ``build_index(include_codes=True)`` emits search rows for
individual SDMX codes alongside dataset-level text.
"""

from __future__ import annotations

import pathlib

import pytest
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString

from statcatlib.sources.sdmx import ingest_sdmx
from statcatlib.store.store import Catalog
from tests.conftest import concept_of

pytestmark = pytest.mark.integration


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _make_registry() -> InMemoryRegistry:
    """Build a minimal registry with two dimensions (2 codes each)."""
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[
            Code(id="CA", name=_i("Canada")),
            Code(id="US", name=_i("United States", "États-Unis")),
        ],
    )
    freq_cl = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency", "Fréquence"),
        codes=[
            Code(id="A", name=_i("Annual", "Annuel")),
            Code(id="M", name=_i("Monthly", "Mensuel")),
        ],
    )
    dsd = DataStructure(
        id="DSD_TEST",
        maintainer="TEST",
        name=_i("Test DSD"),
        dimensions=[
            Dimension(id="GEO", concept=concept_of("GEO"), representation=geo_cl),
            Dimension(id="FREQ", concept=concept_of("FREQ"), representation=freq_cl),
        ],
    )
    flow = Dataflow(
        id="DF_TEST",
        maintainer="TEST",
        name=_i("Test Dataflow", "Flux de test"),
        description=_i("A test dataflow"),
        structure=dsd,
    )
    registry = InMemoryRegistry()
    registry.add(geo_cl)
    registry.add(freq_cl)
    registry.add(dsd)
    registry.add(flow)
    return registry


def _build_store_with_sdmx() -> Catalog:
    """Create an in-memory Catalog with one SDMX dataset (2 dims, 4 codes)."""
    store = Catalog().open()
    ingest_sdmx(store, _make_registry())
    return store


class TestCollectCodeTexts:
    def test_returns_code_rows(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        assert len(items) > 0

    def test_entity_type_is_code(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        for row in items:
            assert row["entity_type"] == "code"

    def test_text_contains_code_label(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        en_texts = [r["text"] for r in items if r["lang"] == "en"]
        labels = {"Canada", "United States", "Annual", "Monthly"}
        found = {label for label in labels if any(label in t for t in en_texts)}
        assert found == labels

    def test_text_contains_dataset_title(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        en_texts = [r["text"] for r in items if r["lang"] == "en"]
        assert all("Test Dataflow" in t for t in en_texts)

    def test_text_contains_dimension_label(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        en_texts = [r["text"] for r in items if r["lang"] == "en"]
        geo_texts = [t for t in en_texts if "Canada" in t]
        assert any("Geography" in t for t in geo_texts)

    def test_bilingual_rows(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        langs = {r["lang"] for r in items}
        assert "en" in langs
        assert "fr" in langs

    def test_french_labels(self) -> None:
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        fr_texts = [r["text"] for r in items if r["lang"] == "fr"]
        assert any("Flux de test" in t for t in fr_texts)
        assert any("Géographie" in t for t in fr_texts)

    def test_structured_columns_populated(self) -> None:
        """Code rows must carry dimension_id/code_id alongside the labels (#83)."""
        store = _build_store_with_sdmx()
        items = store._collect_code_texts()
        canada = next(r for r in items if r["lang"] == "en" and r["code_label"] == "Canada")
        assert canada["dimension_id"] == "GEO"
        assert canada["code_id"] == "CA"
        assert canada["dimension_label"] == "Geography"

    def test_max_codes_per_codelist_filters(self) -> None:
        store = _build_store_with_sdmx()
        # Both codelists have 2 codes, so max=1 should skip them all, and say so.
        with pytest.warns(UserWarning, match="skipped 2 codelist"):
            items = store._collect_code_texts(max_codes_per_codelist=1)
        assert len(items) == 0

    def test_empty_without_sdmx(self) -> None:
        store = Catalog().open()
        items = store._collect_code_texts()
        assert items == []


class TestBuildIndexWithCodes:
    def test_build_index_includes_codes(self, tmp_path: pathlib.Path) -> None:
        """Verify build_index with include_codes adds code rows to the index."""
        store = Catalog(tmp_path / "catalog").open()
        registry = _make_registry()
        ingest_sdmx(store, registry)

        dataset_texts = store._collect_search_texts()
        code_texts = store._collect_code_texts()
        assert len(code_texts) > 0
        assert len(dataset_texts) > 0

        n = store.build_index(include_codes=True)
        assert n == 1

    def test_build_index_excludes_codes(self, tmp_path: pathlib.Path) -> None:
        """Verify include_codes=False skips code rows."""
        store = Catalog(tmp_path / "catalog").open()
        registry = _make_registry()
        ingest_sdmx(store, registry)

        n = store.build_index(include_codes=False)
        assert n == 1


class TestCodesScope:
    """Tests for cat.codes.find() (#83 / #95)."""

    def test_find_locates_label(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(include_codes=True)

        result = store.codes.find("Canada", lang="en", limit=10)
        assert any(h.payload.code_label == "Canada" for h in result.items)

    def test_find_returns_structured_fields(self, tmp_path: pathlib.Path) -> None:
        """Hits expose dimension_id and code_id directly — no text re-parsing."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(include_codes=True)

        result = store.codes.find("Canada", lang="en")
        canada = next(h for h in result.items if h.payload.code_label == "Canada")
        assert canada.payload.dimension_id == "GEO"
        assert canada.payload.code_id == "CA"
        assert canada.payload.dimension_label == "Geography"
        assert canada.lang == "en"
        assert canada.score > 0
        assert canada.kind == "code"
        assert canada.source == "bm25"

    def test_find_french(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(include_codes=True)

        result = store.codes.find("Mensuel", lang="fr", limit=10)
        assert any(h.payload.code_id == "M" for h in result.items)

    def test_find_dataset_filter(self, tmp_path: pathlib.Path) -> None:
        """dataset_ids restricts matches to the named datasets only."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(include_codes=True)

        result = store.codes.find("Canada", dataset_ids=["does-not-exist"])
        assert result.items == []

        all_hits = store.codes.find("Canada", lang="en").items
        ds_id = all_hits[0].payload.dataset_id
        scoped = store.codes.find("Canada", dataset_ids=[ds_id], lang="en").items
        assert all(h.payload.dataset_id == ds_id for h in scoped)
        assert any(h.payload.code_label == "Canada" for h in scoped)

    def test_find_returns_empty_without_index(self, tmp_path: pathlib.Path) -> None:
        """Without a built index, codes.find degrades gracefully (#77)."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store._lance = None
        assert store.codes.find("anything").items == []

    def test_find_excludes_dataset_rows(self, tmp_path: pathlib.Path) -> None:
        """Dataset-level rows must never leak into codes.find results."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(include_codes=True)

        result = store.codes.find("Test", lang="en", limit=20)
        assert all(h.payload.code_id for h in result.items)
        assert all(h.kind == "code" for h in result.items)


class TestEmbedCodes:
    """Tests for the embed_codes flag (#84)."""

    def _stub_model(self):
        import math

        class _Stub:
            DIM = 8

            def encode(self, text):
                if isinstance(text, list):
                    return [self._encode_one(t) for t in text]
                return self._encode_one(text)

            def _encode_one(self, t):
                h = hash(t)
                v = [(h >> i & 0xFF) / 255.0 for i in range(self.DIM)]
                norm = math.sqrt(sum(x**2 for x in v)) or 1.0
                return [x / norm for x in v]

        return _Stub()

    def _count_encode_calls(self, model, *, encoded: list[str]):
        """Wrap a model so we can count how many texts it has encoded."""
        original = model.encode

        def wrapped(text):
            if isinstance(text, list):
                encoded.extend(text)
            else:
                encoded.append(text)
            return original(text)

        model.encode = wrapped
        return model

    def test_embed_codes_default_false_skips_code_embedding(self, tmp_path: pathlib.Path) -> None:
        """Default behaviour: code rows are indexed for BM25 but not embedded."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        model = self._stub_model()
        encoded: list[str] = []
        self._count_encode_calls(model, encoded=encoded)

        store.build_index(embedding_model=model, include_codes=True)

        # Dataset row text contains "A test dataflow"; code rows contain
        # "Geography: Canada" etc. With embed_codes=False, no code-row
        # text should have been encoded.
        assert any("test dataflow" in t.lower() for t in encoded), "dataset row was not encoded"
        assert not any("Geography:" in t for t in encoded), "code row was encoded despite embed_codes=False"

    def test_embed_codes_true_encodes_code_rows(self, tmp_path: pathlib.Path) -> None:
        """Opt-in: embed_codes=True encodes every code row's text."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        model = self._stub_model()
        encoded: list[str] = []
        self._count_encode_calls(model, encoded=encoded)

        store.build_index(embedding_model=model, include_codes=True, embed_codes=True)

        assert any("Geography:" in t for t in encoded), "code row was not encoded"

    def test_find_works_with_embed_codes_false(self, tmp_path: pathlib.Path) -> None:
        """BM25 over code rows still works when embeddings are skipped."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry())
        store.build_index(embedding_model=self._stub_model(), include_codes=True)

        result = store.codes.find("Canada", lang="en", limit=5)
        assert any(h.payload.code_label == "Canada" for h in result.items)

"""Tests for semantic and hybrid search via LanceDB.

Uses a deterministic stub embedding model — no ML dependency required.
All tests are unit-marked; they write a LanceDB dataset to a temp directory.
"""

from __future__ import annotations

import contextlib
import math
import warnings
from pathlib import Path
from typing import override

import pytest
from sdmxlib import InternationalString as I18n

import statcatlib as sc
import statcatlib.query as q
from statcatlib.store.lance import LanceIndex, SearchRow, _dedupe_by_id, _entity_type_clause, _prepare_query
from tests.conftest import (
    ScratchStore,
    concept_of,
    open_scratch,
    require_index,
    require_table,
    scratch_store,
    seal_and_view,
    search_table,
)

pytestmark = pytest.mark.unit

#: The edition a test's `LanceIndex` writes tables for. A writable index needs
#: one; an index opened without one is the read-only index of a sealed edition.
_EDITION = "01a198fc-0000-7000-8000-000000000001"
#: The edition after `_EDITION`, for a rebuild over tables `_EDITION` wrote.
_NEXT_EDITION = "01a198fc-0000-7000-8000-000000000002"


def _search_dir(store: ScratchStore) -> Path:
    """The working copy's ``search/`` directory, where ``embeddings.json`` and ``index.json`` live."""
    search_dir = store._store._search_dir
    assert search_dir is not None, "a working copy always has a search directory"
    return search_dir


# ---------------------------------------------------------------------------
# Stub embedding model
# ---------------------------------------------------------------------------


class _StubModel:
    """Deterministic embedding model for tests — no ML library needed.

    Encodes text to a unit vector derived from the string's hash.
    Two calls with the same text always return the same vector.
    Accepts a single string or a list of strings (like sentence-transformers).
    """

    DIM = 8

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        if isinstance(text, list):
            return [self._encode_one(t) for t in text]
        return self._encode_one(text)

    def _encode_one(self, text: str) -> list[float]:
        h = hash(text)
        v = [(h >> i & 0xFF) / 255.0 for i in range(self.DIM)]
        norm = math.sqrt(sum(x**2 for x in v)) or 1.0
        return [x / norm for x in v]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def model() -> _StubModel:
    return _StubModel()


@pytest.fixture
def store_with_model(tmp_path, model):
    """A working copy with an embedding model, pre-loaded with data and indexed."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    datasets = [
        sc.OpenDataset(
            identifier="lfs-2024",
            title=I18n(en="Labour Force Survey 2024"),
            description=I18n(en="Monthly labour market statistics for Canada"),
        ),
        sc.OpenDataset(
            identifier="cpi-2024",
            title=I18n(en="Consumer Price Index 2024"),
            description=I18n(en="Monthly inflation and price level measures"),
        ),
        sc.OpenDataset(
            identifier="gdp-2024",
            title=I18n(en="Gross Domestic Product 2024"),
            description=I18n(en="Annual economic output and national accounts"),
        ),
        sc.OpenDataset(
            identifier="pop-2024",
            title=I18n(en="Population Estimates 2024"),
            description=I18n(en="Annual demographic estimates for provinces"),
        ),
        sc.OpenDataset(
            identifier="trade-2024",
            title=I18n(en="International Trade Statistics 2024"),
            description=I18n(en="Monthly exports imports merchandise trade data"),
        ),
    ]
    for ds in datasets:
        store.put(ds)
    store.build_index()
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_semantic_search_returns_results(store_with_model):
    results = store_with_model.datasets.find(mode="vector", query="employment and jobs", lang="en", limit=3)
    assert isinstance(results, sc.SearchResult)
    assert len(results.items) <= 3
    assert all(isinstance(ds.payload, sc.BaseDataset) for ds in results.items)
    assert results.total >= 0


@pytest.mark.unit
def test_semantic_search_pagination(store_with_model):
    page1 = store_with_model.datasets.find(mode="vector", query="economic data", lang="en", limit=2, offset=0)
    page2 = store_with_model.datasets.find(mode="vector", query="economic data", lang="en", limit=2, offset=2)
    ids1 = {ds.payload.identifier for ds in page1.items}
    ids2 = {ds.payload.identifier for ds in page2.items}
    # Pages should not overlap (may be empty if not enough results)
    assert ids1.isdisjoint(ids2)


@pytest.mark.unit
def test_hybrid_search_returns_results(store_with_model):
    results = store_with_model.datasets.find(mode="hybrid", query="price inflation", lang="en", limit=3)
    assert isinstance(results, sc.SearchResult)
    assert len(results.items) <= 3
    assert all(isinstance(ds.payload, sc.BaseDataset) for ds in results.items)


@pytest.mark.unit
def test_bm25_search_is_accent_insensitive(tmp_path, model):
    """The FTS index is built with ascii_folding, so a BM25 query for
    "Montreal" / "Quebec" matches indexed "Montréal" / "Québec". Tantivy
    applies the same folding tokenizer to the query, so it's symmetric."""
    store = open_scratch(tmp_path / "accent", embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="mtl",
            title=I18n(en="Recensement de Montréal et Québec"),
            description=I18n(en="Données régionales"),
        )
    )
    store.build_index()
    res = store.datasets.find(mode="bm25", query="Montreal", lang="en", limit=5)
    assert any(ds.payload.identifier == "mtl" for ds in res.items), "un-accented query must match accented title"
    res2 = store.datasets.find(mode="bm25", query="quebec", lang="en", limit=5)
    assert any(ds.payload.identifier == "mtl" for ds in res2.items)


@pytest.mark.unit
def test_hybrid_search_with_querybuilder_filter(store_with_model, tmp_path):
    """hybrid_search() chained on QueryBuilder respects .filter() constraints."""
    # Tag one dataset with status so we can filter
    ds = store_with_model.get(sc.OpenDataset, "lfs-2024")
    assert ds is not None
    ds.status = "current"
    store_with_model.put(ds)
    store_with_model.build_index()

    results = (
        store_with_model.query(sc.OpenDataset)
        .filter(status="current")
        .hybrid_search("labour employment jobs")
        .execute()
    )
    assert all(d.status == "current" for d in results.items)


@pytest.mark.unit
def test_without_model_semantic_raises(tmp_path):
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Test Dataset"),
            description=I18n(en="A test"),
        )
    )
    with pytest.raises(RuntimeError, match="embedding_model"):
        store.datasets.find(mode="vector", query="test query")


@pytest.mark.unit
def test_without_model_hybrid_raises(tmp_path):
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Test Dataset"),
            description=I18n(en="A test"),
        )
    )
    with pytest.raises(RuntimeError, match="embedding_model"):
        store.datasets.find(mode="hybrid", query="test query")


@pytest.mark.unit
def test_embeddings_survive_restart(tmp_path, model):
    """Sealing and re-opening the edition read-only preserves the Lance dataset."""
    catalog_path = tmp_path / "catalog"
    store = open_scratch(catalog_path, embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="restart-test",
            title=I18n(en="Restart Test Dataset"),
            description=I18n(en="Should survive catalog restart"),
        )
    )
    store.build_index()
    store.seal()

    # Re-open the sealed edition with the same model
    with sc.Catalog.open(catalog_path) as cat:
        view = cat.product("p").view(embedding_model=model)
        results = view.datasets.find(mode="vector", query="restart test", lang="en", limit=5)
        identifiers = [ds.payload.identifier for ds in results.items]
        assert "restart-test" in identifiers


@pytest.mark.unit
def test_hybrid_search_empty_store(tmp_path, model):
    """Hybrid search on an empty store returns empty results, no crash."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    results = store.datasets.find(mode="hybrid", query="anything", lang="en", limit=10)
    assert results.items == []
    assert results.total == 0


@pytest.mark.unit
def test_semantic_search_empty_store(tmp_path, model):
    """Semantic search on an empty store returns empty results, no crash."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    results = store.datasets.find(mode="vector", query="anything", lang="en", limit=10)
    assert results.items == []
    assert results.total == 0


@pytest.mark.unit
def test_put_updates_embeddings(tmp_path, model):
    """Re-putting a dataset replaces its embedding (idempotent)."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    ds = sc.OpenDataset(
        identifier="update-test",
        title=I18n(en="Original Title"),
        description=I18n(en="Original description text"),
    )
    store.put(ds)
    ds.description = I18n(en="Updated description with new content")
    store.put(ds)  # should not raise or duplicate
    store.build_index()

    results = store.datasets.find(mode="vector", query="updated new content", lang="en", limit=5)
    assert len(results.items) <= 5  # no assertion on rank — stub model is hash-based


@pytest.mark.unit
def test_search_by_theme_label(tmp_path):
    """BM25 search should find datasets via their theme labels."""
    store = open_scratch(tmp_path / "catalog")
    # Concept must be stored so its label is available for indexing
    scheme = sc.ConceptScheme(
        uri="urn:test:themes",
        label=I18n(en="Test Themes"),
        concepts={"ENV": sc.Concept(notation="ENV", label=I18n(en="Environment"))},
    )
    store.put(scheme)
    ds = sc.OpenDataset(
        identifier="themed-ds",
        title=I18n(en="Generic Dataset"),
        description=I18n(en="No mention of the theme in title or description"),
        themes=[sc.ConceptRef(uri="urn:test:themes/ENV", notation="ENV", scheme_uri="urn:test:themes")],
    )
    store.put(ds)
    store.build_index()

    results = store.datasets.find("Environment")
    ids = [d.payload.identifier for d in results.items]
    assert "themed-ds" in ids


@pytest.mark.unit
def test_build_index_batch_encoding(tmp_path, model):
    """build_index() with an embedding model uses batch encoding."""
    store = open_scratch(tmp_path / "catalog")
    datasets = [
        sc.OpenDataset(
            identifier=f"batch-{i}",
            title=I18n(en=f"Dataset {i}"),
            description=I18n(en=f"Description for dataset {i}"),
        )
        for i in range(5)
    ]
    store.put(datasets)
    count = store.build_index(embedding_model=model)
    assert count == 5

    # Verify embeddings were written — semantic search should work
    results = store.datasets.find(mode="vector", query="Dataset 3", lang="en", limit=5)
    assert len(results.items) > 0


@pytest.mark.unit
def test_build_index_progress_callback(tmp_path, model):
    """build_index() invokes the progress callback at each stage."""
    store = open_scratch(tmp_path / "catalog")
    for i in range(3):
        store.put(
            sc.OpenDataset(
                identifier=f"prog-{i}",
                title=I18n(en=f"Progress {i}"),
                description=I18n(en=f"Description {i}"),
            )
        )
    calls: list[tuple[int, int]] = []
    store.build_index(embedding_model=model, progress=lambda cur, tot: calls.append((cur, tot)))
    assert len(calls) >= 2
    assert calls[0] == (0, calls[0][1])  # first call is (0, total)
    assert calls[-1][0] == calls[-1][1]  # last call is (total, total)


@pytest.mark.unit
def test_build_index_idempotent(tmp_path):
    """Calling build_index() twice should not duplicate rows."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="idem-ds",
            title=I18n(en="Idempotent Test"),
            description=I18n(en="Should not duplicate"),
        )
    )
    store.build_index()
    store.build_index()

    results = store.datasets.find("Idempotent")
    ids = [d.payload.identifier for d in results.items]
    assert ids.count("idem-ds") == 1


@pytest.mark.unit
def test_build_index_creates_vector_ann_index(tmp_path, model):
    """build_index() must create an ANN index on the 'embedding' column.

    Regression: create_index() was called without
    vector_column_name="embedding", so LanceDB raised on the default
    "vector" column and contextlib.suppress swallowed it — leaving every
    bundle with only an FTS index and forcing brute-force vector scans.
    """
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    # IVF-PQ training requires >= 256 rows; create enough datasets to trip it.
    datasets = [
        sc.OpenDataset(
            identifier=f"ds-{i:04d}",
            title=I18n(en=f"Dataset {i}"),
            description=I18n(en=f"Description for dataset {i}"),
        )
        for i in range(260)
    ]
    store.put(datasets)
    store.build_index(embedding_model=model)

    tbl = search_table(store)
    assert tbl is not None
    index_columns = {tuple(ix.columns) for ix in tbl.list_indices()}
    assert ("embedding",) in index_columns, f"No ANN index on 'embedding' column. Indices present: {index_columns}"


# ---------------------------------------------------------------------------
# Ranked projections
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_scalars_identifier(tmp_path):
    """search().scalars('identifier') returns ranked IDs without hydration."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="labour-ds",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment statistics"),
        )
    )
    store.put(
        sc.OpenDataset(
            identifier="price-ds",
            title=I18n(en="Consumer Price Index"),
            description=I18n(en="Inflation data"),
        )
    )
    store.build_index()

    ids = store.query(sc.OpenDataset).search("labour employment").scalars("identifier").values()
    assert isinstance(ids, list)
    assert len(ids) > 0
    assert all(isinstance(i, str) for i in ids)
    assert "labour-ds" in ids


@pytest.mark.unit
def test_search_scalars_title(tmp_path):
    """search().scalars('title') returns ranked titles preserving search order."""
    store = open_scratch(tmp_path / "catalog")
    store.put(sc.OpenDataset(identifier="ds-a", title=I18n(en="Alpha Dataset"), description=I18n(en="First alpha")))
    store.put(sc.OpenDataset(identifier="ds-b", title=I18n(en="Beta Dataset"), description=I18n(en="Second beta")))
    store.build_index()

    titles = store.query(sc.OpenDataset).search("alpha").scalars("title", lang="en").values()
    assert isinstance(titles, list)
    assert len(titles) > 0
    assert "Alpha Dataset" in titles


@pytest.mark.unit
def test_search_select_projection(tmp_path):
    """search().select() returns ranked dicts without full hydration."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="sel-1",
            title=I18n(en="Selection Test"),
            description=I18n(en="For select projection"),
        )
    )
    store.put(
        sc.OpenDataset(
            identifier="sel-2",
            title=I18n(en="Other Dataset"),
            description=I18n(en="Not matching"),
        )
    )
    store.build_index()

    rows = store.query(sc.OpenDataset).search("selection").select("identifier", "title", lang="en").rows()
    assert isinstance(rows, list)
    assert len(rows) > 0
    assert all(isinstance(r, dict) for r in rows)
    assert "identifier" in rows[0]
    assert "title" in rows[0]


@pytest.mark.unit
def test_search_exists(tmp_path):
    """search().exists() returns True/False without hydrating objects."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="exists-ds",
            title=I18n(en="Existence Check"),
            description=I18n(en="A dataset about existence"),
        )
    )
    store.build_index()

    assert store.query(sc.OpenDataset).search("existence").exists() is True
    assert store.query(sc.OpenDataset).search("xyzzy_nonexistent_term_42").exists() is False


@pytest.mark.unit
def test_search_scalars_first(tmp_path):
    """search().scalars().first() returns a single ranked value."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="first-ds",
            title=I18n(en="First Result"),
            description=I18n(en="The first result"),
        )
    )
    store.build_index()

    val = store.query(sc.OpenDataset).search("first result").scalars("identifier").value()
    assert val == "first-ds"


# ---------------------------------------------------------------------------
# Semantic search scores
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_semantic_search_populates_scores(store_with_model):
    """semantic_search() should populate the scores dict."""
    results = store_with_model.datasets.find(mode="vector", query="labour employment", lang="en", limit=5)
    assert isinstance(results.scores, dict)
    assert len(results.scores) > 0
    # Every returned item should have a score
    for ds in results.items:
        assert ds.payload.identifier in results.scores
        assert isinstance(results.scores[ds.payload.identifier], float)


@pytest.mark.unit
def test_hybrid_search_populates_scores(store_with_model):
    """hybrid_search() should populate the scores dict."""
    results = store_with_model.datasets.find(mode="hybrid", query="price inflation", lang="en", limit=5)
    assert isinstance(results.scores, dict)
    # hybrid_search already populated scores before this fix — verify it still works
    for ds in results.items:
        assert ds.payload.identifier in results.scores


# ---------------------------------------------------------------------------
# BM25 query strategy — AND/OR operator and stopword handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_and_operator_default(tmp_path):
    """Default AND operator requires all terms to match."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="both-terms",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment statistics for Canada"),
        )
    )
    store.put(
        sc.OpenDataset(
            identifier="one-term",
            title=I18n(en="Consumer Price Index"),
            description=I18n(en="Monthly statistics for prices"),
        )
    )
    store.build_index()

    # "labour statistics" with AND: only "both-terms" has both words
    ids = store.query(sc.BaseDataset).search("labour statistics").scalars("identifier").values()
    assert "both-terms" in ids


@pytest.mark.unit
def test_search_or_operator(tmp_path):
    """OR operator matches documents with any term."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="labour-only",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    store.put(
        sc.OpenDataset(
            identifier="price-only",
            title=I18n(en="Consumer Price Index"),
            description=I18n(en="Inflation measures"),
        )
    )
    store.build_index()

    # "labour price" with OR: both should match (each has one term)
    ids = store.query(sc.BaseDataset).search("labour price", operator="or").scalars("identifier").values()
    assert "labour-only" in ids
    assert "price-only" in ids


@pytest.mark.unit
def test_search_english_stopwords_stripped(tmp_path):
    """English stopwords like 'in', 'the', 'and' are stripped before AND join."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="health-safety",
            title=I18n(en="Health and Safety Report"),
            description=I18n(en="Workplace health and safety guidelines"),
        )
    )
    store.build_index()

    # "health and safety" → strips "and" → "health AND safety"
    ids = store.query(sc.BaseDataset).search("health and safety").scalars("identifier").values()
    assert "health-safety" in ids


@pytest.mark.unit
def test_search_french_stopwords_stripped():
    """French stopwords like 'de', 'la', 'les' are stripped by _prepare_query."""

    # "estimations de la population" → strips "de", "la" → AND join
    result = _prepare_query("estimations de la population", "and")
    assert result == "estimations AND population"

    # Single non-stopword term after stripping returns original
    result = _prepare_query("de la population", "and")
    assert result == "de la population"  # only 1 term left → passthrough


@pytest.mark.unit
def test_prepare_query_english_stopwords():
    """English stopwords are stripped by _prepare_query."""

    assert _prepare_query("health and safety", "and") == "health AND safety"
    assert _prepare_query("women in tech", "and") == "women AND tech"
    assert _prepare_query("women in tech", "or") == "women tech"


@pytest.mark.unit
def test_search_single_term_unchanged(tmp_path):
    """Single-term queries pass through without modification."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="labour-ds",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    store.build_index()

    ids = store.query(sc.BaseDataset).search("labour").scalars("identifier").values()
    assert "labour-ds" in ids


@pytest.mark.unit
def test_standalone_search_operator(tmp_path):
    """Catalog.search() accepts operator param."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-a",
            title=I18n(en="Alpha Beta Dataset"),
            description=I18n(en="Contains both alpha and beta"),
        )
    )
    store.put(
        sc.OpenDataset(
            identifier="ds-b",
            title=I18n(en="Gamma Dataset"),
            description=I18n(en="Contains only gamma"),
        )
    )
    store.build_index()

    results = store.datasets.find("alpha beta", operator="and")
    ids = [d.payload.identifier for d in results.items]
    assert "ds-a" in ids


# ---------------------------------------------------------------------------
# entity_type filter + dedup in LanceIndex
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dedupe_by_id_keep_min_picks_lowest_distance():
    hits = [("a", 0.5), ("b", 0.4), ("a", 0.1), ("c", 0.9), ("b", 0.6)]
    out = _dedupe_by_id(iter(hits), keep="min")
    # Order: first occurrence; values: best (min) per id.
    assert out == [("a", 0.1), ("b", 0.4), ("c", 0.9)]


@pytest.mark.unit
def test_dedupe_by_id_keep_max_picks_highest_score():
    hits = [("a", 0.1), ("b", 0.4), ("a", 0.9), ("c", 0.2)]
    out = _dedupe_by_id(iter(hits), keep="max")
    assert out == [("a", 0.9), ("b", 0.4), ("c", 0.2)]


@pytest.mark.unit
def test_entity_type_clause_none_returns_empty():
    assert _entity_type_clause(None) == ""


@pytest.mark.unit
def test_entity_type_clause_single_string():
    assert _entity_type_clause("open") == "entity_type = 'open'"


@pytest.mark.unit
def test_entity_type_clause_sequence():
    assert _entity_type_clause(("open", "aggregate")) == "entity_type IN ('open', 'aggregate')"


def _seed_lance_with_mixed_rows(path) -> LanceIndex:
    """Build a LanceIndex with both dataset and code rows for the same id."""
    idx = LanceIndex(path, edition_id=_EDITION)
    rows: list[SearchRow] = [
        {"id": "ds-1", "entity_type": "open", "lang": "en", "text": "labour force survey", "embedding": [0.1] * 8},
        {"id": "ds-1", "entity_type": "code", "lang": "en", "text": "Geography: Canada", "embedding": [0.1] * 8},
        {"id": "ds-1", "entity_type": "code", "lang": "en", "text": "Geography: Ontario", "embedding": [0.1] * 8},
        {"id": "ds-2", "entity_type": "open", "lang": "en", "text": "consumer price index", "embedding": [0.2] * 8},
        {"id": "ds-2", "entity_type": "code", "lang": "en", "text": "Frequency: Monthly", "embedding": [0.2] * 8},
    ]
    idx.add_bulk(rows)
    idx.build_index()
    return idx


@pytest.mark.unit
def test_lance_search_default_returns_dedup_unique_ids(tmp_path):
    """Without entity_type filter, results are deduplicated by id."""
    idx = _seed_lance_with_mixed_rows(tmp_path / "lance")
    hits = idx.search([0.1] * 8, lang="en", limit=10)
    ids = [h[0] for h in hits]
    assert len(ids) == len(set(ids)), f"duplicates returned: {ids}"


@pytest.mark.unit
def test_lance_search_entity_type_filters_to_dataset_rows(tmp_path):
    """Passing dataset entity_types excludes ``code`` rows entirely."""
    idx = _seed_lance_with_mixed_rows(tmp_path / "lance")
    hits = idx.search([0.1] * 8, lang="en", limit=10, entity_type=("open", "aggregate"))
    ids = [h[0] for h in hits]
    assert sorted(ids) == ["ds-1", "ds-2"]


@pytest.mark.unit
def test_lance_search_entity_type_code_returns_only_codes(tmp_path):
    """SDMX/RAG retrieval over codes remains available via entity_type='code'."""
    idx = _seed_lance_with_mixed_rows(tmp_path / "lance")
    hits = idx.search([0.1] * 8, lang="en", limit=10, entity_type="code")
    # Returned ids all came from code rows; we expect 2 distinct ids
    # (ds-1 had 2 code rows but dedup collapses them).
    assert sorted(h[0] for h in hits) == ["ds-1", "ds-2"]


@pytest.mark.unit
def test_lance_bm25_search_excludes_code_rows(tmp_path):
    """BM25 entity_type filter excludes code rows from dataset rankings."""
    idx = _seed_lance_with_mixed_rows(tmp_path / "lance")
    hits = idx.bm25_search("Canada", lang="en", limit=10, entity_type=("open",))
    # "Canada" appears only in a code row's text. With entity_type filtered
    # to "open", the prefilter excludes that row entirely → no hits.
    assert hits == []


@pytest.mark.unit
def test_semantic_search_excludes_code_rows_by_default(tmp_path, model):
    """semantic_search must not return ``code`` rows mixed in with datasets."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    store.build_index()
    # Inject a synthetic code row directly into the lance table.
    code_row: SearchRow = {
        "id": "ds-1",
        "entity_type": "code",
        "lang": "en",
        "text": "Geography: Ontario",
        "embedding": list(model.encode("Geography: Ontario")),
    }
    require_index(store).add([code_row])
    # Vector search must dedup; only a single ds-1 row may come back.
    results = store.datasets.find(mode="vector", query="labour", lang="en", limit=10)
    ids = [d.payload.identifier for d in results.items]
    assert ids.count("ds-1") <= 1


# ---------------------------------------------------------------------------
# Asymmetric-model document/query prefixes
# ---------------------------------------------------------------------------


class _RecordingModel(_StubModel):
    """Stub model that records every text passed to ``encode``."""

    def __init__(self) -> None:
        self.encoded: list[str] = []

    @override
    def encode(self, text):
        if isinstance(text, list):
            self.encoded.extend(text)
        else:
            self.encoded.append(text)
        return super().encode(text)


@pytest.mark.unit
def test_build_index_applies_document_prefix(tmp_path):
    """Document texts are prefixed before being passed to encode()."""
    rec = _RecordingModel()
    store = open_scratch(tmp_path / "catalog", embedding_model=rec, document_prefix="passage: ")
    store.put(
        sc.OpenDataset(
            identifier="prefix-doc",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    store.build_index(model_name="recording-model")
    assert rec.encoded, "encode() was never called"
    assert all(t.startswith("passage: ") for t in rec.encoded)


@pytest.mark.unit
def test_semantic_search_applies_query_prefix(tmp_path):
    """Query string is prefixed with query_prefix before encoding."""
    rec = _RecordingModel()
    store = open_scratch(
        tmp_path / "catalog",
        embedding_model=rec,
        document_prefix="passage: ",
        query_prefix="query: ",
    )
    store.put(
        sc.OpenDataset(
            identifier="qp-1",
            title=I18n(en="Test"),
            description=I18n(en="Test description"),
        )
    )
    store.build_index(model_name="recording-model")
    rec.encoded.clear()
    store.datasets.find(mode="vector", query="labour", lang="en", limit=3)
    assert rec.encoded == ["query: labour"]


@pytest.mark.unit
def test_hybrid_search_applies_query_prefix(tmp_path):
    """Hybrid path also prefixes the query before encoding."""
    rec = _RecordingModel()
    store = open_scratch(
        tmp_path / "catalog",
        embedding_model=rec,
        query_prefix="query: ",
    )
    store.put(
        sc.OpenDataset(
            identifier="qp-2",
            title=I18n(en="Test"),
            description=I18n(en="Test description"),
        )
    )
    store.build_index(model_name="recording-model")
    rec.encoded.clear()
    store.datasets.find(mode="hybrid", query="inflation", lang="en", limit=3)
    assert rec.encoded == ["query: inflation"]


@pytest.mark.unit
def test_default_no_prefix_unchanged(tmp_path):
    """Without prefixes set, encode() receives raw text — no behavior change."""
    rec = _RecordingModel()
    store = open_scratch(tmp_path / "catalog", embedding_model=rec)
    store.put(
        sc.OpenDataset(
            identifier="np-1",
            title=I18n(en="Hello World"),
            description=I18n(en="A description"),
        )
    )
    store.build_index()
    assert rec.encoded
    assert all(not t.startswith("passage: ") for t in rec.encoded)
    rec.encoded.clear()
    store.datasets.find(mode="vector", query="hello", lang="en", limit=3)
    assert rec.encoded == ["hello"]


@pytest.mark.unit
def test_build_index_can_set_prefixes(tmp_path):
    """build_index() accepts prefix overrides that persist on the store."""
    rec = _RecordingModel()
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="bi-1",
            title=I18n(en="Test"),
            description=I18n(en="A test"),
        )
    )
    store.build_index(
        embedding_model=rec, document_prefix="passage: ", query_prefix="query: ", model_name="recording-model"
    )
    assert all(t.startswith("passage: ") for t in rec.encoded)
    rec.encoded.clear()
    store.datasets.find(mode="vector", query="anything", lang="en", limit=3)
    assert rec.encoded == ["query: anything"]


# ---------------------------------------------------------------------------
# Graceful BM25 degradation when LanceDB index missing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_querybuilder_search_returns_empty_when_lance_missing(tmp_path):
    """QueryBuilder.search must not raise when the lance index is absent."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    # Simulate "lancedb not installed" / "index never built".
    store._store._lance = None
    result = store.query(sc.OpenDataset).search("labour").execute()
    assert isinstance(result, sc.SearchResult)
    assert result.items == []
    assert result.total == 0
    assert result.scores == {}


@pytest.mark.unit
def test_catalogstore_search_returns_empty_when_lance_missing(tmp_path):
    """Catalog.search() must not raise when the lance index is absent."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Labour Force Survey"),
            description=I18n(en="Employment data"),
        )
    )
    store._store._lance = None
    result = store.datasets.find("labour")
    assert isinstance(result, sc.SearchResult)
    assert result.items == []
    assert result.total == 0


@pytest.mark.unit
def test_querybuilder_hybrid_returns_empty_when_lance_missing(tmp_path, model):
    """QueryBuilder.hybrid_search degrades gracefully without an index."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="ds-1",
            title=I18n(en="Test Dataset"),
            description=I18n(en="A test"),
        )
    )
    store._store._lance = None
    result = store.query(sc.OpenDataset).hybrid_search("test").execute()
    assert result.items == []
    assert result.total == 0


# ---------------------------------------------------------------------------
# Fuzziness as int on QueryBuilder/Store search
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_fuzzy_one_preserves_exact_match(tmp_path):
    """fuzzy>0 must not silently drop exact matches.

    Tantivy's fuzzy automaton drops the exact term from results when
    the query length is ≥ 8 chars; statcatlib wraps fuzzy + exact in a
    BooleanQuery to defend against that quirk. Without this guard,
    ``search("inflation", fuzzy=1)`` returns an empty list even when
    "inflation" is in the index.
    """
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-exact",
            title=I18n(en="Inflation Index"),
            description=I18n(en="Consumer prices"),
        )
    )
    store.build_index()
    for fz in (1, 2):
        ids = store.query(sc.OpenDataset).search("inflation", fuzzy=fz).scalars("identifier").values()
        assert "ds-exact" in ids, f"exact match dropped at fuzzy={fz}"


@pytest.mark.unit
def test_search_codes_fuzzy_preserves_exact(tmp_path):
    """The exact-match fix also covers the search_codes BM25 path."""
    from sdmxlib.model.codelist import Code, Codelist
    from sdmxlib.model.dataflow import Dataflow
    from sdmxlib.model.datastructure import DataStructure, Dimension
    from sdmxlib.model.in_memory_registry import InMemoryRegistry
    from sdmxlib.model.istring import InternationalString

    from statcatlib.sources.sdmx import ingest_sdmx

    cl = Codelist(
        id="CL_PROV",
        maintainer="TEST",
        name=InternationalString(en="Province"),
        codes=[Code(id="ON", name=InternationalString(en="Saskatchewan"))],
    )
    dsd = DataStructure(
        id="DSD_PROV",
        maintainer="TEST",
        name=InternationalString(en="DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl)],
    )
    flow = Dataflow(
        id="DF_PROV",
        maintainer="TEST",
        name=InternationalString(en="Provincial flow"),
        structure=dsd,
    )
    reg = InMemoryRegistry()
    for art in (cl, dsd, flow):
        reg.add(art)

    store = open_scratch(tmp_path / "catalog")
    ingest_sdmx(store, reg)
    store.build_index(include_codes=True)

    # "Saskatchewan" is 12 chars — comfortably inside the Tantivy
    # quirk's exclusion window. With the BooleanQuery fix, an exact
    # query at fuzzy=1 must still surface it.
    result = store.codes.find("Saskatchewan", lang="en", fuzzy=1)
    assert any(h.payload.code_label == "Saskatchewan" for h in result.items)


@pytest.mark.unit
def test_search_fuzzy_zero_requires_exact_match(tmp_path):
    """fuzzy=0 (default) requires exact term matches — typos do not match."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-exact",
            title=I18n(en="Inflation Index"),
            description=I18n(en="Consumer prices"),
        )
    )
    store.build_index()
    ids = store.query(sc.OpenDataset).search("inflaton", fuzzy=0).scalars("identifier").values()
    assert "ds-exact" not in ids


@pytest.mark.unit
def test_search_fuzzy_two_catches_typos(tmp_path):
    """fuzzy=2 catches typos that fuzzy=0 misses."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-fuzzy",
            title=I18n(en="Inflation Index"),
            description=I18n(en="Consumer prices"),
        )
    )
    store.build_index()
    # 1-character typo: "inflaton" → "inflation"
    ids = store.query(sc.OpenDataset).search("inflaton", fuzzy=2).scalars("identifier").values()
    assert "ds-fuzzy" in ids


@pytest.mark.unit
def test_catalogstore_search_fuzzy_int(tmp_path):
    """Catalog.search() accepts integer fuzzy values, forwarded to BM25."""
    store = open_scratch(tmp_path / "catalog")
    store.put(
        sc.OpenDataset(
            identifier="ds-cs-fuzzy",
            title=I18n(en="Inflation Index"),
            description=I18n(en="Consumer prices"),
        )
    )
    store.build_index()
    result = store.datasets.find("inflaton", fuzzy=2)
    assert any(d.payload.identifier == "ds-cs-fuzzy" for d in result.items)


# ---------------------------------------------------------------------------
# Embedding metadata persistence (embeddings.json sidecar)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_index_writes_embeddings_meta(tmp_path, model):
    """build_index(model_name=...) writes embeddings.json beside the index."""
    import json as _json

    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="meta-1",
            title=I18n(en="Test"),
            description=I18n(en="Test description"),
        )
    )
    store.build_index(
        document_prefix="passage: ",
        query_prefix="query: ",
        model_name="test/stub-model",
    )
    meta_path = _search_dir(store) / "embeddings.json"
    assert meta_path.is_file()
    payload = _json.loads(meta_path.read_text())
    assert payload["model"] == "test/stub-model"
    assert payload["document_prefix"] == "passage: "
    assert payload["query_prefix"] == "query: "
    assert payload["version"] == 4
    # v3-only enrichment fields:
    assert payload["embedding_dim"] is not None
    assert payload["normalize"] is True
    assert payload["include_codes"] is True
    assert payload["embed_codes"] is False
    assert payload["docs_indexed"] == 1
    assert payload["built_by"].startswith("statcatlib==")
    assert payload["built_at"] is not None


@pytest.mark.unit
def test_build_index_without_model_name_skips_meta(tmp_path, model):
    """No model_name means no sidecar written — backward compat."""
    store = open_scratch(tmp_path / "catalog", embedding_model=model)
    store.put(
        sc.OpenDataset(
            identifier="nm-1",
            title=I18n(en="Test"),
            description=I18n(en="Test description"),
        )
    )
    store.build_index()
    assert not (_search_dir(store) / "embeddings.json").exists()


@pytest.mark.unit
def test_open_restores_meta(tmp_path, model):
    """A view of the sealed edition reads embeddings.json and restores prefixes + name."""
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="r-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(
            document_prefix="passage: ",
            query_prefix="query: ",
            model_name="intfloat/multilingual-e5-base",
        )
        reopened = seal_and_view(store)

    assert reopened.embedding_model_name == "intfloat/multilingual-e5-base"
    assert reopened.document_prefix == "passage: "
    assert reopened.query_prefix == "query: "


@pytest.mark.unit
def test_open_without_meta_uses_defaults(tmp_path, model):
    """An edition indexed without a model_name has no sidecar, and its view opens with the defaults."""
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="legacy-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index()  # no model_name -> no sidecar
        reopened = seal_and_view(store)

    assert reopened.embedding_model_name is None
    assert reopened.document_prefix == ""
    assert reopened.query_prefix == ""


@pytest.mark.unit
def test_embedding_metadata_round_trip(tmp_path, model):
    """The full EmbeddingMetadata survives build → seal → open."""
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="rt-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(
            document_prefix="passage: ",
            query_prefix="query: ",
            model_name="intfloat/multilingual-e5-base",
        )
        live = store.embedding_metadata
        assert live is not None
        assert live.model == "intfloat/multilingual-e5-base"
        assert live.docs_indexed == 1
        assert live.embedding_dim is not None
        assert live.built_at is not None
        reopened = seal_and_view(store)

    meta = reopened.embedding_metadata
    assert meta is not None
    assert meta.model == "intfloat/multilingual-e5-base"
    assert meta.embedding_dim == live.embedding_dim
    assert meta.docs_indexed == 1
    assert meta.include_codes is True
    assert meta.embed_codes is False
    assert meta.normalize is True
    assert meta.built_by is not None
    assert meta.built_by.startswith("statcatlib==")


@pytest.mark.unit
def test_open_reads_v2_sidecar(tmp_path):
    """A v2 sidecar (legacy schema) opens cleanly; missing fields are None.

    The sidecar is written into a closed working copy's ``search/`` directory,
    and the resumed working copy reads it as it opens.
    """
    import json as _json

    catalog = tmp_path / "catalog"
    # Start a working copy without writing the sidecar via build_index.
    with scratch_store(catalog) as store:
        search_dir = _search_dir(store)
    search_dir.mkdir(parents=True, exist_ok=True)
    (search_dir / "embeddings.json").write_text(
        _json.dumps(
            {
                "version": 2,
                "model": "legacy/model",
                "document_prefix": "passage: ",
                "query_prefix": "query: ",
            }
        )
    )

    with scratch_store(catalog, resume=True) as reopened:
        meta = reopened.embedding_metadata
        assert meta is not None
        assert meta.model == "legacy/model"
        assert meta.embedding_dim is None
        assert meta.built_at is None
        assert meta.docs_indexed is None
        # legacy flat property still works:
        assert reopened.embedding_model_name == "legacy/model"
        assert reopened.document_prefix == "passage: "
        assert reopened.query_prefix == "query: "


@pytest.mark.unit
def test_check_index_compatible_matches(tmp_path, model):
    """check_index_compatible() is a no-op when arguments match the sidecar."""
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="cic-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(model_name="test/stub-model")
        meta = store.embedding_metadata
        assert meta is not None
        dim = meta.embedding_dim
        reopened = seal_and_view(store)

    # Either argument alone should pass; both together should pass.
    reopened.check_index_compatible(model_name="test/stub-model")
    if dim is not None:
        reopened.check_index_compatible(embedding_dim=dim)
        reopened.check_index_compatible(model_name="test/stub-model", embedding_dim=dim)


@pytest.mark.unit
def test_check_index_compatible_model_mismatch(tmp_path, model):
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="mm-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(model_name="builder/model")
        reopened = seal_and_view(store)

    with pytest.raises(sc.IncompatibleIndexError, match="model"):
        reopened.check_index_compatible(model_name="different/model")


@pytest.mark.unit
def test_check_index_compatible_dim_mismatch(tmp_path, model):
    with scratch_store(tmp_path / "catalog", embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="dim-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(model_name="builder/model")
        meta = store.embedding_metadata
        assert meta is not None
        actual_dim = meta.embedding_dim
        reopened = seal_and_view(store)

    if actual_dim is None:
        pytest.skip("test encoder did not report a dim — cannot exercise dim mismatch")

    with pytest.raises(sc.IncompatibleIndexError, match="-d"):
        reopened.check_index_compatible(embedding_dim=actual_dim + 1)


@pytest.mark.unit
def test_check_index_compatible_no_sidecar_is_noop(tmp_path):
    """Without a sidecar there is nothing to be incompatible with."""
    with scratch_store(tmp_path / "catalog") as store:
        # No build_index, no sidecar.
        store.check_index_compatible(model_name="anything/at/all")
        store.check_index_compatible(embedding_dim=999)


@pytest.mark.unit
def test_open_tolerates_malformed_meta(tmp_path, model):
    """A corrupt embeddings.json is treated as missing, not as an error.

    The working copy is closed, its sidecar overwritten, and the working copy
    resumed: it reads the sidecar as it opens.
    """
    catalog = tmp_path / "catalog"
    with scratch_store(catalog, embedding_model=model) as store:
        store.put(
            sc.OpenDataset(
                identifier="bad-1",
                title=I18n(en="Hello"),
                description=I18n(en="World"),
            )
        )
        store.build_index(model_name="good/model")
        search_dir = _search_dir(store)
    # Stomp on the file with garbage.
    (search_dir / "embeddings.json").write_text("not json {")

    with scratch_store(catalog, resume=True) as reopened:
        assert reopened.embedding_model_name is None
        assert reopened.document_prefix == ""
        assert reopened.query_prefix == ""


class TestExplicitSchema:
    """The Lance table is created under a declared schema, not an inferred one.

    Declaring it is what keeps the per-language tables from
    drifting apart, and it fixes one live defect on the way: a batch in
    which *every* row is unembedded while a model is configured used to
    infer a null-typed embedding column, after which the ANN index could
    never build.
    """

    def test_all_unembedded_rows_still_get_a_typed_column(self, tmp_path):
        """The real hazard: ``embed_codes=False`` (or every codelist
        marked fts_only) leaves no populated vector to infer from. The
        model's width has to pin the type instead."""
        import pyarrow as pa

        from statcatlib.store.lance import LanceIndex

        rows: list[SearchRow] = [
            {"id": "a", "entity_type": "code", "lang": "en", "text": "x", "embedding": None},
            {"id": "b", "entity_type": "code", "lang": "en", "text": "y", "embedding": None},
        ]
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(rows, embedding_dim=8)
        field = require_table(idx).schema.field("embedding")
        assert field.type.value_type == pa.float32()
        assert field.type.list_size == 8

    def test_row_order_is_not_load_bearing(self, tmp_path):
        """Leading with unembedded rows is fine — PyArrow scans the whole
        batch. Pinned so the explicit schema keeps that property."""
        import pyarrow as pa

        from statcatlib.store.lance import LanceIndex

        rows: list[SearchRow] = [
            {"id": "a", "entity_type": "code", "lang": "en", "text": "no vector", "embedding": None},
            {
                "id": "b",
                "entity_type": "open",
                "lang": "en",
                "text": "has vector",
                "embedding": [0.1, 0.2, 0.3, 0.4],
            },
        ]
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(rows)
        field = require_table(idx).schema.field("embedding")
        assert field.type.value_type == pa.float32()
        assert field.type.list_size == 4

    def test_no_embedding_column_without_a_model(self, tmp_path):
        """Rows with no embedding at all keep the pre-existing shape."""
        from statcatlib.store.lance import LanceIndex

        rows: list[SearchRow] = [{"id": "a", "entity_type": "open", "lang": "en", "text": "plain"}]
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(rows)
        assert "embedding" not in require_table(idx).schema.names

    def test_rows_are_padded_to_the_union_schema(self, tmp_path):
        """A row missing an extension column gets NULL, not a failure."""
        from statcatlib.store.lance import LanceIndex

        rows: list[SearchRow] = [{"id": "a", "entity_type": "open", "lang": "en", "text": "sparse row"}]
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(rows)
        names = set(require_table(idx).schema.names)
        assert {"code_urn", "concept_urn", "set_urn", "depth"} <= names

    def test_append_reuses_the_existing_table_schema(self, tmp_path):
        """Appending an all-``None``-embedding batch must not re-derive a
        schema without the embedding column."""
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "x", "embedding": [1.0, 0.0]}])
        idx.add([{"id": "b", "entity_type": "code", "lang": "en", "text": "y", "embedding": None}])
        assert require_table(idx).count_rows() == 2
        assert "embedding" in require_table(idx).schema.names


class TestPerLanguageStemming:
    """Each language's corpus is stemmed with its own Snowball rules.

    Before per-language tables there was one FTS index over one shared
    ``text`` column, built with LanceDB's defaults — ``language='English'``,
    ``stem=True``. French text was therefore stemmed with English rules,
    and French morphology did not match.
    """

    @staticmethod
    def _bilingual_store(tmp_path, model=None):
        store = open_scratch(tmp_path / "bilingual", embedding_model=model)
        store.put(
            sc.OpenDataset(
                identifier="emploi",
                title=I18n(
                    en="Skilled workers in Canadian businesses",
                    fr="Les entreprises canadiennes emploient des travailleurs qualifiés",
                ),
                description=I18n(en="Employment statistics", fr="Statistiques sur les logements occupés"),
            )
        )
        store.build_index()
        return store

    @pytest.mark.parametrize(
        ("query", "inflected"),
        [
            ("canadien", "canadiennes"),
            ("qualifier", "qualifiés"),
            ("occuper", "occupés"),
        ],
    )
    def test_french_morphology_matches(self, tmp_path, query, inflected):
        """A French query stems onto its inflected form in the text.

        Each of these returned nothing under the English stemmer.
        """
        store = self._bilingual_store(tmp_path)
        res = store.datasets.find(mode="bm25", query=query, lang="fr", limit=5)
        assert any(d.payload.identifier == "emploi" for d in res.items), (
            f"French query {query!r} should match {inflected!r} under the French stemmer"
        )

    def test_english_rows_are_unaffected(self, tmp_path):
        store = self._bilingual_store(tmp_path)
        res = store.datasets.find(mode="bm25", query="worker", lang="en", limit=5)
        assert any(d.payload.identifier == "emploi" for d in res.items)

    def test_languages_live_in_separate_tables(self, tmp_path):
        store = self._bilingual_store(tmp_path)
        assert require_index(store).languages == ["en", "fr"]
        en = search_table(store)
        fr = search_table(store, "fr")
        assert en is not None
        assert fr is not None
        # Each table holds exactly one language — that membership is what
        # gives each corpus its own document frequencies.
        assert {r["lang"] for r in en.search().limit(50).to_list()} == {"en"}
        assert {r["lang"] for r in fr.search().limit(50).to_list()} == {"fr"}

    def test_unknown_language_is_indexed_unstemmed_not_mis_stemmed(self, tmp_path):
        """A language with no Snowball stemmer must not borrow English's."""
        from statcatlib.store.lance import _stemmer_for

        assert _stemmer_for("fr") == "French"
        assert _stemmer_for("fr-CA") == "French"
        assert _stemmer_for("ja") is None
        assert _stemmer_for("zh") is None

    def test_rebuild_drops_a_removed_language(self, tmp_path):
        """A language that disappears from the catalog must not leave a
        stale table behind still answering queries."""
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(
            [
                {"id": "a", "entity_type": "open", "lang": "en", "text": "hello"},
                {"id": "a", "entity_type": "open", "lang": "fr", "text": "bonjour"},
            ]
        )
        assert idx.languages == ["en", "fr"]
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        assert idx.languages == ["en"]
        # `table_for`, not `require_table`: the dropped language having no
        # table is what this asserts, so the narrowing helper is wrong here.
        assert idx.table_for("fr") is None


class TestPerLanguageStopwords:
    """Stopwords are stripped in the query's own language.

    Applied as one merged set, a French stopword that is also an English
    content word disappeared from English queries.
    """

    def test_french_stopword_survives_an_english_query(self):
        from statcatlib.store.lance import _strip_stopwords

        # "son" is a French stopword and an English noun.
        assert _strip_stopwords("son preference", "en") == "son preference"
        assert _strip_stopwords("son preference", "fr") == "preference"

    def test_english_stopword_survives_a_french_query(self):
        from statcatlib.store.lance import _strip_stopwords

        # "or" is an English stopword; in French it is a noun (gold).
        assert _strip_stopwords("or et argent", "fr") == "or argent"
        assert _strip_stopwords("gold or silver", "en") == "gold silver"

    def test_unknown_language_keeps_the_merged_behaviour(self):
        from statcatlib.store.lance import _strip_stopwords

        assert _strip_stopwords("son preference", "de") == "preference"
        assert _strip_stopwords("son preference") == "preference"


class TestAtomicRebuild:
    """A rebuild over the previous edition's tables swaps in whole or not at all.

    Per-language tables turned one drop-and-create into N, so a failure
    partway through could leave some languages rebuilt and others stale.
    A new edition writes its tables beside the ones it carried from the
    previous edition (``search_<lang>__<edition id without hyphens>``) and
    commits them by replacing the manifest in one atomic move, so until that
    lands every query still reads the previous edition's tables. A rebuild
    within one edition replaces that edition's tables.
    """

    _EDITION_HEX = _EDITION.replace("-", "")
    _NEXT_EDITION_HEX = _NEXT_EDITION.replace("-", "")

    @staticmethod
    def _rows(text_en: str, text_fr: str) -> list[SearchRow]:
        return [
            {"id": "a", "entity_type": "open", "lang": "en", "text": text_en},
            {"id": "a", "entity_type": "open", "lang": "fr", "text": text_fr},
        ]

    def test_successful_rebuild_replaces_every_language(self, tmp_path):
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(self._rows("first", "premier"))
        idx.add_bulk(self._rows("second", "deuxieme"))
        assert [i for i, _ in idx.bm25_search("second", lang="en")] == ["a"]
        assert [i for i, _ in idx.bm25_search("deuxieme", lang="fr")] == ["a"]
        # The superseded content is gone, not merged in alongside.
        assert idx.bm25_search("first", lang="en") == []

    def test_next_edition_rebuild_drops_the_carried_tables(self, tmp_path):
        """The next edition's committed rebuild leaves only its own tables on disk."""
        import lancedb

        from statcatlib.store.lance import LanceIndex

        LanceIndex(tmp_path / "search", edition_id=_EDITION).add_bulk(self._rows("first", "premier"))
        idx = LanceIndex(tmp_path / "search", edition_id=_NEXT_EDITION)
        idx.add_bulk(self._rows("second", "deuxieme"))
        assert set(lancedb.connect(str(tmp_path / "search")).list_tables().tables) == {
            f"search_en__{self._NEXT_EDITION_HEX}",
            f"search_fr__{self._NEXT_EDITION_HEX}",
        }
        assert idx.bm25_search("first", lang="en") == []

    def test_failed_rebuild_leaves_the_previous_index_serving(self, tmp_path, monkeypatch):
        """The failure mode this design exists to prevent: the second
        language blows up mid-rebuild, and the first must not already
        have been swapped."""
        from statcatlib.store import lance as lance_mod
        from statcatlib.store.lance import LanceIndex

        LanceIndex(tmp_path / "search", edition_id=_EDITION).add_bulk(self._rows("first", "premier"))
        # The next edition rebuilds over the tables it carried from the first.
        idx = LanceIndex(tmp_path / "search", edition_id=_NEXT_EDITION)

        real_create = lance_mod._LanguageIndex.create
        calls = {"n": 0}

        def exploding_create(self, rows, embedding_dim):
            calls["n"] += 1
            if calls["n"] == 2:  # second language staged
                msg = "disk full"
                raise RuntimeError(msg)
            return real_create(self, rows, embedding_dim)

        monkeypatch.setattr(lance_mod._LanguageIndex, "create", exploding_create)
        with pytest.raises(RuntimeError, match="disk full"):
            idx.add_bulk(self._rows("second", "deuxieme"))

        # Both languages still answer with the pre-rebuild content.
        reopened = LanceIndex(tmp_path / "search")
        assert sorted(reopened.languages) == ["en", "fr"]
        assert [i for i, _ in reopened.bm25_search("first", lang="en")] == ["a"]
        assert [i for i, _ in reopened.bm25_search("premier", lang="fr")] == ["a"]
        assert reopened.bm25_search("second", lang="en") == []
        # The failed rebuild dropped the table it had built.
        assert reopened.orphan_tables() == []

    def _add_orphan(self, tmp_path) -> str:
        """Write a French table for the next edition and name it in no manifest, as a crashed rebuild leaves it."""
        import lancedb

        from statcatlib.store.lance import _to_arrow

        name = f"search_fr__{self._NEXT_EDITION_HEX}"
        lancedb.connect(str(tmp_path / "search")).create_table(
            name,
            data=_to_arrow([{"id": "b", "entity_type": "open", "lang": "fr", "text": "bonjour"}]),
        )
        return name

    def test_uncommitted_tables_are_not_adopted(self, tmp_path):
        """Tables from a rebuild that never wrote its manifest must not
        become live just by existing on disk."""
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        self._add_orphan(tmp_path)

        reopened = LanceIndex(tmp_path / "search")
        assert reopened.languages == ["en"]
        assert reopened.table_for("fr") is None

    def test_discard_orphans_sweeps_uncommitted_tables(self, tmp_path):
        import lancedb

        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        orphan = self._add_orphan(tmp_path)

        reopened = LanceIndex(tmp_path / "search", edition_id=_NEXT_EDITION)
        assert reopened.discard_orphans() == 1
        assert orphan not in set(lancedb.connect(str(tmp_path / "search")).list_tables().tables)
        # The live language is untouched.
        assert reopened.languages == ["en"]
        assert [i for i, _ in reopened.bm25_search("hello", lang="en")] == ["a"]

    def test_a_rebuild_reclaims_orphans_first(self, tmp_path):
        """A table no manifest names is dropped by the next rebuild, not shipped with it.

        The orphan is a language the rebuild does not write, so overwriting
        same-named tables cannot account for its going.
        """
        import lancedb

        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        orphan = self._add_orphan(tmp_path)

        reopened = LanceIndex(tmp_path / "search", edition_id=_NEXT_EDITION)
        assert reopened.orphan_tables() == [orphan]
        reopened.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "again"}])
        assert reopened.orphan_tables() == []
        db = lancedb.connect(str(tmp_path / "search"))
        assert set(db.list_tables().tables) == {f"search_en__{self._NEXT_EDITION_HEX}"}

    def test_corrupt_manifest_is_treated_as_missing(self, tmp_path):
        """A catalog must not be stranded by an unreadable manifest."""
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        (tmp_path / "search" / "index.json").write_text("not json {")

        reopened = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        assert reopened.languages == []
        # And a rebuild recovers it.
        reopened.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        assert reopened.languages == ["en"]


class TestIncrementalAddPersistence:
    """``add()`` of a previously-unseen language must survive a reopen.

    Tables are reached through the manifest, so a table written without a
    manifest entry is invisible on the next open — the rows would be on
    disk and unfindable.
    """

    def test_new_language_survives_reopen(self, tmp_path):
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        idx.add([{"id": "b", "entity_type": "open", "lang": "fr", "text": "bonjour"}])

        reopened = LanceIndex(tmp_path / "search")
        assert reopened.languages == ["en", "fr"]
        assert [i for i, _ in reopened.bm25_search("bonjour", lang="fr")] == ["b"]
        assert [i for i, _ in reopened.bm25_search("hello", lang="en")] == ["a"]

    def test_upsert_into_existing_language_survives_reopen(self, tmp_path):
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        idx.add([{"id": "c", "entity_type": "open", "lang": "en", "text": "goodbye"}])

        reopened = LanceIndex(tmp_path / "search")
        assert [i for i, _ in reopened.bm25_search("goodbye", lang="en")] == ["c"]
        assert [i for i, _ in reopened.bm25_search("hello", lang="en")] == ["a"]

    def test_search_after_reopen_writes_nothing(self, tmp_path):
        """A built index is used as found, not rebuilt by the first query.

        The rebuild wrote new index files under ``search/``, so searching a
        sealed edition broke the checksums in its manifest.
        """
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(
            [
                {"id": "a", "entity_type": "open", "lang": "en", "text": "hello"},
                {"id": "b", "entity_type": "open", "lang": "fr", "text": "bonjour"},
            ]
        )
        idx.build_fts_index()

        def snapshot() -> dict[str, bytes]:
            root = tmp_path / "search"
            return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        before = snapshot()
        reopened = LanceIndex(tmp_path / "search")
        assert [i for i, _ in reopened.bm25_search("hello", lang="en")] == ["a"]
        assert [i for i, _ in reopened.bm25_search("bonjour", lang="fr")] == ["b"]
        assert snapshot() == before


class TestIndexManifest:
    """The manifest records what each language was indexed with.

    It is written for every catalog, including BM25-only ones — which
    write no ``embeddings.json`` at all, and are exactly where the
    stemmer question matters most.
    """

    def test_manifest_records_stemmer_and_rows(self, tmp_path):
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(
            [
                {"id": "a", "entity_type": "open", "lang": "en", "text": "hello"},
                {"id": "b", "entity_type": "open", "lang": "fr", "text": "bonjour"},
                {"id": "c", "entity_type": "open", "lang": "fr", "text": "salut"},
            ]
        )
        described = idx.describe()
        assert described["en"]["stemmer"] == "English"
        assert described["fr"]["stemmer"] == "French"
        assert described["fr"]["rows"] == 2
        assert described["en"]["rows"] == 1

    def test_unstemmed_language_is_recorded_as_such(self, tmp_path):
        """A language with no Snowball stemmer must say so, rather than
        looking like it got one."""
        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "ja", "text": "こんにちは"}])
        assert idx.describe()["ja"]["stemmer"] is None

    def test_manifest_written_without_an_embedding_model(self, tmp_path):
        """BM25-only catalogs write no embeddings.json — the manifest is
        the only record of the index layout they have."""
        store = open_scratch(tmp_path / "catalog")
        store.put(
            sc.OpenDataset(
                identifier="ds-1",
                title=I18n(en="Population", fr="Population"),
                description=I18n(en="Estimates", fr="Estimations"),
            )
        )
        store.build_index()
        assert not (_search_dir(store) / "embeddings.json").exists()
        described = require_index(store).describe()
        assert described["fr"]["stemmer"] == "French"
        assert described["en"]["stemmer"] == "English"


class TestIndexLayoutProperty:
    """``EditionView.index_layout`` is the public read of the index manifest."""

    def test_empty_before_any_index_is_built(self, tmp_path):
        with scratch_store(tmp_path / "catalog") as store:
            assert store.index_layout == {}

    def test_reports_each_language_after_build(self, tmp_path):
        with scratch_store(tmp_path / "catalog") as store:
            store.put(
                sc.OpenDataset(
                    identifier="ds-1",
                    title=I18n(en="Population", fr="Population"),
                    description=I18n(en="Estimates", fr="Estimations"),
                )
            )
            store.build_index()
            layout = store.index_layout
            assert sorted(layout) == ["en", "fr"]
            assert layout["fr"]["stemmer"] == "French"
            assert layout["fr"]["rows"] >= 1


class TestManifestVersion:
    """A current index opens without a warning, and its manifest names the version and edition that wrote it."""

    def test_no_warning_for_a_current_index(self, tmp_path):
        import warnings as w

        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        with w.catch_warnings():
            w.simplefilter("error")
            LanceIndex(tmp_path / "search")

    def test_manifest_records_the_writing_version(self, tmp_path):
        import json

        from statcatlib.store.lance import LanceIndex

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        manifest = json.loads((tmp_path / "search" / "index.json").read_text())
        assert manifest["written_by"]
        assert manifest["version"] == 3
        assert manifest["edition_id"] == _EDITION
        assert "generation" not in manifest

    def test_an_older_manifest_version_is_unreadable(self, tmp_path):
        """A version 2 manifest names generation-suffixed tables, which this build does not read."""
        import json

        from statcatlib.store.lance import LanceIndex

        search_dir = tmp_path / "search"
        search_dir.mkdir()
        (search_dir / "index.json").write_text(json.dumps({"version": 2, "generation": 3, "languages": {}}))
        described = LanceIndex.unreadable_version(search_dir)
        assert described is not None
        assert "version 2" in described


# ---------------------------------------------------------------------------
# ANN threshold counts vectors, not rows
# ---------------------------------------------------------------------------


def _row(i: int, *, embedding: list[float] | None) -> SearchRow:
    return {
        "id": f"r-{i:04d}",
        "entity_type": "open",
        "lang": "en",
        "text": f"row {i}",
        "embedding": embedding,
    }


_VEC = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]


@pytest.mark.unit
class TestAnnThresholdCountsVectors:
    """The ANN gate must measure embedded rows, not table rows.

    A table mixing embedded rows with ``fts_only`` ones is mostly NULL in
    the embedding column. Counting rows clears the threshold on exactly
    the tables that must not be indexed: IVF trains its centroids over
    the NULLs, the few real vectors scatter into partitions the probe
    never reaches, and vector search returns near-random results while
    the stored vectors themselves stay perfectly correct.
    """

    def test_count_embedded_ignores_rows_without_a_vector(self, tmp_path):
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=_VEC if i % 10 == 0 else None) for i in range(100)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))

        lang = idx._langs["en"]
        assert lang.count_rows() == 100
        assert lang.count_embedded() == 10

    def test_count_embedded_is_zero_without_an_embedding_column(self, tmp_path):
        """A BM25-only catalog has no embedding column at all.

        Lance rejects a predicate naming an absent column, so this must be
        answered from the schema rather than by pushing the filter down.
        """
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])

        lang = idx._langs["en"]
        assert lang.table is not None
        assert "embedding" not in lang.table.schema.names
        assert lang.count_rows() == 1
        assert lang.count_embedded() == 0

    def test_build_index_skips_ann_when_most_rows_are_unembedded(self, tmp_path):
        """300 rows but only 10 vectors: far past the row bar, far under it.

        Locks the behaviour to our own gate. LanceDB happens to refuse
        this one anyway — IVF-PQ will not train on fewer than 256 vectors
        — but that guard is incidental, applies only to PQ, and is
        swallowed by ``contextlib.suppress`` rather than decided by us.
        """
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=_VEC if i < 10 else None) for i in range(300)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()

        tbl = require_table(idx)
        index_columns = {tuple(ix.columns) for ix in tbl.list_indices()}
        assert ("embedding",) not in index_columns, (
            f"ANN index built over a table that is 97% NULL vectors. Indices: {index_columns}"
        )
        # The FTS index is unaffected — those rows are still searchable.
        assert ("text",) in index_columns

    def test_sparse_table_is_indexed_exactly_rather_than_skipped(self, tmp_path):
        """A sparse table, 300 vectors in 6,000 rows, gets an exact index.

        The original diagnosis blamed NULL rows for poisoning IVF
        training. Measured, they do not: recall@10 was 20.0% on a dense
        table of 3,000 vectors and 20.5% on a sparse one holding the same
        3,000 among 60,000 rows. Density was never the variable — the
        quantiser was. So the sparse table gets an index like any other,
        and it is IVF_FLAT.
        """
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=[(i % 97) / 100.0, *_VEC[1:]] if i < 300 else None) for i in range(6000)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()

        tbl = require_table(idx)
        by_column = {tuple(ix.columns): ix for ix in tbl.list_indices()}
        assert ("embedding",) in by_column
        assert "FLAT" in by_column[("embedding",)].index_type.upper()

    def test_manifest_records_the_vector_index_and_embedded_count(self, tmp_path):
        """``index_layout`` must answer "is this index exact?" without a probe."""
        import json

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=[(i % 97) / 100.0, *_VEC[1:]] if i < 300 else None) for i in range(6000)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()

        manifest = json.loads((tmp_path / "search" / "index.json").read_text())
        en = manifest["languages"]["en"]
        assert en["rows"] == 6000
        assert en["embedded"] == 300
        assert en["ann"] == "IVF_FLAT"

    def test_manifest_reports_no_ann_when_the_bar_is_not_met(self, tmp_path):
        import json

        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=_VEC if i < 10 else None) for i in range(300)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()

        en = json.loads((tmp_path / "search" / "index.json").read_text())["languages"]["en"]
        assert en["embedded"] == 10
        assert en["ann"] is None

    def test_build_index_still_builds_ann_when_rows_are_embedded(self, tmp_path):
        """The gate must not become a blanket refusal to index."""
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        rows = [_row(i, embedding=[(i % 7) / 10.0, *_VEC[1:]]) for i in range(300)]
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()

        tbl = require_table(idx)
        index_columns = {tuple(ix.columns) for ix in tbl.list_indices()}
        assert ("embedding",) in index_columns, f"No ANN index despite 300 vectors. Indices: {index_columns}"


# ---------------------------------------------------------------------------
# Warn on a quantised vector index from before pinaxlib 5.24.0
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQuantisedIndexWarning:
    """A pre-pinaxlib-5.24.0 index keeps serving at ~78% recall with nothing to say so.

    The warning has to distinguish "lossy index" from "no index", which
    the manifest cannot do — it only began recording ``ann`` in version
    2, so a pre-fix index and a BM25-only catalog look identical there.
    The table itself knows.
    """

    def _sparse_rows(self, n_embedded: int, n_total: int) -> list[SearchRow]:
        return [_row(i, embedding=[(i % 97) / 100.0, *_VEC[1:]] if i < n_embedded else None) for i in range(n_total)]

    def test_warns_when_the_vector_index_is_quantised(self, tmp_path, recwarn):
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(self._sparse_rows(300, 6000), embedding_dim=len(_VEC))
        idx.build_index()
        # Downgrade to what a pre-pinaxlib-5.24.0 build left behind.
        require_table(idx).create_index(
            metric="cosine", vector_column_name="embedding", index_type="IVF_PQ", replace=True
        )

        recwarn.clear()
        LanceIndex(tmp_path / "search")
        messages = [str(w.message) for w in recwarn]
        assert any("quantised" in m and "en" in m for m in messages), messages

    def test_does_not_warn_on_a_current_index(self, tmp_path):
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(self._sparse_rows(300, 6000), embedding_dim=len(_VEC))
        idx.build_index()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            LanceIndex(tmp_path / "search")

    def test_does_not_warn_on_a_bm25_only_catalog(self, tmp_path):
        """No embedding column, no vector index, nothing to rebuild."""
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk([{"id": "a", "entity_type": "open", "lang": "en", "text": "hello"}])
        idx.build_index()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            LanceIndex(tmp_path / "search")

    def test_does_not_warn_when_the_language_is_below_the_ann_bar(self, tmp_path):
        """Too few vectors to index means a brute-force scan — which is exact.

        The one case that looks stale from the manifest (``ann: null``)
        and is in fact the most accurate configuration there is.
        """
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(self._sparse_rows(10, 300), embedding_dim=len(_VEC))
        idx.build_index()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            LanceIndex(tmp_path / "search")

    def test_warning_names_every_affected_language_once(self, tmp_path, recwarn):
        rows = self._sparse_rows(300, 3000)
        fr_rows: list[SearchRow] = [{**r, "lang": "fr", "id": r["id"] + "-fr"} for r in rows]
        rows += fr_rows
        idx = LanceIndex(tmp_path / "search", edition_id=_EDITION)
        idx.add_bulk(rows, embedding_dim=len(_VEC))
        idx.build_index()
        for lang in ("en", "fr"):
            require_table(idx, lang).create_index(
                metric="cosine", vector_column_name="embedding", index_type="IVF_PQ", replace=True
            )

        recwarn.clear()
        LanceIndex(tmp_path / "search")
        stale = [str(w.message) for w in recwarn if "quantised" in str(w.message)]
        assert len(stale) == 1, stale
        assert "en, fr" in stale[0]


class TestSearchRowMatchesSchema:
    """`SearchRow` and the Arrow schema declare the same columns.

    The TypedDict duplicates the column list in `_BASE_COLUMNS` /
    `_EXTENSION_COLUMNS`, and a duplicated list that can drift is worse than
    no list at all: a column added to one and not the other is exactly the
    silent-null failure the declared schema exists to prevent.
    """

    def test_search_row_matches_schema(self):
        from typing import get_type_hints

        from statcatlib.store.lance import (
            _BASE_COLUMNS,
            _EMBEDDING_COLUMN,
            _EXTENSION_COLUMNS,
            SearchRow,
        )

        declared = {name for name, _kind in (*_BASE_COLUMNS, *_EXTENSION_COLUMNS)} | {_EMBEDDING_COLUMN}
        annotated = set(get_type_hints(SearchRow).keys())
        assert annotated == declared, (
            f"SearchRow and the Arrow schema have drifted — "
            f"only in SearchRow: {sorted(annotated - declared)}; "
            f"only in the schema: {sorted(declared - annotated)}"
        )

    def test_required_split_is_a_checker_fact_not_a_runtime_one(self):
        """`__required_keys__` cannot witness the `NotRequired` split here.

        `lance.py` carries `from __future__ import annotations`, so every
        annotation is a string at runtime and TypedDict cannot see the
        `NotRequired[...]` wrapper when it computes `__required_keys__` — it
        reports all 19 keys as required. The type checker resolves the strings
        and gets it right, which is why `add_bulk` still refuses a row missing
        ``id`` and accepts one missing ``code_id``.

        Asserted rather than merely commented so the surprise is recorded
        where someone would otherwise reach for `__required_keys__` and build
        on a wrong answer.
        """
        from statcatlib.store.lance import _BASE_COLUMNS, SearchRow

        base = {name for name, _kind in _BASE_COLUMNS}
        assert SearchRow.__required_keys__ != frozenset(base)
        assert base < SearchRow.__required_keys__


class TestAsymmetricPrefixWarnings:
    """`build_index` warns where a prefix mismatch would quietly hurt recall.

    Both cases produce vectors whose document and query sides sit in different
    regions of the space. Nothing raises — recall is merely worse — which is
    how the original defect reached a downstream caller unnoticed.
    """

    def _store(self, tmp_path: Path) -> contextlib.AbstractContextManager[ScratchStore]:
        return scratch_store(tmp_path / "catalog", embedding_model=_StubModel())

    def test_warns_for_e5_without_document_prefix(self, tmp_path: Path):
        with self._store(tmp_path) as store, pytest.warns(UserWarning, match="asymmetric"):
            store.build_index(model_name="intfloat/multilingual-e5-small")

    def test_names_both_prefixes_it_expects(self, tmp_path: Path):
        with self._store(tmp_path) as store, pytest.warns(UserWarning, match="asymmetric") as caught:
            store.build_index(model_name="intfloat/e5-base-v2")
        message = str(caught[0].message)
        assert "passage: " in message
        assert "query: " in message

    def test_silent_when_the_prefix_is_set(self, tmp_path: Path):
        with self._store(tmp_path) as store, warnings.catch_warnings():
            warnings.simplefilter("error")
            store.build_index(
                model_name="intfloat/multilingual-e5-small",
                document_prefix="passage: ",
                query_prefix="query: ",
            )

    def test_silent_for_a_symmetric_model(self, tmp_path: Path):
        """An unlisted model is symmetric as far as statcatlib knows; no prefix, no warning."""
        with self._store(tmp_path) as store, warnings.catch_warnings():
            warnings.simplefilter("error")
            store.build_index(model_name="BAAI/bge-m3")

    def test_warns_when_prefixes_cannot_be_persisted(self, tmp_path: Path):
        """Prefixes set but no ``model_name`` writes no sidecar, so a view of the sealed edition loses them."""
        store = scratch_store(
            tmp_path / "catalog",
            embedding_model=_StubModel(),
            document_prefix="passage: ",
            query_prefix="query: ",
        )
        with store as s, pytest.warns(UserWarning, match="no embeddings.json is written"):
            s.build_index()

    def test_the_loss_it_warns_about_is_real(self, tmp_path: Path):
        """The warning is not theoretical: a view of the sealed edition restores neither prefix.

        The seal rebuilds the index with the same arguments, so it warns again.
        """
        path = tmp_path / "catalog"
        store = scratch_store(path, embedding_model=_StubModel(), document_prefix="passage: ", query_prefix="query: ")
        with store as s:
            with pytest.warns(UserWarning, match="no embeddings.json is written"):
                s.build_index()
            with pytest.warns(UserWarning, match="no embeddings.json is written"):
                s.seal()
        with sc.Catalog.open(path) as cat:
            reopened = cat.product("p").view(embedding_model=_StubModel())
            assert reopened.document_prefix == ""
            assert reopened.query_prefix == ""


class TestQueryBuilderSearchLang:
    """``QueryBuilder.search`` and ``hybrid_search`` read the table *lang* names (GH-60).

    Each language has its own search table. Before ``lang`` both methods always
    read the English one, so a French query combined with filters found nothing.
    The datasets carry French text only, so the English table holds none of them.
    """

    @staticmethod
    def _french_store(tmp_path, model=None):
        store = open_scratch(tmp_path / "fr", embedding_model=model)
        for identifier, title, description in [
            ("chomage-qc", "Taux de chômage au Québec", "Enquête sur la population active"),
            ("chomage-on", "Taux de chômage en Ontario", "Enquête sur la population active"),
            ("loyers-qc", "Loyers moyens au Québec", "Enquête sur le logement"),
        ]:
            store.put(sc.OpenDataset(identifier=identifier, title=I18n(fr=title), description=I18n(fr=description)))
        store.build_index()
        return store

    def test_search_with_a_filter_reads_the_french_table(self, tmp_path):
        store = self._french_store(tmp_path)
        query = store.query(sc.BaseDataset).filter(q.title_contains("Québec", lang="fr")).search("chômage", lang="fr")
        assert [ds.identifier for ds in query.collect()] == ["chomage-qc"]
        assert query.count() == 1
        assert query.exists()

    def test_search_defaults_to_the_english_table(self, tmp_path):
        store = self._french_store(tmp_path)
        query = store.query(sc.BaseDataset).search("chômage")
        assert query.collect() == []
        assert not query.exists()

    def test_hybrid_search_with_a_filter_reads_the_french_table(self, tmp_path, model):
        store = self._french_store(tmp_path, model)
        query = store.query(sc.BaseDataset).filter(q.title_contains("Québec", lang="fr"))
        assert {ds.identifier for ds in query.hybrid_search("chômage", lang="fr").collect()} == {
            "chomage-qc",
            "loyers-qc",
        }
        assert query.hybrid_search("chômage").collect() == []

    def test_lang_survives_later_chaining(self, tmp_path):
        store = self._french_store(tmp_path)
        query = store.query(sc.BaseDataset).search("chômage", lang="fr").sort_by("identifier").limit(5)
        assert {ds.identifier for ds in query.collect()} == {"chomage-qc", "chomage-on"}


class _FarFromCensusModel(_StubModel):
    """Embeds text mentioning "Census" farthest from every other text, the query included."""

    @override
    def _encode_one(self, text: str) -> list[float]:
        return [1.0, 0.0] + [0.0] * (self.DIM - 2) if "Census" in text else [0.0, 1.0] + [0.0] * (self.DIM - 2)


class TestSearchRanksOnlyFilteredDatasets:
    """``QueryBuilder`` search ranks the datasets that pass its filters (GH-59).

    It used to take the top 200 matches across the catalog and filter those,
    so a match ranked past 200 was never returned. Here 250 datasets match
    "population" and the one the filter keeps, with the longest text, ranks
    last.
    """

    _N = 250

    @classmethod
    def _store(cls, tmp_path, model=None):
        store = open_scratch(tmp_path / "pool", embedding_model=model)
        datasets = [
            sc.OpenDataset(
                identifier=f"pop-{i:03d}",
                title=I18n(en=f"Population {i}"),
                description=I18n(en="Population estimates"),
            )
            for i in range(cls._N - 1)
        ]
        datasets.append(
            sc.OpenDataset(
                identifier="o'brien-census",
                title=I18n(en="Census of agriculture, farms, crops, livestock, machinery and land use"),
                description=I18n(en="Farm operators, land tenure, irrigation and the rural population"),
                status="current",
            )
        )
        with store.bulk_load():
            store.put(datasets)
        store.build_index()
        return store

    def test_the_filtered_match_ranks_past_the_pool(self, tmp_path):
        store = self._store(tmp_path)
        unfiltered = store.query(sc.OpenDataset).search("population").collect()
        assert "o'brien-census" not in [ds.identifier for ds in unfiltered]

    def test_search_finds_a_filtered_match_ranked_last(self, tmp_path):
        query = self._store(tmp_path).query(sc.OpenDataset).filter(status="current").search("population")
        assert [ds.identifier for ds in query.collect()] == ["o'brien-census"]
        assert query.count() == 1
        assert query.exists()

    def test_hybrid_search_finds_a_filtered_match_ranked_last(self, tmp_path):
        query = self._store(tmp_path, _FarFromCensusModel()).query(sc.OpenDataset).filter(status="current")
        assert [ds.identifier for ds in query.hybrid_search("population").collect()] == ["o'brien-census"]

    def test_paging_reaches_past_the_pool(self, tmp_path):
        query = self._store(tmp_path).query(sc.OpenDataset).search("population")
        assert len(query.offset(240).limit(20).collect()) == self._N - 240

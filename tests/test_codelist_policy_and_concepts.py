"""Tests for CodelistIndexPolicy and concept indexing (issue #95).

Covers:
- CodelistIndexPolicy(mode="fts_only") bypasses max_codes_per_codelist
- Policy-covered codelists are emitted with embedding=None even when
  embed_codes=True
- _warn_oversized_codelists suppresses URNs covered by an fts_only policy
- Concept rows from sdmx.concept appear in the Lance index with
  entity_type="concept" and carry non-null embeddings when a model is
  configured (default embed_concepts=True)
- Sidecar v4 records the new fields
"""

from __future__ import annotations

import math
import pathlib
import warnings

import pytest
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.concept import Concept, ConceptScheme
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString

from statcatlib.sources.sdmx import ingest_sdmx
from statcatlib.store.store import Catalog, CodelistIndexPolicy
from tests.conftest import search_table

pytestmark = pytest.mark.integration


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _make_registry_with_big_geo() -> tuple[InMemoryRegistry, str]:
    """Registry with a CL_GEO codelist that has 5 codes plus a CL_FREQ with 2.

    Returns the registry and the geo codelist's URN so tests can build a
    policy referencing it without hard-coding URN syntax.
    """
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[
            Code(id="CA", name=_i("Canada")),
            Code(id="US", name=_i("United States")),
            Code(id="MX", name=_i("Mexico")),
            Code(id="FR", name=_i("France")),
            Code(id="DE", name=_i("Germany")),
        ],
    )
    freq_cl = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency"),
        codes=[
            Code(id="A", name=_i("Annual")),
            Code(id="M", name=_i("Monthly")),
        ],
    )
    dsd = DataStructure(
        id="DSD_TEST",
        maintainer="TEST",
        name=_i("Test DSD"),
        dimensions=[
            Dimension(id="GEO", representation=geo_cl),
            Dimension(id="FREQ", representation=freq_cl),
        ],
    )
    flow = Dataflow(
        id="DF_TEST",
        maintainer="TEST",
        name=_i("Test Dataflow"),
        description=_i("A test dataflow"),
        structure=dsd,
    )
    registry = InMemoryRegistry()
    registry.add(geo_cl)
    registry.add(freq_cl)
    registry.add(dsd)
    registry.add(flow)
    geo_urn = str(Codelist.urn_for(agency="TEST", id="CL_GEO", version="1.0"))
    return registry, geo_urn


def _make_registry_with_concepts() -> InMemoryRegistry:
    """Registry with a ConceptScheme so concept-row indexing has something to find."""
    cs = ConceptScheme(
        id="CS_THEMES",
        maintainer="TEST",
        name=_i("Test Themes"),
        description=_i("Statistical themes used by the test catalog"),
        concepts=[
            Concept(
                id="GDP",
                name=_i("Gross Domestic Product"),
                description=_i(
                    "Total monetary value of finished goods and services produced "
                    "within a country during a specified period."
                ),
            ),
            Concept(
                id="CPI",
                name=_i("Consumer Price Index"),
                description=_i(
                    "Measure that examines the weighted average of prices of a basket of consumer goods and services."
                ),
            ),
        ],
    )
    cl = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency"),
        codes=[Code(id="A", name=_i("Annual"))],
    )
    dsd = DataStructure(
        id="DSD_CONCEPT",
        maintainer="TEST",
        name=_i("Concept Test DSD"),
        dimensions=[Dimension(id="FREQ", representation=cl)],
    )
    flow = Dataflow(
        id="DF_CONCEPT",
        maintainer="TEST",
        name=_i("Concept Test Dataflow"),
        structure=dsd,
    )
    registry = InMemoryRegistry()
    registry.add(cs)
    registry.add(cl)
    registry.add(dsd)
    registry.add(flow)
    return registry


def _stub_model(dim: int = 8):
    class _Stub:
        DIM = dim

        def encode(self, text):
            if isinstance(text, list):
                return [self._one(t) for t in text]
            return self._one(text)

        def _one(self, t):
            h = hash(t)
            v = [(h >> i & 0xFF) / 255.0 for i in range(self.DIM)]
            norm = math.sqrt(sum(x**2 for x in v)) or 1.0
            return [x / norm for x in v]

    return _Stub()


class TestCodelistIndexPolicy:
    def test_fts_only_policy_bypasses_size_cap(self) -> None:
        """A codelist over the cap is still emitted when listed in an fts_only policy."""
        store = Catalog().open()
        registry, geo_urn = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)

        # Cap of 3 would skip CL_GEO (5 codes). Policy lifts that.
        items = store._collect_code_texts(
            max_codes_per_codelist=3,
            codelist_policies=[CodelistIndexPolicy(urns=[geo_urn])],
        )
        geo_items = [r for r in items if r["codelist_urn"] == geo_urn]
        assert len(geo_items) > 0, "policy did not lift the size cap"

    def test_warning_suppressed_for_policy_covered_codelist(self) -> None:
        """Codelists explicitly opted-in must not trigger the oversize warning."""
        store = Catalog().open()
        registry, geo_urn = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            store._collect_code_texts(
                max_codes_per_codelist=3,
                codelist_policies=[CodelistIndexPolicy(urns=[geo_urn])],
            )
        relevant = [w for w in caught if "build_index skipped" in str(w.message)]
        assert relevant == [], f"unexpected warning(s): {[str(w.message) for w in relevant]}"

    def test_fts_only_forces_embedding_none_even_when_embed_codes_true(self, tmp_path: pathlib.Path) -> None:
        """Policy-covered code rows must end up with embedding=None.

        Without the policy override, ``embed_codes=True`` would encode
        every code row.  The policy is the way to say "Tantivy is right
        for this codelist; don't burn vector budget on it."
        """
        store = Catalog(tmp_path / "catalog").open()
        registry, geo_urn = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)
        store.build_index(
            embedding_model=_stub_model(),
            include_codes=True,
            embed_codes=True,
            max_codes_per_codelist=3,
            codelist_policies=[CodelistIndexPolicy(urns=[geo_urn])],
        )

        assert store._lance is not None
        rows = search_table(store).search().where(f"codelist_urn = '{geo_urn}'", prefilter=True).limit(100).to_list()
        assert rows, "no geo code rows landed in Lance"
        assert all(r.get("embedding") is None for r in rows), (
            "fts_only policy did not force embedding=None for covered codelist"
        )

    def test_default_build_byte_compatible_when_policy_empty(self, tmp_path: pathlib.Path) -> None:
        """Without codelist_policies, the row counts must match the prior shape.

        Regression gate per the issue's acceptance criteria.
        """
        store = Catalog(tmp_path / "catalog").open()
        registry, _ = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)
        items_no_policy = store._collect_code_texts(max_codes_per_codelist=500)
        items_empty_policy = store._collect_code_texts(
            max_codes_per_codelist=500,
            codelist_policies=(),
        )
        assert len(items_no_policy) == len(items_empty_policy)

    def test_fts_only_builds_at_a_realistic_embedding_width(self, tmp_path: pathlib.Path) -> None:
        """A table mixing vectors and deliberate NULLs must build on lancedb >= 0.36 (#125).

        lancedb now counts a NULL vector as wrong-width and rejects it under
        its default ``on_bad_vectors="error"``. Without a target schema it
        only inspects fixed-size float lists of 10+ dimensions, so the 8-dim
        stub every other test uses never reaches the check; 768 is the width
        the failure was reported at.
        """
        store = Catalog(tmp_path / "catalog").open()
        registry, geo_urn = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)
        model = _stub_model(dim=768)
        store.build_index(
            embedding_model=model,
            include_codes=True,
            embed_codes=True,
            codelist_policies=[CodelistIndexPolicy(urns=[geo_urn])],
        )

        tbl = search_table(store)
        geo_rows = tbl.search().where(f"codelist_urn = '{geo_urn}'", prefilter=True).limit(100).to_list()
        assert geo_rows, "no geo code rows landed in Lance"
        assert all(r["embedding"] is None for r in geo_rows), "fts_only NULLs did not round-trip as NULL"
        assert tbl.count_rows(filter="embedding IS NOT NULL") > 0, "embedded rows were lost alongside the NULLs"

        # Vector search never sees the NULL rows; BM25 still finds them.
        nearest = tbl.search(model.encode("Canada"), vector_column_name="embedding").limit(100).to_list()
        assert all(r["codelist_urn"] != geo_urn for r in nearest)
        assert any(h.payload.code_label == "Canada" for h in store.codes.find("Canada", lang="en").items)


class TestConceptIndexing:
    def test_collect_concept_texts_emits_rows(self) -> None:
        store = Catalog().open()
        ingest_sdmx(store, _make_registry_with_concepts())
        items = store._collect_concept_texts()
        assert len(items) > 0
        labels = {r["text"] for r in items if r["lang"] == "en"}
        assert any("Gross Domestic Product" in t for t in labels)
        assert any("basket of consumer goods" in t for t in labels)

    def test_concept_rows_have_concept_columns(self) -> None:
        store = Catalog().open()
        ingest_sdmx(store, _make_registry_with_concepts())
        items = store._collect_concept_texts()
        gdp = next(r for r in items if r["concept_id"] == "GDP" and r["lang"] == "en")
        assert gdp["entity_type"] == "concept"
        assert (gdp["concept_urn"] or "").endswith(".GDP")
        assert "CS_THEMES" in (gdp["scheme_urn"] or "")
        # Code-extension columns are present but empty for schema parity.
        assert gdp["code_id"] == ""
        assert gdp["dimension_id"] == ""

    def test_build_index_writes_concept_rows_with_embeddings(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        store.build_index(embedding_model=_stub_model(), include_concepts=True)

        assert store._lance is not None
        rows = search_table(store).search().where("entity_type = 'concept'", prefilter=True).limit(100).to_list()
        assert rows, "no concept rows landed in Lance"
        assert all(r.get("embedding") is not None for r in rows), (
            "concept rows missing embeddings under embed_concepts=True default"
        )

    def test_build_index_skips_concepts_when_disabled(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        store.build_index(include_concepts=False)

        assert store._lance is not None
        rows = search_table(store).search().where("entity_type = 'concept'", prefilter=True).limit(10).to_list()
        assert rows == []

    def test_dataset_search_excludes_concept_rows(self, tmp_path: pathlib.Path) -> None:
        """Catalog.search must continue to surface dataset rows only."""
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        store.build_index(embedding_model=_stub_model(), include_concepts=True)

        # "Consumer Price Index" appears in a concept row but in no dataset row.
        # cat.search now spans all kinds — to keep dataset results dataset-shaped,
        # use cat.datasets.find which is type-narrowed.
        result = store.datasets.find("Consumer Price Index", limit=10)
        assert all(hit.kind == "dataset" for hit in result.items)
        assert all(":Concept=" not in hit.payload.identifier for hit in result.items)


class TestSearchAnythingMode:
    """cat.search now spans every indexed kind, RRF-merged."""

    def test_cross_kind_hits(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        store.build_index(include_codes=True, include_concepts=True)

        result = store.search("Annual", limit=20)
        # The token "Annual" appears in both a code row (CL_FREQ:A) and a
        # dataset row (via dim labels). Anything-mode must surface both.
        kinds = {hit.kind for hit in result.items}
        assert "code" in kinds
        assert kinds <= {"dataset", "concept", "code"}

    def test_kinds_filter_narrows_to_one_kind(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        store.build_index(include_codes=True, include_concepts=True)

        result = store.search("Annual", kinds=("code",), limit=20)
        assert all(hit.kind == "code" for hit in result.items)


class TestScopeGet:
    def test_concepts_get_returns_payload(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        ingest_sdmx(store, _make_registry_with_concepts())
        # build_index not required for .get() — it reads sdmx.concept directly.
        row = store._c.execute("SELECT concept_urn FROM sdmx.concept WHERE concept_id = 'GDP'").fetchone()
        assert row is not None
        gdp = store.concepts.get(row[0])
        assert gdp is not None
        assert gdp.concept_id == "GDP"
        assert "Gross Domestic Product" in gdp.name
        assert "monetary value" in gdp.description

    def test_codes_get_returns_payload(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        registry, _ = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)
        row = store._c.execute("SELECT code_urn FROM sdmx.code WHERE code_id = 'CA'").fetchone()
        assert row is not None
        ca = store.codes.get(row[0])
        assert ca is not None
        assert ca.code_id == "CA"
        assert ca.code_label == "Canada"


class TestSidecarV4:
    def test_sidecar_records_new_fields(self, tmp_path: pathlib.Path) -> None:
        store = Catalog(tmp_path / "catalog").open()
        registry, geo_urn = _make_registry_with_big_geo()
        ingest_sdmx(store, registry)
        store.build_index(
            embedding_model=_stub_model(),
            model_name="stub-model",
            include_codes=True,
            include_concepts=True,
            codelist_policies=[CodelistIndexPolicy(urns=[geo_urn])],
        )
        meta = store.embedding_metadata
        assert meta is not None
        assert geo_urn in meta.fts_only_codelist_urns
        assert meta.include_concepts is True
        assert meta.embed_concepts is True
        assert meta.concept_rows_indexed is not None

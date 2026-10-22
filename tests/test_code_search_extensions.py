"""Tests for the v1 code-search extensions.

Covers:

* ``codelist_urns`` scope filter on ``build_index``.
* SKOS ``altLabel`` blending into code search text.
* ``parent_id`` / ``depth`` carried on code rows from the hierarchical
  codelist model.
* ``semantic_search_codes`` — pure vector NN over code rows.
* ``hybrid_search_codes`` — RRF fusion of BM25 + vector over codes.
"""

from __future__ import annotations

import math
import pathlib
import warnings

import pytest
from sdmxlib.model.annotations import Annotation, Annotations
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString

import statcatlib as sc
from statcatlib.sources.sdmx import ingest_sdmx
from statcatlib.store.store import SEARCH_CONTEXT_ANNOTATION_TYPE, SEARCH_EXCLUDE_ANNOTATION_TYPE, Catalog
from tests.conftest import concept_of

pytestmark = pytest.mark.integration


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _alt(title: str) -> Annotation:
    """SKOS altLabel annotation carrying non-localised text in ``title``."""
    return Annotation(type="skos:altLabel", title=title)


def _alt_i18n(en: str, fr: str) -> Annotation:
    """SKOS altLabel annotation carrying localised text, one per language."""
    return Annotation(type="skos:altLabel", text=_i(en, fr))


def _ctx(en: str, fr: str) -> Annotation:
    """statcat:searchContext annotation — localized text, one value per language."""
    return Annotation(type=SEARCH_CONTEXT_ANNOTATION_TYPE, text=_i(en, fr))


def _make_hierarchical_registry() -> InMemoryRegistry:
    """Two codelists: GEO (hierarchical CA → ON, with altLabels) and FREQ (flat).

    The hierarchy lets us assert ``parent_id`` / ``depth`` plumb through.
    The two altLabel shapes are both represented on purpose: ``ON`` carries
    a non-localised ``title`` and ``CA`` a language-tagged one, so the
    tests can pin each one's folding behaviour independently.
    """
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[
            Code(
                id="CA",
                name=_i("Canada"),
                annotations=Annotations([_alt_i18n("the Dominion", "le Dominion")]),
            ),
            Code(
                id="ON",
                name=_i("Ontario"),
                parent_id="CA",
                annotations=Annotations(
                    [
                        _alt("Province of Ontario"),
                        _ctx("Geographic areas", "Régions géographiques"),
                    ]
                ),
            ),
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


def _store_with_sdmx(tmp_path: pathlib.Path | None = None) -> Catalog:
    store = Catalog(tmp_path / "catalog").open() if tmp_path is not None else Catalog().open()
    ingest_sdmx(store, _make_hierarchical_registry())
    return store


def _stub_model():
    """Tiny deterministic encoder — used for tests that build the index."""

    class _Stub:
        DIM = 8

        def encode(self, text):
            if isinstance(text, list):
                return [self._encode_one(t) for t in text]
            return self._encode_one(text)

        def _encode_one(self, t: str) -> list[float]:
            h = hash(t)
            v = [(h >> i & 0xFF) / 255.0 for i in range(self.DIM)]
            norm = math.sqrt(sum(x**2 for x in v)) or 1.0
            return [x / norm for x in v]

        def get_sentence_embedding_dimension(self) -> int:
            return self.DIM

    return _Stub()


class TestMaxCodesWarning:
    def test_warns_when_codelist_exceeds_cap(self) -> None:
        store = _store_with_sdmx()
        # CL_GEO and CL_FREQ have 2 codes each; cap=1 drops both.
        with pytest.warns(UserWarning, match="skipped 2 codelist"):
            store._collect_code_texts(max_codes_per_codelist=1)

    def test_no_warning_when_all_codelists_fit(self) -> None:
        store = _store_with_sdmx()
        # Cap of 500 (default) leaves both 2-code codelists in scope.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            store._collect_code_texts(max_codes_per_codelist=500)


class TestCodelistUrnsFilter:
    def test_filter_narrows_collected_rows(self) -> None:
        store = _store_with_sdmx()
        # Discover the CL_FREQ URN from the registry; filter to it alone.
        cl_urn = store._c.execute("SELECT codelist_urn FROM sdmx.code WHERE code_id = 'A' LIMIT 1").fetchone()
        assert cl_urn is not None
        freq_urn = cl_urn[0]
        items = store._collect_code_texts(codelist_urns=[freq_urn])
        assert items, "filter should not produce an empty result for a valid codelist"
        assert all(r["codelist_urn"] == freq_urn for r in items)
        # GEO codes must be absent.
        assert not any(r["code_id"] in {"CA", "ON"} for r in items)

    def test_filter_none_indexes_all(self) -> None:
        store = _store_with_sdmx()
        all_items = store._collect_code_texts()
        code_ids = {r["code_id"] for r in all_items}
        assert {"CA", "ON", "A", "M"}.issubset(code_ids)


class TestAltLabelBlending:
    def test_alt_label_appears_in_text_by_default(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        ontario = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        assert "Province of Ontario" in ontario["text"]

    def test_alt_label_suppressed_when_disabled(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts(include_alt_labels=False)
        ontario = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        assert "Province of Ontario" not in ontario["text"]
        # Base prefLabel still present.
        assert "Ontario" in ontario["text"]

    def test_untagged_title_still_reaches_every_language(self) -> None:
        """Back-compat: a title carries no language, so the only safe
        reading is "applies to all of them". Catalogs published before
        localised altLabels existed must not lose their synonyms."""
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "Province of Ontario" in on_en["text"]
        assert "Province of Ontario" in on_fr["text"]

    def test_localised_alt_label_is_per_language_not_smeared(self) -> None:
        """A language-tagged synonym belongs to its own language's row
        alone — the point of the change: an English synonym in a French
        row pollutes the French corpus and dilutes its term statistics."""
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        ca_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "CA")
        ca_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "CA")
        assert "the Dominion" in ca_en["text"]
        assert "le Dominion" in ca_fr["text"]
        assert "le Dominion" not in ca_en["text"]
        assert "the Dominion" not in ca_fr["text"]

    def test_localised_alt_labels_are_suppressed_when_disabled(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts(include_alt_labels=False)
        ca_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "CA")
        assert "le Dominion" not in ca_fr["text"]
        assert "Canada" in ca_fr["text"]


def _attach_footnote(
    store: Catalog,
    *,
    code_id: str,
    set_urn: str,
    i18n: dict[str, str] | None = None,
    scalar: str | None = None,
    attribute_path: str = "FOOTNOTE_TEXT",
    sibling_index: int = 0,
) -> None:
    """Attach a reference-metadata value to ``code_id``'s code URN.

    Writes ``sdmx.metadata_target`` / ``sdmx.metadata_value`` directly —
    the blend reads those two tables, and going through them keeps the
    fixture pinned to the storage shape the loader actually queries.
    Pass ``i18n`` for a localised value or ``scalar`` for an untagged one.
    """
    row = store._c.execute(
        "SELECT code_urn FROM sdmx.code WHERE code_id = ? LIMIT 1",
        [code_id],
    ).fetchone()
    assert row is not None, f"no code {code_id!r} in fixture"
    store._c.execute(
        """
        INSERT INTO sdmx.metadata_target (set_urn, ordinal, target_urn)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [set_urn, sibling_index, row[0]],
    )
    store._c.execute(
        """
        INSERT INTO sdmx.metadata_value
            (set_urn, attribute_path, sibling_index, value_kind, scalar, i18n)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            set_urn,
            attribute_path,
            sibling_index,
            "i18n" if i18n else "scalar",
            scalar,
            i18n,
        ],
    )


class TestReferenceMetadataBlend:
    """Blended footnote text is selected per language (#100).

    Same shape as the altLabel fix: a localised value folds into its own
    language's row alone, while a value with no language at all (a bare
    ``scalar`` — URL, vintage) keeps folding into every language's row.
    """

    _BLEND = (sc.ReferenceMetadataBlend(attribute_path="FOOTNOTE_TEXT"),)

    def test_localised_blend_is_per_language_not_smeared(self) -> None:
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:FOOTNOTES",
            i18n={"en": "committed to protect privacy", "fr": "engagé à protéger la vie privée"},
        )
        items = store._collect_code_texts(reference_metadata_blends=self._BLEND)
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "committed to protect privacy" in on_en["text"]
        assert "engagé à protéger la vie privée" in on_fr["text"]
        # The bug: the French row carried the English footnote verbatim.
        assert "committed to protect privacy" not in on_fr["text"]
        assert "engagé à protéger la vie privée" not in on_en["text"]

    def test_scalar_blend_reaches_every_language(self) -> None:
        """Back-compat: a scalar carries no language, so the only safe
        reading is "applies to all of them"."""
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:SCALAR",
            scalar="https://example.org/notes/2021",
        )
        items = store._collect_code_texts(reference_metadata_blends=self._BLEND)
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "https://example.org/notes/2021" in on_en["text"]
        assert "https://example.org/notes/2021" in on_fr["text"]

    def test_localised_value_does_not_also_contribute_its_scalar(self) -> None:
        """A value contributes to exactly one bucket — otherwise a
        migrated catalog would index the scalar twice per row."""
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:BOTH",
            i18n={"en": "localised note", "fr": "note localisée"},
            scalar="stale fallback",
        )
        items = store._collect_code_texts(reference_metadata_blends=self._BLEND)
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "localised note" in on_en["text"]
        assert "stale fallback" not in on_en["text"]
        assert "stale fallback" not in on_fr["text"]

    def test_monolingual_catalog_indexes_as_before(self) -> None:
        """A value localised in one language only must still reach that
        language's row — the common case for un-migrated catalogs."""
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:EN_ONLY",
            i18n={"en": "english only note"},
        )
        items = store._collect_code_texts(reference_metadata_blends=self._BLEND)
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "english only note" in on_en["text"]
        # Not smeared into French just because French has no value.
        assert "english only note" not in on_fr["text"]

    def test_max_chars_budget_applies_per_language(self) -> None:
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:LONG",
            i18n={"en": "A" * 400, "fr": "B" * 400},
        )
        blends = (sc.ReferenceMetadataBlend(attribute_path="FOOTNOTE_TEXT", max_chars=10),)
        items = store._collect_code_texts(reference_metadata_blends=blends)
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        assert "A" * 10 in on_en["text"]
        assert "A" * 11 not in on_en["text"]
        assert "B" * 10 in on_fr["text"]

    def test_codes_without_reference_metadata_unaffected(self) -> None:
        store = _store_with_sdmx()
        _attach_footnote(
            store,
            code_id="ON",
            set_urn="urn:test:set:ONLY_ON",
            i18n={"en": "ontario note", "fr": "note ontarienne"},
        )
        items = store._collect_code_texts(reference_metadata_blends=self._BLEND)
        ca_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "CA")
        assert "ontario note" not in ca_en["text"]
        assert "Canada" in ca_en["text"]


class TestSearchContextBlending:
    """statcat:searchContext annotations fold into the per-language corpus.

    Context text is selected per language — the disambiguation fix for
    same-label codes under different universes (#61). Language-tagged
    altLabels now behave the same way; see :class:`TestAltLabelBlending`.
    """

    def test_context_appears_in_text_by_default(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        assert "Geographic areas" in on_en["text"]
        # Leaf label still leads the text (a bare "Ontario" query stays correct).
        assert on_en["text"].index("Ontario") < on_en["text"].index("Geographic areas")

    def test_context_is_per_language_not_smeared(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        on_fr = next(r for r in items if r["lang"] == "fr" and r["code_id"] == "ON")
        # Each language gets its OWN-language context, with no cross-leak.
        assert "Régions géographiques" in on_fr["text"]
        assert "Régions géographiques" not in on_en["text"]
        assert "Geographic areas" not in on_fr["text"]

    def test_context_suppressed_when_types_empty(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts(context_annotation_types=())
        on_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        assert "Geographic areas" not in on_en["text"]
        # Base prefLabel + altLabel are unaffected.
        assert "Ontario" in on_en["text"]
        assert "Province of Ontario" in on_en["text"]

    def test_codes_without_context_annotation_unaffected(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        # CA carries no searchContext — its text is just the label path.
        ca_en = next(r for r in items if r["lang"] == "en" and r["code_id"] == "CA")
        assert "Canada" in ca_en["text"]
        assert "Geographic areas" not in ca_en["text"]


def _store_with_excluded_leaf() -> Catalog:
    """GEO codelist whose leaf ``DB1`` carries ``statcat:searchExclude``."""
    geo_cl = Codelist(
        id="CL_GEO_X",
        maintainer="TEST",
        name=_i("Geography"),
        codes=[
            Code(id="CA", name=_i("Canada")),
            Code(
                id="DB1",
                name=_i("10010165001"),
                parent_id="CA",
                annotations=Annotations([Annotation(type=SEARCH_EXCLUDE_ANNOTATION_TYPE)]),
            ),
        ],
    )
    dsd = DataStructure(
        id="DSD_X",
        maintainer="TEST",
        name=_i("DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=geo_cl)],
    )
    flow = Dataflow(id="DF_X", maintainer="TEST", name=_i("Flow", "Flux"), structure=dsd)
    registry = InMemoryRegistry()
    for artefact in (geo_cl, dsd, flow):
        registry.add(artefact)
    store = Catalog().open()
    ingest_sdmx(store, registry)
    return store


class TestSearchExclude:
    """statcat:searchExclude keeps a code out of the corpus, not out of the catalog."""

    def test_excluded_code_gets_no_rows_by_default(self) -> None:
        items = _store_with_excluded_leaf()._collect_code_texts()
        assert {r["code_id"] for r in items} == {"CA"}
        assert {r["lang"] for r in items} == {"en", "fr"}

    def test_exclusion_disabled_when_types_empty(self) -> None:
        items = _store_with_excluded_leaf()._collect_code_texts(exclude_annotation_types=())
        assert {r["code_id"] for r in items} == {"CA", "DB1"}

    def test_excluded_code_stays_in_the_structural_tables(self) -> None:
        store = _store_with_excluded_leaf()
        row = store._c.execute("SELECT parent_id FROM sdmx.code WHERE code_id = 'DB1'").fetchone()
        assert row == ("CA",)


class TestHierarchyFields:
    def test_parent_id_and_depth_populated_by_default(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts()
        ontario = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        canada = next(r for r in items if r["lang"] == "en" and r["code_id"] == "CA")
        assert ontario["parent_id"] == "CA"
        assert ontario["depth"] == 1
        assert canada["parent_id"] == ""
        assert canada["depth"] == 0

    def test_hierarchy_fields_can_be_suppressed(self) -> None:
        store = _store_with_sdmx()
        items = store._collect_code_texts(carry_hierarchy_fields=False)
        ontario = next(r for r in items if r["lang"] == "en" and r["code_id"] == "ON")
        assert ontario["parent_id"] == ""
        assert ontario["depth"] == 0


class TestCodesScopeHierarchyExposed:
    """cat.codes.find should expose the hierarchy fields the index stores."""

    def test_find_returns_parent_id(self, tmp_path: pathlib.Path) -> None:
        store = _store_with_sdmx(tmp_path)
        store.build_index(include_codes=True)
        result = store.codes.find("Ontario", lang="en", limit=5)
        ontario = next(h for h in result.items if h.payload.code_id == "ON")
        assert ontario.payload.parent_id == "CA"
        assert ontario.payload.depth == 1
        assert ontario.payload.code_urn.endswith("ON")
        assert ontario.payload.codelist_urn  # non-empty


class TestCodesScopeSemantic:
    def test_vector_mode_returns_results(self, tmp_path: pathlib.Path) -> None:
        store = _store_with_sdmx(tmp_path)
        model = _stub_model()
        store.build_index(embedding_model=model, include_codes=True, embed_codes=True)
        # Stub encoder is deterministic — any query yields some ranked
        # rows. We assert shape + that code rows come back, not relevance.
        result = store.codes.find("anything", mode="vector", lang="en", limit=10)
        assert result.items, "expected at least one semantic code hit"
        assert all(h.payload.code_id for h in result.items)
        # Score is finite and positive (1 / (1 + dist)).
        assert all(h.score > 0 for h in result.items)
        assert all(h.source == "vector" for h in result.items)

    def test_vector_mode_requires_model(self, tmp_path: pathlib.Path) -> None:
        store = _store_with_sdmx(tmp_path)
        with pytest.raises(RuntimeError, match="embedding_model"):
            store.codes.find("anything", mode="vector")


class TestCodesScopeHybrid:
    def test_hybrid_mode_returns_results(self, tmp_path: pathlib.Path) -> None:
        store = _store_with_sdmx(tmp_path)
        model = _stub_model()
        store.build_index(embedding_model=model, include_codes=True, embed_codes=True)
        result = store.codes.find("Canada", mode="hybrid", lang="en", limit=10)
        assert result.items, "expected at least one hybrid code hit"
        assert all(h.payload.code_id for h in result.items)
        # Canada should land somewhere in the top results since BM25
        # matches the prefLabel exactly.
        assert any(h.payload.code_id == "CA" for h in result.items)

    def test_hybrid_dedup_by_code_urn(self, tmp_path: pathlib.Path) -> None:
        """A code that wins on both sides must appear exactly once."""
        store = _store_with_sdmx(tmp_path)
        store.build_index(embedding_model=_stub_model(), include_codes=True, embed_codes=True)
        result = store.codes.find("Canada", mode="hybrid", lang="en", limit=10)
        urns = [h.payload.code_urn for h in result.items if h.payload.code_urn]
        assert len(urns) == len(set(urns))

    def test_hybrid_mode_requires_model(self, tmp_path: pathlib.Path) -> None:
        store = _store_with_sdmx(tmp_path)
        with pytest.raises(RuntimeError, match="embedding_model"):
            store.codes.find("anything", mode="hybrid")

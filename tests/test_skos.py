"""Tests for SKOS domain artefacts — ConceptScheme, Concept, ConceptRef, ConceptSchemeRef."""

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


SCHEME_URI = "urn:test:skos:themes"


# ---------------------------------------------------------------------------
# Pure-Python construction (no store imports)
# ---------------------------------------------------------------------------


class TestConceptScheme:
    def test_construction(self):
        scheme = sc.ConceptScheme(uri=SCHEME_URI, label=_i("Themes"))
        assert scheme.uri == SCHEME_URI
        assert scheme.label.get("en") == "Themes"
        assert scheme.concepts == {}
        assert scheme.top_concepts == ()

    def test_with_concepts(self):
        concept = sc.Concept(notation="ECO", label=_i("Economy"))
        scheme = sc.ConceptScheme(uri=SCHEME_URI, label=_i("Themes"), concepts={"ECO": concept})
        assert "ECO" in scheme.concepts
        assert scheme.concepts["ECO"].notation == "ECO"

    def test_immutable(self):
        scheme = sc.ConceptScheme(uri=SCHEME_URI, label=_i("Themes"))
        with pytest.raises((AttributeError, TypeError)):
            scheme.uri = "other"  # pyrefly: ignore[read-only]


class TestConcept:
    def test_construction(self):
        c = sc.Concept(notation="PUMF", label=_i("PUMF"))
        assert c.notation == "PUMF"
        assert c.label.get("en") == "PUMF"
        assert c.broader == ()
        assert c.narrower == ()
        assert c.related == ()

    def test_with_hierarchy(self):
        c = sc.Concept(notation="SUB", label=_i("Sub"), broader=("PARENT",), narrower=("CHILD",))
        assert "PARENT" in c.broader
        assert "CHILD" in c.narrower

    def test_with_alt_label_and_definition(self):
        c = sc.Concept(notation="ECO", label=_i("Economy"), alt_label=_i("Econ"), definition=_i("Economic activity"))
        assert c.alt_label.get("en") == "Econ"
        assert c.definition is not None
        assert c.definition.get("en") == "Economic activity"

    def test_immutable(self):
        c = sc.Concept(notation="A", label=_i("A"))
        with pytest.raises((AttributeError, TypeError)):
            c.notation = "B"  # pyrefly: ignore[read-only]


class TestConceptRef:
    def test_construction_uri_only(self):
        ref = sc.ConceptRef(uri="https://example.com/c/1")
        assert ref.uri == "https://example.com/c/1"
        assert ref.notation is None
        assert ref.scheme_uri is None
        assert ref.label == InternationalString()

    def test_equality_by_uri(self):
        ref1 = sc.ConceptRef(uri="https://example.com/c/1", label=_i("Economy"))
        ref2 = sc.ConceptRef(uri="https://example.com/c/1")
        assert ref1 == ref2

    def test_inequality_different_uri(self):
        ref1 = sc.ConceptRef(uri="https://example.com/c/1")
        ref2 = sc.ConceptRef(uri="https://example.com/c/2")
        assert ref1 != ref2

    def test_with_hints(self):
        ref = sc.ConceptRef(
            uri="urn:test:themes/ECO",
            notation="ECO",
            scheme_uri="urn:test:themes",
            label=_i("Economy"),
        )
        assert ref.notation == "ECO"
        assert ref.scheme_uri == "urn:test:themes"
        assert ref.label.get("en") == "Economy"

    def test_equality_ignores_metadata(self):
        ref1 = sc.ConceptRef(uri="urn:test:x", notation="ECO", scheme_uri="urn:test:s")
        ref2 = sc.ConceptRef(uri="urn:test:x")
        assert ref1 == ref2


class TestConceptSchemeRef:
    def test_construction(self):
        ref = sc.ConceptSchemeRef(uri="urn:test:scheme")
        assert ref.uri == "urn:test:scheme"
        assert ref.label == InternationalString()

    def test_equality_by_uri(self):
        ref1 = sc.ConceptSchemeRef(uri="urn:test:scheme", label=_i("Themes"))
        ref2 = sc.ConceptSchemeRef(uri="urn:test:scheme")
        assert ref1 == ref2


class TestIsConceptInScheme:
    def test_found(self):
        scheme = sc.ConceptScheme(
            uri=SCHEME_URI,
            label=_i("T"),
            concepts={"ECO": sc.Concept(notation="ECO", label=_i("Economy"))},
        )
        assert sc.is_concept_in_scheme(scheme, "ECO") is True

    def test_not_found(self):
        scheme = sc.ConceptScheme(uri=SCHEME_URI, label=_i("T"))
        assert sc.is_concept_in_scheme(scheme, "NONEXISTENT") is False


# ---------------------------------------------------------------------------
# Store persistence round-trips
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> sc.Catalog:
    return sc.Catalog().open()


def _make_scheme(uri: str = SCHEME_URI) -> sc.ConceptScheme:
    return sc.ConceptScheme(
        uri=uri,
        label=_i("Test Themes"),
        concepts={
            "ECO": sc.Concept(
                notation="ECO",
                label=_i("Economy"),
                alt_label=_i("Econ"),
                broader=(),
                narrower=("ECO-TRADE",),
            ),
            "ECO-TRADE": sc.Concept(
                notation="ECO-TRADE",
                label=_i("Trade"),
                broader=("ECO",),
            ),
            "LAB": sc.Concept(notation="LAB", label=_i("Labour")),
        },
        top_concepts=("ECO", "LAB"),
    )


class TestPutGetConceptScheme:
    def test_round_trip(self, store):
        scheme = _make_scheme()
        store.put(scheme)
        result = store.get_concept_scheme(SCHEME_URI)
        assert result is not None
        assert result.uri == SCHEME_URI
        assert result.label.get("en") == "Test Themes"

    def test_concepts_populated(self, store):
        store.put(_make_scheme())
        result = store.get_concept_scheme(SCHEME_URI)
        assert result is not None
        assert "ECO" in result.concepts
        assert "LAB" in result.concepts
        assert result.concepts["ECO"].label.get("en") == "Economy"

    def test_top_concepts(self, store):
        store.put(_make_scheme())
        result = store.get_concept_scheme(SCHEME_URI)
        assert result is not None
        assert "ECO" in result.top_concepts
        assert "LAB" in result.top_concepts

    def test_hierarchy_relations(self, store):
        store.put(_make_scheme())
        result = store.get_concept_scheme(SCHEME_URI)
        assert result is not None
        eco = result.concepts["ECO"]
        assert "ECO-TRADE" in eco.narrower
        trade = result.concepts["ECO-TRADE"]
        assert "ECO" in trade.broader

    def test_get_nonexistent_returns_none(self, store):
        result = store.get_concept_scheme("urn:nonexistent")
        assert result is None

    def test_idempotent_put(self, store):
        scheme = _make_scheme()
        store.put(scheme)
        store.put(scheme)  # must not raise
        result = store.get_concept_scheme(SCHEME_URI)
        assert result is not None
        assert len(result.concepts) == 3


class TestGetConcept:
    def test_round_trip(self, store):
        store.put(_make_scheme())
        concept = store.get_concept(f"{SCHEME_URI}/ECO")
        assert concept is not None
        assert concept.notation == "ECO"
        assert concept.label.get("en") == "Economy"

    def test_nonexistent_returns_none(self, store):
        assert store.get_concept("urn:nonexistent/NOPE") is None


class TestDatasetWithConceptRefs:
    def test_themes_as_concept_refs(self, store):
        store.put(_make_scheme())
        eco_uri = f"{SCHEME_URI}/ECO"
        ds = sc.OpenDataset(
            identifier="DS1",
            title=_i("Test"),
            description=_i("Desc"),
            themes=[sc.ConceptRef(uri=eco_uri, notation="ECO", scheme_uri=SCHEME_URI)],
        )
        store.put(ds)
        retrieved = store.get(sc.OpenDataset, "DS1")
        assert retrieved is not None
        assert len(retrieved.themes) == 1
        assert retrieved.themes[0].uri == eco_uri
        assert retrieved.themes[0].notation == "ECO"

    def test_put_dataset_does_not_write_concept_tables(self, store):
        """Permissive refs — dataset.put() never creates concept/concept_scheme rows."""
        ds = sc.OpenDataset(
            identifier="DS2",
            title=_i("Test"),
            description=_i("Desc"),
            themes=[sc.ConceptRef(uri="urn:external:concept/FOO")],
        )
        store.put(ds)
        row = store.connection.execute("SELECT COUNT(*) FROM statcat.concept").fetchone()
        assert row[0] == 0

    def test_subject_refs(self, store):
        store.put(_make_scheme())
        lab_uri = f"{SCHEME_URI}/LAB"
        ds = sc.OpenDataset(
            identifier="DS3",
            title=_i("Test"),
            description=_i("Desc"),
            subject=[sc.ConceptRef(uri=lab_uri, notation="LAB", scheme_uri=SCHEME_URI)],
        )
        store.put(ds)
        retrieved = store.get(sc.OpenDataset, "DS3")
        assert retrieved is not None
        assert len(retrieved.subject) == 1
        assert retrieved.subject[0].uri == lab_uri

    def test_dataset_type_ref(self, store):
        ds = sc.OpenDataset(
            identifier="DS4",
            title=_i("Test"),
            description=_i("Desc"),
            dataset_type=sc.ConceptRef(uri="urn:test:type/SURVEY"),
        )
        store.put(ds)
        retrieved = store.get(sc.OpenDataset, "DS4")
        assert retrieved is not None
        assert retrieved.dataset_type is not None
        assert retrieved.dataset_type.uri == "urn:test:type/SURVEY"

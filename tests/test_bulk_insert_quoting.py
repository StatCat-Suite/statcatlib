"""Bulk bridge inserts store quotes as given (GH-89).

The bridge writes for themes, subjects, keywords, spatial coverage, top
concepts and concept relations batch their rows into one multi-row
``VALUES``. They once formatted each value as a SQL literal and left MAP keys
unescaped, so a key holding ``'`` broke the insert. Every value is now bound
as a parameter.
"""

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit

#: Ends the literal it sits in, then tries a second statement.
QUOTED = "l'été'); DROP TABLE dataset; --"


def _i(text: str) -> InternationalString:
    return InternationalString(en=text, fr=text)


@pytest.fixture
def store() -> sc.Catalog:
    return sc.Catalog().open()


def _dataset() -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=f"DS-{QUOTED}",
        title=_i("Quotes"),
        description=_i("Every bridge value holds a quote"),
        keywords=[_i(QUOTED), _i("plain")],
        themes=[sc.ConceptRef(uri=f"urn:theme:{QUOTED}")],
        subject=[sc.ConceptRef(uri=f"urn:subject:{QUOTED}")],
        spatial_coverage=[
            sc.SpatialCoverage(label=_i(QUOTED), uri=f"urn:geo:{QUOTED}", bbox=(-141.0, 41.7, -52.6, 83.1)),
            sc.SpatialCoverage(label=_i("no box")),
        ],
    )


def test_dataset_bridge_values_round_trip(store: sc.Catalog) -> None:
    store.put(_dataset())
    got = store.get(sc.OpenDataset, f"DS-{QUOTED}")
    assert got is not None
    assert [kw.get("en") for kw in got.keywords] == [QUOTED, "plain"]
    assert [t.uri for t in got.themes] == [f"urn:theme:{QUOTED}"]
    assert [s.uri for s in got.subject] == [f"urn:subject:{QUOTED}"]
    assert [c.label for c in got.spatial_coverage] == [_i(QUOTED), _i("no box")]
    assert got.spatial_coverage[0].uri == f"urn:geo:{QUOTED}"
    assert got.spatial_coverage[0].bbox == (-141.0, 41.7, -52.6, 83.1)
    assert got.spatial_coverage[1].bbox is None


def test_rewriting_a_dataset_replaces_its_bridge_rows(store: sc.Catalog) -> None:
    store.put(_dataset())
    store.put(sc.OpenDataset(identifier=f"DS-{QUOTED}", title=_i("Quotes"), description=_i("Emptied")))
    got = store.get(sc.OpenDataset, f"DS-{QUOTED}")
    assert got is not None
    assert (got.keywords, got.themes, got.subject, got.spatial_coverage) == ([], [], [], [])


def test_concept_scheme_values_round_trip(store: sc.Catalog) -> None:
    scheme_uri = f"urn:scheme:{QUOTED}"
    store.put(
        sc.ConceptScheme(
            uri=scheme_uri,
            label=_i("Scheme"),
            concepts={
                QUOTED: sc.Concept(notation=QUOTED, label=_i(QUOTED), narrower=("CHILD",)),
                "CHILD": sc.Concept(notation="CHILD", label=_i("Child"), broader=(QUOTED,)),
            },
            top_concepts=(QUOTED,),
        )
    )
    got = store.get_concept_scheme(scheme_uri)
    assert got is not None
    assert got.top_concepts == (QUOTED,)
    assert got.concepts[QUOTED].narrower == ("CHILD",)
    assert got.concepts["CHILD"].broader == (QUOTED,)


def test_a_map_key_holding_a_quote_is_stored_as_given(store: sc.Catalog) -> None:
    # sdmxlib's normalize_tag will refuse such a tag (StatCat-Suite/sdmxlib#280),
    # so the key reaches the helper directly: the binding, not the tag rule,
    # is what keeps it out of the SQL.
    store._insert_rows(
        "INSERT INTO dataset_keyword (dataset_id, keyword, ordinal)",
        [("DS-KEY", {QUOTED: "value"}, 0)],
    )
    rows = store._c.execute("SELECT keyword FROM dataset_keyword WHERE dataset_id = 'DS-KEY'").fetchall()
    assert rows == [({QUOTED: "value"},)]

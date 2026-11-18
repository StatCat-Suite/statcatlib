"""Request language tags go through `sdmxlib.normalize_tag` on entry, so lookups compare exactly.

sdmxlib 0.79 stores every tag lowercase. A caller may still write ``fr-CA`` or
``FR``: each public ``lang`` parameter normalises it once, and a malformed
tag raises `sdmxlib.InvalidTagError`.
"""

import pytest
import sdmxlib
from sdmxlib.model.codelist import Code, Codelist

import statcatlib as sc
import statcatlib.query as q
from tests.conftest import ScratchStore, open_scratch

pytestmark = pytest.mark.unit

_GEO = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"


@pytest.fixture
def store() -> ScratchStore:
    cat = open_scratch()
    cat.put(
        sc.OpenDataset(
            identifier="d1",
            title=sc.InternationalString(
                **{"en": "Population", "fr": "Population totale", "fr-CA": "Population du Québec"}
            ),
            description=sc.InternationalString(en="People"),
        )
    )
    cat.registry.add(
        Codelist(
            id="CL_GEO",
            maintainer="TEST",
            name=sc.InternationalString(en="Geography"),
            codes=[Code(id="CA", name=sc.InternationalString(en="Canada", fr="Canada (fr)"))],
        )
    )
    return cat


def test_a_mixed_case_region_tag_finds_the_lowercase_label(store: ScratchStore) -> None:
    rows = store.query(sc.OpenDataset).select("title", lang="fr-CA").rows()
    assert [row.title for row in rows] == ["Population du Québec"]


def test_an_uppercase_tag_finds_the_lowercase_label(store: ScratchStore) -> None:
    assert store.query(sc.OpenDataset).filter(q.title_contains("totale", lang="FR")).count() == 1
    assert store.query(sc.OpenDataset).scalars("title", lang="FR").values() == ["Population totale"]


def test_a_malformed_tag_raises(store: ScratchStore) -> None:
    with pytest.raises(sdmxlib.InvalidTagError):
        store.query(sc.OpenDataset).select("title", lang="fr_CA!")
    with pytest.raises(sdmxlib.InvalidTagError):
        q.title_contains("x", lang="fr_CA!")
    with pytest.raises(sdmxlib.InvalidTagError):
        store.codelist(_GEO).batch_labels(["CA"], lang="fr_CA!")


def test_batch_labels_normalise_the_tag(store: ScratchStore) -> None:
    assert dict(store.codelist(_GEO).batch_labels(["CA"], lang="EN")) == {"CA": sdmxlib.LocalisedString("Canada", "en")}
    assert dict(store.codelist(_GEO).batch_labels(["CA"], lang="FR")) == {
        "CA": sdmxlib.LocalisedString("Canada (fr)", "fr")
    }

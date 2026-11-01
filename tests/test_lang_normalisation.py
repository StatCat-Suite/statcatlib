"""Request language tags go through `sdmxlib.normalize_tag` on entry, so lookups compare exactly.

sdmxlib 0.79 stores every tag lowercase. A caller may still write ``fr-CA`` or
``FR``: each public ``lang`` parameter and ``.lang()`` setter normalises it
once, and a malformed tag raises `sdmxlib.InvalidTagError`.
"""

import pytest
import sdmxlib
from sdmxlib.model.codelist import Code, Codelist

import statcatlib as sc
import statcatlib.query as q

pytestmark = pytest.mark.unit

_GEO = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"


@pytest.fixture
def store() -> sc.Catalog:
    cat = sc.Catalog().open()
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


def test_a_mixed_case_region_tag_finds_the_lowercase_label(store: sc.Catalog) -> None:
    rows = store.query(sc.OpenDataset).select("title", lang="fr-CA").rows()
    assert [row.title for row in rows] == ["Population du Québec"]


def test_an_uppercase_tag_finds_the_lowercase_label(store: sc.Catalog) -> None:
    assert store.query(sc.OpenDataset).filter(q.title_contains("totale", lang="FR")).count() == 1
    assert store.query(sc.OpenDataset).scalars("title", lang="FR").values() == ["Population totale"]


def test_a_malformed_tag_raises(store: sc.Catalog) -> None:
    with pytest.raises(sdmxlib.InvalidTagError):
        store.query(sc.OpenDataset).select("title", lang="fr_CA!")
    with pytest.raises(sdmxlib.InvalidTagError):
        q.title_contains("x", lang="fr_CA!")
    with pytest.raises(sdmxlib.InvalidTagError):
        store.codelist(_GEO).lang("fr_CA!")


def test_the_lang_setter_normalises_as_the_parameter_does(store: sc.Catalog) -> None:
    by_setter = store.codelist(_GEO).lang("EN").batch_labels(["CA"])
    by_parameter = store.codelist(_GEO).batch_labels(["CA"], lang="en")
    assert by_setter == by_parameter == {"CA": "Canada"}
    assert store.codelist(_GEO).lang("FR").batch_labels(["CA"]) == {"CA": "Canada (fr)"}

"""Agents and contact points are identified without a language (GH-94).

An agent is stored under its own ``id``; a contact point under its email,
URL or phone, and only otherwise under every language of its name.
"""

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


def _i(**texts: str) -> InternationalString:
    return InternationalString(**texts)


def _dataset(
    *,
    publisher: sc.Agent | None = None,
    creator: sc.Agent | None = None,
    contact_point: sc.ContactPoint | None = None,
) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier="DS",
        title=_i(en="t"),
        description=_i(en="d"),
        publisher=publisher,
        creator=creator,
        contact_point=contact_point,
    )


@pytest.fixture
def store() -> sc.Catalog:
    return sc.Catalog().open()


@pytest.mark.parametrize("role", ["publisher", "creator"])
def test_an_agent_without_an_id_is_refused(store: sc.Catalog, role: str) -> None:
    agent = sc.Agent(name=_i(en="Statistics Canada"))
    ds = _dataset(publisher=agent) if role == "publisher" else _dataset(creator=agent)
    with pytest.raises(ValueError, match=f"the dataset's {role} has no id"):
        store.put(ds)


def test_a_refused_creator_leaves_no_publisher_row(store: sc.Catalog) -> None:
    ds = _dataset(publisher=sc.Agent(id="STC", name=_i(en="StatCan")), creator=sc.Agent(name=_i(en="Someone")))
    with pytest.raises(ValueError, match="creator"):
        store.put(ds)
    assert store._c.execute("SELECT count(*) FROM agent").fetchone() == (0,)


def test_an_agent_is_stored_under_its_id(store: sc.Catalog) -> None:
    store.put(_dataset(publisher=sc.Agent(id="STC", name=_i(fr="Statistique Canada"))))
    got = store.get(sc.OpenDataset, "DS")
    assert got is not None
    assert got.publisher == sc.Agent(id="STC", name=_i(fr="Statistique Canada"))


def _contact_ids(store: sc.Catalog, *contacts: sc.ContactPoint) -> list[str]:
    ids = []
    for contact in contacts:
        store.put(_dataset(contact_point=contact))
        row = store._c.execute("SELECT contact_id FROM dataset WHERE identifier = 'DS'").fetchone()
        assert row is not None
        ids.append(row[0])
    return ids


def test_a_contact_with_an_email_keeps_its_id_when_its_name_changes(store: sc.Catalog) -> None:
    first, second = _contact_ids(
        store,
        sc.ContactPoint(email="info@example.org", name=_i(en="Client Services")),
        sc.ContactPoint(email="info@example.org", name=_i(en="Client Services", fr="Services aux clients")),
    )
    assert first == second


def test_a_name_only_contact_is_identified_by_every_language(store: sc.Catalog) -> None:
    en_only, fr_only, both, both_reordered = _contact_ids(
        store,
        sc.ContactPoint(name=_i(en="Help")),
        sc.ContactPoint(name=_i(fr="Help")),
        sc.ContactPoint(name=_i(en="Help", fr="Aide")),
        sc.ContactPoint(name=_i(fr="Aide", en="Help")),
    )
    assert len({en_only, fr_only, both}) == 3
    assert both == both_reordered


def test_a_contact_with_nothing_to_identify_it_is_refused(store: sc.Catalog) -> None:
    with pytest.raises(ValueError, match="email, url, phone or name"):
        store.put(_dataset(contact_point=sc.ContactPoint()))

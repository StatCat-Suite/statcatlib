"""Tests for generalized entity queries — Agent, DataService, Distribution.

Verifies that store.query() works for non-dataset types and that field
filters map correctly to the entity tables.
"""

from __future__ import annotations

import re

import pytest
from sdmxlib import InternationalString

import statcatlib as sc
from tests.conftest import ScratchStore, open_scratch

pytestmark = pytest.mark.unit


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


# ---------------------------------------------------------------------------
# Fixture — a store with agents, data services, and distributions
# ---------------------------------------------------------------------------


@pytest.fixture
def entity_store() -> ScratchStore:
    s = open_scratch()

    ecb = sc.OpenDataset(
        identifier="DS-ECB",
        title=_i("ECB Dataset"),
        description=_i("A dataset from ECB"),
        publisher=sc.Agent(id="ECB", name=_i("European Central Bank"), type="organization"),
        distributions=[
            sc.Distribution(
                id="DIST-ECB-CSV",
                access_url="https://ecb.europa.eu/data.csv",
                media_type="text/csv",
                format="CSV",
            ),
            sc.Distribution(
                id="DIST-ECB-JSON",
                access_url="https://ecb.europa.eu/data.json",
                media_type="application/json",
                format="JSON",
            ),
        ],
    )

    estat = sc.OpenDataset(
        identifier="DS-ESTAT",
        title=_i("Eurostat Dataset"),
        description=_i("A dataset from Eurostat"),
        publisher=sc.Agent(id="ESTAT", name=_i("Eurostat"), type="organization"),
        distributions=[
            sc.Distribution(
                id="DIST-ESTAT-CSV",
                access_url="https://ec.europa.eu/eurostat/data.csv",
                media_type="text/csv",
                format="CSV",
            ),
        ],
    )

    oecd = sc.OpenDataset(
        identifier="DS-OECD",
        title=_i("OECD Dataset"),
        description=_i("A dataset from OECD"),
        publisher=sc.Agent(id="OECD", name=_i("OECD"), type="igo"),
    )

    s.put([ecb, estat, oecd])
    return s


# ---------------------------------------------------------------------------
# Agent queries
# ---------------------------------------------------------------------------


class TestAgentQuery:
    def test_query_all_agents(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Agent).execute()
        assert result.total == 3

    def test_query_agent_by_type(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Agent).filter(type="organization").execute()
        assert result.total == 2

    def test_query_agent_by_id(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Agent).filter(id="ECB").execute()
        assert result.total == 1
        assert result.items[0].id == "ECB"

    def test_query_agent_by_name_is_refused(self, entity_store: ScratchStore) -> None:
        """An agent is identified by its id; a name is text in some language (LL-D6)."""
        with pytest.raises(ValueError, match=r"'name'.*'homepage', 'id', 'type'"):
            entity_store.query(sc.Agent).filter(name="Eurostat").execute()

    def test_query_agent_no_match(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Agent).filter(id="NONEXISTENT").execute()
        assert result.total == 0
        assert result.items == []

    def test_agent_roundtrip_fields(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Agent).filter(id="ECB").execute()
        agent = result.items[0]
        assert isinstance(agent, sc.Agent)
        assert agent.id == "ECB"
        assert agent.name.as_dict().get("en") == "European Central Bank"
        assert agent.type == "organization"

    def test_all_terminal_method(self, entity_store: ScratchStore) -> None:
        agents = entity_store.query(sc.Agent).filter(type="organization").all()
        assert isinstance(agents, list)
        assert len(agents) == 2

    def test_first_terminal_method(self, entity_store: ScratchStore) -> None:
        agent = entity_store.query(sc.Agent).filter(id="ECB").first()
        assert agent is not None
        assert agent.id == "ECB"

    def test_first_returns_none_on_empty(self, entity_store: ScratchStore) -> None:
        assert entity_store.query(sc.Agent).filter(id="MISSING").first() is None

    def test_count_terminal_method(self, entity_store: ScratchStore) -> None:
        n = entity_store.query(sc.Agent).filter(type="organization").count()
        assert n == 2


# ---------------------------------------------------------------------------
# Distribution queries
# ---------------------------------------------------------------------------


class TestDistributionQuery:
    def test_query_all_distributions(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).execute()
        assert result.total == 3

    def test_query_by_format(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).filter(format="CSV").execute()
        assert result.total == 2

    def test_query_by_media_type(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).filter(media_type="application/json").execute()
        assert result.total == 1
        assert result.items[0].format == "JSON"

    def test_query_by_dataset_id(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).filter(dataset_id="DS-ECB").execute()
        assert result.total == 2

    def test_distribution_roundtrip_fields(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).filter(id="DIST-ECB-CSV").execute()
        assert result.total == 1
        dist = result.items[0]
        assert isinstance(dist, sc.Distribution)
        assert dist.id == "DIST-ECB-CSV"
        assert dist.media_type == "text/csv"
        assert dist.format == "CSV"

    def test_distribution_no_match(self, entity_store: ScratchStore) -> None:
        result = entity_store.query(sc.Distribution).filter(format="SDMX-ML").execute()
        assert result.total == 0

    def test_limit_and_offset(self, entity_store: ScratchStore) -> None:
        all_result = entity_store.query(sc.Distribution).execute()
        limited = entity_store.query(sc.Distribution).limit(2).execute()
        assert len(limited.items) == 2
        assert limited.total == all_result.total


class TestUnknownFilterKeys:
    """An entity query refuses a key its spec does not list (``docs/design/label-lookup.md``, LL-D6)."""

    @pytest.mark.parametrize(
        ("cls", "known"),
        [
            (sc.Agent, "'homepage', 'id', 'type'"),
            (sc.DataService, "'endpoint_url', 'id'"),
            (sc.Distribution, "'access_url', 'dataset_id', 'format', 'id', 'media_type'"),
        ],
    )
    def test_an_unknown_key_raises_naming_the_known_ones(
        self, entity_store: ScratchStore, cls: type, known: str
    ) -> None:
        with pytest.raises(ValueError, match=re.escape(f"has no filter 'nope'; the filters are [{known}]")):
            entity_store.query(cls).filter(nope="x").execute()


class TestAgentLabels:
    """``agent_labels`` returns every agent by id, with a gap for each missing name."""

    def test_every_agent_in_id_order_with_its_tag(self, entity_store: ScratchStore) -> None:
        labels = entity_store.agent_labels(lang="en")
        assert list(labels) == sorted(labels)
        assert labels["ECB"] == sc.LocalisedString("European Central Bank", "en")
        assert labels.gaps == ()

    def test_a_name_missing_in_the_tags_is_none_and_a_gap(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        store = open_scratch(tmp_path_factory.mktemp("agents") / "catalog")
        for ident, name in (("A", {"en": "Alpha"}), ("B", {"en": "Beta", "fr": "Bêta"})):
            store.put(
                sc.OpenDataset(
                    identifier=f"d-{ident}",
                    title=_i("t"),
                    description=_i("d"),
                    publisher=sc.Agent(id=ident, name=InternationalString.of(name)),
                )
            )
        labels = store.agent_labels(lang="fr")
        assert dict(labels) == {"A": None, "B": sc.LocalisedString("Bêta", "fr")}
        assert labels.gaps == (sc.Gap(key="A", entity="A", field="name", langs=("fr",)),)
        assert store.agent_labels(lang=("de", "fr"))["B"] == sc.LocalisedString("Bêta", "fr")


class TestConceptLabelGaps:
    def test_a_concept_missing_the_tag_is_a_gap(self, tmp_path_factory: pytest.TempPathFactory) -> None:
        store = open_scratch(tmp_path_factory.mktemp("concepts") / "catalog")
        store.put_concept("urn:s", sc.Concept(notation="a", label=InternationalString(en="Alpha")))
        store.put_concept("urn:s", sc.Concept(notation="b", label=InternationalString(en="Beta", fr="Bêta")))
        labels = store.concept_labels(lang="fr", scheme_uri="urn:s")
        assert dict(labels) == {"urn:s/a": None, "urn:s/b": sc.LocalisedString("Bêta", "fr")}
        assert labels.gaps == (sc.Gap(key="urn:s/a", entity="urn:s/a", field="label", langs=("fr",)),)

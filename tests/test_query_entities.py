"""Tests for generalized entity queries — Agent, DataService, Distribution.

Verifies that store.query() works for non-dataset types and that field
filters map correctly to the entity tables.
"""

from __future__ import annotations

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


# ---------------------------------------------------------------------------
# Fixture — a store with agents, data services, and distributions
# ---------------------------------------------------------------------------


@pytest.fixture
def entity_store() -> sc.Catalog:
    s = sc.Catalog().open()

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
    def test_query_all_agents(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).execute()
        assert result.total == 3

    def test_query_agent_by_type(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).filter(type="organization").execute()
        assert result.total == 2

    def test_query_agent_by_id(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).filter(id="ECB").execute()
        assert result.total == 1
        assert result.items[0].id == "ECB"

    def test_query_agent_by_name(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).filter(name="Eurostat").execute()
        assert result.total == 1
        assert result.items[0].id == "ESTAT"

    def test_query_agent_no_match(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).filter(id="NONEXISTENT").execute()
        assert result.total == 0
        assert result.items == []

    def test_agent_roundtrip_fields(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Agent).filter(id="ECB").execute()
        agent = result.items[0]
        assert isinstance(agent, sc.Agent)
        assert agent.id == "ECB"
        assert agent.name.get("en") == "European Central Bank"
        assert agent.type == "organization"

    def test_all_terminal_method(self, entity_store: sc.Catalog) -> None:
        agents = entity_store.query(sc.Agent).filter(type="organization").all()
        assert isinstance(agents, list)
        assert len(agents) == 2

    def test_first_terminal_method(self, entity_store: sc.Catalog) -> None:
        agent = entity_store.query(sc.Agent).filter(id="ECB").first()
        assert agent is not None
        assert agent.id == "ECB"

    def test_first_returns_none_on_empty(self, entity_store: sc.Catalog) -> None:
        assert entity_store.query(sc.Agent).filter(id="MISSING").first() is None

    def test_count_terminal_method(self, entity_store: sc.Catalog) -> None:
        n = entity_store.query(sc.Agent).filter(type="organization").count()
        assert n == 2


# ---------------------------------------------------------------------------
# Distribution queries
# ---------------------------------------------------------------------------


class TestDistributionQuery:
    def test_query_all_distributions(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).execute()
        assert result.total == 3

    def test_query_by_format(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).filter(format="CSV").execute()
        assert result.total == 2

    def test_query_by_media_type(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).filter(media_type="application/json").execute()
        assert result.total == 1
        assert result.items[0].format == "JSON"

    def test_query_by_dataset_id(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).filter(dataset_id="DS-ECB").execute()
        assert result.total == 2

    def test_distribution_roundtrip_fields(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).filter(id="DIST-ECB-CSV").execute()
        assert result.total == 1
        dist = result.items[0]
        assert isinstance(dist, sc.Distribution)
        assert dist.id == "DIST-ECB-CSV"
        assert dist.media_type == "text/csv"
        assert dist.format == "CSV"

    def test_distribution_no_match(self, entity_store: sc.Catalog) -> None:
        result = entity_store.query(sc.Distribution).filter(format="SDMX-ML").execute()
        assert result.total == 0

    def test_limit_and_offset(self, entity_store: sc.Catalog) -> None:
        all_result = entity_store.query(sc.Distribution).execute()
        limited = entity_store.query(sc.Distribution).limit(2).execute()
        assert len(limited.items) == 2
        assert limited.total == all_result.total

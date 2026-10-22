"""Tests for cross-entity query filters — publishes() and serves().

These filters allow querying Agent and DataService objects based on the
datasets they are associated with, using nested EXISTS subqueries.
"""

from __future__ import annotations

import pytest
from sdmxlib import InternationalString
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry

import statcatlib as sc
import statcatlib.query as q
from statcatlib.store.store import _QueryContext
from tests.conftest import concept_of

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _store_with_agents() -> sc.Catalog:
    """Store with three agents publishing various datasets via SDMX ingest.

    - ECB: AggregateDataset with GEO=[CA, DE]
    - ESTAT: AggregateDataset with GEO=[FR, DE] and OpenDataset
    - OECD: OpenDataset only (no SDMX structure)

    Separate codelists are used so GEO code filters discriminate by publisher.
    """
    store = sc.Catalog().open()

    # ECB dataflow: CL_GEO_ECB = [CA, DE]
    cl_ecb = Codelist(
        id="CL_GEO_ECB",
        maintainer="ECB",
        name=_i("Geography ECB"),
        codes=[Code(id="CA", name=_i("Canada")), Code(id="DE", name=_i("Germany"))],
    )
    dsd_exr = DataStructure(
        id="DSD_EXR",
        maintainer="ECB",
        name=_i("Exchange Rate DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl_ecb)],
    )
    flow_exr = Dataflow(
        id="DF_EXR",
        maintainer="ECB",
        name=_i("Exchange Rate"),
        description=_i("Daily FX"),
        structure=dsd_exr,
    )

    # ESTAT dataflow: CL_GEO_ESTAT = [FR, DE]
    cl_estat = Codelist(
        id="CL_GEO_ESTAT",
        maintainer="ESTAT",
        name=_i("Geography ESTAT"),
        codes=[Code(id="FR", name=_i("France")), Code(id="DE", name=_i("Germany"))],
    )
    dsd_gdp = DataStructure(
        id="DSD_GDP",
        maintainer="ESTAT",
        name=_i("GDP DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl_estat)],
    )
    flow_gdp = Dataflow(
        id="DF_GDP",
        maintainer="ESTAT",
        name=_i("GDP"),
        description=_i("GDP data"),
        structure=dsd_gdp,
    )

    registry = InMemoryRegistry()
    for obj in [cl_ecb, dsd_exr, flow_exr, cl_estat, dsd_gdp, flow_gdp]:
        registry.add(obj)

    sc.ingest_sdmx(store, registry)

    # Add an open dataset for ESTAT and one for OECD (no SDMX structure)
    estat = sc.Agent(id="ESTAT", name=_i("Eurostat"))
    oecd = sc.Agent(id="OECD", name=_i("OECD"))
    store.put(sc.OpenDataset(identifier="ENV", title=_i("Environment"), description=_i("Env data"), publisher=estat))
    store.put(sc.OpenDataset(identifier="TRADE", title=_i("Trade"), description=_i("Trade data"), publisher=oecd))

    return store


def _store_with_services() -> sc.Catalog:
    """Store with DataService objects linked via distributions.

    - svc-api: serves EXR dataset (AggregateDataset with GEO=[CA])
    - svc-bulk: serves GDP dataset (AggregateDataset with GEO=[FR])
    - svc-static: not linked to any dataset
    """
    store = sc.Catalog().open()

    svc_api = sc.DataService(id="svc-api", title=_i("API"), endpoint_url="https://api.example.com")
    svc_bulk = sc.DataService(id="svc-bulk", title=_i("Bulk"), endpoint_url="https://bulk.example.com")

    # EXR dataflow: CL_GEO_API = [CA]
    cl_api = Codelist(
        id="CL_GEO_API",
        maintainer="TEST",
        name=_i("Geography API"),
        codes=[Code(id="CA", name=_i("Canada"))],
    )
    dsd_exr = DataStructure(
        id="DSD_EXR",
        maintainer="TEST",
        name=_i("Exchange Rate DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl_api)],
    )
    flow_exr = Dataflow(
        id="DF_EXR",
        maintainer="TEST",
        name=_i("Exchange Rate"),
        description=_i("Daily FX"),
        structure=dsd_exr,
    )

    # GDP dataflow: CL_GEO_BULK = [FR]
    cl_bulk = Codelist(
        id="CL_GEO_BULK",
        maintainer="TEST",
        name=_i("Geography Bulk"),
        codes=[Code(id="FR", name=_i("France"))],
    )
    dsd_gdp = DataStructure(
        id="DSD_GDP",
        maintainer="TEST",
        name=_i("GDP DSD"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl_bulk)],
    )
    flow_gdp = Dataflow(
        id="DF_GDP",
        maintainer="TEST",
        name=_i("GDP"),
        description=_i("GDP data"),
        structure=dsd_gdp,
    )

    registry = InMemoryRegistry()
    for obj in [cl_api, dsd_exr, flow_exr, cl_bulk, dsd_gdp, flow_gdp]:
        registry.add(obj)

    # Ingest structural tables and build datasets, then attach distributions
    import attrs

    datasets = sc.ingest_sdmx(store, registry)
    # ingest_sdmx already stored the datasets; evolve them with distributions and re-put
    ds_exr_base = next(d for d in datasets if "DF_EXR" in d.identifier)
    ds_gdp_base = next(d for d in datasets if "DF_GDP" in d.identifier)

    ds_exr = attrs.evolve(
        ds_exr_base,
        distributions=[
            sc.Distribution(
                id="dist-exr",
                access_url="https://api.example.com/exr",
                access_service=svc_api,
            )
        ],
    )
    ds_gdp = attrs.evolve(
        ds_gdp_base,
        distributions=[
            sc.Distribution(
                id="dist-gdp",
                access_url="https://bulk.example.com/gdp",
                access_service=svc_bulk,
            )
        ],
    )

    store.put([ds_exr, ds_gdp])
    # svc_static is added through a dataset with no distribution link — not linked
    store.put(sc.OpenDataset(identifier="STATIC", title=_i("Static"), description=_i("No service")))

    return store


# ---------------------------------------------------------------------------
# publishes() — Agent queries
# ---------------------------------------------------------------------------


class TestPublishesFilter:
    def test_publishes_any_dataset(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes()).all()
        ids = {a.id for a in result}
        # All three agents have at least one dataset
        assert "ECB" in ids
        assert "ESTAT" in ids
        assert "OECD" in ids

    def test_publishes_by_kind(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(kind="aggregate")).all()
        ids = {a.id for a in result}
        assert "ECB" in ids
        assert "ESTAT" in ids
        # OECD only publishes OpenDatasets
        assert "OECD" not in ids

    def test_publishes_with_code_filter(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(q.has_code("GEO", "CA"))).all()
        ids = {a.id for a in result}
        # Only ECB's codelist contains CA
        assert "ECB" in ids
        assert "ESTAT" not in ids
        assert "OECD" not in ids

    def test_publishes_with_shared_code(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(q.has_code("GEO", "DE"))).all()
        ids = {a.id for a in result}
        # Both ECB (CL_GEO_ECB) and ESTAT (CL_GEO_ESTAT) include DE
        assert "ECB" in ids
        assert "ESTAT" in ids
        assert "OECD" not in ids

    def test_publishes_with_kind_and_code(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(q.has_code("GEO", "FR"), kind="aggregate")).all()
        ids = {a.id for a in result}
        assert "ESTAT" in ids
        assert "ECB" not in ids
        assert "OECD" not in ids

    def test_publishes_no_match(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(q.has_code("GEO", "ZZ"))).all()
        assert result == []

    def test_result_type_is_agent(self) -> None:
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes()).all()
        assert all(isinstance(a, sc.Agent) for a in result)

    def test_combined_with_field_filter(self) -> None:
        """Combine publishes() with a flat-column filter (agent id)."""
        store = _store_with_agents()
        result = store.query(sc.Agent).filter(q.publishes(kind="aggregate")).filter(id="ECB").all()
        assert len(result) == 1
        assert result[0].id == "ECB"

    def test_multiple_publishes_filters(self) -> None:
        """Two publishes() calls require two separate datasets — AND semantics."""
        store = _store_with_agents()
        # ESTAT publishes both AggregateDataset (GDP) and OpenDataset (ENV)
        result = store.query(sc.Agent).filter(q.publishes(kind="aggregate")).filter(q.publishes(kind="open")).all()
        ids = {a.id for a in result}
        assert "ESTAT" in ids
        # ECB only publishes aggregate, OECD only open
        assert "ECB" not in ids
        assert "OECD" not in ids


# ---------------------------------------------------------------------------
# serves() — DataService queries
# ---------------------------------------------------------------------------


class TestServesFilter:
    def test_serves_any_dataset(self) -> None:
        store = _store_with_services()
        result = store.query(sc.DataService).filter(q.serves()).all()
        ids = {s.id for s in result}
        assert "svc-api" in ids
        assert "svc-bulk" in ids
        # svc-static has no distribution link
        assert "svc-static" not in ids

    def test_serves_with_code_filter(self) -> None:
        store = _store_with_services()
        result = store.query(sc.DataService).filter(q.serves(q.has_code("GEO", "CA"))).all()
        ids = {s.id for s in result}
        assert "svc-api" in ids
        assert "svc-bulk" not in ids

    def test_serves_by_kind(self) -> None:
        store = _store_with_services()
        result = store.query(sc.DataService).filter(q.serves(kind="aggregate")).all()
        ids = {s.id for s in result}
        assert "svc-api" in ids
        assert "svc-bulk" in ids

    def test_serves_no_match(self) -> None:
        store = _store_with_services()
        result = store.query(sc.DataService).filter(q.serves(q.has_code("GEO", "ZZZZ"))).all()
        assert result == []

    def test_result_type_is_data_service(self) -> None:
        store = _store_with_services()
        result = store.query(sc.DataService).filter(q.serves()).all()
        assert all(isinstance(s, sc.DataService) for s in result)


# ---------------------------------------------------------------------------
# SQL generation smoke tests
# ---------------------------------------------------------------------------


class TestFilterSqlGeneration:
    def test_publishes_sql_structure(self) -> None:
        f = q.publishes()
        sql, params = f.to_sql(_QueryContext(row_alias="d"))
        assert "EXISTS" in sql
        assert "dataset d" in sql
        assert "publisher_id = id" in sql
        assert params == []

    def test_publishes_with_kind_sql(self) -> None:
        f = q.publishes(kind="aggregate")
        sql, params = f.to_sql(_QueryContext(row_alias="d"))
        assert "d.kind = ?" in sql
        assert params == ["aggregate"]

    def test_publishes_nested_filter_sql(self) -> None:
        f = q.publishes(q.has_code("GEO", "ON"))
        sql, params = f.to_sql(_QueryContext(row_alias="d"))
        # has_code now routes through SDMX structural tables
        assert "dsd_component" in sql
        assert "GEO" in params
        assert "ON" in params

    def test_serves_sql_structure(self) -> None:
        f = q.serves()
        sql, _params = f.to_sql(_QueryContext(row_alias="d"))
        assert "EXISTS" in sql
        assert "dataset d" in sql
        assert "service_dataset" in sql

    def test_serves_nested_filter_sql(self) -> None:
        f = q.serves(q.has_dimensions(["GEO", "FREQ"]))
        sql, params = f.to_sql(_QueryContext(row_alias="d"))
        # has_dimensions now routes through SDMX structural tables
        assert "dsd_component" in sql
        assert "GEO" in params or "FREQ" in params

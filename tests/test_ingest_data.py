"""Integration tests for `ingest_data`: observations written, bound and read back (GH-29).

`ingest_data` used to store the pivoted frame while registering a binding that
describes the long layout, so reading the dataflow back raised a DuckDB
`BinderException`. The only test checked that the function was callable. These
tests run the whole path with no network: a stub source serves SDMX-CSV rows
through sdmxlib's own `scan_csv`, so the frame `ingest_data` stores is the
frame a `RestRegistry` would hand it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sdmxlib as sl
from sdmxlib import ObsQuery
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension, Measure, TimeDimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString
from sdmxlib.model.urn import SdmxUrn

import statcatlib as sc
from tests.conftest import concept_of

pytestmark = pytest.mark.integration

IDENTIFIER = "TEST:DF_OBS(1.0)"

CSV = (
    b"STRUCTURE,STRUCTURE_ID,ACTION,GEO,FREQ,TIME_PERIOD,OBS_VALUE\n"
    b"dataflow,TEST:DF_OBS(1.0),I,CA,A,2020,1.5\n"
    b"dataflow,TEST:DF_OBS(1.0),I,US,A,2020,2.5\n"
    b"dataflow,TEST:DF_OBS(1.0),I,CA,M,2020-01,3.5\n"
)


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _registry() -> tuple[InMemoryRegistry, SdmxUrn[Dataflow]]:
    """A dataflow whose DSD has two dimensions, a time dimension and a measure."""
    geo = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography"),
        codes=[Code(id="CA", name=_i("Canada")), Code(id="US", name=_i("United States"))],
    )
    freq = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency"),
        codes=[Code(id="A", name=_i("Annual")), Code(id="M", name=_i("Monthly"))],
    )
    dsd = DataStructure(
        id="DSD_OBS",
        maintainer="TEST",
        name=_i("Observations"),
        dimensions=[
            Dimension(id="GEO", concept=concept_of("GEO"), representation=geo),
            Dimension(id="FREQ", concept=concept_of("FREQ"), representation=freq),
        ],
        time_dimension=TimeDimension(id="TIME_PERIOD", concept=concept_of("TIME_PERIOD")),
        measures=[Measure(id="OBS_VALUE", concept=concept_of("OBS_VALUE"))],
    )
    flow = Dataflow(id="DF_OBS", maintainer="TEST", name=_i("Observation flow"), structure=dsd)
    registry = InMemoryRegistry()
    for artefact in (geo, freq, dsd, flow):
        registry.add(artefact)
    return registry, Dataflow.parse_urn(str(sl.artefact_urn(flow)))


class _CsvSource:
    """Serves `CSV` as the dataflow's observations, the way a `RestRegistry` serves a fetch."""

    def __init__(self, store: sc.Catalog) -> None:
        self._store = store

    def obs(self, flow: SdmxUrn[Dataflow], /) -> ObsQuery:
        assert flow.id == "DF_OBS"
        return self._store.registry.scan_csv(CSV)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[sc.Catalog, SdmxUrn[Dataflow]]]:
    registry, flow_urn = _registry()
    with sc.Catalog(tmp_path / "cat").open() as cat:
        sc.ingest_sdmx(cat, registry)
        yield cat, flow_urn


def test_observations_read_back_through_the_binding(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, flow_urn = store
    assert sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER) == "test.df_df_obs_1_0"
    frame = cat.registry.obs(flow_urn).frame().sort("GEO", "TIME_PERIOD")
    assert frame.columns == ["GEO", "FREQ", "TIME_PERIOD", "OBS_VALUE"]
    assert frame.rows() == [("CA", "A", "2020", 1.5), ("CA", "M", "2020-01", 3.5), ("US", "A", "2020", 2.5)]


def test_the_stored_table_is_long(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    """The binding names one column per component, so the table must carry exactly those."""
    cat, _ = store
    schema, table = sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER, measure_dim="FREQ").split(".")
    columns = cat.connection.execute(
        """
        SELECT column_name FROM duckdb_columns()
        WHERE database_name = 'lake' AND schema_name = $schema AND table_name = $table
        ORDER BY column_index
        """,
        {"schema": schema, "table": table},
    ).fetchall()
    assert [c[0] for c in columns] == ["GEO", "FREQ", "TIME_PERIOD", "OBS_VALUE"]


def test_a_filter_runs_through_the_binding(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, flow_urn = store
    sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER)
    obs = cat.registry.obs(flow_urn)
    assert obs.filter(obs.col("GEO") == "CA").frame().height == 2


def test_measure_dim_is_recorded_as_the_pivot_axis(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, flow_urn = store
    sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER, measure_dim="FREQ")
    ds = cat.get(sc.AggregateDataset, IDENTIFIER)
    assert ds is not None
    assert ds.measure_dim == "FREQ"
    wide = cat.registry.obs(flow_urn).pivot(on=ds.measure_dim).sort("GEO", "TIME_PERIOD")
    assert wide.columns == ["GEO", "TIME_PERIOD", "A", "M"]
    assert wide.rows() == [("CA", "2020", 1.5, None), ("CA", "2020-01", None, 3.5), ("US", "2020", 2.5, None)]


def test_a_measure_dim_outside_the_dsd_is_refused(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, _ = store
    with pytest.raises(ValueError, match="'SEX' is not a dimension of DSD_OBS: GEO, FREQ"):
        sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER, measure_dim="SEX")
    assert cat.connection.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'test'").fetchone() == (0,)


def test_a_rerun_replaces_the_table_and_the_binding(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    """The tutorial calls `ingest_data` idempotent: a second run leaves one table's worth of rows."""
    cat, flow_urn = store
    sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER)
    sc.ingest_data(cat, _CsvSource(cat), IDENTIFIER, measure_dim="FREQ")
    assert cat.registry.obs(flow_urn).frame().height == 3
    binding = cat.registry.binding_for(flow_urn)
    assert binding is not None
    assert binding.table_location == "test.df_df_obs_1_0"


def test_latest_resolves_to_the_stored_version(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, _ = store
    assert sc.ingest_data(cat, _CsvSource(cat), "TEST:DF_OBS(latest)") == "test.df_df_obs_1_0"


def test_a_missing_dataset_is_a_key_error(store: tuple[sc.Catalog, SdmxUrn[Dataflow]]) -> None:
    cat, _ = store
    with pytest.raises(KeyError, match="run ingest_sdmx first"):
        sc.ingest_data(cat, _CsvSource(cat), "TEST:NOPE(1.0)")

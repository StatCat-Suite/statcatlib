"""SDMX source connector for statcatlib.

This is the only statcatlib module that imports sdmxlib beyond InternationalString.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sdmxlib import InternationalString
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure

from statcatlib.model.agents import Agent
from statcatlib.model.dataset import AggregateDataset

if TYPE_CHECKING:
    from sdmxlib.api.registry import RestRegistry
    from sdmxlib.local.registry import LocalRegistry
    from sdmxlib.model.in_memory_registry import InMemoryRegistry

    from statcatlib.model.skos import ConceptRef
    from statcatlib.store.store import Catalog

__all__ = [
    "dataset_from_dataflow",
    "ingest_data",
    "ingest_sdmx",
    "ingest_sdmx_registry",
    "prepare_sdmx",
]

_IDENTIFIER_RE = re.compile(r"^([^:]+):([^(]+)\(([^)]+)\)$")


def _resolve_latest(store: Catalog, identifier: str) -> str:
    """Resolve ``(latest)`` to the highest stored version for that agency:id."""
    if not identifier.endswith("(latest)"):
        return identifier
    m = _IDENTIFIER_RE.match(identifier)
    if m is None:
        return identifier
    agency, flow_id = m.group(1), m.group(2)
    row = store._c.execute(  # noqa: SLF001
        """
        SELECT identifier FROM dataset
        WHERE identifier LIKE ?
        ORDER BY identifier DESC
        LIMIT 1
        """,
        [f"{agency}:{flow_id}(%"],
    ).fetchone()
    return row[0] if row else identifier


# ---------------------------------------------------------------------------
# Public bridge function
# ---------------------------------------------------------------------------


def _build_urn(flow: Dataflow) -> str:
    """Build a URN string for a dataflow using sdmxlib's SdmxUrn."""
    from sdmxlib.model.urn import SdmxUrn as _SdmxUrn  # noqa: PLC0415

    return str(
        _SdmxUrn(
            package=flow.sdmx_package,
            artefact_class=flow.sdmx_class,
            agency=flow.agency_id,
            id=flow.id,
            version=flow.version,
        )
    )


def dataset_from_dataflow(
    flow: Dataflow,
    dsd: DataStructure,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> AggregateDataset:
    """Create an AggregateDataset from sdmxlib Dataflow and DataStructure objects.

    Args:
        flow: The sdmxlib Dataflow to convert.
        dsd: The resolved DataStructure for this dataflow.
        publisher: Optional Agent override. When omitted, the flow's maintainer agency is used.
        themes: Optional list of ConceptRef objects to tag the dataset with.

    Returns:
        The catalog dataset. Code and codelist information is resolved at
        query time via the SDMX structural tables (``sdmx.dsd_component``,
        ``sdmx.code``) using the ``sdmx_dataflow_urn`` foreign key.
    """
    agency = flow.maintainer
    identifier = f"{agency.id}:{flow.id}({flow.version})"
    urn = _build_urn(flow)

    if publisher is None:
        name = agency.name if (agency.name.get("en") or agency.name.get("fr")) else InternationalString(en=agency.id)
        publisher = Agent(id=agency.id, name=name)

    dimension_names: list[InternationalString] = []

    for dim in dsd.dimensions:
        # Use the concept name if resolved; fall back to dimension ID.
        concept_ref = dim.concept
        if concept_ref is not None and concept_ref.resolved:
            resolved_concept = concept_ref()
            if hasattr(resolved_concept, "name"):
                label = resolved_concept.name
            else:
                label = InternationalString(en=dim.id, fr=dim.id)
        else:
            label = InternationalString(en=dim.id, fr=dim.id)
        dimension_names.append(label)

    return AggregateDataset(
        identifier=identifier,
        title=flow.name,
        description=flow.description,
        publisher=publisher,
        sdmx_dataflow_urn=urn,
        dimension_names=dimension_names,
        themes=themes or [],
    )


# ---------------------------------------------------------------------------
# Bulk ingestion
# ---------------------------------------------------------------------------


def prepare_sdmx(
    registry: InMemoryRegistry | LocalRegistry,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]:
    """Build AggregateDataset objects from a registry without storing.

    Callers can enrich each dataset before storing::

        datasets = prepare_sdmx(registry)
        ds = attrs.evolve(datasets[0], num_series=42)
        store.put(ds)

    Code and codelist information is resolved at query time via the SDMX
    structural tables once ``local.ingest(registry)`` has been called.

    *registry* can also be a `LocalRegistry` whose store already holds the
    structures, such as a bundle's: each flow's DSD is read from the store.
    """
    results: list[AggregateDataset] = []
    for flow in registry.get_all(Dataflow):
        structure_ref = flow.structure
        if structure_ref is None or not structure_ref.resolved:
            continue
        dsd = structure_ref()
        if not isinstance(dsd, DataStructure):
            continue
        ds = dataset_from_dataflow(flow, dsd, publisher=publisher, themes=themes)
        results.append(ds)
    return results


def ingest_sdmx(store: Catalog, registry: InMemoryRegistry) -> list[AggregateDataset]:
    """Ingest all SDMX artefacts from a registry into the catalog.

    Step 1: Write all structural artefacts (codelists, DSDs, dataflows) into
    the shared DuckDB connection via sdmxlib's LocalRegistry.

    Step 2: For each Dataflow in the registry, create an AggregateDataset via
    ``dataset_from_dataflow()`` and store it.  Code and codelist queries use
    the SDMX structural tables via ``sdmx_dataflow_urn``.

    Returns the list of AggregateDataset objects created.
    """
    local = store.registry
    local.ingest(registry)

    datasets = prepare_sdmx(registry)
    with store.bulk_load():
        store.put(datasets)
    return datasets


def ingest_sdmx_registry(
    store: Catalog,
    registry: InMemoryRegistry,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]:
    """Bulk ingest all dataflows from an sdmxlib LocalRegistry.

    Like ``ingest_sdmx()`` but exposes ``publisher`` and ``themes`` overrides
    that apply to every ingested dataset.
    """
    local = store.registry
    local.ingest(registry)

    datasets = prepare_sdmx(registry, publisher=publisher, themes=themes)
    with store.bulk_load():
        store.put(datasets)
    return datasets


def _query_dimension_names(conn: object, dataflow_urn: str) -> list[InternationalString]:
    """Query dimension names for a dataflow from the shared sdmx tables."""
    import duckdb  # noqa: PLC0415

    assert isinstance(conn, duckdb.DuckDBPyConnection)  # noqa: S101
    rows = conn.execute(
        """
        SELECT dc.component_id,
               con.name['en'],
               con.name['fr']
        FROM sdmx.dataflow df
        JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
        LEFT JOIN sdmx.concept con ON con.concept_urn = dc.concept_urn
        WHERE df.urn = ?
          AND dc.component_type = 'Dimension'
        ORDER BY dc.position
        """,
        [dataflow_urn],
    ).fetchall()
    result = []
    for component_id, name_en, name_fr in rows:
        en = name_en or component_id
        fr = name_fr or component_id
        result.append(InternationalString(en=en, fr=fr))
    return result


def ingest_data(
    store: Catalog,
    reg: RestRegistry,
    identifier: str,
    *,
    measure_dim: str | None = None,
) -> str:
    """Materialise observation data for one dataflow into the bundle's lake.

    Fetches the observations wide via ``reg.obs(...).pivot()``, writes them
    into ``lake.<agency>.df_<id>_<version>`` through the bundle's
    DuckLake-backed data store, and registers a
    `sdmxlib.DataflowBinding` so the dataflow is queryable through
    ``cat.observations(...)``.

    Requires that ``ingest_sdmx`` has already been called for this
    identifier so the catalog record exists, and that ``store`` is a
    folder-mode `Catalog` (a DuckLake is ATTACHed on ``open()``).

    Args:
        store: The catalog holding the `AggregateDataset` for *identifier*.
            Must be folder-mode — a DuckLake is ATTACHed on ``open()``.
        reg: REST registry to fetch observations from.
        identifier: Catalog identifier in ``AGENCY:ID(VERSION)`` form.
            ``(latest)`` resolves to the highest stored version.
        measure_dim: Dimension to pivot into measure columns. ``None``
            (default) pivots on the DSD's ``MeasureDimension``.

    Returns:
        The fully-qualified table location (``<schema>.<table>``).

    Raises:
        KeyError: No `AggregateDataset` is stored under *identifier* — run
            ``ingest_sdmx`` first.
        ValueError: The dataset carries no ``sdmx_dataflow_urn``, so there is
            nothing to fetch observations for; *identifier* is not in
            ``AGENCY:ID(VERSION)`` form; or *measure_dim* is ``None`` and the
            DSD declares no ``MeasureDimension`` to pivot on.
    """
    identifier = _resolve_latest(store, identifier)

    ds = store.get(AggregateDataset, identifier)
    if ds is None:
        msg = f"No AggregateDataset found for {identifier!r} — run ingest_sdmx first"
        raise KeyError(msg)
    if ds.sdmx_dataflow_urn is None:
        msg = f"{identifier!r} has no sdmx_dataflow_urn — cannot fetch observation data"
        raise ValueError(msg)

    m = _IDENTIFIER_RE.match(identifier)
    if m is None:
        msg = f"Cannot parse identifier {identifier!r} — expected 'AGENCY:ID(VERSION)'"
        raise ValueError(msg)
    agency, flow_id, version = m.group(1), m.group(2), m.group(3)

    schema = agency.lower()
    table = f"df_{flow_id}_{version.replace('.', '_')}".lower()

    # Pull the wide dataframe and write through the bundle's lake. ``pivot()``
    # returns an eager DataFrame; with ``on=None`` it pivots on the DSD's
    # MeasureDimension, as ``reg.data(...).pivot()`` did.
    flow_urn = Dataflow.parse_urn(ds.sdmx_dataflow_urn)
    wide = reg.obs(flow_urn).pivot(on=measure_dim)
    store.data.write_observations(schema=schema, table=table, source=wide)

    # Register a DataflowBinding so ``cat.observations(flow_urn)`` can find
    # the table. Derive identity column maps from the DSD when resolvable.
    import sdmxlib as sl  # noqa: PLC0415

    flow = store.registry.get(flow_urn)
    if isinstance(flow, Dataflow) and isinstance(flow.structure, sl.Ref):
        dsd = store.registry.get(flow.structure.urn)
        if isinstance(dsd, DataStructure):
            binding = sl.DataflowBinding.from_dsd(
                dsd,
                dataflow=sl.Ref[Dataflow].unresolved(flow_urn),
                schema=schema,
                table=table,
            )
            store.registry.add(binding)

    ds.measure_dim = measure_dim
    store.put(ds)
    return f"{schema}.{table}"

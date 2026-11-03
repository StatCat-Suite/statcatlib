"""SDMX source connector for statcatlib.

It builds catalog records from SDMX model types and writes them, with the
structures and observations, into a working copy.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from sdmxlib import InternationalString
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure

from statcatlib.model.agents import Agent
from statcatlib.model.dataset import AggregateDataset
from statcatlib.store.schema import first_row
from statcatlib.store.working_copy import WorkingCopy

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sdmxlib import EditionRegistry, ObsQuery, RestRegistry, WriteSource
    from sdmxlib.local.registry import LocalRegistry
    from sdmxlib.model.in_memory_registry import InMemoryRegistry
    from sdmxlib.model.urn import SdmxUrn

    from statcatlib.model.skos import ConceptRef

__all__ = [
    "ObservationSource",
    "dataset_from_dataflow",
    "ingest_data",
    "ingest_dataflows",
    "ingest_sdmx",
    "ingest_sdmx_registry",
    "prepare_sdmx",
]

_IDENTIFIER_RE = re.compile(r"^([^:]+):([^(]+)\(([^)]+)\)$")

#: BCP 47 tag for a code stored as text: no linguistic content.
_NO_LANGUAGE = "zxx"


class ObservationSource(Protocol):
    """Where `ingest_data` reads observations from.

    `sdmxlib.RestRegistry` is the usual one; `sdmxlib.LocalRegistry` fits too.
    """

    def obs(self, flow: SdmxUrn[Dataflow], /) -> ObsQuery:
        """A lazy query over *flow*'s observations."""
        ...


def require_working_copy(store: object, call: str) -> WorkingCopy:
    """*store*, checked to be a `WorkingCopy`, the only thing ingest writes into.

    Args:
        store: What the caller passed.
        call: The ingest function, for the message.

    Returns:
        *store*.

    Raises:
        TypeError: *store* is anything else, such as an `EditionView`, naming
            ``working_copy()``.
    """
    if not isinstance(store, WorkingCopy):
        msg = (
            f"{call} writes, so it takes a WorkingCopy, not {type(store).__name__}: start one with "
            "Catalog.create(path, exist_ok=True).product(id).working_copy()"
        )
        raise TypeError(msg)
    return store


def _resolve_latest(store: WorkingCopy, identifier: str) -> str:
    """Resolve ``(latest)`` to the highest stored version for that agency:id."""
    if not identifier.endswith("(latest)"):
        return identifier
    m = _IDENTIFIER_RE.match(identifier)
    if m is None:
        return identifier
    agency, flow_id = m.group(1), m.group(2)
    rows = (
        store.view()
        ._cursor()
        .execute(
            """
            SELECT identifier FROM dataset
            WHERE identifier LIKE ?
            ORDER BY identifier DESC
            LIMIT 1
            """,
            [f"{agency}:{flow_id}(%"],
        )
        .fetchall()
    )
    row = first_row(rows)
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
        # An agency with no name is labelled by its ID as a code (GH-94).
        name = agency.name or InternationalString.of({_NO_LANGUAGE: agency.id})
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
                label = InternationalString.of({_NO_LANGUAGE: dim.id})
        else:
            label = InternationalString.of({_NO_LANGUAGE: dim.id})
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
    structures, such as an edition's: each flow's DSD is read from the store.
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


def ingest_sdmx(store: WorkingCopy, registry: InMemoryRegistry) -> list[AggregateDataset]:
    """Ingest all SDMX artefacts from a registry into a working copy.

    Step 1: Write all structural artefacts (codelists, DSDs, dataflows) into
    the edition's store through the working copy's sdmxlib registry.

    Step 2: For each Dataflow in the registry, create an AggregateDataset via
    ``dataset_from_dataflow()`` and store it.  Code and codelist queries use
    the SDMX structural tables via ``sdmx_dataflow_urn``.

    Args:
        store: The working copy to write into.
        registry: The SDMX artefacts.

    Returns:
        The AggregateDataset objects created. A *store* that is not a
        `WorkingCopy`, such as an `EditionView`, raises `TypeError`.
    """
    store = require_working_copy(store, "ingest_sdmx")
    local = store.registry
    local.ingest(registry)

    datasets = prepare_sdmx(registry)
    with store.bulk_load():
        store.put(datasets)
    return datasets


def ingest_dataflows(
    wc: WorkingCopy,
    source: RestRegistry | EditionRegistry,
    dataflows: Sequence[SdmxUrn[Dataflow] | str],
    *,
    observations: Mapping[str, WriteSource] | None = None,
    partials: bool = True,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]:
    """Fill a data product's working copy with dataflows, cutting their codelists, and store their datasets.

    It calls `WorkingCopy.fill_dataflows`, which copies each dataflow's
    closure from *source*, writes its observations, cuts each codelist to the
    codes the product uses and records what each partial was cut from. Then it
    builds an `AggregateDataset` for each dataflow given, read back from the
    working copy with its data structure, and stores them in one
    ``bulk_load``. The other dataflows a republished product holds keep their
    datasets.

    Args:
        wc: The working copy of a data product.
        source: Where the structures are: a `sdmxlib.RestRegistry`, or a
            sealed edition's or a scratch `sdmxlib.EditionRegistry`.
        dataflows: The dataflows, by URN. A ``latest`` URN resolves to the
            version the fill stored.
        observations: The rows of each dataflow that has any, by URN string.
        partials: Whether to cut codelists to partials.
        publisher: The publisher every dataset records; the dataflow's
            maintenance agency when ``None``.
        themes: The themes every dataset records.

    Returns:
        The datasets stored, one per dataflow, in the order given.

    Raises:
        LookupError: A dataflow or its data structure cannot be read back
            from the working copy.

    A *wc* that is not a `WorkingCopy`, such as an `EditionView`, raises
    `TypeError`, as does a structure product's working copy.
    """
    wc = require_working_copy(wc, "ingest_dataflows")
    wc.fill_dataflows(source, dataflows, observations=observations, partials=partials)
    datasets: list[AggregateDataset] = []
    for given in dataflows:
        urn = Dataflow.parse_urn(given) if isinstance(given, str) else given
        flow = wc.registry.get(urn, expect=Dataflow)
        dsd = None if flow is None else wc.registry.dsd(flow.urn)
        if flow is None or dsd is None:
            msg = f"{urn} or its data structure is not in the working copy of {wc.product_id} after the fill"
            raise LookupError(msg)
        datasets.append(dataset_from_dataflow(flow, dsd, publisher=publisher, themes=themes))
    with wc.bulk_load():
        wc.put(datasets)
    return datasets


def ingest_sdmx_registry(
    store: WorkingCopy,
    registry: InMemoryRegistry,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]:
    """Bulk ingest all dataflows from an sdmxlib registry into a working copy.

    Like ``ingest_sdmx()`` but exposes ``publisher`` and ``themes`` overrides
    that apply to every ingested dataset.

    Args:
        store: The working copy to write into.
        registry: The SDMX artefacts.
        publisher: The publisher every dataset records.
        themes: The themes every dataset records.

    Returns:
        The AggregateDataset objects created. A *store* that is not a
        `WorkingCopy`, such as an `EditionView`, raises `TypeError`.
    """
    store = require_working_copy(store, "ingest_sdmx_registry")
    local = store.registry
    local.ingest(registry)

    datasets = prepare_sdmx(registry, publisher=publisher, themes=themes)
    with store.bulk_load():
        store.put(datasets)
    return datasets


def ingest_data(
    store: WorkingCopy,
    reg: ObservationSource,
    identifier: str,
    *,
    measure_dim: str | None = None,
) -> str:
    """Materialise observation data for one dataflow into the working copy's lake.

    Fetches the observations from *reg* in the long layout, one row per
    observation with a column per dimension and measure, and writes them into
    ``<agency>.df_<id>_<version>`` in the edition's lake through
    ``wc.registry.write_observations``. A re-run replaces the table. It then
    registers a `sdmxlib.DataflowBinding` for that table, so the dataflow
    reads back through the edition's registry::

        obs = view.registry.obs(flow_urn)
        obs.filter(obs.col("GEO") == "CA").frame()
        obs.pivot(on=ds.measure_dim)

    Requires that ``ingest_sdmx`` has already been called for this
    identifier, so the catalog record and the DSD are stored.

    Args:
        store: The working copy holding the `AggregateDataset` for *identifier*.
        reg: Where to fetch observations from, usually a
            `sdmxlib.RestRegistry`.
        identifier: Catalog identifier in ``AGENCY:ID(VERSION)`` form.
            ``(latest)`` resolves to the highest stored version.
        measure_dim: The dimension readers pivot on, recorded as
            `AggregateDataset.measure_dim`. It does not change what is
            stored. ``None`` (default) records none, and ``pivot()`` then
            uses the DSD's ``MeasureDimension``.

    Returns:
        The fully-qualified table location (``<schema>.<table>``).

    A *store* that is not a `WorkingCopy`, such as an `EditionView`, raises
    `TypeError`.

    Raises:
        KeyError: No `AggregateDataset` is stored under *identifier* — run
            ``ingest_sdmx`` first.
        ValueError: The dataset carries no ``sdmx_dataflow_urn``, so there is
            nothing to fetch observations for; *identifier* is not in
            ``AGENCY:ID(VERSION)`` form; the dataflow's DSD is not stored, so
            no binding can describe the table; or *measure_dim* is not one of
            the DSD's dimensions.
    """
    import sdmxlib as sl  # noqa: PLC0415

    store = require_working_copy(store, "ingest_data")
    identifier = _resolve_latest(store, identifier)

    ds = store.view().get(AggregateDataset, identifier)
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

    flow_urn = Dataflow.parse_urn(ds.sdmx_dataflow_urn)
    dsd = store.registry.dsd(flow_urn)
    if dsd is None:
        msg = f"The DSD of {ds.sdmx_dataflow_urn} is not stored — ingest it with ingest_sdmx first"
        raise ValueError(msg)
    dimensions = [d.id for d in dsd.dimensions]
    if measure_dim is not None and measure_dim not in dimensions:
        msg = f"measure_dim {measure_dim!r} is not a dimension of {dsd.id}: {', '.join(dimensions)}"
        raise ValueError(msg)

    schema = agency.lower()
    table = f"df_{flow_id}_{version.replace('.', '_')}".lower()

    # Store the long layout. The binding below maps each DSD component to a
    # column of the same name, which is the long layout; a pivoted table has a
    # column per code instead and cannot be read through it (GH-29).
    store.registry.write_observations(schema=schema, table=table, source=reg.obs(flow_urn).frame())
    store.registry.add(
        sl.DataflowBinding.from_dsd(
            dsd,
            dataflow=sl.Ref[Dataflow].unresolved(flow_urn),
            schema=schema,
            table=table,
        )
    )

    ds.measure_dim = measure_dim
    store.put(ds)
    return f"{schema}.{table}"

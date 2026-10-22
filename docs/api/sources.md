# Source Connectors

statcatlib ships two source connectors for ingesting from remote data sources.
All connectors write to the shared DuckDB via `Catalog`.

## SDMX

`statcatlib.sources.sdmx` — connectors for any SDMX 2.1 REST endpoint
(Eurostat, OECD, BIS, ABS, ECB, and many more).

### `dataset_from_dataflow`

```python
dataset_from_dataflow(
    flow: sdmxlib.model.dataflow.Dataflow,
    dsd: sdmxlib.model.datastructure.DataStructure,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> AggregateDataset
```

Convert an sdmxlib `Dataflow` + `DataStructure` pair into an `AggregateDataset`.
Pure in-memory transformation — no database round-trips.

Returns an `AggregateDataset` with `identifier`, `title`, `description`,
`dimension_names`, `sdmx_dataflow_urn`, `publisher`, and `themes` (if provided).

`has_code()` and `has_dimensions()` work on datasets stored by `ingest_sdmx()`
because the structural tables are written alongside the catalog record via the
shared DuckDB connection.

Use `dataset_from_dataflow()` when you want to convert individual artefacts
without the full ingest pipeline.

### `ingest_sdmx`

```python
ingest_sdmx(
    store: Catalog,
    registry: sdmxlib.model.registry.Registry,
) -> list[AggregateDataset]
```

Ingest all artefacts from an sdmxlib `Registry`:

1. Calls `store.registry.ingest(registry)` to write codelists,
   DSDs, and dataflows into sdmxlib's structural tables via the shared
   connection.
2. For each `Dataflow` in the registry, calls `dataset_from_dataflow()` and
   then `store.put(dataset)`; SDMX structural tables written in step 1 provide
   the structural index for `has_code()` / `has_dimensions()` filters.
3. Rebuilds the LanceDB search index on exit (if LanceDB is available).

Returns the list of `AggregateDataset` objects created.

**Example:**

```python
import sdmxlib as sl
import statcatlib as sc

with sc.Catalog("my_catalog") as store:
    with sl.RestRegistry(sl.Provider.ESTAT) as reg:
        reg.get(sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP"), resolve=True)
        datasets = sc.ingest_sdmx(store, reg.registry)
        print(f"Ingested {len(datasets)} datasets")
```

### `ingest_sdmx_registry`

```python
ingest_sdmx_registry(
    store: Catalog,
    registry: sdmxlib.model.registry.Registry,
    *,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]
```

Like `ingest_sdmx()` but stamps a `publisher` and `themes` onto every dataset.
Use when all dataflows in the registry come from one agency and you want a
consistent publisher record:

```python
publisher = sc.Agent(id="ESTAT", name=InternationalString(en="Eurostat"))
themes = [sc.ConceptRef(uri="urn:myorg:themes/ECONOMY")]
sc.ingest_sdmx_registry(store, reg.registry, publisher=publisher, themes=themes)
```

### `ingest_data`

```python
ingest_data(
    store: Catalog,
    reg: sdmxlib.api.registry.RestRegistry,
    identifier: str,
    *,
    measure_dim: str | None = None,
) -> str
```

Materialise observation data for one dataflow into the bundle's lake. The
observations are fetched with `reg.data(...).pivot()`, written into
`lake.<agency>.df_<id>_<version>` through the bundle's DuckLake-backed data
store, and bound so the dataflow is queryable through `cat.observations(...)`.

**Parameters:**

- `store` — a folder-mode `Catalog` (a DuckLake is ATTACHed on `open()`) that
  already holds an `AggregateDataset` for `identifier` — run `ingest_sdmx` first
- `reg` — an open `RestRegistry` connected to the SDMX endpoint
- `identifier` — `"AGENCY:ID(VERSION)"` format, e.g. `"ESTAT:NAMA_10_GDP(1.0)"`.
  The version `latest` is resolved to the highest stored version automatically.
- `measure_dim` — dimension to pivot into measure columns. `None` (the default)
  leaves the data long.

**Returns:** the fully-qualified table location (`<schema>.<table>`).

```python
with sc.Catalog("my_catalog") as store:
    with sl.RestRegistry(sl.Provider.ESTAT) as reg:
        reg.get(sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP"), resolve=True)
        sc.ingest_sdmx(store, reg.registry)

        table = sc.ingest_data(
            store, reg,
            "ESTAT:NAMA_10_GDP(latest)",
            measure_dim="na_item",
        )
        print(f"Observations written to {table}")
```

**Raises:**

- `KeyError` — if no `AggregateDataset` exists for `identifier`
- `ValueError` — if the dataset carries no `sdmx_dataflow_urn`, or `identifier`
  is not in `AGENCY:ID(VERSION)` form

---

## CKAN

`statcatlib.sources.ckan` — connector for any CKAN portal (open.canada.ca,
data.gov, data.gov.uk, …).

### `CkanClient`

HTTP client for the CKAN Action API v3.

```python
class CkanClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
    ): ...

    def __enter__(self) -> CkanClient: ...
    def __exit__(self, *args) -> None: ...
```

```python
from statcatlib.sources.ckan import CkanClient

with CkanClient("https://open.canada.ca/data") as client:
    datasets = sc.ingest_ckan(store, client, organization="statcan")
```

### `ingest_ckan`

```python
ingest_ckan(
    store: Catalog,
    client: CkanClient,
    *,
    organization: str | None = None,
    subject: str | None = None,
    rows: int = 100,
) -> list[OpenDataset]
```

Ingest datasets from a CKAN portal as `OpenDataset` records. Handles
pagination internally up to `rows` total results.

**Parameters:**

- `organization` — filter by CKAN organization slug
- `subject` — filter by subject/tag
- `rows` — maximum number of datasets to ingest

CKAN resources are mapped to `Distribution` objects on each `OpenDataset`.
Bilingual metadata (English/French) is handled where available.

```python
with CkanClient("https://open.canada.ca/data") as client:
    # All StatCan datasets from open.canada.ca
    datasets = sc.ingest_ckan(
        store, client,
        organization="statcan",
        rows=500,
    )

    # Environmental datasets from a thematic filter
    env_datasets = sc.ingest_ckan(
        store, client,
        subject="environment",
        rows=100,
    )
```

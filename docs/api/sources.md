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
    reg: ObservationSource,
    identifier: str,
    *,
    measure_dim: str | None = None,
) -> str
```

Materialise observation data for one dataflow into the bundle's lake. The
observations are fetched with `reg.obs(flow).frame()` in the long layout, one
row per observation with a column per dimension and measure, and written into
`lake.<agency>.df_<id>_<version>` through the bundle's DuckLake-backed data
store. A re-run replaces the table. `ingest_data` then registers an sdmxlib
`DataflowBinding` for the table, so the dataflow reads back through
`store.registry.obs(flow)`.

**Parameters:**

- `store` — a folder-mode `Catalog` (a DuckLake is ATTACHed on `open()`) that
  already holds an `AggregateDataset` for `identifier` — run `ingest_sdmx` first
- `reg` — where to fetch observations from: any object with
  `obs(flow) -> sdmxlib.ObsQuery` (`ObservationSource`), usually an open
  `RestRegistry`
- `identifier` — `"AGENCY:ID(VERSION)"` format, e.g. `"ESTAT:NAMA_10_GDP(1.0)"`.
  The version `latest` is resolved to the highest stored version automatically.
- `measure_dim` — the dimension readers pivot on, recorded as
  `AggregateDataset.measure_dim`. It does not change what is stored. `None`
  (the default) records none, and `pivot()` then uses the DSD's
  `MeasureDimension`.

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
- `ValueError` — if the dataset carries no `sdmx_dataflow_urn`, `identifier`
  is not in `AGENCY:ID(VERSION)` form, the dataflow's DSD is not stored, or
  `measure_dim` is not one of the DSD's dimensions

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

    def package_search(
        self,
        *,
        organization: str | None = None,
        subject: str | None = None,
        rows: int = 100,
        start: int = 0,
    ) -> list[dict[str, object]]: ...
    def alternate_languages(self, dataset_type: str = "dataset") -> dict[str, list[str]]: ...
    def close(self) -> None: ...
    def __enter__(self) -> CkanClient: ...
    def __exit__(self, *args) -> None: ...
```

`package_search` returns raw package dicts, paging internally.
`alternate_languages` returns the language pairs the portal's
`scheming_dataset_schema_show` declares, such as
`{"en": ["en-t-fr"], "fr": ["fr-t-en"]}` on open.canada.ca, or `{}` when the
portal runs no ckanext-scheming schema.

```python
from statcatlib.sources.ckan import CkanClient

with CkanClient("https://open.canada.ca/data") as client:
    packages = client.package_search(organization="statcan-statcan", rows=10)
```

### `ingest_ckan`

```python
ingest_ckan(
    store: Catalog,
    base_url: str,
    *,
    organization: str | None = None,
    subject: str | None = None,
    limit: int = 100,
) -> list[OpenDataset]
```

Ingest datasets from a CKAN portal as `OpenDataset` records, paging
internally up to `limit` results. It also records the portal's declared
language pairs with `store.add_language_alternates(..., source=base_url)`.

**Parameters:**

- `base_url` — the portal, e.g. `"https://open.canada.ca/data"`
- `organization` — filter by CKAN organization slug
- `subject` — free-text filter appended to the search query
- `limit` — maximum number of datasets to ingest

CKAN resources are mapped to `Distribution` objects on each `OpenDataset`.

**Languages.** Each language key is stored under its own tag as the portal
sends it. open.canada.ca sends English translated from French under `en-t-fr`
and French translated from English under `fr-t-en`, and these stay separate
from `en` and `fr`: `title.get("en")` returns `None` for a package whose only
English title is under `en-t-fr`, and `title.get("en", "en-t-fr")` returns it.
CKAN declares no default language, so a plain string is stored under `und`
(undetermined) and a code stored as text, such as a subject slug or frequency
code, under `zxx`. No field is given an English value the portal did not send.

```python
# All StatCan datasets from open.canada.ca
datasets = sc.ingest_ckan(
    store,
    "https://open.canada.ca/data",
    organization="statcan-statcan",
    limit=500,
)

# Environmental datasets from a thematic filter
env_datasets = sc.ingest_ckan(
    store,
    "https://open.canada.ca/data",
    subject="environment",
    limit=100,
)
```

# sources

## Overview

`statcatlib.sources` holds the connectors that write external catalogs into a
`Catalog`. Two sources exist:

- `sdmx.py` ingests an SDMX registry. It writes the structural artefacts into
  sdmxlib's `sdmx` schema through `Catalog.registry`, then stores one
  `AggregateDataset` per dataflow. Each dataset links to its dataflow through
  `sdmx_dataflow_urn` and copies no codes, codelists or DSDs. `ingest_data`
  also materialises one dataflow's observations into the bundle's DuckLake.
- `ckan.py` ingests a CKAN portal (open.canada.ca, data.gov) through the CKAN
  Action API v3 and stores one `OpenDataset` per CKAN package.

The package's `__init__.py` re-exports the six ingest functions, and
`statcatlib/__init__.py` re-exports them again as `sc.ingest_sdmx` and so on.

## Interface

Exported from `statcatlib.sources` (and from `statcatlib`):

- `ingest_sdmx(store, registry) -> list[AggregateDataset]` calls
  `store.registry.ingest(registry)`, then `prepare_sdmx(registry)`, then
  `store.put(datasets)` inside `store.bulk_load()`.
- `ingest_sdmx_registry(store, registry, *, publisher=None, themes=None)`
  does the same as `ingest_sdmx` and applies the `publisher` and `themes`
  overrides to every dataset.
- `prepare_sdmx(registry, *, publisher=None, themes=None) -> list[AggregateDataset]`
  builds datasets without storing them, so callers can `attrs.evolve()` each
  one before `store.put()`. It accepts an `InMemoryRegistry` or a
  `LocalRegistry` whose store already holds the structures. Dataflows whose
  structure reference is missing, unresolved, or not a `DataStructure` are
  skipped.
- `dataset_from_dataflow(flow, dsd, *, publisher=None, themes=None) -> AggregateDataset`
  converts one sdmxlib `Dataflow` and its `DataStructure`. The identifier is
  `AGENCY:ID(VERSION)` from the flow's maintainer. The default publisher is an
  `Agent` built from the maintainer agency, with the agency id as the English
  name when the agency has neither an `en` nor an `fr` name.
  `dimension_names` holds each dimension's resolved concept name, or the
  dimension id in `en` and `fr` when the concept is unresolved or the
  resolved object has no `name`.
- `ingest_data(store, reg, identifier, *, measure_dim=None) -> str` fetches
  observations from `reg.obs(flow_urn).frame()` in the long layout, writes
  them with `store.data.write_observations` to
  `lake.<agency>.df_<id>_<version>` (lowercase, dots in the version become
  `_`), registers an `sdmxlib.DataflowBinding.from_dsd` for that table, sets
  `AggregateDataset.measure_dim`, stores the dataset again, and returns
  `"<schema>.<table>"`. An identifier ending in `(latest)` resolves to the
  highest stored version by a descending string sort on `dataset.identifier`.
  - Raises `KeyError` when no `AggregateDataset` is stored under the
    identifier.
  - Raises `ValueError` when the dataset has no `sdmx_dataflow_urn`, the
    identifier is not `AGENCY:ID(VERSION)`, the DSD is not stored, or
    `measure_dim` is not a DSD dimension.
- `ingest_ckan(store, base_url, *, organization=None, subject=None, limit=100) -> list[OpenDataset]`
  fetches up to `limit` packages with `CkanClient.package_search`, maps each
  with `_ckan_package_to_dataset`, and calls `store.put(datasets)`.

Public in their modules, not re-exported by the package:

- `sdmx.ObservationSource` is a `Protocol` with one method,
  `obs(flow: SdmxUrn[Dataflow], /) -> ObsQuery`. `sdmxlib.RestRegistry` and
  `sdmxlib.LocalRegistry` both satisfy it.
- `ckan.CkanClient(base_url, *, timeout=30.0)` is a context manager over an
  `httpx.Client`. `package_search(*, organization=None, subject=None, rows=100, start=0)`
  returns raw package dicts, paging 100 rows per request until `rows` results
  are collected or the portal's `count` is reached. With no filters the query
  is `*:*`. `close()` closes the HTTP client.

Internal helpers that tests import directly:

- `ckan._ckan_package_to_dataset(raw) -> OpenDataset` is a pure mapper with no
  network or store access. It raises `ValueError` when the package has no
  string `id`. Every other field falls back to a default.

## Invariants

- An `AggregateDataset` links to sdmxlib's tables through
  `sdmx_dataflow_urn` only. Structural queries JOIN through that URN at query
  time, so ingest copies no codes, codelists or DSD components into the
  `statcat` schema.
- `sdmx_dataflow_urn` is built with `sdmxlib.model.urn.SdmxUrn` from the
  flow's package, class, agency, id and version. Do not assemble URN strings
  by hand.
- Catalog identifiers for SDMX datasets have the form `AGENCY:ID(VERSION)`.
  `ingest_data` parses this form with `_IDENTIFIER_RE` and derives the lake
  schema and table names from it.
- `ingest_data` requires a prior `ingest_sdmx` for the same dataflow, so the
  catalog record and the DSD are stored, and a folder-mode `Catalog`, where
  `open()` ATTACHes the DuckLake as `lake`.
- `ingest_data` stores the long layout (one row per observation, one column
  per DSD component). The `DataflowBinding` maps each component to a
  same-named column, and a pivoted table cannot be read through it (GH-29).
  `measure_dim` is metadata for readers that pivot and changes nothing stored.
- CKAN payloads stay typed `object` at the boundary. The `_s`, `_i`, `_d` and
  `_seq` accessors narrow each value and return `None` or an empty container
  on a type mismatch. Keep new CKAN fields behind these accessors rather than
  typing raw JSON as `Any`.
- CKAN bilingual fields can arrive as dicts, JSON-encoded strings, or plain
  strings. `_parse_istring` and `_parse_frequency` accept all three. The
  others return `[]` for one shape: `_parse_keywords` for a non-JSON plain
  string or JSON that decodes to a non-dict, and `_parse_subjects` and
  `_parse_spatial` for a top-level dict (they accept lists; `_parse_spatial`
  accepts dicts as list items).
- CKAN subjects become `ConceptRef(uri="urn:ckan:tag:<slug>")`. These refs are
  unvalidated and match no stored `ConceptScheme` unless one is ingested
  separately.
- CKAN frequency labels map to catalog ids through `_FREQ_LABEL_TO_ID`; an
  unknown label becomes its uppercase form with spaces replaced by `_`.
- A CKAN resource's `url` becomes `download_url` when `url_type` is `upload`
  or `download`, and `access_url` otherwise.

## Side Effects

- `ingest_sdmx` and `ingest_sdmx_registry` write sdmxlib's `sdmx` schema
  through `store.registry.ingest()` and upsert rows in the `statcat` schema
  through `store.put()`. Their `bulk_load()` block raises DuckDB's
  `wal_autocheckpoint` to 1 GB and runs one `CHECKPOINT` on exit.
- `ingest_data` makes network calls through `reg.obs(...)` when `reg` is a
  `RestRegistry`, writes or replaces a table in the DuckLake (`lake` schema),
  adds a `DataflowBinding` to the co-located `LocalRegistry`, and upserts the
  dataset row.
- `ingest_ckan` and `CkanClient` make HTTP GET requests to
  `<base_url>/api/3/action/package_search`. `CkanClient._get` raises
  `httpx.HTTPStatusError` on an HTTP error, `TypeError` when the body is not a
  JSON object, and `RuntimeError` when CKAN reports `success: false`.
  `ingest_ckan` upserts its datasets with `store.put()` outside a
  `bulk_load()` block.
- None of these functions rebuild the search index. Run
  `Catalog.build_index()` after ingest, and publish a bundle after that.

## Verification

- Run: `just ci` (the project gate).
- Unit tier: `uv run pytest -m unit` covers `tests/test_sources_ckan.py` and
  `tests/test_sdmx_bridge.py`.
- Integration tier: `uv run pytest -m integration` covers
  `tests/test_sources_sdmx.py` and `tests/test_ingest_data.py`.
- Live tier (network, outside `just ci`): `just test-live` runs
  `tests/test_live_sdmx.py`.

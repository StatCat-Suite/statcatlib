# sources

## Overview

`statcatlib.sources` holds the connectors that write external catalogs into a
product's `WorkingCopy`, its one unsealed edition. Two sources exist:

- `sdmx.py` ingests an SDMX registry. It writes the structural artefacts into
  sdmxlib's `sdmx` schema through `WorkingCopy.registry`, then stores one
  `AggregateDataset` per dataflow. Each dataset links to its dataflow through
  `sdmx_dataflow_urn` and copies no codes, codelists or DSDs. `ingest_data`
  also materialises one dataflow's observations into the edition's DuckLake.
- `ckan.py` ingests a CKAN portal (open.canada.ca, data.gov) through the CKAN
  Action API v3 and stores one `OpenDataset` per CKAN package.

The package's `__init__.py` re-exports the seven ingest functions, and
`statcatlib/__init__.py` re-exports them again as `sc.ingest_sdmx` and so on.

## Interface

Exported from `statcatlib.sources` (and from `statcatlib`):

- `ingest_sdmx(store: WorkingCopy, registry) -> list[AggregateDataset]`
  calls `require_working_copy`, then `store.registry.ingest(registry)`, then
  `prepare_sdmx(registry)`, then `store.put(datasets)` inside
  `store.bulk_load()`.
- `ingest_sdmx_registry(store, registry, *, publisher=None, themes=None)`
  does the same as `ingest_sdmx` and applies the `publisher` and `themes`
  overrides to every dataset.
- `ingest_dataflows(wc, source, dataflows, *, observations=None,
  partials=True, publisher=None, themes=None) -> list[AggregateDataset]`
  is the partial path: it calls `require_working_copy`, then
  `wc.fill_dataflows` (`sdmxlib.edition.fill_edition`: each dataflow's
  closure and observations, codelists cut to partials, the cuts recorded in
  `statcat._meta.partial_of`), then reads each given dataflow back from
  `wc.registry` with its DSD (`wc.registry.dsd`), builds its dataset with
  `dataset_from_dataflow`, and stores them in one `bulk_load()`. It builds
  datasets for the given dataflows only, so on a republish the other
  tables' datasets stay as they were. A dataflow that cannot be read back
  raises `LookupError`.
- `prepare_sdmx(registry, *, publisher=None, themes=None) -> list[AggregateDataset]`
  builds datasets without storing them, so callers can `attrs.evolve()` each
  one before `store.put()`. It accepts an `InMemoryRegistry` or a
  `LocalRegistry` whose store already holds the structures. Dataflows whose
  structure reference is missing, unresolved, or not a `DataStructure` are
  skipped.
- `dataset_from_dataflow(flow, dsd, *, publisher=None, themes=None) -> AggregateDataset`
  converts one sdmxlib `Dataflow` and its `DataStructure`. The identifier is
  `AGENCY:ID(VERSION)` from the flow's maintainer. The default publisher is an
  `Agent` built from the maintainer agency, named by the agency id under
  `zxx` (a code) when the agency has no name.
  `dimension_names` holds each dimension's resolved concept name, or the
  dimension id under `zxx` when the concept is unresolved or the resolved
  object has no `name` (GH-94).
- `ingest_data(store: WorkingCopy, reg, identifier, *, measure_dim=None) -> str`
  reads the dataset through `store.view().get` and the DSD through
  `store.registry.dsd`, fetches observations from `reg.obs(flow_urn).frame()`
  in the long layout, writes them with `store.registry.write_observations`
  to `<agency>.df_<id>_<version>` in the edition's lake (lowercase, dots in
  the version become `_`; a re-run replaces the table), adds an
  `sdmxlib.DataflowBinding.from_dsd` for that table to `store.registry`, sets
  `AggregateDataset.measure_dim`, stores the dataset again, and returns
  `"<schema>.<table>"`. An identifier ending in `(latest)` resolves to the
  highest stored version by a descending string sort on `dataset.identifier`.
  - Raises `KeyError` when no `AggregateDataset` is stored under the
    identifier.
  - Raises `ValueError` when the dataset has no `sdmx_dataflow_urn`, the
    identifier is not `AGENCY:ID(VERSION)`, the DSD is not stored, or
    `measure_dim` is not a DSD dimension.
- `ingest_ckan(store: WorkingCopy, base_url, *, organization=None, subject=None, limit=100) -> list[OpenDataset]`
  calls `require_working_copy`, then fetches up to `limit` packages with `CkanClient.package_search`, maps each
  with `_ckan_package_to_dataset`, and calls `store.put(datasets)`. It then
  records the portal's `CkanClient.alternate_languages()` with
  `store.add_language_alternates(..., source=base_url)`. When the schema lists
  `geographic_region` choices, `CkanClient.field_choices` returns them as a
  `ConceptScheme`, which is stored first; each region code a package carries
  becomes a `SpatialCoverage` whose `uri` is that code's concept URI, with no
  label of its own (GH-83).

Public in their modules, not re-exported by the package:

- `sdmx.require_working_copy(store, call) -> WorkingCopy` returns *store*
  when it is a `WorkingCopy`, and otherwise raises `TypeError` naming *call*
  and `Catalog.create(path, exist_ok=True).product(id).working_copy()`.
  `ckan.py` imports it from `sdmx.py`.

- `sdmx.ObservationSource` is a `Protocol` with one method,
  `obs(flow: SdmxUrn[Dataflow], /) -> ObsQuery`. `sdmxlib.RestRegistry` and
  `sdmxlib.LocalRegistry` both satisfy it.
- `ckan.CkanClient(base_url, *, timeout=30.0)` is a context manager over an
  `httpx.Client`. `package_search(*, organization=None, subject=None, rows=100, start=0)`
  returns raw package dicts, paging 100 rows per request until `rows` results
  are collected or the portal's `count` is reached. With no filters the query
  is `*:*`. `alternate_languages(dataset_type="dataset")` returns the
  `alternate_languages` declared by `scheming_dataset_schema_show`, or `{}`
  when the portal has no scheming schema (HTTP 400 or 404, or
  `success: false`). `field_choices(field_name, dataset_type="dataset")`
  returns a select field's choices as a `ConceptScheme` at
  `choice_scheme_uri`, each choice a `Concept` whose notation is its code, or
  `None`. Both read one schema, fetched once per client; any other HTTP
  failure raises `httpx.HTTPStatusError`. `close()` closes the HTTP client.
- `ckan.choice_scheme_uri(base_url, field_name, dataset_type="dataset") -> str`
  is `{base_url}/scheming/{dataset_type}/{field_name}`.

Internal helpers that tests import directly:

- `ckan._ckan_package_to_dataset(raw, *, regions=None) -> OpenDataset` is a
  pure mapper with no network or store access. It raises `ValueError` when
  the package has no string `id`. Every other field falls back to a default;
  an organization with neither slug nor UUID gives no publisher (GH-94).
  `regions` is the `geographic_region` scheme that `_parse_spatial` maps
  codes through; other region text is stored under `und`. Each entry of the
  package's `language` field passes through `_tag`, the normalisation text
  keys get, so `OpenDataset.language` spells a tag as every multilingual
  field does; a malformed entry is dropped and duplicates are removed, first
  kept (GH-102).

## Invariants

- Every ingest function writes into a `WorkingCopy` only. `ingest_sdmx`,
  `ingest_sdmx_registry`, `ingest_dataflows`, `ingest_data` and
  `ingest_ckan` pass their *store*
  through `require_working_copy`, so an `EditionView` raises `TypeError`
  before anything is written. A sealed edition is never written.
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
- `ingest_data` requires a prior `ingest_sdmx` into the same working copy,
  so the catalog record and the DSD are stored. statcatlib attaches no lake
  of its own: the observations go into the edition's DuckLake through the
  working copy's registry.
- `ingest_data` stores the long layout (one row per observation, one column
  per DSD component). The `DataflowBinding` maps each component to a
  same-named column, and a pivoted table cannot be read through it (GH-29).
  `measure_dim` is metadata for readers that pivot and changes nothing stored.
- CKAN payloads stay typed `object` at the boundary. The `_s`, `_i`, `_d` and
  `_seq` accessors narrow each value and return `None` or an empty container
  on a type mismatch. Keep new CKAN fields behind these accessors rather than
  typing raw JSON as `Any`.
- Every CKAN language tag is kept, `en-t-fr` and `fr-t-en` included, in the
  form `sdmxlib.normalize_tag` returns. `_lang_map` strips each tag and
  replaces `_` with `-` first. A tag still malformed is dropped with a
  WARNING and counted, and `ingest_ckan` logs the count per `base_url`; of two
  tags that normalise to one, the first text is kept, with a WARNING. No field gets an English value the portal did not send:
  a plain string is stored under `und`, and a code stored as text (a subject
  slug, a frequency code, an organization slug used as its name) under `zxx`.
  Empty values are dropped, and a field with none left is empty (GH-84).
- CKAN multilingual fields can arrive as dicts, JSON-encoded strings, or plain
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

- `ingest_sdmx` and `ingest_sdmx_registry` write the edition's `sdmx` schema
  through `store.registry.ingest()` and upsert rows in its `statcat` schema
  through `store.put()`. Their `bulk_load()` block raises DuckDB's
  `wal_autocheckpoint` to 1 GB and runs one `CHECKPOINT` on exit.
- `ingest_dataflows` writes the edition's `sdmx` schema, lake and
  bindings through `sdmxlib.edition.fill_edition`, the `statcat._meta`
  keys `fill_in_progress` and `partial_of`, and the dataset rows; with a
  `RestRegistry` source, sdmxlib fetches each dataflow's closure over HTTP.
- `ingest_data` makes network calls through `reg.obs(...)` when `reg` is a
  `RestRegistry`, writes or replaces a table in the edition's DuckLake
  through `store.registry.write_observations`, adds a `DataflowBinding` to
  the working copy's `LocalRegistry`, and upserts the dataset row.
- `ingest_ckan` and `CkanClient` make HTTP GET requests to
  `<base_url>/api/3/action/package_search`. `CkanClient._get` raises
  `httpx.HTTPStatusError` on an HTTP error, `TypeError` when the body is not a
  JSON object, and `RuntimeError` when CKAN reports `success: false`.
  `ingest_ckan` upserts its datasets with `store.put()` outside a
  `bulk_load()` block.
- None of these functions rebuild the search index. `WorkingCopy.seal`
  rebuilds it from the edition's rows; call `wc.build_index()` after ingest
  to search through `wc.view()` before the seal.

## Verification

- Run: `just ci` (the project gate).
- Unit tier: `uv run pytest -m unit` covers `tests/test_sources_ckan.py` and
  `tests/test_sdmx_bridge.py`.
- Integration tier: `uv run pytest -m integration` covers
  `tests/test_sources_sdmx.py`, `tests/test_ingest_data.py` and, for
  `ingest_dataflows`, `tests/test_partials.py`.
- Live tier (network, outside `just ci`): `just test-live` runs
  `tests/test_live_sdmx.py`.

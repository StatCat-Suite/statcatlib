# statcatlib.model

## Overview

The domain model holds the catalog's records as plain attrs classes. It
borrows the DCAT-AP vocabulary and the StatDCAT-AP statistical extensions,
and runs without RDF. The store (`../store/`) persists these classes into
the `statcat` DuckDB schema and rebuilds them on read. The model itself
performs no I/O.

The only sdmxlib import is `InternationalString`, a multilingual value
type (`dict` of language code to text). SDMX model types such as
`Dataflow`, `DataStructure` and `Code` never appear in a field here.

## Interface

All symbols below are re-exported from `statcatlib.model` (`__init__.py`)
unless marked otherwise.

### Datasets (`dataset.py`)

- `BaseDataset`: the fields every dataset carries. `identifier`, `title`
  and `description` are required. Optional fields cover publisher,
  creator, contact point, keywords, themes, subject, `dataset_type`,
  dates, frequency, spatial and temporal coverage, distributions,
  licence, language, landing page, version, `version_notes`, `conforms_to`, access
  rights, status, provenance, quality annotations and `extras`.
- Five concrete variants, each with a `kind` class variable typed as a
  one-member `Literal`:
  - `OpenDataset` (`"open"`): adds no fields.
  - `AggregateDataset` (`"aggregate"`): `dimension_names`, `num_series`,
    `sdmx_dataflow_urn`, `measure_dim`.
  - `MicrodataDataset` (`"microdata"`): `variables`, `sample_size`,
    `universe`, `collection_method`, `anonymization`, `survey_id`.
  - `GeospatialDataset` (`"geospatial"`): `crs`, `spatial_resolution`,
    `feature_types`, `scale`.
  - `PublicationDataset` (`"publication"`): `authors`, `doi`, `isbn`,
    `publication_type`, `pages`, `series_title`.
- `Variable`: one variable of a microdata dataset.
- `Dataset`: PEP 695 union alias of the five variants.
- `DatasetT`: `TypeVar` bound to `BaseDataset`, used by `Catalog.get()`
  and `Catalog.query()`.
- `StatisticalDataset`: alias of `AggregateDataset`, kept for backward
  compatibility.
- `DatasetKind` and `DATASET_KINDS`: the type and the tuple of the five
  tags. Exported from `model/dataset.py` only; `store/store.py` imports
  `DATASET_KINDS` from there.

### Supporting records

- `Agent`, `ContactPoint` (`agents.py`): `dct:publisher`/`dct:creator`
  and `dcat:contactPoint`.
- `CatalogRecord`, `CatalogDescription` (`catalog.py`): `dcat:CatalogRecord`
  and `dcat:Catalog`. `CatalogDescription` is the metadata record for a
  catalog. The runtime store is the separate class `statcatlib.Catalog`.
- `Distribution`, `DataService` (`distribution.py`): `dcat:Distribution`
  and `dcat:DataService`.
- `Standard` (`classification.py`): `dct:conformsTo`.
- `LicenceDocument` (`licence.py`): `dct:license`.
- `QualityAnnotation`, `ProvenanceStatement` (`quality.py`):
  `dqv:hasQualityAnnotation` and `dct:provenance`.
- `SpatialCoverage` (`spatial.py`): `dct:spatial`, a labelled region,
  a URI, or a bounding box.
- `TemporalCoverage`, `Frequency` (`temporal.py`): `dct:temporal` and
  `dct:accrualPeriodicity`.
- `InternationalString` (`types.py`): re-export from sdmxlib.

### SKOS vocabulary (`skos.py`)

- `ConceptScheme`: a controlled vocabulary identified by `uri`. It owns
  its concepts as `concepts: dict[str, Concept]` keyed by notation, and
  lists `top_concepts` by notation.
- `Concept`: identified by `notation` within its scheme. It has no `uri`
  field. `broader`, `narrower` and `related` are tuples of notations in
  the same scheme.
- `ConceptRef`: a citation of a concept by `uri`, carried on
  `BaseDataset.themes`, `.subject` and `.dataset_type`.
- `ConceptSchemeRef`: a citation of a scheme by `uri`, carried on
  `CatalogDescription.theme_taxonomy`.
- `is_concept_in_scheme(scheme, notation) -> bool`: in-memory membership
  check.
- `concept_uri(scheme_uri, notation) -> str`: `{scheme_uri}/{notation}`, the
  URI the store gives a concept. Every reference to a stored concept builds
  its URI here.

## Invariants

- The model imports nothing from sdmxlib except `InternationalString`.
  Never add an SDMX model type to a field; link to sdmxlib's tables by
  URN instead.
- `AggregateDataset.sdmx_dataflow_urn` is a foreign key into sdmxlib's
  dataflow rows. It links to the DSD, codelists and codes. Never copy
  that structure into this package.
- `BaseDataset` has no `kind` and is never instantiated directly. Each
  concrete variant declares `kind` as its own one-member `Literal`.
  Annotating a variant with `DatasetKind` widens the type and loses the
  narrowing.
- `DATASET_KINDS` is built from the classes' `kind` values in
  declaration order, so the type checker verifies each tag against
  `DatasetKind`. The tags are the values of the `statcat.dataset.kind`
  column. Adding or renaming a variant changes stored data and the
  store's per-kind logic.
- Dataset and supporting records use `@define` and are mutable. Copy
  them with `attrs.evolve()`. The SKOS types use `attrs.frozen` and are
  immutable.
- `ConceptRef` and `ConceptSchemeRef` compare and hash on `uri` alone.
  `label`, `notation` and `scheme_uri` are declared `eq=False,
  hash=False`, so refs from different sources match.
  `ConceptRef.label` is empty when the cited concept is absent from the
  store.
- `BaseDataset.extras` is `dict[str, str]` to match its
  `MAP(VARCHAR, VARCHAR)` column. Callers encode structured values
  themselves, for example with `json.dumps`.
- Field names in `BaseDataset` and its variants are the relationship
  names that `QueryBuilder.include()` accepts (`VALID_INCLUDE_RELS` in
  the store). Renaming a relationship field changes that public API.
- Multilingual fields are `InternationalString` and map to
  `MAP(VARCHAR, VARCHAR)` columns in the store.

## Side Effects

- None. The package defines data classes and one pure function. It
  performs no database, file or network access. Persistence goes through
  `statcatlib.Catalog.put()` in
  [`src/statcatlib/store/store.py`](../store/store.py), which
  also cascades a `ConceptScheme` into its concept rows.

## Notes

- The `ConceptScheme` docstring in `skos.py` names
  `store.put_concept_scheme(...)`. The store method of that name is now
  private (`_put_concept_scheme`). The public path is
  `Catalog.put(scheme)`.

## Verification

- Run: `just ci` (the project gate).
- Unit tests for this package: `uv run pytest -m unit tests/test_model.py tests/test_skos.py`.

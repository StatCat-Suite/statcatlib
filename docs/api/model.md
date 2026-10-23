# Domain Model

statcatlib's domain model is a set of Python `attrs` classes aligned with
DCAT-AP and StatDCAT-AP. All classes use `@define` (mutable). The DCAT-AP and
PROV-O vocabularies shape the classes, which are plain Python with no RDF
runtime.

## Dataset hierarchy

All datasets inherit from `BaseDataset`. Each variant's `kind` class variable
is a string literal that discriminates the union:

```
BaseDataset (abstract — do not instantiate directly)
├── OpenDataset        kind = "open"
├── AggregateDataset   kind = "aggregate"
├── MicrodataDataset   kind = "microdata"
├── GeospatialDataset  kind = "geospatial"
└── PublicationDataset kind = "publication"
```

The type alias `Dataset` covers all five:

```python
type Dataset = OpenDataset | AggregateDataset | MicrodataDataset | GeospatialDataset | PublicationDataset
```

`StatisticalDataset` is a backward-compatibility alias for `AggregateDataset`.

### Type discrimination with `kind`

In Python code, use `isinstance()` to narrow dataset types. In contexts
where `isinstance` is unavailable — Jinja2 templates, serialised JSON,
switch logic — use the `kind` field instead:

```jinja2
{% if ds.kind == 'aggregate' %}
  <span>{{ ds.dimension_names | length }} dimensions</span>
{% elif ds.kind == 'publication' %}
  <span>{{ ds.publication_type }}</span>
{% endif %}
```

`kind` is a `ClassVar` on each concrete variant, not a field: it is derived
from the class, always populated, and read-only (assigning to it is both a
type error and an `AttributeError`). It does not appear in
`attrs.fields(OpenDataset)` or `attrs.asdict(ds)`, and cannot be passed to a
constructor. It matches the `kind` column in the database, so it is also
usable in facet results (`store.facets("type")` returns `{"aggregate": 42, ...}`).

`BaseDataset` has no `kind` — it is never instantiated, so it carries no tag.
Code that needs the tag should take `Dataset` (the union) rather than
`BaseDataset`; `DATASET_KINDS` holds all five values.

---

## BaseDataset

Common fields shared by all dataset kinds. Aligned with `dcat:Dataset`.

```python
@define
class BaseDataset:
    identifier: str                                      # REQUIRED — unique key
    title: InternationalString                           # REQUIRED
    description: InternationalString                     # REQUIRED

    publisher: Agent | None = None
    contact_point: ContactPoint | None = None
    keywords: list[InternationalString] = []
    themes: tuple[ConceptRef, ...] = ()
    dataset_type: ConceptRef | None = None
    subject: tuple[ConceptRef, ...] = ()
    issued: datetime | None = None
    modified: datetime | None = None
    frequency: Frequency | None = None
    spatial_coverage: list[SpatialCoverage] = []
    temporal_coverage: TemporalCoverage | None = None
    distributions: list[Distribution] = []
    licence: LicenceDocument | None = None
    language: list[str] = []

    landing_page: str | None = None
    version: str | None = None
    version_notes: InternationalString | None = None
    conforms_to: list[Standard] = []
    creator: Agent | None = None
    access_rights: str | None = None
    status: str | None = None
    provenance: list[ProvenanceStatement] = []
    quality_annotations: list[QualityAnnotation] = []
    extras: dict[str, str] = {}
```

`extras` is string-to-string, not `dict[str, Any]`. It is stored in a
`MAP(VARCHAR, VARCHAR)` column, so a non-string value could not be read back
as what went in. `put()` raises `TypeError` for one; coercing would store
`None` as the string `"None"` without a word. Encode structured values
yourself:

```python
ds.extras = {"provenance": json.dumps({"run": 7, "clean": True})}
...
meta = json.loads(ds.extras["provenance"])
```

---

## OpenDataset

Generic dataset (DCAT-AP `dcat:Dataset`). Use for datasets from CKAN portals,
manual curation, or any source that doesn't fit a more specific variant.

```python
@define
class OpenDataset(BaseDataset):
    kind: ClassVar[Literal["open"]] = "open"
```

**Example:**

```python
sc.OpenDataset(
    identifier="co2-emissions-2024",
    title=InternationalString(en="CO2 Emissions 2024", fr="Émissions CO2 2024"),
    description=InternationalString(en="Annual CO2 emissions by country"),
    publisher=sc.Agent(id="OECD", name=InternationalString(en="OECD")),
    themes=(sc.ConceptRef(uri="urn:myorg:themes/ENV"),),
    frequency=sc.Frequency(id="ANNUAL"),
    status="current",
)
```

---

## AggregateDataset

Statistical dataset aligned with StatDCAT-AP `stat:StatDataset`. Carries a
link to its SDMX structural metadata (DSD, codelists) via `sdmx_dataflow_urn`.

```python
@define
class AggregateDataset(BaseDataset):
    kind: ClassVar[Literal["aggregate"]] = "aggregate"
    dimension_names: list[InternationalString] = []  # human-readable dimension labels
    num_series: int | None = None                    # total number of series
    sdmx_dataflow_urn: str | None = None            # FK → sdmxlib dataflows.urn
    measure_dim: str | None = None                  # default pivot axis, set by ingest_data()
```

The `sdmx_dataflow_urn` links this catalog record to sdmxlib's structural tables
(DSDs, codelists). Structural filters like `has_code("geo", "DE")` JOIN through
this URN.

`measure_dim` is the dimension readers pivot the observations on, recorded by
`ingest_data()`: `store.registry.obs(flow).pivot(on=ds.measure_dim)`. It does not
change what is stored. `None` lets `pivot()` use the DSD's `MeasureDimension`.

**Example:**

```python
sc.AggregateDataset(
    identifier="ESTAT:NAMA_10_GDP(1.0)",
    title=InternationalString(en="GDP and main components (output, expenditure and income)"),
    description=InternationalString(en="National accounts data at current prices"),
    publisher=sc.Agent(id="ESTAT", name=InternationalString(en="Eurostat")),
    num_series=12000,
    sdmx_dataflow_urn="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ESTAT:NAMA_10_GDP(1.0)",
    measure_dim="na_item",
)
```

---

## MicrodataDataset

Survey or administrative microdata with variable-level metadata. Aligned with
DDI (Data Documentation Initiative) concepts expressed in DCAT-AP.

```python
@define
class MicrodataDataset(BaseDataset):
    kind: ClassVar[Literal["microdata"]] = "microdata"
    variables: list[Variable] = []
    sample_size: int | None = None
    universe: str | None = None
    collection_method: str | None = None
    anonymization: str | None = None
    survey_id: str | None = None
```

**Example:**

```python
sc.MicrodataDataset(
    identifier="lfs-2023-q4-pumf",
    title=InternationalString(en="Labour Force Survey 2023 Q4 — PUMF"),
    description=InternationalString(en="Public use microdata file"),
    variables=[
        sc.Variable(
            id="AGE",
            label=InternationalString(en="Age of respondent"),
            value_type="integer",
            universe="Civilians aged 15+",
        ),
        sc.Variable(
            id="LFSSTAT",
            label=InternationalString(en="Labour force status"),
            value_type="categorical",
            classification_id="CL_LFSSTAT",
        ),
    ],
    sample_size=56000,
    universe="Civilian non-institutionalized population aged 15+",
    collection_method="telephone interview",
    anonymization="suppression + rounding",
)
```

### Variable

```python
@define
class Variable:
    id: str                                         # REQUIRED
    label: InternationalString                      # REQUIRED
    description: InternationalString | None = None
    value_type: str | None = None                  # "integer", "categorical", "string", …
    classification_id: str | None = None           # links to a codelist
    universe: str | None = None
    question_text: str | None = None
    valid_range: str | None = None
    derivation: str | None = None                  # how derived (for derived variables)
```

---

## GeospatialDataset

Spatial dataset aligned with GeoDCAT-AP. Carries CRS, feature types, and
spatial resolution metadata.

```python
@define
class GeospatialDataset(BaseDataset):
    kind: ClassVar[Literal["geospatial"]] = "geospatial"
    crs: str | None = None                         # e.g. "EPSG:4326"
    spatial_resolution: str | None = None          # e.g. "1:50000"
    feature_types: list[str] = []                  # e.g. ["province", "census_division"]
    scale: str | None = None
```

**Example:**

```python
sc.GeospatialDataset(
    identifier="sgc-2021",
    title=InternationalString(en="Standard Geographical Classification 2021"),
    description=InternationalString(en="Official Canadian geographic boundaries"),
    crs="EPSG:4617",
    feature_types=["province", "census_division", "census_subdivision"],
    spatial_resolution="1:250000",
    spatial_coverage=[
        sc.SpatialCoverage(
            label=InternationalString(en="Canada"),
            bbox=(-141.0, 41.7, -52.6, 83.1),
        )
    ],
    distributions=[
        sc.Distribution(
            id="sgc-2021-shapefile",
            download_url="https://example.com/sgc2021.zip",
            format="Shapefile",
            media_type="application/zip",
        )
    ],
)
```

---

## PublicationDataset

Analytical publications, articles, and reports. Carries DOI, ISBN, authors,
and publication type.

```python
@define
class PublicationDataset(BaseDataset):
    kind: ClassVar[Literal["publication"]] = "publication"
    authors: list[str] = []
    doi: str | None = None
    isbn: str | None = None
    publication_type: str | None = None            # "release", "article", "report", …
    pages: int | None = None
    series_title: InternationalString | None = None
```

**Example:**

```python
sc.PublicationDataset(
    identifier="71-607-x2023017",
    title=InternationalString(en="Labour Force Survey, December 2023"),
    description=InternationalString(en="Monthly labour market snapshot"),
    authors=["Statistics Canada"],
    doi="10.25318/7160002201-eng",
    publication_type="release",
    series_title=InternationalString(en="Labour Force Survey"),
)
```

---

## Supporting types

### Agent

An organization or person responsible for a dataset. Aligned with `foaf:Agent`.

```python
@define
class Agent:
    name: InternationalString                      # REQUIRED
    id: str | None = None
    type: str | None = None                        # "organization", "person", …
    homepage: str | None = None
```

```python
sc.Agent(id="ESTAT", name=InternationalString(en="Eurostat"), homepage="https://ec.europa.eu/eurostat")
```

### ContactPoint

Contact information for a dataset. Aligned with `vcard:Kind`.

```python
@define
class ContactPoint:
    name: InternationalString | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None
```

### Distribution

A specific available form of a dataset (file, API endpoint, etc.).
Aligned with `dcat:Distribution`.

```python
@define
class Distribution:
    id: str                                        # REQUIRED
    title: InternationalString | None = None
    description: InternationalString | None = None
    access_url: str | None = None
    download_url: str | None = None
    media_type: str | None = None                  # IANA media type
    format: str | None = None                      # human-readable format name
    byte_size: int | None = None
    issued: datetime | None = None
    modified: datetime | None = None
    licence: LicenceDocument | None = None
    access_service: DataService | None = None
    distribution_type: str | None = None
```

### DataService

An API endpoint that serves one or more datasets. Aligned with `dcat:DataService`.

```python
@define
class DataService:
    id: str                                        # REQUIRED
    title: InternationalString                     # REQUIRED
    endpoint_url: str                              # REQUIRED
    description: InternationalString | None = None
    serves_dataset: str | None = None             # dataset identifier this service serves
    conforms_to: list[Standard] = []
```

### SKOS types

statcatlib models themes and controlled vocabularies as first-class SKOS artefacts.

#### ConceptScheme

A SKOS concept scheme — a vocabulary that owns a set of concepts.
Identity is the `uri` (global). Store via `store.put(scheme)`.

```python
@attrs.frozen
class ConceptScheme:
    uri: str                                        # REQUIRED — global identity
    label: InternationalString                      # REQUIRED
    definition: InternationalString | None = None
    concepts: dict[str, Concept] = {}              # notation → Concept
    top_concepts: tuple[str, ...] = ()             # notations of root concepts
```

```python
sc.ConceptScheme(
    uri="urn:sdmx:themes",
    label=InternationalString(en="Statistical Themes"),
    concepts={
        "ECONOMY": sc.Concept(notation="ECONOMY", label=InternationalString(en="Economy and finance")),
        "LABOUR":  sc.Concept(notation="LABOUR",  label=InternationalString(en="Labour market")),
    },
    top_concepts=("ECONOMY", "LABOUR"),
)
```

#### Concept

A SKOS concept within a scheme. Identity is `notation` within its scheme.
`broader`/`narrower`/`related` are notations of other concepts within the
same scheme.

```python
@attrs.frozen
class Concept:
    notation: str                                   # REQUIRED — local id within scheme
    label: InternationalString = {}
    alt_label: InternationalString = {}
    definition: InternationalString | None = None
    broader: tuple[str, ...] = ()                   # notations in same scheme
    narrower: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
```

#### ConceptRef

A typed reference to a concept. Carried on `Dataset.themes`, `.subject`, and
`.dataset_type`. Identity is the URI — two refs with the same URI compare
equal regardless of label, notation, or scheme_uri content.

```python
@attrs.frozen
class ConceptRef:
    uri: str                                        # REQUIRED — identity
    scheme_uri: str | None = None                   # hint (eq=False)
    notation: str | None = None                     # hint (eq=False)
    label: InternationalString = {}                 # populated at read time (eq=False)
```

```python
sc.ConceptRef(uri="urn:sdmx:themes/ECONOMY")
```

#### ConceptSchemeRef

A typed reference to a concept scheme. Carried on `Catalog.theme_taxonomy`.

```python
@attrs.frozen
class ConceptSchemeRef:
    uri: str                                        # REQUIRED — identity
    label: InternationalString = {}                 # hint (eq=False)
```

#### is_concept_in_scheme

Pure-Python validation helper (no database required):

```python
sc.is_concept_in_scheme(scheme, "ECONOMY")  # True if "ECONOMY" in scheme.concepts
```

### Frequency

Update frequency of a dataset. Aligned with `dct:Frequency`.

```python
@define
class Frequency:
    id: str                                        # REQUIRED — e.g. "ANNUAL", "MONTHLY"
    label: InternationalString | None = None
```

### TemporalCoverage

The time period covered by the dataset. Aligned with `dct:PeriodOfTime`.

```python
@define
class TemporalCoverage:
    start: datetime | None = None
    end: datetime | None = None
    label: InternationalString | None = None
```

### SpatialCoverage

The geographic area covered by the dataset. Aligned with `dct:Location`.

```python
@define
class SpatialCoverage:
    label: InternationalString | None = None
    uri: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # (west, south, east, north)
```

```python
sc.SpatialCoverage(
    label=InternationalString(en="Canada"),
    uri="http://www.geonames.org/6251999/canada.html",
    bbox=(-141.0, 41.7, -52.6, 83.1),
)
```

### LicenceDocument

A licence document associated with a dataset or distribution.

```python
@define
class LicenceDocument:
    id: str                                        # REQUIRED
    label: InternationalString | None = None
    uri: str | None = None
```

### Standard

A standard or specification a dataset or service conforms to.

```python
@define
class Standard:
    id: str                                        # REQUIRED
    label: InternationalString | None = None
    uri: str | None = None
```

### QualityAnnotation

A quality annotation on a specific dimension of a dataset. Aligned with
DQV (Data Quality Vocabulary).

```python
@define
class QualityAnnotation:
    dimension: str | None = None                   # quality dimension being annotated
    label: InternationalString | None = None
    body: InternationalString | None = None
```

### ProvenanceStatement

A provenance statement about a dataset's history or lineage. Aligned with
`dct:ProvenanceStatement`.

```python
@define
class ProvenanceStatement:
    label: InternationalString                     # REQUIRED
    uri: str | None = None
```

---

## InternationalString

Re-exported from sdmxlib. A multilingual string container — a dict keyed by
BCP-47 language codes.

```python
from sdmxlib import InternationalString

title = InternationalString(en="GDP and components", fr="PIB et composantes")
title.get("en")   # "GDP and components"
title.get("fr")   # "PIB et composantes"
title.get("de")   # None (or fallback if configured)
```

The catalog stores multilingual text in DuckDB `MAP(VARCHAR, VARCHAR)` columns.
Any language is queryable at any time via subscript syntax (e.g. `title['en']`).
No upfront language configuration is needed.

"""Core dataset types for the statcatlib domain model."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, TypeVar

from attrs import Factory, define
from sdmxlib import InternationalString

from statcatlib.model.agents import Agent, ContactPoint
from statcatlib.model.classification import Standard
from statcatlib.model.distribution import Distribution
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.quality import ProvenanceStatement, QualityAnnotation
from statcatlib.model.skos import ConceptRef
from statcatlib.model.spatial import SpatialCoverage
from statcatlib.model.temporal import Frequency, TemporalCoverage

__all__ = [
    "DATASET_KINDS",
    "AggregateDataset",
    "BaseDataset",
    "Dataset",
    "DatasetKind",
    "DatasetT",
    "GeospatialDataset",
    "MicrodataDataset",
    "OpenDataset",
    "PublicationDataset",
    "StatisticalDataset",
    "Variable",
]


#: The domain of the ``dataset.kind`` column. `BaseDataset` deliberately has
#: no ``kind``: it is never instantiated, so it carries no tag — only the five
#: concrete variants do, each annotated with its own single-member `Literal`.
#: Annotating a variant with `DatasetKind` itself would widen it back to "any
#: of the five" and lose the narrowing that makes the tag useful.
type DatasetKind = Literal["open", "aggregate", "microdata", "geospatial", "publication"]


@define
class BaseDataset:
    """Common fields for all dataset variants. Maps to DCAT-AP 3.0.

    Not intended for direct construction — use one of the five concrete
    variants: OpenDataset, AggregateDataset, MicrodataDataset,
    GeospatialDataset, PublicationDataset.
    """

    # --- Identity (always required) ---
    identifier: str
    title: InternationalString
    description: InternationalString

    # --- Core catalog metadata ---
    publisher: Agent | None = None
    contact_point: ContactPoint | None = None
    keywords: list[InternationalString] = Factory(list)
    themes: list[ConceptRef] = Factory(list)  # dcat:theme — URI refs, not owned
    subject: list[ConceptRef] = Factory(list)  # dcterms:subject — URI refs
    dataset_type: ConceptRef | None = None  # dcterms:type — URI ref
    issued: datetime | None = None
    modified: datetime | None = None
    frequency: Frequency | None = None
    spatial_coverage: list[SpatialCoverage] = Factory(list)
    temporal_coverage: TemporalCoverage | None = None
    distributions: list[Distribution] = Factory(list)
    licence: LicenceDocument | None = None
    language: list[str] = Factory(list)

    # --- Extended metadata ---
    landing_page: str | None = None
    version: str | None = None
    version_notes: InternationalString | None = None
    conforms_to: list[Standard] = Factory(list)
    creator: Agent | None = None
    access_rights: str | None = None
    status: str | None = None
    provenance: list[ProvenanceStatement] = Factory(list)
    quality_annotations: list[QualityAnnotation] = Factory(list)

    # --- Extensibility ---
    #: Free-form key/value pairs, both sides strings.
    #:
    #: Stored in a ``MAP(VARCHAR, VARCHAR)`` column, and declared to match
    #: rather than to flatter: as ``dict[str, object]`` it accepted values
    #: it could not return, so `None` read back as the *string* ``"None"``
    #: — present and truthy where an absence was stored (#114). This is
    #: also the shape the vocabulary uses; CKAN's package extras are
    #: string-valued key/value pairs.
    #:
    #: Structured values are the caller's to encode — ``json.dumps`` on
    #: the way in, ``json.loads`` on the way out — so that the lossy step
    #: is one they chose and can reverse.
    extras: dict[str, str] = Factory(dict)


@define
class OpenDataset(BaseDataset):
    """Generic open dataset — the default bucket for non-specialized data."""

    kind: ClassVar[Literal["open"]] = "open"


@define
class AggregateDataset(BaseDataset):
    """A statistical aggregate dataset (tabular, with dimensions and measures).

    Extends BaseDataset with StatDCAT-AP discovery fields and an optional link
    to the full SDMX structural record in sdmxlib's shared DuckDB tables.
    dimension_names carries enough info for catalog search without a JOIN.
    """

    kind: ClassVar[Literal["aggregate"]] = "aggregate"
    dimension_names: list[InternationalString] = Factory(list)
    num_series: int | None = None
    # SDMX link: FK into sdmxlib's dataflows.urn in the shared DuckDB
    sdmx_dataflow_urn: str | None = None
    # Default pivot axis for readers, recorded by ingest_data(). None = the DSD's MeasureDimension.
    measure_dim: str | None = None


@define
class Variable:
    """A variable in a microdata dataset."""

    id: str
    label: InternationalString
    description: InternationalString | None = None
    value_type: str | None = None
    classification_id: str | None = None
    universe: str | None = None
    question_text: str | None = None
    valid_range: str | None = None
    derivation: str | None = None


@define
class MicrodataDataset(BaseDataset):
    """A microdata (survey/record-level) dataset."""

    kind: ClassVar[Literal["microdata"]] = "microdata"
    variables: list[Variable] = Factory(list)
    sample_size: int | None = None
    universe: str | None = None
    collection_method: str | None = None
    anonymization: str | None = None
    survey_id: str | None = None


@define
class GeospatialDataset(BaseDataset):
    """A geospatial dataset."""

    kind: ClassVar[Literal["geospatial"]] = "geospatial"
    crs: str | None = None
    spatial_resolution: str | None = None
    feature_types: list[str] = Factory(list)
    scale: str | None = None


@define
class PublicationDataset(BaseDataset):
    """A publication (article, report, monograph)."""

    kind: ClassVar[Literal["publication"]] = "publication"
    authors: list[str] = Factory(list)
    doi: str | None = None
    isbn: str | None = None
    publication_type: str | None = None
    pages: int | None = None
    series_title: InternationalString | None = None


#: Every tag, in declaration order. Built from the classes rather than from
#: `DatasetKind`, so each variant's `Literal` is checked to be a member of the
#: alias — a typo'd tag is a type error here, not a silent mismatch with the
#: ``dataset.kind`` column.
DATASET_KINDS: tuple[DatasetKind, ...] = (
    OpenDataset.kind,
    AggregateDataset.kind,
    MicrodataDataset.kind,
    GeospatialDataset.kind,
    PublicationDataset.kind,
)

# Public union type alias — the type you work with day-to-day.
# Use BaseDataset as the generic bound in query() and other generic contexts.
type Dataset = OpenDataset | AggregateDataset | MicrodataDataset | GeospatialDataset | PublicationDataset

# TypeVar for pre-PEP-695 generic code
DatasetT = TypeVar("DatasetT", bound=BaseDataset)

# Backward compatibility alias — AggregateDataset is the successor
StatisticalDataset = AggregateDataset

"""Quality and provenance types for the statcatlib domain model."""

from attrs import define
from sdmxlib import InternationalString

__all__ = ["ProvenanceStatement", "QualityAnnotation"]


@define
class QualityAnnotation:
    """A quality statement about the dataset.

    Maps to dqv:hasQualityAnnotation in StatDCAT-AP. Intentionally
    simple — a label + optional body. Richer quality modelling
    can be added later.
    """

    dimension: str | None = None
    label: InternationalString | None = None
    body: InternationalString | None = None


@define
class ProvenanceStatement:
    """A statement about the origin/methodology of the dataset.

    Maps to dct:provenance in DCAT-AP.
    """

    label: InternationalString
    uri: str | None = None

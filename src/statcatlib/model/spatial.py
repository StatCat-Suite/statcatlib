"""Spatial types for the statcatlib domain model."""

from attrs import define
from sdmxlib import InternationalString

__all__ = ["SpatialCoverage"]


@define
class SpatialCoverage:
    """A geographic area covered by the dataset.

    Maps to dct:spatial → dct:Location in DCAT-AP.
    Can be a named region (with URI) or a bounding box.
    """

    label: InternationalString | None = None
    uri: str | None = None
    bbox: tuple[float, float, float, float] | None = None

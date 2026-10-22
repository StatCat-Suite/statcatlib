"""Classification types for the statcatlib domain model."""

from attrs import define
from sdmxlib import InternationalString

__all__ = ["Standard"]


@define
class Standard:
    """A standard or specification that the dataset conforms to.

    Maps to dct:conformsTo in DCAT-AP. Used to link to SDMX DSDs,
    CSV schemas, API specifications, etc.
    """

    id: str
    label: InternationalString | None = None
    uri: str | None = None

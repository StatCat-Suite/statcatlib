"""Licence types for the statcatlib domain model."""

from attrs import define
from sdmxlib import InternationalString

__all__ = ["LicenceDocument"]


@define
class LicenceDocument:
    """A licence under which the dataset is made available.

    Maps to dct:license → dct:LicenseDocument in DCAT-AP.
    """

    id: str
    label: InternationalString | None = None
    uri: str | None = None

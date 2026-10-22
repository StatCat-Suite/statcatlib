"""Agent and contact types for the statcatlib domain model."""

from attrs import define
from sdmxlib import InternationalString

__all__ = ["Agent", "ContactPoint"]


@define
class Agent:
    """An organization or person responsible for a dataset.

    Maps to foaf:Agent / dct:publisher / dct:creator in DCAT-AP.
    """

    name: InternationalString
    id: str | None = None
    type: str | None = None
    homepage: str | None = None


@define
class ContactPoint:
    """Contact information for a dataset.

    Maps to dcat:contactPoint → vcard:Kind in DCAT-AP.
    """

    name: InternationalString | None = None
    email: str | None = None
    phone: str | None = None
    url: str | None = None

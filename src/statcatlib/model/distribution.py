"""Distribution and DataService types for the statcatlib domain model."""

from datetime import datetime

from attrs import Factory, define
from sdmxlib import InternationalString

from statcatlib.model.classification import Standard
from statcatlib.model.licence import LicenceDocument

__all__ = ["DataService", "Distribution"]


@define
class DataService:
    """An API endpoint that serves one or more datasets.

    Maps to dcat:DataService in DCAT-AP. For SDMX sources, the
    endpoint_url is the SDMX REST base URL.
    """

    id: str
    title: InternationalString
    endpoint_url: str
    description: InternationalString | None = None
    serves_datasets: list[str] = Factory(list)
    conforms_to: list[Standard] = Factory(list)


@define
class Distribution:
    """A specific available form of a dataset.

    Maps to dcat:Distribution in DCAT-AP. A dataset may have
    multiple distributions: CSV download, API endpoint, visualization, etc.
    """

    id: str
    title: InternationalString | None = None
    description: InternationalString | None = None
    access_url: str | None = None
    download_url: str | None = None
    media_type: str | None = None
    format: str | None = None
    byte_size: int | None = None
    issued: datetime | None = None
    modified: datetime | None = None
    licence: LicenceDocument | None = None
    access_service: DataService | None = None
    distribution_type: str | None = None

"""Temporal types for the statcatlib domain model."""

from datetime import datetime

from attrs import define
from sdmxlib import InternationalString

__all__ = ["Frequency", "TemporalCoverage"]


@define
class TemporalCoverage:
    """A time interval that the dataset covers.

    Maps to dct:temporal → dct:PeriodOfTime in DCAT-AP.
    """

    start: datetime | None = None
    end: datetime | None = None
    label: InternationalString | None = None


@define
class Frequency:
    """Update frequency of a dataset.

    Maps to dct:accrualPeriodicity in DCAT-AP. The id should use
    the Dublin Core frequency vocabulary or SDMX CL_FREQ values
    where possible (e.g. "ANNUAL", "QUARTERLY", "MONTHLY").
    """

    id: str
    label: InternationalString | None = None

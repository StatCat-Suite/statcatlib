"""CatalogDescription and CatalogRecord types for the statcatlib domain model.

DCAT-AP describes a catalog as a top-level container of datasets. In the
statcatlib runtime, a *catalog* is the DuckDB-backed `statcatlib.Catalog`
store. The DCAT-style metadata record that describes such a catalog (its
title, publisher, list of datasets…) is `CatalogDescription` here.
This split avoids the name collision between "DCAT Catalog the model
record" and "Catalog the store".
"""

from datetime import datetime

from attrs import Factory, define
from sdmxlib import InternationalString

from statcatlib.model.agents import Agent
from statcatlib.model.classification import Standard
from statcatlib.model.dataset import BaseDataset
from statcatlib.model.distribution import DataService
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.skos import ConceptSchemeRef
from statcatlib.model.spatial import SpatialCoverage
from statcatlib.model.temporal import TemporalCoverage

__all__ = ["CatalogDescription", "CatalogRecord"]


@define
class CatalogRecord:
    """Metadata about a dataset's entry in the catalog.

    Maps to dcat:CatalogRecord in DCAT-AP. This is metadata about
    the catalog entry itself — when it was listed, where it was
    harvested from, etc. Optional — not all catalogs use this.
    """

    identifier: str
    primary_topic: BaseDataset
    listing_date: datetime | None = None
    modified: datetime | None = None
    source_catalog: str | None = None
    conforms_to: Standard | None = None


@define
class CatalogDescription:
    """A DCAT-style description record for a catalog of datasets.

    Maps to dcat:Catalog in DCAT-AP. This is metadata describing the
    catalog as a published artefact — title, publisher, list of datasets
    and services, licence, etc. The runtime store with the same
    DCAT-shaped role is `statcatlib.Catalog`; the two are separate
    concerns. Use this when serialising to / from DCAT-AP RDF or when
    a catalog needs a self-describing manifest record.
    """

    identifier: str
    title: InternationalString
    description: InternationalString
    publisher: Agent
    datasets: list[BaseDataset] = Factory(list)
    services: list[DataService] = Factory(list)
    records: list[CatalogRecord] = Factory(list)
    homepage: str | None = None
    language: list[str] = Factory(list)
    licence: LicenceDocument | None = None
    spatial_coverage: list[SpatialCoverage] = Factory(list)
    temporal_coverage: TemporalCoverage | None = None
    theme_taxonomy: list[ConceptSchemeRef] = Factory(list)
    issued: datetime | None = None
    modified: datetime | None = None

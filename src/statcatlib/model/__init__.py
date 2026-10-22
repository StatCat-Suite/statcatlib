"""Domain model for statcatlib."""

from statcatlib.model.agents import Agent, ContactPoint
from statcatlib.model.catalog import CatalogDescription, CatalogRecord
from statcatlib.model.classification import Standard
from statcatlib.model.dataset import (
    AggregateDataset,
    BaseDataset,
    Dataset,
    DatasetT,
    GeospatialDataset,
    MicrodataDataset,
    OpenDataset,
    PublicationDataset,
    StatisticalDataset,
    Variable,
)
from statcatlib.model.distribution import DataService, Distribution
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.quality import ProvenanceStatement, QualityAnnotation
from statcatlib.model.spatial import SpatialCoverage
from statcatlib.model.temporal import Frequency, TemporalCoverage
from statcatlib.model.skos import Concept, ConceptRef, ConceptScheme, ConceptSchemeRef, is_concept_in_scheme
from statcatlib.model.types import InternationalString

__all__ = [
    "Agent",
    "AggregateDataset",
    "BaseDataset",
    "CatalogDescription",
    "CatalogRecord",
    "Concept",
    "ConceptRef",
    "ConceptScheme",
    "ConceptSchemeRef",
    "ContactPoint",
    "DataService",
    "Dataset",
    "DatasetT",
    "Distribution",
    "Frequency",
    "GeospatialDataset",
    "InternationalString",
    "LicenceDocument",
    "MicrodataDataset",
    "OpenDataset",
    "ProvenanceStatement",
    "PublicationDataset",
    "QualityAnnotation",
    "SpatialCoverage",
    "Standard",
    "StatisticalDataset",
    "TemporalCoverage",
    "Variable",
    "is_concept_in_scheme",
]

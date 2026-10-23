"""Store backends for statcatlib."""

from statcatlib.store.scopes import (
    CodelistScope,
    CodelistsScope,
    CodeScope,
    ConceptScope,
    ConceptSchemeScope,
    ConceptSchemesScope,
    DatasetScope,
    DimensionInfo,
    DimensionScope,
    DimensionsScope,
    SchemeScope,
    ThemesScope,
)
from statcatlib.store.row import Row
from statcatlib.store.store import (
    VALID_INCLUDE_RELS,
    Catalog,
    CodeHit,
    CodelistIndexPolicy,
    CodesScope,
    ConceptHit,
    ConceptsScope,
    DatasetsScope,
    EmbeddingMetadata,
    Hit,
    SEARCH_ANNOTATION_TYPES,
    SEARCH_CONTEXT_ANNOTATION_TYPE,
    SEARCH_EXCLUDE_ANNOTATION_TYPE,
    IncompatibleCatalogError,
    IncompatibleIndexError,
    QueryBuilder,
    ReferenceMetadataBlend,
    ReferenceMetadataHit,
    ReferenceMetadataScope,
    SearchResult,
)
from statcatlib.store.xhtml import xhtml_to_markdown

"""DuckDB-backed catalog store."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import threading
import warnings
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, Self, TypedDict, overload, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

    from statcatlib.store.lance import (
        Bm25HitRow,
        IndexAnnotations,
        LanceIndex,
        LanguageLayout,
        SearchRow,
        VectorHitRow,
    )

import duckdb
import sdmxlib
import sdmxlib.model.codelist
import sdmxlib.model.datastructure
from attrs import Factory, define, field
from sdmxlib import InternationalString
from sdmxlib.model.registry import artefact_urn
from sdmxlib.storage.schema import SCHEMA_VERSION as _SDMX_SCHEMA_VERSION

from statcatlib.bundle import Embedded, normalize_lake_data_path, read_current_data_path
from statcatlib.model.agents import Agent, ContactPoint
from statcatlib.model.classification import Standard
from statcatlib.model.dataset import (
    DATASET_KINDS,
    AggregateDataset,
    BaseDataset,
    Dataset,
    GeospatialDataset,
    MicrodataDataset,
    OpenDataset,
    PublicationDataset,
    Variable,
)
from statcatlib.model.distribution import DataService, Distribution
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.quality import ProvenanceStatement, QualityAnnotation
from statcatlib.model.skos import Concept, ConceptRef, ConceptScheme
from statcatlib.model.spatial import SpatialCoverage
from statcatlib.model.temporal import Frequency, TemporalCoverage
from statcatlib.query import (
    CompileContext,
    MapFilter,
    PositionalFilter,
)
from statcatlib.store import schema as _catalog_schema
from statcatlib.store.row import Row
from statcatlib.store.scopes import (
    CodelistScope,
    CodelistsScope,
    ConceptSchemesScope,
    DatasetScope,
    DimensionsScope,
    ThemesScope,
)
from statcatlib.store.sentinel import make_unloaded
from statcatlib.store.xhtml import xhtml_to_markdown

__all__ = ["VALID_INCLUDE_RELS", "Catalog", "QueryBuilder", "Row", "SearchResult"]

# ---------------------------------------------------------------------------
# Type registry
# ---------------------------------------------------------------------------

_DATASET_TYPES: dict[str, type[BaseDataset]] = {
    "open": OpenDataset,
    "aggregate": AggregateDataset,
    "microdata": MicrodataDataset,
    "geospatial": GeospatialDataset,
    "publication": PublicationDataset,
    # backward compat for data stored with old class-name _type values
    "Dataset": OpenDataset,
    "StatisticalDataset": AggregateDataset,
}

#: The five concrete variants, for the runtime `issubclass` test in
#: `_build_filter_sql`. `query()` is handed a class object, whose type the
#: annotation has erased, so that one lookup cannot be resolved statically.
_DATASET_CLASSES = (OpenDataset, AggregateDataset, MicrodataDataset, GeospatialDataset, PublicationDataset)

# entity_type values that identify a dataset-level row in the search index.
# Used to exclude code-level rows (entity_type == "code") from dataset
# rankings, see issue #82. Built from the classes, so the two cannot drift.
_DATASET_ENTITY_TYPES: tuple[str, ...] = DATASET_KINDS

# Sidecar file that records which embedding model + prefixes produced the
# vector index in ``<catalog>/search/``.  Written by ``build_index`` and read
# back by ``open()`` so callers don't have to remember which model to load.
_EMBEDDINGS_META_FILE = "embeddings.json"
# v2: code-row schema gained dimension_id/dimension_label/code_id/code_label
# columns and the default for `embed_codes` flipped to False (#83, #84).
# v3: expanded sidecar to record embedding_dim, normalize, include_codes,
# embed_codes, max_codes_per_codelist, built_at, built_by, docs_indexed,
# and code_rows_indexed so query-side consumers can validate compatibility
# without re-encoding a probe text. Indexes built on v1/v2 remain readable;
# fields absent from older sidecars surface as ``None`` on
# `EmbeddingMetadata`.
# v4: added fts_only_codelist_urns (flattened from codelist_policies) plus
# include_concepts / embed_concepts / concept_rows_indexed /
# concept_rows_with_embeddings to record concept-row indexing. Backwards
# compatible — older sidecars still parse, missing fields default to None.
_EMBEDDINGS_META_VERSION = 4


#: Encoders whose document and query sides must be prefixed differently.
#:
#: e5 is trained asymmetrically: documents are embedded as ``passage: <text>``
#: and queries as ``query: <text>``. Embedding one side without the other
#: leaves the two in different regions of the space, and the symptom is not an
#: error — it is quietly worse recall, which is how #81 went unnoticed until a
#: downstream caller saw BM25 and vector top-K barely overlap.
#:
#: Lives here rather than in `statcatlib.cli` so the library and the CLI share one
#: source; the CLI used to hold the only copy, which left the Python API — the
#: interface #81 was actually reported against — unguarded.
#:
#: A name absent from this table gets no prefix, which is correct for
#: symmetric encoders (BGE-M3, jina-v3).
ASYMMETRIC_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-small": ("passage: ", "query: "),
    "intfloat/multilingual-e5-base": ("passage: ", "query: "),
    "intfloat/multilingual-e5-large": ("passage: ", "query: "),
    "intfloat/e5-small-v2": ("passage: ", "query: "),
    "intfloat/e5-base-v2": ("passage: ", "query: "),
    "intfloat/e5-large-v2": ("passage: ", "query: "),
}


class IncompatibleIndexError(ValueError):
    """Raised by `Catalog.check_index_compatible` on mismatch.

    Subclasses ``ValueError`` so existing broad-catch sites still work.
    """


class IncompatibleCatalogError(RuntimeError):
    """Raised on open when the catalog's ``sdmx`` schema is another sdmxlib's.

    sdmxlib treats its store as a cache: on a schema-version mismatch,
    `LocalRegistry` drops the ``sdmx`` schema and rebuilds it empty. Here
    that schema is not a cache. It is the structural half of the catalog,
    which ``statcat.dataset`` links into by URN, so the rebuild would leave
    every dataset pointing at nothing, and structural queries answering
    empty rather than failing.
    """


def _refuse_sdmx_schema_rebuild(conn: duckdb.DuckDBPyConnection) -> None:
    """Refuse to open a catalog whose SDMX structure sdmxlib would drop.

    Runs before `LocalRegistry.from_connection`, which is where the drop
    happens. A missing ``sdmx`` schema, or one at another version that holds
    no artefacts, has nothing to lose, so sdmxlib is left to create or
    rebuild it.

    Read-only opens are refused here too. sdmxlib would refuse them itself,
    but its message says to reopen read-write and rebuild in place, which
    for a catalog is the data loss this guard exists to prevent.

    Args:
        conn: The catalog's connection, before sdmxlib has touched it.

    Raises:
        IncompatibleCatalogError: The stored ``sdmx`` schema is at another
            version and holds artefacts.
    """
    try:
        row = conn.execute("""
            SELECT value FROM sdmx._meta WHERE key = 'schema_version'
        """).fetchone()
        stored = row[0] if row else None
        artefacts = conn.execute("""
            SELECT count(*) FROM sdmx.artefact
        """).fetchone()
    except duckdb.CatalogException:
        return
    n_artefacts = artefacts[0] if artefacts else 0
    if stored == _SDMX_SCHEMA_VERSION or n_artefacts == 0:
        return
    msg = (
        f"this catalog's SDMX structure is stored in sdmxlib schema {stored}, and this "
        f"build of sdmxlib uses schema {_SDMX_SCHEMA_VERSION}. sdmxlib migrates by dropping "
        f"the sdmx schema, which would delete the {n_artefacts} artefacts this catalog's "
        f"datasets link to. Rebuild the catalog from its sources with this version, or "
        f"open it with the statcatlib release that built it."
    )
    raise IncompatibleCatalogError(msg)


@define(frozen=True)
class EmbeddingMetadata:
    """Read-side view of a bundle's ``search/embeddings.json`` sidecar.

    Returned by `Catalog.embedding_metadata`. Fields that were not
    present in the on-disk sidecar (e.g. when reading a v2 file with a v3
    reader) come back as ``None`` rather than guesses.
    """

    model: str
    embedding_dim: int | None = None
    document_prefix: str = ""
    query_prefix: str = ""
    normalize: bool = True
    include_codes: bool | None = None
    embed_codes: bool | None = None
    max_codes_per_codelist: int | None = None
    built_at: datetime | None = None
    built_by: str | None = None
    docs_indexed: int | None = None
    code_rows_indexed: int | None = None
    fts_only_codelist_urns: tuple[str, ...] = ()
    include_concepts: bool | None = None
    embed_concepts: bool | None = None
    concept_rows_indexed: int | None = None
    concept_rows_with_embeddings: int | None = None


_DEFAULT_EMBED_BATCH_SIZE = 256
_EMBED_BATCH_SIZE_ENV = "STATCAT_EMBED_BATCH_SIZE"


def _embed_batch_size() -> int:
    """Resolve the encoder batch size from ``STATCAT_EMBED_BATCH_SIZE``.

    Falls back to ``_DEFAULT_EMBED_BATCH_SIZE`` when the env var is unset,
    empty, non-numeric, or non-positive — the default is conservative
    enough to run on modest hardware, so we never want to silently pick
    something worse.
    """
    raw = os.environ.get(_EMBED_BATCH_SIZE_ENV)
    if not raw:
        return _DEFAULT_EMBED_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_EMBED_BATCH_SIZE
    return value if value > 0 else _DEFAULT_EMBED_BATCH_SIZE


class _EmbeddingsSidecar(TypedDict):
    """The ``embeddings.json`` payload, as written.

    A `TypedDict` on the *write* side only. Here the shape is guaranteed —
    this module builds it — so declaring it buys key-name checking and stops
    a renamed field drifting away from the reader below.

    `_read_embeddings_meta` does not use it: that value comes off disk,
    possibly written by an older statcatlib, so its shape is unknown until
    checked. It returns `dict[str, object]` and `_parse_embeddings_meta`
    narrows every key.
    """

    version: int
    model: str
    embedding_dim: int | None
    document_prefix: str
    query_prefix: str
    normalize: bool
    include_codes: bool | None
    embed_codes: bool | None
    max_codes_per_codelist: int | None
    built_at: str | None
    built_by: str | None
    docs_indexed: int | None
    code_rows_indexed: int | None
    fts_only_codelist_urns: list[str]
    include_concepts: bool | None
    embed_concepts: bool | None
    concept_rows_indexed: int | None
    concept_rows_with_embeddings: int | None


def _read_embeddings_meta(search_dir: Path) -> dict[str, object] | None:
    """Return the parsed ``embeddings.json`` from *search_dir*, or None.

    Returns ``None`` when the file is missing, empty, or malformed — callers
    should treat that as "no embedding metadata", not as an error.
    """
    meta_path = search_dir / _EMBEDDINGS_META_FILE
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_embeddings_meta(
    search_dir: Path,
    *,
    model_name: str | None,
    document_prefix: str,
    query_prefix: str,
    embedding_dim: int | None = None,
    normalize: bool = True,
    include_codes: bool | None = None,
    embed_codes: bool | None = None,
    max_codes_per_codelist: int | None = None,
    built_at: datetime | None = None,
    built_by: str | None = None,
    docs_indexed: int | None = None,
    code_rows_indexed: int | None = None,
    fts_only_codelist_urns: Sequence[str] = (),
    include_concepts: bool | None = None,
    embed_concepts: bool | None = None,
    concept_rows_indexed: int | None = None,
    concept_rows_with_embeddings: int | None = None,
) -> None:
    """Persist embedding-build metadata to ``<search_dir>/embeddings.json``.

    No-op when *model_name* is ``None`` — without a model name there is no
    way for ``open()`` to reload a matching encoder, so there is no point
    pinning the prefixes either.
    """
    if model_name is None:
        return
    search_dir.mkdir(parents=True, exist_ok=True)
    payload: _EmbeddingsSidecar = {
        "version": _EMBEDDINGS_META_VERSION,
        "model": model_name,
        "embedding_dim": embedding_dim,
        "document_prefix": document_prefix,
        "query_prefix": query_prefix,
        "normalize": normalize,
        "include_codes": include_codes,
        "embed_codes": embed_codes,
        "max_codes_per_codelist": max_codes_per_codelist,
        "built_at": built_at.isoformat() if built_at is not None else None,
        "built_by": built_by,
        "docs_indexed": docs_indexed,
        "code_rows_indexed": code_rows_indexed,
        "fts_only_codelist_urns": list(fts_only_codelist_urns),
        "include_concepts": include_concepts,
        "embed_concepts": embed_concepts,
        "concept_rows_indexed": concept_rows_indexed,
        "concept_rows_with_embeddings": concept_rows_with_embeddings,
    }
    (search_dir / _EMBEDDINGS_META_FILE).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _statcatlib_version_tag() -> str:
    """Return ``"statcatlib==<version>"`` for the running install.

    Recorded in the sidecar's ``built_by`` so debugging "is this index
    stale?" gives a starting point. Falls back to ``"statcatlib==unknown"``
    when the metadata is unavailable (e.g. running from a checkout that
    has not been installed).
    """
    try:
        return f"statcatlib=={importlib.metadata.version('statcatlib')}"
    except importlib.metadata.PackageNotFoundError:
        return "statcatlib==unknown"


def _infer_embedding_dim(model: EmbeddingModel | None, dataset_rows: list[SearchRow]) -> int | None:
    """Best-effort embedding-dim probe.

    Prefer asking the encoder directly via the SentenceTransformers
    convention (``get_sentence_embedding_dimension()``); fall back to
    measuring the first encoded row when present. Returns ``None`` when
    no model was attached (BM25-only build) or when no embedding got
    produced (empty corpus).
    """
    if model is not None:
        getter: object = getattr(model, "get_sentence_embedding_dimension", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                value = getter()
                if isinstance(value, int) and value > 0:
                    return value
    for row in dataset_rows:
        embedding = row.get("embedding")
        if embedding is not None:
            with contextlib.suppress(TypeError):
                return len(embedding)
    return None


def _parse_embeddings_meta(data: dict[str, object]) -> EmbeddingMetadata | None:
    """Build an `EmbeddingMetadata` from a parsed sidecar dict.

    Accepts v2 and v3 payloads; missing keys map to ``None``. Returns
    ``None`` when the payload lacks a model name (without one there is no
    encoder to reload, so the rest is moot).
    """
    model = data.get("model")
    if not isinstance(model, str):
        return None

    def _opt_str(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    def _opt_int(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _opt_bool(key: str) -> bool | None:
        value = data.get(key)
        return value if isinstance(value, bool) else None

    built_at_raw = _opt_str("built_at")
    built_at: datetime | None = None
    if built_at_raw is not None:
        with contextlib.suppress(ValueError):
            built_at = datetime.fromisoformat(built_at_raw)

    normalize = _opt_bool("normalize")
    raw_fts = data.get("fts_only_codelist_urns")
    fts_only_codelist_urns: tuple[str, ...] = tuple(str(u) for u in raw_fts) if isinstance(raw_fts, list) else ()
    return EmbeddingMetadata(
        model=model,
        embedding_dim=_opt_int("embedding_dim"),
        document_prefix=_opt_str("document_prefix") or "",
        query_prefix=_opt_str("query_prefix") or "",
        normalize=True if normalize is None else normalize,
        include_codes=_opt_bool("include_codes"),
        embed_codes=_opt_bool("embed_codes"),
        max_codes_per_codelist=_opt_int("max_codes_per_codelist"),
        built_at=built_at,
        built_by=_opt_str("built_by"),
        docs_indexed=_opt_int("docs_indexed"),
        code_rows_indexed=_opt_int("code_rows_indexed"),
        fts_only_codelist_urns=fts_only_codelist_urns,
        include_concepts=_opt_bool("include_concepts"),
        embed_concepts=_opt_bool("embed_concepts"),
        concept_rows_indexed=_opt_int("concept_rows_indexed"),
        concept_rows_with_embeddings=_opt_int("concept_rows_with_embeddings"),
    )


# Column names for flat-column entity queries
class AgentRow(NamedTuple):
    """One ``statcat.agent`` row. See `DatasetRow` for why these are named."""

    id: str
    name: dict[str, str] | None
    type: str | None
    homepage: str | None


class DataServiceRow(NamedTuple):
    """One ``statcat.data_service`` row."""

    id: str
    title: dict[str, str] | None
    description: dict[str, str] | None
    endpoint_url: str | None


class DistributionRow(NamedTuple):
    """One ``statcat.distribution`` row, as `_ENTITY_COLUMNS` selects it.

    ``_read_distributions`` selects a narrower shape (no ``dataset_id``) and
    builds its `Distribution` inline; it does not come through here.
    """

    id: str
    dataset_id: str
    title: dict[str, str] | None
    description: dict[str, str] | None
    access_url: str | None
    download_url: str | None
    media_type: str | None
    format: str | None
    byte_size: int | None
    issued: datetime | None
    modified: datetime | None
    licence_id: str | None
    service_id: str | None
    distribution_type: str | None


class _ReadsEntity(Protocol):
    """`_EntitySpec` with its type parameters forgotten.

    The dispatch table's value type depends on its key — `Agent` maps to a
    spec building `Agent` — which no `dict` can state. Something has to be
    forgotten; this protocol chooses *what*.

    Erasing here rather than to `_EntitySpec[Any, Any]` keeps `Any` out of
    the table entirely. `build` returns `object`, so a caller that skips the
    narrowing gets a type error rather than an `Any` flowing on unchecked —
    the erasure is made visible instead of contagious. ``raw`` is
    `Sequence[object]` for the same reason: `NamedTuple._make` takes an
    iterable, so the untyped row DuckDB hands back never needs spelling as
    `Any` to reach its row type.
    """

    @property
    def table(self) -> str: ...
    @property
    def filter_cols(self) -> Mapping[str, str]: ...
    @property
    def columns(self) -> str: ...
    def build(self, raw: Sequence[object]) -> object: ...


@define(frozen=True)
class _EntitySpec[R: NamedTuple, E]:
    """Everything the query path needs to read one non-dataset entity.

    One object holds the table, columns, row type, filter columns and
    reconstructor, so they cannot drift apart the way five class-keyed dicts
    kept in step by hand could. `DatasetRow` does the same for the dataset
    columns.

    Being generic is what does the work. ``row_type`` and ``reconstruct``
    share ``R``, so pairing a row type with the wrong reconstructor is a
    type error *at the spec*, where the mistake is made. Without the shared
    ``R`` it would typecheck cleanly and surface as an `AttributeError`
    thousands of lines away, inside the query loop.

    The specs must therefore be bound *unannotated* (see below). Writing
    ``_AGENT_SPEC: _EntitySpec[Any, Any] = ...`` supplies `Any` as the
    expected type, `R` unifies with it, and the mismatch is accepted
    again — the annotation would silently undo the checking.

    Bounding ``R`` by `NamedTuple` is what makes `columns` derivable. A
    `Protocol` carrying ``_fields`` does not serve: declared as an instance
    attribute, pyrefly rejects the specialization.

    Attributes:
        table: The DuckDB table name, unqualified.
        row_type: The `NamedTuple` raw rows are wrapped in.
        reconstruct: Builds the domain object from one wrapped row.
        filter_cols: Supported ``query()`` keyword → SQL column.
    """

    table: str
    row_type: type[R]
    reconstruct: Callable[[R], E]
    filter_cols: Mapping[str, str]

    @property
    def columns(self) -> str:
        """The SELECT list, derived from `row_type` so the two cannot drift."""
        return ", ".join(self.row_type._fields)

    def build(self, raw: Sequence[object]) -> E:
        """Wrap one raw DuckDB row and reconstruct the entity from it."""
        return self.reconstruct(self.row_type._make(raw))


def _map_to_istring(m: dict[str, str] | None) -> InternationalString | None:
    """Convert a DuckDB MAP dict to InternationalString."""
    if m is None:
        return None
    return InternationalString.of(m)


@runtime_checkable
class EmbeddingModel(Protocol):
    """What statcatlib needs of an embedding model: one ``encode`` method.

    Any object with a matching ``encode`` works: a
    ``sentence_transformers.SentenceTransformer``, a remote client, a stub in a
    test. statcatlib passes either one string or a list of strings, and reads
    the result through `as_vector` and `as_vectors`, which do the one
    narrowing.

    ``encode`` returns `object` because real encoders declare wide returns:
    ``SentenceTransformer.encode`` includes ``dict[str, Tensor]`` in its union.
    A ``runtime_checkable`` Protocol checks that the method exists, never its
    signature, so an `isinstance` check cannot narrow the result either.

    The two overloads mirror the two shapes statcatlib passes. A single
    ``str | list[str]`` parameter would not match ``SentenceTransformer.encode``,
    which is overloaded seven ways with no overload taking the whole union.

    The contract names no keyword argument, so an encoder that takes only
    ``encode(text)`` satisfies it. statcatlib passes none; sentence-transformers
    draws its progress bar only under INFO or DEBUG logging.
    """

    @overload
    def encode(self, sentences: str, /) -> object: ...

    @overload
    def encode(self, sentences: list[str], /) -> object: ...


def as_vector(encoded: object) -> list[float]:
    """Narrow one encoder result to a vector.

    Raises:
        TypeError: The encoder returned something that is not an iterable of
            numbers. `str`, `bytes` and `Mapping` are rejected up front —
            each is iterable but over the wrong thing, and a `Mapping` is the
            live case: `SentenceTransformer.encode` can return
            ``dict[str, Tensor]``, which would otherwise iterate its keys and
            fail on `float("a")` with a `ValueError` naming nothing useful.

    Args:
        encoded: Whatever `EmbeddingModel.encode` returned for one text.

    Returns:
        The vector, as a list of floats.
    """
    if isinstance(encoded, (str, bytes, Mapping)) or not isinstance(encoded, Iterable):
        msg = f"embedding model returned {type(encoded).__name__}, expected an iterable of floats"
        raise TypeError(msg)
    try:
        return [float(x) for x in encoded]
    except (TypeError, ValueError) as exc:
        msg = f"embedding model returned {type(encoded).__name__} of non-numbers"
        raise TypeError(msg) from exc


def as_vectors(encoded: object) -> list[list[float]]:
    """Narrow a batch encoder result to one vector per input.

    Args:
        encoded: Whatever `EmbeddingModel.encode` returned for a batch.

    Returns:
        One vector per input text, in order.

    Raises:
        TypeError: As `as_vector`, for the batch itself or any row in it.
    """
    if isinstance(encoded, (str, bytes, Mapping)) or not isinstance(encoded, Iterable):
        msg = f"embedding model returned {type(encoded).__name__}, expected an iterable of vectors"
        raise TypeError(msg)
    return [as_vector(row) for row in encoded]


def _istring_to_map(s: InternationalString | None) -> dict[str, str] | None:
    """Convert InternationalString to a Python dict for DuckDB MAP param."""
    if s is None:
        return None
    return {lang: s[lang] for lang in s}


@define
class _QueryContext(CompileContext):
    """statcatlib-specific compile context.

    Extends `statcatlib.query.CompileContext` with statcatlib-flavored accumulators.
    The base class tracks shared state across predicates that need it (joins,
    join-alias dedup); this subclass adds:

    - **WHERE accumulator** — predicates' fragments collect here. The base
      CompileContext only carries joins because each predicate's
      ``to_sql(ctx)`` returns its own WHERE fragment + params; the compiler
      ANDs them together. We accumulate here for convenience so the
      ``join_clause`` / ``where_clause`` properties produce the final
      stitched SQL.

    Alias generation, schema resolution, and join-dedup live on the base
    `CompileContext`.
    """

    wheres: list[str] = field(factory=list)
    where_params: list[object] = field(factory=list)

    def add_where(self, sql: str, *filter_params: object) -> None:
        """Append a WHERE clause fragment and its parameters."""
        self.wheres.append(sql)
        self.where_params.extend(filter_params)

    @property
    def join_clause(self) -> str:
        return " ".join(self.join_sqls)

    @property
    def where_clause(self) -> str:
        return ("WHERE " + " AND ".join(self.wheres)) if self.wheres else ""

    @property
    def params(self) -> list[object]:
        """All parameters in execution order: joins first, then WHERE params."""
        return [*self.join_params, *self.where_params]


@define
class _BatchContext:
    """Pre-fetched relationship data for batch dataset reconstruction."""

    agents: dict[str, Agent]
    contacts: dict[str, ContactPoint]
    frequencies: dict[str, Frequency]
    licences: dict[str, LicenceDocument]
    themes: dict[str, list[ConceptRef]]
    subject: dict[str, list[ConceptRef]]
    dataset_type: dict[str, ConceptRef]
    keywords: dict[str, list[InternationalString]]
    spatial: dict[str, list[SpatialCoverage]]
    distributions: dict[str, list[Distribution]]
    standards: dict[str, list[Standard]]
    quality: dict[str, list[QualityAnnotation]]
    provenance: dict[str, list[ProvenanceStatement]]
    dimension_names: dict[str, list[InternationalString]]
    variables: dict[str, list[Variable]]
    feature_types: dict[str, list[str]]
    authors: dict[str, list[str]]


@define(frozen=True)
class _DimensionIds:
    """FK IDs returned by ``_upsert_dimension_records``."""

    publisher: str | None = None
    creator: str | None = None
    contact: str | None = None
    frequency: str | None = None
    licence: str | None = None


class DatasetRow(NamedTuple):
    """One ``statcat.dataset`` row, with named fields.

    The single source for the ``dataset`` column order: the SELECT list and the
    two reconstruction paths all derive from ``_fields`` below, so they cannot
    disagree. A hand-maintained SELECT list could swap two adjacent columns of
    the same type, such as ``collection_method`` and ``anonymization``, and no
    type checker or test would notice.

    Types follow the DDL in ``store/schema.py``: ``MAP(VARCHAR, VARCHAR)``
    arrives as ``dict[str, str]``, ``TEXT[]`` as ``list[str]``, ``TIMESTAMP``
    as ``datetime``.
    """

    identifier: str
    kind: str
    title: dict[str, str]
    description: dict[str, str]
    version_notes: dict[str, str] | None
    publisher_id: str | None
    creator_id: str | None
    contact_id: str | None
    frequency_id: str | None
    licence_id: str | None
    temporal_start: datetime | None
    temporal_end: datetime | None
    status: str | None
    access_rights: str | None
    issued: datetime | None
    modified: datetime | None
    landing_page: str | None
    version: str | None
    language: list[str] | None
    num_series: int | None
    sdmx_dataflow_urn: str | None
    measure_dim: str | None
    sample_size: int | None
    universe: str | None
    collection_method: str | None
    anonymization: str | None
    survey_id: str | None
    crs: str | None
    spatial_resolution: str | None
    scale: str | None
    doi: str | None
    isbn: str | None
    publication_type: str | None
    pages: int | None
    series_title: dict[str, str] | None
    dataset_type: str | None
    extras: dict[str, str] | None


#: Derived, never written out. `_fields` is the order; the SELECT, the INSERT
#: column list, its placeholders and its upsert assignments all follow it.
_DATASET_COLUMN_SQL = ", ".join(DatasetRow._fields)
_DATASET_PLACEHOLDERS = ", ".join(["?"] * len(DatasetRow._fields))
#: Every column but the primary key, for ``ON CONFLICT DO UPDATE``.
_DATASET_UPSERT_SET = ",\n                ".join(f"{c} = excluded.{c}" for c in DatasetRow._fields[1:])


def _assemble_dataset(
    row: DatasetRow,
    ctx: _BatchContext,
    *,
    include_rels: frozenset[str] | None = None,
) -> BaseDataset:
    """Assemble a single dataset from a row using pre-fetched batch context.

    Args:
        row: One ``statcat.dataset`` row, in the column order this module's SELECT
            list declares.
        ctx: Relationship data already fetched for the whole result set, so
            assembling N datasets costs one query per relationship rather than N.
        include_rels: When ``None`` all relationships are populated from *ctx*. When a frozenset, only named
            relationships are populated; the rest are set to the ``UNLOADED`` sentinel so that accidental
            access raises ``NotLoadedError`` rather than silently returning empty data.

    Returns:
        The reconstructed dataset, typed by the row's ``kind`` column.
    """
    # Read straight off the row — named, so a moved column cannot
    # silently land in the wrong parameter.

    title = _map_to_istring(row.title) or InternationalString()
    description = _map_to_istring(row.description) or InternationalString()
    version_notes = _map_to_istring(row.version_notes)
    extras = dict(row.extras) if row.extras else {}

    temporal_coverage: TemporalCoverage | None = None
    if row.temporal_start is not None or row.temporal_end is not None:
        temporal_coverage = TemporalCoverage(start=row.temporal_start, end=row.temporal_end)

    lang_list: list[str] = list(row.language) if row.language else []

    def _rel(field: str, loaded_value: Any, unloaded_value: Any = None) -> Any:
        """Return loaded_value if field is included, else the UNLOADED sentinel.

        For FK-based fields (publisher, creator, frequency, licence) pass the already-
        resolved value as *loaded_value* and ``None`` as *unloaded_value* —
        when the FK column itself is NULL we always return None regardless of
        include_rels, because we KNOW from the main row there is no value.
        """
        if include_rels is None or field in include_rels:
            return loaded_value
        # FK is null → genuinely no value; skip UNLOADED
        if unloaded_value is None and loaded_value is None:
            return None
        return make_unloaded(field)

    # Dynamic construction: the field set depends on the dataset row.kind, so
    # this is splatted into one of five constructors. The `Any` is a local,
    # not a signature — it dies on the line below.
    base_kwargs: dict[str, Any] = {
        "identifier": row.identifier,
        "title": title,
        "description": description,
        # FK-based: if FK is null the dataset genuinely has no publisher/etc.
        "publisher": _rel(
            "publisher",
            ctx.agents.get(row.publisher_id) if row.publisher_id else None,
            make_unloaded("publisher") if row.publisher_id else None,
        ),
        "creator": _rel(
            "creator",
            ctx.agents.get(row.creator_id) if row.creator_id else None,
            make_unloaded("creator") if row.creator_id else None,
        ),
        "contact_point": _rel(
            "contact_point",
            ctx.contacts.get(row.contact_id) if row.contact_id else None,
            make_unloaded("contact_point") if row.contact_id else None,
        ),
        "frequency": _rel(
            "frequency",
            ctx.frequencies.get(row.frequency_id) if row.frequency_id else None,
            make_unloaded("frequency") if row.frequency_id else None,
        ),
        "licence": _rel(
            "licence",
            ctx.licences.get(row.licence_id) if row.licence_id else None,
            make_unloaded("licence") if row.licence_id else None,
        ),
        # Bridge-table list fields: always UNLOADED when not included
        "keywords": _rel("keywords", ctx.keywords.get(row.identifier, []), "sentinel"),
        "themes": _rel("themes", ctx.themes.get(row.identifier, []), "sentinel"),
        "subject": _rel("subject", ctx.subject.get(row.identifier, []), "sentinel"),
        "dataset_type": _rel("dataset_type", ctx.dataset_type.get(row.identifier), "sentinel"),
        "spatial_coverage": _rel("spatial_coverage", ctx.spatial.get(row.identifier, []), "sentinel"),
        "distributions": _rel("distributions", ctx.distributions.get(row.identifier, []), "sentinel"),
        "conforms_to": _rel("conforms_to", ctx.standards.get(row.identifier, []), "sentinel"),
        "provenance": _rel("provenance", ctx.provenance.get(row.identifier, []), "sentinel"),
        "quality_annotations": _rel("quality_annotations", ctx.quality.get(row.identifier, []), "sentinel"),
        # Scalar fields — always loaded from the main row
        "issued": row.issued,
        "modified": row.modified,
        "temporal_coverage": temporal_coverage,
        "language": lang_list,
        "landing_page": row.landing_page,
        "version": row.version,
        "version_notes": version_notes,
        "access_rights": row.access_rights,
        "status": row.status,
        "extras": extras,
    }

    if row.kind == "aggregate":
        return AggregateDataset(
            **base_kwargs,
            dimension_names=_rel("dimension_names", ctx.dimension_names.get(row.identifier, []), "sentinel"),
            num_series=row.num_series,
            sdmx_dataflow_urn=row.sdmx_dataflow_urn,
            measure_dim=row.measure_dim,
        )
    if row.kind == "microdata":
        return MicrodataDataset(
            **base_kwargs,
            variables=_rel("variables", ctx.variables.get(row.identifier, []), "sentinel"),
            sample_size=row.sample_size,
            universe=row.universe,
            collection_method=row.collection_method,
            anonymization=row.anonymization,
            survey_id=row.survey_id,
        )
    if row.kind == "geospatial":
        return GeospatialDataset(
            **base_kwargs,
            crs=row.crs,
            spatial_resolution=row.spatial_resolution,
            feature_types=_rel("feature_types", ctx.feature_types.get(row.identifier, []), "sentinel"),
            scale=row.scale,
        )
    if row.kind == "publication":
        series_title = _map_to_istring(row.series_title)
        return PublicationDataset(
            **base_kwargs,
            authors=_rel("authors", ctx.authors.get(row.identifier, []), "sentinel"),
            doi=row.doi,
            isbn=row.isbn,
            publication_type=row.publication_type,
            pages=row.pages,
            series_title=series_title,
        )
    return OpenDataset(**base_kwargs)


def _reconstruct_agent(row: AgentRow) -> Agent:
    name = _map_to_istring(row.name) or InternationalString()
    return Agent(id=row.id, name=name, type=row.type, homepage=row.homepage)


def _reconstruct_data_service(row: DataServiceRow) -> DataService:
    title = _map_to_istring(row.title) or InternationalString()
    description = _map_to_istring(row.description)
    # Load serves_datasets from service_dataset bridge
    return DataService(id=row.id, title=title, description=description, endpoint_url=row.endpoint_url or "")


def _reconstruct_distribution(row: DistributionRow) -> Distribution:
    return Distribution(
        id=row.id,
        title=_map_to_istring(row.title),
        description=_map_to_istring(row.description),
        access_url=row.access_url,
        download_url=row.download_url,
        media_type=row.media_type,
        format=row.format,
        byte_size=row.byte_size,
        issued=row.issued,
        modified=row.modified,
        distribution_type=row.distribution_type,
    )


#: Bound without annotations on purpose. An `_EntitySpec[Any, Any]` hint here
#: would feed `Any` in as the expected type and switch off the very check
#: these bindings exist to get — see `_EntitySpec`.
_AGENT_SPEC = _EntitySpec(
    table="agent",
    row_type=AgentRow,
    reconstruct=_reconstruct_agent,
    filter_cols={"id": "id", "type": "type", "homepage": "homepage", "name": "name['en']"},
)
_DATA_SERVICE_SPEC = _EntitySpec(
    table="data_service",
    row_type=DataServiceRow,
    reconstruct=_reconstruct_data_service,
    filter_cols={"id": "id", "endpoint_url": "endpoint_url"},
)
_DISTRIBUTION_SPEC = _EntitySpec(
    table="distribution",
    row_type=DistributionRow,
    reconstruct=_reconstruct_distribution,
    filter_cols={
        "id": "id",
        "dataset_id": "dataset_id",
        "format": "format",
        "media_type": "media_type",
        "access_url": "access_url",
    },
)

#: The non-dataset entity types, and how to read each one. `Any` here erases
#: the per-key relationship a `dict` cannot express; the bindings above are
#: already checked, so nothing can drift through it.
_ENTITY_SPECS: dict[type, _ReadsEntity] = {
    Agent: _AGENT_SPEC,
    DataService: _DATA_SERVICE_SPEC,
    Distribution: _DISTRIBUTION_SPEC,
}


def _sql_literal(v: object) -> str:
    """Format a Python value as a DuckDB SQL literal for bulk VALUES clauses."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat()}'"
    if isinstance(v, dict):
        # MAP literal for DuckDB
        entries = ", ".join(f"'{k}': '{str(val).replace(chr(39), chr(39) + chr(39))}'" for k, val in v.items())
        return f"MAP {{{entries}}}"
    s = str(v).replace("'", "''")
    return f"'{s}'"


def _validated_extras(extras: Mapping[str, str]) -> dict[str, str] | None:
    """Check `BaseDataset.extras` is string-to-string before it is stored.

    The column is ``MAP(VARCHAR, VARCHAR)``. Coercing with ``str()`` would
    make every value storable and none of them recoverable: `None` would come
    back as the string ``"None"``, truthy and indistinguishable from a stored
    ``"None"``, and lists and dicts as Python ``repr``, which only
    ``ast.literal_eval`` can reverse and which other tools reading this
    column do not understand.

    So the write is refused instead. Callers wanting structured values
    encode them — ``json.dumps`` in, ``json.loads`` out — which keeps the
    lossy step visible and reversible on the side that chose it.

    Args:
        extras: The dataset's ``extras`` mapping.

    Returns:
        The mapping as a plain dict, or ``None`` when it is empty, which is
        what the column stores for "nothing here".

    Raises:
        TypeError: If any key or value is not a `str`. Names the offending
            key and the type found, since the mapping may be large.
    """
    if not extras:
        return None
    for key, value in extras.items():
        if not isinstance(key, str):
            msg = f"extras keys must be str, got {type(key).__name__}: {key!r}"
            raise TypeError(msg)
        if not isinstance(value, str):
            msg = (
                f"extras[{key!r}] must be str, got {type(value).__name__}. "
                "The column is MAP(VARCHAR, VARCHAR); encode structured "
                "values yourself, e.g. json.dumps(value)."
            )
            raise TypeError(msg)
    return dict(extras)


def _agent_id(agent: Agent) -> str:
    """Derive a stable ID for an agent."""
    return (
        agent.id
        or hashlib.md5(  # noqa: S324
            (agent.name.get("en") or agent.name.get("fr") or "").encode()
        ).hexdigest()[:12]
    )


def _contact_id(contact: ContactPoint) -> str:
    """Derive a stable ID for a contact."""
    key = contact.email or contact.url or ""
    if contact.name:
        key += contact.name.get("en") or contact.name.get("fr") or ""
    return hashlib.md5(key.encode()).hexdigest()[:12]  # noqa: S324


# ---------------------------------------------------------------------------
# Sortable columns mapping
# ---------------------------------------------------------------------------

_SORTABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "identifier",
        "kind",
        "status",
        "issued",
        "modified",
        "publisher_id",
        "frequency_id",
    }
)

_MAP_SORTABLE_COLUMNS: frozenset[str] = frozenset({"title"})

# Columns allowed in .select() / .scalars() lightweight projections
_SELECTABLE_COLUMNS: frozenset[str] = frozenset(
    {
        "identifier",
        "kind",
        "status",
        "issued",
        "modified",
        "landing_page",
        "version",
    }
)
_MAP_SELECTABLE_COLUMNS: frozenset[str] = frozenset({"title", "description"})

# ---------------------------------------------------------------------------
# Internal query result container
# ---------------------------------------------------------------------------


@define
class _SortSpec:
    field: str
    desc: bool = False
    nulls: str | None = None
    lang: str | None = None


#: What a hit from `Catalog.search` can carry. The cross-kind search fans out
#: over four typed accessors, so its result is not one payload type — but it is
#: not `object` either. Naming the union lets a caller match exhaustively on
#: `hit.payload` instead of being handed something opaque and told to check
#: `hit.kind`.
#:
#: A union rather than `Hit[object]` because attrs generics are invariant:
#: `Hit` is frozen and `SearchResult.items` could be a `Sequence`, and
#: basedpyright reported the parameter invariant, since the synthesised
#: `__init__` puts `T` in a parameter position. `Hit[CodeHit]` is therefore not
#: a `Hit[object]`, and no amount of freezing changes that.
type AnyHit = Hit[BaseDataset] | Hit[CodeHit] | Hit[ConceptHit] | Hit[ReferenceMetadataHit]


@define
class _QueryResult:
    """One page of a query: the matching identifiers, and how many there were.

    ``rows`` holds *identifiers*, not row tuples, whatever the name suggests.
    Every consumer treats them as ids (``ds_by_id[ident] for ident in
    qr.rows``).
    """

    total: int
    rows: list[str]
    scores: dict[str, float] = Factory(dict)


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@define
class SearchResult[T]:
    """Container for query results with pagination metadata."""

    items: list[T]
    total: int
    facets: dict[str, dict[str, int]] = Factory(dict)
    offset: int = 0
    scores: dict[str, float] = Factory(dict)


@define(frozen=True)
class CodeHit:
    """Payload for a code-search hit.

    Score, language, and ranking metadata live on the enclosing
    `Hit`; this type is the pure structural projection of one
    code row — exactly what an LLM agent translating a phrase like
    "Ontario" into a ``(dimension_id, code_id)`` filter needs.
    """

    dataset_id: str
    dimension_id: str
    dimension_label: str
    code_id: str
    code_label: str
    text: str
    code_urn: str = ""
    codelist_urn: str = ""
    parent_id: str = ""
    depth: int = 0


@define(frozen=True)
class ConceptHit:
    """Payload for a concept-search hit.

    SDMX Concepts carry definitional text (``description``) which is
    where vector search earns its cost — a query like "income
    inequality" finds "Gini coefficient" through its definition, not
    its label. The hit exposes both the structural identifiers
    (URNs, scheme, id) and the localised text.
    """

    concept_urn: str
    scheme_urn: str
    concept_id: str
    name: str
    description: str
    text: str


@define(frozen=True)
class Hit[T]:
    """A search hit — payload plus per-match ranking metadata.

    The discriminator ``kind`` lets a heterogeneous result list (e.g.
    `Catalog.search` over all entity kinds) be narrowed at runtime
    by a tagged match; the generic parameter ``T`` lets type-scoped
    accessors (``cat.codes.find`` etc.) narrow statically.

    Per-hit metadata that depends on the *query* (not the entity) lives
    here, not on ``payload``:

    - ``score`` — backend-native score (BM25 relevance, vector
      similarity, or fused RRF).
    - ``source`` — which retrieval path produced this hit; useful for
      hybrid-search explainability and telemetry.
    - ``highlights`` — per-field snippet extracts when available.
    - ``matched_field`` — which indexed column drove the rank.
    """

    kind: str
    score: float
    payload: T
    lang: str = "en"
    source: str = "bm25"
    highlights: Mapping[str, str] = Factory(dict)
    matched_field: str | None = None


# Columns projected out of LanceDB for code-row hits. Kept here so the
# BM25, semantic, and hybrid code-search paths agree on the row shape
# and the converter ``_row_to_code_hit`` can read whatever's present.
_CODE_ROW_COLUMNS: tuple[str, ...] = (
    "id",
    "dimension_id",
    "dimension_label",
    "code_id",
    "code_label",
    "code_urn",
    "codelist_urn",
    "parent_id",
    "depth",
    "text",
    "lang",
)


_CONCEPT_ROW_COLUMNS: tuple[str, ...] = (
    "id",
    "concept_urn",
    "scheme_urn",
    "concept_id",
    "text",
    "lang",
)


def _row_to_code_hit(
    row: SearchRow,
    *,
    fallback_lang: str,
    score: float,
    source: str = "bm25",
) -> Hit[CodeHit]:
    """Build a ``Hit[CodeHit]`` from a LanceDB row dict."""
    payload = CodeHit(
        dataset_id=row.get("id", "") or "",
        dimension_id=row.get("dimension_id", "") or "",
        dimension_label=row.get("dimension_label", "") or "",
        code_id=row.get("code_id", "") or "",
        code_label=row.get("code_label", "") or "",
        text=row.get("text", "") or "",
        code_urn=row.get("code_urn", "") or "",
        codelist_urn=row.get("codelist_urn", "") or "",
        parent_id=row.get("parent_id", "") or "",
        depth=row.get("depth", 0) or 0,
    )
    return Hit(
        kind="code",
        score=score,
        payload=payload,
        lang=row.get("lang", fallback_lang) or fallback_lang,
        source=source,
    )


def _row_to_concept_hit(
    row: SearchRow,
    *,
    fallback_lang: str,
    score: float,
    source: str = "bm25",
) -> Hit[ConceptHit]:
    """Build a ``Hit[ConceptHit]`` from a LanceDB row dict.

    The Lance ``text`` column is ``"<name> | <description>"``; for the
    typed payload we split on the first ``" | "`` so callers can get
    name and description back as separate fields without round-tripping
    to SQL.
    """
    text = row.get("text", "") or ""
    if " | " in text:
        name, _, description = text.partition(" | ")
    else:
        name, description = text, ""
    payload = ConceptHit(
        concept_urn=row.get("concept_urn", row.get("id", "")) or "",
        scheme_urn=row.get("scheme_urn", "") or "",
        concept_id=row.get("concept_id", "") or "",
        name=name,
        description=description,
        text=text,
    )
    return Hit(
        kind="concept",
        score=score,
        payload=payload,
        lang=row.get("lang", fallback_lang) or fallback_lang,
        source=source,
    )


@define(frozen=True)
class ReferenceMetadataHit:
    """Payload for a reference-metadata (methodology-document) search hit.

    One indexed chunk of a sectioned SDMX MetadataSet — a reference guide,
    a methodology report, a data-quality note. Unlike a code hit (a short
    controlled-vocabulary label), this is prose: the vector/BM25 target is
    the section text itself. The hit carries the identifiers needed to
    navigate back to the structured graph — ``set_urn`` (the document) and
    ``attribute_path`` (the section) — so a caller can fetch the full,
    typed section via `Catalog.metadata_report` or resolve the
    artefacts the document describes via the set's targets.
    """

    set_urn: str
    attribute_path: str
    section_title: str
    text: str
    chunk_index: int = 0


_REFMETA_ROW_COLUMNS: tuple[str, ...] = (
    "id",
    "set_urn",
    "attribute_path",
    "section_title",
    "text",
    "lang",
)


def _row_to_reference_metadata_hit(
    row: SearchRow,
    *,
    fallback_lang: str,
    score: float,
    source: str = "bm25",
) -> Hit[ReferenceMetadataHit]:
    """Build a ``Hit[ReferenceMetadataHit]`` from a LanceDB row dict.

    The Lance ``id`` is ``"<set_urn>#<attribute_path>#<chunk_index>"``;
    the trailing chunk index is parsed back onto the payload so callers can
    tell repeated chunks of one section apart.
    """
    chunk_index = 0
    raw_id = row.get("id", "") or ""
    tail = raw_id.rsplit("#", 1)
    if len(tail) == 2 and tail[1].isdigit():
        chunk_index = int(tail[1])
    payload = ReferenceMetadataHit(
        set_urn=row.get("set_urn", "") or "",
        attribute_path=row.get("attribute_path", "") or "",
        section_title=row.get("section_title", "") or "",
        text=row.get("text", "") or "",
        chunk_index=chunk_index,
    )
    return Hit(
        kind="reference_metadata",
        score=score,
        payload=payload,
        lang=row.get("lang", fallback_lang) or fallback_lang,
        source=source,
    )


# Reference-metadata chunking knobs. A section shorter than the floor is
# provenance-ish noise (a catalogue id, a URL) — skipped. Longer sections
# are packed into chunks near the target so no single embedded unit blows
# past a sentence-transformer's window (~512 tokens ≈ 2000 chars; we aim
# lower for headroom). Sectioned parsing (h2/h3 + markdown tables) upstream
# makes these boundaries cleaner, but the chunker degrades gracefully to
# paragraph/sentence splitting when a section is a single long blob.
_REFMETA_MIN_CHARS = 40
_REFMETA_CHUNK_CHARS = 1400


def _humanize_section(attribute_path: str) -> str:
    """Human-readable section label from an ``attribute_path`` leaf.

    ``"DATA_QUALITY_TOPIC"`` -> ``"Data Quality Topic"``; strips parent
    path and ``[n]`` sibling suffix.
    """
    leaf = attribute_path.rsplit(".", 1)[-1]
    bracket = leaf.find("[")
    if bracket != -1:
        leaf = leaf[:bracket]
    return leaf.replace("_", " ").title()


def _hard_wrap(piece: str, max_chars: int) -> list[str]:
    """Break an over-long token into fixed ``max_chars`` slices."""
    return [piece[i : i + max_chars] for i in range(0, len(piece), max_chars)]


def _atomic_units(text: str, max_chars: int) -> list[str]:
    """Paragraphs, further split on sentences (then hard-cut) when over budget."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        block = para.strip()
        if not block:
            continue
        if len(block) <= max_chars:
            units.append(block)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            units.extend(_hard_wrap(sentence, max_chars) if len(sentence) > max_chars else [sentence])
    return units


def _chunk_prose(text: str, *, max_chars: int = _REFMETA_CHUNK_CHARS) -> list[str]:
    """Split prose into chunks no longer than ``max_chars``.

    Greedy-pack paragraphs; a paragraph over the budget is split on
    sentence boundaries, and a sentence still over the budget is hard-cut.
    Deterministic and dependency-free — the point is bounded embeddable
    units, not perfect linguistic segmentation.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for unit in _atomic_units(text, max_chars):
        if not current:
            current = unit
        elif len(current) + len(unit) + 2 <= max_chars:
            current = f"{current}\n\n{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


@define(frozen=True)
class CodelistIndexPolicy:
    """Per-codelist override for `Catalog.build_index`.

    Lets a caller opt large codelists into the Lance search index even
    when their size would otherwise trip ``max_codes_per_codelist``, and
    decouple "should this codelist be indexed" from "should its codes get
    vector embeddings". Short proper-noun labels (geography, population
    characteristics) score better with Tantivy FTS than with vector
    similarity over short strings, and skipping the encode is what makes
    indexing a geography codelist of tens of thousands of codes take
    seconds rather than minutes, at a fraction of the disk.

    Attributes:
        urns: Codelist URNs this policy applies to.
        mode: How the listed codelists should be indexed:

            - ``"fts_only"`` — bypass ``max_codes_per_codelist``; rows are
              written to Lance with ``embedding=None`` regardless of the
              global ``embed_codes`` flag, so Tantivy FTS still finds them.
    """

    urns: tuple[str, ...] = field(converter=tuple)
    mode: str = "fts_only"


def _fts_only_urns(policies: Sequence[CodelistIndexPolicy]) -> set[str]:
    """Flatten codelist URNs from ``fts_only`` policies into a single set."""
    out: set[str] = set()
    for p in policies:
        if p.mode == "fts_only":
            out.update(p.urns)
    return out


def _code_index_filter(
    *,
    max_codes_per_codelist: int,
    fts_only_urns: set[str],
    codelist_urns: Sequence[str] | None,
    exclude_annotation_types: Sequence[str],
) -> tuple[str, dict[str, object]]:
    """WHERE clause + parameters selecting the codes `Catalog._collect_code_texts` indexes."""
    # Bypass the size cap for codelists that policy explicitly opts in.
    # Without the OR-branch, ``cs.n <= $max_codes`` would still drop a
    # large geography codelist even when the caller asked for it.
    params: dict[str, object] = {"max_codes": max_codes_per_codelist}
    size_clause = "cs.n <= $max_codes"
    if fts_only_urns:
        params["fts_only_urns"] = sorted(fts_only_urns)
        size_clause = "(cs.n <= $max_codes OR dc.codelist_urn IN (SELECT UNNEST($fts_only_urns)))"
    where_parts = ["d.sdmx_dataflow_urn IS NOT NULL", size_clause]
    if codelist_urns is not None:
        where_parts.append("dc.codelist_urn IN (SELECT UNNEST($codelist_urns))")
        params["codelist_urns"] = list(codelist_urns)
    if exclude_annotation_types:
        where_parts.append(
            "NOT EXISTS (SELECT 1 FROM sdmx.annotation xa WHERE xa.owner_urn = c.code_urn"
            " AND xa.annotation_type IN (SELECT UNNEST($exclude_types)))"
        )
        params["exclude_types"] = list(exclude_annotation_types)
    where_clause = " AND ".join(where_parts)
    return where_clause, params


@define(frozen=True)
class ReferenceMetadataBlend:
    """Blend a reference-metadata attribute into a code's search text.

    For each code, statcatlib joins ``sdmx.metadata_target`` →
    ``sdmx.metadata_value`` to pull the localised value of
    *attribute_path* and append it to the code's search text. Improves
    BM25 + vector recall on definitional queries when the catalog
    publishes concept definitions or footnotes via SDMX MetadataSets
    (e.g. an MSD carrying a ``FOOTNOTE_TEXT`` attribute).

    Attributes:
        attribute_path: Dotted MSD attribute id, e.g. ``"FOOTNOTE_TEXT"``.
        max_chars: Per-code truncation applied after joining multiple
            values with a single space. Defaults to 240 — long enough to
            carry one or two sentences without overwhelming the BM25
            term mix or the embedding.
    """

    attribute_path: str
    max_chars: int = 240


#: Annotation type whose localised text statcatlib folds into a code's search
#: corpus by default (see ``context_annotation_types`` on
#: `Catalog.build_index`). statcatlib's own namespaced convention —
#: producers attach it to carry disambiguating context for a code (e.g. its
#: nearest topical-universe label) so same-surface-label codes under
#: different universes rank correctly. Read per-language from
#: ``sdmx.annotation_text``, so each language row gets its own-language
#: context. Distinct from ``skos:altLabel`` on purpose, so consumers that
#: render altLabels as synonyms don't surface context text as one.
SEARCH_CONTEXT_ANNOTATION_TYPE = "statcat:searchContext"

#: Annotation type that keeps a code out of the search corpus by default
#: (see ``exclude_annotation_types`` on `Catalog.build_index`). statcatlib's own
#: namespaced convention — producers attach it to codes that exist for
#: structure, not discovery (e.g. the leaves of a geography hierarchy that
#: carry no observations and are named by a bare identifier), so a large
#: codelist can be indexed without flooding the corpus with them. Its
#: presence is the signal; title and text are ignored. The code stays in the
#: structural tables — only its search rows are skipped.
SEARCH_EXCLUDE_ANNOTATION_TYPE = "statcat:searchExclude"

#: The SKOS synonym annotation `build_index` folds into code rows
#: (``include_alt_labels``).
_ALT_LABEL_ANNOTATION_TYPE = "skos:altLabel"

#: Every annotation type `build_index` reads by default. An annotation
#: overlay touching one of these changes what the index should hold, so it
#: needs ``build_index()`` to take effect in search (GH-3).
SEARCH_ANNOTATION_TYPES: tuple[str, ...] = (
    SEARCH_CONTEXT_ANNOTATION_TYPE,
    SEARCH_EXCLUDE_ANNOTATION_TYPE,
    _ALT_LABEL_ANNOTATION_TYPE,
)


def annotation_fingerprint(conn: duckdb.DuckDBPyConnection, types: Sequence[str]) -> str:
    """A digest of every annotation of *types* in the ``sdmx`` store.

    What `build_index` records, and what a stale-index check recomputes: it
    changes when an annotation of one of *types* is added, removed or edited
    anywhere in the store — on any owner, which over-approximates the codes
    the index reads and so never misses a change. Each annotation row is
    hashed with its owner, position, id, title and value, and each of its
    localised texts with its owner, position and language.

    The row hashes are summed, not sorted and concatenated: order-independent
    without a sort, so it costs one scan (about 140 ms for 400,000
    annotations with 400,000 texts). md5 rather than DuckDB's ``hash``, which
    is not stable across DuckDB versions. NULLs are spelled out, since
    ``concat_ws`` skips them and would let a title and a value trade places.

    Args:
        conn: A connection whose ``sdmx`` schema holds the annotations.
        types: The annotation types to cover.

    Returns:
        A hex digest. With *types* empty, or no ``sdmx`` schema, the digest
        of nothing.
    """
    nothing = hashlib.md5(b"", usedforsecurity=False).hexdigest()
    has_sdmx = conn.execute(
        """SELECT count(*) FROM information_schema.tables WHERE table_schema = 'sdmx' AND table_name = 'annotation'"""
    ).fetchone()
    if not types or not has_sdmx or not has_sdmx[0]:
        return nothing
    row = conn.execute(
        """
        WITH typed AS (
            SELECT id, owner_urn, ordinal, annotation_id, annotation_type, title, value
            FROM sdmx.annotation
            WHERE annotation_type IN (SELECT UNNEST($types))
        )
        SELECT
            (SELECT count(*) FROM typed),
            (SELECT sum(md5_number_lower(concat_ws(chr(31),
                owner_urn, ordinal, coalesce(annotation_id, chr(0)), annotation_type,
                coalesce(title, chr(0)), coalesce(value, chr(0))))) FROM typed),
            (SELECT count(*) FROM typed a JOIN sdmx.annotation_text t ON t.annotation_id = a.id),
            (SELECT sum(md5_number_lower(concat_ws(chr(31),
                a.owner_urn, a.ordinal, coalesce(t.lang, chr(0)), coalesce(t.text, chr(0)))))
             FROM typed a JOIN sdmx.annotation_text t ON t.annotation_id = a.id)
        """,
        {"types": sorted(types)},
    ).fetchone()
    if row is None:
        return nothing
    folded = "|".join(str(part) for part in (sorted(types), *row))
    return hashlib.md5(folded.encode(), usedforsecurity=False).hexdigest()


#: Bucket key for search text that carries no language. Not a language
#: tag: it means "applies to every language", which is the only thing
#: untagged text can mean. Text bucketed here folds into every language's
#: row; text bucketed under a BCP-47 tag folds into that language's row
#: alone. Two kinds of untagged text use it:
#:
#: - ``skos:altLabel`` annotations whose text lives in the non-localised
#:   ``annotation.title`` column (rather than ``sdmx.annotation_text``).
#: - reference-metadata values stored as ``metadata_value.scalar`` with no
#:   ``i18n`` map at all — URLs, vintages, codes.
_ANY_LANG = "*"


# Relationship names accepted by QueryBuilder.include().
# These match the model field names on BaseDataset (and subclasses).
VALID_INCLUDE_RELS: frozenset[str] = frozenset(
    {
        "publisher",
        "creator",
        "contact_point",
        "frequency",
        "licence",
        "themes",
        "subject",
        "dataset_type",
        "keywords",
        "spatial_coverage",
        "distributions",
        "conforms_to",
        "quality_annotations",
        "provenance",
        "dimension_names",  # AggregateDataset
        "variables",  # MicrodataDataset
        "feature_types",  # GeospatialDataset
        "authors",  # PublicationDataset
    }
)


def _rrf_merge_rows(
    bm25_rows: list[Bm25HitRow],
    vec_rows: list[VectorHitRow],
    *,
    rrf_k: int,
    key: Callable[[SearchRow], str],
) -> list[tuple[SearchRow, float, str]]:
    """Reciprocal-rank fusion over two ranked row lists.

    Returns ``(representative_row, rrf_score, source)`` triples sorted by
    descending score. ``source`` is ``"hybrid"`` when both lists ranked
    the same key, otherwise the list that contributed.

    Keeping this as a free function lets every scope share one RRF
    implementation — duplicating it per scope ages badly the first time
    we tweak the formula or the tie-break.
    """
    rep: dict[str, SearchRow] = {}
    scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    for rank, row in enumerate(bm25_rows):
        k = key(row)
        rep.setdefault(k, row)
        scores[k] = scores.get(k, 0.0) + 1.0 / (rrf_k + rank + 1)
        sources.setdefault(k, set()).add("bm25")
    for rank, row in enumerate(vec_rows):
        k = key(row)
        rep.setdefault(k, row)
        scores[k] = scores.get(k, 0.0) + 1.0 / (rrf_k + rank + 1)
        sources.setdefault(k, set()).add("vector")
    ordered = sorted(scores, key=lambda x: -scores[x])
    return [
        (
            rep[k],
            scores[k],
            "hybrid" if len(sources[k]) > 1 else next(iter(sources[k])),
        )
        for k in ordered
    ]


def _as_seq(value: str | Sequence[str] | None) -> list[str] | None:
    """Normalise a ``str | Sequence[str] | None`` filter arg to a list or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


class QueryBuilder[T]:
    """Chainable query builder. Terminal method: ``.execute()`` or ``.all()``."""

    __slots__ = (
        "_cls",
        "_exists_filters",
        "_facet_fields",
        "_filters",
        "_from_ids",
        "_hybrid_text",
        "_include_rels",
        "_limit",
        "_map_filters",
        "_offset",
        "_scalar_col",
        "_search_fuzziness",
        "_search_operator",
        "_select_cols",
        "_select_lang",
        "_sort_fields",
        "_store",
        "_text",
    )

    def __init__(
        self,
        store: Catalog,
        cls: type[T],
        *,
        filters: dict[str, object] | None = None,
        exists_filters: list[PositionalFilter] | None = None,
        map_filters: list[MapFilter] | None = None,
        from_ids: list[str] | None = None,
        text: str | None = None,
        hybrid_text: str | None = None,
        search_operator: str = "or",
        search_fuzziness: int = 0,
        sort_fields: list[_SortSpec] | None = None,
        limit: int | None = None,
        offset: int = 0,
        facet_fields: list[str] | None = None,
        select_cols: tuple[str, ...] | None = None,
        select_lang: str | None = None,
        scalar_col: str | None = None,
        include_rels: frozenset[str] | None = None,
    ) -> None:
        self._store = store
        self._cls = cls
        self._filters = filters or {}
        self._exists_filters = exists_filters or []
        self._map_filters = map_filters or []
        self._from_ids = from_ids
        self._text = text
        self._hybrid_text = hybrid_text
        self._search_operator = search_operator
        self._search_fuzziness = search_fuzziness
        self._sort_fields = sort_fields or []
        self._limit = limit
        self._offset = offset
        self._facet_fields = facet_fields or []
        self._select_cols = select_cols
        self._select_lang = select_lang
        self._scalar_col = scalar_col
        self._include_rels = include_rels

    # -- internal clone helper --

    def _clone(self, **overrides: object) -> QueryBuilder[T]:
        """Return a copy with the given fields overridden."""
        # The one `Any` in this method, and deliberately a local: rebuilding a
        # builder is a genuinely dynamic step (17 fields, splatted), so the
        # hole dies on this line instead of riding out through the signature —
        # `**overrides` stays `object`.
        defaults: dict[str, Any] = {
            "filters": dict(self._filters),
            "exists_filters": list(self._exists_filters),
            "map_filters": list(self._map_filters),
            "from_ids": self._from_ids,
            "text": self._text,
            "hybrid_text": self._hybrid_text,
            "search_operator": self._search_operator,
            "search_fuzziness": self._search_fuzziness,
            "sort_fields": list(self._sort_fields),
            "limit": self._limit,
            "offset": self._offset,
            "facet_fields": list(self._facet_fields),
            "select_cols": self._select_cols,
            "select_lang": self._select_lang,
            "scalar_col": self._scalar_col,
            "include_rels": self._include_rels,
        }
        defaults.update(overrides)
        return QueryBuilder(self._store, self._cls, **defaults)

    # -- chaining methods --

    def filter(self, *pos_filters: PositionalFilter | MapFilter, **kw_filters: object) -> QueryBuilder[T]:
        """Add filter criteria. Returns a new builder (immutable chain)."""
        new_exists = list(self._exists_filters)
        new_map = list(self._map_filters)
        for pf in pos_filters:
            if isinstance(pf, MapFilter):
                new_map.append(pf)
            else:
                new_exists.append(pf)
        merged = {**self._filters, **kw_filters}
        return self._clone(filters=merged, exists_filters=new_exists, map_filters=new_map)

    def from_ids(self, ids: list[str]) -> QueryBuilder[T]:
        """Restrict results to a pre-computed list of identifiers."""
        return self._clone(from_ids=ids)

    def search(self, text: str, *, operator: str = "or", fuzzy: int = 0) -> QueryBuilder[T]:
        """Add full-text search. Requires LanceDB.

        Args:
            text: The search text. Tokenised and stemmed with the index
                language's rules before matching.
            operator: How to join multi-term queries: ``"or"`` (default) matches any term; ``"and"``
                requires all terms.
            fuzzy: Levenshtein edit distance for fuzzy matching: ``0`` (default, exact match), ``1``, or
                ``2``.

        Returns:
            A new `QueryBuilder` with the search text applied. The original is
            unchanged — builders are immutable.
        """
        return self._clone(text=text, search_operator=operator, search_fuzziness=fuzzy)

    def hybrid_search(self, text: str) -> QueryBuilder[T]:
        """Add hybrid BM25 + semantic search. Requires LanceDB."""
        return self._clone(hybrid_text=text)

    def sort_by(
        self,
        field: str,
        *,
        desc: bool = False,
        nulls: str | None = None,
        lang: str | None = None,
    ) -> QueryBuilder[T]:
        """Add a sort criterion.

        For MAP columns like ``title``, pass ``lang`` to specify the
        language key (e.g. ``lang="en"``).  Raises ``ValueError`` if
        ``lang`` is omitted for a MAP column.
        """
        new_sorts = [*self._sort_fields, _SortSpec(field=field, desc=desc, nulls=nulls, lang=lang)]
        return self._clone(sort_fields=new_sorts)

    def limit(self, n: int) -> QueryBuilder[T]:
        """Set maximum number of results."""
        return self._clone(limit=n)

    def offset(self, n: int) -> QueryBuilder[T]:
        """Set pagination offset."""
        return self._clone(offset=n)

    def with_facets(self, *fields: str) -> QueryBuilder[T]:
        """Request facet counts alongside the results."""
        return self._clone(facet_fields=[*self._facet_fields, *fields])

    def include(self, *relationships: str) -> QueryBuilder[T]:
        """Declare which relationships to batch-load during hydration.

        By default all relationships are loaded (~17 queries).  Calling
        ``include()`` switches to selective loading: only the named
        relationships are fetched; everything else stays at its model
        default (``None`` / ``[]``).

        Subsequent ``include()`` calls accumulate — they do not replace
        the previous set.  Use ``full()`` to revert to loading everything.

        Valid names mirror the model field names::

            store.query(BaseDataset)
                .include("publisher", "themes", "keywords")
                .collect()   # 3 batch loads instead of 17

        Raises ``ValueError`` for unrecognised relationship names.
        """
        unknown = set(relationships) - VALID_INCLUDE_RELS
        if unknown:
            msg = f"Unknown relationship(s): {sorted(unknown)}. Valid names: {sorted(VALID_INCLUDE_RELS)}"
            raise ValueError(msg)
        current = self._include_rels if self._include_rels is not None else frozenset()
        return self._clone(include_rels=current | frozenset(relationships))

    def full(self) -> QueryBuilder[T]:
        """Load all relationships (the default behaviour).

        Useful to explicitly opt into full hydration after a partial
        ``include()`` call, or to make the cost visible at the call site::

            store.query(BaseDataset).filter(...).full().collect()
        """
        return self._clone(include_rels=None)

    # -- projection modifiers (SA2-style) --

    def select(self, *columns: str, lang: str | None = None) -> QueryBuilder[T]:
        """Set dict projection mode. Terminals return dicts instead of objects.

        ``lang`` is required when selecting MAP columns (``title``,
        ``description``).  Raises ``ValueError`` for unknown column names.
        """
        all_allowed = _SELECTABLE_COLUMNS | _MAP_SELECTABLE_COLUMNS
        for col in columns:
            if col not in all_allowed:
                msg = f"Invalid select column: {col!r}. Allowed: {sorted(all_allowed)}"
                raise ValueError(msg)
        map_cols = _MAP_SELECTABLE_COLUMNS & set(columns)
        if map_cols and lang is None:
            msg = f"lang is required when selecting MAP columns: {sorted(map_cols)}"
            raise ValueError(msg)
        return self._clone(select_cols=columns, select_lang=lang)

    def scalars(self, column: str, *, lang: str | None = None) -> QueryBuilder[T]:
        """Set single-column flat projection mode.

        Terminals return bare values instead of objects or dicts.
        """
        all_allowed = _SELECTABLE_COLUMNS | _MAP_SELECTABLE_COLUMNS
        if column not in all_allowed:
            msg = f"Invalid scalar column: {column!r}. Allowed: {sorted(all_allowed)}"
            raise ValueError(msg)
        if column in _MAP_SELECTABLE_COLUMNS and lang is None:
            msg = f"lang is required when selecting MAP column {column!r}"
            raise ValueError(msg)
        return self._clone(scalar_col=column, select_lang=lang)

    # -- projection guards --

    def _check_entity_mode(self, method: str) -> None:
        if self._select_cols is not None:
            msg = f".{method}() cannot be used when .select() is active — use .rows() instead"
            raise ValueError(msg)
        if self._scalar_col is not None:
            msg = f".{method}() cannot be used when .scalars() is active — use .values() instead"
            raise ValueError(msg)

    def _require_select_cols(self, method: str) -> tuple[str, ...]:
        """The projection columns, or raise naming what to call first.

        Returns the value rather than merely checking it. A guard that only
        raises leaves the caller holding `tuple[str, ...] | None`, which both
        checkers then refuse to pass on — the two `# pyright: ignore`
        comments this replaces were exactly that, and pyrefly reported the
        same two independently. Handing back what it just proved present
        removes the suppression instead of doubling it.
        """
        if self._select_cols is None:
            msg = f".{method}() requires .select() — call .select(...) first, or use .collect() for domain objects"
            raise ValueError(msg)
        return self._select_cols

    def _require_scalar_col(self, method: str) -> str:
        """The scalar projection column, or raise naming what to call first.

        As `_require_select_cols`.
        """
        if self._scalar_col is None:
            msg = f".{method}() requires .scalars() — call .scalars(...) first"
            raise ValueError(msg)
        return self._scalar_col

    # -- terminal methods --

    def execute(self) -> SearchResult[T]:
        """Execute the query and return a ``SearchResult`` with metadata.

        Only available in entity mode (no ``.select()`` / ``.scalars()``).
        """
        self._check_entity_mode("execute")
        return self._store._execute_query(  # noqa: SLF001
            self._cls,
            self._filters,
            self._exists_filters,
            self._map_filters,
            self._from_ids,
            self._text,
            self._hybrid_text,
            self._search_operator,
            self._search_fuzziness,
            self._sort_fields,
            self._limit,
            self._offset,
            self._facet_fields,
            include_rels=self._include_rels,
        )

    def _resolve_search_ids(self) -> list[str] | None:
        """If text search is active, resolve ranked IDs; otherwise return None."""
        if self._text is None and self._hybrid_text is None:
            return None
        return self._store._ranked_ids_from_search(  # noqa: SLF001
            self._cls,
            self._filters,
            self._exists_filters,
            self._map_filters,
            self._text,
            self._hybrid_text,
            self._search_operator,
            self._search_fuzziness,
            self._sort_fields,
            self._limit,
            self._offset,
        )

    def collect(self) -> list[T]:
        """Execute and return domain objects.

        This is the preferred terminal for entity results — consistent with
        scope terminals (``.collect()`` on ``SchemeScope``, ``CodelistScope``,
        etc.).  Use ``.rows()`` for ``Row`` projections or ``.values()`` for
        scalar projections.
        """
        self._check_entity_mode("collect")
        return self.execute().items

    def all(self) -> list[T]:
        """Alias for ``.collect()``."""
        return self.collect()

    def first(self) -> T | None:
        """Execute with limit=1 and return the first domain object or None."""
        self._check_entity_mode("first")
        items = self.limit(1).collect()
        return items[0] if items else None

    def one(self) -> T:
        """Execute and return exactly one domain object; raise on 0 or >1."""
        self._check_entity_mode("one")
        result = self.limit(2).execute()
        if result.total == 0:
            msg = "No results found"
            raise KeyError(msg)
        if result.total > 1:
            msg = f"Expected exactly one result, got {result.total}"
            raise ValueError(msg)
        return result.items[0]

    # -- row projection terminals --

    def rows(self) -> list[Row]:
        """Execute and return ``Row`` objects (requires ``.select()``)."""
        select_cols = self._require_select_cols("rows")
        ranked_ids = self._resolve_search_ids()
        if ranked_ids is not None and not ranked_ids:
            return []
        return self._store._execute_projection(  # noqa: SLF001
            self._cls,
            select_cols,
            self._select_lang,
            filters={} if ranked_ids is not None else self._filters,
            exists_filters=[] if ranked_ids is not None else self._exists_filters,
            map_filters=[] if ranked_ids is not None else self._map_filters,
            from_ids=ranked_ids if ranked_ids is not None else self._from_ids,
            sort_fields=[] if ranked_ids is not None else self._sort_fields,
            limit=None if ranked_ids is not None else self._limit,
            offset=0 if ranked_ids is not None else self._offset,
        )

    def row(self) -> Row | None:
        """Execute with limit=1 and return the first ``Row`` or None."""
        items = self.limit(1).rows()
        return items[0] if items else None

    # -- scalar projection terminals --

    def values(self) -> list[object]:
        """Execute and return flat scalar values (requires ``.scalars()``)."""
        scalar_col = self._require_scalar_col("values")
        ranked_ids = self._resolve_search_ids()
        if ranked_ids is not None:
            if not ranked_ids:
                return []
            # Fast path: scalar("identifier") returns ranked IDs directly
            if scalar_col == "identifier":
                return [*ranked_ids]
        return self._store._execute_scalar_projection(  # noqa: SLF001
            self._cls,
            scalar_col,
            self._select_lang,
            filters={} if ranked_ids is not None else self._filters,
            exists_filters=[] if ranked_ids is not None else self._exists_filters,
            map_filters=[] if ranked_ids is not None else self._map_filters,
            from_ids=ranked_ids if ranked_ids is not None else self._from_ids,
            sort_fields=[] if ranked_ids is not None else self._sort_fields,
            limit=None if ranked_ids is not None else self._limit,
            offset=0 if ranked_ids is not None else self._offset,
        )

    def value(self) -> object | None:
        """Execute with limit=1 and return the first scalar value or None."""
        items = self.limit(1).values()
        return items[0] if items else None

    # -- projection-independent terminals --

    def count(self) -> int:
        """Return the total count without fetching items."""
        return self._store._execute_query(  # noqa: SLF001
            self._cls,
            self._filters,
            self._exists_filters,
            self._map_filters,
            self._from_ids,
            self._text,
            self._hybrid_text,
            self._search_operator,
            self._search_fuzziness,
            self._sort_fields,
            0,
            self._offset,
            [],
            include_rels=self._include_rels,
        ).total

    def exists(self) -> bool:
        """Return True if at least one result matches the filters."""
        if self._text is not None or self._hybrid_text is not None:
            ranked_ids = self._store._ranked_ids_from_search(  # noqa: SLF001
                self._cls,
                self._filters,
                self._exists_filters,
                self._map_filters,
                self._text,
                self._hybrid_text,
                self._search_operator,
                self._search_fuzziness,
                [],
                limit=1,
                offset=0,
            )
            return len(ranked_ids) > 0
        return self._store._execute_exists(  # noqa: SLF001
            self._cls,
            self._filters,
            self._exists_filters,
            self._map_filters,
            self._from_ids,
        )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@define(frozen=True)
class CodesScope:
    """Search and direct lookup over indexed SDMX code rows.

    Entry point: ``cat.codes``. Wraps the BM25/vector/hybrid paths
    against ``entity_type="code"`` rows in the Lance index, plus a
    pure-SQL ``get(code_urn)`` for cases where the URN is already known.
    """

    _store: Catalog = field(alias="store", repr=False)

    def find(
        self,
        query: str,
        *,
        codelist_urn: str | Sequence[str] | None = None,
        dataset_ids: Sequence[str] | None = None,
        mode: str = "bm25",
        lang: str = "en",
        limit: int = 20,
        offset: int = 0,
        operator: str = "or",
        fuzzy: int = 0,
        rrf_k: int = 60,
        oversample: int = 200,
    ) -> SearchResult[Hit[CodeHit]]:
        """Search code rows; returns `Hit` objects wrapping `CodeHit`.

        ``mode`` is one of ``"bm25"`` (default; Tantivy FTS), ``"vector"``
        (cosine over embeddings — requires an embedding model, returns
        nothing for FTS-only codelists), or ``"hybrid"`` (RRF merge of
        the two).
        """
        store = self._store
        if store._lance is None:  # noqa: SLF001
            return SearchResult(items=[], total=0, offset=offset)
        codelist_urns = _as_seq(codelist_urn)
        id_filter = list(dataset_ids) if dataset_ids is not None else None

        bm25_rows: list[Bm25HitRow] = []
        vec_rows: list[VectorHitRow] = []
        if mode in ("bm25", "hybrid"):
            bm25_rows = store._lance.bm25_search_rows(  # noqa: SLF001
                query,
                columns=_CODE_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                operator=operator,
                fuzziness=fuzzy,
                entity_type="code",
                id_filter=id_filter,
                codelist_urn_filter=codelist_urns,
            )
        if mode in ("vector", "hybrid"):
            if store._embedding_model is None:  # noqa: SLF001
                msg = f"mode={mode!r} requires an embedding_model; pass it to Catalog()"
                raise RuntimeError(msg)
            qvec = as_vector(store._embedding_model.encode(store._query_prefix + query))  # noqa: SLF001
            vec_rows = store._lance.search_rows(  # noqa: SLF001
                qvec,
                columns=_CODE_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                entity_type="code",
                id_filter=id_filter,
                codelist_urn_filter=codelist_urns,
            )

        def _key(row: SearchRow) -> str:
            urn = row.get("code_urn") or ""
            if urn:
                return urn
            return f"{row.get('id', '')}|{row.get('code_id', '')}|{row.get('lang', '')}"

        if mode == "hybrid":
            merged = _rrf_merge_rows(bm25_rows, vec_rows, rrf_k=rrf_k, key=_key)
            hits = [_row_to_code_hit(row, fallback_lang=lang, score=score, source=src) for row, score, src in merged]
        elif mode == "vector":
            hits = [
                _row_to_code_hit(
                    r,
                    fallback_lang=lang,
                    score=1.0 / (1.0 + r.get("_distance", 0.0) or 0.0),
                    source="vector",
                )
                for r in vec_rows
            ]
        else:  # bm25
            hits = [_row_to_code_hit(r, fallback_lang=lang, score=r["_score"], source="bm25") for r in bm25_rows]

        total = len(hits)
        page = hits[offset : offset + limit]
        scores = {h.payload.code_urn or h.payload.code_id: h.score for h in page}
        return SearchResult(items=page, total=total, offset=offset, scores=scores)

    def get(self, code_urn: str) -> CodeHit | None:
        """Direct SQL lookup of a single code by URN — no Lance index involved.

        Resolves the code's labels in any one language present
        (default: prefer ``en``, fall back to whichever language has
        the label). For multi-language access, fetch the underlying
        ``sdmx.code`` row directly.
        """
        store = self._store
        try:
            row = store._c.execute(  # noqa: SLF001
                """
                SELECT c.code_urn, c.codelist_urn, c.code_id, c.parent_id, c.depth,
                       c.name
                FROM sdmx.code c
                WHERE c.code_urn = $code_urn
                """,
                {"code_urn": code_urn},
            ).fetchone()
        except duckdb.Error:
            return None
        if row is None:
            return None
        c_urn, cl_urn, code_id, parent_id, depth, name_map = row
        name = ""
        if name_map:
            name = name_map.get("en") or next(iter(name_map.values()), "")
        return CodeHit(
            dataset_id="",
            dimension_id="",
            dimension_label="",
            code_id=str(code_id or ""),
            code_label=str(name or ""),
            text=str(name or ""),
            code_urn=str(c_urn or ""),
            codelist_urn=str(cl_urn or ""),
            parent_id=str(parent_id or ""),
            depth=int(depth or 0),
        )


@define(frozen=True)
class ConceptsScope:
    """Search and direct lookup over indexed SDMX Concept rows.

    Entry point: ``cat.concepts``. Concepts carry definitional text in
    ``sdmx.concept.description`` — the single highest-value vector
    target in an SDMX catalog, since a query like "income inequality"
    finds "Gini coefficient" through its definition rather than its
    label.
    """

    _store: Catalog = field(alias="store", repr=False)

    def find(
        self,
        query: str,
        *,
        scheme_urn: str | Sequence[str] | None = None,
        mode: str = "bm25",
        lang: str = "en",
        limit: int = 20,
        offset: int = 0,
        operator: str = "or",
        fuzzy: int = 0,
        rrf_k: int = 60,
        oversample: int = 200,
    ) -> SearchResult[Hit[ConceptHit]]:
        """Search concept rows; returns `Hit` objects wrapping `ConceptHit`."""
        store = self._store
        if store._lance is None:  # noqa: SLF001
            return SearchResult(items=[], total=0, offset=offset)
        scheme_urns = _as_seq(scheme_urn)

        bm25_rows: list[Bm25HitRow] = []
        vec_rows: list[VectorHitRow] = []
        if mode in ("bm25", "hybrid"):
            bm25_rows = store._lance.bm25_search_rows(  # noqa: SLF001
                query,
                columns=_CONCEPT_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                operator=operator,
                fuzziness=fuzzy,
                entity_type="concept",
                scheme_urn_filter=scheme_urns,
            )
        if mode in ("vector", "hybrid"):
            if store._embedding_model is None:  # noqa: SLF001
                msg = f"mode={mode!r} requires an embedding_model; pass it to Catalog()"
                raise RuntimeError(msg)
            qvec = as_vector(store._embedding_model.encode(store._query_prefix + query))  # noqa: SLF001
            vec_rows = store._lance.search_rows(  # noqa: SLF001
                qvec,
                columns=_CONCEPT_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                entity_type="concept",
                scheme_urn_filter=scheme_urns,
            )

        def _key(row: SearchRow) -> str:
            return row.get("concept_urn") or row.get("id") or ""

        if mode == "hybrid":
            merged = _rrf_merge_rows(bm25_rows, vec_rows, rrf_k=rrf_k, key=_key)
            hits = [_row_to_concept_hit(row, fallback_lang=lang, score=score, source=src) for row, score, src in merged]
        elif mode == "vector":
            hits = [
                _row_to_concept_hit(
                    r,
                    fallback_lang=lang,
                    score=1.0 / (1.0 + r.get("_distance", 0.0) or 0.0),
                    source="vector",
                )
                for r in vec_rows
            ]
        else:
            hits = [_row_to_concept_hit(r, fallback_lang=lang, score=r["_score"], source="bm25") for r in bm25_rows]

        total = len(hits)
        page = hits[offset : offset + limit]
        scores = {h.payload.concept_urn: h.score for h in page}
        return SearchResult(items=page, total=total, offset=offset, scores=scores)

    def get(self, concept_urn: str) -> ConceptHit | None:
        """Direct SQL lookup of a single concept by URN."""
        store = self._store
        try:
            row = store._c.execute(  # noqa: SLF001
                """
                SELECT c.concept_urn, c.scheme_urn, c.concept_id, c.name, c.description
                FROM sdmx.concept c
                WHERE c.concept_urn = $concept_urn
                """,
                {"concept_urn": concept_urn},
            ).fetchone()
        except duckdb.Error:
            return None
        if row is None:
            return None
        c_urn, sc_urn, c_id, name_map, desc_map = row
        name = ""
        desc = ""
        if name_map:
            name = name_map.get("en") or next(iter(name_map.values()), "")
        if desc_map:
            desc = desc_map.get("en") or next(iter(desc_map.values()), "")
        text = f"{name} | {desc}" if (name and desc) else (name or desc)
        return ConceptHit(
            concept_urn=str(c_urn),
            scheme_urn=str(sc_urn or ""),
            concept_id=str(c_id or ""),
            name=name,
            description=desc,
            text=text,
        )


@define(frozen=True)
class ReferenceMetadataScope:
    """Search over indexed SDMX reference-metadata sections.

    Entry point: ``cat.reference_metadata``. Where `Catalog.codes`
    searches short controlled-vocabulary labels, this searches the prose of
    methodology documents — reference guides, quality reports — indexed one
    chunk per section. Hits carry ``set_urn`` / ``attribute_path`` so a
    caller can pull the full typed section via
    `Catalog.metadata_report` or resolve the artefacts the document
    describes.

    ``target_urn`` scopes the search to metadata attached to a given
    artefact — the semantic-search analogue of `Catalog.metadata_for`.
    """

    _store: Catalog = field(alias="store", repr=False)

    def find(
        self,
        query: str,
        *,
        target_urn: str | None = None,
        mode: str = "bm25",
        lang: str = "en",
        limit: int = 20,
        offset: int = 0,
        operator: str = "or",
        fuzzy: int = 0,
        rrf_k: int = 60,
        oversample: int = 200,
    ) -> SearchResult[Hit[ReferenceMetadataHit]]:
        """Search reference-metadata section chunks.

        ``target_urn`` restricts results to sections whose MetadataSet
        targets that artefact URN — resolved via
        `Catalog.metadata_for`, so a semantic hit and its structured
        resolution stay consistent. Other arguments mirror the code/concept
        scopes (``mode``, ``lang``, ``limit``, ``offset``, ``operator``,
        ``fuzzy``, ``rrf_k``, ``oversample``).
        """
        store = self._store
        if store._lance is None:  # noqa: SLF001
            return SearchResult(items=[], total=0, offset=offset)

        set_urn_filter: list[str] | None = None
        if target_urn is not None:
            set_urn_filter = [str(artefact_urn(s)) for s in store.metadata_for(target_urn)]
            if not set_urn_filter:
                return SearchResult(items=[], total=0, offset=offset)

        bm25_rows: list[Bm25HitRow] = []
        vec_rows: list[VectorHitRow] = []
        if mode in ("bm25", "hybrid"):
            bm25_rows = store._lance.bm25_search_rows(  # noqa: SLF001
                query,
                columns=_REFMETA_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                operator=operator,
                fuzziness=fuzzy,
                entity_type="reference_metadata",
                set_urn_filter=set_urn_filter,
            )
        if mode in ("vector", "hybrid"):
            if store._embedding_model is None:  # noqa: SLF001
                msg = f"mode={mode!r} requires an embedding_model; pass it to Catalog()"
                raise RuntimeError(msg)
            qvec = as_vector(store._embedding_model.encode(store._query_prefix + query))  # noqa: SLF001
            vec_rows = store._lance.search_rows(  # noqa: SLF001
                qvec,
                columns=_REFMETA_ROW_COLUMNS,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                entity_type="reference_metadata",
                set_urn_filter=set_urn_filter,
            )

        def _key(row: SearchRow) -> str:
            return row.get("id") or ""

        if mode == "hybrid":
            merged = _rrf_merge_rows(bm25_rows, vec_rows, rrf_k=rrf_k, key=_key)
            hits = [
                _row_to_reference_metadata_hit(row, fallback_lang=lang, score=score, source=src)
                for row, score, src in merged
            ]
        elif mode == "vector":
            hits = [
                _row_to_reference_metadata_hit(
                    r,
                    fallback_lang=lang,
                    score=1.0 / (1.0 + r.get("_distance", 0.0) or 0.0),
                    source="vector",
                )
                for r in vec_rows
            ]
        else:
            hits = [
                _row_to_reference_metadata_hit(r, fallback_lang=lang, score=r["_score"], source="bm25")
                for r in bm25_rows
            ]

        total = len(hits)
        page = hits[offset : offset + limit]
        scores = {f"{h.payload.set_urn}#{h.payload.attribute_path}#{h.payload.chunk_index}": h.score for h in page}
        return SearchResult(items=page, total=total, offset=offset, scores=scores)


@define(frozen=True)
class DatasetsScope:
    """Search across the catalog's datasets.

    Entry point: ``cat.datasets``. Replaces the older ``cat.search`` /
    ``cat.semantic_search`` / ``cat.hybrid_search`` triple — now a
    single ``find(query, mode=...)`` returning
    ``SearchResult[Hit[BaseDataset]]``.
    """

    _store: Catalog = field(alias="store", repr=False)

    def find(  # noqa: C901
        self,
        query: str,
        *,
        mode: str = "bm25",
        lang: str = "en",
        limit: int = 50,
        offset: int = 0,
        operator: str = "or",
        fuzzy: int = 0,
        rrf_k: int = 60,
        oversample: int = 200,
    ) -> SearchResult[Hit[BaseDataset]]:
        """Search datasets; returns `Hit` objects wrapping `BaseDataset`."""
        store = self._store
        if store._lance is None:  # noqa: SLF001
            return SearchResult(items=[], total=0, offset=offset)

        bm25_pairs: list[tuple[str, float]] = []
        vec_pairs: list[tuple[str, float]] = []
        if mode in ("bm25", "hybrid"):
            bm25_pairs = store._lance.bm25_search(  # noqa: SLF001
                query,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                operator=operator,
                fuzziness=fuzzy,
                entity_type=_DATASET_ENTITY_TYPES,
            )
        if mode in ("vector", "hybrid"):
            if store._embedding_model is None:  # noqa: SLF001
                msg = f"mode={mode!r} requires an embedding_model; pass it to Catalog()"
                raise RuntimeError(msg)
            qvec = as_vector(store._embedding_model.encode(store._query_prefix + query))  # noqa: SLF001
            vec_pairs = store._lance.search(  # noqa: SLF001
                qvec,
                lang=lang,
                limit=oversample if mode == "hybrid" else limit + offset,
                entity_type=_DATASET_ENTITY_TYPES,
            )

        if mode == "hybrid":
            bm25_rank = {ident: i for i, (ident, _) in enumerate(bm25_pairs)}
            vec_rank = {ident: i for i, (ident, _) in enumerate(vec_pairs)}
            all_ids = set(bm25_rank) | set(vec_rank)
            scored: dict[str, tuple[float, str]] = {}
            for ident in all_ids:
                s = 0.0
                srcs: set[str] = set()
                if ident in bm25_rank:
                    s += 1.0 / (rrf_k + bm25_rank[ident] + 1)
                    srcs.add("bm25")
                if ident in vec_rank:
                    s += 1.0 / (rrf_k + vec_rank[ident] + 1)
                    srcs.add("vector")
                scored[ident] = (s, "hybrid" if len(srcs) > 1 else next(iter(srcs)))
            ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
        elif mode == "vector":
            ranked = [(ident, (1.0 / (1.0 + d), "vector")) for ident, d in vec_pairs]
        else:
            ranked = [(ident, (score, "bm25")) for ident, score in bm25_pairs]

        total = len(ranked)
        page = ranked[offset : offset + limit]
        identifiers = [ident for ident, _ in page]
        datasets = store._fetch_by_identifiers(identifiers)  # noqa: SLF001
        by_id = {ds.identifier: ds for ds in datasets}
        hits: list[Hit[BaseDataset]] = []
        for ident, (score, src) in page:
            ds = by_id.get(ident)
            if ds is None:
                continue
            hits.append(Hit(kind="dataset", score=score, payload=ds, lang=lang, source=src))
        scores = {ident: score for ident, (score, _) in page}
        return SearchResult(items=hits, total=total, offset=offset, scores=scores)


class Catalog:
    """DuckDB-backed catalog store.

    **Persistent catalogs** — ``Catalog("my_catalog").open()`` is
    open-or-create: it creates the catalog folder if it does not exist and
    opens it otherwise. Construction alone does not touch the disk.

    On-disk layout under ``<uri>/``:

    - ``meta.duckdb`` — co-located ``sdmx.*`` (registry) and ``statcat.*``
      (catalog) schemas (Topology B).
    - ``lake.ducklake`` — DuckLake catalog sidecar; ATTACHed as ``lake``
      at open time.
    - ``parquet/`` — DuckLake-managed observation files (DATA_PATH).
    - ``search/`` — optional Lance vector index.

    Observation writes go through ``cat.data.write_observations(...)``;
    no public filesystem-parquet seam.

    **In-memory** (no persistence)::

        store = Catalog()

    **Shared-connection** (sdmxlib ``LocalRegistry`` integration)::

        conn = duckdb.connect("shared.duckdb")
        store = Catalog(conn)
        local = sl.LocalRegistry.from_connection(conn)

    ``Catalog(path)`` is open-or-create: it creates the catalog folder if it
    does not exist and opens it otherwise.
    """

    _uri: str | Path
    _conn: duckdb.DuckDBPyConnection | None
    _owns_connection: bool
    _bundle_dir: Path | None
    _data_path: str | Path | Embedded | None
    _azure_credentials: dict[str, str] | None
    _embedding_model: EmbeddingModel | None
    _embedding_metadata: EmbeddingMetadata | None
    _document_prefix: str
    _query_prefix: str
    _registry: sdmxlib.LocalRegistry | None
    _data: sdmxlib.DuckDBDataStore | None
    # Declared alongside its siblings; without it, `self._lance = None` in
    # `__init__` infers `Any | None` and `index_layout` then returns Any.
    _lance: LanceIndex | None

    def __init__(
        self,
        uri: str | Path = ":memory:",
        *,
        data_path: str | Path | Embedded | None = None,
        azure_credentials: dict[str, str] | None = None,
        embedding_model: EmbeddingModel | None = None,
        document_prefix: str = "",
        query_prefix: str = "",
        read_only: bool = False,
    ) -> None:
        """Reference a catalog at ``uri``.

        Path-style: construction is cheap — only the URI and configuration
        are stashed. The DuckDB connection opens on `open` (or
        implicitly when entering a ``with`` block). Use
        `from_connection` to wrap an existing DuckDB connection
        (e.g. when sharing one with sdmxlib).

        Args:
            uri: ``":memory:"`` for an in-memory catalog (default), or a folder path for a persistent
                catalog. Folder mode lays out ``<uri>/meta.duckdb`` (sentinel), ``<uri>/lake.ducklake``
                (DuckLake catalog), ``<uri>/parquet/`` (DuckLake DATA_PATH — unless ``data_path``
                overrides), and an optional ``<uri>/search/`` for the Lance index.
            data_path: Override the DuckLake DATA_PATH (where observation parquet files live). Defaults to
                ``<uri>/parquet/`` (folder mode). Pass a remote URI (e.g. ``abfss://...``) to keep
                ``meta.duckdb`` + ``lake.ducklake`` local while landing observation parquet on object
                storage. An explicit path takes precedence over the lake's stamp for this open
                (DuckLake's ``OVERRIDE_DATA_PATH``) without rewriting it. Pass `statcatlib.bundle.Embedded` to
                omit DATA_PATH from the ATTACH altogether and honour whatever the lake is stamped with.
                Only valid in folder mode. No local ``parquet/`` directory is created either way — DuckLake
                makes its own on first write.
                Prefer `statcatlib.bundle.open_bundle`, which picks this for you from the bundle's layout.
            azure_credentials: Forwarded to `sdmxlib.DuckDBDataStore`. Required when ``data_path`` is an
                ``abfss://`` URI. See sdmxlib's ``DuckDBDataStore`` for accepted shapes
                (``connection_string`` / service-principal / credential chain).
            embedding_model: A sentence-transformers model (or any object exposing ``encode``) used to
                embed text for semantic and hybrid search. When ``None`` (default) the catalog is BM25-only
                and no ``embeddings.json`` sidecar is written.
            document_prefix: Prepended to every document text before embedding in ``build_index``. Required
                for asymmetric models like e5 (``"passage: "``); leave empty for symmetric models.
            query_prefix: Prepended to every query string before embedding in ``semantic_search`` /
                ``hybrid_search``. For e5 use ``"query: "``; leave empty for symmetric models.
            read_only: Open the underlying DuckDB connection with ``read_only=True``. Required when a bundle
                is shared between multiple processes (e.g. ``uvicorn --workers N``) — DuckDB takes an
                exclusive file lock on read-write opens. Skips all DDL and ``mkdir`` side effects; ATTACHes
                the DuckLake sidecar with ``READ_ONLY``; write APIs (``put``, ``bulk_load``,
                ``build_index``, ``write_observations``) raise ``RuntimeError``. Not supported with
                ``:memory:`` or with ``embedding_model`` (index builds need a writable lake).

        Raises:
            ValueError: ``read_only=True`` was combined with ``:memory:`` or
                with an ``embedding_model`` — neither can serve a read-only
                open, since an index build needs a writable lake.
        """
        if read_only:
            if str(uri) == ":memory:":
                msg = "read_only=True is not supported with an in-memory catalog (':memory:')"
                raise ValueError(msg)
            if embedding_model is not None:
                msg = "read_only=True is incompatible with embedding_model (build_index needs a writable catalog)"
                raise ValueError(msg)
        self._uri = uri
        self._conn = None
        # Per-thread cursor holder — DuckDB cursors are not thread-safe (see
        # the ``_c`` property), so every thread gets its own cursor on the
        # shared connection.
        self._cursor_local: threading.local = threading.local()
        self._owns_connection = True
        self._bundle_dir = None
        self._data_path = data_path
        self._azure_credentials = azure_credentials
        self._embedding_model = embedding_model
        self._embedding_metadata = None
        self._document_prefix = document_prefix
        self._query_prefix = query_prefix
        self._read_only = read_only
        self._bulk_depth = 0
        self._lance = None
        self._registry = None
        self._data = None

    @property
    def uri(self) -> str | Path:
        """The URI this catalog references. Available before opening."""
        return self._uri

    def open(self) -> Self:
        """Open the underlying DuckDB connection and prepare schemas + Lance.

        Idempotent within a single open/close cycle. Returns ``self`` so the
        call composes with ``with``::

            with Catalog("my_catalog").open() as cat:
                ...

        Raises `IncompatibleCatalogError` when the catalog's SDMX structure
        was stored by an sdmxlib with another store schema, which this one
        would drop on open. The connection is closed again first.

        Returns:
            This catalog, open.

        Raises:
            ValueError: ``embedding_model`` was given for an in-memory catalog.
        """
        if self._conn is not None:
            return self
        self._conn, self._bundle_dir = self._open_connection()
        self._owns_connection = True
        if self._embedding_model is not None and self._bundle_dir is None:
            msg = "embedding_model requires a folder-mode catalog (pass a path, not ':memory:')"
            raise ValueError(msg)
        # Schema setup runs once on the shared connection; the returned
        # cursor is discarded because each thread builds its own via the
        # ``_c`` property (DDL persists on the connection itself).
        if self._read_only:
            _ = _catalog_schema.open_schema_readonly(self._conn)
        else:
            _ = _catalog_schema.create_schema(self._conn)
        # Compose an sdmxlib LocalRegistry on the same connection (Topology B).
        # The statcatlib
        # schema and the sdmx schema co-exist in one DuckDB file, so cross-
        # schema queries (e.g. join statcat.dataset to sdmx.codelist) work
        # natively without ATTACH plumbing.
        self._registry = self._open_registry(self._conn)
        # ATTACH the DuckLake sidecar (folder mode only — in-memory has no
        # lake). Sidecar layout because DuckDB rejects self-ATTACH; the
        # ducklake catalog file sits next to meta.duckdb so the bundle is
        # still one cleavage point on disk. DATA_PATH defaults to the
        # in-bundle parquet/ directory but can be redirected (e.g. to an
        # abfss:// URI) via ``data_path``.
        if self._bundle_dir is not None:
            attach_url = f"'ducklake:{self._bundle_dir / 'lake.ducklake'}'"
            data_path_literal, override = self._resolve_data_path(self._bundle_dir)
            attach_options = self._attach_options(data_path_literal, override=override)

            self._data = sdmxlib.DuckDBDataStore.from_registry(
                self._registry,
                attach={"lake": (attach_url, attach_options)},
                extensions=["ducklake"],
                azure_credentials=self._azure_credentials,
            )
        else:
            # In-memory catalogs have no lake; write_observations is unavailable.
            self._data = sdmxlib.DuckDBDataStore.from_registry(self._registry)
        if self._bundle_dir is not None:
            self._init_lance()
            self._warn_if_index_annotations_changed()
            # Restore embedding metadata written by a previous build_index()
            # so the caller doesn't have to remember which model + prefixes
            # match this index. Missing/malformed metadata leaves the store
            # in its empty default state — callers can still set things
            # explicitly. v2 sidecars are still accepted; absent fields
            # surface as ``None`` on `EmbeddingMetadata`.
            raw = _read_embeddings_meta(self._bundle_dir / "search")
            if raw is not None:
                meta = _parse_embeddings_meta(raw)
                if meta is not None:
                    self._embedding_metadata = meta
                    if not self._document_prefix:
                        self._document_prefix = meta.document_prefix
                    if not self._query_prefix:
                        self._query_prefix = meta.query_prefix
        return self

    def _open_registry(self, conn: duckdb.DuckDBPyConnection) -> sdmxlib.LocalRegistry:
        """Compose the sdmxlib `LocalRegistry` on *conn*, once the guard allows it.

        Args:
            conn: The connection `open` just made, which this catalog owns.

        Returns:
            The registry sharing *conn*.

        Raises:
            IncompatibleCatalogError: sdmxlib would drop this catalog's SDMX
                structure. *conn* is closed first, releasing the file so the
                release that built it can reopen it from this process.
        """
        try:
            _refuse_sdmx_schema_rebuild(conn)
        except IncompatibleCatalogError:
            conn.close()
            self._conn = None
            raise
        return sdmxlib.LocalRegistry.from_connection(conn)

    def _open_connection(self) -> tuple[duckdb.DuckDBPyConnection, Path | None]:
        """Open the DuckDB connection and (for folder mode) prepare the bundle dir.

        Returns ``(connection, bundle_dir)`` where ``bundle_dir`` is
        ``None`` for in-memory catalogs. The local ``parquet/`` directory
        is only created when no ``data_path`` override is set — a remote
        DATA_PATH must not leave a stray local directory behind.
        """
        if str(self._uri) == ":memory:":
            if self._data_path is not None:
                msg = "data_path is not supported with an in-memory catalog (':memory:')"
                raise ValueError(msg)
            return duckdb.connect(":memory:"), None
        # Absolute, so the stamp DuckLake records on first open is too: a
        # relative one is resolved against whatever the working directory is
        # when the lake is read (GH-23). `absolute()`, not `resolve()`, which
        # follows symlinks and would respell a catalog created by absolute path.
        path = Path(self._uri).absolute()
        if self._read_only:
            meta_path = path / "meta.duckdb"
            if not meta_path.exists():
                msg = f"read_only=True requires an existing catalog bundle; {meta_path} not found"
                raise FileNotFoundError(msg)
            return duckdb.connect(str(meta_path), read_only=True), path
        # Only the bundle directory is created. DuckLake makes its own
        # DATA_PATH directory on first write, so creating ``parquet/`` here
        # bought nothing and cost a real discriminator: an empty local
        # ``parquet/`` beside a split bundle makes an unresolvable layout
        # look co-located (#120).
        path.mkdir(parents=True, exist_ok=True)
        return duckdb.connect(str(path / "meta.duckdb")), path

    def _attach_options(self, data_path_literal: str | None, *, override: bool = False) -> str:
        """Render the DuckLake ATTACH option list.

        A ``None`` literal means honour the lake's own stamp: DuckLake only
        compares (and only rejects) when DATA_PATH is supplied, so omitting
        it is the one form that cannot disagree with what is stored.

        An explicit ``data_path`` also sets ``OVERRIDE_DATA_PATH``: without
        it DuckLake rejects any DATA_PATH not spelled exactly like the
        stamp — even the same directory, absolute instead of relative —
        so an explicit path could only ever restate the stamp. The
        override is per-attach and never rewrites the stamp, and it is
        allowed under ``READ_ONLY``. The default (``data_path=None``) stays
        a checked ``<bundle>/parquet/``, so a plain ``Catalog(path)`` on a
        split bundle still fails at ATTACH rather than reading an empty
        local directory. *override* is the one exception, for a read-only
        open of a catalog stamped with a relative path (`_resolve_data_path`).
        """
        opts = [] if data_path_literal is None else [f"DATA_PATH '{data_path_literal}'"]
        if data_path_literal is not None and (self._data_path is not None or override):
            opts.append("OVERRIDE_DATA_PATH true")
        if self._read_only:
            opts.append("READ_ONLY")
        return f"({', '.join(opts)})" if opts else ""

    def _resolve_data_path(self, bundle_dir: Path) -> tuple[str | None, bool]:
        """Return the DuckLake DATA_PATH literal, with trailing slash, and whether to override.

        Remote URIs (``abfss://``, ``s3://``, ``gs://``, etc.) are passed
        through verbatim; local paths are rendered as ``<bundle>/parquet/``
        when no override is set, reconciled with the lake's stamp by
        `_colocated_data_path`.

        Returns `None` for `statcatlib.bundle.Embedded` — meaning "omit the
        option entirely and let the lake's own stamp stand".
        """
        if isinstance(self._data_path, Embedded):
            return None, False
        if self._data_path is None:
            return self._colocated_data_path(bundle_dir)
        raw = str(self._data_path)
        return (raw if raw.endswith("/") else f"{raw}/"), False

    def _colocated_data_path(self, bundle_dir: Path) -> tuple[str, bool]:
        """The DATA_PATH for a default open: ``<bundle>/parquet/``, as the lake spells it.

        DuckLake compares DATA_PATH with its stamp as strings, so the same
        directory spelled differently was refused (GH-23). The stamp is read
        first:

        - the same directory, spelled any way (a relative open, a symlink):
          passed back verbatim, which DuckLake accepts unchanged;
        - relative and ending in ``<bundle name>/parquet``, as
          ``Catalog("relative")`` stamped it before GH-23: DuckLake resolves
          it against the working directory, so it is replaced by the absolute
          path. A read-write open rewrites the stamp, which repairs the
          catalog; a read-only one overrides it for this attach only;
        - anything else, a remote URI or another directory: the absolute
          path, unchanged, so a split bundle still fails at ATTACH rather than
          reading an empty local directory.
        """
        parquet = bundle_dir / "parquet"
        target = f"{parquet}/"
        lake = bundle_dir / "lake.ducklake"
        if not lake.exists():
            return target, False
        try:
            stamp = read_current_data_path(lake)
        except duckdb.Error, FileNotFoundError:
            return target, False
        stamped = Path(stamp)
        if "://" in stamp or stamp == target:
            return target, False
        if stamped.is_absolute():
            return (stamp, False) if stamped.resolve() == parquet.resolve() else (target, False)
        colocated = stamped.name == "parquet" and stamped.parent.name in {"", bundle_dir.name}
        if not colocated:
            return target, False
        if self._read_only:
            return target, True
        normalize_lake_data_path(lake, target)
        return target, False

    @classmethod
    def from_connection(
        cls,
        conn: duckdb.DuckDBPyConnection,
        *,
        embedding_model: EmbeddingModel | None = None,
        document_prefix: str = "",
        query_prefix: str = "",
        read_only: bool = False,
    ) -> Self:
        """Wrap an existing DuckDB connection.

        The Catalog does NOT take ownership of the connection — closing the
        Catalog leaves the connection open. Use this when the connection is
        shared with another component (e.g. an sdmxlib `Registry`
        co-located in the same DuckDB).

        ``embedding_model`` is rejected here because shared-connection mode
        has no on-disk ``data/`` directory to host the Lance index.

        As `open` does, this raises `IncompatibleCatalogError` when the
        connection's SDMX structure was stored by an sdmxlib with another
        store schema, which this one would drop. *conn* is left open then:
        the caller owns it.

        Args:
            conn: The connection to wrap. Left open by `close`.
            embedding_model: Must be ``None``; see above.
            document_prefix: Prefix for documents embedded into the index.
            query_prefix: Prefix for queries embedded against the index.
            read_only: Open the catalog schema without creating it.

        Returns:
            A catalog over *conn*, already open.

        Raises:
            ValueError: ``embedding_model`` was given.
        """
        if embedding_model is not None:
            msg = "from_connection() does not support embedding_model — Lance needs a folder-mode catalog"
            raise ValueError(msg)
        instance = cls.__new__(cls)
        instance._uri = ":connection:"  # noqa: SLF001 — alternate constructor sets private state
        instance._conn = conn  # noqa: SLF001
        instance._owns_connection = False  # noqa: SLF001
        instance._bundle_dir = None  # noqa: SLF001
        instance._data_path = None  # noqa: SLF001
        instance._azure_credentials = None  # noqa: SLF001
        instance._embedding_model = None  # noqa: SLF001
        instance._embedding_metadata = None  # noqa: SLF001
        instance._document_prefix = document_prefix  # noqa: SLF001
        instance._query_prefix = query_prefix  # noqa: SLF001
        instance._read_only = read_only  # noqa: SLF001
        instance._bulk_depth = 0  # noqa: SLF001
        instance._lance = None  # noqa: SLF001
        instance._cursor_local = threading.local()  # noqa: SLF001
        if read_only:
            _ = _catalog_schema.open_schema_readonly(conn)
        else:
            _ = _catalog_schema.create_schema(conn)
        _refuse_sdmx_schema_rebuild(conn)
        instance._registry = sdmxlib.LocalRegistry.from_connection(conn)  # noqa: SLF001
        # Shared-connection mode: the caller owns any ATTACH plumbing. The
        # DataStore wraps the same connection without applying its own
        # extensions/ATTACH; lake reads/writes only work if the caller has
        # already ATTACHed a ducklake.
        instance._data = sdmxlib.DuckDBDataStore.from_registry(instance._registry)  # noqa: SLF001
        return instance

    def _init_lance(self) -> None:
        """Set up the LanceDB index (called for all folder-mode catalogs).

        Silently skips if lancedb is not installed — search methods will
        raise ``RuntimeError`` when invoked without the index.
        """
        assert self._bundle_dir is not None  # noqa: S101
        search_dir = self._bundle_dir / "search"
        if self._read_only and not search_dir.exists():
            # Read-only catalogs must not create the search dir. Leaving
            # _lance unset means search APIs raise the usual "no index"
            # error rather than blowing up on a read-only mount.
            return
        try:
            from statcatlib.store.lance import LanceIndex  # noqa: PLC0415

            self._lance = LanceIndex(search_dir)
        except ImportError:
            return

    def _warn_if_index_annotations_changed(self) -> None:
        """Warn if the store's search annotations are not the ones the index was built from.

        A warning, never an error and never a rebuild: opening a bundle in a
        serving pod must not fail or write because an overlay was folded in.
        It fires on every open until ``build_index()`` is run (GH-3).
        """
        recorded = self._lance.annotations if self._lance is not None else None
        if recorded is None or self._conn is None:
            return
        if annotation_fingerprint(self._conn, recorded["types"]) == recorded["fingerprint"]:
            return
        warnings.warn(
            f"search index is out of date: annotations of type {', '.join(recorded['types'])} have changed "
            "since it was built (an overlay folded into the store?), so search does not reflect them. "
            "Rebuild with build_index() to fix. (statcatlib GH-3)",
            stacklevel=3,
        )

    @property
    def index_annotations(self) -> IndexAnnotations | None:
        """The annotation types the search index was built from, and their fingerprint.

        ``None`` when no index is built, or for one built before statcatlib
        recorded them. Compare ``types`` with an annotation overlay's entries
        to tell whether applying it needs a ``build_index()`` to reach search.
        """
        return self._lance.annotations if self._lance is not None else None

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The underlying DuckDB connection (for sharing with sdmxlib LocalRegistry).

        Raises:
            RuntimeError: If the catalog hasn't been opened.
        """
        if self._conn is None:
            msg = (
                f"Catalog({self._uri!r}) is not open. Use it as a context manager "
                "(``with Catalog(uri) as cat: ...``) or call ``.open()`` first."
            )
            raise RuntimeError(msg)
        return self._conn

    @property
    def registry(self) -> sdmxlib.LocalRegistry:
        """The co-located `sdmxlib.LocalRegistry` (Topology B composition).

        Both the ``statcat.*`` schema and the ``sdmx.*`` schema live in the
        same DuckDB file on the same connection, so cross-schema queries
        and ``SqlRelation`` composition work without ATTACH plumbing.
        Mutations through the registry (``cat.registry.add(codelist)``)
        are visible immediately to statcatlib queries that join across schemas.

        Raises:
            RuntimeError: If the catalog hasn't been opened.
        """
        if self._registry is None:
            msg = (
                f"Catalog({self._uri!r}) is not open. Use it as a context manager "
                "(``with Catalog(uri) as cat: ...``) or call ``.open()`` first."
            )
            raise RuntimeError(msg)
        return self._registry

    @property
    def embedding_metadata(self) -> EmbeddingMetadata | None:
        """Full sidecar describing the persisted vector index, or ``None``.

        Populated explicitly via `build_index` or by reading
        ``<catalog>/search/embeddings.json`` on `open`. The fields
        that were not persisted by the writing version surface as ``None``
        — e.g. a bundle built with v2 has no ``embedding_dim``.

        Use `check_index_compatible` for an opinionated
        compatibility check; reach for this property when you need a
        specific field.
        """
        return self._embedding_metadata

    @property
    def index_layout(self) -> dict[str, LanguageLayout]:
        """What each language's search index was built with.

        ``{lang: {"table": …, "rows": …, "stemmer": …}}``, read from the
        search index's manifest; empty when no index has been built. A
        ``stemmer`` of ``None`` means no Snowball stemmer exists for that
        language, so it is indexed unstemmed: queries miss inflected forms,
        and no word is stemmed by another language's rules.

        After a rebuild, check that every language you expect is present
        and names the stemmer you expect. A missing language is one whose
        rows were never tagged with it upstream.

        Unlike `embedding_metadata`, this is populated for BM25-only
        catalogs too — they write no ``embeddings.json``, and are exactly
        where the stemmer matters most.
        """
        if self._lance is None:
            return {}
        return self._lance.describe()

    @property
    def embedding_model_name(self) -> str | None:
        """The model id that produced this catalog's vector index, if known.

        Thin shim over `embedding_metadata`. Populated either
        explicitly via ``build_index(model_name=...)`` or by reading
        ``<catalog>/search/embeddings.json`` on ``open()``.
        """
        return self._embedding_metadata.model if self._embedding_metadata else None

    def check_index_compatible(
        self,
        *,
        model_name: str | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        """Raise an error if the loaded encoder doesn't match the index.

        Raises `IncompatibleIndexError` when the persisted sidecar
        disagrees with the caller's expectations.

        No-op when the bundle has no embeddings sidecar yet — there is
        nothing to be incompatible with. No-op when neither *model_name*
        nor *embedding_dim* is supplied. ``None`` arguments are ignored
        (only fields the caller cares about get checked).

        Args:
            model_name: Expected encoder id (Hugging Face / SentenceTransformers
                style). Compared with `EmbeddingMetadata.model`
                under strict equality.
            embedding_dim: Expected vector dimensionality. Compared with
                `EmbeddingMetadata.embedding_dim` when present in
                the sidecar; older v2 sidecars carry no dim and the check
                is skipped.

        Raises:
            IncompatibleIndexError: When any supplied expectation
                mismatches the persisted sidecar.
        """
        meta = self._embedding_metadata
        if meta is None:
            return
        if model_name is not None and meta.model != model_name:
            msg = f"index was built with model {meta.model!r} but caller loaded {model_name!r}"
            raise IncompatibleIndexError(msg)
        if embedding_dim is not None and meta.embedding_dim is not None and meta.embedding_dim != embedding_dim:
            msg = f"index has {meta.embedding_dim}-d vectors but caller's encoder produces {embedding_dim}-d"
            raise IncompatibleIndexError(msg)

    def attach_embedding_model(self, model: EmbeddingModel) -> None:
        """Attach an encoder to a folder-mode store opened from disk.

        Required for `hybrid_search` and `semantic_search` on stores opened
        from a folder.  The constructor restores the *name* of the encoder
        used at
        ``build_index`` time (from ``<catalog>/search/embeddings.json``)
        but cannot re-instantiate the encoder itself — statcatlib core has no
        dependency on ``sentence-transformers`` or any specific encoder
        library.

        Pass an object that exposes ``.encode(text) -> Sequence[float]``
        whose model matches `embedding_model_name`.  The match is
        the caller's responsibility — the encoder name is opaque to
        statcatlib.

        Args:
            model: The encoder, the one `embedding_model_name` names.

        Example:
            store = Catalog("data/catalog").open()
            if store.embedding_model_name:
                from sentence_transformers import SentenceTransformer

                store.attach_embedding_model(SentenceTransformer(store.embedding_model_name))
        """
        self._embedding_model = model

    @property
    def document_prefix(self) -> str:
        """Prefix prepended to documents at index time (e.g. ``"passage: "``)."""
        return self._document_prefix

    @property
    def query_prefix(self) -> str:
        """Prefix prepended to queries at search time (e.g. ``"query: "``)."""
        return self._query_prefix

    @property
    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A fresh DuckDB cursor with ``search_path='statcat'``.

        Returns a new cursor each call so ad-hoc reads never leave an
        implicit transaction that blocks ``store.transaction()``::

            store.cursor.execute("SELECT identifier FROM dataset WHERE kind = 'open'")
        """
        cur = self.connection.cursor()
        cur.execute("SET search_path = 'statcat'")
        return cur

    @property
    def themes(self) -> ThemesScope:
        """Lazy scope over theme classification schemes: ``store.themes["subjects"]``."""
        return ThemesScope(store=self)

    @property
    def concept_schemes(self) -> ConceptSchemesScope:
        """Lazy scope over SKOS concept schemes: ``store.concept_schemes["uri"]``."""
        return ConceptSchemesScope(store=self)

    @property
    def codes(self) -> CodesScope:
        """Typed search and lookup over SDMX codes — ``cat.codes.find("Toronto")``."""
        return CodesScope(store=self)

    @property
    def concepts(self) -> ConceptsScope:
        """Typed search and lookup over SDMX Concepts — ``cat.concepts.find("Gini")``."""
        return ConceptsScope(store=self)

    @property
    def datasets(self) -> DatasetsScope:
        """Typed dataset search — ``cat.datasets.find(q, mode="hybrid")``."""
        return DatasetsScope(store=self)

    @property
    def reference_metadata(self) -> ReferenceMetadataScope:
        """Typed search over indexed SDMX reference-metadata sections.

        ``cat.reference_metadata.find("data quality for income")`` — the
        semantic-search counterpart to `metadata_for` (which resolves
        by target URN). Requires the index to have been built with
        ``include_reference_metadata=True``.
        """
        return ReferenceMetadataScope(store=self)

    # ------------------------------------------------------------------
    # Reference-metadata resolution (structured / URN path)
    # ------------------------------------------------------------------

    def metadata_for(
        self,
        target_urn: str | sdmxlib.SdmxUrn[object],
        *,
        structure_urn: str | sdmxlib.SdmxUrn[object] | None = None,
    ) -> list[sdmxlib.MetadataSet]:
        """Every reference-metadata set attached to ``target_urn``.

        The structured/URN view over reference metadata — the SDMX 3.0
        ``/metadata/structure/{artefactType}/…`` by-target lookup. Given
        any artefact URN (a code, dataflow, concept, …), return the
        MetadataSets reported against it. Complements
        `reference_metadata` (the semantic-search view) — both read
        the same ``sdmx.metadata_*`` edges, so a search hit and a URL
        resolution stay consistent.

        Args:
            target_urn: Artefact URN to resolve attached metadata for.
            structure_urn: Optional MSD/Metadataflow URN filter.

        Returns:
            Every `sdmxlib.MetadataSet` attached to *target_urn*, in insertion
            order. Empty when the target carries no reference metadata.
        """
        return self.registry.metadata_for_target(target_urn, structure_urn=structure_urn)

    def metadata_report(
        self,
        set_urn: str | sdmxlib.SdmxUrn[object],
    ) -> list[sdmxlib.MetadataSection]:
        """Flat, MSD-typed sections of a reference-metadata set.

        Thin pass-through to `sdmxlib.local.Registry.metadata_report`
        — one `sdmxlib.MetadataSection` per reported value, carrying
        the leaf attribute's ``text_type`` and ``is_presentational`` from
        the MSD. This is the sectioned view a caller renders or an indexer
        chunks.
        """
        return self.registry.metadata_report(set_urn)

    def metadata_targets(self, set_urn: str | sdmxlib.SdmxUrn[object]) -> list[str]:
        """The artefact URNs a reference-metadata set describes.

        The document→entity direction: given a set (e.g. from a
        `ReferenceMetadataHit`), return the URNs of the artefacts it
        targets — the topics/dataflows a methodology section is about — so a
        search hit can be resolved back to the structured graph. The inverse
        of `metadata_for`.
        """
        rows = self._c.execute(
            "SELECT target_urn FROM sdmx.metadata_target WHERE set_urn = ? ORDER BY ordinal",
            [str(set_urn)],
        ).fetchall()
        return [str(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # SKOS concept scheme and concept persistence
    # ------------------------------------------------------------------

    def _put_concept_scheme(self, scheme: ConceptScheme) -> None:
        """Persist a ConceptScheme and cascade to its Concept children.

        Upserts the scheme row, replaces top_concept rows, and calls
        ``put_concept()`` for each concept in ``scheme.concepts``.
        """
        self._exec(
            """INSERT INTO concept_scheme (uri, label, definition)
            VALUES (?, ?, ?)
            ON CONFLICT (uri) DO UPDATE SET
                label = excluded.label,
                definition = excluded.definition""",
            [scheme.uri, _istring_to_map(scheme.label), _istring_to_map(scheme.definition)],
        )
        self._exec("DELETE FROM concept_scheme_top_concept WHERE scheme_uri = ?", [scheme.uri])
        if scheme.top_concepts:
            lit = _sql_literal
            vals = ", ".join(f"({lit(scheme.uri)}, {lit(n)})" for n in scheme.top_concepts)
            self._c.execute(f"INSERT INTO concept_scheme_top_concept (scheme_uri, notation) VALUES {vals}")
        for concept in scheme.concepts.values():
            self.put_concept(scheme.uri, concept)

    def get_concept_scheme(self, uri: str) -> ConceptScheme | None:
        """Retrieve a ConceptScheme with its full concepts dict populated."""
        row = self._c.execute("SELECT uri, label, definition FROM concept_scheme WHERE uri = ?", [uri]).fetchone()
        if row is None:
            return None
        label = _map_to_istring(row[1]) or InternationalString()
        definition = _map_to_istring(row[2])

        # Load top_concepts (notations)
        tc_rows = self._c.execute(
            "SELECT notation FROM concept_scheme_top_concept WHERE scheme_uri = ? ORDER BY notation",
            [uri],
        ).fetchall()
        top_concepts = tuple(r[0] for r in tc_rows)

        # Load all concepts in this scheme
        c_rows = self._c.execute(
            """SELECT notation, label, alt_label, definition FROM concept
            WHERE in_scheme = ? ORDER BY notation""",
            [uri],
        ).fetchall()
        concepts: dict[str, Concept] = {}
        for c_row in c_rows:
            notation = c_row[0]
            c_label = _map_to_istring(c_row[1]) or InternationalString()
            c_alt_label = _map_to_istring(c_row[2]) or InternationalString()
            c_definition = _map_to_istring(c_row[3])
            # Load relations for this concept
            concept_uri = f"{uri}/{notation}"
            rel_rows = self._c.execute(
                """SELECT r.relation, c2.notation
                FROM concept_relation r
                JOIN concept c2 ON c2.uri = r.target_uri
                WHERE r.source_uri = ?""",
                [concept_uri],
            ).fetchall()
            broader: list[str] = []
            narrower: list[str] = []
            related: list[str] = []
            for rel_row in rel_rows:
                if rel_row[0] == "broader":
                    broader.append(rel_row[1])
                elif rel_row[0] == "narrower":
                    narrower.append(rel_row[1])
                elif rel_row[0] == "related":
                    related.append(rel_row[1])
            concepts[notation] = Concept(
                notation=notation,
                label=c_label,
                alt_label=c_alt_label,
                definition=c_definition,
                broader=tuple(broader),
                narrower=tuple(narrower),
                related=tuple(related),
            )

        return ConceptScheme(
            uri=uri,
            label=label,
            definition=definition,
            concepts=concepts,
            top_concepts=top_concepts,
        )

    def put_concept(self, scheme_uri: str, concept: Concept) -> None:
        """Persist a single Concept within its scheme.

        The concept URI is derived as ``{scheme_uri}/{concept.notation}``.
        ``broader``/``narrower``/``related`` are notations in the same scheme;
        their URIs are resolved the same way.
        """
        self._require_writable("put_concept()")
        concept_uri = f"{scheme_uri}/{concept.notation}"
        self._exec(
            """INSERT INTO concept (uri, in_scheme, notation, label, alt_label, definition)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (uri) DO UPDATE SET
                notation = excluded.notation,
                label = excluded.label,
                alt_label = excluded.alt_label,
                definition = excluded.definition""",
            [
                concept_uri,
                scheme_uri,
                concept.notation,
                _istring_to_map(concept.label),
                _istring_to_map(concept.alt_label),
                _istring_to_map(concept.definition),
            ],
        )
        self._exec("DELETE FROM concept_relation WHERE source_uri = ?", [concept_uri])
        lit = _sql_literal
        rel_rows: list[tuple[str, str, str]] = [
            *[(concept_uri, f"{scheme_uri}/{n}", "broader") for n in concept.broader],
            *[(concept_uri, f"{scheme_uri}/{n}", "narrower") for n in concept.narrower],
            *[(concept_uri, f"{scheme_uri}/{n}", "related") for n in concept.related],
        ]
        if rel_rows:
            vals = ", ".join(f"({lit(r[0])}, {lit(r[1])}, {lit(r[2])})" for r in rel_rows)
            self._c.execute(
                f"INSERT INTO concept_relation (source_uri, target_uri, relation) VALUES {vals} ON CONFLICT DO NOTHING"
            )

    def get_concept(self, uri: str) -> Concept | None:
        """Retrieve a single Concept by URI."""
        row = self._c.execute(
            "SELECT notation, label, alt_label, definition FROM concept WHERE uri = ?", [uri]
        ).fetchone()
        if row is None:
            return None
        notation = row[0]
        label = _map_to_istring(row[1]) or InternationalString()
        alt_label = _map_to_istring(row[2]) or InternationalString()
        definition = _map_to_istring(row[3])

        rel_rows = self._c.execute(
            """SELECT r.relation, c2.notation
            FROM concept_relation r
            JOIN concept c2 ON c2.uri = r.target_uri
            WHERE r.source_uri = ?""",
            [uri],
        ).fetchall()
        broader: list[str] = []
        narrower: list[str] = []
        related: list[str] = []
        for rel_row in rel_rows:
            if rel_row[0] == "broader":
                broader.append(rel_row[1])
            elif rel_row[0] == "narrower":
                narrower.append(rel_row[1])
            elif rel_row[0] == "related":
                related.append(rel_row[1])

        return Concept(
            notation=notation,
            label=label,
            alt_label=alt_label,
            definition=definition,
            broader=tuple(broader),
            narrower=tuple(narrower),
            related=tuple(related),
        )

    def __enter__(self) -> Self:
        if self._conn is None:
            self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection (only if this catalog owns it).

        After ``close()``, the catalog can be re-opened with `open`.
        """
        if self._conn is not None and self._owns_connection:
            self._conn.close()
        self._conn = None
        # Drop all per-thread cursors — they're invalidated with the connection.
        self._cursor_local = threading.local()
        self._registry = None

    @property
    def _c(self) -> duckdb.DuckDBPyConnection:
        """Return this thread's DuckDB cursor, creating it on first access.

        DuckDB cursors are not thread-safe: concurrent ``execute()`` calls on
        the same cursor corrupt its pending-result state and surface as
        ``InvalidInputException``. Each thread therefore gets its own cursor
        on the shared connection, lazily created and cached in
        ``self._cursor_local`` (a `threading.local`). The cursor's
        ``search_path`` is pinned to ``statcat`` to match the connection-level
        schema setup done in `open`.
        """
        conn = self._conn
        if conn is None:
            msg = (
                f"Catalog({self._uri!r}) is not open. Use it as a context manager "
                "(``with Catalog(uri) as cat: ...``) or call ``.open()`` first."
            )
            raise RuntimeError(msg)
        cursor = getattr(self._cursor_local, "cursor", None)
        if cursor is None:
            cursor = conn.cursor()
            cursor.execute("SET search_path = 'statcat'")
            self._cursor_local.cursor = cursor
        return cursor

    @property
    def bundle_dir(self) -> Path | None:
        """The bundle directory in folder mode, else ``None`` for in-memory.

        Holds ``meta.duckdb``, ``lake.ducklake``, ``parquet/``, and
        optional ``search/``.
        """
        return self._bundle_dir

    @property
    def data(self) -> sdmxlib.DuckDBDataStore:
        """The bundle's data plane.

        A `sdmxlib.DuckDBDataStore` sharing the catalog's DuckDB
        connection, with the DuckLake sidecar ATTACHed as ``lake``
        (folder mode) or unattached (in-memory / shared-connection mode).
        Producers write observations through
        ``cat.data.write_observations(schema, table, source)``; that
        routes through the lake when one is attached.

        Raises:
            RuntimeError: If the catalog hasn't been opened.
        """
        if self._data is None:
            msg = (
                f"Catalog({self._uri!r}) is not open. Use it as a context manager "
                "(``with Catalog(uri) as cat: ...``) or call ``.open()`` first."
            )
            raise RuntimeError(msg)
        return self._data

    def _exec(self, sql: str, params: Sequence[object] | None = None) -> None:
        """Execute a DDL or DML statement on the catalog cursor."""
        if params is None:
            _ = self._c.execute(sql)
        else:
            _ = self._c.execute(sql, params)

    @contextlib.contextmanager
    def bulk_load(self) -> Generator[Self]:
        """Context manager for high-throughput catalog ingest.

        Raises the WAL auto-checkpoint threshold to 1 GB so DuckDB
        does not checkpoint after every 16 MB of writes, then runs
        a single checkpoint on exit.

        Re-entrant: nested ``bulk_load()`` calls are no-ops — only the
        outermost level controls WAL settings, checkpoint, and index
        rebuild.

        .. code-block:: python

            with store.bulk_load():
                for dataset in datasets:
                    store.put(dataset)
            # single checkpoint here
        """
        self._require_writable("bulk_load()")
        self._bulk_depth += 1
        if self._bulk_depth == 1:
            self._c.execute("SET wal_autocheckpoint = '1GB'")
        try:
            yield self
        finally:
            self._bulk_depth -= 1
            if self._bulk_depth == 0:
                self._c.execute("SET wal_autocheckpoint = '16MB'")
                self.connection.execute("CHECKPOINT")

    @contextlib.contextmanager
    def transaction(self) -> Generator[Self]:
        """Wrap multiple writes in a single DuckDB transaction.

        Commits on clean exit, rolls back on exception.  Composes with
        ``bulk_load()`` for batched high-throughput ingest::

            with store.bulk_load():
                for batch in batched(datasets, 500):
                    with store.transaction():
                        for ds in batch:
                            store.put(ds)
        """
        self._c.begin()
        try:
            yield self
            self._c.commit()
        except BaseException:
            self._c.rollback()
            raise

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _require_writable(self, op: str) -> None:
        """Raise if the catalog was opened ``read_only=True``."""
        if self._read_only:
            msg = f"Catalog opened read_only=True; {op} is not available."
            raise RuntimeError(msg)

    def put(self, obj: Dataset | ConceptScheme | Sequence[Dataset | ConceptScheme]) -> None:
        """Add or update a dataset, concept scheme, or list of either.

        Dispatches on type:

        - ``BaseDataset`` (and subclasses) — upserts the dataset record and
          all bridge rows (themes, keywords, distributions, etc.).
        - ``ConceptScheme`` — upserts the scheme and cascades to its concepts.
        - ``list`` / ``Sequence`` — calls ``put()`` for each element.
        """
        self._require_writable("put()")
        # Tested positively, and datasets first: `isinstance(obj, (list, tuple))`
        # let any other Sequence fall through into the dataset branch, which the
        # `cast` here then hid from the checker.
        if isinstance(obj, BaseDataset):
            dim = self._upsert_dimension_records(obj)
            self._upsert_fact_row(obj, dim)
            self._write_bridge_rows(obj)
        elif isinstance(obj, ConceptScheme):
            self._put_concept_scheme(obj)
        else:
            for item in obj:
                self.put(item)

    # ------------------------------------------------------------------
    # Semantic & hybrid search
    # ------------------------------------------------------------------

    def _rrf_rank(
        self,
        text: str,
        *,
        lang: str = "en",
        rrf_k: int = 60,
        oversample: int = 200,
        operator: str = "or",
        fuzziness: int = 0,
    ) -> dict[str, float]:
        """Compute RRF-merged identifier ranking from vector + BM25 hits."""
        assert self._lance is not None  # noqa: S101
        assert self._embedding_model is not None  # noqa: S101

        query_vec: list[float] = as_vector(self._embedding_model.encode(self._query_prefix + text))
        vec_hits = self._lance.search(query_vec, lang=lang, limit=oversample, entity_type=_DATASET_ENTITY_TYPES)
        vec_rank: dict[str, int] = {ident: i + 1 for i, (ident, _) in enumerate(vec_hits)}

        bm25_hits = self._lance.bm25_search(
            text,
            lang=lang,
            limit=oversample,
            operator=operator,
            fuzziness=fuzziness,
            entity_type=_DATASET_ENTITY_TYPES,
        )
        bm25_rank: dict[str, int] = {ident: i + 1 for i, (ident, _) in enumerate(bm25_hits)}

        all_ids = set(vec_rank) | set(bm25_rank)
        rrf: dict[str, float] = {}
        for ident in all_ids:
            score = 0.0
            if ident in vec_rank:
                score += 1.0 / (rrf_k + vec_rank[ident])
            if ident in bm25_rank:
                score += 1.0 / (rrf_k + bm25_rank[ident])
            rrf[ident] = score

        return rrf

    def _fetch_by_identifiers(self, identifiers: list[str]) -> list[BaseDataset]:
        """Fetch datasets by identifier list, preserving order."""
        if not identifiers:
            return []
        datasets = self._fetch_datasets(identifiers)
        order = {ident: i for i, ident in enumerate(identifiers)}
        return sorted(datasets, key=lambda ds: order.get(ds.identifier, 9999))

    def _collect_search_texts(self) -> list[tuple[str, str, str, str]]:
        """Collect ``(id, kind, lang, text)`` tuples for all datasets.

        Uses a single SQL query with LEFT JOINs instead of per-dataset
        object reconstruction — one query instead of a round-trip per dataset.
        """
        db_rows = self._c.execute("""
            SELECT
                d.identifier,
                d.kind,
                d.title,
                d.description,
                LIST(DISTINCT dk.keyword),
                LIST(DISTINCT c.label),
                LIST(DISTINCT dn.label)
            FROM dataset d
            LEFT JOIN dataset_keyword dk ON dk.dataset_id = d.identifier
            LEFT JOIN dataset_theme dt   ON dt.dataset_id = d.identifier
            LEFT JOIN concept c          ON c.uri = dt.concept_uri
            LEFT JOIN dimension_name dn  ON dn.dataset_id = d.identifier
            GROUP BY d.identifier, d.kind, d.title, d.description
        """).fetchall()

        items: list[tuple[str, str, str, str]] = []
        for ident, kind, title_map, desc_map, kw_maps, theme_maps, dim_maps in db_rows:
            items.extend(self._texts_for_row(ident, kind, title_map, desc_map, kw_maps, theme_maps, dim_maps))
        return items

    @staticmethod
    def _texts_for_row(
        ident: str,
        kind: str,
        title_map: dict[str, str] | None,
        desc_map: dict[str, str] | None,
        kw_maps: list[dict[str, str]] | None,
        theme_maps: list[dict[str, str]] | None,
        dim_maps: list[dict[str, str]] | None,
    ) -> list[tuple[str, str, str, str]]:
        """Build ``(id, kind, lang, text)`` tuples for one dataset row."""
        langs = set(title_map or {}) | set(desc_map or {})
        if not langs:
            langs = {"en"}
        result: list[tuple[str, str, str, str]] = []
        for lang in sorted(langs):
            parts = [
                (title_map or {}).get(lang, ""),
                (desc_map or {}).get(lang, ""),
            ]
            parts.extend(m[lang] for m in kw_maps or [] if m and lang in m)
            parts.extend(m[lang] for m in theme_maps or [] if m and lang in m)
            parts.extend(m[lang] for m in dim_maps or [] if m and lang in m)
            text = " ".join(p for p in parts if p)
            if text:
                result.append((ident, kind, lang, text))
        return result

    def _collect_code_texts(  # noqa: C901
        self,
        *,
        max_codes_per_codelist: int = 500,
        codelist_urns: Sequence[str] | None = None,
        include_alt_labels: bool = True,
        carry_hierarchy_fields: bool = True,
        reference_metadata_blends: Sequence[ReferenceMetadataBlend] = (),
        context_annotation_types: Sequence[str] = (SEARCH_CONTEXT_ANNOTATION_TYPE,),
        exclude_annotation_types: Sequence[str] = (SEARCH_EXCLUDE_ANNOTATION_TYPE,),
        codelist_policies: Sequence[CodelistIndexPolicy] = (),
    ) -> list[SearchRow]:
        """Collect LanceDB row dicts for individual SDMX codes (one per language).

        Joins ``dataset`` -> ``sdmx.dataflow`` -> ``sdmx.dsd_component`` ->
        ``sdmx.code`` to emit one search row per (dataset, dimension, code,
        language).  Each row's ``text`` is formatted as
        ``"{dataset_title} | {dimension_label}: {code_label}"`` — a shape that
        works well for RAG retrieval — and the row also carries
        structured fields so callers don't re-parse the joined text:
        ``dimension_id`` / ``dimension_label`` / ``code_id`` /
        ``code_label`` plus ``code_urn`` / ``codelist_urn`` /
        ``parent_id`` / ``depth`` (used by hybrid code search and
        hierarchy-aware UIs).

        Args:
            max_codes_per_codelist: Skip codelists with more entries than
                this. Defaults to 500 — keeps high-cardinality dimensions
                like postal codes out of the index.
            codelist_urns: Restrict indexing to these codelists only.
                ``None`` (default) embeds every codelist referenced by an
                indexed dataflow. The filter is in addition to
                ``max_codes_per_codelist``.
            include_alt_labels: Blend SKOS ``altLabel`` annotations into
                each code's search text. Default ``True`` — these are
                first-class SDMX constructs and they reliably lift recall
                on aliases/abbreviations. Annotations carrying localised
                text (``sdmx.annotation_text``) fold into their own
                language's row only; those carrying a non-localised
                ``annotation.title`` fold into every language's row.
            carry_hierarchy_fields: Populate ``parent_id`` / ``depth``
                from the SDMX hierarchical-codelist model. Default
                ``True``. Pure metadata — no text impact — set to
                ``False`` only to match an older row shape.
            reference_metadata_blends: For each entry, join
                ``sdmx.metadata_target`` → ``sdmx.metadata_value`` for
                the given ``attribute_path`` and append its localised
                value (truncated to ``max_chars``) to the code's search
                text. Default empty — opt-in per catalog. See
                `ReferenceMetadataBlend`.
            context_annotation_types: Annotation types whose *localised*
                text (from ``sdmx.annotation_text``) is appended to each
                code's search text, per language. Defaults to
                ``(SEARCH_CONTEXT_ANNOTATION_TYPE,)`` — statcatlib's own
                ``"statcat:searchContext"`` convention, folded by default
                the way ``include_alt_labels`` folds ``skos:altLabel``.
                Codes without such annotations are unaffected; pass
                ``()`` to disable. Unlike altLabels, the text is selected
                per language so an EN row gets EN context and an FR row
                FR — no cross-language smear.
            exclude_annotation_types: Codes carrying an annotation of one
                of these types get no search rows. Defaults to
                ``(SEARCH_EXCLUDE_ANNOTATION_TYPE,)`` — statcatlib's
                ``"statcat:searchExclude"`` convention. Pass ``()`` to index
                every code regardless.
            codelist_policies: Per-codelist index overrides. Codelists
                listed with ``mode="fts_only"`` bypass
                ``max_codes_per_codelist`` and are emitted with
                ``embedding=None`` (see `CodelistIndexPolicy`).

        Returns:
            One row dict per (code, language), ready for the Lance writer.
        """
        # Guard: sdmx schema may not exist if no SDMX data has been ingested
        has_sdmx = self._c.execute(
            "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'sdmx'"
        ).fetchone()
        if not has_sdmx or has_sdmx[0] == 0:
            return []

        fts_only_urns: set[str] = _fts_only_urns(codelist_policies)
        self._warn_oversized_codelists(max_codes_per_codelist, exclude=fts_only_urns)

        where_clause, params = _code_index_filter(
            max_codes_per_codelist=max_codes_per_codelist,
            fts_only_urns=fts_only_urns,
            codelist_urns=codelist_urns,
            exclude_annotation_types=exclude_annotation_types,
        )

        sql = f"""
            WITH codelist_size AS (
                SELECT codelist_urn, COUNT(*) AS n
                FROM sdmx.code
                GROUP BY codelist_urn
            )
            SELECT
                d.identifier,
                d.title,
                dc.component_id                    AS dimension_id,
                COALESCE(con.name, cl_text.label)  AS dim_label,
                c.code_id                          AS code_id,
                c.name                             AS code_label,
                c.code_urn                         AS code_urn,
                c.codelist_urn                     AS codelist_urn,
                c.parent_id                        AS parent_id,
                c.depth                            AS depth
            FROM dataset d
            JOIN sdmx.dataflow df          ON df.urn = d.sdmx_dataflow_urn
            JOIN sdmx.dsd_component dc     ON dc.dsd_urn = df.dsd_urn
                                           AND dc.component_type = 'Dimension'
            JOIN codelist_size cs           ON cs.codelist_urn = dc.codelist_urn
            JOIN sdmx.code c               ON c.codelist_urn = dc.codelist_urn
            LEFT JOIN sdmx.concept con     ON con.concept_urn = dc.concept_urn
            LEFT JOIN (
                SELECT owner_urn, MAP_FROM_ENTRIES(LIST({{k: lang, v: text}})) AS label
                FROM sdmx.localized_text
                WHERE field = 'name'
                GROUP BY owner_urn
            ) cl_text ON cl_text.owner_urn = dc.codelist_urn
            WHERE {where_clause}
        """
        rows = self._c.execute(sql, params).fetchall()

        alt_by_code: dict[str, dict[str, list[str]]] = {}
        if include_alt_labels and rows:
            alt_by_code = self._load_code_alt_labels(codelist_urns=codelist_urns)

        blend_text_by_code: dict[str, dict[str, list[str]]] = {}
        if reference_metadata_blends and rows:
            blend_text_by_code = self._load_code_reference_metadata(
                blends=reference_metadata_blends,
                codelist_urns=codelist_urns,
            )

        context_by_code: dict[str, dict[str, list[str]]] = {}
        if context_annotation_types and rows:
            context_by_code = self._load_code_context_labels(
                codelist_urns=codelist_urns,
                annotation_types=context_annotation_types,
            )

        items: list[SearchRow] = []
        for (
            ident,
            title_map,
            dim_id,
            dim_label_map,
            code_id,
            code_label_map,
            code_urn,
            codelist_urn,
            parent_id,
            depth,
        ) in rows:
            # Determine languages from the dataset title
            langs = set(title_map or {})
            if not langs:
                langs = {"en"}
            alt_by_lang = alt_by_code.get(str(code_urn), {})
            blend_by_lang = blend_text_by_code.get(str(code_urn), {})
            context_by_lang = context_by_code.get(str(code_urn), {})
            for lang in sorted(langs):
                ds_title = (title_map or {}).get(lang, "")
                dim_label = (dim_label_map or {}).get(lang, "") if dim_label_map else ""
                code_label = (code_label_map or {}).get(lang, "") if code_label_map else ""
                if not code_label:
                    continue
                parts: list[str] = [
                    f"{ds_title} | {dim_label}: {code_label}" if dim_label else f"{ds_title} | {code_label}",
                ]
                # Own-language synonyms plus any that carry no language.
                # A producer publishing language-tagged altLabels gets them
                # folded into that language's row alone; untagged titles
                # still reach every language, as they always have.
                alt_labels = [*alt_by_lang.get(lang, []), *alt_by_lang.get(_ANY_LANG, [])]
                if alt_labels:
                    parts.append(" | ".join(alt_labels))
                # Per-language context (e.g. the code's topical-universe
                # label) — the leaf label leads, so a bare leaf query still
                # prefers leaves named exactly that, while a topic-flavoured
                # query also matches the universe tokens. (#61)
                context_texts = context_by_lang.get(lang, [])
                if context_texts:
                    parts.append(" | ".join(context_texts))
                # Own-language footnote text plus any value that carries no
                # language (scalars: URLs, vintages). Same rule as the
                # altLabels above — see #100.
                parts.extend([*blend_by_lang.get(lang, []), *blend_by_lang.get(_ANY_LANG, [])])
                text = " | ".join(parts)
                items.append(
                    {
                        "id": ident,
                        "entity_type": "code",
                        "lang": lang,
                        "text": text,
                        "dimension_id": dim_id or "",
                        "dimension_label": dim_label,
                        "code_id": code_id or "",
                        "code_label": code_label,
                        "code_urn": str(code_urn or ""),
                        "codelist_urn": str(codelist_urn or ""),
                        "parent_id": (str(parent_id) if carry_hierarchy_fields and parent_id else ""),
                        "depth": int(depth) if carry_hierarchy_fields and depth is not None else 0,
                        "concept_urn": "",
                        "scheme_urn": "",
                        "concept_id": "",
                        "set_urn": "",
                        "attribute_path": "",
                        "section_title": "",
                    }
                )
        return items

    def _collect_concept_texts(
        self,
        *,
        scheme_urns: Sequence[str] | None = None,
    ) -> list[SearchRow]:
        """Collect LanceDB row dicts for SDMX Concepts (one per language).

        Concepts carry definitional text in ``sdmx.concept.description``
        — proper paragraphs of natural language — which makes them the
        single highest-value vector-search target in an SDMX catalog.
        A query like "income inequality across metropolitan areas" is
        far more likely to hit a Concept titled "Gini coefficient" by
        its definition than any code label.

        One row per ``(concept_urn, lang)``. ``id`` is the concept URN so
        FTS hits dedupe by concept identity; ``entity_type='concept'``
        keeps these rows out of `Catalog.search` (which filters to
        dataset kinds).

        Args:
            scheme_urns: Restrict to concepts from these
                ConceptScheme URNs. ``None`` (default) emits every
                ingested concept.

        Returns:
            One row dict per (concept, language), ready for the Lance writer.
        """
        has_sdmx = self._c.execute(
            "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'sdmx'"
        ).fetchone()
        if not has_sdmx or has_sdmx[0] == 0:
            return []

        where_parts: list[str] = ["c.name IS NOT NULL"]
        params: dict[str, object] = {}
        if scheme_urns is not None:
            where_parts.append("c.scheme_urn IN (SELECT UNNEST($scheme_urns))")
            params["scheme_urns"] = list(scheme_urns)
        where_clause = " AND ".join(where_parts)

        sql = f"""
            SELECT
                c.concept_urn,
                c.scheme_urn,
                c.concept_id,
                c.name,
                c.description
            FROM sdmx.concept c
            WHERE {where_clause}
        """
        rows = self._c.execute(sql, params).fetchall()

        items: list[SearchRow] = []
        for concept_urn, scheme_urn, concept_id, name_map, desc_map in rows:
            langs = set(name_map or {}) | set(desc_map or {})
            if not langs:
                continue
            for lang in sorted(langs):
                name = (name_map or {}).get(lang, "")
                desc = (desc_map or {}).get(lang, "") if desc_map else ""
                if not name and not desc:
                    continue
                text = f"{name} | {desc}" if (name and desc) else (name or desc)
                items.append(
                    {
                        "id": str(concept_urn),
                        "entity_type": "concept",
                        "lang": lang,
                        "text": text,
                        "dimension_id": "",
                        "dimension_label": "",
                        "code_id": "",
                        "code_label": "",
                        "code_urn": "",
                        "codelist_urn": "",
                        "parent_id": "",
                        "depth": 0,
                        "set_urn": "",
                        "attribute_path": "",
                        "section_title": "",
                        "concept_urn": str(concept_urn),
                        "scheme_urn": str(scheme_urn or ""),
                        "concept_id": str(concept_id or ""),
                    }
                )
        return items

    def _collect_reference_metadata_texts(
        self,
        *,
        metadataflow_urns: Sequence[str] | None = None,
        min_chars: int = _REFMETA_MIN_CHARS,
        max_chunk_chars: int = _REFMETA_CHUNK_CHARS,
    ) -> list[SearchRow]:
        """Collect LanceDB rows for SDMX reference-metadata sections.

        Turns each multilingual (``value_kind='i18n'``) reported value in
        the ``sdmx.metadata_*`` tables into one or more embeddable chunks —
        the methodology-document counterpart to code/concept rows. Unlike
        the reference-metadata *blend* (which denormalises short text onto a
        dimension code), this indexes the section itself as a first-class,
        retrievable unit that navigates back to its MetadataSet and, from
        there, to the artefacts the document describes.

        One row per ``(set, section, lang, chunk)``. ``id`` is
        ``"<set_urn>#<attribute_path>#<chunk_index>"``; ``entity_type`` is
        ``"reference_metadata"``. Scalar values (codes, ids, URLs) and
        sections below ``min_chars`` are skipped as non-prose.

        Args:
            metadataflow_urns: Restrict to sets whose structure is one of
                these MSD/Metadataflow URNs (e.g. only the reference-guide
                and methodology flows). ``None`` indexes every set.
            min_chars: Drop sections shorter than this (provenance noise).
            max_chunk_chars: Target chunk size; longer sections are split.

        Returns:
            One row dict per (reported attribute, language), ready for the
            Lance writer.
        """
        has_sdmx = self._c.execute(
            "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = 'sdmx'"
        ).fetchone()
        if not has_sdmx or has_sdmx[0] == 0:
            return []

        where_parts = ["v.value_kind = 'i18n'"]
        params: dict[str, object] = {}
        join = ""
        if metadataflow_urns is not None:
            join = "JOIN sdmx.metadata_set s ON s.urn = v.set_urn"
            where_parts.append("s.structure_urn IN (SELECT UNNEST($mdf_urns))")
            params["mdf_urns"] = list(metadataflow_urns)
        where_clause = " AND ".join(where_parts)

        rows = self._c.execute(
            f"""
            SELECT v.set_urn, v.attribute_path, v.i18n
            FROM sdmx.metadata_value v
            {join}
            WHERE {where_clause}
            ORDER BY v.set_urn, v.attribute_path, v.sibling_index
            """,
            params,
        ).fetchall()

        items: list[SearchRow] = []
        for set_urn, attribute_path, i18n_map in rows:
            if not i18n_map:
                continue
            section_title = _humanize_section(str(attribute_path))
            for lang, raw in i18n_map.items():
                # Sections are stored as XHTML (text_type=XHTML); down-convert
                # to compact markdown so the embedding sees content, not tags.
                # A no-op on values that are already plain text.
                body = xhtml_to_markdown(raw or "").strip()
                if len(body) < min_chars:
                    continue
                chunks = _chunk_prose(body, max_chars=max_chunk_chars)
                for chunk_index, chunk in enumerate(chunks):
                    items.append(
                        {
                            "id": f"{set_urn}#{attribute_path}#{chunk_index}",
                            "entity_type": "reference_metadata",
                            "lang": str(lang),
                            # Lead with the section title so a section-name
                            # query ("data quality") matches on BM25 even
                            # when the body doesn't repeat the heading.
                            "text": f"{section_title} | {chunk}",
                            "dimension_id": "",
                            "dimension_label": "",
                            "code_id": "",
                            "code_label": "",
                            "code_urn": "",
                            "codelist_urn": "",
                            "parent_id": "",
                            "depth": 0,
                            "concept_urn": "",
                            "scheme_urn": "",
                            "concept_id": "",
                            "set_urn": str(set_urn),
                            "attribute_path": str(attribute_path),
                            "section_title": section_title,
                        }
                    )
        return items

    def _attach_reference_metadata_embeddings(
        self,
        refmeta_rows: list[SearchRow],
        *,
        embed_reference_metadata: bool,
        progress: Callable[[int, int], None] | None,
    ) -> None:
        """Populate ``embedding`` on reference-metadata rows in place.

        Mirror of `_attach_concept_embeddings`. Reference-metadata
        sections are paragraph-shaped prose — exactly what embeddings are
        for — so they default to embedded.
        """
        if not refmeta_rows or self._embedding_model is None:
            return
        if embed_reference_metadata:
            items = [(r["id"], r["entity_type"], r["lang"], r["text"]) for r in refmeta_rows]
            encoded = self._batch_encode(items, progress=progress)
            for row, enc in zip(refmeta_rows, encoded, strict=True):
                row["embedding"] = enc.get("embedding")
            return
        for row in refmeta_rows:
            row["embedding"] = None

    def _attach_concept_embeddings(
        self,
        concept_rows: list[SearchRow],
        *,
        embed_concepts: bool,
        progress: Callable[[int, int], None] | None,
    ) -> None:
        """Populate the ``embedding`` column on *concept_rows* in place.

        Mirror of `_attach_code_embeddings`. Concepts default to
        embedded — their descriptions are exactly the kind of free-text
        passages embedding models are trained on, so the marginal cost
        per row buys a real recall lift.
        """
        if not concept_rows or self._embedding_model is None:
            return
        if embed_concepts:
            items = [(r["id"], r["entity_type"], r["lang"], r["text"]) for r in concept_rows]
            encoded = self._batch_encode(items, progress=progress)
            for row, enc in zip(concept_rows, encoded, strict=True):
                row["embedding"] = enc.get("embedding")
            return
        for row in concept_rows:
            row["embedding"] = None

    def _warn_oversized_codelists(
        self,
        max_codes_per_codelist: int,
        *,
        exclude: set[str] | None = None,
    ) -> None:
        """Emit a warning for codelists silently dropped by the cap.

        Silent dropping has bitten callers: a characteristics codelist of a
        few thousand codes vanished under the 500 default with no
        diagnostic. One stack-level-3 warning gives
        callers something to act on without changing the default.
        """
        oversized = self._c.execute(
            """
            WITH codelist_size AS (
                SELECT codelist_urn, COUNT(*) AS n
                FROM sdmx.code
                GROUP BY codelist_urn
            )
            SELECT DISTINCT dc.codelist_urn, cs.n
            FROM dataset d
            JOIN sdmx.dataflow df      ON df.urn = d.sdmx_dataflow_urn
            JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                                       AND dc.component_type = 'Dimension'
            JOIN codelist_size cs       ON cs.codelist_urn = dc.codelist_urn
            WHERE d.sdmx_dataflow_urn IS NOT NULL
              AND cs.n > $max_codes
            ORDER BY cs.n DESC
            """,
            {"max_codes": max_codes_per_codelist},
        ).fetchall()
        # Codelists the caller opted in via CodelistIndexPolicy are not
        # silently skipped — re-flagging them here would be a misleading
        # warning, not a useful one.
        if exclude:
            oversized = [(urn, n) for urn, n in oversized if urn not in exclude]
        if not oversized:
            return
        preview = ", ".join(f"{cl} ({n} codes)" for cl, n in oversized[:3])
        extra = f" and {len(oversized) - 3} more" if len(oversized) > 3 else ""
        warnings.warn(
            f"build_index skipped {len(oversized)} codelist(s) larger than "
            f"max_codes_per_codelist={max_codes_per_codelist}: {preview}{extra}. "
            f"Raise max_codes_per_codelist, pass codelist_urns= to scope explicitly, "
            f"or opt in via codelist_policies=[CodelistIndexPolicy(urns=[...], mode='fts_only')].",
            stacklevel=3,
        )

    def _load_code_alt_labels(
        self,
        *,
        codelist_urns: Sequence[str] | None,
    ) -> dict[str, dict[str, list[str]]]:
        """Return ``{code_urn: {lang_or_ANY: [altLabel, …]}}`` for codes in scope.

        Reads SKOS ``altLabel`` annotations attached to codes from both
        places sdmxlib can hold their text:

        - the localised ``sdmx.annotation_text`` table, keyed by language
          — the same join `_load_code_context_labels` uses. These
          land under their own language tag and are folded into that
          language's row only.
        - the non-localised ``annotation.title`` column, which SDMX-ML 2.1
          annotations populate and which carries no language. These land
          under `_ANY_LANG` and are folded into every language's
          row — the historical behaviour, preserved for catalogs that
          publish untagged titles.

        An annotation contributes to exactly one of the two: when it has
        localised rows its title is ignored rather than duplicated
        alongside them. That lets a producer migrate to language-tagged
        synonyms incrementally — a migrated code stops smearing its
        synonyms across languages as soon as it carries localised text,
        while un-migrated codes keep working unchanged.
        """
        where_parts = ["a.annotation_type = 'skos:altLabel'"]
        params: dict[str, object] = {"any_lang": _ANY_LANG}
        if codelist_urns is not None:
            where_parts.append("c.codelist_urn IN (SELECT UNNEST($codelist_urns))")
            params["codelist_urns"] = list(codelist_urns)
        # COALESCE puts localised text under its own language tag and
        # title-only annotations under the any-language bucket; the LEFT
        # JOIN is what keeps the latter in the result set at all. Text is
        # likewise preferred over title, so a migrated annotation never
        # contributes its title as well.
        sql = f"""
            SELECT c.code_urn,
                   COALESCE(axt.lang, $any_lang) AS alt_lang,
                   list(COALESCE(axt.text, a.title))
            FROM sdmx.code c
            JOIN sdmx.annotation a             ON a.owner_urn = c.code_urn
            LEFT JOIN sdmx.annotation_text axt ON axt.annotation_id = a.id
            WHERE {" AND ".join(where_parts)}
            GROUP BY c.code_urn, alt_lang
        """
        rows = self._c.execute(sql, params).fetchall()
        out: dict[str, dict[str, list[str]]] = {}
        for code_urn, alt_lang, texts in rows:
            cleaned = [str(t) for t in (texts or []) if t]
            if cleaned:
                out.setdefault(str(code_urn), {})[str(alt_lang)] = cleaned
        return out

    def _load_code_context_labels(
        self,
        *,
        codelist_urns: Sequence[str] | None,
        annotation_types: Sequence[str],
    ) -> dict[str, dict[str, list[str]]]:
        """Return ``{code_urn: {lang: [context_text, …]}}`` per language.

        Covers codes carrying annotations of ``annotation_types``.
        Reads the *localised* ``sdmx.annotation_text`` table (joined to
        ``sdmx.annotation`` on ``annotation_text.annotation_id =
        annotation.id`` — a BIGINT surrogate key, not the SDMX text-id
        ``annotation.annotation_id``), so each language's text is kept
        separate. This is what lets the search corpus carry a code's
        own-language context (e.g. its nearest topical-universe label) on
        each per-language row, instead of the altLabel path's single
        non-localised ``annotation.title`` smeared across both languages.

        ``annotation_types`` is empty-safe at the call site (the caller
        skips this query when no types are requested).
        """
        where_parts = ["a.annotation_type IN (SELECT UNNEST($types))"]
        params: dict[str, object] = {"types": list(annotation_types)}
        if codelist_urns is not None:
            where_parts.append("c.codelist_urn IN (SELECT UNNEST($codelist_urns))")
            params["codelist_urns"] = list(codelist_urns)
        sql = f"""
            SELECT c.code_urn, axt.lang, list(axt.text)
            FROM sdmx.code c
            JOIN sdmx.annotation a       ON a.owner_urn = c.code_urn
            JOIN sdmx.annotation_text axt ON axt.annotation_id = a.id
            WHERE {" AND ".join(where_parts)}
            GROUP BY c.code_urn, axt.lang
        """
        rows = self._c.execute(sql, params).fetchall()
        out: dict[str, dict[str, list[str]]] = {}
        for code_urn, lang, texts in rows:
            cleaned = [str(t) for t in (texts or []) if t]
            if cleaned:
                out.setdefault(str(code_urn), {})[str(lang)] = cleaned
        return out

    def _load_code_reference_metadata(
        self,
        *,
        blends: Sequence[ReferenceMetadataBlend],
        codelist_urns: Sequence[str] | None,
    ) -> dict[str, dict[str, list[str]]]:
        """Return ``{code_urn: {lang_or_ANY: [blended_text, …]}}`` across all blends.

        One SQL per blend entry, results coalesced. Each entry's text is
        truncated to its ``max_chars`` after concatenating multiple
        values with a single space. Whitespace is collapsed before
        truncation so word boundaries don't matter — the embedding /
        Tantivy are robust to mid-word cuts. ``max_chars`` applies per
        (code, language), so every language's row gets the same budget.

        Values are read from both shapes ``sdmx.metadata_value`` can hold,
        mirroring `_load_code_alt_labels`:

        - the ``i18n`` MAP, unnested to one row per language. These fold
          into their own language's row alone, so a French query matches
          French footnote text and the French corpus carries no English
          tokens.
        - the ``scalar`` column, which carries no language — URLs,
          vintages, codes. These land under `_ANY_LANG` and fold
          into every language's row.

        A value contributes to exactly one of the two: when it has an
        ``i18n`` map its ``scalar`` is ignored rather than duplicated
        alongside it. Monolingual catalogs and scalar-valued attributes
        index exactly as before.

        Raises:
            RuntimeError: When at least one blend is requested but the
                ``sdmx.metadata_target`` / ``sdmx.metadata_value`` tables
                are not present. Silent skip would mask config errors;
                callers asking for reference-metadata blending need to
                know if no rows can possibly match.

        Returns:
            ``{code_urn: {lang_or_ANY: [blended_text, …]}}`` — the blended
            reference-metadata text per code, keyed by language.

        Args:
            blends: Blend specs to run, one SQL each; results are coalesced.
            codelist_urns: Restrict the scan to these codelists. ``None``
                (default) covers every codelist referenced by an indexed
                dataflow.
        """
        if not blends:
            return {}
        # Guard: metadata tables present?
        present = self._c.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema = 'sdmx'
              AND table_name IN ('metadata_target', 'metadata_value')
            """
        ).fetchone()
        if not present or int(present[0]) < 2:
            msg = (
                "reference_metadata_blends requested but sdmx.metadata_target / "
                "sdmx.metadata_value tables are not present in this catalog"
            )
            raise RuntimeError(msg)

        result: dict[str, dict[str, list[str]]] = {}
        for blend in blends:
            where_parts = ["v.attribute_path = $attribute_path"]
            params: dict[str, object] = {
                "attribute_path": blend.attribute_path,
                "any_lang": _ANY_LANG,
            }
            if codelist_urns is not None:
                where_parts.append("c.codelist_urn IN (SELECT UNNEST($codelist_urns))")
                params["codelist_urns"] = list(codelist_urns)
            # Unnesting the i18n MAP puts each language's text under its
            # own tag; the LEFT JOIN is what keeps scalar-only values (an
            # empty or NULL map unnests to no rows) in the result set at
            # all, under the any-language bucket. Text is likewise
            # preferred over scalar, so a localised value never
            # contributes its scalar as well. Ordering by sibling_index
            # keeps repeated ReportedAttributes in MSD order.
            sql = f"""
                SELECT c.code_urn,
                       COALESCE(u.lang, $any_lang) AS blend_lang,
                       list(COALESCE(u.text, v.scalar) ORDER BY v.sibling_index)
                FROM sdmx.code c
                JOIN sdmx.metadata_target t ON t.target_urn = c.code_urn
                JOIN sdmx.metadata_value v  ON v.set_urn = t.set_urn
                LEFT JOIN LATERAL (
                    SELECT UNNEST(MAP_KEYS(v.i18n)) AS lang,
                           UNNEST(MAP_VALUES(v.i18n)) AS text
                ) u ON TRUE
                WHERE {" AND ".join(where_parts)}
                GROUP BY c.code_urn, blend_lang
            """
            rows = self._c.execute(sql, params).fetchall()
            for code_urn, blend_lang, texts in rows:
                cleaned_parts = [str(t) for t in (texts or []) if t]
                if not cleaned_parts:
                    continue
                joined = " ".join(" ".join(p.split()) for p in cleaned_parts)
                head = joined[: blend.max_chars]
                if head:
                    result.setdefault(str(code_urn), {}).setdefault(str(blend_lang), []).append(head)
        return result

    def _warn_about_prefixes(self, model_name: str | None) -> None:
        """Warn where a prefix mismatch would degrade recall without erroring.

        Both cases below produce vectors whose document side and query side
        sit in different regions of the embedding space. Nothing raises and
        recall is merely worse; the visible symptom is BM25 and vector top-K
        results that barely overlap.

        Warned rather than corrected. Applying a prefix silently would change
        what is embedded, and a rebuild is the caller's decision — the same
        reason the out-of-date-index warning in `lance.py` warns instead of
        rebuilding.

        Args:
            model_name: The encoder id passed to `build_index`, if any.
        """
        if self._embedding_model is None:
            return
        expected = ASYMMETRIC_MODEL_PREFIXES.get(model_name or "")
        if expected is not None and not self._document_prefix:
            doc, qry = expected
            warnings.warn(
                f"{model_name} is asymmetric: documents must be embedded with "
                f"{doc!r} and queries with {qry!r}, but document_prefix is empty. "
                "The two sides will not share an embedding space and vector "
                "recall will be quietly worse. Pass document_prefix / "
                "query_prefix to build_index() or to Catalog(). (statcatlib #81)",
                stacklevel=3,
            )
        if model_name is None and (self._document_prefix or self._query_prefix):
            warnings.warn(
                "document_prefix / query_prefix are set but model_name is not, "
                "so no embeddings.json is written and the next open() restores "
                "neither. Queries would then run unprefixed against prefixed "
                "vectors. Pass model_name to build_index() to pin them. "
                "(statcatlib #81)",
                stacklevel=3,
            )

    def build_index(
        self,
        embedding_model: EmbeddingModel | None = None,
        progress: Callable[[int, int], None] | None = None,
        *,
        include_codes: bool = True,
        embed_codes: bool = False,
        max_codes_per_codelist: int = 500,
        codelist_urns: Sequence[str] | None = None,
        codelist_policies: Sequence[CodelistIndexPolicy] = (),
        include_alt_labels: bool = True,
        carry_hierarchy_fields: bool = True,
        reference_metadata_blends: Sequence[ReferenceMetadataBlend] = (),
        context_annotation_types: Sequence[str] = (SEARCH_CONTEXT_ANNOTATION_TYPE,),
        exclude_annotation_types: Sequence[str] = (SEARCH_EXCLUDE_ANNOTATION_TYPE,),
        include_concepts: bool = True,
        embed_concepts: bool = True,
        concept_scheme_urns: Sequence[str] | None = None,
        include_reference_metadata: bool = False,
        embed_reference_metadata: bool = True,
        reference_metadataflow_urns: Sequence[str] | None = None,
        document_prefix: str | None = None,
        query_prefix: str | None = None,
        model_name: str | None = None,
    ) -> int:
        """Re-index all datasets for search and optionally build ANN vectors.

        If *embedding_model* is provided it is stored for future vector
        operations.  Safe to call multiple times.

        Uses batch encoding (256 texts per call) and bulk LanceDB writes
        for efficient full rebuilds.

        Args:
            embedding_model: Optional sentence-transformer-style model.
            progress: Optional ``(current, total)`` callback invoked after
                text collection and after each encoding batch. When provided,
                the model's internal progress bar is suppressed.
            include_codes: Whether to index individual SDMX codes alongside
                dataset-level text. Defaults to True. Code rows are still
                BM25-searchable via ``search_codes()`` even when
                ``embed_codes=False``.
            embed_codes: Whether to compute embeddings for code rows. Defaults
                to False — embedding every code in a large catalog
                with an e5-family model can take hours, but BM25 over
                short controlled-vocabulary code labels is usually
                sufficient. Set to True if you need vector search over
                individual codes.
            max_codes_per_codelist: Skip codelists with more entries than this
                to avoid bloating the index. Defaults to 500.
            codelist_urns: Restrict code indexing to these codelists only.
                ``None`` (default) embeds every codelist referenced by an
                indexed dataflow.
            codelist_policies: Per-codelist overrides. Codelists listed
                with ``mode="fts_only"`` bypass ``max_codes_per_codelist``
                and are written to Lance with ``embedding=None`` regardless
                of the global ``embed_codes`` flag — the right shape for
                large proper-noun codelists (geography, characteristics)
                where Tantivy FTS outperforms vector search on short
                labels. See `CodelistIndexPolicy`.
            include_alt_labels: Blend SKOS ``altLabel`` annotations into
                each code's search text. Defaults to True.
            carry_hierarchy_fields: Populate ``parent_id`` / ``depth``
                on each code row from the SDMX hierarchical-codelist
                model. Defaults to True.
            reference_metadata_blends: SDMX MetadataSet attributes to
                blend into each code's text — e.g. footnote bodies or
                concept definitions. Default empty; opt-in per catalog.
                Raises ``RuntimeError`` at build time when requested but
                the catalog has no reference-metadata tables.
            context_annotation_types: Annotation types whose localised
                text is folded into each code's per-language search text.
                Defaults to ``(SEARCH_CONTEXT_ANNOTATION_TYPE,)`` —
                statcatlib's ``"statcat:searchContext"`` convention, on by
                default like ``include_alt_labels``. Pass ``()`` to
                disable. See `_collect_code_texts`.
            exclude_annotation_types: Codes carrying an annotation of one of
                these types are left out of the code corpus. Defaults to
                ``(SEARCH_EXCLUDE_ANNOTATION_TYPE,)`` — statcatlib's
                ``"statcat:searchExclude"`` convention. Pass ``()`` to index
                every code. See `_collect_code_texts`.
            include_concepts: Whether to index ``sdmx.concept`` rows for
                search. Defaults to True. Concepts carry definitional
                descriptions that are excellent semantic-search targets.
            embed_concepts: Whether to compute vector embeddings for
                concept rows. Defaults to True — concept descriptions
                are paragraph-shaped text where embeddings earn their
                cost (unlike short code labels).
            concept_scheme_urns: Restrict concept indexing to these
                ConceptScheme URNs. ``None`` (default) indexes every
                ingested concept.
            include_reference_metadata: Whether to index SDMX
                reference-metadata sections (reference guides, methodology
                reports) as their own searchable ``reference_metadata``
                rows — chunked one unit per section. Defaults to False;
                opt-in per catalog. Complements the reference-metadata
                *blend*: the blend enriches a code's recall, this makes the
                document itself retrievable and navigable back to its
                targets. See `_collect_reference_metadata_texts`.
            embed_reference_metadata: Compute vector embeddings for
                reference-metadata rows. Defaults to True — section prose is
                paragraph-shaped text where embeddings earn their cost.
            reference_metadataflow_urns: Restrict reference-metadata
                indexing to sets whose structure is one of these
                MSD/Metadataflow URNs. ``None`` (default) indexes every set.
            document_prefix: Override the store's document prefix for this
                build. For e5-family models pass ``"passage: "``. When
                ``None`` (default), the value from ``__init__`` is used.
            query_prefix: Override the store's query prefix going forward.
                For e5-family models pass ``"query: "``. When ``None``
                (default), the value from ``__init__`` is preserved.
            model_name: Hugging Face / SentenceTransformers model id used to
                produce the embeddings (e.g. ``"intfloat/multilingual-e5-base"``).
                When provided, persisted to ``<catalog>/search/embeddings.json``
                along with the encoder's vector dimensionality, build
                timestamp, statcatlib version, and row counts — so a later
                `open` can recover `embedding_metadata` and
                `check_index_compatible` has something to compare
                against. Re-using the previously persisted model name is
                supported by omitting this argument and relying on the
                sidecar restored by `open`.

        Returns:
            Number of datasets indexed.
        """
        self._require_writable("build_index()")
        if embedding_model is not None:
            self._embedding_model = embedding_model
        if document_prefix is not None:
            self._document_prefix = document_prefix
        if query_prefix is not None:
            self._query_prefix = query_prefix
        self._warn_about_prefixes(model_name)
        if self._lance is None:
            self._init_lance()
        assert self._lance is not None  # noqa: S101

        fts_only_urns = _fts_only_urns(codelist_policies)
        dataset_items = self._collect_search_texts()
        code_rows: list[SearchRow] = (
            self._collect_code_texts(
                max_codes_per_codelist=max_codes_per_codelist,
                codelist_urns=codelist_urns,
                include_alt_labels=include_alt_labels,
                carry_hierarchy_fields=carry_hierarchy_fields,
                reference_metadata_blends=reference_metadata_blends,
                context_annotation_types=context_annotation_types,
                exclude_annotation_types=exclude_annotation_types,
                codelist_policies=codelist_policies,
            )
            if include_codes
            else []
        )
        concept_rows: list[SearchRow] = (
            self._collect_concept_texts(scheme_urns=concept_scheme_urns) if include_concepts else []
        )
        refmeta_rows: list[SearchRow] = (
            self._collect_reference_metadata_texts(metadataflow_urns=reference_metadataflow_urns)
            if include_reference_metadata
            else []
        )
        total = len(dataset_items) + len(code_rows) + len(concept_rows) + len(refmeta_rows)
        if progress is not None:
            progress(0, total)

        # Row order is not load-bearing — PyArrow scans the whole batch, so
        # leading with ``embedding=None`` rows still infers the right type.
        # What does matter is a batch where *every* row is unembedded while a
        # model is configured (embed_codes=False, or every codelist marked
        # fts_only): inference yields a null-typed column and the ANN index
        # never builds. ``embedding_dim`` below pins the type from the model
        # so that case produces a properly typed, all-NULL column instead.
        dataset_rows = self._build_dataset_lance_rows(dataset_items, progress=progress)
        self._attach_code_embeddings(
            code_rows,
            embed_codes=embed_codes,
            progress=progress,
            fts_only_urns=fts_only_urns,
        )
        self._attach_concept_embeddings(concept_rows, embed_concepts=embed_concepts, progress=progress)
        self._attach_reference_metadata_embeddings(
            refmeta_rows, embed_reference_metadata=embed_reference_metadata, progress=progress
        )

        embedding_dim = _infer_embedding_dim(self._embedding_model, dataset_rows)
        # The annotation types this build read, and a digest of them, so a
        # later change to them (a folded overlay) is detected, not missed.
        annotation_types: set[str] = set()
        if include_codes:
            annotation_types |= {*context_annotation_types, *exclude_annotation_types}
            if include_alt_labels:
                annotation_types.add(_ALT_LABEL_ANNOTATION_TYPE)
        types = sorted(annotation_types)
        self._lance.add_bulk(
            dataset_rows + code_rows + concept_rows + refmeta_rows,
            embedding_dim=embedding_dim,
            annotations={"types": types, "fingerprint": annotation_fingerprint(self._c, types)},
        )
        self._lance.build_index()
        if progress is not None:
            progress(total, total)

        docs_indexed = len(dataset_rows)
        code_rows_with_embeddings = sum(1 for r in code_rows if r.get("embedding") is not None)
        concept_rows_with_embeddings = sum(1 for r in concept_rows if r.get("embedding") is not None)
        self._persist_embeddings_meta(
            model_name=model_name,
            embedding_dim=embedding_dim,
            include_codes=include_codes,
            embed_codes=embed_codes,
            max_codes_per_codelist=max_codes_per_codelist,
            docs_indexed=docs_indexed,
            code_rows_indexed=len(code_rows) if include_codes else 0,
            code_rows_with_embeddings=code_rows_with_embeddings,
            codelist_policies=codelist_policies,
            include_concepts=include_concepts,
            embed_concepts=embed_concepts,
            concept_rows_indexed=len(concept_rows) if include_concepts else 0,
            concept_rows_with_embeddings=concept_rows_with_embeddings,
        )
        n_datasets = self._c.execute("SELECT COUNT(*) FROM dataset").fetchone()
        return int(n_datasets[0]) if n_datasets else 0

    def _build_dataset_lance_rows(
        self,
        dataset_items: list[tuple[str, str, str, str]],
        *,
        progress: Callable[[int, int], None] | None,
    ) -> list[SearchRow]:
        """Materialize dataset-level LanceDB rows, optionally embedded."""
        if self._embedding_model is not None:
            return self._batch_encode(dataset_items, progress=progress)
        return [
            {
                "id": item[0],
                "entity_type": item[1],
                "lang": item[2],
                "text": item[3],
                "dimension_id": "",
                "dimension_label": "",
                "code_id": "",
                "code_label": "",
                "code_urn": "",
                "codelist_urn": "",
                "parent_id": "",
                "depth": 0,
                "concept_urn": "",
                "scheme_urn": "",
                "concept_id": "",
                "set_urn": "",
                "attribute_path": "",
                "section_title": "",
            }
            for item in dataset_items
        ]

    def _attach_code_embeddings(
        self,
        code_rows: list[SearchRow],
        *,
        embed_codes: bool,
        progress: Callable[[int, int], None] | None,
        fts_only_urns: set[str] | None = None,
    ) -> None:
        """Populate the ``embedding`` column on *code_rows* in place.

        - No model configured → leave rows alone (no embedding column).
        - Model + ``embed_codes=True`` → encode each code row's text,
          except rows whose ``codelist_urn`` is in *fts_only_urns* —
          those are forced to ``embedding=None`` (FTS-only opt-in via
          `CodelistIndexPolicy`).
        - Model + ``embed_codes=False`` → set ``embedding=None`` so the
          row schema lines up with the dataset rows.
        """
        if not code_rows or self._embedding_model is None:
            return
        fts_only = fts_only_urns or set()
        if embed_codes:
            encode_idx = [i for i, r in enumerate(code_rows) if r.get("codelist_urn", "") not in fts_only]
            code_items = [
                (
                    code_rows[i]["id"],
                    code_rows[i]["entity_type"],
                    code_rows[i]["lang"],
                    code_rows[i]["text"],
                )
                for i in encode_idx
            ]
            encoded = self._batch_encode(code_items, progress=progress)
            encoded_set = set(encode_idx)
            for i, enc in zip(encode_idx, encoded, strict=True):
                code_rows[i]["embedding"] = enc.get("embedding")
            for i, row in enumerate(code_rows):
                if i not in encoded_set:
                    row["embedding"] = None
            return
        for row in code_rows:
            row["embedding"] = None

    def _persist_embeddings_meta(
        self,
        *,
        model_name: str | None,
        embedding_dim: int | None,
        include_codes: bool,
        embed_codes: bool,
        max_codes_per_codelist: int,
        docs_indexed: int,
        code_rows_indexed: int,
        code_rows_with_embeddings: int,
        codelist_policies: Sequence[CodelistIndexPolicy] = (),
        include_concepts: bool = False,
        embed_concepts: bool = False,
        concept_rows_indexed: int = 0,
        concept_rows_with_embeddings: int = 0,
    ) -> None:
        """Update `embedding_metadata` and write the sidecar.

        Becomes a partial no-op for in-memory catalogs (in-memory has no
        bundle dir to write to) but the in-memory ``EmbeddingMetadata``
        is still updated so query-side helpers work within the session.
        Without a *model_name* the sidecar is not written — there is
        nothing meaningful for a later `open` to recover.
        """
        # Resolve the carried-forward model name: explicit argument wins,
        # otherwise reuse whatever a previous build (or open()) recorded.
        existing = self._embedding_metadata
        effective_model = model_name if model_name is not None else (existing.model if existing else None)
        if effective_model is None:
            return

        fts_only = tuple(sorted(_fts_only_urns(codelist_policies)))
        meta = EmbeddingMetadata(
            model=effective_model,
            embedding_dim=embedding_dim,
            document_prefix=self._document_prefix,
            query_prefix=self._query_prefix,
            normalize=True,  # encoder-agnostic default; harmless for unnormalized models
            include_codes=include_codes,
            embed_codes=embed_codes,
            max_codes_per_codelist=max_codes_per_codelist,
            built_at=datetime.now(UTC),
            built_by=_statcatlib_version_tag(),
            docs_indexed=docs_indexed,
            code_rows_indexed=code_rows_with_embeddings if embed_codes else code_rows_indexed,
            fts_only_codelist_urns=fts_only,
            include_concepts=include_concepts,
            embed_concepts=embed_concepts,
            concept_rows_indexed=concept_rows_indexed,
            concept_rows_with_embeddings=concept_rows_with_embeddings,
        )
        self._embedding_metadata = meta

        if self._bundle_dir is None:
            return
        _write_embeddings_meta(
            self._bundle_dir / "search",
            model_name=meta.model,
            document_prefix=meta.document_prefix,
            query_prefix=meta.query_prefix,
            embedding_dim=meta.embedding_dim,
            normalize=meta.normalize,
            include_codes=meta.include_codes,
            embed_codes=meta.embed_codes,
            max_codes_per_codelist=meta.max_codes_per_codelist,
            built_at=meta.built_at,
            built_by=meta.built_by,
            docs_indexed=meta.docs_indexed,
            code_rows_indexed=meta.code_rows_indexed,
            fts_only_codelist_urns=meta.fts_only_codelist_urns,
            include_concepts=meta.include_concepts,
            embed_concepts=meta.embed_concepts,
            concept_rows_indexed=meta.concept_rows_indexed,
            concept_rows_with_embeddings=meta.concept_rows_with_embeddings,
        )

    def _batch_encode(
        self,
        items: list[tuple[str, str, str, str]],
        progress: Callable[[int, int], None] | None = None,
    ) -> list[SearchRow]:
        """Batch-encode texts and return LanceDB rows with embeddings.

        Batch size defaults to 256 and can be overridden via the
        ``STATCAT_EMBED_BATCH_SIZE`` env var — useful on hardware with
        more GPU/unified memory than a typical CI machine.
        """
        batch_size = _embed_batch_size()
        total = len(items)
        rows: list[SearchRow] = []
        for i in range(0, total, batch_size):
            batch = items[i : i + batch_size]
            assert self._embedding_model is not None  # noqa: S101
            prefix = self._document_prefix
            texts = [prefix + item[3] for item in batch] if prefix else [item[3] for item in batch]
            # No `show_progress_bar=False` when `progress` is set: see
            # `EmbeddingModel`. The encoder draws no bar under normal logging,
            # and demanding the keyword excluded encoders statcatlib supports.
            vecs = as_vectors(self._embedding_model.encode(texts))
            rows.extend(
                {
                    "id": item[0],
                    "entity_type": item[1],
                    "lang": item[2],
                    "text": item[3],
                    "embedding": vec,
                    "dimension_id": "",
                    "dimension_label": "",
                    "code_id": "",
                    "code_label": "",
                    "code_urn": "",
                    "codelist_urn": "",
                    "parent_id": "",
                    "depth": 0,
                    "concept_urn": "",
                    "scheme_urn": "",
                    "concept_id": "",
                    "set_urn": "",
                    "attribute_path": "",
                    "section_title": "",
                }
                for item, vec in zip(batch, vecs, strict=True)
            )
            if progress is not None:
                progress(min(i + batch_size, total), total)
        return rows

    def _upsert_dimension_records(self, dataset: BaseDataset) -> _DimensionIds:
        """Upsert agents, contacts, frequencies, and licences.

        Theme/subject/type concepts are NOT upserted here — they are citations
        from the dataset to externally-managed ConceptScheme artefacts.
        """
        ag_id: str | None = None
        if dataset.publisher is not None:
            ag_id = _agent_id(dataset.publisher)
            self._exec(
                "INSERT INTO agent (id, name, type, homepage)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " name = excluded.name, type = excluded.type, homepage = excluded.homepage",
                [ag_id, _istring_to_map(dataset.publisher.name), dataset.publisher.type, dataset.publisher.homepage],
            )

        creator_id: str | None = None
        if dataset.creator is not None:
            creator_id = _agent_id(dataset.creator)
            self._exec(
                "INSERT INTO agent (id, name, type, homepage)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " name = excluded.name, type = excluded.type, homepage = excluded.homepage",
                [creator_id, _istring_to_map(dataset.creator.name), dataset.creator.type, dataset.creator.homepage],
            )

        ct_id: str | None = None
        if dataset.contact_point is not None:
            ct_id = _contact_id(dataset.contact_point)
            self._exec(
                "INSERT INTO contact (id, name, email, phone, url)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " name = excluded.name, email = excluded.email,"
                " phone = excluded.phone, url = excluded.url",
                [
                    ct_id,
                    _istring_to_map(dataset.contact_point.name),
                    dataset.contact_point.email,
                    dataset.contact_point.phone,
                    dataset.contact_point.url,
                ],
            )

        freq_id: str | None = None
        if dataset.frequency is not None:
            freq_id = dataset.frequency.id
            self._exec(
                "INSERT INTO frequency (id, label) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET label = excluded.label",
                [freq_id, _istring_to_map(dataset.frequency.label)],
            )

        lic_id: str | None = None
        if dataset.licence is not None:
            lic_id = dataset.licence.id
            self._exec(
                "INSERT INTO licence (id, label, uri) VALUES (?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET label = excluded.label, uri = excluded.uri",
                [lic_id, _istring_to_map(dataset.licence.label), dataset.licence.uri],
            )

        return _DimensionIds(
            publisher=ag_id,
            creator=creator_id,
            contact=ct_id,
            frequency=freq_id,
            licence=lic_id,
        )

    def _upsert_fact_row(self, dataset: Dataset, dim: _DimensionIds) -> None:
        """Upsert the main dataset fact row."""
        # Variant-specific fields
        num_series: int | None = None
        sdmx_dataflow_urn: str | None = None
        measure_dim: str | None = None
        sample_size: int | None = None
        universe: str | None = None
        collection_method: str | None = None
        anonymization: str | None = None
        survey_id: str | None = None
        crs: str | None = None
        spatial_resolution: str | None = None
        scale: str | None = None
        doi: str | None = None
        isbn: str | None = None
        publication_type: str | None = None
        pages: int | None = None
        series_title: dict[str, str] | None = None

        if isinstance(dataset, AggregateDataset):
            num_series = dataset.num_series
            sdmx_dataflow_urn = dataset.sdmx_dataflow_urn
            measure_dim = dataset.measure_dim
        elif isinstance(dataset, MicrodataDataset):
            sample_size = dataset.sample_size
            universe = dataset.universe
            collection_method = dataset.collection_method
            anonymization = dataset.anonymization
            survey_id = dataset.survey_id
        elif isinstance(dataset, GeospatialDataset):
            crs = dataset.crs
            spatial_resolution = dataset.spatial_resolution
            scale = dataset.scale
        elif isinstance(dataset, PublicationDataset):
            doi = dataset.doi
            isbn = dataset.isbn
            publication_type = dataset.publication_type
            pages = dataset.pages
            series_title = _istring_to_map(dataset.series_title)

        temporal_start: datetime | None = None
        temporal_end: datetime | None = None
        if dataset.temporal_coverage is not None:
            temporal_start = dataset.temporal_coverage.start
            temporal_end = dataset.temporal_coverage.end

        # The column is MAP(VARCHAR, VARCHAR), so a non-string value cannot
        # come back as what went in. Refused rather than coerced: `str(v)`
        # turned `None` into the string `"None"` — present and truthy where
        # an absence was stored — and said nothing about it (#114).
        extras_map = _validated_extras(dataset.extras)
        language_list = dataset.language or None

        dataset_type_uri = dataset.dataset_type.uri if dataset.dataset_type is not None else None

        self._exec(
            f"""INSERT INTO dataset ({_DATASET_COLUMN_SQL})
            VALUES ({_DATASET_PLACEHOLDERS})
            ON CONFLICT (identifier) DO UPDATE SET
                {_DATASET_UPSERT_SET}
            """,
            # Keyword-built, so the parameter order is the row's order, and the
            # INSERT column list is derived from the same `_fields` (#110).
            tuple(
                DatasetRow(
                    identifier=dataset.identifier,
                    kind=dataset.kind,
                    # NOT NULL columns, and both model fields are required —
                    # `_istring_to_map` only returns None for a None argument.
                    title=_istring_to_map(dataset.title) or {},
                    description=_istring_to_map(dataset.description) or {},
                    version_notes=_istring_to_map(dataset.version_notes),
                    publisher_id=dim.publisher,
                    creator_id=dim.creator,
                    contact_id=dim.contact,
                    frequency_id=dim.frequency,
                    licence_id=dim.licence,
                    temporal_start=temporal_start,
                    temporal_end=temporal_end,
                    status=dataset.status,
                    access_rights=dataset.access_rights,
                    issued=dataset.issued,
                    modified=dataset.modified,
                    landing_page=dataset.landing_page,
                    version=dataset.version,
                    language=language_list,
                    num_series=num_series,
                    sdmx_dataflow_urn=sdmx_dataflow_urn,
                    measure_dim=measure_dim,
                    sample_size=sample_size,
                    universe=universe,
                    collection_method=collection_method,
                    anonymization=anonymization,
                    survey_id=survey_id,
                    crs=crs,
                    spatial_resolution=spatial_resolution,
                    scale=scale,
                    doi=doi,
                    isbn=isbn,
                    publication_type=publication_type,
                    pages=pages,
                    series_title=series_title,
                    dataset_type=dataset_type_uri,
                    extras=extras_map,
                )
            ),
        )

    def _write_bridge_rows(self, dataset: BaseDataset) -> None:  # noqa: C901, PLR0912
        """Write bridge rows for themes, keywords, spatial, distributions, etc."""
        ds_id = dataset.identifier
        lit = _sql_literal

        # Theme citations (concept URIs only — concepts owned by ConceptScheme)
        self._exec("DELETE FROM dataset_theme WHERE dataset_id = ?", [ds_id])
        if dataset.themes:
            vals = ", ".join(f"({lit(ds_id)}, {lit(t.uri)})" for t in dataset.themes)
            self._c.execute(f"INSERT INTO dataset_theme (dataset_id, concept_uri) VALUES {vals}")

        # Subject citations
        self._exec("DELETE FROM dataset_subject WHERE dataset_id = ?", [ds_id])
        if dataset.subject:
            vals = ", ".join(f"({lit(ds_id)}, {lit(s.uri)})" for s in dataset.subject)
            self._c.execute(f"INSERT INTO dataset_subject (dataset_id, concept_uri) VALUES {vals}")

        # Keywords (MAP column, with ordinal) — batch VALUES insert
        self._exec("DELETE FROM dataset_keyword WHERE dataset_id = ?", [ds_id])
        if dataset.keywords:
            vals = ", ".join(
                f"({lit(ds_id)}, {lit(_istring_to_map(kw))}, {lit(i)})" for i, kw in enumerate(dataset.keywords)
            )
            self._c.execute(f"INSERT INTO dataset_keyword (dataset_id, keyword, ordinal) VALUES {vals}")

        # Spatial coverage (MAP label, with ordinal) — batch VALUES insert
        self._exec("DELETE FROM dataset_spatial WHERE dataset_id = ?", [ds_id])
        if dataset.spatial_coverage:
            vals = ", ".join(
                f"({lit(ds_id)}, {lit(_istring_to_map(cov.label))}, {lit(cov.uri)},"
                f" {lit(cov.bbox[0] if cov.bbox else None)}, {lit(cov.bbox[1] if cov.bbox else None)},"
                f" {lit(cov.bbox[2] if cov.bbox else None)}, {lit(cov.bbox[3] if cov.bbox else None)}, {lit(i)})"
                for i, cov in enumerate(dataset.spatial_coverage)
            )
            self._c.execute(
                f"INSERT INTO dataset_spatial"
                f" (dataset_id, label, uri, bbox_west, bbox_south, bbox_east, bbox_north, ordinal)"
                f" VALUES {vals}"
            )

        # Standards
        self._exec("DELETE FROM dataset_standard WHERE dataset_id = ?", [ds_id])
        for std in dataset.conforms_to:
            self._exec(
                "INSERT INTO standard (id, label, uri) VALUES (?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET label = excluded.label, uri = excluded.uri",
                [std.id, _istring_to_map(std.label), std.uri],
            )
            self._exec(
                "INSERT INTO dataset_standard (dataset_id, standard_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                [ds_id, std.id],
            )

        # Distributions
        self._exec("DELETE FROM distribution WHERE dataset_id = ?", [ds_id])
        for dist in dataset.distributions:
            lic_id: str | None = None
            if dist.licence is not None:
                lic_id = dist.licence.id
            svc_id: str | None = None
            if dist.access_service is not None:
                svc = dist.access_service
                svc_id = svc.id
                self._exec(
                    "INSERT INTO data_service (id, title, description, endpoint_url)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (id) DO UPDATE SET"
                    " title = excluded.title, description = excluded.description,"
                    " endpoint_url = excluded.endpoint_url",
                    [svc.id, _istring_to_map(svc.title), _istring_to_map(svc.description), svc.endpoint_url],
                )
                # service_dataset bridge
                self._exec(
                    "INSERT INTO service_dataset (service_id, dataset_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [svc.id, ds_id],
                )
            self._exec(
                """INSERT INTO distribution
                    (id, dataset_id, title, description, access_url, download_url,
                     media_type, format, byte_size, issued, modified, licence_id,
                     service_id, distribution_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    dataset_id = excluded.dataset_id, title = excluded.title,
                    description = excluded.description,
                    access_url = excluded.access_url, download_url = excluded.download_url,
                    media_type = excluded.media_type, format = excluded.format,
                    byte_size = excluded.byte_size, issued = excluded.issued,
                    modified = excluded.modified, licence_id = excluded.licence_id,
                    service_id = excluded.service_id,
                    distribution_type = excluded.distribution_type""",
                [
                    dist.id,
                    ds_id,
                    _istring_to_map(dist.title),
                    _istring_to_map(dist.description),
                    dist.access_url,
                    dist.download_url,
                    dist.media_type,
                    dist.format,
                    dist.byte_size,
                    dist.issued,
                    dist.modified,
                    lic_id,
                    svc_id,
                    dist.distribution_type,
                ],
            )

        # Quality annotations
        self._exec("DELETE FROM quality_annotation WHERE dataset_id = ?", [ds_id])
        for i, qa in enumerate(dataset.quality_annotations):
            self._exec(
                "INSERT INTO quality_annotation (dataset_id, ordinal, dimension, label, body) VALUES (?, ?, ?, ?, ?)",
                [ds_id, i, qa.dimension, _istring_to_map(qa.label), _istring_to_map(qa.body)],
            )

        # Provenance
        self._exec("DELETE FROM provenance WHERE dataset_id = ?", [ds_id])
        for i, prov in enumerate(dataset.provenance):
            self._exec(
                "INSERT INTO provenance (dataset_id, ordinal, label, uri) VALUES (?, ?, ?, ?)",
                [ds_id, i, _istring_to_map(prov.label), prov.uri],
            )

        # Dimension names (AggregateDataset)
        self._exec("DELETE FROM dimension_name WHERE dataset_id = ?", [ds_id])
        if isinstance(dataset, AggregateDataset) and dataset.dimension_names:
            for i, dim_label in enumerate(dataset.dimension_names):
                self._exec(
                    "INSERT INTO dimension_name (dataset_id, ordinal, label) VALUES (?, ?, ?)",
                    [ds_id, i, _istring_to_map(dim_label)],
                )

        # Variables (MicrodataDataset)
        self._exec("DELETE FROM variable WHERE dataset_id = ?", [ds_id])
        if isinstance(dataset, MicrodataDataset):
            for var in dataset.variables:
                self._exec(
                    """INSERT INTO variable
                        (dataset_id, variable_id, label, description, value_type,
                         classification_id, universe, question_text, valid_range, derivation)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        ds_id,
                        var.id,
                        _istring_to_map(var.label),
                        _istring_to_map(var.description),
                        var.value_type,
                        var.classification_id,
                        var.universe,
                        var.question_text,
                        var.valid_range,
                        var.derivation,
                    ],
                )

        # Feature types (GeospatialDataset)
        self._exec("DELETE FROM feature_type WHERE dataset_id = ?", [ds_id])
        if isinstance(dataset, GeospatialDataset):
            for ft in dataset.feature_types:
                self._exec(
                    "INSERT INTO feature_type (dataset_id, type_name) VALUES (?, ?)",
                    [ds_id, ft],
                )

        # Authors (PublicationDataset)
        self._exec("DELETE FROM author WHERE dataset_id = ?", [ds_id])
        if isinstance(dataset, PublicationDataset):
            for i, name in enumerate(dataset.authors):
                self._exec(
                    "INSERT INTO author (dataset_id, ordinal, name) VALUES (?, ?, ?)",
                    [ds_id, i, name],
                )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get[T: BaseDataset](self, cls: type[T], identifier: str) -> T | None:
        """Retrieve a single dataset by identifier, narrowed to cls.

        Returns None if the identifier does not exist or the stored type
        is not compatible with cls.
        """
        raw = self._c.execute(
            f"SELECT {self._DATASET_COLUMNS} FROM dataset WHERE identifier = ?",
            [identifier],
        ).fetchone()
        if raw is None:
            return None
        ds = self._reconstruct_dataset(DatasetRow(*raw))
        if not isinstance(ds, cls):
            return None
        return ds

    def _reconstruct_dataset(self, row: DatasetRow) -> BaseDataset:
        """Reconstruct a BaseDataset subclass from a dataset table row."""
        # Read straight off the row — named, so a moved column cannot
        # silently land in the wrong parameter.

        title = _map_to_istring(row.title) or InternationalString()
        description = _map_to_istring(row.description) or InternationalString()
        version_notes = _map_to_istring(row.version_notes)
        extras = dict(row.extras) if row.extras else {}

        # Read related entities
        publisher = self._read_agent(row.publisher_id) if row.publisher_id else None
        creator = self._read_agent(row.creator_id) if row.creator_id else None
        contact_point = self._read_contact(row.contact_id) if row.contact_id else None
        frequency = self._read_frequency(row.frequency_id) if row.frequency_id else None
        licence = self._read_licence(row.licence_id) if row.licence_id else None
        themes = self._read_themes(row.identifier)
        subject = self._read_subjects(row.identifier)
        dataset_type = self._read_concept_ref(row.dataset_type) if row.dataset_type else None
        keywords = self._read_keywords(row.identifier)
        spatial_coverage = self._read_spatial(row.identifier)
        distributions = self._read_distributions(row.identifier)
        conforms_to = self._read_standards(row.identifier)
        quality_annotations = self._read_quality_annotations(row.identifier)
        provenance = self._read_provenance(row.identifier)

        temporal_coverage: TemporalCoverage | None = None
        if row.temporal_start is not None or row.temporal_end is not None:
            temporal_coverage = TemporalCoverage(start=row.temporal_start, end=row.temporal_end)

        lang_list: list[str] = list(row.language) if row.language else []

        # Base kwargs common to all variants
        # As `_assemble_dataset` above: a row.kind-dispatched splat, with the
        # `Any` confined to the local doing it.
        base_kwargs: dict[str, Any] = {
            "identifier": row.identifier,
            "title": title,
            "description": description,
            "publisher": publisher,
            "creator": creator,
            "contact_point": contact_point,
            "keywords": keywords,
            "themes": themes,
            "subject": subject,
            "dataset_type": dataset_type,
            "issued": row.issued,
            "modified": row.modified,
            "frequency": frequency,
            "spatial_coverage": spatial_coverage,
            "temporal_coverage": temporal_coverage,
            "distributions": distributions,
            "licence": licence,
            "language": lang_list,
            "landing_page": row.landing_page,
            "version": row.version,
            "version_notes": version_notes,
            "conforms_to": conforms_to,
            "access_rights": row.access_rights,
            "status": row.status,
            "provenance": provenance,
            "quality_annotations": quality_annotations,
            "extras": extras,
        }

        if row.kind == "aggregate":
            dimension_names = self._read_dimension_names(row.identifier)
            return AggregateDataset(
                **base_kwargs,
                dimension_names=dimension_names,
                num_series=row.num_series,
                sdmx_dataflow_urn=row.sdmx_dataflow_urn,
                measure_dim=row.measure_dim,
            )
        if row.kind == "microdata":
            variables = self._read_variables(row.identifier)
            return MicrodataDataset(
                **base_kwargs,
                variables=variables,
                sample_size=row.sample_size,
                universe=row.universe,
                collection_method=row.collection_method,
                anonymization=row.anonymization,
                survey_id=row.survey_id,
            )
        if row.kind == "geospatial":
            feature_types = self._read_feature_types(row.identifier)
            return GeospatialDataset(
                **base_kwargs,
                crs=row.crs,
                spatial_resolution=row.spatial_resolution,
                feature_types=feature_types,
                scale=row.scale,
            )
        if row.kind == "publication":
            authors = self._read_authors(row.identifier)
            series_title = _map_to_istring(row.series_title)
            return PublicationDataset(
                **base_kwargs,
                authors=authors,
                doi=row.doi,
                isbn=row.isbn,
                publication_type=row.publication_type,
                pages=row.pages,
                series_title=series_title,
            )
        # Default: OpenDataset (also handles legacy "Dataset" row.kind)
        return OpenDataset(**base_kwargs)

    # -- Entity readers --

    def _read_agent(self, agent_id: str) -> Agent | None:
        row = self._c.execute("SELECT id, name, type, homepage FROM agent WHERE id = ?", [agent_id]).fetchone()
        if row is None:
            return None
        return Agent(id=row[0], name=_map_to_istring(row[1]) or InternationalString(), type=row[2], homepage=row[3])

    def _read_contact(self, contact_id: str) -> ContactPoint | None:
        row = self._c.execute("SELECT id, name, email, phone, url FROM contact WHERE id = ?", [contact_id]).fetchone()
        if row is None:
            return None
        return ContactPoint(name=_map_to_istring(row[1]), email=row[2], phone=row[3], url=row[4])

    def _read_frequency(self, freq_id: str) -> Frequency | None:
        row = self._c.execute("SELECT id, label FROM frequency WHERE id = ?", [freq_id]).fetchone()
        if row is None:
            return None
        return Frequency(id=row[0], label=_map_to_istring(row[1]) or InternationalString())

    def _read_licence(self, lic_id: str) -> LicenceDocument | None:
        row = self._c.execute("SELECT id, label, uri FROM licence WHERE id = ?", [lic_id]).fetchone()
        if row is None:
            return None
        return LicenceDocument(id=row[0], label=_map_to_istring(row[1]) or InternationalString(), uri=row[2])

    def _read_concept_ref(self, uri: str) -> ConceptRef:
        """Build a ConceptRef for a single URI, hydrating label/notation from store if available."""
        row = self._c.execute("SELECT notation, label, in_scheme FROM concept WHERE uri = ?", [uri]).fetchone()
        if row is None:
            return ConceptRef(uri=uri)
        return ConceptRef(
            uri=uri,
            scheme_uri=row[2],
            notation=row[0],
            label=_map_to_istring(row[1]) or InternationalString(),
        )

    def _read_themes(self, dataset_id: str) -> list[ConceptRef]:
        rows = self._c.execute(
            """SELECT dt.concept_uri, c.notation, c.label, c.in_scheme
            FROM dataset_theme dt
            LEFT JOIN concept c ON c.uri = dt.concept_uri
            WHERE dt.dataset_id = ?""",
            [dataset_id],
        ).fetchall()
        return [
            ConceptRef(
                uri=r[0],
                notation=r[1],
                label=_map_to_istring(r[2]) or InternationalString(),
                scheme_uri=r[3],
            )
            for r in rows
        ]

    def _read_subjects(self, dataset_id: str) -> list[ConceptRef]:
        rows = self._c.execute(
            """SELECT ds.concept_uri, c.notation, c.label, c.in_scheme
            FROM dataset_subject ds
            LEFT JOIN concept c ON c.uri = ds.concept_uri
            WHERE ds.dataset_id = ?""",
            [dataset_id],
        ).fetchall()
        return [
            ConceptRef(
                uri=r[0],
                notation=r[1],
                label=_map_to_istring(r[2]) or InternationalString(),
                scheme_uri=r[3],
            )
            for r in rows
        ]

    def _read_keywords(self, dataset_id: str) -> list[InternationalString]:
        rows = self._c.execute(
            "SELECT keyword FROM dataset_keyword WHERE dataset_id = ? ORDER BY ordinal",
            [dataset_id],
        ).fetchall()
        return [_map_to_istring(r[0]) or InternationalString() for r in rows]

    def _read_spatial(self, dataset_id: str) -> list[SpatialCoverage]:
        rows = self._c.execute(
            """SELECT label, uri, bbox_west, bbox_south, bbox_east, bbox_north
            FROM dataset_spatial WHERE dataset_id = ? ORDER BY ordinal""",
            [dataset_id],
        ).fetchall()
        result = []
        for r in rows:
            label = _map_to_istring(r[0])
            bbox: tuple[float, float, float, float] | None = None
            if r[2] is not None:
                bbox = (r[2], r[3], r[4], r[5])
            result.append(SpatialCoverage(label=label, uri=r[1], bbox=bbox))
        return result

    def _read_distributions(self, dataset_id: str) -> list[Distribution]:
        rows = self._c.execute(
            """SELECT id, title, description, access_url, download_url,
                media_type, format, byte_size, issued, modified,
                licence_id, service_id, distribution_type
            FROM distribution WHERE dataset_id = ?""",
            [dataset_id],
        ).fetchall()
        result = []
        for r in rows:
            lic: LicenceDocument | None = None
            if r[10] is not None:
                lic = self._read_licence(r[10])
            svc: DataService | None = None
            if r[11] is not None:
                svc = self._read_data_service(r[11])
            result.append(
                Distribution(
                    id=r[0],
                    title=_map_to_istring(r[1]),
                    description=_map_to_istring(r[2]),
                    access_url=r[3],
                    download_url=r[4],
                    media_type=r[5],
                    format=r[6],
                    byte_size=r[7],
                    issued=r[8],
                    modified=r[9],
                    licence=lic,
                    access_service=svc,
                    distribution_type=r[12],
                )
            )
        return result

    def _read_data_service(self, service_id: str) -> DataService | None:
        row = self._c.execute(
            "SELECT id, title, description, endpoint_url FROM data_service WHERE id = ?",
            [service_id],
        ).fetchone()
        if row is None:
            return None
        serves_rows = self._c.execute(
            "SELECT dataset_id FROM service_dataset WHERE service_id = ?", [service_id]
        ).fetchall()
        return DataService(
            id=row[0],
            title=_map_to_istring(row[1]) or InternationalString(),
            description=_map_to_istring(row[2]),
            endpoint_url=row[3] or "",
            serves_datasets=[r[0] for r in serves_rows],
        )

    def _read_standards(self, dataset_id: str) -> list[Standard]:
        rows = self._c.execute(
            """SELECT s.id, s.label, s.uri
            FROM dataset_standard ds
            JOIN standard s ON ds.standard_id = s.id
            WHERE ds.dataset_id = ?""",
            [dataset_id],
        ).fetchall()
        return [Standard(id=r[0], label=_map_to_istring(r[1]) or InternationalString(), uri=r[2]) for r in rows]

    def _read_quality_annotations(self, dataset_id: str) -> list[QualityAnnotation]:
        rows = self._c.execute(
            "SELECT dimension, label, body FROM quality_annotation WHERE dataset_id = ? ORDER BY ordinal",
            [dataset_id],
        ).fetchall()
        return [
            QualityAnnotation(dimension=r[0], label=_map_to_istring(r[1]), body=_map_to_istring(r[2])) for r in rows
        ]

    def _read_provenance(self, dataset_id: str) -> list[ProvenanceStatement]:
        rows = self._c.execute(
            "SELECT label, uri FROM provenance WHERE dataset_id = ? ORDER BY ordinal",
            [dataset_id],
        ).fetchall()
        return [ProvenanceStatement(label=_map_to_istring(r[0]) or InternationalString(), uri=r[1]) for r in rows]

    def _read_dimension_names(self, dataset_id: str) -> list[InternationalString]:
        rows = self._c.execute(
            "SELECT label FROM dimension_name WHERE dataset_id = ? ORDER BY ordinal",
            [dataset_id],
        ).fetchall()
        return [_map_to_istring(r[0]) or InternationalString() for r in rows]

    def _read_variables(self, dataset_id: str) -> list[Variable]:
        rows = self._c.execute(
            """SELECT variable_id, label, description, value_type,
                classification_id, universe, question_text, valid_range, derivation
            FROM variable WHERE dataset_id = ?""",
            [dataset_id],
        ).fetchall()
        return [
            Variable(
                id=r[0],
                label=_map_to_istring(r[1]) or InternationalString(),
                description=_map_to_istring(r[2]),
                value_type=r[3],
                classification_id=r[4],
                universe=r[5],
                question_text=r[6],
                valid_range=r[7],
                derivation=r[8],
            )
            for r in rows
        ]

    def _read_feature_types(self, dataset_id: str) -> list[str]:
        rows = self._c.execute("SELECT type_name FROM feature_type WHERE dataset_id = ?", [dataset_id]).fetchall()
        return [r[0] for r in rows]

    def _read_authors(self, dataset_id: str) -> list[str]:
        rows = self._c.execute("SELECT name FROM author WHERE dataset_id = ? ORDER BY ordinal", [dataset_id]).fetchall()
        return [r[0] for r in rows]

    # -- Batch entity readers (selectin-style eager loading) --

    def _batch_read_agents(self, ids: set[str]) -> dict[str, Agent]:
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        rows = self._c.execute(
            f"SELECT id, name, type, homepage FROM agent WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return {
            r[0]: Agent(id=r[0], name=_map_to_istring(r[1]) or InternationalString(), type=r[2], homepage=r[3])
            for r in rows
        }

    def _batch_read_contacts(self, ids: set[str]) -> dict[str, ContactPoint]:
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        rows = self._c.execute(
            f"SELECT id, name, email, phone, url FROM contact WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return {r[0]: ContactPoint(name=_map_to_istring(r[1]), email=r[2], phone=r[3], url=r[4]) for r in rows}

    def _batch_read_frequencies(self, ids: set[str]) -> dict[str, Frequency]:
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        rows = self._c.execute(
            f"SELECT id, label FROM frequency WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return {r[0]: Frequency(id=r[0], label=_map_to_istring(r[1]) or InternationalString()) for r in rows}

    def _batch_read_licences(self, ids: set[str]) -> dict[str, LicenceDocument]:
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        rows = self._c.execute(
            f"SELECT id, label, uri FROM licence WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return {
            r[0]: LicenceDocument(id=r[0], label=_map_to_istring(r[1]) or InternationalString(), uri=r[2]) for r in rows
        }

    def _batch_read_themes(self, dataset_ids: list[str]) -> dict[str, list[ConceptRef]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dt.dataset_id, dt.concept_uri, c.notation, c.label, c.in_scheme
            FROM dataset_theme dt
            LEFT JOIN concept c ON c.uri = dt.concept_uri
            WHERE dt.dataset_id IN ({placeholders})""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[ConceptRef]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                ConceptRef(
                    uri=r[1],
                    notation=r[2],
                    label=_map_to_istring(r[3]) or InternationalString(),
                    scheme_uri=r[4],
                )
            )
        return result

    def _batch_read_subjects(self, dataset_ids: list[str]) -> dict[str, list[ConceptRef]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT ds.dataset_id, ds.concept_uri, c.notation, c.label, c.in_scheme
            FROM dataset_subject ds
            LEFT JOIN concept c ON c.uri = ds.concept_uri
            WHERE ds.dataset_id IN ({placeholders})""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[ConceptRef]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                ConceptRef(
                    uri=r[1],
                    notation=r[2],
                    label=_map_to_istring(r[3]) or InternationalString(),
                    scheme_uri=r[4],
                )
            )
        return result

    def _batch_read_dataset_types(self, dataset_ids: list[str]) -> dict[str, ConceptRef]:
        """Batch-fetch dataset_type ConceptRefs, hydrated from the concept table where available."""
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT d.identifier, d.dataset_type, c.notation, c.label, c.in_scheme
            FROM dataset d
            LEFT JOIN concept c ON c.uri = d.dataset_type
            WHERE d.identifier IN ({placeholders}) AND d.dataset_type IS NOT NULL""",
            dataset_ids,
        ).fetchall()
        return {
            r[0]: ConceptRef(
                uri=r[1],
                notation=r[2],
                label=_map_to_istring(r[3]) or InternationalString(),
                scheme_uri=r[4],
            )
            for r in rows
        }

    def _batch_read_keywords(self, dataset_ids: list[str]) -> dict[str, list[InternationalString]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"SELECT dataset_id, keyword FROM dataset_keyword WHERE dataset_id IN ({placeholders}) ORDER BY ordinal",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[InternationalString]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(_map_to_istring(r[1]) or InternationalString())
        return result

    def _batch_read_spatial(self, dataset_ids: list[str]) -> dict[str, list[SpatialCoverage]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, label, uri, bbox_west, bbox_south, bbox_east, bbox_north
            FROM dataset_spatial WHERE dataset_id IN ({placeholders}) ORDER BY ordinal""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[SpatialCoverage]] = {}
        for r in rows:
            label = _map_to_istring(r[1])
            bbox: tuple[float, float, float, float] | None = None
            if r[3] is not None:
                bbox = (r[3], r[4], r[5], r[6])
            result.setdefault(r[0], []).append(SpatialCoverage(label=label, uri=r[2], bbox=bbox))
        return result

    def _batch_read_data_services(self, ids: set[str]) -> dict[str, DataService]:
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        svc_rows = self._c.execute(
            f"SELECT id, title, description, endpoint_url FROM data_service WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        serves_rows = self._c.execute(
            f"SELECT service_id, dataset_id FROM service_dataset WHERE service_id IN ({placeholders})",
            list(ids),
        ).fetchall()
        serves_map: dict[str, list[str]] = {}
        for r in serves_rows:
            serves_map.setdefault(r[0], []).append(r[1])
        return {
            r[0]: DataService(
                id=r[0],
                title=_map_to_istring(r[1]) or InternationalString(),
                description=_map_to_istring(r[2]),
                endpoint_url=r[3] or "",
                serves_datasets=serves_map.get(r[0], []),
            )
            for r in svc_rows
        }

    def _batch_read_distributions(self, dataset_ids: list[str]) -> dict[str, list[Distribution]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, id, title, description, access_url, download_url,
                media_type, format, byte_size, issued, modified,
                licence_id, service_id, distribution_type
            FROM distribution WHERE dataset_id IN ({placeholders})""",
            dataset_ids,
        ).fetchall()
        # Collect nested FK IDs
        lic_ids: set[str] = set()
        svc_ids: set[str] = set()
        for r in rows:
            if r[11] is not None:
                lic_ids.add(r[11])
            if r[12] is not None:
                svc_ids.add(r[12])
        licences = self._batch_read_licences(lic_ids)
        services = self._batch_read_data_services(svc_ids)

        result: dict[str, list[Distribution]] = {}
        for r in rows:
            dist = Distribution(
                id=r[1],
                title=_map_to_istring(r[2]),
                description=_map_to_istring(r[3]),
                access_url=r[4],
                download_url=r[5],
                media_type=r[6],
                format=r[7],
                byte_size=r[8],
                issued=r[9],
                modified=r[10],
                licence=licences.get(r[11]) if r[11] else None,
                access_service=services.get(r[12]) if r[12] else None,
                distribution_type=r[13],
            )
            result.setdefault(r[0], []).append(dist)
        return result

    def _batch_read_standards(self, dataset_ids: list[str]) -> dict[str, list[Standard]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT ds.dataset_id, s.id, s.label, s.uri
            FROM dataset_standard ds
            JOIN standard s ON ds.standard_id = s.id
            WHERE ds.dataset_id IN ({placeholders})""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[Standard]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                Standard(id=r[1], label=_map_to_istring(r[2]) or InternationalString(), uri=r[3])
            )
        return result

    def _batch_read_quality_annotations(self, dataset_ids: list[str]) -> dict[str, list[QualityAnnotation]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, dimension, label, body
            FROM quality_annotation WHERE dataset_id IN ({placeholders}) ORDER BY ordinal""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[QualityAnnotation]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                QualityAnnotation(dimension=r[1], label=_map_to_istring(r[2]), body=_map_to_istring(r[3]))
            )
        return result

    def _batch_read_provenance(self, dataset_ids: list[str]) -> dict[str, list[ProvenanceStatement]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, label, uri
            FROM provenance WHERE dataset_id IN ({placeholders}) ORDER BY ordinal""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[ProvenanceStatement]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                ProvenanceStatement(label=_map_to_istring(r[1]) or InternationalString(), uri=r[2])
            )
        return result

    def _batch_read_dimension_names(self, dataset_ids: list[str]) -> dict[str, list[InternationalString]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, label
            FROM dimension_name WHERE dataset_id IN ({placeholders}) ORDER BY ordinal""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[InternationalString]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(_map_to_istring(r[1]) or InternationalString())
        return result

    def _batch_read_variables(self, dataset_ids: list[str]) -> dict[str, list[Variable]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"""SELECT dataset_id, variable_id, label, description, value_type,
                classification_id, universe, question_text, valid_range, derivation
            FROM variable WHERE dataset_id IN ({placeholders})""",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[Variable]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(
                Variable(
                    id=r[1],
                    label=_map_to_istring(r[2]) or InternationalString(),
                    description=_map_to_istring(r[3]),
                    value_type=r[4],
                    classification_id=r[5],
                    universe=r[6],
                    question_text=r[7],
                    valid_range=r[8],
                    derivation=r[9],
                )
            )
        return result

    def _batch_read_feature_types(self, dataset_ids: list[str]) -> dict[str, list[str]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"SELECT dataset_id, type_name FROM feature_type WHERE dataset_id IN ({placeholders})",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(r[1])
        return result

    def _batch_read_authors(self, dataset_ids: list[str]) -> dict[str, list[str]]:
        if not dataset_ids:
            return {}
        placeholders = ", ".join("?" * len(dataset_ids))
        rows = self._c.execute(
            f"SELECT dataset_id, name FROM author WHERE dataset_id IN ({placeholders}) ORDER BY ordinal",
            dataset_ids,
        ).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r[0], []).append(r[1])
        return result

    # -- Batch reconstruction --

    #: Derived from `DatasetRow._fields` — see #110.
    _DATASET_COLUMNS = _DATASET_COLUMN_SQL

    def _batch_reconstruct(
        self,
        rows: list[DatasetRow],
        *,
        include_rels: frozenset[str] | None = None,
    ) -> list[BaseDataset]:
        """Reconstruct multiple datasets using batched relationship loading.

        Uses one SQL query per relationship type (selectin strategy) instead of
        N queries per dataset.

        Args:
            rows: Main ``statcat.dataset`` rows already fetched, in this module's
                declared column order.
            include_rels: When ``None`` (default) all relationships are loaded (~17 queries). When a
                frozenset, only the named relationships are fetched; the rest default to ``None`` / ``[]``
                on the assembled objects. Use ``QueryBuilder.include()`` to set this on a query.

        Returns:
            The reconstructed datasets, in the order *rows* supplied them.
        """
        if not rows:
            return []

        # Extract identifiers and FK IDs from the already-fetched main rows.
        # This is cheap (no SQL) and always runs regardless of include_rels.
        identifiers: list[str] = []
        publisher_ids: set[str] = set()
        creator_ids: set[str] = set()
        contact_ids: set[str] = set()
        frequency_ids: set[str] = set()
        licence_ids: set[str] = set()
        for row in rows:
            identifiers.append(row.identifier)
            if row.publisher_id:
                publisher_ids.add(row.publisher_id)
            if row.creator_id:
                creator_ids.add(row.creator_id)
            if row.contact_id:
                contact_ids.add(row.contact_id)
            if row.frequency_id:
                frequency_ids.add(row.frequency_id)
            if row.licence_id:
                licence_ids.add(row.licence_id)

        def _load(rel: str) -> bool:
            return include_rels is None or rel in include_rels

        # Batch-fetch only the requested relationships.
        ctx = _BatchContext(
            agents=(
                self._batch_read_agents(publisher_ids | creator_ids) if (_load("publisher") or _load("creator")) else {}
            ),
            contacts=self._batch_read_contacts(contact_ids) if _load("contact_point") else {},
            frequencies=self._batch_read_frequencies(frequency_ids) if _load("frequency") else {},
            licences=self._batch_read_licences(licence_ids) if _load("licence") else {},
            themes=self._batch_read_themes(identifiers) if _load("themes") else {},
            subject=self._batch_read_subjects(identifiers) if _load("subject") else {},
            dataset_type=self._batch_read_dataset_types(identifiers) if _load("dataset_type") else {},
            keywords=self._batch_read_keywords(identifiers) if _load("keywords") else {},
            spatial=self._batch_read_spatial(identifiers) if _load("spatial_coverage") else {},
            distributions=self._batch_read_distributions(identifiers) if _load("distributions") else {},
            standards=self._batch_read_standards(identifiers) if _load("conforms_to") else {},
            quality=self._batch_read_quality_annotations(identifiers) if _load("quality_annotations") else {},
            provenance=self._batch_read_provenance(identifiers) if _load("provenance") else {},
            dimension_names=self._batch_read_dimension_names(identifiers) if _load("dimension_names") else {},
            variables=self._batch_read_variables(identifiers) if _load("variables") else {},
            feature_types=self._batch_read_feature_types(identifiers) if _load("feature_types") else {},
            authors=self._batch_read_authors(identifiers) if _load("authors") else {},
        )

        return [_assemble_dataset(row, ctx, include_rels=include_rels) for row in rows]

    def _fetch_datasets(
        self,
        identifiers: list[str],
        *,
        include_rels: frozenset[str] | None = None,
    ) -> list[BaseDataset]:
        """Batch-fetch and hydrate datasets by identifier list."""
        if not identifiers:
            return []
        placeholders = ", ".join("?" * len(identifiers))
        raw = self._c.execute(
            f"SELECT {self._DATASET_COLUMNS} FROM dataset WHERE identifier IN ({placeholders})",
            identifiers,
        ).fetchall()
        return self._batch_reconstruct([DatasetRow(*r) for r in raw], include_rels=include_rels)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query[T](self, cls: type[T], **filters: object) -> QueryBuilder[T]:
        """Start a structured query. Returns a QueryBuilder for chaining."""
        builder: QueryBuilder[T] = QueryBuilder(self, cls)
        if filters:
            builder = builder.filter(**filters)
        return builder

    def search(
        self,
        text: str,
        *,
        kinds: Sequence[str] = ("dataset", "concept", "code"),
        lang: str = "en",
        limit: int = 50,
        offset: int = 0,
        operator: str = "or",
        fuzzy: int = 0,
        rrf_k: int = 60,
        per_kind_oversample: int = 100,
    ) -> SearchResult[AnyHit]:
        """Search across every indexed entity kind, RRF-merged.

        Returns a heterogeneous `SearchResult` of `Hit`
        objects — dataset hits, concept hits, and code hits in one
        ranked list. Callers narrow by ``hit.kind`` (a discriminator
        string) or by ``hit.payload`` type when statically known.

        For type-narrowed search restrict to one kind:

        - `datasets` — ``cat.datasets.find(text, mode=...)``
        - `concepts` — ``cat.concepts.find(text, mode=...)``
        - `codes`    — ``cat.codes.find(text, mode=...)``
        - `reference_metadata` — ``cat.reference_metadata.find(text, ...)``

        ``kinds`` defaults to dataset/concept/code; pass
        ``"reference_metadata"`` explicitly to fold methodology-document
        sections into the merged ranking.

        Cross-kind ranking uses Reciprocal-Rank-Fusion over each kind's
        BM25 ranking. RRF is robust to score-scale differences between
        short code labels (high BM25 scores from fewer terms) and long
        dataset descriptions (lower scores from term competition) — a
        plain score sort would silently bias toward codes. To include
        vector evidence per kind, narrow to one scope and pass
        ``mode="hybrid"``.

        Returns an empty result when the index has not been built.

        Returns:
            A new `QueryBuilder` with the search text applied. The original is
            unchanged — builders are immutable.

        Args:
            text: The search text.
            kinds: Entity kinds to search. ``None`` (default) searches all.
            lang: Language whose per-language index answers the query.
            limit: Maximum hits in the merged result.
            offset: Hits to skip, for paging.
            operator: How to join multi-term queries — ``"or"`` (default)
                matches any term, ``"and"`` requires all.
            fuzzy: Levenshtein edit distance for fuzzy matching (0, 1, or 2).
            rrf_k: Reciprocal-rank-fusion constant. Larger values flatten the
                contribution of rank position when merging the per-kind lists.
            per_kind_oversample: Hits to take from each kind before merging, so
                a kind that ranks poorly overall still gets considered.
        """
        if self._lance is None:
            return SearchResult(items=[], total=0, offset=offset)
        # Each kind delegates to its typed scope's bm25 ranking; vector
        # evidence is opt-in per-scope (narrow to one kind, mode="hybrid").
        # Left to infer: the union of the four scope types. Each has `find`,
        # so the call below resolves on the union, and its result unions to
        # exactly `AnyHit` — more precise than any common supertype.
        scopes = {
            "dataset": self.datasets,
            "concept": self.concepts,
            "code": self.codes,
            "reference_metadata": self.reference_metadata,
        }
        per_kind: dict[str, list[AnyHit]] = {
            kind: list(
                scope.find(
                    text,
                    mode="bm25",
                    lang=lang,
                    limit=per_kind_oversample,
                    operator=operator,
                    fuzzy=fuzzy,
                ).items
            )
            for kind, scope in scopes.items()
            if kind in kinds
        }

        # RRF across kinds — keyed by (kind, identity) so a code and a
        # dataset sharing an identifier don't collide.
        def _identity(hit: AnyHit) -> str:
            payload = hit.payload
            set_urn = getattr(payload, "set_urn", None)
            if set_urn:  # reference-metadata chunk — unique per section chunk
                return f"{set_urn}#{getattr(payload, 'attribute_path', '')}#{getattr(payload, 'chunk_index', 0)}"
            for attr in ("concept_urn", "code_urn", "identifier"):
                value = getattr(payload, attr, None)
                if value:
                    return str(value)
            return repr(payload)

        scored: dict[tuple[str, str], tuple[float, AnyHit]] = {}
        for kind, hits_for_kind in per_kind.items():
            for rank, hit in enumerate(hits_for_kind):
                key = (kind, _identity(hit))
                rrf_score = 1.0 / (rrf_k + rank + 1)
                existing = scored.get(key)
                merged_score = (existing[0] if existing else 0.0) + rrf_score
                scored[key] = (merged_score, hit)

        ordered = sorted(scored.values(), key=lambda kv: -kv[0])
        total = len(ordered)
        page = ordered[offset : offset + limit]
        # Each Hit's `score` field is the BM25 score from its own kind's
        # ranking; surface the cross-kind RRF score via the SearchResult
        # scores map so callers can recover the merge order if they need it.
        items = [hit for _, hit in page]
        scores = {f"{hit.kind}:{_identity(hit)}": s for s, hit in page}
        return SearchResult(items=items, total=total, offset=offset, scores=scores)

    def facets(self, *fields: str) -> dict[str, dict[str, int]]:
        """Return facet counts for specified fields across all datasets.

        Supported fields: ``"publisher"``, ``"creator"``, ``"themes"``, ``"frequency"``,
        ``"status"``, ``"spatial"``, ``"type"``.
        """
        return {f: self._facet_all(f) for f in fields}

    def _facet_all(self, field_name: str) -> dict[str, int]:
        cur = self._c
        if field_name == "publisher":
            rows = cur.execute(
                "SELECT a.id, COUNT(*) AS cnt"
                " FROM dataset d JOIN agent a ON d.publisher_id = a.id"
                " GROUP BY a.id ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "creator":
            rows = cur.execute(
                "SELECT a.id, COUNT(*) AS cnt"
                " FROM dataset d JOIN agent a ON d.creator_id = a.id"
                " GROUP BY a.id ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "themes":
            rows = cur.execute(
                "SELECT dt.concept_uri, COUNT(*) AS cnt FROM dataset_theme dt GROUP BY dt.concept_uri ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "frequency":
            rows = cur.execute(
                "SELECT frequency_id, COUNT(*) AS cnt FROM dataset"
                " WHERE frequency_id IS NOT NULL"
                " GROUP BY frequency_id ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "status":
            rows = cur.execute(
                "SELECT status, COUNT(*) AS cnt FROM dataset WHERE status IS NOT NULL GROUP BY status ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "spatial":
            rows = cur.execute(
                "SELECT ds.label['en'], COUNT(DISTINCT ds.dataset_id) AS cnt"
                " FROM dataset_spatial ds"
                " WHERE ds.label['en'] IS NOT NULL"
                " GROUP BY ds.label['en'] ORDER BY cnt DESC"
            ).fetchall()
        elif field_name == "type":
            rows = cur.execute("SELECT kind, COUNT(*) AS cnt FROM dataset GROUP BY kind ORDER BY cnt DESC").fetchall()
        else:
            return {}
        return {row[0]: row[1] for row in rows}

    def agent_labels(self, lang: str = "en") -> dict[str, str]:
        """Return ``{agent_id: display_name}`` for the publisher facet sidebar."""
        rows = self._c.execute(
            "SELECT id, name[?] FROM agent WHERE name[?] IS NOT NULL",
            [lang, lang],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def concept_labels(self, lang: str = "en", *, scheme_uri: str | None = None) -> dict[str, str]:
        """Return ``{concept_uri: display_label}`` for the theme/subject facet sidebar."""
        if scheme_uri is not None:
            rows = self._c.execute(
                "SELECT uri, label[?] FROM concept WHERE label[?] IS NOT NULL AND in_scheme = ?",
                [lang, lang, scheme_uri],
            ).fetchall()
        else:
            rows = self._c.execute(
                "SELECT uri, label[?] FROM concept WHERE label[?] IS NOT NULL",
                [lang, lang],
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def spatial_labels(self, lang: str = "en") -> dict[str, str]:
        """Return ``{en_label: display_label}`` for the spatial facet sidebar."""
        if lang == "en":
            return {}
        # SELECT DISTINCT instead of GROUP BY because DuckDB rejects a
        # COALESCE built from grouped MAP-indexed expressions, even when
        # both inner columns appear in the GROUP BY list.
        rows = self._c.execute(
            """SELECT DISTINCT label['en'], COALESCE(label[?], label['en'])
            FROM dataset_spatial
            WHERE label['en'] IS NOT NULL""",
            [lang],
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # Internal query execution
    # ------------------------------------------------------------------

    def _execute_query[T](
        self,
        cls: type[T],
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        map_filters: list[MapFilter],
        from_ids: list[str] | None,
        text: str | None,
        hybrid_text: str | None,
        search_operator: str,
        search_fuzziness: int,
        sort_fields: list[_SortSpec],
        limit: int | None,
        offset: int,
        facet_fields: list[str],
        *,
        include_rels: frozenset[str] | None = None,
    ) -> SearchResult[T]:
        # Entity types (Agent, DataService, Distribution) use flat-column queries
        if cls in _ENTITY_SPECS:
            return self._execute_entity_query(cls, filters, exists_filters, limit, offset)

        fsql = self._build_filter_sql(cls, filters)
        for ef in exists_filters:
            exists_sql, exists_params = ef.to_sql(fsql)
            fsql.add_where(exists_sql, *exists_params)
        for mf in map_filters:
            mf_sql, mf_params = mf.to_sql(fsql)
            fsql.add_where(mf_sql, *mf_params)

        if from_ids is not None and not from_ids:
            return SearchResult(items=[], total=0, facets={}, offset=offset, scores={})
        if from_ids is not None:
            placeholders = ", ".join("?" * len(from_ids))
            fsql.add_where(f"d.identifier IN ({placeholders})", *from_ids)

        if hybrid_text is not None or text is not None:
            search_text: str = hybrid_text if hybrid_text is not None else (text or "")
            search_type = "hybrid" if hybrid_text is not None else "fts"
            qr = self._run_search_query(
                fsql.join_clause,
                fsql.where_clause,
                fsql.params,
                search_text,
                search_type,
                sort_fields,
                limit,
                offset,
                from_ids=from_ids,
                operator=search_operator,
                fuzziness=search_fuzziness,
            )
        else:
            qr = self._run_filter_query(
                fsql.join_clause,
                fsql.where_clause,
                fsql.params,
                sort_fields,
                limit,
                offset,
                from_ids=from_ids,
            )

        all_datasets = self._fetch_datasets(list(qr.rows), include_rels=include_rels)
        ds_by_id = {ds.identifier: ds for ds in all_datasets}
        # `_fetch_datasets` reconstructs by the row's `kind` column, so it is
        # typed `BaseDataset` while `cls` is what was asked for. The `kind`
        # filter in the SQL is what makes them agree.
        #
        # Not narrowed with `isinstance(ds, cls)`, which is the fix used for
        # `_ENTITY_RECONSTRUCTORS` above and does not work here: `cls` is not
        # always a class. `query(sc.Dataset)` passes a `TypeAliasType` — the
        # union of every dataset kind — and `isinstance` raises `TypeError:
        # arg 2 must be a type`. Found by trying it; five tests failed.
        # Entity queries never see the alias, which is why the same fix holds
        # there.
        items: list[T] = [  # pyrefly: ignore[bad-assignment]
            ds_by_id[ident] for ident in qr.rows if ident in ds_by_id
        ]

        computed_facets: dict[str, dict[str, int]] = {}
        for ff in facet_fields:
            computed_facets[ff] = self._compute_facet_in_context(ff, fsql)

        return SearchResult(items=items, total=qr.total, facets=computed_facets, offset=offset, scores=qr.scores)

    def _execute_entity_query[T](
        self,
        cls: type[T],
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        limit: int | None,
        offset: int,
    ) -> SearchResult[T]:
        """Execute a flat-column query for non-dataset entity types."""
        spec = _ENTITY_SPECS[cls]
        table = spec.table

        wheres: list[str] = []
        params: list[object] = []
        for key, val in filters.items():
            col = spec.filter_cols.get(key)
            if col:
                wheres.append(f"{col} = ?")
                params.append(val)

        # Entity queries (Agent, DataService, Distribution) don't bind to
        # the ``d.identifier`` outer alias that exists_filters expect. We
        # still pass a context so the predicates' to_sql() signatures match;
        # in practice exists_filters here are rare.
        ef_ctx = _QueryContext(row_alias=table, registry=self._registry)
        for ef in exists_filters:
            ef_sql, ef_params = ef.to_sql(ef_ctx)
            wheres.append(ef_sql)
            params.extend(ef_params)

        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        count_sql = f"SELECT COUNT(*) FROM {table} {where_clause}"
        fetch_sql = f"SELECT {spec.columns} FROM {table} {where_clause}"
        if limit is not None:
            fetch_sql += f" LIMIT {limit}"
        if offset:
            fetch_sql += f" OFFSET {offset}"

        count_row = self._c.execute(count_sql, params).fetchone()
        total = int(count_row[0]) if count_row else 0
        rows = self._c.execute(fetch_sql, params).fetchall()
        # `_ENTITY_SPECS` erases each spec to `_EntitySpec[Any, Any]`: a dict
        # cannot say that the value's type depends on the key. So `reconstruct`
        # returns `Any` here, and the `isinstance` is what soundly recovers `T`
        # — the bridge back from runtime dispatch to the declared return type,
        # not a redundant assertion. It is also why this path needs no
        # suppression while the dataset path above does.
        items: list[T] = []
        for raw in rows:
            item = spec.build(raw)
            if not isinstance(item, cls):
                msg = f"_ENTITY_SPECS[{cls.__name__}] built {type(item).__name__}"
                raise TypeError(msg)
            items.append(item)
        return SearchResult(items=items, total=total, offset=offset)

    def _run_search_query(
        self,
        join_clause: str,
        where_clause: str,
        params: list[object],
        text: str,
        search_type: str,
        sort_fields: list[_SortSpec],  # noqa: ARG002
        limit: int | None,
        offset: int,
        *,
        from_ids: list[str] | None = None,  # noqa: ARG002
        operator: str = "or",
        fuzziness: int = 0,
    ) -> _QueryResult:
        """Execute a search query using LanceDB.

        When the LanceDB index is unavailable (lancedb not installed, or
        ``build_index()`` has not been called yet), returns an empty
        result instead of raising — callers should already handle the
        empty-results case, and a soft failure is friendlier than a
        hard ``RuntimeError`` mid-request.
        """
        if self._lance is None:
            return _QueryResult(total=0, rows=[], scores={})

        if search_type == "hybrid":
            if self._embedding_model is None:
                msg = "hybrid_search requires an embedding_model"
                raise RuntimeError(msg)
            rrf_scores = self._rrf_rank(text, operator=operator, fuzziness=fuzziness)
            search_ids = sorted(rrf_scores, key=lambda i: rrf_scores[i], reverse=True)
            scores = rrf_scores
        else:
            bm25_hits = self._lance.bm25_search(
                text,
                limit=200,
                operator=operator,
                fuzziness=fuzziness,
                entity_type=_DATASET_ENTITY_TYPES,
            )
            search_ids = [h[0] for h in bm25_hits]
            scores = {h[0]: h[1] for h in bm25_hits}

        if not search_ids:
            return _QueryResult(total=0, rows=[], scores={})

        # Apply relational filters to search results
        placeholders = ", ".join("?" * len(search_ids))
        extra_where = f"d.identifier IN ({placeholders})"
        if where_clause:
            combined_where = f"{where_clause} AND {extra_where}"
        else:
            combined_where = f"WHERE {extra_where}"

        all_params = [*params, *search_ids]
        count_sql = f"SELECT COUNT(*) FROM dataset d {join_clause} {combined_where}"
        fetch_sql = f"SELECT d.identifier FROM dataset d {join_clause} {combined_where}"

        count_row = self._c.execute(count_sql, all_params).fetchone()
        total = int(count_row[0]) if count_row else 0
        rows = self._c.execute(fetch_sql, all_params).fetchall()

        # Preserve search rank order
        filtered_ids = {r[0] for r in rows}
        ordered = [ident for ident in search_ids if ident in filtered_ids]
        filtered_scores = {k: v for k, v in scores.items() if k in filtered_ids}

        page_start = offset
        page_end = (offset + limit) if limit is not None else None
        return _QueryResult(total=total, rows=ordered[page_start:page_end], scores=filtered_scores)

    def _ranked_ids_from_search(
        self,
        cls: type,
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        map_filters: list[MapFilter],
        text: str | None,
        hybrid_text: str | None,
        search_operator: str,
        search_fuzziness: int,
        sort_fields: list[_SortSpec],
        limit: int | None,
        offset: int,
    ) -> list[str]:
        """Run a search query with relational filters and return ranked IDs."""
        search_text = hybrid_text if hybrid_text is not None else text
        if search_text is None:
            return []
        search_type = "hybrid" if hybrid_text is not None else "fts"

        fsql = self._build_filter_sql(cls, filters)
        for ef in exists_filters:
            exists_sql, exists_params = ef.to_sql(fsql)
            fsql.add_where(exists_sql, *exists_params)
        for mf in map_filters:
            mf_sql, mf_params = mf.to_sql(fsql)
            fsql.add_where(mf_sql, *mf_params)

        qr = self._run_search_query(
            fsql.join_clause,
            fsql.where_clause,
            fsql.params,
            search_text,
            search_type,
            sort_fields,
            limit=limit,
            offset=offset,
            operator=search_operator,
            fuzziness=search_fuzziness,
        )
        return qr.rows

    _NULLS_OPTIONS: frozenset[str] = frozenset({"first", "last"})

    @staticmethod
    def _build_order_clause(
        sort_fields: list[_SortSpec],
        from_ids: list[str] | None,
    ) -> tuple[str, list[object]]:
        """Build the ORDER BY clause for a filter query.

        Returns ``(sql_fragment, params)`` — MAP columns use parameterised
        ``element_at()`` so the language key must be bound.
        """
        all_fields = _SORTABLE_COLUMNS | _MAP_SORTABLE_COLUMNS
        if sort_fields:
            parts: list[str] = []
            order_params: list[object] = []
            for spec in sort_fields:
                if spec.field not in all_fields:
                    msg = f"Invalid sort field: {spec.field!r}. Allowed: {sorted(all_fields)}"
                    raise ValueError(msg)
                if spec.field in _MAP_SORTABLE_COLUMNS:
                    if spec.lang is None:
                        msg = f"lang is required when sorting by MAP column {spec.field!r}"
                        raise ValueError(msg)
                    col_expr = f"d.{spec.field}[?]"
                    order_params.append(spec.lang)
                else:
                    col_expr = f"d.{spec.field}"
                clause = f"{col_expr} {'DESC' if spec.desc else 'ASC'}"
                if spec.nulls is not None:
                    if spec.nulls not in Catalog._NULLS_OPTIONS:
                        msg = f"Invalid nulls option: {spec.nulls!r}. Allowed: {sorted(Catalog._NULLS_OPTIONS)}"
                        raise ValueError(msg)
                    clause += f" NULLS {spec.nulls.upper()}"
                parts.append(clause)
            return "ORDER BY " + ", ".join(parts), order_params
        if from_ids is not None and from_ids:
            return "ORDER BY list_position(?::VARCHAR[], d.identifier)", []
        return "ORDER BY d.identifier ASC", []

    def _run_filter_query(
        self,
        join_clause: str,
        where_clause: str,
        params: list[object],
        sort_fields: list[_SortSpec],
        limit: int | None,
        offset: int,
        *,
        from_ids: list[str] | None = None,
    ) -> _QueryResult:
        count_sql = f"SELECT COUNT(*) FROM dataset d {join_clause} {where_clause}"
        order_clause, order_params = self._build_order_clause(sort_fields, from_ids)

        fetch_sql = f"""SELECT d.identifier
            FROM dataset d {join_clause} {where_clause}
            {order_clause}"""
        if limit is not None:
            fetch_sql += f" LIMIT {limit}"
        if offset:
            fetch_sql += f" OFFSET {offset}"

        count_row = self._c.execute(count_sql, params).fetchone()
        total = int(count_row[0]) if count_row else 0

        if not sort_fields and from_ids is not None and from_ids:
            fetch_params = [*params, *order_params, from_ids]
        else:
            fetch_params = [*params, *order_params]
        rows = self._c.execute(fetch_sql, fetch_params).fetchall()
        return _QueryResult(total=total, rows=[r[0] for r in rows])

    def _execute_projection(
        self,
        cls: type,
        select_cols: tuple[str, ...],
        select_lang: str | None,
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        map_filters: list[MapFilter],
        from_ids: list[str] | None,
        sort_fields: list[_SortSpec],
        limit: int | None,
        offset: int,
    ) -> list[Row]:
        """Lightweight Row projection — single SQL, no store.get()."""
        fsql = self._build_filter_sql(cls, filters)
        for ef in exists_filters:
            exists_sql, exists_params = ef.to_sql(fsql)
            fsql.add_where(exists_sql, *exists_params)
        for mf in map_filters:
            mf_sql, mf_params = mf.to_sql(fsql)
            fsql.add_where(mf_sql, *mf_params)
        if from_ids is not None and not from_ids:
            return []
        if from_ids is not None:
            placeholders = ", ".join("?" * len(from_ids))
            fsql.add_where(f"d.identifier IN ({placeholders})", *from_ids)

        col_exprs: list[str] = []
        col_params: list[object] = []
        for col in select_cols:
            if col in _MAP_SELECTABLE_COLUMNS:
                col_exprs.append(f"d.{col}[?] AS {col}")
                col_params.append(select_lang)
            else:
                col_exprs.append(f"d.{col}")

        order_clause, order_params = self._build_order_clause(sort_fields, from_ids)

        sql = f"SELECT {', '.join(col_exprs)} FROM dataset d {fsql.join_clause} {fsql.where_clause} {order_clause}"
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset:
            sql += f" OFFSET {offset}"

        all_params = col_params + fsql.params
        if not sort_fields and from_ids is not None and from_ids:
            all_params = [*all_params, *order_params, from_ids]
        else:
            all_params = [*all_params, *order_params]

        rows = self._c.execute(sql, all_params).fetchall()
        return [Row({col: row[i] for i, col in enumerate(select_cols)}) for row in rows]

    def _execute_scalar_projection(
        self,
        cls: type,
        scalar_col: str,
        select_lang: str | None,
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        map_filters: list[MapFilter],
        from_ids: list[str] | None,
        sort_fields: list[_SortSpec],
        limit: int | None,
        offset: int,
    ) -> list[object]:
        """Single-column flat projection — returns list of bare values."""
        fsql = self._build_filter_sql(cls, filters)
        for ef in exists_filters:
            exists_sql, exists_params = ef.to_sql(fsql)
            fsql.add_where(exists_sql, *exists_params)
        for mf in map_filters:
            mf_sql, mf_params = mf.to_sql(fsql)
            fsql.add_where(mf_sql, *mf_params)
        if from_ids is not None and not from_ids:
            return []
        if from_ids is not None:
            placeholders = ", ".join("?" * len(from_ids))
            fsql.add_where(f"d.identifier IN ({placeholders})", *from_ids)

        col_params: list[object] = []
        if scalar_col in _MAP_SELECTABLE_COLUMNS:
            col_expr = f"d.{scalar_col}[?]"
            col_params.append(select_lang)
        else:
            col_expr = f"d.{scalar_col}"

        order_clause, order_params = self._build_order_clause(sort_fields, from_ids)

        sql = f"SELECT {col_expr} FROM dataset d {fsql.join_clause} {fsql.where_clause} {order_clause}"
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset:
            sql += f" OFFSET {offset}"

        all_params = col_params + fsql.params
        if not sort_fields and from_ids is not None and from_ids:
            all_params = [*all_params, *order_params, from_ids]
        else:
            all_params = [*all_params, *order_params]

        rows = self._c.execute(sql, all_params).fetchall()
        return [r[0] for r in rows]

    def _execute_exists(
        self,
        cls: type,
        filters: dict[str, object],
        exists_filters: list[PositionalFilter],
        map_filters: list[MapFilter],
        from_ids: list[str] | None,
    ) -> bool:
        """Return True if at least one row matches the filters."""
        fsql = self._build_filter_sql(cls, filters)
        for ef in exists_filters:
            exists_sql, exists_params = ef.to_sql(fsql)
            fsql.add_where(exists_sql, *exists_params)
        for mf in map_filters:
            mf_sql, mf_params = mf.to_sql(fsql)
            fsql.add_where(mf_sql, *mf_params)
        if from_ids is not None and not from_ids:
            return False
        if from_ids is not None:
            placeholders = ", ".join("?" * len(from_ids))
            fsql.add_where(f"d.identifier IN ({placeholders})", *from_ids)

        sql = f"SELECT 1 FROM dataset d {fsql.join_clause} {fsql.where_clause} LIMIT 1"
        row = self._c.execute(sql, fsql.params).fetchone()
        return row is not None

    def _build_filter_sql(
        self,
        cls: type,
        filters: dict[str, object],
    ) -> _QueryContext:
        """Build base SQL filter fragments from a cls + filter dict."""
        ctx = _QueryContext(row_alias="d", registry=self._registry)

        if isinstance(cls, type) and issubclass(cls, _DATASET_CLASSES):
            ctx.add_where("d.kind = ?", cls.kind)

        for key, val in filters.items():
            if key == "publisher":
                ctx.add_where("d.publisher_id = ?", val)
            elif key == "creator":
                ctx.add_where("d.creator_id = ?", val)
            elif key == "status":
                ctx.add_where("d.status = ?", val)
            elif key == "frequency":
                ctx.add_where("d.frequency_id = ?", val)
            elif key == "identifier":
                ctx.add_where("d.identifier = ?", val)

        return ctx

    def _compute_facet_in_context(
        self,
        field_name: str,
        fsql: _QueryContext,
    ) -> dict[str, int]:
        """Compute facet counts restricted to the current query's match set."""
        join_clause = fsql.join_clause
        where_clause = fsql.where_clause
        match_sql = f"SELECT DISTINCT d.identifier FROM dataset d {join_clause} {where_clause}"
        match_params = list(fsql.params)

        if field_name == "publisher":
            sql = f"""SELECT a.id, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset d ON m.identifier = d.identifier
                JOIN agent a ON d.publisher_id = a.id
                GROUP BY a.id ORDER BY cnt DESC"""
        elif field_name == "creator":
            sql = f"""SELECT a.id, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset d ON m.identifier = d.identifier
                JOIN agent a ON d.creator_id = a.id
                GROUP BY a.id ORDER BY cnt DESC"""
        elif field_name == "themes":
            sql = f"""SELECT dt.concept_uri, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset_theme dt ON m.identifier = dt.dataset_id
                GROUP BY dt.concept_uri ORDER BY cnt DESC"""
        elif field_name == "frequency":
            sql = f"""SELECT d.frequency_id, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset d ON m.identifier = d.identifier
                WHERE d.frequency_id IS NOT NULL
                GROUP BY d.frequency_id ORDER BY cnt DESC"""
        elif field_name == "status":
            sql = f"""SELECT d.status, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset d ON m.identifier = d.identifier
                WHERE d.status IS NOT NULL
                GROUP BY d.status ORDER BY cnt DESC"""
        elif field_name == "spatial":
            sql = f"""SELECT ds.label['en'], COUNT(DISTINCT m.identifier) AS cnt
                FROM ({match_sql}) m
                JOIN dataset_spatial ds ON m.identifier = ds.dataset_id
                WHERE ds.label['en'] IS NOT NULL
                GROUP BY ds.label['en'] ORDER BY cnt DESC"""
        elif field_name == "type":
            sql = f"""SELECT d.kind, COUNT(*) AS cnt
                FROM ({match_sql}) m
                JOIN dataset d ON m.identifier = d.identifier
                GROUP BY d.kind ORDER BY cnt DESC"""
        else:
            return {}

        rows = self._c.execute(sql, match_params).fetchall()
        return {row[0]: row[1] for row in rows}

    # ---------------------------------------------------------------------------
    # Structural traversal — shared sdmxlib tables
    # ---------------------------------------------------------------------------

    def dataset(self, identifier: str) -> DatasetScope:
        """Return a scope for navigating a dataset's graph relationships.

        Enables Gremlin-style traversal::

            store.dataset("housing-stats").codelists
            store.dataset("housing-stats").related("codelist")
            store.dataset("housing-stats").upstream(depth=5)
        """
        return DatasetScope(identifier=identifier, store=self)

    def codelist(self, urn: str) -> CodelistScope:
        """Return a lazy scope for the codelist identified by *urn*."""
        return CodelistScope(urn=urn, store=self)

    @property
    def codelists(self) -> CodelistsScope:
        """Lazy scope over all codelists in the shared structural tables."""
        return CodelistsScope(store=self)

    def dimensions(self, ds: AggregateDataset) -> DimensionsScope:
        """Return a lazy scope over the dimensions of *ds*."""
        if ds.sdmx_dataflow_urn is None:
            return DimensionsScope(dataflow_urn="", store=self)
        return DimensionsScope(dataflow_urn=ds.sdmx_dataflow_urn, store=self)

    def _resolve_codelist(self, urn: str) -> sdmxlib.model.codelist.Codelist:
        """Resolve a codelist URN to its full sdmxlib Codelist object."""
        reg = self.registry
        cl = reg.get(sdmxlib.model.codelist.Codelist.parse_urn(urn), eager=True)
        if cl is None:
            msg = f"Codelist not found: {urn}"
            raise LookupError(msg)
        return cl

    def _resolve_codelist_urn(self, dataflow_urn: str, dimension_id: str) -> str | None:
        """Resolve the codelist URN for a specific dimension of a dataflow."""
        try:
            row = self.connection.execute(
                """
                SELECT dc.codelist_urn
                FROM sdmx.dataflow df
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                WHERE df.urn = ?
                  AND dc.component_id = ?
                  AND dc.component_type = 'Dimension'
                  AND dc.codelist_urn IS NOT NULL
                LIMIT 1
                """,
                [dataflow_urn, dimension_id],
            ).fetchone()
        except duckdb.CatalogException:
            return None
        return row[0] if row else None

    def code_map(self, dataset_id: str, lang: str = "en") -> dict[str, dict[str, str]]:
        """Return ``{dimension_id: {code_id: label}}`` for a dataset.

        Joins catalog ``dataset`` -> ``sdmx.dataflow`` -> ``sdmx.dsd_component`` ->
        ``sdmx.code`` entirely in SQL.
        """
        try:
            rows = self.connection.execute(
                """
                SELECT dc.component_id, c.code_id, c.name[?]
                FROM statcat.dataset d
                JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn
                WHERE d.identifier = ?
                  AND dc.component_type = 'Dimension'
                ORDER BY dc.position, c.position
                """,
                [lang, dataset_id],
            ).fetchall()
        except duckdb.CatalogException:
            return {}
        result: dict[str, dict[str, str]] = {}
        for dim_id, code_id, label in rows:
            result.setdefault(dim_id, {})[code_id] = label or code_id
        return result

    def codelist_for(self, dataset_id: str, dimension: str) -> sdmxlib.model.codelist.Codelist | None:
        """Return the Codelist for a specific dimension of a dataset.

        Resolves via the shared structural tables. Returns ``None`` if the
        dataset has no SDMX link or the dimension has no enumerated codelist.
        """
        try:
            row = self.connection.execute(
                """
                SELECT dc.codelist_urn
                FROM statcat.dataset d
                JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                WHERE d.identifier = ?
                  AND dc.component_id = ?
                  AND dc.component_type = 'Dimension'
                  AND dc.codelist_urn IS NOT NULL
                LIMIT 1
                """,
                [dataset_id, dimension],
            ).fetchone()
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        codelist_urn: str = row[0]
        reg = self.registry
        return reg.get(sdmxlib.model.codelist.Codelist.parse_urn(codelist_urn), eager=True)

    def get_structure(self, ds: AggregateDataset) -> sdmxlib.model.datastructure.DataStructure | None:
        """Resolve the full DataStructure for an AggregateDataset.

        Returns ``None`` if ``ds.sdmx_dataflow_urn`` is not set or the DSD
        is not in the shared structural tables.
        """
        if ds.sdmx_dataflow_urn is None:
            return None
        try:
            row = self.connection.execute(
                "SELECT dsd_urn FROM sdmx.dataflow WHERE urn = ? LIMIT 1",
                [ds.sdmx_dataflow_urn],
            ).fetchone()
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        dsd_urn: str = row[0]
        reg = self.registry
        return reg.get(sdmxlib.model.datastructure.DataStructure.parse_urn(dsd_urn))

    # ---------------------------------------------------------------------------
    # Lineage and provenance
    # ---------------------------------------------------------------------------

    #: Recognised relationship type values (``prov:wasDerivedFrom`` family).
    LINEAGE_RELATIONSHIPS: tuple[str, ...] = (
        "derived_from",
        "revision_of",
        "supplement_to",
        "feeds_into",
        "adjusted_from",
        "aggregated_from",
        "uses_classification",
        "replaces",
    )

    #: Recognised confidence values for lineage rows.
    LINEAGE_CONFIDENCE: tuple[str, ...] = ("asserted", "inferred", "extracted")

    def add_lineage(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        *,
        activity_label: str | None = None,
        activity_type: str | None = None,
        agent_id: str | None = None,
        timestamp: datetime | None = None,
        confidence: str = "asserted",
        notes: str | None = None,
    ) -> None:
        """Record a PROV-O-aligned lineage edge between two datasets."""
        row_id = hashlib.md5(  # noqa: S324
            f"{source_id}:{target_id}:{relationship}".encode()
        ).hexdigest()
        self._exec(
            """
            INSERT INTO lineage
                (id, source_id, target_id, relationship, activity_label,
                 activity_type, agent_id, timestamp, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, target_id, relationship) DO UPDATE SET
                id             = excluded.id,
                activity_label = excluded.activity_label,
                activity_type  = excluded.activity_type,
                agent_id       = excluded.agent_id,
                timestamp      = excluded.timestamp,
                confidence     = excluded.confidence,
                notes          = excluded.notes
            """,
            [
                row_id,
                source_id,
                target_id,
                relationship,
                activity_label,
                activity_type,
                agent_id,
                timestamp,
                confidence,
                notes,
            ],
        )

"""LanceDB vector + full-text index for statcatlib search.

Isolated from the core store so that lancedb is never imported unless an
``embedding_model`` is passed to ``Catalog``, or ``search()`` is called
on a folder-mode catalog.  Core statcatlib has no dependency on lancedb.

Each language gets its own table, holding one row per entity in that
language, with a text column (for BM25 full-text search via Tantivy) and
an optional embedding column (for semantic nearest-neighbour search).
Tables are generation-suffixed (``search_fr__000003``) and reached
through a manifest, so a rebuild can write a whole new generation before
committing to any of it.

The split is what lets each language carry its own tokenizer: Tantivy
takes one stemmer and one stopword list per index, so a shared table
meant one shared configuration, and LanceDB's default is English, so one
table would stem French text with English rules. Separate tables also
give each language exact BM25 statistics — pooled into one table, ``N``
and ``avgdl`` spanned every language at once.

`LanceIndex` routes by language and is the only thing callers
touch; `_LanguageIndex` is one language's table.

Search modes
------------
- **BM25 only** — no embedding model required; ``search()`` uses Tantivy FTS
- **Semantic** — requires ``embedding_model``; pure vector nearest-neighbour
- **Hybrid** — requires ``embedding_model``; RRF merge of BM25 + vector
"""

from __future__ import annotations

import contextlib
import json
import warnings
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    import lancedb
    import lancedb.table
    import pyarrow as pa
    from lancedb.query import BooleanQuery, MatchQuery

__all__: list[str] = ["LanceIndex", "make_schema"]

#: Legacy single-table name. Catalogs built before per-language tables
#: (pinaxlib 5.23.0) have exactly this one table, holding every language under a
#: single English-stemmed FTS index. Read-only fallback; ``build_index``
#: replaces it with the per-language layout.
_TABLE = "search"

#: Prefix for per-language tables. The full name carries the language and
#: the generation that wrote it — ``search_fr__000003`` — so a rebuild's
#: tables can sit alongside the ones still serving queries.
_TABLE_PREFIX = "search_"

#: Manifest naming the live table for each language. It is the single
#: source of truth for what a query reads, which is what makes a rebuild
#: atomic without ``rename_table`` — LanceDB OSS raises
#: ``NotImplementedError`` for that. A rebuild writes new
#: generation-suffixed tables, then replaces this file in a single atomic
#: move; until that lands, every query still reads the previous
#: generation. A crash therefore leaves the old index whole and serving,
#: with the half-built tables merely orphaned.
_MANIFEST_FILE = "index.json"
_MANIFEST_VERSION = 2

#: Rows *carrying a vector* that a language's table needs before an ANN
#: index is worth building. IVF-PQ needs this many to train at all, and
#: trains poorly not far above it; under the threshold a brute-force scan
#: is both faster and exact. Now that each language has its own table the
#: bar applies per language, so a small language is left to scan rather
#: than given a badly-trained index.
#:
#: Counted over embedded rows rather than all rows on purpose. A
#: table mixing embedded rows with ``fts_only`` ones is mostly NULL in the
#: embedding column — 95% of it, on a large catalog whose big codelists
#: are ``fts_only`` — and IVF
#: trains its centroids over those NULLs too. The result is one degenerate
#: cluster holding almost all the mass, with the few real vectors
#: scattered across partitions the probe never reaches: stored vectors
#: perfectly correct, approximate retrieval near-random. Counting rows
#: would clear the bar on exactly the tables that must not be indexed.
_ANN_MIN_VECTORS = 256

#: Vector index LanceDB builds for us. IVF_FLAT stores each vector whole
#: inside its partition; IVF_PQ — LanceDB's default, and what statcatlib built
#: before pinaxlib 5.24.0 — stores a product-quantised approximation instead.
#:
#: Measured on clustered 384-d vectors, recall@10 against exact cosine
#: over the same stored vectors:
#:
#:     IVF_PQ    20%   (58% with refine_factor=10; nprobes did not move it)
#:     IVF_FLAT  100%
#:
#: At 20% the vectors were right and retrieval threw most of them away.
#: Quantisation cannot preserve the margin this corpus runs
#: on — e5 puts ~0.001 cosine between the right answer and an unrelated
#: one — and nprobes leaving recall unchanged is what identifies the loss
#: as quantisation rather than probing.
#:
#: IVF_FLAT is also *faster* here, because reading a whole vector beats
#: reconstructing it — several times faster than flat-scan at every
#: size measured. It trains at any size, where IVF_PQ needs 256 vectors.
#: The larger index on disk is the whole trade, and it buys exactness.
_ANN_INDEX_TYPE = "IVF_FLAT"

#: BCP-47 primary subtag → Tantivy Snowball stemmer name. Exactly the set
#: LanceDB accepts; anything outside it raises rather than falling back,
#: so the mapping is a whitelist and not a best guess. Notably absent:
#: Chinese, Japanese, Korean, Catalan — LanceDB rejects all four.
_SNOWBALL_LANGUAGE: dict[str, str] = {
    "ar": "Arabic",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "hu": "Hungarian",
    "it": "Italian",
    "no": "Norwegian",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "es": "Spanish",
    "sv": "Swedish",
    "ta": "Tamil",
    "tr": "Turkish",
}


def _as_int(value: object, default: int = 0) -> int:
    """Narrow a manifest value to an `int`, or *default*."""
    if isinstance(value, bool):  # bool is an int; a flag is not a count
        return default
    return value if isinstance(value, int) else default


def _as_opt_str(value: object) -> str | None:
    """Narrow a manifest value to a `str`, or ``None``."""
    return value if isinstance(value, str) else None


def _as_annotations(entry: object) -> IndexAnnotations | None:
    """Narrow the manifest's ``annotations`` entry, or ``None`` if absent or malformed."""
    if not isinstance(entry, Mapping):
        return None
    types = entry.get("types")
    fingerprint = _as_opt_str(entry.get("fingerprint"))
    if not isinstance(types, list) or fingerprint is None:
        return None
    return {"types": [str(t) for t in types], "fingerprint": fingerprint}


def _as_layout(entry: object) -> LanguageLayout | None:
    """Narrow one manifest ``languages`` entry, or ``None`` if it is not one.

    Returns ``None`` for the v1 shape, where the entry was the table name as a
    bare string rather than a mapping — the caller handles that case, since a
    v1 entry still names a usable table even though it records nothing else.
    """
    if not isinstance(entry, Mapping):
        return None
    table = _as_opt_str(entry.get("table"))
    if table is None:
        return None
    return {
        "table": table,
        "rows": _as_int(entry.get("rows")),
        "embedded": _as_int(entry.get("embedded")),
        "ann": _as_opt_str(entry.get("ann")),
        "stemmer": _as_opt_str(entry.get("stemmer")),
    }


def _statcatlib_version() -> str:
    """Return the running statcatlib version, or ``"unknown"``.

    Defined here rather than imported from ``store``: ``store`` imports
    this module, so the dependency only runs one way.
    """
    import importlib.metadata  # noqa: PLC0415

    try:
        return importlib.metadata.version("statcatlib")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _language_slug(lang: str) -> str:
    """Return the table-name suffix for *lang* (``"fr-CA"`` → ``"fr_ca"``).

    Region subtags are preserved: ``fr-CA`` and ``fr-FR`` are different
    corpora with different vocabularies, even though they stem alike, and
    collapsing them would re-pool the document frequencies that separate
    tables exist to keep apart.
    """
    return lang.strip().lower().replace("-", "_")


def _stemmer_for(lang: str) -> str | None:
    """Return the Snowball stemmer name for *lang*, or ``None``.

    Falls back on the primary subtag, so ``fr-CA`` stems as French. Returns
    ``None`` for any language LanceDB has no stemmer for — the caller then
    indexes it unstemmed, which is merely weaker. The one thing never done
    is substituting a *different* language's stemmer: per-language tables
    exist so French text is never stemmed with English rules, and a third
    language gets the same guarantee.
    """
    return _SNOWBALL_LANGUAGE.get(_language_slug(lang).split("_")[0])


_EMBEDDING_COLUMN = "embedding"

#: Columns every row carries, whatever entity kind it describes.
#: ``text`` is the FTS target; ``lang`` is its BCP-47 language.
_BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "string"),
    ("entity_type", "string"),
    ("lang", "string"),
    ("text", "string"),
)

#: Entity-specific columns. Every row carries every one of these — the
#: collectors pad with empty values (``_EMPTY_CODE_FIELDS`` and friends in
#: ``store.py``) so a single table can hold all four entity kinds.
_EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    # code rows
    ("dimension_id", "string"),
    ("dimension_label", "string"),
    ("code_id", "string"),
    ("code_label", "string"),
    ("code_urn", "string"),
    ("codelist_urn", "string"),
    ("parent_id", "string"),
    ("depth", "int64"),
    # concept rows
    ("concept_urn", "string"),
    ("scheme_urn", "string"),
    ("concept_id", "string"),
    # reference-metadata rows
    ("set_urn", "string"),
    ("attribute_path", "string"),
    ("section_title", "string"),
)


class LanguageLayout(TypedDict):
    """What one language's table was built with, as `Catalog.index_layout` reports it.

    Public: this is the shape callers get back, and it is the thing to check
    after a build — "was French actually stemmed as French?" is answerable from
    it without opening the index.

    `stemmer` is ``None`` for a language LanceDB has no Snowball stemmer for
    (Chinese, Japanese, Korean, Catalan), which is indexed unstemmed rather
    than with another language's rules. `ann` is ``None`` when no vector index
    was built. `rows` and `embedded` differ by an order of magnitude on a
    catalog mixing embedded and ``fts_only`` codelists, which is what makes
    "why is vector search scanning?" answerable here.
    """

    table: str
    rows: int
    embedded: int
    ann: str | None
    stemmer: str | None


class IndexAnnotations(TypedDict):
    """The annotations an index was built from, as `Catalog.index_annotations` reports it.

    `types` are the annotation types `build_index` read (what to exclude, what
    context and synonyms to fold in); `fingerprint` is a digest of every
    annotation of those types in the store at the time. An overlay folded
    into the store afterwards changes the fingerprint the store yields, which
    is how a stale index is detected without rebuilding it (GH-3).
    """

    types: list[str]
    fingerprint: str


class _Manifest(TypedDict):
    """``index.json``, as written.

    A `TypedDict` on the write side only, for the same reason as
    `store._EmbeddingsSidecar`: here the shape is guaranteed because this
    module builds it. `_read_manifest` returns `dict[str, object]` — that
    value came off disk and may have been written by an older statcatlib, so
    its shape is unknown until checked.
    """

    version: int
    written_by: str
    generation: int
    languages: dict[str, LanguageLayout]
    annotations: NotRequired[IndexAnnotations]


class SearchRow(TypedDict):
    """One row of the search corpus, as written to a language table.

    Mirrors `_BASE_COLUMNS` + `_EXTENSION_COLUMNS` + `_EMBEDDING_COLUMN`
    above, and `test_search_row_matches_schema` asserts the two agree — a
    duplicated column list that can drift is worse than none.

    The four base keys are required; every extension key belongs to one row
    kind (code, concept, reference-metadata) and is `NotRequired`. That is
    what makes a typo worth catching here: the Arrow schema is declared, so a
    misspelled key does not raise — it writes nothing and leaves the real
    column null, which is exactly the failure `make_schema` exists to prevent
    for `embedding`.

    Collectors still pad the keys they do not own with ``""`` / ``0`` rather
    than omitting them. `_to_arrow` would turn an absent key into NULL and
    says so, so the padding is belt-and-braces — but the two are not the same
    stored value, and anything filtering on ``code_id = ''`` would notice.
    Removing the padding would change stored data.
    """

    id: str
    entity_type: str
    lang: str
    text: str
    # code rows
    dimension_id: NotRequired[str | None]
    dimension_label: NotRequired[str | None]
    code_id: NotRequired[str | None]
    code_label: NotRequired[str | None]
    code_urn: NotRequired[str | None]
    codelist_urn: NotRequired[str | None]
    parent_id: NotRequired[str | None]
    depth: NotRequired[int | None]
    # concept rows
    concept_urn: NotRequired[str | None]
    scheme_urn: NotRequired[str | None]
    concept_id: NotRequired[str | None]
    # reference-metadata rows
    set_urn: NotRequired[str | None]
    attribute_path: NotRequired[str | None]
    section_title: NotRequired[str | None]
    # present only when an embedding model is configured
    embedding: NotRequired[list[float] | None]


class Bm25HitRow(SearchRow):
    """A `SearchRow` as BM25 returns it — with its relevance score.

    ``_score`` is *required*, not optional: LanceDB always appends it to a
    full-text result. Modelling it as `NotRequired` and reading it with `[]`
    is what the checker refused, correctly — the honest fix is two result
    types rather than one with two maybe-columns, since a query knows which
    of the two it ran.
    """

    _score: float


class VectorHitRow(SearchRow):
    """A `SearchRow` as vector search returns it — with its distance.

    Lower is nearer, which is why `_dedupe_by_id` keeps the *minimum* here
    and the maximum for `Bm25HitRow`.
    """

    _distance: float


def make_schema(*, embedding_dim: int | None) -> pa.Schema:
    """Return the explicit PyArrow schema for the search table.

    Declared rather than inferred from the row dicts. Inference is right
    often enough to be misleading: PyArrow scans the whole batch, so row
    order genuinely does not matter, but a batch in which *every* row
    carries ``embedding=None`` — ``embed_codes=False``, or every codelist
    marked ``fts_only`` — infers a null-typed column, and the ANN index
    then silently never builds. Declaring the schema also guarantees the
    per-language tables share one column set instead of each
    inferring its own from whatever rows that language happened to get.

    Args:
        embedding_dim: Width of the embedding vector, or ``None`` when no
            embedding model is configured — in which case the table has
            no ``embedding`` column at all, exactly as before.

    Returns:
        The PyArrow schema every language table is created with. Declared
        rather than inferred so an all-null column keeps its type.
    """
    import pyarrow as pa  # noqa: PLC0415

    types: dict[str, pa.DataType] = {"string": pa.string(), "int64": pa.int64()}
    fields = [pa.field(name, types[kind]) for name, kind in (*_BASE_COLUMNS, *_EXTENSION_COLUMNS)]
    if embedding_dim is not None:
        # Nullable on purpose: rows opted out of embedding (embed_codes=False,
        # or an fts_only codelist policy) carry NULL here alongside embedded
        # rows in the same table.
        fields.append(pa.field(_EMBEDDING_COLUMN, pa.list_(pa.float32(), embedding_dim), nullable=True))
    return pa.schema(fields)


def _embedding_dim_from_rows(rows: list[SearchRow]) -> int | None:
    """Return the width of the first populated embedding, or ``None``.

    A fallback for callers that cannot supply the width themselves. The
    authoritative source is the embedding model — see ``add_bulk``'s
    ``embedding_dim``. Deriving it from data cannot distinguish "no model
    configured" from "a model is configured but every row opted out of
    embedding", and the latter needs a typed column.
    """
    for row in rows:
        vec = row.get(_EMBEDDING_COLUMN)
        if vec is not None:
            return len(vec)
    return None


def _to_arrow(rows: list[SearchRow], *, schema: pa.Schema | None = None) -> pa.Table:
    """Materialize *rows* as a PyArrow table under the declared schema.

    Keys absent from a row become NULL; keys absent from the schema are
    dropped. Both are intentional — the collectors pad rows to the union
    schema, and this is the backstop if one ever misses a column.

    Args:
        rows: Row dicts to materialize, one per (entity, language).
        schema: Bind to this schema instead of deriving one. Appending to
            an existing table must reuse *that table's* schema: a batch
            of rows that all happen to carry ``embedding=None`` would
            otherwise derive a schema with no embedding column and fail
            to line up with the table it is being added to.

    Returns:
        A PyArrow table holding *rows* under `make_schema`'s schema.
    """
    import pyarrow as pa  # noqa: PLC0415

    if schema is None:
        schema = make_schema(embedding_dim=_embedding_dim_from_rows(rows))
    return pa.Table.from_pylist(rows, schema=schema)


# Stopwords stripped at query time before terms are joined with AND/OR.
# Kept per language: applied as one merged set, a French stopword that is
# an English content word gets silently dropped from an English query —
# "son", "ton", "sur" and "pas" are all in the French list.
_STOPWORDS_EN: frozenset[str] = frozenset(
    {
        # English (standard Lucene/Tantivy set)
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "if",
        "in",
        "into",
        "is",
        "it",
        "no",
        "not",
        "of",
        "on",
        "or",
        "such",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "was",
        "will",
        "with",
    }
)

_STOPWORDS_FR: frozenset[str] = frozenset(
    {
        "au",
        "aux",
        "avec",
        "ce",
        "ces",
        "dans",
        "de",
        "des",
        "du",
        "elle",
        "en",
        "et",
        "eux",
        "il",
        "je",
        "la",
        "le",
        "les",
        "leur",
        "lui",
        "ma",
        "mais",
        "me",
        "même",
        "mes",
        "mon",
        "ne",
        "nos",
        "notre",
        "nous",
        "ou",
        "par",
        "pas",
        "pour",
        "qu",
        "que",
        "qui",
        "sa",
        "se",
        "ses",
        "son",
        "sur",
        "ta",
        "te",
        "tes",
        "toi",
        "ton",
        "tu",
        "un",
        "une",
        "vos",
        "votre",
        "vous",
    }
)

_STOPWORDS_BY_LANG: dict[str, frozenset[str]] = {
    "en": _STOPWORDS_EN,
    "fr": _STOPWORDS_FR,
}

#: Union of every language's stopwords. The fallback when the query's
#: language is unknown, which is the old behaviour for every language.
_STOPWORDS: frozenset[str] = _STOPWORDS_EN | _STOPWORDS_FR


def _stopwords_for(lang: str | None) -> frozenset[str]:
    """Return the stopword set for *lang*, falling back to the union."""
    if lang is None:
        return _STOPWORDS
    return _STOPWORDS_BY_LANG.get(_language_slug(lang).split("_")[0], _STOPWORDS)


def _strip_stopwords(text: str, lang: str | None = None) -> str:
    """Strip *lang*'s stopwords from *text*.

    Returns the original text unchanged when every term is a stopword.
    Tantivy removes stopwords inside the index too, in the language that
    index was built for; this is the query-side half, and it has to agree
    with it. Passing the language matters: the merged set strips French
    "son" / "ton" / "sur" / "pas" out of English queries, where they are
    ordinary content words.
    """
    stopwords = _stopwords_for(lang)
    terms = [t for t in text.split() if t.lower() not in stopwords]
    if not terms:
        return text
    return " ".join(terms)


def _dedupe_by_id(
    hits: Iterable[tuple[str, float]],
    *,
    keep: str,
) -> list[tuple[str, float]]:
    """Collapse repeated ``id`` rows, keeping the best score per id.

    Preserves the order of *first* occurrence of each id, since LanceDB
    returns rows in rank order; ``keep`` controls which score replaces a
    later duplicate (``"max"`` for BM25 relevance, ``"min"`` for vector
    distance).
    """
    best: dict[str, float] = {}
    order: list[str] = []
    for ident, score in hits:
        if ident in best:
            if (keep == "max" and score > best[ident]) or (keep == "min" and score < best[ident]):
                best[ident] = score
        else:
            best[ident] = score
            order.append(ident)
    return [(ident, best[ident]) for ident in order]


def _entity_type_clause(entity_type: str | Sequence[str] | None) -> str:
    """Build a SQL fragment constraining ``entity_type``, or empty string."""
    if entity_type is None:
        return ""
    if isinstance(entity_type, str):
        return f"entity_type = '{entity_type}'"
    values = ", ".join(f"'{v}'" for v in entity_type)
    return f"entity_type IN ({values})"


def _in_clause(column: str, values: Sequence[str] | None) -> str:
    """Build an ``IN`` fragment for *column*, or empty string when unset."""
    if not values:
        return ""
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


def _prepare_query(text: str, operator: str, lang: str | None = None) -> str:
    """Strip stopwords and join remaining terms with the given operator.

    Returns the original text unchanged for single-term queries or when
    all terms are stopwords.  Used internally and exposed for testing.
    ``lang`` selects the stopword set; omitting it uses the union of all
    of them.
    """
    terms = [t for t in text.split() if t.lower() not in _stopwords_for(lang)]
    if len(terms) <= 1:
        return text
    if operator == "and":
        return " AND ".join(terms)
    return " ".join(terms)


def _build_match_query(
    text: str, *, operator: str, fuzziness: int, lang: str | None = None
) -> MatchQuery | BooleanQuery:
    """Construct an FTS query that always includes exact matches.

    Tantivy's fuzzy query (used by LanceDB's `MatchQuery`) has
    a quirk: when ``fuzziness > 0`` is set on a query term of length
    >= 8 chars, the underlying Levenshtein automaton excludes the
    exact term itself from the result set. So a user typing
    ``search("inflation", fuzzy=1)`` would silently get zero hits
    even though "inflation" is in the index.

    Workaround: when ``fuzziness > 0``, wrap an exact ``MatchQuery``
    and the fuzzy ``MatchQuery`` in a ``BooleanQuery`` joined with
    ``SHOULD`` — both paths contribute, so exact hits always come
    back regardless of how Tantivy's fuzzy automaton behaves.
    """
    from lancedb.query import (  # noqa: PLC0415
        BooleanQuery,
        FullTextOperator,
        MatchQuery,
        Occur,
    )

    fts_op = FullTextOperator.AND if operator == "and" else FullTextOperator.OR
    cleaned = _strip_stopwords(text, lang)
    exact = MatchQuery(
        query=cleaned,
        column="text",
        operator=fts_op,
        boost=1.0,
        fuzziness=0,
        max_expansions=50,
        prefix_length=0,
        document_granularity=None,
    )
    if fuzziness == 0:
        return exact
    fuzzy = MatchQuery(
        query=cleaned,
        column="text",
        operator=fts_op,
        boost=1.0,
        fuzziness=fuzziness,
        max_expansions=50,
        prefix_length=0,
        document_granularity=None,
    )
    return BooleanQuery([(Occur.SHOULD, exact), (Occur.SHOULD, fuzzy)])


def _ann_is_quantised(tbl: lancedb.table.Table | None) -> bool:
    """True when *tbl* carries a vector index that stores approximations.

    Read from the table rather than the manifest. The manifest only began
    recording ``ann`` in version 2, so every index built before pinaxlib
    5.24.0 is
    indistinguishable there from one that never had a vector index at all
    — and those two cases want opposite answers. ``list_indices()``
    separates them exactly, at no extra cost: the table is already open.

    False when there is no vector index, which covers both a BM25-only
    catalog and a language under ``_ANN_MIN_VECTORS``. The latter is
    brute-force scanned and therefore *exact* — the last thing to warn
    about, and the one case the manifest would misread as stale.
    """
    if tbl is None:
        return False
    try:
        indices = tbl.list_indices()
    except Exception:  # noqa: BLE001
        return False
    for ix in indices:
        if list(ix.columns) == [_EMBEDDING_COLUMN]:
            # Matched on the absence of FLAT rather than the presence of
            # PQ: IVF_PQ is merely the one statcatlib used to build. IVF_SQ,
            # IVF_RQ and the quantised HNSW variants are lossy too, and a
            # future default should not slip past this check by not being
            # spelled "PQ".
            return "FLAT" not in ix.index_type.upper()
    return False


class _LanguageIndex:
    """One language's slice of the search corpus, in its own Lance table.

    Per-language tables exist so each can carry its own Tantivy tokenizer
    — French text stemmed with French rules, not English ones. The
    separation also gives each language exact BM25 document frequencies:
    with every language in one table, `N` and `avgdl` pooled across all of
    them, so a term common in English looked rarer than it was inside the
    French corpus.

    Because a table holds exactly one language, none of the search methods
    filter on ``lang`` — membership is the filter. The exception is a
    legacy single-table catalog (``filter_lang=True``), where this wraps
    the shared table and must still narrow by language.
    """

    def __init__(self, db: lancedb.DBConnection, lang: str, name: str, *, filter_lang: bool = False) -> None:
        self._db = db
        self._lang = lang
        self._name = name
        self._filter_lang = filter_lang
        self._tbl: lancedb.table.Table | None = None
        self._has_fts = False
        self._ann_index_type: str | None = None
        if name in db.list_tables().tables:
            self._tbl = db.open_table(name)
            self._has_fts = self._fts_covers_table()

    def _fts_covers_table(self) -> bool:
        """Whether the opened table already has an FTS index over every row.

        Without this, the first BM25 query in each process rebuilt the
        index with ``replace=True``: every serving worker re-indexed on
        startup, and a read-only open wrote new index files into a
        published bundle, breaking the checksums in its manifest.
        Rows added after the index was built leave it partial, and then
        a rebuild is still due.
        """
        assert self._tbl is not None  # noqa: S101
        try:
            indices = self._tbl.list_indices()
        except Exception:  # noqa: BLE001
            return False
        return any(
            i.index_type == "FTS" and list(i.columns) == ["text"] and not getattr(i, "num_unindexed_rows", 0)
            for i in indices
        )

    @property
    def table(self) -> lancedb.table.Table | None:
        return self._tbl

    @property
    def name(self) -> str:
        return self._name

    def ann_is_quantised(self) -> bool:
        """True when this table carries a *lossy* vector index.

        See `_ann_is_quantised`; this is the per-language view of it.

        Read from the table rather than the manifest. The manifest only
        began recording ``ann`` in version 2, so every index that predates
        the fix is indistinguishable there from one that never had a
        vector index at all — and those two cases want opposite answers.
        ``list_indices()`` distinguishes them exactly, at no extra cost:
        the table is already open.

        False when there is no vector index, which covers both a
        BM25-only catalog and a language under ``_ANN_MIN_VECTORS``. The
        latter is brute-force scanned and therefore *exact* — the last
        thing to warn about.
        """
        return _ann_is_quantised(self._tbl)

    @property
    def ann_index_type(self) -> str | None:
        """Vector index built for this language, or ``None`` if it scans.

        Set by `build_index`, so it reflects this process's build
        rather than what may sit on disk from an earlier one.
        """
        return self._ann_index_type

    def count_rows(self) -> int:
        return 0 if self._tbl is None else self._tbl.count_rows()

    def count_embedded(self) -> int:
        """Return how many of this table's rows actually carry a vector.

        The number that governs whether an ANN index is worth building —
        the index only ever contains these rows, however many others sit
        beside them.

        Returns 0 when the table has no ``embedding`` column at all
        (a BM25-only catalog): there is nothing to index, and pushing the
        filter down would raise rather than return zero, since Lance
        rejects a predicate naming an absent column.
        """
        if self._tbl is None or _EMBEDDING_COLUMN not in self._tbl.schema.names:
            return 0
        return self._tbl.count_rows(filter=f"{_EMBEDDING_COLUMN} IS NOT NULL")

    def drop(self) -> None:
        """Drop this language's table, ignoring a table that isn't there."""
        with contextlib.suppress(Exception):
            self._db.drop_table(self._name)
        self._tbl = None

    # -- writes --------------------------------------------------------

    def create(self, rows: list[SearchRow], embedding_dim: int | None) -> None:
        """Replace this language's table with *rows*."""
        if self._tbl is not None:
            self._db.drop_table(self._name)
        schema = make_schema(embedding_dim=embedding_dim or _embedding_dim_from_rows(rows))
        # NULL embeddings are deliberate (fts_only codelists, embed_codes=False)
        # and the schema declares the column nullable; lancedb >= 0.36 counts
        # a NULL as a wrong-width vector and rejects it under the default
        # on_bad_vectors="error". "null" keeps them NULL.
        self._tbl = self._db.create_table(self._name, data=_to_arrow(rows, schema=schema), on_bad_vectors="null")
        self._has_fts = False

    def upsert(self, rows: list[SearchRow], embedding_dim: int | None) -> None:
        """Delete-then-add *rows* by ``id``, creating the table if needed."""
        if self._tbl is None:
            self.create(rows, embedding_dim)
            return
        for row in rows:
            with contextlib.suppress(Exception):
                self._tbl.delete(f"id = '{row['id']}'")
        # on_bad_vectors="null": see create().
        self._tbl.add(_to_arrow(rows, schema=self._tbl.schema), on_bad_vectors="null")
        self._has_fts = False

    # -- indexing ------------------------------------------------------

    def build_fts_index(self) -> None:
        """Create or replace this language's Tantivy FTS index.

        ``ascii_folding`` makes BM25 accent-insensitive (a query for
        "Montreal" matches indexed "Montréal"); the tokenizer is applied
        symmetrically at index and query time, so query folding is
        automatic. All three of stemmer, stopwords and folding are pinned
        explicitly rather than left to LanceDB's defaults — the defaults
        are ``language='English'`` with ``stem=True``, which is precisely
        how French text came to be stemmed as English.
        """
        if self._tbl is None:
            return
        from lancedb.index import FTS  # noqa: PLC0415

        stemmer = _stemmer_for(self._lang)
        with contextlib.suppress(Exception):
            self._tbl.create_index(
                "text",
                replace=True,
                config=FTS(
                    language=stemmer or "English",
                    stem=stemmer is not None,
                    remove_stop_words=stemmer is not None,
                    ascii_folding=True,
                ),
            )
            self._has_fts = True

    def build_index(self, *, ann_min_vectors: int = _ANN_MIN_VECTORS) -> None:
        """Build this language's FTS index, and its ANN index if big enough.

        "Big enough" is measured in *embedded* rows, not table rows: the
        index is trained on the embedding column, so rows that carry no
        vector are training noise rather than training data.
        Below the bar the language is left to brute-force scan, which at
        that size is both faster and exact.
        """
        if self._tbl is None:
            return
        self.build_fts_index()
        if self.count_embedded() >= ann_min_vectors:
            # The column must be the embeddings column ("embedding"), not
            # LanceDB's conventional "vector"; a wrong name raises and, under
            # the suppress, silently leaves the table with no ANN index,
            # forcing every semantic query to do a brute-force scan.
            # IvfFlat is what _ANN_INDEX_TYPE records in the manifest.
            from lancedb.index import IvfFlat  # noqa: PLC0415

            with contextlib.suppress(Exception):
                self._tbl.create_index(_EMBEDDING_COLUMN, replace=True, config=IvfFlat(distance_type="cosine"))
                self._ann_index_type = _ANN_INDEX_TYPE

    # -- reads ---------------------------------------------------------

    def _where(self, *extra: str) -> str:
        parts = [p for p in extra if p]
        if self._filter_lang:
            parts.insert(0, f"lang = '{self._lang}'")
        return " AND ".join(parts)

    def bm25(
        self,
        text: str,
        *,
        columns: Sequence[str] | None,
        limit: int,
        operator: str,
        fuzziness: int,
        filters: Sequence[str],
    ) -> list[Bm25HitRow]:
        """Run a BM25 query, returning raw result rows.

        The `# pyrefly: ignore[bad-return]` below (and its twin in `vector`)
        is this module's only suppression, and is the shape sdmxlib's
        CLAUDE.md prescribes for a gradual-typing boundary: the one unsafe
        step, kept to the accessor that performs it. `to_list()` is declared `List[dict[Unknown, Unknown]]`
        — LanceDB hands back rows of the schema *this module declared* and
        says nothing about their shape. Validating 19 keys per row on every
        search to recover what `make_schema` already guarantees would cost
        more than it proves.

        Not `# type: ignore`: that form names no checker and no error kind,
        so a dead one cannot be told from a live one, and pyrefly's
        `unused-ignore` stops being trustworthy.
        """
        if self._tbl is None:
            return []
        if not self._has_fts:
            self.build_fts_index()
        query = _build_match_query(text, operator=operator, fuzziness=fuzziness, lang=self._lang)
        select_cols = ["id", "_score"] if columns is None else list(dict.fromkeys([*columns, "_score"]))
        try:
            search = self._tbl.search(query, query_type="fts")
            where = self._where(*filters)
            if where:
                search = search.where(where, prefilter=True)
            return search.limit(limit).select(select_cols).to_list()  # pyrefly: ignore[bad-return]
        except Exception:  # noqa: BLE001
            return []

    def vector(
        self,
        embedding: list[float],
        *,
        columns: Sequence[str] | None,
        limit: int,
        filters: Sequence[str],
    ) -> list[VectorHitRow]:
        """Run a vector nearest-neighbour query, returning raw result rows."""
        if self._tbl is None:
            return []
        select_cols = ["id", "_distance"] if columns is None else list(dict.fromkeys([*columns, "_distance"]))
        try:
            search = self._tbl.search(embedding)
            where = self._where(*filters)
            if where:
                search = search.where(where, prefilter=True)
            return search.limit(limit).select(select_cols).to_list()  # pyrefly: ignore[bad-return]
        except Exception:  # noqa: BLE001
            return []


class LanceIndex:
    """Router over one `_LanguageIndex` per language.

    Which table a language lives in is an index concern, so it stays
    behind this façade: callers keep passing ``lang=`` to the same
    methods and never name a table.

    Schema (identical in every language's table)
    -------------------------------------------
    id           str               FK to ``dataset.identifier``
    entity_type  str               dataset kind (``"open"``, ``"aggregate"``, …)
    lang         str               BCP-47 language code (``"en"``, ``"fr"``, …)
    text         str               concatenated title + description + keywords
    embedding    list[float]|None  vector (None when no embedding model)

    ``lang`` is retained on the row even though the table already implies
    it: hit construction reads it back, and it keeps a table
    self-describing if one is ever inspected on its own.
    """

    def __init__(self, path: str | Path) -> None:
        import lancedb  # noqa: PLC0415

        self._path = Path(path)
        self._db: lancedb.DBConnection = lancedb.connect(str(path))
        self._langs: dict[str, _LanguageIndex] = {}
        self._legacy: lancedb.table.Table | None = None
        self._generation = 0
        self._annotations: IndexAnnotations | None = None

        names = set(self._db.list_tables().tables)
        manifest = self._read_manifest()
        if manifest is not None:
            self._generation = _as_int(manifest.get("generation"))
            self._annotations = _as_annotations(manifest.get("annotations"))
            languages = manifest.get("languages")
            entries = languages.items() if isinstance(languages, Mapping) else ()
            for lang, entry in entries:
                layout = _as_layout(entry)
                # v1 wrote the table name directly; v2 wraps it in a mapping.
                table = layout["table"] if layout is not None else _as_opt_str(entry)
                if table is not None and table in names:
                    self._langs[str(lang)] = _LanguageIndex(self._db, str(lang), table)
        if not self._langs and _TABLE in names:
            # Pre-#101 catalog: one table, every language, one English
            # stemmer. Served read-only so an existing bundle keeps
            # working; build_index() replaces it with per-language tables.
            self._legacy = self._db.open_table(_TABLE)
        self._warn_if_stale()

    def _warn_if_stale(self) -> None:
        """Warn once if this index is out of date, for any reason.

        One warning rather than one per defect, because there is one
        remedy: ``build_index()`` fixes every item below in a single
        rebuild, and a caller who reads two warnings still does the same
        thing. It also keeps the two from being triaged separately when
        they are usually the same neglected bundle.

        The two conditions co-occur. An index built before pinaxlib 5.23.0
        also predates 5.24.0, so its single legacy table is stemmed as English
        *and* carries an IVF_PQ vector index; warning about the stemming
        alone would send someone to rebuild while leaving them unaware
        their vector search was also degraded.
        """
        reasons: list[str] = []

        if self._legacy is not None:
            reasons.append(
                "it predates per-language tables, so every language is stemmed "
                "as English and non-English queries match fewer documents than "
                "they should"
            )

        # Both the legacy table and the per-language ones can carry a vector
        # index from before pinaxlib 5.24.0, and exactly one of the two is
        # populated.
        stale_ann = sorted(lang for lang, index in self._langs.items() if index.ann_is_quantised())
        if _ann_is_quantised(self._legacy):
            stale_ann = ["the legacy table"]
        if stale_ann:
            reasons.append(
                f"its vector index for {', '.join(stale_ann)} is quantised, so "
                "vector and hybrid search return roughly four fifths of the "
                "correct nearest neighbours, though the stored vectors "
                "themselves are correct"
            )

        if not reasons:
            return
        detail = reasons[0] if len(reasons) == 1 else "; ".join(f"({i}) {r}" for i, r in enumerate(reasons, 1))
        warnings.warn(
            f"search index is out of date: {detail}. Rebuild with build_index() to fix.",
            stacklevel=3,
        )

    # -- manifest ------------------------------------------------------

    def _read_manifest(self) -> dict[str, object] | None:
        """Return the parsed manifest, or ``None`` when absent or corrupt.

        A manifest that cannot be parsed is treated as missing rather than
        as an error: the tables are still on disk, and refusing to open
        would strand a catalog over a file the next rebuild rewrites.
        """
        try:
            data = json.loads((self._path / _MANIFEST_FILE).read_text())
        except OSError, ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _write_manifest(self, indexes: dict[str, _LanguageIndex], generation: int) -> None:
        """Atomically point the index at *indexes*.

        Written to a temporary file and moved into place, which is atomic
        on POSIX and Windows alike. This single move is what commits a
        rebuild.

        Each entry records the stemmer the language was indexed with and
        its row count, so "which languages does this index cover, and was
        French actually stemmed as French?" is answerable by reading one
        file — including for BM25-only catalogs, which write no
        ``embeddings.json`` at all.
        """
        payload: _Manifest = {
            # A reader meeting a manifest version above its own knows the
            # index was written by a newer statcatlib and can say so. It does
            # not help statcatlib older than the manifest itself — those look
            # for a table named "search", find none, and return no hits
            # silently — which is why deploying the library ahead of any
            # bundle built with it is a hard ordering constraint, not a
            # preference.
            "version": _MANIFEST_VERSION,
            "written_by": _statcatlib_version(),
            "generation": generation,
            "languages": {
                lang: {
                    "table": index.name,
                    "rows": index.count_rows(),
                    # Embedded rows are what the vector index actually
                    # contains, and on a catalog mixing embedded and
                    # fts_only codelists the two differ by an order of
                    # magnitude. Recording both makes "why is vector
                    # search scanning?" answerable from the manifest
                    # alone.
                    "embedded": index.count_embedded(),
                    "ann": index.ann_index_type,
                    "stemmer": _stemmer_for(lang),
                }
                for lang, index in sorted(indexes.items())
            },
        }
        if self._annotations is not None:
            payload["annotations"] = self._annotations
        self._path.mkdir(parents=True, exist_ok=True)
        tmp = self._path / f"{_MANIFEST_FILE}.tmp"
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        # Path.replace is os.replace: atomic on POSIX and Windows alike.
        tmp.replace(self._path / _MANIFEST_FILE)

    # -- language routing ----------------------------------------------

    def _for(self, lang: str) -> _LanguageIndex | None:
        """Return the index serving *lang*, or ``None`` if nothing does."""
        slug = _language_slug(lang)
        if slug in self._langs:
            return self._langs[slug]
        if self._legacy is not None:
            return _LanguageIndex(self._db, slug, _TABLE, filter_lang=True)
        return None

    @property
    def annotations(self) -> IndexAnnotations | None:
        """The annotations this index was built from; ``None`` for one built before they were recorded."""
        return self._annotations

    @property
    def languages(self) -> list[str]:
        """Languages with their own table, sorted."""
        return sorted(self._langs)

    def table_for(self, lang: str = "en") -> lancedb.table.Table | None:
        """Return the Lance table serving *lang*, or ``None``.

        The escape hatch for inspecting a single language's corpus
        directly — index introspection and tests. Callers doing ordinary
        search should go through the search methods, which route by
        language themselves.
        """
        index = self._for(lang)
        return None if index is None else index.table

    def describe(self) -> dict[str, LanguageLayout]:
        """Return ``{lang: {table, rows, stemmer}}`` for the live index.

        What each language was actually indexed with, read back from the
        manifest — the thing to check when a query in one language
        behaves unlike the same query in another. A ``stemmer`` of
        ``None`` means that language is indexed unstemmed because
        LanceDB has no Snowball stemmer for it.
        """
        manifest = self._read_manifest()
        if manifest is None:
            return {}
        languages = manifest.get("languages")
        if not isinstance(languages, Mapping):
            return {}
        out: dict[str, LanguageLayout] = {}
        for lang, entry in languages.items():
            layout = _as_layout(entry)
            if layout is not None:
                out[str(lang)] = layout
        return out

    def count_rows(self) -> int:
        """Total rows across every language."""
        if self._legacy is not None and not self._langs:
            return self._legacy.count_rows()
        return sum(li.count_rows() for li in self._langs.values())

    # -- writes --------------------------------------------------------

    @staticmethod
    def _by_language(rows: list[SearchRow]) -> dict[str, list[SearchRow]]:
        grouped: dict[str, list[SearchRow]] = {}
        for row in rows:
            grouped.setdefault(_language_slug(row.get("lang", "") or ""), []).append(row)
        return grouped

    def add(self, rows: list[SearchRow], *, embedding_dim: int | None = None) -> None:
        """Upsert rows into their languages' tables, creating any that are new.

        Args:
            rows: Row dicts, each carrying its own ``lang``.
            embedding_dim: See `add_bulk`.
        """
        if not rows:
            return
        added_language = False
        for lang, lang_rows in self._by_language(rows).items():
            index = self._langs.get(lang)
            if index is None:
                # A language seen for the first time needs a table *and* a
                # manifest entry — an unreferenced table is invisible on
                # the next open, so writing one without the other would
                # silently lose these rows.
                index = _LanguageIndex(self._db, lang, f"{_TABLE_PREFIX}{lang}__{self._generation:06d}")
                self._langs[lang] = index
                added_language = True
            index.upsert(lang_rows, embedding_dim)
        if added_language:
            self._write_manifest(self._langs, self._generation)

    def add_bulk(
        self,
        rows: list[SearchRow],
        *,
        embedding_dim: int | None = None,
        annotations: IndexAnnotations | None = None,
    ) -> None:
        """Bulk-load rows, replacing every language's table.

        More efficient than per-row ``add()`` for full rebuilds — avoids
        O(n) deletes and LanceDB fragment accumulation.

        Languages absent from *rows* have their tables dropped, so a
        rebuild after a language is removed from the catalog does not
        leave a stale corpus behind to be searched.

        Args:
            rows: Row dicts to load, replacing the index's contents.
            embedding_dim: Width of the embedding vectors, taken from the
                embedding model. Supply it whenever a model is configured:
                deriving the width from the rows cannot tell "no model" from
                "a model, but every row opted out of embedding"
                (``embed_codes=False``, or every codelist marked
                ``fts_only``). That case would otherwise produce a
                null-typed embedding column, and the ANN index would
                silently never build. ``None`` falls back to scanning the
                rows, which is correct whenever at least one is embedded.
            annotations: The annotations *rows* were built from. Recorded in
                the same manifest write that commits the new generation, so
                the two cannot disagree after a crash.

        Raises:
            Exception: Whatever the underlying build raised. Every table
                created so far in this rebuild is dropped before the
                exception propagates, so a failed rebuild leaves the previous
                generation whole rather than a half-written one.
        """
        if not rows:
            return
        grouped = self._by_language(rows)
        previous = {lang: index.name for lang, index in self._langs.items()}
        generation = self._generation + 1

        # Build the whole new generation before committing to any of it.
        # Until the manifest is replaced, every query still reads the old
        # tables, so a failure here leaves the previous index whole rather
        # than half-rebuilt. LanceDB OSS has no rename_table, so the
        # generation suffix is what lets new tables coexist with the ones
        # still serving.
        built: dict[str, _LanguageIndex] = {}
        try:
            for lang, lang_rows in grouped.items():
                index = _LanguageIndex(self._db, lang, f"{_TABLE_PREFIX}{lang}__{generation:06d}")
                index.create(lang_rows, embedding_dim)
                built[lang] = index
        except Exception:
            for index in built.values():
                index.drop()
            raise

        self._annotations = annotations
        self._write_manifest(built, generation)

        # Past this point the rebuild has committed; everything below is
        # reclaiming space and must not be able to undo it.
        self._langs = built
        self._generation = generation
        for name in previous.values():
            with contextlib.suppress(Exception):
                self._db.drop_table(name)
        # A rebuild supersedes any pre-#101 table; drop it so the two
        # layouts can never both answer queries.
        if self._legacy is not None:
            with contextlib.suppress(Exception):
                self._db.drop_table(_TABLE)
            self._legacy = None

    def discard_orphans(self) -> int:
        """Drop language tables no live manifest points at.

        A crash between building a generation and committing it leaves
        those tables on disk. They are inert — nothing routes to them —
        but they hold space. Returns the number dropped.
        """
        live = {index.name for index in self._langs.values()}
        names = [n for n in self._db.list_tables().tables if n.startswith(_TABLE_PREFIX) and n not in live]
        for name in names:
            with contextlib.suppress(Exception):
                self._db.drop_table(name)
        return len(names)

    # -- full-text (BM25) search ---------------------------------------

    def build_fts_index(self) -> None:
        """Create or replace every language's FTS index."""
        for index in self._langs.values():
            index.build_fts_index()

    def bm25_search(
        self,
        text: str,
        *,
        lang: str = "en",
        limit: int = 100,
        operator: str = "or",
        fuzziness: int = 0,
        entity_type: str | Sequence[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``[(identifier, bm25_score), …]`` ordered by BM25 relevance.

        Builds the FTS index lazily on first call if not already present.
        Returns an empty list when no table serves *lang*.

        Results are deduplicated by ``id`` (best score per id wins) so a
        single dataset never appears more than once in the ranked output.

        Args:
            text: Query text, tokenised and stemmed with *lang*'s rules.
            lang: Language whose per-language table — and therefore whose stemmer — answers the
                query. Table membership *is* the language filter; see `LanceIndex`.
            limit: Maximum rows to return after dedup.
            operator: How to join multi-term queries after stopword removal. ``"or"`` (default) matches any
                term; ``"and"`` requires all terms.
            fuzziness: Levenshtein edit distance for fuzzy matching (0, 1, or 2). ``0`` (default) requires
                exact term matches.
            entity_type: Restrict matches to one or more ``entity_type`` values (e.g. ``"open"`` or
                ``("open", "aggregate")``). When ``None`` (default), all row kinds are eligible — including
                code rows.

        Returns:
            ``[(identifier, score), …]`` ordered by descending BM25 score,
            at most *limit* long. Empty when no table serves *lang*.
        """
        index = self._for(lang)
        if index is None:
            return []
        # Oversample to leave headroom for dedup; cap to keep the query bounded.
        oversample = min(limit * 4, 1000) if entity_type is None else limit * 2
        rows = index.bm25(
            text,
            columns=None,
            limit=oversample,
            operator=operator,
            fuzziness=fuzziness,
            filters=[_entity_type_clause(entity_type)],
        )
        return _dedupe_by_id(((r["id"], r["_score"]) for r in rows), keep="max")[:limit]

    def bm25_search_rows(
        self,
        text: str,
        *,
        columns: Sequence[str],
        lang: str = "en",
        limit: int = 100,
        operator: str = "or",
        fuzziness: int = 0,
        entity_type: str | Sequence[str] | None = None,
        id_filter: Sequence[str] | None = None,
        codelist_urn_filter: Sequence[str] | None = None,
        scheme_urn_filter: Sequence[str] | None = None,
        set_urn_filter: Sequence[str] | None = None,
    ) -> list[Bm25HitRow]:
        """Return full BM25 result rows ordered by relevance.

        Like `bm25_search` but yields the requested *columns*
        alongside the score (``_score`` is always included).  Used by
        ``Catalog.search_codes`` to return structured code-row data
        without a second SQL round-trip.

        Args:
            text: Query text, tokenised and stemmed with *lang*'s rules.
            columns: Lance columns to project into each result row, in addition to the score.  Always
                includes ``"_score"``.
            lang: Language whose per-language table — and therefore whose stemmer — answers the
                query. Table membership *is* the language filter; see `LanceIndex`.
            limit: Maximum rows to return after dedup.
            operator: How to join multi-term queries after stopword removal. ``"or"`` (default) matches
                any term; ``"and"`` requires all terms.
            fuzziness: Levenshtein edit distance for fuzzy matching (0, 1, or 2). ``0`` (default) requires
                exact term matches.
            entity_type: Restrict matches to one or more ``entity_type`` values. ``None`` (default) leaves
                every row kind eligible.
            id_filter: Optional whitelist of ``id`` values to restrict matches to — used to scope code-level
                search to a specific dataset set.
            codelist_urn_filter: Restrict matches to rows whose ``codelist_urn`` is in this set — the
                primitive that lets ``cat.codes.find(..., codelist_urn=GEO)`` scope a code search to one
                codelist.
            scheme_urn_filter: Restrict matches to rows whose ``scheme_urn`` is in this set — the concept-
                row analogue of ``codelist_urn_filter``.
            set_urn_filter: Restrict matches to rows whose ``set_urn`` is in this set — the
                reference-metadata analogue of ``codelist_urn_filter``.

        Returns:
            One dict per hit, carrying the requested *columns* plus
            ``_score``. Empty when no table serves *lang*.
        """
        index = self._for(lang)
        if index is None:
            return []
        return index.bm25(
            text,
            columns=columns,
            limit=limit,
            operator=operator,
            fuzziness=fuzziness,
            filters=[
                _entity_type_clause(entity_type),
                _in_clause("id", id_filter),
                _in_clause("codelist_urn", codelist_urn_filter),
                _in_clause("scheme_urn", scheme_urn_filter),
                _in_clause("set_urn", set_urn_filter),
            ],
        )

    # -- semantic (vector) search --------------------------------------

    def search(
        self,
        embedding: list[float],
        *,
        lang: str = "en",
        limit: int = 100,
        entity_type: str | Sequence[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``[(identifier, distance), …]`` ordered by vector distance.

        Returns an empty list when no table serves *lang*.

        Results are deduplicated by ``id`` (best — i.e. lowest — distance
        per id wins) so a single dataset never appears more than once in
        the ranked output.

        Args:
            embedding: Query vector, from the same model the index was built
                with. A dimension mismatch is a build-time error, not one
                caught here.
            lang: Language whose per-language table — and therefore whose stemmer — answers the
                query. Table membership *is* the language filter; see `LanceIndex`.
            limit: Maximum rows to return after dedup.
            entity_type: Restrict matches to one or more ``entity_type`` values (e.g. ``"open"`` or
                ``("open", "aggregate")``). When ``None`` (default), all row kinds are eligible — including
                code rows.

        Returns:
            ``[(identifier, distance), …]`` ordered by ascending distance,
            at most *limit* long. Empty when no table serves *lang*.
        """
        index = self._for(lang)
        if index is None:
            return []
        # Oversample to leave headroom for dedup; cap to keep the query bounded.
        oversample = min(limit * 4, 1000) if entity_type is None else limit * 2
        rows = index.vector(
            embedding,
            columns=None,
            limit=oversample,
            filters=[_entity_type_clause(entity_type)],
        )
        return _dedupe_by_id(((r["id"], r["_distance"]) for r in rows), keep="min")[:limit]

    def search_rows(
        self,
        embedding: list[float],
        *,
        columns: Sequence[str],
        lang: str = "en",
        limit: int = 100,
        entity_type: str | Sequence[str] | None = None,
        id_filter: Sequence[str] | None = None,
        codelist_urn_filter: Sequence[str] | None = None,
        scheme_urn_filter: Sequence[str] | None = None,
        set_urn_filter: Sequence[str] | None = None,
    ) -> list[VectorHitRow]:
        """Vector nearest-neighbour search returning full rows.

        Like `search` but yields the requested *columns* alongside
        ``_distance`` instead of collapsing to ``(id, distance)`` pairs.
        Used by code-level semantic search where each code row is
        distinct even when several share the same dataset ``id`` — the
        id-dedup that `search` applies would lose all but one
        code per dataset.

        Returns an empty list when no table serves *lang*.
        """
        index = self._for(lang)
        if index is None:
            return []
        return index.vector(
            embedding,
            columns=columns,
            limit=limit,
            filters=[
                _entity_type_clause(entity_type),
                _in_clause("id", id_filter),
                _in_clause("codelist_urn", codelist_urn_filter),
                _in_clause("scheme_urn", scheme_urn_filter),
                _in_clause("set_urn", set_urn_filter),
            ],
        )

    # -- index management ----------------------------------------------

    def build_index(self) -> None:
        """Build every language's FTS index, and its ANN index if big enough.

        The ANN index requires ``_ANN_MIN_VECTORS`` *embedded* rows —
        rows without a vector are not in the index and so do not count
        toward being worth building one. The threshold applies per
        language now that each has its own table, so a language with too
        few vectors is left to brute-force scan, which at that size costs
        little.

        The manifest is rewritten afterwards: which vector index each
        language ended up with is only known once the build has run, and
        ``add_bulk`` wrote its entry before that.
        """
        for index in self._langs.values():
            index.build_index()
        self._write_manifest(self._langs, self._generation)

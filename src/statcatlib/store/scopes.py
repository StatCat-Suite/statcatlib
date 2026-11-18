"""Lazy scope objects for navigating catalog structure.

Scopes represent positions in the catalog graph.  Navigation builds new
scopes without executing SQL; only terminal methods (``.collect()``,
``.count()``) hit the database.

Usage:

    store.themes["https://example.com/themes"].collect()  # ItemList[ConceptRef]
    store.themes["https://example.com/themes"]["13"].collect()  # Concept
    store.themes["https://example.com/themes"]["13"].datasets.collect()  # list[BaseDataset]

    store.themes["https://example.com/themes"].enrich(
        n=sc.each("datasets").count(),
    ).rows(lang="en")

    store.codelists.enrich(
        n=sc.each("datasets").count(),
    ).rows(lang="en")

    # DatasetScope — Gremlin-inspired graph navigation
    store.dataset("housing-stats").codelists  # CodelistsScope
    store.dataset("housing-stats").themes  # ThemesScope
    store.dataset("housing-stats").related("codelist")  # QueryBuilder[BaseDataset]
    store.dataset("housing-stats").upstream(depth=5)  # QueryBuilder[BaseDataset]
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from sdmxlib.model.codelist import Code

    from statcatlib.expr import AliasedExpr, Expr
    from statcatlib.model.dataset import BaseDataset
    from statcatlib.store.store import QueryBuilder, _Store

import attrs
import duckdb
from attrs import define, field
from sdmxlib import InternationalString, LocalisedString
from sdmxlib.model.collections import ItemList

from statcatlib._lang_sql import localized_map, pick_map
from statcatlib.labels import Gap, Labels, Langs, MissingTranslationError, RowList, UnknownCodeError, _as_langs
from statcatlib.model.skos import Concept, ConceptRef, ConceptScheme
from statcatlib.query import ExistsFilter, label_norm_sql
from statcatlib.store.row import Row
from statcatlib.store.schema import first_row


def _map_to_istring(m: dict[str, str] | None) -> InternationalString | None:
    """Convert a DuckDB MAP dict to InternationalString."""
    if m is None:
        return None
    return InternationalString.of(m)


# ---------------------------------------------------------------------------
# Edge metadata — maps (scope_type, edge_label) to SQL JOIN information
# ---------------------------------------------------------------------------


def _sdmx_codelist_count_sql(_scope_filter: dict[str, object]) -> tuple[str, list[object]]:
    """Custom COUNT subquery: datasets per codelist via SDMX JOIN."""
    return (
        "SELECT dc.codelist_urn, COUNT(DISTINCT d.identifier) AS cnt"
        " FROM statcat.dataset d"
        " JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn"
        " JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn"
        " WHERE dc.codelist_urn IS NOT NULL"
        " GROUP BY dc.codelist_urn"
    ), []


def _sdmx_codelist_exists_sql(_scope_filter: dict[str, object]) -> tuple[str, list[object]]:
    """Custom EXISTS subquery: codelists with at least one dataset via SDMX JOIN."""
    return (
        "SELECT DISTINCT dc.codelist_urn"
        " FROM statcat.dataset d"
        " JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn"
        " JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn"
        " WHERE dc.codelist_urn IS NOT NULL"
    ), []


def _sdmx_code_count_sql(scope_filter: dict[str, object]) -> tuple[str, list[object]]:
    """Custom COUNT subquery: datasets per code (within a codelist) via SDMX JOIN."""
    cl_urn = scope_filter.get("codelist_urn")
    return (
        "SELECT c.code_id, COUNT(DISTINCT d.identifier) AS cnt"
        " FROM statcat.dataset d"
        " JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn"
        " JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn"
        " JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn"
        " WHERE c.codelist_urn = ?"
        " GROUP BY c.code_id"
    ), [cl_urn]


def _sdmx_code_exists_sql(scope_filter: dict[str, object]) -> tuple[str, list[object]]:
    """Custom EXISTS subquery: codes that have at least one dataset via SDMX JOIN."""
    cl_urn = scope_filter.get("codelist_urn")
    return (
        "SELECT DISTINCT c.code_id"
        " FROM statcat.dataset d"
        " JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn"
        " JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn"
        " JOIN sdmx.code c ON c.codelist_urn = dc.codelist_urn"
        " WHERE c.codelist_urn = ?"
    ), [cl_urn]


#: A scope-scoped SQL builder: takes the scope's filter, returns
#: ``(sql, params)``. Typed rather than left as `Any` so a builder with the
#: wrong shape is a finding here, at the table, instead of a `TypeError` when
#: a particular edge is first traversed.
type _ScopeSql = Callable[[dict[str, object]], tuple[str, list[object]]]


class _BridgeEdge(TypedDict):
    """A scope that reaches datasets through a statcatlib bridge table."""

    kind: Literal["bridge"]
    near_key: str
    entity_key: str
    bridge_table: str
    far_key: str


class _SdmxEdge(TypedDict):
    """A scope whose join runs through sdmxlib's tables, so it brings its own SQL."""

    kind: Literal["sdmx"]
    near_key: str
    entity_key: str
    custom_count_sql: _ScopeSql
    custom_exists_sql: _ScopeSql


#: The two are alternatives, not one shape with optional halves — a bridge
#: edge never has a custom builder and vice versa.
#:
#: Discriminated on an explicit ``kind`` rather than on key presence. Testing
#: ``"custom_count_sql" in edge`` to conclude ``custom_exists_sql`` also exists
#: is sound given the TypedDicts, but pyrefly reports reading the second key as
#: an error, where basedpyright (run beside it until GH-7) narrowed it. Two
#: checkers disagreeing about whether a branch is safe is reason enough to say
#: which branch it is.
type _Edge = _BridgeEdge | _SdmxEdge


_EDGE_MAP: dict[str, dict[str, _Edge]] = {
    "concept": {
        "datasets": {
            "kind": "bridge",
            "bridge_table": "statcat.dataset_theme",
            "near_key": "concept_uri",  # column in the bridge table
            "entity_key": "uri",  # column in the entity table (t.uri)
            "far_key": "dataset_id",
        },
    },
    "codelist": {
        "datasets": {
            "kind": "sdmx",
            "custom_count_sql": _sdmx_codelist_count_sql,
            "custom_exists_sql": _sdmx_codelist_exists_sql,
            "near_key": "codelist_urn",
            "entity_key": "urn",
        },
    },
    "code": {
        "datasets": {
            "kind": "sdmx",
            "custom_count_sql": _sdmx_code_count_sql,
            "custom_exists_sql": _sdmx_code_exists_sql,
            "near_key": "code_id",
            "entity_key": "code_id",
        },
    },
}


# ---------------------------------------------------------------------------
# SQL compilation helpers for expressions
# ---------------------------------------------------------------------------


def _unwrap_expr(expr: Expr) -> tuple[Expr | None, dict[str, object], str]:
    """Unwrap an expression into (reduction, filter_conditions, edge_label).

    Raises TypeError/NotImplementedError for unsupported expression shapes.
    """
    from statcatlib.expr import CountExpr, ExistsExpr, FilteredExpr, TraversalExpr  # noqa: PLC0415

    current = expr
    filter_conditions: dict[str, object] = {}

    if isinstance(current, (CountExpr, ExistsExpr)):
        source = current.source
        reduction: Expr | None = current
    else:
        source = current
        reduction = None

    if isinstance(source, FilteredExpr):
        filter_conditions = source.conditions
        source = source.source

    if not isinstance(source, TraversalExpr):
        msg = f"Cannot compile expression: expected TraversalExpr root, got {type(source).__name__}"
        raise TypeError(msg)

    if source.edges != ("datasets",):
        msg = f"Only 'datasets' edge is currently supported, got {source.edges}"
        raise NotImplementedError(msg)

    return reduction, filter_conditions, "datasets"


def _resolve_edge(scope_type: str, edge_label: str) -> _Edge:
    """Look up the edge configuration for a scope type and edge label."""
    edges = _EDGE_MAP.get(scope_type)
    if edges is None:
        msg = f"Unknown scope type: {scope_type!r}"
        raise ValueError(msg)
    edge = edges.get(edge_label)
    if edge is None:
        msg = f"No {edge_label!r} edge for scope type: {scope_type!r}"
        raise ValueError(msg)
    return edge


def _compile_enrich_sql(
    expr: Expr,
    alias_name: str,
    scope_type: str,
    *,
    scope_filter: dict[str, object] | None = None,
) -> tuple[str, list[object], str]:
    """Compile a single expression into a LEFT JOIN + select column fragment.

    Returns ``(select_fragment, params, join_fragment)``.

    The ``scope_type`` selects the edge configuration from ``_EDGE_MAP``.
    ``scope_filter`` provides additional WHERE conditions on the bridge
    table (e.g. ``{"theme_scheme": "subjects"}`` or
    ``{"codelist_urn": "urn:..."}``).
    """
    from statcatlib.expr import CountExpr, ExistsExpr  # noqa: PLC0415

    scope_filter = scope_filter or {}
    reduction, filter_conditions, edge_label = _unwrap_expr(expr)
    edge = _resolve_edge(scope_type, edge_label)

    near_key = edge["near_key"]
    entity_key = edge.get("entity_key", near_key)
    sub_alias = f"_enrich_{alias_name}"

    # SDMX-joined edges have no bridge table and bring their own SQL. Branch on
    # the *edge* first, then the reduction: keyed the other way round, an SDMX
    # edge with an unsupported reduction fell past both arms into the bridge
    # path and died on `edge["bridge_table"]` with a bare KeyError, where the
    # bridge path raises a NotImplementedError naming the reduction. Same
    # unsupported input, two different failures. This ordering is also what
    # lets both checkers narrow `edge` to `_BridgeEdge` below.
    if edge["kind"] == "sdmx":
        if isinstance(reduction, CountExpr):
            subq_sql, subq_params = edge["custom_count_sql"](scope_filter)
            subquery = f"LEFT JOIN ({subq_sql}) {sub_alias} ON {sub_alias}.{near_key} = t.{entity_key}"
            select_col = f"COALESCE({sub_alias}.cnt, 0) AS {alias_name}"
            return select_col, subq_params, subquery
        if isinstance(reduction, ExistsExpr):
            subq_sql, subq_params = edge["custom_exists_sql"](scope_filter)
            subquery = f"LEFT JOIN ({subq_sql}) {sub_alias} ON {sub_alias}.{near_key} = t.{entity_key}"
            select_col = f"({sub_alias}.{near_key} IS NOT NULL) AS {alias_name}"
            return select_col, subq_params, subquery
        msg = f"Unsupported reduction: {type(reduction).__name__ if reduction else 'none'}"
        raise NotImplementedError(msg)

    # Bridge table path (for theme/concept edges)
    bridge = edge["bridge_table"]
    where_clauses: list[str] = []
    params: list[object] = []

    for col, val in scope_filter.items():
        where_clauses.append(f"bt.{col} = ?")
        params.append(val)

    far_join = ""
    for col, val in filter_conditions.items():
        far_join = f" JOIN statcat.dataset ds ON ds.identifier = bt.{edge['far_key']}"
        where_clauses.append(f"ds.{col} = ?")
        params.append(val)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    if isinstance(reduction, CountExpr):
        subquery = (
            f"LEFT JOIN ("
            f"SELECT bt.{near_key}, COUNT(*) AS cnt"
            f" FROM {bridge} bt{far_join}"
            f" WHERE {where_sql}"
            f" GROUP BY bt.{near_key}"
            f") {sub_alias} ON {sub_alias}.{near_key} = t.{entity_key}"
        )
        select_col = f"COALESCE({sub_alias}.cnt, 0) AS {alias_name}"
        return select_col, params, subquery
    if isinstance(reduction, ExistsExpr):
        subquery = (
            f"LEFT JOIN ("
            f"SELECT DISTINCT bt.{near_key}"
            f" FROM {bridge} bt{far_join}"
            f" WHERE {where_sql}"
            f") {sub_alias} ON {sub_alias}.{near_key} = t.{entity_key}"
        )
        select_col = f"({sub_alias}.{near_key} IS NOT NULL) AS {alias_name}"
        return select_col, params, subquery
    msg = f"Unsupported reduction: {type(reduction).__name__ if reduction else 'none'}"
    raise NotImplementedError(msg)


def _column_key(col_expr: str) -> str:
    """The `Row` key of a SELECT expression: ``t.id`` → ``id``, ``… AS label`` → ``label``."""
    if " AS " in col_expr.upper():
        return col_expr.rsplit(" ", 1)[-1].strip()
    return col_expr.rsplit(".", 1)[-1].strip()


def _label_rows(rows: list[Row], *, key: str, entity: str | None, field_name: str, langs: tuple[str, ...]) -> RowList:
    """Wrap label-joined rows, with a `Gap` for each row whose ``label`` is ``None``.

    Args:
        rows: Rows holding ``label``, ``label__lang`` and the *key* column.
        key: The column holding each row's key.
        entity: The entity every gap names, or ``None`` to use the key.
        field_name: The label field the gaps name.
        langs: The tags asked for.

    Returns:
        The rows and their gaps.
    """
    gaps = [
        Gap(key=r[key], entity=entity if entity is not None else str(r[key]), field=field_name, langs=langs)
        for r in rows
        if r["label"] is None
    ]
    return RowList(rows, gaps)


def _compile_enriched_query(
    scope_type: str,
    entity_sql: str,
    entity_id_col: str,
    entity_select_cols: list[str],
    scope_filter: dict[str, object],
    exprs: tuple[AliasedExpr, ...],
    conn: duckdb.DuckDBPyConnection,
    *,
    entity_params: Sequence[object] | None = None,
    where_sql: str = "",
    where_params: Sequence[object] | None = None,
) -> list[Row]:
    """Compile and execute an enriched query for any scope type.

    Args:
        scope_type: Key into ``_EDGE_MAP`` (``"theme"``, ``"codelist"``, ``"code"``).
        entity_sql: FROM clause for the entity table — ``"FROM statcat.theme t"``,
            ``"FROM sdmx.code t"``, and so on.
        entity_id_col: Column alias in ``t`` that becomes ``near_key`` for the JOIN, e.g. ``"t.id"`` for
            themes, ``"t.urn"`` for codelists.
        entity_select_cols: Columns to SELECT, e.g. ``["t.id", "t.label"]``.
        scope_filter: Extra WHERE conditions on the bridge table.
        exprs: The aliased expressions to compile.
        conn: DuckDB connection to execute on.
        entity_params: Parameters for the entity_sql (e.g. lang param for JOIN).
        where_sql: Optional WHERE clause for filtering the entity table.
        where_params: Parameters for the WHERE clause.

    Returns:
        One `Row` per entity, carrying the entity columns plus one column
        per aliased expression.

    Raises:
        ValueError: An alias names an entity column, which it would replace.
    """
    entity_params = entity_params or []
    where_params = where_params or []
    entity_keys = {_column_key(c) for c in entity_select_cols}
    for ae in exprs:
        if ae.name in entity_keys:
            msg = f"enrich alias {ae.name!r} would replace the entity column of that name; choose another alias"
            raise ValueError(msg)

    select_cols = list(entity_select_cols)
    joins: list[str] = []
    all_params: list[object] = []

    for ae in exprs:
        select_col, params, join_sql = _compile_enrich_sql(
            ae.expr,
            ae.name,
            scope_type,
            scope_filter=scope_filter,
        )
        select_cols.append(select_col)
        joins.append(join_sql)
        all_params.extend(params)

    where_clause = f" WHERE {where_sql}" if where_sql else ""

    sql = (
        f"SELECT {', '.join(select_cols)}"
        f" {entity_sql}"
        f" {''.join(f' {j}' for j in joins)}"
        f"{where_clause}"
        f" ORDER BY {entity_id_col}"
    )
    final_params = [*entity_params, *all_params, *where_params]

    rows = conn.execute(sql, final_params).fetchall()
    n_base = len(entity_select_cols)
    results = []
    for row in rows:
        item = Row()
        for i, col_expr in enumerate(entity_select_cols):
            item[_column_key(col_expr)] = row[i]
        for i, ae in enumerate(exprs):
            item[ae.name] = row[n_base + i]
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# DimensionInfo — plain data class replacing DimensionView
# ---------------------------------------------------------------------------


def _dimension_label(component_id: str, name: dict[str, str] | None) -> InternationalString:
    """A dimension's label: its concept's name in every language held.

    A dimension whose concept has no name is labelled by its ID as a code,
    under ``zxx`` (no linguistic content), never as English text (GH-94).
    """
    return InternationalString.of(name) if name else InternationalString.of({"zxx": component_id})


@define
class DimensionInfo:
    """Plain data object describing a dimension of a dataset.

    It has no lazy properties; navigate to the codelist through
    ``DimensionScope.codelist``.

    Mutable rather than frozen so it satisfies sdmxlib's ``SupportsId``,
    the bound on ``ItemList[T]`` since 0.61 — a protocol declaring a
    writable ``id``, which a frozen attrs class cannot provide. This is
    also the project convention (CLAUDE.md: domain objects use ``@define``
    and copy with ``attrs.evolve()``); the frozen spelling was the outlier.
    """

    id: str
    label: InternationalString
    position: int


# ---------------------------------------------------------------------------
# Scope classes — Themes
# ---------------------------------------------------------------------------


@define(frozen=True)
class ThemesScope:
    """Scope over all SKOS concept schemes in the catalog.

    Entry point: ``store.themes``.
    """

    _store: _Store = field(alias="store", repr=False)

    def __getitem__(self, scheme_uri: str) -> SchemeScope:
        return SchemeScope(scheme=scheme_uri, store=self._store)

    def collect(self) -> list[SchemeScope]:
        """Terminal: all concept scheme handles.

        A plain list, not an ``ItemList``: a `SchemeScope` is keyed by
        ``scheme`` URI and has no ``id``, so it never satisfied what an
        item list is for. Use ``store.themes[uri]`` to address one.
        """
        rows = self._store._cursor().execute("SELECT uri FROM statcat.concept_scheme ORDER BY uri").fetchall()
        return [SchemeScope(scheme=r[0], store=self._store) for r in rows]

    def __iter__(self) -> Iterator[SchemeScope]:
        return iter(self.collect())

    def __len__(self) -> int:
        rows = self._store._cursor().execute("SELECT COUNT(*) FROM statcat.concept_scheme").fetchall()
        row = first_row(rows)
        return row[0] if row else 0


@define(frozen=True)
class SchemeScope:
    """Scope over concepts within a single SKOS concept scheme.

    Entry point: ``store.themes["https://example.com/themes"]``.
    The ``scheme`` field is the scheme URI.
    """

    scheme: str
    _store: _Store = field(alias="store", repr=False)
    _exprs: tuple[AliasedExpr, ...] = field(default=(), repr=False)
    _filters: tuple[Expr, ...] = field(default=(), repr=False)
    _sort: tuple[tuple[Expr, bool], ...] = field(default=(), repr=False)

    @property
    def uri(self) -> str:  # noqa: D102
        return self.scheme

    def __getitem__(self, notation: str) -> ConceptScope:
        return ConceptScope(
            scheme=self.scheme,
            notation=notation,
            store=self._store,
        )

    def collect(self) -> list[ConceptRef]:
        """Terminal: materialize concepts in this scheme as ConceptRef objects.

        Use ``.rows()`` for enriched tabular results after ``.enrich()``.
        """
        if self._exprs:
            msg = ".collect() cannot be used when .enrich() is active — use .rows() instead"
            raise ValueError(msg)
        conn = self._store._cursor()
        rows = conn.execute(
            "SELECT uri, notation, label FROM statcat.concept WHERE in_scheme = ? ORDER BY notation",
            [self.scheme],
        ).fetchall()
        return [
            ConceptRef(
                uri=r[0],
                notation=r[1],
                scheme_uri=self.scheme,
                label=InternationalString.of(r[2]) if r[2] else InternationalString(),
            )
            for r in rows
        ]

    def rows(self, *, lang: Langs) -> RowList:
        """Terminal: return enriched results as ``Row`` objects (requires ``.enrich()``).

        Each row holds ``uri``, ``notation``, ``label`` (the concept's label
        in the first of *lang* it holds, or ``None``) and ``label__lang``
        (that tag), then one column per enrich alias.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            The rows, with a `Gap` for each concept whose label holds none of
            the tags.

        Raises:
            ValueError: No ``.enrich()`` was called.
        """
        langs = _as_langs(lang)
        if not self._exprs:
            msg = ".rows() requires .enrich() — call .enrich(...) first, or use .collect() for domain objects"
            raise ValueError(msg)
        pick = pick_map("t.label", langs)
        rows = _compile_enriched_query(
            scope_type="concept",
            entity_sql="FROM statcat.concept t",
            entity_id_col="t.uri",
            entity_select_cols=["t.uri", "t.notation", f"{pick.text} AS label", f"{pick.lang} AS label__lang"],
            scope_filter={},
            exprs=self._exprs,
            conn=self._store._cursor(),
            entity_params=[*pick.text_params, *pick.lang_params],
            where_sql="t.in_scheme = ?",
            where_params=[self.scheme],
        )
        gaps = [
            Gap(key=r["notation"], entity=str(r["uri"]), field="label", langs=langs) for r in rows if r["label"] is None
        ]
        return RowList(rows, gaps)

    def resolve(self) -> ConceptScheme | None:
        """Terminal: fully hydrate the scheme including all its concepts."""
        return self._store.get_concept_scheme(self.scheme)

    def count(self) -> int:
        """Terminal: number of concepts in this scheme."""
        rows = (
            self._store._cursor()
            .execute(
                "SELECT COUNT(*) FROM statcat.concept WHERE in_scheme = ?",
                [self.scheme],
            )
            .fetchall()
        )
        row = first_row(rows)
        return row[0] if row else 0

    def labels(self, *, lang: Langs) -> Labels[str]:
        """Each concept's label in the first of *lang* it holds, by notation.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            Every concept of the scheme, ordered by notation; a concept whose
            label holds none of the tags maps to ``None`` and is a gap.
        """
        langs = _as_langs(lang)
        pick = pick_map("label", langs)
        rows = (
            self._store._cursor()
            .execute(
                f"SELECT notation, uri, {pick.text}, {pick.lang} FROM statcat.concept"
                " WHERE in_scheme = ? ORDER BY notation",
                [*pick.text_params, *pick.lang_params, self.scheme],
            )
            .fetchall()
        )
        return Labels(
            ((r[0], None if r[2] is None else LocalisedString(r[2], r[3])) for r in rows),
            (Gap(key=r[0], entity=r[1], field="label", langs=langs) for r in rows if r[2] is None),
        )

    @property
    def datasets(self) -> QueryBuilder[BaseDataset]:
        """Navigate to datasets tagged with any concept in this scheme (lazy).

        Returns a ``QueryBuilder`` pre-filtered to datasets citing concepts
        from this scheme.
        """
        # Collect all concept URIs in this scheme, then filter datasets
        # that have any of them as themes.
        rows = (
            self._store._cursor()
            .execute(
                "SELECT uri FROM statcat.concept WHERE in_scheme = ?",
                [self.scheme],
            )
            .fetchall()
        )
        concept_uris = [r[0] for r in rows]

        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415

        qb = self._store.query(BaseDataset)
        if not concept_uris:
            return qb
        # ExistsFilter with a list value generates IN (...) — matches any concept in scheme
        ef = ExistsFilter(table="dataset_theme", conditions={"concept_uri": concept_uris})
        return qb.filter(ef)

    # -- Scope-creating operations (lazy, return new scope) --

    def enrich(self, *exprs: AliasedExpr, **kwargs: Expr) -> SchemeScope:
        """Add computed fields. Returns new scope (lazy)."""
        all_exprs = exprs + tuple(e.alias(k) for k, e in kwargs.items())
        return attrs.evolve(self, exprs=self._exprs + all_exprs)

    def filter(self, *exprs: Expr) -> SchemeScope:
        """Keep items where expressions are truthy. Returns new scope (lazy)."""
        return attrs.evolve(self, filters=self._filters + exprs)

    def sort_by(self, expr: Expr, *, desc: bool = False) -> SchemeScope:
        """Order items by expression. Returns new scope (lazy)."""
        return attrs.evolve(self, sort=(*self._sort, (expr, desc)))


@define(frozen=True)
class ConceptScope:
    """Scope for a single concept within a scheme, identified by notation.

    Entry point: ``store.themes["https://example.com/themes"]["13"]``.
    """

    scheme: str
    notation: str
    _store: _Store = field(alias="store", repr=False)

    def collect(self) -> Concept:
        """Terminal: materialize this concept."""
        rows = (
            self._store._cursor()
            .execute(
                """
            SELECT notation, label, alt_label, definition
            FROM statcat.concept
            WHERE in_scheme = ? AND notation = ?
            """,
                [self.scheme, self.notation],
            )
            .fetchall()
        )
        row = first_row(rows)
        if row is None:
            msg = f"Concept not found: notation={self.notation!r} in scheme {self.scheme!r}"
            raise LookupError(msg)
        return Concept(
            notation=row[0],
            label=InternationalString.of(row[1]) if row[1] else InternationalString(),
            alt_label=InternationalString.of(row[2]) if row[2] else InternationalString(),
            definition=InternationalString.of(row[3]) if row[3] else None,
        )

    @property
    def datasets(self) -> QueryBuilder[BaseDataset]:
        """Navigate to datasets tagged with this concept (lazy)."""
        # Resolve notation → URI first
        rows = (
            self._store._cursor()
            .execute(
                "SELECT uri FROM statcat.concept WHERE in_scheme = ? AND notation = ?",
                [self.scheme, self.notation],
            )
            .fetchall()
        )
        row = first_row(rows)
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415

        qb = self._store.query(BaseDataset)
        if row is None:
            return qb.filter(ExistsFilter(table="dataset_theme", conditions={"concept_uri": "__none__"}))
        return qb.filter(ExistsFilter(table="dataset_theme", conditions={"concept_uri": row[0]}))


# ---------------------------------------------------------------------------
# Scope classes — Codelists
# ---------------------------------------------------------------------------


def _codelist_rows(
    store: _Store,
    langs: tuple[str, ...],
    exprs: tuple[AliasedExpr, ...],
    where_sql: str,
    where_params: list[object],
) -> RowList:
    """Enriched codelist rows: ``urn``, ``label`` and ``label__lang``, then each alias.

    Args:
        store: The store to query.
        langs: The tags asked for, normalised.
        exprs: The enrich expressions.
        where_sql: The WHERE clause over ``t`` (``sdmx.artefact``) and ``nm``
            (the codelist's names as a MAP).
        where_params: Its parameters.

    Returns:
        The rows, with a `Gap` for each codelist whose name holds none of the
        tags; empty without an ``sdmx`` schema.
    """
    sub, sub_params = localized_map("name")
    pick = pick_map("nm.m", langs)
    try:
        rows = _compile_enriched_query(
            scope_type="codelist",
            entity_sql=f"FROM sdmx.artefact t LEFT JOIN {sub} nm ON nm.owner_urn = t.urn",
            entity_id_col="t.urn",
            entity_select_cols=["t.urn", f"{pick.text} AS label", f"{pick.lang} AS label__lang"],
            scope_filter={},
            exprs=exprs,
            conn=store._cursor(),
            entity_params=[*pick.text_params, *pick.lang_params, *sub_params],
            where_sql=where_sql,
            where_params=where_params,
        )
    except duckdb.CatalogException:
        return RowList()
    return _label_rows(rows, key="urn", entity=None, field_name="name", langs=langs)


@define(frozen=True)
class CodelistsScope:
    """Scope over all codelists in the shared structural tables.

    Entry point: ``store.codelists``.
    """

    _store: _Store = field(alias="store", repr=False)
    _exprs: tuple[AliasedExpr, ...] = field(default=(), repr=False)
    _filter_text: str | None = field(default=None, repr=False)
    _filter_langs: tuple[str, ...] = field(default=(), repr=False)

    def __getitem__(self, urn: str) -> CodelistScope:
        return CodelistScope(urn=urn, store=self._store)

    def filter(self, *, text_contains: str, lang: Langs) -> CodelistsScope:
        """Keep codelists whose name or URN contains *text_contains*. Returns new scope (lazy).

        The name is the first of *lang* it holds, matched accent-,
        punctuation- and case-insensitively; the URN as a plain
        case-insensitive substring.

        Args:
            text_contains: The text to find.
            lang: A tag, or tags in order of preference.

        Returns:
            The filtered scope.
        """
        return attrs.evolve(self, filter_text=text_contains, filter_langs=_as_langs(lang))

    def collect(self) -> list[CodelistScope]:
        """Terminal: all codelists as scope handles.

        Use ``.rows()`` for enriched tabular results after ``.enrich()``.
        A ``.filter()`` applies only to ``.rows()``.
        """
        if self._exprs:
            msg = ".collect() cannot be used when .enrich() is active — use .rows() instead"
            raise ValueError(msg)
        try:
            rows = (
                self._store._cursor()
                .execute("SELECT DISTINCT urn FROM sdmx.artefact WHERE artefact_type = 'Codelist' ORDER BY urn")
                .fetchall()
            )
        except duckdb.CatalogException:
            return []
        return [CodelistScope(urn=r[0], store=self._store) for r in rows]

    def rows(self, *, lang: Langs) -> RowList:
        """Terminal: return enriched results as ``Row`` objects (requires ``.enrich()``).

        Each row holds ``urn``, ``label`` (the codelist's name in the first
        of *lang* it holds, or ``None``) and ``label__lang`` (that tag), then
        one column per enrich alias.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            The rows, with a `Gap` for each codelist whose name holds none of
            the tags; empty without an ``sdmx`` schema.

        Raises:
            ValueError: No ``.enrich()`` was called.
        """
        langs = _as_langs(lang)
        if not self._exprs:
            msg = ".rows() requires .enrich() — call .enrich(...) first, or use .collect() for domain objects"
            raise ValueError(msg)
        where_parts: list[str] = ["t.artefact_type = 'Codelist'"]
        where_params: list[object] = []
        if self._filter_text is not None:
            # Codelist name text is matched accent- AND punctuation-insensitively
            # (Montréal ~ Montreal, Trois-Rivières ~ Trois Rivieres); the URN is
            # an ASCII identifier, left as a plain ILIKE.
            fpick = pick_map("nm.m", self._filter_langs)
            where_parts.append(
                f"({label_norm_sql(fpick.text)} ILIKE '%' || {label_norm_sql('?')} || '%' OR t.urn ILIKE ?)"
            )
            where_params.extend([*fpick.text_params, self._filter_text, f"%{self._filter_text}%"])
        return _codelist_rows(self._store, langs, self._exprs, " AND ".join(where_parts), where_params)

    def batch_labels(self, pairs: list[tuple[str, str]], *, lang: Langs) -> Labels[tuple[str, str]]:
        """Terminal: resolve labels for ``(codelist_urn, code_id)`` pairs in one query.

        Args:
            pairs: The ``(codelist_urn, code_id)`` pairs to resolve.
            lang: A tag, or tags in order of preference.

        Returns:
            Each pair's name in the first of *lang* it holds, in request
            order with duplicates collapsed; a code whose name holds none of
            the tags maps to ``None`` and is a gap.

        Raises:
            UnknownCodeError: A pair names a code its codelist does not hold,
                or there is no ``sdmx`` schema.
        """
        langs = _as_langs(lang)
        wanted = list(dict.fromkeys(pairs))
        if not wanted:
            return Labels()
        pick = pick_map("c.name", langs)
        values_rows = ", ".join("(?, ?)" for _ in wanted)
        try:
            rows = (
                self._store._cursor()
                .execute(
                    f"""
                SELECT c.codelist_urn, c.code_id, {pick.text}, {pick.lang}
                FROM sdmx.code c
                JOIN (VALUES {values_rows}) AS v(cl_urn, cd_id)
                  ON c.codelist_urn = v.cl_urn AND c.code_id = v.cd_id
                """,
                    [*pick.text_params, *pick.lang_params, *(x for pair in wanted for x in pair)],
                )
                .fetchall()
            )
        except duckdb.CatalogException:
            raise UnknownCodeError(wanted) from None
        found = {(r[0], r[1]): None if r[2] is None else LocalisedString(r[2], r[3]) for r in rows}
        if missing := [pair for pair in wanted if pair not in found]:
            raise UnknownCodeError(missing)
        return Labels(
            ((pair, found[pair]) for pair in wanted),
            (Gap(key=pair, entity=pair[0], field="name", langs=langs) for pair in wanted if found[pair] is None),
        )

    def enrich(self, *exprs: AliasedExpr, **kwargs: Expr) -> CodelistsScope:
        """Add computed fields. Returns new scope (lazy)."""
        all_exprs = exprs + tuple(e.alias(k) for k, e in kwargs.items())
        return attrs.evolve(self, exprs=self._exprs + all_exprs)

    def __iter__(self) -> Iterator[CodelistScope]:
        return iter(self.collect())

    def __len__(self) -> int:
        try:
            rows = (
                self._store._cursor()
                .execute("SELECT COUNT(DISTINCT urn) FROM sdmx.artefact WHERE artefact_type = 'Codelist'")
                .fetchall()
            )
            row = first_row(rows)
        except duckdb.CatalogException:
            return 0
        return row[0] if row else 0


@define(frozen=True)
class CodelistScope:
    """Scope for a codelist identified by URN.

    Entry point: ``store.codelist("urn:...:CL_GEO(1.0)")``
    or ``store.codelists["urn:...:CL_GEO(1.0)"]``.
    """

    urn: str
    _store: _Store = field(alias="store", repr=False)
    _exprs: tuple[AliasedExpr, ...] = field(default=(), repr=False)

    def __getitem__(self, code_id: str) -> CodeScope:
        return CodeScope(codelist_urn=self.urn, code_id=code_id, store=self._store)

    def collect(self) -> ItemList[Code]:
        """Terminal: resolve codelist and return all codes.

        Use ``.rows()`` for enriched tabular results after ``.enrich()``.
        """
        if self._exprs:
            msg = ".collect() cannot be used when .enrich() is active — use .rows() instead"
            raise ValueError(msg)
        return self._store._resolve_codelist(self.urn).codes

    def rows(self, *, lang: Langs) -> RowList:
        """Terminal: return enriched results as ``Row`` objects (requires ``.enrich()``).

        Each row holds ``code_id``, ``label`` (the code's name in the first
        of *lang* it holds, or ``None``) and ``label__lang`` (that tag), then
        one column per enrich alias.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            The rows, with a `Gap` for each code whose name holds none of the
            tags; empty without an ``sdmx`` schema.

        Raises:
            ValueError: No ``.enrich()`` was called.
        """
        langs = _as_langs(lang)
        if not self._exprs:
            msg = ".rows() requires .enrich() — call .enrich(...) first, or use .collect() for domain objects"
            raise ValueError(msg)
        pick = pick_map("t.name", langs)
        try:
            rows = _compile_enriched_query(
                scope_type="code",
                entity_sql="FROM sdmx.code t",
                entity_id_col="t.code_id",
                entity_select_cols=["t.code_id", f"{pick.text} AS label", f"{pick.lang} AS label__lang"],
                scope_filter={"codelist_urn": self.urn},
                exprs=self._exprs,
                conn=self._store._cursor(),
                entity_params=[*pick.text_params, *pick.lang_params],
                where_sql="t.codelist_urn = ?",
                where_params=[self.urn],
            )
        except duckdb.CatalogException:
            return RowList()
        return _label_rows(rows, key="code_id", entity=self.urn, field_name="name", langs=langs)

    def count(self) -> int:
        """Terminal: number of codes in this codelist."""
        cl = self._store._resolve_codelist(self.urn)
        return len(cl.codes)

    def label(self, *, lang: Langs) -> LocalisedString | None:
        """Terminal: this codelist's name in the first of *lang* it holds.

        Resolves via the sdmxlib ``localized_text`` table.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            The name with the tag that answered; ``None`` when no codelist
            has this URN or there is no ``sdmx`` schema.

        Raises:
            MissingTranslationError: The codelist exists and its name holds
                none of the tags.
        """
        langs = _as_langs(lang)
        sub, sub_params = localized_map("name")
        pick = pick_map("nm.m", langs)
        try:
            row = first_row(
                self._store._cursor()
                .execute(
                    f"""
                SELECT {pick.text}, {pick.lang}, map_keys(nm.m)
                FROM sdmx.artefact a
                LEFT JOIN {sub} nm ON nm.owner_urn = a.urn
                WHERE a.urn = ? AND a.artefact_type = 'Codelist'
                LIMIT 1
                """,
                    [*pick.text_params, *pick.lang_params, *sub_params, self.urn],
                )
                .fetchall()
            )
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        if row[0] is None:
            raise MissingTranslationError(self.urn, "name", langs, sorted(row[2] or ()))
        return LocalisedString(row[0], row[1])

    def batch_labels(self, code_ids: list[str], *, lang: Langs) -> Labels[str]:
        """Terminal: resolve the names of *code_ids* in a single SQL query.

        Args:
            code_ids: Code IDs to resolve (e.g. ``["ON", "QC", "BC"]``).
            lang: A tag, or tags in order of preference.

        Returns:
            Each code's name in the first of *lang* it holds, in request
            order with duplicates collapsed; a code whose name holds none of
            the tags maps to ``None`` and is a gap.

        Raises:
            UnknownCodeError: A code ID the codelist does not hold, or no
                ``sdmx`` schema.
        """
        langs = _as_langs(lang)
        wanted = list(dict.fromkeys(code_ids))
        if not wanted:
            return Labels()
        pick = pick_map("name", langs)
        placeholders = ", ".join("?" * len(wanted))
        try:
            rows = (
                self._store._cursor()
                .execute(
                    f"""
                SELECT code_id, {pick.text}, {pick.lang}
                FROM sdmx.code
                WHERE codelist_urn = ?
                  AND code_id IN ({placeholders})
                """,
                    [*pick.text_params, *pick.lang_params, self.urn, *wanted],
                )
                .fetchall()
            )
        except duckdb.CatalogException:
            raise UnknownCodeError([(self.urn, cid) for cid in wanted]) from None
        found = {r[0]: None if r[1] is None else LocalisedString(r[1], r[2]) for r in rows}
        if missing := [cid for cid in wanted if cid not in found]:
            raise UnknownCodeError([(self.urn, cid) for cid in missing])
        return Labels(
            ((cid, found[cid]) for cid in wanted),
            (Gap(key=cid, entity=self.urn, field="name", langs=langs) for cid in wanted if found[cid] is None),
        )

    def enrich(self, *exprs: AliasedExpr, **kwargs: Expr) -> CodelistScope:
        """Add computed fields to per-code results. Returns new scope (lazy)."""
        all_exprs = exprs + tuple(e.alias(k) for k, e in kwargs.items())
        return attrs.evolve(self, exprs=self._exprs + all_exprs)

    @property
    def datasets(self) -> QueryBuilder[BaseDataset]:
        """Navigate to datasets using this codelist (lazy)."""
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415
        from statcatlib.query import SdmxCodelistFilter  # noqa: PLC0415

        return self._store.query(BaseDataset).filter(SdmxCodelistFilter(codelist_urn=self.urn))


@define(frozen=True)
class CodeScope:
    """Scope for a single code within a codelist.

    Entry point: ``store.codelist("urn:...")["ON"]``.
    """

    codelist_urn: str
    code_id: str
    _store: _Store = field(alias="store", repr=False)

    def collect(self) -> Code:
        """Terminal: resolve this code."""
        return self._store._resolve_codelist(self.codelist_urn).codes[self.code_id]

    def dataset_count(self) -> int:
        """Terminal: number of datasets whose DSD uses the codelist containing this code."""
        try:
            rows = (
                self._store._cursor()
                .execute(
                    """
                SELECT COUNT(DISTINCT d.identifier)
                FROM statcat.dataset d
                JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                WHERE dc.codelist_urn = ?
                """,
                    [self.codelist_urn],
                )
                .fetchall()
            )
            row = first_row(rows)
        except duckdb.Error:
            return 0
        return row[0] if row else 0

    @property
    def datasets(self) -> QueryBuilder[BaseDataset]:
        """Navigate to datasets whose DSD uses the codelist containing this code (lazy)."""
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415
        from statcatlib.query import SdmxCodelistFilter  # noqa: PLC0415

        return self._store.query(BaseDataset).filter(SdmxCodelistFilter(codelist_urn=self.codelist_urn))


# ---------------------------------------------------------------------------
# Scope classes — Dimensions
# ---------------------------------------------------------------------------


@define(frozen=True)
class DimensionsScope:
    """Scope over dimensions of a dataset.

    Entry point: ``store.dimensions(ds)``.
    """

    dataflow_urn: str
    _store: _Store = field(alias="store", repr=False)

    def __getitem__(self, dim_id: str) -> DimensionScope:
        return DimensionScope(
            dataflow_urn=self.dataflow_urn,
            dimension_id=dim_id,
            store=self._store,
        )

    def collect(self) -> ItemList[DimensionInfo]:
        """Terminal: all dimensions for this dataset's DSD."""
        if not self.dataflow_urn:
            return ItemList([])
        try:
            rows = (
                self._store._cursor()
                .execute(
                    """
                SELECT dc.component_id,
                       con.name,
                       dc.position
                FROM sdmx.dataflow df
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                LEFT JOIN sdmx.concept con ON con.concept_urn = dc.concept_urn
                WHERE df.urn = ?
                  AND dc.component_type = 'Dimension'
                ORDER BY dc.position
                """,
                    [self.dataflow_urn],
                )
                .fetchall()
            )
        except duckdb.CatalogException:
            return ItemList([])
        return ItemList(
            [
                DimensionInfo(
                    id=r[0],
                    label=_dimension_label(r[0], r[1]),
                    position=r[2],
                )
                for r in rows
            ]
        )

    def count(self) -> int:
        """Terminal: number of dimensions."""
        return len(self.collect())


@define(frozen=True)
class DimensionScope:
    """Scope for a single dimension of a dataset.

    Entry point: ``store.dimensions(ds)["GEO"]``.
    """

    dataflow_urn: str
    dimension_id: str
    _store: _Store = field(alias="store", repr=False)

    @property
    def codelist(self) -> CodelistScope | None:
        """Navigate to this dimension's codelist (lazy)."""
        urn = self._store._resolve_codelist_urn(
            self.dataflow_urn,
            self.dimension_id,
        )
        return CodelistScope(urn=urn, store=self._store) if urn else None

    def collect(self) -> DimensionInfo:
        """Terminal: materialize this dimension."""
        rows = (
            self._store._cursor()
            .execute(
                """
            SELECT dc.component_id,
                   con.name,
                   dc.position
            FROM sdmx.dataflow df
            JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
            LEFT JOIN sdmx.concept con ON con.concept_urn = dc.concept_urn
            WHERE df.urn = ?
              AND dc.component_id = ?
              AND dc.component_type = 'Dimension'
            LIMIT 1
            """,
                [self.dataflow_urn, self.dimension_id],
            )
            .fetchall()
        )
        row = first_row(rows)
        if row is None:
            msg = f"Dimension not found: {self.dimension_id}"
            raise LookupError(msg)
        return DimensionInfo(
            id=row[0],
            label=_dimension_label(row[0], row[1]),
            position=row[2],
        )


# ---------------------------------------------------------------------------
# Scope classes — Dataset
# ---------------------------------------------------------------------------

_RELATED_SQL: dict[str, str] = {
    "codelist": """
        SELECT DISTINCT d2.identifier
        FROM statcat.dataset d1
        JOIN sdmx.dataflow df1 ON df1.urn = d1.sdmx_dataflow_urn
        JOIN sdmx.dsd_component dc1 ON dc1.dsd_urn = df1.dsd_urn
        JOIN sdmx.dsd_component dc2 ON dc2.codelist_urn = dc1.codelist_urn
        JOIN sdmx.dataflow df2 ON df2.dsd_urn = dc2.dsd_urn
        JOIN statcat.dataset d2 ON d2.sdmx_dataflow_urn = df2.urn
        WHERE d1.identifier = ?
          AND d2.identifier != ?
          AND dc1.codelist_urn IS NOT NULL
    """,
}


@define(frozen=True)
class DatasetScope:
    """Scope for a single dataset — enables graph navigation.

    Entry point: ``store.dataset("housing-stats")``.

    Navigation::

        ds = store.dataset("housing-stats")
        ds.codelists  # CodelistsScope
        ds.themes  # ThemesScope
        ds.related("codelist")  # list[BaseDataset]
        ds.upstream(depth=5)  # list[BaseDataset]
        ds.downstream()  # list[BaseDataset]
        ds.lineage_records()  # list[dict]
    """

    identifier: str
    _store: _Store = field(alias="store", repr=False)

    @property
    def codelists(self) -> _FilteredCodelistsScope:
        """Navigate to codelists used by this dataset (lazy).

        Resolves via the dataset's SDMX dataflow → DSD → codelist URNs.
        """
        conn = self._store._cursor()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT dc.codelist_urn
                FROM statcat.dataset d
                JOIN sdmx.dataflow df ON df.urn = d.sdmx_dataflow_urn
                JOIN sdmx.dsd_component dc ON dc.dsd_urn = df.dsd_urn
                WHERE d.identifier = ? AND dc.codelist_urn IS NOT NULL
                """,
                [self.identifier],
            ).fetchall()
        except duckdb.CatalogException:
            # No sdmx schema on this connection — no codelists to scope to.
            return _FilteredCodelistsScope(store=self._store, codelist_urns=[])
        urns = [r[0] for r in rows]
        return _FilteredCodelistsScope(store=self._store, codelist_urns=urns)

    @property
    def themes(self) -> _FilteredThemesScope:
        """Navigate to themes (concepts) assigned to this dataset (lazy)."""
        conn = self._store._cursor()
        rows = conn.execute(
            "SELECT concept_uri FROM statcat.dataset_theme WHERE dataset_id = ?",
            [self.identifier],
        ).fetchall()
        concept_uris = [r[0] for r in rows]
        return _FilteredThemesScope(store=self._store, concept_uris=concept_uris)

    def related(self, edge: str) -> QueryBuilder[BaseDataset]:
        """Datasets sharing an entity via *edge* type.

        Returns a lazy ``QueryBuilder`` — chain ``.include()``, ``.filter()``,
        ``.limit()``, ``.collect()`` etc. before materializing.

        Gremlin equivalent: ``g.V(id).out(edge).in(edge).dedup()``

        Supported edges: ``"codelist"``.

        Args:
            edge: The kind of entity the datasets share.

        Returns:
            A query over the datasets linked to this one through *edge*.

        Raises:
            ValueError: *edge* is not a supported edge type.

        Example:
            store.dataset("id").related("codelist").include("publisher").limit(10).collect()
        """
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415

        sql = _RELATED_SQL.get(edge)
        if sql is None:
            msg = f"Unknown edge type: {edge!r}. Supported: {list(_RELATED_SQL)}"
            raise ValueError(msg)
        conn = self._store._cursor()
        try:
            rows = conn.execute(sql, [self.identifier, self.identifier]).fetchall()
        except duckdb.CatalogException:
            # No sdmx schema on this connection — nothing to traverse to.
            return self._store.query(BaseDataset).from_ids([])
        identifiers = [r[0] for r in rows]
        return self._store.query(BaseDataset).from_ids(identifiers)

    def upstream(
        self,
        *,
        relationship: str | None = None,
        depth: int = 10,
    ) -> QueryBuilder[BaseDataset]:
        """Transitive ancestors via lineage (recursive CTE).

        Returns a lazy ``QueryBuilder`` — chain ``.include()``, ``.filter()``,
        ``.limit()``, ``.collect()`` etc. before materializing.

        Gremlin equivalent: ``g.V(id).repeat(in('derivedFrom')).times(depth)``

        Args:
            relationship: Follow only lineage edges of this relationship; all of them when ``None``.
            depth: How many lineage hops to follow.

        Returns:
            A query over this dataset's ancestors, to *depth* hops.

        Example:
            store.dataset("id").upstream(depth=3).include("publisher").collect()
        """
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415

        rel_clause = "AND relationship = ?" if relationship is not None else ""
        base_params: list[object] = [self.identifier, *([relationship] if relationship else [])]
        rec_params: list[object] = [depth, *([relationship] if relationship else [])]

        rows = (
            self._store._cursor()
            .execute(
                f"""
            WITH RECURSIVE ancestors(identifier, depth) AS (
                SELECT source_id, 1
                FROM statcat.lineage
                WHERE target_id = ?
                  {rel_clause}
                UNION
                SELECT l.source_id, a.depth + 1
                FROM statcat.lineage l
                JOIN ancestors a ON l.target_id = a.identifier
                WHERE a.depth < ?
                  {rel_clause}
            )
            SELECT DISTINCT identifier FROM ancestors
            """,
                base_params + rec_params,
            )
            .fetchall()
        )
        identifiers = [r[0] for r in rows]
        return self._store.query(BaseDataset).from_ids(identifiers)

    def downstream(
        self,
        *,
        relationship: str | None = None,
        depth: int = 10,
    ) -> QueryBuilder[BaseDataset]:
        """Transitive descendants via lineage (recursive CTE).

        Returns a lazy ``QueryBuilder`` — chain ``.include()``, ``.filter()``,
        ``.limit()``, ``.collect()`` etc. before materializing.

        Gremlin equivalent: ``g.V(id).repeat(out('derivedFrom')).times(depth)``

        Args:
            relationship: Follow only lineage edges of this relationship; all of them when ``None``.
            depth: How many lineage hops to follow.

        Returns:
            A query over this dataset's descendants, to *depth* hops.

        Example:
            store.dataset("id").downstream().include("publisher").collect()
        """
        from statcatlib.model.dataset import BaseDataset  # noqa: PLC0415

        rel_clause = "AND relationship = ?" if relationship is not None else ""
        base_params: list[object] = [self.identifier, *([relationship] if relationship else [])]
        rec_params: list[object] = [depth, *([relationship] if relationship else [])]

        rows = (
            self._store._cursor()
            .execute(
                f"""
            WITH RECURSIVE descendants(identifier, depth) AS (
                SELECT target_id, 1
                FROM statcat.lineage
                WHERE source_id = ?
                  {rel_clause}
                UNION
                SELECT l.target_id, dn.depth + 1
                FROM statcat.lineage l
                JOIN descendants dn ON l.source_id = dn.identifier
                WHERE dn.depth < ?
                  {rel_clause}
            )
            SELECT DISTINCT identifier FROM descendants
            """,
                base_params + rec_params,
            )
            .fetchall()
        )
        identifiers = [r[0] for r in rows]
        return self._store.query(BaseDataset).from_ids(identifiers)

    def lineage_records(
        self,
        *,
        role: str = "either",
        relationship: str | None = None,
        confidence: str | None = None,
    ) -> list[dict[str, object]]:
        """Return lineage rows involving this dataset as plain dicts."""
        wheres: list[str] = []
        params: list[object] = []

        if role == "source":
            wheres.append("source_id = ?")
            params.append(self.identifier)
        elif role == "target":
            wheres.append("target_id = ?")
            params.append(self.identifier)
        else:
            wheres.append("(source_id = ? OR target_id = ?)")
            params.extend([self.identifier, self.identifier])

        if relationship is not None:
            wheres.append("relationship = ?")
            params.append(relationship)
        if confidence is not None:
            wheres.append("confidence = ?")
            params.append(confidence)

        where_clause = "WHERE " + " AND ".join(wheres)
        rows = (
            self._store._cursor()
            .execute(
                f"""
            SELECT id, source_id, target_id, relationship, activity_label,
                   activity_type, agent_id, timestamp, confidence, notes
            FROM statcat.lineage {where_clause}
            ORDER BY timestamp NULLS LAST
            """,
                params,
            )
            .fetchall()
        )
        cols = [
            "id",
            "source_id",
            "target_id",
            "relationship",
            "activity_label",
            "activity_type",
            "agent_id",
            "timestamp",
            "confidence",
            "notes",
        ]
        return [dict(zip(cols, row, strict=True)) for row in rows]


# ---------------------------------------------------------------------------
# Internal helper scopes for DatasetScope navigation
# ---------------------------------------------------------------------------


@define(frozen=True)
class _FilteredCodelistsScope:
    """CodelistsScope filtered to specific codelist URNs."""

    _store: _Store = field(alias="store", repr=False)
    codelist_urns: list[str] = field(factory=list)
    _exprs: tuple[AliasedExpr, ...] = field(default=(), repr=False)

    def __getitem__(self, urn: str) -> CodelistScope:
        return CodelistScope(urn=urn, store=self._store)

    def collect(self) -> list[CodelistScope]:
        """Terminal: materialize codelists as scope handles.

        Use ``.rows()`` for enriched tabular results after ``.enrich()``.
        """
        if self._exprs:
            msg = ".collect() cannot be used when .enrich() is active — use .rows() instead"
            raise ValueError(msg)
        if not self.codelist_urns:
            return []
        return [CodelistScope(urn=urn, store=self._store) for urn in self.codelist_urns]

    def rows(self, *, lang: Langs) -> RowList:
        """Terminal: return enriched results as ``Row`` objects (requires ``.enrich()``).

        As `CodelistsScope.rows`, over these codelists.

        Args:
            lang: A tag, or tags in order of preference.

        Returns:
            The rows and their gaps.

        Raises:
            ValueError: No ``.enrich()`` was called.
        """
        langs = _as_langs(lang)
        if not self._exprs:
            msg = ".rows() requires .enrich() — call .enrich(...) first, or use .collect() for domain objects"
            raise ValueError(msg)
        if not self.codelist_urns:
            return RowList()
        placeholders = ", ".join("?" * len(self.codelist_urns))
        return _codelist_rows(
            self._store,
            langs,
            self._exprs,
            f"t.artefact_type = 'Codelist' AND t.urn IN ({placeholders})",
            list(self.codelist_urns),
        )

    def enrich(self, *exprs: AliasedExpr, **kwargs: Expr) -> _FilteredCodelistsScope:
        """Add computed fields. Returns new scope (lazy)."""
        all_exprs = exprs + tuple(e.alias(k) for k, e in kwargs.items())
        return attrs.evolve(self, exprs=self._exprs + all_exprs)

    def __iter__(self) -> Iterator[CodelistScope]:
        return iter(self.collect())

    def __len__(self) -> int:
        return len(self.codelist_urns)


@define(frozen=True)
class _FilteredThemesScope:
    """ThemesScope filtered to specific concept URIs."""

    _store: _Store = field(alias="store", repr=False)
    concept_uris: list[str] = field(factory=list)

    def collect(self) -> list[ConceptRef]:
        """Terminal: materialize concepts as ConceptRef objects."""
        if not self.concept_uris:
            return []
        placeholders = ", ".join("?" * len(self.concept_uris))
        rows = (
            self._store._cursor()
            .execute(
                f"""
            SELECT c.uri, c.notation, c.in_scheme, c.label
            FROM statcat.concept c
            WHERE c.uri IN ({placeholders})
            ORDER BY c.notation
            """,
                self.concept_uris,
            )
            .fetchall()
        )
        # Return refs for materialized concepts; fall back to bare URI for unknowns
        found_uris = {r[0] for r in rows}
        result: list[ConceptRef] = [
            ConceptRef(
                uri=r[0],
                notation=r[1],
                scheme_uri=r[2],
                label=InternationalString.of(r[3]) if r[3] else InternationalString(),
            )
            for r in rows
        ]
        # Append bare refs for concept URIs not in the concept table
        result.extend(ConceptRef(uri=uri) for uri in self.concept_uris if uri not in found_uris)
        return result

    def __iter__(self) -> Iterator[ConceptRef]:
        return iter(self.collect())

    def __len__(self) -> int:
        return len(self.concept_uris)


# ---------------------------------------------------------------------------
# Public aliases — SKOS-named equivalents for ThemesScope / SchemeScope
# ---------------------------------------------------------------------------

#: Alias: scope over all SKOS concept schemes. Prefer this name in new code.
ConceptSchemesScope = ThemesScope

#: Alias: scope for a single SKOS concept scheme. Prefer this name in new code.
ConceptSchemeScope = SchemeScope

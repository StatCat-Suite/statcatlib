"""Composable filter objects for Catalog queries.

Usage:

    import statcatlib.query as q

    results = (
        store.query(sc.AggregateDataset)
        .filter(q.has_code("GEO", "ON"))
        .filter(q.has_dimensions(["GEO", "SEX", "AGE"]))
        .execute()
    )

    results = store.query(sc.BaseDataset).filter(q.distribution(format="CSV")).execute()

Filter objects are passed as positional arguments to ``QueryBuilder.filter()``.
Multiple positional filters are ANDed together.  Within a single
``distribution()`` call, all keyword arguments must match the *same*
distribution row (AND semantics).  Two separate ``distribution()`` calls
require *two* matching distribution rows.

Predicate compilation
---------------------

Each filter implements ``to_sql(ctx)`` returning ``(fragment, params)``.
The ``ctx`` is a `CompileContext` (extended in ``store.store`` as
``_QueryContext`` with a WHERE accumulator on top of the shared join
state).

statcatlib owns `CompileContext`. Since sdmxlib 0.50, a downstream library
passes sdmxlib a `SqlRelation`, never a predicate, so no compile state is
shared between the two.
"""

from __future__ import annotations

from typing import override

import sdmxlib as sl
from attrs import define, field

#: What a caller may pass as ``hierarchy=``. Spelled out rather than left as
#: `Any`: `str | Any` collapses to `Any`, so the union it looked like it was
#: documenting was never checked. `SupportsUrn` is sdmxlib's protocol for
#: "has a maintainable URN", which is exactly what `_hierarchy_urn` requires
#: of the third arm — a Protocol rather than a concrete class, so a future
#: artefact type satisfies it without a change here.
type HierarchyLike = str | sl.Ref[sl.Hierarchy] | sl.SupportsUrn

__all__ = [
    "CodeDescent",
    "ExistsFilter",
    "FieldFilter",
    "MapCondition",
    "MapFilter",
    "PositionalFilter",
    "SdmxCodeFilter",
    "SdmxCodelistFilter",
    "SdmxDimensionFilter",
    "descendants_of",
    "description_contains",
    "distribution",
    "has_code",
    "has_dimensions",
    "has_keyword",
    "has_service",
    "has_spatial",
    "has_spatial_uri",
    "publishes",
    "serves",
    "title_contains",
    "within",
]


@define
class CompileContext:
    """State threaded through one compilation pass.

    Each filter's ``to_sql(ctx)`` returns its own WHERE fragment and
    parameters; the context carries what has to be *shared* across the
    filters in a single query — most importantly joins, so a second
    filter touching the same bridge table reuses the first one's alias
    instead of emitting a duplicate.

    Attributes:
        row_alias: The main table's alias in the outer query — ``"d"``
            for ``statcat.dataset d``, ``"c"`` for ``sdmx.code c``.
        join_aliases: Dedup map for repeated joins. Keyed by an
            implementation-defined join id (annotation type, bridge
            table + dimension); the value is the alias assigned the
            first time that join was added.
        join_sqls: Accumulated JOIN fragments, spliced between ``FROM``
            and ``WHERE`` in the outer query.
        join_params: Parameters for those joins, in insertion order.
        schemas: Logical-to-physical schema name map. Identity by
            default (``"sdmx"`` -> ``"sdmx"``); override to remap.
        registry: The catalog's co-located `sdmxlib.LocalRegistry`, when
            one is open. Filters that delegate to sdmxlib's path language
            build their relation through it — see `SdmxCodeFilter`. Left
            ``None`` by callers that compile filters without a catalog
            (the unit tests do), which is why it is optional rather than
            required: only the descent path needs it, and it says so.
    """

    row_alias: str
    join_aliases: dict[str, str] = field(factory=dict)
    join_sqls: list[str] = field(factory=list)
    join_params: list[object] = field(factory=list)
    schemas: dict[str, str] = field(factory=dict)
    registry: sl.LocalRegistry | None = None
    _alias_counter: int = 0

    def schema(self, logical: str) -> str:
        """Resolve a logical schema name to its physical SQL identifier."""
        return self.schemas.get(logical, logical)

    def next_alias(self, prefix: str = "f") -> str:
        """Return a fresh ``{prefix}{n}`` alias, unique within this pass.

        Correlated subqueries would otherwise collide when two filters
        target the same table. The counter is per-context, so aliases
        are deterministic within a query and renumber for the next one.
        Prefixes like ``"ef"`` (exists filter) keep the SQL readable.
        """
        self._alias_counter += 1
        return f"{prefix}{self._alias_counter}"


@define(frozen=True)
class MapCondition:
    """A condition on a MAP(VARCHAR, VARCHAR) column.

    Used inside ``ExistsFilter.conditions`` when the bridge-table column
    is a DuckDB MAP rather than a scalar.  ``element_at(col, key)`` is
    emitted in the SQL instead of a plain ``col = ?``.
    """

    key: str  # language key, e.g. "en"
    value: str  # value to match


# Accent- AND punctuation-insensitive label matching. ``strip_accents`` folds
# diacritics; the regexp collapses runs of Unicode punctuation + separators
# (``\p{P}\p{Z}`` + whitespace) to one space, so "Trois Rivieres" matches
# "Trois-Rivières". ``\p{P}\p{Z}`` keeps letters of any script (Latin,
# syllabics, …) — unlike a naive ``[^a-zA-Z0-9]`` — and strips ILIKE wildcards
# (``%`` / ``_`` are punctuation), so the bound value needs no LIKE-escaping.
_LABEL_NORM_REGEX = r"[\p{P}\p{Z}\s]+"


def label_norm_sql(expr: str) -> str:
    """Wrap a SQL ``expr`` in the accent + punctuation fold used for label search."""
    return f"trim(regexp_replace(strip_accents({expr}), '{_LABEL_NORM_REGEX}', ' ', 'g'))"


@define(frozen=True)
class MapFilter:
    """Filter on a MAP column of the main ``dataset`` table.

    Unlike ``ExistsFilter`` (which targets bridge tables via a subquery),
    ``MapFilter`` adds a direct WHERE clause against ``d.<column>``.
    """

    column: str  # "title", "description"
    key: str  # language key, e.g. "en"
    value: str  # raw search text (ilike) or exact value (eq)
    op: str = "eq"  # "eq" | "ilike"

    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        """Return ``(sql_fragment, params)`` for a WHERE clause."""
        del ctx  # MapFilter targets the main row directly; no subquery alias needed
        if self.op == "ilike":
            # Accent- AND punctuation-insensitive contains: fold diacritics and
            # collapse punctuation/whitespace on both sides, so "Trois Rivieres"
            # matches "Trois-Rivières" and "Montreal" matches "Montréal".
            # ``value`` is the raw search text. (eq stays exact for identity.)
            col = label_norm_sql(f"d.{self.column}[?]")
            val = label_norm_sql("?")
            return f"{col} ILIKE '%' || {val} || '%'", [self.key, self.value]
        return f"d.{self.column}[?] = ?", [self.key, self.value]


@define
class FieldFilter:
    """A simple equality/containment filter on a flat column."""

    field: str
    op: str  # "eq" | "in" | "like"
    value: object


@define
class ExistsFilter:
    """A correlated EXISTS subquery filter.

    Instances are created by the helper functions below, not directly.

    Attributes:
        table: The table to run the EXISTS subquery against.
        conditions: Column → value pairs that must all be satisfied (AND).
        join: Optional extra JOIN clause inside the subquery (e.g. to reach data_service through
            distributions).
        group_count: When set, appends a ``GROUP BY / HAVING COUNT(DISTINCT ...) = n`` clause for "all-of"
            semantics (used by ``has_dimensions``).
    """

    table: str
    conditions: dict[str, object] = field(factory=dict)
    join: str | None = None
    group_count: tuple[str, int] | None = None  # (column, expected_count)

    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        """Return (sql_fragment, params) for a correlated EXISTS clause.

        ``alias_prefix`` is used to avoid table-alias collisions when
        multiple ExistsFilters are combined in one query.
        """
        alias_prefix = ctx.next_alias("ef")
        table_alias = f"{alias_prefix}_t"
        join_sql = f" {self.join}" if self.join else ""

        col_clauses: list[str] = [f"{table_alias}.dataset_id = d.identifier"]
        params: list[object] = []

        for col, val in self.conditions.items():
            if isinstance(val, MapCondition):
                col_clauses.append(f"{table_alias}.{col}[?] = ?")
                params.extend([val.key, val.value])
            elif val is None:
                col_clauses.append(f"{table_alias}.{col} IS NULL")
            elif isinstance(val, (list, tuple)):
                placeholders = ", ".join("?" * len(val))
                col_clauses.append(f"{table_alias}.{col} IN ({placeholders})")
                params.extend(val)
            else:
                col_clauses.append(f"{table_alias}.{col} = ?")
                params.append(val)

        where_sql = " AND ".join(col_clauses)

        if self.group_count is not None:
            group_col, expected = self.group_count
            group_sql = f" GROUP BY {table_alias}.dataset_id HAVING COUNT(DISTINCT {table_alias}.{group_col}) = ?"
            params.append(expected)
        else:
            group_sql = ""

        sql = f"EXISTS (SELECT 1 FROM {self.table} {table_alias}{join_sql} WHERE {where_sql}{group_sql})"
        return sql, params


# ---------------------------------------------------------------------------
# Internal: no-op filter (matches everything — used for empty dimension lists)
# ---------------------------------------------------------------------------


@define
class _NoOpFilter(ExistsFilter):
    """A filter that never restricts results."""

    table: str = ""

    @override
    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        del ctx
        return "1=1", []


# ---------------------------------------------------------------------------
# Public filter constructors
# ---------------------------------------------------------------------------


@define(frozen=True)
class SdmxCodelistFilter:
    """Filter datasets whose SDMX DSD uses *codelist_urn* in any dimension.

    JOINs through ``sdmx.dataflow → sdmx.dsd_component``.
    """

    codelist_urn: str

    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        """Return (sql_fragment, params) for a correlated EXISTS clause."""
        alias_prefix = ctx.next_alias("ef")
        df = f"{alias_prefix}_df"
        comp = f"{alias_prefix}_comp"
        return (
            f"EXISTS ("
            f"SELECT 1 FROM sdmx.dataflow {df}"
            f" JOIN sdmx.dsd_component {comp} ON {comp}.dsd_urn = {df}.dsd_urn"
            f"   AND {comp}.codelist_urn = ?"
            f" WHERE {df}.urn = d.sdmx_dataflow_urn"
            f")"
        ), [self.codelist_urn]


@define(frozen=True)
class CodeDescent:
    """Descent over a code tree, as an argument to `has_code`.

    Built by `descendants_of`. Carries *what* to match, never how to
    match it — the SQL is `SdmxCodeFilter`'s, and the tree-walking is
    sdmxlib's.

    Attributes:
        root: The ancestor code id (e.g. ``"CA"``).
        hierarchy_urn: A Hierarchy URN to descend. ``None`` resolves the
            applicable Hierarchy per dataflow, and falls back to the
            codelist's own parent chain where none applies.
        include_self: Whether *root* matches itself. Strict by default,
            matching sdmxlib's ``q.hcode``, whose ``ancestors`` excludes
            the node itself.
    """

    root: str
    hierarchy_urn: str | None = None
    include_self: bool = False


@define(frozen=True)
class SdmxCodeFilter:
    """Filter datasets whose SDMX DSD has *dimension* with *code* in its codelist.

    JOINs through ``sdmx.dataflow -> sdmx.dsd_component -> sdmx.code``.
    Only matches ``AggregateDataset`` records with ``sdmx_dataflow_urn`` set.

    ``code`` is either a literal code id (exact match) or a `CodeDescent`
    from `descendants_of`, which matches any code under a root.

    Descent delegates to ``sl.q.hierarchy_applicability`` — sdmxlib's view
    answering "which hierarchy applies to this component, in this
    dataflow's context", with scoped associations outranking unscoped ones
    at ``rank == 1``. The projected relation is a `sdmxlib.SqlRelation`,
    spliced here as a ``LEFT JOIN`` and matched on ``dataflow_urn``. It
    stays lazy: the descendant set is never enumerated client-side, and
    neither is the candidate dataflow list.

    What statcatlib still decides is the *fallback*. The applicability view has
    a row only where a HierarchyAssociation exists; where none does, this
    filter reads the codelist's intrinsic parent chain from
    ``sdmx.code.ancestor_ids``. That is a statcatlib policy — "when nothing
    says otherwise, use the codelist's own tree" — rather than an SDMX
    rule, which is why it lives here and the rest does not.
    """

    dimension: str
    code: str | CodeDescent
    codelist_urn: str | None = None

    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        """Return (sql_fragment, params) for a correlated EXISTS clause."""
        alias_prefix = ctx.next_alias("ef")
        df = f"{alias_prefix}_df"
        comp = f"{alias_prefix}_comp"
        c = f"{alias_prefix}_c"
        params: list[object] = [self.dimension]
        urn_clause = ""
        if self.codelist_urn is not None:
            urn_clause = f" AND {comp}.codelist_urn = ?"
            params.append(self.codelist_urn)

        if isinstance(self.code, str):
            code_clause, code_params, extra_joins = f"{c}.code_id = ?", [self.code], ""
        else:
            code_clause, code_params, extra_joins = self._compile_descent(
                self.code, ctx, df_alias=df, c_alias=c, alias_prefix=alias_prefix
            )

        return (
            f"EXISTS ("
            f"SELECT 1 FROM sdmx.dataflow {df}"
            f" JOIN sdmx.dsd_component {comp} ON {comp}.dsd_urn = {df}.dsd_urn"
            f"   AND {comp}.component_id = ?"
            f"{urn_clause}"
            f" JOIN sdmx.code {c} ON {c}.codelist_urn = {comp}.codelist_urn"
            f"{extra_joins}"
            f" WHERE {df}.urn = d.sdmx_dataflow_urn"
            f"   AND ({code_clause})"
            f")"
        ), [*params, *code_params]

    def _compile_descent(
        self,
        descent: CodeDescent,
        ctx: CompileContext,
        *,
        df_alias: str,
        c_alias: str,
        alias_prefix: str,
    ) -> tuple[str, list[object], str]:
        """Compile a `CodeDescent` to ``(code_clause, params, extra_joins)``.

        ``extra_joins`` are two ``LEFT JOIN``s against sdmxlib's applicability
        relation; they must land between the ``sdmx.code`` join and the WHERE,
        because the clause reads their columns.

        **The descendant set is joined on, never scanned.** The obvious
        spelling — join the relation on ``dataflow_urn`` and test
        ``list_contains(codes, code_id)`` — makes the match a linear scan of
        the descendant list once per (dataflow x code) row, which is a nested
        loop wearing a join's clothes. At 100 dataflows over a 3k-code
        geography that measured ~900M element comparisons and 7s, against
        49ms for the shape below. ``unnest`` gives one row per pair, and the
        equality on ``code_id`` lets DuckDB hash-join it.

        That costs a second relation. Unnesting drops any dataflow whose
        descendant list is empty, so "an association applies but nothing
        matched" would become indistinguishable from "no association applies"
        and fall through to the parent chain — the wrong answer, and the one
        the divergent fixture in the tests exists to catch. The existence
        relation carries that distinction; it repeats the applicability query
        without the descent, which is the cheap half.

        Parameter order follows SQL text order: each join's parameters are
        emitted where the join is, ahead of the WHERE fragment's.
        """
        if ctx.registry is None:
            msg = (
                "descendants_of() needs the catalog's sdmxlib registry to resolve "
                "which hierarchy applies. Compile this filter through an open "
                "Catalog (store.query(...).filter(...)), not a bare CompileContext."
            )
            raise ValueError(msg)

        ha = f"{alias_prefix}_ha"
        hr = f"{alias_prefix}_hr"
        a, hcode = sl.q.hierarchy_applicability, sl.q.hcode

        # Strict descent by default: `ancestors` excludes the node itself,
        # so self-inclusion is a second disjunct rather than a wider walk.
        under = hcode.ancestors.any(hcode.id == descent.root)
        if descent.include_self:
            under = under | (hcode.id == descent.root)

        applicable = ctx.registry.query(a).filter((a.rank == 1) & (a.component.id == self.dimension))
        if descent.hierarchy_urn is not None:
            applicable = applicable.filter(a.hierarchy.urn == descent.hierarchy_urn)

        exists_rel = applicable.select(dataflow_urn=a.dataflow.urn).to_sql()
        pairs_rel = applicable.select(
            dataflow_urn=a.dataflow.urn,
            codes=a.hierarchy.hcodes.code.id.list(where=under),
        ).to_sql()

        extra_joins = (
            f" LEFT JOIN ({exists_rel.sql}) {ha} ON {ha}.dataflow_urn = {df_alias}.urn"
            f" LEFT JOIN (SELECT dataflow_urn, unnest(codes) AS code_id FROM ({pairs_rel.sql})) {hr}"
            f"   ON {hr}.dataflow_urn = {df_alias}.urn AND {hr}.code_id = {c_alias}.code_id"
        )

        # No applicable Hierarchy: the codelist's own parent chain, which
        # the storage writer has already flattened into `ancestor_ids`.
        fallback = f"? = ANY({c_alias}.ancestor_ids)"
        fallback_params: list[object] = [descent.root]
        if descent.include_self:
            fallback = f"({fallback} OR {c_alias}.code_id = ?)"
            fallback_params.append(descent.root)

        code_clause = f"CASE WHEN {ha}.dataflow_urn IS NOT NULL THEN {hr}.code_id IS NOT NULL ELSE {fallback} END"
        params = [*exists_rel.params, *pairs_rel.params, *fallback_params]
        return code_clause, params, extra_joins


@define(frozen=True)
class SdmxDimensionFilter:
    """Filter datasets whose SDMX DSD has ALL of the listed dimensions.

    JOINs through ``sdmx.dataflow → sdmx.dsd_component``.
    Only matches ``AggregateDataset`` records with ``sdmx_dataflow_urn`` set.
    """

    dimensions: tuple[str, ...]

    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        """Return (sql_fragment, params) for a correlated EXISTS clause."""
        alias_prefix = ctx.next_alias("ef")
        df = f"{alias_prefix}_df"
        comp = f"{alias_prefix}_comp"
        placeholders = ", ".join("?" * len(self.dimensions))
        params: list[object] = [*self.dimensions, len(self.dimensions)]
        return (
            f"EXISTS ("
            f"SELECT 1 FROM sdmx.dataflow {df}"
            f" JOIN sdmx.dsd_component {comp} ON {comp}.dsd_urn = {df}.dsd_urn"
            f"   AND {comp}.component_id IN ({placeholders})"
            f" WHERE {df}.urn = d.sdmx_dataflow_urn"
            f" GROUP BY {df}.urn"
            f" HAVING COUNT(DISTINCT {comp}.component_id) = ?"
            f")"
        ), params


# Union of all filter types accepted as positional arguments to QueryBuilder.filter().
# All members implement to_sql(alias_prefix: str) -> tuple[str, list[object]].
PositionalFilter = ExistsFilter | SdmxCodeFilter | SdmxCodelistFilter | SdmxDimensionFilter


def descendants_of(
    root: str,
    *,
    hierarchy: HierarchyLike | None = None,
    include_self: bool = False,
) -> CodeDescent:
    """Match any code beneath *root* in a code tree.

    Pass the result to `has_code`. `within` is the one-call shortcut and
    is usually what you want::

        q.has_code("REF_AREA", q.descendants_of("CA"))
        q.within("REF_AREA", "CA")  # the same filter

    Args:
        root: The ancestor code id (e.g. ``"CA"``).
        hierarchy: A Hierarchy URN string, `sdmxlib.Hierarchy`, or ``Ref[Hierarchy]`` to descend. ``None``
            resolves the applicable Hierarchy per dataflow through ``sdmx.hierarchy_applicability``, falling
            back to the codelist's own parent chain where none applies.
        include_self: Whether *root* matches itself. Strict by default, matching sdmxlib's
            ``q.hcode.ancestors``, which excludes the node itself.

    Returns:
        A `CodeDescent` carrying *root*, the resolved hierarchy URN and
        the self-inclusion flag. Pass it to `has_code`.
    """
    return CodeDescent(
        root=root,
        hierarchy_urn=_hierarchy_urn(hierarchy),
        include_self=include_self,
    )


def _hierarchy_urn(hierarchy: HierarchyLike | None) -> str | None:
    """Coerce a URN string, ``Ref[Hierarchy]``, or Hierarchy to its URN."""
    if hierarchy is None or isinstance(hierarchy, str):
        return hierarchy
    if isinstance(hierarchy, sl.Ref):
        return str(hierarchy.urn)
    return str(sl.artefact_urn(hierarchy))


def has_code(
    dimension: str,
    code: str | CodeDescent,
    *,
    codelist_urn: str | None = None,
) -> SdmxCodeFilter:
    """Filter datasets whose SDMX DSD has *dimension* with *code* in its codelist.

    JOINs through ``sdmx.dataflow -> sdmx.dsd_component -> sdmx.code``.
    Only applies to ``AggregateDataset`` records ingested via SDMX.

    Args:
        dimension: The dimension ID to match (e.g. ``"GEO"``).
        code: Either a literal code id for exact match (e.g. ``"ON"``) **or** a `CodeDescent` from
            `descendants_of`, which matches any code under a root. Descent stays server-side: the descendant
            set is never enumerated first.
        codelist_urn: Optional codelist URN to disambiguate dimensions that share the same ID across
            different DSDs.  When omitted, matches any codelist.

    Examples:
        Exact match — find datasets that use the ``"ON"`` code on ``GEO``::

            store.query(sc.AggregateDataset).filter(q.has_code("GEO", "ON"))

        Hierarchy descent against an explicit Hierarchy — find datasets that
        use *any* descendant of ``"CA"`` in ``H_GEO_ADMIN``::

            store.query(sc.AggregateDataset).filter(
                q.has_code("REF_AREA", q.descendants_of("CA", hierarchy=h_admin))
            )

        Default hierarchy resolution — when ``hierarchy=`` is omitted, the
        applicable Hierarchy is resolved per dataflow via
        ``sdmx.hierarchy_applicability`` (scoped associations outrank
        unscoped ones), falling back to the codelist's parent chain when no
        association applies::

            store.query(sc.AggregateDataset).filter(q.has_code("REF_AREA", q.descendants_of("CA")))

    Returns:
        An `SdmxCodeFilter` for `QueryBuilder.filter`.
    """
    return SdmxCodeFilter(dimension=dimension, code=code, codelist_urn=codelist_urn)


def within(
    dimension: str,
    root_code: str,
    *,
    hierarchy: HierarchyLike | None = None,
    include_self: bool = False,
    codelist_urn: str | None = None,
) -> SdmxCodeFilter:
    """Filter datasets whose DSD has *dimension* with a code beneath *root_code*.

    The common geography-descent case, in one call. Equivalent to::

        q.has_code(
            dimension,
            q.descendants_of(root_code, hierarchy=hierarchy, include_self=include_self),
            codelist_urn=codelist_urn,
        )

    When ``hierarchy`` is ``None``, the applicable Hierarchy is resolved
    per dataflow via ``sdmx.hierarchy_applicability`` (scoped associations
    outrank unscoped ones), falling back to the codelist's intrinsic
    parent chain when no association applies.

    Args:
        dimension: The dimension ID (e.g. ``"REF_AREA"``).
        root_code: The ancestor code id (e.g. ``"ON"``).
        hierarchy: A Hierarchy URN string, `sdmxlib.Hierarchy`, or ``Ref[Hierarchy]``. ``None`` triggers
            per-dataflow resolution.
        include_self: Whether *root_code* matches itself. **Strict by default** — a codelist containing
            ``ON`` and nothing under it does not match ``within("REF_AREA", "ON")``. This matches sdmxlib's
            ``q.hcode.ancestors``, which excludes the node itself.
        codelist_urn: Optional codelist URN to disambiguate same-named dimensions across DSDs.

    Example:
        store.query(sc.AggregateDataset).filter(
            q.within("REF_AREA", "ON", hierarchy=h_geo_statistical_urn)
        )

    Returns:
        An `SdmxCodeFilter` for `QueryBuilder.filter` — the same object
        `has_code` would return for the equivalent `descendants_of`.
    """
    return SdmxCodeFilter(
        dimension=dimension,
        code=descendants_of(root_code, hierarchy=hierarchy, include_self=include_self),
        codelist_urn=codelist_urn,
    )


def has_dimensions(dims: list[str]) -> SdmxDimensionFilter | _NoOpFilter:
    """Filter datasets whose SDMX DSD has ALL of the listed dimensions.

    JOINs through ``sdmx.dataflow → sdmx.dsd_component``.
    An empty list matches all datasets.

    Args:
        dims: The dimension ids every matching DSD must have.

    Returns:
        The filter, or one that matches everything when *dims* is empty.

    Example:
        store.query(sc.AggregateDataset).filter(q.has_dimensions(["GEO", "SEX"])).execute()
    """
    if not dims:
        return _NoOpFilter()
    return SdmxDimensionFilter(dimensions=tuple(dims))


#: Keyword keys ``distribution()`` accepts, mapped to the column each one
#: compares. The keys are caller input, so only these column names reach the
#: SQL text (GH-71).
_DISTRIBUTION_KWARG_COLS: dict[str, str] = {
    "format": "dist_t.format",
    "media_type": "dist_t.media_type",
    "endpoint_url": "svc.endpoint_url",
}
#: Keyword keys ``has_service()`` accepts. ``DataService`` stores no service
#: type, so ``endpoint_url`` is the only service column.
_SERVICE_KWARG_COLS: dict[str, str] = {
    "endpoint_url": "svc.endpoint_url",
}
_SERVICE_JOIN = "LEFT JOIN data_service svc ON dist_t.service_id = svc.id"


def _kwarg_conditions(fn: str, allowed: dict[str, str], kwargs: dict[str, object]) -> dict[str, object]:
    """Map *kwargs* keys to their columns, refusing any key not in *allowed*.

    Args:
        fn: The calling function's name, for the error message.
        allowed: Accepted keys, mapped to qualified column names.
        kwargs: The caller's keyword arguments.

    Returns:
        The values keyed by qualified column name.

    Raises:
        ValueError: If a key is not in *allowed*.
    """
    unknown = sorted(k for k in kwargs if k not in allowed)
    if unknown:
        msg = f"{fn}() got unknown keys {unknown}; accepted keys are {sorted(allowed)}"
        raise ValueError(msg)
    return {allowed[k]: v for k, v in kwargs.items()}


def distribution(**kwargs: object) -> ExistsFilter:
    """Filter datasets that have a distribution matching *all* kwargs.

    Supported keys: ``format``, ``media_type``, and ``endpoint_url``, which
    is resolved via a LEFT JOIN to the distribution's ``data_service``. Any
    other key raises ``ValueError``.

    All kwargs within one call must match the *same* distribution row.
    For "at least two separate distributions" semantics, call this
    function twice::

        .filter(q.distribution(format="CSV"))
        .filter(q.distribution(format="JSON"))

    Args:
        **kwargs: Column values one distribution must all match: ``format``,
            ``media_type`` or ``endpoint_url``.

    Returns:
        An EXISTS filter over the dataset's distributions.

    Example:
        store.query(sc.BaseDataset).filter(q.distribution(media_type="text/csv")).execute()
    """
    conditions = _kwarg_conditions("distribution", _DISTRIBUTION_KWARG_COLS, kwargs)
    join = _SERVICE_JOIN if any(col.startswith("svc.") for col in conditions) else None
    return _DistributionFilter(conditions=conditions, join=join)


def has_service(**kwargs: object) -> ExistsFilter:
    """Filter datasets served by a data service matching *all* kwargs.

    Supported key: ``endpoint_url``. Any other key raises ``ValueError``.

    Args:
        **kwargs: Column values the data service must all match.

    Returns:
        An EXISTS filter over the dataset's distributions and their services.

    Example:
        store.query(sc.BaseDataset).filter(
            q.has_service(endpoint_url="https://example.com/sdmx")
        ).execute()
    """
    conditions = _kwarg_conditions("has_service", _SERVICE_KWARG_COLS, kwargs)
    return _DistributionFilter(conditions=conditions, join=_SERVICE_JOIN)


# ---------------------------------------------------------------------------
# Internal: distribution filter with correct alias handling
# ---------------------------------------------------------------------------


@define
class _DistributionFilter(ExistsFilter):
    """EXISTS filter against distributions, with optional service JOIN.

    Overrides ``to_sql`` to use the ``dist`` alias so that svc.* conditions
    reference the correct alias.
    """

    table: str = "distribution"

    @override
    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        alias_prefix = ctx.next_alias("ef")
        _ = alias_prefix
        join_sql = f" {self.join}" if self.join else ""
        col_clauses: list[str] = ["dist_t.dataset_id = d.identifier"]
        params: list[object] = []

        # Keys are qualified column names from _DISTRIBUTION_KWARG_COLS or
        # _SERVICE_KWARG_COLS, never caller input (GH-71).
        for fq_col, val in self.conditions.items():
            if val is None:
                col_clauses.append(f"{fq_col} IS NULL")
            elif isinstance(val, (list, tuple)):
                placeholders = ", ".join("?" * len(val))
                col_clauses.append(f"{fq_col} IN ({placeholders})")
                params.extend(val)
            else:
                col_clauses.append(f"{fq_col} = ?")
                params.append(val)

        where_sql = " AND ".join(col_clauses)
        sql = f"EXISTS (SELECT 1 FROM distribution dist_t{join_sql} WHERE {where_sql})"
        return sql, params


# ---------------------------------------------------------------------------
# Cross-entity filter constructors
# ---------------------------------------------------------------------------


# Supported kwargs for dataset-level conditions inside publishes/serves
_DATASET_KWARG_COLS: dict[str, str] = {
    "kind": "kind",
    "status": "status",
}


@define
class _PublishesFilter(ExistsFilter):
    """EXISTS filter for Agent queries — datasets the agent publishes.

    Correlates ``datasets.publisher_id = id`` where ``id`` is the ``agents``
    primary key from the outer query.  Inner ExistsFilter objects (e.g.
    ``has_code``) are applied inside the ``datasets d`` subquery where they
    can reference ``d.identifier`` normally.
    """

    table: str = "dataset"
    inner: list[PositionalFilter] = field(factory=list)
    dataset_kwargs: dict[str, object] = field(factory=dict)

    @override
    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        wheres: list[str] = ["d.publisher_id = id"]
        params: list[object] = []

        for key, val in self.dataset_kwargs.items():
            col = _DATASET_KWARG_COLS.get(key)
            if col:
                wheres.append(f"d.{col} = ?")
                params.append(val)

        for ef in self.inner:
            ef_sql, ef_params = ef.to_sql(ctx)
            wheres.append(ef_sql)
            params.extend(ef_params)

        where_sql = " AND ".join(wheres)
        sql = f"EXISTS (SELECT 1 FROM dataset d WHERE {where_sql})"
        return sql, params


@define
class _ServesFilter(ExistsFilter):
    """EXISTS filter for DataService queries — datasets the service serves.

    Correlates via ``service_dataset`` bridge table.  Inner ExistsFilter
    objects are applied against the ``dataset d`` alias inside the subquery,
    where they can reference ``d.identifier`` normally.
    """

    table: str = "dataset"
    inner: list[PositionalFilter] = field(factory=list)
    dataset_kwargs: dict[str, object] = field(factory=dict)

    @override
    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        wheres: list[str] = ["d.identifier IN (SELECT dataset_id FROM service_dataset WHERE service_id = id)"]
        params: list[object] = []

        for key, val in self.dataset_kwargs.items():
            col = _DATASET_KWARG_COLS.get(key)
            if col:
                wheres.append(f"d.{col} = ?")
                params.append(val)

        for ef in self.inner:
            ef_sql, ef_params = ef.to_sql(ctx)
            wheres.append(ef_sql)
            params.extend(ef_params)

        where_sql = " AND ".join(wheres)
        sql = f"EXISTS (SELECT 1 FROM dataset d WHERE {where_sql})"
        return sql, params


def publishes(*inner_filters: PositionalFilter, **kwargs: object) -> ExistsFilter:
    """Filter Agents that publish datasets matching the given criteria.

    Pass ``ExistsFilter`` objects (e.g. ``has_code``, ``has_dimensions``) as
    positional arguments, and/or dataset-level conditions as keyword arguments
    (``kind``, ``status``).

    Args:
        *inner_filters: Filters a published dataset must match.
        **kwargs: Dataset columns a published dataset must match.

    Returns:
        An EXISTS filter over the agent's datasets.

    Example:
        # Agents that publish aggregate datasets about Ontario
        store.query(sc.Agent).filter(
            q.publishes(q.has_code("GEO", "ON"), kind="aggregate")
        ).all()
    """
    return _PublishesFilter(inner=list(inner_filters), dataset_kwargs=dict(kwargs))


def serves(*inner_filters: PositionalFilter, **kwargs: object) -> ExistsFilter:
    """Filter DataServices that serve datasets matching the given criteria.

    Pass ``ExistsFilter`` objects as positional arguments and/or dataset-level
    conditions as keyword arguments (``kind``, ``status``).

    Args:
        *inner_filters: Filters a served dataset must match.
        **kwargs: Dataset columns a served dataset must match.

    Returns:
        An EXISTS filter over the service's datasets.

    Example:
        # DataServices that serve datasets with a GEO dimension
        store.query(sc.DataService).filter(q.serves(q.has_dimensions(["GEO"]))).all()
    """
    return _ServesFilter(inner=list(inner_filters), dataset_kwargs=dict(kwargs))


# ---------------------------------------------------------------------------
# MAP column filter helpers
# ---------------------------------------------------------------------------


@define
class _SpatialFilter(ExistsFilter):
    """EXISTS filter on a spatial coverage's own label or its concept's label.

    A region stored by identifier, such as a CKAN ``geographic_region`` code
    (GH-83), has no label of its own; its labels are on the concept whose
    URI is the region's ``uri``.
    """

    table: str = "dataset_spatial"
    lang: str = "en"
    label: str = ""

    @override
    def to_sql(self, ctx: CompileContext) -> tuple[str, list[object]]:
        alias = ctx.next_alias("ef")
        sql = f"""EXISTS (SELECT 1 FROM dataset_spatial {alias}_t
            LEFT JOIN concept {alias}_c ON {alias}_c.uri = {alias}_t.uri
            WHERE {alias}_t.dataset_id = d.identifier
                AND ({alias}_t.label[?] = ? OR {alias}_c.label[?] = ?))"""
        return sql, [self.lang, self.label, self.lang, self.label]


def has_spatial(label: str, *, lang: str = "en") -> ExistsFilter:
    """Filter datasets that have a spatial coverage matching *label*.

    The label is matched against the coverage's own label and, for a region
    stored by identifier, against the label of its concept, so
    ``has_spatial("Terre-Neuve-et-Labrador", lang="fr")`` finds a CKAN
    dataset that carries the region code ``10``. CKAN region text that is
    not a code carries no language and is stored under ``und``, so it
    matches with ``lang="und"``. To filter on a spatial facet key that is a
    URI, use `has_spatial_uri`.

    Args:
        label: The spatial coverage label, matched exactly.
        lang: The language of *label*.

    Returns:
        An EXISTS filter over the dataset's spatial coverage.

    Example:
        store.query(sc.BaseDataset).filter(q.has_spatial("Canada")).execute()
    """
    return _SpatialFilter(lang=lang, label=label)


def has_spatial_uri(uri: str) -> ExistsFilter:
    """Filter datasets that have a spatial coverage identified by *uri*.

    A spatial facet key is a region's URI when it has one (`Catalog.facets`),
    so a key from ``facets("spatial")`` filters back to the datasets it
    counts.

    Args:
        uri: The region's URI, such as a CKAN ``geographic_region`` concept
            URI (GH-83), matched exactly.

    Returns:
        An EXISTS filter over the dataset's spatial coverage.

    Example:
        key = next(iter(store.facets("spatial")["spatial"]))
        store.query(sc.BaseDataset).filter(q.has_spatial_uri(key)).execute()
    """
    return ExistsFilter(table="dataset_spatial", conditions={"uri": uri})


def has_keyword(keyword: str, *, lang: str = "en") -> ExistsFilter:
    """Filter datasets that have a keyword matching *keyword*.

    Args:
        keyword: The keyword, matched exactly.
        lang: The language of *keyword*.

    Returns:
        An EXISTS filter over the dataset's keywords.

    Example:
        store.query(sc.BaseDataset).filter(q.has_keyword("employment")).execute()
    """
    return ExistsFilter(
        table="dataset_keyword",
        conditions={"keyword": MapCondition(key=lang, value=keyword)},
    )


def title_contains(text: str, *, lang: str = "en") -> MapFilter:
    """Filter datasets whose title contains *text* (case-insensitive).

    Args:
        text: The text to find, anywhere in the title.
        lang: Which language's title to search.

    Returns:
        A filter on the dataset's title in *lang*.

    Example:
        store.query(sc.BaseDataset).filter(q.title_contains("GDP")).execute()
    """
    return MapFilter(column="title", key=lang, value=text, op="ilike")


def description_contains(text: str, *, lang: str = "en") -> MapFilter:
    """Filter datasets whose description contains *text* (case-insensitive).

    Args:
        text: The text to find, anywhere in the description.
        lang: Which language's description to search.

    Returns:
        A filter on the dataset's description in *lang*.

    Example:
        store.query(sc.BaseDataset).filter(q.description_contains("exchange")).execute()
    """
    return MapFilter(column="description", key=lang, value=text, op="ilike")

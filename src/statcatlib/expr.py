"""Sub-traversal expression language for scope-based graph queries.

Expressions describe computations over related graph data without executing
any SQL.  They are context-free objects — like ``pl.col()`` in Polars — and
can be reused across ``.enrich()``, ``.filter()``, and ``.sort_by()`` on any
collection scope.

Usage:

    import statcatlib as sc

    n_datasets = sc.each("datasets").count()
    has_data = sc.each("datasets").exists()

    store.themes["subjects"].enrich(n=n_datasets).filter(has_data).collect()
"""

from __future__ import annotations

from attrs import define

# ---------------------------------------------------------------------------
# Base expression types
# ---------------------------------------------------------------------------


@define(frozen=True)
class Expr:
    """Base class for all expression nodes."""

    def alias(self, name: str) -> AliasedExpr:
        """Name this expression result (like Polars ``.alias()``)."""
        return AliasedExpr(expr=self, name=name)

    def count(self) -> CountExpr:
        """Count items at the far end of the traversal."""
        return CountExpr(source=self)

    def exists(self) -> ExistsExpr:
        """Check if any items exist at the far end."""
        return ExistsExpr(source=self)

    def sum(self, field: str) -> SumExpr:
        """Sum a numeric field at the far end."""
        return SumExpr(source=self, field=field)

    def ids(self) -> IdsExpr:
        """Collect identifiers at the far end."""
        return IdsExpr(source=self)

    def distinct(self, field: str) -> DistinctExpr:
        """Select distinct values of a field at the far end."""
        return DistinctExpr(source=self, field=field)

    def filter(self, **kwargs: object) -> FilteredExpr:
        """Filter far-side nodes by attribute values."""
        return FilteredExpr(source=self, conditions=kwargs)

    def count_distinct(self) -> CountDistinctExpr:
        """Count distinct items at the far end."""
        return CountDistinctExpr(source=self)


@define(frozen=True)
class TraversalExpr(Expr):
    """A traversal along one or more edges from the current scope position.

    Created by ``sc.each("edge1", "edge2", ...)``.  Strings are always
    edge labels (table names or relationship names in the graph).
    """

    edges: tuple[str, ...]


@define(frozen=True)
class AliasedExpr:
    """An expression with a named output column.

    Created by ``expr.alias("name")`` or via ``**kwargs`` in ``.enrich()``.
    """

    expr: Expr
    name: str


# ---------------------------------------------------------------------------
# Terminal reduction expressions
# ---------------------------------------------------------------------------


@define(frozen=True)
class CountExpr(Expr):
    """COUNT(*) at the far end of a traversal."""

    source: Expr


@define(frozen=True)
class ExistsExpr(Expr):
    """EXISTS(...) — boolean, true if any far-side items exist."""

    source: Expr


@define(frozen=True)
class SumExpr(Expr):
    """SUM(field) at the far end of a traversal."""

    source: Expr
    field: str


@define(frozen=True)
class IdsExpr(Expr):
    """LIST(id) — collect identifiers at the far end."""

    source: Expr


@define(frozen=True)
class DistinctExpr(Expr):
    """SELECT DISTINCT field — narrows to unique values."""

    source: Expr
    field: str


@define(frozen=True)
class CountDistinctExpr(Expr):
    """COUNT(DISTINCT ...) at the far end."""

    source: Expr


@define(frozen=True)
class FilteredExpr(Expr):
    """A traversal with far-side filtering applied.

    Created by ``sc.each("datasets").filter(status="current")``.
    """

    source: Expr
    conditions: dict[str, object]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def each(*edges: str) -> TraversalExpr:
    """Create a sub-traversal expression starting from the current scope.

    Each argument is an edge label (relationship name).  Multiple arguments
    describe a multi-hop path::

        sc.each("datasets")  # one hop
        sc.each("datasets", "dimensions")  # two hops
        sc.each("datasets", "dimensions", "codelist")  # three hops

    The returned expression is context-free and can be used in ``.enrich()``,
    ``.filter()``, and ``.sort_by()`` on any collection scope.
    """
    if not edges:
        msg = "each() requires at least one edge label"
        raise ValueError(msg)
    return TraversalExpr(edges=edges)

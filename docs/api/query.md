# Query Filters

`statcatlib.query` provides composable filter objects that compile to
correlated SQL `EXISTS` subqueries. Import the module and pass filter objects
as positional arguments to `QueryBuilder.filter()`.

```python
import statcatlib.query as q

store.query(sc.AggregateDataset).filter(
    q.has_code("geo", "DE"),
    q.has_dimensions(["geo", "freq"]),
).all()
```

Multiple filter objects passed to a single `.filter()` call are **ANDed**.
Multiple `.filter()` calls are also ANDed.

---

## Dataset filters

### `has_code`

```python
q.has_code(dimension: str, code: str | CodeDescent, *, codelist_urn: str | None = None) -> SdmxCodeFilter
```

Match datasets whose DSD contains `code` in dimension `dimension`. Joins
through sdmxlib's structural tables (`dataflow → dsd_component → code`).

```python
# Datasets with code GEO=DE
store.query(sc.AggregateDataset).filter(q.has_code("geo", "DE")).all()

# Datasets with code FREQ=A (annual)
store.query(sc.AggregateDataset).filter(q.has_code("freq", "A")).all()
```

Dimension IDs are case-insensitive in most SDMX implementations but are stored
as-ingested — use the same casing as the source SDMX registry.

Pass `codelist_urn=` to disambiguate dimensions that share an ID across DSDs.

### `descendants_of` and `within`

```python
q.descendants_of(root: str, *, hierarchy=None, include_self: bool = False) -> CodeDescent
q.within(dimension: str, root_code: str, *, hierarchy=None, include_self=False, codelist_urn=None) -> SdmxCodeFilter
```

Match any code *beneath* `root` rather than the root itself — "every dataset
with a geography under Ontario". `within` is the one-call form and is usually
what you want; `descendants_of` builds the argument for `has_code`.

```python
# Equivalent
store.query(sc.AggregateDataset).filter(q.within("REF_AREA", "CA"))
store.query(sc.AggregateDataset).filter(q.has_code("REF_AREA", q.descendants_of("CA")))

# Descend a named Hierarchy instead of the resolved default
store.query(sc.AggregateDataset).filter(q.within("REF_AREA", "CA", hierarchy=h_admin))
```

**Descent is strict.** The root does not match itself, so a codelist holding
`ON` and nothing beneath it will *not* match `within("REF_AREA", "ON")`. Pass
`include_self=True` to widen it. This follows sdmxlib's `q.hcode.ancestors`,
which excludes the node itself.

**Which Hierarchy applies** is resolved per dataflow when `hierarchy=` is
omitted, through sdmxlib's `hierarchy_applicability` — a HierarchyAssociation
naming the dataflow outranks one that does not. Where no association applies,
the codelist's own parent chain answers instead.

Descent runs entirely in SQL: neither the descendant set nor the candidate
dataflow list is fetched first, so a large geography tree stays one query.

!!! warning "Changed in the sdmxlib 0.61 upgrade"

    `has_code` previously accepted an `sl.code` predicate
    (`sl.code.descendants_of(...)`). sdmxlib retired that seam; use
    `q.descendants_of(...)` instead. `q.within(...)` and the literal-string
    form of `has_code` are unchanged.

### `has_dimensions`

```python
q.has_dimensions(dims: list[str]) -> ExistsFilter
```

Match datasets whose DSD contains **all** listed dimensions. An empty list
matches all datasets.

```python
# Datasets with both GEO and FREQ dimensions
store.query(sc.AggregateDataset).filter(q.has_dimensions(["geo", "freq"])).all()

# Three required dimensions
store.query(sc.AggregateDataset).filter(
    q.has_dimensions(["geo", "freq", "age"])
).all()
```

### `distribution`

```python
q.distribution(
    format: str | None = None,
    media_type: str | None = None,
    service_type: str | None = None,
    endpoint_url: str | None = None,
) -> ExistsFilter
```

Match datasets that have at least one distribution matching all specified
criteria. All keyword arguments are ANDed within a single distribution row.

```python
# Datasets with a CSV distribution
store.query(sc.OpenDataset).filter(q.distribution(format="CSV")).all()

# Datasets with a Parquet distribution (written by ingest_data)
store.query(sc.AggregateDataset).filter(
    q.distribution(media_type="application/parquet")
).all()
```

### `has_service`

```python
q.has_service(
    service_type: str | None = None,
    endpoint_url: str | None = None,
) -> ExistsFilter
```

Match datasets served by a `DataService` matching the specified criteria.
Equivalent to `distribution(...)` but focuses on the linked service rather
than the distribution itself.

```python
# Datasets served via the OECD SDMX endpoint
store.query(sc.AggregateDataset).filter(
    q.has_service(endpoint_url="https://sdmx.oecd.org/public/rest")
).all()
```

---

## Cross-entity filters

These filters apply to `Agent` and `DataService` queries, correlating through
the datasets table.

### `publishes`

```python
q.publishes(
    *inner_filters: PositionalFilter,
    kind: str | None = None,
    **dataset_kwargs,
) -> ExistsFilter
```

For use with `store.query(sc.Agent)`. Match agents that publish at least one
dataset satisfying all specified criteria.

- `inner_filters` — any `PositionalFilter` (e.g. `has_code`, `has_dimensions`,
  `distribution`, `has_service`)
- `kind` — filter to a specific dataset kind (`"open"`, `"aggregate"`,
  `"microdata"`, `"geospatial"`, `"publication"`)
- `**dataset_kwargs` — flat column filters (`status`, `frequency`, etc.)

```python
# Agents that publish any dataset
store.query(sc.Agent).filter(q.publishes()).all()

# Agents that publish aggregate datasets
store.query(sc.Agent).filter(q.publishes(kind="aggregate")).all()

# Agents that publish datasets with GEO=CA
store.query(sc.Agent).filter(
    q.publishes(q.has_code("geo", "CA"))
).all()

# Agents that publish aggregate datasets with GEO=CA
store.query(sc.Agent).filter(
    q.publishes(q.has_code("geo", "CA"), kind="aggregate")
).all()

# Agents that publish BOTH aggregate and open datasets (two separate conditions)
store.query(sc.Agent).filter(
    q.publishes(kind="aggregate")
).filter(
    q.publishes(kind="open")
).all()
```

### `serves`

```python
q.serves(
    *inner_filters: PositionalFilter,
    kind: str | None = None,
    **dataset_kwargs,
) -> ExistsFilter
```

For use with `store.query(sc.DataService)`. Match services that serve at least
one dataset satisfying all specified criteria. Same argument semantics as
`publishes()`.

```python
# Services that serve any dataset
store.query(sc.DataService).filter(q.serves()).all()

# Services that serve aggregate datasets with GEO=DE
store.query(sc.DataService).filter(
    q.serves(q.has_code("geo", "DE"), kind="aggregate")
).all()

# Services that serve datasets, combined with field filter
store.query(sc.DataService).filter(
    q.serves(kind="aggregate")
).filter(id="ECB-API").all()
```

---

## Combining filters

All filters compose with AND semantics. There is no OR filter — use separate
queries and merge results in Python when needed.

```python
# Every condition must hold: GEO=DE, GEO and FREQ dimensions, a CSV distribution, ECB, current
store.query(sc.AggregateDataset).filter(
    q.has_code("geo", "DE"),
    q.has_dimensions(["geo", "freq"]),
    q.distribution(format="CSV"),
    publisher="ECB",
    status="current",
).all()
```

Keyword arguments (flat column filters) and `ExistsFilter` objects can be
mixed in the same `.filter()` call.

---

## ExistsFilter and PositionalFilter

`ExistsFilter` is the base class for subquery-based filters. `PositionalFilter`
is the type alias accepted by `QueryBuilder.filter()` as positional arguments:

```python
PositionalFilter = ExistsFilter | SdmxCodeFilter | SdmxCodelistFilter | SdmxDimensionFilter
```

`has_code()` returns `SdmxCodeFilter` (not `ExistsFilter`) because it JOINs
sdmxlib's structural tables rather than using a correlated EXISTS subquery. Both
are accepted anywhere `PositionalFilter` is expected.

You can implement custom filters by subclassing `ExistsFilter`:

```python
from attrs import define, field
from statcatlib.query import ExistsFilter

@define
class MyCustomFilter(ExistsFilter):
    my_param: str

    def to_sql(self, alias_prefix: str) -> tuple[str, list]:
        return (
            f"EXISTS (SELECT 1 FROM my_table t WHERE t.col = ?)",
            [self.my_param],
        )
```

Pass your custom filter to `.filter()` like any built-in:

```python
store.query(sc.OpenDataset).filter(MyCustomFilter(my_param="value")).all()
```

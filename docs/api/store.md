# Catalog API

`Catalog` is the single entry point to the catalog. It wraps a DuckDB
connection and exposes read, write, and query operations on the catalog star schema.

## Lifecycle

### Constructors

```python
Catalog()
Catalog(":memory:")
```

Create an in-memory store. No data is persisted after the process exits.
Useful for tests, scripts, and ephemeral pipelines.

```python
Catalog(path: str | Path, *, embedding_model: Any | None = None)
```

Name a catalog folder at `path`. Construction is cheap and does **not** touch the
disk — the DuckDB connection opens on `.open()` or on entering a `with` block,
which creates `path/meta.duckdb`, `path/lake.ducklake` and `path/parquet/` if
they do not exist.
Querying an unopened catalog raises `RuntimeError`.

Pass `embedding_model` to enable semantic and hybrid search. The model must have
an `encode(str) -> list[float]` method — any SentenceTransformers, OpenAI, or
Cohere model works. A LanceDB dataset is created at `{path}/search/`.

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("intfloat/multilingual-e5-small")
store = sc.Catalog("my_catalog", embedding_model=model)
```

Requires the `search` optional extra (`uv run --extra search` or
`uv add statcatlib[search]`). In-memory stores cannot use `embedding_model`
(raises `ValueError`).

```python
Catalog.from_connection(conn: duckdb.DuckDBPyConnection)
```

Attach to an existing DuckDB connection. The store does **not** own the
connection — it will not close it on `close()` or context manager exit. Use
this when sharing a connection with sdmxlib's `LocalRegistry`:

```python
import duckdb
import sdmxlib as sl
import statcatlib as sc

conn = duckdb.connect("shared.duckdb")
store = sc.Catalog.from_connection(conn)
local_reg = sl.LocalRegistry(conn)   # same connection, same tables
```

### Opening

```python
store.open() -> Self
```

Open the connection, creating the catalog folder if it does not exist —
open-or-create, and the only spelling. Prefer the context manager, which opens
on entry and closes on exit:

```python
with sc.Catalog("my_catalog") as store:
    store.query(sc.AggregateDataset).limit(5).collect()
```

### Properties

```python
store.connection -> duckdb.DuckDBPyConnection
```

The underlying DuckDB connection. Use for raw SQL queries or to pass to
sdmxlib's `LocalRegistry`.

```python
store.parquet_dir -> Path | None
```

Path to the `parquet/` directory inside the bundle folder. `None` for in-memory
stores or connection-mode stores.

### Context manager

```python
with sc.Catalog("my_catalog") as store:
    ...
store.close()
```

`close()` closes the DuckDB connection if the store owns it (not in
connection-mode). Safe to call multiple times.

---

## Writing datasets

### `put`

```python
store.put(obj: BaseDataset | ConceptScheme | list[BaseDataset | ConceptScheme] | tuple[...]) -> None
```

Add or update a dataset, a SKOS concept scheme, or a list/tuple of either.
If a record with the same `identifier`/`uri` already exists it is replaced
(upsert semantics).

**Accepted types:**

- `BaseDataset` (any variant: `OpenDataset`, `AggregateDataset`, etc.)
- `ConceptScheme` — upserts the scheme row and cascades to all `Concept` rows
- `list` or `tuple` containing any mix of the above

**Examples:**

```python
# Single dataset
store.put(sc.OpenDataset(identifier="co2-2024", title=I18n(en="CO2 2024"), description=I18n(en="...")))

# Concept scheme (cascades to concept rows)
store.put(sc.ConceptScheme(
    uri="urn:myorg:themes",
    label=I18n(en="My Themes"),
    concepts={
        "ENV": sc.Concept(notation="ENV", label=I18n(en="Environment")),
    },
))

# Batch — any mix of dataset kinds and concept schemes
store.put([ds1, ds2, scheme1])
```

**Theme indexing note:** `put(dataset)` writes only the `dataset_theme` bridge
rows. For theme labels to appear in BM25 search results, store the
`ConceptScheme` first so the `concept` table is populated before `build_index()`
runs.

**SDMX structural filters:** `has_code()` and `has_dimensions()` JOIN through
sdmxlib's structural tables via `sdmx_dataflow_urn`. These filters only work for
datasets created by `ingest_sdmx()`.

---

## Reading datasets

### `get`

```python
store.get(cls: type[T], identifier: str) -> T | None
```

Direct lookup by identifier. Returns `None` if:

- no dataset with that `identifier` exists, or
- the stored `kind` does not match `cls`.

Type narrowing is strict — requesting `AggregateDataset` for an `OpenDataset`
returns `None`.

```python
ds = store.get(sc.AggregateDataset, "ESTAT:NAMA_10_GDP(1.0)")
if ds is not None:
    print(ds.sdmx_dataflow_urn)
    print(ds.measure_dim)
```

Use `sc.BaseDataset` to retrieve without type filtering:

```python
ds = store.get(sc.BaseDataset, "co2-2024")
```

---

## Querying

### `query`

```python
store.query(cls: type[T], **kwargs) -> QueryBuilder[T]
```

Start a structured query. Returns a `QueryBuilder` — no database round-trip
until `.execute()`, `.all()`, `.first()`, `.one()`, or `.count()` is called.

```python
results = store.query(sc.AggregateDataset).filter(publisher="ESTAT").execute()
```

### `search`

```python
store.search(
    text: str,
    *,
    kinds: Sequence[str] = ("dataset", "concept", "code"),
    lang: str = "en",
    limit: int = 50,
    offset: int = 0,
    operator: str = "or",
    fuzzy: int = 0,
    rrf_k: int = 60,
) -> SearchResult[Hit[Any]]
```

[How search works](../search.md) explains what is indexed, the
per-language tables, BM25, vectors and fusion.

BM25 full-text search across every indexed entity kind, RRF-merged.
Results are relevance-ranked. Not type-generic — use `query(...).search(text)`
to narrow by dataset kind.

| Parameter  | Description |
|------------|-------------|
| `kinds`    | Entity kinds to search; pass `"reference_metadata"` explicitly to include methodology sections |
| `lang`     | Which language's index to search. Each language has its own index, so a French query needs `lang="fr"` — the default `"en"` will not match French text |
| `operator` | `"or"` (default) matches any term; `"and"` requires all terms |
| `fuzzy`    | Levenshtein edit distance for fuzzy matching: `0` (default), `1`, or `2` |
| `rrf_k`    | Reciprocal-Rank-Fusion constant for merging across kinds |

Requires LanceDB (`statcatlib[search]`). Returns an empty `SearchResult` when
the LanceDB index has not been built yet; never raises for in-memory or
unbuilt-index stores.

```python
results = store.search("labour force survey", kinds=("dataset",), limit=10)
for hit in results.items:
    print(hit.score, hit.payload.identifier, hit.payload.title.get("en"))

# Fuzzy search catches typos and spelling variations
results = store.search("consumr price", fuzzy=1)
```

### Vector and hybrid search — `datasets.find`

```python
store.datasets.find(
    query: str, *, mode: str = "bm25", lang: str = "en", limit: int = 50,
    offset: int = 0, operator: str = "or", fuzzy: int = 0, rrf_k: int = 60,
) -> SearchResult[Hit[BaseDataset]]
```

Dataset search with a choice of evidence: `mode="bm25"` (the default),
`"vector"` for nearest-neighbour search over the embeddings, or `"hybrid"`,
which merges both rankings with Reciprocal Rank Fusion (k=60 by default).
Each `Hit` carries the dataset as `hit.payload`, plus `hit.score` and
`hit.source` (`"bm25"`, `"vector"`, or `"hybrid"` when both matched).

`"vector"` and `"hybrid"` encode `query` with the configured
`embedding_model` and raise `RuntimeError` without one. `cat.concepts`,
`cat.codes` and `cat.reference_metadata` have the same `find`.

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("intfloat/multilingual-e5-small")  # 384 dims, bilingual
store = sc.Catalog("my_catalog", embedding_model=model)

# Pure semantic
results = store.datasets.find("people moving between provinces", mode="vector")

# Hybrid — recommended for most queries
results = store.datasets.find("labour market monthly data", mode="hybrid", limit=20)
for hit in results.items:
    print(hit.payload.identifier, hit.score, hit.source)

# Hybrid + relational filter via QueryBuilder
import statcatlib.query as q
results = (
    store.query(sc.AggregateDataset)
    .filter(q.has_code("GEO", "ON"), status="current")
    .hybrid_search("unemployment by region")
    .limit(20)
    .execute()
)
```

### `build_index`

```python
store.build_index(embedding_model: Any = None) -> int
```

Re-index all datasets for search and optionally build ANN vectors.
If `embedding_model` is provided it is stored for future vector operations.

Returns the number of datasets embedded.

```python
from sentence_transformers import SentenceTransformer
import statcatlib as sc

model = SentenceTransformer("intfloat/multilingual-e5-small")
store = sc.Catalog("my_catalog").open()
n = store.build_index(model)
print(f"embedded {n} datasets")
```

After `build_index()` completes, open the store with `embedding_model=model`
to use `datasets.find(mode="vector")` and `mode="hybrid"`.

Requires the `search` optional extra. Raises `ValueError` for in-memory stores.

A rebuild is atomic: the new index is written in full and committed in one
step, so an interrupted `build_index()` leaves the previous index intact and
still serving rather than half-replaced.

---

### `index_layout`

```python
store.index_layout -> dict[str, dict[str, Any]]
```

What each language's search index was built with — `{lang: {"table", "rows",
"stemmer"}}`. Empty when no index has been built.

```python
print(store.index_layout)
# {"en": {"table": "search_en__000001", "rows": 946,  "stemmer": "English"},
#  "fr": {"table": "search_fr__000001", "rows": 1173, "stemmer": "French"}}
```

After a rebuild, check that every language you expect is present and names
the stemmer you expect. A missing language is one whose rows were never tagged
with it upstream. A `stemmer` of `None` means no Snowball stemmer exists for
that language, so it is indexed unstemmed: queries miss inflected forms, and
no word is stemmed by another language's rules.

Unlike `embedding_metadata`, this is populated for BM25-only catalogs, which
write no `embeddings.json` at all.

It also doubles as a startup guard. An index written by a *newer* statcatlib is
not readable by an older one, which returns zero hits with no error:

```python
if not store.index_layout:
    raise RuntimeError("search index missing or built by a newer statcatlib")
```

---

### `facets`

```python
store.facets(*fields: str) -> dict[str, dict[str, int]]
```

Aggregation counts across all datasets for each requested field. Useful for
building filter panels in UIs or profiling catalog composition.

Supported fields: `"publisher"`, `"themes"`, `"frequency"`, `"status"`,
`"spatial"`, `"type"`.

```python
counts = store.facets("publisher", "themes", "frequency")
# {"publisher": {"ESTAT": 42, "ECB": 18}, "themes": {"Labour": 22, ...}}
```

---

## Structural traversal

These methods query sdmxlib's structural tables (codelists, DSDs, dataflows)
via the shared connection.

### `code_map`

```python
store.code_map(identifier: str) -> dict[str, list[str]]
```

Return all dimension→codes for an `AggregateDataset`. Keys are dimension IDs;
values are lists of code IDs.

```python
codes = store.code_map("ESTAT:NAMA_10_GDP(1.0)")
# {"na_item": ["B1GQ", "P31_S14", ...], "geo": ["AT", "BE", "DE", ...], ...}
```

### `codelist_for`

```python
store.codelist_for(identifier: str, dimension_id: str) -> sdmxlib.model.codelist.Codelist | None
```

Return the full sdmxlib `Codelist` object for a specific dimension of an
`AggregateDataset`. Returns `None` if not found.

### `get_structure`

```python
store.get_structure(identifier: str) -> sdmxlib.model.datastructure.DataStructure | None
```

Return the sdmxlib `DataStructure` (DSD) for an `AggregateDataset`. Returns
`None` if not found.

### `registry`

```python
store.registry -> sdmxlib.LocalRegistry
```

Return a `LocalRegistry` backed by the store's DuckDB connection. Use this to
query sdmxlib structural objects (dataflows, codelists, DSDs) directly:

```python
reg = store.registry
flow = reg.get(sl.SdmxUrn.parse("urn:sdmx:..."))
```

---

## Lineage (PROV-O)

### `add_lineage`

```python
store.add_lineage(
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
) -> None
```

Record a PROV-O-aligned derivation edge from `source_id` (upstream) to
`target_id` (downstream). The triple `(source_id, target_id, relationship)`
is unique — calling again with the same triple updates the other fields (upsert).

**`relationship` vocabulary** (see `Catalog.LINEAGE_RELATIONSHIPS`):

| Value | Meaning |
|---|---|
| `derived_from` | Generic derivation |
| `aggregated_from` | Target aggregates source microdata |
| `adjusted_from` | Seasonal/price/other adjustment |
| `revision_of` | Target is a revised version |
| `supplement_to` | Target supplements source |
| `feeds_into` | Source feeds into target computation |
| `uses_classification` | Dataset depends on a codelist |
| `replaces` | Target replaces source (archival) |

**`confidence` vocabulary** (see `Catalog.LINEAGE_CONFIDENCE`):
`"asserted"` (manual), `"extracted"` (scraped), `"inferred"` (LLM).

```python
store.add_lineage(
    "lfs-microdata",
    "ESTAT:UNE_RT_M(1.0)",
    "aggregated_from",
    activity_type="aggregation",
    activity_label="LFS monthly tabulation",
    agent_id="ESTAT",
    confidence="asserted",
)
```

### `DatasetScope` — lineage navigation

Lineage traversal and querying is accessed via `store.dataset(id)`:

#### `.lineage_records()`

```python
store.dataset(id).lineage_records(
    *,
    role: str = "either",
    relationship: str | None = None,
    confidence: str | None = None,
) -> list[dict[str, Any]]
```

Return lineage rows involving the dataset as plain dicts. Each dict has keys:
`id`, `source_id`, `target_id`, `relationship`, `activity_label`,
`activity_type`, `agent_id`, `timestamp`, `confidence`, `notes`.

**Parameters:**

- `role` — `"source"` (dataset is upstream input), `"target"` (downstream
  output), `"either"` (default — both roles).
- `relationship` — filter to a specific relationship type.
- `confidence` — filter to a specific confidence level.

#### `.upstream()`

```python
store.dataset(id).upstream(
    *,
    relationship: str | None = None,
    depth: int = 10,
) -> QueryBuilder[BaseDataset]
```

Recursively traverse `source_id` links upward. Returns a `QueryBuilder` over
all ancestor datasets found in the catalog — datasets referenced by lineage edges
but not in the `datasets` table are silently skipped. Deduplicates — each
dataset appears at most once even with diamond-shaped lineage.

Because this returns a `QueryBuilder`, you can chain `.filter()`, `.include()`,
`.sort_by()`, `.limit()`, etc. before materializing:

```python
# Simple traversal
ancestors = store.dataset("ESTAT:UNE_RT_M_SA(1.0)").upstream(depth=5).collect()

# Chain additional operations before executing
publishers = (
    store.dataset("ESTAT:UNE_RT_M_SA(1.0)")
    .upstream()
    .include("publisher")
    .scalars("publisher")
    .values()
)
```

#### `.downstream()`

```python
store.dataset(id).downstream(
    *,
    relationship: str | None = None,
    depth: int = 10,
) -> QueryBuilder[BaseDataset]
```

Recursively traverse `target_id` links downward. Returns a `QueryBuilder` over
all descendant datasets in the catalog.

```python
# Impact analysis: datasets affected by a change to CL_GEO
affected = store.dataset("CL_GEO").downstream(relationship="uses_classification").collect()

# Chain filters after traversal
current_descendants = (
    store.dataset("lfs-microdata")
    .downstream()
    .filter(status="current")
    .collect()
)
```

#### `.related()`

```python
store.dataset(id).related(edge: str) -> QueryBuilder[BaseDataset]
```

Find datasets sharing an entity via the given edge type. Currently supports
`"codelist"` — returns a `QueryBuilder` over datasets that share at least one
codelist with the given dataset.

```python
related = store.dataset("ESTAT:NAMA_10_GDP(1.0)").related("codelist").collect()
```

#### `.codelists`

Navigate to codelists used by this dataset (lazy scope).

```python
store.dataset("housing-stats").codelists.collect()
```

#### `.themes`

Navigate to themes assigned to this dataset (lazy scope).

```python
store.dataset("housing-stats").themes.collect()
```

---

## Batch label resolution

### `CodelistScope.batch_labels`

```python
store.codelist(urn).batch_labels(
    code_ids: list[str],
    lang: str | None = None,
) -> dict[str, str]
```

Resolve labels for many codes in a single SQL query. Returns a dict mapping
`code_id → label`. Falls back to the code ID when the requested language has
no entry. `lang` defaults to the scope's `.lang()` setting.

```python
geo_urn = sc.urn.codelist("SDMX", "CL_GEO")
labels = store.codelist(geo_urn).batch_labels(["CA", "US", "DE"])
# → {"CA": "Canada", "US": "United States", "DE": "Germany"}

# Language override via scope
labels = store.codelist(geo_urn).lang("fr").batch_labels(["CA", "US"])
# Falls back to code_id when French label is absent
```

### `CodelistsScope.batch_labels`

```python
store.codelists.batch_labels(
    pairs: list[tuple[str, str]],
    lang: str | None = None,
) -> dict[tuple[str, str], str]
```

Resolve labels across multiple codelists in one query. Accepts a list of
`(codelist_urn, code_id)` tuples and returns a dict mapping each pair to its
label.

```python
pairs = [
    (sc.urn.codelist("SDMX", "CL_GEO"), "CA"),
    (sc.urn.codelist("SDMX", "CL_GEO"), "US"),
    (sc.urn.codelist("SDMX", "CL_FREQ"), "A"),
]
labels = store.codelists.batch_labels(pairs, lang="en")
# → {("urn:...CL_GEO...", "CA"): "Canada",
#    ("urn:...CL_GEO...", "US"): "United States",
#    ("urn:...CL_FREQ...", "A"): "Annual"}
```

---

## QueryBuilder

`QueryBuilder[T]` is returned by `store.query()`. Builders are immutable: each
method returns a new `QueryBuilder` and leaves the original unchanged, so
calls chain.

### `.filter`

```python
.filter(*pos_filters: PositionalFilter | MapFilter, **kwargs) -> QueryBuilder[T]
```

Add one or more filter conditions (ANDed together). Accepts:

- keyword arguments for flat column filters (`publisher`, `status`,
  `frequency`, `identifier`)
- `PositionalFilter` objects from `statcatlib.query`: `has_code`, `has_dimensions`,
  `distribution`, `has_service`, `publishes`, `serves` (and any `ExistsFilter` subclass)
- `MapFilter` objects: `has_spatial`, `has_keyword`, `title_contains`,
  `description_contains`

Multiple calls to `.filter()` are also ANDed.

### `.search`

```python
.search(text: str, *, operator: str = "or", fuzzy: int = 0) -> QueryBuilder[T]
```

Add BM25 full-text search. Combines with any `.filter()` conditions.

| Parameter  | Description |
|------------|-------------|
| `operator` | `"or"` (default) matches any term; `"and"` requires all terms |
| `fuzzy`    | Levenshtein edit distance for fuzzy matching: `0` (default), `1`, or `2` |

### `.hybrid_search`

```python
.hybrid_search(text: str) -> QueryBuilder[T]
```

Add hybrid BM25 + vector search (RRF). Requires `embedding_model` on the store.
Relational filters from `.filter()` are applied on top of the RRF-merged result.

```python
import statcatlib.query as q
results = (
    store.query(sc.AggregateDataset)
    .filter(status="current")
    .filter(q.has_code("GEO", "ON"))
    .hybrid_search("unemployment trends")
    .limit(20)
    .execute()
)
```

### `.sort_by`

```python
.sort_by(field: str, *, desc: bool = False) -> QueryBuilder[T]
```

Sort by a flat column. Valid fields: `identifier`, `status`, `frequency_id`,
`issued`, `modified`.

### `.limit` / `.offset`

```python
.limit(n: int) -> QueryBuilder[T]
.offset(n: int) -> QueryBuilder[T]
```

Pagination controls.

### `.with_facets`

```python
.with_facets(*fields: str) -> QueryBuilder[T]
```

Request facet counts alongside the result. Same fields as `store.facets()`.
Counts will reflect the filtered result set, not the whole catalog.

### `.include`

```python
.include(*relationships: str) -> QueryBuilder[T]
```

Declare which relationships to batch-load during hydration. By default all
relationships are loaded (~17 queries per list call). Calling `.include()`
switches to selective loading — only the named relationships are fetched;
everything else is set to an `UNLOADED` sentinel.

Multiple `.include()` calls accumulate. Use `.full()` to revert.

```python
# Load only publisher and themes — 3 queries instead of ~17
store.query(sc.BaseDataset).include("publisher", "themes").all()

# Accumulate across calls
store.query(sc.BaseDataset).include("publisher").include("themes", "keywords").all()
```

Unloaded relationships are set to a special `UNLOADED` sentinel (not `None` or
`[]`). Accessing any attribute, iterating, taking `len()`, or indexing an unloaded
field raises `NotLoadedError`:

```python
ds = store.query(sc.BaseDataset).include("publisher").first()
ds.publisher.name    # OK — publisher was included
ds.themes            # returns UNLOADED sentinel (falsy, no raise)
ds.themes[0]         # raises NotLoadedError: 'themes' was not loaded
list(ds.keywords)    # raises NotLoadedError: 'keywords' was not loaded

# Check before accessing
if not sc.is_unloaded(ds.themes):
    print(ds.themes)
```

`NotLoadedError` is a subclass of `AttributeError`. `sc.is_unloaded(value)` returns
`True` for any `UNLOADED` sentinel. Both are in `statcatlib.__all__`.

Valid relationship names (matching model field names):

| Name | Field type |
|---|---|
| `publisher` | `Agent \| None` |
| `contact_point` | `ContactPoint \| None` |
| `frequency` | `Frequency \| None` |
| `licence` | `LicenceDocument \| None` |
| `themes` | `tuple[ConceptRef, ...]` |
| `subject` | `tuple[ConceptRef, ...]` |
| `dataset_type` | `ConceptRef \| None` |
| `keywords` | `list[InternationalString]` |
| `spatial_coverage` | `list[SpatialCoverage]` |
| `distributions` | `list[Distribution]` |
| `conforms_to` | `list[Standard]` |
| `quality_annotations` | `list[QualityAnnotation]` |
| `provenance` | `list[ProvenanceStatement]` |
| `dimension_names` | `list[InternationalString]` (AggregateDataset) |
| `variables` | `list[Variable]` (MicrodataDataset) |
| `feature_types` | `list[str]` (GeospatialDataset) |
| `authors` | `list[str]` (PublicationDataset) |

Raises `ValueError` for unrecognised names.

### `.full`

```python
.full() -> QueryBuilder[T]
```

Load all relationships (the default behaviour). Useful to explicitly opt into
full hydration, or to reset after a partial `.include()` chain:

```python
store.query(sc.BaseDataset).filter(...).full().all()
```

Note: `store.get()` always loads all relationships regardless of `.include()`.

### Terminal methods

Entity terminals (default — return domain objects):

```python
.execute() -> SearchResult[T]    # run query; return items + total + facets
.collect() -> list[T]            # just the items (preferred)
.all()     -> list[T]            # alias for .collect()
.first()   -> T | None           # limit(1).collect()[0] or None
.one()     -> T                  # assert exactly 1 result
.count()   -> int                # total count
.exists()  -> bool               # True if at least one match
```

Row terminals (require `.select()` — return `Row` projections):

```python
.rows()    -> list[Row]          # projected rows
.row()     -> Row | None         # first projected row or None
```

Scalar terminals (require `.scalars()` — return bare values):

```python
.values()  -> list[Any]          # flat scalar values
.value()   -> Any | None         # first scalar value or None
```

Calling an entity terminal (`.collect()`, `.first()`, etc.) when `.select()`
or `.scalars()` is active raises `ValueError` — use `.rows()` / `.values()`
instead.

---

## Projection modifiers

### `Row`

`Row` is a `dict` subclass that also supports attribute-style field access:

```python
rows = store.query(sc.BaseDataset).select("identifier", "status").rows()
row = rows[0]

row["identifier"]   # dict-style access
row.identifier      # attribute-style access — same value
isinstance(row, dict)  # True — fully dict-compatible

# Missing attribute raises AttributeError
row.nonexistent     # AttributeError: Row has no field 'nonexistent'
```

`Row` is exported as `sc.Row`.

### `.select`

```python
.select(*columns: str, lang: str | None = None) -> QueryBuilder[T]
```

Set row projection mode. Use `.rows()` or `.row()` as the terminal.
A single SQL query is issued — no `store.get()` call per row.

Selectable columns: `identifier`, `kind`, `status`, `issued`, `modified`,
`landing_page`, `version` (flat), `title`, `description` (MAP — require `lang`).

```python
rows = (
    store.query(sc.BaseDataset)
    .filter(status="current")
    .sort_by("title", lang="en")
    .select("identifier", "title", lang="en")
    .rows()
)
# → [Row({"identifier": "EXR", "title": "Exchange Rates"}), ...]

rows[0].identifier   # "EXR"
rows[0]["title"]     # "Exchange Rates"
```

Raises `ValueError` for unrecognised columns or MAP columns missing `lang`.

### `.scalars`

```python
.scalars(column: str, *, lang: str | None = None) -> QueryBuilder[T]
```

Set scalar projection mode. Use `.values()` or `.value()` as the terminal.
Single SQL query.

```python
ids = store.query(sc.BaseDataset).filter(status="current").scalars("identifier").values()
# → ["EXR", "M1", "UNEMP", ...]

titles = store.query(sc.BaseDataset).scalars("title", lang="en").values()
# → ["Exchange Rates", "Money Supply", ...]
```

---

## SearchResult

```python
@define
class SearchResult(Generic[T]):
    items: list[T]
    total: int
    offset: int = 0
    facets: dict[str, dict[str, int]] = Factory(dict)
    scores: dict[str, float] = Factory(dict)
```

- `items` — the dataset objects for the current page
- `total` — total matching datasets (may exceed `len(items)` when paginating)
- `offset` — the offset this page starts at
- `facets` — populated when `.with_facets()` was used; otherwise empty dict

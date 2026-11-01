# Catalog API

A catalog is a directory of products, and each product is a chain of immutable
editions. Four classes make up the API:

- `Catalog` creates or opens the catalog and returns its products.
- `Product` returns a view of its live edition, and starts its working copy.
- `WorkingCopy` is the product's one unsealed edition. It holds the write
  methods (`put`, `add_lineage`, `add_language_alternates`, `build_index`) and
  the writable sdmxlib registry, and `seal()` turns it into an edition.
- `EditionView` reads one edition. It holds every query method on this page:
  `get`, `query`, `search`, `facets`, the scopes, and the structural lookups.

In the examples, `wc` is a `WorkingCopy` and `view` is an `EditionView`.
Callers reach the catalog's files only through this API.
[Catalog Format](../catalog-format.md) specifies them.

## Lifecycle

### Catalogs

```python
Catalog.create(path: str | Path, *, exist_ok: bool = False) -> Catalog
Catalog.open(path: str | Path) -> Catalog
```

`Catalog.create` creates a catalog at `path`, a missing or empty directory,
and returns it writable. With `exist_ok=True` it reopens an existing catalog
for writing, and writes nothing. It raises `FileExistsError` when `path` is a
catalog and `exist_ok` is false.

`Catalog.open` opens the catalog at `path` read-only and writes nothing. Its
products' `working_copy()` raises `RuntimeError`. It raises
`FileNotFoundError` when `path` is not a catalog.

Both raise `IncompatibleCatalogError` for a directory holding a statcatlib 0.11
catalog, a published sdmxlib 0.72 bundle, or other files, and change nothing
in it. [Upgrading](../upgrading.md) gives the re-ingest steps.

A catalog is a context manager. `close()` closes every view it returned.

```python
import statcatlib as sc

with sc.Catalog.create("my_catalog") as cat:
    wc = cat.product("14100287").working_copy()
    sc.ingest_sdmx(wc, registry)
    print(wc.view().query(sc.AggregateDataset).count())
    wc.seal()

with sc.Catalog.open("my_catalog") as cat:
    view = cat.product("14100287").view()
    print(view.query(sc.AggregateDataset).count())
```

```python
cat.product(product_id: str) -> Product
cat.products() -> list[str]
cat.refresh() -> None
cat.writable -> bool
cat.catalog_id -> str
```

`product` returns the product `product_id`, which need not exist yet. A
product id names a directory: letters, digits, `.`, `_` and `-`, starting with
a letter or digit. `products` lists the products with a live edition.
`refresh` re-reads the catalog's Inventory, so `product(id).view()` returns
the newest live edition.

`Catalog.search` and `Catalog.facets` raise `NotImplementedError`. Search and
count one product's view.

For tests and scripts, `statcatlib.testing.temporary_catalog()` creates a
catalog in a temporary directory and deletes it when the block ends:

```python
from statcatlib.testing import temporary_catalog

with temporary_catalog() as cat:
    wc = cat.product("scratch").working_copy()
```

### Products

```python
product.view(*, embedding_model: EmbeddingModel | None = None, query_prefix: str = "") -> EditionView
```

A read-only `EditionView` of the product's live edition, as of the catalog's
last `refresh`. The view stays on that edition after a later seal; call
`product.view()` again after `cat.refresh()` to follow the live edition. It
raises `NoLiveEditionError` when the product has no live edition.

Pass `embedding_model`, the model the index was built with, for vector and
hybrid search. Any model with an `encode` method works, such as a
SentenceTransformers model. BM25 search needs none. `query_prefix` defaults to the one the index recorded.

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("intfloat/multilingual-e5-small")
view = cat.product("14100287").view(embedding_model=model)
```

Requires the `search` optional extra: `uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'` (`uv run --extra search` in a checkout).

```python
product.working_copy(
    *,
    lifecycle: str = "revisable",
    resume: bool = False,
    embedding_model: EmbeddingModel | None = None,
    document_prefix: str = "",
    query_prefix: str = "",
) -> WorkingCopy
```

Start the product's working copy on its live edition. The working copy starts
with the live edition's rows, so a seal records the next edition of the
product. A product has one working copy at a time, and one writer.
`working_copy()` raises `WorkingCopyExistsError` while one is unsealed, and
`resume=True` reopens it. `lifecycle` is `"revisable"` or `"final"`; a product
keeps the lifecycle it was first recorded with. `embedding_model` and the
prefixes are the encoder settings the seal's index rebuild uses.

```python
product.editions() -> list[Edition]
```

The product's live edition, with the `previous` edition its manifest records,
or an empty list.

### Working copies

```python
wc.view() -> EditionView
wc.seal(checks: Iterable[SealCheck] = ()) -> Edition
wc.discard() -> None
wc.close() -> None
wc.registry -> sdmxlib.LocalRegistry
wc.product_id -> str
wc.edition_id -> str
wc.previous -> str | None
```

`view` returns an `EditionView` over the working copy that sees each write as
it is made.

`seal` turns the working copy into an immutable edition. It runs `checks`
against the working copy's view, rebuilds the search index from the edition's
rows with the arguments of the last `build_index` call (the defaults when
there was none), and records the edition as the product's live edition. A
check that finds a problem raises `SealCheckError` and leaves the working copy
open. `statcatlib.checks.conf_status()` refuses observations whose
confidentiality status is not public:

```python
from statcatlib import checks

edition = wc.seal(checks=[checks.conf_status()])
```

`discard` deletes the unsealed edition. `close` leaves it unsealed, for
`working_copy(resume=True)`; leaving a `with wc:` block calls it. After a
seal, a discard or a close, every method on the working copy and on its views
raises `WorkingCopyClosedError`.

`registry` is the writable sdmxlib registry over the edition.
`wc.registry.ingest(...)` adds SDMX structures, and
`wc.registry.write_observations(...)` writes observations into the edition's
DuckLake.

---

## Writing datasets

The write methods are `WorkingCopy` methods.

### `put`

```python
wc.put(obj: BaseDataset | ConceptScheme | list[BaseDataset | ConceptScheme] | tuple[...]) -> None
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
wc.put(sc.OpenDataset(identifier="co2-2024", title=I18n(en="CO2 2024"), description=I18n(en="...")))

# Concept scheme (cascades to concept rows)
wc.put(sc.ConceptScheme(
    uri="urn:myorg:themes",
    label=I18n(en="My Themes"),
    concepts={
        "ENV": sc.Concept(notation="ENV", label=I18n(en="Environment")),
    },
))

# Batch — any mix of dataset kinds and concept schemes
wc.put([ds1, ds2, scheme1])
```

**Theme indexing note:** `put(dataset)` writes only the `dataset_theme` bridge
rows. For theme labels to appear in BM25 search results, store the
`ConceptScheme` first so the `concept` table is populated before the index is
built, by `wc.build_index()` or by the seal.

**SDMX structural filters:** `has_code()` and `has_dimensions()` JOIN through
sdmxlib's structural tables via `sdmx_dataflow_urn`. These filters only work for
datasets created by `ingest_sdmx()`.

---

## Reading datasets

The read methods are `EditionView` methods, on `cat.product(id).view()` and on
`wc.view()`.

### `get`

```python
view.get(cls: type[T], identifier: str) -> T | None
```

Direct lookup by identifier. Returns `None` if:

- no dataset with that `identifier` exists, or
- the stored `kind` does not match `cls`.

Type narrowing is strict — requesting `AggregateDataset` for an `OpenDataset`
returns `None`.

```python
ds = view.get(sc.AggregateDataset, "ESTAT:NAMA_10_GDP(1.0)")
if ds is not None:
    print(ds.sdmx_dataflow_urn)
    print(ds.measure_dim)
```

Use `sc.BaseDataset` to retrieve without type filtering:

```python
ds = view.get(sc.BaseDataset, "co2-2024")
```

---

## Querying

### `query`

```python
view.query(cls: type[T], **kwargs) -> QueryBuilder[T]
```

Start a structured query. Returns a `QueryBuilder` — no database round-trip
until `.execute()`, `.all()`, `.first()`, `.one()`, or `.count()` is called.

```python
results = view.query(sc.AggregateDataset).filter(publisher="ESTAT").execute()
```

### `search`

```python
view.search(
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
the edition has no search index, as in a product's first working copy before
`wc.build_index()`, or without the extra; it never raises for a missing index.

```python
results = view.search("labour force survey", kinds=("dataset",), limit=10)
for hit in results.items:
    print(hit.score, hit.payload.identifier, hit.payload.title.get("en"))

# Fuzzy search catches typos and spelling variations
results = view.search("consumr price", fuzzy=1)
```

### Vector and hybrid search — `datasets.find`

```python
view.datasets.find(
    query: str, *, mode: str = "bm25", lang: str = "en", limit: int = 50,
    offset: int = 0, operator: str = "or", fuzzy: int = 0, rrf_k: int = 60,
) -> SearchResult[Hit[BaseDataset]]
```

Dataset search with a choice of evidence: `mode="bm25"` (the default),
`"vector"` for nearest-neighbour search over the embeddings, or `"hybrid"`,
which merges both rankings with Reciprocal Rank Fusion (k=60 by default).
Each `Hit` carries the dataset as `hit.payload`, plus `hit.score` and
`hit.source` (`"bm25"`, `"vector"`, or `"hybrid"` when both matched).

`"vector"` and `"hybrid"` encode `query` with the view's embedding model and
raise `RuntimeError` without one. Pass it as
`cat.product(id).view(embedding_model=model)`; a working copy's view uses the
model given to `working_copy()` or to `wc.build_index()`. `view.concepts`,
`view.codes` and `view.reference_metadata` have the same `find`.

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("intfloat/multilingual-e5-small")  # 384 dims, bilingual
view = cat.product("14100287").view(embedding_model=model)

# Pure semantic
results = view.datasets.find("people moving between provinces", mode="vector")

# Hybrid — recommended for most queries
results = view.datasets.find("labour market monthly data", mode="hybrid", limit=20)
for hit in results.items:
    print(hit.payload.identifier, hit.score, hit.source)

# Hybrid + relational filter via QueryBuilder
import statcatlib.query as q
results = (
    view.query(sc.AggregateDataset)
    .filter(q.has_code("GEO", "ON"), status="current")
    .hybrid_search("unemployment by region")
    .limit(20)
    .execute()
)
```

### `build_index`

```python
wc.build_index(
    embedding_model: EmbeddingModel | None = None,
    progress: Callable[[int, int], None] | None = None,
    *,
    include_codes: bool = True,
    embed_codes: bool = False,
    max_codes_per_codelist: int = 500,
    ...
) -> int
```

A `WorkingCopy` method. Re-index the working copy's datasets, concepts and
codes for search, and optionally build ANN vectors, so `wc.view()` can search
before the seal. If `embedding_model` is provided it is stored for future
vector operations on the working copy.

Returns the number of datasets indexed.

```python
from sentence_transformers import SentenceTransformer
import statcatlib as sc

model = SentenceTransformer("intfloat/multilingual-e5-small")
with sc.Catalog.create("my_catalog", exist_ok=True) as cat:
    with cat.product("14100287").working_copy() as wc:
        ...  # ingest into wc
        n = wc.build_index(model)
        print(f"indexed {n} datasets")
        wc.seal()
```

`wc.seal()` rebuilds the index from the edition's rows with the arguments of
the last `build_index` call, or the defaults when there was none, so a sealed
edition's index holds exactly its rows. To use `datasets.find(mode="vector")`
and `mode="hybrid"` on the sealed edition, pass the model to
`cat.product(id).view(embedding_model=model)`.

`statcat build-index CATALOG PRODUCT [options]` runs `build_index` from the
command line. It resumes the product's working copy, builds its index, and
leaves the working copy unsealed.

Requires the `search` optional extra.

A rebuild is atomic: the new index is written in full and committed in one
step, so an interrupted `build_index()` leaves the previous index intact and
still serving rather than half-replaced.

---

### `index_layout`

```python
view.index_layout -> dict[str, dict[str, Any]]
```

What each language's search index was built with — `{lang: {"table", "rows",
"stemmer"}}`. Empty when no index has been built.

```python
print(view.index_layout)
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
if not view.index_layout:
    raise RuntimeError("search index missing or built by a newer statcatlib")
```

---

### `facets`

```python
view.facets(*fields: str) -> dict[str, dict[str, int]]
```

Aggregation counts across all datasets for each requested field. Useful for
building filter panels in UIs or profiling catalog composition.

Supported fields: `"publisher"`, `"themes"`, `"frequency"`, `"status"`,
`"spatial"`, `"type"`.

A `"spatial"` key is the region's `uri` when it has one, such as a CKAN
`geographic_region` code stored as a concept URI, and its English label
otherwise. `view.spatial_labels(lang)` maps each key to a display label,
read from the region's concept for a `uri` key, and `q.has_spatial_uri(key)`
filters on a `uri` key.

```python
counts = view.facets("publisher", "themes", "frequency")
# {"publisher": {"ESTAT": 42, "ECB": 18}, "themes": {"Labour": 22, ...}}
```

---

## Structural traversal

These `EditionView` methods query sdmxlib's structural tables (codelists,
DSDs, dataflows) in the edition's database.

### `code_map`

```python
view.code_map(dataset_id: str, lang: str = "en") -> dict[str, dict[str, str]]
```

Return all dimension→codes for an `AggregateDataset`. Keys are dimension IDs;
values map each code ID to its label in `lang`.

```python
codes = view.code_map("ESTAT:NAMA_10_GDP(1.0)")
# {"na_item": {"B1GQ": "Gross domestic product at market prices", ...}, "geo": {"AT": "Austria", ...}, ...}
```

### `codelist_for`

```python
view.codelist_for(dataset_id: str, dimension: str) -> sdmxlib.model.codelist.Codelist | None
```

Return the full sdmxlib `Codelist` object for a specific dimension of an
`AggregateDataset`. Returns `None` if not found.

### `get_structure`

```python
view.get_structure(ds: AggregateDataset) -> sdmxlib.model.datastructure.DataStructure | None
```

Return the sdmxlib `DataStructure` (DSD) for an `AggregateDataset`. Returns
`None` if not found.

### `registry`

```python
view.registry -> sdmxlib.EditionRegistry
```

The sdmxlib read surface over the same edition: `get`, `query` and `obs`. Use
this to query sdmxlib structural objects (dataflows, codelists, DSDs) and
observations directly:

```python
reg = view.registry
flow = reg.get(sl.SdmxUrn.parse("urn:sdmx:..."))
```

A working copy's writable registry is `wc.registry`, an `sdmxlib.LocalRegistry`.

---

## Lineage (PROV-O)

### `add_lineage`

```python
wc.add_lineage(
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

A `WorkingCopy` method. Record a PROV-O-aligned derivation edge from `source_id` (upstream) to
`target_id` (downstream). The triple `(source_id, target_id, relationship)`
is unique — calling again with the same triple updates the other fields (upsert).

**`relationship` vocabulary** (see `WorkingCopy.LINEAGE_RELATIONSHIPS`):

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

**`confidence` vocabulary** (see `WorkingCopy.LINEAGE_CONFIDENCE`):
`"asserted"` (manual), `"extracted"` (scraped), `"inferred"` (LLM).

```python
wc.add_lineage(
    "lfs-microdata",
    "ESTAT:UNE_RT_M(1.0)",
    "aggregated_from",
    activity_type="aggregation",
    activity_label="LFS monthly tabulation",
    agent_id="ESTAT",
    confidence="asserted",
)
```

## Language pairs

### `add_language_alternates`

```python
wc.add_language_alternates(
    pairs: Mapping[str, Sequence[str]],
    *,
    source: str,
) -> None
```

A `WorkingCopy` method. Record language tags that a source declares as
alternates of a primary tag.
open.canada.ca's CKAN schema declares `{"en": ["en-t-fr"], "fr": ["fr-t-en"]}`.
`en-t-fr` is an RFC 6497 transformed-content tag, meaning English translated
from French, so text under it is English. statcatlib infers no pairs from the
tags themselves. It records what a source or caller declares, and
`ingest_ckan` records the portal's pairs with the portal's URL as `source`.
Label lookup still matches tags exactly: `title.get("en")` does not return
`en-t-fr` text.

A pair already recorded is kept, its source included. Nothing is written when
any pair is refused:

- `TypeError` when the alternates of a primary are a single string.
- `ValueError` when a tag is empty, an alternate equals its primary, an
  alternate is already recorded under another primary, or a tag would be both
  a primary and an alternate. The last rule keeps the step from alternate to
  primary to one.

```python
wc.add_language_alternates({"en": ["en-t-fr"]}, source="https://open.canada.ca/data")
```

### `language_alternates`

```python
view.language_alternates() -> list[Row]
```

The recorded pairs, one `Row` per alternate tag with `primary_tag`,
`alternate_tag` and `source`, ordered by primary then alternate tag.

```python
for pair in view.language_alternates():
    print(pair.primary_tag, pair.alternate_tag, pair.source)
# en en-t-fr https://open.canada.ca/data
# fr fr-t-en https://open.canada.ca/data
```

### `DatasetScope` — lineage navigation

Lineage traversal and querying is accessed via `view.dataset(id)`:

#### `.lineage_records()`

```python
view.dataset(id).lineage_records(
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
view.dataset(id).upstream(
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
ancestors = view.dataset("ESTAT:UNE_RT_M_SA(1.0)").upstream(depth=5).collect()

# Chain additional operations before executing
publishers = (
    view.dataset("ESTAT:UNE_RT_M_SA(1.0)")
    .upstream()
    .include("publisher")
    .scalars("publisher")
    .values()
)
```

#### `.downstream()`

```python
view.dataset(id).downstream(
    *,
    relationship: str | None = None,
    depth: int = 10,
) -> QueryBuilder[BaseDataset]
```

Recursively traverse `target_id` links downward. Returns a `QueryBuilder` over
all descendant datasets in the catalog.

```python
# Impact analysis: datasets affected by a change to CL_GEO
affected = view.dataset("CL_GEO").downstream(relationship="uses_classification").collect()

# Chain filters after traversal
current_descendants = (
    view.dataset("lfs-microdata")
    .downstream()
    .filter(status="current")
    .collect()
)
```

#### `.related()`

```python
view.dataset(id).related(edge: str) -> QueryBuilder[BaseDataset]
```

Find datasets sharing an entity via the given edge type. Currently supports
`"codelist"` — returns a `QueryBuilder` over datasets that share at least one
codelist with the given dataset.

```python
related = view.dataset("ESTAT:NAMA_10_GDP(1.0)").related("codelist").collect()
```

#### `.codelists`

Navigate to codelists used by this dataset (lazy scope).

```python
view.dataset("housing-stats").codelists.collect()
```

#### `.themes`

Navigate to themes assigned to this dataset (lazy scope).

```python
view.dataset("housing-stats").themes.collect()
```

---

## Batch label resolution

### `CodelistScope.batch_labels`

```python
view.codelist(urn).batch_labels(
    code_ids: list[str],
    lang: str | None = None,
) -> dict[str, str]
```

Resolve labels for many codes in a single SQL query. Returns a dict mapping
`code_id → label`. Falls back to the code ID when the requested language has
no entry. `lang` defaults to the scope's `.lang()` setting.

```python
geo_urn = sc.urn.codelist("SDMX", "CL_GEO")
labels = view.codelist(geo_urn).batch_labels(["CA", "US", "DE"])
# → {"CA": "Canada", "US": "United States", "DE": "Germany"}

# Language override via scope
labels = view.codelist(geo_urn).lang("fr").batch_labels(["CA", "US"])
# Falls back to code_id when French label is absent
```

### `CodelistsScope.batch_labels`

```python
view.codelists.batch_labels(
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
labels = view.codelists.batch_labels(pairs, lang="en")
# → {("urn:...CL_GEO...", "CA"): "Canada",
#    ("urn:...CL_GEO...", "US"): "United States",
#    ("urn:...CL_FREQ...", "A"): "Annual"}
```

---

## QueryBuilder

`QueryBuilder[T]` is returned by `view.query()`. Builders are immutable: each
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
  `distribution`, `has_service`, `publishes`, `serves`, `has_spatial_uri` (and any
  `ExistsFilter` subclass)
- `MapFilter` objects: `has_spatial`, `has_keyword`, `title_contains`,
  `description_contains`

Multiple calls to `.filter()` are also ANDed.

### `.search`

```python
.search(text: str, *, lang: str = "en", operator: str = "or", fuzzy: int = 0) -> QueryBuilder[T]
```

Add BM25 full-text search. Combines with any `.filter()` conditions.

| Parameter  | Description |
|------------|-------------|
| `lang`     | Which language's index to search, as for `view.search()`. A French query needs `lang="fr"` |
| `operator` | `"or"` (default) matches any term; `"and"` requires all terms |
| `fuzzy`    | Levenshtein edit distance for fuzzy matching: `0` (default), `1`, or `2` |

### `.hybrid_search`

```python
.hybrid_search(text: str, *, lang: str = "en") -> QueryBuilder[T]
```

Add hybrid BM25 + vector search (RRF). Requires an embedding model on the view.
Relational filters from `.filter()` are applied on top of the RRF-merged result.
`lang` selects the language's index for both techniques, as for `.search`.

```python
import statcatlib.query as q
results = (
    view.query(sc.AggregateDataset)
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

Request facet counts alongside the result. Same fields as `view.facets()`.
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
view.query(sc.BaseDataset).include("publisher", "themes").all()

# Accumulate across calls
view.query(sc.BaseDataset).include("publisher").include("themes", "keywords").all()
```

Unloaded relationships are set to a special `UNLOADED` sentinel (not `None` or
`[]`). Accessing any attribute, iterating, taking `len()`, or indexing an unloaded
field raises `NotLoadedError`:

```python
ds = view.query(sc.BaseDataset).include("publisher").first()
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
view.query(sc.BaseDataset).filter(...).full().all()
```

Note: `view.get()` always loads all relationships regardless of `.include()`.

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
rows = view.query(sc.BaseDataset).select("identifier", "status").rows()
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
A single SQL query is issued — no `view.get()` call per row.

Selectable columns: `identifier`, `kind`, `status`, `issued`, `modified`,
`landing_page`, `version` (flat), `title`, `description` (MAP — require `lang`).

```python
rows = (
    view.query(sc.BaseDataset)
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
ids = view.query(sc.BaseDataset).filter(status="current").scalars("identifier").values()
# → ["EXR", "M1", "UNEMP", ...]

titles = view.query(sc.BaseDataset).scalars("title", lang="en").values()
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

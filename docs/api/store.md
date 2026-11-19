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
    kind: str | None = None,
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
`resume=True` reopens it. `kind` is `"data"` or `"structure"` (see
[Structure products](#structure-products)); `None` takes the kind the product
is recorded with, or `"data"` for a new product. `lifecycle` is `"revisable"`
or `"final"`. A product keeps the kind and the lifecycle it was first
recorded with: another value raises `InventoryRuleError` with `rule="kind"`
or `rule="lifecycle"` before anything is built. The seal writes the lifecycle
into the manifest's `statcat` section, where the release job reads it for a
product the Inventory does not record yet ([catalog format
§3.1](../catalog-format.md#31-versions)). `embedding_model` and the
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
wc.kind -> str
wc.product_id -> str
wc.edition_id -> str
wc.previous -> str | None
```

`view` returns an `EditionView` over the working copy that sees each write as
it is made.

`seal` turns the working copy into an immutable edition. It runs `checks`
against the working copy's view, brings the search index up to the edition's
rows (see [`build_index`](#build_index)), and
records the edition as the product's live edition. A
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

### Sealing several products

```python
cat.seal(working_copies: Iterable[WorkingCopy], *, checks: Iterable[SealCheck] = ()) -> SealOutcomes
```

`Catalog.seal` seals one working copy per product and records every edition
it can in one Inventory version, so a bulk ingest of N products writes N
leaves and one root. It runs in four passes:

1. The structure pass. For every structure product's working copy, it
   settles the product's journal, runs `checks` against the working copy's
   view, refuses a dataflow, a binding or a partial, and refuses a working
   copy whose base is no longer the live edition. The journal repair leaves
   the batch's other products alone. Then it checks the cross-product rules
   on the pass's working copies together, and seals each one that passed:
   the search index, the `statcat` section, and sdmxlib's journal entry and
   manifest.
2. The structure-check pass. It runs each sealed structure edition's publish
   check, given the batch's other structure editions in place of their
   products' recorded ones, and drops each edition with a finding. It repeats
   until a pass drops none, since a reference may have resolved only in a
   dropped edition.
3. The data pass. It pre-checks every data product's working copy as in the
   structure pass, the cross-product rules given the batch's structure
   editions, and seals each that passed. No data working copy is sealed
   until every one has passed or been refused.
4. It records the sealed editions in one commit, leaving out each product
   whose record sdmxlib's rules or the cross-product rules refuse, and each
   edition whose publish check was given a left-out structure edition.

A refusal in one product never stops another's seal. `SealOutcomes.sealed`
maps each recorded product to its `Edition`, and `SealOutcomes.refused` maps
each refused product to its error:

| Error | When | The working copy |
|---|---|---|
| `SealCheckError` | a check found something | stays open |
| `NonFastForwardError` | its base is no longer live, before the seal | stays open; `discard()` it |
| `CrossProductRuleError` | the cross-product rules refuse it, before the seal | stays open |
| `ManifestError` | sdmxlib refused the edition | closed; `working_copy(resume=True)` reopens it |
| `EditionCheckError` | a structure edition's publish check found something | sealed and unrecorded, logged at WARNING |
| `InventoryRuleError` | sdmxlib's rules or the cross-product rules refused the record, such as a rollback landing during the batch | sealed and unrecorded, logged at WARNING |
| `InventoryContentionError` | the commit lost 8 races in a row | sealed; the product's next `working_copy()` records it |
| `InventoryWriteRefusedError` | the catalog has opted in to expiry, or the newest Inventory version is more than 5 minutes ahead of this machine's clock | stays open when the pre-check refuses it; sealed, with its journal entry kept, when the record does |

```python
with sc.Catalog.create("my_catalog", exist_ok=True) as cat:
    batch = []
    for product_id, source in sources.items():
        wc = cat.product(product_id).working_copy()
        ingest_sdmx(wc, source)
        batch.append(wc)
    outcomes = cat.seal(batch, checks=[checks.conf_status()])
    for product_id, error in outcomes.refused.items():
        print(product_id, error)
```

Every working copy in the batch belongs to this catalog and to a different
product; otherwise `seal` raises `ValueError` before it seals anything. A
crash during a batch can leave its products recorded across several Inventory
versions: each sealed, unrecorded edition keeps its journal entry, and its
product's next `working_copy()` records it in a version of its own. On a
laptop catalog that is acceptable.

### Structure products

A structure product holds one agency's full codelists, concept schemes,
category schemes and hierarchies, and no dataflow, binding, partial or
observation. Start one with `working_copy(kind="structure")`; later working
copies take the recorded kind. Fill it with `fill_structures`:

```python
wc.fill_structures(
    source: sdmxlib.EditionRegistry,
    *,
    families: Collection[type[sdmxlib.MaintainableArtefact]] = (),
    agency: str | None = None,
    ids: Collection[str] = (),
    artefacts: Collection[SdmxUrn | str] = (),
) -> tuple[str, ...]
```

It calls `sdmxlib.edition.fill_structures` and returns the URNs it copied.
An artefact is selected when `artefacts` names its URN, or when its class is
one of `families`, its agency is `agency` and its id matches one of the `ids`
patterns (`SGC*`). `source` is any `sdmxlib.EditionRegistry`, such as a
scratch `LocalRegistry` an FMR's structures were ingested into. Nothing is
copied for what a selected artefact references, so each full artefact has at
most one structure home among live editions; a structure edition entering
`live` with a full artefact another structure product holds live is refused
with `CrossProductRuleError` (`rule="structure-home"`). On a data product's working copy it raises `TypeError`.

StatCan splits its structures by change rate into three products. A
hierarchy goes in the product of the codelist it is built on:

```python
geo = cat.product("structure-geo").working_copy(kind="structure")
geo.fill_structures(fmr, families=[sl.Codelist, sl.Hierarchy], agency="STC", ids=["SGC*", "HCL_SGC*"])
cls = cat.product("structure-classifications").working_copy(kind="structure")
cls.fill_structures(fmr, artefacts=[naics, noc, unit_of_measure, scalar_factor])
con = cat.product("structure-concepts").working_copy(kind="structure")
con.fill_structures(fmr, families=[sl.ConceptScheme], agency="STC")
outcomes = cat.seal([geo, cls, con])
```

The seal of a structure working copy refuses, with `SealCheckError`, a
dataflow, a binding or a partial it holds, and the working copy stays open.
After sdmxlib's seal, it runs `sdmxlib.edition.check_edition` at `publish`
with the catalog's other structure products' live and embargoed editions as
`structures=`, so a concept whose core representation names a codelist in
`structure-classifications` resolves there. A finding raises
`EditionCheckError` (with `findings`, `product` and `edition`) and leaves
the sealed edition unrecorded; `statcat doctor` reports it. Seal the three
products in one `Catalog.seal`, in any order, to record them in one Inventory
version.

### Partials in data products

A data product holds its dataflows' closures, with each codelist cut to a
partial: the codes the product's observations and allowed constraints use,
with their ancestors. Fill its working copy with `fill_dataflows`, or with
[`ingest_dataflows`](sources.md#ingest_dataflows), which also stores a
dataset per dataflow:

```python
wc.fill_dataflows(
    source: sdmxlib.RestRegistry | sdmxlib.EditionRegistry,
    dataflows: Sequence[SdmxUrn[Dataflow] | str],
    *,
    observations: Mapping[str, WriteSource] | None = None,
    partials: bool = True,
) -> dict[str, str]
```

It calls `sdmxlib.edition.fill_edition` and returns the fingerprint of the
full codelist each partial was cut from, by URN. The working copy keeps that
map in `statcat._meta.partial_of`, so a seal after
`working_copy(resume=True)`, in this process or another, records each
partial's `partial_of` in the manifest. A republish built on the live edition
keeps the other dataflows' bindings and cuts a shared codelist to the codes
every binding uses; a seal that fills nothing keeps every partial's
`partial_of` from the previous manifest. `partials=False` copies each
codelist full. On a structure product's working copy it raises `TypeError`.

!!! warning "Fill through `fill_dataflows`"
    `sdmxlib.edition.fill_edition(wc.registry, ...)` cuts the codelists but
    leaves the map out of `statcat._meta`. A partial cut that way, whose
    previous edition records a `partial_of`, fails the seal with
    `SealCheckError`, and so does a working copy whose `fill_dataflows` call
    stopped before it recorded its cuts; call `fill_dataflows` again.

The seal of an edition with partials runs the publish check with every
structure product's live and embargoed editions as `structures=`: each
partial must match the full codelist its `partial_of` names, in any of them,
and hold no code that codelist lacks. A finding raises `EditionCheckError`
and leaves the edition unrecorded.

Before sdmxlib's seal, the cross-product pre-check refuses with
`CrossProductRuleError`, and leaves the working copy open, a data edition
that holds a cut codelist no structure product holds (`rule="full-artefacts"`,
naming the URN: add it to a structure product, or fill with
`partials=False`), a partial or a full artefact matching no live or embargoed
edition of its structure home, and, with embargo off, one matching only an
embargoed structure edition (`rule="publish-order"`). See
[Catalog administration](admin.md#publish-order-and-the-cross-product-rules). `Catalog.seal` checks a batch's data editions against the
batch's structure editions, so a structure product and the tables cut from
it go live in one Inventory version.

`statcatlib.testing.codr_structures` and `codr_product` build such a catalog
from a seed, for tests and benchmarks. `codr_working_copy` fills a data
product's working copy without sealing it, for a `Catalog.seal` batch, and
`codr_corpus` seals groups of one-table and multi-table products, a share of
them under embargo; `bench/corpus.py` builds the Phase 4 corpus with it.
`inventory_corpus` writes Inventory documents alone, for measuring the
Inventory read; `bench/inventory_read.py` does that over Azurite.

---

## The Inventory on object storage

`InventoryWriter` commits Inventory versions to a catalog in object storage
through `BlobInventoryStorage` (`statcatlib[blob]`). Working copies and seals
stay on local disk. Tests, benches and `CatalogAdmin` write versions there
([Catalogs in object storage](admin.md#catalogs-in-object-storage)). A `commit` with an entry that becomes live or
embargoed runs the cross-product rules, which read that edition's manifest
through the storage (`ManifestArtefacts(storage.read)`) and every other
product's records from its leaf entry; a missing or altered manifest raises
`ManifestError`, and nothing is written.

```python
from obstore.store import AzureStore
from statcatlib import BlobLocation, TokenIdentity
from statcatlib.store.blob import BlobInventoryStorage
from statcatlib.store.inventory import InventoryWriter

identity = TokenIdentity(provider)  # returns {"token": ..., "expires_at": ...}
store = AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=identity)
writer = InventoryWriter(BlobInventoryStorage(BlobLocation("az://catalogs/stc/", store)))
writer.write_catalog_json()
```

A blob writer follows the local writer's rules, and writes in these ways:

| Document | Write | Requests for a one-product change |
|---|---|---|
| A leaf the version changes | One unconditional Put, with no existence check. A carried-forward leaf gets none. | 1 |
| A root | One Put with `If-None-Match: *`, at any size. | 1 |
| `CURRENT` | A read, then one Put conditional on its `e_tag`, moving only forward. | 1 |

The root is the commit point. A `CURRENT` that fails to move logs a WARNING, the
commit stands, and readers probe forward from the old value.

A create whose outcome is unknown, as after a timeout, reads its name back. Equal
bytes mean the writer won, other bytes raise `InventoryConflictError` from
`try_commit`, and an absent name is tried once more. If that fails too,
`InventoryCommitUnknownError` names the root, and the caller re-reads the head.

The writer takes its time from the `Date` header of an anonymous `HEAD` at the
store's endpoint. It raises `InventoryWriteRefusedError` when the header is
missing or malformed, the request fails, or this machine's clock runs more than
30 s ahead of it, naming both times. A clock behind the storage's passes.

`event.actor` is the `oid` claim of a `TokenIdentity`'s token. A store with no
`TokenIdentity`, or a token with no `oid`, takes `actor=` from
`BlobInventoryStorage`, recorded verbatim, and with neither the writer raises
`InventoryWriteRefusedError` before it writes anything.

A `catalog.json` at format 2.x is refused with `InventoryWriteRefusedError`, as
on local disk. Any other store fault raises `statcatlib.store.blob.BlobStoreError`,
naming the object.

[Object storage](blob.md) describes `statcat upload`, which copies a sealed edition
to a catalog in Azure Blob Storage, and [reading a catalog](blob.md#reading-a-catalog)
with `Catalog.open`, with its cache and the embargo boundary.

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
    lang: str,
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
| `lang`     | Required: the one tag whose table to search. Each language has its own table, so a French query needs `lang="fr"`. An alternate tag such as `en-t-fr` reads its primary's table. A sequence of tags raises `TypeError` |
| `operator` | `"or"` (default) matches any term; `"and"` requires all terms |
| `fuzzy`    | Levenshtein edit distance for fuzzy matching: `0` (default), `1`, or `2` |
| `rrf_k`    | Reciprocal-Rank-Fusion constant for merging across kinds |

Requires LanceDB (`statcatlib[search]`). Returns an empty `SearchResult` when
the edition has no search index, as in a product's first working copy before
`wc.build_index()`, or without the extra; it never raises for a missing index.
It raises `IncompatibleCatalogError` for an index of format 3, written by
statcatlib 0.20 or earlier, until the product is re-indexed
([Upgrading](../upgrading.md#stored-data)).

Each hit's `lang` is the stored tag of the row that matched, which can be an
alternate of the tag searched: a search with `lang="en"` on open.canada.ca
returns `en-t-fr` hits.

```python
results = view.search("labour force survey", kinds=("dataset",), lang="en", limit=10)
for hit in results.items:
    print(hit.score, hit.lang, hit.payload.identifier, hit.payload.title.get(hit.lang))

# Fuzzy search catches typos and spelling variations
results = view.search("consumr price", lang="en", fuzzy=1)
```

### Hits

| Hit payload | Fields |
| --- | --- |
| `CodeHit` | `dataset_id`, `dimension_id`, `dimension_label: LocalisedString \| None`, `code_id`, `code_label: LocalisedString`, `document`, `code_urn`, `codelist_urn`, `parent_id`, `depth` |
| `ConceptHit` | `concept_urn`, `scheme_urn`, `concept_id`, `name: LocalisedString \| None`, `description: LocalisedString \| None`, `document` |
| `ReferenceMetadataHit` | `set_urn`, `attribute_path`, `section_title`, `document`, `chunk_index` |

A dataset hit's payload is the dataset. `document` is the text the row was
indexed under. A hit field's `LocalisedString` carries the row's tag, and a
field the row holds no text for is `None`.

### Vector and hybrid search — `datasets.find`

```python
view.datasets.find(
    query: str, *, mode: str = "bm25", lang: str, limit: int = 50,
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
results = view.datasets.find("people moving between provinces", mode="vector", lang="en")

# Hybrid — recommended for most queries
results = view.datasets.find("labour market monthly data", mode="hybrid", lang="en", limit=20)
for hit in results.items:
    print(hit.payload.identifier, hit.score, hit.source, hit.lang)

# Hybrid + relational filter via QueryBuilder
import statcatlib.query as q
results = (
    view.query(sc.AggregateDataset)
    .filter(q.has_code("GEO", "ON"), status="current")
    .hybrid_search("unemployment by region", lang="en")
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

`build_index` records its options in the working copy's
`statcat._meta.index_options`, all but `embedding_model` and `progress`, and
a fingerprint of the edition's tables in `statcat._meta.index_built_from`.
`wc.seal()` keeps the index when the tables still have that fingerprint, in
the same process or a later one, such as a seal after `statcat build-index`.
Otherwise it rebuilds the index from the edition's rows: with the last
`build_index` call on the same object, with the recorded options and the
encoder given to `working_copy(embedding_model=)` in another process, or with
the defaults when there was no call. A sealed edition's index holds exactly
its rows. A new working copy starts with neither key.

Each embedded row records `text_sha256`, the SHA-256 of the model name, the
document prefix and the text. When `build_index` is called with `model_name`,
it reads the stored vectors before the rebuild replaces the tables and gives
each text whose hash matches its stored vector; only the other texts reach
the model. A seal that rebuilds repeats the last `build_index` call, so a
working copy that calls `wc.build_index(model_name=...)` and changes no text
calls `encode` zero times, and one with edits encodes only the edited texts. A
change of model name or document prefix re-encodes every text.

Without `model_name`, `build_index` reuses nothing and encodes every text,
since the encoder may be a different model from the one that wrote the
stored vectors; it logs that at INFO. This includes a seal of a working copy
that never called `build_index`. The rows still record `text_sha256` under
the model name `embeddings.json` records, so a later build that passes
`model_name` can reuse them. To use `datasets.find(mode="vector")`
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
"stemmer"}}`, keyed by `sdmxlib.tag_alias` of each primary or unpaired tag; an
alternate tag's rows count in its primary's entry. Empty when no index has been
built.

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

An empty layout means the edition has no index to search. `Product.view`
refuses an index format this build does not read with
`IncompatibleCatalogError`. A format 3 index opens, reports its layout, and
raises on every search.

```python
if not view.index_layout:
    raise RuntimeError("search index missing")
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

A `"spatial"` key is in no language. It is the region's `uri` when it has
one, such as a CKAN `geographic_region` code stored as a concept URI, and
otherwise `sha256:` followed by the SHA-256 of the region's whole label map.
Regions that differ in any translation get separate keys, and a region's key
changes when a later edition adds or edits one of its translations. A region
with neither a `uri` nor a label text has no key and no facet entry.
`view.spatial_labels(lang=...)` maps each key to a display label, read from
the region's concept for a `uri` key, and `q.has_spatial_key(key)` filters on
either kind of key.

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
view.code_map(dataset_id: str, *, lang: Langs) -> dict[str, Labels[str]]
```

Return all dimension→codes for an `AggregateDataset`. Keys are dimension IDs;
each value is a `Labels` mapping each code ID to its label in the first of
`lang` the code's name holds, or `None`, with a `Gap` for each such code in its
`gaps`.

```python
codes = view.code_map("ESTAT:NAMA_10_GDP(1.0)", lang="en")
codes["geo"]["AT"]        # LocalisedString(text="Austria", lang="en")
codes["geo"]["AT"].text   # "Austria"
codes["geo"].gaps         # () when every code has an English name
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
`en-t-fr` text. `view.preference(lang="en")` gives `("en", "en-t-fr")`, a tag
tuple to pass as a label call's `lang` so that it answers from either tag.
Search indexes an alternate tag's text in its primary's table, so
`view.search(..., lang="en")` finds `en-t-fr` text
([How search works](../search.md#one-table-per-language)).

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

## Label lookup

Every label call follows the same rules
([`statcatlib.labels`](../reference/labels.md), and the design
[Label lookup](../design/label-lookup.md)):

- **`lang` is required and keyword-only.** It is a tag or a sequence of tags
  (`sc.Langs`). Each tag is normalised; the first tag the stored text holds
  answers, and a held `""` answers. An empty sequence raises `ValueError`, a
  non-`str` tag `TypeError`, and a malformed tag `sdmxlib.InvalidTagError`.
- **A label is a `LocalisedString`.** Read the text as `.text` and the tag
  that answered as `.lang`. A `Row` column holds the text, with the tag in a
  `__lang` column beside it.
- **A single lookup raises** `MissingTranslationError`, a `KeyError` and an
  `sdmxlib.MissingTagError`, when the entity exists and its required label
  holds none of the tags. An unknown entity returns `None`.
- **A bulk call returns `None`** for each missing label, and lists it in the
  result's `gaps` as a `Gap(key, entity, field, langs)`. `Labels` is the
  mapping result, `RowList` the `Row` list and `ValueList` the scalar list.
  `json.dumps` and `msgspec.json.encode` both refuse a `Labels`: encode
  `dict(labels)` with `msgspec.json.encode`, which writes each
  `LocalisedString` as `{"text": …, "lang": …}`, and use
  `msgspec.structs.asdict(label)` for one label.

```python
labels = view.agent_labels(lang=view.preference(lang="fr"))
for agent_id, label in labels.items():
    print(agent_id, label.text if label else "—")
print(labels.gaps)   # (Gap(key="X", entity="X", field="name", langs=("fr", "fr-t-en")), ...)
```

### Label maps on the view

```python
view.agent_labels(*, lang: Langs) -> Labels[str]                          # agent ID → name, by ID
view.concept_labels(*, lang: Langs, scheme_uri: str | None = None) -> Labels[str]  # concept URI → label, by URI
view.spatial_labels(*, lang: Langs) -> Labels[str]                        # spatial facet key → label, by key
```

Each maps every entity to its label, `None` and a gap where it holds none of
the tags. A spatial key's label comes from the region's concept, for a `uri`
key, and from every coverage under the key; a tag earlier in `lang` wins, and
within one tag the concept wins over a coverage.

### `preference` and `language_counts`

```python
view.preference(*, lang: str) -> tuple[str, ...]
view.language_counts() -> list[Row]
```

`preference` gives a tag with its language pairs, for a label call's `lang`.
For a primary tag it is the tag, then its alternates in tag order; for an
alternate it is the tag, its primary, then the primary's other alternates; for
a tag in no pair it is the tag alone. On open.canada.ca,
`view.preference(lang="en")` is `("en", "en-t-fr")`. A display UI that shows
untagged text and code labels last can pass
`(*view.preference(lang="fr"), "und", "zxx")`.

`language_counts` gives one `Row` per stored text column and tag, with
`table` (schema-qualified, such as `statcat.dataset`), `column`, `lang` and
`texts`, the number of texts held under the tag. It covers every MAP column of
the `statcat` schema except `dataset.extras`, and the code names, concept
names and descriptions, and localised texts of the `sdmx` schema.

### Single lookups

```python
view.codelist(urn).label(*, lang: Langs) -> LocalisedString | None
view.codes.get(code_urn: str, *, lang: Langs) -> CodeHit | None
view.concepts.get(concept_urn: str, *, lang: Langs) -> ConceptHit | None
```

`label` returns the codelist's name, or `None` when the edition holds no
codelist with that URN. `codes.get` and `concepts.get` return the hit
payload with `document` empty and no search row read. A code's or a concept's
name raises `MissingTranslationError` when it holds none of the tags; a
concept's description is `None` when absent, and may answer in another tag
than its name.

```python
view.codelist(geo_urn).label(lang="fr")   # LocalisedString(text="Géographie", lang="fr")
try:
    hit = view.codes.get(code_urn, lang="de")
except sc.MissingTranslationError as err:
    print(err)   # no text for 'de' in name of urn:…; held: 'en', 'fr'
```

### Codelist and scheme scopes

```python
view.codelists.filter(*, text_contains: str, lang: Langs) -> CodelistsScope
view.codelists.enrich(...).rows(*, lang: Langs) -> RowList
view.codelist(urn).enrich(...).rows(*, lang: Langs) -> RowList
view.themes[scheme_uri].labels(*, lang: Langs) -> Labels[str]
view.themes[scheme_uri].rows(*, lang: Langs) -> RowList
```

The codelist scopes have no `.lang()` setter; each terminal takes `lang`.
`rows` gives `label` and `label__lang` beside the enrich columns, `None` and a
gap where the name holds none of the tags, and an empty `RowList` when the
edition has no `sdmx` schema. `filter` keeps codelists whose name, in the
first tag it holds, or whose URN contains the text. `labels` maps every
concept's notation to its label, ordered by notation.

```python
rows = view.codelists.enrich(n=sc.each("datasets").count()).rows(lang="en")
# → [Row({"urn": "...", "label": "Geography", "label__lang": "en", "n": 5}), ...]
```

### Batch label resolution

#### `CodelistScope.batch_labels`

```python
view.codelist(urn).batch_labels(
    code_ids: list[str],
    *,
    lang: Langs,
) -> Labels[str]
```

Resolve labels for many codes in a single SQL query. Returns a `Labels`
mapping each `code_id` to its `LocalisedString`, in request order with
duplicates collapsed. A code whose name holds none of the tags maps to `None`
and has a `Gap` in `labels.gaps`. A code the codelist does not hold raises
`UnknownCodeError`, whose `missing` lists every such `(codelist URN, code ID)`
pair.

```python
geo_urn = sc.urn.codelist("SDMX", "CL_GEO")
labels = view.codelist(geo_urn).batch_labels(["CA", "US", "DE"], lang="en")
labels["CA"]   # LocalisedString(text="Canada", lang="en")

# French first, then English
labels = view.codelist(geo_urn).batch_labels(["CA", "US"], lang=("fr", "en"))
for code_id, label in labels.items():
    print(code_id, label.text if label else None, label.lang if label else None)
for gap in labels.gaps:
    print("no label for", gap.key, "in", gap.langs)
```

#### `CodelistsScope.batch_labels`

```python
view.codelists.batch_labels(
    pairs: list[tuple[str, str]],
    *,
    lang: Langs,
) -> Labels[tuple[str, str]]
```

Resolve labels across multiple codelists in one query. Accepts a list of
`(codelist_urn, code_id)` tuples and returns a `Labels` mapping each pair to
its label, with the rules of `CodelistScope.batch_labels`.

```python
pairs = [
    (sc.urn.codelist("SDMX", "CL_GEO"), "CA"),
    (sc.urn.codelist("SDMX", "CL_GEO"), "US"),
    (sc.urn.codelist("SDMX", "CL_FREQ"), "A"),
]
labels = view.codelists.batch_labels(pairs, lang="en")
# → Labels({("urn:...CL_GEO...", "CA"): LocalisedString(text="Canada", lang="en"),
#           ("urn:...CL_GEO...", "US"): LocalisedString(text="United States", lang="en"),
#           ("urn:...CL_FREQ...", "A"): LocalisedString(text="Annual", lang="en")})
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
  `distribution`, `has_service`, `publishes`, `serves`, `has_spatial_key` (and any
  `ExistsFilter` subclass)
- `MapFilter` objects: `has_spatial`, `has_keyword`, `title_contains`,
  `description_contains`. Each takes a required `lang`, a tag or a sequence
  of tags, and tests the text of the first tag the stored map holds.

Multiple calls to `.filter()` are also ANDed.

### `.search`

```python
.search(text: str, *, lang: str, operator: str = "or", fuzzy: int = 0) -> QueryBuilder[T]
```

Add BM25 full-text search. Combines with any `.filter()` conditions.

| Parameter  | Description |
|------------|-------------|
| `lang`     | Required: the one tag whose table to search, as for `view.search()`. A French query needs `lang="fr"` |
| `operator` | `"or"` (default) matches any term; `"and"` requires all terms |
| `fuzzy`    | Levenshtein edit distance for fuzzy matching: `0` (default), `1`, or `2` |

### `.hybrid_search`

```python
.hybrid_search(text: str, *, lang: str) -> QueryBuilder[T]
```

Add hybrid BM25 + vector search (RRF). Requires an embedding model on the view.
Relational filters from `.filter()` are applied on top of the RRF-merged result.
`lang` selects the language's index for both techniques, as for `.search`.
`execute()` returns a `SearchResult` whose `langs` maps each dataset
identifier to the stored tag of its matched row.

```python
import statcatlib.query as q
results = (
    view.query(sc.AggregateDataset)
    .filter(status="current")
    .filter(q.has_code("GEO", "ON"))
    .hybrid_search("unemployment trends", lang="en")
    .limit(20)
    .execute()
)
```

### `.sort_by`

```python
.sort_by(field: str, *, desc: bool = False, nulls: str | None = None, lang: Langs | None = None) -> QueryBuilder[T]
```

Sort by a flat column. Valid fields: `identifier`, `status`, `frequency_id`,
`issued`, `modified`. A MAP column such as `title` needs `lang`, and sorts on
the text of the first tag each row holds, so one order can mix languages.

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
.rows()    -> RowList            # projected rows, with .gaps
.row()     -> Row | None         # first projected row or None
```

Scalar terminals (require `.scalars()` — return bare values):

```python
.values()  -> ValueList          # flat scalar values, with .gaps
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
.select(*columns: str, lang: Langs | None = None) -> QueryBuilder[T]
```

Set row projection mode. Use `.rows()` or `.row()` as the terminal.
A single SQL query is issued — no `view.get()` call per row.

Selectable columns: `identifier`, `kind`, `status`, `issued`, `modified`,
`landing_page`, `version` (flat), `title`, `description` (MAP — require `lang`).
A MAP column gives two `Row` columns: `title`, the text of the first tag of
`lang` the row holds, or `None`, and `title__lang`, that tag. `.rows()`
returns a `RowList` whose `gaps` lists each row with no text in any of the
tags. `.row()` returns `rows()[0]` and drops the gaps.

```python
rows = (
    view.query(sc.BaseDataset)
    .filter(status="current")
    .sort_by("title", lang="en")
    .select("identifier", "title", lang="en")
    .rows()
)
# → [Row({"identifier": "EXR", "title": "Exchange Rates", "title__lang": "en"}), ...]

rows[0].identifier   # "EXR"
rows[0]["title"]     # "Exchange Rates"
rows.gaps            # one Gap per row with no English title
```

Raises `ValueError` for unrecognised columns or MAP columns missing `lang`.

### `.scalars`

```python
.scalars(column: str, *, lang: Langs | None = None) -> QueryBuilder[T]
```

Set scalar projection mode. Use `.values()` or `.value()` as the terminal.
Single SQL query. A MAP column yields a `LocalisedString`, or `None` for a row
with no text in any of the tags; `.values()` returns a `ValueList` whose
`gaps` lists those rows. `.value()` returns `None` both for no row and for no
text; use `.values()` to tell them apart.

```python
ids = view.query(sc.BaseDataset).filter(status="current").scalars("identifier").values()
# → ["EXR", "M1", "UNEMP", ...]

titles = view.query(sc.BaseDataset).scalars("title", lang=("en", "fr")).values()
# → [LocalisedString(text="Exchange Rates", lang="en"), ...]
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
    langs: dict[str, str] = Factory(dict)
```

- `items` — the dataset objects for the current page
- `total` — total matching datasets (may exceed `len(items)` when paginating)
- `offset` — the offset this page starts at
- `facets` — populated when `.with_facets()` was used; otherwise empty dict
- `langs` — for a `QueryBuilder` `.search()` or `.hybrid_search()`, each
  item's identifier mapped to the stored tag of its matched row; otherwise
  empty

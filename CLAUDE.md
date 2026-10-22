# statcatlib

A Python library for building queryable open data catalog engines.
Designed for developers working with statistical and open data catalogs
(DCAT-AP, StatDCAT-AP, CKAN, SDMX, PROV-O).

## Project philosophy

- **The catalog is the materialized graph.** Rather than federated querying
  across separate stores, ingest pulls the full structural model into one
  DuckDB database. SQL JOINs are the graph traversal. URNs are the links
  (exactly like RDF URIs, but as SQL foreign keys).
- **No duplication — only links.** statcatlib owns the catalog star schema.
  sdmxlib owns the SDMX structural tables (codelists, DSDs, dataflows).
  statcatlib's `Dataset` links to sdmxlib's tables via `sdmx_dataflow_urn`;
  it does not copy or re-model what sdmxlib already represents.
- **Extensible by design.** Any future model with a DuckDB representation
  (GIS, unit-of-measure ontology, etc.) follows the same pattern: ingest its
  tables into the shared database, add a reference column to `datasets`, JOIN
  to query. No architecture changes needed.
- **Influenced by DCAT, not beholden to it.** We borrow DCAT-AP's vocabulary
  and StatDCAT-AP's statistical extensions, but the internal representation is
  Pythonic attrs classes — no RDF runtime, no triple stores.
- **sdmxlib is a dependency, not the other way around.** sdmxlib knows nothing
  about statcatlib. sdmxlib's Python model types are used in `sources/sdmx.py`
  during ingest; they do not appear in the catalog domain model.
- **Query surface matters as much as the model.** This library powers chatbots,
  CLI tools, and analytical workflows. The query API must support full-text
  search, faceted filtering, structural queries (dimensions, codes), and
  aggregation counts.

## Architecture

```
src/
    statcatlib/
    ├── model/          # Domain model — attrs classes, no sdmxlib imports
    │   ├── __init__.py
    │   ├── catalog.py       # Catalog, CatalogRecord
    │   ├── dataset.py       # Dataset, StatisticalDataset
    │   ├── distribution.py  # Distribution, DataService
    │   ├── agents.py        # Agent, ContactPoint
    │   ├── temporal.py      # TemporalCoverage, Frequency
    │   ├── spatial.py       # SpatialCoverage
    │   ├── classification.py # Concept, Standard
    │   ├── quality.py       # QualityAnnotation, ProvenanceStatement
    │   ├── licence.py       # LicenceDocument
    │   ├── skos.py          # Concept, ConceptRef, ConceptScheme
    │   └── types.py         # Shared types, InternationalString re-export
    ├── store/          # DuckDB-backed query engine
    │   ├── __init__.py
    │   ├── store.py         # Catalog
    │   ├── schema.py        # DDL: catalog namespace, MAP columns
    │   ├── lance.py         # LanceDB search index (BM25 + vector)
    │   ├── scopes.py        # Scope-based graph traversal
    │   ├── row.py           # Row — dict returned by .select() projections
    │   ├── sentinel.py      # UNLOADED sentinels for selective loading
    │   └── xhtml.py         # XHTML → markdown for reference metadata
    ├── sources/        # Connectors that ingest into the catalog
    │   ├── __init__.py
    │   ├── sdmx.py          # ingest_sdmx() — writes sdmxlib tables + catalog record
    │   └── ckan.py          # CkanClient, ingest_ckan()
    ├── query.py        # Composable filter objects (q.has_code, q.within, ...)
    ├── expr.py         # Sub-traversal expressions for scopes (sc.each)
    ├── bundle.py       # Portable bundles — layout-aware data_path policies
    ├── urn.py          # Convenience URN builders for SDMX artefacts
    └── __init__.py     # Public API re-exports
```

## Key design decisions

### Domain model

- **Plain attrs inheritance** for dataset type hierarchy:
  `BaseDataset` → `OpenDataset`, `AggregateDataset`, `MicrodataDataset`, `GeospatialDataset`, `PublicationDataset`. Use `DatasetT = TypeVar("DatasetT", bound=BaseDataset)`
  for generic code on `get()` and `query()`.
- **`AggregateDataset` links, does not embed.** It carries `sdmx_dataflow_urn`
  as a foreign key into sdmxlib's `dataflows` table — not copies of the DSD,
  codelist, or dimension data. Structural queries traverse the link via SQL JOIN.
- **`InternationalString`** reused from sdmxlib for all multilingual fields.
  This is a pure value type with no SDMX-specific coupling.
- All domain objects use `@define` (mutable). Use `attrs.evolve()` for copies.
- Fields that reference controlled vocabularies use `Concept` (a lightweight
  id + label type), not raw strings.

### Query surface

The `Catalog` is a single concrete class backed by DuckDB. No ABC, no
separate implementations. DuckDB supports both in-memory and persistent storage:

```python
cat = Catalog()                    # in-memory (DuckDB :memory:)
cat = Catalog("catalog_folder")    # persistent folder-based
```

**Construction is cheap and does not open the database.** `Catalog(uri)` only
stashes the URI and configuration; the DuckDB connection opens on `.open()`
or on entering a `with` block. Querying an unopened catalog raises
`RuntimeError`. Always use it as a context manager:

```python
with Catalog("catalog_folder") as cat:
    cat.query(AggregateDataset).limit(5).collect()
```

`Catalog(path).open()` is open-or-create: it creates the catalog folder if it
does not exist and opens it otherwise. Use `Catalog.from_connection(conn)` to
wrap an existing DuckDB connection (e.g. one shared with sdmxlib).

Four query operations:

1. `get(cls, id)` → direct lookup by identifier (generic, narrows return type)
2. `query(cls, **filters)` → structured query, returns `QueryBuilder` for
   chaining (generic, narrows return type)
3. `search(text)` → BM25 search via LanceDB (requires folder-mode with `statcatlib[search]`)
4. `facets(*fields)` → aggregation counts per field value (not generic,
   counts across all datasets)

`search()` and `facets()` are intentionally not generic — searching and
counting should span the whole catalog. Use `query(AggregateDataset)` when
you need type-narrowed results.

`QueryBuilder` supports chaining: `.filter()`, `.search()`, `.include()`,
`.full()`, `.sort_by()`, `.limit()`, `.offset()`, `.with_facets()`, then
terminal `.execute()` → `SearchResult[DatasetT]`.

**Selective relationship loading** — `.include(*rels)` declares which
relationships to batch-load. Default (no `.include()`) loads all ~17. First
`.include()` call switches to a whitelist; subsequent calls accumulate.
`.full()` resets to loading everything. `get()` always loads all relationships
regardless. Valid names match model field names: `publisher`, `contact_point`,
`frequency`, `licence`, `themes`, `subject`, `dataset_type`, `keywords`,
`spatial_coverage`, `distributions`, `conforms_to`, `quality_annotations`,
`provenance`, `dimension_names`, `variables`, `feature_types`, `authors`.
Defined in `VALID_INCLUDE_RELS`, exported as `sc.VALID_INCLUDE_RELS`. Unknown names raise `ValueError`.

**UNLOADED sentinel** — fields not in `include_rels` are set to a
`_UnloadedType` sentinel (`make_unloaded(field)`, cached per field name in
`store/sentinel.py`). Accessing any attribute, iterating, `len()`, or
indexing raises `NotLoadedError(field)` (subclass of `AttributeError`).
`__bool__` returns `False` so `if ds.publisher:` works without raising.
FK-null special case: if the FK column is NULL in the main row, the field
returns `None` regardless of `include_rels`. `is_unloaded(value)` and
`NotLoadedError` are exported from `statcatlib.__all__`.

**Projection modifiers** bypass full object reconstruction:
- `.select(*cols, lang=)` — sets `Row` projection. Use `.rows()` / `.row()`
  as terminals (return `list[Row]` / `Row | None`). Single SQL query, no
  `store.get()`. `Row` is a `dict` subclass in `store/row.py` that adds
  attribute-style access (`row.identifier`). Exported as `sc.Row`.
- `.scalars(col, lang=)` — sets flat value projection. Use `.values()` /
  `.value()` as terminals (return `list[Any]` / `Any | None`).
- Entity terminals (`.collect()`, `.first()`, `.one()`, `.execute()`) raise
  `ValueError` when a projection modifier is active.
- `.exists()` — terminal returning `bool` via `SELECT 1 ... LIMIT 1`.
- Allowed columns: `_SELECTABLE_COLUMNS` (identifier, kind, status, issued,
  modified, landing_page, version) and `_MAP_SELECTABLE_COLUMNS` (title,
  description — require `lang` parameter).

`.sort_by()` supports MAP columns (e.g. `title`) via the `lang` parameter:
`.sort_by("title", lang="en")`. Uses DuckDB subscript syntax (`d.title[?]`).

Structural filters (e.g. `has_code=("GEO", "ON")`) are resolved via the
`dataset_code` bridge table, which JOINs into sdmxlib's tables when needed.

**Hierarchy descent** — `q.within(dim, root)` and
`q.has_code(dim, q.descendants_of(root))` match any code beneath *root*.
Descent is **strict** (the root itself does not match) with
`include_self=True` to widen it, matching sdmxlib's `q.hcode.ancestors`.

Which Hierarchy applies is sdmxlib's answer, not statcatlib's: the filter
projects `sl.q.hierarchy_applicability` — a `Frame`, so a `SqlRelation` —
and splices it as one `LEFT JOIN` on `dataflow_urn`. Nothing is
enumerated client-side. statcatlib owns exactly one decision here, the
fallback: where no HierarchyAssociation applies, it reads the codelist's
intrinsic parent chain from `sdmx.code.ancestor_ids`. That is a statcatlib
policy, not an SDMX rule, which is why it lives in `query.py` and the
rest does not. Do not reintroduce hierarchy traversal or URN string
surgery here — that was deleted deliberately (sdmxlib#372).

MAP column filters in `statcatlib.query`:
- `MapCondition` — condition on a MAP column inside a bridge-table
  `ExistsFilter` (e.g. `dataset_spatial.label`, `dataset_keyword.keyword`).
  Uses `col[?] = ?` subscript syntax.
- `MapFilter` — direct WHERE clause on a dataset MAP column
  (e.g. `d.title[?] ILIKE ?`). Supports `eq` and `ilike` operators.
- Convenience functions: `has_spatial(label, lang=)`, `has_keyword(kw, lang=)`,
  `title_contains(text, lang=)`, `description_contains(text, lang=)`.

Scope-based traversal supports `CodelistsScope` (via `store.codelists`
property) parallel to `ThemesScope` (`store.themes`). Both support
`.enrich()` with `sc.each("datasets")` expressions. `CodelistScope` also
has `.label(lang)` for quick name lookup via `sdmx.localized_text`.

`CodelistsScope` supports `.lang(lang)` and `.filter(text_contains=)` for
SQL-level label resolution and text filtering. Enriched `.collect()` always
includes a `label` field (resolved via `sdmx.localized_text`).

`CodelistScope` supports `.lang(lang)`. Enriched `.collect()` always
includes a `label` field from the code's `name` MAP column (falls back
to `code_id` when the requested language is missing).

**Batch label resolution** — `CodelistScope.batch_labels(code_ids, lang=)`
resolves labels for many codes in a single SQL query, returning
`dict[str, str]`. `CodelistsScope.batch_labels(pairs, lang=)` does the same
across multiple codelists using a VALUES JOIN, returning
`dict[tuple[str, str], str]` keyed by `(codelist_urn, code_id)`. Both
methods fall back to the code ID when the language has no entry.

**Lazy graph traversal** — `DatasetScope.upstream()`, `.downstream()`, and
`.related(edge)` return `QueryBuilder[BaseDataset]` (not `list[BaseDataset]`).
They collect the matching IDs via SQL, then call
`self._store.query(BaseDataset).from_ids(identifiers)`. Callers chain
`.filter()`, `.include()`, `.sort_by()` etc. before calling a terminal.

### Storage — shared DuckDB database

One DuckDB file, two schema owners:

- **`statcat` schema** — catalog star schema: `dataset`, `agent`, `contact`,
  `frequency`, `licence`, `theme`, `standard`, `distribution`, `data_service`,
  `dataset_theme`, `dataset_keyword`, `dataset_spatial`, `dataset_code`,
  `dataset_standard`, `service_dataset`, `quality_annotation`, `provenance`,
  `variable`, `feature_type`, `author`, `dimension_name`,
  `codelist_dependency`, `lineage`, `_meta`
- **`sdmx` schema** — SDMX structural tables owned by sdmxlib (schema 2.48).
  Catalogue spine (`artefact`, `localized_text`, `annotation`,
  `annotation_text`, `artefact_ref`, `fetch_scope`); item tables (`code`,
  `concept`, `category`, `agency`, `organisation_contact`, `data_provider`,
  `metadata_provider`); structure arms (`dsd_component`, `dsd_group`,
  `group_dimension`, `dataflow`, `provision_agreement`,
  `metadata_provision_agreement`, `categorisation`, `hierarchy_association`,
  `hierarchy_meta`, `hierarchy_level`, `hierarchy_node`); metadata
  structures (`metadata_structure`, `metadata_attribute`, `metadataflow`,
  `metadata_set`, `metadata_target`, `metadata_declared_target`,
  `metadata_value`); codelist composition (`codelist_extension`,
  `code_selection_member`); constraints (`data_constraint`, `cube_region`,
  `region_key_value`); mappings (`representation_map`, `value_map`).
  Codelists, DSDs, ConceptSchemes etc. live in `artefact` keyed by URN —
  there are no separate per-type spine tables.

  One view matters downstream: `hierarchy_applicability` resolves which
  Hierarchy applies to a component in a dataflow's context, scoped
  associations outranking unscoped ones at `rank = 1`. `q.within()`
  reads it rather than restating the rule (see "Structural filters").

  An absent reference is `NULL`, not `''` — never compare against the
  empty string here.

  sdmxlib treats this schema as a cache and migrates it by dropping it.
  Here it is half the catalog, so `Catalog.open()` and `from_connection()`
  refuse with `IncompatibleCatalogError` when the stored `sdmx._meta`
  version differs from sdmxlib's and `sdmx.artefact` holds rows
  (`_refuse_sdmx_schema_rebuild` in `store/store.py`). An sdmxlib that bumps
  its store schema therefore means rebuilding every catalog.

statcatlib creates its own tables via `store/schema.py:create_schema(conn)` which
creates the `statcat` schema and returns a cursor with
`search_path='statcat'`. sdmxlib's `LocalRegistry` (reachable as `cat.registry`)
handles its own `sdmx` schema. Both write to the same connection. statcatlib's SQL
JOINs freely across both sets of tables.

**Search** — LanceDB (optional, `statcatlib[search]`) provides BM25 full-text
search via Tantivy and optional vector search via ANN index. All search
functionality lives in `store/lance.py`. Stores the LanceDB dataset at
`{catalog_path}/search/`. Search text is enriched with theme labels and
dimension names (not just title/description/keywords). `build_index()`
uses batch encoding (256 texts per `encode()` call).

**One table per language** — the search index is not one table but one
per language, each with its own Tantivy tokenizer: French text stemmed
with French rules, English with English. Tantivy takes one stemmer and
one stopword list per index, and `MatchQuery` selects an index by
*column* rather than by name, so per-language configuration requires
per-language tables. Two consequences worth knowing:

- Each language gets **exact BM25 document frequencies**. Pooled into
  one table, `N` and `avgdl` spanned every language at once, so terms
  common in one language looked rarer than they were in another.
- Queries carry no `lang` filter — table membership *is* the filter.

A language with no Snowball stemmer (LanceDB rejects Chinese, Japanese,
Korean, Catalan) is indexed **unstemmed**, never with another
language's stemmer. Query-side stopwords are per-language too: applied
as one merged set, French `son`/`ton`/`sur` were stripped from English
queries where they are ordinary words.

**Index layout and rebuilds** — tables are generation-suffixed
(`search_fr__000003`) and reached through a `{catalog_path}/search/index.json`
manifest. A rebuild writes a whole new generation, then commits by
replacing the manifest in one atomic move, so a crash mid-rebuild leaves
the previous index whole and serving rather than half-swapped
(`discard_orphans()` reclaims the abandoned tables). The manifest records
each language's stemmer and row count, surfaced as `Catalog.index_layout`
— the thing to check after a build, and populated for BM25-only catalogs,
which write no `embeddings.json` at all.

Indexes built before pinaxlib v5.23.0 (a single `search` table, everything stemmed
as English) are still served, with a warning, until `build_index()`
rebuilds them. The reverse does not hold: **pinaxlib older than v5.23.0
reading a newer index finds no table it recognises and returns zero hits
silently**, so the library must be deployed before any bundle built with
it.

**Multilingual columns** — All multilingual text fields use
`MAP(VARCHAR, VARCHAR)`. Any language is queryable at any time via `title['en']`
subscript syntax. No upfront language configuration needed.

### SDMX ingest

Ingest is the operation that materializes a remote SDMX registry into the
shared DuckDB. The function lives in `sources/sdmx.py`:

```python
from statcatlib.sources.sdmx import ingest_sdmx

ingest_sdmx(store, registry)   # writes sdmxlib tables + catalog Dataset rows
```

Under the hood:
1. Call `store.registry.ingest(registry)` — `registry` is a property returning
   the co-located `sdmxlib.LocalRegistry`; this writes all SDMX artifacts into
   sdmxlib's `sdmx` schema tables.
2. For each dataflow, create an `AggregateDataset` with `sdmx_dataflow_urn`
   linking to the stored dataflow row, then `store.put(datasets)` inside a
   `with store.bulk_load():` block.

`sources/sdmx.py` is the ONLY file that imports sdmxlib. sdmxlib Python types
are used here during ingest and do not appear anywhere else in statcatlib.

## SQL style

- Always use triple-quoted strings for SQL
- Always use parameterized queries (`$name` style preferred) for values
- Never use f-strings or concatenation to interpolate values into SQL
- For dynamic WHERE clause composition, build a list of clause strings
  and join them; interpolate clause structure with f-strings but
  parameterize all values

## Tooling

- **Package manager**: `uv`
- **Command runner**: `just`
- **Type checker**: `basedpyright`
- **Linter/formatter**: `ruff`
- **Domain model**: `attrs`
- **Storage**: `duckdb`
- **Search**: `lancedb` (optional, BM25 FTS via Tantivy + ANN vector index)
- **HTTP**: `httpx`
- **SDMX dependency**: `sdmxlib` (only in `sources/sdmx.py`)
- **Testing**: `pytest`
- the `ruff.toml` file will NEVER be changed to fix a linting error. If a fix can't be made ask the user to change it.

## Commands

```bash
just ci                        # the gate: index guard + lint + fmt + docstrings + Any + typecheck + tests + docs
just typecheck                 # basedpyright over src/
just lint                      # ruff check src tests
just fmt                       # ruff format + ruff check --fix
just check-docstrings          # Google sections, no RST roles, pydoclint
just check-any                 # the `Any` ratchet (tools/count_any.py)
just check-indexes             # only public PyPI named in tracked files; uv.lock is gitignored
```

### `just ci` is the gate, and it runs locally

Hosted CI runs no tests — only `.github/workflows/docs.yml`, which publishes
the docs to GitHub Pages on pushes to main that touch the site's sources (or
by hand from the Actions tab). So nothing merges on a green pipeline; it
merges on a green `just ci`.

**The pre-push hook is the real gate**: `just hooks-install` points
`core.hooksPath` at `.githooks`, and every `git push` runs `just ci` first.
Bypass with `git push --no-verify`, sparingly and say why.

`just ci` ends with `docs-build`, which is `mkdocs build --strict`: it fails
on broken links and pages missing from the nav, where a plain build only
warns.

`core.hooksPath` is per-clone and cannot be set by a commit, so a fresh clone
has no gate until someone runs `just hooks-install`.

## Git / GitHub

- git commit/push/merge etc. will NEVER be done without asking.
- the project always uses conventional commits for commit messages.
- The project lives at github.com/StatCat-Suite/statcatlib; use `gh` for the API.
- Releases go through a PR: `just release-1-preview` … `release-5-pypi`.
- Any change that is non trivial (more than a line or 2 change, or some simple CI/justfile change) will be done in a feature branch.

**Issue references.** A bare `#N` or `!N` in the tree, the CHANGELOG, or old
commit messages means GitLab (gitlab.com/pinax-suite/pinax, an archive now,
from when the project was named pinax) and is not rewritten. **Write a GitHub
issue or PR as `GH-N`**, never a bare `#N`: GitHub numbers from 1, so a bare
number would collide with the GitLab one the tree already means.

## SDMX / DCAT glossary

Terms that may be unfamiliar — use these definitions, do not hallucinate
alternative meanings:

| Term                | Definition                                                                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| DCAT                | W3C Data Catalog Vocabulary — RDF vocabulary for describing datasets                                                                              |
| DCAT-AP             | DCAT Application Profile for European data portals                                                                                                |
| StatDCAT-AP         | DCAT-AP extension for statistical datasets — adds dimensions, attributes, quality                                                                 |
| PROV-O              | W3C Provenance Ontology (2013) — OWL ontology for provenance; core concepts: Entity, Activity, Agent; relationships: wasDerivedFrom, wasGeneratedBy, used, wasAssociatedWith; statcatlib adopts the concepts without the RDF runtime |
| Dataset             | A collection of data, published by a single agent                                                                                                 |
| Distribution        | A specific available form of a dataset (CSV file, API endpoint, etc.)                                                                             |
| DataService         | An API endpoint that serves one or more datasets                                                                                                  |
| CatalogRecord       | Metadata about a dataset's entry in the catalog (provenance, dates)                                                                               |
| Catalog             | A collection of dataset descriptions                                                                                                              |
| Agent               | An organization or person responsible for a dataset                                                                                               |
| Facet               | A queryable metadata dimension used for filtering/counting results                                                                                |
| DSD                 | Data Structure Definition (SDMX) — defines dimensions, attributes, measures                                                                       |
| Dataflow            | Published dataset backed by a DSD (SDMX)                                                                                                          |
| Codelist            | Finite set of allowed values for a dimension (SDMX)                                                                                               |
| DuckDB              | Embedded columnar database — catalog storage engine                                                                                               |
| LanceDB             | Embedded vector database with Tantivy FTS — powers BM25 and semantic search                                                                       |
| InternationalString | Multilingual label — reused from sdmxlib as a pure value type                                                                                     |
| URN                 | Uniform Resource Name — used as the stable link identifier between catalog records and SDMX artifacts (analogous to URI in RDF linked data)        |

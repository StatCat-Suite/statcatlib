# statcatlib.store

## Overview

`statcatlib.store` is the DuckDB-backed query engine. `Catalog` owns one
DuckDB connection that holds two schemas: `statcat` (the catalog star
schema, created by `schema.py`) and `sdmx` (written by the co-located
`sdmxlib.LocalRegistry`). Catalog SQL JOINs across both schemas, with
`dataset.sdmx_dataflow_urn` as the link into sdmxlib's tables. In folder
mode the catalog also attaches a DuckLake sidecar for observation data and
serves an optional LanceDB search index from `search/`.

Folder layout under `<uri>/`:

- `meta.duckdb` holds the `statcat` and `sdmx` schemas.
- `lake.ducklake` is the DuckLake catalog, attached as `lake` on open.
- `parquet/` is the default DuckLake DATA_PATH, created by DuckLake on first write.
- `search/` holds the per-language LanceDB tables, `index.json` and the optional `embeddings.json`.

## Interface

`__init__.py` re-exports the `store.py` names below (except the module-level
helpers), the `scopes.py` scopes, `Row` and `xhtml_to_markdown`. The
`schema.py`, `lance.py` and `sentinel.py` names are imported from their
modules; `statcatlib/__init__.py` re-exports `NotLoadedError` and
`is_unloaded`.

### `store.py`

- `Catalog(uri=":memory:", *, data_path=, azure_credentials=, embedding_model=, document_prefix=, query_prefix=, read_only=False)`
  - Lifecycle: `open()`, `close()`, context manager, `Catalog.from_connection(conn)`.
  - Properties: `uri`, `connection`, `cursor`, `registry`, `data`, `bundle_dir`, `index_layout`, `index_annotations`, `embedding_metadata`, `embedding_model_name`, `document_prefix`, `query_prefix`.
  - Writes: `put(obj)` for a dataset, `ConceptScheme`, or a sequence of either; `put_concept()`; `add_lineage()`; `bulk_load()`; `transaction()`; `build_index(...) -> int`; `attach_embedding_model()`.
  - Reads: `get(cls, id)`, `query(cls, **filters) -> QueryBuilder`, `search(text, *, kinds=, lang=, ...) -> SearchResult[Hit]`, `facets(*fields)`, `get_concept_scheme()`, `get_concept()`, `agent_labels()`, `concept_labels()`, `spatial_labels()`, `code_map()`, `codelist_for()`, `get_structure()`, `metadata_for()`, `metadata_report()`, `metadata_targets()`, `check_index_compatible()`.
  - Scope entry points: `themes`, `concept_schemes`, `codes`, `concepts`, `datasets`, `reference_metadata`, `codelists`, `dataset(id)`, `codelist(urn)`, `dimensions(ds)`.
- `QueryBuilder[T]` with modifiers `filter`, `from_ids`, `search`, `hybrid_search`, `include`, `full`, `sort_by`, `limit`, `offset`, `with_facets`, `select`, `scalars`, and terminals `execute`, `collect`, `all`, `first`, `one`, `rows`, `row`, `values`, `value`, `count`, `exists`.
- `SearchResult[T]`, `Hit[T]`, `CodeHit`, `ConceptHit`, `ReferenceMetadataHit`.
- `CodesScope`, `ConceptsScope`, `DatasetsScope`, `ReferenceMetadataScope`, each with `find(...)`; the first two also have `get(urn)`.
- `CodelistIndexPolicy`, `ReferenceMetadataBlend` configure `build_index`.
- `EmbeddingMetadata` describes the model recorded in `embeddings.json`.
- `VALID_INCLUDE_RELS`: the 18 relationship names accepted by `include()`.
- `SEARCH_ANNOTATION_TYPES`, `SEARCH_CONTEXT_ANNOTATION_TYPE` (`statcat:searchContext`), `SEARCH_EXCLUDE_ANNOTATION_TYPE` (`statcat:searchExclude`).
- `IncompatibleCatalogError` (`RuntimeError`), `IncompatibleIndexError` (`ValueError`).
- Module-level helpers used elsewhere in the package: `statcat_schema_mismatch(conn)`, `annotation_fingerprint(conn, types)`, `EmbeddingModel` protocol, `as_vector`, `as_vectors`, and `ASYMMETRIC_MODEL_PREFIXES` (default query/document prefixes per embedding model, read by `cli`).
- Row `NamedTuple`s for batch hydration: `AgentRow`, `DataServiceRow`, `DistributionRow`, `DatasetRow`.

### `scopes.py`

- `ThemesScope`, `SchemeScope` (`resolve`, `labels`, `datasets`, `enrich`, `filter`, `sort_by`), `ConceptScope`. `ConceptSchemesScope` is an alias of `ThemesScope`; `ConceptSchemeScope` is an alias of `SchemeScope`. New code should use the SKOS names.
- `CodelistsScope` (`lang`, `filter(text_contains=)`, `collect`, `rows`, `batch_labels`, `enrich`), `CodelistScope` (`lang`, `collect`, `rows`, `count`, `label`, `batch_labels`, `enrich`, `datasets`), `CodeScope` (`dataset_count`, `datasets`).
- `DimensionsScope`, `DimensionScope` (`codelist`), `DimensionInfo`.
- The lists above name the main methods; see each class for the full set.
- `DatasetScope` (`codelists`, `themes`, `related(edge)`, `upstream()`, `downstream()`, `lineage_records()`).

### `schema.py`

- `SCHEMA_VERSION` (`"2.0"`), `create_schema(conn)`, `open_schema_readonly(conn)`, `schema_exists(conn)`, `get_schema_version(conn)`, `first_row(rows)`.

### `lance.py`

- `LanceIndex` routes by language over one `_LanguageIndex` table per language: `add`, `add_bulk`, `bm25_search`, `bm25_search_rows`, `search`, `search_rows`, `build_fts_index`, `build_index`, `describe`, `languages`, `table_for`, `count_rows`, `orphan_tables`, `discard_orphans`, `unreadable_version`, and the properties `version` and `annotations`.
- `LanguageLayout`, `IndexAnnotations`, `SearchRow`, `Bm25HitRow`, `VectorHitRow` (TypedDicts), `make_schema(*, embedding_dim)`.

### Small modules

- `row.py`: `Row`, a `dict` subclass with attribute access, returned by `select()`.
- `sentinel.py`: `NotLoadedError` (`AttributeError`), `make_unloaded(field)`, `is_unloaded(value)`.
- `xhtml.py`: `xhtml_to_markdown(html)`, a stdlib-only converter used when indexing reference metadata.

## Invariants

- Construction does no I/O. The connection opens on `open()` or `with`. Any query on an unopened catalog raises `RuntimeError`.
- `open()` is idempotent within one open/close cycle and is open-or-create in folder mode.
- Three guards raise `IncompatibleCatalogError`; `open()` closes its owned connection first:
  - `_refuse_statcat_schema_mismatch` (stored `statcat._meta` version differs from `SCHEMA_VERSION`) runs before any DDL in both `open()` and `from_connection()`.
  - `_refuse_unreadable_index` (`index.json` version above `lance._MANIFEST_VERSION`) runs before any DDL in `open()`. `from_connection()` has no bundle folder and skips it.
  - `_refuse_sdmx_schema_rebuild` (stored `sdmx._meta` version differs from sdmxlib's while `sdmx.artefact` has rows) runs after `create_schema` has issued the `statcat` DDL and before the `LocalRegistry` opens, in both paths.
- `create_schema` stamps `schema_version` once with `INSERT OR IGNORE`; a later open never restamps it. Bumping `SCHEMA_VERSION` means rebuilding every catalog.
- All `statcat` DDL uses `IF NOT EXISTS`. Multilingual columns are `MAP(VARCHAR, VARCHAR)`.
- DuckDB cursors are not thread-safe. The `_c` property gives each thread its own cursor with `search_path = 'statcat'`, held in a `threading.local`.
- `schema.first_row(conn.execute(...).fetchall())` reads a single row without leaving a DuckDB 2 result pending; a pending `fetchone()` result makes the relation API (`conn.sql`, `conn.table`) raise `ResourceInUseException` (GH-43). `store.py` still has 19 `.fetchone()` calls. Use `first_row` for new single-row reads.
- `read_only=True` issues no DDL and no `mkdir` and attaches DuckLake `READ_ONLY`. `put`, `put_concept`, `bulk_load` and `build_index` raise `RuntimeError` through `_require_writable`. `add_lineage` has no such check, and `data.write_observations` fails with DuckDB's own error from the `READ_ONLY` attach. `read_only` is rejected with `:memory:` or with an `embedding_model`.
- `embedding_model` requires folder mode; `data_path` is rejected for `:memory:`.
- Structural queries read sdmxlib's tables by JOIN, and this package keeps no copy of codes or hierarchies. The filter SQL, including hierarchy descent over `hierarchy_applicability`, is built in `../query.py`; `store.py` compiles it through `_QueryContext`. An absent `sdmx` reference is `NULL`, never `''`.
- `include()` accepts only names in `VALID_INCLUDE_RELS` and raises `ValueError` otherwise. Unloaded fields hold a per-field cached sentinel whose `__bool__` is `False` and whose attribute, iteration, `len` or index access raises `NotLoadedError`. A NULL foreign key yields `None` regardless of `include()`. `get()` always loads every relationship.
- Entity terminals raise `ValueError` while `select()` or `scalars()` is active. `select()` accepts `_SELECTABLE_COLUMNS` plus the MAP columns `title` and `description`, which need `lang`.
- SQL values are parameterized; clause structure is built from lists of strings.
- Search uses one LanceDB table per language, each with its own Snowball stemmer and stopwords. Languages without a stemmer are indexed unstemmed. Queries on per-language tables carry no `lang` filter. A legacy single-table index (pre-pinaxlib v5.23.0) is served through a `_LanguageIndex` with `filter_lang=True`, which adds `lang = ...` to each query.
- Nothing on the open or search path writes to the bundle. The FTS index is reused as found. A changed annotation fingerprint makes `open()` warn; the open still succeeds and nothing rebuilds automatically (GH-3).
- `index.json` is the commit point of a rebuild: a new generation of tables is written first, then the manifest is replaced atomically with `Path.replace`. Bump `_MANIFEST_VERSION` only when an existing field changes meaning or shape.
- `lance.py` imports `lancedb` only under `TYPE_CHECKING` and inside the methods that use it, so core statcatlib has no hard dependency on `lancedb` (the `statcatlib[search]` extra).

## Side Effects

- Opens and closes a DuckDB connection; read-write opens take DuckDB's exclusive file lock on `meta.duckdb`.
- Folder mode creates `<uri>/` and `meta.duckdb`, ATTACHes `lake.ducklake`, and may rewrite a relative DuckLake DATA_PATH stamp to the absolute path on a read-write open (GH-23).
- `create_schema` creates the `statcat` schema and tables; `LocalRegistry` creates or migrates the `sdmx` schema on the same connection.
- `put`, `put_concept` and `add_lineage` upsert rows in `statcat` tables. `bulk_load()` raises `wal_autocheckpoint` to 1GB; on exit of the outermost block it resets it to 16MB and runs `CHECKPOINT`. `transaction()` commits on clean exit and rolls back on exception.
- `build_index()` writes LanceDB tables, `search/index.json` and, when a model is set, `search/embeddings.json`; it calls the embedding model in batches (256 by default, `STATCAT_EMBED_BATCH_SIZE` overrides).
- `discard_orphans()` drops LanceDB tables left by an abandoned rebuild.
- Emits `warnings.warn` for stale or legacy search indexes and for changed search annotations.
- `data` with an `abfss://` DATA_PATH reads and writes object storage through `sdmxlib.DuckDBDataStore`.

## Verification

- Run: `just ci`
- Unit tier only: `uv run pytest -m unit`

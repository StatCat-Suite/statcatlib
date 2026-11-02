# statcatlib.store

## Overview

`statcatlib.store` is the query and write engine over one edition, and the
code that writes a catalog's Inventory. The private `_Store` (`store.py`)
runs every query and write over one edition's DuckDB catalog, which holds two
schemas: `statcat` (the catalog star schema, created by `schema.py`) and
`sdmx` (written by sdmxlib's registry). Catalog SQL JOINs across both
schemas, with `dataset.sdmx_dataflow_urn` as the link into sdmxlib's tables.
Two public classes wrap a `_Store`: `EditionView` (read-only, `store.py`) and
`WorkingCopy` (writable, `working_copy.py`), which also seals the edition.
`inventory.py` commits Inventory versions, and `lance.py` serves an optional
LanceDB search index from the edition's `search/`.

Edition layout under `<catalog>/products/<product>/<edition_id>/`, which
sdmxlib owns (`docs/catalog-format.md` §1):

- `metadata.duckdb` holds the `statcat` and `sdmx` schemas.
- `tables.ducklake` is sdmxlib's DuckLake catalog for observations; the data
  files sit in `<catalog>/products/<product>/data/`, shared by the product's
  editions.
- `search/` holds the per-language LanceDB tables, `index.json` and the
  optional `embeddings.json`.
- `manifest.json` is written by the seal. An edition without one is the
  product's working copy.

## Interface

`__init__.py` re-exports `EditionView`, `WorkingCopy`, `QueryBuilder`,
`SearchResult`, the hit types, the `*Scope` classes of `store.py` and
`scopes.py`, `CodelistIndexPolicy`, `ReferenceMetadataBlend`,
`EmbeddingMetadata`, `VALID_INCLUDE_RELS`, the three search annotation
constants, `IncompatibleCatalogError`, `IncompatibleIndexError`, `Row` and
`xhtml_to_markdown`. Other names are imported from their modules;
`statcatlib/__init__.py` re-exports the inventory and working-copy errors,
`NotLoadedError` and `is_unloaded`.

### `store.py`

- `_Store` (private, design decision P8): built by `_over(cursor_factory, *,
  catalog_name, search_dir, writable, registry, embedding_model=None,
  document_prefix="", query_prefix="")`. It owns no connection: cursors come
  from `cursor_factory`, and the registry's owner closes the registry.
  `_cursor()` returns this thread's cursor after
  `USE "<catalog_name>".statcat`. `_close(closed)` closes every cursor, and
  every later call raises the exception `closed()` returns. It keeps every
  query and write method of statcatlib 0.11's `Catalog`, and
  `LINEAGE_RELATIONSHIPS` and `LINEAGE_CONFIDENCE`.
- `EditionView`: built only by `Product.view` and `WorkingCopy.view`; direct
  construction raises `TypeError`. It has no write method.
  - Reads: `get(cls, id)`, `query(cls, **filters) -> QueryBuilder`,
    `search(text, *, kinds=, lang=, limit=, offset=, operator=, fuzzy=,
    rrf_k=, per_kind_oversample=) -> SearchResult`, `facets(*fields)`,
    `agent_labels()`, `concept_labels()`, `spatial_labels()`,
    `get_concept_scheme()`, `get_concept()`, `code_map()`, `codelist_for()`,
    `get_structure()`, `metadata_for()`, `metadata_report()`,
    `metadata_targets()`, `language_alternates() -> list[Row]`.
  - Scope entry points: `themes`, `concept_schemes`, `codes`, `concepts`,
    `datasets`, `reference_metadata`, `codelists`, `dataset(id)`,
    `codelist(urn)`, `dimensions(ds)`.
  - Search index: `index_annotations`, `index_layout`,
    `embedding_metadata`, `embedding_model_name`, `document_prefix`,
    `query_prefix`, `check_index_compatible(*, model_name=, embedding_dim=)`,
    `attach_embedding_model(model)`.
  - `registry`: sdmxlib's read surface (`get`, `query`, `obs`) over the same
    edition: a sealed edition's `sdmxlib.EditionRegistry`, or a working
    copy's build registry.
  - `_cursor()` is private, for statcatlib's tests that run raw SQL.
- `VIEW_ATTRIBUTES`: the public attribute names of `EditionView`.
  `Catalog.__getattr__` names them in its guided errors.
- `QueryBuilder[T]` with modifiers `filter`, `from_ids`, `search`, `hybrid_search`, `include`, `full`, `sort_by`, `limit`, `offset`, `with_facets`, `select`, `scalars`, and terminals `execute`, `collect`, `all`, `first`, `one`, `rows`, `row`, `values`, `value`, `count`, `exists`.
- `SearchResult[T]`, `Hit[T]`, `CodeHit`, `ConceptHit`, `ReferenceMetadataHit`.
- `CodesScope`, `ConceptsScope`, `DatasetsScope`, `ReferenceMetadataScope`, each with `find(...)`; the first two also have `get(urn)`.
- `CodelistIndexPolicy`, `ReferenceMetadataBlend` configure `build_index`.
- `EmbeddingMetadata` describes the model recorded in `embeddings.json`.
- `VALID_INCLUDE_RELS`: the 18 relationship names accepted by `include()`.
- `SEARCH_ANNOTATION_TYPES`, `SEARCH_CONTEXT_ANNOTATION_TYPE` (`statcat:searchContext`), `SEARCH_EXCLUDE_ANNOTATION_TYPE` (`statcat:searchExclude`).
- `IncompatibleCatalogError` (`RuntimeError`): a legacy layout, a
  `manifest.json` the Inventory did not record, another `statcat` schema
  version, a search manifest of another version, or a filesystem without
  hard links. `IncompatibleIndexError` (`ValueError`).
- Module-level helpers used elsewhere in the package: `statcat_schema_mismatch(conn)`, `annotation_fingerprint(conn, types)`, `EmbeddingModel` protocol, `as_vector`, `as_vectors`, and `ASYMMETRIC_MODEL_PREFIXES` (default query/document prefixes per embedding model, read by `cli`).
- Row `NamedTuple`s for batch hydration: `AgentRow`, `DataServiceRow`, `DistributionRow`, `DatasetRow`.

### `working_copy.py`

- `WorkingCopy`: a product's one unsealed edition, built only by
  `Product.working_copy`; direct construction raises `TypeError`.
  - Properties: `product_id`, `edition_id` (a UUIDv7), `previous` (the base
    edition, or `None` for the product's first), `registry` (the writable
    `sdmxlib.LocalRegistry`: `ingest` for structures, `write_observations`
    for observations).
  - `view() -> EditionView` over the same store, which sees the writes as
    they are made.
  - Writes: `put(obj)`, `put_concept(scheme_uri, concept)`,
    `add_lineage(...)`, `add_language_alternates(pairs, *, source)`,
    `bulk_load()`, `transaction()`, `build_index(...) -> int`.
  - Ending: `seal(checks=()) -> Edition`, `close()` (leaves the edition
    unsealed for `working_copy(resume=True)`; also the context manager's
    exit), `discard()` (`sdmxlib.edition.discard`).
- `Edition`: a frozen attrs value with `edition_id`, `product_id`,
  `previous` and `manifest_sha256`.
- `settle_journal(writer, product_dir, product_id, lifecycle)` applies the
  journal rule; `record_journalled(...)` is its pure part.
  `manifest_sha256(edition_dir)` hashes an edition's `manifest.json`.
- Errors: `WorkingCopyExistsError`, `WorkingCopyClosedError`
  (`RuntimeError`), `SealCheckError` (with `findings`), `JournalError`.

### `inventory.py`

- `InventoryWriter(root)`: `root`, `catalog_document()`, `has_version()`,
  `full() -> FullInventory | None`, `head() -> Root | None`,
  `write_catalog_json(*, catalog_id=None)`, `try_commit(base, leaves, *,
  retired=None, embargo=None, extensions=None, kind, reason) -> Root`, and
  `commit(change, *, kind, reason) -> Root`.
- `Change(leaves, retired=None, embargo=None, extensions=None)`: what
  `commit`'s *change* callable returns for a head.
- `MAX_RACES = 8`. `producer()` returns
  `Producer(name="statcatlib", version=<installed version>)`.
- Errors: `InventoryConflictError` (another writer created the version
  first), `InventoryContentionError` (`MAX_RACES` lost in a row),
  `InventoryRuleError(message, *, rule, product, edition)` with
  `InventoryRuleError.wrapping(exc, ...)` over sdmxlib's `TransitionError`,
  and its subclass `NonFastForwardError`.

### `scopes.py`

- `ThemesScope`, `SchemeScope` (`resolve`, `labels`, `datasets`, `enrich`, `filter`, `sort_by`), `ConceptScope`. `ConceptSchemesScope` is an alias of `ThemesScope`; `ConceptSchemeScope` is an alias of `SchemeScope`. New code should use the SKOS names.
- `CodelistsScope` (`lang`, `filter(text_contains=)`, `collect`, `rows`, `batch_labels`, `enrich`), `CodelistScope` (`lang`, `collect`, `rows`, `count`, `label`, `batch_labels`, `enrich`, `datasets`), `CodeScope` (`dataset_count`, `datasets`).
- `DimensionsScope`, `DimensionScope` (`codelist`), `DimensionInfo`.
- The lists above name the main methods; see each class for the full set.
- `DatasetScope` (`codelists`, `themes`, `related(edge)`, `upstream()`, `downstream()`, `lineage_records()`).
- Every scope holds a `_Store` and runs its SQL on `_store._cursor()`.

### `schema.py`

- `SCHEMA_VERSION` (`"2.2"`), `use_statcat(cursor, catalog_name)`,
  `create_schema(conn, catalog_name=None)`,
  `open_schema_readonly(conn, catalog_name=None)`, `schema_exists(conn)`,
  `get_schema_version(conn)`, `first_row(rows)`. With `catalog_name=None`
  they work on the connection's default catalog and set
  `search_path = 'statcat'`.

### `lance.py`

- `LanceIndex(path, *, edition_id=None)` routes by language over one `_LanguageIndex` table per language: `add`, `add_bulk`, `drop_carried`, `bm25_search`, `bm25_search_rows`, `search`, `search_rows`, `build_fts_index`, `build_index`, `carried_vectors`, `describe`, `languages`, `table_for`, `count_rows`, `orphan_tables`, `discard_orphans`, `unreadable_version` (static), and the properties `version` and `annotations`. *edition_id* names the edition whose tables a rebuild writes; `None` opens a sealed edition's index for reading only.
- `text_sha256(model, prefix, text)`: the SHA-256 of the JSON array `[model, prefix, text]`, recorded in each embedded row's `text_sha256` column.
- `LanguageLayout`, `IndexAnnotations`, `SearchRow`, `Bm25HitRow`, `VectorHitRow` (TypedDicts), `make_schema(*, embedding_dim)`.

### Small modules

- `row.py`: `Row`, a `dict` subclass with attribute access, returned by `select()`.
- `sentinel.py`: `NotLoadedError` (`AttributeError`), `make_unloaded(field)`, `is_unloaded(value)`.
- `xhtml.py`: `xhtml_to_markdown(html)`, a stdlib-only converter used when indexing reference metadata.

## Invariants

- `EditionView` and `WorkingCopy` refuse direct construction with
  `TypeError`; `Product.view` and `Product.working_copy` build them.
- `_Store._over` runs, in this order: open a cursor (`RuntimeError` when the
  catalog has no `statcat` schema); `_refuse_statcat_schema_mismatch`
  (`IncompatibleCatalogError` when `statcat._meta` records another version
  than `SCHEMA_VERSION`, or none); with a `search_dir`,
  `_refuse_unreadable_index` (`IncompatibleCatalogError` when `index.json`
  records a version other than `lance._MANIFEST_VERSION`, or none), opening
  the LanceDB index, the annotation warning, and restoring the embedding
  metadata from `embeddings.json`. Before it, sdmxlib's
  `LocalRegistry.edition` (view) and `LocalRegistry.build` (working copy)
  refuse another `sdmx` store schema with `sdmxlib.StoreSchemaError`.
- `create_schema` runs only on a product's first edition, in
  `WorkingCopy._start` before `_over`; a later edition arrives with the
  schema in its copied `metadata.duckdb`. It stamps `schema_version` once
  with `INSERT OR IGNORE`, and bumping `SCHEMA_VERSION` means rebuilding
  every catalog.
- All `statcat` DDL uses `IF NOT EXISTS`. Multilingual columns are `MAP(VARCHAR, VARCHAR)`.
- DuckDB cursors are not thread-safe. `_cursor()` gives each thread its own
  cursor, held in a `threading.local`, and runs `USE "<catalog_name>".statcat`
  on it, so unqualified and two-part names resolve inside this edition's
  catalog even with other editions attached to the same engine. Every
  `information_schema` query filters by `current_database()`.
- `schema.first_row(conn.execute(...).fetchall())` reads a single row without leaving a DuckDB 2 result pending; a pending `fetchone()` result makes the relation API (`conn.sql`, `conn.table`) raise `ResourceInUseException` (GH-43). `store.py` still has 19 `.fetchone()` calls. Use `first_row` for new single-row reads.
- A `_Store` over a sealed edition is built with `writable=False`: `put`,
  `put_concept`, `add_lineage`, `add_language_alternates`, `bulk_load`,
  `transaction` and `build_index` raise `RuntimeError` through
  `_require_writable`, and its `LanceIndex` has no `edition_id`, so any index
  write raises `RuntimeError`.
- After `_close`, every method raises the recorded error: a sealed,
  discarded or closed `WorkingCopy` and every view taken from it raise
  `WorkingCopyClosedError`, and the views of a closed `Catalog` raise
  `RuntimeError`.
- A product has one unsealed edition. `WorkingCopy._start` settles the
  journal, refuses a product recorded with another lifecycle
  (`InventoryRuleError`, `rule="lifecycle"`), takes the live edition at
  sdmxlib's store `SCHEMA_VERSION` as its base, and calls
  `LocalRegistry.build(previous=)` or, with `resume=True`,
  `LocalRegistry.build(resume=)`. An existing unsealed edition raises
  `WorkingCopyExistsError` without `resume`, and `resume` with none raises
  `FileNotFoundError`. A new edition whose store fails to open is discarded.
- `WorkingCopy.seal` runs these steps: settle the journal; run each check
  against `view()` (any finding raises `SealCheckError` and leaves the
  working copy open); raise `NonFastForwardError` when the base is no longer
  the live edition; rebuild the search index and `drop_carried`;
  `catalog_section(store)`; close the store and registry;
  `sdmxlib.edition.seal`, which writes the journal entry and then the
  manifest; commit the record (`kind="seal"`), live when the catalog's
  embargo is off; clear the journal entry. The steps are split for
  `Catalog.seal`: `_precheck(checks)` (steps 0 to 2), `_seal_edition()`
  (steps 3 to 5, returning a private `_Sealed` with the edition and its
  manifest's store schema), `_next_leaf(full, sealed)` (the record's leaf;
  `InventoryRuleError` on a refusal), `_recorded()` and `_unrecorded(exc)`
  (clear the journal entry; the second also logs the WARNING).
- The seal's index rebuild reuses the arguments of the last
  `WorkingCopy.build_index` call on that object, or the defaults when there
  was none. Without lancedb installed it deletes `search/`, so a carried
  index never describes the previous edition's rows.
- Embedding reuse (S2b): each embedded search row records `text_sha256`,
  `lance.text_sha256(model, prefix, text)`. `_Store.build_index` reads
  `LanceIndex.carried_vectors()` before `add_bulk` replaces the tables, and
  `_batch_encode` takes a stored vector for each text whose hash matches and
  encodes the rest. Vectors are reused only when `build_index` gets
  `model_name` explicitly (`_Store._vector_reuse`); without it every text is
  encoded, an INFO line says so, and the hash uses `embedding_model_name`
  (the carried `embeddings.json`). With no name at all `text_sha256` is NULL. A table without the column
  (written before S2b) yields no vectors.
- The journal rule (`settle_journal`; the table is in `working_copy.py`'s
  module docstring) runs when a working copy starts or resumes, at the start
  of a seal and after its record, against the newest root. An entry whose
  edition the root names or tombstones, or whose edition has no manifest, is
  cleared. A sealed, unrecorded edition is recorded in one commit and its
  entry cleared; a `duplicate` refusal with the same `manifest_sha256` counts
  as recorded. Any other refusal clears the entry and logs a WARNING.
  `InventoryContentionError` keeps the entry for the next working copy. An
  unreadable entry raises `JournalError`.
- A refusal after the manifest (a rollback landing during the seal) raises
  `NonFastForwardError` from the record; the seal clears the journal entry,
  logs a WARNING, and leaves the sealed edition on disk unrecorded. A
  `ManifestError` from `sdmxlib.edition.seal` comes before the journal entry;
  the working copy stays closed, and `working_copy(resume=True)` reopens it.
- `InventoryWriter` is the only code that writes `catalog.json`,
  `inventory/` and `CURRENT`. It creates each file with sdmxlib's
  `sdmxlib.storage.create_exclusive` (staged file, fsync, `os.link` to the
  name, unlink the staged file, fsync the directory), so a name never holds
  half a file and of two writers exactly one creates a root: the loser's
  `FileExistsError` becomes `InventoryConflictError`; a leaf that exists already counts as written, since
  its name is the hash of its bytes. It writes leaves, then the root, then
  `CURRENT`, and replaces `CURRENT` only with a higher `seq` in the same
  `restore_epoch`. `commit` re-reads the head and calls *change* again after
  each `InventoryConflictError`, so every rule is evaluated against the
  version the commit lands on, and raises `InventoryContentionError` after
  `MAX_RACES`. A root records `catalog.json`'s `restore_epoch`, and its
  event's actor is `<os user>@<hostname>`, taken from the process.
- Structural queries read sdmxlib's tables by JOIN, and this package keeps no copy of codes or hierarchies. The filter SQL, including hierarchy descent over `hierarchy_applicability`, is built in `../query.py`; `store.py` compiles it through `_QueryContext`. An absent `sdmx` reference is `NULL`, never `''`.
- `include()` accepts only names in `VALID_INCLUDE_RELS` and raises `ValueError` otherwise. Unloaded fields hold a per-field cached sentinel whose `__bool__` is `False` and whose attribute, iteration, `len` or index access raises `NotLoadedError`. A NULL foreign key yields `None` regardless of `include()`. `get()` always loads every relationship.
- Every public `lang` parameter and `.lang()` setter passes its tag through `sdmxlib.normalize_tag` once on entry; a malformed tag raises `sdmxlib.InvalidTagError`. Internal helpers trust the value. `add_language_alternates` stores each tag as `normalize_tag` spells it.
- Entity terminals raise `ValueError` while `select()` or `scalars()` is active. `select()` accepts `_SELECTABLE_COLUMNS` plus the MAP columns `title` and `description`, which need `lang`.
- SQL values are parameterized; clause structure is built from lists of strings.
- Search uses one LanceDB table per language, each with its own Snowball stemmer and stopwords. Languages without a stemmer are indexed unstemmed. Queries carry no `lang` filter.
- Tables are named `search_<lang>__<edition_id without hyphens>`, with `<lang>` as `sdmxlib.tag_alias` spells it, and `index.json` records the `edition_id` that wrote them. `index.json` is the commit point of a rebuild: `add_bulk` drops orphaned tables, writes the edition's tables, then replaces the manifest atomically with `Path.replace`. `drop_carried` then drops every table another edition wrote and rewrites the manifest when it named one. `_MANIFEST_VERSION` is 3; bump it only when an existing field changes meaning or shape.
- Nothing on the view or search path writes to an edition. The FTS index is reused as found. A changed annotation fingerprint makes `_over` warn; the view still opens and nothing rebuilds automatically (GH-3).
- `lance.py` imports `lancedb` only under `TYPE_CHECKING` and inside the methods that use it, so core statcatlib has no hard dependency on `lancedb` (the `statcatlib[search]` extra).

## Side Effects

- `_Store` opens and closes cursors on its registry's connection; it opens no
  connection of its own. `Product.view` attaches the edition to the
  catalog's `sdmxlib.EditionEngine`.
- `Product.working_copy` creates the edition directory through
  `LocalRegistry.build`, which copies the base edition's `metadata.duckdb`
  and carries its `search/`; on a product's first edition `create_schema`
  creates the `statcat` schema and tables.
- `put`, `put_concept` and `add_lineage` upsert rows in `statcat` tables. `add_language_alternates` inserts rows into `statcat.language_alternate` and keeps a pair already recorded. `bulk_load()` raises `wal_autocheckpoint` to 1GB; on exit of the outermost block it resets it to 16MB and runs `CHECKPOINT`. `transaction()` commits on clean exit and rolls back on exception.
- `build_index()` writes LanceDB tables, `search/index.json` and, when a model is known, `search/embeddings.json`; it calls the embedding model in batches (256 by default, `STATCAT_EMBED_BATCH_SIZE` overrides) for the texts whose `text_sha256` has no stored vector, and logs the reused and encoded counts at INFO.
- `WorkingCopy.seal` rebuilds `search/`, drops carried tables, writes the
  journal entry and `manifest.json` through `sdmxlib.edition.seal`, commits
  an Inventory version, and deletes the journal entry. `discard` deletes the
  unsealed edition directory.
- `settle_journal` can commit an Inventory version and deletes journal
  entries.
- `InventoryWriter` writes `catalog.json`, leaves under `inventory/leaves/`
  and roots under `inventory/` through `sdmxlib.storage.create_exclusive`,
  and `CURRENT` through its own staged `.<name>.<pid>.<uuid>.tmp` file and
  rename; each file and directory is fsynced.
- `discard_orphans()` drops LanceDB tables the manifest does not name.
- Emits `warnings.warn` for a quantised (stale) vector index and for changed search annotations, and logs a WARNING for an edition the journal rule cannot record.

## Verification

- Run: `just ci`
- Unit tier only: `uv run pytest -m unit`
- The Inventory writer: `tests/test_inventory_writer.py` (unit) and
  `tests/test_inventory_conformance.py` (integration, sdmxlib's conformance
  suite). Seals: `tests/test_seal_checks.py`, `tests/test_seal_crash.py`
  and `tests/test_concurrent_seal.py`; the batched seal in
  `tests/test_catalog_model.py::TestBatchSeal`. Views:
  `tests/test_edition_view.py`. Embedding reuse:
  `tests/test_embedding_reuse.py`.

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
`inventory.py` commits Inventory versions through an `InventoryStorage`,
`blob.py` is that storage on object storage (`statcatlib[blob]`),
`cross_product.py` holds the rules
that read several products (specification §6.5 items 4, 7 and 8), and
`lance.py` serves an optional LanceDB search index from the edition's
`search/`.

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
    edition, or `None` for the product's first), `kind` (`"data"` or
    `"structure"`), `registry` (the writable
    `sdmxlib.LocalRegistry`: `ingest` for structures, `write_observations`
    for observations).
  - `view() -> EditionView` over the same store, which sees the writes as
    they are made.
  - Writes: `put(obj)`, `put_concept(scheme_uri, concept)`,
    `add_lineage(...)`, `add_language_alternates(pairs, *, source)`,
    `bulk_load()`, `transaction()`, `build_index(...) -> int`; on a
    structure product's working copy `fill_structures(source, *, families,
    agency, ids, artefacts) -> tuple[str, ...]`
    (`sdmxlib.edition.fill_structures`; `TypeError` on a data product's);
    and on a data product's `fill_dataflows(source, dataflows, *,
    observations=None, partials=True) -> dict[str, str]`
    (`sdmxlib.edition.fill_edition`; `TypeError` on a structure product's),
    which writes `_meta.fill_in_progress`, fills, then in one transaction
    merges the returned map into `_meta.partial_of` (JSON, the later value
    winning) and deletes the marker.
  - Ending: `seal(checks=()) -> Edition`, `close()` (leaves the edition
    unsealed for `working_copy(resume=True)`; also the context manager's
    exit), `discard()` (`sdmxlib.edition.discard`).
- `Edition`: a frozen attrs value with `edition_id`, `product_id`,
  `previous` and `manifest_sha256`.
- `settle_journal(writer, product_dir, product_id, lifecycle, *,
  exclude=())` applies the journal rule catalog-wide (`_Repair` runs the
  publish checks and the one commit, with each edition's `routing_fields`);
  `recorded_routing(fields)` gives `record` both routing fields and
  `artefacts`, for every kind (GH-125 part 4); `record_journalled(...)` is its pure
  part per product, taking each entry as `(JournalEntry, manifest_sha256,
  schema_version, kind)`.
- `fold_lifecycle(leaf, manifest) -> Lifecycle`: the lifecycle the release
  job's fold records an uploaded edition with (Phase 5 D3, spec §11.2 step 2):
  the leaf's when the head records the product and the `statcat` section's
  `lifecycle` is absent or equal; the section's for a product the head does
  not record. A different value, or none for a new product, raises
  `InventoryRuleError(rule="lifecycle")`, the second naming statcatlib 0.17.
  `tests/stores/_stores.py`'s `record_uploaded` uses it until the fold exists.
- `Planned(product_id, edition_id, leaf, read=None)` and
  `precheck_refusals(writer, full, planned, fixed=())`: the seal's
  cross-product pre-check over provisional leaves, each working copy's
  artefacts read through `cross_product.StoreArtefacts`, dropping refused
  products until a pass refuses none.
  `manifest_sha256(edition_dir)` hashes an edition's `manifest.json`, and
  `edition_kind(manifest)` reads the kind its `statcat` section names.
- `structure_editions(full, catalog_root, *, product_id, batch)` lists the
  structure editions an edition's publish check is given: the other
  structure products' live and embargoed editions (every structure
  product's, for a data product), with each `batch` product's batch edition
  in their place. `publish_check(edition_dir, *, product_id, structures,
  partials=(), fingerprints=True)` runs `check_edition(at="publish",
  structures=..., extensions={"statcat": check_catalog}, fingerprints=...)`,
  passing `None` for an empty list, adds a `partials` finding for each of *partials* when *structures*
  is empty, and raises `EditionCheckError`. `cut_partials(manifest)` lists
  the partials a manifest records a `partial_of` for.
- `PARTIAL_OF_KEY` (`"partial_of"`) and `FILL_IN_PROGRESS_KEY`
  (`"fill_in_progress"`) name the `statcat._meta` keys of
  `fill_dataflows`; `recorded_cuts(cursor)` reads the first as a dict, and
  `unvouched_partials(cursor, registry, previous)` is the data pre-check.
- Errors: `WorkingCopyExistsError`, `WorkingCopyClosedError`
  (`RuntimeError`), `SealCheckError` (with `findings`), `EditionCheckError`
  (with `findings`, `product`, `edition`), `JournalError`.

### `inventory.py`

- `InventoryStorage` (a runtime-checkable `Protocol`): `location`,
  `read(path) -> bytes | None` (sdmxlib's reader calls it from worker threads,
  up to 64 at once), `create(path, data)` (`FileExistsError` when
  the name is taken), `write_leaf(path, data)`, `advance_current(root, data)`,
  `now()`, `actor()`. `LocalInventoryStorage(root)` holds the local code, and
  `root` names its directory. `current_is_older(held, root) -> bool` is the rule
  both storages use for `CURRENT`.
- `InventoryWriter(root, *, artefacts=None)`: *root* is a directory or an
  `InventoryStorage`; `storage`; `root` (a `TypeError` on a storage that is not
  local); `artefacts` (the
  reader the rules use; `cross_product.ManifestArtefacts(root)` by default on a
  directory, and `cross_product.ManifestArtefacts(storage.read)` on any other
  storage);
  `catalog_document()`, `has_version()`, `full() -> FullInventory | None`,
  `head() -> Root | None`, `write_catalog_json(*, catalog_id=None)`,
  `check_writable(base, *, now=None) -> CatalogDocument` (the two stage-1
  refusals alone), `try_commit(base, leaves, *, retired=None, embargo=None,
  extensions=None, built_from=None, now=None, kind, reason) -> Root` (no
  cross-product rule), `refusals(full, leaves, *,
  artefacts=None) -> dict[str, CrossProductRuleError]`, and `commit(change,
  *, kind, reason) -> Root`, which runs `refusals` on each planned change and
  raises the first refusal by product id before writing.
- `Change(leaves, retired=None, embargo=None, extensions=None,
  built_from=None)`: what
  `commit`'s *change* callable returns for a head.
- `MAX_RACES = 8`. `producer()` returns
  `Producer(name="statcatlib", version=<installed version>)`.
- Errors: `InventoryConflictError` (another writer created the version
  first), `InventoryContentionError` (`MAX_RACES` lost in a row),
  `InventoryWriteRefusedError` (a subclass of sdmxlib's `InventoryError`: a
  `catalog.json` at 2.x, or a base version more than 5 minutes ahead of this
  machine's clock, named in the message),
  `InventoryRuleError(message, *, rule, product, edition)` with
  `InventoryRuleError.wrapping(exc, ...)` over sdmxlib's `TransitionError`,
  and its subclasses `NonFastForwardError` and `CrossProductRuleError(message,
  *, rule, item, urn, editions, product, edition)` (`rule` one of
  `full-artefacts`, `structure-home`, `publish-order`; `item` 4, 7 or 8;
  `editions` the incoming `(product, edition)` first; `of(refusal)` builds
  one from a `cross_product.Refusal`), and `InventoryCommitUnknownError` (a
  subclass of `InventoryError`: a create on object storage whose outcome stayed
  unknown).

### `blob.py`

`_load_obstore()` imports `obstore` and `obstore.exceptions` for every route to
the store, so core statcatlib imports without the `blob` extra, and a missing one
raises `ImportError` naming the extra. Function-level imports with
`# noqa: PLC0415` load the `blob` extra until Python 3.15 is the minimum
version, when `lazy import` (PEP 810) can replace them.

- `BlobLocation(uri, store, secrets=(), cache_dir=None, ops=...)`: a frozen
  value; `store` is prefixed at the catalog root. `Catalog.open` reads `secrets`
  and `cache_dir`.
- `BlobInventoryStorage(location, *, actor=None, clock=None)`: an
  `InventoryStorage`, built by the caller and passed to `InventoryWriter`.
- `TokenIdentity(provider)`: the credential provider to pass to `AzureStore`;
  `__call__()`, `token() -> str`, `object_id() -> str | None`.
- `_ObjectOps(store)`: the one route to `obstore`: `read(path) -> tuple[bytes,
  str | None] | None`, `put(path, data, *, mode=None, use_multipart=False) ->
  str | None`, `head(path)`, `delete(path)`. Tests subclass it and pass it as
  `BlobLocation.ops`.
- `_endpoint(store) -> str`: `config["endpoint"]`, else Azure's account URL or
  S3's regional URL; `ValueError` when the config names neither.
- `_token_identity(store) -> TokenIdentity | None`: the store's credential provider
  when it is a `TokenIdentity`.
- `AzureBlobRest(location)`: `head(path) -> RemoteFile | None`, `Get Blob Properties`
  by `httpx` with `x-ms-version: 2023-11-03` and the bearer token, for the `Content-MD5`
  `obstore` cannot read. A config with no `container_name`, as on a non-Azure store,
  raises `ValueError`. Only a 404 returns `None`; any other failure raises
  `BlobStoreError`, so a missing role never reads as a missing file. `_send` is the one
  method the account-key test path overrides. `close()`, or use it in a `with`.
- `_statcat_check() -> Check`: `check_catalog`, or `check_catalog_no_index` when
  `lancedb` cannot be found.
- `pull_edition(location, product_id, edition_id, manifest_bytes, dest) -> Path`:
  the edition's `required` and `lazy` own files, from `edition_files`, each checked
  against the manifest's size and sha256 and fsynced in a sibling `.staged-*`
  directory, then the manifest, then each directory, then one rename to *dest*. A
  loser of the rename uses the winner's copy, and refuses another manifest there.
  Staged directories older than a day are swept first, a failure leaves *dest* absent,
  and a `root: data` file is never pulled. It raises `IncompatibleCatalogError`. The caller has
  compared the bytes with the Inventory's SHA-256.
- `_read_remote_manifest` reads an edition's manifest first of its files, and
  compares its SHA-256 with the Inventory's. `_cached_edition` is `Product.view`'s
  steps 2 and 3: a cached copy's manifest compared, else `_read_remote_manifest` and
  `pull_edition`.
- `_SAFE_ID` (`^[A-Za-z0-9][A-Za-z0-9._-]*$`) is what a catalog, product or edition id
  must match to name a cache directory, so no id is a dot path or a staged name;
  `_refuse_unsafe_ids` applies it. `_is_plain_name` and `_is_safe_path` judge manifest
  paths for `pull_edition` and for `upload.py`; `_refuse_unsafe_files` also refuses a
  listed `manifest.json` and a `data_path` leaving the product directory.
- `MAX_CLOCK_SKEW` (30 s). `BlobStoreError(message, *, path, outcome_unknown)`
  (`RuntimeError`): a store fault, with `outcome_unknown` true for `obstore`'s
  `GenericError`.

### `cross_product.py`

Imports only sdmxlib, so `inventory.py` can import it.

- `_records(product_id, entry, artefacts, *, incoming)`: where every read site
  takes an entry's records (Phase 5 D6). An incoming entry's come from
  *artefacts*, which reads its manifest and checks its SHA-256 (spec §6.5 item
  2); any other entry's from `entry.artefacts` when filled, else from
  *artefacts*. `refusals`, `stale_editions`, `homeless_partials`,
  `cited_structures` and `working_copy`'s `_may_be_refused` call it.
- `refusals(full, leaves, artefacts) -> dict[str, Refusal]`: the rules over
  the version *leaves* would write on *full*, for each incoming entry (one
  that becomes live or embargoed, `incoming(full, leaves)`), comparing
  editions of other products only; empty, with no artefact read, when
  nothing is incoming. `Refusal(product, edition, rule, item, urn, editions,
  message)`.
- `type Artefacts = Callable[[str, str, str], Mapping[str, PartialRecord]]`:
  `(product, edition, manifest_sha256)` to the edition's full artefacts and
  its partials that record a `partial_of`. `ManifestArtefacts(source: Path |
  Fetch)` reads `products/<p>/<e>/manifest.json` from a catalog directory, or
  through an `sdmxlib.inventory.Fetch` such as `InventoryStorage.read`, checks
  its SHA-256 and decodes it with `decode_manifest` (`ManifestError` for an
  absent, unreadable, mismatched or malformed manifest; a fetch's own error,
  such as `BlobStoreError`, propagates) and caches by `(edition_id, sha)`;
  `StoreArtefacts(edition_id, read, *, fallback)` answers one working copy's edition with `read()` and the rest with
  *fallback*. The conversion is `sdmxlib.inventory.manifest_records`.
- `StaleEdition` and `stale_editions(full, artefacts)`: each live or
  embargoed data edition and URN whose fingerprint (a partial's
  `partial_of`) no live or embargoed edition of its structure home holds.
- `HomelessPartial` and `homeless_partials(full, artefacts)`: each partial
  of a live or embargoed data edition whose URN no live or embargoed
  structure edition holds full, as after its home drops the URN.
  `stale_editions` skips a URN with no home; item 4 refuses the edition's
  release, and `doctor` warns.
- `cited_structures(full, artefacts)`: the structure editions, any state, a
  live or embargoed data edition's `partial_of` cites (prune keeps them).
- `ACTIVE` (`live`, `embargoed`), `CrossRule`.

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
  (`InventoryRuleError`, `rule="lifecycle"`) or another kind
  (`rule="kind"`), takes the live edition at
  sdmxlib's store `SCHEMA_VERSION` as its base, and calls
  `LocalRegistry.build(previous=)` or, with `resume=True`,
  `LocalRegistry.build(resume=)`. An existing unsealed edition raises
  `WorkingCopyExistsError` without `resume`, and `resume` with none raises
  `FileNotFoundError`. A new edition whose store fails to open is discarded.
  It then writes `statcat._meta.product_kind`: a new edition takes the
  leaf's kind, the caller's, or `"data"`, and drops the `partial_of` and
  `fill_in_progress` keys carried from the previous edition; a resumed one
  keeps the stored kind and refuses another (`rule="kind"`), after closing
  the registry.
- `WorkingCopy.seal` runs these steps: settle the journal; run each check
  against `view()`, on a structure working copy `bundle.structure_content`,
  and on a data working copy `unvouched_partials` (a set
  `_meta.fill_in_progress`, or a partial `_meta.partial_of` does not name
  whose fingerprint differs from the one the previous manifest records with
  a `partial_of`); any finding raises `SealCheckError` and leaves the
  working copy open; raise `NonFastForwardError` when the base is no
  longer the live edition; the cross-product pre-check (`_cross_precheck`:
  `_planned(full)`, a provisional `record` with a placeholder
  `manifest_sha256`, checked by `precheck_refusals` when `_may_be_refused`
  says a rule could refuse it; `_own_artefacts` fingerprints the store's
  artefacts as the seal does, each partial's `partial_of` from `_cuts`),
  whose `CrossProductRuleError` leaves the working copy open; rebuild the search index and `drop_carried`;
  `catalog_section(store, kind=..., lifecycle=...)`; read `_meta.partial_of`, kept to the
  URNs `sdmx.artefact` holds as partials; close the store and registry;
  `sdmxlib.edition.seal(partial_of=...)`, which writes the journal entry and
  then the manifest, and carries the previous manifest's `partial_of` for a
  partial the map does not name; on a structure edition, or one whose
  manifest lists a partial with `partial_of`, `_publish_check(structures,
  partials=...)` (step 5b; a finding raises `EditionCheckError` and leaves
  the edition unrecorded). `_publish_check` passes `fingerprints=False`:
  the seal just wrote the manifest's fingerprints from this store, so
  sdmxlib skips recomputing them (sdmxlib GH-357) and still hashes every
  file. The journal repair's `publish_check` and `verify_catalog` check an
  edition another process sealed, and recompute;
  commit the record (`kind="seal"`, `_record_change`), live when the
  catalog's embargo is off, the cross-product rules running in `commit` (an
  edition the head records already with its `manifest_sha256`, by another
  writer's journal repair, counts as recorded); clear the journal entry. The steps are split for
  `Catalog.seal`: `_precheck(checks)` (steps 0 to 2), `_seal_edition()`
  (steps 3 to 5, returning a private `_Sealed` with the edition, its
  manifest's store schema, its `routing_fields` and its `cut_partials`), `_next_leaf(full, sealed)` (the record's leaf, with `recorded_routing`;
  `InventoryRuleError` on a refusal), `_publish_check(structures)`,
  `_recorded()` and `_unrecorded(exc)` (clear the journal entry; the second
  also logs the WARNING, for an `InventoryRuleError` or an
  `EditionCheckError`).
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
  of a seal and after its record, against the newest root, over every
  product's journal. An entry of the product whose edition the root names or
  tombstones, or whose edition has no manifest, is cleared. Every product's
  sealed, unrecorded edition is recorded in one commit, with the kind its
  manifest's `statcat` section names, and its entry cleared; a `duplicate`
  refusal with the same `manifest_sha256` counts as recorded. Another
  product is acted on only for an entry with a manifest, and only when the
  head records the product; `exclude` (a `Catalog.seal` batch) and an
  unreadable journal leave it out. A structure edition, and a data edition
  whose manifest lists a partial with `partial_of`, is first given the
  publish check, with the other structure products' live and embargoed
  editions and each other product's journalled structure edition in their
  place. The cross-product rules run on the change; a refused product, and
  one whose check was given a journalled structure edition not recorded,
  is dropped. Any other refusal, and a failed check, clears the entry and
  logs a WARNING.
  `InventoryContentionError` and `InventoryWriteRefusedError` keep the entry
  for a later working copy. An
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
  `restore_epoch`. Every root it writes carries `previous_root_sha256` and
  `changes`, from root `seq - 1`'s stored bytes and the leaves of each product
  whose pointer or tombstones change, also when `built_from` names an older
  version; only those leaves are read. `try_commit` runs `check_writable`
  first: `catalog.json` is decoded with `decoder="stage-1-writer"`, and
  `next_created_at` refuses a base more than 5 minutes ahead. The seal's
  `_precheck` runs it too, so a refused seal writes nothing and leaves the
  working copy open. `catalog_document()` decodes as a reader does.
  `commit` re-reads the head and calls *change* again after
  each `InventoryConflictError`, so every rule is evaluated against the
  version the commit lands on, and raises `InventoryContentionError` after
  `MAX_RACES`. A root records `catalog.json`'s `restore_epoch`, and on local
  disk its event's actor is `<os user>@<hostname>`, taken from the process.
  `try_commit` resolves the actor before it writes the first leaf, and takes
  its time from `storage.now()` when not given `now=`.
- Over object storage the writer's rules are the same, and `BlobInventoryStorage`
  differs in these ways. Each root and `catalog.json` is one
  `put(mode="create")`, one Put at any size. Each leaf a version changes is one
  unconditional `put(use_multipart=False)` with no existence check, and a
  carried-forward leaf gets no Put. `create` settles `AlreadyExistsError` and
  `GenericError` by reading the name: equal bytes return, so twin roots count as
  one commit, other bytes raise `FileExistsError`, and an absent name is tried
  once more before `InventoryCommitUnknownError`; any other store fault raises
  `BlobStoreError` at once. `write_catalog_json` reads first, so a second run
  raises `FileExistsError` on every storage. `advance_current` reads `CURRENT`
  and writes on its `e_tag`, or `mode="create"` when absent, up to `MAX_RACES`
  times, and any failure logs a WARNING and returns, since the root is the
  commit point. `now()` is the `Date` of one anonymous `HEAD` at `_endpoint`,
  one per `try_commit`, refused when missing, malformed, unreachable or more
  than `MAX_CLOCK_SKEW` behind this machine; given `clock=` it sends nothing,
  and a `MemoryStore` without one refuses. `actor()` resolves once: the `oid` of
  a `TokenIdentity` on the store, else `actor=`, else
  `InventoryWriteRefusedError`.
- Every entry a seal, a batched seal or the journal repair records carries
  `dataflows`, `pull_bytes` and `artefacts` equal to its manifest's
  `routing_fields`, data and structure editions alike (GH-125 part 4). The
  cross-product rules take an incoming entry's records from its manifest, after
  the SHA-256 check, and never from its leaf entry, so no edition becomes live
  or embargoed on a manifest the Inventory does not record; another product's
  filled entry is read from its leaf, and an entry recorded before 0.17 from its
  manifest (`cross_product._records`).
- A blob `commit` with an incoming entry runs the rules through
  `ManifestArtefacts(storage.read)`: each incoming manifest is fetched and
  checked, and an absent one raises `ManifestError` before anything is
  written. `check_writable` refuses a 2.x `catalog.json` on blob as on disk.
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
  rename; each file and directory is fsynced. `pull_edition` writes under the
  `cache_dir` it is given, and nothing else in `blob.py` touches local disk. Over `BlobInventoryStorage` it
  writes the same names under the store's prefix, and each `try_commit` sends
  one anonymous `HEAD` to the store's endpoint unless given a clock.
- `discard_orphans()` drops LanceDB tables the manifest does not name.
- Emits `warnings.warn` for a quantised (stale) vector index and for changed search annotations, and logs a WARNING for an edition the journal rule cannot record.

## Verification

- Run: `just ci`
- Unit tier only: `uv run pytest -m unit`
- The blob writer: `tests/test_blob_storage.py` (unit, over `MemoryStore`,
  with `pytest-httpx` for the clock) and `just test-stores`:
  `tests/stores/test_blob_conformance.py` (sdmxlib's conformance suite over
  Azurite) and `tests/stores/test_blob_writer.py` (conditional headers, by a
  recording proxy). The tier needs a container runtime and fails without one.
- The upload: `tests/test_upload.py` (unit, over `MemoryStore`),
  `tests/test_azure_blob_rest.py` (unit, the bearer path by `pytest-httpx`),
  `tests/test_cli_upload.py` (unit, the command) and
  `tests/stores/test_upload_store.py` (Azurite, with an account-key digest read).
  Nothing here runs on real Azure: `docs/api/blob.md` lists the real-account checklist.
- Remote catalogs: `tests/test_blob_open.py` (unit, refusals, `pull_edition` and hostile
  ids over `MemoryStore`) and `tests/stores/test_remote_catalog.py` (Azurite, the read
  path through `obstore` and DuckDB, the embargo, the rename race).
- The Inventory writer: `tests/test_inventory_writer.py` (unit),
  `tests/test_inventory_conformance.py` (integration, sdmxlib's conformance
  suite and `run_history`) and `tests/test_inventory_history.py` (integration:
  the chain over seals, a correction, a lost race, the fast base, an opted-in
  catalog). The routing fields: `tests/test_inventory_routing.py`. The cross-product rules: `tests/test_cross_product_rules.py` (unit,
  `refusals` over built leaves) and `tests/test_cross_product.py`
  (integration: seals, batches, releases, rollbacks, stale editions,
  partials with no structure home, the committed sabotage test). Seals: `tests/test_seal_checks.py`, `tests/test_seal_crash.py`
  and `tests/test_concurrent_seal.py`; the batched seal in
  `tests/test_catalog_model.py::TestBatchSeal`. Structure products, their
  publish check (and which checks skip the fingerprint recompute), batch
  and journal repair: `tests/test_structure_products.py`. Partials in data products
  (`fill_dataflows`, `_meta.partial_of`, the pre-check, the publish check,
  resume, batch and repair): `tests/test_partials.py`. Views:
  `tests/test_edition_view.py`. Embedding reuse:
  `tests/test_embedding_reuse.py`.

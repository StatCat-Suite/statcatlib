# Upgrading

This page lists each breaking change, newest first, with what replaces it.
statcatlib is pre-1.0, so a minor version may break; the
[CHANGELOG](https://github.com/StatCat-Suite/statcatlib/blob/main/CHANGELOG.md)
lists every change.

## 0.12.0 (unreleased): catalogs of products and editions, on sdmxlib 0.81

statcatlib requires `sdmxlib>=0.81.0,<0.82`. sdmxlib's
[upgrading guide](https://statcat-suite.github.io/sdmxlib/upgrading/) lists
its own breaking changes from 0.73 to 0.81; the ones below are those a
statcatlib user sees.

Serve a 0.12 catalog with sdmxlib-rest 0.9.0 or later, which reads editions
and requires sdmxlib 0.81. Earlier sdmxlib-rest releases cannot read it.
sdmxlib 0.81 adds the Inventory event kind `describe`, which
`CatalogAdmin.describe` writes; sdmxlib 0.80 and older readers skip a
`describe` version and stay on the version before it, so upgrade every other
reader of a catalog to sdmxlib 0.81 before you describe it.

A catalog is now a directory of products, each a chain of immutable
editions, with an Inventory that records which edition of each product is
live ([Catalog format](catalog-format.md)). statcatlib 0.12 refuses every
0.11 catalog and 0.72 bundle, and there is no migrator: rebuild each catalog
by re-ingesting it.

### Rebuild every catalog by re-ingesting

sdmxlib 0.80 stores SDMX structure at store schema 2.52, and statcatlib 0.11
wrote 2.48; the layout changed too. `Catalog.open` and `Catalog.create`
refuse a directory holding a 0.11 catalog (a root-level `meta.duckdb`), a
0.72 bundle (`bundle.manifest.json`), or other files, with
`IncompatibleCatalogError`, and change nothing in it. To rebuild:

1. Create a catalog in a new, empty directory:
   `cat = Catalog.create("~/stats")`.
2. For each product, start a working copy and ingest into it as before:
   `wc = cat.product("14100287").working_copy()`, then
   `ingest_sdmx(wc, registry)`, `ingest_data(wc, rest_registry, identifier)`,
   `ingest_ckan(wc, base_url)` or `wc.put(...)`.
3. Optionally call `wc.build_index(...)` with the options your index needs;
   the seal rebuilds the index with the same arguments.
4. `wc.seal()`. The edition is recorded live.
5. Read it with `Catalog.open("~/stats").product("14100287").view()`.

### The catalog API

| 0.11 | 0.12 |
| --- | --- |
| `Catalog(path)`, `Catalog(path).open()` | `Catalog.create(path)` creates a catalog, `Catalog.create(path, exist_ok=True)` reopens one for writing, `Catalog.open(path)` opens one read-only. `Catalog(...)` raises `TypeError` naming these. |
| `Catalog()`, `Catalog(":memory:")` | `statcatlib.testing.temporary_catalog()`, a catalog in a temporary directory |
| `Catalog(path, read_only=True)` | `Catalog.open(path)`; every view is read-only |
| `Catalog.from_connection(conn)` | none; a catalog owns its storage |
| `cat.put`, `put_concept`, `add_lineage`, `add_language_alternates`, `bulk_load`, `transaction`, `build_index` | the same methods on `cat.product(id).working_copy()` |
| `cat.registry` for writes (`ingest`, `add`), `cat.data.write_observations` | `wc.registry`, the working copy's writable `LocalRegistry`; `wc.registry.write_observations` |
| `cat.get`, `query`, `search`, `facets`, the scopes, `code_map`, `metadata_for`, `index_layout` and every other read | the same methods on an `EditionView`: `wc.view()`, or `Catalog.open(path).product(id).view()`. On a `Catalog`, each raises `AttributeError` naming `product(id).view()`. |
| `cat.registry` for reads (`get`, `query`, `obs`) | `view.registry` |
| `Catalog(path, embedding_model=m, document_prefix=..., query_prefix=...)` | `product.working_copy(embedding_model=m, ...)` to build, `product.view(embedding_model=m, query_prefix=...)` to search |
| `cat.connection`, `cat.cursor`, `cat.bundle_dir`, `cat.uri`, `cat.data` | none: callers reach storage only through the API |
| `ingest_sdmx(cat, ...)`, `ingest_sdmx_registry`, `ingest_data`, `ingest_ckan` | the same, given a `WorkingCopy`; given an `EditionView` they raise `TypeError` naming `working_copy()` |
| `statcat build-index BUNDLE` | `statcat build-index CATALOG PRODUCT`, which indexes the product's unsealed working copy |
| `statcatlib.testing.synthetic_catalog(path, shape, *, seed=0, index=True)` returning a `SyntheticCatalog` | `synthetic_catalog(path, shape, *, product_id="synthetic", seed=0)`, which creates the catalog, seals one edition and returns the `Edition` |
| `statcatlib.bundle.catalog_section(cat)` | `WorkingCopy.seal` writes the section (format 3); `catalog_section` takes the private store |
| `STATCAT_SECTION_FORMAT` 2 | 3, which adds `kind`; `check_catalog` refuses format 2 |
| `search/index.json` version 2, tables `search_<lang>__<generation>` | version 3, tables `search_<lang>__<edition_id without hyphens>`; version 2 is refused |

New names: `Catalog`, `CatalogAdmin`, `Product`, `Edition`, `EditionView`, `WorkingCopy`,
the module `statcatlib.checks` (seal checks, such as `checks.conf_status()`),
and the errors `NoLiveEditionError`, `WorkingCopyExistsError`,
`WorkingCopyClosedError`, `SealCheckError`, `JournalError`,
`InventoryRuleError`, `NonFastForwardError`, `InventoryConflictError` and
`InventoryContentionError`.

### What a seal does

- A product has one working copy at a time. A second `working_copy()`
  raises `WorkingCopyExistsError`; `working_copy(resume=True)` reopens the
  unsealed edition, and `discard()` deletes it. `close()` leaves it unsealed
  for a later resume.
- `wc.seal()` rebuilds the BM25 search index from the edition's rows when
  `statcatlib[search]` is installed, writes the manifest, and records the
  edition `live` in a new Inventory version, superseding the edition it was
  built on. After a seal or a discard, every method on the working copy and
  its views raises `WorkingCopyClosedError`.
- A working copy built on an edition that is no longer live is refused at
  seal with `NonFastForwardError`. Discard it and start from the live one.
- `add_language_alternates` and the CKAN `language` list store tags lowercase,
  as `sdmxlib.normalize_tag` spells them.

### Checking a catalog and reporting on it

Two new commands only read a catalog
([Catalog administration](api/admin.md)):

- `statcat verify-catalog CATALOG` checks the Inventory and every edition it
  records, `data/` files included. It exits 0 when clean, 1 on a finding,
  naming the file, and 2 when the catalog cannot be read.
- `statcat doctor CATALOG` reports a synced-folder path, unsealed editions,
  seal journal entries, sealed editions no Inventory version names, and files
  a writer left in `inventory/`. It writes nothing.

### Rollback, embargo, the catalog description and prune

`CatalogAdmin.open(path)` makes the operational changes to a catalog
([Catalog administration](api/admin.md)). Each takes a `reason` and writes
one Inventory version:

- `rollback(product, reason=...)` withdraws the live edition and makes the
  edition it superseded live again. `Product.rollback` raises
  `AttributeError` naming it.
- `set_embargo(on=True, reason=...)` makes later seals record their editions
  `embargoed`; `release(product, edition, reason=...)` makes one live, and
  `cancel(product, edition, reason=...)` withdraws it. `release` uses the
  local clock.
- `describe(description, reason=...)` records a `CatalogDescription`'s
  catalog-level fields, which `Catalog.description` returns.
- `editions(product)` lists every edition in every state.
- `prune(older_than=timedelta(days=30), reason=...)` retires superseded and
  withdrawn editions older than the window, deletes the directories of
  editions tombstoned for longer, and deletes `data/` files no manifest
  lists. It skips a product with a working copy or a seal journal entry. It
  is a dry run unless `dry_run=False`; `statcat prune CATALOG --older-than
  DAYS --reason TEXT --apply` runs it from the command line.

### The bundle API and the data-path policies are removed

| Removed | Replacement |
| --- | --- |
| `open_bundle`, `inspect_bundle` | `Catalog.open(path).product(id).view()` |
| `Embedded`, `Colocated`, `At`, `Auto`, `BundleInfo`, `BundleDataPathError` | none; an edition reads its data files from its product's `data/` |
| `read_current_data_path`, `normalize_lake_data_path` | none |
| `statcatlib.bundle.publish_bundle` | `WorkingCopy.seal` |
| `statcatlib.bundle.check_bundle` | `sdmxlib.edition.check_edition(path, at=..., extensions={"statcat": check_catalog})` on an edition directory |
| `statcat bundle check` | `statcat verify-catalog CATALOG`, which checks every edition the Inventory records; `sdmxlib edition check` checks one edition directory |
| `statcat bundle explain`, `rebind`, `attach-cmd` | `sdmxlib edition copy` and `files`; `rebind` has no replacement, because an edition resolves its data path against its own directory |

`check_catalog(root, manifest, reg, profile)` takes sdmxlib 0.80's `Check`
arguments: an `EditionRegistry` and the `check_edition` profile.

A deployment that keeps observations on object storage through a remote
`data_path` stays on statcatlib 0.11 until Phase 4 brings remote editions.

### Language tags are lowercase and checked

sdmxlib 0.79 stores every language tag in the form `sdmxlib.normalize_tag`
returns: lowercase, and well-formed by RFC 5646. statcatlib follows it:

- Every public `lang` parameter, every `.lang()` setter and the filters
  `has_spatial`, `has_keyword`, `title_contains` and `description_contains`
  normalise the tag they are given, so `lang="fr-CA"` finds a label stored
  under `fr-ca`. A malformed tag, such as `fr_CA!` or `fr_CA`, raises
  `sdmxlib.InvalidTagError`, a `ValueError`.
- CKAN ingest strips each tag and replaces `_` with `-` before it
  normalises it, so `fr_CA` is stored as `fr-ca`. A tag that is still
  malformed is dropped from its field with a WARNING, and `ingest_ckan`
  logs how many it dropped. When two tags normalise to one (`EN` and `en`),
  the first text is kept, with a WARNING.

`InternationalString` lookups return `sdmxlib.LocalisedString`. Read the
text as `name["en"].text`, or take a plain `dict` from `name.as_dict()`;
`name.get("en")` returns a `LocalisedString` or `None`.

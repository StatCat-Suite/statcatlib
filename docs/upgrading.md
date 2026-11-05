# Upgrading

This page lists each breaking change, newest first, with what replaces it.
statcatlib is pre-1.0, so a minor version may break; the
[CHANGELOG](https://github.com/StatCat-Suite/statcatlib/blob/main/CHANGELOG.md)
lists every change.

## Next release: the `blob` extra and the Inventory writer on object storage

A local catalog keeps its files, its writer and its checks. The new `statcatlib[blob]` extra (`uv add 'statcatlib[blob]'`, or
`pip install 'statcatlib[blob]'`) adds `obstore`, `azure-identity`, `typer` and
`rich`, and `statcatlib.BlobLocation`, `statcatlib.TokenIdentity` and
`statcatlib.store.blob` give an `InventoryWriter` a catalog in object storage.
[Object storage](api/store.md#the-inventory-on-object-storage) describes it.

`InventoryWriter` takes a directory or an `InventoryStorage`. `InventoryWriter.root`
raises `TypeError` on a catalog in object storage, and `InventoryWriter.storage`
returns the storage on either kind.

The `lifecycle` refusal from `working_copy()` now says that a product keeps the
lifecycle it was first recorded with.

`just ci` gains the `stores` tier, so a push from a clone needs a container
runtime.

## 0.14.0: Inventory history fields, on sdmxlib 0.85

statcatlib requires `sdmxlib>=0.85,<0.86`. sdmxlib's
[upgrading guide](https://statcat-suite.github.io/sdmxlib/upgrading/) lists
its own changes. No catalog is rebuilt: a 0.13 catalog opens and seals under
0.14, and its next version carries the history fields. The Inventory format
becomes 1.2, which every reader of an Inventory 1.x reads. The store schema, the
manifest, the `statcat` schema, the `statcat` section format and the search
index manifest stay as they were.

`InventoryWriter` writes `previous_root_sha256` and `changes` in every root,
computed from root `seq - 1` and its leaves. Nothing expires yet.

Two commits that used to land are now refused with the new
`InventoryWriteRefusedError`, a subclass of `sdmxlib.inventory.InventoryError`
exported from `statcatlib`:

- A base version whose `created_at` is more than 5 minutes ahead of this
  machine's clock. The message names the base version's time and this
  machine's. Fix the clock that is wrong. A clock behind by less than that
  writes the base version's time plus 1 ms.
- A `catalog.json` at format 2.x, which marks a catalog opted in to expiry.
  A later statcatlib writes to it. Readers, `Catalog.open`, `CatalogAdmin.open`
  and `statcat verify-catalog` still read it.

`WorkingCopy.seal` raises it before it writes anything, and leaves the working
copy open. Raised at the record, it keeps the journal entry for a later
`working_copy()`, as `InventoryContentionError` does. `Catalog.seal` reports it
in `SealOutcomes.refused`, and `statcat prune` prints it and exits 1. Code that
catches `InventoryRuleError` around a commit also needs to catch it.

`InventoryWriter.try_commit` takes `built_from=` and `now=`, and `Change` takes
`built_from`, for a correction built from an older version.
`InventoryWriter.check_writable(base)` runs the two refusals alone.

### Routing fields

Each edition a seal or the journal repair records carries `dataflows` and
`pull_bytes`, from `sdmxlib.inventory.routing_fields`, and a structure edition
also carries `artefacts`. An entry an earlier statcatlib recorded keeps none
of them, and readers fall back to its manifest. A release, a rollback and a
reinstate keep what the entry holds.

`statcat verify-catalog` reports a filled field that differs from the
manifest as `FAIL inventory/leaves/<sha256>.json: ...`, and skips an entry
without the fields.

`statcatlib.store.cross_product.manifest_records` is gone. Import
`sdmxlib.inventory.manifest_records`, which is the same function.

## 0.13.0: structure products, on sdmxlib 0.84

statcatlib requires `sdmxlib>=0.84,<0.85`. sdmxlib's
[upgrading guide](https://statcat-suite.github.io/sdmxlib/upgrading/) lists
its own changes. No catalog is rebuilt: the store schema stays 2.52, the
manifest 3.0, the Inventory 1.0, the `statcat` schema 2.2, the `statcat`
section format 3 and the search index manifest 3. A 0.12 catalog opens and
seals under 0.13.

### Structure products

A product is now a data product or a structure product
([Structure products](api/store.md#structure-products)):

- `Product.working_copy(kind=...)` takes `"data"` or `"structure"`. The
  default, `None`, takes the kind the product is recorded with, or `"data"`
  for a new product, so existing calls are unchanged. `WorkingCopy.kind`
  returns it.
- `WorkingCopy.fill_structures(source, *, families, agency, ids, artefacts)`
  copies full artefacts into a structure product's working copy, through
  `sdmxlib.edition.fill_structures`.
- A product reopened with the other kind raises `InventoryRuleError` with
  `rule="kind"` before anything is built, as one reopened with another
  lifecycle does with `rule="lifecycle"`.
- A structure working copy holding a dataflow, a binding or a partial fails
  its seal with `SealCheckError`, and `check_catalog` reports each one in a
  sealed structure edition.
- A structure edition's seal runs `sdmxlib.edition.check_edition` at
  `publish` against the catalog's other structure products before the
  Inventory commit. A finding raises the new `EditionCheckError` (exported
  from `statcatlib`) and leaves the sealed edition unrecorded. In
  `Catalog.seal` the product goes to `SealOutcomes.refused`. The journal
  repair runs the same check, and clears a failing edition's entry with a
  WARNING.
- `Catalog.seal` seals and checks the batch's structure products before its
  data products, and drops a structure edition whose publish check was given
  a batch edition that was not recorded.
- `statcatlib.bundle.catalog_section(store, *, kind="data")` takes the kind
  it writes.

### Partials in data products

- `WorkingCopy.fill_dataflows(source, dataflows, *, observations, partials)`
  fills a data product's working copy through
  `sdmxlib.edition.fill_edition`, cutting each codelist to a partial, and
  keeps the map of cuts in `statcat._meta.partial_of`
  ([Partials in data products](api/store.md#partials-in-data-products)).
  The seal passes it to sdmxlib as `partial_of`, also after
  `working_copy(resume=True)` in another process.
- `ingest_dataflows(wc, source, dataflows, *, observations, partials,
  publisher, themes)`, exported from `statcatlib`, fills through
  `fill_dataflows` and stores a dataset per dataflow. `ingest_sdmx` still
  copies an in-memory registry whole.
- The seal of an edition whose partials record a `partial_of` runs the
  publish check with every structure product's live and embargoed editions,
  and raises `EditionCheckError` for a partial that matches none of them or
  holds a code its full codelist lacks, and for a partial in a catalog with
  no structure edition. A data edition with no partial seals as in 0.12.
- The seal refuses, with `SealCheckError`, a working copy whose
  `fill_dataflows` stopped before it recorded its cuts, and one holding a
  partial cut outside `fill_dataflows` (a direct `fill_edition` on
  `wc.registry`) that would carry the previous edition's `partial_of`.
- `statcatlib.testing` gains the CODR generator: `CodrShape`,
  `STRUCTURE_PRODUCTS`, `CODR_AGENCY`, `codr_structures` and `codr_product`.

### The cross-product rules

The Inventory writer now refuses a version that breaks specification §6.5
item 4, 7 or 8 for an edition entering `live` or `embargoed`, with the new
`CrossProductRuleError` (exported from `statcatlib`, a subclass of
`InventoryRuleError`). It names the rule, the item, the URN and the editions
compared, and nothing is written
([The cross-product rules](catalog-format.md#33-the-cross-product-rules)):

- `rule="full-artefacts"` (item 4): a data edition's full artefact that a
  structure product holds differs from every live or embargoed edition of
  that structure home; a partial's `partial_of` matches none; a cut codelist
  has no structure home; or a full artefact no structure product holds, such
  as a DSD two data products share, differs from another data product's live
  copy when it enters `live`.
- `rule="structure-home"` (item 7): a structure edition entering `live`
  holds a full artefact another structure product holds live.
- `rule="publish-order"` (item 8): a data edition entering `live` holds a
  partial or a full artefact whose fingerprint only an embargoed structure
  edition holds; release that structure edition first.

The rules run in every seal, `Catalog.seal`, `CatalogAdmin.release` and
`CatalogAdmin.rollback`. The seal checks them before sdmxlib's seal, so a
refusal there leaves the working copy open; a refusal at the record, after
another writer changed the head, leaves the sealed edition unrecorded.
`Catalog.seal` checks the batch together, so data products that change a
shared full artefact with no structure home pass when they seal in one
batch. `CatalogAdmin.release` releases one product per call, so under
embargo such a change cannot go live until the embargo is off and the
products are sealed in one batch.

What changes for existing code:

- A data product filled with `fill_dataflows` or `ingest_dataflows` needs a
  structure product holding each codelist it cuts, or `partials=False`.
- Two data products holding one URN with different content (two copies of a
  DSD or a codelist, from `ingest_sdmx` included) can no longer both be live:
  the second seal raises `CrossProductRuleError`.
- `InventoryWriter(root, *, artefacts=None)` reads each live and embargoed
  edition's `manifest.json` for the rules, and raises
  `sdmxlib.edition.ManifestError` for one it cannot read or whose SHA-256
  differs from the Inventory's. Code that commits leaves naming editions
  with no manifest on disk passes an `artefacts` reader.
- `InventoryRuleError.rule` also takes `"full-artefacts"`,
  `"structure-home"` and `"publish-order"`.

A structure edit is accepted while data editions hold the old copy. Those
data editions are **stale**: a stale live edition keeps serving until its
product is republished, and a stale embargoed edition is refused at release.
`statcat verify-catalog` prints each as a `STALE` line
(`VerifyReport.stale`, of `statcatlib.diagnostics.StaleEdition`) without
counting it as a finding, and `statcat doctor` reports it under
`stale data editions`. `statcat doctor` also warns, under `partials with no
structure home`, about each data edition holding a partial whose URN no
structure edition holds full any more; item 4 refuses its release.
`verify-catalog` also checks each data edition's
partials against the structure editions they were cut from, and each
structure edition's references in the other structure products' live and
embargoed editions. `CatalogAdmin.prune` keeps a structure edition that a
live or embargoed data edition's `partial_of` cites.

The journal repair is catalog-wide: a product's next `working_copy()`
records every product's sealed, unrecorded editions in one version,
leaving out the products of a running `Catalog.seal` and a product whose
first edition has no leaf yet.

Write a catalog that holds a structure product with statcatlib 0.13 only. A
0.12 writer records every edition as `kind: "data"`, so sdmxlib refuses its
record of a structure product's edition after the seal, and the edition stays
unrecorded; it also applies none of the cross-product rules.

## 0.12.0: catalogs of products and editions, on sdmxlib 0.81

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

### Records need an identifier, and no text is invented in English

- `put()` raises `ValueError` for a publisher or creator without an id,
  where it used to hash the English (else French) name. SDMX ingest passes
  the agency id and CKAN ingest the organization slug; a CKAN organization
  with neither slug nor UUID now gives no publisher.
- A contact point is identified by its email, else its URL, else its phone,
  else every (tag, text) pair of its name, so contact ids change. A contact
  point with none of these raises `ValueError`.
- A dimension without a concept name is labelled by its id under `zxx`, and
  SDMX ingest labels an unnamed agency and an unresolved concept the same
  way, where it wrote the id as `en` (and `fr`).
- CKAN ingest stores each language under the tag the portal sends,
  including open.canada.ca's `en-t-fr` and `fr-t-en`, which it dropped.
  `fallback_en` is removed, so a package with no English title no longer
  gets its id as one. Untagged text is stored under `und`, and codes stored
  as text (subject slugs, frequency codes) under `zxx`.
- `ingest_ckan` records the language pairs a portal's schema declares
  (`CkanClient.alternate_languages()`); read them with
  `EditionView.language_alternates()`.

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

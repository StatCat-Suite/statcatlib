# The statcat catalog format

A statcat catalog is a directory of products, each a chain of immutable
editions, in the layout of sdmxlib's
[sharded catalog spec](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
§4.1. sdmxlib owns every document format: `catalog.json`, `CURRENT`, the
Inventory's roots and leaves, and each edition's `manifest.json`, with its
file digests and checks. statcatlib writes the Inventory, and this page
specifies what it adds: the `statcat` schema in each edition's
`metadata.duckdb`, the `search/` directory, the `statcat` extension section
of the manifest, and the checks that hold the three to each other.

It describes **statcat section format 3**, **search index manifest version 3**
and **embeddings sidecar version 4**, on sdmxlib's manifest format 3.0 and
Inventory format 1.2. statcatlib's code is the reference implementation
(`statcatlib.catalog`, `statcatlib.bundle`, `statcatlib.store.inventory`,
`statcatlib.store.lance`).

MUST, MUST NOT and MAY carry their RFC 2119 meanings. A *producer* seals an
edition; a *reader* opens one. A requirement sdmxlib's spec already makes is
not repeated here.

## 1. Layout

```text
<catalog>/
  catalog.json                       the catalog's id and restore epoch (sdmxlib)
  CURRENT                            names the newest Inventory version, as a hint
  inventory/
    <seq>.json                       one root per Inventory version, seq zero-padded to 12 digits
    leaves/<sha256>.json             one leaf per product and version, named by the hash of its bytes
  products/<product_id>/
    data/                            DuckLake's data files, shared by every edition of the product
    journal/<edition_id>.json        a seal not yet recorded in the Inventory
    <edition_id>/                    one directory per edition, named by its UUIDv7
      metadata.duckdb                sdmxlib's sdmx.* schema, plus the statcat.* catalog schema (§2)
      tables.ducklake                the DuckLake catalog for observations (sdmxlib)
      search/                        the search index (§4)
        index.json                   names the table for each language
        search_<lang>__<edition>.lance   one LanceDB table per language
        embeddings.json              the embedding model's settings; only with vectors
      manifest.json                  sdmxlib's manifest, with a statcat section (§3), written last
  pending/<product_id>/<edition_id>.json   an uploaded edition waiting for the fold, in object storage only (§1.5)
```

- `Catalog.create` writes `catalog.json` and nothing else. The first seal
  writes Inventory version 1 and `CURRENT`.
- statcatlib's Inventory writer is the only code that writes `catalog.json`,
  `inventory/` and `CURRENT`. It creates each file by writing a staged file,
  fsyncing it, hard-linking it to its name and fsyncing the directory, so a
  name never holds half a file and two writers never both create one root.
  `CURRENT` is replaced only by a higher `seq`.
- Every root carries `previous_root_sha256`, the SHA-256 of the stored bytes
  of root `seq - 1`, and `changes`, the change list against root `seq - 1` and
  its leaves. A correction built from an older version records that version
  in `previous_inventory` and still links and diffs against root `seq - 1`. A
  writer that loses a race computes both again from the new head. The first
  root records `null` and its products. A root an earlier statcatlib wrote has
  neither field, and the next root links it the same way.
- Every leaf entry a statcatlib of this format records carries `dataflows`,
  its manifest's binding URNs sorted and without duplicates, `pull_bytes`,
  the size of its `required`-tier files, and `artefacts`, its manifest's
  artefact records. statcatlib 0.17 fills `artefacts` for data and structure
  editions; 0.14 to 0.16 filled it for structure editions only. An entry
  without the fields is read from its manifest, and `statcat verify-catalog`
  checks each filled field against the manifest.
- `created_at` is at least the base version's plus 1 ms, and a writer refuses
  a base version more than 5 minutes ahead of its clock. A writer of this
  format takes no catalog lock, so it refuses a `catalog.json` at format 2.x.
- `Catalog.open` reads the Inventory with sdmxlib's `LiveInventory` and
  writes nothing to the catalog. Over a `BlobLocation`, a view writes the live
  edition's files and its receive stamp to `cache_dir`, outside the catalog.
- An edition without `manifest.json` is unsealed: a product's working copy.
  A product has at most one.
- `search/` is an edition's own files: the manifest lists everything in it.
  The seal rebuilds the index before it writes the manifest, and nothing
  writes to `search/` afterwards.

### 1.1 Embargo on local disk

On local disk, statcatlib's API enforces embargo, and file permissions do
not. `Catalog` and its views read live editions only, through
`LiveInventory`. Any process or person that can read the catalog directory
can read an embargoed edition's files, and so can its backups and copies.
Storage identity enforces embargo from Phase 4, when catalogs move to object
storage. Each Inventory version records its actor as `<os user>@<hostname>`,
taken from the process and unauthenticated.

### 1.2 The Inventory root's `statcat` section

An Inventory root's `extensions` MAY hold a `statcat` section, the catalog's
description. `CatalogAdmin.describe` writes it in a version whose
`event.kind` is `describe`, a kind sdmxlib 0.81 added; an older reader skips
that root and stays on the version before it. Every later version carries
the section unchanged. Its fields:

| Field | Value |
| --- | --- |
| `format_version` | `"3"` |
| `description` | The catalog-level fields of a `CatalogDescription`: `identifier`, `title`, `description`, `publisher` (`name`, `id`, `type`, `homepage`), `homepage`, `language`, `licence` (`id`, `label`, `uri`), `theme_taxonomy` (each `uri`, `label`), `issued` and `modified`. Multilingual text is an object of language tag to text, and an instant an ISO 8601 string. |

A reader MUST ignore a key it does not know, in the section and in
`description`, so a later field does not stop it. The description never holds
datasets, services or records: those come from the live editions.
`Catalog.description` reads the section of the version the catalog holds.

### 1.3 Supported locations

A catalog lives on a local disk with hard links, such as APFS, ext4 or NTFS.
`Catalog.create` refuses a filesystem without hard links, such as exFAT,
FAT32 and some SMB mounts, with `IncompatibleCatalogError`. statcatlib does
not support catalogs in synced folders (Dropbox, OneDrive, iCloud Drive,
Google Drive) or on network shares: a sync client can deliver a root without
its leaves, or two versions of one file, and the exclusive create relies on
a local filesystem's link semantics.

### 1.4 Legacy layouts

`Catalog.open` and `Catalog.create` refuse a directory that holds a
root-level `meta.duckdb` (a statcatlib 0.11 catalog), a
`bundle.manifest.json` (an sdmxlib 0.72 bundle), or files and no
`catalog.json`, with `IncompatibleCatalogError`, and write nothing first.
Names starting with `.` are not counted. [Upgrading](upgrading.md) gives the
re-ingest steps.

### 1.5 The `pending/` registrations

A catalog in object storage also holds `pending/<product_id>/<edition_id>.json`, one
document per uploaded edition that waits for Phase 5's fold. [`statcat
upload`](api/blob.md) creates it after every listed file is verified, and nothing else
writes it. [`statcat gc`](api/gc.md) deletes it once the Inventory has recorded the
edition for longer than the GC window, once its `edition_id` time is older than the
tombstone window, or on an operator's `--discard`. A local catalog has none.

The document is UTF-8 compact JSON with two keys in this order:

```json
{"edition_id":"019a2b00-0000-7000-8000-000000000001","release_at":"2026-11-05T13:30:00.000Z"}
```

`release_at` is `null` for no scheduled release. Times are UTC, `Z`, in milliseconds,
as a root's `created_at` is. Equal input gives equal bytes, so a second upload of the
same edition leaves the same document. This form is a proposal to the specification
(spec question S14), and Phase 5's fold reads it.

## 2. The catalog schema

`statcat` is a star schema keyed by `dataset.identifier`, created by
`statcatlib.store.schema.create_schema` in a product's first edition and
copied into each later edition with `metadata.duckdb`. It holds `dataset`,
`agent`, `contact`, `frequency`, `licence`, `standard`, `concept_scheme`,
`concept`, `concept_relation`, `concept_scheme_top_concept`,
`dataset_theme`, `dataset_subject`, `dataset_keyword`, `dataset_spatial`,
`dataset_standard`, `distribution`, `data_service`, `service_dataset`,
`quality_annotation`, `provenance`, `variable`, `feature_type`, `author`,
`dimension_name`, `catalog_theme_taxonomy`, `lineage`, `language_alternate`
and `_meta`. Multilingual text is a `MAP(VARCHAR, VARCHAR)` from language tag
to string.

- `statcat._meta` records `schema_version`, `"2.2"` for this page, written
  once when the schema is created. A reader MUST refuse an edition that
  records another version, or none, before running any DDL on it:
  `create_schema` adds missing tables and leaves existing ones alone, so an
  older store would otherwise fail later, on the first query that reads a
  column added since. `Product.view` and `Product.working_copy` raise
  `IncompatibleCatalogError`.
- `statcat._meta` also records `product_kind`, `"data"` or `"structure"`,
  which `Product.working_copy` writes on each new edition and reads on a
  resume, so a resumed working copy keeps the kind it was started with. A
  new edition also drops the `partial_of` and `fill_in_progress` keys the
  previous edition's `metadata.duckdb` carries. Readers take the kind from
  the manifest's `statcat` section (§3); the `_meta` keys are the writer's.
- `statcat._meta.partial_of` is a JSON object from a codelist URN to the
  fingerprint of the full codelist `WorkingCopy.fill_dataflows` cut it from,
  merged over every fill of the edition, the later fill's value winning. The
  seal passes the entries whose URN `sdmx.artefact` holds as a partial to
  `sdmxlib.edition.seal` as `partial_of`. The key stays in the sealed
  `metadata.duckdb` and can name a URN the manifest lists as full; a reader
  takes `partial_of` from the manifest's artefact entries.
- `statcat._meta.fill_in_progress` holds the time a `fill_dataflows` call
  started, and that call deletes it in the transaction that writes
  `partial_of`. A working copy holding it refuses its seal, since its map
  may miss a cut.
- `statcat.language_alternate` records the language tags a source declares as
  alternates of a primary tag, such as `en-t-fr` for `en`, with the source,
  each tag lowercase as `sdmxlib.normalize_tag` spells it. Each alternate has
  one primary, and no tag is both.
- A dataset whose `sdmx_dataflow_urn` is set MUST name a dataflow the `sdmx`
  schema holds. That column is the only link from the catalog into sdmxlib's
  tables; statcatlib copies no structure.

### The `sdmx` schema check

sdmxlib refuses an edition on another store schema with `StoreSchemaError`:
`LocalRegistry.edition` reads the manifest's `schema_version` before it opens
`metadata.duckdb`, which is `Product.view`'s path, and `LocalRegistry.build`
checks the copied store, which is `Product.working_copy`'s. An edition on
another store schema is rebuilt from its sources with a matching release.

### Names inside one engine

A `Catalog` attaches every edition it views into one DuckDB engine, under
sdmxlib's prefix `<product_id>__<edition_id>`. Each view's cursor runs
`USE "<prefix>__metadata".statcat`, so unqualified names and two-part names
(`statcat.dataset`, `sdmx.code`) resolve inside that view's edition, and
every `information_schema` query filters by `current_database()`.

## 3. The `statcat` section

The manifest's `extensions` holds statcatlib's section under the key
`"statcat"`. `WorkingCopy.seal` computes it with
`statcatlib.bundle.catalog_section` after it rebuilds the search index and
passes it to `sdmxlib.edition.seal`.

For the edition `statcatlib.testing.synthetic_catalog` builds from two
languages, the section is:

```json
{
  "format": 3,
  "kind": "data",
  "lifecycle": "revisable",
  "statcatlib_version": "0.17.0",
  "datasets": 1,
  "search": {
    "index_version": 3,
    "embedding_model": null,
    "languages": {
      "en": {"rows": 39, "stemmer": "English"},
      "fr": {"rows": 39, "stemmer": "French"}
    },
    "annotations": {
      "types": ["skos:altLabel", "statcat:searchContext", "statcat:searchExclude"],
      "fingerprint": "f07cc818562f78e5b8173d3223260db8"
    }
  }
}
```

### `StatcatSection`

| field | presence | meaning |
| --- | --- | --- |
| `format` | required | The section format: `3` for this page. |
| `kind` | required | The product's kind: `"data"`, or `"structure"` for a structure product, which holds full artefacts only (§3.2). |
| `lifecycle` | optional | The product's lifecycle: `"final"` or `"revisable"`, the one the seal compacted the edition by. statcatlib 0.17 and later write it. |
| `statcatlib_version` | required | The version of statcatlib that wrote the section. |
| `datasets` | required | How many rows `statcat.dataset` holds. |
| `search` | required | The search index: a `SearchSection`, or `null` when the edition has no `search/`. |

### `SearchSection`

| field | presence | meaning |
| --- | --- | --- |
| `index_version` | required | The `version` of `search/index.json` (§4.1). |
| `embedding_model` | required | The model id that produced the vectors, or `null` for a BM25-only index. |
| `languages` | required | One `SearchLanguage` per language table, keyed by `sdmxlib.tag_alias`; empty for an edition with no rows to index. |
| `annotations` | required | The annotations the index was built from: an `IndexAnnotations` (§5). |

### `SearchLanguage`

| field | presence | meaning |
| --- | --- | --- |
| `rows` | required | How many rows that language's table holds. |
| `stemmer` | required | The Snowball stemmer it was indexed with, such as `"French"`, or `null` when it is indexed unstemmed (§4.2). |

### 3.1 Versions

Format 2 added `search.annotations`. Format 3 added `kind`. A reader that
checks the section (`check_catalog`) MUST refuse a format other than the one
it reads, and statcatlib reads exactly `3`. A reader that only opens the
catalog MAY ignore the section; `Product.view` does not read it.

statcatlib 0.17 added `lifecycle` to format 3 as an optional key, so the
format stays `3` and an edition sealed earlier has none. A reader that checks
the section MUST refuse a `lifecycle` other than `"final"` or `"revisable"`.
The release job's fold (sharded catalog specification §11.2 step 2) records an
uploaded edition with a lifecycle:

| The Inventory records the product | Section `lifecycle` | The fold records |
| --- | --- | --- |
| yes | absent | the leaf's |
| yes | equal to the leaf's | the leaf's |
| yes | different | nothing: the registration is refused, rule `lifecycle`, naming both |
| no | `"final"` or `"revisable"` | the section's |
| no | absent | nothing: the registration is refused, rule `lifecycle`; seal the edition with statcatlib 0.17 or later |

A product keeps the lifecycle it was first recorded with, so the third row is
the only guard against a relabel. `statcatlib.store.working_copy.fold_lifecycle`
applies the table.

A `structure` edition holds full SDMX structures and no observations: a
reader MUST refuse one whose manifest binds a dataflow to observations, or
whose store holds a dataflow, a binding or a partial. statcatlib 0.12 seals
`data` editions only; statcatlib 0.13 seals both.

### 3.2 Structure products and the publish check

A structure product (`Product.working_copy(kind="structure")`) holds one
agency's full codelists, concept schemes, category schemes and hierarchies,
copied by selection with `WorkingCopy.fill_structures`. Its leaf records
`kind: "structure"`, and so does its section. A product keeps the kind it
was first recorded with.

A structure edition's references may name artefacts another structure
product holds (sharded catalog specification §6.5 item 9). Its seal
therefore runs `check_edition(at="publish", structures=[...],
extensions={"statcat": check_catalog})` after sdmxlib's seal and before the
Inventory commit, given the live and embargoed editions of the catalog's
other structure products. `Catalog.seal` gives each structure edition of a
batch the batch's other structure editions in place of their products'
recorded ones. A finding leaves the edition sealed and unrecorded and raises
`EditionCheckError`.

### 3.3 The cross-product rules

sdmxlib's Inventory rules read one product's leaf. statcatlib's Inventory
writer adds the rules that read several products: specification §6.5 items
4, 7 and 8 (`statcatlib.store.cross_product`). A writer MUST NOT write a
version that breaks one. Each rule applies to an **incoming** entry, one
that becomes `live` or `embargoed` in the version, and compares it with
editions of other products only, since a product's own embargoed edition
replaces its live one at release. A URN's **structure home** is the
structure product whose live or embargoed edition holds the URN as a full
artefact.

| item | applies to | refused when | `rule` |
| --- | --- | --- | --- |
| 7 | a structure edition entering `live` | another structure product's live edition holds one of its full artefacts | `structure-home` |
| 4 | a data edition's full artefact with a structure home | no live or embargoed edition of the home holds its fingerprint | `full-artefacts` |
| 4 | a data edition's partial that records a `partial_of` | no live or embargoed structure edition holds the URN full with that fingerprint, or none holds it at all | `full-artefacts` |
| 4 | a data edition entering `live`, full artefact with no structure home | another data product's edition, live once the version is written, holds another fingerprint | `full-artefacts` |
| 8 | a data edition entering `live`, full artefact with a home, or partial | only an embargoed home edition holds its fingerprint | `publish-order` |

The rules read an incoming edition's artefacts from its `manifest.json`,
after checking the file's SHA-256 against the Inventory's `manifest_sha256`
(specification §6.5 item 2), whether or not its leaf entry is filled. They
read every other edition's from its leaf entry's `artefacts` when the writer
filled it (§1), and from its manifest otherwise, so a seal on a catalog
recorded with statcatlib 0.17 reads one manifest, the incoming edition's.
On object storage the writer reads each manifest through the storage, and a
missing one raises `ManifestError`. A partial with no `partial_of` is
skipped. A refusal raises
`CrossProductRuleError`, naming the rule, the item, the URN and the editions
compared.

The seal checks the rules before sdmxlib's seal, against the head, with the
working copy's artefacts fingerprinted from its open store; `Catalog.seal`
checks each pass's working copies together, with the batch's sealed
structure editions. The Inventory commit checks them again on the version it
lands on, as do `CatalogAdmin.release` and `CatalogAdmin.rollback`.

A data edition is **stale** when it is `live` or `embargoed` and holds a URN
with a structure home, as a full artefact or as a partial, whose fingerprint
(a partial's `partial_of`) matches no live or embargoed edition of that home.
A structure edit, or a rollback of a structure product, makes the data
editions holding the old copy stale once the old structure edition leaves
`live` and `embargoed`. A stale live edition stays live and keeps serving its
own closure; a stale embargoed edition is refused at release by the item 4
rows. Stale is derived from the Inventory and the manifests and is never
recorded. `statcat verify-catalog` and `statcat doctor` report each stale
edition with the URN, the fingerprint it holds and the home's live edition,
and the fix: republish the product. `CatalogAdmin.prune` keeps a structure
edition that a live or embargoed data edition's `partial_of` cites
(specification §11.4).

## 4. The search index

### 4.1 `index.json`

`search/index.json` names the table that serves each language. It is the
single source of truth for what a query reads: a reader MUST read the tables
it names and no others.

#### `IndexManifest`

| field | presence | meaning |
| --- | --- | --- |
| `version` | required | The manifest version: `3` for this page. |
| `written_by` | required | The version of statcatlib that wrote it. |
| `edition_id` | required | The edition whose tables it names. |
| `languages` | required | One `LanguageLayout` per language, keyed by `sdmxlib.tag_alias` (`fr-CA` is `fr_ca`). |
| `annotations` | optional | The annotations the index was built from: an `IndexAnnotations` (§5). |

#### `LanguageLayout`

`EditionView.index_layout` returns these entries as they are stored.

| field | presence | meaning |
| --- | --- | --- |
| `table` | required | The LanceDB table, `search_<alias>__<edition_id without hyphens>`. |
| `rows` | required | How many rows it holds. |
| `embedded` | required | How many of those rows carry a vector. |
| `ann` | required | The vector index type, such as `"IVF_FLAT"`, or `null` when none was built. |
| `stemmer` | required | The Snowball stemmer the table's full-text index uses, or `null` for unstemmed. |

A new edition carries its previous edition's `search/` (sdmxlib copies the
files its manifest lists at tier `lazy`). Its seal writes the tables named
by its own edition id, replaces `index.json` with one atomic rename, and then
drops the carried tables. An edition with no rows to index gets an
`index.json` with no languages. Tables no manifest names are orphans: a
process killed before the rename leaves them. Each rebuild drops every orphan
first, so a sealed edition holds only the tables `index.json` names, and the
`statcat` check reports any other (§6).

A writer bumps `version` only when an existing field changes meaning or
shape. Version 2 changed each `languages` entry; version 3 named tables by
edition id in place of a generation number. A reader MUST refuse a manifest
whose `version` is not the one it reads, or that records none, since it
would serve that index wrongly. statcatlib raises `IncompatibleCatalogError`
from `Product.view` and `Product.working_copy`, naming the version and the
statcatlib release in `written_by`. Indexes of version 2 or lower exist only
in layouts statcatlib 0.12 refuses (§1.4).

### 4.2 Tables and languages

Each language has its own table, because Tantivy takes one stemmer and one
stopword list per full-text index. Per-language tables also give each
language exact BM25 statistics. A query carries no language filter: the
table it reads is the filter.

A language with no Snowball stemmer in LanceDB, such as Chinese, Japanese,
Korean or Catalan, MUST be indexed unstemmed, with `stemmer` `null`. A
producer MUST NOT index one language with another language's stemmer.

Every table has these columns, whatever kind of entity a row describes.
Columns of other kinds hold an empty value.

#### Search table columns

| column | type | carried by |
| --- | --- | --- |
| `id` | string | every row |
| `entity_type` | string | every row: `"code"`, `"concept"`, `"reference_metadata"`, or a dataset kind such as `"aggregate"` |
| `lang` | string | every row: the table's language |
| `text` | string | every row: the full-text target |
| `dimension_id` | string | code rows |
| `dimension_label` | string | code rows |
| `code_id` | string | code rows |
| `code_label` | string | code rows |
| `code_urn` | string | code rows |
| `codelist_urn` | string | code rows |
| `parent_id` | string | code rows |
| `depth` | int64 | code rows |
| `concept_urn` | string | concept rows |
| `scheme_urn` | string | concept rows |
| `concept_id` | string | concept rows |
| `set_urn` | string | reference-metadata rows |
| `attribute_path` | string | reference-metadata rows |
| `section_title` | string | reference-metadata rows |
| `text_sha256` | string | embedded rows: the SHA-256, lowercase hex, of the JSON array `[model, document_prefix, text]`; `NULL` for a row not embedded, or when no model name is known |
| `embedding` | fixed-size list of float32 | every row, only when an embedding model is configured; `NULL` for a row not embedded |

A producer MAY reuse a stored vector for a text whose `text_sha256` matches,
in place of encoding it again; statcatlib's `build_index` does. A table
written before the column was added has none, and a reader MUST accept it.
Adding a column changes no existing field, so the manifest version stays 3.

### 4.3 `embeddings.json`

`build_index()` writes `search/embeddings.json` only when it embeds with a
model, so a BM25-only index has none. `Product.view` reads it to restore the
model's settings, so a caller does not repeat them.

#### `EmbeddingsSidecar`

| field | presence | meaning |
| --- | --- | --- |
| `version` | required | The sidecar version: `4` for this page. |
| `model` | required | The model id, such as `"intfloat/multilingual-e5-small"`. |
| `embedding_dim` | required | The vector width. |
| `document_prefix` | required | The text prepended to each document before encoding. |
| `query_prefix` | required | The text prepended to each query before encoding. |
| `normalize` | required | Whether vectors are normalised to unit length. |
| `include_codes` | required | Whether code rows were indexed. |
| `embed_codes` | required | Whether code rows were embedded. |
| `max_codes_per_codelist` | required | The size above which a codelist's codes were left out. |
| `built_at` | required | When the index was built, in UTC. |
| `built_by` | required | The statcatlib that built it. |
| `docs_indexed` | required | How many dataset rows were indexed. |
| `code_rows_indexed` | required | How many code rows were indexed. |
| `fts_only_codelist_urns` | required | The codelists indexed for full text only, never embedded. |
| `include_concepts` | required | Whether concept rows were indexed. |
| `embed_concepts` | required | Whether concept rows were embedded. |
| `concept_rows_indexed` | required | How many concept rows were indexed. |
| `concept_rows_with_embeddings` | required | How many concept rows carry a vector. |

Versions 1 to 3 hold fewer fields. A reader MUST read a sidecar of any
version and treat an absent field as unknown; statcatlib returns `None` for
it from `EditionView.embedding_metadata`.

## 5. Annotations and the index

`build_index()` reads three annotation types from the `sdmx` store, listed
in `SEARCH_ANNOTATION_TYPES`:

- `statcat:searchExclude` keeps a code out of the index;
- `statcat:searchContext` adds context text to a code's row;
- `skos:altLabel` adds synonyms.

Changing one of those annotations changes what the index should hold.
`index.json` and the `statcat` section therefore record which annotations
the index was built from.

#### `IndexAnnotations`

| field | presence | meaning |
| --- | --- | --- |
| `types` | required | The annotation types the build read, sorted. |
| `fingerprint` | required | A digest of every annotation of those types in the store at build time. |

The fingerprint is statcatlib's, computed by
`statcatlib.store.store.annotation_fingerprint`. It covers every annotation
of the listed types on any owner: each annotation's owner, position, id,
type, title and value, and each localised text's owner, position, language
and text. Rows are hashed with MD5 and summed, so the digest does not depend
on row order. A reader MUST treat it as opaque and compare it only for
equality with a fingerprint statcatlib computes over the same store.
An edition with no rows to index records it too, under an index with no languages.

When the store's fingerprint differs from the recorded one, an annotation
overlay has been folded into the store since the index was built (sdmxlib
§5.5):

- `Product.view` MUST warn, and MUST NOT fail or rebuild. A stale index still
  serves structure queries and observations, and a serving process never
  writes to an edition.
- `check_catalog` reports a finding (§6).
- A new edition clears both: its seal rebuilds the index.

An overlay a server applies at serve time never reaches the edition, so the
index never sees it. `overlay_affects_search(overlay, types)` returns the
entries that act on an annotation type the index reads, taking the types from
the section's `search.annotations`; a server SHOULD call it where overlays are
loaded, to log or refuse them.

## 6. Conformance

`check_catalog` is the `statcat` extension of sdmxlib's checker: run
`sdmxlib.edition.check_edition(path, at=..., extensions={"statcat":
check_catalog})` on an edition directory. Run it where an edition is built or
received, never on the path that serves it.

The `statcat` check stops at the first of its first five rows that fails,
and otherwise reports every finding:

| check | passes when | on failure |
| --- | --- | --- |
| schema | `metadata.duckdb` holds `statcat.dataset` | stops |
| schema version | `statcat._meta` records the version this build reads (§2) | stops |
| section | the manifest has a `statcat` section | stops |
| format | the section's `format` is `3` | stops |
| kind | the section's `kind` is `data` or `structure` | stops |
| structure bindings | a `structure` edition's manifest binds no dataflow | continues, one finding per binding |
| structure content | a `structure` edition's store holds no dataflow, binding or partial (`structure_content`) | continues, one finding per artefact |
| datasets | `statcat.dataset` holds the section's `datasets` | continues |
| dataflow links | every `sdmx_dataflow_urn` names a stored dataflow | continues, one finding per dataset |
| search present | `search/` holds an index exactly when the section declares one, and `index.json` exists | continues |
| index version | `index.json` records the version this build reads (§4.1) | skips the search rows below |
| declared version | the section's `index_version` is the version `index.json` records | continues |
| languages | the index has exactly the declared languages | continues, one finding per language |
| stemmers | each language's table uses the declared stemmer | continues |
| rows | each language's table holds the declared rows | continues |
| orphans | `search/` holds no `search_*` table that `index.json` does not name | continues, one finding per table |
| annotations | the store's fingerprint of the declared types is the declared one | continues |

Checking `search/` needs `statcatlib[search]`; without it the check reports
that it could not. Because the manifest lists every file with its SHA-256,
sdmxlib's `files` check also fails on any write made to an edition after its
seal.

`Product.view` checks the SHA-256 of the live edition's `manifest.json`
against the one the Inventory records, and refuses a mismatch with
`IncompatibleCatalogError`.

## 7. Producing an edition

1. `Catalog.create(path, exist_ok=True)`, then `product(id).working_copy()`.
   It first settles the product's seal journal, so an edition a crash left
   sealed and unrecorded is recorded before the new one picks its base.
2. Ingest: `ingest_sdmx` writes the `sdmx` schema and one catalog record per
   dataflow, `ingest_data` writes each dataflow's observations to the lake
   with its `DataflowBinding`, and `put` adds any other datasets.
3. Optionally run `build_index()` to search `wc.view()` before the seal.
4. `wc.seal(checks=[...])` runs the checks, refuses a base that is no longer
   live, rebuilds the index, computes the `statcat` section, seals with
   sdmxlib (journal entry, then manifest), and records the edition: `live`
   with embargo off, `embargoed` with it on. `CatalogAdmin.release` makes an
   embargoed edition live.
5. Run `check_edition` on the result.

A structure product's edition replaces step 2 with
`wc.fill_structures(source, ...)` on a working copy from
`working_copy(kind="structure")`, and step 4 refuses a dataflow, a binding or
a partial before the seal and runs the publish check (§3.2) before the
record. Step 4 checks the cross-product rules (§3.3) before sdmxlib's seal
and again at the record.

`statcatlib.testing.synthetic_catalog(path, shape, seed=...)` runs these
steps over `sdmxlib.testing.synthetic_edition`, building a sealed statcat
edition of any shape for tests and benchmarks.

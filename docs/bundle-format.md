# The statcat bundle format

A statcat bundle is an [sdmxlib bundle](https://statcat-suite.github.io/sdmxlib/bundle/)
that also holds a catalog and its search index. sdmxlib's page specifies the
directory, the manifest, its token and checksums, overlays, and sdmxlib's
checks. This page specifies what statcatlib adds on top: the `statcat` schema
in `meta.duckdb`, the `search/` directory, the `statcat` extension section of
the manifest, and the checks that hold the three to each other.

It describes **statcat section format 2**, **search index manifest version 2**
and **embeddings sidecar version 4**, on sdmxlib's manifest format 1.1.
statcatlib's code is the reference implementation (`statcatlib.bundle`,
`statcatlib.store.lance`). `tests/test_bundle_spec.py` keeps the field tables
below equal to what statcatlib writes.

MUST, MUST NOT and MAY carry their RFC 2119 meanings. A *producer* writes a
bundle; a *reader* opens one. A requirement sdmxlib's page already makes is
not repeated here.

## 1. Layout

```text
<bundle>/
  meta.duckdb            sdmxlib's sdmx.* schema, plus the statcat.* catalog schema (§2)
  lake.ducklake          the DuckLake catalog for observations (sdmxlib)
  parquet/               DuckLake's data files (sdmxlib)
  search/                the search index (§4); absent when no index is built
    index.json           names the live table for each language
    search_<lang>__<generation>.lance   one LanceDB table per language
    embeddings.json      the embedding model's settings; only with vectors
  bundle.manifest.json   sdmxlib's manifest, with a statcat section (§3), written last
```

- `meta.duckdb` MUST hold the `statcat` schema next to sdmxlib's `sdmx`
  schema, in the same file, so SQL joins across the two.
- `search/` is a producer's own files in sdmxlib's sense: the manifest's
  `files` MUST list everything in it. `build_index()` MUST therefore run
  before the manifest is written, and nothing MAY write to `search/`
  afterwards.
- An index built before pinaxlib 5.23.0 (pinaxlib is statcatlib's former
  name) has one table named `search` for every language and no `index.json`. A reader MAY serve it, and statcatlib
  does, with a warning. A producer MUST NOT publish one (§3.1).

## 2. The catalog schema

`statcat` is a star schema keyed by `dataset.identifier`, created by
`statcatlib.store.schema.create_schema`. It holds `dataset`, `agent`,
`contact`, `frequency`, `licence`, `standard`, `concept_scheme`, `concept`,
`concept_relation`, `concept_scheme_top_concept`, `dataset_theme`,
`dataset_subject`, `dataset_keyword`, `dataset_spatial`, `dataset_standard`,
`distribution`, `data_service`, `service_dataset`, `quality_annotation`,
`provenance`, `variable`, `feature_type`, `author`, `dimension_name`,
`catalog_theme_taxonomy`, `lineage` and `_meta`. Multilingual text is a
`MAP(VARCHAR, VARCHAR)` from language tag to string.

- `statcat._meta` records `schema_version`, `"2.0"` for this page, written
  once when the schema is created. A reader MUST refuse a catalog that
  records another version, or none, before running any DDL on it:
  `create_schema` adds missing tables and leaves existing ones alone, so an
  older catalog would otherwise fail later, on the first query that reads a
  column added since. `Catalog.open()`, `from_connection()` and
  `publish_bundle` raise `IncompatibleCatalogError`.
- A dataset whose `sdmx_dataflow_urn` is set MUST name a dataflow the `sdmx`
  schema holds. That column is the only link from the catalog into sdmxlib's
  tables; statcatlib copies no structure.

### The `sdmx` schema check

sdmxlib migrates its own store by dropping the `sdmx` schema. In a statcat
bundle that schema is half the catalog, so a reader MUST NOT let a migration
run on one that holds artefacts. When `sdmx._meta`'s `schema_version` differs
from the reader's sdmxlib and `sdmx.artefact` holds rows, `Catalog.open()`
raises `IncompatibleCatalogError` before sdmxlib touches the connection. The
manifest's `schema_version` (sdmxlib §4) carries the same value and lets
sdmxlib refuse the bundle without opening `meta.duckdb`. Either way the
bundle is rebuilt from its sources with a matching release.

## 3. The `statcat` section

The manifest's `extensions` holds statcatlib's section under the key
`"statcat"`. `publish_bundle` writes it; a producer MUST NOT pass a `statcat`
section of its own, and `publish_bundle` raises `ValueError` if one is given.

For the bundle `statcatlib.testing.synthetic_catalog` builds from two
languages, the section is:

```json
{
  "format": 2,
  "statcatlib_version": "0.11.0",
  "datasets": 1,
  "search": {
    "index_version": 2,
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
| `format` | required | The section format: `2` for this page. |
| `statcatlib_version` | required | The version of statcatlib that wrote the section. |
| `datasets` | required | How many rows `statcat.dataset` holds. |
| `search` | required | The search index: a `SearchSection`, or `null` when `search/` holds no index. |

### `SearchSection`

| field | presence | meaning |
| --- | --- | --- |
| `index_version` | required | The `version` of `search/index.json` (§4.1). |
| `embedding_model` | required | The model id that produced the vectors, or `null` for a BM25-only index. |
| `languages` | required | One `SearchLanguage` per language table, keyed by language tag. |
| `annotations` | required | The annotations the index was built from: an `IndexAnnotations` (§5), or `null` for an index that predates recording them. |

### `SearchLanguage`

| field | presence | meaning |
| --- | --- | --- |
| `rows` | required | How many rows that language's table holds. |
| `stemmer` | required | The Snowball stemmer it was indexed with, such as `"French"`, or `null` when it is indexed unstemmed (§4.2). |

### 3.1 Versions

Format 2 added `search.annotations`. A reader that checks the section
(`check_catalog`) MUST refuse a format other than the one it reads, and
statcatlib reads exactly `2`: a bundle published by another format is
republished with `publish_bundle`. A reader that only opens the catalog MAY
ignore the section; `Catalog.open()` does not read the manifest.

`publish_bundle` raises `ValueError` when `search/` holds an index with no
`index.json`, so a published section always has a layout to record.

## 4. The search index

### 4.1 `index.json`

`search/index.json` names the table that serves each language. It is the
single source of truth for what a query reads: a reader MUST read the
tables it names and no others.

#### `IndexManifest`

| field | presence | meaning |
| --- | --- | --- |
| `version` | required | The manifest version: `2` for this page. |
| `written_by` | required | The version of statcatlib that wrote it. |
| `generation` | required | The build number, counted up by each rebuild. Table names carry it. |
| `languages` | required | One `LanguageLayout` per language, keyed by language tag. |
| `annotations` | optional | The annotations the index was built from: an `IndexAnnotations` (§5). |

#### `LanguageLayout`

`Catalog.index_layout` returns these entries as they are stored.

| field | presence | meaning |
| --- | --- | --- |
| `table` | required | The LanceDB table, `search_<lang>__<generation>` with the generation zero-padded to six digits. |
| `rows` | required | How many rows it holds. |
| `embedded` | required | How many of those rows carry a vector. |
| `ann` | required | The vector index type, such as `"IVF_FLAT"`, or `null` when none was built. |
| `stemmer` | required | The Snowball stemmer the table's full-text index uses, or `null` for unstemmed. |

A rebuild writes a whole new generation of tables, then replaces
`index.json` with one atomic rename. A reader that opens the bundle mid-build
therefore reads the previous generation whole. Tables no manifest names are
orphans: a process killed before the rename leaves them, and so does a
superseded table whose drop failed. `build_index()` drops every orphan before
it builds, so a bundle published after a build holds only the tables
`index.json` names. A producer MUST NOT publish orphans; the `statcat` check
reports each one (§6).

Version 1 stored each language's table name as a bare string where version 2
stores a `LanguageLayout`, and statcatlib still reads both. A reader reads
the fields it knows and ignores the rest.

A writer bumps `version` only when an existing field changes meaning or
shape, as version 2 did to each `languages` entry. A new field leaves
`version` as it is, because readers ignore fields they do not know. A reader
therefore MUST refuse a manifest whose `version` is above the newest it
reads, or that records none, since it would serve that index wrongly.
statcatlib's `Catalog.open()` raises `IncompatibleCatalogError` before
running any DDL, naming the version and the statcatlib release in
`written_by`. This is stricter than a stale index, which statcatlib serves
with a warning (§5), because a stale index is in a format the reader reads.
A version bump therefore requires every reader to upgrade before it serves
an index built at the new version.

**A statcatlib older than pinaxlib
5.23.0 finds no table named `search` in a newer index and returns zero hits
without an error**, so a deployment MUST upgrade the library before it serves
a bundle built with a newer one.

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
| `embedding` | fixed-size list of float32 | every row, only when an embedding model is configured; `NULL` for a row not embedded |

### 4.3 `embeddings.json`

`build_index()` writes `search/embeddings.json` only when it embeds with a
model, so a BM25-only index has none. `Catalog.open()` reads it to restore
the model's settings, so a caller does not repeat them.

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
it from `Catalog.embedding_metadata`.

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

When the store's fingerprint differs from the recorded one, an annotation
overlay has been folded into the store since the index was built (sdmxlib
§5.5):

- `Catalog.open()` MUST warn, and MUST NOT fail or rebuild. A stale index
  still serves structure queries and observations, and a serving process
  never writes to a bundle.
- `check_bundle` reports a finding (§6).
- `build_index()` followed by `publish_bundle` clears both.

An overlay a server applies at serve time never reaches the bundle, so the
index never sees it. `overlay_affects_search(overlay)` returns the entries
that act on an annotation type the index reads; a server SHOULD call it
where overlays are loaded, to log or refuse them.

## 6. Conformance

`statcatlib.bundle.check_bundle(path)` runs sdmxlib's checks and then the
`statcat` check, `check_catalog`. `statcat bundle check PATH` runs it from a
shell, prints each finding, and exits 1 if there is any; `--no-references`
skips sdmxlib's costly `references` check. Run it where a bundle is built or
received, never on the path that serves it.

The `statcat` check stops at the first of its first four rows that fails,
and otherwise reports every finding:

| check | passes when | on failure |
| --- | --- | --- |
| schema | `meta.duckdb` holds `statcat.dataset` | stops |
| schema version | `statcat._meta` records the version this build reads (§2) | stops |
| section | the manifest has a `statcat` section | stops |
| format | the section's `format` is `2` | stops |
| datasets | `statcat.dataset` holds the section's `datasets` | continues |
| dataflow links | every `sdmx_dataflow_urn` names a stored dataflow | continues, one finding per dataset |
| search present | `search/` holds an index exactly when the section declares one, and `index.json` exists | continues |
| index version | `index.json` records a version this build reads (§4.1) | skips the search rows below |
| declared version | the section's `index_version` is the version `index.json` records | continues |
| languages | the index has exactly the declared languages | continues, one finding per language |
| stemmers | each language's table uses the declared stemmer | continues |
| rows | each language's table holds the declared rows | continues |
| orphans | `search/` holds no `search_*` table that `index.json` does not name | continues, one finding per table |
| annotations | the store's fingerprint of the declared types is the declared one | continues |

Checking `search/` needs `statcatlib[search]`; without it the check reports
that it could not. Because the manifest lists every file with its SHA-256,
sdmxlib's `files` check also fails on any write made after publishing: a
rebuilt index, or a data path rewritten by `statcat bundle rebind` or by
`open_bundle`'s default `Auto()` policy repointing a moved bundle (both
change `lake.ducklake`). A server MUST open a published bundle with a
policy that never writes, such as `Embedded()`
([Bundles](api/bundles.md)).

## 7. Producing a bundle

1. Open a folder catalog and ingest: `ingest_sdmx` writes the `sdmx` schema
   and one catalog record per dataflow, `ingest_data` writes each dataflow's
   observations to the lake with its `DataflowBinding`, and `put` adds any
   other datasets.
2. Run `build_index()`. It also drops any table an earlier, interrupted
   build left behind.
3. Call `publish_bundle(path, bundle_id=..., producer=...)` last. It is
   sdmxlib's `write_manifest` with the `statcat` section added, and takes
   the same `product`, `extensions` and `provenance`.
4. Run `check_bundle` on the result.

Publishing again mints a new token. `statcatlib.testing.synthetic_catalog(path,
shape, seed=...)` runs these steps over `sdmxlib.testing.synthetic_bundle`,
building a published statcat bundle of any shape for tests and benchmarks.

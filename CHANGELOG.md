# Changelog

All notable changes to this project will be documented in this file.

## [0.11.0] - 2026-10-23

Fixes `ingest_data`, whose observations could not be read back. Up to 0.10.1
it stored the pivoted frame, one column per code of the measure dimension,
while the `DataflowBinding` it registered describes the long layout, a column
per dimension and measure. Reading the dataflow through
`store.registry.obs(flow)` raised a DuckDB `BinderException`.

From 0.11.0 `ingest_data` stores the long layout, one row per observation, and
the binding reads it:

```python
obs = store.registry.obs(sl.Dataflow.parse_urn(ds.sdmx_dataflow_urn))
obs.filter(obs.col("GEO") == "CA").frame()   # long
obs.pivot(on=ds.measure_dim)                  # wide
```

`measure_dim` no longer changes what is stored. It must name one of the DSD's
dimensions, and it is recorded as `AggregateDataset.measure_dim`, the axis to
pass to `pivot(on=...)`. `ingest_data` now raises `ValueError` when the
dataflow's DSD is not stored, where it used to write a table with no binding.

**Upgrading:** re-run `ingest_data` for each dataflow in an existing catalog.
That rewrites its `lake.<agency>.df_<id>_<version>` table in the long layout,
which the binding can read. SQL that read the wide columns directly must pivot
instead. The sdmxlib store schema is unchanged, so nothing else needs a
rebuild.

The docs described a Parquet file attached as a `Distribution` and read with
`pl.scan_parquet`, and a `cat.observations()` method that does not exist. They
now describe the DuckLake table and `store.registry.obs(flow)`. A docs pass
also corrected the catalog folder layout (`meta.duckdb`, `lake.ducklake`,
`parquet/`), the `Hit` objects `search()` returns, and `QueryBuilder`
immutability.

### Breaking Changes

- **sources**: `ingest_data` stores the long layout its binding reads (GH-29)

### Documentation

- Rewrite user docs and CLAUDE.md to the writing-style rules; gate prose (GH-28)

### Miscellaneous

- **types**: Pyrefly is the only type checker; cap the checkers (GH-7)
- **docs**: Port the docstring gate tool from sdmxlib; lint tools/ (GH-9)

## [0.10.1] - 2026-10-23

Fixes folder catalogs created from a relative path, such as
`Catalog("my_catalog")`. Up to 0.10.0 such a catalog recorded its parquet
location relative to the working directory. From any other directory it
opened and answered counts and facets, then failed on the first query that
read observations. `Catalog(path)` also refused a catalog opened by a
different spelling of its path, relative versus absolute.

From 0.10.1 a new catalog records an absolute path, and `Catalog(path)` opens
any catalog by any spelling of its path. An older relative one reads
correctly from any directory. **Open it read-write once to repair it:** that
rewrites the recorded path to absolute, which readers that use it exactly as
written, such as `data_path=Embedded()`, need. A read-only open leaves it as
it is. Catalogs created from an absolute path need nothing. The store schema
is unchanged (2.48), so 0.10.0 catalogs open without a rebuild.

### Bug Fixes

- **store**: Open a folder catalog by any spelling of its path (GH-23)

### Miscellaneous

- Align the release tooling and CLAUDE.md with the suite baseline (GH-10, GH-11, GH-17)

### Testing

- Strict pytest, warnings as errors, and coverage with a floor (GH-8)

## [0.10.0] — 2026-10-23

Moves to sdmxlib 0.72, which owns the bundle manifest
(`bundle.manifest.json`, sdmxlib GH-56). sdmxlib 0.72 keeps store schema
2.48, so catalogs built by 0.9.x open without a rebuild.

**Publish a bundle with `statcatlib.bundle.publish_bundle`, after
`build_index()`.** It writes sdmxlib's manifest with a `statcat` section,
and `check_bundle` / `statcat bundle check` checks the bundle against it.
The manifest lists every file with its sha256, so anything written
afterwards fails the check, including `statcat bundle rebind`: check before
rebinding. **A builder that writes its own `bundle.manifest.json` now has
its bundle refused by sdmxlib's `LocalRegistry.bundle`, and so by
sdmxlib-rest.** Republish it with `publish_bundle`. statcatlib's own
`open_bundle` does not read the manifest.

**Run `build_index()` once on an existing catalog before publishing it.**
The index now records a fingerprint of the annotations it was built from
(`statcat:searchExclude`, `statcat:searchContext`, `skos:altLabel`).
`check_bundle` reports an index built without one. When an overlay folded
into the store changes those annotations, `Catalog.open()` warns, and keeps
warning until the index is rebuilt. Nothing rebuilds it automatically.

Searching a catalog no longer rewrites its search index: the first query in
each process used to rebuild it, so serving workers re-indexed on startup.

### Bug Fixes

- Ship the actual Apache-2.0 licence text
- **search:** Reuse a built FTS index instead of rebuilding it on the first query

### Documentation

- **bundle:** Publishing and checking a bundle

### Features

- **search:** Detect a stale index when overlays change its annotations (GH-3)
- **testing:** `synthetic_catalog` builds published statcat bundles of any shape (GH-3)
- **bundle:** `publish_bundle`, `check_bundle` and `statcat bundle check` (GH-3)

### Dependencies

- Require sdmxlib 0.72.0 for its bundle manifest and checker

## [0.9.1] — 2026-10-22

`ingest_data()` can fetch observations from Eurostat, which 0.9.0
listed as a known issue. sdmxlib 0.70.1 keeps store schema 2.48, so
catalogs built by 0.9.0 open without a rebuild.

### Bug Fixes

- **deps:** Require sdmxlib 0.70.1 so ingest_data fetches from Eurostat

## [0.9.0] — 2026-10-22

Moves to sdmxlib 0.70, which needs **Python 3.14** and **DuckDB 2.0**.

**Catalogs and bundles built by 0.8.0 must be rebuilt.** sdmxlib's store
schema went from 2.19 to 2.48, and sdmxlib migrates by dropping its `sdmx`
schema. Opened read-write, a 0.8.0 catalog would have lost every artefact
its datasets link to, with no error. `Catalog.open()` now refuses such a
catalog with `IncompatibleCatalogError` and leaves it untouched. A catalog
written under DuckDB 2 cannot be opened by 0.8.0, so deploy this release
before building any bundle with it.

`ingest_data()` cannot fetch observations from Eurostat yet: sdmxlib's
`RestRegistry.obs()` sends an `Accept` header Eurostat refuses (sdmxlib
GH-50). Structure ingest from Eurostat is unaffected.

### Bug Fixes

- **sources:** Ingest_data reads observations through reg.obs()

### Documentation

- README and CLAUDE.md for sdmxlib 0.70

### Features

- **store:** Refuse to open a catalog whose sdmx schema sdmxlib would drop (**breaking**)

### chore

- **deps:** Require sdmxlib 0.70, DuckDB 2 and Python 3.14 (**breaking**)

## [0.8.0] — 2026-10-22

First release as **statcatlib**, formerly `pinaxlib` (imported as `pinax`),
renamed to stop colliding with the Django project of that name. The version
line restarts at 0.x on purpose: the old 1.0–7.x numbering claimed an API
stability the project never had. Until 1.0, a minor release may break the
API. Every entry below this one is a pinaxlib release, and its `#N`
references are issues in the archived GitLab project.

Catalogs and bundles built by pinaxlib must be rebuilt, and code updated:

| pinaxlib                   | statcatlib                   |
| -------------------------- | ---------------------------- |
| `import pinax as pk`       | `import statcatlib as sc`    |
| `pinax` CLI                | `statcat` CLI                |
| DuckDB schema `pinax`      | DuckDB schema `statcat`      |
| `pinax:searchContext`      | `statcat:searchContext`      |
| `pinax:searchExclude`      | `statcat:searchExclude`      |
| `PINAX_EMBED_BATCH_SIZE`   | `STATCAT_EMBED_BATCH_SIZE`   |

### Bug Fixes

- **deps:** Cap duckdb below 2

### Documentation

- Bring the reference up to the current API

### Refactoring

- Rename pinax to statcatlib, and make GitHub its home (**breaking**)

## [7.2.0] — 2026-10-19

### Features

- **search:** Pinax:searchExclude keeps annotated codes out of the code corpus

## [7.1.1] — 2026-10-08

### Bug Fixes

- **deps:** Support sdmxlib 0.67
- **bundle:** Honour an explicit data_path over the lake stamp (#126)

## [7.1.0] — 2026-10-02

### Bug Fixes

- **search:** Accept deliberate NULL embeddings on lancedb >= 0.39 (#125)

## [7.0.0] — 2026-08-27

### Documentation

- **bundle:** Align tables and lift callouts into admonitions

### Features

- **bundle:** Layout-aware data_path policies for open_bundle (#120) (**breaking**)

## [6.1.0] — 2026-08-27

### Bug Fixes

- **search:** Warn when an asymmetric encoder's prefixes are missing (#81)

### Features

- **bundle:** Trust the lake's embedded data_path (#98)
- **ci:** Type-check tests/ with both checkers (#116)

## [6.0.0] — 2026-08-26

### Bug Fixes

- **tests:** Correct the tmp_path annotation, drop 28 suppressions
- **model:** Extras is string-to-string, and says so (#114) (**breaking**)
- **model:** Kind is a ClassVar on each variant, not a field on the base (**breaking**)
- **store:** Guards that return, a tagged edge, and 32 dead conversions (#107)
- **scopes:** An SDMX edge reports an unsupported reduction legibly (#109)

### Documentation

- Name the real gate, and why a pipeline must never block a merge
- Docstrings that agree with their signatures (#108)
- Google docstring sections, and the args they were hiding (#105)
- Plain backticks, not Sphinx roles (#105)
- Bring the prose to sdmxlib 0.61

### Features

- **query:** Descent resolves through q.hierarchy_applicability (**breaking**)

### Performance

- **query:** Join the descendant set, do not scan it (**breaking**)

### Refactoring

- **store:** One checked spec per entity, not five parallel tables
- **store:** One source for the dataset column order (**breaking**)
- Remove the StatCan coupling, and the dead API names it hid (**breaking**)
- **store:** Name the search union, check the reconstructor table (#109)
- **lance:** Name the two libraries and the manifest (#109)
- **store:** The encoder contract, stated so a real encoder meets it (#109)
- **store:** The search corpus has a shape, so declare it (#109)
- **store:** A Protocol for the encoder, a TypedDict for the sidecar (#109)
- **query:** Recover the types Any was erasing (#109)
- Type the two shells properly (#109)
- Object over Any in the small files (#109)
- **store:** Row values are object, not Any (**breaking**)
- The compile context is attrs, like everything around it
- **store:** Sl.Registry is sl.LocalRegistry
- **scopes:** Meet ItemList's SupportsId bound
- **query:** Pinax owns its compile context

## [5.24.0] — 2026-08-18

### Bug Fixes

- **search:** Build IVF_FLAT so vector search stops quantising away the answer

### Documentation

- Document per-language search indexes

### Features

- **search:** Warn once when a search index is out of date

### Refactoring

- **search:** Gate the ANN build on vectors, not rows

## [5.23.0] — 2026-08-14

### Features

- **search:** Warn when serving a pre-#101 search index
- **search:** Commit index rebuilds atomically via a manifest
- **search:** Give each language its own FTS index and table

### Refactoring

- **search:** Declare the Lance table schema explicitly

## [5.22.1] — 2026-08-14

### Bug Fixes

- **search:** Select reference-metadata blend text per language

### Documentation

- Refresh stale API references in CLAUDE.md

## [5.22.0] — 2026-08-14

### Features

- **search:** Fold language-tagged altLabels per language

## [5.21.0] — 2026-07-07

### Features

- **search:** Down-convert XHTML reference-metadata to markdown at index time

## [5.20.0] — 2026-07-07

### Features

- **search:** Reference metadata as a first-class entity kind + query API

## [5.19.0] — 2026-06-30

### Features

- **search:** Punctuation-insensitive name/text matching

## [5.18.0] — 2026-06-30

### Features

- **search:** Accent-insensitive name/text matching across FTS and substring filters

## [5.17.0] — 2026-06-29

### Features

- **cli:** Expose reference-metadata blends via --reference-metadata-blend
- **store:** Fold pinax:searchContext annotations into per-language code corpus

## [5.16.1] — 2026-06-11

### Performance

- **deps:** Bump sdmxlib to >=0.35 for batched ingest writes

## [5.16.0] — 2026-06-10

### Features

- **cli:** Expose CodelistIndexPolicy via --codelist-policy flag

## [5.15.0] — 2026-06-10

### Features

- **store:** Typed search scopes + FTS-only codelist policy + concept indexing

## [5.14.0] — 2026-06-10

### Features

- **bundle:** Portable lake.ducklake via open-time data_path normalization

## [5.13.0] — 2026-06-09

### Features

- **query:** Migrate to sdmxlib 0.33 + add q.within (#93)

## [5.12.1] — 2026-06-03

### Bug Fixes

- **store:** Use per-thread DuckDB cursors for thread safety
- **search:** Build ANN index on embedding column

## [5.12.0] — 2026-06-02

### Bug Fixes

- **cli:** Drop resolve_path=True on build-index BUNDLE arg

### Features

- **store:** Read_only=True for multi-worker Catalog opens
- **store:** Catalog.data_path + azure_credentials kwargs

## [5.11.0] — 2026-05-12

### Features

- **store:** Code-search extensions for catalog-specific tuning

## [5.10.1] — 2026-05-11

### Bug Fixes

- **cli:** Force multi-command mode so `pinax build-index` parses correctly

## [5.10.0] — 2026-05-11

### Features

- **cli:** Add pinax build-index command and richer embeddings sidecar

## [5.9.0] — 2026-05-11

### Features

- **bundle:** DuckLake data plane via meta.duckdb + lake sidecar

## [5.8.1] — 2026-05-10

### Documentation

- **catalog:** Update for bundle architecture; add registry tests

## [5.8.0] — 2026-05-10

### Refactoring

- **catalog:** Bundle on-disk layout — bundle.duckdb + parquet/
- **catalog:** Compose sl.Registry as a property (Decision 7)
- **query:** Compile filters via sl.CompileContext
- **schema:** Rename _pinax schema → pinax (parallel with sdmx)
- **catalog:** Path-style construction, drop create/open/open_or_create
- Rename CatalogStore → Catalog, free pk.Catalog for the store

## [5.6.0] — 2026-04-28

### Bug Fixes

- **store:** Bump sdmxlib to v0.14.1 and fix spatial_labels FR query

### Features

- **store:** Public attach_embedding_model() for opened catalogs

## [5.5.0] — 2026-04-27

### Bug Fixes

- **search:** Preserve exact matches when fuzzy matching is enabled
- **search:** Improve search quality and API ergonomics

### Features

- **search:** Code-level BM25 search and opt-in code embeddings

## [5.4.0] — 2026-04-26

### Bug Fixes

- **store:** Persist creator field, uniform terminal methods, internal cleanup

### Features

- **search:** Add code-level search indexing

## [5.3.0] — 2026-04-24

### Documentation

- Document fuzzy search parameter
- Update all docs for v5.2.0 features

### Features

- **search:** Add fuzzy parameter to BM25 full-text search

## [5.2.0] — 2026-04-20

### Features

- **store:** Row typed projection — .select() returns Row not dict
- **scopes:** Batch_labels() on CodelistScope and CodelistsScope
- **query:** Lazy graph traversal — upstream/downstream/related return QueryBuilder
- **query:** NotLoadedError and UNLOADED sentinel for selective loading

### Refactoring

- **query:** Make .collect() the canonical terminal, .all() the alias

## [5.1.0] — 2026-04-20

### Features

- **query:** Add QueryBuilder.include() for selective relationship loading

## [5.0.0] — 2026-04-20

### Documentation

- Update all docs for SKOS, unified put(), and removed codes= param

### Features

- SKOS concept schemes as first-class artefacts (#72) (**breaking**)

## [4.0.0] — 2026-04-18

### Bug Fixes

- **search:** Use MatchQuery for proper BM25 AND/OR and default to OR

### Features

- DatasetScope API, batch hydration, empty IN() fix (#70, #71) (**breaking**)
- **search:** Configurable BM25 query strategy with AND default (#68)
- **store:** Ranked projections and semantic search scores (#65, #66)

## [3.5.0] — 2026-04-17

### Bug Fixes

- **store:** Call _init_lance() in _open_folder() so .open() enables search
- **store:** Catch ImportError from LanceIndex.__init__
- **store:** Make bulk_load() re-entrant to support nesting
- **store:** Use plain CHECKPOINT instead of FORCE CHECKPOINT
- **store:** Resolve bulk ingest deadlock, cursor leak, and per-row indexing

### Features

- **sources:** Expose prepare_sdmx() for single-put ingest with caller enrichment

### Performance

- **store:** Remove pointless LanceDB index rebuild from bulk_load() exit
- **store:** Pass embedding vectors to LanceDB without list conversion
- **store:** Bulk SQL for text collection, progress callback for build_index
- **store:** Batch INSERT for keywords and spatial bridge tables

## [3.4.0] — 2026-04-17

### Features

- **store:** Add public transaction() context manager

## [3.3.0] — 2026-04-17

### Bug Fixes

- **schema:** Rename catalog schema to _pinax to avoid DuckDB name collision

### Features

- **store:** Expose public store.cursor property with search_path set

## [3.2.0] — 2026-04-17

### Documentation

- Document .select(), .scalars(), .exists() and enriched scope labels

### Features

- **query:** Add .select(), .scalars() projection modifiers and .exists() terminal

## [3.1.0] — 2026-04-17

### Documentation

- Update documentation for v3.1 search & discovery features

### Features

- **search:** Enrich index with theme/dimension labels, add batch encoding (#50)
- **query:** Add MAP column filters — has_spatial, has_keyword, title_contains, description_contains (#51, #47)
- **query:** Support sort_by() on MAP columns like title (#46)
- **scopes:** Generalize enrich system to codelists, add CodelistsScope

## [3.0.0] — 2026-04-16

### Features

- V3 redesign — relational schema, MAP columns, LanceDB search (**breaking**)
- **statcan:** Add UOM label support and bulk code index insert

### Refactoring

- Remove StatCan connector, add bulk_load() and bulk bridge INSERTs

## [2.1.0] — 2026-04-15

### Features

- Add pk.urn helpers for building SDMX URN strings

## [2.0.0] — 2026-04-15

### Bug Fixes

- **store:** Add codelist_urn to dataset_codes table (#45)
- Silence git-cliff warnings for merge commits
- **store:** Key publisher facet and filter by agent ID instead of name
- **store:** Populate dataset_dimensions bridge table on write
- **store:** Persist French columns in contacts, licences, spatial, distributions
- **statcan:** Handle plain-string values in bilingual freq dict
- Handle plain string freq values in _parse_ndm_freq

### Documentation

- Add AI assistance disclaimer to README
- Add Pinakes etymology to README
- Add llms.txt for LLM context sharing
- **model:** Document ds.kind as the preferred type discrimination mechanism
- **statcan:** Add semantic and hybrid search tutorial
- **api:** Document build_index and fix vector extra install instructions

### Features

- Add lazy scope-based graph traversal API with pk.each() expressions (**breaking**)
- **store:** Add "spatial" and "type" to the facet vocabulary
- **store:** Multi-field sort and NULLS LAST in QueryBuilder.sort_by()
- **store:** Expose per-item relevance scores in SearchResult
- **store:** Add theme_labels() and spatial_labels() convenience methods
- **store:** Add QueryBuilder.from_ids() to seed queries from ID lists
- **store:** Add defer_fts() context manager to batch FTS rebuilds
- **statcan:** Make ndm_record_to_dataset public; batch publications with put_all
- **search:** Add statcan-embeddings just recipe and semantic search docs
- **search:** Hybrid semantic search via LanceDB (issue #27)

### Performance

- Use NDM API max page size of 500 (was defaulting to 10)

### Refactoring

- **store:** Make themes schema scheme-aware
- Rename package from cataloglib to pinax (**breaking**)
- **model:** Remove unit_of_measure from AggregateDataset
- **statcan:** Make NDM constants and utilities public

## [0.4.0] — 2026-04-13

### Bug Fixes

- **sources:** Correct NDM frequency code map; add StatCan catalog guides
- WDS POST endpoint, invalid PIDs, and NDM publication identifier
- **sources:** WDS getCubeMetadata is POST not GET
- **sources:** Wire resolved concept refs into DSD dimensions

### Documentation

- Comprehensive rewrite + archive stale design docs
- **claude:** Add PROV-O to standards list and glossary

### Features

- **sources:** StatCan CSV → wide-format Parquet data download
- **store:** Rework lineage to PROV-O spec; resolve (latest) in ingest_data
- **store:** Dataset lineage — record_lineage, upstream_of, downstream_of
- **query:** Cross-entity filters — publishes() and serves()
- **store:** Structural traversal — local_registry, code_map, codelist_for, get_structure, datasets_using_codelist, related_by_codelist
- **api:** Public API smoke tests — verify all cl.* exports and cl.query DSL
- **sources:** SDMX bridge — dataset_from_dataflow returns AggregateDataset + codes_dict
- **store:** Code index population via put(codes=...) — Path A (#19)
- **store:** Generalize query() to Agent, DataService, Distribution (#18)
- **query:** Composable ExistsFilter DSL — has_code, has_dimensions, distribution (#17)
- **store:** Per-variant CRUD tests and unit_of_measure_fr column (#13)
- **store:** Extract schema module with configurable languages (#12)
- **model:** Dataset variant hierarchy (#11)
- **sources:** Bilingual NDM metadata — fetch EN+FR endpoints and merge
- **scripts:** StatCan catalog bootstrap script and curated dataset YAML
- **sources:** WDSClient, NDMClient, and StatCan ingest functions
- **sources:** Add StatCan WDS Cube types and SDMX structure builders
- **sources:** Add generic CKAN connector

## [0.3.0] — 2026-04-12

### Documentation

- Fix three-layer diagram dark mode; add watchfiles for macOS hot-reload

### Features

- Catalog folder abstraction — CatalogStore.create/open/open_or_create

## [0.2.0] — 2026-04-12

### Documentation

- Document ingest_data, Distribution handoff, and Polars workflow
- Simplify SDMX ingest examples — resolve() fetches all descendants
- Fix pipe escaping in table, add dark mode toggle, GitLab icon

### Features

- Add ingest_data() — stream observation data to Parquet, link as Distribution

## [0.1.0] — 2026-04-12

### Bug Fixes

- Replace --block-network with correct integration/live test split

### Documentation

- Add MkDocs site with overview, tutorial, and mermaid diagrams

### Features

- Complete MVP — publisher mapping, FTS, persistent storage, live SDMX test
- Initial MVP — domain model, DuckDB catalog store, SDMX ingest



# Catalog, Products, Editions and the Inventory

* **Author:** Gabriel Gellner
* **Date:** 2026-10-28
* **Status:** RFC for team feedback. It comes from an EGM brainstorm: three
  Goldfish rounds plus a Fabric and an operations review, with 32 decisions
  settled by the owner. The next step is a formal design (`/egm-draft`). Round 3 used the six
  standard reviewers plus two personas: a lakehouse implementer building a
  CODR-scale platform on FMR and ADLS Gen2, and a .Stat Suite operator
  migrating an NSI installation. This brief is authoritative for the names.

## Overview

statcatlib must adopt sdmxlib's sharded bundle format (GH-70, plan Phase 2a).
The owner set three goals:

1. **Encapsulation.** Callers reach stores only through the API and never
   handle edition directories or files.
2. **One model from a laptop to .Stat.** The same API serves a user who pulls a
   few tables from an SDMX endpoint and a .Stat backend: about 7,000 CODR
   tables, tens to hundreds released each weekday at 08:30 under embargo,
   served by sdmxlib-rest pods behind a CDN, with rollback.
3. **Build on established systems.** statcatlib is pre-alpha, so sdmxlib and
   the other repos may be rebuilt.

The model:

- **A Catalog** is the `dcat:Catalog`: a publisher's holdings, as a directory
  on a laptop or a container on ADLS Gen2. It holds **Products**.
- **A Product** is the unit of publication, routing and lifecycle, one CODR
  table by default. It holds sealed **Editions** over a shared `data/` pool.
- **A WorkingCopy** is a product's single unsealed edition. It is writable,
  can be queried, and lives on local disk. `seal()` turns it into an Edition.
- **The Inventory** decides each product's live edition, on every Catalog.
  It is a chain of immutable, versioned documents, each a root plus one leaf
  per product.
- **The public side** (`Catalog`, `EditionView`) sees only live editions.
- **The operational side** is two separately constructed objects, both off by
  default:
  - `EmbargoReader` reads embargoed editions;
  - `CatalogAdmin` changes the Inventory.
- **Embargo** is enforced by storage identity for outsiders, and by the
  operational split for trusted processes.
- **sdmxlib comes first.** The rename and every core change land in one
  sdmxlib release, and statcatlib Phase 2a pins that release.

## Analysis

### 1. Glossary

| Term | Meaning |
|---|---|
| Catalog | The `dcat:Catalog`: a directory or container holding `catalog.json`, `CURRENT`, `inventory/` and `products/`. `CatalogDescription` is its DCAT record, stored in the Inventory's statcat extension. |
| Product | The unit of publication, routing and lifecycle. It can hold several `dcat:Dataset`s, one per dataflow. A `structure` product holds codelists and no datasets. |
| Edition | One sealed, immutable state of a product, identified by `edition_id` (UUIDv7) and linked to its predecessor by `previous`. It is a reference period only for `final` products: revisable LFS has many editions of one product, while census has one product per reference period, grouped in a `ProductSeries`. In Python an `Edition` is a frozen value (`edition_id`, `product_id`, `previous`, `manifest_sha256`) with no methods. |
| WorkingCopy | A product's single unsealed edition. sdmxlib calls it an "unsealed edition". It is unrelated to SDMX 3.0 `-draft` artefact versions. |
| EditionView | The read-only query object over one edition: `get`, `query`, `search`, `facets`, scopes. |
| Inventory | The chain of Inventory versions. One version is a root document plus one leaf per product, each immutable and content-addressed. |
| `CURRENT` | A hint naming the newest Inventory version. Readers verify it and probe beyond it. |
| States | Every Inventory entry has one of four states:<br>• `embargoed`: sealed, recorded, not routable, with `release_at`.<br>• `live`: routable.<br>• `superseded`: replaced by a newer live edition, and a valid rollback target.<br>• `withdrawn`: rolled back from, and never routed again automatically.<br>Retired editions are dropped from the next version and kept as tombstones. |
| Lifecycle | `final` or `revisable`. The old lifecycle value `live` is gone; `live` is now only a state. |
| seal / release / upload | **Seal** makes a WorkingCopy an Edition. **Release** makes an edition `live` in a new Inventory version. **Upload** copies a sealed edition to blob storage and registers it as embargoed. |
| discard / rollback / prune | **Discard** throws a WorkingCopy away; there is no `revert()`. **Rollback** makes a superseded edition live again and withdraws the current one. **Prune** deletes the bytes of retired editions. |
| non-fast-forward | A release whose edition's `previous` is not the product's current live edition. It is refused. |
| sealed-but-unrecorded | An edition with a manifest that no Inventory version has ever named. The next writing call repairs it (§6). |

### 2. Layout

```
<catalog>/catalog.json                     format_version, catalog_id
<catalog>/CURRENT                          hint: {seq, root_sha256}
<catalog>/inventory/<seq>.json             root, created exclusively (zero-padded seq)
<catalog>/inventory/leaves/<sha256>.json   leaves, content-addressed
<catalog>/pending/<product>/<edition>.json blob only: upload inbox (§7)
<catalog>/products/<product_id>/data/
<catalog>/products/<product_id>/<edition_id>/{manifest.json, metadata.duckdb,
                                             tables.ducklake, search/}
```

- A WorkingCopy is an `<edition_id>/` directory with no manifest.
- Blob storage uses the same names, so upload is a copy.
- **Refusing other layouts.** Opening a directory that has a root-level
  `meta.duckdb`, a `bundle.manifest.json`, or no `catalog.json` beside
  existing files is refused, and nothing is created.
- **The ADLS boundary is the account or container** (lakehouse reviewer). On
  ADLS Gen2, RBAC is evaluated before POSIX ACLs, so any principal with
  Storage Blob Data Reader on the container reads embargoed bytes. Data
  engineers and Databricks workspaces usually hold that role. So:
  - the Catalog lives in a container (or account) that only the publish job,
    the GC job and the pods hold a role on;
  - `results/` goes on a separate account;
  - every release checks, as a precondition, which principals hold Data
    Reader on the Catalog's container.

### 3. The Inventory format

sdmxlib owns the schemas, validation, the state-transition rules and a reader.
statcatlib owns the writer and its statcat extension (§10).

**Root** (`inventory/<seq>.json`):

| Field | Meaning |
|---|---|
| `format_version` | `"1.0"` |
| `seq` | This version's sequence number |
| `previous_inventory` | The `seq` of the version this one was built from |
| `created_at` | An RFC 3339 UTC instant ending in `Z` |
| `producer` | The library and version that wrote this root |
| `embargo` | `true` or `false` |
| `products` | A map from `product_id` to `{leaf, leaf_sha256}` |
| `retired` | Tombstones (below) |
| `extensions.statcat` | Includes the `CatalogDescription` |
| `event` | Who acted and why (below) |

- **Tombstones.** `retired` lists every retired `edition_id` within the
  retention window, so a retired edition stays distinguishable from one that
  was never recorded.
- **Audit.** `event` holds `{actor, reason, kind}`. It names the acting
  principal and the reason for each version, where kind is one of `seal`,
  `release`, `rollback`, `withdraw`, `retire` or `embargo-change`.

**Leaf** (`inventory/leaves/<sha256>.json`): one product's entries for every
edition that is not retired.

- Each entry: `edition_id`, `previous`, `state`, `manifest_sha256`, `kind`,
  `lifecycle`, `schema_version`, `state_changed_at`, `release_at` (null for an
  embargoed edition with no scheduled release), and `structure_source`.
- `artefacts` with their `partial_of` fingerprints (`cut_from` today) are
  optional 1.x fields (D25). The release job enforces the structures-first
  rule (spec invariant 10) from the manifests, which it reads anyway to
  verify `manifest_sha256`.
- Leaves are content-addressed and written before the root that names them.
  A one-product change writes one new leaf and one new root.

**Versioning.** A minor version adds only optional fields, and readers ignore
unknown fields. Phase 4's `pool`, `pinned`, `location`, `pull_bytes` and
`dataflows` arrive as 1.x optional fields.

**The live view.** The live view is derived inside sdmxlib's reader and never
stored. sdmxlib exposes two separately constructed reader
types:
- `LiveInventory` returns only live entries and drops products that have no
  live edition.
- `FullInventory` returns everything and is used only by the operational
  objects and the release job.

The public Catalog and the pods' routing use `LiveInventory`, so a public
reader never sees an embargoed entry and has nothing to filter. That moves the
capability split into sdmxlib, where the pods' reads happen (structure
reviewer, round 3).

**The head.** A product's current edition is its `live` entry at the
reader's `schema_version` (D24). During a store schema rollout (spec §9.2) a
product has one live edition per schema version: old pods read the old one,
new pods the new one. The fast-forward check runs per (product,
`schema_version`). A product with no live edition has no head, and
`product().view()` raises an error naming its states.

**An escape route.** The Inventory is a small log of our own, close to
Delta's `_delta_log` with a hint pointer. If it ever needs checkpoints, leaf
compaction or more tree levels, that need triggers a review of replacing it
with a Delta table of Inventory rows rather than adding a feature.

### 4. The write protocol

**Atomic exclusive publish.** Exclusive creation must win the
compare-and-swap with the file already complete. `O_EXCL`, and ADLS DFS
create followed by separate append and flush calls, both create the name
before its content, so a crash leaves an empty head that blocks every later
write (design and lakehouse reviewers). So:
- **Locally:** write a temp file, fsync it, then `link()` it into place, or
  use `renameat2(RENAME_NOREPLACE)`, `renamex_np(RENAME_EXCL)` or `MoveFileEx`
  without the replace flag. Then fsync the directory.
- **On ADLS:** use a single-shot Blob `Put Blob` (or `Put Block List`) with
  `If-None-Match: *`, never DFS create+append. statcatlib uses `obstore`
  (`LocalStore`, `AzureStore`, `put(..., mode="create")`).

**Retry.** A writer that loses re-reads the new head and re-evaluates every
precondition against it: the fast-forward check, the expected current
edition, and the `embargo` header. A stale automatic release must not write
`live` after a concurrent change turned embargo on.

**`CURRENT`.** It holds `{seq, root_sha256}`. Writes to it are conditional
and only move forward. Readers:
1. read `CURRENT`;
2. verify the root's sha256, and fall back to the previous version on a
   mismatch;
3. probe `seq+1`, `seq+2`, … until one is absent, with no LIST;
4. never accept a lower seq than the one they already hold.

This is Delta's `_last_checkpoint` pattern. On startup, the release job
brings `CURRENT` up to the newest root.

**Unsupported locations.** Synced folders (Dropbox, OneDrive) and network
shares are documented as unsupported.

**Retention.** Inventory versions are kept for a configured window. The
window must exceed the longest pod poll interval plus fetch time. Objects
reachable from the newest K roots are never deleted.

### 5. State transitions

| From | To | Operation | Precondition | Who |
|---|---|---|---|---|
| (sealed) | `live` | seal, embargo off | product has no live edition and `previous` is null, or `previous` = the live edition | `WorkingCopy.seal()` |
| (sealed) | `embargoed` | upload or register | same fast-forward check against the live edition | publish job |
| `embargoed` | `live` | release, or `release_due(now)` | `release_at` ≤ now (storage-server time), or explicit; fast-forward check | `CatalogAdmin` |
| `embargoed` | `withdrawn` | cancel | none | `CatalogAdmin` |
| `embargoed` | `withdrawn` | `supersede_pending(product, by=)` | the new edition is built on the same live edition | `CatalogAdmin` |
| `live` | `superseded` | another edition of the product goes live | (same Inventory write) | (same writer) |
| `live` | `withdrawn` | `rollback(product, to=Y, if_live=X)` | live = X (by default, the edition live when the call started), and Y is `superseded` | `CatalogAdmin` |
| `superseded` | `live` | the same rollback | as above | `CatalogAdmin` |
| `superseded`, `withdrawn` | tombstone | retention | past the window | prune job |

Notes:
- **Rollback is conditional.** `if_live` defaults to the edition that was
  live when the call started. A caller passes it explicitly when the decision
  was made earlier, for example from a dashboard. A retry is refused if the
  live edition is no longer X. `to` defaults to the most recent superseded
  edition. It never moves `CURRENT` back to
  an old version, because that would drop other products' later releases.
- **Rollback ripples.** Any WorkingCopy or pending edition built on a
  withdrawn edition then fails the fast-forward check. The error names the
  withdrawn base, the current live edition, and the fix: discard, then start
  from the live edition.
- **Release batches report per product.** `release_due` releases what it can
  and refuses the rest, without failing the whole batch.
- **The release clock is the storage server's.** The release job takes `now`
  from the storage response `Date` header, with a skew bound, and refuses to
  run if its own clock is ahead. `release_due(now=None)` defaults to that time
  and refuses naive datetimes.

### 6. Crash recovery

A seal writes the manifest (sdmxlib) and then an Inventory version
(statcatlib), so a crash between the two leaves a sealed-but-unrecorded
edition.

**The repair is narrow.** Design and structure found that a broad repair
re-releases withdrawn editions: B rolled back, then retired, still has a
manifest naming the live edition as `previous`. So the repair records an
edition only when:
1. no Inventory version within retention has named it, and it is not a
   tombstone; **and**
2. it is the edition this WorkingCopy itself sealed, read from a journal entry
   that `seal()` writes before the manifest.

**The repair runs when a WorkingCopy is created**, before `build()` picks
`previous`, and again on `seal()` and `resume()`. Running it at creation stops
a new WorkingCopy from being built on the wrong base. Reads never repair.

**`rebuild-inventory` is narrow too.** It rebuilds from the manifests only for
a Catalog that has never been rolled back, because `withdrawn` is not
recorded in any manifest. Otherwise it refuses and names the missing history.

### 7. Publishing at .Stat scale

- **Upload never writes `CURRENT`.** It copies the edition with its manifest
  last. It verifies every file the manifest lists. Then it writes
  `pending/<product>/<edition>.json`, holding the edition id and `release_at`,
  put-if-absent.
- **`pending/` is an inbox, not an authority.** The release job folds pending
  registrations into an Inventory version as `embargoed` entries and then
  deletes them. From then on the Inventory is the only authority.
- **Duplicates are refused.** Two pending editions of one product built on the
  same live edition are both refused, and the job reports them.
- **Releases are batched.** Only the release job writes Inventory versions on
  the server, once per batch. Run it with `concurrencyPolicy: Forbid`.
- **Laptops batch too.** A batched seal records many products in one
  Inventory version, so a bulk ingest doesn't write thousands of roots.
- **Pods** conditional-GET `CURRENT`, probe forward, fetch the root, and fetch
  only the leaves whose hash changed (Phase 6). They fail static on errors and
  expose the Inventory's staleness against the expected release instant as a
  metric.
- **Throughput at 08:30** (lakehouse reviewer). A standard GPv2 account
  targets about 20,000 requests a second. 300 released tables × 20 hot pods ×
  about 1,000 cold tier-3 GETs is about 6 million GETs.
  - Promote the day's pinned hot products to node-local disk before 08:30.
  - Load-test throttling in the deployment's region.
- **Alerts:**
  - embargoed editions past `release_at`;
  - sealed-but-unrecorded editions;
  - `CURRENT` behind the newest root;
  - GC deletions;
  - storage 503s.

### 8. Ingest, FMR and the WorkingCopy on servers

- **The landing contract** (lakehouse reviewer). ADF, Spark and Databricks
  can't write a DuckDB-file DuckLake catalog. Pipelines write Parquet or Delta
  to a raw zone they own. One statcatlib build job per product, serialised by
  the orchestrator, reads it into a WorkingCopy, seals, and uploads.
- **A WorkingCopy on an ephemeral disk can be lost.** Recovery is `discard`
  plus a rebuild from the landing zone.
- **Structures from FMR.** When FMR is authoritative, `metadata.duckdb` is a
  per-edition cache of an FMR closure plus the statcat records.
  - Each manifest records `structure_source`: the registry URL, the query, the
    retrieval instant, and the artefact fingerprints.
  - FMR edits `1.0` artefacts in place, so a version number is not an
    identity. The release rules compare fingerprints.
  - Release operations include an FMR freeze window and an 08:00 pre-flight.
    The pre-flight diffs FMR's live fingerprints against the pending editions
    and reports the products held back.
- **Corrections and schema changes.**
  - A correction while an edition is embargoed goes through
    `supersede_pending`.
  - An additive DSD change keeps the table, and any other change starts a new
    table or product.
  - A store schema bump republishes every product. Set a seal throughput
    target before Phase 5, for example a full rebuild within one non-release
    window.
- **Governance mirror.** An optional mirror (Postgres DuckLake or Unity) can
  be fed from the Inventory chain for governance and audit queries. It is
  never authoritative.

### 9. The Python surface

```python
# laptop
cat = sc.Catalog.create("~/stats")                    # writes catalog.json and version 1
wc = cat.product("14100287").working_copy()           # creates or resumes; refuses a second
sc.ingest_sdmx(wc, registry)
wc.view().query(sc.AggregateDataset).first()          # read your own writes
ed = wc.seal()                                        # Edition value; auto-released (embargo off)

cat = sc.Catalog.open("~/stats")                      # never writes
cat.product("14100287").view().query(...)             # EditionView over the live edition
cat.product("14100287").editions()                    # live view: id, state, previous

# operations
admin = sc.CatalogAdmin.open("~/stats")
admin.rollback("14100287", to=prior_id)         # if_live= defaults to the edition live now
admin.release_due()                                   # server, at 08:30

reader = sc.EmbargoReader.open("abfss://…")           # its own BundleEngine
reader.open_edition(edition_id)                       # EditionView over an embargoed edition
```

- **The write entry points are `Catalog.create()`, `product().working_copy()`
  and `WorkingCopy.seal()`.** A Catalog from `Catalog.open()` never writes.
- **The audit rule.** Only `CatalogAdmin` changes the Inventory, except for
  the automatic release in `seal()` on a Catalog with embargo off. Only
  `EmbargoReader` sees embargoed editions. Both are constructed explicitly and
  are never properties of `Catalog`.
- **WorkingCopy:**
  - writes through `put`, `bulk_load`, `transaction`, `build_index`, `data`
    and `registry`, and queries through `view()`;
  - `seal()` closes every cursor first, and any call after `seal()` or
    `discard()` raises a named error;
  - creating a second WorkingCopy is refused with an error that names the
    existing one and its start time, and gives the resume and discard calls.
- **Open-ended handles.** `connection`, `cursor`, `bundle_dir` and
  `from_connection` leave the public surface. `Catalog(":memory:")` is
  replaced by a test helper that builds a Catalog in a temporary directory.
- **Guided errors for old calls.** `Catalog(path)` and methods that moved to
  `EditionView` raise errors naming the new call. Cross-product `search` and
  `facets` raise "not yet supported" until the multi-product view exists.
- **A copied Catalog opens with `Catalog.open`.** It writes nothing: no
  repair, no DDL, no lake stamp. A copy is outside the embargo boundary, so
  `CatalogAdmin.export(live_only=True)` makes safe copies. The export keeps
  the `catalog_id` and writes one fresh Inventory version.

### 10. Code changes in statcatlib

From the codebase reviewer's verification, rounds 2 and 3:

- **EditionView builds on today's `read_only=True` path.** That path already
  issues no DDL, skips the lake-stamp repair and creates no `search/`
  (`store.py:2924-2925`, `schema.py:377-389`, `store.py:3126-3127`,
  `store.py:3209-3213`).
- **No SQL rewrite is needed.** Point each cursor at the edition's catalog
  with `USE "<catalog>".statcat` in place of `SET search_path = 'statcat'`
  (`store.py:3420`, `store.py:3729`, `schema.py:363`, `schema.py:388`). That
  covers the roughly 133 unqualified table names in `store.py` and the two-part
  names in `query.py` and `scopes.py`.
  - The catalog name comes from a public sdmxlib accessor, which today exists
    only as the private `_sdmx_catalog()` (`registry.py:731`).
  - A test confirms that two-part names resolve inside the `USE`d catalog
    while two editions are attached.
- **Opening checks are scoped to one catalog.** `schema_exists`
  (`schema.py:414-416`) and the `_meta` checks (`store.py:193`, `216`, `277`,
  `282`) filter by `table_catalog`.
- **The WorkingCopy rides the build registry's connection.**
  `LocalRegistry.build()` holds `metadata.duckdb` exclusively, and its lake is
  already attached as `lake`, which would clash with a second `lake` attach.
  So:
  - a new constructor takes the build registry plus the edition directory,
    which keeps Lance and `build_index` working;
  - observations are read through the registry's data plane;
  - `create_schema` runs only for a product's first edition, because
    `build(previous=)` copies the statcat tables.
- **Delete the data-path machinery:** `bundle.py:105-591`,
  `store.py:3048-3129`, and the CLI's `rebind` and `explain`.
- **`add_lineage` gains its missing writable guard** (`store.py:7219-7263`).
- **Search.** BM25 tables are rebuilt at each seal. Embeddings are carried
  forward, keyed by a hash of the embedded text.

### 11. The sdmxlib release

The order of work comes from the readiness reviewer.

1. **Measure the seal cost** on census and on one CODR table, with a stated
   threshold that would change the seal design. This measurement gates the
   spec PR.
2. **Spec PR** to `docs/design/sharded-bundle-catalog.md`, and to plan line
   49, the non-goal "Renaming token". It contains:
   - manifest 3.0;
   - the layout (§2);
   - the root and leaf schemas with worked examples (§3);
   - the write and read protocol (§4);
   - the transition table (§5);
   - the unrecorded-edition rule (§6);
   - the ownership split (§3);
   - an old→new table for every renamed symbol, key, file and CLI command;
   - the release version and the store schema version.
3. **The format rename:**
   - `manifest.json` with keys `edition_id`, `previous`, `product_id` and
     `root: "edition"`;
   - the manifest's `product` field becomes `producer_fields`;
   - lifecycle `live` becomes `revisable`;
   - the files become `metadata.duckdb` and `tables.ducklake`;
   - the store meta key `publication_token` becomes `edition_id`, including in
     `_resume` and `build`;
   - `inventory()` becomes `list_files()`;
   - `cut_from` becomes `partial_of`, `ProductGroup` becomes `ProductSeries`,
     tiers become `required`/`lazy`/`remote`, and the kind `structures`
     becomes `structure`;
   - `build`, `bundle`, the `*_bundle` functions, `BundleEngine` and the CLI
     `bundle` group get their final names, listed in the spec's rename table.
4. **The Inventory core:** schemas, validation, transition rules,
   `LiveInventory` and `FullInventory`, and the reader over a small
   `fetch(path) -> bytes | None` protocol. sdmxlib holds no blob client
   (spec §11.5).
5. **Behaviour changes, each with a refusal test:**
   - `build()` refuses a second unsealed edition;
   - `list_editions()`;
   - `discard()`;
   - an unsealed `synthetic_bundle`;
   - the public catalog-name accessor;
   - carrying `search/` forward on `build(previous=)`;
   - writing the seal journal entry before the manifest.
6. **A pre-release, then the release.** `docs/upgrading.md` states that
   catalogs are rebuilt by re-ingesting, because there is no migrator.

Then:
- **sdmxlib-rest:** an adoption issue that pins the new release and reads
  manifest 3.0. It lands before statcatlib's Phase 2a merges.
- **statcatlib:** Phase 2a stays on a branch until then.

### 12. Migrating a .Stat Suite installation

The .Stat operator persona found gaps that are outside Phase 2a. They need
their own migration design. None of them changes a 2a format except where
noted.

- **Search for Data Explorer.** Data Explorer finds dataflows through sfs
  (Solr), not SDMX REST. A migration needs an sfs-compatible search endpoint
  built from the live view, with a facet parity test. The plan currently has
  holdings-wide search last (Phase 7).
- **The serving path.** Nothing can be migrated until pods read the Inventory
  and map dataflows to products (Phases 4 to 6).
- **Internal preview.** .Stat staff check embargoed data in Data Explorer with
  `X-Release: PIT`. The counterpart here is a separate, authenticated preview
  deployment that runs `EmbargoReader` under its own storage identity,
  isolated from the public pods.
- **The write path.** Transfer imports, DLM uploads and releases, and SDMX
  REST structure submission have no counterpart. Ingest becomes the landing
  contract (§8), and structure authoring moves to FMR (spec §8).
- **Permissions beyond embargo.** Per-dataflow and per-audience access maps to
  one Catalog per audience, each with its own storage identity and pods.
- **Data spaces:**
  - design maps to the WorkingCopy;
  - staging maps to embargoed editions;
  - dissemination maps to live editions.
- **PIT import.** A tool reads the mapping table and the A/B tables. It
  writes live, embargoed and superseded editions with `previous` links,
  converts release dates to UTC, allows `release_at: null` for manual
  releases, and diffs observations against the NSI.
  - .Stat embargoes per DSD, so a DSD shared by N dataflows becomes N products
    released in one batch.
- **Observation confidentiality.** Anything sealed is public at release.
  `seal(checks=[...])` takes publisher-supplied checks, and a ready-made
  `CONF_STATUS` check ships as an opt-in (D26). It is not a default, because
  many publishers suppress values before loading and others use their own
  attribute or codes.
- **`updatedAfter` and history.** These are an sdmxlib-rest serving question.
  A recent window can be derived from the edition chain (each edition is a
  dated snapshot) with no format change, back as far as retention keeps
  editions. A stored `LAST_UPDATED` column is added, as an optional column,
  only if a publisher needs history beyond retention (D26).
- **Still to check:** reference metadata endpoints, and Data Explorer
  annotations (`NOT_DISPLAYED`, `LAYOUT_*`) with `availableconstraint` parity.
  A parallel run needs dual-write, a daily diff, and a release freeze during
  the rollback window.

Improvements over .Stat the reviewer recorded:
- immutable history with rollback to any superseded edition;
- one atomic release of hundreds of tables;
- UTC instants;
- embargo enforced by storage identity, not by a check in code that can be
  commented out.

### 13. What .Stat Suite's embargo design teaches

.Stat's point-in-time (PIT) mechanism keeps embargoed data in the public
dissemination database:
- each DSD has A/B fact tables, and a mapping table assigns them to Live and
  PIT;
- release moves no data;
- serving filters by an `X-Release` header and the `CanReadPitData`
  permission;
- search is one shared Solr index that holds future dataflows behind a date
  filter.

Its bugs were all in derived state:
- an availability cache that replayed a cancelled release date;
- related-dataflow constraints that weren't updated;
- mapping sets regenerated only on some changes;
- a swallowed error on the rollback path;
- a future dataflow version resolved as "latest";
- PIT data readable with plain read permission;
- release times parsed in the server's locale.

Requirements carried forward:
- every derived artefact is regenerated from `CURRENT`, never patched;
- serving embargoed editions is off by default;
- release times are UTC instants;
- rollback tests reset every derived artefact.

Sources: https://sis-cc.gitlab.io/dotstatsuite-documentation/using-api/embargo-management/
and the dotstatsuite issue trackers on GitLab.

### 14. Review history

- **Round 1** took the brief from mutable folders to products and editions.
- **Round 2** took it from standalone and managed modes to one Inventory
  everywhere.
- **Round 3** added:
  - the atomic exclusive publish;
  - the narrow repair;
  - conditional rollback;
  - the probing `CURRENT` reader;
  - derived live views in sdmxlib;
  - `pending/` as an inbox;
  - the ADLS container boundary;
  - FMR provenance;
  - the landing contract;
  - the migration findings.

Superseded along the way:
- standalone and managed modes;
- the head as the end of the manifest chain;
- the sdmxlib fork refusal;
- lock files;
- a flat Inventory list;
- a stored `retired` state;
- `draining`;
- `Draft` and `WorkingEdition`;
- Holdings;
- a stored live projection;
- the embargo setting and DCAT record in `catalog.json`.

### 15. Enterprise architecture review (Microsoft Fabric)

A reviewer took the role of an enterprise architect whose strategy is
Fabric, told to argue for it but concede where it falls short.

**Where Fabric stands against each part of this design:**
- **OneLake** is ADLS Gen2 underneath, so it is a hosting choice, not a new
  design.
- **Delta time travel** can't replace editions and the Inventory. A Delta
  version belongs to one table, VACUUM limits how far back it reaches, it
  records no file hashes, and a Lakehouse can't commit across tables.
- **Fabric Data Factory** fits the landing contract (§8) unchanged.
- **Purview** records lineage after the fact. It can't refuse a non-fast-
  forward release or do a conditional rollback, so it works as a mirror and
  not as an authority.
- **OneLake data access roles** have the flaw of ADLS RBAC: workspace Admin,
  Member and Contributor roles bypass them, so they can't be the embargo
  boundary.
- **Warehouse and Power BI** are fine for internal analytics, but have no SDMX
  structures and no SDMX REST.

**Fabric would fail this workload** on four counts:
- it has no laptop or offline mode;
- it has no anonymous origin that a CDN can cache;
- an atomic 08:30 release would go through an Entra-authenticated SQL
  endpoint, under capacity throttling;
- it has no SDMX structure semantics.

Hosting the Catalog in OneLake would also tie public dissemination to
capacity units at the 08:30 spike.

**Requirements carried forward:**
- **A live-only replica is the only Fabric touchpoint.** OneLake shortcuts and
  Purview scans need an identity that can read the Catalog's container, and
  that identity could read embargoed editions. So they point only at a
  separate live-only replica account, regenerated from `CURRENT` and never
  patched. `CatalogAdmin.export(live_only=True)` becomes a supported, continuous
  derived artefact with its own lag alert (D30).
- **A conformance suite for the Inventory protocol.** The Inventory is the part
  a review board will call undifferentiated heavy lifting. The crash and
  concurrency tests of §4 and §6 ship as a conformance suite owned by sdmxlib.
  The same suite runs against a Delta-backed Inventory prototype, which proves
  the escape route in §3 rather than asserting it (D31).
- **A Delta projection for internal use.** A non-authoritative Delta
  projection of live observation tables, and a Delta mirror of Inventory rows
  carrying the `event` fields, let Fabric and Power BI read the data. Whether
  DuckLake can emit Delta or Iceberg metadata itself is unverified.
- **The access precondition checks effective access.** It covers nested Entra
  groups, eligible PIM assignments, inherited subscription roles and Fabric
  workspace identities. The publish job, the GC job, the pods and the preview
  deployment use Entra workload identities, with no keys and no SAS tokens.
- **The design document records each Fabric capability** as adopt (pipelines,
  Entra, Purview on the replica), integrate (Delta mirror, shortcuts to the
  replica) or reject (Delta time travel, OneLake roles as the boundary,
  Warehouse serving), each with a reason tied to a decision. It also needs:
  - an embargo threat model listing every identity that can read embargoed
    bytes;
  - a cost model comparing requests at 08:30 with capacity units;
  - an exit and portability statement;
  - data residency.
- **Untested:** the atomic exclusive publish (`If-None-Match: *`) on OneLake's
  Blob API.

### 16. Operations review

A reviewer took the role of the IT operations team that runs the system day
to day on Azure and supports analysts on laptops.

**Restoring a whole Catalog breaks the forward-only readers.** After a restore
from backup or a failover to an RA-GZRS secondary, `CURRENT` comes back lower
than the seq every pod holds. The pods stay on stale data, and the next write
reuses seq numbers from the lost history. Asynchronous replication can also
deliver a root before its leaves or files. So:
- `catalog.json` and `CURRENT` carry a `restore_epoch`. Readers reset their
  seq floor when it changes (D27).
- `verify-catalog --find-newest-consistent` names the newest complete
  version.
- A failover runbook: freeze the release job, verify, bump the epoch, restart
  the pods, purge the CDN.

**Restoring one product or edition has no supported path.** One `CURRENT`
covers every product, and §5 has no transition from a tombstone. So:
- `CatalogAdmin.reinstate(product, edition, reason)` verifies the manifest and
  every listed file. It writes one forward Inventory version with a `restore`
  event, entering the edition as `superseded`, so making it live takes a
  separate rollback (D28).
- Restoring a product is: copy the bytes, reinstate, roll back.
- A restored Inventory version is never made current. Its contents come back
  only through `reinstate`.

**Backups can resurrect withdrawn data.** Once the tombstone window has
passed, a `pending/` file restored from an old backup counts as never named,
and the release job would publish it. So:
- tombstones are kept at least as long as backups;
- `pending/` is excluded from backups;
- the release job refuses a pending entry whose edition id (a UUIDv7, so it
  carries a time) is older than the tombstone window.

**"No migrator" doesn't work in production.** Re-ingesting loses the edition
chain, rollback targets, withdrawals and audit history. It also assumes the
landing zone still holds the source of every product, including final census
products. So:
- "No migrator" applies before 1.0 only.
- After 1.0, a format change is either an additive 1.x change or ships with a
  migrator that carries the Inventory history forward.
- The landing zone keeps the source of every product that can be re-ingested
  (D29).

**The retention windows need one stated order.** There are five: backup
retention, `data/` GC, superseded editions, tombstones, and Inventory
versions. One configuration states the inequalities and checks them:
- backup retention ≤ the `data/` GC window ≤ the tombstone window, as spec
  §10.3 requires;
- poll interval plus fetch time < Inventory retention.

Prune refuses to start if the inequalities fail, and it has a dry run and a
deletion log.

**Upgrades need a soak.** A rolled-back pod image finds no head once the
old-schema entries are retired (D24). Old-schema live entries stay until the
rollback window closes, and the rollout runbook gates spec §9.2 step 5 on it.

**Backups need an order and a proof.**
- Copy in dependency order: `data/`, editions, leaves, roots, then `CURRENT`.
- Give the seal journal (§6) a place in the layout.
- `verify-catalog` walks the newest root and checks every leaf, every
  `manifest_sha256` and every listed file's digest. Its exit status is the
  monthly restore test.

**Withdrawing a confidential release needs an expunge.** A withdrawn edition
keeps its bytes in the Catalog, the backups and the CDN. So:
- `expunge` purges the CDN, the `results/` keys and pod disks;
- it deletes the edition's bytes at once, with an audit entry;
- it records which backups need scrubbing;
- every rollback also purges the product from the CDN.

**A corrupt or wrong Inventory version.** Readers skip an invalid root and
alert. `CatalogAdmin.correct(from_seq=, reason=)` writes the next version from
a known-good one, with an audit entry. Exclusive create means a bad version is
only ever fixed by writing a new one.

**Access control is a convention until identities are separated.**
- Container RBAC can't stop the upload identity from writing `inventory/`, so
  "only the release job writes" needs separate write paths per identity, by
  ACL-only roles or separate containers.
- `event.actor` comes from the token's object id, never from the caller.
- The reader-principal check runs at the 08:00 pre-flight against a reviewed
  allowlist, so an overnight access change doesn't silently block 08:30.

**Every alert needs a runbook.**

- **Existing alerts:**
  - **Embargoed past `release_at`** needs `release_due`'s per-product refusal
    reasons persisted, and a manual release command that takes a reason.
  - **Sealed-but-unrecorded on a server** needs an owner and a
    discard-or-reinstate decision.
  - **`CURRENT` behind** needs a safe `advance-current` command.
  - **GC deletions** alert on a deviation from the dry-run forecast.
  - **Storage 503s** need a threshold and steps.
- **Missing alerts:**
  - the release job missed or failed;
  - a root fails validation;
  - pod staleness over a threshold;
  - old `pending/` entries;
  - a failed access precondition;
  - backup age;
  - replication lag;
  - stale WorkingCopies on build nodes.

**Laptop support:**
- `statcat doctor` is read-only. It reports the layout, chain validity, the
  WorkingCopy, the journal and a synced-folder warning, and produces a support
  bundle.
- A local prune comes in 2a (D32).
- An upgrade refusal names the re-ingest steps.

**Not covered yet:** rotating workload identities, CDN certificates and
`obstore` credential refresh in long-running pods. These go in an operations
appendix in the design document.

## Goals vs. Non-Goals

* **In-scope goals (statcatlib Phase 2a, after the sdmxlib release):**
  * The pin to the new sdmxlib release, with `just ci` green.
  * The layout, the Inventory writer and the atomic exclusive publish on
    local disk.
  * `Catalog.create`, `Catalog.open`, `product().view()`, `editions()`,
    `working_copy()`, `seal()` and `discard()`.
  * Automatic release when embargo is off, a batched seal, the narrow repair
    and the crash tests.
  * `CatalogAdmin.rollback(product, to=, if_live=None)`, the same call on a
    laptop and a server (D23), and the embargo setting.
  * `seal(checks=[...])`, with an opt-in `CONF_STATUS` check.
  * The EditionView over the read-only path, with `USE` per cursor and
    catalog-scoped checks.
  * The WorkingCopy over the build registry.
  * Deleting the data-path machinery, refusing legacy layouts and adding
    guided errors.
  * The statcat extension carrying `CatalogDescription`, and statcat section
    format 3 with `kind: "data"`.
  * `docs/upgrading.md`.
* **Out-of-scope non-goals:**
  * Blob storage writes, `pending/`, the release job, `release_due`,
    `EmbargoReader`, prune and the pods' reader (Phases 4 to 6).
  * Cross-product search and facets.
  * Moving `metadata.duckdb` into DuckLake, which needs measurements to
    reopen.
  * Encryption-based embargo.
  * The .Stat migration tools (§12), which need their own design.

## Acceptance Tests

| Decision | Test |
|---|---|
| D1, D12 | statcatlib pins the sdmxlib release, and `just ci` is green with no compatibility code. |
| D2 | `Catalog.open` on a fixture with two products lists both. `product().view()` returns an EditionView. |
| D3 | The first seal creates version 1 with the edition `live`. A second seal supersedes it. An open Catalog sees the new edition after refresh. |
| D4, D13, D16 | The public Catalog never returns an embargoed entry, tested against a fixture Inventory that holds embargoed entries. `EmbargoReader` uses its own engine. `Catalog` has no property returning either operational object. |
| D5, D18 | A docs check that the boundary is stated. (Phase 5: the release precondition lists the principals that hold Data Reader.) |
| D6 | Two concurrent seals on different products both end up live. Killing the writer between create and the last byte leaves the previous head readable and writable. |
| D7 | A non-fast-forward release is refused and names both editions. A conditional rollback retry after a competing release is refused. |
| D8 | Every written root and leaf validates against sdmxlib's schemas. A 1.1 root with unknown fields is read; a 2.0 root is refused. |
| D9 | Sealing an edition and then changing anything under it fails `check_bundle(at="open")`. |
| D10 | Legacy layouts are refused, and the file set is unchanged. |
| D11 | A one-product change writes one new leaf and one new root. Other leaves are reused by hash. |
| D14 | The enum has four values. A withdrawn edition is not re-released by any repair or by `release_due`. |
| D15 | Calls after `seal()` or `discard()` raise. A second `working_copy()` is refused. |
| D17 | Changing embargo writes a new Inventory version through `CatalogAdmin`. A stale automatic release after embargo is turned on is refused. |
| D19 | `Catalog(":memory:")` raises with a pointer to the temporary-directory helper. |
| D20 | `CatalogDescription` round-trips through the statcat extension. Two Inventory versions can carry different descriptions. |
| D21 | A second seal with unchanged text calls the embedding model zero times for that text. |
| D22 | The measurement and its threshold are recorded in the sdmxlib issue before the spec PR merges. |
| §6 | Kill at each step of seal, then open a new WorkingCopy: one live edition, no duplicate, correct `previous`. |
| §4 | A reader holding seq 5 never accepts `CURRENT` = 4. A missing `CURRENT` update is found by probing. |
| §10 | Two editions attached in one engine resolve `statcat.x` and `sdmx.x` to their own catalog. |

## Key Decisions & Staging for Next Steps

Settled by the owner:

1. **The rename and core changes land in sdmxlib first.** Phase 2a pins that
   release.
2. **Catalog is the `dcat:Catalog`.** It holds Products, on a laptop too.
   Product stays Product.
3. **One head rule everywhere.** The Inventory decides, and a Catalog with
   embargo off releases on seal.
4. **The public side sees only live editions.** The operational side is off
   by default.
5. **Embargo has two layers:** storage identity for outsiders, and the
   operational split for trusted processes.
6. **Immutable sequence-numbered Inventory versions.** Exclusive creation is
   the compare-and-swap, and `CURRENT` is a hint.
7. **Forks are refused at release** (fast-forward). Rollback is explicit.
8. **sdmxlib owns the Inventory schemas and the reader.** statcatlib owns the
   writer and the extension.
9. **No lock files.** WorkingCopies are local, and nothing maintains a lake
   after its edition is sealed.
10. **The data-path machinery is deleted,** and legacy layouts are refused.
11. **Inventory shape:** a root plus per-product leaves, listing editions that
    are not retired.
12. **Phase 2a waits for sdmxlib's behaviour changes,** with no workarounds.
13. **Two operational objects.**
14. **Four stored states.** Retirement is a tombstone, never a state.
15. **The writable object is `WorkingCopy`.** sdmxlib says "unsealed edition".
16. **The operational objects are `EmbargoReader` and `CatalogAdmin`.**
17. **The embargo setting lives in the Inventory root,** and `CatalogAdmin`
    changes it.
18. **Only the jobs and pods read the Catalog's storage.** Round 3 sharpened
    the boundary to the account or container.
19. **`Catalog(":memory:")` is dropped.**
20. **`CatalogDescription` lives in the Inventory's statcat extension.**
21. **Search:** BM25 is rebuilt at each seal, and embeddings are carried
    forward by text hash.
22. **Seal cost is measured first.**

Adopted from round 3 as corrections, with no change of direction: the atomic
exclusive publish, conditional and forward-only `CURRENT` with probing
readers, conditional rollback, re-evaluating every precondition on retry, the
narrow repair at WorkingCopy creation, tombstones, the storage-server release
clock, `pending/` as an inbox, the audit `event`, `previous_inventory`,
`Edition` as a value, and `USE` per cursor instead of qualified SQL.

Settled by the owner after round 3:

23. **Rollback goes through `CatalogAdmin` everywhere.** The call is
    `CatalogAdmin.rollback(product, to=None, if_live=None)`, the same on a
    laptop and a server. `Catalog.open()` never writes. `product().rollback()`
    raises an error that names the `CatalogAdmin` call.
24. **The head key is (product, `schema_version`),** so the spec's
    zero-downtime schema rollout (§9.2) works unchanged.
25. **`artefacts` and `partial_of` are optional in Inventory 1.0.** The release
    job enforces the structures-first rule from the manifests.
26. **No 2a format change for the .Stat items.** `seal(checks=[...])` takes an
    opt-in `CONF_STATUS` check. `updatedAfter` is a later serving question,
    and no `LAST_UPDATED` column is added.

Assumed in this brief and not challenged by the owner:

- `revert()` is left out. `discard()` covers a bad WorkingCopy.
- `CatalogAdmin` ships in 2a with rollback and the embargo setting.
  `EmbargoReader` arrives in Phase 4 with its first caller.
- The live view is derived in sdmxlib's reader (`LiveInventory`,
  `FullInventory`), with no stored projection file.
- The FMR additions (§8) are accepted.
- The .Stat migration (§12) gets its own design document after this design is
  drafted.

Settled by the owner after the Fabric and operations reviews:

27. **A `restore_epoch`** in `catalog.json` and `CURRENT`, frozen in 2a, so a
    whole-Catalog restore or failover can reset the readers' seq floor.
28. **`reinstate`, and `restore` as an `event.kind`.** The `kind` enum freezes
    in 2a.
29. **"No migrator" applies before 1.0 only.** After 1.0, a format change is
    additive or ships a migrator, and the landing zone keeps every product's
    source.
30. **The live-only replica as a supported derived artefact.** It is the only
    Fabric, Purview and analytics touchpoint.
31. **An sdmxlib-owned conformance suite** for the Inventory protocol, also
    run against a Delta-backed prototype.
32. **A local prune in 2a,** with the retention inequality check, a dry run
    and a deletion log (§16).

Format consequences for the sdmxlib spec PR (§11):

- `catalog.json` and `CURRENT` gain `restore_epoch`.
- `event.kind` gains `restore` and `correct`.
- The root's `retired` tombstones are kept at least as long as backups.
- The layout gains a place for the seal journal.

All of these freeze in 2a. Phase 2a also gains `reinstate`, `correct`, prune,
`verify-catalog` and `statcat doctor`.

Open for the owner: none.

# Phase 2a: Catalogs of products and editions over sdmxlib 0.80

*   **Author:** Gabriel Gellner
*   **Status:** Reviewed. The EGM review (Stages 5–9, two resolution
    rounds and one re-audit) is in `docs/design/egm/phase-2a-catalog-editions/`.
    It awaits owner approval.
*   **Date:** 2026-10-31
*   **Issue:** GH-96. Implemented by GH-70. Suite tracker:
    StatCat-Suite/statcat-suite.github.io#15.
*   **Inputs:** the RFC [Catalog, Products, Editions and the
    Inventory](products-editions-inventory.md) (the concepts brief and the
    authority for statcatlib's API); sdmxlib's [sharded catalog spec
    v6](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
    (the authority for every file format); sdmxlib 0.80.0's code.

--------------------------------------------------------------------------------

## Overview

statcatlib 0.11 stores a catalog in one of two ways: as one DuckDB file, or as
one sdmxlib 0.72 bundle directory that `publish_bundle` writes. sdmxlib 0.80.0
made three changes:

- it replaced that bundle with **products** and **editions** (manifest 3.0);
- it added the **Inventory**, a chain of immutable documents that decides
  which edition of each product is live;
- it assigned the Inventory's writer and its `extensions.statcat` section to
  statcatlib.

Until statcatlib implements that writer, no catalog in the new format can be
written.

Phase 2a moves statcatlib onto sdmxlib 0.80.0 and onto the RFC's model, on
local disk:

- A **`Catalog`** is a directory of products. `Catalog.open()` opens one
  read-only, and `Catalog.create()` creates one or reopens it for writing.
- A product's **`WorkingCopy`** is its one unsealed edition. Ingest writes into
  it, and `seal(checks=[...])` turns it into an immutable **Edition**.
- With embargo off, a seal also releases its edition. One new Inventory version
  records the edition as `live`, and the previous live edition becomes
  `superseded`.
- An **`EditionView`** queries one edition through `get`, `query`, `search`,
  `facets` and the scopes. The public `Catalog` returns views of live editions
  only.
- **`CatalogAdmin`** is constructed separately and changes the Inventory. Its
  operations are rollback, the embargo setting with explicit release and
  cancel, the catalog description, and a narrow local prune.
- `statcat verify-catalog` checks a catalog's integrity, and `statcat doctor`
  reports on it for support.

2a deletes the bundle data-path machinery (`data_path`, `rebind`, `explain`)
and refuses the legacy layouts (see Legacy layouts).

## The Problem

statcatlib pins sdmxlib 0.72 and needs code changes to use any later release.
Each minor release from 0.73 to 0.80 changed something statcatlib calls, and
0.80.0 renamed `sdmxlib.bundle`, the module statcatlib's bundle code is built
on, to `sdmxlib.edition`. GH-70 started adopting 0.76 and paused when the RFC
redesigned the model. Until statcatlib moves, three things stay blocked:

1. **sdmxlib-rest cannot serve a statcatlib catalog in the new format.** Pods
   read the Inventory through sdmxlib. Only statcatlib may write an Inventory,
   and it does not write one yet, so no catalog exists that pods can route.
2. **The suite's next release is held.** The statcatlib release waits for the
   sdmxlib integration. Every sdmxlib fix since 0.72 is unavailable to
   statcatlib users, including refused out-of-codelist values, httpx2, the
   DuckDB 2.0 dependencies and language-tag normalisation.
3. **Callers handle files.** Today a caller passes a folder to
   `Catalog(path)`, picks a `data_path` policy, runs `publish_bundle`, and
   rebinds a copied bundle with `statcat bundle rebind`. The RFC's first goal
   requires callers to reach storage only through the API, and these steps
   break it.

The RFC settled the model and 32 decisions. It disagrees with the spec on
prune, the adoption gate, `restore_epoch` in `CURRENT`, the width of `seq`,
and `journal/`. It left four questions to this design:

- **Prune.** The RFC's Non-Goals place prune in Phases 4 to 6, but its
  decision 32 places a local prune in 2a. Settled by P7.
- **The adoption gate.** RFC §11 has Phase 2a merge after sdmxlib-rest's
  adoption (sdmxlib-rest GH-81) ships. Spec §18.1 requires only that
  sdmxlib-rest has an adoption issue. GH-81 is that issue, so the spec's gate
  is met. Settled by P2.
- **The upgrade.** Each breaking change from sdmxlib 0.72 to 0.80 must be
  mapped to the statcatlib code it touches. Settled in "The upgrade from
  sdmxlib 0.72".
- **The order of slices.** Each released slice must stand on its own, with its
  acceptance tests taken from the RFC's table. Settled by P5.

## Goals

The Detailed Implementation Plan's "RFC acceptance test map" names the test
for each goal.

*   **G1. sdmxlib 0.80 pinned.** `pyproject.toml` requires
    `sdmxlib>=0.80.0,<0.81`, `just ci` passes, and no code branches on the
    sdmxlib version (RFC decisions 1 and 12).
*   **G2. The layout and the Inventory writer on local disk.**
    `Catalog.create` writes `catalog.json`. The first seal writes Inventory
    version 1 and `CURRENT`, in spec §4.1's layout. Every root and leaf the
    writer produces validates with `sdmxlib.inventory.validate_root` and
    `validate_leaf`. The writer passes `sdmxlib.inventory.conformance.run`
    (spec §6.6).
*   **G3. The public surface.** `Catalog.create`, `Catalog.open`,
    `Catalog.product(id)`, `Product.view()`, `Product.editions()`,
    `Product.working_copy()`, `WorkingCopy.view()`,
    `WorkingCopy.seal(checks=[...])` and `WorkingCopy.discard()` behave as RFC
    §9 states. `Catalog.open` writes no bytes.
*   **G4. Release on seal.** With embargo off, a seal records the edition as
    `live` in the same Inventory version. A non-fast-forward seal is refused,
    and the error names both editions.
*   **G4b. Batched seal (S2b).** `Catalog.seal([...])` records every product it
    can in one Inventory version and returns per-product outcomes for the
    rest.
*   **G5. Crash safety.** Kill the writer at each named seal step, then open a
    new WorkingCopy. Exactly one edition is live, and its `previous` is the
    edition that was live before the seal. No edition is recorded twice. The
    repair records only editions that have a journal entry (spec §6.7).
*   **G6. `CatalogAdmin`.** It provides `rollback`, `set_embargo`, an explicit
    `release` and `cancel` for embargoed editions (P1), `describe` (S3, P3)
    and a narrow local prune (S4, P7). Each method that changes the Inventory
    records its `reason`. The CatalogAdmin section gives the signatures.
*   **G7. Seal checks.** `seal(checks=[...])` runs caller-supplied checks before
    the manifest is written. Any finding refuses the seal. statcatlib ships a
    `conf_status` check that callers opt into.
*   **G8. Read-only edition queries.** `EditionView` runs today's queries over
    an attached edition. Every statcat query runs on a cursor that has run
    `USE "<catalog>".statcat`. With two editions attached in one engine,
    `statcat.x` and `sdmx.x` resolve to each edition's own catalog.
*   **G9. The statcat sections.** The manifest's `extensions.statcat` moves to
    format 3 with `kind: "data"` (S2). `CatalogDescription` round-trips
    through the Inventory root's `extensions.statcat`, and two versions can
    carry different descriptions (S3).
*   **G10. Search carried forward.** BM25 tables are rebuilt at each seal (S2).
    A second seal with unchanged text calls the embedding model zero times for
    that text (S2b).
*   **G11. Old layouts and old calls refused.** Opening a legacy layout is
    refused and leaves the file set unchanged. `Catalog(path)`,
    `Catalog(":memory:")` and the methods that moved to `EditionView` raise
    errors that name the new call.
*   **G12. Integrity and support tools.** `statcat verify-catalog` walks the
    newest root and checks every leaf, every `manifest_sha256` and every
    listed file. It exits 0 when clean, 1 on any finding, and 2 when the
    catalog cannot be read. `statcat doctor` writes nothing. It reports the
    layout, chain validity, WorkingCopies, journal entries, sealed editions
    that no version names, and synced-folder risk.
*   **G13. Upgrade notes.** `docs/upgrading.md` gives each breaking change with
    its replacement call, and the re-ingest steps. Users re-ingest until 1.0
    (RFC decision 29). This is checked by review.

## Non-Goals

*   **Blob storage.** Writing to ADLS or any object store, `pending/`, upload,
    and the access-precondition check. The 2a writer targets local disk only.
    The publish protocol over `obstore` comes with Phase 4.
*   **Observations in object storage.** S1 removes 0.11's
    `data_path="abfss://…"` with `azure_credentials`. Remote data returns in
    Phase 4 through sdmxlib's `EditionEngine(secrets=)` (GH-180).
*   **The release job and embargoed reads.** `release_due`,
    `supersede_pending`, `EmbargoReader` and the storage-server release clock
    (Phases 4 to 6). The explicit `release` in 2a uses the local clock.
*   **`CatalogAdmin.correct` and `reinstate`** (P6). They are server recovery
    tools, and they move to Phase 4 with the backup and restore runbook
    (RFC §16). `restore` and `correct` are already in 0.80's `event.kind`
    enum, so deferring them changes no format.
*   **The server GC.** Deleting Inventory roots and leaves, expiring
    tombstones, sweeping unrecorded editions, and the five-window retention
    check (spec §11.4, §11.6). The 2a prune is the narrow one of P7.
*   **Serving.** The pods' reader, routing fields in leaves, and
    `advance-current`.
*   **Server-side restore.** Bumping `restore_epoch`,
    `verify-catalog --find-newest-consistent`, `expunge`, and the failover
    runbook. 2a writes `restore_epoch: 0` and reads the field.
*   **`CatalogAdmin.export` and the live-only replica** (RFC decision 30).
*   **Search and facets across products.** `Catalog.search` and
    `Catalog.facets` raise `NotImplementedError`.
*   **`rebuild-inventory`.** The journal-based repair (spec §6.7) covers a
    local crash.
*   **A migrator from 0.11 catalogs.** Users re-ingest (RFC decision 29).
*   **Moving `metadata.duckdb` into DuckLake, encryption-based embargo, and
    the .Stat migration tools** (RFC Non-Goals and §12).

## Open Questions

The review's two resolution rounds settled every finding of substance. They
are recorded in `goldfish_review_report.md` and
`goldfish_review_report_round2.md`.

*   **Cosmetic structure and style edits** (round 2, R15): Status wording,
    bullet markers, the Components diagram's alignment, IDs for the "Settled
    by this design" bullets, and moving file:line detail out of the Technical
    Plan. These do not change what is built.

## Critical User Journeys (CUJs)

### CUJ 1: An analyst builds a catalog on a laptop

*   **Given**: An empty directory `~/stats` and an SDMX registry.
*   **When**: The analyst runs these steps:
    1. `Catalog.create("~/stats")`;
    2. `cat.product("14100287").working_copy()`;
    3. `ingest_sdmx(wc, registry)`;
    4. a query on `wc.view()`;
    5. `wc.seal()`.
*   **Then**: `~/stats` holds `catalog.json`, `CURRENT`, Inventory version 1,
    and one sealed edition under `products/14100287/`.
    `Catalog.open("~/stats").product("14100287").view()` answers the same
    query, and `editions()` lists one `live` edition.

### CUJ 2: A republish followed by a rollback

*   **Given**: Product `14100287` with a live edition A.
*   **When**: The analyst seals edition B built on A, finds an error, and runs
    `CatalogAdmin.open("~/stats").rollback("14100287", reason="…")`.
*   **Then**: A new Inventory version records B as `withdrawn` and A as `live`
    again. A WorkingCopy built on B fails its seal. The error names B, the
    live edition A, and the fix: discard, then start from A.

### CUJ 3: A seal interrupted by a crash

*   **Given**: A WorkingCopy of product P built on live edition A.
*   **When**: The process dies after the manifest is written and before the
    Inventory version is.
*   **Then**: The next `working_copy()` on P finds the journal entry, records
    the sealed edition as `live` before it chooses `previous`, and builds on
    that edition. No edition is recorded twice, and the repair never
    re-records a withdrawn edition.

### CUJ 4: A user upgrades from statcatlib 0.11

*   **Given**: A 0.11 catalog folder (a root-level `meta.duckdb`) or a
    published 0.72 bundle.
*   **When**: The user opens it with the new statcatlib.
*   **Then**: The open is refused, and no file is created or changed. The
    error names the re-ingest steps in `docs/upgrading.md`.

### CUJ 5: Support diagnoses a broken laptop catalog

*   **Given**: A catalog in a synced folder, with a stale WorkingCopy and a
    journal entry left by a crash.
*   **When**: The user runs `statcat doctor ~/stats` and
    `statcat verify-catalog ~/stats`.
*   **Then**: `doctor` writes nothing. It reports the synced-folder risk, the
    WorkingCopy with its start time, and the journal entry. `verify-catalog`
    exits 1 if any check fails, and names the file.

### CUJ 6: An embargoed seal and an explicit release

*   **Given**: A catalog with a live edition A of product P, after
    `CatalogAdmin.open(path).set_embargo(True, reason="…")`.
*   **When**: The analyst seals edition B built on A, then reads
    `Catalog.open(path).product(P).view()`, and then runs
    `CatalogAdmin.open(path).release(P, B, reason="…")`.
*   **Then**: Before the release, the view shows A, and B is visible only
    through `CatalogAdmin.editions(P)` as `embargoed`. After the release and a
    new `view()`, the view shows B, and A is `superseded`.

## Technical Plan (Architecture)

### Decisions

These were settled by the owner while drafting and during the review:

| ID | Decision | Consequence |
|---|---|---|
| P1 | `CatalogAdmin.release(product, edition, *, reason)` and `cancel(product, edition, *, reason)` ship in 2a. | Embargo works end to end through the API on local disk. Releases use the local clock, and no `EmbargoReader` preview exists until Phase 4. |
| P2 | Phase 2a uses spec §18.1's gate: sdmxlib-rest has an adoption issue. GH-81 is that issue, so the gate is met. | Slices merge to main and are released as they finish. Until GH-81 ships, the deployed sdmxlib-rest cannot read catalogs that a released statcatlib writes. `docs/upgrading.md` says so. |
| P3 | Revised in the review. S2 writes no description and needs no new event kind. `create` writes only `catalog.json`, and the first seal writes Inventory version 1. The description, `Catalog.description` and `CatalogAdmin.describe` move to S3. | S2 pins sdmxlib 0.80.0. The `describe` kind is an sdmxlib reader change that GH-81 must pin before S3 is released (sdmxlib issue 1). RFC test D3 holds as written. |
| P4 | `Catalog.create(path, exist_ok=True)` reopens an existing catalog for writing. `Catalog.open` is read-only, and its `working_copy()` raises a named error. | Only a created Catalog writes. A mistyped path with `exist_ok=True` creates a new empty catalog, so `create` logs the path it created at INFO. |
| P5 | Slices: S1, the upgrade, merges to main first and has no release of its own; its changes first ship in S2's release. S2, S2b, S3 and S4 are each released. | One rebuild for users. Main cannot cut a patch release between S1 and S2. |
| P6 | Defer `CatalogAdmin.correct` and `reinstate` to Phase 4. | No format change. The four defects Stage 7 found in `correct` leave 2a's scope. |
| P7 | Prune is the narrow laptop prune described below. | No prune path can touch a WorkingCopy, a journalled edition or a live edition. Roots, leaves and tombstones are kept until Phase 4. |
| P8 | `WorkingCopy` and `EditionView` delegate to one private store class. The class is not split in two. Embedding reuse and the batched seal move to S2b. | `store.py` changes in place. A later refactor can split it with no API change. |

Settled by this design:

- **The spec decides formats, and the RFC decides statcatlib's API.** Where
  they differ (`restore_epoch` in `CURRENT`, 12-digit `seq`, `journal/`), the
  spec and sdmxlib 0.80 hold.
- **statcat `SCHEMA_VERSION` stays `"2.2"`.** 2a changes no statcat DDL. The
  layout check refuses old catalogs before any version check runs, and the
  sdmx schema's move from 2.48 to 2.52 already forces a rebuild.
- **The search index manifest moves to version 3.** Table names change
  meaning: an `edition_id` replaces the generation number (spec §4.1).
  CLAUDE.md requires a `_MANIFEST_VERSION` bump when an existing field
  changes meaning.
- **The root's `extensions.statcat` (S3) carries only catalog-level fields**
  of `CatalogDescription`: identifier, title, description, publisher,
  homepage, language, licence, theme taxonomy, issued, modified. The
  `datasets`, `services` and `records` lists are derived from live editions
  and never stored.
- **`Catalog(":memory:")` is replaced by
  `statcatlib.testing.temporary_catalog()`,** a context manager that creates
  a Catalog in a temporary directory.
- **statcatlib's Inventory writer fsyncs** the staged file and the
  directory, as RFC §4 requires. sdmxlib 0.80 calls no `fsync` in
  `write_journal` or in `seal`'s manifest write. sdmxlib issue 2 asks for
  these fsyncs. Until sdmxlib ships them, a power loss mid-seal can leave an
  empty journal entry or manifest.

### Components

```text
 public                              operational
┌───────────────────────────────┐   ┌───────────────────────────────┐
│ Catalog.open      (read-only) │   │ CatalogAdmin.open             │
│ Catalog.create(exist_ok)      │   │  rollback  release  cancel    │
│  └ Product                    │   │  set_embargo  describe  prune │
│     ├ view()                  │   └───────────────┬───────────────┘
│     ├ editions()              │                   │
│     └ working_copy()          │                   │
└──────┬─────────────┬──────────┘                   │
       ▼             ▼                              ▼
  EditionView    WorkingCopy                  InventoryWriter      statcatlib
  (read-only)    (writable) ── view() ──┐     store/inventory.py
       │             │                  │           │
       └──── _Store (private; _cursor() runs USE) ──┘
───────────────────────┬────────────────────────────┬──────────────────────
                       ▼                            ▼                sdmxlib 0.80
   LocalRegistry.edition(engine=)        sdmxlib.inventory:
   LocalRegistry.build, edition.seal,    record, release, cancel, rollback,
   journal, discard, list_editions       retire, next_root, repairable,
                                         LiveInventory, FullInventory
```

Today's `Catalog` class in `store/store.py` opens storage, writes and
queries. 2a assigns those jobs as follows:

- **`Catalog`** (new, `catalog.py`) is a directory of products. It holds a
  `LiveInventory` and one shared `EditionEngine`. A Catalog from `create`
  also holds an `InventoryWriter`.
- **`_Store`** is today's class under a private name. It has private
  constructors and keeps its query and write methods. Every query it runs
  goes through `_cursor()`, which returns the per-thread cursor after
  running `USE "<catalog_name>".statcat`.
- **`EditionView`** is a read-only wrapper over a `_Store`. It forwards the
  query methods, scopes and projections, and has no write methods.
- **`WorkingCopy`** owns a writable `_Store` over the build registry's
  connection. It forwards the write methods (`put`, `bulk_load`,
  `transaction`, `put_concept`, `add_lineage`, `add_language_alternates`,
  `build_index`) and exposes `registry`. `view()` returns an `EditionView`
  over the same `_Store`, so a caller reads their own writes.
- **`EditionView.registry`** is the sdmxlib read surface (`get`, `query`,
  `obs`). A sealed view returns its `EditionRegistry`, and a WorkingCopy's
  view returns the build registry.
- **`CatalogAdmin`** makes operational changes to the Inventory over a
  `FullInventory`.
- **`InventoryWriter`** is the only code that writes `catalog.json`,
  `inventory/` and `CURRENT`.

### Layout

The layout is spec §4.1's, unchanged. statcatlib adds no file to it.

### The Inventory writer (`store/inventory.py`)

`InventoryWriter(root: Path)` commits Inventory versions on local disk. It
uses sdmxlib.inventory's `encode`, `leaf_ref`, `root_path`, `leaf_path`,
`CatalogDocument`, `Current`, `CATALOG_NAME`, `CURRENT_NAME` and
`decode_current`. It adds only three things: exclusive create, the `CURRENT`
advance, and the retry loop. Its producer is
`Producer(name="statcatlib", version=statcatlib.__version__)`.

- **Exclusive create.** The writer creates a file in five steps:
  1. write a staged file named `.<name>.<pid>.<uuid>.tmp`;
  2. fsync it;
  3. `os.link` it to the target name;
  4. unlink the staged file;
  5. fsync the directory.

  For a root, `FileExistsError` on the link means another writer won. For a
  leaf, it counts as success, because a leaf's name is the hash of its
  bytes.
- **`try_commit(base, leaves, *, retired=, embargo=, extensions=, kind,
  reason)`** makes one attempt. It writes the changed leaves, then creates
  the root from `next_root`. It raises statcatlib's `InventoryConflictError`
  when another writer created that `seq` first. The conformance test
  adapter maps that error to `sdmxlib.inventory.conformance.ConflictError`
  for `Backend.commit`.
- **`commit(change, *, kind, reason)`** is the retry loop around
  `try_commit`. It reads the head with `FullInventory` and calls `change`,
  which applies sdmxlib's pure rules and raises on a refusal. On an
  `InventoryConflictError` it re-reads the head and calls `change` again, so every
  precondition is re-evaluated against the new head: the fast-forward check,
  `if_live`, and the `embargo` header. After 8 consecutive lost races it
  raises `InventoryContentionError`.
- **`CURRENT`.** After the root, the writer writes `CURRENT` through a staged
  file and `os.replace`, but only if its `seq` is higher than the one on
  disk. Two writers can race this replace and leave `CURRENT` one version
  behind. Readers probe forward (spec §6.6), so a lagging `CURRENT` costs
  extra reads and still yields the newest root.
- **The actor.** `event.actor` is `<os user>@<hostname>`, taken from the
  process and never from the caller. `event.reason` is the caller's string.
- **Errors.**
  - `InventoryRuleError(rule, product, edition)` has
    `rule: sdmxlib.inventory.Rule | Literal["lifecycle"]`.
  - It wraps sdmxlib's `TransitionError` as its `__cause__`, except for
    `"lifecycle"`. sdmxlib's `record` relabels a lifecycle instead of
    refusing it (`_rules.py:216-222`), so statcatlib raises that refusal
    itself.
  - `NonFastForwardError`, a subclass, carries the guided message.
- **Conformance.** A test adapter over `try_commit` passes
  `sdmxlib.inventory.conformance.run`. The suite's fault and restore hooks
  live only in the test adapter.

### Catalog, Product and Edition

- **`Catalog.create(path, *, exist_ok=False)`** refuses a legacy layout. On a
  missing or empty directory, it writes `catalog.json` (a UUIDv7
  `catalog_id` and `restore_epoch: 0`) by exclusive create. First it probes
  `os.link` in the directory. A filesystem without hard links, such as exFAT
  or some SMB mounts, raises `IncompatibleCatalogError` naming the filesystem.
  `create` writes no
  Inventory version: the first seal writes version 1, with `embargo: false`.
  `create` takes no embargo argument, because `catalog.json` has no field to
  hold it. Embargo is turned on with `CatalogAdmin.set_embargo` (S3). On a
  catalog with no root, that call writes version 1 with no products. With
  `exist_ok=True` on an existing catalog, `create` opens it for writing and
  writes nothing.
- **`Catalog.open(path)`** refuses a legacy layout, reads `catalog.json`, and
  builds a `LiveInventory` over `local_fetch(path)` at sdmxlib's
  `SCHEMA_VERSION`. It writes nothing: no repair, no DDL, no lake stamp. A
  catalog with `catalog.json` and no root has no products. `refresh()`
  re-reads the Inventory.
- **`Catalog.product(product_id) -> Product`** returns a product with these
  methods:
  - `view(*, embedding_model=None, query_prefix="")` works in three steps:
    1. It hashes the head edition's `manifest.json` and compares the result
       with the head's `manifest_sha256`. A mismatch raises
       `IncompatibleCatalogError`, naming the edition and
       `statcat verify-catalog`.
    2. It calls `LocalRegistry.edition(edition_dir, engine=self._engine)`.
    3. It returns an `EditionView` over the returned `EditionRegistry`.

    A product with no head raises `NoLiveEditionError`, which says "no live
    edition" and names `CatalogAdmin.editions(product)`. An `EditionView`
    stays on its edition after `refresh()` or a rollback. A long-running
    process calls `view()` again to follow the head.
  - `editions()` returns the live entries as `Edition` values, reading
    `previous` from each edition's manifest.
  - `working_copy(*, lifecycle="revisable", resume=False,
    embedding_model=None, document_prefix="", query_prefix="")` returns a
    WorkingCopy on a Catalog from `create`. On a Catalog from `open`, it
    raises the named error of P4.
    - 2a writes data products only (`kind: "data"`). Structure products
      arrive in Phase 4.
    - If the product exists with a different `lifecycle`, it raises
      `InventoryRuleError(rule="lifecycle")` before it builds. A lifecycle
      change is a Phase 4 operation.
  - `rollback()` raises an error naming `CatalogAdmin.rollback` (RFC
    decision 23).
- **`Catalog.seal(working_copies, *, checks=())` (S2b)** is the batched seal.
  It runs the checks and the fast-forward pre-check for every WorkingCopy
  before any sdmxlib seal. At record time, it drops refused products from
  the commit and records the rest in one Inventory version. It returns
  per-product outcomes, as `release_due` does. A crash during a batch can
  leave its products recorded across several versions. On a laptop that is
  acceptable, and the docs say so.
- **`Catalog.description` (S3)** returns the `CatalogDescription` from the
  root's `extensions.statcat`, or `None` before any `describe`.
- **`Edition`** is a frozen attrs value with `edition_id`, `product_id`,
  `previous` and `manifest_sha256`. It has no methods.
- **Moved and refused calls.** `Catalog(path)` and `Catalog(":memory:")`
  raise a `TypeError` naming `Catalog.open`, `Catalog.create` and
  `testing.temporary_catalog`. `Catalog.__getattr__` raises an
  `AttributeError` for every public attribute of `EditionView`, naming
  `product(id).view().<name>`. `Catalog.search` and `Catalog.facets` raise
  `NotImplementedError("not yet supported across products")`.

### The WorkingCopy

**The journal rule.** A product has at most one writer at a time (a laptop
convention, stated in the docs). The writer reads the product's journal with
`read_journal` at four points: in `working_copy()`, on resume, at the start of
`seal`, and in seal step 6. It applies this rule to each entry, against the
newest root:

| Journal entry | Action |
|---|---|
| The newest root names or tombstones the edition | Clear the entry |
| The edition has no `manifest.json` (a crash between the journal and the manifest) | Clear the entry; the edition is an ordinary unsealed WorkingCopy that `resume` or `discard` reaches |
| It has a manifest and no root names it | Record it (`kind: seal`, `previous` and `manifest_sha256` from its manifest), then clear the entry |
| Recording raises `duplicate` with the same `manifest_sha256` | Treat as success; clear the entry |
| Recording raises any other `InventoryRuleError` | Clear the entry, log a WARNING, and leave the orphan for `doctor` |
| `InventoryContentionError` | Keep the entry; the error says the edition is recorded at the next `working_copy()` |
| The entry cannot be read | Raise `JournalError` naming the file and `statcat doctor` |

Every repair that needs recording goes into one commit. Checking only the
newest root is enough: tombstones never expire in 2a (P7), and `correct`
is deferred (P6).

**Starting one.** `product.working_copy()` runs these steps in order:

1. **The repair.** Apply the journal rule.
2. **Find the base.** The head at sdmxlib's `SCHEMA_VERSION` is `previous`,
   or `None` for a new product.
3. **Build or resume.**
   - Call `list_editions(product_dir)` first. If an unsealed edition exists,
     raise `WorkingCopyExistsError` naming it, its creation time,
     `working_copy(resume=True)` and `discard()`.
   - Otherwise call `LocalRegistry.build(product_dir,
     previous=<head edition_id>)`.
   - With `resume=True`, call `build(resume=<unsealed edition_id>)` and run
     the repair again.
4. **Set up the schema.** For a product's first edition, run `create_schema`
   on the build connection. With `previous`, the statcat tables arrive in
   the copied `metadata.duckdb`.

The WorkingCopy takes `catalog_name` from its build registry. statcatlib
attaches no lake of its own. Observations are written through
`wc.registry.write_observations`.

**Embeddings.** A WorkingCopy with an `embedding_model` embeds at seal. If
the carried `embeddings.json` names another model, seal re-encodes every
text and logs that at INFO. A view with no model serves BM25 `search`. Its
`hybrid_search` keeps today's error, which names `view(embedding_model=)`.

**Sealing.** `wc.seal(checks=())` returns an `Edition` and runs these steps:

0. **Repair.** Apply the journal rule.
1. **Checks.** Run each check against `wc.view()`. A check is
   `Callable[[EditionView], Iterable[Finding]]`, and it reads observations
   through `view.registry.obs(flow)`. Any finding raises `SealCheckError`
   listing the findings, and the WorkingCopy stays open.
2. **Fast-forward pre-check.** Re-read the head. If `previous` is no longer
   the live edition, raise `NonFastForwardError` before writing a manifest.
   The error names the withdrawn base, the live edition, and the fix:
   `discard()`, then `working_copy()`.
3. **Search.** If `statcatlib[search]` is installed, rebuild the BM25 tables
   from this edition's rows. `add_bulk` drops the superseded tables after
   its commit. Seal then checks the index manifest: if any table in it does
   not carry this edition's id, it drops that table and writes a manifest
   with no languages. That covers an edition with no rows.
   3b. **Section.** Compute the statcat section with
   `catalog_section(store)` while the store is open.
4. **Close.** Close every cursor and the build registry.
5. **sdmxlib seal.** Call `sdmxlib.edition.seal(edition_dir, product_id=,
   producer=, lifecycle=, extensions={"statcat": section})`. It writes the
   journal entry and then the manifest. If it raises `ManifestError`, for
   example for a coded value outside its codelist, it does so before writing
   the journal entry, so nothing needs repair.
   `working_copy(resume=True)` reopens the WorkingCopy, so the caller can
   fix the data and seal again.
6. **Record.** Commit `record(...)` with `kind: seal`. With embargo off the
   edition is recorded `live`, superseding the old head. With embargo on it
   is recorded `embargoed`. The journal rule then settles the entry.

Seal runs no `check_edition(at="publish")` (spec §11.1 step 5), because
`seal` computes every digest it records. `statcat verify-catalog` runs that
check on demand.

After a seal, every method on the WorkingCopy, and on views taken from it,
raises `WorkingCopyClosedError`. `discard()` calls `sdmxlib.edition.discard`
and closes the object the same way.

**A refusal after the manifest.** Steps 2 and 6 read the head separately, so
a rollback can land between them. In that case:

1. The record in step 6 raises `NonFastForwardError` after the edition is
   sealed.
2. `sdmxlib.discard` refuses to delete a sealed edition.
3. Under the journal rule, statcatlib clears the edition's journal entry
   (so the repair never records it), logs a WARNING, and re-raises the
   error.

The orphaned edition stays on disk, and `statcat doctor` reports it. No 2a
code deletes it (see Future Improvements).

### The EditionView read path

- **One engine per Catalog.** A sealed edition opens through
  `LocalRegistry.edition(edition_dir, engine=self._engine)`. That call
  checks the manifest and refuses another store schema with
  `StoreSchemaError`. It also wires the lake to `<prefix>__tables` and
  returns the `EditionRegistry`, whose `catalog_name` the view uses.
- **`USE` per cursor.** `_Store._cursor()` runs
  `USE "<catalog_name>".statcat` on the per-thread cursor. It replaces
  `SET search_path = 'statcat'` in `_Store`'s cursor and per-thread cursor
  paths, and in `create_schema` and `open_schema_readonly`. Every direct use
  of `connection` goes through `_cursor()`: about 27 sites in `scopes.py`
  and five in `store.py` (`CHECKPOINT`, `code_map` and three more).
- **Name resolution.** Unqualified and two-part names (`statcat.dataset`,
  `sdmx.code`) resolve inside the `USE`d catalog. A test attaches two
  editions and checks that both kinds of name, a scope, `batch_labels`,
  `code_map` and `annotation_fingerprint` all resolve to each edition's own
  catalog.
- **Schema guards.**
  - On the `EditionView` path, sdmxlib's `edition()` refuses a mismatched
    store schema, so statcatlib's `_refuse_sdmx_schema_rebuild` is dropped
    there.
  - On the WorkingCopy path, `build()` enforces the store schema.
  - The statcat check (`_refuse_statcat_schema_mismatch`, via
    `schema_exists` and `get_schema_version`) stays on both paths.
- **Catalog-scoped queries.** Every `information_schema` query filters by
  `table_catalog`. That covers `schema_exists`, `get_schema_version`,
  `check_catalog`, `annotation_fingerprint` and four more queries in
  `store.py`.
- **Removed paths.** `from_connection`, the lake-stamp repair and
  `azure_credentials` are deleted. sdmxlib's attach uses the manifest's
  `data_path`.
- **The search index.** It opens from `<edition_dir>/search/` read-only.

### Search

- **Table names.** Tables are named `search_<lang>__<edition_id without
  hyphens>`. `<lang>` comes from `sdmxlib.tag_alias`, which replaces
  `lance._language_slug`. `index.json` moves to manifest version 3. Version 2
  indexes and the legacy single-table index (pre-pinaxlib v5.23) exist only
  in refused layouts, so their read paths are deleted.
- **Carrying forward.** `build(previous=)` carries `search/` as a lazy-tier
  file set (GH-258). Seal rebuilds every BM25 table and drops the previous
  edition's tables. Spec §4.1 has the producer drop orphaned tables before
  it seals.
- **Embedding reuse (S2b).** Each search table gains a `text_sha256` column,
  which hashes the model name, the prefix and the embedded text. Before seal
  drops the carried tables, `build_index` reads `(text_sha256, vector)` from
  them and encodes only the texts whose hash is missing. RFC test D21 counts
  the `encode` calls.

### The statcat sections

- **Manifest `extensions.statcat`, format 3:** `{format: 3, kind: "data",
  statcatlib_version, datasets, search}`. This keeps format 2's fields and
  adds `kind`. `check_catalog` takes the 0.80 `Check` signature
  `(root, manifest, EditionRegistry, profile)` and runs under
  `check_edition(..., extensions={"statcat": check_catalog})`.
  `catalog_section` takes an open store, not a path.
- **Root `extensions.statcat` (S3):** `{format_version: "3", description:
  {...}}`. `describe` writes it, and every later commit carries it unchanged
  through `next_root`'s default.

### CatalogAdmin

`CatalogAdmin.open(path)` refuses legacy layouts and builds a `FullInventory`
and an `InventoryWriter`. No attribute of `Catalog`, `Product` or
`EditionView` returns a `CatalogAdmin`. `editions(product)` only reads: it
returns every entry with its state, for picking a rollback target. Every
other method requires `reason` and makes one commit:

| Method | sdmxlib rule | `event.kind` | Slice |
|---|---|---|---|
| `rollback(product, *, to=None, if_live=None, reason)`; `if_live` defaults to the head when the call starts; passes sdmxlib's `SCHEMA_VERSION` (rolling back another schema's head is a Phase 4 operation) | `rollback` | `rollback` | S3 |
| `release(product, edition, *, reason)` | `release(explicit=True)` | `release` | S3 |
| `cancel(product, edition, *, reason)` | `cancel` | `withdraw` | S3 |
| `set_embargo(on, *, reason)` | (root header) | `embargo-change` | S3 |
| `describe(description, *, reason)` | (root extension) | `describe` | S3 |
| `prune(*, older_than, reason, dry_run=True)` | `retire` | `retire` | S4 |

`release` uses the local clock, so a laptop with a skewed clock releases
early or late.

**Prune (P7).** `prune` takes one `older_than` window and does three things:

1. It retires `superseded` and `withdrawn` entries whose `state_changed_at`
   is older than the window, in one `retire` commit.
2. It deletes the directories of editions tombstoned for longer than the
   window.
3. It deletes a file under a product's `data/` only when all three of these
   hold:
   - no `manifest.json` on disk under the product lists the file;
   - the product has no unsealed edition and no journal entry;
   - the file's mtime is older than the window.

Prune deletes no roots or leaves, expires no tombstones, and sweeps no
unrecorded editions. A just-sealed edition is never tombstoned, so prune
cannot race a seal into deleting a live edition. With `dry_run=True` (the
default), prune returns its report and deletes nothing. A real run returns
the same report and logs every deleted path at INFO. If a deletion fails,
for example on Windows when another process has the edition attached, prune
records the path in `PruneReport.failed`, continues, and retries the path on
its next run.

### Tools

- **`statcat verify-catalog PATH`** reads `FullInventory`. For every entry
  that is not retired, it checks `manifest_sha256`, then runs
  `check_edition(at="publish", extensions={"statcat": check_catalog})`, the
  profile that checks every recorded digest, `data/` files included (spec
  §4.6). It exits 0 when clean, 1 on any finding, and 2 when the catalog
  cannot be read.
- **`statcat doctor PATH`** writes nothing. It reports:
  - the layout (catalog, legacy, or unknown);
  - `catalog.json`;
  - `CURRENT` against the newest root found by probing;
  - root and leaf validation;
  - unsealed editions with their creation times (`list_editions`);
  - journal entries, including empty ones;
  - sealed editions that no version names;
  - leftover `.tmp` files in `inventory/`;
  - a synced-folder warning when the path is under a Dropbox, OneDrive,
    iCloud Drive or Google Drive folder.
- **Changed commands.**
  - `statcat build-index CATALOG PRODUCT` refuses a path with no
    `catalog.json`. Otherwise it resumes the product's WorkingCopy, and
    refuses when there is none.
  - The `bundle` group (`check`, `explain`, `rebind`, `attach-cmd`) is
    deleted in S1. `verify-catalog` replaces `check`, and `sdmxlib edition
    check | copy | files` replaces `explain` and `attach-cmd`. `rebind` has
    no replacement, because editions resolve `data_path` relative to their
    own directory.

### Legacy layouts

`Catalog.open`, `Catalog.create` and `CatalogAdmin.open` raise
`IncompatibleCatalogError`, and write no file, for a directory that has any
of:

- a root-level `meta.duckdb`;
- a `bundle.manifest.json`;
- files but no `catalog.json`.

The message names the re-ingest section of `docs/upgrading.md`.

### The upgrade from sdmxlib 0.72

| sdmxlib | Change | statcatlib sites | Work |
|---|---|---|---|
| 0.73 | Bundles become `<product>/<edition>/` over `data/`; a manifest is written once | `store.py` folder layout, `testing.py`, `tests/test_bundle_manifest.py` | S1: the test helper wraps the builder. S2: the WorkingCopy and seal |
| 0.73 | `check_bundle(references=)` becomes `at=`; a `Check` takes a 4th argument | `bundle.py` (`check_catalog`, `check_bundle`), `cli/__init__.py` bundle group, 3 test files | `check_catalog` signature; `verify-catalog` |
| 0.75 | sdmxlib moves to httpx2 and no longer installs httpx | `sources/ckan.py` keeps its own httpx | None; two HTTP stacks are installed |
| 0.75 | `seal` refuses coded values outside their codelist with `ManifestError`, before the journal entry | S2 seal step 5 | Resume and seal again |
| 0.79 | Language tags are lowercased and validated (`InvalidTagError`) | every public `lang` parameter in `store.py`, `scopes.py` and `query.py`; `ckan.py` `_lang_map` | `sdmxlib.normalize_tag` at each entry point; CKAN follows language-tags decision 2 |
| 0.79 | `InternationalString` maps tags to `LocalisedString` | `store.py:744` (`_istring_to_map`), `store.py:1186` (`_contact_id`); the test assertions the istring audit reports | `as_dict()`; test edits |
| 0.80 | `sdmxlib.bundle` becomes `sdmxlib.edition`; `write_manifest` becomes `seal`; `meta.duckdb` and `lake.ducklake` become `metadata.duckdb` and `tables.ducklake`; `synthetic_bundle` becomes `synthetic_edition` | `bundle.py`, `testing.py`, `cli/__init__.py`, about 40 test sites | Rename, or delete with the data-path code |
| 0.80 | Store schema 2.52 | `_refuse_sdmx_schema_rebuild` imports the constant | None; every catalog is rebuilt |

0.74, 0.76, 0.77 and 0.78 touch no statcatlib code.

### Issues filed on sdmxlib

Filed on 2026-11-01. The rest of this design refers to them as sdmxlib
issues 1, 2 and 3.

1. StatCat-Suite/sdmxlib#334: add `describe` to `event.kind` in Inventory
   1.0, as a reader change. sdmxlib-rest GH-81 must pin it before
   statcatlib S3 is released (P3).
2. StatCat-Suite/sdmxlib#335: expose one public
   `create_exclusive(path, data)` that fsyncs the staged file and the
   directory, and use it in `write_journal` and in `seal`'s manifest write. statcatlib's writer switches to it when it ships, and no
   slice waits on it.
3. StatCat-Suite/sdmxlib#336: add
   `LocalRegistry.edition(..., manifest_sha256=)`, so the attach checks the
   Inventory's digest, as pods need to.

## Alternatives Considered

The RFC's three brainstorm rounds rejected the larger alternatives (RFC
§14): standalone and managed modes, lock files, a flat Inventory list, a
stored live projection, and the DCAT record in `catalog.json`. This section
covers the alternatives that bear on the 2a design, and the options the
owner turned down.

### Alternative A: Keep `Catalog` as the query class and add products beside it

*   **Pros**: Existing callers of `Catalog(path).query(...)` keep working, and
    the `Catalog` class in the 7,395-line `store.py` keeps its name.
*   **Cons**: One class would open storage, write, query and route products.
    A sealed edition would carry write methods that must each refuse.
*   **Reason for Rejection**: The RFC names `Catalog` as the `dcat:Catalog`
    of products (RFC decision 2), and encapsulation is its first goal. Under
    P8, `EditionView` has no write methods. A write on a sealed edition is
    then impossible without a guard on each method. Today `add_lineage` has
    no read-only guard, which shows how a per-method guard gets missed.
    Splitting the class physically was the third option. P8 rejected it as
    too much risk for the format slice.

### Alternative B: Fully qualified SQL instead of `USE` per cursor

*   **Pros**: Every statement names its catalog, and nothing depends on
    cursor state.
*   **Cons**: About 133 unqualified table names in `store.py`, plus the
    two-part names in `query.py` and `scopes.py`, would be rewritten. Every
    future query would need the catalog name threaded through.
*   **Reason for Rejection**: `USE "<catalog>".statcat` gives the same
    resolution. Its cost is the four `SET search_path` sites plus about 35
    direct `connection` uses, all routed through `_cursor()`. sdmxlib's
    `EditionEngine.cursor` already uses `USE` for `sdmx`. A test pins the
    resolution with two editions attached.

### Alternative C: Reuse sdmxlib's test writer

*   **Pros**: `tests/support/inventory.py` already passes the conformance
    suite.
*   **Cons**: It is test code outside the package. It does not fsync, and it
    writes `CURRENT` in place with no forward-only check.
*   **Reason for Rejection**: The spec makes statcatlib the writer (RFC
    decision 8). Copying the helper would ship a writer with no fsync and an
    in-place `CURRENT` write.

### Alternative D: A broad crash repair

*   **Pros**: It recovers every sealed but unrecorded edition, whether or not
    it has a journal entry.
*   **Cons**: A withdrawn edition that was later retired still has a manifest
    naming the live edition as `previous`, so a scan would record it again.
*   **Reason for Rejection**: Re-releasing withdrawn data is the failure the
    journal-based repair exists to prevent (RFC §6, spec §6.7).

### Alternative E: Other answers to P1 to P8

The Decisions table records each choice. These options were rejected:

*   **P1.** Two alternatives were rejected:
    - Refusing embargo until Phase 4 would leave RFC test D17 runnable only
      against a hand-built fixture.
    - Shipping `set_embargo` without `release` and `cancel` would leave
      embargoed editions with no way to go live or be withdrawn.
*   **P2.** Two alternatives were rejected:
    - A long-lived branch until GH-81 ships would need repeated rebases onto
      main.
    - Merging to main and releasing nothing until GH-81 ships would block
      every statcatlib release, fixes included.
*   **P3.** New `create` and `describe` kinds in S2 were rejected:
    - Readers on 0.80.0 reject an unknown `event.kind`.
    - S2 would wait on an sdmxlib patch.
*   **P4.** Two alternatives were rejected:
    - With `Catalog.open(writable=True)`, read-only access would depend on
      callers omitting a flag.
    - With `working_copy()` on any Catalog, a read-only mount would fail at
      the first write, not at open.
*   **P6.** Keeping `correct` meant fixing four defects for a server tool
    that 2a has no server to use.
*   **P7.** Two alternatives were rejected:
    - Copying the server GC deletes live WorkingCopy files and races seals.
    - Deferring prune leaves laptop disk use growing with every republish.

### Alternative F: A Delta table as the Inventory

*   **Pros**: Delta's log, checkpoints and conflict detection are
    established. Fabric and Spark could read it directly.
*   **Cons**: It needs a Delta writer on laptops. Its conflict rules apply
    per table, so fast-forward per (product, `schema_version`) would still
    have to be built on top.
*   **Reason for Rejection**: The RFC keeps it as the escape route (RFC §3),
    to be reviewed when the Inventory needs checkpoints or compaction.
    sdmxlib's conformance suite runs against a Delta prototype to keep the
    route open (RFC decision 31).

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Durability.** statcatlib's writer fsyncs before every link and every
    directory change. Until sdmxlib fsyncs its journal and manifest writes,
    a power loss mid-seal can leave an empty journal entry or manifest, and
    `statcat doctor` reports it.
*   **Rollout.** Users re-ingest every catalog, because sdmx schema 2.52 and
    the new layout both refuse old catalogs. Until sdmxlib-rest GH-81 ships,
    the deployed sdmxlib-rest cannot read catalogs that a released
    statcatlib writes (P2). `docs/upgrading.md` states both.
*   **Write volume.** In S2b, a laptop bulk ingest of N products uses
    `Catalog.seal([...])`, which writes N leaves and one root.

### Risks and limits

| Risk | Mitigation |
|---|---|
| On local disk, the API enforces embargo, and any process that can read the files sees embargoed editions, as do backups and copies | Stated in `docs/catalog-format.md` (RFC D5/D18 test). Storage identity enforces embargo from Phase 4 |
| `event.actor` is the OS user and host, unauthenticated | Stated in the docs. Phase 4 takes the actor from the storage token's object id |
| Synced folders and network shares are unsupported | `doctor` warns. `create` refuses a filesystem without hard links |
| `CURRENT` lags after two writers race | Readers probe forward (spec §6.6) |
| A late refusal leaves a sealed edition no version names | The journal rule clears its entry and logs a WARNING. `doctor` reports it |
| A power loss before sdmxlib's fsyncs ship leaves an empty journal entry or manifest | sdmxlib issue 2. `doctor` reports empty files, and the journal rule raises `JournalError` |
| The deployed sdmxlib-rest cannot read 0.12 catalogs until GH-81 ships | P2. Stated in `docs/upgrading.md` |
| A long-running process keeps reading an edition after a rollback | `view()` stays on its edition. The process calls `view()` again to follow the head |

--------------------------------------------------------------------------------

### Future Improvements

*   **`CatalogAdmin.correct` and `reinstate`** (P6), with the Phase 4 backup
    and restore runbook.
*   **The server GC:** pruning roots and leaves, and expiring tombstones,
    with a repair that checks every retained root.
*   **A discard for sealed but unrecorded editions:** an sdmxlib option to
    delete a sealed edition that no Inventory version names, for the orphan
    a late fast-forward refusal leaves.
*   **`statcat doctor --bundle`:** a support zip of the report,
    `catalog.json`, `CURRENT` and the roots, once support asks for these
    files.
*   **Renaming `bundle.py`,** which no longer concerns bundles.
*   **Splitting `_Store`** into read and write classes (P8).

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Slices

Line numbers refer to commit 833005d. Find each site by its symbol; use the
number only as a hint.

| Slice | Content | Released | Waits on |
|---|---|---|---|
| S1 | sdmxlib 0.80 upgrade; data-path and bundle-publish code deleted | No; ships in S2's release | nothing |
| S2 | Inventory writer, `Catalog`, `WorkingCopy`, `EditionView`, single seal, refusals, BM25 rebuilt at each seal | Yes, 0.12.0 | S1 |
| S2b | Embedding reuse; batched seal | Yes | S2 |
| S3 | `CatalogAdmin`: rollback, release, cancel, `set_embargo`, `describe` | Yes | S2; sdmxlib issue 1 pinned by GH-81 |
| S4 | Narrow prune, `verify-catalog`, `doctor`; RFC correction | Yes | S2 (`verify-catalog`, `doctor`); S3 (prune) |

Each slice is a feature branch and a PR, merged with a merge commit after
`just ci` passes. Breaking commits use `feat!:` or `fix!:`. Releases go
through `just release-1-preview` to `release-5-pypi`. S2 releases as 0.12.0,
and S2b, S3 and S4 each release as a minor version.

- **Rollback.** Revert the slice's merge commit on main, and users pin the
  previous minor version. 0.11 cannot read catalogs written by 0.12, so a
  user who downgrades past S2 re-ingests.
- **A hotfix while S1 is unreleased.** The release recipes work only from
  main, so a patch to 0.11 is a manual branch from tag `0.11.0`, built and
  published by hand. Keep the window between merging S1 and releasing S2
  short.

CHANGELOG entries come from `just release-2-changelog` when each release is
cut. Slice PRs do not edit `CHANGELOG.md`. The breaking-change detail goes
in `docs/upgrading.md`, which each slice updates.

Each slice also runs `uv run python tools/count_any.py --write` and commits
`tools/any_baseline.txt` when the count changes. It runs `just docs-build`
before opening its PR.

### Slice S1: the sdmxlib 0.80 upgrade

#### 1. `pyproject.toml` (Modify)

*   **Scope**: Dependencies.
*   **Changes**: Pin `sdmxlib>=0.80.0,<0.81`. Replace the comment above it
    with `# <0.81: every suite library is pre-1.0, so a minor bump may break
    (GH-67).` Keep `httpx` for CKAN.

#### 2. `src/statcatlib/store/store.py` (Modify)

*   **Scope**: The store.
*   **Changes**:
    - Change `_istring_to_map` (line 744) to return `s.as_dict()`.
    - Change `_contact_id` (line 1186) to hash
      `sorted(contact.name.as_dict().items())`. The JSON is byte-identical to
      0.11's, so contact ids do not change.
    - Call `sdmxlib.normalize_tag(lang)` once on entry in every public method
      that takes `lang`. An invalid tag raises `sdmxlib.InvalidTagError`.
    - In `open()`, attach `ducklake:<dir>/lake.ducklake AS lake (DATA_PATH
      '<dir>/parquet/')`, adding `READ_ONLY` when `read_only=True`. Keep
      `meta.duckdb` and `lake.ducklake` as the folder-mode names in S1.
    - Delete these:
      - `_attach_options` and `_resolve_data_path`;
      - `_colocated_data_path`, which is the lake-stamp repair (lines
        3116–3154);
      - the `data_path` check in `_open_connection` (lines 3050–3052).
        The in-memory branch around it stays until S2.
      - the `data_path=` and `azure_credentials=` parameters of
        `Catalog.__init__`.
    - Give `from_connection` a private `search_dir: Path | None = None`
      argument. When it is set, `_init_lance` uses it in place of
      `_bundle_dir / "search"`, so `build_index` and `catalog_section`
      work on a store opened over a builder's connection.
    - Remove `Embedded`, `normalize_lake_data_path` and
      `read_current_data_path` from the `bundle` import at line 38.

#### 3. `src/statcatlib/store/scopes.py`, `src/statcatlib/query.py` (Modify)

*   **Scope**: Queries.
*   **Changes**: Every `.lang()` setter, public method and filter constructor
    that takes `lang` calls `sdmxlib.normalize_tag` once on entry. That
    includes `has_keyword`, `title_contains`, `description_contains`,
    `MapCondition` and `MapFilter`. Internal helpers trust the value they
    receive.

#### 4. `src/statcatlib/sources/ckan.py` (Modify)

*   **Scope**: CKAN ingest.
*   **Changes**: `_lang_map` (lines 333–339) applies language-tags decision 2
    (`docs/design/language-tags.md`):
    1. strip each tag and replace `_` with `-`;
    2. call `sdmxlib.normalize_tag`;
    3. drop a tag that raises `InvalidTagError`, count it per source, and
       log at WARNING;
    4. when two tags normalise to the same value, keep the first text and
       log a WARNING.

    Lines 358 and 393 read through `_lang_map` and need no change.

#### 5. `src/statcatlib/bundle.py` (Modify)

*   **Scope**: The statcat section.
*   **Changes**:
    - Delete lines 105–591 (the data-path policies, `BundleInfo`,
      `inspect_bundle`, `normalize_lake_data_path`, `read_current_data_path`
      and `open_bundle`), plus `publish_bundle` and `check_bundle`.
    - Delete the module docstring on data paths (lines 1–38), and
      `_LAKE_FILENAME` and `_REMOTE_SCHEMES` (lines 96–102).
    - Replace the `sdmxlib.bundle` imports (lines 53–64) with imports of
      `Finding`, `Manifest`, `MANIFEST_NAME`, `Overlay` and `OverlayEntry`
      from `sdmxlib.edition`, the names the kept code uses.
    - Trim `__all__` (lines 74–93) to `STATCAT_SECTION`,
      `STATCAT_SECTION_FORMAT`, `catalog_section`, `check_catalog` and
      `overlay_affects_search`.
    - Change `catalog_section` to take an open `Catalog` instead of a path.
    - Change `check_catalog`'s signature to the 0.80 `Check` signature
      `(root, manifest, EditionRegistry, profile)`.
    - Reword its finding texts at lines 850 and 856 so they no longer name
      `meta.duckdb` or `publish_bundle`.

#### 6. `src/statcatlib/testing.py` (Modify)

*   **Scope**: Test helpers.
*   **Changes**: Give `synthetic_catalog` the signature
    `synthetic_catalog(path, shape, *, seed=0, index=True) ->
    SyntheticCatalog`. `SyntheticCatalog` is an attrs record with `cat`,
    `reg` and `edition_dir`. The function runs these steps:
    1. call `reg = synthetic_edition(path, shape, seed=seed, seal=False)`
       and set `edition_dir = reg.edition_dir`;
    2. call `cat = Catalog.from_connection(reg.connection,
       search_dir=edition_dir / "search")`;
    3. run `prepare_sdmx` and `put`;
    4. when `index=True`, call `build_index`;
    5. return the record with `cat` and `reg` open.

    The caller closes `cat`, then `reg`, and only then may call
    `sdmxlib.edition.seal` (sdmxlib `testing.py:196-199`). Replace the
    `sdmxlib.bundle` imports of `Producer` and `synthetic_bundle`, and the
    `Manifest` return type. Remove the `publish_bundle` call.

#### 7. `src/statcatlib/cli/__init__.py` (Modify)

*   **Scope**: The `statcat` CLI.
*   **Changes**: Delete the `bundle` group (`check`, `explain`, `rebind`,
    `attach-cmd`). Keep the folder-mode form of `build-index`.

#### 8. `src/statcatlib/__init__.py` (Modify)

*   **Scope**: Public API.
*   **Changes**: Remove `BundleDataPathError`, `Embedded`, `Colocated`,
    `At`, `Auto`, `BundleInfo`, `inspect_bundle`, `normalize_lake_data_path`,
    `open_bundle` and `read_current_data_path` from the imports and from
    `__all__`. The section helpers stay reachable as `statcatlib.bundle.*`
    and are not top-level exports, as today.

#### 9. Tests (Create, Modify or Delete)

*   **Scope**: The S1 test suite.
*   **Delete**:
    - `tests/test_bundle.py`, `tests/test_catalog_path_spelling.py` and
      `tests/test_bundle_spec.py`;
    - the four `data_path` tests in `test_catalog_folder.py` (lines 93–131);
    - the `publish_bundle` and `check_bundle` cases in
      `test_statcat_schema_guard.py` (149–159) and
      `test_index_version_guard.py` (111–128);
    - from `test_bundle_manifest.py`:
      - the `_published` fixture (36–46);
      - TestPublish (61–92), TestCatalogChecks (108–178), TestCLI (180–199)
        and TestIndexAnnotations (216–275);
      - the `open_bundle` tests (98, 103);
      - the `sdmxlib.bundle` and `synthetic_bundle` imports (13, 23).
*   **Keep**: from `test_bundle_manifest.py`, keep TestAnnotationFingerprint
    (277–323) and the `overlay_affects_search` tests (325–344), with their
    imports taken from `sdmxlib.edition`.
*   **Move to S2**: the TestReadable and TestGuard tests in
    `test_index_version_guard.py`, which open the old published layout. S1
    marks them `pytest.mark.skip(reason="rewritten in S2")`, and S2 item 24
    rewrites them.
*   **Rewrite**:
    - `test_shared_connection_idle.py` uses the `SyntheticCatalog` record's
      `cat`, with `index=True`.
    - `test_bundle_manifest.py` gains one `check_catalog` test. The test:
      1. computes `section = catalog_section(rec.cat)`;
      2. closes `rec.cat` and then `rec.reg`;
      3. seals `rec.edition_dir` with `extensions={"statcat": section}`;
      4. runs `check_edition` with the statcat check.
*   **Rewrite for `LocalisedString`**:
    1. Run the suite once with `SDMXLIB_ISTRING_AUDIT=1` and
       `SDMXLIB_ISTRING_AUDIT_OUT=<file>` (sdmxlib `istring.py:559-579`).
    2. Change each assertion the audit reports to compare `.get(tag).text`,
       or `as_dict()`, with the expected string.

    The files include `test_model.py`, `test_public_api.py`,
    `test_query_builder.py`, `test_query_entities.py`, `test_scopes.py`,
    `test_skos.py`, `test_sources_sdmx.py`, `test_sources_ckan.py`,
    `test_store.py`, `test_sdmx_bridge.py`, `test_store_crud.py`,
    `test_codelist_policy_and_concepts.py` and `test_hybrid_search.py`.
*   **Rewrite for the sdmxlib 0.80 renames**:
    `test_sdmx_schema_guard.py`, `test_statcat_schema_guard.py`,
    `test_index_version_guard.py`, `test_cli_build_index.py` and
    `test_bundle_manifest.py` use the 0.80 names and expect sdmxlib store
    schema 2.52.
*   **Extend**: `test_sources_ckan.py` gains three cases:
    - `fr_CA` is stored as `fr-ca`;
    - a malformed tag is dropped with a WARNING, and the other languages
      are kept;
    - an `EN`/`en` pair keeps the first text, with a WARNING.
*   **Create** `tests/test_lang_normalisation.py`
    (`pytestmark = pytest.mark.unit`). It checks these cases:
    - `lang="fr-CA"` finds a label stored under `fr-ca`;
    - `lang="FR"` finds `fr`;
    - `lang="fr_CA!"` raises `InvalidTagError`;
    - `.lang("EN")` behaves like `lang="en"`.

#### 10. Docs and agent context (Modify or Delete)

*   **Scope**: The S1 documentation.
*   **Delete**: `docs/api/bundles.md` and its `mkdocs.yml` entry.
*   **Modify**:
    - Remove the link at `docs/bundle-format.md:355`. Add a banner at the
      top of that page: publishing is unavailable until S2, and the page
      describes the 0.11 format (`publish_bundle`, `statcat bundle check`,
      `tests/test_bundle_spec.py`) until S2 rewrites it.
    - Remove the sentence that links to `../api/bundles.md` from
      `docs/reference/bundle.md:3`. The page's `::: statcatlib.bundle`
      directive documents the module as it remains.
    - Remove the `data_path` and `parquet_dir` text at
      `docs/api/store.md:22-25,84-89` and the `parquet/` placement text at
      `docs/tutorial.md:25-26`.
    - In `CLAUDE.md`, update the sdmxlib cap line and replace the "Bundle
      contract" paragraph.
    - Update the root `EGM.md` (lines 54, 64), `src/statcatlib/EGM.md`
      (lines 51, 117, 141) and `src/statcatlib/cli/EGM.md` (line 106).
*   **Create**: `docs/upgrading.md` and its `mkdocs.yml` entry. It records
    S1's breaking changes:
    - the removed bundle API, `data_path` and `azure_credentials`, with a
      note that users with a remote `data_path` stay on 0.11 until Phase 4;
    - the rebuild for schema 2.52;
    - lowercase tags and `InvalidTagError`.

### Slice S2: the Inventory writer and the public model

#### 11. `src/statcatlib/store/inventory.py` (Create)

*   **Scope**: Inventory writer.
*   **Changes**: Create `InventoryWriter(root)` with these members:
    - `head()`;
    - `try_commit(base, leaves, *, retired=, embargo=, extensions=, kind,
      reason) -> Root`, which raises `InventoryConflictError`;
    - `commit(change, *, kind, reason) -> Root`;
    - `write_catalog_json()`.

    Give it these internal helpers:
    - `_create_exclusive(path, data)`, which writes a staged file, fsyncs it,
      `os.link`s it to the target, unlinks the staged file and fsyncs the
      directory;
    - `_advance_current(root, data)`.

    `commit` raises `InventoryContentionError` after 8 consecutive lost
    races. Add `InventoryConflictError`. `InventoryRuleError(rule, product,
    edition)` takes `rule: Rule | Literal["lifecycle"]` and an optional
    `TransitionError` cause, and `NonFastForwardError` subclasses it.
    `_actor()`
    returns `<user>@<host>`. The producer is
    `Producer(name="statcatlib", version=statcatlib.__version__)`. Use the
    sdmxlib.inventory helpers named in the Technical Plan.

#### 12. `src/statcatlib/catalog.py` (Create)

*   **Scope**: Public model.
*   **Changes**:
    - Create `Catalog` with these members:
      - `create(path, *, exist_ok=False)`, which probes `os.link`, and
        `open(path)`;
      - `refresh()`;
      - `product(id)` and `products() -> list[str]`;
      - `close()` and the context-manager protocol.
    - Create `Product` with these members:
      - `view(*, embedding_model=None, query_prefix="")`, which checks the
        manifest's sha256 and then calls `LocalRegistry.edition(edition_dir,
        engine=)`;
      - `editions()`, which reads `previous` from each manifest;
      - `working_copy(*, lifecycle, resume, embedding_model,
        document_prefix, query_prefix)`, which refuses a lifecycle change
        before it builds;
      - `rollback()`, which raises an `AttributeError` naming
        `CatalogAdmin.rollback`.
    - Create `Edition` as a frozen attrs value, and `NoLiveEditionError`.
    - `Catalog.__init__` raises a `TypeError` naming `Catalog.create`,
      `Catalog.open` and `testing.temporary_catalog`.
    - `Catalog.__getattr__` raises an `AttributeError` for every public
      attribute of `EditionView`, naming its replacement.
    - Add `_refuse_legacy_layout(path)`, shared with `CatalogAdmin`.

#### 13. `src/statcatlib/store/working_copy.py` (Create)

*   **Scope**: Writable edition.
*   **Changes**:
    - Create `WorkingCopy`, which owns a writable `_Store` over
      `LocalRegistry.build`. It runs these steps from the Technical Plan:
      - the journal rule, at creation, on resume and at the start of
        `seal`;
      - the `list_editions` check before `build`;
      - `create_schema` on a product's first edition.
    - Forward these write methods to the `_Store`: `put`, `bulk_load`,
      `transaction`, `put_concept`, `add_lineage`,
      `add_language_alternates` and `build_index`.
    - Expose `registry`.
    - Add `view()`, `discard()` and `seal(checks=()) -> Edition`. `seal`
      runs the steps in the Technical Plan: the repair, checks,
      fast-forward, search, the section, close, the sdmxlib seal, and the
      record.
    - Add the errors `WorkingCopyExistsError`, `WorkingCopyClosedError`,
      `SealCheckError` and `JournalError`.

#### 14. `src/statcatlib/store/store.py` (Modify)

*   **Scope**: The private store.
*   **Changes**:
    - Rename the `Catalog` class to `_Store`. Make its constructor private:
      `_over(cursor_factory, *, catalog_name, search_dir, writable,
      registry, embedding_model, document_prefix, query_prefix)`.
      `writable=False` makes every write method raise.
    - Add `_cursor()`, which returns the per-thread cursor after running
      `USE "<catalog_name>".statcat`. It replaces the `SET search_path`
      calls at lines 3445 and 3753.
    - Route `CHECKPOINT` (line 3839) and the direct `connection` uses at
      lines 7167, 7192, 7219 and 7251 through `_cursor()`.
    - Delete `open`, `_open_connection`, `from_connection`, `connection`,
      `cursor`, `bundle_dir` and the in-memory mode.
    - Drop `_refuse_sdmx_schema_rebuild` on the read path.
    - Filter the `information_schema` queries at lines 1739, 4084, 4251,
      4344 and 4659 by `table_catalog`.
    - Add `EditionView`, the read-only wrapper that forwards the query
      methods, scopes and projections, and exposes `registry` (the sdmxlib
      read surface: `get`, `query`, `obs`). Every public attribute of
      `EditionView` is named in `Catalog.__getattr__`'s guided errors.

#### 15. `src/statcatlib/store/scopes.py` (Modify)

*   **Scope**: Scopes.
*   **Changes**: Replace every `self._store.connection` (27 sites) with
    `self._store._cursor()`.

#### 16. `src/statcatlib/store/__init__.py`, `query.py`, `cli/__init__.py`, `sources/*.py`, `store/lance.py` (Modify)

*   **Scope**: Name references.
*   **Changes**:
    - `store/__init__.py` re-exports `EditionView` and `WorkingCopy`.
    - In the other modules, change each type reference to the store from
      `Catalog` to `EditionView` or `WorkingCopy`, whichever the call needs.
    - `ingest_sdmx`, `ingest_data` and `ingest_ckan` take a `WorkingCopy`,
      and raise a `TypeError` naming `working_copy()` when they are given an
      `EditionView`.
    - `sources/sdmx.py:311` calls `wc.registry.write_observations`.

#### 17. `src/statcatlib/store/schema.py` (Modify)

*   **Scope**: DDL.
*   **Changes**:
    - `create_schema` and `open_schema_readonly` take a `catalog_name` and
      run `USE` (lines 374, 399). `catalog_name` defaults to `None`, which
      runs no `USE`, so `test_schema.py`'s direct `create_schema(c)` calls
      keep working.
    - `schema_exists` and `get_schema_version` filter by `table_catalog`.
    - `SCHEMA_VERSION` stays `"2.2"`.

#### 18. `src/statcatlib/store/lance.py` (Modify)

*   **Scope**: Search index.
*   **Changes**:
    - Name tables `search_<lang>__<edition_id without hyphens>`, with
      `<lang>` from `sdmxlib.tag_alias`. Delete `_language_slug`. A test
      records the slugs for `en`, `fr`, `fr-ca` and `zh-hant`.
    - Set `_MANIFEST_VERSION` to 3. An index of version 2 or lower raises
      `IncompatibleCatalogError`.
    - Delete the legacy single-table read path (pre-pinaxlib v5.23) and its
      warning.
    - For an edition with no rows, seal drops the carried tables and writes
      a manifest with no languages.

#### 19. `src/statcatlib/bundle.py` (Modify)

*   **Scope**: The statcat section.
*   **Changes**: `catalog_section(store: _Store)` emits format 3 with
    `kind: "data"`. Seal step 3b passes the WorkingCopy's `_Store`. Keep
    the module name.

#### 20. `src/statcatlib/checks.py` (Create)

*   **Scope**: Seal checks.
*   **Changes**: Define the `SealCheck` alias
    (`Callable[[EditionView], Iterable[Finding]]`) and
    `conf_status(attribute="CONF_STATUS", public=("F",))`. `conf_status`
    reads `view.registry.obs(flow)` for each dataflow whose DSD has the
    attribute. It returns one `Finding` per observation whose `attribute`
    value is outside `public`. Every finding refuses the seal. `seal` runs no checks by
    default.

#### 21. `src/statcatlib/testing.py` (Modify)

*   **Scope**: Test helpers.
*   **Changes**:
    - Add `temporary_catalog()`, a context manager over a
      `TemporaryDirectory` that yields a created `Catalog`.
    - Change `synthetic_catalog` to
      `synthetic_catalog(path, shape, *, product_id="synthetic", seed=0,
      index=True) -> Edition`. It creates the catalog, builds one product,
      seals it, and returns the `Edition`.

#### 22. `src/statcatlib/cli/__init__.py` (Modify)

*   **Scope**: The `statcat` CLI.
*   **Changes**: `build-index CATALOG PRODUCT` refuses a path with no
    `catalog.json` and names `Catalog.create`. Only then does it call
    `create(exist_ok=True)` and resume the product's WorkingCopy. It refuses
    when the product has none.

#### 23. `src/statcatlib/__init__.py` (Modify)

*   **Scope**: Public API.
*   **Changes**: Export these names:
    - the classes `Catalog`, `Product`, `Edition`, `EditionView` and
      `WorkingCopy`;
    - the module `checks`;
    - the errors `InventoryConflictError`, `InventoryContentionError`,
      `InventoryRuleError`, `NonFastForwardError`, `WorkingCopyExistsError`,
      `WorkingCopyClosedError`, `SealCheckError`, `JournalError` and
      `NoLiveEditionError`.

#### 24. Tests (Create or Modify)

*   **Scope**: The S2 test suite.
*   **Create**:

    | File | Marker | What it tests |
    |---|---|---|
    | `tests/test_inventory_writer.py` | unit | Exclusive create; a kill before the link leaves no root; forward-only `CURRENT`; a lagging `CURRENT` found by probing (RFC §4); one leaf and one root per one-product change (RFC D11); every document validates (RFC D8); a 1.1 root is read and a 2.0 root refused |
    | `tests/test_inventory_conformance.py` | integration | The test adapter over `try_commit` passes `sdmxlib.inventory.conformance.run` |
    | `tests/test_catalog_model.py` | unit | RFC D2, D3 (the first seal writes version 1), D15, D19; P4; a lifecycle change is refused before build; `create` refuses a directory where `os.link` fails (patched) |
    | `tests/test_seal_crash.py` | integration | A kill at each named seal step (after the journal, after compaction, after the manifest, after the leaves, after the root, after `CURRENT`, before the journal is cleared), then `working_copy()`: one live edition, no duplicate, the expected `previous` (RFC §6); after a late `NonFastForwardError`, the repair records no edition; a seal refused for an out-of-codelist value leaves no journal entry, and resume then seal succeeds; a crash between compaction and the manifest, then `working_copy(resume=True).discard()` succeeds; a `duplicate` with matching digests is reported as success; an empty journal entry raises `JournalError` |
    | `tests/test_concurrent_seal.py` | integration | Two processes sealing different products both end live (RFC D6) |
    | `tests/test_edition_view.py` | unit | Two attached editions resolve `statcat.x`, `sdmx.x`, a scope, `batch_labels`, `code_map` and `annotation_fingerprint` to their own catalog (RFC §10); `Catalog.open` writes no bytes (file-set and mtime snapshot); an edition with no search rows answers `search()` with no hits; a changed `manifest.json` makes `view()` raise; `editions()` returns `previous` |
    | `tests/test_legacy_layout.py` | unit | RFC D10, with the file set unchanged |
    | `tests/test_seal_checks.py` | unit | `conf_status`; a failing check leaves the WorkingCopy open |
    | `tests/test_edition_check.py` | unit | RFC D9: changing a sealed edition fails `check_edition(at="open")` |
    | `tests/test_docs_boundary.py` | unit | RFC D5/D18: `docs/catalog-format.md` states the local embargo boundary |
    | `tests/test_live_cuj.py` | live | CUJ 1 against a real SDMX registry: create, working_copy, `ingest_sdmx`, a query on the view, seal, open, the same query |

    `test_public_api.py` (Modify) gains the RFC D13/D16 assertion: no
    attribute of `Catalog`, `Product` or `EditionView` returns a
    `CatalogAdmin` or an embargoed edition.

*   **Modify `tests/conftest.py`**:
    - `wc` is a WorkingCopy in a `temporary_catalog`.
    - `view` is `wc.view()`, taken once. It sees later writes because it
      shares the `_Store`.
    - `sealed_view` is `cat.product(p).view()` after `wc.seal()`.
    - `store` is an alias for an object that forwards `wc`'s write methods
      and `view`'s query methods, so the 162 tests that use it stay
      unchanged.
    - `indexed_store` keeps module scope over a sealed edition.
    - `require_index` and `search_table` take an `EditionView`.
    - Tests that run raw SQL through `store.connection` or `cat.connection`
      (about 15 sites, in `test_sources_sdmx.py`, `test_ingest_data.py`,
      `test_skos.py`, `test_store.py`, `test_catalog_folder.py` and
      `test_sdmx_schema_guard.py`) call `view._cursor()` instead. It is
      private, and the tests use it on purpose.
    - Tests that reopen a store to check that writes persist
      (`test_sources_sdmx.py` 321–340 and 517–521) are rewritten to seal
      and then read through `sealed_view`.
*   **Each file's fate**:

    | File | Disposition |
    |---|---|
    | `test_sdmx_schema_guard.py` | Rewrite: the guard is sdmxlib's `StoreSchemaError` in `LocalRegistry.edition` |
    | `test_statcat_schema_guard.py` | Rewrite: the guard runs at `Product.view()` and at WorkingCopy creation |
    | `test_index_version_guard.py` | Rewrite TestReadable and TestGuard (skipped in S1) over `sealed_view`: v3 is read and v2 refused; delete the v1 case |
    | `test_thread_safety.py`, `test_shared_connection_idle.py` | Rewrite over `sealed_view` |
    | `test_catalog_folder.py`, `test_read_only.py` | Rewrite around `create` and `open` |
    | `test_agent_contact_ids.py`, `test_bulk_insert_quoting.py`, `test_code_search.py`, `test_code_search_extensions.py`, `test_codelist_policy_and_concepts.py`, `test_cross_entity_queries.py`, `test_filters.py`, `test_hybrid_search.py`, `test_ingest_data.py`, `test_language_alternates.py`, `test_lineage.py`, `test_live_sdmx.py`, `test_public_api.py`, `test_query_builder.py`, `test_query_entities.py`, `test_reference_metadata_search.py`, `test_scopes.py`, `test_sdmx_bridge.py`, `test_skos.py`, `test_sources_ckan.py`, `test_sources_sdmx.py`, `test_store.py`, `test_store_crud.py`, `test_structural_queries.py`, `test_bundle_manifest.py` | Replace each `Catalog(...)` call with the `wc`, `view` or `store` fixture |

#### 25. Docs and agent context (Create or Modify)

*   **Scope**: The S2 documentation.
*   **Changes**:
    - **Examples:** in `README.md`, `llms.txt`, `docs/tutorial.md`,
      `docs/search.md`, `docs/api/store.md` and `docs/api/sources.md`,
      change every `Catalog(...)` example to `Catalog.create`,
      `working_copy`, `seal`, `open` and `product().view()`.
    - **`docs/bundle-format.md`:** rename it to `docs/catalog-format.md`
      and rewrite it. It describes:
      - the layout;
      - statcat section format 3;
      - the local embargo boundary: on disk, embargo is enforced by the
        API, not by file permissions;
      - the locations statcatlib does not support: synced folders and
        network shares.

      Keep its section numbers.
    - **`docs/search.md:196`:** point it at
      `catalog-format.md#4-the-search-index`.
    - **New pages:** create `docs/reference/checks.md` for
      `statcatlib.checks`. Document `Catalog`, `Product`, `Edition`,
      `EditionView` and `WorkingCopy` in `docs/reference/catalog.md`.
    - **`mkdocs.yml`:** rename the nav entry to
      `- Catalog Format: catalog-format.md`, and add
      `- Checks: reference/checks.md` under Reference.
    - **`docs/upgrading.md`:** add the 2a API changes with their replacement
      calls, the re-ingest steps, and that until sdmxlib-rest GH-81 ships,
      the deployed sdmxlib-rest cannot read catalogs this release writes.
    - **`CLAUDE.md`:** update the Architecture tree, "Query surface" and
      "Storage". Delete the paragraph on the pinaxlib legacy index.
    - **Per-directory `EGM.md` files:** update `src/statcatlib/EGM.md`,
      `src/statcatlib/store/EGM.md`, `src/statcatlib/cli/EGM.md` and
      `src/statcatlib/sources/EGM.md`. Update `tools/EGM.md` only if it
      mentions bundles.

### Slice S2b: embedding reuse and the batched seal

#### 26. `src/statcatlib/store/lance.py` (Modify)

*   **Scope**: Search index.
*   **Changes**: Add a `text_sha256` column to each search table. Make
    `add_bulk` reuse a carried vector whose hash matches, and encode only
    the texts whose hash is missing.

#### 27. `src/statcatlib/catalog.py` (Modify)

*   **Scope**: Public model.
*   **Changes**: Add `Catalog.seal(working_copies, *, checks=())` with
    per-product outcomes, as the Technical Plan describes.

#### 28. Tests and docs (Create or Modify)

*   **Scope**: The S2b tests and docs.
*   **Changes**:
    - Create `tests/test_embedding_reuse.py` (unit). It checks two cases:
      - RFC D21: unchanged text causes zero `encode` calls;
      - after a model change, every text is re-encoded.
    - Add the batched seal's refused-product case and its crash note to
      `test_catalog_model.py`.
    - Document the batched seal in `docs/api/store.md`.

### Slice S3: CatalogAdmin

#### 29. `pyproject.toml` (Modify)

*   **Scope**: Dependencies.
*   **Changes**: Raise the sdmxlib pin to the release that carries sdmxlib
    issue 1, once sdmxlib-rest GH-81 pins it.

#### 30. `src/statcatlib/admin.py` (Create)

*   **Scope**: Operational object.
*   **Changes**: Create `CatalogAdmin.open(path)`, which builds a
    `FullInventory` and an `InventoryWriter`. `editions(product)` only
    reads. `rollback`, `release`, `cancel`, `set_embargo` and `describe`
    each require `reason` and make one commit, as in the CatalogAdmin table.

#### 31. `src/statcatlib/bundle.py`, `src/statcatlib/catalog.py` (Modify)

*   **Scope**: The catalog description.
*   **Changes**:
    - Add `description_to_json` and `description_from_json` to
      `bundle.py`. They cover the catalog-level fields only.
    - Add `Catalog.description` to `catalog.py`.

#### 32. `src/statcatlib/__init__.py` (Modify)

*   **Scope**: Public API.
*   **Changes**: Export `CatalogAdmin`.

#### 33. `tests/test_catalog_admin.py` (Create)

*   **Scope**: The S3 tests (`pytestmark = pytest.mark.integration`).
*   **Changes**:
    - **RFC D4, D13, D16:** the public `Catalog` never returns an embargoed
      entry, tested against a fixture that holds embargoed entries. No
      attribute of `Catalog`, `Product` or `EditionView` returns a
      `CatalogAdmin`.
    - **RFC D7:** a non-fast-forward release is refused, and the error names
      both editions. A conditional rollback retry after a competing release
      is refused.
    - **RFC D14:** `len(get_args(State)) == 4`, and neither repair nor
      `release` re-releases a withdrawn edition.
    - **RFC D17:** a stale automatic release after `set_embargo(True)` is
      refused.
    - **RFC D20:** `describe` changes the description in one commit, and two
      versions carry different descriptions.
    - **CUJ 2 and CUJ 6**, end to end.

#### 34. Docs (Create or Modify)

*   **Scope**: The S3 documentation.
*   **Changes**:
    - Create `docs/api/admin.md`. It covers rollback, and embargo with
      explicit release and cancel. It notes that `release` uses the local
      clock, so a skewed laptop clock releases early or late.
    - Create `docs/reference/admin.md`.
    - Add both pages to `mkdocs.yml`.
    - Update `docs/upgrading.md`.

### Slice S4: prune and the tools

#### 35. `src/statcatlib/admin.py` (Modify)

*   **Scope**: Operational object.
*   **Changes**: Add `PruneReport`, an attrs class with `retired`,
    `deleted_dirs`, `deleted_files`, `skipped_products` and `failed`. Add
    `CatalogAdmin.prune(*, older_than: timedelta, reason, dry_run=True) ->
    PruneReport`, which runs the three steps of the Technical Plan.

#### 36. `src/statcatlib/cli/__init__.py` (Modify)

*   **Scope**: The `statcat` CLI.
*   **Changes**: Add three commands:
    - `statcat verify-catalog PATH`, which exits 0, 1 or 2;
    - `statcat doctor PATH`;
    - `statcat prune PATH --older-than DAYS --reason TEXT [--apply]`.
      Without `--apply`, it prints the dry-run report.

#### 37. Tests (Create)

*   **Scope**: The S4 tests.
*   **Changes**:
    - Create `tests/test_prune.py` (unit). It checks that:
      - a dry run deletes nothing;
      - a product with a WorkingCopy or a journal entry is skipped and
        reported;
      - a `data/` file newer than the window survives;
      - tombstoned directories and unreferenced `data/` files past the
        window are deleted;
      - no root or leaf is deleted;
      - a failing deletion is recorded in `failed` and the run continues.
    - Create `tests/test_cli_verify_doctor.py` (unit). It checks that:
      - a corrupted file makes `verify-catalog` exit 1 and name it, both
        for an edition file and for a `data/` Parquet file;
      - an unreadable catalog exits 2;
      - `doctor` writes nothing and reports a journal entry, a WorkingCopy,
        an unrecorded sealed edition, a leftover `.tmp` file in
        `inventory/`, and a synced-folder path (CUJ 5).

#### 38. Docs and design records (Modify)

*   **Scope**: Documentation and the RFC.
*   **Changes**:
    - Document prune and the tools in `docs/api/admin.md` and
      `docs/upgrading.md`.
    - In `docs/design/products-editions-inventory.md`:
      - move prune out of the Non-Goals to match decision 32, as narrowed by
        P7;
      - record that `correct` and `reinstate` move to Phase 4 (P6).

      The RFC already links this design as superseding it for
      implementation detail.
    - `docs/design/SITEMAP.md` was registered on 2026-10-31.

### RFC acceptance test map

| RFC row | Slice | Test |
|---|---|---|
| D1, D12 | S1, S2 | `just ci` passes with the 0.80 pin and no version branches |
| D2, D3, D15, D19 | S2 | `test_catalog_model.py` |
| D4, D13, D16 | S2, S3 | `test_public_api.py`; `test_catalog_admin.py` |
| D5/D18 | S2 | `test_docs_boundary.py` |
| D6 | S2 | `test_concurrent_seal.py` |
| D7, D14, D17, D20 | S3 | `test_catalog_admin.py` |
| D8, D11, RFC §4 | S2 | `test_inventory_writer.py` |
| D9 | S2 | `test_edition_check.py` |
| D10 | S2 | `test_legacy_layout.py` |
| D21 | S2b | `test_embedding_reuse.py` |
| D22 | — | N/A: the seal-cost measurement is sdmxlib GH-248, already done |
| D28 | — | N/A in 2a: `reinstate` deferred (P6) |
| D32 | S4 | `test_prune.py` |
| RFC §6 | S2 | `test_seal_crash.py` |
| RFC §10 | S2 | `test_edition_view.py` |

### Verification Plan

*   **Type check**: `just typecheck` (pyrefly over `src` and `tests`).
    `just check-any` passes after each slice updates
    `tools/any_baseline.txt`.
*   **Unit and integration tests**: `just ci`. Every new test file sets the
    `pytestmark` given in its item.
*   **Lint, docstrings and prose**: `just lint`, `just check-docstrings` and
    `just check-prose`.
*   **Docs**: `just docs-build` (`mkdocs build --strict`) passes in every
    slice.
*   **Coverage**: `just coverage` reaches 85% or more. `test_seal_crash.py`
    kills the writer at every named seal step.
*   **Conformance**: `sdmxlib.inventory.conformance.run` passes from S2
    onwards.
*   **Live**: `tests/test_live_cuj.py` (`pytestmark = pytest.mark.live`) runs
    CUJ 1 against a real SDMX registry through `just test-live`. CUJ 2 runs
    as an integration test in `test_catalog_admin.py`.

--------------------------------------------------------------------------------

*Drafted with EGM*

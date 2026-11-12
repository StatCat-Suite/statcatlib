# statcatlib

## Overview

`statcatlib` is the package root. `__init__.py` re-exports the public API of
`model`, `sources` and `store`; `CatalogAdmin` from `admin.py`; `EmbargoReader` from `embargo.py`;
`Catalog`, `Product`, `Edition`, `SealOutcomes` and `NoLiveEditionError` from `catalog.py`; the errors `InventoryConflictError`,
`InventoryContentionError`, `InventoryRuleError`, `InventoryWriteRefusedError`,
`NonFastForwardError` and `CrossProductRuleError` from `store/inventory.py`; `BlobLocation` and `TokenIdentity`
from `store/blob.py`; `upload_edition`, `UploadReport` and `UploadCheckError` from `upload.py`; `WorkingCopyExistsError`,
`WorkingCopyClosedError`, `SealCheckError`, `EditionCheckError` and `JournalError` from
`store/working_copy.py`; `NotLoadedError` and `is_unloaded` from the store's
sentinel module; and part of `expr` (listed below). It exposes `checks`,
`query` and `urn` as submodules (`sc.checks`, `sc.query`, `sc.urn`). The
top-level modules hold the parts that sit across subpackages: the catalog of
products (`catalog.py`), the operational changes to its Inventory
(`admin.py`), the reader of embargoed editions (`embargo.py`), seal checks (`checks.py`), the `statcat` section of
an sdmxlib edition manifest (`bundle.py`), the integrity and support
checks behind `statcat verify-catalog` and `statcat doctor`
(`diagnostics.py`), the copy of a sealed edition to object storage (`upload.py`), the
jobs configuration the release and GC jobs read (`jobs.py`), the GC job (`gc.py`), the release job
(`release.py`) and its access precondition (`access.py`), composable query filters
(`query.py`), traversal expressions (`expr.py`), SDMX URN builders (`urn.py`)
and test catalogs (`testing.py`).

## Interface

### Subpackages

- `model/`: [`./model/EGM.md`](./model/EGM.md) — attrs domain model (datasets,
  agents, distributions, SKOS concepts); imports only `InternationalString`
  from sdmxlib.
- `store/`: [`./store/EGM.md`](./store/EGM.md) — the private `_Store`,
  `EditionView`, `WorkingCopy` and the seal, the Inventory writer and its
  local and blob storage, `QueryBuilder`, scopes, DDL, LanceDB search index.
- `sources/`: [`./sources/EGM.md`](./sources/EGM.md) — SDMX and CKAN ingest
  connectors.
- `cli/`: [`./cli/EGM.md`](./cli/EGM.md) — the `statcat` Typer command.

### `catalog.py`

- `Catalog`: a directory of products. `Catalog(...)` raises `TypeError`
  naming the two constructors and `testing.temporary_catalog`.
  - `Catalog.create(path, *, exist_ok=False)`: on a missing or empty
    directory, probes `os.link` and writes `catalog.json` (a UUIDv7
    `catalog_id`, `restore_epoch` 0) and nothing else; the first seal writes
    Inventory version 1. On an existing catalog it raises `FileExistsError`,
    or with `exist_ok=True` reopens it for writing and writes nothing.
  - `Catalog.open(path)`: read-only. Raises `FileNotFoundError` when *path*
    has no `catalog.json`. A catalog with no Inventory version has no
    products. A URI string raises `ValueError` naming `BlobLocation`.
  - `Catalog.open(BlobLocation)`: the same over object storage, with a
    `_Opening` whose `root` is `<cache_dir>/<catalog_id>`. No `cache_dir` raises
    `ValueError`, and a `catalog_id` that cannot name a directory raises
    `IncompatibleCatalogError`. `catalog_id` is read once; `refresh` and
    `description` read through a `BlobInventoryStorage`, and `repr` and messages
    show `location.uri`. `Catalog.create` raises `TypeError` for one.
  - `description`: the `CatalogDescription` in the root's
    `extensions.statcat` of the Inventory version the live view holds (as of
    the last `refresh()`), read with `bundle.description_from_root`; `None`
    when none was recorded.
  - `writable`, `catalog_id`, `refresh()`, `products()` (the products with a
    live edition, sorted), `product(product_id) -> Product` (`ValueError`
    unless the id matches `^[A-Za-z0-9][A-Za-z0-9._-]*$`), `close()` and the
    context manager.
  - `seal(working_copies, *, checks=()) -> SealOutcomes`: the batched seal.
    It refuses, with `ValueError`, a working copy of another catalog or two
    of one product, and raises `WorkingCopyClosedError` for a closed one,
    before sealing anything. `_seal_each` then runs the structure pass
    (`_seal_ready`: `WorkingCopy._precheck(checks, batch=)` for each
    structure working copy, whose journal repair leaves the batch's other
    products out; `_cross_ready`, the cross-product pre-check of the pass's
    working copies together through `precheck_refusals`; then
    `_seal_edition` for each), the structure-check pass
    (`_check_structures`: each structure edition's `_publish_check`, given
    the batch's other structure editions, repeated until it drops none) and
    the data pass (the same `_seal_ready`, its cross-product pre-check given
    the checked structure editions as `Planned` records (`_fixed`), then
    `_check_partials`: the `_publish_check` of each data edition whose
    partials record a `partial_of`, given every structure product's editions
    with the batch's structure editions in their place). `_record_batch`
    records every sealed edition in one `InventoryWriter.commit`;
    `_batch_leaves` leaves out each product whose `_next_leaf` raises
    `InventoryRuleError`, each the cross-product rules refuse, and each
    edition whose publish check was given a left-out one (`_drop_dependents`),
    running the rules again until a pass drops none, and counts an edition a
    journal repair recorded already as recorded. `RuntimeError` on a catalog from `open`.
  - `search` and `facets` raise `NotImplementedError` naming
    `product(id).view()`. `__getattr__` raises `AttributeError` naming
    `cat.product(id).view().<name>` for every name in
    `store.store.VIEW_ATTRIBUTES`.
- `Product`: `product_id`; `view(*, embedding_model=None, query_prefix="")
  -> EditionView`, which calls `_open_view` with the live head and the stamps
  directory `<root>/verified`; `editions() -> list[Edition]` (the live head, with the
  `previous` its manifest records; empty without one); `working_copy(*,
  kind=None, lifecycle="revisable", resume=False, embedding_model=None,
  document_prefix="", query_prefix="") -> WorkingCopy` (`kind` is `"data"`
  or `"structure"`; `None` takes the recorded one, or `"data"`); `rollback()`, which
  raises `AttributeError` naming `CatalogAdmin.rollback`.
  `Catalog.product` is the only constructor.
- `NoLiveEditionError` (`LookupError`): `view()` on a product with no live
  edition.
- `_open_read_only(path, *, opener) -> _Opening`: `Catalog.open`'s checks and
  its `_Opening`, shared with `EmbargoReader.open`; *opener* names the call in
  messages.
- `_open_view(engine, root, location, product_id, edition_id, *,
  manifest_sha256, stamps, state, embedding_model, query_prefix) ->
  tuple[EditionView, EditionRegistry, _Store]`: the one place an `EditionView`
  of a recorded edition is built, shared by `Product.view` and
  `EmbargoReader.view` (Phase 5 D12). *engine* is a callable that builds the
  caller's engine on first use, so an edition refused before it opens builds none.
  On local disk it attaches
  `<root>/products/<product>/<edition>`; over a `BlobLocation`, `_checked_copy`
  runs `_cached_edition` (the manifest first, then `pull_edition`), refuses an
  edition with data files when the location holds no `secrets` (`ValueError`),
  and runs `check_on_open(stamps=, references=bool(manifest.bindings),
  extensions={"statcat": ...})`. Then `LocalRegistry.edition(...,
  manifest_sha256=)`, its `ManifestError` re-raised as `IncompatibleCatalogError`,
  and a read-only `_Store`. The caller keeps the registry and the store to close.
  *state* names the entry's state in messages.
- `SealOutcomes`: a frozen attrs value with `sealed` (product id to
  `Edition`, all recorded in one version) and `refused` (product id to the
  error: `SealCheckError`, `NonFastForwardError` or another
  `InventoryRuleError` (`CrossProductRuleError` from the pre-check or the
  record included), `ManifestError`, `EditionCheckError`,
  `InventoryContentionError`, or `InventoryWriteRefusedError`).
- `Edition` is re-exported from `store/working_copy.py`.

### `admin.py`

`CatalogAdmin` is exported from `statcatlib`; `EditionEntry` and `PruneReport`
are imported from `statcatlib.admin`.

- `CatalogAdmin.open(path, *, actor=None, clock=None)`: *path* is a catalog
  directory or a `BlobLocation`. A directory: refuses a legacy layout
  (`catalog._refuse_legacy_layout`) and a path with no `catalog.json`
  (`FileNotFoundError`), and refuses `actor=` or `clock=` with `TypeError`. A
  `BlobLocation`: builds `BlobInventoryStorage(location, actor=, clock=)`, and a
  store with no `catalog.json` raises `FileNotFoundError`. Either way it reads
  `catalog.json` and holds an `InventoryWriter`, and writes nothing until a
  change is made. `repr` shows `storage.location`. Direct construction raises
  `TypeError`.
- Reads, each from a fresh `InventoryWriter.full()`: `embargo`,
  `products()` (every recorded product, any state) and
  `editions(product_id) -> list[EditionEntry]` (every entry the newest version
  records, in id order; empty for an unrecorded product).
- Changes, each requiring a non-blank `reason` (`ValueError` otherwise) and
  writing one version through `InventoryWriter.commit`, returning its `seq`:
  `rollback(product_id, *, to=None, if_live=None, reason)` (sdmxlib
  `rollback` at sdmxlib's store `SCHEMA_VERSION`, `if_live` defaulting to the
  head when the call starts; kind `rollback`), `release(product_id,
  edition_id, *, reason)` (`release(explicit=True)`; a `fast-forward` refusal
  becomes `NonFastForwardError` naming the edition, the live edition and the
  fix; kind `release`), `cancel(product_id, edition_id, *, reason)` (kind
  `withdraw`), `set_embargo(*, on, reason)` (`Change(embargo=on)`; kind
  `embargo-change`) and `describe(description, *, reason)` (sets
  `format_version` `"3"` and `description` in the root's `extensions.statcat`,
  keeping its other keys; kind `describe`).
- `advance_current() -> int | None`: `InventoryWriter.advance_current`; it
  writes `CURRENT` only.
- Product changes go through `_commit_product(product_id, edition_id, step,
  *, kind, reason)`, which reads `storage.now()` once per head (the local
  clock on disk, design decision P1; the storage clock on blob), applies one of
  sdmxlib's pure rules to the head's leaf with it, returns `Change(now=)` with
  the same time, and wraps a `TransitionError` with `InventoryRuleError.wrapping`. A product the
  head does not record raises `InventoryRuleError(rule="unknown")`. The
  writer's `commit` runs the cross-product rules on a release or a rollback
  and raises `CrossProductRuleError`; `admin.py` adds no rule of its own.
- `prune(*, older_than, reason, dry_run=True) -> PruneReport` (design
  decision P7): raises `TypeError` on a storage other than
  `LocalInventoryStorage`, naming `statcat gc` and the release job; refuses a
  blank *reason* or a window that is not positive (`ValueError`). Its cutoff
  and tombstone time are `statcatlib.admin.datetime.now(UTC)`. It skips every product `_busy` names (an unsealed edition
  whose id is not tombstoned, a `journal/*.json` file, or an unreadable
  manifest), then runs three steps. `_prune_retire` commits
  `store.inventory.retire_change(head, cutoff, now=, artefacts=writer.artefacts,
  skipped=)` in one `InventoryWriter.commit` of kind `retire`, re-planned
  against each head; `None` writes nothing. A dry run plans the same change
  against the head it read. `_new_tombstones` lists the tombstones the change
  adds. `_prune_dirs` deletes
  `products/<p>/<edition_id>` for each tombstone whose `retired_at` is
  before the cutoff. `_prune_data` deletes each `data/` file no on-disk
  manifest lists (`sdmxlib.edition.edition_files`) whose mtime is before the
  cutoff. `_delete` records an `OSError` in `failed` and continues. A dry run
  plans the same steps against the head and changes nothing. It deletes no
  root, leaf or tombstone.
- `PruneReport`: a frozen attrs value with `retired` (`<product>/<edition>`),
  `deleted_dirs`, `deleted_files` (paths from the catalog directory),
  `skipped_products` and `failed` (dicts to the reason or error), `root`,
  `dry_run` and `seq` (the `retire` version, or `None`).
- `EditionEntry`: a frozen attrs value with `edition_id`, `product_id`,
  `previous`, `state`, `manifest_sha256`, `schema_version`,
  `state_changed_at` and `release_at`.

### `embargo.py`

`EmbargoReader` (Phase 5 D12) is exported from `statcatlib`. statcat-serve's preview
and pre-materialisation read through it (sdmxlib plan Q8, Q9).

- `EmbargoReader.open(location: str | Path | BlobLocation) -> Self`: runs
  `catalog._open_read_only(opener="EmbargoReader.open")`, so it refuses what
  `Catalog.open` refuses, then `refresh()`. Direct construction raises `TypeError`
  naming `open`.
- `refresh() -> Self`: holds a `FullInventory` of its own over
  `InventoryWriter(...).storage.read`, made once a version exists, and refreshes it.
- `editions(*, states=("embargoed",)) -> list[EditionEntry]`: the entries in
  *states* of the held version, by product id and then leaf order, through
  `admin._edition_entry`; empty before the first version. A string, or a state
  outside sdmxlib's `State`, raises `ValueError`.
- `view(product_id, edition_id, *, embedding_model=None, query_prefix="") ->
  EditionView`: an unrecorded product raises `InventoryRuleError(rule="unknown")`
  (`admin._leaf`), as does an unrecorded edition; a `superseded` or `withdrawn`
  entry raises `ValueError`, before anything under the edition is read. Otherwise
  `catalog._open_view` on the reader's own `sdmxlib.EditionEngine`, with stamps in
  `<root>/embargo-verified`.
- `close()`: closes every view's store and registry, then the engine; idempotent.
  A closed reader's methods and views raise `RuntimeError`.

### `jobs.py`

The jobs configuration (Phase 5 D7), imported from `statcatlib.jobs`; nothing is
re-exported from `statcatlib`. Standard library and `attrs` only.

- `load_jobs_config(path) -> JobsConfig`: reads the bytes, parses with
  `tomllib`, decodes, and runs `RetentionPolicy.check()`. Every refusal raises
  `JobsConfigError` (`ValueError`), its message starting with the path: an
  unreadable file, not UTF-8, not TOML, an unknown key at any level (the top
  level, `[retention]`, `[access]`, each allowlist entry, `[expiry]`), a value of
  the wrong type (`bool` is no integer), a missing or other `format_version`
  (`FORMAT_VERSION = 1`), a missing `[retention]` or required key, a bad duration
  or instant, an `[expiry]` with `enabled = true` and no `not_before`,
  `namespace`, `deployments` or `port`, a port outside 1 to 65535, or a failed
  inequality.
- `JobsConfig(retention, access, expiry, sha256, format_version=1)`: `access` is
  `None` when the file has no `[access]`, which `ReleaseJob` refuses; `expiry` is
  `ExpiryPolicy()` (disabled) when it has no `[expiry]`; `sha256` is the file's,
  which each run logs.
- `RetentionPolicy(backup, gc_window, tombstone_window, inventory_window,
  retire_after, pod_staleness, keep_roots=DEFAULT_KEEP_ROOTS (10),
  upload_grace=DEFAULT_UPLOAD_GRACE (24 h))`, `durations()`, and `check()`,
  which raises `JobsConfigError` naming both values for the first failure of:
  every duration positive; `backup <= gc_window <= tombstone_window`;
  `inventory_window >= gc_window + 1d`; `pod_staleness < inventory_window`;
  `keep_roots >= 2`.
- `AccessPolicy(storage_account, container, allowlist, sentinels=(), scopes=())`
  with `Principal(object_id, name)`; `storage_account`, `container`,
  `allowlist` and `sentinels` are required keys, `sentinels` may be empty.
- `ExpiryPolicy(enabled=False, not_before=None, namespace=None, deployments=(),
  port=None)`.
- `parse_duration(text) -> timedelta` (`^[0-9]+[smhd]$`) and
  `format_duration(value) -> str` (the largest whole unit, as messages write it).
- `decode_registration(path, raw) -> Registration`: reads
  `pending/<product_id>/<edition_id>.json` as the release job's fold and the GC job
  read it. `ValueError` for a path of another shape, a product id that cannot name a
  directory (`_PRODUCT_ID`, `store/blob.py`'s `_SAFE_ID` pattern), an edition id that
  is no canonical UUIDv7, a document that is not one object with the keys
  `edition_id` and `release_at` in that order, an `edition_id` other than the file
  name's, a `release_at` that is neither `null` nor a zoned time, or bytes that differ
  from `_pending_document` of their own decode.
- `Registration(product_id, edition_id, release_at)` (frozen): `path`, and
  `edition_time`, the UUIDv7's time, which the tombstone window is measured from.
- `_pending_document(edition_id, release_at) -> bytes`: the `pending/` document's
  bytes, the contract `docs/catalog-format.md` §1.5 states; `upload.py` writes with
  it. `_stamp(moment)` writes a time in UTC to the millisecond with `Z`.
- `_emit(logger, level, event, message)`: logs *message* with the JSON object *event*
  as the record's `_EVENT_ATTRIBUTE` (`"statcat_event"`), the D14 event a job command
  prints as one line on standard output.

### `gc.py`

The GC job (Phase 5 D10), imported from `statcatlib.gc`; nothing is re-exported from
`statcatlib`. `statcat gc` runs it.

- `collect_garbage(location, policy, *, storage=None, dry_run=False, forecast=None,
  discard=()) -> GcReport`: runs `policy.check()` (`JobsConfigError` before any
  read), parses each *discard* as `<product>/<edition>` (`ValueError`), reads
  `storage.now()` once (`storage` defaults to `BlobInventoryStorage(location)`), then
  `_Run`'s steps: `inventory()` (the head through `FullInventory`, then older roots
  by `seq` until `keep_roots` are read and one created before `now - gc_window`,
  never at or below `HISTORY.expired_through`; leaves by hash on `GC_THREADS`),
  the discard refusal (`ValueError` naming `statcat admin cancel` for an edition the
  head records or a root read keeps), `registrations()` (LIST `pending/`; a
  registration of an edition the head records is `folded` once last modified
  before the cutoff and is never read; any other is decoded with
  `jobs.decode_registration`, and an unreadable one fails its product closed; one
  whose `edition_time` is older than `tombstone_window` is `expired-registration`
  and protects nothing), `manifests()` (every kept and registered manifest on
  `GC_THREADS`; a kept one must match its `manifest_sha256`, a registered one must
  name its path; a failure, or a kept tombstone with no entry read, fails the
  product closed), `products()` (LIST `products/`: `data/` files no manifest lists,
  older than `upload_grace`, outside failed-closed products, are `unreferenced`)
  and `directories()` (`retired` for an edition tombstoned before the cutoff and not
  kept; `discarded`; `gc.unregistered` for a directory nothing keeps or registers,
  older than `upload_grace`). It logs every `gc.planned` line, then each
  `gc.forecast_deviation`, then deletes unless `dry_run`, in three pooled phases:
  registrations and `data/` files; each directory's `manifest.json` once its
  registration, if any, is deleted; the rest once its manifest is deleted or absent.
  With *forecast* only paths in both plans are deleted.
- `GcReport(planned, deleted, failed, failed_closed, unregistered, deviations,
  dry_run)` and `Deletion(path, reason, edition=None)`, frozen. `Reason` is
  `retired`, `unreferenced`, `folded`, `expired-registration` or `discarded`.
- `GcStoppedError(RuntimeError)`: no `catalog.json`, an opted-in catalog with no
  `HISTORY`, an unreadable head, any root the reader skipped, a missing or invalid
  root above `expired_through`, a missing or unhashed leaf. Nothing is deleted.
- `_delete(location, path)` refuses (`ValueError`) any path outside `products/` and
  `pending/`, and returns a `BlobStoreError`'s text in place of raising it.
- Events go through `jobs._emit` on the `statcatlib.gc` logger: deletion log lines
  (`event`, `path`, `reason`, `edition`, and `error` on `gc.delete_failed`) and D14
  alerts (`event`, `severity`, `at`, `catalog`, and `product`, `edition`, `reason`,
  `detail` when they apply).

### `release.py`

The release job (Phase 5 Part 3), imported from `statcatlib.release`; nothing is
re-exported from `statcatlib`. `statcat release` runs it.

- `ReleaseJob(location, config, *, rest, listing, storage=None)`: *config* is a
  `JobsConfig` whose `access` is set and names the container of `location.uri`
  (`az://<container>/...`) and, when the store's config names `account_name`, that
  account (`JobsConfigError` otherwise); *rest* has `head(path) -> RemoteFile | None`
  (`AzureBlobRest`); *listing* is an `access.ReaderListing`; *storage* defaults to
  `BlobInventoryStorage(location)`. It holds one `InventoryWriter` over the storage.
- `run(*, dry_run=False) -> ReleaseReport`: step 0 reads `catalog.json`
  (`FileNotFoundError` when absent), refuses an opted-in catalog with
  `InventoryWriteRefusedError` naming Part 6, and reads `storage.now()`. Then, each
  through `_step`, which catches `_STEP_ERRORS` (`BlobStoreError`, `ManifestError`,
  `InventoryError`, `InventoryConflictError`, `InventoryContentionError`,
  `InventoryRuleError`, `IncompatibleCatalogError`, `OSError`), logs `step.failed`
  and goes on: `_advance` (`InventoryWriter.advance_current`, `current.behind`; left
  out of a dry run); `_access`, outside `_step` (`listing.readers()` and
  `access.check_access`; an `AccessListingError` becomes a failed `AccessResult`;
  any other error stops the run); `_fold`; `_release`; `_retire`. Each of the last
  three reads `storage.now()` once and commits through `InventoryWriter.commit` with
  a *change* that plans on the head it is given and returns `None` when nothing
  changes; a dry run calls the plan on `writer.full()` and commits nothing.
- The fold (`_plan_fold`, kind `seal`, reason `fold of pending/ registrations`):
  `full.leaves()`, one LIST of `pending/` a run, then for each
  `pending/<p>/<e>.json` the head neither records in a leaf nor tombstones, in path
  order, `_judge`: `_read` (kept by path for the run: the document through
  `jobs.decode_registration`, `too-old` against `tombstone_window`, the manifest
  through `decode_manifest` and `blob._refuse_unsafe_files`, `canonical-form`,
  `files` through `verify_remote` over `upload._ALL_TIERS` and `upload._lookup`),
  then `fold_lifecycle`, `_kind`, `chain` and `_refuse_changed_final`. A recorded
  registration is skipped by its path and never read; an object of another shape
  under `pending/` is ignored. A `BlobStoreError` is the registration's `store`
  refusal. `_check_structures` runs `_reference_check` for each candidate with no
  binding (`pull_edition` into the run's temporary directory, each home through
  `structure_entries`, `check_edition(at="receive", remote=, lookup=, structures=,
  extensions={"statcat": ...})`), the fold's structure editions together until a
  pass refuses none, and keeps each answer by edition and homes given.
  `_refuse_forks` refuses passing registrations of one product, `previous` and
  schema (`pending-fork`). `_record_stable` records per product in id order
  (`_record`: `fold_lifecycle` on the evolving leaf, sdmxlib's `record(embargo=True)`
  with `recorded_routing(routing_fields(manifest))`, `supersede_pending` for each
  embargoed entry with its `previous` and schema), runs `drop_until_stable`, refuses
  every registration of a dropped product, and refuses with `references` a product
  whose check was given a fold edition that is refused, until a pass refuses none.
  `_release_at` records `None` for a `release_at` before the registration's
  last-modified time (`fold.release_at_past`) and, on a restore epoch above 0, for a
  registration last modified before the epoch's first root (`fold.restored`), which
  `_find_epoch` takes from the head's record or finds by reading roots back from the
  head while they carry the epoch.
- The release step (`_plan_release`, kind `release`, reason `release_due at <time>`):
  `full.leaves()`, then, when the head's `embargo` is off and the access result
  passed, sdmxlib's `release_due` and `drop_until_stable`; the record from
  `refusal_record`; a `Change` when a leaf or the record differs. It logs
  `release.refused`, `release.held` (embargo on), `release.unprepared` (released and
  folded by this run) and `release.overdue` (every entry still embargoed with a
  `release_at` at or before the step's time). A failed fold makes the record keep
  the head's fold refusals.
- Retirement (`_retire`, kind `retire`): `inventory.retire_change(full, now -
  retire_after, now=, artefacts=writer.artefacts)`.
- `ReleaseReport(folded, released, retired, refused, skipped, failed, versions,
  access, dry_run)` and `ReleaseRefusal(product_id, edition_id, step, rule, reason,
  since)`, frozen; `Step` is `fold` or `release`.
- `refusal_record(full, refusals, now, *, epoch=(0, None))`: the next root's
  `extensions`, with `statcat.release` set to `{refused, omitted, epoch_started_at,
  restore_epoch}` and `format_version` to `bundle.ROOT_SECTION_FORMAT`; `since`
  carried from the head's record by `(product_id, edition_id, step)`; at most
  `MAX_REFUSALS` (500) kept, the oldest `since` first; the head's sections unchanged
  when there is no refusal, no epoch time and no record. `current_record(full)` is
  the head's `extensions`, and `current_refusals(full)` the refusals its record
  holds, leaving out an entry it cannot read. `RELEASE_KEY` is `"release"`.
- `drop_until_stable(writer, full, leaves) -> (kept, dropped)`: reads each incoming
  entry's manifest through `writer.artefacts` (`ManifestError` drops the product with
  `manifest`, `BlobStoreError` with `store`), then `writer.refusals`, dropping each
  refused product's whole change until a pass refuses none. `Dropped(edition_id,
  rule, reason)`. A manifest of another product's unfilled entry that cannot be
  read propagates.
- `structure_entries(full, *, product_id, batch) -> list[StructureEntry]`:
  `working_copy.structure_editions`'s choice as `StructureEntry(product_id,
  edition_id, manifest_sha256)` values.
- Events go through `jobs._emit` on the `statcatlib.release` logger: `event`,
  `severity`, `at` (the step's storage time), `catalog`, and `product`, `edition`,
  `rule`, `reason`, `detail` where they apply.

### `access.py`

The release job's access precondition (Phase 5 D8), imported from
`statcatlib.access`. `httpx`, the standard library and `attrs` only.

- `ReaderListing` (a `Protocol`): `readers() -> ReaderReport`, raising
  `AccessListingError` (`RuntimeError`) when the listing cannot be made.
- `ReaderReport(assignment_ids, grants)` and `ReaderGrant(assignment_id,
  principal_id, principal_type, role, scope)`, frozen.
- `check_access(report, allowlist, sentinels) -> AccessResult(passed, outsiders,
  unused, missing_sentinels, errors)`: passed when every grant's principal is on
  the allowlist and every sentinel id is listed; ids compare without regard to case.
- `AzureReaderListing(storage_account, container, *, arm_token, scopes=())`:
  `readers()` sends, with the bearer token *arm_token* returns for `ARM_SCOPE`,
  `GET https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments?$filter=atScope()&api-version=2022-04-01`
  at `{storage_account}/blobServices/default/containers/{container}` and at each of
  *scopes*, following each `nextLink` on that host and refusing one on another;
  then one `GET {storage_account}/providers/Microsoft.Authorization/roleDefinitions/{guid}`
  per distinct role. A 429 or 5xx is tried again after each of `_BACKOFF` (1, 2 and
  4 s); any other failure raises `AccessListingError`. A resource id that does not
  start with `/`, or a container that is empty or holds `/`, raises `ValueError`.
- `reads_blobs(permissions) -> bool`: whether one permission block's `dataActions`
  match `BLOB_READ` and none of its `notDataActions` does, `*` matching any run of
  characters, case ignored.

### `checks.py`

- `SealCheck`: the type `Callable[[EditionView], Iterable[Finding]]`, passed
  to `WorkingCopy.seal(checks=[...])`.
- `conf_status(attribute="CONF_STATUS", public=("F",)) -> SealCheck`: for
  each dataflow whose DSD has *attribute* and that has a `DataflowBinding`,
  reads `view.registry.obs(flow)` and yields one `Finding` (check name
  `CONF_STATUS_CHECK = "conf_status"`) per observation whose value is set
  and outside *public*.

### `bundle.py`

The module keeps its 0.11 name and describes editions. Imported from
`statcatlib.bundle`; nothing here is re-exported from `statcatlib`:

- `catalog_section(store: _Store, *, kind="data", lifecycle) -> dict`: builds the
  `statcat` manifest section (format 3: `format`, `kind`, `lifecycle`,
  `statcatlib_version`, `datasets`, `search`) from a working copy's open
  store. `lifecycle` (`"final"` or `"revisable"`, from 0.17) is the one the
  seal compacts by, which the release job's fold records for a product the
  Inventory does not record (`store.working_copy.fold_lifecycle`). `WorkingCopy.seal` passes it to
  `sdmxlib.edition.seal(..., extensions={"statcat": ...})`. It raises
  `ValueError` when `search/` holds files and no `index.json`.
- `check_catalog(root, manifest, reg, profile) -> list[Finding]`: the
  `statcat` check, with sdmxlib 0.80's `Check` signature, run as an extension
  of `sdmxlib.edition.check_edition`. It refuses a section format other than
  3, a `kind` outside `PRODUCT_KINDS`, a `lifecycle` other than `final` or
  `revisable` (an absent one passes: editions sealed before 0.17 have none),
  and a `structure` edition whose
  manifest binds a dataflow or whose store holds a dataflow, binding or
  partial.
- `structure_content(conn) -> list[Finding]`: one finding per `Dataflow`,
  `DataflowBinding` and partial artefact in `sdmx.artefact`. `check_catalog`
  runs it on a structure edition, and `WorkingCopy._precheck` on a
  structure working copy's store.
- `overlay_affects_search(overlay, types=None)`: lists overlay entries that
  change search results.
- `description_to_json(description) -> dict[str, object]` and
  `description_from_json(data) -> CatalogDescription`: the catalog-level
  fields of a `CatalogDescription` (identifier, title, description,
  publisher, homepage, language, licence, theme taxonomy, issued, modified)
  as JSON. `description_from_json` ignores unknown keys and raises
  `TypeError` on a field of the wrong JSON type.
  `description_from_root(extensions)` reads `extensions.statcat.description`
  of an Inventory root, or returns `None`.
- `ROOT_SECTION_FORMAT = "3"`: the root section's `format_version`.
- `STATCAT_SECTION = "statcat"`, `STATCAT_SECTION_FORMAT = 3`,
  `PRODUCT_KINDS = ("data", "structure")`.

- `check_catalog_no_index(root, manifest, reg, profile) -> list[Finding]`:
  `check_catalog` without the search index check, for an install with no
  `lancedb`. `store/blob.py`'s `_statcat_check()` chooses between the two.

### `upload.py`

`statcat upload`'s work, on the `blob` extra. It reads and writes nothing under
`catalog.json`, `inventory/` or `CURRENT`.

- `upload_edition(edition_dir, location, *, release_at=None) -> UploadReport`:
  refuses a store other than `AzureStore` (`TypeError`, naming W3) and a store with
  no `TokenIdentity` (`ValueError`), then runs `_upload` with an `AzureBlobRest`.
- `_upload(edition_dir, location, *, release_at=None, rest)`: the steps, over any
  store and any digest client with `head(path) -> RemoteFile | None`. Tests call it,
  through `tests/stores/_stores.py`'s `upload` and `record_uploaded`. A naive
  `release_at` raises `ValueError`.
- `UploadReport(edition_id, product_id, sent, kept, registered)`: `sent` and `kept`
  hold store paths, and `registered` is advisory, since a retried create can report
  false against its own earlier success.
- `UploadCheckError(findings)` (`RuntimeError`): any finding, an oversize file, a
  path leaving its directory, a rotted local file, a differing manifest, or a
  differing `pending/` document.
- `_pending_document` comes from `jobs.py`, which the release job's fold and GC
  decode against.
- `_lookup(rest, manifest)`: adapts `rest.head` to sdmxlib's `Lookup`, asking once per
  file. `_store_path(manifest, entry)` normalises `../data/`, which `obstore` refuses.
- `MAX_PUT_BYTES`: Put Blob's limit, 5,000 MiB.

### `diagnostics.py`

Imported from `statcatlib.diagnostics`; nothing here is re-exported from
`statcatlib`. The `statcat` commands in `cli/` call it. Both functions only
read.

- `verify_catalog(path) -> VerifyReport`: reads `FullInventory`; reports each
  root the read protocol skipped (`full.skipped`), each leaf that fails its
  hash or `validate_leaf`, and for every leaf entry a `manifest.json` whose
  SHA-256 differs from the entry's, or, when it matches, each filled routing
  field or artefact record `sdmxlib.inventory.verify_entry` finds differing,
  with the leaf as the finding's file, then runs
  `check_edition(at="publish", structures=..., extensions={"statcat":
  check_catalog})`, with `structures=` from `_structures_for`: every recorded
  edition of every structure product for a data edition, the other
  structure products' live and embargoed editions for a structure edition,
  `None` when empty. Last it lists the stale data editions
  (`cross_product.stale_editions`, re-exported here as `StaleEdition`).
  `VerifyReport` has `root`, `seq` (`None` before the first version),
  `editions`, `findings` (`VerifyFinding(file, detail)`, the file a path
  from the catalog directory), `stale` (no finding; `ok` ignores it) and
  `ok`. A path that is no directory, a legacy
  layout, a missing or malformed `catalog.json`, or no valid root raise
  `CatalogUnreadableError`.
- `doctor(path) -> DoctorReport`: `layout` (`catalog`, `legacy`, `unknown`)
  and `items`, each a `DoctorItem(topic, level, detail)` with level `info` or
  `warning`; `warnings` and `about(topic)` filter them. Topics: `synced
  folder`, `layout`, `catalog.json`, `CURRENT`, `inventory`, `stale data
  editions` (an item per stale data edition and URN: `info` for a live
  edition, `warning` for an embargoed one), `partials with no structure
  home` (a `warning` per live or embargoed data edition and partial whose
  URN no live or embargoed structure edition holds full,
  `cross_product.homeless_partials`), `working copy`
  (`list_editions`), `journal` (empty entries included), `unrecorded edition`
  (sealed, and neither named nor tombstoned by the newest version) and
  `leftover file` (`*.tmp` in `inventory/` and `.*.tmp` in the root).
- `synced_folder(path) -> str | None`: the sync client whose folder name
  appears in the path or its resolved form.

### `query.py`

- Filter constructors: `has_code`, `descendants_of`, `within`,
  `has_dimensions`, `distribution`, `has_service`, `publishes`, `serves`,
  `has_spatial`, `has_spatial_uri`, `has_keyword`, `title_contains`,
  `description_contains`. `has_spatial` also matches a region's concept
  label; `has_spatial_uri` filters on a spatial facet key that is a URI.
- Filter nodes with `to_sql(ctx) -> (fragment, params)`: `MapFilter`,
  `ExistsFilter`, `SdmxCodelistFilter`, `SdmxCodeFilter`,
  `SdmxDimensionFilter`, `PositionalFilter`.
- Data holders compiled by other nodes: `FieldFilter` (`field`, `op`,
  `value`), `MapCondition` (inside `ExistsFilter`), `CodeDescent` (inside
  `SdmxCodeFilter`).
- Outside `query.__all__`, used by `store/`: `CompileContext` (join state
  for filter compilation, subclassed as `_QueryContext` in `store/store.py`)
  and `label_norm_sql(expr)` (SQL that normalizes labels: accents,
  punctuation, whitespace).

### `expr.py`

- `each(...)` and the expression nodes used by scope `.enrich()`,
  `.filter()` and `.sort_by()`. `statcatlib` re-exports `each`, `Expr`,
  `TraversalExpr`, `AliasedExpr`, `CountExpr` and `ExistsExpr`; `SumExpr`,
  `IdsExpr`, `DistinctExpr`, `FilteredExpr` and `CountDistinctExpr` are
  imported from `statcatlib.expr`.

### `urn.py`

- `codelist`, `dataflow`, `data_structure`, `concept_scheme(agency, id,
  version="1.0")` and `parse(urn)`: wrappers over sdmxlib's `SdmxUrn`.

### `testing.py`

- `temporary_catalog()`: a context manager yielding a writable `Catalog`
  from `Catalog.create` in a temporary directory, deleted when the block
  ends. It replaces 0.11's `Catalog(":memory:")`.
- `synthetic_catalog(path, shape, *, product_id="synthetic", seed=0) ->
  Edition`: creates a catalog at *path* with one sealed, live edition. It
  builds `sdmxlib.testing.synthetic_edition(..., seal=False)` into the
  product's directory, resumes it as a working copy, stores one
  `AggregateDataset` per dataflow (`prepare_sdmx(wc.registry)`), and seals.
  The seal builds the search index when `statcatlib[search]` is installed.
- The CODR generator (design decision O6): `CodrShape(geography_codes=6000,
  geography_depth=5, classification_codes=(900, 500, 300),
  observations=2000)`; `STRUCTURE_PRODUCTS` (`structure-geo`,
  `structure-classifications`, `structure-concepts`); `CODR_AGENCY`;
  `codr_structures(catalog, *, seed=0, shape=...) -> dict[str, Edition]`,
  which builds the structures from the seed alone (agency `CODR`, version
  `1.0`, names `"<id> en"` and `"<id> fr"`) into a scratch `LocalRegistry`,
  fills the three structure working copies by URN and seals them in one
  `Catalog.seal`, raising `RuntimeError` on a refusal; and `codr_product(catalog,
  product_id, *, tables=1, seed=0, shape=...) -> Edition`, which seals the
  structure products first unless all three are live, writes one DSD and
  dataflow per table and the rows from `(seed, product_id)`, fills with
  `ingest_dataflows(..., partials=True)`, indexes `CL_GEO`'s codes with an
  `fts_only` `CodelistIndexPolicy` when lancedb is installed, and seals.
  `codr_product` is `codr_working_copy(...).seal()`: `codr_working_copy`
  takes the same arguments and returns the filled, indexed working copy,
  unsealed. `codr_corpus(catalog, groups=(CorpusGroup(7000),), *, seed=0,
  shape=..., embargoed=0.1, on_sealed=None) -> list[str]` seals each
  `CorpusGroup(products, tables=1)`'s products, named by
  `corpus_product_id` (`codr-<tables>t-<index:05d>`), through
  `codr_product`, skipping products the Inventory records and discarding a
  working copy a crash left; it seals the share `corpus_embargoed` picks (a
  SplitMix64 draw per product id) between two `CatalogAdmin.set_embargo`
  versions, turns the embargo off first and leaves it off, and returns the
  products it sealed. `bench/corpus.py` (GH-124) drives it.
  `structure-classifications` holds `CL_CLASS_*`, `CL_UNIT` and the
  SDMX-agency `CL_OBS_STATUS`, so every codelist a table cuts has a
  structure home (A5). The draws are SplitMix64 seeded by SHA-256, so they
  do not depend on Python's `random`.
- `inventory_corpus(writer, products=7000, *, embargoed=0.1, artefacts=False,
  batch=500, seed=0) -> Root` writes `catalog.json` and one `try_commit` per
  `batch` products, with no manifest behind them, so no rule runs. The leaves
  come from sdmxlib's `record`, with hashed ids, one dataflow URN, `pull_bytes`
  13,131,776 and, with `artefacts=True`, 10 artefact records. Commits start at
  a fixed time and step one millisecond, so a seed writes the same bytes for a
  given actor and statcatlib version. A catalog with a `catalog.json` raises
  `FileExistsError`. `bench/inventory_read.py` (GH-98) drives it.

## Invariants

- Filters bind every value as a `?` parameter (e.g. `d.{col}[?] = ?` with
  params `[key, value]`). `CompileContext.join_params` keeps insertion order.
- Column names in the SQL text come from code. `distribution(**kwargs)` and
  `has_service(**kwargs)` map keys through `_DISTRIBUTION_KWARG_COLS` and
  `_SERVICE_KWARG_COLS` and raise `ValueError` on any other key (GH-71).
  `publishes` and `serves` map theirs through `_DATASET_KWARG_COLS` and drop
  unknown keys.
- Positional filters are ANDed. The keyword arguments of one `distribution()`
  call must match the same distribution row; two calls require two matching
  rows.
- statcatlib owns `CompileContext`. sdmxlib receives a `SqlRelation`
  (`sl.q.hierarchy_applicability`) and shares no compile state.
- Hierarchy descent reads the `sdmx.hierarchy_applicability` relation through
  two `LEFT JOIN`s on `dataflow_urn`: an existence relation and an unnested
  pairs relation. Where no HierarchyAssociation applies, it falls back to the
  codelist's parent chain in `sdmx.code.ancestor_ids`; that fallback is the one
  hierarchy rule statcatlib owns. Descent is strict unless `include_self=True`.
- `WorkingCopy.seal` computes `catalog_section` after it rebuilds the search
  index and before it closes the store and registry; it then seals. The
  manifest records each file's digests, so a later write fails
  `check_edition`'s files check.
- `Catalog.open` writes nothing to the catalog. Over a `BlobLocation`, `Product.view`
  writes to `cache_dir` alone: the live edition's files, by a rename of a verified,
  fsynced staged directory, and the receive stamp. No embargoed edition's files and no
  leaf bytes reach it. No `Catalog` or `Product` method takes or returns a path
  inside the catalog.
- A library call never takes a credential from the environment: `Catalog.open` over a
  store takes a `BlobLocation` the caller built. `catalog.py` and `store/blob.py` read
  no environment variable.
- Only `CatalogAdmin` and, on a catalog in object storage, the release job
  (`release.ReleaseJob`) change the Inventory, apart from a seal's record and
  the journal repair. `CatalogAdmin` runs over a catalog directory or a `BlobLocation`,
  `prune` on a directory only. No attribute of `Catalog`, `Product` or `EditionView`
  returns a `CatalogAdmin`, an `EmbargoReader` (spec §6.4) or an embargoed,
  superseded or withdrawn edition. `Catalog`'s reads (`products`, `Product.view`,
  `Product.editions`) go through a `LiveInventory`.
- Every `CatalogAdmin` rule is re-evaluated against the head each commit
  lands on: a rollback whose `if_live` is no longer live, a release that is
  no longer a fast-forward, and a seal record computed before the embargo was
  turned on are each refused or recomputed.
- `EmbargoReader` writes nothing to the catalog. Over a `BlobLocation` it writes to its
  `cache_dir` alone: the files of the embargoed and live editions it views, and their
  receive stamps under `<cache_dir>/<catalog_id>/embargo-verified`. That `cache_dir`
  holds embargoed bytes, so a deployment gives the reader its own. A superseded or
  withdrawn edition, or one the Inventory does not record, is refused before any read
  under it.
- `Product.view` passes the Inventory's `manifest_sha256` to
  `sdmxlib.LocalRegistry.edition`, which hashes the bytes of
  `manifest.json` it parses. A missing, malformed or different manifest
  raises sdmxlib's `ManifestError`, which `view` re-raises as
  `IncompatibleCatalogError`, chained. A local catalog's message names
  `statcat verify-catalog`, and a blob catalog's names the cached directory to
  delete, so the edition is pulled again. Every view a `Catalog` returns shares one
  `sdmxlib.EditionEngine`, and `Catalog.close` closes them all.
- A seal check only reads: `seal` runs every check against `wc.view()`
  before writing anything, and any finding raises `SealCheckError`.
- `Catalog.seal` seals and checks the batch's structure products before
  any data product, runs every data pre-check before any data seal, and
  writes at most one Inventory version; a batch whose products are all refused writes
  none. A refusal in one product never stops another's. A crash during the
  batch leaves each sealed, unrecorded edition's journal entry, so its
  product's next `working_copy()` records it, in a version of its own.
- `ReleaseJob.run` writes nothing under `products/` or `pending/` and deletes
  nothing in the store. It releases no edition while the access result failed or the
  head's `embargo` is on, and each retry of the release commit checks both again on
  the new head. Every entry the fold records is `embargoed`, carries its manifest's
  SHA-256 as read and its artefact records, and passed every check of its own; no
  entry goes live whose manifest differs from the one its entry records. A refusal
  in one registration or product never stops another's, and a failure of one step
  never stops the next. A run against an unchanged catalog writes nothing. A dry
  run writes nothing, `CURRENT` included.
- `check_access` fails closed: a listing error, a missing sentinel and a principal
  off the allowlist each fail it, and `AzureReaderListing` sends its token to
  `management.azure.com` alone.
- `collect_garbage` deletes nothing when it cannot read the Inventory back through
  the window (`GcStoppedError`), nothing under the `data/` of a product whose
  manifest or registration it cannot read or verify, no file a kept or registered
  manifest lists, no `data/` file younger than `upload_grace`, no edition directory
  whose tombstone is within the window or that no Inventory version read records,
  and nothing under `inventory/` or `history/`. A directory loses `manifest.json`
  before any other file, and keeps every file when it keeps its manifest.
- `CatalogAdmin.prune` never retires or deletes anything of a product with
  an unsealed edition or a seal journal entry, deletes only the directories
  of tombstoned editions, and deletes no Inventory root, leaf or tombstone.
  A dry run, the default, changes nothing.
- `MapCondition`, `MapFilter` and `has_spatial` pass their language tag
  through `sdmxlib.normalize_tag`.
- `Expr` nodes are frozen attrs objects and run no SQL themselves.

## Side Effects

- `Catalog.create` creates the directory and `catalog.json`, logs the path
  at INFO, and writes and removes a `.statcat-link-probe.*` file and its hard
  link.
- `Product.view` attaches the edition to the catalog's engine
  (`LocalRegistry.edition(path, engine=, manifest_sha256=)`), and
  `EmbargoReader.view` to the reader's; over a `BlobLocation` each writes the pulled
  edition and its stamp under its own `cache_dir`. `Product.working_copy` writes as
  `store/EGM.md` describes.
- `synthetic_catalog` writes a whole catalog: `catalog.json`, sdmxlib's
  synthetic edition, the catalog rows, the search index when lancedb is
  installed, the manifest and Inventory version 1. `temporary_catalog`
  deletes its directory on exit.
- Each `CatalogAdmin` change writes one Inventory version (a root, and for a
  product change one leaf) and `CURRENT`; over a `BlobLocation` it sends one
  anonymous `HEAD` for the storage clock unless given `clock=`.
  `advance_current` writes `CURRENT` alone, or nothing. `prune(dry_run=False)` writes at
  most one, of kind `retire`, deletes edition directories and `data/` files,
  and logs each deleted path at INFO and each failed deletion at WARNING on
  the `statcatlib.admin` logger.
- `diagnostics.verify_catalog` opens each edition in a DuckDB engine of
  `check_edition`'s own; neither it nor `diagnostics.doctor` writes a file.
- These modules make no network calls, except `upload.py`, `admin.py`, `gc.py`,
  `release.py`, `catalog.py` and `embargo.py`, which reach a store only through the
  `BlobLocation` they are given, and `access.py`, whose `AzureReaderListing` sends
  GET requests to Azure Resource Manager. `jobs.py` reads
  the one file it is given.
- `ReleaseJob.run` writes at most three Inventory versions (kinds `seal`, `release`
  and `retire`) and `CURRENT`, creates and deletes one temporary directory holding
  the pulled files of the editions the fold's reference check reads, and logs each
  event on the `statcatlib.release` logger at WARNING, or ERROR for `step.failed`,
  `access.mismatch` and `access.unchecked`. It sends one anonymous `HEAD` for the
  storage clock at its start and at each of its three committing steps, unless its
  storage has a `clock=`.
- `gc.collect_garbage` writes nothing. It deletes under `products/` and `pending/`
  only, unless `dry_run`, and logs each event on the `statcatlib.gc` logger at INFO
  (`gc.planned`, `gc.deleted`), WARNING (`gc.failed_closed`, `gc.unregistered`,
  `gc.forecast_deviation`) or ERROR (`gc.delete_failed`). It sends one anonymous
  `HEAD` for the storage clock unless its storage has a `clock=`.

## Verification

- Run: `just ci` (unit tier alone: `uv run pytest -m unit`).
- `EmbargoReader`: `tests/test_embargo_reader.py` (a local catalog), the
  `TestNoOperationalReach` and blob-extra tests in `tests/test_public_api.py`, and,
  against Azurite, `tests/stores/test_embargo_reader.py`.
- The release job: `tests/test_release_fold.py`, `tests/test_release_step.py` and
  `tests/test_release_job.py` (every step over a `MemoryStore`, with the catalog of
  `tests/_release.py` and the fakes of `tests/_fakes.py`),
  `tests/test_access_listing.py` (`AzureReaderListing` through `pytest-httpx`, and
  `check_access`), `tests/test_cli_release.py` and, against Azurite,
  `tests/stores/test_release_job.py` (the 200-table release, the reference check, a
  crash, a fork and its discard, a restore).
- GC: `tests/test_gc.py` (every rule over a `MemoryStore`, with the helpers in
  `tests/_gc.py`), `tests/test_cli_gc.py` and, against Azurite,
  `tests/stores/test_gc.py`; `decode_registration` in `tests/test_jobs_config.py`.

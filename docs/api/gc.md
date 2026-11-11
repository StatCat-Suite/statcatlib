# The GC job

A catalog in object storage keeps every file an edition ever listed until
something deletes it. `statcat gc`, the GC job, deletes what no recorded,
tombstoned or registered edition lists: the directories of editions retired
longer than the GC window, `data/` files no manifest lists, and old
registrations in `pending/`. It writes no Inventory version, and it is the only
principal that deletes under `products/` (specification §10.1, §11.4). A local
catalog runs `statcat prune` in its place ([Catalog
administration](admin.md#pruning-old-editions)).

GC runs on Azure only, as `statcat upload` does, under its own workload
identity: Storage Blob Data Contributor on the catalog's container, which reads,
lists and deletes. It reads `catalog.json`, `inventory/` and `CURRENT`, and
lists, reads and deletes under `products/` and `pending/` only. It never writes
or deletes under `inventory/` or `history/`.

```bash
statcat gc az://catalogs/stc --config /etc/statcat/jobs.toml
```

The command reads the account from `AZURE_STORAGE_ACCOUNT_NAME` and the identity
from `DefaultAzureCredential`, with `statcat upload`'s environment rules
([Object storage](blob.md#the-command)). `--config` names the [jobs
configuration](jobs.md); GC reads its `[retention]` table and refuses to start
when a retention inequality fails.

## What one run does

1. **Start.** It loads and checks the configuration, logs `jobs.config` with the
   file's SHA-256, and reads the storage clock. The cutoff is `now - gc_window`.
2. **The Inventory.** It reads the head by the reader protocol, then each older
   root by `seq` until it has read `keep_roots` roots and the newest root created
   before the cutoff. On a catalog that has opted in to expiry it stops at
   `HISTORY.expired_through`. Each distinct leaf is read once. The kept editions
   are every entry of every leaf read, in any state, and every tombstone in those
   roots retired at or after the cutoff.
3. **The registrations.** It lists `pending/` after the Inventory, so a
   registration the release job folds meanwhile still protects its files. Each
   registration is read as the fold reads it (`statcatlib.jobs.decode_registration`).
4. **The manifests.** It reads the manifest of every kept and every registered
   edition on 16 threads. A kept edition's manifest must match the SHA-256 its
   newest entry records; a registered edition's must decode and name its own
   product and edition.
5. **The listing.** It lists `products/` and plans the deletions below.
6. **The deletion.** It deletes on 16 threads: registrations and `data/` files
   first, then each directory's `manifest.json`, then the rest of the directory.
   A failed deletion is logged and left for the next run.

| Deleted | When | `reason` |
|---|---|---|
| Every object of `products/<p>/<e>/` | The edition was tombstoned before the cutoff and no root read records it. `manifest.json` goes first, so a partly deleted directory never reads as an edition; the next run finishes it. | `retired` |
| `products/<p>/data/<file>` | No kept or registered manifest lists it, it was last modified before `now - upload_grace`, and the product has not failed closed | `unreferenced` |
| `pending/<p>/<e>.json` | The head records the edition, in a leaf or in `retired`, and the registration was last modified before the cutoff | `folded` |
| `pending/<p>/<e>.json` | The Inventory does not record the edition, and its `edition_id` time is older than `tombstone_window`. The fold refuses such a registration, and it protects no file. | `expired-registration` |
| The registration, manifest and directory of an edition named by `--discard` | An operator decided to drop an edition the Inventory does not record | `discarded` |

GC keeps everything else. In particular it keeps:

- every file a registered edition's manifest lists, before the release job
  folds it;
- every file the editions of the newest `keep_roots` roots list, however old;
- every edition directory whose tombstone is within the GC window;
- every `data/` file younger than `upload_grace`, which covers an upload still
  sending its data files;
- every edition directory that nothing records or registers, which it reports as
  `gc.unregistered`.

## Safety rules

GC deletes nothing when it cannot tell what the Inventory keeps. Each of these
stops the run with `GcStoppedError` before any deletion, and the command exits 1
with `step.failed`:

- no `catalog.json`, or an opted-in catalog with no `inventory/HISTORY`;
- a head the reader protocol cannot read, or a root the reader skipped as
  invalid;
- a missing or invalid root above `expired_through` on the walk back (spec
  §11.4); a catalog that has not opted in has nothing expired, so any missing
  root stops the run;
- a missing leaf, or one that does not hash to its name.

GC fails closed per product when it cannot read or verify one manifest: a kept
edition's manifest that is missing or differs from the Inventory's SHA-256, a
registered edition's that is missing, cannot be decoded or names another
edition, an unreadable registration, or a tombstone in the window whose entry no
root read holds. It then deletes nothing under that product's `data/`, logs
`gc.failed_closed` naming the edition, and goes on with the other products. The
exit code stays 0.

GC refuses a `--discard` of an edition the Inventory records, live, embargoed,
superseded, withdrawn or tombstoned, and exits 2 before any deletion.

## Dry runs and forecasts

`--dry-run` plans and logs the plan, as `gc.planned` lines, and deletes nothing.
Its standard output is a forecast: `--forecast FILE` takes it, and the run
deletes only the paths in both plans. Each path in one plan only is logged as
`gc.forecast_deviation` and kept. A directory whose `manifest.json` is not
deleted keeps the rest of its files, so a forecast that leaves out a manifest
keeps the whole directory.

```bash
statcat gc az://catalogs/stc --config jobs.toml --dry-run > plan.jsonl
# review plan.jsonl
statcat gc az://catalogs/stc --config jobs.toml --forecast plan.jsonl
```

Without `--forecast`, a run deletes its own plan, and `gc.delete_failed` is the
alert for a deletion off the plan.

## The deletion log and the events

Standard output holds one JSON line per event, and nothing else; standard error
holds the human lines (`FAIL`, `FAILED-CLOSED`, `UNREGISTERED`, `DEVIATION`,
`ERROR`) and the summary. The platform's log store keeps standard output, since
GC holds no right to write a log into the catalog (S22).

A deletion log line has these fields:

| Field | Meaning |
|---|---|
| `event` | `gc.planned`, `gc.deleted` or `gc.delete_failed` |
| `path` | The object, from the catalog root |
| `reason` | `retired`, `unreferenced`, `folded`, `expired-registration` or `discarded` |
| `edition` | `<product>/<edition>` for a directory or a registration, else `null` |
| `error` | The store's error, on `gc.delete_failed` only |

```json
{"event":"gc.deleted","path":"products/14100287/data/obs/df/ducklake-0193.parquet","reason":"unreferenced","edition":null}
```

Every other event is an alert with `event`, `severity`, `at` (the storage
time), `catalog`, and, where the event names one, `product`, `edition`,
`reason` and `detail`.

| Event | Severity | Runbook |
|---|---|---|
| `jobs.config` | info | `detail.sha256` is the configuration's SHA-256. Compare it with the release job's; two values mean the jobs run different copies of the ConfigMap. |
| `gc.failed_closed` | warning | GC kept the product's `data/` because `reason` names a manifest or registration it cannot read or verify. For a registration, look at its upload: rerun it, or drop the edition with `--discard`. For a kept edition, compare the manifest with the leaf entry's `manifest_sha256` and restore the manifest from the backup; never edit it. |
| `gc.unregistered` | warning | An uploaded edition has no registration and no Inventory version records it: its upload died before the last step. Rerun the upload, which registers it and sends any `data/` file GC deleted, or drop it with `statcat gc --discard <product>/<edition>`. `detail.manifest` says whether its manifest arrived. |
| `gc.forecast_deviation` | warning | `detail.path` is in `detail.in` (`forecast` or `plan`) only and was kept. A path in the plan only appeared after the dry run, such as a file that aged past the grace period; one in the forecast only went in between. Review, then run the next dry run. |
| `gc.delete_failed` | error | The deletion log line carries the store's error. The next run tries the path again; a repeated failure on one path needs the store's diagnostics. |
| `step.failed` | error | The run stopped before deleting anything, or the store failed; `detail.error` says why. A missing root above `expired_through` needs the backup; a skipped root needs `statcat verify-catalog` on a copy; a bad `CURRENT` is moved by `statcat advance-current`. |

The command exits 0 when the plan ran, failed-closed products and unregistered
editions included; 1 when a deletion failed, the configuration is refused or the
run stopped; and 2 on a usage error, an unreadable `--forecast`, a `--discard`
that is no `<product>/<edition>` or names an edition the Inventory records, and a
store other than Azure Blob Storage.

## Discarding an edition

An upload the release job will never fold stays in `pending/` until an operator
decides: each registration of a `pending-fork`, or one the fold refuses for its
files, manifest, chain, lifecycle or `final` rule. `--discard` deletes the
edition's registration, then its manifest, then the rest of its directory, each a
logged deletion with `reason` `discarded`. Its `data/` files then fall to the
`unreferenced` rule in the same run.

```bash
statcat gc az://catalogs/stc --config jobs.toml --discard 14100287/0193a1b2-0000-7000-8000-000000000001
```

An edition the Inventory records is withdrawn with `statcat admin cancel` instead
([The `statcat admin` commands](admin.md#the-statcat-admin-commands)), and GC
deletes its directory once it is retired and past the window.

## Scheduling

Run GC nightly, at a time with no release, under `concurrencyPolicy: Forbid`. A
run at 7,000 products, a 35-day window and ten versions a day reads about 350
roots and 21,000 manifests and lists about 400,000 objects: 3 to 6 minutes on 16
threads, an estimate no real store has measured. Each request may take five
minutes. Before a whole-catalog restore, stop GC until the restore writes the
tombstones that keep the discarded roots' editions.

## The library call

```python
from statcatlib.gc import collect_garbage
from statcatlib.jobs import load_jobs_config

config = load_jobs_config("/etc/statcat/jobs.toml")
report = collect_garbage(location, config.retention, dry_run=True)
report.planned        # each a Deletion(path, reason, edition)
report.failed_closed  # product to the reason
```

`collect_garbage(location, policy, *, storage=None, dry_run=False, forecast=None,
discard=())` takes any `BlobLocation`; the command builds one over an
`AzureStore`. Each event is a record on the `statcatlib.gc` logger, and the
command prints each as one JSON line ([reference](../reference/gc.md)).

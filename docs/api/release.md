# The release job

`statcat upload` leaves a sealed edition in a catalog's `pending/` inbox, where no
reader sees it. `statcat release`, the release job, records it in the Inventory
and publishes it on schedule. One run does three things, each in at most one
Inventory version:

- **the fold** records every `pending/` registration that passes its checks as an
  `embargoed` entry;
- **the release** makes every `embargoed` entry whose `release_at` has come
  `live`, once the access precondition passes;
- **the retirement** tombstones the `superseded` and `withdrawn` entries older
  than `retire_after`, which [`statcat gc`](gc.md) later deletes.

The release job is the only writer of Inventory versions on a server
(specification §10.1, §11.2). It runs on Azure only, as `statcat upload` does,
under its own workload identity, the release identity. A local catalog seals,
releases and prunes through `Catalog` and [`CatalogAdmin`](admin.md) in its place.

```bash
statcat release az://catalogs/stc --config /etc/statcat/jobs.toml
```

The command reads the account from `AZURE_STORAGE_ACCOUNT_NAME` and the identity
from `DefaultAzureCredential`, with `statcat upload`'s environment rules
([Object storage](blob.md#the-command)). `--config` names the [jobs
configuration](jobs.md). The release job reads its `[retention]` and requires its
`[access]`, whose `container` and `storage_account` must name the catalog's own.

## The release identity

| It needs | For |
|---|---|
| Read and list on `products/` and `pending/` | The fold's registrations, manifests, file digests and reference checks |
| Read and write on `catalog.json`, `inventory/` and `CURRENT` | Every version it writes |
| `Microsoft.Authorization/roleAssignments/read` and `Microsoft.Authorization/roleDefinitions/read` at the storage account | [The access precondition](#the-access-precondition), through one custom role |

It holds no account key and no SAS, no Reader role above the account, and no
Microsoft Graph permission. It never writes or deletes under `pending/` or
`products/`. Specification §10.1 lists every principal's rights.

## Creating the catalog

A new catalog needs `catalog.json` before its first upload. Run this once, under
the release identity, at an empty prefix:

```bash
statcat admin create az://catalogs/stc
```

The command prints the new `catalog_id` and writes `catalog.json` alone
([Creating a catalog](admin.md#creating-a-catalog)). It exits 1 and writes nothing
when the prefix holds a `catalog.json`, or holds objects under `inventory/`,
`products/` or `pending/` without one. `statcat upload` then registers editions in
`pending/`, and the first release run that folds one writes Inventory version 1 with
the embargo off. Until `catalog.json` exists, `statcat release` exits 1 with `ERROR
az://<container>/<prefix>/ has no catalog.json`.

## What one run does

A run is short and idempotent. Each step that changes the Inventory commits its own
version, and a commit that loses a race plans the step again on the new head. A
step that finds nothing to do writes nothing, so a run against an unchanged catalog
writes nothing.

| Step | What it does | Version |
|---|---|---|
| Start | Loads and checks the configuration and logs `jobs.config`. Refuses a catalog that has opted in to expiry. Reads the storage clock, and refuses to run when this machine's clock is more than 30 s ahead of it. | none |
| Advance | Moves `CURRENT` to the newest root, forward only, and logs `current.behind` when it moved. | `CURRENT` only |
| Access | Runs [the access precondition](#the-access-precondition) and keeps its result. | none |
| Fold | Records the passing registrations as `embargoed`. | kind `seal` |
| Release | Writes [the refusal record](#the-refusal-record). When the access check passed and the catalog's embargo is off, releases what is due. | kind `release` |
| Retire | Tombstones the `superseded` and `withdrawn` entries whose `state_changed_at` is older than `retire_after` by the storage clock, except a structure edition that a live or embargoed data edition's `partial_of` cites. | kind `retire` |

The fold, the release and the retirement each catch their own failures: a store
fault, a manifest of an entry recorded before statcatlib 0.17 that cannot be read,
or eight lost races in a row. The step logs `step.failed`, the run goes on to the
next step, and the command exits 1. After a fault in the fold, the release step
still releases what an earlier run folded. The next scheduled run repairs any
step.

Each step reads the storage clock once, and its rules and its version record that
time. The catalog lock and expiry (`--expire`) belong to a later release (Phase 5
Part 6), and until then the command refuses a catalog whose `catalog.json` is
format 2.x.

## The fold

The fold reads every leaf of the head, lists `pending/`, and judges each
registration the head does not record, in the order of its path. A registration
whose edition the head records, in a leaf or as a tombstone, is skipped by its path
alone and never read, so a rerun reads no folded registration again. The checks run in this order,
and the first that fails is the registration's refusal:

| Rule | The registration is refused when |
|---|---|
| `pending-document` | Its bytes differ from the document `statcat upload` writes ([the format](../catalog-format.md#15-the-pending-registrations)) |
| `too-old` | Its `edition_id`, a UUIDv7, carries a time older than `tombstone_window`, so a registration a backup restored cannot bring back withdrawn data |
| `manifest` | `products/<p>/<e>/manifest.json` is absent, cannot be decoded, names another product or edition, or lists a path outside its product |
| `canonical-form` | The manifest's `canonical_form` is another than this sdmxlib's |
| `files` | A listed file is missing from the store, or its size or kept MD5 differs from the manifest's |
| `lifecycle` | The `statcat` section names another lifecycle than the leaf's, or the product is new and the section names none ([the table](../catalog-format.md#31-versions)) |
| `kind` | The section names no product kind, or another than the leaf's |
| `chain` | The manifest's `previous` is neither recorded nor tombstoned for the product |
| `final` | The product is `final` and the edition's `content_fingerprint` differs from the live edition's at its schema, else the live edition's at any schema, else its `previous`'s. A `previous` that is retired, with no live edition, is refused too. |
| `references` | The edition has no binding, and `check_edition(at="receive")` finds a reference that resolves in no structure edition |
| `store` | The store failed while the registration was checked |

**The reference check.** An edition with no binding holds no observations, and its
references may resolve in another structure product, so upload defers them. The
fold pulls the edition's catalog files and those of every other structure
product's live and embargoed editions to a temporary directory, with the fold's
own structure editions in place of their products' recorded ones, and runs
`check_edition(at="receive", remote=, structures=)`. The fold's structure editions
are checked together, and the check repeats without each refused one. The
directory is deleted when the fold ends.

**The record.** The passing registrations are recorded in one version, per product
in `edition_id` order:

- Two or more passing registrations of one product with the same `previous` and
  schema version are each refused with `pending-fork`, naming the others. An
  operator discards all but one with [`statcat gc
  --discard`](gc.md#discarding-an-edition), and the next run folds the one left.
- sdmxlib's `record` writes the entry `embargoed`, with the registration's
  `release_at`, the manifest's SHA-256 and its artefact records and routing
  fields. Its refusals keep their rule: `duplicate`, `tombstone`, `kind`.
- An `embargoed` entry with the same `previous` and schema version is withdrawn in
  the same version: the new edition is a correction made under embargo.
- The cross-product rules run on the version. A product they refuse
  (`full-artefacts`, `structure-home`), and a product whose manifest changed since
  the fold read it (`manifest`), loses its whole change, and the rules run again
  on the rest.
- An edition whose reference check was given a fold edition that ends up
  unrecorded is refused with `references`. The next run checks it again.

**Held release times.** Two kinds of registration are recorded with `release_at:
null`, so only `statcat admin release` publishes them:

- one whose `release_at` is earlier than its own last-modified time, logged
  `fold.release_at_past`: a late upload whose time has passed;
- on a catalog whose restore epoch is above 0, one last modified before the first
  root of that epoch, logged `fold.restored`: a registration that predates the
  restore, whose edition an operator may have cancelled since.

A refused registration stays in `pending/` and is refused again by every run until
its cause is fixed or an operator discards it. GC deletes a `too-old` registration by itself.

## The release

The release step runs sdmxlib's `release_due` over every leaf at the storage
clock's time, then the cross-product rules, and writes every accepted product in
one version.

- A structure edition and the data editions cut from it go live in the same
  version when both are due. A data edition whose structure edition is not due
  stays `embargoed`, refused with `publish-order`.
- An edition built on an edition that is no longer live, after a rollback, is
  refused with `release_due`'s message, which names both editions. Its `rule` is
  `null`.
- An entry whose manifest is absent, or differs from the SHA-256 the Inventory
  records, is refused with `manifest`, so no edition goes live on a manifest the
  Inventory does not record (specification §6.5 item 2).
- A refusal in one product never holds another product. A product with several
  tables keeps them all embargoed.

**The embargo holds automatic releases.** While the head's `embargo` is `true`, the
release step releases nothing and logs `release.held`. `statcat admin embargo <url>
on --reason …` is therefore the stop switch, and `statcat admin release` still
publishes one edition by hand ([the commands](admin.md#the-statcat-admin-commands)).
A server catalog runs with `embargo: false`. An embargo turned on while a release
commit is in flight wins: the commit's retry plans again and holds.

**A failed access check skips the release.** The fold and the retirement still run,
since neither makes anything public.

A registration that is due when the fold records it is released by the same run,
in a second version, and logged `release.unprepared`: no pre-materialisation ran
between the two, so its first requests miss the result cache.

## The refusal record

The newest run's refusals live in the newest root, in `extensions.statcat.release`,
beside the catalog's description ([the format](../catalog-format.md#12-the-inventory-roots-statcat-section)):

```json
{"refused": [
   {"product_id": "14100287", "edition_id": "01a18e76-…", "step": "release", "rule": null,
    "reason": "14100287: 01a18e76-… is built on 01a17f00-…, but the live edition at schema 2.52 is 01a18e63-…. Discard it, then build from 01a18e63-….",
    "since": "2026-11-12T13:30:05.120Z"}
 ], "omitted": 0, "epoch_started_at": null, "restore_epoch": 0}
```

The release step writes the record whenever it differs from the head's, also when
the release is held or skipped. A version that changes the record alone has kind
`release` and an empty change list. A run with the same refusals writes nothing,
and `since` keeps the time of the first run that refused the same product, edition
and step. The record holds at most 500 refusals, the oldest `since` first, and
`omitted` counts the rest; the events name them all. When a fold fails, the record
keeps the head's fold refusals.

```python
from statcatlib.release import current_refusals

for refusal in current_refusals(full):  # an sdmxlib FullInventory
    print(refusal.product_id, refusal.edition_id, refusal.step, refusal.rule, refusal.since)
```

The record names embargoed editions, as leaves and roots do, so it stays inside
the catalog's access boundary.

## The access precondition

Any principal that holds a role reading blobs in the catalog's container reads
embargoed bytes (specification §10.1). Before a release step runs, the job lists
the role assignments that apply to the container, inherited ones included, reads
each role definition, and keeps the assignments whose role grants the data action
`Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read`. A `*` in a
data action matches any run of characters, and a matching `notDataActions` entry
in the same permission block withholds it. Storage Blob Data Reader, Contributor
and Owner each read blobs, and so does any custom role with the data action.

The check passes when every kept assignment's principal object id is in `[access]
allowlist`, and every assignment id in `[access] sentinels` is in the listing. A
group is one principal: its own object id is on the list or off it, and its members
are never read.

The release identity holds one custom role, assigned at the storage account and
nowhere else:

```json
{"Name": "statcat access check", "IsCustom": true,
 "Actions": ["Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleDefinitions/read"],
 "NotActions": [], "DataActions": [], "NotDataActions": [],
 "AssignableScopes": ["/subscriptions/…/resourceGroups/…/providers/Microsoft.Storage/storageAccounts/acct"]}
```

The job sends two kinds of request to Azure Resource Manager, with a token for
`https://management.azure.com/.default`:

1. `GET {account}/blobServices/default/containers/{container}/providers/Microsoft.Authorization/roleAssignments?$filter=atScope()&api-version=2022-04-01`,
   following every `nextLink`;
2. `GET {account}/providers/Microsoft.Authorization/roleDefinitions/{id}?api-version=2022-04-01`
   for each distinct role.

A request answered 429 or 5xx is tried again three times. A 403, any other
failure, a failure that persists and a missing sentinel fail the check.

**The sentinels.** A deployment names one assignment it knows to exist at each
scope above the account, of any role, such as the subscription owner's. A listing
that lacks one did not see that scope, and the check fails closed with
`access.unchecked`. A check on a real account on 2026-11-12 found that the
account-scoped role lists the assignments inherited from the resource group and
the subscription, and reads their role definitions. Inheritance from a management
group is untested, so a tenant that assigns roles there names a sentinel there.
Where a listing does lack a scope, assign the same custom role at that scope and
name it in `[access] scopes`; the job then lists there too.

**Outside the check.** The deployment's owner reviews these with the allowlist:

- the members of an allowlisted group;
- PIM-eligible assignments, which the listing shows once they are active;
- the account's shared-key and anonymous-access settings, both off;
- POSIX ACLs on a hierarchical-namespace account.

## Events and runbooks

Standard output holds one JSON line per event, and nothing else. Standard error
holds the human lines and the summary:

```text
REFUSED <product>/<edition> <rule or ->: <reason>
SKIPPED <step>: <reason>
OUTSIDER <principal> (<type>) holds <role> at <scope>; …
UNCHECKED <why the listing failed, or the sentinel it lacks>
UNUSED <object id> (<name>) is on the allowlist and holds no blob-read role
ERROR <step>: <error>
```

Each event has `event`, `severity`, `at` (the storage time of its step) and
`catalog`, and where they apply `product`, `edition`, `rule`, `reason` and
`detail`.

```json
{"event":"fold.refused","severity":"warning","at":"2026-11-12T13:00:02.000Z","catalog":"az://catalogs/stc/","product":"14100287","edition":"01a18e76-…","rule":"chain","reason":"…"}
```

| Event | Severity | Runbook |
|---|---|---|
| `jobs.config` | info | `detail.sha256` is the configuration's SHA-256. Compare it with the GC job's; two values mean the jobs run different copies of the ConfigMap. |
| `current.behind` | warning | `CURRENT` named an older version than the newest root, and the run moved it to `detail.seq`. A writer died between its root and `CURRENT`. Repeated events point at a writer that cannot write `CURRENT`. |
| `access.mismatch` | error | `detail.outsiders` lists each assignment whose principal is off the allowlist. No edition is released until it is gone. Remove the assignment, or add the principal to `[access] allowlist` after a review, then let the next run release. `statcat admin release` publishes one edition meanwhile. |
| `access.unchecked` | error | The listing failed (`detail.errors`) or lacks a sentinel (`detail.missing_sentinels`). A 403 means the custom role is missing at the storage account. A missing sentinel means the assignment was removed, or the listing does not see its scope: correct `[access] sentinels`, or assign the role at that scope and name it in `[access] scopes`. |
| `fold.refused` | warning | The registration named by `product` and `edition` was not recorded; `rule` and `reason` say why. For `files`, `manifest` and `pending-document`, rerun the upload. For `chain`, `final`, `lifecycle`, `kind` and `canonical-form`, discard the edition with `statcat gc --discard` and seal a new one. For `pending-fork`, discard all but one. For `references`, fold or release the structure edition it refers to. For `too-old`, GC deletes the registration. For `store`, the next run tries again. |
| `fold.release_at_past` | warning | The upload arrived after its own release time, and the edition is recorded with no release time. Release it with `statcat admin release <url> <product> <edition> --reason …`, or cancel it. |
| `fold.restored` | warning | The registration predates the catalog's restore, and the edition is recorded with no release time. Confirm it was not cancelled before the restore, then release it by hand, or cancel it. |
| `release.refused` | warning | A due edition stayed embargoed. With `rule` `null`, `reason` is `release_due`'s: for an edition built on an edition that is no longer live, cancel it with `statcat admin cancel` and have the producer build from the live one. `publish-order` waits for its structure edition's release. `full-artefacts` needs the product republished from its structure home's current edition. `manifest` means the stored manifest changed: restore it from the backup, or cancel the edition. |
| `release.overdue` | warning | An edition is embargoed past `detail.release_at`. The refusal record, or `release.held`, `access.mismatch` or `access.unchecked` in the same run, names the cause. |
| `release.held` | warning | The catalog's embargo is on, and `detail.due` editions wait. `statcat admin embargo <url> off --reason …` resumes automatic releases. |
| `release.unprepared` | warning | An edition was folded and released by one run, with no pre-materialisation between. Check that the pre-flight run ran and that the upload arrived before it. |
| `step.failed` | error | `detail.step` failed with `detail.error`, and the other steps ran. A store fault clears with the next run. A manifest that cannot be read belongs to an entry recorded before statcatlib 0.17: restore it, or republish the product. With `detail.step` `start` the run stopped before its first step. |

The orchestrator raises "the release job missed or failed" from the exit code and
the schedule.

The command exits 0 when every step ran, with refusals, a held release and a
skipped release reported; 1 when a step failed or the run stopped, each printed as
`ERROR <message>`: a refused configuration, one with no `[access]`, an opted-in
catalog, a clock more than 30 s ahead of the storage clock, or a store or
credential failure; and 2 on a usage error, as `statcat upload`'s, or a store other
than Azure Blob Storage.

## Scheduling

The orchestrator knows the release times the agency publishes, and the job learns
nothing from the catalog. For each release time it starts the command at least
twice:

1. **A pre-flight run**, such as 08:00 for an 08:30 release. It folds the uploads,
   so pre-materialisation can open each embargoed edition through
   [`EmbargoReader`](embargo.md) before the release.
2. **A run just after the release time**, such as 08:30:05. It releases what is due.

Any run releases what is due, so a missed run is made up by the next. As a
Kubernetes CronJob:

| Setting | Value | Why |
|---|---|---|
| `concurrencyPolicy` | `Forbid` | One run at a time. Two runs still write a correct chain, since each version is created exclusively, and one of them plans again. |
| `backoffLimit` | `0` | A failed run is repaired by the next scheduled run, which plans every step again. |
| The jobs configuration | One ConfigMap, mounted into this job and the GC job | Both check it and log its SHA-256. |

A run reads every leaf on 64 threads in each step. A fold also reads each
registration and each manifest it judges, sends one `HEAD` for each file those
manifests list, and reads each recorded manifest once more through the rules. A
release reads one manifest for each entry it makes live.
`tests/stores/test_release_job.py` records the counts on Azurite for 25 products
holding 212 tables: the pre-flight run made 50 leaf reads, 50 manifest reads and
27 writes, and the release run 99 leaf reads, 25 manifest reads and 26 writes. No
real store has timed a release.

A server catalog starts with `embargo: false`. An operator runs `statcat admin`
from a pod that holds the release identity and names themself in `--reason`.

## A dry run

`--dry-run` runs the access check and plans the fold, the release and the
retirement against the head. It moves no `CURRENT` and writes nothing. The release
is planned on the head as it is, so an edition the run would fold is not planned
for release.

```bash
statcat release az://catalogs/stc --config jobs.toml --dry-run
```

## The library call

```python
from statcatlib.access import AzureReaderListing
from statcatlib.jobs import load_jobs_config
from statcatlib.release import ReleaseJob
from statcatlib.store.blob import AzureBlobRest

config = load_jobs_config("/etc/statcat/jobs.toml")
listing = AzureReaderListing(config.access.storage_account, config.access.container, arm_token=arm_token)
with AzureBlobRest(location) as rest:
    report = ReleaseJob(location, config, rest=rest, listing=listing).run()
report.folded    # each "<product>/<edition>" recorded embargoed
report.released  # each made live
report.refused   # each a ReleaseRefusal(product_id, edition_id, step, rule, reason, since)
report.failed    # step to error
```

`ReleaseJob(location, config, *, rest, listing, storage=None)` takes each client
as an argument and reads no environment variable; the command builds them.
`storage` defaults to `BlobInventoryStorage(location)`, and a test passes one with
`clock=` and `actor=`. `listing` is any object with `readers() -> ReaderReport`,
and `rest` any object with `head(path) -> RemoteFile | None`. Each event is a record
on the `statcatlib.release` logger, and the command prints each as one JSON line
([reference](../reference/release.md)).

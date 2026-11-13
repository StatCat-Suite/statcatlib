# Catalog administration

`CatalogAdmin` makes the operational changes to a catalog's Inventory: it
rolls a product back, releases or cancels an embargoed edition, turns the
embargo on and off, records the catalog's description, moves `CURRENT`
forward, and prunes old editions. It runs on a catalog directory and on a
catalog in object storage, except `prune`, which runs on local disk only.
`CatalogAdmin.create` creates a catalog in object storage
([Creating a catalog](#creating-a-catalog)).
`statcat admin` and `statcat advance-current` run the changes from the command
line ([The `statcat admin` commands](#the-statcat-admin-commands)). Two
`statcat` commands check a catalog and report on it, and only read;
`statcat prune` runs the prune.

## The operational object

```python
from statcatlib import CatalogAdmin

admin = CatalogAdmin.open("~/stats")
```

`CatalogAdmin.open` reads `catalog.json` and writes nothing until a change
is made. It refuses a legacy layout as `Catalog.open` does, and a path with
no `catalog.json` with `FileNotFoundError`. It also takes a `BlobLocation`
([Catalogs in object storage](#catalogs-in-object-storage)). Nothing on the public
`Catalog`, `Product` or `EditionView` returns a `CatalogAdmin`: a program
that only reads cannot change the Inventory by accident
([reference](../reference/admin.md)).

Every change takes a `reason`, which the Inventory version records as
`event.reason` beside its actor: `<os user>@<hostname>` on local disk, and the
storage identity's object id in object storage. A blank reason raises
`ValueError`. Each change reads the storage's clock once and gives that time to
sdmxlib's rule and to the version it writes: this machine's clock on local
disk, the storage clock in object storage. Each change writes one Inventory
version and returns its number. When another writer creates that version first, the change is
evaluated again against the new head, so a refusal reflects the version the
change would have landed on.

| Method | What it records | `event.kind` |
| --- | --- | --- |
| `rollback(product, *, to=None, if_live=None, reason)` | The live edition `withdrawn`, a superseded one `live` | `rollback` |
| `release(product, edition, *, reason)` | An embargoed edition `live`, the edition it was built on `superseded` | `release` |
| `cancel(product, edition, *, reason)` | An embargoed edition `withdrawn` | `withdraw` |
| `set_embargo(*, on, reason)` | The catalog's embargo setting | `embargo-change` |
| `describe(description, *, reason)` | The catalog's description | `describe` |
| `prune(*, older_than, reason, dry_run=True)` | Old `superseded` and `withdrawn` editions retired, on local disk only; see [Pruning old editions](#pruning-old-editions) | `retire` |

`admin.editions(product)` lists the product's editions in every state, each
an `EditionEntry` with its `state`, `previous`, `state_changed_at` and
`release_at`; `admin.products()` lists every recorded product, and
`admin.embargo` reads the setting. They only read.

A refused change raises `InventoryRuleError`, whose `rule` names sdmxlib's
rule (`if-live`, `state`, `unknown`, `fast-forward`) and whose `product` and
`edition` name what it concerned. The Inventory is left as it was.

### Rolling back a product

```python
admin.rollback("14100287", reason="the March figures double-count Ontario")
```

The live edition becomes `withdrawn`, and the most recent `superseded`
edition becomes `live` again; `to=` names another superseded edition. A
`Catalog` sees the change after `refresh()`, and a view taken before stays on
its edition.

The rollback is conditional. `if_live` names the edition you expect to be
live, by default the one live when the call starts. When another edition has
gone live by the time the version is written, such as after a competing
release, the rollback is refused with `rule="if-live"`. Read
`admin.editions(product)` again and decide afresh.

A working copy built on the withdrawn edition fails its seal with
`NonFastForwardError`, naming its base, the live edition and the fix:
`discard()` it, then start one from the live edition with `working_copy()`.
A withdrawn edition never goes live again on its own: neither the seal
journal repair nor `release` makes it live.

A rollback makes an edition live, so the cross-product rules apply
([Publish order and the cross-product rules](#publish-order-and-the-cross-product-rules)).
A rollback of a data product to an edition cut from a structure edition that
is neither live nor embargoed is refused with `CrossProductRuleError`
(`rule="full-artefacts"`). A rollback of a structure product withdraws the
edition the live data editions were cut from, so those become stale.

### Embargo, release and cancel

```python
admin.set_embargo(on=True, reason="hold the 08:30 release")

with Catalog.create("~/stats", exist_ok=True) as cat:
    wc = cat.product("14100287").working_copy()
    ingest_sdmx(wc, registry)
    edition = wc.seal()  # recorded embargoed

admin.editions("14100287")  # the new edition, state "embargoed"
admin.release("14100287", edition.edition_id, reason="08:30 release")
```

`set_embargo` refuses to set the state the catalog already has: turning the
embargo on when it is on, or off when it is off (as it is before the first
version), raises `ValueError` naming the current state and writes no version.

With embargo on, a seal records its edition `embargoed`. `Catalog.open`
never shows an embargoed edition: `products()`, `product(id).editions()` and
`product(id).view()` see live editions only, and a product whose only
edition is embargoed raises `NoLiveEditionError` from `view()`. A seal that
read the head before `set_embargo(on=True)` landed re-reads it and records
its edition `embargoed`.

`release` makes an embargoed edition live now and supersedes the edition it
was built on. It is refused with `NonFastForwardError` when that edition is
no longer live, such as after another edition built on the same base was
released; the message names both editions. `cancel` the stale one and seal a
new edition on the live one. `cancel` withdraws an embargoed edition, which
is never released afterwards.

On local disk `release` and `cancel` use this machine's clock: the time they
record as `state_changed_at` is the laptop's. A local catalog has no scheduled
release, so a release happens when a person or a job on the laptop calls it,
and a job timed by a skewed clock releases early or late. In object storage
they record the storage clock.

On local disk the embargo holds only for readers that go through
`Catalog`: anyone who can read the catalog directory can read an embargoed
edition's files ([Catalog format](../catalog-format.md)).

### Publish order and the cross-product rules

The Inventory writer refuses a version that breaks specification §6.5 item
4, 7 or 8 for an edition entering `live` or `embargoed`
([The cross-product rules](../catalog-format.md#33-the-cross-product-rules)).
`release` and `rollback` raise `CrossProductRuleError` and write nothing;
its `rule`, `urn` and `editions` say what to fix:

- `publish-order`: a data edition's partial, or a full artefact a structure
  product holds, matches only an embargoed structure edition. Release the
  structure edition first, then the data edition:

  ```python
  admin.release("structure-geo", geo.edition_id, reason="08:30 release")
  admin.release("14100287", table.edition_id, reason="08:30 release")
  ```

- `full-artefacts`: a data edition holds a copy no live or embargoed edition
  of its structure home holds. A **stale** embargoed edition, cut from a
  structure edition an edit has since replaced, is refused this way; cancel
  it and republish the product from the structure's current edition. The
  same rule refuses a full artefact with no structure home, such as a DSD
  two data products share, whose copy differs from another data product's
  live copy. `release` takes one product per call, so under embargo such a
  change cannot go live: turn the embargo off and seal every product holding
  it in one `Catalog.seal`, or give the artefact a structure home.
- `structure-home`: a structure edition holds a full artefact another
  structure product holds live.

A structure edit is accepted while data editions hold the old copy. The live
ones stay live and keep serving until their products are republished;
`statcat verify-catalog` and `statcat doctor` list them as stale.

To move a full artefact from one structure product to another, release the
edition that drops it first, then the edition that adds it: a data edition
cut from it in between is refused until the second release.

### The catalog's description

```python
from statcatlib import Agent, CatalogDescription, InternationalString

admin.describe(
    CatalogDescription(
        identifier="stats",
        title=InternationalString(en="Statistics", fr="Statistiques"),
        description=InternationalString(en="Every table we publish"),
        publisher=Agent(name=InternationalString(en="Stats Agency")),
    ),
    reason="describe the catalog",
)

Catalog.open("~/stats").description  # the CatalogDescription
```

`describe` records the catalog-level fields of a `CatalogDescription` in the
Inventory root's `extensions.statcat`: identifier, title, description,
publisher, homepage, language, licence, theme taxonomy, issued and modified.
Every later version carries it unchanged, and each `describe` writes a new
version, so two versions can carry different descriptions.
`Catalog.description` reads it from the version the catalog holds, and is
`None` before the first `describe`. The datasets come from the live
editions, and the description never stores them.

`statcatlib.bundle.description_to_json` and `description_from_json` convert
the fields; `description_from_json` ignores keys it does not know.

## Catalogs in object storage

```python
from obstore.store import AzureStore
from statcatlib import BlobLocation, CatalogAdmin, TokenIdentity

identity = TokenIdentity(provider)  # the release job's identity
store = AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=identity)
admin = CatalogAdmin.open(BlobLocation("az://catalogs/stc/", store))
admin.release("14100287", edition_id, reason="jdoe: released by hand after the job refused it")
```

`CatalogAdmin.open` over a `BlobLocation` writes through a
`BlobInventoryStorage`, as the [Inventory on object
storage](store.md#the-inventory-on-object-storage) describes: the version's
time is the storage clock, read once per change from the `Date` of an
anonymous `HEAD`, and its `event.actor` is the object id in the store's
`TokenIdentity` token. `actor=` names the actor for a store whose credential
is no `TokenIdentity`, and `clock=` replaces the storage clock; tests pass
both. A catalog directory takes neither, and raises `TypeError` when given
one.

Every change but `prune` runs as it does on disk, with the same rules. A
`release` or `rollback` reads the manifest of the edition it makes live from
the store and compares its SHA-256 with the Inventory's, so an edition whose
manifest changed after it was recorded is refused with
`sdmxlib.edition.ManifestError`. `prune` raises `TypeError`: on a server the
release job retires old editions, and [`statcat gc`](gc.md) deletes their files.

On a server, an operator makes these changes under the release job's identity,
the only one with write on `inventory/`, so the version's `event.actor` is the
job's object id. Name yourself in the reason, which the version records as
`event.reason`.

### Creating a catalog

```python
admin = CatalogAdmin.create(BlobLocation("az://catalogs/stc/", store))
admin.catalog_id  # the new catalog's UUIDv7
```

`CatalogAdmin.create` writes `catalog.json` at an empty prefix, with a new
UUIDv7 `catalog_id` and `restore_epoch` 0, and returns the operational object
over the new catalog. It writes nothing else. `statcat upload`,
`statcat release` and `statcat gc` each need that `catalog.json`, so creation
is the first step of a new catalog on a server, and
[`statcat admin create`](#the-statcat-admin-commands) runs it. The identity
needs write on `catalog.json`, which the release identity holds
([The release identity](release.md#the-release-identity)).

The call refuses two prefixes, and writes nothing to either:

- A prefix that holds `catalog.json` raises `FileExistsError`, and the message
  names the existing `catalog_id`. `create` never replaces a `catalog.json`:
  the write is one create-only Put, so of two calls at one time the store
  accepts one, and the other raises `FileExistsError` naming the winner's
  `catalog_id`.
- A prefix that holds an object under `inventory/`, `products/` or `pending/`
  and no `catalog.json` raises `IncompatibleCatalogError`, naming the first
  object found. Those objects belong to a catalog whose `catalog_id` is lost.
  Restore that catalog's `catalog.json` from the backup, or create the new
  catalog at an empty prefix. The check lists each of the three prefixes and
  stops at the first object.

A new catalog has no Inventory version and holds no embargo setting. The
release job's first fold writes version 1, which records `embargo: false`, so
the job releases each edition at its `release_at`
([the design's Q1](../design/release-job.md#open-questions)). `admin.embargo` reads `False`
until then. To hold every automatic release from the start, call
`set_embargo(on=True, reason=...)` on the new catalog before the first upload;
that call writes version 1 with the embargo on.

`create` takes a `BlobLocation` only, and raises `TypeError` for a path. A
local catalog is created by `Catalog.create(path)`, which the program that
builds the catalog calls to get its writable `Catalog`.

### Moving `CURRENT` forward

```python
admin.advance_current()  # the version CURRENT now names, or None
```

A writer that stops after its root and before `CURRENT` leaves `CURRENT` one
version behind. Readers probe forward and still find the newest version, at
the cost of extra reads. `advance_current` reads the newest root by the read
protocol and moves `CURRENT` to it when `CURRENT` names a lower `seq`, or a
version of an earlier restore epoch. It never moves `CURRENT` back: when
another writer has moved it further meanwhile, it stays there. It writes no
root and no leaf, and returns `None` when `CURRENT` named the newest version
already.

## The `statcat admin` commands

The commands need the `blob` extra for an `az://` catalog
(`uv add 'statcatlib[blob]'`, or `pip install 'statcatlib[blob]'`), or the
`search` extra for a local one; both install the CLI.

```bash
statcat advance-current URL
statcat admin create AZ_URL
statcat admin editions URL PRODUCT
statcat admin release URL PRODUCT EDITION --reason TEXT
statcat admin cancel URL PRODUCT EDITION --reason TEXT
statcat admin rollback URL PRODUCT [--to EDITION] [--if-live EDITION] --reason TEXT
statcat admin embargo URL on|off --reason TEXT
```

`URL` is a catalog directory or `az://<container>[/<prefix>]`. An `az://`
catalog is reached as `statcat upload` reaches one
([Object storage](blob.md#the-command)): `DefaultAzureCredential`, the account
in `AZURE_STORAGE_ACCOUNT_NAME`, and the same refused variables, with a
five-minute request timeout. On a server, run the commands from a pod that
holds the release job's identity and name yourself in `--reason`:

```bash
statcat admin editions az://catalogs/stc 14100287
statcat admin cancel az://catalogs/stc 14100287 01a18e76-… --reason "jdoe: built on a withdrawn base"
statcat admin embargo az://catalogs/stc on --reason "jdoe: hold every release until the fix"
```

`editions` prints one line per edition: its id, state, `previous`, store
schema, the time it entered its state and its `release_at`. Each change prints
the Inventory version it wrote. `embargo on` holds every automatic release; an
explicit `statcat admin release` still releases.

`create` takes an `az://` URL only and runs
[`CatalogAdmin.create`](#creating-a-catalog); a directory exits 2, since
`Catalog.create(path)` creates a local catalog. Standard output holds the new
`catalog_id` alone, and standard error the human line, so a script reads the id
from the command's output:

```bash
catalog_id=$(statcat admin create az://catalogs/stc)
```

It exits 1 and writes nothing when the prefix holds a `catalog.json`, naming
its `catalog_id`, or holds objects under `inventory/`, `products/` or
`pending/` and no `catalog.json`. It prints each `ERROR <message>` on standard
error. It exits 2 for a store other than Azure Blob Storage.

| Exit code | Meaning |
| --- | --- |
| 0 | The change was written, the catalog was created, `CURRENT` moved or was current, or the editions were listed |
| 1 | The change or the creation was refused, the catalog cannot be read, or a store or credential failed; each prints as `ERROR <message>`. `editions` of a product the catalog does not record exits 1 too |
| 2 | Usage: a missing or blank `--reason`, a bad `URL`, `s3://`, no `AZURE_STORAGE_ACCOUNT_NAME`, a refused variable, or a missing `blob` extra |

## Pruning old editions

Every seal leaves the edition it supersedes on disk, so a laptop catalog
grows with each republish. `prune` reclaims that space with one window. It
runs on a catalog directory; on a catalog in object storage it raises
`TypeError`:

```python
from datetime import timedelta

report = admin.prune(older_than=timedelta(days=30), reason="reclaim disk")  # a dry run
report.retired, report.deleted_dirs, report.deleted_files, report.skipped_products

admin.prune(older_than=timedelta(days=30), reason="reclaim disk", dry_run=False)
```

It runs three steps:

1. It retires each `superseded` and `withdrawn` edition that took its state
   more than `older_than` ago, in one Inventory version with `event.kind`
   `retire`. The Inventory keeps a tombstone for each, and
   `admin.editions(product)` no longer lists them.
2. It deletes the directory of each edition tombstoned more than `older_than`
   ago. An edition retired in step 1 keeps its directory until a later run
   finds its tombstone older than the window.
3. It deletes each file under a product's `data/` that no `manifest.json` on
   disk under the product lists and whose modification time is more than
   `older_than` ago.

Prune never touches a product with a working copy or a seal journal entry:
it reports each in `skipped_products` with the reason, and retires and
deletes nothing of it. Seal or `discard()` the working copy, or let the
product's next `working_copy()` settle the journal, and run prune again.
A live or embargoed edition is never retired, nor is a structure edition
that a live or embargoed data edition's `partial_of` cites, so a stale data
edition's partials still verify. Prune deletes only the
directories of tombstoned editions, so it never deletes a live, embargoed
or unrecorded edition. It deletes no Inventory root or leaf, and tombstones
never expire.

`dry_run=True` is the default: the report lists what a real run would
retire and delete, and nothing changes. A real run logs every deleted path
at INFO on the `statcatlib.admin` logger. A deletion that fails, such as on
Windows while another process has the edition open, is recorded in
`report.failed` with its error, and the run continues; the next run tries
the path again. `report.seq` is the version the `retire` step wrote, or
`None` when it retired nothing. `older_than` must be positive, and a dry run
needs a `reason` too.

The same prune runs from the command line, a dry run unless `--apply` is
given, with the window in days:

```bash
statcat prune ~/stats --older-than 30 --reason "reclaim disk"
statcat prune ~/stats --older-than 30 --reason "reclaim disk" --apply
```

It prints one line per skipped product, retired edition, deleted path and
failed deletion, then a summary. It exits 1 when the catalog cannot be
opened or a deletion failed, and 2 when an option is missing, `--reason`
included.

## Checking catalogs from the command line

The two commands below need the `search` extra, which installs the CLI
(`uv add 'statcatlib[search]'`, or `pip install 'statcatlib[search]'`). They
repair, record and delete nothing. The functions behind them are in
`statcatlib.diagnostics` ([reference](../reference/diagnostics.md)).

### Checking a catalog

```bash
statcat verify-catalog ~/stats
```

`verify-catalog` reads the newest valid Inventory version, as a reader does,
and checks:

- every Inventory root the read protocol skipped, which is a finding;
- each product's leaf, against the SHA-256 its name holds and sdmxlib's leaf
  rules;
- for each edition a leaf records, in any state but retired, that its
  `manifest.json` has the SHA-256 the Inventory records;
- each of those entries' filled `dataflows`, `pull_bytes` and `artefacts`
  against the manifest's (`sdmxlib.inventory.verify_entry`), skipping an entry
  without them, with the leaf as the finding's file;
- each of those editions with
  `sdmxlib.edition.check_edition(at="publish", extensions={"statcat": check_catalog})`:
  the size and digests of every file the manifest lists, the product's shared
  `data/` files included, the lake, the bindings, the references, and the
  `statcat` section with its search index. A data edition is checked with
  every recorded structure edition, so each partial is checked against the
  one it was cut from; a structure edition with the other structure
  products' live and embargoed editions, where its references must resolve.

It then lists each **stale** data edition: a live or embargoed one holding a
URN whose structure home no longer holds that copy, after a structure edit.
A stale edition is allowed until its product is republished, so it is no
finding and leaves the exit code alone. It prints one line per edition and
URN:

```text
STALE table-a/0192… (live) holds urn:…Codelist=CODR:CL_GEO(1.0) at 4be1f0a2c9d3, which no live or embargoed edition of its structure home structure-geo holds (live edition: 0193…); republish the product
```

It prints each finding as `FAIL <file>: <detail>`, where the file is a path
from the catalog directory, and exits with one of three codes:

| Exit code | Meaning |
| --- | --- |
| 0 | Every check passed, or the catalog has no Inventory version yet |
| 1 | A check failed; each failing file is printed |
| 2 | The catalog cannot be read: the path is no directory, holds a legacy layout, has no or a malformed `catalog.json`, or has no valid Inventory version |

`Product.view()` refuses an edition whose `manifest.json` is not the one the
Inventory recorded, and its error names `statcat verify-catalog`.

From Python, `statcatlib.diagnostics.verify_catalog(path)` returns a
`VerifyReport` (`ok`, `seq`, `editions`, `findings`, `stale`) and raises
`CatalogUnreadableError` where the command exits 2. Each item of `stale` is
a `StaleEdition`: `product_id`, `edition_id`, `state`, `urn`, `fingerprint`
(a partial's `partial_of`), `home` and `home_live`.

### Reporting on a catalog for support

```bash
statcat doctor ~/stats
```

`doctor` writes nothing. It prints one line per item, `ok` for state worth
knowing and `WARNING` for something to act on:

| Topic | What it reports |
| --- | --- |
| `synced folder` | A warning when the path is in a Dropbox, OneDrive, iCloud Drive or Google Drive folder. A sync client can copy a catalog's files out of order; keep catalogs on a local disk |
| `layout` | `catalog`, `legacy` (a statcatlib 0.11 catalog or a 0.72 bundle) or `unknown` |
| `catalog.json` | The catalog id and restore epoch, or why the file cannot be read |
| `CURRENT` | The version `CURRENT` names against the newest valid version found by probing. A lag after two writers raced is safe, because readers probe forward |
| `inventory` | Roots the read protocol skipped, and leaves that fail sdmxlib's checks |
| `stale data editions` | One item for each stale data edition and URN, as `verify-catalog` lists them, with the fix: republish the product. A stale live edition's item is `ok`; a stale embargoed edition's is a warning, since its release is refused until then |
| `partials with no structure home` | A warning for each partial of a live or embargoed data edition whose URN no live or embargoed structure edition holds full, as after its structure product drops the codelist. Its release, or a seal that cuts the codelist, is refused. The fix: seal a structure edition that holds the URN full and republish the product from it, or republish the product with `partials=False` |
| `working copy` | Each unsealed edition with its start time; `working_copy(resume=True)` resumes it and `discard()` deletes it |
| `journal` | Each seal journal entry, an empty one included. An entry is a seal a crash may have stopped; the product's next `working_copy()` settles it |
| `unrecorded edition` | Each sealed edition that the newest version neither names nor tombstones |
| `leftover file` | Each `.tmp` file the Inventory writer staged in `inventory/` or the catalog directory and left behind. Delete it when no writer is running |

It exits 0 when it reports no warning and 1 when it reports one. A path that
is not a catalog gets a report on its layout only.

From Python, `statcatlib.diagnostics.doctor(path)` returns a `DoctorReport`
(`layout`, `items`, `warnings`, and `about(topic)`).

A seal journal entry that cannot be read stops the product's next
`working_copy()` with `JournalError`, whose message names `statcat doctor`.

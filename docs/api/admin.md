# Catalog administration

`CatalogAdmin` makes the operational changes to a catalog's Inventory: it
rolls a product back, releases or cancels an embargoed edition, turns the
embargo on and off, and records the catalog's description. Two `statcat`
commands check a catalog and report on it, and only read.

## The operational object

```python
from statcatlib import CatalogAdmin

admin = CatalogAdmin.open("~/stats")
```

`CatalogAdmin.open` reads `catalog.json` and writes nothing until a change
is made. It refuses a legacy layout as `Catalog.open` does, and a path with
no `catalog.json` with `FileNotFoundError`. Nothing on the public
`Catalog`, `Product` or `EditionView` returns a `CatalogAdmin`: a program
that only reads cannot change the Inventory by accident
([reference](../reference/admin.md)).

Every change takes a `reason`, which the Inventory version records as
`event.reason` beside its actor, `<os user>@<hostname>`. A blank reason
raises `ValueError`. Each change writes one Inventory version and returns its
number. When another writer creates that version first, the change is
evaluated again against the new head, so a refusal reflects the version the
change would have landed on.

| Method | What it records | `event.kind` |
| --- | --- | --- |
| `rollback(product, *, to=None, if_live=None, reason)` | The live edition `withdrawn`, a superseded one `live` | `rollback` |
| `release(product, edition, *, reason)` | An embargoed edition `live`, the edition it was built on `superseded` | `release` |
| `cancel(product, edition, *, reason)` | An embargoed edition `withdrawn` | `withdraw` |
| `set_embargo(*, on, reason)` | The catalog's embargo setting | `embargo-change` |
| `describe(description, *, reason)` | The catalog's description | `describe` |

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

`release` and `cancel` use this machine's clock: the time they record as
`state_changed_at` is the laptop's. statcatlib has no scheduled release before
Phase 4, so a release happens when a person or a job on the laptop calls it,
and a job timed by a skewed clock releases early or late.

On local disk the embargo holds only for readers that go through
`Catalog`: anyone who can read the catalog directory can read an embargoed
edition's files ([Catalog format](../catalog-format.md)).

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
- each of those editions with
  `sdmxlib.edition.check_edition(at="publish", extensions={"statcat": check_catalog})`:
  the size and digests of every file the manifest lists, the product's shared
  `data/` files included, the lake, the bindings, the references, and the
  `statcat` section with its search index.

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
`VerifyReport` (`ok`, `seq`, `editions`, `findings`) and raises
`CatalogUnreadableError` where the command exits 2.

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

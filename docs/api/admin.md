# Catalog administration

Two `statcat` commands check a catalog and report on it. Both only read: they
repair, record and delete nothing. They need the `search` extra, which
installs the CLI (`uv add 'statcatlib[search]'`, or
`pip install 'statcatlib[search]'`). The functions behind them are in
`statcatlib.diagnostics` ([reference](../reference/diagnostics.md)).

## Checking a catalog

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

## Reporting on a catalog for support

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

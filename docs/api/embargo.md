# Embargo reader

A public `Catalog` reads live editions only. `EmbargoReader` reads every state the
Inventory records and opens an embargoed or live edition as an `EditionView`. It
serves two jobs of statcat-serve (sdmxlib's plan, Q8 and Q9):

- **The preview.** A deployment inside the catalog's access boundary shows an
  embargoed edition beside the live edition it replaces, so a reviewer compares a
  release with what it supersedes before `release_at`.
- **Pre-materialisation.** A job running the pods' image opens each embargoed
  edition, computes the release set (the full-table downloads per language, the
  slices the front end requests on load) and writes it to `results/` before the
  release, so the first requests after it hit the result cache (specification
  §11.2).

```python
from pathlib import Path

import sdmxlib as sl
from obstore.store import AzureStore
from statcatlib import BlobLocation, EmbargoReader, TokenIdentity

location = BlobLocation(
    "az://catalogs/stc/",
    store=AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=TokenIdentity(provider)),
    secrets=[sl.StoreSecret("azure", options=options)],
    cache_dir=Path("/var/cache/statcat-preview"),
)
reader = EmbargoReader.open(location)
try:
    for entry in reader.editions():
        held = reader.view(entry.product_id, entry.edition_id)
        replaced = reader.view(entry.product_id, entry.previous) if entry.previous else None
        ...
finally:
    reader.close()
```

`EmbargoReader.open` takes a catalog directory or a `BlobLocation`, as
`Catalog.open` does, and refuses the same inputs: a URI string, a `BlobLocation`
with no `cache_dir`, and a directory that is not a catalog. It writes nothing to
the catalog.

## Listing and viewing

- `editions(*, states=("embargoed",))` lists the entries of the Inventory version
  the reader holds, in each state asked for, as
  [`EditionEntry`](../reference/admin.md) values: by product id, then in each
  product's id order. A state other than `embargoed`, `live`, `superseded` or
  `withdrawn` raises `ValueError`.
- `view(product_id, edition_id, *, embedding_model=None, query_prefix="")` opens
  an `embargoed` or `live` edition. A `superseded` or `withdrawn` one raises
  `ValueError`, and a product or edition the Inventory does not record raises
  `InventoryRuleError` with `rule="unknown"`. Neither reads anything under the
  edition.
- `refresh()` re-reads the Inventory and returns the reader, so a loop that runs
  after each release job run sees the editions that run folded. A view taken
  before stays on its edition.
- `close()` closes every view the reader returned, and its DuckDB engine.

A view runs the steps of `Product.view`, through one function both share. On a
catalog in object storage it reads the manifest first and compares its SHA-256
with the entry's, pulls the catalog files to the cache, runs the `receive` check,
and later the `open` check, and reads observations from the store through DuckDB.
The errors are `Product.view`'s ([Object storage](blob.md#reading-a-catalog)).

## The cache and the access boundary

The reader's `cache_dir` holds embargoed bytes:

```text
<cache_dir>/<catalog_id>/products/<product_id>/<edition_id>/        an embargoed or live edition's catalog files
<cache_dir>/<catalog_id>/embargo-verified/<product_id>/<edition_id>  its receive stamp
```

So the preview deployment and the pre-materialisation job each keep their own
`cache_dir`, on a volume inside the catalog's access boundary (specification
§10.1), and never share one with a public `Catalog` or a serving pod. The
receive stamps live in `embargo-verified/`, apart from a public reader's
`verified/`.

The reader's identity reads `products/`, `catalog.json`, `inventory/` and
`CURRENT`, and writes nothing to the store: §10.1's row for the preview
deployment. It holds a role that reads blobs in the catalog's container, so the
release job's access check counts it, and the [`[access]` allowlist](jobs.md#access)
names it.

No attribute of `Catalog`, `Product` or `EditionView` returns an `EmbargoReader`
(specification §6.4). Code reaches one only by calling `EmbargoReader.open`, so a
search for that call finds every place that reads embargoed editions.

The module imports nothing from the `blob` extra at module level, so the core
package imports without it, and a reader over a catalog directory runs without
`obstore`.

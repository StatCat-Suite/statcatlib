# Object storage

`statcat upload` copies a sealed edition from a publish job's local disk to a
catalog in Azure Blob Storage, and registers it in `pending/`. `Catalog.open`
reads a catalog there, returning live editions only. Install both with
`uv add 'statcatlib[blob]'`, or `pip install 'statcatlib[blob]'`.

An uploaded edition waits in `pending/`. Phase 5's fold records it in the
Inventory, so no reader sees it until then, and until that fold a catalog holding
real editions comes only from the test helper `record_uploaded`. Upload supports
Azure only in Phase 4, and refuses an `S3Store`, because `obstore` cannot set or read
CRC32C.

[The Inventory on object storage](store.md#the-inventory-on-object-storage)
describes the Inventory writer, and [the reference](../reference/blob.md) lists
each name.

## The command

```text
statcat upload EDITION_DIR AZ_URL [--release-at TIME]
```

| Input | Value |
|---|---|
| `EDITION_DIR` | The sealed edition's directory, with the product's shared `data/` beside it. |
| `AZ_URL` | The catalog root, `az://<container>[/<prefix>]`. |
| `--release-at` | RFC 3339 with `Z` or an offset. Omitted writes `null`, which schedules no release. |
| `AZURE_STORAGE_ACCOUNT_NAME` | The storage account. Required. |
| `azure.identity`'s variables | Such as `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_FEDERATED_TOKEN_FILE` for a workload identity. `DefaultAzureCredential` reads them. |

The command exits 2 when any of `AZURE_STORAGE_ACCOUNT_KEY`,
`AZURE_STORAGE_ACCESS_KEY`, `AZURE_STORAGE_MASTER_KEY`, `AZURE_STORAGE_SAS_KEY`,
`AZURE_STORAGE_SAS_TOKEN`, `AZURE_STORAGE_TOKEN`, `AZURE_STORAGE_ENDPOINT` or
`AZURE_ENDPOINT` is set, even to an empty value, before it builds a credential. The
publish job acts under one identity, and `obstore` reads each of these variables: a key
or token would write under another identity, and an endpoint would send the writes to
another host than the digest read.

The identity reads and writes under `products/` and `pending/`, as Storage Blob Data
Contributor grants. The command reads nothing under `catalog.json`, `inventory/` or
`CURRENT`.

The output names the identity's `oid`, the files sent and kept, and whether this
run registered the edition. `DefaultAzureCredential` tries several sources, so
check that the `oid` is the identity you meant.

| Exit code | Meaning |
|---|---|
| 0 | The edition is uploaded, or already registered with the same bytes. |
| 1 | A finding, printed as `FAIL <file>: <detail>`, or a store, manifest or credential failure, printed as `ERROR <message>`. |
| 2 | A usage error, a key or SAS variable, or a missing `blob` extra. |

## What an upload does

1. It reads the manifest. An unsealed directory raises `ManifestError`. A file
   above Azure's 5,000 MiB limit for one Put, or a path leaving its directory,
   raises `UploadCheckError` before anything is sent.
2. It creates each data file under `products/<product_id>/data/`. It hashes a file
   against the manifest before it sends it, so a rotted local file stays out of
   the shared `data/`. A file the store holds stays, since data files are immutable.
3. It creates the edition's own files in the same way.
4. It creates `manifest.json`. Other bytes under that name raise `UploadCheckError`,
   because an edition is never rewritten.
5. It verifies:
    - `check_edition(at="receive", lookup=)` checks the lake, the bindings, the
      section and `data/` in the store;
    - `verify_remote` checks the edition's uploaded files;
    - a GET of the uploaded manifest compares its SHA-256.
6. It creates `pending/<product_id>/<edition_id>.json`.

Any finding raises `UploadCheckError` before step 6, so a bad upload registers
nothing.

Each create is one conditional Put, so Azure keeps the file's MD5. Step 5 reads that
MD5 with `Get Blob Properties` under the identity's bearer token, through
`AzureBlobRest`, because `obstore` reads no `Content-MD5`.

An edition with no bindings defers the reference check, because a structure product's
references may resolve in another structure product and the job may not read the
Inventory. The fold owns that check.

A crash and a rerun send only what is missing and leave one registration. A rerun with
another `--release-at` is refused, naming both times.

Each file is read into memory for its Put, so peak memory is the largest file, up to
5,000 MiB. The command sets a one-hour request timeout.

## From Python

```python
from azure.identity import DefaultAzureCredential
from obstore.auth.azure import AzureCredentialProvider
from obstore.store import AzureStore
from statcatlib import BlobLocation, TokenIdentity, upload_edition

identity = TokenIdentity(AzureCredentialProvider(DefaultAzureCredential()))
store = AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=identity)
report = upload_edition(edition_dir, BlobLocation("az://catalogs/stc/", store))
```

`upload_edition` raises `TypeError` for any store but an `AzureStore`, and `ValueError`
when its credential provider is no `TokenIdentity`.

## Reading a catalog

```python
from pathlib import Path

import sdmxlib as sl
from obstore.store import AzureStore
from statcatlib import AggregateDataset, BlobLocation, Catalog, TokenIdentity

location = BlobLocation(
    "az://catalogs/stc/",
    store=AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=TokenIdentity(provider)),
    secrets=[sl.StoreSecret("azure", options=options)],
    cache_dir=Path("/var/cache/statcat"),
)
with Catalog.open(location) as cat:
    view = cat.product("14100287").view()
    view.query(AggregateDataset).limit(5).collect()
```

The caller builds both credentials, one for `obstore` and one as DuckDB's
`StoreSecret`, because sdmxlib reads data files through DuckDB. A library call never
takes a credential from the environment, so `Catalog.open("az://...")` with a string
raises `ValueError`. `cache_dir` is required, so disk use stays visible. A view of an
edition with data files raises `ValueError` when `secrets` is empty. Phase 6 decides
the production form of the secret.

`Catalog.open` writes nothing to the catalog, and neither does anything else a reader
calls. It reads `catalog.json` once, for `catalog_id`, and the Inventory. `refresh()`
reads `CURRENT`, one root and one probe, and only the leaves that changed. `products()`
reads every leaf, and keeps them in memory.

The first `view()` of an edition:

1. Reads `manifest.json` and compares its SHA-256 with the Inventory entry. A difference
   raises `IncompatibleCatalogError`, before any other file of the edition is read.
2. Pulls the `required` and `lazy` files with `pull_edition`. Each file goes to a staged
   directory beside the destination, is checked against the manifest's size and sha256
   and is fsynced. Only then does one rename make the edition visible. A process that
   loses the rename uses the winner's copy.
3. Runs `sdmxlib.edition.check_on_open`, whose `receive` profile writes a stamp, and a
   finding raises `IncompatibleCatalogError`.
4. Opens the edition with observations read from the store through DuckDB. statcatlib
   never caches a data file.

Later views use the cached files and run the `open` profile. The cache holds:

```text
<cache_dir>/<catalog_id>/products/<product_id>/<edition_id>/   manifest, metadata.duckdb, tables.ducklake, search/
<cache_dir>/<catalog_id>/verified/<product_id>/<edition_id>    the receive stamp
```

A cached edition whose manifest differs from the Inventory's raises
`IncompatibleCatalogError` naming the directory to delete. Phase 4 evicts nothing, so
`cache_dir` grows by each edition viewed.

Deleting an edition's directory leaves its receive stamp under `verified/`. The next view
pulls the edition again and hashes every file it pulls against the manifest, then runs the
`open` profile with no new `receive`. To force a new `receive`, delete the stamp as well.

`Product.editions()` reads the manifest alone and pulls nothing.

An edition with no bindings defers the reference check on a reader, as on upload, since a
structure product's references may resolve in another structure product. The bytes hash to
a manifest the Inventory recorded, and Phase 5's fold owns that check.

### Ids and paths

The Inventory and the manifest are bytes from a store, and each names local files.

- A `catalog_id`, `product_id` or `edition_id` that names a directory matches
  `^[A-Za-z0-9][A-Za-z0-9._-]*$`. `Catalog.product` raises `ValueError` for another
  product id. A leaf's edition id or `catalog.json`'s `catalog_id` that fails raises
  `IncompatibleCatalogError` before anything under it is read or written.
- A manifest naming another product or edition than its Inventory entry raises
  `IncompatibleCatalogError` before any other file is read.
- A manifest `files` path that is absolute, holds `..`, `.` or an empty segment, holds a
  backslash or a NUL, or names `manifest.json` raises it too, as does a `data_path`
  leaving the product directory.

### The embargo boundary

The boundary is the storage account or container, and statcatlib cannot enforce it from
inside a process.

- A principal that can run `Catalog.open` holds a role that can read embargoed bytes.
  `Catalog.open` keeps the public API from returning them, as on local disk. An
  analyst's identity belongs on a live-only export, never on the catalog's container.
- `Catalog` holds only live entries. `products()` and `view()` never return an embargoed
  one, and an embargoed product raises `NoLiveEditionError`.
- Leaf bytes name embargoed editions, so they stay in memory and never reach `cache_dir`.
  Only live editions' files do.
- A cached edition stays after a rollback or a withdrawal until an operator deletes its
  directory. Delete it to remove the edition from that disk. Nothing purges pod disks
  until `expunge` exists.

## Real-account checklist

**None of these has run.** Azurite and `pytest-httpx` test the code, and neither proves
how real Azure behaves. No test reads a real account through DuckDB either. The owner
runs this list once, with a real storage account, before Phase 5 relies on the blob
writer, the upload and the remote read, and records the results in GH-134. Item 9 runs in
the same pass and feeds Phase 6, serving.

1. A commit under a workload identity: `event.actor` holds the `oid`.
2. The anonymous `Date` `HEAD`, also behind the deployment's private endpoint.
3. A bearer `HEAD` returns `Content-MD5`, with Storage Blob Data Contributor.
4. A duplicate create's status, and that `obstore` raises `AlreadyExistsError`.
5. `statcat upload` with a file over 1 GiB: time, peak memory, and the one-hour timeout.
6. `from_url` with `az://` and `account_name=` from `AZURE_STORAGE_ACCOUNT_NAME` gives
   the account's endpoint.
7. `import obstore.auth.azure`, which imports `azure.identity.aio`, works on a clean
   `[blob]` install.
8. `Catalog.open(BlobLocation)` over a bearer `TokenIdentity` store with a real
   `StoreSecret`: a view of a data edition reads rows through DuckDB.
9. A read of every leaf on the reader's own pool, from a machine in the account's region.
   The concurrent read shipped in sdmxlib 0.87 (StatCat-Suite/sdmxlib#375) and is the
   default. The baked summary is the fallback if this read cannot serve plan Goal 1, a
   cold pod with one pinned product ready in under 60 s, on real storage. Record each
   run's wall time, GET count and any throttling responses, such as 503 `ServerBusy` or
   client retries.

    `bench/inventory_read.py` starts Azurite and has no real-account option, so import
    its `RecordingFetch` into a short script in the repository root, run with
    `uv run python`. Build a `BlobLocation` over an empty prefix, with a store as
    `statcat upload` builds it (`from_url` with
    `TokenIdentity(AzureCredentialProvider(DefaultAzureCredential()))` and the one-hour
    timeout). Call
    `statcatlib.testing.inventory_corpus(InventoryWriter(BlobInventoryStorage(location)))`.
    At its defaults it records 7,000 products in 7,016 blobs (7,000 leaves, 14 roots,
    `catalog.json` and `CURRENT`), about 21 MB, one Put at a time.

    Run the read twice, each in a new process, so the connections start cold. Call
    `LiveInventory(fetch).refresh().editions()` with
    `fetch = RecordingFetch(_ObjectOps(location.store))`, at the default
    `DEFAULT_LEAF_THREADS` (64), and time the whole call. `len(fetch.take())` is the GET
    count, expected to be 7,003. `leaf_threads=1` gives an optional sequential comparison.

    The first run uses the store above. The second uses item 8's store, built as
    [Reading a catalog](#reading-a-catalog) shows, with
    `AzureStore(container, prefix=prefix, account_name=account, credential_provider=TokenIdentity(provider))`
    and `obstore`'s default timeout. Both stores hold a `TokenIdentity`, so record for each
    run whether a request failed with an authentication error or the credential provider
    raised. A provider that raises gives a `BlobStoreError` naming `UnauthenticatedError`,
    and a 403 gives one naming `PermissionDeniedError`. Storage Blob Data Contributor, as
    in item 3, covers the corpus, both reads and the deletion; a read alone needs Storage
    Blob Data Reader.

    `obstore` retries a 5xx response inside its client, and `RecordingFetch` counts calls,
    so it misses each retry. Read the throttling from the account's `Transactions` metric
    in Azure Monitor, split by `ResponseType`, with no other traffic on the account and a
    gap between the runs wide enough to tell them apart. `ServerBusyError` counts the 503
    responses, and `GetBlob` transactions above 7,003 a run count the retries. Any other
    `BlobStoreError` from the call means the retries ran out, and is a result to record.

    The corpus holds Inventory documents with no edition behind them. Write it once, since
    a second `inventory_corpus` at the prefix raises `FileExistsError`. Both runs read that
    one corpus, because a read writes nothing and both stores reach the same prefix. Delete
    the prefix afterwards, since no other item uses it.
10. Which credential writes when another credential's variable sits beside
    `credential_provider`. Record `obstore.__version__` with the results.

    In a new process per case, with the variable set, call `_azure_location` from
    `statcatlib.cli`, which skips the refusal in `_azure_account`, and create one small
    blob at a scratch prefix with `obstore.put(location.store, path, data, mode="create")`.
    Print `location.store.credential_provider.object_id()`. Run the cases with an identity
    that holds Storage Blob Data Contributor, and first run one with no extra variable as
    the baseline.

    | Case | Set to | The result that shows the provider's token wrote |
    |---|---|---|
    | `AZURE_STORAGE_ACCOUNT_KEY` | `Ym9ndXM=` | The create succeeds. |
    | `AZURE_STORAGE_SAS_TOKEN` | `sv=bogus` | The create succeeds. |
    | `AZURE_STORAGE_TOKEN` | `bogus` | The create succeeds. |
    | `AZURE_CREDENTIAL_TYPE` | `access_key` | The create succeeds. |
    | `AZURE_STORAGE_CLIENT_ID`, `AZURE_STORAGE_CLIENT_SECRET` and `AZURE_STORAGE_TENANT_ID` together | `bogus` | The create succeeds. |
    | `AZURE_USE_AZURE_CLI` | `true`, on a machine with no `az` login | The create succeeds. |
    | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` together, which `azure.identity` reads too | A second service principal with no role on the account | The printed `oid` is the second principal's, and the create fails with `PermissionDeniedError`. |

    A bogus value authorizes nothing, so a create that succeeds came from the provider. To
    name the writer, turn on the account's diagnostic setting for blob writes before the
    run, and read `AuthenticationType` and `RequesterObjectId` from `StorageBlobLogs` for
    each blob: expect `OAuth` and the printed `oid`. An upload records no `event.actor`, so
    these logs are the account's only record of the writer. Without the setting, record
    that the writer stays inferred. Delete the prefix afterwards.

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

The command exits 2 before it builds a credential when any of these `obstore` variables
is set, even to an empty value or `false`:

| Variable | Why the command refuses it |
|---|---|
| `AZURE_STORAGE_ACCOUNT_KEY`, `AZURE_STORAGE_ACCESS_KEY`, `AZURE_STORAGE_MASTER_KEY`, `AZURE_STORAGE_SAS_KEY`, `AZURE_STORAGE_SAS_TOKEN`, `AZURE_STORAGE_TOKEN` | Spec §10.1 allows the publish job no account key or SAS. On `obstore` 0.12.1 the credential provider's token wins beside each of them, and a later `obstore` could change that order. |
| `AZURE_STORAGE_ENDPOINT`, `AZURE_ENDPOINT` | The writes go to that host, and the digest read goes to the account's host. |
| `AZURE_STORAGE_USE_EMULATOR` | The writes go to `127.0.0.1:10000`, signed with the emulator's key, and the digest read goes to the account's host. |
| `AZURE_USE_FABRIC_ENDPOINT` | The writes and the token go to the Fabric host. |
| `AZURE_SKIP_SIGNATURE` | The writes go unsigned, so an upload can exit 0 and print an `oid` that signed nothing. |
| `AZURE_STORAGE_ENCRYPTION_KEY` | The blobs are encrypted with a customer-provided key, which the digest read omits. A lost key leaves the data unrecoverable. |

`obstore` reads every variable whose name starts with `AZURE_` and matches the rest in
any case, so the command refuses `AZURE_storage_endpoint` too, and names each variable as
the environment spells it. It accepts `azure_storage_endpoint`, which `obstore` ignores.
The [design's D7](../design/catalog-on-blob-storage.md#d7-statcat-upload) lists the
variables the command accepts, with the reason for each.

The command also exits 2 when `obstore` cannot build the store from an accepted
variable's value, such as `AZURE_DISABLE_TAGGING=junk` or an empty
`AZURE_DISABLE_TAGGING`. It prints `obstore`'s reason and the variable, and sends no
request. It has built `DefaultAzureCredential` by then and asked it for no token.

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
| 2 | A usage error, a refused variable, or a missing `blob` extra. |

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
- An [`EmbargoReader`](embargo.md) opens embargoed editions, so its `cache_dir` holds
  their files. Give it a `cache_dir` of its own, never a public reader's.
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
10. Which identity writes when other credential variables sit beside
    `credential_provider`. An offline run with a stub provider, on `obstore` 0.12.1, shows
    the store sends the provider's bearer token beside each variable below. This item shows
    which identity Azure records under a real `DefaultAzureCredential`. Record
    `obstore.__version__` with the results.

    Use an identity that holds Storage Blob Data Contributor. Run this script from the
    repository root with `uv run python`, once per case, each in a new process with the
    case's variables set. `_azure_location` skips the refusal in `_azure_account`, so a
    refused variable reaches the store.

    ```python
    import os
    import sys

    import obstore
    from statcatlib.cli import _azure_location

    container, case = sys.argv[1:]
    location = _azure_location(f"az://{container}/checklist-10/", os.environ["AZURE_STORAGE_ACCOUNT_NAME"])
    print(obstore.__version__, location.store.credential_provider.object_id())
    obstore.put(location.store, f"{case}.txt", b"x", mode="create")
    ```

    | Case | Variables | Expected |
    |---|---|---|
    | `baseline` | None beyond the identity's own. | The create succeeds. |
    | `bogus` | `AZURE_STORAGE_ACCOUNT_KEY=Ym9ndXM=`, `AZURE_STORAGE_SAS_TOKEN=sv=bogus`, `AZURE_STORAGE_TOKEN=bogus`, `AZURE_CREDENTIAL_TYPE=access_key`, `AZURE_STORAGE_CLIENT_ID=bogus`, `AZURE_STORAGE_CLIENT_SECRET=bogus`, `AZURE_STORAGE_TENANT_ID=bogus` and `AZURE_USE_AZURE_CLI=true`. | The create succeeds and the `oid` equals the baseline's. A bogus value authorizes nothing, so the provider's token wrote the blob. |
    | `second` | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` of a service principal with no role on the account. `azure.identity` reads these too. | The printed `oid` is that principal's, and the create raises `PermissionDeniedError`. |

    To name the writer, turn on the account's diagnostic setting for `StorageWrite` before
    the run, and read `AuthenticationType` and `RequesterObjectId` from `StorageBlobLogs`
    for each blob. Expect `OAuth` and the printed `oid`. An upload records no `event.actor`,
    so these logs are the account's only record of the writer. Without the setting, record
    that the `bogus` case infers the writer. Delete the prefix afterwards.

    Leave out `AZURE_STORAGE_USE_EMULATOR`, `AZURE_USE_FABRIC_ENDPOINT`,
    `AZURE_SKIP_SIGNATURE` and the endpoint variables. The offline run settled them, and
    each sends the write to another host or unsigned.

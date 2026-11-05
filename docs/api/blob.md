# Object storage

`statcat upload` copies a sealed edition from a publish job's local disk to a
catalog in Azure Blob Storage, and registers it in `pending/`. Install it with
`uv add 'statcatlib[blob]'`, or `pip install 'statcatlib[blob]'`.

An uploaded edition waits in `pending/`. Phase 5's fold records it in the
Inventory, so nothing reads it until then. Upload supports Azure only in Phase
4, and refuses an `S3Store`, because `obstore` cannot set or read CRC32C.

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
`AZURE_STORAGE_ACCESS_KEY`, `AZURE_STORAGE_MASTER_KEY`, `AZURE_STORAGE_SAS_KEY`
or `AZURE_STORAGE_SAS_TOKEN` is set, since the publish job acts under an identity.

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

## Real-account checklist

**None of these has run.** Azurite and `pytest-httpx` test the code, and neither proves
how real Azure behaves. The owner runs this list once, with a real storage account,
before Phase 5 relies on the blob writer and the upload, and records the results in the
upload's pull request.

1. A commit under a workload identity: `event.actor` holds the `oid`.
2. The anonymous `Date` `HEAD`, also behind the deployment's private endpoint.
3. A bearer `HEAD` returns `Content-MD5`, with Storage Blob Data Contributor.
4. A duplicate create's status, and that `obstore` raises `AlreadyExistsError`.
5. `statcat upload` with a file over 1 GiB: time, peak memory, and the one-hour timeout.
6. `from_url` with `az://` and `account_name=` from `AZURE_STORAGE_ACCOUNT_NAME` gives
   the account's endpoint.
7. `import obstore.auth.azure`, which imports `azure.identity.aio`, works on a clean
   `[blob]` install.

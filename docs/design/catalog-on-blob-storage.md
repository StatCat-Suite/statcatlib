# Phase 4: the catalog on blob storage

*   **Author:** Gabriel Gellner
*   **Status:** Draft, revision 2, not yet reviewed. Revision 2 records the owner's
    decisions of 2026-11-04 on revision 1 (Recorded decisions) and changes upload's
    digest lookup and the test setup. One owner decision is pending (O4).
*   **Date:** 2026-11-04
*   **Issue:** GH-98. Stage 2's later needs: GH-125. Suite tracker:
    StatCat-Suite/statcat-suite.github.io#19.
*   **Read against:** statcatlib `974ea27` (`origin/main`, 0.14.0); sdmxlib
    `11f18e3f` (`origin/main`, after 0.85.0); the hub at `cb144f6`, with the
    suite design at revision 4; for the leaf rule, sdmxlib's `docs/phase-4-restated` at
    `cf11dd1a`. Every `file:line` below is at these commits.
    Find each site by its symbol and use the number as a hint.
*   **Inputs:**
    *   GH-98's body, restated on 2026-11-04, which is the scope and overrides
        the plan where they differ.
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        ("the spec"): §4.1, §4.6, §6.1 to §6.7, §6.9, §10.1, §11.2, §11.5.
    *   The owner's leaf rule of 2026-11-04: a blob writer writes each leaf its
        version changes with an unconditional single-shot Put, with no
        existence check, on every blob store. It rewrites no carried-forward
        leaf. The spec's §6.6 is being changed to match.
    *   The suite design [Inventory: carry each version's changes in its
        root](https://github.com/StatCat-Suite/statcat-suite.github.io/blob/main/docs/design/inventory-history-log.md),
        decisions H10, H11, H21, H23 and H24 and its lease section; GH-125.
    *   The Phase 2a review findings DES-M6, N13 and ZM2
        (`egm/phase-2a-catalog-editions/`).
    *   `bench/corpus-results.md`: 6,300 manifests (91 MB) in one cold read at
        7,000 products.

--------------------------------------------------------------------------------

## Overview

statcatlib's catalog lives on local disk. `InventoryWriter` holds a `Path`
(`store/inventory.py:300-311`), `Catalog.open` checks `is_file`
(`catalog.py:594-620`), and `CatalogAdmin` has no way to recover from a wrong
Inventory version or to bring a retired edition back. Phase 4 adds:

- a storage seam in `InventoryWriter`, with a blob implementation over
  `obstore`, so one set of rules commits versions on both;
- `statcat upload`, which copies a sealed edition to `products/` and registers
  it in `pending/`;
- `Catalog.open` over a blob catalog, returning live editions only;
- `CatalogAdmin.correct` and `CatalogAdmin.reinstate`;
- a measured 7,000-leaf Inventory read over documents generated without data
  files;
- a `stores` test tier over containerised emulators, in `just ci`.

Three findings from checking `obstore` 0.12.1 shape the design:

1. `obstore` cannot set or read `Content-MD5` or CRC32C, and it exposes no
   `Date` header. Azure computes an MD5 for a single-shot Put by itself, and a
   `HEAD` with the identity's bearer token, through `httpx`, reads it back.
2. `obstore` has no lease call, no conditional delete and no per-request
   header, so stage 2's Azure duties need a small REST client beside it. The
   seam leaves room for one.
3. `check_edition(at="receive", lookup=)` checks only `data/` in the store.
   Upload adds `verify_remote` to cover the edition's own uploaded files.

## The Problem

The spec's blob protocol has five needs the local writer does not meet:

- Exclusive creation of a root must win with the object complete, which on
  blob storage is a single-shot `Put Blob` with `If-None-Match: *` (§6.6).
- `CURRENT` must move forward by a conditional write (§6.1).
- A server writer takes its time from the storage `Date` header (§6.6 item 4).
- `event.actor` must be the writer's storage identity (§6.2). The local writer
  records `<os user>@<hostname>` (`store/inventory.py:237-243`).
- The cross-product rules read each manifest (`store/cross_product.py:138`),
  about 6,300 GETs for one cold read at 7,000 products, or 63 s in sequence at
  10 ms a GET (GH-98).

The Phase 2a review found `correct` as the RFC states it unsafe. A retry could
erase concurrent commits, tombstones could vanish, pruned bytes could return,
and the embargo could turn off (DES-M6). A pruned or invalid `from_seq` had no
error (N13). Phase 2a deferred it here (ZM2, P6).

## Goals

*   One writer's rules on local disk and on blob storage, with the local writer
    keeping its atomic link publish.
*   A blob writer that passes `sdmxlib.inventory.conformance.run` and the
    stage-1 history checks over Azurite.
*   An upload that writes nothing under `inventory/`, needs no read of it, and
    leaves one `pending/` entry however often it runs.
*   A remote `Catalog.open` that never returns an embargoed entry and refuses a
    wrong manifest before it reads any other file of the edition.
*   `correct` and `reinstate` that close DES-M6 and N13, with every edition a
    correction drops able to come back.
*   A recorded Inventory read cost that lets the owner decide on the baked
    summary.

## Non-Goals

*   The fold of `pending/`, the release job and `release_due` (Phase 5, GH-52).
    An uploaded edition waits in `pending/`.
*   `EmbargoReader` (its first caller, Phase 5 or 6).
*   The catalog lock, opt-in, expiry, the `HISTORY` lease and `If-Match` leaf
    deletes (GH-125 parts 2 and 3). This design names the seam they use.
*   Filling `artefacts` for data products and the leaf-backed reader (GH-125
    part 4).
*   The full data corpus. It moves to Phase 6, whose benches and load driver are
    its first named uses (owner, 2026-11-04), and Phase 6 builds it with the shipped
    `codr_corpus`. Part 5 measures the Inventory-documents read only.
*   Upload to S3 (owner, 2026-11-04).
*   Sealing on blob storage. A working copy stays on local disk.
*   Blob deletion by `CatalogAdmin.prune`. GC owns deletes under `products/`
    (§11.4), so `prune` refuses a blob catalog.
*   Evicting the local edition cache. Phase 6's router owns placement on pods.

## Open Questions

The owner's decisions on revision 1 are under Recorded decisions. What stays open is
one owner decision, O4, and each contract question, with the repository that answers it.

### O4: an embargoed entry recorded after `from_seq` (pending)

**Pending the owner's decision.** W2 tombstones each entry a correction drops. A
`Tombstone` holds only `product_id`, `edition_id` and `retired_at`, so an embargoed
entry's `release_at` is lost. sdmxlib's `reinstate` writes `superseded`, and `rollback`
makes a `superseded` edition `live` with no `release_at` check. That path can publish an
edition before its release time.

| Option | Gain | Cost |
|---|---|---|
| **1. Recommended.** `correct` keeps embargoed entries recorded after `from_seq` in their leaves, unchanged, with no tombstone. | No format change and no code beyond the rule itself. `release_at` survives, and the embargo holds. Recovery never blocks. | A wrongly recorded entry stays until an operator runs `cancel`. If its `previous` is still live, its release goes through when due, so the operator must look at what remains. If its `previous` is no longer live, the release's fast-forward check refuses it and reports it. |
| 2. `Tombstone` gains the prior state and `release_at`, and `reinstate` restores the entry as `embargoed`. | Every dropped entry leaves the leaf, as W2 reads. A wrong entry cannot release by itself. | An optional-field format change, and a new rule in sdmxlib. Readers and the conformance suite follow. |
| 3. `correct` refuses while such an entry exists, and the operator cancels first. | The simplest rule, and no gap. | It blocks recovery at the worst moment, when the Inventory is already wrong. |

D7 and Part 4's tests are written for option 1, and they name what changes under the
others. Whichever is chosen, the Phase 5 fold cannot re-record the edition from
`pending/`: §11.2 skips an edition recorded in a leaf or in `retired`, so option 1's kept
entry, option 2's tombstone and option 3's `withdrawn` entry are each skipped.

### Contract questions

None of these block Part 1 unless stated. Each goes to the owner named first.

| # | Owner | Where | Question | Proposal |
|---|---|---|---|---|
| S1 | sdmxlib spec | §6.4 table against §10.1 and §11.2 | The table says the publish job moves a sealed edition to `embargoed`. §10.1 gives that job no access to `inventory/`, and §11.2 says only the release job writes versions. | Change the table's "who" to the release job's fold. |
| S2 | sdmxlib spec | §6.2 against §10.1 | §6.2 says the actor is "never from the caller". §10.1 says an operator who needs another identity sets it explicitly. | The actor is set once when the writer is constructed, and no method takes one (D3). |
| S3 | sdmxlib spec | §6.6 item 4 | "On a server" is undefined. | Read it as every writer over a blob store, a laptop's `CatalogAdmin` included (D4). |
| S4 | sdmxlib code, before Phase 6 | §4.6, §4.7, §7.1 | `check_edition` reports a missing lazy file at every profile (`edition/_check.py:382-405`), so a pod holding the required tier alone fails `check_on_open`. | Add a tier argument. Phase 4 pulls both tiers (D6). |
| S5 | statcatlib, no spec change | §11.2 step 1 | "Verifies every file" goes beyond `check_edition(lookup=)`, which covers `data/` only (`edition/_check.py:202-205`). | Upload adds `verify_remote` (Part 2). |
| S6 | sdmxlib code | sdmxlib API | Reading a manifest from bytes needs `decode_manifest`, which `sdmxlib.edition` does not export. `read_manifest` takes a directory. | Export it. Blocks D5's reader; the fallback writes each manifest to a temporary directory. |
| S7 | sdmxlib spec | §10.1 | The publish job's "write" must include reading its own uploads for the digest check. | State it. Storage Blob Data Contributor already includes read. |
| S8 | sdmxlib spec | §6.3, §6.4 | §6.3 says "A tombstone comes from a `retire` version", and the §6.4 table moves only `superseded` and `withdrawn` entries to a tombstone. `correct` also tombstones live entries, and embargoed ones under O4's option 2. | Add `correct` to both. No format change (D7). |
| S9 | sdmxlib code and spec | `rollback`, §6.4 | `rollback` needs a live edition, so an edition reinstated into a product with none cannot go live by rollback (D7). | Let `rollback` take `if_live=None` to mean "expects none live". Part 4 does not depend on it. |
| S10 | sdmxlib code | `sdmxlib.inventory`, §6.4 | `retire` refuses live and embargoed entries, so `correct` needs its own rule. | Add `sdmxlib.inventory.correct` (D7). Blocks Part 4. |
| S11 | sdmxlib code and spec | `conformance.py`, §6.3 | `check_a_correction_links_and_diffs_against_the_version_before_it` expects `("live", None)` for the edition the correction leaves out. `CatalogAdmin.correct` never writes that, and §6.3 gives `to: null` only the meaning "drops the tombstone". | Keep the check, which tests `change_list`. Add a check using `correct`, and state the `to: null` case in §6.3. Does not block. |

## Critical User Journeys (CUJs)

### CUJ 1: A publish job uploads an embargoed edition

The job seals an edition locally, then runs `statcat upload` with its workload
identity and a `--release-at`. Data files go first, then the edition's files,
then the manifest. Every listed file is checked against a digest the store
keeps. `pending/<product>/<edition>.json` appears, and nothing else changes. A
crash and a rerun send only what is missing and leave one registration.

### CUJ 2: An operator reads a blob catalog

```python
location = BlobLocation(
    "az://catalogs/stc/",
    store=AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=TokenIdentity(credential)),
    secrets=[sl.StoreSecret("azure", options={"connection_string": connection})],
    cache_dir=Path("/var/cache/statcat"),
)
with Catalog.open(location) as cat:
    view = cat.product("14100287").view()
    view.query(AggregateDataset).limit(5).collect()
```

The first `view()` pulls the edition's catalog files to the cache, checks them,
and reads observations from the store. An embargoed product has no live edition
and raises `NoLiveEditionError`.

Until Phase 5's fold, nothing in production records an upload, so a catalog in this
state comes from `statcatlib.testing.record_uploaded` (Test setup).

### CUJ 3: An operator corrects a wrong Inventory version

An operator finds version 41 wrong. `CatalogAdmin.editions` shows the head at 42
and version 39 good. `correct(from_seq=39, if_seq=42, reason="...")` writes 43.
Root 43 links root 42 and diffs against it, and its `previous_inventory` is 39.
If another writer committed 43 first, the call is refused and nothing is lost. An
edition sealed after 39 is tombstoned, so its files stay for the retention window and
`reinstate` can bring it back. In Phase 4 the blob form of this journey runs in tests
and drills, over editions `record_uploaded` put there.

### CUJ 4: A restore drill brings back a retired or dropped edition

`reinstate(product, edition, reason=...)` verifies the manifest and every listed
file and moves the tombstone to `superseded`. A `rollback` can then make it live.

## Technical Plan (Architecture)

### What `obstore` does

Checked on 2026-11-04 against `obstore` 0.12.1 (uploaded 2026-10-27, the newest
on PyPI, `requires-python >=3.11`, no Python dependencies, abi3 wheels of 4.6 to
5.6 MB), which builds on the `object_store` crate 0.14.2 (`Cargo.lock`). The runs
used sdmxlib's two pinned emulator images in a throwaway environment, through a
recording proxy that logged every request. Sources are marked **stub** (the
`.pyi` files), **src** (the sdist or the crate), **run** (Azurite, or SeaweedFS
where stated) and **docs** (Microsoft Learn, fetched that day).

| # | Fact | Source |
|---|---|---|
| F1 | `put(mode="create")` sends `If-None-Match: *` and `x-ms-blob-type: BlockBlob`. A second create raises `AlreadyExistsError` (Azurite 409, SeaweedFS 412), and the object keeps its bytes. | run; src `azure/client.rs:760,770-775` |
| F2 | `put(mode={"e_tag": etag})` sends `If-Match`. A stale or absent target raises `PreconditionError`. `GetResult.meta` and `PutResult` carry the `e_tag`. Azure's `version` was `None`. `MemoryStore` supports it. `LocalStore` raises `NotImplementedError`, so `LocalStore` cannot hold `CURRENT`. | run, on Azurite, SeaweedFS and both stores; src `:761-764` |
| F3 | No response exposes `Date`. `ObjectMeta` holds `path`, `last_modified`, `size`, `e_tag`, `version`. `PutResult` holds `e_tag`, `version`. `GetResult.attributes` held only `Content-Type`. The sdist's Rust sources do not mention the header, and the crate's `client/header.rs` reads only `Last-Modified`, `ETag`, size and version from a response. | stub `_list.pyi:10-40`, `_put.pyi`; run; src grep |
| F4 | A response to any request carries `Date`, also an anonymous `HEAD`: Azurite answered 400 or 403, SeaweedFS 405, each with `Date`. Azure documents `Date` on responses as service-generated. | run; docs Put Blob |
| F5 | CRC32C cannot be set. `checksum_algorithm` takes `sha256` or `crc64nvme`, and `"crc32c"` raises a config error. `sha256` sends `x-amz-checksum-sha256`, which `obstore` never reads back. | src `aws/checksum.rs:25-30`, stub `_aws.pyi:60`; run on SeaweedFS |
| F6 | `Content-MD5` cannot be set. `attributes={"Content-MD5": ...}` becomes `x-ms-meta-content-md5` (user metadata), which Azurite refuses with 400. `client_options.default_headers` sends `x-ms-blob-content-md5` outside the SharedKey signature, and Azurite answers 403. | src `attributes.rs:18-27`; run |
| F7 | A single-shot Put Blob keeps an MD5 the service computes. `Get Blob Properties` returns it. A multipart upload (Put Block List) keeps none, and `obstore` cannot send the header that sets one. | run: `content-md5` on `HEAD` after a Put, absent after block upload; docs Put Blob, Put Block List |
| F8 | `obstore` does not read `Content-MD5`. With an account key, a signed GET URL (`sign(store, "GET", path, ...)`, `sp=r`) answers a `HEAD` with `Content-MD5`, `Content-Length` and `ETag`. `sign(store, "HEAD", ...)` yields an empty `sp=` and 403, since only GET, PUT and DELETE map to a permission. | run; src `azure/credential.rs:391-403` |
| F9 | A signed PUT URL accepts added headers. A wrong `Content-MD5` gives 400, a right one 201, and `If-None-Match: *` a 409 on the second try. | run |
| F10 | Multipart starts when the size exceeds `chunk_size`, 5 MiB by default: 5,242,880 bytes went as one PUT, 5,242,881 bytes as blocks and a block list. An iterable goes multipart at any size, and a `Path` by its size. | run; src `obstore/src/put.rs:81-82,215-218,322-331` |
| F11 | `use_multipart=False` forces one PUT at any size, and it reads the whole input into memory. A `mode` other than overwrite forces one PUT, even for an iterable, and even with `use_multipart=True`. | run, 5 MiB + 1, 12 MiB `Path`; src `put.rs:328-333`; stub `_put.pyi` |
| F12 | Put Blob's limit is 5,000 MiB on service version 2019-12-12 and later. `object_store` sends version 2023-11-03 (`azure/credential.rs:49`). | docs; run |
| F13 | A 5xx, 429 or 408 is retried on every request, a create included. A timeout is retried only on an idempotent request, and a create or an update is not idempotent. Defaults: 10 retries, 180 s. So a create can raise `AlreadyExistsError` against its own earlier success, and a timeout leaves its outcome unknown. | src `client/retry.rs:404-408,435-442`, `azure/client.rs:759-760`; run for the defaults |
| F14 | `delete(store, paths)` takes paths alone, `put` takes no free-form header, and no call takes a lease. So stage 2's `If-Match` delete, lease and `x-ms-lease-id` writes are outside `obstore`. | stub `_delete.pyi`, `_put.pyi`; `dir(obstore)` |
| F15 | No API returns an identity. `credential_provider` is a callback we supply, returning a token and its expiry. `obstore.auth.azure.AzureCredentialProvider` wraps `azure.identity`, which `obstore` does not install. | stub `_azure.pyi:331`, `auth/azure.py`; run |
| F16 | An Entra access token is a JWT, and `oid` is "the immutable identifier for the requestor, which is the verified identity of the user or service principal". Microsoft says an application should not depend hard on any claim being present. | docs, access token claims |
| F18 | A `HEAD` with `Authorization: Bearer <token>` returns `Content-MD5`, `Content-Length`, `ETag` and `Date`, on a blob written by `obstore` with a `credential_provider` or by raw PUT. The MD5 equals the content's. Azure documents `Get Blob Properties` under Entra ID with `blobs/read`, Storage Blob Data Reader at least, and says `Put Blob` sets the MD5 with no header sent. | run on Azurite 3.37.0, `--oauth basic`; docs Get Blob Properties |
| F19 | Azurite's `--oauth basic` checks audience, issuer and expiry: a wrong one of each, or a malformed token, gets 403. It does not check the signature, so a test can mint a token. It needs TLS. Over plain HTTP it starts, and it answers a valid token with 403. Its `--cert` and `--key` take a self-signed pair, and `client_options={"allow_invalid_certificates": True}` lets `obstore` connect. Microsoft's Azurite page does not cover OAuth. | run; `azurite-blob --help` |
| F20 | `obstore.sign` on a bearer credential asks for a user delegation key, which that Azurite did not grant, so the emulator cannot exercise a token-based SAS. | run |
| F21 | The same bearer token through `httpx` took and released a lease, and a delete with a wrong `If-Match` got 412 and with the right one 202. | run |
| F22 | A bind mount from `/private/tmp` was not visible in a colima container. `docker cp` into a created container worked. | run |
| F17 | 2,000 GETs of 14 KB on Azurite: 1.35 s in sequence, 0.61 s with 8 threads, 0.51 s with 32, 0.43 s with 64. The sync `get` runs concurrently in threads. Azurite was CPU-bound here, so this is a floor on the gain. | run |

**Not confirmed.** Nothing here ran against real Azure or AWS.

- That an anonymous `HEAD` on a real Azure endpoint returns `Date` under a
  private endpoint or a firewall rule.
- That a token from a managed or workload identity, for the storage audience,
  carries `oid`. The docs say service principals, and do not name managed
  identities on that page.
- That a `HEAD` with a real Entra token returns `Content-MD5` on real Azure. The docs
  say so, and Azurite does not validate a real token.
- Whether a real Azure duplicate create answers 409 or 412. The crate maps both
  to `AlreadyExistsError`.
- Any AWS behaviour: the presigned rules for `x-amz-checksum-*` headers, and the
  ETag being an MD5. SeaweedFS's ETag was an MD5 in the run; that proves nothing
  about AWS.
- The default 30 s request `timeout` on a large single-shot Put. The stub
  documents it for streams. Upload sets its own `timeout` in `client_options` and
  Part 2 tests a large file on Azurite (the emulator's limit is 2 GiB, docs).

### Recorded decisions

The owner decided these on 2026-11-04, and GH-98 records them.

| ID | Decision | Consequence here |
|---|---|---|
| W1 | The full data corpus moves to Phase 6. | Part 5 measures Inventory documents only. |
| W2 | `correct` writes a tombstone for each entry recorded after `from_seq` that it drops. | `reinstate` can bring the edition back, and GC keeps its files for the window (D7). O4 asks whether an embargoed entry counts as dropped. |
| W3 | Upload is Azure-only in Phase 4 and refuses an `S3Store`. | `obstore` cannot set or read CRC32C (F5). The Inventory writer still runs on S3. §11.2's S3 clause waits for a deployment that needs it. |
| W4 | The skew bound is 30 s, and spec §6.4 states it. | `MAX_CLOCK_SKEW` (D4). |
| W5 | Whether §10.1 allows a short-lived user delegation SAS is deferred to Phase 6, when DuckDB's remote reads need an answer. | Phase 4 takes whatever `StoreSecret` the caller gives. duckdb/duckdb-azure#171, open on 2026-10-19, is why it comes up: sdmxlib's `StoreSecret` docstring warns that `credential_chain` fails on Azure with a managed identity. Upload's lookup uses no SAS on real Azure (D2), so nothing else waits on this. |
| W6 | The `stores` tier fails when no container runtime exists, since the pre-push hook is statcatlib's only gate. | A push needs Docker. This differs from sdmxlib's tier, which skips. |

### Design decisions

| ID | Decision | Question |
|---|---|---|
| D1 | `InventoryWriter` takes an injected `InventoryStorage`, and rules stay in the writer. | 1 |
| D2 | `obstore` is an extra, `statcatlib[blob]`, imported lazily. Upload's lookup uses a bearer token and `httpx`. | 2 |
| D3 | The actor is the token's `oid`, else a value declared on the location, else a refusal. | 3 |
| D4 | The blob clock is an anonymous `HEAD`'s `Date`, under a 30 s skew bound. | 4 |
| D5 | `ManifestArtefacts` reads through `Fetch`, with a bounded concurrent prefetch. | 5 |
| D6 | `Catalog.open(BlobLocation(...))`, with an explicit `cache_dir`, pulling both local tiers. | 6 |
| D7 | `correct` runs the cross-product rules and replaces the fast-forward check with `if_seq` and leaf validation. | 7 |

### D1: the storage seam

```python
class InventoryStorage(Protocol):
    """Where a catalog's Inventory documents live: the writer's only route to them."""

    @property
    def location(self) -> str: ...
    def read(self, path: str) -> bytes | None: ...              # the reader's Fetch
    def create(self, path: str, data: bytes) -> None: ...       # FileExistsError if taken; complete before visible
    def write_leaves(self, leaves: Mapping[str, bytes]) -> None: ...
    def advance_current(self, current: Current, data: bytes) -> None: ...
    def now(self) -> datetime: ...
    def actor(self) -> str: ...
    def exclusive(self) -> ContextManager[None]: ...            # no-op in stage 1
```

`LocalInventoryStorage(root)` moves the existing code behind this: `local_fetch`,
`create_exclusive`, `_stage`, `_fsync_dir`, the leaf skip, the staged replace of
`CURRENT`, `datetime.now(UTC)` and `_actor()`. `InventoryWriter(root)` still takes
a path and wraps it, so every local caller and test is unchanged.

`BlobInventoryStorage(location)`:

| Operation | What it does | Writes for a one-product change |
|---|---|---|
| `write_leaves` | For each leaf the version changes, `put(path, data, use_multipart=False)`, with no existence check, through a bounded thread pool. | 1 |
| `create` | `put(path, data, mode="create")`, which F11 guarantees is one Put at any size. A republish's 8 MB root is safe. | 1 |
| `advance_current` | `get`, then forward only: `put(mode={"e_tag": ...})`, or `mode="create"` when absent. On a precondition failure, re-read and repeat up to `MAX_RACES`, stopping when `held.seq >= root.seq`. | 1 |

A carried-forward leaf gets no Put, because its base root is the head, which
expiry keeps (hub design, lease section). `leaves` passed to `try_commit` is
already the changed set. So a release of 200 products makes 200 leaf Puts, and a
one-product commit at 7,000 products makes one. At 10 ms a request, such a commit
costs about 120 ms: 3 writes and 9 reads (`CURRENT`, its root, the probe, the
product's leaf, `catalog.json`, the base root, the base leaf, the `Date` `HEAD`,
and `CURRENT` again). The rules' reads are extra.

Three blob behaviours are new to the writer:

- **A create's unknown outcome (F13).** On `AlreadyExistsError` or a transport
  error, `create` reads the name. Bytes equal to its own mean this writer won, and
  the call returns. Other bytes raise `FileExistsError`, which `try_commit` turns
  into `InventoryConflictError` as today. An absent name after a transport error
  is retried once, then the error propagates with nothing committed.
- **`CURRENT` after a committed root.** The root is the commit point, so a failed
  `advance_current` on blob logs a WARNING and `try_commit` returns the root.
  Readers probe forward (§6.6).
- **A long-lived reader.** `InventoryWriter.full()` builds a new `FullInventory`
  on each call, cheap locally (1 to 4 ms, `bench/corpus-results.md`) and five
  calls a seal. On blob, a fresh reader's `leaves()` costs 7,000 GETs. The writer
  keeps one `FullInventory` when its storage says to and calls `refresh()`, so
  leaves stay cached by hash in memory. A `_ObjectOps` class wraps the `obstore`
  calls, so a test can subclass it to record every write.

Alternatives:

| Alternative | Gain | Cost |
|---|---|---|
| **Injected protocol (chosen).** | One set of rules, tested once; a fake storage drives faults; stage 2 changes one method per backend. | An indirection for one local and one blob backend. |
| Abstract base class with template methods. | No extra object. | Rules and storage share a class, and `try_commit`'s control flow spreads through overrides. |
| `obstore.LocalStore` for both. | One code path. | Update mode is missing (F2), so `CURRENT` fails. The writer's fsyncs go. Local catalogs gain a Rust dependency. GH-98 requires keeping the link publish. |
| A separate `BlobInventoryWriter` with shared functions. | The local file is untouched. | Two `commit` loops to keep equal, and the retry rules are where bugs would diverge. |

The seam's other edges:

- `ManifestArtefacts` takes a `Fetch` (D5).
- `CatalogAdmin` calls `writer.now()` at `admin.py:325` and `:650`, so a rule's
  `state_changed_at` uses the same clock as `created_at`.
- `InventoryWriter.root` stays a `Path` for a local catalog and raises on a blob
  one. `WorkingCopy` and `prune` read it, and neither runs on blob.
- `check_writable` reads `catalog.json` through `storage.read` and keeps
  `decoder="stage-1-writer"`, so a 2.x document is refused on blob with the same
  named error, on every commit.

**How stage 2 fits (GH-125, H21, H23).** Nothing here is built now.

| Stage 2 duty | Where it goes |
|---|---|
| Lock from first leaf write to `CURRENT` | `exclusive()`: `flock` locally, the `HISTORY` lease on Azure. `try_commit` wraps its body in `with storage.exclusive()` from Part 1, and calls `_commit_locked`, which a restore calls directly. |
| Stale-base refusal after the lock | Inside `_commit_locked`, on a fresh `storage.read(HISTORY)`. |
| Decoder for 2.x | One attribute, `storage.stage_2`, picks the decoder. |
| Unconditional leaf Put | Already `write_leaves`. |
| `If-Match` leaf delete, lease, `x-ms-lease-id` writes | `AzureBlobRest`, which Part 2 adds for the digest lookup (D2), since `obstore` has none of them (F14). The same bearer token ran a lease and a conditional delete on Azurite (F21). |
| Opt-in refused on a store other than Azure | `isinstance(store, AzureStore)` in `CatalogAdmin`. |

### D2: `obstore` as an extra

**Recommendation:** `[project.optional-dependencies] blob = ["obstore>=0.12,<0.13"]`,
imported inside functions as `lance.py` imports `lancedb`, plus a `dev` group
entry so the type checker and the tier have it. A missing import raises
`ImportError` naming `uv add 'statcatlib[blob]'`, then the `pip` form. The CLI's
`upload` needs `typer` and `rich` too, which `[search]` holds today, so `blob`
adds them. The `azure-identity` package stays the caller's dependency: a
`credential_provider` is any callable, and a deployment picks its credential.

| Option | Gain | Cost |
|---|---|---|
| **Extra (chosen).** | A laptop user installs nothing new. Same pattern as `search`. | Lazy imports need care under the `Any` ratchet, and a test must prove core runs without the extra. |
| Base dependency. | No import guard, simpler typing. | Every laptop install gains a 5 MB native wheel it never calls. |
| `fsspec` or the Azure SDK instead. | Azure SDK has lease, `If-Match` delete and digest headers in one client, so stage 2's REST client goes away. | GH-98 names `obstore`. `azure-storage-blob` serves Azure only, and its create semantics would need the checks F1 to F11 give here. Worth reconsidering if stage 2's REST client grows. |

**Reading the digest upload needs.** `AzureBlobRest(location)` holds an `httpx` client
and one method in Phase 4, `head(path) -> RemoteFile | None`, sending `Get Blob
Properties` with `x-ms-version`. It authorizes in one of two ways, chosen when it is
built:

1. A `TokenIdentity` on the store's credential provider sends `Authorization: Bearer`.
   This is the production path, and it needs `blobs/read` and nothing else.
2. An `account_key` in `store.config` signs a GET URL with `obstore.sign` and sends
   `HEAD` to it (F8). This is the plain Azurite path, two lines with no signer.

With neither, upload refuses before it writes anything and names both.

| Option | Gain | Cost |
|---|---|---|
| **Bearer token through `httpx`, account key for the emulator (chosen).** | No SAS, and no `generateUserDelegationKey` at account scope. §10.1 stands as written. The client is the one stage 2 needs (F21). `httpx` is a base dependency. | Two authorization branches. The bearer branch needs a TLS Azurite in the tier (below). A token provider must be a `TokenIdentity`, since `obstore` keeps its credential to itself. |
| `obstore.sign` for every credential. | One branch, with no bearer handling. | A SAS on every lookup, a role at account scope (docs: the action must be scoped to the account or above), and §10.1 would need amending, which W5 defers. Azurite cannot exercise the token path (F20), so the production path would run first on real Azure. |
| A SharedKey signer for the emulator instead of `sign`. | The emulator path sends the same `HEAD` as production. | 20 lines of signing in `src` or test code for a path no deployment uses, as sdmxlib's `_stores.py` carries. |
| `azure-storage-blob` in an extra. | Handles all credentials, digests, leases and conditional deletes. | A second client beside `obstore`, against GH-98's choice. Stage 2 is the point to look again if `AzureBlobRest` grows. |
| Stream each file back and hash it. | No digest from the store at all. | A download of every file on every upload, and §11.2 asks for a digest the store keeps. |

**What the tier needs for the bearer path.** A second Azurite container runs with
`--oauth basic --cert --key`, created first and given its certificate by `docker cp`
(F22). The pair comes from `trustme`, a new `dev` dependency, so no private key sits in
the repository. A test mints an unsigned token with the right audience and issuer
(F19) and passes `allow_invalid_certificates`. The tests that read data through DuckDB
stay on the plain container: this design did not try DuckDB against a self-signed
certificate, and those tests gain nothing from it.

### D3: `event.actor`

**Recommendation.** `TokenIdentity(provider)` wraps the caller's credential
provider. It is the callable passed to `AzureStore(credential_provider=...)`. It
calls the inner provider, decodes the JWT payload without verifying it, keeps
`oid`, and returns the credential unchanged. The token came from Entra over TLS to
this process, and the value only labels an audit record; no access decision reads it.
`TokenIdentity` also gives `AzureBlobRest` its token (D2).

The actor resolves once, when the writer first needs it:

1. the `oid` of a `TokenIdentity` on the store's credential provider;
2. else `BlobLocation.actor`, a string the operator declares;
3. else `InventoryWriteRefusedError`, naming both ways to supply one, before
   anything is written.

No method takes an actor. Upload writes no version, so it needs none.

| Option for an identity with no object id | Gain | Cost |
|---|---|---|
| **Refuse unless an actor is declared (chosen).** | Every blob `event.actor` was stated on purpose. Azurite and S3 keys work with a declared name. | One more field to set for a key-based store. |
| Fall back to `<os user>@<hostname>`. | Nothing to configure. | It is unauthenticated, and personal data that stays in segments for good (§10.1). |
| Refuse outright. | Only verified identities reach the record. | Azurite and every S3 deployment cannot write, so the conformance tier cannot run. |

| Option for reading the object id | Gain | Cost |
|---|---|---|
| **The `oid` claim (chosen).** | No request and no added permission. | F16: Microsoft warns against depending on a claim. Absence falls through to step 2. |
| Microsoft Graph lookup. | An official answer. | A directory permission for a storage job, and a new endpoint. |
| The configured client id. | Already at hand. | A client id names an application, where §6.2 asks for the object id. |

A declared actor is recorded verbatim. Telling verified from declared values
apart in the audit record would need a format change, which this design does not
propose.

### D4: the storage clock

**Recommendation.** `BlobInventoryStorage.now()` sends an anonymous `HEAD` to the
store's endpoint with `httpx` and parses `Date` from whatever status comes back.
It takes the endpoint from `store.config` for `AzureStore` and `S3Store`. A
`MemoryStore` takes an injected clock in tests.

The rule, one request per `try_commit` and per `_commit_product`:

```python
storage = parse_http_date(response.headers["date"])
if datetime.now(UTC) - storage > MAX_CLOCK_SKEW:   # 30 s, W4
    raise InventoryWriteRefusedError(...)          # names both times
return storage
```

- The bound is 30 s (W4): it catches a broken clock, NTP-synced hosts stay well inside
  it, and `Date` has one-second resolution. A release cannot go out minutes off, which
  a 5-minute bound would allow.
- A clock behind the storage clock passes, since the writer records storage time.
- A missing or malformed `Date` refuses, as does a failed request.
- `next_created_at` then applies §6.6 items 2 and 3 to the storage time, so a root
  is at least 1 ms past its base, and a base over 5 minutes ahead of storage is
  refused. `Date` has one-second resolution, and the 1 ms step covers equal
  seconds. `created_at` keeps its field position.

| Option | Gain | Cost |
|---|---|---|
| **Anonymous `HEAD` (chosen).** | No credential, no permission, no signing, every store (F4). | An extra request. Unconfirmed behind a private endpoint. |
| Signed GET on `CURRENT` with `Range`. | An authenticated response. | A SAS and a delegation key for a timestamp, on every commit. Fails on `CURRENT`'s first write. |
| `head().last_modified` after a Put. | Uses `obstore` alone. | Not the `Date` header the spec names, and needs a write first: a `describe` version has no leaf to write. |
| A probe object written and read back. | Storage's own clock, one source. | A write and a delete for every commit, and `inventory/` gains litter. |
| Patch `obstore` to expose headers. | The clean API. | Depends on an upstream release. Worth an upstream issue, since stage 2 reads `Date` too. |

### D5: the cross-product rules on blob storage

In Phase 4 the rules read manifests on blob only in `CatalogAdmin.release`,
`rollback` and `correct`. No seal runs there, and the fold is Phase 5. The corpus
uses `try_commit`, which runs no rule.

**Recommendation.** `ManifestArtefacts(fetch)` reads
`products/<p>/<e>/manifest.json`, checks the SHA-256 as today, and decodes the
bytes. A new `cross_product.to_read(full, leaves) -> list[(product, entry)]`
names the editions the rules will read, by the same conditions as `_Rules`: every
active structure edition when something is incoming, and every live data edition
when a data entry enters live. `InventoryWriter.refusals` calls
`ManifestArtefacts.prefetch(to_read(...))` first, which fills the cache with
`MANIFEST_READERS = 64` threads (F17). Local reading keeps its sequential path.

A cold read at 7,000 products is 6,300 GETs and 91 MB. At 10 ms a GET and 64
threads that estimates to about 1 s against 63 s in sequence. Part 5's run records
the real ratio on Azurite, and nothing has measured it on real storage. The cache
lives for the writer's life, and a lost race re-reads nothing.

| Option | Gain | Cost |
|---|---|---|
| **Bounded prefetch (chosen).** | About 60 times faster by estimate, small code, and it retires with GH-125 part 4 by replacing one callable. | Peak memory of 91 MB in transit, and 6,300 billed reads per cold process. |
| Sequential GETs. | No new code. | 63 s on each cold `release`. |
| Bring GH-125 part 4 forward, reading `artefacts` from leaves. | Zero GETs for filled entries. | The owner placed it in Phase 5, and every entry recorded earlier still falls back to its manifest, so the fallback is needed anyway. |
| A disk cache of records keyed by edition and SHA. | Survives restarts. | Writes embargoed editions' URNs to a disk outside the boundary (§10.1), and a job's pod starts cold anyway. |
| Skip the rules on blob until Phase 5. | Fast. | A release could break §6.5 items 4, 7 and 8. |

A test asserts `to_read` covers what the rules read, by running both over a
recording reader on the rules' existing fixtures. A gap only costs one sequential
GET, so the test guards speed.

### D6: remote `Catalog.open`

**Recommendation:**

```python
@frozen
class BlobLocation:
    """A catalog in object storage, and how to reach it."""

    uri: str                                  # "az://catalogs/stc/", the DuckDB form
    store: ObjectStore                        # prefixed at the catalog's root
    secrets: Sequence[StoreSecret] = ()       # given to the EditionEngine
    cache_dir: Path | None = None             # required by Catalog.open
    actor: str | None = None                  # D3

Catalog.open(path: str | Path | BlobLocation) -> Catalog
CatalogAdmin.open(path: str | Path | BlobLocation) -> CatalogAdmin
CatalogAdmin.create(location: BlobLocation) -> CatalogAdmin      # writes catalog.json, mode="create"
```

The caller builds the store, so `obstore`'s own credential options stay available
and statcatlib wraps none of them. A plain string with a scheme, such as
`"az://..."`, raises `ValueError` naming `BlobLocation`, so no credentials come
silently from the environment. `Catalog.create` refuses a `BlobLocation` with a
`TypeError` naming `CatalogAdmin.create`, since a working copy needs local disk.
A `BlobLocation` with no `cache_dir` raises `ValueError` from `Catalog.open`.

Two credentials are needed, one for `obstore` and one as DuckDB's `StoreSecret`,
because sdmxlib reads data through DuckDB. Sharing them would need DuckDB and
`obstore` to accept one form of credential, which they do not.

**Where the files land:**

```text
<cache_dir>/<catalog_id>/products/<product_id>/<edition_id>/   manifest, metadata.duckdb, tables.ducklake, search/
<cache_dir>/<catalog_id>/verified/<product_id>/<edition_id>    the receive stamp (§4.7)
```

**`Product.view` on a blob catalog:**

1. `LiveInventory.head(product)`, one leaf.
2. If the cache holds the edition, compare its manifest's SHA-256 to the entry's.
   Otherwise GET `manifest.json` first and compare its SHA-256, raising
   `IncompatibleCatalogError` before any other file of the edition is read.
3. Pull the `required` and `lazy` files into a staged sibling directory, check each
   against the manifest's sha256, and rename the directory into place. A loser of a
   rename race uses the winner's copy.
4. `check_on_open(dir, stamps=<cache>/verified, remote=, engine=, extensions=)`.
   The first open runs `receive`, later ones `open`. A finding raises
   `IncompatibleCatalogError`.
5. `LocalRegistry.edition(dir, remote=, engine=, manifest_sha256=)`, then `_Store._over`
   as today.

`Lazy` files are pulled because `check_edition` reports a missing lazy file (S4), so a
required-only copy fails every profile. `search/` was 0.3 MB of a 16.7 MB product
(`bench/corpus-results.md`), and more with embeddings. `pull_bytes` gives only the
required size.

| Option | Gain | Cost |
|---|---|---|
| **`BlobLocation` value on `open` (chosen).** | One type for `Catalog.open`, `CatalogAdmin.open`, upload and the CLI. `Catalog` keeps two constructors. | A new public class. |
| `Catalog.open("az://...", store=, secrets=, cache_dir=)`. | Reads naturally. | Keyword arguments that mean nothing for a local path, repeated on every entry point. |
| `Catalog.open_remote(...)`. | Plain separation. | A third constructor and a second admin constructor, with the same body after the first lines. |

| Option for `cache_dir` | Gain | Cost |
|---|---|---|
| **Required (chosen).** | Disk use is visible, and a pod points it at its SSD. | One more argument. |
| A default under the user's cache directory. | Less to type. | Hidden gigabytes, and a home directory on a pod. |
| A temporary directory per process. | Nothing to clean. | Every process start pulls again, and stamps are lost, so `receive` runs every time. |

**Caching, local against blob:**

| Concern | Local catalog | Blob catalog |
|---|---|---|
| Inventory read | `local_fetch`, leaves cached in memory by hash | The same reader over `read`. `product(x).view()` reads `CURRENT`, one root, one probe and one leaf. `products()` reads every leaf, 7,000 GETs cold, in memory only. |
| `refresh()` | A few file reads | 3 GETs, and only changed leaves after them |
| Edition files | Read in place | Pulled once to `cache_dir`, keyed by immutable edition id |
| Observations | Local `data/` | Byte-range reads through DuckDB, never cached by statcatlib |
| Verification | Manifest SHA-256 at each `view()` | The same, plus each file's sha256 at pull and `receive` once per stamp |
| `catalog_id` | Read on each access | Read once at open, since it never changes |
| A view after a refresh | Stays on its edition | The same; its cached files stay |

**`_refuse_legacy_layout` on a remote catalog.** It does not run there. A blob
catalog reads `catalog.json`, and a missing one raises `FileNotFoundError` with the
same message. The function exists to protect a local directory from being written
into and to name a re-ingest. statcatlib 0.11 and sdmxlib 0.72 wrote no catalog to
object storage, and a remote bundle has no `catalog.json`, so it is refused either
way. Probing `meta.duckdb` and `bundle.manifest.json` would add two requests to name
a case nothing produces, and listing needs a right the read path otherwise does not
use.

**The embargo boundary (§10.1).** The boundary is the account or container, and
statcatlib cannot enforce it from inside a process:

- A principal that can run `Catalog.open` holds a role that can read embargoed
  bytes. `Catalog.open` keeps a public API from returning them, as on local disk.
  Docs state that an analyst's identity belongs on a live-only export, never on the
  catalog's container.
- `LiveInventory` holds only live entries. Leaf bytes, which name embargoed editions,
  stay in memory and never reach `cache_dir`. Only live editions' files do, so the
  cache holds nothing embargoed unless a later correction re-embargoes an edition.
- `FullInventory` is built only by the writer and `CatalogAdmin`, never by `Catalog`.
- `upload` reads nothing under `catalog.json`, `inventory/` or `CURRENT`, since the
  publish job has no access there (§10.1). A test uses a store that refuses those
  paths, reads included.
- A cached edition stays after a rollback or a withdrawal until an operator deletes
  it. Expunge (§11.3) purges pod disks and is unbuilt, so the risk table records this.

### D7: `correct` and `reinstate`

```python
CatalogAdmin.correct(*, from_seq: int, if_seq: int, reason: str) -> int
CatalogAdmin.reinstate(product_id: str, edition_id: str, *, reason: str) -> int
```

**`correct`'s closure, run against each head `commit` gives it:**

1. `full.seq != if_seq` raises `InventoryRuleError(rule="if-seq")`. A lost race lands
   here, so a retry cannot erase a concurrent commit. `if_seq` is required.
2. Read root `from_seq`. A root that is absent raises `rule="from-seq"` saying it was
   pruned or never written, and an invalid one says why it fails to decode. `from_seq`
   at or above `if_seq` raises the same rule. A missing or bad leaf of that root
   refuses too.
3. Find each product's restored leaf: its leaf in `from_seq`, or `None` for a product
   new since then.
4. Call sdmxlib's new rule on each product (S10):
   `correct(head_leaf, restored, head.retired, now=writer.now()) -> (Leaf, tuple[Tombstone, ...])`.
   One `now` serves the whole version. The rule does three things:
   - It drops from the restored leaf every entry the head tombstones. When that drops a
     product's live entry at some schema, it raises `TransitionError(rule="tombstone")`,
     naming the edition, since the product would lose its head with no version saying
     so. The fix is `reinstate`, then `correct` again, or a later `from_seq`.
   - It keeps, unchanged, each entry that is embargoed in the head and absent from the
     restored leaf (O4, option 1).
   - It returns a tombstone for every other entry the head holds and the restored leaf
     does not. A product new since `from_seq` gets a leaf with the head's `kind` and
     `lifecycle` and the entries kept above.
5. Build `retired` as the head's tombstones plus the new ones.
6. Verify every entry that differs from the head's: the stored manifest against the
   entry's `manifest_sha256`, then every listed file, through the helper `reinstate`
   uses. A failure raises `EditionCheckError`. A dropped entry gets no check, since its
   tombstone restores nothing.
7. Return `Change(leaves=<differing leaves>, retired=<from step 5>, embargo=None,
   extensions=None, built_from=from_seq)`. No change at all raises `ValueError`.

`try_commit` and `Change` need no new field.

**Tombstone fields.** `product_id` and `edition_id` come from the head's entry.
`retired_at` is the correction's time, the storage clock on blob, so the retention
window starts at the correction. GC and `prune` measure from it (§11.4), which keeps
the files for the window. A tombstone holds nothing else, so `reinstate` re-reads the
manifest.

**Who builds the tombstones.** sdmxlib's `retire` refuses a live or embargoed entry
(`_rules.py`, `rule="state"`), so it cannot serve. sdmxlib adds the rule.

| Option | Gain | Cost |
|---|---|---|
| **A new `sdmxlib.inventory.correct` rule (chosen).** | sdmxlib owns the state-transition rules (§6, RFC decision 8), and `admin.py` adds no rule of its own (`src/statcatlib/EGM.md`), so that invariant stands. The conformance suite can test it, and O4's choice lands in one function. | A sdmxlib release before Part 4, and a raised cap. S6 already asks for a release, so the two can ship together. |
| `CatalogAdmin` builds `Tombstone` values itself. | No sdmxlib release: `Tombstone` is public and the format accepts the result (table below). | A state transition lives outside sdmxlib, the one place in `admin.py` that would, and no conformance case covers it. |

**Products new since `from_seq`.** The product stays in the root with a leaf holding only
its kept embargoed entries, which is an empty leaf when it has none. Its other entries
become tombstones.

| Option | Gain | Cost |
|---|---|---|
| **Keep it with an empty leaf (chosen).** | `kind` and `lifecycle` survive, so a later `working_copy` keeps its refusals. `reinstate` has the leaf it requires. No field and no sdmxlib change. `retire` already produces such leaves. | `FullInventory.products()` lists a product `from_seq` did not hold, so the root's map is a superset of `from_seq`'s. `LiveInventory` drops it, so no public reader sees the difference. |
| Remove it from `products`. | The map equals `from_seq`'s. | `reinstate` needs a leaf, and `kind` and `lifecycle` would have to be rebuilt from the manifest. `Change`, `try_commit` and `_planned` each need a removal path. |

**What `sdmxlib.inventory` accepts**, run against 0.85.0 (`inventory/` at `11f18e3f`
adds nothing that bears on this):

| Call | Result |
|---|---|
| `next_root`, `validate_root`, `decode_root` | Accept tombstones for entries that were live, and an empty leaf. They require only that no tombstone repeats (`_check_root`). |
| `retire` | Refuses a live or embargoed entry (`rule="state"`), so it cannot build these. |
| `change_list` | Writes `{edition_id, from: "live", to: "tombstone"}` with no `entry`, and for a restored one `{from: "superseded", to: "live", entry}`. An empty-leaf product gets `leaf_sha256.to` set to the empty leaf's hash. `_marks` and `_check_item` handle these as they stand. |
| `decode_leaf`, `validate_leaf` | Accept `editions: ()`, and the leaf schema has no minimum. |
| `LiveInventory`, `FullInventory` | `head` raises `KeyError`, `products()` omits it; `FullInventory.products()` lists it. |
| `record` | Refuses a tombstoned edition (`rule="tombstone"`), so the journal repair cannot re-record one. `_named` counts a tombstone, so `settle_journal` clears its entry. `record` of a new edition on an empty leaf succeeds. |
| `reinstate` | Works on an empty leaf and on a restored one. |
| `rollback` | After `reinstate` on a product with a live edition, it moves the live one to `withdrawn` and the reinstated one to `live`. On a product with no live edition it refuses (`rule="if-live"`), also with `if_live=None`. |

**The sdmxlib changes.** The format and its validators need no change. Tombstones need
the `correct` rule (S10) and one spec change (S8). S9 is a separate, optional change for
the last row. Until it ships, an edition reinstated into a product with no live edition
goes live by a second `correct`, from the version that held it live, which restores that
whole state. A new seal also works, since the empty leaf takes a first edition.

**The conformance check.** `check_a_correction_links_and_diffs_against_the_version_before_it`
(`conformance.py:546-567`) calls `backend.commit_at(third, {"p": first.leaves["p"]},
kind="correct", built_from=1)`, with no `retired` and no `correct` rule. statcatlib's
backend maps that to `try_commit`, which does not change. So it still passes, and it
still expects `("live", None)` for edition 2, which now describes a correction with no
tombstone. It tests `change_list` and the chain, and `CatalogAdmin.correct` never writes
that item (S11). Part 4's own tests and S11's new check cover the tombstone.

**Verification** (`_verify_edition`). Locally, `check_edition(at="publish",
extensions={"statcat": check_catalog})` hashes every file. On blob, it pulls the
edition as in D6 and runs `check_edition(at="receive", remote=, engine=, lookup=)`,
which compares data files to a digest the store keeps. Both cost the edition's size,
and both are rare operations.

**History (H10).** The change list and `previous_root_sha256` follow root `seq - 1`.
`previous_inventory` is `from_seq`. `try_commit` already does this, and
`check_a_correction_links_and_diffs_against_the_version_before_it` covers it.
A restored leaf keeps its bytes, so its hash is unchanged and `state_changed_at`
keeps its old value. On blob, a restored leaf is a leaf the version changes, so it
gets its Put, and a missing leaf object returns.

**Question 7: the cross-product rules and the fast-forward check on a correction.**
`correct` runs the rules, through `commit`, unchanged, and it replaces the fast-forward check.

| Check | Option | Gain | Cost |
|---|---|---|---|
| Rules | **Run them and refuse (chosen).** | A correction cannot publish a state that breaks §6.5. A dropped structure entry surfaces as a refusal naming the URN. A clean `from_seq` passes. | An operator can be blocked. The way out is `reinstate` or another `from_seq`, and a blob run reads manifests (D5). |
| Rules | Warn only. | Recovery never blocks. | A stale or homeless state goes live with only a log line. |
| Rules | Skip, since `from_seq` passed once. | Fast. | Dropped tombstoned entries make the state one that never passed. |
| Fast-forward | **Skip it; `if_seq` plus `validate_leaf` replace it (chosen).** | Correction works. | A working copy built on the head's live edition then fails `NonFastForwardError` at its seal, as after a rollback (§6.4). |
| Fast-forward | Run it. | Uniform rules. | A correction moves the live edition backward by design, so this refuses nearly every useful correction. |

How the rules see it: `incoming` compares `leaves` with the head, so each entry
returning to `live` or `embargoed` counts as incoming, and `_planned` holds the restored
leaves, so a dropped entry is gone from the comparison. §6.5 item 6, chain continuity,
still holds, since `previous` may name a tombstoned edition.

**What a correction changes for editions recorded after `from_seq`.** Each one that is not
embargoed in the head leaves the leaf and becomes a tombstone, and its files stay for the
window. An
operator brings one back with `reinstate`, which enters it as `superseded`, and a
`rollback` makes it live. The working copies built on a dropped live edition fail
`NonFastForwardError` at their seal, as after a rollback.

A release after `from_seq` still returns to `embargoed`, because the edition's entry in
`from_seq` is embargoed, and it needs releasing again. The embargo setting stays the
head's, so it cannot turn off.

An entry embargoed in the head and recorded after `from_seq` stays embargoed, with its
`release_at`, pending O4. It is not incoming, so the rules and step 6 skip it. If its
`previous` was dropped, its release is refused, naming the withdrawn base (§6.4).

**The fold.** A tombstone stops the Phase 5 fold from re-recording a dropped edition from
its `pending/` entry, since §11.2 skips an edition recorded in `retired`, and `record`
refuses one too. A kept embargoed entry is skipped because a leaf records it.

**`reinstate`**: a change closure that requires a tombstone (sdmxlib raises
`rule="tombstone"`); reads the manifest; runs `_verify_edition`; calls sdmxlib's
`reinstate(leaf, retired, edition_id=, previous=, manifest_sha256=, schema_version=,
now=writer.now(), **recorded_routing(routing_fields(manifest), leaf.kind))`, so
Phase 5's change to `recorded_routing` reaches it too; and commits `event.kind:
reinstate`. A tombstone holds no digest, and an old root may name one. The manifest's
own hash becomes `manifest_sha256`, and the file checks are what prove it, as §6.4
states the precondition. `superseded` is not an active state, so the cross-product
rules see nothing incoming and read no manifest.

### Upload

```python
upload_edition(edition_dir: Path, location: BlobLocation, *, release_at: datetime | None = None) -> UploadReport
```

`statcat upload EDITION_DIR URL [--release-at TIME]` builds the location from the
environment and calls it. `UploadReport` holds `edition_id`, `product_id`, the
files `sent` and `kept`, and `registered: bool`.

Steps, none reading `catalog.json`, `inventory/` or `CURRENT`:

1. `read_manifest(edition_dir)`, refusing an unsealed directory. An `S3Store` is
   refused, naming the reason (W3).
2. For each `root: data` entry, `head`, and when absent `put(path, file, mode="create")`.
   A create is always one Put (F11), so Azure keeps its MD5 (F7). Present files stay,
   since data files are immutable and shared across editions (§4.1).
3. The same for the edition's own files, except `manifest.json`.
4. `manifest.json`, `mode="create"`. Present and with other bytes raises, because an
   edition is never rewritten.
5. Verify: `check_edition(edition_dir, at="receive", lookup=lookup)` for the lake,
   bindings and `statcat` section; `verify_remote(manifest, lookup, tiers=all)` for
   every uploaded file; and a GET of the uploaded `manifest.json`, compared by SHA-256.
   The lookup caches, so no file is asked twice.
6. `put("pending/<product>/<edition>.json", {edition_id, release_at}, mode="create")`.
   `AlreadyExistsError` sets `registered=False` and is no error.

Any finding raises `UploadCheckError` with its findings, before step 6, so a bad
upload registers nothing.

**File size.** A single-shot Put reads the file into memory (F11) and stops at 5,000
MiB (F12). `upload` refuses a file larger than the limit, naming it. Two alternatives
wait for a real need: a streaming signed PUT through `httpx` (F9), which saves the
memory, and Put Block plus Put Block List through signed URLs with
`x-ms-blob-content-md5`, for files past the limit.

### Test setup: an edition in a blob Inventory

Phase 4 has no seal on blob and no fold, so nothing puts a real edition into a blob
Inventory. Parts 3 and 4 need one, and so does any drill before Phase 5.

```python
statcatlib.testing.record_uploaded(
    location: BlobLocation, edition_dir: Path, *, embargoed: bool = False, release_at: datetime | None = None,
    reason: str = "record_uploaded",
) -> Edition
```

It runs `upload_edition(edition_dir, location, release_at=release_at)`. Then, through a
blob `InventoryWriter.commit` with `event.kind: seal`, it applies sdmxlib's `record` with
`routing_fields` and `recorded_routing`, as the fold will. The cross-product rules run,
reading the uploaded manifests. It leaves the `pending/` entry in place, and Phase 5's
fold skips an edition the Inventory records (§11.2), so a catalog built this way stays
valid. Editions come from a local catalog (`codr_product`, `synthetic_catalog`), with
`previous` chains from there.

| Option | Gain | Cost |
|---|---|---|
| **`statcatlib.testing` (chosen).** | Part 3, Part 4, drills and sdmxlib-rest's Phase 6 tests share it, beside `synthetic_catalog`. Its module says it is for tests, and the docs keep it out of the operator guide. | It ships in the wheel, so a determined user can record without the release job. It adds no power: the identity already needs write on `inventory/`, and `CatalogAdmin` could do as much. |
| `tests/stores/_stores.py` only. | Nothing public. | sdmxlib-rest and bench scripts cannot reach it, so each would rewrite the record step. |
| Public `CatalogAdmin.record_uploaded`. | A supported manual path until Phase 5, for a real operator. | It commits the API before the fold is designed, and offers a route around `pending/` that §11.2 reserves to the release job. |

## Alternatives Considered

Each decision above carries its own table. Three wider choices:

*   **Run the blob writer against ADLS DFS.** §6.6 forbids DFS create plus append.
    `object_store` reads only the account name from a `dfs.core.windows.net` URL
    (`azure/builder.rs:743`) and writes with Put Blob. Every write in the runs carried
    `x-ms-blob-type`, and Part 1 asserts that header. No real DFS endpoint was tried.
*   **Copy a whole catalog as Part 5's corpus.** GH-98 replaces it, since the read
    touches only `CURRENT`, roots and leaves. The full data corpus moves to Phase 6 (W1).
*   **Keep a write-through leaf cache on disk to speed cold reads.** Leaves name
    embargoed editions, so it moves embargoed metadata out of the boundary.

## Non-Functional Requirements

*   **Cost.** One-product commit: 3 writes, about 9 reads. Release of 200 products:
    200 leaf Puts, a root and `CURRENT`.
*   **Latency.** Cold read of 7,000 leaves: 70 s in sequence at 10 ms a GET, and about
    1.1 s at 64 threads by estimate. Part 5 measures the counts.
*   **Atomicity.** No reader sees a partial root, leaf, or `CURRENT` (single Put).
*   **Compatibility.** No format change. A local catalog behaves as today.
*   **Coverage.** The `stores` tier stays outside the 85% floor's selection, as
    `just coverage` selects `unit or integration`, so `store/blob.py`'s rules need
    unit tests over `MemoryStore`.

### Risks and limits

| Risk | Mitigation |
|---|---|
| Nothing ran on real Azure (the unconfirmed list) | Part 1's PR has a manual checklist for one real-account run before Phase 5 relies on it. The clock, the `oid` and the signing are each small and replaceable. |
| An upstream `obstore` minor changes behaviour | Capped below the next minor. The tier reruns F1, F2, F10 and F11's cases as tests. |
| An anonymous `HEAD` is blocked | `now()` refuses and names the cause. A bearer `HEAD` on `catalog.json` also returns `Date` (F18), and is the fallback on a token credential. |
| A leaked orphan leaf per crashed commit | Bounded by one version's changed leaves (§6.9). |
| A cached edition outlives a withdrawal | Recorded limit until `expunge` and Phase 6's placement. Docs say to delete the directory. |
| The pre-push hook gets slower, and a push needs Docker (W6) | About 20 s once images are pulled, on sdmxlib's figures for its tier, and more for the OAuth container. The failure message names the fix. |
| A correction's tombstones outlive the window before anyone reinstates | The files go with the tombstone's window, as for any retirement. The correction's `changes` name every dropped edition. |

## Detailed Implementation Plan

Each part lands as its own PR, in this order. Parts 1 to 4 can release separately.
Each runs `just ci`, updates `EGM.md` beside the code, `CHANGELOG.md`, and
`CLAUDE.md` where it names a new module or command.

### Part 1: object storage and the blob Inventory writer

*   **`pyproject.toml`:** the `blob` extra, `obstore` and `trustme` in the `dev`
    group, and the `stores` marker. Cap as in D2.
*   **`justfile`:** `test-stores` (`uv run pytest tests/stores -m stores -rs`), and
    `ci: check test test-integration test-stores docs-build`.
*   **`.githooks/pre-push`:** the comment names the tier.
*   **sdmxlib:** S6's export, with the cap raised here if it ships in a new minor.
*   **`src/statcatlib/store/storage.py` (Create):** `InventoryStorage`,
    `LocalInventoryStorage`.
*   **`src/statcatlib/store/blob.py` (Create):** `BlobLocation`, `_ObjectOps`,
    `BlobInventoryStorage`, `TokenIdentity`, `MAX_CLOCK_SKEW`, `read_many`.
*   **`src/statcatlib/store/inventory.py` (Modify):** take a storage; split out
    `_commit_locked` inside `with storage.exclusive()`; add `now()`.
*   **`src/statcatlib/store/cross_product.py` (Modify):** `ManifestArtefacts(fetch)`,
    `prefetch`, `to_read`. Needs S6.
*   **`src/statcatlib/admin.py` (Modify):** `CatalogAdmin.open(BlobLocation)`,
    `CatalogAdmin.create`, `writer.now()`; `prune` refuses a blob catalog.
*   **`tests/stores/{__init__,_stores,conftest}.py` (Create):** sdmxlib's model, with
    one change. The Azurite and SeaweedFS images at sdmxlib's digests and a free local
    port are the same. A session fixture calls `pytest.fail` naming the fix when no
    container runtime exists (W6), so `just ci` fails. Part 2 adds the OAuth container.

Unit tests use `MemoryStore` in place of GH-98's `LocalStore`, because `LocalStore`
has no update mode for `CURRENT` (F2).

| Test | File, tier | Covers |
|---|---|---|
| `run` and `run_history` over Azurite; `run` over SeaweedFS | `tests/stores/test_blob_conformance.py`, stores | conformance |
| Kill between leaf and root: the `Backend`'s `commit_interrupted` writes the leaf alone; head reads, next commit lands | same | killed writer |
| Lost race: two writers on one base, the loser's root links the winner and diffs against it | `tests/stores/test_blob_writer.py`, stores | lost race |
| 2.x `catalog.json` is refused, and the message names it | same; `check_a_writer_without_stage_2...` | 2.x |
| An 8 MB root makes one request; `If-None-Match: *` is sent | same, by `_ObjectOps` and a 5 MiB + 1 case | single-shot root |
| A one-product version at 50 products makes exactly one leaf Put; a carried leaf gets none; a present changed leaf is Put again | `tests/test_blob_storage.py`, unit, `MemoryStore` | leaf rule |
| `CURRENT` moves forward only, under races | same | `CURRENT` |
| A create with `AlreadyExistsError` or a transport fault, equal and other bytes | same, fake `_ObjectOps` | F13 |
| Clock: ahead over the bound refuses, behind passes, missing `Date` refuses | same | D4 |
| `TokenIdentity` on a hand-made JWT, no `oid`, declared actor, none refuses | same | D3 |
| Core imports with `obstore` absent; the error names the extra | `tests/test_public_api.py`, unit | D2 |
| `to_read` covers what the rules read | `tests/test_cross_product_rules.py`, unit | D5 |

### Part 2: upload

*   **`src/statcatlib/upload.py` (Create):** `upload_edition`, `UploadReport`,
    `UploadCheckError`, `_lookup`.
*   **`src/statcatlib/store/blob.py` (Modify):** `AzureBlobRest`.
*   **`src/statcatlib/testing.py` (Modify):** `record_uploaded`.
*   **`tests/stores/_stores.py` (Modify):** the OAuth Azurite container and a token
    minter (D2).
*   **`src/statcatlib/cli/__init__.py` (Modify):** `statcat upload`.
*   **`docs/`:** the operator guide and `catalog-format.md`'s `pending/` section.

| Test | Tier | Covers |
|---|---|---|
| Every write recorded: none under `inventory/`, none at `CURRENT` or `catalog.json`, and no read there either | stores | upload writes |
| A second upload writes no second `pending/` entry, sends no file again, reports `registered=False` | stores | second upload |
| The manifest is the last edition file written; a corrupt blob fails before `pending/` | stores | §11.2 order |
| A file lost after step 2 gives a `remote` finding; Azure's stored MD5 compares equal to the manifest's | stores | digest |
| An `S3Store` is refused, naming the reason | unit | W3 |
| `AzureBlobRest.head` with a bearer token reads `Content-MD5` from a blob `obstore` wrote, on the OAuth container | stores | D2 |
| The same over `sign` on the plain container; no identity and no key refuses before any write | stores, unit | D2 |
| `record_uploaded` makes an edition live, and with `embargoed=True` embargoed; a rule failure writes no root | stores | Test setup |
| A 64 MiB data file over Azurite is one PUT | stores | F11 |

### Part 3: remote catalogs

*   **`src/statcatlib/catalog.py` (Modify):** `Catalog.open(BlobLocation)`, routing
    `refresh`, `description` and `catalog_id` through the storage's `read`;
    `Product.view` and `Product.editions` through the pull.
*   **`src/statcatlib/store/blob.py` (Modify):** `pull_edition`.
*   **`docs/`:** the guide, with the embargo boundary stated.

Fixtures seal editions in a local catalog and put them in the blob catalog with
`record_uploaded`.

| Test | Tier | Covers |
|---|---|---|
| A wrong `manifest_sha256`: `IncompatibleCatalogError`, and the recorded reads hold only that manifest of the edition | stores | wrong manifest |
| A catalog with embargoed entries: `products()` and `view()` never return one; no GET under its edition; none in `cache_dir` | stores | embargo |
| Rows through a blob view equal the local read | stores | remote read |
| The second `view()` pulls nothing and runs `open` | stores | caching |
| A two-process rename race leaves one copy | stores | pull |
| A string URI, no `cache_dir`, and `Catalog.create(BlobLocation)` each raise as D6 says | unit | D6 |
| Missing `catalog.json` raises `FileNotFoundError` | stores | D6 |

### Part 4: `CatalogAdmin.correct` and `reinstate`

*   **sdmxlib (first):** the `correct` rule (S10), with a conformance check, and a raised
    cap here.
*   **`src/statcatlib/admin.py` (Modify):** `correct`, `reinstate`, `_verify_edition`.
*   **`docs/`:** the operator guide, with what a correction loses.

Tests run locally in the integration tier, over a local catalog. Each also runs once
over Azurite, with editions from `record_uploaded`.

| Test | Covers |
|---|---|
| A stale `if_seq`, and a race landing between the read and the create, both refuse | stale `if_seq` |
| `from_seq` absent, undecodable, at or above `if_seq`, and with a missing leaf | pruned or invalid |
| A restored entry with a corrupted file or manifest refuses | failed verify |
| A tombstone added after `from_seq` stays, its entry is dropped; a dropped live entry refuses | tombstones |
| With embargo on at the head and off at `from_seq`, it stays on | embargo |
| Root `seq - 1` is linked, `changes` diff against it, `previous_inventory` is `from_seq`, `event.kind` is `correct` | history |
| A restore that breaks item 4 raises `CrossProductRuleError` | Question 7 |
| An entry embargoed in the head and recorded after `from_seq` stays, with its `release_at` and no tombstone; `cancel` then withdraws it, and a dropped `previous` makes its release raise `NonFastForwardError`. These rows change if O4 picks another option. | O4 |
| A registration in `pending/` for a dropped edition and for a kept one is skipped; `record` refuses the dropped one | §11.2 |
| An edition recorded after `from_seq`: its entry leaves the leaf, a tombstone holds the correction's time, and `changes` records `from: <state>, to: tombstone` with no `entry` | W2 |
| That edition, on a product with a live edition, can be reinstated and rolled back to | W2, CUJ 4 |
| A product new since `from_seq` keeps an empty leaf, its `kind` and `lifecycle`. `Catalog.products()` omits it, `CatalogAdmin.products()` lists it, and its edition can be reinstated. | W2 |
| An edition reinstated into a product with no live edition goes live by a second `correct`, and a new seal on the empty leaf works | W2, S9 |
| `doctor` reports no `unrecorded edition`, and a journal entry for a dropped edition is cleared and not recorded | W2 |
| `prune` keeps a dropped edition's directory inside the window and deletes it after | W2 |
| An edition released after `from_seq` returns to `embargoed` and can be released again | W2 |
| `reinstate` refuses a bad manifest and a bad listed file; a good one is `superseded`, with routing fields, and `artefacts` on a structure entry | reinstate |
| A reinstated edition can be rolled back to | CUJ 4 |

### Part 5: the Inventory read measurement

This corpus holds Inventory documents only, and the data corpus is out of this design.

*   **`src/statcatlib/testing.py` (Modify):** `inventory_corpus(writer, products=7000,
    *, embargoed=0.1, artefacts=False, batch=500, seed=0)`. It builds leaves with
    sdmxlib's `record`, one edition each, with routing fields and hashed placeholder
    `manifest_sha256` values, keeps `corpus_embargoed`'s share of each run, and writes
    with `try_commit`, which runs no rule since no manifest exists. A commit per 500
    products means 14 roots and 7,000 leaf Puts in all, each one a leaf that version
    changes. A commit per product would write quadratic root bytes for no gain to the
    read.
*   **`bench/inventory_read.py` (Create):** builds the corpus, twice: without
    `artefacts` (about 510 bytes a leaf, 3.6 MB), and with them (about 2.4 KB, 17 MB),
    the Phase 5 shape. Each read counts through a recording `Fetch`.
*   **`bench/inventory-read-results.md` (Create):** the record.

| Case | Records |
|---|---|
| Cold `LiveInventory.refresh()` then `editions()`, sequential | fetch count, bytes, Azurite wall time |
| Cold, 64-thread leaf prefetch | the same |
| `head(one product)` cold | the same |
| `refresh()` after a one-product and a 200-product version | the same |
| `FullInventory` for the same | the same |

Azurite's wall time excludes real storage latency. The report says so and estimates
wall time as `GETs x 10 ms` in sequence and `ceil(GETs / 64) x 10 ms` concurrent, in
the form of `bench/corpus-results.md`.

A 50-product run asserts counts in the `stores` tier: a cold read touches only
`CURRENT`, roots and leaves. The full run stays outside `just ci`.

**The handoff.** The specification lives in sdmxlib, so the result reaches §6.5 through
an sdmxlib PR, or a subagent the hub session delegates to. The baked-summary choice is
the owner's, on these numbers, against the readiness budget of §7.1. This design
proposes no threshold, since the spec gives no number for a pod's Inventory read.

### Acceptance map

| GH-98 acceptance | Part | Test |
|---|---|---|
| `conformance.run` passes over Azurite, with stage-1 history cases | 1 | `test_blob_conformance.py` |
| A writer killed between leaf and root leaves the head readable and writable | 1 | `test_blob_conformance.py` |
| A lost race recomputes `changes` and `previous_root_sha256` | 1 | `test_blob_writer.py` |
| The blob writer refuses 2.x and names the reason | 1 | `test_blob_writer.py` |
| An upload writes nothing under `inventory/` or `CURRENT`, by a recording store | 2 | `test_upload.py` |
| A second upload writes no second `pending/` entry | 2 | `test_upload.py` |
| A wrong `manifest_sha256` is refused before any other file | 3 | `test_remote_catalog.py` |
| `Catalog.open` never returns an embargoed entry | 3 | `test_remote_catalog.py` |
| `correct` refuses stale `if_seq`, pruned or invalid `from_seq`, and failed files; keeps tombstones and embargo; tombstones each entry it drops | 4 | `test_correct.py` |
| A correction links root `seq - 1`, and `changes` diff against it | 4 | `test_correct.py` |
| An edition a correction dropped can be reinstated | 4 | `test_correct.py` |
| `reinstate` refuses bad files; the entry keeps routing fields and `artefacts` | 4 | `test_reinstate.py` |
| The 7,000-leaf measurement is in spec §6.5, with the summary decision | 5 | `bench/inventory-read-results.md`, then an sdmxlib PR |
| `just ci` passes | all | the gate, with `test-stores` |

### Verification Plan

*   `just ci` for each part, on a machine with a container runtime, so the tier runs.
*   One manual pass against a real Azure account before Phase 5 relies on Part 1,
    answering the unconfirmed list: a commit with a workload identity (`oid`), the
    `Date` `HEAD`, a bearer `HEAD` for `Content-MD5`, and a duplicate create's status.
*   `uv run python bench/inventory_read.py` for Part 5, recorded with machine and
    versions, as `bench/corpus-results.md` is.

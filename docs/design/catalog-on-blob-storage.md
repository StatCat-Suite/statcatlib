# Phase 4: the catalog on blob storage

*   **Author:** Gabriel Gellner
*   **Status:** Draft, revision 3, with the owner's decisions of 2026-11-04 applied. A
    second, narrower EGM review is pending. Revision 2 had an EGM review
    ([report](egm/catalog-on-blob-storage/review_report.md): 4 Blocker, 37 Major, 33
    Minor), which revision 3 applies.
*   **Date:** 2026-11-04
*   **Issue:** GH-98. Stage 2's later needs: GH-125. Suite tracker:
    StatCat-Suite/statcat-suite.github.io#19.
*   **Read against:** statcatlib `e7eba47`, whose code equals `origin/main` `974ea27`
    (0.14.0); sdmxlib `11f18e3f` (`origin/main`, after 0.85.0) for code, and sdmxlib
    `011312c3` (`docs/phase-4-restated`) for the spec and the plan; the hub at `cb144f6`,
    with the suite design at revision 4. Every `file:line` is at these commits. Find each
    site by its symbol and use the number as a hint.
*   **Inputs:**
    *   GH-98's body, which is the scope and overrides the plan where they differ.
        Moving `correct` and `reinstate` to GH-130 (W7) still has to reach that body.
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        ("the spec"), read on `docs/phase-4-restated`, which must merge before Part 1:
        §4.1, §4.6, §4.7, §6.1 to §6.7, §6.9, §7.1, §10.1, §11.2 to §11.5.
    *   [sdmxlib's
        plan](https://github.com/StatCat-Suite/sdmxlib/blob/main/docs/design/sharded-bundle-catalog-plan.md),
        Phase 4, at the same commit.
    *   The owner's leaf rule of 2026-11-04: a blob writer writes each leaf its version
        changes with an unconditional single-shot Put, with no existence check, on every
        blob store, and rewrites no carried-forward leaf (spec §6.6).
    *   The suite design [Inventory: carry each version's changes in its
        root](https://github.com/StatCat-Suite/statcat-suite.github.io/blob/main/docs/design/inventory-history-log.md),
        decisions H21 and H23 and its lease section; GH-125.
    *   `bench/corpus-results.md`: 6,300 manifests (91 MB) in one cold read at 7,000
        products.

--------------------------------------------------------------------------------

## Overview

statcatlib's catalog lives on local disk. `InventoryWriter` holds a `Path`
(`store/inventory.py:300-311`), and `Catalog.open` checks `is_file`
(`catalog.py:594-620`). Phase 4 builds the parts of a catalog on blob storage that sit
below the release job, in four parts:

1. a storage seam in `InventoryWriter`, with a blob implementation over `obstore`, so one
   set of rules commits versions on both;
2. `statcat upload`, which copies a sealed edition to `products/` and registers it in
   `pending/`;
3. `Catalog.open` over a blob catalog, returning live editions only;
4. a measured 7,000-leaf Inventory read over documents generated without data files.

A `stores` test tier over one containerised emulator joins `just ci`.

**A catalog on blob storage does not serve production after Phase 4.** An upload waits in
`pending/`, and nothing in production records it until Phase 5's fold. Only tests and the
bench write real versions.

| Capability | Local disk | Azure Blob Storage | S3 |
|---|---|---|---|
| Seal a working copy | Today | Never: a working copy stays local | Never |
| Commit a version, no entry incoming | Today | Part 1 | Part 1, not in the gate |
| Commit a version with the cross-product rules | Today | Phase 5 | Phase 5 |
| Upload an edition | Not applicable | Part 2 | Refused (W3) |
| Fold `pending/`, release on schedule | Not applicable | Phase 5 | Phase 5 |
| `Catalog.open` | Today | Part 3 | Part 3's path, with no way to place an edition |
| `CatalogAdmin` | Today | A later phase | A later phase |
| `correct`, `reinstate` | GH-130 (see Moved out) | GH-130 | GH-130 |

Three findings from checking `obstore` 0.12.1 shape the design:

1. `obstore` cannot set or read `Content-MD5` or CRC32C, and it exposes no `Date` header.
   Azure computes an MD5 for a single-shot Put by itself, and a `HEAD` with the identity's
   bearer token, through `httpx`, reads it back.
2. `obstore` has no lease call, no conditional delete and no per-request header, so stage
   2's Azure duties need a small REST client beside it. Part 2 builds that client for the
   digest read.
3. `check_edition(at="receive", lookup=)` checks only `data/` in the store. Upload adds
   `verify_remote` for the edition's own uploaded files.

### Terms

| Term | Meaning here |
|---|---|
| Inventory | The chain of documents deciding each product's live edition (spec §6). |
| Inventory root | `inventory/<seq>.json`, one per version (§6.2). "Catalog root" is the catalog's top-level prefix or directory, a different thing. |
| Leaf | `inventory/leaves/<sha256>.json`, one product's entries (§6.3). |
| Head | The newest Inventory version. |
| Live edition | A product's entry in state `live`, the one a reader gets. |
| Stage 2 | The catalog lock and expiry of §6.9, GH-125 parts 2 and 3. |
| Fold | Phase 5's job step that records `pending/` registrations as entries (§11.2). |
| Baked summary | A possible single document of all leaves (plan, Future Improvements), decided by Part 4's numbers. |
| The cross-product rules | §6.5 items 4, 7 and 8, in `store/cross_product.py`. |
| Probe | The reader's GET of `seq + 1`, which finds it absent (§6.6). |
| GH-98 question N | The Nth question in GH-98's "Local design first". |

Phases are the plan's: Phase 5 is release and GC (GH-52), Phase 6 is serving, Phase 9 is
rollout and restore drills.

## The Problem

The spec's blob protocol has five needs the local writer does not meet:

- Exclusive creation of an Inventory root must win with the object complete, which on blob
  storage is a single-shot `Put Blob` with `If-None-Match: *` (§6.6).
- `CURRENT` must move forward by a conditional write (§6.1).
- A server writer takes its time from the storage `Date` header (§6.6 item 4).
- `event.actor` must be the writer's storage identity (§6.2). The local writer records
  `<os user>@<hostname>` (`store/inventory.py:237-243`).
- The cross-product rules read each manifest (`store/cross_product.py:138`) and every leaf
  (`:214`), about 13,300 GETs on one cold commit at 7,000 products.

## Goals

*   One writer's rules on local disk and on blob storage, with the local writer keeping its
    atomic link publish.
*   A blob writer that passes `sdmxlib.inventory.conformance.run` and the stage-1 history
    checks over Azurite.
*   An upload that writes nothing under `inventory/`, needs no read of it, and leaves one
    `pending/` entry however often it runs.
*   A remote `Catalog.open` that never returns an embargoed entry and refuses a wrong
    manifest before it reads any other file of the edition.
*   A recorded Inventory read cost that lets the owner decide on the baked summary.

## Non-Goals

*   `CatalogAdmin.correct` and `reinstate`, which move to GH-130 (W7).
*   `CatalogAdmin` over a blob catalog, and the cross-product rules' manifest reader on blob.
    Neither has a Phase 4 caller (Land order).
*   The fold of `pending/`, the release job and `release_due` (Phase 5, GH-52).
*   `EmbargoReader` (its first caller, Phase 5 or 6).
*   The catalog lock, opt-in, expiry, the `HISTORY` lease and `If-Match` leaf deletes (GH-125
    parts 2 and 3).
*   Filling `artefacts` for data products and the leaf-backed reader (GH-125 part 4).
*   The full data corpus (W1).
*   Upload to S3 (W3).
*   Sealing on blob storage.
*   Evicting the local edition cache. Phase 6's router owns placement on pods.

## Open Questions

Each open item is a contract question for another repository. The Contract changes and
handoff table gives its owner and order. Owner decisions are under Recorded decisions.

**Spec questions for sdmxlib.** This design leaves each to the spec's owner.

| # | Where | Question | Proposal |
|---|---|---|---|
| S1 | §6.4 table, against §10.1 and §11.2 | The table says the publish job moves a sealed edition to `embargoed`. §10.1 gives that job no access to `inventory/`, and §11.2 says only the release job writes versions. Its fast-forward precondition also contradicts `record` (`_rules.py`). | Change the table's "who" to the fold, and fix the precondition. |
| S2 | §6.2, against §10.1 | §6.2 says the actor is "never from the caller". §10.1 says an operator who needs another identity sets it explicitly. | The actor is fixed when the writer's storage is constructed, and no method takes one (D3). |
| S3 | §6.6 item 4 | "On a server" is undefined. | Read it as every writer over a blob store (D4). |
| S7 | §10.1 | The publish job's "write" must include reading its own uploads for the digest check. | State it. Storage Blob Data Contributor includes read. |
| S12 | §11.4 | GC can delete a pending edition's files: an uploaded edition is listed by no recorded manifest, so GC deletes its files, and the fold later records an edition whose bytes are gone (review M5). | Open. Left to the spec's owner, before Phase 5 builds GC. |
| S13 | §11.2 step 1 | The fold has no source for `lifecycle`: `record` requires it, the manifest has none, and `pending/` holds none (review M7). | Open, for Phase 5. Candidates are the `statcat` section and the `pending/` document. |
| S14 | §11.2 | The `pending/` document's byte format. D7 defines what upload writes, and Phase 5 reads it. | Adopt D7's form as the contract. |
| S15 | §4.6 line 277 | Extensions at `open` are "section only", while `check_catalog` runs everything, so each `view()` pays for it. | Align the table, or give `check_catalog` the profile. |

**Code changes in sdmxlib.**

| # | Change | Why |
|---|---|---|
| S16 | StatCat-Suite/sdmxlib#371: `references: bool = True` on `check_edition` and `check_on_open`, with a stamp rule (review B1). | A structure product's references may resolve in another structure product (§6.5 item 9), and with no `structures=` each one is a finding (`edition/_check.py:755-759`). Upload may not read the Inventory, so it cannot name those editions. |
| S6 | Export `decode_manifest` from `sdmxlib.edition`, in Phase 5 with its first caller. | Phase 5's `ManifestArtefacts(fetch)` reads a manifest from bytes (D5). `read_manifest` takes a directory. |
| S4 | A tier argument on `check_edition` (issue, before Phase 6). | It reports a missing lazy file at every profile (`edition/_check.py:382-405`), so a pod holding the required tier alone fails `check_on_open`. Phase 4 pulls both tiers (D6). |
| S17 | Refuse absolute and `..` paths in manifest `files` (review m5). | Part 3 also checks on pull, so this does not block. |

### Contract changes and handoff

In order. "Gates" names what cannot start until the row is done.

| Order | Change | Owner | Gates |
|---|---|---|---|
| 1 | Merge sdmxlib's `docs/phase-4-restated` PR: the leaf rule, W4, S2, S3 and S7 | sdmxlib | Part 1, Part 2 (S7) |
| 2 | Part 1, then Part 4, on sdmxlib 0.85 | statcatlib | Part 2, and the summary decision |
| 3 | The StatCat-Suite/sdmxlib#371 release: S16 alone | sdmxlib | Part 2, Part 3 |
| 4 | Raise the cap in `pyproject.toml`, in Part 2's PR | statcatlib | Part 2 |
| 5 | Part 2, then Part 3 | statcatlib | Phase 5 |
| 6 | Part 4's result into §6.5, with the owner's summary decision. The hub session delegates this PR. | sdmxlib | GH-98's last acceptance item |
| 7 | S1, S12, S13, S14, S15, and S6 with its first caller | sdmxlib, GH-52 | Phase 5 |
| 8 | The fold owns the reference check for an edition with no bindings: it runs `check_edition(at="receive", remote=, structures=)` before it records the edition, since `check_edition` refuses `remote=` at `publish` (`edition/_check.py:262`). Nothing else checks those references after the seal, so this is a Phase 5 acceptance item. | statcatlib, GH-52 | Phase 5 |
| 9 | Issue: S4 | sdmxlib | Phase 6 |
| 10 | GH-130 exists, and its design files the sdmxlib issues for S9, S11 and its `correct` rule | statcatlib, sdmxlib | Phase 9's restore drill |

The hub's suite design needs no change for Phase 4. The moved work's tombstone wording
(`inventory-history-log.md:196-197`) goes with row 10. S5 from revision 2 is no contract
change: upload adds `verify_remote` (D7). S8 is done at `011312c3`, apart from what moves.
The plan's Phase 4 section and GH-98 line 61 say `check_edition` verifies every file, which
holds only with `verify_remote`; the plan and the issue need the moved scope too.

## Critical User Journeys (CUJs)

### CUJ 1: A publish job uploads an embargoed edition

The job seals an edition locally, then runs `statcat upload` with its workload identity and a
`--release-at`. Data files go first, then the edition's files, then the manifest. Every listed
file is checked against a digest the store keeps. `pending/<product>/<edition>.json` appears,
and nothing else changes. A crash and a rerun send only what is missing and leave one
registration. A rerun with another release time is refused, naming both. The edition then waits
for Phase 5.

### CUJ 2: A job reads a blob catalog

```python
location = BlobLocation(
    "az://catalogs/stc/",
    store=AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=TokenIdentity(provider)),
    secrets=[sl.StoreSecret("azure", options=options)],  # W5: Phase 6 decides the production form
    cache_dir=Path("/var/cache/statcat"),
)
with Catalog.open(location) as cat:
    view = cat.product("14100287").view()
    view.query(AggregateDataset).limit(5).collect()
```

The first `view()` pulls the edition's catalog files to the cache, checks them, and reads
observations from the store. An embargoed product has no live edition and raises
`NoLiveEditionError`. Until Phase 5's fold, a catalog in this state comes only from the test
helper `record_uploaded` (D8).

## Technical Plan (Architecture)

### `obstore` 0.12.1 behaviour

Checked on 2026-11-04 against `obstore` 0.12.1 (uploaded 2026-10-27, the newest on PyPI,
`requires-python >=3.11`, no Python dependencies, abi3 wheels of 4.6 to 5.6 MB), which builds on
the `object_store` crate 0.14.2 (`Cargo.lock`). The runs used sdmxlib's pinned Azurite and SeaweedFS
images in a throwaway environment, through a recording proxy that logged every request. Sources
are marked **stub** (the `.pyi` files), **src** (the sdist or the crate), **run** (Azurite, or
SeaweedFS where stated) and **docs** (Microsoft Learn, fetched that day). The review partly verified
these against the wheel and contradicted none.

| # | Fact | Source |
|---|---|---|
| F1 | `put(mode="create")` sends `If-None-Match: *` and `x-ms-blob-type: BlockBlob`. A second create raises `AlreadyExistsError` (Azurite 409, SeaweedFS 412), and the object keeps its bytes. | run; src `azure/client.rs:760,770-775` |
| F2 | `put(mode={"e_tag": etag})` sends `If-Match`. A stale or absent target raises `PreconditionError`. `GetResult.meta` and `PutResult` carry the `e_tag`. Azure's `version` was `None`. `MemoryStore` supports it. `LocalStore` raises `NotImplementedError`, so `LocalStore` cannot hold `CURRENT`. | run, on Azurite, SeaweedFS and both stores; src `:761-764` |
| F3 | No response exposes `Date`. `ObjectMeta` holds `path`, `last_modified`, `size`, `e_tag`, `version`. `PutResult` holds `e_tag`, `version`. `GetResult.attributes` held only `Content-Type`. The sdist's Rust sources do not mention the header, and the crate's `client/header.rs` reads only `Last-Modified`, `ETag`, size and version from a response. | stub `_list.pyi:10-40`, `_put.pyi`; run; src grep |
| F4 | A response to any request carries `Date`, also an anonymous `HEAD`: Azurite answered 400 or 403, SeaweedFS 405, each with `Date`. Azure documents `Date` on responses as service-generated. | run; docs Put Blob |
| F5 | CRC32C cannot be set. `checksum_algorithm` takes `sha256` or `crc64nvme`, and `"crc32c"` raises a config error. `sha256` sends `x-amz-checksum-sha256`, which `obstore` never reads back. | src `aws/checksum.rs:25-30`, stub `_aws.pyi:60`; run on SeaweedFS |
| F6 | `Content-MD5` cannot be set. `attributes={"Content-MD5": ...}` becomes `x-ms-meta-content-md5` (user metadata), which Azurite refuses with 400. `client_options.default_headers` sends `x-ms-blob-content-md5` outside the SharedKey signature, and Azurite answers 403. | src `attributes.rs:18-27`; run |
| F7 | A single-shot Put Blob keeps an MD5 the service computes. `Get Blob Properties` returns it. A multipart upload (Put Block List) keeps none, and `obstore` cannot send the header that sets one. | run: `content-md5` on `HEAD` after a Put, absent after block upload; docs Put Blob, Put Block List |
| F8 | `obstore` does not read `Content-MD5`. With an account key, the test path, a signed GET URL (`sign(store, "GET", path, ...)`, `sp=r`) answers a `HEAD` with `Content-MD5`, `Content-Length` and `ETag`. `sign(store, "HEAD", ...)` yields an empty `sp=` and 403, since only GET, PUT and DELETE map to a permission. | run; src `azure/credential.rs:391-403` |
| F9 | A signed PUT URL accepts added headers. A wrong `Content-MD5` gives 400, a right one 201, and `If-None-Match: *` a 409 on the second try. | run |
| F10 | Multipart starts when the size exceeds `chunk_size`, 5 MiB by default: 5,242,880 bytes went as one PUT, 5,242,881 bytes as blocks and a block list. An iterable goes multipart at any size, and a `Path` by its size. | run; src `obstore/src/put.rs:81-82,215-218,322-331` |
| F11 | `use_multipart=False` forces one PUT at any size, and it reads the whole input into memory. A `mode` other than overwrite forces one PUT, even for an iterable, and even with `use_multipart=True`. | run, 5 MiB + 1, 12 MiB `Path`; src `put.rs:328-333`; stub `_put.pyi` |
| F12 | Put Blob's limit is 5,000 MiB on service version 2019-12-12 and later. `object_store` sends version 2023-11-03 (`azure/credential.rs:49`). | docs; run |
| F13 | A 5xx, 429 or 408 is retried on every request, a create included. A timeout is retried only on an idempotent request, and a create or an update is not idempotent. Defaults: 10 retries, 180 s. So a create can raise `AlreadyExistsError` against its own earlier success, and a timeout leaves its outcome unknown. | src `client/retry.rs:404-408,435-442`, `azure/client.rs:759-760`; run for the defaults |
| F14 | `delete(store, paths)` takes paths alone, `put` takes no free-form header, and no call takes a lease. So stage 2's `If-Match` delete, lease and `x-ms-lease-id` writes are outside `obstore`. | stub `_delete.pyi`, `_put.pyi`; `dir(obstore)` |
| F15 | No API returns an identity. `credential_provider` is a callback we supply, returning a token and its expiry. `obstore.auth.azure.AzureCredentialProvider` wraps `azure.identity`, which `obstore` does not install. | stub `_azure.pyi:331`, `auth/azure.py`; run |
| F16 | An Entra access token is a JWT, and `oid` is "the immutable identifier for the requestor, which is the verified identity of the user or service principal". Microsoft says an application should not depend hard on any claim being present. | docs, access token claims |
| F17 | 2,000 GETs of 14 KB on Azurite: 1.35 s in sequence, 0.61 s with 8 threads, 0.51 s with 32, 0.43 s with 64. The sync `get` runs concurrently in threads. Azurite was CPU-bound here, so this is a floor on the gain. | run |
| F18 | A `HEAD` with `Authorization: Bearer <token>` returns `Content-MD5`, `Content-Length`, `ETag` and `Date`, on a blob written by `obstore` with a `credential_provider` or by raw PUT. The MD5 equals the content's. Azure documents `Get Blob Properties` under Entra ID with `blobs/read`, Storage Blob Data Reader at least, and says `Put Blob` sets the MD5 with no header sent. | run on Azurite 3.37.0, `--oauth basic`; docs Get Blob Properties |
| F19 | Azurite's `--oauth basic` checks audience, issuer and expiry: a wrong one of each, or a malformed token, gets 403. It does not check the signature, and it needs TLS: over plain HTTP it starts, then answers a valid token with 403. Microsoft's Azurite page does not cover OAuth. This design used that container for one run and keeps it out of the tier (W9). | run; `azurite-blob --help` |
| F20 | `obstore.sign` on a bearer credential asks for a user delegation key, which that Azurite did not grant, so the emulator cannot exercise a token-based SAS. | run |
| F21 | The same bearer token through `httpx` took and released a lease, and a delete with a wrong `If-Match` got 412 and with the right one 202. | run |

**Not confirmed.** Nothing here ran against real Azure or AWS. Part 2's real-account checklist
answers each Azure item, and the Risks table has a row for each.

- That an anonymous `HEAD` on a real Azure endpoint returns `Date` under a private endpoint or a
  firewall rule.
- That a token from a managed or workload identity, for the storage audience, carries `oid`. The
  docs say service principals, and do not name managed identities on that page.
- That a `HEAD` with a real Entra token returns `Content-MD5` on real Azure. The docs say so, and
  Azurite does not validate a real token.
- Whether a real Azure duplicate create answers 409 or 412. The crate maps both to
  `AlreadyExistsError`.
- That `AzureStore.from_url("az://c/p", credential_provider=...)` uses the provider when account-key
  variables are also set. The CLI refuses those variables (D7).
- The request `timeout` on a large single-shot Put. The stub documents the 30 s default for streams.
- Any AWS behaviour, which matters only when an S3 deployment exists.

### Recorded decisions

The owner decided these on 2026-11-04. W2 and W8 move with `correct` to GH-130.

| ID | Decision | Consequence here |
|---|---|---|
| W1 | The full data corpus moves to Phase 6. | Part 4 measures Inventory documents only. |
| W2 | `correct` tombstones each entry recorded after `from_seq` that it drops. | Moved out. |
| W3 | Upload is Azure-only in Phase 4 and refuses an `S3Store`. | `obstore` cannot set or read CRC32C (F5). The Inventory writer still accepts `S3Store`. |
| W4 | The skew bound is 30 s, and spec §6.4 states it. | `MAX_CLOCK_SKEW` (D4). |
| W5 | Whether §10.1 allows a short-lived user delegation SAS is deferred to Phase 6, when DuckDB's remote reads need an answer. | Phase 4 takes whatever `StoreSecret` the caller gives. sdmxlib's `StoreSecret` docstring warns that `credential_chain` fails on Azure with a managed identity (duckdb/duckdb-azure#171, open on 2026-10-19). |
| W6 | The `stores` tier fails when no container runtime exists, since the pre-push hook is statcatlib's only gate. | A push needs Docker. sdmxlib's tier skips instead. |
| W7 | `correct` and `reinstate` leave Phase 4 for GH-130, to land before Phase 9's restore drill. | GH-98 question 7 moves with them. See Moved out. |
| W8 | `correct` keeps embargoed entries recorded after `from_seq` through a correction, lists every entry it keeps, and logs a WARNING for any that `from_seq` does not have. | Moved out. |
| W9 | No OAuth Azurite container. The bearer branch is tested with `pytest-httpx`, the account-key path lives in tests, and the real-account checklist sits in Part 2. | One container in the tier (D8). |
| W10 | `ManifestArtefacts(fetch)` is sequential. No `to_read`, `prefetch`, `MANIFEST_READERS`, `read_many` or kept reader. | D5; Phase 5 measures the cost. The 64-thread leaf case stays in `bench/`. |
| W11 | `CatalogAdmin.open(BlobLocation)`, `CatalogAdmin.create` and `ManifestArtefacts(fetch)` leave Phase 4, since none has a caller. | Until Phase 5, a blob `commit` with an incoming entry raises `NotImplementedError` (Land order). |
| W12 | The sdmxlib release is StatCat-Suite/sdmxlib#371 and holds only `references: bool = True`, with a stamp rule. `decode_manifest` moves to Phase 5 with its first caller, and `check_on_open(structures=)` waits for a caller. | S16, S6. |
| W13 | Parts 1 and 4 land on sdmxlib 0.85, and Parts 2 and 3 on the #371 release. | Land order. |
| W14 | S2, S3 and S7 go into sdmxlib's `docs/phase-4-restated` PR. | Handoff row 1. |
| W15 | `view` and upload pass `references=False` when the manifest has no bindings, and statcatlib passes no `structures=` in Phase 4. The fold owns that check. | D6, D7, handoff row 8. |
| W16 | The CLI's credential is `DefaultAzureCredential` in `AzureCredentialProvider` in `TokenIdentity`, with `azure-identity`, `typer` and `rich` in `blob`. | D2, D7. |
| W17 | The CLI's grammar, variables and codes: `az://<container>[/<prefix>]`, `AZURE_STORAGE_ACCOUNT_NAME`, a refusal when key or SAS variables are set, RFC 3339 with a zone, exit codes 0, 1 and 2, and a one-hour timeout. | D7. |
| W18 | `lancedb` stays out of `blob`, and a `[blob]`-only install skips the search index check. | D2. |
| W19 | `record_uploaded` lives in `tests/stores/_stores.py`, runs no cross-product rule, and takes `lifecycle=`. | D8. |
| W20 | `exclusive()` and the internal commit split wait for GH-125. | D1. |
| W21 | SeaweedFS stays out of the gate, so `S3Store` is accepted and untested there. | D8. |
| W22 | `actor=` moves from `BlobLocation` to `BlobInventoryStorage`. | D3. |
| W23 | The `pending/` document's bytes are D7's, as a proposal for the spec. | S14. |

### Design decisions

| ID | Decision | GH-98 question |
|---|---|---|
| D1 | `InventoryWriter` takes an injected `InventoryStorage`, and rules stay in the writer. | 1 |
| D2 | `obstore` and `azure-identity` are an extra, `statcatlib[blob]`, imported lazily. | 2 |
| D3 | The actor is the token's `oid`, else a value given to the blob storage, else a refusal. | 3 |
| D4 | The blob clock is an anonymous `HEAD`'s `Date`, under a 30 s skew bound. | 4 |
| D5 | The rules' manifest reader on blob is a sequential `ManifestArtefacts(fetch)`, built in Phase 5 with its first caller. | 5 |
| D6 | `Catalog.open(BlobLocation(...))`, with an explicit `cache_dir`, pulling both local tiers. | 6 |
| D7 | `statcat upload`: a bearer digest read, `DefaultAzureCredential` in the CLI, and a defined command line. | Beyond GH-98's list |
| D8 | The test rig: one Azurite container, an `_ObjectOps` seam, and `record_uploaded` in tests. | Beyond GH-98's list |

### D1: the storage seam

```python
class InventoryStorage(Protocol):
    """Where a catalog's Inventory documents live: the writer's only route to them."""

    @property
    def location(self) -> str: ...                               # the path or URI, for messages
    def read(self, path: str) -> bytes | None: ...               # the reader's Fetch
    def create(self, path: str, data: bytes) -> None: ...        # FileExistsError if taken; complete before visible
    def write_leaf(self, path: str, data: bytes) -> None: ...
    def advance_current(self, root: Root, data: bytes) -> None: ...  # data: the root's stored bytes
    def now(self) -> datetime: ...
    def actor(self) -> str: ...

InventoryWriter(root: str | Path | InventoryStorage, *, artefacts: Artefacts | None = None)
```

The protocol and `LocalInventoryStorage(root)` stay in `store/inventory.py`, holding the
existing code: `local_fetch`, `create_exclusive`, `_stage`, `_fsync_dir`, the leaf skip, the
staged replace of `CURRENT`, `datetime.now(UTC)` and `_actor()`. Keeping them in that module
keeps the tests that patch `inventory_module.create_exclusive`, `getpass` and `socket` valid
(`tests/test_inventory_writer.py:118,303-305`, `tests/test_seal_crash.py:88`). The writer
keeps `_write_leaf` and `_advance_current` as thin delegates, since
`tests/test_seal_crash.py:86,91` and `tests/test_inventory_writer.py:165` use them. The Part
1 test run confirms this, and any test it still breaks becomes a Modify entry in that PR.

`BlobInventoryStorage(location, *, actor=None, clock=None)` in `store/blob.py`:

| Operation | What it does | Writes for a one-product change |
|---|---|---|
| `write_leaf` | `put(path, data, use_multipart=False)` for each leaf the version changes, with no existence check. | 1 |
| `create` | `put(path, data, mode="create")`, which F11 guarantees is one Put at any size, a republish's 8 MB root included. | 1 |
| `advance_current` | `get`, then `put(mode={"e_tag": ...})`, or `mode="create"` when absent. A malformed `CURRENT` counts as absent for the decision and is replaced by its `e_tag`. `PreconditionError` or `AlreadyExistsError` re-reads and repeats up to `MAX_RACES`. | 1 |

Both backends decide with one helper, `current_is_older(held: Current | None, root) ->
bool`, which is the rule at `inventory.py:388`: no held value, another `restore_epoch`, or a
lower `seq` means advance. A carried-forward leaf gets no Put, because its base root is the
head, which expiry will keep once expiry exists (hub design, lease section). `leaves` passed
to `try_commit` is already the changed set.

Two blob behaviours are new to the writer:

- **A create's unknown outcome (F13).** On `AlreadyExistsError` or a transport error,
  `create` reads the name. Bytes equal to its own mean this writer won, and the call
  returns. Other bytes raise `FileExistsError`, which `try_commit` turns into
  `InventoryConflictError` as today. An absent name after a transport error is retried once.
  If that fails too, `InventoryCommitUnknownError` names the version: a timed-out create can
  still land later, so the outcome stays unknown, and the caller re-reads the head.
- **`CURRENT` after a committed root.** The root is the commit point, so a failed
  `advance_current` on blob logs a WARNING and `try_commit` returns the root. Readers probe
  forward (§6.6).

`InventoryWriter.full()` keeps building a new `FullInventory` on each call on blob as on
local disk (W10). `commit` with an incoming entry on a blob writer with no `artefacts=`
raises `NotImplementedError` naming Phase 5 (D5). `InventoryWriter.root` stays a `Path` for
a local catalog and raises `TypeError` on a blob one; `WorkingCopy` and `CatalogAdmin` read
it, and neither runs on blob. Messages use `storage.location`. `check_writable` reads
`catalog.json` through `storage.read` and keeps `decoder="stage-1-writer"`, so a 2.x
document is refused on blob with the same named error, on every commit.

| Alternative | Gain | Cost |
|---|---|---|
| **Injected protocol (chosen).** | One set of rules, tested once. A fake storage drives faults. Stage 2 changes one method per backend. | An indirection for one local and one blob backend. |
| Abstract base class with template methods. | No extra object. | Rules and storage share a class, and `try_commit`'s control flow spreads through overrides. |
| `obstore.LocalStore` for both. | One code path. | Update mode is missing (F2), so `CURRENT` fails. The writer's fsyncs go. Local catalogs gain a Rust dependency. GH-98 requires keeping the link publish. |
| A separate `BlobInventoryWriter` with shared functions. | The local file is untouched. | Two `commit` loops to keep equal, and the retry rules are where bugs would diverge. |

**Stage 2 placement (GH-125, H21, H23).** Phase 4 builds only the rows marked Part 2.

| Stage 2 duty | Where it goes | Built |
|---|---|---|
| Lock from first leaf write to `CURRENT` | A new `exclusive()` on the protocol: `flock` locally, the `HISTORY` lease on Azure. `try_commit` splits out an internal commit, which a restore calls directly. | GH-125 |
| Stale-base refusal after the lock | That internal commit, on a fresh `storage.read(HISTORY)`. | GH-125 |
| Decoder for 2.x | One attribute on the storage picks the decoder. | GH-125 |
| Unconditional leaf Put | `write_leaf`. | Part 1 |
| Digest read | `AzureBlobRest`. | Part 2 |
| `If-Match` leaf delete, lease, `x-ms-lease-id` writes | New `AzureBlobRest` methods, since `obstore` has none of them (F14). The same bearer token ran a lease and a conditional delete on Azurite (F21). | GH-125 |
| Opt-in refused on a store other than Azure | `isinstance(store, AzureStore)` in `CatalogAdmin`. | GH-125 |

Adding `exclusive()` and the split costs the same in GH-125 as now, and building them here
would give a no-op lock with no caller.

### D2: the `blob` extra

**Recommendation:**

```toml
[project.optional-dependencies]
blob = ["obstore>=0.12,<0.13", "azure-identity>=1", "typer>=0.15", "rich>=15.0"]
```

`obstore` and `azure.identity` are imported inside functions, as `lance.py` imports
`lancedb`, with a `dev` group entry so the type checker and the tier have them. A missing
import raises `ImportError` naming `uv add 'statcatlib[blob]'`, then the `pip` form.
`azure-identity` is here because `statcat upload` needs a credential (D7). `store/blob.py`
imports `obstore` types under `TYPE_CHECKING` only, and decodes JSON into `object` with
`isinstance` checks, so `tools/count_any.py`'s count stays.

| Option | Gain | Cost |
|---|---|---|
| **Extra (chosen).** | A laptop user installs nothing new. Same pattern as `search`. | A test must prove core runs without the extra. |
| Base dependency. | No import guard, simpler typing. | Every laptop install gains a 5 MB native wheel and Azure's identity library, which it never calls. |
| `azure-identity` in a second extra, `azure`. | `blob` stays store-neutral. | W3 makes upload Azure-only, so every `statcat upload` needs both, and a missing one fails at run time. |
| `azure-storage-blob` in place of `obstore`. | Lease, `If-Match` delete and digest headers in one client, so `AzureBlobRest` goes away. | GH-98 names `obstore`. It serves Azure only, and its create semantics would need the checks F1 to F11 give here. Worth reconsidering if `AzureBlobRest` grows in stage 2. |

**`lancedb` stays out of `blob`.** `check_catalog` opens the search index (`bundle.py:237`),
and on a `[blob]`-only install `lance.py:1080` raises `ModuleNotFoundError`, which
`check_edition` does not catch. This existing defect gets two fixes. `_check_search` catches
`ImportError` at the constructor as it does at the import, which yields its finding.
`store/blob.py` then passes `check_catalog` only when `lancedb` is importable, and otherwise
a `bundle.check_catalog_no_index` that runs the section's other checks. `_Store._init_lance`
already gives no index without `lancedb` (`store.py:2925-2932`), so a view works and
`search` raises when called. The search files' digests are still checked at pull.

### D3: `event.actor`

**Recommendation.** `TokenIdentity(provider)` wraps the caller's credential provider and is
the callable passed to `AzureStore(credential_provider=...)`. `provider` is a synchronous
callable returning `{"token", "expires_at"}`; any other return raises `TypeError` naming the
supported shape, so an async provider, a key or a SAS is refused. It calls the provider,
decodes the JWT payload without verifying it, keeps `oid`, and returns the credential
unchanged. `TokenIdentity.object_id()` fetches a token first if none has been fetched.
`TokenIdentity.token()` gives `AzureBlobRest` its token. The token came from Entra over TLS
to this process, and the value only labels an audit record; no access decision reads it.

The actor resolves once, when the writer first needs it:

1. the `oid` of a `TokenIdentity` on the store's credential provider;
2. else `actor=` given to `BlobInventoryStorage`;
3. else `InventoryWriteRefusedError`, naming both ways to supply one, before anything is written.

No `InventoryWriter` method takes an actor. Upload writes no version and reads and
`Catalog.open` never resolve one, so `BlobLocation` holds no actor.

| Option for an identity with no object id | Gain | Cost |
|---|---|---|
| **Refuse unless an actor is given (chosen).** | Every blob `event.actor` was stated on purpose. Azurite and S3 keys work with a given name. | One more argument for a key-based store. |
| Fall back to `<os user>@<hostname>`. | Nothing to configure. | It is unauthenticated, and personal data that stays in segments for good (§10.1). |
| Refuse outright. | Only verified identities reach the record. | Azurite and every S3 deployment cannot write, so the conformance tier cannot run. |

| Option for reading the object id | Gain | Cost |
|---|---|---|
| **The `oid` claim (chosen).** | No request and no added permission. | F16: Microsoft warns against depending on a claim. Absence falls through to step 2. |
| Microsoft Graph lookup. | An official answer. | A directory permission for a storage job, and a new endpoint. |
| The configured client id. | Already at hand. | A client id names an application, where §6.2 asks for the object id. |

A given actor is recorded verbatim. Telling verified from given values apart in the audit
record would need a format change, which this design does not propose.

### D4: the storage clock

**Recommendation.** `BlobInventoryStorage.now()` sends an anonymous `HEAD` to the store's
endpoint with `httpx` and parses `Date` from whatever status comes back. It takes the
endpoint from `store.config` for `AzureStore` and `S3Store`. Given `clock=`, it uses that
and sends nothing, as unit tests and the conformance backend do. One request per
`try_commit`.

```python
storage = parse_http_date(response.headers["date"])
if datetime.now(UTC) - storage > MAX_CLOCK_SKEW:   # 30 s, W4
    raise InventoryWriteRefusedError(...)          # names both times
return storage
```

- A clock behind the storage clock passes, since the writer records storage time.
- A missing or malformed `Date` refuses, as does a failed request.
- `next_created_at` then applies §6.6 items 2 and 3 to the storage time, so a root is at
  least 1 ms past its base, and a base over 5 minutes ahead of storage is refused. `Date`
  has one-second resolution, and the 1 ms step covers equal seconds.
- `Date` comes from whatever answers at the endpoint, so a gateway ahead of real time passes
  this one-sided check. No evidence of one exists in the deployment, and the risk table
  records it.

| Option | Gain | Cost |
|---|---|---|
| **Anonymous `HEAD` (chosen).** | No credential, no permission, every store (F4), and available in Part 1. | An extra request. Unconfirmed behind a private endpoint, which the real-account checklist tests. |
| Bearer `HEAD` on `catalog.json` through `AzureBlobRest` (F18). | Authenticated, and it reaches the store itself. | Azure-only, a token needed, and the client would have to move to Part 1. The fallback if the checklist shows an anonymous `HEAD` blocked, one method's change. |
| `head().last_modified` after a Put. | Uses `obstore` alone. | Not the `Date` header the spec names, and needs a write first: an `embargo-change` version has no leaf to write. |
| A probe object written and read back. | Storage's own clock, one source. | A write and a delete for every commit, and `inventory/` gains litter. |
| Patch `obstore` to expose headers. | The clean API. | Depends on an upstream release. Worth an upstream issue. |

### D5: the cross-product rules on blob storage

**Recommendation.** When Phase 5 needs them, the rules read manifests through a sequential
`ManifestArtefacts(fetch)`, which reads `products/<p>/<e>/manifest.json` through a `Fetch`,
checks the SHA-256 as today, and decodes the bytes with S6's `decode_manifest` (its `path`
argument is for messages). Phase 4 builds none of it, because nothing in Phase 4 commits an
incoming entry through the rules on blob (Land order). `ManifestArtefacts(root)` is
unchanged, so `bench/corpus.py:170` keeps working.

**The cost, as an input for Phase 5 to measure.** `refusals` calls `_planned`, which calls
`full.leaves()` (`store/cross_product.py:214`), and the reader has no batch path
(`inventory/_reader.py:302-304`). A cold commit with an incoming data entry at 7,000
products is therefore about 7,000 leaf GETs plus about 6,300 manifest GETs, about 133 s at
10 ms a GET. Revision 2's prefetch would have reached about 71 s. GH-125 part 4, already in
Phase 5, removes the manifest GETs for filled entries. Phase 5 measures this with its
release job and then decides on any concurrency.

| Option | Gain | Cost |
|---|---|---|
| **Sequential reader, built with its first caller (chosen).** | No code without a caller. Phase 5 designs from a measurement. | A trap until then: a blob `commit` with an incoming entry raises, naming Phase 5, unless a test passes `artefacts=`. |
| Build `ManifestArtefacts(fetch)` in Phase 4. | The trap goes. | Code with no caller and no acceptance item, and Part 1 would wait for an sdmxlib release. |
| Concurrent prefetch of leaves and manifests (revision 2). | About 2 times faster by estimate. | A second statement of `_Rules`' conditions, a thread pool, and no caller at this scale (W10). |
| Bring GH-125 part 4 forward. | Zero manifest GETs for filled entries. | The owner placed it in Phase 5, and earlier entries still fall back to manifests. |
| A disk cache of records keyed by edition and SHA. | Survives restarts. | Writes embargoed editions' URNs to a disk outside the boundary (§10.1). |
| Skip the rules on blob. | Fast. | A release could break §6.5 items 4, 7 and 8. |

### D6: remote `Catalog.open`

**Recommendation:**

```python
@frozen
class BlobLocation:
    """A catalog in object storage, and how to reach it."""

    uri: str                                  # "az://catalogs/stc/", the DuckDB form
    store: ObjectStore                        # prefixed at the catalog root
    secrets: Sequence[StoreSecret] = ()       # given to the EditionEngine
    cache_dir: Path | None = None             # required by Catalog.open
    ops: _ObjectOps = ...                     # defaults to _ObjectOps(store); tests replace it (D8)

Catalog.open(path: str | Path | BlobLocation) -> Catalog
```

The caller builds the store, so `obstore`'s own credential options stay available.
`Catalog.open("az://...")` with a plain string raises `ValueError` naming `BlobLocation`, so
a library call never takes a credential from the environment; the `statcat upload` command
is the deployment's edge and does (D7). `Catalog.create` and `CatalogAdmin.open` take no
`BlobLocation`, and `Catalog.create` raises `TypeError` for one. A `BlobLocation` with no
`cache_dir` raises `ValueError` from `Catalog.open`. `catalog.json` on blob is created by
`InventoryWriter(storage).write_catalog_json()` (`inventory.py:395`), which the fixtures and
the bench call, and which Phase 5 can wrap.

`Catalog` holds a `_Opening(root=<cache_dir>/<catalog_id>, writer=None, location=location)`.
`refresh`, `description` and `catalog_id` read through a `BlobInventoryStorage`; `repr` and
messages show `location.uri`.

Two credentials are needed, one for `obstore` and one as DuckDB's `StoreSecret`, because
sdmxlib reads data through DuckDB. Sharing them would need both to accept one form of
credential, which they do not.

**Cache layout:**

```text
<cache_dir>/<catalog_id>/products/<product_id>/<edition_id>/   manifest, metadata.duckdb, tables.ducklake, search/
<cache_dir>/<catalog_id>/verified/<product_id>/<edition_id>    the receive stamp (§4.7)
```

**`Product.view` on a blob catalog:**

1. `LiveInventory.head(product)`, one leaf.
2. If the cache holds the edition, compare its manifest's SHA-256 to the entry's, and a
   mismatch raises `IncompatibleCatalogError` naming the directory to delete. Otherwise GET
   `manifest.json` first and compare its SHA-256, raising `IncompatibleCatalogError` before
   any other file of the edition is read.
3. `pull_edition`: list the `required` and `lazy` files with `edition_files`, refuse an
   absolute or `..` path, GET each into a staged sibling directory, check its sha256 against
   the manifest, and rename the directory into place. A loser of the rename race uses the
   winner's copy. Staged directories older than a day are swept first.
4. `check_on_open(dir, stamps=<cache>/verified, remote=, engine=, extensions=,
   references=<False when the manifest has no bindings>)`, with D2's choice of
   `check_catalog`. The first open runs `receive`, later ones `open`. A finding raises
   `IncompatibleCatalogError`.
5. `LocalRegistry.edition(dir, remote=, engine=, manifest_sha256=)`, then `_Store._over` as today.

`Product.editions` reads the manifest alone, by a GET and a SHA-256 check, with no pull.

**A structure product's references (review B1).** A data edition's references resolve inside
it, since it vendors its closure. A structure edition's may resolve in another structure
product (§6.5 item 9). `view` skips that check, for these reasons:

| Option for an edition with no bindings | Gain | Cost |
|---|---|---|
| **`references=False` at `view` (chosen).** | The bytes hash to a manifest the Inventory recorded, and the fold's `check_edition(at="receive", remote=, structures=)` owns the references. No other edition is pulled. | The reference check does not re-run on a reader. |
| A `check_on_open(structures=)`, which does not exist, passing the other live structure editions. | The check runs again on each machine. | Finding them reads every leaf, 7,000 GETs cold, and pulls each one. `LiveInventory` hides embargoed products, and §6.5 item 9 allows a reference into one, so a valid edition would be refused. |

So upload and `view` pass `references=False` when the manifest has no bindings (W15). By
#371's stamp rule, a deferring call accepts either stamp, and a later full check meeting a
deferred stamp reruns `receive`.

`Lazy` files are pulled because `check_edition` reports a missing lazy file (S4), so a
required-only copy fails every profile. `search/` was 0.3 MB of a 16.7 MB product
(`bench/corpus-results.md`), and more with embeddings. `pull_bytes` gives only the required
size.

| Option for the entry point | Gain | Cost |
|---|---|---|
| **`BlobLocation` value on `open` (chosen).** | One type for `Catalog.open` and upload. `Catalog` keeps two constructors. | A new public class. |
| `Catalog.open("az://...", store=, secrets=, cache_dir=)`. | Reads naturally. | Keyword arguments that mean nothing for a local path. |
| `Catalog.open_remote(...)`. | Plain separation. | A third constructor, with the same body after the first lines. |

| Option for `cache_dir` | Gain | Cost |
|---|---|---|
| **Required (chosen).** | Disk use is visible, and a pod points it at its SSD. | One more argument. |
| A default under the user's cache directory. | Less to type. | Hidden gigabytes, and a home directory on a pod. |
| A temporary directory per process. | Nothing to clean. | Every process start pulls again, and stamps are lost, so `receive` runs every time. |

**Caching, local against blob:**

| Concern | Local catalog | Blob catalog |
|---|---|---|
| Inventory read | `local_fetch`, leaves cached in memory by hash | The same reader over `read`. `product(x).view()` reads `CURRENT`, one Inventory root, one probe and one leaf. `products()` reads every leaf, 7,000 GETs cold, in memory only. |
| `refresh()` | A few file reads | 3 GETs, and only changed leaves after them |
| Edition files | Read in place | Pulled once to `cache_dir`, keyed by immutable edition id |
| Observations | Local `data/` | Byte-range reads through DuckDB, never cached by statcatlib |
| Verification | Manifest SHA-256 at each `view()` | The same, plus each file's sha256 at pull, `receive` once per stamp, and `check_catalog` at each `open` (S15) |
| `catalog_id` | Read on each access | Read once at open, since it never changes |
| A view after a refresh | Stays on its edition | The same; its cached files stay |

`Catalog.open` writes nothing to the catalog, and on blob it does write to `cache_dir`,
which `src/statcatlib/EGM.md:339` must now say.

**`_refuse_legacy_layout` on a remote catalog.** It does not run there. A blob catalog reads
`catalog.json`, and a missing one raises `FileNotFoundError` with the same message. The
function exists to protect a local directory from being written into and to name a
re-ingest. statcatlib 0.11 and sdmxlib 0.72 wrote no catalog to object storage, and a remote
bundle has no `catalog.json`, so it is refused either way. Probing `meta.duckdb` and
`bundle.manifest.json` would add two requests to name a case nothing produces.

**The embargo boundary (§10.1).** The boundary is the account or container, and statcatlib
cannot enforce it from inside a process:

- A principal that can run `Catalog.open` holds a role that can read embargoed bytes.
  `Catalog.open` keeps a public API from returning them, as on local disk. Docs state that
  an analyst's identity belongs on a live-only export, never on the catalog's container.
- `LiveInventory` holds only live entries. Leaf bytes, which name embargoed editions, stay
  in memory and never reach `cache_dir`. Only live editions' files do.
- `upload` reads nothing under `catalog.json`, `inventory/` or `CURRENT`, since the publish
  job has no access there (§10.1). A test uses an `_ObjectOps` that refuses those paths,
  reads included.
- A cached edition stays after a rollback or a withdrawal until an operator deletes it.
  Expunge (§11.3) purges pod disks and is unbuilt, so the risk table records this.

### D7: `statcat upload`

```python
upload_edition(edition_dir: Path, location: BlobLocation, *, release_at: datetime | None = None) -> UploadReport
```

`UploadReport` holds `edition_id`, `product_id`, the files `sent` and `kept`, and
`registered: bool`. `upload_edition` refuses any store but `AzureStore` with `TypeError`
naming W3. A private `_upload(..., rest=)` takes the digest client, so tests can run over
`MemoryStore` and Azurite.

Steps, none reading `catalog.json`, `inventory/` or `CURRENT`:

1. `read_manifest(edition_dir)`, refusing an unsealed directory with `ManifestError`. A file
   over 5,000 MiB (F12) raises `UploadCheckError` naming it, before anything is sent.
2. For each `root: data` entry, `head`, and when absent hash the local file against the
   manifest, then `put(path, file, mode="create")`. A create is always one Put (F11), so
   Azure keeps its MD5 (F7). The hash keeps a rotted local file out of the shared `data/`.
   Present files stay, since data files are immutable and shared across editions (§4.1).
   `AlreadyExistsError` counts as present (F13).
3. The same for the edition's own files, except `manifest.json`.
4. `manifest.json`, `mode="create"`. Present with other bytes raises `UploadCheckError`,
   because an edition is never rewritten.
5. Verify:
   - `check_edition(edition_dir, at="receive", lookup=lookup, extensions={"statcat": <D2's
     check>}, references=<False when the manifest has no bindings>)` covers the lake, the
     bindings, the section and `data/` in the store;
   - `verify_remote(<the manifest limited to root: edition>, lookup, tiers=("required",
     "lazy", "remote"))` covers the edition's uploaded files;
   - a GET of the uploaded `manifest.json`, compared by SHA-256.
   The lookup caches, so no file is asked twice.
6. `put("pending/<product>/<edition>.json", document, mode="create")`. On
   `AlreadyExistsError`, read the entry. Equal bytes set `registered=False`, and F13 means
   that flag is advisory: this run may have written it. A different `release_at` raises
   `UploadCheckError` naming both, since the fold would otherwise use the old time.

Any finding raises `UploadCheckError` with its findings, before step 6, so a bad upload
registers nothing. The fold owns the reference check for an edition with no bindings
(handoff row 8).

**The `pending/` document.** UTF-8 compact JSON with two keys in this order:
`{"edition_id":"<uuid7>","release_at":"2026-11-05T13:30:00.000Z"}`, or `"release_at":null`
for no scheduled release. Times are UTC, `Z`, milliseconds, as a root's `created_at` is.
Equal input gives equal bytes (S14).

**File size and memory.** A single-shot Put reads the file into memory (F11), so peak memory
is the largest file, up to 5,000 MiB. Two alternatives wait for a real need: a streaming
signed PUT through `httpx` (F9), and Put Block plus Put Block List through signed URLs with
`x-ms-blob-content-md5`, for files past the limit.

**The digest read.** `AzureBlobRest(location)` in `store/blob.py` holds an `httpx` client
and, in Phase 4, one method: `head(path) -> RemoteFile | None`, sending `Get Blob
Properties` with `x-ms-version: 2023-11-03` and `Authorization: Bearer` from the store's
`TokenIdentity`. It needs `blobs/read` and nothing else. With no `TokenIdentity`,
`upload_edition` raises `ValueError` before it writes anything. The account-key path that
Azurite needs is a test subclass that signs a GET URL with `obstore.sign` and sends `HEAD`
to it (F8), since no deployment uses a key (§10.1).

| Option | Gain | Cost |
|---|---|---|
| **Bearer token through `httpx` (chosen).** | No SAS, and no `generateUserDelegationKey` at account scope. §10.1 stands as written. The client is the one stage 2 needs (F21). `httpx` is a base dependency. | Azurite's plain container cannot run it, so unit tests use `pytest-httpx` and the real-account checklist proves it. A provider must be a `TokenIdentity`. |
| `obstore.sign` for every credential. | One branch, and it runs on Azurite with a key. | A SAS on every lookup and a role at account scope (docs), which W5 defers. Azurite cannot exercise the token path (F20). |
| OAuth Azurite over TLS in the tier. | The bearer branch runs against a server. | A second container, certificates and a token minter, to prove an emulator accepts a token it does not validate (W9, F19). |
| `azure-storage-blob` in the extra. | Handles credentials, digests, leases and conditional deletes. | A second client beside `obstore`, against GH-98's choice. |
| Stream each file back and hash it. | No digest from the store at all. | A download of every file on every upload, and §11.2 asks for a digest the store keeps. |

**The command line.**

```text
statcat upload EDITION_DIR AZ_URL [--release-at TIME]
```

| Item | Choice | Reason |
|---|---|---|
| `AZ_URL` | `az://<container>[/<prefix>]`, the catalog root. Anything else exits 2; `s3://` names W3. | The form `BlobLocation.uri` and DuckDB take, so one string names a catalog everywhere. |
| Store | `AzureStore.from_url(AZ_URL, credential_provider=TokenIdentity(AzureCredentialProvider(DefaultAzureCredential())), client_options={"timeout": "1h"})` | The caller of a console command has nothing to pass. One hour covers a 5,000 MiB Put at about 1.4 MB/s; the real-account checklist tests a large file. |
| Account | `AZURE_STORAGE_ACCOUNT_NAME`, required. Missing exits 2. | `obstore`'s documented variable (`_store/_azure.pyi`), so no new name. |
| Identity | `azure.identity`'s own variables, such as `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_FEDERATED_TOKEN_FILE` for a workload identity. | A publish job's pod already has them. |
| Keys | `AZURE_STORAGE_ACCOUNT_KEY`, `AZURE_STORAGE_ACCESS_KEY`, `AZURE_STORAGE_MASTER_KEY` or a SAS variable set exits 2. | §10.1 says no keys, and which credential wins is unconfirmed. |
| `--release-at` | RFC 3339 with `Z` or an offset, parsed with `datetime.fromisoformat`. A naive time exits 2. Omitted writes `null`. | §6.3's instant, with no silent time zone. |
| Output | Sent and kept counts, the identity's `oid`, and whether it registered. | A chained credential can pick an unintended identity, and this shows which one acted. |
| Exit codes | 0: uploaded, or already registered with the same bytes. 1: `UploadCheckError`, each finding printed as `FAIL <file>: <detail>`, or a store or credential fault. 2: usage. | `verify-catalog`'s `FAIL` form, Typer's 2. |

| Option for the credential | Gain | Cost |
|---|---|---|
| **`DefaultAzureCredential` (chosen).** | Works on a pod and on an operator's laptop with no flag. | The chain can pick an unintended identity, which the output line reveals. |
| `WorkloadIdentityCredential` only. | Exactly one identity. | A laptop cannot run the command, so a first upload needs a pod. |
| A dotted-path provider option. | Any credential. | An import from the command line, with no production need. |

D6 refuses a string URI because a library call should not reach into the environment. The
console command is the opposite case: it has no caller, and its environment is its input. So
the rule is stated once: library calls take a built store, and only `statcat upload` reads
the environment.

### D8: the test rig

The `stores` tier follows sdmxlib's `tests/stores/`, with two differences: one container,
Azurite at sdmxlib's digest, and `pytest.fail` naming the fix when no container runtime
exists (W6). SeaweedFS stays out of the gate, since an S3 catalog can hold no real edition
in Phase 4, and the leaf rule gives one code path. The earlier run covers F1 and F2 on it.
The OAuth container is out (W9).

**`_ObjectOps`** in `store/blob.py` is the one route to `obstore`, and every blob path uses
it: `BlobInventoryStorage`, `_upload`, `pull_edition`. Its methods are `read(path) ->
tuple[bytes, str | None] | None`, `put(path, data, *, mode=None, use_multipart=False) -> str
| None`, `head(path) -> ObjectMeta | None` and `delete(path)`. It wraps store faults (see
Errors). A test subclass records every call or refuses a path, and reaches all three through
`BlobLocation.ops`.

**A recording HTTP proxy** in `tests/stores/_stores.py` shows what `_ObjectOps` cannot:
`If-None-Match`, `x-ms-blob-type`, and request counts for F10 and F11. Rows that use it say
so.

**`tests/stores/_backend.py`** holds `BlobBackend`, the conformance adapter. It is a copy of
`WriterBackend` (`tests/test_inventory_conformance.py:76-197`) over `read` and `ops`, which
leaves the local adapter untouched. Each factory call uses a fresh `uuid4` prefix.
`restore_to` uses `ops.delete`, the actor is `"conformance"`, and `clock=lambda:
datetime.now(UTC)` keeps a lagging container VM from failing commits. One separate test runs
the real `HEAD`, and reports the difference when it fails. `commit_interrupted` writes the
leaf alone.

**`record_uploaded`** puts a real edition into a blob Inventory, since Phase 4 has no seal
on blob and no fold. It lives in `tests/stores/_stores.py`:

```python
record_uploaded(
    writer: InventoryWriter, location: BlobLocation, edition_dir: Path, *,
    lifecycle: Lifecycle = "revisable", release_at: datetime | None = None,
) -> Edition
```

It uploads with the test `rest`, then calls `writer.try_commit(head, {product: record(...)},
kind="seal", reason="record_uploaded")`, which runs no cross-product rule. `record`'s
arguments come from these sources:

| Argument | Source |
|---|---|
| `kind`, `previous`, `schema_version`, `edition_id` | The manifest (`edition_kind`). |
| `lifecycle` | The caller, since the manifest has none (S13). |
| `embargo` | The head's `embargo`, as a seal's is, so a catalog with embargo on records `embargoed`. |
| `release_at` | The caller. |
| `retired` | The head's tombstoned ids for the product. |
| `manifest_sha256` | The uploaded manifest's bytes. |
| `dataflows`, `pull_bytes`, `artefacts` | `recorded_routing(routing_fields(manifest), kind)`, passed as three keywords. |

| Option for its home | Gain | Cost |
|---|---|---|
| **`tests/stores/_stores.py` (chosen).** | Nothing public to remove when the fold ships. Phase 6 follows the real fold. | Another repository cannot reach it, and none needs to. |
| `statcatlib.testing` (revision 2). | Reusable. | A public helper that bypasses `pending/` and has no caller outside this tier. |
| Public `CatalogAdmin.record_uploaded`. | A manual path until Phase 5. | Commits an API before the fold's design, on `CatalogAdmin`, which Phase 4 does not extend to blob. |

### Errors

| Site | Condition | Raises |
|---|---|---|
| `_ObjectOps`, every call | `obstore` `BaseError` other than the handled cases | `BlobStoreError(RuntimeError)`, naming the path, chained |
| `read`, `head` | Absent object | Returns `None` |
| `create` | Name holds other bytes | `FileExistsError`, then `InventoryConflictError` from `try_commit` |
| `create` | Outcome unknown | `InventoryCommitUnknownError(InventoryError)`, naming the version |
| `now` | Clock ahead, or `Date` missing or unreachable | `InventoryWriteRefusedError` |
| `actor` | No identity | `InventoryWriteRefusedError` |
| `check_writable` | 2.x `catalog.json`, base far ahead | `InventoryWriteRefusedError`, as today |
| `commit` | Incoming entry on blob, no `artefacts=` | `NotImplementedError`, naming Phase 5 |
| `InventoryWriter.root` | Blob storage | `TypeError` |
| `TokenIdentity` | Provider returns another shape | `TypeError` |
| `upload_edition` | Store other than `AzureStore` | `TypeError`, naming W3 |
| `upload_edition` | No `TokenIdentity` | `ValueError` |
| `upload_edition` | Unsealed directory | `ManifestError` |
| `upload_edition` | Findings, oversize file, differing manifest or `release_at` | `UploadCheckError`, with `findings` |
| `Catalog.open` | String URI, or no `cache_dir` | `ValueError` |
| `Catalog.open` | No `catalog.json` | `FileNotFoundError` |
| `Catalog.create` | `BlobLocation` | `TypeError` |
| `Product.view` | Manifest mismatch, check finding, cache mismatch, unsafe path | `IncompatibleCatalogError` |
| Any import | Missing extra | `ImportError`, naming `statcatlib[blob]` |

`InventoryCommitUnknownError`, `UploadCheckError` and `BlobStoreError` are new.
`store/inventory.py` holds the first, `store/blob.py` the third, and `upload.py` the second.

### Symbols

| Symbol | Module | Signature | Part |
|---|---|---|---|
| `InventoryStorage`, `LocalInventoryStorage` | `store/inventory.py` | D1 | 1 |
| `BlobLocation` | `store/blob.py`, exported from `statcatlib` | D6 | 1 |
| `BlobInventoryStorage` | `store/blob.py` | `(location, *, actor=None, clock=None)` | 1 |
| `TokenIdentity` | `store/blob.py`, exported | `(provider)`; `__call__()`, `token() -> str`, `object_id() -> str \| None` | 1 |
| `_ObjectOps` | `store/blob.py` | D8 | 1 |
| `MAX_CLOCK_SKEW` | `store/blob.py` | `timedelta(seconds=30)` | 1 |
| `current_is_older` | `store/inventory.py` | `(held, root) -> bool` | 1 |
| `AzureBlobRest` | `store/blob.py` | `(location)`; `head(path) -> RemoteFile \| None` | 2 |
| `upload_edition`, `UploadReport`, `UploadCheckError` | `upload.py`, exported | D7 | 2 |
| `check_catalog_no_index` | `bundle.py` | sdmxlib's `Check` | 2 |
| `pull_edition` | `store/blob.py` | `(location, product_id, edition_id, manifest_bytes, dest) -> Path`; raises `IncompatibleCatalogError` | 3 |
| `inventory_corpus` | `testing.py` | Part 4 | 4 |

## Moved out: `correct` and `reinstate`

`CatalogAdmin.correct` and `CatalogAdmin.reinstate` leave Phase 4 (W7) for GH-130, to land
before Phase 9's restore drill. That issue takes GH-98 question 7, its four acceptance
items, S9, S11, and from revision 2 (at `e7eba47`) D7, the rest of S8, S10's `correct` rule
in sdmxlib, and the embargo question O4, which W8 now answers. Nothing depends on them until that drill.

The [review](egm/catalog-on-blob-storage/review_report.md) holds the findings the issue must
take: B2, M1, M2's verification rules, M4, M10, M13, M16, M26, M34 to M36, and m1, m6 to m9,
m19 and m24.

**The rule for sdmxlib's `correct`, from the review's B2, with its wording for the kept
entry changed to name W8.** H is the head's state and R the restored state.

| H | R | Result |
|---|---|---|
| embargoed | any | Keep the head's entry |
| withdrawn | embargoed | Stays withdrawn |
| withdrawn | live or superseded | Refuse unless the operator names the edition |
| live or superseded | embargoed | Restore with `release_at=null`, so an explicit release is required |
| embargoed | absent | Keep, and log a WARNING naming the edition and its `release_at` (W8) |
| any other | absent | Tombstone (W2) |
| tombstoned | live | Refuse, as drafted |

**The owner's decision that moves with it (W8).** `correct` keeps embargoed entries recorded
after `from_seq` through a correction, lists every entry it keeps, and logs a WARNING for
any that `from_seq` does not have.

The issue also needs an operator read for `if_seq` and `from_seq`, as `EditionEntry` has no
`seq` (review M10), and a statement of the rollback risk after `reinstate`, including the
default target (review M1). `admin.py:364-366` promises other-schema rollback "in Phase 4",
which no plan scopes; Part 1 removes the phase from that docstring.

## Alternatives Considered

Each decision above carries its own table. Three wider choices:

*   **Run the blob writer against ADLS DFS.** §6.6 forbids DFS create plus append.
    `object_store` reads only the account name from a `dfs.core.windows.net` URL
    (`azure/builder.rs:743`) and writes with Put Blob. Every write in the runs carried
    `x-ms-blob-type`, and Part 1 asserts that header. No real DFS endpoint was tried.
*   **Copy a whole catalog as Part 4's corpus.** GH-98 replaces it, since the read touches
    only `CURRENT`, Inventory roots and leaves. The full data corpus moves to Phase 6 (W1).
*   **Keep a write-through leaf cache on disk to speed cold reads.** Leaves name embargoed
    editions, so it moves embargoed metadata out of the boundary.

## Non-Functional Requirements

Request counts, at 10 ms a request, an assumed figure from GH-98:

| Operation | Reads | Writes | Time |
|---|---|---|---|
| `try_commit`, one product, base known | `catalog.json`, base root, one base leaf, `Date` `HEAD`, `CURRENT` | leaf, root, `CURRENT` | 80 ms |
| `commit`, no incoming entry | Adds `CURRENT`, root, probe | The same | 110 ms |
| `try_commit`, 200 products | About 204 | 202 | 4 s |
| Part 4 corpus, 7,000 products in 14 commits | About 70 | 7,000 leaf Puts, 14 roots, 14 `CURRENT` | 71 s |
| Cold `LiveInventory.editions()` at 7,000 products | About 7,003 | None | 70 s; about 1.1 s at 64 threads, by estimate, in `bench/` only |
| Cold commit through the rules at 7,000 products (Phase 5) | About 13,300 | At least 3 | 133 s |
| `Product.view`, cached | `CURRENT`, root, probe, one leaf | None | 40 ms |

*   **Atomicity.** No reader sees a partial Inventory root, leaf or `CURRENT` (single Put).
*   **Compatibility.** No format change. A local catalog behaves as today.
*   **Security.** No key and no SAS in the production path. A token never reaches a log or a
    `repr`. `cache_dir` holds live editions only, and its permissions are the caller's.
*   **Memory.** Upload's peak is its largest file, up to 5,000 MiB (F11). The rest is
    proportional to a leaf set.
*   **Disk.** `cache_dir` grows by each edition viewed, about 16.7 MB a one-table product,
    and is never evicted in Phase 4.
*   **Gate time.** Part 1's PR measures the tier and records the number. sdmxlib's different
    tier takes about 20 s with three containers.
*   **Coverage.** `just coverage` selects `unit or integration`, so the `stores` tier does
    not count toward the 85% floor. The floor's effect is uncomputed, and `just ci` does not
    run coverage, so each part runs `just coverage` in its PR. Per module:

| Module | Unit tier covers | Only `stores` covers |
|---|---|---|
| `store/blob.py`, `BlobInventoryStorage`, `TokenIdentity`, `_ObjectOps` | Everything, over `MemoryStore` | Real conditional headers |
| `AzureBlobRest` | Everything, with `pytest-httpx` | The test subclass only |
| `upload.py` | Every step, over `MemoryStore` and a fake `rest` | Azure's kept MD5 |
| `pull_edition` | Every step, over `MemoryStore` | The rename race across processes |
| `catalog.py` blob branches | Refusals | The read path through DuckDB |

### Risks and limits

| Risk | Mitigation |
|---|---|
| An anonymous `HEAD` is blocked on real Azure | `now()` refuses and names the cause. The checklist tests it, and a bearer `HEAD` is the one-method fallback (D4). |
| A gateway answers with a fast `Date` | Unaddressed by a one-sided check, with no evidence of one in the deployment. |
| A workload identity's token has no `oid` | The writer refuses unless given an actor. The checklist tests it. |
| A real bearer `HEAD` differs from the docs | This is the production path, and only the checklist proves it. `AzureBlobRest` is small and replaceable. |
| A duplicate create's status differs | The crate maps both to `AlreadyExistsError`. The checklist records it. |
| A large single-shot Put times out, or exhausts memory | The CLI sets a one-hour timeout, and the checklist runs a large file. A streaming PUT is the alternative (D7). |
| `from_url` prefers an environment key over the provider | The CLI refuses those variables. |
| `DefaultAzureCredential` picks an unintended identity | The output names the `oid`. |
| A given actor is recorded as if verified | Recorded verbatim. Only emulator tests and key-based stores use one in Phase 4. |
| An upstream `obstore` minor changes behaviour | Capped below the next minor. The tier reruns F1, F2, F10 and F11's cases. |
| A leaked orphan leaf per crashed commit | Bounded by one version's changed leaves (§6.9). |
| A cached edition outlives a withdrawal | Recorded limit until `expunge` and Phase 6's placement. Docs say to delete the directory. |
| A blob `commit` with an incoming entry raises | Named error, until Phase 5 builds the reader (D5). |
| A push needs Docker (W6) | The failure names the fix. |
| GC deletes a pending edition's files | S12, before Phase 5 builds GC. No GC exists in Phase 4. |

## Detailed Implementation Plan

### Land order

The three pieces review ZO-9 moved have no Phase 4 caller once `correct` leaves, by this
reading of the code:

| Piece | Who would call it | Finding |
|---|---|---|
| `CatalogAdmin.open(BlobLocation)` | `correct` and `reinstate` (moved). GH-98's parts name no other. Conformance and the fixtures use `InventoryWriter`. | Dropped from Phase 4. `admin.py` needs no blob change, so `writer.now()` there, `prune`'s refusal and `repr` go too. |
| `CatalogAdmin.create` | Writing `catalog.json`, which `InventoryWriter.write_catalog_json` already does through the seam. | Dropped. |
| `ManifestArtefacts(fetch)` | `commit` with an incoming entry. The corpus and `record_uploaded` use `try_commit`, which runs no rule. Lost-race tests use a `commit` with no incoming entry or an injected `artefacts=`. | Dropped from Phase 4, specified for Phase 5 (D5). |

The read measurement commits 7,000 leaves with `write_catalog_json` and `try_commit` over
`BlobInventoryStorage`. `try_commit` reads `catalog.json`, the base root and the leaves of
products already in the base, which are none for a new product. It reads no manifest and
needs neither `full()` nor a reader, so Part 4 needs Part 1 alone.

| Part | Content | sdmxlib | Waits on |
|---|---|---|---|
| 1 | The seam, the blob writer, the `stores` tier | 0.85 | Handoff row 1 |
| 2 | Upload, `AzureBlobRest`, the CLI, `record_uploaded`, the real-account checklist | The #371 release | Part 1, handoff rows 1, 3 |
| 3 | Remote `Catalog.open`, `pull_edition` | The #371 release | Part 2, for `record_uploaded` |
| 4 | The Inventory read measurement | 0.85 | Part 1 |

Each part is a branch from `origin/main` and a PR, merged with a merge commit after `just
ci` passes. Part PRs do not edit `CHANGELOG.md`; each updates `docs/upgrading.md`, runs `uv
run python tools/count_any.py --write` when the count changes, runs `just docs-build`,
updates the `EGM.md` beside the code, and updates `CLAUDE.md` where it names a new module or
command. Parts release in this order, and Part 4 may land before Parts 2 and 3.

### Part 1: object storage and the blob Inventory writer

*   **`pyproject.toml` (Modify):** the `blob` extra (D2), `obstore`, `azure-identity` in the
    `dev` group, and the `stores` marker.
*   **`justfile` (Modify):** `test-stores` (`uv run pytest tests/stores -m stores -rs`), and
    `ci: check test test-integration test-stores docs-build`.
*   **`.githooks/pre-push` (Modify):** the comment names the tier.
*   **`src/statcatlib/store/inventory.py` (Modify):** D1's protocol,
    `LocalInventoryStorage`, `current_is_older`, `InventoryCommitUnknownError`; the writer
    takes a storage and keeps its delegates.
*   **`src/statcatlib/store/blob.py` (Create):** `BlobLocation`, `_ObjectOps`,
    `BlobStoreError`, `BlobInventoryStorage`, `TokenIdentity`, `MAX_CLOCK_SKEW`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export
    `BlobLocation`, `TokenIdentity`.
*   **`src/statcatlib/admin.py` (Modify):** the docstring at `:364-366` names no phase.
*   **`tests/stores/{__init__,_stores,_backend,conftest}.py` (Create):** D8.
*   **`src/statcatlib/store/EGM.md`, `src/statcatlib/EGM.md`, `docs/reference/blob.md`
    (Create), `mkdocs.yml` (Modify):** the module and its nav entry.
*   **Docs:** `docs/api/store.md` says what a blob writer does; `docs/upgrading.md` adds the extra.

| Test | File | Tier | Covers |
|---|---|---|---|
| `run` and `run_history` over Azurite | `tests/stores/test_blob_conformance.py` | stores | conformance |
| Kill between leaf and root: `commit_interrupted` writes the leaf alone; the head reads, the next commit lands | `tests/stores/test_blob_conformance.py` | stores | killed writer |
| Lost race: two writers on one base, the loser's root links the winner and diffs against it | `tests/stores/test_blob_writer.py` | stores | lost race |
| 2.x `catalog.json` is refused, and the message names it | `tests/stores/test_blob_writer.py` | stores | 2.x |
| An 8 MB root makes one PUT with `If-None-Match: *` and `x-ms-blob-type`, by the proxy | `tests/stores/test_blob_writer.py` | stores | single-shot root |
| A 5 MiB + 1 leaf makes one PUT, by the proxy; a stale `e_tag` raises on `CURRENT` | `tests/stores/test_blob_writer.py` | stores | F2, F10, F11 |
| The real `HEAD` gives a `Date` within the bound, reporting the difference | `tests/stores/test_blob_writer.py` | stores | D4 |
| A one-product version at 50 products makes one leaf Put; a carried leaf gets none; a present changed leaf is Put again | `tests/test_blob_storage.py` | unit | leaf rule |
| `CURRENT`: forward only, a recorded restore's lower `seq` replaces it, a malformed one is replaced, `AlreadyExistsError` on a first version retries | `tests/test_blob_storage.py` | unit | D1 |
| A failed `advance_current` logs a WARNING and returns the root | `tests/test_blob_storage.py` | unit | D1 |
| A create with `AlreadyExistsError` or a transport fault: equal bytes, other bytes, absent, still failing | `tests/test_blob_storage.py` | unit | F13 |
| Clock: ahead over the bound refuses, behind passes, missing `Date` refuses, with `pytest-httpx` | `tests/test_blob_storage.py` | unit | D4 |
| `TokenIdentity`: a hand-made JWT, no `oid`, no token fetched yet, another provider shape; given actor; none refuses | `tests/test_blob_storage.py` | unit | D3 |
| `commit` with an incoming entry and no `artefacts=` raises `NotImplementedError` | `tests/test_blob_storage.py` | unit | D5 |
| `BlobInventoryStorage` satisfies the protocol; `InventoryWriter.root` raises on blob | `tests/test_blob_storage.py` | unit | D1 |
| Core imports with `obstore` absent; the error names the extra | `tests/test_public_api.py` | unit | D2 |
| A missing runtime fails the tier, naming the fix | `tests/stores/conftest.py`, by inspection in the PR | stores | W6 |

### Part 2: upload

*   **`pyproject.toml` (Modify):** the cap on the #371 release.
*   **`src/statcatlib/upload.py` (Create):** D7.
*   **`src/statcatlib/store/blob.py` (Modify):** `AzureBlobRest`.
*   **`src/statcatlib/bundle.py` (Modify):** `_check_search`'s catch, `check_catalog_no_index` (D2).
*   **`src/statcatlib/cli/__init__.py` (Modify):** `statcat upload`; `src/statcatlib/cli/EGM.md`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export
    `upload_edition`, `UploadReport`, `UploadCheckError`.
*   **`tests/stores/_stores.py` (Modify):** `record_uploaded`, the account-key
    `AzureBlobRest` subclass.
*   **Docs:** `docs/api/blob.md` (Create), the operator guide for upload, with its nav
    entry; `docs/catalog-format.md` gains a `pending/` section; `docs/reference/blob.md`.

| Test | File | Tier | Covers |
|---|---|---|---|
| Every write recorded: none under `inventory/`, none at `CURRENT` or `catalog.json`, and no read there either | `tests/stores/test_upload_store.py` | stores | upload writes |
| A second upload writes no second `pending/` entry, sends no file again, reports `registered=False` | `tests/stores/test_upload_store.py` | stores | second upload |
| A rerun after a crash at each step sends only what is missing | `tests/stores/test_upload_store.py` | stores | CUJ 1 |
| The manifest is the last edition file written; a corrupt blob fails before `pending/` | `tests/stores/test_upload_store.py` | stores | §11.2 order |
| A file lost after step 2 gives a `remote` finding; Azure's kept MD5 equals the manifest's | `tests/stores/test_upload_store.py` | stores | digest |
| A structure product with external references uploads and registers | `tests/stores/test_upload_store.py` | stores | B1 |
| A 64 MiB data file over Azurite is one PUT, by the proxy | `tests/stores/test_upload_store.py` | stores | F11 |
| `record_uploaded`: live, embargoed when the head's embargo is on, a tombstoned edition refused | `tests/stores/test_upload_store.py` | stores | D8 |
| A rerun with another `release_at` raises, naming both; bytes of the `pending/` document | `tests/test_upload.py` | unit | D7 |
| An unsealed directory, a differing manifest, an oversize file, a rotted local file, an `S3Store`, no `TokenIdentity` | `tests/test_upload.py` | unit | D7, W3 |
| `AzureBlobRest.head`: bearer header, `x-ms-version`, MD5 parsing, 404, with `pytest-httpx` | `tests/test_azure_blob_rest.py` | unit | D7 |
| `[blob]`-only: `_check_search` with `lancedb` absent gives no crash, for upload and view | `tests/test_upload.py` | unit | D2 |
| `statcat upload`: success, already registered, findings exit 1, naive time, bad URL, `s3://`, no account, key variable set exit 2, missing extra | `tests/test_cli_upload.py` | unit, `CliRunner` | B4 |

**The real-account checklist**, one manual pass before Phase 5 relies on Parts 1 and 2,
recorded in Part 2's PR:

1. A commit under a workload identity: `event.actor` holds the `oid`.
2. The anonymous `Date` `HEAD`, also behind the deployment's private endpoint.
3. A bearer `HEAD` returns `Content-MD5`, with Storage Blob Data Contributor.
4. A duplicate create's status, and that `obstore` raises `AlreadyExistsError`.
5. `statcat upload` with a file over 1 GiB: time, peak memory, and the one-hour timeout.
6. `from_url` with `az://` and `AZURE_STORAGE_ACCOUNT_NAME` resolves as D7 says.

### Part 3: remote catalogs

*   **`pyproject.toml`:** no change beyond Part 2's cap.
*   **`src/statcatlib/catalog.py` (Modify):** `Catalog.open(BlobLocation)`, `_Opening`,
    `refresh`, `description`, `catalog_id`, `Product.view`, `Product.editions`.
*   **`src/statcatlib/store/blob.py` (Modify):** `pull_edition`.
*   **`src/statcatlib/EGM.md:339`:** what `Catalog.open` writes.
*   **Docs:** `docs/api/blob.md` gains reading and the embargo boundary; `docs/api/store.md`
    links it.

Fixtures seal editions in a local catalog, with `synthetic_catalog` or the CODR generator,
and place them with `record_uploaded`.

| Test | File | Tier | Covers |
|---|---|---|---|
| A wrong `manifest_sha256`: `IncompatibleCatalogError`, and the recorded reads hold only that manifest of the edition | `tests/stores/test_remote_catalog.py` | stores | wrong manifest |
| A catalog with embargoed entries: `products()` and `view()` never return one; no GET under its edition; none in `cache_dir` | `tests/stores/test_remote_catalog.py` | stores | embargo |
| Rows through a blob view equal the local read | `tests/stores/test_remote_catalog.py` | stores | remote read |
| A structure product with external references opens, and #371's deferred stamp lets its second `view()` run `open` | `tests/stores/test_remote_catalog.py` | stores | B1 |
| The second `view()` pulls nothing and runs `open` | `tests/stores/test_remote_catalog.py` | stores | caching |
| `catalog_id`, `refresh` and `description` read through the store; `editions()` pulls nothing | `tests/stores/test_remote_catalog.py` | stores | D6 |
| Missing `catalog.json` raises `FileNotFoundError` | `tests/stores/test_remote_catalog.py` | stores | D6 |
| A two-process rename race leaves one copy | `tests/stores/test_remote_catalog.py` | stores | pull |
| A string URI, no `cache_dir`, and `Catalog.create(BlobLocation)` each raise | `tests/test_blob_open.py` | unit | D6 |
| `pull_edition`: an absolute or `..` path, a digest mismatch, a stale staged directory, a cached SHA mismatch | `tests/test_blob_open.py` | unit | D6 |

### Part 4: the Inventory read measurement

This corpus holds Inventory documents only (W1).

*   **`src/statcatlib/testing.py` (Modify):** `inventory_corpus(writer, products=7000, *,
    embargoed=0.1, artefacts=False, batch=500, seed=0)`. It builds leaves with sdmxlib's
    `record`, one edition each, and writes with `try_commit`, which runs no rule since no
    manifest exists. Values: one `dataflows` URN named from the product id, `pull_bytes`
    13,131,776 (spec §6.3's example), `manifest_sha256` a hash of `(product, edition)`, and
    `corpus_embargoed`'s share embargoed with a future `release_at`. With `artefacts=True`,
    10 records of 64-hex fingerprints, which reach about 2.4 KB a leaf. A commit per 500
    products means 14 Inventory roots and 7,000 leaf Puts in all, each a leaf that version
    changes. A commit per product would write quadratic root bytes for no gain to the read.
*   **`bench/inventory_read.py` (Create):** starts Azurite with
    `tests.stores._stores.start`, builds the corpus twice, without `artefacts` (about 3.6 MB
    of leaves) and with them (about 17 MB, the Phase 5 shape), and counts each read through
    a recording `Fetch`. The 64-thread leaf case fills a dictionary with a local thread
    pool, then serves the reader from it (F17); no `src` code does this.
*   **`bench/inventory-read-results.md` (Create):** the record.
*   **`docs/reference/testing.md`:** the new function.

| Case | Records |
|---|---|
| Cold `LiveInventory.refresh()` then `editions()`, sequential | fetch count, bytes, Azurite wall time |
| Cold, 64-thread leaf prefill | the same |
| `head(one product)` cold | the same |
| `refresh()` after a one-product and a 200-product version | the same |
| `FullInventory` for the same | the same |

Azurite's wall time excludes real storage latency. The report says so and estimates wall
time as `GETs x 10 ms` in sequence and `ceil(GETs / 64) x 10 ms` concurrent, in the form of
`bench/corpus-results.md`. This measures no manifest read, and D5's 133 s stays for Phase 5.

| Test | File | Tier | Covers |
|---|---|---|---|
| A 50-product run: a cold read touches only `CURRENT`, Inventory roots and leaves, with the expected count | `tests/stores/test_inventory_read_counts.py` | stores | GH-98 part 5 |
| `inventory_corpus` over `MemoryStore`: entry states, sizes, determinism by seed | `tests/test_inventory_corpus.py` | unit | Part 4 |

The full run stays outside `just ci`. The specification lives in sdmxlib, so the result
reaches §6.5 through handoff row 6. The baked-summary choice is the owner's, on these
numbers, against the readiness budget of §7.1. This design proposes no threshold, since the
spec gives no number for a pod's Inventory read.

### Acceptance map

GH-98's four items on `correct` and `reinstate` move with W7. Its body still lists them
until it is updated.

| GH-98 acceptance | Part | Test |
|---|---|---|
| `conformance.run` passes over Azurite, with stage-1 history cases | 1 | `tests/stores/test_blob_conformance.py` |
| A writer killed between leaf and root leaves the head readable and writable | 1 | `tests/stores/test_blob_conformance.py` |
| A lost race recomputes `changes` and `previous_root_sha256` | 1 | `tests/stores/test_blob_writer.py` |
| The blob writer refuses 2.x and names the reason | 1 | `tests/stores/test_blob_writer.py` |
| An upload writes nothing under `inventory/` or `CURRENT`, by a recording store | 2 | `tests/stores/test_upload_store.py` |
| A second upload writes no second `pending/` entry | 2 | `tests/stores/test_upload_store.py` |
| A wrong `manifest_sha256` is refused before any other file | 3 | `tests/stores/test_remote_catalog.py` |
| `Catalog.open` never returns an embargoed entry | 3 | `tests/stores/test_remote_catalog.py` |
| The 7,000-leaf measurement is in spec §6.5, with the summary decision | 4 | `bench/inventory-read-results.md`, then handoff row 6 |
| `just ci` passes | all | the gate, with `test-stores` |

### Verification Plan

*   `just ci` for each part, on a machine with a container runtime; without one the gate fails (W6).
*   `just coverage` in each part's PR, since `just ci` does not run it.
*   The real-account checklist in Part 2.
*   `uv run python bench/inventory_read.py` for Part 4, recorded with machine and versions,
    as `bench/corpus-results.md` is.

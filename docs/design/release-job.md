# Phase 5: the release job and garbage collection

*   **Author:** Gabriel Gellner
*   **Status:** Draft, revision 2. Revision 1 had an EGM review
    ([report](egm/release-job/review_report.md): 2 Blocker, 37 Major, 31 Minor). This revision
    applies every Blocker and Major and the Minors that change content; the section
    [Review response](#review-response) maps each finding to its change. Where the report left a
    choice to the owner (M18, M19, M20), this revision takes one and lists the other in the owner
    questions. The owner decided Q1 to Q9 on 2026-11-10: the recommendations for all but Q4, and
    for Q4 a least-privilege access check (D8).
*   **Date:** 2026-11-10
*   **Issue:** GH-52. Its Inventory parts: GH-125 part 4, and the server half of GH-125 part 3.
    Suite tracker: StatCat-Suite/statcat-suite.github.io#19 ("the suite tracker" below).
*   **Read against:** statcatlib `3306996` (`origin/main`, v0.16.2); sdmxlib `6a646b75`
    (`origin/main`: the 0.87.0 code, with StatCat-Suite/sdmxlib#381 and StatCat-Suite/sdmxlib#383
    merged into the spec and the plan); the hub at `83da9c2`, with the suite design
    [Inventory history log](https://statcat-suite.github.io/design/inventory-history-log/) at
    revision 4. Every `file:line` is at these commits. Find each site by its symbol and use the
    number as a hint. Issue texts were read on 2026-11-10.
*   **Inputs:**
    *   GH-52's body and comments. The plan's Phase 5 section restates them and overrides the
        body where they differ.
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        ("the spec") at `6a646b75`: §6.2 to §6.7, §6.9, §10.1, §10.3, §11.
    *   [sdmxlib's
        plan](https://github.com/StatCat-Suite/sdmxlib/blob/main/docs/design/sharded-bundle-catalog-plan.md)
        ("the plan"), Phase 5 (plan lines 348 to 376), with its bullets and gating tests.
    *   GH-125's body: four parts, of which this design plans part 4 and the server half of part 3.
    *   GH-134's real-account results of 2026-11-10, and of 2026-11-12 for handoff rows 6 and 6a.
    *   The owner's decisions, recorded below as O1 to O8 and not reopened.
    *   StatCat-Suite/sdmxlib#380 (manifest paths), StatCat-Suite/sdmxlib#382 (the upload grace
        period in §11.6), StatCat-Suite/sdmxlib#384 (`tiers=`), StatCat-Suite/sdmxlib#385 (a
        `final` seal leaves several files).
*   **Identifier prefixes.** D, O and Q are this design's. S1 to S17, W1 to W24 and F1 to F27 are
    the [Phase 4 design](catalog-on-blob-storage.md)'s, and this design continues the S series
    from S18. P1 to P7 are the [Phase 2a design](phase-2a-catalog-editions.md)'s. H1 to H24 are
    the suite design's. Plan Q1 to Q10 are written "plan Q9". `GH-N` is a statcatlib issue.

--------------------------------------------------------------------------------

## Overview

After Phase 4, a catalog on blob storage holds uploads in `pending/` that nothing records, and a
blob `commit` with an incoming entry raises `NotImplementedError`
(`store/inventory.py:411-417`). Phase 5 builds the two server jobs that turn uploads into
releases and keep storage bounded:

1. `statcat release`, the release job. Each run folds `pending/` registrations into the
   Inventory as `embargoed` entries, releases what is due and retires old entries. A run started
   with `--expire` on an opted-in Azure catalog also expires old Inventory versions. The release
   identity is the only writer of Inventory versions on a server.
2. `statcat gc`, the GC job. It deletes retired edition directories, unreferenced `data/` files
   and folded registrations, and writes no Inventory version.

Both run under their own workload identities (spec §10.1). Six parts ship them. After each part:

| Part | After it lands, a user can |
|---|---|
| 1. Artefact records in every leaf entry (GH-125 part 4), and the `statcat` section's `lifecycle` (S13) | commit a version with an incoming entry on a blob catalog. Every local seal now fills `artefacts` for data editions too, so a leaf entry grows from about 665 to 2,579 bytes with 10 records. |
| 2. `CatalogAdmin` on blob, `advance-current`, the jobs configuration | release, cancel, roll back and set the embargo on a blob catalog by hand, and write and check a jobs configuration. |
| 3. The release job | publish uploads on schedule on Azure. |
| 4. The GC job | reclaim storage on Azure. Before Part 4, nothing deletes on a server. |
| 5. `EmbargoReader` | open embargoed editions for the preview and for pre-materialisation. CUJ 1's 08:05 step needs it. |
| 6. Server expiry (GH-125 part 3, server half), provisional until GH-125's stage 2 design | expire old Inventory versions on Azure. |

| Capability | Local disk | Azure Blob Storage | S3 |
|---|---|---|---|
| Commit a version with the cross-product rules | Today | Part 1 | Part 1, not in the gate |
| Rules read artefact records from leaves | Part 1 | Part 1 | Part 1 |
| `CatalogAdmin` | Today | Part 2 | Part 2, not in the gate |
| `advance-current` | Part 2 | Part 2 | Part 2, not in the gate |
| Fold `pending/`, release on schedule, retire | Not applicable | Part 3 | Refused, as upload is (Phase 4 W3) |
| GC of `products/` and `pending/` | `CatalogAdmin.prune` (Phase 2a P7) | Part 4 | Refused |
| `EmbargoReader` | Part 5 | Part 5 | Part 5's path, with no upload to fill it |
| Expiry | GH-125 part 3 (`prune`) | Part 6 | Refused (H21, H23) |

### Terms

| Term | Meaning here |
|---|---|
| Run | One invocation of `statcat release` or `statcat gc`, from start to exit. |
| Orchestrator | Whatever starts runs: in the reference deployment, Kubernetes CronJobs. It knows the release times the agency publishes and schedules a pre-flight run and a release-time run for each, a nightly `--expire` run, and a nightly GC run. It learns nothing from the catalog. |
| Operator | A person who runs `statcat admin` on a server, under the release identity (D11). |
| Release identity | The workload identity the release job runs under, the only one with write on `inventory/` (spec §10.1). |
| Registration | `pending/<product_id>/<edition_id>.json`, written by upload (spec §11.2 step 1). |
| Fold | The release run's step that records registrations as `embargoed` entries (spec §11.2 step 2). |
| Incoming entry | An entry that becomes `live` or `embargoed` in the version being written (spec §6.5 item 4). |
| Refusal | A registration the fold does not record, or a due edition the release step does not release, with its reason. |
| Refusal record | The refusals of the newest release run, kept in the root's `extensions.statcat.release` (D5). |
| Filled entry | A `LeafEntry` whose `artefacts` is present. |
| Jobs configuration | The TOML file both jobs read: retention values, the access allowlist and the expiry gates (D7). |
| Storage clock | The `Date` of an anonymous `HEAD`, through `BlobInventoryStorage.now()` (Phase 4 D4). |
| Opted-in catalog | A catalog whose `catalog.json` is format 2.x, with `history.since_seq` and `inventory/HISTORY` (spec §6.1). Only an opted-in catalog expires. |
| `HISTORY` | `inventory/HISTORY`, the index of history segments; on Azure its blob lease is the catalog lock (spec §6.9). |
| `expired_through` | The highest `seq` expiry has removed from the head chain (spec §6.9). |
| Restore epoch | `catalog.json`'s `restore_epoch`, which a whole-catalog restore or failover bumps (spec §6.1). |
| Stage 2 | GH-125 parts 2 and 3: the catalog lock, opt-in, segments and expiry (spec §6.9). |
| Land, release | A part lands when its PR merges to `main`, and is released when a statcatlib version carrying it is on PyPI. |

## The Problem

The spec and the plan give the release job and GC their rules. Four questions remain for
statcatlib (plan line 350):

- the shape of `statcat release`: one run that folds and later releases, or separate runs;
- the retention configuration's schema;
- where `release_due`'s per-product refusals are persisted (spec §6.4);
- where the fold takes each product's `lifecycle` from (S13, Phase 4 review M7): `record`
  requires it (`_rules.py:178-195`), the manifest has none, and the registration holds two keys.

Six gaps in the code also block the jobs:

- A blob writer has no manifest reader, so a commit with an incoming entry raises
  (`store/inventory.py:411-417`, Phase 4 D5).
- `recorded_routing` fills `artefacts` for structure editions only
  (`store/working_copy.py:337`), so the rules read every live data manifest: about 6,300 GETs a
  cold commit at 7,000 products.
- `CatalogAdmin` runs on local disk only (Phase 4 W11), so a server has no manual release,
  cancel or rollback.
- `_ObjectOps` has no LIST and no conditional delete (`store/blob.py`), which the fold, GC and
  expiry need.
- statcatlib checks neither spec §6.5 item 3 (known `canonical_form`) nor item 5 (final means
  final); a grep for `content_fingerprint` and `canonical_form` under `src/statcatlib` finds
  nothing.
- No code lists role assignments, reads pod versions, or holds an Azure lease.

## Goals

1.  A 200-table synthetic release, in the plan's mix of one-table and grouped products, becomes
    visible in one Inventory version, and a re-run of either job changes nothing (Part 3).
2.  No edition is recorded whose manifest, listed files or references fail their checks, and no
    edition becomes `live` through any writer whose manifest differs from the one recorded
    (Parts 1 and 3).
3.  Every refusal is persisted with its reason where the next run and an operator can read it,
    and the batch goes on (Part 3).
4.  GC deletes exactly the files no recorded, tombstoned or registered edition lists, nothing the
    newest K roots reach, and no file younger than the upload grace period (plan Goal 4; Part 4).
5.  The cross-product rules on a commit open no manifest of another product's filled entry
    (Part 1).
6.  An operator can release, cancel, roll back and stop releases on a blob catalog (Part 2).
7.  No release step runs while a blob-read role assignment that applies to the container names a
    principal outside the allowlist (Part 3).
8.  The preview and pre-materialisation open embargoed editions only through `EmbargoReader`
    (Part 5).
9.  Server expiry runs only under the `HISTORY` lease and both gates (Part 6).

## Non-Goals

*   Pre-materialisation. statcat-serve runs it on the pods' image, through `EmbargoReader` (plan
    Q8, Q9). This design gives it the reader and a place in the schedule.
*   GC of `results/`, which Phase 6 defines.
*   Upload, release or GC on S3. Upload is Azure-only (Phase 4 W3), so a server catalog on S3 has
    nothing to fold.
*   `correct` and `reinstate`, which stay in GH-130.
*   GH-125's local stage 2: the `flock` lock, the stale-base refusal, local opt-in, the expiry
    engine, restores and `verify-catalog`'s chain checks. Part 6 depends on them.
*   An operation that changes a product's `lifecycle` (Q8).
*   Alert delivery. The jobs log each alert as a structured event; Phase 8 wires them (plan).
*   A backfill of `artefacts` into entries recorded before Part 1.
*   The 404 for an embargoed dataflow from every pod, which GH-52's comment of 2026-11-02 moved to
    Phase 6.

## Open Questions

### Owner decisions on the design's questions

The owner decided Q1 to Q9 on 2026-11-10; each row is decided (owner, 2026-11-10). The Gates
column names the part the decision was needed for.

| # | Question | Decision | Trade-off accepted | Gates |
|---|---|---|---|---|
| Q1 | What does the root's `embargo` mean for the release step on a server? | `embargo: true` holds every automatic release, as the table below states. A server catalog runs with `embargo: false`, and `statcat admin embargo on` is the stop switch (D4). | On an opted-in catalog the switch lands between runs, since a run holds the lease (D1). The name reads oddly on a server. A separate hold flag would be an Inventory format change. | Part 3 |
| Q2 | Where are refusals persisted? | In the root, as `extensions.statcat.release`: the refusals of the newest release run (D5). | Each root carries the record, at about 300 bytes a refusal, capped at 500, and segments keep it for good. The rejected alternative, "each run recomputes and reports them, and nothing is stored", loses `since` and leaves the reasons in logs only. | Part 3 |
| Q3 | Where does the jobs configuration live? | One TOML file, deployed to both jobs from one ConfigMap. Each run checks it and logs its SHA-256 (D7). | Two jobs could run different copies; the logged hash shows it. | Part 2 |
| Q4 | What permissions does the access check use, and what does it count? | Least privilege, in place of revision 2's recommendation. The release identity holds one custom role, scoped to the storage account only, with `Microsoft.Authorization/roleAssignments/read` and `Microsoft.Authorization/roleDefinitions/read` and nothing else: no Reader at the management group or subscription, and no Microsoft Graph permission. The check lists the role assignments that apply to the catalog's container, inherited ones included, keeps those whose role grants blob read, and compares each assignment's principal object id with the allowlist. A group is compared as one principal by its object id (D8). | Group membership and PIM-eligible assignments are outside the check, and the owner reviews them elsewhere (D8's known limits). That the account-scoped role lists inherited assignments was an assumption (A1), which a real-account check confirmed on 2026-11-12 for every scope but the management groups (D8); the sentinels fail the check closed where it does not hold. The owner deploys to Kubernetes over ADLS Gen2 with little other Azure tooling, and rates tenant-wide directory read as a larger risk than these gaps. | Part 3 |
| Q5 | A registration already due when the fold records it: release it in the same run, or hold it? | Release it in the same run, in its own version after the fold's, and log `release.unprepared`. A registration whose `release_at` is earlier than its own last-modified time is recorded with `release_at: null`, so only `statcat admin release` publishes it (D2). | The first requests for the table miss the result cache. A genuinely late upload whose time has passed needs a manual release. | Part 3 |
| Q6 | Does GC delete a registration that the fold refuses for age? | Yes: GC deletes a registration whose `edition_id` time is older than the tombstone window (S20). An operator removes any other refused registration with `statcat gc --discard` (D10). | An expired registration stops protecting `data/` files. | Part 4 |
| Q7 | A registration of a product the Inventory does not record, from a statcatlib that writes no `lifecycle`: refuse, or record `revisable`? | Refuse, naming the fix: seal with statcatlib 0.17, the release with Part 1 (D3). | A refusal costs one re-seal; no server catalog holds production data yet. | Part 1 |
| Q8 | Does Phase 5 add an operation that makes a `final` product `revisable`? | No. An issue records it, since spec §6.5 item 5 names the operation and nothing needs it before Phase 9. | A `final` product whose content changes cannot be republished until then; its fold is refused with rule `final`. | none |
| Q9 | Does Part 6 stay in this design, or move to GH-125's stage 2 design? | Keep it here as the release job's requirements on GH-125's engine, marked provisional, and re-review it once GH-125's design lands (handoff row 11). | Nothing in Part 6 can run before GH-125's design and code. | Part 6 |

**Q1's table.** "Automatic" means any release no operator asked for.

| Catalog | `embargo: false` | `embargo: true` |
|---|---|---|
| Local, today | A seal records `live`. | A seal records `embargoed`; `CatalogAdmin.release` releases. |
| Server, today | No release job exists. | No release job exists. |
| Server, with S18 | The fold records `embargoed`; the release step releases what is due. | The fold records `embargoed`; the release step releases nothing and logs `release.held`; `statcat admin release` still releases. |

Spec §6.6 Retry already says "A stale automatic release must not write `live` after a concurrent
change turned embargo on"; S18 states what that implies for `release_due`. A server catalog
starts with `embargo: false` when `write_catalog_json` and the first version create it, and the
deployment runbook says so.

### Spec and suite questions

S1 to S17 belong to the Phase 4 design. StatCat-Suite/sdmxlib#381 resolved S1, S12 and S14.
This design proposes S13 and S18 to S30. Each changes a document another repository owns; this
design edits neither. "Owner" is the repository whose document changes.

| # | Where | Change | Options | Recommendation | Owner | Gates |
|---|---|---|---|---|---|---|
| S13 | Spec §11.2 step 2; statcatlib's `docs/catalog-format.md` §3 | The fold takes `lifecycle` from the product's leaf when the Inventory records it, and from the edition's `statcat` section otherwise; it refuses a mismatch or an absent value (D3). | D3's table | The section | sdmxlib (spec text), statcatlib (section) | Part 1 lands; spec text before Part 3 |
| S15 | Spec §4.6 line 277 | Phase 4's question on extensions at `open`. No Phase 5 job opens a view except `EmbargoReader`, which runs the same check as `Product.view`. | resolve now; move | Move to Phase 6 with S4 (StatCat-Suite/sdmxlib#384) | sdmxlib | none in Phase 5 |
| S18 | Spec §6.2 `embargo`, §6.4, the worked example at spec lines 460-466 | With `true`, no automatic release runs (Q1's table). The worked example's release version shows `"embargo": true`, and becomes `false`. `release_due` itself does not change; the writer checks the flag. | Q1 | Q1 | sdmxlib | Part 3 |
| S19 | Spec §6.2 `extensions`, §6.4 "Release batches report per product", §11.6 | `extensions.statcat` also holds the release job's refusal record (D5), where the alert's "persisted refusal reasons" are read. | Q2 | Q2 | sdmxlib | Part 3 |
| S20 | Spec §11.2 step 2, §11.4 | GC deletes a registration whose `edition_id` time is older than the tombstone window. | Q6 | Q6 | sdmxlib | Part 4 |
| S21 | Spec §6.4 table, §11.6 | Name the retire window ("past the window" in §6.4) and the pod staleness bound as configuration values (D7). | name them; leave them to deployments | Name them | sdmxlib | Part 2 |
| S22 | Spec §11.4, §11.6 | The deletion log is the GC job's standard output, one JSON line per planned and per performed deletion, since GC holds no write right in §10.1. GC also deletes, on an operator's `--discard`, an edition the Inventory does not record: its registration, then its manifest, then its directory. | log on stdout or an object; discard by GC or by the publish identity | stdout; GC, since §10.1 makes GC the only principal that deletes under `products/` | sdmxlib | Part 4 |
| S23 | Suite design, expiry step 6's leaf-set cache | The cache survives between runs on a persistent volume (`--state-dir`) and holds root paths, ETags, SHA-256s and leaf hashes only. Without it every run pays the first run's 1.7 to 4.7 GB read. | volume; in-memory per run | Volume | hub | Part 6 |
| S24 | Spec §10.1 table | GC reads and lists `products/` and `pending/` (manifests, registrations), and the table gives it delete only. Storage Blob Data Contributor, which deletes, includes the read. | state it | State it | sdmxlib | Part 4 |
| S25 | Spec §11.2 step 2, §6.9 Restores; suite design Restores | A registration last modified before the first root of the current restore epoch, whose edition the head does not record, is recorded with `release_at: null`, so only an explicit release publishes it (D2). The suite design's restore step 5 should also tombstone, in root S + 1, every entry the discarded roots recorded that root S lacks, so GC keeps their files while the tombstones are in the window. | hold such registrations; refuse them; clear `pending/` in the restore runbook | Hold them, and tombstone in S + 1 | sdmxlib, hub | Part 3; GH-125's restore |
| S26 | Spec §6.9, The catalog lock | A `CatalogAdmin` call on an opted-in Azure catalog waits up to 120 s for a run's lease and then fails, naming the break runbook, where the spec says it "waits for the run". Release-time runs hold the lease for seconds, since expiry runs only in `--expire` runs (D1). | wait for the run; bounded wait | Bounded | sdmxlib, hub | Part 6 |
| S27 | Spec §6.2 `event.kind`; §10.1 live-only export | A version that changes only the refusal record has kind `release` and empty `changes`. A live-only export drops `extensions.statcat.release`, since it names embargoed editions. | `release`; `describe`; a new kind | `release`: `describe` means the description changed, and a new kind costs every reader older than 0.82 the version (spec §6.2) | sdmxlib | Part 3 |
| S28 | Spec §11.2 step 2 | The fork rule ("two pending editions of one product built on the same live edition") keys on `previous` and `schema_version`, as `supersede_pending` does, and counts only registrations that pass the fold's per-registration checks. | as stated; `previous` alone | As stated | sdmxlib | Part 3 |
| S29 | Spec §10.1 | `statcat admin` runs under the release identity, so `event.actor` records the job's object id for an operator's change, and the operator is named only in `event.reason`. | as stated; a separate operator identity with write on `inventory/` | As stated for Phase 5; an operator identity is a §10.1 row for Phase 8 | sdmxlib | Part 2 |
| S30 | Spec §10.1 "The boundary is the account or container" | The release precondition lists, with a custom role scoped to the storage account (`roleAssignments/read`, `roleDefinitions/read`), the role assignments that apply to the catalog's container, inherited ones included, and keeps those whose role reads blobs. It compares each assignment's principal object id with the allowlist, a group as one principal. Group membership and PIM-eligible assignments are outside the check and reviewed by the deployment's owner; §10.1's "counting nested groups and eligible PIM assignments" becomes that review. The 2026-11-07 decision (O6) still holds: every blob-read role counts, and the publish job is on the allowlist. | owner decision Q4 | Q4 | sdmxlib | Part 3 |

### Cross-repo contract

This design is statcatlib's and stays local, because every contract change it needs is a text
change to documents whose formats it does not change: the spec gains rules for the jobs
(S13, S18 to S22, S24 to S30), and the suite design gains S23 and S25's restore sentence. No
Inventory, manifest or registration format changes. Readers outside statcatlib:

| Change | Writer | Readers |
|---|---|---|
| `statcat` section `lifecycle` (D3) | statcatlib's seal | the fold; sdmxlib ignores the section |
| `extensions.statcat.release` (D5) | the release job | `CatalogAdmin`, Phase 8 alerting, statcat-serve's operator views; pods ignore it |
| `EmbargoReader` (D12) | statcatlib | statcat-serve's preview and pre-materialisation (plan Q8, Q9) |
| D14's event names | both jobs | Phase 8 alerting |
| `[expiry] deployments` labels (D7) | the deployment | statcat-serve's manifests under `deploy/` must use the same labels |

The hub session takes S13, S18 to S22 and S24 to S30 to sdmxlib's spec in one PR and S23 and S25 to the
suite design as a revision with its own review, as the hub's rules ask for a suite design.

### Contract changes and handoff

In order. "Gates" names what cannot land until the row is done.

| Order | Change | Owner | Gates |
|---|---|---|---|
| 1 | Done: the owner decided Q1 to Q9 on 2026-11-10 | owner | none |
| 2 | Part 1 on sdmxlib 0.87, with no sdmxlib release; it carries S13's section field | statcatlib | Parts 2 to 5 |
| 3 | S21 and S29 in the spec | sdmxlib | Part 2 |
| 4 | Part 2 | statcatlib | Parts 3, 4, 5 |
| 5 | The spec PR with S13's text, S18, S19, S25's fold half, S27, S28, S30 | sdmxlib | Part 3 |
| 6 | Done 2026-11-12: the owner reran GH-134 item 9 with `inventory_corpus(artefacts=True)`, since the 2026-11-10 run used the default `artefacts=False` and its leaves carried no artefact records. The filled read took 0.94 to 1.54 s at 64 threads and 50.56 to 52.28 s on one thread (O8). | owner | Part 3 |
| 6a | Done 2026-11-12, except for management groups: the owner ran the new real-account check item (D8). An identity holding only the custom role at the storage account listed the assignments inherited from the resource group and the subscription, and read the role definitions they name. The tenant has no management-group assignment, so that scope is untested (A1). | owner | Part 3's first production release |
| 7 | Part 3 | statcatlib | Part 6 |
| 8 | S20, S22, S24 in the spec | sdmxlib | Part 4 |
| 9 | Part 4, which may land before Part 3 | statcatlib | none |
| 10 | Part 5, which may land any time after Part 2 | statcatlib | statcat-serve's pre-materialisation (Phase 6) |
| 11 | GH-125 parts 2 and 3, local, with their own design, which defines D13's interface; then this design's Part 6 is re-reviewed against it | statcatlib | Part 6 |
| 12 | S23, S25's restore half and S26 in the suite design | hub | Part 6 |
| 13 | Part 6 | statcatlib | the first server expiry, after `expiry_not_before` |
| 14 | StatCat-Suite/sdmxlib#380 (manifest paths). The fold already refuses unsafe paths with `_refuse_unsafe_files`, so it gates nothing. | sdmxlib | none |
| 15 | Issues: Q8's lifecycle change; GH-125's body records that D6 replaces its leaf-backed reader; GH-130's `reinstate` passes `artefacts=` | statcatlib | none |

StatCat-Suite/sdmxlib#385 changes how many data files a `final` seal writes, which the fold and
GC treat like any file list. StatCat-Suite/sdmxlib#384 is Phase 6's.

## Critical User Journeys (CUJs)

### CUJ 1: A scheduled daily release

At 06:00 a publish job seals 200 tables on its build node and runs `statcat upload` for each with
`--release-at 2026-11-12T13:30:00Z` (08:30 Eastern). At 08:00 the orchestrator runs `statcat
release`: the access check passes, the fold records the 200 editions as `embargoed` in one
version of kind `seal`, nothing is due, and the run exits 0. At 08:05 statcat-serve's
pre-materialisation job opens each embargoed edition through `EmbargoReader` (Part 5) and writes
`results/`. At 08:30:05 the orchestrator runs `statcat release` again: the release step moves the
200 entries to `live` in one version of kind `release`, and pods see them on their next poll.

### CUJ 2: An operator handles a refused release

One table was built on an edition a rollback withdrew. At 08:30 the release step refuses it,
releases the other 199, logs `release.refused`, and exits 0. The refusal record holds the
edition, `"rule": null` and `release_due`'s message, which names both editions. The operator
runs `statcat admin editions az://… 14100287`, cancels the edition with `statcat admin cancel …
--reason "jdoe: built on withdrawn base"`, and the producer rebuilds from the live edition.

### CUJ 3: A correction uploaded before the fold

A producer uploads B1 at 06:00 and a corrected B2 at 07:30, both on live edition L. The 08:00 fold
refuses both with `pending-fork`. The operator runs `statcat gc az://… --discard 14100287/<B1>`
under the GC identity, which deletes B1's registration, manifest and directory. The next release
run folds B2.

### CUJ 4: GC runs at night

At 02:00 `statcat gc az://… --config jobs.toml` checks the retention inequalities, reads the
Inventory back through the GC window and at least the newest 10 roots, reads the manifest of every
kept and every registered edition, lists `products/` and `pending/`, and deletes retired editions'
directories, unreferenced `data/` files older than 24 h, and folded registrations older than the
window. Each planned and performed deletion is one JSON line in its output.

## Technical Plan (Architecture)

### Recorded decisions

The owner decided these before this design. They are inputs.

| ID | Date | Decision | Consequence here |
|---|---|---|---|
| O1 | 2026-11-03 | The fold skips a registration the Inventory already records, in a leaf or in `retired`, and records the kind `seal`. The release job keeps read-only access to `pending/`. GC removes folded registrations after the window. | D2, D10 |
| O2 | 2026-11-03 | Plan Q9: the operator runs the `EmbargoReader` preview; the suite ships `EmbargoReader` in Phase 5. | D12 |
| O3 | 2026-11-04 | Phase 5 fills `LeafEntry.artefacts` in every entry, and the cross-product rules read records from leaves. | D6 |
| O4 | 2026-11-04 | Server expiry is Azure-only (H21, H23). | D13 |
| O5 | 2026-11-07 | GC protects every file a registered edition's manifest lists, and fails closed per product when it cannot read or verify one. A sealed but unregistered edition gets an alert only. | D10 |
| O6 | 2026-11-07 | The access check counts every blob-read role at every scope that reaches the container, with the publish job on the allowlist. Q4 (2026-11-10) keeps this and compares a group as one principal; group membership and PIM-eligible assignments are reviewed outside the check (S30). | D8 |
| O7 | 2026-11-07 | The upload grace period is 24 h by default, longer than the longest upload, with no timeout check. | D7 |
| O8 | 2026-11-06, measured 2026-11-10 and 2026-11-12 | Concurrent leaf reads in place of a baked summary (StatCat-Suite/sdmxlib#375). GH-134 item 9 read 7,000 leaves in 0.94 s at 64 threads from the account's region, and in 51.12 s on one thread. That run used `inventory_corpus`'s default `artefacts=False`, so its leaves carried no artefact records. The rerun of 2026-11-12 with `artefacts=True` (handoff row 6) read the filled leaves in 0.94 to 1.54 s at 64 threads and 50.56 to 52.28 s on one thread, so artefact records leave the read time about where it was, and the concurrent read serves plan Goal 1 (D6's cost has the run). | D6's cost |
| O9 | 2026-11-10 | Q1 to Q9, recorded in the Open Questions section; Q4 as a least-privilege access check. | D1 to D13 |

### Design decisions

| ID | Decision | Settles |
|---|---|---|
| D1 | `statcat release` is one command whose runs are short, ordered and idempotent, with failures isolated per step; expiry runs only in `--expire` runs. | plan line 350, shape |
| D2 | The fold checks each registration, records the passing ones in one `seal` version, and refuses the rest per registration. | spec §11.2 step 2 |
| D3 | The fold takes `lifecycle` from the leaf, else from the edition's `statcat` section. | S13 |
| D4 | The release step runs `release_due` over every leaf and drops refused products until the rules refuse none. | spec §6.4, §6.5 |
| D5 | Refusals are persisted in `extensions.statcat.release` of the newest root. | plan line 350, refusals |
| D6 | Every recorded entry carries `artefacts`; the rules read an incoming entry's manifest and every other filled entry's leaf. | GH-125 part 4 |
| D7 | One TOML jobs configuration, checked by both jobs. | plan line 350, retention schema |
| D8 | The access precondition lists the container's blob-read role assignments with a two-action role at the storage account, behind a replaceable listing, and compares their principals with the allowlist. | spec §10.1, Q4 |
| D9 | The release run writes the `retire` version. | plan, Retirement |
| D10 | `statcat gc` plans from the Inventory, the registrations and a LIST, and fails closed per product. | spec §11.4 |
| D11 | `CatalogAdmin` and `advance-current` over a blob catalog, with `statcat admin`. | Phase 4 W11 |
| D12 | `EmbargoReader`, a separate object over `FullInventory`. | plan, `EmbargoReader` |
| D13 | Server expiry's requirements on GH-125's engine: the lease, the gates, the persisted leaf-set cache and `If-Match` deletes. | GH-125 part 3 |
| D14 | Each alert is a structured event with a fixed name. | spec §11.6 |
| D15 | The command line builds every client and reads the environment; the library takes them as arguments. | the library rule (`src/statcatlib/EGM.md:386-388`) |

### D1: the shape of `statcat release`

```text
statcat release AZ_URL --config FILE [--dry-run]                              # Part 3
statcat release AZ_URL --config FILE [--dry-run] [--expire] [--state-dir DIR]  # from Part 6
```

Part 3 ships the first line. `--expire` and `--state-dir` arrive with Part 6, and so do
`ReleaseJob`'s `pods=` and `state_dir=` and `run`'s `expire=` (D15), since each would be accepted
and do nothing before it.

A run performs these steps in order. Each step that changes the Inventory writes its own version
through `InventoryWriter.commit`, so a lost race re-plans that step against the new head (spec
§6.6 Retry).

| Step | What it does | Version |
|---|---|---|
| 0. Start | Build the clients (D15). Read `catalog.json`; refuse a 2.x catalog with exit 1 naming Part 6 until Part 6 lands, since the writer refuses one (`store/inventory.py:572`). Load and check the jobs configuration (D7). Read the storage clock, refusing a clock more than 30 s ahead. | none |
| 1. Lock | On an opted-in catalog (Part 6), take the `HISTORY` lease; a held lease ends the run with exit 1 and writes nothing. | none |
| 2. Advance | Move `CURRENT` to the head, forward only (spec §6.6), logging `current.behind` when it moved. | `CURRENT` only |
| 3. Access | Run the access precondition (D8) and keep its result. | none |
| 4. Fold | Record passing registrations as `embargoed` (D2). | kind `seal` |
| 5. Release | Write the refusal record (D5); when step 3 passed and the head's `embargo` is `false`, release what is due (D4). | kind `release` |
| 6. Retire | Tombstone superseded and withdrawn entries past `retire_after` (D9). | kind `retire` |
| 7. Expire | Only with `--expire`, on an opted-in Azure catalog with expiry enabled and both gates passed (D13). | none; segments and deletes |
| 8. Finish | Print the summary, release the lease. | none |

**Failure isolation.** Steps 2, 4, 5 and 6 each catch the errors of their own work: a store
fault, a `ManifestError` from an unfilled entry's manifest, or `InventoryContentionError`. A
failed step logs `step.failed` with the error and the run goes on to the next step, so a fault in
the fold does not hold a release that the pre-flight run already folded. Step 2 is isolated with
them, since a `CURRENT` that cannot move holds no release: the root is the commit point, and
readers probe forward. A store fault on one registration is that registration's refusal (rule
`store`). Step 0's refusals, a held lease (step 1) and a credential error stop the run.

**The clock.** The run reads the storage clock once at step 0 and once at the start of each of
steps 4, 5 and 6. A step's rules, its version and its events carry that step's time, so a release
planned after a long fold uses the time the release step read.

A step that finds nothing to do writes nothing (`commit` returns `None` for a `None` change, a
Part 2 change), so a re-run against an unchanged catalog writes nothing. `--dry-run` runs steps 0
and 3 and the plans of 4 to 7, moves no `CURRENT`, and writes nothing.

**Exit codes.** 0: every step ran; refusals, a held release and a skipped step are reported in
the output and the events, and do not change the code. 1: a step failed or the run stopped, each
printed as `ERROR <message>`. 2: usage, as `statcat upload`'s, and a store other than
`AzureStore`. A CronJob sets `backoffLimit: 0`, since the next scheduled run repairs any step
(the deployment runbook states it).

**The schedule.** The orchestrator runs the command at least twice for each release time: a
pre-flight run (08:00 in CUJ 1), after which pre-materialisation runs, and a run just after the
release time. Any run releases what is due, so a missed run is made up by the next. A nightly
`--expire` run at a time with no release expires; release-time runs leave expiry out and hold
the lease, on an opted-in catalog, for the seconds their steps take. A CronJob with
`concurrencyPolicy: Forbid` keeps one run at a time; on an opted-in catalog the lease also
refuses a second run (spec §6.9).

A registration due when the fold records it is released in step 5 of the same run, in a second
version, and the run logs `release.unprepared` for it (Q5).

**Output.** Standard output holds one JSON line per event (D14) and nothing else; standard error
holds the human lines (`REFUSED <product>/<edition> <rule or ->: <reason>`, `SKIPPED <step>:
<reason>`, `ERROR <message>`) and the summary.

| Option | Gain | Cost |
|---|---|---|
| **Short ordered runs of one command (chosen).** | One identity, one lease holder, one CronJob. The next run repairs a crash. No process holds a credential while it waits. Pre-materialisation fits between two runs. | The orchestrator owns the timing, and must schedule the pre-flight run. |
| One long run that folds, waits for pre-materialisation, then releases. | The schedule is in one place. | A run that waits 30 minutes meets the lease's 30-minute cap (spec §6.9) and blocks every admin call meanwhile. The run must learn when pre-materialisation finished, which is another job's state. A crash during the wait loses the release until someone notices. |
| Separate commands, `statcat fold` and `statcat release`. | Each is smaller. | Two CronJobs can overlap on a catalog with no lease, and spec §11.2 step 4 wants one writer under `concurrencyPolicy: Forbid`. The fold runs before every release anyway. |

### D2: the fold

The fold reads the head with `FullInventory.leaves()` (every leaf, on the reader's 64-thread
pool), lists `pending/`, and checks each registration in the order of its path. The checks run in
the order of the table, and the first that fails is the registration's refusal. Every check reads
the head's leaf for the product, or `retired`; none reads another registration.

The skip of a recorded edition comes first, and is decided from the registration's path,
`pending/<product_id>/<edition_id>.json`, before its bytes are read. A folded registration stays
in `pending/` until GC deletes it after the GC window (D10), so a catalog that folds 200
registrations a day lists about 7,000 folded ones, and a fold that read each would make that many
GETs in sequence on every run. GC skips them the same way. An object under `pending/` of another
shape is left alone, as GC leaves it.

| Check | Refused with rule | Source |
|---|---|---|
| The head records the edition its path names, in the product's leaf or `retired`. | none: skipped, and never read | O1 |
| The bytes equal `upload._pending_document(edition_id, release_at)` re-encoded from their decode: two keys in spec §11.2 step 1's order, compact, `edition_id` equal to the file name and a UUIDv7, `release_at` `null` or UTC with `Z` and milliseconds. | `pending-document` | spec §11.2 step 1 |
| The `edition_id`'s UUIDv7 time is older than `tombstone_window` by the storage clock. | `too-old` | spec §11.2 step 2 |
| `products/<p>/<e>/manifest.json` exists, decodes (`decode_manifest`), names the product and edition of its path, and passes `_refuse_unsafe_files` (`store/blob.py`). | `manifest` | spec §6.5 item 2, StatCat-Suite/sdmxlib#380 |
| `canonical_form` equals `sdmxlib.edition.CANONICAL_FORM`. sdmxlib knows one form today, so equality is "one the release job knows". | `canonical-form` | spec §6.5 item 3 |
| Every listed file is in the store at its size and digest: `verify_remote` over every tier, through upload's `_lookup` over `AzureBlobRest.head`. | `files` | spec §11.2 step 1 |
| `lifecycle` resolves (D3), and the section's `kind` is one of `PRODUCT_KINDS` and equals the leaf's for a recorded product. | `lifecycle`, `kind` | S13 |
| `previous` is `null`, or an edition the product's leaf or `retired` holds. | `chain` | spec §6.5 item 6 |
| For a `final` product: the edition's `content_fingerprint` equals that of the product's live edition at its `schema_version`, or else its live edition at any schema, or else `previous` when the leaf records it. A tombstoned `previous`, with no live edition to compare, is refused, since its manifest may be gone. A product with nothing recorded passes. | `final` | spec §6.5 item 5 |
| An edition with no bindings passes the reference check below. | `references` | spec §11.2 step 2, Phase 4 handoff row 9 |
| Any store fault while checking. | `store` | D1 |

**The reference check.**

```python
own = pull_edition(location, p, e, manifest_bytes, tmp / "products" / p / e)
homes = [pull(tmp, s) for s in structure_entries(full, product_id=p, batch=fold_structures)]
report = check_edition(
    own, at="receive",
    remote=f"{location.uri}products/{p}/{e}/",
    lookup=_lookup(rest, manifest),
    structures=homes or None,                     # an empty list raises ValueError
    extensions={"statcat": _statcat_check()},
)
```

`structure_entries` is `structure_editions`'s choice (`store/working_copy.py`) over Inventory
entries in place of paths: every other structure product's live and embargoed editions, with
this fold's own structure editions in place of their products' recorded ones. `pull` uses
`pull_edition` into `<tmp>/products/<s>/<e>`. A shared `_check_structures` loop, taken from
`Catalog.seal`'s structure-check pass, repeats the check over the fold's structure editions until
a pass refuses none. An edition with no bindings has no observations, so `check_edition` reads no
data file through DuckDB and its engine needs no `StoreSecret`; a no-binding edition that does
list data files fails with `references` and names the missing secret. The check runs once per run,
before the commit; a lost race re-plans the recording below with the check's result kept. The
temporary directory is deleted at the end of the run. The partials check needs no rerun: the
producer's seal ran it against a structure edition, and item 4 at the fold matches each
partial's `partial_of` to the fingerprint the home holds.

**Recording.** The `commit` callable takes the passing registrations and applies sdmxlib's rules
to the head's leaf, per product, in `edition_id` order:

- Two or more passing registrations with one `previous` and `schema_version` are each refused with
  `pending-fork`, naming the others (S28).
- Otherwise `record(leaf, embargo=True, release_at=<the registration's, or null under Q5's
  rule>, lifecycle=<D3>, kind=<the section's>, retired=<the product's tombstones>, now=<storage
  clock>, manifest_sha256=<the bytes read>, artefacts, dataflows, pull_bytes from
  routing_fields(manifest))`. A `TransitionError` becomes a refusal with its rule.
- When the leaf holds an `embargoed` entry with the same `previous` and `schema_version`, the fold
  calls `supersede_pending(leaf, <that entry>, by=<new>, now=)`, which withdraws it (plan, Fold).

**Past and restored registrations.** A registration whose `release_at` is earlier than its own
last-modified time is recorded with `release_at: null` and logged `fold.release_at_past` (Q5). On
a restore epoch above 0, a registration last modified before the first root of that epoch is
recorded with `release_at: null` and logged `fold.restored` (S25), so a registration a restore
re-armed publishes only by an explicit release. The fold finds that root's `created_at` by walking
back from the head once per epoch and keeps it in the refusal record as `epoch_started_at` (D5).

The callable then runs `drop_until_stable(writer, full, leaves)`. Each pass first reads every
incoming entry's manifest through the writer's reader, which compares its SHA-256 with the entry's
(spec §6.5 item 2): a `ManifestError`, for a manifest that is absent or changed since the fold
read it, drops that product with `manifest`, and a `BlobStoreError` drops it with `store`. The
pass then runs `writer.refusals(full, leaves)` and drops each refused product's whole leaf change,
a `supersede_pending` included, with the rule the refusal names. Passes repeat until one refuses
none, as `precheck_refusals` does (`store/working_copy.py:848-890`). A dropped product refuses
each registration its change would have recorded. A manifest of another product's unfilled entry
that cannot be read fails the step (D1).

**Dependents.** A reference check can pass because of a fold edition that ends up unrecorded: one
of a fork, one sdmxlib's `record` refuses, or one of a dropped product. An edition whose check was
given such an edition is refused with `references`, its product's whole change is dropped, and the
recording runs again until a pass drops none, as `Catalog.seal`'s `_drop_dependents` does. The
next run checks it against what the Inventory then records.

It commits with `kind="seal"` and the reason `fold of pending/ registrations`. The reason carries
no count, since `commit` takes it before the callable plans, and a plan made beforehand would read
every leaf a second time. The per-registration reads are kept in the `ReleaseJob` by path for the
run, so a lost race re-plans the recording without reading them again.

The fold writes nothing under `pending/`, deletes nothing, and reads `pending/` and `products/`
only.

| Option for the file check | Gain | Cost |
|---|---|---|
| **`verify_remote` on every fold (chosen).** | No edition is recorded whose bytes are gone, the failure S12 named. One `HEAD` a file: about 3,000 for 200 editions. | The release job needs `AzureBlobRest`. |
| Trust the upload's check. | No requests. | A file lost between upload and fold reaches pods as a failed open at release time. |
| Full `check_edition(at="receive")` on every edition. | Lake and bindings checked again. | Pulls every edition's `metadata.duckdb` to the job, about 9 MB a table, 1.8 GB for 200. |

### D3: the fold's `lifecycle` (S13)

`WorkingCopy.seal` writes the product's lifecycle into the manifest's `statcat` section as a new
key, `lifecycle` (`"final"` or `"revisable"`), through `catalog_section(store, kind=,
lifecycle=)`. The value is the working copy's, which is the leaf's for a recorded product
(`store/working_copy.py:999-1004`), and is the value `sdmxlib.edition.seal` compacts by. The
section format stays 3: the key is optional, `_section_findings` ignores keys it does not read
(`bundle.py:353-377`), and `check_catalog` gains a finding for a value outside the two.
`docs/catalog-format.md` §3 and §3.1 state the key.

`fold_lifecycle(leaf, manifest)` resolves the value:

| Head records the product | Section `lifecycle` | Fold records |
|---|---|---|
| yes | absent | the leaf's |
| yes | equal to the leaf's | the leaf's |
| yes | different | refused, `lifecycle`, naming both |
| no | present | the section's |
| no | absent | refused, `lifecycle`, naming statcatlib 0.17 as the fix (Q7) |

`record` relabels the leaf with the lifecycle it is given (`_rules.py:268-274`), so the third row
is the only guard against a silent change; the fold runs it before `record`. `record_uploaded` in
`tests/stores/_stores.py` takes `lifecycle: Lifecycle | None = None` and uses `fold_lifecycle`
when it is `None`.

| Option | Gain | Cost |
|---|---|---|
| **The `statcat` section (chosen).** | Written by the call that compacts by it, hashed by `manifest_sha256`, and owned by statcatlib, so no sdmxlib format changes. | Editions sealed before 0.17 have none (Q7). |
| A third key in the registration. | The fold reads one document. | The spec's registration format changes (§11.2 step 1), upload gains a flag, and the flag can disagree with how the seal compacted. |
| A product map in the jobs configuration. | No format change. | Out of band, and stale whenever a producer adds a product. |
| Always `revisable` for a new product. | Nothing to write. | A `final` product loses item 5 with no message. |

### D4: the release step

```python
def change(full: FullInventory | None) -> Change | None:
    now = storage.now()
    record = refusal_record(full, run_refusals, now)              # D5
    if full is None or full.embargo or not access.passed:
        return None if record == current_record(full) else Change(leaves={}, extensions=record, now=now)
    leaves = full.leaves()                                        # the 64-thread pool
    changed, refused = release_due(leaves, now)
    changed, dropped = drop_until_stable(writer, full, changed)   # cross-product rules, item 2 included
    record = refusal_record(full, run_refusals | refused | dropped, now)
    if not changed and record == current_record(full):
        return None
    return Change(leaves=changed, extensions=record, now=now)

writer.commit(change, kind="release", reason=f"release_due at {now:%Y-%m-%dT%H:%M:%SZ}")
```

- `now` is the storage clock (spec §6.4). `Change` gains `now`, which `commit` passes to
  `try_commit(now=)`, so the rules and the root use one time and the run sends one `HEAD` (a Part
  2 change).
- `drop_until_stable` runs `writer.refusals(full, changed)`, drops each refused product's whole
  change, and runs again until a pass refuses none. D6 makes the rules read each incoming entry's
  manifest and compare its SHA-256 (spec §6.5 item 2), so a mismatch or an absent manifest drops
  the product with `manifest`; the same check now guards `CatalogAdmin.release` and `rollback`.
- A structure edition and the data editions cut from it release in the same version when both
  are due, so item 8 passes; a data edition whose structure edition is not due stays `embargoed`
  with `publish-order`.
- `release_due`'s refusals arrive as messages (`_rules.py:323-347`). The step keeps each message
  as the reason, with `rule` `null`; sdmxlib's `fast-forward` message names both editions.
- The commit re-applies `change` to each head it lands on (`store/inventory.py:713-760`). On a
  catalog that has not opted in, a concurrent `set_embargo(on=True)` turns the retry into a held
  run that releases nothing, the plan's "stale automatic release" test. On an opted-in catalog the
  lease keeps admin calls out during the run, and the switch takes effect at the next run.
- A crash before the root leaves every entry `embargoed`; the next run plans the same release.
- After the step, every entry still `embargoed` with a `release_at` before `now` is logged
  `release.overdue`. That holds in the held and the skipped branches too, where the event is the
  only one that names each waiting edition, so the step reads the leaves in every branch.
- When the fold failed, it judged nothing, so `run_refusals` holds the head's fold refusals
  unchanged and the record keeps them with their `since`. Under a hold or a failed access check
  the release step judges nothing either, and the record then holds the fold's refusals alone, as
  the code above writes.

| Option for the embargo flag (Q1) | Gain | Cost |
|---|---|---|
| **`embargo: true` holds automatic releases (chosen).** | One stop switch for an operator, and the meaning spec §6.6 Retry already implies. | Server catalogs run with `embargo: false`, a name that reads backwards there. |
| The release step ignores the flag. | No spec text. | Contradicts §6.6 Retry, and an operator has no stop short of stopping the CronJob. |
| A new root field `hold`. | A clear name. | An Inventory format change in sdmxlib, for a switch the flag already gives. |

### D5: the refusal record

The newest release run's refusals live in the root's `statcat` section, beside `description`:

```json
{"format_version": "3", "description": {"…": "…"},
 "release": {"refused": [
   {"product_id": "14100287", "edition_id": "01a18e76-…", "step": "release", "rule": null,
    "reason": "14100287: 01a18e76-… is built on 01a17f00-…, but the live edition at schema 2.52 is 01a18e63-…. Discard it, then build from 01a18e63-….",
    "since": "2026-11-12T13:30:05.120Z"}
 ], "omitted": 0, "epoch_started_at": null, "restore_epoch": 0}}
```

| Field | Meaning |
|---|---|
| `refused` | Every refusal of the run's steps 4 and 5, sorted by `product_id`, `edition_id`, `step`. |
| `step` | `fold` or `release`. |
| `rule` | The rule name for statcatlib's and the cross-product rules' refusals; `null` for `release_due`'s. |
| `reason` | The refusal's message. |
| `since` | The storage time of the first run that refused the same product, edition and step, carried from the head's record. |
| `omitted` | How many refusals the cap left out. At most 500 are kept, the oldest `since` first, so a standing refusal is never pushed out by new ones; the events hold them all. |
| `epoch_started_at` | The `created_at` of the first root of the restore epoch `restore_epoch` names, once a fold has found it, else `null`; always `null` on epoch 0 (D2). |
| `restore_epoch` | The catalog's restore epoch when the record was written. Every later root carries the record, a restore's root included, so after a second restore the carried `epoch_started_at` is the earlier epoch's. Read as the new epoch's start, it would let a registration written between the two starts keep its `release_at`. The fold trusts the time only when this key equals the catalog's epoch, and otherwise walks back for the new epoch's first root. |

- Step 5 writes the record whenever it differs from the head's, also when the release is held or
  skipped, so the fold's refusals persist under a failed access check. A record equal to the
  head's writes nothing, so a re-run with the same refusals writes nothing.
- A version that changes the record alone has kind `release` and empty `changes` (S27), and uses
  one of the newest `keep_roots` slots.
- Every later version carries the section unchanged (`store/inventory.py:269`), so the record
  reads "as of the newest release run". `CatalogAdmin.editions` shows the current states beside
  it.
- `ROOT_SECTION_FORMAT` stays `"3"`: `docs/catalog-format.md` §1.2 tells a reader to ignore a key
  it does not know.
- **Cost.** About 300 bytes a refusal: 150 KB a root at the cap. History segments keep each
  expired root's `extensions` for good (spec §6.9), so a standing cap-sized record adds about
  1.5 MB a day to `history/` at 10 versions a day; a typical record of a few refusals adds
  kilobytes.
- **Boundary.** Leaves and roots already name embargoed editions, and a filled leaf holds their
  URNs, so the record moves nothing across the §10.1 boundary. A live-only export drops it (S27).

| Option (Q2) | Gain | Cost |
|---|---|---|
| **Root `extensions.statcat.release` (chosen).** | No new path or role. The next run, `CatalogAdmin` and an alert reader find it with the head. | Roots and segments carry the record; a version can be written for the record alone. |
| Recompute and report each run, store nothing. | No format; the fold and `release_due` refuse the same entries every run. | `since` is lost, and the reasons live in logs that Phase 8 does not yet keep. |
| A statcat-owned object, `statcat/release/<seq>.json`. | Roots stay small. | A §10.1 row for the release job's write, GC rules for it, and a second document format. |
| A `LeafEntry` field. | Beside the entry. | An Inventory format change, and every refusal rewrites a leaf. |

### D6: artefact records in every leaf entry (GH-125 part 4)

- `recorded_routing(fields, kind)` returns `fields.artefacts` for every kind
  (`store/working_copy.py:322-337`). The seal, the batched seal, the journal repair, the fold and
  `record_uploaded` pass it to `record`. GH-130's `reinstate` passes it too, since
  `reinstate(artefacts=)` exists in sdmxlib 0.87 (`_rules.py:465-531`); handoff row 15 records it
  on GH-130.
- `cross_product` gets an entry's records through one private function, `_records(product_id,
  entry, artefacts, *, incoming)`: an incoming entry's come from the reader, which reads its
  manifest and checks its SHA-256 (spec §6.5 item 2, "reads each incoming edition's manifest");
  any other entry's come from `entry.artefacts` when it is not `None`, and from the reader
  otherwise. Every read site calls it: `refusals`' incoming and other-product loops,
  `stale_editions`, `homeless_partials`, `cited_structures`, and `working_copy._may_be_refused`
  (`store/working_copy.py:1661-1666`). The seal's provisional entry still reads through
  `StoreArtefacts` for its own working copy.
- `ManifestArtefacts(source: Path | Fetch)`. A `Path` keeps today's local read, its `_root` and its
  `_cache`, so `bench/corpus.py:164-176` keeps working. A `Fetch` reads
  `products/<p>/<e>/manifest.json` through it and decodes with `decode_manifest(raw,
  source=<that path>)`. An absent manifest and a SHA-256 mismatch raise `ManifestError`, as the
  local read does; a `BlobStoreError` from the fetch propagates, and the release job's step
  isolation (D1) catches it. `InventoryWriter` over a non-local storage defaults to
  `ManifestArtefacts(storage.read)`, so `_no_manifest_reader` and its `NotImplementedError` go
  (Phase 4 D5). A local writer keeps `ManifestArtefacts(root)`.
- `verify-catalog` already compares a filled `artefacts` with its manifest for every entry
  (sdmxlib `verify_entry`, `_routing.py:131-145`), so a data entry is checked once it is filled.
- An entry recorded before Part 1 stays unfilled until its product records again; the rules read
  its manifest. A daily product is filled within a day, and a `final` product never is.
- This replaces GH-125 part 4's "leaf-backed `Artefacts` reader as the writer's default": the
  `(product, edition, sha)` signature carries no entry, so the records are read where the entries
  are (handoff row 15).

**Cost.** A leaf entry grows from about 665 to 2,579 bytes with 10 records (spec §6.3). A cold
read of 7,000 leaves fetched 6.6 MB without records and 20.0 MB with them on Azurite. On the real
account the read took 0.94 s at 64 threads (O8), without records, and handoff row 6 measured the
filled shape on 2026-11-12 (below). A cold commit with an incoming entry then makes about 7,003 leaf
GETs on the pool and one manifest GET for each incoming entry, against about 13,300 sequential
GETs before (Phase 4 D5). The filled read serves plan Goal 1's 60 s on real storage, so the baked
summary (spec §6.5) stays unbuilt.

**The filled read on the real account** (handoff row 6, GH-134 item 9, 2026-11-12). The run used
statcatlib 0.17.0, sdmxlib 0.88 and obstore 0.12.1, from a VM in the account's region under the
VM's managed identity. `inventory_corpus(writer, artefacts=True)` at its defaults wrote 7,000
products in 7,016 blobs, 34.9 MB in all, in 130.9 s, one Put at a time; the largest blob is
1.9 MB. The corpus of 2026-11-10, without records, held about 21 MB. Each read is
`LiveInventory(fetch).refresh().editions()`, and made 7,003 GETs and returned 6,299 live editions
in every run.

| Read | First process | Second process |
|---|---|---|
| 64 threads, the store `statcat upload` builds | 1.54 s, cold | 0.98 s |
| 64 threads, the reader's store (item 8's) | 0.94 s | 0.99 s |
| 1 thread; the two runs overlapped in time | 50.56 s | 52.28 s |

The account's Transactions metric for the window shows GetBlob Success, PutBlob Success and 8
GetBlob ClientOtherError, the 404 that ends each reader's forward probe. It shows no ServerBusy
and no other error type, and no run met an authentication error or a provider failure. The
2026-11-10 run without records read in 0.94 s at 64 threads and 51.12 s on one thread, so
artefact records leave the read time about where it was, and the concurrent read still serves
plan Goal 1. The corpus prefix `checklist-9a/` is deleted.

| Option | Gain | Cost |
|---|---|---|
| **Records read from the entry inside `cross_product` (chosen).** | Every rule takes them, with no new reader type; the existing reader stays the fallback and the incoming check. | Six read sites change. |
| A leaf-backed `Artefacts` reader passed to the writer. | `cross_product` is unchanged. | The reader needs the planned leaves of each call, which its signature does not carry. |
| A backfill version that fills every entry. | Every entry is filled at once. | A new event kind, and a rewrite of every leaf, for a cost the daily republish removes. |

### D7: the jobs configuration

One TOML file, read with the standard library's `tomllib`, decoded into frozen attrs values in a
new module `jobs.py`. Unknown keys and a missing `format_version` are refused. A duration is a
string `<n><unit>`, `n` a positive integer and `unit` one of `s`, `m`, `h`, `d`, so one key reads
`"24h"` or `"35d"`; an instant is RFC 3339 with a zone. `[retention]` is required; `[access]` is
required by the release job; `[expiry]` is optional and defaults to `enabled = false`. In
`[access]`, `allowlist` holds principal object ids, a group's included; `sentinels` holds
assignment ids the listing must return (D8); an optional `scopes` lists scopes above the account
where the access-check role is also assigned, used only if assumption A1 fails (D8).

```toml
format_version = 1

[retention]
backup = "30d"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
keep_roots = 10
retire_after = "90d"
upload_grace = "24h"
pod_staleness = "5m"

[access]
storage_account = "/subscriptions/…/resourceGroups/…/providers/Microsoft.Storage/storageAccounts/acct"
container = "catalogs"
allowlist = [
  { object_id = "…", name = "release job" },
  { object_id = "…", name = "publish job" },
  { object_id = "…", name = "gc job" },
  { object_id = "…", name = "pods" },
  { object_id = "…", name = "search pods" },
  { object_id = "…", name = "preview deployment" },
]
# Assignments known to exist above the account, one per scope; each must appear in the listing.
sentinels = [
  "/subscriptions/…/resourceGroups/…/providers/Microsoft.Authorization/roleAssignments/…",
  "/subscriptions/…/providers/Microsoft.Authorization/roleAssignments/…",
]

[expiry]
enabled = false
not_before = "2027-01-15T00:00:00Z"
namespace = "statcat"
deployments = ["app=statcat-hot", "app=statcat-base", "app=statcat-search", "app=statcat-preview"]
port = 8000
```

| Key | Governs | Read by | First part | Spec name |
|---|---|---|---|---|
| `backup` | Nothing directly; checked against `gc_window` | both | 2 | backup retention (§11.6) |
| `gc_window` | How far back GC reads the Inventory; when a retired directory and a folded registration may go | GC | 4 | the `data/` GC window, §11.4's "retention window" |
| `tombstone_window` | The fold's `too-old` refusal; with Q6, GC's deletion of expired registrations. Tombstones themselves are never dropped today (spec §6.3). | both | 3 | the tombstone window (§11.2, §11.6) |
| `inventory_window` | When expiry may take a root | release `--expire` | 6 | W (§11.6) |
| `keep_roots` | Roots expiry never takes; roots GC always reads | both | 4 | K (§11.6, H17) |
| `retire_after` | When a superseded or withdrawn entry is retired | release | 3 | "past the window" (§6.4 table; S21) |
| `upload_grace` | The age below which GC deletes no `data/` file | GC | 4 | the upload grace period (§11.6) |
| `pod_staleness` | Nothing directly; checked against `inventory_window` | both | 2 | the longest pod poll interval plus fetch time (§11.6; S21) |

`RetentionPolicy.check()` refuses with `JobsConfigError(ValueError)`, naming both values, when any
of these fails (spec §11.6):

1. `backup <= gc_window <= tombstone_window`;
2. `inventory_window >= gc_window + 1d`;
3. `pod_staleness < inventory_window`;
4. `keep_roots >= 2`;
5. every duration is positive.

Only `keep_roots` (10) and `upload_grace` (24 h) have defaults, the spec's. Every window that
decides a deletion is written out. GC refuses to start on a failed check (spec §11.4); the release
job stops at step 0. Each run logs `jobs.config` with the file's SHA-256 (Q3).

| Option for the format | Gain | Cost |
|---|---|---|
| **TOML with unit strings (chosen).** | `tomllib` is in the standard library from Python 3.11; comments; one file reviewed in a PR; `"24h"` and `"35d"` in one key form. | A small duration parser. |
| TOML with unit-named integer keys (`gc_window_days = 35`). | No parser. | Two units in one file, and a key renamed when a unit changes. |
| Command-line flags. | Nothing to deploy. | Two jobs' flags drift apart, and the inequalities span both. |
| JSON. | No new parser. | No comments in a file meant for review. |

### D8: the access precondition

```python
class ReaderListing(Protocol):
    def readers(self) -> ReaderReport: ...

@frozen
class ReaderGrant:
    assignment_id: str; principal_id: str; principal_type: str; role: str; scope: str

@frozen
class ReaderReport:
    assignment_ids: frozenset[str]        # every assignment listed, any role
    grants: tuple[ReaderGrant, ...]       # those whose role reads blobs

def check_access(report: ReaderReport, allowlist: Collection[str], sentinels: Collection[str]) -> AccessResult: ...
```

**Permissions (Q4, owner, 2026-11-10).** The release identity holds one custom role, assigned at
the storage account and nowhere else, with exactly two actions:

```json
{"Name": "statcat access check", "IsCustom": true,
 "Actions": ["Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleDefinitions/read"],
 "NotActions": [], "DataActions": [], "NotDataActions": [],
 "AssignableScopes": ["/subscriptions/…/resourceGroups/…/providers/Microsoft.Storage/storageAccounts/acct"]}
```

It holds no Reader at the management group or subscription, no `Microsoft.Storage` read, and no
Microsoft Graph permission. The deployment creates the role and the assignment; the runbook
gives the JSON.

**The listing.** `AzureReaderListing(storage_account, container, *, arm_token, scopes=())` in
`access.py`, through `httpx`; the command line passes the token callable for
`https://management.azure.com/.default` (D15):

1. `GET {container scope}/providers/Microsoft.Authorization/roleAssignments?$filter=atScope()&api-version=2022-04-01`,
   where the container scope is
   `{storage_account}/blobServices/default/containers/{container}`. It returns the assignments at
   the container and, by A1 below, every assignment inherited from the account, the
   resource group, the subscription and the management groups. Every `nextLink` on
   `https://management.azure.com` is followed. A `nextLink` on another host fails the listing, so
   the bearer token is sent to Azure Resource Manager alone.
2. For each distinct role definition,
   `GET {storage_account}/providers/Microsoft.Authorization/roleDefinitions/{id}?api-version=2022-04-01`,
   and read `permissions[].dataActions` and `notDataActions`. A role reads blobs when a data
   action matches `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read`, with `*`
   matching any run of characters and case ignored, and no not data action does. `reads_blobs`
   judges each permission block by itself, as Azure does, and the role reads blobs when any block
   grants the action: a `notDataActions` entry withholds only what its own block grants. Judged
   over the whole role, one block's exclusion would hide another block's grant. The built-in
   Storage Blob Data Reader, Contributor and Owner match by this rule, as does any custom role
   with the blob read data action.

**The comparison.** `check_access` passes when the principal object id of every kept assignment
is on the allowlist, and every assignment id in `[access] sentinels` is in the listing. A group
principal is one principal: its object id must be on the allowlist, and its members are not
read. Any principal not on the list stops the release, as O6 decided. Allowlisted principals that
hold no blob-read role are reported as information. A request answered 429 or 5xx is retried
three times with backoff; a 403, any other error, one that persists, or a missing sentinel fails
the check. A listing that cannot be made raises `AccessListingError`, the one error the job
catches from a `ReaderListing`: it becomes a failed `AccessResult` with the message in `errors`.
Any other error from the listing, a credential error included, stops the run (D1). A failed
check skips the release in step 5, logs `access.mismatch` or `access.unchecked`, and leaves the
fold and retirement to run, since neither makes anything public.

**The catalog's own container.** `ReleaseJob` raises `JobsConfigError` when `[access] container`
is another than the container the catalog's `az://` URI names, or `[access] storage_account`
names another account than the store's `account_name`, compared without regard to case. A check
of another container would pass or fail on assignments that say nothing about the catalog's.

**Known limits.** Each is outside the check, and the deployment's owner reviews it elsewhere.

- Group membership. An allowlisted group can gain a member; the check sees only the group's
  object id.
- PIM-eligible assignments. `roleAssignments/read` lists active assignments only, and eligible
  ones need `roleEligibilityScheduleInstances/read`, which the role does not hold. An eligible
  principal becomes visible once it activates, and the next run's check stops the release.
- Shared key and anonymous public access. The role reads no account property, so
  `allowSharedKeyAccess` and `allowBlobPublicAccess` are set to `false` when the account is
  created and are part of the same review; §10.1 allows no keys.
- POSIX ACLs on a hierarchical-namespace account, which grant reads with no role; §10.1 counts
  roles.

**Assumption A1, verified on 2026-11-12 except for management groups.** That
`roleAssignments/read` and `roleDefinitions/read`, granted at the storage account scope, list the
assignments inherited from the resource group, the subscription and the management groups, and
read the definitions of the roles they name, built-in and custom. The owner ran the real-account
check item (handoff row 6a) on the GH-134 account. An identity holding only the two-action custom
role at the storage account received all six assignments that apply to the container: one at the
subscription, one at the resource group, three at the account and one at the container. The
subscription Owner's `az role assignment list --include-inherited` returned the same six ids. A
role definition read answered 200 at
`{account scope}/providers/Microsoft.Authorization/roleDefinitions/{guid}`, the path step 2 uses,
and at the `roleDefinitionId` the assignment carries. The tenant has no management-group
assignment, so inheritance from a management group is untested. The sentinels make the check
fail closed wherever A1 does not hold, the management groups included: the deployment names one
known assignment at each scope above the account (any role, such as the subscription owner's),
and a listing that lacks one fails with `access.unchecked`. If a deployment finds that A1
fails, every automatic release stops, and the operator then does one of two things: assigns the
same two-action custom role at each scope above the account that holds assignments (resource
group, subscription), which keeps the two read actions and widens only their scope, and lists
those scopes in `[access] scopes` so the job lists at each; or keeps releases manual with
`statcat admin release` until the role is placed. The design does not fall back to Reader or
Graph.

Tests pass a fake listing for the job, and `tests/test_access_listing.py` runs
`AzureReaderListing` against `pytest-httpx` with one row per rule, since Azurite has no role
assignments (plan).

| Option | Gain | Cost |
|---|---|---|
| **Two-action custom role at the account, groups as principals (chosen, Q4).** | No directory read and no tenant-wide role; the identity can read only who has roles on the account's scopes. | Group membership and PIM-eligible assignments are outside the check; inherited visibility rests on A1. |
| Reader at the root management group and Graph `GroupMember.Read.All` (revision 2). | Expands groups and sees eligible assignments. | Tenant-wide read of role assignments and directory membership for a job that runs every release. |
| `azure-mgmt-authorization` in place of `httpx`. | Paging handled. | A large dependency in `blob` for two requests. |

### D9: retirement

Step 6 retires the `superseded` and `withdrawn` entries whose `state_changed_at` is older than
`retire_after` by the storage clock, except the structure editions that a live or embargoed data
edition's `partial_of` cites. `_prune_retire`'s change builder moves with `_retirable`,
`_NothingToRetireError` and `_cited`'s fallback (every structure edition kept when a manifest
cannot be read, `admin.py:668-682`) into `store/inventory.py` as one function:

```python
def retire_change(full: FullInventory | None, cutoff: datetime, *, now: datetime,
                  skipped: Collection[str] = ()) -> Change | None
```

`CatalogAdmin._prune_retire` (`admin.py:617-666`) and the release job both call it inside their
`commit` callable, which commits one version of kind `retire` with the head's tombstones plus the
new ones; `None` writes nothing. The release job is then the only writer on a server (plan,
Retirement); `prune` keeps its local path. This is where the Phase 2a design's P7 ("keep roots,
leaves and tombstones") ends on a server for entries; for roots and leaves it ends with expiry
(Part 6, plan line 358).

| Option | Gain | Cost |
|---|---|---|
| **The release run retires (chosen).** | One writer on a server, as the plan states. | The release identity decides the rollback reach. |
| GC retires. | GC already reads the window. | GC would write the Inventory, which §10.1 does not allow. |

### D10: `statcat gc`

```text
statcat gc AZ_URL --config FILE [--dry-run] [--forecast FILE] [--discard PRODUCT/EDITION ...]
```

GC runs under the GC identity: read on `catalog.json`, `inventory/` and `CURRENT`, read, list and
delete on `products/` and `pending/` (S24). It writes no Inventory version, and touches nothing
under `inventory/` or `history/`.

1. **Start.** Load the configuration; refuse to start when `RetentionPolicy.check()` fails. Read
   the storage clock: `cutoff = now - gc_window`.
2. **The Inventory, back through the window.** Read the head by the reader protocol, then each
   older root by `seq` until both hold: `keep_roots` roots are read, and the newest root whose
   `created_at` is before `cutoff` is read. On an opted-in catalog stop at
   `HISTORY.expired_through`, and fail closed, deleting nothing, on a missing root above it (spec
   §11.4). Each distinct leaf is read once. The kept editions are every entry of every leaf read,
   and every tombstone in those roots whose `retired_at` is at or after `cutoff`, each with the
   `manifest_sha256` its newest entry recorded.
3. **The registrations,** listed after the Inventory is read, so a registration folded meanwhile
   is still protected. Each is decoded as the fold decodes it.
4. **Manifests.** GC reads the manifest of each kept and each registered edition and lists its
   files with `edition_files`. A kept edition's manifest must match its SHA-256; a registered
   edition's must decode and name its path. A failure marks the product failed closed: GC
   deletes nothing under its `data/`, and logs `gc.failed_closed` naming the edition (O5).
5. **LIST `products/` and `pending/`,** and plan:
    - `products/<p>/<e>/…` for an edition tombstoned before `cutoff` and not kept: delete
      `manifest.json` first, then the rest, so a partly deleted directory never reads as an
      edition. A later run finishes a partial deletion.
    - `products/<p>/data/<file>` listed by no kept or registered manifest, last modified before
      `now - upload_grace`, in a product not failed closed: delete.
    - `pending/<p>/<e>.json` whose edition the head records, in a leaf or in `retired`, last
      modified before `cutoff`: delete (O1). With Q6, also a registration whose `edition_id` time
      is older than `tombstone_window` (S20).
    - An edition directory that no Inventory version records and no registration names, older
      than `upload_grace`, with or without a manifest: log `gc.unregistered`, delete nothing (O5).
6. **Delete,** on a pool of 16 threads, writing the deletion log (below). A failed deletion is
   logged and left for the next run.

**`--discard p/e`**, an operator's decision (spec §11.6, S22). For an edition the head does not
record, GC deletes its registration if any, then its manifest, then the rest of its directory, as
logged deletions; its `data/` files then fall to the normal rule. For an edition the head
records, `--discard` refuses and names `statcat admin cancel`. This is how a `pending-fork`
(CUJ 3), or any registration refused for `files`, `manifest`, `chain`, `final` or `lifecycle`,
leaves `pending/`.

`--dry-run` stops after step 5 and writes the plan. `--forecast FILE` takes an earlier dry run's
plan and deletes only paths in both plans, logging `gc.forecast_deviation` for each path in one
plan only: the spec's "alerts on a deviation from the dry run's forecast". Without it, a run
deletes its own plan and the deviation alert covers failed deletions only.

**The deletion log.** Standard output, one JSON line each:

| Field | Meaning |
|---|---|
| `event` | `gc.planned`, `gc.deleted` or `gc.delete_failed` |
| `path` | The object, from the catalog root |
| `reason` | `retired`, `unreferenced`, `folded`, `expired-registration` or `discarded` |
| `edition` | `<product>/<edition>` for a directory or registration, else `null` |
| `error` | The error for `gc.delete_failed`, else absent |

A plan file is the `gc.planned` lines. The other events (D14) share the stream; the summary goes
to standard error. **Exit codes:** 0, the plan ran (failed-closed products and unregistered
editions are reported, and do not change the code); 1, a deletion failed or the run stopped; 2,
usage or a store other than `AzureStore`.

**Ordering against the other jobs.** GC reads the Inventory, then the registrations, then the
store. A fold between the first two reads leaves the registration listed, which protects its
files. An upload's data files precede its registration by less than the grace period (O7). A
retire after GC's read leaves the edition kept by a stale head, the safe direction. A restore
removes roots from the head chain, so their editions are kept only through S25's tombstones;
until GH-125's restore writes them, a restore drill keeps GC stopped (Risks).

| Option for the kept set | Gain | Cost |
|---|---|---|
| **Read back through the window and the newest K roots (chosen).** | The spec's two rules (§11.4, §11.6). | About 1.6 MB a root at 7,000 products: at 10 versions a day and a 35-day window, about 560 MB a run. |
| The head's leaves and tombstones only. | One root. | Misses editions an older root in the window names, and the reader fallback's reach. |

| Option for the deletion log | Gain | Cost |
|---|---|---|
| **JSON lines on standard output (chosen).** | GC needs no write right; the platform's log store keeps it. | Lost if the platform keeps no pod logs (S22). |
| An object under `products/` or a new prefix. | Kept with the catalog. | GC gains a write right §10.1 does not give it. |

### D11: `CatalogAdmin` over a blob catalog

- `CatalogAdmin.open(path | BlobLocation, *, actor=None, clock=None)` builds
  `BlobInventoryStorage(location, actor=actor, clock=clock)` for a location. Its changes take `now`
  from `storage.now()` once per change, on disk and on blob, which on disk is the local clock (P1).
  `__repr__` shows `storage.location`. The writer's rules and the leaf records come from D6.
- `prune` on a blob catalog raises `TypeError` naming `statcat gc` and the release job.
- `InventoryWriter.advance_current() -> int | None` reads the head by the reader protocol, which
  probes forward, and moves `CURRENT` to it with `current_is_older`, forward only, across a
  restore epoch as that rule allows. It returns the new `seq`, or `None` when `CURRENT` was
  current. `CatalogAdmin.advance_current()` calls it.
- `commit(change)` writes nothing and returns `None` when `change` returns `None`, and `Change`
  gains `now` (D4).

The command line gains, under the release identity (S29):

```text
statcat advance-current URL
statcat admin editions URL PRODUCT
statcat admin release URL PRODUCT EDITION --reason TEXT
statcat admin cancel URL PRODUCT EDITION --reason TEXT
statcat admin rollback URL PRODUCT [--to EDITION] [--if-live EDITION] --reason TEXT
statcat admin embargo URL on|off --reason TEXT
```

`URL` is a local directory or `az://`, with `statcat upload`'s environment rules. The operator
runs the commands from a pod that holds the release identity (a `kubectl run` from the release
CronJob's template, in the runbook) and names themself in `--reason`. `rollback` is in Phase 5
because a bad release on a server has no other remedy; the rest are the runbook's actions for
D14's alerts. Exit codes follow D1's 0, 1 and 2. The lease wait on an opted-in catalog is Part 6's
(D13).

### D12: `EmbargoReader`

```python
class EmbargoReader:
    @classmethod
    def open(cls, location: str | Path | BlobLocation) -> Self: ...
    def refresh(self) -> Self: ...
    def editions(self, *, states: Collection[State] = ("embargoed",)) -> list[EditionEntry]: ...
    def view(self, product_id: str, edition_id: str, *, embedding_model=None, query_prefix: str = "") -> EditionView: ...
    def close(self) -> None: ...
```

It lives in `embargo.py`, is exported from `statcatlib`, imports nothing from the `blob` extra at
module level, and holds a `FullInventory` and an `EditionEngine` of its own.

- `view` accepts an `embargoed` or `live` entry and raises `ValueError` for another state, so the
  preview can compare a release with what it replaces. A product it does not record raises
  `InventoryRuleError(rule="unknown")`.
- `view` and `Product.view` share one function taken out of `Product.view`
  (`catalog.py:1018-1027`), `_open_view(engine, edition_dir, *, manifest_sha256, stamps, remote,
  references, ...) -> EditionView`, the only other place an `EditionView` is built. On blob it runs
  `Product.view`'s steps: the manifest first, `pull_edition` into `<cache_dir>/<catalog_id>/`, then
  `check_on_open` with `stamps=<cache_dir>/<catalog_id>/embargo-verified` and
  `references=False` when the manifest has no bindings (Phase 4 W15).
- No attribute of `Catalog`, `Product` or `EditionView` returns an `EmbargoReader` (spec §6.4).
- Its `cache_dir` holds embargoed bytes and their stamps, so the preview deployment and
  pre-materialisation each use their own, inside the §10.1 boundary, and never share one with a
  public reader.

### D13: server expiry (GH-125 part 3, server half; provisional)

Part 6 runs GH-125's expiry engine from step 7 of an `--expire` run. GH-125's stage 2 design
defines the engine and its interface, and Part 6 is re-reviewed against it (handoff row 11, Q9).
These are the release job's requirements on it, and the Azure pieces Part 6 adds:

| Piece | Where | What it does |
|---|---|---|
| `AzureHistoryLease` | `store/blob.py` | `acquire()`, `renew()`, `release()`, `check()`. Acquires a 60 s lease on `inventory/HISTORY` (`x-ms-lease-action: acquire`), renews it every 20 s on a thread, and raises `LeaseLostError` from `check()` when the last renewal is 40 s old or more. The 30-minute cap counts from `acquire()`: at the cap it stops expiry at the next `check()`, releases, and logs `expiry.stopped`; steps 4 to 6 of a run take seconds and finish first. It implements GH-125's `exclusive()` for `BlobInventoryStorage` on an opted-in catalog. |
| Lease on writes | `BlobInventoryStorage(location, *, lease=None)` | The release run passes its held lease; otherwise each `try_commit` acquires one, waiting up to 120 s, then raising `InventoryWriteRefusedError` naming the break runbook (S26). `HISTORY` writes carry `x-ms-lease-id` and `If-Match`. |
| REST verbs | `AzureBlobRest.lease(path, action, *, lease_id=None)`, `.delete(path, *, if_match)`, `.put(path, data, *, if_match, lease_id)`, `.get_range(path, length)` | The lease, step 6's `If-Match` leaf delete, the `HISTORY` write, and step 1's 1 KiB ranged read of a root's `created_at`, which `obstore` cannot send (Phase 4 F14). `_send(method, path, headers)` replaces today's `HEAD`-only `_send(path)`, so the account-key test subclass signs every verb. |
| LIST | `_ObjectOps.list(prefix) -> Iterator[ObjectMeta]` (Part 2) | Pages of `obstore.list` under the store's prefix; step 3's LIST of `history/` and step 6's of `inventory/*.json`. |
| Gate 1 | `[expiry] not_before` | No expiry before it (H20). |
| Gate 2 | `PodVersions` protocol, `versions() -> PodReport`; `KubernetesPodVersions(api, token, namespace, selectors, port)` | Lists pods of each selector through the in-cluster API, not-Ready pods included, and reads `sdmxlib_version` from `GET http://<pod IP>:<port>/`. A pod with no IP, an unreachable pod, a missing field, or a version that `packaging.version` parses below 0.85.0 fails the gate. The command line builds it from the service-account token file; with no token file the run logs `expiry.pod_check_not_run` and gate 1 alone applies; a 403 from the API fails the gate. |
| Leaf-set cache | `<state-dir>/expiry-cache.json` | Each root's path, ETag, SHA-256 and leaf hashes, so a run reads only roots written since the last (S23). No edition id reaches it. A missing file costs one full read. |
| Opt-in on Azure | `CatalogAdmin.opt_in(reason)` over a `BlobLocation` | GH-125 part 2's opt-in, with `HISTORY` created only if absent and `catalog.json` rewritten by its `e_tag`. Refused on any store but `AzureStore` (H21). |

The engine interface Part 6 needs, which GH-125's design names: `exclusive()` and `check()` on the
storage, and an expiry storage with `list(prefix)`, `read(path) -> (bytes, etag)`,
`read_range(path, length)`, `create(path, data)`, `delete(path, *, if_match=None)` and
`write_history(data, *, etag)`. Part 6 implements it as `BlobExpiryStorage` in `store/blob.py`.

A failed gate skips expiry, logs `expiry.skipped`, and leaves the run's other steps done (spec
§6.9).

### D14: alerts

Each alert is one JSON line on standard output:

| Field | Meaning |
|---|---|
| `event` | The name below |
| `severity` | `info`, `warning` or `error` |
| `at` | The storage time |
| `catalog` | The location's URI |
| `product`, `edition` | When the event names one |
| `rule`, `reason` | For a refusal |
| `detail` | Any other fields, an object |

| Spec §11.6 alert | Event | Raised by |
|---|---|---|
| Embargoed editions past `release_at`, with the persisted reasons | `release.refused`, `release.overdue` | release |
| Sealed but unrecorded editions; an uploaded edition with no registration | `fold.refused`; `gc.unregistered` | release; GC |
| `CURRENT` behind the newest root | `current.behind`, then advanced | release |
| GC deletions off the forecast | `gc.forecast_deviation`, `gc.delete_failed` | GC |
| Expiry skipped, stopped, orphan segment, kept root | `expiry.skipped`, `expiry.pod_check_not_run`, `expiry.stopped`, `expiry.orphan_segment`, `expiry.kept_root` | release |
| Old `pending/` entries | `fold.refused` with `too-old` | release |
| A registration whose manifest GC cannot read | `gc.failed_closed` | GC |
| A failed access precondition | `access.mismatch`, `access.unchecked` | release |
| Storage 503s | `step.failed`, with the `BlobStoreError` | both |
| (not in §11.6) | `release.held`, `release.unprepared`, `fold.release_at_past`, `fold.restored`, `jobs.config`, `step.failed` | release; both for `jobs.config` |

The orchestrator raises "the release job missed or failed" from the exit code and the schedule. A
root that fails validation is skipped inside sdmxlib's reader, which logs it, and pod staleness,
backup age, replication lag and stale build-node working copies are outside the jobs (Phases 6, 8
and 9).

### D15: clients and the environment

The command line is the only layer that reads the environment or a credential, as for `statcat
upload` (`src/statcatlib/EGM.md:386-388`). For `statcat release` and `statcat gc` it builds:

- the store, through a helper generalised from `_azure_location` and `_refused_variables`
  (`cli/__init__.py:635-694`) that takes the command's name for its messages and a request timeout
  (one hour for upload, five minutes for the jobs);
- `AzureBlobRest` over the store's `TokenIdentity`;
- for `release`, `AzureReaderListing` with a token callable from the same `DefaultAzureCredential`
  for `https://management.azure.com/.default`, and no Graph token (Q4);
- for `release --expire`, `KubernetesPodVersions` from
  `/var/run/secrets/kubernetes.io/serviceaccount/` and `KUBERNETES_SERVICE_HOST`.

The library classes take each as an argument, plus `storage=` and `clock=` seams for tests:

```python
ReleaseJob(location, config, *, rest, listing, storage=None)                                 # Part 3
ReleaseJob(location, config, *, rest, listing, pods=None, storage=None, state_dir=None)      # from Part 6
collect_garbage(location, policy, *, storage=None, dry_run=False, forecast=None, discard=()) -> GcReport
```

`storage` defaults to `BlobInventoryStorage(location)`; a test passes one with `clock=` and
`actor=`.

### Errors

| Site | Condition | Raises or reports |
|---|---|---|
| `load_jobs_config` | Unknown key, bad duration, missing `format_version` or section, failed inequality | `JobsConfigError(ValueError)`, exit 1 |
| `statcat release`, `statcat gc` | A store other than `AzureStore` | `TypeError`, naming Phase 4 W3, exit 2 |
| Step 0 | Clock ahead by more than 30 s, or no `Date` | `InventoryWriteRefusedError`, exit 1 |
| Step 0, before Part 6 | Opted-in catalog | `InventoryWriteRefusedError` naming Part 6, exit 1 |
| `ReleaseJob` | No `[access]`, or one whose `container` or `storage_account` names another container or account than the catalog's | `JobsConfigError`, exit 1 |
| Step 1 | Lease held by another run | exit 1, nothing written |
| Fold, per registration | Any D2 check, a store fault | a refusal |
| Fold, reference check | A structure pull fails | the registration's `references` refusal |
| Fold, reference check | The check was given a fold edition that ends up unrecorded | the product's registrations refused with `references` |
| Fold and release, per product | `ManifestError` or `BlobStoreError` on an incoming entry's manifest | the product dropped with `manifest` or `store` |
| Release, per product | `release_due`, item 2, cross-product rules | a refusal |
| Steps 2 and 4 to 6 | `ManifestError` from an unfilled entry, `BlobStoreError`, `InventoryContentionError`, `InventoryCommitUnknownError` | `step.failed`; the run goes on; exit 1 |
| Access | Mismatch, or `AccessListingError` from the listing: a failed request, an answer that is not the documented JSON, a `nextLink` off Azure Resource Manager | the release skipped |
| Access | Any other error from the listing, a credential error included | the run stops, exit 1 |
| Expiry | Lease lost, cap reached, gate failed | expiry stops or skips |
| GC | Missing root above `expired_through` | deletes nothing, `GcStoppedError`, exit 1 |
| GC, per product | Unreadable or unverified manifest | failed closed |
| GC `--discard` | A recorded edition | `ValueError` naming `statcat admin cancel`, exit 2 |
| `CatalogAdmin.prune` | Blob catalog | `TypeError` |
| `EmbargoReader.view` | Entry not `embargoed` or `live` | `ValueError` |
| `BlobInventoryStorage` on an opted-in catalog | Lease not taken in 120 s | `InventoryWriteRefusedError` (Part 6) |

`JobsConfigError`, `GcStoppedError`, `AccessListingError` and `LeaseLostError` are new.

### Symbols

| Symbol | Module | Signature | Part |
|---|---|---|---|
| `_records` | `store/cross_product.py` | `(product_id, entry, artefacts, *, incoming) -> Mapping[str, PartialRecord]` | 1 |
| `ManifestArtefacts` | `store/cross_product.py` | `(source: Path \| Fetch)` | 1 |
| `catalog_section` | `bundle.py` | gains `lifecycle: Lifecycle` | 1 |
| `fold_lifecycle` | `store/working_copy.py` | `(leaf: Leaf \| None, manifest: Manifest) -> Lifecycle`, raising `InventoryRuleError(rule="lifecycle")` | 1 |
| `InventoryWriter.advance_current`, `CatalogAdmin.advance_current` | `store/inventory.py`, `admin.py` | `() -> int \| None` | 2 |
| `InventoryWriter.commit` | `store/inventory.py` | `change` may return `None`; returns `Root \| None` | 2 |
| `Change.now` | `store/inventory.py` | `datetime \| None = None` | 2 |
| `CatalogAdmin.open` | `admin.py` | `(path: str \| Path \| BlobLocation, *, actor=None, clock=None)` | 2 |
| `retire_change` | `store/inventory.py` | D9 | 2 |
| `_ObjectOps.list` | `store/blob.py` | `(prefix: str) -> Iterator[ObjectMeta]` | 2 |
| `JobsConfig`, `RetentionPolicy`, `AccessPolicy`, `ExpiryPolicy`, `load_jobs_config(path) -> JobsConfig`, `JobsConfigError` | `jobs.py` | D7 | 2 |
| `decode_registration` | `jobs.py` | `(path: str, raw: bytes) -> Registration`, raising `ValueError`; shared by the fold and GC | 3 or 4, whichever lands first |
| `ReleaseJob`, `ReleaseReport`, `ReleaseRefusal` | `release.py` | D15; `run(*, dry_run=False) -> ReleaseReport`; `expire=` from Part 6 | 3 |
| `ReleaseRefusal` | `release.py` | frozen: `product_id`, `edition_id`, `step`, `rule \| None`, `reason`, `since` | 3 |
| `Step` | `release.py` | `Literal["fold", "release"]`: a `ReleaseRefusal.step` | 3 |
| `ReleaseReport` | `release.py` | frozen: `folded`, `released`, `retired` (each `<product>/<edition>` tuples), `refused` (`ReleaseRefusal` tuple), `skipped` (step to reason), `failed` (step to error), `versions` (seqs written), `access` (the `AccessResult`), `dry_run` | 3 |
| `refusal_record`, `current_record`, `current_refusals` | `release.py` | D5; `refusal_record(full, refusals, now, *, epoch=(0, None))` returns the next root's `extensions`; `current_record(full)` is the head's; `current_refusals(full) -> tuple[ReleaseRefusal, ...]` reads the head's record | 3 |
| `RELEASE_KEY`, `MAX_REFUSALS` | `release.py` | `"release"`, the record's key in `extensions.statcat`; 500, the cap | 3 |
| `drop_until_stable`, `Dropped` | `release.py` | D4, D2; `(writer, full, leaves) -> (kept, dropped)`; `Dropped(edition_id, rule, reason)` per dropped product | 3 |
| `structure_entries`, `StructureEntry` | `release.py` | D2; `(full, *, product_id, batch) -> list[StructureEntry]`; `StructureEntry(product_id, edition_id, manifest_sha256)` | 3 |
| `ReleaseJob._check_structures` | `release.py` | D2; a method, since it reads the run's pulled editions and kept answers | 3 |
| `ReaderListing`, `ReaderGrant`, `ReaderReport`, `AzureReaderListing`, `check_access`, `AccessResult` | `access.py` | D8; `AzureReaderListing(storage_account, container, *, arm_token, scopes=())`; `AccessResult(passed, outsiders, unused, missing_sentinels, errors)` | 3 |
| `AccessListingError`, `reads_blobs`, `BLOB_READ`, `ARM_SCOPE` | `access.py` | D8; `AccessListingError(RuntimeError)`; `reads_blobs(permissions) -> bool`; the blob read data action; the token scope `https://management.azure.com/.default` | 3 |
| `collect_garbage`, `GcReport`, `GcStoppedError` | `gc.py` | D15; `GcReport(planned, deleted, failed, failed_closed, unregistered, deviations, dry_run)` | 4 |
| `EmbargoReader`, `_open_view` | `embargo.py`, `catalog.py` | D12 | 5 |
| `AzureHistoryLease`, `LeaseLostError`, `BlobExpiryStorage` | `store/blob.py` | D13 | 6 |
| `AzureBlobRest.lease`, `.delete`, `.put`, `.get_range`, `_send(method, path, headers)` | `store/blob.py` | D13 | 6 |
| `PodVersions`, `PodReport`, `KubernetesPodVersions` | `release.py` | D13 | 6 |

No part adds `Any` to `src/`; `tools/count_any.py` stays at its baseline.

## GH-125's parts

| GH-125 part | Where it is built |
|---|---|
| 1. History fields (stage 1) | Done in statcatlib 0.14.0. |
| 2. The catalog lock and opt-in | GH-125: `flock`, the stale-base refusal in `try_commit`, local opt-in, the writer's stage-2 `catalog.json` decode, the `exclusive()` protocol method. This design's Part 6: the Azure lease behind `exclusive()`, the lease on blob writes, and opt-in over a `BlobLocation`. |
| 3. Expiry, restores and reporting | GH-125: the engine (steps 0 to 8, segments, crash rules), restores (with S25's tombstones), `verify-catalog` and `doctor` reporting, run through `prune`. This design's Part 6: the engine on Azure from an `--expire` run, with the lease, both gates, the persisted leaf-set cache, `If-Match` leaf deletes and the ranged read. |
| 4. `LeafEntry.artefacts` | This design's Part 1, whole, with D6's change to the reader. |

GH-125's acceptance items split the same way. Part 1 takes "every entry recorded after Phase 5
carries `artefacts` equal to its manifest's records, a reinstated entry included, and the
cross-product rules on a seal open no manifest of another product's filled entry"; the reinstated
case is tested through sdmxlib's `reinstate` until GH-130 calls it. Part 6 takes the second
release run that fails to take the lease, the failed renewal, the `If-Match` survivor and the pod
check. The rest stay with GH-125.

## Alternatives Considered

Each decision above carries its own table. Three wider choices:

*   **Run the fold inside `statcat upload`.** The publish job would record its own edition. §10.1
    gives it no access to `inventory/`, and the publish identity would then write the Inventory,
    which the spec gives to the release job alone.
*   **Make GC a step of the release run.** One job. GC's delete on `products/` would join the
    Inventory writer's identity, and §10.1 separates them so a fault in one cannot delete what the
    other recorded.
*   **Build an index of artefact records outside the leaves.** The owner chose leaves on
    2026-11-04 (O3), and the 64-thread leaf read makes a separate index unnecessary at 7,000
    products.

**The public surface, against the gating tests.** Every command and flag has a caller:

| Surface | Why Phase 5 needs it |
|---|---|
| `statcat release`, `--config`, `--dry-run` | the plan's release and fold bullets and gating tests |
| `--expire`, `--state-dir` | Part 6, GH-125's server acceptance; S26's short release-time runs |
| `statcat gc`, `--dry-run` | the plan's GC bullet and gating tests |
| `--forecast` | spec §11.4's deviation alert |
| `--discard` | the only way a refused registration leaves `pending/` (B1, CUJ 3) |
| `statcat advance-current` | a plan gating test |
| `statcat admin release`, `cancel`, `embargo`, `editions` | §11.6's runbooks: the manual release with a reason, CUJ 2, Q1's stop |
| `statcat admin rollback` | the only remedy for a bad release on a server |

Revision 1's `--report` is cut: standard output carries the events.

## Non-Functional Requirements

Request counts at 7,000 products, at an assumed 10 ms a request in sequence, with leaves read on
the reader's 64-thread pool (O8):

| Operation | Reads | Writes | Time, estimated |
|---|---|---|---|
| Steps 0 to 2 | `catalog.json`, `CURRENT`, head root, probe, `Date` `HEAD` | `CURRENT` when behind | 50 ms |
| Fold of 200 one-table registrations | 7,003 leaves (pool), LIST `pending/`, 200 registrations, 200 manifests, about 3,000 file `HEAD`s, and per no-binding edition its own files and every structure product's editions | 200 leaves, root, `CURRENT` | 1 s + 34 s in sequence, plus the structure pulls when a structure edition folds |
| Release of 200 | 7,003 leaves (pool), 200 manifests (item 2) | 200 leaves, root, `CURRENT` | 1 s + 4 s |
| Retire, a typical day | the step's own `leaves()` read | a few leaves, root, `CURRENT` | about 1 s |
| Access check | one role-assignment listing and its pages, one role-definition read per distinct role | none | under 1 s |
| GC, 35-day window at 10 versions a day, 200 releases a day, 90-day `retire_after` | about 350 roots (560 MB), distinct leaves, about 21,000 manifests (300 MB), LIST of about 400,000 objects | the planned deletes | 3 to 6 minutes on 16 threads |

Each step reads the leaves once, on the pool; a lost race reads them again, since `commit` builds
a new `FullInventory` per attempt (`store/inventory.py:742-744`). The fold's sequential reads fit
inside the 30 minutes between the pre-flight and the release. The release step's time is what
readers wait for after the release instant. Part 3's stores test asserts the pool by counting
concurrent fetches and records the counts; the first real release records the times.

*   **Atomicity.** Each step's change is one root; a crash leaves the head as before the step.
*   **Idempotency.** A re-run writes nothing when the catalog has not changed: the fold skips
    recorded editions, `release_due` finds nothing due, retire finds nothing old, the refusal
    record is equal, and GC's plan is empty.
*   **Memory.** The release job holds one head's leaves (about 20 MB filled) and the fold's reads
    (200 manifests, about 3 MB). GC holds the kept set's leaves and manifests' file lists, about
    300 MB at the figures above; a larger catalog streams manifests per product.
*   **Disk.** The fold's temporary directory holds each folded no-binding edition and every
    structure product's live and embargoed editions while a structure edition folds, bounded by
    the structure products' size (three products in the StatCan split, plan Q2). `--state-dir`
    holds the leaf-set cache, about 80 bytes a leaf hash per root, a few hundred MB at 2,979
    roots. `EmbargoReader`'s `cache_dir` grows by each edition viewed and is never evicted in
    Phase 5, as Phase 4's cache is.
*   **Security.** No key or SAS. The release job holds embargoed structure editions in a
    temporary directory for the reference check, inside the §10.1 boundary, and deletes them. The
    leaf-set cache holds hashes only.
*   **Compatibility.** No Inventory or manifest format change. The `statcat` section gains an
    optional key; the root's `statcat` section gains `release`. Older readers ignore both.
*   **Coverage.** `just coverage` runs in each part's PR.

| Module | Unit tier covers | Only `stores` covers |
|---|---|---|
| `store/cross_product.py`, `store/working_copy.py` (Part 1) | every read site, with a recording reader | `ManifestArtefacts(fetch)` over Azurite |
| `jobs.py` | everything | none |
| `release.py` | every step over `MemoryStore`, with fakes for `rest`, the listing, the pods and the clock | the 200-table release, the reference check's pulls |
| `access.py` | `AzureReaderListing` through `pytest-httpx`, and `check_access` | none |
| `gc.py` | every rule over `MemoryStore` with a clock | the independent reachability computation |
| `embargo.py` | refusals, local catalogs | a blob view |
| `store/blob.py` lease, REST verbs | through `pytest-httpx` | the lease and `If-Match` on Azurite |

### Not confirmed

Nothing below has run against a real account or cluster. Each has a risk row and a step in the
Verification Plan.

- A1 for management groups: that the two-action custom role at the storage account lists an
  assignment inherited from a management group at the container scope. The real-account check
  of 2026-11-12 confirmed A1 for the resource group, the subscription and the role definitions
  (handoff row 6a, D8); the tenant has no management-group assignment to list. A sentinel at that
  scope fails the check closed if the listing lacks it (D8).
- That an in-cluster service account can list pods by label in the namespace, and that every
  Inventory-reading pod reports `sdmxlib_version` in `GET /`.
- That the lease, `If-Match` delete, lease-id put and ranged read work over `httpx` with a bearer
  token on real Azure (Azurite showed lease and conditional delete, Phase 4 F21).

### Risks and limits

| Risk | Mitigation |
|---|---|
| An ACL on a hierarchical-namespace account grants a read outside RBAC | §10.1 counts roles only. The runbook forbids ACL grants on the catalog's container; a later check can read the container's root ACL. |
| A1 fails for a management group, or on another tenant: the account-scoped role does not see inherited assignments | The real-account check of 2026-11-12 confirmed A1 for the resource group and the subscription (D8). The sentinels fail the check closed; the operator places the same two-action role at the higher scopes or releases by hand (D8). The first real run also compares the listing with the portal's "Check access" for the container. |
| A group gains a member, or a principal is eligible through PIM | Outside the check by the owner's decision (Q4); the owner reviews group membership and eligible assignments elsewhere. An eligible principal that activates is seen by the next run. |
| Shared key or anonymous access is turned on | Outside the check, since the role reads no account property; both are off at account creation and in the owner's review. |
| A listing permission is missing for good | Every automatic release stops with `access.unchecked`; manual releases still work. The runbook names the roles. |
| The release identity can read the role assignments on the account's scopes | Two read actions at one account, and no directory read (Q4). |
| The release identity gains pod-list rights for gate 2 | Read-only, one namespace, only for `--expire` runs. |
| A refusal record grows roots and segments | Capped at 500 entries; D5's cost paragraph. |
| A registration folded and due in one run misses pre-materialisation | `release.unprepared`; the first requests miss the result cache (Q5). |
| Editions sealed before 0.17 carry no `lifecycle` | A recorded product uses its leaf; a new one is refused with the fix (Q7). |
| A `final` product whose content must change | Refused with `final` until Q8's operation exists. |
| `--discard` names the wrong edition | It refuses a recorded edition, and every deletion is logged; an unrecorded edition's upload can be rerun. |
| A restore before S25's tombstones exist | The restore runbook stops GC until GH-125's restore writes them, and the fold holds re-armed registrations (D2). |
| GC's read of roots grows with the window | About 560 MB a run at 35 days; a cache like expiry's can follow if a measurement asks for it. |
| A deletion log lost with the pod | Platform log retention (S22). |
| The leaf-set cache's volume is lost | One full read of retained roots, 1.7 to 4.7 GB, then normal runs. |
| The filled leaf read misses plan Goal 1 on real storage | Handoff row 6 measured it on 2026-11-12: 0.94 to 1.54 s at 64 threads for 7,000 filled leaves (D6's cost). The baked summary stays the fallback for a store that reads slower (spec §6.5). |
| Concurrent release runs on a catalog that has not opted in | `concurrencyPolicy: Forbid`; the Inventory's exclusive create keeps the chain correct if two run. |
| `obstore` retries a timed-out create (Phase 4 F13) | The writer's unknown-outcome rule (Phase 4 D1) is unchanged. |

## Detailed Implementation Plan

### Land order

| Part | Content | sdmxlib | Waits on |
|---|---|---|---|
| 1 | Artefact records in every entry, the rules' reads, `ManifestArtefacts` over a fetch, the section's `lifecycle` | 0.87 | handoff row 1 (Q7) |
| 2 | `CatalogAdmin` on blob, `advance-current`, `statcat admin`, `commit`'s `None` and `Change.now`, `_ObjectOps.list`, `retire_change`, the jobs configuration | 0.87 | Part 1, handoff row 3 |
| 3 | The release job and the access check | 0.87 | Part 2, handoff rows 5 and 6 |
| 4 | GC | 0.87 | Part 2, handoff row 8 |
| 5 | `EmbargoReader` | 0.87 | Part 2 |
| 6 | Server expiry | GH-125 stage 2's | Part 3, handoff rows 11 and 12 |

Each part is a branch from `origin/main` and a PR, merged with a merge commit after `just ci`
passes. Each updates `docs/upgrading.md`, the `EGM.md` beside the code, `CLAUDE.md` where it names a
new module or command, and the docs listed per part with their nav entries and reference pages.
Each part's tests carry a `pytestmark`, and new files are marked Create. Parts 1 and 2 release in
order; Parts 3, 4 and 5 release in any order after Part 2; Part 6 releases last. The first PR
of each part uses `feat:`, and a docs-only follow-up `docs:`.

### Part 1: artefact records in leaves, and `lifecycle`

*   **`src/statcatlib/store/working_copy.py` (Modify):** `recorded_routing` returns `artefacts`
    for every kind; `fold_lifecycle`; the seal passes `lifecycle=` to `catalog_section`;
    `_may_be_refused` reads through `_records`.
*   **`src/statcatlib/store/cross_product.py` (Modify):** `_records`, at every read site;
    `ManifestArtefacts(Path | Fetch)`.
*   **`src/statcatlib/store/inventory.py` (Modify):** the default reader over any storage;
    `_no_manifest_reader` and the `NotImplementedError` go.
*   **`src/statcatlib/bundle.py` (Modify):** `catalog_section(..., lifecycle=)`, and
    `check_catalog`'s finding for another value.
*   **`tests/stores/_stores.py` (Modify):** `record_uploaded(lifecycle: Lifecycle | None = None)`
    uses `fold_lifecycle`.
*   **Existing tests and pages that state the Phase 4 behaviour (Modify):**
    `tests/test_blob_storage.py:719-735` (two tests expect `NotImplementedError` naming Phase 5);
    `tests/test_inventory_routing.py:113`, `:133`, `:160` (a data entry's `artefacts` is `None`);
    `docs/api/store.md:364-366`; `src/statcatlib/store/EGM.md`'s `inventory.py` and
    `cross_product.py` entries.
*   **`bench/corpus-results.md` (Modify):** a note that the cross-product meter after Part 1 reads
    leaves, so the recorded numbers describe the manifest path.
*   **Docs:** `docs/catalog-format.md` §3 and §3.1 (`lifecycle`), §3.3 (records from leaves).

| Test | File | Tier | Covers |
|---|---|---|---|
| A seal, a batched seal, a journal repair and `record_uploaded` each record `artefacts` equal to `manifest_records`, for a data and a structure edition | `tests/test_inventory_routing.py` | integration | GH-125 part 4 |
| A seal's cross-product rules, with a recording reader, read the incoming manifest, no manifest of another product's filled entry, and an unfilled one's | `tests/test_cross_product_rules.py` | unit | Goal 5 |
| `CatalogAdmin.release` of an entry whose manifest was altered or removed is refused with `ManifestError` | `tests/test_catalog_admin.py` | integration | Goal 2 |
| `sdmxlib.inventory.reinstate(artefacts=)` keeps the records | `tests/test_inventory_routing.py` | integration | GH-125 part 4 |
| `verify-catalog` reports a data entry whose `artefacts` differ from its manifest | `tests/test_cli_verify_doctor.py` | unit | GH-125 part 4 |
| A blob `commit` with an incoming entry runs the rules through `ManifestArtefacts(fetch)`; an absent manifest raises `ManifestError` | `tests/stores/test_blob_writer.py`, `tests/test_blob_storage.py` | stores, unit | Phase 4 D5 |
| The section records `lifecycle`; `check_catalog` refuses another value | `tests/test_bundle_manifest.py` | unit | D3 |
| `fold_lifecycle`'s five rows | `tests/test_fold_lifecycle.py` (Create) | unit | D3, Q7 |

### Part 2: `CatalogAdmin` on blob, and the jobs configuration

*   **`src/statcatlib/admin.py` (Modify):** `open(BlobLocation, *, actor, clock)`, one
    `storage.now()` per change, `__repr__`, `advance_current`, `prune`'s refusal on blob,
    `_prune_retire` through `retire_change`.
*   **`src/statcatlib/store/inventory.py` (Modify):** `advance_current`, `retire_change`,
    `commit`'s `None`, `Change.now`.
*   **`src/statcatlib/store/blob.py` (Modify):** `_ObjectOps.list`, with the recording and refusing
    test ops classes in `tests/stores/_stores.py` covering it.
*   **`src/statcatlib/jobs.py` (Create):** D7.
*   **`src/statcatlib/cli/__init__.py` (Modify):** `advance-current`, the `admin` group, the
    generalised store helper (D15); `src/statcatlib/cli/EGM.md`.
*   **`tests/test_blob_open.py:170-172` (Modify):** `CatalogAdmin.open` now accepts a
    `BlobLocation`.
*   **Docs:** `docs/api/admin.md` gains blob catalogs and `statcat admin`; `docs/api/jobs.md`
    (Create) holds the configuration's reference; `docs/reference/jobs.md` (Create); nav entries.

| Test | File | Tier | Covers |
|---|---|---|---|
| `advance-current` moves a lagging `CURRENT` forward and never back, also across a restore epoch | `tests/stores/test_admin_blob.py` (Create) | stores | plan gating test |
| `CatalogAdmin.release`, `cancel`, `rollback` and `set_embargo` over Azurite, each one version, `event.actor` the given one, `now` the storage clock | `tests/stores/test_admin_blob.py` | stores | D11 |
| `prune` on blob raises `TypeError` | `tests/test_prune.py` | unit | D11 |
| `commit` with a `None` change writes nothing; `Change.now` reaches the root | `tests/test_inventory_writer.py` | unit | D4, D11 |
| `retire_change` keeps a cited structure edition and every structure edition when a manifest is unreadable | `tests/test_prune.py` | unit | D9 |
| `_ObjectOps.list` pages and the ops classes record it | `tests/test_blob_storage.py` | unit | D13 |
| Each inequality's refusal names both values; unknown key; bad duration; missing section; defaults | `tests/test_jobs_config.py` (Create) | unit | D7 |
| `statcat admin` commands: missing reason, local and `az://`, exit codes | `tests/test_cli_admin.py` (Create) | unit | D11 |

### Part 3: the release job

*   **`src/statcatlib/release.py` (Create):** `ReleaseJob`, the steps of D1, the fold (D2), the
    release step (D4), the refusal record (D5), retirement through `retire_change` (D9), events
    (D14).
*   **`src/statcatlib/access.py` (Create):** D8.
*   **`src/statcatlib/jobs.py` (Modify):** `decode_registration`, if Part 4 has not added it.
*   **`src/statcatlib/cli/__init__.py` (Modify):** `statcat release`.
*   **`tests/stores/_stores.py` (Modify):** a fixture that uploads the plan's mix with the CODR
    generator: one-table products and grouped products of 5 to 20 tables, 200 tables in all.
*   **`tests/_fakes.py` (Create):** the fake listing, fake pods and a recording `rest`, importable
    by unit and stores tests.
*   **Docs:** `docs/api/release.md` (Create): the job, its schedule, the CronJob settings and the
    runbook for each event; `docs/reference/release.md` (Create); `docs/catalog-format.md` §1.2
    (`release`); nav entries.

| Test | File | Tier | Covers |
|---|---|---|---|
| The 200-table mix: one `seal` version, a stub pre-materialisation that opens every embargoed edition through `EmbargoReader` (or, before Part 5, `CatalogAdmin.editions`), then one `release` version making all 200 tables live; a refusal in a grouped product holds back all its tables and the rest go live | `tests/stores/test_release_job.py` (Create) | stores | plan gating test, Goal 1 |
| The release step's leaf read runs on the pool (concurrent fetches counted), with the request counts recorded | `tests/stores/test_release_job.py` | stores | NFR |
| A structure edition whose reference resolves in no structure product is refused with `references`; one that resolves in another structure product is recorded | `tests/stores/test_release_job.py` | stores | plan gating test |
| A crash before the release root leaves every entry `embargoed`, and the next run releases them | `tests/stores/test_release_job.py` | stores | plan gating test |
| A second run against an unchanged catalog writes nothing | `tests/stores/test_release_job.py` | stores | plan gating test |
| Two registrations on one live edition and schema are both refused; one older than the tombstone window is refused; a fork with a failed third registration counts only the two | `tests/test_release_fold.py` (Create) | unit | plan gating test, S28 |
| After a refused fork, `--discard` of one registration lets the next run fold the other | `tests/stores/test_release_job.py` | stores | B1, CUJ 3 |
| A `set_embargo(on=True)` landing during the release commit turns the retry into a held run | `tests/test_release_step.py` (Create) | unit | plan gating test, Q1 |
| A non-fast-forward edition is refused naming both editions, and the rest go live | `tests/test_release_step.py` | unit | plan gating test |
| A data edition whose structure edition is not due stays `embargoed` with `publish-order`; both due release together | `tests/test_release_step.py` | unit | plan gating test |
| A released entry whose manifest changed after the fold is dropped with `manifest` | `tests/test_release_step.py` | unit | Goal 2 |
| A `final` product's registration with a different `content_fingerprint` is refused with `final`: against the live edition, with `previous: null` at a new schema, and with a tombstoned `previous` | `tests/test_release_fold.py` | unit | plan gating test |
| A clock ahead of the storage clock stops the run with exit 1, through `pytest-httpx` answering the `HEAD` | `tests/test_release_job.py` (Create) | unit | plan gating test |
| A principal outside the allowlist, a group principal not on the list, a listing error, a 403 and a missing sentinel each skip the release and leave the fold done and its refusals recorded | `tests/test_release_job.py` | unit | plan gating test, Goal 7 |
| `AzureReaderListing`: the container-scope listing with paging, inherited assignments kept, role definitions read at the account scope, wildcards and not-actions, built-in and custom roles, a group kept as one principal, `[access] scopes`, retries | `tests/test_access_listing.py` (Create) | unit, `pytest-httpx` | D8 |
| A registration with a missing file, a wrong `canonical_form`, an unknown `previous`, a bad document, another `kind`, a store fault: each refused, the rest recorded | `tests/test_release_fold.py` | unit | D2 |
| A cross-product refusal at the fold drops the product and records the rest; recorded entries carry `artefacts` | `tests/test_release_fold.py` | unit | D2, D6 |
| `supersede_pending` on a registration built on the live edition beside an embargoed one; dropping the new one keeps the old one embargoed | `tests/test_release_fold.py` | unit | D2 |
| A past `release_at` and a registration older than the restore epoch's first root are recorded with `release_at: null` and never released automatically | `tests/test_release_fold.py` | unit | Q5, S25 |
| A restore to S, then a release run: nothing cancelled after S goes live | `tests/stores/test_release_job.py` | stores | B2 |
| The refusal record: written with a release, written alone when it changes, written under a failed access check, unchanged by a re-run, `since` carried, capped by age | `tests/test_release_step.py` | unit | D5 |
| A due registration is folded and released in one run, with `release.unprepared` | `tests/test_release_job.py` | unit | Q5 |
| A failed fold leaves the release step to run; exit 1 | `tests/test_release_job.py` | unit | D1 |
| `--dry-run` writes nothing, `CURRENT` included; step 2 advances a lagging `CURRENT` | `tests/test_release_job.py` | unit | D1 |
| Each D14 event a step raises appears once with its fields | `tests/test_release_job.py` | unit | D14 |
| `statcat release` exit codes 0, 1 and 2, the refused variables, stdout holding events only | `tests/test_cli_release.py` (Create) | unit | D1, D15 |

### Part 4: GC

*   **`src/statcatlib/gc.py` (Create):** D10.
*   **`src/statcatlib/jobs.py` (Modify):** `decode_registration`, if Part 3 has not added it.
*   **`src/statcatlib/cli/__init__.py` (Modify):** `statcat gc`.
*   **Docs:** `docs/api/gc.md` (Create), with the runbook for each event; `docs/reference/gc.md`
    (Create); nav entries.

| Test | File | Tier | Covers |
|---|---|---|---|
| GC deletes exactly the unreferenced files, checked against an independent reachability computation over the store | `tests/stores/test_gc.py` (Create) | stores | plan gating test, Goal 4 |
| It keeps a file a racing upload wrote inside the grace period, and everything a rolled-back-to edition lists | `tests/stores/test_gc.py` | stores | plan gating test |
| With files older than `upload_grace` (by the `storage` clock), it keeps every file a registration's manifest lists, run before the fold, and the fold's edition then verifies; with no registration the same files are deleted | `tests/stores/test_gc.py` | stores | plan gating test, O5 |
| A registration whose manifest is missing or names another edition: nothing deleted in that product's `data/`, `gc.failed_closed` | `tests/test_gc.py` (Create) | unit | plan gating test |
| Backup retention longer than the GC window refuses to start | `tests/test_gc.py` | unit | plan gating test |
| A second run deletes nothing | `tests/stores/test_gc.py` | stores | plan gating test |
| A catalog with fewer than `keep_roots` roots in the window keeps what the newest 10 name | `tests/test_gc.py` | unit | spec §11.6 |
| A missing root above `expired_through` deletes nothing | `tests/test_gc.py` | unit | spec §11.4 |
| Folded registrations past the window are deleted; refused ones stay, or with Q6 go after the tombstone window | `tests/test_gc.py` | unit | O1, S20 |
| An unregistered edition, with or without a manifest, is reported and kept; `--discard` deletes its registration, manifest and directory in that order; `--discard` of a recorded edition refuses | `tests/test_gc.py` | unit | S22, B1 |
| `--dry-run` writes the plan and deletes nothing; `--forecast` deletes the intersection and reports each side's extra; a retired directory loses its manifest first; nothing under `inventory/` or `history/` is touched | `tests/test_gc.py` | unit | D10 |
| `statcat gc` exit codes and the deletion log's fields | `tests/test_cli_gc.py` (Create) | unit | D10, D15 |

### Part 5: `EmbargoReader`

*   **`src/statcatlib/embargo.py` (Create):** D12.
*   **`src/statcatlib/catalog.py` (Modify):** `_open_view`, used by `Product.view`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export it.
*   **Docs:** `docs/api/embargo.md` (Create): the preview and pre-materialisation;
    `docs/reference/embargo.md` (Create); nav entries.

| Test | File | Tier | Covers |
|---|---|---|---|
| An embargoed and a live edition's views read their rows; a withdrawn one raises; `refresh` sees a new fold | `tests/stores/test_embargo_reader.py` (Create) | stores | D12, Goal 8 |
| No attribute of `Catalog`, `Product` or `EditionView` returns one; core imports with `obstore` absent | `tests/test_public_api.py` | unit | spec §6.4 |
| A local catalog's embargoed edition opens through it | `tests/test_embargo_reader.py` (Create) | integration | D12 |

### Part 6: server expiry (provisional)

*   **`src/statcatlib/store/blob.py` (Modify):** `AzureHistoryLease`, `LeaseLostError`,
    `BlobExpiryStorage`, the REST verbs and `_send(method, path, headers)`,
    `BlobInventoryStorage(lease=)`; the account-key test subclass in `tests/stores/_stores.py`
    signs each verb.
*   **`src/statcatlib/release.py` (Modify):** step 7, the gates, `KubernetesPodVersions`, the
    leaf-set cache.
*   **`src/statcatlib/admin.py` (Modify):** opt-in over a `BlobLocation`.
*   **Docs:** `docs/api/release.md` gains expiry, the lease runbook and `--state-dir`.

| Test | File | Tier | Covers |
|---|---|---|---|
| A second release run fails to take the `HISTORY` lease and writes nothing | `tests/stores/test_expiry_blob.py` (Create) | stores | GH-125 acceptance |
| A failed renewal stops expiry before its next destructive call; the 30-minute cap stops it | `tests/test_history_lease.py` (Create) | unit | GH-125 acceptance |
| A leaf rewritten after step 6's final check survives the `If-Match` delete | `tests/stores/test_expiry_blob.py` | stores | GH-125 acceptance |
| Expiry on Azurite never deletes a leaf a retained root names, against an independent reachability computation | `tests/stores/test_expiry_blob.py` | stores | GH-125 acceptance |
| The pod check with an unreachable pod, a pod with no IP or a missing `sdmxlib_version` skips expiry and leaves the release done; no token file gives "not run"; a 403 fails the gate; against a fake API through `pytest-httpx` | `tests/test_release_job.py` | unit | GH-125 acceptance |
| Expiry before `not_before` is skipped | `tests/test_release_job.py` | unit | H20 |
| An admin commit waits for a run's lease, and gives up after 120 s naming the runbook | `tests/test_history_lease.py` | unit | S26 |
| The second run reads only roots written since the first, from the cache | `tests/stores/test_expiry_blob.py` | stores | S23 |
| Opt-in on Azurite succeeds; over a `MemoryStore` or `S3Store` location it is refused | `tests/stores/test_expiry_blob.py`, `tests/test_catalog_admin.py` | stores, integration | H21 |

### Acceptance map

| Plan Phase 5 gating test or goal | Part | Test |
|---|---|---|
| A 200-table synthetic release becomes visible in one Inventory version | 3 | `tests/stores/test_release_job.py` |
| The fold runs `check_edition(at="receive", remote=, structures=)` on an edition with no bindings | 3 | `tests/stores/test_release_job.py` |
| An interrupted release leaves every entry `embargoed`, and a re-run releases them | 3 | `tests/stores/test_release_job.py` |
| The fold refuses two pending editions on one live edition and one older than the tombstone window | 3 | `tests/test_release_fold.py` |
| A stale automatic release after embargo was turned on is refused | 3 | `tests/test_release_step.py` |
| A non-fast-forward edition is refused, naming both, and the rest goes live | 3 | `tests/test_release_step.py` |
| A data edition whose structure edition is not due stays `embargoed` | 3 | `tests/test_release_step.py` |
| A new edition of a `final` product with a different `content_fingerprint` is refused | 3 | `tests/test_release_fold.py` |
| A release run with its clock ahead is refused | 3 | `tests/test_release_job.py` |
| A principal outside the allowlist stops the release | 3 | `tests/test_release_job.py`, `tests/test_access_listing.py` |
| GC deletes exactly the unreferenced files; keeps a racing upload's and a rolled-back-to edition's | 4 | `tests/stores/test_gc.py` |
| GC keeps a registered edition's files and fails closed on an unreadable manifest | 4 | `tests/stores/test_gc.py`, `tests/test_gc.py` |
| GC refuses to start with backup retention longer than the GC window | 4 | `tests/test_gc.py` |
| `advance-current` moves a lagging `CURRENT` forward and never back | 2 | `tests/stores/test_admin_blob.py` |
| A re-run of each job changes nothing | 3, 4 | `tests/stores/test_release_job.py`, `tests/stores/test_gc.py` |
| Goal 2: no edition goes live with a different manifest, through any writer | 1, 3 | `tests/test_catalog_admin.py`, `tests/test_release_step.py` |
| GH-52: a data edition whose partial gained a code is refused `live` until its structure edition is live | 3 | `tests/test_release_step.py` (the `publish-order` row) |
| GH-52: an embargoed dataflow returns 404 from every pod | Phase 6 | moved by GH-52's comment of 2026-11-02 |
| GH-125 part 4's acceptance | 1 | Part 1's tests |
| GH-125's server expiry acceptance | 6 | Part 6's tests |
| `just ci` passes | all | the gate |

### Verification Plan

*   `just ci` for each part, on a machine with a container runtime (Phase 4 W6).
*   `just coverage` in each part's PR.
*   Part 3's stores test records the fold's and release's request counts, as `bench/` records do.
*   Handoff row 6, done 2026-11-12: GH-134 item 9 rerun with filled leaves, before Part 3 lands
    (D6's cost has the numbers).
*   Before the first production release, one run against the GH-134 account and a test cluster:
    the access check with the sentinels at the resource group and subscription, compared with the
    portal's "Check access" for the container (A1 itself is handoff row 6a, done 2026-11-12 for
    every scope but the management groups); a 200-table fold and release with their
    times; and, before Part 6's first expiry, the lease, `If-Match` delete and ranged read over a
    bearer token, and the pod listing under the release job's service account.

## Future work

*   Q8's operation that makes a `final` product `revisable`.
*   An operator identity separate from the release identity (S29), with Phase 8.
*   A root and manifest cache for GC, if a measurement shows the 560 MB read matters.
*   A check of the container's ACLs beside the role listing.
*   Eviction of `EmbargoReader`'s cache, with Phase 6's placement.

## Review response

Round 1 ([report](egm/release-job/review_report.md)). Each finding and its change in this
revision.

| Finding | Change |
|---|---|
| B1 | `--discard` covers registered, unrecorded editions (D10, S22, CUJ 3); the fork counts passing registrations only (S28); a gating row |
| B2 | Re-armed registrations are held with `release_at: null` (D2, S25); S + 1 tombstones proposed to the suite design; GC's claim corrected; a stores row |
| M1 | Expiry only in `--expire` runs; release-time runs hold the lease for seconds; a bounded 120 s admin wait (S26); Q1 states the limit |
| M2 | Q5's second rule and `fold.release_at_past` |
| M3 | Paging and `api-version`s in D8; `tests/test_access_listing.py`. The owner's Q4 decision of 2026-11-10 then set group membership, PIM eligibility and the account's public and shared-key settings outside the check, as known limits, and added the sentinels and A1 |
| M4 | `_records` reads an incoming entry's manifest (D6); a `CatalogAdmin` test row; S29 for the actor |
| M5 | D2's `final` row compares with the live edition, handles `previous: null` and a tombstoned `previous` |
| M6 | Q1's table, §6.6 cited, the worked example in S18, the "ignore" option marked, CUJ 1 retitled |
| M7 | `full.leaves()` in every step; the fold's per-run read cache; the NFR text; a pool assertion |
| M8 | The lease before any write; step 0 refuses a 2.x catalog before Part 6; the lease on writes moved to D13 |
| M9 | Failure isolation per step (D1); exit codes 0, 1, 2 with `backoffLimit: 0`; listing retries |
| M10 | Step 5 writes the record whenever it changes, held or skipped |
| M11 | `commit` writes nothing for `None`; `Change.now` |
| M12 | GC reads at least the newest `keep_roots` roots; a test row |
| M13 | `retire_change` moves the builder, the sentinel and the fallback; one signature |
| M14 | `ManifestArtefacts(Fetch)`'s error contract in D6 |
| M15 | S24 to S29 and the Cross-repo contract section |
| M16 | Options, recommendation, owner and gate columns; S15's status |
| M17 | One gate per Q and S; land and release defined |
| M18 | D13 and Part 6 marked provisional; Q9; the ranged read; a reachability row; handoff row 11 |
| M19 | D5's cost, kind (S27), export rule, age cap; Q2 lists the recompute option |
| M20 | The surface table; `--report` cut |
| M21 | `storage=` and `clock=` seams (D15); the `HEAD` path through `pytest-httpx` |
| M22 | D15; GC's exit codes and CLI test |
| M23 | Output fields for events, the deletion log and the reports |
| M24 | The Symbols table completed; `decode_registration` in `jobs.py` |
| M25 | D2's reference check as code |
| M26 | `_open_view`, the stamps directory, `references=False`, lazy imports (D12) |
| M27 | Part 1 and Part 2 Modify entries for existing tests and pages |
| M28 | Tiers corrected; `tests/_fakes.py`; `_send(method, path, headers)`; Create marks |
| M29 | The 200-table mix with grouped products and the stub pre-materialisation |
| M30 | GC's registered-edition row ages the files |
| M31 | Rows for each listed behaviour |
| M32 | The Not confirmed list and its risk rows |
| M33 | Memory, Disk and the per-module coverage table |
| M34 | D7's retention table; Terms rows |
| M35 | The identifier prefixes; issue references qualified; the registration bytes re-encoded |
| M36 | The per-part outcomes; O8 and D6 on the unfilled corpus; handoff row 6 |
| M37 | Terms rows for orchestrator, operator and release identity; how an operator runs `statcat admin` |
| m1 | S28 |
| m2 | D10's registration rule names `retired`; manifest-less directories reported |
| m3 | D13's "not run" rule; the hub's H20 text left to the hub |
| m4 | D2 drops a product's whole leaf change |
| m5 | D2 states the order of checks and the leaf they read |
| m6 | `_may_be_refused` through `_records` |
| m7 | D6 names the six read sites |
| m8 | `_refuse_unsafe_files`, `_lookup`, `_pending_document`, `structure_editions` and `precheck_refusals` named for reuse |
| m9 | D11's `__repr__` and one `now()` per change |
| m10 | Docs, nav and reference pages per part |
| m11 | `record_uploaded(lifecycle=None)` |
| m12 | `_ObjectOps.list` in the ops classes; `_send`'s change |
| m13 | Part 1's note in `bench/corpus-results.md` |
| m14 | CUJ 2 shows `"rule": null`; the `REFUSED` line prints `-` |
| m15 | D2 states why equality suffices |
| m16 | D3 states `record`'s relabel |
| m17 | D13 states the cap's start |
| m18 | D14 and the Errors table completed |
| m19 | O8's dates; StatCat-Suite/sdmxlib#382; issue texts dated; §6.2 and §6.6 in the Inputs |
| m20 | D9 states where P7 ends |
| m21 | D6 records the change to GH-125's reader; handoff row 15 |
| m22 | D7's allowlist and required sections |
| m23 | Kept the unit strings; D7's table weighs the integer keys |
| m24 | No new `Any` |
| m25 | Acceptance map rows for Goal 2 and GH-52's items |
| m26 | Errors rows |
| m27 | D8 and D13 state version parsing, a pod with no IP, member types and wildcards |
| m28 | Future work list |
| m29 | Options tables for D8, D9 and the surface; D11 to D14 have no alternative left after the spec's rules |
| m30, m31 | The comprehension and style reports' rewrites, applied where the text changed |

# Inventory history stage 2: the catalog lock, opt-in, expiry and restores

*   **Author:** Gabriel Gellner
*   **Status:** Draft, revision 1. No EGM review yet.
*   **Date:** 2026-11-12
*   **Issue:** GH-125 parts 2 and 3. Part 1 shipped in 0.14.0 and part 4 in 0.17.0. Suite
    tracker: StatCat-Suite/statcat-suite.github.io#19.
*   **Read against:** statcatlib `932e8b8` (`origin/main`, the merge of GH-157: version 0.17.0
    plus the unreleased release job); sdmxlib `198f0b13` (`origin/main`, which is `v0.88.0`);
    sdmxlib-rest `af53a59` (`origin/main`, 0.10.3 plus an unreleased change); the hub at `4779ed5`,
    with the suite design
    [Inventory history log](https://statcat-suite.github.io/design/inventory-history-log/) at
    revision 5. Every `file:line` is at these commits. Find each site by its symbol and use the
    number as a hint. Issue texts were read on 2026-11-12.
*   **Inputs:**
    *   GH-125's body: parts 2 and 3 and their acceptance items. The body cites revision 4 of the
        suite design; this design follows revision 5, which adds S23, S25 and S26.
    *   The suite design, which sets the cross-repo contract (decisions H1 to H24). This design
        links it and changes none of it. The contract changes it found necessary are in the table
        "Proposed suite design and specification changes".
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        ("the spec") at `198f0b13`: §6.1, §6.2, §6.3, §6.6, §6.9, §9.2, §10.1, §10.3, §11.4 and
        §11.6.
    *   `sdmxlib.inventory` at `v0.88.0`: `_format.py`, `_rules.py`, `_reader.py`,
        `conformance.py` and its `EGM.md`.
    *   StatCat-Suite/sdmxlib#361, whose acceptance list holds the stage-2 conformance cases.
    *   The [Phase 5 design](release-job.md) ("the release-job design"): its section "GH-125's
        parts", D13, Part 6, handoff rows 11 and 12, Q9, S23, S25 and S26.
*   **Identifier prefixes.** E1 to E17 are this design's decisions and Q1 to Q9 its open questions.
    N1 to N7 are its handoff rows for sdmxlib, and R1 to R10 its changes to the release-job
    design's Part 6. S31 to S43 continue the S series of the Phase 4 and Phase 5 designs, which
    ends at S30. The release-job design's own identifiers are written with its name, as in
    "release-job D13". H1 to H24 are the suite design's, and P1 to P7 the
    [Phase 2a design](phase-2a-catalog-editions.md)'s. `GH-N` is a statcatlib issue.

--------------------------------------------------------------------------------

## Overview

A catalog keeps every Inventory root it has written. At 7,000 products a root is about 1.6 MB,
and a server writes 3 to 10 a day (suite design, Problem). Stage 1 (statcatlib 0.14.0) made each
root carry its change list and a hash of the root before it. Stage 2 lets a catalog that opts in
move old roots into append-only history segments and delete those roots and the leaves no
retained root names.

This design is the local half of stage 2 and the engine both halves share. It adds, on a catalog
directory:

1.  the catalog lock, an OS advisory lock on `<catalog>/LOCK` that every Inventory commit takes;
2.  opt-in, which writes `inventory/HISTORY` and rewrites `catalog.json` as format 2.0;
3.  the expiry engine, steps 0 to 8 of spec §6.9, run by `CatalogAdmin.prune`;
4.  restores of an opted-in catalog to an older root, which bump the restore epoch;
5.  the history checks of `statcat verify-catalog` and the history lines of `statcat doctor`.

The engine is one function over an expiry-storage interface. Local disk implements the interface
here. The release-job design's Part 6 implements it over Azure Blob Storage, adds the `HISTORY`
lease, the two expiry gates and the persistent volume for the leaf-set cache, and calls the same
function from `statcat release --expire`.

| Part | After it lands, a user can |
|---|---|
| 1. The catalog lock, the stale-base refusal, the writer's stage-2 decode | seal and run `CatalogAdmin` on a local catalog whose `catalog.json` is 2.x. Every local commit takes the lock. Nothing opts in or expires yet. |
| 2. Opt-in | opt a local catalog in with `statcat admin opt-in`, and read `HISTORY` in `statcat doctor`. |
| 3. The expiry engine and `prune` | expire old Inventory versions of a local opted-in catalog with `statcat prune --expire-older-than`, with a dry run and a deletion log. |
| 4. `verify-catalog` and `doctor` | check the root chain, the segments and `HISTORY`, and run sdmxlib's stage-2 conformance cases against the writer. |
| 5. Restores | restore a local opted-in catalog to root S with `statcat admin restore`. |

| Capability | Local disk | Azure Blob Storage | S3, GCS |
|---|---|---|---|
| Commit under the catalog lock | Part 1 | release-job Part 6 (the `HISTORY` lease) | No lock; a 2.x catalog is refused (H21) |
| Opt-in | Part 2 | release-job Part 6 | Refused (H21, H23) |
| Expiry | Part 3, in `prune` | release-job Part 6, in `statcat release --expire` | Refused |
| History checks in `verify-catalog` and `doctor` | Part 4 | Not planned; both commands take a directory | Not planned |
| Restore | Part 5 | release-job Part 6 (R9) | Refused |

### Terms

The suite design's Terms section defines Inventory version, base version, change list, root
chain, history fields, retained root, head chain, checkpoint, expiry, history segment,
`expired_through` and catalog lock. This design adds:

| Term | Meaning here |
|---|---|
| Opted-in catalog | A catalog whose `catalog.json` is format 2.x, with `history.since_seq` and `inventory/HISTORY` (spec §6.1). |
| 1.x catalog | A catalog whose `catalog.json` is format 1.x. Nothing expires in it. |
| Stage-2 writer | A statcatlib from Part 1 on: it takes the catalog lock and checks `expired_through`, so sdmxlib's `decode_catalog(decoder="stage-2-writer")` lets it write a 2.x catalog. |
| Stage-1 writer | statcatlib 0.14 to 0.17, and 0.18 if it ships before Part 1: it writes the history fields, takes no lock, and refuses a 2.x catalog. |
| Engine | `statcatlib.store.expiry.expire`, the function that runs steps 0 to 8. |
| Expiry storage | The interface the engine reads, writes and deletes through (E8). |
| Candidate | A root that step 1 takes for this run. |
| Pending segment | An `expiry` segment on the segment chain after `HISTORY.cleaned_through`: its roots and leaves may still exist. |
| Orphan segment | A segment under `history/` that the chain from `HISTORY.newest_segment` does not reach. |
| W, K | The retention window and the number of newest roots expiry never takes (spec §11.6): `inventory_window` and `keep_roots`. |
| Run | One call of the engine, from the lock to its report. |
| Land, release | A part lands when its PR merges to `main`, and is released when a statcatlib version carrying it is on PyPI. |

## The Problem

`inventory/` grows without bound, and nothing in statcatlib can shrink it. `CatalogAdmin.prune`
"deletes no Inventory root or leaf" (`admin.py:568`), and the Phase 2a design's P7 kept roots,
leaves and tombstones until stage 2. The suite design and spec §6.9 give the rules. Eight gaps in
the code stand between those rules and a catalog that expires:

- `try_commit` takes no lock. The local writer skips a leaf it finds present
  (`store/inventory.py:385`) and creates the root later (`:692`). An expiry between the two would
  delete a leaf the new root names.
- `commit` reads the head before `try_commit` runs (`store/inventory.py:747`), and nothing checks
  that base against `HISTORY.expired_through`. Expiry frees root names, so a writer with a stale
  base could create a root at a freed name.
- The writer decodes `catalog.json` with `decoder="stage-1-writer"` (`store/inventory.py:570`), so
  every write to an opted-in catalog raises `InventoryWriteRefusedError`.
- Nothing writes `inventory/HISTORY` or a segment, and `CatalogAdmin` has no opt-in. Under
  `src/statcatlib` only `gc.py` reads `HISTORY` (`gc.py:303-317`).
- `prune` takes one window, `older_than`, and has no W or K (`admin.py:550`).
- `InventoryStorage` has no list, no delete and no conditional replace
  (`store/inventory.py:322-353`), which expiry needs on every store.
- statcatlib has no restore. Its tests restore by hand: they unlink the roots above S, rewrite
  `catalog.json` and call `try_commit` (`tests/test_inventory_conformance.py:184-196`,
  `tests/stores/test_release_job.py:481-497`).
- `verify_catalog` reads the head's leaves only (`diagnostics.py:354-369`), and `doctor` reports
  nothing about history (`diagnostics.py:577-627`).

The release-job design's Part 6 is written against "GH-125's engine" and marked provisional until
this design defines the interface (release-job Q9, handoff row 11).

## Goals

1.  Every Inventory commit on a local catalog runs under the catalog lock, and a process that
    dies while it holds the lock blocks no later commit (Part 1).
2.  A writer never creates a root on a base at or below `expired_through` (Part 1).
3.  A local catalog opts in with one command, and the command refuses a catalog whose roots
    cannot support expiry (Part 2).
4.  After expiry, `inventory/` holds the newest K roots, the roots within W, the checkpoints and
    the roots from before the opt-in, and every leaf a retained root names (Part 3).
5.  A crash after any step leaves a catalog that reads correctly, and the next run finishes the
    work (Part 3).
6.  "What was live at time T" has the same answer after expiry as before it (Part 3).
7.  `statcat verify-catalog` detects an edited segment line and a deleted root (Part 4).
8.  A restore to root S records every root it discards, and no edition recorded only by a
    discarded root loses its files to GC inside the window (Part 5).
9.  The release-job design's Part 6 needs no engine code of its own: it supplies a storage, a
    lock, the gates and a cache directory (E8, E16).

## Non-Goals

*   The Azure `HISTORY` lease, the expiry gates, the pod check, opt-in over a `BlobLocation` and
    the persistent volume. They stay in the release-job design's Part 6.
*   Expiry, opt-in or a lock on S3 or GCS (H21, H23).
*   A restore of a 1.x catalog (Q8). Phase 9 owns the restore drill and
    `verify-catalog --find-newest-consistent` (sdmxlib's plan, line 431).
*   Undoing an opt-in. The spec has no rule for it.
*   Collecting the leaves a crashed commit or a restore leaves unnamed. The suite design defers
    it.
*   Dropping tombstones from `retired`. `prune` and the release job still never expire one.
*   `statcat verify-catalog` and `statcat doctor` over a `BlobLocation`.
*   The PROV-O exporter and a public reader of history segments (stage 3). N4 asks sdmxlib for
    the one reader function a test here needs.
*   `correct` and `reinstate`, which stay in GH-130.

## Open Questions

Each question lists its options with the cost of each. The recommendation is the option this
design is written to. An answer that differs changes the sections named in the last column.

| # | Question | Options and their costs | Recommendation | Changes |
|---|---|---|---|---|
| Q1 | Where do the pure stage-2 rules live: the checkpoint test, the candidate choice, the chain check, the restore's tombstones and the history checks of `verify-catalog`? | **(a) sdmxlib**, as `sdmxlib.inventory` functions in a 0.89.0 release (N1, N2). Cost: one sdmxlib design and release before Part 3, and a cap bump in statcatlib and sdmxlib-rest. **(b) statcatlib**, in a module that imports only sdmxlib. Cost: rules the suite design gives to sdmxlib live in the writer's repository; sdmxlib's reference writer needs its own copy to run its stage-2 conformance cases; a later move is a breaking import change. | (a). Every other Inventory rule is a pure function in sdmxlib that statcatlib's writer calls (`retire`, `release_due`, `change_list`, `verify_entry`). Parts 1 and 2 need nothing from it, so sdmxlib's work runs beside them. | E17, the land order, N1, N2 |
| Q2 | What does a commit do on a filesystem that refuses advisory locks? | **(a)** On a 1.x catalog, commit without the lock and log one WARNING per process; on a 2.x catalog, and for opt-in, expiry and a restore, refuse. Cost: a stage-2 writer on such a filesystem races an opt-in made through another mount, the race spec §6.1 already accepts for stage-1 writers. **(b)** Refuse every commit. Cost: a catalog that works today on such a filesystem stops taking seals after an upgrade, with no change to its data. | (a). Nothing expires on a 1.x catalog, and opt-in is refused where the lock is. | E5 |
| Q3 | Step 1 reads `created_at` with "a ranged GET of the first 1 KiB" (GH-125, the suite design and spec §6.9). The walk also tests each root for the history fields, which are the last two fields of the document, and step 2 hashes each candidate's whole bytes. Does the engine use the ranged read? | **(a) Whole roots.** Cost: a run that takes nothing reads one or two roots (at most 3.2 MB at 7,000 products) where a ranged read costs 2 KiB. **(b) Ranged reads in step 1.** Cost: a second read path for roots, which saves bytes only on a run that takes nothing; every candidate is then requested twice. | (a). The interface keeps `read_range`, and the engine uses it to read segment headers (E10). S31 makes the spec's sentence optional. | E9, R1 |
| Q4 | How does a caller ask `prune` to expire, and with what W and K? A local catalog has no jobs configuration. | **(a)** `prune(..., inventory_window=None, keep_roots=10)`: expiry runs only when `inventory_window` is given, and it must be at least `older_than` plus one day. Cost: two windows on one call. **(b)** `prune` on an opted-in catalog always expires, with W equal to `older_than` plus one day. Cost: an upgrade changes what an existing `prune` call deletes. **(c)** A separate `CatalogAdmin.expire`. Cost: GH-125's acceptance asks for one call that retires and expires. | (a) | E13 |
| Q5 | What clock measures W on local disk? Spec §6.9 says "the storage clock", and a directory has none. | **(a) The machine's clock**, through `LocalInventoryStorage.now()`. Cost: a laptop clock set forward expires roots early; the newest K roots, the checkpoints and every segment line stay. **(b) The head root's `created_at`.** Cost: a quiet catalog never expires, and the rule differs from the server's. | (a), which is P1's rule for every local change. On a server the engine reads the storage clock through `BlobInventoryStorage.now()`. | E13, S37 |
| Q6 | A restore to S brings back root S's entries. An entry that S records `embargoed` with a `release_at`, and that a discarded root cancelled, is released by the next release run. Does the restore guard against it? | **(a) No guard**, as the suite design reads. Cost: the operator must find and repeat every discarded cancellation before the next release run. **(b) Root S + 1 sets `embargo: true`.** Cost: one operator step after every restore (`statcat admin embargo off`), and a local seal records `embargoed` until then; a suite design change (S34). **(c) Root S + 1 clears `release_at` on every embargoed entry.** Cost: the restore version rewrites leaves, and every scheduled release needs a manual release. | (b). `embargo: true` already holds every automatic release (release-job Q1), and the restore's report lists each discarded transition for the operator. | E14, S34 |
| Q7 | Is an orphan segment a finding of `verify-catalog`, which sets exit status 1? | **(a) A note**, printed as `ORPHAN` and leaving the exit status alone, when the orphan decodes and names this catalog. Cost: an orphan someone planted is reported at the same level as a crash's. **(b) A finding.** Cost: nothing under `history/` is ever deleted, so one crashed run fails every later `verify-catalog`, which is the monthly restore test (spec §10.3). | (a). An unlinked or overlapping segment on the chain stays a finding. | E15, S36 |
| Q8 | Which catalogs does `CatalogAdmin.restore` accept? | **(a) Opted-in catalogs, to a root at or above `since_seq - 1`.** Cost: no command restores a 1.x catalog or discards back past the opt-in point. **(b) Also 1.x catalogs.** Cost: the roots a 1.x restore discards are deleted with no record. **(c) Also below `since_seq - 1`, lowering `since_seq` in the same rewrite.** Cost: a format rule the spec lacks (S40). | (a) | E14, S40 |
| Q9 | How long does a local commit wait for the lock? The spec gives 120 s for an Azure `CatalogAdmin` call (S26) and no local figure. | **(a) 120 s**, then `CatalogLockTimeoutError`. Cost: a seal that meets a first expiry of thousands of roots fails after two minutes and keeps its journal entry. **(b) Until the lock is free.** Cost: a hung holder blocks every writer with no error. | (a), with one constant for both stores. | E1, S39 |

The owner's answers are recorded here before Part 1 starts. Q1 gates Part 3, Q2 and Q9 gate
Part 1, Q3 to Q5 gate Part 3, Q7 gates Part 4, and Q6 and Q8 gate Part 5.

### Proposed suite design and specification changes

Each row changes a document another repository owns. This design edits neither the spec nor
the suite design. "Owner" is the repository whose document changes; a hub row also needs the
spec to carry the same text.

| # | Where | Change | Why | Owner | Gates |
|---|---|---|---|---|---|
| S31 | Spec §6.2 `created_at`, §6.9 step 1; suite design step 1; GH-125 part 3 | The ranged read of a root's first 1 KiB is optional. A writer may read whole roots in step 1. | Q3 | sdmxlib, hub | Part 3 |
| S32 | Spec §6.9 step 2; suite design step 2 | The chain check starts at the head-chain predecessor of the lowest candidate: the line of that `seq` in the newest `expiry` segment reached from `newest_segment`, stepping over `restore` segments, or the retained root when no such line exists. | "From the newest segment's last line" names a discarded root after a restore, since the newest segment is then a `restore` segment. | sdmxlib, hub | Part 3 |
| S33 | Spec §6.6 rule 5, §6.9 Restores; suite design Restores | The restore's root S + 1 is exempt from the stale-base refusal. From the restore's `HISTORY` write until root S + 1 exists, a stage-2 writer refuses every other commit and expiry refuses to run. A rerun takes S from the newest `restore` segment (`first - 1`) and refuses another target. | A restore to a checkpoint below `expired_through` sets `expired_through` to S, so rule 5 as written refuses the restore's own root. The spec says a rerun "resumes from the epoch in `HISTORY`" and gives writers no rule for the interval. | sdmxlib, hub | Part 5 |
| S34 | Spec §6.9 Restores step 5; suite design Restores | Root S + 1 is written with `embargo: true`. | Q6 | sdmxlib, hub | Part 5 |
| S35 | Spec §6.9 Restores, `cleaned_through` | A restore leaves `cleaned_through` as it was. The next expiry run steps over `restore` segments when it advances `cleaned_through`. A rerun of a restore that crashed before its `HISTORY` write adopts its orphan `restore` segment under step 3's link rule. | The spec gives `cleaned_through` no value after a restore, and gives a `restore` orphan no adoption rule. | sdmxlib | Part 5 |
| S36 | Spec §6.9 `verify-catalog` check 3; §11.6 alerts | `verify-catalog` prints an orphan segment that decodes and names the catalog as a note that leaves its exit status alone. An unlinked or overlapping segment on the chain is a finding. | Q7 | sdmxlib | Part 4 |
| S37 | Spec §6.9 step 1, §11.6 | On a catalog directory, W is measured by the machine's clock, and W is at least `prune`'s window plus one day. | Q5; the spec names only the storage clock and the `data/` GC window. | sdmxlib | Part 3 |
| S38 | Spec §6.9 `verify-catalog` check 2; §10.3 | The head's entries get the full edition checks. Every other retained root gets its leaves checked: present, hashing to their names and valid. | The spec exempts a checkpoint's manifests only. A root inside W can also name an edition `prune` or GC has deleted. | sdmxlib | Part 4 |
| S39 | Spec §6.9 The catalog lock, Locally | A commit waits up to 120 s for the lock, then is refused. On a filesystem that refuses advisory locks a stage-2 writer commits to a 1.x catalog without the lock and refuses a 2.x catalog, opt-in, expiry and a restore. On Windows the lock is a one-byte exclusive byte-range lock, taken with `LockFileEx` or the equivalent `LockFile`. | Q2, Q9, E1 | sdmxlib | Part 1 |
| S40 | Spec §6.9 Restores | A restore refuses a target below `since_seq - 1`. | Q8. Below it the new epoch's roots sit under `since_seq` and never expire. | sdmxlib | Part 5 |
| S41 | Spec §6.1 Opt-in | Opt-in on a catalog with no Inventory version records `since_seq: 1`. Opt-in reads every root, verifies the chain from `since_seq` to the head, and refuses a missing or undecodable root. | The spec's two refusals assume a head and readable roots. A broken chain would otherwise stop every expiry at step 2. | sdmxlib | Part 2 |
| S42 | Spec §6.9 step 6 | Step 6 deletes no leaf when a retained root cannot be read or decoded. | The step computes the leaves "no retained root names", which an unreadable root leaves unknown. | sdmxlib | Part 3 |
| S43 | Spec §6.9 Crashes | A root listed in a pending header whose name a later epoch's root has taken since a restore is kept without a report. A dry run takes no lock. | After a restore to S below `expired_through`, the new epoch reuses `seq` values that pending headers list. | sdmxlib | Part 5 |

## Cross-repo contract

The suite design sets the contract, and this design stays inside it: it writes the documents
sdmxlib 0.88 already decodes and changes no format. It is statcatlib's and stays local for that
reason. What other code reads of its output:

| Output | Writer | Readers |
|---|---|---|
| `<catalog>/LOCK` | every stage-2 writer on local disk | none; its content is empty and never read |
| `catalog.json` 2.0 with `history.since_seq` | opt-in; a restore's epoch bump | sdmxlib's reader fallback, `statcat gc`, every writer's decode |
| `inventory/HISTORY` | opt-in, the engine, a restore | sdmxlib's reader fallback, `statcat gc` (`gc.py:303-317`), `verify-catalog`, `doctor` |
| `history/<epoch>/<first>-<last>-<uuid7>.jsonl` | the engine, a restore | `verify-catalog`, `doctor`; a stage-3 exporter |
| The `expiry.*` and `restore.*` events (E12) | the engine, a restore | `statcat prune`'s output; Phase 8 alerting through the release job |
| `ExpiryStorage` and `InventoryStorage.exclusive()`, `check()`, `stage_2` (E2, E8) | this design | the release-job design's Part 6 |

Dependencies stay one way: the engine imports sdmxlib, and sdmxlib imports nothing of
statcatlib's.

## Handoff

### sdmxlib (StatCat-Suite/sdmxlib#361)

sdmxlib 0.88 ships every document stage 2 writes: `History`, `SegmentHeader`, `SegmentLine`,
`CatalogHistory`, `encode`, `encode_segment`, `segment_line`, `segment_path`, `decode_history`,
`decode_segment`, `decode_catalog(decoder="stage-2-writer")`, `Root.has_history`, the reader
fallback and the nine JSON Schemas. It ships no stage-2 rule, check or conformance case
(sdmxlib's `conformance.py:21-23`, `:717-724`). The rows below are what stage 2 needs beyond 0.88.

| # | What sdmxlib ships | Needed by | Blocks |
|---|---|---|---|
| N1 | Pure history rules in `sdmxlib.inventory`, under Q1 (a): `is_checkpoint` (H4's three cases, from a root's or line's `seq`, `created_at` and `restore_epoch` and those of the one before); `expiry_candidates` (step 1's walk over those facts, K, W, the time, `CURRENT.seq`, `expired_through` and `since_seq`, returning the candidates, the checkpoints stepped over and why the walk stopped); `SEGMENT_MAX_LINES` (1,000) and `SEGMENT_MAX_BYTES` (64 MiB); `restore_retired` (S25's tombstones for root S + 1, from root S, its leaves and the discarded roots' leaves). | E9, E10, E14 | Part 3 and Part 5, under Q1 (a) |
| N2 | `check_history(fetch, *, roots, segments) -> list[HistoryFinding]`: `verify-catalog`'s checks 1 to 3 of spec §6.9 over a `Fetch`, the listed root numbers and the listed segment paths. It stands beside `verify_entry`, which already serves check 4. | E15 | Part 4, under Q1 (a) |
| N3 | Stage-2 conformance: an `ExpiryBackend` protocol that extends `HistoryBackend` with `opt_in()`, `expire(*, now, keep_roots, window, stop_after=None)`, `restore(seq, *, stop_after=None)`, `verify()`, `list(prefix)` and `delete(path)`; the checks in an `EXPIRY_CHECKS` tuple; and `run_expiry`. The cases are the open ones of StatCat-Suite/sdmxlib#361: opt-in refused when a root without the history fields follows one with them; a base at or below `expired_through` refused after the lock; `CURRENT` lagging by more than K; the stop at a root above `since_seq` without the fields; a crash after each step in a run of several segments, with linked orphans adopted, an unlinked orphan left and `cleaned_through` limiting the crash rule; a root below `expired_through` with no matching line kept; a restore that writes a `restore` segment and fails closed until root S + 1 exists; `verify-catalog` keyed by `(restore_epoch, seq)` across a restore; an edited segment line and a deleted root, each detected. The stale-base case accepts `ConflictError`, since statcatlib raises a conflict there (E4). | GH-125's first acceptance item | Part 4's conformance test; no other part |
| N4 | `live_at(fetch, at) -> dict[str, str]`, or a reader that gives it: the live edition of each product at an instant, from the checkpoint at or before it and the change lists after it, read from retained roots and segment lines. | GH-125's "live at time T" acceptance item | Nothing. Part 3's test carries its own replay until sdmxlib ships one. |
| N5 | The spec text of S31 to S43, as the owner accepts them. | each row's part | the part each row names |
| N6 | `stage_2 = True` in sdmxlib's own reference writer, or a statement that the stage-2 cases run against statcatlib's writer alone. Spec §6.9 says they "also run against statcatlib's writer". | N3 | Nothing in statcatlib |
| N7 | A release, 0.89.0, with N1 to N3. statcatlib raises its cap to `sdmxlib>=0.89,<0.90` in the part that first uses it, and sdmxlib-rest follows. | Part 3 | Part 3, under Q1 (a) |

sdmxlib needs no release before Part 1 or Part 2: both run on 0.88. Under Q1 (a), 0.89.0 with N1
ships before Part 3 lands, and N2 and N3 before Part 4. Under Q1 (b) no part waits for an sdmxlib
release, and N3 closes GH-125's first acceptance item whenever it ships.

### The hub

S31 to S34 change the suite design. The hub session takes them there as revision 6, with its own
review, and S31 to S43 to sdmxlib's spec through N5.

### The release-job design's Part 6

Handoff row 11 of the release-job design re-reviews Part 6 against this design. Section E16
lists the changes Part 6's text needs (R1 to R10). This design edits no line of
`docs/design/release-job.md`.

## Critical User Journeys (CUJs)

### CUJ 1: A laptop owner opts in and expires

A catalog directory holds 400 Inventory versions. The owner upgrades every tool that reads it,
then runs `statcat admin opt-in ./catalog`, which prints `since_seq`, the head and what the step
changes, and writes nothing. `statcat admin opt-in ./catalog --apply` writes `inventory/HISTORY`
and `catalog.json` 2.0. A month later `statcat prune ./catalog --older-than 30
--expire-older-than 31 --reason "monthly prune"` prints the plan, and with `--apply` retires old
editions in one `retire` version, then writes one segment, advances `HISTORY`, and deletes the
expired roots and the leaves no retained root names. `statcat doctor ./catalog` shows
`expired_through`, the newest segment and the newest checkpoint.

### CUJ 2: A seal races `prune`

A build script seals product `lfs` while `statcat prune --apply` expires. Whichever takes
`LOCK` first runs alone. When the seal goes first, its root is the head, and expiry then chooses
below it and keeps every leaf it names. When expiry goes first, the seal's `try_commit` waits,
then reads `HISTORY` and the head's bytes, writes its leaves and its root, and every leaf its
root names exists.

### CUJ 3: A crash during expiry

`prune` is killed after it advanced `HISTORY` and deleted half the candidate roots. The OS
releases the lock. Readers are unaffected: `CURRENT` names a root expiry never takes, and the
fallback's lower bound is `expired_through`. The next `prune` finds the pending segment after
`cleaned_through`, deletes each remaining root whose bytes match its line, deletes the leaves,
and advances `cleaned_through`.

### CUJ 4: An operator restores an opted-in catalog

A disk fault damages the three newest roots. `statcat verify-catalog` names them. The operator
runs `statcat admin restore ./catalog --to 412 --reason "jdoe: disk fault"`, which prints the
roots it would discard, the transitions they hold and the editions it would tombstone, and
writes nothing. With `--apply` it writes a `restore` segment that lists roots 413 to 415 and
marks the unreadable ones `unread`, writes `HISTORY` with epoch 1, bumps `catalog.json`, deletes
the three roots, and writes root 413 of epoch 1 with `event.kind` `restore` and the embargo on.
The operator repeats the cancellations the report lists and turns the embargo off.

### CUJ 5: An older tool meets an opted-in catalog

A script on statcatlib 0.16 opens the opted-in catalog. `Catalog.open` and every view work. Its
next `seal` raises `InventoryWriteRefusedError`, naming format 2.0, before it writes anything. A
script on statcatlib 0.13 fails at `Catalog.open` with `InventoryError`.

## Technical Plan (Architecture)

### Design decisions

| ID | Decision | Settles |
|---|---|---|
| E1 | The local lock is an OS advisory lock on `<catalog>/LOCK`, polled for at most 120 s. | GH-125 part 2, H12 |
| E2 | `InventoryStorage` gains `exclusive()`, `check()` and `stage_2`. | release-job D13's interface |
| E3 | `try_commit` holds the lock from before its `catalog.json` decode to after `CURRENT`. | GH-125 part 2 |
| E4 | The stale-base refusal is a conflict that `commit` retries; an unfinished restore is a refusal. | spec §6.6 rule 5 |
| E5 | A filesystem that refuses advisory locks keeps 1.x catalogs writable and refuses stage 2. | Q2 |
| E6 | `CatalogAdmin.opt_in` writes `HISTORY`, then `catalog.json` 2.0, under the lock. | spec §6.1 |
| E7 | The writer decodes as a stage-2 writer where its storage locks. | spec §6.1 Decoding |
| E8 | The engine is one function over `ExpiryStorage`, which extends `InventoryStorage`. | release-job D13, Q9 |
| E9 | Steps 0 to 2 read whole roots and stop at the first root that is neither a checkpoint nor old enough. | spec §6.9 steps 0 to 2 |
| E10 | Step 3 plans segments per epoch, adopts linked orphans, and reads segment headers by range. | spec §6.9 step 3 |
| E11 | Steps 4 to 7 work from the pending segments, so a normal run and a recovery share one path. | spec §6.9 steps 4 to 7, Crashes |
| E12 | The deletion log is a stream of events; a dry run takes no lock and writes nothing. | spec §6.9 step 8 |
| E13 | `prune` expires when given `inventory_window`, by the storage's clock. | Q4, Q5 |
| E14 | `CatalogAdmin.restore` runs spec §6.9's order with S25's tombstones, and resumes a crashed restore. | spec §6.9 Restores |
| E15 | `verify-catalog` runs sdmxlib's history checks; `doctor` reports `HISTORY`, segments and the lock. | spec §6.9 `verify-catalog` |
| E16 | GC is unchanged but for one stop; the release job keeps its 2.x refusal until Part 6. | release-job D1, D10, D13 |
| E17 | The pure rules come from sdmxlib 0.89; the engine holds the I/O and its order. | Q1 |

### E1: the local catalog lock

`LocalInventoryStorage.exclusive()` opens `<catalog>/LOCK` with `os.open(path, O_RDWR | O_CREAT)`,
locks it, yields, unlocks it and closes it. The file is created by the first call, stays empty,
and is never deleted or read. The OS drops the lock when the process exits, so a killed holder
leaves `LOCK` in place and the next commit proceeds.

- **POSIX.** `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, retried every 50 ms. `flock` locks belong to
  the open file description, so two threads of one process that each open `LOCK` exclude each
  other, and a second process does too.
- **Windows.** A one-byte exclusive lock at offset 0, taken with
  `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` and retried on the same period. The C runtime
  implements it with `LockFile`, the byte-range lock `LockFileEx` takes with
  `LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY` (S39; Not confirmed).
- **The wait.** `LOCK_TIMEOUT` is 120 s, the figure S26 gives the Azure lease (Q9). A commit that
  waits longer raises `CatalogLockTimeoutError`, a subclass of `InventoryWriteRefusedError`, which
  names `LOCK` and says that another process holds it. A seal refused this way keeps its journal
  entry, as it does for every `InventoryWriteRefusedError`.
- **Release.** The holder calls `flock(fd, LOCK_UN)` before `os.close(fd)`, so a child forked
  while the lock was held does not keep it through its copy of the descriptor.
- **Not reentrant.** A process-wide table maps each resolved `LOCK` path to the thread that holds
  it. A thread that calls `exclusive()` while it holds the same lock gets `RuntimeError` at once,
  through any storage object. Code that holds the lock and writes a root calls
  `InventoryWriter._commit_locked` (E3).

`prune` never nests the lock. It commits its `retire` version through `commit`
(`admin.py:693`), whose `try_commit` takes and releases the lock, and only then calls the
engine, which takes the lock for the run. A seal may commit between the two; expiry chooses under
its own lock from the head it then finds.

| Option | Gain | Cost |
|---|---|---|
| **`flock` and `msvcrt.locking`, polled (chosen).** | Standard library only, no `Any`, a bounded wait, thread exclusion by open file description. | The Windows call is `LockFile` under the C runtime, where the spec names `LockFileEx` (S39). No Windows machine runs the gate. |
| `LockFileEx` through `ctypes`. | The call the spec names. | `ctypes` signatures to maintain and type, for the same lock. |
| `fcntl.lockf` (POSIX record locks). | Works on more network filesystems. | The process loses the lock when any descriptor of the file closes, and threads of one process do not exclude each other. |
| A third-party lock package. | One call for both platforms. | A new base dependency for 40 lines. |
| A lock file created with `O_EXCL` and unlinked on release. | No OS lock needed. | A killed holder leaves a stale lock, which H12 rules out. |
| A reentrant lock. | `prune` and a restore could call `commit` while holding it. | Reentrancy by thread hides which call owns the lock; the suite design chose ordering (Alternatives considered). |

### E2: `exclusive()`, `check()` and `stage_2` on `InventoryStorage`

`InventoryStorage` (`store/inventory.py:322-353`) gains three members:

```python
class InventoryStorage(Protocol):
    @property
    def stage_2(self) -> bool: ...
    def exclusive(self) -> AbstractContextManager[None]: ...
    def check(self) -> None: ...
```

- `exclusive()` holds the catalog lock for the block. It waits at most `LOCK_TIMEOUT`, raises
  `CatalogLockTimeoutError` after it, and `CatalogLockUnsupportedError` where the store cannot
  lock.
- `check()` raises `CatalogLockLostError` when the lock held through `exclusive()` can no longer
  be trusted. The engine and a restore call it before each destructive call: a segment write, a
  `HISTORY` or `catalog.json` write, a root or leaf delete. On local disk a live process cannot
  lose an OS lock, so `check()` returns; called outside `exclusive()` it raises `RuntimeError`.
- `stage_2` says whether `exclusive()` excludes expiry on an opted-in catalog. The writer picks
  its `catalog.json` decoder by it (E7).

| Storage | `stage_2` | `exclusive()` | `check()` |
|---|---|---|---|
| `LocalInventoryStorage` | `True` | E1 | returns while the lock is held |
| `BlobInventoryStorage`, until release-job Part 6 | `False` | yields at once, as today's writer takes no lock | returns |
| `BlobInventoryStorage` over `AzureStore`, from Part 6 | `True` | the `HISTORY` lease: the held lease a release run passes in, else one acquired with a wait of at most 120 s; a catalog with no `HISTORY` has no lease to take and yields | `LeaseLostError` when the last renewal is 40 s old or more |
| `BlobInventoryStorage` over another store | `False` | yields at once | returns |

release-job D13 asks for "`exclusive()` and `check()` on the storage". This design keeps both
and adds `stage_2`, because a storage with no lock must make the writer refuse a 2.x catalog,
and the decoder is where sdmxlib puts that refusal. All three error classes subclass
`InventoryWriteRefusedError`, and Part 6's `LeaseLostError` subclasses `CatalogLockLostError`
(R4).

### E3: what `try_commit` holds the lock over

`try_commit` (`store/inventory.py:623-697`) becomes the lock and a call:

```python
def try_commit(self, base, leaves, *, ..., kind, reason) -> Root:
    with self._storage.exclusive():
        return self._commit_locked(base, leaves, ..., kind=kind, reason=reason)
```

`_commit_locked` is today's body with the stage-2 checks added, in this order:

1.  read the clock when the caller gave no `now` (`:665` moves inside the lock, so a commit that
    waited records the time it wrote);
2.  `check_writable(base, now=now)` (`:666`): decode `catalog.json` (E7) and refuse a base more
    than 5 minutes ahead;
3.  on an opted-in catalog, read `inventory/HISTORY` and run E4's two checks;
4.  resolve the actor (`:667`) and read the base's stored bytes (`:668`);
5.  write the changed leaves (`:670-673`), where the local storage skips a leaf it finds present
    (`:385`);
6.  build and create the root (`:675-695`);
7.  advance `CURRENT` (`:696`).

GH-125 asks for the lock "from the first leaf existence check to `CURRENT`". This span starts
earlier, at step 1, so the `catalog.json` decode and the `HISTORY` read are under the lock too:
an opt-in or an expiry then lands wholly before or wholly after a commit's checks.

`commit` (`:713-771`) is unchanged. It reads the head (`:747`), computes the change and runs the
cross-product rules outside the lock, so the lock is held for the writes of one version, a few
milliseconds on local disk.

`check_writable` stays public and lock-free for the seal's pre-check
(`store/working_copy.py:1622`). It runs steps 2 and, best effort, 3; `_commit_locked` repeats
both under the lock.

| Option | Gain | Cost |
|---|---|---|
| **The lock in `try_commit`, from the decode to `CURRENT` (chosen).** | `try_commit` is public, so every path that writes a root is covered: `commit`, the conformance adapter, `inventory_corpus`. | A direct `try_commit` caller that holds the lock deadlocks itself; the thread table turns that into `RuntimeError`. |
| The lock in `commit`, around the head read and the change. | The base is never stale. | The lock is held across the change callable, which reads every leaf and runs the cross-product rules: seconds at 7,000 products. `try_commit` callers go unlocked. |
| The lock from the first leaf write only (GH-125's wording). | The shortest hold. | An opt-in can land between the decode and the leaf check. |

### E4: the stale-base refusal and the unfinished-restore refusal

On an opted-in catalog `_commit_locked` reads `inventory/HISTORY` fresh, after the lock:

- **No `HISTORY`.** `InventoryWriteRefusedError`: a 2.x catalog without `HISTORY` is an error
  (spec §6.1).
- **Stale base.** When `base.seq <= HISTORY.expired_through`, it raises `StaleBaseError`, a
  subclass of `InventoryConflictError`. `commit` catches the conflict, re-reads the head and
  applies the change again (`:764-768`), which is the repair the spec's rule wants. A direct
  `try_commit` caller sees the conflict, as it does for a lost race. The restore's own commit
  skips this check (S33).
- **Unfinished restore.** When `HISTORY.restore_epoch` differs from `catalog.json`'s, or the
  base's `restore_epoch` differs from `HISTORY`'s, a restore stopped between its steps 2 and 5.
  The commit raises `InventoryWriteRefusedError` naming `statcat admin restore`, and stays
  refused until the restore is rerun (E14). The restore's own commit skips this check.

The expiry candidates are never at or above `CURRENT.seq` and never among the newest K, so a
base that `commit` read moments ago is stale only after K later versions and an expiry. The
check costs one read of a 300-byte file per commit on an opted-in catalog.

| Option | Gain | Cost |
|---|---|---|
| **A conflict, retried by `commit` (chosen).** | A seal that slept through an expiry lands on the true head. | N3's conformance case must accept `ConflictError`. |
| `InventoryWriteRefusedError`. | The word the spec uses. | A seal fails for a base its own retry loop would have replaced. |

### E5: a filesystem that refuses advisory locks

`flock` fails with `ENOLCK`, `ENOTSUP`, `EOPNOTSUPP` or `ENOSYS` where the filesystem has no
advisory locks, and `msvcrt.locking` fails with `EINVAL` or `EACCES` variants there (Not
confirmed). `LocalInventoryStorage.exclusive()` raises `CatalogLockUnsupportedError` for them.
`try_commit` then reads `catalog.json` without the lock:

- on a 1.x catalog it logs one WARNING per process and catalog, naming the directory and spec
  §6.6's "Unsupported locations", and commits as statcatlib 0.17 does;
- on a 2.x catalog it raises the error.

Opt-in, the engine and a restore raise it on any catalog, so a catalog on such a filesystem
never becomes 2.x through statcatlib. A filesystem that accepts `flock` and excludes no other
host is undetectable; spec §6.6 already documents network shares as unsupported, and
`statcat doctor` keeps its synced-folder warning.

### E6: opt-in through `CatalogAdmin`

```python
CatalogAdmin.opt_in(*, dry_run: bool = True) -> OptIn
statcat admin opt-in URL [--apply]
```

`OptIn` is frozen: `since_seq`, `head_seq`, `already`, `dry_run`. The method takes no reason,
since opt-in writes no Inventory version and no document has a field for one (R2). It runs
`InventoryWriter.opt_in`, in this order, under `exclusive()`:

1.  Refuse a storage that cannot run stage 2: `TypeError` over a `BlobInventoryStorage`, naming
    the release-job design's Part 6, and `CatalogLockUnsupportedError` from the lock (E5).
2.  Read `catalog.json` with its tag. A 2.x document with a valid `HISTORY` returns
    `OptIn(already=True)` and writes nothing; a 2.x document without `HISTORY` raises
    `InventoryError`.
3.  Read every root from the head down to 1. Refuse, with `InventoryWriteRefusedError` naming the
    root: a missing root, a root that does not decode, a head without the history fields, a root
    with the fields below a root without them, and a `previous_root_sha256` between `since_seq`
    and the head that does not hash the root before it (S41). `since_seq` is the lowest `seq` of
    the unbroken run of roots with the fields that ends at the head, or 1 on a catalog with no
    version.
4.  A dry run returns here.
5.  Create `inventory/HISTORY`: `expired_through: 0`, no segment, `cleaned_through: null`, the
    catalog's `catalog_id` and `restore_epoch`. A `HISTORY` left by a crash after this step is
    kept when it holds exactly these values and refused otherwise.
6.  Replace `catalog.json` with format `"2.0"` and `history: {"since_seq": N}`, by read, compare
    and rename: `ExpiryStorage.replace` refuses when the file's tag differs from step 2's, so a
    `restore_epoch` changed since the read is refused.

A crash between steps 5 and 6 leaves a 1.x catalog with an unused `HISTORY`, which no reader
opens, and a rerun completes it. Opt-in never expires. The first expiry is a later `prune`
call, and step 1's stop at a root without the history fields covers a stage-1 writer that
decoded `catalog.json` before the rewrite (spec §6.1).

`statcat admin opt-in` prints, in a dry run and again with `--apply`: `since_seq`, the head, the
number of roots below `since_seq` that will never expire, and three statements. Opt-in cannot be
undone. statcatlib 0.13 and older cannot open the catalog, and 0.14 to the last release before
Part 1 can read it and cannot write it. A local sdmxlib-rest must run sdmxlib 0.85 or later,
which sdmxlib-rest 0.10.0 requires.

| Option | Gain | Cost |
|---|---|---|
| **A dry run by default, applied with `--apply` (chosen).** | The one-way step shows `since_seq` and its refusals first, as `prune` does. | Two invocations. |
| Apply at once. | One invocation. | A one-way change with no preview. |
| Opt in at `Catalog.create(history=True)`. | New catalogs expire from version 1. | A second way in; `opt_in` on an empty catalog does the same (S41). |

### E7: the writer's stage-2 decode, and older readers

`check_writable` (`store/inventory.py:548-574`) decodes with
`decoder="stage-2-writer" if self._storage.stage_2 else "stage-1-writer"` (`:570`). A local
writer therefore accepts a 2.x catalog from Part 1 on, and a blob writer keeps refusing one
until Part 6 sets `stage_2` for `AzureStore`. The module docstring (`:28-30`), the error's
docstring (`:123-132`) and `docs/catalog-format.md:68-69` change with it.

What an opted-in local catalog means for each reader and writer, checked in each version's
code:

| Tool | Reads | Writes |
|---|---|---|
| statcatlib 0.13 and older (sdmxlib 0.84 and older) | `Catalog.open` and `CatalogAdmin.open` raise `InventoryError`: `decode_catalog` there reads major 1 only. | none |
| statcatlib 0.14 to 0.17, and a 0.18 without Part 1 | Everything: the reader decodes majors 1 and 2 and its fallback reads `HISTORY`. `statcat verify-catalog` checks the head. | `seal`, the journal repair and every `CatalogAdmin` change raise `InventoryWriteRefusedError` and write nothing. `prune` with nothing to retire still deletes tombstoned edition directories and unlisted `data/` files, which touches no Inventory object. |
| statcatlib from Part 1 | Everything. | Everything, under the lock. |
| `statcat gc` 0.17 | Stops its walk at `expired_through` and stops the run on a missing root above it (`gc.py:303-332`). | none in `inventory/` |
| sdmxlib-rest | It reads no Inventory object; its consumer's provider does. With sdmxlib 0.85 or later (sdmxlib-rest 0.10.0 or later) the provider's reader has the fallback. With an older sdmxlib a reader whose `CURRENT` fails can take a checkpoint for the head. | none |

A stage-1 writer and a stage-2 writer may share a 1.x catalog. The first takes no lock, and
nothing there expires.

### E8: the expiry storage

The engine and a restore reach the catalog through `ExpiryStorage`, a `Protocol` in a new module
`store/expiry.py` that extends `InventoryStorage`:

```python
@frozen
class Listed:
    path: str            # from the catalog root
    etag: str | None

class ExpiryStorage(InventoryStorage, Protocol):
    def list(self, prefix: str, *, shallow: bool = False) -> Iterator[Listed]: ...
    def read_tagged(self, path: str) -> tuple[bytes, str | None] | None: ...
    def read_range(self, path: str, length: int) -> bytes | None: ...
    def etag(self, path: str) -> str | None: ...
    def delete(self, path: str, *, if_match: str | None = None) -> bool: ...
    def replace(self, path: str, data: bytes, *, etag: str | None) -> str | None: ...
```

It inherits `location`, `read`, `create`, `advance_current`, `now`, `actor`, `exclusive`,
`check` and `stage_2`.

| Method | Contract | Local disk (this design) | Azure (release-job Part 6) |
|---|---|---|---|
| `list` | Every object under `prefix`; with `shallow`, only its direct children | `os.scandir`, or a walk | `obstore.list`, or `list_with_delimiter` |
| `read_tagged` | Bytes and tag, or `None` when absent | the file's bytes and a tag from `st_ino`, `st_mtime_ns` and `st_size` | `_ObjectOps.read` (`store/blob.py:179-187`) |
| `read_range` | The first `length` bytes | a short read | `obstore.get_range` |
| `etag` | The tag alone, or `None` when absent | `os.stat` | `obstore.head` |
| `delete` | Delete; `False` when the object is absent or its tag differs from `if_match` | `os.unlink`; `if_match` is ignored, since the lock excludes every writer | `Delete Blob` with `If-Match`, through `AzureBlobRest` |
| `replace` | Replace `HISTORY` or `catalog.json` when its tag is `etag`, or create it when `etag` is `None`; `InventoryConflictError` otherwise; returns the new tag | stat, compare, stage, `os.replace`, fsync the directory | a conditional Put, with the lease ID for `HISTORY` |
| `create` | Create-if-absent, complete before visible (segments) | `create_exclusive` | `put(mode="create")` |

`LocalInventoryStorage` implements all of it in `store/inventory.py`. A tag is opaque to the
engine. Every local `replace` renames a new file over the name, so the tag changes with it.

Differences from the interface release-job D13 lists, each with its reason:

- `read(path) -> (bytes, etag)` is named `read_tagged`, because `InventoryStorage.read` already
  returns bytes alone and the reader's `Fetch` depends on that.
- `write_history(data, *, etag)` becomes `replace(path, data, *, etag)`. Opt-in and a restore
  rewrite `catalog.json` by the same read, compare and write (spec §6.1).
- `etag(path)` is added for step 6's final check, which needs a leaf's tag and no bytes.
- `list` gains `shallow`, so step 6 lists the roots without paging through every leaf.
- `read_range` stays and changes its use: segment headers in place of step 1's `created_at`
  (Q3).

| Option | Gain | Cost |
|---|---|---|
| **One protocol that extends `InventoryStorage` (chosen).** | The engine takes one object for the lock, the clock, the actor and the calls; Part 6 writes one class. | `LocalInventoryStorage` grows six methods. |
| A separate expiry storage beside the inventory storage. | `InventoryStorage` implementers are untouched. | Two objects that must name one catalog and one lock. |
| The engine written against local paths, with Part 6 writing its own. | No protocol. | Two engines, and the crash rules tested once per store. |

### E9: steps 0 to 2

```python
def expire(
    writer: InventoryWriter,
    *,
    keep_roots: int,
    window: timedelta,
    dry_run: bool = False,
    state_dir: Path | None = None,
) -> ExpiryReport
```

The writer's storage must be an `ExpiryStorage` with `stage_2`; any other raises `TypeError`.
`keep_roots` below 2 and a `window` that is not positive raise `ValueError`. A real run holds
`storage.exclusive()` from here to its report. It reads `catalog.json` and `HISTORY` with their
tags, and raises `ExpiryRefusedError`, a subclass of `InventoryWriteRefusedError`, for a 1.x
catalog, a 2.x catalog with no `HISTORY`, and an unfinished restore (E4). It reads `now` once,
from `storage.now()`.

**Step 0.** `writer.advance_current()` (`store/inventory.py:773-803`), then a read of `CURRENT`.
A `CURRENT` that is missing or malformed after the step ends the run with nothing taken.

**Step 1.** With the head from the reader protocol, walk `seq` upward from
`max(expired_through + 1, since_seq)`. The walk reads each root whole and treats it by the
first row that applies:

| The root | The walk | Event |
|---|---|---|
| is at or above `CURRENT.seq`, or above `head.seq - keep_roots` | stops | none |
| is missing, or does not decode | stops | `expiry.stopped_at_root`, warning |
| lacks the history fields | stops | `expiry.stopped_at_root`, warning; `verify-catalog` reports it |
| is a checkpoint | steps over it and goes on | none |
| has a successor whose `created_at` is at or after `now - window` | stops | none |
| otherwise | takes it as a candidate and goes on | none |

A root is a checkpoint when it is `since_seq`, when the root or line before it has another
`restore_epoch` or it is version 1, or when its `created_at` falls in a later UTC month than the
one before it (H4). The fact about `seq - 1` comes from the retained root, or from its segment
line when the root is expired; a line keeps `created_at` and `restore_epoch`. Under Q1 (a) the
walk is `sdmxlib.inventory.expiry_candidates` over facts the engine reads (N1).

The engine keeps, for each root it walks, the SHA-256 of its bytes, its segment line and its
leaf hashes, and frees the bytes. A first run over 2,979 roots therefore reads their 4.7 GB one
root at a time and holds only the lines and hashes.

**Step 2.** For every `seq` from the lowest candidate to the first retained root above the
highest, checkpoints included, `previous_root_sha256` must equal the SHA-256 of the bytes of
`seq - 1`. For the lowest candidate c, the hash of c - 1 is the `root_sha256` of its line in
the newest `expiry` segment reached from `HISTORY.newest_segment`, stepping over `restore`
segments, and the hash of retained root c - 1 when that segment holds no such line (S32). A
mismatch ends the run with nothing written,
`ExpiryReport.stopped` set, and `expiry.chain_mismatch` naming both versions.

| Option | Gain | Cost |
|---|---|---|
| **Whole roots in steps 1 and 2 (chosen).** | One read per root serves the history-field test, the hash, the line and the leaf set. | Q3 (a)'s 3.2 MB on a run that takes nothing. |
| A 1 KiB ranged read in step 1, whole roots in step 2. | The spec's wording. | Q3 (b). |

### E10: step 3, the segments

**The plan.** The engine lists `inventory/` shallowly and reads every root that is neither a
candidate nor already read, to learn the leaves each names. With `state_dir` it reads and
writes `<state_dir>/expiry-cache.json`: each root's path, tag, SHA-256 and leaf hashes, and each
segment's `previous_segment`. A root whose listed tag equals the cached one is not read again,
and the file holds hashes and paths only (S23). The file is written to a staged name and
renamed, and a missing or unreadable file costs one full read. Without `state_dir` every run
reads every retained root.

The candidates are grouped by `restore_epoch` in `seq` order, and each group is split at
`SEGMENT_MAX_LINES` lines or `SEGMENT_MAX_BYTES` of encoded bytes, whichever comes first. Each
segment's header holds `kind: expiry`, the group's epoch, `first` and `last`, `roots`, the
`actor` and `expired_at` from the storage, and `leaves`: every leaf a root of this segment
names that no root outside the candidates names. The first header's `previous_segment` and hash
are `HISTORY`'s `newest_segment` and its hash, and each later one names the segment before it.
A segment's path is `segment_path(epoch, first, last, str(uuid.uuid7()))`.

**Orphans.** The engine lists `history/` and reads each segment's header with `read_range`,
doubling the length from 64 KiB until it holds the first newline. It walks the chain from
`newest_segment` by `previous_segment`; with `state_dir` it reads only headers the cache lacks.
Every listed segment the walk does not reach is an orphan. The orphans whose `first` is the
lowest candidate are read whole and adopted when all of spec §6.9 step 3's conditions hold: the
first one's `previous_segment` and hash are `HISTORY`'s, each later one links to the one before
with a matching hash, the first line is the lowest candidate, every line is a current candidate
in order, each line's `root_sha256` equals the SHA-256 of its root's stored bytes, and the
header names this catalog and `kind: expiry`. The engine logs `expiry.adopted_segment` for each
and plans new segments only for the candidates above the last adopted line. Every other orphan
is logged as `expiry.orphan_segment` and left.

An adopted header's `leaves` was planned by the crashed run. Step 6 checks every listed leaf
against the retained roots again before it deletes, so a leaf a newer root has come to name
survives.

**The writes.** Before each `create` the engine calls `storage.check()`. It writes the segments
in chain order with `encode_segment`, and keeps each one's SHA-256 for the next header and for
`HISTORY`.

| Option | Gain | Cost |
|---|---|---|
| **Headers by `read_range`, the chain cached with `state_dir` (chosen).** | Every orphan is reported, as the spec asks; a run with the cache reads only new headers. | Without the cache a run reads one header per segment ever written: about 365 a year of nightly runs. |
| Read whole segments to walk the chain. | No ranged read. | A run reads the catalog's whole history, up to 64 MiB a segment. |
| Detect only the orphans that start at the lowest candidate, by name. | No chain walk. | Other orphans are found by `verify-catalog` alone, and the spec's alert for an orphan segment has no source. |

### E11: steps 4 to 7, and the crash rule after every step

**Step 4.** `check()`, then `replace("inventory/HISTORY", ..., etag=<the tag read at the start>)`
with `expired_through` set to the highest candidate, `newest_segment` and its hash set to the
last segment written or adopted, and `cleaned_through` unchanged. A run with no candidate skips
steps 3 and 4.

**Steps 5 to 7 work from the pending segments.** The engine walks back from `newest_segment` to
`cleaned_through` and takes the `expiry` segments between them, oldest first. After step 4 these
are the run's own segments and any a crashed run left. A `restore` segment in the range is
stepped over (S35). A run with no pending segment ends here.

**Step 5.** For each `seq` in a pending header's `roots`: `read_tagged` the root. An absent root
is done. A root whose SHA-256 equals its line's `root_sha256` is deleted, after `check()`, with
`if_match` set to the tag just read. Any other root is kept. The engine logs
`expiry.kept_root` for it when its `seq` is at or below `expired_through`, and logs nothing when
a later epoch's root has taken the name since a restore (S43).

**Step 6.** List `inventory/` shallowly again and read every root not yet known, so the retained
set includes the roots below `since_seq`, the checkpoints, the kept roots and any root committed
since the plan. When a retained root cannot be read or decoded, the step deletes no leaf, logs
`expiry.unreadable_root` and leaves `cleaned_through` where it is (S42). For each leaf in the
pending headers' `leaves` that no retained root names: `etag(leaf)`, then `check()`, then
`delete(leaf, if_match=<that tag>)`. That order is step 6's final check: on Azure a writer that
took the lease after a pause has Put the leaf, so the tag differs and the delete fails, or it
Puts the leaf afterwards and recreates it.

**Step 7.** When every delete of steps 5 and 6 succeeded or found nothing, `check()`, then
`replace` `HISTORY` with `cleaned_through` set to `newest_segment`. A failed delete is logged
as `expiry.delete_failed`, listed in `ExpiryReport.failed`, and leaves `cleaned_through`
unchanged, so the next run repeats steps 5 and 6.

| A crash | Leaves | The next run |
|---|---|---|
| in step 0 | `CURRENT` at the old or the new head | repeats step 0 |
| in steps 1 or 2 | nothing written | starts again |
| in step 3, or before step 4 | orphan segments; `HISTORY` unchanged; every root and leaf present | adopts the linked orphans whose lines are still candidates and writes the rest; logs and leaves any other |
| in step 4 | the old `HISTORY` or the new one, since the replace is atomic | as the row above, or as the row below |
| after step 4, in step 5 | `expired_through` advanced; some candidate roots still present at or below it | deletes each pending root whose bytes match its line, then steps 6 and 7 |
| in step 6 | some planned leaves deleted, none that a retained root names | repeats step 6, then step 7 |
| before or in step 7 | everything deleted; `cleaned_through` behind | finds nothing to delete and writes step 7 |
| in step 8 | the run's events may be lost; the header plans hold what was planned | nothing to do |

Between step 4 and the end of step 5 a reader with a good `CURRENT` reads as before, a reader on
the fallback starts above `expired_through` (spec §6.6), and a writer with a base at or below it
gets `StaleBaseError` (E4). A `CatalogLockLostError` from any `check()` ends the run at that
point with `ExpiryReport.stopped` set and `expiry.stopped` logged, which is one of the crash
rows above.

Leaves that a crashed commit wrote before its root, and leaves only a restore's discarded roots
named, appear in no header. The engine never deletes them (spec §6.9, Crashes).

### E12: step 8, the deletion log, the dry run and the report

Every event is a log record on the `statcatlib.expiry` logger that carries one JSON object,
written with `statcatlib.jobs._emit` (`jobs.py:665`), the helper the release job and GC use.
`statcat prune` prints each as one line, and Part 6's `statcat release` prints them on standard
output with its other events.

| Event | Severity | When |
|---|---|---|
| `expiry.planned` | info | Each root and leaf the run plans to delete, with `path`, `reason` (`expired-root` or `unreferenced-leaf`) and `segment` |
| `expiry.deleted` | info | Each object deleted |
| `expiry.delete_failed` | error | A delete the store refused, with `error` |
| `expiry.adopted_segment`, `expiry.orphan_segment` | info, warning | Step 3 |
| `expiry.kept_root` | warning | Step 5: a root at or below `expired_through` that matches no pending line |
| `expiry.stopped_at_root`, `expiry.chain_mismatch`, `expiry.unreadable_root` | warning | Steps 1, 2 and 6 |
| `expiry.stopped` | warning | The lock was lost, or on Azure the 30-minute cap was reached |
| `restore.planned`, `restore.discarded`, `restore.tombstoned` | info | A restore (E14) |

The deletion log is the `expiry.planned` and `expiry.deleted` lines. The durable record of what
a run planned is each segment's header, which is in the catalog and never deleted.

A dry run takes no lock, moves no `CURRENT`, writes no segment, no `HISTORY` and no cache file,
and deletes nothing (S43). It performs steps 1 and 2 against the `CURRENT` it finds, reads the
retained roots to plan, and reports what steps 3 to 7 would do: the segments by epoch with their
line counts, the orphans it would adopt and leave, the new `expired_through`, every root and
leaf it would delete, and the pending segments of a crashed run it would finish.

`ExpiryReport` is frozen: `candidates`, `checkpoints` (stepped over), `segments` (written),
`adopted`, `orphans`, `expired_through`, `deleted_roots`, `deleted_leaves`, `kept_roots`,
`failed` (path to error), `stopped` (the reason, or `None`), `current_seq` and `dry_run`.

### E13: `prune` with expiry, and the time source

```python
CatalogAdmin.prune(
    *, older_than: timedelta, reason: str, dry_run: bool = True,
    inventory_window: timedelta | None = None, keep_roots: int = 10, state_dir: Path | None = None,
) -> PruneReport
```

```text
statcat prune PATH --older-than DAYS --reason TEXT [--expire-older-than DAYS] [--keep-roots N]
                   [--state-dir DIR] [--apply]
```

Without `inventory_window`, `prune` does what it does today and expires nothing, on every
catalog (Q4). With it:

1.  `inventory_window` below `older_than` plus one day raises `ValueError` naming both (spec
    §11.6's inequality, with `prune`'s window as the `data/` GC window; S37), and so does
    `keep_roots` below 2. A 1.x catalog raises `ExpiryRefusedError` naming
    `statcat admin opt-in`. These run before the retire step.
2.  `prune` runs its three steps as today (`admin.py:612-653`). The `retire` commit takes and
    releases the lock inside `commit` (`admin.py:693`).
3.  It calls `expire(self._writer, keep_roots=..., window=inventory_window, dry_run=dry_run,
    state_dir=...)`, which takes the lock for the run.

`PruneReport` gains `expiry: ExpiryReport | None`, and `statcat prune` prints its lines after
the retire and delete lines. A dry run of `prune` plans expiry against the head as it is. A
real run that writes a `retire` version first has a head one higher, so it can take one root
more than the dry run shows.

**The time source.** The engine reads `storage.now()` once per run and compares each candidate's
successor's `created_at` with `now - window`.

| Store | `now()` | Guard |
|---|---|---|
| Local disk | The machine's clock (`store/inventory.py:409-411`), P1's rule | The writer already refuses a base more than 5 minutes ahead of the clock, so `created_at` values track the clocks that wrote them. A clock set forward expires early; K roots, the checkpoints and every line stay. |
| Azure (Part 6) | The `Date` of an anonymous `HEAD` (`store/blob.py:562-586`) | A machine clock more than 30 s ahead of it is refused there (`MAX_CLOCK_SKEW`). |

| Option | Gain | Cost |
|---|---|---|
| **`storage.now()`, read once (chosen).** | One rule for both stores, and the one every other local change uses. | Q5 (a). |
| The head root's `created_at`. | Immune to a wrong laptop clock. | Q5 (b). |

### E14: restores

```python
CatalogAdmin.restore(to: int, *, reason: str, dry_run: bool = True) -> RestoreReport
statcat admin restore URL --to SEQ --reason TEXT [--apply]
```

A restore discards every root above S and makes root S the base of a new epoch. It is the remedy
for damaged or lost roots and for a failover. `CatalogAdmin.rollback` (`admin.py:361-411`) is a
different operation and keeps its meaning on an opted-in catalog: it changes one product's leaf
in one new version of kind `rollback`, under the lock like any commit, and discards nothing. Its
reach is the `superseded` entries of the head's leaf, which expiry never shortens, since a leaf
keeps an entry until it is retired.

`restore` raises `ExpiryRefusedError` on a 1.x catalog and for S below `since_seq - 1` (Q8), and
`ValueError` for a blank reason. Under `exclusive()`, with `check()` before each write and
delete, it runs spec §6.9's order:

**Before step 1.** Read `catalog.json` and `HISTORY`. Root S must exist, decode, and have every
leaf it names present and hashing to its name. Where S's successor on the head chain is
readable, as retained root S + 1 or as the `expiry` line of S + 1, its `previous_root_sha256`
must hash root S's bytes, which shows S is on the head chain. List the roots above S and read
each, with its leaves. A dry run reports and returns here.

1.  **The `restore` segments.** One group per `restore_epoch` of the roots above S, in `seq`
    order, split as E10 splits. A header holds `kind: restore`, the old epoch, `roots` (every
    listed root above S in the group), `leaves: []` and `unread` (each listed root that could
    not be read or decoded). An unread root goes in the group of the nearest readable root below
    it, or of the current epoch. The first header links to `HISTORY.newest_segment`. A rerun
    adopts an orphan `restore` segment a crashed attempt left when it links the same way, lists
    the same roots and each line matches its root's bytes (S35).
2.  **`HISTORY`:** the epoch plus 1, `expired_through` the smaller of its old value and S,
    `newest_segment` the last `restore` segment, `cleaned_through` unchanged.
3.  **`catalog.json`:** `restore_epoch` plus 1, by `replace`.
4.  **Delete** every root step 1 listed, unread ones included.
5.  **Root S + 1** through `InventoryWriter._commit_locked(base=<root S>, leaves={}, ...,
    kind="restore", reason=reason, restoring=True)`, which skips E4's two checks. It carries the
    new epoch, a `previous_root_sha256` that hashes root S, and `retired`: root S's tombstones,
    and one tombstone, with `retired_at` the restore's time, for each edition that a discarded
    root it could read records in a leaf and that root S records in neither a leaf nor `retired`
    (S25; `restore_retired` under N1). Its change list holds those tombstones as
    `from: null, to: tombstone` items. Under Q6 (b) it sets `embargo: true`.
6.  **`CURRENT`**, inside the same `_commit_locked`.

A restore deletes no leaf and never deletes or overwrites a segment. When the head is root
S + 1 of kind `restore`, built on root S, a call with the same `to` returns that restore's
report and writes nothing.

| A crash | Leaves | The rerun |
|---|---|---|
| before step 2 | an orphan `restore` segment; a working catalog on the old epoch | adopts the segment, or the operator abandons the restore and the orphan is reported |
| after step 2 or 3 | `HISTORY` on the new epoch with no root in it; readers whose `CURRENT` fails fail closed; writers and expiry refuse (E4) | finds the newest segment is a `restore` segment of the epoch before `HISTORY`'s with no root of the new epoch at `first`; takes S as `first - 1`; refuses another `to`; resumes at the first step not done |
| in step 4 | some discarded roots deleted | deletes the rest |
| in step 5 or 6 | root S + 1 may exist, and `CURRENT` may lag | writes root S + 1 if absent; `advance_current` |

`RestoreReport` is frozen: `to`, `epoch`, `discarded` (the `seq` values), `unread`,
`transitions` (each discarded root's change items, as product, edition, `from` and `to`),
`tombstoned` (the editions step 5 tombstones), `segments`, `seq` (root S + 1's) and `dry_run`.
`transitions` is what the operator repeats by hand: a cancellation, a rollback or a release
made after S.

| Option | Gain | Cost |
|---|---|---|
| **One command that runs the six steps and resumes (chosen).** | The order and the crash rule are code, tested step by step. | A new public method. |
| A runbook of manual steps, as the tests do today. | No code. | A missed step loses the discarded roots' record or leaves the catalog failing closed. |

### E15: `verify-catalog` and `doctor`

`verify_catalog` (`diagnostics.py:322-374`) keeps its checks of the head: each leaf, each
entry's manifest SHA-256, routing fields and artefact records, and `check_edition` on every
edition. On a catalog whose head carries the history fields it adds, through
`sdmxlib.inventory.check_history` (N2) over the listed roots and segments:

| Spec §6.9 check | What is checked | Source |
|---|---|---|
| 1 | The root chain over retained roots and segment lines, keyed by `(restore_epoch, seq)`; a key in both a root and a line has one `root_sha256` | `check_history` |
| 2 | Every retained root's leaves: present, hashing to their names, valid (S38) | `check_history` |
| 3 | Segment links, hashes and kinds; `HISTORY`: every head-chain root above `expired_through` exists, no `expiry` line on the head chain lies above it, `cleaned_through` is on the chain at or below `newest_segment`; every root at or above `since_seq` carries the history fields; the checkpoint rule recomputed from roots and lines; unlinked and overlapping segments | `check_history` |
| 3, orphans | An orphan segment is a note (Q7) | `check_history`, as its own finding kind |
| 4 | Each filled `artefacts`, `dataflows` and `pull_bytes` against its manifest | `verify_entry`, since 0.17 |

On a 1.x catalog check 1 runs over the roots that carry the history fields, and check 3 has
nothing to read. An opted-in catalog with an unfinished restore is one finding that names the
rerun. `VerifyReport` gains `notes`, and `statcat verify-catalog` (`cli/__init__.py:514-550`)
prints each as an `ORPHAN` line that leaves the exit status alone, as a `STALE` line does.

The conformance suite reaches these checks through N3's `ExpiryBackend.verify()`, which the
adapter in `tests/test_inventory_conformance.py` maps to `verify_catalog`. The cases that come
from sdmxlib are the chain keyed by `(restore_epoch, seq)` across a restore, the edited segment
line and the deleted root. statcatlib's own tests cover what the suite cannot build: an edition
whose manifest differs, and the report's files and exit status.

`doctor` (`diagnostics.py:577-627`) gains these items, all read-only and none walking the whole
history:

| Topic | Level | Line |
|---|---|---|
| `catalog.json` | info | adds `format 2.0, history since version N` to today's line (`diagnostics.py:425-431`) |
| `LOCK` | info, or warning | absent; present and free; or held by another process now, found with a non-blocking attempt that is released at once. Warning when the filesystem refuses the lock (E5). |
| `HISTORY` | info, or warning | `expired_through`, `newest_segment`, `cleaned_through`. Warning when a 2.x catalog has none, when it does not decode, and when `cleaned_through` is behind `newest_segment`, naming the next `prune` as the repair. |
| checkpoint | info | the newest checkpoint: its `seq` and `created_at`, found by reading roots down from the head, at most one month of them |
| segments | warning | each orphan segment, and each segment on the chain whose link or hash fails, from the headers alone |
| restore | warning | an unfinished restore, with the command that resumes it |

`_leftovers` (`diagnostics.py:567-574`) is unchanged: `LOCK` matches neither of its patterns.

### E16: GC, the release job and the split with Part 6

**`statcat gc`.** GC 0.17 already reads an opted-in catalog: it takes `expired_through` as its
floor and stops the run on a missing root above it (`gc.py:303-332`, `:352-377`). With one jobs
configuration, expiry never takes a root GC needs. Expiry takes root N only when root N + 1 was
created before `now - inventory_window`, and `inventory_window >= gc_window + 1d`
(`jobs.py:170`, `RetentionPolicy.check`), so root N + 1 is older than GC's cutoff and GC's walk
ends at or above it, with a day of margin between the two jobs' clock reads. S25's tombstones in
root S + 1 keep a discarded edition's files, since GC keeps every tombstone retired inside the
window (`gc.py:379-396`). Part 5 adds one stop to `expired_through()` (`gc.py:303-317`): an
unfinished restore, detected as in E4, raises `GcStoppedError`.

**The release job.** `ReleaseJob.run` refuses an opted-in catalog at its start
(`release.py:754-760`). This design leaves that refusal in place, and a blob writer still
refuses a 2.x catalog through `stage_2` (E2), so no Azure catalog is opted in before Part 6:
opt-in over a `BlobLocation` raises `TypeError` until then (E6). Part 6 removes the refusal,
takes the lease in step 1, passes it to `BlobInventoryStorage(lease=)`, and calls `expire` in
step 7.

**The split.**

| Piece | This design | release-job Part 6 |
|---|---|---|
| The lock | `flock` on `LOCK`; the protocol members | `AzureHistoryLease` behind them; the lease on every blob commit; the 30-minute cap |
| Opt-in | `InventoryWriter.opt_in`, local | the same method over `BlobExpiryStorage`, with `HISTORY` created if absent and `catalog.json` replaced by its ETag |
| The engine | `expire`, `ExpiryStorage`, the cache file, the events | `BlobExpiryStorage`; the REST verbs for lease, `If-Match` delete and lease-ID Put |
| Gates | none on local disk | `not_before`, the pod check |
| The cache | `state_dir=` and the file format | `--state-dir` on a persistent volume (S23) |
| Restore | `restore`, local | the same code over `BlobExpiryStorage` (R9) |
| `verify-catalog`, `doctor` | local | not planned |

**The Azure pieces, in plain terms.** Part 6 builds them; the engine sees each only through
`ExpiryStorage`.

| Azure term | What it is | Where the engine meets it |
|---|---|---|
| Blob lease | A lock Azure keeps on one blob. One holder at a time gets a lease ID for 15 to 60 s and renews it. A write to the leased blob must carry the ID. | `exclusive()` takes it on `inventory/HISTORY`, and `check()` asks how old the last renewal is. |
| ETag | A version stamp Azure returns with every blob and changes on every write. | The `etag` values of `list`, `read_tagged` and `etag`. |
| `If-Match` | A request header that makes a write or delete apply only while the blob still has the given ETag. | `delete(if_match=)` and `replace(etag=)`. |
| Conditional create | A Put that fails when the name exists. | `create`, which the writer already uses for roots. |

Expiry adds one data action to what the release identity uses today. It already reads, writes
and lists blobs in the catalog's container: the fold lists `pending/`, and every commit writes
`inventory/`. Expiry also deletes blobs, under `inventory/` only. The lease needs no further
action if Azure authorises a lease request under the blob write action (Not confirmed). The
engine needs no role at the account or above, no key and no SAS, and it sends three kinds of
request `obstore` cannot: the lease, the `If-Match` delete and the Put with a lease ID. Every
other call goes through `obstore`. An RBAC role on a container covers every path in it, so a
delete right scoped to `inventory/` alone needs the ACL-only setup spec §10.1 describes for a
hierarchical-namespace account; Part 6 states which of the two the deployment uses.

**Changes Part 6's provisional text needs.** The owner decides each when the release-job design
is re-reviewed (its handoff row 11).

| # | release-job text | Change |
|---|---|---|
| R1 | D13's `AzureBlobRest.get_range`, "which `obstore` cannot send (Phase 4 F14)" | Drop the verb. `obstore` 0.12.1, the version this checkout resolves, exports `get_range`, and Phase 4's F14 lists the lease, the `If-Match` delete and the lease-ID Put only. The engine reads segment headers with it and reads roots whole (Q3). |
| R2 | D13's `CatalogAdmin.opt_in(reason)` | `opt_in(*, dry_run=True)`: opt-in writes no version, so nothing records a reason (E6). |
| R3 | D13's engine interface: `read`, `write_history` | `ExpiryStorage` as E8 defines it: `read_tagged`, `replace`, `etag`, `list(shallow=)`. |
| R4 | `LeaseLostError` | Subclass `CatalogLockLostError`; the 120 s wait raises `CatalogLockTimeoutError`. |
| R5 | Part 6's `release.py` entry: "the leaf-set cache" | The cache is the engine's (`store/expiry.py`). Part 6 passes `state_dir`. |
| R6 | D13's lease on writes | `BlobInventoryStorage.stage_2` is true over `AzureStore`; `exclusive()` yields at once on a catalog with no `HISTORY`. |
| R7 | D14's expiry events | Add E12's `expiry.planned`, `expiry.deleted`, `expiry.delete_failed`, `expiry.adopted_segment`, `expiry.stopped_at_root`, `expiry.chain_mismatch` and `expiry.unreadable_root`. |
| R8 | D1's step 7 and `--dry-run` | A dry run with `--expire` takes no lease and calls `expire(dry_run=True)`. |
| R9 | Part 6's file list | Add `CatalogAdmin.restore` over a `BlobLocation`, with its tests on Azurite. A server restore is the case S25 was written for, and no part builds it today. |
| R10 | D13's gate 2 | sdmxlib-rest reports `sdmxlib_version` on its `main` (`af53a59`) and in no release: 0.10.3's `app.py` lacks it. Gate 2 needs the release that carries it. |

### E17: where the pure rules live

Under Q1 (a) the engine calls sdmxlib for every decision that reads no storage, and holds the
reads, the writes, their order, `check()` and the events.

| Rule | sdmxlib (N1, N2) | statcatlib |
|---|---|---|
| Is root N a checkpoint | `is_checkpoint` | reads the facts |
| Which roots step 1 takes, and why the walk stopped | `expiry_candidates` | reads the roots, emits the event |
| How segments split | the two constants | groups, encodes, writes |
| The restore's tombstones | `restore_retired` | reads the discarded roots' leaves |
| The history checks | `check_history` | lists roots and segments, maps findings to files |
| The chain link of step 2, orphan adoption, the pending walk | none | `store/expiry.py`, since each mixes reads with the decision |

| Option | Gain | Cost |
|---|---|---|
| **Rules in sdmxlib 0.89 (chosen, Q1 a).** | One normative checkpoint rule for the writer, the verifier and a later exporter. sdmxlib's reference writer can run N3's cases. | Part 3 waits for a release. `check_history` and the engine then share `is_checkpoint`, so a fault in it is not caught by `verify-catalog`; N3's cases state the expected checkpoints by hand to cover that. |
| Rules in statcatlib. | No release to wait for. | Q1 (b). |

### Errors

| Site | Condition | Raises or reports |
|---|---|---|
| `exclusive()`, local | The lock is held for 120 s | `CatalogLockTimeoutError` |
| `exclusive()`, local | The filesystem refuses the lock | `CatalogLockUnsupportedError`; on a 1.x commit a WARNING and an unlocked commit (E5) |
| `exclusive()` | The calling thread holds this lock | `RuntimeError` |
| `check()` | Called outside `exclusive()` | `RuntimeError` |
| `_commit_locked`, 2.x catalog | No `HISTORY`, or it does not decode | `InventoryWriteRefusedError` |
| `_commit_locked`, 2.x catalog | `base.seq <= expired_through` | `StaleBaseError`, retried by `commit` |
| `_commit_locked`, 2.x catalog | An unfinished restore | `InventoryWriteRefusedError` naming `statcat admin restore` |
| `check_writable`, blob storage | A 2.x catalog, until Part 6 | `InventoryWriteRefusedError`, from the stage-1 decode |
| `opt_in` | A blob storage | `TypeError` naming Part 6 |
| `opt_in` | A missing or undecodable root, a head without the history fields, a root with the fields below one without, a broken chain | `InventoryWriteRefusedError` naming the root |
| `opt_in` | `catalog.json` changed since the read | `InventoryConflictError` |
| `expire`, `restore` | A storage that is no `ExpiryStorage` with `stage_2` | `TypeError` |
| `expire`, `restore`, `prune(inventory_window=)` | A 1.x catalog; a 2.x catalog with no `HISTORY`; for `expire`, an unfinished restore | `ExpiryRefusedError` |
| `prune` | `inventory_window < older_than + 1d`, `keep_roots < 2` | `ValueError` |
| `expire`, step 1 | A missing, undecodable or field-less root | the walk stops; `expiry.stopped_at_root` |
| `expire`, step 2 | A chain mismatch | nothing written; `ExpiryReport.stopped`; `expiry.chain_mismatch` |
| `expire`, any `check()` | The lock is lost | the run ends; `ExpiryReport.stopped`; `expiry.stopped` |
| `expire`, step 5 | A root that matches no pending line | kept; `expiry.kept_root` |
| `expire`, step 6 | An unreadable retained root | no leaf deleted; `expiry.unreadable_root` |
| `expire`, steps 5 and 6 | A delete the store refuses | `expiry.delete_failed`; `cleaned_through` unchanged |
| `restore` | Root S missing, undecodable, with a missing leaf, or off the head chain | `ExpiryRefusedError`, nothing written |
| `restore` | S below `since_seq - 1`; a rerun with another `to` | `ExpiryRefusedError` |
| `collect_garbage` | An unfinished restore | `GcStoppedError` |

`CatalogLockTimeoutError`, `CatalogLockUnsupportedError`, `CatalogLockLostError`,
`StaleBaseError` and `ExpiryRefusedError` are new. The first three and the last subclass
`InventoryWriteRefusedError`; `StaleBaseError` subclasses `InventoryConflictError`.

### Symbols

| Symbol | Module | Signature | Part |
|---|---|---|---|
| `LOCK_NAME`, `LOCK_TIMEOUT`, `LOCK_POLL` | `store/lock.py` (Create) | `"LOCK"`, 120 s, 50 ms | 1 |
| `hold` | `store/lock.py` | `(path: Path, *, timeout: timedelta) -> AbstractContextManager[None]`: the OS lock and the thread table | 1 |
| `InventoryStorage.exclusive`, `.check`, `.stage_2` | `store/inventory.py` | E2 | 1 |
| `InventoryWriter._commit_locked` | `store/inventory.py` | `try_commit`'s arguments, plus `restoring: bool = False` | 1 |
| `InventoryWriter.history` | `store/inventory.py` | `() -> History \| None`: `HISTORY` decoded, `None` on a 1.x catalog | 1 |
| `CatalogLockTimeoutError`, `CatalogLockUnsupportedError`, `CatalogLockLostError`, `StaleBaseError` | `store/inventory.py` | E2, E4 | 1 |
| `ExpiryStorage`, `Listed` | `store/expiry.py` (Create) | E8 | 2 |
| `LocalInventoryStorage.list`, `.read_tagged`, `.read_range`, `.etag`, `.delete`, `.replace` | `store/inventory.py` | E8 | 2 |
| `InventoryWriter.opt_in`, `CatalogAdmin.opt_in`, `OptIn` | `store/inventory.py`, `admin.py` | E6 | 2 |
| `expire`, `ExpiryReport`, `ExpiryRefusedError` | `store/expiry.py` | E9, E12 | 3 |
| `CatalogAdmin.prune` | `admin.py` | gains `inventory_window`, `keep_roots`, `state_dir` | 3 |
| `PruneReport.expiry` | `admin.py` | `ExpiryReport \| None = None` | 3 |
| `verify_catalog`, `VerifyReport.notes` | `diagnostics.py` | E15 | 4 |
| `doctor` | `diagnostics.py` | E15's items | 2 (`catalog.json`, `LOCK`, `HISTORY`), 4 (the rest) |
| `restore`, `RestoreReport` | `store/expiry.py` | `(writer, to, *, reason, dry_run=False) -> RestoreReport` | 5 |
| `CatalogAdmin.restore` | `admin.py` | E14 | 5 |

No part adds `Any` to `src/`; `tools/count_any.py` stays at its baseline.

## Alternatives Considered

Each decision carries its own table. Three wider choices:

*   **Run local expiry in a separate command, `statcat expire`.** One command per job. The suite
    design (H5) and GH-125 place expiry in `prune`, and GH-125's acceptance tests one call that
    retires and expires. `prune` with `inventory_window` is that call, and a second command
    would repeat its window checks.
*   **Put the whole engine in sdmxlib.** sdmxlib would then write and delete catalog objects.
    It holds no blob client and writes no Inventory object today (spec §6, Ownership), and the
    engine needs both stores.
*   **Skip the lock on 1.x catalogs.** No `LOCK` file appears until opt-in. A stage-2 writer's
    commit could then straddle an opt-in, the race the lock removes for every writer that has
    it.

**The public surface, against GH-125's items.**

| Surface | Why stage 2 needs it |
|---|---|
| `statcat admin opt-in`, `CatalogAdmin.opt_in` | GH-125 part 2: opt-in through `CatalogAdmin` |
| `statcat prune --expire-older-than`, `--keep-roots` | GH-125 part 3: expiry in `prune`, with W and K |
| `--state-dir` on `prune` | A catalog that opts in late keeps its earlier roots for good, and each run reads them without the cache |
| `statcat admin restore`, `CatalogAdmin.restore` | GH-125 part 3: restores in the spec's order |
| `ExpiryStorage`, `expire` | release-job Part 6 |

## Non-Functional Requirements

Counts for one run that takes n candidates and deletes m leaves, with c checkpoints stepped
over, r retained roots and s segments ever written:

| Step | Reads | Writes and deletes |
|---|---|---|
| The lock, `catalog.json`, `HISTORY` | 2 | none |
| 0 | `CURRENT`, the head, a probe | `CURRENT` when behind |
| 1, 2 | n + c + 1 roots, whole; the newest `expiry` segment, whole | none |
| 3 | every retained root not cached (r without `state_dir`); one header range per segment not cached (s without it); the orphans that start at the lowest candidate | one segment per 1,000 candidates per epoch |
| 4 | none | `HISTORY` |
| 5 | n roots, for their tags and hashes | n deletes |
| 6 | a shallow list of `inventory/`; the roots committed since the plan | m tag reads, m deletes |
| 7 | none | `HISTORY` |

Sizes come from the suite design's Sizes table (StatCat-Suite/statcatlib#126's corpus): 560 KB a
root at 2,521 products, 1.6 MB at 7,000. None of the times below is measured.

*   **Lock hold, commits.** One version's writes: a `HISTORY` read, the changed leaves, a root
    and `CURRENT`. Milliseconds on local disk.
*   **Lock hold, expiry.** A nightly-sized run on a laptop (10 candidates at 560 KB) reads about
    6 MB and holds the lock under a second with `state_dir`. Without `state_dir` it reads every
    retained root: a catalog that opted in at version 2,979 reads 1.7 GB each run, tens of
    seconds on a laptop disk, inside the 120 s wait. A first run that takes thousands of
    candidates can exceed 120 s, and a seal that meets it is refused and keeps its journal entry
    (Q9); the opt-in page says to run the first expiry while no seal runs.
*   **Memory.** The engine holds segment lines and leaf hashes, and frees each root's bytes once
    it has hashed them. A segment's lines are at most 64 MiB, and the retained leaf sets are
    64 bytes a hash for each distinct leaf.
*   **Disk.** `history/` grows by each expired root's line: 920 bytes a released table, 185 KB
    for a release of 200, 6.5 MB for a republish of 7,000. Nothing under it is deleted.
    `LOCK` is empty. `expiry-cache.json` holds about 80 bytes a leaf hash per retained root.
*   **Atomicity.** Every `HISTORY` and `catalog.json` write is a staged file renamed over the
    name, with the directory fsynced. Every segment is created whole. E11's table gives the
    state after a crash at each step.
*   **Idempotency.** A second `prune` with the same arguments expires nothing more and writes
    nothing. A second `opt_in` returns `already=True`. A rerun of a finished restore with the
    same `to`, while its root S + 1 is still the head, returns that restore's report and writes
    nothing.
*   **Security.** A segment line names embargoed editions and their `release_at`, as a root
    does. On local disk file permissions are the only boundary, as for `inventory/`
    (`docs/catalog-format.md` §1.1). A local `event.actor` and a segment header's `actor` are
    `<os user>@<hostname>` and stay in `history/` for good.
*   **Compatibility.** No document format changes. E7's table states what each older version
    does with an opted-in catalog.
*   **Coverage.** `just coverage` runs in each part's PR.

| Module | Unit and integration tiers cover | Left to release-job Part 6 |
|---|---|---|
| `store/lock.py` | POSIX in the gate: threads, a second process, a killed holder, the timeout with a short `timeout=`, a filesystem error injected through `fcntl.flock` | none; the Windows branch has no gate (Risks) |
| `store/inventory.py` | the lock span, both refusals, the decoder choice, the six local storage methods | the Azure lease |
| `store/expiry.py` | every step, every crash row and the dry run over `LocalInventoryStorage` and over a recording wrapper that raises at the n-th call; `check()` and `if_match` ordering with a fake storage that tags and refuses | `BlobExpiryStorage` on Azurite |
| `admin.py`, `cli/` | `opt_in`, `prune` with expiry, `restore`, their commands and exit codes | the same over `az://` |
| `diagnostics.py` | every finding and note, on catalogs built by the engine and then damaged | none |

### Not confirmed

Each item below was read in documentation or code and not run. Each has a risk row or a test
that will settle it.

- That `fcntl.flock` and a Windows byte-range lock are released when the holding process is
  killed, on every platform statcatlib runs on. The suite design lists it under "Verify before
  citing". Part 1's killed-holder test settles it for the platform the gate runs on.
- That `msvcrt.locking` takes its lock with `LockFile`, that the lock may cover a byte beyond the
  end of an empty file, and which `errno` values it gives on a filesystem without locks. No
  Windows machine runs `just ci`.
- The `errno` values `flock` gives on a filesystem without advisory locks (`ENOLCK`, `ENOTSUP`,
  `EOPNOTSUPP`, `ENOSYS` are assumed), and that Linux maps `flock` on NFS to a lock other clients
  see.
- That `flock` excludes two threads of one process that open the file separately on macOS as it
  does on Linux. Part 1's thread test settles it.
- That `validate_root` and the shipped `root-1.2` schema accept a change item whose
  `leaf_sha256` is `{"from": null, "to": null}`, which root S + 1 writes for a tombstoned
  edition of a product root S does not record. `change_list` builds it and
  `_check_history_fields` has no rule against it (sdmxlib's `_rules.py:595-667`,
  `_format.py:839-861`), and no test in either repository writes one.
- That `os.replace` over `inventory/HISTORY` or `catalog.json` succeeds on Windows while a
  reader holds the old file open. `CURRENT` is replaced the same way today
  (`store/inventory.py:405-406`).
- Every time in the Non-Functional Requirements. No expiry has run at 7,000 products.
- The shape of N1 to N3. sdmxlib's own design decides the names and signatures.
- Everything on Azure: that a lease request on a missing blob answers 404, the renewal rules,
  the `If-Match` delete, `obstore.get_range` under a bearer token, that a lease request is
  authorised under the blob write action, and which role or ACL the release identity holds on
  the catalog's container today. This design sent no
  request to an Azure host. The release-job design's Not confirmed list carries the lease and
  the `If-Match` delete.

### Risks and limits

| Risk | Mitigation |
|---|---|
| The Windows lock has no test in the gate | The lock module is 40 lines with one platform branch; a manual run on Windows before the release that carries Part 1 is in the Verification Plan. The owner states whether Windows is supported. |
| A network or synced filesystem accepts `flock` and excludes no other host | Unsupported by spec §6.6. `doctor` warns for a synced folder. Opt-in's page repeats it. |
| A laptop clock set forward expires roots early | K roots, the checkpoints and every segment line stay; the dry run shows the candidates first (Q5). |
| A stage-1 writer older than 0.14 wrote a root without the history fields above `since_seq` | Expiry stops at that root for good, and `verify-catalog` reports it. Only a statcatlib 0.13 racing the opt-in can write one, and 0.13 cannot open the catalog afterwards. |
| A first expiry holds the lock past 120 s | The seal is refused with its journal entry kept; the opt-in page gives the order (Q9). |
| An orphan segment stays for good | It is a note, and the engine adopts the common case, a crash between steps 3 and 4 (Q7). |
| A restore brings back an entry whose cancellation it discarded | Q6 (b): root S + 1 holds automatic releases, and the report lists the discarded transitions. |
| A restore's discarded leaves and a crashed commit's leaves are never collected | Bounded by the changes since S, and by one version per crash (spec §6.9). A later design collects them. |
| `check_history` and the engine share one checkpoint rule | N3's cases state the expected checkpoints by hand (E17). |
| Opt-in cannot be undone | A dry run by default, and three statements printed before `--apply` (E6). |
| A run without `state_dir` reads every retained root | Stated in the NFRs; `--state-dir` on `prune`. |
| The retained set is wrong and a named leaf is deleted | Step 6 fails closed on an unreadable root (S42), re-lists after step 5, and Part 3's reachability test compares every deletion with an independent computation. |
| Part 3 waits for sdmxlib 0.89 | Parts 1 and 2 land first on 0.88; Q1 (b) removes the wait. |

## Detailed Implementation Plan

### Land order and releases

| Part | Content | sdmxlib | Waits on |
|---|---|---|---|
| 1 | The lock, the protocol members, `_commit_locked`, both refusals, the stage-2 decode | 0.88 | Q2, Q9; S39 |
| 2 | The local `ExpiryStorage` methods, opt-in, `doctor`'s first three items | 0.88 | Part 1; S41 |
| 3 | The engine, `prune` with expiry, the events | 0.89 under Q1 (a) | Part 2; Q1, Q3, Q4, Q5; N1, N7; S31, S32, S37, S42 |
| 4 | `verify-catalog`'s history checks, `doctor`'s remaining items, the stage-2 conformance run | 0.89 | Part 3; Q7; N2, N3; S36, S38 |
| 5 | Restores, GC's stop | 0.89 | Part 3; Q6, Q8; S33, S34, S35, S40, S43 |
| release-job Part 6 | Server expiry | 0.89 | Parts 1 to 5, and the release-job design's handoff rows 11 and 12 |

Each part is a branch from `origin/main` and a PR, merged with a merge commit after `just ci`
passes. Each updates `docs/upgrading.md`, the `EGM.md` beside the code, `CLAUDE.md` where it
names a module, a command or a writer rule, and the pages listed per part. Each part's tests
carry a `pytestmark`, and new files are marked Create. The first PR of each part uses `feat:`.
Parts 4 and 5 may land in either order.

The owner cuts releases. A grouping that keeps each release whole:

| Release | Carries | Breaking |
|---|---|---|
| The next release (0.18.0) | The release job, already on `main`. Part 1 too if it has landed. | The release job's entries in `docs/upgrading.md`. |
| The release with Parts 1 and 2 | The lock and opt-in | `InventoryStorage` gains three members, so a class written against the protocol must add them. A local commit creates `<catalog>/LOCK`, can wait up to 120 s, and can raise `CatalogLockTimeoutError`. A local commit to a 2.x catalog no longer raises `InventoryWriteRefusedError`. No catalog is rebuilt, and an older statcatlib still reads and writes a 1.x catalog that holds a `LOCK` file. |
| The release with Parts 3 to 5 | Expiry, the checks, restores | `sdmxlib>=0.89,<0.90` under Q1 (a). `verify-catalog` reads every retained root and can report a broken chain on a catalog that passed before. `prune` and `PruneReport` gain keyword parameters and one field. |
| The release with Part 6 | Server expiry | The release-job design states it. |

No release opts a catalog in. Opt-in is the owner's own one-way step, and E7's table gives what
each older tool then does.

### Part 1: the catalog lock, the refusals and the stage-2 decode

*   **`src/statcatlib/store/lock.py` (Create):** `hold`, the two platform branches, the thread
    table, the constants.
*   **`src/statcatlib/store/inventory.py` (Modify):** the protocol members (`:322-353`);
    `LocalInventoryStorage.exclusive`, `check`, `stage_2`; `try_commit` and `_commit_locked`
    (`:623-697`); `check_writable`'s decoder (`:570`); `history()`; the four error classes; the
    module and error docstrings (`:28-30`, `:123-132`).
*   **`src/statcatlib/store/blob.py` (Modify):** `BlobInventoryStorage.exclusive`, `check`,
    `stage_2 = False` (`:432-628`).
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export the error
    classes.
*   **`tests/test_inventory_conformance.py:80`, `tests/stores/_backend.py:64` (Modify):** the
    local adapter sets `stage_2 = True`; the blob adapter keeps `False`.
*   **Existing tests and pages that state the stage-1 refusal (Modify):** the local 2.x refusal
    tests in `tests/test_inventory_history.py` (`:300`, `:319`); `docs/catalog-format.md:68-69`;
    `docs/api/store.md:227`, `:411`; `src/statcatlib/store/EGM.md`'s `inventory.py` entry.
*   **Docs:** `docs/catalog-format.md` §1 gains `LOCK` in the layout and a paragraph on the lock
    and §1.3 on filesystems without advisory locks; `docs/api/store.md` gains the lock, the wait
    and the errors.

| Test | File | Tier | Covers |
|---|---|---|---|
| A commit creates `LOCK`, leaves it in place, and a second commit reuses it | `tests/test_catalog_lock.py` (Create) | integration | E1 |
| A child process that holds the lock is killed; `LOCK` stays and the next commit proceeds | `tests/test_catalog_lock.py` | integration | GH-125 acceptance |
| A second process and a second thread wait for a held lock and then commit; with a short timeout they raise `CatalogLockTimeoutError` | `tests/test_catalog_lock.py` | integration | E1, Q9 |
| A thread that calls `exclusive()` twice gets `RuntimeError`; `check()` outside it raises | `tests/test_catalog_lock.py` | unit | E1, E2 |
| With `flock` patched to raise `ENOLCK`: a 1.x commit lands with one WARNING, a 2.x commit raises `CatalogLockUnsupportedError` | `tests/test_catalog_lock.py` | unit | E5, Q2 |
| The lock is held from before the `catalog.json` read to after `CURRENT`, shown with a recording storage | `tests/test_inventory_writer.py` | unit | E3 |
| A commit that waited for the lock records a `created_at` read after the wait | `tests/test_inventory_writer.py` | unit | E3 |
| On a hand-written 2.0 catalog: a local commit lands; a base at or below `expired_through` raises `StaleBaseError` from `try_commit` and is retried by `commit`; a missing `HISTORY` is refused | `tests/test_inventory_history.py` | integration | E4, E7, GH-125 part 2 |
| `HISTORY` advanced between `commit`'s head read and the lock is seen by the check | `tests/test_inventory_history.py` | integration | "a fresh read of `HISTORY`" |
| A blob commit to a 2.x catalog is still refused | `tests/test_blob_storage.py` | unit | E2, E7 |
| The stage-1 history checks still pass with `stage_2 = True` | `tests/test_inventory_conformance.py` | integration | N3's base |

### Part 2: opt-in

*   **`src/statcatlib/store/expiry.py` (Create):** `ExpiryStorage`, `Listed`.
*   **`src/statcatlib/store/inventory.py` (Modify):** `LocalInventoryStorage`'s six methods;
    `InventoryWriter.opt_in`.
*   **`src/statcatlib/admin.py` (Modify):** `CatalogAdmin.opt_in`, `OptIn`.
*   **`src/statcatlib/cli/__init__.py` (Modify):** `statcat admin opt-in`, beside the other
    `admin` commands (`:826-836`); `src/statcatlib/cli/EGM.md`.
*   **`src/statcatlib/diagnostics.py` (Modify):** `doctor`'s `catalog.json`, `LOCK` and
    `HISTORY` items.
*   **Docs:** `docs/api/admin.md` gains "Opting in to expiry", with the three statements and the
    order of upgrades; `docs/catalog-format.md` §1 gains `inventory/HISTORY`.

| Test | File | Tier | Covers |
|---|---|---|---|
| Opt-in writes `HISTORY`, then `catalog.json` 2.0, with `since_seq` the lowest root of the run that carries the fields; a dry run writes nothing | `tests/test_opt_in.py` (Create) | integration | E6 |
| Refusals: a head without the fields, a root with them below one without, a missing root, an undecodable root, a broken chain, each naming the root | `tests/test_opt_in.py` | integration | spec §6.1, S41 |
| A catalog with no version opts in with `since_seq` 1 | `tests/test_opt_in.py` | integration | S41 |
| A crash after `HISTORY` is completed by a rerun; a second opt-in returns `already` | `tests/test_opt_in.py` | integration | E6 |
| `catalog.json` changed between the read and the replace is refused | `tests/test_opt_in.py` | unit | spec §6.1 |
| Opt-in over a `BlobLocation` raises `TypeError` | `tests/test_catalog_admin.py` | unit | E6, H21 |
| After opt-in: a seal lands; the reader with `CURRENT` removed finds the head through `HISTORY` | `tests/test_opt_in.py` | integration | E7 |
| Each local storage method: `list` deep and shallow, tags that change on `replace`, `delete` of an absent path | `tests/test_inventory_writer.py` | unit | E8 |
| `statcat admin opt-in`: the dry run's lines, `--apply`, exit codes | `tests/test_cli_admin.py` | unit | E6 |
| `doctor` on a 1.x and a 2.x catalog, with the lock free and held | `tests/test_cli_verify_doctor.py` | unit | E15 |

### Part 3: the expiry engine and `prune`

*   **`src/statcatlib/store/expiry.py` (Modify):** `expire`, the steps, the cache file, the
    events, `ExpiryReport`, `ExpiryRefusedError`.
*   **`src/statcatlib/admin.py` (Modify):** `prune`'s parameters and `PruneReport.expiry`
    (`:109-139`, `:550-653`).
*   **`src/statcatlib/cli/__init__.py` (Modify):** `--expire-older-than`, `--keep-roots`,
    `--state-dir` on `prune` (`:579-641`).
*   **`pyproject.toml` (Modify):** `sdmxlib>=0.89,<0.90`, under Q1 (a).
*   **`tests/_history.py` (Create):** a catalog builder that writes versions at given times
    across month boundaries; the reachability computation; the "live at T" replay, until N4.
*   **`tests/_fakes.py` (Modify):** a recording `ExpiryStorage` wrapper that raises at the n-th
    write or delete, and a fake that tags objects, refuses a stale `if_match` and fails
    `check()` on demand.
*   **Docs:** `docs/api/admin.md` gains "Expiring Inventory versions" with the steps, the dry
    run, the events and the crash table; `docs/catalog-format.md` §1 gains `history/`;
    `docs/reference/` gains the `store/expiry.py` page; nav entries.

| Test | File | Tier | Covers |
|---|---|---|---|
| Candidates: a contiguous run, never below `since_seq`, never at or above `CURRENT.seq`, outside the newest K, successor older than W; checkpoints for `since_seq`, an epoch's first root and each UTC month are stepped over | `tests/test_expiry.py` (Create) | integration | steps 0, 1 |
| `CURRENT` lagging by more than K: step 0 advances it and step 1 takes no root at or above it | `tests/test_expiry.py` | integration | spec conformance case |
| The walk stops at a root above `since_seq` without the history fields, and at a missing root | `tests/test_expiry.py` | integration | step 1 |
| A chain mismatch stops the run with nothing written | `tests/test_expiry.py` | integration | step 2 |
| Segments: one per epoch, split at 1,000 lines and at 64 MiB (with a lowered limit), linked in order, each line equal to `segment_line` of its root, the header's `roots` and `leaves` | `tests/test_expiry.py` | integration | step 3 |
| "What was live at time T" is the same from the full roots before expiry and from the checkpoints, retained roots and segment lines after it, for a T in each month | `tests/test_expiry.py` | integration | GH-125 acceptance |
| Expiry never deletes a leaf a retained root names: an independent reachability computation over the files, compared with the `expiry.deleted` lines | `tests/test_expiry.py` | integration | GH-125 acceptance |
| A crash after each step, in a run of several segments: linked orphans adopted, an unlinked orphan left, `cleaned_through` limiting the second run to the pending headers | `tests/test_expiry_crash.py` (Create) | integration | GH-125 acceptance, E11 |
| A root below `expired_through` with no matching line is kept and reported | `tests/test_expiry_crash.py` | integration | step 5 |
| A retained root that does not decode: no leaf deleted, `cleaned_through` unchanged | `tests/test_expiry_crash.py` | integration | S42 |
| `check()` is called before every segment write, `HISTORY` write and delete; a `check()` that raises ends the run before the next one | `tests/test_expiry.py` | unit | GH-125 acceptance (the engine's half of the failed renewal) |
| Each leaf delete carries the tag read at its final check; a leaf re-tagged after that check survives | `tests/test_expiry.py` | unit | GH-125 acceptance (the engine's half of `If-Match`) |
| A seal that races `prune`: with expiry paused after step 1, the seal's `try_commit` waits and then commits, and every leaf its root names exists; and the reverse order | `tests/test_expiry_race.py` (Create) | integration | GH-125 acceptance |
| Two processes, one sealing in a loop and one pruning in a loop: every root's leaves exist at the end | `tests/test_expiry_race.py` | integration | GH-125 acceptance |
| `prune` with `inventory_window` commits its `retire` version and expires in one call, and returns | `tests/test_prune.py` | integration | GH-125 acceptance |
| `prune` without `inventory_window` expires nothing on an opted-in catalog; `inventory_window` below `older_than` plus a day, and a 1.x catalog, are refused before the retire step | `tests/test_prune.py` | unit | E13, Q4 |
| A dry run writes nothing, takes no lock, and its plan equals what the real run then does | `tests/test_expiry.py` | integration | step 8 |
| With `state_dir`, a second run reads only the roots written since the first; a corrupt cache file costs one full read | `tests/test_expiry.py` | integration | S23 |
| A second run with the same arguments writes nothing | `tests/test_expiry.py` | integration | idempotency |
| `statcat prune --expire-older-than`: the lines, the events, exit codes | `tests/test_prune.py` | unit | E12, E13 |

### Part 4: `verify-catalog`, `doctor` and the conformance run

*   **`src/statcatlib/diagnostics.py` (Modify):** `verify_catalog` through `check_history`,
    `VerifyReport.notes`; `doctor`'s checkpoint, segment and restore items.
*   **`src/statcatlib/cli/__init__.py` (Modify):** the `ORPHAN` lines (`:514-550`).
*   **`tests/test_inventory_conformance.py` (Modify):** `WriterBackend` implements N3's
    `ExpiryBackend`: `opt_in`, `expire` with `stop_after`, `restore` (from Part 5), `verify`,
    `list`, `delete`.
*   **Docs:** `docs/api/admin.md`'s `verify-catalog` and `doctor` sections gain the history
    checks, the `ORPHAN` note and the new items; `docs/catalog-format.md` §6 names the checks.

| Test | File | Tier | Covers |
|---|---|---|---|
| `verify-catalog` passes on an expired catalog; an edited segment line, a deleted root above `expired_through`, a deleted retained leaf, a broken segment link, a `cleaned_through` off the chain and a root without the fields above `since_seq` are each one finding naming the file | `tests/test_cli_verify_doctor.py` | integration | GH-125 acceptance, spec checks 1 to 3 |
| An orphan segment prints `ORPHAN` and leaves exit status 0 | `tests/test_cli_verify_doctor.py` | unit | Q7 |
| A 1.x catalog with history fields gets the chain check | `tests/test_cli_verify_doctor.py` | integration | spec §6.9 |
| `doctor`: the newest checkpoint, an orphan, a pending segment after a crash | `tests/test_cli_verify_doctor.py` | unit | GH-125 part 3 |
| `run_expiry` passes every stage-2 check against the local writer | `tests/test_inventory_conformance.py` | integration | GH-125's first acceptance item |

### Part 5: restores

*   **`src/statcatlib/store/expiry.py` (Modify):** `restore`, `RestoreReport`, the resume rule.
*   **`src/statcatlib/admin.py`, `src/statcatlib/cli/__init__.py` (Modify):**
    `CatalogAdmin.restore`, `statcat admin restore`.
*   **`src/statcatlib/gc.py` (Modify):** the unfinished-restore stop (`:303-317`).
*   **`tests/test_inventory_conformance.py:184-196` (Modify):** on an opted-in catalog
    `restore_with_new_epoch` calls `restore`; `restore_to` stays for 1.x catalogs.
*   **Docs:** `docs/api/admin.md` gains "Restoring a catalog", with the report, the crash table
    and the difference from `rollback`; the release and GC pages gain the unfinished-restore
    stop.

| Test | File | Tier | Covers |
|---|---|---|---|
| A restore to S writes the `restore` segment, `HISTORY` with the new epoch and `expired_through` the smaller of its old value and S, `catalog.json`, deletes the roots above S, writes root S + 1 with kind `restore` hashing root S, and `CURRENT`, in that order (recording storage) | `tests/test_restore.py` (Create) | integration | GH-125 part 3 |
| Root S + 1's `retired` holds S's tombstones and one for each edition only a discarded root recorded; its change list holds them; `statcat gc`'s kept set includes them | `tests/test_restore.py`, `tests/test_gc.py` | integration, unit | S25 |
| Unreadable roots above S are listed in `unread` and deleted | `tests/test_restore.py` | integration | spec step 1 |
| A crash after each step: readers with a failed `CURRENT` fail closed until root S + 1 exists; a commit, an expiry and GC are refused; the rerun resumes; another `to` is refused | `tests/test_restore.py` | integration | spec Restores, S33 |
| A restore to a checkpoint below `expired_through` writes root S + 1 | `tests/test_restore.py` | integration | S33 |
| After a restore, expiry steps over the `restore` segment, keeps a new-epoch root that reuses a pending header's `seq`, and expires the new epoch into `history/<epoch>/` | `tests/test_restore.py` | integration | S35, S43 |
| Root S + 1 has the embargo on; `transitions` lists a discarded cancellation | `tests/test_restore.py` | integration | Q6 |
| A 1.x catalog, S below `since_seq - 1`, a missing root S and a root S with a missing leaf are refused with nothing written | `tests/test_restore.py` | unit | Q8 |
| `verify-catalog` passes across a restore, keyed by `(restore_epoch, seq)` | `tests/test_cli_verify_doctor.py` | integration | spec check 1 |
| `CatalogAdmin.rollback` on an opted-in catalog after expiry makes a superseded edition live | `tests/test_catalog_admin.py` | integration | E14 |
| `statcat admin restore`: the dry run's lines, `--apply`, exit codes | `tests/test_cli_admin.py` | unit | E14 |

### Acceptance map

| GH-125 acceptance item | Where | Test |
|---|---|---|
| sdmxlib's history conformance cases pass against statcatlib's writer, and every root it writes carries both history fields | Stage 1: done in 0.14.0. Stage 2: Part 4, once N3 ships | `tests/test_inventory_conformance.py`, `tests/stores/test_blob_conformance.py` |
| "What was live at time T" gives the same answer after expiry as from the full roots before it | Part 3 | `tests/test_expiry.py` |
| Expiry never deletes a leaf a retained root names, checked by an independent reachability computation compared with the deletion log | Part 3; on Azurite, release-job Part 6 | `tests/test_expiry.py` |
| A seal that races `prune` on a local catalog either commits before expiry chooses or waits for it, and every leaf its root names exists | Part 3 | `tests/test_expiry_race.py` |
| A process killed while it holds the local lock leaves `LOCK` in place, and the next commit proceeds | Part 1 | `tests/test_catalog_lock.py` |
| `prune` with expiry commits its `retire` version and expires in one call without deadlock | Part 3 | `tests/test_prune.py` |
| An expiry crash after each step recovers on the next run under the crash rules | Part 3 | `tests/test_expiry_crash.py` |
| A second release-job run fails to take the `HISTORY` lease and writes nothing | release-job Part 6 | its `tests/stores/test_expiry_blob.py` |
| A failed lease renewal stops expiry before its next destructive call | The engine's half in Part 3; the lease in release-job Part 6 | `tests/test_expiry.py`; its `tests/test_history_lease.py` |
| A leaf rewritten after step 6's final check survives the `If-Match` delete | The engine's half in Part 3; the delete in release-job Part 6 | `tests/test_expiry.py`; its `tests/stores/test_expiry_blob.py` |
| The pod check with an unreachable pod or a missing `sdmxlib_version` skips expiry, and the release step still runs | release-job Part 6 | its `tests/test_release_job.py` |
| Every entry recorded after Phase 5 carries `artefacts` equal to its manifest's records | Done in 0.17.0 | `tests/test_inventory_routing.py` |
| `verify-catalog` detects an edited segment line, a deleted root, and an entry whose `artefacts` differ from its manifest | Part 4; the third is done in 0.17.0 | `tests/test_cli_verify_doctor.py` |
| `just ci` passes | every part | the gate |

GH-125's part 2 and part 3 bullets that are no acceptance item map to Part 1 (the lock span,
the stale-base refusal, non-reentrancy, the 2.x decode), Part 2 (opt-in and its refusals),
Part 3 (the steps and crash rules), Part 4 (`verify-catalog`, `doctor`) and Part 5 (restores).
The Azure lease, the gates and the leaf-set cache's volume are the release-job design's Part 6.

### Verification Plan

*   `just ci` for each part, on a machine with a container runtime, and `just coverage` in each
    part's PR.
*   Part 1: the lock tests run on macOS and Linux. Before the release that carries Part 1, one
    manual run of `tests/test_catalog_lock.py` on Windows, or a statement in
    `docs/catalog-format.md` §1.3 that the Windows lock is untested.
*   Part 3: one run of `prune --expire-older-than` on a copy of the StatCat-Suite/statcatlib#126
    corpus (2,979 versions), opted in, with and without `--state-dir`. It records the lock hold
    time, the bytes read and the peak memory in `bench/`, which replaces the estimates in the
    Non-Functional Requirements.
*   Part 4: `statcat verify-catalog` on that corpus after the expiry, with its time recorded.
*   Part 5: a restore drill on a copy of the expired corpus: damage the three newest roots,
    restore, then run `verify-catalog`, a seal and a second expiry.
*   Before release-job Part 6: the engine's tests run once over a `MemoryStore`-backed
    `BlobExpiryStorage` prototype, to show the interface is enough before Part 6 writes the
    Azure class.

## Future work

*   Collection of leaves no root names, after a crashed commit or a restore.
*   A restore of a 1.x catalog, with Phase 9's drill.
*   `verify-catalog` and `doctor` over a `BlobLocation`.
*   A lock for S3 and GCS (H21, H23).
*   A public reader of history segments, with the stage-3 exporter.

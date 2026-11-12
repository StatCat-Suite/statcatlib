# Inventory history stage 2: the catalog lock, opt-in, expiry and restores

*   **Author:** Gabriel Gellner
*   **Status:** Draft, revision 2. Revision 1 had an EGM review
    ([report](egm/inventory-history-stage-2/review-1.md): 4 Blocker, 22 Major, 31 Minor). The
    project owner then decided seven points and accepted four recommendations on 2026-11-12
    (the table "Owner decisions"). This revision applies those decisions and every Blocker and
    Major; the section "Review response" maps each finding to its change.
*   **Build deferred.** The project owner starts Phase 6 (serving) first. No part of this design
    is scheduled. The session that builds it re-reads the heads named below, and revises this
    design where they have moved, before Part 1 starts.
*   **Date:** 2026-11-12
*   **Issue:** GH-125 parts 2 and 3. GH-125 part 1 shipped in 0.14.0 and GH-125 part 4 in
    0.17.0. Suite tracker: StatCat-Suite/statcat-suite.github.io#19.
*   **Read against:**
    *   statcatlib `dfc519d` (`origin/main`, v0.18.0). 0.18.0 shipped the release job without
        any part of this design. `src/` and `tests/` are unchanged since `932e8b8`, which
        revision 1 read.
    *   sdmxlib `4886fdb` (`origin/main`): the 0.88.0 code plus the `tiers` argument of
        StatCat-Suite/sdmxlib#391, which is unreleased. `src/sdmxlib/inventory/` is unchanged
        since `v0.88.0`.
    *   sdmxlib-rest `af53a59` (`origin/main`, v0.10.3).
    *   The hub at `b773d93`, with the suite design
        [Inventory history log](https://statcat-suite.github.io/design/inventory-history-log/) at
        revision 5.
    *   StatCat-Suite/statcat-serve `42ad7ad`, a skeleton with no Inventory code.

    Every `file:line` is at these commits. Find each site by its symbol and use the number as a
    hint. Issue texts were read on 2026-11-12.
*   **Inputs:**
    *   GH-125's body: parts 2 and 3 and their acceptance items. The body cites revision 4 of the
        suite design. This design follows revision 5, which took release-job S23, S25 and
        S26.
    *   The suite design, which sets the cross-repo contract (decisions H1 to H24).
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        ("the spec") at `4886fdb`: §6.1, §6.2, §6.3, §6.6, §6.9, §9.2, §10.1, §10.3, §11.4 and
        §11.6. Its plan is `docs/design/sharded-bundle-catalog-plan.md` at the same commit ("the
        plan").
    *   `sdmxlib.inventory` at `v0.88.0`: `_format.py`, `_rules.py`, `_reader.py`,
        `conformance.py` and its `EGM.md`.
    *   StatCat-Suite/sdmxlib#361, whose acceptance list holds the stage-2 conformance cases.
    *   The [Phase 5 design](release-job.md) ("the release-job design"): its section "GH-125's
        parts", D13, Part 6, handoff rows 11 and 12, Q9, S23, S25 and S26.
*   **Identifier prefixes.** E1 to E19 are this design's decisions, O1 to O11 the project
    owner's, and Q1 and Q2 the questions still open. N1 to N7 are handoff rows for sdmxlib, and
    R1 to R12 changes to the release-job design's Part 6. S31 to S48 continue the S series of the
    Phase 4 and Phase 5 designs, which ends at S30; a number keeps its meaning across revisions,
    and a withdrawn row keeps its number. Every identifier of the release-job design carries its
    name: "release-job D13", "release-job Q9", "release-job S25", "release-job Part 6". F14 is
    a finding of the [Phase 4 design](catalog-on-blob-storage.md). H1 to H24 are the suite
    design's, and P1 to P7 the [Phase 2a design](phase-2a-catalog-editions.md)'s. `GH-N` is a
    statcatlib issue. "Part 1" to "Part 5" are this design's parts, and "GH-125 part N" is a
    part of the issue.

--------------------------------------------------------------------------------

## Overview

A catalog keeps every Inventory root it has written. At 7,000 products a root is about 1.6 MB,
and a server writes 3 to 10 a day (suite design, Problem). Stage 1 (statcatlib 0.14.0) made each
root carry its change list and the SHA-256 of the root before it. Stage 2 lets a catalog that
opts in move old roots into append-only history segments, and delete those roots and the leaves
no retained root names.

This design is the local half of stage 2 and the engine both halves share. On a catalog
directory it adds:

1.  the catalog lock, an OS advisory lock on `<catalog>/LOCK` that every Inventory commit takes;
2.  opt-in, which writes `inventory/HISTORY` and rewrites `catalog.json` as format 2.0;
3.  the expiry engine, steps 0 to 8 of spec §6.9, run by `CatalogAdmin.prune`;
4.  restores of an opted-in catalog to an older root, which bump the restore epoch;
5.  the history checks of `statcat verify-catalog` and the history lines of `statcat doctor`.

The engine is one function over an expiry-storage interface. Local disk implements the interface
here. Release-job Part 6 implements it over Azure Blob Storage, adds the `HISTORY` lease, the two
expiry gates and the persistent volume for the leaf-set cache, and calls the same function from
`statcat release --expire`.

| Part | After it lands, a user can |
|---|---|
| 1. The catalog lock, the local storage methods, the commit checks, the writer's stage-2 decode | commit on a local catalog under the lock. No command opts a catalog in, so nothing else changes for a user. |
| 2. Opt-in, internal | do nothing new. The opt-in function exists for tests and later parts, with no command and no `CatalogAdmin` method (O4). `statcat doctor` gains its `catalog.json`, `LOCK` and `HISTORY` lines. |
| 3. The expiry engine and `prune` | call `statcat prune --expire-older-than`, which refuses every catalog until one is opted in. |
| 4. Restores | call `statcat admin restore`, which refuses every catalog until one is opted in. |
| 5. `verify-catalog`, `doctor`, the conformance run and public opt-in | opt a local catalog in with `statcat admin opt-in`, expire its old Inventory versions, check its history and restore it. |

| Capability | Local disk | Azure Blob Storage | S3, GCS |
|---|---|---|---|
| Commit under the catalog lock | Part 1 | release-job Part 6 (the `HISTORY` lease) | No lock; a 2.x catalog is refused (H21) |
| Opt-in | Part 5 | release-job Part 6 | Refused (H21, H23) |
| Expiry | Part 3, in `prune` | release-job Part 6, in `statcat release --expire` | Refused |
| Restore | Part 4 | Unbuilt. R9 asks the release-job design to plan it. | Refused |
| History checks in `verify-catalog` and `doctor` | Part 5 | Open (Q2) | Open (Q2) |

GH-125's parts map to this design's parts as follows.

| GH-125 part | Built in |
|---|---|
| 1. History fields | statcatlib 0.14.0 |
| 2. The catalog lock and opt-in | Parts 1, 2 and 5 |
| 3. Expiry, restores and reporting | Parts 3, 4 and 5; the Azure half in release-job Part 6 |
| 4. `LeafEntry.artefacts` | statcatlib 0.17.0 |

### Terms

The suite design's Terms section defines base version, change list, root chain, history fields,
retained root, head chain, checkpoint, expiry, history segment, `expired_through` and catalog
lock. This design uses these terms as well:

| Term | Meaning here |
|---|---|
| Inventory version, root | One file `inventory/<seq>.json`. "Version N" and "root N" name the same object. |
| Leaf | `inventory/leaves/<sha256>.json`: one product's entries, named by the hash of its bytes. A root names one leaf per product. |
| Head | The newest valid root, which a reader finds by probing forward from `CURRENT` (spec §6.6). |
| `CURRENT` | A hint file that names a root. A writer moves it after each commit, and it can lag the head. |
| Reader fallback | What sdmxlib's reader does when `CURRENT` is missing or names a bad root: it reads `catalog.json` and `HISTORY` and probes from `expired_through` (spec §6.6). |
| Edition | One immutable version of a product's content, a directory under `products/<product_id>/`. |
| Entry state | An edition's state in its leaf: `embargoed`, `live`, `superseded` or `withdrawn`. |
| Root embargo flag | The root's `embargo` field. While it is `true`, a local seal records its edition `embargoed`, and on a server no automatic release runs (release-job Q1). |
| Tombstone, `retired` | A root's `retired` list holds one tombstone for each retired edition. A retired edition has left its leaf. |
| Seal, journal entry | A seal writes an edition's manifest and records the edition in a commit. A journal entry marks a seal whose commit has not succeeded yet; the product's next `working_copy()` settles it. |
| Restore epoch | The integer `restore_epoch` in `catalog.json`, `HISTORY` and every root. A restore adds 1. |
| `since_seq` | `catalog.json`'s `history.since_seq`: the lowest `seq` from which every root carries the history fields. Roots below it never expire. |
| `HISTORY` | `inventory/HISTORY`. It holds `restore_epoch`, `expired_through`, `newest_segment` with its SHA-256, and `cleaned_through`, the newest segment whose roots and leaves are deleted. |
| Segment header, segment line | A segment's first line is its header: its kind (`expiry` or `restore`), its links and its plan, the `roots` and `leaves` to delete. Each later line is one removed root without `products` and `retired`. |
| 2.x catalog | A catalog whose `catalog.json` is format 2.x. Rules that key on the format use this term. |
| Opted-in catalog | A 2.x catalog with a valid `HISTORY` (spec §6.1). |
| 1.x catalog | A catalog whose `catalog.json` is format 1.x. Nothing expires in it. |
| Stage-1 writer | statcatlib from 0.14.0 to the last release before Part 1, which is at least 0.18.0. It writes the history fields, takes no lock and refuses a 2.x catalog. |
| Stage-2 writer | A statcatlib from Part 1 on. It takes the catalog lock and checks `expired_through`. |
| Engine | `statcatlib.store.expiry.expire`, the function that runs expiry steps 0 to 8. |
| Run | One call of the engine. |
| Expiry storage | The interface the engine reads, writes and deletes through (E8). |
| Tag | An opaque string that changes whenever an object's bytes change: an ETag on Azure, and a value built from `os.stat` on local disk. |
| Candidate | A root that expiry step 1 takes in this run. |
| Pending segment | An `expiry` segment on the segment chain after `HISTORY.cleaned_through`. Its roots and leaves may still exist. |
| Orphan segment | A segment under `history/` that the chain from `HISTORY.newest_segment` does not reach. |
| S | A restore's target `seq`. The restore discards every root above S and writes root S + 1. The S-numbered rows (S31 to S48) are a different series. |
| Unfinished restore | The state E4 defines: a restore wrote `HISTORY` and has not yet written root S + 1. |
| W, K | The retention window and the number of newest roots expiry keeps (spec §11.6): `inventory_window` and `keep_roots`. |
| GC | Garbage collection. On a server it is `statcat gc`; on local disk `prune` deletes edition files. |
| The gate | `just ci`, which the pre-push hook runs. |
| The hub | The repository StatCat-Suite/statcat-suite.github.io, which holds the suite design. |
| Catalog owner, project owner | The person who runs a catalog; the person who decides this project's questions. |
| Land, release | A part lands when its PR merges to `main`. It is released when a statcatlib version that carries it is on PyPI. |

## The problem

`inventory/` grows without bound, and nothing in statcatlib can shrink it. `CatalogAdmin.prune`
"deletes no Inventory root or leaf" (`admin.py:567-568`), and the Phase 2a design's P7 kept
roots, leaves and tombstones until stage 2. The suite design and spec §6.9 give the rules. The
code differs from those rules in eight places:

- `try_commit` takes no lock. The local writer skips a leaf it finds present
  (`store/inventory.py:385`) and creates the root later (`:692`). An expiry between the two would
  delete a leaf the new root names.
- `commit` reads the head before `try_commit` runs (`store/inventory.py:747`), and no code
  compares that base with `HISTORY.expired_through`. Expiry frees root names, so a writer with a
  stale base could create a root at a freed name.
- The writer decodes `catalog.json` with `decoder="stage-1-writer"` (`store/inventory.py:570`), so
  every write to a 2.x catalog raises `InventoryWriteRefusedError`.
- No code writes `inventory/HISTORY` or a segment, and `CatalogAdmin` has no opt-in. Under
  `src/statcatlib` only `gc.py` reads `HISTORY` (`gc.py:303-319`).
- `prune` takes one window, `older_than`, and has no W or K (`admin.py:550`).
- `InventoryStorage` has no list, no delete and no conditional replace
  (`store/inventory.py:322-353`), which expiry needs on every store.
- statcatlib has no restore. Its tests restore by hand: they unlink the roots above S, rewrite
  `catalog.json` and call `try_commit` (`tests/test_inventory_conformance.py:184-197`,
  `tests/stores/test_release_job.py:481-497`).
- `verify_catalog` reads the head's leaves only (`diagnostics.py:354-369`), and `doctor` reports
  nothing about history (`diagnostics.py:577-627`).

Release-job Part 6 is written against "GH-125's engine" and marked provisional until this design
defines the interface (release-job Q9, release-job handoff row 11).

## Goals

1.  On a filesystem with advisory locks, every Inventory commit on a local catalog runs under
    the catalog lock, and a process killed while it holds the lock blocks no later commit
    (Part 1).
2.  A writer creates no root on a base at or below `expired_through` (Part 1).
3.  A local catalog opts in with one command, which refuses a catalog whose roots cannot
    support expiry. No release offers the command before it also offers expiry, the history
    checks and the restore (Part 5, O4).
4.  After expiry, `inventory/` holds the newest K roots, the roots within W, the checkpoints and
    the roots from before the opt-in, and every leaf a retained root names (Part 3).
5.  After a process is killed at any point of a run, readers find the same head and the same
    entries as before the run, and the next run finishes the work (Part 3).
6.  "What was live at time T" has the same answer after expiry as before it (E18, Part 3).
7.  `statcat verify-catalog` detects an edited segment line and a deleted root, and `statcat
    doctor` shows the state of `HISTORY`, the segments and the lock (Part 5).
8.  A restore to root S records every root it discards. An edition that only a readable
    discarded root recorded keeps its files from GC and `prune` for their retention window
    (Part 4).
9.  The engine runs unchanged over a second expiry storage with tag semantics, an in-memory
    one in the tests. Release-job Part 6 then supplies a storage, a lock, the gates and a cache
    directory (E8, Part 3).

## Non-goals

*   The Azure `HISTORY` lease, the expiry gates, the pod check, opt-in over a `BlobLocation` and
    the persistent volume. They stay in release-job Part 6.
*   A restore on object storage. R9 hands its requirements to the release-job design (O5).
*   Expiry, opt-in or a lock on S3 or GCS (H21, H23).
*   The lock on Windows (O2).
*   A restore of a 1.x catalog, a restore to a root at or below `expired_through` (O1), and a
    restore that discards no root. Phase 9 owns the restore drill and
    `verify-catalog --find-newest-consistent` (the plan, "Phase 9", line 429).
*   Undoing an opt-in. The spec has no rule for it.
*   Collecting the leaves that a crashed commit or a restore leaves unnamed. The suite design
    defers it.
*   Dropping tombstones from `retired`.
*   A public reader of history segments and the PROV-O exporter (stage 3). E18 gives the replay
    a test uses.
*   `correct` and `reinstate`, which stay in GH-130.

## Open questions

### Owner decisions

The project owner decided these on 2026-11-12, after the review of revision 1. They are inputs
to this revision. "Rev 1" names revision 1's question.

| ID | Rev 1 | Decision | Consequence here |
|---|---|---|---|
| O1 | Q8, review M8 | A restore refuses a target at or below `expired_through` in stage 2. | E14. The restore's root S + 1 needs no exemption from the stale-base rule, no `seq` is reused below `expired_through`, and S43's first half is withdrawn. |
| O2 | review M4 | The lock is unsupported on Windows. There a write to a 2.x catalog, opt-in, expiry and a restore are refused with a clear error, and a 1.x catalog commits as it does today. | E1, E5, S39. The `msvcrt` design is dropped. |
| O3 | Q1 | The pure rules, `check_history` and the stage-2 conformance cases live in sdmxlib 0.89. | E17, N1 to N7. Parts 3 to 5 wait for 0.89.0. |
| O4 | review M10 | Opt-in ships only in the release that also has expiry, the history-aware `verify-catalog` and the restore. | Part 2 lands the function unexported. Part 5 adds `CatalogAdmin.opt_in` and `statcat admin opt-in`. |
| O5 | Q6 | No embargo guard in root S + 1 on local disk. The question of an automatic release after a restore moves to the design that builds the server restore. The restore's report stays, built from each discarded line's `event` and `embargo` as well as its changes, and is flagged incomplete when a root is unread. | E14, R9. S34 is withdrawn. |
| O6 | Q2 | On a filesystem that refuses advisory locks, a commit to a 1.x catalog proceeds unlocked with one warning. A commit to a 2.x catalog, opt-in, expiry and a restore are refused. | E5, S39 |
| O7 | Q9, review M5 | A commit waits at most 120 s for the lock. Expiry, and opt-in's verification, read before they take the lock and check again under it, so the lock covers the writes and a bounded re-list. | E3, E6, E9, S46; the bound is in the Non-functional requirements. |
| O8 | Q3 | The engine reads whole roots in expiry steps 1 and 2. | E9. S31 is withdrawn: the spec's ranged read was never an obligation. |
| O9 | Q4 | `prune` expires only when the caller gives `inventory_window`. | E13 |
| O10 | Q5 | On local disk W is measured by the machine's clock. | E13, S37 |
| O11 | Q7 | An orphan segment is a note of `verify-catalog`, which does not change its exit status. | E15, S36 |

### Questions still open

Each lists its options with the cost of each. The recommendation is the option this design is
written to. Both gate the part named, and neither gates Parts 1 to 3.

| # | Question | Options and their costs | Recommendation | Gates |
|---|---|---|---|---|
| Q1 | Root S can record an edition whose files are gone. `prune` deletes the directory of an edition tombstoned longer than its window (`admin.py:740-769`), and a restore to a root older than that brings the entry back. What does `restore` do when root S records such an edition `live` or `embargoed`? | **(a) Refuse**, naming each edition whose `manifest.json` is missing or differs from `manifest_sha256`. Cost: no command restores to that root; the operator picks a newer S or puts the files back first. **(b) Restore and list them** in the report. Cost: the new head names editions no reader can open, and `Product.view` raises for those products until a new seal. **(c) Tombstone them in root S + 1.** Cost: the restore version rewrites leaves and removes a product's live edition; a rule the suite design lacks. | (a). A head that names missing files is the state a restore exists to leave. The report also lists the `superseded` and `withdrawn` entries of S whose directories are gone, which only limits a later `rollback`. | Part 4 |
| Q2 | Do the history checks run on object storage? The suite design's Handoff lists "`verify-catalog` and `doctor` reporting" with no local qualifier, and both commands take a directory today. | **(a) Local only here.** `check_history` takes a `Fetch` and two listings, so a later issue adds `az://` as a command-line change. Cost: until then a server catalog's history is checked only on a downloaded copy. **(b) Release-job Part 6 adds `statcat verify-catalog az://`.** Cost: the edition checks need every edition's files over the store, a design of its own. **(c) Part 5 adds a history-only check over `az://`.** Cost: Part 5 reads a blob store, and the check reads every retained root, 1.7 GB on the GH-126 corpus. | (a), with R11 recording the need for the release-job design's re-review. | Part 5 |

### Proposed suite design and specification changes

This design changes no document format. It proposes the rule changes below. Each row changes
text another repository owns, and this design edits neither the spec nor the suite design.

- **Changes** names what the row does to the existing text: it *contradicts* a sentence, *adds*
  a rule to a section, or *clarifies* one.
- **Repositories** names where the text changes. "sdmxlib" is the spec, and "hub" the suite
  design, which needs revision 6 with its own review (hub `CLAUDE.md`, "Suite designs").
- **Gates** names the first part that codes the rule. A part waits on a row until the row is
  merged in the suite design's revision 6, for a hub row, and in the spec.

| # | Where | Proposed rule | Other option | Changes | Repositories | Gates |
|---|---|---|---|---|---|---|
| S31 | | Withdrawn (O8). Spec §6.2 says a ranged read can fetch `created_at`; it obliges no writer. | | | | |
| S32 | Spec §6.9 step 2; suite design step 2 | The chain check starts at the predecessor of the lowest candidate c. When c - 1 equals `expired_through`, the predecessor's hash is the `root_sha256` of the line for c - 1, of the head chain's epoch, in the newest `expiry` segment on the chain. Otherwise it is the SHA-256 of retained root c - 1. | Keep "the newest segment's last line", which is a discarded root after a restore. | contradicts | sdmxlib, hub | Part 3 |
| S33 | Spec §6.6, §6.9 Restores; suite design Restores | From a restore's `HISTORY` write until its root S + 1 exists, the restore is unfinished (E4's definition). A stage-2 writer then refuses every commit except the restore's own version of kind `restore`, and expiry refuses to run. A rerun derives S from the restore's segments and refuses another target. | Leave writers free during the interval, which lets a commit land on a discarded root. | adds | sdmxlib, hub | Part 1 (the writer's refusal), Part 3 (expiry's), Part 4 (the rerun) |
| S34 | | Withdrawn (O5). R9 carries the question to the server-restore design. | | | | |
| S35 | Spec §6.9 Restores, `cleaned_through` | A restore leaves `cleaned_through` as it was. Expiry skips `restore` segments when it walks the pending segments. A rerun of a restore that stopped before its `HISTORY` write writes new segments, and the earlier ones stay as orphans. | Adopt the earlier `restore` segments, as revision 1 proposed. | adds | sdmxlib, hub | Part 3 (the skip), Part 4 |
| S36 | Spec §6.9 `verify-catalog` check 3, §11.6 alerts; suite design `verify-catalog` | `verify-catalog` prints an orphan segment that decodes and names the catalog as a note, which does not change its exit status. An unlinked or overlapping segment on the chain is a finding (O11). | A finding, which fails every later run, since no segment is ever deleted. | contradicts ("reports unlinked, orphan and overlapping segments") | sdmxlib, hub | Part 5 |
| S37 | Spec §6.9 step 1, §11.6; suite design step 1, Bounds | On a catalog directory, W is measured by the machine's clock, and W is at least `prune`'s `older_than` plus one day (O10). | The head root's `created_at`. | contradicts ("by the storage clock", "in storage time") | sdmxlib, hub | Part 3 |
| S38 | Spec §6.9 `verify-catalog` check 2, §10.3; suite design `verify-catalog` | The head's entries get the full edition checks. Every other retained root gets its leaves checked: present, with bytes that hash to their names, and valid. | Full edition checks for every root that is no checkpoint, which fails once `prune` or GC has deleted a retired edition. | contradicts ("checks a checkpoint's leaves only") | sdmxlib, hub | Part 5 |
| S39 | Spec §6.9 The catalog lock, Locally; suite design Exclusion | A commit waits at most 120 s for the lock and is then refused (O7). On Windows, and on a filesystem that refuses advisory locks, a stage-2 writer commits to a 1.x catalog without the lock and refuses a 2.x catalog, opt-in, expiry and a restore (O2, O6). | `LockFileEx` on Windows, as the texts say today. | contradicts ("every Inventory write", "`LockFileEx` on Windows") | sdmxlib, hub | Part 1 |
| S40 | Spec §6.9 Restores; suite design Restores | A restore refuses a target at or below `expired_through` (O1), a target below `since_seq - 1`, a target with no root above it, and a 1.x catalog. | Allow a target below `expired_through`, with the exemptions revision 1 listed. | adds | sdmxlib, hub | Part 4 |
| S41 | Spec §6.1 Opt-in; suite design "Catalogs that expire" | Opt-in on a catalog with no Inventory version records `since_seq: 1`. Opt-in reads every root, checks the chain from `since_seq` to the head, and refuses a missing or undecodable root. | The two refusals the texts list, which let a broken chain through to stop every expiry at step 2. | adds | sdmxlib, hub | Part 2 |
| S42 | Spec §6.9 steps 3 and 6 | A run that cannot read or decode a retained root writes no segment. A run that finishes pending segments in that state deletes no leaf. | Delete on the roots it could read, which can delete a leaf the unread root names. | adds | sdmxlib | Part 3 |
| S43 | Spec §6.9 step 8 | A dry run of expiry, opt-in or a restore takes no lock and writes no file. Its first half in revision 1 is withdrawn (O1). | A dry run under the lock, which creates `LOCK` and blocks writers for a read. | clarifies | sdmxlib | Part 2 |
| S44 | Spec §6.6 rule 5; suite design "Writer rules" | A base at or below `expired_through`, or of an epoch before `HISTORY`'s, is a conflict: the writer reads the head again and applies its change to it, as it does after a lost race. | A refusal, which fails a seal for a base its own retry replaces. | contradicts ("refuses a base") | sdmxlib, hub | Part 1 |
| S45 | Spec §6.9 The catalog lock; suite design Exclusion | `try_commit` holds the lock from before its `catalog.json` decode to after `CURRENT`. | From the first leaf check, as the texts say, which lets an opt-in fall between the decode and the leaf check. | clarifies (a wider span) | sdmxlib, hub | Part 1 |
| S46 | Spec §6.9 Expiry, "Each run, under the catalog lock"; suite design Steps | Expiry steps 0 to 2 and the plan of step 3 read without the lock. Under the lock the run reads `HISTORY` and `catalog.json` again, stops when either changed, lists `inventory/` again, and performs steps 3 to 7. A run takes at most 1,000 candidates. A run that finds pending segments finishes steps 5 to 7 for them and takes no candidate (O7). | Hold the lock for the whole run, which blocks every commit while the run reads each retained root. | contradicts | sdmxlib, hub | Part 3 |
| S47 | Spec §6.9 Segments | One run names every segment it writes with one UUIDv7. A restore's segments are the `restore` segments on the chain that share the newest segment's UUID. | Derive a restore's segments from their rosters, which fails across two restores. | adds | sdmxlib, hub | Part 3 |
| S48 | Spec §6.9 Restores step 5; suite design Restores step 5 | Root S + 1's added tombstones are computed from the change lists of the `restore` segments' lines: each edition in a leaf those lists name that root S records in neither a leaf nor `retired`. Restore step 4 keeps a root that carries the new epoch. | Compute them from the discarded roots' `products`, which a rerun cannot read once restore step 4 has deleted the roots. | clarifies | sdmxlib, hub | Part 4 |

## Cross-repo contract

This design writes the documents sdmxlib 0.88 already decodes and changes no format. It does
propose changes to the rules: sixteen rows of the table above stand, fourteen of them also
change the suite design, and seven contradict a sentence of revision 5. The hub's rule is that "a
local design that needs a contract change brings it back to the suite design first". Every part
is therefore provisional until the suite design's revision 6 and the spec carry the rows that
gate it (the land order names them).

The table lists each output, its writer and its readers.

| Output | Writer | Readers |
|---|---|---|
| `<catalog>/LOCK` | every stage-2 writer on local disk | none; no code reads its content |
| `catalog.json` 2.0 with `history.since_seq` | opt-in; a restore's epoch bump | sdmxlib's reader fallback, `statcat gc`, every writer's decode |
| `inventory/HISTORY` | opt-in, the engine, a restore | sdmxlib's reader fallback, `statcat gc` (`gc.py:303-319`), `verify-catalog`, `doctor` |
| `history/<epoch>/<first>-<last>-<uuid7>.jsonl` | the engine, a restore | `verify-catalog`, `doctor`; a stage-3 exporter |
| The `expiry.*` and `restore.*` events (E12) | the engine, a restore | Phase 8 alerting, through release-job Part 6's `statcat release` |
| `ExpiryStorage`, and `InventoryStorage.exclusive()`, `check()` and `stage_2` (E2, E8) | this design | release-job Part 6 |

Dependencies stay one way: the engine imports sdmxlib, and sdmxlib imports nothing of
statcatlib's.

## Handoff

### sdmxlib (StatCat-Suite/sdmxlib#361)

sdmxlib 0.88 ships every document stage 2 writes: `History`, `SegmentHeader`, `SegmentLine`,
`CatalogHistory`, `encode`, `encode_segment`, `segment_line`, `segment_path`, `decode_history`,
`decode_segment`, `decode_catalog(decoder="stage-2-writer")`, `Root.has_history`, the reader
fallback and the nine JSON Schemas. It ships no stage-2 rule, check or conformance case
(sdmxlib's `conformance.py:21-23`, `:715-723`). Under O3 one release, 0.89.0, carries N1 to N4.
sdmxlib `main` already holds the `tiers` argument of StatCat-Suite/sdmxlib#391 (`4886fdb`),
which ships in the same 0.89.0.

**N1. Pure history rules, in `sdmxlib.inventory`.**

- `is_checkpoint`: H4's rule, from the `seq`, `created_at` and `restore_epoch` of a root or
  line and those of version `seq - 1`.
- `expiry_candidates`: expiry step 1's walk over those facts, K, W, the time, `CURRENT.seq`,
  the head's `seq`, `expired_through`, `since_seq` and a limit. It returns the candidates, the
  checkpoints it stepped over and why the walk ended.
- `SEGMENT_MAX_LINES` (1,000) and `SEGMENT_MAX_BYTES` (64 MiB).
- `restore_retired`: S48's tombstones for root S + 1, from root S, its leaves and the leaves
  the discarded lines name.
- `decode_segment_header(raw) -> SegmentHeader`: a header from a segment's first line alone.
  0.88's `decode_segment` needs the whole segment, and E10 reads headers by range.
- `parse_segment_path(path)`: the epoch, `first`, `last` and UUID of a segment's path, or
  `None` for any other name. 0.88 keeps the pattern private.

**N2. `check_history(fetch, *, roots, segments) -> list[HistoryFinding]`.** It runs
`verify-catalog`'s checks 1 to 3 of spec §6.9, with S36 and S38, over a `Fetch`, the listed root
numbers and the listed segment paths. A finding carries its kind, so an orphan can print as a
note. It stands beside `verify_entry`, which already serves check 4.

**N3. Stage-2 conformance.**

- An `ExpiryBackend` protocol that extends `HistoryBackend` with `opt_in()`, `expire(*, now,
  keep_roots, window, stop_after=None)`, `restore(seq, *, stop_after=None)`, `verify()`,
  `list(prefix)` and `delete(path)`. `stop_after` counts the storage's creates, replaces and
  deletes: the backend stops as a killed process would after that many.
- The checks in an `EXPIRY_CHECKS` tuple, and `run_expiry`.
- The cases are the open ones of StatCat-Suite/sdmxlib#361, written to the accepted S rows:
    - opt-in is refused when a root without the history fields follows one with them;
    - a base at or below `expired_through` gives `ConflictError` after the lock (S44);
    - with `CURRENT` lagging by more than K, expiry step 0 advances it and step 1 takes no
      root at or above it;
    - the walk stops at a root above `since_seq` without the fields;
    - a stop after each storage change of a run of several segments: linked orphans are
      adopted, an unlinked orphan is left, and `cleaned_through` limits the next run (S46);
    - a root below `expired_through` with no matching line is kept;
    - a restore writes its `restore` segments, readers fail closed until root S + 1 exists,
      and a target at or below `expired_through` is refused (S40);
    - `verify()` passes across a restore, keyed by `(restore_epoch, seq)`;
    - `verify()` reports an edited segment line and a deleted root, and reports an orphan as a
      note (S36).
- The cases state each expected checkpoint by hand, so a fault in `is_checkpoint` fails them.

**N4. A test for a tombstone of an unrecorded product.** Root S + 1 can carry a change item
whose `leaf_sha256` is `{"from": null, "to": null}`, for a tombstoned edition of a product root
S does not record. The review read that 0.88's decode, `validate_root` and `root-1.2` schema
accept it (sdmxlib's `_rules.py:634-666`, `_format.py:839-880`); no test writes one.

**N5. The spec text** of each S row the project owner accepts, in one PR.

**N6. The reference writer.** sdmxlib either runs the stage-2 cases against its own reference
writer, with `stage_2 = True`, or states that they run against statcatlib's writer alone.

**N7. The release, 0.89.0,** with N1 to N4. statcatlib raises its cap to
`sdmxlib>=0.89,<0.90` in Part 3, and sdmxlib-rest follows.

sdmxlib's own design fixes the names and signatures of N1 to N3. Before Part 3 starts, the
session that builds it reads that design and revises E9, E10, E14 and E15 where a name or a
signature differs.

| Row | Needed by | Blocks |
|---|---|---|
| N1 | E9, E10, E14 | Parts 3 and 4 |
| N2 | E15 | Part 5 |
| N3 | GH-125's first acceptance item | Part 5 |
| N4 | E14 | Part 4 |
| N5 | every S row | the part each row gates |
| N6 | N3 | no part |
| N7 | Parts 3 to 5 | Part 3 |

sdmxlib needs no release before Part 1 or Part 2, which run on 0.88. Both still wait for the S
rows that gate them (N5).

### The hub

Fourteen S rows change the suite design: S32, S33, S35 to S41 and S44 to S48. The hub session
applies them as revision 6, with its own review, before the spec PR of N5. The land order has
the row.

### sdmxlib-rest

sdmxlib-rest raises its sdmxlib cap to 0.89 after N7. This design asks nothing else of it:
sdmxlib-rest reads no Inventory object, and every release from 0.10.0 already reports
`sdmxlib_version` in `GET /` and requires sdmxlib 0.85 or later, which has the reader fallback.
Revision 1's R10 said no release reported the version. That was a reading error, and R10 is
withdrawn.

### The release-job design's Part 6

Release-job handoff row 11 calls for a re-review of release-job Part 6 against this design. E16
lists the changes its text needs (R1 to R12). This design edits no line of
`docs/design/release-job.md`.

## Critical user journeys (CUJs)

### CUJ 1: opt-in and expiry on a laptop

A catalog directory holds 400 Inventory versions. The catalog owner upgrades every tool that
reads it and runs `statcat admin opt-in ./catalog`. The command takes no lock and writes no
file. It prints `since_seq`, the head and the three statements of E6, and exits 0. `statcat
admin opt-in ./catalog --apply` writes `inventory/HISTORY`, then `catalog.json` 2.0.

A month later `statcat prune ./catalog --older-than 30 --expire-older-than 31 --reason "monthly
prune"` prints the plan and changes nothing. With `--apply` it retires old editions in one
`retire` version. It then writes one segment, advances `HISTORY`, and deletes the expired roots
and the leaves no retained root names, printing one `expire` line for each. `statcat doctor
./catalog` shows `expired_through`, the newest segment and the newest checkpoint.

### CUJ 2: a seal concurrent with `prune`

A build script seals product `lfs` while `statcat prune --apply` expires old versions. The
engine plans without the lock, so the seal commits during the plan. When the engine then takes
the lock, it lists `inventory/` again, reads the seal's root, and keeps every leaf that root
names.

When the seal's `try_commit` asks for the lock while the engine holds it, the seal waits. It
then reads `HISTORY` and its base's stored bytes, writes its leaves and its root, and every leaf
its root names exists. The seal exits 0 in both orders. A seal that waits longer than 120 s
fails with `CatalogLockTimeoutError` and keeps its journal entry.

### CUJ 3: a crash during expiry

`prune` is killed after it advanced `HISTORY` and deleted half the candidate roots. The OS
releases the lock. Readers are unaffected: `CURRENT` names a root the engine does not take, and
the reader fallback starts above `expired_through`. The next `prune --apply
--expire-older-than 31` finds the pending segment after `cleaned_through`. It deletes each
remaining root whose bytes match its line, deletes the leaves, advances `cleaned_through`,
prints `recovered` with one `expire` line per deletion, and exits 0. It takes no new candidate;
the run after it does.

### CUJ 4: restore of an opted-in catalog

A disk fault damages roots 414 and 415 of a catalog whose head is 415 and whose
`expired_through` is 380. `statcat verify-catalog` names both files. The operator runs `statcat
admin restore ./catalog --to 412 --reason "jdoe: disk fault"`. The command takes no lock and
writes no file. It prints:

```text
discard version 413 2026-12-01T09:14:02Z withdraw by jdoe@host: wrong data
  14100287/019a2b00-… embargoed -> withdrawn
unread version 414
unread version 415
embargo at version 412: off
INCOMPLETE 2 version(s) could not be read; their changes are unknown
```

With `--apply` it writes one `restore` segment that lists roots 413 to 415 and marks 414 and
415 `unread`, writes `HISTORY` with epoch 1, bumps `catalog.json`, deletes the three roots, and
writes root 413 of epoch 1 with `event.kind` `restore`. The operator repeats the cancellation
the report lists with `statcat admin cancel`, and checks every embargoed entry by hand, since
the report is incomplete.

### CUJ 5: an older tool on an opted-in catalog

A script on statcatlib 0.16 opens the opted-in catalog. `Catalog.open` and every view work. Its
next `seal` raises `InventoryWriteRefusedError`, which names format 2.0, before it writes
anything. A script on statcatlib 0.13 fails at `Catalog.open` with `InventoryError`.

## Technical plan

### Design decisions

| ID | Decision | Settles |
|---|---|---|
| E1 | The local lock is `flock` on `<catalog>/LOCK`, polled for at most 120 s, and unsupported on Windows. | GH-125 part 2, H12, O2, O7 |
| E2 | `InventoryStorage` gains `exclusive()`, `check()` and `stage_2`. | release-job D13's interface |
| E3 | `try_commit` holds the lock from before its `catalog.json` decode to after `CURRENT`. | GH-125 part 2, S45 |
| E4 | On a 2.x catalog a stale base is a conflict that `commit` retries, and one function defines an unfinished restore. | spec §6.6 rule 5, S33, S44 |
| E5 | Windows and a filesystem that refuses advisory locks keep 1.x catalogs writable and refuse stage 2. | O2, O6 |
| E6 | Opt-in verifies without the lock, then writes `HISTORY` and `catalog.json` 2.0 under it. | spec §6.1, O4, O7 |
| E7 | The writer decodes as a stage-2 writer where its storage locks. | spec §6.1 Decoding |
| E8 | The engine is one function over `ExpiryStorage`, which extends `InventoryStorage`. | release-job D13, release-job Q9 |
| E9 | A run plans without the lock, reads whole roots, and takes at most 1,000 candidates. | spec §6.9 steps 0 to 2, O7, O8 |
| E10 | Expiry step 3 writes segments per epoch under one UUID, adopts linked orphans, and reads segment headers by range. | spec §6.9 step 3, S47 |
| E11 | Expiry steps 4 to 7 work from the pending segments, and a run that finds pending segments only finishes them. | spec §6.9 steps 4 to 7, Crashes |
| E12 | The engine emits one event per action, and expiry step 8 closes the run. | spec §6.9 step 8 |
| E13 | `prune` expires when given `inventory_window`, by the storage's clock. | O9, O10 |
| E14 | `CatalogAdmin.restore` runs spec §6.9's order above `expired_through` and resumes an unfinished restore. | spec §6.9 Restores, O1, O5, Q1 |
| E15 | `verify-catalog` runs sdmxlib's history checks, and `doctor` reports `HISTORY`, the segments and the lock. | spec §6.9 `verify-catalog`, O11, Q2 |
| E16 | GC is unchanged, and the release job keeps its 2.x refusal until release-job Part 6. | release-job D1, D10, D13 |
| E17 | The pure rules come from sdmxlib 0.89, and the engine holds the reads, the writes and their order. | O3 |
| E18 | "Live at time T" is a replay from a checkpoint over the change lists of roots and `expiry` lines. | Goal 6 |
| E19 | No dry run takes the lock or creates `LOCK`. | S43 |

E1, E3, E4, E6, E8, E9, E10, E13, E14 and E17 each end with a table of the options weighed. The
options of E5, E15 and E19 are in O6, O11 and S43. E2, E7, E11, E12, E16 and E18 follow from the
spec and the decisions around them and record no alternative.

### E1: the local catalog lock

`LocalInventoryStorage.exclusive()` calls `store.lock.hold(path, timeout=...)`, which opens
`<catalog>/LOCK` with `os.open(path, O_RDONLY | O_CREAT, 0o666)`, locks it, yields, unlocks it
and closes it. The first call creates the file. No code writes to it, reads it or deletes it.
The OS drops the lock when the holding process exits, so a killed holder leaves `LOCK` in place
and the next commit proceeds.

- **The call.** `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, retried every 50 ms. A `flock` lock
  belongs to the open file description, so two threads of one process that each open `LOCK`
  exclude each other, and so do two processes. The file is opened read-only, which `flock`
  accepts, so a second OS user who can read the catalog can take the lock.
- **The wait.** `LOCK_TIMEOUT` is 120 s, the figure release-job S26 gives the Azure lease (O7).
  A caller that waits longer gets `CatalogLockTimeoutError`, a subclass of
  `InventoryWriteRefusedError`, whose message names `LOCK` and says another process holds it.
  A seal refused this way keeps its journal entry, as for every `InventoryWriteRefusedError`.
  `LocalInventoryStorage(root, *, clock=None, lock_timeout=LOCK_TIMEOUT)` lets a test shorten
  the wait and set the clock.
- **Release.** The holder calls `flock(fd, LOCK_UN)` before `os.close(fd)`.
- **Not reentrant.** A module-level table in `store/lock.py`, guarded by a `threading.Lock`,
  maps each resolved `LOCK` path to the thread that holds it. A thread that calls `exclusive()`
  while it holds the same lock gets `RuntimeError` at once, whichever storage object it calls
  through. Code that holds the lock and writes a root calls `InventoryWriter._commit_locked`
  (E3).
- **Windows.** `store/lock.py` imports `fcntl` under `if sys.platform != "win32":`. On Windows
  `hold` raises `CatalogLockUnsupportedError` without opening a file (O2, E5). That branch
  carries `# pragma: no cover`, since the gate runs on macOS and Linux.
- **A forked child.** A child created by `fork` without `exec` while the lock is held shares
  the open file description. If the parent is then killed, the lock stays until the child exits.
  statcatlib forks nowhere, and its tests start processes with `spawn`. Goal 1 covers a holder
  with no such child.

`prune` never nests the lock. It commits its `retire` version through `commit`
(`admin.py:693`), whose `try_commit` takes and releases the lock, and only then calls the
engine, which takes the lock for its writes. A seal may commit between the two; the engine
chooses from the head it then finds.

| Option | Gain | Cost |
|---|---|---|
| **`flock`, polled, POSIX only (chosen).** | Standard library only, no `Any`, a bounded wait, exclusion between threads. | No lock on Windows (O2). |
| `msvcrt.locking` or `LockFileEx` on Windows as well. | The lock the suite design names. | Its error for a held lock is the one a filesystem without locks would give, and no Windows machine runs the gate (review M4). |
| `fcntl.lockf` (POSIX record locks). | Works on more network filesystems. | The process loses the lock when any descriptor of the file closes, and two threads of one process share it. |
| A third-party lock package. | One call for every platform. | A new base dependency for one small module. |
| A lock file created with `O_EXCL` and unlinked on release. | No OS lock. | A killed holder leaves a stale lock, which H12 rules out. |
| A reentrant lock. | `prune` and a restore could call `commit` while they hold it. | The suite design orders the calls so that no caller nests the lock, and reentrancy hides which call holds it. |

### E2: `exclusive()`, `check()` and `stage_2` on `InventoryStorage`

`InventoryStorage` (`store/inventory.py:322-353`) gains three members and stays
`@runtime_checkable`:

```python
class InventoryStorage(Protocol):
    @property
    def stage_2(self) -> bool: ...
    def exclusive(self) -> AbstractContextManager[None]: ...
    def check(self) -> None: ...
```

- `exclusive()` holds the catalog lock for the block. It waits at most the storage's timeout,
  then raises `CatalogLockTimeoutError`. Where the platform or the filesystem has no lock it
  raises `CatalogLockUnsupportedError`.
- `check()` raises `CatalogLockLostError` when the process may have lost the lock it took
  through `exclusive()`. On Azure that is a lease whose last renewal is 40 s old or more. On
  local disk a live process cannot lose an OS lock, so `check()` returns. Called outside
  `exclusive()` it raises `RuntimeError`. The engine and a restore call it before each segment
  create, each `HISTORY` or `catalog.json` replace, and each root or leaf delete.
- `stage_2` says whether this storage implements the catalog lock. The writer picks its
  `catalog.json` decoder by it (E7). On local disk it is true, also on a filesystem where the
  lock call then fails; E5 covers that case.

| Storage | `stage_2` | `exclusive()` | `check()` |
|---|---|---|---|
| `LocalInventoryStorage` | `True` | E1 | returns while the lock is held |
| `BlobInventoryStorage`, until release-job Part 6 | `False` | yields at once; today's blob writer takes no lock | returns |
| `BlobInventoryStorage` over `AzureStore`, from release-job Part 6 | `True` | takes the `HISTORY` lease: the held lease a release run passes in, else one it acquires within 120 s. On a catalog with no `HISTORY` it yields at once. | raises `LeaseLostError` when the last renewal is 40 s old or more |
| `BlobInventoryStorage` over another store | `False` | yields at once | returns |

Release-job D13 asks for "`exclusive()` and `check()` on the storage". This design keeps both
and adds `stage_2`: a storage with no lock must make the writer refuse a 2.x catalog, and the
decoder is where sdmxlib puts that refusal. The three lock errors subclass
`InventoryWriteRefusedError`, and release-job Part 6's `LeaseLostError` subclasses
`CatalogLockLostError` (R4).

### E3: the lock span of `try_commit`

`try_commit` (`store/inventory.py:623-697`) takes the lock and calls `_commit_locked`:

```python
def try_commit(self, base, leaves, *, ..., kind, reason) -> Root:
    try:
        with self._storage.exclusive():
            return self._commit_locked(base, leaves, ..., kind=kind, reason=reason)
    except CatalogLockUnsupportedError:
        self._refuse_unlocked_2x()   # E5: raises on a 2.x catalog, warns once on a 1.x one
        return self._commit_locked(base, leaves, ..., kind=kind, reason=reason)
```

`_commit_locked` is today's body with the 2.x checks added. Its steps are the "commit steps":

1.  read the clock when the caller gave no `now`. Line `:665` moves inside the lock, so a
    commit that waited records the time it wrote;
2.  `check_writable(base, now=now)` (`:666`): decode `catalog.json` (E7) and refuse a base more
    than 5 minutes ahead;
3.  on a 2.x catalog, read `inventory/HISTORY` and run E4's checks;
4.  resolve the actor (`:667`) and read the base's stored bytes (`:668`);
5.  write the changed leaves (`:670-673`), where the local storage skips a leaf it finds present
    (`:385`);
6.  build and create the root (`:675-695`);
7.  advance `CURRENT` (`:696`).

GH-125, the spec and the suite design put the lock "from the first leaf existence check to
`CURRENT`". This span starts earlier, at commit step 1, so the `catalog.json` decode and the
`HISTORY` read are under the lock too (S45). An opt-in, an expiry or a restore then falls wholly
before or wholly after a commit's checks.

`commit` (`:713-771`) reads the head (`:747`), computes the change and runs the cross-product
rules outside the lock, as today. The lock covers the writes of one version. E4 adds two
branches to its loop.

`check_writable` stays public and takes no lock, for the seal's pre-check
(`store/working_copy.py:1622`). It runs commit step 2 and the refusals of commit step 3: a
missing or undecodable `HISTORY`, and differing epochs. It raises for each one it sees. A pass
is advisory, since an opt-in or a restore can follow it, and `_commit_locked` repeats every
check under the lock. The pre-check leaves the two conflicts of E4 to `_commit_locked`.

| Option | Gain | Cost |
|---|---|---|
| **The lock in `try_commit`, from the decode to `CURRENT` (chosen).** | `try_commit` is public, so every path that writes a root is covered: `commit`, the conformance adapter, `inventory_corpus`. | A caller that already holds the lock and calls `try_commit` gets `RuntimeError` from the thread table. |
| The lock in `commit`, around the head read and the change. | The base cannot be stale. | The lock covers the change callable, which reads every leaf and runs the cross-product rules. Direct `try_commit` callers go unlocked. |
| The lock from the first leaf write (the texts' wording). | The shortest span. | An opt-in can fall between the decode and the leaf check. |

### E4: the commit checks on a 2.x catalog, and the unfinished restore

**One definition.** `InventoryWriter.unfinished_restore() -> int | None` returns S while a
restore is unfinished and `None` otherwise. Every caller uses it: `commit`, the engine,
`restore`, `verify_catalog` and `doctor`. It needs an `ExpiryStorage` (E8), and its rule is:

1.  Read `catalog.json` and `HISTORY`. A 1.x catalog gives `None`.
2.  Read the header of `HISTORY.newest_segment`. When there is no segment, or its kind is
    `expiry`, the answer is `None`. If the epochs of `catalog.json` and `HISTORY` differ in that
    case, the function raises `InventoryError`: no step of any operation leaves that state.
3.  The newest segment is a `restore` segment. Its restore's segments are the `restore`
    segments reached by `previous_segment` that carry the same UUID in their names (S47). Let
    `lowest` be the least `first` among them.
4.  S is the highest root under `inventory/` below `lowest` whose `restore_epoch` is below
    `HISTORY`'s. The restore listed every root above S, so no older-epoch root lies between S
    and `lowest`.
5.  The restore is finished when `catalog.json` carries `HISTORY`'s epoch and root S + 1 exists
    and carries it too. The function then returns `None`. Otherwise it returns S.

Root S + 1 is the restore's commit point. It is the first root of its epoch, so it is a
checkpoint and no expiry removes it while the newest segment is that restore's.

**The checks.** Commit step 3 reads `inventory/HISTORY` fresh, under the lock, and runs these in
order:

1.  **No `HISTORY`, or one that does not decode.** `InventoryWriteRefusedError`: a 2.x catalog
    without `HISTORY` is an error (spec §6.1).
2.  **Differing epochs.** When `catalog.json`'s `restore_epoch` differs from `HISTORY`'s, a
    restore stopped between its steps 2 and 3. The commit raises `UnfinishedRestoreError`, a
    subclass of `InventoryWriteRefusedError`.
3.  **A base of another epoch.** When the base's `restore_epoch` differs from `HISTORY`'s, the
    commit raises `StaleBaseError`, a subclass of `InventoryConflictError`. One commit is
    exempt: a version of kind `restore` whose base carries an epoch below `HISTORY`'s, which
    is a restore's root S + 1.
4.  **A stale base.** When `base.seq <= HISTORY.expired_through`, the commit raises
    `StaleBaseError`. O1 keeps every restore target above `expired_through`, so the restore's
    commit passes this check as any commit does.

A restore's own commit runs all four checks and passes them. No parameter skips a check.

**The retry.** `commit` catches `InventoryConflictError`, reads the head again and applies the
change again (`:764-768`), which is what spec §6.6 asks of a writer that lost a race (S44). It
gains two branches, so that an unfinished restore gives its own message:

- when `commit` catches `StaleBaseError`, it calls `unfinished_restore()`. A value S raises
  `UnfinishedRestoreError`; `None` retries;
- when `self.full()` raises `InventoryError` on a 2.x catalog, it calls `unfinished_restore()`.
  A value S raises `UnfinishedRestoreError`; `None` re-raises. This is the state after restore
  step 4, where `CURRENT` names a deleted root and the reader fails closed.

`UnfinishedRestoreError` names S and `CatalogAdmin.restore(S, ...)`. Every commit raises it
until an operator reruns the restore (E14). The slow function runs only on these two error
paths, so a healthy commit pays one read of `HISTORY`, a file under 1 KB.

The engine never takes a root at or above `CURRENT.seq` or among the newest K. A base that
`commit` just read is therefore stale only after K later versions and an expiry, or after a
restore.

| Option | Gain | Cost |
|---|---|---|
| **A conflict, retried by `commit` (chosen).** | A seal whose base an expiry or a finished restore made stale retries on the current head. | A rule change (S44), and N3's case expects `ConflictError`. |
| `InventoryWriteRefusedError` for a stale base. | The word the spec uses. | A seal fails for a base its own retry loop replaces, and every commit that overlaps a restore fails (review B3). |

### E5: Windows, and a filesystem that refuses advisory locks

`hold` raises `CatalogLockUnsupportedError` in two cases: on Windows (O2), and when `flock`
fails with `ENOLCK`, `ENOTSUP`, `EOPNOTSUPP` or `ENOSYS` (O6; the list is unverified, see "Not
confirmed"). `EWOULDBLOCK` means another holder and is retried. `try_commit` then reads
`catalog.json` without the lock (the `except` branch in E3):

- on a 1.x catalog it commits as statcatlib 0.18 does. For a filesystem refusal it logs one
  WARNING per process and catalog, which names the directory and spec §6.6's "Unsupported
  locations". On Windows it logs nothing, since the rule holds for the whole platform and
  `docs/catalog-format.md` §1.3 states it. `store/lock.py` keeps the set of catalogs it has
  warned about;
- on a 2.x catalog it raises `CatalogLockUnsupportedError`, whose message names the platform or
  the filesystem.

Opt-in, the engine and a restore raise the same error on any catalog. A real run of each
starts by taking the lock once and releasing it, so the error comes before any other read or
write. A catalog on Windows or on such a filesystem therefore never becomes 2.x through
statcatlib. A filesystem that accepts `flock` and excludes no other host is undetectable; spec
§6.6 documents network shares as unsupported, and `statcat doctor` keeps its synced-folder
warning.

A stage-2 writer on such a filesystem can commit to a 1.x catalog while another process opts
the catalog in through a mount that has locks. The commit decoded `catalog.json` before the
rewrite and creates its root after it. Spec §6.1 accepts the same race for stage-1 writers and
covers it: the first expiry is a later call, and expiry step 1 keeps the newest K roots.

### E6: opt-in

```python
def opt_in(writer: InventoryWriter, *, dry_run: bool = False) -> OptIn   # store/expiry.py, Part 2
CatalogAdmin.opt_in(*, dry_run: bool = True) -> OptIn                    # Part 5
```

```text
statcat admin opt-in URL [--apply]                                       # Part 5
```

```python
@frozen
class OptIn:
    since_seq: int
    head_seq: int | None      # None on a catalog with no version
    roots_below: int          # roots below since_seq, which never expire
    already: bool             # the catalog was opted in before the call
    dry_run: bool
```

Part 2 lands `opt_in` in `store/expiry.py`, with no entry in `statcatlib.__all__`, no
`CatalogAdmin` method, no command and no reference page (O4). Tests and Parts 3 and 4 call it.
Part 5 adds the method and the command. The method takes no reason: opt-in writes no Inventory
version, and no document has a field for one (R2). The function's `dry_run` defaults to `False`
and the method's to `True`, as for `expire` and `restore`: code calls the functions, and an
operator calls the methods.

The "opt-in steps":

1.  **Refuse** a storage that cannot run stage 2. A `BlobInventoryStorage` raises
    `ExpiryRefusedError`, which names release-job Part 6. A real run then takes the lock once
    and releases it, so Windows and a lock-refusing filesystem raise
    `CatalogLockUnsupportedError` before any root is read (E5).
2.  **Read** `catalog.json` with its tag. A 2.x document with a valid `HISTORY` returns
    `OptIn(already=True)`. A 2.x document without `HISTORY` raises `InventoryError`.
3.  **Verify, without the lock.** Read every root from the head down to version 1. `since_seq`
    is the lowest `seq` of the unbroken run of roots with the history fields that ends at the
    head, or 1 on a catalog with no version. The step raises `ExpiryRefusedError`, naming the
    root, for each of these (S41):
    - a missing root;
    - a root that does not decode;
    - a head without the history fields;
    - a root with the fields below a root without them;
    - a root between `since_seq` and the head whose `previous_root_sha256` differs from the
      SHA-256 of the root before it.
4.  **A dry run returns here.** It has taken no lock and written no file (E19).
5.  **Lock, and check again.** Under `exclusive()`, read `catalog.json` again. A tag other than
    opt-in step 2's raises `InventoryConflictError`: another process rewrote the file. Probe for
    roots above the head of opt-in step 3 and verify each as that step does. The lock is held
    from here to the end.
6.  **Create `inventory/HISTORY`:** `expired_through: 0`, no segment, `cleaned_through: null`,
    and the catalog's `catalog_id` and `restore_epoch`. If a `HISTORY` exists, a crashed opt-in
    left it: `opt_in` keeps it when it holds exactly these values and raises
    `ExpiryRefusedError` otherwise.
7.  **Replace `catalog.json`** with format `"2.0"` and `history: {"since_seq": N}`, through
    `ExpiryStorage.replace` with the tag of opt-in step 5.

A process killed between opt-in steps 6 and 7 leaves a 1.x catalog with an unused `HISTORY`,
which no reader opens, and a rerun completes the opt-in. Opt-in runs no expiry. The first
expiry is a later `prune` call, and expiry step 1 stops at a root without the history fields,
which covers a stage-1 writer that decoded `catalog.json` before the rewrite (spec §6.1).

The command prints three statements, in a dry run and again with `--apply`. Its lines are in
"Command output and exit codes".

1.  Opt-in cannot be undone.
2.  statcatlib 0.13 and older cannot open the catalog. Versions from 0.14.0 up to the last
    release before `STAGE_2_SINCE` can read it and cannot write it. `STAGE_2_SINCE` is a
    constant in `store/lock.py` that the release PR carrying Part 1 sets to its version.
3.  A local sdmxlib-rest must be 0.10.0 or later, which requires sdmxlib 0.85 and its reader
    fallback.

| Option | Gain | Cost |
|---|---|---|
| **Verify without the lock; a dry run by default; public in Part 5 (chosen).** | The one-way step shows `since_seq` and its refusals first, and blocks no commit while it reads every root. No release offers opt-in before expiry, the checks and the restore. | Two invocations, and a second short verification under the lock. |
| Verify under the lock. | One pass. | Every commit waits while opt-in reads every root, 1.7 GB on the GH-126 corpus (review M5). |
| Public in Part 2. | Opt-in can be tried early. | A catalog opted in on that release cannot expire, be checked or be restored, and every stage-1 writer is refused (review M10). |
| Opt in at `Catalog.create(history=True)`. | New catalogs expire from version 1. | A second opt-in path; `opt_in` on an empty catalog does the same (S41). |

### E7: the writer's stage-2 decode, and older tools

`check_writable` (`store/inventory.py:548-574`) decodes with
`decoder="stage-2-writer" if self._storage.stage_2 else "stage-1-writer"` (`:570`). A local
writer therefore accepts a 2.x catalog from Part 1 on, and a blob writer keeps refusing one
until release-job Part 6 sets `stage_2` for `AzureStore`. The module docstring (`:28-30`), the
error's docstring (`:123-132`) and `docs/catalog-format.md:68-69` change with it.

The table gives each tool's behaviour on an opted-in local catalog. The statcatlib rows were
read in each version's code, and the sdmxlib-rest row in each tag's `pyproject.toml` and
`app.py`.

| Tool | Reads | Writes |
|---|---|---|
| statcatlib 0.13 and older (sdmxlib 0.84 and older) | `Catalog.open` and `CatalogAdmin.open` raise `InventoryError`: `decode_catalog` there reads major 1 only. | none |
| Stage-1 writers: statcatlib 0.14.0 to the last release before Part 1, 0.18.0 included | Everything. The reader decodes majors 1 and 2, and its fallback reads `HISTORY`. `statcat verify-catalog` checks the head. | `seal`, the journal repair and every `CatalogAdmin` change raise `InventoryWriteRefusedError` and write nothing. `prune` with nothing to retire still deletes tombstoned edition directories and unlisted `data/` files; it touches no Inventory object. |
| statcatlib from Part 1 | Everything. | Everything, under the lock. |
| `statcat gc` from 0.17 | It stops its walk at `expired_through`, and stops the run on a missing root above it (`gc.py:321-335`, `:352-377`). | none in `inventory/` |
| sdmxlib-rest | It reads no Inventory object; its consumer's provider does. From 0.10.0 it requires sdmxlib 0.85, whose reader has the fallback, and reports `sdmxlib_version`. With sdmxlib 0.84 or older, a reader that cannot read `CURRENT` can mistake a checkpoint for the head. | none |

A stage-1 writer and a stage-2 writer may share a 1.x catalog. The first takes no lock, and
nothing there expires.

### E8: the expiry storage

The engine, opt-in and a restore reach the catalog through `ExpiryStorage`. It is a
`@runtime_checkable` `Protocol` defined in `store/inventory.py` beside `InventoryStorage`, so
that `InventoryWriter.unfinished_restore` can use it and `store/expiry.py` imports it with no
cycle.

```python
@frozen
class Listed:
    path: str                 # from the catalog root
    etag: str | None

type Deleted = Literal["deleted", "absent", "changed"]

@runtime_checkable
class ExpiryStorage(InventoryStorage, Protocol):
    def list(self, prefix: str, *, shallow: bool = False) -> Iterator[Listed]: ...
    def read_tagged(self, path: str) -> tuple[bytes, str | None] | None: ...
    def read_range(self, path: str, length: int) -> bytes | None: ...
    def etag(self, path: str) -> str | None: ...
    def delete(self, path: str, *, if_match: str | None = None) -> Deleted: ...
    def replace(self, path: str, data: bytes, *, etag: str | None) -> str | None: ...
```

It inherits `location`, `read`, `create`, `write_leaf`, `advance_current`, `now`, `actor`,
`exclusive`, `check` and `stage_2`. The engine calls every one of them but `write_leaf`.

| Method | Contract | Local disk (this design) | Azure (release-job Part 6) |
|---|---|---|---|
| `list` | Every object under `prefix`, in any order; with `shallow`, its direct children only. Staged files are included, and callers filter by name. | `os.scandir`, or a walk | `obstore.list`, or `list_with_delimiter` |
| `read_tagged` | Bytes and tag, or `None` when absent | the file's bytes, and a tag built from `st_ino`, `st_mtime_ns` and `st_size` | `_ObjectOps.read` (`store/blob.py:179-187`) |
| `read_range` | The first `length` bytes, or the whole object when it is shorter; `None` when absent | a short read | `obstore.get_range` |
| `etag` | The tag alone, or `None` when absent | `os.stat` | `obstore.head` |
| `delete` | `"deleted"`; `"absent"` when no object is there; `"changed"` when its tag differs from `if_match` | `os.unlink`. It ignores `if_match`, since the lock excludes every writer, and never answers `"changed"`. | `Delete Blob` with `If-Match`, through `AzureBlobRest` |
| `replace` | Replace `inventory/HISTORY` or `catalog.json` when its tag is `etag`, or create it when `etag` is `None`. It returns the new tag, raises `InventoryConflictError` when the tag differs, and `ValueError` for any other path. | `os.stat`, compare, then the existing `_stage` and `_fsync_dir` helpers around `os.replace` (`store/inventory.py:289-311`) | a conditional Put, with the lease ID for `HISTORY` |
| `create` | Create-if-absent, complete before visible (segments) | `create_exclusive` | `put(mode="create")` |

A store fault raises `OSError` on local disk and `BlobStoreError` on a blob store. The engine
catches both from a `delete`, records the path in `ExpiryReport.failed` and goes on. From any
other call it lets them propagate, which ends the run as a killed process would.

A tag is opaque to the engine. Each local `replace` renames a new file over the name, so the
tag changes with it.

Differences from the interface release-job D13 lists, each with its reason:

- `read(path) -> (bytes, etag)` is named `read_tagged`. `InventoryStorage.read` already returns
  bytes alone, and the reader's `Fetch` depends on that.
- `write_history(data, *, etag)` becomes `replace(path, data, *, etag)`. Opt-in and a restore
  rewrite `catalog.json` by the same read, compare and write (spec §6.1).
- `etag(path)` is added for expiry step 6's final check, which needs a leaf's tag and no bytes.
- `list` gains `shallow`, so the engine lists the roots without paging through every leaf.
- `delete` answers one of three outcomes, so expiry step 5 can tell a root that is gone from
  one that changed.
- `read_range` stays and changes its use: the engine reads segment headers with it, and reads
  roots whole (O8).

`ExpiryStorage`, `expire`, `opt_in` and `restore` are internal until release-job Part 6: they
have no entry in `statcatlib.__all__` and no page under `docs/reference/`. The public surface
of this design is `CatalogAdmin`'s methods, the three report classes, the error classes and the
commands.

| Option | Gain | Cost |
|---|---|---|
| **One protocol that extends `InventoryStorage` (chosen).** | The engine takes one object for the lock, the clock, the actor and the calls, and release-job Part 6 writes one class. | `LocalInventoryStorage` gains six methods. |
| A separate expiry storage beside the inventory storage. | `InventoryStorage` implementers are untouched. | Two objects that must name one catalog and one lock. |
| The engine written against local paths, with release-job Part 6 writing its own. | No protocol. | Two engines, and the crash rules tested once per store. |

### E9: the run, and expiry steps 0 to 2

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

The nine expiry steps, with where each runs:

| Expiry step | Action | Lock | Section |
|---|---|---|---|
| 0 | Move `CURRENT` to the head | no | E9 |
| 1 | Choose the candidates | no | E9 |
| 2 | Check the chain over them | no | E9 |
| 3 | Plan the segments, then write them | plan: no; writes: yes | E10 |
| 4 | Advance `HISTORY` | yes | E11 |
| 5 | Delete the roots of the pending segments | yes | E11 |
| 6 | Delete the leaves no retained root names | yes | E11 |
| 7 | Advance `cleaned_through` | yes | E11 |
| 8 | Write the cache file, emit `expiry.finished`, return the report | no | E12 |

**Before any step.** The writer's storage must be an `ExpiryStorage` whose `stage_2` is true;
any other raises `TypeError`. `keep_roots` below 2 and a zero or negative `window` raise
`ValueError`. The engine reads `catalog.json` and `HISTORY` with their tags, and reads `now`
once from `storage.now()`. It raises `ExpiryRefusedError`, a subclass of
`InventoryWriteRefusedError`, for a 1.x catalog, for a 2.x catalog with no valid `HISTORY`, and
when `writer.unfinished_restore()` returns a value (S33). A real run on Windows or on a
lock-refusing filesystem raises `CatalogLockUnsupportedError` before expiry step 0, from the
first lock call E5 describes.

**The lock (O7, S46).** Expiry steps 0 to 2 and the plan of step 3 take no lock. The engine then
enters `storage.exclusive()` and, before any write:

1.  reads `HISTORY` and `catalog.json` again. A tag other than the one it planned with means
    another expiry, an opt-in or a restore ran. The run stops with `stopped =
    "history-changed"` and writes nothing;
2.  lists `inventory/` shallowly again and reads each root the plan lacks. These are the
    versions committed since the plan began. Their leaves join the retained set.

It holds the lock from there through expiry step 7. Versions committed during the plan only
raise the head, so every candidate stays one a fresh choice would take.

**A recovery run (S46).** When pending segments exist at the start, a crashed run left them.
The run performs expiry step 0, then steps 5 to 7 for those segments under the lock, and takes
no candidate. `ExpiryReport.recovered` is true. The next run chooses again. A run that takes
candidates therefore never shares a deletion with an older plan.

**Expiry step 0.** `writer.advance_current()` (`store/inventory.py:773-803`), then a read of
`CURRENT`. When `CURRENT` is still missing or malformed, the run takes no candidate and stops
with `stopped = "no-current"`.

**Expiry step 1.** With the head from the reader protocol, the engine walks `seq` upward from
`max(expired_through + 1, since_seq)`. It reads each root whole and treats it by the first row
that applies:

| The root | The walk | `walk_end` | Event |
|---|---|---|---|
| is at or above `CURRENT.seq` | ends | `current` | none |
| is above `head.seq - keep_roots` | ends | `keep-roots` | none |
| is missing | ends | `missing-root` | `expiry.stopped_at_root` |
| does not decode | ends | `undecodable-root` | `expiry.stopped_at_root` |
| lacks the history fields | ends | `no-history-fields` | `expiry.stopped_at_root`; `verify-catalog` reports it |
| is a checkpoint | steps over it and goes on | | none |
| has a successor whose `created_at` is at or after `now - window` | ends | `window` | none |
| is the 1,001st candidate | ends | `limit` | none |
| otherwise | takes it as a candidate and goes on | | none |

The candidates taken before the walk ended stay candidates, whatever ended it.

A root is a checkpoint (H4) when any of these holds:

1.  its `seq` equals `since_seq`;
2.  it is version 1, or its `restore_epoch` differs from that of version `seq - 1`;
3.  its `created_at` is in a later UTC month than the `created_at` of version `seq - 1`.

The `created_at` and `restore_epoch` of version `seq - 1` come from retained root `seq - 1`, or
from its line in the newest `expiry` segment when `seq - 1` equals `expired_through`. The walk
itself is `sdmxlib.inventory.expiry_candidates` over the values the engine reads (N1).

For each root it walks, the engine keeps the SHA-256 of the bytes, the tag, the segment line
and the leaf hashes, and frees the bytes. A run of 1,000 candidates at 560 KB a root reads 560
MB one root at a time.

**Expiry step 2.** For every `seq` from the lowest candidate to the first retained root above
the highest, checkpoints included, `previous_root_sha256` must equal the SHA-256 of the bytes of
version `seq - 1`. For the lowest candidate c the engine finds that hash as S32 states. When
c - 1 equals `expired_through`, it walks the segment chain from `HISTORY.newest_segment`, skips
`restore` segments, stops at the first `expiry` segment and uses the `root_sha256` of that
segment's line for c - 1. Otherwise it hashes the bytes of retained root c - 1, which is a
checkpoint or root `since_seq - 1`. A mismatch stops the run with `stopped = "chain-mismatch"`,
nothing written, and `expiry.chain_mismatch` naming both versions.

**What stops a run.** The table gives every stop, the steps that still run and what the report
holds.

| Cause | Steps that still run | `stopped` | Event |
|---|---|---|---|
| A refusal before any step | none; the call raises | | none |
| Pending segments at the start | 0, 5, 6, 7, 8 | `None`; `recovered` is true | `expiry.finished` |
| `CURRENT` unusable after expiry step 0 | 8 | `no-current` | `expiry.stopped` |
| The walk ends on a missing, undecodable or field-less root | all, for the candidates before it | `None`; `walk_end` says why | `expiry.stopped_at_root` |
| A chain mismatch in expiry step 2 | 8 | `chain-mismatch` | `expiry.chain_mismatch`, `expiry.stopped` |
| A retained root cannot be read while planning | 8 | `unreadable-root` | `expiry.unreadable_root`, `expiry.stopped` |
| `HISTORY` or `catalog.json` changed since the plan | 8 | `history-changed` | `expiry.stopped` |
| `check()` raises `CatalogLockLostError` | none after it | `lock-lost` | `expiry.stopped` |
| A retained root cannot be read in a recovery run | 0, 5 and 8. Expiry step 6 deletes no leaf, and step 7 is skipped. | `unreadable-root` | `expiry.unreadable_root`, `expiry.stopped` |
| A delete raises a store fault | every other delete; 7 is skipped; 8 | `None`; `failed` holds the path | `expiry.delete_failed` |
| No candidate and no pending segment | 8 | `None` | `expiry.finished` |

| Option | Gain | Cost |
|---|---|---|
| **Plan without the lock, at most 1,000 candidates, recovery first (chosen).** | The lock covers writes, deletes, one listing and the roots committed during the plan. A first expiry of thousands of roots converges over several runs. | A rule change (S46). A run that finds another expiry's work stops and is run again. An interrupted expiry resumes choosing one call later. |
| The whole run under the lock. | The spec's wording; no re-check. | Every commit waits while the run reads each retained root, and a first run can pass the 120 s wait (review M5). |
| A ranged read of 1 KiB in expiry step 1. | Fewer bytes on a run that takes nothing. | A second read path for roots; every candidate is requested twice (O8). |

### E10: expiry step 3, the segments

**The plan.** The engine lists `inventory/` shallowly and reads every root that is neither a
candidate nor already read, to learn the leaves each names. A retained root that is missing or
does not decode stops the run before any write (S42, the table in E9).

With `state_dir` the engine reads and writes `<state_dir>/expiry-cache.json`:

```json
{"format": 1, "catalog_id": "…", "restore_epoch": 0,
 "roots": {"inventory/000000000412.json": {"etag": "…", "sha256": "…", "leaves": ["…"]}},
 "segments": {"history/0/…jsonl": {"kind": "expiry", "previous_segment": null}}}
```

The engine does not read again a root whose listed tag equals the cached one. The file holds
paths, tags and hashes only (release-job S23). The engine discards a file whose `format`,
`catalog_id` or `restore_epoch` differs from the catalog's, since a restore reuses root paths
and one directory can serve two catalogs. Expiry step 8 writes the file to a staged name and
renames it. A missing or unreadable file costs one full read. Without `state_dir` every run
reads every retained root.

The engine groups the candidates by `restore_epoch` in `seq` order, and splits each group at
`SEGMENT_MAX_LINES` lines or `SEGMENT_MAX_BYTES` of encoded bytes, whichever comes first. A
segment's header holds `kind: expiry`, the group's epoch, `first` and `last`, `roots`, the
`actor` and `expired_at` from the storage, and `leaves`: every leaf a root of this segment
names that no root outside the candidates names. The engine builds the headers under the lock,
after the second listing, so a leaf a newly committed root names is left out. The first header's
`previous_segment` and hash are `HISTORY`'s `newest_segment` and its hash, and each later
header names the segment before it. One run uses one UUIDv7: a segment's path is
`segment_path(epoch, first, last, run_uuid)` (S47).

**Orphans.** The engine lists `history/` and keeps the names `parse_segment_path` accepts (N1),
which drops a staged file a killed create left. It reads each segment's header with
`read_range`, doubling the length from 64 KiB until the bytes hold the first newline, and
decodes it with `decode_segment_header` (N1). It walks the chain from `newest_segment` by
`previous_segment`. With `state_dir` it reads only the headers the cache lacks. Every listed
segment the walk does not reach is an orphan. The engine reads whole the `expiry` orphans whose
`first` is the lowest candidate, and adopts them when all of these hold (spec §6.9 step 3):

- the first one's `previous_segment` and hash are `HISTORY`'s `newest_segment` and its hash;
- each later one names the one before it, with the SHA-256 of that one's bytes;
- the first line is the lowest candidate, and every line is a current candidate, in order;
- each line equals `segment_line(root, stored)` for the root's stored bytes, field for field;
- the header names this catalog and `kind: expiry`.

The engine emits `expiry.adopted_segment` for each and plans new segments only for the
candidates above the last adopted line. It emits `expiry.orphan_segment` for every other orphan
and leaves it.

A crashed run planned an adopted header's `leaves`. Expiry step 6 checks every listed leaf
against the retained roots again before it deletes, so a leaf a newer root now names survives.

**The writes.** Under the lock, and after `storage.check()` before each one, the engine creates
the segments in chain order with `encode_segment`. It keeps each one's SHA-256 for the next
header and for `HISTORY`. `create_exclusive` fsyncs a segment and its directory. When a run
creates `history/` or `history/<epoch>/`, the engine also fsyncs that directory's parent with
`_fsync_dir` before expiry step 4, so the new directory entry is on disk before `HISTORY` names
a segment in it.

| Option | Gain | Cost |
|---|---|---|
| **Headers by `read_range`, the chain cached with `state_dir` (chosen).** | The engine reports every orphan, as the spec asks, and a run with the cache reads only new headers. | A header decoder from sdmxlib (N1). Without the cache a run reads one header per segment ever written, about 365 after a year of nightly runs. |
| Read whole segments to walk the chain. | No ranged read and no new decoder. | Each run reads the catalog's whole history, up to 64 MiB a segment. |
| Detect only the orphans that start at the lowest candidate, by name. | No chain walk. | Only `verify-catalog` finds the others, and the spec's alert for an orphan segment has no source. |

### E11: expiry steps 4 to 7, and the state after a crash

**Expiry step 4.** `check()`, then `replace("inventory/HISTORY", ...)` with the tag read under
the lock. `expired_through` becomes the highest candidate, `newest_segment` and its hash the
last segment written or adopted, and `cleaned_through` stays. A run with no candidate skips
expiry steps 3 and 4.

**The pending segments.** The engine walks back from `newest_segment` to `cleaned_through` and
takes the `expiry` segments between them, oldest first. It skips a `restore` segment in the
range (S35). In a run that took candidates these are its own segments. In a recovery run they
are the ones a crashed run left: the engine reads each whole, and checks each one's SHA-256
against `HISTORY.newest_segment_sha256` or the next header's `previous_segment_sha256` before it
deletes anything a header lists.

**Expiry step 5.** For each `seq` in a pending header's `roots`, the engine compares the root's
tag with the one from its read. With the same tag it deletes the root, after `check()`, with
`if_match` set to that tag. With another tag, or with no earlier read, it reads the root and
deletes it when its bytes hash to the line's `root_sha256`. It skips an absent root. It keeps
any other root, which sits at or below `expired_through` with bytes no pending line records,
and emits `expiry.kept_root` for it.

**Expiry step 6.** The retained set is every root listed under the lock, less the roots expiry
step 5 deleted. It includes the roots below `since_seq`, the checkpoints, the kept roots and the
roots committed since the plan. In a recovery run the engine builds it here; when a retained
root cannot be read or decoded, the run deletes no leaf, emits `expiry.unreadable_root` and
leaves `cleaned_through` where it is (S42). For each leaf in the pending headers' `leaves` that
no retained root names, the engine calls `etag(leaf)`, then `check()`, then `delete(leaf,
if_match=<that tag>)`. That order is expiry step 6's final check. On Azure the expiry process
can pause long enough to lose the lease, and another writer can then take the lease and Put the
same leaf. If that Put comes before the delete, the leaf's tag differs and `delete` answers
`"changed"`, which the engine counts as kept. If it comes after, the Put recreates the leaf.

**Expiry step 7.** The engine fsyncs `inventory/` and `inventory/leaves/` with `_fsync_dir`.
When every delete of expiry steps 5 and 6 answered `"deleted"`, `"absent"` or `"changed"`, it
calls `check()` and replaces `HISTORY` with `cleaned_through` set to `newest_segment`. A delete
that raised a store fault is in `ExpiryReport.failed`, and `cleaned_through` stays, so the next
run, a recovery run, repeats expiry steps 5 and 6.

**The crash model.** A crash here is the death of the process at any instruction: a kill, an
exception, or the machine's loss of power. Every file the engine creates or replaces is staged,
fsynced and linked or renamed, and its directory is fsynced, as the Inventory writer does
today. On macOS `os.fsync` does not force the drive's cache, which is today's exposure for
roots as well.

| The process dies | On disk | A reader | The next run |
|---|---|---|---|
| in expiry step 0 | `CURRENT` at the old or the new head | unaffected | repeats expiry step 0 |
| in expiry steps 1 or 2, or in the plan | nothing written | unaffected | starts again |
| between two segment creates of expiry step 3, or before expiry step 4 | orphan segments; `HISTORY` unchanged; every root and leaf present | unaffected | adopts the linked orphans whose lines are still candidates and writes the rest; emits `expiry.orphan_segment` for any other |
| in expiry step 4 | the old `HISTORY` or the new one, since the replace is one rename | unaffected | as the row above, or as the row below |
| between two root deletes of expiry step 5 | `expired_through` advanced; some candidate roots still present at or below it | one with a readable `CURRENT` is unaffected; one on the fallback starts above `expired_through` | a recovery run: deletes each pending root whose bytes match its line, then runs expiry steps 6 and 7 |
| between two leaf deletes of expiry step 6 | some planned leaves deleted, none that a retained root names | unaffected | a recovery run: repeats expiry steps 5 and 6, then step 7 |
| between expiry steps 6 and 7, or in step 7 | everything deleted; `cleaned_through` behind | unaffected | a recovery run: finds nothing to delete and writes expiry step 7 |
| in a recovery run | one of the three rows above | as that row | another recovery run |
| in expiry step 8 | the cache file is the old one, or absent | unaffected | reads the roots the cache lacks |

Between expiry step 4 and the end of step 5, a writer with a base at or below `expired_through`
gets `StaleBaseError` (E4). A `CatalogLockLostError` from any `check()` ends the run at that
point with `stopped = "lock-lost"`, which leaves one of the rows above.

A leaf that a crashed commit wrote before its root, and a leaf only a restore's discarded roots
named, appears in no header. The engine deletes neither (spec §6.9, Crashes).

### E12: events, expiry step 8 and the report

The engine emits each event when the action happens, as a log record on the `statcatlib.expiry`
logger that carries one JSON object. Part 3 moves the `_emit` helper from `jobs.py:665` to a
new module `statcatlib/_events.py`, which `jobs.py`, `gc.py`, `release.py`, `cli/` and
`store/expiry.py` import, so that `store/` imports no job module. Release-job Part 6's `statcat
release` prints the events on standard output through `_events("statcatlib.expiry")`
(`cli/__init__.py:1042-1055`). `statcat prune` prints human lines from the report in place of
the events ("Command output and exit codes").

The deletion log is three events with the fields of GC's deletion log, at the top level:

| Event | Severity | Fields | When |
|---|---|---|---|
| `expiry.planned` | info | `path`, `reason` (`expired-root` or `unreferenced-leaf`), `segment` | Each root and leaf a run plans to delete, when the segment that lists it is written; in a dry run, when it is planned |
| `expiry.deleted` | info | the same | Each object deleted |
| `expiry.delete_failed` | error | the same, and `error` | A delete that raised a store fault |

Every other event has the fields of release-job D14: `event`, `severity`, `at`, `catalog` and a
`detail` object.

| Event | Severity | `detail` | When |
|---|---|---|---|
| `expiry.segment_written`, `expiry.adopted_segment` | info | `path`, `first`, `last`, `restore_epoch` | Expiry step 3 |
| `expiry.orphan_segment` | warning | `path`, `kind` | Expiry step 3 |
| `expiry.stopped_at_root` | warning | `seq`, `why` (a `walk_end` value) | Expiry step 1 |
| `expiry.chain_mismatch` | warning | `seq`, `expected`, `found` | Expiry step 2 |
| `expiry.unreadable_root` | warning | `path`, `error` | The plan, or expiry step 6 of a recovery run |
| `expiry.kept_root` | warning | `path`, `seq`, `segment` | Expiry step 5 |
| `expiry.stopped` | warning | `reason` (a `stopped` value) | Any stop of E9's table. On Azure the lease's 30-minute cap also raises it. |
| `expiry.finished` | info | the report's counts, `recovered`, `dry_run` | Expiry step 8 |
| `restore.planned` | info | `seq`, `unread` | Each root a restore will discard, before restore step 1 |
| `restore.discarded` | info | `path`, `seq` | Each root restore step 4 deletes |
| `restore.tombstoned` | info | `product`, `edition` | Each tombstone restore step 5 adds |
| `restore.finished` | info | `to`, `epoch`, `seq`, `incomplete`, `already` | After restore step 6 |

The durable record of what a run planned is each segment's header, which is in the catalog and
is never deleted.

**Expiry step 8** writes the cache file when `state_dir` is given, emits `expiry.finished` and
returns the report. It runs after every stop except a lost lock and a raised store fault.

```python
type WalkEnd = Literal["current", "keep-roots", "window", "limit", "missing-root",
                       "undecodable-root", "no-history-fields", "not-run"]
type StopReason = Literal["no-current", "chain-mismatch", "unreadable-root",
                          "history-changed", "lock-lost"]

@frozen
class ExpiryReport:
    dry_run: bool
    recovered: bool                   # the run finished pending segments and chose no candidate
    current_seq: int | None           # CURRENT after expiry step 0
    head_seq: int
    expired_through: int              # after the run; in a dry run, what it would become
    candidates: tuple[int, ...]       # seq values
    checkpoints: tuple[int, ...]      # seq values the walk stepped over
    walk_end: WalkEnd
    segments: tuple[str, ...]         # paths written; in a dry run, "<epoch>: <first>-<last>"
    adopted: tuple[str, ...]          # paths
    orphans: tuple[str, ...]          # paths left
    deleted_roots: tuple[str, ...]    # paths; in a dry run, the roots a real run would delete
    deleted_leaves: tuple[str, ...]
    kept_roots: tuple[str, ...]
    failed: dict[str, str]            # path to error
    stopped: StopReason | None
```

### E13: `prune` with expiry, and the time source

```python
CatalogAdmin.prune(
    *, older_than: timedelta, reason: str, dry_run: bool = True,
    inventory_window: timedelta | None = None, keep_roots: int = 10,
) -> PruneReport
```

```text
statcat prune PATH --older-than DAYS --reason TEXT [--expire-older-than DAYS] [--keep-roots N]
                   [--apply]
```

Without `inventory_window`, `prune` does what it does today and expires nothing, on every
catalog (O9). With it:

1.  **Checks, before the retire step.** `prune` raises `ValueError`, naming both values, when
    `inventory_window` is below `older_than` plus one day. That is spec §11.6's inequality, with
    `older_than` as the `data/` GC window (S37). It raises `ValueError` for `keep_roots` below 2.
    `RetentionPolicy.check` (`jobs.py:217-249`) holds the same two rules for the jobs
    configuration, with its own error class and key names; `prune` states them again for its
    own parameters. A 1.x catalog raises `ExpiryRefusedError`, which names `statcat admin
    opt-in`, and an unfinished restore raises it too.
2.  **Retire and delete.** `prune` retires old editions, deletes tombstoned edition directories
    and deletes unlisted `data/` files, as today (`admin.py:612-653`). The `retire` commit takes
    and releases the lock inside `commit` (`admin.py:693`).
3.  **Expire.** `prune` calls `expire(self._writer, keep_roots=keep_roots,
    window=inventory_window, dry_run=dry_run)`.

`PruneReport` gains `expiry: ExpiryReport | None = None` and `expiry_error: str | None = None`.
When `expire` raises an `InventoryWriteRefusedError`, such as a lock timeout, after the retire
step and the deletions are done, `prune` returns its report with `expiry_error` set to the
message, so the work already done is reported.

A dry run of `prune` plans expiry against the head as it is. A real run that writes a `retire`
version first has a head one higher, so it can take one root more than the dry run shows.

`prune` passes no `state_dir`, and `statcat prune` has no `--state-dir`. Without the cache a run
reads every retained root, outside the lock. The bench of the Verification plan measures that
read, and a later change adds the flag if the figure asks for it.

**The time source.** The engine reads `storage.now()` once per run and compares each
candidate's successor's `created_at` with `now - window`.

| Store | `now()` | Guard |
|---|---|---|
| Local disk | The machine's clock (`store/inventory.py:409-411`), P1's rule (O10) | The writer already refuses a base more than 5 minutes ahead of the clock, so each `created_at` follows the clock that wrote it. A clock set forward makes the engine take roots early. K roots, the checkpoints and every segment line stay, and the dry run shows the candidates first. |
| Azure (release-job Part 6) | The `Date` of an anonymous `HEAD` (`store/blob.py:562-586`) | `now()` there refuses a machine clock more than 30 s ahead of it (`MAX_CLOCK_SKEW`). |

| Option | Gain | Cost |
|---|---|---|
| **An explicit `inventory_window`, measured by `storage.now()` (chosen).** | An existing `prune` call deletes what it deleted before. One clock rule for both stores, the one every other local change uses. | Two windows on one call. A wrong laptop clock moves the cut. |
| `prune` on an opted-in catalog always expires. | One window. | An upgrade changes what an existing call deletes. |
| The head root's `created_at` as the clock. | A wrong laptop clock changes nothing. | A quiet catalog never expires, and the rule differs from the server's. |

### E14: restores

```python
def restore(
    writer: InventoryWriter, to: int, *, reason: str, dry_run: bool = False
) -> RestoreReport
CatalogAdmin.restore(to: int, *, reason: str, dry_run: bool = True) -> RestoreReport
```

```text
statcat admin restore URL --to SEQ --reason TEXT [--apply]
```

A restore discards every root above S and makes root S the base of a new epoch. It is the
remedy for damaged or lost roots on a catalog directory. `CatalogAdmin.rollback`
(`admin.py:361-411`) is a different operation and keeps its meaning on an opted-in catalog. It
changes one product's leaf in one new version of kind `rollback`, under the lock as any commit,
and discards nothing. It can make live any `superseded` entry of the head's leaf, and expiry
removes no entry from a leaf.

**Refusals.** `restore` raises `ValueError` for a blank reason, and `ExpiryRefusedError`,
before it writes anything, for each of these (S40):

- a `BlobInventoryStorage`, naming release-job Part 6; a 1.x catalog; a 2.x catalog with no
  valid `HISTORY`;
- `to` at or below `expired_through` (O1);
- `to` below `since_seq - 1`. Every discarded root then carries a change list, which restore
  step 5 needs;
- no root above `to`. A segment needs at least one root, and an epoch bump that discards
  nothing is a failover's case, which R9 hands to the server-restore design;
- root `to` missing or undecodable, or a leaf it names missing or with bytes that do not hash
  to its name;
- under Q1 (a), a `live` or `embargoed` entry of root `to` whose `manifest.json` is missing or
  differs from its `manifest_sha256`;
- an unfinished restore whose S differs from `to`. The message names S.

A real run on Windows or on a lock-refusing filesystem raises `CatalogLockUnsupportedError`
before it reads any root (E5).

**The plan, without the lock.** `restore` reads `catalog.json` and `HISTORY` with their tags,
lists the roots above S and reads each. A listed root that does not decode is `unread`. It
builds the report from the lines the roots give. A dry run returns here (E19).

**The restore steps**, under `exclusive()`, with `check()` before each create, replace and
delete. `restore` first reads the two documents again, raises `InventoryConflictError` when a
tag changed, lists the roots above S again and reads any the plan lacks.

1.  **The `restore` segments.** The roots above S, grouped by `restore_epoch` in `seq` order
    and split as E10 splits, all named with one UUIDv7 (S47). A header holds `kind: restore`,
    the group's epoch, `roots` (every listed root above S in the group), `leaves: []` and
    `unread`. `restore` puts an unread root in the group of the nearest readable root below it;
    when no readable root lies below it, the root goes in the current epoch's group. The first
    header links to `HISTORY.newest_segment`, and each later one to the one before.
2.  **`HISTORY`:** the epoch plus 1, `newest_segment` and its hash set to the last `restore`
    segment, and `expired_through` and `cleaned_through` unchanged. The spec's "smaller of its
    old value and S" is the old value, since S is above it.
3.  **`catalog.json`:** `restore_epoch` plus 1, through `replace`.
4.  **Delete** every root the segments list, from the highest `seq` down. `restore` keeps a
    root that decodes and carries the new epoch, which is the root S + 1 an earlier attempt
    wrote (S48). It emits `restore.discarded` for each delete.
5.  **Root S + 1,** through `_commit_locked(base=<root S>, leaves={}, retired=..., kind="restore",
    reason=reason)`, when no root of the new epoch exists at S + 1. The commit runs E4's four
    checks and passes them. The root carries the new epoch and a `previous_root_sha256` equal
    to the SHA-256 of root S's bytes. Its `retired` holds root S's tombstones. It also holds one
    tombstone, with `retired_at` set to the restore's time, for each edition that a leaf named
    in the segments' change lists records and that root S records in neither a leaf nor
    `retired` (`restore_retired`, N1; release-job S25; S48). The root's own change list holds
    those tombstones as `from: null, to: tombstone` items. `embargo` is root S's (O5).
6.  **`CURRENT`,** inside the same `_commit_locked`.

A restore deletes no leaf and never deletes or overwrites a segment.

**The tombstones' source.** Restore step 5 reads the segments it wrote in restore step 1, in
the first attempt and in a rerun. Each line keeps its root's `changes`, and each change item
names the product's new leaf by hash. Those leaves are under `inventory/leaves/`, which a
restore never deletes and which no expiry can touch while the restore is unfinished. A rerun
after a kill in restore step 4 therefore computes the same `retired` as an uninterrupted run.
An `unread` root has no line. An edition that only an unread root recorded gets no tombstone,
and the report is marked incomplete.

**A rerun.** `restore` calls `writer.unfinished_restore()` first.

- **It returns S.** `to` must equal S. The rerun reads the restore's segments, which carry the
  roster and the lines. It performs restore step 3 when `catalog.json` still has the old epoch,
  then restore steps 4 to 6. The root it writes records the rerun's `reason`.
- **It returns `None`, and root `to + 1` is the head, of kind `restore`, built on root `to`.**
  The restore is done. `restore` moves `CURRENT` to the head if it lags, writes nothing else,
  and returns a report with `already=True`, built from the segments.
- **It returns `None` otherwise.** The call is a new restore. If a seal committed on top of an
  earlier restore's root, the dry run lists that seal's version among the roots to discard.

A restore that stopped before restore step 2 left `HISTORY` unchanged and orphan `restore`
segments. The catalog works on the old epoch. A rerun writes new segments under a new UUID, and
`verify-catalog` prints the earlier ones as notes (S35).

| The process dies | On disk | A reader | A writer, expiry | `unfinished_restore()` | The rerun |
|---|---|---|---|---|---|
| in the plan | nothing written | unaffected | unaffected | `None` | starts again |
| between two creates of restore step 1, or before step 2 | orphan `restore` segments | unaffected | unaffected | `None` | a new restore with new segments |
| between restore steps 2 and 3 | `HISTORY` on the new epoch, `catalog.json` on the old | one with a readable `CURRENT` serves the old head; one on the fallback fails closed | refused: differing epochs (E4 check 2) | S | restore steps 3 to 6 |
| between restore steps 3 and 4 | both documents on the new epoch; every old root present | as the row above | refused through E4 check 3 and the retry's first branch | S | restore steps 4 to 6 |
| between two deletes of restore step 4 | the highest old roots deleted | every reader fails closed once the root `CURRENT` names is deleted, which is the first delete when `CURRENT` named the head | refused, through one branch of the retry or the other | S | deletes the rest, then restore steps 5 and 6 |
| between the root's create and `CURRENT`, in restore steps 5 and 6 | root S + 1 of the new epoch; `CURRENT` names a deleted root | the fallback finds root S + 1 | commits on root S + 1 | `None` | returns `already=True` and moves `CURRENT` |

A reader that holds the old head in memory keeps serving it until its next refresh, as after
any restore (spec §6.6).

```python
@frozen
class Transition:
    product_id: str
    edition_id: str
    from_: Marker | None
    to: Marker | None
    release_at: datetime | None       # the entry's, when `to` is a state

@frozen
class DiscardedVersion:
    seq: int
    created_at: datetime
    kind: str                         # event.kind
    actor: str
    reason: str
    embargo: bool                     # the root's embargo flag
    transitions: tuple[Transition, ...]

@frozen
class RestoreReport:
    to: int
    epoch: int                        # the new epoch
    seq: int | None                   # root S + 1's, None in a dry run
    embargo: bool                     # root S's embargo flag, which root S + 1 carries
    discarded: tuple[int, ...]        # every seq above S
    unread: tuple[int, ...]
    versions: tuple[DiscardedVersion, ...]   # one per readable discarded root
    tombstoned: tuple[str, ...]       # "<product>/<edition>"
    missing: tuple[str, ...]          # superseded or withdrawn entries of S whose directory is gone
    incomplete: bool                  # true when `unread` is not empty
    segments: tuple[str, ...]
    already: bool
    dry_run: bool
```

`versions` is what the operator repeats by hand: a cancellation, a rollback, a release or an
embargo change made after S. An `embargo-change` version has an empty change list, so the
report shows each version's `kind` and `embargo` beside its transitions. When `incomplete` is
true the report cannot list what the unread roots changed, and the command says so.

| Option | Gain | Cost |
|---|---|---|
| **One command that runs the six steps above `expired_through` and resumes (chosen).** | The order and the state after a kill are code, tested call by call. No `seq` at or below `expired_through` is reused, so E4's checks and expiry need no restore-specific exemption. | No restore to a monthly checkpoint (O1), and none that discards nothing. |
| A restore to any retained root. | A catalog can go back to a checkpoint. | The exemptions revision 1 needed, the failures the review found in them (B1, M1), and a head that names editions `prune` has deleted. |
| A runbook of manual steps, as the tests do today. | No code. | A missed step loses the discarded roots' record or leaves the catalog failing closed. |

### E15: `verify-catalog` and `doctor`

`verify_catalog` (`diagnostics.py:322-374`) keeps its checks of the head: each leaf, each
entry's manifest SHA-256, routing fields and artefact records, and `check_edition` on every
edition. On a catalog whose head carries the history fields it adds
`sdmxlib.inventory.check_history` (N2) over the listed roots and the listed segments.

| Spec §6.9 check | What it covers | Source |
|---|---|---|
| 1 | The root chain over retained roots and segment lines, keyed by `(restore_epoch, seq)`. A key present in both a root and a line has one `root_sha256`. | `check_history` |
| 2 | Every retained root's leaves: present, with bytes that hash to their names, and valid (S38) | `check_history` |
| 3 | Segment links, hashes and kinds | `check_history` |
| 3 | `HISTORY`: every head-chain root above `expired_through` exists; no `expiry` line on the head chain lies above it; `cleaned_through` is on the chain at or below `newest_segment` | `check_history` |
| 3 | Every root at or above `since_seq` carries the history fields | `check_history` |
| 3 | The checkpoint rule, computed again from roots and lines | `check_history` |
| 3 | Unlinked and overlapping segments on the chain | `check_history` |
| 3 | An orphan segment, as a note (O11, S36) | `check_history`, as its own finding kind |
| 4 | Each filled `artefacts`, `dataflows` and `pull_bytes` against its manifest | `verify_entry`, since 0.17 |

On a 1.x catalog check 1 runs over the roots that carry the history fields, and check 3 has
nothing to read. `verify_catalog` reports an unfinished restore as one finding that names the
rerun, from `unfinished_restore()`, and skips the history checks in that state. It lists
`history/` through `parse_segment_path`, so a staged file is no segment. `VerifyReport` gains
`notes`, and `statcat verify-catalog` (`cli/__init__.py:514-550`) prints each as an `ORPHAN`
line, which does not change the exit status, as a `STALE` line does not.

The conformance cases reach these checks through N3's `ExpiryBackend.verify()`, which the
adapter in `tests/test_inventory_conformance.py` maps to `verify_catalog`. The cases that come
from sdmxlib are the chain across a restore, the edited segment line, the deleted root and the
orphan note. statcatlib's own tests cover what those cases cannot build: an edition whose
manifest differs, and the report's files and exit status.

`doctor` (`diagnostics.py:577-627`) gains these items. Each reads without writing.

| Topic | Level | Line | Part |
|---|---|---|---|
| `catalog.json` | info | adds `format 2.0, history since version N` to today's line (`diagnostics.py:425-431`) | 2 |
| `LOCK` | info, or warning | absent; present and free; or held by another process now. `doctor` opens the file without `O_CREAT`, so it creates nothing, tries the lock once without waiting and releases it. Warning on Windows and on a filesystem that refuses the lock, where the catalog cannot opt in. | 2 |
| `HISTORY` | info, or warning | `expired_through`, `newest_segment`, `cleaned_through`. Warning when a 2.x catalog has none or it does not decode. | 2 |
| pending | warning | A pending `expiry` segment exists: "an expiry stopped before it finished; `statcat prune --expire-older-than` finishes it". A trailing `restore` segment is not pending. | 5 |
| checkpoint | info | the newest checkpoint's `seq` and `created_at`. `doctor` reads roots down from the head, at most one month of them. | 5 |
| segments | warning | each orphan segment, and each segment on the chain whose `previous_segment` names no file. `doctor` reads headers only, so it checks links and leaves the hashes to `verify-catalog`. | 5 |
| restore | warning | an unfinished restore, with S and the command that resumes it | 5 |
| leftover file | warning | `_leftovers` (`diagnostics.py:567-574`) also scans `history/` for staged files | 5 |

`LOCK` matches neither of `_leftovers`' patterns.

### E16: GC, the release job and the split with release-job Part 6

**`statcat gc`.** GC from 0.17 reads an opted-in catalog: it takes `expired_through` as its
floor and stops the run on a missing root above it (`gc.py:303-319`, `:321-335`, `:352-377`).
This design changes nothing in `gc.py`. When expiry and GC read the same jobs configuration,
the engine takes no root GC needs:

- the engine takes root N only when root N + 1 was created before `now - inventory_window`;
- `RetentionPolicy.check` requires `inventory_window >= gc_window + 1d` (`jobs.py:235`);
- root N + 1 is therefore older than GC's cutoff, `now - gc_window`, and GC's walk ends at or
  above it, with a day between the two jobs' clock reads.

GC keeps every tombstone retired inside its window (`gc.py:379-396`), which is how the
tombstones of a restore's root S + 1 keep a discarded edition's files.

GC runs over object storage only (`gc.py:75`), and no restore exists there, so an unfinished
restore cannot meet GC through this design. The stop revision 1 added to `gc.py` moves to R9.

**The release job.** `ReleaseJob.run` refuses a 2.x catalog at its start
(`release.py:754-760`). This design leaves that refusal in place, and a blob writer still
refuses a 2.x catalog through `stage_2` (E2). Opt-in over a `BlobLocation` raises
`ExpiryRefusedError` (E6), so no Azure catalog is opted in before release-job Part 6. That part
removes the refusal. The release job then takes the lease in release-job D1 step 1, passes it
to `BlobInventoryStorage(lease=)`, and calls `expire` in release-job D1 step 7.

**The split.**

| Piece | This design | Release-job Part 6 |
|---|---|---|
| The lock | `flock` on `LOCK`; the protocol members | `AzureHistoryLease` behind them; the lease on every blob commit; the 30-minute cap |
| Opt-in | `opt_in`, local | the same function over `BlobExpiryStorage` |
| The engine | `expire`, `ExpiryStorage`, the cache file's format, the events | `BlobExpiryStorage`; the REST calls for the lease, the `If-Match` delete and the Put with a lease ID |
| Gates | none on local disk | `not_before`, the pod check |
| The cache | `state_dir=` on `expire` | `--state-dir` on a persistent volume (release-job S23) |
| Restore | `restore`, local | unbuilt; R9 |
| `verify-catalog`, `doctor` | local | Q2, R11 |

**Changes release-job Part 6's text needs.** The project owner decides each when the
release-job design is re-reviewed (release-job handoff row 11).

| # | Release-job text | Change |
|---|---|---|
| R1 | D13's `AzureBlobRest.get_range`, "which `obstore` cannot send (Phase 4 F14)" | Drop the call. `obstore` 0.12.1, the version this checkout resolves, exports `get_range`, and F14 lists the lease, the `If-Match` delete and the lease-ID Put only. The engine reads segment headers with it and reads roots whole (O8). |
| R2 | D13's `CatalogAdmin.opt_in(reason)` | `opt_in(*, dry_run=True)`: opt-in writes no version, so no document records a reason (E6). |
| R3 | D13's engine interface: `read`, `write_history`, a boolean `delete` | `ExpiryStorage` as E8 defines it: `read_tagged`, `replace`, `etag`, `list(shallow=)`, and `delete` with three outcomes. |
| R4 | `LeaseLostError`; the 120 s wait | `LeaseLostError` subclasses `CatalogLockLostError`, and the wait raises `CatalogLockTimeoutError`, with the constant `LOCK_TIMEOUT` both stores share. |
| R5 | Part 6's `release.py` entry: "the leaf-set cache" | The cache is the engine's, in `store/expiry.py`. Release-job Part 6 passes `state_dir`. |
| R6 | D13's lease on writes | `BlobInventoryStorage.stage_2` is true over `AzureStore`, and `exclusive()` yields at once on a catalog with no `HISTORY`. |
| R7 | D14's expiry events | Add E12's events, the `restore.*` ones included, with their fields. |
| R8 | D1's step 7 and `--dry-run` | A dry run with `--expire` takes no lease and calls `expire(dry_run=True)` (E19). The engine plans before the lock (S46), so a release run holds the lease for expiry's writes only. |
| R9 | No part plans a restore on object storage. | Plan one, in release-job Part 6 or a design of its own, over `BlobExpiryStorage` and the code of E14. It settles four things this design leaves to it: whether root S + 1 guards against an automatic release of an entry whose cancellation the restore discarded (revision 1's Q6, with release-job Q1); an epoch bump that discards no root, for a failover; a stop in `gc.py` on an unfinished restore; and whether a target at or below `expired_through` stays refused. |
| R10 | | Withdrawn. sdmxlib-rest reports `sdmxlib_version` from 0.10.0 on. |
| R11 | No command checks a server catalog's history. | Record Q2's answer. Under Q2 (a), a later issue adds `az://` to `verify-catalog` over `check_history`. |
| R12 | D13 and Part 6 give the release identity's storage rights no list. | State them. Expiry adds one data action to what the identity uses today: it already reads, writes and lists blobs in the catalog's container, and expiry also deletes blobs, under `inventory/` only. Whether a lease request needs a further action is unverified. A role on a container covers every path in it, so a delete right limited to `inventory/` needs the ACL-only setup spec §10.1 describes. |

### E17: pure rules in sdmxlib, I/O in the engine

Under O3 the engine calls sdmxlib for every decision that reads no storage. It holds the reads,
the writes, their order, `check()` and the events.

| Rule | sdmxlib (N1, N2) | statcatlib |
|---|---|---|
| Whether root N is a checkpoint | `is_checkpoint` | reads the values |
| Which roots expiry step 1 takes, and why the walk ended | `expiry_candidates` | reads the roots, emits the event |
| How segments split | the two constants | groups, encodes, writes |
| A segment's header and name | `decode_segment_header`, `parse_segment_path` | reads by range, lists |
| The restore's tombstones | `restore_retired` | reads the leaves the lines name |
| The history checks | `check_history` | lists roots and segments, maps findings to files |
| The chain link of expiry step 2, orphan adoption, the pending walk, the unfinished restore | none | `store/expiry.py` and `InventoryWriter`, since each mixes reads with the decision |

| Option | Gain | Cost |
|---|---|---|
| **Rules in sdmxlib 0.89 (chosen, O3).** | One checkpoint rule for the writer, the verifier and a later exporter. sdmxlib's reference writer can run N3's cases. | Parts 3 to 5 wait for a release. `check_history` and the engine share `is_checkpoint`, so `verify-catalog` cannot catch a fault in it; N3's cases state the expected checkpoints by hand. |
| Rules in statcatlib. | No release to wait for. | Rules the suite design gives to sdmxlib live in the writer's repository, and a later move breaks imports. |

### E18: reading history after expiry

Goal 6 is a property of the documents, and no statcatlib code answers "what was live at time
T" in stage 2. Part 3's test computes the answer twice, with a helper `live_at(fetch, at)` in
`tests/_history.py`:

1.  Collect the `seq`, `restore_epoch` and `created_at` of every version on the head chain,
    from the retained roots and from the lines of the `expiry` segments reached from
    `HISTORY.newest_segment`. A `restore` segment's lines are discarded roots and are left out.
2.  Take v, the newest of them with `created_at` at or before T. No such version means the
    catalog had none at T.
3.  If root v is retained, read its leaves. The answer is each product's `live` entries.
4.  Otherwise take c, the newest retained root at or below v, which is a checkpoint or a root
    below `since_seq`. Start from the `live` entries of root c's leaves. Apply the change lists
    of versions c + 1 to v in `seq` order, each from its root or its line: an entry item whose
    `to` is `live` adds its edition, and one whose `from` is `live` and whose `to` is anything
    else removes it.

The test builds a catalog across three UTC months, records the answer of step 3 from the full
roots for a T in each month, expires, and compares with the answer of step 4. A public reader
with this replay belongs to stage 3.

### E19: dry runs and the lock

No dry run takes the lock, creates `LOCK` or writes a file (S43). Each is the plan of its
operation, which already runs without the lock.

| Operation | Real run takes the lock | Real run creates `LOCK` | Dry run takes the lock | Dry run on Windows or a lock-refusing filesystem |
|---|---|---|---|---|
| A commit | yes | yes, at the first commit | no dry run | |
| `opt_in` | for opt-in steps 5 to 7 | yes | no | reports; the real run raises `CatalogLockUnsupportedError` |
| `expire` | for expiry steps 3 to 7 | yes | no | reports; the real run raises `CatalogLockUnsupportedError` |
| `prune` with `inventory_window` | in its `retire` commit, then in `expire` | yes | no | as `expire` |
| `restore` | for restore steps 1 to 6 | yes | no | reports; the real run raises `CatalogLockUnsupportedError` |
| `doctor`'s `LOCK` probe | once, without waiting | no | | reports the refusal as a warning |

A dry run reads a catalog that other processes change, so its report is a forecast. An
expiry dry run performs no expiry step 0: it plans against the `CURRENT` it finds and reports
how far `CURRENT` lags. On a catalog with pending segments it reports the recovery a real run
would do. During an unfinished restore, a dry run of `expire` raises `ExpiryRefusedError` as
the real run does.

### Command output and exit codes

`prune`, `opt-in`, `restore`, `verify-catalog` and `doctor` print human lines on standard
output with `_say`, as `prune`, `verify-catalog` and `doctor` do today. Only a job command
(`statcat gc`, `statcat release`) puts events on standard output. Every refusal is an
`InventoryError` or a `ValueError`, both in `_ADMIN_ERRORS` (`cli/__init__.py:839-849`), so an
`az://` URL given to `opt-in` or `restore` prints `ERROR <message>` and exits 1.

**`statcat admin opt-in URL [--apply]`**

| Outcome | Lines | Exit |
|---|---|---|
| Dry run | `since_seq <N>`; `head <H>`; `<K> version(s) below <N> never expire`; the three statements of E6, one line each; `This dry run changed nothing. Run again with --apply.` | 0 |
| Applied | the same first four groups; `Opted in: catalog.json is format 2.0, history since version <N>.` | 0 |
| Already opted in | `Already opted in: history since version <N>.` | 0 |
| A refusal of E6, a lock error | `ERROR <message>` | 1 |

**`statcat prune PATH --older-than DAYS --reason TEXT [--expire-older-than DAYS] [--keep-roots N]
[--apply]`**

`--expire-older-than` is an integer of days, at least 2. `--keep-roots` is an integer, at least
2, default 10; without `--expire-older-than` it is a usage error. The retire and delete lines
are today's (`cli/__init__.py:617-641`). The expiry lines follow them:

| Line | When |
|---|---|
| `recovered: finished an expiry that stopped earlier` | a recovery run |
| `segment <path>` / `adopt <path>` / `orphan <path>` | each segment written, adopted or left |
| `expire <path>` | each root and leaf deleted, or planned in a dry run |
| `keep <path>: matches no line of <segment>` | each kept root |
| `FAIL <path>: <error>` | each failed delete |
| `STOPPED <stopped>: <what it means>` | a stop of E9's table |
| `STOPPED at version <seq>: <walk_end>` | the walk ended on a bad root |
| `FAIL expiry: <message>` | `PruneReport.expiry_error` |
| a summary sentence with `expired_through` and the counts | always |

| Outcome | Exit |
|---|---|
| Everything done, or nothing to do; a dry run | 0 |
| A check of E13 step 1, before anything is written | 1, `Cannot prune the catalog: <message>` as today |
| A failed delete; `stopped` set; `walk_end` one of `missing-root`, `undecodable-root`, `no-history-fields`; `expiry_error` set | 1 |
| A usage error | 2 |

**`statcat admin restore URL --to SEQ --reason TEXT [--apply]`**

A blank `--reason` is a usage error, exit 2, through `_reason()` (`cli/__init__.py:890-894`),
as for the other `admin` commands.

| Line | When |
|---|---|
| `discard version <seq> <created_at> <kind> by <actor>: <reason>` | each readable root above S |
| `  embargo <on or off>` | under a version whose `embargo` differs from the version below it |
| `  <product>/<edition> <from> -> <to>` and, for an embargoed entry, ` release_at <time>` | each transition of that version |
| `unread version <seq>` | each unread root |
| `tombstone <product>/<edition>` | each tombstone of root S + 1 |
| `missing <product>/<edition> (<state>)` | each `superseded` or `withdrawn` entry of S whose directory is gone |
| `embargo at version <S>: <on or off>` | always |
| `INCOMPLETE <n> version(s) could not be read; their changes are unknown` | `incomplete` |
| `This dry run changed nothing. Run again with --apply.` / `Restored to version <S>: version <S+1> written, epoch <e>.` / `Already restored to version <S>.` | the summary |

| Outcome | Exit |
|---|---|
| A dry run; applied; already restored | 0 |
| A refusal of E14, a lock error | 1, `ERROR <message>` |
| A usage error | 2 |

**`statcat verify-catalog`** adds `ORPHAN <path>` lines and counts them in its summary. Its exit
codes stay 0, 1 and 2. **`statcat doctor`** adds E15's items in its two existing line forms.

### Errors

| Site | Condition | Raises or reports |
|---|---|---|
| `exclusive()`, local | Another holder keeps the lock for the whole wait | `CatalogLockTimeoutError` |
| `exclusive()`, local | Windows, or the filesystem refuses the lock | `CatalogLockUnsupportedError`. A 1.x commit then proceeds unlocked (E5). |
| `exclusive()` | The calling thread holds this lock | `RuntimeError` |
| `check()` | Called outside `exclusive()` | `RuntimeError` |
| `_commit_locked`, `check_writable`, 2.x catalog | No `HISTORY`, or it does not decode | `InventoryWriteRefusedError` |
| `_commit_locked`, `check_writable`, 2.x catalog | The epochs of `catalog.json` and `HISTORY` differ | `UnfinishedRestoreError` |
| `_commit_locked`, 2.x catalog | The base's epoch differs from `HISTORY`'s, and the commit is no restore's | `StaleBaseError`; `commit` retries, or raises `UnfinishedRestoreError` |
| `_commit_locked`, 2.x catalog | `base.seq <= expired_through` | `StaleBaseError`; `commit` retries |
| `commit`, 2.x catalog | The head cannot be read during an unfinished restore | `UnfinishedRestoreError` |
| `unfinished_restore` | The epochs differ and the newest segment is no `restore` segment | `InventoryError` |
| `check_writable`, blob storage | A 2.x catalog, until release-job Part 6 | `InventoryWriteRefusedError`, from the stage-1 decode |
| `opt_in`, `restore` | A blob storage | `ExpiryRefusedError`, naming release-job Part 6 |
| `opt_in` | A 2.x catalog without `HISTORY` | `InventoryError` |
| `opt_in` | A missing or undecodable root, a head without the history fields, a root with the fields below one without, a broken chain; a `HISTORY` with other values | `ExpiryRefusedError`, naming the root or the file |
| `opt_in`, `restore` | `catalog.json` or `HISTORY` changed between the plan and the lock | `InventoryConflictError` |
| `replace` | A path other than `inventory/HISTORY` and `catalog.json` | `ValueError` |
| `expire` | A storage other than an `ExpiryStorage` whose `stage_2` is true | `TypeError` |
| `expire` | `keep_roots` below 2; a zero or negative `window` | `ValueError` |
| `expire`, `restore`, `prune(inventory_window=)` | A 1.x catalog; a 2.x catalog with no valid `HISTORY` | `ExpiryRefusedError` |
| `expire`, `prune(inventory_window=)` | An unfinished restore | `ExpiryRefusedError`, naming S |
| `prune` | `inventory_window < older_than + 1d`; `keep_roots < 2` | `ValueError` |
| `prune` | `expire` raises `InventoryWriteRefusedError` after the retire step | `PruneReport.expiry_error` |
| `expire` | Each stop of E9's table | `ExpiryReport.stopped` or `walk_end`, and the event |
| `expire`, expiry step 5 | A root at or below `expired_through` whose bytes no pending line records | kept; `expiry.kept_root` |
| `expire`, expiry steps 5 and 6 | A delete raises `OSError` or `BlobStoreError` | `expiry.delete_failed`; `ExpiryReport.failed`; `cleaned_through` unchanged |
| `restore` | A blank reason | `ValueError` |
| `restore` | Each refusal of E14 | `ExpiryRefusedError`, nothing written |

`CatalogLockTimeoutError`, `CatalogLockUnsupportedError`, `CatalogLockLostError`,
`UnfinishedRestoreError`, `ExpiryRefusedError` and `StaleBaseError` are new. The first five
subclass `InventoryWriteRefusedError`, and `StaleBaseError` subclasses `InventoryConflictError`.

### Symbols

"Public" means exported from `statcatlib` and listed in `tests/test_public_api.py`.

| Symbol | Module | Signature | Part | Public |
|---|---|---|---|---|
| `LOCK_NAME`, `LOCK_TIMEOUT`, `LOCK_POLL`, `STAGE_2_SINCE` | `store/lock.py` (Create) | `"LOCK"`, 120 s, 50 ms, the version of the first release with Part 1 | 1 | no |
| `hold`, `probe` | `store/lock.py` | `hold(path: Path, *, timeout: timedelta) -> AbstractContextManager[None]`; `probe(path: Path) -> Literal["absent", "free", "held", "unsupported"]`, which creates nothing | 1 | no |
| `InventoryStorage.exclusive`, `.check`, `.stage_2` | `store/inventory.py` | E2 | 1 | the protocol is |
| `ExpiryStorage`, `Listed`, `Deleted` | `store/inventory.py` | E8 | 1 | no |
| `LocalInventoryStorage` | `store/inventory.py` | `(root, *, clock=None, lock_timeout=LOCK_TIMEOUT)`; gains `list`, `read_tagged`, `read_range`, `etag`, `delete`, `replace` | 1 | yes |
| `InventoryWriter._commit_locked` | `store/inventory.py` | `try_commit`'s arguments | 1 | no |
| `InventoryWriter.history` | `store/inventory.py` | `() -> History \| None`: `HISTORY` decoded; `None` on a 1.x catalog; `InventoryError` when it is missing or undecodable on a 2.x one | 1 | yes |
| `InventoryWriter.unfinished_restore` | `store/inventory.py` | `() -> int \| None` (E4) | 1 | yes |
| `CatalogLockTimeoutError`, `CatalogLockUnsupportedError`, `CatalogLockLostError`, `UnfinishedRestoreError`, `StaleBaseError` | `store/inventory.py` | E2, E4 | 1 | yes |
| `ExpiryRefusedError` | `store/inventory.py` | a subclass of `InventoryWriteRefusedError` | 2 | yes |
| `opt_in`, `OptIn` | `store/expiry.py` (Create) | E6 | 2 | `OptIn` from Part 5 |
| `expire`, `ExpiryReport`, `WalkEnd`, `StopReason` | `store/expiry.py` | E9, E12 | 3 | `ExpiryReport` |
| `_emit` | `_events.py` (Create) | moved from `jobs.py:665` | 3 | no |
| `CatalogAdmin.prune` | `admin.py` | gains `inventory_window`, `keep_roots` | 3 | yes |
| `PruneReport.expiry`, `.expiry_error` | `admin.py` | E13 | 3 | yes |
| `restore`, `RestoreReport`, `DiscardedVersion`, `Transition` | `store/expiry.py` | E14 | 4 | the three classes |
| `CatalogAdmin.restore` | `admin.py` | E14 | 4 | yes |
| `verify_catalog`, `VerifyReport.notes` | `diagnostics.py` | E15 | 5 | as today |
| `doctor` | `diagnostics.py` | E15's items | 2 and 5 | as today |
| `CatalogAdmin.opt_in` | `admin.py` | E6 | 5 | yes |

No part adds `Any` to `src/`; `tools/count_any.py` stays at its baseline.

## Alternatives considered

Three wider choices, beside the option tables above:

*   **Run local expiry in a separate command, `statcat expire`.** One command per job. The suite
    design (H5) and GH-125 place expiry in `prune`, and GH-125's acceptance tests one call that
    retires and expires. `prune` with `inventory_window` is that call.
*   **Put the whole engine in sdmxlib.** sdmxlib would then write and delete catalog objects.
    It holds no blob client and writes no Inventory object today (spec §6, Ownership), and the
    engine needs both stores.
*   **Take no lock on 1.x catalogs.** No `LOCK` file appears until opt-in, and Part 1 breaks
    less. A stage-2 writer's commit could then overlap an opt-in on every filesystem, where O6
    accepts that race only where the lock is refused.

The table sets each command, flag and method against the item of GH-125 that needs it.

| Surface | GH-125 item |
|---|---|
| `statcat admin opt-in`, `CatalogAdmin.opt_in` | part 2: opt-in through `CatalogAdmin` |
| `statcat prune --expire-older-than`, `--keep-roots` | part 3: expiry in `prune`, with W and K |
| `statcat admin restore`, `CatalogAdmin.restore` | part 3: restores in the spec's order |
| `ExpiryStorage`, `expire`, internal | release-job Part 6 |

## Non-functional requirements

No time below is measured. Each is an estimate until the bench of the Verification plan
replaces it. Sizes come from the suite design's Sizes table and the GH-126 corpus: 2,979
versions of 2,521 products at 560 KB a root, 1.7 GB in all. At 7,000 products a root is 1.6 MB.

Counts for one run that takes n candidates (at most 1,000) and deletes m leaves, on a catalog
with r retained roots, s segments, and v versions committed while the run plans:

| Phase | Lock | Reads | Writes and deletes |
|---|---|---|---|
| Before any step | no | `catalog.json`, `HISTORY`; the newest segment's header | none |
| Expiry step 0 | no | `CURRENT`, the head, one probe | `CURRENT` when it lags |
| Expiry steps 1 and 2 | no | the n candidates, each checkpoint stepped over and one root more, whole; the newest `expiry` segment, whole | none |
| The plan of expiry step 3 | no | every retained root the cache lacks (all r without `state_dir`); one header range per segment the cache lacks (all s without it); the orphans that start at the lowest candidate | none |
| Under the lock, before expiry step 3 | yes | `catalog.json`, `HISTORY`; one shallow listing of `inventory/`; v roots, whole | none |
| Expiry steps 3 and 4 | yes | none | one segment per epoch, more when a size limit splits; `HISTORY` |
| Expiry step 5 | yes | n tag reads | n deletes |
| Expiry step 6 | yes | m tag reads | m deletes |
| Expiry step 7 | yes | none | two directory fsyncs; `HISTORY` |

*   **The lock's bound (O7).** Under the lock a run reads two documents under 1 KB each, one
    directory listing and the v roots committed during its plan. It writes at most a few
    segments and `HISTORY` twice, and deletes at most 1,000 roots and the m leaves their
    headers plan, each after one `stat`. It reads no retained root and no candidate there. m is
    bounded by the leaves the 1,000 candidates changed. A commit holds the lock for one read of
    `HISTORY`, its changed leaves, one root and `CURRENT`. Opt-in holds it for two document
    reads, the roots committed during its verification, one create and one replace. A restore
    holds it for the same re-read, its segment creates, two replaces, one delete per discarded
    root and one commit.
*   **Outside the lock.** Without `state_dir` each run reads every retained root: 1.7 GB on a
    catalog that opted in at version 2,979 of the GH-126 corpus. With it a run reads the roots
    written since the last run. Opt-in's verification reads every root once, the same 1.7 GB,
    and its dry run reads them as well.
*   **Memory.** The engine holds segment lines and leaf hashes and frees each root's bytes once
    it has hashed them. One run's lines are at most 64 MiB per segment, and the retained leaf
    sets take 64 bytes a hash for each distinct leaf.
*   **Disk.** `history/` grows by each expired root's line: 920 bytes for one released product,
    185 KB for a release of 200, 6.5 MB for a republish of 7,000. No code deletes under it.
    `LOCK` is empty. `expiry-cache.json` holds about 80 bytes a leaf hash per retained root.
*   **Durability.** E11 states the crash model and what the engine fsyncs.
*   **Idempotency.** A second `prune` with the same arguments expires nothing more and writes
    nothing. A second `opt_in` returns `already=True`. A second `restore` with the same `to`,
    while root S + 1 is the head, returns `already=True`.
*   **Security.** A segment line names embargoed editions and their `release_at`, as a root
    does. On local disk file permissions are the only boundary, as for `inventory/`
    (`docs/catalog-format.md` §1.1). A local `event.actor` and a segment header's `actor` are
    `<os user>@<hostname>` and stay in `history/` permanently.
*   **Compatibility.** No document format changes. E7's table states what each older version
    does with an opted-in catalog.
*   **Coverage.** `just coverage` runs in each part's PR; the gate does not include it.

| Module | Unit and integration tiers cover | Left to release-job Part 6 |
|---|---|---|
| `store/lock.py` | POSIX: threads, a second process, a killed holder, the timeout with a short `lock_timeout`, a refusal injected through `fcntl.flock`, the probe | none; the Windows branch is three lines with no gate |
| `store/inventory.py` | the lock span, E4's four checks and both retry branches, `unfinished_restore`, the decoder choice, the six local storage methods | the Azure lease |
| `store/expiry.py` | every expiry step, every stop of E9's table, a kill after every storage change, the dry run, over `LocalInventoryStorage` and over the in-memory tagged storage of `tests/_fakes.py` | `BlobExpiryStorage` on Azurite |
| `admin.py`, `cli/` | `opt_in`, `prune` with expiry, `restore`, each command's lines and exit codes | the same over `az://` |
| `diagnostics.py` | every finding and note, on catalogs the engine built and a test then damaged | Q2 |

### Not confirmed

The author read each item below in documentation or code and did not run it. The last column
says what settles it.

| Item | Settled by |
|---|---|
| `flock` is released when the holding process is killed, on macOS and Linux. The suite design lists it under "Verify before citing". | Part 1's killed-holder test, on the platform the gate runs on |
| `flock` on a descriptor opened `O_RDONLY` excludes as one opened read-write does, and excludes two threads of one process that each open the file, on macOS as on Linux. | Part 1's thread and second-user tests |
| The `errno` values `flock` gives on a filesystem without advisory locks: `ENOLCK`, `ENOTSUP`, `EOPNOTSUPP` and `ENOSYS` are assumed. Whether Linux maps `flock` on NFS to a lock other clients see. | No test. The risk row "A filesystem refuses the lock with another error" covers it: any other `OSError` propagates and fails the commit. |
| sdmxlib accepts a change item whose `leaf_sha256` is `{"from": null, "to": null}`. The review read that it does. | N4, and Part 4's tombstone test |
| The names and signatures of N1 to N3 | sdmxlib's design; the re-read step before Part 3 |
| Every time, and the 80 bytes a leaf hash of the cache file | the bench of the Verification plan |
| Everything on Azure: that a lease request on a missing blob answers 404, the renewal rules, the `If-Match` delete, `obstore.get_range` under a bearer token, which data action a lease request needs, and which role or ACL the release identity holds today. The author sent no request to an Azure host. | release-job Part 6 and its Not confirmed list |

### Risks and limits

| Risk | Mitigation |
|---|---|
| A catalog on Windows cannot opt in | O2. The refusal names the platform, `doctor` warns, and `docs/catalog-format.md` §1.3 states it. A 1.x catalog there works as before. |
| A network or synced filesystem accepts `flock` and excludes no other host | Unsupported by spec §6.6. `doctor` warns for a synced folder, and the opt-in section of `docs/api/admin.md` repeats it. |
| A filesystem refuses the lock with another error than the four listed | The `OSError` propagates and fails the commit, which is the safe direction. The fix is one more value in `store/lock.py`. |
| A laptop clock set forward makes the engine take roots early | K roots, the checkpoints and every segment line stay, and the dry run shows the candidates first (O10). |
| A statcatlib older than 0.14 wrote a root without the history fields above `since_seq` | The walk ends at that root in every run, and `verify-catalog` reports it. Only a 0.13 commit in flight at the opt-in can write one. |
| A commit waits more than 120 s | The lock covers no bulk read (O7), and a run deletes at most 1,000 roots. The seal is refused with its journal entry kept. |
| A run stops on `history-changed` | Two processes expired at once. The second run changes nothing, and its next call plans again. |
| An orphan segment stays permanently | It is a note (O11), and the engine adopts the common case, a kill between expiry steps 3 and 4. |
| A restore discards a cancellation, and the edition is released later | On local disk no release job runs. The report lists each discarded version, and R9 carries the question to the server restore (O5). |
| A restore's report is incomplete | The command prints `INCOMPLETE` and the count, and CUJ 4 shows the manual check. |
| A leaf only a discarded or crashed root named is never collected | Bounded by the changes since S, and by one version per crash (spec §6.9). A later design collects them. |
| `check_history` and the engine share one checkpoint rule | N3's cases state the expected checkpoints by hand (E17). |
| Opt-in cannot be undone | A dry run by default, three statements printed before `--apply`, and no opt-in command before Part 5 (O4). |
| The retained set is wrong and a named leaf is deleted | The engine stops on an unreadable root (S42), lists again under the lock, discards a cache file of another catalog or epoch, and Part 3's reachability test compares every deletion with an independent computation. |
| sdmxlib's final signatures differ from N1 to N3 | The re-read step before Part 3, in the land order. |
| The build starts long after this revision | The header names every commit read, and the land order's first row is a re-read of them. |

## Detailed implementation plan

### Land order and releases

The build is deferred until Phase 6 has started (the header). The rows are in order, and each
"Waits on" names the rows before it that must be done.

| Row | What | Repository | Waits on |
|---|---|---|---|
| A | Re-read this design against the heads of statcatlib, sdmxlib, sdmxlib-rest, the hub and statcat-serve, and revise it where they moved. Record the answers to Q1 and Q2. | statcatlib | the project owner scheduling the build |
| B | Suite design revision 6, with the fourteen hub rows, and its review | hub | A |
| C | The spec PR with the accepted S rows (N5) | sdmxlib | B |
| D | Part 1 | statcatlib | C, for S33, S39, S44 and S45 |
| E | Part 2 | statcatlib | D; C, for S41 and S43 |
| F | sdmxlib's design for N1 to N4, then 0.89.0 (N7) | sdmxlib | C. It can run beside D and E. |
| G | Read sdmxlib's design and revise E9, E10, E14 and E15 to its names | statcatlib | F |
| H | Part 3 | statcatlib | E, G; C, for S32, S35, S37, S42, S46 and S47 |
| I | Part 4 | statcatlib | H; Q1; C, for S40 and S48 |
| J | Part 5 | statcatlib | I; Q2; C, for S36 and S38 |
| K | Release-job Part 6, after its re-review against this design (R1 to R12) | statcatlib | J; release-job handoff rows 11 and 12 |

The parts land in the order 1, 2, 3, 4, 5. Part 5's conformance run and its `verify-catalog`
test across a restore need Part 4's restore, and Part 4 needs nothing of Part 5.

| Part | Content | sdmxlib |
|---|---|---|
| 1 | The lock, the protocol members, the local storage methods, `_commit_locked`, E4's checks, `unfinished_restore`, the stage-2 decode | 0.88 |
| 2 | `opt_in`, internal; `doctor`'s `catalog.json`, `LOCK` and `HISTORY` items | 0.88 |
| 3 | The engine, `prune` with expiry, the events | 0.89 |
| 4 | Restores | 0.89 |
| 5 | `verify-catalog`'s history checks, `doctor`'s other items, the stage-2 conformance run, public opt-in | 0.89 |

Each part is a branch from `origin/main` and a PR, merged with a merge commit after the gate
passes. Each updates `docs/upgrading.md`, the `EGM.md` beside the code, `CLAUDE.md` where it
names a module, a command, the sdmxlib cap or a writer rule, and the pages its Docs entry
lists. Each part's tests carry a `pytestmark`, and new files are marked Create. Parts 1 and 3
break callers, so their first PR is `feat!:`, which `cliff.toml:36` groups under "Breaking
Changes". Parts 2, 4 and 5 use `feat:`.

The project owner cuts releases, and no release has a number here: statcatlib may ship other
minors first, for Phase 6. The release PR that first carries Part 1 sets `STAGE_2_SINCE` and
writes its number in `docs/upgrading.md`.

| Release | Carries | Breaking |
|---|---|---|
| The first with Part 1, with or without Part 2 | The lock. No command opts a catalog in, so no catalog becomes 2.x. | `InventoryStorage` gains three members, so a class written against the protocol must add them. A local commit creates `<catalog>/LOCK`, can wait up to 120 s, and can raise `CatalogLockTimeoutError`. A local commit to a hand-written 2.x catalog now succeeds. No catalog is rebuilt, and an older statcatlib reads and writes a 1.x catalog that holds a `LOCK` file. |
| The first with Parts 3 to 5 together | Expiry, restores, the checks and opt-in (O4) | `sdmxlib>=0.89,<0.90`. `verify-catalog` reads every retained root and can report a broken chain on a catalog that passed before. `prune` and `PruneReport` gain keyword parameters and two fields. |
| The first with release-job Part 6 | Server expiry | The release-job design states it. |

A release cut between Part 3 and Part 5 carries `prune --expire-older-than` or `admin restore`
with no way to reach them: both refuse a 1.x catalog. No release opts a catalog in. Opt-in is
the catalog owner's own one-way step, and E7's table gives what each older tool then does.

### Part 1: the catalog lock, the storage methods and the commit checks

*   **`src/statcatlib/store/lock.py` (Create):** `hold`, `probe`, the platform guard, the thread
    table with its mutex, the warned-catalogs set, the constants.
*   **`src/statcatlib/store/inventory.py` (Modify):** the protocol members (`:322-353`);
    `ExpiryStorage`, `Listed`, `Deleted`; `LocalInventoryStorage`'s constructor, `exclusive`,
    `check`, `stage_2` and the six storage methods; `try_commit` and `_commit_locked`
    (`:623-697`); `commit`'s two branches (`:746-771`); `check_writable`'s decoder (`:570`) and
    refusals; `history()`; `unfinished_restore()`; the five error classes; the module docstring
    (`:4-6`, `:28-30`) and the error's (`:123-132`).
*   **`src/statcatlib/store/blob.py` (Modify):** `BlobInventoryStorage.exclusive`, `check`,
    `stage_2 = False` (`:432-628`).
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export the five error
    classes.
*   **`tests/test_inventory_conformance.py:80`, `tests/stores/_backend.py:64` (Modify):** the
    local adapter sets `stage_2 = True`; the blob adapter keeps `False`.
*   **`tests/_history.py` (Create):** a builder that writes a 2.0 `catalog.json`, a `HISTORY`
    and segments by hand, for the tests of E4 before `opt_in` and `restore` exist.
*   **Existing tests and pages that state the stage-1 refusal (Modify):**
    `tests/test_inventory_history.py:300`, `:319` and `:324`, where a local commit to a 2.x
    catalog without `HISTORY` is still refused, with the new message;
    `tests/test_live_cuj.py:40`, which lists the catalog's files and gains `LOCK`;
    `docs/catalog-format.md:48-52` and `:68-69`; `docs/api/store.md:227` and `:411`;
    `src/statcatlib/store/EGM.md`'s `inventory.py` entry.
*   **Docs:** `docs/catalog-format.md` §1 gains `LOCK` in the layout and a paragraph on the
    lock, and §1.3 gains Windows and filesystems without advisory locks. `docs/api/store.md`
    gains the lock, the wait and the errors. `docs/upgrading.md` gains the breaking changes.
    `CLAUDE.md`'s architecture tree gains `store/lock.py`.

The refusals of Part 1 name `CatalogAdmin.restore`, which arrives in Part 4. No user reaches
them before Part 5, since no command makes a catalog 2.x. On sdmxlib 0.88
`unfinished_restore` reads the newest `restore` segments whole with `decode_segment`, and
takes a segment's UUID from the 36 characters before `.jsonl` in its path. Part 3 moves both to
`decode_segment_header` and `parse_segment_path`.

| Test | File | Tier | Covers |
|---|---|---|---|
| `test_a_commit_creates_lock_and_leaves_it`: a commit creates `LOCK`, a second commit reuses it, and its bytes stay empty | `tests/test_catalog_lock.py` (Create) | integration | E1 |
| `test_a_killed_holder_blocks_no_commit`: a child process that holds the lock is killed; `LOCK` stays and the next commit succeeds within 10 s | `tests/test_catalog_lock.py` | integration | GH-125 acceptance, Goal 1 |
| `test_a_second_process_and_thread_wait`: each waits for a held lock, then commits; with `lock_timeout` of 0.2 s each raises `CatalogLockTimeoutError` | `tests/test_catalog_lock.py` | integration | E1, O7 |
| `test_the_lock_is_not_reentrant`: a thread that calls `exclusive()` twice gets `RuntimeError`; `check()` outside it raises | `tests/test_catalog_lock.py` | unit | E1, E2 |
| `test_probe_creates_nothing`: `probe` on a catalog with no `LOCK` answers `absent` and leaves the directory as it was | `tests/test_catalog_lock.py` | unit | E15 |
| `test_a_refused_lock`: with `flock` patched to raise `ENOLCK`, a 1.x commit succeeds with one WARNING, a second logs none, and a 2.x commit raises `CatalogLockUnsupportedError`; with `sys.platform` patched to `win32`, the same with no WARNING | `tests/test_catalog_lock.py` | unit | E5, O2, O6 |
| `test_the_lock_spans_the_decode_to_current`, with a recording storage | `tests/test_inventory_writer.py` | unit | E3, S45 |
| `test_a_commit_that_waited_records_the_time_it_wrote`, with `clock=` | `tests/test_inventory_writer.py` | unit | E3 |
| `test_a_local_commit_to_a_2x_catalog`: on a hand-written 2.0 catalog a commit succeeds; a missing `HISTORY` is refused, in `check_writable` too | `tests/test_inventory_history.py` | integration | E4, E7 |
| `test_a_stale_base_is_a_retried_conflict`: `try_commit` raises `StaleBaseError` for a base at or below `expired_through`, and `commit` succeeds on the head; a `HISTORY` advanced between `commit`'s head read and the lock is seen | `tests/test_inventory_history.py` | integration | E4, S44, GH-125 part 2, Goal 2 |
| `test_a_base_of_an_older_epoch`: with a hand-built finished restore, `try_commit` on the old-epoch base raises `StaleBaseError` and `commit` succeeds on root S + 1 | `tests/test_inventory_history.py` | integration | E4, review B3 |
| `test_an_unfinished_restore_refuses_commits`, parametrised over the hand-built states after restore steps 2, 3 and 4: `unfinished_restore()` returns S, `commit` raises `UnfinishedRestoreError` naming S, and a commit of kind `restore` on root S succeeds | `tests/test_inventory_history.py` | integration | E4, S33 |
| `test_unfinished_restore_is_none`: on a 1.x catalog, an opted-in one, one whose newest segment is an `expiry` segment, and a finished restore with later commits and with root S + 1 missing before it | `tests/test_inventory_history.py` | integration | E4, review B1 |
| The six local storage methods: `list` deep and shallow with a staged file present, tags that change on `replace`, `delete`'s `absent`, `replace` on another path | `tests/test_inventory_writer.py` | unit | E8 |
| `test_a_blob_commit_to_a_2x_catalog_is_still_refused` | `tests/test_blob_storage.py` | unit | E2, E7 |
| `test_run_history_passes_every_check` with `stage_2 = True` | `tests/test_inventory_conformance.py` | integration | N3's base |

### Part 2: opt-in, internal

*   **`src/statcatlib/store/expiry.py` (Create):** `opt_in`, `OptIn`. The module has no
    reference page and no export.
*   **`src/statcatlib/store/inventory.py` (Modify):** `ExpiryRefusedError`.
*   **`src/statcatlib/diagnostics.py` (Modify):** `doctor`'s `catalog.json`, `LOCK` and
    `HISTORY` items.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export
    `ExpiryRefusedError`; assert that `opt_in` and `OptIn` are not exported.
*   **Docs:** `docs/catalog-format.md` §1 gains `inventory/HISTORY` and `catalog.json` 2.0, with
    the sentence that no command writes them yet; `docs/api/admin.md`'s `doctor` section gains
    the three items; `CLAUDE.md`'s tree gains `store/expiry.py`.

| Test | File | Tier | Covers |
|---|---|---|---|
| `test_opt_in_writes_history_then_catalog`: the order, with a recording storage; `since_seq` is the lowest root of the run that carries the fields | `tests/test_opt_in.py` (Create) | integration | E6 |
| `test_a_dry_run_takes_no_lock_and_writes_nothing`: the directory listing is unchanged and holds no `LOCK` | `tests/test_opt_in.py` | integration | E19, S43 |
| `test_opt_in_refusals`, parametrised: a head without the fields, a root with them below one without, a missing root, an undecodable root, a broken chain; each names the root | `tests/test_opt_in.py` | integration | spec §6.1, S41 |
| `test_a_catalog_with_no_version_opts_in_at_1` | `tests/test_opt_in.py` | integration | S41 |
| `test_a_commit_during_verification_is_verified_under_the_lock` | `tests/test_opt_in.py` | integration | O7 |
| `test_a_killed_opt_in_is_completed`, parametrised over a kill after each storage change; a `HISTORY` with other values is refused; a second opt-in returns `already` | `tests/test_opt_in.py` | integration | E6 |
| `test_catalog_json_changed_before_the_replace` raises `InventoryConflictError` | `tests/test_opt_in.py` | unit | spec §6.1 |
| `test_opt_in_on_a_blob_storage_and_on_a_refused_lock` | `tests/test_opt_in.py` | unit | E5, E6, H21 |
| `test_after_opt_in`: a seal succeeds; a reader with `CURRENT` removed finds the head through `HISTORY` | `tests/test_opt_in.py` | integration | E7 |
| `doctor` on a 1.x and a 2.x catalog, with the lock free, held and refused | `tests/test_cli_verify_doctor.py` | unit | E15 |

### Part 3: the expiry engine and `prune`

*   **`src/statcatlib/store/expiry.py` (Modify):** `expire`, the steps, the cache file, the
    events, `ExpiryReport`.
*   **`src/statcatlib/_events.py` (Create), `jobs.py`, `gc.py`, `release.py`, `cli/__init__.py`
    (Modify):** `_emit` moves; the four modules import it from its new place.
*   **`src/statcatlib/admin.py` (Modify):** `prune`'s parameters, `PruneReport.expiry` and
    `.expiry_error` (`:109-139`, `:550-653`).
*   **`src/statcatlib/cli/__init__.py` (Modify):** `--expire-older-than` and `--keep-roots` on
    `prune`, with the lines and exit codes of "Command output and exit codes" (`:579-641`).
*   **`pyproject.toml`, `CLAUDE.md` (Modify):** `sdmxlib>=0.89,<0.90`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export
    `ExpiryReport`.
*   **`tests/_history.py` (Modify):** a catalog builder that writes versions at given times
    across month boundaries, through `clock=`; the reachability computation; `live_at` (E18).
*   **`tests/_fakes.py` (Modify):** `RecordingExpiryStorage`, a wrapper that counts creates,
    replaces and deletes, raises `KeyboardInterrupt` after the n-th, and can block on one named
    call; `MemoryExpiryStorage`, an in-memory `ExpiryStorage` with tags that change on every
    write, a `delete` that honours `if_match`, and a `check()` a test can make fail.
*   **Docs:** `docs/api/admin.md` gains "Expiring Inventory versions": the steps, the dry run,
    the lines, the stops and E11's table. `docs/catalog-format.md` §1 gains `history/`, and its
    sentence that the Inventory writer alone writes `inventory/` (`:48-52`) names the engine.
    Root `EGM.md:69-72` and `src/statcatlib/store/EGM.md` change the same way.
    `docs/upgrading.md` gains the cap and the new parameters.

Each kill test is parametrised over every index n of the recording wrapper, and asserts three
things after the kill and again after the rerun: a reader opens and finds the head and entries
it found before the run; no retained root names a missing leaf; and the reruns reach the same
files as an uninterrupted run. Every test of the engine runs over `LocalInventoryStorage` and
over `MemoryExpiryStorage`.

| Test | File | Tier | Covers |
|---|---|---|---|
| `test_candidates`: a contiguous run, none below `since_seq`, none at or above `CURRENT.seq`, none among the newest K, each with a successor older than W; the checkpoints of `since_seq`, an epoch's first root and each UTC month are stepped over; `walk_end` for each ending | `tests/test_expiry.py` (Create) | integration | expiry steps 0 and 1, Goal 4 |
| `test_current_lagging_by_more_than_k`: expiry step 0 advances it | `tests/test_expiry.py` | integration | spec conformance case |
| `test_the_walk_ends_at_a_bad_root`, parametrised: missing, undecodable, no history fields; the candidates before it are still expired | `tests/test_expiry.py` | integration | E9 |
| `test_a_run_takes_at_most_the_limit`, with a lowered limit; the next run takes the rest | `tests/test_expiry.py` | integration | S46 |
| `test_a_chain_mismatch_writes_nothing` | `tests/test_expiry.py` | integration | expiry step 2 |
| `test_the_chain_starts_at_the_line_or_the_root`: after one expiry that ended below a checkpoint, and after one that ended at `expired_through` | `tests/test_expiry.py` | integration | S32 |
| `test_segments`: one per epoch, split at 1,000 lines and at the byte limit (lowered), one UUID, linked in order, each line equal to `segment_line` of its root, the header's `roots` and `leaves` | `tests/test_expiry.py` | integration | expiry step 3, S47 |
| `test_live_at_is_the_same_after_expiry`, for a T in each of three months | `tests/test_expiry.py` | integration | GH-125 acceptance, Goal 6 |
| `test_no_named_leaf_is_deleted`: an independent reachability computation over the files, compared with the `expiry.deleted` events | `tests/test_expiry.py` | integration | GH-125 acceptance, Goal 4 |
| `test_a_kill_after_each_storage_change`, parametrised over n, in a run of three segments: the three assertions above; linked orphans are adopted and an unlinked one is left | `tests/test_expiry_crash.py` (Create) | integration | GH-125 acceptance, Goal 5, E11 |
| `test_a_second_kill_during_recovery`, parametrised over n | `tests/test_expiry_crash.py` | integration | E11 |
| `test_a_recovery_run_takes_no_candidate`, and the run after it does | `tests/test_expiry_crash.py` | integration | S46, review m1 |
| `test_a_root_with_no_matching_line_is_kept`, with `expiry.kept_root` | `tests/test_expiry_crash.py` | integration | expiry step 5 |
| `test_an_unreadable_retained_root`: in a plan, nothing is written; in a recovery run, no leaf is deleted and `cleaned_through` stays | `tests/test_expiry_crash.py` | integration | S42 |
| `test_a_failed_delete_leaves_cleaned_through`, and the next run finishes | `tests/test_expiry_crash.py` | unit | E11 |
| `test_check_precedes_every_write_and_delete`, and a `check()` that raises ends the run before the next one, with `stopped = "lock-lost"` | `tests/test_expiry.py` | unit | GH-125 acceptance (the engine's half of the failed renewal) |
| `test_a_leaf_retagged_after_its_final_check_survives`, over `MemoryExpiryStorage` | `tests/test_expiry.py` | unit | GH-125 acceptance (the engine's half of `If-Match`), Goal 9 |
| `test_a_seal_during_the_plan_is_kept`: the wrapper blocks the plan's last read, a seal commits, the run goes on, and every leaf the seal's root names exists | `tests/test_expiry_race.py` (Create) | integration | GH-125 acceptance, O7 |
| `test_a_seal_waits_for_the_writes`: the wrapper blocks the first segment create under the lock; the seal's `try_commit` waits, then commits | `tests/test_expiry_race.py` | integration | GH-125 acceptance |
| `test_two_processes_seal_and_prune`: one seals in a loop and one prunes in a loop for a fixed count; every root's leaves exist at the end; a 60 s timeout | `tests/test_expiry_race.py` | integration | GH-125 acceptance |
| `test_two_expiries_at_once`: the second stops with `history-changed` and writes nothing | `tests/test_expiry_race.py` | integration | S46 |
| `test_prune_retires_and_expires_in_one_call`, under a 30 s timeout | `tests/test_prune.py` | integration | GH-125 acceptance |
| `test_prune_without_inventory_window_expires_nothing`; a window below `older_than` plus a day, a 1.x catalog and an unfinished restore are refused before the retire step | `tests/test_prune.py` | unit | E13, O9 |
| `test_prune_reports_an_expiry_error_after_retiring`, with the lock held by another process and a short timeout | `tests/test_prune.py` | integration | E13 |
| `test_a_dry_run`: no lock, no `LOCK`, no file written, no `CURRENT` moved; its plan equals what the real run then does; on a catalog with pending segments it reports the recovery | `tests/test_expiry.py` | integration | E19 |
| `test_the_cache_file`: a second run reads only the roots written since the first; a corrupt file, another `catalog_id` and another epoch each cost one full read | `tests/test_expiry.py` | integration | release-job S23, review m2 |
| `test_expire_refuses`: a 1.x catalog, no `HISTORY`, an unfinished restore, a refused lock, a blob storage | `tests/test_expiry.py` | unit | E9 |
| `test_a_second_run_writes_nothing` | `tests/test_expiry.py` | integration | idempotency |
| `test_each_event_has_its_fields`, one case per event of E12 | `tests/test_expiry.py` | unit | E12 |
| `statcat prune --expire-older-than`: each line, each exit code, the usage errors | `tests/test_prune.py` | unit | "Command output and exit codes" |

### Part 4: restores

*   **`src/statcatlib/store/expiry.py` (Modify):** `restore`, `RestoreReport`,
    `DiscardedVersion`, `Transition`.
*   **`src/statcatlib/admin.py`, `src/statcatlib/cli/__init__.py` (Modify):**
    `CatalogAdmin.restore`, `statcat admin restore`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export the three
    report classes.
*   **`tests/test_inventory_conformance.py:184-197` (Modify):** on an opted-in catalog
    `restore_with_new_epoch` calls `restore`; `restore_to` stays for 1.x catalogs.
*   **Docs:** `docs/api/admin.md` gains "Restoring a catalog": the refusals, the report, the
    state after a kill, and the difference from `rollback`.

| Test | File | Tier | Covers |
|---|---|---|---|
| `test_the_order_of_a_restore`, with a recording storage: the segments, `HISTORY` with the new epoch and an unchanged `expired_through`, `catalog.json`, the deletes from the head down, root S + 1 of kind `restore` linked to root S, `CURRENT` | `tests/test_restore.py` (Create) | integration | GH-125 part 3 |
| `test_restore_refusals`, parametrised: a 1.x catalog; `to` at `expired_through` and below it; `to` below `since_seq - 1`; no root above `to`; a missing root; a missing leaf; under Q1 (a) a live entry with no manifest; a blob storage; a refused lock. Each writes nothing. | `tests/test_restore.py` | unit | S40, O1, Q1 |
| `test_the_tombstones`: root S + 1's `retired` holds S's tombstones and one for each edition only a readable discarded root recorded, a product root S does not record included; its change list holds them; `collect_garbage`'s kept set, built by hand, includes them | `tests/test_restore.py`, `tests/test_gc.py` | integration, unit | release-job S25, S48, Goal 8, N4 |
| `test_unread_roots`: listed in `unread`, deleted, and the report is `incomplete` | `tests/test_restore.py` | integration | O5 |
| `test_the_report`: a discarded cancellation, a rescheduled `release_at`, a discarded `embargo-change` and root S's own flag each appear | `tests/test_restore.py` | integration | O5, review M3 |
| `test_a_kill_after_each_storage_change`, parametrised over n, with roots of two epochs so that two segments are written: after the kill `unfinished_restore()` answers as E14's table says, a commit and `expire` are refused where it says, a reader with `CURRENT` removed fails closed until root S + 1 exists; the rerun reaches the files and the `retired` of an uninterrupted run | `tests/test_restore.py` | integration | review B1, B2, S33, S47, S48 |
| `test_a_rerun_with_another_target_is_refused`, naming S | `tests/test_restore.py` | integration | S33 |
| `test_root_s_plus_1_missing_before_the_restore`: the roots above S start at S + 3; the restore finishes, `unfinished_restore()` is `None`, and it stays `None` after two more commits | `tests/test_restore.py` | integration | review B1 |
| `test_a_restore_abandoned_before_history`: the catalog works on the old epoch, and a new restore writes new segments | `tests/test_restore.py` | integration | S35 |
| `test_a_second_call_returns_already`, also after a kill between the root and `CURRENT`, where it moves `CURRENT`; after a later seal the same call is a new restore whose dry run lists the seal | `tests/test_restore.py` | integration | E14 |
| `test_expiry_after_a_restore`: the engine skips the `restore` segment, expires roots of both epochs into `history/<epoch>/`, and its chain check passes | `tests/test_restore.py` | integration | S35, S32 |
| `test_a_dry_run_takes_no_lock_and_writes_nothing` | `tests/test_restore.py` | integration | E19 |
| `test_rollback_after_expiry`: `CatalogAdmin.rollback` on an opted-in, expired catalog makes a superseded edition live | `tests/test_catalog_admin.py` | integration | E14 |
| `statcat admin restore`: each line, each exit code, a blank reason | `tests/test_cli_admin.py` | unit | "Command output and exit codes" |

### Part 5: `verify-catalog`, `doctor`, the conformance run and public opt-in

*   **`src/statcatlib/diagnostics.py` (Modify):** `verify_catalog` through `check_history`,
    `VerifyReport.notes`; `doctor`'s pending, checkpoint, segments, restore and leftover items.
*   **`src/statcatlib/admin.py`, `src/statcatlib/cli/__init__.py` (Modify):**
    `CatalogAdmin.opt_in`; `statcat admin opt-in`, beside the other `admin` commands
    (`:826-836`); the `ORPHAN` lines (`:514-550`); `src/statcatlib/cli/EGM.md`.
*   **`src/statcatlib/__init__.py`, `tests/test_public_api.py` (Modify):** export `OptIn`.
*   **`tests/test_inventory_conformance.py` (Modify):** `WriterBackend` implements N3's
    `ExpiryBackend`. `opt_in`, `expire` and `restore` call the functions of `store/expiry.py`
    over a `RecordingExpiryStorage` that stops after `stop_after` changes, with `now` through
    `clock=`; `verify` calls `verify_catalog`.
*   **Docs:** `docs/api/admin.md` gains "Opting in to expiry", with the three statements, the
    order of upgrades, Windows and network filesystems, and the advice to run the first expiry
    soon after; its `verify-catalog` and `doctor` sections gain the history checks, the `ORPHAN`
    note and the new items. `docs/catalog-format.md` §6 names the checks, and §1 drops the
    sentence that no command writes `HISTORY`.

| Test | File | Tier | Covers |
|---|---|---|---|
| `test_verify_passes_on_an_expired_catalog`, and across a restore, keyed by `(restore_epoch, seq)` | `tests/test_cli_verify_doctor.py` | integration | spec check 1 |
| `test_verify_findings`, parametrised: an edited segment line, a deleted root above `expired_through`, a deleted retained leaf, a segment whose link names no file, a `cleaned_through` off the chain, a root without the fields above `since_seq`; each is one finding that names the file, and exit 1 | `tests/test_cli_verify_doctor.py` | integration | GH-125 acceptance, Goal 7 |
| `test_an_orphan_is_a_note`: `ORPHAN`, exit 0; a staged file under `history/` is neither | `tests/test_cli_verify_doctor.py` | unit | O11, S36 |
| `test_verify_reports_an_unfinished_restore` as one finding naming S | `tests/test_cli_verify_doctor.py` | integration | E15 |
| `test_a_1x_catalog_with_history_fields_gets_the_chain_check` | `tests/test_cli_verify_doctor.py` | integration | spec §6.9 |
| `doctor`: the newest checkpoint; an orphan; a pending segment after a kill; no pending warning after a restore; an unfinished restore; a staged file under `history/` | `tests/test_cli_verify_doctor.py` | unit | GH-125 part 3, Goal 7 |
| `test_run_expiry_passes_every_check` against the local writer | `tests/test_inventory_conformance.py` | integration | GH-125's first acceptance item |
| `statcat admin opt-in`: the dry run's lines, `--apply`, already opted in, `az://`, exit codes | `tests/test_cli_admin.py` | unit | E6, Goal 3 |
| `test_cuj_1`: opt in, seal across a month boundary with `clock=`, prune with expiry, verify, restore, verify | `tests/test_opt_in.py` | integration | CUJ 1, CUJ 4 |

### Acceptance map

GH-125's acceptance list holds fourteen items. The suite design's Handoff lists ten of them,
and the other four are GH-125's own.

| GH-125 acceptance item | Goal | Part | Test |
|---|---|---|---|
| sdmxlib's history conformance cases pass against statcatlib's writer, and every root it writes carries both history fields | | Stage 1: done in 0.14.0. Stage 2: Part 5, on local disk; the blob adapter keeps `stage_2 = False` until release-job Part 6. | `test_run_expiry_passes_every_check` |
| "What was live at time T" gives the same answer after expiry as from the full roots before it | 6 | 3 | `test_live_at_is_the_same_after_expiry` |
| Expiry never deletes a leaf a retained root names, checked by an independent reachability computation compared with the deletion log | 4 | 3; on Azurite, release-job Part 6 | `test_no_named_leaf_is_deleted` |
| A seal that races `prune` on a local catalog either commits before expiry chooses or waits for it, and every leaf its root names exists | 1 | 3 | `test_a_seal_during_the_plan_is_kept`, `test_a_seal_waits_for_the_writes`, `test_two_processes_seal_and_prune` |
| A process killed while it holds the local lock leaves `LOCK` in place, and the next commit proceeds | 1 | 1 | `test_a_killed_holder_blocks_no_commit` |
| `prune` with expiry commits its `retire` version and expires in one call without deadlock | | 3 | `test_prune_retires_and_expires_in_one_call` |
| An expiry crash after each step recovers on the next run under the crash rules | 5 | 3 | `test_a_kill_after_each_storage_change`, `test_a_second_kill_during_recovery` |
| A second release-job run fails to take the `HISTORY` lease and writes nothing | | release-job Part 6 | its `tests/stores/test_expiry_blob.py` |
| A failed lease renewal stops expiry before its next destructive call | | 3 for the engine; release-job Part 6 for the lease | `test_check_precedes_every_write_and_delete`; its `tests/test_history_lease.py` |
| A leaf rewritten after step 6's final check survives the `If-Match` delete | 9 | 3 for the engine; release-job Part 6 for the delete | `test_a_leaf_retagged_after_its_final_check_survives`; its `tests/stores/test_expiry_blob.py` |
| The pod check with an unreachable pod or a missing `sdmxlib_version` skips expiry, and the release step still runs | | release-job Part 6 | its `tests/test_release_job.py` |
| Every entry recorded after Phase 5 carries `artefacts` equal to its manifest's records | | done in 0.17.0 | `tests/test_inventory_routing.py` |
| `verify-catalog` detects an edited segment line, a deleted root, and an entry whose `artefacts` differ from its manifest | 7 | 5; the third is done in 0.17.0 | `test_verify_findings` |
| `just ci` passes | | every part | the gate |

The goals no acceptance item names map as follows.

| Goal | Part | Test |
|---|---|---|
| 2. No root on a base at or below `expired_through` | 1 | `test_a_stale_base_is_a_retried_conflict` |
| 3. One opt-in command, with its refusals, in the release that has the rest | 2, 5 | `test_opt_in_refusals`; `statcat admin opt-in`'s tests; the land order |
| 8. A restore records what it discards and tombstones what only it recorded | 4 | `test_the_order_of_a_restore`, `test_the_tombstones` |
| 9. The engine runs over a second storage | 3 | every engine test over `MemoryExpiryStorage` |

GH-125's part 2 and part 3 bullets outside the acceptance list map to Part 1 (the lock span,
the stale-base check, non-reentrancy, the 2.x decode), Parts 2 and 5 (opt-in and its
refusals), Part 3 (the steps and the state after a kill), Part 4 (restores) and Part 5
(`verify-catalog`, `doctor`).

### Verification plan

*   The gate for each part, on a machine with a container runtime, and `just coverage` in each
    part's PR.
*   Part 3, a bench recorded in `bench/`: `prune --expire-older-than` on a copy of the GH-126
    corpus (2,979 versions), opted in. It records the time under the lock, the time and bytes
    read outside it, and the peak memory, for the first three runs and for a nightly-sized run,
    with and without `state_dir` on `expire`. The figures replace the estimates in the
    Non-functional requirements and decide whether `prune` gains `--state-dir`.
*   Part 5: `statcat verify-catalog` on that corpus after the expiry, with its time recorded,
    and a restore drill on a copy: damage the two newest roots, restore, then run
    `verify-catalog`, a seal and a second expiry.
*   Before release-job Part 6: the engine's tests already run over `MemoryExpiryStorage`. The
    re-review of release-job Part 6 reads R1 to R12 against that storage's methods.

## Future work

*   Collection of leaves no root names, after a crashed commit or a restore.
*   A restore to a root at or below `expired_through`, of a 1.x catalog, and one that discards
    no root.
*   `verify-catalog` and `doctor` over a `BlobLocation` (Q2).
*   A lock on Windows, and a lock for S3 and GCS (H21, H23).
*   A public reader of history segments with E18's replay, with the stage-3 exporter.
*   `--state-dir` on `prune`, if the bench asks for it.

## Review response

Round 1 ([report](egm/inventory-history-stage-2/review-1.md)). Each finding and its change in
this revision.

| Finding | Change |
|---|---|
| B1 | O1 removes the target below `expired_through`. A restore's segments share one UUID (S47), `unfinished_restore()` derives S from the least `first` among them and the roots' epochs (E4), a rerun accepts only that S, and a target with no root above it is refused (S40). Tests for two segments, a missing root S + 1 and later commits. |
| B2 | Restore step 5 computes the tombstones from the segments' lines in every attempt (S48), restore step 4 keeps a root of the new epoch, E14's table has reader, writer and detection columns, and the `already` call moves `CURRENT`. |
| B3 | One function, `InventoryWriter.unfinished_restore`, in Part 1 with tests. E4 orders four checks; a base of another epoch is a `StaleBaseError`; `commit` calls the function on its two error paths. The engine, `restore`, `verify_catalog` and `doctor` use it. GC's stop moves to R9. |
| B4 | The order is 1, 2, 3, 4, 5 with restores as Part 4. The conformance run and the cross-restore `verify-catalog` test are in Part 5, and the land order has no "either order". |
| M1 | S32 and expiry step 2 use the line only when c - 1 equals `expired_through`, of the head chain's epoch. O1 removes the reused `seq`. |
| M2 | Q1, with refusal recommended; the report lists the `superseded` and `withdrawn` entries whose directories are gone. |
| M3 | O5: no guard here, S34 withdrawn, R9 carries the question. The report has each version's `kind`, `actor`, `reason`, `embargo` and each transition's `release_at`, root S's flag, and `incomplete`. CUJ 4 has one readable root and names `statcat admin cancel`. |
| M4 | O2: no lock on Windows; E1 and E5 give the guard and the refusals; the `msvcrt` items leave "Not confirmed". |
| M5 | O7 and S46: the plan and opt-in's verification run before the lock; a run takes at most 1,000 candidates; the Non-functional requirements state the lock's bound and opt-in's cost. |
| M6 | The Cross-repo contract section says which rows contradict revision 5; each S row has Changes, Repositories and an "Other option"; twelve rows are marked hub; the land order has revision 6 as row B. |
| M7 | S44 (the retried conflict, gating Part 1), S45 (the lock span) and S46 are new rows. GC's stop and the server restore go to R9. `verify-catalog` over a blob store is Q2. |
| M8 | O1 takes option (d). The exemption, the `restoring` parameter and S43's first half are gone. The remaining restore rows wait for revision 6 (land order rows B and C). |
| M9 | Each S row gates the first part that codes it; S43 is one rule; Part 1 tests the unfinished-restore refusal. |
| M10 | O4: `opt_in` is internal from Part 2 and public in Part 5, and the release table says no release opts a catalog in before Parts 3 to 5. |
| M11 | The header reads the current heads; stage-1 writers are 0.14.0 to the last release before Part 1; `STAGE_2_SINCE` carries the number; the release table names no number and says who fills it. |
| M12 | 0.89.0 carries N1 to N4; the land order defines "waits on" and has a re-read step (row G); N1 asks for `decode_segment_header` and `parse_segment_path`; Q1 (b) is rejected by O3. |
| M13 | E9 has the nine-step table and the table of stops; E12 gives typed reports, each event's fields, when events are emitted and what expiry step 8 does; E10 gives the cache file's schema; E19 covers dry runs in each state. |
| M14 | `delete` has three outcomes; E8 names the store faults and what the engine does with them; `ExpiryStorage` is `@runtime_checkable` and lives in `store/inventory.py`; `opt_in` lives in `store/expiry.py`; `write_leaf`, `read_range` on a short object and `list`'s order are stated. |
| M15 | The section "Command output and exit codes"; blob storage raises `ExpiryRefusedError`; `PruneReport.expiry_error`. |
| M16 | `LocalInventoryStorage(clock=, lock_timeout=)`; `RecordingExpiryStorage`; N3's `stop_after` counts storage changes. |
| M17 | Both kill tests are parametrised over every storage change with three stated assertions; the rows the review listed are added. |
| M18 | E18 gives the replay. Goal 9 is restated and tested over `MemoryExpiryStorage` in Part 3. `ExpiryStorage` and the functions are internal. The Acceptance map has a Goal column and a table for the other goals. |
| M19 | E19: no dry run takes the lock or creates `LOCK`, in one table. |
| M20 | "Expiry step", "restore step", "commit step" and "opt-in step" throughout; the GH-125 mapping table; "release-job" before each of its identifiers; E4 numbers its four checks; "2.x catalog" is a term. |
| M21 | The Terms table. |
| M22 | The checkpoint rule, expiry step 6's pause, the kept-root rule, `check_writable`'s behaviour, opt-in's `HISTORY` rule, the unread root's group and the rerun are rewritten as the style report proposed. |
| m1 | A recovery run takes no candidate (S46). |
| m2 | The cache file holds `catalog_id` and `restore_epoch`, and the engine discards a file that differs. |
| m3 | Adoption compares the whole line; a recovery run checks each pending segment's SHA-256. |
| m4 | E11 states the crash model and the directory fsyncs. |
| m5 | `parse_segment_path` filters the listing; `_leftovers` scans `history/`. |
| m6 | `doctor` warns for a pending `expiry` segment only. |
| m7 | `LOCK` is opened read-only; the forked-child limit is stated; a plan-time unreadable root stops before any write; the repeat call is defined. |
| m8 | `probe` opens without `O_CREAT`. |
| m9 | `lock_timeout=`, the mutex, the warned set, E3's `except` branch, and `stage_2`'s meaning. |
| m10 | `_stage`, `_fsync_dir` and `RetentionPolicy.check` are named; `_emit` moves to `_events.py`. |
| m11 | The invariant sentences, exports and docs are in each part's lists. |
| m12 | The six references are corrected. |
| m13 | `tests/test_live_cuj.py:40` and `tests/test_inventory_history.py:324` are in Part 1. |
| m14 | Part 1 states that no user reaches the message before Part 5. |
| m15 | `feat!:` for Parts 1 and 3. |
| m16 | Signatures for `opt_in` and `restore`; the `dry_run` defaults are stated as intended. |
| m17 | No change to `gc.py`; the GC test builds the kept set by hand. |
| m18 | The Acceptance map names tests, the deadlock tests have timeouts, and the blob adapter's limit is stated. |
| m19 | "Not confirmed" has a "Settled by" column; N4 asks sdmxlib for the test. |
| m20 | S31 is withdrawn. |
| m21 | No adoption of `restore` segments; `already`; no `--state-dir` on `prune`. Not applied: `read_range` stays, since O3 puts the header decode in sdmxlib 0.89 and a whole-segment walk reads the full history in every run without the cache. |
| m22 | Goal 1 names the filesystem. The lock on 1.x catalogs stays (O6), with the option in "Alternatives considered". |
| m23 | The Azure terms table is gone, and the storage rights are R12; N4's `live_at` is dropped; Handoff has an sdmxlib-rest subsection, which corrects revision 1's R10. |
| m24 | The answered questions are in "Owner decisions"; Q1 and Q2 give reasons; the Non-goals follow the decisions. |
| m25 | The sentence after the decision table says which sections hold options; E14 and E12 place the `restore.*` events; the Errors table is complete. |
| m26 | One basis, the GH-126 corpus at 560 KB a root; every time is labelled an estimate. |
| m27 | CUJ 2 names the base and both orders; CUJs 2 to 4 give lines and exit status. |
| m28 | `doctor` checks links only; H4's rule is listed as three cases. |
| m29 | The plan has a path and a commit in Inputs. |
| m30 | Headings are noun phrases in sentence case. |
| m31 | The prose findings are applied where the text survived; `GH-126`; the abbreviations are in Terms. |

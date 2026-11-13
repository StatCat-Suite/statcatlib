# Design critic review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (revision 1)
**Reviewer lens:** `egm-design`

Verdict: 1 blocker (Part 5 only), 6 majors, 8 minors. Parts 1 to 4 have no blocker. The reviewer found no interleaving in which expiry deletes a leaf a retained root names, and none in which a root is deleted without a matching line in a segment `HISTORY` reaches. The serious problems are all in restores and in the two spec changes that cover them (S32, S33).

**Read:** the persona, the whole design, spec §6.1, §6.2, §6.6 to §6.9, suite design revision 5 (Reader through Embargo, Decisions, Alternatives, Risks), release-job S23/S25/S26 and D13, and the code cited below. Nothing was run; every finding is from reading.

**Paths:** SPEC = `/Users/gabrielgellner/dev/sdmxlib/docs/design/sharded-bundle-catalog.md`; SUITE = `/Users/gabrielgellner/dev/statcat-suite.github.io/docs/design/inventory-history-log.md`; code paths are relative to the statcatlib worktree unless given in full.

## Blocker

### B1. The restore resume rule "S = `first - 1` of the newest `restore` segment" gives the wrong S

- **Where:** S33 (line 191), E14 step 1 (857-863), E14 crash table (884). Blocker for Part 5 and for S33's text.
- **Evidence:** "A rerun takes S from the newest `restore` segment (`first - 1`) and refuses another target" (191). "takes S as `first - 1`; refuses another `to`" (884). Unfinished is detected as "no root of the new epoch at `first`" (884).
- **Four cases where it fails:**
  - (a) Restore to a checkpoint below `expired_through`, the case S33 exists for. Roots 1..420, checkpoints 1 and 300, `expired_through` 350. Restore to S=300 lists roots 351..420, so `first` = 351. Crash after step 2. The rerun computes S = 350. `--to 300` is refused as "another target"; `--to 350` is refused because root 350 was expired. No supported command resumes. Writers, expiry and GC refuse and fallback readers fail closed, as E14's own table says.
  - (b) More than 1,000 discarded roots, or roots of two epochs. E14 step 1 splits "as E10 splits" and groups per epoch, so the newest restore segment is the last, and its `first` is S+1001 or the later epoch's first root. Example: restore #1 to 400 gives epoch 1 roots 401..420. Restore #2 to S=390 writes segment A (epoch 0, 391..400) and B (epoch 1, 401..420). Newest is B, so S computes as 400. Root 400 exists and root 401's `previous_root_sha256` hashes it, so a rerun with `--to 400` passes the pre-checks and restores to the wrong version.
  - (c) Root S+1 is missing (failover lag). Only listed roots go in `roots`, so `first` > S+1.
  - (d) No root above S (a pure epoch bump after a failover). `SegmentHeader.roots` has `min_length=1` (`/Users/gabrielgellner/dev/sdmxlib/src/sdmxlib/inventory/_format.py:485`), so no restore segment can be written and step 2 has no `newest_segment` to set. The design does not say whether this restore is refused.
- In (a) and (c), "no root of the new epoch at `first`" also stays true after the restore finishes, so `doctor` and `verify-catalog` (E15) would report a finished restore as unfinished. E4 uses a different test for the same state.
- **Fix:**
  - Derive S as the highest root under `inventory/` below the lowest `first` among this restore's segments. That root is S, because step 1 lists every root above S and S is verified to exist.
  - Identify this restore's segments by walking back from `newest_segment` over `restore` segments, and stop at the first whose roster is not wholly above the candidate S.
  - Accept the operator's `--to` only if it equals that value.
  - Define "unfinished" once, for E4, E14, E15 and GC: `HISTORY.restore_epoch` differs from `catalog.json`'s, or no root of `HISTORY.restore_epoch` exists at S+1.
  - State what a restore with nothing above S does. This needs an owner decision.

## Major

### M1. S32's chain start is looked up by `seq` alone; after a restore below `expired_through`, expiry stops permanently

- **Where:** S32 (190), E9 step 2 (649-655).
- **Evidence:** "the hash of c - 1 is the `root_sha256` of its line in the newest `expiry` segment ... and the hash of retained root c - 1 when that segment holds no such line."
- **Sequence:** same catalog as B1(a). The first expiry wrote one segment with lines 2..299 and 301..350 (epoch 0). Restore to 300 gives epoch 1 with root 301 (first of its epoch, so a checkpoint) and 302 onward. At the next expiry the lowest candidate is c=302 and c-1=301. The newest `expiry` segment holds a line with seq 301, the epoch-0 root. Its `root_sha256` differs from what epoch-1 root 302 chains to. The run ends with `expiry.chain_mismatch` and writes nothing. No later run can write a newer `expiry` segment, so every run fails the same way. Nothing is deleted, but the catalog never expires again.
- E9's checkpoint rule (639-642) uses the opposite precedence: retained root first, line only when the root is expired.
- **Fix:** take the line only when c-1 equals `expired_through` and the line's `restore_epoch` is the head-chain epoch at that seq; otherwise use retained root c-1. Word S32 that way. Add the Part 5 test: restore to a checkpoint whose successor's seq is in the newest expiry segment, then expire.

### M2. A restore does not check that the editions root S records still have their files

- **Where:** E14 "Before step 1" (851-855), Q8 (175), Goal 8 (140).
- **Evidence:** the pre-check is "Root S must exist, decode, and have every leaf it names present and hashing to its name". Leaves only. Local `prune` deletes "the directory of each edition tombstoned for longer than the window" (`src/statcatlib/admin.py:559-560`, `_prune_dirs` `:740-769`), and GC does the same on a server. SPEC §6.9 check 2 says a checkpoint's leaves "can name manifests GC has deleted".
- **Sequence:** S records E1 `live`. Root S+5 supersedes E1, S+9 retires it, and a later prune deletes `products/P/E1`. Restore to S. Root S+1 carries S's leaves, so the head records E1 `live` with no manifest or data. The S25 rule adds no tombstone, because S records E1 in a leaf. Any restore to a checkpoint older than `older_than` does this. The dry run prints discarded roots, transitions and tombstones, and nothing about S's own editions.
- **Fix:** before step 1, check each `live` and `embargoed` entry of S (manifest present, `manifest_sha256` matches) and report the superseded and withdrawn ones that are gone. Make the response an open question: refuse; restore and list them; or retire them in S+1.

### M3. Q6 and S34 do not let the owner decide, and option (b) does not hold in three cases

- **Where:** Q6 (173), S34 (192), CUJ 4 (287-293), E14 step 5 and `RestoreReport` (868-874, 888-892).
- **Evidence:**
  - (i) No automatic release exists on a local catalog. `release_due` is called only at `src/statcatlib/release.py:1520`, and `statcat release` takes only `az://` and requires Azure (`src/statcatlib/cli/__init__.py:1238-1288`). Part 5 builds the local restore only; the server restore is R9. So under (b) every local restore turns the embargo on, every later local seal is recorded `embargoed`, and the hazard it guards cannot occur there. Q6's cost line does not say this.
  - (ii) `transitions` is "each discarded root's change items". An `embargo-change` has empty `changes` (SPEC §6.2 `changes` row). Sequence: S has `embargo: false`; root S+1 is an operator's `embargo-change` to on, made to stop releases; restore to S. Root S+1 (new epoch) is written with `embargo: true`. `transitions` lists nothing for the discarded embargo-change. The operator follows CUJ 4 ("repeats the cancellations the report lists and turns the embargo off"), and the next release run releases everything due.
  - (iii) A discarded root that is `unread` has no line, so its transitions are unknown. CUJ 4 is this case: the three damaged roots are the three discarded ones. A cancellation in one of them cannot be listed, and turning the embargo off releases the edition.
  - (iv) A reschedule shows as `embargoed` to `embargoed`; the report's product, edition, `from` and `to` omit `release_at`.
- **Traced and sound here:** an edition first recorded after S in a readable discarded root is tombstoned in S+1 and cannot be released. An edition only in an unread root is held on a server by S25's fold rule (`release_at: null`). A commit or release that read the old-epoch head is refused by E4's epoch test.
- **Fix:**
  - Build the report from each discarded line's `event` and `embargo` as well as `changes`, and include the entry record.
  - When any root is `unread`, say the transitions are incomplete and tell the operator to review every embargoed entry with a `release_at` before turning the embargo off.
  - Add options to Q6: (d) set `embargo: true` only when S records at least one embargoed entry with a non-null `release_at`, or only on a store with a release job; (e) clear `release_at` only on entries a discarded transition touched, falling back to (b) when a root is unread.
  - Record S's own `embargo` value in the report.

### M4. E4's unfinished-restore test also fires for a stale base across a finished restore, and its message is unreachable for most of an unfinished one

- **Where:** E4 (451-454), E14 table (884).
- **Evidence:** "or the base's `restore_epoch` differs from `HISTORY`'s, a restore stopped between its steps 2 and 5." That inference is wrong.
- **Sequence 1:** `commit` reads head H (epoch 0) at `src/statcatlib/store/inventory.py:747`, then waits up to 120 s for the lock a restore holds. The restore completes (epoch 1, root S+1, `CURRENT`). The commit takes the lock; `base.restore_epoch` is 0 and `HISTORY`'s is 1. It raises `InventoryWriteRefusedError` naming `statcat admin restore`, and `commit` does not retry. A re-read of the head would have succeeded. Every commit that overlaps a restore gets this.
- **Sequence 2:** after step 4 and before step 5, `CURRENT` names a deleted root. `InventoryWriter.full()` goes to the reader fallback, which takes epoch 1 from `HISTORY`, finds no root of it, and raises `InventoryError` "the catalog has no valid Inventory version" (`/Users/gabrielgellner/dev/sdmxlib/src/sdmxlib/inventory/_reader.py:194-207`). `_commit_locked` never runs, so the refusal naming the restore never appears.
- The refusal itself is necessary. Without the epoch test, a stale epoch-0 base at a seq the new epoch has reused would pass `_stored_root` and commit discarded state.
- **Fix:** under the lock, call it an unfinished restore only when the epochs of `HISTORY` and `catalog.json` differ or no root of the new epoch exists at S+1. A base of another epoch on a finished restore is a `StaleBaseError`, which `commit` retries. Have `commit` or `full()` test for an unfinished restore before the head read, or map the fallback failure. N3's case should distinguish the two.

### M5. On Windows, E5 would read lock contention as "no advisory locks"

- **Where:** E1 Windows (335-338), E5 (467-469), S39 (197).
- **Evidence:** "`msvcrt.locking` fails with `EINVAL` or `EACCES` variants there (Not confirmed)". Microsoft's CRT documentation for `_locking` gives `EACCES` as the locking-violation error, which is what `LK_NBLCK` returns when another process holds the bytes. The reviewer did not run this.
- **Consequence:** if E5 maps `EACCES` to `CatalogLockUnsupportedError`, a held lock makes a 1.x commit log a warning and proceed unlocked, and a 2.x commit fail at once where it should poll. That drops mutual exclusion exactly under contention.
- **Fix:** treat `EACCES` and `EDEADLOCK` as "held, retry" on Windows, and classify "unsupported" by another signal or not at all there. Move "is Windows supported" from the Risks table (1198) to an open question that gates Part 1, since S39 writes the Windows lock into the spec.

### M6. The lock is held across all of a run's reads, and Q9 offers only a timeout

- **Where:** Q9 (176), E9 (616-621), E10 plan (664-670), E6 steps 1 to 4 (490-503), NFR lock hold (1124-1131).
- **Evidence:** the design's own figures. Without `state_dir`, every run reads every retained root under the lock ("1.7 GB each run, tens of seconds"). The first run reads 4.7 GB, deletes thousands of roots, and "can exceed 120 s", when every seal fails. The only mitigation is a sentence on the opt-in page. Opt-in also reads every root under the lock, twice (its dry run takes the lock, where expiry's dry run does not), and has no row in the NFR table.
- **Options the design does not weigh:**
  - Read retained roots and the opt-in chain before taking the lock, then re-list under it and read only what is new. Roots are immutable and tagged.
  - Cap the candidates per run, for example at one segment, so the first expiry converges over several runs. Azure has a 30-minute cap; local has none.
  - Make the wait a parameter.
- **Fix:** add these to Q9 or a new question, and add opt-in's cost to the NFRs.

## Minor

### m1. A recovery run with new candidates leaks leaves permanently

- **Where:** E10 (664-677), E11 (713-716).
- The plan counts every listed root "that is neither a candidate nor already read" as outside the candidates. After a crash in step 5, the undeleted pending roots are still listed. Leaf L is named by pending root P and new candidate C and no kept root. L is not in the old header (C was kept then) and not in the new one (P counts as outside). Step 5 deletes P and C, and step 6 never sees L. SPEC says "no root the run keeps names". No needed leaf is deleted.
- **Fix:** run steps 5 to 7 for pending segments before step 1, or exclude roots that match a pending line from the "outside" set.

### m2. The `state_dir` cache is the one input that could make step 6 delete a named leaf, and the Risks row (1209) does not list it

- **Where:** E10 (665-670), E8 local tag (576).
- The key is path plus `(st_ino, st_mtime_ns, st_size)`. A restore reuses root paths in a new epoch, and one `--state-dir` can serve two catalogs. A collision is unlikely.
- **Fix:** store `catalog_id` and `restore_epoch` in the file and discard it when either differs; state that the file is per catalog.

### m3. Adoption and step 5 compare only `root_sha256`

- **Where:** E10 (684-688), E11 step 5 (718-720). The engine holds the root's bytes at both points.
- **Fix:** require the whole line to equal `segment_line(root, bytes)`, and check each pending segment's SHA-256 against `HISTORY` or the next header before deleting from it.

### m4. The crash model and durability are unstated

- **Where:** Goal 5 (136), NFR Atomicity (1138-1140).
- `create_exclusive` fsyncs only the file's parent directory (`/Users/gabrielgellner/dev/sdmxlib/src/sdmxlib/storage/_files.py:78-127`). The first segment creates `history/` and `history/<epoch>/`, whose own entries are never fsynced, and the first expiry is the one that deletes thousands of roots.
- `os.fsync` on macOS does not force the drive cache.
- Leaf unlinks are in `inventory/leaves/`, which no step fsyncs.
- **Fix:** say whether "crash" means process death or power loss; fsync the new directories' parents before step 4; state the macOS position.

### m5. Staged leftovers are not addressed

- A kill inside `create_exclusive` or `replace` leaves `.<name>.<pid>.<uuid>.tmp` under `history/<epoch>/` or `inventory/`, and nothing under `history/` is ever deleted.
- **Fix:** say that `list`, the orphan scan, `verify-catalog` and `doctor` skip staged names; otherwise one becomes an undecodable orphan, which Q7 makes a permanent finding.

### m6. Some rows change suite-design sentences but are routed to sdmxlib only

- **Where:** lines 194-197, 247 ("S31 to S34 change the suite design").
- S36: SUITE 627 "reports unlinked, orphan and overlapping segments".
- S37: SUITE 501-503 and 560-562, "by the storage clock" and "in storage time".
- S38: SUITE 629-631, "checks a checkpoint's leaves only".
- S39: SUITE 441 "every Inventory write run under the catalog lock", and 445 `LockFileEx`.
- E3's lock span starts earlier than SPEC §6.9 and SUITE 452 say; the design notes it (422-424) and gives it no S row.

### m7. `prune` does not say what it returns when `expire` raises after the retire commit and the directory deletions are done

- **Where:** E13 (810-816). A lock timeout would lose the report of work already done.
- **Fix:** state whether `PruneReport` carries the expiry error.

### m8. Smaller silent choices

- `LOCK`'s file mode on a catalog two OS users share.
- A fork-only child holds the lock if the parent is killed; E1's Release bullet covers only normal exit.
- A kept root is reported only in the run that processes its header, where SPEC says "reported and kept".
- A retained root unreadable at plan time: under S42 as written, roots keep being deleted while `cleaned_through` never moves and the pending set grows. Stopping before step 4 would be safer.
- After a restore that crashed between steps 5 and 6 and a seal on S+1, rerunning the same `restore --to S --apply` starts a second restore that discards that seal. Idempotency (1142-1144) covers only "while S+1 is still the head".

## S31 to S43

- S31: needed only to release the spec's "ranged GET" wording; sound.
- S32: needed; wrong as worded (M1).
- S33: the exemption and the interval refusal are needed (traced: base S is at or below the new `expired_through`); the rerun rule is wrong (B1).
- S34: see M3; no contradiction with the suite design, which is silent on embargo at restore.
- S35: needed and sound. A restore across a crashed expiry leaves pending headers; the next run steps over the restore segment, keeps reused names (S43) and rechecks leaves.
- S36: reasonable; touches suite text (m6).
- S37: touches suite text (m6).
- S38: needed (see M2's cause); touches suite text (m6).
- S39: see M5 and m6.
- S40: sound. `since_seq - 1` as target works, since S+1 is both `since_seq` and the first root of its epoch.
- S41: sound; cost unstated (M6).
- S42: needed; see m8 for plan-time detection.
- S43: needed and sound. Its dry-run sentence is unrelated and belongs in its own row.

## Open questions

- **Decidable as written:** Q1, Q3, Q5, Q7.
- **Not yet decidable:**
  - Q2 omits the Windows errno problem (M5).
  - Q4 omits the failure-after-retire case (m7).
  - Q6: see M3.
  - Q8 omits the deleted-editions case (M2), a restore with nothing above S (B1 d), and that a non-checkpoint S at or below `expired_through` is accepted when a crashed expiry left it on disk.
  - Q9: see M6.
- **Proposed new questions:**
  - What a restore does when S records editions whose files are gone.
  - Whether a pure epoch bump is supported.
  - Whether Windows is supported.
  - Whether expiry is capped per run or reads outside the lock.
  - Whether recovery of pending segments runs before new planning.

## Traced and found sound

**Expiry (process-death model):**

- Crash after step 3: only orphans exist; no root or leaf is gone. Rerun with the same, a narrower or a wider candidate set: adopts when every line is still a candidate, otherwise leaves the orphan and writes new segments; a chain of two orphans adopts the first alone when only it qualifies. A leaf shared with later candidates lands in the new header.
- Crash in step 4: old or new `HISTORY`, both handled.
- Crash in or after step 5: a root is deleted only against a matching line in a reachable segment; a failed delete keeps the root in the retained set for step 6.
- Crash in step 6 or before step 7: the rerun repeats and finds nothing.
- A seal between a crash and the rerun that skips a present leaf listed in a pending header: step 6's re-list reads the new root and the leaf survives.
- A seal cannot interleave inside a run.
- A second run with another cutoff deletes nothing wrongly (it leaks, m1).
- A restore between a crashed expiry and its rerun, to S above, below or inside the pending header's range: reused names are kept, S+1 keeps S's leaves, and the chain stays consistent even when root S itself is later deleted against its line.

**The lock:**

- `prune` cannot deadlock: one lock, never nested. `advance_current`, `opt_in` and the engine call no `try_commit`, and the restore uses `_commit_locked`.
- The gap between the retire commit and expiry is safe: expiry chooses under its own lock from the head it finds.
- Stale base against expiry: no TOCTOU window. A base above `expired_through` either is the head (kept, with its leaves) or loses the exclusive create; the base's leaves are read under the lock.
- E5 on POSIX: the 1.x fallback is safe given nothing expires on a 1.x catalog and opt-in is refused through the same mount; the cross-mount race is the one Q2 states.
- Object stores: `stage_2 = False` keeps the stage-1 decode, so a 2.x catalog is refused. There is no mutual exclusion and nothing to exclude.

**Restores:**

- The write order is safe for data: segment, then `HISTORY`, then `catalog.json`, then deletes. A crash after step 1 leaves a working catalog; after steps 2 to 4 it fails closed, checked against `_reader.py`.
- A restore does not bring back an expired leaf reference: S's leaves are verified present, S+1 names only those, and later step 6 runs recheck against S+1.
- Tombstones for editions first recorded after S hold.
- Not sound: B1, M1, M2, M3, M4.

**Code references in "The Problem" spot-checked and accurate:** `store/inventory.py:385`, `:570`, `:692`, `:747`; `admin.py:568`, `:693`; `release.py:754-760`; `gc.py:303-317`.

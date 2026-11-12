# EGM review 1: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` (lines 1-1438), revision 1 at `7538491` on `docs/gh-125-history-stage-2-design`

**Read against:** statcatlib worktree at `7538491`; sdmxlib at `v0.88.0`; suite design `inventory-history-log.md` revision 5 (hub `main` at `84615c2`).

**Reviewers:** codebase-details, comprehension, design, implementation-readiness, structural, style, zoom-out. One instance each. The per-role reports sit beside this file (`codebase-details.md`, `comprehension.md`, `design.md`, `implementation-readiness.md`, `structural.md`, `style.md`, `zoom-out.md`).

**Counts after deduplication:** 4 blockers, 22 majors, 31 minors.

## Limits of this review

- No reviewer could run a command, so nobody read the bodies of StatCat-Suite/statcatlib#125 or StatCat-Suite/sdmxlib#361. Acceptance items were checked through the design's Acceptance map, `release-job.md` "GH-125's parts" and the suite design's Handoff.
- Every finding comes from reading. No test, lock call or crash sequence was executed. The Windows errno finding (M4) rests on Microsoft's CRT documentation.
- The synthesizer did not re-verify the reviewers' evidence. B1 and M1 were each reached independently by three reviewers with matching traces; findings with one source are marked by their lens.
- Not checked by anyone: E7's table of older versions (statcatlib 0.13 to 0.17, sdmxlib 0.84/0.85, sdmxlib-rest 0.10.x), the obstore 0.12.1 `get_range` claim (R1), R10's sdmxlib-rest claim, the byte and time figures, and statcatlib `origin/main` beyond the worktree.

## Answers to the seven checks requested

1. **Expiry crash rules (E9-E12).** Sound for the deletion invariants. The design critic traced a crash after each of steps 3 to 7, mid-step crashes, a second run with another cutoff, an interleaved seal and a restore between crash and rerun. It found no sequence that deletes a leaf a retained root names, and none that deletes a root without a matching line in a segment `HISTORY` reaches. Open items: a permanent leaf leak in a recovery run with new candidates (m1), an unstated power-loss model (m4), and unspecified control flow after an early stop (M13).
2. **The lock (E1-E5).** `prune` cannot deadlock: one lock, never nested, confirmed against `admin.py:693` and `inventory.py:747-803`. The stale-base check against expiry has no TOCTOU window. On POSIX the E5 fallback is safe within the race Q2 states. Open items: the Windows errno mapping can turn contention into an unlocked commit (M4), the stale-base check misfires across a finished restore (B3), and the lock is held across gigabytes of reads (M5).
3. **Restores (E14, S32-S35, Q6).** The write order protects data, and tombstones for editions first recorded after S hold. Four things fail: the resume rule computes the wrong S (B1), a rerun after a crash in step 4 cannot rebuild the tombstones (B2), expiry stops permanently after a restore below `expired_through` (M1), and the `embargo: true` guard does not stop a discarded cancellation from releasing in three cases (M3). A restore can also make live an edition whose files `prune` deleted (M2).
4. **`file:line` references and sdmxlib 0.88 claims.** All statcatlib references are exact except six that are shifted by one to three lines (m12). Every sdmxlib 0.88 symbol the design uses as existing is public and matches. The symbols it asks for (N1-N4) are absent, as it says. One gap: E10 needs a header-only segment decoder that 0.88 does not export and N1 does not request (M12).
5. **S31-S43.** S32 and S33 are wrong as worded (M1, B1). S43 bundles two rules (M9). S31 is not a contract change (m20). S36 to S39 contradict suite revision 5 text without saying so, and the design claims it "stays inside" the contract (M6). Three contract changes have no S row (M7). S35, S40, S41 and S42 are sound.
6. **Q1-Q9.** Q1, Q3, Q5 and Q7 are decidable as written. Q2, Q4, Q6, Q8 and Q9 lack options or consequences (M3, M4, M5, M8, m24). Decisions made silently: Windows support, stale base as a retried conflict, the wider lock span, the GC stop, `verify-catalog` over a blob store, server restore, the release that first ships opt-in, a restore with nothing to discard, and a restore onto deleted editions (M2, M7, M10).
7. **Parts and `just ci`.** Parts 1 and 2 use no later symbol and run on sdmxlib 0.88. Parts 4 and 5 each need the other (B4). Part 1 implements S33's rule without a test and without waiting on S33 (M9). Crash tests are one row per operation (M17), and no injection seam exists for them (M16). Goals 6 and 9 map to no part (M18). The Acceptance map names files, never tests (m18).

## Blockers

### B1. The restore resume rule computes the wrong S, and a restore with nothing to discard cannot be written

- **Lens:** design, codebase-details, comprehension
- **Section:** S33 (line 191); E14 step 1 (857-863), step 2, crash table (884); Part 5.
- **Evidence:** "A rerun takes S from the newest `restore` segment (`first - 1`) and refuses another target" (191). Step 1 writes "One group per `restore_epoch` of the roots above S, in `seq` order, split as E10 splits", and step 2 sets `newest_segment` to the last one. `SegmentHeader.first` is the lowest `seq` in that header's `roots` (sdmxlib `_format.py:460`, `:772`).
  - Restore below `expired_through`: roots 1..420, checkpoints 1 and 300, `expired_through` 350. Restore to S=300 lists roots 351..420, so `first` = 351. After a crash following step 2 the rerun computes S = 350. `--to 300` is refused as another target and `--to 350` is refused because root 350 was expired. No supported command resumes, while writers, expiry and GC refuse and fallback readers fail closed.
  - Two epochs or more than 1,000 roots: restore #2 to S=390 writes segment A (epoch 0, 391..400) and B (epoch 1, 401..420). The newest is B, so S computes as 400. Root 400 exists and chains, so a rerun with `--to 400` passes the pre-checks and restores to the wrong version.
  - Root S+1 missing after failover lag gives `first` > S+1.
  - No root above S: `SegmentHeader.roots` has `min_length=1` (`_format.py:485`), so no `restore` segment can be encoded and step 2 has no `newest_segment` to set. The design names failover as a use of restore and does not say whether this case is refused.
  - In the first and third cases "no root of the new epoch at `first`" stays true after the restore finishes, so E15 would report a finished restore as unfinished.
- **Proposed fix:**
  1. Identify this restore's segments by walking `previous_segment` back from `newest_segment` over `restore` segments. Derive S as the highest root under `inventory/` below the lowest `first` among them. Accept `--to` only when it equals that value.
  2. Reword S33 to match, and add crash tests for two restore segments and for a target below `expired_through`.
  3. Decide, as an open question, what `restore` does when no root lies above S: refuse `to == head.seq` with `ExpiryRefusedError`, or define an epoch-only restore that writes no segment. Add the Errors row and a Part 5 test.

### B2. A restore rerun after a crash in step 4 has no stated input for root S + 1's tombstones or the report

- **Lens:** implementation-readiness, comprehension
- **Section:** E14 step 5 (868-874), crash table (881-886), `RestoreReport` (888-892), idempotent call (877-879); Goal 8.
- **Evidence:** step 5 builds `retired` from "each edition that a discarded root it could read records in a leaf". Step 4 has deleted those roots by then. The rerun row says only "deletes the rest" and "writes root S + 1 if absent". A segment line drops `products` and `retired` (sdmxlib `_format.py:490-523`) and keeps `changes`. The design does not say the rerun rebuilds from the lines, and `unread` roots have no line. Goal 8 and the GC argument at 946-948 rest on these tombstones. Also unstated: how the rerun tells an old-epoch root at `inventory/<S+1>.json` from the new-epoch root step 5 wrote at the same path; in what order step 4 deletes; what a reader with a good `CURRENT` sees during steps 4 and 5; and which rule wins after a crash between steps 5 and 6, where "returns that restore's report and writes nothing" (877) and "`advance_current`" (crash row) both apply.
- **Proposed fix:**
  1. State the rerun's inputs: tombstones and `transitions` come from the `restore` segments' lines (`changes[].leaf_sha256.to`, with leaves read from `inventory/leaves/`, which a restore never deletes). State what is lost for `unread` roots.
  2. State that step 4 skips a root at S+1 whose `restore_epoch` equals `HISTORY`'s and whose `event.kind` is `restore`.
  3. Add "readers see" and "detected by" columns to the crash table, and reconcile the idempotent return with the `CURRENT` repair.
  4. Add a test: crash mid-step-4, rerun, assert `retired` equals the uncrashed run's.

### B3. "Unfinished restore" is defined three ways, and E4's version refuses every commit that overlaps a finished restore

- **Lens:** implementation-readiness, design, codebase-details
- **Section:** E4 (451-454); E14 crash table (884); E16 GC (948-949); E9 (619-620); E15 (915, 935); Symbols (1059-1076). The predicate lands in Part 1.
- **Evidence:** E4: "`HISTORY.restore_epoch` differs from `catalog.json`'s, or the base's `restore_epoch` differs from `HISTORY`'s". E14: "the newest segment is a `restore` segment of the epoch before `HISTORY`'s with no root of the new epoch at `first`". GC, `expire`, `doctor` and `verify-catalog` have no base.
  - `commit` reads head H (epoch 0) at `store/inventory.py:747`, then waits for the lock a restore holds. The restore finishes. Under the lock `base.restore_epoch` is 0 and `HISTORY`'s is 1, so the commit raises `InventoryWriteRefusedError` naming `statcat admin restore`, which `commit` does not retry. A re-read of the head would have succeeded.
  - After restore step 4 and before step 5, `CURRENT` names a deleted root. `InventoryWriter.full()` falls back, finds no root of the new epoch and raises `InventoryError` (sdmxlib `_reader.py:194-207`). `_commit_locked` never runs, so the refusal that names the restore never appears.
  - After restore step 3 the two epochs agree, so E4's first half cannot see a crash between steps 3 and 5.
  - GC's stop sits in `expired_through()` (`gc.py:303-319`), which runs before the head is read (`gc.py:354-357`), so half the condition cannot be evaluated there.
- **Proposed fix:** define one function, for example `InventoryWriter.unfinished_restore() -> int | None` returning S, with one rule: the epochs of `HISTORY` and `catalog.json` differ, or no root of `HISTORY.restore_epoch` exists at S+1. Land it in Part 1 with a test. A base of an older epoch on a finished restore raises `StaleBaseError`, which `commit` retries. State the order of the two checks in `_commit_locked`. Have `commit` or `full()` test for an unfinished restore before the head read. Use the same function in `expire`, GC (after `full.refresh()`), `doctor` and `verify-catalog`.

### B4. Parts 4 and 5 each need the other, and the plan says either order

- **Lens:** implementation-readiness, structural, zoom-out
- **Section:** Land order (1221-1222, 1229); Part 4 (1350-1352, 1362); Part 5 (1386); E15 (915, 935).
- **Evidence:** "Parts 4 and 5 may land in either order." Part 4's adapter implements "`restore` (from Part 5)" and its test is "`run_expiry` passes every stage-2 check", where N3 (235) includes the restore cases. Part 5's test "`verify-catalog` passes across a restore, keyed by `(restore_epoch, seq)`" needs Part 4's `check_history` wiring. Part 4 also ships `doctor`'s "restore" item for a command only Part 5 has. Neither part can leave `just ci` green alone in the wrong order, and neither "Waits on" cell names the other.
- **Proposed fix:** set the order to 3, 5, 4 and move line 1386's test to Part 4; or keep 3, 4, 5 and move the conformance run and the restore-dependent `doctor` and `verify` items into Part 5. Add the dependency to "Waits on" and delete line 1229's sentence.

## Majors

### M1. S32 looks up the chain start by `seq` alone; after a restore below `expired_through`, expiry stops permanently

- **Lens:** design, codebase-details, zoom-out, style
- **Section:** S32 (190); E9 step 2 (649-655); E9 checkpoint facts (639-642).
- **Evidence:** "the hash of c - 1 is the `root_sha256` of its line in the newest `expiry` segment ... and the hash of retained root c - 1 when that segment holds no such line." Lines are per epoch (`_format.py:748-750`) and a `seq` recurs across epochs. With the catalog of B1: the first expiry wrote lines 2..299 and 301..350 (epoch 0). Restore to 300 gives epoch-1 root 301, a checkpoint. The next candidate is c=302. The newest `expiry` segment holds a line for seq 301 of epoch 0, whose hash differs from what epoch-1 root 302 chains to. The run ends in `expiry.chain_mismatch`, and every later run does the same. Nothing is deleted wrongly. The sentence is also hard to parse: "that `seq`" has no antecedent.
- **Proposed fix:** state the rule as: use retained root c - 1 when it exists; otherwise the line keyed by `(restore_epoch, seq)` on the head chain. Reword S32 the same way. Add the Part 5 test: restore to a checkpoint whose successor's `seq` is in the newest `expiry` segment, then expire.

### M2. A restore does not check that the editions root S records still have their files

- **Lens:** design
- **Section:** E14 "Before step 1" (851-855); Q8 (175); Goal 8 (140).
- **Evidence:** the pre-check covers leaves only. Local `prune` deletes "the directory of each edition tombstoned for longer than the window" (`admin.py:559-560`, `:740-769`), and spec §6.9 check 2 says a checkpoint's leaves "can name manifests GC has deleted". S records E1 `live`; later roots supersede and retire it; a prune deletes `products/P/E1`. Restore to S: root S+1 records E1 `live` with no manifest or data. S25 adds no tombstone, because S records E1 in a leaf. The dry run reports nothing about S's own editions.
- **Proposed fix:** before step 1, check each `live` and `embargoed` entry of S (manifest present, `manifest_sha256` matches) and report the superseded and withdrawn ones that are gone. Add an open question for the response: refuse, restore and list them, or retire them in S+1.

### M3. Q6 and S34: the `embargo: true` guard fails in three cases and guards a release that cannot run on a local catalog

- **Lens:** design, zoom-out, comprehension
- **Section:** Q6 (173); S34 (192); CUJ 4 (287-293); E14 step 5 and `RestoreReport` (868-874, 888-892).
- **Evidence:**
  - `release_due` is called only at `release.py:1520`, and `statcat release` takes only `az://` (`cli/__init__.py:1238-1288`; `release.py:615`). Part 5 builds the local restore only. Under option (b) every local restore turns the embargo on and every later local seal records `embargoed`, for a release run that never happens on a directory. Q6's cost line omits this.
  - `transitions` is "each discarded root's change items". An `embargo-change` root has empty `changes`. If the discarded root S+1 was an operator turning the embargo on, the report lists nothing; the operator follows CUJ 4, turns the embargo off, and the next release run releases everything due.
  - An `unread` root has no line, so a cancellation in it cannot be listed. CUJ 4 is this case: its three discarded roots are the three damaged ones, yet it says "The operator repeats the cancellations the report lists".
  - A reschedule shows as `embargoed` to `embargoed`; the report omits `release_at`.
  - Q6 uses `embargoed` (an entry state) and `embargo: true` (a root flag) in one cell and defines neither.
- **Proposed fix:** recommend Q6 (a) for the local restore and move the guard and S34 to the design that builds server restore; or keep (b) and add options (d) set `embargo: true` only when S records an embargoed entry with a non-null `release_at`, or only on a store with a release job, and (e) clear `release_at` only on entries a discarded transition touched. In either case build the report from each line's `event` and `embargo` as well as `changes`, include `release_at` and S's own `embargo` value, and state that the transitions are incomplete when any root is `unread`. Give CUJ 4 one readable discarded root and name `statcat admin embargo off`.

### M4. Windows: contention can be read as "no advisory locks", and Windows support is an undecided owner question outside Q1-Q9

- **Lens:** design, implementation-readiness, structural, zoom-out
- **Section:** E1 (335-338); E5 (467-469); S39 (197); Not confirmed (1166-1171, 1182-1184); Risks (1198); Verification Plan (1418-1420).
- **Evidence:** "`msvcrt.locking` fails with `EINVAL` or `EACCES` variants there (Not confirmed)". Microsoft documents `EACCES` as the locking-violation error `LK_NBLCK` returns when another process holds the bytes (not run). If E5 maps `EACCES` to `CatalogLockUnsupportedError`, a held lock makes a 1.x commit warn and proceed unlocked and a 2.x commit fail at once. Risks says "The owner states whether Windows is supported", and the Verification Plan offers "one manual run ... or a statement that the Windows lock is untested". S39 writes the Windows lock into the spec regardless. The design gives no platform guard for `msvcrt`/`fcntl` under pyrefly.
- **Proposed fix:** add Q10, gating Part 1: Windows supported and manually verified; best effort and documented untested; or refused with `CatalogLockUnsupportedError`. If supported, treat `EACCES` and `EDEADLOCK` as "held, retry" and classify "unsupported" by another signal. Give the import-guard idiom and the coverage pragma.

### M5. The lock is held across every read of a run, and Q9 offers only a timeout

- **Lens:** design, implementation-readiness
- **Section:** Q9 (176); E9 (616-621); E10 (664-670); E6 steps 1-4 (490-503); NFR (1124-1131).
- **Evidence:** the design's own figures: without `state_dir` each run reads every retained root under the lock ("1.7 GB each run, tens of seconds"); the first run reads 4.7 GB, deletes thousands of roots and "can exceed 120 s", during which every seal fails. Opt-in reads every root under the lock, including in its dry run, and has no NFR row. The only mitigation is a sentence on the opt-in page.
- **Proposed fix:** add options to Q9 or a new question: read retained roots and the opt-in chain before the lock, then re-list under it and read only what is new; cap candidates per run so the first expiry converges over several runs; make the wait a parameter. Add opt-in's cost to the NFR table.

### M6. The design says it "stays inside" the contract while proposing 13 rule changes, and four rows contradict suite revision 5 without saying so

- **Lens:** zoom-out, design
- **Section:** Inputs (18-20); Cross-repo contract (205-207); "The hub" (247-248); S table Owner column (189-201).
- **Evidence:** "The suite design sets the contract, and this design stays inside it." (205); line 247 says only S31 to S34 change the suite design. Against revision 5:
  - S36 against suite line 627 "reports unlinked, orphan and overlapping segments".
  - S37 (machine clock) against suite 501-503 "by the storage clock" and 560-562 "in storage time".
  - S38 against suite 629-631 "`verify-catalog` checks a checkpoint's leaves only".
  - S39 and E5 against suite 441 "every Inventory write run under the catalog lock" and 445 "`LockFileEx` on Windows".
  - S35, S40 and S41 add rules to suite sections that state the restore order and opt-in refusals in full.
  - Hub `CLAUDE.md`: "A local design that needs a contract change brings it back to the suite design first."
- **Proposed fix:** replace line 205 with: no format changes; these rule changes are proposed; Parts 3 to 5 are provisional until suite revision 6 is accepted. Mark S35 to S41 "sdmxlib, hub". Add "suite design revision 6 accepted" as a land-order row before Parts 3 and 5. Give each S row "options" and "proposed" columns, as `release-job.md`'s S table has.

### M7. Contract changes and owner decisions made with no S row and no open question

- **Lens:** zoom-out, implementation-readiness, design
- **Section:** E4 (446-450, 460-463); N3 (235); E3 (422-424); E16 (948-949); Non-Goals (156); capability table (74-75); R9 (1003).
- **Evidence:**
  - Stale base as a retried conflict. Spec §6.6 rule 5 says the writer "refuses a base version at or below `HISTORY.expired_through`" (`sharded-bundle-catalog.md:589`, `:750`). E4 raises `StaleBaseError(InventoryConflictError)`, `commit` retries, and N3 is asked to "accept `ConflictError`".
  - E3's lock span starts earlier than spec line 687 and suite 452 ("from its first leaf existence check to `CURRENT`").
  - E16 stops GC on an unfinished restore, a new §11.4 rule. S33 names writers and expiry only.
  - `verify-catalog` and `doctor` over a `BlobLocation` are a Non-Goal, while the suite Handoff lists them with no local qualifier.
  - Server restore: the capability table states "release-job Part 6 (R9)" as settled, while line 1003 says "no part builds it today".
- **Proposed fix:** add S rows for the first three (the stale-base row gates Part 1). Add open questions for `verify-catalog` over a blob store and for who builds server restore.

### M8. Restores are suite-level contract work done inside a local design, and a simpler stage-2 restore is not offered

- **Lens:** zoom-out
- **Section:** E14 (833-897); S33, S34, S35, S40, S43; Q6, Q8; Part 5 (tests 1382-1383).
- **Evidence:** five of the 13 S rows and two of the nine questions concern restores. The spec's restore text is six steps and one crash sentence (spec line 730). E14 adds the resume rule, three refusals during an unfinished restore, `cleaned_through` after a restore, orphan-`restore`-segment adoption, the floor at `since_seq - 1` and an embargo guard. S33's exemption from rule 5 and S43's reused-name rule exist only for a restore to a checkpoint below `expired_through`, which is also the case behind B1's first failure, M1 and M2.
- **Proposed fix:** add Q8 option (d): refuse a target at or below `expired_through` in stage 2, at the cost of no restore to a monthly checkpoint. It removes the rule-5 exemption, the `restoring=True` skip, S43's first half, the `seq`-reuse path and two test rows. Take the remaining restore rows to suite revision 6 first, and keep Part 5 as a provisional outline until it is accepted.

### M9. S rows gated on Part 5 hold rules that Parts 1 and 3 implement

- **Lens:** structural, comprehension, implementation-readiness, zoom-out
- **Section:** S table (191, 193, 201); land order (1218-1222); Symbols (1064); Part 1 tests (1264-1276); Part 3 test (1340).
- **Evidence:** S33 ("Gates: Part 5") includes "a stage-2 writer refuses every other commit and expiry refuses to run", which is E4 (Part 1) and E9 (Part 3). S35 is cited by E11 line 716 (Part 3). S43 is cited by E11 line 722 and E12 line 780 (Part 3), and bundles "A dry run takes no lock", which its Why cell does not cover. Part 1 lands `restoring: bool = False` and the unfinished-restore refusal with no test for either.
- **Proposed fix:** split S43 into two rows. Regate S33, S35 and S43 (or their halves) to the first part that codes them and update "Waits on" for Parts 1 and 3; or move the restore-aware branches to Part 5 and state that Part 1 to 4 writers do not fail closed during a hand-made restore. Add a Part 1 test for the unfinished-restore refusal.

### M10. The first release with opt-in ships an irreversible step before expiry, history-aware verification or a supported restore

- **Lens:** zoom-out
- **Section:** release table (1236); "No release opts a catalog in" (1240); E6.
- **Evidence:** "The release with Parts 1 and 2: the lock and opt-in." A user who opts in on that release has no expiry (Part 3), no history-aware `verify-catalog` (Part 4) and no supported restore: spec §6.1 line 356 makes the by-hand `restore_to` unsupported on 2.0, and `CatalogAdmin.restore` is Part 5. Every pre-Part-1 writer is locked out.
- **Proposed fix:** release Part 1 alone if wanted and hold `statcat admin opt-in` until the release that carries Parts 3 to 5, or land Part 2 with the command unexported. Make the choice an open question.

### M11. The design is read against stale versions: statcatlib 0.18.0 has shipped, and no planned release has a number

- **Lens:** implementation-readiness, structural
- **Section:** header (8-11); Terms (88); E6 (518-519); E7 (542); release table (1233-1238).
- **Evidence:** the design says "The next release (0.18.0) ... Part 1 too if it has landed", "0.18 if it ships before Part 1" and "0.14 to the last release before Part 1". The hub's `docs/architecture.md:153` reads "statcatlib 0.18.0", and hub `main` is `84615c2`, past the header's `4779ed5`. Part 2 must print "0.14 to <version> can read it and cannot write it" and has no number. Inferred from the hub only; the worktree's `pyproject.toml` says 0.17.0.
- **Proposed fix:** re-read against the 0.18.0 tag and current heads, update the header commits, state "stage-1 writers are 0.14 to 0.18", and number the planned releases or name who fills the number at release time.

### M12. sdmxlib gating and question gating are inconsistent and incomplete

- **Lens:** implementation-readiness, comprehension, structural
- **Section:** N1 (233); N7 (239); 241-243; Q table (168, 178-179); land order (1218-1223); Part 3 (1312); Not confirmed (1186); E10 (680-682).
- **Evidence:**
  - N7: "A release, 0.89.0, with N1 to N3." Line 242: "0.89.0 with N1 ships before Part 3 lands, and N2 and N3 before Part 4." Handoff: "Under Q1 (b) no part waits for an sdmxlib release"; the land order has Parts 4 and 5 on "sdmxlib 0.89" unconditionally.
  - "Waits on S39" and similar never say what satisfies the wait: owner accepted, merged in the spec, or released.
  - N1 to N3's signatures are "Not confirmed", yet E9, E15 and Part 4 are written to those exact names.
  - E10 decodes a segment header from `read_range` bytes. sdmxlib 0.88 exports only `decode_segment`, which needs the whole segment (`_format.py:725-764`); `_check_header` is private; N1 asks for no header decoder.
  - "The owner's answers are recorded here before Part 1 starts" is followed by per-part gates. Q1 also decides Parts 4 and 5. Q1 (b) has no module, file list or tests. Q6, Q7, Q8 and S32/S33/S35/S36/S38/S43 shape N2's and N3's cases, which sdmxlib builds before statcatlib's Part 3. Q2 binds Parts 2, 3 and 5.
- **Proposed fix:** write "the sdmxlib minor that carries N1 (expected 0.89)" and say whether N2 and N3 are in it; add the cap bump to Part 4 if not. Define "waits on an S row". Add `decode_segment_header(raw) -> SegmentHeader` to N1, or read segments whole (m21). Add a step to revise E9, E10, E14 and E15 against sdmxlib's final signatures before Part 3. State "all questions answered and S rows accepted or rejected before Part 1 starts", add a "blocks sdmxlib N row" column, and plan Q1 (b) or mark it rejected unless replanned.

### M13. `expire`'s control flow after an early stop, step 8, and the shapes of its outputs are unspecified

- **Lens:** implementation-readiness, comprehension
- **Section:** E9 step 0 (623-624), step 2 (654-655); E11 (710-716, crash rows 744-747); E12 (760-787); E10 cache (665-670).
- **Evidence:** a bad `CURRENT` "ends the run with nothing taken" and a mismatch "ends the run with nothing written". Neither says whether steps 5 to 7 still run for pending segments a crashed run left; the crash table assumes they do. Steps 0 to 8 are never listed in one place. Step 8 appears only in a heading and the row "in step 8 | the run's events may be lost", while E12 describes events emitted as steps 3 to 6 run. `ExpiryReport.stopped` has no value list. `ExpiryReport`, `OptIn` and `RestoreReport` have no field types. Only `expiry.planned` lists its payload keys, and Part 6 and Phase 8 alerting read these events. `expiry-cache.json` has no key names, no version field and no `catalog_id`, though line 966 makes its format a deliverable to Part 6. A dry run on a catalog with pending segments, a missing `CURRENT` or an unfinished restore is undefined.
- **Proposed fix:** add a nine-row step table before E9. Add a state table: stop cause, steps still run, `stopped` value, event. Give typed attrs definitions for the three reports, a key list per event and the cache schema with `catalog_id` and `restore_epoch`. State whether events are emitted per action or batched.

### M14. `ExpiryStorage` leaves error and type contracts open

- **Lens:** implementation-readiness, codebase-details
- **Section:** E8 (556-584); E11 steps 5-7 (718-736); E9 (616); Symbols (1069).
- **Evidence:** `delete` returns "`False` when the object is absent or its tag differs from `if_match`", and step 5 must tell "gone" from "changed under me". No exception type is named for a refused `delete`, `list`, `read_tagged`, `read_range` or `replace`. `expire` raises `TypeError` unless the storage "is an `ExpiryStorage`", which needs `@runtime_checkable`. `store/expiry.py` imports from `store/inventory.py`, and `InventoryWriter.opt_in` there needs `ExpiryStorage.replace`: an import cycle. E8's inherited-member list omits `write_leaf` (`store/inventory.py:339`). `read_range` on a short file and `list` ordering are unspecified.
- **Proposed fix:** give `delete` three outcomes (deleted, absent, precondition failed), name the storage error type the engine catches into `ExpiryReport.failed`, mark the protocol runtime-checkable, and place `opt_in`'s storage check, or move `opt_in` to `store/expiry.py`.

### M15. CLI output, streams, exit codes and error mapping are unspecified for every new command and flag

- **Lens:** implementation-readiness, codebase-details
- **Section:** E6 (485, 516-520); E12 (760-763); E13 (798-816); E14 (837); E15 (916-917); test rows 1301, 1343, 1388.
- **Evidence:** `statcat admin opt-in URL` and `statcat admin restore URL` accept `az://`; the method raises `TypeError`, and `_ADMIN_ERRORS` (`cli/__init__.py:839-849`) has no `TypeError`, so the user gets a traceback. `prune`'s exit code is unstated when `ExpiryReport.stopped` is set or `failed` is non-empty; today it exits 1 only on a failed deletion (`cli/__init__.py:638-640`). "`statcat prune` prints each [event] as one line" conflicts with the convention at `cli/__init__.py:1018-1019` ("human lines of a job command on standard error; standard output holds its events only") and with `prune`'s `_say` lines on stdout. The opt-in statements and restore dry-run lines have no literal text. Option types, minimums and the behaviour of `--state-dir` or `--keep-roots` without `--expire-older-than` are unstated. `prune` does not say what it returns when `expire` raises after the retire commit and directory deletions are done.
- **Proposed fix:** add one table per command with each line's format, its stream and the exit code per outcome, following the `verify-catalog` docstring. Catch the blob-storage refusal as an `InventoryWriteRefusedError` subclass. Name `_events("statcatlib.expiry")` as the helper `prune` reuses. State whether `PruneReport` carries the expiry error.

### M16. No clock or crash-injection seam is designed, though the tests and N3 need both

- **Lens:** implementation-readiness
- **Section:** E9 signature (606-613); N3 (235); Part 3 fixtures (1313-1317, 1336); Part 4 (1351).
- **Evidence:** N3 asks for `expire(*, now, keep_roots, window, stop_after=None)` and `restore(seq, *, stop_after=None)`. `expire` reads `storage.now()` and takes no `now`. `LocalInventoryStorage.__init__(self, root)` has no clock parameter (`store/inventory.py:359-362, 409-411`). Part 3's race test needs "expiry paused after step 1". The mapping from `stop_after`'s step number to the n-th write or delete is undefined.
- **Proposed fix:** add `LocalInventoryStorage(root, *, clock=None)` to Part 1 or 2. State that the adapter wraps the storage in the recording `ExpiryStorage` from `tests/_fakes.py`, and define how a step boundary is detected.

### M17. Crash-test coverage is one row per operation and omits the named crash points

- **Lens:** implementation-readiness
- **Section:** Part 3 (1331); Part 5 (1381); E11 table (738-747); E14 table (881-886).
- **Evidence:** Part 3: "A crash after each step, in a run of several segments". Part 5: "A crash after each step". Not listed as cases:
  - Expiry: between two segment creates in step 3; inside step 4; between two root deletes in step 5; between two leaf deletes in step 6; between steps 6 and 7; a second crash during recovery.
  - Restore: between two `restore` segments; between `HISTORY` and `catalog.json`; between two root deletes in step 4; between root S+1's create and `CURRENT`; orphan `restore` segment adoption (S35); the abandoned restore (883).
  - No test at all: the idempotent restore rerun; `doctor`'s and `verify-catalog`'s unfinished-restore output; opt-in, `expire` and `restore` on a lock-refusing filesystem; `expiry.delete_failed` leaving `cleaned_through` unchanged; opt-in's crashed-`HISTORY` refusal, which also has no error class.
- **Proposed fix:** parametrise both crash tests over every destructive call index of the recording wrapper, with one invariant set per index: the reader opens; no retained root names a missing leaf; the rerun reaches the uncrashed end state. Add the missing rows.

### M18. Goals 6 and 9 have no Technical Plan section, part or test, and `ExpiryStorage` goes public before its second implementer exists

- **Lens:** structural, zoom-out
- **Section:** Goal 6 (138); Goal 9 (142-143); N4 (236); Part 3 (1313-1314, 1320); Verification Plan (1428-1430); Acceptance map (1392-1411).
- **Evidence:** Goal 6 ("What was live at time T") appears only in N4 and as a test helper ("the 'live at T' replay, until N4"); the implementer has to design the replay. Goal 9's only check is "the engine's tests run once over a `MemoryStore`-backed `BlobExpiryStorage` prototype", which is in no part's file list and sits against the first Non-Goal. `etag`, `if_match`, `check()` and `read_range` are no-ops locally, yet Part 3 adds the `store/expiry.py` reference page. Goals 2, 3, 4 and 8 are covered only by the map's catch-all sentence.
- **Proposed fix:** write the replay algorithm in a short subsection. Move the `MemoryStore` prototype run into Part 3's tests in the `stores` tier, or drop it and restate Goal 9. Keep `ExpiryStorage` and `expire` out of the public API and reference docs until Part 6. Add a Goal column to the Acceptance map.

### M19. Dry runs and the lock are stated three ways

- **Lens:** comprehension, structural, codebase-details, implementation-readiness
- **Section:** E12 (779); S43 (201); E6 (490, 503); E14 (848-855); CUJ 1 (262); E1 (328).
- **Evidence:** an expiry dry run "takes no lock". Opt-in runs "in this order, under `exclusive()`" with "4. A dry run returns here", and the restore dry run is also under `exclusive()`. The first `exclusive()` creates `<catalog>/LOCK`, so a dry-run opt-in writes a file where CUJ 1 says it "writes nothing", and it fails on a lock-refusing filesystem.
- **Proposed fix:** add one table of operation by real run and dry run with columns "takes lock", "creates `LOCK`" and "on a lock-refusing filesystem". Scope S43's sentence to expiry, or align opt-in and restore with it.

### M20. Identifiers collide: step numbers, "Part", S rows, "two checks" and "opted-in"

- **Lens:** comprehension, style, structural
- **Section:** lines 6, 17, 30-35, 85, 233, 314, 415, 431, 442-454, 513, 810, 869, 955-956, 1218, 1409.
- **Evidence:**
  - Six numbered procedures all use "step N". E6 line 513 "step 1's stop" means expiry step 1 inside opt-in's own list; E16 "takes the lease in step 1 ... calls `expire` in step 7" means the release job's.
  - "Part" names GH-125's parts (line 6, capitalised), this design's Parts 1 to 5 and release-job Part 6.
  - S23, S25, S26 and E8's "Q9" are release-job identifiers written bare, against the prefix rule at 30-35.
  - "E4's two checks" and "both refusals" appear while E4 lists three bullets; a reader cannot tell whether the restore's commit skips the no-`HISTORY` refusal.
  - Terms defines an opted-in catalog as 2.x with `HISTORY`, then E4 writes "On an opted-in catalog ... **No `HISTORY`.**".
- **Proposed fix:** prefix every cross-list reference ("expiry step 1", "release-job step 7"). Add a mapping from GH-125 parts to this design's Parts and always write "GH-125 part N" and "release-job Part 6". Write "release-job S25" and "release-job Q9". Name the two skipped checks. Add "2.x catalog" to Terms and use it where the rule keys on the format.

### M21. Core vocabulary is defined neither here nor in the suite design's Terms

- **Lens:** comprehension
- **Section:** Terms (77-96) and throughout.
- **Evidence:** Terms delegates twelve terms to the suite design, whose Terms (62-98) does not define: `CURRENT` versus "head" (E9 step 1 depends on the difference), "reader fallback", "leaf", "tombstone" and `retired`, "restore epoch", `since_seq`, `cleaned_through`, `newest_segment`, "segment line" and "pending header", "seal" and "journal entry", "edition", "the fold", "gate 2", `not_before`, "the hub", "GC's cutoff".
- **Proposed fix:** extend the Terms table with one line or one spec pointer each. At minimum: `CURRENT` versus head, reader fallback, tombstone and `retired`, restore epoch, the three `HISTORY` fields, segment header and line, seal and journal, the two meanings of embargo, S as the restore target.

### M22. Seven rule sentences read two ways

- **Lens:** style
- **Section:** 169, 431, 505-509, 639-641, 720-722 with 201 and 1046, 729-731, 860-861, 884.
- **Evidence:**
  - Checkpoint rule (639-641): "or it is version 1" can attach to either clause.
  - Step 6 (729-731): "a writer that took the lease after a pause" does not say which process pauses.
  - Kept-root logging is stated four ways (201, 720, 771, 1046), and 1046 drops the condition.
  - 431: "It runs steps 2 and, best effort, 3" leaves the pre-check's behaviour undefined.
  - 505-506: "kept when it holds exactly these values and refused otherwise" names no error.
  - Q2 (169): "opt-in is refused where the lock is." reads as its opposite.
  - 860: "goes in the group of the nearest readable root below it, or of the current epoch."
- **Proposed fix:** apply the rewrites in `style.md` M2 and M4 to M10. For example, the checkpoint rule becomes: "A root is a checkpoint (H4) when any of these holds: (1) its `seq` equals `since_seq`; (2) it is version 1, or its `restore_epoch` differs from that of version `seq - 1`; (3) its `created_at` is in a later UTC month than the `created_at` of version `seq - 1`."

## Minors

Each minor names its lens; the per-role report holds the full evidence and rewrite.

1. **A recovery run with new candidates leaks leaves permanently** (design m1). E10 664-677, E11 713-716. A leaf named only by a pending root and a new candidate lands in neither header. Fix: run steps 5 to 7 for pending segments before step 1, or exclude roots matching a pending line from the "outside" set.
2. **The `state_dir` cache key omits the catalog and epoch** (design m2). E10 665-670. A restore reuses root paths, and one `--state-dir` can serve two catalogs. Fix: store `catalog_id` and `restore_epoch` and discard the file when either differs.
3. **Adoption and step 5 compare only `root_sha256`** (design m3). E10 684-688, E11 718-720. Fix: require the whole line to equal `segment_line(root, bytes)` and check each pending segment's SHA-256 before deleting from it.
4. **The crash model and durability are unstated** (design m4). Goal 5, NFR 1138-1140. `create_exclusive` fsyncs only the file's parent (sdmxlib `storage/_files.py:78-127`); the new `history/` directories' own entries and leaf unlinks are never fsynced. Fix: say whether "crash" means process death or power loss and fsync the new directories' parents before step 4.
5. **Staged leftovers under `history/` are not handled** (design m5, codebase-details m7). E10 680-683, E15 937; `_leftovers` scans the root and `inventory/` only (`diagnostics.py:569-571`). Fix: filter the orphan scan by the segment name pattern and extend `_leftovers` to `history/`.
6. **`doctor`'s `cleaned_through` warning stays true after a restore** (codebase-details m9). E15 932 against E11 710-716. Fix: let step 7 advance over trailing `restore` segments, or have `doctor` step over them.
7. **Smaller silent choices** (design m8, comprehension m7). `LOCK`'s file mode for two OS users; a forked child holding the lock after the parent is killed; a retained root unreadable at plan time under S42; a rerun of `restore --to S` after a seal on S+1 starts a second restore.
8. **`doctor`'s `LOCK` probe can create the file** (implementation-readiness m7, comprehension m11). E15 931. Fix: open without `O_CREAT`.
9. **Lock details left open** (implementation-readiness m8, comprehension m6). How a test shortens `LOCK_TIMEOUT`; the mutex around the thread table; where the once-per-process warning set lives; E3's snippet omits E5's fallback branch; `stage_2` is `True` locally even where the filesystem refuses locks.
10. **Existing helpers are re-described without being named** (codebase-details m3, implementation-readiness m10). `_stage` and `_fsync_dir` (`store/inventory.py:289-311`); `RetentionPolicy.check` (`jobs.py:217-249`) duplicates E13's two rules with another error class; `store/expiry.py` importing private `jobs._emit` reverses the layer direction.
11. **Invariant statements and per-part docs edits are missing from the Modify lists** (codebase-details m6, implementation-readiness m4, structural 10). `store/inventory.py:4-6`, `docs/catalog-format.md:48-52` and root `EGM.md:69-72` say the Inventory writer alone writes these files. Export decisions for `OptIn`, `ExpiryReport`, `RestoreReport`, `ExpiryStorage` and `expire` are unlisted. Docs obligations at 1131, 1199 and 1202 are absent from the parts' Docs bullets.
12. **Six line references are shifted** (codebase-details m10). `conformance.py:717-724` is `:715-723`; `jobs.py:170` should be `:217`; `gc.py:303-317` runs to `:319`; `gc.py:303-332` should be `:321-335`; `test_inventory_conformance.py:184-196` ends at `:197`; `admin.py:568` begins at `:567`.
13. **Part 1 breaks tests it does not list** (implementation-readiness m1, m2). `tests/test_live_cuj.py:40` asserts the catalog's file list, and Part 1 adds `LOCK`. `tests/test_inventory_history.py:324` is a third 2.x-refusal test.
14. **Messages name commands that arrive in later parts** (implementation-readiness m3). Part 1's refusal names `statcat admin restore` (Part 5); Part 2's `doctor` names "the next `prune`" (Part 3).
15. **Breaking parts use `feat:`** (implementation-readiness m5). Line 1228; `cliff.toml:36` groups only `!` commits under "Breaking Changes". Fix: `feat!:` for Parts 1 and 3.
16. **`InventoryWriter.opt_in` has no signature, and engine defaults differ from the admin methods** (implementation-readiness m11, comprehension m10). Symbols 1069, 1075; `dry_run=False` on the functions and `True` on `CatalogAdmin`.
17. **GC's Part 5 test can only use a hand-built state** (codebase-details m1). `gc.py:75` works over `BlobInventoryStorage` only, and blob restore is R9.
18. **The Acceptance map names files, never tests** (implementation-readiness m9). Row 1 cites `tests/stores/test_blob_conformance.py` for stage 2 while the blob adapter keeps `stage_2 = False`. The "without deadlock" test has no timeout.
19. **"Each has a risk row or a test that will settle it" is false for three "Not confirmed" items** (structural 5). 1163-1186. The `{"from": null, "to": null}` change item is accepted by 0.88 decode and schema (`_rules.py:634-666`, `_format.py:839-880`); it needs a sdmxlib test and a handoff row.
20. **S31 is not a contract change** (zoom-out m1). The ranged read is a property of field order (spec line 370). Fix: drop S31 and keep Q3 as a local choice.
21. **Three mechanisms are over-built for local stage 2** (zoom-out m3, m4, m5). Orphan `restore` segment adoption and the rebuilt report (S35, 861-863, 877-879); `--state-dir` on `prune`; `read_range` with length doubling. Fix: write a fresh `restore` segment on rerun, return `already` on a repeat call, read segments whole, and add the flag only if Part 3's bench shows the need.
22. **Goal 1 contradicts E5** (zoom-out m6). "Every Inventory commit on a local catalog runs under the catalog lock" against the unlocked 1.x commit. Fix: reword to "wherever the filesystem locks" and give Q2 an option (c), never lock 1.x catalogs.
23. **Part 6 material and unowned handoffs** (zoom-out m8, m9; structural 11, 13). The Azure primer and RBAC paragraph (970-988) belong in the R table; N4 "blocks nothing"; Handoff has no sdmxlib-rest subsection though R10 needs an sdmxlib-rest release.
24. **Question table gaps** (zoom-out m10, structural 6, 16; design). Q4 and Q8 recommend (a) with no reason. The "Changes" and "Settles" columns disagree. The Non-Goal "A restore of a 1.x catalog (Q8)" and "release-job Part 6 (R9)" are stated as settled. Q4 omits the failure-after-retire case.
25. **"Each decision carries its own table" is false, and events and errors dangle** (structural 8, 9). E2, E5, E7, E11, E12, E15 and E16 have no options table. `restore.planned`, `restore.discarded` and `restore.tombstoned` are never placed in E14. The Errors table omits `opt_in`'s `InventoryError` and both `ValueError`s.
26. **Figures for one corpus use two bases, and estimates read as facts** (comprehension m4, structural 14, style m12, m13). 646 "2,979 roots ... 4.7 GB" (1.6 MB a root) against 1128 "1.7 GB" (560 KB a root). "a few milliseconds", "under a second" and "40 lines" are unmeasured.
27. **CUJ gaps** (comprehension m1, m2). CUJ 2 says the seal reads "the head's bytes" where E3 reads the base's; CUJs 2 and 3 show no printed line or exit status.
28. **`doctor` checks segment hashes "from the headers alone"** (comprehension m5). E15 934. A hash check needs the whole segment. N1's "three cases" against E9's four (comprehension m3).
29. **An unresolvable reference** (structural 17). "sdmxlib's plan, line 431" (151) has no path or commit in Inputs.
30. **Headings and table introductions break the title rule** (style m1, m2). "E3: what `try_commit` holds the lock over", "E17: where the pure rules live", the five CUJ headings, "What other code reads of its output:". Heading case is mixed.
31. **Remaining prose findings** (style m3 to m11, m14 to m16; structural 18; zoom-out m11). Negative form in nine places; fifteen passives that hide the actor; words with several meanings ("owner", "land", "gate", "window", "expire", "holds", "S", "c"); `StatCat-Suite/statcatlib#126` should be `GH-126` (1121, 1421); undefined GC, EGM, SAS, RBAC, ACL and "F14"; cells of 100 to 200 words in N1 and N3; "hash" used as a verb for equality; idioms ("slept through", "for good", "meets"); "(Not confirmed)" inline; mixed code-fence languages and bullet markers.

## What the reviewers confirmed

- Every statcatlib `file:line` reference other than the six in minor 12 is exact, and every file a part marks Modify exists.
- Every sdmxlib 0.88 symbol the design treats as existing is public in `sdmxlib.inventory.__all__` with the signature the design uses. N1 to N4's symbols are absent in 0.88, as the design says.
- None of S31 to S43 contradicts shipped 0.88 code.
- Section set and order match `release-job.md`; E1-E17, S31-S43, Q1-Q9, N1-N7, R1-R10 and CUJ 1-5 are contiguous and every citation resolves; the design is in `SITEMAP.md` and the mkdocs nav.
- Parts 1 and 2 use no symbol from a later part and run on sdmxlib 0.88; no test double implements `InventoryStorage`.
- The expiry, lock and restore sequences listed under "Traced and found sound" in `design.md`.

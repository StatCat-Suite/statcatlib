# Comprehension review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (1438 lines, read in full)
**Reviewer lens:** `egm-comprehension` (naive reader)
**References consulted by search only:** the suite design's Terms and Restores sections, spec §6.9, the release-job design's S/D/Q rows.

Verdict: 1 blocker, 12 majors, 11 minors. The lock sections (E1 to E5) and the expiry crash table (E11) mostly answer who holds the lock and what is on disk. The restore resume rule, the step numbering and the dry-run lock rules leave those questions open.

## Blocker

### B1. Restore rerun derives S from the wrong segment when a restore writes more than one segment

- **Where:** E14 step 1 (857-863), E14 crash table (884), S33 (191).
- **Evidence:**
  - Step 1 allows several segments: "One group per `restore_epoch` of the roots above S, in `seq` order, split as E10 splits."
  - Step 2 sets "`newest_segment` the last `restore` segment".
  - The rerun rule: "finds the newest segment is a `restore` segment ... takes S as `first - 1`".
  - S33 repeats it: "A rerun takes S from the newest `restore` segment (`first - 1`)".
- **Problem:** `first - 1` of the newest segment equals S only when the restore wrote exactly one segment. It is wrong with more than 1,000 discarded roots (the newest segment's `first` is S + 1001 or higher) and with discarded roots spanning two epochs (the newest segment's `first` is the first root of the later epoch). The suite design (Restores, line 580) says "a `restore` segment", singular, so the multi-segment form is new here and has no stated resume rule.
- **Fix:** state that the rerun walks `previous_segment` back through the contiguous `restore` segments this restore wrote and takes S from the oldest one's `first - 1`. Say how it tells this restore's segments from an earlier restore's. Add a crash test with two restore segments, and correct S33.

## Major

### M1. Steps 0 to 8 are never listed, and step 8 has no stated action

- **Where:** Overview (52), Terms "Engine" (89), E9 to E12, E11 crash row "in step 8" (747).
- **Evidence:** the document refers to "steps 0 to 8 of spec §6.9" throughout, and the step contents are scattered through E9 to E12 prose. Step 8 appears only in the E12 heading and the crash row "in step 8 | the run's events may be lost". E12 describes events as a stream emitted while steps 3 to 6 run (`expiry.deleted`: "Each object deleted"), which leaves no separate step 8 to crash in.
- **Fix:** add a nine-row table (step, one-line action, section) before E9. State whether events are emitted as each action happens or batched at the end, and rewrite the "in step 8" row to match.

### M2. Five numbered lists are all called "step N", and cross-references do not say which list

- **Where:** 513, 431, 452, 728-729, 955-956, 193.
- **The lists:** expiry steps 0-8, `_commit_locked` steps 1-7 (E3), opt-in steps 1-6 (E6), `prune` steps 1-3 plus "its three steps as today" (E13), restore steps 1-6 plus "Before step 1" (E14), and the release job's steps 1 and 7 (E16).
- **Evidence:**
  - E6, inside the opt-in list, line 513: "step 1's stop at a root without the history fields" means expiry step 1. Opt-in's own step 1 is the storage refusal.
  - E16, 955-956: "takes the lease in step 1 ... and calls `expire` in step 7" means release-job steps, written beside expiry steps 1 and 7.
  - S35: "step 3's link rule" sits in a row about restores and means expiry step 3.
  - E3, 431: "It runs steps 2 and, best effort, 3" means E3's steps.
- **Fix:** prefix every cross-list reference ("expiry step 1", "restore step 2", "release-job step 7"), or give the non-expiry lists letters.

### M3. "Part" means three different series

- **Where:** header (6), Overview table, Land order table (1216-1223), Acceptance map (1409-1412).
- **Evidence:** line 6: "GH-125 parts 2 and 3. Part 1 shipped in 0.14.0 and part 4 in 0.17.0." Here capital "Part 1" is GH-125's part 1. From line 61 on, "Part 1" to "Part 5" are this design's. "Part 6" is the release-job design's, and sits in the land-order table as the row after Part 5. Line 1409: "GH-125's part 2 and part 3 bullets ... map to Part 1 ... Part 5".
- **Fix:** add one sentence and a mapping table. GH-125 part 2 maps to Parts 1 and 2; GH-125 part 3 maps to Parts 3, 4 and 5. Always write "GH-125 part N" and "release-job Part 6". Fix the capital in line 6.

### M4. Bare S23, S25, S26 and one bare Q9 break the document's own prefix rule

- **Where:** prefix rule (30-35); uses at 17, 233, 339, 668, 314, 872, 947, 1003.
- **Evidence:** the rule says "The release-job design's own identifiers are written with its name", and "S31 to S43 continue the S series ... which ends at S30". S23, S25 and S26 are rows of the release-job design (its lines 211-214), yet are written bare: "S25's tombstones", "(S23)", "the figure S26 gives". Line 17 says revision 5 of the suite design "adds S23, S25 and S26", which reads as if they are suite-design identifiers. None of the three is glossed where first used. E8's Settles cell (314) reads "release-job D13, Q9"; that Q9 is the release-job's (does Part 6 stay there), and this design has its own Q9 (the lock wait).
- **Fix:** write "release-job S23/S25/S26" or add a three-row gloss (S23: the leaf-set cache persists and holds hashes only; S25: root S + 1 carries tombstones for editions only discarded roots recorded; S26: a 120 s bounded wait for the Azure lease). Write "release-job Q9" in E8.

### M5. Core vocabulary is used but defined neither here nor in the suite design's Terms

- **Where:** Terms (77-96) and throughout.
- **Evidence:** Terms delegates twelve terms to the suite design. Its Terms section (lines 62-98) does not define the following, and neither does this document: `CURRENT` and "head" (and that `CURRENT` can lag the head, which E9 step 1 depends on); "the fallback" / "reader fallback" (CUJ 3, E7, E11); "leaf"; "tombstone" and `retired`; "restore epoch"; `since_seq`, `cleaned_through`, `newest_segment`; "segment line", "header", "pending header"; "seal" and "journal entry"; "edition"; "the fold", "pod check", "gate 2", "expiry gates", `not_before`; "the hub" (used as a repository, a session and an owner); "GC's cutoff" (E16, 945); "EGM".
- **Fix:** extend the Terms table with one line each, or a pointer to the spec section. At minimum: `CURRENT` versus head, reader fallback, tombstone and `retired`, restore epoch, the three `HISTORY` fields, segment header and line, seal and journal, hub.

### M6. "Embargo" means two things, and Q6 cannot be read without knowing which

- **Where:** Q6 (173), S34 (192), E14 step 5 (874), CUJ 4 (292-293).
- **Evidence:** Q6 uses "`embargoed`" (an entry's state), "`release_at`", "`embargo: true`" (a root-level switch) and "a local seal records `embargoed` until then" in one cell. Neither meaning is defined. Option (b)'s cost only makes sense once the reader knows the root flag changes what a seal records, which is stated only in the release-job design's Q1 and `docs/catalog-format.md:620`. CUJ 4's "turns the embargo off" does not name the command; Q6 names `statcat admin embargo off`.
- **Fix:** define "entry state `embargoed`" and "the root's `embargo` flag (the stop switch, release-job Q1)" in Terms. Name the command in CUJ 4.

### M7. Dry runs and the lock are stated three different ways

- **Where:** E12 (779), S43 (201), E6 (490, 503), E14 (848-855), CUJ 1 (262), CUJ 4 (289-290), E1 (328).
- **Evidence:**
  - E12: an expiry dry run "takes no lock".
  - S43 states "A dry run takes no lock" with no subject, in a row about restore crashes.
  - E6: opt-in "runs ... in this order, under `exclusive()`" and "4. A dry run returns here", so the opt-in dry run holds the lock.
  - E14: the restore runs "Under `exclusive()`" and "A dry run reports and returns here", so the restore dry run holds it too.
  - E1: `exclusive()` creates `LOCK` on first call. CUJ 1 says the opt-in dry run "writes nothing"; on a catalog that has never committed under Part 1 code, it creates `LOCK`.
  - E6 step 1 is listed under the lock but includes "`CatalogLockUnsupportedError` from the lock", so it is unclear whether step 1 runs before or after the lock is taken.
- **Fix:** one table of operation by real run and dry run, with columns "takes lock", "creates LOCK", "behaviour on a lock-refusing filesystem". Give S43 a subject.

### M8. S43 and S33 gate Part 5, but Parts 1 and 3 implement text from them

- **Where:** S43 (201), land order (1220, 1222), Part 3 test (1340), E4 (451-454), Part 1 content (1218), Symbols (1064).
- **Evidence:** S43 ("A dry run takes no lock"; a kept root logs nothing after a restore) gates Part 5. E12 and Part 3's test "A dry run writes nothing, takes no lock" depend on it, and Part 3's "Waits on" omits S43. E11 step 5 (S43) and the pending walk (S35) are Part 3 code. E4's unfinished-restore refusal and the `restoring` parameter are Part 1 ("both refusals"; Symbols row Part 1), and the rule that defines them, S33, gates Part 5. No Part 1 test covers that refusal.
- **Fix:** split the dry-run sentence out of S43 into a Part 3 row. State in which part the unfinished-restore check lands and which test covers it there.

### M9. "E4's two checks" and "both refusals" are ambiguous, because E4 lists three

- **Where:** E3 step 3 (415), E4 (444-454), E14 step 5 (869), Part 1 (1218, 1248).
- **Evidence:** E4 has three bullets (no `HISTORY`, stale base, unfinished restore). The text then says "run E4's two checks", "skips E4's two checks" and "both refusals". The Overview's Part 1 row names only "the stale-base refusal". A reader cannot tell whether the restore's commit also skips the no-`HISTORY` refusal.
- **Fix:** name the two skipped checks (stale base, unfinished restore) and say the no-`HISTORY` refusal always applies.

### M10. Restore crash table: reader state and resume detection are not derivable for steps 4 to 6

- **Where:** E14 crash table (881-886), 877-879.
- **Derivable:** before step 2, an orphan segment and a working catalog; after steps 2 or 3, the epochs differ (or the base's epoch differs), so E4 refuses writers.
- **Not derivable:**
  - Step 4: `CURRENT` names a root being deleted. The table says only "some discarded roots deleted". It does not say what a reader with a previously good `CURRENT` sees during steps 4 and 5, or in what order roots are deleted (head first or S + 1 first). The "fail closed" sentence covers only "readers whose `CURRENT` fails".
  - "resumes at the first step not done": no rule for how the rerun detects steps 3 or 4 as done. The rerun's "Before step 1" reads "the roots above S", which are partly gone.
  - Steps 5 and 6 against line 877: "When the head is root S + 1 of kind `restore` ... a call with the same `to` returns that restore's report and writes nothing". The crash row says the rerun does "`advance_current`". If the crash fell between steps 5 and 6, both rules apply and disagree.
  - How the rerun or the idempotent call rebuilds `RestoreReport.transitions` after the discarded roots are deleted, and whether an `unread` root has a segment line at all.
- **Fix:** add a "readers see" column and a "detected by" column to the table. Reconcile the idempotent return with the `CURRENT` repair. State the source of the report on a rerun.

### M11. Expiry: whether pending-segment recovery runs when steps 0 to 2 stop the run

- **Where:** E9 (624, 654-655), E11 (710-716), crash table (744-746).
- **Evidence:** "A `CURRENT` that is missing or malformed after the step ends the run with nothing taken." "A mismatch ends the run with nothing written". "A run with no candidate skips steps 3 and 4."
- **Problem:** after a crash in step 5 or 6, the next run must finish pending segments. The text does not say whether a run that stops at step 0, or at step 2 on a chain mismatch, still performs steps 5 to 7 on segments already pending. "Ends the run" suggests it does not; the crash table assumes it does. Smaller gaps in the same rows: step 5 needs each line's `root_sha256`, so it must read pending segments whole, and neither E11 nor the NFR table says so; the `state_dir` cache file appears in no crash row; step 5 does not say how the engine tells "a later epoch's root has taken the name" from a mismatching root (by the root's `restore_epoch`, or by `seq > expired_through`).
- **Fix:** say explicitly that a stop in steps 0 to 2 still runs steps 5 to 7 over pending segments, or the opposite. Add the cache file and the whole-segment read to the tables.

### M12. Q1 (b) and N3: the handoff and the land order disagree

- **Where:** 235, 241-243, 1221-1222, 239.
- **Evidence:** Handoff: "Under Q1 (b) no part waits for an sdmxlib release", and N3 blocks "Part 4's conformance test; no other part". Land order: Part 4 "sdmxlib 0.89 | Waits on ... N2, N3" and Part 5 "0.89", both unconditional; only Part 3 says "under Q1 (a)". Part 4's content includes "the stage-2 conformance run". N7 says 0.89.0 carries "N1 to N3". Line 242 says "0.89.0 with N1 ships before Part 3 lands, and N2 and N3 before Part 4", which reads as possibly a later release, against the cap `<0.90`.
- **Fix:** mark Parts 4 and 5 "under Q1 (a)" in the land-order table. State whether Part 4 lands without its conformance test if N3 is late. State whether N2 and N3 are in 0.89.0 or a later 0.89.x.

## Minor

### m1. CUJ 4's scenario cannot produce the transitions it tells the operator to repeat

- **Where:** 287-293, 888-892.
- **Evidence:** all three discarded roots are damaged ("marks the unreadable ones `unread`"). The CUJ still says the dry run prints "the transitions they hold" and "The operator repeats the cancellations the report lists". Unread roots yield no transitions and no tombstones (E14 step 5: "a discarded root it could read").
- **Fix:** make one root readable in the CUJ, or state that the report says what could not be recovered.

### m2. CUJ 2 names the wrong object, and CUJs 2 and 3 show no observable output

- **Where:** 273-275, 277-283.
- **Evidence:** CUJ 2 says the seal "reads `HISTORY` and the head's bytes". E3 step 4 reads "the base's stored bytes", and a stale base raises `StaleBaseError` and is retried (E4). The CUJ omits that path. Neither CUJ gives a printed line or an exit status.
- **Fix:** say "base", mention the retry, and add what the user sees (for example the `expiry.deleted` lines and exit 0 on the second `prune`).

### m3. Checkpoint rule: three cases or four

- **Where:** E9 (639-641), N1 (233).
- **Evidence:** N1 says "H4's three cases". E9 lists `since_seq`, an epoch change, "or it is version 1", and a later UTC month.
- **Fix:** say that version 1 is the first root of epoch 0, as H4 does.

### m4. Byte figures use two bases for the same corpus

- **Where:** E9 (646), NFR (1128).
- **Evidence:** "A first run over 2,979 roots therefore reads their 4.7 GB" uses 1.6 MB a root. "a catalog that opted in at version 2,979 reads 1.7 GB each run" uses 560 KB a root.
- **Fix:** state the product count with each figure.

### m5. `doctor` checks segment hashes "from the headers alone"

- **Where:** E15 (934, 925).
- **Evidence:** "each segment on the chain whose link or hash fails, from the headers alone", alongside "none walking the whole history". Checking a segment's hash needs its whole bytes, and listing every orphan needs every header.
- **Fix:** say what `doctor` actually reads and limit the claim to links.

### m6. The local `stage_2` value contradicts its definition on a lock-refusing filesystem, and E3's code omits E5's path

- **Where:** E2 (383, 388), E5 (469-474), E3 (404-406).
- **Evidence:** "`stage_2` says whether `exclusive()` excludes expiry", and local is always `True`. E5 says "`try_commit` then reads `catalog.json` without the lock", but the `try_commit` shown is only `with self._storage.exclusive():`.
- **Fix:** show the fallback branch in E3, and say `stage_2` means "can lock where the filesystem allows".

### m7. A killed holder with a forked child

- **Where:** E1 (329-330, 343-344), Goal 1.
- **Evidence:** "The OS drops the lock when the process exits". The Release bullet admits a forked child shares the open file description. If the holder is killed before `LOCK_UN`, a surviving child keeps the lock. The text does not say whether Goal 1 covers this.
- **Fix:** state the limit, or open with close-on-exec and say fork without exec is unsupported.

### m8. Q2's recommendation sentence is unreadable

- **Where:** 169.
- **Evidence:** "opt-in is refused where the lock is." It presumably means "where the lock is unavailable". The option (a) cost cites "the race spec §6.1 already accepts for stage-1 writers" without saying what the race is.
- **Fix:** complete the sentence and describe the race in one clause.

### m9. Version, root and `seq` are used interchangeably

- **Where:** for example 260, 501, 639.
- **Evidence:** "400 Inventory versions", "a catalog with no version", "root N", "version 1".
- **Fix:** one Terms line: an Inventory version is root `inventory/<seq>.json`, and the two words are synonyms.

### m10. Engine-level defaults differ from the admin methods without comment

- **Where:** 611, 793, 836, 1075.
- **Evidence:** `expire(dry_run=False)` and `restore(writer, to, *, reason, dry_run=False)` in `store/expiry.py`, against `CatalogAdmin.prune` and `CatalogAdmin.restore` with `dry_run=True`.
- **Fix:** one sentence saying this is intended.

### m11. `doctor`'s LOCK probe on an absent file

- **Where:** 931.
- **Evidence:** the states are "absent; present and free; or held", found "with a non-blocking attempt". It is not stated whether the probe creates `LOCK`, which `O_CREAT` would do, although `doctor` is "all read-only".
- **Fix:** state that the probe opens without `O_CREAT`.

## Coverage against the seven priorities

1. Terms: M5, M6, m9, m3. "Root", "retained root" and "checkpoint" resolve through the suite design. `CURRENT` and head, fallback, tombstone, embargo, epoch, cutoff and hub resolve nowhere a pointer leads.
2. Naming and numbering: M2, M3, M4. Q1 to Q9, E1 to E17, N1 to N7, R1 to R10 and S31 to S43 are each contiguous with no skips.
3. Crash rules: in E11 the on-disk state is clear for steps 0 to 7; the next run's behaviour is unclear when it stops early (M11), and step 8 is undefined (M1). In E14 the state is clear before step 2 and after steps 2 or 3; it is unclear for step 4 and steps 5 and 6, and the resume rule is wrong for multi-segment restores (B1, M10).
4. Lock: the holder and the second process's view are clear for commit, `prune` and expire (a 50 ms poll, 120 s, then `CatalogLockTimeoutError`; the same thread gets `RuntimeError`). Unclear for dry runs (M7) and the lock-refusing path in code (m6). Fairness under polling is not stated.
5. CUJs: 1 and 5 are concrete and agree with the plan. 2, 3 and 4 have the gaps in m1 and m2.
6. Open questions: Q1, Q3, Q4, Q5, Q7, Q8 and Q9 read alone. Q2 (m8) and Q6 (M6) do not.
7. Contradictions: M7, M8, M9, M12, m4, m5.

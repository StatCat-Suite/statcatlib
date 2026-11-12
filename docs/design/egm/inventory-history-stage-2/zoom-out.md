# Zoom-out review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (revision 1)
**Reviewer lens:** `egm-zoom-out` (simplicity and scope)

Verdict: 0 blockers, 8 majors, 11 minors. Context is sufficient and the design addresses the root problem. The lock and the engine core are close to the simplest thing that meets the contract. The weight is in restores (5 of the 13 S rows, plus Q6 and Q8) and in a contract-change process that the design describes as "staying inside" the contract.

**Read in full:** the design, the zoom-out persona and standard, suite design revision 5, spec §6.9 (lines 633-757), the hub `CLAUDE.md` "Suite designs" section, and `release-job.md`'s S table, D13, "GH-125's parts" and Part 6.

**Not checked:** the GH-125 issue body (the reviewer relied on the suite design's Handoff and release-job "GH-125's parts"), the design's `file:line` citations into `src/`, sdmxlib's code, and anything on Azure.

**Reuse search:** `src/statcatlib` has no existing lock utility (no `flock`, `msvcrt`, `filelock` or `portalocker`), so `store/lock.py` duplicates nothing. `jobs._emit` exists at `jobs.py:665` as cited.

## Major

### M1. The design says it stays inside the contract while proposing 13 changes to it, and the hub rows are under-counted

- **Where:** Inputs (lines 18-20), Cross-repo contract (205-207), "The hub" (247-248), S table Owner column (189-201).
- **Evidence:**
  - Line 205: "The suite design sets the contract, and this design stays inside it." Line 19: "This design links it and changes none of it."
  - Hub `CLAUDE.md:68-69`: "A local design that needs a contract change brings it back to the suite design first."
  - Line 247 says only S31 to S34 change the suite design. At least three more rows contradict revision-5 text without saying so:
    - **S37** (machine clock; W at least `prune`'s window plus a day) against suite step 1 "older than the retention window W by the storage clock" (line 503) and Bounds "both measured ... in storage time" (560-562).
    - **S38** (non-head retained roots get leaf checks only) against suite lines 629-631 "`verify-catalog` checks a checkpoint's leaves only" and check 2.
    - **S39 / E5** (an unlocked commit on a 1.x catalog; `LockFile`) against suite line 441 "every Inventory write run under the catalog lock" and line 445 "`LockFileEx` on Windows".
  - S35, S40 and S41 add rules to suite sections (Restores, Catalogs that expire) that state the restore order and the opt-in refusals in full.
- **Why it matters:** the format claim ("changes no format") is true; the contract claim is false. The S rows change writer rules, the restore order and `verify-catalog` semantics, which the suite design's Cross-repo contract gives to sdmxlib and the hub.
- **Fix:**
  1. Replace the line-205 sentence with: no format changes; these rule changes are proposed; Parts 3 to 5 are provisional until revision 6 is accepted.
  2. Mark S35, S37, S38, S39, S40 and S41 "sdmxlib, hub".
  3. Add "suite design revision 6 accepted" as an explicit row in the land order before Parts 3 and 5, as release-job handoff row 12 does.
  4. Give each S row the "options" and "proposed" columns `release-job.md`'s S table has.

### M2. Restores are being suite-designed inside a local design

- **Where:** E14 (833-897), S33, S34, S35, S40, S43, Q6, Q8, Part 5.
- **Evidence:** five of the 13 S rows and two of the nine questions concern restores. The spec's restore text is six steps and one crash sentence ("a rerun resumes from the epoch in `HISTORY`", spec line 730). E14 invents the resume rule (S from `first - 1`); the writer, expiry and GC refusals during an unfinished restore; `cleaned_through` after a restore; orphan-`restore`-segment adoption; the floor at `since_seq - 1`; and an embargo guard.
- **Why it matters:** this is contract work on the shared crash rules and restore order, done at local altitude with full test tables against text nobody has accepted.
- **Fix:** take the restore rows to revision 6 first. Keep Part 5 here as a provisional outline (the pattern release-job used for its Part 6), or move it to a follow-up design. Parts 1 to 4 then need only S32, S36 to S39, S41 and S42.

### M3. A simpler restore: refuse a target at or below `expired_through` in stage 2

- **Where:** Q8 (175), S33 first sentence, S43 first sentence, E14, Part 5 tests at 1382-1383.
- **Evidence:** S33's exemption from rule 5 and S43's "a later epoch's root has taken the name" exist only for a restore to a checkpoint below `expired_through`. The CUJ (lines 287-293) and the suite design's motive (failover) target roots near the head, which K and W retain. A root older than W can name editions whose files GC or `prune` has already deleted (E16: W is at least the GC window plus a day; S38 concedes this for retained roots), so such a restore yields a head naming missing data.
- **Fix:** add Q8 option (d): refuse S at or below `expired_through`, with the cost "no restore to a monthly checkpoint". Recommend it for stage 2. It removes the rule-5 exemption, the `restoring=True` skip of the stale-base check, S43's first half, the `seq`-reuse path in step 5, and two test rows. The spec's "smaller of its old value and S" stays valid for a later design.

### M4. S32 is not precise enough to write spec text from, and as worded appears wrong after a restore below `expired_through`

- **Where:** S32 (190), E9 step 2 (649-655), E9's checkpoint facts (639-642).
- **Evidence:** S32 takes "the line of that `seq` in the newest `expiry` segment reached from `newest_segment`", with no epoch. Traced by hand (no test covers it):
  - Restore to checkpoint S below old `expired_through` E. The new epoch's S + 1 is a checkpoint, so the lowest candidate is S + 2 and its predecessor is the retained new-epoch root S + 1.
  - The old epoch's S + 1 was expired and can sit in the newest `expiry` segment.
  - S32's wording picks that discarded-branch line, the hash mismatches, and expiry stops for good.
  - E9's "the fact about `seq - 1` comes from the retained root, or from its segment line" has the same ambiguity.
- **Fix:** state the rule as: the retained root at `seq - 1` when one exists, otherwise the line keyed by `(restore_epoch, seq)` on the head chain. M3 removes the case entirely.

### M5. S34 / Q6 (b) solves a server problem in a local-only restore

- **Where:** Q6 (173), S34 (192), E14 step 5, CUJ 4 (292-293).
- **Evidence:** Q6's hazard is "released by the next release run". The release job takes a `BlobLocation` only (`src/statcatlib/release.py:615`), and this design's restore is local only; server restore is R9, unbuilt. Locally, option (b) costs a mandatory `statcat admin embargo off` after every restore and embargoed seals until then, for a release run that never happens on a directory.
- **Fix:** recommend Q6 (a) here, with `RestoreReport.transitions` as the operator's list. Move the guard and S34 to whichever design builds server restore, where the hazard exists and the owner can weigh it with release-job Q1.

### M6. The release cut ships an irreversible opt-in before anything that benefits from it or can restore it

- **Where:** release table (1236), "No release opts a catalog in" (1240), E6.
- **Evidence:** "The release with Parts 1 and 2: the lock and opt-in." After opting in on that release a user has no expiry (Part 3); no history-aware `verify-catalog` (Part 4); no supported restore (spec §6.1 line 356 makes the by-hand `restore_to` unsupported on 2.0, and `CatalogAdmin.restore` is Part 5); and every pre-Part-1 writer locked out.
- **Fix:** release Part 1 alone if wanted (it is inert for 1.x catalogs), and hold `statcat admin opt-in` until the release that carries Parts 3 to 5. Alternatively land Part 2's code with the command unexported. Either way, make it an open question.

### M7. Decisions made silently that should be open questions

- **Windows support.** The Risks row (1198) says "The owner states whether Windows is supported", and the Verification Plan (1419-1420) offers "or a statement that the Windows lock is untested". It is absent from Q1 to Q9, yet S39 changes the spec's `LockFileEx` wording for it, and four "Not confirmed" items hang on it. No repo doc or CI config searched claims Windows support. If the answer is no, drop the `msvcrt` branch and S39's Windows sentence, and raise `CatalogLockUnsupportedError` there.
- **E4: the stale base as a retried conflict.** The spec and suite design say the writer "refuses" (spec line 750). E4 makes `commit` succeed silently on the new head and asks N3 to accept `ConflictError`. That changes the semantics of a conformance case, and it has no S row and no question.
- **E3: the wider lock span.** The spec (line 687) and suite design (452) say "from its first leaf existence check to `CURRENT`". The design starts earlier. That is reasonable, and it has no S row.
- **E16: GC stops on an unfinished restore** (`GcStoppedError`, `gc.py`). This is a new §11.4 rule. S33 names writers and expiry only.
- **`verify-catalog` and `doctor` over a `BlobLocation` as a Non-Goal** (156, capability table line 74 "Not planned"). The suite Handoff lists "`verify-catalog` and `doctor` reporting" with no local qualifier, and the catalogs that expire at scale are on Azure. N2's `check_history(fetch, ...)` is store-agnostic, so the exclusion concerns the command only. The owner should decide this explicitly.
- **Server restore (R9).** The capability table (75) states "release-job Part 6 (R9)" as settled, while line 1003 says "no part builds it today". The suite's "restores that bump the epoch" has no owner on the only store with failover. Make it a question or a numbered handoff row.

### M8. Goal 9 has no part and no test, and the public interface ships before its second implementer exists

- **Where:** Goal 9 (142), E8, Verification Plan last bullet (1428-1430), release table (1237), Part 3 docs ("`docs/reference/` gains the `store/expiry.py` page").
- **Evidence:** the only check that `ExpiryStorage` suffices for Part 6 is "the engine's tests run once over a `MemoryStore`-backed `BlobExpiryStorage` prototype", which has no part, file or owner and is scheduled after Parts 1 to 5 release. The protocol is shaped by Azure needs: `etag`, `if_match`, `check()` and `read_range` are no-ops or ignored locally (E8 table: "`if_match` is ignored").
- **Fix:** move the `MemoryStore` prototype run into Part 3's test list, in the `stores` tier. Keep `ExpiryStorage` and `expire` out of the public API and the reference docs, marked provisional, until Part 6 lands.

## Minor

### m1. S31 is not a contract change

- **Where:** S31 (189), Q3.
- **Evidence:** spec §6.2 (line 370) and suite step 1 (507-509) say a ranged GET "reads it". That is a property of field order and places no obligation on a writer. Spec §6.9 step 1 (line 710) does not mention a ranged read, though S31's "Where" cites it.
- **Fix:** drop S31 and keep Q3 as a local implementation choice. R1 still stands.

### m2. S43 bundles two unrelated rules and gates the wrong part

- **Where:** S43 (201).
- **Evidence:** "A dry run takes no lock" is used by E12 and tested in Part 3 (line 1340), but S43 gates Part 5 only.
- **Fix:** split it. The dry-run half gates Part 3. M3 removes the other half.

### m3. Orphan `restore` segment adoption and the reconstructed report are over-built

- **Where:** S35 third sentence, E14 step 1 (861-863), lines 877-879.
- **Fix:** a rerun writes a fresh `restore` segment (the UUIDv7 name cannot collide) and the old one becomes an `ORPHAN` note, as non-adoptable expiry orphans already are. A repeat call on a finished restore returns `already` instead of rebuilding `transitions` and `tombstoned`. This removes one adoption rule, one S sentence and one crash-table branch.

### m4. `--state-dir` on local `prune` is speculative

- **Where:** E13 (794, 799-800), the surface table (1101), NFR (1126-1129).
- **Evidence:** the design's own figures: without it, "tens of seconds" a run on a corpus-sized catalog, for a monthly command. No time is measured (line 1185).
- **Fix:** keep `state_dir` on `expire` for Part 6. Add the `prune` parameter and flag only if Part 3's bench shows the need.

### m5. `read_range` with length-doubling for segment headers is a premature optimisation locally

- **Where:** E10 (680-682), E8.
- **Evidence:** it saves reading segments that are typically tens of kilobytes, in a run that already reads every retained root whole.
- **Fix:** read segments whole in stage 2. Add `read_range` to the protocol with Part 6 if Azure measurements ask for it. This also shortens R1 and R3.

### m6. Goal 1 contradicts E5

- **Where:** Goal 1 (129-130), E5 (465-479), Q2.
- **Evidence:** "Every Inventory commit on a local catalog runs under the catalog lock", yet under Q2 (a) a 1.x commit on a no-lock filesystem runs unlocked.
- **Fix:** reword the goal to "wherever the filesystem locks". Give Q2 an option (c): never lock 1.x catalogs. It is rejected at line 1091, but E5 already accepts the same straddle race, and the owner should see it priced against the breaking changes listed at line 1236.

### m7. Parts 4 and 5 are not order-free

- **Where:** line 1229 against 1350-1352 and 1362.
- **Evidence:** "Parts 4 and 5 may land in either order", but Part 4's `ExpiryBackend` adapter implements "`restore` (from Part 5)" and `run_expiry` must pass "every stage-2 check", which includes the restore cases.
- **Fix:** move the conformance run to whichever part lands last, or order 5 before 4.

### m8. Part 6 material sits in this design

- **Where:** E16 "The Azure pieces, in plain terms" and the RBAC/ACL paragraph (970-988), R10, the last "Not confirmed" item (1187-1192).
- **Evidence:** the lease primer, the release identity's data actions, hierarchical-namespace ACLs and the sdmxlib-rest release gate are release-job content. The design itself says "This design sent no request to an Azure host".
- **Fix:** move these to the R table as pointers, or to `release-job.md` at its re-review.

### m9. N4 is an ask with no consumer

- **Where:** N4 (236), Non-Goals (157-158).
- **Evidence:** it "blocks nothing", the test carries its own replay, and a public reader is a stated stage-3 Non-Goal.
- **Fix:** drop N4, or record it as a stage-3 note.

### m10. Q4 and Q8 recommend "(a)" with no reason

- **Where:** lines 171 and 175.
- **Evidence:** every other question gives one. Q8 also lacks the option in M3.
- **Fix:** add a one-sentence reason to each.

### m11. Altitude drops and uncovered plan items

- **Altitude:** E1 and E5 carry `os.open` flags, errno lists, `msvcrt.LK_NBLCK` and a 50 ms poll in the architecture section (327-348, 467-469), for a module the design sizes at 40 lines. The decision is "an OS advisory lock, bounded wait, non-reentrant"; the rest belongs in the PR. This matches the house style in `release-job.md`, so it is a nit.
- **No goal covers `doctor`:** E15 adds six `doctor` items. GH-125 part 3 asks for them, so add a goal or fold them into Goal 7.
- **Unanchored test:** the `rollback` test in Part 5 (1387) serves no goal or acceptance item.

## Answers to the five priorities

1. **Scope.** The design stays in statcatlib and correctly leaves the lease, gates and volume to Part 6. Leaks outward: m8, m9. In-scope work pushed out or left unowned: M7 (`verify-catalog` over blob; server restore via R9).
2. **S31 to S43.**
   - Necessary for this design: S32 (after rewording, M4), S36, S37, S38, S39's wait, S41, S42, and S33's refusal-during-restore half.
   - Not necessary: S31 (m1).
   - Avoidable by a simpler design: S33's exemption and S43's first half (M3), S34 (M5), S35's adoption sentence (m3), S39's Windows sentence if Windows is unsupported (M7).
   - Not precise enough for spec text: S32 (M4), S43 (m2).
   - Missing rows: the E3 lock span, E4 conflict semantics, the GC stop (M7).
   - Process and unstated contradictions with revision 5: M1.
3. **Simplicity.** The lock is sound; the only real alternative is in m6. The single pending-segment path (E11) is a real simplification; trim per m4, m5 and M8. Restores are the over-built area (M2, M3, M5, m3). Five parts is a reasonable cut, but the release grouping (M6) and the Part 4/5 order (m7) need fixing.
4. **Open questions.** All nine give options, costs and a recommendation; m10 lists the two without a reason. Silent decisions are in M6 and M7.
5. **Goals against the plan.** Goal 9 is untested (M8); Goal 1 contradicts E5 (m6); `doctor` and the `rollback` test have no goal (m11).

## Files consulted

- Suite design: `/Users/gabrielgellner/dev/statcat-suite.github.io/docs/design/inventory-history-log.md`
- Hub rule: `/Users/gabrielgellner/dev/statcat-suite.github.io/CLAUDE.md` (lines 51-69)
- Spec: `/Users/gabrielgellner/dev/sdmxlib/docs/design/sharded-bundle-catalog.md` (§6.9 at lines 633-757)
- Release-job design: `docs/design/release-job.md` (S table 205-218, D13 1072-1095, "GH-125's parts" 1217-1231, Part 6 1514-1535)
- Release job code: `src/statcatlib/release.py` (line 615, `BlobLocation` only)

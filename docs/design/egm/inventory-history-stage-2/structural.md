# Structural review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (read in full)
**Reviewer lens:** `egm-structural`
**Standards applied:** `feature-spec-standard.md`, `implementation-readiness-standard.md`, the hub's "Suite designs" rules, and `release-job.md` as the house pattern.

Verdict: 0 blockers, 5 majors, 13 minors.

## What passes

- Section set and order match `release-job.md` (Overview/Terms, Problem, Goals, Non-Goals, Open Questions, CUJs, Technical Plan, Alternatives, NFR with Not confirmed and Risks, Detailed Implementation Plan with Acceptance map and Verification Plan, Future work). "Cross-repo contract" and "Handoff" are H2, as the hub rule asks.
- Heading levels run H1, H2, H3 with no skips.
- E1-E17 (table and sections), S31-S43, Q1-Q9, N1-N7, R1-R10, CUJ 1-5 and Parts 1-5 are contiguous, with no duplicate or skipped identifier. Every E/S/N/R/Q citation in the body resolves.
- All tables have consistent column counts; the `\|` escapes in Symbols are correct.
- Header: status, date, and commits for statcatlib, sdmxlib, sdmxlib-rest and hub are present. The suite design is linked, is at revision 5, and carries S23/S25/S26 (its lines 29-32). The release-job S series ends at S30. H citations fall inside H1-H24.
- The design is in `SITEMAP.md` and the mkdocs nav.
- "Eight gaps" matches eight bullets. The gate sentence at lines 178-179 agrees with the land-order table.

## Major

### 1. Parts 4 and 5 depend on each other, but the plan says either order

- **Where:** Land order, lines 1221-1222 and 1229; Part 4, lines 1350-1352 and 1362; Part 5, line 1386; E15, lines 915 and 935.
- **Evidence:** "Parts 4 and 5 may land in either order."
  - Part 4 needs Part 5: "`WriterBackend` implements N3's `ExpiryBackend`: `opt_in`, `expire` with `stop_after`, `restore` (from Part 5)", and "`run_expiry` passes every stage-2 check", where N3 (line 235) includes the restore cases. Part 4 also ships doctor's "restore" item and the unfinished-restore finding with no test.
  - Part 5 needs Part 4: "`verify-catalog` passes across a restore, keyed by `(restore_epoch, seq)`" requires `check_history`.
  - Neither "Waits on" cell names the other part. This fails the standard's requirement that each phase is stable and testable.
- **Fix:** fix the order (Part 5 then Part 4, or move the conformance run and the cross-restore verify test into whichever lands second). Add the dependency to "Waits on" and delete line 1229's sentence. Give doctor's and verify-catalog's unfinished-restore behaviour a named test.

### 2. S rows gated on Part 5 hold rules that Parts 1 and 3 implement

- **Where:** S table, lines 191, 193 and 201; land order, lines 1218-1222.
- **Evidence:**
  - S33 ("Gates: Part 5") includes "a stage-2 writer refuses every other commit and expiry refuses to run". That is E4 (lines 451-454, Part 1) and E9 (lines 619-620, Part 3).
  - S35 ("Part 5") is cited by E11 at line 716, in Part 3: "A `restore` segment in the range is stepped over (S35)".
  - S43 ("Part 5") is cited by E11 at line 722 and E12 at line 780, both Part 3. It also bundles "A dry run takes no lock", which its "Why" cell does not cover.
  - Part 1 lands `restoring: bool = False` and the unfinished-restore refusal (lines 1064, 1218 "both refusals"), but Part 1's test table (lines 1264-1276) has no test for it.
- **Fix:** split S43 into two rows. Regate S33/S35/S43 (or their halves) to the first part that codes them and update "Waits on" for Parts 1 and 3. Alternatively move the restore-aware branches to Part 5. Add a Part 1 test for the unfinished-restore refusal.

### 3. Goals 6 and 9 have no Technical Plan section, and Goals 2, 3, 4, 8 and 9 have no Acceptance map line

- **Evidence:**
  - Goal 6 (line 138, "What was live at time T") has no E-section. The read rule appears only in handoff row N4 (line 236) and as a test helper ("the 'live at T' replay, until N4", lines 1313-1314). The implementer has to design the replay.
  - Goal 9 (lines 142-143) cites "(E8, E16)", with no Part and no acceptance line. Its only check is the Verification Plan's "`MemoryStore`-backed `BlobExpiryStorage` prototype" (lines 1428-1430). That prototype is in no part's file list and sits against the Non-Goal at lines 147-148 and the coverage table's "Left to release-job Part 6: `BlobExpiryStorage`" (line 1157).
  - The Acceptance map (lines 1392-1407) is keyed by GH-125 items. Goals 2, 3, 4 and 8 are covered only by the catch-all sentence at lines 1409-1411.
- **Fix:** add a short "reading history after expiry" subsection, or state that Goal 6 is a property with the replay algorithm written out. Assign the prototype to a part with a file path, or drop it and restate Goal 9. Add a Goal-to-E-to-Part-to-test table, or a Goal column in the Acceptance map.

### 4. "Read against" appears stale, and the 0.18 conditionals are left open

- **Where:** header, lines 8-11; Terms, line 88; E7, line 542; releases, line 1235.
- **Evidence:** the header reads the hub at `4779ed5`. Hub `main` is now `84615c2`, whose commit `48ee1a0` is "docs: update the version table for statcatlib 0.18.0", and the hub's `docs/architecture.md` line 153 says "statcatlib 0.18.0". The design still says "version 0.17.0 plus the unreleased release job", "0.18 if it ships before Part 1", "a 0.18 without Part 1", and "The next release (0.18.0) ... Part 1 too if it has landed".
- **Not checked:** statcatlib `origin/main` itself. Only the hub side was verified.
- **Fix:** re-read against current heads and update the commits. Resolve each conditional (stage-1 writer = 0.14 to 0.18) and remove or rewrite the first row of the release table.

### 5. Owner decisions and unknowns sit outside Open Questions, and the "Not confirmed" guarantee is false

- **Where:** Risks, line 1198; Verification Plan, lines 1418-1420; Not confirmed, lines 1163-1164, 1177-1186.
- **Evidence:**
  - Windows: "The owner states whether Windows is supported" and "one manual run ... on Windows, or a statement ... that the Windows lock is untested". This is an owner decision with an either/or left to the implementer, and it has no Q number. E1 and S39 specify a Windows lock regardless.
  - "Each has a risk row or a test that will settle it" fails for three items:
    - the `{"from": null, "to": null}` change item (lines 1177-1181). If sdmxlib rejects it, that is a format or contract matter needing an N or S row; no Part 5 test names it.
    - `os.replace` on Windows with an open reader (lines 1182-1184).
    - "The shape of N1 to N3" (line 1186), although E9 (line 643), E15 (line 904) and Part 4 (lines 1350-1352) are written to those exact names.
- **Fix:** add Q10 for Windows support, gating Part 1. Add a handoff row asking sdmxlib to confirm or test the null/null item. Either mark the N1-N3 names provisional where used, or add a re-read step when sdmxlib's design lands. Correct line 1163's sentence.

## Minor

### 6. Q table "Changes" column and the decision table's "Settles" column disagree

- **Where:** lines 168-176 and 307-323.
- **Evidence:** Q2 lists "E5" but S39 cites Q2 (line 197). Q3 lists "E9, R1" but S31, E8 (lines 594-595) and E10 also depend on it. E1's Settles omits Q9, E9 omits Q3, E14 omits Q6 and Q8, E15 omits Q7; E5, E13 and E17 do list their Qs.
- **Fix:** make both columns complete and symmetric.

### 7. Ambiguous foreign identifier

- **Evidence:** line 314: E8 Settles "release-job D13, Q9". Under the prefix rule at lines 33-34 a bare Q9 is this design's lock-wait question; the release-job's Q9 is meant. "Phase 4 F14" (line 995) uses an undeclared prefix.
- **Fix:** write "release-job D13, release-job Q9"; declare F.

### 8. "Each decision carries its own table" is false

- **Evidence:** line 1082. E2, E5, E7, E11, E12, E15 and E16 have no options table. E5's and E15's options are in Q2 and Q7. E11 (one path for run and recovery) and E12 (dry run without the lock) have no alternative recorded anywhere.
- **Fix:** reword the sentence and add the missing alternatives or pointers.

### 9. Dangling event and error references

- **Evidence:**
  - Line 774: `restore.planned`, `restore.discarded`, `restore.tombstoned` are attributed to "A restore (E14)", but E14 (lines 833-897) never names an event.
  - R7 (line 1001) adds no `restore.*` event to release-job D14, although R9 adds a server restore.
  - E9 step 0's "ends the run with nothing taken" (line 624) names no event or `stopped` value.
  - The Errors table (lines 1027-1051) omits `opt_in`'s `InventoryError` (lines 495-496), `expire`'s `ValueError` (line 617) and `restore`'s `ValueError` (line 848). The crashed-`HISTORY` case "refused otherwise" (line 506) has no error class.
- **Fix:** state the emission points in E14 and complete the table.

### 10. Documentation obligations appear only in NFR and Risks

- **Evidence:** lines 1131 ("the opt-in page says to run the first expiry while no seal runs"), 1199 ("Opt-in's page repeats it") and 1202. Part 2's Docs bullet (lines 1288-1289) lists only "the three statements and the order of upgrades". `docs/reference/` gains the `store/expiry.py` page in Part 3 (line 1320) although the module is created in Part 2. No reference page is listed for `store/lock.py`.
- **Fix:** carry each obligation into the owning part's Docs bullet.

### 11. Misplaced content

- **Evidence:** "The public surface, against GH-125's items" (lines 1095-1103) is a justification table and belongs in the Technical Plan or beside the Acceptance map. "The Azure pieces, in plain terms" and the RBAC paragraph (lines 970-988) are a glossary and Part 6 scope, placed inside decision E16 of a design whose first Non-Goal excludes Azure.
- **Fix:** move the surface table; move the glossary to Terms and the RBAC text to an R row.

### 12. N7 disagrees with the paragraph below it

- **Evidence:** line 239: "A release, 0.89.0, with N1 to N3", with "Blocks: Part 3" only. Lines 241-242: "0.89.0 with N1 ships before Part 3 lands, and N2 and N3 before Part 4." It is unclear which release carries N2 and N3 under the `<0.90` cap.
- **Fix:** name the release or releases, and add Part 4 to N7's Blocks.

### 13. Handoff has no sdmxlib-rest subsection

- **Evidence:** line 239: "sdmxlib-rest follows" (a cap bump). R10 (line 1004) needs an sdmxlib-rest release carrying `sdmxlib_version`. The suite design has "### sdmxlib-rest" under Handoff.
- **Fix:** add the subsection, or state that nothing is handed off and why.

### 14. Unqualified figures for one corpus

- **Evidence:** line 646: "2,979 roots ... 4.7 GB". Line 1128: "version 2,979 reads 1.7 GB each run". These are 1.6 MB and 560 KB a root, with no label saying which applies.
- **Fix:** state the root size beside each figure.

### 15. Dry-run locking is inconsistent with S43's wording

- **Evidence:** S43 (line 201) and E12 (line 779) say "A dry run takes no lock". The opt-in dry run returns at step 4 "under `exclusive()`" (lines 490, 503). The restore dry run is also under `exclusive()` (lines 848-855).
- **Fix:** scope S43 to expiry, or align the other two.

### 16. Contingent statements presented as settled

- **Evidence:** the Non-Goal "A restore of a 1.x catalog (Q8)" (line 150) depends on an open question. The Overview (line 75) and E16 split (line 967) state "release-job Part 6 (R9)" as fact, while line 990 says "The owner decides each".
- **Fix:** mark both "if Q8 (a)" and "if R9 is accepted".

### 17. Unresolvable reference and ambiguous step numbers

- **Evidence:** "sdmxlib's plan, line 431" (line 151) is not in Inputs and has no path or commit. E6 line 513, "step 1's stop", means engine step 1 inside a section with its own steps 1-6. "E4's two checks" (lines 415, 869) appears while E4 lists three bullets (lines 444-454).
- **Fix:** cite the plan properly, write "expiry step 1", and say which two checks.

### 18. Formatting consistency

- CLI lines sit inside `python` fences in E6 (lines 483-486) and E14 (lines 835-838), where E13 uses a separate `text` block (lines 791-801).
- Bullet markers mix `-` (lines 105-122, 332-348, 1166-1192) and `*   ` (lines 147-159, 1124-1151).
- Heading case mixes Title Case ("Alternatives Considered", "Verification Plan") and sentence case ("Future work", "Risks and limits").
- Overview list items 4 and 5 (lines 53-54) are in the reverse order of Parts 4 and 5.
- Test "Covers" cells mix E-numbers, step names and an unsourced quotation (line 1274).

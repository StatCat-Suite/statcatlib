# Style review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (1,439 lines, read in full)
**Reviewer lens:** `egm-style`
**Rules applied:** the "Writing style" section of the hub `CLAUDE.md` and of statcatlib's `CLAUDE.md` (identical; statcatlib adds that a local issue is written `GH-N`). Wording only; facts were not checked against code.

Verdict: 0 blockers, 10 majors, 16 minors. Each major is wording that leaves a lock rule, a crash rule or a step reference open to two readings. The document has no rhetorical questions and no punchline endings, and the questions in the Open Questions table are genuine.

## Major

### M1. Unqualified step numbers across six procedures

- **Lines:** 431, 452, 513, 810-811, 884-886, 955-956, 1380.
- **Problem:** six numbered procedures exist: the engine (steps 0 to 8), `_commit_locked` (1 to 7), opt-in (1 to 6), `prune` (1 to 3, plus "its three steps"), the restore (1 to 6) and the release job's run. A bare "step N" depends on the reader guessing which.
- **Quotes:**
  - 431: "It runs steps 2 and, best effort, 3" (`_commit_locked`'s steps)
  - 452: "a restore stopped between its steps 2 and 5"
  - 513: "step 1's stop at a root without the history fields" (the engine's step 1, inside opt-in's own list)
  - 810: "`prune` runs its three steps as today" (inside a three-item list that is a different three)
  - 955-956: "takes the lease in step 1 ... calls `expire` in step 7" (the release job's)
- **Rewrite:** qualify every reference outside its own section: "expiry step 1", "restore steps 2 and 5", "`_commit_locked` step 3", "release-job D1 step 7". At 810 name the steps: "`prune` retires, deletes edition directories and deletes unlisted `data/` files as today".

### M2. Checkpoint rule groups three cases ambiguously

- **Lines:** 639-641.
- **Quote:** "A root is a checkpoint when it is `since_seq`, when the root or line before it has another `restore_epoch` or it is version 1, or when its `created_at` falls in a later UTC month than the one before it (H4)."
- **Problem:** "or it is version 1" can attach to either clause, and "the one before it" can mean the month or the root.
- **Rewrite:** "A root is a checkpoint (H4) when any of these holds: (1) its `seq` equals `since_seq`; (2) it is version 1, or its `restore_epoch` differs from that of version `seq - 1`; (3) its `created_at` is in a later UTC month than the `created_at` of version `seq - 1`."

### M3. Chain-check base (S32 and step 2) is hard to parse

- **Lines:** 190, 651-654.
- **Quote 190:** "the line of that `seq` in the newest `expiry` segment reached from `newest_segment`, stepping over `restore` segments, or the retained root when no such line exists."
- **Quote 651:** "the hash of c - 1 is the `root_sha256` of its line in the newest `expiry` segment reached from `HISTORY.newest_segment`, stepping over `restore` segments, and the hash of retained root c - 1 when that segment holds no such line (S32)."
- **Problem:** "that `seq`" has no antecedent, "stepping over" has no subject, and "is A ..., and B when ..." reads as a conjunction.
- **Rewrite (both places):** "For the lowest candidate c, the engine walks the segment chain from `HISTORY.newest_segment`, skips `restore` segments, and stops at the first `expiry` segment. If that segment has a line for c - 1, the engine uses the line's `root_sha256`. Otherwise it hashes the bytes of retained root c - 1."

### M4. Step 6's final check does not say which process pauses

- **Lines:** 729-731.
- **Quote:** "on Azure a writer that took the lease after a pause has Put the leaf, so the tag differs and the delete fails, or it Puts the leaf afterwards and recreates it."
- **Rewrite:** "On Azure the expiry process can pause long enough to lose the lease, and another writer can then take the lease and Put the same leaf. If that Put comes before the delete, the leaf's tag differs and the delete fails. If it comes after, the Put recreates the leaf."

### M5. The kept-root logging rule is stated four ways

- **Lines:** 201, 720-722, 771, 1046.
- **Quote 720:** "The engine logs `expiry.kept_root` for it when its `seq` is at or below `expired_through`, and logs nothing when a later epoch's root has taken the name since a restore (S43)."
- **Quote 201:** "A root listed in a pending header whose name a later epoch's root has taken since a restore is kept without a report."
- **Quote 1046:** "A root that matches no pending line | kept; `expiry.kept_root`"
- **Problem:** 720 presents two conditions without saying they are exhaustive, 201 hides the actor, and 1046 drops the condition.
- **Rewrite (720):** "The engine keeps any other root. If the root's `seq` is at or below `expired_through`, the engine logs `expiry.kept_root`. If it is above, a later epoch's root has taken the name since a restore, and the engine logs nothing (S43)."
- **Rewrite (201):** "After a restore, a new-epoch root can hold a `seq` a pending header lists. The engine keeps that root and logs nothing."
- **Rewrite (1046):** add "at or below `expired_through`" to the condition.

### M6. "best effort" leaves the pre-check's behaviour undefined

- **Line:** 431.
- **Quote:** "It runs steps 2 and, best effort, 3; `_commit_locked` repeats both under the lock."
- **Rewrite:** say what the unlocked check does on each outcome, for example: "It runs `_commit_locked` steps 2 and 3 without the lock. It raises on a refusal it sees, and its pass is advisory, because an opt-in or expiry can follow it. `_commit_locked` repeats both under the lock."

### M7. Opt-in's crash rule names no error, and the tag conflict is a passive

- **Lines:** 505-509.
- **Quote:** "A `HISTORY` left by a crash after this step is kept when it holds exactly these values and refused otherwise."
- **Quote:** "so a `restore_epoch` changed since the read is refused."
- **Problem:** the Errors table has no row for the first refusal.
- **Rewrite:** "If a crashed opt-in left a `HISTORY`, `opt_in` keeps it when it holds exactly these values and raises `<ErrorName>` otherwise." and "so `opt_in` raises `InventoryConflictError` when another process changed `catalog.json` (for example its `restore_epoch`) after step 2."

### M8. Q2's recommendation reads as its opposite

- **Line:** 169.
- **Quote:** "opt-in is refused where the lock is."
- **Problem:** it reads as "refused where the lock exists".
- **Rewrite:** "opt-in is refused on a filesystem that refuses the lock."

### M9. "Opted-in catalog" and "2.x catalog" are used as synonyms but defined differently

- **Lines:** 85 (definition), 442-445; "2.x catalog" also at 71, 169, 197, 394, 474, 477, 532, 619, 1033-1041, 1236.
- **Quote 442-444:** "On an opted-in catalog `_commit_locked` reads `inventory/HISTORY` ... **No `HISTORY`.** ... a 2.x catalog without `HISTORY` is an error"
- **Problem:** Terms defines an opted-in catalog as 2.x with `HISTORY`, so the "No `HISTORY`" case cannot occur on one.
- **Rewrite:** add "2.x catalog: a catalog whose `catalog.json` is format 2.x" to Terms. Use "2.x catalog" where the rule keys on the format (442, 458, 1033-1035) and "opted-in catalog" only where `HISTORY` is known valid.

### M10. Restore step 1 and its crash row

- **Lines:** 860-861, 884.
- **Quote 860:** "An unread root goes in the group of the nearest readable root below it, or of the current epoch."
- **Rewrite:** "The restore puts an unread root in the group of the nearest readable root below it. When no readable root lies below it, the root goes in the current epoch's group."
- **Quote 884:** "finds the newest segment is a `restore` segment of the epoch before `HISTORY`'s with no root of the new epoch at `first`; takes S as `first - 1`; refuses another `to`; resumes at the first step not done"
- **Rewrite:** "The rerun detects the unfinished restore: `HISTORY.newest_segment` is a `restore` segment whose epoch is `HISTORY.restore_epoch - 1`, and no root of the new epoch exists at its `first`. It sets S to `first - 1`, raises `ExpiryRefusedError` for any other `to`, and continues from restore step 3 when `catalog.json` still has the old epoch, else from step 4."
- **Also 884, column 2:** "readers whose `CURRENT` fails fail closed" should be "a reader that cannot read `CURRENT` refuses to serve". The same "`CURRENT` fails" phrase is at 545 and 1381.

## Minor

### m1. Headings that break the title rule

- 399: "E3: what `try_commit` holds the lock over" (hook clause). Rewrite: "E3: the lock span of `try_commit`".
- 1006: "E17: where the pure rules live" (hook clause). Rewrite: "E17: pure rules in sdmxlib, I/O in the engine".
- 258, 269, 277, 285, 295: the CUJ headings are sentences. Rewrites: "CUJ 1: opt-in and expiry on a laptop"; "CUJ 2: a seal concurrent with `prune`"; "CUJ 3: crash during expiry"; "CUJ 4: restore of an opted-in catalog"; "CUJ 5: an older tool on an opted-in catalog".
- 970: "**The Azure pieces, in plain terms.**" Drop "in plain terms".
- Capitalisation is mixed: Title Case at 98, 145, 161, 256, 301, 1080, 1105, 1212, 1414; sentence case at 181, 203, 303, 1161, 1194, 1214, 1390, 1432. Pick one.

### m2. Hook-clause table introductions

- 207: "What other code reads of its output:". Rewrite: "The table lists each output, its writer and its readers."
- 536-537: "What an opted-in local catalog means for each reader and writer, checked in each version's code:". Rewrite: "The table gives each tool's behaviour on an opted-in local catalog, read from each version's code."

### m3. Negative form and contrast by negation

- 102-103: "Eight gaps in the code stand between those rules and a catalog that expires:". Rewrite: "The code differs from those rules in eight places:".
- 142: "needs no engine code of its own: it supplies a storage, a lock, the gates and a cache directory". Rewrite: "The release-job design's Part 6 reuses the engine and supplies a storage, a lock, the gates and a cache directory".
- 315: "stop at the first root that is neither a checkpoint nor old enough". Rewrite: "stop at the first non-checkpoint root whose successor is inside W".
- 322: "GC is unchanged but for one stop". Rewrite: "GC gains one stop".
- 617: "a `window` that is not positive". Rewrite: "a zero or negative `window`".
- 1040: "A storage that is no `ExpiryStorage` with `stage_2`". Rewrite: "Any storage other than an `ExpiryStorage` whose `stage_2` is true".
- 1236: "no longer raises `InventoryWriteRefusedError`". Rewrite: "A local commit to a 2.x catalog now succeeds".
- 1409: "bullets that are no acceptance item". Rewrite: "bullets outside the acceptance list".
- 4: "No EGM review yet." Rewrite: "Awaiting EGM review."

### m4. Passives that hide the actor

- 328: "The file is created by the first call, stays empty, and is never deleted or read." Rewrite: "The first call creates the file. No code writes, reads or deletes it."
- 379-380: "when the lock held through `exclusive()` can no longer be trusted". Rewrite: "when the process may have lost the lock it took through `exclusive()` (on Azure, when the last lease renewal is 40 s old or more)".
- 453-454: "stays refused until the restore is rerun". Rewrite: "every commit raises it until an operator reruns the restore".
- 668-669: "The file is written to a staged name and renamed". Rewrite: "The engine writes ...".
- 672-673: "The candidates are grouped ... each group is split". Rewrite: "The engine groups ... and splits ...".
- 716: "A `restore` segment in the range is stepped over". Rewrite: "The engine skips a `restore` segment in the range".
- 718-720: "An absent root is done. A root whose SHA-256 equals ... is deleted ... Any other root is kept." Rewrite: "The engine skips an absent root, deletes a root whose SHA-256 equals ..., and keeps any other."
- 883: "the orphan is reported". Rewrite: "`verify-catalog` prints the orphan as a note".
- 914-915: "An opted-in catalog with an unfinished restore is one finding". Rewrite: "`verify_catalog` reports an unfinished restore as one finding".
- 955: "Part 6 removes the refusal, takes the lease in step 1". Rewrite: "Part 6 removes the refusal. The release job then takes the lease ...".
- 984-985: "The engine ... sends three kinds of request `obstore` cannot". The storage sends them (970-971 says the engine sees Azure only through `ExpiryStorage`). Rewrite: "`BlobExpiryStorage` sends ...".
- 1029: "The lock is held for 120 s". Rewrite: "Another holder keeps the lock for the whole 120 s wait".
- 178: "The owner's answers are recorded here". Rewrite: "The author records the owner's answers here".
- 1190: "This design sent no request to an Azure host". Rewrite: "The author sent no request ...".
- 252: "Handoff row 11 ... re-reviews Part 6". Rewrite: "Handoff row 11 calls for a re-review of Part 6".

### m5. One word with several meanings

- "owner": the catalog's owner (258-260), the project owner (178, 990, 1198, 1231, 1240) and a table column for a repository (184, 187). Rewrite: "catalog owner", "project owner", column "Repository".
- "land": Terms (96) defines it as a part merging, and it is also used for a commit succeeding at 424, 462, 1270, 1273, 1299. Rewrite: "succeeds" or "commits".
- "gate": `just ci` (357, 1155, 1168, 1198, 1407), the two expiry gates (58, 147, 965, 1004) and a verb (178-179). The reader is never told "the gate" means `just ci`. Define it at first use.
- "Part 6": bare at 533, 954-956, 970, 990, 999, 1036-1037 and 1238, beside this design's Parts 1 to 5. Line 33 promises the release-job design's identifiers carry its name. Rewrite: "release-job Part 6" throughout.
- "the window": W, `gc_window`, `older_than` and the lock wait. It is ambiguous at 141 ("inside the window"), 195 and 807 ("`prune`'s window") and 947-948. Name the parameter each time.
- "expire" with the actor as subject: 258 ("A laptop owner opts in and expires"), 271 ("while `statcat prune --apply` expires"), 319, 1338, 1399. Elsewhere the thing deleted is the subject (86, 103). Rewrite: "expires old Inventory versions". 512 "Opt-in never expires." reads as "has no expiry date"; rewrite "Opt-in runs no expiry."
- "expiry" / "the engine" / "a run" as the acting process: "expiry chooses" (272, 352), "expiry never takes" (280, 943), "expiry refuses to run" (191). Terms defines Engine and Run; use "the engine" where a process acts.
- "tag" / `etag` / "ETag": "tag" appears at 494 and 508 before it is introduced at 583, and ETag is explained at 976. Define "tag" at 494.
- `ConflictError` (235, 462) against `InventoryConflictError` (447, 580, 1039). Qualify the first as sdmxlib's conformance class, or use one name.
- "holds": 173 "`embargo: true` already holds every automatic release" and 1204 "root S + 1 holds automatic releases" read as "contains". Rewrite: "blocks".
- "S": root S and S + 1 sit beside the S-series rows ("S25's tombstones for root S + 1", 233 and 947). Add a Terms row: "S: the restore's target `seq`".
- "c": the lowest candidate at 651 and the checkpoint count at 1107. Rename one.
- "the suite": 919-922 "The conformance suite ... what the suite cannot build" next to "suite design". Rewrite: "the conformance cases".
- "the opt-in page" (1131, 1202) and "Opt-in's page" (1199): name it as `docs/api/admin.md`, "Opting in to expiry".

### m6. Issue-reference format

- 1121, 1421: "StatCat-Suite/statcatlib#126". The design lives in statcatlib, so the rule gives `GH-126`. All other references follow the rule.

### m7. Undefined abbreviations and terms

- GC: first at 141, never expanded.
- EGM: 4.
- SAS (984), RBAC (986), ACL (987).
- NFRs: 1208.
- "Phase 4 F14" (995): F is absent from the identifier-prefix paragraph (30-35).
- "table" (1135, "920 bytes a released table"): used nowhere else. Say "product" or define it.
- "failover" (841).
- "fails closed": used for readers (884, 1381) and for step 6 (1209) with different effects. State the effect at 1209: "deletes no leaf".

### m8. Sentences too long to parse; convert to lists

- 233 (N1) and 235 (N3): single cells of 100 to 200 words with nested parentheses. Use one bullet per function or case.
- 235: "a root below `expired_through` with no matching line kept". Rewrite: "... with no matching line is kept".
- 263-266 (CUJ 1's `prune` sentence): split at "and with `--apply`".
- 497-502 (opt-in step 3): list the five refusals, and define `since_seq` before the refusal that uses it.
- 683-688 (orphan adoption): list the six conditions.
- 868-874 (restore step 5). Rewrite: "`retired` holds root S's tombstones. It also holds one tombstone, with `retired_at` set to the restore's time, for each edition that a readable discarded root records in a leaf and that root S records in neither a leaf nor `retired`."
- 910 (check 3 cell): six checks in one cell.
- 943-946. Rewrite: "Expiry takes root N only when root N + 1 was created before `now - inventory_window`. `RetentionPolicy.check` requires `inventory_window >= gc_window + 1d`. Root N + 1 is therefore older than GC's cutoff, and GC's walk ends at or above it."
- 749-753: split at "A `CatalogLockLostError`".

### m9. Ambiguous pronouns and clipped constructions

- 19: "The contract changes it found necessary". Rewrite: "The contract changes this design proposes".
- 168: "Parts 1 and 2 need nothing from it". Rewrite: "... from sdmxlib 0.89".
- 206: "It is statcatlib's and stays local for that reason." Rewrite: "This design belongs to statcatlib and stays in its repository."
- 247-248: "The hub session takes them there as revision 6, with its own review, and S31 to S43 to sdmxlib's spec through N5." Rewrite: "The hub session applies them as revision 6 of the suite design, with its own review. N5 carries S31 to S43 into sdmxlib's spec."
- 272-275 (CUJ 2): "expiry then chooses below it and keeps every leaf it names" has two different "it". Rewrite: "the engine then chooses candidates below the seal's root and keeps every leaf that root names". Rewrite the next sentence as: "When the engine goes first, the seal's `try_commit` waits for the lock, reads `HISTORY` and the head's bytes, then writes its leaves and its root. Every leaf its root names exists."
- 336-338: "The C runtime implements it with `LockFile`, the byte-range lock `LockFileEx` takes with `LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY`". Rewrite: "The C runtime implements it with `LockFile`, which takes the same byte-range lock as `LockFileEx` called with ...".
- 346-347: "gets `RuntimeError` at once, through any storage object". Rewrite: "... whichever storage object it calls through".
- 362: "the suite design chose ordering". Rewrite: "the suite design orders the calls so that no caller nests the lock".
- 389: "yields at once, as today's writer takes no lock". Rewrite: "yields at once; today's blob writer takes no lock".
- 390: "a catalog with no `HISTORY` has no lease to take and yields". Rewrite: "on a catalog with no `HISTORY`, `exclusive()` yields at once".
- 401: "`try_commit` becomes the lock and a call:". Rewrite: "`try_commit` takes the lock and calls `_commit_locked`:".
- 436: "deadlocks itself; the thread table turns that into `RuntimeError`". Rewrite: "A direct `try_commit` caller that already holds the lock gets `RuntimeError` from the thread table."
- 474: "it raises the error". Name `CatalogLockUnsupportedError`.
- 511-512: "a rerun completes it". Rewrite: "a rerun completes the opt-in".
- 516-520: the "three statements" are not marked as such. Introduce them ("The three statements are:") and list them.
- 623-624: "A `CURRENT` that is missing or malformed after the step ends the run with nothing taken." Rewrite: "When `CURRENT` is still missing or malformed after step 0, the run ends and takes nothing."
- 641, 643: "The fact about `seq - 1`", "over facts the engine reads". Rewrite: "The `created_at` and `restore_epoch` of `seq - 1`".
- 744: "then steps 6 and 7". Rewrite: "then runs steps 6 and 7".
- 825: "A clock set forward expires early". Rewrite: "A clock set forward makes the engine take roots early".
- 861-863: "when it links the same way". Rewrite: "when its first header links to `HISTORY.newest_segment`".
- 941: "With one jobs configuration". Rewrite: "When expiry and GC read the same jobs configuration".
- 1101: "each run reads them without the cache". Rewrite: "and, with no cache, every run re-reads them".
- 1274: "`HISTORY` advanced between `commit`'s head read and the lock is seen by the check". Rewrite: "The check sees a `HISTORY` that expiry advanced between `commit`'s head read and the lock".
- 1297: "`catalog.json` changed between the read and the replace is refused". Rewrite: "Opt-in raises `InventoryConflictError` when `catalog.json` changes between its read and its replace".
- 1338: "and returns". Rewrite: "and returns without deadlock".
- 1429: "to show the interface is enough". Rewrite: "to show `ExpiryStorage` suffices for a blob store".

### m10. "hash" used for "equal the SHA-256 of"

- **Lines:** 500, 853-854, 870, 1378.
- **Quote:** "a `previous_root_sha256` ... that does not hash the root before it"; "must hash root S's bytes"; "that hashes root S".
- **Rewrite:** "does not equal the SHA-256 of the root before it"; "must equal the SHA-256 of root S's bytes".

### m11. Figurative or idiomatic wording

- 462: "A seal that slept through an expiry lands on the true head." Rewrite: "A seal whose base an expiry made stale retries on the current head."
- 176, 295, 1130: a seal or tool "meets" an expiry or catalog. Rewrite: "waits on" / "opens".
- 545: "can take a checkpoint for the head". Rewrite: "can mistake a checkpoint for the head".
- 1101, 1148, 1201, 1203: "for good". Rewrite: "permanently".
- 457: "moments ago". Delete.
- 446-448: "the repair the spec's rule wants". Rewrite: "requires".
- 174, 194, 917: "leaves the exit status alone". Rewrite: "does not change the exit status".
- 526: "A second way in". Rewrite: "A second opt-in path".
- 693: "has come to name". Rewrite: "now names".
- 844: "Its reach is". Rewrite: "It can make live any `superseded` entry of the head's leaf".
- 468: "`EINVAL` or `EACCES` variants". Drop "variants" or list the values.

### m12. Estimates stated as fact

- 1122 says no time is measured, but these lines read as facts: 428 "a few milliseconds on local disk"; 1125 "Milliseconds on local disk"; 1127 "under a second"; 1128-1129 "tens of seconds"; 360 and 1198 "40 lines", for a module that does not exist yet.
- **Rewrite:** prefix each with "estimated", or point 428 to the NFR caveat.

### m13. Inconsistent figure for the same root count

- 646: "2,979 roots ... 4.7 GB" uses 1.6 MB a root, the 7,000-product size. 1128: "version 2,979 reads 1.7 GB" uses 560 KB a root, the 2,521-product size. 2,979 is the GH-126 corpus, whose roots are 560 KB.
- **Rewrite:** state the root size beside each figure or use one basis.

### m14. Vague goal

- 136: "leaves a catalog that reads correctly". Rewrite: "leaves a catalog whose readers find the same head and entries as before the run".

### m15. Unrelated facts joined in one sentence or row

- 667-668: "A root whose listed tag equals the cached one is not read again, and the file holds hashes and paths only (S23)." Split.
- 201 (S43): "A dry run takes no lock." sits in a row about restore-reused `seq` values, and the Why column does not cover it. Give it its own row.
- 806-808: the `ValueError` sentence mixes "naming both", a parenthesis and "and so does `keep_roots` below 2". Split into two sentences.

### m16. "(Not confirmed)" as an inline pointer

- **Lines:** 338, 468-469, 983.
- **Problem:** the capitalised mid-sentence phrase reads as an editing note.
- **Rewrite:** "(unverified; see Not confirmed)".

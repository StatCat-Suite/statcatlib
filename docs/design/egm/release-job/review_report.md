# EGM review report: the release job and garbage collection

**Target:** `docs/design/release-job.md`, revision 1 (Phase 5, GH-52), whole document, 1045 lines.

**Read against:** statcatlib `3306996` (v0.16.2); sdmxlib `origin/main` `6a646b75`, through copies of
the spec, the plan, `inventory/_rules.py`, `_routing.py`, `_format.py`, `_reader.py` and
`edition/_check.py` at that commit (the sdmxlib checkout's working tree was not read); hub `83da9c2`,
suite design `inventory-history-log.md` revision 4; issue texts for statcatlib GH-52, GH-125 and
GH-134, sdmxlib#381 and StatCat-Suite/statcat-suite.github.io#19.

**Reviewers:** design critic (`DES`), codebase details in two instances, statcatlib claims (`CB-1` to
`CB-19`) and sdmxlib, spec, plan and issue claims (`CB-101` to `CB-123`), implementation readiness
(`IR`), comprehension (`CMP`), structural (`STR`), style (`STY`), zoom out (`ZO`). Their full reports
sit beside this file; the two codebase reports are `codebase_details_report.md` and
`codebase_details_sdmxlib_report.md`.

**Counts after deduplication:** 2 Blockers, 37 Major, 31 Minor, from 149 raw findings.

**Verdict.** The structure of the design stands: one short, ordered, idempotent `statcat release`
command, a TOML jobs configuration checked against §11.6, refusals persisted beside the head, S13
answered from the manifest's `statcat` section with the leaf winning, and artefact records read from
leaves. The commit retry behind Q1, the fold's idempotency through O1, GC's read order and the window
inequalities all held under review. The design is not ready to implement. A refused registration can
never leave `pending/`, so one fork blocks a product for up to 400 days (B1). An Inventory restore
re-arms folded registrations, and the fold then publishes editions that were cancelled or rolled back
(B2). The emergency stop cannot land during a run on an opted-in catalog (M1), and the access check
misses anonymous container access (M3). Part 6 is written against an interface no design defines yet
(M18). The design was not changed.

**Owner decisions.** No finding contradicts the decisions of 2026-11-03, 2026-11-04, 2026-11-07 or
2026-11-10. B1's fix extends `--discard`, which stays an operator's decision, so O5's "alert only"
for GC's own sweep holds. `ZO-4` proposes the opposite, cutting `--discard`; B1 explains why the
compiler ranks the remedy first.

**Severity changes the compiler made.** Each differs from what the reviewer reported.

- `CB-101` is lowered from Blocker to Major (M7). The compiler confirmed it, and the fix is one call
  (`full.leaves()`), though the NFR figure it corrects is the release instant's wait.
- `DES-10` is raised from Minor to Major (M12). GC deleting what the newest K roots name breaks a
  §11.6 rule outright.
- `DES-12` is raised from Minor to Major by merging into M7.
- `STR-7` (six decisions without options tables) is lowered from Major to Minor (m29). It causes no
  failure.

**Compiler's spot checks, by reading only.** sdmxlib `_reader.py:286-366` (`leaf()` fetches in the
calling thread; only `leaves()` uses the pool); `_rules.py:178-347`; statcatlib
`store/inventory.py:505-512`, `:572`, `:713-762` (`commit` builds a new `FullInventory` per attempt;
the writer decodes `catalog.json` as `stage-1-writer`); `store/cross_product.py:170`, `:275-276`;
`store/blob.py:547-564`; spec lines 371, 460-466, 513-529, 541-554, 579, 638, 945-958, 987-992,
1006-1032; plan lines 348-376; `catalog-on-blob-storage.md:156`, `:189`; `CLAUDE.md:823-827`;
GH-52's comment of 2026-11-05; GH-134 item 9. Each supports its finding. Nothing was run, and no
reviewer had git, so the commits come from the brief.

Line references are `design:N` for the target. `spec` is sdmxlib
`docs/design/sharded-bundle-catalog.md` and `plan` its `sharded-bundle-catalog-plan.md`.

## Blockers

### B1. A refused registration can never leave `pending/`, so one fork blocks a product

**Reviewers:** `DES-1`, `IR-12`, `CB-122`; contrary proposal `ZO-4`. **Target:** design and spec.
**Sites:** design:151, 332-336, 352-353, 634-639; spec §10.1 (line 956), §11.2 step 2 (line 988),
§11.4 (line 1008); `upload.py:257-269`.

**Failure.** At 06:00 the publish job uploads edition B1 of a product on live edition L. A corrected
B2, also on L, is uploaded at 07:30. Both are unfolded at the 08:00 pre-flight, so the fold refuses
both with `pending-fork`, as spec §11.2 step 2 requires. Neither can leave `pending/`: the release job
is read-only there (O1), GC deletes only registrations whose edition the head records (design:634),
Q6 deletes only those past the 400-day tombstone window, and `--discard` covers only unregistered
editions. Every rebuild the producer makes is built on L, so it joins the fork and is refused too. The
product cannot publish for 400 days with Q6, or ever without it. Every run in that time exits 3, the
refusal record keeps the entries, and the orchestrator's alert fires. A registration refused for
`files`, `manifest`, `chain`, `final` or `lifecycle` sits in the same state, and the design does not
say whether the fork rule counts registrations that failed another check.

**Fix.** Give the operator a path that removes a registration: `statcat gc --discard PRODUCT/EDITION`
on a registered edition the Inventory does not record deletes the registration, then the manifest,
then the directory, logged as deletions, and on a recorded edition refuses, naming `statcat admin
cancel`. List it as a spec change to §11.2 step 2 and §11.4 beside S20 and S22. State which
registrations the fork rule counts, and widen Q6 to refusals other than age. Add a gating test: after
a refused fork, discarding one registration lets the other fold. `ZO-4`'s alternative runbook
(re-upload, then cancel) does not work here, since upload refuses a second registration of the same
edition and any new edition joins the fork.

### B2. An Inventory restore re-arms folded registrations, and the fold publishes withdrawn editions

**Reviewers:** `DES-2`. **Target:** design and spec. **Sites:** design:150, 294-295, 311, 634, 656;
spec line 638 (the head chain skips the roots a restore discarded), §6.9 restores (lines 719-728),
§6.7 (line 623), §10.3 (line 970); `_rules.py:299-306`.

**Failure.**

1. Edition E is folded at seq S+3 and cancelled (`withdrawn`) at S+5 for a confidentiality problem.
2. A failover restores the catalog to S, and the roots above S leave the head chain.
3. E's registration is still in `pending/`, since GC deletes a registration only when the head records
   its edition, and the new head does not.
4. The next release run finds E neither recorded nor tombstoned (design:311), within the window, its
   files protected as a registered edition, and its `previous` live at S.
5. The fold records E `embargoed` with its old, past `release_at`, and step 4 releases it in the same
   run (Q5).

The withdrawn edition goes public. The same path republishes every edition rolled back after S and
re-folds every registration of the last 35 days. Design:656, "an edition a restore discarded ...
stays protected while its root is in the window", is wrong as written: GC walks roots by `seq` from
the new head, and those roots are gone. Spec §6.7 names this hazard for a broad scan ("A broad scan
would re-release withdrawn editions"). The finding holds unless the restore's root S+1 tombstones the
dropped entries, which neither this design nor GH-125 part 3 states.

**Fix.** Make the fold restore-aware and list the change against spec §11.2 step 2 and §6.9. Either
record a registration created before the first root of the current `restore_epoch`, whose edition the
head does not record, as `embargoed` with `release_at: null` and log `fold.restored`, so only an
explicit release or `reinstate` publishes it; or refuse it until an operator decides, with a restore
runbook step that clears `pending/` through B1's discard. Correct design:656. Add a gating test:
restore to S, run the release job, and nothing cancelled after S goes live. Ask GH-125's restore
design to state whether S+1 tombstones the dropped entries.

## Major

### Embargo and release safety

**M1. The emergency stop cannot land during a run on an opted-in catalog.** `CMP-2`, `CB-104`,
`DES-4`. Design:146, 271, 415-417, 673-674, 718; spec line 691; hub `inventory-history-log.md:458-462`,
`:478-480`. A run takes the `HISTORY` lease at step 1 and can hold it up to 30 minutes through expiry.
D11 makes an admin commit wait 60 s, so `statcat admin embargo URL on` during a long run fails, and no
`set_embargo(on=True)` lands during a release commit at all. D4's "a concurrent `set_embargo(on=True)`
turns the retry into a held run" holds only on a catalog that has not opted in, and Q1 presents the
flag as the stop switch. The spec and the suite design say a `CatalogAdmin` call "waits for the run",
bounded by the 30-minute cap, so the 60 s is also an unlisted contract change. **Fix:** choose and
state the behaviour. `DES-4` proposes running expiry only in a run with no release time, so
release-time runs hold the lease for seconds. Size the admin wait to that run, print the holder and
the break runbook on giving up, and state the limit in Q1 and D4.

**M2. A past `release_at` publishes at the next run with no guard.** `DES-5`. Design:150, 294-295,
310, 337; `upload.py:249-254`, `:265-268`. Uploads at 06:00 with a mistyped
`--release-at 2025-11-12T13:30:00Z` are folded at the 08:00 pre-flight and, under Q5, released in the
same run, 30 minutes before the announced time. Nothing compares `release_at` with the registration's
time or the storage clock, Q5's trade-off covers only a late upload, and upload refuses a changed
`release_at` on rerun, so the producer cannot correct it. **Fix:** add the case to Q5. One rule
records a registration whose `release_at` is earlier than its last-modified time, or earlier than the
fold's clock by more than a configured bound, as `embargoed` with `release_at: null`, logged
`fold.release_at_past`, so only `statcat admin release` publishes it.

**M3. The access check fails open on anonymous access, paging and eligible group membership.**
`DES-6`, `ZO-3`, `IR-9`. Design:149, 554-591, 854-855, 955; spec line 958. D8 reads
`allowSharedKeyAccess` and ignores `allowBlobPublicAccess` and the container's public access level, so
a container set to public `blob` access serves every embargoed byte while the check passes. ARM and
Graph lists are paged (`nextLink`, `@odata.nextLink`), and D8 does not follow them. A principal
eligible through PIM for Groups for a group holding a blob-read role is no transitive member until it
activates, and whether O6 covers it is unstated. Q4's management-group scope has no source. Every test
uses a fake listing, so none of the listing's rules run before the first real release. Azure behaviour
here is the reviewers' knowledge, unverified. **Fix:** fail step 4 unless public access is off on the
account and container; follow every next-page link and fail on any page error; ask the owner whether
O6 covers eligible group membership; set Q4's scope from the real-account run; add
`tests/test_access_listing.py` (unit, `pytest-httpx`) with a row per rule; state the `api-version`
values.

**M4. Leaf-first records remove the manifest check from manual releases.** `CB-1`, `DES-14`.
Design:113-114, 470-474, 488-489, 666-687; `store/cross_product.py:157-175`, `:275-276`;
`admin.py:296-330`, `:384-428`. Today every incoming entry is read through `ManifestArtefacts`, which
hashes the manifest against `manifest_sha256` (`cross_product.py:170`). D6's `_records` returns a
filled entry's `artefacts` for the incoming entry too, so after Part 1 `CatalogAdmin.release` and
`rollback` check no manifest, on disk or on blob. Goal 2 then holds for the job's D4 step only, and
D6's cost paragraph counts a manifest GET that the rule no longer makes. Under the release identity,
`event.actor` also records the job's oid for every operator action. **Fix:** read or hash each
incoming entry's manifest in `refusals`, or add D4's check to `CatalogAdmin._commit_product`; align
the cost; test that a release with an altered or missing manifest is refused; record the operator in
`event.reason` or give operators an identity of their own (a §10.1 row).

**M5. The `final` check misses `previous: null` and a tombstoned `previous`.** `CB-103`, `DES-15`.
Design:317-318; spec §6.5 items 5 and 6; `_rules.py:13-15`, `:166-175`. D2 checks item 5 only "for a
`final` product with a `previous`". Fast-forward accepts `previous: null` when the product has no live
edition at that schema (a schema rollout's first edition), so a changed `final` product passes. A
tombstoned `previous` has no leaf entry, and GC may have deleted its manifest. **Fix:** compare with a
recorded edition's `content_fingerprint`, such as the live edition at any schema, whatever `previous`
is; refuse with `final` for a tombstoned `previous`; add both cases to row 953.

**M6. Q1 and S18 cannot be decided as written.** `CMP-1`, `CB-113`, `ZO-8`. Design:146, 164, 195,
420-424; spec lines 371, 460-466, 579. Q1 puts the flag's existing local meaning (a seal records
`embargoed`) and its new server meaning (the release step releases nothing) in one sentence, omits that
an explicit release still goes live, and does not say how a server catalog's flag starts. Spec §6.6
Retry already says "A stale automatic release must not write `live` after a concurrent change turned
embargo on", so D4's option "the release step ignores the flag, no spec change" contradicts the spec.
The compiler also notes that the spec's worked example (lines 460-466) shows a release job's `release`
version with `"embargo": true`, which S18 makes impossible, so S18 must amend the example. CUJ 1 is
titled "under embargo" on a catalog Q1 runs with `embargo: false`. **Fix:** a two-row table for local
and server catalogs, today and after S18; cite §6.6 as the text S18 makes explicit; add the worked
example to S18; mark the "ignores the flag" option contrary to §6.6; retitle CUJ 1.

### Correctness of the jobs

**M7. The release step reads every leaf in sequence, and each attempt starts cold.** `CB-101`,
`DES-12`. Design:305, 350, 398, 836, 842; `_reader.py:286-366`; `store/inventory.py:505-512`,
`:742-744`; GH-134 item 9. D4 builds `{p: full.leaf(p) for p in full.products()}`; `leaf()` fetches in
the calling thread, and only `leaves()` uses the 64-thread pool. `commit` builds a new `FullInventory`
on each attempt, so nothing is held. GH-134 measured the one-thread read at 51.12 s, where the NFR row
says 1 s and the release instant is what readers wait for. D2's "the reads of the first pass are
cached by path and reused" has no mechanism in today's writer, and `_retirable` calls `full.entries(p)`
per product with the same cost. **Fix:** call `full.leaves()` first in each callable; let the writer
refresh one held `FullInventory` across steps and races, or state the cache the fold uses; assert the
pool in Part 3's stores test.

**M8. Lease order, Part 3 on a 2.x catalog, and the lease on ordinary writes are unsettled.**
`CB-102`, `CB-16`, `IR-14`, `STR-6`, `CMP-5`. Design:269-271, 279, 673-674, 718-719, 880-884, 1006;
`store/inventory.py:570-575`; spec §6.9. Step 0 advances `CURRENT` before step 1 takes the lease, so a
second run writes `CURRENT` before it learns the lease is held, against "a second run fails to take it
and writes nothing". No error row or exit code covers a held lease. The writer decodes `catalog.json`
as `stage-1-writer` (`inventory.py:572`), so every commit on a 2.x catalog raises
`InventoryWriteRefusedError` until GH-125 stage 2, and Part 3's step 1 protects nothing. D11's
per-commit lease (a Part 2 decision) needs `AzureHistoryLease` from Part 6, and neither Part lists
`BlobInventoryStorage(lease=)` or tests an admin commit that waits or gives up. **Fix:** take the lease
before any write; state Part 3's behaviour on a 2.x catalog (refuse at step 0, exit 1, naming Part
6); move D11's lease bullet to D13 and Part 6, with files, tests and error rows.

**M9. A transient fault holds every due release, and exit 3 retries under a Kubernetes Job.**
`DES-3`, `ZO-9`. Design:269-292, 313, 345-350, 580-583, 752, 766, 835. At 08:30:05 a non-404 store
error in the fold, or a `ManifestError` from another product's unfilled entry, stops the run with exit
1 before step 4, and the 200 editions folded at 08:00 stay embargoed until the next scheduled time. A
429 from ARM holds them through step 2. A Kubernetes Job counts exit 3 as a failure and retries up to
`backoffLimit` (6 by default, unverified), so a standing refusal triggers repeated runs and "the release
job missed or failed". **Fix:** make a per-registration store fault a refusal; let a failed fold leave
step 4 to run; in the release-time run, release recorded entries before folding; retry the listing or
reuse a recent passing result; state the orchestrator's re-run rule; choose exit 0 for a completed run
with refusals, or state the `podFailurePolicy` Phase 8 must set.

**M10. Fold refusals are lost when step 4 does not run.** `CMP-3`, `IR-6`, `DES-7`. Design:115-116,
273, 396-397, 445, 580-583. D5 makes step 4 the only writer of a record that holds steps 3 and 4. A
failed access check skips step 4 and lets the fold run, so its refusals never persist, against Goal 3.
D1's table says step 4 does not run when the flag is on, and D4's sketch runs and writes the record.
**Fix:** one rule in D1, D4 and D5, for example "step 4 always writes the record and releases nothing
when held or unchecked"; add a test row for a fold refusal under a failed access check.

**M11. A step with nothing to write still writes a version.** `IR-5`, `CB-8`. Design:278-280,
393-404, 445-447; `store/inventory.py:667`, `:742-762`; `admin.py:125-126`. `commit` writes whatever
`Change` the callable returns, so D4's held path and any step that finds nothing after a lost race
write a version, and the idempotency gating tests rest on an unmade choice. `commit` takes no `now`,
`try_commit` reads its own clock (a second `HEAD` on blob), and the sketch uses `now` outside its
scope. **Fix:** a sentinel exception per step, as `prune`'s `_NothingToRetireError` does, or `commit`
writing nothing for `None`; either pass `now` through `Change` or drop "and to the root".

**M12. GC ignores the newest K roots.** `DES-10`. Design:616-621; spec §11.6 (line 1025). Spec says
objects reachable from the newest K roots are never deleted. A monthly catalog writes fewer than 10
roots in 35 days, and GC reads only roots back to the cutoff, so it deletes directories that roots
head-1 to head-9 name, the reader fallback's reach. **Fix:** read at least the newest `keep_roots`
roots whatever their age.

**M13. The retirement move leaves the change builder and its fallback behind.** `CB-4`, `CB-120`.
Design:595-602, 786; `admin.py:157-187`, `:640-682`. D9 moves `_retirable` only. The `retire` change
closure and `_NothingToRetireError` sit in `_prune_retire`, and `_cited`'s fallback (keep every
structure edition when a manifest cannot be read) is dropped, so an unfilled entry with a missing
manifest stops the run. Three signatures disagree. **Fix:** move the builder and the fallback together,
use one signature, and list both `admin.py` callers.

**M14. The error contract of `ManifestArtefacts(Fetch)` is unspecified.** `CB-5`. Design:475-479, 781,
896-899; `cross_product.py:133-178`; `admin.py:674-682`; `diagnostics.py:377-382`; `bench/corpus.py:164`,
`:170`. A `None` fetch, a digest mismatch and a `BlobStoreError` each need an outcome; two callers
catch `ManifestError` only; `decode_manifest`'s `source` needs a value; and "defaults to
`ManifestArtefacts(storage.read)` over any storage" contradicts "a `Path` keeps today's local read",
which the bench's `_root` and `_cache` rely on. **Fix:** `None` and mismatch raise `ManifestError`;
wrap `BlobStoreError` or widen both callers; name `source`; keep `ManifestArtefacts(root)` locally.

### Contracts, scope and order

**M15. Contract changes the design makes and does not list, and no cross-repo section.** `STR-4`,
`CB-116`, `DES-9`, `CB-104`. Design:157-169, 459, 529-531, 610-611, 703-709, 734-737. Changes to
documents other repositories own, absent from the S table: GC needs read and list on `products/` and
`pending/`, where §10.1 gives it delete only; B1's discard of a registration; B2's restore-aware fold;
M1's admin wait against "waits for the run"; M19's record-only version kind and export rule; m1's fork
key. Readers outside statcatlib consume S19's root key (pods in sdmxlib-rest and statcat-serve, Phase
8 alerting), D12's `EmbargoReader`, D14's event names and D7's `[expiry]` labels, and the design never
tests itself against the hub's rule that a cross-repository contract change goes to a suite design
first. **Fix:** add each as an S row; add a "Cross-repo contract" subsection naming writer and readers
per change and stating why the design stays local; route S23 as a revision of the suite design.

**M16. The S table gives no options or gates, and S15 is dropped.** `STR-2`, `STR-3`. Design:157-169,
177-186; `catalog-on-blob-storage.md:156`, `:189`. S rows carry Where, Change and Status only. S18 to
S20 borrow Q1, Q2 and Q6, and S21, S22's `--discard` and S23 have options nowhere. S13 says "Resolved
here" for a change to sdmxlib's spec. S18 does not say whether `release_due` changes in sdmxlib. Row 3
bundles six S rows into one unfiled PR. Phase 4's handoff row 8 sends S15 (the §4.6 "section only"
table) to GH-52 and gates Phase 5 on it, and this design neither resolves nor moves it. **Fix:** add
recommendation, consequence and gate columns, or point each S row at its D section; one handoff line
per S row; add S15 with a status.

**M17. The handoff table and the land order disagree on gates.** `STR-1`, `CMP-7`, `IR-13`, `STR-13`,
`CMP-20`. Design:177-183, 879-889, 912, 930. Row 1 gates Parts 3 and 4 on Q1 to Q8, while Q7 decides
`fold_lifecycle` (Part 1) and Q3 decides `jobs.py` (Part 2). Row 3 gates Parts 3 and 4 on the spec PR,
while S13 is built in Part 1 and S21 fixes keys in Part 2. "Parts release in order" contradicts Part 4
landing before Part 3, and "land" and "release" are undefined. **Fix:** one gate per Q and S row (Q7
and S13 gate Part 1; Q3 and S21 gate Part 2); match "Waits on"; define land and release.

**M18. Part 6 is written against an interface no design defines yet.** `STR-5`, `ZO-1`, `IR-11`,
`CB-121`. Design:184-186, 711-732, 884, 995-1012, 1034; GH-125. D13 says "If GH-125's design names
these differently, Part 6 follows it", and that design is unwritten. Nothing in Part 6 can run before
it: the writer refuses a 2.x catalog, no server catalog has opted in, and gate 1's example is
2027-01-15. No plan gating test names expiry, and GH-52's closure would wait on GH-125. The listed
interface also lacks step 1's ranged read, and no row checks reachability on blob. **Two fixes, the
owner picks:** move D13, Part 6, S23, `[expiry]`, step 1 and D11's lease paragraph to GH-125's stage
2 design, keeping D1 step 6 as a named slot, so this design closes on Parts 1 to 5 (`ZO-1`; no gating
test or owner decision touched, and Part 6 already waits on that work); or keep Part 6 here marked
provisional, name the blob expiry storage, add the ranged read and a reachability row, and add a
handoff row to re-review it after GH-125's design lands.

**M19. The refusal record's cost and kind are unexamined.** `ZO-2`, `DES-7`, `DES-8`, `CB-114`.
Design:147, 165, 426-462; spec lines 374, 528, 678, 962, 1032. A version written for the record alone
takes kind `release` with empty `changes`, where spec §6.2 reserves `describe` for an
`extensions.statcat`-only change. Each record-only version uses a `keep_roots` slot. Segment lines keep
`extensions` for good, so up to 150 KB a root enters `history/` under object lock. A refusal's reason
names embargoed ids and fingerprints, and the spec's live-only export would carry them if it copies
`extensions.statcat`. The 500 cap drops the highest product ids whatever their age. **Two fixes, the
owner picks:** keep D5, write record-only versions as `describe` (or add the `release` use to S19),
add an S row that the live-only export drops `release`, cap by age, and add the segment growth to the
costs; or answer plan:350 with "each run recomputes and reports them", since the fold and
`release_due` refuse the same entries every run and `since` is derivable, and replace S19 with one
spec sentence (`ZO-2`; touches Q2 and Goal 3, no gating test).

**M20. The public surface exceeds the gating tests.** `ZO-5`, `ZO-4`, `ZO-6`, `ZO-7`. Design:260,
607, 644-647, 676-689. The design adds 8 commands, 11 flags, 5 modules, about 20 public symbols, 17
keys, 2 format additions, 7 external document changes and 8 owner questions, and weighs none against
the gating tests. `--forecast` needs cross-run state and changes deletion behaviour beyond §11.4's
alert; `--report` writes into a pod's disk; `admin rollback` has no Phase 5 caller before Phase 9.
**Fix:** add `ZO-5`'s surface table, and let the owner rule on each cut: `--forecast` to Phase 8,
`--report` dropped, `admin rollback` to Phase 9, Q6 and S20 to an issue. `--discard` stays and widens
(B1).

### Implementer decisions left open

**M21. The job objects have no clock, actor or storage seam.** `IR-1`. Design:666-668, 696, 785-791,
854-855; rows 949, 954, 973-979; `store/blob.py:559-564`, `:602-611`. Over `MemoryStore` every unit row
stops at the first `now()`, over Azurite `actor()` refuses for the jobs, and GC's age rows need files
older than 24 h or 35 d. The compiler adds that an injected `clock=` skips the skew comparison
(`blob.py:559-560`), so row 954 needs the HTTP `HEAD` path. **Fix:** a `storage=` seam on `ReleaseJob`
and `collect_garbage`, `clock=` on `CatalogAdmin.open`; row 954 through `pytest-httpx` answering the
`HEAD`; a GC clock or short windows for the age rows.

**M22. The CLI wiring and the environment rule are unstated.** `IR-2`, `CB-17`. Design:269, 563, 688,
722, 789; `src/statcatlib/EGM.md:386-388`; `cli/EGM.md:83-84`; `cli/__init__.py:635-694`. No layer is
named for building `AzureBlobRest`, `AzureReaderListing` and `KubernetesPodVersions`, nor the listing's
credential. An in-cluster client in `release.py` reads the environment, against the library rule.
"No API" has no detection rule. The reused CLI helpers hard-code upload's messages and its one-hour
timeout. `statcat gc` has no exit-code table or CLI test. **Fix:** the CLI builds and passes all three;
name the credentials and the detection rule; generalise the helpers; add GC's exit codes and
`tests/test_cli_gc.py`.

**M23. Output contracts have no schema.** `IR-3`. Design:260, 276, 282-286, 545, 640-647, 736-737,
789-791. The report, the deletion-log line, the plan `--forecast` reads, each D14 event, `ReleaseReport`
and `GcReport` have no fields, and all share standard output with the human lines. **Fix:** a field
table for each, and a stream per output.

**M24. Named symbols have no definition.** `IR-4`. Design:397, 559-560, 718-729, 774-795, 324, 623.
`record_ext`, `ReaderReport`, `AccessResult`, `ReleaseRefusal`, `AzureHistoryLease`'s interface, the
three REST verbs, `PodVersions`, the blob expiry storage, `_ObjectOps.list`'s batching and prefix, a
blob `structure_editions`, and a home for the shared registration decoder if Part 4 lands first.
**Fix:** complete the Symbols table, and put shared decoding in a module both jobs import.

**M25. The fold's reference-check call is open.** `IR-7`, `CB-110`, `ZO-10`. Design:319-331, 349-350;
`_check.py:203-218`, `:269-271`; `upload.py:229-238`. `remote=` and `lookup=` have no values; with
`remote=` the registry opens through an engine that may need the store's secret; an empty
`structures=` raises `ValueError`, as for a catalog's first structure product; the retry rule for the
check after a lost race is unstated; and the structure pulls are missing from the cost row. **Fix:**
write the call as code; pass `structures=None` when empty; state the retry rule; pull into
`<tmp>/products/<p>/<e>` and call `structure_editions` and a shared `_check_structures` loop.

**M26. `EmbargoReader` has no way to build an `EditionView`.** `IR-10`, `CB-111`, `CB-19`.
Design:693-709, 986; `store/EGM.md:54-55`; `catalog.py:1018-1027`; `_check.py:324-333`;
`tests/test_public_api.py:179-188`. `EditionView` is built only by `Product.view` and
`WorkingCopy.view`. `check_on_open` needs `stamps=`, and a stamps directory names embargoed editions;
`references=False` is unstated; the core import must stay free of the blob extra. **Fix:** name the
extracted view function, add a stamps directory beside `cache_dir`, state `references=False` and the
lazy imports, and add the refusals and lifecycle to D12.

### Tests

**M27. Existing tests and pages that assert the Phase 4 refusals are missing from Parts 1 and 2.**
`CB-2`, `IR-8`. `tests/test_blob_storage.py:719-735` (two tests expect `NotImplementedError` naming
Phase 5); `tests/test_inventory_routing.py:113`, `:133`, `:160` (a data entry's `artefacts` is `None`);
`tests/test_blob_open.py:170-172` (`CatalogAdmin` refuses a `BlobLocation`); `docs/api/store.md:364-366`.
Each Part fails `just ci` until they change. **Fix:** list each as Modify, and widen design:886-888 to
statements a Part makes false.

**M28. Test files, tiers and marks are wrong or missing.** `CB-3`, `CB-11`, `IR-8`, `STR-19`.
Design:905-1012. `tests/test_inventory_routing.py` and `tests/test_catalog_admin.py` are integration,
not unit; `tests/test_cli_verify_doctor.py` is unit. `record_uploaded` runs against Azurite in a
non-stores file. The lease and delete rows need a signing seam, since `KeyedRest` overrides only
`_send(path)` for `HEAD`. Fifteen new files lack a Create mark and a `pytestmark`. The fake listing
sits in `tests/stores/`, which no unit test imports. **Fix:** correct each tier or file, add
`_send(method, path, headers)`, mark every file, and move the listing fake.

**M29. The 200-table release test lacks grouped products and the stub pre-materialisation.**
`CB-105`, `IR-22`. Design:835, 939-940, 946; plan lines 106, 177; GH-52 comment of 2026-11-02. The plan
mixes one-table and grouped products, "where each per-product refusal of `release_due` holds back
several tables", and runs a stub pre-materialisation between the runs. **Fix:** build the mix, put a
refusal in a grouped product, run the stub through `EmbargoReader`, and state the counts asserted.

**M30. The GC registered-edition test passes without testing the rule.** `CB-106`. Design:974, 1029;
GH-52 comment of 2026-11-05. The comment specifies "a registration older than the grace period"; the
row does not age the files, so the grace period alone keeps them. **Fix:** files older than
`upload_grace`, by M21's seam, and the converse with no registration.

**M31. Stated behaviours have no test row.** `STR-10`, `IR-18`. The release step's manifest check
(half of Goal 2); `--dry-run` writing nothing; step 0's `CURRENT` advance; each D14 event; the fold's
`kind` refusal and a cross-product refusal at the fold; the fold recording `artefacts`; GC's dry run,
deletion log, manifest-first delete and untouched `inventory/`; `_ObjectOps.list`;
`EmbargoReader.view` of a `live` entry and `refresh`; a successful opt-in on Azurite;
`KubernetesPodVersions` against a fake API; the 30-minute cap. **Fix:** a row each, with file and tier,
and Goal 2 in the acceptance map.

### What a reader will get wrong

**M32. Unconfirmed items have no list, and the risks miss most of them.** `STR-8`. Design:568-570,
857-871, 1037-1044. Only `atScope()` is marked. Unrun on a real account: eligibility schedules at
`atScope()`, `transitiveMembers` under PIM for Groups, Reader's sufficiency at the management group,
in-cluster pod listing and its RBAC, `sdmxlib_version` on every pod, the lease and `If-Match` delete
over `httpx`. Missing failure modes: a permanently missing listing permission stops every automatic
release; `--discard` names the wrong edition; the release job gains pod-read rights; a `final`
product waits on Q8. **Fix:** a "Not confirmed" list with a risk row and a real-account step each.

**M33. The NFR section has no memory, disk or per-module coverage entry.** `STR-9`. Design:827-855.
The fold holds about 20 MB of filled leaves, GC the kept set of about 350 roots, and the fold's
temporary directory pulls every structure product's editions, with no bound stated; `--state-dir` and
`EmbargoReader`'s `cache_dir` also lack one. The Coverage line claims a unit tier for every module
while the listing, lease and pod clients have none (M3). **Fix:** Memory and Disk entries with figures,
the structure pulls in the cost row, and a per-module coverage table.

**M34. Window names and stage 2 terms are inconsistent or undefined.** `CMP-4`, `CMP-5`, `STY-1`,
`STY-2`. Design:41, 80, 218, 229-230, 271-276, 507-515, 618, 656, 961, 979. "The window" stands for
four keys; `retire_after` is also "the retire window"; "2.x catalog" and "opted in" name one state
undefined; `HISTORY`, `expired_through`, segment and epoch are undefined; `inventory_window`,
`keep_roots`, `backup` and `pod_staleness` are unexplained, and `tombstone_window` says "tombstones
kept" though nothing drops one. **Fix:** a retention-values table in D7 (what each governs, who reads
it, first Part, spec name), Terms rows for the stage 2 objects, and key names everywhere.

**M35. Foreign identifiers collide or have no source.** `CMP-6`, `STY-3`, `STR-12`, `CB-10`,
`CB-108`. Design:9, 28, 62-65, 85, 230, 232, 310, 357, 668, 721, 762. "Two keys in D7's order" points at
this design's jobs configuration; the order is `upload._pending_document` and spec §11.2 step 1, which
also requires compact bytes, `Z` and milliseconds. O4 echoes Phase 4's O4, "Q9" is the plan's, "GH-19"
is the suite tracker, bare `#383` to `#385` mean the archived GitLab project (`CLAUDE.md:823-827`), and
W3, P1, P7, H20 to H23 and S12 have no named document; W3 appears in a user-facing error. **Fix:**
qualify each at first use or add a prefix map to the Inputs; have the fold re-encode a registration
and refuse when the bytes differ.

**M36. The Overview gives no outcome per Part, and the 0.94 s is for unfilled leaves.** `CMP-8`,
`CMP-9`. Design:24-25, 46-65, 236, 485-489, 830-836. A reader cannot learn that Part 1 changes every
local seal and grows leaf entries about four times, that CUJ 1 needs Part 5, or that nothing deletes
on a server before Part 4. The header, O8 and the NFR present 0.94 s as Phase 5's leaf read, while D6
says the real account read the corpus without records (GH-134 does not say which corpus). **Fix:** "After
Part N, a user can" per Part; say the measurement is for unfilled leaves and give the filled estimate
or the fallback if Part 3's test misses plan Goal 1.

**M37. The orchestrator and the operator's identity are undefined.** `CMP-12`, `CMP-13`, `ZO-7`.
Design:41-43, 197-211, 242, 288-299, 601, 676-686, 752. "The orchestrator" runs at each release time
and is never defined against the CronJob, nor how it learns release times. "Only the release job writes
the Inventory" conflicts with six `statcat admin` commands "under the release identity", with no
mechanism for an operator to hold it. **Fix:** a Terms row for the orchestrator with what it must know;
write "only the release identity writes"; state how an operator runs `statcat admin` on a server.

## Minor

| # | Reviewers | Target | Finding | Proposed fix |
|---|---|---|---|---|
| m1 | `DES-9` | Design:335-336; spec §11.2 step 2 | The fork rule ignores `schema_version`, refusing a rollout's republish beside the daily edition | Key on `(previous, schema_version)` as `supersede_pending` does; an S row |
| m2 | `DES-11` | Design:634-639 | "Whose edition the head records" omits `retired`; a manifest-less directory is never reported | "In a leaf or in `retired`"; report and allow `--discard` for it |
| m3 | `DES-13`, `CMP-11` | Design:722; hub H20 and gates section | "No API" does not separate a 403 from no cluster; H20 and the hub's gates text disagree | "Not run" only with no service-account token; a hub question |
| m4 | `DES-15` | Design:341-346 | Dropping a refused B leaves A withdrawn by `supersede_pending` | Drop the product's whole leaf change |
| m5 | `IR-23` | Design:308-343 | Which leaf the `chain` and fork checks read for chained registrations | State the leaf and the order of checks |
| m6 | `CB-6` | `working_copy.py:1661-1666` | `_may_be_refused` reads other manifests, bypassing `_records` | Route it through the records; Part 1 Modify |
| m7 | `CB-7` | Design:470-473, 493 | Six read sites change, against "four functions" | Name them |
| m8 | `CB-9` | D2, D4, D10 | `_decode_checked`, `_refuse_unsafe_ids`, `_read_remote_manifest`, `upload._lookup`, `edition_kind`, `precheck_refusals` are re-specified | Name and reuse them |
| m9 | `CB-12` | `admin.py:248-249`, `:325`, `:650` | `__repr__` raises on blob; two clock calls; `advance_current` cannot report a no-op | `storage.location`, `storage.now()`, state the protocol |
| m10 | `CB-13`, `IR-15`, `STR-19` | Docs lines of each Part | No nav for three pages, no reference pages, `docs/api/admin.md` untouched, exports, `upgrading.md`, commit types | List each per Part |
| m11 | `CB-14`, `IR-21` | `_stores.py:383` | `record_uploaded`'s `"revisable"` default bypasses `fold_lifecycle` | `Lifecycle \| None = None` |
| m12 | `CB-15` | `test_public_api.py:199-222`; `_stores.py` | No `list` route case; ops classes do not record LIST; `_send` hard-codes `HEAD` | Add them; state the `_send` change |
| m13 | `CB-18` | `bench/corpus.py:126-176` | The bench meter's meaning changes after Part 1 | Re-run or annotate the results |
| m14 | `CB-107`, `CMP-10` | Design:207-209, 285-286 | CUJ 2 names a `fast-forward` rule the record holds as `null`; the `REFUSED` line has no null form | Align CUJ 2, or call `release` per edition to keep the rule |
| m15 | `CB-109` | Design:314 | Equality with `CANONICAL_FORM` is narrower than "one the release job knows" | State why it suffices |
| m16 | `CB-112` | `_rules.py:268-274` | `record` relabels the leaf's lifecycle, so D3's mismatch row is the only guard | State it; test before `record` |
| m17 | `CB-115` | Design:718, 768 | The 30-minute cap's start and its effect during steps 3 to 5 | State both |
| m18 | `CB-117`, `STR-18` | D14, D10, Errors | 503s, a root failing validation and an unregistered upload are unmapped; `expiry.pod_check_not_run` and `jobs.config` missing; GC exit codes | Complete the tables |
| m19 | `CB-119`, `STR-14` | Design:8-28, 236 | O8 dated 11-10 (the decision is 11-06's); sdmxlib#382 unnamed; issue snapshots undated; spec revision unpinned; §6.2 and §6.6 missing | Correct and pin |
| m20 | `CB-123` | Design:63; plan:358 | The plan's end of Phase 2a's P7 is missing | State where it ends |
| m21 | `STR-11`, `CB-118`, `DES-17` | D6; GH-125 part 4 | D6 departs from GH-125's leaf-backed reader; part 2's server pieces unlisted; GH-130's `artefacts=` | Record it; handoff rows for both issues |
| m22 | `CB-116`, `DES-16`, `IR-16` | D7 | The example allowlist omits pods, search pods and preview; required sections and instant types unstated | Match §10.1; state required keys |
| m23 | `ZO-11` | D7 | A duration grammar where integer keys with units suffice | `gc_window_days = 35` and similar |
| m24 | `IR-17` | Design:887-889 | The `Any` instruction lets each Part raise the baseline | No new `Any` in `src/` |
| m25 | `IR-19`, `STR-17` | Acceptance map; Goals | Wrong file for the allowlist item; GH-52's own items unmapped (the 404 test moved to Phase 6); no goals for Parts 2 and 5 or the access check | Map and add |
| m26 | `IR-20` | Errors | `ManifestError` from an unfilled entry, a failed structure pull, a 2.x catalog, `InventoryCommitUnknownError`, GC's missing-root class; exit 2 overloaded | A row each; reconcile exit 2 |
| m27 | `IR-24` | D8, D13 | Version parsing, a pod with no IP, member types, wildcard semantics, extras and lazy imports | State each |
| m28 | `STR-15`, `STR-16` | Throughout | Future work scattered; D10 and row 979 conditional on Q6; the `lifecycle` Non-Goal conditional on Q8 | A Future work list; write for the recommendations |
| m29 | `STR-7` | D8, D9, D11 to D14; design:815 | Six decisions have no options table, and design:815 says each does | Add them, or one line where nothing is left to choose |
| m30 | `CMP-14` to `CMP-21` | Throughout | Step numbers from four sequences; "kind" and "gate" with two and three senses; numbers without derivation; missing Inventory vocabulary; Q3, Q6 and Q7 detail; small signature and key-name mismatches | `comprehension_report.md` has each |
| m31 | `STY-1` to `STY-15` | Throughout | No heading ends in `?` and no `PHRASES` match, so the prose check passes. Two names for one thing, dense cells, repeats, passive rules, fragments, punchline endings, mixed polarity in D2's check column | `style_report.md` has each rewrite |

## Q1 to Q8 and the proposed contract changes

| Item | Decidable as written | What the owner needs added |
|---|---|---|
| Q1 | No | M6's two-row table, §6.6 cited, the worked example in S18, M1's lease limit |
| Q2 | Yes, with gaps | M19's kind and export rows, and `ZO-2`'s recompute option beside the root record |
| Q3 | Mostly | One plain sentence for the alternative (`CMP-19`) |
| Q4 | Partly | The scope's source (M3) |
| Q5 | No | M2's past `release_at` case |
| Q6 | No | B1's discard path, refusals other than age, and the cost of a no (`CMP-19`) |
| Q7 | Yes | "0.17, the release with Part 1", and Part 1 gated on it (M17) |
| Q8 | Yes | none |
| S13 | Yes | A handoff line and spec PR; its status is "proposed", since sdmxlib owns §11.2 |
| S18 | No | M6 |
| S19 | No | M19 |
| S20 | Partly | B1 |
| S21, S22, S23 | No | Options and gates (M16); S23 as a suite design revision (M15, M18) |
| Unlisted | n/a | GC read on `products/` and `pending/`; registration discard; restore-aware fold; admin lease wait; record-only version kind; live-only export; fork key (M15) |

## Findings for other repositories

### sdmxlib spec

| Finding | Location | Change |
|---|---|---|
| B1 | §11.2 step 2, §11.4 | GC deletes a registration on an operator's decision; which registrations the fork rule counts |
| B2 | §11.2 step 2, §6.9 restores | The fold's rule for registrations older than the current epoch's first root |
| M1 | §6.9 (line 691) | Whether `CatalogAdmin` waits for the run or for a bounded time |
| M6 | §6.2 `embargo`, §6.4, worked example lines 460-466 | S18, citing §6.6 Retry |
| M15 | §10.1 table | GC's read and list on `products/` and `pending/` |
| M19 | §6.2 `event.kind`; §10.1 live-only export | The kind of a record-only version; the export drops `extensions.statcat.release` |
| m1 | §11.2 step 2 | The fork key includes `schema_version` |
| M16 | §4.6 line 277 | Phase 4's S15, still open |

### Hub

| Finding | Location | Change |
|---|---|---|
| M18, M15 | `docs/design/inventory-history-log.md` | S23 as a revision with its own review, or the server half moved with GH-125's stage 2 design |
| m3 | H20 and "Expiry gates" | The two texts disagree on gate 1 alone without the Kubernetes API |
| M1 | Lines 461-462 | Align with whatever M1 decides |

### statcatlib issues

| Finding | Issue | Change |
|---|---|---|
| m21 | GH-125 part 4 | The leaf-backed reader replaced by D6 |
| B2, M18 | GH-125 parts 2 and 3 | The restore's handling of dropped entries; where the server half is designed |
| m21 | GH-130 | `reinstate` passes `artefacts=` |
| m25 | GH-52 | The 404 test moved to Phase 6 |

## Sound under review

- The commit retry behind Q1: `commit` re-reads the head and re-applies the change after each lost
  race (`store/inventory.py:742-762`), so a `set_embargo(on=True)` during a release commit on a catalog
  that has not opted in yields a held run.
- A cancel or rollback landing during a release commit is re-evaluated; `release` requires `embargoed`
  and the fast-forward check (`_rules.py:297-310`).
- The fold's idempotency through O1, with `record` refusing `duplicate` and `tombstone` behind it.
- The refusal record's survival across `describe` (`store/inventory.py:269`; `admin.py:508-514`).
- GC's read order (Inventory, registrations, LIST), and `inventory_window >= gc_window + 1d` keeping
  GC's walk clear of root expiry.
- `edition_files` covers all three tiers by default, and `_refuse_unsafe_files` keeps a `data_path`
  inside the product directory.
- D7's inequalities match spec §11.6.
- D3: `_section_findings` ignores unknown keys (`bundle.py:353-377`), so the section stays format 3.
- D6: `verify_entry` compares a filled `artefacts` with the manifest (`_routing.py:131-149`).
- D9 recomputed inside the commit callable cannot retire an edition a concurrent rollback made live.
- Every statcatlib `file:line` the Problem section cites, and the `content_fingerprint` and
  `canonical_form` grep.
- Every plan Phase 5 gating test has a row (`codebase_details_sdmxlib_report.md` has the table), with
  the defects above.
- The recorded decisions O1 to O7 match their sources; O8's content matches GH-134.

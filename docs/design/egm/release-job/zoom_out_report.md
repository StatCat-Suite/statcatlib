# Zoom-out report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`.

## Verdict

No Blockers. The design answers GH-52's four questions: the shape of `statcat release` (D1), the
retention schema (D7), where refusals are persisted (D5), and S13 (D3). It places GH-125 part 4 (D6)
and the server half of part 3 (D13), and maps every plan Phase 5 gating test to a test
(design:1016-1035). It is larger than the gating tests and GH-52 need: 8 commands, 11 per-command
flags, 5 new modules, about 20 new public classes and functions, 17 configuration keys, 2 format
additions, 7 proposed changes to other repositories' documents and 8 owner questions (ZO-5).

Proposed cuts:

- Part 6 and everything that serves only it, moved into GH-125's stage 2 design. Part 6 already waits
  on that work, so its schedule does not change (ZO-1).
- The root refusal record (ZO-2).
- Two GC deletion paths, `--discard` and Q6 (ZO-4).
- `--forecast`, `--report` and `admin rollback` (ZO-6, ZO-7).

None touches a plan Phase 5 gating test or goes against an owner decision; cutting `--discard` keeps
GC at O5's "alert only". The access precondition is the one place where the design is thinner than
its risk (ZO-3).

Reviewed and kept as designed: D8's ARM and Graph listing (O6 and §10.1 require it); GC's read back
through the window (§11.4 requires it, and nothing cheaper is safe once GH-130's `correct` lands);
Q5's same-run release (steps 3 and 4 already do it, plus one event); the repeated
structure-reference check (correctness needs it; ZO-10 proposes reuse); D3, D6 and D9.

| Surface | Now | After the cuts |
|---|---|---|
| Commands | 8 | 7 |
| Per-command flags | 11 | 5 |
| New public classes and functions | about 20 | about 15 |
| Configuration keys | 17 | 12 |
| Format additions | 2 | 1 |
| Spec and suite changes | 7 | 4 (S13, S18, S21, S22's log half) |
| Owner questions | 8 | 7 |

## What I read

- The persona `agents/zoom_out.md` and `zoom-out-standard.md` of the EGM plugin.
- The target in full.
- The plan, lines 322-446 (Phases 4 to 9 and the verification plan); the spec §6.2, §6.4 to §6.9, §10
  and §11.
- The issue texts for GH-52, GH-125, GH-134, sdmxlib#381 and the suite tracker.
- The hub's `inventory-history-log.md`, lines 505-554 and 820-944.
- statcatlib `CLAUDE.md`, `src/statcatlib/EGM.md`, `store/EGM.md`, `cli/EGM.md`; code in
  `store/inventory.py` (250-430, 690-765), `store/cross_product.py` (110-170),
  `store/working_copy.py` (210-339), `catalog.py` (320-380), and searches in `bench/corpus.py`,
  `admin.py` and `cli/__init__.py`.
- Phase 4's `zoom_out_report.md`, and searches of its `review_report.md` and the Phase 4 design
  (W10, W19 to W21), which show the owner accepted most round 1 cuts.

## Limits

- Nothing was run, and sdmxlib's source was not read; claims about `release_due`, `record` and
  `supersede_pending` are the design's.
- The Azure and Kubernetes points in ZO-3 and ZO-9 come from the reviewer's knowledge of the
  documentation and are marked "verify": paging, PIM for Groups, the permission `atScope()` needs,
  Job retry behaviour.
- The byte and day figures in ZO-1, ZO-2 and ZO-4 are arithmetic on the design's figures.

## Blocker

None.

## Major

### ZO-1. Server expiry (Part 6) belongs in GH-125's stage 2 design

**Sites:** design:6, 41-42, 55, 65, 169 (S23), 185-187, 254, 260, 271, 275, 526-532, 673-674,
711-732, 793-795, 869, 884, 995-1012, 1034; plan:358; hub `inventory-history-log.md`:523-528,
850-853; GH-125; spec:633, 713; `store/EGM.md`:472.

**Problem:** Part 6 lands only after GH-125's local stage 2, which has no design yet (design:185).
D13 writes against an engine interface it has to guess ("If GH-125's design names these differently,
Part 6 follows it", design:729). Nothing Part 6 builds can run before then: the blob writer refuses a
2.x `catalog.json` (`store/EGM.md`:472), no server catalog has opted in, and gate 1's example
`not_before` is 2027-01-15. No plan Phase 5 gating test names expiry; the four rows at
design:1006-1009 are GH-125's acceptance items. Part 6 carries `AzureHistoryLease`, `LeaseLostError`,
three `AzureBlobRest` verbs, `BlobInventoryStorage(lease=)`, `PodVersions` and
`KubernetesPodVersions` (in-cluster RBAC and HTTP to each pod), opt-in over a `BlobLocation`, the
persisted leaf-set cache and `--state-dir`, five `[expiry]` keys, step 1, D11's 60 s lease wait, and
S23. S23's case also needs work: GC reads about 560 MB of roots nightly with no cache and the design
accepts it (design:656, 839, 867); expiry run once a day without a cache costs one 1.7 to 4.7 GB read
a day and needs no stateful CronJob.

**Proposal:** End this design at Part 5, keeping step 6 in D1's table as a named slot ("expiry,
GH-125") so the run's order is fixed now. Keep GC's stop at `expired_through` and its test
(design:978). Move D13, Part 6, S23, `[expiry]`, step 1 and D11's lease paragraph into GH-125's
stage 2 design, and decide the cache there against "expiry runs once a day without a cache".

**Touches:** No gating test and no owner decision (O4 holds wherever Part 6 is written). Sequencing
text changes in plan:358 ("Stage 2 ships here"), hub `inventory-history-log.md`:852, and design:6.
GH-125's four server acceptance items stay on GH-125. No schedule risk, since Part 6 already waits on
handoff rows 8 and 9; server roots accumulate until stage 2 either way, about 16 MB a day at 10
versions a day.

### ZO-2. The refusal record in the root

**Sites:** design:76, 115-116, 147, 165, 208-209, 246, 273, 393-401, 426-462, 741, 864, 959; spec:374,
528, 678, 1032; plan:350.

**Problem:** Every refusal can be recomputed from the head, `pending/` and the store: the fold refuses
an unrecorded registration again on every run, and `release_due` refuses a due embargoed entry again
on every run. Every run, `--dry-run` included, reproduces the current set with reasons. The stored
copy adds only `since`, which can be derived (a release refusal from the entry's `release_at`, a fold
refusal from the registration's last-modified time). Storing it costs:

- versions that release nothing, written as kind `release` with no leaf change (design:445-447);
  spec:374 makes `describe` the kind for a change to `extensions.statcat` alone, so a correct kind
  needs a further spec change;
- roots larger by up to 150 KB, with every copy kept for good in history segment lines (spec:678);
- record-only versions that consume `keep_roots`, which at 10 covers 1 to 3 days;
- the 500 cap, `omitted`, the `since` carry, the sort rules and their tests.

**Proposal:** Answer plan:350 with "each run recomputes and reports them": the `REFUSED` lines, the
`fold.refused`, `release.refused` and `release.overdue` events with product, edition, rule, reason
and derived `since`, and `ReleaseReport.refusals`. Make `statcat release --dry-run` the operator's
view, and rewrite CUJ 2 to read it. Drop D5, S19, the cap and the record-only version. If the owner
wants an in-catalog copy before Phase 8, keep D5, derive `since`, and add the kind question to S19.

**Touches:** Q2's recommendation. Spec §6.4's "persisting each refusal's reason" (spec:528) and
§11.6's "persisted refusal reasons" (spec:1032) need one sentence saying recomputation and the job's
log satisfy them, in place of S19. Goal 3 needs rewording. No gating test. Risk: before Phase 8, a
refusal's history lasts as long as the platform keeps pod logs.

### ZO-3. The access precondition is thin for an embargo control

**Sites:** design:149, 554-591, 861-863, 955, 1042-1043; spec:958; plan:356; O6.

**Problem:** D8 is the only control between the container's readers and the embargo. Four cases fail
open or are unstated.

1. **Anonymous access.** Step 4 reads `allowSharedKeyAccess` and ignores `allowBlobPublicAccess` and
   the container's public access level. A container with public `blob` access serves every embargoed
   byte to anyone, and no role assignment shows it.
2. **Paging.** ARM role assignment lists and Graph `transitiveMembers` return pages (`nextLink`,
   `@odata.nextLink`), and D8 does not say the listing follows them. A first-page-only listing passes
   with principals missing.
3. **Eligible group membership.** `roleEligibilityScheduleInstances` covers eligible role
   assignments. A principal eligible, through PIM for Groups, for membership of a group holding a
   blob-read role is not a transitive member until it activates. Whether O6's "eligible assignments"
   covers this is unstated (verify against Graph's `privilegedAccess/group/eligibilitySchedules`).
4. **Scope of the release identity.** Q4 grants Reader at the management group with no source showing
   that scope is needed. Verify whether `roleAssignments/read` at the account, queried with
   `atScope()` at the container scope, already returns inherited assignments.

**Proposal:** Fail step 4 on `allowBlobPublicAccess` or the container's `publicAccess`, both from GETs
the check already makes. State that every list follows its next-page link and an error on any page
fails the check, with unit rows for a second page holding an unlisted principal and for a public
container. Ask the owner whether O6 includes eligible group membership. Set Q4's scope from the
real-account run and record it.

**Touches:** The gating test "A principal outside the allowlist stops the release", which gets
stronger; O6 (a confirmation, reversing nothing); Q4. Risk if left: an embargo leak while the check
passes.

### ZO-4. GC deletion paths the plan does not ask for: `--discard` and Q6

**Sites:** design:151, 166, 168, 233, 607, 635-639, 979-980; spec:1008-1010, 1032; plan:358, 360.

**Problem:** Two new deletion paths are each argued in one sentence. `--discard p/e` deletes, "as a
retired one", an edition no Inventory version records and no registration names. O5 makes such an
edition "an alert only", and the plan's sweep "reports each for an owner's discard-or-reinstate
decision" (plan:360); neither asks GC to carry out the discard. D10 does not say the named edition is
checked again at deletion time. Q6 and S20 let GC delete a refused registration older than the
tombstone window, 400 days in D7. No server catalog holds production data yet, so the first
registration reaches that age about 13 months after the first server upload.

**Proposal:** Cut `--discard`, S22's second half, Q6 and S20. Runbook for `gc.unregistered`: re-run
`statcat upload` without `--release-at`, so the fold records the edition `embargoed` with
`release_at: null`; run `statcat admin cancel`; retirement and GC remove it. File one issue for both
paths.

**Touches:** O5 (kept at "alert only"), Q6 and S20 (withdrawn), half of S22, the second halves of the
rows at design:979 and 980. No gating test. Risk: a discarded edition takes `retire_after` plus the
GC window to go, about 125 days at D7's values; a refused re-upload keeps alerting until the issue
lands; an unregistered edition's data files still go after the 24 h grace (spec:1010).

### ZO-5. New public surface against the gating tests

**Sites:** design:260, 607, 676-689, 497-546, 774-795, 157-169, 736-751; plan:352-376.

**Problem:** The design never weighs its added surface against what the gating tests need.

| Surface | In the design | Needed by a gating test, plan bullet or spec text | Proposed cut |
|---|---|---|---|
| Commands (8) | `release`, `gc`, `advance-current`, `admin editions`, `admin release`, `admin cancel`, `admin rollback`, `admin embargo` | `release`, `gc`, `advance-current` (gating tests); `admin release` (§11.6 manual release); `admin editions`, `admin cancel` (CUJ 2); `admin embargo` (Q1's stop switch) | `admin rollback` (ZO-7) |
| Per-command flags (11) | release: `--config`, `--dry-run`, `--state-dir`, `--report`; gc: `--config`, `--dry-run`, `--forecast`, `--discard`; admin: `--reason`, `--to`, `--if-live` | `--config` on both; gc `--dry-run` (plan:358); release `--dry-run` (ZO-2); `--reason` (§11.6) | `--state-dir` (ZO-1), `--report` (ZO-7), `--forecast` (ZO-6), `--discard` (ZO-4), `--to` and `--if-live` (ZO-7) |
| New modules (5) | `jobs.py`, `release.py`, `access.py`, `gc.py`, `embargo.py` | all five | none |
| New public classes and functions (about 20) | D7, D8, D10, D12, D13 symbols | all except the expiry ones | `ExpiryPolicy`, `AzureHistoryLease`, `LeaseLostError`, `PodVersions`, `KubernetesPodVersions` (ZO-1) |
| Methods added or widened (about 11) | `advance_current` twice, `CatalogAdmin.open(BlobLocation)`, `retirable`, `fold_lifecycle`, `ManifestArtefacts(fetch)`, `catalog_section(lifecycle=)`, `_ObjectOps.list`, three `AzureBlobRest` verbs, `BlobInventoryStorage(lease=)`, opt-in on blob | the first eight | the Azure lease and delete methods, `lease=`, opt-in (ZO-1) |
| Configuration keys (17) | `format_version`; 8 `[retention]`; 3 `[access]`; 5 `[expiry]` | the retention keys GC's start check and the gating tests use; `[access]`; `retire_after` (plan:357) | the five `[expiry]` keys (ZO-1) |
| Format additions (2) | the manifest section's `lifecycle`; the root's `extensions.statcat.release` | `lifecycle` (S13) | root `release` (ZO-2) |
| Changes to other repositories' documents (7) | S13, S18, S19, S20, S21, S22 (two halves), S23 | S13; S18 (gating test 5, ZO-8); S21; S22's log half | S19, S20, S22's `--discard` half, S23 |
| Alert event names (17) | D14 | the events for alerts the jobs detect (plan:360) | five `expiry.*` (ZO-1) and `gc.forecast_deviation` (ZO-6) |
| Owner questions (8) | Q1 to Q8 | Q1, Q2 (answered differently), Q3, Q4, Q5, Q7, Q8 | Q6 (ZO-4) |

**Proposal:** Add this table, or its after-cut form, to the design.

**Touches:** None by itself; it summarises ZO-1, ZO-2, ZO-4, ZO-6 and ZO-7.

## Minor

### ZO-6. GC's `--forecast`

**Sites:** design:607, 644-647, 744, 981; spec:1010, 1032; plan:358.

**Problem:** `--forecast FILE` needs the orchestrator to keep a dry run's plan between two job runs,
the same cross-run state S23 needs a volume for. It also changes deletion behaviour, deleting only
paths in both plans, which goes beyond the alert §11.4 asks for. No gating test or plan bullet names
it.

**Proposal:** Move it to Phase 8 with the orchestration and alert routing; keep `--dry-run` and the
deletion log, and mark §11.6's forecast alert as Phase 8's in D14.

**Touches:** The row at design:981; no gating test. Risk: until Phase 8 an unexpected deletion shows
only in the log, after the fact, with fail-closed and the 24 h grace still applying.

### ZO-7. `--report` and `statcat admin rollback`

**Sites:** design:260, 276, 676-689, 931; plan:430.

**Problem:** In a CronJob pod, `--report FILE` writes to a disk that disappears with the pod unless a
volume is mounted, and standard output already carries every line. `admin rollback` with `--to` and
`--if-live` is the most involved admin command, nothing in Phase 5 calls it, and its first use is
Phase 9's drill (plan:430), which the Python `CatalogAdmin.rollback` serves once D11 lands. The
design also does not say which identity an operator uses for `statcat admin` on a server.

**Proposal:** Drop `--report` and print `ReleaseReport`. Ship `editions`, `release`, `cancel` and
`embargo`, and leave `rollback` to Phase 9. State the operator's identity in D11, for example a
one-off pod under the release job's workload identity.

**Touches:** No gating test; CUJ 2's `editions` and `cancel` stay. Risk: a server rollback before
Phase 9 goes through Python.

### ZO-8. Q1 and S18 restate spec §6.6

**Sites:** design:146, 164, 420-424; spec:579; `src/statcatlib/EGM.md`:392-395.

**Problem:** §6.6 Retry already says "a stale automatic release must not write `live` after a
concurrent change turned embargo on". S18 adds one sentence, that an automatic release also does
nothing when the head has `embargo: true` at the start of the run. The option table weighs a `hold`
field that §6.6 already makes unnecessary.

**Proposal:** Present S18 as a one-sentence clarification of §6.4 citing §6.6 Retry, and reduce Q1 to
the naming concern or record it as decided.

**Touches:** How Q1 is framed. Gating test 5 unchanged.

### ZO-9. Exit code 3 under a Kubernetes Job

**Sites:** design:282-286, 288-292, 752, 962; `cli/EGM.md`:36-42.

**Problem:** A Kubernetes Job counts a non-zero exit as a failed pod and retries up to
`backoffLimit`, 6 by default (verify against the cluster version). A refusal stands until an
operator acts, so every scheduled run exits 3, each becomes up to seven, and "the release job missed
or failed" (design:752) fires for a business refusal. Exit 3 is also a new convention; existing
commands exit 1 on a finding.

**Proposal:** Either exit 0 when the run completed and carry refusals in events and `REFUSED` lines,
or keep 3, state the `podFailurePolicy` (`onExitCodes`) rule Phase 8 must set, and say how the
alert tells 3 from 1.

**Touches:** The row at design:962. Risk: noisy repeated runs; they are idempotent.

### ZO-10. Reuse in the fold's reference check and in `ManifestArtefacts`

**Sites:** design:321-330, 475-479; `store/working_copy.py`:215-250; `catalog.py`:326-359;
`store/inventory.py`:367-369; `bench/corpus.py`:170.

**Problem:** The fold picks structure editions "as `structure_editions` chooses locally" and repeats
the check "as `Catalog.seal`'s structure-check pass does", which reads as a second copy of both.
`structure_editions(full, catalog_root, ...)` returns `catalog_root/products/<p>/<e>`, so a temporary
directory with that layout lets the fold call it unchanged. `ManifestArtefacts(Path | Fetch)` keeps
a `Path` branch only for a bench monkeypatch, and `LocalInventoryStorage.read` is already a `Fetch`.

**Proposal:** Pull into `<tmp>/products/<p>/<e>` and call `structure_editions`. Factor the loop in
`_check_structures` into one function shared by `Catalog.seal` and the fold. Make `ManifestArtefacts`
take a `Fetch` only, and move the bench's byte counter onto the fetch.

**Touches:** None.

### ZO-11. The duration grammar in the jobs configuration

**Sites:** design:499-503, 507-515, 550; `cli/__init__.py`:554-555; `admin.py`:520.

**Problem:** D7 adds a duration grammar and parser, listed as TOML's cost. Every window is in days,
the grace period in hours, and only `pod_staleness` in minutes. `statcat prune` already takes
`--older-than DAYS` as an integer.

**Proposal:** Integer keys with the unit in the name (`gc_window_days = 35`, `upload_grace_hours =
24`, `pod_staleness_seconds = 300`), validated by TOML's integer type.

**Touches:** S21's key names, in spelling only.

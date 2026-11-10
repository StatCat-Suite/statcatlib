# Structural report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`.

**Verdict.** The design does not pass yet: 0 Blockers, 10 Major and 9 Minor findings. Its sections
follow the Phase 4 house order: Overview, Terms, Problem, Goals, Non-Goals, Open Questions with the
handoff table, CUJs, Technical Plan with Errors and Symbols, the GH-125 scope section, Alternatives,
NFR with Risks, then the Detailed Implementation Plan with the acceptance map. Heading levels skip
nothing, and the tables and code fences are well formed. Each of the four Problem questions has one
findable answer: D1 for the shape, D7 for the retention schema, D5 for the refusals and D3 for S13.
The design is listed in `SITEMAP.md` and in the mkdocs nav.

The gaps sit in five places. The handoff table and Land order disagree about what gates Parts 1 and
2. Phase 4 handed S15 to GH-52, and this design drops it. The S rows give an owner no options and no
gate. Six decisions have no options table, although design:815 says each one does. Part 6 is
designed against an interface that a design not yet written will define.

The house's round 1 expectations are met in part. The design has the handoff table, the Parts table
with "Waits on", files and tiers for each test row, the error table and the symbol table. It has no
list of unconfirmed items, and the NFR section has no memory or disk entry.

## What I read

- The persona `agents/structural.md` and the standard `feature-spec-standard.md` of the EGM plugin.
- The target, all 1045 lines.
- `docs/design/catalog-on-blob-storage.md`, lines 1-340 and 905-1293, and its round 1
  `review_report.md` and `structural_report.md`.
- statcatlib `CLAUDE.md`: Writing style, the `just ci` gate and test markers, design conventions.
- The hub's `CLAUDE.md`, "Suite designs", and `docs/design/inventory-history-log.md`: Expiry steps
  (484-542), Cross-repo contract, Staging, Handoff (805-970).
- Issue texts for GH-52, GH-125 and the suite tracker; the plan, lines 330-399; the spec §10 and §11.
- Glob checks of `tests/` for which named test files exist, and of `mkdocs.yml` and
  `docs/design/SITEMAP.md`.

## Limits

- I had no git, took the commits at design:8-13 as given, and did not check `file:line` code
  citations.
- I did not read GH-134's results or sdmxlib#380, #381, #384 and #385. The claim that #381 resolved
  S1, S12 and S14 rests on design:157 and the suite tracker.
- I read the spec only in §10 and §11.

## Blocker

None.

## Major

### STR-1. The handoff table and Land order miss what Parts 1 and 2 wait on

**Sites:** design:177, 179, 879-880, 912, 930; also 152 (Q7), 148 (Q3), 167 (S21), 783
(`fold_lifecycle`, Part 1).

**Problem:** Two owner questions and two spec questions feed Parts 1 and 2, and neither table
records it.

- Handoff row 1 says Q1 to Q8 gate only Parts 3 and 4. Q7 decides the fifth row of D3's table,
  which `fold_lifecycle` implements in Part 1 and Part 1 tests (design:912). Q3 decides where the
  jobs configuration lives, and `jobs.py` is Part 2.
- Row 3 says the spec PR gates only Parts 3 and 4. S13 is built in Part 1 (the section key,
  `catalog-format.md` §3, `fold_lifecycle`). S21 names values that D7's `format_version = 1`
  schema fixes in Part 2.
- Land order gives Part 1 "Waits on: none" and Part 2 "Part 1".

An implementer can merge Part 1 or Part 2 and then see the owner or the spec PR change it.

**Fix:** Give each Q and S row its own gate. Make Q7 and S13 gate Part 1, and Q3 and S21 gate Part 2,
and make Land order's "Waits on" column match. If Part 1 lands before Q7 is decided, say that Part 1
builds the recommendation and name the rows that change under the other answer.

### STR-2. The S rows cannot be decided as written

**Sites:** design:161-169, 177, 179, 186; the Q table at 144-153.

**Problem:** The S table has three columns, Where, Change and Status, with no alternative, no
consequence and no gate.

- S18, S19 and S20 borrow their options from Q1, Q2 and Q6. S21, the `--discard` half of S22, and
  S23 have options nowhere in the document.
- S13 says "Resolved here". It changes sdmxlib's spec §11.2 step 2, and the Phase 4 design left it
  to GH-52 for the spec's owner (`catalog-on-blob-storage.md:154`, `:189`).
- S18 says "`release_due` releases nothing". D4 meets that by not calling `release_due` while the
  flag is set (design:396-397). The row does not say whether sdmxlib's function changes, so the
  repository that writes code for S18 is undecided.
- Handoff row 3 bundles S13 and S18 to S22 into one PR with no issue. Row 9 names no hub PR or issue
  for S23. Round 1 B3 asked for one row per item, with target, issue, carrying release and the Part
  it gates.
- The Q table has no "Gates" column.

**Fix:** Add "Recommendation", "If rejected" and "Gates" columns to the S table, or point each S row
to the D section holding its options, and add options for S21, S22's `--discard` and S23. Say
whether S18 changes `release_due` in sdmxlib or only its caller. Give each S row its own handoff
line with an issue placeholder. Add a "Gates" column to the Q table.

### STR-3. Phase 4's S15 was handed to GH-52 and then dropped

**Sites:** design:157; `catalog-on-blob-storage.md:156` (S15) and `:189` (handoff row 8, "S1, S12,
S13, S14, S15 | sdmxlib, GH-52 | Phase 5").

**Problem:** Design:157 accounts for S1, S12 and S14 (resolved by sdmxlib#381) and S13 (resolved
here). S15 is the spec §4.6 table, which says extensions run "section only" at `open` while
`check_catalog` runs everything. Phase 4's handoff row 8 sends S15 to GH-52 and gates Phase 5 on
it. This design neither resolves S15 nor moves it, so a Phase 4 gate is left open with no owner. The
compiler confirmed both Phase 4 lines.

**Fix:** Add S15 to the S table with a status: resolved, moved to sdmxlib#384 or Phase 6, or dropped
with a reason. Add it to handoff row 3 if it rides with the spec PR.

### STR-4. The design does not test itself against the hub's suite-design rule

**Sites:** design:147 (Q2, "readable by ... every pod"), 165 (S19), 169 (S23), 183 (row 7), 185 (row
9), 459, 529-531, 703-709, 734-737; hub `CLAUDE.md`, "Suite designs".

**Problem:** The hub's rule puts a design that changes a contract between repositories in the hub,
and requires a local design needing a contract change to take it to the suite design first. This
design holds several such changes and does not say why it stays local.

- S23 edits the hub's suite design.
- S19 adds `extensions.statcat.release` to a root sdmxlib defines. Pods in sdmxlib-rest and
  statcat-serve fetch that root on every poll, and it can grow by about 150 KB (design:459). Phase 8
  alerting is meant to read it.
- D12's `EmbargoReader` API is consumed by statcat-serve (row 7).
- D14's event names are consumed by Phase 8, in statcat-serve's deployment.
- D7's `[expiry] deployments` labels and `port` must match statcat-serve's manifests.

The Phase 4 design closed the same question in one sentence (`catalog-on-blob-storage.md:197`).

**Fix:** Add a short "Cross-repo contract" subsection under the handoff table. For each change, name
the writer and every reader in another repository, and state whether the design stays local under
the hub rule, and why. Route S23 as a revision of the suite design with its own review before Part
6. If S19's readers outside statcatlib are real, add a handoff row for statcat-serve or Phase 8.

### STR-5. Part 6 is designed against an interface another design will define

**Sites:** design:6, 55, 130, 184, 186, 711-732 (726-729 in particular), 797-811, 884, 995-1012,
1034; GH-125 body.

**Problem:** D13 states the expiry engine's interface and then says "If GH-125's design names these
differently, Part 6 follows it". Handoff row 8 says GH-125 parts 2 and 3 have "their own design",
which does not exist yet. Part 6 cannot be implemented from this document. Its sdmxlib column reads
"GH-125 stage 2's", a release with no number. The blob expiry storage has no symbol name. GH-52
closes on the acceptance map, which includes "GH-125's server expiry acceptance | 6", so GH-52's
closure waits on an unwritten design. The hub's statcatlib handoff places server expiry under GH-125
stage 2 "with Phase 5", which supports one stage 2 design covering the engine on both storages.

**Fix:** Pick one layout. Either move D13 and Part 6 to GH-125's stage 2 design, keeping D1 step 6
as a hook, the `[expiry]` keys and `--state-dir`, so the acceptance map closes on Parts 1 to 5; or
keep Part 6 here, mark it provisional, name the blob expiry storage, and add a handoff row that
re-reviews Part 6 after GH-125's design lands. Either way, say which issue closes on server expiry.

### STR-6. The `HISTORY` lease on ordinary writes has no Part, file or test

**Sites:** design:673-674 (D11, a Part 2 decision), 718-719 (D13, "Lease on writes"), 916-919 (Part 2
files), 997-1001 (Part 6 files), 1004-1012 (Part 6 tests); GH-125 part 3 ("The blob writer and
`CatalogAdmin` take the same lease").

**Problem:** D11 says each commit on an opted-in Azure catalog takes the `HISTORY` lease and waits
up to 60 s. That needs `AzureHistoryLease`, which arrives in Part 6. D13 adds
`BlobInventoryStorage(location, *, lease=None)`, with `try_commit` acquiring a lease when none is
passed. Neither Part's file list names that change. No test row covers an admin commit that waits
for a release run's lease, or one that times out. The Errors table has no row for a lease that
cannot be acquired, by a second release run (GH-125's acceptance) or by an admin commit after 60 s.

**Fix:** Move D11's lease bullet to D13. List `BlobInventoryStorage(lease=)` and the acquire in
`try_commit` in Part 6's files. Add test rows for an admin commit that waits and one that gives up.
Add the acquire failure, its error and its exit code to the Errors table.

### STR-7. Six decisions have no options table

**Sites:** design:554-591 (D8), 593-602 (D9), 664-689 (D11), 691-709 (D12), 711-732 (D13), 734-755
(D14), 815 ("Each decision above carries its own table").

**Problem:** D1 to D7 and D10 weigh options. D8, D9 and D11 to D14 weigh none. D8 chooses ARM and
Graph REST calls through `httpx` and does not weigh Azure Resource Graph, or `atScope()` against a
walk per scope; Q4 covers only the identity. D13 picks a 60 s lease with a renewal thread and a
Kubernetes pod listing with no alternative. The sentence at design:815 is false.

**Fix:** Add an options table to each of D8, D9 and D11 to D14. Where the spec or the suite design
leaves nothing to choose (the lease timings), say so in one line, and correct design:815.

### STR-8. Unconfirmed items have no list, and the risk table misses most of them

**Sites:** design:568-570 (the only "Verify before citing"), 585-588, 722, 857-871, 1037-1044; house
form `catalog-on-blob-storage.md:272-287` ("Not confirmed") and round 1 M29.

**Problem:** The house lists each fact not confirmed on real Azure and gives each a risk row. This
design marks only `atScope()`. It also rests on these unrun facts:

- `roleEligibilityScheduleInstances` with `atScope()`;
- whether Graph's `transitiveMembers` covers eligible group membership under PIM for Groups, which
  O6 requires;
- whether built-in Reader at the management group can read eligibility schedules;
- whether the in-cluster API lets the job list pods, and with which Kubernetes RBAC;
- whether every pod's `GET /` exposes `sdmxlib_version`;
- the lease, renewal and `If-Match` delete over `httpx` on real Azure.

The risk table also lacks four failure modes the design creates: a listing permission that stays
missing makes every run exit 3 with `access.unchecked`, so no automatic release ever happens;
`--discard` can delete the wrong edition; the release job gains rights to read pods in the cluster;
and a `final` product cannot be republished until Q8's operation exists. The Verification Plan's
real-account run checks only the access result and the fold and release times.

**Fix:** Add a "Not confirmed" list under the Technical Plan, with a risk row and a real-account step
for each item, the lease and the pod check included. Add the four failure modes to the risk table.

### STR-9. The NFR section has no memory or disk entry

**Sites:** design:827-855; house form `catalog-on-blob-storage.md:1029-1045` and round 1 M30.

**Problem:** The section has Atomicity, Idempotency, Security, Compatibility and Coverage. It states
no memory bound, although the fold holds a `FullInventory` of 7,003 leaves (about 20 MB filled), GC
holds the kept set from about 350 roots (560 MB read), and expiry holds the leaf-set cache. It states
no disk bound for the fold's temporary directory, which pulls every structure product's live and
embargoed editions (design:321-327), for `--state-dir`, or for `EmbargoReader`'s `cache_dir`. The
fold's cost row leaves out those structure pulls. The Coverage line claims a unit tier for every
module, and no test row exercises `AzureReaderListing`'s requests or `KubernetesPodVersions`.

**Fix:** Add Memory and Disk entries with figures. Add the structure pulls to the fold's cost row.
Add a per-module coverage table for `release.py`, `access.py`, `gc.py`, `embargo.py`, and the lease
and listing clients.

### STR-10. Stated behaviours with no test row, Goal 2's release check among them

**Sites:** Goal at design:113-114; D4 at 405-407; D1 at 269, 278-280, 286; D8 at 571-579; D10 at
629-631, 640-641; D13 at 718, 722, 724; D14 at 739-750; Parts 2, 3, 4 and 6 test tables.

**Problem:** These behaviours have no test row:

- the release step's manifest check, which drops a product whose manifest is absent or differs:
  half of Goal 2 and spec §6.5 item 2;
- `--dry-run` writing nothing, and `--report`'s JSON;
- step 0 advancing a lagging `CURRENT` and logging `current.behind`;
- each D14 event firing with its fixed name, `release.overdue` and `release.held` included;
- the fold's `kind` refusal;
- `AzureReaderListing`'s wildcard and `notDataActions` matching, the group expansion and the
  shared-key read, against recorded responses;
- `_ObjectOps.list` in Part 2;
- GC deleting a retired directory with `manifest.json` first, and the deletion log's line format;
- `KubernetesPodVersions` against a fake API, a successful opt-in on Azure, and `expiry.stopped`
  at the 30-minute cap.

**Fix:** Add a row for each, with file and tier. Add the release-time manifest check to the
acceptance map against Goal 2.

## Minor

### STR-11. The departures from GH-125 part 4's body are unrecorded

**Sites:** design:23, 466-479, 491-495, 804, 806-809; GH-125 part 4.

**Problem:** GH-125 part 4 says a leaf-backed `Artefacts` reader becomes `InventoryWriter`'s default
and replaces `ManifestArtefacts(root)`. D6 rejects that reader and keeps `ManifestArtefacts`.
Design:804 still says Part 1 takes part 4 "whole". D6 relies on GH-130's `reinstate` passing
`artefacts=`, and no handoff row tells GH-130.

**Fix:** State in "GH-125's parts" that D6 overrides that bullet. Add handoff rows to edit GH-125's
body and to add the `artefacts=` acceptance item to GH-130.

### STR-12. Cross-references and labels that resolve wrongly or not at all

**Sites:** design:310, 598 and 786, 230, 65 and 232 (H21, H23), 721 (H20), 61 and 127 (W3, W11), 63
(P7), 667 (P1), 720 (F14), 871 (F13).

**Problem:** "Two keys in D7's order" (design:310) points at this design's D7, the jobs
configuration; the source is Phase 4's D7 or spec §11.2 step 1. `retirable` has two signatures
(design:598 and 786). O2's "Q9" is the plan's Q9. The Terms table does not say where the H, W, P and
F labels come from.

**Fix:** Write "Phase 4 D7" or "spec §11.2 step 1". Use one `retirable` signature. Write "plan Q9".
Add a Terms row naming the source of each label prefix.

### STR-13. Land order's prose disagrees with handoff rows 6 and 7

**Sites:** design:182-183, 179, 884, 888-889.

**Problem:** Row 6 lets Part 4 land before Part 3. Design:888-889 says "Parts release in order, and
Parts 4 and 5 may land in either order after Part 2", which leaves Part 4 against Part 3 unclear.
Row 3 says "Parts 3, 4 merge", while design:173 defines "Gates" as what cannot land. Part 6's
sdmxlib column names no release.

**Fix:** One sentence matching rows 6 and 7. Drop "merge". Name Part 6's sdmxlib release, or write
"to be named by GH-125's design".

### STR-14. Some inputs are unpinned

**Sites:** design:8-28.

**Problem:** The GH-52 and GH-125 bodies carry no snapshot date. The spec link follows the published
site and names no revision. The section list leaves out §6.2 and §6.6, which S18, S19, D1 and step 0
rely on. O8 is dated 2026-11-10, while design:26 lists only 11-03, 11-04 and 11-07. GH-134's results
have no comment link. sdmxlib#382, which O7 rests on, is not named.

**Fix:** Date the issue snapshots, name the spec revision at `6a646b75`, complete the section list,
add 2026-11-10, and link the GH-134 comment and sdmxlib#382.

### STR-15. Future work is scattered

**Sites:** design:136, 188, 861, 867, 735, 191.

**Problem:** Six follow-ups are spread through the document: the `artefacts` backfill, Q8's issue, a
check of the container's root ACL, a cache for GC's root reads, Phase 8's alert wiring, and
sdmxlib#384. Round 1 m29 asked for them in one place.

**Fix:** Add a "Future work" subsection at the end of the Detailed Implementation Plan, one line and
its trigger each.

### STR-16. Non-Goals and decision text written conditionally

**Sites:** design:132-134, 635-636, 979; GH-52 comment of 2026-11-02.

**Problem:** The `lifecycle` change is a Non-Goal that Q8 may reverse. D10 says "With Q6, also ..."
and Part 4's row 979 keeps both outcomes, though design:142 says the design follows each
recommendation. GH-52's acceptance item "an embargoed dataflow returns 404 from every pod" moved to
Phase 6 in that comment, and no Non-Goal records it.

**Fix:** Write D10 and row 979 for the recommendation and list in Q6 what changes on a rejection.
Phrase the `lifecycle` Non-Goal as following Q8's recommendation. Add the 404 test to Non-Goals as
Phase 6's.

### STR-17. Goals and the acceptance map trace only part of the scope

**Sites:** design:109-120, 1014-1035; GH-52 acceptance list.

**Problem:** No goal covers Part 2 (`CatalogAdmin` on blob, `advance-current`), Part 5
(`EmbargoReader`) or the access precondition. The acceptance map lists the plan's gating tests, and
GH-52's own item for a data edition whose partial gained a code is not shown against its nearest
row.

**Fix:** Add goals for Parts 2 and 5 and the access check. Map GH-52's acceptance list to the rows
of the map.

### STR-18. The event, error and exit-code lists are incomplete

**Sites:** design:722, 545, 739-750, 757-774, 604-647.

**Problem:** `expiry.pod_check_not_run` and `jobs.config` are missing from D14's table. D10 defines
no exit codes; GC's 1 and 3 appear only in the Errors table, and 0 and 2 nowhere. The Errors table
has no row for an unreadable `--forecast` file or a lease that cannot be acquired (STR-6).

**Fix:** Complete D14's table, add an exit-code paragraph to D10, and add the missing error rows.

### STR-19. Target-scope details for new files

**Sites:** design:886-889, 905-912, 922-923, 941-942, 968, 987, 1002.

**Problem:** Of the new test files, only `tests/test_fold_lifecycle.py` is marked (Create); twelve
others are new, `tests/test_release_job.py` and both `test_gc.py` files among them. `docs/api/release.md`,
`gc.md` and `embargo.md` have no nav entry, and `mkdocs build --strict` fails on a page missing from
the nav. No Part names `docs/reference/` pages for the new modules. Land order leaves out the Phase 4
sentence on `CHANGELOG.md` and `just docs-build`.

**Fix:** Mark each new test file (Create), add each page's nav entry, list the reference pages, and
reuse the Phase 4 sentence at `catalog-on-blob-storage.md:1092-1096`.

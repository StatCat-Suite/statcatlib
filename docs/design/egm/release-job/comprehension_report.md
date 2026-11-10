# Comprehension report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`.

## Verdict

The design is dense but mostly well organised. The Terms table, the per-decision option tables and
the acceptance map help a first-time reader. No finding blocks a reader completely, so this review
has no Blocker. Thirteen Major findings would lead the owner or an implementer to a wrong
conclusion. Q2, Q4, Q5 and Q8 can be decided from the text alone. Q1, Q3, Q6 and Q7 need the fixes
below.

## What I read

- The persona file `agents/comprehension.md` of the EGM plugin.
- The target, all 1045 lines, read cold first, with the places I got lost noted before consulting
  anything else.
- From the Phase 4 design, `docs/design/catalog-on-blob-storage.md`: O4, W3, W6, W8, W11, D5, D7,
  S12, S13, F13, F14, handoff row 9, and "the gate".
- `docs/catalog-format.md` §7 step 4, the local meaning of embargo.
- From the spec copy: §6.1 (`format_version` 2.0 after opt-in), §6.2 `embargo`, §6.4, §6.9
  `expired_through`, and the §11.6 retention inequalities.
- From the plan copy: lines 345 to 377 (Phase 5), and its Q8 and Q9.
- From the hub's `inventory-history-log.md`: H20 to H23, and the leaf-set cache with its 1.7 to
  4.7 GB figure.
- A grep of statcatlib for `release_due` callers (none in `src`) and for P1 and P7, both decisions
  of `docs/design/phase-2a-catalog-editions.md`.

## Limits

- I did not read sdmxlib's code or the GitHub issue texts, so I could not check claims that rest on
  issue bodies.
- I did not check the `file:line` citations into statcatlib source.
- I read the spec and plan copies only around the sections this design cites.

## Blocker

None.

## Major

### CMP-1. The Q1 embargo semantics mix local and server behaviour

**Sites:** design:146 (Q1), 164 (S18), 195 (CUJ 1 title), 273 (D1 step 4), 396-397, 420-424, 685;
`docs/catalog-format.md` §7 step 4; spec §6.2 `embargo`.

**Problem:** Q1's recommendation says "`embargo: true` holds every automatic release: a seal records
`embargoed`, and the release step releases nothing." That sentence holds two different things.

- "A seal records `embargoed`" is the flag's current meaning on a local catalog (catalog-format §7:
  `live` with embargo off, `embargoed` with it on). It does not change.
- "The release step releases nothing" is new, and it applies only on a server, where nothing seals
  into the Inventory.

A reader cannot tell from Q1 alone which behaviour is new, whether a local catalog changes at all
(no local code calls `release_due` today, so it does not), or what `embargo: false` means on a
server. On a server the fold records `embargoed` whatever the flag says, which departs from spec
§6.2 ("With `false`, a seal releases its edition in the same version"). Q1 also omits a point only
S18 states, that an explicit `statcat admin release` still goes live while the flag is `true`, and
it does not say who sets a new server catalog to `embargo: false`.

CUJ 1 is titled "A daily release under embargo" while, by Q1, its catalog has `embargo: false`. The
CUJ uses "embargo" for the state of the entries and Q1 uses it for the root flag.

**Fix:** Rewrite Q1 as a two-row table, local catalog and server catalog, each with what `true` and
`false` do today and after S18. Name `statcat admin embargo URL on` as the stop switch. State that
explicit releases still go live. State how a server catalog's flag starts out. Rename CUJ 1 to "A
scheduled daily release", and say in it that the root flag is `false`.

### CMP-2. The emergency stop waits behind the release run's lease on an opted-in catalog

**Sites:** design:146 (Q1 "the emergency stop"), 271 and 276 (D1 steps 1 and 7), 415-417 (D4 retry),
673-674 (D11), 718-719 (D13).

**Problem:** D4 says a concurrent `set_embargo(on=True)` turns the release commit's retry into a held
run, and Q1 presents the flag as an emergency stop. D1 says a release run on a 2.x catalog holds the
`HISTORY` lease from step 1 to step 7, which with expiry can last up to 30 minutes. D11 says an
admin commit on an opted-in catalog waits at most 60 s for that lease. Read together, on an
opted-in catalog the stop can never land during a release commit, and if the run lasts longer than
60 s the stop fails. A reader deciding Q1 concludes the switch acts at once on every catalog.

**Fix:** State in Q1 and D4 that on an opted-in catalog the stop takes effect at the next run, and
what an operator does when the 60 s wait fails (retry, or stop the CronJob). Otherwise, release the
lease between steps 4 and 6 and say so.

### CMP-3. A skipped step 4 drops the fold's refusals, against a Goal

**Sites:** design:115-116 (Goal "Every refusal is persisted"), 273 (D1 step 4 runs "when step 2
passed and the head's `embargo` is `false`"), 396-397 (D4 writes the record when held), 439-447 (D5
"Step 4 writes the record"), 581-583 (D8), 766.

**Problem:** D5 makes step 4 the only writer of the refusal record, which holds the refusals of
steps 3 and 4. D8 says a failed access check skips step 4 while the fold still runs, so the fold's
refusals from that run never reach the root. The design also gives two answers for a run with the
embargo flag on: D1's table says step 4 does not run, and D4's code says it runs and writes the
record alone.

**Fix:** Pick one rule and state it in D1, D4 and D5, for example "step 4 always writes the record;
with a failed access check or the flag on, it writes the record and releases nothing." Add a Part 3
test row for a fold refusal persisted under a failed access check.

### CMP-4. The window names do not line up across the design

**Sites:** design:229 (O1 "after the window"), 218 (CUJ 3 "older than the window"), 274 ("the
retire window"), 167 (S21), 312, 507-515, 537-543, 615, 634-636, 753-754.

**Problem:** The jobs configuration has eight retention values, and the text refers to them by other
names. `gc_window` appears as "the window" (O1, CUJ 3), "the GC window" (CUJ 3) and §11.4's
"retention window". `retire_after` appears as "the retire window" (D1, S21) and §6.4's "past the
window". A reader meeting "the window" in O1 cannot tell which value it is until D10 step 5.

- `inventory_window` and `keep_roots` carry only the comments "W" and "K". Nothing in this design
  uses them before Part 6, and the design never says they govern expiry (spec §6.9).
- `backup` has no explanation of which backup it refers to, or why it must be at most `gc_window`.
- `pod_staleness` appears only in inequality 3, while D14 says pod staleness is outside the jobs.
- The `tombstone_window` comment says "tombstones kept", and nothing in this design drops a
  tombstone. D9 always carries the head's tombstones forward.

**Fix:** Add a "Retention values" table to D7 with one row per key: what it governs, which job and
step reads it, which Part first uses it, and the spec's name for it. Then use only the key names in
O1, CUJ 3, D1, D10 and S21.

### CMP-5. "2.x catalog", "opted in" and the stage 2 objects are undefined

**Sites:** design:41, 80 (Terms "Stage 2"), 147 ("history segments"), 271, 275, 291-292, 618
(`expired_through`), 673, 718, 724.

**Problem:** D1 and D10 branch on "a 2.x catalog", while the Overview, D1's schedule and D11 say
"opted in". Nothing in the design says these name the same state; the spec does (§6.1,
`catalog.json` `format_version` "2.0" once a catalog opts in). A reader may take "2.x" for a
manifest version. `HISTORY`, `expired_through`, "history segments" and "epoch" also appear without
definition. The design does not say that no Azure catalog can be 2.x until Part 6 lands, since
opt-in over a `BlobLocation` is Part 6. Between Part 3 and Part 6, D1 step 1 and D11's lease wait do
nothing, and a Part 3 implementer cannot tell whether to build step 1.

**Fix:** Add Terms rows for "opted-in (2.x) catalog", `HISTORY`, `expired_through` and "history
segment", each with its spec section. Use one name throughout. In D1 and D11, say that step 1 and
the lease wait first take effect with Part 6.

### CMP-6. Identifiers collide with other documents' numbering

**Sites:** design:310 ("two keys in D7's order"; this design's D7 is the TOML configuration, and the
key order is Phase 4's D7); design:232 (O4; in the Phase 4 design O4 is the embargo question, the
subject of this design's Q1); design:124-125 and 230 ("plan Q8, Q9" and "Q9"; this design's Q8 is
about lifecycle); design:63 and 668 (P7 and P1, Phase 2a decisions, and that design is no Input);
design:85 ("GH-19", which is the suite tracker, a hub issue).

**Problem:** A reader chasing any of these lands on the wrong item. The worst case is "D7's order"
in the fold's first check, which sends the implementer to the TOML section.

**Fix:** Qualify every foreign identifier where it appears: "Phase 4 D7", "plan Q9", "Phase 2a P1",
"StatCat-Suite/statcat-suite.github.io#19". Rename this design's recorded decisions so they do not
echo Phase 4's O4, or note the collision once in the Recorded decisions introduction.

### CMP-7. The handoff table understates which Parts the owner questions gate

**Sites:** design:177 (row 1, "Gates Parts 3, 4"), 148 (Q3), 152 (Q7), 783 (`fold_lifecycle`, Part
1), 912 (Part 1 test "`fold_lifecycle`'s five rows"), 788 and 920 (`jobs.py`, Part 2).

**Problem:** Q7's answer decides the fifth row of D3's table, which `fold_lifecycle` implements and
Part 1 tests. Q3 decides where D7's configuration lives, which Part 2 builds. Row 1 tells the Part 1
and Part 2 implementers they can start without these decisions. The same applies to S13's spec text
(row 3 gates Parts 3 and 4), although `fold_lifecycle` ships in Part 1.

**Fix:** Split row 1 into one row per question, each with the Part it gates (Q7 gates Part 1, Q3
gates Part 2, Q2, Q4 and Q5 gate Part 3, Q6 gates Part 4, Q8 gates nothing). Alternatively, move
`fold_lifecycle` to Part 3.

### CMP-8. The Overview names components and gives no outcome for each Part

**Sites:** design:46-65 (the six pieces and the capability table), 879-889 (Land order), 195-203
(CUJ 1).

**Problem:** The Overview lists what each Part contains and where each capability lives. Neither
says what a user can do after each Part lands, or what changes for an existing user. A reader cannot
learn from the Overview that:

- Part 1 changes every local seal: it writes `lifecycle` into the section and fills `artefacts` for
  data editions, so leaf entries grow about four times.
- CUJ 1 needs both Part 3 and Part 5, since pre-materialisation uses `EmbargoReader`. Between Part 3
  and Part 5 every release is unprepared.
- Until Part 4 lands, nothing deletes anything on a server.

In the capability table, "Part 1, not in the gate" uses "the gate" for the CI gate (Phase 4 W3 and
W6), which this design never explains, and "Part 5's path, with no upload to fill it" is cryptic.

**Fix:** Add one sentence per Part to the Overview list, "After Part N, a user can …". State the
local-catalog change from Part 1 and the CUJ 1 dependency on Part 5. Write "untested in `just ci`"
for "not in the gate".

### CMP-9. The leaf-read measurement used unfilled leaves

**Sites:** design:24-25 (Inputs), 236 (O8), 485-489 (D6 Cost), 830, 835-836.

**Problem:** The header, O8 and the NFR table present 0.94 s as the cost of a cold read of 7,000
leaves. D6 shows that the real account read "the smaller corpus" (6.6 MB, entries without records),
while Part 1 fills every entry (20.0 MB on Azurite). A reader of the header, O8 or the NFR rows
concludes that the read Phase 5 performs has been measured. The NFR's "1 s" for the fold's and the
release's leaf reads rests on an extrapolation.

**Fix:** Say in O8 and the NFR introduction that 0.94 s is for unfilled leaves. Give an estimate for
filled leaves, and state what happens if Part 3's stores test or the first real release shows the
filled read missing plan Goal 1.

### CMP-10. CUJ 2 names a rule that D4 and D5 say `release_due` refusals lack

**Sites:** design:208-209 (CUJ 2), 413-414 (D4), 433 and 440-441 (D5).

**Problem:** CUJ 2 says the release step "refuses it with `fast-forward`" and that the record "names
the edition, the rule and the message". D4 says `release_due`'s refusals arrive as messages "with no
`rule`", and D5's example, which is this exact case, shows `"rule": null`. A reader building an alert
or a runbook on CUJ 2 expects a `fast-forward` value that never appears. CUJ 2 also gives the time
as 08:30, while CUJ 1 runs the release at 08:30:05.

**Fix:** Change CUJ 2 to say the record holds `rule: null` and a reason naming both editions, as D5
shows (or carry the rule, see `review_report.md` M-findings on D5). Use 08:30:05.

### CMP-11. The gate 2 fallback contradicts H20

**Sites:** design:721-722 (Gate 2, "With no API, gate 1 alone applies"); hub
`inventory-history-log.md` H20.

**Problem:** H20 says every server expiry checks every pod's `sdmxlib_version` on every run,
counting an unknown version as a failure. D13 lets expiry run on gate 1 alone when the Kubernetes
API is unavailable, which makes every pod's version unknown. A reader who knows H20 cannot tell
which document wins. The suite design's "Expiry gates" section does allow gate 1 alone where the
job cannot reach the API, so the two hub texts disagree with each other as well.

**Fix:** State which hub text D13 follows, and record the conflict between H20 and the gates section
as a hub question.

### CMP-12. "The orchestrator" is undefined and conflated with the CronJob

**Sites:** design:197-203 (CUJ 1), 242 (D1), 288-292 (the schedule), 299 (options, "one CronJob"),
752.

**Problem:** CUJ 1 and D1 have "the orchestrator" running `statcat release` at a pre-flight time and
just after each `release_at`. D1's options and schedule describe a Kubernetes CronJob, which runs on
a fixed cron schedule. A reader cannot tell whether the orchestrator is the CronJob or a separate
workflow engine, or how it learns each day's release times, which live in registrations and leaves.
Without that, CUJ 1's 08:30:05 run has no source.

**Fix:** Add a Terms row for "orchestrator": the deployment's job scheduler, outside this design.
State what it must know (the release times, and where it reads them) and how it relates to the
CronJob that enforces `concurrencyPolicy: Forbid`.

### CMP-13. The "only writer" claim conflicts with `statcat admin`

**Sites:** design:42-43 (Overview), 601 (D9), 676-677 and 680-686 (D11), 210-211 (CUJ 2).

**Problem:** The Overview and D9 say the release job is the only writer of Inventory versions on a
server. D11 then gives operators six commands that write versions on a server, run "under the
release identity". A reader concludes either that operators cannot change a server catalog, or that
humans share the release job's workload identity, and the design gives no mechanism for the second.
CUJ 2's operator relies on it.

**Fix:** Write "only the release identity writes Inventory versions on a server". In D11, say how an
operator runs `statcat admin` under that identity.

## Minor

### CMP-14. Step numbers from four sequences share one namespace

**Sites:** design:269-276 (D1 steps 0 to 7), 614-642 (GC steps 1 to 6), 713, 720 and 1008 (the
expiry engine's steps 3 and 6), 169 (S23 "step 6's leaf-set cache"), 315 ("upload's step 5"), and
spec §11.2 steps throughout.

**Problem:** D13's first sentence uses "step 6 of a release run" for D1's step 6. Two lines later,
"step 6's LIST of `inventory/*.json`" means the expiry engine's step 6. A reader takes S23's "step 6"
for D1's.

**Fix:** Prefix every foreign step with its source: "engine step 6", "GC step 4", "spec §11.2 step
2".

### CMP-15. "Kind" has two meanings

**Sites:** design:229 (O1), 272-275, 337-340.

**Problem:** "Kind `seal`, `release`, `retire`" is the version kind. In `record(..., kind=<section's>)`
and "`record` refuses a `kind` change", "kind" is the product kind (data or structure). The Terms
table defines neither, and nothing says why the fold's version is kind `seal` when nothing seals on
the server.

**Fix:** Add Terms rows "event kind" and "product kind". Write "product kind" in D2's `record` call.
Add one clause to O1 on why the fold writes `seal`.

### CMP-16. "Gate" means three things

**Sites:** design:59 and 61 ("not in the gate"), 120 and 721-722 (expiry gates 1 and 2), 173 (the
"Gates" column), 1035 ("the gate", `just ci`).

**Problem:** "The gate" can mean the CI gate, an expiry gate, or a handoff dependency.

**Fix:** Write "untested in `just ci`" in the table. Keep "gate" for the expiry gates, and rename the
handoff column "Blocks".

### CMP-17. Several numbers have no derivation

**Sites:** design:98-99 (6,300 GETs), 357 (about 3,000 HEADs for 200 editions), 489 (7,003 leaves,
13,300 GETs), 835-839 (NFR rows), 169 and 869 (1.7 to 4.7 GB), 722 (version 0.85), 841.

**Problem:**

- 3,000 HEADs implies 15 files an edition, which the design does not state.
- The "+3" in 7,003 is unexplained (the head root, `CURRENT` and the probe).
- 13,300 appears to be 7,000 leaves plus 6,300 manifests, unstated.
- The fold row's "34 s" counts reads only, while the release row's "4 s" includes the 200 leaf
  writes.
- "About 7,500 manifests" and "2 to 5 minutes" for GC have no basis.
- 1.7 to 4.7 GB comes from the hub design, uncited.
- 0.85 is the stage 1 sdmxlib, unnamed.
- The "30 minutes" of design:841 is CUJ 1's schedule, a different figure from the lease's 30-minute
  cap.

**Fix:** Add a one-line derivation or a source to each figure. Count writes the same way in every
NFR row.

### CMP-18. Terms leave out the Inventory and edition vocabulary

**Sites:** design:67-80, and uses at 269 (`CURRENT`, "the refused variables"), 319 and 326
("bindings"), 346 (`full-artefacts`, `structure-home`, `publish-order`), 412 ("item 8"), 260
(`--state-dir`, unexplained until D13).

**Problem:** A new reader meets root, leaf, head, `CURRENT`, tombstone, the entry states, probe and
bindings with no definition or link. Spec §6.5 items appear by number only. "An edition with no
bindings" means a structure edition in practice, and the reader has to infer it.

**Fix:** Add one Terms row pointing to `docs/catalog-format.md` and spec §6. Write "a structure
edition (no bindings)". Give each §6.5 item a short gloss on first use.

### CMP-19. Q3, Q6 and Q7 leave part of the choice implicit

**Sites:** design:148, 151, 152, 379, 866, 979.

**Problem:** Q3's alternative ("would need a writer for GC to read and a new event kind") is opaque.
Q6 does not state the cost of answering no: refused registrations stay for good, and GC protects
their `data/` files for good. Q6 also says "refuses for age", although every refused registration
eventually turns `too-old`. Q7 names "statcatlib 0.17" without saying that 0.17 is the release
carrying Part 1.

**Fix:** Write Q3's alternative as one plain sentence. Add Q6's consequence of a no. In Q7, write
"0.17, the release with Part 1".

### CMP-20. The land order contradicts the handoff table

**Sites:** design:182-183, 886-889.

**Problem:** "Parts release in order" sits beside "Parts 4 and 5 may land in either order after Part
2" and handoff row 6, "Part 4, which may land before Part 3". The design never says whether "land"
(merge) and "release" (a statcatlib version) differ.

**Fix:** Define both words once, and say whether each Part is its own statcatlib release.

### CMP-21. Small inconsistencies between sections

**Sites and problems:**

- design:598 and 786: `retirable` with and without `skipped=()`, and `skipped` is unexplained.
- design:696 and 706-709: `cache_dir` is used, and `open` takes no such argument.
- design:186 and 528: `expiry_not_before` and `[expiry] not_before`.
- design:282-286 and 731: whether `enabled = false`, a 1.x catalog, or a build before Part 6 counts
  as a skipped step and exits 3 on every run.
- design:644-647: who runs the dry run that `--forecast` consumes, and when.

**Fix:** Align the signatures and the key name. State that disabled or inapplicable expiry exits 0.
Add the dry-run-then-run sequence to CUJ 3.

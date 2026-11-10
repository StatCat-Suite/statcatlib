# Style report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`.

**Verdict.** No Blocker. The draft has no Markdown heading ending in `?`, and no line matches
`PHRASES` in `tools/check_prose.py`, so the prose check passes on this file. Outside a quoted
sdmxlib message (design:434) there is no contrast by negation. The prose is concrete, with real
numbers, paths and `file:line` sites. Most findings fall into four groups: one thing under two names
(an opted-in catalog is also a "2.x catalog"; a registration is also an "inbox" entry and a "pending
edition"); references that rest on context the reader may lack (an unqualified "the window" with
four windows defined, foreign IDs with no source, "the plan", "baked summary", "hub session");
sentences and table cells that pack four to six facts; and statements that recur in up to seven
sections. Counts: 0 Blocker, 8 Major, 7 Minor. Eight factual contradictions seen in passing are
listed at the end.

## What I read

- The persona `agents/style.md` of the EGM plugin.
- statcatlib `CLAUDE.md`, "Writing style" (lines 634-697) and "Issue references" (lines 823-827).
- `tools/check_prose.py`: the heading rule (a heading ending in `?`, outside code fences) and
  `PHRASES`. `docs/design/` is in scope.
- The target, all 1045 lines.
- `docs/design/egm/catalog-on-blob-storage/style_report.md`, for depth and precedent.

## Limits

- No shell, so `check_prose.py` was not run. Its heading rule and every `PHRASES` pattern were
  applied by search, and none matched.
- The spec, plan, Phase 4 design and suite design were not read, so the source of W3, H20, H21,
  H23, P1, P7, S12 and the "D7" at design:310 is unconfirmed here. Rewrites naming a source need
  the author's confirmation.
- Correctness belongs to other lenses. The notes at the end are unverified observations.

## Major

### STY-1. One thing under two or more names

**Sites:** design:41, 271, 276, 291-292, 617-618, 673, 718; 34, 151, 746, 1021; 428, 448, 853; 135,
737.

**Problem:**

- An opted-in catalog is "opted-in Azure catalog" or "opted-in catalog" at design:41, 291 and 673,
  and "a 2.x catalog" at design:271, 276, 618 and 718. The Terms table defines neither, and "2.x"
  is a number with no unit (it is `catalog.json`'s format).
- A registration is "registration" (design:72), "uploads in `pending/`" (design:34), "the inbox"
  (design:151) and "two pending editions" (design:1021).
- The root's extension is "the root's `statcat` section" (design:428, 853) and
  "`extensions.statcat`" (design:76, 147, 246). "Every later version carries the section
  unchanged" (design:448) can be read as the manifest's `statcat` section, which D3 also calls "the
  section".
- "Phase 8 wires them" (design:135) and "Phase 8 routes them" (design:737) name one activity two
  ways.

**Rewrite:**

- Add a Terms row, "Opted-in catalog: a catalog whose `catalog.json` is at format 2.x, after GH-125's
  opt-in (spec §6.9)", and use "opted-in catalog" everywhere. Design:271: "On an opted-in catalog,
  take the `HISTORY` lease for the run (D13)."
- Design:34: "a catalog on blob storage holds registrations in `pending/` that nothing records".
  Design:151: "Refused registrations leave `pending/`, and ...". Design:1021: "The fold refuses two
  registrations on one live edition and one older than the tombstone window". Keep design:746,
  which quotes the spec's alert name.
- Write "the root's `extensions.statcat`" for the root and "the manifest's `statcat` section" for
  the manifest, at design:428 and 448.
- Design:135: "Phase 8 routes them (plan)."

### STY-2. "The window" with four windows in play

**Sites:** design:218, 230, 274, 656, 961, 979.

**Problem:** D7 defines `gc_window`, `tombstone_window`, `inventory_window` and `retire_after`, and
these sentences still say "the window": design:218 "folded registrations older than the window";
design:230 "GC removes folded registrations after the window"; design:274 "past the retire window",
where the key is `retire_after`; design:656 "while its root is in the window"; design:961 "retires
the rest past the window"; design:979 "Folded registrations past the window are deleted".

**Rewrite:** Design:218, "older than `gc_window` (35 d)". Design:230 records an owner decision, so
add a bracketed gloss, "after the window [`gc_window`]". Design:274, "Tombstone superseded and
withdrawn entries older than `retire_after` (D9)." Design:656, "while its root is younger than
`gc_window`". Design:961, "older than `retire_after`". Design:979, "older than `gc_window`".

### STY-3. ID collisions and IDs with no named source

**Sites:** design:9, 28, 62, 63, 65, 85, 230, 310, 357, 668, 721, 762.

**Problem:**

- Design:230, "O2 | ... | Q9: the operator runs the `EmbargoReader` preview". This design's owner
  questions run Q1 to Q8, so "Q9" reads as a missing row; design:125 shows it is the plan's Q9.
- Design:310, "two keys in D7's order". This design's D7 is the jobs configuration; the key order
  is set elsewhere, probably by Phase 4 D7.
- Design:85, "(plan line 350, GH-19)". In this repository GH-19 is statcatlib issue 19; design:7
  names the suite tracker as `StatCat-Suite/statcat-suite.github.io#19`.
- Design:9, "sdmxlib#381 and #383", and design:28, "#384 (`tiers=`), #385". `CLAUDE.md:823-827`
  says a bare `#N` in this tree means the archived GitLab project.
- Unqualified foreign IDs: W3 (design:62, 762), P7 (63), H21 and H23 (65, 232), H20 (721), P1
  (668), "the failure S12 named" (357). Design:100 shows the qualified form, "Phase 4 W11".
- Design:762 puts "W3" in an error message, where the user has no access to design IDs.

**Rewrite:** "Plan Q9" at design:230. "Two keys in Phase 4 D7's order" at design:310 (confirm).
"(plan line 350; StatCat-Suite/statcat-suite.github.io#19)" at design:85. "sdmxlib#383",
"sdmxlib#384", "sdmxlib#385" in full. Prefix each foreign ID with its document at first use, or add
one Inputs line mapping each series (W, F, S1 to S17, H, P) to its document. Design:762:
"`TypeError` naming the Azure-only rule, exit 2".

### STY-4. References the reader cannot resolve

**Sites:** design:9, 15, 25, 59, 61, 63, 91, 120, 159, 179, 236, 459, 574, 591, 668, 722, 901.

**Problem and rewrite:**

- "The plan" (design:9, 15) appears before its link (design:20-22) and never gets a short label, as
  the spec does at design:19. Add '("the plan")' after the link.
- "So no baked summary is built" (design:25, 236). The design never says what a baked summary is.
  Define it in one clause, or cite where the spec does.
- "Delegated by the hub session" (design:179) is workflow vocabulary. Write "An sdmxlib spec PR with
  S13 and, as the owner decides, S18 to S22".
- "History segments keep it with the line" (design:459); "the line" has no referent. Write "History
  segments keep each root's record."
- "Today" and "not in the gate" (design:59, 61, 63). Write "Built (0.16.2)" and "Part 1; no S3 test
  in `just ci`".
- "Under the `HISTORY` lease and both gates" (design:120); the gates are defined 600 lines later.
  Write "after `not_before` and the pod version check (D13)".
- "The registration holds two keys" (design:91). Write "holds only `edition_id` and `release_at`".
- "This design edits neither" (design:159) follows a singular noun. Write "neither the spec nor the
  suite design".
- "Built-in Reader, Contributor and Owner for blob data" (design:574). Write the full role names,
  Storage Blob Data Reader, Contributor and Owner.
- "The Risks table" (design:591): link `#risks-and-limits`. "The writer's rules and the leaf records
  come from D6" (design:668): "The writer reads leaf records first and manifests for unfilled
  entries (D6)."
- "With no API" (design:722): "When the in-cluster Kubernetes API is unreachable".
- "`check_catalog`'s finding for another value" (design:901): "for a `lifecycle` other than `final`
  or `revisable`".

### STY-5. Passive voice and negative form in goals and rules

**Sites:** design:113-116, 350, 543-544, 862.

**Problem and rewrite:**

- Design:113-114, "No edition is recorded whose manifest ... and no edition is released whose
  manifest differs", is negative and names no actor. Write "The fold records only editions whose
  manifest, listed files and references pass D2's checks, and the release step releases only
  editions whose manifest matches the recorded `manifest_sha256`."
- Design:115-116, "Every refusal is persisted with its reason in storage the next run and an
  operator can read, and the batch goes on." Write "The release job writes every refusal and its
  reason to the root's refusal record, where the next run and an operator read it, and records or
  releases the rest of the batch."
- Design:543-544, "Every window that decides a deletion must be written out." Write "The file must
  set every window that decides a deletion."
- Design:862, "the first real run compares the result with the portal's 'Check access'". Write "the
  operator compares the first real run's reader list with the portal's 'Check access'."
- Design:350, "the reads of the first pass are cached by path and reused". Write "the fold caches
  the first pass's reads by path and reuses them."

### STY-6. Statements repeated across sections

**Sites:** embargo flag (design:146, 164, 273, 396-397, 415-417, 420-424, 749); refusal record size
and cap (147, 443, 459, 864, 959); re-runs write nothing (111-112, 278-279, 445-447, 846-848, 956,
977, 1032); the release job as only writer (41-42, 601, 676-677, 819); the 0.94 s read (24-25, 236,
486-487, 824, 830); Q5's same-run release (150, 294-295, 865); counts recorded by tests (842-843,
1041-1044); coverage per PR (854-855, 1040).

**Problem:** Each fact appears in three to seven places, and the wording drifts. "Capped at 500"
(design:147) becomes "Capped at 500 entries" (design:864); "about 300 bytes a refusal" (design:147)
becomes "up to about 150 KB more" (design:459). A later edit has to find every copy.

**Rewrite:** State each fact once in its decision section and refer to it by ID. Cut the Q1 and Q2
trade-off cells to one gain and one cost each, ending "(D4's options)" and "(D5's options)". Delete
the last sentence of design:447, which the Idempotency bullet covers. Cite O8 at design:830. Delete
design:1040, which design:855 states.

### STY-7. Sentences that pack several facts

**Sites:** design:215-219, 279-280, 282-286, 321-325, 485-487, 597-600, 617-621.

**Problem and rewrite:**

- Design:321-325 is one sentence with two pulls, a definition and a provenance. Split it: "The fold
  pulls the edition's own files with `pull_edition` into a temporary directory. It also pulls each
  structure edition `structures=` names. `structures=` holds every other structure product's live
  and embargoed editions, with this fold's structure editions in place of their products' recorded
  ones, chosen as `structure_editions` chooses them locally."
- Design:617-621, GC step 2, puts the read order, a stop rule, a fail-closed rule and the kept-set
  definition into four sentences, and "inclusive" dangles. Make it four bullets: the read order;
  the stop at `HISTORY.expired_through` and the fail-closed rule; one read per distinct leaf; the
  kept set and its `manifest_sha256`.
- Design:215-219, CUJ 3, is one 70-word sentence with six actions. Split it into five sentences:
  the command, the checks and the Inventory read, the manifest reads and LISTs, the deletions, the
  log.
- Design:279-280 mixes a qualified step, a bare step and "the plans of". Write "With `--dry-run`, the
  run performs step 0 without moving `CURRENT`, runs step 2, plans steps 3 to 6, and writes
  nothing."
- Design:282-286 chains colons ("1: the run stopped: a configuration ..."). Make it a list, one exit
  code a bullet.
- Design:485-487: "On Azurite, a cold read of 7,000 leaves fetched 6.6 MB without records and 20.0 MB
  with them. The real account read the 6.6 MB corpus in 0.94 s at 64 threads (GH-134 item 9)."
  Then give the time for the 20.0 MB corpus, or state that it is unmeasured.
- Design:597-600: split after "(`cross_product.cited_structures`)", and start "Part 2 moves
  `admin._retirable` ...".

### STY-8. Table cells too dense to scan

**Sites:** design:146, 168, 269, 718, 722, 802-803.

**Problem and rewrite:**

- Design:269 (step 0) holds six actions and a parenthetical. Reduce it to "Build the store, read
  `catalog.json`, load the configuration, read the storage clock, advance `CURRENT`", with details
  in a list under the table.
- Design:718 (`AzureHistoryLease`) holds five timing facts and a protocol mapping, and design:722
  (Gate 2) holds four sentences. Reduce each cell to its role and list the timings below the table.
- Design:168 (S22) holds two unrelated changes, the deletion log and `--discard`. Split them into two
  rows.
- Design:802-803 put two owners' work in one cell, split by inner colons. Use two columns,
  "GH-125's own design" and "This design's Part 6".
- Design:146 (Q1) has four sentences in each of its recommendation and trade-off cells. Cut each to
  one sentence and refer to D4.

## Minor

### STY-9. Headings and labels against the title rule

**Sites:** design:205, 213, 241-254, 267, 422-423, 457, 622, 628, 640, 716, 1037.

**Problem and rewrite:**

- "CUJ 2: An operator handles a refused release" and "CUJ 3: GC runs at night" are clauses. Write
  "CUJ 2: Handle a refused release" and "CUJ 3: Nightly GC run".
- Design:422's chosen option is a clause where its siblings are noun phrases. Write "`embargo: true`
  as the hold on automatic releases (chosen)".
- Design:267 and 716 head a column "What it does". Write "Action" and "Role".
- Design:457, "Option (Q2)": "Option for the refusal record (Q2)".
- Design:622, 628 and 640 are run-in labels in shapes that differ from "**Start.**" and
  "**Manifests.**". Write "**Registrations.**", "**Plan.**" and "**Delete.**", each followed by a
  full sentence.
- Design:1037, "Verification Plan", uses title case among sentence-case siblings. Write
  "Verification plan".
- Design:252-254: D11 to D13 are noun phrases in a column of full-sentence decisions. Write them as
  sentences.

### STY-10. Subjective, thin or allusive cell text

**Sites:** design:146, 149, 150, 164-168, 357, 422, 424, 462, 550, 743, 837, 839, 867, 884, 958,
981, 1009.

**Problem and rewrite:**

- "The name reads oddly: a server that releases on schedule has `embargo: false`" (design:146) and
  "a name that reads backwards there" (design:422) are subjective, and the first is a colon setup.
  Write "On a server, `embargo: false` is the normal state, though every edition still passes
  through `embargoed`."
- "Gives the plan's gating test ... a meaning" (design:146, 422). Write "The plan's test of a stale
  automatic release under embargo then tests a held run."
- "A clear name" (design:424): "The field name states the hold." "One job and one precondition"
  (design:149): "The release job runs the check it depends on, with no second identity." "Releases
  on the announced time, which is what an agency publishes" (design:150): "The table goes live at
  the time the agency announced."
- "Proposed, with Q1" (design:164-168): "Proposed; follows Q1's decision."
- Design:357 puts a cost in the Gain cell ("One `HEAD` a file: about 3,000 for 200 editions"). Move
  it to Cost.
- "Beside the entry" (design:462): "The refusal sits in the refused product's leaf." "Comments;"
  (design:550): "TOML allows comments;". "`current.behind`, then advanced" (design:743):
  "`current.behind`; the run then advances `CURRENT`".
- "A typical day", "a few leaves" (design:837) and "distinct leaves" (design:839) need counts.
- "A cache like expiry's can follow if a measurement asks for it" (design:867): name the trigger.
- "GH-125 stage 2's" (design:884): "the sdmxlib release GH-125 stage 2 requires".
- Design:958 states no outcome. Write "A registration built on the live edition withdraws the
  embargoed entry with the same `previous` (`supersede_pending`)".
- "Reports each side's extra" (design:981): "logs `gc.forecast_deviation` for each path in one plan
  only". 'No API gives "not run"' (design:1009): "with no Kubernetes API, the run logs
  `expiry.pod_check_not_run`".

### STY-11. Punchline endings and wordplay

**Sites:** design:105, 151, 483, 652, 672.

**Problem and rewrite:**

- Design:483, "A daily product is filled within a day, and a `final` product never is", ends on a
  short twist. Write "A daily product's entry is filled at its next record. A `final` product's
  entry stays unfilled until the product records a new edition."
- Design:652 ends on an appositive verdict, "the safe direction". Write "A retire after GC's read
  leaves the edition in this run's kept set, so GC deletes its files on a later run."
- Design:672, "when `CURRENT` was current", plays on the name. Write "when `CURRENT` already names the
  head".
- Design:105, "(final means final)", is a slogan. Write "(a `final` product's `content_fingerprint`
  does not change)".
- Design:151, "The inbox drains", is a metaphor. See STY-1.

### STY-12. Fragments and colon setups

**Sites:** design:6, 173, 191, 225, 543, 563-564, 726-729, 752, 815, 849, 852.

**Problem and rewrite:**

- Design:6, "Its Inventory parts: GH-125 part 4, and ...": "It also covers GH-125 part 4 and the
  server half of GH-125 part 3."
- Design:173, 'In order. "Gates" names ...': "Rows are in landing order. The Gates column names the
  work that waits for the row."
- Design:191, "sdmxlib#384 is Phase 6's.": "Phase 6 adopts sdmxlib#384 (`tiers=`)."
- Design:225, "They are inputs.": merge into the sentence before.
- Design:543, "the spec's": "Only `keep_roots` (10) and `upload_grace` (24h) have defaults, both from
  the spec."
- Design:563-564 ends in a colon with no verb: "`AzureReaderListing(...)` in `access.py` makes these
  requests through `httpx`, with tokens for ...".
- Design:726-729: "GH-125's local design defines the engine's interface, and Part 6 implements it on
  blob."
- Design:752: "`obstore` retries a storage 503, and a `BlobStoreError` after those retries exits 1."
- Design:815: "The three choices below span several decisions."
- Design:849, "No key or SAS.": "The jobs authenticate with workload identities and hold no account
  key or SAS." Design:852, "No Inventory or manifest format change.": "The Inventory and manifest
  formats stay as they are."

### STY-13. Numbers without units or time zones

**Sites:** design:147, 197-203, 215, 722, 959.

**Problem:** CUJ 1 gives 06:00, 08:00, 08:05 and 08:30:05 with no zone, and only the `--release-at`
instant has one. CUJ 3's 02:00 has none. "Capped at 500" (design:147, 959) does not say 500 of what.
"A version below 0.85" (design:722) does not name the package.

**Rewrite:** Open CUJ 1 with "Times are Eastern (UTC-5)" and give CUJ 3 a zone. Write "capped at 500
refusals" and "an `sdmxlib_version` below 0.85".

### STY-14. Needless words and loose pronouns

**Sites:** design:46-47, 142, 187, 190, 278-280, 301, 445-447, 478, 508, 571, 601, 889, 899.

**Problem and rewrite:**

- Design:142: "The design follows each recommendation until the owner decides."
- Design:46-47: "Six parts build them:".
- Design:278-280 says "writes nothing" three times in two sentences: "A step with nothing to do
  writes no version, so a re-run against an unchanged catalog writes none."
- Design:301: delete "anyway".
- Design:508, `backup = "30d"  # backup retention`, repeats the key. Write "# how long storage
  backups are kept".
- Design:478 and 899, "go": "are removed".
- Design:571, "no not data action does": "no `notDataActions` entry matches it".
- Design:187, "so it gates nothing": "so sdmxlib#380 gates nothing".
- Design:190, "which the fold and GC treat like any file list", has no clear antecedent: "The fold
  and GC treat those files like any other listed file."
- Design:445-447 uses "it" for the step and then for the record. Name each.
- Design:601, "The release job is then the only writer", reads as sequence or consequence: "With
  retirement in the release run, the release job is the only Inventory writer on a server".
- Design:889 uses "release" and "land" for one act and contradicts itself. Write "Each part merges
  after the parts in its Waits on column; Parts 3, 4 and 5 may merge in any order after Part 2."

### STY-15. Mixed polarity in the fold's check table

**Sites:** design:311, 312.

**Problem:** D2's "Check" column states the passing condition in eight rows and the failing one in
two: "The Inventory records the edition in the product's leaf or `retired`" and "The `edition_id`'s
UUIDv7 time is older than the tombstone window". A reader scanning the column reads those two
backwards.

**Rewrite:** Design:312: "The `edition_id`'s UUIDv7 time is within the tombstone window by the
storage clock." Design:311: "The Inventory records the edition in neither the product's leaf nor
`retired`", with the rule cell "skipped when recorded (O1)".

## Checked and not flagged

- The question marks in the owner questions table (design:146-153): open decisions in table cells,
  outside the heading check.
- `ID: noun phrase` headings such as "D1: the shape of `statcat release`", which follow the earlier
  report's precedent.
- The gap list in "The Problem" (design:93-107) and negative claims such as design:352 and 611-612,
  where the negative statement is the reported fact.
- The "but" at design:434, inside a quoted sdmxlib refusal message.

## Factual notes in passing

- CUJ 2 (design:207-209) says the refusal is "with `fast-forward`" and names "the rule"; D4 and D5
  record `rule: null` for `release_due`'s refusals.
- Design:300 cites "the lease's 30-minute cap"; D13 describes a 60 s lease renewed every 20 s, with
  the 30-minute limit on the run's hold.
- Design:24-25, 236 and 830 cite 0.94 s generally; design:486-487 says it was the corpus without
  records.
- Design:26 lists decisions from three dates; O8 is dated 2026-11-10.
- `retirable` has `skipped=()` at design:786 and not at design:598.
- Design:552 credits JSON with "No new parser", and any format needs D7's duration parser.
- D12's `open(location)` takes no `cache_dir`, and design:708 refers to "its `cache_dir`".
- Design:483 says a `final` product's entry is "never" filled, while design:318 shows a `final`
  product recording new editions.

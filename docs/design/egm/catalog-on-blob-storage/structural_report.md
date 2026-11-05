# Structural review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standards:** the EGM feature-spec and implementation-readiness standards.
**Reviewer:** `egm-structural`.

**Verdict as reported:** does not pass yet. 1 Blocker, 15 Major, 10 Minor.

**Limitation.** The reviewer had no git command. It read the sdmxlib spec from the
working tree at `011312c3`, the tip of `docs/phase-4-restated`, and could not read the
`11f18e3f` or `cf11dd1a` blobs. It cannot say which of those commits introduced a given
spec line.

## Checked and found in order

- Section order matches phase-2a: Overview, Problem, Goals, Non-Goals, Open Questions, CUJs, Technical Plan, Alternatives Considered, NFR with Risks, Detailed Implementation Plan, acceptance map, Verification Plan. Heading levels skip nothing. Tables and code fences are well formed.
- The Design decisions table maps D1 to D7 one-to-one onto GH-98 questions 1 to 7.
- Each of D1 to D7 has an alternatives table.
- S1 to S11 and W1 to W6 run in order with no gaps. Every S row has an owner and a proposal.
- These internal references resolve: "Test setup" (186), "the risk table" (619, row at 864), "table below" (672, table at 683), "(below)" (399, text at 405), "Part N".
- The acceptance map has 14 rows for GH-98's 14 checks.
- Parts 1 to 5 follow GH-98's order and scope.
- NFR cost numbers agree with D1 (3 writes, 9 reads, 200 leaf Puts).
- NFR's 70 s and 1.1 s are arithmetically consistent with Part 5's formula.
- SITEMAP and mkdocs nav both list the design.
- Nothing references O1 to O3.

## Blocker

### STR-1. Contract changes are not sent back first

**Target:** design. **Citations:** lines 141, 143-155, 425-427, 463, 698; GH-98 line 26 ("A question that needs a contract change goes back to the specification first"); hub `CLAUDE.md` lines 68-69.

Line 141 says no S row blocks Part 1 unless stated, but Part 1 ships D3 and D4, which rest on S2 and S3. D3 step 2 records a caller-declared actor while spec §6.2 (line 370) says "never from the caller", so Part 1 would land code that contradicts the spec's text until S2 resolves it. The design has no Handoff section, no sdmxlib issue, no spec PR scope and no sequence for S1, S2, S3, S7, S8, S9, S11. Only S6 (881) and S10 (961) appear in any Part. No row says whether the hub suite design needs a change. Hub revision 4's sdmxlib section owns "the writer rules" (hub 805-809), and D7 adds a new rule and tombstone semantics.

**Consequence:** the design can be approved and Part 1 merged with its contract questions unanswered, against both the issue and the hub rule.

**Fix:** add "Contract changes and handoff". List each S row with its target (spec PR, sdmxlib code or hub design), the issue, the Part it gates and its order. Make S2 and S3 gate Part 1, and S10 and S8 gate Part 4. State whether the hub design changes, and with what. Replace line 141.

## Major

### STR-2. S8 is stale

**Target:** design, against the spec at `011312c3`. **Citations:** S8 line 152; D7 line 698; spec line 427 ("A tombstone comes from a `retire` or `correct` version"); spec line 519 (a `correct` row in the §6.4 table, "any state" to tombstone).

S8 quotes wording and a table gap that are absent at `011312c3`. That commit's log message says it records the 2026-11-04 Phase 4 decisions.

**Consequence:** D7's "one spec change (S8)" asks for work that branch has already done. The design's account of the spec is one commit behind.

**Fix:** re-read at `011312c3`. Rewrite S8 to what remains, namely the refusal and the embargoed case (STR-25), or close it.

### STR-3. The status block does not pin what it relies on

**Target:** design. **Citations:** lines 10-13, 18-20, 24.

"Read against" names no spec revision (sixth revision, Inventory 1.2) and does not name the spec commit used for the design's spec citations beyond "for the leaf rule ... `cf11dd1a`". The Inputs link goes to the published site, which follows `main`, while line 24 says §6.6 "is being changed". W1 to W4 rely on text on the unmerged branch: line 266's "§11.2's S3 clause" is at spec line 969 on that branch. Inputs lists §4.1, §4.6, §6.1 to §6.7, §6.9, §10.1, §11.2, §11.5, and the body also relies on §4.7 (148, 548), §7.1 (148, 1024), §11.3 (619) and §11.4 (112, 662). The plan is an input by reference (line 17), with no commit.

**Consequence:** a reader cannot tell which spec text any § citation means. STR-2 is the result.

**Fix:** name spec revision and commit, plan commit, and whether the branch must merge first. Complete the § list.

### STR-4. D7's "The sdmxlib changes" disagrees with the S table and Part 4

**Target:** design. **Citations:** 697-701, 709, S8 152, S9 153, S10 154, S11 155, Part 4 961.

D7 says "one spec change (S8)", yet S9 and S11 are also "code and spec". S11 says "Does not block", while line 961 makes a conformance check part of the "sdmxlib (first)" step and line 709 relies on it. S10 mentions no conformance check. S6 is also an sdmxlib change and has no cross-reference here.

**Consequence:** the sdmxlib release's contents, and what gates Part 4, read differently in three places.

**Fix:** keep one list, in the STR-1 section. D7 refers to it and restates nothing.

### STR-5. O4's dependents are unstated, and a claim about them is false

**Target:** design. **Citations:** 134, 152, 646, 658, 671, 697, 751-752, 851, 978.

Line 134 says D7 and Part 4 "name what changes under the others". They only flag it ("pending O4", "These rows change"). "No format change" (851), "format and its validators need no change" (697) and "`try_commit` and `Change` need no new field" (658) hold only under options 1 and 3. Option 2 is an optional-field format change (131). O4 does not say which Part it blocks. Line 671 suggests S6 and S10 ship together, which would make Part 1's S6 wait on O4, since O4's choice lands in the S10 function.

**Consequence:** when the owner picks an option other than 1, nobody has a list of what to edit, and the release pairing can stall Part 1.

**Fix:** in O4 add a per-option list of lines and tests that change, and state "blocks S10 and Part 4, not Part 1". Qualify the three absolute statements. Remove or condition the S6 and S10 pairing.

### STR-6. S6 gives two answers about blocking

**Target:** design. **Citations:** S6 150 ("Blocks D5's reader; the fallback writes each manifest to a temporary directory"); 141; 881 ("if it ships in a new minor"); 889 ("Needs S6").

**Consequence:** the Part 1 implementer must decide whether to wait or build the fallback, which fails the Zero-Decision Rule.

**Fix:** pick one. Either S6 ships first and the fallback is deleted, or Part 1 uses the fallback with a named replacement step.

### STR-7. Upload and the test tier sit outside the numbered decisions

**Target:** design. **Citations:** 271-281, 369-411, 769-803, 805-829, 833, 892-895, Overview line 50; GH-98 "Test rig".

Line 833 claims "Each decision above carries its own table", but Upload has no D number and no alternatives table, only two prose alternatives under File size. Upload's own choices have no recorded alternatives: head then create, the verification sequence, and the size refusal. The `stores` tier is in the Overview and is a GH-98 section, but its design is split across D2 (405-411), W6, a Part 1 bullet (892-895) and a risk row. `### Test setup` holds only `record_uploaded`, so its name misleads a reader looking for the tier. It is absent from the decisions table and from the Overview. The D2 row's second sentence (276) and D2's second half (385-411) carry upload's digest decision under a question about packaging.

**Consequence:** this fails the Alternatives Rule for one of five Parts, and a reader of the decisions table misses three decisions.

**Fix:** add D8 Upload (moving 385-403 there, with a table) and D9 Test rig (tier plus `record_uploaded`). Mark them "scope, not a GH-98 question" in the Question column.

### STR-8. `statcat upload` has no defined way to build its location

**Target:** design. **Citations:** 161-162, 376-377, 390-395, 775, 923.

Line 775 says it "builds the location from the environment". It names no variables, credential type, store construction or URL parsing. D2 says azure-identity "stays the caller's dependency", but the CLI is the caller, and its production path needs a `TokenIdentity` wrapping a real credential.

**Consequence:** CUJ 1, the primary journey, requires the implementer to design the CLI's credentials and possibly add a dependency.

**Fix:** state the environment variables, the credential class, its import and install rule, and the error when it is absent.

### STR-9. The upload timeout has no value or home

**Target:** design. **Citations:** 254-256, 533, 769-803, 936.

Line 255 says upload "sets its own `timeout` in `client_options`". D6 says the caller builds the store (533), upload takes that store in `BlobLocation`, and neither the Upload section nor Part 2 mentions a timeout. Line 256's "Part 2 tests a large file" is row 936, a 64 MiB one-PUT case, which covers F11 and says nothing about timing.

**Consequence:** an open item on the Not-confirmed list points to a mechanism with no design and a test that does not exercise it.

**Fix:** decide the value and who sets it (a required caller option or a CLI-only setting), and add a row that tests it, or move it to the manual pass.

### STR-10. Plan and prose name things nothing defines

**Target:** design.

- `read_many` (885) appears nowhere else.
- NFR's "1.1 s at 64 threads" (848) and Part 5's "64-thread leaf prefetch" (1010) have no design. D5's prefetch covers manifests only, and D6 line 587 gives `products()` as 7,000 GETs.
- "when its storage says to" (334): the `InventoryStorage` protocol (285-298) has no such member.
- `_verify_edition` on blob pulls "as in D6" (712), but `cache_dir` is only required by `Catalog.open` (525, 538), so `CatalogAdmin` with `cache_dir=None` is undecided.

**Consequence:** Zero-Decision failures, and the NFR's concurrent figure has no owner in production code.

**Fix:** define each one, or for `read_many` state that it is bench-only and correct the NFR.

### STR-11. The Risk table's clock fallback contradicts D4 and the Part order

**Target:** design. **Citations:** 862, 467, 475; Part 1 910; Part 2 919.

Line 862 calls a bearer `HEAD` on `catalog.json` "the fallback on a token credential". D4 says a failed request refuses and has no fallback, Part 1's clock row tests none, and `AzureBlobRest` arrives only in Part 2.

**Consequence:** the mitigation for a listed unconfirmed risk is unbuilt, or the implementer adds it unprompted.

**Fix:** either add it to D4 and a Part with a test, or restate the row as "refuses; the fallback is a later change".

### STR-12. Acceptance map file names have no source in the Parts

**Target:** design. **Citations:** 1035-1042; tables at 926, 949, 969; 966; 1019.

`test_upload.py`, `test_remote_catalog.py`, `test_correct.py` and `test_reinstate.py` appear only in the map, with no directory. Parts 2 and 3 have a Tier column and no File column. Part 4 has neither, and line 966 says each test runs in the integration tier and also once over Azurite. Tiers are mixed: a unit row at 932, and "stores, unit" in one row at 934. `CLAUDE.md` requires a `pytestmark` per file (737-739), so one file cannot hold mixed tiers without a decision. Part 5's 50-product test (1019) has no file.

**Consequence:** fails Target Scope, and the map cannot be checked against a Part.

**Fix:** give Parts 2 to 5 Part 1's table form with full paths, including how Part 4's Azurite runs are placed. Use the same paths in the map.

### STR-13. Target scope lists documents and exports that do not exist or are absent

**Target:** design. **Citations:** 827, 924, 944, 964.

"the operator guide" is no page in `docs/`, and no page under `docs/` outside `design/` uses the term. `docs/catalog-format.md` has no mention of `pending`, so "`pending/` section" is new work described as an existing section. "`docs/`:" names no file, and a new page needs a nav entry or `mkdocs build --strict` fails (`CLAUDE.md` 750-752). No Part lists `src/statcatlib/__init__.py`, although CUJ 2 (170-172) uses `BlobLocation` and `TokenIdentity` and phase-2a and phase-3 both list it. No Part lists a `docs/reference` page for the new modules, or the `docs/upgrading.md` entry.

**Consequence:** implementers guess the public names and doc locations, and the gate can fail on nav.

**Fix:** name each file with (Create) or (Modify), and state the exports, including how lazy `obstore` import coexists with them.

### STR-14. Stated behaviours have no test row

**Target:** design.

- Part 2 (926-936) has no row for: `statcat upload` itself; `--release-at` and the `pending/` body; a crash followed by a rerun (CUJ 1, line 165); an unsealed directory; the over-limit refusal (800); `manifest.json` present with other bytes (787).
- Part 1 (900-913) has no row for: `prune` refusing a blob catalog (111, 891); `InventoryWriter.root` raising (352); a direct test of `CatalogAdmin.create` (530).
- Part 4 has no row for `correct` with no change raising `ValueError` (656).

**Consequence:** fails the Test Plan requirement on edge cases.

**Fix:** add the rows.

### STR-15. The Risks table does not cover the Not-confirmed list or the standard's areas

**Target:** design. **Citations:** 240-256, 856-866, 1049-1051.

The Not-confirmed items on real-Azure bearer MD5 and managed-identity `oid` sit under one catch-all row, yet the bearer path is upload's production path (391), and its failure stops upload entirely. AWS has no row, although W3 keeps the Inventory writer on S3 (266). Line 1050 claims the manual pass answers "the unconfirmed list", but it covers 4 of 6 items, omitting AWS and the timeout.

Absent risks: memory up to 5,000 MiB per file on upload (799); O4's early-publish path (125-126); `record_uploaded` shipping in the wheel (827); a declared actor being unverified (443); an unverified JWT decode (417); `pending/` entries stranded until Phase 5; the sdmxlib release dependency for Parts 1 and 4.

**Consequence:** fails Risks and Limits (security implications, scalability limits).

**Fix:** one row per Not-confirmed item and per risk above. Make line 1050 match, or say which items stay open.

### STR-16. The Non-Functional section is incomplete

**Target:** design. **Citations:** 844-854, 496-503, 588, 799.

It has no security or embargo entry; D6 (604-619) holds that content with no pointer. It has no memory or disk bound: 91 MB in transit, 5,000 MiB per upload, `cache_dir` growth. Latency gives 7,000 leaves but omits the 6,300-manifest cold read, which is D5's reason for existing, and the Inventory-read `refresh()` costs. Coverage names only `store/blob.py`, while `upload.py`, `AzureBlobRest` and `pull_edition` are tested almost wholly in `stores` (926-957), which the 85% floor's selection excludes.

**Consequence:** `just coverage` may fall below the floor, with no plan for it.

**Fix:** add those entries, and state unit coverage or an exemption for those modules.

## Minor

### STR-17. Question labelling is uneven

**Target:** design. Only Q7 has a label in the body (724). D1 to D6 carry none, and GH-98's questions are never restated, so the Question column (273) means nothing without the issue. D2 to D6 open with "Recommendation"; D1 and D7 do not, and D1 alone says "Alternatives:" (338). Rows D2 and D7 are narrower or wider than the question: D2 adds upload's lookup, and D7's row omits `reinstate`, the new rule and W2. **Fix:** open each D section with "Question N: <text>. Recommendation: ...".

### STR-18. D5 line 483-485 is out of date

**Target:** design. It says the rules read manifests on blob "only" in `release`, `rollback` and `correct`, and "No seal runs there". `record_uploaded` commits `event.kind: seal` with the rules reading uploaded manifests (818-820). **Fix:** add it to D5.

### STR-19. Prose conflicts with detail

**Target:** design. CUJ 4 (201) and 743-744 say `rollback` can make a reinstated edition live, unconditionally; lines 695 and 699 and S9 refuse this with no live edition. CUJ 3 (193-194) says "An edition sealed after 39 is tombstoned", while D7 uses "recorded" and keeps embargoed entries (645). **Fix:** add the condition and the term.

### STR-20. Numbering

**Target:** design. F17 sits after F22 (238). O4 is the only O number, and nothing explains the absent O1 to O3 (120). S5 ("statcatlib, no spec change", 149) is no contract question, despite line 118's framing. "The unconfirmed list" (860, 1050) refers to a heading reading "Not confirmed" (240). **Fix:** reorder F17, rename O4 to O1 or add one sentence on the earlier numbers, move S5 to Upload, use one term.

### STR-21. Risk and verification claims lack backing

**Target:** design.

- Line 860's "the signing" is left from revision 1. The production path is now a bearer token (391).
- Line 860 puts the manual checklist in Part 1's PR, but Part 1's list has none, and its bearer `HEAD` item (1051) needs Part 2's `AzureBlobRest`.
- Line 861 says the tier reruns F1, F2, F10 and F11's cases. Part tables have no F10 row, and row 908 is `MemoryStore`, so F2 does not rerun on an emulator.
- Line 838 says "Part 1 asserts" `x-ms-blob-type`. No row does, and row 906 observes through `_ObjectOps`, which wraps calls and does not see headers on the wire.

**Fix:** correct the wording, add the rows, and name a recording proxy if headers must be asserted.

### STR-22. Hub references

**Target:** design. "lease section" (27, 314) is no hub heading. The text is the "On Azure Blob Storage" list under `### Expiry` (hub 451-472). GH-98 cites H12 here. Inputs name H11 and H24, which the body never cites, and omit H12, which GH-98 relies on for this rule. **Fix:** cite "### Expiry" and H12, and drop or use the others.

### STR-23. Line 871 tells each part to update `CHANGELOG.md`

**Target:** design. Phase-2a line 917 and phase-3 line 1011 say slice PRs do not edit it. `just release-2` generates it with git-cliff (justfile 136, 161-166). Line 871 also omits phase-3's `docs/upgrading.md` and `uv run python tools/count_any.py --write`, though `just check` runs the `Any` ratchet. **Fix:** use phase-3's sentence.

### STR-24. The plan's layout differs from the house form

**Target:** design. Phase-2a and phase-3 open with `### Slices`, and phase-3 has a "Waits on" table (1015-1020). Both use numbered `#### N. file (Modify)` headings. This plan has no summary table, so its cross-repo waits are only in bullets (881, 961), and Parts 3 and 4's dependence on Part 2's `record_uploaded` is only in prose. Part 5's title drops GH-98's "Corpus on blob storage". The map is "Acceptance map", where phase-3 has "Acceptance test map". Future work is scattered (D2 383, D4 479, 801), where phase-2a has `### Future Improvements`. **Fix:** add a Parts table with "Waits on"; gather future items.

### STR-25. §6.4 and the new rule

**Target:** spec. Spec line 519 gives "any state" to tombstone, with no refusal when a correction would drop a product's live entry. D7 step 4 raises `TransitionError(rule="tombstone")` there (641-644). Spec line 528, on the rules as pure functions, says nothing of `correct`. **Consequence:** a sdmxlib implementer of S10 has no spec text for the refusal, and "any state" will read differently depending on O4. **Fix:** S8 or S10 carries both once O4 is settled.

### STR-26. Plan and GH-98 wording on upload's verification

**Target:** plan and GH-98. Plan line 330 and GH-98 line 61 say upload verifies every listed file "with `check_edition(at="receive", lookup=)`". S5 records that this covers `data/` only (`_check.py:204` confirms it), and the design adds `verify_remote`. S5 says no spec change but names no plan or issue edit, although line 16 relies on GH-98 as the scope. **Fix:** amend the plan and GH-98 wording, or note the override in S5.

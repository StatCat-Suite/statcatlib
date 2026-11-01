# Stage 7: Critic Review report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md),
  FeatureRange (lines 1–678)
* **Reviewers:** three fresh-context Goldfish reviewers:
  - `egm-design`, against the feature-spec standard;
  - `egm-structural`, against the feature-spec standard and the template;
  - `egm-style`, against the CLAUDE.md writing rules.
* **Date:** 2026-11-01
* **Excluded:** findings that Stages 5 and 6 already raised.

**Totals:**

| Reviewer | Blocker | Major | Nit |
|---|---|---|---|
| Design | 1 | 8 | 5 |
| Structural | 1 | 8 | 9 |
| Style | 0 | 15 | 15 |

## Elephant notes

- **DES-B1 is confirmed.** `seal` writes the journal entry first
  (`sdmxlib/edition/_write.py:564-570`). It then runs
  `compact_for_publication` and the hashing, and only after that writes the
  manifest.
- **DES-M5 is confirmed.** Spec §4.6 (`sharded-bundle-catalog.md:264`): the
  `receive` profile checks `root: data` files by size only. Only `publish`
  checks their digests.
- **The design findings overlap with Stage 5 and 6 findings.**
  - DES-M3 (the prune race) adds to Stage 5 B1 and M1.
  - DES-M2 (repair racing a seal) adds to Stage 6 M7.
  - DES-M8 (local embargo) and STR-B1 (no security analysis) describe the
    same gap.
- **Many findings come from the same few root causes.** Fixing those causes
  closes several findings at once:
  - **The repair and journal lifecycle:** DES-B1, M1, M2 and M7.
  - **Prune's scope:** DES-M3, plus Stage 5 B1 and M1.
  - **A risks and security section:** STR-B1 and DES-M8.

## Design (`egm-design`)

### DES-B1 (Blocker). A crash between the journal entry and the manifest locks the product

- **Lines:** 377–389, 419–421, 99–101.
- **Problem:** `repairable` returns a journal entry for an edition that has no
  manifest, and repair step 1 then fails reading that manifest.
  - `working_copy()` and `resume=True` both run the repair first, so both
    fail.
  - `discard()` exists only on a WorkingCopy, so the user cannot reach it.
- **Fix:**
  - Repair only entries whose edition directory holds a manifest.
  - Treat any other entry as an interrupted seal: refuse to resume it, and
    add a product-level discard (for example
    `Product.discard_working_copy()`) that also clears the journal.
  - `doctor` reports these entries.
  - Add this crash point to the G5 test.

### DES-M1 (Major). Journal entries of recorded editions are never cleared

- **Lines:** 377–381 and 415–417.
- **Problem:** A crash between the commit and `clear_journal` leaves a stale
  journal entry. If the tombstone later expires, or `correct` drops the
  edition, the repair re-records withdrawn data, which RFC D14 forbids.
- **Spec gap:** spec §6.7 checks every root within retention; the design
  checks only the newest.
- **Fix:**
  - Clear the entries for editions the head names or tombstones.
  - Prune clears the journal entries of the editions it deletes.
  - Check every retained root before recording.

### DES-M2 (Major). The repair races an in-flight seal, and the outcomes after step 5 are undefined

- **Lines:** 377–381 and 398–429.
- **Problem:**
  - Process B's repair records process A's sealed edition. A's record then
    raises `duplicate`, so A reports failure although the edition is live.
  - `InventoryContentionError` keeps the journal entry, so the edition goes
    live later without notice. The late-refusal path clears the entry
    instead.
  - The seal-time repair from spec §6.7 is dropped without saying so.
- **Fix:**
  - Treat `duplicate` as success when the `manifest_sha256` values match.
  - Write an outcome table for steps 5 and 6 that gives what happens to the
    journal entry in each case.
  - Restore the seal-time repair, or record why it is dropped.
  - State the assumption of one writer per product.

### DES-M3 (Major). Prune can delete a live edition, and step 3 ignores the GC window

- **Lines:** 499–514.
- **Problem:**
  - Prune reads the head, then scans. A seal can commit and clear its
    journal entry between those two steps.
  - The age comes from the UUIDv7, which records when the build started, not
    when the edition was sealed.
  - Step 3 has the same race, and it never applies the `data/` GC window.
- **Fix:**
  - Scan first, then re-read the newest root.
  - Age editions by the manifest's `created_at`.
  - Apply the GC window to each file's age.

### DES-M4 (Major). `view()` never verifies `manifest_sha256`

- **Lines:** 351–352 and 432–435.
- **Problem:** Spec §6.3 requires the check before any other read.
  `EditionEngine.attach` takes no digest (`local/engine.py:115-142`).
- **Fix:**
  - Hash `manifest.json` and refuse a mismatch.
  - State that a view stays pinned to its edition after `refresh()`.

### DES-M5 (Major). The `receive` profile skips the data-file digests

- **Lines:** 130–132, 217 and 522–525.
- **Problem:** A bit flip in a Parquet file passes `verify-catalog`.
- **Fix:** Use `at="publish"`, or add a digest pass over `data/`, and
  measure its cost.

### DES-M6 (Major). `correct` is unsafe

- **Line:** 493.
- **Problems:**
  - It has no expected-head precondition, so a retry erases concurrent
    commits.
  - It drops tombstones added since `from_seq`.
  - It can resurrect editions whose bytes were pruned.
  - Copying the old embargo flag can turn embargo off.
- **Fix:**
  - Add `if_seq`.
  - Take the union of the old and current tombstones.
  - Drop entries that the current head tombstones.
  - Verify the files as `reinstate` does.
  - Keep the current embargo setting unless the caller passes one.

### DES-M7 (Major). A crash during a batched seal splits the batch

- **Lines:** 359–361, 377–381 and 664–665.
- **Problem:** The repair is per product and runs lazily. After a crash, the
  editions of one batch are recorded one at a time, which breaks the RFC's
  atomic release (§7, §12) and the structures-first order.
- **Fix:** Repair reads every product's journal and records the result in one
  commit, at the start of `Catalog.seal` and `working_copy()`.

### DES-M8 (Major, owner decision P1). Local embargo is overstated

- **Lines:** 141–147, 228 and 533–534.
- **Problem:** On disk, any reader of the files, any backup, and any synced
  folder can see an embargoed edition. The RFC's D5/D18 docs check has no
  goal.
- **Fix:**
  - Restate P1's consequence.
  - Add a goal that documents the boundary.
  - Refuse or warn on `embargo=True` under a synced folder.

### Design nits

- **DES-N1.** `os.link` fails on exFAT and some SMB mounts.
  - Probe for support at create.
  - Specify the `renamex_np`/`renameat2` fallback.
  - Say who removes leftover `.tmp` files.
- **DES-N2.** RFC D6 and D9 map to no goal. The conformance suite has no
  journal or repair cases, so G5 needs named crash points.
- **DES-N3.** A refusal from `sdmxlib.seal` comes after step 3 has dropped
  the carried tables. State the WorkingCopy's state at that point, and
  either keep the vectors until the manifest exists or accept a full
  re-encode on resume.
- **DES-N4.** `rollback` needs a `schema_version`. Say where it comes from
  and how a caller reaches the other head during a rollout.
- **DES-N5.** On Windows, deleting an attached edition directory fails
  partway. State the retry behaviour and report partial deletions.

## Structural (`egm-structural`)

### STR-B1 (Blocker). No security analysis and no Risks section

- **Lines:** 654–665; risks are scattered at 229, 231, 253–257, 327–331 and
  423–429.
- **Problem:** The standard requires failure modes, security, scalability
  and mitigations. Two security limits go unstated:
  - local embargo is not access control;
  - `event.actor` is an unauthenticated OS user and host.
- **Fix:** Add a "Risks & Mitigations" section with one row per failure mode,
  its mitigation and where the design handles it. Include contention and
  growth.

### STR-M1 (Major). Goals promise named tests that are missing

- **Lines:** 80, and G3, G6, G7, G9, G12 and G13.
- **Problem:**
  - The goals cite RFC D23/D28/D32/D26, which have no acceptance rows.
  - The goals omit RFC tests D4, D9 and §4.
  - "RFC Dn" is ambiguous between a decision and a test.
- **Fix:** Give each goal a "Verified by" line, write "RFC decision N" in
  full, and map D4, D9 and §4 to goals.

### STR-M2 (Major). P5 is out of scope, and The Problem's questions have no pointers to their answers

- **Lines:** 65–75, 162–168 and 224–257.
- **Fix:**
  - Add P5 to the Decisions table.
  - End each question in The Problem with "Settled by …".
  - Make Open Questions one sentence.

### STR-M3 (Major). Seven design decisions have no IDs and no rejected alternatives

- **Lines:** 233–257.
- **Fix:** Number these decisions. Record the rejected options for prune
  timing, the fsync workaround and the embedding cache.

### STR-M4 (Major). G6 duplicates the CatalogAdmin API and has drifted from it

- **Lines:** 102–110 and 485–494.
- **Fix:** State the outcome in G6 and keep the signatures only in the table.

### STR-M5 (Major). "Every method takes reason and makes one commit" is false

- **Lines:** 483, 494 and 496.
- **Problem:** `prune` takes no `reason`, and `editions` makes no commit.
- **Fix:** Limit the sentence to the methods that change the Inventory.

### STR-M6 (Major). CUJ coverage gaps, and CUJ 2's precondition is missing

- **Lines:** 170–218.
- **Problem:**
  - No CUJ covers embargo with release, the batched seal, a failing check,
    prune, or `describe`.
  - CUJ 2's "Then" uses a WorkingCopy that its "Given" never sets up.
  - CUJ 1 omits `description`.
- **Fix:** Add CUJ 6 (embargo, then release) and CUJ 7 (prune: dry run, then
  real run), and fix CUJs 1 and 2.

### STR-M7 (Major). The Technical Plan drops to file-and-line detail

- **Lines:** 439–449, 551–564 and 566–573.
- **Fix:**
  - Move the sites column and the `store.py:NNNN` citations to the Detailed
    Implementation Plan.
  - Move "Issues filed on sdmxlib" to Rollout, or to the Detailed
    Implementation Plan.

### STR-M8 (Major). The Alternatives' IDs collide, and E–H break the format

- **Lines:** 583–652.
- **Problem:** "D" and "G" clash with the RFC's decision IDs and the goal
  IDs. E–H have no Pros, Cons or Reason fields.
- **Fix:** Renumber as ALT-n, and split E–H into separate entries.

### Structural nits

- **STR-N1.** Status should be "In Review".
- **STR-N2.** Name the blank first header cell of the Decisions table.
- **STR-N3.** Add a horizontal rule before the NFR and before Future
  Improvements.
- **STR-N4.** Bullet markers mix `-` and `*`.
- **STR-N5.** Decisions are cited in several styles. Use "RFC decision N"
  and "Pn".
- **STR-N6.** Non-Goals line 155 and line 371 disagree on the error the
  `search`/`facets` calls raise.
- **STR-N7.** The inequality names "backup retention" and "the `data/` GC
  window", which are not `Retention` fields.
- **STR-N8.** The Components diagram's box edges are misaligned.
- **STR-N9.** Overview line 42 paraphrases the legacy-layout rules. Point to
  the section instead.

## Style (`egm-style`)

### Major

- **STY-M1, lines 164–168.** The Open Questions introduction describes
  questions, but the list says "None". Write "None. The owner settled the
  slice order while drafting (P5)."
- **STY-M2, lines 72 and 229.** The gate's status is implied, not stated.
  Write "That issue, GH-81, already exists, so the spec's gate is met."
- **STY-M3, lines 54–55.** The sentence is a double negative and implies
  statcatlib writes an Inventory today. Write "Only statcatlib may write an
  Inventory, and it does not write one yet, so no catalog exists that pods
  can route."
- **STY-M4, lines 80 and 99–101.** "Right" and "narrow" are untestable.
  Rewrite G5 with an exact `previous` and journal-only repair, citing
  `tests/test_seal_crash.py`.
- **STY-M5, lines 235–236.** "Is corrected" hides that the correction
  happens later. Write "Slice S4 moves prune out of the RFC's Non-Goals."
- **STY-M6, lines 254–257.** The tense is wrong: the issue is not filed yet.
  The sentence also ends in a negative. Write "Issue 2 … asks for these
  fsyncs. Until sdmxlib ships them, … `statcat doctor` reports an empty
  journal entry or manifest."
- **STY-M7, lines 321–323.** The leaf handling hides inside a parenthesis.
  Write "It treats `FileExistsError` on a leaf as success, because a leaf's
  name is the hash of its bytes."
- **STY-M8, lines 352–354.** "A Catalog built by CatalogAdmin" is undefined.
  Say which error each entry point raises.
- **STY-M9, line 357.** "Exists on a created Catalog only" contradicts P4,
  where the method exists and raises. Rewrite it.
- **STY-M10, lines 423–429.** The coordinated verb is split, the link to
  prune is implied, and "neither … nor" has three items. Rewrite the
  paragraph.
- **STY-M11, lines 499–502.** "`data/` GC window" is undefined. Define it.
- **STY-M12, line 514.** "Leaves" reads as a verb. Make it two sentences.
- **STY-M13, lines 523–524.** "With the statcat check" can attach to either
  verb. Name `extensions={"statcat": check_catalog}`.
- **STY-M14, lines 592–593.** The sentence uses a colon reveal, ends on a
  punchline, and inverts its meaning. Write "Today `add_lineage` has no
  read-only guard, which shows how a per-method guard gets missed."
- **STY-M15, lines 629–640.** The rejection reasons for E–H are compressed
  until they are unreadable. Spell out each one.

### Nits

- **STY-N1, line 47.** "Cannot adopt" should be "pins 0.72 and needs code
  changes".
- **STY-N2, lines 60–63.** Say how today's steps break the RFC's first goal.
- **STY-N3, lines 65–66.** Name the five disagreements up front.
- **STY-N4, G6.** Fix the line breaks and name `set_embargo`.
- **STY-N5, lines 135–137.** "Since there is no migrator" frames the point
  by what is missing. State that users re-ingest until 1.0.
- **STY-N6, line 157.** Replace "narrow" with "journal-based".
- **STY-N7, line 199.** The withdrawn edition is never set up.
- **STY-N8, line 233.** Replace "each with one defensible answer" with
  "Settled by this design:".
- **STY-N9, lines 244–245.** A rule does not bump a version; drop "exactly".
- **STY-N10, lines 249–250.** The slogan quote needs a source.
- **STY-N11, lines 329–331.** "Never wrong" is a punchline.
- **STY-N12, lines 536–538.** Name the `sdmxlib edition` subcommands, and
  state that `rebind` has no replacement.
- **STY-N13, lines 542–543.** An interposed phrase delays the object of the
  sentence.
- **STY-N14, line 568.** "As issues and not as edits" is contrast by
  negation.
- **STY-N15.** Small wording fixes at lines 586, 614, 648 and 671.

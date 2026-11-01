# Stage 8: Intermediate Zoom Out report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md),
  the full file. The design is unchanged since Stage 5.
* **Inputs:** [zoom_out_report.md](zoom_out_report.md),
  [comprehension_report.md](comprehension_report.md) and
  [critic_report.md](critic_report.md).
* **Reviewer:** `egm-zoom-out`, a Goldfish with a fresh context.
* **Standard:** EGM Zoom Out Standard.
* **Date:** 2026-11-01.

**Verdict: changes required.** Stages 5 to 7 raised about 105 findings, and
most of them trace to six root causes. Patching each finding separately would
make the design larger than it needs to be. The reviewer recommends four
changes to scope:

- narrow prune;
- give the journal and the repair one rule;
- take the description out of S2;
- defer `correct` and `reinstate`.

Each change closes a cluster of findings at once.

## Elephant notes

- **Contested item 1 is confirmed.** Spec §4.1 says `search/` table names
  carry the `edition_id`. The rename stays, and Stage 5 N1 is rejected.
- **Contested item 3 is confirmed.** It matches the spec §4.6 table that was
  checked in Stage 7: `publish` checks every data-file digest.
- **Four items reopen owner decisions:** ZB1 (decision 32), ZM1 (P3), ZM2
  (decisions 28 and 32) and the S2 re-slicing in ZM3. They need the owner's
  answer before resolution.

## Blocker

### ZB1. Prune copies the server GC job onto a laptop

- **Lines:**
  - prune: 499–518, 307–309, 427–429;
  - S4 items 31–33.
- **Root cause:** Prune copies spec §11.4 and §11.6, which describe a server
  GC job: five retention windows, backup retention, root pruning, and
  WorkingCopies kept on build nodes. On a laptop, a WorkingCopy shares
  `data/` with the sealed editions. That shared directory is the cause of
  Stage 5 B1. The prune sweep of unrecorded editions brings in DES-M3's race.
  Tombstone expiry would bring in DES-M1's re-release hazard, because
  `repairable` checks only the newest root (`_rules.py:536-538`).
- **Closes:** Stage 5 B1 and M1 (the sweep), DES-M3, DES-M1 (expiry), Stage 6
  M12 and M13, STR-N7, Stage 5 N6, STY-M11, STY-M12, DES-N5, and STR-M6's
  CUJ 7.
- **Fix (owner decision, RFC decision 32):** choose one of two options.
  - **(a) Defer prune to Phase 4.**
  - **(b) Keep a narrow laptop prune.** It runs with one `older_than` window
    and a `reason`, and does the following:
    1. Retire superseded and withdrawn entries.
    2. Delete the directories of tombstoned editions older than the window.
    3. Delete a `data/` file only when no `manifest.json` on disk lists it,
       the product has no unsealed edition or journal entry, and the file's
       mtime is older than the window.

    It deletes no roots or leaves, expires no tombstones, and runs no sweep.

### ZB2. Each journal entry needs one rule

- **Lines:** 375–389, 398–429 and 99–101.
- **Evidence:** The journal entry is written before compaction and the
  manifest (`_write.py:564-601`). `read_journal` raises on an empty entry
  (`_layout.py:174-179`).
- **Closes:** DES-B1, DES-M1, DES-M2, Stage 6 M7, Stage 5 M1 (the late
  refusal), Stage 6 M6, and Stage 6 N5, N6 and N7.
- **Fix:** Apply one rule at `working_copy()`, at resume, at seal, and in
  seal step 6. It assumes one writer per product.

| Journal entry | Action |
|---|---|
| The head names or tombstones the edition | Clear the entry |
| The edition has no manifest | Clear the entry; the edition is an ordinary unsealed WorkingCopy |
| It has a manifest and is not named | Record it |
| Recording raises `duplicate` with the same `manifest_sha256` | Treat it as success and clear the entry |
| Recording raises any other `TransitionError` | Clear the entry, log a WARNING, and leave the orphan for `doctor` |
| `InventoryContentionError` | Keep the entry |
| The entry cannot be read | Raise a named error that points at `statcat doctor` |

Add a test showing that `working_copy(resume=True).discard()` works after a
crash between compaction and the manifest. Do not add a product-level
discard.

## Major

### ZM1. P3's description gates S2 and breaks 0.80.0 readers (owner decision)

- **Evidence:**
  - The `EventKind` enum is closed (`_format.py:38`).
  - RFC decision 28 already froze the enum in 2a.
- **Closes:** Stage 5 M2; Stage 6 M10, M11, M3 (the description part), N1,
  N2 and N14; and the CUJ 1 part of STR-M6.
- **Fix:**
  - In S2, `create` writes only `catalog.json`, and the first seal writes
    version 1, as RFC D3 already states.
  - A catalog with no root becomes the normal empty state, which closes
    M11.
  - The description, `describe` and the sdmxlib patch move to S3 or later,
    stated as a reader change that GH-81 must pin.

### ZM2. Defer `correct` and `reinstate` (owner decisions 28 and 32)

- **Why:** Both are server-recovery tools from the RFC's operations review.
- **Format impact:** Deferring them changes no format. `restore` and
  `correct` are already in the 0.80 enum.
- **Duplication:** `reinstate`'s file verification duplicates
  `verify-catalog`.
- **Closes:** DES-M6 and Stage 6 N13.
- **If `correct` is kept:**
  - copy only `products` from `from_seq`;
  - take `retired`, `embargo` and `extensions` from the head;
  - add an `if_seq` precondition.
- **Keep** `release` and `cancel` (P1). Each is one sdmxlib rule.

### ZM3. S2 holds too much risk: split it, and split the store by delegation

- **Closes:** Stage 5 M3, M5 and M6; DES-M7; DES-N3; and Stage 6 N10.
- **Fix:**
  - **Delegation.** `WorkingCopy` wraps a private writable instance of the
    existing class, and `EditionView` is the read-only face.
  - **Embedding reuse** (RFC decision 21) and the **batched seal** move to a
    slice after S2. The batched seal reports outcomes per product, as
    `release_due` does.
  - **DES-M7** (the batch split after a crash) is documented. It needs no
    catalog-wide repair.

### ZM4. Describe the writer and the error model once

- **Closes:** Stage 5 M4 and N7; Stage 6 M1, M2, M4, M5 and M9; and STY-M8.
- **Fix:**
  - `try_commit(base, leaves, *, retired=, embargo=, extensions=,
    built_from=, kind, reason)` raises `ConflictError` and serves as the
    conformance `Backend.commit`.
  - `commit(change)` is the retry loop around it.
  - The restore and fault hooks live in the test adapter only.
  - The created `Catalog` holds the writer.
  - One error, `InventoryRuleError(rule, product, edition)`, wraps sdmxlib's
    refusals. `NonFastForwardError` stays separate because it carries a
    guided message.
  - Every check finding refuses the seal.

### ZM5. Close the real gaps

- **Embedding model (Stage 6 B1).** Add `embedding_model`,
  `document_prefix` and `query_prefix` parameters to `working_copy` and to
  `view`.
- **Seal order (Stage 6 M8).** Compute the statcat section and run the
  checks before step 4 closes the store, and give the checks access to the
  observations.
- **Manifest hash (DES-M4).** Hash `manifest.json` before attaching it, and
  file an sdmxlib issue asking `attach` to take `manifest_sha256=`.
- **`Edition.previous` (Stage 6 M3).** Read it from the manifest, or drop
  it.
- **Kind and lifecycle (Stage 6 M6).** Support data products only in 2a and
  drop the `kind=` parameter. Check `lifecycle` at `working_copy()`.

### ZM6. Cut these proposed mitigations

| Proposal | Why cut |
|---|---|
| `Product.discard_working_copy()` (DES-B1) | The ZB2 rule plus `resume` and `discard()` covers it |
| Refusing `embargo=True` under a synced folder (DES-M8) | Spec §6.6 already declares synced folders unsupported, and a statement in the docs meets RFC D5/D18 |
| Tombstone expiry (Stage 6 M13) | It creates DES-M1's hazard; on a laptop the cost is about 100 bytes per retired edition |
| A CLI flag per `Retention` field and a JSON-lines log (Stage 6 M12) | Moot under ZB1 |
| Output schemas for the tools (Stage 6 M14) | Exit codes are enough, and `doctor` is read by a person |
| A `renameat2`/`renamex_np` fallback (DES-N1) | Refuse with a named error when the filesystem has no `link` |
| A separate digest pass (DES-M5) | `at="publish"` already checks the digests |
| CUJs for `describe`, the batched seal and failing checks (STR-M6) | Tests cover them; add an embargo CUJ only if P1 stays |
| A catalog-wide atomic repair (DES-M7) | See contested item 4 |

### ZM7. Add a short Risks section (STR-B1, downgraded from Blocker)

Five to seven rows:

- local embargo is not access control;
- `event.actor` is an unauthenticated OS user and host;
- synced folders are unsupported;
- `CURRENT` can lag the newest root;
- a refused record leaves an orphan;
- power loss can lose data until sdmxlib adds its fsyncs;
- sdmxlib-rest cannot read the catalogs until GH-81 ships.

## Nits

- **Tool fixes:**
  - `verify-catalog` uses `check_edition(at="publish")`.
  - `doctor` reports journal entries that have no manifest, and orphan
    editions.
- **sdmxlib requests:**
  - File the shared `create_exclusive` with sdmxlib. S2 does not depend on
    it.
  - Drop the fsync in `lance._write_manifest` (Stage 5 N2).
- **Editorial fixes (STR and STY findings):** apply these last, because
  many of them target text that the changes above rewrite.

## Contested findings

1. **Stage 5 N1 (drop the search table rename):** rejected. Spec §4.1
   requires `edition_id` in the table names. The Elephant confirmed this.
2. **The product-level discard in DES-B1's fix:** not needed. ZB2 covers it.
3. **DES-M5's "add a digest pass and measure its cost":** not needed, since
   `at="publish"` already checks the digests.
4. **DES-M7, downgraded from Major to Nit:**
   - The structures-first order does not apply to a 2a that supports data
     products only.
   - An atomic release is a property of the release job.
   - On a laptop, a crashed batch is acceptable once it is documented.
5. **DES-M8's synced-folder refusal:** a statement in the docs suffices.
6. **STR-B1, downgraded from Blocker to Major:** a short Risks section fixes
   it.
7. **Stage 6 M13:** do not build tombstone expiry, because it reopens the
   RFC D14 hazard.
8. **Stage 6 M5 and M14:** one wrapper error and exit codes are enough.
9. **Stage 5 M1 (a `discard_unrecorded` issue now):** this is future work.
   Under ZB1 and ZB2, `doctor` reports the orphan.

## Resolution plan

1. **Get owner decisions** on:
   - ZM1 (P3);
   - ZM2 (decisions 28 and 32);
   - ZB1 (decision 32);
   - the re-slicing in ZM3.
2. **Write the journal rule (ZB2).**
   - Add the seal-time repair.
   - Delete the late-refusal paragraph, the prune sweep and the related
     Future Improvements item.
   - Add the crash point between compaction and the manifest to the tests.
3. **Rewrite prune or delete it (ZB1),** and add `reason` to it.
4. **Split the store by delegation and re-slice (ZM3):**
   - S2: the writer, the model, seal, and the refusals;
   - S2b: embedding reuse and the batched seal;
   - S3: `CatalogAdmin`;
   - S4: the tools.
5. **Close the gaps (ZM5).**
6. **Describe the writer and the errors once (ZM4).**
7. **Fix the tools:**
   - `verify-catalog` uses `at="publish"` and exits with 0, 1 or 2.
   - `doctor` reports orphan editions.
   - Refuse with a named error when the filesystem has no `link`.
8. **Make the editorial fixes:**
   - add the Risks section;
   - fix CUJs 1 and 2;
   - reduce G6 to its outcome;
   - renumber the alternatives as ALT-n;
   - apply the style findings;
   - leave out the items cut in ZM6.

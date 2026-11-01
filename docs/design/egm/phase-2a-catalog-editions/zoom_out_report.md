# Stage 5: Initial Zoom Out report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md),
  the full file (lines 1–1054)
* **Reviewer:** `egm-zoom-out` (Goldfish, fresh context)
* **Standard:** EGM Zoom Out Standard
* **Date:** 2026-11-01
* **Baseline read:**
  - `CLAUDE.md`
  - the RFC
  - sdmxlib spec §4, §6 and §18
  - `sdmxlib/inventory/`, plus `sdmxlib/storage/_layout.py`,
    `sdmxlib/inventory/_format.py` and `sdmxlib/tests/support/inventory.py`
  - `store.py`, `bundle.py` and `lance.py`

**Verdict:** changes required. 1 Blocker, 5 Major, 7 Nits.

The design states its objective clearly: adopt sdmxlib 0.80 and write an
Inventory that sdmxlib-rest can serve. It reuses sdmxlib's pure rules, the
readers, `check_edition`, `EditionRegistry.catalog_name` and the conformance
suite. The reviewer checked the cited line numbers in `store.py`, `schema.py`
and `bundle.py` and found them accurate.

## Elephant notes

- **B1 confirmed.** The design places a WorkingCopy in
  `products/<id>/<edition_id>/` on the same disk as the sealed editions. sdmxlib
  writes its data into the shared `data/` before any manifest exists.
- **M2 confirmed.** The decode-failure claim holds. `EventKind` is a closed
  `Literal` (`sdmxlib/inventory/_format.py:38`). The reader treats a root that
  fails to decode as invalid and skips it (`_reader.py:111`). So a reader on
  0.80.0 cannot read a root with `kind: create`. This contradicts P3's
  consequence, "costs nothing downstream", and the owner should revisit P3.

## Blocker

### B1. Prune deletes the data files of WorkingCopies and of editions waiting for repair

- **Lines:** prune steps 2–3 (505–514); S4 item 31; `tests/test_prune.py`
  (1019–1022).
- **Problem:** Step 3 deletes every file under `data/` that no manifest of a
  recorded or tombstoned edition lists. Two kinds of work fail that test:
  - **An unsealed WorkingCopy.** It shares `products/<id>/` and `data/` on a
    laptop.
  - **A journalled sealed edition waiting for repair.** Step 2 protects its
    directory, but no recorded manifest lists its data files, so step 3 deletes
    them. The next `working_copy()` then records an edition whose files are
    gone.

  Step 3 also has no age guard.
- **Evidence:**
  - Spec §11.4 is a server GC rule, written for a layout where WorkingCopies
    live on build nodes (spec §8 and §11.6).
  - `sdmxlib/storage/_layout.py:127-147`: `discard` leaves data files for the
    GC to collect.
- **Recommendation:**
  - Prune skips and reports a product that has an unsealed edition
    (`list_editions`) or a journal entry (`read_journal`).
  - Step 3 skips files newer than the tombstone window.
  - Add both cases to the tests.

## Major

### M1. Fix the late seal refusal in sdmxlib instead of working around it in three places

- **Lines:**
  - "A refusal after the manifest" (423–429);
  - the unrecorded sweep in prune step 2 (507–510);
  - Future Improvements (672–674);
  - items 15 and 31;
  - `test_seal_crash.py` and `test_prune.py`.
- **Problem:** The workaround clears the journal entry by hand, leaves an
  orphan sealed edition behind, and adds a broad age-based sweep to prune.
  - Spec §6.7 and RFC §6 reject a broad scan when recording editions.
  - Spec §11.6 treats an unrecorded edition as an alert with an owner and a
    discard-or-reinstate decision. The design deletes it by age instead.
  - The sweep also catches an edition whose journal entry was lost, which can
    happen because sdmxlib does not fsync.
- **Recommendation:**
  - File a third sdmxlib issue now for one of two options:
    - `discard(edition_dir, unrecorded=True)`;
    - `discard_unrecorded(product_dir, edition_id, *, retired, leaves)`, which
      checks the same conditions as `repairable`.
  - On a late refusal, statcatlib calls it.
  - Delete the unrecorded sweep from prune, so prune deletes only tombstoned
    editions (spec §11.4).

### M2. The description in the root (P3) holds up the first 2a release with no consumer for it

- **Lines:** P3 (230), G9 (118–121), `Catalog.create` (340–345), the root
  extension (475–477), the S2 "Waits on" column (689), items 12 and 19.
- **Problem:** Each step forces the next:
  - The description lives in the root.
  - So `create` writes version 1.
  - That version needs a new `create` event kind.
  - That kind needs an sdmxlib patch.
  - S2 cannot release until the patch ships.

  Three further problems follow:
  - **No consumer.** Nothing persists `CatalogDescription` today
    (`model/catalog.py:45-71`). 2a adds a JSON codec for `Agent`,
    `LicenceDocument`, `ConceptSchemeRef` and `InternationalString`, and its
    only consumer is `Catalog.description`.
  - **CUJ 1 contradicts the signature.** CUJ 1 (line 176) calls
    `Catalog.create("~/stats")`, but `description` is a required keyword
    (line 340).
  - **Older readers break (owner decision P3).** `EventKind` is a closed
    `Literal`, so a reader on 0.80.0 rejects `create` and `describe` roots.
    Spec §6.3 allows only optional fields in a minor change. If sdmxlib-rest
    GH-81 pins 0.80.0, version 1 of every statcatlib catalog is invalid to it.
- **Recommendation:**
  - S2 pins 0.80.0 and makes `description` optional. With no description,
    `create` writes `catalog.json` only, and the first seal writes version 1
    (RFC D3 as written).
  - The description codec, the version that `create` writes, and `describe`
    move to S3.
  - The sdmxlib change must either decode unknown kinds or be stated as a
    reader change that GH-81 has to pin.

### M3. Delegation would do the job of moving about 1,700 lines of write code

- **Lines:** Components (289–299), items 15–16 (827–853), Alternative A
  (583–593).
- **Problem:** The write methods span about 1,700 lines: `put_concept`
  (3633), `bulk_load`, `transaction` and `put` (3811–3872), `build_index`
  (4751–~5534), `add_lineage` (7283) and `add_language_alternates` (7329).
  - Moving them in the same slice as the format change adds risk.
  - `_over(..., writable)` keeps a write flag on the query class. That
    undercuts the "impossible by construction" argument against
    Alternative A.
  - `build_index` reads through query methods, so the read and write halves
    stay coupled after the split.
  - The alternatives section considers no middle option.
- **Recommendation:**
  - `WorkingCopy` owns a private, writable instance of today's class and
    exposes the write methods by delegation.
  - `EditionView` is the read-only face.
  - The physical split of the class becomes a later refactor with no API
    change.

### M4. Use one atomic-write primitive, and keep the writer independent of where it writes

- **Lines:** "The Inventory writer" (311–336), Alternative C (607–613),
  item 13, "Issues filed on sdmxlib" (566–573).
- **Problem:** Two parts:
  - **Duplicate primitives.** sdmxlib already has two complete-before-visible
    writers, `tests/support/inventory.py:41-55` and `storage/_layout.py:155-161`,
    and neither fsyncs. The design adds a third in statcatlib.
  - **A writer tied to local disk.** `InventoryWriter(root: Path)` binds the
    commit loop to the local filesystem. The conformance `Backend` expects a
    single attempt per commit (`commit(base, leaves)` raising `ConflictError`),
    while the design's `commit(change)` retries internally.
- **Recommendation:**
  - Ask sdmxlib for one public `create_exclusive(path, bytes)` that stages,
    fsyncs, links and fsyncs the directory. sdmxlib's journal and manifest
    writes use it, and statcatlib calls it. This replaces filed issue 2.
  - In `store/inventory.py`, keep all file I/O behind `fetch`,
    `create_exclusive` and `advance_current`.
  - Expose a single-attempt
    `try_commit(base, leaves, *, retired, embargo, kind, reason)` that maps
    onto the conformance `Backend`, and build `commit(change)` as the retry
    loop on top of it.
  - Do not build an obstore backend in 2a.

### M5. The batched seal does not say what happens when one product is refused

- **Lines:** `Catalog.seal` (359–361), G4 (95–98), the write-volume NFR
  (664–665).
- **Problem:** The design does not say whether one fast-forward refusal at
  record time fails the whole batch. If it does, the late-refusal path leaves
  N orphan editions. Spec §6.4 and §11.2 report release batches per product.
- **Recommendation:**
  - Run the checks and the fast-forward pre-check for every WorkingCopy
    before any sdmxlib seal.
  - At record time, drop the refused products, commit the rest, and return
    the refusals with their reasons, as `release_due` does.
  - Add a test for this case.

### M6. S2 bundles every risk into one release

- **Lines:** the Slices table (689), items 12–26.
- **Problem:** S2 carries:
  - the writer;
  - the public model;
  - the store split;
  - the refusals;
  - the search manifest v3;
  - embedding reuse (G10, from RFC D21, an owner decision);
  - 32 test files;
  - the docs.

  Embedding reuse is a performance change: re-encoding at every seal is
  correct, only slower.
- **Recommendation:** Move D21's embedding reuse into its own slice before S3.
  It still ships in 2a.

## Nits

- **N1. The search table rename and manifest v3 are unnecessary.** Lines
  243–245, 455–457 and item 18. Each edition has its own `search/`, and the
  generation suffix already separates the carried tables from the rebuilt ones
  (`lance.py:1287-1378`). Keep the suffix, drop the version bump, and amend
  spec §4.1.
- **N2. Fsync in `_write_manifest` is wasted** inside a WorkingCopy, which is
  resumed or discarded after an interruption and hashed at seal anyway. Drop
  it from item 18.
- **N3. `NoLiveEditionError` contradicts the design.** Lines 352–354 have the
  error name states when `CatalogAdmin` built the Catalog, but lines 481–483
  say `CatalogAdmin` is never reachable from `Catalog`. Use one message that
  names `CatalogAdmin.editions(product)`.
- **N4. Seal omits spec §11.1 step 5,** `check_edition(at="publish")`. Run it
  with the statcat check, or state why a laptop skips it.
- **N5. Rename `bundle.py` in S1,** since S1 deletes most of it and S2 adds to
  it.
- **N6. The prune CLI shows two retention flags, but `Retention` has five
  fields.** State the defaults: every field except `superseded_after` defaults
  to the tombstone window, so the inequality check holds.
- **N7. Keep the conformance restore hooks in the test adapter.** Bumping
  `restore_epoch` is a non-goal, so `restore_to` and `restore_with_new_epoch`
  stay out of `InventoryWriter`'s API.

## Owner decisions challenged

- **P3:** see M2. It holds up the first release on an sdmxlib patch, and that
  patch breaks readers on 0.80.0.
- **P1:** acceptable. `wc.view()` before seal acts as a preview, so the phrase
  "released without a preview" (line 147) overstates the gap.
- **P2, P4 and P5:** no findings beyond the consequences already recorded for
  them.

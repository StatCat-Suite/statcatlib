# Stage 6: Comprehension Test report

* **Target:** [phase-2a-catalog-editions.md](../../phase-2a-catalog-editions.md).
  FeatureRange is lines 1–678; DetailedRange is lines 679–1054.
* **Reviewer:** `egm-comprehension` (Goldfish, fresh context)
* **Standard:** EGM Re-implementation Standard
* **Date:** 2026-11-01

**Verdict:** a re-implementer would get stuck. The review found 1 Blocker,
14 Major and 14 Nit findings, with three main causes:

- The new constructors dropped parameters that today's code needs.
- The design describes the writer's interface in two different ways.
- Several shapes are named but never defined: `PruneReport`, `Retention`, the
  root description JSON, and the statcat `Finding`.

None of these findings repeats one from Stage 5.

## Elephant notes

Two findings were checked against the code:

- **B1 is confirmed.** Today's `Catalog.__init__` takes `embedding_model`,
  `document_prefix` and `query_prefix` (`store.py:2830-2840`). None of the new
  entry points takes them.
- **M3 is confirmed.** `LiveEdition` has no `previous` field
  (`sdmxlib/inventory/_reader.py:70-88`).

## Blocker

### B1. No API supplies the embedding model or its prefixes

- **Lines:** 122–124, 340–357, 408–410, 462–466, 821, 896.
- **Problem:**
  - Today the search paths raise "pass it to Catalog()" when no model is set
    (`store.py:2383-2384`).
  - No new entry point takes a model: `create`, `open`, `working_copy`,
    `view`, `temporary_catalog`, `_over`.
  - The design's search features depend on a model:
    - seal step 3 reuses vectors only when a model exists;
    - G10's `encode` count needs a model;
    - `text_sha256` hashes the model name and the prefix.
  - Semantic and hybrid search on an `EditionView` would always raise. That
    contradicts "every query method stays as it is" (line 294).
- **Fix:**
  - Give signatures, for example
    `working_copy(..., embedding_model=None, document_prefix="")` and
    `view(*, embedding_model=None, query_prefix="")`.
  - State that seal embeds only when a model is set.
  - Say what happens when the model differs from the one recorded in the
    carried `embeddings.json`.

## Major

### M1. The design gives two signatures for the writer's `commit`

- **Lines:** 319–326 and 807.
- **Problem:** The two signatures are `commit(change, event)` and
  `commit(change, *, kind, reason) -> Root`. Several parts are undefined:
  - the inputs and outputs of `change(head)`;
  - whether `leaves` holds only the changed products or all of them;
  - which keys `root_args` may carry;
  - where `now` and `producer` come from;
  - the return type of `head()`.
- **Fix:** Define one signature:
  `commit(change: Callable[[FullInventory], Change], *, kind, reason) -> Root`.
  - `Change` is an attrs class: `leaves` (changed products only) plus
    optional `retired`, `embargo`, `extensions` and `built_from`.
  - `now` is `datetime.now(UTC)`.
  - `producer` is `Producer("statcatlib", __version__)`.

### M2. The conformance adapter is unspecified

- **Lines:** 335–336 and 918.
- **Problem:**
  - `Backend.commit` must raise `ConflictError` on a stale base. The design's
    `commit` retries internally instead.
  - The `Backend` also needs four restore and fault hooks:
    `commit_interrupted`, `hint_behind`, `restore_to` and
    `restore_with_new_epoch`.
- **Fix:**
  - Specify a single-attempt `commit_once`, or state that the adapter calls a
    private method.
  - State that the four hooks are test-only file operations.

### M3. `LiveInventory` cannot supply everything the public Catalog needs

- **Lines:** 290, 355, 362–364.
- **Problem:**
  - `LiveEdition` has no `previous` field, yet `editions()` returns `Edition`
    values that include `previous`.
  - Only `FullInventory` exposes `root`, so the public Catalog has no source
    for `Catalog.description`.
- **Fix:**
  - Read `previous` from the edition's manifest, or add an sdmxlib accessor.
  - Read the description with `decode_root` of the seq the reader holds, or
    ask sdmxlib for a new accessor.

### M4. The design never says which reader and writer a created Catalog holds

- **Lines:** 289–291, 340–361, 377–381.
- **Problem:**
  - Line 291 says the Catalog "writes nothing itself". Yet `create`, `seal`
    and the repair all write.
  - The repair needs every leaf and every tombstone, which only
    `FullInventory` provides.
- **Fix:** State that a created Catalog holds an `InventoryWriter`, which
  reads `FullInventory` internally, alongside its `LiveInventory`.

### M5. Errors are specified only for the fast-forward rule

- **Lines:** 404–417 and 485–494.
- **Problem:** `TransitionError.rule` can be any of eight values:
  fast-forward, if-live, not-due, state, tombstone, duplicate, unknown and
  kind. The design maps only fast-forward to a statcatlib error.
- **Fix:** Add an error table listing, for each error:
  - the statcatlib exception;
  - its base class;
  - the rule it wraps;
  - the method that raises it.

### M6. A kind or lifecycle mismatch is found only after the manifest is written

- **Lines:** 356–357, 415 and 470–471.
- **Problem:**
  - `record()` raises `rule="kind"` at step 6, after the manifest is written.
    The journal-clearing rule does not cover that refusal.
  - `record()` silently relabels the product's lifecycle.
  - `extensions.statcat.kind` is always `"data"`, even for a structure
    product.
- **Fix:**
  - Check `kind` and `lifecycle` against the existing leaf in
    `working_copy()`.
  - State whether 2a supports structure products.

### M7. The repair has no defined failure path

- **Lines:** 377–381.
- **Problem:**
  - `record()` inside the repair can raise. The rule may be fast-forward
    (after a rollback), tombstone or duplicate.
  - `read_journal` raises `ManifestError` on an empty entry, which is exactly
    what the NFR says a power loss leaves.
- **Fix:**
  - A refused repair clears the journal entry, logs a WARNING and continues.
  - An unreadable entry raises a named error that points at `statcat doctor`.

### M8. The seal step order leaves the section and the check data without a source

- **Lines:** 398–417.
- **Problem:**
  - `catalog_section` opens the store, but step 4 has already closed it.
  - The checks run against `wc.view()`, but observations are reachable only
    through `WorkingCopy.data`.
  - `conf_status` needs observations.
- **Fix:**
  - Compute the section before the close.
  - State the observation API a check uses, for example `EditionView.data`
    or a `(view, data)` signature.

### M9. `Finding` and "error-level" are undefined

- **Lines:** 400–403 and 883–886.
- **Problem:** sdmxlib's `Finding` is `{check, subject, detail}` and has no
  severity, so the design's error-level findings have nothing to read.
- **Fix:**
  - Define statcatlib's own `Finding` with a `level` field, or state that
    every finding refuses the seal.
  - Give the attributes of `SealCheckError`.
  - State the scope and the `subject` of `conf_status`.

### M10. The `description` argument of `Catalog.create` contradicts its callers

- **Lines:** 175, 340, 817 and 896.
- **Problem:**
  - `create` requires `description`, but CUJ 1 omits it and
    `temporary_catalog` passes None.
  - Three behaviours are unstated:
    - whether `~` is expanded;
    - what happens to the derived lists;
    - what `exist_ok=True` does when the arguments differ from the stored
      values.
- **Fix:** Make it `description: CatalogDescription | None = None`. Drop or
  refuse the derived lists. With `exist_ok=True`, ignore differing arguments
  and log a WARNING.

### M11. A crash during `create` leaves a state that nothing handles

- **Lines:** 340–345.
- **Problem:** A crash can leave `catalog.json` with no root.
  - With `exist_ok=True`, `create` opens the catalog, and the first read
    raises `InventoryError`.
  - With `exist_ok=False`, the error type is unstated.
- **Fix:** Treat this state as a half-created catalog. Either `create`
  finishes it, or a named error says to delete the directory.

### M12. Prune's API contradicts the `CatalogAdmin` contract, and its configuration is undefined

- **Lines:** 483, 494, 499–518 and 1005–1015.
- **Problem:**
  - `prune` takes no `reason`, but its `retire` commit needs one.
  - `Retention` gives no types or defaults, and the CLI exposes only two of
    its five fields.
  - `PruneReport` has no fields.
  - The format of the `--log` file is unspecified.
  - The age clock is unnamed: `state_changed_at` or `created_at`.
- **Fix:**
  - Add `reason`.
  - Give `Retention` and `PruneReport` as attrs classes with types and
    defaults.
  - Add a CLI flag for every `Retention` field.
  - Make the log JSON lines.
  - Name the age clock.

### M13. Tombstones never expire, and "named" has no defined scope

- **Lines:** 505–514.
- **Problem:**
  - `next_root` carries `retired` forever (`_rules.py:503-507`), so the root
    grows without bound.
  - Steps 2 and 3 do not say whether they test the newest root or every root
    kept. Step 4 deletes roots, so later runs get a different answer.
- **Fix:**
  - Add a step that drops tombstones past the window from `retired`, after
    their directories are deleted.
  - Define "named" against the roots that step 4 keeps.
  - Fix the order of the steps.

### M14. The tools' output contracts are underspecified

- **Lines:** 522–534, 656–659 and 1012–1013.
- **Problem:**
  - `verify-catalog` does not map `InventoryError` to an exit code, and its
    output format is unstated.
  - `doctor` gives no exit status, no output format and no rule for
    detecting a synced folder.
  - The NFR says `doctor` reports empty journal entries or manifests, but
    `doctor`'s report list does not include them.
- **Fix:**
  - Give each tool an output schema and its exit codes.
  - Write down the synced-folder detection rule.
  - Add the empty-file check to `doctor`'s list.

## Nits

- **N1. Mismatched format fields.** The manifest section uses `format: 3`
  (an int). The root extension uses `format_version: "3"` (a string). Pick
  one.
- **N2. The description JSON shape is undefined.** Lines 475 and 876–877
  leave the root's description shape open: `InternationalString`, `Agent`,
  dates and the taxonomy. Give a schema.
- **N3. Two names for one format, and undefined hash bytes.**
  - "Hyphens removed" (line 455) and "edition_id hex" (line 865) describe
    the same thing; use one name.
  - Give the exact bytes `text_sha256` hashes.
- **N4. `wc.seal()` has no stated return type.** Make it `Edition`
  (RFC §9).
- **N5. sdmxlib's second-WorkingCopy refusal is a plain `ManifestError`.**
  Call `list_editions` first and raise `WorkingCopyExistsError` from its
  result. Name `read_journal` and `clear_journal`.
- **N6. Resume is underspecified.** Say why resume repeats the repair, and
  give the error for `resume=True` when no unsealed edition exists.
- **N7. Repair on seal is missing.** The RFC and spec §6.7 run the repair
  "on seal and resume", but the design runs it only on create and resume.
  Say whether that is deliberate.
- **N8. The fast-forward pre-check under embargo is unstated.**
  `record(embargo=True)` does not check fast-forward. Say whether step 2
  still refuses.
- **N9. `EditionView` attach and release are unstated.** Say how many times
  a view attaches, when `release` runs, and how the `EditionRegistry` is
  obtained.
- **N10. `_over(writable=)` is unexplained.** State what the flag enables.
- **N11. Legacy-layout edge cases.**
  - `.DS_Store` alone would count as "files" and trigger a refusal.
  - The error for a missing path is unstated.
- **N12. Smaller inconsistencies:**
  - the `reason` position in G6;
  - `editions()` makes no commit;
  - `Catalog.products()` has no return type;
  - P5 is cited before the Decisions table defines it;
  - the list of guided `__getattr__` names is missing.
- **N13. `correct(from_seq)` gives no error** for a root that has been
  pruned or is invalid.
- **N14. `Catalog.description` derived lists.** Say whether the lists are
  filled from the live editions or left empty.

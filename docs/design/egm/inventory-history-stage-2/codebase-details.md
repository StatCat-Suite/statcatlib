# Codebase-details review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491`
**Reviewer lens:** `egm-codebase-details`

Verdict: 0 blockers, 3 majors, 10 minors. No statement about current code is false. The three majors concern proposed rules that the sdmxlib 0.88 formats do not support as written.

**Read:** the design in full; statcatlib `store/inventory.py` (whole), `admin.py:90-730`, `gc.py:280-410`, `diagnostics.py:300-628`, `store/blob.py:150-240, 420-634`, `release.py:735-775`, `jobs.py:120-260, 645-673`, `cli/__init__.py:500-650, 800-860, 1015-1065`, `working_copy.py:1600-1640`, the test adapters, `docs/catalog-format.md` §1, release-job D13; sdmxlib 0.88 `inventory/__init__.py` (whole), `_reader.py` (whole), `_format.py` (most), `_rules.py:470-775`, `conformance.py:1-220, 455-744`, `storage/_files.py:89-135`, spec §6.1, §6.6, §6.9, §11.6.

**Not checked:** E7's table of older statcatlib/sdmxlib versions (0.13, 0.14-0.17, sdmxlib 0.84/0.85, sdmxlib-rest 0.10.x), the obstore 0.12.1 `get_range` claim (R1), R10's sdmxlib-rest claim, all byte and time figures, spec §9.2/§10.1/§10.3/§11.4 text, and the suite design beyond a grep (revision 5, the Restores section, H4/H5 and "Verify before citing" exist).

## Major

### M1. Step 2's predecessor lookup (E9 step 2, S32) picks the wrong epoch's line after a restore below `expired_through`

- **Design:** lines 649-655 and S32 (line 190): the hash of c - 1 is "the `root_sha256` of its line in the newest `expiry` segment reached from `HISTORY.newest_segment`, stepping over `restore` segments, and the hash of retained root c - 1 when that segment holds no such line".
- **Evidence:** lines are per epoch (`decode_segment` enforces `line.restore_epoch == header.restore_epoch`, sdmxlib `_format.py:748-750`), and the same `seq` recurs across epochs.
- **Failure case** (the one S33 and the Part 5 test "A restore to a checkpoint below `expired_through`" build):
  1. Epoch e expired S+1..E into `expiry` segments.
  2. A restore goes to checkpoint S; `expired_through` becomes S.
  3. Epoch e+1 writes roots S+1 (a checkpoint, first of its epoch), S+2, and so on.
  4. The next expiry's lowest candidate is c = S+2. The rule steps over the restore segment, reaches the old-epoch expiry segment, and finds a line with seq S+1 of epoch e.
  5. That line's `root_sha256` differs from the hash of the retained epoch e+1 root S+1, so every later run ends in `expiry.chain_mismatch`.
- **Fix:** key the lookup by `(restore_epoch, seq)`, as check 1 of `verify-catalog` does, and prefer the retained root c - 1 when it exists. Use a line only when root c - 1 is absent and the line's epoch is the candidate's predecessor epoch. Reword S32 the same way and add the case to Part 5's "after a restore, expiry..." test.

### M2. The restore resume rule "S = `first - 1` of the newest `restore` segment" (E14 crash table line 884, S33 line 191) is wrong in two cases the design itself creates

- **Evidence:** `SegmentHeader.first` is the lowest `seq` in that header's `roots` (`_format.py:460`, `_check_header` `_format.py:772`). E14 step 1 (lines 857-863) writes one segment per `restore_epoch` group and splits at 1,000 lines, and step 2 sets `newest_segment` to the last one.
- **Case (a), several segments:** the newest segment's `first` is the start of the last group. Example: epoch 0 roots 31..50 and epoch 1 roots 51..80 above S=30 give `first - 1` = 50.
- **Case (b), S below the old `expired_through`:** the roots above S are sparse checkpoints, so even the lowest segment's `first` is the next checkpoint. Example: S=100 with the next retained root at 230.
- **Fix:** derive S by walking the contiguous run of `restore` segments of the old epoch(s) back from `newest_segment`, combined with `HISTORY.expired_through` when the restore lowered it. Alternatively verify the rerun's `to` by re-listing: no root above `to` other than a new-epoch S+1, and the restore segments' `roots` equal the planned set. The "no root of the new epoch at `first`" test has the same flaw. Update S33's wording.

### M3. A restore with nothing to discard cannot write the segment the rest of E14 depends on

- **Design:** E14 steps 1-2 and the resume rule assume at least one `restore` segment. "Before step 1" (line 851) does not refuse S = head. E14's intro names failover as a use, where the epoch is bumped and possibly no root is lost.
- **Evidence:** `SegmentHeader.roots` has `min_length=1` (`_format.py:485`) and `_check_header` indexes `roots[0]` (`_format.py:768-772`). An empty restore segment cannot be encoded or decoded. Without a segment, step 2's `newest_segment` and the unfinished-restore resume have nothing to read.
- **Fix:** state the behaviour. Either refuse `to == head.seq` with `ExpiryRefusedError`, or define an epoch-only restore that writes no segment and resumes from `HISTORY.restore_epoch` against the head epoch. Add a row to the Errors table and a Part 5 test.

## Minor

### m1. GC's unfinished-restore stop is placed where half the condition cannot be evaluated (E16 line 948, Part 5 `gc.py:303-317`)

- `expired_through()` (`gc.py:303-319`) reads only `catalog.json` and `HISTORY`, and runs before the head is read (`inventory()`, `gc.py:354-357`). E4's second condition, "the base's `restore_epoch` differs from `HISTORY`'s" (the gap between restore steps 3 and 5), needs the head root.
- **Fix:** put the `HISTORY`-versus-`catalog.json` comparison in `expired_through()` and the head-epoch comparison in `inventory()` after `full.refresh()` (`head.restore_epoch`, `gc.py:365`). Or return the decoded `History` from one helper.
- `gc.py` works only over `BlobInventoryStorage` (`gc.py:75`), and blob restore is R9 in Part 6. Part 5's GC test can therefore only use a hand-built state.

### m2. Event output for `statcat prune` conflicts with the existing job-command convention (E12 lines 760-763, E13 line 815)

- The design says `statcat prune` "prints each [event] as one line" and also prints report lines after the retire and delete lines.
- Existing code: `_EventLines` and `_events(name)` (`cli/__init__.py:1030-1055`) print event JSON on stdout. The convention at `cli/__init__.py:1018-1019` is "human lines of a job command on standard error; standard output holds its events only". `prune` today prints human lines on stdout with `_say` (`cli/__init__.py:617-641`) and attaches no event handler.
- **Fix:** state which stream carries what for `prune`, and name `_events("statcatlib.expiry")` as the helper to reuse.

### m3. Reuse: helpers the design re-describes without naming

- E8 local `replace` ("stat, compare, stage, `os.replace`, fsync the directory"): `_stage` and `_fsync_dir` exist at `store/inventory.py:289-311` and are what `advance_current` uses (`:405-407`). Name them.
- sdmxlib has `replace_durably` (`storage/_files.py:130`), but `sdmxlib.storage` does not export it (only `create_exclusive`, `storage/__init__.py:24-30`). The design should not rely on it.
- E9/E13 `keep_roots < 2` and `inventory_window >= window + 1d`: the same two rules live in `RetentionPolicy.check` (`jobs.py:217-249`). Say whether `prune` duplicates them, since they raise `JobsConfigError` there and `ValueError` here.
- E12 `_emit`: it exists at `jobs.py:665` and is imported privately by `gc.py:74`, `release.py:95` and `cli/__init__.py:60`. `store/expiry.py` importing `statcatlib.jobs` would be the first `store/` module to import a top-level job module. `jobs.py` imports nothing from `statcatlib.store`, so there is no cycle. State the dependency or move `_emit`.

### m4. `TypeError` from `opt_in`/`expire`/`restore` over a blob storage is not caught by the admin CLI (E6 step 1, Errors table lines 1037 and 1040)

- `statcat admin opt-in URL` and `statcat admin restore URL` accept `az://`. `_ADMIN_ERRORS` (`cli/__init__.py:839-849`) has no `TypeError`, so the user gets a traceback where `ERROR <message>` and exit 1 are expected.
- **Fix:** catch it in the two commands, or raise an `InventoryWriteRefusedError` subclass.

### m5. Dry-run locking is inconsistent between operations (E6 lines 489-503, E14 lines 848-855, E12 line 779 / S43)

- The expiry dry run "takes no lock". Opt-in runs "in this order, under `exclusive()`" with "4. A dry run returns here". The restore dry run is also described under `exclusive()`.
- The first `exclusive()` creates `<catalog>/LOCK` (E1 line 328), so a dry-run opt-in on a catalog not yet committed to since Part 1 writes a file. CUJ 1 says it "writes nothing".
- **Fix:** state per operation whether the dry run locks. If it does, either say `LOCK` creation does not count as a write here or open without `O_CREAT` in dry runs.

### m6. Stated invariants the design breaks but does not list for modification

- `store/inventory.py:4-6` (module docstring): "this module is the only code that decides what goes in `catalog.json`, `inventory/` and `CURRENT`".
- `docs/catalog-format.md:48-52`: "statcatlib's Inventory writer is the only code that writes `catalog.json`, `inventory/` and `CURRENT`".
- `store/expiry.py` will decide `HISTORY` content, root and leaf deletions, and the restore's `catalog.json` bump. Part 1's Modify list names only `:28-30`, `:123-132` and `catalog-format.md:68-69`.
- **Fix:** add these two sites, in Part 2 or 3.

### m7. `_leftovers` and the orphan listing versus staged files under `history/` (E15 line 937, E10 lines 680-683)

- `_leftovers` scans the catalog root and `inventory/` only (`diagnostics.py:569-571`). Segments are created with `create_exclusive`, which stages a file beside the target (sdmxlib `storage/_files.py:112`), that is under `history/<epoch>/`. A crash mid-create leaves a staged file that `doctor` will not report, and that E10's `list("history/")` will return.
- **Fix:** say the orphan scan filters names by the segment pattern (`_SEGMENT_PATH`, `_format.py:50`, which is private), and extend `_leftovers` to `history/`. "`_leftovers` is unchanged" is true for `LOCK` but incomplete.

### m8. E8's inherited-member list omits `write_leaf`

- Line 570 lists `location`, `read`, `create`, `advance_current`, `now`, `actor`, `exclusive`, `check`, `stage_2`. The protocol also has `write_leaf` (`store/inventory.py:339`).
- `InventoryStorage` is `@runtime_checkable` (`:322`), which E2's code block omits. `expire`'s "must be an `ExpiryStorage`... else `TypeError`" needs `ExpiryStorage` to be `@runtime_checkable` too. Say so.

### m9. `doctor`'s `cleaned_through` warning is permanently true after a restore until an expiry with candidates runs (E15 line 932 versus E11 lines 710-716 and S35)

- After a restore, `newest_segment` is a `restore` segment and `cleaned_through` is unchanged. E11 says a run with no candidate skips steps 3-4, and "a run with no pending segment ends here", so step 7 never advances `cleaned_through` over the restore segment. `doctor` then warns "naming the next `prune` as the repair", which will not repair it.
- **Fix:** let step 7 advance over trailing `restore` segments even with no pending expiry segment, or have `doctor` step over `restore` segments when comparing.

### m10. Shifted or imprecise references (all found by symbol; none wrong in substance)

- sdmxlib `conformance.py:717-724`: `HISTORY_CHECKS` is `:715-723` (comment at 715-716, tuple at 717-723).
- `jobs.py:170` for `RetentionPolicy.check`: line 170 is the class; `check` is `:217`, and the inequality is `:235`.
- `gc.py:303-317` for `expired_through()`: the function runs to `:319`.
- `gc.py:303-332` for "stops the run on a missing root": `older_root` is `:321-335`.
- `tests/test_inventory_conformance.py:184-196`: `restore_with_new_epoch` ends at `:197`, the `try_commit` line that Part 5 replaces.
- `admin.py:568`: the quoted sentence begins on `:567` ("Prune") and continues on `:568`.

## Verified correct

**statcatlib `file:line` references, all exact:**

- `store/inventory.py`: `:28-30`, `:123-132`, `:322-353`, `:385`, `:405-406`, `:409-411`, `:548-574`, `:570`, `:623-697` with each inner step (`:665`, `:666`, `:667`, `:668`, `:670-673`, `:675-695`, `:692`, `:696`), `:713-771`, `:747`, `:764-768`, `:773-803`.
- `admin.py`: `:109-139`, `:361-411`, `:550`, `:550-653`, `:612-653`, `:693`.
- `diagnostics.py`: `:322-374`, `:354-369`, `:425-431`, `:567-574`, `:577-627`.
- `gc.py`: `:352-377`, `:379-396`.
- `release.py:754-760`; `jobs.py:665`; `store/blob.py:179-187`, `:432-628`, `:562-586`; `MAX_CLOCK_SKEW` = 30 s (`blob.py:100`); `store/working_copy.py:1622`.
- `cli/__init__.py`: `:514-550`, `:579-641`, `:826-836`.
- Tests and docs: `tests/test_inventory_conformance.py:80`, `tests/stores/_backend.py:64`, `tests/stores/test_release_job.py:481-497`, `tests/test_inventory_history.py:300` and `:319`, `docs/catalog-format.md:68-69`, `docs/api/store.md:227` and `:411`; sdmxlib plan line 431.
- Every existing file the plan marks Modify exists, including `tests/_fakes.py`, `tests/test_gc.py`, `tools/count_any.py` and both `EGM.md` files. `catalog-format.md` §1.3 and §6 exist. `pyproject.toml:32` is `sdmxlib>=0.88,<0.89`.

**sdmxlib 0.88 exports and behaviour:**

- Public in `sdmxlib.inventory.__all__`: `History`, `SegmentHeader`, `SegmentLine`, `CatalogHistory`, `encode`, `encode_segment`, `segment_line`, `segment_path`, `decode_history`, `decode_segment`, `decode_catalog(decoder=...)`, `HISTORY_NAME`, `HISTORY_DIR`, `OPTED_IN_CATALOG_FORMAT`, `verify_entry`, `change_list`, `retire`, `release_due`, `next_root`, `next_created_at`, `local_fetch`, `Fetch`.
- `Root.has_history` and `CatalogDocument.opted_in` are properties. There are nine schemas, in both the `schema()` literal and `_SCHEMAS`.
- `decode_catalog` has the `"stage-2-writer"` decoder (`_format.py:542, 631-664`).
- `segment_path(restore_epoch, first, last, suffix)` matches the design's call, and `str(uuid.uuid7())` fits the 36-character path pattern.
- `encode()` accepts `History`. `History`'s fields and null rules match E6 step 5 and E11. `CatalogHistory.since_seq >= 1` is compatible with S41. `SegmentHeader` requires `leaves: []` for `restore` and `unread: []` for `expiry`, as E10 and E14 write them.
- `next_root` takes `restore_epoch`, `retired` and `embargo`, and `"restore"` is an `EventKind`, so E14 step 5 and S34 are writable through today's `try_commit` body.
- No stage-2 rule, check or conformance case exists in 0.88. `is_checkpoint`, `expiry_candidates`, `SEGMENT_MAX_*`, `restore_retired`, `check_history`, `live_at`, `ExpiryBackend`, `EXPIRY_CHECKS` and `run_expiry` are all absent, and the design correctly presents them as N1-N4.
- `HistoryBackend` has `stage_2`, `fetch`, `put` and `commit_at` (`conformance.py:476-510`). `check_a_writer_without_stage_2_support...` returns early when `stage_2` is true (`:685-686`), so Part 1's "stage-1 checks still pass with `stage_2 = True`" holds.
- Reader fallback (`_reader.py:176-264`): the epoch comes from `HISTORY`, the bound is max(held, `expired_through`), and it fails closed with no root of the new epoch. This matches CUJ 3 and E14's crash table.

**The design's "Not confirmed" item on `leaf_sha256 {"from": null, "to": null}`:**

- `change_list` does build it (`_rules.py:634-666`).
- `_check_history_fields` and `_check_item` have no rule against it (`_format.py:839-880`; the from/to-both-null rule is on `EntryItem` only).
- `_DEFINITION_RULES` has no `LeafHashes` entry (`_format.py:944-984`).
- Decode, `validate_root` and the schema therefore accept it; only a test is missing.
- statcatlib's `_base_leaves` intersects with `base.products` (`inventory.py:603`), so no leaf read is attempted for the unrecorded product.

**Lock, `prune` and stale-base placement:**

- `prune` cannot nest the lock. `_prune_retire` goes through `commit` and then `try_commit` (`admin.py:693`), the directory and data deletion (`:627-642`) touches no Inventory object, and `expire` would be called after the return path.
- The `change` callable and `_refuse` run before `try_commit` (`inventory.py:747-751`), so they stay outside the lock as E3 says.
- Engine step 0's `writer.advance_current()` does not call `try_commit` (`:773-803`).
- No other root-writing path bypasses `try_commit`: `inventory.py:692` is the only `create(root_path(...))`.
- The stale-base check is placeable at E3 step 3. `check_writable` returns the `CatalogDocument`, so `.opted_in` is available, and the check comes before `_stored_root` (`:668`), which already raises `InventoryConflictError` for a deleted base.
- `StaleBaseError` as an `InventoryConflictError` is retried by `commit` (`:764-768`) and mapped to `ConflictError` by the conformance adapter (`test_inventory_conformance.py:143-144`).
- E7's `prune` claim holds: `commit` returns before `try_commit` when the change is `None` (`inventory.py:749-750`), so a stage-1 `prune` with nothing to retire still deletes directories on a 2.x catalog.
- `prune`'s CLI already catches `InventoryWriteRefusedError` (`cli:612`), which covers the new subclasses.
- Under `src/statcatlib` only `gc.py` reads `HISTORY` (`gc.py:63-67, 313-317`); `release.py:755` reads only `opted_in`.
- E16's GC and expiry argument holds against `gc.py:368-372` given the same K on both sides.

**Spec conflicts with 0.88 code:** none of S31-S43 contradicts shipped code. The stage-2 rules have no implementation in 0.88, and the formats accept everything S33-S35, S40-S41 and S43 write. The only format-level problems are M1-M3. `created_at` is the fifth `Root` field (`_format.py:295-299`), consistent with S31 making the ranged read optional. R1-R3 quote release-job D13 accurately (`release-job.md:1082, 1087, 1089-1092`).

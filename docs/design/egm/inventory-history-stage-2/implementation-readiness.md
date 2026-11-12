# Implementation-readiness review: Inventory history stage 2

**Target:** `docs/design/inventory-history-stage-2.md` at `7538491` (revision 1; line numbers below are the design's unless a file is named)
**Reviewer lens:** `egm-implementation-readiness`

Verdict: 3 blockers, 11 majors, 11 minors. The design is not ready to implement as written. Parts 1 and 2 are close. Parts 3 to 5 leave design choices to the implementer, and the land order has a circular dependency between Parts 4 and 5.

**Read in full:** the persona, the standard, the design (1,438 lines), statcatlib `CLAUDE.md`, `justfile`, root `EGM.md`, `cliff.toml`.

**Read in part:** `store/inventory.py` (300-815, error classes), `cli/__init__.py` (505-650, 690-912), `gc.py` (296-335), `jobs.py` (`_emit`, `keep_roots`), `tests/test_inventory_conformance.py`, `tests/test_inventory_history.py` (285-334), `release-job.md` ("GH-125's parts", land order), sdmxlib `_format.py` (segments, `History`), `conformance.py` (grep only), the spec (grep only), the suite design's statcatlib Handoff (945-987), hub `docs/architecture.md:153`.

**Not checked:** `admin.py`, `diagnostics.py`, `store/blob.py` bodies, sdmxlib `_rules.py`, the per-directory `EGM.md` files, GH-125's body (no `gh`). No command was run.

## Blockers

### B1. Parts 4 and 5 cannot land "in either order"; each needs the other

- **Where:** Land order, line 1229 ("Parts 4 and 5 may land in either order"); Part 4, lines 1350-1352 and 1362; Part 5, line 1386.
- **Evidence:**
  - Part 4's adapter implements "`restore` (from Part 5)" (line 1351). Its test is "`run_expiry` passes every stage-2 check" (line 1362). N3's cases include "a restore that writes a `restore` segment and fails closed until root S + 1 exists" and "`verify-catalog` keyed by `(restore_epoch, seq)` across a restore" (line 235). With Part 4 first, that test cannot pass.
  - Part 5's test "`verify-catalog` passes across a restore, keyed by `(restore_epoch, seq)`" (line 1386) needs Part 4's `check_history` wiring. With Part 5 first, that test cannot pass.
  - Part 4's `doctor` "restore" item (line 935) and the "unfinished restore is one finding" rule (lines 915-916) name a command only Part 5 ships.
- **Fix:** set the order to 3, 5, 4, and move line 1386's test to Part 4. Or keep 3, 4, 5 and move the conformance run and the restore-dependent `doctor`/`verify` items into Part 5. Then delete line 1229's sentence.

### B2. A restore rerun after a crash in step 4 has no stated way to compute root S + 1's tombstones or its report

- **Where:** E14 step 5 (lines 868-874), crash table (lines 881-886), `RestoreReport` (lines 888-892).
- **Evidence:** step 5 builds `retired` from "each edition that a discarded root it could read records in a leaf". Step 4 has by then deleted those roots. The rerun row says only "deletes the rest" and "writes root S + 1 if absent". A segment line drops `products` and `retired` (sdmxlib `_format.py:490-523`); it keeps `changes`, whose items carry `leaf_sha256`. The design does not say the rerun rebuilds the leaf set from the lines' `changes`, nor what it does for `unread` roots, which have no line. Goal 8 and the GC argument at lines 946-948 rest on these tombstones.
- **Also unspecified on the same path:**
  - how the rerun tells an old-epoch root at `seq` S + 1 that step 4 has yet to delete from the new-epoch root S + 1 that step 5 wrote; both sit at `inventory/<S+1>.json`;
  - how `transitions`, `tombstoned` and `unread` are rebuilt for the idempotent call "returns that restore's report" (lines 877-879);
  - which `reason` a resumed restore records.
- **Fix:** state the rerun's inputs. For example: tombstones and `transitions` come from the `restore` segments' lines (`changes[].leaf_sha256.to`, with leaves read from `inventory/leaves/`, which a restore never deletes). Step 4 skips a root at S + 1 whose `restore_epoch` equals `HISTORY`'s and whose `event.kind` is `restore`. State what is lost for `unread` roots. Add a test: crash mid-step-4, rerun, assert `retired` equals the uncrashed run's.

### B3. The "unfinished restore" test is defined three ways, has no symbol, and E4's version refuses a commit after a finished restore

- **Where:** E4 (lines 451-454); E14 crash table (line 884); E16 GC (lines 948-949, "detected as in E4"); E9 (lines 619-620); E15 (lines 915, 935); Symbols table (lines 1059-1076).
- **Evidence:**
  - E4: "`HISTORY.restore_epoch` differs from `catalog.json`'s, or the base's `restore_epoch` differs from `HISTORY`'s".
  - E14: "the newest segment is a `restore` segment of the epoch before `HISTORY`'s with no root of the new epoch at `first`".
  - GC, `expire`, `doctor` and `verify-catalog` have no "base". After restore step 3 the two epochs agree, so E4's first half cannot see a crash between steps 3 and 5.
  - E4's second half fires for a `commit` whose head read preceded a completed restore: the base is of the old epoch, `HISTORY` of the new. That seal gets `InventoryWriteRefusedError` naming `statcat admin restore` when nothing is unfinished, instead of the conflict that `commit` retries. The order of the stale-base check and this check is not given either.
  - This predicate is used in Part 1 (commit), Part 3 (`expire`), Part 4 (`doctor`, `verify`) and Part 5 (GC, restore).
- **Fix:** define one function, for example `InventoryWriter.unfinished_restore() -> int | None` returning S, land it in Part 1, and give its exact rule. State that a base of an older epoch on a finished restore raises `InventoryConflictError`. State the order of the two checks in `_commit_locked`.

## Major

### M1. The release table is stale: statcatlib 0.18.0 has shipped without Part 1, and no later release has a number

- **Where:** header line 8; Terms line 88; E6 lines 518-519; E7 line 542; release table lines 1233-1238.
- **Evidence:** the design says "The next release (0.18.0) ... Part 1 too if it has landed", "0.18 if it ships before Part 1" and "0.14 to the last release before Part 1". The hub's `docs/architecture.md:153` reads "sdmxlib 0.88.0, statcatlib 0.18.0 and sdmxlib-rest 0.10.3", and the hub's latest commit is "update the version table for statcatlib 0.18.0". The worktree's `pyproject.toml` still says 0.17.0, so the release is inferred from the hub alone.
- **Effect:** Part 2 must print "0.14 to <version> can read it and cannot write it" (E6) and write the same in `docs/upgrading.md`. The implementer has no number.
- **Fix:** re-read against the 0.18.0 tag. State "stage-1 writers are 0.14 to 0.18". Number the planned releases (for example 0.19.0 = Parts 1 and 2, 0.20.0 = Parts 3 to 5), or say the text takes the number at release time and name who edits it.

### M2. sdmxlib gating: 0.89.0 is hard-coded, N7 contradicts the paragraph under it, and S-row "waits" have no definition

- **Where:** N7 (line 239); lines 241-243; land order (lines 1218-1223); Part 3 `pyproject.toml` (line 1312).
- **Evidence:**
  - N7: "A release, 0.89.0, with N1 to N3." Line 242: "0.89.0 with N1 ships before Part 3 lands, and N2 and N3 before Part 4." If N2/N3 ship in 0.90, Part 4 needs a second cap bump. The table has Part 4 on 0.89 and Part 4's file list has no `pyproject.toml`.
  - Nothing reserves 0.89 for N1. Any other sdmxlib minor moves the number.
  - "Waits on ... S39" for Part 1, and S31/S32/S37/S42 for Part 3, do not say what state satisfies the wait: owner accepted, merged in sdmxlib's spec, or released. Line 241 says "sdmxlib needs no release before Part 1", while Part 1 waits on a spec row sdmxlib owns.
  - `CLAUDE.md` quotes the cap literally (`sdmxlib>=0.88,<0.89`, Tooling section); Part 3's list does not name that edit.
  - N1 to N3's signatures are "Not confirmed" (line 1186). `expire` calls `expiry_candidates`, so Part 3's code cannot be written from this design until sdmxlib's design fixes them.
  - E10 decodes a segment header alone, from `read_range` bytes. sdmxlib 0.88 exports only `decode_segment`, which needs the whole segment (it refuses bytes not ending in a newline and checks lines against `roots`, `_format.py:725-764`); `_check_header` is private. N1 asks for no header decoder.
- **Fix:** write "the sdmxlib minor that carries N1 (expected 0.89)". Say whether N2/N3 are in the same minor and add the cap bump to Part 4 if not. Define "waits on S-row" as "accepted by the owner and recorded here" or "merged in the spec". Add `decode_segment_header(raw) -> SegmentHeader` to N1. Add a step to revise E9/E10/E14/E15 against sdmxlib's final signatures before Part 3 starts.

### M3. Part 1 builds S33's writer rule, but S33 is listed as gating Part 5 only

- **Where:** S33 (line 191); E4 (lines 449-454); Symbols (line 1064: `restoring: bool = False`, Part 1); land order (line 1218: Part 1 waits on "Q2, Q9; S39").
- **Evidence:** the unfinished-restore refusal and the `restoring` exemption are S33's text ("a stage-2 writer refuses every other commit"). They land in Part 1; the Errors table lists them under `_commit_locked`.
- **Fix:** add S33 to Part 1's waits, or move the unfinished-restore check and `restoring` to Part 5. In the second case Part 1 to 4 writers do not fail closed during a hand-made restore; say so.

### M4. The stale-base conflict departs from the spec with no S-row and no open question

- **Where:** E4 (lines 446-450, option table 460-463); N3 (line 235, last sentence).
- **Evidence:** spec §6.6 rule 5 says the writer "refuses a base version at or below `HISTORY.expired_through`" (`sharded-bundle-catalog.md:589`, also `:750`). The design raises `StaleBaseError(InventoryConflictError)` and has `commit` retry. It asks sdmxlib's conformance case to "accept `ConflictError`". That is a contract change routed through a handoff note. S31 to S43 hold no row for it.
- **Fix:** add an S-row gating Part 1 ("a stale base is a conflict the writer may retry against a fresh head"), or an open question.

### M5. Windows support is undecided and is not an open question

- **Where:** E1 (lines 335-338); Not confirmed (lines 1166-1171, 1182-1184); Risks (line 1198: "The owner states whether Windows is supported"); Verification Plan (lines 1418-1420: "one manual run ... or a statement").
- **Evidence:** Part 1 must ship a `msvcrt.locking` branch whose semantics, errno values and byte-beyond-EOF behaviour are all "Not confirmed", with no gate. The either/or in the Verification Plan is left to whoever cuts the release.
- **Related gaps:** the design does not give the platform guard (`sys.platform == "win32"`) that pyrefly needs on macOS/Linux for `msvcrt`/`fcntl`, nor whether the untested branch carries `# pragma: no cover`.
- **Fix:** add Q10 (Windows: supported and manually verified / best effort and documented untested / refused with an error). Give the import-guard idiom.

### M6. `ExpiryStorage` leaves error and type contracts open

- **Where:** E8 (lines 556-584); E11 steps 5 to 7 (lines 718-736); E9 line 616.
- **Evidence:**
  - `delete` returns "`False` when the object is absent or its tag differs from `if_match`". Step 7 advances `cleaned_through` "when every delete ... succeeded or found nothing". Step 5's root delete must distinguish "gone" from "changed under me", and one `False` covers both. Step 6 says "the tag differs and the delete fails" without saying whether that counts as `expiry.delete_failed`.
  - "A delete the store refused" (line 769): no exception type is named for `delete`, `list`, `read_tagged`, `read_range` or `replace` failures. For local disk that is `OSError`; for blob, `BlobStoreError`. The engine needs to know which to catch into `ExpiryReport.failed` and which to propagate.
  - `expire` raises `TypeError` unless the storage "is an `ExpiryStorage` with `stage_2`". That needs `@runtime_checkable` on `ExpiryStorage`, which is not stated.
  - `store/expiry.py` imports `InventoryStorage` and `InventoryWriter` from `store/inventory.py`. `InventoryWriter.opt_in` in `store/inventory.py` (line 1282) needs `ExpiryStorage.replace` and a storage type check. That is an import cycle the design does not resolve.
  - `replace` is specified for "`HISTORY` or `catalog.json`" only. Whether another path raises is not said.
  - `read_range(path, length)` on a file shorter than `length`, and `list` ordering, are unspecified.
- **Fix:** give `delete` three outcomes (deleted / absent / precondition failed), name the storage error type, mark the protocol runtime-checkable, and place `InventoryWriter.opt_in`'s storage check (or move `opt_in` to `store/expiry.py`).

### M7. `expire` control flow is ambiguous after an early stop

- **Where:** E9 step 0 (lines 623-624), step 1 table (lines 630-637), step 2 (lines 654-655); E11 (lines 710-716); E12 `ExpiryReport` (lines 785-787).
- **Evidence:**
  - Step 0: a bad `CURRENT` "ends the run with nothing taken". Step 2: a mismatch "ends the run with nothing written". Neither says whether steps 5 to 7 still run for pending segments a crashed run left. E11 says "A run with no candidate skips steps 3 and 4", which implies they do.
  - `ExpiryReport.stopped` is "the reason, or `None`" with no value list. It is set for a chain mismatch and a lost lock. It is not said to be set for `expiry.stopped_at_root`, a step-0 failure or `expiry.unreadable_root`.
  - `ExpiryReport` field types are not given (`candidates`: seqs or roots; `segments`/`adopted`/`orphans`: paths; `expired_through`: before or after). The same holds for `OptIn` and `RestoreReport` (`transitions`' element type).
  - Event payloads: only `expiry.planned` lists its keys (line 767). The rest say "naming both versions" or nothing. Part 6 and Phase 8 alerting read these (line 215).
  - `expiry-cache.json`: lines 665-670 list contents, with no key names, no version field and no rule for a cache written by another catalog (`catalog_id`). Line 966 makes "the file format" this design's deliverable to Part 6.
  - A dry run on a catalog with pending segments or a missing `CURRENT`, and a dry run during an unfinished restore: refused or reported is not said.
- **Fix:** add a short state table (stop cause → steps still run → `stopped` value → event), typed attrs definitions for the three reports, a key list per event, and the cache schema.

### M8. CLI output, exit codes and error mapping are unspecified for every new command and flag

- **Where:** E6 (lines 485, 516-520); E13 (lines 798-816); E14 (line 837); E15 (lines 916-917); test rows 1301, 1343, 1388 ("the lines ... exit codes").
- **Evidence:**
  - `statcat admin opt-in URL` and `statcat admin restore URL` take a URL. Over `az://` the method raises `TypeError` (lines 492, 1040). `_admin_call` catches `_ADMIN_ERRORS` only (`cli/__init__.py:839-849`, no `TypeError`), so the user gets a traceback. The intended exit code (1 or 2) is not stated.
  - `statcat prune --expire-older-than`: the exit code when `ExpiryReport.stopped` is set, when `failed` is non-empty, or when the walk stopped at a bad root is not stated. Today prune exits 1 only on a failed deletion (`cli/__init__.py:638-640`).
  - "`statcat prune` prints each [event] as one line" (line 762). Prune's existing output is plain `_say` lines (`retire ...`, `delete ...`); whether events print as JSON or text, and on which stream, is not given. `--apply`'s summary sentence gets no wording for expiry.
  - `opt-in` prints "three statements" described in prose with no literal text. `restore`'s dry-run lines are not given. A blank `--reason` on restore: `ValueError` (exit 1) or `_reason()`'s usage exit 2, as the other admin commands do (`cli/__init__.py:890-894`)?
  - `--expire-older-than` / `--keep-roots` / `--state-dir` option types and `min=` are not given. `--state-dir` or `--keep-roots` without `--expire-older-than`: error or ignored?
- **Fix:** add one table per command: each line's format, the stream, and exit code per outcome (0 / 1 / 2), following the `verify-catalog` docstring's pattern.

### M9. No clock or crash injection seam is specified for `expire`, though tests and N3 need both

- **Where:** E9 signature (lines 606-613); N3 (line 235: `expire(*, now, keep_roots, window, stop_after=None)`, `restore(seq, *, stop_after=None)`); Part 3 fixtures (lines 1313-1317); Part 4 line 1351.
- **Evidence:** `expire` reads `storage.now()` and takes no `now`. `LocalInventoryStorage.__init__(self, root)` has no clock parameter and `now()` returns `datetime.now(UTC)` (`store/inventory.py:359-362, 409-411`); `BlobInventoryStorage` has `clock=`. The conformance adapter must implement `now=` and `stop_after=`. Part 3's race test needs "expiry paused after step 1" (line 1336). None of the three hooks is designed. The mapping from N3's `stop_after` step number to "the n-th write or delete" of the recording wrapper is also undefined.
- **Fix:** add `LocalInventoryStorage(root, *, clock=None)` to Part 1 or 2. Say the adapter wraps the storage in the recording `ExpiryStorage` from `tests/_fakes.py`. Define how a step boundary is detected (for example a private `_after_step` callback on `expire`/`restore`, or wrapper counters keyed by path prefix).

### M10. Crash-test coverage is one row per operation and does not list the crash points

- **Where:** Part 3 line 1331; Part 5 line 1381; E11 table (lines 738-747); E14 table (lines 881-886).
- **Expiry, against "a crash after each of steps 3 to 7":** line 1331 says "A crash after each step, in a run of several segments". Not stated as cases:
  - between two segment creates inside step 3;
  - inside step 4 (old or new `HISTORY`);
  - between two root deletes in step 5;
  - between two leaf deletes in step 6;
  - between step 6 and step 7;
  - a second crash during the recovery run.
- **Restore, against "between each pair of restore writes":** line 1381 says "A crash after each step". Not stated:
  - between two `restore` segments when the discarded roots span epochs;
  - between `HISTORY` and `catalog.json` (the only window where the two epochs differ, see B3);
  - between two root deletes in step 4 (see B2);
  - between root S + 1's create and `CURRENT`;
  - adoption of an orphan `restore` segment (S35) and the "operator abandons the restore" path (line 883) have no test at all.
- **Other rows with no test:**
  - the idempotent restore rerun (lines 877-879);
  - `doctor`'s "restore" item and `verify-catalog`'s unfinished-restore finding (lines 915, 935);
  - opt-in, `expire` and `restore` on a filesystem that refuses locks (E5, lines 476-477; Part 1 tests commits only);
  - `expiry.delete_failed` leaving `cleaned_through` unchanged (line 734);
  - opt-in's "`HISTORY` left by a crash ... refused otherwise" (lines 505-506), which also has no error class in the Errors table.
- **Fix:** parametrise both crash tests over every destructive call index n of the recording wrapper, with one stated invariant set per n: the reader opens; no retained root names a missing leaf; the rerun reaches the uncrashed end state. Add the missing rows.

### M11. Open questions: gates are too narrow, Q1 (b) has no plan, and Q7 gates sdmxlib's work

- **Where:** lines 178-179; Q1 row (line 168); land order (lines 1220-1222).
- **Evidence:**
  - "The owner's answers are recorded here before Part 1 starts" covers the undecided case for all nine. The next sentence then gives per-part gates, which reads as if later answers are acceptable. Pick one.
  - Q1 is said to gate Part 3. It also decides Part 4 (`check_history`, N2) and Part 5 (`restore_retired`, N1).
  - Under Q1 (b) the design names no module ("a module that imports only sdmxlib"), no file list, no tests and no signatures. The plan is written to (a) only.
  - Q7 (orphan = note) and S36/S38 shape N2's `HistoryFinding` kinds. Q6/S34, Q8/S40 and S32/S33/S35/S43 shape N3's cases and `restore_retired`. sdmxlib builds these before statcatlib's Part 3. So Q6, Q7, Q8 and those S-rows gate the sdmxlib release, earlier than "Part 4" and "Part 5".
  - Q2 also binds Parts 2, 3 and 5 (each refuses on such a filesystem). Q9's constant binds Part 6.
- **Fix:** state "all nine answered and S31 to S43 accepted or rejected before Part 1 starts; Parts 1 and 2 need only Q2, Q9, S39, S41 (plus S33 per M3)". Add a column "blocks sdmxlib N-row". Either plan Q1 (b) or mark it rejected-unless-replanned.

## Minor

### m1. Part 1 breaks a `live`-tier test that `just ci` does not run

- `tests/test_live_cuj.py:40` asserts `sorted(p.name for p in root.iterdir()) == ["CURRENT", "catalog.json", "inventory", "products"]` after a seal. Part 1 adds `LOCK`. Add the file to Part 1's Modify list.

### m2. Part 1's list of 2.x-refusal tests is incomplete

- Line 1258 names `tests/test_inventory_history.py:300, :319`. `test_a_refusal_at_the_record_keeps_the_journal_entry` (`:324`) is a third. `_opt_in` there writes no `HISTORY`, so the new outcome is the "no `HISTORY`" refusal with another message.
- "`check_writable` ... runs steps 2 and, best effort, 3" (lines 430-432) does not say whether the pre-check raises on a missing `HISTORY` or a stale base. That decides whether the seal at `:312-321` is still refused before the manifest is written.

### m3. Forward references in messages

- Part 1's refusal names `statcat admin restore` (lines 453, 1035), which arrives in Part 5.
- Part 2's `doctor` names "the next `prune`" as the repair (line 932), which arrives in Part 3.
- Say what the text is until then, or accept it and state so.

### m4. Public exports and docs are not listed per part

- Part 1 exports its error classes via `__init__.py` and `tests/test_public_api.py` (lines 1253-1254). Parts 2, 3 and 5 list no export decision for `OptIn`, `ExpiryReport`, `ExpiryRefusedError`, `RestoreReport`, `ExpiryStorage`, `expire`.
- `store/lock.py` and `store/expiry.py` are new modules. `CLAUDE.md`'s Architecture tree and `src/statcatlib/store/EGM.md` are covered only by the general sentence at lines 1226-1227. Root `EGM.md`'s invariant "`InventoryWriter` is the only code that writes `catalog.json`, `inventory/` and `CURRENT`" (`EGM.md:69-72`) changes once `store/expiry.py` writes `HISTORY`, deletes roots and replaces `catalog.json`. Name these edits in Parts 2, 3 and 5.

### m5. `feat:` for breaking parts

- Line 1228 says the first PR of each part uses `feat:`. The release table calls Parts 1-2 and 3-5 breaking. `cliff.toml` groups only a `!` commit under "Breaking Changes" (`cliff.toml:36`). Use `feat!:` for Part 1 (protocol members, new error on commit) and Part 3 (cap bump), or say why not.

### m6. Opt-in's dry run takes the lock and creates `LOCK`

- Line 490 has every step "under `exclusive()`", and step 4 returns for a dry run that "writes nothing" (line 262). `expire`'s dry run takes no lock (line 779). On a lock-refusing filesystem an opt-in dry run then fails where a report is expected.
- Step 3 reads every root under the lock (1.7 GB at 2,979 roots by the design's own figure), which can exceed other writers' 120 s wait. The NFR section mentions that risk for expiry only.

### m7. `doctor`'s `LOCK` probe

- Line 931: "a non-blocking attempt that is released at once". `hold` (line 1062) has no non-blocking mode and opens with `O_CREAT`. `doctor` "writes nothing" (`cli/__init__.py:557`). Specify a probe that opens without `O_CREAT` and never creates the file.

### m8. Lock details left open

- How a test shortens `LOCK_TIMEOUT` (`exclusive()` takes no argument; line 1268 says "with a short timeout").
- The mutex around the thread table.
- Where the "one WARNING per process and catalog" set lives.
- `try_commit`'s code for the E5 fallback (the E3 snippet at lines 404-406 shows only the locked path).
- `InventoryWriter.history()` on a 2.x catalog with an undecodable `HISTORY`.

### m9. Acceptance map

- It names test files, never test names.
- Row 1 cites `tests/stores/test_blob_conformance.py` for stage 2, but the blob adapter keeps `stage_2 = False` until Part 6 (line 1256), so no stage-2 case runs there.
- The suite design's Handoff lists nine items plus `just ci` (`inventory-history-log.md:968-987`). The map has four more (conformance, crash recovery, `artefacts`, `verify-catalog`), presumably from GH-125's body, which the reviewer could not read. Say so in the map.
- "without deadlock" (line 1399) has no bound in its test (line 1338 "and returns"). A regression would hang the gate; state a timeout.

### m10. Events use a private helper across layers

- E12 writes events with `statcatlib.jobs._emit` (line 761) from `store/expiry.py`. `store/` importing a private function of top-level `jobs.py` reverses the current direction. Name the move (for example to a shared module) or accept it explicitly.

### m11. `InventoryWriter.opt_in` has no signature

- Symbols line 1069 points to E6, which gives `CatalogAdmin.opt_in` only.
- `restore`'s function default is `dry_run=False` (line 1075) and the admin method's is `True` (line 836). Confirm that is intended.

## Checked and found in order

- Part 1 and Part 2 use no symbol from a later part. `ExpiryRefusedError` first appears in Part 3 and nothing earlier raises it. Both run on sdmxlib 0.88: `History`, `decode_history`, `decode_catalog(decoder="stage-2-writer")` and `EventKind "restore"` exist at v0.88.0.
- No test fake implements `InventoryStorage`; only `LocalInventoryStorage` and `BlobInventoryStorage` do. The protocol change in Part 1 breaks no test double.
- The dependency cap rule is followed: the bump is in the PR that first uses the new minor (`CLAUDE.md`, Tooling).
- `CHANGELOG.md` is generated by git-cliff at release time, so no per-part entry is required. `docs/upgrading.md` per part is stated.
- The design is in `mkdocs.yml:133`, so the strict docs build has its nav entry. The reviewer could not confirm a `docs/design/SITEMAP.md` row (the structural reviewer did confirm it).
- `keep_roots` default 10 and minimum 2 match `jobs.py:17, 103, 223, 247`.
- The gate (`just ci`) does not include `just coverage`. The design's "`just coverage` runs in each part's PR" is an added manual step, consistent with `CLAUDE.md`.

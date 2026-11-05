# EGM review report: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, branch
`docs/design-catalog-on-blob-storage` at `e7eba47`, whole document.

**Read against:** statcatlib `e7eba47` (code equal to `origin/main` `974ea27`); sdmxlib
`011312c3` (branch `docs/phase-4-restated`), spec and plan at that text; hub `cb144f6`, suite
design revision 4; GH-98 body snapshot.

**Reviewers:** design critic (`DES`), codebase details (`CB`), implementation readiness
(`IR`), comprehension (`CMP`), structural (`STR`), style (`STY`), zoom out (`ZO`). Their full
reports sit beside this file.

**Counts after deduplication:** 4 Blockers, 37 Major, 33 Minor, from 150 raw findings.

**Verdict.** The architecture stands: the storage seam, the commit path, `if_seq`, the leaf
rule and the history links all held up under review. The design is not ready to implement.
`correct`'s state rule can publish an embargoed or cancelled edition, the edition checks refuse
valid structure products, the sdmxlib work has no order, and the production `statcat upload` is
undefined. The design was not changed.

**Severity changes the compiler made.** Each differs from what the reviewers reported.

- B2 is raised to Blocker from Major. Three reviewers found it independently. DES-2 needs no
  Phase 5 code, and the rule lands in sdmxlib's released API (S10), so it must be right before
  Part 4 starts. The owner may rank it otherwise.
- CB-2 is lowered from Blocker to Minor (m1). It is a one-line slip that the first test catches.
- STY-1 and STY-2 are lowered from Major to Minor (m33). A heading's form causes no failure.

**Compiler's spot checks, by reading only.** `sdmxlib/edition/_check.py:293,305-313,755-759`;
`sdmxlib/inventory/_rules.py:135,302,313-320,340-344,359-362,422-427`; spec lines 427, 505, 510,
519, 522, 523; statcatlib `working_copy.py:322-337,1128-1131`, `bundle.py:229-237`,
`inventory.py:595-613`, `tests/test_seal_crash.py:86-91`, `tests/test_inventory_writer.py:118,165`.
Each supports its finding. No reviewer ran anything, and no reviewer had git, so commit hashes come
from the brief. Other findings rest on the reviewer's reading.

**`obstore` and bearer-token facts.** None of F1 to F22 is contradicted. `CB` read the 0.12.1 wheel
in `~/.cache/uv/archive-v0/CrCci5PwDHbOclfm/` and verified F15 in full and F11 and F14 at stub level.
F1, F2, F3, F5, F6, F8, F10 and F13 are partly verified. F4, F7, F9, F12, F16 to F22 are not
independently verified, F7's `HEAD` pattern having partial support in sdmxlib's
`tests/stores/_stores.py:228-232`. No Rust source is on disk, so the `src` citations are unchecked.
The table is in `codebase_details_report.md`.

Line references are `design:N` for the target. `rules` is sdmxlib `src/sdmxlib/inventory/_rules.py`.
`spec` is sdmxlib `docs/design/sharded-bundle-catalog.md`.

## Blockers

### B1. The edition checks refuse a valid structure product at upload, open and recovery

**Reviewers:** `DES-1`, `CB-1`, `IR-7`. **Target:** design and sdmxlib code.
**Sites:** design:789, 560, 712-713; `_check.py:293`, `:305-313` (`check_on_open` has no
`structures`), `:755-759`; statcat `testing.py:285,405-406`; `working_copy.py:1128-1131`; spec
§6.5 item 9.

**Failure.** A concept-scheme product whose references resolve in another structure product is
valid. With `structures=None` each unresolved reference is a finding. So `upload_edition` raises
`UploadCheckError` and never registers, `record_uploaded` fixtures over CODR's structure products
fail with it, the first `view()` raises `IncompatibleCatalogError`, and `correct` or `reinstate`
raises `EditionCheckError`. Upload may not read the Inventory, so it cannot compute `structures=`.

**Fix.** sdmxlib adds `structures=` to `check_on_open`, plus a way to defer unresolved references
at `receive`. Upload defers them for an edition with no bindings, and the fold owns the check. D7
passes what `diagnostics._structures_for` names, and on blob states which editions it pulls. Add a
test row at each site using a structure product with external references. Add this as a new S row.

### B2. `correct`'s rule reverses cancels and releases, and publishes early

**Reviewers:** `DES-2`, `DES-3`, `DES-4`, `CB-10`, `ZO-1`. **Target:** design D7, and sdmxlib's new
rule (S10). **Sites:** design:636-649, 747-749, Part 4 rows 971-988; `rules:135,302,313-320,340-344,
350-362,365-389`; spec:505.

D7 handles an entry the head tombstones, an entry embargoed in the head and absent from the restored
leaf, and any other entry absent from it. Everywhere else the restored copy wins, with no regard to
the head's state. Three scenarios follow, and the rules and the file checks pass in each.

1. **Early publish, no Phase 5 needed.** E is embargoed at 39 and released by mistake at 40.
   `correct(from_seq=39)` writes 41 with E embargoed. Then `correct(from_seq=40)`, aimed at another
   product, restores E as live before its time.
2. **Cancel undone.** E is embargoed at 39 and cancelled at 41 for confidentiality. `correct` from 39,
   aimed at another product, restores E as embargoed with its old `release_at`, and Phase 5's
   `release_due` publishes it. With `supersede_pending`, the withdrawn E_old and its replacement E_new
   are both due, E_old has the lower id and goes first, and E_new is refused. A rollback made for a
   leak also returns as live. Spec:505 says `withdrawn` is "never routed again automatically".
3. **Release undone, then redone.** E's release at 40 was the error. `correct` restores E as embargoed
   with a `release_at` already past, and the next release job republishes it. Design:748 tells the
   operator that it "needs releasing again".

**Fix.** Replace D7 step 4 with one table over head state and restored state, in the O4 section below,
and test each row. `correct` is catalog-wide, so also report what it changes on every product.

### B3. Contract changes have no owner, order or issue

**Reviewers:** `STR-1`, `IR-1`, `STR-4`, `STR-6`, `CMP-19`, `ZO-9`. **Target:** design and plan.
**Sites:** design:141, 143-155, 425-427, 671, 697-701, 709, 881, 889, 961; GH-98 line 26; hub
`CLAUDE.md:68`; sdmxlib plan:322-344.

**Failure.** Part 1 can merge with D3 recording a declared actor while spec §6.2 says "never from the
caller" (S2), with S3 unanswered. A Part 1 implementer cannot tell whether S6 blocks, since S6 says
"Blocks" and offers a fallback, and design:889 says "Needs S6". If S6 ships with S10 as design:671
suggests, Part 1 waits on O4. Only S6 and S10 appear in any Part. D7's "one spec change (S8)" disagrees
with S9 and S11, S11 "does not block" while design:961 puts a conformance check first, and no sdmxlib
issue exists. No row says whether the hub suite design changes.

**Fix.** Add "Contract changes and handoff" with one row per item: target, issue, carrying release and
the Part it gates. Make S2 and S3 gate Part 1, and S10 gate Part 4. Separate S6 from S10 and pick one S6
path. `ZO-9` shows a smaller order: move `CatalogAdmin.open(BlobLocation)`, `CatalogAdmin.create` and
`ManifestArtefacts(fetch)` to Part 4, so Parts 1 to 3 land on sdmxlib 0.85 and one release carries S6
and S10. It also sorts S1 to Phase 5, S4, S9 and S11 to issues, and S2, S3 and S7 to one prose PR.

### B4. `statcat upload` has no way to get a credential on its production journey

**Reviewers:** `IR-2`, `ZO-2`, `CB-7`, `STR-8`, `CMP-12`. **Target:** design. **Sites:** design:161-162,
376-377, 390-395, 534-536, 775, 923, 926-936; `obstore/auth/azure.py:11-12`.

**Failure.** CUJ 1 runs the command with a workload identity. The CLI "builds the location from the
environment", names no variable, and needs a `TokenIdentity`. The `blob` extra has no `azure-identity`,
and `obstore`'s provider imports it at module load. The command refuses or raises `ImportError`. Only
`upload_edition` is designed and tested. URL grammar, `--release-at` parsing and exit codes are undefined.
Design:536 also rejects credentials taken silently from the environment, which design:775 then does.

**Fix.** Name the variables, the URL grammar and the time format, and refuse a naive time. State the
credential source, for example `AzureCredentialProvider` in `TokenIdentity` with `azure-identity` in an
extra. Add `CliRunner` rows, the pattern in `cli/EGM.md:27`. Reconcile design:536 with design:775.

## Major

### Embargo and recovery

**M1. `reinstate` then `rollback` publishes a never-released edition, and the default target finds it.**
`DES-5`, `ZO-11`. Design, spec, sdmxlib code. A cancelled edition has `release_at` cleared (`rules:135`).
Prune or `correct` tombstones it, `reinstate` makes it `superseded`, and `rollback` makes it live, under
every O4 option. With `to=None` the rule takes `candidates[-1]`, the highest id (`rules:427`), where
spec:523 says most recent. An operator can publish a reinstated edition without naming it. **Fix:** rank
the default by `state_changed_at` or skip reinstated entries. State the risk in the operator guide. The
owner decides on an optional `Tombstone` field recording whether the edition was ever public.

**M2. Verifying on blob copies embargoed files out of the boundary, to a directory that may not exist.**
`DES-6`, `CB-6`, `IR-8`. Design. Design:712-713 pulls "as in D6", into a cache that design:613 says holds
nothing embargoed. A laptop `correct` leaves an embargoed `metadata.duckdb` there with no eviction, and
`cache_dir=None` is legal on `CatalogAdmin` (design:525, 538). On S3, F5 means no lookup reads a digest, so
the strength of "every listed file verifies" is undecided. Part 4 also depends on Part 2's `_lookup` and Part
3's `pull_edition` unstated. **Fix:** verify in a temporary directory removed in `finally`, test that nothing
remains, define S3 behaviour, move the lookup to `store/blob.py`, and state the Part dependency.

**M3. An upload rerun with a different release time is silently ignored.** `DES-7`. Design:793-794, 929. A
rerun correcting a too-early `--release-at` gets `registered=False` and exit 0, and Phase 5 folds the old time.
**Fix:** read and compare the entry on `AlreadyExistsError`. A difference raises `UploadCheckError` naming both.

**M4. S8 is stale, and spec line 519 contradicts O4's recommendation.** `CB-11`, `STR-2`, `STR-25`, `DES-20`.
Design and spec. Design:152 asks for text spec:427 and spec:519 already hold at `011312c3`. The row says "any
state", while option 1 keeps embargoed entries and D7 refuses on a live restored entry. **Fix:** restate S8
against this commit. Make the row follow O4. Align hub `inventory-history-log.md:196-197`.

**M5. GC can delete a pending edition's files.** `DES-9`. Spec §11.4 line 990, no Phase 4 block. An uploaded
edition is listed by no recorded manifest, so GC deletes its files, and the fold later records an edition whose
bytes are gone. **Fix:** §11.4 counts manifests named in `pending/`, with a minimum file age, before Phase 5
builds GC.

### Correctness of the code as designed

**M6. Cold release costs about 71 s, and D5 claims 1 s.** `DES-8`, `CB-4`, `IR-11`, `CMP-7`, `CMP-8`, `CMP-9`,
`ZO-4`. Design. `_planned` calls `full.leaves()` (`cross_product.py:214`), 7,000 sequential GETs
(`_reader.py:302-304`) that design:496-503 does not count. At 10 ms a GET, 133 s falls to 71 s, about 2 times
rather than 60. Part 5 measures no manifest read, though design:497 says it does, and its 64-thread leaf prefetch
has no production code. `read_many` has no consumer. **Two fixes, the owner picks:** build a caching `Fetch` that
bulk-fills leaves, and restate the totals (`DES-8`); or drop `to_read`, `prefetch`, `MANIFEST_READERS`, `read_many`
and the kept reader, answer question 5 with a sequential `ManifestArtefacts(fetch)`, keep the concurrent case in
`bench/`, and leave 133 s to Phase 5 (`ZO-4`). The second touches no acceptance item, and nothing runs a 7,000-product
blob release before Phase 5.

**M7. `record_uploaded` has no source for `lifecycle`, and its default is unsafe.** `CB-5`, `IR-21`, `ZO-8`, `CMP-11`,
`STR-18`, `CB-16`. Design and spec. `record` requires `lifecycle` (`rules:183`), the manifest has none, and `pending/`
holds none, so a guessed value makes a later `working_copy(lifecycle="final")` raise. The Phase 5 fold has the same gap.
`embargoed=False` records live on an embargo-on catalog, and omitting `retired=` records a dropped edition. D5's "only"
list omits it, and design:819's "as the fold will" is false for the live default. **Fix:** add a `lifecycle=` argument
and a spec question for the fold. Derive state from the head's `embargo`. State each `record` argument. `ZO-8` also
proposes moving it to `tests/stores/_stores.py`, since Phase 6 follows the real fold.

**M8. A `[blob]`-only install crashes on `view()` of any edition with `search/`.** `CB-3`. Design D2, D6. `bundle.py:229`
guards only the module import, `bundle.py:237` builds `LanceIndex`, `lance.py:1080` imports `lancedb` there, and
`check_edition` catches only `duckdb.Error`. **Fix:** catch at the constructor, decide whether `blob` needs `lancedb`, and
test with it absent.

**M9. "Every local caller and test is unchanged" is false.** `CB-8`. Design:303. `tests/test_seal_crash.py:86,88,91` and
`tests/test_inventory_writer.py:118,165,303-305` patch or call what moves, and `bench/corpus.py:170` reads
`ManifestArtefacts._root`. Part 1's PR fails `just ci`. **Fix:** list them as Modify or keep delegating methods, and drop
the claim.

**M10. An operator has no API for `if_seq` or `from_seq`.** `CB-9`, `ZO-13`. Design CUJ 3. `EditionEntry` has no `seq`
(`admin.py:67-89`), and `editions` reads the newest version only, so design:190 describes a read that does not exist.
**Fix:** add `head_seq` and a `versions()` read from retained roots to D7 and Part 4. Also add "expired" to the `from-seq`
error, as GH-98 line 77 asks, or say why stage 1 cannot see one.

**M11. The kept `FullInventory` wedges a writer after `restore_to`, and is unspecified.** `IR-12`, `CB-15`. Design:331-335.
The reader keeps a higher root in the same epoch (`_reader.py:178-180`), so the writer loops on `InventoryConflictError`
to `InventoryContentionError`. `full()` also now returns a shared mutable object, the protocol has no member for "when its
storage says to", and conformance misses this. **Fix:** add the member, rebuild the reader on conflict, have the test
`head` use a fresh reader. M6's second fix removes this finding.

**M12. The D1 seam is incomplete, and `advance_current` diverges from local.** `IR-14`, `DES-11`, `CB-14`, `CB-20`. Design:300-303,
311, 352-353. "Stopping when `held.seq >= root.seq`" drops the epoch test at `inventory.py:388`, so after a recorded restore a
stale `CURRENT` is never replaced. A malformed `CURRENT` is unhandled, `AlreadyExistsError` escapes the retry on a first version,
`repr(admin)` raises (`admin.py:249`), and messages name a path-only `verify-catalog`. `InventoryWriter`'s signature and `data`'s
meaning are unwritten. **Fix:** write the signatures, keep one epoch rule, use `storage.location`, and add the cases to row 908.

**M13. The new rule literals fail the type checker, and a None head crashes.** `IR-9`, `CB-18`, `DES-23`. Design:630-633.
`InventoryRuleError.rule` excludes `if-seq` and `from-seq`, `product` is required though `correct` is catalog-wide
(`inventory.py:140-151`), and `commit` may pass `None` (`:596-597`). `store/inventory.py` is in no Part 4 list. **Fix:** state the
union, `product`'s type and the no-version refusal.

### Implementer decisions left open

**M14. Tests have no injection point.** `IR-3`. Design:305, 335, 452, 517-526, 772, 909. `now()` over `MemoryStore` has no clock
route, a fake `_ObjectOps` has nowhere to go, and GH-98's recording store cannot reach `upload_edition`. **Fix:** write constructors
such as `BlobInventoryStorage(location, *, ops=None, clock=None)`, list `_ObjectOps`'s methods, and state who uses it.

**M15. Tests assert HTTP facts `_ObjectOps` cannot see.** `IR-4`, `STR-21`. Design:838, 861, 906, 936. A wrapper sees arguments.
No recording proxy is specified, and no row covers `x-ms-blob-type` or F10, while F2 runs on `MemoryStore` only. **Fix:** add a proxy
to `tests/stores/_stores.py` in Part 1, and name which rows use it.

**M16. Some Part 4 rows cannot pass as written.** `IR-5`, `CB-23`. Design:966-967, 979, 983-985. Each row "also runs once over
Azurite", yet seal, `doctor` and `prune` have no blob path, and row 979's `pending/` skip has no code until Phase 5. **Fix:** mark
each row local, blob or both, and move the skip to GH-52. `ZO-5` proposes one Azurite smoke test each for `correct` and `reinstate`.

**M17. Test files do not trace.** `IR-6`, `STR-12`, `CMP-24`. Design:878, 926-988, 1035-1042. Four file names appear only in the
map. A `tests/test_correct.py` marked `stores` is not collected by `just test-stores`, and `CLAUDE.md:737-739` requires one mark per
file. All 14 GH-98 items have a row. **Fix:** a File column with full paths, and a layout for Part 4's two tiers.

**M18. Named symbols have no definition.** `IR-10`, `STR-10`, `CMP-22`. `read_many` (885), `pull_edition` (943), `_lookup` (918),
`MANIFEST_READERS`'s home (`store/EGM.md:181` says `cross_product.py` imports only sdmxlib), `prefetch` on `StoreArtefacts`,
`TokenIdentity` before any token is fetched, and `tiers=all` at 790, which passes Python's builtin. **Fix:** a symbol table with module,
signature, returns and raises.

**M19. The blob conformance `Backend` is one clause.** `IR-13`. Design:902-903; `tests/test_inventory_conformance.py:76-197`. Unstated:
shared base or copy, prefix per factory, `restore_to`'s delete, the actor, the clock. With the real `HEAD`, a VM clock over 30 s behind
fails every commit in the pre-push gate (not verified on colima). **Fix:** name `tests/stores/_backend.py` with these choices.

**M20. Error types are undefined.** `IR-15`. Design:323-327, 352, 395, 467, 781-800, 891. Each implementer picks a class, and raw `obstore`
exceptions escape through `commit` and `view()`. **Fix:** an error table covering sites, classes and transport faults.

**M21. The OAuth rig cannot pass as written, and may not earn its cost.** `IR-16`, `ZO-3`, `CB-21`, `CB-13`. Design:405-411, 933, 451.
`allow_invalid_certificates` reaches `obstore` and not `AzureBlobRest`'s `httpx` client, and sdmxlib's `start()` polls `http://`. The command
line, mint values and endpoint derivation are unstated. **Two fixes, the owner picks:** specify the rig, with `verify=` and `root_certificate`
plus `trustme`'s CA; or drop the container, since it serves one row and Azurite validates no real token, test the bearer branch with
`pytest-httpx`, and move the account-key path to a test subclass. The second touches no acceptance item. Revision 2 added the container, and
this review finds no owner decision naming it in W1 to W6.

**M22. Upload and the test tier sit outside the numbered decisions.** `STR-7`. Design:271-281, 369-411, 769-829, 833. Line 833 says each decision
carries a table, and Upload has none. **Fix:** add D8 Upload and D9 Test rig, marked as scope beyond GH-98's questions.

**M23. The upload timeout has no value or home.** `STR-9`. Design:254-256, 533, 936. The caller builds the store, and row 936 tests no timing.
**Fix:** decide who sets it, and add a row or move it to the manual pass.

**M24. The clock's fallback is unbuilt, and D4 never weighs a bearer `HEAD`.** `STR-11`, `ZO-12`, `DES-10`. Design:449-479, 862. Design:862 names a
fallback D4 lacks, and `AzureBlobRest` arrives a Part later. D4 also takes `Date` from any responder, so a gateway ahead of real time passes the
one-sided check (the reviewer has no evidence of one in the deployment). W4's 30 s bound stands. **Fix:** compare a bearer `HEAD` in D4's table,
and either build it or restate the risk row.

**M25. The spec text the design relies on is not pinned.** `STR-3`. Design:10-24. The Inputs link follows `main`, the plan has no commit, and §4.7,
§7.1, §11.3 and §11.4 are unlisted. M4 is the result. **Fix:** name spec revision and commit, and whether the branch merges first.

**M26. O4's dependents are unstated.** `STR-5`. Design:134, 658, 697, 851. "No format change" and two similar statements hold only under options 1
and 3, and design:134 overstates what D7 lists. **Fix:** a per-option edit list, and "blocks S10 and Part 4".

**M27. Docs and exports are unnamed.** `STR-13`, `IR-17`. Design:827, 924, 944, 964. "The operator guide" is no page, `catalog-format.md` has no
`pending/` section, and strict mkdocs fails on an unlisted page. `__init__.py`, `docs/reference/` and `upgrading.md` are in no Part. **Fix:** name
each file and export.

**M28. Stated behaviours have no test row.** `STR-14`, `IR-20`. Upload rerun after a crash, unsealed directory, over-limit refusal, differing manifest;
`prune` refusal; `CatalogAdmin.open` and `create`; `correct` with no change; `catalog_id` and `refresh` on blob. `Product.editions` "through the pull"
downloads files to return `previous`. **Fix:** add the rows, and make it a manifest read.

**M29. The risk table misses the Not-confirmed list.** `STR-15`, `CMP-18`, `STY-5`. Design:240-256, 856-866, 1049-1051. Real-Azure bearer MD5 is the
production path, under one catch-all row with "the signing", a revision 1 leftover. The manual pass covers 4 of 6 items, and sits in Part 1's PR while
its `AzureBlobRest` item needs Part 2. Memory up to 5,000 MiB, O4 and a declared actor have no row. **Fix:** one row per item, and move the checklist.

**M30. The NFR section is incomplete, and coverage may drop.** `STR-16`, `IR-18`. Design:844-854. It has no security, memory or disk entry. `upload.py`,
`AzureBlobRest`, `pull_edition` and the blob branches run only in `stores`, which `just coverage` excludes, and `just ci` would not catch a fall under the
floor. The drop was not computed. **Fix:** add the entries, and state per module what the unit tier covers.

### What a reader will get wrong

**M31. The Overview reads as upload-and-serve.** `CMP-1`, `CMP-13`. Design:37-50, 99, 159-165, 313, 866. A reader plans a production rollout after Phase 4.
"GC keeps its files" and "which expiry keeps" describe unbuilt machinery in the present tense. **Fix:** a paragraph on what a real user gets, and a capability
table by store and phase.

**M32. Terms and references need the spec open.** `CMP-2`, `CMP-14`, `STY-4`, `STY-7`, `STR-17`, `STR-22`. Inventory, root, leaf, stage 2, fold and baked
summary are undefined. The "Question" column means GH-98's list, unsaid. "The plan", "the RFC" and P6 are unlinked. **Fix:** a glossary with spec links, a phase
map, and "GH-98 question N".

**M33. One word carries several meanings.** `CMP-3`. Root, head, restore, rules, Part 4 and probe. A reader could call `_commit_locked` from `correct` on
design:362's "a restore". **Fix:** one qualified term per sense.

**M34. CUJ 3 overstates what a correction removes.** `CMP-4`, `STR-19`, `DES-23`. Design:193-195. It says each later edition is tombstoned. An operator skips
`cancel`, and a kept wrong edition releases. **Fix:** state the embargoed exception and the re-embargo.

**M35. "Drop" has two senses, and row 974 contradicts design:744.** `CMP-5`. An implementer raising on the wrong sense breaks ordinary corrections and W2. **Fix:**
two terms, and reword row 974.

**M36. `rollback` after `reinstate` is stated without its limit, and the workaround has its own.** `CMP-6`, `CB-17`, `STR-19`. Design:201, 695, 700, 743. `rollback`
refuses with no live edition. The second `correct` refuses if any other product's live entry stays tombstoned. `CatalogAdmin.rollback(if_live=None)` already has a
meaning that collides with S9. **Fix:** state both preconditions, and respell S9.

**M37. "Nothing here is built now" heads a table of things built now.** `CMP-10`, `STY-3`, `ZO-7`. Design:58-59, 358, 362, 366, 887. A reader drops `_commit_locked`
and `AzureBlobRest` as stage 2 scope. **Fix:** a "built when" column. `ZO-7` also proposes dropping `exclusive()` and `_commit_locked` until GH-125, which costs
the same then.

## Minor

| # | Reviewers | Target | Finding | Proposed fix |
|---|---|---|---|---|
| m1 | `CB-2` | Design:762 | `**recorded_routing(...)` unpacks a 3-tuple and raises `TypeError` | Pass the three keywords |
| m2 | `DES-12` | Design:326-327 | "Nothing committed" is false: a timed-out create can land later | Say unknown; a named error |
| m3 | `DES-13` | Design:783-794 | F13's self-conflict is unhandled in steps 2, 3 and 6 | Treat as present; `registered` is advisory |
| m4 | `DES-14` | Design:784-796 | A rotted file stays after a failed verify, and poisons later editions | Hash before each Put |
| m5 | `DES-15` | Design:557, sdmxlib `_manifest.py` | No path check on pull; unverified as an exploit | Refuse absolute and `..` paths |
| m6 | `DES-16`, `CB-13` | Design:714-715 | Default fingerprints cost 774 s against 46 s on 2.0 GB; each lost race repeats verify | State `fingerprints`, memoize |
| m7 | `DES-17` | Design:632-635 | `from_seq`'s `restore_epoch` is unchecked | Refuse a differing epoch |
| m8 | `DES-18` | Design:39 | An invalid head root wedges writers; only a writer bug reaches it | A specific error and recovery |
| m9 | `DES-19` | Design:764-766 | `reinstate` trusts a replaced manifest | Compare to the last root naming it |
| m10 | `DES-21` | Design:554-559 | Staged directories leak; a cached SHA mismatch is undefined; `EGM.md:339` goes stale | Sweep, raise, update |
| m11 | `DES-22` | Design:392-393 | The account-key branch reaches real accounts; SAS in logs unverified | Limit to emulators (see M21) |
| m12 | `CB-12`, `IR-24`, `DES-23` | Design:789-790 | Step 5 has no `extensions=`, so no `statcat` check runs; data faults report twice | Pass it; limit `verify_remote` |
| m13 | `CB-13` | Design D7, Upload | Misses `publish_check`, `edition_files`, `edition_kind`; a colliding name; a false error message | Reuse them |
| m14 | `CB-19`, `CMP-16`, `DES-23` | Design:317-319, 846 | 10 reads and 2 `Date` HEADs on an admin commit; 63 s, 70 s, 1 s, 1.1 s unexplained | Correct; a numbers table |
| m15 | `CB-22` | Design D3 | A provider may return a coroutine, key or SAS | State supported shapes |
| m16 | `CB-24` | Spec:510, 522 | The `(sealed)` row's fast-forward check contradicts `record` | Fix with S1 |
| m17 | `CB-25` | Spec:277 | `check_catalog` runs everything at `open`; each `view()` pays | Align; record the cost |
| m18 | `CB-26` | sdmxlib `_remote.py:7-8` | Docstring disagrees with F7 | Update after real Azure |
| m19 | `CB-27`, `CMP-23`, `DES-23` | Design:753 | "The withdrawn base" is a tombstoned one | Reword |
| m20 | `IR-19` | Design:381 | JWT `json.loads` trips the `Any` ratchet | A typed struct; `TYPE_CHECKING` imports |
| m21 | `IR-22` | Design:793, spec:969 | `pending/` has no byte format, and Phase 5 reads it | Define it; a contract goes in the spec |
| m22 | `IR-23` | Design:994-1025 | Generator values, test file, bench Azurite, PR owner | State them |
| m23 | `IR-24` | Design:870, 897 | Stale "`LocalStore`"; Parts release separately only in order | Reword |
| m24 | `CMP-15` | Design:720 | "Keeps its bytes" fails when the rule alters the leaf | Limit the sentence |
| m25 | `CMP-17`, `STR-20` | Design:120, 149, 238, 860 | F17 out of order; O4 alone; S5 no contract question | Reorder and explain |
| m26 | `CMP-20`, `ZO-6` | Design:109, 266, 902 | S3 scope is scattered; SeaweedFS adds a container on each push with no populated catalog | One sentence; drop it from the gate; W3 stands |
| m27 | `CMP-21` | Design:171-183 | CUJ 2 invites copying a `connection_string` to production | Annotate |
| m28 | `STR-23` | Design:871 | Parts must not edit `CHANGELOG.md`; `count_any.py` omitted | Use phase-3's sentence |
| m29 | `STR-24` | Design:868 on | No Parts table with "Waits on"; future work scattered | House form |
| m30 | `STR-26` | Plan:330, GH-98 line 61 | Both say `check_edition` verifies every file; it covers `data/` | Amend or note in S5 |
| m31 | `ZO-5` | Design:966-967, 865 | 18 rows rerun on Azurite on each push; 20 s is borrowed; the cost is an estimate | Smoke tests; measure |
| m32 | `ZO-10` | Design:519-526 | `actor` on `BlobLocation` is what creates S2 | Pass it to the writer |
| m33 | `STY-1`, `STY-2`, `STY-5` to `STY-13` | Design | Hook headings, three colon setups, thin table cells, repeats. The document is largely clean | `style_report.md` has each rewrite |

`DES-23`'s other item: `admin.py:364-366` promises other-schema rollback "in Phase 4", and neither the design nor GH-98 scopes it. Scope it or fix the docstring.

## O4 assessment

**Recommendation: option 1, and only with the table below.** `DES` and `ZO` reached this independently. No listed option closes the embargo gaps, because the
options cover one cell of a larger table: an entry embargoed in the head and absent from the restored leaf.

| Option | What holds | What the design's text gets wrong |
|---|---|---|
| 1, keep | `release_at` survives, the fold skips the entry, a dropped `previous` makes release refuse (`rules:155-175`), no format change | "Recovery never blocks": a kept structure entry's bad manifest raises `ManifestError` (`cross_product.py:252,271`). "No code beyond the rule": spec:519 needs editing. A wrong entry releases silently, and `correct` returns an int |
| 2, tombstone carries it | A wrong entry cannot release by itself | A correct dated release silently vanishes, since `due` reads leaves only. `retired_at` starts the window for a far-future edition. A reinstate becomes incoming, unlike design:766. It leaves M1 open, since `cancel` already cleared `release_at`, and one deliberate `CatalogAdmin.release` publishes any embargoed edition anyway |
| 3, refuse | Forces the operator to look | "No gap" is false: a cancelled entry is withdrawn and then tombstoned, opening M1. `cancel` has no way back, so a good dated release needs a new seal during an incident. The fold skips it through `retired`, unlike design:137 |

**A further option the table misses.** `correct` applies `cancel` to each embargoed entry recorded after `from_seq` (`ZO`). No format change, and it fails closed: nothing wrong
publishes, and a good edition misses its time and needs a new seal. The choice between this and option 1 is the owner's ranking of harms. Option 1 can publish a wrong edition on
schedule. This one cancels every unrelated release in the window, so the reviewer's default is option 1.

**Further embargo paths.** All six are in findings above.

1. Embargoed in head, live or superseded in restored leaf (B2 scenario 1).
2. Withdrawn in head, embargoed or live in restored leaf, including `supersede_pending` (B2 scenario 2).
3. Live in head, embargoed in restored leaf with a past `release_at` (B2 scenario 3).
4. Cancelled, tombstoned, reinstated, rolled back, including the default target (M1).
5. Verification pulling embargoed editions to disk (M2).
6. A rerun that fails to change a wrong `release_at` (M3).

Also, `record_uploaded`'s default records live on an embargo-on catalog (M7), in test and drill use.

Traced clean: the fold, `LiveInventory`, the `Catalog` cache for embargoed entries, `set_embargo`, a kept entry's fast-forward refusal, and `cache_dir` after a re-embargo.

**Proposed rule for sdmxlib's `correct`**, from `DES`, with `ZO-1`'s rule as its core. H is the head's state and R the restored state.

| H | R | Result |
|---|---|---|
| embargoed | any | Keep the head's entry |
| withdrawn | embargoed | Stays withdrawn |
| withdrawn | live or superseded | Refuse unless the operator names the edition |
| live or superseded | embargoed | Restore with `release_at=null`, so an explicit release is required |
| embargoed | absent | Keep (option 1), and log a WARNING naming the edition and its `release_at` |
| any other | absent | Tombstone (W2) |
| tombstoned | live | Refuse, as drafted |

**Owner decisions this needs.**

1. Whether "kept is not dropped" is the reading of W2. GH-98's line 107 survives it.
2. Option 1 or the fail-closed `cancel`.
3. Whether `Tombstone` gains an optional field for "was ever public". Without it, M1 is documented as a known operator risk.

`release_at=null` means the entry is no longer "unchanged bytes" (design:720).

## Findings for other repositories

### sdmxlib spec

| Finding | Location | Change |
|---|---|---|
| M4 | Spec:519 | The `any state` row follows O4; add D7's refusal on a tombstoned live entry |
| M5 | §11.4, spec:990 | GC counts manifests named in `pending/`, with a minimum age |
| M1 | Spec:523 | State the default rollback target and the reinstated-edition risk |
| M7 | §11.2 step 1 | Where the fold gets `lifecycle` |
| m16 | Spec:510, 522 | The `(sealed)` row's fast-forward check |
| m17 | Spec:277 | Extensions at `open` are "section only" while `check_catalog` runs everything |
| m21 | §11.2, spec:969 | The `pending/` document's format, if a contract |
| B3 | S1, S2, S3, S7, S9, S11 | The design's own proposals, which need an owner and order. S8 is done at `011312c3` apart from M4 |

### sdmxlib plan

| Finding | Location | Change |
|---|---|---|
| B3 | Plan:322-344 | Add S6, S10 and S11, and any `check_on_open` change, to Phase 4 |
| m30 | Plan:330, also GH-98 line 61 | Upload's verification names `check_edition`, which covers `data/` only |

### sdmxlib code

| Finding | Location | Change |
|---|---|---|
| B1 | `edition/_check.py:305-313` | `check_on_open(structures=)`, and a way to defer references at `receive` |
| B2 | New `sdmxlib.inventory.correct` (S10) | The H and R table above, with a conformance check per row |
| M1 | `inventory/_rules.py:427` | The default rollback target ranks by `state_changed_at` or skips reinstated entries |
| m5 | `storage/_manifest.py`, `edition/_copy.py:158` | Refuse absolute and `..` paths |
| m18 | `edition/_remote.py:7-8` | Docstring, after real Azure confirms |

### Hub

| Finding | Location | Change |
|---|---|---|
| M4 | `docs/design/inventory-history-log.md:196-197` | Says a tombstone comes from a `retire` version |
| B3 | Suite design | Decide whether D7's new rule and tombstone semantics change revision 4 |

## Sound under review

- The `if_seq` loop: a lost race re-enters the closure and refuses (`inventory.py:595-613`).
- `try_commit` and `next_root` give `previous_inventory=from_seq` with links and diffs on `seq - 1`, and `Change` needs no new field.
- The table "What `sdmxlib.inventory` accepts" matches the code in every row, with two added conditions on `record` (M7).
- Commit crash points, unconditional leaf Puts under retry, and the rename race.
- GH-98's 14 acceptance items each have a row or artefact, its seven questions each have an answer, and Parts 1 to 5 follow its order.
- The bench numbers, the spec section references and the W5 docstring warning.
- W1 to W6: no concrete defect was found. W6's Docker cost is as stated.

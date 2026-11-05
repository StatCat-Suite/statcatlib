# Design critic review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** the EGM feature-spec standard.
**Reviewer:** `egm-design`.

**Verdict as reported:** not ready. 1 Blocker, 8 Major, 13 Minor. O4: take option 1, and no
option alone closes the embargo gaps (DES-2 to DES-6). `correct` needs one rule covering
every pair of head state and restored state.

**How it was checked:** by reading only; nothing was run. Design line numbers refer to the
target. "statcat" is `/Users/gabrielgellner/dev/statcatlib`; "sdmx" is
`/Users/gabrielgellner/dev/sdmxlib`.

**Coverage:** read in full: the design, the standard, the GH-98 body, `_rules.py`,
`_reader.py`, `_check.py`, `_remote.py`, `cross_product.py`, `inventory.py`, `admin.py`, and
the `EGM.md` files for inventory, store and statcatlib. Read in part: spec §4.6, §4.7, §6.1 to
§6.8, §10 and §11; plan Phase 4 only; `conformance.py` (the checks that bear on this). Searched
by keyword only: the hub design. Spec §6.9 was read only at its opening. Not read: the `EGM.md`
files for cli, sources and model.

## Blocker

### DES-1. Three checks refuse a valid structure edition because none passes `structures=`

**Target:** design and sdmxlib code. **Citations:** design 789 (upload step 5), 560 (D6 step 4), 712-713 (`_verify_edition`); sdmx `src/sdmxlib/edition/_check.py:293` (references runs at receive and publish), `:747-759` (`structures=None` reports every unresolved reference), `:305-313` (`check_on_open` has no `structures` parameter); statcat `src/statcatlib/testing.py:285`, `:405-406` (`CS_CODR`'s concepts name codelists in other products: "its publish check resolves references in the other two"); statcat `src/statcatlib/diagnostics.py:315-317` (`verify-catalog` does pass `structures=`); spec §6.5 item 9, which uses this exact case as its example.

**Failure scenario, from the code paths:**

- Upload: `upload_edition` on structure-concepts gets references findings, raises `UploadCheckError` and never registers. Upload may not read the Inventory, so it cannot compute `structures=` itself. `record_uploaded` runs upload first, so any Part 3 or Part 4 fixture that records the CODR structure products fails the same way.
- Open: the first `view()` of that product raises `IncompatibleCatalogError` from `check_on_open`. sdmxlib-rest pods would hit the same check through the same function.
- Recovery: `correct` or `reinstate` of that edition raises `EditionCheckError`, so recovery is blocked for a healthy edition.

**Fix:**

- sdmxlib: add `structures=` to `check_on_open`, plus a receive option that defers unresolved references when the caller cannot know the structure editions.
- Design: upload defers cross-product references (the fold or the local publish check owns them). D6 and D7 pass the editions `_structures_for` names. On blob, D7 must also pull those editions.
- Add a test row for each of the three sites using structure-concepts.

## Major

### DES-2. A correction of a correction publishes an embargoed edition early

**Target:** design and the new sdmxlib `correct` rule (S10). **Citations:** design 645-646 (keep applies only when the entry is "embargoed in the head and absent from the restored leaf"), 747-748; sdmx `inventory/_rules.py:302` (the due check lives only in `release`).

**Failure scenario:**

1. Version 39: E embargoed, `release_at` T. Version 40: E released prematurely.
2. Version 41: `correct(from_seq=39)` returns E to embargoed.
3. Version 42: another product goes wrong, and the operator judges 40 good for it. `correct(from_seq=40, if_seq=42)` restores E as live before T.

Rules pass, verification passes, and nothing tests `release_at`. A variant stops at `superseded`, then a `rollback` makes it live.

**Fix:** an entry embargoed in the head stays unchanged, whatever the restored leaf holds. Test it.

### DES-3. A correction resurrects a withdrawn edition, and an embargoed return can auto-release

**Target:** design and the sdmxlib `correct` rule. **Citations:** design 636-649 (step 4 has no case for an entry withdrawn in the head and present in the restored leaf); spec §6.4 line 505 (withdrawn is "never routed again automatically"); `_rules.py:365-389` (`supersede_pending`), `:313-320`, `:340-344` (`due` and `release_due` run in id order).

**Failure scenarios:**

- Cancel: E embargoed at 39, cancelled at 41 because it is wrong or confidential. `correct(from_seq=39)` restores E as embargoed with its old `release_at`, and the next release job publishes it.
- Pending correction: E_old embargoed at 39. At 40, E_new is recorded and `supersede_pending` withdraws E_old. Correct to 39 restores E_old, option 1 keeps E_new, and both are due. `release_due` takes the lower id first, so E_old goes live and E_new is refused. The known-wrong edition is published and the fix is rejected.
- Leak: a rollback at 41 for a confidentiality leak goes live again when `correct` targets an unrelated error at 40.

`correct` is catalog-wide, so an operator fixing product Q reverses these on product P with no signal. Test rows 974 to 986 cover none of it.

**Fix:** withdrawn in the head stays withdrawn when the restored state is embargoed. For restored live or superseded, refuse unless the operator names the edition in an explicit argument. Offer a product-scoped form, or list what would change across other products.

### DES-4. A release undone by `correct` is re-released automatically once Phase 5 runs

**Target:** design. **Citations:** design 747-748 ("it needs releasing again"), test row 986; `_rules.py:313-320`; spec §6.4 line 525, §11.2 step 4.

**Failure scenario:** E's release at 40 was the error. `correct(from_seq=39)` restores E as embargoed with its `release_at`, which has already passed. The next release job finds it due and republishes it, reversing the correction. An operator reads the design as saying a manual release is needed.

**Fix:** the rule writes `release_at=null` for an entry live or superseded in the head and embargoed in the restored leaf, so `due()` skips it and an explicit release is required. `validate_leaf` allows this, and the leaf is then no longer "unchanged bytes" (design 720). State that.

### DES-5. `reinstate` then `rollback` publishes never-released editions by a path O4 does not consider

**Target:** design, spec and sdmxlib code. **Citations:** `_rules.py:127-140` (`_moved` clears `release_at`), `:512-522` (`reinstate` writes `superseded`), `:422-427` (default target is `candidates[-1]`, the highest `edition_id`, while the spec says most recent superseded); spec line 523, line 518 (retention), line 519 (`correct`); O4 at design 120-126.

**Failure scenarios:**

- E is cancelled under embargo, so it is withdrawn, and it never went public. Prune's `retire` or `correct`'s "every other entry" (design 647) tombstones it. `reinstate` makes it `superseded`. `rollback` makes it live, with no release time anywhere. This holds under all three O4 options.
- Silent variant: reinstate E3 (newest id, dropped by a correction), then seal E4 on E2. `rollback(product, reason=...)` with `to=None` picks E3 by id and publishes an edition the correction dropped or that never went public.

**Fix:** sdmxlib: the default target ranks by `state_changed_at`, or skips reinstated entries; require an explicit `to=` for a reinstated edition. Spec and design: record whether a tombstoned edition was ever public (an optional `Tombstone` field, see O4), and have `reinstate` refuse a never-public edition without an explicit argument.

### DES-6. Verification copies embargoed editions out of the boundary

**Target:** design. **Citations:** design 712-713 (on blob, `_verify_edition` "pulls the edition as in D6"), 651-653, 759-760, 611-613 (claims only live editions reach the cache), 506 (rejects a disk cache of records for this reason), S3 at 147 (a laptop `CatalogAdmin` is a blob writer); spec 944 ("A copy of a catalog is outside the embargo boundary"); test row 952, which covers `Catalog` only.

**Failure scenario:** an operator on a laptop runs `correct` where a restored entry is embargoed at `from_seq`, or reinstates an edition cancelled under embargo. `metadata.duckdb`, `tables.ducklake` and `search/` land in the operator's local cache and stay, since nothing evicts them. `cache_dir` may also be `None` on `CatalogAdmin` (design 525, 538 require it only for `Catalog.open`), so where the pull lands is undefined.

**Fix:** verify in a private temporary directory removed in a `finally` block, never in `cache_dir`. State where it runs. Add a test that no embargoed file remains after `correct` and `reinstate`.

### DES-7. An upload rerun with a different release time is silently ignored

**Target:** design. **Citations:** design 793-794, 929.

**Failure scenario:** the first upload used a wrong, too-early `--release-at`. The operator reruns with the right time. `AlreadyExistsError` gives `registered=False`, "no error". The old time stands, and Phase 5 folds it and releases early. Exit status is success, and the report says only that nothing new was registered.

**Fix:** on `AlreadyExistsError`, read the entry and compare it. Equal is `registered=False`; different raises `UploadCheckError` naming both times. This stays inside the publish job's rights (S7). Also refuse a naive `--release-at`.

### DES-8. The cold release estimate leaves out 7,000 sequential leaf GETs

**Target:** design. **Citations:** statcat `src/statcatlib/store/cross_product.py:212-216` (`_planned` calls `full.leaves()`), `:398`; sdmx `inventory/_reader.py:302-304` (one fetch at a time); design 487-498, 503 ("About 60 times faster"), 319, 333, 846-849, 506 (a job's pod starts cold).

**Failure scenario:** a cold `release` or `rollback` at 7,000 products. `to_read` and the rules need all leaves first, about 70 s at 10 ms, then the prefetched manifests at about 1 s. Total is about 71 s against about 133 s with no prefetch, roughly 2 times faster. Part 5 benches a 64-thread leaf prefetch, no production path uses one, and `read_many` (Part 1) has no named consumer on the rules' path. The same applies to `Catalog.products()`.

**Fix:** a `Fetch` adapter that bulk-fills from the root's leaf refs before `full.leaves()` and `LiveInventory.products()`, added to Part 1's scope. Restate the D5 and NFR numbers as a total. No private reader field is needed if the adapter serves from its own map.

### DES-9. GC can delete the files of a pending edition

**Target:** spec; does not block Phase 4. **Citations:** spec §11.4 line 990 (deletes data files "listed in no manifest of any edition recorded or tombstoned within the window", with no age guard); design 100 and GH-98 (an upload waits in `pending/` with nothing folding it); design 784-785 ("Present files stay").

**Failure scenario:** an uploaded edition is not recorded, so its new files and any carried file are listed by no recorded manifest. A GC run deletes them. Upload verified earlier and `pending/` still exists, so the fold records an edition whose bytes are gone. Compare `prune`, which scans on-disk manifests and has an mtime guard (`admin.py:709-719`).

**Fix:** §11.4 counts manifests named by `pending/` entries, plus a minimum age for any file. Settle this before Phase 5 builds GC.

## Minor

### DES-10. The clock takes `Date` from any responder, with a one-sided bound

**Target:** design. Environment-dependent, labelled as such. **Citations:** design 449-460 ("whatever status comes back"), 466; `_rules.py:550-557`. **Scenario:** a TLS-inspecting gateway or a front-end proxy answers the anonymous `HEAD` with its own `Date`, ahead of real time. The check tests only local minus storage, so it passes. `created_at` is then in the future, and other writers are refused for being over 5 minutes behind the base. Phase 5's due check could fire early. The reviewer has no evidence of such a proxy in the deployment. **Fix:** use a bearer `HEAD` on `catalog.json` first when a `TokenIdentity` exists (F18). It needs no SAS and is absent from D4's alternatives table. Also refuse a `Date` far ahead of local unless the operator overrides it.

### DES-11. `advance_current` as written loses three behaviours of the local code

**Target:** design. **Citations:** design 311; statcat `src/statcatlib/store/inventory.py:384-389`; F1. **Scenarios:** "stopping when `held.seq >= root.seq`" omits the epoch test, so after a restore `CURRENT` holding a higher old-epoch seq never advances; conformance does not catch this, because its backend lowers `CURRENT` first (statcat `tests/test_inventory_conformance.py:184-197`). A malformed `CURRENT` is not treated as replaceable, though the local code does. `mode="create"` raises `AlreadyExistsError`, while the loop names the precondition error, so a first-version race escapes the retry. **Fix:** restate the condition as the local code has it, and add the three cases to the unit row at 908.

### DES-12. "Nothing committed" is an overclaim

**Target:** design. **Citations:** design 326-327; F13. **Scenario:** a timed-out create can land after the read returned absent and after the retry failed, so the caller is told nothing was committed while a root appears. Retries stay safe through `if_seq` and the rules. What the error tells the operator is false. A transport error on the resolving read is also undefined. **Fix:** say the outcome is unknown, and raise a named error telling the operator to re-read the head.

### DES-13. Upload's creates do not handle F13's self-conflict

**Target:** design. **Citations:** design 783, 786, 793-794. **Scenarios:** in steps 2 and 3, a 5xx retry after the first attempt succeeded raises `AlreadyExistsError`, and only step 4's handling is described. A concurrent uploader of a shared data file does the same. At step 6 the same retry reports `registered=False` on the run that registered. **Fix:** treat `AlreadyExistsError` as present in steps 2 and 3 and let step 5 judge. Document `registered` as advisory.

### DES-14. A bad blob cannot be repaired by rerunning, and it can poison later editions

**Target:** design. **Citations:** design 784-785, 796; `_check.py:391-392` (local data not hashed when a lookup is given), `_remote.py:111-118`; spec 990 (only GC deletes under `products/`). **Scenario:** a local data file has bit rot. The create sends it, with no `Content-MD5` to reject it (F6), and step 5 finds the mismatch. Reruns keep the present file and fail again. A new edition carrying that data file lists the same path and fails too. **Fix:** hash each file against the manifest before its Put. The bytes are already in memory (F11), so this is cheap. Add a runbook for a human delete.

### DES-15. Pull has no path check

**Target:** design and sdmxlib code. Unverified as an exploit, because `obstore`'s path handling is not on disk to read. **Citations:** sdmx `src/sdmxlib/storage/_manifest.py:258-267`, `:471-503`; sdmx `src/sdmxlib/edition/_copy.py:141-142`, `:158` (`dst / entry.path`); `_check.py:341` (stamps do guard this); design 557; spec §10.1 (publish job and pods are separate principals). **Scenario:** a publish identity writes a manifest whose `root=edition` path is absolute or holds `..`. Its SHA is recorded honestly by the fold. A pod's pull joins the path and writes outside the staged directory before any check, if the store GET succeeds. **Fix:** refuse absolute paths and `..` in `decode_manifest` or in `pull_edition`. Apply the same check to `catalog_id` (design 547), leaf edition ids and upload.

### DES-16. Verification cost is understated

**Target:** design. **Citations:** design 714-715, 759-760; `_check.py:148-150` (774 s against 46 s on 2.0 GB when fingerprints are recomputed), `:296`. **Scenarios:** the local `at="publish"` call recomputes fingerprints and availability by default. A correction across a republish verifies every differing entry, up to the 117 GB of a full catalog, and on blob pulls them all. `reinstate` verifies inside the closure, so each lost race repeats it. **Fix:** state whether `fingerprints=False` applies, memoize a verified (edition, sha) across races, and estimate the worst case.

### DES-17. `from_seq`'s epoch is unchecked

**Target:** design. **Citations:** design 632-635; spec line 952 ("a restored Inventory version is never made current"). **Scenario:** a root left from an earlier `restore_epoch` decodes and is restored. **Fix:** refuse `rule="from-seq"` when `root.restore_epoch` differs from the head's.

### DES-18. An invalid head root wedges every writer, and `correct` cannot help

**Target:** design. Reachable only through a writer bug. **Citations:** `_reader.py:198-203` (the reader skips it); `inventory.py:553-557`, `:595-615`. **Scenario:** seq 43 exists and is invalid. The base stays 42, every create conflicts, and after 8 races the writer raises `InventoryContentionError` with a misleading "try again". The design's "recover from a wrong Inventory version" (39) does not cover it. **Fix:** when create reports the name taken and `full.skipped` names that seq, raise a specific error. Document the recovery.

### DES-19. `reinstate` trusts whatever manifest is there now

**Target:** design. **Citations:** design 764-766; spec §10.1 (publish job writes `products/`). **Scenario:** a tombstoned edition's manifest and files are replaced under its id. Verification passes against the new manifest, and the new hash is recorded. **Fix:** compare against the entry in the last root that named it when that root is available. Log when it is absent.

### DES-20. Spec and hub text are out of step

**Target:** spec and hub. **Citations:** spec line 519 ("any state") conflicts with option 1, which excludes embargoed; hub `docs/design/inventory-history-log.md:196-197` still says a tombstone comes from a `retire` version. Design S8 at 152 says §6.3 lacks `correct`, and spec line 427 at `011312c3` already has it. **Fix:** align the spec and hub once O4 is decided, and update S8.

### DES-21. Cache lifecycle gaps

**Target:** design. **Citations:** design 554-559, 113, 618-619; statcat `src/statcatlib/EGM.md:339` ("`Catalog.open` writes nothing"). **Scenarios:** a crash or ENOSPC leaves staged directories that nothing removes. Full use reaches about 117 GB with no eviction. Step 2 does not say what a cached manifest with the wrong SHA does: raise or re-pull. The `EGM.md` invariant needs restating for blob. **Fix:** sweep old staged directories at pull, define the mismatch as a raise naming the path, and update `EGM.md`.

### DES-22. The account-key branch is not limited to emulators

**Target:** design. Unverified: `httpx` source is not on disk, and the reviewer recalls it logging full URLs at INFO. **Citations:** design 392-393; spec 940 (no keys, no SAS). **Scenario:** any real account with a key in `store.config` takes the signed-URL branch, and a read SAS on an embargoed blob could reach the logs. **Fix:** quiet the `httpx` logger for this client, and log or refuse this branch on a non-emulator endpoint.

### DES-23. Small corrections

**Target:** design.

- `admin.py:249`: `CatalogAdmin.__repr__` reads `writer.root`, which design 352 makes raise on blob.
- `admin.py:364-366` promises other-schema rollback "in Phase 4". The design and GH-98 are silent, so either scope it or fix the docstring.
- Design 753 says the refusal names "the withdrawn base". `_rules.py:167-173` says "not recorded" for a tombstoned base with no live edition.
- CUJ 3 (193-195) omits the embargoed exception.
- Step 1 (630) needs a guard for `full=None`, because `commit` may pass `None` (`inventory.py:596-597`).
- Upload step 5 (789) claims the `statcat` section is checked, and the call shown has no `extensions=`.
- D4 says one `HEAD` per `try_commit` and per `_commit_product`, so an admin commit makes two, while the cost count (317-319) lists one.

## O4 assessment

**Recommendation:** option 1 is the best base, and it is not sufficient as drafted. It needs the
rule table below. Whichever option is chosen, DES-2 to DES-5 remain unless that table goes in.

**Option 1 (keep in the leaf). What holds in code:** `release_at` survives, since no `_moved` call
touches the entry. The fold skips it (§11.2), and `record` refuses a duplicate (`_rules.py:244`). A
dropped `previous` makes `release` refuse (`_rules.py:155-175`). It is not incoming, so step 6 and
the rules do not act on it.

**Corrections to option 1's text:**

- "Recovery never blocks" (design 130) is false. With anything incoming, `_Rules` reads every active structure edition's manifest, kept ones included (`cross_product.py:252`, 271). A kept embargoed structure entry with a bad manifest raises `ManifestError`. The way out is `cancel`, then `correct`.
- A wrong entry whose `previous` is live, or a first edition with `previous=None` in an otherwise empty leaf, releases on schedule, and the operator gets no signal. `correct` returns an int (design 624). Add a WARNING listing kept entries and their `release_at`, and put them in the report or a readable list.
- The keep rule covers only "absent from the restored leaf" (DES-2).

**Option 2 (`Tombstone` carries the state and `release_at`).** Decodes compatibly: `Tombstone` does
not forbid unknown fields (`_format.py:178`), though older readers drop the field if they rewrite it.
Costs the table omits:

- The release silently disappears. `due()` reads leaves only (`_rules.py:313-320`), so a correct 08:30 release does not happen, and §11.6's "embargoed past `release_at`" alert has nothing to fire on.
- `retired_at` starts the window, so GC could remove a far-future edition.
- A reinstate into embargoed becomes incoming, so it now runs the rules, unlike design 766.
- A wrong entry is safer here, a right one is worse. Option 1 has the opposite balance.

It does not close DES-5 for cancelled editions unless the field records "was never public" too.

**Option 3 (refuse, cancel first).** "No gap" (design 132) is false. Once cancelled, the entry is
withdrawn and step 4 tombstones it, so DES-5's `reinstate` and `rollback` path opens. It is not a
withdrawn entry at the fold, as design 137 implies; the fold still skips it, through `retired`.
`cancel` is also irreversible for a good entry: §6.4 has no un-cancel, and `release_at` is cleared at
`_rules.py:135`. So a valid scheduled release needs a new seal under a new id, during an incident.

**Embargo paths no option covers:**

1. Embargoed in head, live or superseded in restored leaf (DES-2).
2. Withdrawn in head, embargoed or live in restored leaf, including `supersede_pending` (DES-3).
3. Live in head, embargoed in restored leaf with a past `release_at` (DES-4).
4. Cancelled, then retired or dropped, then `reinstate` and `rollback`; default rollback picks by id (DES-5).
5. Verification pulls embargoed editions to disk (DES-6).
6. A rerun that fails to change a wrong `release_at` (DES-7).

**Traced and clean:** the fold (§11.2 skips a leaf or a tombstone), `LiveInventory`
(`_reader.py:323-327` holds live entries only), the `Catalog` cache for embargoed entries,
`set_embargo` (`Change.embargo=None` keeps the head's), and a kept entry's fast-forward refusal.

**Proposed rule for `sdmxlib.inventory.correct`** (H = head state, R = restored state):

| H | R | Result |
|---|---|---|
| embargoed | any | keep the head's entry |
| withdrawn | embargoed | stays withdrawn |
| withdrawn | live or superseded | refuse unless named explicitly |
| live or superseded | embargoed | restore with `release_at=null` |
| embargoed | absent | keep (option 1), and warn |
| any other | absent | tombstone (W2) |
| tombstoned | live | refuse, as drafted |

Plus one owner decision: an optional `Tombstone` field recording whether the edition was ever public,
so `reinstate` can refuse a never-public edition by default. Without it, Phase 4 must document DES-5
as a known operator risk.

## Checked and found sound

- `if_seq`: a lost race re-enters the closure and refuses, so no retry erases a commit (`inventory.py:595-613`).
- History: `try_commit` and `next_root` already give `previous_inventory=from_seq` with links and diffs on `seq - 1`. `retired` and `built_from` need no new field.
- Design table 686-695 matches the code: `retire` refuses live and embargoed; `rollback` refuses with no live edition, also with `if_live=None`; `change_list` items, `_check_item`, `record`'s tombstone refusal, `reinstate` and repairable all behave as stated. Decode and validate of an empty leaf: `validate_leaf`'s first part is consistent, and the rest was not read.
- Commit crash points: a crash after leaves leaves orphans only; a crash after the root is committed with a lagging `CURRENT` that readers probe past.
- Unconditional leaf Puts and retries: idempotent with no read and no conflict; a restored leaf is rewritten, so a missing object returns.
- Create's equal-bytes rule: two same-actor writers on one base in the same `Date` second could produce identical roots and both "win". The state is the same, so this is harmless.
- `CURRENT` under F13: a retried update that gets 412 re-reads and stops. Forward-only holds apart from DES-11.
- Upload crash and rerun: a single Put means a present file is complete, manifest last holds, and verification precedes `pending/`.
- Rename race: atomic directory rename and a complete-implies-manifest check are safe, and the stamp is keyed by manifest digest (`_check.py:362-368`).
- Tombstones, embargo and extensions come from the head, as DES-M6 asks, and `from_seq` errors cover N13.
- S4 confirmed at `_check.py:400-401`, S5 at `:202-205`. F-independent claims in S9, S10 and S11 match the code.
- Standard compliance: problem, goals, non-goals, architecture, alternatives (per decision) and risks are present. The risk table lacks the items above. Cost is given as request counts with no currency figures, adequate at these volumes.

## `obstore` facts F1 to F22

Not independently verified: no `obstore` install under sdmxlib or statcatlib, and no sdist on disk.
None treated as wrong. Partial corroboration: F7's `HEAD` pattern, since sdmx `tests/stores/_stores.py:228-232`
reads `Content-MD5` from a `HEAD`. sdmx `src/sdmxlib/edition/_remote.py:7-8` describes the block-upload
header and is consistent with F7 without establishing it. `REQUIRED_DIGESTS` (`_manifest.py:63`) confirms
the manifest holds `md5`, so the comparison at design 931 is possible.

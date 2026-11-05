# Zoom-out review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** the EGM zoom-out standard.
**Reviewer:** `egm-zoom-out`.

**Verdict as reported:** no Blockers. The design meets GH-98's acceptance list and answers
its seven questions, and it is larger than the list needs. 4 Major, 9 Minor. ZO-1 is an
embargo defect that O4's options miss. ZO-2 is scope GH-98 asks for that cannot be built as
described. ZO-3 and ZO-4 are built pieces with no Phase 4 caller, or tests no acceptance
item needs. The removals proposed change no GH-98 acceptance item.

**Checked:** the target in full, the GH-98 snapshot, `CLAUDE.md`, the `EGM.md` files,
sdmxlib `_rules.py` in full, DES-M6, N13 and ZM2; relevant parts of the spec, the plan,
`conformance.py`, `_reader.py`, `inventory.py` and `tests/stores/`; the hub design through
targeted searches. Nothing was run. F1 to F22 are not independently verified, and they are
not treated as wrong.

**Short paths:** "design" is the target. "rules" is sdmxlib `src/sdmxlib/inventory/_rules.py`.
"spec" is sdmxlib `docs/design/sharded-bundle-catalog.md`. "#98" is the GH-98 snapshot.

## Major

### ZO-1. A correction re-arms a cancelled embargoed edition, and the release job then publishes it unattended

**Target:** design (D7) and the S10 rule in sdmxlib. **Citations:** design:636-649 (steps 3 and 4 handle only head tombstones, entries embargoed in the head and absent from the restored leaf, and entries absent from the restored leaf); design:747-749 (covers a release after `from_seq`, says nothing about a cancel); rules:350-362 (`cancel`); rules:313-320 (`due`); rules:323-347 (`release_due`); spec:505 ("never routed again automatically"); spec:55.

**Scenario:**

1. At version 39, E is embargoed with `release_at` T.
2. At version 40, an operator cancels E, say for a confidentiality error, and E is withdrawn in the head.
3. Version 41 is wrong, and the operator runs `correct(from_seq=39, ...)`.
4. E is in both the head and the restored leaf and is not tombstoned, so the restored copy wins: embargoed, with `release_at` T. It is incoming, its files verify, and the rules pass.
5. At T, Phase 5's `release_due` makes it live with no operator action.

**A worse variant.** E was replaced by embargoed N through `supersede_pending` (rules:365). Option 1 keeps N and the correction restores E. Both are due. `due` follows leaf order, sorted by id (rules:121), so the older E releases first. N then fails fast-forward. The withdrawn edition goes out and its replacement is refused. This variant comes from reading the code and assuming UUIDv7 ids sort by time. It was not run. `CatalogAdmin` does not expose `supersede_pending` today, so it needs that spec operation or a cancel plus a new seal.

Part 4's table (design:971-988) has no row for this.

**Proposed change:** one rule in sdmxlib's `correct`. An entry the restored leaf holds as embargoed and the head holds as embargoed or withdrawn keeps the head's copy. Add one test row and one conformance check. **Acceptance:** none removed; it strengthens "keeps ... the head's embargo setting" (#98:107).

### ZO-2. `statcat upload` on real Azure is not specified and has no test

**Target:** design (omitted scope). **Citations:** #98:57; design:161 (CUJ 1 runs it with its workload identity); design:775 ("builds the location from the environment"; no variable is named); design:376 (`azure-identity` stays the caller's dependency); design:390-395 (no `TokenIdentity` and no key means refusal); design:534-536 (a string URI is refused "so no credentials come silently from the environment"); design:926-936 (no CLI row).

**Failure scenario:** the CLI is the caller, so it must build a `TokenIdentity` from a credential. The `blob` extra installs no credential library, so CUJ 1's command has no way to get its token and refuses. Only the Python API `upload_edition` is designed and tested. Line 536 also gives a principle that line 775 breaks.

**Proposed change:** name the environment variables. Have the CLI wrap `obstore.auth.azure.AzureCredentialProvider` (F15) in `TokenIdentity`. Either add `azure-identity` to `blob` or raise a named `ImportError`. Add a `CliRunner` row (the existing pattern, `cli/EGM.md:27`). Reconcile line 536 with line 775. **Acceptance:** Part 2 scope, which the acceptance items check only through upload's effects.

### ZO-3. The OAuth container exists for one test row and does not remove the need for a real-Azure run

**Target:** design. **Citations:** design:405-411 (second Azurite, TLS, `docker cp`, `trustme`, token minter); design:921-922; design:933 (the only row using it); design:247-248 and F19 (Azurite does not check the signature or a real token); design:1050-1051 (the manual pass already covers the bearer `HEAD`); statcatlib `pyproject.toml:118` (`pytest-httpx` is already a dev dependency).

**Cost:** a new dev dependency, certificate generation, a copy-in step, a token minter, a TLS client option, and a further container on every push (design:865 says "more", with no number). All of it proves that `Authorization: Bearer` is accepted by an emulator that does not validate the token. The MD5 read-back itself is covered on the plain container (design:931, 934). Design:400 rejects `sign` because the production path would run first on real Azure, and that stays true for a real token under the chosen option too.

**Proposed change:** keep the bearer branch in `src`, since it is the only production path. Test it in the unit tier with `pytest-httpx` (headers, `x-ms-version`, MD5 parsing, 404). Drop `trustme`, the container and the minter. Move the account-key path out of `src` into a test subclass in `tests/stores/_stores.py`: the design itself says no deployment uses an emulator path (design:393, 401), and `src`'s refusal message should not offer an account key as an equal choice. Move the real-account checklist to Part 2: design:860 puts it in Part 1's PR, `AzureBlobRest` arrives in Part 2 (design:919), and the checklist includes it (design:1051). **Acceptance:** none. "`just ci` passes" gets faster.

### ZO-4. `to_read`, `prefetch`, `read_many` and the writer's kept reader have no caller at the scale that justifies them

**Target:** design (D5). **Citations:**

- design:483-485: rules on blob run only in admin calls, there is no seal, the fold is Phase 5, and the corpus uses `try_commit`.
- design:497-498: "Part 5's run records the real ratio".
- design:994-998: Part 5 uses hashed placeholder manifests and runs no rule, so no manifest read can be measured.
- F17: the measured gain is about 3x (1.35 s to 0.43 s) on a CPU-bound emulator, against the "about 60 times" estimate at design:503.
- design:505: GH-125 part 4 is placed in Phase 5.
- design:885: `read_many` is listed and defined nowhere else. No `src` caller appears in the design. Only Part 5's bench needs concurrent leaf reads (design:1010).
- design:331-335: the kept `FullInventory` exists "when its storage says to", but the protocol (design:286-297) has no such member. The stated pressure, five calls a seal, does not occur on blob (design:110).

**Cost:** a second statement of `_Rules`' conditions that must track them, with its own test (design:509-511, 913); a thread pool; 91 MB peak; an undefined storage attribute. All of it is justified by a 7,000-product cold `release` that nothing runs until Phase 5, where the owner already expects `artefacts` reads to change the picture.

**Proposed change:** answer question 5 with `ManifestArtefacts(fetch)`, sequential, and state the 63 s estimate as a Phase 5 input to measure. Drop `to_read`, `prefetch`, `MANIFEST_READERS`, `read_many` from `src`, and the kept reader. Keep the 64-thread leaf case in `bench/`. **Acceptance:** none. Question 5 is still answered.

## Minor

### ZO-5. Part 4's tests all run a second time on Azurite on every push

**Target:** design. **Citations:** design:966-967 ("Each also runs once over Azurite", 18 rows at design:971-988); #98:83; #98:107-110 (no acceptance item names a store); design:865 ("about 20 s", from sdmxlib's figures for a different tier); `bench/corpus-results.md:112` (a cold seal is at least 715 ms).

**Cost (reviewer's estimate, unmeasured):** 18 rows at a local seal, an upload, a commit and a pull each add tens of seconds to every push. Those rows test rule logic that the storage seam makes store-independent.

**Proposed change:** keep the rows local, and add one Azurite smoke test each for `correct` and `reinstate`. Replace the 20 s figure with a measurement in Part 1's PR. **Acceptance:** none.

### ZO-6. SeaweedFS and D4's S3 branch

**Target:** design. **Citations:** design:902; design:451; W3; design:817 (`record_uploaded` calls upload, which refuses S3, design:781-782); design:252-253; #98:99 (acceptance names Azurite only); #98:46 (names `S3Store`); hub `inventory-history-log.md:477` (no server expiry on S3).

**Cost:** one container on every push, and an endpoint branch. An S3 catalog can hold no real edition in Phase 4.

**Proposed change:** keep the writer store-agnostic, which costs nothing because the leaf rule gives one code path. Remove SeaweedFS from the `just ci` gate until a deployment on S3 exists. #98:46 still holds, since `S3Store` is still accepted. W3 is unchanged.

### ZO-7. The `exclusive()` seam and `_commit_locked`

**Target:** design. **Citations:** design:297; design:358 ("Nothing here is built now") against design:362 and 887 (built in Part 1); hub `inventory-history-log.md:432`, 453-458.

**Cost:** a no-op protocol method on two backends and a split of `try_commit` with no behaviour change. The only caller of the split is a restore in GH-125. The `with storage.exclusive()` line reads as a held lock that does not exist. Line 358 and Part 1 contradict each other.

**Proposed change:** drop both. Keep the stage-2 table as prose. Adding the method in GH-125 costs the same then as now, and the injected seam already makes it one method per backend. **Acceptance:** none.

### ZO-8. `record_uploaded` on the public wheel, with an unsafe default

**Target:** design. **Citations:** design:811-814; design:827-828; plan:348 (Phase 5 starts once Phase 4 is done); #98:83 (no drill before Phase 9); design:994 (the bench does not use it).

**Cost:** the reasons for shipping it (sdmxlib-rest Phase 6 tests, bench scripts, drills) do not hold. Phase 6 follows Phase 5's real fold, and the bench uses `inventory_corpus`. A public helper becomes a removal once the fold ships.

**Separate defect:** `embargoed: bool = False` takes the state from the caller, while seals follow the catalog's embargo setting (`CLAUDE.md:404`). A default call on an embargo-on catalog records the edition live.

**Proposed change:** move it to `tests/stores/_stores.py`. Wherever it lives, derive the state from the head's `embargo` and drop the `embargoed` argument. **Acceptance:** none.

### ZO-9. The sdmxlib coordination is justified in one place

**Target:** design sequencing and spec bookkeeping.

**Justified:** S10. `admin.py` adds no rule of its own (`src/statcatlib/EGM.md:133`), and rules belong to sdmxlib (plan:155).

**By item:** only S6 and S10 change sdmxlib code in Phase 4. S5 is statcatlib-only by its own label. S4, S9 and S11 do not block Phase 4 (design:148, 153, 155), so file them as issues. S1 concerns the fold, so it belongs to Phase 5. S8 is stale against the branch under review: spec:427 and spec:519 already say `correct`. S2, S3 and S7 are one prose PR on `docs/phase-4-restated`.

**Smaller first step:** Part 1 asks for S6 (design:881) and Part 4 for S10 (design:961). Design:671's "the two can ship together" cannot happen while O4 is open and Part 1 goes first. Move `CatalogAdmin.open(BlobLocation)`, `CatalogAdmin.create` and `ManifestArtefacts(fetch)` to Part 4. Part 1's acceptance items run through `try_commit` and the conformance backend. Blob catalogs in fixtures get `catalog.json` from `InventoryWriter.write_catalog_json` (`inventory.py:395`) and record with `try_commit`, as the corpus does (design:485), or pass the existing `artefacts=` injection (`inventory.py:300`). Parts 1 to 3 then land on sdmxlib 0.85 with no sdmxlib release, and one release carries S6 and S10. **Acceptance:** none. The part order and "Parts 1 to 4 can release separately" stand.

### ZO-10. `BlobLocation` carries fields most of its users ignore

**Target:** design. **Citations:** design:519-526; design:429 (upload needs no actor); design:525 and 538 (`cache_dir` matters only to `Catalog.open`); design:574 (rejects the keyword form for the same fault); S2 at design:146.

**Cost:** the public `actor` field is what creates spec question S2. Its only Phase 4 users are emulator-backed tests and key-based stores.

**Proposed change:** pass `actor` to `BlobInventoryStorage` or `CatalogAdmin.open`, so readers and upload never see it. Keep D3's resolution order. **Acceptance:** none.

### ZO-11. A cancelled edition can go live before its release time through `reinstate` and `rollback`, independent of `correct`

**Target:** spec and sdmxlib code. **Citations:** rules:135 and 362 (`cancel` clears `release_at`); rules:439-462 (`retire` accepts withdrawn); rules:512-524 (`reinstate` writes `superseded`); spec:517-518; rules:422-427 and spec:523 (`to=None` takes the newest superseded edition); rules:121 (id order).

**Scenario:** cancel, retire by the prune job, reinstate during a drill. A later routine `rollback` with no `to` then picks the reinstated edition if its id is the newest, so an operator publishes it without ever naming it.

**Proposed change:** state in the operator guide and in a §6.4 note that a reinstated edition becomes the default rollback target. Consider having `CatalogAdmin.rollback` show its chosen target in the error or log. No format change.

### ZO-12. Two `httpx` routes to the same store

**Target:** design. **Citations:** design:449-452; design:385-395; F18 (a bearer `HEAD` returns `Date`); design:242-243 and 862 (the anonymous `HEAD` is unconfirmed behind a private endpoint, with an unbuilt fallback). The alternatives table at design:475-479 never weighs a bearer `HEAD`.

**Cost:** an extra code path, plus a fallback named and not designed. The anonymous option's gain, "every store", matters only for S3 (ZO-6).

**Proposed change:** compare the options in D4's table. If a token exists, read `Date` from `AzureBlobRest.head("catalog.json")`, which removes one item from the unconfirmed list. This would need `AzureBlobRest` to move to Part 1, and the key path would need the test subclass from ZO-3.

### ZO-13. Two small gaps against GH-98 and the CUJs

**Target:** design (omitted scope). #98:77 says `correct` raises a clear error for "pruned, expired or invalid". Design:632-633 words only "pruned or never written". Add "expired" to the message, or say why stage 1 cannot see an expired root. CUJ 3 (design:190) says `CatalogAdmin.editions` shows "version 39 good". `editions` reads the newest version only (`src/statcatlib/EGM.md:111-114`), and no API reads an old root. Either state how an operator picks `from_seq` (reading `changes` by hand is fine until Phase 9) or correct the CUJ.

## The named pieces, one by one

- `record_uploaded` on the wheel: move to tests (ZO-8).
- `exclusive()`, `_commit_locked`: drop (ZO-7). `storage.stage_2`: already unbuilt, keep as prose.
- Bearer branch: keep; it is the production path. OAuth container: drop (ZO-3).
- Account-key branch: move to a test subclass (ZO-3).
- S3 writer support: keep it agnostic, at no cost. SeaweedFS: remove from the gate (ZO-6).
- `BlobLocation.actor`: relocate, and keep the capability (ZO-10). D3's declared actor is needed so Azurite can write.
- D5's prefetch, `to_read`: drop (ZO-4). `ManifestArtefacts(fetch)`: keep, moved to Part 4 (ZO-9).
- `CatalogAdmin.create` on blob: move to Part 4 (ZO-9). Phase 5 will need it.
- Blob `Product.view`: keep. Acceptance #98:105-106 requires it.
- `correct` and `reinstate`: keep, as #98:107-110 requires. Reduce only the Azurite duplication (ZO-5).

## Tiers and the owner's constraint

A local tier fits the Actions-minutes constraint, since hosted CI runs no tests
(`CLAUDE.md:732-735`). W6 is settled and the reviewer found no defect it causes. The hook
has no path filter (`.githooks/pre-push:17`), so every push including a docs-only one needs
Docker. That is W6's stated cost (design:269), so it is no finding. The tier's size is the
problem: three containers (design:405, 893) and duplicated rows. ZO-3, ZO-5 and ZO-6 bring
it down to one Azurite container, which is all the acceptance list requires.

## O4 assessment

**Recommendation:** option 1, with three additions. Option 2 should go. A fail-closed
alternative the table misses is worth the owner's look.

**Why option 1 fits first use.** In Phase 4 no release job and no fold exist, and only tests
record an embargoed entry. Phase 9's drill is the first real use (#98:83). Option 1 costs one
branch in the rule and no format change. It leaves option 2 available later, because optional
fields are keyed on presence (plan:85). The design's check holds: `_fast_forward` refuses a
kept entry whose `previous` was dropped (rules:155-175).

**Three additions option 1 needs:**

1. ZO-1's rule. Better, generalise option 1 to one sentence: "An entry embargoed in the restored leaf or in the head keeps the head's copy when the head holds it embargoed or withdrawn." This also covers a rescheduled `release_at`. No statcatlib API reschedules today, but spec:426 and `conformance.py:630` contemplate it, and a restored copy would carry the earlier time.
2. Make the hazard visible. `correct` returns an int (design:624), so a kept wrong entry is silent, and "the operator must look at what remains" (design:130) depends on memory. Log a WARNING naming each kept edition and its `release_at`, and add a test row. Without this, the correction is run to remove a bad seal, the bad edition is embargoed and kept, its `previous` is live, and it publishes at T.
3. Fix the spec. Spec:519 at `011312c3` says "any state | tombstone | correct". Option 1 contradicts that row, so "no code beyond the rule" (design:130) omits a spec edit. W2's text then needs the reading "kept is not dropped". #98:107 survives that reading, and the owner should state it.

**Option 2: reject.** It is the most expensive option: format, rule, readers and conformance.
It protects against two deliberate operator acts (`reinstate`, then `rollback`), while one
deliberate act already publishes any embargoed edition at once: `CatalogAdmin.release` uses
`explicit=True` (`src/statcatlib/admin.py:415`). It is incomplete: `cancel` already clears
`release_at` (rules:135, 362), so a cancelled entry reaches the tombstone with nothing to
carry, and ZO-11's route stays open.

**Option 3.** Its stated cost is overstated, and its real cost is different. "Blocks recovery"
amounts to one `cancel` call per entry. The real cost is that `cancel` has no way back to
embargoed (spec:507-518), so each good embargoed edition must be re-sealed, and a dated release
can be missed. It does force the operator to look, which is its one merit.

**Option 4, missing from the table.** `correct` applies sdmxlib's `cancel` to each embargoed
entry recorded after `from_seq`. No format change, about one line, and an existing transition.
The fold still skips the edition because a leaf records it. It fails closed: nothing wrong
publishes and nothing publishes early. The cost is the same as option 3's: a good edition misses
its time and needs a new seal, and `release_at` is lost. The choice between options 1 and 4 is
the owner's ranking of harms. Option 1 can publish a wrong edition on schedule. Option 4 can miss
a correct edition's schedule. The reviewer's default is option 1 with the WARNING, because a
correction is usually aimed at one bad version, and option 4 would cancel every unrelated release
in the window. An agency that ranks wrong publication far above lateness should pick option 4.

**Further embargo paths the options miss:**

- Restored embargoed, head withdrawn: automatic publication (ZO-1). This is the important one.
- Withdrawn after `cancel`, then tombstoned by `correct` or the prune job, then `reinstate` and `rollback`, including through the default rollback target (ZO-11).
- A restored copy with an earlier `release_at` than the head's (addition 1). No current API reaches it.
- `record_uploaded` defaulting to live on an embargo-on catalog (ZO-8).
- `cache_dir` after a re-embargo (design:613) gives no route, because `view` reads `LiveInventory`. The design records this, and it is no finding.

## Incomplete or unverified

- F1 to F22 and all `obstore` or Azure behaviour: not independently verified.
- ZO-5's timing is an estimate, not a measurement.
- ZO-1's `supersede_pending` variant comes from reading the code. It was not run.

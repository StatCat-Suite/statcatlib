# Codebase details report, instance B: sdmxlib, spec, plan and issue claims

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`. `spec` is sdmxlib `docs/design/sharded-bundle-catalog.md` at `6a646b75`, `plan` its
`sharded-bundle-catalog-plan.md`, and `_rules.py`, `_routing.py`, `_format.py`, `_reader.py` and
`_check.py` the sdmxlib modules at the same commit. Instance A's report on statcatlib claims is
`codebase_details_report.md`.

## Verdict

Revise before approval. The design's sdmxlib, spec, plan and issue claims mostly match the sources.
Every plan Phase 5 gating test is mapped. O1 to O7, and S1, S12 and S14 as resolved by sdmxlib#381,
are stated correctly, and no recorded owner decision conflicts with the design. The reviewer
reported one Blocker, five Majors and seventeen Minors. The compiler confirmed CB-101 in the code
and lowered it to Major in `review_report.md`, since the fix is one call; this file keeps the
reviewer's severity.

## What I read

- The persona `agents/codebase_details.md` of the EGM plugin.
- The target, all 1045 lines.
- `_rules.py`, `_routing.py`, `_format.py`, `_reader.py` and `_check.py`, in full.
- The spec §6 to §6.9 and §9 to §11.6; the plan, lines 1 to 379.
- The issue texts for statcatlib GH-52, GH-125 and GH-134, sdmxlib#381 and the suite tracker.
- The hub's `inventory-history-log.md`, lines 325 to 554, the decision table H5 to H24, and the risk
  rows.

## Limits

- The sdmxlib sources of `CANONICAL_FORM`, `decode_manifest` (beyond its call `decode_manifest(raw,
  path)`), `verify_remote`, `pull_edition` and `sdmxlib.edition.seal` were not supplied; claims
  about them are unverified.
- statcatlib symbols (`Change`, `MAX_RACES`, `current_is_older`, `ManifestArtefacts`,
  `_refuse_unsafe_files`, every `store/*.py`, `admin.py` and `bundle.py` line) are instance A's.
- The texts of sdmxlib#380, #382, #383, #384 and #385 were not supplied.
- Azure ARM, Microsoft Graph and Kubernetes API behaviour were not checked. Nothing was run.

## Blocker

### CB-101. The release step reads every leaf in the calling thread

**Sites:** design:398, 836 (NFR "Release of 200 | 7,003 leaves (pool)"), 842; `_reader.py:31-36`,
`:304-322`, `:356-366`; GH-134 item 9, run 3.

**Failure:** D4 builds its input as `release_due({p: full.leaf(p) for p in full.products()}, now)`.
`FullInventory.leaf` calls `_leaf(ref)`, which fetches in the calling thread unless the leaf is
held. Only `leaves()` goes through `_products_read()` and `_read_leaves`, the 64-thread pool. GH-134
measured the one-thread read at 51.12 s for 7,003 GETs. The step as written takes about 51 s on the
real account, where the NFR table says 1 s on the pool, and the design itself says "The release
step's time is what readers wait for after the release instant."

Compiler's check: `InventoryWriter.commit` builds a new reader on every attempt (`full =
self.full()`, `store/inventory.py:743`, which returns `FullInventory(self._storage.read).refresh()`
at `:512`), so no earlier read holds the leaves.

**Fix:** Use `full.leaves()` in D4, and say in D2 and D4 that every whole-catalog read goes through
`leaves()`. Add a request-count assertion to Part 3's stores test that the release step fetches its
leaves on the pool.

## Major

### CB-102. A second release run writes `CURRENT` before it takes the lease

**Sites:** design:269-270, 279, 1006; spec §6.9 (lines 691, 705-707); suite design lines 458-460;
GH-125 part 3 and its acceptance.

**Failure:** Step 0 ends with "Advance `CURRENT` to the head (spec §6.6)", and step 1 then takes the
`HISTORY` lease. On an opted-in catalog a second run writes `CURRENT` before it learns the lease is
held. Spec §6.9 says a run "holds the lease for its whole run; a second run fails to take it and
writes nothing", and expiry step 0 advances `CURRENT` "under the catalog lock". Part 6's own test
says "A second release run fails to take the `HISTORY` lease and writes nothing". D1 gives no
outcome for a held lease, and neither the Errors table nor the exit codes cover it.

**Fix:** On an opted-in catalog take the lease before any write, and move the `CURRENT` advance after
step 1. Add an Errors row for a lease held by another run, with its exit code and event.

### CB-103. The `final` check has gaps

**Sites:** design:317-318; spec §6.5 items 5 and 6 (lines 548-549); `_rules.py:13-15`, `:166-175`.

**Failure:** D2 checks item 5 only "For a `final` product with a `previous`", against "the previous
edition's manifest". The spec's item 5 covers "a new edition of a `final` product", whatever its
`previous`. sdmxlib's fast-forward accepts `previous: null` when the product has no live edition at
that schema, "which is how a schema rollout's first edition is built (§9.2)". A schema-bump edition
of a `final` product with `previous: null` passes D2 with changed content. D2's `chain` row also
accepts a `previous` that `retired` holds; a tombstone carries no `manifest_sha256`, and GC may have
deleted that manifest, so the `final` row is undefined there.

**Fix:** Compare a `final` product's incoming `content_fingerprint` with a recorded edition's, for
example the live edition at any schema, whether or not `previous` is set. State the outcome for a
tombstoned `previous`. Add both cases to the `final` test (design:953).

### CB-104. `CatalogAdmin`'s 60 s lease wait, and the emergency stop

**Sites:** design:146, 415-417, 422, 673-674; spec §6.9 (line 691); suite design lines 461-462,
478-480, and its risk row at line 796.

**Failure:** D11 says each commit on an opted-in catalog takes the lease, "waiting up to 60 s for a
release run's lease to end". The spec says "a `CatalogAdmin` call under the release job's identity
waits for the run", and the suite design bounds that wait by the 30-minute cap "so a hung holder
cannot block an emergency withdraw or rollback for longer". With 60 s, `statcat admin embargo on`
during a run longer than 60 s fails. While the run holds the lease no `set_embargo(on=True)` lands
at all, so D4's "a concurrent `set_embargo(on=True)` therefore turns the retry into a held run" and
Q1's "one stop switch" hold only on a catalog that has not opted in.

**Fix:** Make D11 wait for the run, up to the 30-minute cap, as the spec says, or raise the 60 s as a
spec question with an S number. State in D4 and Q1 that on an opted-in catalog the stop takes effect
at the next run, and how an operator stops a run already holding the lease. Name the catalog state
the unit test covers.

### CB-105. The 200-table release test lacks grouped products and the stub pre-materialisation

**Sites:** design:835, 939-940, 946, 1018; plan lines 106 and 177; GH-52 comment of 2026-11-02.

**Failure:** The plan says "Phase 5 tests a release of 200 tables that mixes both shapes: one-table
products, and grouped products where each per-product refusal of `release_due` holds back several
tables", and tests the job with "the fold, a stub pre-materialisation job and the release version".
GH-52 says "Pre-materialisation is a separate job (stubbed in tests)". The design's fixture "uploads
N synthetic tables", its NFR row reads "Fold of 200 one-table registrations", and no test names
grouped products, a refusal in one, or a stub pre-materialisation step.

**Fix:** Build the plan's mix in the Part 3 fixture, one-table products plus grouped products of 5
to 20 tables. Put one refusal in a grouped product and assert that all its tables stay embargoed.
Run a stub pre-materialisation between the two runs through `EmbargoReader`, or name the test that
does.

### CB-106. The GC registered-edition test can pass without testing the rule

**Sites:** design:974, 1029; GH-52 comment of 2026-11-05.

**Failure:** GH-52 specifies "a registration older than the grace period with GC run before the
fold: the files survive, and the fold's edition verifies". The design's row never ages the files
past `upload_grace`. Files younger than the grace period survive on the grace rule alone, so the
test passes without exercising the registration rule.

**Fix:** State that every data file of the registered edition is older than `upload_grace`, by a
fake clock or a backdated store. Add the converse: the same files with no registration are deleted.

## Minor

### CB-107. The rule name for `release_due` refusals is inconsistent

**Sites:** design:207-209, 285-286, 413-414, 439-441; `_rules.py:323-347`.

**Failure:** CUJ 2 says the step "refuses it with `fast-forward`" and the record "names the edition,
the rule and the message". D4 and D5 say `release_due`'s refusals have no rule, which matches
`_rules.py:323-347`: it returns `dict[str, str]` keyed `<product_id>/<edition_id>` with `str(exc)`.
The exit line `REFUSED <product>/<edition> <rule>: <reason>` has no form for a null rule.

**Fix:** Align CUJ 2 with D5 and define the line for a null rule, or call `release` per due edition
so the step keeps `TransitionError.rule`; `release_due` is a 25-line loop over `due` and `release`.

### CB-108. The registration document check is loose

**Sites:** design:310; spec §11.2 step 1 (line 987).

**Failure:** "Two keys in D7's order" points at this design's D7, the jobs configuration. The byte
format is spec §11.2 step 1, which also requires "UTF-8 compact JSON", "UTC, `Z`, milliseconds" and
"Equal input gives equal bytes". D2 does not say whether the fold refuses non-compact bytes or a
`+00:00` offset.

**Fix:** Cite "spec §11.2 step 1 (Phase 4 D7)". Have the fold re-encode the decoded document and
refuse with `pending-document` when the bytes differ, which covers order, compact form, `Z` and
milliseconds in one rule.

### CB-109. The `canonical_form` check is narrower than the spec

**Sites:** design:314; spec §6.5 item 3 (line 542), §9.1 (line 920).

**Failure:** The spec says "one the release job knows", and §9.1 says "known values". D2 requires
equality with `sdmxlib.edition.CANONICAL_FORM`, a constant not in the supplied files.

**Fix:** State why equality with this build's form suffices (§9.2 pins one image for the publish and
release jobs), or check membership in the forms the build reads. Confirm the constant is exported.

### CB-110. The reference check with no other structure product

**Sites:** design:319-330, 835; `_check.py:269-271`.

**Failure:** For a catalog's first structure product, `structures=` is empty, and `check_edition`
raises `ValueError("structures= names no edition …")` for an empty sequence. The fold's cost row
leaves out the pulls of the other structure editions.

**Fix:** Pass `structures=None` when the list is empty, with a test. Add the pulls to the cost row.

### CB-111. `EmbargoReader`'s open check

**Sites:** design:704-709, 992; `_check.py:324-333`; plan line 359.

**Failure:** `check_on_open` requires keyword `stamps=` and takes no `structures=`. D12 names
`cache_dir` and leaves out the stamps directory, though a stamp names embargoed editions and belongs
inside the §10.1 boundary. D12 does not say `references=False`, without which a structure edition
whose references resolve elsewhere fails at open. "No attribute of `Catalog` returns one" comes from
plan line 359, and the design cites spec §6.4.

**Fix:** Add a stamps directory beside `cache_dir` with the same boundary note, state
`references=False` as Phase 4's `view` does, and cite plan line 359.

### CB-112. `record` overwrites the leaf's lifecycle

**Sites:** design:132-134, 371-379; `_rules.py:268-274`.

**Failure:** `record` builds `Leaf(..., lifecycle=lifecycle, ...)`, so any value passed replaces the
recorded one with no refusal. D3's "yes | different | refused" row is the only guard on a server.

**Fix:** State it in D3, and test that the fold refuses a mismatched section `lifecycle` before
calling `record`.

### CB-113. S18 is partly in spec §6.6 already

**Sites:** design:146, 164, 423; spec §6.6 Retry (line 579).

**Failure:** Spec §6.6 says "A stale automatic release must not write `live` after a concurrent change
turned embargo on", which implies `embargo: true` holds automatic releases. D4's option "The release
step ignores the flag | No spec change" conflicts with §6.6 and the plan's gating test.

**Fix:** Cite §6.6 Retry in Q1 and S18 as the text S18 makes explicit, and mark that option as
contrary to §6.6.

### CB-114. The event kind and history cost of a record-only version

**Sites:** design:445-447, 459; spec §6.2 `event` (line 374) and `changes` (line 377);
`_format.py:486-519`.

**Failure:** A version that changes only `extensions.statcat.release` is written as kind `release`
with an empty change list. Spec §6.2 reserves `describe` for "a change to the catalog description in
`extensions.statcat` and nothing else". Every `SegmentLine` keeps `extensions`, so up to 150 KB a
root sits in `history/` for good, under object lock.

**Fix:** Choose the kind deliberately or raise it with S19, and add the segment growth to D5's cost
and the Risks table.

### CB-115. The scope of the 30-minute lease cap

**Sites:** design:718, 768; spec §6.9 (line 694); GH-125 part 3.

**Failure:** The spec and GH-125 say "A run holds the lease for at most 30 minutes, then releases it
and alerts". D13 "stops expiry at 30 minutes", while the lease is taken at step 1 for the whole run.

**Fix:** State that the cap runs from the acquisition in step 1, and what a run does when the cap
passes during steps 3 to 5.

### CB-116. The GC identity's rights and the allowlist example

**Sites:** design:520-524, 610-611; spec §10.1 (lines 950-951, 958).

**Failure:** D10 says GC has "delete on `products/` and `pending/` (spec §10.1)". Steps 3 to 5 read
registrations and manifests and LIST both prefixes, so GC needs read and list there too. §10.1 says
the allowlist names "every principal in the table above that holds such a role"; D7's example lists
only the three jobs, so the pods, the search pods and the preview deployment are missing, and a
deployment copying it fails the check on every run.

**Fix:** State GC's role as read plus delete on `products/` and `pending/`, as an S row if §10.1's
table changes. Complete the example allowlist.

### CB-117. Alert coverage

**Sites:** design:739-755; spec §11.6 (line 1032), §6.6 (line 596); plan line 360.

**Failure:** Three spec alerts are unmapped: storage 503s (left to `obstore` retries with no event);
a root that fails validation (the reader records skipped roots, `_reader.py:28-29`); and an uploaded
edition with no `pending/` registration, distinct from sealed-but-unrecorded. The plan's
"discard-or-reinstate decision" has a discard path (`--discard`) and no reinstate path.

**Fix:** Add events for `FullInventory.skipped` and 503 bursts, or state why each is out of scope.
Split the unregistered-upload row and give its runbook (rerun the upload, §11.4).

### CB-118. GH-125 part 4's mechanism, and the header's scope

**Sites:** design:6, 466-479, 493-495, 802; GH-125 part 4.

**Failure:** GH-125 part 4 says "A leaf-backed `Artefacts` reader becomes `InventoryWriter`'s default
and replaces `ManifestArtefacts(root)`". D6 rejects that option without saying the issue specified
it. The header claims part 4 and "the server half of GH-125 part 3", while the design's GH-125 table
also gives Part 6 two pieces of part 2: the Azure lease behind `exclusive()`, and opt-in over a
`BlobLocation`.

**Fix:** Record the departure and plan an issue edit. Add part 2's server pieces to the header.

### CB-119. Decision dates and sources

**Sites:** design:8-9, 26, 233-236, 487.

**Failure:** O8 is dated 2026-11-10 and listed as an owner decision; the no-summary decision is the
owner's of 2026-11-06 (plan line 346, spec §6.5), and GH-134 item 9 met its condition. D6's "the
smaller corpus" is not stated in GH-134. The header names sdmxlib#381 and #383, while the suite
tracker names #382 for the §11.6 grace period that O7 rests on. O7's "with no timeout check" has no
source in the supplied texts.

**Fix:** Date O8 2026-11-06 and cite GH-134 as the condition met. Drop or source "the smaller
corpus". Name sdmxlib#382 as O7's source.

### CB-120. `retirable` has two signatures

**Sites:** design:598, 786.

**Failure:** `retirable(full, cutoff, *, kept)` in D9, `(full, cutoff, *, kept, skipped=())` in
Symbols.

**Fix:** One signature, with `skipped` defined.

### CB-121. The expiry storage interface has no ranged read

**Sites:** design:726-729; spec §6.2 `created_at` (line 369), §6.9 step 1; GH-125 part 3.

**Failure:** GH-125 step 1 says "ranged GET of the first 1 KiB for `created_at`", and the interface
lists `read(path) -> (bytes, etag)` only.

**Fix:** Add a ranged read, or state that Part 6 reads whole roots in step 1 and give the cost.

### CB-122. A bad registration blocks GC with no operator remedy

**Sites:** design:233, 624-627, 637-639.

**Failure:** A registration whose manifest names another edition fails its product closed on every GC
run until the tombstone window (400 days by default), and only with Q6. The release job is read-only
on `pending/`, and `--discard` covers only an unregistered edition. O5 records an unregistered
edition as "an alert only", and `--discard` deletes one.

**Fix:** Let `--discard` also name a registration, as an operator's decision. Say in S22 how
`--discard` relates to O5's "alert only".

### CB-123. The plan's end of P7 is missing

**Sites:** design:63; plan line 358.

**Failure:** The plan says "The Phase 2a design's P7 (keep roots, leaves and tombstones) ends with
stage 2". The design cites P7 for local GC and does not say when it ends.

**Fix:** State that P7 ends with Part 6 on Azure and with GH-125's local stage 2.

## Plan Phase 5 coverage

| Plan line | Bullet or gating test | Design site | Verdict |
|---|---|---|---|
| 350 | Start conditions; the design settles shape, retention schema, refusal storage | Problem, D1, D7, D5 | Covered |
| 352 | Fold: one `embargoed` version; forks refused; `supersede_pending`; too-old; read-only `pending/`; skip recorded; GC removes folded | D2, D10 step 5 | Covered |
| 353 | Release through `release_due`, `Date` clock, skew refusal, structure first, refusals persisted, batch goes on | D1 step 0, D4, D5 | Covered; leaf read cost wrong (CB-101) |
| 354 | Invariants 2, 3, 4, 5, 6, with 7 and 8 | D2, D4 | Partial: item 5 (CB-103), item 3 (CB-109) |
| 355 | Artefact records in leaves; rules read leaves | D6, Part 1 | Covered; mechanism departs from GH-125 (CB-118) |
| 356 | Access precondition, replaceable listing | D8 | Covered; allowlist example (CB-116) |
| 357 | Release job writes `retire`; local prune unchanged | D9 | Covered |
| 358 | GC rules, pending protection, fail closed, grace, dry run, log, no version, stop at `expired_through` | D10 | Covered; GC rights (CB-116) |
| 358 | Stage 2: opt-in, expiry after release, segments, lock and lease, gates, Azure-only | D13, Part 6 | Covered with CB-102, CB-104, CB-115, CB-121 |
| 358 | P7 ends with stage 2 | none | Missing (CB-123) |
| 358 | `results/` GC is Phase 6's | Non-Goals | Covered |
| 359 | `EmbargoReader`, own engine, no `Catalog` attribute | D12, Part 5 | Covered (CB-111) |
| 360 | Unrecorded sweep with discard-or-reinstate; `advance-current`; alert events | D10, D11, D14 | Partial: reinstate path and alert rows (CB-117) |
| 106, 177 | 200-table release mixes grouped products; stub pre-materialisation | Part 3 fixture | Missing (CB-105) |
| 362 | 200-table release in one version | design:946, map 1018 | Mapped; fixture incomplete (CB-105) |
| 363 | Fold runs `check_edition(at="receive", remote=, structures=)` | design:947, map 1019 | Mapped (CB-110 edge case) |
| 364 | Interrupted release stays embargoed, re-run releases | design:948, map 1020 | Mapped |
| 365 | Two pending on one live, and a too-old one, refused | design:949, map 1021 | Mapped |
| 366 | Stale automatic release after embargo on is refused | design:950, map 1022 | Mapped; holds only on a catalog that has not opted in (CB-104) |
| 367 | Non-fast-forward refused naming both; rest live | design:951, map 1023 | Mapped |
| 368 | Data edition with structure not due stays embargoed | design:952, map 1024 | Mapped |
| 369 | `final` product with different `content_fingerprint` refused | design:953, map 1025 | Mapped; `previous: null` case missing (CB-103) |
| 370 | Clock ahead refused | design:954, map 1026 | Mapped |
| 371 | Principal outside allowlist stops release | design:955, map 1027 | Mapped; the map names only `tests/test_access.py`, and the "stops the release" half is in `tests/test_release_job.py` |
| 372 | GC exact; racing upload; rolled-back-to edition | design:972-973, map 1028 | Mapped |
| 373 | GC keeps `pending/` files; fails closed | design:974-975, map 1029 | Mapped; test not aged past grace (CB-106) |
| 374 | GC refuses backup above GC window | design:976, map 1030 | Mapped |
| 375 | `advance-current` forward only | design:927, map 1031 | Mapped |
| 376 | Re-run of each job changes nothing | design:956, 977, map 1032 | Mapped |

## Claims verified

| Claim (design site) | Source | Verdict |
|---|---|---|
| `record` requires `lifecycle` (91) | `_rules.py:178-196` | Correct; it also relabels the leaf (CB-112) |
| `record` refuses a `kind` change with `kind` (339-340) | `_rules.py:241-243` | Correct |
| `release_due` refusals are messages (413) | `_rules.py:323-347` | Correct; keyed `<product_id>/<edition_id>` |
| The `fast-forward` message names both editions (414, 434) | `_rules.py:160-164` | Correct |
| `supersede_pending(leaf, edition_id, by=, now=)` (341-343) | `_rules.py:365-389` | Correct |
| `retire` per product returns tombstones (599-600) | `_rules.py:439-462` | Correct |
| `reinstate(artefacts=)` at `_rules.py:465-531` (469) | `_rules.py:465-524` | Correct; range overshoots by 7 lines |
| `verify_entry` compares a filled `artefacts` (480-481) | `_routing.py:131-149` | Correct |
| `routing_fields`, `manifest_records` (339, 907) | `_routing.py:66-83` | Correct |
| `decode_manifest` (313, 476) | `_routing.py:28`, `:128` | Exists, `(raw, path)` |
| `CANONICAL_FORM` (314) | not supplied | Unverified (CB-109) |
| `check_edition(at="receive", remote=, lookup=, structures=, extensions=)` (319) | `_check.py:87-99` | Correct; empty `structures` raises (CB-110) |
| `check_on_open` in the `EmbargoReader` view (707) | `_check.py:324-333` | Exists; needs `stamps=` (CB-111) |
| `FullInventory`'s 64-thread pool (305) | `_reader.py:87`, `:304-322` | Correct for `leaves()` only (CB-101) |
| `LeafEntry.artefacts`, `Tombstone.retired_at`, `state_changed_at`, `HISTORY.expired_through` | `_format.py:178-189`, `:375-378`, `:427-440` | Correct |
| Root `extensions` carried forward; segment lines keep `extensions` (448, 459) | `_rules.py:765-767`; `_format.py:486-519` | Correct (cost in CB-114) |
| Leaf entry 665 to 2,579 bytes; Azurite 6.6 MB and 20.0 MB (485-486) | spec lines 436, 560 | Correct |
| 0.94 s at 64 threads in-region (24, 487, 830) | GH-134 item 9 | Correct; "smaller corpus" unstated (CB-119) |
| About 6,300 manifest GETs a cold commit (98) | plan line 88; GH-125 part 4 | Correct |
| §6.4 skew 30 s and storage clock; §6.5 items as cited; §6.6 Retry | spec lines 529, 541-552, 579 | Correct; item 3 narrowed (CB-109); §6.6 implies S18 (CB-113) |
| §6.9 lease timings, `If-Match`, gates, leaf-set cache, Azure-only (718-724) | spec lines 691-713 | Correct; cap scope, wait and step order (CB-102, CB-104, CB-115) |
| §10.1 rights as cited (610) | spec lines 950-956 | Correct as cited; GC also needs read (CB-116) |
| §11.2 steps 1, 2 and 4; §11.4; §11.6 inequalities | spec lines 987-1026 | Correct; registration bytes partly (CB-108); alerts partly (CB-117) |
| O1 to O6 | suite tracker; sdmxlib#381 | Correct |
| O7 | spec §11.6; sdmxlib#382 | Default correct; "no timeout check" unsourced (CB-119) |
| O8 | GH-134; plan line 346 | Content correct; date and attribution (CB-119) |
| S1, S12, S14 resolved by sdmxlib#381; S13 left to this design | sdmxlib#381 | Correct |
| GH-125 acceptance split (806-811) | GH-125 acceptance | Correct; the Part 1 quote is verbatim |
| Suite design: `CatalogAdmin` waits for the run | suite design lines 461-462 | Contradicted by D11 (CB-104) |
| 1.7 to 4.7 GB first run without the cache (169, 869) | suite design line 524; GH-125 part 3 | Correct |

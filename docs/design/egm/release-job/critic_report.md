# Design critic report: the release job and garbage collection, revision 1

**Target:** `docs/design/release-job.md`, revision 1, whole document. Line references are
`design:N`; `spec` is sdmxlib `docs/design/sharded-bundle-catalog.md` at `6a646b75`.

## Verdict

Not ready to accept, with two Blockers. In the first, the fork rule and the lack of any path that
removes a refused registration can block a product's publication for up to 400 days. In the second,
an Inventory restore re-arms registrations that were already folded, and the fold then records and
releases them automatically, cancelled and rolled-back editions included. Four Major findings cover
release-time robustness (a transient fault in the fold or the access listing holds every due
release), the emergency stop on opted-in catalogs, a past `release_at` under Q5, and anonymous
container access. Much of the design holds up under the code and the spec: the commit retry
semantics behind Q1, the fold's idempotency through O1, GC's read order, the window inequalities,
the refusal record's survival across `describe`, and D3's section key. No finding contradicts an
owner decision. Three need spec or suite-design changes the design does not list (DES-1, DES-2,
DES-8).

## What I read

- The persona `agents/design_critic.md` and `feature-spec-standard.md` of the EGM plugin.
- The target, all 1,045 lines.
- statcatlib `CLAUDE.md`, `store/EGM.md`; `store/inventory.py` (`Change`, `_no_manifest_reader`,
  `full`, `try_commit`, `commit`), `store/cross_product.py` (readers, `incoming`, `_Rules`,
  `refusals`), `store/working_copy.py` (`recorded_routing`, `precheck_refusals`, the lifecycle
  refusal), `admin.py` (`_retirable`, `release`, `cancel`, `rollback`, `set_embargo`, `describe`),
  `bundle.py` (`_section_findings`), `upload.py` (send, verify, `_register`); greps of
  `store/blob.py`, `catalog.py`, `diagnostics.py` and the Phase 4 design.
- sdmxlib copies: the spec §6 to §6.9, §9, §10, §11; plan Phase 5; `_rules.py`, `_reader.py`,
  `_routing.py`; the issue texts by grep.
- The hub's `inventory-history-log.md`: gates, lease, risks.
- `edition_files` in the sdmxlib 0.87 installed in statcatlib's virtual environment
  (`sdmxlib/edition/_copy.py:106`).

## Limits

- Nothing was run, and there was no git.
- The Phase 4 design in full, its reports, `docs/catalog-format.md`, `_format.py` and `_check.py`
  were not read.
- Azure behaviour in DES-6 and DES-13 is from general knowledge and unverified: container public
  access levels, `allowBlobPublicAccess`, RBAC denials from the Kubernetes API.
- `CatalogAdmin.export(live_only=True)` does not exist in statcatlib (no `live_only` or `def
  export`), so DES-8 concerns a contract the spec states and the code has not built.

## Blocker

### DES-1. A permanent `pending-fork` refusal with no way to remove a registration

**Sites:** design:332-336, 151, 352-353, 634-639; spec §11.2 step 2 (line 988), §11.4 (line 1008),
§10.1 (line 956); `_rules.py:365-389`; `upload.py:257-269`.

**Failure:** At 06:00 the publish job uploads edition B1 of product 14100287 on live edition L. A
corrected B2, also on L, is uploaded at 07:30. Both are unfolded at the 08:00 pre-flight, so the fold
refuses both with `pending-fork`. Neither registration can leave `pending/`:

- the release job has read-only access (O1, §10.1);
- GC deletes only registrations whose edition the head records (design:634);
- Q6 deletes only those older than the tombstone window (400 d);
- `--discard` covers only unregistered editions (design:638-639).

The producer rebuilds B3 from L as CUJ 2 advises, giving three registrations on one `previous`, and
the fold refuses all three. Every edition a working copy can build is built on L, so the product
cannot publish until 400 days pass (with Q6) or ever (without it). Each run exits 3 and logs
`fold.refused` the whole time. A registration refused for `references` or `full-artefacts` that
later passes becomes a fork partner of the producer's fixed rebuild the same way. The design does not
say whether the fork rule counts registrations that failed a per-registration check.

**Fix:** Give an operator a path that removes a registration, for example `statcat gc --discard
PRODUCT/EDITION` accepting a registered, unrecorded edition: delete the registration, then the
manifest, then the directory, logged as deletions. List it as a spec change to §11.2 step 2 and §11.4
beside S20 and S22. State which registrations the fork rule counts. Extend Q6 to refusals other than
age. Add a gating test: after a fork is refused and one registration discarded, the other folds.

### DES-2. An Inventory restore re-arms folded registrations, and the fold releases withdrawn editions

**Sites:** design:311 (the O1 skip), 294-295 and 150 (Q5), 634, 656; spec §6.9 restores (lines
719-728, step 4 deletes the roots above S) and line 638 (the head chain skips the roots a restore
discarded), §10.3 (line 970), §6.7 (line 623, a broad scan re-releases withdrawn editions);
`_rules.py:299-306`.

**Failure:**

1. Edition E of product P is folded at seq S+3 and cancelled (`withdrawn`) at S+5 for a
   confidentiality problem.
2. A failover restores the catalog to S. The restore removes roots S+1 to S+5 and writes S+1 of the
   new epoch.
3. E's registration is still in `pending/`, since GC deletes a registration only when the head
   records its edition (design:634), and the new head does not.
4. The next release run checks E. It is neither recorded nor tombstoned (design:311), not too old,
   its manifest and files are intact (protected as a registered edition), and its `previous`, L, is
   live at S.
5. The fold records E `embargoed` with its old, past `release_at`. Step 4 releases it in the same run
   (Q5), and `_fast_forward` passes because L is live.

The withdrawn edition goes public. The same path republishes every edition rolled back after S, and
re-folds every registration folded after S and not yet removed, up to 35 days of folds. Design:656
("an edition a restore discarded ... stays protected while its root is in the window") is wrong as
written: GC walks roots by `seq` from the new head, and the restore removed the discarded roots.
Those editions are protected only by their registrations, which is the mechanism that re-arms them.
This holds unless the restore's root S+1 tombstones the dropped entries, which neither this design
nor GH-125's restore text states.

**Fix:** Make the fold restore-aware and list the change against spec §11.2 step 2 and §6.9. Either
record a registration created before the first root of the current `restore_epoch`, whose edition
the head does not record, as `embargoed` with `release_at: null` and log `fold.restored`, so only an
explicit release or `reinstate` publishes it; or refuse it until an operator decides, with a restore
runbook step that clears `pending/` through DES-1's discard path. Correct design:656. Add a gating
test: restore to S, run the release job, and no edition cancelled after S goes live.

## Major

### DES-3. A transient fault in the fold or the access listing holds every due release

**Sites:** design:269-276, 282-286, 288-292, 313, 345-350, 580-583, 766.

**Failure:** At 08:30:05 the fold reads a manifest and a non-404 error raises `BlobStoreError` (only a
404 returns `None` through `AzureBlobRest`), or `writer.refusals` raises `ManifestError` for another
product's unfilled entry (`store/inventory.py:479`). Step 3 stops the run with exit 1, and step 4
never runs. The 200 editions folded at 08:00 stay embargoed, and the schedule (design:288-292) gives
no further run until the next release time. A 429 from ARM at 08:30 holds the same releases through
step 2: a listing error fails the check, which skips step 4. A burst of late registrations also
delays every folded release by the fold's time, about 35 s per 200 (design:835).

**Fix:** Make a per-registration store fault a refusal (`store`) and keep the batch going. Let a
failed fold leave step 4 to run over the head. In the release-time run, release recorded entries
before the fold, then fold, then release again for Q5's case. Retry the access listing within the
run, or reuse a passing pre-flight result for a bounded time. Require the orchestrator to re-run on
exit 1 until the next release time, and state it in D1's schedule.

### DES-4. The run's `HISTORY` lease blocks the emergency stop and manual changes

**Sites:** design:146, 272, 415-417, 673-674 (a 60 s wait), 718 (the 30-minute cap); hub
`inventory-history-log.md:458-462`, `:796`; spec line 691.

**Failure:** On an opted-in catalog the 08:30:05 run takes the lease at step 1. At 08:30:20 an
operator runs `statcat admin embargo URL on`, which waits up to 60 s. The fold and the release step
finish under the lease, so the stop cannot land before step 4 commits. The run then expires under the
same lease for up to 30 minutes, and the admin call fails after 60 s. A rollback or cancel at 08:31
also fails for up to 30 minutes. D4's argument that a concurrent `set_embargo` turns the retry into a
held run holds only on catalogs that have not opted in. The hub design says a `CatalogAdmin` call
"waits for the run", which D11's 60 s contradicts for any run longer than a minute.

**Fix:** Choose and state the behaviour, for example running expiry only in a run with no release
time (a nightly expiry-only run or a configuration switch), so release-time runs hold the lease for
seconds. Size the admin wait to the longest release-time run, have the admin call print the holder
and the break runbook when it gives up, and state in Q1 that the stop lands only before step 1 of a
run on an opted-in catalog.

### DES-5. A past `release_at` is published at the next run with no guard (Q5)

**Sites:** design:150, 294-295, 310, 337; `upload.py:249-254`; spec line 987.

**Failure:** A publish job uploads 200 tables at 06:00 with `--release-at 2025-11-12T13:30:00Z`, a
wrong year. The 08:00 pre-flight folds them and, under Q5, releases them in the same run, 30 minutes
before the announced 08:30. The fold checks only that `release_at` is `null` or a millisecond UTC
instant, and nothing compares it with the registration's creation time or the storage clock. Q5's
trade-off covers a late upload and never a mistyped or stale `release_at`, the case that turns Q5
into early publication. Upload refuses a changed `release_at` on rerun (`upload.py:265-268`), so a
producer who spots the mistake after upload cannot correct it.

**Fix:** Add the case to Q5. One rule records a registration whose `release_at` is earlier than its
last-modified time, or earlier than the fold's storage clock by more than a configured bound, as
`embargoed` with `release_at: null`, logged `fold.release_at_past`, so only `statcat admin release`
publishes it. Add the test.

### DES-6. The access check misses anonymous reads of the container

**Sites:** design:563-583; spec line 958.

**Failure:** An administrator sets the container's public access level to `blob`, which Azure allows
while the account's `allowBlobPublicAccess` is true. Every embargoed manifest and data file is then
readable by URL without a principal. D8 lists role assignments, eligible assignments, group members
and `allowSharedKeyAccess`, so the check passes and step 4 releases on schedule. (Azure behaviour
unverified here.)

**Fix:** Read the account's `allowBlobPublicAccess` and the container's `publicAccess` through ARM
and fail the check unless public access is off. Add a Risks row for object replication out of the
account, since a replica has its own readers.

## Minor

### DES-7. Gaps in persisting the refusal record

**Sites:** design:273, 439-447, 580-583, 117.

**Failure:** When the access check fails, step 4 is skipped and the fold's refusals are never written,
against Goal 3. The 500-entry cap keeps entries by `product_id`, so the highest product ids drop
whatever their age, and a dropped entry loses its `since`. A record-only version takes `event.kind:
release` with empty `changes`; spec §6.2 defines `describe` for an `extensions.statcat`-only change,
and no S row covers this use. Fold refusals persist until their registrations go (DES-1), so the
record and exit 3 stay up indefinitely.

**Fix:** Write the record in a skipped or held step 4 too. Cap by `since`, newest kept, or keep every
product's oldest entry. Write record-only versions as `describe` or add the use of `release` to S19.
Tie the record's lifetime to DES-1's discard.

### DES-8. The refusal record carries embargoed details into history segments and a live-only export

**Sites:** design:451-455, 459; spec line 678 (segment lines keep `extensions`), line 962
(live-only export).

**Failure:** A refusal's `reason` names embargoed edition ids, URNs and fingerprints (design:434).
Inside §10.1 nothing new crosses, but segment lines keep `extensions` for good, and the live-only
export is defined to carry only transitions into or out of `live`. If that export copies
`extensions.statcat` for the description, the record leaks embargoed detail into the copy meant for
analytics tools. The design lists no rule for the export.

**Fix:** Add an S row requiring the live-only export to drop `extensions.statcat.release`, and state
that segments keep the record inside the boundary.

### DES-9. The fork rule ignores `schema_version`

**Sites:** design:335-336, 341-343; `_rules.py:383`; spec §9.2 (line 931).

**Failure:** During a schema rollout, a republish at schema 2.53 built on L and the daily edition at
2.52 built on L share a `previous`, and the fold refuses both as a fork, though they target different
heads. `supersede_pending` compares `previous` and `schema_version`.

**Fix:** Key the fork rule on `(previous, schema_version)` and raise the §11.2 step 2 wording as an S
row.

### DES-10. GC ignores the newest K roots

**Sites:** design:616-621; spec §11.6 (line 1025).

**Failure:** A catalog publishing monthly writes fewer than 10 roots in 35 days. GC reads only the head
when it is older than the cutoff (design:617), and deletes directories of editions that roots head-1
to head-9 name, the reader fallback's reach.

**Fix:** In GC step 2, read at least the newest `keep_roots` roots whatever their age.

### DES-11. GC's rules for registrations and manifest-less directories

**Sites:** design:634-639; spec lines 1010, 1032.

**Failure:** "Whose edition the head records" does not say whether `retired` counts; O1 says it does.
An upload that died before its manifest leaves `metadata.duckdb` and `tables.ducklake` with no
manifest, and `gc.unregistered` requires a manifest, so the directory is never reported or deleted.

**Fix:** Write "in a leaf or in `retired`". Report a manifest-less directory older than
`upload_grace` as `gc.unregistered`, and allow `--discard` for it.

### DES-12. D4 reads leaves sequentially, and each commit attempt starts cold

**Sites:** design:398, 836-837; `_reader.py:356-366`; `store/inventory.py:505-512`, `:742-744`.

**Failure:** `{p: full.leaf(p) for p in full.products()}` runs before the rules call `leaves()`, so it
fetches 7,000 leaves in sequence. Each step and each lost race also re-reads every leaf cold, since
`commit` builds a new `FullInventory` per attempt. `_retirable` calls `full.entries(p)` per product
(`admin.py:178-184`) with the same cost.

**Fix:** Call `full.leaves()` first in each callable, and let `InventoryWriter.full()` refresh one held
`FullInventory` so leaves are cached by hash across steps and races. Record the counts in Part 3's
stores test.

### DES-13. Gate 2 can fail open on an RBAC denial

**Sites:** design:722; hub `inventory-history-log.md:364-365`.

**Failure:** "With no API, gate 1 alone applies" does not separate an unreachable API from a 403 on
listing pods. A missing Role binding would quietly drop the pod check, and expiry would run past old
pods.

**Fix:** Apply "not run" only outside a cluster (no service-account token), and treat every API error
inside a cluster as a gate failure.

### DES-14. Manual release on blob skips item 2 and records the job's identity as the actor

**Sites:** design:666-687; `admin.py:384-428`; `store/EGM.md` (`actor()` takes the `TokenIdentity`
oid first).

**Failure:** `statcat admin release` over blob commits with no SHA-256 comparison (§6.5 item 2) and no
access check, and under the release identity `event.actor` records the job's oid for every operator
action.

**Fix:** Have the blob `release` and `rollback` verify the incoming manifest as D4 does. Record the
operator in `event.reason`, or give operators their own identity with write on `inventory/` (a §10.1
row).

### DES-15. Open points in the D2 fold

**Sites:** design:310, 318, 341-346.

**Failure:** The `final` check reads "the previous edition's manifest, checked against its leaf's
`manifest_sha256`", and a tombstoned `previous` has no leaf entry and may have lost its manifest to
GC. "Drops each refused product's new entries" does not say that dropping B also reverts the
`supersede_pending` withdrawal of A, which would otherwise leave A withdrawn with no replacement.
"Two keys in D7's order" should cite spec §11.2 step 1.

**Fix:** Refuse with `final` for a tombstoned `previous`, naming the edition. Drop a refused product's
whole leaf change. Correct the citation.

### DES-16. The allowlist example omits the pods and the preview deployment

**Sites:** design:520-524; spec line 958.

**Failure:** The pods and the preview deployment hold blob-read roles. An operator copying the example
fails the check on every run.

**Fix:** List every §10.1 principal that holds a read role.

### DES-17. D6 departs from GH-125 part 4's body without saying so

**Sites:** design:466-479, 494; GH-125 part 4.

**Failure:** GH-125 part 4 specifies a leaf-backed `Artefacts` reader replacing `ManifestArtefacts` as
the writer's default. D6 reads records inside `cross_product`, and no handoff row updates the issue.

**Fix:** Add a handoff row that updates GH-125 part 4's body to D6.

## Sound under review

- **Q1 retry semantics.** `commit` re-reads the head and re-applies the change after each conflict
  (`store/inventory.py:742-762`), so a `set_embargo(on=True)` during a release commit on a catalog
  that has not opted in yields a held run.
- **Withdrawn and cancelled editions.** A cancel or rollback landing during the release commit is
  re-evaluated: `release` requires `embargoed` and the fast-forward check (`_rules.py:299-306`). A
  withdrawn edition cannot be released outside DES-2's restore path.
- **Fold idempotency.** The O1 skip makes a re-run fold nothing new, and `record` refuses a
  `duplicate` or `tombstone` anyway (`_rules.py:244-249`).
- **Refusal record under `describe`.** `Change.extensions=None` keeps the head's sections
  (`store/inventory.py:269`), and `describe` copies the section's other keys (`admin.py:508-514`).
- **GC read order.** Inventory, then registrations, then LIST protects an edition folded between the
  reads. `inventory_window >= gc_window + 1d` keeps GC's walk clear of root expiry.
- **GC tiers.** `edition_files` lists all three tiers by default (installed `_copy.py:106`); the
  design should still pass `tiers` explicitly.
- **Per-product fail-closed.** `_refuse_unsafe_files` and `edition_files` refuse a `data_path` that
  leaves the product directory.
- **Window inequalities.** D7's match spec §11.6.
- **D3.** `_section_findings` ignores unknown keys (`bundle.py:353-377`), and `record` relabels the
  leaf with the lifecycle passed (`_rules.py:268-275`), so the leaf-first rule keeps a recorded
  product's value.
- **D6.** `verify_entry` compares a filled `artefacts` with `manifest_records` (`_routing.py:131-149`),
  and `verify-catalog` calls it (`diagnostics.py:286-296`).
- **Gate 2 threshold.** The 0.85 floor matches the stage 1 release.
- **D9 under rollback.** Retirement recomputed inside the commit callable cannot retire an edition a
  concurrent rollback made live.
- **Owner decisions.** No decision in D1 to D14 contradicts O1 to O8.

Status: one resolution round done (2026-11-02). Findings that need the owner are Open Questions 2 to 8 in the design.

# EGM review of the Phase 3 design

**Target:** [phase-3-structure-products.md](../../phase-3-structure-products.md),
first draft at commit `6604707`.

**Read against:** statcatlib `41d4b88`, sdmxlib `d52010c2` (`v0.83.0`), the
hub at `2fce9f2`.

**Reviewers:** five fresh-context reviewers, run in parallel, each given the
design, the suite specification and plan, sdmxlib's open-builder design and
code, and statcatlib's code:

| Reviewer | Verdict | Blocker | Major | Minor or Nit |
|---|---|---|---|---|
| `egm-design` | not ready | 2 | 7 | 6 |
| `egm-implementation-readiness` | not ready | 1 | 8 | 7 |
| `egm-codebase-details` | citations hold; one structural correction | 0 | 4 | 8 |
| `egm-zoom-out` | right scope, one slice too much | 0 | 1 | 9 |
| `egm-style` | 14 problems | 0 | 10 | 4 |

Each finding below has a disposition: **Fixed** (the design now says what the
finding asked, or an equivalent), **Escalated** (an Open Question for the
owner), or **Declined** (with the reason). Findings two reviewers raised are
listed once.

## Blockers

### B1. The journal repair skipped the publish check (design, readiness)

A crash between sdmxlib's seal and the publish check left a journal entry,
and the repair recorded the edition with no check. **Fixed:** the repair runs
`_publish_check` on each journalled structure edition and each journalled
edition holding a partial, and clears the entry with a WARNING on a finding
(The journal repair). S1 tests the crash.

### B2. A crash mid-batch could strand the batch's data editions (design)

The repair settled one product at a time, so a table's journalled edition was
refused before its structure edition's repair recorded it, and its entry was
cleared. **Fixed:** `settle_journal` settles every product's journal entries
in one change that runs the batch's rules. S3 tests a crashed batch.

### B3. The batch order did not fit `_seal_each` (readiness, codebase details, design)

`_seal_each` (`catalog.py:174`) pre-checks every working copy before any
seal, so a data pre-check could not see the batch's structure editions, and a
structure check depended on the caller's list order. `EditionCheckError` was
not caught. **Fixed:** three passes (structure seal, structure check repeated
until it drops none, data), `batch` parameters on `_precheck` and
`_publish_check`, the exceptions `_seal_each` catches, and a dependency drop
in the Inventory commit (Batch order). CUJ 1 now holds in any order.

## Major

| # | Reviewer | Finding | Disposition |
|---|---|---|---|
| M1 | design | Every codelist `fill_edition` cuts (unit, status, SDMX-agency) needs a structure home or is refused, and the generator would not show it | **Fixed and escalated:** D11 states the policy, the generator homes `CL_UNIT`, S3 tests an unhomed cut; Open Question 5 offers the alternatives |
| M2 | design | Partials with no `partial_of` pass ungated; a direct `fill_edition` makes `seal` carry a false `partial_of`; the fill and the merge were not atomic | **Fixed:** the `fill_in_progress` marker and the pre-check in "Partials the seal cannot vouch for". Partials from a source that held `isPartial` codelists keep Phase 2a's behaviour, so `ingest_sdmx` callers are unaffected |
| M3 | design | Data products sharing a full artefact with no home deadlock under item 4, as in D7 | **Escalated:** the rule row is removed; Non-Goals and Open Question 4 |
| M4 | design | Item 7 counted embargoed editions, which blocks moving an artefact between homes under embargo | **Fixed:** D10 follows the specification (live editions, on entry to `live`); the move procedure is in Item 7 and Non-Goals; Open Question 7 |
| M5 | design | `verify-catalog` gave structure editions superseded editions, hiding item 9 gaps; a pruned source fails a stale data edition's check | **Fixed:** structure editions get live and embargoed editions of other structure products; `prune` keeps a cited structure edition |
| M6 | design | The manifest reader did not verify `manifest_sha256` | **Fixed:** `ManifestArtefacts` hashes before parsing and caches by `(edition_id, manifest_sha256)` |
| M7 | readiness | `EditionArtefacts` undefined | **Fixed:** replaced by sdmxlib's `PartialRecord` (also codebase details M2); `Artefacts` reader type defined |
| M8 | readiness | The reader's behaviour on a missing manifest, and `tests/test_inventory_writer.py`'s manifest-less leaves | **Fixed:** `ManifestError` propagates; `InventoryWriter(root, *, artefacts=)` for the tests; item 17 |
| M9 | readiness | `CrossProductRuleError` had no constructor, and the second edition when no holder matches was unstated | **Fixed:** signature and `editions` rule given; export listed |
| M10 | readiness | How the batch and the repair call the rules; `record_journalled`'s tuple has no kind | **Fixed:** `InventoryWriter.refusals`; the four-element tuple |
| M11 | readiness | A data edition's `structures=` could be `None`, skipping the partial check (also zoom-out M9) | **Fixed:** every live and embargoed structure edition is passed; with none, `EditionCheckError` |
| M12 | readiness | Two tests could not be written as described; the conformance bullet proved nothing | **Fixed:** the extra-code test edits the partial after the fill; the `structures` seam is a `monkeypatch` wrapper; the conformance bullet is reworded |
| M13 | readiness, design | The rollback text named item 8 where the table gives item 4's partial row | **Fixed** |
| M14 | readiness | The generator was under-specified for equal fingerprints; `CL_CLASS_3` at 30 codes would stay full | **Fixed:** names, versions, concept ids, measure, levels, fill path, Arrow observations; `CL_CLASS_3` is 300 codes |
| M15 | codebase details | `CodrShape` duplicates `sdmxlib.testing.Shape` | **Declined, with the reason in the design:** `Shape` builds one dataflow per edition under agency `SYNTHETIC`, and its codelist helpers are private |
| M16 | codebase details | The rollback NFR overstated 0.12 compatibility for writes | **Fixed:** read-only under 0.12, and a Risks row |
| M17 | zoom-out | The pre-check was a second implementation of the rules | **Fixed:** the pre-check calls `refusals` with a provisional leaf and `StoreArtefacts` (D5, Alternative F) |

## Minor and Nit

| Reviewer | Finding | Disposition |
|---|---|---|
| zoom-out | Use `at="receive"` for the publish check | **Escalated:** O4 names `publish`; Open Question 2 |
| zoom-out | `doctor`'s stale report and `verify-catalog`'s second-home finding are beyond the plan | **Fixed:** moved to an optional slice S4; `verify-catalog`'s `structures=` stays in S3 |
| zoom-out | Three fill entry points; drop `WorkingCopy.fill_structures` and `fill_dataflows` | **Declined:** `fill_dataflows` is where the map is kept (O3), and a caller may fill without catalog records; `fill_structures` refuses a data working copy before writing |
| zoom-out | Trim `CodrShape` | **Fixed:** languages and the geography share are module constants |
| zoom-out | Deleting `_meta.partial_of` at a new edition is optional | **Declined:** kept, so a sealed store's key names only the edition's own cuts |
| zoom-out | Release cadence and the S2 gap | **Escalated:** Open Question 8 |
| design | Skip the rules when no entry is incoming | **Fixed** |
| design | `kind: Kind \| None = None`, inheriting the recorded kind | **Fixed:** D1 |
| design | D2's claim that the key equals the manifest | **Fixed** |
| design | Measure a census-sized edition | **Fixed:** S2's measurement |
| codebase details | `retire` is `prune`; `cancel` produces no incoming entry | **Fixed** |
| codebase details | Citation hints (`_start`, `record`, `artefact_entries`) | **Fixed** |
| codebase details | `get` takes an `SdmxUrn`; `ingest_dataflows` strings | **Fixed:** both parse with `SdmxUrn.parse` |
| codebase details | `_unrecorded`'s parameter type | **Fixed** |
| codebase details | `check_catalog` could read the manifest | **Declined:** one `structure_content` query serves `check_catalog` and `_precheck`, which has no manifest; the design says so |
| codebase details | `require_working_copy`; why `ingest_dataflows` filters | **Fixed** |
| codebase details | The `DoctorItem` topic list | **Fixed:** in S4 |
| readiness | Name the shared query; `EditionCheckError`'s base class; pick one docs page; `admin.py` `Raises` | **Fixed** |
| readiness | Slice ownership of the batch passes; the sabotage test | **Fixed** |
| readiness | `ingest_dataflows` and `latest` URNs | **Fixed** |
| style | "home" defined two ways; "entering entry"; "stale"; "full copy"; "orphan"; "moves"; "the plan" | **Fixed:** Terms redefines structure home (an edition), incoming entry, stale data edition, unrecorded edition, head, closure; "sdmxlib's plan" throughout; "copies into its edition" for "vendors" |
| style | Colon reveals, negative framing, closing fragments, hard sentences, vague quantities | **Fixed** in the rewritten passages |
| style | "names the test for each goal" | **Fixed:** the map names acceptance criteria |

## Findings not fixed

- **M15** (`CodrShape` from `sdmxlib.testing`): declined, as above.
- **Fewer fill entry points** (zoom-out): declined, as above.
- **Optional `_meta.partial_of` delete** (zoom-out): declined, as above.
- **`check_catalog` from the manifest** (codebase details): declined, as above.
- **Escalated to the owner:** M1 (Open Question 5), M3 (Open Question 4),
  M4 (Open Question 7), the publish profile (Open Question 2), the release
  cadence (Open Question 8).

*Drafted with EGM*

Status: resolved (2026-11-01). Auto-accepted: R1–R12 applied; R13 sentence added; R14 contested; R15–R16 declined.

# Goldfish Annotated Review Report: round 2

**Target:** `docs/design/phase-2a-catalog-editions.md`, the full file as
revised on 2026-11-01.

**Findings source:** Stage 5–8 findings left open after the Stage 9
resolution. They come from [zoom_out_report.md](zoom_out_report.md),
[comprehension_report.md](comprehension_report.md),
[critic_report.md](critic_report.md) and
[intermediate_zoom_out_report.md](intermediate_zoom_out_report.md).
The revision has already resolved the following Stage 5–8 findings, so this
report does not repeat them:

- Stage 5: M2, M3, M5, M6, N1, N3, N6 and N7.
- Stage 6: M1, M2, M4, M5, M9, M10, M11, M12, N1, N4 and N5.
- Stage 7: DES-M6 and DES-M7.
- Stage 8: ZB1, ZM1, ZM2 and ZM3.
- Most of the STR and STY findings.

**Counts:** 0 ESCALATE, 2 CONTEST, 11 PROPOSE FIX, 3 DECLINE.

## Prioritized Findings & AI Suggestions

### [Issue R1] [`egm-design`, `egm-zoom-out`]. Severity: High. The journal has no rule for each kind of entry

*   **Affected Scope:** The WorkingCopy: starting one, sealing, and "A refusal
    after the manifest".
*   **Discrepancy Description:** These are DES-B1, DES-M1 and DES-M2, Stage 6
    M7, and Stage 8 ZB2.
    - `seal` writes the journal entry before compaction and before the
      manifest (`_write.py:564-601`). If the process dies in between,
      `repairable` returns an entry whose edition has no manifest, and step 1
      of the repair fails. `working_copy()` and `resume=True` both run the
      repair first, so the product locks.
    - `read_journal` raises on an empty entry (`_layout.py:174-179`).
    - Suppose another process's repair records an edition that this process
      is still sealing. This process's record then raises `duplicate`, and
      the user is told the seal failed although the edition is live.
    - `InventoryContentionError` keeps the entry, so the edition goes live at
      the next `working_copy()` without notice.
    - The design drops the repair at seal time, which spec §6.7 requires,
      and does not say so.
*   **Rationale:** The product can lock after a crash, and the user can be
    told the opposite of what happened.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** I verified the order of writes in sdmxlib. P6 and
    P7 remove the tombstone-expiry and `correct` paths that made DES-M1
    re-release withdrawn data. Checking the newest root is therefore enough.
*   ***Proposed Edit:***
    *   Replace repair step 1 and the "refusal after the manifest" paragraph
        with one **journal rule**. It applies at `working_copy()`, at resume,
        at the start of `seal`, and in seal step 6. It assumes one writer per
        product, and the design says so:

        | Journal entry | Action |
        |---|---|
        | The newest root names or tombstones the edition | Clear the entry |
        | The edition has no `manifest.json` | Clear the entry; the edition is an ordinary unsealed WorkingCopy that `resume` or `discard` reaches |
        | It has a manifest and no root names it | Record it (`kind: seal`), then clear the entry |
        | Recording raises `duplicate` with the same `manifest_sha256` | Treat it as success and clear the entry |
        | Recording raises any other `InventoryRuleError` | Clear the entry, log a WARNING, and leave the orphan for `doctor` |
        | `InventoryContentionError` | Keep the entry; the error says the edition is recorded at the next `working_copy()` |
        | The entry cannot be read | Raise `JournalError` naming the file and `statcat doctor` |

    *   `test_seal_crash.py` adds three cases:
        - a crash between compaction and the manifest, then
          `working_copy(resume=True).discard()` succeeds;
        - a `duplicate` with matching digests is reported as success;
        - an empty journal entry raises `JournalError`.
    *   Export `JournalError`.

### [Issue R2] [`egm-comprehension`]. Severity: High. No entry point takes the embedding model

*   **Affected Scope:** Catalog, Product and Edition; the WorkingCopy;
    Search.
*   **Discrepancy Description:** This is Stage 6 B1. Today `Catalog.__init__`
    takes `embedding_model`, `document_prefix` and `query_prefix`
    (`store.py:2830-2840`). No new entry point takes them. As a result:
    - seal cannot embed;
    - RFC test D21's count of `encode` calls has nothing to count;
    - `hybrid_search` on an `EditionView` always raises.
*   **Rationale:** An implementer cannot build G10 or semantic search from
    the design.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** I verified the constructor in the code.
*   ***Proposed Edit:***
    *   `working_copy(..., embedding_model=None, document_prefix="",
        query_prefix="")` passes these to its `_Store`.
    *   `Product.view(*, embedding_model=None, query_prefix="")` does the same
        for its view.
    *   Seal embeds only when the WorkingCopy has a model.
    *   If the carried `embeddings.json` names a different model, seal
        re-encodes every text and logs at INFO.
    *   A view with no model serves BM25 `search`. `hybrid_search` keeps
        today's error and names `view(embedding_model=)`.
    *   `test_embedding_reuse.py` covers a model change.

### [Issue R3] [`egm-comprehension`]. Severity: Medium. The seal steps run out of order, and a check has no way to read observations

*   **Affected Scope:** Sealing, steps 1–5; `checks.py`.
*   **Discrepancy Description:** This is Stage 6 M8.
    - Step 5 needs the statcat section, but step 4 has already closed the
      store that `catalog_section` reads.
    - `conf_status` must read observations, but `EditionView` exposes no
      registry.
*   **Rationale:** Neither step can be built as written.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** `LocalRegistry` and `EditionRegistry` both provide
    `obs(flow)`, a lazy observation query (`registry.py:1730`, `576-579`).
*   ***Proposed Edit:***
    *   Insert step 3b, "Compute the statcat section with
        `catalog_section(store)`", before step 4.
    *   Add `EditionView.registry`, the read surface (`get`, `query`, `obs`):
        - for a sealed view, the `EditionRegistry`;
        - for a WorkingCopy's view, the build registry.
    *   `conf_status` reads `view.registry.obs(flow)` for each dataflow that
        has the attribute.

### [Issue R4] [`egm-design`]. Severity: Medium. `view()` never checks the manifest's digest

*   **Affected Scope:** `Product.view()`; the EditionView read path.
*   **Discrepancy Description:** This is DES-M4.
    - Spec §6.3 says a reader verifies `manifest_sha256` before it reads
      anything else.
    - `LocalRegistry.edition` reads the manifest but does not know the
      Inventory's digest.
*   **Rationale:** A replaced or sync-conflicted edition is served silently.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** `LiveEdition.manifest_sha256` exists for this check
    (`_reader.py:76-77`).
*   ***Proposed Edit:***
    *   `view()` hashes `manifest.json` and compares the hash with the head's
        `manifest_sha256` before calling `edition()`. A mismatch raises
        `IncompatibleCatalogError`, naming the edition and
        `statcat verify-catalog`.
    *   An `EditionView` stays on its edition after `refresh()` or a
        rollback, and the docs say so.
    *   File sdmxlib issue 3: `LocalRegistry.edition(..., manifest_sha256=)`,
        since pods need the same check.
    *   `test_edition_view.py` covers a changed manifest.

### [Issue R5] [`egm-design`]. Severity: Medium. `verify-catalog`'s check skips data-file digests

*   **Affected Scope:** Tools; G12; CUJ 5.
*   **Discrepancy Description:** This is DES-M5. Spec §4.6 says the
    `receive` profile checks a `root: data` file by size only. Only
    `publish` checks every recorded digest.
*   **Rationale:** A bit flip in a Parquet file passes `verify-catalog`.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** I verified `sharded-bundle-catalog.md:264`. No new
    pass is needed (Stage 8, contested item 3).
*   ***Proposed Edit:*** Change `at="receive"` to `at="publish"`.
    `test_cli_verify_doctor.py` corrupts a `data/` file and checks that the
    exit code is 1.

### [Issue R6] [`egm-structural`, `egm-design`]. Severity: Medium. The design has no Risks section

*   **Affected Scope:** Non-Functional Requirements.
*   **Discrepancy Description:** These are STR-B1, DES-M8 and Stage 8 ZM7.
    The design does not state that local embargo is not access control, or
    that `event.actor` is unauthenticated, and its failure modes are
    scattered across sections.
*   **Rationale:** The feature-spec standard requires failure modes and
    their mitigations.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** None needed.
*   ***Proposed Edit:*** Add "Risks and limits" to the Non-Functional
    Requirements, as a table with one row per risk:

    | Risk | Mitigation |
    |---|---|
    | On local disk, embargo is enforced by the API only; any reader of the files sees embargoed editions | Stated in `docs/catalog-format.md` (RFC D5/D18 test); storage identity is Phase 4 |
    | `event.actor` is the OS user and host, unauthenticated | Stated; Phase 4 uses the token's object id |
    | Synced folders and network shares are unsupported | `doctor` warns |
    | `CURRENT` lags after a race | Readers probe forward |
    | A late refusal leaves an orphan sealed edition | The journal rule; `doctor` reports it |
    | Power loss before sdmxlib's fsyncs ship | sdmxlib issue 2; `doctor` reports empty files |
    | sdmxlib-rest cannot read 0.12 catalogs until GH-81 ships | P2; `docs/upgrading.md` |
    | A filesystem without hard links (exFAT, some SMB mounts) | `create` probes `os.link` and raises a named error (Issue R9) |

### [Issue R7] [`egm-comprehension`, `egm-zoom-out`]. Severity: Medium. `Edition.previous` has no source, and `kind` and `lifecycle` are checked too late

*   **Affected Scope:** `Product.editions()`; `working_copy(kind=,
    lifecycle=)`.
*   **Discrepancy Description:** These are Stage 6 M3 and M6, and Stage 8
    ZM5.
    - `LiveEdition` has no `previous`.
    - `record()` raises `rule="kind"` only after the manifest is written,
      and silently relabels `lifecycle`.
*   **Rationale:** `editions()` cannot be built as written, and a kind
    mismatch is found too late to refuse cleanly.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** 2a writes data products only.
*   ***Proposed Edit:***
    *   `editions()` reads `previous` from each live edition's manifest.
    *   Remove the `kind=` parameter: 2a writes `kind: "data"` only, and
        structure products move to Phase 4.
    *   `working_copy()` compares `lifecycle` with the existing leaf and
        refuses a change with `InventoryRuleError(rule="lifecycle")` before
        it builds. Changing a product's lifecycle is a Phase 4 operation.

### [Issue R8] [`egm-structural`]. Severity: Medium. No CUJ covers embargo with explicit release

*   **Affected Scope:** CUJs.
*   **Discrepancy Description:** This is STR-M6. P1 is the newest behaviour
    in 2a, and no journey exercises it.
*   **Rationale:** The goals have no end-to-end journey for embargo.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Stage 8 said to add an embargo CUJ only if P1
    stays, and P1 stays.
*   ***Proposed Edit:*** Add CUJ 6:
    *   **Given**: a catalog where `set_embargo(True)` was called.
    *   **When**: a seal, then `Catalog.open().product(p).view()`, then
        `CatalogAdmin.release(p, edition, reason=…)`.
    *   **Then**:
        - before the release, the view raises `NoLiveEditionError` (or
          returns the previous live edition);
        - after the release, the view shows the new edition.

    Map it in `test_catalog_admin.py`.

### [Issue R9] [`egm-design`]. Severity: Low. Exclusive create on a filesystem without hard links

*   **Affected Scope:** The Inventory writer.
*   **Discrepancy Description:** This is DES-N1. `os.link` fails on exFAT
    and on some SMB mounts with `OSError`, not `FileExistsError`. The
    design also does not say who removes leftover `.tmp` files.
*   **Rationale:** The writer would fail with an unnamed OS error.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** This is the smallest of the options in Stage 8
    ZM6. Spec §6.6 already treats network shares as unsupported.
*   ***Proposed Edit:***
    *   `Catalog.create` probes `os.link` in the target directory and raises
        `IncompatibleCatalogError` naming the filesystem.
    *   `doctor` reports leftover `.tmp` files in `inventory/`.

### [Issue R10] [`egm-design`]. Severity: Low. `rollback`'s `schema_version`

*   **Affected Scope:** The CatalogAdmin table.
*   **Discrepancy Description:** This is DES-N4. sdmxlib's `rollback`
    requires a `schema_version`, and the design does not say which one
    statcatlib passes.
*   **Rationale:** An implementer has to guess the value.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** One sentence settles it.
*   ***Proposed Edit:*** "`rollback` passes sdmxlib's `SCHEMA_VERSION`.
    Rolling back the other head during a schema rollout is a Phase 4
    operation."

### [Issue R11] [`egm-design`]. Severity: Low. Prune on Windows

*   **Affected Scope:** Prune.
*   **Discrepancy Description:** This is DES-N5. On Windows, deleting an
    edition directory that another process has attached fails partway
    through.
*   **Rationale:** Prune's report would not show the failure.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** This is a small change.
*   ***Proposed Edit:*** Prune skips a path whose deletion fails, records it
    in `PruneReport.failed`, and continues. A later run retries it.

### [Issue R12] [`egm-zoom-out`]. Severity: Low. A shared `create_exclusive` in sdmxlib

*   **Affected Scope:** Issues filed on sdmxlib.
*   **Discrepancy Description:** This is Stage 5 M4. sdmxlib and statcatlib
    each carry an atomic-write primitive.
*   **Rationale:** Two copies of the same primitive can drift apart.
*   ***AI Suggestion:*** PROPOSE FIX
*   ***Justification:*** Stage 8 says to file the issue and not to gate any
    slice on it.
*   ***Proposed Edit:*** Fold this into sdmxlib issue 2. sdmxlib exposes one
    public `create_exclusive(path, data)` with fsyncs, and uses it for the
    journal and the manifest. statcatlib switches to it when it ships. No
    slice waits on it.

### [Issue R13] [`egm-zoom-out`]. Severity: Low. Seal skips `check_edition(at="publish")` (spec §11.1 step 5)

*   **Affected Scope:** Sealing.
*   **Discrepancy Description:** This is Stage 5 N4.
*   **Rationale:** Spec §11.1 runs `check_edition(at="publish")` after a
    seal.
*   ***AI Suggestion:*** CONTEST
*   ***Justification:*** Spec §11.1 describes the publish job, which uploads
    an edition to a store other readers trust. On a laptop, `seal` itself
    computes every digest it writes into the manifest (`_write.py:573-601`).
    Re-hashing straight away would double the seal's I/O and catch nothing
    new. `verify-catalog` at `publish` (Issue R5) gives the same check on
    demand. The design adds one sentence stating this.
*   ***Proposed Edit:*** Add to Sealing: "Seal runs no
    `check_edition(at="publish")`, because `seal` computes the digests;
    `statcat verify-catalog` runs that check on demand."

### [Issue R14] [`egm-comprehension`]. Severity: Low. Tombstone expiry (Stage 6 M13)

*   **AI Suggestion:** CONTEST
*   **Justification:** P7 expires no tombstones. Expiry would let the repair
    re-release withdrawn data, because `repairable` reads only the newest
    root's tombstones (`_rules.py:536-538`). Spec §6.2 also requires
    tombstones to be kept at least as long as backups. The design's
    Non-Goals already list tombstone expiry under the server GC.

### [Issue R15] [`egm-structural`, `egm-style`]. Severity: Low. Remaining structural and style findings

*   **AI Suggestion:** DECLINE
*   **Justification:** The rewrite already applied:
    - STR-M2, M4, M5 and M8;
    - STR-N2, N5 and N6;
    - most of STY-M1 to M15.

    The remaining items do not affect what gets built:
    - Status wording;
    - bullet markers;
    - the Components diagram's alignment;
    - IDs for the "Settled by this design" bullets (STR-M3);
    - moving the file:line detail out of the Technical Plan (STR-M7).

    A later edit can apply them.

### [Issue R16] [`egm-zoom-out`]. Severity: Low. `discard_unrecorded` issue now (Stage 5 M1)

*   **AI Suggestion:** DECLINE
*   **Justification:** Under the journal rule, the orphan is cleared,
    logged, and reported by `doctor`. The design already lists the sdmxlib
    discard option under Future Improvements.

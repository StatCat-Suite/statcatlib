# Phase 3: structure products and partial codelists

*   **Author:** Gabriel Gellner
*   **Status:** Accepted. The EGM review (five reviewers, one resolution
    round) is in `docs/design/egm/phase-3-structure-products/`. The owner
    answered the open questions on 2026-11-02 (Recorded answers).
*   **Date:** 2026-11-02
*   **Issue:** GH-51. Suite tracker:
    StatCat-Suite/statcat-suite.github.io#19.
*   **Read against:** statcatlib `41d4b88` (main); sdmxlib `d52010c2` (main,
    tag `v0.83.0`); the hub at `2fce9f2`. Every `file:line` below is at these
    commits. Find each site by its symbol and use the number as a hint.
*   **Inputs:**
    *   sdmxlib's [sharded catalog
        specification](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/)
        §6.5 items 4, 7, 8 and 9, §8 and §17 Q8: the authority for every
        format and invariant.
    *   Phase 3 of sdmxlib's [implementation
        plan](https://github.com/StatCat-Suite/sdmxlib/blob/main/docs/design/sharded-bundle-catalog-plan.md)
        ("sdmxlib's plan" below): the scope, the gating tests, and the three
        cases this design settles.
    *   sdmxlib's [open builders and structure
        products](https://github.com/StatCat-Suite/sdmxlib/blob/main/docs/design/open-builder-structures.md)
        design (GH-347, GH-351), shipped in sdmxlib 0.83.0.
    *   The owner's decisions of 2026-11-02, recorded on GH-51 (listed under
        Decisions).
    *   This repository's [Phase 2a design](phase-2a-catalog-editions.md),
        whose seal, writer and batch this design extends.

--------------------------------------------------------------------------------

## Overview

Phase 2a writes data products only: `WorkingCopy` records every edition with
`kind: "data"` in its leaf and in the manifest's `statcat` section, and the
Inventory writer applies sdmxlib's one-product rules. Phase 3 adds the second
product kind and the rules that span products:

- A **structure product** (`kind: "structure"`) holds one agency's full
  codelists, concept schemes, category schemes and hierarchies, with no
  dataflow and no observations. StatCan splits its structure products by
  change rate into `structure-geo`, `structure-classifications` and
  `structure-concepts`. No two structure products hold the same full
  artefact.
- A **data product** copies its dataflows' closures into its edition, with
  each codelist cut to a **partial** that holds the codes the product uses
  and their ancestors. Each partial records `partial_of`, the fingerprint of
  the full codelist it was cut from.
- The seal of a structure edition or of an edition with partials runs
  `sdmxlib.edition.check_edition(at="publish", structures=[...])` against the
  catalog's structure editions before the Inventory commit.
- The **Inventory writer** refuses a version that breaks specification §6.5
  item 4 (full artefacts agree with their structure home), item 7 (one
  structure home per full artefact) or item 8 (a data edition goes live only
  after the structure edition its partials were cut from).
- **`statcatlib.testing`** gains a CODR-shaped generator: three structure
  products and data products of one or more tables, from a seed.

sdmxlib 0.83.0 does the closure walk, the partial cut and the partial check.
statcatlib calls `fill_edition`, `fill_structures` and `check_edition`, and
adds the product kind, the persisted `partial_of` map, the cross-product
rules and the generator. Every file format keeps its version (G1).

## The Problem

Three parts of statcatlib 0.12 at `41d4b88` need changing:

1. **Data products only.** `working_copy.py:81` fixes `_KIND = "data"`, and
   both records use it: `_next_leaf` (`working_copy.py:841`) and the journal
   repair in `record_journalled` (`working_copy.py:194`). `catalog_section`
   writes `"kind": "data"` (`bundle.py:134`). `Product.working_copy`
   (`catalog.py:696`) takes no kind.
2. **Codelists copied whole.** `ingest_sdmx` (`sources/sdmx.py:217`) copies
   every artefact of an in-memory registry into the working copy with
   `wc.registry.ingest`. `_seal_edition` (`working_copy.py:782`) passes no
   `partial_of` to `sdmxlib.edition.seal`. sdmxlib's `fill_edition` returns
   the map for the caller to pass, so a working copy closed and resumed in
   another process loses it (sdmxlib design, open question 2).
3. **Rules read one product.** `InventoryWriter.commit`
   (`store/inventory.py:342`) applies whatever the caller's `change` returns.
   sdmxlib's `inventory/_rules.py` checks one product's leaf, and sdmxlib's
   plan assigns the rules that read several products to statcatlib's
   writer. Today the writer would record a second structure home for a
   codelist, and record a data edition live while its source structure
   edition is embargoed.

## Terms

| Term | Meaning |
|---|---|
| head | The newest valid Inventory root, and the leaves it names. |
| closure | A dataflow and every artefact it references, transitively, plus the constraints and hierarchy associations that reference it (specification §4.4). |
| structure product | A product whose leaf `kind` and `statcat` section `kind` are `"structure"`. Its editions hold full artefacts and no `Dataflow`, `DataflowBinding` or partial. |
| data product | A product of `kind: "data"`, as in Phase 2a. |
| full artefact | An artefact a manifest lists with `partial: false`. |
| partial | A codelist a manifest lists with `partial: true`. The Phase 3 rules read the partials that record `partial_of`. |
| structure home | A live or embargoed edition of a structure product that holds a URN as a full artefact. A URN has homes in at most one structure product, and at most two editions of it (one live, one embargoed). |
| incoming entry | A leaf entry that becomes `live` or `embargoed` in the version being written. A seal's record, a release and a rollback each produce one. |
| stale data edition | A live data edition holding a full artefact or a partial that matches no live structure home, after an edit to that structure. Specification §6.5 item 8 lets such an edition stay live until its product is republished. |
| unrecorded edition | A sealed edition that no Inventory version names. `statcat doctor` reports it. |

## Goals

The acceptance test map names the test for each acceptance criterion; each
slice's test list covers its goals.

*   **G1. sdmxlib 0.83.** `pyproject.toml` requires `sdmxlib>=0.83,<0.84`
    (owner decision O5). `just ci` passes. No catalog is rebuilt: the store
    schema stays 2.52, the manifest 3.0, the Inventory 1.0, statcat
    `SCHEMA_VERSION` `"2.2"` and the search index manifest 3.
*   **G2. The product kind.** `Product.working_copy(kind="structure")` starts
    a structure product, and later calls take the recorded kind. Its edition
    is recorded with `kind: "structure"` in the leaf and in the `statcat`
    section. A product recorded with one kind and reopened with the other is
    refused with `InventoryRuleError(rule="kind")` before anything is built.
*   **G3. Structure products filled by selection.**
    `WorkingCopy.fill_structures` copies full artefacts by family, agency, id
    pattern and URN (`sdmxlib.edition.fill_structures`). A structure working
    copy holding a dataflow, a binding or a partial fails its seal check and
    stays open.
*   **G4. Data products filled with partials.** `WorkingCopy.fill_dataflows`
    adds dataflow closures and observations and cuts codelists
    (`sdmxlib.edition.fill_edition`). The working copy keeps the returned
    `partial_of` map in its store, so a seal after `working_copy(resume=True)`
    in another process records every partial's `partial_of` (O3).
*   **G5. The publish check at seal.** A structure edition is checked with
    the catalog's other live or embargoed structure editions; an edition with
    partials, with the catalog's live or embargoed structure editions (O4).
    A finding leaves the edition sealed and unrecorded, and raises
    `EditionCheckError` naming each finding. The journal repair runs the same
    check.
*   **G6. The cross-product rules.** The Inventory writer refuses a version
    that breaks §6.5 items 4, 7 or 8 for an incoming entry, with
    `CrossProductRuleError` naming the item, the URN and both editions.
*   **G7. Integrity tools.** `statcat verify-catalog` checks each partial
    against the catalog's structure editions, and each structure edition's
    references against the other structure products' live and embargoed
    editions.
*   **G8. The CODR generator.** `statcatlib.testing` builds the three
    structure products and a CODR-shaped data product of a given number of
    tables, from a seed (O6).
*   **G9. Docs.** `docs/upgrading.md`, `docs/catalog-format.md`, the API
    guides, `CLAUDE.md` and the `EGM.md` files describe the new surface.

## Non-Goals

*   **Fetching a structures-only set over SDMX REST.** `fill_structures`
    reads a registry that holds the artefacts. A producer ingests an FMR's
    structures into a scratch `LocalRegistry` first (sdmxlib design,
    Non-goals).
*   **Routing fields** (`full_artefacts`, `dataflows`, `pull_bytes` in the
    leaf): Phase 4, plan Q7.
*   **Writing the leaf's optional `artefacts` field.** RFC decision 25 has
    the rules read manifests; Phase 4 decides the field with the routing
    fields (Recorded answers, A1).
*   **The release job, `release_due` and `supersede_pending`**: Phase 5.
*   **Catalog-level search over structure products** (specification §12):
    Phase 7. A structure edition's seal builds its per-edition search index
    as a data edition's does.
*   **Releases and rollbacks of several products in one version.**
    `CatalogAdmin.release` and `rollback` stay one product per call. Moving a
    full artefact from one structure product to another takes two releases,
    the edition that drops it first (see Item 7).
*   **Moving `ingest_sdmx` onto partials.** `ingest_sdmx` keeps copying an
    in-memory registry whole; `ingest_dataflows` is the partial path.

## Decisions

Owner decisions of 2026-11-02, recorded on GH-51:

| ID | Decision | Consequence |
|---|---|---|
| O1 | StatCan's structure products are split by change rate: `structure-geo`, `structure-classifications`, `structure-concepts`. Each full artefact has exactly one home (invariant 7). | The writer refuses a second home (item 7). The generator builds the three. |
| O2 | A hierarchy goes in the structure product of the codelist it is built on. | The generator puts the geography hierarchy in `structure-geo`. |
| O3 | statcatlib persists the `partial_of` map `fill_edition` returns, so a resumed `WorkingCopy` passes it to `seal`. sdmxlib adds no build-store key. | The map goes in `statcat._meta` (D2). |
| O4 | Sealing a structure product passes the catalog's other live or embargoed structure editions to `check_edition(at="publish", structures=[...])`. sdmxlib treats an edition with no bindings as a structure product. Sealing a data product passes the structure editions its partials were cut from. | The seal gains a publish check (D4). |
| O5 | The pin becomes `sdmxlib>=0.83,<0.84`. | G1. |
| O6 | The CODR generator is written in `statcatlib.testing`, with a seed and a tables-per-product parameter. Phase 4's corpus builds on it. | G8. |

Settled by this design; the owner accepted each on 2026-11-02 (Recorded
answers):

| ID | Decision | Consequence |
|---|---|---|
| D1 | `Product.working_copy(*, kind: Kind \| None = None, ...)`. `None` takes the recorded kind, or `"data"` for a new product. A kind that differs from the leaf, or from `statcat._meta` on resume, is refused before the build. | A structure product's caller passes `kind="structure"` once, on its first working copy. |
| D2 | `statcat._meta` holds two new keys: `product_kind`, and `partial_of` (JSON, URN to fingerprint, the cuts made in this edition). `_meta` is a key-value table, so no DDL changes and `SCHEMA_VERSION` stays `"2.2"`. | A sealed `metadata.duckdb` carries both keys. Readers use the manifest. |
| D3 | `WorkingCopy.fill_dataflows` and `WorkingCopy.fill_structures` wrap sdmxlib's two fill functions. `fill_dataflows` merges the returned map into `_meta.partial_of`, between two writes of a `fill_in_progress` marker. | The seal can tell a cut it knows from one it does not (Partials the seal cannot vouch for). |
| D4 | The publish check runs after sdmxlib's seal and before the Inventory commit, for a structure edition or an edition holding a partial. A data edition with no partial seals as in Phase 2a. | Phase 2a's seal cost is unchanged for data products without partials. A finding leaves the edition unrecorded (Risks). |
| D5 | The cross-product rules live in a new pure module, `store/cross_product.py`. `InventoryWriter.commit` runs them on every planned change with an incoming entry. The seal's pre-check, the batch seal and the journal repair call the same function. | One implementation covers the pre-check, the seal, the batch seal, the journal repair, `release` and `rollback`. |
| D6 | Each accepted version keeps its operation's `event.kind` (`seal`, `release`, `rollback`). A refused version is not written. | sdmxlib is unchanged. |
| D7 | Item 4 treats the structure home as authoritative. An incoming data edition's full artefact must equal a home's fingerprint. An incoming structure edition is accepted when a data edition holds an older copy; that data edition becomes stale. | Settles case C of sdmxlib's plan (Item 4). |
| D8 | An item-8 refusal under embargo off is raised by the pre-check, before sdmxlib's seal, so the working copy stays open. A refusal at the Inventory commit, after another writer changed the head, leaves the edition unrecorded, as a late fast-forward refusal does. | Settles case A of sdmxlib's plan. |
| D9 | `Catalog.seal` seals and checks the batch's structure working copies first, then pre-checks, seals and checks each data working copy against them. The Inventory commit runs the rules on the whole version and drops refused products until a pass drops none. | Settles case B of sdmxlib's plan: a structure edition and its data editions go live in one version. |
| D10 | Item 7 is checked as the specification states it: on entry to `live`, against the other structure products' live editions. | A second home under embargo is refused at its release. |
| D11 | Every codelist `fill_edition` cuts needs a structure home. A partial whose URN no structure product holds is refused by the pre-check, naming the URN and the fix. | A producer puts every cut codelist (unit, status and scale codelists included) in a structure product, or fills with `partials=False`. Unit, status and SDMX-agency codelists get a home, mostly in `structure-classifications` (A5). |

## Critical User Journeys (CUJs)

### CUJ 1: A producer builds the three structure products

*   **Given**: A catalog with embargo off, and a scratch `LocalRegistry`
    holding StatCan's structures ingested from FMR.
*   **When**: The producer opens three working copies with
    `kind="structure"`, fills each with `fill_structures` (the geography
    codelist and its hierarchy; the classification and unit codelists by
    URN; the concept schemes by family), and seals them in one
    `Catalog.seal`, in any order.
*   **Then**: One Inventory version records the three as live, each leaf with
    `kind: "structure"`. `structure-concepts`' concepts name codelists in the
    other two, and its publish check passes because the batch's other
    structure editions are in its `structures=`.

### CUJ 2: A producer builds a CODR table with partials

*   **Given**: The catalog of CUJ 1, and a source registry holding the table's
    dataflow and DSD.
*   **When**: The producer runs `ingest_dataflows(wc, source, [flow],
    observations={...})` on `cat.product("14100287").working_copy()`, closes
    the process, resumes with `working_copy(resume=True)` in a new process,
    and seals.
*   **Then**: The manifest lists each cut codelist as a partial whose
    `partial_of` equals its fingerprint in `structure-geo` or
    `structure-classifications`. The publish check ran with the three
    structure editions.

### CUJ 3: A structure edit under embargo

*   **Given**: The catalog of CUJ 2, with embargo on.
*   **When**: The producer seals a new `structure-geo` edition with an edited
    geography codelist at the same URN, then the republished table cut from
    it, then runs `CatalogAdmin.release` on the table first.
*   **Then**: The table's release is refused with
    `CrossProductRuleError(rule="publish-order")`, naming the geography URN,
    the table's edition and the embargoed `structure-geo` edition. After
    `release("structure-geo", ...)`, the table's release succeeds. Between
    the two releases the live table edition is stale.

### CUJ 4: A second home is refused

*   **Given**: `structure-classifications` holds `CL_NAICS` live, and embargo
    is off.
*   **When**: A producer seals `structure-geo` with `CL_NAICS` in it by
    mistake.
*   **Then**: The pre-check refuses the seal with
    `CrossProductRuleError(rule="structure-home")`, naming `CL_NAICS`, the
    working copy's edition and the live `structure-classifications` edition.
    The working copy stays open.

## Technical Plan (Architecture)

### Components

```text
 statcatlib                                                     sdmxlib 0.83
┌──────────────────────────────────────────────┐
│ Product.working_copy(kind=)                  │
│  └ WorkingCopy                               │
│     ├ fill_structures ───────────────────────┼──▶ edition.fill_structures
│     ├ fill_dataflows ── _meta.partial_of ────┼──▶ edition.fill_edition
│     └ seal                                   │
│        ├ _precheck: kind, structure content, │
│        │   cross_product.refusals (pre-check)│
│        ├ _seal_edition(partial_of=) ─────────┼──▶ edition.seal
│        ├ _publish_check(structures=) ────────┼──▶ edition.check_edition
│        └ InventoryWriter.commit              │
│           └ cross_product.refusals ──────────┼──▶ inventory.record/release/…
│ sources.sdmx.ingest_dataflows                │
│ diagnostics: verify_catalog                  │
│ testing: codr_structures, codr_product       │
└──────────────────────────────────────────────┘
```

### The product kind

`Product.working_copy` gains `kind: Kind | None = None`
(`sdmxlib.inventory.Kind`, `Literal["data", "structure"]`), and
`WorkingCopy` gains a read-only `kind` property. `WorkingCopy._start`:

1. settles the journal, as now;
2. resolves the kind: the leaf's for a recorded product, `kind` or `"data"`
   for a new one. A `kind` that differs from the leaf's raises
   `InventoryRuleError(rule="kind")`, beside the lifecycle check
   (`working_copy.py:392-397`). sdmxlib's `Rule` already holds `"kind"`, and
   `record` raises it at record time (`sdmxlib/inventory/_rules.py:203-205`);
   this check moves the refusal before the build;
3. builds or resumes, as now;
4. on a new edition, writes `_meta.product_kind` and deletes
   `_meta.partial_of` and `_meta.fill_in_progress`, which the copied
   `metadata.duckdb` carries from the previous edition. On a resume,
   compares `_meta.product_kind` with the resolved kind and raises
   `InventoryRuleError(rule="kind")` on a difference, after closing the
   registry. A resumed first edition with no leaf takes
   `_meta.product_kind` when `kind` is `None`.

`_KIND` goes away. `_next_leaf` records `self._kind`. The journal repair reads
each edition's kind from its sealed manifest's `statcat` section
(`extensions["statcat"]["kind"]`, `"data"` when absent): the `entries` tuple
of `record_journalled` becomes `(JournalEntry, sha, schema_version, kind)`.
`catalog_section(store, *, kind: Kind = "data")` writes the kind given.
`PRODUCT_KINDS` and the section format stay as they are: format 3 reserved
`"structure"` (`bundle.py:70`).

A private helper, `structure_content(conn) -> list[Finding]` in `bundle.py`,
reads `sdmx.artefact` (`artefact_type`, `is_partial`) and returns a finding
for each `Dataflow`, `DataflowBinding` and partial artefact. `check_catalog`
runs it on a structure edition (`reg.connection`) beside its binding finding
(`bundle.py:297`), and `_precheck` runs it on a structure working copy's
store. One query serves both, since `_precheck` has no manifest.

### Filling a structure product

```python
def fill_structures(
    self,
    source: sdmxlib.EditionRegistry,
    *,
    families: Collection[type[MaintainableArtefact]] = (),
    agency: str | None = None,
    ids: Collection[str] = (),
    artefacts: Collection[SdmxUrn[MaintainableArtefact] | str] = (),
) -> tuple[str, ...]:
```

It raises `TypeError` on a data working copy, naming
`working_copy(kind="structure")`, then calls
`sdmxlib.edition.fill_structures(self.registry, source, ...)` and returns its
URNs. `source` is any `EditionRegistry`, a `LocalRegistry` included (it
subclasses `EditionRegistry`, `sdmxlib/local/registry.py:2494`). The StatCan
split:

```python
geo.fill_structures(fmr, families=[sl.Codelist, sl.Hierarchy], agency="STC", ids=["SGC*", "HCL_SGC*"])
cls.fill_structures(fmr, artefacts=[naics, noc, unit_of_measure, scalar_factor])
con.fill_structures(fmr, families=[sl.ConceptScheme], agency="STC")
```

`fill_structures` copies only the selected artefacts, and none they
reference (sdmxlib design, `fill_structures`). The geography hierarchy is
therefore selected with its codelist (O2). A concept whose core
representation names a codelist in `structure-classifications` keeps an
open reference, which the publish check resolves (§6.5 item 9).

A structure working copy's `_precheck` runs `structure_content` and raises
`SealCheckError` on any finding. The working copy stays open.

### Filling a data product, and where `partial_of` is kept

```python
def fill_dataflows(
    self,
    source: sdmxlib.RestRegistry | sdmxlib.EditionRegistry,
    dataflows: Sequence[SdmxUrn[Dataflow] | str],
    *,
    observations: Mapping[str, WriteSource] | None = None,
    partials: bool = True,
) -> dict[str, str]:
```

It raises `TypeError` on a structure working copy. It parses each string with
`SdmxUrn.parse`, writes `_meta.fill_in_progress`, calls
`sdmxlib.edition.fill_edition(self.registry, source, dataflows, ...)`, then,
in one transaction, merges the returned map into `_meta.partial_of` (a later
fill's value wins for a URN) and deletes `_meta.fill_in_progress`. It returns
the map. `fill_edition` keeps what the builder held: a republish built on the
live edition keeps the other dataflows' bindings and cuts each shared
codelist to the codes every binding uses (sdmxlib design, `fill_edition`). It
does not cut again a codelist the builder holds as a partial
(`_build.py:383`), so merging with the later value winning is correct.

`_seal_edition` reads `_meta.partial_of` while the store is open, keeps the
URNs the store holds as partials (`sdmx.artefact.is_partial`), and passes the
result as `seal(partial_of=...)`. The filter is needed because a later fill
can replace a partial with the full codelist, and `seal` raises
`ManifestError` for a `partial_of` URN it holds as no partial
(`sdmxlib/edition/_write.py:290`). `seal` takes the previous manifest's value
for each partial the map does not name (`_carried_partials`, `_write.py:640`,
applied at `:292`), so a republish that cuts nothing keeps every partial's
`partial_of`.

The key stays in the sealed `metadata.duckdb`. It can name a URN the
manifest records as full; the manifest is the record. A `ManifestError` from
sdmxlib's seal closes the working copy with the key intact, so
`working_copy(resume=True)` and a second `seal` record the same values.

#### Partials the seal cannot vouch for

The data pre-check refuses, with `SealCheckError`, a working copy that:

- holds `_meta.fill_in_progress`: a fill stopped between its writes and the
  merge, and the map may miss a cut. The finding says to fill again;
- holds a partial that `_meta.partial_of` does not name, when the previous
  manifest records a `partial_of` for that URN and a different fingerprint:
  `seal` would carry a `partial_of` the partial was not cut from. This
  happens after a direct `fill_edition` on `wc.registry`.

A partial with no `partial_of` in either place (from a source that already
held `isPartial` codelists, as `ingest_sdmx` can bring) keeps Phase 2a's
behaviour: it is recorded, and the rules and the partial check skip it.

#### `ingest_dataflows`

```python
def ingest_dataflows(
    wc: WorkingCopy,
    source: sdmxlib.RestRegistry | sdmxlib.EditionRegistry,
    dataflows: Sequence[SdmxUrn[Dataflow] | str],
    *,
    observations: Mapping[str, WriteSource] | None = None,
    partials: bool = True,
    publisher: Agent | None = None,
    themes: list[ConceptRef] | None = None,
) -> list[AggregateDataset]:
```

It refuses anything but a working copy with `require_working_copy`
(`sources/sdmx.py:55`), calls `wc.fill_dataflows`, then reads each given
dataflow back from `wc.registry` (a `latest` URN resolves to the version the
fill stored), builds its `AggregateDataset` with `dataset_from_dataflow` and
the DSD its structure reference resolves to, as `prepare_sdmx` does, and
stores them in one `bulk_load`. It builds datasets for the given dataflows
only: `prepare_sdmx(wc.registry)` would rebuild every dataflow the builder
holds, which on a republish includes the other tables.

### The publish check at seal

`WorkingCopy.seal` gains one step between sdmxlib's seal and the Inventory
commit:

| Step | Phase 2a | Phase 3 change |
|---|---|---|
| 0 | settle the journal | none |
| 1 | run the seal checks | structure: `structure_content`; data: the partials the seal cannot vouch for |
| 2 | fast-forward | then the cross-product pre-check |
| 3 | rebuild the search index | none |
| 4 | compute the `statcat` section | with `kind` |
| 5 | close, sdmxlib `seal` | with `partial_of=` from `_meta` |
| 5b | (new) | `check_edition(at="publish", structures=..., extensions={"statcat": check_catalog})` when the edition is a structure edition or its manifest lists a partial with `partial_of` |
| 6 | the Inventory commit | the cross-product rules run in `commit` |
| 7 | clear the journal entry | none |

`_publish_check(structures: Sequence[Path])` takes the list its caller
computes:

- **for a structure edition**, the live and embargoed editions of the other
  structure products in the head, with each batched product's head editions
  replaced by its batch edition (O4). With none, `structures=None`, since
  sdmxlib refuses an empty sequence (`_check.py:245`);
- **for a data edition**, every live and embargoed structure edition in the
  head, with the batch's structure editions in place of their products' head
  editions. sdmxlib checks each partial against the holder with its
  `partial_of` fingerprint and reports a partial no holder matches
  (sdmxlib design, `check_edition`). With no structure edition at all, the
  check raises `EditionCheckError` naming each partial, since
  `structures=None` would skip the partials check (`_check.py:291`).

A finding raises `EditionCheckError(findings, *, product, edition)`, a
subclass of `Exception`: `SealCheckError`'s message says nothing was sealed.
The edition is sealed, so the seal clears its journal entry and logs a
WARNING through `_unrecorded`, whose parameter widens to
`InventoryRuleError | EditionCheckError`. The message names each finding and
tells the producer to start a new working copy from the live edition. In
`Catalog.seal`, the product goes to `SealOutcomes.refused`.

The check runs at `publish`, as O4 states: it re-hashes every file and
recomputes every fingerprint. GH-248 measured a CODR seal at 1.6 s. Slice S2
measures the check's addition on a generated product and on a census-sized
edition (`$SDMXLIB_BENCH_BUNDLE`), and records both in its PR. The check
stays at `publish`; the owner revisits `at="receive"`, which runs the partial
and references checks with `structures=`, only if S2's measurement shows the
check is slow (A2).

### The cross-product rules

`src/statcatlib/store/cross_product.py` (new) holds pure functions:

```python
type Artefacts = Callable[[str, str, str], Mapping[str, PartialRecord]]
# (product_id, edition_id, manifest_sha256) -> URN to sdmxlib.inventory.PartialRecord

def refusals(
    full: FullInventory | None,
    leaves: Mapping[str, Leaf],
    artefacts: Artefacts,
) -> dict[str, CrossProductRuleError]:
```

`full` is the head, `leaves` the planned change, and `artefacts` returns an
edition's artefacts as `sdmxlib.inventory.PartialRecord` values
(`inventory/_format.py:182`): `fingerprint`, and `partial_of` for a partial.
That is the type of the leaf's optional `LeafEntry.artefacts`, so Phase 4
can swap the reader for the leaf field (A1). The result maps
each refused product to its error.

Two readers implement `Artefacts`:

- **`ManifestArtefacts`**, held by `InventoryWriter`, reads
  `products/<p>/<e>/manifest.json`, compares the bytes' SHA-256 with the
  entry's `manifest_sha256` before parsing (specification §6.3), and caches
  by `(edition_id, manifest_sha256)` for the writer's life. A missing,
  mismatched or unreadable manifest raises sdmxlib's `ManifestError`, which
  `commit` lets through: the writer refuses to reason about an edition it
  cannot read. `InventoryWriter(root, *, artefacts: Artefacts | None = None)`
  takes another reader; `tests/test_inventory_writer.py`, which commits
  leaves with no manifest on disk (`:246-255`), passes one that returns
  nothing.
- **`StoreArtefacts`**, for the pre-check, answers for the working copy's own
  edition from its open store: each full artefact's fingerprint, the SHA-256
  of `sdmxlib.edition.canonical_json(wc.registry.get(SdmxUrn.parse(urn),
  eager=True))`, as `seal` computes it (specification §4.4;
  `artefact_entries`, `_write.py:270`), and each partial's `partial_of` from
  `_meta.partial_of`, then the previous manifest. `sdmxlib.edition` exports
  `canonical_json` and keeps `artefact_fingerprint` internal. For every other
  edition it defers to the writer's reader. A test pins the pre-check's
  fingerprints to the sealed manifest's.

Let `A` be the planned version's live and embargoed entries, `S` those of
structure products, `S_live` the live ones, and `E` the incoming entries. The
rules compare entries of different products only, because an embargoed
edition replaces its own product's live edition at release. For each `e` in
`E`, of product `p`:

| Item | Applies to | Refused when | `rule` |
|---|---|---|---|
| 7 | `e` in a structure product, entering `live` | a full artefact of `e` is a full artefact of an entry of `S_live` in another product | `structure-home` |
| 4 | `e` in a data product, full artefact `u` with fingerprint `f` | entries of `S` hold `u` full, and none with `f` | `full-artefacts` |
| 4 | `e` in a data product, partial `u` with `partial_of` `f` | no entry of `S` holds `u` full with fingerprint `f` | `full-artefacts` |
| 4 | `e` in a data product, full artefact `u` with fingerprint `f`, no entry of `S` holding `u` | an entry of `A` in another data product holds `u` full with another fingerprint | `full-artefacts` |
| 8 | `e` in a data product entering `live`, partial `u` with `partial_of` `f` | an embargoed entry of `S` holds `u` with `f`, and no live one does | `publish-order` |

`commit` skips `refusals` when the planned change has no incoming entry, so
`describe`, `set_embargo`, `cancel` (event kind `withdraw`) and `prune`
(event kind `retire`) read no manifest.

`CrossProductRuleError(InventoryRuleError)`:

```python
def __init__(
    self,
    message: str,
    *,
    rule: Literal["full-artefacts", "structure-home", "publish-order"],
    item: Literal[4, 7, 8],
    urn: str,
    editions: Sequence[tuple[str, str]],   # (product, edition); the incoming one first
) -> None:
```

`product` and `edition` (inherited) are the incoming entry's. When no holder
matches (item 4, partial row), `editions` names the incoming entry and every
structure edition that holds `u` with another fingerprint, or the incoming
entry alone when none holds `u`. The message names the item, the URN and the
editions, for example
`structure-geo/0193…: CL_NAICS(1.0) is a full artefact of structure-classifications/0192…, live; a full artefact has one structure home (specification §6.5 item 7)`.
`InventoryRuleError.rule` widens to
`Rule | Literal["lifecycle", "full-artefacts", "structure-home", "publish-order"]`,
and `statcatlib` exports `CrossProductRuleError`.

Where the rules run:

- **`InventoryWriter.commit`** runs `self.refusals(full, planned.leaves)` (the
  module function with the writer's reader) on each planned change and
  raises the first refusal by product id. The rules are evaluated again on
  each head a lost race brings, as the fast-forward check is.
  `CatalogAdmin.release` and `rollback` (`admin.py:273`) need no change.
- **The pre-check** (step 2) builds the product's provisional leaf with
  sdmxlib's `record` (`embargo=` the head's), and calls `refusals` with
  `StoreArtefacts` and the batch's sealed structure editions in the planned
  leaves. Under embargo off the incoming entry is `live`, so items 7 and 8
  apply before the seal (D8). Under embargo on, items 4 and the partial
  match apply, and items 7 and 8 wait for the release.
- **`Catalog.seal`'s Inventory commit** (`_record_batch`, `catalog.py:206`)
  computes every leaf, calls `writer.refusals`, removes the refused
  products, and repeats until a pass drops none. A dropped structure
  edition also drops each batch structure edition whose publish check was
  given it, since its references may have resolved there. Each dropped
  product's edition goes to `refused` and `_unrecorded`.
- **The journal repair** (below).

#### Batch order (case B of sdmxlib's plan)

`_seal_each` (`catalog.py:174`) runs every pre-check before any seal. Phase 3
restructures it into three passes over the batch:

1. **Structure.** For each structure working copy: `_precheck(checks)` (items
   7 and the structure content, against the head), then `_seal_edition()`.
2. **Structure check.** For each sealed structure edition:
   `_publish_check(structures)` with every other structure edition the batch
   sealed plus the head's, as above. An edition with a finding is dropped
   and the pass repeats for the rest, until a pass drops none.
3. **Data.** For each data working copy: `_precheck(checks, batch=...)`,
   where `batch` is the surviving structure editions as provisional `live`
   (or `embargoed`) entries; then `_seal_edition()`; then
   `_publish_check(structures)` with the head's and the batch's structure
   editions.

`_precheck` and `_publish_check` take the batch's structure editions as
`batch: Mapping[str, Path]` (product to sealed edition directory), empty for
`WorkingCopy.seal`. `_seal_each` catches `SealCheckError`,
`InventoryRuleError` (`CrossProductRuleError` included), `ManifestError` and
`EditionCheckError`. Slice S1 builds passes 1 and 2 and the data pass's
publish check; S3 adds the cross-product pre-check to passes 1 and 3.

#### The journal repair

A crash between sdmxlib's seal and step 6 leaves a journal entry. Phase 3's
`settle_journal` settles every product's journal entries together when any
product has one, in one change that runs the batch's rules: the edition of
`structure-geo` and a table sealed in one crashed batch are recorded
together. Before the change it runs `_publish_check` on each journalled
structure edition and each journalled edition holding a partial, with the
same `structures=` rule (the journalled structure editions counting as a
batch). An edition with a finding, or one the rules refuse, is cleared with
a WARNING and stays unrecorded, as other refusals are. Listing every
product's `journal/` costs one directory read per product at each
`working_copy()`; Phase 4 re-measures it at 7,000 products.

#### Item 4 and a structure edit (case C of sdmxlib's plan)

A data product copies a concept scheme full, since `fill_edition` cuts
codelists only. When `structure-concepts` seals an edited scheme at the same
URN, the specification's item 4 read literally has two live editions holding
one URN with two fingerprints. Refusing the structure edition blocks the edit
until every data product is republished. Refusing each republished data
product, whose copy no longer matches the live structure, blocks the
republish. Under both refusals neither the edit nor the republish can go
live. D7 makes the structure home authoritative: the structure edition is
accepted, the data editions holding the old copy stay live and stale, and
each republish must match the new home. Item 8 already treats partials this
way: it holds back a data product "until that product is republished". The
owner accepted D7 (A3); the specification's wording is amended in
StatCat-Suite/sdmxlib#354 before S3.

A full artefact with no structure home, such as a DSD shared by two data
products or an agency scheme, is compared across the data products that hold
it, as item 4 states (A4). An incoming data edition whose copy differs from
another data product's live or embargoed copy is refused with
`full-artefacts`. An in-place edit at the source then blocks each product's
republish until the products agree; the fix is a structure home for the
artefact.

#### Item 8 under embargo off (case A of sdmxlib's plan)

With embargo off a seal records its edition live. An embargo that was on
earlier can leave a structure edition embargoed. The pre-check refuses a data
edition whose partials match only that edition, and the working copy stays
open; the producer releases the structure edition and seals again. A refusal
at the Inventory commit, after another writer changed the head, leaves the
edition unrecorded. Alternative B records such an edition `embargoed`.

#### Item 7

D10 checks item 7 as the specification states it, among live editions on
entry to `live`. An embargoed edition of `structure-geo` that adds `CL_SGC`
while `structure-classifications` still holds it live is recorded, and its
release is refused until a `structure-classifications` edition without
`CL_SGC` is live. Moving an artefact between structure products is two
releases, the edition dropping it first; between them the artefact has no
home, and a data edition cut from it in that window is refused by item 4.

#### Rollback of a structure product

A rollback makes a superseded structure edition live (an incoming entry) and
withdraws the live one. Item 7 checks the incoming edition. Data editions cut
from the withdrawn edition stay live and stale, as after a forward structure
edit. A rollback of a data product to an edition cut from a structure edition
that is neither live nor embargoed is refused by item 4's partial row
(`full-artefacts`). A rollback of both together needs a release of several
products in one version (Non-Goals).

### `verify-catalog`, `doctor` and `prune`

`_verify_edition` (`diagnostics.py:241`) passes `structures=`:

- for a data edition, every recorded edition of every structure product.
  sdmxlib checks each partial against the holder with its `partial_of`
  fingerprint and ignores the others, so a superseded data edition verifies
  against the superseded structure edition it was cut from;
- for a structure edition, the live and embargoed editions of the other
  structure products, so a reference only a superseded edition resolves is
  a finding. This lets a valid `structure-concepts` edition pass the
  `references` check;
- `None` when the list is empty.

`CatalogAdmin.prune` keeps a structure edition that a live or embargoed data
edition's `partial_of` cites: `_retirable` (`admin.py:149`) skips it. A
pruned source would make the stale data edition fail its partial check in
`verify-catalog`.

Two reports wait for slice S4, which the owner may drop: a `verify-catalog`
finding for a URN two structure products' live editions hold (a state only a
writer other than statcatlib can produce), and a `doctor` topic,
`stale data editions` (level `warning`), listing each stale data edition and
each embargoed data edition item 8 can never release, with the fix: republish
the product. Both call `cross_product` functions.

### The CODR generator

`statcatlib.testing` gains:

```python
@frozen
class CodrShape:
    geography_codes: int = 6000
    geography_depth: int = 5
    classification_codes: tuple[int, ...] = (900, 500, 300)
    observations: int = 2000   # per table

STRUCTURE_PRODUCTS: tuple[str, ...] = ("structure-geo", "structure-classifications", "structure-concepts")

def codr_structures(catalog: Catalog, *, seed: int = 0, shape: CodrShape = CodrShape()) -> dict[str, Edition]: ...

def codr_product(
    catalog: Catalog,
    product_id: str,
    *,
    tables: int = 1,
    seed: int = 0,
    shape: CodrShape = CodrShape(),
) -> Edition: ...
```

`sdmxlib.testing.Shape` and `synthetic_edition` build one dataflow,
`DF_SYNTHETIC` of agency `SYNTHETIC`, per edition, and the helpers that build
their codelists are private. The generator needs codelists shared across
products and one dataflow per table, so it builds its artefacts with
sdmxlib's model classes. Languages are `en` and `fr`; each table uses 5 % of
the geography's leaves and their ancestors, as module constants.

- **Structures**, from `seed` alone, agency `CODR`, version `1.0`, with fixed
  names (`"<id> en"`, `"<id> fr"`), no validity dates and no annotations, so
  one seed gives one set of fingerprints:
  - `CL_GEO`, `geography_codes` codes in `geography_depth` levels, and
    `HCL_GEO`, a hierarchy over all of them (O2), in `structure-geo`;
  - `CL_CLASS_1` to `CL_CLASS_3`, sized by `classification_codes`, the first
    two in three levels, and `CL_UNIT`, five units of measure, in
    `structure-classifications`;
  - `CS_CODR`, with concepts `GEO`, `CLASS_1` to `CLASS_3`, `UNIT_MEASURE`,
    `TIME_PERIOD` and `OBS_VALUE`, each coded concept's core representation
    naming its codelist, in `structure-concepts`.

  `codr_structures` writes them into a scratch `LocalRegistry` in a
  temporary directory, opens the three working copies with
  `kind="structure"`, fills each with `wc.fill_structures(scratch, ...)`,
  seals them in one `Catalog.seal`, and returns their editions. `CS_CODR`
  references codelists in the other two products, so every generated catalog
  exercises §6.5 item 9.
- **Data**, from `(seed, product_id)`: `codr_product` calls
  `codr_structures` first unless the three are live. It writes one DSD
  (`DSD_<product_id>_<n>`: dimensions `GEO`, `CLASS_1` to `CLASS_3`, then
  `TIME_PERIOD`; attribute `UNIT_MEASURE`; measure `OBS_VALUE`) and one
  dataflow (`DF_<product_id>_<n>`) per table into a scratch registry that
  also holds the structures, and an Arrow table of `observations` rows per
  table over seeded codes (`pyarrow`, which sdmxlib requires). It fills the
  product's working copy with `ingest_dataflows(..., partials=True)`, seals
  it, and deletes the scratch directory. No `HierarchyAssociation` names a
  table's dataflow, so the closure leaves `HCL_GEO` out and `CL_GEO` is cut
  (specification §8). With 2,000 rows each classification uses part of its
  codes and is cut; `CL_UNIT` may stay full when every unit is used.
- **Determinism.** One seed and shape give one set of artefact
  fingerprints, so a catalog's products can be generated in separate calls
  and still match the live structure products. A product generated with
  another seed than the live structures gets a `full-artefacts` refusal,
  which the tests use.

GH-51's body names `tests/support/codr.py`, which does not exist at
`41d4b88`. The generator is new code in `src/statcatlib/testing.py`.

### Docs and the agent context

- `docs/upgrading.md`: a 0.13.0 section. The pin; no rebuild (G1); the new
  arguments and methods; the seal's publish check and `EditionCheckError`;
  the new refusals (`CrossProductRuleError` and its three rules,
  `rule="kind"`); `catalog_section`'s `kind` argument; "write a catalog that
  holds a structure product with 0.13 only" (Risks).
- `docs/catalog-format.md`: the section's `kind` row loses "reserved"; the
  three `_meta` keys (§2); the check table gains the structure-content
  findings; a section on the cross-product rules and the publish check.
- `docs/api/store.md`: a section "Structure products and partials" with
  `fill_structures`, `fill_dataflows`, `ingest_dataflows`, the StatCan split
  as the example, and a warning against calling `fill_edition` directly on
  `wc.registry`.
- `docs/api/admin.md`: publish order, the refusals of `release` and
  `rollback`, and moving an artefact between structure products. The
  `Raises` sections of `CatalogAdmin.release` and `rollback`.
- `CLAUDE.md` ("Section format 3", "Seal steps", "The batched seal", "The
  journal rule") and the `EGM.md` files of `src/statcatlib`, `store`,
  `sources` and `cli`.
- The hub's `docs/architecture.md` describes data products only. A hub issue
  asks for a paragraph on structure products; this repository does not edit
  the hub.
- sdmxlib's plan marks Phase 3 statcatlib-side as built once S3 merges; that
  edit is sdmxlib's.

## Alternatives Considered

### Alternative A: Keep `partial_of` in a file beside the edition

*   **Pros**: the sealed `metadata.duckdb` carries no working state.
*   **Cons**: specification §4.1 owns the product directory's layout, and
    Phase 2a adds no file to it. A file inside the edition directory is
    listed by `seal` into the manifest, or refused by `check_edition` as
    unlisted, unless deleted first, which loses it when the seal fails.
*   **Reason for Rejection**: `statcat._meta` is statcatlib's, survives a
    failed seal, and needs no layout change.

### Alternative B: Record a held-back data edition as embargoed under embargo off

*   **Pros**: a data edition sealed before its structure edition is live is
    kept, and a later `release` makes it live; no seal is wasted.
*   **Cons**: a catalog with embargo off would hold embargoed entries that
    only an explicit `release` moves, and specification §6.4 lists no such
    transition.
*   **Reason for Rejection**: D8's pre-check refuses before the seal, so a
    seal is wasted only when another writer changes the head between the
    pre-check and the Inventory commit.

### Alternative C: Refuse the structure edit under item 4

*   **Pros**: the specification's item 4 holds literally at every version.
*   **Cons**: an edited concept scheme deadlocks against every data product
    that holds it (Item 4 and a structure edit).
*   **Reason for Rejection**: D7.

### Alternative D: Write every artefact into the leaf's `artefacts` field

*   **Pros**: the rules read leaves the writer already holds, with no
    manifest read per entry. Writing it is one argument to sdmxlib's
    `record` (`artefacts=`), and the reader type is already `PartialRecord`.
*   **Cons**: it changes what statcatlib writes into a shared document, and
    RFC decision 25 assigns the rules to manifests.
*   **Reason for Rejection**: the manifest cache bounds the reads to one per
    edition per writer process, and a Phase 3 test catalog holds tens of
    entries. Phase 4 times the 7,000-entry read (A1).

### Alternative E: Run the publish check on every seal

*   **Pros**: one seal path for every edition.
*   **Cons**: every Phase 2a data seal pays a full publish check, although
    its result equals the Phase 2a seal's.
*   **Reason for Rejection**: D4. `verify-catalog` runs the full check on
    every edition.

### Alternative F: A separate pre-check implementation

*   **Pros**: the pre-check reads the open store directly, with no
    provisional leaf.
*   **Cons**: two implementations of items 4, 7 and 8 can disagree.
*   **Reason for Rejection**: D5. The pre-check calls `refusals` with a
    provisional leaf and `StoreArtefacts`.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Compatibility.** No format, schema or layout version changes (G1). A
    0.12 catalog opens and seals under 0.13. A data product with no partial
    seals in the same steps and time as under 0.12.
*   **Cost.** The rules read one manifest per live or embargoed edition per
    writer process, then serve from the cache. The publish check adds a full
    publish profile to a structure seal and to the seal of an edition with
    partials; S2 records its time.
*   **Rollback.** Revert the slice's merge commit. Catalogs written by 0.13
    open read-only under 0.12, which reads `kind: "structure"` (format 3
    reserves it) and ignores the `_meta` keys.

### Risks and limits

| Risk | Mitigation |
|---|---|
| A full artefact with no structure home, edited in place at the source, blocks the republish of every data product that holds it (A4) | The refusal names the URN and the editions; the fix is a structure home for the artefact |
| A publish-check finding leaves a sealed edition unrecorded, and a rebuild costs a full ingest | The pre-check catches items 4, 7 and 8 before the seal. The remaining findings (a partial's codes, a structure reference) point at a producer error. `doctor` reports the unrecorded edition |
| A 0.12 writer on a catalog with structure products records `kind: "data"` for a structure product (refused after sdmxlib's seal, so unrecorded) and applies none of the cross-product rules | `docs/upgrading.md`: write such a catalog with 0.13 only |
| A caller fills `wc.registry` with `fill_edition` directly | The pre-check refuses a re-cut partial the map does not name (Partials the seal cannot vouch for). The guide names `fill_dataflows` |
| A source's own unit, status or SDMX-agency codelists are cut and have no structure home | D11 refuses them with the fix in the message; the producer gives them a home, mostly in `structure-classifications` (A5) |
| A stale data edition serves an old copy of a structure | Specification §6.5 item 8 allows it. S4's `doctor` topic lists stale editions |
| A rollback of a structure product strands data editions cut from the withdrawn edition | Stated in `docs/api/admin.md` |
| The first commit of a process on Phase 4's 7,000 entries reads 7,000 manifests | Phase 4 measures it (A1) |
| Two generator calls with different seeds produce mismatching structures | The refusal names `full-artefacts`; the generator's docstring states the seed rule |

## Recorded answers

The owner answered the draft's eight open questions on 2026-11-02, in the
latest comment on GH-51:

| ID | Question | Answer |
|---|---|---|
| A1 | Should Phase 4 write each edition's artefacts into the leaf's `artefacts` field? | Phase 4 decides it with the routing fields (StatCat-Suite/sdmxlib#348). Phase 3 reads manifests (RFC decision 25). |
| A2 | The publish check's profile: `publish` or `receive`? | The seal's check stays at `at="publish"`. The owner revisits it only if S2's measurement shows it is slow. |
| A3 | Item 4 and a structure edit (D7) | D7 accepted: the structure home is authoritative, a structure edit is accepted, and data editions holding the old copy become stale. The specification's amendment is StatCat-Suite/sdmxlib#354, needed before S3. |
| A4 | Full artefacts shared by data products with no structure home | Compared, as specification item 4 states (Item 4 and a structure edit). The rule is S3 work, in `cross_product.refusals`. |
| A5 | Cut codelists with no structure home (D11) | D11 accepted: the pre-check refuses them. Unit, status and SDMX-agency codelists get a home, mostly in `structure-classifications`. |
| A6 | Item 8 under embargo off (D8) | D8 accepted: the pre-check refuses before the seal. |
| A7 | Item 7 timing (D10) | Item 7 stays at release, among live editions, as the specification states. |
| A8 | Release cadence | One 0.13.0 release after S3, together with StatCat-Suite/sdmxlib-rest#90. |

## Detailed Implementation Plan

### Slices

Each slice is a branch from `origin/main` and a PR, merged with a merge
commit after `just ci` passes. Slice PRs do not edit `CHANGELOG.md`; each
updates `docs/upgrading.md`, runs `uv run python tools/count_any.py --write`
when the count changes, and runs `just docs-build`.

| Slice | Content | Waits on |
|---|---|---|
| S1 | sdmxlib 0.83 pin; the product kind; `fill_structures`; `structure_content`; the batch's structure passes; the publish check for structure editions; `EditionCheckError`; the journal repair's kind and publish check | nothing |
| S2 | `fill_dataflows`, `_meta.partial_of` and `fill_in_progress`, `ingest_dataflows`; the publish check for editions with partials; the partials the seal cannot vouch for; the CODR generator; the cost measurement | S1 |
| S3 | `cross_product.py`; the rules in `commit`, the pre-check, the batch and the journal repair; `CrossProductRuleError`; `verify-catalog`'s `structures=`; `prune`'s guard | S2 |
| S4 (optional) | `verify-catalog`'s second-home finding; `doctor`'s `stale data editions` | S3 |

### Slice S1: the pin and structure products

#### 1. `pyproject.toml` (Modify)

*   **Changes**: `sdmxlib>=0.83,<0.84`, the comment above it naming `<0.84`.

#### 2. `src/statcatlib/catalog.py` (Modify)

*   **Changes**: `Product.working_copy(*, kind: Kind | None = None, ...)`,
    passed to `WorkingCopy._start`. `_seal_each` gains the structure pass,
    the structure-check pass (repeated until it drops none) and the data
    pass (Batch order), and catches `EditionCheckError`. `_record_batch`
    drops the batch structure editions whose publish check was given a
    dropped one.

#### 3. `src/statcatlib/store/working_copy.py` (Modify)

*   **Changes**:
    - Delete `_KIND`. `_Parts` and `WorkingCopy` carry `kind`; add the
      `kind` property.
    - `_start`: kind resolution and the `rule="kind"` refusals; the `_meta`
      keys on a new edition and on resume.
    - `_precheck(checks, *, batch=None)`: `structure_content` for a
      structure working copy.
    - `_seal_edition`: `catalog_section(store, kind=self._kind)`.
    - New `_publish_check(structures)` (step 5b) and
      `EditionCheckError(Exception)`; `_unrecorded` takes
      `InventoryRuleError | EditionCheckError`.
    - `_next_leaf` records `self._kind`. `record_journalled` takes
      `(JournalEntry, sha, schema_version, kind)`; `settle_journal` reads
      each kind from the manifest, settles every product's entries in one
      change, and runs `_publish_check` on journalled structure editions.
    - `fill_structures` (above).

#### 4. `src/statcatlib/bundle.py` (Modify)

*   **Changes**: `catalog_section(store, *, kind: Kind = "data")`;
    `structure_content(conn)`; `check_catalog` runs it on a structure
    edition. Update the `PRODUCT_KINDS` comment.

#### 5. `src/statcatlib/__init__.py` (Modify)

*   **Changes**: export `EditionCheckError`.

#### 6. Tests (Create or Modify)

*   `tests/test_structure_products.py` (new, `pytest.mark.integration`):
    - a structure working copy filled by family, by id pattern and by URN
      seals with `kind: "structure"` in the leaf and the section; a second
      `working_copy()` with no `kind` takes `"structure"`;
    - `working_copy(kind="structure")` on a data product, and the reverse,
      raise `InventoryRuleError(rule="kind")` and build nothing; a resumed
      structure working copy opened with `kind="data"` is refused;
    - a structure working copy holding a dataflow, a binding or a partial
      fails its seal with `SealCheckError` and stays open;
    - a structure edition whose concept names a codelist in another live
      structure product seals; sealed with that product absent, it raises
      `EditionCheckError` naming the reference, and `doctor` reports the
      unrecorded edition;
    - three structure working copies in one `Catalog.seal`, listed with the
      concepts one first, are recorded in one version (CUJ 1); with the
      classifications one given a failing check, the concepts one is dropped
      too;
    - a crash after sdmxlib's seal of a structure edition and before step
      5b is repaired by the next `working_copy()`, which runs the publish
      check and records it with `kind: "structure"`; with an unresolved
      reference, the repair clears the entry with a WARNING.
*   `tests/test_bundle_manifest.py`: a sealed structure edition passes
    `check_catalog`; one holding a dataflow or a partial gets the finding.

#### 7. Docs (Modify)

*   `docs/upgrading.md`, `docs/catalog-format.md`, `docs/api/store.md`,
    `CLAUDE.md` and the `EGM.md` files, for the kind and the publish check.

### Slice S2: partials and the generator

#### 8. `src/statcatlib/store/working_copy.py` (Modify)

*   **Changes**: `fill_dataflows` (above); `_precheck` refuses the partials
    the seal cannot vouch for; `_seal_edition` passes `partial_of=` from
    `_meta`, filtered by `is_partial`; the publish check runs for an edition
    whose manifest lists a partial with `partial_of`, with every live and
    embargoed structure edition, and raises `EditionCheckError` when there
    is none.

#### 9. `src/statcatlib/sources/sdmx.py` (Modify)

*   **Changes**: `ingest_dataflows` (above).

#### 10. `src/statcatlib/testing.py` (Modify)

*   **Changes**: `CodrShape`, `STRUCTURE_PRODUCTS`, `codr_structures`,
    `codr_product` (above).

#### 11. Tests (Create)

*   `tests/test_partials.py` (`pytest.mark.integration`):
    - a generated product's partials each record `partial_of` equal to the
      structure product's fingerprint; the publish check received the three
      structure editions (the test wraps
      `statcatlib.store.working_copy.check_edition` with `monkeypatch` and
      records its `structures` argument);
    - a working copy filled, closed, resumed in a subprocess and sealed
      records every `partial_of`; resumed after a `ManifestError`, the same;
    - a republish with new observations re-cuts and keeps every partial
      with `partial_of`; a republish that fills nothing keeps them;
    - after a fill, the test replaces the `CL_GEO` partial in `wc.registry`
      with one holding an extra code (`_meta.partial_of` still names the
      source fingerprint); the seal raises `EditionCheckError` naming the
      code;
    - a fill that replaces a partial with its full codelist
      (`partials=False`) seals with no `partial_of` for it;
    - a working copy with `fill_in_progress` set, and one re-cut through a
      direct `fill_edition`, fail the pre-check with `SealCheckError`;
    - a partial with no structure edition in the catalog fails the seal with
      `EditionCheckError`.
*   `tests/test_codr.py` (`pytest.mark.integration`):
    - a generated data product's `metadata.duckdb` is under 10 MB;
    - two generations with one seed give equal artefact fingerprints;
    - a product of two tables holds each shared partial once, cut to the
      codes both tables use.

#### 12. Docs (Modify)

*   `docs/api/store.md`'s partials section; `docs/upgrading.md`.
    `docs/reference/testing.md` renders the generator's docstrings.

### Slice S3: the cross-product rules

#### 13. `src/statcatlib/store/cross_product.py` (Create)

*   **Changes**: `Artefacts`, `refusals`, `CrossProductRuleError`,
    `ManifestArtefacts`, `StoreArtefacts` (above).

#### 14. `src/statcatlib/store/inventory.py` (Modify)

*   **Changes**: widen `InventoryRuleError.rule`;
    `InventoryWriter(root, *, artefacts=None)` holds a `ManifestArtefacts` by
    default; `InventoryWriter.refusals(full, leaves)`; `commit` runs it when
    the planned change has an incoming entry.

#### 15. `src/statcatlib/store/working_copy.py`, `src/statcatlib/catalog.py`, `src/statcatlib/__init__.py` (Modify)

*   **Changes**: the cross-product pre-check in `_precheck` (step 2), with
    `batch`; `_record_batch` drops refused products until a pass drops none;
    `settle_journal` runs the rules on its catalog-wide change and drops
    refused editions; export `CrossProductRuleError`.

#### 16. `src/statcatlib/diagnostics.py`, `src/statcatlib/admin.py` (Modify)

*   **Changes**: `_verify_edition` passes `structures=` (above); `_retirable`
    skips a structure edition a live or embargoed data edition's
    `partial_of` cites; the `Raises` sections of `release` and `rollback`
    name `CrossProductRuleError`.

#### 17. Tests (Create or Modify)

*   `tests/test_cross_product.py` (`pytest.mark.unit` for `refusals` over
    built leaves and a stub `Artefacts`; `pytest.mark.integration` for the
    rest):
    - item 4: a data edition whose full `CS_CODR` differs from
      `structure-concepts` is refused, naming the URN and both editions; a
      structure edit is accepted and the data edition holding the old copy
      stays live; a data edition whose full DSD with no structure home
      differs from another data product's live copy is refused (A4);
    - item 7: under embargo off, a second home is refused by the pre-check
      (working copy open) and at the Inventory commit (a race simulated by
      committing the first home between the pre-check and the commit); under
      embargo on, its release is refused;
    - item 8: with embargo on, a data edition's release is refused with
      `publish-order` while its structure edition is embargoed and succeeds
      after the structure's release (CUJ 3); with embargo off, the seal is
      refused before sdmxlib's seal;
    - D11: a cut codelist with no structure home is refused by the
      pre-check, naming the URN;
    - a batch of a structure edition and its data edition records both live
      in one version; a batch whose structure edition breaks item 7 drops it
      and its data edition;
    - a crashed batch of `structure-geo` and a table is recorded in one
      version by the next `working_copy()` of the table;
    - a rollback of a data product to an edition cut from a withdrawn
      structure edition is refused with `full-artefacts`;
    - each refusal leaves no root written (`CURRENT` unchanged);
    - a sabotage test, committed: with `cross_product.refusals` monkeypatched
      to refuse every product, `WorkingCopy.seal`, `Catalog.seal` and
      `CatalogAdmin.release` each refuse.
*   `tests/test_inventory_writer.py`: the tests that commit leaves with no
    manifest construct the writer with a reader that returns nothing; a new
    test shows `commit` raising `ManifestError` for an incoming entry whose
    manifest is missing.
*   `tests/test_cli_verify_doctor.py`: `verify-catalog` is clean on a
    generated catalog with a structure edit and a stale data edition, and
    after a `prune`; a structure reference only a superseded edition
    resolves is a finding.

#### 18. Docs (Modify)

*   `docs/catalog-format.md` (the rules), `docs/api/admin.md`,
    `docs/upgrading.md`, `CLAUDE.md`, the `EGM.md` files.

### Slice S4 (optional): reports

*   `verify_catalog` adds a finding for a URN two structure products' live
    editions hold; `doctor` adds `stale data editions`, and the `DoctorItem`
    docstring's topic list (`diagnostics.py:138`) names it. Tests in
    `tests/test_cli_verify_doctor.py`.

### Acceptance test map

GH-51's acceptance list, from the issue body and sdmxlib's plan's gating
tests, as restated against `41d4b88` and sdmxlib 0.83.0:

| Acceptance | Slice | Test |
|---|---|---|
| A CODR-shaped data product's `metadata.duckdb` is under 10 MB | S2 | `test_codr.py` |
| A partial with a code its full artefact lacks is a `check_edition` finding naming the code | S2 | `test_partials.py` (at seal, through `EditionCheckError`) |
| A partial whose full artefact is in a second structure product passes `check_edition` given both | S2 | `test_partials.py` (`CL_GEO` in `structure-geo`, `CL_CLASS_*` in `structure-classifications`) |
| Two editions holding one URN as a full artefact with different fingerprints are refused, naming the URN and both editions (item 4) | S3 | `test_cross_product.py` (a data edition against its structure home; two data products' copies of a full artefact with no structure home, A4) |
| A full artefact with two structure homes is refused (item 7) | S3 | `test_cross_product.py` |
| A data edition is refused `live` while its structure edition is not live, and released once it is (item 8) | S3 | `test_cross_product.py` |
| A republish of a data product through a working copy keeps its partials cut | S2 | `test_partials.py` |
| A product generated with two tables vendors each shared partial once | S2 | `test_codr.py` |
| Dropped: a structure edition with a binding fails `check_catalog` | — | Passes on main: `tests/test_bundle_manifest.py:113` (`test_a_structure_kind_with_bindings_is_refused`), run at `41d4b88` with sdmxlib 0.82.0 (2 passed with its companion, `:107`). S1 adds a sealed structure edition to the same file. |
| Dropped: a partial lacking an ancestor is refused at build | — | Holds by construction: `Codelist.partial` adds ancestors (GH-51, first comment) |

### Verification Plan

*   **Gate**: `just ci` in each slice, read to its exit line before each
    commit; the pre-push hook runs it again.
*   **Coverage**: every refusal and finding above has a test that fails
    without it. The rules start at zero refusals on every existing test, so
    S3 commits the sabotage test in item 17.
*   **Conformance**: `sdmxlib.inventory.conformance.run` drives `try_commit`
    and passes unchanged; the rules are tested through `commit` in item 17.
*   **Measurement**: S2's PR records the publish check's time on a generated
    product and on the census edition, beside the seal's.

--------------------------------------------------------------------------------

*Drafted with EGM*

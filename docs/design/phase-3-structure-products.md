# Phase 3: structure products and partial codelists

*   **Author:** Gabriel Gellner
*   **Status:** Draft for the owner's review. The EGM review is in
    `docs/design/egm/phase-3-structure-products/`. No code is written until
    the owner approves this design.
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
        plan](https://github.com/StatCat-Suite/sdmxlib/blob/main/docs/design/sharded-bundle-catalog-plan.md):
        the scope, the gating tests, and the three cases this design settles.
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
  `structure-concepts`. Each full artefact has one structure home.
- A **data product** vendors its dataflows' closures, with each codelist cut
  to a **partial** that holds the codes the product uses and their
  ancestors. Each partial records `partial_of`, the fingerprint of the full
  codelist it was cut from.
- The seal of a structure edition or of an edition with partials runs
  `sdmxlib.edition.check_edition(at="publish", structures=[...])` against the
  catalog's structure editions before it records the edition.
- The **Inventory writer** refuses a version that breaks specification §6.5
  item 4 (full artefacts agree), item 7 (one structure home per full
  artefact) or item 8 (a data edition goes live only after the structure
  edition its partials were cut from).
- **`statcatlib.testing`** gains a CODR-shaped generator: three structure
  products and data products of one or more tables, from a seed.

sdmxlib 0.83.0 does the closure walk, the partial cut and the partial check.
statcatlib calls `fill_edition`, `fill_structures` and `check_edition`, and
adds the product kind, the persisted `partial_of` map, the cross-product
rules and the generator. No file format changes.

## The Problem

Three parts of statcatlib 0.12 at `41d4b88` stand in the way:

1. **One product kind.** `working_copy.py:81` fixes `_KIND = "data"`, and both
   records use it: `_next_leaf` (`working_copy.py:819`) and the journal repair
   in `record_journalled` (`working_copy.py:157`). `catalog_section` writes
   `"kind": "data"` (`bundle.py:134`). `Product.working_copy`
   (`catalog.py:696`) takes no kind.
2. **No partials.** `ingest_sdmx` (`sources/sdmx.py:217`) copies every
   artefact of an in-memory registry into the working copy with
   `wc.registry.ingest`. Nothing cuts a codelist, and `_seal_edition`
   (`working_copy.py:782`) passes no `partial_of` to `sdmxlib.edition.seal`.
   sdmxlib's `fill_edition` returns the map for the caller to pass, so a
   working copy closed and resumed in another process loses it (sdmxlib
   design, open question 2).
3. **One-product rules only.** `InventoryWriter.commit`
   (`store/inventory.py:342`) applies whatever the caller's `change` returns.
   sdmxlib's `inventory/_rules.py` checks one product's leaf, and the plan
   assigns the rules that read several products to statcatlib's writer.
   Nothing refuses two structure homes for one codelist, or a data edition
   whose source structure is not live.

## Terms

| Term | Meaning |
|---|---|
| structure product | A product whose leaf `kind` and `statcat` section `kind` are `"structure"`. Its editions hold full artefacts and no `Dataflow` or `DataflowBinding`. |
| data product | A product of `kind: "data"`, as in Phase 2a. |
| full artefact | An artefact a manifest lists with `partial: false`. |
| partial | A codelist a manifest lists with `partial: true`. Phase 3 rules read only the partials that record `partial_of`. |
| structure home | The structure product whose live or embargoed edition holds a URN as a full artefact. |
| entering entry | A leaf entry whose state in the version being written is `live` or `embargoed` and differs from its state in the head, or that the head does not hold. A seal record, a release and a rollback each produce one. |
| stale data edition | A live data edition whose full copy or partial no longer matches a live structure edition, after a structure edit. Specification §6.5 item 8 allows it until the product is republished. |

## Goals

The Detailed Implementation Plan's acceptance test map names the test for
each goal.

*   **G1. sdmxlib 0.83.** `pyproject.toml` requires `sdmxlib>=0.83,<0.84`
    (owner decision 5). `just ci` passes. No catalog is rebuilt: the store
    schema stays 2.52, the manifest 3.0, the Inventory 1.0, statcat
    `SCHEMA_VERSION` `"2.2"` and the search index manifest 3.
*   **G2. The product kind.** `Product.working_copy(kind="structure")` starts
    a structure product. Its edition is recorded with `kind: "structure"` in
    the leaf and in the `statcat` section. A product recorded with one kind
    and reopened with the other is refused with
    `InventoryRuleError(rule="kind")` before anything is built.
*   **G3. Structure products filled by selection.**
    `WorkingCopy.fill_structures` copies full artefacts by family, agency, id
    pattern and URN (`sdmxlib.edition.fill_structures`). A structure working
    copy holding a dataflow, a binding or a partial fails its seal check and
    stays open.
*   **G4. Data products filled with partials.** `WorkingCopy.fill_dataflows`
    adds dataflow closures and observations and cuts codelists
    (`sdmxlib.edition.fill_edition`). The working copy keeps the returned
    `partial_of` map in its store, so a seal after `working_copy(resume=True)`
    in another process records every partial's `partial_of` (owner decision
    3).
*   **G5. The publish check at seal.** A structure edition is checked with
    the catalog's other live or embargoed structure editions; an edition with
    partials, with the structure editions its partials were cut from (owner
    decision 4). A finding leaves the edition sealed and unrecorded, and
    raises `EditionCheckError` naming each finding.
*   **G6. The cross-product rules.** The Inventory writer refuses a version
    that breaks §6.5 items 4, 7 or 8 for an entering entry, with
    `CrossProductRuleError` naming the item, the URN and both editions.
*   **G7. Integrity tools.** `statcat verify-catalog` checks each partial
    against the catalog's structure editions and reports two structure
    homes for one URN. `statcat doctor` reports stale data editions.
*   **G8. The CODR generator.** `statcatlib.testing` builds the three
    structure products and a CODR-shaped data product of a given number of
    tables, from a seed (owner decision 6).
*   **G9. Docs.** `docs/upgrading.md`, `docs/catalog-format.md`, the API
    guides, `CLAUDE.md` and the `EGM.md` files describe the new surface.

## Non-Goals

*   **Fetching a structures-only set over SDMX REST.** `fill_structures`
    reads a registry that holds the artefacts. A producer ingests an FMR's
    structures into a working copy or a scratch `LocalRegistry` first (sdmxlib
    design, Non-goals).
*   **Routing fields** (`full_artefacts`, `dataflows`, `pull_bytes` in the
    leaf): Phase 4, plan Q7.
*   **Writing the leaf's optional `artefacts` field.** RFC decision 25 has
    the rules read manifests; Open Question 1 asks whether Phase 4 changes
    that.
*   **The release job, `release_due` and `supersede_pending`**: Phase 5.
*   **Catalog-level search over structure products** (specification §12):
    Phase 7. A structure edition's seal builds its per-edition search index
    as a data edition's does.
*   **A batched rollback across products.** `CatalogAdmin.rollback` stays
    one product per call (see Rollback of a structure product).
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
| O6 | The CODR generator moves to `statcatlib.testing`, with a seed and a tables-per-product parameter. Phase 4's corpus builds on it. | G8 and D9. |

Settled by this design (each is a candidate for the owner's review):

| ID | Decision | Consequence |
|---|---|---|
| D1 | `Product.working_copy(*, kind="data", ...)`, mirroring `lifecycle`. The kind is checked against the leaf before the build, and against `statcat._meta` on resume. | A structure product's caller passes `kind="structure"` on every `working_copy()` call. |
| D2 | `statcat._meta` holds two new keys: `product_kind` and `partial_of` (JSON, this edition's cuts). `_meta` is a key-value table, so no DDL changes and `SCHEMA_VERSION` stays `"2.2"`. | A sealed `metadata.duckdb` carries both keys. Readers use the manifest. |
| D3 | `WorkingCopy.fill_dataflows` and `WorkingCopy.fill_structures` wrap sdmxlib's two fill functions. `fill_dataflows` merges the returned map into `_meta.partial_of`. | A caller that calls `sdmxlib.edition.fill_edition(wc.registry, ...)` directly records no `partial_of`; the API guide says to use the method. |
| D4 | The publish check runs after sdmxlib's seal and before the record, for a structure edition or an edition holding a partial. A data edition with no partial seals as in Phase 2a. | Phase 2a's seal cost is unchanged for data products without partials. A finding orphans the sealed edition (Risks). |
| D5 | The cross-product rules live in a new pure module, `store/cross_product.py`. `InventoryWriter.commit` runs them on every planned change. The batch seal and the journal repair run them inside their `change` to drop refused products. | Every path that writes a version is covered: seal, batch seal, journal repair, release, rollback. |
| D6 | No new `event.kind`. A refused version is not written; an accepted one keeps its operation's kind (`seal`, `release`, `rollback`). | No sdmxlib change. |
| D7 | Item 4 treats the structure home as authoritative. An entering data edition's full copy must equal the home's fingerprint. An entering structure edition is never refused because a data edition holds an older copy; that data edition becomes stale. | Settles the plan's case C (Item 4). |
| D8 | An item-8 refusal under embargo off is raised before sdmxlib's seal, so the working copy stays open. A refusal at record time (a race) leaves the edition sealed and unrecorded, as a late fast-forward refusal does. | Settles the plan's case A. |
| D9 | In `Catalog.seal`, structure working copies seal before data ones, and the batch's sealed structure editions count as live for the data editions' pre-seal check and publish check. The record runs the rules on the whole version and drops refused products to a fixpoint. | Settles the plan's case B: a structure edition and its data editions go live in one version. |
| D10 | Item 7 is checked on entry to `live` or `embargoed`, against the other structure products' live and embargoed editions. | An embargoed second home is refused at seal, before release. |

## Critical User Journeys (CUJs)

### CUJ 1: A producer builds the three structure products

*   **Given**: A catalog with embargo off, and a scratch `LocalRegistry`
    holding StatCan's structures ingested from FMR.
*   **When**: The producer opens three working copies with
    `kind="structure"`, fills each with `fill_structures` (the geography
    codelist and its hierarchy; NAICS, NOC and SGC by URN; the concept
    schemes by family), and seals them in one `Catalog.seal`.
*   **Then**: One Inventory version records the three as live, each leaf has
    `kind: "structure"`. `structure-concepts`' concepts name codelists in
    `structure-classifications`, and its publish check passes because the
    batch's `structure-classifications` edition is in `structures=`.

### CUJ 2: A producer builds a CODR table with partials

*   **Given**: The catalog of CUJ 1, and a source registry holding the table's
    dataflow and DSD.
*   **When**: The producer runs `ingest_dataflows(wc, source, [flow],
    observations={...})` on `cat.product("14100287").working_copy()`, closes
    the process, resumes with `working_copy(resume=True)` in a new process,
    and seals.
*   **Then**: The manifest lists the geography and the three classification
    codelists as partials, each with `partial_of` equal to the fingerprint
    in `structure-geo` or `structure-classifications`. The publish check ran
    with both structure editions.

### CUJ 3: A structure edit under embargo

*   **Given**: The catalog of CUJ 2, with embargo on.
*   **When**: The producer seals a new `structure-geo` edition with an edited
    geography codelist at the same URN, then the republished table cut from
    it, then runs `CatalogAdmin.release` on the table first.
*   **Then**: The table's release is refused with
    `CrossProductRuleError(rule="publish-order")`, naming the geography URN,
    the table's edition and the embargoed `structure-geo` edition. After
    `release("structure-geo", ...)`, the table's release succeeds. Between
    the two releases, `statcat doctor` reports the live table edition cut
    from the old geography as stale.

### CUJ 4: A second home is refused

*   **Given**: `structure-classifications` holds `CL_NAICS` live.
*   **When**: A producer seals `structure-geo` with `CL_NAICS` in it by
    mistake.
*   **Then**: The seal is refused before sdmxlib's seal with
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
│        ├ _precheck: kind rules, cross-product│
│        │   pre-check (items 4, 7, 8)         │
│        ├ _seal_edition(partial_of=) ─────────┼──▶ edition.seal
│        ├ _publish_check(structures=) ────────┼──▶ edition.check_edition
│        └ record ── InventoryWriter.commit    │
│                     └ cross_product.refusals │──▶ inventory.record/release/…
│ sources.sdmx.ingest_dataflows                │
│ diagnostics: verify_catalog, doctor          │
│ testing: codr_structures, codr_product       │
└──────────────────────────────────────────────┘
```

### The product kind

`Product.working_copy` gains `kind: Kind = "data"` (`sdmxlib.inventory.Kind`,
`Literal["data", "structure"]`), and `WorkingCopy` gains a read-only `kind`
property. `WorkingCopy._start`:

1. settles the journal, as now;
2. refuses a recorded product whose leaf `kind` differs, with
   `InventoryRuleError(rule="kind")`, beside the lifecycle check
   (`working_copy.py:355`). sdmxlib's `Rule` already holds `"kind"`, and
   `record` raises it at record time (`sdmxlib/inventory/_rules.py:150`);
   this check moves the refusal before the build;
3. builds or resumes, as now;
4. on a new edition, writes `_meta.product_kind` and deletes
   `_meta.partial_of`, which the copied `metadata.duckdb` carries from the
   previous edition. On a resume, refuses a `_meta.product_kind` that differs
   from `kind` with `InventoryRuleError(rule="kind")`, after closing the
   registry. A resumed edition without the key (started before Phase 3)
   takes `kind`.

`_KIND` goes away. `_next_leaf` records `self._kind`. The journal repair
reads the kind from the sealed manifest's `statcat` section
(`extensions["statcat"]["kind"]`, `"data"` when absent), since a crash can
leave a sealed edition of either kind. `catalog_section(store, *, kind)`
writes the kind given. `PRODUCT_KINDS` and the section format stay as they
are: format 3 reserved `"structure"` (`bundle.py:70`), so nothing bumps.

`check_catalog` keeps its refusal of a structure edition with a binding
(`bundle.py:297`) and adds two findings for a structure edition: a
`Dataflow` it holds, and a partial artefact it holds. Both read
`sdmx.artefact` (`artefact_type`, `is_partial`).

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
cls.fill_structures(fmr, artefacts=[naics, noc, sgc_classification])
con.fill_structures(fmr, families=[sl.ConceptScheme], agency="STC")
```

`fill_structures` adds nothing a selected artefact references (sdmxlib
design, `fill_structures`), so the hierarchy is selected with its codelist
(O2), and a concept's core representation naming a codelist in
`structure-classifications` stays an open reference that the publish check
resolves (§6.5 item 9).

The structure seal pre-check, in `_precheck`, refuses a structure working
copy that holds a `Dataflow`, a `DataflowBinding` or a partial with
`SealCheckError`, using the same query as `check_catalog`'s new findings.
The working copy stays open.

### Filling a data product, and where `partial_of` is kept

```python
def fill_dataflows(
    self,
    source: sdmxlib.RestRegistry | sdmxlib.EditionRegistry,
    dataflows: Sequence[SdmxUrn[Dataflow]],
    *,
    observations: Mapping[str, WriteSource] | None = None,
    partials: bool = True,
) -> dict[str, str]:
```

It raises `TypeError` on a structure working copy, calls
`sdmxlib.edition.fill_edition(self.registry, source, dataflows, ...)`, merges
the returned map into `_meta.partial_of` inside one transaction (a later
fill's value wins for a URN), and returns the map. `fill_edition` keeps what
the builder held, so a republish built on the live edition keeps the other
dataflows' bindings and cuts each shared codelist to the codes every binding
uses (sdmxlib design, `fill_edition`).

`_meta.partial_of` is a JSON object, URN to fingerprint. `_seal_edition`
reads it while the store is open, keeps the URNs the store holds as partials
(`sdmx.artefact.is_partial`), and passes the result as
`seal(partial_of=...)`. The filter matters: a later fill can replace a
partial with the full codelist, and `seal` raises `ManifestError` for a
`partial_of` URN it holds as no partial (`sdmxlib/edition/_write.py:290`).
`seal` takes the previous manifest's value for each partial the map does not
name (`_carried_partials`, `_write.py:640`), so a republish that cuts nothing
keeps every partial's `partial_of`.

The key stays in the sealed `metadata.duckdb`, equal to the manifest's values
for this edition's cuts. A ManifestError from sdmxlib's seal closes the
working copy with the key intact, so `working_copy(resume=True)` and a second
`seal` record the same values. The next new edition deletes the key at start
(The product kind, step 4).

`sources/sdmx.py` gains the ingest path over it:

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

It calls `wc.fill_dataflows`, then builds one `AggregateDataset` per given
dataflow with `dataset_from_dataflow` over `wc.registry` and stores them in
one `bulk_load`, as `ingest_sdmx` does.

### The publish check at seal

`WorkingCopy.seal` gains one step between sdmxlib's seal and the record. Its
steps become:

| Step | Phase 2a | Phase 3 change |
|---|---|---|
| 0 | settle the journal | none |
| 1 | run the seal checks | structure working copy: dataflow, binding or partial refused |
| 2 | fast-forward | then the cross-product pre-check (below) |
| 3 | rebuild the search index | none |
| 4 | compute the `statcat` section | with `kind` |
| 5 | close, sdmxlib `seal` | with `partial_of=` from `_meta` |
| 5b | (new) | `check_edition(at="publish", structures=..., extensions={"statcat": check_catalog})` when the edition is a structure edition or holds a partial |
| 6 | record in one Inventory version | the cross-product rules run in `commit` |
| 7 | clear the journal entry | none |

`structures=` is:

- **for a structure edition**, the editions of the other structure products
  that are live or embargoed in the head, plus, in a batch, the structure
  editions the batch sealed before it (O4). With none, `structures=None`,
  since sdmxlib refuses an empty sequence;
- **for a data edition**, each live or embargoed structure edition (and, in a
  batch, each batch-sealed structure edition) holding a full artefact at a
  partial's URN whose fingerprint is that partial's `partial_of`.

A finding raises `EditionCheckError(findings, product, edition)`. The edition
is sealed, so the seal clears its journal entry and logs a WARNING, as
`_unrecorded` does after a late fast-forward refusal; `statcat doctor`
reports it as an unrecorded edition. The message names each finding and the
way forward: a new working copy from the live edition. In `Catalog.seal`,
the product goes to `SealOutcomes.refused`.

The check runs at `publish`, which recomputes every fingerprint and reads
every row. GH-248 measured a CODR seal at 1.6 s; Implementation slice S2
measures the check's addition on the generator's product and records it in
the PR (plan Q10 sets the target in Phase 5).

### The cross-product pre-check

Run in `_precheck` after the fast-forward check, against the head (and in a
batch, the batch's sealed structure editions), from manifests and the open
store. It refuses with `CrossProductRuleError` before anything is written,
and the working copy stays open:

- **structure working copy (item 7):** a full artefact the store holds
  (`sdmx.artefact`, `NOT is_partial`) that another structure product's live
  or embargoed edition holds as a full artefact;
- **data working copy (items 4 and 8):** each partial's `partial_of` (from
  `_meta.partial_of`, then the previous manifest) must match a structure
  edition: live, or live or embargoed when the head's embargo is on. A
  partial matching only an embargoed one under embargo off is refused with
  `rule="publish-order"`; a partial matching none, with
  `rule="full-artefacts"`;
- **data working copy (item 4):** each full artefact whose URN a structure
  home holds is fingerprinted as `seal` fingerprints it, the SHA-256 of
  `sdmxlib.edition.canonical_json(wc.registry.get(urn, eager=True))`
  (specification §4.4; `artefact_entries`, `sdmxlib/edition/_write.py:295`),
  and compared with the home. `sdmxlib.edition` exports `canonical_json`
  and keeps `artefact_fingerprint` internal. Only URNs a structure home
  holds are fingerprinted, a handful per product. A test pins the pre-check's
  fingerprint to the manifest's.

The pre-check saves a wasted seal in the common case. The rules at record
time stay the authority, since another writer can change the head between
the two.

### The Inventory writer's cross-product rules

`src/statcatlib/store/cross_product.py` (new) holds pure functions over a
planned version:

```python
def refusals(
    full: FullInventory | None,
    leaves: Mapping[str, Leaf],
    artefacts: Callable[[str, str], EditionArtefacts],
) -> dict[str, CrossProductRuleError]:
```

`full` is the head, `leaves` the planned change, and `artefacts(product,
edition)` returns an edition's full artefacts (URN to fingerprint) and its
partials that record `partial_of` (URN to `partial_of`), read from
`products/<p>/<e>/manifest.json`. The writer passes a reader that caches by
edition id: an edition's manifest never changes once sealed, and the cache
lives as long as the `InventoryWriter`. The result maps each refused product
to its error.

Let `A` be the planned version's live and embargoed entries, `S` those of
structure products, and `E` the entering entries. For each entering entry
`e` of product `p`:

| Item | Applies to | Refused when | `rule` |
|---|---|---|---|
| 7 | `e` in a structure product | a full artefact of `e` is a full artefact of an entry of `S` in another product | `structure-home` |
| 4 | `e` in a data product, full artefact `u` with fingerprint `f` | entries of `S` hold `u` and none with `f`; or none of `S` holds `u` and an entry of `A` in another data product holds `u` full with another fingerprint | `full-artefacts` |
| 4 | `e` in a data product, partial `u` with `partial_of` `f` | no entry of `S` holds `u` full with fingerprint `f` | `full-artefacts` |
| 8 | `e` entering `live`, partial `u` with `partial_of` `f` | an embargoed entry of `S` holds `u` with `f` and no live one does | `publish-order` |

Entries of the same product are never compared: an embargoed edition is its
product's next edition, and replaces the live one at release.

`CrossProductRuleError(InventoryRuleError)` carries `item` (4, 7 or 8), `urn`
and `editions` (the `(product, edition)` pairs concerned: the entering one
first). Its message names all three, for example
`structure-geo/0193…: CL_NAICS(1.0) is a full artefact of structure-classifications/0192…, live; a full artefact has one structure home (specification §6.5 item 7)`.
`InventoryRuleError.rule` widens to
`Rule | Literal["lifecycle", "full-artefacts", "structure-home", "publish-order"]`.

`InventoryWriter.commit` runs `refusals` on each planned change before
`try_commit`, and raises the first refusal by product id. The rules are
evaluated again on each head a lost race brings, as the fast-forward check
is. Callers that record several products drop the refused ones inside their
`change`, so `commit` finds nothing to refuse:

- **`Catalog.seal`** (`_record_batch`, `catalog.py:206`): computes every
  leaf, runs `refusals`, removes the refused products, and repeats until no
  refusal remains, since dropping a structure edition can strand a data
  edition of the same batch. Each dropped product's edition goes to
  `refused` and `_unrecorded`, as a `_next_leaf` refusal does now;
- **the journal repair** (`settle_journal`, `working_copy.py:216`): a
  refused journalled edition is cleared with a WARNING, as other refusals
  are.

`CatalogAdmin.release`, `rollback` and `cancel` (`admin.py:273`) need no
change: their single-product change goes through `commit`, which raises the
`CrossProductRuleError`. `set_embargo`, `describe` and `retire` produce no
entering entry.

#### Item 4 and a structure edit (plan case C)

A data product vendors a concept scheme full, since `fill_edition` cuts
codelists only. When `structure-concepts` seals an edited scheme at the same
URN, the specification's literal item 4 has two live editions holding one
URN with two fingerprints. Refusing the structure edition blocks the edit
until every data product is republished, and refusing each republished data
product (whose copy no longer matches the live structure) blocks the
republish: a deadlock. D7 makes the structure home authoritative: the
structure edition is accepted, the data editions holding the old copy stay
live as stale editions, and each republish must match the new home. This
mirrors item 8's rule for partials, which holds back a data product "until
that product is republished". Open Question 2 asks the owner to confirm it
and to amend the specification's wording.

#### Item 8 under embargo off (plan case A)

With embargo off a seal records its edition live. A data edition whose
partials match only an embargoed structure edition, which an earlier embargo
left, is refused by the pre-check, and the working copy stays open: release
the structure edition, then seal again. A refusal at record time, after
another writer changed the head, leaves the edition sealed and unrecorded.
Alternative B records such an edition `embargoed` in place of refusing it.

#### One `Catalog.seal` holding a structure edition and its data editions (plan case B)

`_seal_each` seals the batch's structure working copies first. Each data
working copy's pre-check and publish check then see the batch's sealed
structure editions, as if live. `_record_batch` records all of them in one
version, where the rules see the structure entries in `A` and accept the
data entries. A structure edition the rules drop takes its data editions
with it, through the fixpoint.

#### Rollback of a structure product

A rollback makes a superseded structure edition live (an entering entry) and
withdraws the live one. Item 7 checks the entering edition. Data editions
cut from the withdrawn edition stay live as stale editions, as after a
forward structure edit. A rollback of a data product to an edition cut from a
structure edition that is no longer live is refused by item 8. A rollback of
both together needs a batched admin call, which Non-Goals leaves out.

### `verify-catalog` and `doctor`

`verify_catalog` (`diagnostics.py:258`) changes in two places:

- `_verify_edition` (`diagnostics.py:241`) passes `structures=`: for a data
  edition, every recorded edition of every structure product; for a
  structure edition, every recorded edition of the other structure
  products; `None` when there are none. sdmxlib checks each partial against
  the holder with its `partial_of` fingerprint and ignores the others
  (sdmxlib design, `check_edition`), so passing superseded editions lets a
  superseded data edition verify against the structure edition it was cut
  from. Without this, a valid `structure-concepts` edition would fail the
  `references` check.
- A catalog-wide finding for each URN that two structure products' live or
  embargoed editions hold full (item 7), from `cross_product`. The writer
  refuses that state, so a finding means a writer outside statcatlib.

`doctor` (`diagnostics.py:456`) gains the topic `stale structures`, level
`warning`, one item per live data edition whose full copy differs from its
structure home or whose partial matches no live structure edition, naming
the URN and the fix: republish the product. A stale edition is a state the
rules allow, so `verify-catalog` does not fail on it.

### The CODR generator

`statcatlib.testing` gains:

```python
@frozen
class CodrShape:
    geography_codes: int = 6000
    geography_depth: int = 5
    classification_codes: tuple[int, ...] = (900, 500, 30)
    observations: int = 2000            # per table
    geography_share: float = 0.05       # share of geography leaves one table uses
    languages: tuple[str, ...] = ("en", "fr")

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

- **Structures**, from `seed` alone, agency `CODR`: `CL_GEO`, a geography
  codelist of `geography_codes` codes in `geography_depth` levels, and
  `HCL_GEO`, a hierarchy over it (O2); one classification codelist per
  entry of `classification_codes` (`CL_CLASS_1`, …), the first two with
  levels; `CS_CODR`, a concept scheme whose concepts' core representations
  name those codelists. `codr_structures` seals the three structure products
  in one `Catalog.seal` and returns their editions. `CS_CODR` in
  `structure-concepts` references codelists in `structure-classifications`
  and `structure-geo`, so every generated catalog exercises §6.5 item 9.
- **Data**, from `(seed, product_id)`: `codr_product` calls
  `codr_structures` first unless the three are live. It builds one DSD and
  one dataflow per table (`DF_<product_id>_<n>`), with four dimensions
  (geography and the three classifications) and `TIME_PERIOD`, and
  `observations` rows per table over a seeded subset of the codes, written
  as a DuckDB relation. The source is a scratch `LocalRegistry` in a
  temporary directory holding the full structures and the tables; the
  generator fills the product's working copy with
  `ingest_dataflows(..., partials=True)`, seals it, and deletes the scratch
  directory. No `HierarchyAssociation` names a table's dataflow, so the
  closure leaves `HCL_GEO` out and `CL_GEO` is cut (specification §8).
- **Determinism**: the same seed and shape give the same artefact
  fingerprints, so a catalog's products can be generated in separate calls
  and still match the live structure products. A product generated with
  another seed than the live structures gets a `full-artefacts` refusal,
  which the tests use.

`tests/support/codr.py`, which GH-51's body names, does not exist at
`41d4b88`; nothing moves.

### Docs and the agent context

- `docs/upgrading.md`: a 0.13.0 section. The pin; no rebuild (G1); the new
  arguments and methods; the seal's publish check for structure editions
  and editions with partials, and `EditionCheckError`; the new refusals
  (`CrossProductRuleError` and its three rules, `rule="kind"`);
  `catalog_section`'s `kind` argument.
- `docs/catalog-format.md`: the section's `kind` row loses "reserved"; the
  two `_meta` keys (§2); the check table gains the structure findings; a
  section on the cross-product rules and the publish check.
- `docs/api/store.md` (or a new `docs/api/structures.md`, listed in the nav):
  structure products, `fill_structures`, `fill_dataflows`,
  `ingest_dataflows`, a warning against calling `fill_edition` directly on
  `wc.registry`, and the StatCan split as the example.
- `docs/api/admin.md`: publish order and the refusals of `release` and
  `rollback`.
- `CLAUDE.md` ("Section format 3", "Seal steps", "The batched seal") and the
  `EGM.md` files of `src/statcatlib`, `store`, `sources` and `cli`.
- The hub's `docs/architecture.md` names one product kind. A hub issue asks
  for a paragraph on structure products; this repository does not edit it.
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
*   **Reason for Rejection**: D8's pre-check refuses before the seal, so the
    waste is limited to a race. Open Question 3 keeps the option.

### Alternative C: Refuse the structure edit under item 4

*   **Pros**: the specification's item 4 holds literally at every version.
*   **Cons**: an edited concept scheme deadlocks against every data product
    that vendors it (Item 4 and a structure edit).
*   **Reason for Rejection**: D7.

### Alternative D: Write every artefact into the leaf's `artefacts` field

*   **Pros**: the rules read leaves the writer already holds, with no
    manifest read per entry.
*   **Cons**: it changes what statcatlib writes into a shared document, and
    RFC decision 25 assigns the rules to manifests.
*   **Reason for Rejection**: Phase 3's catalogs are small, and the manifest
    cache bounds the reads to one per edition per process. Phase 4 times the
    7,000-entry read (Open Question 1).

### Alternative E: Run the publish check on every seal

*   **Pros**: one seal path for every edition.
*   **Cons**: every Phase 2a data seal pays a full publish check with
    nothing new to check.
*   **Reason for Rejection**: D4. `verify-catalog` runs the full check on
    every edition.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Compatibility.** No format, schema or layout version changes (G1). A
    0.12 catalog opens and seals under 0.13. A data product with no partial
    seals in the same steps and time as under 0.12.
*   **Cost.** The rules read one manifest per live or embargoed edition per
    writer process, then serve from the cache. The publish check adds a
    full publish profile to a structure seal and a partial-holding seal;
    S2 records its time.
*   **Rollback.** Revert the slice's merge commit. Catalogs written by 0.13
    open under 0.12, which reads `kind: "structure"` (format 3 reserves it)
    and ignores the `_meta` keys.

### Risks and limits

| Risk | Mitigation |
|---|---|
| A publish-check finding orphans a sealed edition, and a rebuild costs a full ingest | The pre-check catches items 4, 7 and 8 before the seal. The remaining findings (a partial's codes, a structure reference) point at a producer error. `doctor` reports the orphan |
| A caller fills `wc.registry` with `fill_edition` directly and records no `partial_of` | The guide names `fill_dataflows`. Such a partial is outside items 4 and 8 and `check_edition`'s partial check |
| A stale data edition serves an old copy of a structure | Item 8 as specified. `doctor` lists stale editions |
| A rollback of a structure product strands data editions cut from the withdrawn edition | Stated in `docs/api/admin.md`; a batched rollback is a Non-Goal |
| Phase 4's 7,000 entries make the first commit of a process read 7,000 manifests | Phase 4 measures it (Open Question 1) |
| Two generator calls with different seeds produce mismatching structures | The refusal names `full-artefacts`; the generator's docstring states the seed rule |

## Open Questions

1.  **The leaf's `artefacts` field.** Phase 3 reads manifests (RFC decision
    25). Should Phase 4 write each edition's artefacts into its leaf entry,
    so the writer and the release job read leaves only? This changes what
    statcatlib writes into a shared document, though not its format.
2.  **Item 4 and a structure edit (D7).** Confirm that a structure edition
    is never refused because a data edition holds an older full copy, and
    that the specification's item 4 is amended to say so.
3.  **Item 8 under embargo off (D8).** Refuse before the seal (this design),
    or record the edition `embargoed` (Alternative B)?
4.  **Item 7's "exactly one".** This design refuses a second home. It does
    not require a home for every full artefact a data product holds (a DSD
    or a dataflow has none). Confirm that "exactly one" means "at most one,
    and one for each partial's source".
5.  **Release cadence.** One 0.13.0 release after S3, or a release per
    slice? S1 alone is usable (structure products), and S2 alone makes
    partials that no rule guards.

## Detailed Implementation Plan

### Slices

Each slice is a branch from `origin/main` and a PR, merged with a merge
commit after `just ci` passes. Slice PRs do not edit `CHANGELOG.md`; each
updates `docs/upgrading.md`, runs `uv run python tools/count_any.py --write`
when the count changes, and runs `just docs-build`.

| Slice | Content | Waits on |
|---|---|---|
| S1 | sdmxlib 0.83 pin; the product kind; `fill_structures`; the structure seal pre-check; the publish check for structure editions; `EditionCheckError`; `check_catalog`'s structure findings | nothing |
| S2 | `fill_dataflows`, `_meta.partial_of`, `ingest_dataflows`; the publish check for editions with partials; the CODR generator | S1 |
| S3 | `cross_product.py`; the rules in `commit`, the batch and the journal repair; the pre-check for items 4, 7 and 8; `CrossProductRuleError`; `verify-catalog` and `doctor` | S2 |

### Slice S1: the pin and structure products

#### 1. `pyproject.toml` (Modify)

*   **Changes**: `sdmxlib>=0.83,<0.84`, the comment above it naming `<0.84`.

#### 2. `src/statcatlib/catalog.py` (Modify)

*   **Changes**: `Product.working_copy(*, kind: Kind = "data", ...)`, passed
    to `WorkingCopy._start`. `_seal_each` seals structure working copies
    first and passes each later working copy the batch's sealed structure
    editions.

#### 3. `src/statcatlib/store/working_copy.py` (Modify)

*   **Changes**:
    - Delete `_KIND`. `_Parts` and `WorkingCopy` carry `kind`; add the
      `kind` property.
    - `_start`: the leaf-kind refusal; `_meta.product_kind` written on a new
      edition, compared on resume; `_meta.partial_of` deleted on a new
      edition.
    - `_precheck`: the structure refusal (dataflow, binding, partial) as
      `SealCheckError`.
    - `_seal_edition`: `catalog_section(store, kind=self._kind)`.
    - New `_publish_check(structures)`, step 5b, raising `EditionCheckError`
      after clearing the journal entry. `seal` calls it; `Catalog.seal`
      calls it per product in pass 2.
    - `_next_leaf` records `self._kind`. `record_journalled` and
      `settle_journal` take each edition's kind from its manifest.
    - `fill_structures` (above).
    - New `EditionCheckError(findings, *, product, edition)`.

#### 4. `src/statcatlib/bundle.py` (Modify)

*   **Changes**: `catalog_section(store, *, kind: Kind = "data")`.
    `check_catalog` adds the structure findings for a dataflow and a
    partial. Update the `PRODUCT_KINDS` comment.

#### 5. `src/statcatlib/__init__.py` (Modify)

*   **Changes**: export `EditionCheckError`.

#### 6. Tests (Create or Modify)

*   `tests/test_structure_products.py` (new, `pytest.mark.integration`):
    - a structure working copy filled by family, by id pattern and by URN
      seals with `kind: "structure"` in the leaf and the section;
    - `working_copy(kind="structure")` on a data product, and the reverse,
      raise `InventoryRuleError(rule="kind")` and build nothing; a resumed
      structure working copy opened with `kind="data"` is refused;
    - a structure working copy holding a dataflow, a binding or a partial
      fails its seal with `SealCheckError` and stays open;
    - a structure edition whose concept names a codelist in another live
      structure product seals; sealed with that product absent, it raises
      `EditionCheckError` naming the reference, and `doctor` reports the
      unrecorded edition;
    - three structure working copies in one `Catalog.seal`, the concepts one
      referencing the other two, are recorded in one version (CUJ 1);
    - a crash after sdmxlib's seal of a structure edition is repaired with
      `kind: "structure"`.
*   `tests/test_bundle_manifest.py`: a sealed structure edition passes
    `check_catalog`; one holding a dataflow or a partial gets the finding.

#### 7. Docs (Modify)

*   `docs/upgrading.md`, `docs/catalog-format.md`, `docs/api/store.md`,
    `CLAUDE.md` and the `EGM.md` files, for the kind and the publish check.

### Slice S2: partials and the generator

#### 8. `src/statcatlib/store/working_copy.py` (Modify)

*   **Changes**: `fill_dataflows` (above); `_seal_edition` reads
    `_meta.partial_of`, keeps the URNs held as partials, and passes
    `partial_of=`; the publish check runs for an edition holding a partial,
    with the structure editions matching its `partial_of` values.

#### 9. `src/statcatlib/sources/sdmx.py` (Modify)

*   **Changes**: `ingest_dataflows` (above), exported from
    `statcatlib.sources.sdmx`.

#### 10. `src/statcatlib/testing.py` (Modify)

*   **Changes**: `CodrShape`, `STRUCTURE_PRODUCTS`, `codr_structures`,
    `codr_product` (above).

#### 11. Tests (Create)

*   `tests/test_partials.py` (`pytest.mark.integration`):
    - a generated product's partials each record `partial_of` equal to the
      structure product's fingerprint, and its seal's publish check ran
      with `structure-geo` and `structure-classifications`;
    - a working copy filled, closed, resumed in a subprocess and sealed
      records every `partial_of`; resumed after a `ManifestError`, the same;
    - a republish with new observations re-cuts and keeps every partial
      with `partial_of`; a republish that fills nothing keeps them;
    - a partial given a code its full codelist lacks fails the seal with
      `EditionCheckError` naming the code;
    - a fill that replaces a partial with its full codelist
      (`partials=False`) seals with no `partial_of` for it.
*   `tests/test_codr.py` (`pytest.mark.integration`):
    - a generated data product's `metadata.duckdb` is under 10 MB;
    - two generations with one seed give equal manifests' artefact
      fingerprints;
    - a product of two tables holds each shared partial once, cut to the
      codes both tables use.

#### 12. Docs (Modify)

*   The API guide's partials section; `docs/reference/testing.md` lists the
    generator; `docs/upgrading.md`.

### Slice S3: the cross-product rules

#### 13. `src/statcatlib/store/cross_product.py` (Create)

*   **Changes**: `EditionArtefacts`, `refusals` (above), `CrossProductRuleError`,
    the manifest reader with its per-edition cache, and the queries
    `verify-catalog` and `doctor` share (`second_homes`, `stale`).

#### 14. `src/statcatlib/store/inventory.py` (Modify)

*   **Changes**: widen `InventoryRuleError.rule`; `InventoryWriter` holds the
    cached reader; `commit` runs `refusals` on each planned change.

#### 15. `src/statcatlib/store/working_copy.py`, `src/statcatlib/catalog.py` (Modify)

*   **Changes**: the cross-product pre-check in `_precheck` (and, in a batch,
    before each data seal); `_record_batch` drops refused products to a
    fixpoint; `settle_journal` drops refused editions.

#### 16. `src/statcatlib/diagnostics.py` (Modify)

*   **Changes**: `_verify_edition` passes `structures=`; `verify_catalog`
    adds the second-home findings; `doctor` adds `stale structures`.

#### 17. Tests (Create or Modify)

*   `tests/test_cross_product.py` (`pytest.mark.unit` for the pure rules over
    built leaves and stub manifests; `pytest.mark.integration` for the
    rest):
    - item 4: a data edition whose full `CS_CODR` differs from
      `structure-concepts` is refused, naming the URN and both editions; two
      data products holding one DSD full with two fingerprints, and no
      home, are refused; a structure edit is accepted and the data edition
      holding the old copy stays live and is reported by `doctor`;
    - item 7: a second home is refused at the pre-check (working copy
      open) and at the record (a race simulated by committing the first
      home between the pre-check and the record);
    - item 8: with embargo on, a data edition's release is refused while
      its structure edition is embargoed and succeeds after the structure's
      release (CUJ 3); with embargo off, the seal is refused before the
      sdmxlib seal;
    - a batch of a structure edition and its data edition records both live
      in one version; a batch whose structure edition breaks item 7 drops
      it and its data edition;
    - a rollback of a data product to an edition cut from a withdrawn
      structure edition is refused;
    - each refusal leaves no root written (`CURRENT` unchanged).
*   `tests/test_cli_verify_doctor.py`: `verify-catalog` is clean on a
    generated catalog with a structure edit and a stale data edition;
    reports a second home written by `try_commit` directly; `doctor`
    reports the stale edition.
*   `tests/test_inventory_conformance.py`: sdmxlib's conformance suite still
    passes with the rules in `commit`.

#### 18. Docs (Modify)

*   `docs/catalog-format.md` (the rules), `docs/api/admin.md` (publish
    order), `docs/upgrading.md`, `CLAUDE.md`, the `EGM.md` files.

### Acceptance test map

GH-51's acceptance list, from the issue body and the plan's gating tests, as
restated against `41d4b88` and sdmxlib 0.83.0:

| Acceptance | Slice | Test |
|---|---|---|
| A CODR-shaped data product's `metadata.duckdb` is under 10 MB | S2 | `test_codr.py` |
| A partial with a code its full artefact lacks is a `check_edition` finding naming the code | S2 | `test_partials.py` (at seal, through `EditionCheckError`) |
| A partial whose full artefact is in a second structure product passes `check_edition` given both | S2 | `test_partials.py` (`CL_GEO` in `structure-geo`, `CL_CLASS_*` in `structure-classifications`) |
| Two editions holding one URN as a full artefact with different fingerprints are refused, naming the URN and both editions (item 4) | S3 | `test_cross_product.py` |
| A full artefact with two structure homes is refused (item 7) | S3 | `test_cross_product.py` |
| A data edition is refused `live` while its structure edition is not live, and released once it is (item 8) | S3 | `test_cross_product.py` |
| A republish of a data product through a working copy keeps its partials cut | S2 | `test_partials.py` |
| A product generated with two tables vendors each shared partial once | S2 | `test_codr.py` |
| Dropped: a structure edition with a binding fails `check_catalog` | — | Passes on main: `tests/test_bundle_manifest.py:113` (`test_a_structure_kind_with_bindings_is_refused`), run at `41d4b88` with sdmxlib 0.82.0, 2 passed with its companion. S1 adds a sealed structure edition to the same file. |
| Dropped: a partial lacking an ancestor is refused at build | — | Holds by construction: `Codelist.partial` adds ancestors (GH-51, first comment) |

### Verification Plan

*   **Gate**: `just ci` in each slice, read to its exit line before each
    commit; the pre-push hook runs it again.
*   **Coverage**: every refusal and finding above has a test that fails
    without it. The rules in `commit` start at zero refusals on every
    existing test, so S3 adds a sabotage test: a rule function that always
    refuses fails the batch and admin tests.
*   **Conformance**: `sdmxlib.inventory.conformance.run` passes after S3.
*   **Measurement**: S2's PR records the publish check's time on a generated
    product beside the seal's.

--------------------------------------------------------------------------------

*Drafted with EGM*

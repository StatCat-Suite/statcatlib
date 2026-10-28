# Naming proposal for storage, versioning and publishing

> **Historical.** This records how the names in the RFC `docs/designs/products-editions-inventory.md` were chosen, from two clean-room namers and two brainstorm passes. The RFC is authoritative; names and decisions here may have changed since.

* **Date:** 2026-10-28
* **Status:** historical. `docs/designs/products-editions-inventory.md` (§1 glossary and Key Decisions) is authoritative for the names; this file records how they were chosen. Last revision: 2, the second brainstorm pass. Revision 1 compared
  two clean-room namers. The first Goldfish round on `concepts_brief.md`
  reviewed it, and the owner settled two points afterwards (below).

## Settled by the owner

1. **The rename happens in sdmxlib**, together with any other core change the
   model needs, before statcatlib's Phase 2a pins it. There is one rebuild.
   sdmxlib's API, manifest keys, file names and store meta keys take the final
   names. statcatlib adds no translation layer.
2. **"Catalog" means `dcat:Catalog` only.** It is a publisher's collection of
   dataset descriptions, the DCAT meaning. The query object over one edition is
   **`EditionView`**. Today's `Catalog` class takes that name.
3. **Holdings becomes `Catalog`** (leaning, second pass): the holdings-wide
   object is the `dcat:Catalog`, and the existing `CatalogDescription` is its
   record. **Product stays Product.** A product can hold several
   `dcat:Dataset`s (one per dataflow), and a `structure` product holds none, so
   "Dataset" would be false for it. The DCAT alignment lives in the export
   mapping, not in the storage names.
4. **Embargo has two layers.** Storage access control keeps embargoed bytes
   from anyone outside the publish job, the GC job and the pods. Inside that
   circle, pods and the pre-materialisation job can read embargoed editions by
   design (spec §10.1, §11.2), so only software keeps them unserved until
   release. The Catalog rule "public surfaces (search, facets, DCAT export,
   routing) see only live editions" is that software control. It is a
   correctness invariant for trusted processes, not a security boundary.
5. **Two objects** (owner, second pass). A public `Catalog`, whose search,
   facets, product views and DCAT export only see live editions, and a
   separate operational object for the inventory, release, rollback, prune and
   opening an edition by id. A process can see embargoed editions only if its
   code built the operational object, and that setting is off by default. The
   operational object is unnamed so far; candidates are `CatalogOperations`,
   `Publisher` and `Steward`.
6. **A Catalog holds many products, on a laptop too** (owner, second pass).
   Search and facets across products are wanted, and can follow the first
   version.
7. **One head rule everywhere** (owner, second pass). Every Catalog, a
   laptop's included, has an Inventory and a `CURRENT` pointer, and a product's
   current edition is whatever the Inventory says. A Catalog with embargo off
   releases an edition automatically when it is sealed. Phase 2a ships a
   minimal Inventory format with a version number: per product, the current
   edition id and each edition's state. Phase 4 extends that format rather
   than replacing it. The standalone/managed split is gone.

## The scheme as it stands

| Piece | Current name | Proposed name | Status |
|---|---|---|---|
| Unit of publication, routing and lifecycle (one CODR table) | bundle | **Product** | agreed by both namers, unchallenged |
| One sealed, immutable version of a product | publication | **Edition** | challenged; see below |
| The writable, unsealed version | unpublished publication | **WorkingCopy** (sdmxlib: "unsealed edition") | settled after round 2 |
| Read-only query object over one edition | `Catalog` | **EditionView** | settled |
| Everything an organisation publishes | catalog (spec sense) | **Catalog** (`dcat:Catalog`) | settled, decision 3 |
| Holdings-wide list of every edition and its state | catalog index | **Inventory** | collides with sdmxlib's `inventory()` |
| Operational objects | (none) | **`CatalogAdmin`** (changes the Inventory), **`EmbargoReader`** (reads embargoed editions) | settled after round 2 |
| Pointer to the current Inventory version | `catalog/current` | **`CURRENT`**, at the Catalog root | one per Catalog, decision 7 |
| Identity of an edition | token | **edition id** (`edition_id`, UUIDv7) | agreed |
| File list with hashes and lineage | `bundle.manifest.json` | **`manifest.json`**, keys `edition_id`, `previous`, `product_id` | agreed |
| Metadata database file | `meta.duckdb` | **`metadata.duckdb`** | agreed |
| DuckLake file | `lake.ducklake` | **`tables.ducklake`** | agreed |
| Shared data files | `data/` | **`data/`** | unchanged |
| Lifecycle | `final` / `live` | **`final` / `revisable`** | agreed; frees `live` for routing |
| Routing state | `staged` / `serving` / `draining` / `retired` | **`embargoed` / `live` / `superseded` / `withdrawn`**; retired is derived | settled after round 2 |
| Product kind | `product` / `structures` | **`data` / `structure`** | agreed |
| Code-list cut | partial artefact | **`PartialCodelist`**, `partial_of` | matches SDMX `isPartial` |
| Group of vintages | `ProductGroup` | **`ProductSeries`** = `dcat:DatasetSeries` | agreed |
| Placement tiers | tier 1 / 2 / 3 | **`required` / `lazy` / `remote`** | agreed |
| Operations | build, write_manifest, publish, gc | **start, resume, discard, seal, upload, release, rollback, retire, prune, verify** | `discard` added in round 1 |

## The objections to "Edition"

1. **Speech.** In statistical publishing, "the 2021 edition" means a
   reference-period issue, which is a member of a ProductSeries. A revisable
   monthly product (LFS) gets a new edition each time it changes. A final
   product (census) gets a new Product for each reference period. The everyday
   meaning matches the second case, and the proposal uses the word for the
   first.
2. **The namers split.** Namer A chose Edition; Namer B chose Version and gave
   Edition as runner-up, rejecting it for the reason in point 1.
3. **The working form.** `WorkingEdition` reads as a kind of Edition, but it is
   unsealed, may be discarded, and its id is minted at start. The design
   reviewer suggested `Workspace` or `Build`.
4. **"Version" returns through DCAT.** An edition is exported as a DCAT
   revision (`dcat:hasVersion`, `dcat:previousVersion`). Readers of the DCAT
   output see "version" in a different sense from SDMX semver (`1.2.0`).
5. **It is now in the main class name.** `EditionView` makes the word the first
   one a user meets.

Candidates already rejected, with the reason:

| Word | Reason |
|---|---|
| Version | statcatlib and SDMX already use it for artefact semver (`1.2.0`, `(latest)`) |
| Release | a sealed edition can still be embargoed; kept as the verb |
| Snapshot | DuckLake snapshots live inside each edition |
| Revision | in statistics it means a revised figure, and a new edition may revise none |
| Publication | the current sdmxlib name; it suggests a document and reads as "published", while an edition can be sealed but unpublished |
| Generation | already the name for search-index generations (`search_fr__000003`) |
| Commit, deploy | git and Netlify meanings; "embargoed deploy" contradicts itself |
| Vintage | two statistical meanings (data as known at a date; a reference-period product) |

## Collisions found in this pass

- **`CatalogDescription`** (`statcatlib.model`) is today's record of a
  `dcat:Catalog`. Once "Catalog" means `dcat:Catalog`, the Catalog's DCAT export and
  this record must be the same thing or name their relation.
- **`EditionView` over a working edition.** "View" says read-only. Today's
  `Catalog` both writes (`put`, `bulk_load`, `build_index`) and reads. The
  object that writes into a working edition, and whether it also queries, has
  no name yet.
- **`Inventory`** collides with sdmxlib's public `inventory()` function, which
  lists a publication's files. That function is renamed in the sdmxlib rename
  (for example `list_files()`).

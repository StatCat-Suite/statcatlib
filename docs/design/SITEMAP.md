# Design sitemap

Current designs and RFCs for statcatlib. Superseded specs are in `design/archive/`
in the repository, outside this site. Cross-repo designs that statcatlib
implements part of live in sdmxlib, for example
[the sharded bundle catalog](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/).

| Design | Date | Author | Status | Components | Description |
|---|---|---|---|---|---|
| [Phase 4: the catalog on blob storage](catalog-on-blob-storage.md) | 2026-11-04 | Gabriel Gellner | Draft, revision 2 | store, catalog, admin, cli, testing, sdmxlib | GH-98's design: the Inventory writer's storage seam and blob writer over `obstore`, `statcat upload`, remote `Catalog.open`, `CatalogAdmin.correct` and `reinstate`, the Inventory read measurement and the `stores` test tier, in five parts. |
| [Phase 3: structure products and partial codelists](phase-3-structure-products.md) | 2026-11-02 | Gabriel Gellner | Accepted | store, catalog, sources, testing, diagnostics, sdmxlib | GH-51's design: the product kind, structure products filled by selection, data products with partial codelists and a persisted `partial_of`, the publish check at seal, the Inventory writer's cross-product rules (spec §6.5 items 4, 7, 8) and the CODR generator, in three slices and an optional fourth. |
| [Phase 2a: catalogs of products and editions](phase-2a-catalog-editions.md) | 2026-10-31 | Gabriel Gellner | Reviewed | store, catalog, admin, cli, sdmxlib | The design GH-70 implements: the sdmxlib 0.80 upgrade, the Inventory writer, `Catalog`, `WorkingCopy`, `EditionView` and `CatalogAdmin`, in four slices. Supersedes the RFC for implementation detail. |
| [Products, editions and the Inventory](products-editions-inventory.md) | 2026-10-28 | Gabriel Gellner | RFC | store, bundle, cli, sdmxlib, sdmxlib-rest | The Catalog as the `dcat:Catalog` of Products and Editions, the WorkingCopy, and an Inventory that decides each product's live edition. GH-70 waits on it. |
| [Language tags and label lookup](language-tags.md) | 2026-10-28 | Gabriel Gellner | RFC | store, sources, query, sdmxlib | BCP 47 tags as MAP keys, no default language, exact matching, labels returned with their tag, and explicit language pairs for search. Ships in four slices starting with GH-84 and GH-83. |

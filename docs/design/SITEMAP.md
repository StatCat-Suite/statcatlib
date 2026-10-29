# Design sitemap

Current designs and RFCs for statcatlib. Superseded specs are in `design/archive/`
in the repository, outside this site. Cross-repo designs that statcatlib
implements part of live in sdmxlib, for example
[the sharded bundle catalog](https://statcat-suite.github.io/sdmxlib/design/sharded-bundle-catalog/).

| Design | Date | Author | Status | Components | Description |
|---|---|---|---|---|---|
| [Products, editions and the Inventory](products-editions-inventory.md) | 2026-10-28 | Gabriel Gellner | RFC | store, bundle, cli, sdmxlib, sdmxlib-rest | The Catalog as the `dcat:Catalog` of Products and Editions, the WorkingCopy, and an Inventory that decides each product's live edition. GH-70 waits on it. |
| [Language tags and label lookup](language-tags.md) | 2026-10-28 | Gabriel Gellner | RFC | store, sources, query, sdmxlib | BCP 47 tags as MAP keys, no default language, exact matching, labels returned with their tag, and explicit language pairs for search. Ships in four slices starting with GH-84 and GH-83. |

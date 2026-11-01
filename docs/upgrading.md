# Upgrading

This page lists each breaking change, newest first, with what replaces it.
statcatlib is pre-1.0, so a minor version may break; the
[CHANGELOG](https://github.com/StatCat-Suite/statcatlib/blob/main/CHANGELOG.md)
lists every change.

## 0.12.0 (unreleased): sdmxlib 0.80

statcatlib requires `sdmxlib>=0.80.0,<0.81`. sdmxlib's
[upgrading guide](https://statcat-suite.github.io/sdmxlib/upgrading/) lists
its own breaking changes from 0.73 to 0.80; the ones below are those a
statcatlib user sees.

### Rebuild every catalog by re-ingesting

sdmxlib 0.80 stores SDMX structure at store schema 2.52, and statcatlib 0.11
wrote 2.48. `Catalog.open()` and `Catalog.from_connection()` refuse a catalog
whose `sdmx` schema holds artefacts at another version, with
`IncompatibleCatalogError`. There is no migrator: build each catalog again
from its sources with `ingest_sdmx`, `ingest_data` and `ingest_ckan`, then
run `build_index()`.

### The bundle API and the data-path policies are removed

statcatlib 0.12 publishes catalogs as sdmxlib editions. Until that writer
ships, `main` cannot publish a catalog. These names are removed:

| Removed | Replacement |
| --- | --- |
| `open_bundle`, `inspect_bundle` | `Catalog(path)` opens a folder catalog with `DATA_PATH` set to `<path>/parquet/` |
| `Embedded`, `Colocated`, `At`, `Auto`, `BundleInfo`, `BundleDataPathError` | none; a folder catalog's data is always in its own `parquet/` |
| `read_current_data_path`, `normalize_lake_data_path` | none |
| `statcatlib.bundle.publish_bundle` | `catalog_section(cat)`, passed to `sdmxlib.edition.seal(..., extensions={"statcat": section})` |
| `statcatlib.bundle.check_bundle` | `sdmxlib.edition.check_edition(path, at=..., extensions={"statcat": check_catalog})` |
| `statcat bundle check`, `explain`, `rebind`, `attach-cmd` | none |

Three kept functions change shape:

- `catalog_section` takes an open `Catalog` in place of a bundle path.
- `check_catalog(root, manifest, reg, profile)` takes sdmxlib 0.80's
  `Check` arguments: an `EditionRegistry` and the `check_edition` profile.
- `statcatlib.testing.synthetic_catalog(path, shape, *, seed=0, index=True)`
  returns a `SyntheticCatalog` with the open catalog (`cat`), its registry
  (`reg`) and the unsealed `edition_dir`. Close `cat`, then `reg`, then seal
  the edition with `sdmxlib.edition.seal`. Its `bundle_id` parameter is
  removed.

`Catalog(path)` loses its `data_path=` and `azure_credentials=` parameters.
A deployment that keeps observations on object storage through a remote
`data_path` stays on statcatlib 0.11 until Phase 4 brings remote editions.

### Language tags are lowercase and checked

sdmxlib 0.79 stores every language tag in the form `sdmxlib.normalize_tag`
returns: lowercase, and well-formed by RFC 5646. statcatlib follows it:

- Every public `lang` parameter, every `.lang()` setter and the filters
  `has_spatial`, `has_keyword`, `title_contains` and `description_contains`
  normalise the tag they are given, so `lang="fr-CA"` finds a label stored
  under `fr-ca`. A malformed tag, such as `fr_CA!` or `fr_CA`, raises
  `sdmxlib.InvalidTagError`, a `ValueError`.
- CKAN ingest strips each tag and replaces `_` with `-` before it
  normalises it, so `fr_CA` is stored as `fr-ca`. A tag that is still
  malformed is dropped from its field with a WARNING, and `ingest_ckan`
  logs how many it dropped. When two tags normalise to one (`EN` and `en`),
  the first text is kept, with a WARNING.

`InternationalString` lookups return `sdmxlib.LocalisedString`. Read the
text as `name["en"].text`, or take a plain `dict` from `name.as_dict()`;
`name.get("en")` returns a `LocalisedString` or `None`.

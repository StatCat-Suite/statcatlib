# Codebase-details review: label-lookup.md (revision 1)

Lens: codebase details. Base: statcatlib `2e5055a`. The shared sdmxlib checkout
is 0.88.0 (`/Users/gabrielgellner/dev/sdmxlib/pyproject.toml:3`), not
v0.89.0, and the reviewer could not run `git show`. The text-model claims hold
at 0.88.0. They should be re-checked at the v0.89.0 tag.

Verdict: not ready. 1 blocker, 7 major, 10 minor.

## Line citations

Every cited `file:line` in State today, Language-tags slices and Stored formats
matches the base:

- `store.py`: 734-738, 1057, 1129-1180, 1176, 1206, 1280-1318, 1343, 1401,
  1437, 1502, 1441-1460, 2452, 2572, 2574, 2781, 3636, 3812, 4042, 6171-6195,
  6224-6230, 6642, 6883, 7070-7071;
- `scopes.py`: 412-417, 557, 755, 789, 803, 884, 908, 933, 947, 685, 838,
  1414;
- `query.py`: 139, 167, 923-1016;
- `lance.py`: 74, 209-219, 1209, 1297, 1237;
- `sdmx.py:153`;
- `schema.py`: 51, 353-360;
- `bundle.py:68`;
- `pyproject.toml`: 23-33, 32;
- `test_lang_normalisation.py:54-61`;
- `test_agent_contact_ids.py:82`.

The exceptions are listed below.

## Blocker

**1. Migration: "A working copy opens over it, since `working_copy.py:1172`
checks only the `statcat` schema" is false.** Line 1172 is the
first-edition schema guard. `working_copy.py:1179-1188` then calls
`_Store._over(..., search_dir=...)`, which runs `_refuse_unreadable_index`
(`store.py:2906-2908`). `unreadable_version` accepts only
`== _MANIFEST_VERSION` (`lance.py:1140`).
`tests/test_index_version_guard.py:105` asserts the refusal. Three documents
agree that `Product.working_copy()` refuses: `docs/catalog-format.md:447-450`,
hub `architecture.md:164` and that test. **Fix:** specify how a writable 0.21
store opens a v3 `search/` to read `carried_vectors()`, replace that test, and
update `catalog-format.md` §4.1 and hub `architecture.md:164`.

## Major

**2. Migration and Downstream: no rollout window.** A v3 index fails
`check_catalog` (`bundle.py:241-244`) and `_over`. The failure reaches:

- `Product.view` and `EmbargoReader.view` (`catalog.py:378-386`);
- the blob `check_on_open` path (`catalog.py:289-296`);
- the release fold (`release.py:1174-1181`);
- upload `_verify` (`upload.py:231-237`);
- `verify_catalog` for every non-retired edition (`diagnostics.py:315-317`).

0.20 refuses v4, so no library version reads both. statcat-serve uses
`EmbargoReader` (`statcat_serve/__init__.py:10`). **Fix:** either let 0.21
read v3, or write an ordered cut-over and state what `verify_catalog` reports.

**3. `check_bundle` no longer exists** (`docs/upgrading.md:553`). **Fix:**
name `bundle.check_catalog` (`bundle.py:285`) and `statcat verify-catalog`,
and give the finding's text.

**4. State today: the count of `lang="en"` defaults is 37.** The design's own
list has 37 entries: 21 public and 16 private. The five `LanceIndex` methods
(`lance.py:1237, 1471, 1525, 1593, 1640`) are public. **Fix:** say 37 and
reclassify those five as public.

**5. Tests: the enumeration test cannot pass as specified.**

- (a) `select`, `scalars` and `sort_by` default `lang` to `None`
  (`store.py:2009, 2072, 2091`).
- (b) `_for` swallows `InvalidTagError` (`lance.py:1208-1211`).
- (c) The walk would also find `Hit` (`store.py:1343`) and `Gap`. sdmxlib
  skips only exceptions (sdmxlib `tests/unit/test_language_tags.py:451`).
- (d) `_FilteredCodelistsScope.rows` is skipped as private, although
  `scopes.py:1166` makes it reachable.
- (e) The search defaults survive Part A.

**Fix:** state the exceptions, make `_for` raise, add explicit entries, and
split the test by part.

**6. An existing bug in `check_catalog` with region tags.**
`bundle._check_language` calls `index.table_for(lang)` with manifest keys
(`bundle.py:259-281`). Those keys are aliases such as `fr_ca` and `en_t_fr`
(`lance.py:1193, 1297`). `tag_alias("fr_ca")` raises, `_for` swallows the
error, and the check reports "0 rows, the manifest says N". Every
open.canada.ca edition is affected. Fix 5(b) would turn this into an
exception. **Fix:** fix the key form in v4, give the check an accessor keyed
by alias, and add a test for `fr-CA` and `en-t-fr`. Verified by the
synthesizer at `bundle.py:279` and `lance.py:1193, 1206-1211`.

**7. The Public API tables omit label-returning calls.**

- `SchemeScope.rows` returns the raw `label` MAP (`scopes.py:523-538`).
- `bm25_search`, `search` and `_rrf_rank` return `(id, score)`
  (`lance.py:1518, 1633`; `store.py:3542-3585`), so a change to their return
  shape is needed.
- `sources/ckan.py:451` reads `raw.get("en")` for the frequency ID. Either
  scope ingest out of Goal 4 or fix it.

**8. The hub amendments are incomplete.** Hub D4 says bulk outputs return text
(`localised-string.md:215-219`). `Labels` and the hit fields contradict that,
and the amendment covers only `.scalars()`. The D9 amendment must also change
the Stage 4 row (`:594`). sdmxlib already depends on msgspec at runtime
(`istring.py:11`). The hub's D6, D7, D8 and D13 match the design's citations.

## Minor

- 9 `CodelistScope.label` returns `None` today for an unknown codelist and on
  `CatalogException` (`scopes.py:903-906`). The `batch_labels`
  `CatalogException` fallbacks (`scopes.py:798-799, 942-943`) are undecided.
- 10 `MissingTranslationError`: `args == (langs, held)`
  (`istring.py:120-124`), so pickling it raises `TypeError`. Override
  `__str__` (`istring.py:126-131`), and set `args` or define `__reduce__`.
- 11 `pick()` with no tags raises `TypeError` (`istring.py:375-377`), while
  the design's empty sequence raises `ValueError`. "As `pick` does" should
  cover only the first-held-tag rule.
- 12 Re-check the sdmxlib claims at v0.89.0.
- 13 "61 call sites" is 59 calls plus 2 definitions (`store.py:614`,
  `scopes.py:54`).
- 14 `domain-model.md:136-141`: the identifier is valid. The real errors are
  `sc.Catalog(...)`, which raises `TypeError`, and calling methods on a
  `Catalog`.
- 15 statcat-serve pins `statcatlib>=0.18,<0.19` and `sdmxlib>=0.88,<0.89`
  (`pyproject.toml:28-30`). Commit `42ad7ad` was not verified. Neither
  sdmxlib nor sdmxlib-rest reads `index.json`.
- 16 The docs list should name `catalog-format.md` lines 265, 418, 447-452,
  474 and 475, and hub `architecture.md:164`.
- 17 `schema.py` has 27 MAP columns, and `dataset.extras` (`:122`) is the
  one excluded from the 26. List the 26.
- 18 22 test files match the affected APIs, a few of them false positives.
  The design says sixteen.

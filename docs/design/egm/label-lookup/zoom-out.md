# Zoom-out review: label-lookup.md (revision 1)

Lens: zoom-out (scope, simplicity, rollout). The scope follows from GH-91 and
slice 4. 1 blocker, 4 major, 7 minor.

## Blocker

**1. Migration: a 0.21 working copy cannot open over a carried version 3
index.** The design says "A working copy opens over it, since
`working_copy.py:1172` checks only the `statcat` schema". The carried
`search/` is then opened through
`_Store._over(..., search_dir=edition_dir / "search", writable=True)`
(`working_copy.py:1179-1188`), which runs `_refuse_unreadable_index` whenever
`search_dir` is set (`store.py:2906-2908`). That check is an equality test
(`lance.py:1140`). Every `product.working_copy()` on a product with a v3 live
edition therefore raises `IncompatibleCatalogError`. Because this is the only
upgrade route, the planned test "a rebuild over a carried version 3 index
reuses its vectors" cannot pass. Hub `architecture.md:164` also lists
`Product.working_copy()` as refusing. Verified by the synthesizer at
`store.py:2904-2910` and `working_copy.py:1179-1188`.

**Fix:** in commit 12, let a writable `_over` accept version N-1 only to read
`carried_vectors()`, and make the seal always rebuild. Confirm that the vector
readers handle the v3 `text` column. State that vectors are reused only when
`build_index(model_name=...)` is given: `index_options` does not carry to a new
edition (`working_copy.py:1618`), so a seal without it re-encodes every text.

## Major

**2. Migration: the refusal works in both directions, so readers have no
overlap.** 0.20 refuses v4 and 0.21 refuses v3. Hub `architecture.md:166-169`
says to seal at the new version and deploy the new library to every reader
first, and that order deadlocks here. After the upgrade:

- `CatalogAdmin.rollback` to a retained v3 edition gives an unreadable product.
- `verify-catalog` stays red until retention prunes the v3 editions
  (`bundle.py:241-244`).
- The release job's `_reference_check` refuses mixed versions.
- `EmbargoReader` refuses the same editions.

sdmxlib-rest and statcat-serve are not stranded: neither reads `search/`. The
stranded readers are all on the statcatlib side. **Fix:** write an ordered
cut-over procedure:

1. Seal v4 editions for every product.
2. Record them in one Inventory version.
3. Switch the release job and the producers together.
4. Accept or soften the `verify-catalog` and rollback consequences.

**3. Work order: `get()` changes in Part A, but its hit types change in Part
B.** Commit 5 changes `CodesScope.get` and `ConceptsScope.get`. The fields of
`CodeHit` and `ConceptHit` stay `str` until commit 13 (`store.py:1289-1317`).
`get()` builds its composite text by hand (`store.py:2575`). **Fix:** move the
`LocalisedString` field types into commit 5 and keep only `document`,
`concept_name` and `concept_description` in Part B. Alternatively, move both
`get()` changes to Part B. D14 is unchanged either way.

**4. D14: `main` is releasable, and its docs are published, between the two
merges.** `release-2-changelog` cuts from `origin/main` (`justfile:143-162`),
and `docs.yml:12-22` publishes the site on every push to `main` that touches
`docs/**` or `src/**`. **Fix:** declare a release freeze between the merges,
or move commit 9 into Part B's docs commit. Name `v0.20.0` as the branch point
for a 0.20.x fix.

**5. Migration: a simpler option is to refuse v3 at search time rather than
when the edition opens.** Part A's label APIs never touch `search/`. Option
(a) keeps `Product.view` open over v3 and raises from the search entry points
and `_check_search`. This removes most of findings 1 and 2. It narrows the
"no compatibility shims" principle and leaves every D-number unchanged, so the
owner should confirm it. Option (b) keeps the open-time refusal and implements
findings 1 and 2.

## Minor

6. The hub docs list is incomplete:
   - `moving-a-service.md:11-21, 364` says no format version changes.
   - `architecture.md:164` needs its version row moved from 3 to 4.
   - `domain-model.md:136-141` is wrong beyond `code_map`: `sc.Catalog(...)`
     raises `TypeError` and `cat.query` raises `AttributeError`.
   - The status line of `localised-string.md:6-7` needs updating.
7. `language_counts()` hand-lists "26" columns, and `schema.py` defines no
   such list. Derive the set from `information_schema`, or defer the method.
   Keep `preference()`.
8. A sealed edition cannot reach the stale-pairs state:
   - `_tables_fingerprint` covers `statcat.language_alternate`
     (`working_copy.py:816-847`).
   - The seal rebuilds when pairs change (`working_copy.py:1605-1618`).

   `check_bundle` does not exist; the check is `bundle.check_catalog` and
   `_check_search`. **Fix:** drop the fingerprint and the warning, or keep one
   `_check_search` finding. This amends RFC decision 11, which is outside
   D1–D14.
9. `Gap.held` costs a `map_keys` call per missing row. **Fix:** keep `held`
   on the error only. Document that list subclasses lose `gaps` on slicing,
   `copy()`, `+` and pickling, and that `row()` and `value()` drop gaps.
10. D11's audit runs after Stage 4 shipped. **Fix:** record in the file's
    header which commit was audited.
11. D9 is cheap only because of the v4 bump. Note the coupling.
12. The SQL helper and the `as_langs` details could move to the store's
    `EGM.md`. No migration command is needed: `Catalog.seal(batch)` and
    `statcat build-index` exist. Document a loop recipe for running the
    rebuild across a whole corpus.

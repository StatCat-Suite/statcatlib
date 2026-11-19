# Design review: label-lookup.md (revision 1)

Lens: design critic. Checked against statcatlib `2e5055a`, the hub's
`localised-string.md` and sdmxlib's `istring.py`.

Verdict: not ready. 1 blocker, 9 major, 12 minor. Three findings would change
a decision: #8 changes D5, #2 bears on D14's no-shims stance, and #10 changes
what D14's two parts contain.

## Blocker

**1. Migration: a working copy will not open over a v3 index.**
`working_copy.py:1179-1182` calls `_Store._over(..., search_dir, writable=True)`,
and `_over` runs `_refuse_unreadable_index` (`store.py:2906-2908`).
`LocalRegistry.build` carries `search/` forward, and `_start` then discards
the edition (`working_copy.py:1190-1195`). The workaround in the error message
is to delete `search/` (`store.py:245-249`), which loses the carried vectors
(`lance.py:1274-1289`). **Fix:** let a writable store accept version N-1 for
`carried_vectors` and `drop_carried` only, and test it through the real path,
`Product.working_copy()` over a v3 edition. Decision impact: none.

## Major

**2. Migration and D14: readers are stranded.** `lance.py:1140` accepts one
version. `check_catalog` reports v3 (`bundle.py:241-243`) on the blob
`check_on_open` path, in `verify_catalog`, in the release job's fold
`_reference_check` and in the journal-repair publish check. The design does
not list these consequences:

- During the rollout, no reader version opens every product.
- A rollback to a v3 edition leaves the live edition unreadable.
- Embargoed v3 uploads in `pending/` are refused at fold.
- `verify-catalog` reports every recorded v3 edition.

**Fix:** either (a) a degraded read of v3, or (b) a cut-over runbook:
re-index before upgrading any reader, freeze rollbacks, drain `pending/`, and
accept or suppress the verify findings. Option (a) touches D14's no-shims
stance.

**3. Search: language pairs create duplicate hits.** `bm25_search_rows` and
`search_rows` do not deduplicate (`lance.py:1520-1585, 1635-1673`).
`CodesScope.find` and `ConceptsScope.find` merge nothing in BM25-only mode
(`store.py:2544-2550`). `_LanguageIndex.upsert` deletes by `id` across the
table (`lance.py:918-927`). **Fix:** deduplicate results by entity, state
that `add()` receives every tag's rows for an ID, and add a test where `en`
and `en-t-fr` hold the same code.

**4. D9: `ConceptHit.name: LocalisedString` is impossible for some rows.** A
tag that only the description holds yields `name=""` (`store.py:3925-3933`).
The index stores no separate tag for `dimension_label`
(`store.py:3818, 3851`). **Fix:** skip rows that have no name in their tag, or
type the field `name: LocalisedString | None`. State that `dimension_label`
takes the row's tag.

**5. D1: the `Gap` entity model is unspecified.** `Gap.entity: str` cannot
represent the tuple keys of `CodelistsScope.batch_labels`. `SchemeScope.labels`
is keyed by notation (`scopes.py:557-568`). `code_map`'s outer dict has no
`.gaps`. **Fix:** add a table of entity and field per call, add
`key: object` to `Gap`, and settle where `code_map` reports its gaps.

**6. Single-lookup errors are ambiguous.** Today `label()` returns `None` for
an unknown codelist (`scopes.py:903-906`). `MissingTranslationError` is a
`KeyError`, and so a `LookupError`. `get` returns `None` for an unknown entity
(`store.py:2447, 2566`). `one()` raises a bare `KeyError`
(`store.py:2212-2214`). **Fix:** keep `None` for an unknown codelist, and
document catching `MissingTranslationError` by name.

**7. The SQL helper covers MAP columns only.** Codelist names come from a
row-per-tag `sdmx.localized_text` join (`scopes.py:723-729, 1444-1450,
895-897`). **Fix:** aggregate those rows into a MAP first, as
`store.py:3768-3773` does, or rank them with `arg_min`. Specify how `filter`
matches a tag tuple and whether the `t.urn ILIKE` match stays
(`scopes.py:737-740`).

**8. D5: translated CKAN regions lose their facet key.** A dict-form region
is stored with no `uri` and no `und` (`sources/ckan.py:511-513`, confirmed by
the synthesizer). Today it is keyed by `label['en']` (`store.py:1206`). Under
D5 it gets no key and drops out of facets and `has_spatial_uri`. `facets()`
has nowhere to report the gap (`store.py:6169, 1274`). A `zxx` key cannot
round-trip through `has_spatial(lang="und")`. **Fix:** use a language-neutral
key, such as a hash of the sorted `as_dict()` items as `_contact_id` does
(`store.py:1175-1176`), with `has_spatial_key`. Or keep D5 and add a gap count
to `facets()`. **Would change D5.**

**9. D6: unknown filter keys are ignored silently.** The query loop skips a
key that has no column (`store.py:6346-6350`, confirmed by the synthesizer).
After D6, `query(Agent, name=...)` returns every agent. **Fix:** raise
`ValueError` for an unknown key, and add a test.

**10. D14: the Part A and Part B split does not compile cleanly.** `get`
returns `LocalisedString` names in Part A, while the hit field types change in
Part B (`store.py:1385-1396, 1425-1432`). **Fix:** move the hit field types
and the `_row_to_*` builders into Part A, or move both `get` changes into
Part B. This adjusts D14's contents.

## Minor

- 11 `MissingTranslationError` does not pickle (`istring.py:120-124`). Add
  `__reduce__` and a round-trip test.
- 12 The routing and fingerprint details are open:
  - route from `index.json`'s pairs;
  - fingerprint the sorted `(alternate, primary)` pairs;
  - `check_bundle` does not exist; the check is `bundle.check_catalog`
    (`bundle.py:285`);
  - a sealed edition cannot carry stale pairs;
  - cite `store.py:7075-7084`, which already refuses chains;
  - `languages`, `describe()` and `index_layout` will list primaries only.
- 13 Dataset hit `lang` needs `(id, score, lang)` returned
  (`lance.py:1518, 1633`) and a rule for hybrid RRF (`store.py:2750-2765`).
- 14 The dataset's tag union adds `zxx` dimension IDs and creates a `zxx`
  table (`sources/sdmx.py:166-168`). Exclude `zxx` and decide on `und`.
- 15 `value()` returns `None` for both "no row" and "untranslated"
  (`store.py:2271-2274`).
- 16 `RowList` and `ValueList` let `gaps` drift after mutation, and slicing
  returns a plain list. `==` ignores `gaps`. `json.dumps(Labels)` fails.
  Whole-table maps lack an `ORDER BY` (`store.py:6174-6177, 6189-6192`). The
  design does not say whether duplicate IDs are deduplicated.
- 17 `has_spatial` and `spatial_labels` resolve in different orders
  (`store.py:6214`). Taking `min(label)` can pair one row's text with another
  row's tag.
- 18 A tuple predicate matches only the first held tag's text, and `sort_by`
  mixes languages in one order. `MapCondition.key` changes type.
- 19 D2 column collisions: `item[ae.name]` overwrites silently
  (`scopes.py:400-401`). `SchemeScope.rows` returns the raw MAP
  (`scopes.py:532`).
- 20 D13 is narrower than the problem. It misses empty dimension names
  (`sources/sdmx.py:163-164`) and mixed maps such as `{en: "", fr: "X"}`.
- 21 The hub amendments are incomplete: hub D4 says bulk outputs return text
  (`localised-string.md:215-219`), and hub lines 581-583 give the test path.
- 22 `language_counts` omits concept tables. `preference("en-t-fr")` is
  unspecified. The design does not say what `document` holds for `get()`.

## Synthesizer check on #3

Search row IDs carry no tag. A code row's ID is `ident` (`store.py:3846`) and
a concept row's ID is the concept URN (`store.py:3936`). Today each table holds
one tag, so IDs are unique within a table. Once alternate-tag rows share the
primary's table, the IDs collide. `upsert` then deletes the other tag's row,
and any merge by ID conflates the two rows.

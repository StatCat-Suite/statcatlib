# Implementation-readiness lens, round 2: label-lookup.md revision 2

**Target:** `docs/design/label-lookup.md` at `7279c05`, against statcatlib
`2e5055a`. Lens: `egm-implementation-readiness`. Counts: 0 blocker, 3
major, 11 minor. Read: persona, standard, design, round-1 consolidated
report, `EGM.md` files, `justfile`, `pyproject.toml`, `pyrefly.toml`,
`mkdocs.yml`, the cited source and tests.

## Major

### MJ1. LL-D15: no way to build a real version 3 edition, and four named readers have no test
- **Section:** LL-D15 (lines 251–266); commit 11 "Tests added" (line 675).
- **Evidence:** tests simulate a version by rewriting `search/index.json`
  (`tests/test_index_version_guard.py:32-39, 111-112`). That cannot work for
  `check_catalog`: `check_edition` compares `index.json` with the manifest's
  digests, and `_check_search` compares the section's `index_version` with
  the file (`bundle.py:250-252`). LL-D15 names six readers; commit 11 tests
  three. Untested: `EmbargoReader.view`, the fold's `_reference_check`
  (`release.py:1174-1180`), upload `_verify` (`upload.py:231-236`),
  `verify_catalog` (`diagnostics.py:315-316`).
- **Fix:** say how a v3 edition is made (seal under
  `monkeypatch.setattr(lance, "_MANIFEST_VERSION", 3)`, or a fixture catalog
  sealed by 0.20.0), and whether it must hold the v3 table schema (`text`, no
  concept columns), which monkeypatching does not give. Add one test per
  named reader.

### MJ2. LL-D15: the search guard has no defined home or version source
- **Section:** LL-D15 bullet 3; Errors (line 304); commit 11.
- **Evidence:** `lance.py` cannot import `IncompatibleCatalogError`
  (`store.store` imports `lance`). `LanceIndex._version` is set only in
  `__init__` (`lance.py:1086, 1092`); `_write_manifest`
  (`lance.py:1160-1202`) never updates it. After `wc.build_index()` over a
  carried v3 index, `store._lance.version` still reads 3, since
  `_Store.build_index` reuses the object (`store.py:4539-4609`); a guard on
  `.version` then refuses a rebuilt v4 index. An index first built in a
  working copy has `_version = None`, so a guard written `!= 4` breaks every
  fresh-working-copy search. The seal's keep rule
  (`working_copy.py:1606-1613`) keeps a resumed working copy's v3 index.
- **Fix:** `_write_manifest` sets `self._version = _MANIFEST_VERSION`. One
  `_Store` helper raises only when `self._lance.version == 3`, called after
  the `_lance is None` early returns in `_execute_query`, the four `find`
  methods, `_Store.search` and `_rrf_rank`. State whether `index_layout`,
  `check_index_compatible` and direct `LanceIndex` calls raise. Add the
  version to the seal's `current` test. Tests: build over a carried v3 index
  then search succeeds; resume a v3-indexed working copy, seal, edition
  records 4.

### MJ3. Deduplication keyed by `code_urn` merges distinct hits, cuts pages, and lands too early
- **Section:** Search rows, Duplicates (lines 470–478); commit 9.
- **Evidence:** one code row per (dataset, dimension, code, lang)
  (`store.py:3665-3667, 3846-3855`); bm25 and vector `find` return each
  today (`store.py:2408-2419`); only hybrid merges (`store.py:2399-2406`).
  `*_rows` pass `limit` to LanceDB (`lance.py:1572-1585, 1662-1673`), so
  dedup afterwards shortens pages while `total = len(hits)`
  (`store.py:2421-2422`). Before commit 11 each table holds one tag, so in
  commit 9 the only effect is the cross-dataset collapse, untested until 11.
- **Fix:** key on row identity minus `lang` (code `(id, dimension_id,
  code_urn or code_id)`, concept `concept_urn`, reference metadata `id`);
  oversample as `bm25_search` does (`lance.py:1509`); move the dedup into
  commit 11 with its test; if cross-dataset collapse is intended, list it as
  a silent behaviour change.

## Minor

- **mn1. Commit 9 makes `_for` raise before commit 10 fixes
  `_check_language`.** `_check_language` calls `table_for` with alias keys
  (`bundle.py:258-280`); `normalize_tag` refuses `_`; today `_for` swallows
  it (`lance.py:1206-1211`). Between 9 and 10, `check_catalog` raises for any
  edition with a region or alternate tag. No test covers it, so CI stays
  green. Fix: swap commits 9 and 10, or move "`_for` raises" into 10.
- **mn2. Enumeration test details.** Open: callable discovery (`__module__`
  filter, `vars(cls)` vs `inspect.getmembers`, attrs `__init__`, re-export
  dedup), `_calls` shape and a set-equality check, the fixture, behaviour
  without lancedb. It cannot see the 11 private defaults (line 785), e.g.
  `search_lang="en"` (`store.py:1890, 6260, 6507`), `_SpatialFilter.lang`
  (`query.py:923`). Fix: spell out the rules and add an AST check that no
  `lang`, `langs` or `*_lang` parameter or attrs field has a string default.
- **mn3. Pinned hash test lacks fixture and value.** Fix: give the literal
  map and expected hex, and also compute it from a Python reference; define
  NULL-label and empty-map keys (`schema.py:225-235`, `store.py:6162, 6810`).
- **mn4. Tests-updated lists miss files:** commit 7
  `test_structural_queries.py:153-188`; commit 8
  `test_lang_normalisation.py:45-51`; commit 9 `test_store.py:170-208`,
  `test_sources_sdmx.py:294-312`, `test_sources_ckan.py:687`,
  `test_query_builder.py:24`, `test_index_version_guard.py:69`,
  `test_codelist_policy_and_concepts.py`.
- **mn5. Hit field renames are unassigned.** `get()` builds `document` in
  commit 9, so `CodeHit`/`ConceptHit` rename there; `ReferenceMetadataHit.text`
  has no commit; in commit 9 `ConceptHit.name`/`description` can only come
  from splitting `text` (`store.py:1420-1424`). Fix: assign all three to
  commit 9 and state the interim rule.
- **mn6. Commit 10 `_langs` key.** Table names are unchanged
  (`lance.py:218`), but `_langs` and `languages` are keyed by alias
  (`lance.py:1096-1099, 1193`); keying them by tag makes `add()` recreate
  tables (`lance.py:1311-1318`) and changes `index.json` keys. Fix: state
  that `_langs` stays keyed by alias.
- **mn7. Commit 5 adds `_lang_sql.py` with no caller until 6 and 7**, and its
  `!` marks no breaking change. Fix: add helpers with their first callers;
  drop the `!`.
- **mn8. Import direction and spatial key alias.** `query.py` imports only
  sdmxlib and attrs (`query.py:35-40`); it will import `labels` and
  `_lang_sql`, so neither may import `store.store` or `store.scopes`;
  `RowList` needs `Row` from `store/row.py`. `ExistsFilter` generates aliases
  (`query.py:928`). Fix: state the rule; make the key SQL a function of the
  alias.
- **mn9. No reference page for `statcatlib.labels`.** The docs build stays
  green before commit 15 (prose mentions at `docs/api/store.md:732, 1029,
  1037, 1085` do not fail `--strict`). Fix: add `docs/reference/labels.md`
  and a nav entry.
- **mn10. Commit 11 is large.** The row-tag rules (no invented `en`,
  `store.py:3635-3636`; code rows tagged from the code label,
  `store.py:3810-3812`; `zxx` excluded) are format-independent. Fix: split
  them into a commit before 11.
- **mn11. LL-D15 change map is implied.** All readers go through
  `unreadable_version` (`lance.py:1122-1144`, via `store.py:227-250, 2908`)
  or `bundle._check_search` (`bundle.py:241`). Fix: list the changes per
  file in commit 11 and state that `embargo`, `release`, `upload`, `admin`,
  `catalog`, `diagnostics` and `cli` do not change.

## Answers to the brief

- Commit 5: passes CI; unused until 6–7 (mn7).
- Commit 9 dedup: move to 11 (MJ3). `_for` raising: only `_check_language`
  needs the `None` return (mn1).
- Commit 10: names unchanged; v3 editions and fixtures still open (mn6).
- Commit 11: coupled by necessity; split row-tag rules (mn10).
- Commits 13 and 14: order correct; nothing in 5–12 imports msgspec.
- Commits 7 and 8 both edit `query.py` without conflict.
- Docs build stays green before 15 (mn9).

## Review-response rows

| Row | Holds | Note |
|---|---|---|
| M2 | Yes, with a gap | rename commit open (mn5) |
| M3 | Yes | |
| M7 | Yes | details in mn2 |
| M8 | Yes | ordering opens mn1 |
| M16 | Partly | lists incomplete (mn4) |
| M19 | Yes | commit 5's `!` (mn7) |
| m1 | Yes | |

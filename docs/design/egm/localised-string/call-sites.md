# LocalisedString call sites in statcatlib

* **Audited commit:** `9c6061b` (statcatlib 0.20.0 code, `sdmxlib>=0.89,<0.90`,
  sdmxlib `v0.89.0`).
* **Run:** `SDMXLIB_ISTRING_AUDIT=1 SDMXLIB_ISTRING_AUDIT_OUT=<file> uv run pytest -m "unit or integration"`
  (the suite design's migration audit, LS "Breaking changes and migration";
  [Label lookup](../../label-lookup.md), LL-D11).
* **Result:** the run wrote no site. No code in `src/` or `tests/` read an
  `InternationalString` through `[]`, `get`, `pick`, `values` or `items`
  during the run. A one-line probe in the same environment wrote its site,
  so the hook was active.

## Why the table is empty

Stage 4 shipped in `04b27ac` (GH-101, statcatlib 0.12.0). It replaced every
lookup on an `InternationalString` with `as_dict()`, which the hook does not
audit, and statcatlib reads stored labels in SQL (`MAP` subscripts) rather
than through the text model. The sites that slice 4 changes are those SQL
reads and the `str` returns built from them, listed with `file:line` in the
design's appendix "State today":

| Site | Old | New | Caught by |
|---|---|---|---|
| `store/scopes.py:557, 755, 884, 908` | `str` labels from `MAP[?]` / `localized_text` | `LocalisedString`, `Labels`, `RowList` | `tests/test_scopes.py` |
| `store/store.py:6171, 6180, 6195, 6883` | `dict[..., str]` label maps | `Labels` | `tests/test_edition_view.py`, `tests/test_filters.py`, `tests/test_structural_queries.py` |
| `store/store.py:6642` | `d.title[?] AS title` | `title`, `title__lang` | `tests/test_lang_normalisation.py` |
| `store/store.py:2452, 2572, 2574` | English-first pick over a stored MAP | `pick_map` over the requested tags | `tests/test_code_search.py` |
| `store/store.py:1343, 1401, 1437, 1502, 2781` | `Hit.lang` default and fallback | the row's stored tag | `tests/test_code_search.py`, `tests/test_hybrid_search.py` |
| `store/store.py:1206` | `label['en']` facet key | URI or hash (LL-D5) | `tests/test_filters.py`, `tests/test_spatial_key.py` |
| `sources/sdmx.py:153` | `InternationalString` truthiness | empty texts dropped (LL-D13) | `tests/test_sources_sdmx.py` |

# Label lookup: LocalisedString returns, required tags and language pairs in search

*   **Author:** Gabriel Gellner
*   **Date:** 2026-11-18
*   **Status:** Draft, revision 1, for an EGM review round. The project owner
    set the scope and decided the fourteen points under "Decisions" on
    2026-11-18. No code is written yet.
*   **Issue:** GH-91 (Stages 4 and 5 of the suite design, its
    single-repository items and its comment), together with slice 4 of
    [Language tags and label lookup](language-tags.md). GH-88's contract is
    already built and closes with Part A.
*   **Suite design:** [Text model: add LocalisedString per the SDMX
    Information
    Model](https://github.com/StatCat-Suite/statcat-suite.github.io/blob/main/docs/design/localised-string.md)
    in the StatCat Suite hub. This design builds its Stages 4 and 5 and
    proposes two amendments to it ("Hub design amendments").
*   **Read against:**
    *   statcatlib `2e5055a` (`origin/main`, v0.20.0, on `sdmxlib>=0.89,<0.90`).
        Every `file:line` below is at this commit unless it names another.
    *   sdmxlib `v0.89.0`, `src/sdmxlib/model/istring.py` (`LocalisedString`,
        `InternationalString`, `pick`, `as_dict`, `tag_alias`,
        `normalize_tag`, `InvalidTagError`, `MissingTagError`).
    *   statcat-serve `42ad7ad` and the hub at `2ef02da`, for downstream
        impact.

## Overview

statcatlib returns labels as `str`, falls back to English or to an
identifier when a translation is missing, and defaults every `lang`
parameter to `"en"`. The language-tags RFC decided the replacement
(decisions 4 to 12): no default language, exact matching over a tag or an
ordered tuple, labels that carry the tag that answered, an error from single
lookups, `None` and a gap list from bulk calls, and language pairs in
search. sdmxlib 0.79 shipped the value type, `LocalisedString(text, lang)`,
and `InternationalString` as a `Mapping[str, LocalisedString]`.

This design builds the statcatlib side in two parts, released together as
0.21.0 with no compatibility shims:

*   **Part A, the lookup API.** Required `lang`, tag tuples,
    `LocalisedString` returns, `MissingTranslationError`, gap lists,
    `title` / `title__lang` projection columns, and the last sites that use
    English as a key.
*   **Part B, search.** Required `lang`, hits tagged with the matched
    text's tag, language pairs in the index, and search index format
    version 4.

### Terms

*   **Tag:** a BCP 47 language tag in the lowercase form `normalize_tag`
    returns, such as `en` or `fr-ca`.
*   **Tag tuple:** an ordered sequence of tags; the first one held answers.
*   **Held tag:** a key of a stored multilingual MAP.
*   **Gap:** a label a bulk call could not answer in any requested tag.
*   **Language pair:** an alternate tag mapped to a primary tag, such as
    `en-t-fr` to `en`, recorded in `statcat.language_alternate`
    (`store/schema.py:353-360`).
*   **Document:** the composite text a search row is indexed under, built
    from a title, labels and context.

## State today

### GH-91 Stage 4

Stage 4 shipped in `04b27ac` (GH-101, statcatlib 0.12.0), without the audit
table and without msgspec.

| Item | State | Evidence |
|---|---|---|
| sdmxlib with Stages 1–2 (0.79 or later) | Done | `pyproject.toml:32` pins `sdmxlib>=0.89,<0.90` |
| `_istring_to_map` becomes `s.as_dict()` | Done; the helper remains as a `None` guard | `store/store.py:734-738` |
| `store.py:1162, 1171` (`.encode()` and `+=` on a lookup result) | Done; GH-94 rewrote `_agent_id` and `_contact_id` | `store/store.py:1129-1180` |
| Contact ID in the issue comment | Done; hashes `as_dict()` items, so IDs are unchanged | `store/store.py:1176`; `tests/test_agent_contact_ids.py:82` |
| `sources/sdmx.py:115` truthiness | Changed shape. It now tests `InternationalString` truthiness, so an agency named `{"en": ""}` keeps an empty English label; no test covers it | `sources/sdmx.py:153` |
| `scopes.py:999, 1054` `InternationalString(en=r[1] or "")` | Done; an unnamed dimension is `{zxx: id}` (GH-94) | `store/scopes.py:412-417` |
| `_language_slug` becomes `tag_alias` | Partly. Lookup and grouping call `tag_alias`; `_table_name` inlines the old body | `store/lance.py:1209, 1297`; `lance.py:218` |
| msgspec dependency | Open | `pyproject.toml:23-33` |
| Audit table `call-sites.md` | Open | `docs/design/egm/` has no `localised-string/` |
| msgspec JSON-order test; frozen `tag_alias` test | Open | |
| Every `lang` parameter refuses a malformed tag, enumerated | Partly; `tests/test_lang_normalisation.py:54-61` checks three sites by hand | |

### GH-91 Stage 5 and its single-repository items

| Item | State | Evidence |
|---|---|---|
| Label APIs return `LocalisedString` | Open; they return `str` or `dict[..., str]` | `scopes.py:557, 755, 884, 908`; `store.py:6171, 6180, 6195, 6883` |
| `batch_labels` with `None` and gaps | Open; both fall back to the code ID | `scopes.py:789, 803`; `scopes.py:933, 947` |
| `title` and `title__lang` columns | Open; `.select()` writes `d.title[?] AS title` | `store.py:6642` |
| `MissingTranslationError` | Open | |
| Ingest refuses an empty `lang` | Done in effect: every stored key passed `normalize_tag`, and `tag_alias("")` raises in `_by_language` | `lance.py:1297` |
| `Hit.lang` default and fallback | Open | `store.py:1343`; `1401, 1437, 1502`; dataset hits take the requested tag at `2781` |
| `lang="en"` defaults | Open; 33 | Public: `query.py:936, 981, 1000, 1016`; `scopes.py:557, 884`; scope fields `scopes.py:685, 838, 1414`; `store.py:1343, 1968, 1989, 2348, 2486, 2610, 2713, 7302, 7349, 7360, 7372, 7405`. Private: `query.py:923`; `store.py:1890, 3546, 6007, 6171, 6180, 6195, 6260, 6402, 6507, 6883`; `lance.py:1237, 1471, 1525, 1593, 1640` |
| Legacy search filter on a slug | Done; `c4e6e4f` removed the single legacy table and its filter | |
| English-first fallback over a stored MAP | Open; now `store.py:2452, 2572, 2574` | |
| Composite `text` in hits | Open; `ReferenceMetadataHit.text` is composite too (`store.py:4042`) | `store.py:1280-1318, 1441-1460` |

### Language-tags slices

| Slice | State |
|---|---|
| 1. CKAN tag handling (GH-84) | Done (PR #86) |
| 2. Spatial identifiers and English as a key (GH-83, GH-94) | Done (PRs #93, #95), except two sites GH-94 left for slice 4: the agent filter `name['en']` (`store.py:1057`) and the spatial facet key and labels (`store.py:1206, 6224-6230`) |
| 3. `normalize_tag` and `LocalisedString` | Done in code: every public `lang` normalises, `MapCondition` and `MapFilter` convert (`query.py:139, 167`), `add_language_alternates` normalises (`store.py:7070-7071`). GH-88 is still open |
| 4. The breaking lookup change | This design |

Slice 4 also covers decision 9's last two sites, the invented `{"en"}`
search language (`store.py:3636, 3812`), and decision 11's search side:
pairs are stored but the index ignores them, and `index.json` records no
pairs fingerprint.

## Goals

*   Every `lang` parameter is required. Label lookups take a tag or a tag
    tuple; search takes one tag.
*   Every label returned carries the tag that answered.
*   A single lookup raises `MissingTranslationError` for a missing label; a
    bulk or paged call returns `None` and lists every gap.
*   No query uses English as a key, a fallback or an invented language.
*   Search indexes alternate-tag text in its primary tag's table and reports
    the stored tag of each hit.
*   The acceptance criteria of GH-91 and of the suite design's Stages 4–5.

## Non-goals

*   RFC 4647 range matching, and display-case tags.
*   JSON or VARIANT columns.
*   Per-source refusal counts in `.gaps` (RFC finding 13). Ingest already
    logs its refusals per source; `.gaps` lists missing labels at read time.
*   A `.frame()` method. statcatlib has none; one added later uses the
    columns of decision 2.
*   The read cost of `_map_to_istring`'s `InternationalString.of()` (61 call
    sites). sdmxlib's trusted constructor is private; a later issue can ask
    for a public one.

## Decisions

Each decision below was decided by the owner, 2026-11-18.

**D1. Bulk result containers.** `Labels[K]` is a `Mapping[K,
LocalisedString | None]` with `gaps: tuple[Gap, ...]`. `RowList` and
`ValueList` are `list` subclasses with the same `gaps`. One `Gap` type
serves all three. Results keep their shape (`for k, v in labels.items()`
works), and the gaps ride on the result.

**D2. Projection columns.** A selected MAP column gives two `Row` columns:
`title` (text or `None`) and `title__lang` (the tag that answered, or
`None`). `__lang` is the suite suffix (hub D13). A future column per tag is
written `<column>_<tag_alias>` (`title_fr_ca`, hub D8). This settles the RFC's
open `title_lang` item.

**D3. `.scalars()` over a MAP column** yields `LocalisedString | None` per
value, since one column has no room for `__lang`. statcatlib's rule is that
tables carry `__lang` and every other shape carries `LocalisedString`.

**D4. Codelist scopes lose `.lang()`.** `CodelistScope.lang`,
`CodelistsScope.lang`, `_FilteredCodelistsScope.lang` and their `_lang="en"`
fields are removed. Each terminal or filter that reads a label takes
`lang=`. A scope setter with no default would leave terminals that raise
until it is called, and two ways to give a tag.

**D5. Spatial facet key.** `facets("spatial")` keys a coverage by
`COALESCE(uri, label['und'], label['zxx'])`. CKAN stores free-text regions
under `und`, so every row CKAN writes keeps a key. A row with none of the
three has no key and is counted as a gap.

**D6. The agent `name` filter is removed** (`store.py:1057`). Agents are
identified by `id` (GH-94).

**D7. Search takes one tag.** One table and one stemmer answer a query, so
`lang` is a `str`; a sequence raises `TypeError`. An alternate tag routes to
its primary tag's table, and each hit's `lang` names the tag its text is
stored under.

**D8. Single lookups raise for required labels only.** A code's or concept's
name raises `MissingTranslationError`; an optional field, such as a
concept's description, returns `None`.

**D9. Index columns.** Search index format version 4 renames `text` to
`document` and adds `concept_name` and `concept_description`, so
`ConceptHit` reads its fields from columns instead of splitting the document
on `" | "`. `text_sha256` keeps its name and value.

**D10. msgspec is a dev dependency only.** statcatlib source never
serialises a `LocalisedString`: its writers call `as_dict()` and the CLI
prints no labels. The JSON-order acceptance test imports msgspec from the
`dev` group.

**D11. Audit table.** sdmxlib's audit hook runs on this branch before the
API changes, and its de-duplicated `src/` sites are committed as
`docs/design/egm/localised-string/call-sites.md`.

**D12. This design comes first**, with one EGM review round before code.

**D13. Empty text at SDMX ingest.** An agency whose name texts are all empty
or whitespace is labelled `{zxx: agency_id}` (`sources/sdmx.py:153`).

**D14. Shape of the work.** One branch, two PRs (Part A, Part B) merged in
order and released together as 0.21.0. Each passes `just ci` alone. GH-88
closes with Part A.

## Public API

### Conventions

*   **`Langs`**, exported as `type Langs = str | Sequence[str]`. A bare `str`
    is one tag. An empty sequence raises `ValueError`, a non-`str` item
    `TypeError`, a malformed tag `sdmxlib.InvalidTagError`. Tags are
    normalised once, duplicates dropped keeping the first, and the first
    held tag answers, as `InternationalString.pick` does. One private
    helper, `statcatlib._langs.as_langs()`, does this everywhere.
*   **Required and keyword-only.** No `lang` has a default. Positional
    `lang` parameters (`agent_labels(lang)`) become keyword-only.
*   **Search** takes `lang: str` (D7).
*   **Single lookups** return `LocalisedString` and raise
    `MissingTranslationError` (D8). An unknown entity keeps today's result
    (`None` or `LookupError`).
*   **Bulk and paged calls** return `None` per missing label and list gaps
    (D1). They raise only for a caller bug: `batch_labels` raises `KeyError`
    naming every unknown code ID (RFC finding 10).

### New public types

| Name | Definition |
|---|---|
| `MissingTranslationError` | `class MissingTranslationError(sdmxlib.MissingTagError)`, so also a `KeyError`. `__init__(entity: str, field: str, langs: Sequence[str], held: Sequence[str])`; attributes `entity`, `field`, `langs`, `held`. `str(e)`: `no text for 'de' in name of urn:…:CL_GEO(1.0).CA; held: 'en', 'fr'` |
| `Gap` | attrs frozen: `entity: str`, `field: str`, `langs: tuple[str, ...]`, `held: tuple[str, ...]` |
| `Labels[K]` | `Mapping[K, LocalisedString \| None]` with `gaps`; immutable; iterates in request order, or in SQL order for whole-table maps |
| `RowList` | `list[Row]` with `gaps` |
| `ValueList` | `list[object]` with `gaps` |
| `Langs` | `str \| Sequence[str]` |

`statcatlib` also re-exports `LocalisedString`, `MissingTagError` and
`InvalidTagError` beside `InternationalString`.

### Label lookups (Part A)

| Name | Before | After |
|---|---|---|
| `CodelistScope.label` | `label(lang="en") -> str \| None` | `label(*, lang: Langs) -> LocalisedString`; raises `MissingTranslationError`; an unknown codelist raises `LookupError` |
| `CodelistScope.batch_labels` | `(code_ids, lang=None) -> dict[str, str]`, ID fallback | `(code_ids, *, lang: Langs) -> Labels[str]`; unknown IDs raise `KeyError`; an untranslated code is `None` and a gap |
| `CodelistsScope.batch_labels` | `(pairs, lang=None) -> dict[tuple[str, str], str]` | `(pairs, *, lang: Langs) -> Labels[tuple[str, str]]`, same rules |
| `.lang()` on the three codelist scopes | setter, default `"en"` | removed (D4) |
| `CodelistScope.rows` (enriched) | label `COALESCE(name[?], code_id)` | `rows(*, lang: Langs) -> RowList`; `label`, `label__lang` |
| `CodelistsScope.rows`, `_FilteredCodelistsScope.rows` | label `COALESCE(lt.text, t.urn)` | `rows(*, lang: Langs) -> RowList`; `label`, `label__lang`; no URN fallback |
| `CodelistsScope.filter` | `filter(*, text_contains)` | `filter(*, text_contains: str, lang: Langs)` |
| `SchemeScope.labels` | `labels(lang="en") -> dict[str, str]`, omits concepts without the tag | `labels(*, lang: Langs) -> Labels[str]`, every concept present |
| `EditionView.agent_labels` | `agent_labels(lang="en") -> dict[str, str]` | `agent_labels(*, lang: Langs) -> Labels[str]` |
| `EditionView.concept_labels` | `concept_labels(lang="en", *, scheme_uri=None) -> dict[str, str]` | `concept_labels(*, lang: Langs, scheme_uri: str \| None = None) -> Labels[str]` |
| `EditionView.spatial_labels` | `spatial_labels(lang="en") -> dict[str, str]` | `spatial_labels(*, lang: Langs) -> Labels[str]` over D5's keys |
| `EditionView.code_map` | `code_map(dataset_id, lang="en") -> dict[str, dict[str, str]]`, ID fallback | `code_map(dataset_id, *, lang: Langs) -> dict[str, Labels[str]]` |
| `CodesScope.get` | `get(code_urn) -> CodeHit \| None`, English first | `get(code_urn, *, lang: Langs) -> CodeHit \| None`; raises `MissingTranslationError` |
| `ConceptsScope.get` | `get(concept_urn) -> ConceptHit \| None`, English first | `get(concept_urn, *, lang: Langs) -> ConceptHit \| None`; name raises, description `None` when absent |
| `EditionView.preference` (new) | | `preference(lang: str) -> tuple[str, ...]`: the tag, then its recorded alternates in tag order. On open.canada.ca, `preference("en") == ("en", "en-t-fr")` |
| `EditionView.language_counts` (new) | | `language_counts() -> list[Row]`: one row per `table`, `column` and `lang`, with `texts`, over the 26 multilingual `statcat` MAP columns, `sdmx.code.name` and `sdmx.localized_text` |

`preference` and `language_counts` settle the RFC's open items on a
preference tuple per locale and tag discovery (finding 11). The docs
recommend `preference(tag) + ("und", "zxx")` for a display UI that should
show untagged text and code labels last.

Domain objects (`get`, `collect`, `.one()`, `ConceptScope.collect`) keep
their `InternationalString` fields.

### Projections, filters and facets (Part A)

| Name | Before | After |
|---|---|---|
| `QueryBuilder.select` | `lang: str \| None`; a MAP column gives `title` | `lang: Langs \| None`; `title` and `title__lang` (D2); `lang` required for a MAP column, as today |
| `QueryBuilder.rows` / `row` | `list[Row]` / `Row \| None` | `RowList` / `Row \| None`; gaps cover the page fetched |
| `QueryBuilder.scalars` | MAP column yields `str` | `lang: Langs \| None`; MAP column yields `LocalisedString \| None` (D3) |
| `QueryBuilder.values` / `value` | `list[object]` / `object \| None` | `ValueList` / `object \| None` |
| `QueryBuilder.sort_by` | `lang: str \| None` | `lang: Langs \| None`; sorts on the first held tag's text |
| `has_spatial` | `(label, *, lang="en")` | `(label, *, lang: Langs)`; compares the first held tag's text of the coverage, then of its concept |
| `has_keyword`, `title_contains`, `description_contains` | `lang="en"` | `lang: Langs`, required |
| `MapCondition.key`, `MapFilter.key` | `str` | `tuple[str, ...]` through `as_langs`; SQL `COALESCE(col[?], col[?], …)` |
| `facets("spatial")` key | `COALESCE(uri, label['en'], label['und'])` | D5 |
| `query(Agent, name=…)` | filters `name['en']` | removed (D6) |

A private SQL helper returns `(text_expr, lang_expr, params)` for a MAP
column and a tag tuple: `COALESCE(m[?], …)` for the text and `CASE WHEN m[?]
IS NOT NULL THEN ? … END` for the tag. It needs no macro, so it works on
read-only connections.

### Search (Part B)

| Name | Before | After |
|---|---|---|
| `Hit.lang` | `str = "en"` | `str`, required: the stored tag of the matched row. Field order unchanged |
| `CodeHit` | `dimension_label: str`, `code_label: str`, `text: str` | `dimension_label: LocalisedString \| None`, `code_label: LocalisedString`, `document: str` |
| `ConceptHit` | `name: str`, `description: str`, `text: str` | `name: LocalisedString`, `description: LocalisedString \| None`, `document: str` (D9) |
| `ReferenceMetadataHit.text` | `str` | renamed `document` |
| `QueryBuilder.search`, `hybrid_search` | `lang="en"` | `lang: str`, required |
| `DatasetsScope.find`, `CodesScope.find`, `ConceptsScope.find`, `ReferenceMetadataScope.find`, `EditionView.search` | `lang="en"` | `lang: str`, required |
| `LanceIndex.table_for` and its four search methods | `lang="en"` | `lang: str`, required |
| Dataset hits | `lang` is the requested tag | the tag of the dataset's best-scoring row |
| Routing | an alternate tag has its own table | alternate-tag text is indexed in the primary's table; `search(lang="en-t-fr")` reads the `en` table |

The index stops inventing English: a dataset is indexed under the union of
the tags its title, description, keywords, themes and dimension names hold
(`store.py:3636`), and a code row under its code label's tags
(`store.py:3812`).

### CLI

No change. No `statcat` command takes a language or prints a label.

## Stored formats

| Format | Change | Reason |
|---|---|---|
| `statcat` `SCHEMA_VERSION` (`store/schema.py:51`, `"2.2"`) | None | No table or column changes; pairs are already stored; facet keys are computed at query time |
| `search/index.json` `version` (`lance.py:74`, `3`) | 3 → 4 | A primary's table now holds its alternates' text, so a row's `lang` is no longer the table's language (`docs/catalog-format.md` §4.2); `index.json` gains `language_pairs`, `{"pairs": {alternate: primary}, "fingerprint": <sha256>}` |
| Search table columns | `text` → `document`; add `concept_name`, `concept_description` | D9 |
| `text_sha256` | Unchanged | `build_index` reuses carried vectors by it, so the upgrade re-embeds only rows whose document changed |
| `statcat` section (`STATCAT_SECTION_FORMAT = 3`, `bundle.py:68`) | Format unchanged; `search.index_version` becomes `4` | The section copies the version; no field changes meaning. The fingerprint lives in `index.json` (RFC decision 11) |
| Contact and agent IDs, Inventory, sdmx store schema | None | |

When the stored pairs' fingerprint differs from `index.json`'s, `open()`
warns and `check_bundle` reports until `build_index()` runs, as the
annotation fingerprint does (GH-3).

### Migration

*   `Product.view` refuses an edition whose `index.json` records version 3
    (`store.py:242`, `IncompatibleCatalogError`). A working copy opens over
    it, since `working_copy.py:1172` checks only the `statcat` schema. Each
    product needs one new edition: open a working copy, `build_index()`,
    seal, release. No re-ingest; carried vectors are reused.
*   CKAN text under `en-t-fr` and `fr-t-en` moves into the `en` and `fr`
    tables at that rebuild.
*   Spatial facet keys of rows without a URI change from English labels to
    `und` or `zxx` text.
*   Code that relied on a default `lang`, on `str` labels, on `.lang()`, on
    ID fallbacks or on `hit.payload.text` changes as the tables above show.

## Cross-repo contract

This design keeps the suite contract: `LocalisedString` and its JSON form
(hub D6, D9), `InvalidTagError` and `MissingTagError` with statcatlib's
error subclassing sdmxlib's (D7), `tag_alias` in generated names (D8) and
`__lang` for the answering tag (D13). It adds no contract that sdmxlib or
sdmxlib-rest must follow.

### Hub design amendments

The hub session applies these to `docs/design/localised-string.md`:

*   **D9:** statcatlib adds msgspec to its `dev` dependency group only. Its
    source never serialises a `LocalisedString` (this design's D10).
*   **D4:** add that statcatlib's `.scalars()` over a MAP column returns
    `LocalisedString | None` per value; `Row` projection columns stay text
    with a `__lang` column (this design's D2 and D3).
*   **Status and Staging:** Stages 4 and 5 built in statcatlib 0.21.0, once
    released.

## Work order

### Part A: the lookup API

1.  `docs(design)`: this design; `language-tags.md` status updated.
2.  `docs(design)`: the audit table (D11).
3.  `feat!(labels)`: `_langs.as_langs`, `Langs`, `Gap`, `Labels`,
    `RowList`, `ValueList`, `MissingTranslationError`, the SQL helper, the
    exports.
4.  `feat!(scopes)`: `label`, `batch_labels`, `labels`, enriched `rows`,
    `filter`; remove `.lang()`.
5.  `feat!(store)`: `agent_labels`, `concept_labels`, `spatial_labels`,
    `code_map`, `CodesScope.get`, `ConceptsScope.get`, `preference`,
    `language_counts`; remove the English-first fallbacks.
6.  `feat!(query)`: `select`, `scalars`, `sort_by`, `rows`, `values`; the
    filters, `MapCondition` and `MapFilter`.
7.  `fix!(store)`: D5 and D6.
8.  `fix(sdmx)`: D13.
9.  `docs`: Part A's surface in upgrading, API, tutorial, README,
    `llms.txt`, CLAUDE.md and the EGM files.

### Part B: search

10. `feat!(search)`: required `lang`; `Hit.lang` from the row; dataset hits
    from their best row.
11. `feat!(search)`: index tags from the stored maps, with no invented
    `en`.
12. `feat!(search)`: pairs in `_by_language` and `_for`; `index.json`
    version 4 with `language_pairs`; the stale-pairs warning and check;
    `_table_name` calls `tag_alias`.
13. `feat!(search)`: `document`, `concept_name`, `concept_description`;
    the hit payload fields.
14. `docs`: `catalog-format.md` §4, `search.md`, upgrading, CLAUDE.md,
    `store/EGM.md`; `language-tags.md` status to built.

### Tests

New `tests/test_language_tags.py` (statcatlib's tests are flat):

*   Every public callable with a `lang` or `langs` parameter, found with
    `inspect` as sdmxlib's
    `test_every_public_lang_parameter_refuses_a_malformed_tag` does, refuses
    `fr_CA` with `InvalidTagError` and has no default for `lang`. Each is
    registered in a `_calls` table, so a new parameter fails until listed.
*   `tag_alias(t) == t.strip().lower().replace("-", "_")` for sdmxlib's
    well-formed fixtures, copied with the sdmxlib tag noted, and every
    two-letter ASCII pair.
*   `msgspec.json.encode` of a returned label is `{"text":…,"lang":…}` in
    that order.
*   `MissingTranslationError` is a `MissingTagError` and a `KeyError`, with
    the message and attributes above.
*   Tag tuples: first held wins, duplicates dropped, empty raises.

Elsewhere:

*   `batch_labels`: unknown ID raises `KeyError`; untranslated is `None`
    with one `Gap`; `("fr-ca", "fr")` against a code holding `fr` returns
    `lang="fr"`.
*   Projections: `title` and `title__lang`; `None` and a gap for a missing
    title; `scalars` returns `LocalisedString | None`.
*   Facets: no spatial key is an English label; a `und` key round-trips
    through `has_spatial(…, lang="und")`.
*   Search: `Hit.lang` is the row's tag; with pairs `{"en": ["en-t-fr"]}`,
    `search(lang="en")` finds `en-t-fr` text with `hit.lang == "en-t-fr"`;
    changed pairs warn on open and fail `check_bundle`; a version 3
    `index.json` is refused; a rebuild over a carried version 3 index
    reuses its vectors.
*   SDMX ingest: an agency named `""` is labelled `{zxx: id}`.

Sixteen existing test files call an affected API; each passes `lang=` and
reads `.text`.

### Docs

*   `docs/upgrading.md`: a 0.21.0 section covering required `lang`, return
    types and errors, gaps, projection columns, search pairs and the
    re-index.
*   `docs/api/store.md`, `docs/search.md`, `docs/tutorial.md`, `README.md`,
    `llms.txt`, `docs/catalog-format.md` §4.1–4.2.
*   CLAUDE.md "Query surface" and the search-index section.
*   `src/statcatlib/EGM.md`, `store/EGM.md`, `sources/EGM.md`.
*   CHANGELOG from the conventional commits.

### Size

About 80 source sites in `store/store.py`, `store/scopes.py`,
`store/lance.py`, `query.py`, `sources/sdmx.py`, `bundle.py` and
`__init__.py`: roughly 1,500–2,000 source lines, 1,000–1,500 test lines and
600–900 doc lines. Part A is about 60% of it.

## Downstream impact

*   **statcat-serve** (`42ad7ad`) pins `statcatlib>=0.18,<0.19` and calls no
    label or search API. Its pin moves to 0.21 when Phase 6 starts.
*   **sdmxlib-rest** depends on sdmxlib only and does not read `search/`.
*   **Hub docs:** `docs/domain-model.md:141` calls `cat.code_map(...)` on a
    `Catalog`, which is already wrong at 0.20 (`code_map` is on
    `EditionView` and takes a dataset identifier) and needs `lang=` after
    this work. The version list in `docs/architecture.md` gains 0.21.0
    with the re-index note.

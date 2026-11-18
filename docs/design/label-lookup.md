# Label lookup: return LocalisedString, require `lang`, add language pairs to search

*   **Author:** Gabriel Gellner
*   **Date:** 2026-11-18
*   **Status:** Draft, revision 2. Revision 1 had an EGM review
    ([report](egm/label-lookup/goldfish_review_report.md): 1 Blocker, 19
    Major, 22 Minor). The project owner then decided four findings on
    2026-11-18 (LL-D5, LL-D6, LL-D14 and LL-D15 below). This revision applies
    those decisions and every other finding; "Review response" maps each
    finding to its change. Code starts once the owner accepts this revision.
*   **Issue:** GH-91: Stages 4 and 5 of the suite design, the issue's list of
    single-repository items, and [its comment on the contact
    ID](https://github.com/StatCat-Suite/statcatlib/issues/91). Together with
    slice 4 of the RFC [Language tags and label lookup](language-tags.md).
    GH-88 closes with this work.
*   **Suite design:** [Text model: add LocalisedString per the SDMX
    Information
    Model](https://statcat-suite.github.io/design/localised-string/) in the
    StatCat Suite hub. This design builds its Stages 4 and 5 and proposes the
    amendments under "Amendments to other designs".
*   **Read against:**
    *   statcatlib `2e5055a` (`origin/main`, v0.20.0, on `sdmxlib>=0.89,<0.90`).
        Every `file:line` is at this commit unless it names another.
    *   sdmxlib `v0.89.0` (`f49a4fc`): `src/sdmxlib/model/istring.py`,
        `src/sdmxlib/__init__.py` and `pyproject.toml`, read with `git show`.
        The revision 1 reviewers read 0.88.0; every sdmxlib claim below was
        checked again at `v0.89.0`.
    *   statcat-serve `42ad7ad` and the hub at `2ef02da`.

## Overview

### The problem

statcatlib returns labels as `str`, so a caller cannot tell which language
answered. When a translation is missing it falls back to English or to an
identifier without saying so, and 37 parameters default `lang` to `"en"`.
Search stores text under an alternate tag such as `en-t-fr` in a table of its
own, so an English search never finds it. A multilingual UI or chatbot built
on statcatlib needs the tag of each label and a list of the missing ones.

### The change

The RFC settled the rules (LT-4 to LT-12): no default language, exact
matching over a tag or an ordered tuple, labels that carry the tag that
answered, an error from single lookups, `None` and a gap list from bulk
calls, and language pairs in search. sdmxlib 0.79 shipped the value type,
`LocalisedString(text, lang)`, and `InternationalString` as a
`Mapping[str, LocalisedString]`.

This design builds the statcatlib side as one PR of ordered commits,
released as 0.21.0 with no compatibility shims (LL-D14):

*   **The lookup API.** Required `lang`, tag tuples, `LocalisedString`
    returns, `MissingTranslationError`, gap lists, `title` and `title__lang`
    projection columns, and language-neutral facet keys.
*   **Search.** Required `lang`, hits tagged with the stored tag of the
    matched text, language pairs in the index, and search index format
    version 4, which 0.21 reads beside version 3 for everything but search
    (LL-D15).

### Terms

*   **Decision prefixes.** `LL-Dn` is a decision of this design. `LS-Dn` is a
    decision of the hub's suite design. `LT-n` is a decision of the RFC
    `language-tags.md`, and `LT finding n` one of its findings.
*   **Tag:** a BCP 47 language tag in the lowercase form
    `sdmxlib.normalize_tag` returns, such as `en` or `fr-ca`. GH-88's
    contract is that every stored tag has this form.
*   **Tag tuple:** an ordered sequence of tags; the first one held answers.
*   **MAP column:** a DuckDB `MAP(VARCHAR, VARCHAR)` column from tag to text.
    The `statcat` schema has 26 of them, plus `dataset.extras`, whose keys
    are not tags.
*   **Held tag:** a key of a stored MAP.
*   **`und`:** the tag of CKAN text sent with no language. **`zxx`:** the tag
    of a code or identifier stored as text (LT-3).
*   **Gap:** a label a bulk call could not answer in any requested tag.
*   **Language pair:** an alternate tag mapped to its primary tag, such as
    `en-t-fr` (English translated from French, RFC 6497) to `en`, recorded in
    `statcat.language_alternate` (`store/schema.py:353-360`).
*   **Document:** the composite text one search row is indexed under, built
    from a title, labels and context in one tag.
*   **Stage n:** a stage of the hub's suite design; Stage 5 is narrower than
    slice 4 (hub lines 605–607). **Slice n:** a slice of the RFC's staging.
*   **Working copy, seal, release:** a working copy is a writable edition
    under construction; sealing makes it an immutable edition; the release
    job makes an embargoed edition live (`docs/catalog-format.md` §1).
*   **Carried vectors:** the embeddings a new working copy reuses from its
    previous edition's `search/`, matched by `text_sha256`.
*   **Terminal:** a scope or builder method that runs the query, such as
    `rows()` or `batch_labels()`.

## Goals

*   No `lang` parameter defaults to a tag. Label lookups take a tag or a tag
    tuple; search takes one tag. `select`, `scalars` and `sort_by` keep
    `lang=None` for non-MAP columns and raise `ValueError` for a MAP column
    given no `lang`, as today.
*   Every label returned carries the tag that answered.
*   A single lookup raises `MissingTranslationError` for a missing required
    label; a bulk or paged call returns `None` and lists every gap.
*   Every read path uses only the tags the caller passes and the tags a
    stored map holds. A facet key is a URI or a hash, in no language.
*   Search indexes alternate-tag text in its primary tag's table, and each
    hit reports the stored tag of its text.
*   Meet the acceptance criteria of GH-91 and of the suite design's Stages
    4–5 ("Acceptance map").

## Non-goals

*   RFC 4647 range matching, and display-case tags.
*   JSON or VARIANT columns.
*   Per-source refusal counts in `.gaps` (LT finding 13). CKAN ingest already
    logs each refused tag per source; `.gaps` lists missing labels at read
    time.
*   A `.frame()` method. statcatlib has none; one added later uses the
    columns of LL-D2.
*   The CKAN frequency ID, which `_parse_frequency` derives from the `en`
    text (`sources/ckan.py:451`). It is an ingest identifier, outside the
    read paths of Goal 4; a follow-up issue covers it.
*   The read cost of `_map_to_istring`: 59 calls rebuild a stored map with
    `InternationalString.of()`, which validates every tag again, where
    sdmxlib's store readers use the private `_from_normalized`. A later issue
    can ask sdmxlib for a public trusted constructor.

## Open questions

None. The RFC's two open items are settled here: the projection columns and
`.frame()` by LL-D2, and the preference tuple and tag discovery by
`EditionView.preference` and `EditionView.language_counts`.

## Conventions

These rules hold for every call in "Public API".

*   **`Langs`**, exported as `type Langs = str | Sequence[str]`
    (`requires-python >= 3.14`). A bare `str` is one tag. Each tag is
    normalised, then duplicates are dropped keeping the first. An empty
    sequence raises `ValueError`; a non-`str` item raises `TypeError`; a
    malformed tag raises `sdmxlib.InvalidTagError`. The first held tag
    answers, as `InternationalString.pick` does; unlike `pick()`, which
    raises `TypeError` with no tags, an empty sequence is a `ValueError`.
*   **Empty text answers.** A held `""` is an answer, never a gap, as
    `pick` and SQL `COALESCE` behave. LL-D13 keeps empty names out of ingest.
*   **Keyword-only.** Every `lang` parameter is keyword-only and required,
    except the three projection modifiers above.
*   **Search takes one tag** (LL-D7).
*   **Single lookups** return `LocalisedString` and raise
    `MissingTranslationError` when the entity's required label has none of
    the tags (LL-D8). An unknown entity returns `None`, as today.
*   **Bulk and paged calls** return `None` per missing label and list gaps
    (LL-D1, "Gaps"). They raise only for a caller bug: `batch_labels` raises
    `UnknownCodeError` naming every unknown code ID (LT finding 10: an
    unknown code and an untranslated code are different misses).
*   **Tables carry text; everything else carries `LocalisedString`.** A
    `Row` column holds text with a `__lang` column beside it (LS-D13); a
    mapping, a scalar list, a single lookup and a hit field hold
    `LocalisedString`.

## Decisions

The project owner decided LL-D1 to LL-D4 and LL-D7 to LL-D13 on 2026-11-18,
accepting the plan's recommendations, and decided LL-D5, LL-D6, LL-D14 and
LL-D15 on 2026-11-18 after the review. Each entry gives the reason;
"Public API" gives the signatures.

**LL-D1. Bulk result containers.** Decided by the owner, 2026-11-18.
`Labels[K]`, `RowList` and `ValueList` each have a `gaps` attribute, and one
`Gap` type serves all three. Results keep their shape: `for k, v in
labels.items()` works as on a `dict`.

**LL-D2. Projection columns.** Decided by the owner, 2026-11-18. A selected
MAP column gives two `Row` columns, `title` (text or `None`) and
`title__lang` (the tag that answered, or `None`). `__lang` is the suite
suffix (LS-D13). A future column per tag is `<column>_<tag_alias>`
(`title_fr_ca`, LS-D8). An enrich alias that equals an entity column or a
`__lang` column raises `ValueError`.

**LL-D3. `.scalars()` over a MAP column** yields `LocalisedString | None`.
Decided by the owner, 2026-11-18. One column has no room for `__lang`.

**LL-D4. Codelist scopes lose `.lang()`.** Decided by the owner, 2026-11-18.
Passing `lang=` to each terminal gives one way to supply a tag. A setter with
no default would make every terminal raise until the setter runs, and would
add a second way.

**LL-D5. Spatial facet key.** Decided by the owner, 2026-11-18 (revised
after M10). The key is language-neutral: the coverage's `uri` when it has
one, otherwise `sha256:` followed by the lowercase hex SHA-256 of the
canonical JSON of its whole label map. The canonical JSON is a JSON array of
`{"key": <tag>, "value": <text>}` objects sorted by tag, UTF-8, with no
whitespace, exactly as DuckDB writes `to_json(list_sort(map_entries(label)))`.
The key is computed only in SQL, from one expression in
`store/_lang_sql.py`, so no second implementation can drift; a test pins the
hash of a fixture map holding accents and a quote. Labels for every key come
back per language through `spatial_labels(lang=)`, like every other label.
Two coverages with the same label map share a key, as they shared an English
key before. `has_spatial_key(key)` filters by either key form and replaces
`has_spatial_uri`.

**LL-D6. Entity filters.** Decided by the owner, 2026-11-18 (with M11).
The agent `name` filter (`name['en']`, `store.py:1057`) is removed, since
agents are identified by `id` (GH-94). `query(Agent, …)`,
`query(DataService, …)` and `query(Distribution, …)` raise `ValueError` for
a filter key their spec does not list, naming the keys it does
(`store.py:6346-6350` skips one today).

**LL-D7. Search takes one tag.** Decided by the owner, 2026-11-18. One table
and one stemmer answer a query, so `lang` is a `str`, and a sequence raises
`TypeError`. "Search rows" gives the routing of an alternate tag.

**LL-D8. Single lookups raise for required labels only.** Decided by the
owner, 2026-11-18. A code's or concept's name raises
`MissingTranslationError`; an optional field, such as a concept's
description, returns `None`.

**LL-D9. Index columns.** Decided by the owner, 2026-11-18. Index format
version 4 renames `text` to `document` and adds `concept_name` and
`concept_description`, so `ConceptHit` reads its fields from columns and
stops splitting the document on `" | "`. `text_sha256` keeps its name and
value. This decision rides on the version 4 bump (LL-D15); without the bump
the rename would need its own.

**LL-D10. msgspec is a dev dependency only.** Decided by the owner,
2026-11-18. statcatlib's writers serialise labels through `as_dict()`, and
the CLI prints no labels, so only the JSON-order test needs msgspec. At
runtime msgspec arrives through sdmxlib (`msgspec>=0.22.0`, sdmxlib
`pyproject.toml:40` at `v0.89.0`).

**LL-D11. Audit table.** Decided by the owner, 2026-11-18. sdmxlib's audit
hook (`SDMXLIB_ISTRING_AUDIT=1`, which logs each `file:line` that reads an
`InternationalString`) runs on this branch before the API commits. Its
de-duplicated `src/` sites are committed as
`docs/design/egm/localised-string/call-sites.md`, whose header names the
audited commit. Stage 4 shipped in `04b27ac`, so the table lists the sites
this work changes.

**LL-D12. Review before code.** Decided by the owner, 2026-11-18. This
design is reviewed before any code; the Status line records the rounds.

**LL-D13. Empty names at SDMX ingest.** Decided by the owner, 2026-11-18
(widened after m14). Ingest drops a tag whose text is empty or whitespace
from an agency name and from a dimension's concept name. When no tag
remains, the label is `{zxx: id}`, as for an absent name today
(`sources/sdmx.py:153, 163-168`). `{en: "", fr: "X"}` becomes `{fr: "X"}`.

**LL-D14. One PR.** Decided by the owner, 2026-11-18 (revised after M2 and
M3). One branch, one PR of ordered commits, merged once and released as
0.21.0. Every commit leaves `just ci` green. The hit field types change in
the commit that changes `get()`. GH-88 closes with the PR.

**LL-D15. Index version 3 stays readable except for search.** Decided by the
owner, 2026-11-18 (after B1 and M1). 0.21 reads an index of version 3 or 4
and refuses any other version:

*   `Product.view`, `EmbargoReader`, the release fold's reference check,
    upload's verify and `verify-catalog` / `check_catalog` accept a version 3
    index and check its layout.
*   A writable store and a working copy open a carried version 3 index to
    read its vectors, and the seal always rebuilds it as version 4.
*   Only a search call over a version 3 index raises
    `IncompatibleCatalogError`, naming the fix: open a working copy, call
    `build_index()`, seal.

Every reader is upgraded to 0.21 before any product seals a version 4
edition, the usual rollout for a format bump ("Breaking changes and
migration").

## Public API

### New public types

All are in a new public module, `statcatlib.labels`, and exported from
`statcatlib`. `as_langs` is private there as `_as_langs`.

| Name | Definition |
|---|---|
| `Langs` | `type Langs = str \| Sequence[str]` |
| `MissingTranslationError` | `class MissingTranslationError(sdmxlib.MissingTagError)`, so also a `KeyError`. `__init__(entity: str, field: str, langs: Sequence[str], held: Sequence[str])` calls `super().__init__(langs, held)` and sets `entity` and `field`. `__str__`: `no text for 'de', 'it' in name of urn:…:CL_GEO(1.0); held: 'en', 'fr'`. `__reduce__` returns the four arguments, so it pickles. |
| `UnknownCodeError` | `class UnknownCodeError(KeyError)`; `__init__(codelist_urn: str, codes: Sequence[str])`; attributes `codelist_urn`, `codes` (a tuple); `__str__` names both; `__reduce__` as above. |
| `Gap` | attrs frozen: `key: object` (the result key it belongs to), `entity: str`, `field: str`, `langs: tuple[str, ...]` (the `_as_langs` tuple). The held tags are on `MissingTranslationError` only, so a bulk call reads no `map_keys`. |
| `Labels[K]` | `Mapping[K, LocalisedString \| None]`; `Labels(items: Iterable[tuple[K, LocalisedString \| None]], gaps: Iterable[Gap])`; immutable; `gaps: tuple[Gap, ...]`. |
| `RowList` | `list[Row]` subclass with `gaps: tuple[Gap, ...]`. |
| `ValueList` | `list[object]` subclass with `gaps: tuple[Gap, ...]`. |

`statcatlib` also re-exports `LocalisedString`, `MissingTagError` and
`InvalidTagError` from sdmxlib (exported there at `__init__.py:162-181`)
beside `InternationalString`.

The containers' limits, stated in their docstrings: `gaps` describes the
result as returned, so it drifts if a caller mutates a `RowList` or
`ValueList`; slicing, `copy()` and `+` return a plain `list`; `==` ignores
`gaps`; `json.dumps` refuses a `Labels` (a `Mapping`, not a `dict`) and a
`LocalisedString` (use `msgspec.json.encode` or `as_dict()`).

### Errors

| Error | Raised by | When |
|---|---|---|
| `sdmxlib.InvalidTagError` (`ValueError`) | every `lang` parameter, `LanceIndex` lookups | a malformed tag |
| `TypeError` | every `lang` parameter; search | a non-`str` tag; a sequence passed to search |
| `ValueError` | `Langs` parameters; projections; entity queries; `enrich` | an empty tag sequence; a MAP column given no `lang`; an unknown filter key (LL-D6); an alias collision (LL-D2) |
| `MissingTranslationError` | `CodelistScope.label`, `CodesScope.get`, `ConceptsScope.get` | the entity exists and its required label holds none of the tags |
| `UnknownCodeError` | both `batch_labels` | a requested code ID the codelist does not hold, or no `sdmx` schema |
| `IncompatibleCatalogError` | every search entry point | the edition's index is version 3 (LL-D15) |

`MissingTranslationError` and `UnknownCodeError` are both `KeyError`s, and so
`LookupError`s. A caller catches each by name; `except KeyError` would catch
both.

### Label lookups

| Name | Before | After |
|---|---|---|
| `CodelistScope.label` | `label(lang="en") -> str \| None` | `label(*, lang: Langs) -> LocalisedString \| None`; `None` for an unknown codelist or no `sdmx` schema, as today; raises `MissingTranslationError` |
| `CodelistScope.batch_labels` | `(code_ids, lang=None) -> dict[str, str]`, ID fallback | `(code_ids, *, lang: Langs) -> Labels[str]` in request order, duplicates collapsed; raises `UnknownCodeError`; an untranslated code is `None` and a gap |
| `CodelistsScope.batch_labels` | `(pairs, lang=None) -> dict[tuple[str, str], str]` | `(pairs, *, lang: Langs) -> Labels[tuple[str, str]]`, same rules |
| `.lang()` on `CodelistScope`, `CodelistsScope`, `_FilteredCodelistsScope` | setter, `_lang="en"` | removed (LL-D4) |
| `CodelistScope.rows` | label `COALESCE(name[?], code_id)` | `rows(*, lang: Langs) -> RowList`; `label`, `label__lang` |
| `CodelistsScope.rows`, `_FilteredCodelistsScope.rows` | label `COALESCE(lt.text, t.urn)` | `rows(*, lang: Langs) -> RowList`; `label`, `label__lang`; no URN fallback |
| `CodelistsScope.filter` | `filter(*, text_contains)` | `filter(*, text_contains: str, lang: Langs)`; matches the first held tag's text, or the URN, as today |
| `SchemeScope.labels` | `labels(lang="en") -> dict[str, str]`, omits concepts without the tag | `labels(*, lang: Langs) -> Labels[str]`, every concept, `ORDER BY notation` |
| `SchemeScope.rows` | the raw `label` MAP | `rows(*, lang: Langs) -> RowList`; `label`, `label__lang` |
| `EditionView.agent_labels` | `agent_labels(lang="en") -> dict[str, str]` | `agent_labels(*, lang: Langs) -> Labels[str]`, `ORDER BY id` |
| `EditionView.concept_labels` | `concept_labels(lang="en", *, scheme_uri=None)` | `concept_labels(*, lang: Langs, scheme_uri: str \| None = None) -> Labels[str]`, `ORDER BY uri` |
| `EditionView.spatial_labels` | `spatial_labels(lang="en") -> dict[str, str]` | `spatial_labels(*, lang: Langs) -> Labels[str]` over LL-D5's keys, `ORDER BY key` |
| `EditionView.code_map` | `code_map(dataset_id, lang="en") -> dict[str, dict[str, str]]`, ID fallback | `code_map(dataset_id, *, lang: Langs) -> dict[str, Labels[str]]`; each dimension's `Labels` holds its gaps |
| `CodesScope.get` | `get(code_urn) -> CodeHit \| None`, English first | `get(code_urn, *, lang: Langs) -> CodeHit \| None`; raises `MissingTranslationError` |
| `ConceptsScope.get` | `get(concept_urn) -> ConceptHit \| None`, English first | `get(concept_urn, *, lang: Langs) -> ConceptHit \| None`; name raises, description `None` when absent |
| `EditionView.preference` (new) | none | `preference(*, lang: str) -> tuple[str, ...]` |
| `EditionView.language_counts` (new) | none | `language_counts() -> list[Row]` |

**`preference(lang=t)`.** For a primary tag `t`: `(t, *alternates)`, the
alternates in tag order (`ORDER BY alternate_tag`). For an alternate `t`:
`(t, primary, *the primary's other alternates)`. For a tag in no pair:
`(t,)`. On open.canada.ca, `preference(lang="en") == ("en", "en-t-fr")`. The
docs recommend `(*view.preference(lang="fr"), "und", "zxx")` for a display UI
that shows untagged text and code labels last.

**`language_counts()`.** One `Row` per (`table`, `column`, `lang`) with
`texts`, the number of non-NULL texts held under that tag, ordered by
`table`, `column`, `lang`. `table` is schema-qualified (`statcat.dataset`).
The columns are every MAP column of the `statcat` schema found in
`information_schema.columns`, except `dataset.extras`, plus `sdmx.code.name`,
`sdmx.concept.name`, `sdmx.concept.description`, and `sdmx.localized_text`
with `column` set to its `field`. The sdmx list is a constant, because the
`sdmx` schema also has MAP columns that are not text (`dimension_column_map`,
sdmxlib `storage/schema.py:935-937`).

**`get()` hits.** `CodesScope.get` returns `CodeHit` with `dataset_id`,
`dimension_id` and `document` empty, `dimension_label=None`, and
`code_label` the picked name. `ConceptsScope.get` returns `ConceptHit` with
`document` empty. No search row is read.

Domain objects (`get`, `collect`, `.one()`, `ConceptScope.collect`) keep
their `InternationalString` fields.

### Projections, filters and facets

| Name | Before | After |
|---|---|---|
| `QueryBuilder.select` | `lang: str \| None`; a MAP column gives `title` | `lang: Langs \| None`; `title` and `title__lang` (LL-D2) |
| `QueryBuilder.rows` | `list[Row]` | `RowList`; gaps cover the page fetched |
| `QueryBuilder.row` | `Row \| None` | unchanged type; `rows()[0]`, so its gaps are not returned |
| `QueryBuilder.scalars` | MAP column yields `str` | `lang: Langs \| None`; MAP column yields `LocalisedString \| None` (LL-D3) |
| `QueryBuilder.values` | `list[object]` | `ValueList` |
| `QueryBuilder.value` | `object \| None` | unchanged type; `None` means no row or no text; use `values()` to tell them apart |
| `QueryBuilder.sort_by` | `lang: str \| None` | `lang: Langs \| None`; sorts on the first held tag's text, so one order can mix languages |
| `has_spatial` | `(label, *, lang="en")` | `(label, *, lang: Langs)`; true when the coverage's first held text equals `label`, or its concept's does |
| `has_spatial_uri` | `(uri)` | removed; `has_spatial_key(key)` takes a URI or a `sha256:` key |
| `has_keyword` | `lang="en"` | `lang: Langs`; equality with the first held text |
| `title_contains`, `description_contains` | `lang="en"` | `lang: Langs`; containment in the first held text |
| `MapCondition.key`, `MapFilter.key` | `str` | `tuple[str, ...]` through `_as_langs` |
| `facets("spatial")` key | `COALESCE(uri, label['en'], label['und'])` | LL-D5 |
| `query(Agent, name=…)` | filters `name['en']` | raises `ValueError` (LL-D6) |

### Gaps

`langs` is always the `_as_langs` tuple of the call.

| Call | `key` | `entity` | `field` | Gap when |
|---|---|---|---|---|
| `CodelistScope.batch_labels` | code ID | codelist URN | `name` | the code holds none of the tags |
| `CodelistsScope.batch_labels` | `(codelist URN, code ID)` | codelist URN | `name` | same |
| `CodelistScope.rows` | code ID | codelist URN | `name` | same |
| `CodelistsScope.rows`, `_FilteredCodelistsScope.rows` | codelist URN | codelist URN | `name` | the codelist's name holds none |
| `SchemeScope.labels`, `SchemeScope.rows` | notation | concept URI | `label` | the concept's label holds none |
| `agent_labels` | agent ID | agent ID | `name` | the name holds none |
| `concept_labels` | concept URI | concept URI | `label` | same |
| `spatial_labels` | facet key | facet key | `label` | neither the concept nor any coverage under the key holds one |
| `code_map` (each dimension) | code ID | codelist URN | `name` | the code holds none |
| `QueryBuilder.rows`, `values` | dataset identifier | dataset identifier | selected MAP column | the column holds none, `description` included |

Example: `Gap(key="QC", entity="urn:…:CL_GEO(1.0)", field="name",
langs=("de",))`. `facets()` keeps its `dict[str, dict[str, int]]` shape and
has no gaps: under LL-D5 every coverage has a key.

### SQL helpers

`statcatlib/store/_lang_sql.py` holds them, with positional `?` parameters
only. `CodesScope.get` and `ConceptsScope.get` change from `$code_urn` to
`?`.

```python
@define(frozen=True)
class PickSql:
    text: str                     # COALESCE(col[?], col[?], …)
    text_params: tuple[str, ...]  # the tags
    lang: str                     # CASE WHEN col[?] IS NOT NULL THEN ? … END
    lang_params: tuple[str, ...]  # each tag twice

def pick_map(column: str, langs: tuple[str, ...]) -> PickSql: ...
def localized_map(field: str) -> tuple[str, tuple[str, ...]]: ...
SPATIAL_KEY_SQL: str              # LL-D5's key over a dataset_spatial alias
```

`localized_map` returns a subquery that aggregates `sdmx.localized_text` rows
into one MAP per owner, as `store.py:3768-3773` does, so codelist names go
through `pick_map` too. The helper needs no macro, so it works on read-only
connections, which cannot create a persistent one (LT, "Smaller points").
For `spatial_labels`, a URI key takes its text from the concept first, then
from its coverages, picked with `arg_min({text, lang}, (rank, text))`, so
text and tag come from one row.

### Search

| Name | Before | After |
|---|---|---|
| `Hit.lang` | `str = "en"` | `str`, required: the stored tag of the matched row |
| `CodeHit` | `dimension_label: str`, `code_label: str`, `text: str` | `dimension_label: LocalisedString \| None`, `code_label: LocalisedString`, `document: str` |
| `ConceptHit` | `name: str`, `description: str`, `text: str` | `name: LocalisedString \| None`, `description: LocalisedString \| None`, `document: str` (LL-D9) |
| `ReferenceMetadataHit.text` | `str` | renamed `document` |
| `QueryBuilder.search`, `hybrid_search` | `lang="en"` | `lang: str`, required |
| `DatasetsScope.find`, `CodesScope.find`, `ConceptsScope.find`, `ReferenceMetadataScope.find`, `EditionView.search` | `lang="en"` | `lang: str`, required |
| `LanceIndex.table_for`, `bm25_search`, `bm25_search_rows`, `search`, `search_rows` | `lang="en"` | `lang: str`, required |
| `LanceIndex.bm25_search`, `search` | `-> list[tuple[str, float]]` | `-> list[tuple[str, float, str]]` (id, score or distance, lang) |
| `_Store._rrf_rank` (private) | `-> dict[str, float]` | `-> dict[str, tuple[float, str]]` |
| `LanceIndex.add_bulk` | `(rows, …)` | `(rows, *, pairs: Mapping[str, str], …)` |
| `LanceIndex.table_for_key` (new) | none | `table_for_key(key: str)`: the table under an `index.json` key, for checks |

`ConceptHit.name` is optional because a concept's description can hold a tag
its name lacks; `ConceptsScope.get` never returns `None` there (LL-D8).

### Search rows

*   **Tags.** A dataset row exists for each tag its title, description,
    keywords, themes and dimension names hold, `und` included and `zxx`
    excluded, whose document is not empty (`store.py:3636` today adds an
    `en` tag when the title and description hold none). A code row exists
    for each tag its code label holds, `zxx` excluded (`store.py:3812` today
    takes the tags of the dataset title). A concept row exists for each tag
    its name or description holds, `zxx` excluded. Reference-metadata rows
    keep today's tags.
*   **Text.** A row's document and its other text columns hold only that
    tag's text: `dimension_label`, `concept_name` and `concept_description`
    are `""` when the tag has none, and the hit reads `""` as `None`.
*   **`lang`.** A row's `lang` column holds its own tag, also when the row
    sits in its primary's table.
*   **Tables.** Rows are grouped by `pairs.get(tag, tag)`. A table is named
    `search_<tag_alias(primary)>__<edition>`, aliased once inside
    `_table_name`, and keyed by that alias in `index.json`. Today
    `_by_language` aliases before `_table_name` sees the tag
    (`lance.py:1297`), so `_table_name` cannot call `tag_alias` on its input;
    grouping by the tag fixes that.
*   **Routing.** `_for(lang)` normalises the tag, raising `InvalidTagError`
    for a malformed one (today it returns `None`, `lance.py:1206-1211`), maps
    an alternate to its primary through `index.json`'s `language_pairs`, and
    returns that table, or `None` for a tag with no table, which gives an
    empty result. `search(lang="en-t-fr")` therefore reads the `en` table and
    returns hits under both tags.
*   **Duplicates.** A code or concept that `en` and `en-t-fr` both hold has
    two rows in one table, with one `id` (`store.py:3846, 3936`).
    `bm25_search_rows` and `search_rows` deduplicate by entity (code: its
    `code_urn`, else `(id, code_id)`; concept: `concept_urn`;
    reference metadata: `id`), keeping the best-ranked row and its `lang`.
    `bm25_search` and `search` deduplicate by `id` as today and return the
    kept row's `lang`. `add()` receives every row of an `id` in one call,
    since `upsert` deletes by `id` (`lance.py:918-927`); `build_index` uses
    `add_bulk` and rebuilds every table.
*   **Dataset hits.** `Hit.lang` is the `lang` of the dataset's best row. In
    hybrid mode it comes from whichever of the BM25 and vector rankings ranks
    the dataset higher, BM25 on a tie.

Example, open.canada.ca with pairs `{"en-t-fr": "en", "fr-t-en": "fr"}`: a
dataset with title `{en-t-fr: "Population estimates", fr: "Estimations de la
population"}` and description `{en: "Annual estimates", fr: "Estimations
annuelles"}` has three rows. Rows `en-t-fr` and `en` go to `search_en__…`,
row `fr` to `search_fr__…`. `datasets.find("population", lang="en")` matches
the `en-t-fr` row, so the hit's `lang` is `en-t-fr`.

### CLI

No change. No `statcat` command takes a language or prints a label.

## Stored formats

| Format | Change | Reason |
|---|---|---|
| `statcat` `SCHEMA_VERSION` (`store/schema.py:51`, `"2.2"`) | None | No table or column changes. Pairs are already stored. Facet keys are computed at query time |
| `search/index.json` `version` (`lance.py:74`) | 3 → 4 | A primary's table holds its alternates' rows, so a row's `lang` is no longer the table's language (`catalog-format.md` §4.2); `index.json` gains `language_pairs` |
| `index.json` `language_pairs` | new, always present: `{alternate_tag: primary_tag}`, normalised tags, `{}` when none | the routing source for `_for` |
| `index.json` `languages` | keys stay `tag_alias` of the table's tag, now always a primary or unpaired tag | `languages`, `describe()` and `index_layout` list primary tags only |
| Search table columns | `text` → `document`; add `concept_name`, `concept_description` | LL-D9 |
| `text_sha256` | Unchanged | `carried_vectors` reads only `text_sha256` and `embedding` (`lance.py:880-896`), so it reads a version 3 table |
| `statcat` section (`STATCAT_SECTION_FORMAT = 3`, `bundle.py:68`) | Format unchanged; `search.index_version` is `4` for a new edition | The section copies the index version; no field changes meaning |
| Contact and agent IDs, Inventory, sdmx store schema | None | |

The RFC's pairs fingerprint and its open-time warning (LT-11) are dropped
(M9): `_tables_fingerprint` covers `statcat.language_alternate`
(`working_copy.py:816-847`), and the seal rebuilds the index when it changes
(`working_copy.py:1605-1618`), so a sealed edition never holds stale pairs.
`add_language_alternates` already refuses chains and an alternate that is
also a primary (`store.py:7075-7084`).

## Breaking changes and migration

### Stored data

*   **Readers first.** 0.20 refuses a version 4 index, and 0.21 reads
    versions 3 and 4. Upgrade every reader of a catalog (`EmbargoReader`
    services, the release job, `verify-catalog` runs, upload) to 0.21 before
    any product seals a version 4 edition.
*   **Search over version 3 raises.** Under 0.21, `Product.view` opens a
    version 3 edition and every label, facet and query call works; each
    search call raises `IncompatibleCatalogError` until the product has a
    version 4 edition. A rollback to a retained version 3 edition behaves the
    same.
*   **Re-index each product.** Open a working copy, call
    `build_index(model_name=…)`, seal and release. No re-ingest is needed.
    Carried vectors are reused only when `model_name` is given:
    `index_options` does not carry to a new edition
    (`working_copy.py:785, 1618`), so a seal without it encodes every text
    again. A loop over a catalog:

    ```python
    for product_id in catalog.products():
        with catalog.product(product_id).working_copy() as wc:
            wc.build_index(embedding_model=model, model_name="intfloat/e5-base-v2")
            wc.seal()
    ```

*   **CKAN text moves.** Text under `en-t-fr` and `fr-t-en` moves into the
    `en` and `fr` tables at that rebuild.
*   **Spatial facet keys change.** A coverage without a URI is keyed by
    `sha256:…` (LL-D5) where it was keyed by its English or `und` text.
    Downstream code that stored facet keys maps them again.

### Caller code

*   Pass `lang=` to every label, filter and search call; nothing defaults to
    English.
*   Read text as `.text` on a `LocalisedString`, and the tag as `.lang`;
    `Row` projections give `title` and `title__lang`.
*   Handle `None` and `.gaps` in bulk results; catch
    `MissingTranslationError` and `UnknownCodeError` by name.
*   Replace `.lang(t)` on codelist scopes with `lang=t` on the terminal.
*   Replace `has_spatial_uri` with `has_spatial_key`, and
    `hit.payload.text` with `hit.payload.document`.
*   Remove `name=` from `query(Agent, …)`.

## Cross-repo contract

This design keeps the suite contract: `LocalisedString` and its JSON form
(LS-D6, LS-D9), `InvalidTagError` and `MissingTagError` with statcatlib's
error subclassing sdmxlib's (LS-D7), `tag_alias` in generated names (LS-D8)
and `__lang` for the answering tag (LS-D13). It adds no contract that sdmxlib
or sdmxlib-rest must follow.

### Drift from the suite design

| Hub claim | At `2e5055a` |
|---|---|
| `_language_slug` at `lance.py:211-219` | removed in `c4e6e4f`; `_table_name` (`lance.py:209-219`) inlines its body |
| about 27 `lang="en"` defaults | 37 |
| the legacy filter at `lance.py:942` | removed in `c4e6e4f` |
| English-first fallbacks at `store.py:2425, 2544, 2546` | `store.py:2452, 2572, 2574` |
| `store.py:1162, 1171`, `scopes.py:999, 1054` | fixed in `04b27ac` and GH-94 |
| Stage 4 "add msgspec as a dependency" | LL-D10 |

### Amendments to other designs

A follow-up PR in the StatCat Suite hub applies these to
`docs/design/localised-string.md`:

*   **LS-D4:** statcatlib's mappings (`Labels`), single lookups,
    `.scalars()` values and hit fields carry `LocalisedString`; only `Row`
    projection columns carry text, with a `__lang` column.
*   **LS-D9 and the Stage 4 row:** statcatlib adds msgspec to its `dev`
    group only; at runtime it arrives through sdmxlib (LL-D10).
*   **Acceptance criteria:** statcatlib's tag test is
    `tests/test_language_tags.py`; its tests are flat.
*   **Stage 5, "ingest refuses an empty `lang`":** met by `normalize_tag` in
    every `InternationalString` constructor and by `tag_alias` in index
    grouping, with a test in `tests/test_language_tags.py`.
*   **Status and Staging:** once 0.21.0 is released, mark Stages 4 and 5 as
    built in statcatlib 0.21.0.

This PR applies one to statcatlib's own RFC, `language-tags.md`:

*   **LT-11:** the pairs fingerprint in `index.json`, the warning from
    `open()` and the `check_bundle` report are dropped; `index.json` records
    the pairs for routing.

## Handoff

*   **Hub** (one PR after the 0.21.0 release):
    *   the amendments above;
    *   `docs/architecture.md:164`: the search index row moves to version 4,
        with 0.21 reading version 3 except for search;
    *   `docs/moving-a-service.md:11-21, 364`: add 0.21 and its re-index
        step;
    *   `docs/domain-model.md:136-141`: `sc.Catalog(...)` raises `TypeError`
        at 0.20, and `query` and `code_map` are `EditionView` methods; the
        example opens a view and passes `lang=`;
    *   the version list in `docs/architecture.md` gains 0.21.0.
*   **statcat-serve** (`42ad7ad`): no action. It pins `statcatlib>=0.18,<0.19`
    and `sdmxlib>=0.88,<0.89`, reads through `EmbargoReader`, and calls no
    label or search API. Phase 6 moves its pin.
*   **sdmxlib, sdmxlib-rest:** no action. Neither reads `search/`.
*   **statcatlib follow-up issue:** the CKAN frequency ID (Non-goals).

## Alternatives considered

*   **LL-D1, `(result, gaps)` tuples.** Rejected: every `for k, v in
    labels.items()` chain breaks, and the shape differs between calls.
*   **LL-D2, `LocalisedString` cells in `Row`.** Rejected: a `Row` is a
    `dict` meant for JSON and frames; LS-D4 keeps columnar output as text.
*   **LL-D4, a `.lang()` setter with no default.** Rejected: two ways to
    give a tag, and terminals that raise until the setter runs.
*   **LL-D5, `COALESCE(uri, label['und'], label['zxx'])`.** Rejected (M10):
    a translated CKAN region (`sources/ckan.py:511-513`) holds neither and
    loses its key. A stored key column was rejected too: it bumps
    `SCHEMA_VERSION` and forces a re-ingest.
*   **LL-D7, a tag tuple for search.** Rejected: several tables would need a
    merge across stemmers and BM25 statistics.
*   **LL-D9, keeping `text` until a later bump.** Rejected: this bump already
    forces a re-index.
*   **LL-D15, refusing a version 3 index on open.** Rejected (B1, M1): no
    working copy could open to re-index, and every reader would need the new
    index before upgrading, which the hub's rollout order forbids.
*   **LL-D15, a cut-over runbook with a rollback freeze.** Rejected: more
    steps for operators than reading version 3.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| A reader on 0.20 meets a version 4 edition | "Readers first" in Migration; hub `architecture.md` and `moving-a-service.md` updates |
| Search fails on every product until it is re-indexed | The error names the fix; the loop recipe; label and facet calls keep working |
| A re-index without `model_name` encodes every text again | Migration says so; `build_index` docs |
| Duplicate hits from paired rows (M5) | Entity deduplication; test with a code held under `en` and `en-t-fr` |
| `except KeyError` swallows `MissingTranslationError` | Upgrading tells callers to catch it by name; test of the class hierarchy |
| Behaviour changes without an error: `None` in `Labels`, facet keys, dataset `Hit.lang` | `docs/upgrading.md` lists each; tests pin each |
| Long tag tuples grow the `COALESCE` / `CASE` SQL | Tuples are short (the six surveyed registries send about 30 tags between them); no test bound |
| A filter key typo returns every row | LL-D6's `ValueError` |
| statcat-serve stays pinned at 0.18 | Phase 6 moves it; it reads version 3 and 4 editions once it does |

## Implementation plan

One PR on `feat/gh-91-localised-string`. Every commit updates every caller,
docstring and test it breaks, since `just ci` runs pyrefly and pydoclint over
`src/` and `tests/`, including `tests/test_live_sdmx.py`.

| # | Commit | Content | Tests updated | Tests added |
|---|---|---|---|---|
| 1 | `docs(design): add the label lookup design for GH-91 and language-tags slice 4` | revision 1 | | |
| 2 | `docs(design): add the EGM review of the label lookup design` | review reports | | |
| 3 | `docs(design): revise the label lookup design after review` | this revision; LT-11 amendment | | |
| 4 | `docs(design): add the LocalisedString audit table` | LL-D11 | | |
| 5 | `feat(labels)!: add Langs, Gap, Labels, RowList, ValueList and the label errors` | `statcatlib.labels`; `store/_lang_sql.py`; exports | `test_public_api` | `test_labels.py`: `_as_langs`, containers, error messages, pickling, class hierarchy |
| 6 | `feat(scopes)!: return LocalisedString from codelist and scheme labels` | `label`, both `batch_labels`, `labels`, the `rows`, `filter`; `.lang()` removed; enrich collisions | `test_scopes`, `test_structural_queries`, `test_codelist_policy_and_concepts`, `test_lang_normalisation`, `test_shared_connection_idle`, `test_live_sdmx` | unknown and untranslated codes; tuple fallback; `None` for an unknown codelist; `MissingTranslationError` |
| 7 | `feat(store)!: return Labels from the view's label maps; key spatial facets by URI or hash` | `agent_labels`, `concept_labels`, `spatial_labels`, `code_map`, `preference`, `language_counts`; LL-D5; `has_spatial_key`; LL-D6 | `test_edition_view`, `test_store`, `test_filters`, `test_query_entities`, `test_sources_ckan` | each map's tags and gaps; the pinned spatial hash; a translated CKAN region keyed and labelled; unknown filter key; `preference`; `language_counts` |
| 8 | `feat(query)!: require lang and take tag tuples in projections and filters` | `select`, `scalars`, `sort_by`, `rows`, `values`, the filters, `MapCondition`, `MapFilter` | `test_query_builder`, `test_filters`, `test_cross_entity_queries` | `title__lang`; `None` and a gap; `scalars` values; tuple semantics of each filter |
| 9 | `feat(search)!: require lang in search and tag every hit with its stored language` | required `lang`; `Hit.lang` from the row; `(id, score, lang)`; entity deduplication; `_for` raises; `get()` with `lang` and the `CodeHit` / `ConceptHit` field types | `test_code_search`, `test_code_search_extensions`, `test_hybrid_search`, `test_reference_metadata_search`, `test_embedding_reuse`, `test_edition_view`, `test_live_sdmx` | search refuses a sequence; `get` raising and its `None` description; dataset `Hit.lang` |
| 10 | `fix(search): name index tables from the tag and count region-tag tables in check_catalog` | group by tag, alias once; `table_for_key`; `_check_language` | `test_edition_check`, `test_bundle_manifest` | `check_catalog` over an edition holding `fr-CA` and `en-t-fr` |
| 11 | `feat(search)!: write search index format 4 with language pairs` | pairs routing; `language_pairs`; `document`, `concept_name`, `concept_description`; row tags; version 3 read rules; seal rebuilds a version 3 index | `test_index_version_guard` (the version literals; `test_a_working_copy_carrying_a_refused_index_is_refused` becomes a test that it opens), `test_language_alternates`, `test_code_search`, `test_reference_metadata_search`, `test_embedding_reuse`, `test_bundle_manifest` | pairs search; duplicate rows; search over version 3 raises; `view`, `check_catalog` and a working copy accept version 3; carried vectors reused from version 3; no `en` row for a dataset with none; no `zxx` table |
| 12 | `fix(sdmx): drop empty names at SDMX ingest` | LL-D13 | `test_sources_sdmx` | an agency named `{"en": ""}`; a mixed map; an empty dimension name |
| 13 | `build(deps): add msgspec to the dev group` | LL-D10 | | |
| 14 | `test: enumerate every lang parameter and pin tag_alias` | `tests/test_language_tags.py` | | below |
| 15 | `docs: describe label lookup, gaps and search index format 4` | below | | |

Commits 5–15 land in the order shown. Each passes `just ci`; the docs build
stays green because each code commit also updates the docstrings and API
pages that name what it removes.

### `tests/test_language_tags.py`

*   **Enumeration.** The test walks `statcatlib`, `statcatlib.query`,
    `statcatlib.labels`, `statcatlib.store.scopes` and
    `statcatlib.store.store` (`EditionView`, `QueryBuilder` and the scopes)
    and `statcatlib.store.lance.LanceIndex`, as sdmxlib's
    `test_every_public_lang_parameter_refuses_a_malformed_tag` does (sdmxlib
    `tests/unit/test_language_tags.py:440-481, 593`). It collects every
    public function and method with a `lang` or `langs` parameter. It adds by
    hand `_FilteredCodelistsScope.rows` and `.filter` (reachable through
    `DatasetScope.codelists`, `scopes.py:1166`) and `MapCondition` /
    `MapFilter` (parameter `key`). It exempts exceptions, `Hit` and `Gap`,
    whose `lang` fields are data. Each callable must be registered in a
    `_calls` table; each refuses `fr_CA` with `InvalidTagError`; each has no
    default for `lang`, except `select`, `scalars` and `sort_by`, listed by
    name.
*   **`tag_alias`.** `tag_alias(t) == t.strip().lower().replace("-", "_")`
    for sdmxlib's well-formed fixtures, copied with `v0.89.0` noted, and
    every two-letter ASCII pair.
*   **JSON order.** `msgspec.json.encode(label)` is
    `b'{"text":"…","lang":"…"}'`.
*   **Empty `lang`.** `LanceIndex` grouping refuses a row with `lang=""`.

### Acceptance map

| Criterion | Source | Commit | Test |
|---|---|---|---|
| `msgspec.json.encode` of a label gives `text` then `lang` | GH-91, hub Stages 4–5 | 14 | `test_language_tags.py` |
| `tag_alias` equals the old slug body on every fixture | GH-91, hub | 14 | `test_language_tags.py` |
| every `lang` refuses a malformed tag | GH-91, hub | 14 | `test_language_tags.py` enumeration |
| ingest refuses an empty `lang` | hub Stage 5 | 14 | `test_language_tags.py` |
| label APIs return `LocalisedString` | GH-91 Stage 5, LT-6 | 6, 7, 9 | `test_scopes`, `test_edition_view`, `test_code_search` |
| `batch_labels` returns `None` and gaps | GH-91 Stage 5, LT-8 | 6 | `test_scopes` |
| `title` and `title__lang` | GH-91 Stage 5 | 8 | `test_query_builder` |
| `MissingTranslationError(MissingTagError)` | GH-91 Stage 5, LT-7 | 5, 6, 9 | `test_labels`, `test_scopes`, `test_code_search` |
| `Hit.lang` required, from the row | GH-91, LT-12 | 9, 11 | `test_code_search`, `test_hybrid_search` |
| no `lang="en"` default | GH-91, LT-4 | 6–9 | enumeration |
| no English-first fallback | GH-91 | 9 | `test_code_search` (`get` with `lang="fr"` on a code without `en`) |
| no English facet key or agent filter | LT-9 | 7 | `test_edition_view`, `test_query_entities` |
| no `en` added to the index | LT-9 | 11 | `test_language_alternates` |
| search pairs | LT-11 | 11 | `test_language_alternates` |
| tag tuples, exact matching | LT-5 | 5, 8 | `test_labels`, `test_filters` |
| empty agency name | GH-91 Stage 4 | 12 | `test_sources_sdmx` |
| audit table | GH-91 Stage 4 | 4 | the committed file |
| `just ci` passes | GH-91 | every commit | the pre-push hook |

### Docs

*   `docs/upgrading.md`: a 0.21.0 section following "Breaking changes and
    migration".
*   `docs/api/store.md`, `docs/search.md` (line 52 says `lang` defaults to
    `"en"`), `docs/tutorial.md`, `README.md`, `llms.txt`.
*   `docs/catalog-format.md` lines 13, 265, 418, 421, 447-452, 474 and 475:
    version 4, `language_pairs`, the `document` and concept columns, and a
    row's `lang`.
*   CLAUDE.md "Query surface" (lines 216-278) and the search-index section
    (lines 611-654).
*   `src/statcatlib/EGM.md`, `store/EGM.md` (with the `_lang_sql` and
    `_as_langs` details), `sources/EGM.md`.
*   `language-tags.md` and this design: status to built.
*   CHANGELOG from the conventional commits.

### Size

About 80 source sites in `store/store.py`, `store/scopes.py`,
`store/lance.py`, `query.py`, `bundle.py`, `sources/sdmx.py`,
`working_copy.py` and a new `labels.py`: roughly 1,800–2,300 source lines,
1,200–1,700 test lines over about 22 test files, and 700–1,000 doc lines.

## Appendix: state today

### GH-91 Stage 4

Stage 4 shipped in `04b27ac` (GH-101, statcatlib 0.12.0). The audit table
and the msgspec dependency remain open.

| Item | State | Evidence |
|---|---|---|
| sdmxlib with Stages 1–2 (0.79 or later) | Done | `pyproject.toml:32` |
| `_istring_to_map` over `s.as_dict()` | Done; the helper is a `None` guard | `store/store.py:734-738` |
| `store.py:1162, 1171` | Done (GH-94) | `store/store.py:1129-1180` |
| Contact ID (the issue comment) | Done; hashes `as_dict()` items, so IDs are unchanged | `store/store.py:1176`; `tests/test_agent_contact_ids.py:82` |
| `sources/sdmx.py:115` truthiness | Changed: an agency named `{"en": ""}` keeps an empty label; LL-D13 | `sources/sdmx.py:153` |
| `scopes.py:999, 1054` | Done; `{zxx: id}` (GH-94) | `store/scopes.py:412-417` |
| `_language_slug` → `tag_alias` | Partly; "Search rows" | `lance.py:218, 1209, 1297` |
| msgspec | Open; LL-D10 | `pyproject.toml:23-33` |
| Audit table | Open; LL-D11 | |
| JSON-order and `tag_alias` tests | Open | |
| Enumerated `lang` test | Partly: `tests/test_lang_normalisation.py:54-61` checks three sites | |

### GH-91 Stage 5 and single-repository items

| Item | State | Evidence |
|---|---|---|
| Label APIs return `LocalisedString` | Open | `scopes.py:557, 755, 884, 908`; `store.py:6171, 6180, 6195, 6883` |
| `batch_labels` with `None` and gaps | Open; ID fallback | `scopes.py:789, 803, 933, 947` |
| `title` and `title__lang` | Open | `store.py:6642` |
| `MissingTranslationError` | Open | |
| Ingest refuses an empty `lang` | Done; untested | `lance.py:1297` |
| `Hit.lang` default and fallback | Open | `store.py:1343, 1401, 1437, 1502, 2781` |
| `lang="en"` defaults | Open; 37 sites | Public (26): `query.py:936, 981, 1000, 1016`; `scopes.py:557, 884`; scope fields `scopes.py:685, 838, 1414`; `store.py:1343, 1968, 1989, 2348, 2486, 2610, 2713, 7302, 7349, 7360, 7372, 7405`; `lance.py:1237, 1471, 1525, 1593, 1640`. Private (11): `query.py:923`; `store.py:1890, 3546, 6007, 6171, 6180, 6195, 6260, 6402, 6507, 6883` |
| Legacy search filter | Done (`c4e6e4f`) | |
| English-first fallback | Open | `store.py:2452, 2572, 2574` |
| Composite `text` in hits | Open | `store.py:1280-1318, 1441-1460, 4042` |

### Language-tags slices

| Slice | State |
|---|---|
| 1. CKAN tag handling (GH-84) | Done (PR #86) |
| 2. Spatial identifiers, English as a key (GH-83, GH-94) | Done (PRs #93, #95), except the agent filter (`store.py:1057`) and the spatial facet key and labels (`store.py:1206, 6224-6230`) |
| 3. `normalize_tag`, `LocalisedString` | Done in code (`query.py:139, 167`; `store.py:7070-7071`; every public `lang`); GH-88 open |
| 4. The breaking lookup change | This design |

## Review response

Finding IDs are those of the [consolidated
report](egm/label-lookup/goldfish_review_report.md); it merges the seven
per-lens reports in the same directory.

| Finding | Response |
|---|---|
| B1 working copy refuses a v3 index | Changed: LL-D15; Migration; commit 11 rewrites `test_index_version_guard` |
| M1 no rollout window | Changed: LL-D15 (option a, decided by the owner); "Readers first" |
| M2 `get()` vs hit types | Changed: LL-D14; commit 9 carries both |
| M3 releasable `main` between merges | Changed: one PR, one merge (LL-D14) |
| M4 gap semantics | Changed: "Gaps" table; `Gap.key`; `held` on the error only; `row()`/`value()` rules |
| M5 duplicate row IDs | Changed: "Search rows", Duplicates |
| M6 search row model | Changed: "Search rows" with the open.canada.ca example; `(id, score, lang)` |
| M7 enumeration test | Changed: Goal 1 narrowed; `_for` raises; walk, additions and exemptions given; added after search (commit 14) |
| M8 `_table_name` and the check_catalog bug | Changed: commit 10; `table_for_key` |
| M9 pairs fingerprint | Changed: dropped; LT-11 amendment; `add_bulk(pairs=)`; one pairs shape |
| M10 D5 | Changed: LL-D5 hash key (owner) |
| M11 unknown filter key | Changed: LL-D6 |
| M12 SQL helper | Changed: "SQL helpers"; filter tuple semantics |
| M13 hub amendments | Changed: "Amendments to other designs"; LL-D10 notes sdmxlib's runtime msgspec |
| M14 API omissions and counts | Changed: 37; `LanceIndex` public; `SchemeScope.rows`; enrich collisions; return shapes. CKAN frequency: declined, a Non-goal with a follow-up issue, since it is ingest |
| M15 single-lookup errors | Changed: `None` for an unknown codelist; `UnknownCodeError`; catch by name |
| M16 tests per commit | Changed: the commit table; about 22 files |
| M17 missing sections | Changed: Alternatives, Risks, Acceptance map, Handoff, Breaking changes and migration |
| M18 numbering | Changed: LL-, LS-, LT- prefixes defined in Terms |
| M19 commit syntax | Changed: `type(scope)!:` |
| m1 index format in two commits | Changed: commit 11 holds the bump and the rename |
| m2 `language_counts` | Changed: column list derived and stated |
| m3 module locations | Changed: `statcatlib.labels`, `store/_lang_sql.py` |
| m4 error construction | Changed: `super().__init__`, `__reduce__`, tuple message |
| m5 `Langs` vs `pick` | Changed: Conventions |
| m6 empty text | Changed: Conventions |
| m7 list subclasses | Changed: container limits stated |
| m8 order | Changed: `ORDER BY` per call; duplicates collapsed |
| m9 resolution order | Changed: `arg_min` on `(rank, text)` |
| m10 `preference` | Changed: keyword-only; order and alternate rules |
| m11 terms | Changed: Terms |
| m12 section order | Changed: Problem, Open questions, Conventions before Decisions, Errors, state today in an appendix. Ingest gets LL-D13 and commit 12 in place of a section |
| m13 process decisions | Partly: LL-D10 to LL-D12 and LL-D14 stay numbered so the issue comment's numbers hold; each states its reason once |
| m14 D13 | Changed: LL-D13 widened |
| m15 hub docs | Changed: Handoff |
| m16 audit timing | Changed: LL-D11 |
| m17 docs list | Changed: Docs |
| m18 counts | Changed: 59 calls |
| m19 D9 coupling | Changed: LL-D9 |
| m20 altitude | Partly: the helper's signature stays here because commits depend on it; `store/EGM.md` gets the detail. Loop recipe added |
| m21 style | Changed: every rewrite applied |
| m22 version provenance | Changed: sdmxlib claims checked at `v0.89.0` (`f49a4fc`) |

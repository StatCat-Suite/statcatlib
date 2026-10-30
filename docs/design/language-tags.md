# Language tags and label lookup

* **Author:** Gabriel Gellner
* **Date:** 2026-10-28
* **Status:** RFC for team feedback. It comes from an EGM brainstorm: one
  Goldfish round with the six standard reviewers, and 14 decisions settled by
  the owner. The next step is a formal design (`/egm-draft`).

## Overview

statcatlib stores every multilingual field as `MAP(VARCHAR, VARCHAR)` keyed by
language. The storage shape works. The code that reads it has three problems:

- English is used as an identifier. Spatial facets group by `label['en']`.
  Dimension names hard-code `name['en']`. Agent and contact IDs hash the
  English name.
- Fallbacks are scattered and unstated: `.get("en") or .get("fr")`, about 28
  `lang: str = "en"` defaults, and code-ID fallbacks in `batch_labels`.
- CKAN ingest invents English. It drops translated text (`en-t-fr` and
  `fr-t-en`, RFC 6497 transformed-content tags), uses the package id as the
  English title (GH-84), and stores spatial codes as English labels (GH-83).

The design keeps MAP. Map keys become validated BCP 47 tags, the library has
no default language, matching is exact, every returned label carries the tag
it came from. A single lookup raises an error for a missing label, and bulk
calls list every gap. The users are callers that
build multilingual UIs, chatbots and analytical workflows on statcatlib, and
they need to know which language they got and when a translation is missing.

## Analysis

The evidence comes from two sources:

- **A survey of 14 SDMX registries** (about 1.34 million `xml:lang` values).
  Every tag found was a canonical two-letter BCP 47 code, so validating tags
  costs nothing for these sources. Partial translation is common. OECD lacks
  French for 2% of items and Arabic or Hebrew for 47%, IMF lacks 23% and
  UNICEF 50-53%. INSEE has 9 French-only codelists. OECD's SDMX-JSON carries
  only the requested language; SDMX-ML carries all of them.
- **A sample of 100 open.canada.ca CKAN packages.** Their title, description
  and keyword keys were `en` (63), `fr` (73), `en-t-fr` (37) and `fr-t-en` (27).
  `en` and `en-t-fr` never appear together in one field.

Six Goldfish reviewers read the design against both repositories. The findings
are ranked below. The main ones were checked against the code.

### Blocking findings

1. **Exact tags split the search index.** `_language_slug`
   (`store/lance.py:211-219`) makes one table per full tag, so `en-t-fr` text
   goes into its own table, `search_en_t_fr__N`. `search(lang="en")` takes one
   tag. The 37 CKAN packages whose English is under `en-t-fr` would never
   appear in English search results, and nothing would report it. Resolved by
   the owner: language pairs are declared explicitly and stored in the
   catalog (decisions 11-12).
2. **Agent and contact IDs depend on the English name.** `_agent_id` and
   `_contact_id` (`store/store.py:1157-1171`) hash `name.get("en") or
   name.get("fr")` when no ID is given. These are identifiers, so removing the
   fallback changes every derived ID. Today an agent with neither name hashes
   the empty string, and all such agents collide. Proposed fix: hash a
   language-independent key, such as the sorted map items, and list the ID
   change as a migration item.
3. **Spatial facets have no identifier to group by yet.**
   `dataset_spatial.uri` is nullable and CKAN never sets it. Removing the
   `label['en']` grouping before GH-83 puts every CKAN region in one NULL
   bucket. GH-83 has to land first.
4. **sdmxlib writes half the database without validation or case folding.**
   sdmxlib stores `xml:lang` as given in `sdmx.code.name` and
   `sdmx.localized_text`. DuckDB map subscripts and
   `InternationalString.get` are case-sensitive. A request for `fr-CA` misses
   a stored `fr-ca` in every `sdmx` table. Proposed fix: sdmxlib owns
   `normalize_tag()`, applies it in `InternationalString` and in its writers,
   and statcatlib canonicalises request tags with the same function.
   Resolved by the owner (decision 13).
5. **Old catalogs keep invented English.** Ingest writes
   `InternationalString(en=dim.id, fr=dim.id)` and `en=agency.id`
   (`sources/sdmx.py:115, 128, 130`) and package ids as English titles. An old
   catalog would return `REF_AREA` as an English label instead of raising.
   Proposed fix: bump `statcat` `SCHEMA_VERSION` (`store/schema.py:49`, now
   `"2.0"`) so `_refuse_statcat_schema_mismatch` refuses old catalogs, and
   record the rebuild in the CHANGELOG.

### Major findings

6. **`extras` is not multilingual.** `extras` is a MAP with arbitrary keys
   (`store/schema.py:120`). Tag validation would refuse it. Exempt it. That
   leaves 26 multilingual MAP columns.
7. **Raising does not suit paged and predicate paths.** `.select`,
   `.scalars` and `sort_by` apply LIMIT and OFFSET in SQL, so whether a call
   raises would depend on which page it fetches. `MapFilter` and
   `has_spatial` are predicates, where a missing key means "no match". The
   reviewers propose raising in single-entity calls and `batch_labels`, and
   returning `None` with the matched tag in projections. Resolved by the
   owner: only single lookups raise (decisions 7-8).
8. **`Label` is taken.** sdmxlib already has a query expression called
   `Label` (`sdmxlib/query/_expr.py:1209`), and it names output columns
   `f"{column}_{lang}"`, which breaks for `en-t-fr` and tuples. Rename the
   value type and decide the projection column names (`title` plus
   `title_lang`). The type is `LocalizedText` (decision 14); the column names
   are open.
9. **Untagged SDMX-ML text is English by schema default.** Every SDMX-ML
   schema declares `xml:lang` with `default="en"`, and sdmxlib applies it
   (`SCHEMA_DEFAULT_LANG`, `sdmxlib/formats/sdmx_ml/_common.py:89-93`). The
   `und` rule applies only to CKAN (decision 3).
10. **`batch_labels` merges two misses.** An unknown code and an untranslated
    code both come back as the code ID. Raise `KeyError` for the first and
    report only the second as a gap.
11. **Callers need a fallback recipe and tag discovery.** With
    `lang=("fr", "en")`, a caller misses `fr-t-en` text that is present.
    Publish a recommended preference tuple per locale, and add an API that
    lists the tags present with counts.
12. **The `LocalizedText` and `str` boundary is unspecified.** The docs promise `str`
    from `Row` values, `.scalars()` and `batch_labels`. The design must define
    equality, hashing, JSON serialisation and `.frame()` columns.
13. **Ingest normalisation is invisible.** Count and log fixes and refusals
    per source, and carry the source in `.gaps`.

### Smaller points

- The `pick_lang` macro returns text only. To return the tag, filter the
  preference list and return a STRUCT. Read-only opens cannot create a
  persistent macro, so it has to be a TEMP macro per connection.
- `InternationalString(**{"en-t-fr": ...})` works, but `__repr__` prints it as
  invalid Python.
- The code-ID fallbacks are in `.rows()`, `batch_labels` and `code_map`;
  codelist labels fall back to the URN (`store/scopes.py:695, 1371`).

### Size of the change

About 77 statcatlib call sites change: `store/store.py` 31, `store/scopes.py`
18, `sources/ckan.py` 10, `query.py` 7, `store/lance.py` 5, `sources/sdmx.py`
4, plus `store/schema.py` and `__init__.py`. 18 test files have 197 hits on
the affected APIs, and 7 docs pages have 106.

## Goals vs. Non-Goals

* **In-Scope Goals:**
  - Validate BCP 47 tags at ingest with sdmxlib's `normalize_tag()`.
  - Store tags in the lowercase form `normalize_tag()` returns, `-t-`
    extensions included; `und` for untagged CKAN text; `zxx` for codes
    stored as text.
  - Require `lang` on every label-returning API. Accept a tag or an ordered
    tuple; each requested tag is normalised, then matched exactly.
  - Return labels with the tag that matched.
  - Raise `MissingTranslationError` from single lookups; return `None` and
    a gap list from bulk and paged calls.
  - Remove English as an identifier from facets, IDs and dimension names.
  - Fix CKAN ingest (GH-83, GH-84).
* **Out-of-Scope Non-Goals:**
  - RFC 4647 range matching.
  - JSON or VARIANT columns.
  - Exporting metadata to Fabric or Delta.

## Key Decisions & Staging for Next Steps

Decided by the owner:

1. Keep `MAP(VARCHAR, VARCHAR)`.
2. Keys are BCP 47 tags, stored in the lowercase form `sl.normalize_tag()`
   returns (the GH-88 contract with StatCat-Suite/sdmxlib#280). Request tags
   go through the same function, so lookups compare exactly and no SQL path
   folds case. `normalize_tag()` refuses `_` and whitespace, so the CKAN
   reader repairs them before calling it: it strips the ends of a tag and
   replaces `_` with `-`. Internal whitespace, malformed and empty tags are
   refused; the reader drops the refused tag from the field, counts it and
   logs it per source (finding 13), and keeps ingesting the package.
3. Untagged CKAN text is stored under `und`; codes stored as text are `zxx`.
   SDMX-ML text with no `xml:lang` stays English, as the SDMX-ML schemas
   declare (`default="en"`) and sdmxlib already applies.
4. No default language; `lang` is required.
5. Exact matching only, over a tag or an ordered tuple.
6. Labels carry the matched tag.
7. Only single lookups raise. A single-entity label accessor raises
   `MissingTranslationError` naming the entity, field and requested tags.
8. Bulk and paged calls do not raise. `.select()`, `.scalars()`, `sort_by()`,
   facet labels and `batch_labels` return `None` for a missing label, and
   the result lists every gap (entity, field, requested tags). Filters such
   as `has_spatial` treat a missing tag as no match. This replaces the
   earlier "raise once" rule.
9. English is never an identifier.
10. Search stays one table per language.
11. **Language pairs are explicit and stored in the catalog.** A pair maps an
    alternate tag to a primary tag (`en-t-fr` to `en`). Search indexes text
    under an alternate tag in the primary tag's table. Pairs come from the
    caller or from the source; CKAN ingest records the portal's declared
    `alternate_languages` (ckanext-fluent, for example
    `{"en": ["en-t-fr"], "fr": ["fr-t-en"]}`). Each pair records its source.
    An alternate tag may not also be a primary tag. RFC 6497 defines what
    `en-t-fr` means (English transformed from French) but no matching rule,
    and it does not mean machine-translated by itself, so statcatlib applies
    no pairing rule of its own. `index.json` records a fingerprint of the
    pairs; a changed set makes `open()` warn and `check_bundle` report until
    `build_index()` runs (GH-3 rule).
12. **Search hits report the actual tag** of the matched text, as labels do.
13. **Tag normalisation lives in sdmxlib** (`normalize_tag()`, applied in
    `InternationalString` and in sdmxlib's writers); statcatlib normalises
    request tags with the same function. Filed as
    StatCat-Suite/sdmxlib#280.
14. **The label value type is `LocalizedText(text, lang)`, in sdmxlib**,
    beside `InternationalString`, with a lookup such as
    `InternationalString.pick(*langs) -> LocalizedText | None`. The name
    avoids sdmxlib's query expression `Label`. Part of
    StatCat-Suite/sdmxlib#280.

Open, for the design document:

- Whether sdmxlib refuses `xml:lang=""` or reads it as the schema default
  `en` (StatCat-Suite/sdmxlib#280).
- The exact signatures that change, the projection column names (`title`
  plus `title_lang`), and `LocalizedText` equality, hashing and JSON form
  (findings 8 and 12).
- A recommended preference tuple per locale, and an API that lists the tags
  present with counts (finding 11).

Staging, decided by the owner. The work ships in four slices, in this order:

1. CKAN tag handling (GH-84): every tag kept, `und` for untagged text, no
   package-id titles, `alternate_languages` recorded as language pairs.
2. Spatial identifiers (GH-83) and removing English as a key: facets,
   dimension names, agent and contact IDs.
3. Adopting sdmxlib's `normalize_tag()` and `LocalizedText` once sdmxlib
   ships them (StatCat-Suite/sdmxlib#280).
4. The breaking lookup change: required `lang`, exact matching over tuples,
   `LocalizedText` returns, `MissingTranslationError`, gap lists, search
   pairs and tagged hits. It needs slice 3.

Migration items: `statcat` `SCHEMA_VERSION` bump and catalog rebuild, changed
agent and contact IDs, CKAN re-ingest, search index rebuild, and facet keys
changing from English labels to identifiers for downstream callers.

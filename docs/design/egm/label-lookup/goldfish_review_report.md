# EGM review report: label-lookup.md, revision 1

**Target:** `docs/design/label-lookup.md` (lines 1–456), statcatlib branch
`feat/gh-91-localised-string` at `28e4899`, base `main` `2e5055a` (0.20.0).

**Round:** 1. Seven reviewers, one instance each: codebase-details, design,
implementation-readiness, comprehension, structural, style and zoom-out.
Each reviewer's report is in this directory.

**Evidence base.** Every `file:line` is at statcatlib `2e5055a` unless it
names another repository. The reviewers could not run `git show`, and the
shared sdmxlib checkout is at **0.88.0**
(`/Users/gabrielgellner/dev/sdmxlib/pyproject.toml:3`). Citations to
`istring.py` are therefore at 0.88.0. Every claim the design makes about the
text model holds at 0.88.0, and should be re-checked at the `v0.89.0` tag
before code. The synthesizer re-read the evidence for B1, M5, M8, M10 and M11
and confirmed each.

**Counts:** 1 blocker, 19 major, 22 minor.

## Decision impact (D1–D14)

- **D5 (spatial facet key): M10 would change it.** Under
  `COALESCE(uri, und, zxx)`, a translated CKAN region has no key. Such a
  region is stored as a tagged dict with no `uri` and no `und`
  (`sources/ckan.py:511-513`). The fix is a language-neutral key; failing
  that, D5 must name where the gap is reported.
- **D14 (shape of the work): M2 changes its contents, not its direction.**
  `get()` cannot change in Part A while the `CodeHit` and `ConceptHit` field
  types change in Part B.
- **D14's "no compatibility shims": M1 flags it.** The simplest fix for the
  rollout deadlock refuses a v3 index at search time, so `Product.view` can
  still open the edition. This narrows the refusal, and the owner should
  confirm it.
- **RFC decision 11, outside D1–D14.** M8 shows that a sealed edition can
  never hold stale pairs. The fingerprint, the open-time warning and its test
  can go.
- No finding reverses D1, D2, D3, D4, D6, D7, D8, D9, D10, D11, D12 or D13.
  Notes on some of them:
  - D6 needs an unknown-key error (M11).
  - D13 is narrower than the problem (m14).
  - D9 depends on the v4 bump (m19).
  - D10 holds, but sdmxlib already brings msgspec in at runtime (M13).

---

## Blocker

### B1. The upgrade path cannot start: a working copy refuses a carried v3 index

- **Lenses:** codebase-details, design, implementation-readiness, zoom-out
  (all four found it independently).
- **Section:** Stored formats › Migration; Tests › Search ("a rebuild over a
  carried version 3 index reuses its vectors").
- **Evidence:** The design says "A working copy opens over it, since
  `working_copy.py:1172` checks only the `statcat` schema". In the code:
  - `working_copy.py:1172` is only the first-edition schema guard.
  - `working_copy.py:1179-1188` then calls
    `_Store._over(..., search_dir=edition_dir / "search", writable=True)`.
  - `_over` runs `_refuse_unreadable_index(search_dir)` whenever `search_dir`
    is set (`store.py:2906-2908`).
  - The check is an equality test (`lance.py:1140`).
  - `LocalRegistry.build` carries the previous `search/` forward, and
    `_start` discards the new edition on failure
    (`working_copy.py:1190-1195`).
  - `tests/test_index_version_guard.py:105-120` asserts this refusal, and
    `docs/catalog-format.md:447-450` and hub `architecture.md:164` document
    it.

  Under 0.21, every `product.working_copy()` over a v3 edition raises
  `IncompatibleCatalogError`. The workaround in the error message deletes
  `search/` (`store.py:245-249`), which loses the carried vectors.
- **Fix:** add a step to commit 12. `_over(writable=True)` accepts
  `_MANIFEST_VERSION - 1` and opens that index only for `carried_vectors()`
  and `drop_carried`. Search on such a store raises, and the seal always
  rebuilds. Also:
  - Confirm that `carried_vectors` and `hashed_vectors` read a v3 table,
    which has `text` where v4 has `document`.
  - State that vectors are reused only when `build_index(model_name=...)` is
    given. `index_options` does not carry to a new edition
    (`working_copy.py:1618`).
  - Replace `test_a_working_copy_carrying_a_refused_index_is_refused`, and
    change the version literals at `test_index_version_guard.py:63, 112`.
  - Update `catalog-format.md` §4.1 (lines 418, 447-452) and hub
    `architecture.md:164`.
  - Rewrite the Migration bullet.

---

## Major

### M1. The v3/v4 refusal leaves no rollout window and strands statcatlib-side readers

- **Lenses:** codebase-details, design, zoom-out.
- **Section:** Stored formats › Migration; Downstream impact; D14.
- **Evidence:** 0.20 refuses v4 and 0.21 refuses v3 (`lance.py:1140`). A v3
  index fails `check_catalog` (`bundle.py:241-244`), which reaches:
  - `Product.view` and `EmbargoReader.view` (`catalog.py:378-386`);
  - the blob `check_on_open` path (`catalog.py:289-296`);
  - the release job's fold `_reference_check` (`release.py:1174-1181`);
  - upload `_verify` (`upload.py:231-237`);
  - `verify_catalog` for every non-retired edition (`diagnostics.py:315-317`).

  The consequences:
  - Rollback to a retained v3 edition gives an unreadable live edition.
  - Embargoed v3 uploads in `pending/` are refused at fold.
  - `verify-catalog` stays red until the v3 editions retire.
  - The hub's rollout rule (`architecture.md:166-169`, deploy the new
    library to every reader first) deadlocks.
  - statcat-serve reads through `EmbargoReader`
    (`statcat_serve/__init__.py:10`).

  sdmxlib and sdmxlib-rest are not stranded, because neither reads
  `search/`.
- **Fix:** choose one option.
  - **(a)** Refuse v3 only in the search entry points and in
    `_check_search`, so `Product.view`, rollback, `EmbargoReader`, labels
    and facets keep working. Part A never touches `search/`. This narrows
    D14's no-shims stance, and the owner should confirm it.
  - **(b)** Keep the open-time refusal and write an ordered cut-over:
    1. Re-index and seal every product with 0.21.
    2. Record them in one Inventory version (`Catalog.seal(batch)`, or one
       release run).
    3. Drain `pending/` first.
    4. Switch the release job and the producers together.
    5. Freeze rollback across the boundary.
    6. State what `verify_catalog` reports for superseded v3 editions.

  Copy the procedure into the hub's `moving-a-service.md`.

### M2. Work order: `get()` changes in Part A, but its hit types change in Part B

- **Lenses:** design, implementation-readiness, zoom-out.
- **Section:** Work order, commits 5 and 13; D14; the Search table.
- **Evidence:** Commit 5 makes `CodesScope.get` and `ConceptsScope.get`
  return tagged labels, with the name raising and the description `None`.
  The `CodeHit` and `ConceptHit` fields stay `str` until commit 13
  (`store.py:1289-1317`). `get` builds them at `store.py:2453-2464, 2576-2583`,
  and the search builders pass `str` (`store.py:1385-1396, 1425-1432`).
  "Each PR passes `just ci` alone" fails, or Part A ships `get` without tags.
- **Fix:** move the `LocalisedString` field types and the `_row_to_*`
  builders into commit 5, taking the tags from `row["lang"]`. Keep only
  `document`, `concept_name` and `concept_description` (D9) in Part B. State
  what `document` holds on a hit built by `get()`. This changes D14's
  contents.

### M3. `main` is releasable, and its docs are published, between the two merges

- **Lens:** zoom-out.
- **Section:** D14; Work order.
- **Evidence:** `release-2-changelog` cuts from `origin/main`
  (`justfile:143-162`). `.github/workflows/docs.yml:12-22` publishes the site
  on every push to `main` that touches `docs/**` or `src/**`. Merging Part A
  publishes the 0.21 upgrading notes while PyPI still serves 0.20.
- **Fix:** state a release freeze from the Part A merge until 0.21.0, or
  move commit 9 into Part B's docs commit. Name `v0.20.0` as the branch point
  for any 0.20.x fix.

### M4. `Gap` and the gap semantics are undefined per call

- **Lenses:** comprehension (rated a blocker), design,
  implementation-readiness, zoom-out.
- **Section:** D1; New public types; Conventions.
- **Evidence:** The only example is a code URN with `name`. The design leaves
  these open:
  - `Gap.entity: str` cannot hold `CodelistsScope.batch_labels`'s tuple keys.
  - `SchemeScope.labels` is keyed by notation (`scopes.py:557-568`).
  - The outer dict of `code_map -> dict[str, Labels]` has no `.gaps`.
  - `facets()` returns `dict[str, dict[str, int]]` (`store.py:6169, 1274`),
    with nowhere to put D5's gaps.
  - `row()` and `value()` drop gaps, and `value()`'s `None` means both "no
    row" and "untranslated" (`store.py:2271-2274`).
  - The design does not say whether a missing optional column (`description`
    in `select`) is a gap.
  - `held` costs a `map_keys` call per missing row.
- **Fix:** add a table with columns for call, `entity`, `field`, gap
  condition and an example `Gap`, covering:
  - `batch_labels` (both scopes), `labels` and `agent_labels`;
  - `concept_labels`, `spatial_labels` and `code_map`;
  - `rows` (codelists), `select`/`rows`, `values` and `scalars`;
  - `facets("spatial")`.

  Then:
  - Add `key: object` to `Gap` so a caller can join a gap to its result.
  - Settle `code_map`'s outer gaps and where facet gaps go.
  - Say whether `row()` and `value()` raise or stay silent.
  - Say that `langs` is the `as_langs` tuple.
  - Optionally move `held` to the error only.

### M5. Language pairs make search row IDs collide within one table

- **Lens:** design, extended by the synthesizer.
- **Section:** Search (Part B); D7; Work order commit 12.
- **Evidence:** Search row IDs carry no tag: a code row's ID is `ident`
  (`store.py:3846`) and a concept row's is the concept URN
  (`store.py:3936`). Today each table holds one tag, so IDs are unique within
  a table. Once `en-t-fr` rows share the `en` table:
  - `_LanguageIndex.upsert` deletes by `id` (`lance.py:918-927`), so adding
    one tag's rows deletes the other tag's.
  - `bm25_search_rows` and `search_rows` do not deduplicate
    (`lance.py:1520-1585, 1635-1673`), so a code held in both tags returns
    twice, inflating `total` and filling `limit`.
  - `CodesScope.find` and `ConceptsScope.find` merge nothing in BM25-only
    mode (`store.py:2544-2550`).
- **Fix:** make row IDs tag-qualified, or require `add()` to receive every
  tag's rows for an ID. Deduplicate the `*_rows` results by entity, keeping
  the best row and its `lang`. Test a code that both `en` and `en-t-fr` hold.

### M6. The search row model and hit tags are undefined

- **Lenses:** comprehension (rated a blocker), design,
  implementation-readiness, codebase-details.
- **Section:** Search (Part B); D9; Stored formats.
- **Evidence:**
  - The design does not say how a document that mixes `en` and `en-t-fr`
    parts gets its tag.
  - Code rows take their tags from the dataset title (`store.py:3810-3812`)
    and skip a tag that has no code label (`3820`).
  - `ConceptHit.name: LocalisedString` (required) is impossible for a
    description-only tag, which today gets `name=""` (`store.py:3925-3933`).
  - The index stores no separate tag for `dimension_label`
    (`store.py:3818, 3851`).
  - "Dataset hits from their best row" cannot be computed, because
    `bm25_search` and `search` return `(id, score)` after `_dedupe_by_id`
    (`lance.py:1477, 1518, 1633`), and so does `_rrf_rank`
    (`store.py:3542-3585`).
  - Hybrid RRF can rank a different best row in BM25 than in vector search
    (`store.py:2750-2765`).
  - `search(lang="en-t-fr")` has two possible results: every `en`-table hit,
    or only the `en-t-fr` rows. The result for a tag with no table is
    unstated.
  - The dataset tag union would create a `zxx` table of bare dimension IDs
    (`sources/sdmx.py:166-168`).
- **Fix:** add a "Search rows" subsection with these rules:
  - one row per stored tag, with the document built from that tag's text;
  - the row's `lang` is that tag, and `dimension_label` takes the row's tag;
  - `ConceptHit.name: LocalisedString | None`, or skip rows that have no
    name;
  - `zxx` excluded from the dataset union, and a decision on `und`.

  Give the new return type `(id, score, lang)` for `bm25_search`, `search`
  and `_rrf_rank`. The hybrid rule takes the better-ranked row and breaks a
  tie in favour of BM25. Add an open.canada.ca worked example.

### M7. The enumeration test cannot pass as specified, and Goal 1 contradicts the API

- **Lenses:** codebase-details, implementation-readiness, structural,
  comprehension.
- **Section:** Goals bullet 1; Conventions ("No `lang` has a default");
  Tests › `test_language_tags.py` bullet 1.
- **Evidence:**
  - (a) `select`, `scalars` and `sort_by` keep `lang: Langs | None = None`
    (`store.py:2009, 2072, 2091`).
  - (b) `LanceIndex._for` swallows `InvalidTagError` and returns `None`
    (`lance.py:1206-1211`), so `fr_CA` gives `[]`.
  - (c) A walk modelled on sdmxlib's (sdmxlib
    `tests/unit/test_language_tags.py:440-481`, which skips only exceptions
    and `_`-prefixed names) finds `Hit(lang=)` (`store.py:1343`), `Gap` and
    `preference(lang)`. It misses `_FilteredCodelistsScope.rows`/`filter`,
    which `DatasetScope.codelists` (`scopes.py:1166`) makes reachable. It
    also misses `MapCondition`/`MapFilter`, whose parameter is named `key`.
  - (d) The search `lang` defaults survive Part A, so the test cannot pass at
    the end of Part A, although GH-88 closes with Part A.
- **Fix:**
  - Narrow Goal 1 and the Conventions line: no `lang` defaults to a tag, and
    `None` is allowed only on `select`, `scalars` and `sort_by`, where a MAP
    column without `lang` raises `ValueError` at build time.
  - Make `_for` raise.
  - Give the module filter and the exemptions (exceptions, `Hit`, `Gap`),
    and add hand-written cases.
  - Add a temporary allow-list for the search callables in commit 3 or 4,
    removed in commit 10.

### M8. `_table_name` calling `tag_alias` breaks region tags, and `check_catalog` already miscounts them

- **Lenses:** implementation-readiness, codebase-details. Verified by the
  synthesizer.
- **Section:** Work order commit 12; Stored formats.
- **Evidence:**
  - `_table_name` keeps an alias given to it (docstring, `lance.py:212-218`).
  - `_by_language` groups rows by `tag_alias(...)` (`lance.py:1297`), and
    `write_manifest` keys `languages` by alias (`lance.py:1193`).
  - `tag_alias("fr_ca")` normalises its input, and `normalize_tag` refuses
    `_`.
  - Commit 12 as written therefore raises on `fr_ca` and `en_t_fr`.
  - The same bug already exists: `bundle._check_language` calls
    `index.table_for(lang)` with alias keys (`bundle.py:259-281`). `_for`
    swallows the error, so `check_catalog` reports "0 rows, the manifest says
    N" for every edition that holds a hyphenated tag, including
    open.canada.ca.
  - `tests/test_edition_view.py:214-228` seals `fr-CA` but never runs the
    check.
- **Fix:** group rows by the normalised tag, and alias it once, inside
  `_table_name`. Define the key form of v4's `languages`. Give `check_catalog`
  an accessor keyed by alias. Add a `check_catalog` test over an edition that
  holds `fr-CA` and `en-t-fr`.

### M9. The pairs fingerprint and stale-pairs check name a removed function and guard a state a sealed edition cannot reach

- **Lenses:** codebase-details, implementation-readiness, design, zoom-out.
- **Section:** Stored formats (the paragraph after the table); Work order
  commit 12; Tests › Search.
- **Evidence:**
  - `check_bundle` was removed (`docs/upgrading.md:553`); the check is
    `bundle.check_catalog` and `_check_search` (`bundle.py:223-253, 285`).
  - `_tables_fingerprint` covers `statcat.language_alternate`
    (`working_copy.py:816-847`), and the seal rebuilds when it changes
    (`working_copy.py:1605-1618`), so a sealed edition never holds stale
    pairs.
  - The fingerprint's input bytes are unstated.
  - The design does not say how `LanceIndex` receives pairs, given that
    `_by_language` is a staticmethod (`lance.py:1294`).
  - `check_catalog_no_index` skips `_check_search`.
  - `add_language_alternates` already refuses chains and an alternate that
    is also a primary (`store.py:7075-7084`).
  - The Stored-formats shape is `{alternate: primary}`, but the Tests use
    `{"en": ["en-t-fr"]}`.
- **Fix:** keep `language_pairs: {alternate: primary}` in `index.json` as the
  routing source for `_for`. Drop the fingerprint, the open-time warning and
  its test, or keep at most one `_check_search` finding that compares the
  stored pairs. This amends RFC decision 11. Give `add_bulk(..., pairs=)`.
  Cite `store.py:7075-7084` for pairs semantics. Use one pairs shape
  throughout. Note in upgrading that `languages`, `describe()` and
  `index_layout` will list primary tags only.

### M10. D5 leaves translated CKAN regions without a facet key

- **Lens:** design. Verified by the synthesizer.
- **Section:** D5; Projections, filters and facets; Migration.
- **Evidence:**
  - `_parse_spatial` stores a dict-form region (ckanext-fluent `{en, fr}`)
    as a tagged label with no `uri` and no `und` (`sources/ckan.py:511-513`).
  - Today such a region is keyed by `label['en']` (`store.py:1206`).
  - Under D5 it gets no key, so it drops out of facets and `has_spatial_uri`.
    The same holds for user-`put` coverages tagged only `en` or `fr`.
  - The gap has no container.
  - A `zxx` key does not round-trip through `has_spatial(..., lang="und")`.
- **Fix:** key every row with a language-neutral value, such as a hash of the
  sorted `as_dict()` items as `_contact_id` does (`store.py:1175-1176`).
  Resolve labels in `spatial_labels`, and add `has_spatial_key(key)`. If the
  owner keeps `COALESCE(uri, und, zxx)`, `facets()` must report a gap count.
  **This would change D5.**

### M11. D6: after removal, an unknown filter key is ignored silently

- **Lens:** design. Verified by the synthesizer.
- **Section:** D6; Projections table (`query(Agent, name=…)`).
- **Evidence:** `_execute_entity_query` runs
  `col = spec.filter_cols.get(key); if col: …` (`store.py:6346-6350`). After
  D6, `query(Agent, name="Statistics Canada")` returns every agent.
- **Fix:** raise `ValueError` for an unknown filter key on entity specs that
  are not datasets, naming `id` for `name`, and add a test.

### M12. The SQL helper covers MAP columns only, and its parameters are underspecified

- **Lenses:** design, implementation-readiness.
- **Section:** Projections, the paragraph on the private SQL helper.
- **Evidence:**
  - Codelist names come from a row-per-tag `sdmx.localized_text` join on
    `lt.lang = ?` (`scopes.py:723-729, 895-897, 1444-1450`).
  - One `params` list cannot serve a text expression and a tag expression
    that land in different clauses.
  - `?` and `$name` cannot mix in one statement, and `get` uses `$code_urn`
    (`store.py:2441`).
  - The design does not say how `filter(text_contains, lang)` treats a
    tuple, or whether the `t.urn ILIKE` match stays (`scopes.py:737-740`).
  - Tuple predicates (`has_keyword`, `title_contains`, `MapCondition`)
    compile to `COALESCE`, so they match only the first held tag's text.
- **Fix:**
  - Give the helper's module and signature, returning text SQL with its
    params and tag SQL with its params, in the positional style.
  - Add a second form for `localized_text`: aggregate it to a MAP first, as
    `store.py:3768-3773` does, or rank the rows with `arg_min`.
  - State the tuple semantics of the predicates and of `filter`.
  - List the type change of `MapCondition.key`.

### M13. The hub design amendments are incomplete

- **Lenses:** codebase-details, design, structural, implementation-readiness.
- **Section:** Hub design amendments; Goals bullet 6.
- **Evidence:**
  - Hub D4 says "Bulk and columnar outputs return text and carry the tag
    separately" (`localised-string.md:215-219`). `Labels` and the
    `LocalisedString` hit fields contradict it, but the amendment covers
    only `.scalars()`.
  - The hub's Stage 4 row says "add msgspec as a dependency"
    (`localised-string.md:594`).
  - The hub names `tests/unit/test_language_tags.py` (`:581-583`).
  - Stage 5's "ingest refuses an empty `lang`" is marked "Done in effect"
    with no test.
  - sdmxlib already depends on msgspec at runtime (`istring.py:11`).
- **Fix:** add amendments for hub D4 (bulk mappings and hits carry
  `LocalisedString`; `Row` columns carry `__lang`), the Stage 4 row and the
  flat test path. Add a test that `_by_language` refuses `lang=""`. Note in
  D10 that msgspec arrives through sdmxlib.

### M14. The Public API tables omit calls and miscount the defaults

- **Lenses:** codebase-details, design.
- **Section:** State today, the `lang="en"` defaults row; Public API.
- **Evidence:**
  - The design's own list has 37 entries, not 33.
  - The five `LanceIndex` methods (`lance.py:1237, 1471, 1525, 1593, 1640`)
    are public, although the design files them as private.
  - `SchemeScope.rows` returns the raw `label` MAP (`scopes.py:523-538`), and
    D2/D3 leave it undecided.
  - `sources/ckan.py:451` keys the frequency ID on `raw.get("en")`.
  - Enrich aliases can overwrite `label` or `label__lang` silently
    (`scopes.py:400-401`).
- **Fix:**
  - Correct the count to 37 and move the five methods to the public list.
  - Add a `SchemeScope.rows` row: `label` and `label__lang`, or name it
    exempt.
  - Fix the CKAN frequency ID, or scope ingest out of Goal 4.
  - Raise on enrich aliases that collide with entity columns.
  - Add the `bm25_search`/`search` return shape change from M6.

### M15. Single-lookup error contract: unknown entity versus missing translation

- **Lenses:** design, codebase-details, implementation-readiness,
  comprehension, structural.
- **Section:** Conventions ("An unknown entity keeps today's result"); the
  `CodelistScope.label` row.
- **Evidence:**
  - Today `label()` returns `None` for an unknown codelist and on
    `CatalogException` (`scopes.py:903-906`). The table says it raises
    `LookupError`.
  - `MissingTranslationError` is a `KeyError` and so a `LookupError`, so
    `except LookupError` catches both cases.
  - `get` returns `None` for an unknown entity (`store.py:2447, 2566`).
  - `one()` raises a bare `KeyError` (`store.py:2212-2214`).
  - The `batch_labels` `CatalogException` ID fallbacks are undecided
    (`scopes.py:798-799, 942-943`).
  - `batch_labels` raises `KeyError` for an unknown ID, which a caller cannot
    tell apart from a missing translation.
- **Fix:** keep `None` for an unknown codelist, as today, or list the change
  in Migration. Decide the `CatalogException` fallbacks. In upgrading, tell
  callers to catch `MissingTranslationError` by name, never `KeyError` or
  `LookupError`. Optionally give the unknown-ID error a `KeyError` subclass.

### M16. Tests are not assigned to commits, and the existing-test count is low

- **Lenses:** implementation-readiness, codebase-details, structural.
- **Section:** Work order; Tests.
- **Evidence:**
  - `just ci` runs pyrefly over `tests/`, including `test_live_sdmx.py`
    (`pyrefly.toml:47`), and pydoclint, so each signature change must update
    every caller and docstring in the same commit.
    `test_shared_connection_idle.py:62` calls `.label("en")` positionally.
  - About 18 to 22 test files are affected, not sixteen. The work also
    includes `payload.text` → `.document`, row keys and version literals.
  - Tests miss several Goals:
    - search raising `TypeError` for a sequence;
    - `agent_labels`, `concept_labels`, `spatial_labels`, `code_map`,
      `SchemeScope.labels` and the `get` methods returning tags;
    - `label` and `get` raising;
    - D8's optional description returning `None`;
    - the removal of D6 and of the English-first fallback;
    - step 11, which stops the invented `en`;
    - D9's columns;
    - `preference()` and `language_counts()`.
  - Docs never break the gate, because nothing runs doc examples, so commit 9
    lagging is safe.
- **Fix:** for each commit, list the test files it updates and the tests it
  adds. Add the missing tests, and map each Goal to its tests in the
  acceptance map (M17).

### M17. Missing sections: Alternatives, Risks, Acceptance map, Handoff

- **Lens:** structural (rated blockers under the feature-spec standard), with
  design agreeing.
- **Section:** whole document.
- **Evidence:** The hub (`localised-string.md:388-417, 419`) and the sibling
  designs (`release-job.md:1233`; `inventory-history-stage-2.md:1825`) have
  these sections. Goal 6 cites GH-91's and Stages 4–5's acceptance criteria
  and lists none of them. The design has no Handoff section for the hub doc
  fixes.
- **Fix:**
  - Add "Alternatives considered", one bullet per rejected option for D1,
    D2, D4, D5, D7, D9 and the migration.
  - Add "Risks and mitigations" covering M1, M5, M11, M15 and statcat-serve's
    pin.
  - Add an "Acceptance map" table with columns for criterion, commit and
    test.
  - Rename "Downstream impact" to "Handoff" and place it after Cross-repo
    contract.
  - Promote Migration to "Breaking changes and migration", with "Stored
    data" and "Caller code" subsections.

### M18. Three decision-numbering schemes collide

- **Lenses:** comprehension (rated a blocker), structural.
- **Section:** throughout; Cross-repo contract (line 340); Hub design
  amendments (lines 348–352); Non-goals (line 130).
- **Evidence:** "decisions 4 to 12" refers to the RFC, D1–D14 to this
  design, and "hub D13" to the hub. Line 340's unprefixed "(D7)" and "(D8)"
  mean hub decisions, while this design's D7 and D8 are search and single
  lookups. The amendment bullets "D9:" and "D4:" are hub numbers. "decision
  2" at line 130 is ambiguous.
- **Fix:** prefix every outside reference ("hub D7", "RFC decision 11", "RFC
  finding 10"). Optionally rename the local decisions (L1–L14, as
  `inventory-history-stage-2.md:49-59` does). Define the schemes once under
  Terms.

### M19. Breaking-change commit headers use invalid syntax

- **Lens:** style.
- **Section:** Work order, lines 362, 365, 367, 370, 372, 379, 381, 383, 386.
- **Evidence:** Conventional Commits 1.0.0 writes
  `<type>[(scope)][!]: <description>`. `feat!(labels)` is not valid, so
  git-cliff would not read the header as breaking, and pre-1.0 the minor bump
  depends on it (`cliff.toml`; `justfile:149-151`).
- **Fix:** write `feat(labels)!`, `feat(scopes)!`, `feat(store)!`,
  `feat(query)!`, `fix(store)!` and `feat(search)!`. Consider `feat(store)!`
  for D6, which removes a public filter.

---

## Minor

- **m1. Index format in commits 12 and 13** (implementation-readiness).
  Commit 12 writes v4 with a `text` column, and commit 13 renames it without
  a version bump. Merge the rename into 12, or say so.
- **m2. `language_counts`** (implementation-readiness, codebase-details,
  zoom-out). `schema.py` has 27 MAP columns, and the count of 26 excludes
  `dataset.extras` (`:122`). The design gives no column list, `table` and
  `column` format, order or return type. It also omits `sdmx.concept.name`
  and `description` (`store.py:2558`). List the columns or derive them from
  `information_schema`, or defer the method.
- **m3. Module locations and constructors** (implementation-readiness). The
  design gives none for `Gap`, `Labels`, `RowList`, `ValueList` or
  `MissingTranslationError`.
- **m4. `MissingTranslationError` construction** (design, codebase-details,
  implementation-readiness). It must call `super().__init__(langs, held)`
  (`istring.py:120-124`) and override `__str__` (`126-131`). Pickling then
  raises `TypeError`, so add `__reduce__` and a round-trip test. Show a
  message example for a tag tuple.
- **m5. `Langs` versus `pick`** (codebase-details). An empty sequence raises
  `ValueError`, while `pick()` raises `TypeError` (`istring.py:375-377`).
  Limit "as `pick` does" to the first-held-tag rule.
- **m6. Empty text** (comprehension). State that tags are normalised and then
  de-duplicated, and that a held `''` answers and is not a gap, as `pick` and
  `COALESCE` behave.
- **m7. List subclasses** (design, zoom-out). `gaps` drifts after mutation,
  a slice returns a plain `list`, `==` ignores `gaps`, `copy()` and `+` drop
  them, and `json.dumps(Labels)` fails. Document these.
- **m8. Order** (comprehension, design, implementation-readiness). "SQL order
  for whole-table maps" has no `ORDER BY` (`store.py:6174-6177, 6189-6192`).
  Specify `ORDER BY key` and the handling of duplicate requested IDs.
- **m9. Two resolution orders** (design). `has_spatial` checks the coverage
  first, while `spatial_labels` prefers the concept (`store.py:6214`).
  `min(label)` can pair one row's text with another row's tag. Aggregate on
  `(rank, text)`.
- **m10. `preference`** (comprehension, implementation-readiness, design).
  It takes `lang` positionally, against the convention. "Tag order" and the
  result for an alternate or an unknown tag are undefined.
- **m11. Undefined terms** (comprehension). Define MAP, `und`, `zxx`,
  "Stage N", "slice N", "enriched", "terminal", "carried vectors", "working
  copy", "seal", "GH-88's contract", "GH-3" and the audit hook. Link the RFC
  at first use, and gloss the RFC findings cited.
- **m12. Section order** (comprehension, structural). Move Conventions
  before Decisions and State today to the end or an appendix. Add a Problem
  paragraph, "Open questions: none", an Errors table and an "Ingest
  (Part A)" section for D13.
- **m13. Process decisions** (structural). D10, D11, D12 and D14 repeat the
  Status, the Overview and the Work order. D1–D9 repeat their table rows.
  Keep each reason in one place.
- **m14. D13 is narrower than the problem** (design). Empty dimension names
  (`sources/sdmx.py:163-164`) and mixed maps such as `{en: "", fr: "X"}`
  remain. Extend D13 or state the per-tag rule. The test should name its
  input map.
- **m15. Hub docs** (zoom-out, codebase-details). Add 0.21 to
  `moving-a-service.md:11-21, 364` and move the `architecture.md:164` row to
  version 4. The `domain-model.md:136-141` errors are `sc.Catalog(...)`
  (`TypeError`) and calls on a `Catalog`; the dataset identifier is valid.
  Update the status line of `localised-string.md:6-7`.
- **m16. D11 audit timing** (zoom-out). Stage 4 shipped in `04b27ac`, so
  the audit lists Stage 5 sites. Record the audited commit in the file's
  header.
- **m17. Docs list** (codebase-details). Name `catalog-format.md` lines 265,
  418, 447-452, 474 and 475, and hub `architecture.md:164`.
- **m18. Counts** (codebase-details). "61 call sites" is 59 calls plus 2
  definitions.
- **m19. D9 depends on the v4 bump** (zoom-out). Record the dependency, so
  D9 is reconsidered if the bump goes away.
- **m20. Altitude** (zoom-out). The SQL helper and `as_langs` details could
  move to `store/EGM.md`. Document a loop recipe for re-indexing a corpus;
  no new command is needed.
- **m21. Style** (style). Lines 1, 358 and 377 have heading colons. Line 298
  is a colon reveal ("The index stops inventing English: …"). Lines 6-7, 65,
  118 and 211 use negative framing. Line 121 is a fragment. Lines 142-143
  are figurative. Lines 158-159 are hard to parse. The word "invented"
  appears at lines 106, 119 and 381. See `style.md` for the rewrites.
- **m22. Version provenance** (synthesizer). The sdmxlib claims were checked
  at 0.88.0 only. Re-check them at `v0.89.0` and record the commit read, as
  the hub CLAUDE.md requires.

## Reviewer reports

- `codebase-details.md`
- `design.md`
- `implementation-readiness.md`
- `comprehension.md`
- `structural.md`
- `style.md`
- `zoom-out.md`

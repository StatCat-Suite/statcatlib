# How search works

statcatlib searches its catalog with two techniques: BM25 keyword matching and
vector similarity over embeddings. Hybrid search runs both and merges their
rankings with Reciprocal Rank Fusion. This page explains each technique, how
the index is built, and how search combines with structured filters. The calls
themselves are documented in [Catalog API](api/store.md#search).

Search needs the `statcatlib[search]` extra and a folder-mode catalog. Until
`build_index()` has run, every search returns an empty result.

## What is indexed

`build_index()` writes one search row per entity per language, into a LanceDB
directory beside the catalog (`search/`):

| Entity kind | Row text | Searched by |
| --- | --- | --- |
| Dataset | Title, description, keywords, theme labels and dimension names | `cat.datasets.find`, `cat.search`, `query(...).search()` |
| Concept | The concept's name and its definition text | `cat.concepts.find`, `cat.search` |
| Code | `"{dataset title} \| {dimension}: {code label}"`, with SKOS `altLabel` synonyms and `statcat:searchContext` text | `cat.codes.find`, `cat.search` |
| Reference metadata | Sections of methodology and quality text, in chunks | `cat.reference_metadata.find`, `cat.search` with `kinds=("reference_metadata",)` |

Theme labels and dimension names make a dataset findable by what it is about
and how it is broken down, which its title often leaves out.

Codes are the largest kind, so `build_index()` limits them. It skips codelists
with more than `max_codes_per_codelist` codes (500 by default), skips codes
carrying a `statcat:searchExclude` annotation, and indexes the codelists named
in a `CodelistIndexPolicy(mode="fts_only")` for keyword search only, with no
embeddings. Short proper-noun labels, such as place names, match better by
keyword than by vector similarity, and skipping their embeddings makes a large
geography codelist index in seconds.

## One table per language

Each language has its own table, with its own full-text index:

- **Each table uses its language's stemmer.** French text is stemmed with
  French rules, so a query for "canadien" matches "canadiennes"; English text
  with English rules. A language LanceDB has no Snowball stemmer for, such as
  Chinese, Japanese, Korean or Catalan, is indexed unstemmed rather than
  with another language's rules.
- **BM25 statistics are exact per language.** BM25 weighs a term by how rare it
  is. Pooled into one table, a term common in French looked rare beside the
  English documents, and ranked too high.
- **The table is the language filter.** A query reads only the table for its
  `lang`, which defaults to `"en"`. A French query needs `lang="fr"`.

`Catalog.index_layout` reports each language's table, row count and stemmer:

```python
print(cat.index_layout)
# {"en": {"table": "search_en__000001", "rows": 946, "stemmer": "English", ...},
#  "fr": {"table": "search_fr__000001", "rows": 1173, "stemmer": "French", ...}}
```

## BM25: keyword matching

BM25 is the standard keyword-relevance formula. A document ranks higher when:

- it contains the query's **rarer** terms ("GDP" carries more weight than
  "data");
- it repeats them, with diminishing returns for each repetition;
- it is **shorter**, so the terms make up more of it.

statcatlib runs BM25 with Tantivy, the full-text engine inside LanceDB. Both
the index and the query are accent-folded, so "Montreal" matches "Montréal".
Common words are dropped from the query in its own language's list, so the
French "sur" is removed from French queries and kept in English ones.

| Option | Effect |
| --- | --- |
| `operator="or"` (default) | A document matches if it contains any query term. |
| `operator="and"` | A document must contain every query term. |
| `fuzzy=1` or `fuzzy=2` | Terms match within that many edits, so "consumr" finds "consumer". |

BM25 is best at exact words, codes, names and technical vocabulary. It misses
synonyms and paraphrases: "joblessness" does not match "unemployment".

## Vectors: matching by meaning

An embedding model turns a text into a vector, and texts with similar meanings
get nearby vectors whatever their wording. Vector search embeds the query and
returns the rows whose vectors are nearest to it.

statcatlib has no built-in model. Pass any model with an `encode` method, such
as a `sentence-transformers` model, to `build_index()` and to `Catalog`:

```python
from sentence_transformers import SentenceTransformer

import statcatlib as sc

model = SentenceTransformer("intfloat/multilingual-e5-small")
with sc.Catalog("my-catalog", embedding_model=model) as cat:
    cat.build_index(model)
    hits = cat.datasets.find("people moving between provinces", mode="vector")
```

`build_index()` records the model's name and its query and document prefixes
in `search/embeddings.json`, and a later open restores the prefixes. Models
such as the e5 family expect one (`"query: "`, `"passage: "`) on each text.

A language's table gets an approximate nearest-neighbour index (`IVF_FLAT`)
once it holds 256 rows with vectors. A smaller table is scanned in full, which
at that size is faster and exact. `IVF_FLAT` keeps each vector whole, so
approximate search returns the same neighbours as an exact scan. Indexes built
before pinaxlib 5.24.0 (pinaxlib is statcatlib's former name) used a quantised
index that misses many of them, and `Catalog.open()` warns when it finds one.

Vector search is best at synonyms, paraphrases and vague queries. It misses
exact codes and rare names, which carry little meaning to a model.

Each language's vectors live in that language's table. With a multilingual
model, an English query can still find French datasets: search the French
table with it, using `lang="fr"`.

## Hybrid: Reciprocal Rank Fusion

`mode="hybrid"` runs BM25 and vector search over the same table and merges the
two rankings with Reciprocal Rank Fusion. Each result scores the sum, over the
rankings it appears in, of

```text
1 / (k + rank)
```

where `rank` is its position in that ranking (1 for the top) and `k` is 60 by
default (`rrf_k`).

Fusion uses ranks only, so it needs no way to compare a BM25 score with a
vector distance, and no weight between the two. A result near the top of both
rankings outscores one that leads a single ranking. Each hit's `source` says
which evidence found it: `"bm25"`, `"vector"`, or `"hybrid"` for both.

```python
for hit in cat.datasets.find("labour market monthly data", mode="hybrid", limit=20).items:
    print(hit.score, hit.source, hit.payload.identifier)
```

`cat.search()` uses the same fusion a second way: it runs BM25 over every
entity kind it is given and merges their rankings into one list.

## Search with filters

`query(...).search()` and `query(...).hybrid_search()` combine search with the
relational and structural filters of a `QueryBuilder`:

```python
import statcatlib.query as q

results = (
    cat.query(sc.AggregateDataset)
    .filter(q.has_code("GEO", "ON"), status="current")
    .hybrid_search("unemployment by region")
    .limit(20)
    .execute()
)
```

The filters run first, in SQL, and the search ranks only the datasets that pass
them, so a narrow filter with a broad query still finds every match.
`search()` takes the top 200 BM25 matches among those datasets, and
`hybrid_search()` fuses the top 200 from each technique. A page that reaches
further, such as `.offset(240).limit(20)`, raises the count to `offset + limit`.
`total` counts the matches among those candidates.

The filtered identifiers reach LanceDB as an `id IN (...)` prefilter. When the
filters keep every dataset of the queried kind, as `query(sc.AggregateDataset)`
with no `.filter()` does, statcatlib sends no list. On a 50,000-dataset index a
list of all 50,000 identifiers made a BM25 query take 140 ms, against 2 ms
without one; a list of 1,000 took 6 ms.

Both methods take `lang`, which names the table to search and defaults to
`"en"`. A French query with filters needs `lang="fr"`:

```python
cat.query(sc.AggregateDataset).filter(q.has_code("GEO", "QC")).search("chômage", lang="fr")
```

## Keeping the index current

The index is a snapshot of the catalog when `build_index()` ran. A dataset
added afterwards is not searchable until the next build, and a rebuild writes a
complete new set of tables, then switches to them in one step, so a failed
rebuild leaves the previous index serving.

`build_index()` also records the annotations it read (`statcat:searchExclude`,
`statcat:searchContext`, `skos:altLabel`). When an overlay changes them in the
store afterwards, `Catalog.open()` warns that the index is out of date until
the next build. Opening a catalog never rebuilds its index, because a published
bundle must not change.

The format of the index directory is specified in the
[Bundle Format](bundle-format.md#4-the-search-index).

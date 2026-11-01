# How search works

statcatlib searches its catalog with two techniques: BM25 keyword matching and
vector similarity over embeddings. Hybrid search runs both and merges their
rankings with Reciprocal Rank Fusion. This page explains each technique, how
the index is built, and how search combines with structured filters. The calls
themselves are documented in [Catalog API](api/store.md#search).

Search runs on an `EditionView` and needs the `statcatlib[search]` extra. Each
edition has its own search index. `wc.seal()` builds it from the edition's
rows, so every sealed edition can be searched. A working copy's view,
`wc.view()`, searches the index its last `wc.build_index()` built, or before
that the index it carried from the previous edition; a product's first working
copy returns empty results until `wc.build_index()` runs.

## What is indexed

`build_index()` writes one search row per entity per language, into the
edition's search index, a LanceDB directory:

| Entity kind | Row text | Searched by |
| --- | --- | --- |
| Dataset | Title, description, keywords, theme labels and dimension names | `view.datasets.find`, `view.search`, `query(...).search()` |
| Concept | The concept's name and its definition text | `view.concepts.find`, `view.search` |
| Code | `"{dataset title} \| {dimension}: {code label}"`, with SKOS `altLabel` synonyms and `statcat:searchContext` text | `view.codes.find`, `view.search` |
| Reference metadata | Sections of methodology and quality text, in chunks | `view.reference_metadata.find`, `view.search` with `kinds=("reference_metadata",)` |

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

`EditionView.index_layout` reports each language's table, row count and stemmer:

```python
print(view.index_layout)
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
as a `sentence-transformers` model, to `wc.build_index()`, and to
`product.view()` to search a sealed edition:

```python
from sentence_transformers import SentenceTransformer

import statcatlib as sc

model = SentenceTransformer("intfloat/multilingual-e5-small")
with sc.Catalog.create("my-catalog", exist_ok=True) as cat:
    with cat.product("14100287").working_copy() as wc:
        ...  # ingest into wc
        wc.build_index(model)
        hits = wc.view().datasets.find("people moving between provinces", mode="vector")
        wc.seal()  # rebuilds the index with the same model

with sc.Catalog.open("my-catalog") as cat:
    view = cat.product("14100287").view(embedding_model=model)
    hits = view.datasets.find("people moving between provinces", mode="vector")
```

`build_index()` records the model's name and its query and document prefixes
in the index's `embeddings.json`, and `product.view()` restores the prefixes.
Models such as the e5 family expect one (`"query: "`, `"passage: "`) on each
text.

A language's table gets an approximate nearest-neighbour index (`IVF_FLAT`)
once it holds 256 rows with vectors. A smaller table is scanned in full, which
at that size is faster and exact. `IVF_FLAT` keeps each vector whole, so
approximate search returns the same neighbours as an exact scan. Indexes built
before pinaxlib 5.24.0 (pinaxlib is statcatlib's former name) used a quantised
index that misses many of them, and `product.view()` warns when it finds one.

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
for hit in view.datasets.find("labour market monthly data", mode="hybrid", limit=20).items:
    print(hit.score, hit.source, hit.payload.identifier)
```

`view.search()` uses the same fusion a second way: it runs BM25 over every
entity kind it is given and merges their rankings into one list.

## Search with filters

`query(...).search()` and `query(...).hybrid_search()` combine search with the
relational and structural filters of a `QueryBuilder`:

```python
import statcatlib.query as q

results = (
    view.query(sc.AggregateDataset)
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
view.query(sc.AggregateDataset).filter(q.has_code("GEO", "QC")).search("chômage", lang="fr")
```

## Keeping the index current

A working copy's index is a snapshot of its rows when `wc.build_index()` ran.
A dataset added afterwards is not searchable on `wc.view()` until the next
build. A rebuild writes a complete new set of tables, then switches to them in
one step, so a failed rebuild leaves the previous index serving.

`wc.seal()` rebuilds the index from the edition's rows, with the arguments of
the last `wc.build_index()` call, or the defaults when there was none, so a
sealed edition's index holds exactly the sealed rows. `statcat build-index
CATALOG PRODUCT` builds the index of a product's working copy from the command
line and leaves the working copy unsealed.

`build_index()` also records the annotations it read (`statcat:searchExclude`,
`statcat:searchContext`, `skos:altLabel`). When an overlay changes them in a
working copy afterwards, resuming the working copy warns that the index is out
of date until the next build. Opening an edition never rebuilds its index,
because a sealed edition does not change.

The format of the index directory is specified in the
[Catalog Format](catalog-format.md#4-the-search-index).

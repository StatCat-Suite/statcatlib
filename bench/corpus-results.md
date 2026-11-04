# Phase 4 corpus: cost of the no-home comparison

GH-124. The bench is `bench/corpus.py`, and the corpus generator is
`statcatlib.testing.codr_corpus`. The run below stopped at 2,506 one-table
data products, short of 7,000, because the full corpus needs about 117 GB
of disk and the brief capped it at 50 GB. The numbers at 7,000 are
extrapolated from the samples.

## Machine and versions

| | |
|---|---|
| Machine | Apple M5, 10 cores, 24 GiB RAM |
| Disk | internal SSD (Apple Fabric), APFS, macOS 26.6.2 |
| statcatlib | `b3b07dd` (branch `feat/gh-124-corpus-bench`, from `main` at `95fd7ad`, 0.13.0) |
| sdmxlib | 0.84.0 |
| Python, DuckDB, LanceDB | 3.14.8, 2.0.0, 0.40.0 |
| Date | 2026-11-03 |

Other sessions ran on the machine during the build. The one-minute load
average was 16 to 21 at the 100-product sample and 1.5 to 4 at the others;
each record in `bench.jsonl` carries its load average.

## The corpus

- `CodrShape()` defaults (`CL_GEO` 6,000 codes in 5 levels, classifications
  of 900, 500 and 300 codes, 2,000 observations a table), seed 0, one table
  a product, about one product in ten sealed under embargo.
- Location: `~/statcat-bench/corpus`, outside the repository. At the end of
  the run it held 2,516 data products (2,285 live, 231 embargoed) and the
  three structure products, in 2,979 Inventory versions, and took 42.1 GB
  (39.2 GiB). The records are in `~/statcat-bench/corpus/bench.jsonl`.
- Build time: 78 minutes (4,677 s) for the whole run, samples included.
- A one-table product takes 16.7 MB: `metadata.duckdb` 12 MB,
  `tables.ducklake` 4 MB, `search/` 0.3 MB. Its `manifest.json` is 14,484
  bytes.
- One product took 1.57 s to build (fill, index and seal) between 500 and
  1,000 products, 1.69 s between 1,000 and 2,000, and 1.83 s between 2,000
  and 2,500, averaged over each build segment.

Estimate for the full 7,000-product corpus, from these rates: 117 GB of disk
and about 4 hours. Disk is the limit the run hit. The build stopped before
the 3,000 sample because 2,506 products took 41.9 GB, which projects to
50.2 GB at 3,000.

### Multi-table products

Plan Q1 option C adds about 50 products of 5 to 20 tables to 6,500 one-table
products. The 50 multi-table products, 615 tables, were built in a separate
catalog, `~/statcat-bench/mixed`, in 281 s and 1.34 GB. The 5-table group's
time includes sealing the three structure products:

| Tables a product | Products | Build time a product (s) | Disk a product (MB) |
|---:|---:|---:|---:|
| 5 | 13 | 3.0 | 19.6 |
| 10 | 13 | 4.4 | 23.0 |
| 15 | 12 | 6.6 | 29.1 |
| 20 | 12 | 8.9 | 34.7 |

The full mixed corpus would take about 110 GB and 4 hours, so it is left
for a machine or a run with that much disk to spare.

## What the bench measures

Each sample seals three more one-table products in the building process
(**warm**: its `InventoryWriter` has cached every manifest it read before)
and three in new processes (**cold**: `multiprocessing` spawn, a fresh
writer and cache), all with the embargo off, so each enters `live` and runs
the no-home comparison. The bench wraps four functions in each process:
`cross_product.refusals` (the rules: the seal's pre-check and the Inventory
commit, two calls a seal), `cross_product.ManifestArtefacts.__call__` (a
read from disk is a call whose `(edition_id, manifest_sha256)` is not in the
cache), `InventoryWriter.full` and `WorkingCopy.seal`. The library has no
measuring hook. The OS file cache stayed warm; the first cold seal at 2,000
and at 2,500 took 440 and 544 ms in the rules, against 290 and 335 ms for
the next two, which suggests the manifests were then out of the page cache.

## Results

Medians of three seals. "Live" counts the live data products before the
seal; the no-home comparison reads each of their manifests.

| Data products | Live | Embargoed | Warm rules (ms) | Warm manifest reads | Warm seal (s) | Cold rules (ms) | Cold manifest reads | Cold read (MiB) | Cold read time (ms) | Cold seal (s) | Cold process (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 87 | 13 | 130 | 1 | 1.18 | 143 | 95 | 1.3 | 12 | 1.25 | 3.9 |
| 500 | 462 | 38 | 88 | 1 | 0.70 | 132 | 470 | 6.5 | 37 | 0.80 | 2.3 |
| 1,000 | 906 | 94 | 102 | 1 | 0.74 | 171 | 914 | 12.6 | 68 | 0.86 | 2.3 |
| 2,000 | 1,812 | 188 | 134 | 1 | 0.84 | 292 | 1,820 | 25.1 | 159 | 1.03 | 2.5 |
| 2,500 | 2,267 | 233 | 143 | 1 | 0.89 | 335 | 2,275 | 31.4 | 162 | 1.10 | 2.6 |

A cold seal reads the manifest of each live data product, each structure
product and the edition it seals (at the Inventory commit). Each manifest is read from disk twice, once for its
SHA-256 and once by `read_manifest`; the "read" column counts it once. The
warm seal reads one manifest, its own, at the Inventory commit. The Inventory head
reads (`InventoryWriter.full`, five calls a seal) took 1 to 4 ms at every
size.

At 2,286 live products, a cold seal's 335 ms in the rules split into 75 ms
computing the working copy's own artefact fingerprints from its store
(`StoreArtefacts`), 162 ms reading 2,289 manifests, and 98 ms comparing.

### Growth rate

Least squares over the four samples at load average 4 or less (462 to 2,267
live products):

| Quantity | Fit | R² | At 6,300 live (7,000 products) |
|---|---|---:|---:|
| Cold rules | 74 ms + 117 µs a live product | 0.995 | 0.81 s |
| Cold manifest reads | 4 ms + 76 µs a live product | 0.957 | 0.48 s, 91 MB |
| Warm rules | 74 ms + 31 µs a live product | 0.993 | 0.27 s |
| Cold seal, whole | 715 ms + 171 µs a live product | 0.997 | 1.79 s |

The rules' time grows linearly with the live data products: each cold data
seal reads every live data manifest once. The whole cold seal grows 54 µs a
live product faster than the rules; this bench does not attribute that part.
Building a corpus with one new process per seal costs the sum of the per-seal
cost, which is quadratic: about 0.117 ms × 6,300² / 2 = 39 minutes of rules
time over 7,000 seals.

### Batch seal and release at 2,506 products

| Operation | Live | Wall (s) | Rules (ms) | Rules calls | Manifest reads | Read (MiB) | Read time (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `Catalog.seal` of 5, warm | 2,273 | 3.65 | 440 | 3 | 8 | 0.1 | 1 |
| `Catalog.seal` of 5, cold | 2,273 | 3.93 | 633 | 3 | 2,286 | 31.6 | 163 |
| `CatalogAdmin.release`, cold (first in its process) | 2,283 | 0.23 | 224 | 1 | 2,287 | 31.6 | 163 |
| `CatalogAdmin.release`, warm (second, same `CatalogAdmin`) | 2,283 | 0.06 | 53 | 1 | 1 | 0.0 | 2 |

A cold release spends 73 % of its rules time reading manifests. Scaled to
6,300 live products, a cold release reads 6,300 manifests (91 MB, about
0.45 s on this disk), and a cold `Catalog.seal` of a batch reads them once
for the batch.

## The index question for Phase 5

Whether the comparison needs an index before Phase 5 depends on where the
release job reads manifests from. On local disk at 7,000 products, the
extrapolated comparison costs 0.81 s of a 1.8 s cold data seal and about
0.6 s of a cold release, and it reads 6,300 manifests (91 MB) each time; a
warm process pays 0.27 s.

Phase 5's release job reads the catalog from object storage, which this
bench did not measure. The rules read manifests one at a time, so a cold
release there makes about 6,300 sequential GETs: 63 s at 10 ms a GET, 189 s
at 30 ms.

Three ways to replace the reads, for the owner to choose between:

- an artefact-to-edition map, built once per Inventory version, which
  answers "which live data editions hold this URN full" without opening
  manifests;
- the Inventory 1.1 `full_artefacts` routing field
  (StatCat-Suite/sdmxlib#348). The plan proposes it for structure products
  only, and the no-home comparison reads data editions, so it would need to
  cover data entries too;
- the leaf's existing optional `LeafEntry.artefacts` (Inventory 1.0), which
  statcatlib's writer leaves empty. `cross_product.Artefacts` has its type,
  so a reader of that field can replace `ManifestArtefacts`; leaves grow by
  the artefact records.

## Reproducing

```bash
uv run python bench/corpus.py build --root ~/statcat-bench/corpus \
    --samples 100,500,1000,2000,2500,3000,4000,7000 --max-gb 50 --max-hours 8
uv run python bench/corpus.py mixed --root ~/statcat-bench/mixed
uv run python bench/corpus.py report --root ~/statcat-bench/corpus
```

A second `build` on the same root resumes the corpus. `report` prints the
table above from `bench.jsonl`, fitted over every sample, the loaded
100-product one included (cold: 106 ms + 99 µs a live product).

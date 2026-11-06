# Phase 4 Part 4: Inventory read cost at 7,000 products

GH-98. The bench is `bench/inventory_read.py`, and the corpus generator is
`statcatlib.testing.inventory_corpus`. The corpus holds Inventory documents
only, so this run measures no manifest read, and the 133 s of design decision
D5 stays for Phase 5. The design is `docs/design/catalog-on-blob-storage.md`,
revision 4.

Azurite's wall time excludes real storage latency. Azurite ran in a container
on the same machine as the reader, so its wall time is a local figure. The
columns named "estimate" are arithmetic on the measured request counts, and
10 ms a request is the figure GH-98 assumes; no real store was contacted. Every
other number in this file was measured in the run below.

## Machine and versions

| | |
|---|---|
| Machine | Apple M5, 10 cores, 24 GiB RAM |
| OS | macOS 26.6.2 |
| Container runtime | colima 0.10.3, 2 CPUs and 4 GiB, Docker 29.5.2 |
| Azurite | the image `tests/stores/_stores.py` pins, `sha256:830430c1da1a2d53...`, on 127.0.0.1 |
| statcatlib | `fdf2f11` (branch `feat/gh-98-inventory-read`, from `main` at `3a8d07b`, 0.14.0) |
| sdmxlib, obstore, msgspec | 0.86.0, 0.12.1, 0.22.0 |
| Python | 3.14.8 |
| Date | 2026-11-06 |

The run took 123 s, Azurite's start included. The one-minute load average at
its end was 3.2. Other processes ran on the machine, among them an unrelated
container.

## The corpus

`inventory_corpus` at its defaults: 7,000 products in commits of 500, seed 0,
6,299 live and 701 embargoed, one edition a product. The bench builds it twice
in one Azurite, at two prefixes.

| Shape | Leaves | Leaf bytes | Bytes a leaf | Roots | Root bytes, all | Newest root bytes |
|---|---:|---:|---:|---:|---:|---:|
| plain, no artefact records | 7,000 | 4,653,623 | 665 | 14 | 16,809,426 | 1,922,568 |
| artefacts, 10 records an entry | 7,000 | 18,051,623 | 2,579 | 14 | 16,809,426 | 1,922,568 |

The 14 roots grow from 475,551 to 1,922,568 bytes. Each carries the leaf
pointer of every product recorded so far and the change list of its own commit.

## Writes

`WriterOps` counts each store call of the writer, and a read counts as one
request and a Put as one request. No version took its time from the `Date`
`HEAD`, because the bench passes `now` to `try_commit`, so no row holds that
request. The estimate is every request in sequence at 10 ms.

| Shape | Write | Runs | Reads | Leaf Puts | Root Puts | `CURRENT` Puts | Other Puts | Leaf bytes | Last root bytes | Root bytes, all | Wall (s), median | Estimate (s) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| plain | 7000 products, batch 500 | 1 | 42 | 7000 | 14 | 14 | 1 | 4653623 | 1922568 | 16809426 | 10.17 | 70.71 |
| plain | one version of 1 | 3 | 4 | 1 | 1 | 1 | 0 | 1237 | 1555882 | 1555882 | 0.05 | 0.07 |
| plain | one version of 200 | 3 | 203 | 200 | 1 | 1 | 0 | 247400 | 1827519 | 1827519 | 0.39 | 4.05 |
| artefacts | 7000 products, batch 500 | 1 | 42 | 7000 | 14 | 14 | 1 | 18051623 | 1922568 | 16809426 | 10.43 | 70.71 |
| artefacts | one version of 1 | 3 | 4 | 1 | 1 | 1 | 0 | 5065 | 1555882 | 1555882 | 0.05 | 0.07 |
| artefacts | one version of 200 | 3 | 203 | 200 | 1 | 1 | 0 | 1013000 | 1827519 | 1827519 | 0.45 | 4.05 |

The corpus's 42 reads are 15 of `catalog.json` (one before it is written, one a
commit), 14 of `CURRENT` and 13 of the base root. Its other Put is
`catalog.json`, 108 bytes. A version of one product reads `catalog.json`, the
base root, the product's base leaf and `CURRENT`. A version of 200 reads the
same with 200 base leaves. Each leaf the versions write holds two entries, the
new one and the one it supersedes, so a version's leaf bytes are higher a leaf
than the corpus's.

## Reads

Each case's GETs pass through `RecordingFetch`. An absent object counts as a
GET of no bytes, and each read's one absent GET is the probe of the next root.
"Roots" holds the newest root and that probe. Each case ran three times with
the same counts, and the bytes are the first run's. The cold cases ran before
the bench wrote any further version, and each refresh row is a reader holding
every leaf of the version before.

- **Estimates.** Sequential is `GETs x 10 ms`. Concurrent is
  `ceil(GETs / 64) x 10 ms`. Both count requests alone and exclude transfer
  time, which grows with the bytes column.
- **Cold.** `LiveInventory.refresh()` then `editions()`, or
  `FullInventory.refresh()` then `leaves()`, on a new reader.
- **64-thread leaf prefill.** The bench fetches `CURRENT` and the root, fills a
  dictionary from a local pool of 64 threads, then serves a new reader from the
  dictionary, whose only GET is its probe. "Reader served" is the wall time of
  that last step, which verifies and decodes every leaf. No code under `src`
  does this.
- **One product, cold.** `LiveInventory.head` or `FullInventory.leaf` on a new
  reader.
- **Refresh after N changed.** `refresh()` then the full read, after a version
  that changed N products. The `refresh()` alone makes the first three GETs,
  and the leaves follow in the full read.

| Shape | Reader | Case | GETs | `CURRENT` | Roots | Leaves | Absent | Bytes | Wall (s), median (range) | Reader served (s), median | Sequential estimate (s) | Concurrent estimate (s) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| plain | LiveInventory | cold | 7003 | 1 | 2 | 7000 | 1 | 6576314 | 4.746 (4.171 to 5.006) |  | 70.03 | 1.10 |
| plain | LiveInventory | cold, 64-thread leaf prefill | 7003 | 1 | 2 | 7000 | 1 | 6576314 | 1.816 (1.719 to 1.862) | 0.058 | 70.03 | 1.10 |
| plain | LiveInventory | one product, cold | 4 | 1 | 2 | 1 | 1 | 1923347 | 0.014 (0.014 to 0.015) |  | 0.04 | 0.01 |
| plain | FullInventory | cold | 7003 | 1 | 2 | 7000 | 1 | 6576314 | 4.656 (4.612 to 4.870) |  | 70.03 | 1.10 |
| plain | FullInventory | cold, 64-thread leaf prefill | 7003 | 1 | 2 | 7000 | 1 | 6576314 | 1.758 (1.593 to 1.837) | 0.053 | 70.03 | 1.10 |
| plain | FullInventory | one product, cold | 4 | 1 | 2 | 1 | 1 | 1923347 | 0.014 (0.014 to 0.058) |  | 0.04 | 0.01 |
| plain | LiveInventory | refresh after 1 changed | 4 | 1 | 2 | 1 | 1 | 1557242 | 0.013 (0.012 to 0.017) |  | 0.04 | 0.01 |
| plain | FullInventory | refresh after 1 changed | 4 | 1 | 2 | 1 | 1 | 1557242 | 0.010 (0.010 to 0.011) |  | 0.04 | 0.01 |
| plain | LiveInventory | refresh after 200 changed | 203 | 1 | 2 | 200 | 1 | 2075042 | 0.142 (0.124 to 0.185) |  | 2.03 | 0.04 |
| plain | FullInventory | refresh after 200 changed | 203 | 1 | 2 | 200 | 1 | 2075042 | 0.132 (0.121 to 0.150) |  | 2.03 | 0.04 |
| artefacts | LiveInventory | cold | 7003 | 1 | 2 | 7000 | 1 | 19974314 | 4.926 (4.473 to 5.000) |  | 70.03 | 1.10 |
| artefacts | LiveInventory | cold, 64-thread leaf prefill | 7003 | 1 | 2 | 7000 | 1 | 19974314 | 1.734 (1.714 to 1.753) | 0.081 | 70.03 | 1.10 |
| artefacts | LiveInventory | one product, cold | 4 | 1 | 2 | 1 | 1 | 1925261 | 0.013 (0.012 to 0.030) |  | 0.04 | 0.01 |
| artefacts | FullInventory | cold | 7003 | 1 | 2 | 7000 | 1 | 19974314 | 4.878 (4.877 to 4.955) |  | 70.03 | 1.10 |
| artefacts | FullInventory | cold, 64-thread leaf prefill | 7003 | 1 | 2 | 7000 | 1 | 19974314 | 1.709 (1.660 to 1.709) | 0.074 | 70.03 | 1.10 |
| artefacts | FullInventory | one product, cold | 4 | 1 | 2 | 1 | 1 | 1925261 | 0.012 (0.011 to 0.014) |  | 0.04 | 0.01 |
| artefacts | LiveInventory | refresh after 1 changed | 4 | 1 | 2 | 1 | 1 | 1561070 | 0.026 (0.023 to 0.027) |  | 0.04 | 0.01 |
| artefacts | FullInventory | refresh after 1 changed | 4 | 1 | 2 | 1 | 1 | 1561070 | 0.013 (0.011 to 0.014) |  | 0.04 | 0.01 |
| artefacts | LiveInventory | refresh after 200 changed | 203 | 1 | 2 | 200 | 1 | 2840642 | 0.154 (0.150 to 0.157) |  | 2.03 | 0.04 |
| artefacts | FullInventory | refresh after 200 changed | 203 | 1 | 2 | 200 | 1 | 2840642 | 0.147 (0.142 to 0.150) |  | 2.03 | 0.04 |

## Limits of these numbers

- The estimates assume 10 ms a request. This run measured no real store's
  latency, throughput or throttling, and a pool of 64 threads against a real
  store is unmeasured.
- The prefill's wall time reflects one Azurite process in a 2-CPU VM, so the
  ratio of the cold to the prefill wall time holds for this setup alone.
- The reader kept one store client through a case's repeats, so "cold" means a
  new reader with no cached documents, over connections already open.
- Every corpus leaf holds one edition. A longer history holds more entries a
  leaf and more bytes.
- `RecordingFetch` counts calls. `tests/stores/test_inventory_read_counts.py`
  shows through an HTTP proxy that the calls and the requests match at 50
  products; the full run did not go through the proxy, so a client retry there
  would go uncounted.
- The run records no memory use and no reader outside this bench, such as
  `Catalog.open`.

## Reproducing

```bash
uv run python -m bench.inventory_read
uv run python -m bench.inventory_read --json results.jsonl
```

It needs a container runtime, and it removes the Azurite container it starts.
The tables under "Writes" and "Reads" are its output as printed. `just
ci` runs the module's functions at 50 products
(`tests/stores/test_inventory_read_counts.py`) and never the full run.

These tables come from sdmxlib 0.86, whose reader fetched every leaf in the
calling thread. From sdmxlib 0.87 the reader fetches leaves on a pool of 64
threads by default, and `Catalog` and `InventoryWriter` read through that pool,
so "No code under `src` does this" above holds for 0.86 alone. The script passes
`leaf_threads=1` to the cold, prefill and warm readers, so a rerun measures the
same reads. This file holds no measurement of the reader's pool.

On 2026-11-06 the owner chose concurrent leaf reads (StatCat-Suite/sdmxlib#375,
shipped in sdmxlib 0.87) with no baked summary. The summary returns as the
fallback if checklist item 9 in `docs/api/blob.md` shows the concurrent read
cannot serve plan Goal 1 on real storage.

# Bundles

A statcatlib **bundle** is the on-disk triple `meta.duckdb` + `lake.ducklake` +
`parquet/` — a catalog you can copy, ship, and mount somewhere else — with
its `search/` index, and a `bundle.manifest.json` once it is published
(see [Publishing and checking a bundle](#publishing-and-checking-a-bundle)).

The DuckLake catalog file embeds an absolute `data_path` recorded at write
time. Call it the **stamp**. It does two jobs at once: it locates the
observation parquet, and DuckLake compares against it at ATTACH time.

## The deployments

| Deployment          | Catalog files             | Parquet             | Who knows where the data is     |
|:--------------------|:--------------------------|:--------------------|:--------------------------------|
| **Co-located**      | local                     | `<bundle>/parquet/` | the bundle's location           |
| **Split**           | local                     | object storage      | **the stamp, and nothing else** |
| **Sync-then-serve** | pulled local at pod start | object storage      | nobody, until you rebind        |

The two ends need opposite handling. For a
co-located bundle the stamp carries no information — it is derivable from
where the bundle sits — so a stale value is noise and rewriting it is
always safe. For a split bundle the stamp is the *only* record of where
the data lives, and overwriting it costs a re-sync to recover.

A bundle path alone does not show which deployment it is. The stamp and the
bundle's contents do, and `open_bundle` reads both before it attaches.

## `open_bundle`

```python
open_bundle(
    bundle_uri: str | Path,
    *,
    data: DataPolicy = Auto(),
    access_mode: Literal["read_write", "read_only"] = "read_write",
    azure_credentials: dict[str, str] | None = None,
) -> Generator[Catalog]
```

A context manager yielding an open [`Catalog`](store.md). The default
works without the caller knowing the layout:

```python
import statcatlib as sc

with sc.open_bundle("bundles/my-catalog") as cat:
    cat.query(sc.AggregateDataset).limit(5).collect()
```

### Policies

Pass `data=` to override the judgement.

| Policy                    | Meaning                                                       |
|:--------------------------|:--------------------------------------------------------------|
| `Auto()`                  | Read the stamp and decide. The default.                       |
| `Embedded()`              | Honour the stamp; never write. **Pin this in production.**    |
| `Colocated()`             | `<bundle>/parquet/` is the truth; rewrite the stamp to match. |
| `At(path, persist=False)` | Use this path, ignore the stamp.                              |

`Auto` dispatches on what it finds:

| Stamp           | `<bundle>/parquet/`              | Action                       |
|:----------------|:---------------------------------|:-----------------------------|
| remote scheme   | —                                | honour it, no write          |
| local, resolves | —                                | honour it, no write          |
| local, missing  | exists                           | bundle moved → rewrite to it |
| local, missing  | missing, lake references no data | rewrite (nothing to lose)    |
| local, missing  | missing, lake references data    | raise `BundleDataPathError`  |

!!! note "Opening is not re-stamping"

    `Auto` writes only when the bundle has demonstrably moved, so the
    steady state is zero-write and lock-free — N read-only workers can
    open one bundle off a shared mount concurrently. Deliberately
    repointing a bundle is a separate act: `statcat bundle rebind`, or
    `At(..., persist=True)`.

!!! warning "Production should pin `Embedded()`"

    `Auto` self-heals a moved bundle, which is right for dev and CLI use
    and wrong under a pod: if the stamp stops resolving, the useful
    outcome is a crashloop, not a quiet repoint at an empty directory.

```python
# serving: honour what the init container stamped, never write, share the mount
with sc.open_bundle(uri, data=sc.Embedded(), access_mode="read_only") as cat:
    ...
```

### Credentials are not part of the bundle

`azure_credentials` is supplied by the deployment, never baked in. The
bundle declares **where** the data is; the deployment supplies **how to
authenticate**. A bundle can therefore be copied and shared without
carrying secrets.

## `inspect_bundle`

```python
inspect_bundle(source: str | Path) -> BundleInfo
```

Reports where a bundle says its data is and what `Auto` would do — reads
only, no write lock, changes nothing.

!!! danger "A stale stamp does not fail at `ATTACH`"

    It fails on the first query that reads observations. Metadata-only
    queries — `count(*)`, `facets()` — answer correctly from the catalog
    either way. So a misconfigured pod can start, attach, pass a health
    check and serve counts, and fail on its first observation query.

    Run `inspect_bundle` at application startup to turn that into a
    startup failure.

```python
info = sc.inspect_bundle("bundles/my-catalog")
if info.will_write or info.target is None and info.verdict.startswith("ambiguous"):
    raise SystemExit(info.verdict)
```

`BundleInfo` carries:

| Field              | Meaning                                                                    |
|:-------------------|:---------------------------------------------------------------------------|
| `bundle`           | The bundle directory.                                                      |
| `stamp`            | The `data_path` recorded in the lake, verbatim.                            |
| `is_remote`        | The stamp carries a remote URI scheme.                                     |
| `stamp_resolves`   | The stamp names an existing directory. `None` when remote.                 |
| `colocated_exists` | `<bundle>/parquet/` is present.                                            |
| `data_files`       | Parquet files the lake references. Zero means the stamp points at nothing. |
| `target`           | What `Auto` would ATTACH with. `None` means "omit `DATA_PATH`".            |
| `will_write`       | `Auto` would rewrite the stamp.                                            |
| `verdict`          | One line naming the layout and the action, for logs.                       |

## Publishing and checking a bundle

A bundle is published by writing its `bundle.manifest.json` last. The
manifest format is sdmxlib's (`sdmxlib.bundle`, from sdmxlib 0.72): a fresh
`token` per publication, which caches and ETags key on, and a sha256 for
every file. A statcat bundle is an sdmxlib bundle plus the `statcat`
schema and `search/`, so statcatlib adds a `statcat` section describing
them, and checks that sit on top of sdmxlib's. [The statcat bundle
format](../bundle-format.md) specifies all of it field by field.

```python
from sdmxlib.bundle import Producer

from statcatlib.bundle import check_bundle, publish_bundle

with sc.Catalog("out/my-catalog") as cat:
    ...  # ingest
    cat.build_index()

publish_bundle("out/my-catalog", bundle_id="my-catalog", producer=Producer(name="my-builder", version="1.2"))
assert check_bundle("out/my-catalog").ok
```

The `statcat` section records:

| Field                | Meaning                                                              |
|:---------------------|:---------------------------------------------------------------------|
| `format`             | The section's own version, `2`.                                      |
| `statcatlib_version` | The statcatlib that published it.                                    |
| `datasets`           | Rows in `statcat.dataset`.                                           |
| `search`             | `null` when no index is built; else its `index_version`, `embedding_model`, each language's `rows` and `stemmer`, and `annotations`: the annotation `types` the index read and their `fingerprint`. |

`check_bundle` runs sdmxlib's checks (manifest, files, lake, bindings,
structural references) and then the `statcat` check: the schema is there,
the dataset count matches, every `sdmx_dataflow_urn` names a stored
dataflow, and `search/` is present exactly when declared, each language
stemmed as declared and holding the declared rows, and the store's
annotations of the index's types matching its fingerprint.
`references=False` skips the structural check, the costly one.

!!! warning "Publish after `build_index()`, check before `rebind`"

    The manifest lists every file, so anything written afterwards fails
    the check: an index built after publishing, or a stamp rewritten by
    `statcat bundle rebind` or by `Auto` repointing a moved bundle (both
    change `lake.ducklake`). Run the check where a bundle is built or
    received, before deploy-time rebinding, and never on the serving path.
    Opening and searching a bundle, read-only or not, changes nothing.

### Annotation overlays and search

`build_index()` reads three annotation types: `statcat:searchExclude`
keeps a code out of search, `statcat:searchContext` adds context to its
text, and `skos:altLabel` adds synonyms (`sc.SEARCH_ANNOTATION_TYPES`).
An annotation overlay touching one of them changes what the index should
hold, and **nothing rebuilds the index for you**. A rebuild is always an
explicit `build_index()`, never something an open does in a pod.

- **Folded overlays** (`sdmxlib.bundle.fold_overlays`) are detected. The
  index records a fingerprint of the annotations it was built from, so
  until `build_index()` and `publish_bundle` are run again, every
  `Catalog.open()` **warns** and `check_bundle` reports a finding. The
  open warns rather than fails: a stale index should not take structure
  queries and observations down with it. Turn the warning into an error
  with `warnings.filterwarnings` if you want it strict.
- **Serve-time overlays** (`apply_overlays` in sdmxlib-rest) never reach
  the bundle, so search does not see them. `overlay_affects_search(overlay)`
  returns the entries that would need a rebuild to reach search; call it
  where overlays are loaded, to log or refuse them.

```python
from sdmxlib.bundle import read_overlay

from statcatlib.bundle import overlay_affects_search

if entries := overlay_affects_search(read_overlay("overlays/geo.json")):
    raise SystemExit(f"{len(entries)} entries change search: fold them and run build_index()")
```

Overlays of any other type (display hints, layouts, default selections)
leave search alone and need no rebuild.

### Synthetic bundles

`statcatlib.testing.synthetic_catalog(path, shape, seed=)` builds a
published bundle of any shape from `sdmxlib.testing.Shape`: very large
codelists, deep or wide hierarchies, many languages. Use it for stress
tests and benchmarks without shipping a real bundle.

## `read_current_data_path`

```python
read_current_data_path(source: str | Path) -> str
```

The stamp, verbatim — including its trailing slash, and without
interpreting local paths or remote URIs differently. Accepts a bundle
directory or the `lake.ducklake` file itself. Use it when you want the raw
value (to log it, to assert it is remote, to swap an account);
`inspect_bundle` is usually the better tool, since it also tells you what
an open will decide.

## `normalize_lake_data_path`

```python
normalize_lake_data_path(
    lake_path: Path,
    target_data_path: str | Path,
    *,
    scratch_dir: Path | None = None,
    force: bool = False,
) -> Path
```

The rewrite primitive behind `statcat bundle rebind`. Idempotent and
read-first: if the stamp already matches, no write lock is taken and no
UPDATE is issued.

Rewriting a **remote** stamp to a **local** path is refused unless `force`
is passed — that direction discards the only record of where a split
bundle's data lives. `scratch_dir` handles a lake on a read-only mount by
byte-copying it and rewriting the copy, returning the copy's path.

## CLI

| Command                                | Does                                                  |
|:---------------------------------------|:------------------------------------------------------|
| `statcat bundle explain <dir>`           | Print the stamp, the layout, and what an open would do. |
| `statcat bundle rebind <dir> --to <uri>` | Point it at where the data actually is.               |
| `statcat bundle attach-cmd <dir>`        | Print the `ATTACH` for ad-hoc `duckdb` CLI use.       |
| `statcat bundle check <dir>`             | Check a published bundle; exits 1 on any finding.     |

`explain` exits 1 when the layout is ambiguous, so it composes:

```bash
statcat bundle rebind /data/bundle --to "abfss://container@acct.dfs.core.windows.net/my-bundle/parquet/" \
  && statcat bundle explain /data/bundle
```

That pair is the init-container step for sync-then-serve: the sync leaves a
bundle nobody stamped, `rebind` says where the data went, and the serving
container then opens with `Embedded()`.

`rebind` refuses a remote → local rewrite without `--force`, for the same
reason `normalize_lake_data_path` does.

## What has to stay local

Only `parquet/` may be remote. `meta.duckdb`, `lake.ducklake`, and
`search/` are local to the reader. This page assumes that throughout: it
makes the stamp the only location a split bundle records.

Lifting it — a bundle whose catalog files also live on object storage,
attached in place with no sync step — works at the DuckDB layer, and
statcatlib does not support it yet.

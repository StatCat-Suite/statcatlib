# Bundles

A statcatlib **bundle** is the on-disk triple `meta.duckdb` + `lake.ducklake` +
`parquet/` — a catalog you can copy, ship, and mount somewhere else.

The DuckLake catalog file embeds an absolute `data_path` recorded at write
time. Call it the **stamp**. It does two jobs at once: it locates the
observation parquet, and it is what DuckLake compares against at ATTACH
time. Everything on this page follows from that.

## The deployments

| Deployment          | Catalog files             | Parquet             | Who knows where the data is     |
|:--------------------|:--------------------------|:--------------------|:--------------------------------|
| **Co-located**      | local                     | `<bundle>/parquet/` | the bundle's location           |
| **Split**           | local                     | object storage      | **the stamp, and nothing else** |
| **Sync-then-serve** | pulled local at pod start | object storage      | nobody, until you rebind        |

The distinction matters because the two ends want opposite things. For a
co-located bundle the stamp carries no information — it is derivable from
where the bundle sits — so a stale value is noise and rewriting it is
always safe. For a split bundle the stamp is the *only* record of where
the data lives, and overwriting it costs a re-sync to recover.

A consumer handed a bundle path cannot tell these apart by looking. The
bundle can, immediately and cheaply — so `open_bundle` asks it.

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
authenticate**. Keeping those separate is what lets "just point at the
bundle" work without the bundle becoming a credential store.

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
    check, serve counts, and only then fall over.

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
| `statcat bundle explain <dir>`           | What does this bundle say, and what would an open do? |
| `statcat bundle rebind <dir> --to <uri>` | Point it at where the data actually is.               |
| `statcat bundle attach-cmd <dir>`        | Print the `ATTACH` for ad-hoc `duckdb` CLI use.       |

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
`search/` are local to the reader. That invariant is what reduces a split
bundle to a single variable, and everything above assumes it.

Lifting it — a bundle whose catalog files also live on object storage,
attached in place with no sync step — works at the DuckDB layer already
but is not yet supported by statcatlib. See issue #121.

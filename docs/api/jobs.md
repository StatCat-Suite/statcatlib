# The jobs configuration

A catalog in object storage runs two server jobs: the release job
(`statcat release`), which records uploads and releases them on schedule, and
the GC job (`statcat gc`), which deletes what no edition needs. Both read one
TOML file, the jobs configuration. A deployment keeps the file in one ConfigMap,
reviews it in a PR, and mounts it into both jobs. Each run checks the file and
logs its SHA-256, so two jobs running different copies show in the logs.

statcatlib reads and checks the file. [The release job](release.md) reads its
`[retention]` and requires its `[access]`, and [the GC job](gc.md) reads its
`[retention]`.

```python
from statcatlib.jobs import load_jobs_config

config = load_jobs_config("/etc/statcat/jobs.toml")
config.retention.retire_after  # datetime.timedelta(days=90)
config.sha256                  # the file's SHA-256
```

`load_jobs_config` raises `JobsConfigError`, a `ValueError`, for any refusal
below, naming the file and the key or the values compared
([reference](../reference/jobs.md)).

## The file

```toml
format_version = 1

[retention]
backup = "30d"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
keep_roots = 10
retire_after = "90d"
upload_grace = "24h"
pod_staleness = "5m"

[access]
storage_account = "/subscriptions/…/resourceGroups/…/providers/Microsoft.Storage/storageAccounts/acct"
container = "catalogs"
allowlist = [
  { object_id = "…", name = "release job" },
  { object_id = "…", name = "publish job" },
  { object_id = "…", name = "gc job" },
  { object_id = "…", name = "pods" },
  { object_id = "…", name = "search pods" },
  { object_id = "…", name = "preview deployment" },
]
# Assignments known to exist above the account, one per scope; each must appear in the listing.
sentinels = [
  "/subscriptions/…/resourceGroups/…/providers/Microsoft.Authorization/roleAssignments/…",
  "/subscriptions/…/providers/Microsoft.Authorization/roleAssignments/…",
]

[expiry]
enabled = false
not_before = "2027-01-15T00:00:00Z"
namespace = "statcat"
deployments = ["app=statcat-hot", "app=statcat-base", "app=statcat-search", "app=statcat-preview"]
port = 8000
```

`format_version` is required and must be `1`. `[retention]` is required.
`[access]` is optional to the loader, since GC reads none of it, and the
release job requires it. `[expiry]` is optional and defaults to
`enabled = false`. A key the file does not define is refused at every level,
so a misspelt key never falls back to a default.

A duration is a string `<n><unit>`: a whole number and one of `s`, `m`, `h`
and `d`, such as `"24h"` or `"35d"`. A bare number, a fraction, a negative
value, a space or another unit is refused. An instant is RFC 3339 with `Z` or
an offset, written as a string or as a TOML offset date-time.

## `[retention]`

| Key | Governs | Read by | Default | Specification |
|---|---|---|---|---|
| `backup` | Nothing directly; checked against `gc_window` | both | required | backup retention (§11.6) |
| `gc_window` | How far back GC reads the Inventory; when a retired edition's directory and a folded registration may go | GC | required | the `data/` GC window (§11.4) |
| `tombstone_window` | The fold refuses a registration older than this, and GC deletes such a registration. Tombstones themselves are never dropped. | both | required | the tombstone window (§11.2, §11.6) |
| `inventory_window` | When expiry may take a root | release, `--expire` | required | W (§11.6) |
| `keep_roots` | The newest roots expiry never takes, and GC always reads | both | 10 | K (§11.6) |
| `retire_after` | When the release job retires a `superseded` or `withdrawn` entry, by its `state_changed_at` and the storage clock | release | required | the retire window (§6.4, §11.6) |
| `upload_grace` | The age below which GC deletes no `data/` file; longer than the longest upload | GC | 24h | the upload grace period (§11.6) |
| `pod_staleness` | Nothing directly; checked against `inventory_window` | both | required | the pod staleness bound (§11.6) |

Only `keep_roots` and `upload_grace` have defaults, the specification's. Every
window that decides a deletion is written out.

The loader runs `RetentionPolicy.check()`, which refuses the file when any of
these fails, naming both values:

1. `backup <= gc_window <= tombstone_window`;
2. `inventory_window >= gc_window + 1d`;
3. `pod_staleness < inventory_window`;
4. `keep_roots >= 2`;
5. every duration is positive.

```text
jobs.toml: retention: backup (40d) must not exceed gc_window (35d)
```

GC refuses to start on a failed check, and the release job stops before its
first step.

## `[access]`

The release job's [access precondition](release.md#the-access-precondition)
reads this table: before any release, it lists the role assignments that let a
principal read the container's blobs and compares each principal with the
allowlist. The job refuses a table whose `container` or `storage_account` names
another container or account than the catalog's.

| Key | Meaning | Required |
|---|---|---|
| `storage_account` | The storage account's resource id | yes |
| `container` | The catalog's container | yes |
| `allowlist` | The principals allowed a blob-read role, each `{ object_id, name }`. A group is one principal, by its own object id. | yes |
| `sentinels` | Role assignment ids the listing must return: one known assignment at each scope above the account, so a listing that cannot see inherited assignments fails closed. May be empty. | yes |
| `scopes` | Scopes above the account where the access-check role is also assigned, listed only when the account-scoped role cannot see inherited assignments | no |

## `[expiry]`

| Key | Meaning | Required when `enabled = true` |
|---|---|---|
| `enabled` | Whether a `statcat release --expire` run may expire Inventory versions; `false` by default. The flag arrives with expiry (Phase 5 Part 6). | no |
| `not_before` | Gate 1: no expiry before this instant | yes |
| `namespace` | The Kubernetes namespace of the pods gate 2 reads | yes |
| `deployments` | Gate 2's label selectors; statcat-serve's manifests must use the same labels | yes |
| `port` | The port each pod reports its `sdmxlib_version` on | yes |

With `enabled = false` the other keys may be left out.

## The `pending/` registrations

Both jobs read a registration, `pending/<product_id>/<edition_id>.json`, with
`decode_registration(path, raw)`. It returns a `Registration(product_id,
edition_id, release_at)`, whose `edition_time` is the time the edition id, a
UUIDv7, carries. It raises `ValueError` for any document other than the one
`statcat upload` writes: two keys, `edition_id` then `release_at`, compact,
`edition_id` equal to the file name and a canonical UUIDv7, and `release_at`
`null` or in UTC with `Z` and milliseconds ([The catalog
format](../catalog-format.md#15-the-pending-registrations)).

```python
from statcatlib.jobs import decode_registration

registration = decode_registration(path, raw)
registration.edition_time  # the tombstone window is measured from it
```

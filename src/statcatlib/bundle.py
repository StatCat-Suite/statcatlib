"""Portable bundle support — where a bundle's observation parquet lives.

A statcat bundle is the on-disk triple ``meta.duckdb`` + ``lake.ducklake`` +
``parquet/`` (or a remote URI in place of the local ``parquet/``). The
DuckLake catalog file embeds an absolute ``data_path`` string recorded at
write time — the *stamp*. The stamp does two jobs at once: it is the
locator for the parquet, and it is what DuckLake compares against at
ATTACH time.

Who is authoritative about the stamp depends on how the bundle was
deployed, and the consumer cannot tell by looking:

- **Co-located** — everything in one directory. The stamp carries no
  information (it is derivable from the bundle location), so a stale
  value is noise and rewriting it is always safe.
- **Split** — catalog files local, parquet on object storage. The stamp
  is the *only* record of where the data lives; rewriting it destroys
  information and costs a re-sync to recover.

`open_bundle` therefore does not ask the caller which of those it is.
It reads the stamp and dispatches on what it finds — see `Auto`.

Two DuckLake behaviours shape this module:

- ``ATTACH`` with **no** ``DATA_PATH`` option honours the stamp and skips
  the comparison entirely. To honour the stamp, `open_bundle` passes no
  ``DATA_PATH`` at all.
- A stale stamp does **not** fail at ATTACH. It fails at the first query
  that reads observations; metadata-only queries (``count(*)``, facets)
  answer correctly from the catalog. So a bad deploy can pass a health
  check and fail later, which is why `inspect_bundle` checks that the
  stamp resolves before DuckDB reads from it.

A bundle is published by writing sdmxlib's ``bundle.manifest.json`` last.
`publish_bundle` writes it with a ``statcat`` section describing the catalog
and its search index, and `check_bundle` runs sdmxlib's checks and then
`check_catalog` against that section (GH-3).
"""

from __future__ import annotations

import errno
import json
import shutil
from collections.abc import Collection, Mapping
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import duckdb
from attrs import field, frozen
from sdmxlib.bundle import (
    MANIFEST_NAME,
    Finding,
    Manifest,
    Overlay,
    OverlayEntry,
    Producer,
    Provenance,
    Report,
    write_manifest,
)
from sdmxlib.bundle import check_bundle as sdmx_check_bundle

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from sdmxlib import LocalRegistry

    from statcatlib.store import Catalog
    from statcatlib.store.lance import LanceIndex, LanguageLayout

__all__ = [
    "STATCAT_SECTION",
    "STATCAT_SECTION_FORMAT",
    "At",
    "Auto",
    "BundleDataPathError",
    "BundleInfo",
    "Colocated",
    "DataPolicy",
    "Embedded",
    "catalog_section",
    "check_bundle",
    "check_catalog",
    "inspect_bundle",
    "normalize_lake_data_path",
    "open_bundle",
    "overlay_affects_search",
    "publish_bundle",
    "read_current_data_path",
]

#: The DuckLake catalog file inside a bundle.
_LAKE_FILENAME = "lake.ducklake"

#: URI schemes that mean "not on this filesystem". A stamp carrying one of
#: these is authoritative by construction: a bundle directory cannot host an
#: ``abfss://`` path, so there is no reading under which overwriting it is
#: what the caller meant.
_REMOTE_SCHEMES = ("abfss://", "az://", "s3://", "s3a://", "gs://", "gcs://", "http://", "https://", "r2://")


class BundleDataPathError(RuntimeError):
    """The bundle's parquet location could not be determined.

    Raised by `Auto` when the stamp names a directory that is not there
    *and* there is no ``<bundle>/parquet/`` to fall back to, while the
    lake still references data files. Guessing either way would be wrong:
    rewriting the stamp destroys the only pointer to the data, and
    honouring it yields a catalog that answers counts correctly and
    fails on the first real read.
    """


@frozen
class Embedded:
    """Honour whatever path the producer stamped into the lake.

    ATTACHes with no ``DATA_PATH`` option at all, so DuckLake uses the
    stamp and performs no comparison. Never writes to the lake.

    This is what a serving deployment should pin. `Auto` self-heals a
    moved bundle, which is right for dev and CLI use and wrong in
    production: if the stamp stops resolving under a pod, the useful
    outcome is a crashloop, not a quiet repoint at an empty directory.
    """


@frozen
class Colocated:
    """Treat ``<bundle>/parquet/`` as the truth and rewrite the stamp to match.

    The co-location contract: the bundle directory holds its own data, so
    the stamp is derivable and a stale value is noise. This is the right
    policy for a bundle that has been copied or moved between mounts.
    """


@frozen
class At:
    """Use an explicit path, ignoring the stamp.

    ATTACHes with ``DATA_PATH`` and ``OVERRIDE_DATA_PATH``, so the path
    need not match the stamp — not even in spelling — and read-only opens
    work too. The override lasts for this open only: with
    ``persist=False``, files written during it land under *path* while the
    stamp still names the old location, so a later open that honours the
    stamp will not see them.
    """

    path: str | Path
    """A local directory or a remote URI (``abfss://``, ``s3://``, ``gs://``).

    A trailing slash is added if missing.
    """

    persist: bool = field(default=False, kw_only=True)
    """Write the path into the lake so later opens find it without being told again.

    Defaults to `False` — pointing one open at a location is not the same
    act as re-stamping the bundle, and conflating the two is what made the
    old default destructive. The persisting form is `statcat bundle rebind`.
    """


@frozen
class Auto:
    """Read the stamp and decide — the default.

    ==========================  =====================  =========================
    stamp                       ``<bundle>/parquet/``  action
    ==========================  =====================  =========================
    remote scheme               —                      honour it, no write
    local, resolves             —                      honour it, no write
    local, missing              exists                 moved → rewrite to it
    local, missing              missing, no data       rewrite (nothing to lose)
    local, missing              missing, has data      `BundleDataPathError`
    ==========================  =====================  =========================

    It asks whether the stamp resolves, which covers "is it remote?" and
    also parquet on a local mount other than ``<bundle>/parquet/``.

    Writes are rare by construction. Every deployment whose bundle has
    not moved opens with zero writes and takes no lock, which is what
    lets N pods open one bundle off a shared mount concurrently.
    """


#: Where a bundle's observation parquet lives, and who gets to say so.
type DataPolicy = Auto | Embedded | Colocated | At

#: The default policy, as a singleton — a mutable default would be a shared
#: instance anyway, and these carry no state.
_AUTO = Auto()


@frozen
class BundleInfo:
    """What `inspect_bundle` found, and what `Auto` would do about it.

    The discriminator made inspectable. The bundle can tell a co-located
    layout from a split one immediately and cheaply; the caller cannot.
    This is that judgement as a value — for logging, for an init
    container's dry run, and for an application to fail at startup rather
    than on the first query that reads observations.
    """

    bundle: Path
    """The bundle directory."""

    stamp: str
    """The ``data_path`` literal recorded in the lake, verbatim."""

    is_remote: bool
    """The stamp carries a remote URI scheme."""

    stamp_resolves: bool | None
    """The stamp names an existing directory. `None` when remote — not checked."""

    colocated_exists: bool
    """``<bundle>/parquet/`` is present."""

    data_files: int
    """Parquet files the lake references. Zero means the stamp points at nothing."""

    target: str | None
    """What `Auto` would ATTACH with. `None` means "omit ``DATA_PATH``, honour the stamp"."""

    will_write: bool
    """`Auto` would rewrite the stamp."""

    verdict: str
    """One line naming the layout and the action, for logs and CLI output."""


def _is_remote(raw: str) -> bool:
    """Does this ``data_path`` literal point off the local filesystem?"""
    return raw.startswith(_REMOTE_SCHEMES)


def _with_slash(raw: str) -> str:
    """Canonicalise a ``data_path`` literal for comparison.

    DuckLake stores ``data_path`` with a trailing slash. We always compare
    against that canonical form so callers can pass either ``/tmp/parquet``
    or ``/tmp/parquet/`` without surprises.
    """
    return raw if raw.endswith("/") else f"{raw}/"


def _colocated_target(bundle_dir: Path) -> str:
    """The DATA_PATH literal for a bundle that holds its own parquet."""
    return f"{bundle_dir / 'parquet'}/"


def _lake_file(source: str | Path) -> Path:
    """Resolve *source* — a bundle directory or the lake file — to the lake file."""
    lake_path = Path(source)
    if lake_path.is_dir():
        lake_path = lake_path / _LAKE_FILENAME
    return lake_path


def read_current_data_path(source: str | Path) -> str:
    """Read the ``data_path`` the producer stamped into a bundle's lake.

    The path a bundle *says* its parquet lives at. Split-deployment
    consumers keep ``meta.duckdb`` and ``lake.ducklake`` local and leave
    the observation parquet on object storage; this is how they learn
    where that is without duplicating the producer's configuration.

    Call it directly when the path is wanted *before* opening — to log it,
    to assert it is remote, or to swap an account. `inspect_bundle` is
    usually the better tool, since it also says what `Auto` will do.

    Reads DuckLake's own ``ducklake_metadata`` rather than a copy: the lake
    already records this, and a second copy in statcatlib's schema could disagree
    with it after a `normalize_lake_data_path`.

    Opens the lake file read-only — no write lock, no contention with
    concurrent readers.

    Args:
        source: A bundle directory, or the ``lake.ducklake`` file itself.
            Accepting the directory keeps the layout an implementation
            detail rather than something every caller must know.

    Returns:
        The embedded ``data_path``, verbatim — including its trailing slash.
        Local paths and remote URIs (``abfss://``, ``s3://``) are returned
        alike; no attempt is made to interpret one.

    Raises:
        FileNotFoundError: *source* names neither a lake file nor a
            directory containing one.
        RuntimeError: The lake has no ``data_path`` entry — not a DuckLake
            catalog, or its schema has changed.
        TypeError: The stored value is not a string.
    """
    lake_path = _lake_file(source)
    if not lake_path.exists():
        msg = f"{source}: no {_LAKE_FILENAME} here — not a statcatlib bundle?"
        raise FileNotFoundError(msg)
    conn = duckdb.connect(str(lake_path), read_only=True)
    try:
        row = conn.execute("SELECT value FROM ducklake_metadata WHERE key = 'data_path'").fetchone()
    finally:
        conn.close()
    if row is None:
        msg = f"{lake_path}: no 'data_path' entry in ducklake_metadata — not a DuckLake catalog or schema has changed"
        raise RuntimeError(msg)
    data_path = row[0]
    if not isinstance(data_path, str):
        msg = f"{lake_path}: ducklake_metadata.data_path is {type(data_path).__name__}, expected a string"
        raise TypeError(msg)
    return data_path


def _count_data_files(lake_path: Path) -> int:
    """How many parquet files the lake references.

    Zero means the stamp addresses nothing: either the bundle holds no
    observations yet, or its rows are still inlined in the catalog. Either
    way a rewrite loses no information, which is what lets `Auto` repoint
    an empty relocated bundle instead of refusing it.

    A lake whose schema lacks the table is treated as empty rather than
    raising — this only ever widens what `Auto` will fix.
    """
    conn = duckdb.connect(str(lake_path), read_only=True)
    try:
        row = conn.execute("SELECT count(*) FROM ducklake_data_file").fetchone()
    except duckdb.Error:
        return 0
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


def inspect_bundle(source: str | Path) -> BundleInfo:
    """Report where a bundle says its data is, and what `Auto` would do.

    The dry run behind ``statcat bundle explain``. Reads only — opens the
    lake read-only, takes no write lock, and changes nothing.

    Because a stale stamp surfaces at first read rather than at ATTACH,
    this is the check that turns a latent runtime failure into a startup
    one. An application that validates configuration at boot should call
    it there.

    Args:
        source: A bundle directory, or the ``lake.ducklake`` file itself.

    Returns:
        A `BundleInfo` describing the layout and the action `Auto` would
        take. Reports rather than raises: the ambiguous case comes back
        with a ``verdict`` saying so and ``target`` of `None`, where
        `open_bundle` would raise `BundleDataPathError`.

        A *source* naming neither a lake file nor a directory containing
        one propagates `FileNotFoundError` from `read_current_data_path`.
    """
    lake_path = _lake_file(source)
    bundle_dir = lake_path.parent
    stamp = read_current_data_path(lake_path)
    remote = _is_remote(stamp)
    resolves = None if remote else Path(stamp).is_dir()
    colocated = (bundle_dir / "parquet").is_dir()
    data_files = _count_data_files(lake_path)

    if remote:
        target, will_write, verdict = None, False, f"split — parquet on object storage at {stamp}"
    elif resolves:
        target, will_write, verdict = None, False, f"stamp resolves — parquet at {stamp}"
    elif colocated:
        target = _colocated_target(bundle_dir)
        will_write = True
        verdict = f"bundle moved — would rewrite stamp to {target}"
    elif data_files == 0:
        target = _colocated_target(bundle_dir)
        will_write = True
        verdict = f"no data files — would rewrite stamp to {target}"
    else:
        target, will_write = None, False
        verdict = (
            f"ambiguous — stamp {stamp} does not exist, no {bundle_dir / 'parquet'}/, "
            f"and the lake references {data_files} data file(s)"
        )

    return BundleInfo(
        bundle=bundle_dir,
        stamp=stamp,
        is_remote=remote,
        stamp_resolves=resolves,
        colocated_exists=colocated,
        data_files=data_files,
        target=target,
        will_write=will_write,
        verdict=verdict,
    )


def _write_data_path(lake_path: Path, target: str) -> None:
    """Acquire the lake write lock and UPDATE ``ducklake_metadata.data_path``."""
    conn = duckdb.connect(str(lake_path), read_only=False)
    try:
        conn.execute(
            "UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'",
            [target],
        )
    finally:
        conn.close()


def normalize_lake_data_path(
    lake_path: Path,
    target_data_path: str | Path,
    *,
    scratch_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Ensure the lake's embedded ``data_path`` equals ``target_data_path``.

    The rewrite is idempotent and read-first: if the embedded value
    already matches the target (after trailing-slash normalization), no
    write lock is acquired and no UPDATE is issued — safe to call from N
    concurrent readers in steady state.

    Rewriting a **remote** stamp to a **local** path is refused unless
    ``force`` is passed. That direction discards the only record of where
    a split bundle's data lives, and recovering costs a re-sync; it is
    occasionally what someone means (after pulling the data down), never
    something to arrive at by default.

    Args:
        lake_path: Path to the ``lake.ducklake`` file (a DuckDB database).
        target_data_path: The desired DuckLake DATA_PATH. May be a local directory or a remote URI
            (``abfss://``, ``s3://``, ``gs://``). A trailing slash is added if missing.
        scratch_dir: When supplied and the rewrite fails because ``lake_path`` is on a read-only mount, the
            lake file is byte-copied into ``scratch_dir`` and the rewrite is retried there. Returns the path
            of the copy.
        force: Permit a remote → local rewrite. Ignored for every other direction.

    Returns:
        The path to the rewritten (or unchanged) lake file. Usually
        ``lake_path``; switches to ``scratch_dir / lake.ducklake`` only
        when the read-only fallback fires.

    Raises:
        BundleDataPathError: The rewrite would replace a remote stamp with
            a local path and ``force`` was not passed.
        duckdb.IOException: The lake could not be opened for writing and no
            ``scratch_dir`` was supplied to fall back to.
        OSError: As above (``PermissionError`` included), or the write
            failed for a reason other than a read-only mount (``EROFS`` /
            ``EACCES`` / ``EPERM``) — those are the only errno values the
            ``scratch_dir`` fallback handles.
    """
    target = _with_slash(str(target_data_path))

    current = read_current_data_path(lake_path)
    if _with_slash(current) == target:
        return lake_path

    if _is_remote(current) and not _is_remote(target) and not force:
        msg = (
            f"{lake_path}: refusing to rewrite remote data_path {current!r} to local {target!r} — "
            f"the stamp is the only record of where this bundle's data lives, and recovering it "
            f"costs a re-sync. Pass force=True (or `statcat bundle rebind --force`) if this is deliberate."
        )
        raise BundleDataPathError(msg)

    try:
        _write_data_path(lake_path, target)
    except (duckdb.IOException, OSError) as exc:
        if scratch_dir is None:
            raise
        # Read-only mount fallback: copy and rewrite the copy.
        if isinstance(exc, OSError) and exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
        scratch_dir.mkdir(parents=True, exist_ok=True)
        copy_path = scratch_dir / lake_path.name
        shutil.copyfile(lake_path, copy_path)
        _write_data_path(copy_path, target)
        return copy_path

    return lake_path


def _resolve_policy(bundle_dir: Path, policy: DataPolicy) -> tuple[str | None, bool]:
    """Reduce a `DataPolicy` to ``(data_path, persist)``.

    A ``data_path`` of `None` means "omit the ``DATA_PATH`` option from
    ATTACH entirely" — DuckLake then uses its own stamp and skips the
    comparison, which is the one form that cannot disagree with the lake.
    """
    if isinstance(policy, Embedded):
        return None, False
    if isinstance(policy, Colocated):
        return _colocated_target(bundle_dir), True
    if isinstance(policy, At):
        return _with_slash(str(policy.path)), policy.persist

    lake_path = bundle_dir / _LAKE_FILENAME
    if not lake_path.exists():
        # A bundle being created, not opened: there is no stamp to read
        # and co-location is the only thing "here" can mean.
        return _colocated_target(bundle_dir), False

    info = inspect_bundle(lake_path)
    if info.target is None and not info.will_write:
        if info.verdict.startswith("ambiguous"):
            msg = (
                f"{bundle_dir}: cannot determine where this bundle's parquet lives. "
                f"The lake is stamped {info.stamp!r}, which does not exist; there is no "
                f"{bundle_dir / 'parquet'}/ to fall back to; and {info.data_files} data file(s) "
                f"are referenced, so rewriting the stamp would lose the only pointer to them. "
                f"Pass data=At(...) with the real location, or run `statcat bundle rebind`."
            )
            raise BundleDataPathError(msg)
        return None, False
    return info.target, info.will_write


@contextmanager
def open_bundle(
    bundle_uri: str | Path,
    *,
    data: DataPolicy = _AUTO,
    access_mode: Literal["read_write", "read_only"] = "read_write",
    azure_credentials: dict[str, str] | None = None,
) -> Generator[Catalog]:
    """Open a statcatlib Catalog bundle, working out where its parquet lives.

    The default works without the caller knowing how the bundle is laid
    out — co-located, split, or moved. Pass ``data`` to override that
    judgement; see `Auto`, `Embedded`, `Colocated` and `At`.

    Opening is not re-stamping. `Auto` writes to the lake only when the
    bundle has demonstrably moved, so the steady state for every
    deployment is zero-write and lock-free — N read-only workers can open
    one bundle concurrently. Deliberately repointing a bundle at a new
    location is `statcat bundle rebind`, or ``At(..., persist=True)``.

    Args:
        bundle_uri: Folder containing ``meta.duckdb`` + ``lake.ducklake``.
        data: Where the observation parquet lives. Defaults to `Auto`, which reads the stamp and decides.
        access_mode: ``'read_write'`` (default) or ``'read_only'``. The read-only mode is what high-
            concurrency worker pools want — DuckDB takes a shared file lock and many processes can attach in
            parallel. A read-only open never writes the stamp, whatever the policy says.
        azure_credentials: Forwarded to `statcatlib.Catalog` when the effective data path is a remote URI
            (e.g. ``abfss://``). Deliberately separate from the bundle: the bundle declares *where*, the
            deployment supplies *how to authenticate*.

    Yields:
        The opened `statcatlib.Catalog`, closed when the context exits.

        `BundleDataPathError` propagates when `Auto` cannot determine
        where the parquet lives — see `Auto` for the case, and
        `inspect_bundle` for a non-raising report of the same judgement.
    """
    bundle_path = Path(bundle_uri)
    read_only = access_mode == "read_only"
    data_path, persist = _resolve_policy(bundle_path, data)

    # A read-only attach cannot UPDATE ducklake_metadata, so persistence is
    # dropped rather than refused — the open itself is still correct, since
    # the resolved path is passed to ATTACH with OVERRIDE_DATA_PATH, which
    # DuckLake honours under READ_ONLY without touching the stamp.
    if persist and not read_only and data_path is not None:
        lake_file = bundle_path / _LAKE_FILENAME
        if lake_file.exists():
            normalize_lake_data_path(lake_file, data_path)

    # Imported here to avoid a circular dependency at module load.
    from statcatlib.store import Catalog  # noqa: PLC0415

    catalog = Catalog(
        bundle_uri,
        # `Catalog(data_path=None)` means "co-located" — the sensible default
        # when creating a catalog directly. "Omit DATA_PATH and honour the
        # stamp" is a third state, spelled with the policy that means it.
        data_path=data_path if data_path is not None else Embedded(),
        azure_credentials=azure_credentials,
        read_only=read_only,
    )
    with catalog.open() as opened:
        yield opened


# -- the published bundle: statcatlib's part of sdmxlib's contract -------
#
# sdmxlib owns ``bundle.manifest.json`` (sdmxlib GH-56): the token caches key
# on, a sha256 per file, and the checker. A statcat bundle is an sdmxlib
# bundle plus the ``statcat`` schema and ``search/``, so statcatlib adds what
# only it can describe: a ``statcat`` section in the manifest, and checks
# that the catalog and its search index are what that section says (GH-3).

#: The manifest's extension section statcatlib owns.
STATCAT_SECTION = "statcat"

#: Version of the ``statcat`` section. Bumped when its fields change meaning.
#: 2 added ``search.annotations``, the annotations the index was built from.
STATCAT_SECTION_FORMAT = 2

_SEARCH_DIRNAME = "search"
_INDEX_MANIFEST = "index.json"


def _has_index(search_dir: Path) -> bool:
    """Whether ``search/`` holds anything. Opening a folder catalog creates it empty."""
    return search_dir.is_dir() and any(search_dir.iterdir())


def _search_section(cat: Catalog, search_dir: Path) -> dict[str, object] | None:
    """The ``search`` part of the section.

    Args:
        cat: The bundle's catalog, open.
        search_dir: Its ``search/`` directory.

    Returns:
        The index format version, embedding model and per-language layout,
        or ``None`` when no index is built.

    Raises:
        ValueError: ``search/`` holds an index from before per-language
            tables, which has no layout to record.
    """
    if not _has_index(search_dir):
        return None
    layout = cat.index_layout
    if not layout:
        msg = f"{search_dir} holds a search index with no {_INDEX_MANIFEST}: rebuild it with build_index() first"
        raise ValueError(msg)
    index_manifest = json.loads((search_dir / _INDEX_MANIFEST).read_text())
    return {
        "index_version": index_manifest.get("version"),
        "embedding_model": cat.embedding_model_name,
        "languages": {lang: {"rows": entry["rows"], "stemmer": entry["stemmer"]} for lang, entry in layout.items()},
        "annotations": cat.index_annotations,
    }


def catalog_section(path: str | Path) -> dict[str, object]:
    """Describe the catalog in a bundle, as its manifest's ``statcat`` section.

    Opens the bundle read-only. The section records the number of catalog
    datasets and, when a search index is built, its index format version,
    embedding model, and each language's row count and stemmer.

    Args:
        path: The bundle directory.

    Returns:
        The section, ready for ``write_manifest(extensions=...)``.
    """
    from statcatlib.store import Catalog  # noqa: PLC0415

    root = Path(path)
    with Catalog(root, read_only=True, data_path=Embedded()) as cat:
        row = cat.connection.execute("""SELECT count(*) FROM statcat.dataset""").fetchone()
        return {
            "format": STATCAT_SECTION_FORMAT,
            "statcatlib_version": metadata.version("statcatlib"),
            "datasets": row[0] if row else 0,
            "search": _search_section(cat, root / _SEARCH_DIRNAME),
        }


def publish_bundle(
    path: str | Path,
    *,
    bundle_id: str,
    producer: Producer,
    product: dict[str, object] | None = None,
    extensions: dict[str, dict[str, object]] | None = None,
    provenance: Provenance | None = None,
) -> Manifest:
    """Publish a statcat bundle: sdmxlib's manifest, with the ``statcat`` section.

    `sdmxlib.bundle.write_manifest` with `catalog_section` added to its
    extensions. Call it **last**, after `Catalog.build_index`, once nothing
    in the directory will change: the manifest lists every file with its
    sha256, so a file written afterwards fails `check_bundle`. Publishing
    again mints a new token, which is what tells caches to refresh.

    Args:
        path: The bundle directory.
        bundle_id: The producer's stable name for the bundle, kept across rebuilds.
        producer: What built it.
        product: Producer-specific fields, carried and not interpreted.
        extensions: Other packages' sections; ``statcat`` is filled in here.
        provenance: Where a fetched bundle came from.

    Returns:
        The manifest written.

    Raises:
        ValueError: *extensions* already has a ``statcat`` section, or the
            search index predates per-language tables and must be rebuilt.
    """
    if extensions and STATCAT_SECTION in extensions:
        msg = f"the {STATCAT_SECTION!r} section is written by publish_bundle, not passed to it"
        raise ValueError(msg)
    section = catalog_section(path)
    return write_manifest(
        path,
        bundle_id=bundle_id,
        producer=producer,
        product=product,
        extensions={**(extensions or {}), STATCAT_SECTION: section},
        provenance=provenance,
    )


def _check_catalog_rows(conn: duckdb.DuckDBPyConnection, section: Mapping[str, object]) -> Iterator[Finding]:
    """The catalog's datasets: counted as declared, each linked flow stored."""
    row = conn.execute("""SELECT count(*) FROM statcat.dataset""").fetchone()
    found = row[0] if row else 0
    if found != section.get("datasets"):
        detail = f"{found} datasets, the manifest says {section.get('datasets')}"
        yield Finding(STATCAT_SECTION, "statcat.dataset", detail)
    dangling = conn.execute(
        """
        SELECT d.identifier, d.sdmx_dataflow_urn
        FROM statcat.dataset d
        WHERE d.sdmx_dataflow_urn IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM sdmx.dataflow f WHERE f.urn = d.sdmx_dataflow_urn)
        ORDER BY d.identifier
        """
    ).fetchall()
    for identifier, urn in dangling:
        yield Finding(STATCAT_SECTION, identifier, f"links {urn}, which the registry does not hold")


def _check_index_annotations(conn: duckdb.DuckDBPyConnection, declared: object) -> Iterator[Finding]:
    """The store holds the annotations the index was built from: no overlay folded in since."""
    from statcatlib.store.store import annotation_fingerprint  # noqa: PLC0415

    subject = f"{_SEARCH_DIRNAME} annotations"
    if not isinstance(declared, Mapping) or not isinstance(types := declared.get("types"), list):
        yield Finding(
            STATCAT_SECTION, subject, "the index records no annotation fingerprint: rebuild with build_index()"
        )
        return
    if annotation_fingerprint(conn, [str(t) for t in types]) != declared.get("fingerprint"):
        detail = (
            f"annotations of type {', '.join(map(str, types))} differ from those the index was built from "
            "(an overlay folded in?): rebuild with build_index()"
        )
        yield Finding(STATCAT_SECTION, subject, detail)


def _check_search(root: Path, declared: object) -> Iterator[Finding]:
    """The search index: present exactly when declared, with the declared layout."""
    search_dir = root / _SEARCH_DIRNAME
    if declared is None:
        if _has_index(search_dir):
            yield Finding(STATCAT_SECTION, _SEARCH_DIRNAME, "a search index the manifest does not declare")
        return
    if not isinstance(declared, Mapping) or not isinstance(languages := declared.get("languages"), Mapping):
        yield Finding(STATCAT_SECTION, MANIFEST_NAME, "the search section has no languages")
        return
    if not (search_dir / _INDEX_MANIFEST).is_file():
        yield Finding(STATCAT_SECTION, f"{_SEARCH_DIRNAME}/{_INDEX_MANIFEST}", "the manifest declares a search index")
        return
    try:
        from statcatlib.store.lance import LanceIndex  # noqa: PLC0415
    except ImportError:
        yield Finding(STATCAT_SECTION, _SEARCH_DIRNAME, "cannot check the search index without statcatlib[search]")
        return
    index = LanceIndex(search_dir)
    layout = index.describe()
    for lang in sorted(set(languages) | set(layout)):
        yield from _check_language(index, lang, layout.get(lang), languages.get(lang))


def _check_language(index: LanceIndex, lang: str, found: LanguageLayout | None, want: object) -> Iterator[Finding]:
    """One language's table: declared, stemmed as declared, holding the declared rows."""
    subject = f"{_SEARCH_DIRNAME} [{lang}]"
    if found is None:
        yield Finding(STATCAT_SECTION, subject, "declared in the manifest, absent from the index")
        return
    if not isinstance(want, Mapping):
        yield Finding(STATCAT_SECTION, subject, "in the index, not declared in the manifest")
        return
    if found["stemmer"] != want.get("stemmer"):
        yield Finding(
            STATCAT_SECTION, subject, f"stemmed with {found['stemmer']}, the manifest says {want.get('stemmer')}"
        )
    table = index.table_for(lang)
    rows = table.count_rows() if table is not None else 0
    if rows != want.get("rows"):
        yield Finding(STATCAT_SECTION, subject, f"{rows} rows, the manifest says {want.get('rows')}")


def check_catalog(root: Path, manifest: Manifest, reg: LocalRegistry) -> list[Finding]:
    """The ``statcat`` check `check_bundle` adds to sdmxlib's.

    - the ``statcat`` schema is in ``meta.duckdb``, and the manifest has a
      ``statcat`` section in a format this build reads;
    - the catalog holds the datasets the section counts, and every dataset
      linked to a dataflow names one the registry holds;
    - ``search/`` is present exactly when the section declares an index,
      with the declared languages, each stemmed as declared and holding the
      declared rows;
    - the store's annotations of the types the index read are the ones it was
      built from, so an overlay folded in since shows up as a stale index.

    Args:
        root: The bundle directory.
        manifest: Its manifest.
        reg: sdmxlib's registry over the bundle.

    Returns:
        Everything found wrong with the catalog's part of the bundle.
    """
    conn = reg.connection
    has_schema = conn.execute(
        """SELECT count(*) FROM information_schema.tables WHERE table_schema = 'statcat' AND table_name = 'dataset'"""
    ).fetchone()
    if not has_schema or not has_schema[0]:
        return [Finding(STATCAT_SECTION, "meta.duckdb", "no statcat schema: not a statcat catalog")]
    section = manifest.extensions.get(STATCAT_SECTION)
    if section is None:
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, "no statcat section: publish with statcatlib's publish_bundle")]
    if section.get("format") != STATCAT_SECTION_FORMAT:
        detail = f"statcat section format {section.get('format')!r}, this build reads {STATCAT_SECTION_FORMAT}"
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, detail)]
    findings = [*_check_catalog_rows(conn, section), *_check_search(root, section.get("search"))]
    search = section.get("search")
    if isinstance(search, Mapping):
        findings += _check_index_annotations(conn, search.get("annotations"))
    return findings


def check_bundle(path: str | Path, *, references: bool = True) -> Report:
    """Check a statcat bundle: sdmxlib's checks, then the catalog's.

    sdmxlib's `sdmxlib.bundle.check_bundle` checks the manifest, the files
    against their checksums, the lake, the bindings and the structural
    closure; `check_catalog` runs after them as the ``statcat`` check. Run
    it where a bundle is built or received, never on the path that serves
    it. ``statcat bundle check`` runs it from a shell.

    Args:
        path: The bundle directory.
        references: Whether to check the structural closure, the costly check.

    Returns:
        What was checked and what was found.
    """
    return sdmx_check_bundle(path, references=references, extensions={STATCAT_SECTION: check_catalog})


def overlay_affects_search(overlay: Overlay, types: Collection[str] | None = None) -> tuple[OverlayEntry, ...]:
    """The entries of *overlay* that change what the search index should hold.

    An overlay applied at serve time never reaches statcatlib's index, and one
    folded into a bundle needs ``build_index()`` before search reflects it.
    Either way, an entry acting on an annotation type the index reads is a
    change search will not see on its own. Call this where overlays are
    loaded, to log or refuse them. Static: it needs no bundle.

    Args:
        overlay: The overlay document.
        types: The annotation types the index reads. ``None`` means
            `SEARCH_ANNOTATION_TYPES`, what `Catalog.build_index` reads by
            default; pass ``cat.index_annotations["types"]`` for an index
            built otherwise.

    Returns:
        Those entries, in document order; empty, and so falsy, when there are none.
    """
    from statcatlib.store.store import SEARCH_ANNOTATION_TYPES  # noqa: PLC0415

    wanted = set(SEARCH_ANNOTATION_TYPES if types is None else types)
    return tuple(entry for entry in overlay.entries if entry.type in wanted)

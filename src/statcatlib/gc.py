"""The GC job: what ``statcat gc`` deletes from a catalog in object storage, and the deletion (Phase 5 D10).

`collect_garbage` runs one GC run (spec §11.4). It reads, in this order:

1. the storage clock, once `RetentionPolicy.check` passes: the cutoff is
   ``now - gc_window``;
2. the Inventory: the head by the reader protocol, then each older root by
   ``seq`` until ``keep_roots`` roots are read and so is the newest root created
   before the cutoff. On an opted-in catalog the walk stops at
   ``HISTORY.expired_through``. Each distinct leaf is read once. The kept
   editions are every entry of every leaf read, and every tombstone in those
   roots retired at or after the cutoff;
3. the registrations under ``pending/``, listed after the Inventory, so a
   registration folded meanwhile still protects its files;
4. the manifest of each kept and each registered edition, on `GC_THREADS`
   threads. A kept edition's manifest must match the Inventory's SHA-256, and a
   registered edition's must decode and name its path. A failure fails the
   product closed: nothing under its ``data/`` is deleted, and the run logs
   ``gc.failed_closed``;
5. a listing of ``products/``.

It then plans these deletions, each a `Deletion` with its reason:

- ``retired``: every object of ``products/<p>/<e>/`` for an edition tombstoned
  before the cutoff and not kept, ``manifest.json`` first, so a partly deleted
  directory never reads as an edition; a later run finishes a partial one;
- ``unreferenced``: a ``products/<p>/data/`` file no kept or registered manifest
  lists, last modified before ``now - upload_grace``, in a product not failed
  closed;
- ``folded``: a registration of an edition the head records, in a leaf or in
  ``retired``, last modified before the cutoff;
- ``expired-registration``: a registration of an edition the Inventory does not
  record whose ``edition_id`` time is before ``now - tombstone_window`` (S20); it
  protects no file;
- ``discarded``: for each edition the operator names in *discard* and the
  Inventory does not record, its registration, then its manifest, then the rest
  of its directory (S22).

An edition directory that no Inventory version read records and no registration
names, older than the upload grace period, is logged ``gc.unregistered`` and kept.
A dry run plans and deletes nothing. With a forecast, a run deletes only the paths
in both plans and logs ``gc.forecast_deviation`` for each path in one plan only.

GC writes nothing, and reads only ``catalog.json``, ``inventory/HISTORY``,
``CURRENT`` and roots and leaves under ``inventory/``; it lists, reads and deletes
under ``products/`` and ``pending/`` only (spec §10.1, S24). Every event, the
deletion log included, is a log record on the ``statcatlib.gc`` logger carrying one
JSON object (`statcatlib.jobs._emit`), which ``statcat gc`` prints as one line on
standard output.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from operator import attrgetter
from typing import TYPE_CHECKING, Literal

from attrs import frozen
from sdmxlib.edition import MANIFEST_NAME, ManifestError, decode_manifest, edition_files
from sdmxlib.inventory import (
    CATALOG_NAME,
    HISTORY_NAME,
    FullInventory,
    InventoryError,
    decode_catalog,
    decode_history,
    decode_leaf,
    decode_root,
    root_path,
    sha256_hex,
)

from statcatlib.jobs import _emit, _stamp, decode_registration
from statcatlib.store.blob import BlobInventoryStorage, BlobStoreError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping
    from datetime import datetime

    from obstore import ObjectMeta
    from sdmxlib.inventory import Leaf, LeafRef, Root

    from statcatlib.jobs import RetentionPolicy
    from statcatlib.store.blob import BlobLocation
    from statcatlib.store.inventory import InventoryStorage

__all__ = ["GC_THREADS", "Deletion", "GcReport", "GcStoppedError", "Reason", "collect_garbage"]

log = logging.getLogger(__name__)

#: The threads GC reads leaves and manifests, and deletes, on.
GC_THREADS = 16

#: Why GC deletes an object: the deletion log's ``reason``.
type Reason = Literal["retired", "unreferenced", "folded", "expired-registration", "discarded"]

#: Which plan holds a path the other lacks: the forecast given, or this run's plan.
type Side = Literal["forecast", "plan"]

#: A product id and an edition id.
type _Key = tuple[str, str]

#: The prefixes GC lists and deletes under.
_DELETABLE = ("products/", "pending/")


class GcStoppedError(RuntimeError):
    """GC cannot tell what the Inventory keeps, and deleted nothing.

    A missing or unreadable ``catalog.json``, ``inventory/HISTORY``, head, root
    above ``expired_through`` or leaf, a root the reader skipped, or a root whose
    ``seq`` is not its name's raises it, naming the document.
    """


@frozen
class Deletion:
    """One planned deletion: a line of the deletion log.

    Attributes:
        path: The object, from the catalog root.
        reason: Why it goes.
        edition: ``<product>/<edition>`` for an edition's directory or
            registration, else ``None``.
    """

    path: str
    reason: Reason
    edition: str | None = None


@frozen
class GcReport:
    """What one GC run planned and did.

    Attributes:
        planned: Every deletion planned, in the order the run deletes them:
            registrations and ``data/`` files, then manifests, then the rest of
            each directory.
        deleted: The paths deleted; empty in a dry run.
        failed: Each path whose deletion failed, to the error. The next run
            tries it again.
        failed_closed: Each product whose ``data/`` was left alone, to the
            reason, which names the edition.
        unregistered: Each edition directory, ``<product>/<edition>``, that no
            Inventory version read records and no registration names, older
            than the upload grace period. GC keeps them.
        deviations: Each path in one of the forecast and this run's plan only,
            to the plan that holds it; empty without a forecast.
        dry_run: Whether the run deleted nothing by request.
    """

    planned: tuple[Deletion, ...]
    deleted: tuple[str, ...]
    failed: Mapping[str, str]
    failed_closed: Mapping[str, str]
    unregistered: tuple[str, ...]
    deviations: Mapping[str, Side]
    dry_run: bool


@frozen
class _Kept:
    """What the Inventory read keeps.

    Attributes:
        editions: Each kept edition, to the ``manifest_sha256`` its newest entry
            records, or ``None`` for a tombstone whose entry no leaf read holds.
        recorded: The editions the head records, in a leaf or in ``retired``.
        retired: The editions tombstoned before the cutoff and not kept.
    """

    editions: dict[_Key, str | None]
    recorded: frozenset[_Key]
    retired: frozenset[_Key]


@frozen
class _Pending:
    """What ``pending/`` holds.

    Attributes:
        registered: The editions the Inventory does not record whose
            registrations protect their manifests' files, each to its path.
        paths: Every registration's path, by edition.
        deletions: The folded and expired registrations to delete.
        expired: The editions of the expired ones, which protect no file.
    """

    registered: dict[_Key, str]
    paths: dict[_Key, str]
    deletions: list[Deletion]
    expired: frozenset[_Key]


@frozen
class _Directory:
    """An edition directory to delete: its registration, then its manifest, then the rest."""

    edition: str
    reason: Reason
    registration: str | None
    manifest: str | None
    rest: tuple[str, ...]


class _UnverifiedError(Exception):
    """A manifest GC cannot read or verify; the message names it."""


def _parse_discard(discard: Iterable[str]) -> frozenset[_Key]:
    """Each ``<product>/<edition>`` of *discard*; any other form raises `ValueError`."""
    found: set[_Key] = set()
    for item in discard:
        parts = item.split("/")
        if len(parts) != 2 or not all(parts) or {".", ".."} & set(parts):
            msg = f"--discard {item!r}: name an edition as <product>/<edition>"
            raise ValueError(msg)
        found.add((parts[0], parts[1]))
    return frozenset(found)


def _name(key: _Key) -> str:
    return f"{key[0]}/{key[1]}"


def _pooled[T, R](work: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """*work* over *items* on `GC_THREADS` threads, in the order of *items*."""
    with ThreadPoolExecutor(GC_THREADS, thread_name_prefix="statcatlib-gc") as pool:
        return list(pool.map(work, items))


class _Run:
    """One GC run's reads, plan and deletions."""

    def __init__(self, location: BlobLocation, policy: RetentionPolicy, storage: InventoryStorage) -> None:
        self.location = location
        self.policy = policy
        self.storage = storage
        self.now = storage.now()
        self.cutoff = self.now - policy.gc_window
        self.failed_closed: dict[str, str] = {}

    # -- events ---------------------------------------------------------------

    def alert(
        self,
        name: str,
        level: int,
        message: str,
        *,
        key: _Key | None = None,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Log the D14 event *name* with *message*."""
        severity = {logging.INFO: "info", logging.WARNING: "warning"}.get(level, "error")
        event: dict[str, object] = {
            "event": name,
            "severity": severity,
            "at": _stamp(self.now),
            "catalog": self.location.uri,
        }
        if key is not None:
            event |= {"product": key[0], "edition": key[1]}
        if reason is not None:
            event["reason"] = reason
        if detail is not None:
            event["detail"] = dict(detail)
        _emit(log, level, event, message)

    def line(self, name: str, deletion: Deletion, error: str | None = None) -> None:
        """Log one line of the deletion log."""
        event: dict[str, object] = {
            "event": name,
            "path": deletion.path,
            "reason": deletion.reason,
            "edition": deletion.edition,
        }
        level = logging.INFO
        if error is not None:
            event["error"] = error
            level = logging.ERROR
        _emit(log, level, event, f"{name} {deletion.path} ({deletion.reason})")

    def fail_closed(self, key: _Key, why: str) -> None:
        """Keep everything under the product's ``data/`` this run, and log ``gc.failed_closed`` naming the edition."""
        self.failed_closed.setdefault(key[0], why)
        self.alert(
            "gc.failed_closed",
            logging.WARNING,
            f"{_name(key)}: {why}; nothing under products/{key[0]}/data/ is deleted",
            key=key,
            reason=why,
        )

    # -- 2. the Inventory --------------------------------------------------------

    def _stop(self, what: str) -> GcStoppedError:
        return GcStoppedError(f"{self.storage.location}: {what}; GC cannot tell what it keeps, and deleted nothing")

    def expired_through(self) -> int:
        """``HISTORY.expired_through`` on an opted-in catalog, else 0."""
        raw = self.storage.read(CATALOG_NAME)
        if raw is None:
            msg = f"there is no {CATALOG_NAME}"
            raise self._stop(msg)
        try:
            catalog = decode_catalog(raw)
            if not catalog.opted_in:
                return 0
            history = self.storage.read(HISTORY_NAME)
            if history is None:
                msg = f"{CATALOG_NAME} is format {catalog.format_version} and there is no {HISTORY_NAME}"
                raise self._stop(msg)
            return decode_history(history).expired_through
        except InventoryError as exc:
            raise self._stop(str(exc)) from exc

    def older_root(self, seq: int, floor: int) -> Root:
        """Root *seq*, which the walk needs; a missing or invalid one stops the run."""
        raw = self.storage.read(root_path(seq))
        if raw is None:
            msg = f"{root_path(seq)} is missing, above expired_through ({floor})"
            raise self._stop(msg)
        try:
            root = decode_root(raw)
        except InventoryError as exc:
            msg = f"{root_path(seq)} does not validate: {exc}"
            raise self._stop(msg) from exc
        if root.seq != seq:
            msg = f"{root_path(seq)} says it is version {root.seq}"
            raise self._stop(msg)
        return root

    def leaf(self, ref: LeafRef) -> Leaf:
        """The leaf *ref* names, checked against its hash; a missing or bad one stops the run."""
        raw = self.storage.read(ref.leaf)
        if raw is None:
            msg = f"{ref.leaf} is missing"
            raise self._stop(msg)
        if sha256_hex(raw) != ref.leaf_sha256:
            msg = f"{ref.leaf} does not hash to its name"
            raise self._stop(msg)
        try:
            return decode_leaf(raw)
        except InventoryError as exc:
            msg = f"{ref.leaf}: {exc}"
            raise self._stop(msg) from exc

    def inventory(self) -> _Kept:
        """Read the head, then older roots back through the window and the newest ``keep_roots``, and their leaves."""
        floor = self.expired_through()
        full = FullInventory(self.storage.read)
        try:
            full.refresh()
            held = full.leaves()
        except InventoryError as exc:
            msg = f"the head cannot be read: {exc}"
            raise self._stop(msg) from exc
        if full.skipped:
            msg = f"the reader skipped {'; '.join(full.skipped)}"
            raise self._stop(msg)
        head = full.root
        roots = [head]
        seq = head.seq - 1
        while seq > floor and not (
            len(roots) >= self.policy.keep_roots and any(root.created_at < self.cutoff for root in roots)
        ):
            roots.append(self.older_root(seq, floor))
            seq -= 1
        leaves = {head.products[product_id].leaf_sha256: leaf for product_id, leaf in held.items()}
        wanted = {ref.leaf_sha256: ref for root in roots for ref in root.products.values()}
        missing = [ref for sha, ref in sorted(wanted.items()) if sha not in leaves]
        leaves |= {ref.leaf_sha256: leaf for ref, leaf in zip(missing, _pooled(self.leaf, missing), strict=True)}
        return self.kept(roots, leaves, held)

    def kept(self, roots: list[Root], leaves: Mapping[str, Leaf], held: Mapping[str, Leaf]) -> _Kept:
        """The kept editions of *roots*, newest first, whose leaves *leaves* holds by hash; *held* is the head's."""
        editions: dict[_Key, str | None] = {}
        stones: dict[_Key, datetime] = {}
        for root in roots:
            for product_id, ref in sorted(root.products.items()):
                for entry in leaves[ref.leaf_sha256].editions:
                    editions.setdefault((product_id, entry.edition_id), entry.manifest_sha256)
            for stone in root.retired:
                stones.setdefault((stone.product_id, stone.edition_id), stone.retired_at)
        for key, retired_at in stones.items():
            if retired_at >= self.cutoff:
                editions.setdefault(key, None)
        head = roots[0]
        recorded = {(product_id, entry.edition_id) for product_id, leaf in held.items() for entry in leaf.editions}
        recorded |= {(stone.product_id, stone.edition_id) for stone in head.retired}
        retired = frozenset(key for key, at in stones.items() if at < self.cutoff and key not in editions)
        return _Kept(editions=editions, recorded=frozenset(recorded), retired=retired)

    # -- 3. the registrations ----------------------------------------------------

    def registrations(self, kept: _Kept, discard: Collection[_Key]) -> _Pending:
        """List ``pending/`` and sort each registration."""
        registered: dict[_Key, str] = {}
        paths: dict[_Key, str] = {}
        deletions: list[Deletion] = []
        expired: set[_Key] = set()
        for meta in self.location.ops.list("pending"):
            path = meta["path"]
            parts = path.split("/")
            if len(parts) != 3 or not parts[2].endswith(".json"):
                continue
            key = (parts[1], parts[2].removesuffix(".json"))
            paths[key] = path
            if key in kept.recorded:
                if meta["last_modified"] < self.cutoff:
                    deletions.append(Deletion(path, "folded", _name(key)))
                continue
            if key in discard or key in kept.editions:
                continue
            try:
                got = self.location.ops.read(path)
                if got is None:
                    continue
                registration = decode_registration(path, got[0])
            except (BlobStoreError, ValueError) as exc:
                self.fail_closed(key, f"the registration {path} cannot be read: {exc}")
                continue
            if registration.edition_time < self.now - self.policy.tombstone_window:
                deletions.append(Deletion(path, "expired-registration", _name(key)))
                expired.add(key)
                continue
            registered[key] = path
        return _Pending(registered=registered, paths=paths, deletions=deletions, expired=frozenset(expired))

    # -- 4. the manifests --------------------------------------------------------

    def protected(self, item: tuple[_Key, str | None, bool]) -> frozenset[str] | str:
        """The ``data/`` paths a kept or registered edition's manifest lists, or why it cannot be read or verified."""
        (product_id, edition_id), sha, registered = item
        path = f"products/{product_id}/{edition_id}/{MANIFEST_NAME}"
        try:
            return self._listed(path, product_id, edition_id, sha, registered=registered)
        except _UnverifiedError as exc:
            return str(exc)

    def _listed(
        self, path: str, product_id: str, edition_id: str, sha: str | None, *, registered: bool
    ) -> frozenset[str]:
        if sha is None and not registered:
            msg = f"the Inventory keeps a tombstone for it, and no root read records its manifest's SHA-256 ({path})"
            raise _UnverifiedError(msg)
        try:
            got = self.location.ops.read(path)
        except BlobStoreError as exc:
            raise _UnverifiedError(str(exc)) from exc
        if got is None:
            msg = f"{path} is missing"
            raise _UnverifiedError(msg)
        raw = got[0]
        if sha is not None and sha256_hex(raw) != sha:
            msg = f"{path} has SHA-256 {sha256_hex(raw)}, and the Inventory records {sha}"
            raise _UnverifiedError(msg)
        try:
            manifest = decode_manifest(raw, f"{self.location.uri}{path}")
            files = edition_files(manifest)
        except ManifestError as exc:
            raise _UnverifiedError(str(exc)) from exc
        if (manifest.product_id, manifest.edition_id) != (product_id, edition_id):
            msg = f"{path} names {manifest.product_id}'s edition {manifest.edition_id}"
            raise _UnverifiedError(msg)
        return frozenset(f"products/{product_id}/{file}" for file in files if file.startswith("data/"))

    def manifests(self, kept: _Kept, registered: Mapping[_Key, str]) -> frozenset[str]:
        """Every ``data/`` path a kept or registered manifest lists; a failure fails its product closed."""
        items = [(key, sha, False) for key, sha in sorted(kept.editions.items())]
        items += [(key, None, True) for key in sorted(registered)]
        listed: set[str] = set()
        for (key, _, _), found in zip(items, _pooled(self.protected, items), strict=True):
            if isinstance(found, str):
                self.fail_closed(key, found)
            else:
                listed |= found
        return frozenset(listed)

    # -- 5. the listing and the plan -------------------------------------------

    def products(self, protected: Collection[str]) -> tuple[list[Deletion], dict[_Key, list[ObjectMeta]]]:
        """List ``products/``: the unreferenced ``data/`` files to delete, and every other object by edition."""
        oldest = self.now - self.policy.upload_grace
        deletions: list[Deletion] = []
        editions: dict[_Key, list[ObjectMeta]] = {}
        for meta in self.location.ops.list("products"):
            path = meta["path"]
            parts = path.split("/")
            if len(parts) < 4:
                continue
            if parts[2] != "data":
                editions.setdefault((parts[1], parts[2]), []).append(meta)
                continue
            if parts[1] in self.failed_closed or path in protected or meta["last_modified"] >= oldest:
                continue
            deletions.append(Deletion(path, "unreferenced"))
        return deletions, editions

    def directories(
        self, kept: _Kept, pending: _Pending, discard: Collection[_Key], editions: Mapping[_Key, list[ObjectMeta]]
    ) -> tuple[list[_Directory], list[str]]:
        """The edition directories to delete, and the unregistered ones to report."""
        planned: list[_Directory] = []
        unregistered: list[str] = []
        for key in sorted({*editions, *discard}):
            found = editions.get(key, [])
            named = key in pending.paths and key not in pending.expired
            if key in discard:
                planned.append(_directory(key, "discarded", pending.paths.get(key), found))
            elif key in kept.retired and key not in pending.registered:
                planned.append(_directory(key, "retired", None, found))
            elif key not in kept.editions and not named:
                newest = max(meta["last_modified"] for meta in found)
                if newest < self.now - self.policy.upload_grace:
                    unregistered.append(_name(key))
                    has_manifest = any(meta["path"].endswith(f"/{MANIFEST_NAME}") for meta in found)
                    self.alert(
                        "gc.unregistered",
                        logging.WARNING,
                        f"{_name(key)} was uploaded and is registered nowhere; rerun its upload, or discard it",
                        key=key,
                        detail={"manifest": has_manifest, "last_modified": _stamp(newest)},
                    )
        return planned, unregistered

    # -- 6. the deletion -------------------------------------------------------

    def delete(self, deletion: Deletion) -> str | None:
        """Delete one planned object; the error, or ``None``."""
        error = _delete(self.location, deletion.path)
        self.line("gc.deleted" if error is None else "gc.delete_failed", deletion, error)
        return error

    def run_phase(self, deletions: list[Deletion], failed: dict[str, str]) -> set[str]:
        """Delete *deletions* on the pool; the paths deleted. Each failure goes into *failed*."""
        done: set[str] = set()
        for deletion, error in zip(deletions, _pooled(self.delete, deletions), strict=True):
            if error is None:
                done.add(deletion.path)
            else:
                failed[deletion.path] = error
        return done


def _directory(key: _Key, reason: Reason, registration: str | None, found: list[ObjectMeta]) -> _Directory:
    manifest_path = f"products/{key[0]}/{key[1]}/{MANIFEST_NAME}"
    paths = sorted(meta["path"] for meta in found)
    manifest = manifest_path if manifest_path in paths else None
    rest = tuple(path for path in paths if path != manifest_path)
    return _Directory(_name(key), reason, registration, manifest, rest)


def _delete(location: BlobLocation, path: str) -> str | None:
    """Delete *path*, refusing any path outside ``products/`` and ``pending/`` with `ValueError`.

    Args:
        location: The catalog.
        path: The object, from the catalog root.

    Returns:
        The store's error, or ``None`` once the object is deleted.

    Raises:
        ValueError: *path* lies outside ``products/`` and ``pending/``, which GC
            never deletes under.
    """
    if not path.startswith(_DELETABLE):
        msg = f"GC deletes under products/ and pending/ only, and was asked to delete {path}"
        raise ValueError(msg)
    try:
        location.ops.delete(path)
    except BlobStoreError as exc:
        return str(exc)
    return None


def _everything(_path: str) -> bool:
    return True


def _deviations(planned: Iterable[Deletion], forecast: Collection[str] | None) -> dict[str, Side]:
    if forecast is None:
        return {}
    paths = {deletion.path for deletion in planned}
    found: dict[str, Side] = {}
    for path in sorted(set(forecast) - paths):
        found[path] = "forecast"
    for path in sorted(paths - set(forecast)):
        found[path] = "plan"
    return found


def collect_garbage(
    location: BlobLocation,
    policy: RetentionPolicy,
    *,
    storage: InventoryStorage | None = None,
    dry_run: bool = False,
    forecast: Iterable[str] | None = None,
    discard: Iterable[str] = (),
) -> GcReport:
    """Run GC over the catalog at *location*: read, plan and delete, as the module docstring states.

    Args:
        location: The catalog. ``statcat gc`` builds one over an
            `obstore.store.AzureStore`; a test passes any store.
        policy: ``[retention]`` of the jobs configuration.
        storage: Where the Inventory and the storage clock are read;
            `BlobInventoryStorage` over *location* by default. A test passes one
            with ``clock=``.
        dry_run: Plan, log the plan, and delete nothing.
        forecast: The paths of an earlier dry run's plan. The run deletes only
            the paths in both plans.
        discard: Editions, each ``<product>/<edition>``, that the Inventory does
            not record and an operator decided to delete (S22).

    Returns:
        What the run planned and did.

    Raises:
        ValueError: A *discard* item is no ``<product>/<edition>``, or names an
            edition the Inventory records, which ``statcat admin cancel``
            withdraws instead; nothing is deleted.

    A *policy* that fails `RetentionPolicy.check` raises `JobsConfigError`
    before anything is read. An Inventory that cannot be read back through the
    window raises `GcStoppedError`, and a failed listing of ``products/`` or
    ``pending/`` raises `BlobStoreError`; neither deletes anything. A storage
    clock that cannot be read, or that this machine's clock runs ahead of,
    raises `InventoryWriteRefusedError`. A failed deletion is logged and
    reported in `GcReport.failed`, and the run goes on.
    """
    policy.check()
    discards = _parse_discard(discard)
    expected = None if forecast is None else frozenset(forecast)
    run = _Run(location, policy, storage or BlobInventoryStorage(location))
    kept = run.inventory()
    refused = sorted(key for key in discards if key in kept.recorded or key in kept.editions)
    if refused:
        msg = (
            f"--discard {', '.join(_name(key) for key in refused)}: the Inventory records it; withdraw an "
            "embargoed edition with statcat admin cancel, and GC deletes a retired one after the window"
        )
        raise ValueError(msg)
    pending = run.registrations(kept, discards)
    protected = run.manifests(kept, pending.registered)
    unreferenced, editions = run.products(protected)
    directories, unregistered = run.directories(kept, pending, discards, editions)

    first = (
        pending.deletions
        + unreferenced
        + [Deletion(d.registration, d.reason, d.edition) for d in directories if d.registration]
    )
    manifests = [Deletion(d.manifest, d.reason, d.edition) for d in directories if d.manifest]
    rest = [Deletion(path, d.reason, d.edition) for d in directories for path in d.rest]
    by_path = attrgetter("path")
    planned = (*sorted(first, key=by_path), *sorted(manifests, key=by_path), *sorted(rest, key=by_path))
    for deletion in planned:
        run.line("gc.planned", deletion)
    deviations = _deviations(planned, expected)
    for path, side in deviations.items():
        held = "the forecast" if side == "forecast" else "this run's plan"
        run.alert(
            "gc.forecast_deviation",
            logging.WARNING,
            f"{path} is in {held} only; it is not deleted",
            detail={"path": path, "in": side},
        )

    failed: dict[str, str] = {}
    deleted: set[str] = set()
    if not dry_run:
        allowed: Callable[[str], bool] = _everything if expected is None else expected.__contains__
        deleted |= run.run_phase([d for d in sorted(first, key=by_path) if allowed(d.path)], failed)
        gated = [d for d in directories if d.registration is None or d.registration in deleted]
        deleted |= run.run_phase(
            [Deletion(d.manifest, d.reason, d.edition) for d in gated if d.manifest and allowed(d.manifest)], failed
        )
        emptied = [d for d in gated if d.manifest is None or d.manifest in deleted]
        deleted |= run.run_phase(
            [Deletion(path, d.reason, d.edition) for d in emptied for path in d.rest if allowed(path)], failed
        )
    return GcReport(
        planned=planned,
        deleted=tuple(d.path for d in planned if d.path in deleted),
        failed=failed,
        failed_closed=dict(sorted(run.failed_closed.items())),
        unregistered=tuple(unregistered),
        deviations=deviations,
        dry_run=dry_run,
    )

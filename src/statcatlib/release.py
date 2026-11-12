"""The release job: fold ``pending/``, release what is due, retire old entries (Phase 5 Part 3, ``statcat release``).

`ReleaseJob.run` is one run of the job over a catalog in object storage (spec
§11.2). The orchestrator starts a run at least twice for each release time: a
pre-flight run that folds, and a run just after the release time. A run is
short and idempotent, and the next run repairs whatever one left undone. It
performs these steps in order, each writing at most one Inventory version
through `InventoryWriter.commit`, so a lost race plans the step again on the
new head:

0. **Start.** Refuse a catalog that has opted in to expiry, and read the
   storage clock, which refuses a machine clock more than 30 s ahead.
2. **Advance.** Move ``CURRENT`` to the head, forward only.
3. **Access.** List who holds a role that reads the catalog's blobs and compare
   them with the allowlist (`statcatlib.access`). The result decides step 5.
4. **Fold.** Record each ``pending/`` registration that passes its checks as an
   ``embargoed`` entry, in one version of kind ``seal``.
5. **Release.** Write the refusal record. When the access check passed and the
   head's ``embargo`` is ``false``, release every due entry the rules accept, in
   one version of kind ``release``.
6. **Retire.** Tombstone the ``superseded`` and ``withdrawn`` entries older than
   ``retire_after``, in one version of kind ``retire``.

Step 1, the catalog lock, and step 7, expiry, belong to Phase 5 Part 6.

**The fold** reads every leaf, lists ``pending/`` and judges each registration
the head does not record, in the order of its path. The first check that fails
is the registration's refusal, named by its rule:

| Rule | The registration is refused when |
|---|---|
| ``pending-document`` | its bytes are not the document ``statcat upload`` writes |
| ``too-old`` | its ``edition_id`` time is older than ``tombstone_window`` |
| ``manifest`` | its manifest is absent, unreadable, names another edition, or lists an unsafe path |
| ``canonical-form`` | its manifest's ``canonical_form`` is not this sdmxlib's |
| ``files`` | a listed file is missing from the store, or differs in size or digest |
| ``lifecycle`` | `fold_lifecycle` refuses it |
| ``kind`` | its ``statcat`` section names no product kind, or another than the leaf's |
| ``chain`` | its ``previous`` is neither recorded nor tombstoned for the product |
| ``final`` | the product is ``final`` and its content fingerprint differs |
| ``references`` | it has no binding, and a reference resolves in no structure edition |
| ``pending-fork`` | another passing registration has its ``previous`` and schema |
| ``store`` | the store failed while it was checked |

sdmxlib's ``record`` adds ``duplicate``, ``tombstone`` and ``kind``, and the
cross-product rules add ``full-artefacts``, ``structure-home`` and
``publish-order``. A refusal never stops another registration's record.

**The refusal record.** The newest run's refusals live in the newest root's
``extensions.statcat.release`` (`refusal_record`), where the next run, an
operator and an alert read them. Step 5 writes it whenever it differs from the
head's, also when the release is held or skipped.

Every event is a log record on the ``statcatlib.release`` logger carrying one
JSON object (`statcatlib.jobs._emit`), which ``statcat release`` prints as one
line on standard output.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import attrs
from attrs import frozen
from sdmxlib.edition import (
    CANONICAL_FORM,
    MANIFEST_NAME,
    ManifestError,
    check_edition,
    decode_manifest,
    verify_remote,
)
from sdmxlib.inventory import (
    FullInventory,
    InventoryError,
    TransitionError,
    decode_root,
    head,
    record,
    release_due,
    root_path,
    routing_fields,
    sha256_hex,
    supersede_pending,
)

from statcatlib.access import AccessListingError, AccessResult, check_access
from statcatlib.bundle import PRODUCT_KINDS, ROOT_SECTION_FORMAT, STATCAT_SECTION
from statcatlib.jobs import JobsConfigError, _emit, _stamp, decode_registration, format_duration
from statcatlib.store import cross_product
from statcatlib.store.blob import (
    BlobInventoryStorage,
    BlobStoreError,
    _read_remote_manifest,
    _refuse_unsafe_files,
    _statcat_check,
    pull_edition,
)
from statcatlib.store.inventory import (
    Change,
    InventoryConflictError,
    InventoryContentionError,
    InventoryRuleError,
    InventoryWriter,
    InventoryWriteRefusedError,
    retire_change,
)
from statcatlib.store.store import IncompatibleCatalogError
from statcatlib.store.working_copy import fold_lifecycle, recorded_routing
from statcatlib.upload import _ALL_TIERS, _lookup

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Sequence

    from obstore import ObjectMeta
    from sdmxlib.edition import Lifecycle, Manifest
    from sdmxlib.inventory import Kind, Leaf, LeafEntry
    from sdmxlib.storage import JsonValue

    from statcatlib.access import ReaderListing
    from statcatlib.jobs import JobsConfig, Registration
    from statcatlib.store.blob import BlobLocation
    from statcatlib.store.inventory import InventoryStorage
    from statcatlib.upload import _DigestReader

__all__ = [
    "MAX_REFUSALS",
    "RELEASE_KEY",
    "Dropped",
    "ReleaseJob",
    "ReleaseRefusal",
    "ReleaseReport",
    "Step",
    "StructureEntry",
    "current_record",
    "current_refusals",
    "drop_until_stable",
    "refusal_record",
    "structure_entries",
]

log = logging.getLogger(__name__)

#: The key of the refusal record in the root's ``extensions.statcat``.
RELEASE_KEY = "release"

#: How many refusals the record keeps, the oldest ``since`` first; the events hold them all.
MAX_REFUSALS = 500

#: The step that refused: a ``ReleaseRefusal.step``.
type Step = Literal["fold", "release"]

#: A product id and an edition id.
type _Key = tuple[str, str]

#: The root's extension sections, as a root holds them.
type _Extensions = dict[str, dict[str, JsonValue]]

#: What steps 2 and 4 to 6 catch as their own failure; the run then goes on to the next step.
_STEP_ERRORS: tuple[type[Exception], ...] = (
    BlobStoreError,
    ManifestError,
    InventoryError,
    InventoryConflictError,
    InventoryContentionError,
    InventoryRuleError,
    IncompatibleCatalogError,
    OSError,
)


@frozen
class ReleaseRefusal:
    """A registration the fold did not record, or a due edition the release step did not release.

    Attributes:
        product_id: The product.
        edition_id: The edition.
        step: ``fold`` or ``release``.
        rule: The rule's name for the fold's and the cross-product rules'
            refusals; ``None`` for one of sdmxlib's ``release_due``.
        reason: The refusal's message.
        since: The storage time of the first run that refused the same
            product, edition and step, to the millisecond.
    """

    product_id: str
    edition_id: str
    step: Step
    rule: str | None
    reason: str
    since: datetime

    @property
    def key(self) -> tuple[str, str, Step]:
        """What ``since`` is carried by: the product, the edition and the step."""
        return self.product_id, self.edition_id, self.step


@frozen
class ReleaseReport:
    """What one run of the release job did, or in a dry run planned.

    Attributes:
        folded: Each registration recorded ``embargoed``, as
            ``<product>/<edition>``.
        released: Each edition made ``live``.
        retired: Each edition tombstoned.
        refused: Every refusal of the fold and the release step, by product,
            edition and step.
        skipped: Each step that did not run, to the reason: ``release`` under a
            failed access check or with the catalog's embargo on.
        failed: Each step that failed, to the error. The next run tries it
            again.
        versions: The ``seq`` of each Inventory version written, in order.
        access: The access precondition's result.
        dry_run: Whether the run wrote nothing by request.
    """

    folded: tuple[str, ...]
    released: tuple[str, ...]
    retired: tuple[str, ...]
    refused: tuple[ReleaseRefusal, ...]
    skipped: Mapping[str, str]
    failed: Mapping[str, str]
    versions: tuple[int, ...]
    access: AccessResult
    dry_run: bool


@frozen
class Dropped:
    """Why `drop_until_stable` dropped one product's change.

    Attributes:
        edition_id: The incoming edition the rule names.
        rule: ``manifest`` (the manifest is absent or not the one recorded,
            spec §6.5 item 2), ``store`` (the store failed while the manifest
            was read), or the cross-product rule.
        reason: The message.
    """

    edition_id: str
    rule: str
    reason: str


@frozen
class StructureEntry:
    """A structure edition a reference check reads: `structure_entries` lists them.

    Attributes:
        product_id: The structure product.
        edition_id: Its edition.
        manifest_sha256: The SHA-256 of the edition's manifest.
    """

    product_id: str
    edition_id: str
    manifest_sha256: str

    @property
    def key(self) -> _Key:
        """The product and the edition."""
        return self.product_id, self.edition_id


def _name(key: _Key) -> str:
    return f"{key[0]}/{key[1]}"


def _millis(moment: datetime) -> datetime:
    """*moment* in UTC, cut to the millisecond, as the record writes a time."""
    utc = moment.astimezone(UTC)
    return utc.replace(microsecond=utc.microsecond // 1000 * 1000)


def _time(value: object) -> datetime | None:
    """The zoned time the JSON string *value* holds, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed


# -- the refusal record (D5) ---------------------------------------------------


def current_record(full: FullInventory | None) -> _Extensions:
    """The extension sections of the head *full* holds, which `refusal_record`'s result is compared with."""
    if full is None:
        return {}
    return {name: dict(section) for name, section in full.root.extensions.items()}


def _release_section(full: FullInventory | None) -> Mapping[str, object]:
    """The head's ``extensions.statcat.release`` as a JSON object, empty when absent or malformed."""
    if full is None:
        return {}
    found = full.root.extensions.get(STATCAT_SECTION, {}).get(RELEASE_KEY)
    return {str(key): value for key, value in found.items()} if isinstance(found, dict) else {}


def _refusal(item: object) -> ReleaseRefusal | None:
    """The refusal a record's JSON *item* holds, or ``None`` for one this build cannot read."""
    if not isinstance(item, dict):
        return None
    held: dict[str, object] = {str(key): value for key, value in item.items()}
    product_id, edition_id, reason = held.get("product_id"), held.get("edition_id"), held.get("reason")
    step, rule, since = held.get("step"), held.get("rule"), _time(held.get("since"))
    if not isinstance(product_id, str) or not isinstance(edition_id, str) or not isinstance(reason, str):
        return None
    if since is None or not (rule is None or isinstance(rule, str)):
        return None
    if step == "fold":
        return ReleaseRefusal(product_id, edition_id, "fold", rule, reason, since)
    if step == "release":
        return ReleaseRefusal(product_id, edition_id, "release", rule, reason, since)
    return None


def current_refusals(full: FullInventory | None) -> tuple[ReleaseRefusal, ...]:
    """The refusals the head's record holds, in its order; an entry this build cannot read is left out."""
    listed = _release_section(full).get("refused")
    if not isinstance(listed, list):
        return ()
    return tuple(found for found in map(_refusal, listed) if found is not None)


def _epoch_of(full: FullInventory | None) -> tuple[int, datetime] | None:
    """The restore epoch the head's record names, with its first root's ``created_at``, or ``None``."""
    section = _release_section(full)
    epoch, started = section.get("restore_epoch"), _time(section.get("epoch_started_at"))
    if isinstance(epoch, bool) or not isinstance(epoch, int) or started is None:
        return None
    return epoch, started


def _dated(full: FullInventory | None, refusals: Iterable[ReleaseRefusal], now: datetime) -> list[ReleaseRefusal]:
    """*refusals* sorted, each with the ``since`` the head's record holds for it, or *now* for a new one."""
    held = {found.key: found.since for found in current_refusals(full)}
    at = _millis(now)
    dated = {one.key: attrs.evolve(one, since=held.get(one.key, at)) for one in refusals}
    return [dated[key] for key in sorted(dated)]


def refusal_record(
    full: FullInventory | None,
    refusals: Iterable[ReleaseRefusal],
    now: datetime,
    *,
    epoch: tuple[int, datetime | None] = (0, None),
) -> _Extensions:
    """The next root's extension sections, with the run's refusals in ``statcat.release`` (D5).

    The record is ``{"refused": [...], "omitted": n, "epoch_started_at": time or
    null, "restore_epoch": n}``. Each refusal holds ``product_id``,
    ``edition_id``, ``step``, ``rule``, ``reason`` and ``since``, sorted by the
    first three. A refusal the head's record holds for the same product,
    edition and step keeps its ``since``; a new one takes *now*. At most
    `MAX_REFUSALS` are kept, the oldest ``since`` first, so a standing refusal
    is never pushed out by new ones, and ``omitted`` counts the rest. Every
    other key of the section, and every other section, is the head's.

    Args:
        full: The head, or ``None`` before the first version.
        refusals: The run's refusals; their own ``since`` is ignored.
        now: The storage time of this run's step.
        epoch: The catalog's restore epoch, and the ``created_at`` of that
            epoch's first root when the fold found it (S25).

    Returns:
        The sections. A run with no refusal on a head with no record returns
        the head's sections unchanged, so `current_record` equals them and no
        version is written.
    """
    extensions = current_record(full)
    dated = _dated(full, refusals, now)
    by_age = sorted(dated, key=lambda one: (one.since, one.key))
    kept = sorted(by_age[:MAX_REFUSALS], key=lambda one: one.key)
    started = epoch[1]
    section = dict(extensions.get(STATCAT_SECTION, {}))
    if not kept and started is None and RELEASE_KEY not in section:
        return extensions
    refused: list[JsonValue] = [
        {
            "product_id": one.product_id,
            "edition_id": one.edition_id,
            "step": one.step,
            "rule": one.rule,
            "reason": one.reason,
            "since": _stamp(one.since),
        }
        for one in kept
    ]
    section["format_version"] = ROOT_SECTION_FORMAT
    section[RELEASE_KEY] = {
        "refused": refused,
        "omitted": len(dated) - len(kept),
        "epoch_started_at": None if started is None else _stamp(started),
        "restore_epoch": epoch[0],
    }
    extensions[STATCAT_SECTION] = section
    return extensions


# -- the rules over a planned version ------------------------------------------


def drop_until_stable(
    writer: InventoryWriter, full: FullInventory | None, leaves: Mapping[str, Leaf]
) -> tuple[dict[str, Leaf], dict[str, Dropped]]:
    """Drop each product of *leaves* the version's rules refuse, until a pass refuses none.

    Each pass reads every incoming entry's manifest through the writer's
    reader, which compares its SHA-256 with the entry's (spec §6.5 item 2),
    then runs the cross-product rules (`InventoryWriter.refusals`). A refused
    product's whole change is dropped, and the rules run again on the rest,
    since a dropped edition can be the one another was accepted with.

    Args:
        writer: The catalog's Inventory writer.
        full: The head the change is planned on.
        leaves: The planned change's leaves, by product.

    Returns:
        The leaves the rules accept, and each dropped product with why.

    A manifest of another product's entry that cannot be read raises
    `sdmxlib.edition.ManifestError`, and a store fault while reading one
    raises `BlobStoreError`; the step that planned the change reports it.
    """
    kept = dict(leaves)
    dropped: dict[str, Dropped] = {}
    while True:
        found: dict[str, Dropped] = {}
        for product_id, entry in cross_product.incoming(full, kept):
            if product_id in found:
                continue
            try:
                writer.artefacts(product_id, entry.edition_id, entry.manifest_sha256)
            except ManifestError as exc:
                found[product_id] = Dropped(entry.edition_id, "manifest", str(exc))
            except BlobStoreError as exc:
                found[product_id] = Dropped(entry.edition_id, "store", str(exc))
        if not found:
            for product_id, exc in writer.refusals(full, kept).items():
                found[product_id] = Dropped(exc.edition or "", exc.rule, str(exc))
        if not found:
            return kept, dropped
        for product_id in found:
            del kept[product_id]
        dropped |= found


def structure_entries(
    full: FullInventory | None, *, product_id: str, batch: Mapping[str, StructureEntry]
) -> list[StructureEntry]:
    """The structure editions an edition of *product_id* is checked with, as Inventory entries.

    It makes `statcatlib.store.working_copy.structure_editions`'s choice over
    entries in place of directories: the live and embargoed editions of every
    other structure product in *full*, with each product of *batch* holding
    its batch edition in place of its recorded ones.

    Args:
        full: The head, or ``None`` before the first version.
        product_id: The product whose edition is checked; its own editions
            are left out.
        batch: The fold's own structure editions, by product.

    Returns:
        The entries, possibly none.
    """
    found = [entry for other, entry in sorted(batch.items()) if other != product_id]
    if full is None:
        return found
    for other, leaf in sorted(full.leaves().items()):
        if other == product_id or other in batch or leaf.kind != "structure":
            continue
        found += [
            StructureEntry(other, entry.edition_id, entry.manifest_sha256)
            for entry in leaf.editions
            if entry.state in cross_product.ACTIVE
        ]
    return found


# -- the fold's reads -----------------------------------------------------------


@frozen
class _Read:
    """What the fold read of one registration, kept by path for the run.

    Attributes:
        key: The product and the edition, from the path.
        last_modified: When the store last wrote the registration.
        registration: The decoded registration, or ``None`` when refused
            before it was decoded.
        manifest: The edition's manifest, or ``None`` when refused before it
            was read.
        manifest_bytes: Its bytes.
        refusal: The rule and the reason of the first failed check that reads
            the store alone, or ``None``.
    """

    key: _Key
    last_modified: datetime
    registration: Registration | None = None
    manifest: Manifest | None = None
    manifest_bytes: bytes = b""
    refusal: tuple[str, str] | None = None


@frozen
class _Candidate:
    """A registration that passed every check of its own, ready to record."""

    key: _Key
    registration: Registration
    manifest: Manifest
    manifest_bytes: bytes
    last_modified: datetime
    kind: Kind
    lifecycle: Lifecycle

    @property
    def entry(self) -> StructureEntry:
        """The edition as a reference check is given it."""
        return StructureEntry(self.key[0], self.key[1], sha256_hex(self.manifest_bytes))


@frozen
class _Note:
    """An event the fold raises once its version is written: ``fold.release_at_past`` or ``fold.restored``."""

    name: str
    key: _Key
    message: str
    detail: Mapping[str, object]


@frozen
class _FoldPlan:
    """The fold planned on one head."""

    leaves: dict[str, Leaf]
    folded: tuple[_Key, ...]
    refused: tuple[ReleaseRefusal, ...]
    notes: tuple[_Note, ...]


@frozen
class _ReleasePlan:
    """The release step planned on one head."""

    change: Change | None
    released: tuple[_Key, ...]
    refused: tuple[ReleaseRefusal, ...]
    fold_refused: tuple[ReleaseRefusal, ...]
    overdue: tuple[tuple[_Key, datetime], ...]
    due: int
    held: bool


@frozen
class _Recorded:
    """The fold's planned leaves, and the registrations they record."""

    leaves: dict[str, Leaf]
    keys: frozenset[_Key]


class _RefusedError(Exception):
    """One registration fails a check; the fold records the rule and the reason."""

    def __init__(self, reason: str, *, rule: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


def _container(uri: str) -> str:
    """The container an ``az://<container>/...`` URI names, or the empty string for another form."""
    scheme, sep, rest = uri.partition("://")
    return rest.split("/", 1)[0] if sep and scheme == "az" else ""


def _account(location: BlobLocation) -> str:
    """The storage account *location*'s store names in its configuration, or the empty string."""
    found = getattr(location.store, "config", None)
    name = found.get("account_name") if isinstance(found, Mapping) else None
    return name if isinstance(name, str) else ""


class ReleaseJob:
    """One catalog's release job: `run` performs one run (Phase 5 D1).

    The command line builds every client and reads the environment (D15); this
    class takes each as an argument and reads none.
    """

    def __init__(
        self,
        location: BlobLocation,
        config: JobsConfig,
        *,
        rest: _DigestReader,
        listing: ReaderListing,
        storage: InventoryStorage | None = None,
    ) -> None:
        """Run over the catalog at *location*.

        Args:
            location: The catalog. ``statcat release`` builds one over an
                `obstore.store.AzureStore`; a test passes any store.
            config: The jobs configuration, with ``[access]``.
            rest: Reads the size and kept digest of a stored file:
                `statcatlib.store.blob.AzureBlobRest` in a deployment.
            listing: Lists who can read the catalog's blobs:
                `statcatlib.access.AzureReaderListing` in a deployment.
            storage: Where the Inventory is read and written, and the storage
                clock; `BlobInventoryStorage` over *location* by default. A
                test passes one with ``clock=`` and ``actor=``.

        Raises:
            JobsConfigError: *config* has no ``[access]``, which the release
                job requires, or its ``container`` or ``storage_account``
                names another container or account than *location*'s, so the
                access check would judge a container the catalog is not in.
        """
        if config.access is None:
            msg = "[access] is missing; the release job requires it for the access precondition"
            raise JobsConfigError(msg)
        named = _container(location.uri)
        if named and named != config.access.container:
            msg = (
                f"access.container is {config.access.container!r}, and the catalog {location.uri} is in the "
                f"container {named!r}; the access check must list the catalog's own container"
            )
            raise JobsConfigError(msg)
        account = _account(location)
        configured = config.access.storage_account.rstrip("/").rsplit("/", 1)[-1]
        if account and account.lower() != configured.lower():
            msg = (
                f"access.storage_account names the account {configured!r}, and the catalog {location.uri} is in "
                f"the account {account!r}; the access check must list the catalog's own account"
            )
            raise JobsConfigError(msg)
        self._location = location
        self._retention = config.retention
        self._access_policy = config.access
        self._rest = rest
        self._listing = listing
        self._storage: InventoryStorage = storage or BlobInventoryStorage(location)
        self._writer = InventoryWriter(self._storage)
        self._now = datetime.now(UTC)
        self._epoch = 0
        self._epoch_started: datetime | None = None
        self._epoch_walked = False
        self._pending_listed: list[ObjectMeta] | None = None
        self._reads: dict[str, _Read] = {}
        self._fold_bytes: dict[_Key, bytes] = {}
        self._manifests: dict[tuple[str, str, str], Manifest] = {}
        self._tmp: Path | None = None
        self._pulled: dict[_Key, Path] = {}
        self._checked: dict[tuple[_Key, frozenset[_Key]], str | None] = {}
        self._given: dict[_Key, frozenset[_Key]] = {}

    def _forget(self) -> None:
        """Drop what an earlier run of this job read and kept, so each run reads the store afresh."""
        self._epoch_started = None
        self._epoch_walked = False
        self._pending_listed = None
        for kept in (self._reads, self._fold_bytes, self._manifests, self._pulled, self._checked, self._given):
            kept.clear()

    # -- events ---------------------------------------------------------------

    def _alert(
        self,
        name: str,
        level: int,
        message: str,
        *,
        key: _Key | None = None,
        refusal: ReleaseRefusal | None = None,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Log the D14 event *name* with *message*, at the storage time of the step that raised it."""
        severity = {logging.INFO: "info", logging.WARNING: "warning"}.get(level, "error")
        event: dict[str, object] = {
            "event": name,
            "severity": severity,
            "at": _stamp(self._now),
            "catalog": self._location.uri,
        }
        if refusal is not None:
            event |= {
                "product": refusal.product_id,
                "edition": refusal.edition_id,
                "rule": refusal.rule,
                "reason": refusal.reason,
            }
        if key is not None:
            event |= {"product": key[0], "edition": key[1]}
        if reason is not None:
            event["reason"] = reason
        if detail is not None:
            event["detail"] = dict(detail)
        _emit(log, level, event, message)

    def _refused(self, key: _Key, step: Step, rule: str | None, reason: str) -> ReleaseRefusal:
        return ReleaseRefusal(key[0], key[1], step, rule, reason, _millis(self._now))

    # -- the run ----------------------------------------------------------------

    def run(self, *, dry_run: bool = False) -> ReleaseReport:
        """Perform one run: advance ``CURRENT``, check access, fold, release, retire.

        Args:
            dry_run: Run the access check and plan the fold, the release and
                the retirement against the head, move no ``CURRENT`` and
                write nothing. The release is planned on the head as it is,
                so an edition this run would fold is not planned for release.

        Returns:
            What the run did. A refusal, a held release and a skipped step are
            reported and leave `ReleaseReport.failed` empty; a step that
            failed is in it, and the run went on to the next step.

        Raises:
            InventoryWriteRefusedError: The catalog has opted in to expiry
                (``catalog.json`` 2.x), which needs the catalog lock of Phase 5
                Part 6, or the storage clock cannot be read, or this machine's
                clock is more than 30 s ahead of it. Nothing was written.

        A store with no ``catalog.json`` raises `FileNotFoundError`. A
        credential error stops the run, and a `BlobStoreError` or an
        `sdmxlib.inventory.InventoryError` from reading ``catalog.json`` does
        too.
        """
        document = self._writer.catalog_document()
        if document.opted_in:
            msg = (
                f"{self._storage.location} has opted in to expiry (catalog.json format {document.format_version}); "
                "its release job needs the catalog lock, which Phase 5 Part 6 adds. This run wrote nothing"
            )
            raise InventoryWriteRefusedError(msg)
        self._forget()
        self._epoch = document.restore_epoch
        self._now = self._storage.now()
        failed: dict[str, str] = {}
        skipped: dict[str, str] = {}
        versions: list[int] = []
        if not dry_run:
            self._step("advance", self._advance, failed)
        access = self._access()
        self._tmp = Path(tempfile.mkdtemp(prefix="statcat-release-"))
        try:
            fold = self._step("fold", lambda: self._fold(versions, dry_run=dry_run), failed)
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        released = self._step(
            "release", lambda: self._release(fold, access, versions, skipped, dry_run=dry_run), failed
        )
        retired = self._step("retire", lambda: self._retire(versions, dry_run=dry_run), failed)
        refused: list[ReleaseRefusal] = []
        if released is not None:
            refused = [*released.fold_refused, *released.refused]
        elif fold is not None:
            refused = [*fold.refused]
        return ReleaseReport(
            folded=tuple(_name(key) for key in fold.folded) if fold is not None else (),
            released=tuple(_name(key) for key in released.released) if released is not None else (),
            retired=tuple(retired or ()),
            refused=tuple(sorted(refused, key=lambda one: one.key)),
            skipped=skipped,
            failed=failed,
            versions=tuple(versions),
            access=access,
            dry_run=dry_run,
        )

    def _step[T](self, name: str, work: Callable[[], T], failed: dict[str, str]) -> T | None:
        """Run one step; a failure of its own work is logged ``step.failed``, kept in *failed*, and the run goes on."""
        try:
            return work()
        except _STEP_ERRORS as exc:
            error = f"{type(exc).__name__}: {exc}"
            failed[name] = error
            self._alert(
                "step.failed",
                logging.ERROR,
                f"step.failed {name}: {error}",
                detail={"step": name, "error": error},
            )
            return None

    # -- 2. advance -------------------------------------------------------------

    def _advance(self) -> None:
        """Move ``CURRENT`` to the head, and log ``current.behind`` when it moved."""
        seq = self._writer.advance_current()
        if seq is not None:
            self._alert(
                "current.behind",
                logging.WARNING,
                f"CURRENT was behind the newest root, and now names Inventory version {seq}",
                detail={"seq": seq, "advanced": True},
            )

    # -- 3. access --------------------------------------------------------------

    def _access(self) -> AccessResult:
        """Run the access precondition, and log ``access.mismatch`` or ``access.unchecked`` when it fails."""
        policy = self._access_policy
        try:
            report = self._listing.readers()
        except AccessListingError as exc:
            result = AccessResult(passed=False, errors=(str(exc),))
        else:
            result = check_access(report, [principal.object_id for principal in policy.allowlist], policy.sentinels)
        if result.outsiders:
            named = ", ".join(sorted({f"{g.principal_id} ({g.role} at {g.scope})" for g in result.outsiders}))
            reason = f"a blob-read role names a principal outside the allowlist: {named}"
            detail: dict[str, object] = {"outsiders": [attrs.asdict(grant) for grant in result.outsiders]}
            self._alert("access.mismatch", logging.ERROR, f"access.mismatch {reason}", reason=reason, detail=detail)
        if result.errors or result.missing_sentinels:
            causes = [*result.errors]
            if result.missing_sentinels:
                causes.append(f"the listing lacks the sentinel(s) {', '.join(result.missing_sentinels)}")
            reason = f"the role assignments could not be checked: {'; '.join(causes)}"
            detail = {"errors": list(result.errors), "missing_sentinels": list(result.missing_sentinels)}
            self._alert("access.unchecked", logging.ERROR, f"access.unchecked {reason}", reason=reason, detail=detail)
        return result

    # -- 4. the fold ------------------------------------------------------------

    def _fold(self, versions: list[int], *, dry_run: bool) -> _FoldPlan:
        """Record the passing registrations in one ``seal`` version, and log what the plan it landed on found."""
        self._now = now = self._storage.now()
        plans: list[_FoldPlan] = []

        def change(full: FullInventory | None) -> Change | None:
            plan = self._plan_fold(full, now)
            plans.append(plan)
            return Change(leaves=plan.leaves, now=now) if plan.leaves else None

        if dry_run:
            change(self._writer.full())
        else:
            root = self._writer.commit(change, kind="seal", reason="fold of pending/ registrations")
            if root is not None:
                versions.append(root.seq)
        plan = plans[-1]
        for refusal in plan.refused:
            self._alert(
                "fold.refused",
                logging.WARNING,
                f"fold.refused {refusal.product_id}/{refusal.edition_id} {refusal.rule}: {refusal.reason}",
                refusal=refusal,
            )
        for note in plan.notes:
            self._alert(note.name, logging.WARNING, note.message, key=note.key, detail=note.detail)
        return plan

    def _pending(self) -> list[ObjectMeta]:
        """The objects under ``pending/``, by path, listed once a run."""
        if self._pending_listed is None:
            self._pending_listed = sorted(self._location.ops.list("pending"), key=lambda meta: meta["path"])
        return self._pending_listed

    def _plan_fold(self, full: FullInventory | None, now: datetime) -> _FoldPlan:
        """Judge every registration the head *full* does not record, and plan the passing ones' records."""
        leaves = dict(full.leaves()) if full is not None else {}
        retired: dict[str, set[str]] = {}
        for stone in full.retired if full is not None else ():
            retired.setdefault(stone.product_id, set()).add(stone.edition_id)
        refused: dict[_Key, ReleaseRefusal] = {}
        candidates: list[_Candidate] = []
        for meta in self._pending():
            parts = meta["path"].split("/")
            if len(parts) != 3 or not parts[2].endswith(".json"):
                continue
            key = (parts[1], parts[2].removesuffix(".json"))
            leaf = leaves.get(key[0])
            recorded = leaf is not None and any(entry.edition_id == key[1] for entry in leaf.editions)
            if recorded or key[1] in retired.get(key[0], ()):
                continue
            try:
                candidates.append(self._judge(meta, key, leaf, retired.get(key[0], set()), now))
            except _RefusedError as exc:
                refused[key] = self._refused(key, "fold", exc.rule, exc.reason)
            except BlobStoreError as exc:
                refused[key] = self._refused(key, "fold", "store", str(exc))
        candidates = self._check_structures(full, candidates, refused)
        candidates = self._refuse_forks(candidates, refused)
        notes: dict[_Key, list[_Note]] = {}
        if candidates:
            self._find_epoch(full)
        planned = self._record_stable(full, leaves, retired, candidates, refused, notes, now)
        folded = tuple(sorted(planned.keys))
        kept_notes = tuple(note for key in folded for note in notes.get(key, ()))
        ordered = tuple(refused[key] for key in sorted(refused))
        return _FoldPlan(leaves=planned.leaves, folded=folded, refused=ordered, notes=kept_notes)

    def _read(self, meta: ObjectMeta, key: _Key, now: datetime) -> _Read:
        """The registration's document, manifest and files check, read once a run."""
        path = meta["path"]
        held = self._reads.get(path)
        if held is not None:
            return held
        modified = meta["last_modified"]
        try:
            found = self._read_checked(path, key, modified, now)
        except _RefusedError as exc:
            found = _Read(key, modified, refusal=(exc.rule, exc.reason))
        except BlobStoreError as exc:
            found = _Read(key, modified, refusal=("store", str(exc)))
        self._reads[path] = found
        return found

    def _read_checked(self, path: str, key: _Key, modified: datetime, now: datetime) -> _Read:
        """The checks that read the store alone, in the fold's order; the first that fails raises `_RefusedError`."""
        ops = self._location.ops
        got = ops.read(path)
        if got is None:
            msg = f"{path} was listed and is absent now"
            raise _RefusedError(msg, rule="pending-document")
        try:
            registration = decode_registration(path, got[0])
        except ValueError as exc:
            raise _RefusedError(str(exc), rule="pending-document") from exc
        window = self._retention.tombstone_window
        if registration.edition_time < now - window:
            msg = (
                f"{_name(key)} was sealed at {_stamp(registration.edition_time)}, before the tombstone window "
                f"({format_duration(window)}); a registration that old may be one a backup restored. Discard it "
                "with statcat gc --discard, and seal a new edition"
            )
            raise _RefusedError(msg, rule="too-old")
        manifest_path = f"products/{key[0]}/{key[1]}/{MANIFEST_NAME}"
        held = ops.read(manifest_path)
        if held is None:
            msg = f"{manifest_path} is missing; run the edition's upload again"
            raise _RefusedError(msg, rule="manifest")
        raw = held[0]
        try:
            manifest = decode_manifest(raw, f"{self._location.uri}{manifest_path}")
            if (manifest.product_id, manifest.edition_id) != key:
                msg = f"{manifest_path} names {manifest.product_id}'s edition {manifest.edition_id}"
                raise _RefusedError(msg, rule="manifest")
            _refuse_unsafe_files(manifest)
        except (ManifestError, IncompatibleCatalogError) as exc:
            raise _RefusedError(str(exc), rule="manifest") from exc
        if manifest.canonical_form != CANONICAL_FORM:
            msg = (
                f"{_name(key)} was fingerprinted in the canonical form {manifest.canonical_form!r}, and this "
                f"release job knows {CANONICAL_FORM!r}; seal it with the sdmxlib the release job runs"
            )
            raise _RefusedError(msg, rule="canonical-form")
        findings = verify_remote(manifest, _lookup(self._rest, manifest), tiers=_ALL_TIERS)
        if findings:
            listed = "; ".join(f"{finding.subject}: {finding.detail}" for finding in findings)
            msg = f"{_name(key)}: {len(findings)} listed file(s) differ from the store: {listed}"
            raise _RefusedError(msg, rule="files")
        self._fold_bytes[key] = raw
        return _Read(key, modified, registration=registration, manifest=manifest, manifest_bytes=raw)

    def _judge(
        self, meta: ObjectMeta, key: _Key, leaf: Leaf | None, retired: Collection[str], now: datetime
    ) -> _Candidate:
        """One registration's checks against its product's leaf; a failed one raises `_RefusedError`."""
        read = self._read(meta, key, now)
        if read.refusal is not None:
            raise _RefusedError(read.refusal[1], rule=read.refusal[0])
        registration, manifest = read.registration, read.manifest
        if registration is None or manifest is None:
            msg = f"{_name(key)} was read with no registration or manifest"
            raise _RefusedError(msg, rule="store")
        try:
            lifecycle = fold_lifecycle(leaf, manifest)
        except InventoryRuleError as exc:
            raise _RefusedError(str(exc), rule="lifecycle") from exc
        kind = self._kind(key, leaf, manifest)
        previous = manifest.previous
        recorded = {entry.edition_id for entry in leaf.editions} if leaf is not None else set()
        if previous is not None and previous not in recorded and previous not in retired:
            msg = (
                f"{_name(key)} is built on {previous}, which the Inventory neither records nor tombstones for "
                f"{key[0]} (specification §6.5 item 6); fold the edition it is built on first, or seal from the "
                "live edition"
            )
            raise _RefusedError(msg, rule="chain")
        if lifecycle == "final":
            self._refuse_changed_final(key, leaf, retired, manifest)
        return _Candidate(
            key=key,
            registration=registration,
            manifest=manifest,
            manifest_bytes=read.manifest_bytes,
            last_modified=read.last_modified,
            kind=kind,
            lifecycle=lifecycle,
        )

    @staticmethod
    def _kind(key: _Key, leaf: Leaf | None, manifest: Manifest) -> Kind:
        """The product kind the edition's ``statcat`` section names, refused when it names none or another."""
        named = (manifest.extensions.get(STATCAT_SECTION) or {}).get("kind")
        kind: Kind
        if named == "data":
            kind = "data"
        elif named == "structure":
            kind = "structure"
        else:
            msg = (
                f"{_name(key)}: the edition's statcat section names the kind {named!r}, and a product is one of "
                f"{', '.join(PRODUCT_KINDS)}; seal it with statcatlib"
            )
            raise _RefusedError(msg, rule="kind")
        if leaf is not None and leaf.kind != kind:
            msg = (
                f"{_name(key)}: the edition is a {kind} edition, and the Inventory records {key[0]} as a "
                f"{leaf.kind} product; a product keeps its kind"
            )
            raise _RefusedError(msg, rule="kind")
        return kind

    def _manifest(self, product_id: str, entry: LeafEntry) -> Manifest:
        """A recorded edition's manifest, checked against the SHA-256 its entry records; read once a run."""
        cache_key = (product_id, entry.edition_id, entry.manifest_sha256)
        held = self._manifests.get(cache_key)
        if held is None:
            _, held = _read_remote_manifest(self._location, product_id, entry.edition_id, entry.manifest_sha256)
            self._manifests[cache_key] = held
        return held

    def _refuse_changed_final(self, key: _Key, leaf: Leaf | None, retired: Collection[str], manifest: Manifest) -> None:
        """Specification §6.5 item 5: a ``final`` product's new edition holds the content of the one it follows.

        The edition is compared with the product's live edition at its
        schema, else its live edition at any schema, else its ``previous``
        when the leaf records it. A tombstoned ``previous`` with no live
        edition is refused, since its manifest may be gone. A product with
        nothing to compare passes.
        """
        if leaf is None:
            return
        previous = manifest.previous
        compared = head(leaf, manifest.schema_version)
        if compared is None:
            live = [entry for entry in leaf.editions if entry.state == "live"]
            compared = live[-1] if live else None
        if compared is None and previous is not None:
            compared = next((entry for entry in leaf.editions if entry.edition_id == previous), None)
        if compared is None:
            if previous is not None and previous in retired:
                msg = (
                    f"{_name(key)}: {key[0]} is a final product with no live edition, and the edition is built on "
                    f"{previous}, which is retired, so its content cannot be compared (specification §6.5 item 5)"
                )
                raise _RefusedError(msg, rule="final")
            return
        try:
            other = self._manifest(key[0], compared)
        except IncompatibleCatalogError as exc:
            msg = (
                f"{_name(key)}: {key[0]} is a final product, and the edition it is compared with cannot be read: {exc}"
            )
            raise _RefusedError(msg, rule="final") from exc
        if manifest.content_fingerprint is None or manifest.content_fingerprint != other.content_fingerprint:
            msg = (
                f"{_name(key)}: {key[0]} is a final product, and the edition's content fingerprint "
                f"{manifest.content_fingerprint} differs from {other.content_fingerprint}, which "
                f"{compared.edition_id} ({compared.state}) holds; a final product's editions hold equal content "
                "(specification §6.5 item 5). Publish changed content as a new product"
            )
            raise _RefusedError(msg, rule="final")

    # -- the reference check ----------------------------------------------------

    def _check_structures(
        self, full: FullInventory | None, candidates: list[_Candidate], refused: dict[_Key, ReleaseRefusal]
    ) -> list[_Candidate]:
        """The reference check of each candidate with no binding (spec §6.5 item 9), repeated until it refuses none.

        The fold's structure editions are checked together: each is given the
        others in place of their products' recorded editions. One with a
        finding is refused with ``references``, and the pass repeats for the
        rest, since a reference may have resolved only in a refused edition.
        A data edition with no binding is then checked once against the
        surviving ones.

        Args:
            full: The head.
            candidates: The registrations that passed their other checks.
            refused: Filled with each refused registration.

        Returns:
            The candidates that passed, in their order.
        """
        dropped: set[_Key] = set()
        structures = [one for one in candidates if one.kind == "structure" and not one.manifest.bindings]
        while True:
            alive = [one for one in structures if one.key not in dropped]
            batch = self._batch(alive)
            failed = {one.key: why for one in alive if (why := self._references(full, one, batch)) is not None}
            if not failed:
                break
            for key, why in failed.items():
                refused[key] = self._refused(key, "fold", "references", why)
            dropped |= failed.keys()
        batch = self._batch([one for one in structures if one.key not in dropped])
        for one in candidates:
            if one.kind == "structure" or one.manifest.bindings:
                continue
            why = self._references(full, one, batch)
            if why is not None:
                refused[one.key] = self._refused(one.key, "fold", "references", why)
                dropped.add(one.key)
        return [one for one in candidates if one.key not in dropped]

    @staticmethod
    def _batch(structures: Iterable[_Candidate]) -> dict[str, StructureEntry]:
        """Each product's newest structure edition among *structures*, by product."""
        batch: dict[str, StructureEntry] = {}
        for one in sorted(structures, key=lambda found: found.key):
            batch[one.key[0]] = one.entry
        return batch

    def _references(
        self, full: FullInventory | None, candidate: _Candidate, batch: Mapping[str, StructureEntry]
    ) -> str | None:
        """Why *candidate* fails its reference check given *batch*, or ``None``; each answer is kept for the run."""
        homes = structure_entries(full, product_id=candidate.key[0], batch=batch)
        given = frozenset(home.key for home in homes)
        cached = (candidate.key, given)
        if cached not in self._checked:
            try:
                self._checked[cached] = self._reference_check(candidate, homes, batch)
            except (IncompatibleCatalogError, BlobStoreError) as exc:
                self._checked[cached] = f"{_name(candidate.key)}: the reference check could not read an edition: {exc}"
        self._given[candidate.key] = frozenset(entry.key for entry in batch.values()) & given
        return self._checked[cached]

    def _reference_check(
        self, candidate: _Candidate, homes: Sequence[StructureEntry], batch: Mapping[str, StructureEntry]
    ) -> str | None:
        """Pull the edition and its structure homes, and run `check_edition` at ``receive``; the findings, if any."""
        product_id, edition_id = candidate.key
        manifest = candidate.manifest
        if any(entry.root == "data" for entry in manifest.files):
            return (
                f"{_name(candidate.key)} has no binding and lists data files, and the release job holds no "
                "StoreSecret to read them with; an edition with no binding holds no observations"
            )
        own = self._pull(candidate.entry, candidate.manifest_bytes)
        in_batch = {entry.key for entry in batch.values()}
        pulled = [self._pull(home, self._fold_bytes[home.key] if home.key in in_batch else None) for home in homes]
        report = check_edition(
            own,
            at="receive",
            remote=f"{self._location.uri.rstrip('/')}/products/{product_id}/{edition_id}/",
            lookup=_lookup(self._rest, manifest),
            structures=pulled or None,
            extensions={STATCAT_SECTION: _statcat_check()},
        )
        if report.ok:
            return None
        found = "; ".join(str(finding) for finding in report.findings)
        looked = ", ".join(_name(home.key) for home in homes) or "no structure edition"
        return f"{_name(candidate.key)} failed its receive check, given {looked}: {found}"

    def _pull(self, entry: StructureEntry, manifest_bytes: bytes | None) -> Path:
        """The edition's files in the run's temporary directory, pulled once.

        *manifest_bytes* is the manifest the fold read for one of its own
        editions; a recorded edition's is read and compared with its entry's
        SHA-256.
        """
        held = self._pulled.get(entry.key)
        if held is not None:
            return held
        if self._tmp is None:
            msg = "the fold's temporary directory is gone"
            raise OSError(msg)
        raw = manifest_bytes
        if raw is None:
            raw, _ = _read_remote_manifest(self._location, entry.product_id, entry.edition_id, entry.manifest_sha256)
        dest = self._tmp / "products" / entry.product_id / entry.edition_id
        self._pulled[entry.key] = pull_edition(self._location, entry.product_id, entry.edition_id, raw, dest)
        return dest

    # -- the fork rule and the record ---------------------------------------------

    def _refuse_forks(self, candidates: list[_Candidate], refused: dict[_Key, ReleaseRefusal]) -> list[_Candidate]:
        """Refuse each passing registration that shares its product, ``previous`` and schema with another (S28)."""
        groups: dict[tuple[str, str | None, str], list[_Candidate]] = {}
        for one in candidates:
            groups.setdefault((one.key[0], one.manifest.previous, one.manifest.schema_version), []).append(one)
        forked: set[_Key] = set()
        for (product_id, previous, schema), group in groups.items():
            if len(group) < 2:
                continue
            for one in group:
                others = ", ".join(other.key[1] for other in group if other is not one)
                reason = (
                    f"{_name(one.key)} and {others} are all built on {previous or 'no edition'} at schema {schema}, "
                    f"so the fold cannot tell which one {product_id} publishes; discard all but one with statcat "
                    "gc --discard, and the next run folds the one that is left"
                )
                refused[one.key] = self._refused(one.key, "fold", "pending-fork", reason)
                forked.add(one.key)
        return [one for one in candidates if one.key not in forked]

    def _record_stable(
        self,
        full: FullInventory | None,
        leaves: Mapping[str, Leaf],
        retired: Mapping[str, Collection[str]],
        candidates: list[_Candidate],
        refused: dict[_Key, ReleaseRefusal],
        notes: dict[_Key, list[_Note]],
        now: datetime,
    ) -> _Recorded:
        """Record the candidates on the head's leaves, dropping what the rules refuse until a pass drops none.

        A product the version's rules refuse loses its whole change, and so
        does a product with an edition whose reference check was given a fold
        edition that ends up unrecorded.
        """
        by_product: dict[str, list[_Candidate]] = {}
        for one in sorted(candidates, key=lambda found: found.key):
            by_product.setdefault(one.key[0], []).append(one)
        while True:
            planned: dict[str, Leaf] = {}
            recorded: set[_Key] = set()
            for product_id, group in by_product.items():
                leaf = leaves.get(product_id)
                for one in group:
                    if one.key in refused:
                        continue
                    try:
                        leaf = self._record(leaf, retired.get(product_id, ()), one, notes, now)
                    except _RefusedError as exc:
                        refused[one.key] = self._refused(one.key, "fold", exc.rule, exc.reason)
                    else:
                        recorded.add(one.key)
                        planned[product_id] = leaf
            kept, dropped = drop_until_stable(self._writer, full, planned)
            lost = self._refuse_dropped(by_product, dropped, refused)
            lost += self._refuse_dependents(by_product, recorded, refused)
            if not lost:
                return _Recorded(leaves=kept, keys=frozenset(recorded - refused.keys()))

    def _refuse_dropped(
        self,
        by_product: Mapping[str, list[_Candidate]],
        dropped: Mapping[str, Dropped],
        refused: dict[_Key, ReleaseRefusal],
    ) -> int:
        """Refuse every registration of each product `drop_until_stable` dropped; how many it refused."""
        count = 0
        for product_id, why in dropped.items():
            for one in by_product[product_id]:
                if one.key in refused:
                    continue
                reason = why.reason
                if one.key[1] != why.edition_id:
                    reason = f"{_name(one.key)} is recorded in one version with {why.edition_id}: {why.reason}"
                refused[one.key] = self._refused(one.key, "fold", why.rule, reason)
                count += 1
        return count

    def _refuse_dependents(
        self,
        by_product: Mapping[str, list[_Candidate]],
        recorded: Collection[_Key],
        refused: dict[_Key, ReleaseRefusal],
    ) -> int:
        """Refuse each product with an edition whose reference check was given a fold edition now refused.

        Each registration of such a product is refused with ``references``;
        the count of those is returned.
        """
        count = 0
        for key in sorted(recorded):
            gone = sorted(self._given.get(key, frozenset()) & refused.keys())
            if key in refused or not gone:
                continue
            for one in by_product[key[0]]:
                if one.key in refused:
                    continue
                reason = (
                    f"{_name(one.key)}: the reference check was given the fold's edition {_name(gone[0])}, "
                    f"which was not recorded ({refused[gone[0]].reason}); the next run checks it again"
                )
                refused[one.key] = self._refused(one.key, "fold", "references", reason)
                count += 1
        return count

    def _record(
        self,
        leaf: Leaf | None,
        retired: Collection[str],
        one: _Candidate,
        notes: dict[_Key, list[_Note]],
        now: datetime,
    ) -> Leaf:
        """The product's leaf with *one* recorded ``embargoed``, and the embargoed edition it corrects withdrawn."""
        product_id, edition_id = one.key
        manifest = one.manifest
        try:
            lifecycle = fold_lifecycle(leaf, manifest)
        except InventoryRuleError as exc:
            raise _RefusedError(str(exc), rule="lifecycle") from exc
        corrected = [
            entry.edition_id
            for entry in (leaf.editions if leaf is not None else ())
            if entry.state == "embargoed"
            and entry.previous == manifest.previous
            and entry.schema_version == manifest.schema_version
        ]
        dataflows, pull_bytes, artefacts = recorded_routing(routing_fields(manifest))
        try:
            nxt = record(
                leaf,
                product_id=product_id,
                kind=one.kind,
                lifecycle=lifecycle,
                edition_id=edition_id,
                previous=manifest.previous,
                manifest_sha256=sha256_hex(one.manifest_bytes),
                schema_version=manifest.schema_version,
                embargo=True,
                now=now,
                release_at=self._release_at(one, notes),
                retired=retired,
                artefacts=artefacts,
                dataflows=dataflows,
                pull_bytes=pull_bytes,
            )
            for old in corrected:
                nxt = supersede_pending(nxt, old, by=edition_id, now=now)
        except TransitionError as exc:
            raise _RefusedError(str(exc), rule=exc.rule) from exc
        return nxt

    def _release_at(self, one: _Candidate, notes: dict[_Key, list[_Note]]) -> datetime | None:
        """The ``release_at`` to record: the registration's, or ``None`` for one only an explicit release publishes.

        A registration whose ``release_at`` is earlier than its own
        last-modified time is held (Q5), and so is one last modified before
        the first root of the current restore epoch (S25).
        """
        wanted = one.registration.release_at
        found: list[_Note] = []
        if wanted is not None and wanted < one.last_modified:
            found.append(
                _Note(
                    "fold.release_at_past",
                    one.key,
                    f"{_name(one.key)} was registered at {_stamp(one.last_modified)} with release_at "
                    f"{_stamp(wanted)}, already past; it is recorded with no release time, and statcat admin "
                    "release publishes it",
                    {"release_at": _stamp(wanted), "last_modified": _stamp(one.last_modified)},
                )
            )
        if wanted is not None and self._epoch > 0 and self._predates_epoch(one.last_modified):
            started = self._epoch_started
            found.append(
                _Note(
                    "fold.restored",
                    one.key,
                    f"{_name(one.key)} was registered at {_stamp(one.last_modified)}, before restore epoch "
                    f"{self._epoch} began; it is recorded with no release time, and statcat admin release "
                    "publishes it",
                    {
                        "release_at": _stamp(wanted),
                        "last_modified": _stamp(one.last_modified),
                        "epoch_started_at": None if started is None else _stamp(started),
                    },
                )
            )
        notes[one.key] = found
        return None if found else wanted

    def _predates_epoch(self, modified: datetime) -> bool:
        """Whether *modified* is before the current restore epoch's first root; true while the epoch has no root."""
        return self._epoch_started is None or modified < self._epoch_started

    def _find_epoch(self, full: FullInventory | None) -> None:
        """Find the ``created_at`` of the current restore epoch's first root, once a run (S25).

        The head's refusal record holds it once a run has found it. Otherwise
        the roots are read back from the head while they carry the epoch.
        """
        if self._epoch_walked or self._epoch == 0:
            return
        self._epoch_walked = True
        held = _epoch_of(full)
        if held is not None and held[0] == self._epoch:
            self._epoch_started = held[1]
            return
        if full is None or full.root.restore_epoch != self._epoch:
            return
        first = full.root
        seq = first.seq - 1
        while seq >= 1:
            raw = self._storage.read(root_path(seq))
            if raw is None:
                break
            try:
                older = decode_root(raw)
            except InventoryError:
                seq -= 1
                continue
            if older.restore_epoch != self._epoch:
                break
            first = older
            seq -= 1
        self._epoch_started = first.created_at

    # -- 5. the release ---------------------------------------------------------

    def _release(
        self,
        fold: _FoldPlan | None,
        access: AccessResult,
        versions: list[int],
        skipped: dict[str, str],
        *,
        dry_run: bool,
    ) -> _ReleasePlan:
        """Write the refusal record, and release what is due when the access check passed and the embargo is off."""
        self._now = now = self._storage.now()
        plans: list[_ReleasePlan] = []

        def change(full: FullInventory | None) -> Change | None:
            plan = self._plan_release(full, fold, access, now)
            plans.append(plan)
            return plan.change

        if dry_run:
            change(self._writer.full())
        else:
            root = self._writer.commit(change, kind="release", reason=f"release_due at {now:%Y-%m-%dT%H:%M:%SZ}")
            if root is not None:
                versions.append(root.seq)
        plan = plans[-1]
        if not access.passed:
            skipped["release"] = "the access check failed, so no edition is released automatically"
        elif plan.held:
            skipped["release"] = "the catalog's embargo is on, which holds every automatic release"
            self._alert(
                "release.held",
                logging.WARNING,
                f"release.held the catalog's embargo is on; {plan.due} due edition(s) stay embargoed",
                reason="the catalog's embargo is on; statcat admin embargo off resumes automatic releases",
                detail={"due": plan.due},
            )
        for refusal in plan.refused:
            self._alert(
                "release.refused",
                logging.WARNING,
                f"release.refused {refusal.product_id}/{refusal.edition_id} {refusal.rule or '-'}: {refusal.reason}",
                refusal=refusal,
            )
        folded = set(fold.folded) if fold is not None else set()
        for key in plan.released:
            if key in folded:
                self._alert(
                    "release.unprepared",
                    logging.WARNING,
                    f"release.unprepared {_name(key)} was folded and released in one run, with no "
                    "pre-materialisation between them",
                    key=key,
                )
        for key, release_at in plan.overdue:
            self._alert(
                "release.overdue",
                logging.WARNING,
                f"release.overdue {_name(key)} is embargoed past its release_at {_stamp(release_at)}",
                key=key,
                detail={"release_at": _stamp(release_at)},
            )
        return plan

    def _plan_release(
        self, full: FullInventory | None, fold: _FoldPlan | None, access: AccessResult, now: datetime
    ) -> _ReleasePlan:
        """The release step on the head *full*: the record, and the due entries the rules accept."""
        carried: Iterable[ReleaseRefusal]
        if fold is not None:
            carried = fold.refused
        else:
            # A fold that failed judged nothing, so the record keeps the head's fold refusals and their `since`.
            carried = [one for one in current_refusals(full) if one.step == "fold"]
        epoch = self._record_epoch(full)
        leaves = dict(full.leaves()) if full is not None else {}
        held = full is not None and full.embargo
        changed: dict[str, Leaf] = {}
        refused: list[ReleaseRefusal] = []
        due = sum(len(_due(leaf, now)) for leaf in leaves.values())
        if full is not None and not held and access.passed:
            due_leaves, declined = release_due(leaves, now)
            changed, dropped = drop_until_stable(self._writer, full, due_leaves)
            for name, reason in sorted(declined.items()):
                product_id, _, edition_id = name.partition("/")
                refused.append(self._refused((product_id, edition_id), "release", None, reason))
            for product_id, why in sorted(dropped.items()):
                for entry in _entering_live(full, product_id, due_leaves[product_id]):
                    reason = why.reason
                    if entry.edition_id != why.edition_id:
                        reason = (
                            f"{product_id}/{entry.edition_id} is released in one version with {why.edition_id}: "
                            f"{why.reason}"
                        )
                    refused.append(self._refused((product_id, entry.edition_id), "release", why.rule, reason))
        dated = _dated(full, [*carried, *refused], now)
        extensions = refusal_record(full, dated, now, epoch=epoch)
        change: Change | None = None
        if changed or extensions != current_record(full):
            change = Change(leaves=changed, extensions=extensions, now=now)
        after = leaves | changed
        overdue = tuple(
            ((product_id, entry.edition_id), entry.release_at)
            for product_id, leaf in sorted(after.items())
            for entry in leaf.editions
            if entry.state == "embargoed" and entry.release_at is not None and entry.release_at <= now
        )
        released_keys = tuple(
            (product_id, entry.edition_id)
            for product_id, leaf in sorted(changed.items())
            for entry in _entering_live(full, product_id, leaf)
        )
        return _ReleasePlan(
            change=change,
            released=released_keys,
            refused=tuple(one for one in dated if one.step == "release"),
            fold_refused=tuple(one for one in dated if one.step == "fold") if fold is not None else (),
            overdue=overdue,
            due=due,
            held=held,
        )

    def _record_epoch(self, full: FullInventory | None) -> tuple[int, datetime | None]:
        """The restore epoch and its first root's time, as the record carries them."""
        if self._epoch == 0:
            return 0, None
        if self._epoch_started is not None:
            return self._epoch, self._epoch_started
        held = _epoch_of(full)
        return self._epoch, held[1] if held is not None and held[0] == self._epoch else None

    # -- 6. retirement ------------------------------------------------------------

    def _retire(self, versions: list[int], *, dry_run: bool) -> list[str]:
        """Tombstone the superseded and withdrawn entries older than ``retire_after``, in one ``retire`` version."""
        self._now = now = self._storage.now()
        window = self._retention.retire_after
        cutoff = now - window
        retired: list[str] = []

        def change(full: FullInventory | None) -> Change | None:
            planned = retire_change(full, cutoff, now=now, artefacts=self._writer.artefacts)
            held = set() if full is None else set(full.root.retired)
            stones = () if planned is None or planned.retired is None else planned.retired
            retired[:] = [f"{stone.product_id}/{stone.edition_id}" for stone in stones if stone not in held]
            return planned

        if dry_run:
            change(self._writer.full())
            return retired
        reason = f"retire entries superseded or withdrawn for more than {format_duration(window)}"
        root = self._writer.commit(change, kind="retire", reason=reason)
        if root is None:
            return []
        versions.append(root.seq)
        return retired


def _due(leaf: Leaf, now: datetime) -> list[str]:
    """The embargoed editions of *leaf* whose ``release_at`` has come at *now*."""
    return [
        entry.edition_id
        for entry in leaf.editions
        if entry.state == "embargoed" and entry.release_at is not None and entry.release_at <= now
    ]


def _entering_live(full: FullInventory | None, product_id: str, leaf: Leaf) -> list[LeafEntry]:
    """The entries of *leaf* that are ``live`` and were not in the head *full*."""
    return [
        entry
        for other, entry in cross_product.incoming(full, {product_id: leaf})
        if other == product_id and entry.state == "live"
    ]

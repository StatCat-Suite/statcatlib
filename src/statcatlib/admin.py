"""`CatalogAdmin`: operational changes to a catalog's Inventory (Phase 2a S3).

The public `Catalog` reads live editions and records a seal. Every other
change to the Inventory goes through `CatalogAdmin`, which is constructed
explicitly with `CatalogAdmin.open` and is never returned by an attribute of
`Catalog`, `Product` or `EditionView`:

| Method | sdmxlib rule | ``event.kind`` |
|---|---|---|
| `CatalogAdmin.rollback` | `sdmxlib.inventory.rollback` | ``rollback`` |
| `CatalogAdmin.release` | `sdmxlib.inventory.release`, explicit | ``release`` |
| `CatalogAdmin.cancel` | `sdmxlib.inventory.cancel` | ``withdraw`` |
| `CatalogAdmin.set_embargo` | the root's ``embargo`` | ``embargo-change`` |
| `CatalogAdmin.describe` | the root's ``extensions.statcat`` | ``describe`` |
| `CatalogAdmin.prune` | `sdmxlib.inventory.retire` | ``retire`` |

Each of them requires a ``reason`` and writes one Inventory version through
`InventoryWriter.commit`, which re-applies the rule to the new head after a
lost race. `CatalogAdmin.editions` only reads, and
`CatalogAdmin.advance_current` moves only ``CURRENT``.

`CatalogAdmin.open` takes a catalog directory or a `BlobLocation`. Each change
reads the storage's clock once, through `InventoryStorage.now`, and gives that
time to the rule and to the version (`Change.now`). On local disk that is this
machine's clock (design decision P1), so a laptop with a skewed clock records
skewed times; on object storage it is the storage clock (Phase 4 D4). On a
server, an operator runs it under the release job's identity, so the version's
``event.actor`` is the job's and ``event.reason`` names the operator (spec
§10.1).

`CatalogAdmin.prune` is the narrow laptop prune of design decision P7 (Phase
2a S4), on local disk only; on object storage the release job retires and
``statcat gc`` deletes. It also deletes files: the directories of editions
tombstoned longer than its window, and ``data/`` files no manifest on disk
lists. It never
touches a product with a working copy or a seal journal entry, and deletes no
Inventory root or leaf. It keeps a structure edition that a live or embargoed
data edition's ``partial_of`` cites (specification §11.4), so a stale data
edition's partials still verify.

`CatalogAdmin.release` and `CatalogAdmin.rollback` make an edition live, so
the Inventory writer runs the cross-product rules on their version
(`statcatlib.store.cross_product`): a refusal raises `CrossProductRuleError`
and writes nothing.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Self

from attrs import frozen
from sdmxlib.edition import ManifestError, edition_files, list_editions, read_manifest
from sdmxlib.inventory import CATALOG_NAME, TransitionError, cancel, head, release, rollback
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.bundle import ROOT_SECTION_FORMAT, STATCAT_SECTION, description_to_json
from statcatlib.catalog import _refuse_legacy_layout
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import (
    Change,
    InventoryRuleError,
    InventoryWriter,
    LocalInventoryStorage,
    NonFastForwardError,
    retire_change,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from sdmxlib.inventory import EventKind, FullInventory, Leaf, LeafEntry, State, Tombstone

    from statcatlib.model.catalog import CatalogDescription

__all__ = ["CatalogAdmin", "EditionEntry", "PruneReport"]

log = logging.getLogger(__name__)


@frozen
class EditionEntry:
    """One edition as the newest Inventory version records it, in any state.

    Attributes:
        edition_id: The edition's id, a UUIDv7.
        product_id: Its product.
        previous: The edition it was built on, or ``None``.
        state: ``embargoed``, ``live``, ``superseded`` or ``withdrawn``.
        manifest_sha256: The SHA-256 of its ``manifest.json``.
        schema_version: The sdmxlib store schema it was written at.
        state_changed_at: When it entered *state*.
        release_at: For an embargoed edition, when it may be released.
    """

    edition_id: str
    product_id: str
    previous: str | None
    state: State
    manifest_sha256: str
    schema_version: str
    state_changed_at: datetime
    release_at: datetime | None


@frozen
class PruneReport:
    """What `CatalogAdmin.prune` retired and deleted, or would have in a dry run.

    Every path is ``/``-separated, from the catalog directory.

    Attributes:
        retired: Each edition retired, as ``<product>/<edition_id>``.
        deleted_dirs: Each tombstoned edition's directory deleted, as
            ``products/<product>/<edition_id>``.
        deleted_files: Each ``data/`` file deleted, as
            ``products/<product>/data/...``.
        skipped_products: Each product left untouched, with the reason: a
            working copy, a seal journal entry, or a manifest that cannot be
            read.
        failed: Each path whose deletion failed, with the error. The next
            run tries it again.
        root: The catalog directory.
        dry_run: Whether this is a dry run, which retired and deleted nothing.
        seq: The Inventory version the ``retire`` commit wrote, or ``None``
            when nothing was retired or the run was dry.
    """

    retired: tuple[str, ...]
    deleted_dirs: tuple[str, ...]
    deleted_files: tuple[str, ...]
    skipped_products: dict[str, str]
    failed: dict[str, str]
    root: Path
    dry_run: bool
    seq: int | None


def _busy(product_dir: Path, tombstoned: Collection[str]) -> str | None:
    """Why prune must leave the product alone, or ``None``.

    A product is busy while it has an unsealed edition (a working copy) or a
    seal journal entry, or when an edition's manifest cannot be read. An
    edition directory without a manifest whose id is tombstoned is a
    directory an earlier prune began to delete, never a working copy.

    Args:
        product_dir: The product's directory.
        tombstoned: The product's tombstoned edition ids.

    Returns:
        The reason, or ``None``.
    """
    try:
        found = list_editions(product_dir)
    except ManifestError as exc:
        return f"an edition's manifest cannot be read: {exc}"
    for info in found:
        if not info.sealed and info.edition_id not in tombstoned:
            return f"working copy {info.edition_id} is open; seal or discard it first"
    journal = product_dir / "journal"
    if journal.is_dir() and any(journal.glob("*.json")):
        return "it has a seal journal entry; its next working_copy() settles it"
    return None


def _new_tombstones(full: FullInventory | None, change: Change | None) -> list[str]:
    """The tombstones *change* adds to *full*'s, as ``<product>/<edition_id>``."""
    if change is None or change.retired is None:
        return []
    held = set() if full is None else set(full.root.retired)
    return [f"{t.product_id}/{t.edition_id}" for t in change.retired if t not in held]


def _edition_entry(product_id: str, entry: LeafEntry) -> EditionEntry:
    """The `EditionEntry` of *product_id*'s Inventory *entry*; `EmbargoReader.editions` shares it."""
    return EditionEntry(
        edition_id=entry.edition_id,
        product_id=product_id,
        previous=entry.previous,
        state=entry.state,
        manifest_sha256=entry.manifest_sha256,
        schema_version=entry.schema_version,
        state_changed_at=entry.state_changed_at,
        release_at=entry.release_at,
    )


def _require_reason(reason: str) -> str:
    if not reason.strip():
        msg = "an operational change needs a reason; the Inventory version records it as event.reason"
        raise ValueError(msg)
    return reason


def _leaf(full: FullInventory | None, product_id: str) -> Leaf:
    """The product's leaf in *full*, or `InventoryRuleError` (``rule="unknown"``) when it records none."""
    if full is None or product_id not in full.products():
        msg = f"the catalog records no product {product_id}"
        raise InventoryRuleError(msg, rule="unknown", product=product_id, edition=None)
    return full.leaf(product_id)


class CatalogAdmin:
    """Operational changes to a catalog's Inventory: rollback, release, cancel, embargo, description and prune.

    Open one with `open`. Every method but `editions`, `products`,
    `embargo` and `prune` writes one Inventory version and returns its
    number; `prune` writes at most one and returns a `PruneReport`.
    """

    _writer: InventoryWriter

    def __init__(self, *args: object, _writer: InventoryWriter | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError naming `open`."""
        if _writer is None or args or kwargs:
            msg = "open the operational object with CatalogAdmin.open(path)"
            raise TypeError(msg)
        self._writer = _writer

    @classmethod
    def open(
        cls,
        path: str | Path | BlobLocation,
        *,
        actor: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Self:
        """Open the catalog at *path* for operational changes.

        It reads ``catalog.json`` and writes nothing until a method is called.

        Args:
            path: The catalog directory, or a catalog in object storage.
            actor: On object storage, recorded as ``event.actor`` when the
                store's credential provider is no `TokenIdentity` or its token
                has no ``oid`` (`BlobInventoryStorage`).
            clock: On object storage, the time source in place of the storage
                clock; tests pass one.

        Returns:
            The operational object.

        Raises:
            FileNotFoundError: *path* is not a catalog.
            TypeError: *actor* or *clock* is given with a catalog directory,
                whose actor comes from the process and whose clock is this
                machine's.

        A legacy layout or other files at *path* raise `IncompatibleCatalogError`.
        """
        if isinstance(path, BlobLocation):
            writer = InventoryWriter(BlobInventoryStorage(path, actor=actor, clock=clock))
            writer.catalog_document()
            return cls(_writer=writer)
        if actor is not None or clock is not None:
            msg = (
                "actor= and clock= apply to a catalog in object storage; a catalog directory records "
                "<os user>@<hostname> and this machine's clock"
            )
            raise TypeError(msg)
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        if not (root / CATALOG_NAME).is_file():
            msg = f"{root} is not a catalog (no {CATALOG_NAME}); create one with Catalog.create({str(path)!r})"
            raise FileNotFoundError(msg)
        writer = InventoryWriter(root)
        writer.catalog_document()
        return cls(_writer=writer)

    def __repr__(self) -> str:
        return f"<CatalogAdmin {self._writer.storage.location}>"

    def advance_current(self) -> int | None:
        """Move ``CURRENT`` forward to the newest Inventory version (`InventoryWriter.advance_current`).

        It writes ``CURRENT`` only, never a root or a leaf, and never moves it
        back. A reader probes forward from a lagging ``CURRENT`` (spec §6.6),
        so this saves readers the probe; ``statcat advance-current`` runs it.

        Returns:
            The version ``CURRENT`` now names, or ``None`` when it named the
            newest version already, or none has been written.
        """
        return self._writer.advance_current()

    # -- reading ---------------------------------------------------------

    @property
    def embargo(self) -> bool:
        """The newest Inventory version's embargo setting; ``False`` before the first version."""
        full = self._writer.full()
        return False if full is None else full.embargo

    def products(self) -> list[str]:
        """Every product the newest Inventory version records, in any state, sorted."""
        full = self._writer.full()
        return [] if full is None else full.products()

    def editions(self, product_id: str) -> list[EditionEntry]:
        """The product's editions in every state, as the newest Inventory version records them.

        Retired editions are left out: the Inventory keeps only their
        tombstones.

        Args:
            product_id: The product.

        Returns:
            The editions in id order, which is the order they were started
            in; empty for a product the Inventory does not record.
        """
        full = self._writer.full()
        if full is None or product_id not in full.products():
            return []
        return [_edition_entry(product_id, e) for e in full.entries(product_id)]

    # -- product changes -------------------------------------------------

    def _commit_product(
        self,
        product_id: str,
        edition_id: str | None,
        step: Callable[[Leaf, datetime], Leaf],
        *,
        kind: EventKind,
        reason: str,
    ) -> int:
        """Apply *step* to the product's leaf in one Inventory version, re-applied after each lost race.

        Args:
            product_id: The product.
            edition_id: The edition concerned, named in a refusal.
            step: sdmxlib's rule, given the leaf and the storage's clock,
                read once per head; the version records the same time.
            kind: The version's ``event.kind``.
            reason: The version's ``event.reason``.

        Returns:
            The Inventory version written.

        A product the head does not record raises `InventoryRuleError`
        (``rule="unknown"``), and so does a change sdmxlib's rule refuses.
        """
        _require_reason(reason)

        def change(full: FullInventory | None) -> Change:
            leaf = _leaf(full, product_id)
            now = self._writer.storage.now()
            try:
                nxt = step(leaf, now)
            except TransitionError as exc:
                raise InventoryRuleError.wrapping(exc, product=product_id, edition=edition_id) from exc
            return Change(leaves={product_id: nxt}, now=now)

        return self._writer.commit(change, kind=kind, reason=reason).seq

    def rollback(self, product_id: str, *, to: str | None = None, if_live: str | None = None, reason: str) -> int:
        """Withdraw the product's live edition and make a superseded one live again.

        The rollback is conditional: it is refused when the live edition is
        no longer *if_live* when the version is written, such as after a
        competing release. A working copy built on the withdrawn edition then
        fails its seal with `NonFastForwardError`.

        Args:
            product_id: The product.
            to: The superseded edition to make live; the most recent
                superseded one when ``None``.
            if_live: The edition expected live; the live edition when the call
                starts when ``None``.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``rollback``).

        Raises:
            InventoryRuleError: The product is not recorded
                (``rule="unknown"``), has no live edition or a live edition
                other than *if_live* (``rule="if-live"``), or *to* is not a
                superseded edition (``rule="state"`` or ``"unknown"``).

        An edition made live that breaks a cross-product rule raises
        `CrossProductRuleError` and writes nothing: a data edition cut from a
        structure edition that is neither live nor embargoed
        (``rule="full-artefacts"``), or a structure edition holding a full
        artefact another structure product holds live
        (``rule="structure-home"``).

        An empty *reason* raises `ValueError`. It acts on the live edition at
        this sdmxlib's store schema, and does not roll back another schema's
        live edition.
        """
        _require_reason(reason)
        if if_live is None:
            live = head(_leaf(self._writer.full(), product_id), SCHEMA_VERSION)
            if live is None:
                msg = f"{product_id} has no live edition at store schema {SCHEMA_VERSION} to roll back"
                raise InventoryRuleError(msg, rule="if-live", product=product_id, edition=None)
            if_live = live.edition_id
        expected = if_live
        return self._commit_product(
            product_id,
            expected,
            lambda leaf, now: rollback(leaf, schema_version=SCHEMA_VERSION, if_live=expected, to=to, now=now),
            kind="rollback",
            reason=reason,
        )

    def release(self, product_id: str, edition_id: str, *, reason: str) -> int:
        """Make an embargoed edition live now, superseding the live edition it was built on.

        The release time is the storage's clock: this machine's on local disk
        (design decision P1), the storage clock on object storage.

        Args:
            product_id: The product.
            edition_id: The embargoed edition.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``release``).

        A release that breaks a cross-product rule raises
        `CrossProductRuleError` and writes nothing: a data edition whose
        partial or full artefact matches only an embargoed structure edition
        (``rule="publish-order"``; release that one first); a stale data
        edition, or one whose full artefact with no structure home differs
        from another data product's live copy (``rule="full-artefacts"``); a
        structure edition holding a full artefact another structure product
        holds live (``rule="structure-home"``).

        An edition built on an edition that is no longer live raises
        `NonFastForwardError`, whose message names both and the fix. An edition
        that is not recorded (``rule="unknown"``) or not embargoed
        (``rule="state"``) raises `InventoryRuleError`, so a withdrawn edition
        is never released. An empty *reason* raises `ValueError`.
        """

        def step(leaf: Leaf, now: datetime) -> Leaf:
            try:
                return release(leaf, edition_id, now=now, explicit=True)
            except TransitionError as exc:
                entry = next((e for e in leaf.editions if e.edition_id == edition_id), None)
                live = None if entry is None else head(leaf, entry.schema_version)
                if exc.rule != "fast-forward" or entry is None or live is None:
                    raise
                msg = (
                    f"{product_id}: {edition_id} is built on {entry.previous}, and the live edition is "
                    f"{live.edition_id}, so releasing it would drop that edition's changes. Cancel it with "
                    f"cancel(), then seal a new edition built on {live.edition_id}."
                )
                raise NonFastForwardError(msg, rule="fast-forward", product=product_id, edition=edition_id) from exc

        return self._commit_product(product_id, edition_id, step, kind="release", reason=reason)

    def cancel(self, product_id: str, edition_id: str, *, reason: str) -> int:
        """Withdraw an embargoed edition before its release.

        Args:
            product_id: The product.
            edition_id: The embargoed edition.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``withdraw``).

        An edition that is not recorded (``rule="unknown"``) or not embargoed
        (``rule="state"``) raises `InventoryRuleError`, and an empty *reason*
        raises `ValueError`.
        """
        return self._commit_product(
            product_id,
            edition_id,
            lambda leaf, now: cancel(leaf, edition_id, now=now),
            kind="withdraw",
            reason=reason,
        )

    # -- catalog changes -------------------------------------------------

    def set_embargo(self, *, on: bool, reason: str) -> int:
        """Turn the catalog's embargo on or off.

        With embargo on, a seal records its edition ``embargoed``, and only
        `release` makes it live. A seal that computed its record with embargo
        off and lost the race to this version re-reads the head, and records
        its edition ``embargoed``.

        Args:
            on: The new setting, keyword-only: ``set_embargo(on=True, reason=...)``.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``embargo-change``).

        Setting the embargo it already has raises `ValueError` naming the
        current state and writes no version; the check runs against the head
        the version would land on, and the embargo is off before the first
        version. An empty *reason* raises `ValueError` too.
        """
        _require_reason(reason)

        def change(full: FullInventory | None) -> Change:
            current = False if full is None else full.embargo
            if current == on:
                state = "on" if current else "off"
                msg = f"the catalog's embargo is already {state}; set_embargo(on={on}) wrote no Inventory version"
                raise ValueError(msg)
            return Change(leaves={}, embargo=on)

        return self._writer.commit(change, kind="embargo-change", reason=reason).seq

    def describe(self, description: CatalogDescription, *, reason: str) -> int:
        """Record the catalog's description in the Inventory root's ``extensions.statcat``.

        The section becomes ``{"format_version": "3", "description": ...}``,
        with the catalog-level fields `statcatlib.bundle.description_to_json`
        writes; any other key a later statcatlib put there is kept. Every
        later version carries the description unchanged, and
        `Catalog.description` reads it.

        Args:
            description: The description.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``describe``).

        An empty *reason* raises `ValueError`.
        """
        _require_reason(reason)
        described = description_to_json(description)

        def change(full: FullInventory | None) -> Change:
            extensions = {} if full is None else dict(full.root.extensions)
            section = dict(extensions.get(STATCAT_SECTION, {}))
            section["format_version"] = ROOT_SECTION_FORMAT
            section["description"] = described
            extensions[STATCAT_SECTION] = section
            return Change(leaves={}, extensions=extensions)

        return self._writer.commit(change, kind="describe", reason=reason).seq

    # -- prune -----------------------------------------------------------

    def prune(self, *, older_than: timedelta, reason: str, dry_run: bool = True) -> PruneReport:
        """Retire old editions and delete the files no edition needs (design decision P7).

        It takes one window, *older_than*, and runs three steps:

        1. It retires the ``superseded`` and ``withdrawn`` editions whose
           ``state_changed_at`` is older than the window, in one Inventory
           version (``event.kind`` ``retire``), re-applied to the new head
           after a lost race.
        2. It deletes the directory of each edition tombstoned for longer
           than the window.
        3. It deletes a file under a product's ``data/`` when no
           ``manifest.json`` on disk under the product lists it and its
           mtime is older than the window.

        A product with a working copy or a seal journal entry is skipped in
        all three steps and reported in `PruneReport.skipped_products`, and
        so is one with an edition whose manifest cannot be read. Prune
        deletes no Inventory root or leaf, never expires a tombstone, and
        never deletes an edition the Inventory does not tombstone, so a
        live, embargoed or unrecorded edition stays. It never retires a
        structure edition that a live or embargoed data edition's
        ``partial_of`` cites (specification §11.4); when a manifest that
        decision needs cannot be read, it retires no structure edition. An
        edition retired in
        step 1 keeps its directory until a run after its tombstone has aged
        past the window.

        With ``dry_run=True``, the default, it retires and deletes nothing
        and reports what a real run would do. A real run logs every deleted
        path at INFO. A deletion that fails is recorded in
        `PruneReport.failed`, logged at WARNING, and the run continues; the
        path is tried again on the next run.

        Args:
            older_than: The window, a positive `timedelta`.
            reason: Why, recorded as the ``retire`` version's ``event.reason``.
                A dry run requires one too.
            dry_run: Report only, the default; ``False`` retires and deletes.

        Returns:
            The report.

        Raises:
            ValueError: *older_than* is not positive, or *reason* is blank.
            TypeError: The catalog is in object storage, where the release
                job retires and ``statcat gc`` deletes.

        A retirement sdmxlib's rule refuses raises `InventoryRuleError`, and
        `InventoryContentionError` follows `MAX_RACES` lost races; neither
        deletes anything.
        """
        if not isinstance(self._writer.storage, LocalInventoryStorage):
            msg = (
                f"prune runs on a catalog directory; on {self._writer.storage.location} the release job "
                "(statcat release) retires editions and statcat gc deletes their files"
            )
            raise TypeError(msg)
        _require_reason(reason)
        if older_than <= timedelta(0):
            msg = f"prune needs a positive window; got older_than={older_than!r}"
            raise ValueError(msg)
        root = self._writer.root
        cutoff = datetime.now(UTC) - older_than
        full = self._writer.full()
        stones = () if full is None else full.root.retired
        product_dirs = {p.name: p for p in sorted((root / "products").glob("*")) if p.is_dir()}
        skipped: dict[str, str] = {}
        recorded: list[str] = [] if full is None else full.products()
        for product_id in sorted({*product_dirs, *recorded}):
            tombstoned = {t.edition_id for t in stones if t.product_id == product_id}
            why = _busy(root / "products" / product_id, tombstoned)
            if why is not None:
                skipped[product_id] = why

        retired, seq, stones = self._prune_retire(full, cutoff, skipped, reason=reason, dry_run=dry_run)
        failed: dict[str, str] = {}
        deleted_dirs = _prune_dirs(root, stones, cutoff, skipped, dry_run=dry_run, failed=failed)

        deleted_files: list[str] = []
        gone = set(deleted_dirs)
        for product_id, product_dir in product_dirs.items():
            if product_id in skipped:
                continue
            tombstoned = {t.edition_id for t in stones if t.product_id == product_id}
            why = _busy(product_dir, tombstoned)
            if why is not None:
                skipped[product_id] = why
                continue
            try:
                deleted_files += self._prune_data(product_dir, cutoff, gone, dry_run=dry_run, failed=failed)
            except ManifestError as exc:
                skipped[product_id] = f"an edition's manifest cannot be read: {exc}"

        return PruneReport(
            retired=tuple(retired),
            deleted_dirs=tuple(deleted_dirs),
            deleted_files=tuple(deleted_files),
            skipped_products=dict(sorted(skipped.items())),
            failed=failed,
            root=root,
            dry_run=dry_run,
            seq=seq,
        )

    def _prune_retire(
        self,
        full: FullInventory | None,
        cutoff: datetime,
        skipped: Collection[str],
        *,
        reason: str,
        dry_run: bool,
    ) -> tuple[list[str], int | None, tuple[Tombstone, ...]]:
        """Prune's step 1: retire the old superseded and withdrawn editions in one ``retire`` version.

        The change comes from `statcatlib.store.inventory.retire_change`,
        which the release job also uses.

        Args:
            full: The head read when prune started, or ``None``.
            cutoff: The window's start; older entries retire.
            skipped: The products prune leaves alone.
            reason: The version's ``event.reason``.
            dry_run: Plan against *full* and write nothing.

        Returns:
            The editions retired, as ``<product>/<edition_id>``; the version
            written, or ``None``; and the tombstones of the head prune's
            later steps read.
        """
        stones: tuple[Tombstone, ...] = () if full is None else full.root.retired
        artefacts = self._writer.artefacts
        if dry_run:
            planned = retire_change(full, cutoff, now=datetime.now(UTC), artefacts=artefacts, skipped=skipped)
            return _new_tombstones(full, planned), None, stones
        retired: list[str] = []

        def change(head_now: FullInventory | None) -> Change | None:
            planned = retire_change(head_now, cutoff, now=datetime.now(UTC), artefacts=artefacts, skipped=skipped)
            retired[:] = _new_tombstones(head_now, planned)
            return planned

        written = self._writer.commit(change, kind="retire", reason=reason)
        if written is None:
            return [], None, stones
        return retired, written.seq, written.retired

    def _prune_data(
        self, product_dir: Path, cutoff: datetime, gone: Collection[str], *, dry_run: bool, failed: dict[str, str]
    ) -> list[str]:
        """Prune's step 3 for one product: delete its old ``data/`` files that no manifest on disk lists.

        Args:
            product_dir: The product's directory, which is not busy.
            cutoff: The window's start; older files go.
            gone: The edition directories step 2 deleted, or would delete in
                a dry run, as ``products/<product>/<edition_id>``; their
                manifests protect nothing.
            dry_run: Report only.
            failed: Where a failed deletion is recorded.

        Returns:
            The files deleted, as ``products/<product>/data/...``.

        A manifest that cannot be read raises `ManifestError` before any
        file is deleted.
        """
        data_dir = product_dir / "data"
        if not data_dir.is_dir():
            return []
        root = self._writer.root
        listed: set[str] = set()
        for info in list_editions(product_dir):
            edition_dir = product_dir / info.edition_id
            if info.sealed and edition_dir.relative_to(root).as_posix() not in gone:
                listed |= {p for p in edition_files(read_manifest(edition_dir)) if p.startswith("data/")}
        deleted: list[str] = []
        oldest = cutoff.timestamp()
        for file in sorted(data_dir.rglob("*")):
            if not file.is_file() or file.relative_to(product_dir).as_posix() in listed:
                continue
            if file.stat().st_mtime >= oldest:
                continue
            rel = file.relative_to(root).as_posix()
            if _delete(file, rel, Path.unlink, dry_run=dry_run, failed=failed):
                deleted.append(rel)
        return deleted


def _prune_dirs(
    root: Path,
    stones: Iterable[Tombstone],
    cutoff: datetime,
    skipped: Collection[str],
    *,
    dry_run: bool,
    failed: dict[str, str],
) -> list[str]:
    """Prune's step 2: delete the directory of each edition tombstoned before *cutoff*.

    Args:
        root: The catalog directory.
        stones: The head's tombstones.
        cutoff: The window's start; older tombstones' directories go.
        skipped: The products prune leaves alone.
        dry_run: Report only.
        failed: Where a failed deletion is recorded.

    Returns:
        The directories deleted, as ``products/<product>/<edition_id>``.
    """
    deleted: list[str] = []
    for stone in stones:
        edition_dir = root / "products" / stone.product_id / stone.edition_id
        if stone.retired_at >= cutoff or stone.product_id in skipped or not edition_dir.is_dir():
            continue
        rel = edition_dir.relative_to(root).as_posix()
        if _delete(edition_dir, rel, shutil.rmtree, dry_run=dry_run, failed=failed):
            deleted.append(rel)
    return deleted


def _delete(path: Path, rel: str, remove: Callable[[Path], object], *, dry_run: bool, failed: dict[str, str]) -> bool:
    """Delete *path* with *remove*, unless *dry_run*; a failure goes into *failed* and returns ``False``.

    Args:
        path: The file or directory.
        rel: Its path from the catalog directory, for the report and the log.
        remove: `shutil.rmtree` for a directory, `Path.unlink` for a file.
        dry_run: Delete nothing and report it as deleted.
        failed: Where a failure is recorded, *rel* to the error.

    Returns:
        Whether it was deleted, or would be in a dry run.
    """
    if dry_run:
        return True
    try:
        remove(path)
    except OSError as exc:
        failed[rel] = str(exc)
        log.warning("prune could not delete %s: %s; the next prune tries it again", path, exc)
        return False
    log.info("prune deleted %s", path)
    return True

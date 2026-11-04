"""The Inventory writer: commits Inventory versions on local disk (Phase 2a).

sdmxlib owns the Inventory's documents, their validation and the state
transition rules (`sdmxlib.inventory`). statcatlib writes them, and this module
is the only code that writes ``catalog.json``, ``inventory/`` and ``CURRENT``.
It adds these things to sdmxlib's helpers:

- **exclusive create**: each document is written by
  `sdmxlib.storage.create_exclusive`, which stages the file, fsyncs it,
  hard-links it to its name and fsyncs the directory, so a name never holds
  half a file, and two writers never both create one root;
- **the ``CURRENT`` advance**: ``CURRENT`` is replaced only by a higher
  ``seq``, so a slow writer never moves it back;
- **the retry loop**: `InventoryWriter.commit` re-reads the head and re-applies
  the change when another writer created the next version first, so every
  rule is evaluated against the version it lands on;
- **the cross-product rules** (`statcatlib.store.cross_product`, Phase 3):
  `InventoryWriter.commit` refuses a version that breaks specification §6.5
  item 4, 7 or 8 for an entry that becomes live or embargoed in it, with
  `CrossProductRuleError`;
- **the history fields** (Inventory 1.2, GH-125): every root carries
  ``previous_root_sha256`` and ``changes``, computed from root ``seq - 1`` and
  its leaves, whatever version a correction is built from, and again after a
  lost race;
- **the stage-1 refusals**: every commit decodes ``catalog.json`` as a writer
  without the catalog lock, so it refuses a catalog that has opted in to expiry,
  and refuses a base version more than 5 minutes ahead of this machine's clock.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from attrs import frozen
from sdmxlib.edition import Producer
from sdmxlib.inventory import (
    CATALOG_FORMAT,
    CATALOG_NAME,
    CURRENT_NAME,
    CatalogDocument,
    ChangeItem,
    Current,
    Event,
    EventKind,
    FullInventory,
    InventoryError,
    Leaf,
    Root,
    Tombstone,
    TransitionError,
    change_list,
    decode_catalog,
    decode_current,
    decode_leaf,
    encode,
    leaf_ref,
    local_fetch,
    next_created_at,
    next_root,
    root_path,
    sha256_hex,
)
from sdmxlib.storage import create_exclusive

from statcatlib.store import cross_product

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from sdmxlib.inventory import LeafRef, Rule
    from sdmxlib.storage import JsonValue

    from statcatlib.store.cross_product import Artefacts, CrossRule

__all__ = [
    "MAX_RACES",
    "Change",
    "CrossProductRuleError",
    "InventoryConflictError",
    "InventoryContentionError",
    "InventoryRuleError",
    "InventoryWriteRefusedError",
    "InventoryWriter",
    "NonFastForwardError",
    "producer",
]

log = logging.getLogger(__name__)

#: Consecutive lost races after which `InventoryWriter.commit` gives up.
MAX_RACES = 8


class InventoryConflictError(Exception):
    """Another writer created the Inventory version this one tried to create.

    `InventoryWriter.try_commit` raises it; `InventoryWriter.commit` catches
    it, re-reads the head and tries again.
    """


class InventoryContentionError(Exception):
    """A commit lost `MAX_RACES` races in a row to other writers, and gave up."""


class InventoryWriteRefusedError(InventoryError):
    """This writer must not write the next Inventory version, whatever the version holds.

    The catalog has opted in to expiry (``catalog.json`` 2.x), which needs a
    writer with the catalog lock, or the base version's ``created_at`` is more
    than 5 minutes ahead of this machine's clock. The message names the
    format, or the base version's time and this machine's. It stays refused
    until the catalog or the clock changes, so a seal refused after its
    manifest keeps its journal entry, as `InventoryContentionError` does.
    """


class InventoryRuleError(Exception):
    """A change sdmxlib's Inventory rules refuse, or a lifecycle change statcatlib refuses.

    Attributes:
        rule: The rule broken: one of sdmxlib's `Rule` values, ``"kind"``
            also for a product reopened with another kind,
            ``"lifecycle"`` for a product reopened with another lifecycle, or
            a cross-product rule (`CrossProductRuleError`):
            ``"full-artefacts"``, ``"structure-home"`` or
            ``"publish-order"``.
        product: The product.
        edition: The edition, where one is concerned.
    """

    rule: Rule | Literal["lifecycle"] | CrossRule
    product: str
    edition: str | None

    def __init__(
        self, message: str, *, rule: Rule | Literal["lifecycle"] | CrossRule, product: str, edition: str | None
    ) -> None:
        """Say why, and name the rule, the product and the edition."""
        super().__init__(message)
        self.rule = rule
        self.product = product
        self.edition = edition

    @classmethod
    def wrapping(cls, exc: TransitionError, *, product: str, edition: str | None) -> InventoryRuleError:
        """The error for sdmxlib's refusal *exc*, which becomes its ``__cause__`` when raised.

        A ``fast-forward`` refusal becomes a `NonFastForwardError`.
        """
        kind = NonFastForwardError if exc.rule == "fast-forward" else cls
        return kind(str(exc), rule=exc.rule, product=product, edition=edition)


class NonFastForwardError(InventoryRuleError):
    """An edition built on an edition that is no longer live.

    The message names the edition, its base, the live edition, and the fix:
    discard the working copy, then start one from the live edition.
    """


class CrossProductRuleError(InventoryRuleError):
    """A version that breaks a cross-product rule, specification §6.5 item 4, 7 or 8.

    `product` and `edition` name the incoming entry, the one that becomes
    live or embargoed in the version. The message names the item, the URN,
    the editions compared and the fix. `rule` is ``"full-artefacts"`` (item
    4: a data edition's full artefact or partial matches no live or
    embargoed edition of its structure home, a cut codelist has no home, or
    a full artefact with no home differs from another data product's live
    copy), ``"structure-home"`` (item 7: a second structure home among live
    editions) or ``"publish-order"`` (item 8: a data edition entering live
    before its structure edition).

    Attributes:
        item: The specification §6.5 item: 4, 7 or 8.
        urn: The artefact concerned.
        editions: ``(product, edition)`` pairs: the incoming entry first,
            then each edition it was compared with.
    """

    item: Literal[4, 7, 8]
    urn: str
    editions: tuple[tuple[str, str], ...]

    def __init__(
        self,
        message: str,
        *,
        rule: CrossRule,
        item: Literal[4, 7, 8],
        urn: str,
        editions: Sequence[tuple[str, str]],
        product: str,
        edition: str | None,
    ) -> None:
        """Say why, and name the rule, the item, the URN and the editions compared."""
        super().__init__(message, rule=rule, product=product, edition=edition)
        self.item = item
        self.urn = urn
        self.editions = tuple(editions)

    @classmethod
    def of(cls, refusal: cross_product.Refusal) -> CrossProductRuleError:
        """The error for *refusal*, from `statcatlib.store.cross_product.refusals`."""
        return cls(
            refusal.message,
            rule=refusal.rule,
            item=refusal.item,
            urn=refusal.urn,
            editions=refusal.editions,
            product=refusal.product,
            edition=refusal.edition,
        )


def producer() -> Producer:
    """What statcatlib records as the producer of an Inventory version or an edition."""
    from importlib import metadata  # noqa: PLC0415

    try:
        version = metadata.version("statcatlib")
    except metadata.PackageNotFoundError:
        version = "unknown"
    return Producer(name="statcatlib", version=version)


def _actor() -> str:
    """``<os user>@<hostname>``: who acts, from the process, never from a caller."""
    try:
        user = getpass.getuser()
    except KeyError, OSError:
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


@frozen
class Change:
    """What one Inventory version changes, as `InventoryWriter.commit`'s *change* returns it.

    Attributes:
        leaves: Each changed product's next leaf.
        retired: The tombstones the next root holds; ``None`` keeps the head's.
        embargo: The next root's embargo setting; ``None`` keeps the head's.
        extensions: The next root's extension sections; ``None`` keeps the head's.
        built_from: The older version a correction is built from
            (``previous_inventory``); ``None`` builds from the head. The
            history fields follow the head either way.
    """

    leaves: Mapping[str, Leaf]
    retired: Sequence[Tombstone] | None = None
    embargo: bool | None = None
    extensions: Mapping[str, Mapping[str, JsonValue]] | None = None
    built_from: int | None = None


def _fsync_dir(directory: Path) -> None:
    """Flush *directory*'s entries to disk; a no-op where directories cannot be opened (Windows)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _stage(path: Path, data: bytes) -> Path:
    """Write *data* beside *path* under a staged name, fsynced, and return that name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with staged.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return staged


class InventoryWriter:
    """Commits Inventory versions to the catalog directory *root*, on local disk.

    It reads the head with `sdmxlib.inventory.FullInventory`, builds the next
    root with `sdmxlib.inventory.next_root`, and writes leaves, the root and
    ``CURRENT`` in that order (spec §6.6).
    """

    def __init__(self, root: str | Path, *, artefacts: Artefacts | None = None) -> None:
        """Write the catalog at *root*.

        Args:
            root: The catalog directory.
            artefacts: How the cross-product rules read an edition's
                artefacts; a `statcatlib.store.cross_product.ManifestArtefacts`
                over *root* when ``None``.
        """
        self._root = Path(root)
        self._artefacts: Artefacts = artefacts or cross_product.ManifestArtefacts(self._root)

    @property
    def root(self) -> Path:
        """The catalog directory."""
        return self._root

    @property
    def artefacts(self) -> Artefacts:
        """The reader the cross-product rules read each edition's artefacts with."""
        return self._artefacts

    def refusals(
        self, full: FullInventory | None, leaves: Mapping[str, Leaf], *, artefacts: Artefacts | None = None
    ) -> dict[str, CrossProductRuleError]:
        """The cross-product rules (`statcatlib.store.cross_product.refusals`) over the version *leaves* would write.

        Args:
            full: The head, or ``None`` before the first version.
            leaves: The planned change's leaves.
            artefacts: The reader; the writer's when ``None``.

        Returns:
            Each refused product's error, by product.

        A manifest the reader cannot read raises `sdmxlib.edition.ManifestError`.
        """
        found = cross_product.refusals(full, leaves, artefacts or self._artefacts)
        return {product_id: CrossProductRuleError.of(refusal) for product_id, refusal in found.items()}

    # -- reading ---------------------------------------------------------

    def catalog_document(self) -> CatalogDocument:
        """``catalog.json``, decoded as a reader decodes it, 2.x included.

        A missing one raises `FileNotFoundError`, a malformed one `InventoryError`.
        """
        return decode_catalog((self._root / CATALOG_NAME).read_bytes())

    def has_version(self) -> bool:
        """Whether any Inventory version has been written: ``CURRENT`` or version 1 exists."""
        return (self._root / CURRENT_NAME).is_file() or (self._root / root_path(1)).is_file()

    def full(self) -> FullInventory | None:
        """The newest valid version, by the read protocol, or ``None`` before the first.

        Versions that exist and are all invalid raise `InventoryError`.
        """
        if not self.has_version():
            return None
        return FullInventory(local_fetch(self._root)).refresh()

    def head(self) -> Root | None:
        """The newest valid version's root, or ``None`` before the first."""
        full = self.full()
        return None if full is None else full.root

    # -- writing ---------------------------------------------------------

    def _write_leaf(self, path: Path, data: bytes) -> None:
        """Create a leaf. One already there is the same leaf: its name is the hash of its bytes."""
        if path.exists():
            return
        with contextlib.suppress(FileExistsError):
            create_exclusive(path, data)

    def _advance_current(self, root: Root, data: bytes) -> None:
        """Point ``CURRENT`` at *root*, unless it already names a version at least as new.

        Two writers can race the replace and leave ``CURRENT`` one version
        behind; readers probe forward from it (spec §6.6), so a lagging hint
        costs extra reads and still finds the newest root.
        """
        target = self._root / CURRENT_NAME
        try:
            held = decode_current(target.read_bytes())
        except FileNotFoundError, InventoryError:
            held = None
        if held is not None and held.restore_epoch == root.restore_epoch and held.seq >= root.seq:
            return
        current = Current(seq=root.seq, root_sha256=sha256_hex(data), restore_epoch=root.restore_epoch)
        staged = _stage(target, encode(current))
        staged.replace(target)
        _fsync_dir(self._root)

    def write_catalog_json(self, *, catalog_id: str | None = None) -> CatalogDocument:
        """Create ``catalog.json``, with a UUIDv7 ``catalog_id`` and ``restore_epoch`` 0.

        Args:
            catalog_id: The id to record; a new UUIDv7 when ``None``.

        Returns:
            The document written. A catalog that has one already raises
            `FileExistsError`.
        """
        document = CatalogDocument(
            format_version=CATALOG_FORMAT, catalog_id=catalog_id or str(uuid.uuid7()), restore_epoch=0
        )
        create_exclusive(self._root / CATALOG_NAME, encode(document))
        return document

    def check_writable(self, base: Root | None, *, now: datetime | None = None) -> CatalogDocument:
        """Refuse a commit this writer must not make on top of *base*, and return ``catalog.json``.

        Every commit runs it first, and a seal runs it before it writes
        anything.

        Args:
            base: The newest version, or ``None`` for the catalog's first.
            now: This machine's time; the clock when ``None``.

        Returns:
            ``catalog.json``, decoded as a writer without the catalog lock.

        Raises:
            InventoryWriteRefusedError: ``catalog.json`` is malformed or 2.x, or
                *base* was created more than 5 minutes ahead of *now*; the
                message names the format or the base version's time.

        A missing ``catalog.json`` raises `FileNotFoundError`.
        """
        raw = (self._root / CATALOG_NAME).read_bytes()
        try:
            document = decode_catalog(raw, decoder="stage-1-writer")
            next_created_at(base, now or datetime.now(UTC))
        except InventoryError as exc:
            raise InventoryWriteRefusedError(str(exc)) from exc
        return document

    def _stored_root(self, base: Root) -> bytes:
        """The stored bytes of *base*; a root a restore removed means another writer moved the head."""
        try:
            return (self._root / root_path(base.seq)).read_bytes()
        except FileNotFoundError as exc:
            msg = f"Inventory version {base.seq} no longer exists"
            raise InventoryConflictError(msg) from exc

    def _base_leaves(
        self, base: Root, products: Mapping[str, LeafRef], retired: Iterable[Tombstone]
    ) -> dict[str, Leaf]:
        """The leaves of *base* the change list reads: each product whose pointer or tombstones change.

        Args:
            base: The base version, root ``seq - 1``.
            products: Every product's leaf pointer in the new version.
            retired: The new version's tombstones.

        Returns:
            Those products' leaves in *base*, by product.

        Raises:
            InventoryError: A leaf is missing or malformed.
        """
        moved = {t.product_id for t in set(base.retired) ^ set(retired)}
        moved |= {p for p, ref in base.products.items() if products.get(p) != ref}
        found: dict[str, Leaf] = {}
        for product_id in moved & base.products.keys():
            ref = base.products[product_id]
            try:
                found[product_id] = decode_leaf((self._root / ref.leaf).read_bytes())
            except FileNotFoundError as exc:
                msg = f"{product_id}'s leaf {ref.leaf} in Inventory version {base.seq} is missing"
                raise InventoryError(msg) from exc
        return found

    def _changes(
        self,
        base: Root | None,
        leaves: Mapping[str, Leaf],
        products: Mapping[str, LeafRef],
        retired: Sequence[Tombstone],
    ) -> tuple[ChangeItem, ...]:
        """The change list of the version after *base*, which is root ``seq - 1`` whatever it is built from."""
        base_leaves = {} if base is None else self._base_leaves(base, products, retired)
        return change_list(base, base_leaves, products=products, leaves=leaves, retired=retired)

    def try_commit(
        self,
        base: Root | None,
        leaves: Mapping[str, Leaf],
        *,
        retired: Sequence[Tombstone] | None = None,
        embargo: bool | None = None,
        extensions: Mapping[str, Mapping[str, JsonValue]] | None = None,
        built_from: int | None = None,
        now: datetime | None = None,
        kind: EventKind,
        reason: str,
    ) -> Root:
        """Write the version after *base*, once: its changed leaves, its root, then ``CURRENT``.

        The root carries the history fields, computed from *base*'s stored
        bytes and its leaves, also when *built_from* names an older version.

        Args:
            base: The newest version, read before the change was computed, or
                ``None`` for the catalog's first.
            leaves: Each changed product's next leaf; every other product
                keeps its leaf from *base*.
            retired: The tombstones; *base*'s when ``None``.
            embargo: The embargo setting; *base*'s when ``None``.
            extensions: The extension sections; *base*'s when ``None``.
            built_from: The older version a correction is built from; *base*
                when ``None``.
            now: This machine's time; the clock when ``None``.
            kind: What the version records (``event.kind``).
            reason: Why, in the caller's words (``event.reason``).

        Returns:
            The root written.

        Raises:
            InventoryConflictError: Another writer created the version after
                *base* first.

        `check_writable` raises `InventoryWriteRefusedError` before anything is
        written.
        """
        now = now or datetime.now(UTC)
        catalog = self.check_writable(base, now=now)
        previous_bytes = None if base is None else self._stored_root(base)
        products = dict(base.products) if base is not None else {}
        for product_id, leaf in leaves.items():
            data, ref = leaf_ref(leaf)
            self._write_leaf(self._root / ref.leaf, data)
            products[product_id] = ref
        tombstones = tuple(retired) if retired is not None else (base.retired if base is not None else ())
        root = next_root(
            base,
            products=products,
            event=Event(actor=_actor(), reason=reason, kind=kind),
            producer=producer(),
            now=now,
            retired=tombstones,
            embargo=embargo,
            # A root records catalog.json's restore_epoch when it is written.
            restore_epoch=catalog.restore_epoch,
            built_from=built_from,
            extensions=extensions,
            previous_bytes=previous_bytes,
            changes=self._changes(base, leaves, products, tombstones),
        )
        data = encode(root)
        try:
            create_exclusive(self._root / root_path(root.seq), data)
        except FileExistsError as exc:
            msg = f"another writer created Inventory version {root.seq} first"
            raise InventoryConflictError(msg) from exc
        self._advance_current(root, data)
        return root

    def _refuse(self, full: FullInventory | None, leaves: Mapping[str, Leaf]) -> None:
        """Raise the first refused product's `CrossProductRuleError`, by product id, when the rules refuse *leaves*."""
        found = self.refusals(full, leaves)
        if found:
            raise found[min(found)]

    def commit(self, change: Callable[[FullInventory | None], Change], *, kind: EventKind, reason: str) -> Root:
        """Commit one version, re-applying *change* to the new head after each lost race.

        *change* receives the head, or ``None`` before the first version, and
        applies sdmxlib's pure rules to it, raising on a refusal. Then, when
        the change makes an entry live or embargoed, the cross-product rules
        run on the version (`refusals`). Every precondition is evaluated
        again against each head it is given: the fast-forward check,
        ``if_live``, the embargo setting and the cross-product rules.

        Args:
            change: Computes the version's change from the head.
            kind: What the version records (``event.kind``).
            reason: Why (``event.reason``).

        Returns:
            The root written.

        Raises:
            InventoryContentionError: `MAX_RACES` races were lost in a row.

        A version that breaks a cross-product rule raises the first refused
        product's `CrossProductRuleError`, by product id, and a manifest the
        rules cannot read raises `sdmxlib.edition.ManifestError`; neither
        writes anything. `try_commit` raises `InventoryWriteRefusedError` for a
        catalog or a base version this writer must not write on, and it
        writes nothing either.
        """
        for _ in range(MAX_RACES):
            full = self.full()
            planned = change(full)
            self._refuse(full, planned.leaves)
            try:
                root = self.try_commit(
                    None if full is None else full.root,
                    planned.leaves,
                    retired=planned.retired,
                    embargo=planned.embargo,
                    extensions=planned.extensions,
                    built_from=planned.built_from,
                    kind=kind,
                    reason=reason,
                )
            except InventoryConflictError:
                log.debug("lost the race for the next Inventory version of %s; re-reading the head", self._root)
                continue
            return root
        msg = f"{self._root}: {MAX_RACES} commits in a row lost to other writers; try again"
        raise InventoryContentionError(msg)

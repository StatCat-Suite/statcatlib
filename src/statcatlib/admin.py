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

Each of them requires a ``reason`` and writes one Inventory version through
`InventoryWriter.commit`, which re-applies the rule to the new head after a
lost race. `CatalogAdmin.editions` only reads. A rule is evaluated with this
machine's clock (design decision P1), so a laptop with a skewed clock records
skewed times.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

from attrs import frozen
from sdmxlib.inventory import CATALOG_NAME, TransitionError, cancel, head, release, rollback
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.bundle import ROOT_SECTION_FORMAT, STATCAT_SECTION, description_to_json
from statcatlib.catalog import _refuse_legacy_layout
from statcatlib.store.inventory import Change, InventoryRuleError, InventoryWriter, NonFastForwardError

if TYPE_CHECKING:
    from collections.abc import Callable

    from sdmxlib.inventory import EventKind, FullInventory, Leaf, State

    from statcatlib.model.catalog import CatalogDescription

__all__ = ["CatalogAdmin", "EditionEntry"]


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
    """Operational changes to a catalog's Inventory: rollback, release, cancel, embargo and description.

    Open one with `open`. Every method but `editions`, `products` and
    `embargo` writes one Inventory version and returns its number.
    """

    _writer: InventoryWriter

    def __init__(self, *args: object, _writer: InventoryWriter | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError naming `open`."""
        if _writer is None or args or kwargs:
            msg = "open the operational object with CatalogAdmin.open(path)"
            raise TypeError(msg)
        self._writer = _writer

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open the catalog at *path* for operational changes.

        It reads ``catalog.json`` and writes nothing until a method is called.

        Args:
            path: The catalog directory.

        Returns:
            The operational object.

        Raises:
            FileNotFoundError: *path* is not a catalog.

        A legacy layout or other files at *path* raise `IncompatibleCatalogError`.
        """
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        if not (root / CATALOG_NAME).is_file():
            msg = f"{root} is not a catalog (no {CATALOG_NAME}); create one with Catalog.create({str(path)!r})"
            raise FileNotFoundError(msg)
        writer = InventoryWriter(root)
        writer.catalog_document()
        return cls(_writer=writer)

    def __repr__(self) -> str:
        return f"<CatalogAdmin {self._writer.root}>"

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
        return [
            EditionEntry(
                edition_id=e.edition_id,
                product_id=product_id,
                previous=e.previous,
                state=e.state,
                manifest_sha256=e.manifest_sha256,
                schema_version=e.schema_version,
                state_changed_at=e.state_changed_at,
                release_at=e.release_at,
            )
            for e in full.entries(product_id)
        ]

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
            step: sdmxlib's rule, given the leaf and this machine's clock.
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
            try:
                nxt = step(leaf, datetime.now(UTC))
            except TransitionError as exc:
                raise InventoryRuleError.wrapping(exc, product=product_id, edition=edition_id) from exc
            return Change(leaves={product_id: nxt})

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

        An empty *reason* raises `ValueError`. It acts on the live edition at
        this sdmxlib's store schema; rolling back another schema's live
        edition arrives in Phase 4.
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

        The release time is this machine's clock (design decision P1).

        Args:
            product_id: The product.
            edition_id: The embargoed edition.
            reason: Why, recorded as the version's ``event.reason``.

        Returns:
            The Inventory version written (``event.kind`` ``release``).

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

"""The cross-product rules: specification §6.5 items 4, 7 and 8 (Phase 3).

sdmxlib's Inventory rules read one product's leaf. These rules read every
product's live and embargoed editions and their manifests, so the Inventory
writer applies them (sdmxlib's plan assigns them to statcatlib). Each rule
applies to an **incoming** entry, one that becomes ``live`` or ``embargoed``
in the version being written, and compares it with editions of other
products only, since a product's own embargoed edition replaces its live one
at release.

- Item 7, ``structure-home``: a structure edition entering ``live`` holds a
  full artefact another structure product's live edition holds.
- Item 4, ``full-artefacts``: a data edition's full artefact that has a
  structure home matches no live or embargoed edition of the home; a
  partial's ``partial_of`` matches no live or embargoed structure edition
  holding the URN full (a cut codelist with no home included); or a data
  edition entering ``live`` holds a full artefact with no structure home
  whose fingerprint differs from another data product's copy that is live
  once the version is written.
- Item 8, ``publish-order``: a data edition entering ``live`` holds a full
  artefact with a home, or a partial, whose fingerprint only an embargoed
  home edition holds.

A URN's **structure home** is the structure product whose live or embargoed
edition holds it as a full artefact. A data edition is **stale** when it is
live or embargoed and holds a URN with a home, full or as a partial, whose
fingerprint (a partial's ``partial_of``) matches no live or embargoed edition
of the home: `stale_editions` lists them for ``statcat verify-catalog`` and
``statcat doctor``. Stale is derived and never recorded. A live or embargoed
data edition can also hold a partial whose URN no live or embargoed
structure edition holds full, after its structure home drops the URN.
`stale_editions` skips that URN, which has no home; item 4 refuses the
edition's release, and `homeless_partials` lists each such partial for
``statcat doctor``.

The rules read an edition's artefacts through an `Artefacts` callable:
`ManifestArtefacts` reads sealed manifests, and `StoreArtefacts` answers for
a working copy that is not sealed yet, for the seal's pre-check. This module
depends on sdmxlib only; `statcatlib.store.inventory.InventoryWriter` turns
each `Refusal` into a `CrossProductRuleError`.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import TYPE_CHECKING, Literal

from attrs import frozen
from sdmxlib.edition import MANIFEST_NAME, ManifestError, read_manifest
from sdmxlib.inventory import PartialRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from sdmxlib.edition import Manifest
    from sdmxlib.inventory import FullInventory, Kind, Leaf, LeafEntry, State

__all__ = [
    "ACTIVE",
    "Artefacts",
    "CrossRule",
    "HomelessPartial",
    "ManifestArtefacts",
    "Refusal",
    "StaleEdition",
    "StoreArtefacts",
    "cited_structures",
    "homeless_partials",
    "incoming",
    "manifest_records",
    "refusals",
    "stale_editions",
]

type Artefacts = Callable[[str, str, str], Mapping[str, PartialRecord]]
"""An edition's artefacts by URN, given ``(product_id, edition_id, manifest_sha256)``.

A full artefact maps to its fingerprint; a partial that records a
``partial_of`` maps to its fingerprint and that ``partial_of``. A partial
with no ``partial_of`` is left out: the rules skip it. It is the type of the
leaf's optional ``LeafEntry.artefacts``, so a reader of that field can take
the manifest reader's place.
"""

type CrossRule = Literal["full-artefacts", "structure-home", "publish-order"]
"""The cross-product rule a version breaks: `Refusal.rule`."""

#: The states an edition is compared in: the ones a reader can be given, now or at release.
ACTIVE: frozenset[State] = frozenset({"live", "embargoed"})


@frozen
class Refusal:
    """Why the cross-product rules refuse one incoming entry.

    Attributes:
        product: The incoming entry's product.
        edition: The incoming entry's edition.
        rule: The rule broken.
        item: The specification §6.5 item.
        urn: The artefact concerned.
        editions: ``(product, edition)`` pairs: the incoming entry first,
            then the editions it was compared with.
        message: What is wrong, and the fix.
    """

    product: str
    edition: str
    rule: CrossRule
    item: Literal[4, 7, 8]
    urn: str
    editions: tuple[tuple[str, str], ...]
    message: str


@frozen
class _Held:
    """One active edition holding a URN as a full artefact."""

    product: str
    entry: LeafEntry
    fingerprint: str


def manifest_records(manifest: Manifest) -> dict[str, PartialRecord]:
    """A sealed edition's artefacts as the rules read them (see `Artefacts`)."""
    found: dict[str, PartialRecord] = {}
    for artefact in manifest.artefacts:
        if not artefact.partial:
            found[artefact.urn] = PartialRecord(fingerprint=artefact.fingerprint)
        elif artefact.partial_of is not None:
            found[artefact.urn] = PartialRecord(fingerprint=artefact.fingerprint, partial_of=artefact.partial_of)
    return found


class ManifestArtefacts:
    """The `Artefacts` reader over sealed manifests, ``products/<product>/<edition>/manifest.json``.

    It compares the manifest's SHA-256 with the one the Inventory records
    (specification §6.3) before it parses it, and caches each edition's
    records by ``(edition_id, manifest_sha256)`` for its own life.
    """

    def __init__(self, root: Path) -> None:
        """Read the manifests of the catalog at *root*.

        Args:
            root: The catalog directory.
        """
        self._root = root
        self._cache: dict[tuple[str, str], dict[str, PartialRecord]] = {}

    def __call__(self, product_id: str, edition_id: str, manifest_sha256: str) -> Mapping[str, PartialRecord]:
        """The edition's artefacts.

        Args:
            product_id: The product.
            edition_id: The edition.
            manifest_sha256: The SHA-256 the Inventory records for its manifest.

        Returns:
            Its artefacts, by URN.

        Raises:
            ManifestError: The manifest is missing, unreadable, or not the one
                the Inventory records; the rules refuse to reason about it.
        """
        key = (edition_id, manifest_sha256)
        held = self._cache.get(key)
        if held is not None:
            return held
        edition_dir = self._root / "products" / product_id / edition_id
        try:
            raw = (edition_dir / MANIFEST_NAME).read_bytes()
        except OSError as exc:
            msg = (
                f"{product_id}/{edition_id}: the Inventory records the edition, and its manifest cannot be read: {exc}"
            )
            raise ManifestError(msg) from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != manifest_sha256:
            msg = (
                f"{product_id}/{edition_id}: manifest.json has SHA-256 {digest}, and the Inventory records "
                f"{manifest_sha256}; run statcat verify-catalog"
            )
            raise ManifestError(msg)
        found = manifest_records(read_manifest(edition_dir))
        self._cache[key] = found
        return found


class StoreArtefacts:
    """The `Artefacts` reader for a working copy's own edition, before it is sealed (the seal's pre-check).

    *read* computes the edition's records from its open store once, on first
    use; every other edition is read through *fallback*.
    """

    def __init__(
        self, edition_id: str, read: Callable[[], Mapping[str, PartialRecord]], *, fallback: Artefacts
    ) -> None:
        """Answer for *edition_id* with *read*, and for any other edition with *fallback*.

        Args:
            edition_id: The working copy's edition.
            read: Computes its artefacts, as its seal would fingerprint them.
            fallback: The reader for every other edition.
        """
        self._edition_id = edition_id
        self._read = read
        self._fallback = fallback
        self._held: Mapping[str, PartialRecord] | None = None

    def __call__(self, product_id: str, edition_id: str, manifest_sha256: str) -> Mapping[str, PartialRecord]:
        """The edition's artefacts: the working copy's from its store, any other's from the fallback."""
        if edition_id != self._edition_id:
            return self._fallback(product_id, edition_id, manifest_sha256)
        if self._held is None:
            self._held = self._read()
        return self._held


def _planned(full: FullInventory | None, leaves: Mapping[str, Leaf]) -> dict[str, Leaf]:
    """Every product's leaf in the version being written: the head's, with *leaves* in their place."""
    planned = {} if full is None else dict(full.leaves())
    planned.update(leaves)
    return planned


def incoming(full: FullInventory | None, leaves: Mapping[str, Leaf]) -> list[tuple[str, LeafEntry]]:
    """The entries of *leaves* that become ``live`` or ``embargoed`` in the version being written.

    Args:
        full: The head, or ``None`` before the first version.
        leaves: The planned change's leaves.

    Returns:
        ``(product, entry)`` pairs, by product, then edition.
    """
    found: list[tuple[str, LeafEntry]] = []
    for product_id, leaf in sorted(leaves.items()):
        before: dict[str, State] = {}
        if full is not None and product_id in full.products():
            before = {e.edition_id: e.state for e in full.leaf(product_id).editions}
        found += [(product_id, e) for e in leaf.editions if e.state in ACTIVE and before.get(e.edition_id) != e.state]
    return found


def _active(planned: Mapping[str, Leaf], kind: Kind) -> list[tuple[str, LeafEntry]]:
    return [
        (product_id, entry)
        for product_id, leaf in sorted(planned.items())
        if leaf.kind == kind
        for entry in leaf.editions
        if entry.state in ACTIVE
    ]


def _homes(structures: Iterable[tuple[str, LeafEntry]], artefacts: Artefacts) -> dict[str, list[_Held]]:
    """Each URN the structure entries hold as a full artefact, with the entries that hold it."""
    homes: dict[str, list[_Held]] = defaultdict(list)
    for product_id, entry in structures:
        for urn, held in artefacts(product_id, entry.edition_id, entry.manifest_sha256).items():
            if held.partial_of is None:
                homes[urn].append(_Held(product_id, entry, held.fingerprint))
    return homes


def _name(product_id: str, entry: LeafEntry) -> str:
    return f"{product_id}/{entry.edition_id} ({entry.state})"


def _short(fingerprint: str) -> str:
    return fingerprint[:12]


class _Rules:
    """The rules over one planned version; `refusals` builds one."""

    def __init__(self, planned: Mapping[str, Leaf], artefacts: Artefacts) -> None:
        self._artefacts = artefacts
        self._homes = _homes(_active(planned, "structure"), artefacts)
        self._data = _active(planned, "data")
        self._live_copies: dict[str, list[_Held]] | None = None

    def _read(self, product_id: str, entry: LeafEntry) -> Mapping[str, PartialRecord]:
        return self._artefacts(product_id, entry.edition_id, entry.manifest_sha256)

    def _no_home_copies(self) -> dict[str, list[_Held]]:
        """Each URN the live data entries hold full that no structure entry holds, read on first use."""
        if self._live_copies is None:
            self._live_copies = defaultdict(list)
            for product_id, entry in self._data:
                if entry.state != "live":
                    continue
                for urn, held in self._read(product_id, entry).items():
                    if held.partial_of is None and urn not in self._homes:
                        self._live_copies[urn].append(_Held(product_id, entry, held.fingerprint))
        return self._live_copies

    def structure(self, product_id: str, entry: LeafEntry) -> Refusal | None:
        """Item 7 for an incoming structure entry."""
        if entry.state != "live":
            return None
        for urn, held in sorted(self._read(product_id, entry).items()):
            if held.partial_of is not None:
                continue
            other = next(
                (h for h in self._homes.get(urn, ()) if h.product != product_id and h.entry.state == "live"), None
            )
            if other is not None:
                holder = _name(other.product, other.entry)
                message = (
                    f"{product_id}/{entry.edition_id}: {urn} is a full artefact of {holder}; "
                    "a full artefact has at most one structure home among live editions (specification §6.5 item 7). "
                    f"Make an edition of {other.product} that drops it live first, or drop it here"
                )
                editions = ((product_id, entry.edition_id), (other.product, other.entry.edition_id))
                return Refusal(product_id, entry.edition_id, "structure-home", 7, urn, editions, message)
        return None

    def data(self, product_id: str, entry: LeafEntry) -> Refusal | None:
        """Items 4 and 8 for an incoming data entry."""
        for urn, held in sorted(self._read(product_id, entry).items()):
            homes = self._homes.get(urn, [])
            if held.partial_of is not None:
                found = self._against_home(product_id, entry, urn, held.partial_of, homes, partial=True)
            elif homes:
                found = self._against_home(product_id, entry, urn, held.fingerprint, homes, partial=False)
            else:
                found = self._without_home(product_id, entry, urn, held.fingerprint)
            if found is not None:
                return found
        return None

    def _against_home(
        self, product_id: str, entry: LeafEntry, urn: str, fingerprint: str, homes: list[_Held], *, partial: bool
    ) -> Refusal | None:
        """Item 4 against the structure home, then item 8 for an entry entering ``live``."""
        what = f"the partial {urn}, cut from {_short(fingerprint)}," if partial else f"{urn} ({_short(fingerprint)})"
        incoming_one = (product_id, entry.edition_id)
        matches = [h for h in homes if h.fingerprint == fingerprint]
        if not matches:
            if not homes:
                message = (
                    f"{product_id}/{entry.edition_id}: {what} has no structure home: no live or embargoed structure "
                    "edition holds it full. Every codelist a data product cuts needs one (specification §6.5 items 4 "
                    "and 7). Add it to a structure product, or fill this product with partials=False"
                )
                return Refusal(product_id, entry.edition_id, "full-artefacts", 4, urn, (incoming_one,), message)
            held = "; ".join(f"{_name(h.product, h.entry)} holds {_short(h.fingerprint)}" for h in homes)
            message = (
                f"{product_id}/{entry.edition_id}: {what} matches no live or embargoed edition of its structure home "
                f"({held}); a data edition carries its home's copy (specification §6.5 item 4). Republish the "
                "product from the home's current edition"
            )
            editions = (incoming_one, *((h.product, h.entry.edition_id) for h in homes))
            return Refusal(product_id, entry.edition_id, "full-artefacts", 4, urn, editions, message)
        if entry.state == "live" and not any(h.entry.state == "live" for h in matches):
            names = ", ".join(_name(h.product, h.entry) for h in matches)
            message = (
                f"{product_id}/{entry.edition_id}: {what} matches only {names}; a data edition goes live after the "
                "structure edition it was cut from, or whose full artefact it holds (specification §6.5 item 8). "
                "Release that structure edition first"
            )
            editions = (incoming_one, *((h.product, h.entry.edition_id) for h in matches))
            return Refusal(product_id, entry.edition_id, "publish-order", 8, urn, editions, message)
        return None

    def _without_home(self, product_id: str, entry: LeafEntry, urn: str, fingerprint: str) -> Refusal | None:
        """Item 4 for a full artefact with no structure home, entering ``live``."""
        if entry.state != "live":
            return None
        others = [
            h for h in self._no_home_copies().get(urn, ()) if h.product != product_id and h.fingerprint != fingerprint
        ]
        if not others:
            return None
        held = "; ".join(f"{_name(h.product, h.entry)} holds {_short(h.fingerprint)}" for h in others)
        message = (
            f"{product_id}/{entry.edition_id}: {urn} ({_short(fingerprint)}) has no structure home, and differs from "
            f"the live copy of another data product ({held}); the products holding a full artefact with no structure "
            "home carry one fingerprint (specification §6.5 item 4). Seal them together in one Catalog.seal, or give "
            "the artefact a structure home"
        )
        editions = ((product_id, entry.edition_id), *((h.product, h.entry.edition_id) for h in others))
        return Refusal(product_id, entry.edition_id, "full-artefacts", 4, urn, editions, message)


def refusals(full: FullInventory | None, leaves: Mapping[str, Leaf], artefacts: Artefacts) -> dict[str, Refusal]:
    """The cross-product rules over the version *leaves* would write on *full*: each refused product's refusal.

    Args:
        full: The head, or ``None`` before the first version.
        leaves: The planned change's leaves.
        artefacts: Reads an edition's artefacts.

    Returns:
        The first refusal of each refused product, by product; empty when
        the version breaks no rule, and always empty when it has no incoming
        entry, in which case no manifest is read.

    *artefacts* raises sdmxlib's `ManifestError` for an edition it cannot
    read, and it propagates.
    """
    entering = incoming(full, leaves)
    if not entering:
        return {}
    planned = _planned(full, leaves)
    rules = _Rules(planned, artefacts)
    found: dict[str, Refusal] = {}
    for product_id, entry in entering:
        if product_id in found:
            continue
        if planned[product_id].kind == "structure":
            refusal = rules.structure(product_id, entry)
        else:
            refusal = rules.data(product_id, entry)
        if refusal is not None:
            found[product_id] = refusal
    return found


@frozen
class StaleEdition:
    """A live or embargoed data edition holding an old copy of a structure (specification §6.5 item 4).

    Attributes:
        product_id: The data product.
        edition_id: The stale edition.
        state: ``live``, which keeps serving its own closure, or
            ``embargoed``, whose release is refused.
        urn: The artefact.
        fingerprint: The fingerprint the edition holds: a full artefact's
            own, or a partial's ``partial_of``.
        home: The structure product that holds the URN.
        home_live: The home's live edition, or ``None`` when only an
            embargoed edition holds the URN.
    """

    product_id: str
    edition_id: str
    state: State
    urn: str
    fingerprint: str
    home: str
    home_live: str | None

    def __str__(self) -> str:
        live = self.home_live or "no live edition"
        return (
            f"{self.product_id}/{self.edition_id} ({self.state}) holds {self.urn} at {_short(self.fingerprint)}, "
            f"which no live or embargoed edition of its structure home {self.home} holds (live edition: {live}); "
            "republish the product"
        )


def stale_editions(full: FullInventory | None, artefacts: Artefacts) -> list[StaleEdition]:
    """Every stale data edition of *full*, one item per edition and URN.

    Args:
        full: The Inventory version, or ``None``.
        artefacts: Reads an edition's artefacts.

    Returns:
        The stale editions, by product, edition and URN.
    """
    if full is None:
        return []
    planned = dict(full.leaves())
    homes = _homes(_active(planned, "structure"), artefacts)
    found: list[StaleEdition] = []
    for product_id, entry in _active(planned, "data"):
        for urn, held in sorted(artefacts(product_id, entry.edition_id, entry.manifest_sha256).items()):
            holders = homes.get(urn)
            fingerprint = held.partial_of if held.partial_of is not None else held.fingerprint
            if not holders or any(h.fingerprint == fingerprint for h in holders):
                continue
            live_holder = next((h for h in holders if h.entry.state == "live"), None)
            home = (live_holder or holders[0]).product
            live = None if live_holder is None else live_holder.entry.edition_id
            found.append(StaleEdition(product_id, entry.edition_id, entry.state, urn, fingerprint, home, live))
    return found


@frozen
class HomelessPartial:
    """A live or embargoed data edition's partial whose URN no structure edition holds full (§6.5 item 4).

    Attributes:
        product_id: The data product.
        edition_id: The edition.
        state: ``live`` or ``embargoed``.
        urn: The partial's URN.
        partial_of: The fingerprint of the full artefact it was cut from.
    """

    product_id: str
    edition_id: str
    state: State
    urn: str
    partial_of: str

    def __str__(self) -> str:
        if self.state == "embargoed":
            effect = "its release is refused"
        else:
            effect = "it keeps serving its own closure, and the product's next seal that cuts the codelist is refused"
        return (
            f"{self.product_id}/{self.edition_id} ({self.state}) holds the partial {self.urn}, cut from "
            f"{_short(self.partial_of)}, and no live or embargoed structure edition holds {self.urn} full, so the "
            f"partial has no structure home and {effect} (specification §6.5 item 4). Seal a structure edition "
            f"that holds {self.urn} full and republish the product from it, or republish the product with "
            "partials=False"
        )


def homeless_partials(full: FullInventory | None, artefacts: Artefacts) -> list[HomelessPartial]:
    """Every partial of a live or embargoed data edition of *full* whose URN no structure edition holds full.

    `stale_editions` skips such a URN, since it has no structure home to be
    stale against.

    Args:
        full: The Inventory version, or ``None``.
        artefacts: Reads an edition's artefacts.

    Returns:
        The partials, by product, edition and URN.
    """
    if full is None:
        return []
    planned = dict(full.leaves())
    homes = _homes(_active(planned, "structure"), artefacts)
    return [
        HomelessPartial(product_id, entry.edition_id, entry.state, urn, held.partial_of)
        for product_id, entry in _active(planned, "data")
        for urn, held in sorted(artefacts(product_id, entry.edition_id, entry.manifest_sha256).items())
        if held.partial_of is not None and urn not in homes
    ]


def cited_structures(full: FullInventory | None, artefacts: Artefacts) -> set[tuple[str, str]]:
    """The structure editions, in any state, that a live or embargoed data edition's ``partial_of`` cites.

    Retention keeps them (specification §11.4), so a stale edition's partials
    still verify.

    Args:
        full: The Inventory version, or ``None``.
        artefacts: Reads an edition's artefacts.

    Returns:
        ``(product, edition)`` pairs.
    """
    if full is None:
        return set()
    planned = dict(full.leaves())
    cuts: set[tuple[str, str]] = set()
    for product_id, entry in _active(planned, "data"):
        cuts |= {
            (urn, held.partial_of)
            for urn, held in artefacts(product_id, entry.edition_id, entry.manifest_sha256).items()
            if held.partial_of is not None
        }
    if not cuts:
        return set()
    cited: set[tuple[str, str]] = set()
    for product_id, leaf in sorted(planned.items()):
        if leaf.kind != "structure":
            continue
        for entry in leaf.editions:
            held = artefacts(product_id, entry.edition_id, entry.manifest_sha256)
            if any((urn, record.fingerprint) in cuts for urn, record in held.items() if record.partial_of is None):
                cited.add((product_id, entry.edition_id))
    return cited

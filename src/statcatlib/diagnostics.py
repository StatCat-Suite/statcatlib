"""Integrity and support checks over a catalog directory (Phase 2a S4).

Two functions, each run by a ``statcat`` command:

- `verify_catalog` (``statcat verify-catalog PATH``) reads the newest valid
  Inventory version with `sdmxlib.inventory.FullInventory`. It checks every
  root the read protocol skipped, every leaf, and, for every entry the leaves
  hold, the ``manifest_sha256`` the Inventory records. It then runs
  `sdmxlib.edition.check_edition` at the ``publish`` profile with
  `statcatlib.bundle.check_catalog`, which checks every file the manifest
  lists, the shared ``data/`` files included, each partial against the
  structure edition it was cut from, and each structure edition's references
  in the other structure products. It also lists the stale data editions
  (specification §6.5 item 4), which are no finding. A catalog it cannot read
  at all raises `CatalogUnreadableError`.
- `doctor` (``statcat doctor PATH``) reports on a catalog for support: the
  layout, ``catalog.json``, ``CURRENT`` against the newest root, root and leaf
  validation, stale data editions, unsealed editions, seal journal entries,
  sealed editions no version names, files a writer staged and left behind,
  and a synced-folder path.

Both only read. Neither repairs, records or deletes anything: the journal
rule (`statcatlib.store.working_copy.settle_journal`) repairs a crashed seal
when the product's next working copy starts.
"""

from __future__ import annotations

import contextlib
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from attrs import frozen
from sdmxlib.edition import MANIFEST_NAME, ManifestError, check_edition, list_editions, read_journal
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    FullInventory,
    InventoryError,
    decode_current,
    validate_leaf,
)

from statcatlib.bundle import STATCAT_SECTION, check_catalog
from statcatlib.catalog import _refuse_legacy_layout
from statcatlib.store.cross_product import StaleEdition, stale_editions
from statcatlib.store.inventory import InventoryWriter
from statcatlib.store.store import IncompatibleCatalogError
from statcatlib.store.working_copy import manifest_sha256

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sdmxlib.edition import EditionInfo, Finding, JournalEntry
    from sdmxlib.inventory import LeafEntry

    from statcatlib.store.cross_product import Artefacts

__all__ = [
    "CatalogUnreadableError",
    "DoctorItem",
    "DoctorReport",
    "StaleEdition",
    "VerifyFinding",
    "VerifyReport",
    "doctor",
    "synced_folder",
    "verify_catalog",
]

#: Path parts that mark a folder a sync client manages, with the client's name.
#: macOS keeps the newer clients under ``~/Library/CloudStorage/<Client>-<account>``
#: and iCloud Drive under ``~/Library/Mobile Documents/com~apple~CloudDocs``.
_SYNC_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Dropbox", "Dropbox"),
    ("OneDrive", "OneDrive"),
    ("GoogleDrive", "Google Drive"),
    ("Google Drive", "Google Drive"),
    ("My Drive", "Google Drive"),
    ("iCloud Drive", "iCloud Drive"),
    ("iCloudDrive", "iCloud Drive"),
    ("com~apple~CloudDocs", "iCloud Drive"),
    ("Mobile Documents", "iCloud Drive"),
)


class CatalogUnreadableError(Exception):
    """The directory is not a catalog this build can read, so nothing in it can be verified.

    ``statcat verify-catalog`` exits 2 on it. Its message says why: no
    directory, a legacy layout, no or a malformed ``catalog.json``, or no
    valid Inventory version.
    """


@frozen
class VerifyFinding:
    """One thing `verify_catalog` found wrong.

    Attributes:
        file: What it is about, as a ``/``-separated path from the catalog
            directory: the damaged file where a check names one, or else the
            edition's directory.
        detail: What is wrong with it.
    """

    file: str
    detail: str

    def __str__(self) -> str:
        return f"{self.file}: {self.detail}"


@frozen
class VerifyReport:
    """What `verify_catalog` checked, and what it found.

    Attributes:
        root: The catalog directory.
        seq: The Inventory version read, or ``None`` for a catalog with no
            version yet, which has nothing to verify.
        editions: Each edition checked, as ``<product>/<edition_id>``.
        findings: Everything found wrong, in the order found.
        stale: Each stale data edition, one item per edition and URN: a live
            or embargoed data edition holding a copy of a structure that no
            live or embargoed edition of its structure home holds
            (specification §6.5 item 4). A stale edition is a state the
            specification allows until its product is republished, so it is
            no finding and leaves `ok` alone.
    """

    root: Path
    seq: int | None
    editions: tuple[str, ...]
    findings: tuple[VerifyFinding, ...]
    stale: tuple[StaleEdition, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether nothing was found wrong."""
        return not self.findings


@frozen
class DoctorItem:
    """One line of a `doctor` report.

    Attributes:
        topic: What it is about: ``layout``, ``synced folder``,
            ``catalog.json``, ``CURRENT``, ``inventory``,
            ``stale data editions``, ``working copy``, ``journal``,
            ``unrecorded edition`` or ``leftover file``.
        level: ``warning`` for something support acts on, ``info`` for state
            worth knowing.
        detail: The report.
    """

    topic: str
    level: Literal["info", "warning"]
    detail: str

    def __str__(self) -> str:
        return f"{self.topic}: {self.detail}"


@frozen
class DoctorReport:
    """What `doctor` found.

    Attributes:
        root: The directory examined.
        layout: ``catalog`` for a directory holding ``catalog.json``,
            ``legacy`` for a statcatlib 0.11 catalog or a 0.72 bundle, and
            ``unknown`` for anything else.
        items: The report, in the order `doctor` examined it.
    """

    root: Path
    layout: Literal["catalog", "legacy", "unknown"]
    items: tuple[DoctorItem, ...]

    @property
    def warnings(self) -> tuple[DoctorItem, ...]:
        """The items at level ``warning``."""
        return tuple(item for item in self.items if item.level == "warning")

    def about(self, topic: str) -> tuple[DoctorItem, ...]:
        """The items about *topic*.

        Args:
            topic: One of `DoctorItem.topic`'s values.

        Returns:
            Those items, in report order.
        """
        return tuple(item for item in self.items if item.topic == topic)


# -- verify-catalog ----------------------------------------------------------


def _open_inventory(root: Path) -> FullInventory | None:
    """The catalog's newest valid Inventory version, or ``None`` before the first.

    Args:
        root: The catalog directory.

    Returns:
        The version, read by the read protocol.

    Raises:
        CatalogUnreadableError: *root* is not a directory, holds a legacy
            layout, has no or a malformed ``catalog.json``, or has Inventory
            versions and no valid one.
    """
    if not root.is_dir():
        msg = f"{root} is not a directory"
        raise CatalogUnreadableError(msg)
    try:
        _refuse_legacy_layout(root)
    except IncompatibleCatalogError as exc:
        raise CatalogUnreadableError(str(exc)) from exc
    writer = InventoryWriter(root)
    try:
        writer.catalog_document()
    except FileNotFoundError:
        msg = f"{root} is not a catalog: it has no {CATALOG_NAME}"
        raise CatalogUnreadableError(msg) from None
    except InventoryError as exc:
        msg = f"{root / CATALOG_NAME} cannot be read: {exc}"
        raise CatalogUnreadableError(msg) from exc
    try:
        return writer.full()
    except InventoryError as exc:
        msg = f"{root}: {exc}"
        raise CatalogUnreadableError(msg) from exc


def _skipped(full: FullInventory) -> Iterator[VerifyFinding]:
    """A finding for each root the read protocol skipped, naming the root's file."""
    for line in full.skipped:
        file, _, detail = line.partition(": ")
        yield VerifyFinding(file=file, detail=detail or "skipped by the read protocol")


def _edition_findings(edition_rel: str, findings: tuple[Finding, ...]) -> Iterator[VerifyFinding]:
    """`check_edition`'s findings, each naming the file it is about where it names one."""
    for finding in findings:
        if finding.check == "files" or finding.subject == MANIFEST_NAME:
            yield VerifyFinding(file=posixpath.normpath(f"{edition_rel}/{finding.subject}"), detail=finding.detail)
        else:
            yield VerifyFinding(file=edition_rel, detail=f"[{finding.check}] {finding.subject}: {finding.detail}")


def _structures_for(full: FullInventory, root: Path, product_id: str) -> list[Path]:
    """The structure editions an edition of *product_id* is verified with.

    For a data product, every recorded edition of every structure product:
    sdmxlib checks each partial against the edition with its ``partial_of``,
    so a superseded data edition verifies against the superseded structure
    edition it was cut from. For a structure product, the other structure
    products' live and embargoed editions, where its references resolve.
    """
    structure = full.leaf(product_id).kind == "structure"
    found: list[Path] = []
    for other in full.products():
        if other == product_id:
            continue
        with contextlib.suppress(InventoryError):
            leaf = full.leaf(other)
            if leaf.kind != "structure":
                continue
            found += [
                root / "products" / other / e.edition_id
                for e in leaf.editions
                if not structure or e.state in {"live", "embargoed"}
            ]
    return found


def _verify_edition(root: Path, product_id: str, entry: LeafEntry, structures: list[Path]) -> list[VerifyFinding]:
    """Check one recorded edition: its manifest's SHA-256, then `check_edition` at ``publish`` with *structures*."""
    edition_rel = f"products/{product_id}/{entry.edition_id}"
    edition_dir = root / edition_rel
    manifest_rel = f"{edition_rel}/{MANIFEST_NAME}"
    if not (edition_dir / MANIFEST_NAME).is_file():
        return [VerifyFinding(file=manifest_rel, detail=f"missing; the Inventory records the edition {entry.state}")]
    found: list[VerifyFinding] = []
    digest = manifest_sha256(edition_dir)
    if digest != entry.manifest_sha256:
        detail = f"SHA-256 {digest}, the Inventory records {entry.manifest_sha256}"
        found.append(VerifyFinding(file=manifest_rel, detail=detail))
    report = check_edition(
        edition_dir, at="publish", structures=structures or None, extensions={STATCAT_SECTION: check_catalog}
    )
    found += _edition_findings(edition_rel, report.findings)
    return found


def verify_catalog(path: str | Path) -> VerifyReport:
    """Check a catalog's integrity: its Inventory, and every edition the Inventory records.

    It reads the newest valid Inventory version, as a reader does. A root the
    read protocol skipped is a finding. For each product it checks the leaf
    against its name and sdmxlib's leaf rules; for each entry of the leaf, in
    every state but retired, it compares ``manifest.json`` with the
    ``manifest_sha256`` the Inventory records, then runs
    ``check_edition(at="publish", extensions={"statcat": check_catalog})``,
    which checks the size and digests of every file the manifest lists,
    ``data/`` files included, the lake, the bindings and the ``statcat``
    section. It gives the check the catalog's structure editions: for a data
    edition every recorded structure edition, so each partial is checked
    against the edition it was cut from; for a structure edition the other
    structure products' live and embargoed editions, where its references
    resolve. Last it lists the stale data editions (`VerifyReport.stale`).
    It writes nothing.

    Args:
        path: The catalog directory.

    Returns:
        The report. A catalog with no Inventory version yet has nothing to
        verify, and its report has no ``seq``.

    A *path* that is not a directory, holds a legacy layout, has no or a
    malformed ``catalog.json``, or has Inventory versions and no valid one
    raises `CatalogUnreadableError`.
    """
    root = Path(path).absolute()
    full = _open_inventory(root)
    if full is None:
        return VerifyReport(root=root, seq=None, editions=(), findings=())
    findings = list(_skipped(full))
    editions: list[str] = []
    for product_id, ref in sorted(full.root.products.items()):
        try:
            leaf = full.leaf(product_id)
            validate_leaf(leaf)
        except InventoryError as exc:
            findings.append(VerifyFinding(file=ref.leaf, detail=f"{product_id}'s leaf: {exc}"))
            continue
        structures = _structures_for(full, root, product_id)
        for entry in leaf.editions:
            editions.append(f"{product_id}/{entry.edition_id}")
            findings += _verify_edition(root, product_id, entry, structures)
    stale, unread = _stale(full, InventoryWriter(root).artefacts)
    findings += unread
    return VerifyReport(
        root=root, seq=full.root.seq, editions=tuple(editions), findings=tuple(findings), stale=tuple(stale)
    )


def _stale(full: FullInventory, artefacts: Artefacts) -> tuple[list[StaleEdition], list[VerifyFinding]]:
    """The stale data editions, or a finding when a manifest the list needs cannot be read."""
    try:
        return stale_editions(full, artefacts), []
    except (ManifestError, InventoryError) as exc:
        return [], [VerifyFinding(file="inventory", detail=f"the stale data editions cannot be listed: {exc}")]


# -- doctor ------------------------------------------------------------------


def synced_folder(path: str | Path) -> str | None:
    """The sync client whose folder holds *path*, or ``None``.

    It matches the folder names Dropbox, OneDrive, iCloud Drive and Google
    Drive create, in *path* and in *path* with symbolic links resolved.

    Args:
        path: The path.

    Returns:
        The client's name, such as ``"Dropbox"``.
    """
    given = Path(path).absolute()
    for candidate in (given, given.resolve()):
        for part in candidate.parts:
            for prefix, client in _SYNC_PREFIXES:
                if part.startswith(prefix):
                    return client
    return None


def _layout(root: Path) -> tuple[Literal["catalog", "legacy", "unknown"], DoctorItem]:
    """The directory's layout, and the report line for it."""
    if not root.is_dir():
        return "unknown", DoctorItem("layout", "warning", f"{root} is not a directory")
    try:
        _refuse_legacy_layout(root)
    except IncompatibleCatalogError as exc:
        return "legacy", DoctorItem("layout", "warning", str(exc))
    if not (root / CATALOG_NAME).is_file():
        return "unknown", DoctorItem("layout", "warning", f"{root} has no {CATALOG_NAME}, so it is not a catalog")
    return "catalog", DoctorItem("layout", "info", f"{root} is a catalog of products and editions")


def _catalog_json(root: Path) -> DoctorItem:
    try:
        document = InventoryWriter(root).catalog_document()
    except InventoryError as exc:
        return DoctorItem(CATALOG_NAME, "warning", f"cannot be read: {exc}")
    detail = f"catalog_id {document.catalog_id}, restore_epoch {document.restore_epoch}"
    return DoctorItem(CATALOG_NAME, "info", detail)


def _current(root: Path, full: FullInventory) -> DoctorItem:
    """``CURRENT`` against the newest valid root the read protocol found by probing."""
    newest = full.root.seq
    try:
        named = decode_current((root / CURRENT_NAME).read_bytes()).seq
    except FileNotFoundError:
        return DoctorItem(CURRENT_NAME, "warning", f"missing; probing found version {newest}")
    except InventoryError as exc:
        return DoctorItem(CURRENT_NAME, "warning", f"cannot be read ({exc}); probing found version {newest}")
    if named == newest:
        return DoctorItem(CURRENT_NAME, "info", f"names version {named}, the newest valid version")
    if named < newest:
        detail = f"names version {named}, and probing found version {newest}; readers probe forward, so a lag is safe"
        return DoctorItem(CURRENT_NAME, "info", detail)
    detail = f"names version {named}, which is not valid; the newest valid version is {newest}"
    return DoctorItem(CURRENT_NAME, "warning", detail)


def _inventory(full: FullInventory) -> Iterator[DoctorItem]:
    """Root and leaf validation: each skipped root, each leaf that fails sdmxlib's checks."""
    for line in full.skipped:
        yield DoctorItem("inventory", "warning", f"skipped {line}")
    bad = 0
    for product_id, ref in sorted(full.root.products.items()):
        try:
            validate_leaf(full.leaf(product_id))
        except InventoryError as exc:
            bad += 1
            yield DoctorItem("inventory", "warning", f"{product_id}'s leaf {ref.leaf}: {exc}")
    if not full.skipped and not bad:
        detail = f"version {full.root.seq} and its {len(full.root.products)} leaves are valid"
        yield DoctorItem("inventory", "info", detail)


def _named(full: FullInventory | None, product_id: str) -> set[str]:
    """The edition ids the newest version names for *product_id*, in any state, or tombstones."""
    if full is None:
        return set()
    named = {t.edition_id for t in full.retired if t.product_id == product_id}
    if product_id in full.root.products:
        with contextlib.suppress(InventoryError):
            named |= {e.edition_id for e in full.entries(product_id)}
    return named


def _journal(product_dir: Path, product_id: str) -> tuple[list[DoctorItem], set[str]]:
    """The report lines for the product's seal journal, and the edition ids it holds entries for."""
    folder = product_dir / "journal"
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        return [], set()
    items: list[DoctorItem] = []
    entries: dict[str, JournalEntry] = {}
    try:
        entries = {entry.edition_id: entry for entry in read_journal(product_dir)}
    except ManifestError as exc:
        items.append(DoctorItem("journal", "warning", f"{product_id}: the journal cannot be read: {exc}"))
    for file in files:
        rel = f"products/{product_id}/journal/{file.name}"
        entry = entries.get(file.stem)
        if file.stat().st_size == 0:
            detail = f"{rel} is empty: a seal stopped before its entry reached the disk"
        elif entry is not None:
            detail = (
                f"{rel}: a seal of {entry.edition_id} started at {entry.sealed_at.isoformat(timespec='seconds')} "
                "and was not cleared; the product's next working_copy() settles it"
            )
        else:
            detail = f"{rel} cannot be read"
        items.append(DoctorItem("journal", "warning", detail))
    return items, {file.stem for file in files}


def _editions(product_dir: Path, product_id: str, full: FullInventory | None) -> Iterator[DoctorItem]:
    """Unsealed editions, the journal, and sealed editions the newest version neither names nor tombstones."""
    try:
        found: list[EditionInfo] = list_editions(product_dir)
    except ManifestError as exc:
        yield DoctorItem("unrecorded edition", "warning", f"{product_id}: an edition cannot be read: {exc}")
        found = []
    for info in found:
        if not info.sealed:
            detail = (
                f"{product_id}: unsealed edition {info.edition_id}, started "
                f"{info.created_at.isoformat(timespec='seconds')}; resume it with "
                "working_copy(resume=True), or discard() it"
            )
            yield DoctorItem("working copy", "info", detail)
    journal_items, journalled = _journal(product_dir, product_id)
    yield from journal_items
    named = _named(full, product_id)
    for info in found:
        if info.sealed and info.edition_id not in named:
            detail = f"{product_id}: sealed edition {info.edition_id} is named by no Inventory version"
            if info.edition_id in journalled:
                detail += "; its journal entry records it at the product's next working_copy()"
            yield DoctorItem("unrecorded edition", "warning", detail)


def _stale_items(root: Path, full: FullInventory) -> Iterator[DoctorItem]:
    """One ``stale data editions`` warning per stale data edition and URN."""
    stale, unread = _stale(full, InventoryWriter(root).artefacts)
    for finding in unread:
        yield DoctorItem("stale data editions", "warning", finding.detail)
    for item in stale:
        detail = str(item)
        if item.state == "embargoed":
            detail += "; its release is refused until then"
        yield DoctorItem("stale data editions", "warning", detail)


def _leftovers(root: Path) -> Iterator[DoctorItem]:
    """Files the Inventory writer staged and never linked or unlinked: in ``inventory/`` and the catalog root."""
    staged = sorted(root.glob(".*.tmp"))
    if (root / "inventory").is_dir():
        staged += sorted((root / "inventory").rglob("*.tmp"))
    for file in staged:
        detail = f"{file.relative_to(root).as_posix()}, staged by a writer that stopped; delete it when no writer runs"
        yield DoctorItem("leftover file", "warning", detail)


def doctor(path: str | Path) -> DoctorReport:
    """Report on a catalog for support. It writes nothing.

    The report covers, in order: a synced-folder path (Dropbox, OneDrive,
    iCloud Drive or Google Drive), the layout, ``catalog.json``, ``CURRENT``
    against the newest valid root, root and leaf validation, each stale data
    edition (specification §6.5 item 4, with the fix: republish the
    product), and for each
    product its unsealed editions with their start times, its seal journal
    entries (empty ones included), and its sealed editions that the newest
    version neither names nor tombstones. Last come files in ``inventory/``
    or the catalog root that a writer staged and left behind.

    Args:
        path: The directory.

    Returns:
        The report. A directory that is not a catalog gets a report on its
        layout only.
    """
    root = Path(path).absolute()
    items: list[DoctorItem] = []
    if (client := synced_folder(root)) is not None:
        detail = (
            f"{root} is in a {client} folder; a sync client can copy a catalog's files out of order, "
            "so keep catalogs on a local disk"
        )
        items.append(DoctorItem("synced folder", "warning", detail))
    layout, item = _layout(root)
    items.append(item)
    if layout != "catalog":
        return DoctorReport(root=root, layout=layout, items=tuple(items))
    items.append(_catalog_json(root))
    full: FullInventory | None = None
    try:
        full = InventoryWriter(root).full()
    except InventoryError as exc:
        items.append(DoctorItem("inventory", "warning", str(exc)))
    if full is None and not InventoryWriter(root).has_version():
        items.append(DoctorItem("inventory", "info", "no Inventory version yet; the first seal writes version 1"))
    elif full is not None:
        items += [_current(root, full), *_inventory(full), *_stale_items(root, full)]
    products = root / "products"
    product_dirs = sorted(p for p in products.iterdir() if p.is_dir()) if products.is_dir() else []
    for product_dir in product_dirs:
        items += _editions(product_dir, product_dir.name, full)
    items += _leftovers(root)
    return DoctorReport(root=root, layout=layout, items=tuple(items))

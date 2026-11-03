"""A product's one unsealed edition, and its seal (Phase 2a, Phase 3).

`Product.working_copy` starts or resumes a `WorkingCopy`. Ingest writes into
it through the write methods and `WorkingCopy.registry`, `WorkingCopy.view`
reads it back, and `WorkingCopy.seal` turns it into an immutable edition and
records it in the Inventory, live when the catalog's embargo is off.

A product is a ``data`` product or a ``structure`` product (`WorkingCopy.kind`).
A structure product holds full artefacts only, copied with
`WorkingCopy.fill_structures`, and its seal runs the publish check
(`sdmxlib.edition.check_edition` at ``publish``) against the catalog's other
structure products before the Inventory records the edition. A data product
is filled with `WorkingCopy.fill_dataflows`, which cuts each codelist to a
partial and records the full codelist's fingerprint in
``statcat._meta.partial_of``; the seal passes it to sdmxlib as
``partial_of``, and checks the edition's partials against the catalog's
structure editions the same way. The Inventory writer runs the cross-product
rules (`statcatlib.store.cross_product`) on every record, and the seal runs
them first as a pre-check, before anything is sealed.

**The journal rule.** sdmxlib's `seal` writes a journal entry,
``<product>/journal/<edition_id>.json``, before the manifest, and statcatlib
clears it once the edition is recorded. An entry left behind is a seal that a
crash may have stopped before the record. Every writer settles the catalog's
entries against the newest Inventory version when it starts a working copy,
when it resumes one, at the start of a seal and after its record
(`settle_journal`), recording every product's unrecorded editions in one
version:

| Journal entry | Action |
|---|---|
| The newest root names or tombstones the edition | Clear the entry |
| The edition has no ``manifest.json`` | Clear the entry; the edition is an unsealed working copy |
| It has a manifest and no root names it | Record it, then clear the entry |
| Recording raises ``duplicate`` with the same ``manifest_sha256`` | Treat as success; clear the entry |
| Recording raises any other `InventoryRuleError` | Clear the entry, log a WARNING, leave the edition |
| An edition fails its publish check | Clear the entry, log a WARNING, leave the edition |
| The cross-product rules refuse it | Clear the entry, log a WARNING, leave the edition |
| Its check was given a structure edition that is not recorded | Clear the entry, log a WARNING, leave the edition |
| `InventoryContentionError` | Keep the entry, so the next working copy records it |
| The entry cannot be read | Raise `JournalError` naming the file and ``statcat doctor`` |

Another product's entry is acted on only when its edition has a manifest and
the head records the product; a ``Catalog.seal`` batch leaves its other
products out. Each edition is recorded with the kind its manifest's
``statcat`` section names, ``data`` when it names none. A structure edition's publish check is
given the other structure products' live and embargoed editions, and each
other product's journalled structure edition in place of that product's; a
data edition whose partials record ``partial_of`` is checked with every
structure product's, chosen the same way.

A product has one writer at a time: a laptop convention, stated in the docs.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import sdmxlib
import sdmxlib.edition as _edition
from attrs import frozen
from sdmxlib import SdmxUrn
from sdmxlib.edition import MANIFEST_NAME, Finding, ManifestError, canonical_json, check_edition
from sdmxlib.inventory import FullInventory, Leaf, PartialRecord, TransitionError, head, record
from sdmxlib.storage.schema import SCHEMA_VERSION, meta_value

from statcatlib.bundle import STATCAT_SECTION, catalog_section, check_catalog, structure_content
from statcatlib.store import schema as _catalog_schema
from statcatlib.store.cross_product import ACTIVE, StoreArtefacts
from statcatlib.store.inventory import (
    Change,
    CrossProductRuleError,
    InventoryContentionError,
    InventoryRuleError,
    InventoryWriter,
    NonFastForwardError,
    producer,
)
from statcatlib.store.store import EditionView, _refuse_statcat_schema_mismatch, _Store

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Iterable, Mapping, Sequence
    from pathlib import Path

    import duckdb
    from sdmxlib import Dataflow, EditionRegistry, MaintainableArtefact, RestRegistry, WriteSource
    from sdmxlib.edition import JournalEntry, Lifecycle, Manifest
    from sdmxlib.inventory import Kind

    from statcatlib.model.dataset import Dataset
    from statcatlib.model.skos import Concept, ConceptScheme
    from statcatlib.store.cross_product import Artefacts
    from statcatlib.store.store import CodelistIndexPolicy, EmbeddingModel, ReferenceMetadataBlend

__all__ = [
    "Edition",
    "EditionCheckError",
    "JournalError",
    "Planned",
    "SealCheckError",
    "WorkingCopy",
    "WorkingCopyClosedError",
    "WorkingCopyExistsError",
    "precheck_refusals",
    "settle_journal",
]

log = logging.getLogger(__name__)


@frozen
class Edition:
    """A sealed edition, as the Inventory records it.

    Attributes:
        edition_id: The edition's id, a UUIDv7.
        product_id: Its product.
        previous: The edition it was built on, or ``None`` for the product's
            first.
        manifest_sha256: The SHA-256 of its ``manifest.json``.
    """

    edition_id: str
    product_id: str
    previous: str | None
    manifest_sha256: str


class WorkingCopyExistsError(Exception):
    """The product already has an unsealed edition; a product has one at a time."""


class WorkingCopyClosedError(RuntimeError):
    """The working copy was sealed or discarded, so neither it nor its views answer."""


class SealCheckError(Exception):
    """A seal check found something wrong, so the edition was not sealed.

    Attributes:
        findings: Everything the checks found.
    """

    findings: tuple[Finding, ...]

    def __init__(self, findings: Sequence[Finding]) -> None:
        """List the findings."""
        self.findings: tuple[Finding, ...] = tuple(findings)
        listed = "; ".join(str(f) for f in self.findings)
        super().__init__(
            f"the seal checks found {len(self.findings)} problem(s), so nothing was sealed and the working "
            f"copy stays open: {listed}"
        )


class EditionCheckError(Exception):
    """The publish check found something wrong with a sealed edition, so it was not recorded.

    The edition stays on disk, sealed and unrecorded, and ``statcat doctor``
    reports it. Fix the source, then start a new working copy from the live
    edition with ``working_copy()``.

    Attributes:
        findings: Everything `sdmxlib.edition.check_edition` found.
        product: The product.
        edition: The sealed edition.
    """

    findings: tuple[Finding, ...]
    product: str
    edition: str

    def __init__(self, findings: Sequence[Finding], *, product: str, edition: str) -> None:
        """List the findings, and name the product and the edition."""
        self.findings = tuple(findings)
        self.product = product
        self.edition = edition
        listed = "; ".join(str(f) for f in self.findings)
        super().__init__(
            f"{product}: the publish check of the sealed edition {edition} found {len(self.findings)} problem(s), "
            f"so it was not recorded and stays on disk unrecorded: {listed}. Fix the source, then start a new "
            "working copy from the live edition with working_copy()."
        )


class JournalError(Exception):
    """A seal journal entry cannot be read, so the product's seals cannot be settled."""


def manifest_sha256(edition_dir: Path) -> str:
    """The SHA-256 of *edition_dir*'s ``manifest.json``, lowercase hex."""
    return hashlib.sha256((edition_dir / MANIFEST_NAME).read_bytes()).hexdigest()


def edition_kind(manifest: Manifest) -> Kind:
    """The product kind a sealed edition's ``statcat`` section names; ``data`` when it names none."""
    section = manifest.extensions.get(STATCAT_SECTION) or {}
    return "structure" if section.get("kind") == "structure" else "data"


def cut_partials(manifest: Manifest) -> tuple[str, ...]:
    """The URNs of the partials a sealed edition's manifest records a ``partial_of`` for, sorted."""
    return tuple(sorted(a.urn for a in manifest.artefacts if a.partial and a.partial_of is not None))


def structure_editions(
    full: FullInventory | None, catalog_root: Path, *, product_id: str, batch: Mapping[str, Path]
) -> list[Path]:
    """The structure editions an edition of *product_id* is checked with (design decision O4).

    They are the live and embargoed editions of every other structure
    product in *full*, with each product of *batch* holding its batch
    edition in place of its recorded ones. For a data product that is every
    structure product's.

    Args:
        full: The Inventory version, or ``None`` before the first.
        catalog_root: The catalog directory.
        product_id: The product whose edition is checked; its own editions
            are left out.
        batch: Other products' sealed, unrecorded structure editions, by
            product.

    Returns:
        The edition directories, possibly none.
    """
    found = [edition_dir for other, edition_dir in sorted(batch.items()) if other != product_id]
    if full is None:
        return found
    for other in full.products():
        if other == product_id or other in batch:
            continue
        leaf = full.leaf(other)
        if leaf.kind != "structure":
            continue
        found += [
            catalog_root / "products" / other / entry.edition_id
            for entry in leaf.editions
            if entry.state in {"live", "embargoed"}
        ]
    return found


def publish_check(
    edition_dir: Path, *, product_id: str, structures: Sequence[Path], partials: Sequence[str] = ()
) -> None:
    """Seal step 5b: `sdmxlib.edition.check_edition` at ``publish`` on a sealed edition, with `check_catalog`.

    Args:
        edition_dir: The sealed edition.
        product_id: Its product.
        structures: The structure editions its references and partials
            resolve in (`structure_editions`); ``structures=None`` when empty,
            since sdmxlib refuses an empty sequence.
        partials: The partials the edition records a ``partial_of`` for
            (`cut_partials`). With no *structures*, sdmxlib would skip their
            check, so each is a finding.

    Raises:
        EditionCheckError: The check found something.
    """
    report = check_edition(
        edition_dir,
        at="publish",
        structures=list(structures) or None,
        extensions={STATCAT_SECTION: check_catalog},
    )
    findings = list(report.findings)
    if not structures:
        findings += [
            Finding(
                "partials",
                urn,
                "the catalog has no live or embargoed structure edition to check this partial against; seal the "
                "structure product that holds its full codelist first",
            )
            for urn in partials
        ]
    if findings:
        raise EditionCheckError(findings, product=product_id, edition=edition_dir.name)


def _read_journal(product_dir: Path) -> list[JournalEntry]:
    try:
        return _edition.read_journal(product_dir)
    except ManifestError as exc:
        msg = f"{exc}. Run `statcat doctor` on the catalog to see the product's seals."
        raise JournalError(msg) from exc


def _named(full: FullInventory | None, product_id: str, edition_id: str) -> bool:
    """Whether the version *full* holds names *edition_id* in any state, or tombstones it."""
    if full is None:
        return False
    if any(t.product_id == product_id and t.edition_id == edition_id for t in full.retired):
        return True
    if product_id not in full.products():
        return False
    return any(e.edition_id == edition_id for e in full.entries(product_id))


def record_journalled(
    leaf: Leaf | None,
    entries: Sequence[tuple[JournalEntry, str, str, Kind]],
    *,
    product_id: str,
    lifecycle: Lifecycle,
    embargo: bool,
    retired: Iterable[str],
    now: datetime,
) -> tuple[Leaf | None, list[str], dict[str, TransitionError]]:
    """Record each journalled edition in *leaf*, by the journal rule.

    Args:
        leaf: The product's leaf in the head, or ``None``.
        entries: Each journal entry with its edition's ``manifest_sha256``,
            ``schema_version`` and product kind.
        product_id: The product.
        lifecycle: The product's lifecycle, for a first record.
        embargo: The head's embargo setting.
        retired: The edition ids the head tombstones for the product.
        now: The record time.

    Returns:
        The next leaf (``None`` when nothing changed), the edition ids that are
        settled (recorded now, or recorded already with the same
        ``manifest_sha256``), and each refused edition's refusal.
    """
    current = leaf
    changed = False
    settled: list[str] = []
    refused: dict[str, TransitionError] = {}
    gone = set(retired)
    for entry, sha, schema_version, kind in entries:
        try:
            current = record(
                current,
                product_id=product_id,
                kind=kind,
                lifecycle=current.lifecycle if current is not None else lifecycle,
                edition_id=entry.edition_id,
                previous=entry.previous,
                manifest_sha256=sha,
                schema_version=schema_version,
                embargo=embargo,
                now=now,
                retired=gone,
            )
        except TransitionError as exc:
            held = next((e for e in (current.editions if current else ()) if e.edition_id == entry.edition_id), None)
            if exc.rule == "duplicate" and held is not None and held.manifest_sha256 == sha:
                settled.append(entry.edition_id)
            else:
                refused[entry.edition_id] = exc
            continue
        changed = True
        settled.append(entry.edition_id)
    return (current if changed else None), settled, refused


def settle_journal(
    writer: InventoryWriter,
    product_dir: Path,
    product_id: str,
    lifecycle: Lifecycle,
    *,
    exclude: Collection[str] = (),
) -> None:
    """Apply the journal rule to every product's journal entries (see the module docstring).

    The repair is catalog-wide: every edition that needs recording, of any
    product, goes into one Inventory commit, each with the kind its manifest
    names, and the cross-product rules run on that version, so a structure
    edition and a data edition sealed in one crashed batch are recorded
    together. Another product's entry is acted on only when its edition has
    a manifest and the head does not name it, and only when the head records
    the product: a product whose first edition is journalled is left to its
    own next ``working_copy()``, which knows its lifecycle. A structure
    edition, and a data edition whose partials record a ``partial_of``, is
    recorded only once its publish check finds nothing; an edition whose
    check was given a journalled structure edition that is not recorded is
    left unrecorded with it.

    Args:
        writer: The catalog's Inventory writer.
        product_dir: The product's directory.
        product_id: The product.
        lifecycle: The lifecycle to record the product's first edition with.
        exclude: Other products to leave alone: the products of the
            `Catalog.seal` batch running, whose sealed editions the batch
            records itself.

    An entry of the product that cannot be read raises `JournalError`;
    another product's unreadable journal is left to that product. A record
    that loses too many races raises `InventoryContentionError`, and the
    entries are kept for the next working copy.
    """
    products_dir = product_dir.parent
    own = _read_journal(product_dir)
    others = _other_journals(products_dir, product_id, exclude)
    if not own and not others:
        return
    full = writer.full()
    pending: dict[str, list[_Journalled]] = {}
    mine = _pending_entries(full, product_dir, product_id, own)
    if mine:
        pending[product_id] = mine
    for other, entries in others.items():
        if full is None or other not in full.products():
            continue
        found = _journalled(full, products_dir / other, other, entries, clear=False)
        if found:
            pending[other] = found
    if not pending:
        return
    lifecycles = {
        other: full.leaf(other).lifecycle if full is not None and other in full.products() else lifecycle
        for other in pending
    }
    repair = _Repair(writer, full, pending, lifecycles=lifecycles)
    repair.commit()
    for other, items in pending.items():
        for entry, _, _, _ in items:
            why = repair.why(other, entry.edition_id)
            if why is not None:
                log.warning(
                    "%s: the journalled edition %s cannot be recorded (%s); it stays on disk unrecorded, and "
                    "`statcat doctor` reports it",
                    other,
                    entry.edition_id,
                    why,
                )
            _edition.clear_journal(products_dir / other, entry.edition_id)


type _Journalled = tuple[JournalEntry, str, str, Kind]
"""A journal entry with its edition's ``manifest_sha256``, ``schema_version`` and product kind."""


def _other_journals(products_dir: Path, product_id: str, exclude: Collection[str]) -> dict[str, list[JournalEntry]]:
    """The journal entries of every product but *product_id* and *exclude*; an unreadable journal is skipped."""
    found: dict[str, list[JournalEntry]] = {}
    for other_dir in sorted(products_dir.iterdir()) if products_dir.is_dir() else ():
        other = other_dir.name
        if other == product_id or other in exclude or not (other_dir / "journal").is_dir():
            continue
        with contextlib.suppress(ManifestError):
            entries = _edition.read_journal(other_dir)
            if entries:
                found[other] = entries
    return found


class _Repair:
    """The journal repair's checks and its one Inventory commit, over every product's journalled editions."""

    def __init__(
        self,
        writer: InventoryWriter,
        full: FullInventory | None,
        pending: Mapping[str, Sequence[_Journalled]],
        *,
        lifecycles: Mapping[str, Lifecycle],
    ) -> None:
        self._writer = writer
        self._pending = pending
        self._lifecycles = lifecycles
        self._failed: dict[tuple[str, str], Exception] = {}
        self._refused: dict[tuple[str, str], Exception] = {}
        self._given: dict[tuple[str, str], frozenset[str]] = {}
        self._structures = self._journalled_structures()
        self._check(full)

    def why(self, product_id: str, edition_id: str) -> Exception | None:
        """Why the edition was not recorded, or ``None`` when it was."""
        key = (product_id, edition_id)
        return self._failed.get(key) or self._refused.get(key)

    def _journalled_structures(self) -> dict[str, tuple[str, Path]]:
        """Each product's newest journalled structure edition, by product: its id and directory."""
        found: dict[str, tuple[str, Path]] = {}
        for product_id, items in self._pending.items():
            for entry, _, _, kind in items:
                newest = found.get(product_id)
                if kind == "structure" and (newest is None or newest[0] < entry.edition_id):
                    found[product_id] = (entry.edition_id, self._dir(product_id, entry.edition_id))
        return found

    def _dir(self, product_id: str, edition_id: str) -> Path:
        return self._writer.root / "products" / product_id / edition_id

    def _check(self, full: FullInventory | None) -> None:
        """The publish check of each journalled structure edition and each data edition with ``partial_of``."""
        for product_id, items in self._pending.items():
            for entry, _, _, kind in items:
                edition_dir = self._dir(product_id, entry.edition_id)
                partials = cut_partials(_edition.read_manifest(edition_dir))
                if kind != "structure" and not partials:
                    continue
                batch = {other: found[1] for other, found in self._structures.items() if other != product_id}
                structures = structure_editions(full, self._writer.root, product_id=product_id, batch=batch)
                try:
                    publish_check(edition_dir, product_id=product_id, structures=structures, partials=partials)
                except EditionCheckError as exc:
                    self._failed[product_id, entry.edition_id] = exc
                else:
                    self._given[product_id, entry.edition_id] = frozenset(batch)

    def _lost(self) -> dict[str, Exception]:
        """Each product whose journalled structure edition was refused or failed its check, with why."""
        lost: dict[str, Exception] = {}
        for product_id, (edition_id, _) in self._structures.items():
            why = self.why(product_id, edition_id)
            if why is not None:
                lost[product_id] = why
        return lost

    def _change(self, head_now: FullInventory | None) -> Change:
        self._refused.clear()
        leaves: dict[str, Leaf] = {}
        for product_id, items in self._pending.items():
            recordable = [item for item in items if (product_id, item[0].edition_id) not in self._failed]
            if not recordable:
                continue
            leaf = head_now.leaf(product_id) if head_now is not None and product_id in head_now.products() else None
            retired = [t.edition_id for t in head_now.retired if t.product_id == product_id] if head_now else []
            nxt, _, found = record_journalled(
                leaf,
                recordable,
                product_id=product_id,
                lifecycle=self._lifecycles[product_id],
                embargo=head_now.embargo if head_now is not None else False,
                retired=retired,
                now=datetime.now(UTC),
            )
            for edition_id, exc in found.items():
                self._refused[product_id, edition_id] = exc
            if nxt is not None:
                leaves[product_id] = nxt
        self._drop(head_now, leaves)
        if not leaves:
            raise _NothingToRepairError
        return Change(leaves=leaves)

    def _drop(self, head_now: FullInventory | None, leaves: dict[str, Leaf]) -> None:
        """Drop each product the cross-product rules refuse, and each whose check was given a lost one."""
        while True:
            dropping: dict[str, Exception] = dict(self._writer.refusals(head_now, leaves))
            lost = self._lost()
            for (product_id, edition_id), given in self._given.items():
                gone = sorted(given & set(lost))
                if product_id in leaves and gone and product_id not in dropping:
                    lost_id = self._structures[gone[0]][0]
                    finding = Finding(
                        "structures",
                        f"{gone[0]}/{lost_id}",
                        f"the publish check was given this journalled edition, which was not recorded "
                        f"({lost[gone[0]]})",
                    )
                    dropping[product_id] = EditionCheckError([finding], product=product_id, edition=edition_id)
            if not dropping:
                return
            for product_id, exc in dropping.items():
                del leaves[product_id]
                for entry, _, _, _ in self._pending[product_id]:
                    self._refused.setdefault((product_id, entry.edition_id), exc)

    def commit(self) -> None:
        """Record every journalled edition the checks and the rules accept, in one Inventory commit."""
        try:
            self._writer.commit(
                self._change, kind="seal", reason="record a seal its writer did not record (journal repair)"
            )
        except _NothingToRepairError:
            pass
        except InventoryContentionError as exc:
            msg = f"{exc}; the journalled edition is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc


class _NothingToRepairError(Exception):
    """The journal repair has no edition to record, so it writes no Inventory version."""


def _pending_entries(
    full: FullInventory | None, product_dir: Path, product_id: str, entries: Iterable[JournalEntry]
) -> list[_Journalled]:
    """The journal entries whose sealed edition *full* does not name, with what records each; clears the rest.

    Args:
        full: The newest Inventory version, or ``None`` before the first.
        product_dir: The product's directory.
        product_id: The product.
        entries: Its journal entries.

    Returns:
        Each entry with its edition's ``manifest_sha256``, ``schema_version``
        and product kind.
    """
    return _journalled(full, product_dir, product_id, entries, clear=True)


def _journalled(
    full: FullInventory | None, product_dir: Path, product_id: str, entries: Iterable[JournalEntry], *, clear: bool
) -> list[_Journalled]:
    """The entries whose edition has a manifest and that *full* does not name; with *clear*, clears the others."""
    pending: list[_Journalled] = []
    for entry in entries:
        edition_dir = product_dir / entry.edition_id
        if _named(full, product_id, entry.edition_id) or not (edition_dir / MANIFEST_NAME).is_file():
            if clear:
                _edition.clear_journal(product_dir, entry.edition_id)
            continue
        manifest = _edition.read_manifest(edition_dir)
        pending.append((entry, manifest_sha256(edition_dir), manifest.schema_version, edition_kind(manifest)))
    return pending


def _as_kind(value: object) -> Kind | None:
    """*value* as a product kind, or ``None`` when it names none."""
    if value == "structure":
        return "structure"
    if value == "data":
        return "data"
    return None


def _settle_kind(
    cursor: duckdb.DuckDBPyConnection, product_id: str, edition_id: str, wanted: Kind | None, *, resume: bool
) -> Kind:
    """`WorkingCopy._start`'s fourth step: the working copy's kind, recorded in ``statcat._meta.product_kind``.

    A new edition takes *wanted*, or ``data``, and drops the ``partial_of``
    and ``fill_in_progress`` keys the copied ``metadata.duckdb`` carries from
    the previous edition. A resumed one keeps the kind it was started with.

    Args:
        cursor: A cursor on the edition's ``statcat`` schema.
        product_id: The product.
        edition_id: The edition.
        wanted: The leaf's kind, or the caller's ``kind``; ``None`` for
            neither.
        resume: Whether the edition is resumed.

    Returns:
        The kind.

    Raises:
        InventoryRuleError: A resumed edition was started with another kind
            (``rule="kind"``).
    """
    resolved: Kind = wanted or "data"
    if resume:
        row = _catalog_schema.first_row(cursor.execute("SELECT value FROM _meta WHERE key = 'product_kind'").fetchall())
        stored = None if row is None else _as_kind(row[0])
        if stored is not None and wanted is not None and stored != wanted:
            msg = (
                f"{product_id}'s working copy {edition_id} was started as a {stored} product, and "
                f"working_copy() asked for kind={wanted!r}; resume it with kind={stored!r}, or discard it"
            )
            raise InventoryRuleError(msg, rule="kind", product=product_id, edition=edition_id)
        resolved = wanted or stored or "data"
    else:
        cursor.execute("DELETE FROM _meta WHERE key IN ('partial_of', 'fill_in_progress')")
    cursor.execute("INSERT OR REPLACE INTO _meta VALUES ('product_kind', $kind)", {"kind": resolved})
    return resolved


#: The ``statcat._meta`` key holding the cuts `WorkingCopy.fill_dataflows` made: JSON, URN to fingerprint (D2).
PARTIAL_OF_KEY = "partial_of"
#: The ``statcat._meta`` key `WorkingCopy.fill_dataflows` holds while sdmxlib fills (D3).
FILL_IN_PROGRESS_KEY = "fill_in_progress"


def _meta_text(cursor: duckdb.DuckDBPyConnection, key: str) -> str | None:
    """The value ``statcat._meta`` holds for *key*, or ``None``."""
    row = _catalog_schema.first_row(cursor.execute("SELECT value FROM _meta WHERE key = $key", {"key": key}).fetchall())
    return None if row is None else str(row[0])


def recorded_cuts(cursor: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """The ``statcat._meta.partial_of`` map: the fingerprint of the full codelist each cut was made from, by URN."""
    text = _meta_text(cursor, PARTIAL_OF_KEY)
    if text is None:
        return {}
    loaded: dict[str, str] = json.loads(text)
    return loaded


def _held_partials(cursor: duckdb.DuckDBPyConnection) -> set[str]:
    """The URNs of the partial artefacts the edition's store holds."""
    return {urn for (urn,) in cursor.execute("SELECT urn FROM sdmx.artefact WHERE is_partial").fetchall()}


def _fingerprint(artefact: object) -> str:
    """An artefact's fingerprint, as sdmxlib's manifest records it: the SHA-256 of its canonical text."""
    return hashlib.sha256(canonical_json(artefact)).hexdigest()


def unvouched_partials(
    cursor: duckdb.DuckDBPyConnection, registry: EditionRegistry, previous: Path | None
) -> list[Finding]:
    """The data pre-check: the partials whose ``partial_of`` the seal cannot vouch for.

    A finding names a fill that stopped before it recorded its cuts
    (``statcat._meta.fill_in_progress`` is set), and each partial the
    ``partial_of`` map does not name whose content differs from the partial
    the previous edition records a ``partial_of`` for: sdmxlib's seal would
    carry that ``partial_of`` to a partial cut from something else. A partial
    with no ``partial_of`` in either place is recorded without one.

    Args:
        cursor: A cursor on the working copy's ``statcat`` schema.
        registry: The working copy's registry.
        previous: The previous edition's directory, or ``None``.

    Returns:
        The findings, possibly none.
    """
    findings: list[Finding] = []
    if _meta_text(cursor, FILL_IN_PROGRESS_KEY) is not None:
        findings.append(
            Finding(
                "partials",
                f"statcat._meta.{FILL_IN_PROGRESS_KEY}",
                "a fill_dataflows call stopped before it recorded the codelists it cut; call fill_dataflows "
                "again with the same dataflows",
            )
        )
    if previous is None:
        return findings
    known = recorded_cuts(cursor)
    earlier = {a.urn: a for a in _edition.read_manifest(previous).artefacts if a.partial and a.partial_of is not None}
    for urn in sorted(_held_partials(cursor) - set(known)):
        entry = earlier.get(urn)
        if entry is None:
            continue
        held = registry.get(SdmxUrn.parse(urn), eager=True)
        if held is None or _fingerprint(held) != entry.fingerprint:
            findings.append(
                Finding(
                    "partials",
                    urn,
                    f"the partial was cut again outside fill_dataflows, and the previous edition records it as cut "
                    f"from {entry.partial_of}, which the seal would carry; fill it with fill_dataflows",
                )
            )
    return findings


@frozen
class _Sealed:
    """An edition sealed by sdmxlib and not yet recorded, with the store schema its manifest names.

    Attributes:
        edition: The edition.
        schema_version: Its store schema.
        partials: The partials its manifest records a ``partial_of`` for.
    """

    edition: Edition
    schema_version: str
    partials: tuple[str, ...] = ()


@frozen
class Planned:
    """One edition as the cross-product pre-check plans it: its product's provisional leaf, and how to read it.

    Attributes:
        product_id: The product.
        edition_id: The edition.
        leaf: The product's leaf with the edition recorded, as the seal's
            record would write it on the head.
        read: Computes the edition's artefacts from a working copy's open
            store (`StoreArtefacts`); ``None`` for a sealed edition, whose
            manifest the writer's reader reads.
    """

    product_id: str
    edition_id: str
    leaf: Leaf
    read: Callable[[], Mapping[str, PartialRecord]] | None = None


def precheck_refusals(
    writer: InventoryWriter,
    full: FullInventory | None,
    planned: Sequence[Planned],
    fixed: Sequence[Planned] = (),
) -> dict[str, CrossProductRuleError]:
    """The seal's cross-product pre-check: the rules over *planned* and *fixed* together, before any of *planned* seals.

    Each refused product of *planned* is dropped, and the rules run again on
    the rest until a pass refuses none, since a dropped edition can be the
    one another was accepted with. *fixed* holds editions the caller keeps,
    such as a batch's sealed structure editions.

    Args:
        writer: The catalog's Inventory writer.
        full: The head the pre-check runs on.
        planned: The working copies to check.
        fixed: Other editions of the version, checked with them.

    Returns:
        Each refused product of *planned*, with its error.
    """
    reader: Artefacts = writer.artefacts
    for one in [*fixed, *planned]:
        if one.read is not None:
            reader = StoreArtefacts(one.edition_id, one.read, fallback=reader)
    leaves = {one.product_id: one.leaf for one in [*fixed, *planned]}
    checked = {one.product_id for one in planned}
    refused: dict[str, CrossProductRuleError] = {}
    while True:
        found = {
            product_id: exc
            for product_id, exc in writer.refusals(full, leaves, artefacts=reader).items()
            if product_id in checked
        }
        if not found:
            return refused
        for product_id, exc in found.items():
            refused[product_id] = exc
            checked.discard(product_id)
            del leaves[product_id]


@frozen
class _Parts:
    """What `WorkingCopy._start` hands its constructor."""

    store: _Store
    registry: sdmxlib.LocalRegistry
    writer: InventoryWriter
    product_id: str
    previous: str | None
    lifecycle: Lifecycle
    kind: Kind


def _live_head(writer: InventoryWriter, product_id: str) -> str | None:
    """The product's live edition at this sdmxlib's store schema, or ``None``."""
    full = writer.full()
    if full is None or product_id not in full.products():
        return None
    entry = head(full.leaf(product_id), SCHEMA_VERSION)
    return None if entry is None else entry.edition_id


class WorkingCopy:
    """A product's one unsealed edition, open for writing.

    `Product.working_copy` returns one. Write datasets with `put` and the
    other write methods, and SDMX structures and observations through
    `registry`; read them back through `view`; then `seal` it, or `discard` it.
    Once sealed or discarded, every method on it, and on its views, raises
    `WorkingCopyClosedError`.
    """

    #: The relationship values `add_lineage` accepts (``prov:wasDerivedFrom`` family).
    LINEAGE_RELATIONSHIPS: tuple[str, ...] = _Store.LINEAGE_RELATIONSHIPS
    #: The confidence values `add_lineage` accepts.
    LINEAGE_CONFIDENCE: tuple[str, ...] = _Store.LINEAGE_CONFIDENCE

    _store: _Store
    _registry: sdmxlib.LocalRegistry
    _writer: InventoryWriter
    _product_dir: Path
    _product_id: str
    _edition_id: str
    _previous: str | None
    _lifecycle: Lifecycle
    _kind: Kind
    _closed: str | None
    _rebuild_index: Callable[[], int] | None

    def __init__(self, *args: object, _parts: _Parts | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError: `Product.working_copy` returns working copies."""
        if _parts is None or args or kwargs:
            msg = "start a working copy with Catalog.create(path).product(id).working_copy()"
            raise TypeError(msg)
        self._store = _parts.store
        self._registry = _parts.registry
        self._writer = _parts.writer
        self._product_dir = _parts.registry.edition_dir.parent
        self._product_id = _parts.product_id
        self._edition_id = _parts.registry.edition_dir.name
        self._previous = _parts.previous
        self._lifecycle = _parts.lifecycle
        self._kind = _parts.kind
        self._closed = None
        self._rebuild_index = None

    @classmethod
    def _start(
        cls,
        writer: InventoryWriter,
        product_id: str,
        *,
        lifecycle: Lifecycle,
        kind: Kind | None,
        resume: bool,
        embedding_model: EmbeddingModel | None,
        document_prefix: str,
        query_prefix: str,
    ) -> Self:
        """Start or resume the product's working copy: repair, find the base, build, set up the schema.

        `_build` raises `WorkingCopyExistsError` for a product with an
        unsealed edition when *resume* is false, and `FileNotFoundError` for
        one without when it is true.

        Args:
            writer: The catalog's Inventory writer.
            product_id: The product.
            lifecycle: The product's lifecycle.
            kind: The product's kind; ``None`` takes the recorded kind, or
                ``data`` for a new product.
            resume: Reopen the unsealed edition in place of starting one.
            embedding_model: The encoder the seal embeds with.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.

        Returns:
            The working copy.

        Raises:
            InventoryRuleError: The product is recorded with another lifecycle
                (``rule="lifecycle"``), or recorded or resumed with another
                kind (``rule="kind"``).
        """
        product_dir = writer.root / "products" / product_id
        settle_journal(writer, product_dir, product_id, lifecycle)
        full = writer.full()
        leaf = full.leaf(product_id) if full is not None and product_id in full.products() else None
        if leaf is not None and leaf.lifecycle != lifecycle:
            msg = (
                f"{product_id} is recorded as a {leaf.lifecycle} product, and working_copy() asked for "
                f"{lifecycle}; changing a product's lifecycle is not supported before Phase 4"
            )
            raise InventoryRuleError(msg, rule="lifecycle", product=product_id, edition=None)
        if leaf is not None and kind is not None and leaf.kind != kind:
            msg = (
                f"{product_id} is recorded as a {leaf.kind} product, and working_copy() asked for kind={kind!r}; "
                "a product keeps the kind it was first recorded with"
            )
            raise InventoryRuleError(msg, rule="kind", product=product_id, edition=None)
        wanted: Kind | None = leaf.kind if leaf is not None else kind
        live = head(leaf, SCHEMA_VERSION)
        registry = _build(
            writer, product_dir, product_id, lifecycle, base=None if live is None else live.edition_id, resume=resume
        )
        edition_dir = registry.edition_dir
        opened = False
        try:
            previous = _previous_of(registry)
            if previous is None:
                # A product's first edition: the statcat tables are created here.
                # An edition built on another arrives with them, in the copied
                # metadata.duckdb. A resumed first edition holds them already, so
                # the version guard runs before any DDL; every statement is IF
                # NOT EXISTS.
                _refuse_statcat_schema_mismatch(registry.connection.cursor())
                _catalog_schema.create_schema(registry.connection, registry.catalog_name).close()
            cursor = _catalog_schema.use_statcat(registry.connection.cursor(), registry.catalog_name)
            try:
                resolved = _settle_kind(cursor, product_id, edition_dir.name, wanted, resume=resume)
            finally:
                cursor.close()
            store = _Store._over(
                registry.connection.cursor,
                catalog_name=registry.catalog_name,
                search_dir=edition_dir / "search",
                writable=True,
                registry=registry,
                embedding_model=embedding_model,
                document_prefix=document_prefix,
                query_prefix=query_prefix,
            )
            opened = True
        finally:
            if not opened:
                registry.close()
                if not resume:
                    # The edition this call started holds nothing yet; leave none behind.
                    _edition.discard(edition_dir)
        return cls(
            _parts=_Parts(
                store=store,
                registry=registry,
                writer=writer,
                product_id=product_id,
                previous=previous,
                lifecycle=lifecycle,
                kind=resolved,
            )
        )

    # -- state -----------------------------------------------------------

    def _live(self) -> _Store:
        """The store, or `WorkingCopyClosedError` once the working copy is sealed or discarded."""
        if self._closed is not None:
            raise WorkingCopyClosedError(self._closed)
        return self._store

    def _close(self, why: str) -> None:
        """Close the store and the build registry; every later call raises `WorkingCopyClosedError`."""
        self._closed = why
        self._store._close(lambda: WorkingCopyClosedError(self._closed or why))
        self._registry.close()

    @property
    def product_id(self) -> str:
        """The product this working copy belongs to."""
        return self._product_id

    @property
    def edition_id(self) -> str:
        """The id of the edition it seals into, a UUIDv7."""
        return self._edition_id

    @property
    def previous(self) -> str | None:
        """The edition it was built on, or ``None`` for the product's first."""
        return self._previous

    @property
    def kind(self) -> Kind:
        """The product's kind: ``data``, or ``structure`` for a product of full artefacts only."""
        return self._kind

    @property
    def registry(self) -> sdmxlib.LocalRegistry:
        """The writable sdmxlib registry over the edition: SDMX structures and observations.

        ``wc.registry.ingest(source)`` adds structures, and
        ``wc.registry.write_observations(...)`` writes observations into the
        edition's lake.
        """
        self._live()
        return self._registry

    def view(self) -> EditionView:
        """A read-only view of this working copy, which sees its writes as they are made."""
        return EditionView._over(self._live())

    # -- writes ----------------------------------------------------------

    def fill_structures(
        self,
        source: EditionRegistry,
        *,
        families: Collection[type[MaintainableArtefact]] = (),
        agency: str | None = None,
        ids: Collection[str] = (),
        artefacts: Collection[SdmxUrn[MaintainableArtefact] | str] = (),
    ) -> tuple[str, ...]:
        """Copy full artefacts from *source* into a structure product's working copy.

        It calls `sdmxlib.edition.fill_structures`. An artefact is selected
        when *artefacts* names its URN, or when its class is one of
        *families*, its agency is *agency* (any when ``None``) and its id
        matches one of the *ids* patterns (``SGC*``; any id when empty).
        Nothing is added for what a selected artefact references, so a
        reference into another structure product stays open; the seal's
        publish check resolves it there. An artefact the working copy holds at
        a selected URN is replaced.

        Args:
            source: The registry holding the full artefacts, such as a
                scratch `sdmxlib.LocalRegistry` an FMR's structures were
                ingested into.
            families: The artefact classes to select, such as
                `sdmxlib.Codelist` or `sdmxlib.ConceptScheme`.
            agency: The maintenance agency of the artefacts *families* selects.
            ids: Patterns the id of an artefact *families* selects must match.
            artefacts: More artefacts, by URN.

        Returns:
            The URNs copied, sorted.

        Raises:
            TypeError: The working copy is a data product's.

        sdmxlib raises `TypeError` for a family that is `Dataflow`,
        `DataflowBinding` or a class the store does not hold, and
        `ValueError` for a URN that names a dataflow, a binding, a partial or
        nothing *source* holds, or when nothing is selected.
        """
        self._live()
        if self._kind != "structure":
            msg = (
                f"{self._product_id} is a {self._kind} product, and fill_structures fills a structure product: "
                "start one with working_copy(kind='structure')"
            )
            raise TypeError(msg)
        return _edition.fill_structures(
            self._registry, source, families=families, agency=agency, ids=ids, artefacts=artefacts
        )

    def fill_dataflows(
        self,
        source: RestRegistry | EditionRegistry,
        dataflows: Sequence[SdmxUrn[Dataflow] | str],
        *,
        observations: Mapping[str, WriteSource] | None = None,
        partials: bool = True,
    ) -> dict[str, str]:
        """Add dataflows' closures and observations to a data product's working copy, and cut its codelists.

        It calls `sdmxlib.edition.fill_edition`, which keeps what the working
        copy holds: on a republish the other dataflows' bindings stay, and a
        codelist they share is cut to the codes every binding uses. The map
        it returns is merged into ``statcat._meta.partial_of``, a later
        fill's value winning for a URN, so a seal after
        ``working_copy(resume=True)`` in another process records each
        partial's ``partial_of``. ``statcat._meta.fill_in_progress`` is set
        while sdmxlib fills; a fill that stops before the merge leaves it, and
        the seal refuses the working copy until a fill completes.

        Fill a data product through this method: a direct
        ``fill_edition(wc.registry, ...)`` leaves its cuts out of the map.

        Args:
            source: Where the structures are: a `sdmxlib.RestRegistry`, or a
                sealed edition's or a scratch `sdmxlib.EditionRegistry`.
            dataflows: The dataflows, by URN. A ``latest`` URN is filled at
                the version it resolves to.
            observations: The rows of each dataflow that has any, by URN
                string, in any form `sdmxlib.LocalRegistry.write_observations`
                reads.
            partials: Whether to cut codelists to partials.

        Returns:
            The fingerprint of the full codelist each partial cut here was
            cut from, by URN.

        Raises:
            TypeError: The working copy is a structure product's.

        sdmxlib raises `TypeError` for a URN that names no dataflow at
        *source*, and `ValueError` for a dataflow with no data structure, for
        *observations* naming a dataflow not in *dataflows*, and for a coded
        column holding a value outside its codelist.
        """
        store = self._live()
        if self._kind != "data":
            msg = (
                f"{self._product_id} is a {self._kind} product, and fill_dataflows fills a data product; a "
                "structure product is filled with fill_structures"
            )
            raise TypeError(msg)
        urns = [sdmxlib.Dataflow.parse_urn(given) if isinstance(given, str) else given for given in dataflows]
        cursor = store._cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO _meta VALUES ($key, $at)",
            {"key": FILL_IN_PROGRESS_KEY, "at": datetime.now(UTC).isoformat()},
        )
        cut = _edition.fill_edition(self._registry, source, urns, observations=observations, partials=partials)
        with store.transaction():
            merged = recorded_cuts(cursor) | cut
            cursor.execute(
                "INSERT OR REPLACE INTO _meta VALUES ($key, $map)",
                {"key": PARTIAL_OF_KEY, "map": json.dumps(merged, sort_keys=True)},
            )
            cursor.execute("DELETE FROM _meta WHERE key = $key", {"key": FILL_IN_PROGRESS_KEY})
        return cut

    def put(self, obj: Dataset | ConceptScheme | Sequence[Dataset | ConceptScheme]) -> None:
        """Add or update a dataset, a concept scheme, or a sequence of either.

        Args:
            obj: What to write.
        """
        self._live().put(obj)

    def put_concept(self, scheme_uri: str, concept: Concept) -> None:
        """Add or update one concept of a scheme.

        Args:
            scheme_uri: The scheme's URI.
            concept: The concept.
        """
        self._live().put_concept(scheme_uri, concept)

    def add_lineage(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        *,
        activity_label: str | None = None,
        activity_type: str | None = None,
        agent_id: str | None = None,
        timestamp: datetime | None = None,
        confidence: str = "asserted",
        notes: str | None = None,
    ) -> None:
        """Record a PROV-O-aligned lineage edge between two datasets.

        Args:
            source_id: The source dataset.
            target_id: The target dataset.
            relationship: One of `LINEAGE_RELATIONSHIPS`.
            activity_label: The activity's label.
            activity_type: The activity's type.
            agent_id: The agent responsible.
            timestamp: When.
            confidence: One of `LINEAGE_CONFIDENCE`.
            notes: Free text.
        """
        self._live().add_lineage(
            source_id,
            target_id,
            relationship,
            activity_label=activity_label,
            activity_type=activity_type,
            agent_id=agent_id,
            timestamp=timestamp,
            confidence=confidence,
            notes=notes,
        )

    def add_language_alternates(self, pairs: Mapping[str, Sequence[str]], *, source: str) -> None:
        """Record language tags a source declares as alternates of a primary tag.

        Args:
            pairs: Each primary tag mapped to its alternate tags.
            source: Where the pairs come from.
        """
        self._live().add_language_alternates(pairs, source=source)

    @contextlib.contextmanager
    def bulk_load(self) -> Generator[Self]:
        """Hold DuckDB's write-ahead log until the block ends, then checkpoint once."""
        with self._live().bulk_load():
            yield self

    @contextlib.contextmanager
    def transaction(self) -> Generator[Self]:
        """Wrap the writes in the block in one DuckDB transaction."""
        with self._live().transaction():
            yield self

    def build_index(
        self,
        embedding_model: EmbeddingModel | None = None,
        progress: Callable[[int, int], None] | None = None,
        *,
        include_codes: bool = True,
        embed_codes: bool = False,
        max_codes_per_codelist: int = 500,
        codelist_urns: Sequence[str] | None = None,
        codelist_policies: Sequence[CodelistIndexPolicy] = (),
        include_alt_labels: bool = True,
        carry_hierarchy_fields: bool = True,
        reference_metadata_blends: Sequence[ReferenceMetadataBlend] = (),
        context_annotation_types: Sequence[str] | None = None,
        exclude_annotation_types: Sequence[str] | None = None,
        include_concepts: bool = True,
        embed_concepts: bool = True,
        concept_scheme_urns: Sequence[str] | None = None,
        include_reference_metadata: bool = False,
        embed_reference_metadata: bool = True,
        reference_metadataflow_urns: Sequence[str] | None = None,
        document_prefix: str | None = None,
        query_prefix: str | None = None,
        model_name: str | None = None,
    ) -> int:
        """Build the search index now, so `view` can search before the seal.

        `seal` rebuilds the index from the edition's rows with the arguments
        of the last call here (the defaults when there was none), so the
        sealed index always holds the sealed rows.

        With *model_name*, a text whose `text_sha256` (model name, document
        prefix, text) matches a stored vector takes that vector, so only new
        or changed texts reach the encoder, and a seal with unchanged text
        encodes nothing. Without *model_name* every text is encoded, since
        the encoder may be a new model.

        Args:
            embedding_model: The encoder; the working copy's when ``None``.
            progress: Called with ``(current, total)`` while texts are encoded.
            include_codes: Index SDMX code rows.
            embed_codes: Embed code rows too (slow on large catalogs).
            max_codes_per_codelist: Leave out codelists larger than this.
            codelist_urns: Index only these codelists' codes.
            codelist_policies: Per-codelist overrides (`CodelistIndexPolicy`).
            include_alt_labels: Fold ``skos:altLabel`` annotations into code text.
            carry_hierarchy_fields: Record each code's parent and depth.
            reference_metadata_blends: Reference-metadata attributes folded into code text.
            context_annotation_types: Annotation types folded into code text;
                ``statcat:searchContext`` when ``None``.
            exclude_annotation_types: Annotation types that keep a code out;
                ``statcat:searchExclude`` when ``None``.
            include_concepts: Index SDMX concept rows.
            embed_concepts: Embed concept rows.
            concept_scheme_urns: Index only these schemes' concepts.
            include_reference_metadata: Index reference-metadata sections as rows.
            embed_reference_metadata: Embed reference-metadata rows.
            reference_metadataflow_urns: Index only sets of these structures.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.
            model_name: The model id recorded in ``embeddings.json`` and
                hashed into each row's ``text_sha256``. Carried vectors are
                reused only when it is given; when ``None``, every text is
                encoded and hashed under the carried name.

        Returns:
            The number of datasets indexed.
        """
        store = self._live()
        extra: dict[str, Sequence[str]] = {}
        if context_annotation_types is not None:
            extra["context_annotation_types"] = context_annotation_types
        if exclude_annotation_types is not None:
            extra["exclude_annotation_types"] = exclude_annotation_types

        def rebuild() -> int:
            return store.build_index(
                embedding_model,
                progress,
                include_codes=include_codes,
                embed_codes=embed_codes,
                max_codes_per_codelist=max_codes_per_codelist,
                codelist_urns=codelist_urns,
                codelist_policies=codelist_policies,
                include_alt_labels=include_alt_labels,
                carry_hierarchy_fields=carry_hierarchy_fields,
                reference_metadata_blends=reference_metadata_blends,
                include_concepts=include_concepts,
                embed_concepts=embed_concepts,
                concept_scheme_urns=concept_scheme_urns,
                include_reference_metadata=include_reference_metadata,
                embed_reference_metadata=embed_reference_metadata,
                reference_metadataflow_urns=reference_metadataflow_urns,
                document_prefix=document_prefix,
                query_prefix=query_prefix,
                model_name=model_name,
                **extra,
            )

        n = rebuild()
        self._rebuild_index = rebuild
        return n

    # -- ending ----------------------------------------------------------

    def close(self) -> None:
        """Close the working copy and leave its edition unsealed, for ``working_copy(resume=True)``.

        A no-op once it is sealed, discarded or closed.
        """
        if self._closed is None:
            self._close(
                f"the working copy {self._edition_id} of {self._product_id} is closed; reopen it with "
                "working_copy(resume=True)"
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def discard(self) -> None:
        """Delete the unsealed edition (`sdmxlib.edition.discard`) and close the working copy."""
        self._live()
        edition_dir = self._registry.edition_dir
        self._close(f"the working copy {self._edition_id} of {self._product_id} was discarded")
        _edition.discard(edition_dir)

    def _search(self, store: _Store) -> None:
        """Seal step 3: rebuild the BM25 tables from this edition's rows, and drop the carried ones."""
        if importlib.util.find_spec("lancedb") is None:
            # Without lancedb nothing can rebuild the index, so a carried one
            # would describe the previous edition's rows: it is removed.
            search_dir = store._search_dir
            if search_dir is not None and search_dir.is_dir():
                shutil.rmtree(search_dir)
            return
        if self._rebuild_index is not None:
            self._rebuild_index()
        else:
            store.build_index()
        if store._lance is not None:
            store._lance.drop_carried()

    def seal(self, checks: Iterable[Callable[[EditionView], Iterable[Finding]]] = ()) -> Edition:
        """Seal the working copy into an edition and record it in the Inventory.

        The steps: settle the journal; run *checks*; refuse when the base is
        no longer live; rebuild the search index; compute the ``statcat``
        section; close; seal with sdmxlib (journal entry, then manifest),
        passing the ``partial_of`` of each partial ``statcat._meta`` names
        and the store still holds; run the publish check on a structure
        edition, or on an edition whose partials record a ``partial_of``, with
        every other structure product's live and embargoed editions; record
        the edition, ``live`` when the catalog's embargo is off; settle the
        journal entry.
        `Catalog.seal` runs the same steps for several working copies and
        records them in one Inventory version.

        Args:
            checks: Seal checks (`statcatlib.checks.SealCheck`), run against
                `view` before anything is written. Any finding refuses the
                seal and leaves the working copy open.

        Returns:
            The edition.

        Raises:
            EditionCheckError: The publish check found something, such as a
                reference no other structure product's live or embargoed
                edition resolves, or a partial whose codes its full codelist
                lacks, or one with no structure edition in the catalog to
                check it against; the sealed edition stays on disk
                unrecorded.
            InventoryRuleError: sdmxlib's rules refused the record after the
                manifest was written, such as `NonFastForwardError` when a
                rollback lands during the seal; the sealed edition stays on
                disk unrecorded.
            InventoryContentionError: The record lost too many races; the
                product's next ``working_copy()`` records the edition.

        Before anything is written, a check's finding raises `SealCheckError`
        (for a structure product, also each dataflow, binding or partial it
        holds; for a data product, a fill that stopped before it recorded its
        cuts, or a partial cut outside `fill_dataflows` that would carry the
        previous edition's ``partial_of``), and a base that is no longer the
        live edition raises
        `NonFastForwardError`; both leave the working copy open. A
        `ManifestError` means sdmxlib refused the edition, such as for a coded
        value outside its codelist, before writing the journal entry; the
        working copy is closed, and ``working_copy(resume=True)`` reopens it.
        """
        self._precheck(checks)
        self._cross_precheck()
        sealed = self._seal_edition()
        if self._kind == "structure" or sealed.partials:
            structures = structure_editions(
                self._writer.full(), self._writer.root, product_id=self._product_id, batch={}
            )
            try:
                self._publish_check(structures, partials=sealed.partials)
            except EditionCheckError as exc:
                self._unrecorded(exc)
                raise
        try:
            self._writer.commit(
                lambda full: self._record_change(full, sealed), kind="seal", reason=f"seal {self._edition_id}"
            )
        except _RecordedAlreadyError:
            pass
        except InventoryRuleError as exc:
            self._unrecorded(exc)
            raise
        except InventoryContentionError as exc:
            msg = f"{exc}; the edition {self._edition_id} is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc
        self._recorded()
        return sealed.edition

    def _precheck(
        self, checks: Iterable[Callable[[EditionView], Iterable[Finding]]], *, batch: Collection[str] = ()
    ) -> None:
        """Seal steps 0 to 2: settle the journal, run *checks*, and refuse a base that is no longer live.

        A structure working copy is also checked for content a structure
        product does not hold (`statcatlib.bundle.structure_content`), and a
        data working copy for partials whose ``partial_of`` the seal cannot
        vouch for (`unvouched_partials`). The cross-product pre-check
        (`_cross_precheck`, or `precheck_refusals` over a batch) follows.

        Args:
            checks: The seal checks, run against `view`.
            batch: The products of the `Catalog.seal` batch, which the
                journal repair leaves to the batch.

        Raises:
            SealCheckError: A check found something.
            NonFastForwardError: The base is no longer the live edition.
        """
        store = self._live()
        settle_journal(
            self._writer,
            self._product_dir,
            self._product_id,
            self._lifecycle,
            exclude=[other for other in batch if other != self._product_id],
        )
        findings = [finding for check in checks for finding in check(self.view())]
        if self._kind == "structure":
            findings += structure_content(store._cursor())
        else:
            previous = None if self._previous is None else self._product_dir / self._previous
            findings += unvouched_partials(store._cursor(), self._registry, previous)
        if findings:
            raise SealCheckError(findings)
        live = _live_head(self._writer, self._product_id)
        if live != self._previous:
            msg = (
                f"{self._product_id}: this working copy is built on {self._previous}, and the live edition is "
                f"{live}. Discard it with discard(), then start one from the live edition with working_copy()."
            )
            raise NonFastForwardError(msg, rule="fast-forward", product=self._product_id, edition=self._edition_id)

    def _cross_precheck(self) -> None:
        """Seal step 2's cross-product pre-check of this working copy alone, against the head (design D8).

        A record that would break specification §6.5 item 4, 7 or 8 raises
        `CrossProductRuleError`; nothing is sealed, and the working copy stays
        open.
        """
        full = self._writer.full()
        planned = self._planned(full)
        if self._may_be_refused(full, ()):
            refused = precheck_refusals(self._writer, full, [planned])
            if self._product_id in refused:
                raise refused[self._product_id]

    def _planned(self, full: FullInventory | None) -> Planned:
        """This working copy's provisional record on *full*, for the cross-product pre-check.

        Args:
            full: The head.

        Returns:
            The provisional record, read through `_own_artefacts`.

        Raises:
            InventoryRuleError: sdmxlib's rules refuse the record.
        """
        product_id = self._product_id
        leaf = full.leaf(product_id) if full is not None and product_id in full.products() else None
        retired = {t.edition_id for t in full.retired if t.product_id == product_id} if full else set()
        try:
            provisional = record(
                leaf,
                product_id=product_id,
                kind=self._kind,
                lifecycle=self._lifecycle,
                edition_id=self._edition_id,
                previous=self._previous,
                manifest_sha256="0" * 64,
                schema_version=SCHEMA_VERSION,
                embargo=full.embargo if full is not None else False,
                now=datetime.now(UTC),
                retired=retired,
            )
        except TransitionError as exc:
            raise InventoryRuleError.wrapping(exc, product=product_id, edition=self._edition_id) from exc
        return Planned(product_id, self._edition_id, provisional, read=self._own_artefacts)

    def _may_be_refused(self, full: FullInventory | None, batch: Sequence[Planned]) -> bool:
        """Whether any cross-product rule could refuse this working copy, so the pre-check needs its artefacts.

        No rule can refuse a structure working copy when no other structure
        product has a live or embargoed edition, nor a data working copy
        when no structure product has one, it holds no partial with a
        ``partial_of``, no other data working copy is in *batch*, and no
        other data product's live edition shares a URN with it. The
        pre-check then fingerprints nothing, so a Phase 2a seal costs what it
        did; the Inventory commit runs the rules either way.

        Args:
            full: The head.
            batch: The other editions the pre-check is given.

        Returns:
            Whether to run the pre-check.
        """
        others = {one.product_id: one.leaf for one in batch if one.product_id != self._product_id}
        if full is not None:
            others = {p: full.leaf(p) for p in full.products() if p != self._product_id} | others
        if any(leaf.kind == "structure" and any(e.state in ACTIVE for e in leaf.editions) for leaf in others.values()):
            return True
        if self._kind == "structure":
            return False
        if self._cuts() or any(one.product_id != self._product_id and one.leaf.kind == "data" for one in batch):
            return True
        cursor = self._live()._cursor()
        own = {urn for (urn,) in cursor.execute("SELECT urn FROM sdmx.artefact").fetchall()}
        for other, leaf in others.items():
            for entry in leaf.editions:
                if entry.state == "live" and own & set(
                    self._writer.artefacts(other, entry.edition_id, entry.manifest_sha256)
                ):
                    return True
        return False

    def _cuts(self) -> dict[str, str]:
        """Each held partial's ``partial_of`` as the seal records it: ``_meta``'s, else the previous manifest's."""
        cursor = self._live()._cursor()
        held = _held_partials(cursor)
        carried: dict[str, str] = {}
        if self._previous is not None:
            manifest = _edition.read_manifest(self._product_dir / self._previous)
            carried = {a.urn: a.partial_of for a in manifest.artefacts if a.partial and a.partial_of is not None}
        cuts = carried | recorded_cuts(cursor)
        return {urn: fingerprint for urn, fingerprint in cuts.items() if urn in held}

    def _own_artefacts(self) -> dict[str, PartialRecord]:
        """The working copy's artefacts as its seal would fingerprint them (`StoreArtefacts`).

        Each full artefact's fingerprint is the SHA-256 of
        `sdmxlib.edition.canonical_json`, as ``seal`` computes it; each
        partial's ``partial_of`` is `_cuts`'s. A partial with none is left
        out, and so are the bindings, which the manifest does not list.
        """
        cursor = self._live()._cursor()
        cuts = self._cuts()
        rows = cursor.execute(
            "SELECT urn, is_partial FROM sdmx.artefact WHERE artefact_type <> 'DataflowBinding' ORDER BY urn"
        ).fetchall()
        found: dict[str, PartialRecord] = {}
        for urn, partial in rows:
            if partial and urn not in cuts:
                continue
            artefact = self._registry.get(SdmxUrn.parse(urn), eager=True)
            if artefact is None:
                continue
            found[urn] = PartialRecord(
                fingerprint=_fingerprint(artefact), partial_of=cuts.get(urn) if partial else None
            )
        return found

    def _record_change(self, full: FullInventory | None, sealed: _Sealed) -> Change:
        """Seal step 6's change: the product's next leaf, or `_RecordedAlreadyError` when a repair recorded it."""
        if self._recorded_in(full, sealed):
            raise _RecordedAlreadyError
        return Change(leaves={self._product_id: self._next_leaf(full, sealed)})

    def _recorded_in(self, full: FullInventory | None, sealed: _Sealed) -> bool:
        """Whether *full* records *sealed* already with its ``manifest_sha256``: another writer's journal repair did."""
        if full is None or self._product_id not in full.products():
            return False
        return any(
            e.edition_id == self._edition_id and e.manifest_sha256 == sealed.edition.manifest_sha256
            for e in full.entries(self._product_id)
        )

    def _seal_edition(self) -> _Sealed:
        """Seal steps 3 to 5: rebuild the search index, compute the section, close, and seal with sdmxlib.

        Returns:
            The sealed edition, not yet recorded.

        Raises:
            ManifestError: sdmxlib refused the edition; the working copy is
                closed, and ``working_copy(resume=True)`` reopens it.
        """
        store = self._live()
        self._search(store)
        section = catalog_section(store, kind=self._kind)
        cursor = store._cursor()
        held = _held_partials(cursor)
        # A later fill can have replaced a cut partial by its full codelist,
        # and sdmxlib refuses a partial_of for a URN it holds as no partial.
        cuts = {urn: fingerprint for urn, fingerprint in recorded_cuts(cursor).items() if urn in held}
        edition_dir = self._registry.edition_dir
        self._close(f"the working copy {self._edition_id} of {self._product_id} is sealed")
        try:
            manifest = _edition.seal(
                edition_dir,
                product_id=self._product_id,
                producer=producer(),
                lifecycle=self._lifecycle,
                extensions={STATCAT_SECTION: section},
                partial_of=cuts or None,
            )
        except ManifestError as exc:
            self._closed = (
                f"the seal of {self._edition_id} of {self._product_id} was refused ({exc}); reopen it with "
                "working_copy(resume=True)"
            )
            raise
        edition = Edition(
            edition_id=manifest.edition_id,
            product_id=self._product_id,
            previous=manifest.previous,
            manifest_sha256=manifest_sha256(edition_dir),
        )
        return _Sealed(edition=edition, schema_version=manifest.schema_version, partials=cut_partials(manifest))

    def _next_leaf(self, full: FullInventory | None, sealed: _Sealed) -> Leaf:
        """Seal step 6's record: the product's next leaf in *full*, with *sealed* recorded.

        Args:
            full: The Inventory version the record lands on, or ``None``
                before the first.
            sealed: The edition `_seal_edition` sealed.

        Returns:
            The product's next leaf.

        Raises:
            InventoryRuleError: sdmxlib's rules refuse the record, such as a
                base that is no longer live (`NonFastForwardError`).
        """
        product_id, edition_id = self._product_id, self._edition_id
        leaf = full.leaf(product_id) if full is not None and product_id in full.products() else None
        retired = {t.edition_id for t in full.retired if t.product_id == product_id} if full else set()
        try:
            return record(
                leaf,
                product_id=product_id,
                kind=self._kind,
                lifecycle=self._lifecycle,
                edition_id=edition_id,
                previous=self._previous,
                manifest_sha256=sealed.edition.manifest_sha256,
                schema_version=sealed.schema_version,
                embargo=full.embargo if full is not None else False,
                now=datetime.now(UTC),
                retired=retired,
            )
        except TransitionError as exc:
            raise InventoryRuleError.wrapping(exc, product=product_id, edition=edition_id) from exc

    def _publish_check(self, structures: Sequence[Path], *, partials: Sequence[str] = ()) -> None:
        """Seal step 5b: the publish check of the sealed edition, with *structures* (`publish_check`).

        Args:
            structures: The structure editions its references and partials
                resolve in.
            partials: The partials its manifest records a ``partial_of`` for.

        A finding raises `EditionCheckError`.
        """
        publish_check(
            self._product_dir / self._edition_id,
            product_id=self._product_id,
            structures=structures,
            partials=partials,
        )

    def _recorded(self) -> None:
        """After the record: clear the edition's journal entry."""
        _edition.clear_journal(self._product_dir, self._edition_id)

    def _unrecorded(self, exc: InventoryRuleError | EditionCheckError) -> None:
        """After a refused record or publish check: clear the journal entry, so no repair records it; log a WARNING.

        The edition stays sealed and unrecorded on disk.
        """
        _edition.clear_journal(self._product_dir, self._edition_id)
        log.warning(
            "%s: the sealed edition %s was not recorded (%s); it stays on disk unrecorded, and "
            "`statcat doctor` reports it",
            self._product_id,
            self._edition_id,
            exc,
        )


class _RecordedAlreadyError(Exception):
    """The edition the seal records is recorded already, by another writer's journal repair."""


def _build(
    writer: InventoryWriter, product_dir: Path, product_id: str, lifecycle: Lifecycle, *, base: str | None, resume: bool
) -> sdmxlib.LocalRegistry:
    """`WorkingCopy._start`'s third step: the build registry of a new edition on *base*, or of the resumed one.

    Args:
        writer: The catalog's Inventory writer.
        product_dir: The product's directory.
        product_id: The product.
        lifecycle: The product's lifecycle, for the journal rule.
        base: The live edition to build on, or ``None``.
        resume: Reopen the unsealed edition in place of starting one.

    Returns:
        The open build registry.

    Raises:
        WorkingCopyExistsError: The product has an unsealed edition and
            *resume* is false.
        FileNotFoundError: *resume* is true and the product has no unsealed
            edition.
    """
    unsealed = [info for info in _edition.list_editions(product_dir) if not info.sealed]
    if unsealed and not resume:
        info = unsealed[0]
        msg = (
            f"{product_id} has an unsealed edition, {info.edition_id}, started "
            f"{info.created_at.isoformat(timespec='seconds')}; a product has one working copy at a time. "
            "Resume it with working_copy(resume=True), or throw it away with working_copy(resume=True).discard()."
        )
        raise WorkingCopyExistsError(msg)
    if resume:
        if not unsealed:
            msg = f"{product_id} has no unsealed edition to resume; call working_copy() to start one"
            raise FileNotFoundError(msg)
        registry = sdmxlib.LocalRegistry.build(product_dir, resume=unsealed[0].edition_id)
        settle_journal(writer, product_dir, product_id, lifecycle)
        return registry
    product_dir.mkdir(parents=True, exist_ok=True)
    registry = None
    try:
        registry = sdmxlib.LocalRegistry.build(product_dir, previous=base)
    finally:
        if registry is None:
            # sdmxlib makes the edition's directory before it opens the
            # store, which can refuse it (another store schema); the
            # directory holds nothing to keep.
            for found in _edition.list_editions(product_dir):
                if not found.sealed:
                    _edition.discard(product_dir / found.edition_id)
    return registry


def _previous_of(registry: sdmxlib.LocalRegistry) -> str | None:
    """The edition a build registry extends, from its ``sdmx._meta``."""
    return meta_value(registry.connection, "previous")

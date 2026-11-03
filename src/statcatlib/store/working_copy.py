"""A product's one unsealed edition, and its seal (Phase 2a, Phase 3).

`Product.working_copy` starts or resumes a `WorkingCopy`. Ingest writes into
it through the write methods and `WorkingCopy.registry`, `WorkingCopy.view`
reads it back, and `WorkingCopy.seal` turns it into an immutable edition and
records it in the Inventory, live when the catalog's embargo is off.

A product is a ``data`` product or a ``structure`` product (`WorkingCopy.kind`).
A structure product holds full artefacts only, copied with
`WorkingCopy.fill_structures`, and its seal runs the publish check
(`sdmxlib.edition.check_edition` at ``publish``) against the catalog's other
structure products before the Inventory records the edition.

**The journal rule.** sdmxlib's `seal` writes a journal entry,
``<product>/journal/<edition_id>.json``, before the manifest, and statcatlib
clears it once the edition is recorded. An entry left behind is a seal that a
crash may have stopped before the record. Every writer settles the product's
entries against the newest Inventory version when it starts a working copy,
when it resumes one, at the start of a seal and after its record
(`settle_journal`):

| Journal entry | Action |
|---|---|
| The newest root names or tombstones the edition | Clear the entry |
| The edition has no ``manifest.json`` | Clear the entry; the edition is an unsealed working copy |
| It has a manifest and no root names it | Record it, then clear the entry |
| Recording raises ``duplicate`` with the same ``manifest_sha256`` | Treat as success; clear the entry |
| Recording raises any other `InventoryRuleError` | Clear the entry, log a WARNING, leave the edition |
| A structure edition fails its publish check | Clear the entry, log a WARNING, leave the edition |
| `InventoryContentionError` | Keep the entry, so the next working copy records it |
| The entry cannot be read | Raise `JournalError` naming the file and ``statcat doctor`` |

Each edition is recorded with the kind its manifest's ``statcat`` section
names, ``data`` when it names none. A structure edition's publish check is
given the other structure products' live and embargoed editions, and each
other product's journalled structure edition in place of that product's.

A product has one writer at a time: a laptop convention, stated in the docs.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import sdmxlib
import sdmxlib.edition as _edition
from attrs import frozen
from sdmxlib.edition import MANIFEST_NAME, Finding, ManifestError, check_edition
from sdmxlib.inventory import FullInventory, Leaf, TransitionError, head, record
from sdmxlib.storage.schema import SCHEMA_VERSION, meta_value

from statcatlib.bundle import STATCAT_SECTION, catalog_section, check_catalog, structure_content
from statcatlib.store import schema as _catalog_schema
from statcatlib.store.inventory import (
    Change,
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
    from sdmxlib import EditionRegistry, MaintainableArtefact, SdmxUrn
    from sdmxlib.edition import JournalEntry, Lifecycle, Manifest
    from sdmxlib.inventory import Kind

    from statcatlib.model.dataset import Dataset
    from statcatlib.model.skos import Concept, ConceptScheme
    from statcatlib.store.store import CodelistIndexPolicy, EmbeddingModel, ReferenceMetadataBlend

__all__ = [
    "Edition",
    "EditionCheckError",
    "JournalError",
    "SealCheckError",
    "WorkingCopy",
    "WorkingCopyClosedError",
    "WorkingCopyExistsError",
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


def structure_editions(
    full: FullInventory | None, catalog_root: Path, *, product_id: str, batch: Mapping[str, Path]
) -> list[Path]:
    """The structure editions a structure edition of *product_id* is checked with (design decision O4).

    They are the live and embargoed editions of every other structure
    product in *full*, with each product of *batch* holding its batch
    edition in place of its recorded ones.

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


def publish_check(edition_dir: Path, *, product_id: str, structures: Sequence[Path]) -> None:
    """Seal step 5b: `sdmxlib.edition.check_edition` at ``publish`` on a sealed edition, with `check_catalog`.

    Args:
        edition_dir: The sealed edition.
        product_id: Its product.
        structures: The structure editions its references and partials
            resolve in (`structure_editions`); ``structures=None`` when empty,
            since sdmxlib refuses an empty sequence.

    Raises:
        EditionCheckError: The check found something.
    """
    report = check_edition(
        edition_dir,
        at="publish",
        structures=list(structures) or None,
        extensions={STATCAT_SECTION: check_catalog},
    )
    if report.findings:
        raise EditionCheckError(report.findings, product=product_id, edition=edition_dir.name)


def _journalled_structures(products_dir: Path, product_id: str, full: FullInventory | None) -> dict[str, Path]:
    """Each other product's newest sealed, unrecorded structure edition that a journal entry names.

    A batch that crashed after sdmxlib's seals leaves one per product; the
    repair of one checks it with the others, as the batch would have.
    """
    found: dict[str, Path] = {}
    for other_dir in sorted(products_dir.iterdir()) if products_dir.is_dir() else ():
        if other_dir.name == product_id or not (other_dir / "journal").is_dir():
            continue
        with contextlib.suppress(ManifestError):
            for entry in _edition.read_journal(other_dir):
                edition_dir = other_dir / entry.edition_id
                if _named(full, other_dir.name, entry.edition_id) or not (edition_dir / MANIFEST_NAME).is_file():
                    continue
                if edition_kind(_edition.read_manifest(edition_dir)) == "structure":
                    newest = found.get(other_dir.name)
                    if newest is None or newest.name < edition_dir.name:
                        found[other_dir.name] = edition_dir
    return found


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


def settle_journal(writer: InventoryWriter, product_dir: Path, product_id: str, lifecycle: Lifecycle) -> None:
    """Apply the journal rule to the product's journal entries (see the module docstring).

    Every edition that needs recording goes into one Inventory commit, each
    with the kind its manifest names. A structure edition is recorded only
    once its publish check finds nothing.

    Args:
        writer: The catalog's Inventory writer.
        product_dir: The product's directory.
        product_id: The product.
        lifecycle: The lifecycle to record a product's first edition with.

    An entry that cannot be read raises `JournalError`.

    Raises:
        InventoryContentionError: The record lost too many races; the entries
            are kept, and the next working copy records them.
    """
    entries = _read_journal(product_dir)
    if not entries:
        return
    full = writer.full()
    pending = _pending_entries(full, product_dir, product_id, entries)
    if not pending:
        return
    failed: dict[str, Exception] = dict(_check_journalled(full, product_dir, product_id, pending))
    recordable = [item for item in pending if item[0].edition_id not in failed]
    refused: dict[str, TransitionError] = {}

    def change(head_now: FullInventory | None) -> Change:
        leaf = head_now.leaf(product_id) if head_now is not None and product_id in head_now.products() else None
        retired = [t.edition_id for t in head_now.retired if t.product_id == product_id] if head_now else []
        nxt, _, found = record_journalled(
            leaf,
            recordable,
            product_id=product_id,
            lifecycle=lifecycle,
            embargo=head_now.embargo if head_now is not None else False,
            retired=retired,
            now=datetime.now(UTC),
        )
        refused.clear()
        refused.update(found)
        return Change(leaves={} if nxt is None else {product_id: nxt})

    if recordable and change(full).leaves:
        try:
            writer.commit(change, kind="seal", reason="record a seal its writer did not record (journal repair)")
        except InventoryContentionError as exc:
            msg = f"{exc}; the journalled edition is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc
    for entry, _, _, _ in pending:
        why = failed.get(entry.edition_id) or refused.get(entry.edition_id)
        if why is not None:
            log.warning(
                "%s: the journalled edition %s cannot be recorded (%s); it stays on disk unrecorded, and "
                "`statcat doctor` reports it",
                product_id,
                entry.edition_id,
                why,
            )
        _edition.clear_journal(product_dir, entry.edition_id)


def _pending_entries(
    full: FullInventory | None, product_dir: Path, product_id: str, entries: Iterable[JournalEntry]
) -> list[tuple[JournalEntry, str, str, Kind]]:
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
    pending: list[tuple[JournalEntry, str, str, Kind]] = []
    for entry in entries:
        edition_dir = product_dir / entry.edition_id
        if _named(full, product_id, entry.edition_id) or not (edition_dir / MANIFEST_NAME).is_file():
            _edition.clear_journal(product_dir, entry.edition_id)
            continue
        manifest = _edition.read_manifest(edition_dir)
        pending.append((entry, manifest_sha256(edition_dir), manifest.schema_version, edition_kind(manifest)))
    return pending


def _check_journalled(
    full: FullInventory | None,
    product_dir: Path,
    product_id: str,
    pending: Sequence[tuple[JournalEntry, str, str, Kind]],
) -> dict[str, EditionCheckError]:
    """The journal repair's publish check: each journalled structure edition's finding, by edition id."""
    failed: dict[str, EditionCheckError] = {}
    checked = [entry.edition_id for entry, _, _, kind in pending if kind == "structure"]
    if not checked:
        return failed
    products_dir = product_dir.parent
    structures = structure_editions(
        full,
        products_dir.parent,
        product_id=product_id,
        batch=_journalled_structures(products_dir, product_id, full),
    )
    for edition_id in checked:
        try:
            publish_check(product_dir / edition_id, product_id=product_id, structures=structures)
        except EditionCheckError as exc:
            failed[edition_id] = exc
    return failed


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


@frozen
class _Sealed:
    """An edition sealed by sdmxlib and not yet recorded, with the store schema its manifest names."""

    edition: Edition
    schema_version: str


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
        section; close; seal with sdmxlib (journal entry, then manifest);
        run the publish check on a structure edition; record the edition,
        ``live`` when the catalog's embargo is off; settle the journal entry.
        `Catalog.seal` runs the same steps for several working copies and
        records them in one Inventory version.

        Args:
            checks: Seal checks (`statcatlib.checks.SealCheck`), run against
                `view` before anything is written. Any finding refuses the
                seal and leaves the working copy open.

        Returns:
            The edition.

        Raises:
            EditionCheckError: The publish check of a structure edition found
                something, such as a reference no other structure product's
                live or embargoed edition resolves; the sealed edition stays
                on disk unrecorded.
            InventoryRuleError: sdmxlib's rules refused the record after the
                manifest was written, such as `NonFastForwardError` when a
                rollback lands during the seal; the sealed edition stays on
                disk unrecorded.
            InventoryContentionError: The record lost too many races; the
                product's next ``working_copy()`` records the edition.

        Before anything is written, a check's finding raises `SealCheckError`
        (for a structure product, also each dataflow, binding or partial it
        holds), and a base that is no longer the live edition raises
        `NonFastForwardError`; both leave the working copy open. A
        `ManifestError` means sdmxlib refused the edition, such as for a coded
        value outside its codelist, before writing the journal entry; the
        working copy is closed, and ``working_copy(resume=True)`` reopens it.
        """
        self._precheck(checks)
        sealed = self._seal_edition()
        if self._kind == "structure":
            structures = structure_editions(
                self._writer.full(), self._writer.root, product_id=self._product_id, batch={}
            )
            try:
                self._publish_check(structures)
            except EditionCheckError as exc:
                self._unrecorded(exc)
                raise
        try:
            self._writer.commit(
                lambda full: Change(leaves={self._product_id: self._next_leaf(full, sealed)}),
                kind="seal",
                reason=f"seal {self._edition_id}",
            )
        except InventoryRuleError as exc:
            self._unrecorded(exc)
            raise
        except InventoryContentionError as exc:
            msg = f"{exc}; the edition {self._edition_id} is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc
        self._recorded()
        return sealed.edition

    def _precheck(self, checks: Iterable[Callable[[EditionView], Iterable[Finding]]]) -> None:
        """Seal steps 0 to 2: settle the journal, run *checks*, and refuse a base that is no longer live.

        A structure working copy is also checked for content a structure
        product does not hold (`statcatlib.bundle.structure_content`).

        Args:
            checks: The seal checks, run against `view`.

        Raises:
            SealCheckError: A check found something.
            NonFastForwardError: The base is no longer the live edition.
        """
        store = self._live()
        settle_journal(self._writer, self._product_dir, self._product_id, self._lifecycle)
        findings = [finding for check in checks for finding in check(self.view())]
        if self._kind == "structure":
            findings += structure_content(store._cursor())
        if findings:
            raise SealCheckError(findings)
        live = _live_head(self._writer, self._product_id)
        if live != self._previous:
            msg = (
                f"{self._product_id}: this working copy is built on {self._previous}, and the live edition is "
                f"{live}. Discard it with discard(), then start one from the live edition with working_copy()."
            )
            raise NonFastForwardError(msg, rule="fast-forward", product=self._product_id, edition=self._edition_id)

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
        edition_dir = self._registry.edition_dir
        self._close(f"the working copy {self._edition_id} of {self._product_id} is sealed")
        try:
            manifest = _edition.seal(
                edition_dir,
                product_id=self._product_id,
                producer=producer(),
                lifecycle=self._lifecycle,
                extensions={STATCAT_SECTION: section},
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
        return _Sealed(edition=edition, schema_version=manifest.schema_version)

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

    def _publish_check(self, structures: Sequence[Path]) -> None:
        """Seal step 5b: the publish check of the sealed edition, with *structures* (`publish_check`).

        Args:
            structures: The structure editions its references resolve in.

        A finding raises `EditionCheckError`.
        """
        publish_check(self._product_dir / self._edition_id, product_id=self._product_id, structures=structures)

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

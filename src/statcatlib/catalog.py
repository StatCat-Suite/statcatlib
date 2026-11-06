"""A catalog: a directory of products, each a chain of editions (Phase 2a).

The layout is the sharded catalog spec's §4.1::

    <catalog>/catalog.json                              identity and restore epoch
    <catalog>/CURRENT                                   names the newest Inventory version
    <catalog>/inventory/<seq>.json                      one root per Inventory version
    <catalog>/inventory/leaves/<sha256>.json            one leaf per product and version
    <catalog>/products/<product>/<edition_id>/          one directory per edition
    <catalog>/products/<product>/data/                  data files every edition shares
    <catalog>/products/<product>/journal/               seal journal entries

`Catalog.create` creates a catalog, or reopens one for writing, and
`Catalog.open` opens one read-only. `Catalog.product` returns a `Product`:
its `Product.view` reads the live edition, and `Product.working_copy` starts
the product's one unsealed edition, a `WorkingCopy`, which
`WorkingCopy.seal` turns into the next edition. `Catalog.seal` seals several
products' working copies and records them in one Inventory version.

Callers reach storage only through this API: no method takes or returns a path
inside the catalog.

`Catalog.open` also takes a `BlobLocation`, a catalog in object storage. It reads
the same Inventory through the store. A view pulls the live edition's catalog files
to ``<cache_dir>/<catalog_id>``, and reads observations from the store through DuckDB.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Self

import sdmxlib
from attrs import frozen
from sdmxlib.edition import Finding, ManifestError, check_on_open, read_manifest
from sdmxlib.inventory import CATALOG_NAME, LiveEdition, LiveInventory, decode_root, root_path

from statcatlib.bundle import STATCAT_SECTION, description_from_root
from statcatlib.store.blob import (
    _SAFE_ID,
    BlobInventoryStorage,
    BlobLocation,
    _cached_edition,
    _read_remote_manifest,
    _statcat_check,
)
from statcatlib.store.inventory import (
    Change,
    InventoryContentionError,
    InventoryRuleError,
    InventoryWriter,
    InventoryWriteRefusedError,
)
from statcatlib.store.store import VIEW_ATTRIBUTES, EditionView, IncompatibleCatalogError, _Store
from statcatlib.store.working_copy import (
    Edition,
    EditionCheckError,
    Planned,
    SealCheckError,
    WorkingCopy,
    precheck_refusals,
    structure_editions,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from sdmxlib.edition import Lifecycle
    from sdmxlib.inventory import FullInventory, Kind, Leaf

    from statcatlib.model.catalog import CatalogDescription
    from statcatlib.store.inventory import CrossProductRuleError, InventoryStorage
    from statcatlib.store.store import EmbeddingModel
    from statcatlib.store.working_copy import _Sealed

__all__ = ["Catalog", "Edition", "NoLiveEditionError", "Product", "SealOutcomes"]

log = logging.getLogger(__name__)

#: Where `docs/upgrading.md` gives the re-ingest steps a refused layout needs.
_UPGRADING = "the re-ingest steps in docs/upgrading.md (https://statcat-suite.github.io/statcatlib/upgrading/)"

#: A URI where a path belongs: a catalog in object storage needs a `BlobLocation`.
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class NoLiveEditionError(LookupError):
    """The product has no live edition, so there is nothing for a public reader to view."""


@frozen
class SealOutcomes:
    """What `Catalog.seal` did with each working copy, keyed by product id.

    Every product of the batch is in exactly one of the two mappings.

    Attributes:
        sealed: Each recorded product's new edition. All of them were
            recorded in one Inventory version.
        refused: Each refused product's error. A `SealCheckError`, a
            `NonFastForwardError` from the pre-check, or another
            `InventoryRuleError` from sdmxlib's rules leaves the working copy
            open; a `ManifestError` closes it, and
            ``working_copy(resume=True)`` reopens it. An `EditionCheckError`
            from the publish check of a structure edition or of an edition
            with partials, or an
            `InventoryRuleError` at record time, leaves the sealed edition on
            disk unrecorded, and an `InventoryContentionError` at record time
            leaves it for the product's next ``working_copy()`` to record.
    """

    sealed: Mapping[str, Edition]
    refused: Mapping[str, Exception]


class _NothingToRecordError(Exception):
    """Every product of a batch was refused at record time, so no Inventory version is written."""


@frozen
class _Opening:
    """What `Catalog.open` and `Catalog.create` hand the constructor."""

    root: Path
    writer: InventoryWriter | None
    location: BlobLocation | None = None
    catalog_id: str | None = None


def _refuse_legacy_layout(path: Path) -> None:
    """Refuse a directory that holds a statcatlib 0.11 catalog, a 0.72 bundle, or other files.

    `Catalog.open`, `Catalog.create` and the operational object run it before
    they write anything, so a refused directory is left as it was. Names that
    start with ``.`` (such as ``.DS_Store``) are not counted as files.

    Args:
        path: The directory.

    Raises:
        IncompatibleCatalogError: It has a root-level ``meta.duckdb``, a
            ``bundle.manifest.json``, or files and no ``catalog.json``.
    """
    if not path.is_dir():
        return
    names = {child.name for child in path.iterdir() if not child.name.startswith(".")}
    if "meta.duckdb" in names:
        found = "a statcatlib 0.11 catalog (a root-level meta.duckdb)"
    elif "bundle.manifest.json" in names:
        found = "a published sdmxlib 0.72 bundle (bundle.manifest.json)"
    elif names and CATALOG_NAME not in names:
        found = f"files and no {CATALOG_NAME}, so it is not a catalog"
    else:
        return
    msg = (
        f"{path} holds {found}. statcatlib 0.12 reads only catalogs of products and editions, and "
        f"changed nothing here. Rebuild the catalog in a new directory with {_UPGRADING}."
    )
    raise IncompatibleCatalogError(msg)


def _probe_hard_links(path: Path) -> None:
    """Refuse a filesystem without hard links, which the Inventory writer's exclusive create needs.

    Args:
        path: The catalog directory, which exists.

    Raises:
        IncompatibleCatalogError: ``os.link`` fails in *path*, as on exFAT and
            some SMB mounts.
    """
    probe = path / f".statcat-link-probe.{os.getpid()}.{uuid.uuid4().hex}"
    linked = probe.with_name(probe.name + ".link")
    probe.write_bytes(b"")
    try:
        os.link(probe, linked)
    except OSError as exc:
        msg = (
            f"the filesystem holding {path} does not support hard links ({exc}), and a catalog's Inventory "
            "needs them to create each version exclusively. Use a local disk formatted with APFS, ext4 or "
            "NTFS; exFAT, FAT32 and some network shares are not supported."
        )
        raise IncompatibleCatalogError(msg) from exc
    finally:
        for leftover in (linked, probe):
            with contextlib.suppress(FileNotFoundError):
                leftover.unlink()


def _check_product_id(product_id: str) -> str:
    if not _SAFE_ID.fullmatch(product_id):
        msg = (
            f"{product_id!r} is not a product id: it names a directory, so it starts with a letter or digit "
            "and holds only letters, digits, '.', '_' and '-'"
        )
        raise ValueError(msg)
    return product_id


@frozen
class _BatchSealed:
    """A batch's sealed, unrecorded edition, with the batch structure editions its publish check was given."""

    wc: WorkingCopy
    sealed: _Sealed
    given: frozenset[str] = frozenset()


def _seal_each(
    writer: InventoryWriter,
    batch: list[WorkingCopy],
    checks: tuple[Callable[[EditionView], Iterable[Finding]], ...],
    refused: dict[str, Exception],
) -> list[_BatchSealed]:
    """`Catalog.seal`'s passes before the record: the structure pass, the structure-check pass, the data pass.

    1. Structure: pre-check each structure working copy, run the
       cross-product pre-check on them together, and seal each that passed.
    2. Structure check: run the publish check of each sealed structure
       edition, given the batch's other structure editions in place of
       their products' recorded ones. An edition with a finding is dropped,
       and the pass repeats for the rest until a pass drops none, since a
       reference may have resolved only in a dropped edition.
    3. Data: pre-check every data working copy, run the cross-product
       pre-check on them together with the batch's structure editions, then
       seal each that passed, then run the publish check of each data
       edition whose partials record a ``partial_of``, given every structure
       product's live and embargoed editions with the batch's structure
       editions in their place.

    Args:
        writer: The catalog's Inventory writer.
        batch: The working copies.
        checks: The seal checks.
        refused: Filled with each refused product's error.

    Returns:
        Each sealed edition that passed, not yet recorded: the structure
        editions first.
    """
    ids = frozenset(wc.product_id for wc in batch)
    structures = _seal_ready(writer, [wc for wc in batch if wc.kind == "structure"], checks, refused, batch=ids)
    checked = _check_structures(writer, structures, refused)
    fixed = _fixed(writer, checked)
    data = _seal_ready(writer, [wc for wc in batch if wc.kind != "structure"], checks, refused, batch=ids, fixed=fixed)
    return [*checked, *_check_partials(writer, data, checked, refused)]


def _fixed(writer: InventoryWriter, checked: list[_BatchSealed]) -> list[Planned]:
    """The batch's checked structure editions as the data pass's cross-product pre-check is given them.

    An edition whose record sdmxlib's rules refuse is left out; `_record_batch`
    refuses it again, with each edition whose publish check was given it.
    """
    full = writer.full()
    fixed: list[Planned] = []
    for done in checked:
        with contextlib.suppress(InventoryRuleError):
            fixed.append(Planned(done.wc.product_id, done.wc.edition_id, done.wc._next_leaf(full, done.sealed)))
    return fixed


def _cross_ready(
    writer: InventoryWriter, ready: list[WorkingCopy], refused: dict[str, Exception], fixed: list[Planned]
) -> list[WorkingCopy]:
    """The cross-product pre-check of one pass's working copies, together with *fixed*; the ones it accepts.

    A refused working copy stays open, and goes to *refused*.
    """
    full = writer.full()
    planned: dict[str, Planned] = {}
    for wc in ready:
        try:
            planned[wc.product_id] = wc._planned(full)
        except InventoryRuleError as exc:
            refused[wc.product_id] = exc
    checked = [
        planned[wc.product_id]
        for wc in ready
        if wc.product_id in planned
        and wc._may_be_refused(full, [*fixed, *(one for p, one in planned.items() if p != wc.product_id)])
    ]
    found: dict[str, CrossProductRuleError | ManifestError] = {}
    if checked:
        try:
            found.update(precheck_refusals(writer, full, checked, fixed))
        except ManifestError as exc:
            found.update(dict.fromkeys((one.product_id for one in checked), exc))
    refused.update(found)
    return [wc for wc in ready if wc.product_id in planned and wc.product_id not in found]


def _seal_ready(
    writer: InventoryWriter,
    working_copies: list[WorkingCopy],
    checks: tuple[Callable[[EditionView], Iterable[Finding]], ...],
    refused: dict[str, Exception],
    *,
    batch: frozenset[str],
    fixed: list[Planned] | None = None,
) -> list[_BatchSealed]:
    """Pre-check every working copy, run the cross-product pre-check on them together, then seal each that passed."""
    ready: list[WorkingCopy] = []
    for wc in working_copies:
        try:
            wc._precheck(checks, batch=batch)
        except (SealCheckError, InventoryRuleError, InventoryContentionError, InventoryWriteRefusedError) as exc:
            refused[wc.product_id] = exc
        else:
            ready.append(wc)
    ready = _cross_ready(writer, ready, refused, fixed or [])
    sealed: list[_BatchSealed] = []
    for wc in ready:
        try:
            sealed.append(_BatchSealed(wc=wc, sealed=wc._seal_edition()))
        except ManifestError as exc:
            refused[wc.product_id] = exc
    return sealed


def _check_structures(
    writer: InventoryWriter, sealed: list[_BatchSealed], refused: dict[str, Exception]
) -> list[_BatchSealed]:
    """The structure-check pass: each sealed structure edition's publish check, repeated until it drops none.

    Args:
        writer: The catalog's Inventory writer.
        sealed: The batch's sealed structure editions.
        refused: Filled with each dropped product's `EditionCheckError`.

    Returns:
        The editions that passed, each with the batch editions it was given.
    """
    surviving = list(sealed)
    while surviving:
        full = writer.full()
        dirs = {
            done.wc.product_id: writer.root / "products" / done.wc.product_id / done.wc.edition_id for done in surviving
        }
        kept: list[_BatchSealed] = []
        for done in surviving:
            product_id = done.wc.product_id
            given = {other: path for other, path in dirs.items() if other != product_id}
            try:
                done.wc._publish_check(structure_editions(full, writer.root, product_id=product_id, batch=given))
            except EditionCheckError as exc:
                done.wc._unrecorded(exc)
                refused[product_id] = exc
            else:
                kept.append(_BatchSealed(wc=done.wc, sealed=done.sealed, given=frozenset(given)))
        if len(kept) == len(surviving):
            return kept
        surviving = kept
    return surviving


def _check_partials(
    writer: InventoryWriter, sealed: list[_BatchSealed], structures: list[_BatchSealed], refused: dict[str, Exception]
) -> list[_BatchSealed]:
    """The data pass's publish check: each sealed data edition whose partials record a ``partial_of``.

    Args:
        writer: The catalog's Inventory writer.
        sealed: The batch's sealed data editions.
        structures: The batch's structure editions that passed their check.
        refused: Filled with each dropped product's `EditionCheckError`.

    Returns:
        The editions that passed or needed no check, each checked one with the
        batch structure editions it was given.
    """
    full = writer.full()
    given = {
        done.wc.product_id: writer.root / "products" / done.wc.product_id / done.wc.edition_id for done in structures
    }
    kept: list[_BatchSealed] = []
    for done in sealed:
        if not done.sealed.partials:
            kept.append(done)
            continue
        product_id = done.wc.product_id
        try:
            done.wc._publish_check(
                structure_editions(full, writer.root, product_id=product_id, batch=given),
                partials=done.sealed.partials,
            )
        except EditionCheckError as exc:
            done.wc._unrecorded(exc)
            refused[product_id] = exc
        else:
            kept.append(_BatchSealed(wc=done.wc, sealed=done.sealed, given=frozenset(given)))
    return kept


def _dropped_with(done: _BatchSealed, lost: _BatchSealed, exc: Exception) -> EditionCheckError:
    """The error of *done*, whose publish check was given *lost*'s edition, which was refused at record time."""
    finding = Finding(
        "structures",
        f"{lost.wc.product_id}/{lost.wc.edition_id}",
        f"the publish check was given this batch edition, which was not recorded ({exc})",
    )
    return EditionCheckError([finding], product=done.wc.product_id, edition=done.wc.edition_id)


def _drop_dependents(
    sealed: list[_BatchSealed],
    leaves: dict[str, Leaf],
    at_record: dict[str, InventoryRuleError | EditionCheckError],
) -> None:
    """Move each edition whose publish check was given a refused structure edition from *leaves* to *at_record*.

    Repeats until a pass moves none, since a dropped edition can have been
    given to another.
    """
    dropping = True
    while dropping:
        dropping = False
        for done in sealed:
            product_id = done.wc.product_id
            lost = next((d for d in sealed if d.wc.product_id in done.given and d.wc.product_id in at_record), None)
            if product_id in leaves and lost is not None:
                at_record[product_id] = _dropped_with(done, lost, at_record[lost.wc.product_id])
                del leaves[product_id]
                dropping = True


def _batch_leaves(
    writer: InventoryWriter,
    full: FullInventory | None,
    sealed: list[_BatchSealed],
    at_record: dict[str, InventoryRuleError | EditionCheckError],
) -> dict[str, Leaf]:
    """The batch's next leaves on *full*: each product the rules accept; each refused one goes to *at_record*.

    An edition *full* records already with its ``manifest_sha256`` gets no
    leaf and no refusal. The cross-product rules run again after each drop,
    since a dropped edition can be the one another was accepted with.
    """
    leaves: dict[str, Leaf] = {}
    for done in sealed:
        if done.wc._recorded_in(full, done.sealed):
            continue
        try:
            leaves[done.wc.product_id] = done.wc._next_leaf(full, done.sealed)
        except InventoryRuleError as exc:
            at_record[done.wc.product_id] = exc
    while True:
        _drop_dependents(sealed, leaves, at_record)
        found = writer.refusals(full, leaves)
        if not found:
            return leaves
        for product_id, exc in found.items():
            at_record[product_id] = exc
            del leaves[product_id]


def _record_batch(
    writer: InventoryWriter, sealed: list[_BatchSealed], refused: dict[str, Exception]
) -> dict[str, Edition]:
    """`Catalog.seal`'s last pass: record every sealed edition the rules accept, in one commit.

    A product sdmxlib's rules or the cross-product rules refuse is left out.
    A structure edition refused at record time also drops each batch
    edition whose publish check was given it, and the rules run again on
    the rest, until a pass drops none. An edition another writer's journal
    repair recorded already, with the same ``manifest_sha256``, counts as
    recorded.

    Args:
        writer: The catalog's Inventory writer.
        sealed: Each sealed working copy with its edition.
        refused: Filled with each product refused at record time.

    Returns:
        Each recorded product's edition.
    """
    at_record: dict[str, InventoryRuleError | EditionCheckError] = {}

    def change(full: FullInventory | None) -> Change:
        at_record.clear()
        leaves = _batch_leaves(writer, full, sealed, at_record)
        if not leaves:
            raise _NothingToRecordError
        return Change(leaves=leaves)

    products = ", ".join(done.wc.product_id for done in sealed)
    try:
        writer.commit(change, kind="seal", reason=f"seal {products}")
    except _NothingToRecordError:
        pass
    except InventoryContentionError as exc:
        # The journal entries stay, so each product's next working_copy() records its edition.
        for done in sealed:
            msg = f"{exc}; the edition {done.wc.edition_id} is recorded at the next working_copy()"
            refused[done.wc.product_id] = InventoryContentionError(msg)
        return {}
    except InventoryWriteRefusedError as exc:
        # The journal entries stay, so a later working_copy() records each edition.
        for done in sealed:
            msg = f"{exc}; the edition {done.wc.edition_id} stays unrecorded until a working_copy() can record it"
            refused[done.wc.product_id] = InventoryWriteRefusedError(msg)
        return {}
    recorded: dict[str, Edition] = {}
    for done in sealed:
        exc = at_record.get(done.wc.product_id)
        if exc is None:
            done.wc._recorded()
            recorded[done.wc.product_id] = done.sealed.edition
        else:
            done.wc._unrecorded(exc)
            refused[done.wc.product_id] = exc
    return recorded


class Catalog:
    """A directory of products, each a chain of immutable editions (`dcat:Catalog`).

    Create one with `create`, which also reopens an existing catalog for
    writing, or open one read-only with `open`. A catalog reads its Inventory
    through `sdmxlib.inventory.LiveInventory`, so it sees live editions only;
    `refresh` re-reads it. Every view it returns shares one DuckDB engine,
    and `close` releases them all.

    Queries run on a product's view: ``cat.product(id).view().query(...)``.
    Search and facets across products are not yet supported.
    """

    _root: Path
    _writer: InventoryWriter | None
    _location: BlobLocation | None
    _inventory: Path | InventoryStorage
    _catalog_id: str | None
    _live: LiveInventory | None
    _engine: sdmxlib.EditionEngine | None
    _registries: list[sdmxlib.EditionRegistry]
    _stores: list[_Store]
    _closed: bool

    def __init__(self, *args: object, _opening: _Opening | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError naming `open`, `create` and `temporary_catalog`."""
        if _opening is None or args or kwargs:
            msg = (
                "Catalog(path) and Catalog(':memory:') were removed in statcatlib 0.12. Open a catalog "
                "read-only with Catalog.open(path), create one (or reopen it for writing) with "
                "Catalog.create(path, exist_ok=True), or use statcatlib.testing.temporary_catalog() for a "
                "throwaway one."
            )
            raise TypeError(msg)
        self._root = _opening.root
        self._writer = _opening.writer
        self._location = _opening.location
        self._inventory = _opening.root if self._location is None else BlobInventoryStorage(self._location)
        self._catalog_id = _opening.catalog_id
        self._live = None
        self._engine = None
        self._registries = []
        self._stores = []
        self._closed = False

    @classmethod
    def _new(cls, opening: _Opening) -> Self:
        cat = cls(_opening=opening)
        cat.refresh()
        return cat

    @classmethod
    def create(cls, path: str | Path, *, exist_ok: bool = False) -> Self:
        """Create a catalog at *path*, or with *exist_ok* reopen the one there for writing.

        A missing or empty directory gets ``catalog.json``, holding a UUIDv7
        ``catalog_id`` and ``restore_epoch`` 0, and nothing else: the first
        seal writes Inventory version 1. The path created is logged at INFO,
        since a mistyped path with ``exist_ok=True`` creates a new, empty
        catalog.

        Args:
            path: The catalog directory.
            exist_ok: Reopen an existing catalog for writing, writing nothing.

        Returns:
            The catalog, writable: its products' `Product.working_copy` works.

        Raises:
            FileExistsError: *path* is a catalog and *exist_ok* is false.
            TypeError: *path* is a `BlobLocation`, and `create` writes a local catalog.

        A legacy layout or other files at *path*, or a filesystem without
        hard links, raise `IncompatibleCatalogError`.
        """
        if isinstance(path, BlobLocation):
            msg = f"Catalog.create writes a local catalog; open {path.uri} with Catalog.open(location)"
            raise TypeError(msg)
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        writer = InventoryWriter(root)
        if (root / CATALOG_NAME).is_file():
            if not exist_ok:
                msg = f"{root} is a catalog already; pass exist_ok=True to open it for writing"
                raise FileExistsError(msg)
            return cls._new(_Opening(root=root, writer=writer))
        root.mkdir(parents=True, exist_ok=True)
        _probe_hard_links(root)
        try:
            writer.write_catalog_json()
        except FileExistsError:
            if not exist_ok:
                raise
        else:
            log.info("created the catalog %s", root)
        return cls._new(_Opening(root=root, writer=writer))

    @classmethod
    def open(cls, path: str | Path | BlobLocation) -> Self:
        """Open the catalog at *path* read-only.

        Reads ``catalog.json`` and the Inventory, and writes nothing to the
        catalog: no repair, no DDL. A catalog with no Inventory version yet has
        no products.

        A `BlobLocation` opens a catalog in object storage. A library call never
        takes a credential from the environment, so the caller builds the store
        and the DuckDB secrets. `Product.view` writes the live edition's files
        and its receive stamp under ``<cache_dir>/<catalog_id>``, and nothing
        else writes there.

        Args:
            path: The catalog directory, or a `BlobLocation`.

        Returns:
            The catalog. Its products' `Product.working_copy` raises; open it
            with `create` to write.

        Raises:
            FileNotFoundError: *path* is not a catalog.
            ValueError: *path* is a URI string, or a `BlobLocation` with no
                ``cache_dir``.

        A legacy layout or other files at *path* raise `IncompatibleCatalogError`,
        as does a ``catalog_id`` in object storage that cannot name a directory.
        """
        if isinstance(path, BlobLocation):
            return cls._open_blob(path)
        if isinstance(path, str) and _URI.match(path):
            msg = (
                f"Catalog.open({path!r}) takes a directory; a catalog in object storage needs a BlobLocation, "
                "holding a store you built, so a library call never takes a credential from the environment"
            )
            raise ValueError(msg)
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        if not (root / CATALOG_NAME).is_file():
            msg = f"{root} is not a catalog (no {CATALOG_NAME}); create one with Catalog.create({str(path)!r})"
            raise FileNotFoundError(msg)
        InventoryWriter(root).catalog_document()
        return cls._new(_Opening(root=root, writer=None))

    @classmethod
    def _open_blob(cls, location: BlobLocation) -> Self:
        """`open` over object storage: the cache root comes from ``catalog_id``, which is read once."""
        if location.cache_dir is None:
            msg = f"Catalog.open({location.uri!r}) needs a BlobLocation with cache_dir, where it keeps pulled editions"
            raise ValueError(msg)
        catalog_id = InventoryWriter(BlobInventoryStorage(location)).catalog_document().catalog_id
        if not _SAFE_ID.fullmatch(catalog_id):
            msg = f"{location.uri} has catalog_id {catalog_id!r}, which cannot name a directory under cache_dir"
            raise IncompatibleCatalogError(msg)
        root = Path(location.cache_dir).absolute() / catalog_id
        return cls._new(_Opening(root=root, writer=None, location=location, catalog_id=catalog_id))

    # -- state -----------------------------------------------------------

    @property
    def _name(self) -> str:
        """The catalog's directory or URI, for messages."""
        return str(self._root) if self._location is None else self._location.uri

    def _require_open(self) -> None:
        if self._closed:
            msg = f"the catalog {self._name} is closed"
            raise RuntimeError(msg)

    @property
    def writable(self) -> bool:
        """Whether the catalog came from `create`, so its products can be written."""
        return self._writer is not None

    @property
    def catalog_id(self) -> str:
        """The catalog's identity, a UUIDv7 from ``catalog.json``."""
        return self._catalog_id or InventoryWriter(self._inventory).catalog_document().catalog_id

    def refresh(self) -> None:
        """Re-read the Inventory, so `product` and `products` see the newest live editions.

        A view taken before stays on its edition; call ``product(id).view()``
        again to follow the live edition. On a catalog in object storage, a
        refresh after the first version is seen reads ``CURRENT``, one root,
        one probe, and later only the leaves that changed.
        """
        self._require_open()
        writer = InventoryWriter(self._inventory)
        if (self._location is None or self._live is None) and not writer.has_version():
            self._live = None
            return
        if self._live is None:
            self._live = LiveInventory(writer.storage.read)
        self._live.refresh()

    @property
    def description(self) -> CatalogDescription | None:
        """The catalog's description, as of the last `refresh`; ``None`` when none was recorded.

        ``CatalogAdmin.describe`` records it in the Inventory root's
        ``extensions.statcat``, and every later version carries it. It holds
        the catalog-level fields only; the datasets come from the products'
        live editions.
        """
        self._require_open()
        if self._live is None:
            return None
        raw = InventoryWriter(self._inventory).storage.read(root_path(self._live.seq))
        if raw is None:
            return None
        return description_from_root(decode_root(raw).extensions)

    def products(self) -> list[str]:
        """The products with a live edition, sorted."""
        self._require_open()
        return [] if self._live is None else self._live.products()

    def product(self, product_id: str) -> Product:
        """The product *product_id*, which need not exist yet.

        Args:
            product_id: The product's id. It names a directory: letters,
                digits, ``.``, ``_`` and ``-``, starting with a letter or digit.

        Returns:
            The product. An invalid *product_id* raises `ValueError`.
        """
        self._require_open()
        return Product._for(self, _check_product_id(product_id))

    def _require_writer(self) -> InventoryWriter:
        """The Inventory writer, or `RuntimeError` for a catalog from `open`."""
        self._require_open()
        if self._writer is None:
            fix = (
                f"open it for writing with Catalog.create({str(self._root)!r}, exist_ok=True)"
                if self._location is None
                else "a catalog in object storage is read through Catalog.open only"
            )
            msg = f"the catalog {self._name} was opened read-only with Catalog.open; {fix}"
            raise RuntimeError(msg)
        return self._writer

    def seal(
        self,
        working_copies: Iterable[WorkingCopy],
        *,
        checks: Iterable[Callable[[EditionView], Iterable[Finding]]] = (),
    ) -> SealOutcomes:
        """Seal several products' working copies and record them in one Inventory version.

        It runs `WorkingCopy.seal`'s steps in four passes. The structure pass
        pre-checks each structure product's working copy (journal, *checks*,
        content, fast-forward) and seals each that passed. The
        structure-check pass runs each sealed structure edition's publish
        check, given the batch's other structure editions in place of their
        products' recorded ones, and drops each edition with a finding until
        a pass drops none. The data pass pre-checks every data product's
        working copy before any of them is sealed, then seals each that
        passed, and runs the publish check of each data edition with
        partials, given the batch's structure editions in place of their
        products' recorded ones. Last it records every sealed edition in one
        Inventory commit, leaving out each product whose record sdmxlib's
        rules refuse, and each edition whose publish check was given a
        left-out one. A refusal in one product never stops another's seal.

        A crash during the batch can leave its products recorded across
        several Inventory versions: each sealed, unrecorded edition keeps its
        journal entry, and its product's next ``working_copy()`` records it.

        Args:
            working_copies: The working copies, one per product, each from
                this catalog.
            checks: Seal checks (`statcatlib.checks.SealCheck`), run against
                every working copy's view.

        Returns:
            Each product's outcome. An empty batch writes nothing.

        Before anything is sealed, a working copy of another catalog, or two
        of one product, raise `ValueError`, and a sealed, discarded or closed
        working copy raises `WorkingCopyClosedError`. A catalog opened
        read-only with `Catalog.open` raises `RuntimeError`.
        """
        writer = self._require_writer()
        batch = self._check_batch(working_copies)
        refused: dict[str, Exception] = {}
        sealed = _seal_each(writer, batch, tuple(checks), refused)
        recorded = _record_batch(writer, sealed, refused) if sealed else {}
        return SealOutcomes(sealed=recorded, refused=refused)

    def _check_batch(self, working_copies: Iterable[WorkingCopy]) -> list[WorkingCopy]:
        """The batch as a list, once each working copy is open, of this catalog, and alone for its product."""
        batch = list(working_copies)
        seen: set[str] = set()
        for wc in batch:
            wc._live()
            if wc._writer.root != self._root:
                msg = (
                    f"the working copy of {wc.product_id} belongs to the catalog {wc._writer.root}; "
                    f"this catalog is {self._root}"
                )
                raise ValueError(msg)
            if wc.product_id in seen:
                msg = f"two working copies of {wc.product_id} in one batch; a product seals once per batch"
                raise ValueError(msg)
            seen.add(wc.product_id)
        return batch

    def _engine_for_views(self) -> sdmxlib.EditionEngine:
        if self._engine is None:
            secrets = () if self._location is None else self._location.secrets
            self._engine = sdmxlib.EditionEngine(secrets=secrets)
        return self._engine

    def close(self) -> None:
        """Close every view this catalog returned, and its engine."""
        if self._closed:
            return
        self._closed = True
        for store in self._stores:
            store._close(lambda: RuntimeError(f"the catalog {self._name} is closed"))
        for registry in self._registries:
            registry.close()
        if self._engine is not None:
            self._engine.close()
        self._stores.clear()
        self._registries.clear()
        self._engine = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        mode = "writable" if self.writable else "read-only"
        return f"<Catalog {self._name} ({mode})>"

    # -- refused and moved calls -----------------------------------------

    def search(self, *args: object, **kwargs: object) -> NoReturn:
        """Not yet supported across products: raises `NotImplementedError`; search one product's view."""
        del args, kwargs
        msg = "search is not yet supported across products; use cat.product(id).view().search(...)"
        raise NotImplementedError(msg)

    def facets(self, *args: object, **kwargs: object) -> NoReturn:
        """Not yet supported across products: raises `NotImplementedError`; count one product's view."""
        del args, kwargs
        msg = "facets is not yet supported across products; use cat.product(id).view().facets(...)"
        raise NotImplementedError(msg)

    def __getattr__(self, name: str) -> NoReturn:
        """Name the replacement of a query method that moved to `EditionView`.

        Args:
            name: The attribute asked for.

        Raises:
            AttributeError: Always; for a moved method, naming
                ``product(id).view().<name>``.
        """
        if name in VIEW_ATTRIBUTES:
            msg = f"Catalog.{name} moved to the edition view in statcatlib 0.12: use cat.product(id).view().{name}"
            raise AttributeError(msg)
        msg = f"{type(self).__name__!r} object has no attribute {name!r}"
        raise AttributeError(msg)


class Product:
    """One product of a catalog: its live edition, and its working copy.

    `Catalog.product` returns one.
    """

    _catalog: Catalog
    _product_id: str

    def __init__(self, *args: object, _of: tuple[Catalog, str] | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError: `Catalog.product` returns products."""
        if _of is None or args or kwargs:
            msg = "get a product with Catalog.open(path).product(id)"
            raise TypeError(msg)
        self._catalog, self._product_id = _of

    @classmethod
    def _for(cls, catalog: Catalog, product_id: str) -> Self:
        return cls(_of=(catalog, product_id))

    @property
    def product_id(self) -> str:
        """The product's id."""
        return self._product_id

    @property
    def _dir(self) -> Path:
        return self._catalog._root / "products" / self._product_id

    def __repr__(self) -> str:
        return f"<Product {self._product_id} of {self._catalog._name}>"

    def _head(self) -> LiveEdition:
        cat = self._catalog
        cat._require_open()
        live = cat._live
        found = None
        if live is not None:
            with contextlib.suppress(KeyError):
                found = live.head(self._product_id)
        if found is None:
            msg = f"{self._product_id} has no live edition"
            if cat._location is None:
                msg += f"; CatalogAdmin.editions({self._product_id!r}) lists its editions in every state"
            raise NoLiveEditionError(msg)
        return found

    def _checked_copy(self, location: BlobLocation, live: LiveEdition) -> tuple[Path, str]:
        """The live edition's cached directory and its store URI, pulled when absent and checked on open.

        Args:
            location: The catalog.
            live: The live edition.

        Returns:
            The directory, and the URI of the edition in the store.

        Raises:
            IncompatibleCatalogError: Anything `_cached_edition` refuses, or a
                finding from `sdmxlib.edition.check_on_open`.
            ValueError: The edition has data files, and the location holds no
                ``secrets`` for DuckDB to read them with.
        """
        cat = self._catalog
        edition_dir, manifest = _cached_edition(
            location, cat._root, self._product_id, live.edition_id, live.manifest_sha256
        )
        if not location.secrets and any(entry.root == "data" for entry in manifest.files):
            msg = (
                f"{self._product_id}'s edition {live.edition_id} has data files, and the BlobLocation of "
                f"{location.uri} holds no secrets; give it the StoreSecret DuckDB reads them with"
            )
            raise ValueError(msg)
        remote = f"{location.uri.rstrip('/')}/products/{self._product_id}/{live.edition_id}/"
        report = check_on_open(
            edition_dir,
            stamps=cat._root / "verified",
            remote=remote,
            engine=cat._engine_for_views(),
            references=bool(manifest.bindings),
            extensions={STATCAT_SECTION: _statcat_check()},
        )
        if not report.ok:
            found = "; ".join(str(finding) for finding in report.findings)
            msg = f"{self._product_id}'s live edition {live.edition_id} failed its {report.at} check: {found}"
            raise IncompatibleCatalogError(msg)
        return edition_dir, remote

    def view(self, *, embedding_model: EmbeddingModel | None = None, query_prefix: str = "") -> EditionView:
        """A read-only view of the product's live edition, as of the catalog's last `Catalog.refresh`.

        It attaches the edition to the catalog's engine with
        ``LocalRegistry.edition``, which reads ``manifest.json`` once, checks
        its SHA-256 against the one the Inventory records, and checks its
        store schema. The view stays on this edition after a refresh or a
        rollback.

        On a catalog in object storage, the first view reads the manifest and
        compares its SHA-256 before it reads any other file of the edition, pulls
        the catalog files to ``cache_dir``, and runs the ``receive`` check; later
        views run ``open``. Observations stay in the store. A cached edition
        stays after a rollback or a withdrawal until an operator deletes it.

        Args:
            embedding_model: The encoder the index was built with, for vector
                and hybrid search. BM25 search needs none.
            query_prefix: Prepended to every query before embedding; the one
                the index recorded when empty.

        Returns:
            The view.

        Raises:
            IncompatibleCatalogError: The edition's ``manifest.json`` is
                missing, unreadable, or not the one the Inventory recorded
                (sdmxlib's `ManifestError`, chained). Its ``statcat`` schema at
                another version, or a search index this build does not read,
                raise it too, and a product with no live edition raises
                `NoLiveEditionError`. On a catalog in object storage, so do a
                cached manifest that differs, a file that differs from the
                manifest, an unsafe id or path, and a check finding.
        """
        live = self._head()
        cat = self._catalog
        if cat._location is None:
            edition_dir, remote = self._dir / live.edition_id, None
        else:
            edition_dir, remote = self._checked_copy(cat._location, live)
        try:
            registry = sdmxlib.LocalRegistry.edition(
                edition_dir, remote=remote, engine=cat._engine_for_views(), manifest_sha256=live.manifest_sha256
            )
        except ManifestError as exc:
            msg = (
                f"{self._product_id}'s live edition {live.edition_id} does not hold the manifest.json the Inventory "
                f"recorded ({exc}); run `statcat verify-catalog` on {cat._name}"
            )
            raise IncompatibleCatalogError(msg) from exc
        opened = False
        try:
            store = _Store._over(
                registry.connection.cursor,
                catalog_name=registry.catalog_name,
                search_dir=edition_dir / "search",
                writable=False,
                registry=registry,
                embedding_model=embedding_model,
                query_prefix=query_prefix,
            )
            opened = True
        finally:
            if not opened:
                registry.close()
        cat._registries.append(registry)
        cat._stores.append(store)
        return EditionView._over(store)

    def editions(self) -> list[Edition]:
        """The product's live editions, each with the ``previous`` its manifest records.

        The public catalog holds live editions only, one per store schema;
        ``CatalogAdmin.editions`` lists every state. On a catalog in object
        storage it reads the manifest alone, compares its SHA-256 with the
        Inventory's and pulls nothing; a difference raises
        `IncompatibleCatalogError`.
        """
        try:
            live = self._head()
        except NoLiveEditionError:
            return []
        location = self._catalog._location
        if location is None:
            manifest = read_manifest(self._dir / live.edition_id)
        else:
            _, manifest = _read_remote_manifest(location, self._product_id, live.edition_id, live.manifest_sha256)
        return [
            Edition(
                edition_id=live.edition_id,
                product_id=self._product_id,
                previous=manifest.previous,
                manifest_sha256=live.manifest_sha256,
            )
        ]

    def working_copy(
        self,
        *,
        kind: Kind | None = None,
        lifecycle: Lifecycle = "revisable",
        resume: bool = False,
        embedding_model: EmbeddingModel | None = None,
        document_prefix: str = "",
        query_prefix: str = "",
    ) -> WorkingCopy:
        """Start the product's working copy on its live edition, or resume the unsealed one.

        It settles the product's seal journal first (the journal rule, in
        `statcatlib.store.working_copy`), so an edition a crash left sealed
        and unrecorded is recorded before the new one picks its base.

        Args:
            kind: ``data``, or ``structure`` for a product of full artefacts
                only (`WorkingCopy.fill_structures`). ``None`` takes the kind
                the product is recorded with, or the one a resumed working
                copy was started with, and ``data`` for a new product. A
                product keeps the kind it was first recorded with.
            lifecycle: ``revisable`` or ``final``. A product keeps the
                lifecycle it was first recorded with.
            resume: Reopen the product's unsealed edition in place of
                starting one.
            embedding_model: The encoder `WorkingCopy.seal` embeds with.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.

        Returns:
            The working copy.

        A catalog opened read-only with `Catalog.open` raises `RuntimeError`.
        A product recorded with another lifecycle raises `InventoryRuleError`
        (``rule="lifecycle"``); one recorded with another kind, or resumed
        with another kind than it was started with, raises it with
        ``rule="kind"`` before anything is built; and one with an unsealed
        edition raises `WorkingCopyExistsError` unless *resume* is true.
        """
        writer = self._catalog._require_writer()
        return WorkingCopy._start(
            writer,
            self._product_id,
            lifecycle=lifecycle,
            kind=kind,
            resume=resume,
            embedding_model=embedding_model,
            document_prefix=document_prefix,
            query_prefix=query_prefix,
        )

    def rollback(self, *args: object, **kwargs: object) -> NoReturn:
        """Rollback is an operational change: raises `AttributeError` naming ``CatalogAdmin.rollback``."""
        del args, kwargs
        msg = f"a product has no rollback; use CatalogAdmin.open(path).rollback({self._product_id!r}, reason=...)"
        raise AttributeError(msg)

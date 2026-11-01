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
`WorkingCopy.seal` turns into the next edition.

Callers reach storage only through this API: no method takes or returns a path
inside the catalog.
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
from sdmxlib.edition import read_manifest
from sdmxlib.inventory import CATALOG_NAME, LiveEdition, LiveInventory, local_fetch

from statcatlib.store.inventory import InventoryWriter
from statcatlib.store.store import VIEW_ATTRIBUTES, EditionView, IncompatibleCatalogError, _Store
from statcatlib.store.working_copy import Edition, WorkingCopy, manifest_sha256

if TYPE_CHECKING:
    from sdmxlib.edition import Lifecycle

    from statcatlib.store.store import EmbeddingModel

__all__ = ["Catalog", "Edition", "NoLiveEditionError", "Product"]

log = logging.getLogger(__name__)

#: Where `docs/upgrading.md` gives the re-ingest steps a refused layout needs.
_UPGRADING = "the re-ingest steps in docs/upgrading.md (https://statcat-suite.github.io/statcatlib/upgrading/)"

#: What a product id may be: it names a directory, so no separator and no dot path.
_PRODUCT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class NoLiveEditionError(LookupError):
    """The product has no live edition, so there is nothing for a public reader to view."""


@frozen
class _Opening:
    """What `Catalog.open` and `Catalog.create` hand the constructor."""

    root: Path
    writer: InventoryWriter | None


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
    if not _PRODUCT_ID.fullmatch(product_id):
        msg = (
            f"{product_id!r} is not a product id: it names a directory, so it starts with a letter or digit "
            "and holds only letters, digits, '.', '_' and '-'"
        )
        raise ValueError(msg)
    return product_id


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
        self._live = None
        self._engine = None
        self._registries = []
        self._stores = []
        self._closed = False

    @classmethod
    def _new(cls, root: Path, writer: InventoryWriter | None) -> Self:
        cat = cls(_opening=_Opening(root=root, writer=writer))
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

        A legacy layout or other files at *path*, or a filesystem without
        hard links, raise `IncompatibleCatalogError`.
        """
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        writer = InventoryWriter(root)
        if (root / CATALOG_NAME).is_file():
            if not exist_ok:
                msg = f"{root} is a catalog already; pass exist_ok=True to open it for writing"
                raise FileExistsError(msg)
            return cls._new(root, writer)
        root.mkdir(parents=True, exist_ok=True)
        _probe_hard_links(root)
        try:
            writer.write_catalog_json()
        except FileExistsError:
            if not exist_ok:
                raise
        else:
            log.info("created the catalog %s", root)
        return cls._new(root, writer)

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open the catalog at *path* read-only.

        Reads ``catalog.json`` and the Inventory, and writes nothing: no
        repair, no DDL. A catalog with no Inventory version yet has no
        products.

        Args:
            path: The catalog directory.

        Returns:
            The catalog. Its products' `Product.working_copy` raises; open it
            with `create` to write.

        Raises:
            FileNotFoundError: *path* is not a catalog.

        A legacy layout or other files at *path* raise `IncompatibleCatalogError`.
        """
        root = Path(path).absolute()
        _refuse_legacy_layout(root)
        if not (root / CATALOG_NAME).is_file():
            msg = f"{root} is not a catalog (no {CATALOG_NAME}); create one with Catalog.create({str(path)!r})"
            raise FileNotFoundError(msg)
        InventoryWriter(root).catalog_document()
        return cls._new(root, None)

    # -- state -----------------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            msg = f"the catalog {self._root} is closed"
            raise RuntimeError(msg)

    @property
    def writable(self) -> bool:
        """Whether the catalog came from `create`, so its products can be written."""
        return self._writer is not None

    @property
    def catalog_id(self) -> str:
        """The catalog's identity, a UUIDv7 from ``catalog.json``."""
        return InventoryWriter(self._root).catalog_document().catalog_id

    def refresh(self) -> None:
        """Re-read the Inventory, so `product` and `products` see the newest live editions.

        A view taken before stays on its edition; call ``product(id).view()``
        again to follow the live edition.
        """
        self._require_open()
        writer = InventoryWriter(self._root)
        if not writer.has_version():
            self._live = None
            return
        if self._live is None:
            self._live = LiveInventory(local_fetch(self._root))
        self._live.refresh()

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

    def _engine_for_views(self) -> sdmxlib.EditionEngine:
        if self._engine is None:
            self._engine = sdmxlib.EditionEngine()
        return self._engine

    def close(self) -> None:
        """Close every view this catalog returned, and its engine."""
        if self._closed:
            return
        self._closed = True
        for store in self._stores:
            store._close(lambda: RuntimeError(f"the catalog {self._root} is closed"))
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
        return f"<Catalog {self._root} ({mode})>"

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
        return f"<Product {self._product_id} of {self._catalog._root}>"

    def _head(self) -> LiveEdition:
        cat = self._catalog
        cat._require_open()
        live = cat._live
        found = None
        if live is not None:
            with contextlib.suppress(KeyError):
                found = live.head(self._product_id)
        if found is None:
            msg = (
                f"{self._product_id} has no live edition; CatalogAdmin.editions({self._product_id!r}) lists "
                "its editions in every state"
            )
            raise NoLiveEditionError(msg)
        return found

    def view(self, *, embedding_model: EmbeddingModel | None = None, query_prefix: str = "") -> EditionView:
        """A read-only view of the product's live edition, as of the catalog's last `Catalog.refresh`.

        It checks the edition's ``manifest.json`` against the SHA-256 the
        Inventory records, then attaches the edition to the catalog's engine
        (``LocalRegistry.edition``), which checks the manifest's store schema.
        The view stays on this edition after a refresh or a rollback.

        Args:
            embedding_model: The encoder the index was built with, for vector
                and hybrid search. BM25 search needs none.
            query_prefix: Prepended to every query before embedding; the one
                the index recorded when empty.

        Returns:
            The view.

        Raises:
            IncompatibleCatalogError: The edition's ``manifest.json`` is not
                the one the Inventory recorded. Its ``statcat`` schema at
                another version, or a search index this build does not read,
                raise it too, and a product with no live edition raises
                `NoLiveEditionError`.
        """
        live = self._head()
        edition_dir = self._dir / live.edition_id
        try:
            found = manifest_sha256(edition_dir)
        except FileNotFoundError:
            found = "missing"
        if found != live.manifest_sha256:
            msg = (
                f"{self._product_id}'s live edition {live.edition_id} has a manifest.json ({found}) that is not "
                f"the one the Inventory recorded ({live.manifest_sha256}); run `statcat verify-catalog` on "
                f"{self._catalog._root}"
            )
            raise IncompatibleCatalogError(msg)
        cat = self._catalog
        registry = sdmxlib.LocalRegistry.edition(edition_dir, engine=cat._engine_for_views())
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
        ``CatalogAdmin.editions`` lists every state.
        """
        try:
            live = self._head()
        except NoLiveEditionError:
            return []
        manifest = read_manifest(self._dir / live.edition_id)
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
            lifecycle: ``revisable`` or ``final``. A product keeps the
                lifecycle it was first recorded with.
            resume: Reopen the product's unsealed edition in place of
                starting one.
            embedding_model: The encoder `WorkingCopy.seal` embeds with.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.

        Returns:
            The working copy.

        Raises:
            RuntimeError: The catalog was opened read-only with `Catalog.open`.

        A product recorded with another lifecycle raises `InventoryRuleError`
        (``rule="lifecycle"``), and one with an unsealed edition raises
        `WorkingCopyExistsError` unless *resume* is true.
        """
        cat = self._catalog
        cat._require_open()
        writer = cat._writer
        if writer is None:
            msg = (
                f"the catalog {cat._root} was opened read-only with Catalog.open; open it for writing with "
                f"Catalog.create({str(cat._root)!r}, exist_ok=True)"
            )
            raise RuntimeError(msg)
        return WorkingCopy._start(
            writer,
            self._product_id,
            lifecycle=lifecycle,
            resume=resume,
            embedding_model=embedding_model,
            document_prefix=document_prefix,
            query_prefix=query_prefix,
        )

    def rollback(self, *args: object, **kwargs: object) -> NoReturn:
        """Rollback is an operational change: raises `AttributeError` naming ``CatalogAdmin.rollback``."""
        del args, kwargs
        msg = (
            f"a product has no rollback; use CatalogAdmin.open(path).rollback({self._product_id!r}, reason=...), "
            "which arrives in statcatlib's Phase 2a S3"
        )
        raise AttributeError(msg)

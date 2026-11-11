"""`EmbargoReader`: views of a catalog's embargoed editions, for the preview and pre-materialisation (Phase 5 D12).

The public `Catalog` reads its Inventory through `sdmxlib.inventory.LiveInventory`,
so it never sees an embargoed entry. An `EmbargoReader` reads every state through
`sdmxlib.inventory.FullInventory` and opens an `embargoed` or `live` edition as an
`EditionView`, so statcat-serve's preview deployment can compare a release with what
it replaces, and its pre-materialisation job can compute results before the release.

It is constructed explicitly with `EmbargoReader.open`. No attribute of `Catalog`,
`Product` or `EditionView` returns one (spec §6.4), and it holds its own
`FullInventory` and `sdmxlib.EditionEngine`, shared with no `Catalog`.

Over a `BlobLocation` a view runs `Product.view`'s steps (`catalog._open_view`): it
reads the manifest first, pulls the catalog files to ``<cache_dir>/<catalog_id>/``,
and runs ``check_on_open`` with its receive stamps in
``<cache_dir>/<catalog_id>/embargo-verified``. That ``cache_dir`` holds embargoed
bytes, so each deployment that opens one keeps its own inside the §10.1 boundary and
never shares it with a public reader. The module imports nothing from the ``blob``
extra at module level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, get_args

import sdmxlib
from sdmxlib.inventory import FullInventory, State

from statcatlib.admin import EditionEntry, _edition_entry, _leaf
from statcatlib.catalog import _open_read_only, _open_view
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryRuleError, InventoryWriter

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from statcatlib.catalog import _Opening
    from statcatlib.store.inventory import InventoryStorage
    from statcatlib.store.store import EditionView, EmbeddingModel, _Store

__all__ = ["EmbargoReader"]

#: Every Inventory state, in sdmxlib's order.
_STATES: tuple[State, ...] = get_args(State.__value__)

#: The states `EmbargoReader.view` opens.
_VIEWABLE: frozenset[State] = frozenset({"embargoed", "live"})


class EmbargoReader:
    """A read-only reader of a catalog's embargoed and live editions, for the preview and pre-materialisation.

    Open one with `open`; `refresh` re-reads the Inventory, `editions` lists
    entries by state, and `view` opens one edition. `close` closes every view
    it returned, and its engine.
    """

    _root: Path
    _location: BlobLocation | None
    _inventory: Path | InventoryStorage
    _full: FullInventory | None
    _engine: sdmxlib.EditionEngine | None
    _registries: list[sdmxlib.EditionRegistry]
    _stores: list[_Store]
    _closed: bool

    def __init__(self, *args: object, _opening: _Opening | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError naming `open`."""
        if _opening is None or args or kwargs:
            msg = "open an embargo reader with EmbargoReader.open(path)"
            raise TypeError(msg)
        self._root = _opening.root
        self._location = _opening.location
        self._inventory = _opening.root if self._location is None else BlobInventoryStorage(self._location)
        self._full = None
        self._engine = None
        self._registries = []
        self._stores = []
        self._closed = False

    @classmethod
    def open(cls, location: str | Path | BlobLocation) -> Self:
        """Open the catalog at *location* to read its editions in every state.

        It reads ``catalog.json`` and the Inventory, as `Catalog.open` does, and
        writes nothing to the catalog. Over a `BlobLocation`, `view` writes the
        edition's catalog files and the receive stamps under
        ``<cache_dir>/<catalog_id>``. That directory holds embargoed bytes: give
        the reader a ``cache_dir`` of its own, never one a public `Catalog` uses.

        Args:
            location: The catalog directory, or a `BlobLocation`.

        Returns:
            The reader, holding the newest Inventory version.

        It refuses what `Catalog.open` refuses: a *location* that is not a
        catalog raises `FileNotFoundError`, a URI string or a `BlobLocation` with
        no ``cache_dir`` raises `ValueError`, and a legacy layout or other files,
        or a ``catalog_id`` in object storage that cannot name a directory, raise
        `IncompatibleCatalogError`.
        """
        return cls(_opening=_open_read_only(location, opener="EmbargoReader.open")).refresh()

    @property
    def _name(self) -> str:
        """The catalog's directory or URI, for messages."""
        return str(self._root) if self._location is None else self._location.uri

    def _require_open(self) -> None:
        if self._closed:
            msg = f"the embargo reader of {self._name} is closed"
            raise RuntimeError(msg)

    def refresh(self) -> Self:
        """Re-read the Inventory, so `editions` and `view` see the newest version.

        A view taken before stays on its edition. On a catalog in object storage,
        a refresh after the first version is seen reads ``CURRENT``, one root, one
        probe, and later only the leaves that changed.

        Returns:
            This reader.
        """
        self._require_open()
        if self._full is None:
            writer = InventoryWriter(self._inventory)
            if not writer.has_version():
                return self
            self._full = FullInventory(writer.storage.read)
        self._full.refresh()
        return self

    def editions(self, *, states: Collection[State] = ("embargoed",)) -> list[EditionEntry]:
        """The editions in *states*, as the Inventory version the reader holds records them.

        Args:
            states: The states to list: any of ``embargoed``, ``live``,
                ``superseded`` and ``withdrawn``.

        Returns:
            The editions, by product id and then in each product's id order;
            empty before the first Inventory version.

        Raises:
            ValueError: A state in *states* is none of the four.
        """
        self._require_open()
        wanted = set(states)
        unknown = sorted(wanted.difference(_STATES))
        if isinstance(states, str) or unknown:
            msg = f"states takes a collection of {', '.join(_STATES)}; got {states!r}"
            raise ValueError(msg)
        if self._full is None:
            return []
        leaves = self._full.leaves()
        return [
            _edition_entry(product_id, entry)
            for product_id in sorted(leaves)
            for entry in leaves[product_id].editions
            if entry.state in wanted
        ]

    def view(
        self,
        product_id: str,
        edition_id: str,
        *,
        embedding_model: EmbeddingModel | None = None,
        query_prefix: str = "",
    ) -> EditionView:
        """A read-only view of one embargoed or live edition, as of the last `refresh`.

        It runs `Product.view`'s steps over the entry the Inventory records:
        ``LocalRegistry.edition`` checks the manifest's SHA-256 against the
        entry's, and over a `BlobLocation` the manifest is read first, the
        catalog files are pulled to ``cache_dir`` and the ``receive`` check
        runs, with ``open`` on later views. Observations stay in the store.

        Args:
            product_id: The product.
            edition_id: The edition.
            embedding_model: The encoder the index was built with, for vector
                and hybrid search. BM25 search needs none.
            query_prefix: Prepended to every query before embedding; the one
                the index recorded when empty.

        Returns:
            The view.

        Raises:
            InventoryRuleError: The Inventory records no such product, or the
                product records no such edition (``rule="unknown"``).
            ValueError: The edition is ``superseded`` or ``withdrawn``.

        `Product.view`'s other errors apply: `IncompatibleCatalogError` for a
        manifest that is missing, unreadable or not the recorded one, and on a
        catalog in object storage for a file that differs from the manifest or a
        check finding; `ValueError` for an edition with data files whose
        `BlobLocation` holds no ``secrets``.
        """
        self._require_open()
        leaf = _leaf(self._full, product_id)
        entry = next((e for e in leaf.editions if e.edition_id == edition_id), None)
        if entry is None:
            msg = f"{product_id} records no edition {edition_id}"
            raise InventoryRuleError(msg, rule="unknown", product=product_id, edition=edition_id)
        if entry.state not in _VIEWABLE:
            msg = (
                f"{product_id}'s edition {edition_id} is {entry.state}; an embargo reader opens an "
                "embargoed or live edition only"
            )
            raise ValueError(msg)
        view, registry, store = _open_view(
            self._engine_for_views,
            self._root,
            self._location,
            product_id,
            edition_id,
            manifest_sha256=entry.manifest_sha256,
            stamps=self._root / "embargo-verified",
            state=entry.state,
            embedding_model=embedding_model,
            query_prefix=query_prefix,
        )
        self._registries.append(registry)
        self._stores.append(store)
        return view

    def _engine_for_views(self) -> sdmxlib.EditionEngine:
        if self._engine is None:
            secrets = () if self._location is None else self._location.secrets
            self._engine = sdmxlib.EditionEngine(secrets=secrets)
        return self._engine

    def close(self) -> None:
        """Close every view this reader returned, and its engine."""
        if self._closed:
            return
        self._closed = True
        for store in self._stores:
            store._close(lambda: RuntimeError(f"the embargo reader of {self._name} is closed"))
        for registry in self._registries:
            registry.close()
        if self._engine is not None:
            self._engine.close()
        self._stores.clear()
        self._registries.clear()
        self._engine = None

    def __repr__(self) -> str:
        return f"<EmbargoReader {self._name}>"

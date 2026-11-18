"""Errors the store and the search index both raise.

A leaf module: it imports nothing from statcatlib, so `statcatlib.store.lance`
can raise `IncompatibleCatalogError` without importing `statcatlib.store.store`,
which imports it (``docs/design/label-lookup.md``, LL-D15).
"""

from __future__ import annotations


class IncompatibleCatalogError(RuntimeError):
    """Raised when a catalog or an edition is in a layout or at a version this build does not read.

    The cases: a directory holding a statcatlib 0.11 catalog, a 0.72 bundle or
    other files (`statcatlib.catalog`); an edition whose ``manifest.json`` is
    not the one the Inventory recorded; a ``statcat`` schema written by a
    statcatlib with another `schema.SCHEMA_VERSION`
    (`_refuse_statcat_schema_mismatch`); a ``search/index.json`` at another
    manifest version this build does not read (`_refuse_unreadable_index`);
    a search over a version 3 index
    (`statcatlib.store.lance.LanceIndex.check_searchable`); and a filesystem
    without hard links under `Catalog.create`. An edition on another sdmxlib store
    schema is refused by sdmxlib itself, with `sdmxlib.StoreSchemaError`.
    """

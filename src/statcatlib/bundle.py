"""The ``statcat`` section of an sdmxlib edition manifest, and its check.

sdmxlib owns ``manifest.json`` (`sdmxlib.edition`): the edition id, a digest
per file, and the checker. A statcat edition is an sdmxlib edition plus the
``statcat`` schema and ``search/``, so statcatlib adds what only it can
describe: a ``statcat`` section in the manifest, written by `catalog_section`,
and `check_catalog`, which checks that the catalog and its search index are
what that section says (GH-3). Run `check_catalog` as an extension of
`sdmxlib.edition.check_edition`.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from sdmxlib.edition import MANIFEST_NAME, Finding, Manifest, Overlay, OverlayEntry

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import duckdb
    from sdmxlib import EditionRegistry
    from sdmxlib.edition import Profile

    from statcatlib.store import Catalog
    from statcatlib.store.lance import LanceIndex, LanguageLayout

__all__ = [
    "STATCAT_SECTION",
    "STATCAT_SECTION_FORMAT",
    "catalog_section",
    "check_catalog",
    "overlay_affects_search",
]

#: The manifest's extension section statcatlib owns.
STATCAT_SECTION = "statcat"

#: Version of the ``statcat`` section. Bumped when its fields change meaning.
#: 2 added ``search.annotations``, the annotations the index was built from.
STATCAT_SECTION_FORMAT = 2

_SEARCH_DIRNAME = "search"
_INDEX_MANIFEST = "index.json"


def _has_index(search_dir: Path) -> bool:
    """Whether ``search/`` holds anything. Opening a folder catalog creates it empty."""
    return search_dir.is_dir() and any(search_dir.iterdir())


def _search_section(cat: Catalog, search_dir: Path) -> dict[str, object] | None:
    """The ``search`` part of the section.

    Args:
        cat: The catalog, open.
        search_dir: Its ``search/`` directory.

    Returns:
        The index format version, embedding model and per-language layout,
        or ``None`` when no index is built.

    Raises:
        ValueError: ``search/`` holds an index from before per-language
            tables, which has no layout to record.
    """
    if not _has_index(search_dir):
        return None
    layout = cat.index_layout
    if not layout:
        msg = f"{search_dir} holds a search index with no {_INDEX_MANIFEST}: rebuild it with build_index() first"
        raise ValueError(msg)
    index_manifest = json.loads((search_dir / _INDEX_MANIFEST).read_text())
    return {
        "index_version": index_manifest.get("version"),
        "embedding_model": cat.embedding_model_name,
        "languages": {lang: {"rows": entry["rows"], "stemmer": entry["stemmer"]} for lang, entry in layout.items()},
        "annotations": cat.index_annotations,
    }


def catalog_section(cat: Catalog) -> dict[str, object]:
    """Describe an open catalog, as its edition manifest's ``statcat`` section.

    The section records the number of catalog datasets and, when a search
    index is built, its index format version, embedding model, and each
    language's row count and stemmer. Call it after `Catalog.build_index`,
    then close the catalog and its registry and pass the section to
    `sdmxlib.edition.seal` as ``extensions={"statcat": section}``.

    Args:
        cat: The catalog, open.

    Returns:
        The section, ready for ``seal(extensions=...)``.
    """
    from statcatlib.store.schema import first_row  # noqa: PLC0415

    rows = cat.connection.execute("SELECT count(*) FROM statcat.dataset").fetchall()
    row = first_row(rows)
    search_dir = cat._search_dir
    return {
        "format": STATCAT_SECTION_FORMAT,
        "statcatlib_version": metadata.version("statcatlib"),
        "datasets": row[0] if row else 0,
        "search": _search_section(cat, search_dir) if search_dir is not None else None,
    }


def _check_catalog_rows(conn: duckdb.DuckDBPyConnection, section: Mapping[str, object]) -> Iterator[Finding]:
    """The catalog's datasets: counted as declared, each linked flow stored."""
    from statcatlib.store.schema import first_row  # noqa: PLC0415

    rows = conn.execute("SELECT count(*) FROM statcat.dataset").fetchall()
    row = first_row(rows)
    found = row[0] if row else 0
    if found != section.get("datasets"):
        detail = f"{found} datasets, the manifest says {section.get('datasets')}"
        yield Finding(STATCAT_SECTION, "statcat.dataset", detail)
    dangling = conn.execute(
        """
        SELECT d.identifier, d.sdmx_dataflow_urn
        FROM statcat.dataset d
        WHERE d.sdmx_dataflow_urn IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM sdmx.dataflow f WHERE f.urn = d.sdmx_dataflow_urn)
        ORDER BY d.identifier
        """
    ).fetchall()
    for identifier, urn in dangling:
        yield Finding(STATCAT_SECTION, identifier, f"links {urn}, which the registry does not hold")


def _check_index_annotations(
    conn: duckdb.DuckDBPyConnection,
    declared: object,
    annotation_fingerprint: Callable[[duckdb.DuckDBPyConnection, list[str]], str],
) -> Iterator[Finding]:
    """The store holds the annotations the index was built from: no overlay folded in since."""
    subject = f"{_SEARCH_DIRNAME} annotations"
    if not isinstance(declared, Mapping) or not isinstance(types := declared.get("types"), list):
        yield Finding(
            STATCAT_SECTION, subject, "the index records no annotation fingerprint: rebuild with build_index()"
        )
        return
    if annotation_fingerprint(conn, [str(t) for t in types]) != declared.get("fingerprint"):
        detail = (
            f"annotations of type {', '.join(map(str, types))} differ from those the index was built from "
            "(an overlay folded in?): rebuild with build_index()"
        )
        yield Finding(STATCAT_SECTION, subject, detail)


def _check_search(root: Path, declared: object) -> Iterator[Finding]:
    """The search index: present exactly when declared, with the declared layout."""
    search_dir = root / _SEARCH_DIRNAME
    if declared is None:
        if _has_index(search_dir):
            yield Finding(STATCAT_SECTION, _SEARCH_DIRNAME, "a search index the manifest does not declare")
        return
    if not isinstance(declared, Mapping) or not isinstance(languages := declared.get("languages"), Mapping):
        yield Finding(STATCAT_SECTION, MANIFEST_NAME, "the search section has no languages")
        return
    if not (search_dir / _INDEX_MANIFEST).is_file():
        yield Finding(STATCAT_SECTION, f"{_SEARCH_DIRNAME}/{_INDEX_MANIFEST}", "the manifest declares a search index")
        return
    try:
        from statcatlib.store.lance import LanceIndex  # noqa: PLC0415
    except ImportError:
        yield Finding(STATCAT_SECTION, _SEARCH_DIRNAME, "cannot check the search index without statcatlib[search]")
        return
    if (found := LanceIndex.unreadable_version(search_dir)) is not None:
        detail = f"{found}, which this build does not read: check it with that release, or rebuild with build_index()"
        yield Finding(STATCAT_SECTION, f"{_SEARCH_DIRNAME}/{_INDEX_MANIFEST}", detail)
        return
    index = LanceIndex(search_dir)
    if (declared_version := declared.get("index_version")) != index.version:
        detail = f"the manifest declares index_version {declared_version}, {_INDEX_MANIFEST} records {index.version}"
        yield Finding(STATCAT_SECTION, MANIFEST_NAME, detail)
    layout = index.describe()
    for lang in sorted(set(languages) | set(layout)):
        yield from _check_language(index, lang, layout.get(lang), languages.get(lang))
    for name in index.orphan_tables():
        detail = f"a table {_INDEX_MANIFEST} does not name: rebuild with build_index(), then seal again"
        yield Finding(STATCAT_SECTION, f"{_SEARCH_DIRNAME}/{name}", detail)


def _check_language(index: LanceIndex, lang: str, found: LanguageLayout | None, want: object) -> Iterator[Finding]:
    """One language's table: declared, stemmed as declared, holding the declared rows."""
    subject = f"{_SEARCH_DIRNAME} [{lang}]"
    if found is None:
        yield Finding(STATCAT_SECTION, subject, "declared in the manifest, absent from the index")
        return
    if not isinstance(want, Mapping):
        yield Finding(STATCAT_SECTION, subject, "in the index, not declared in the manifest")
        return
    if found["stemmer"] != want.get("stemmer"):
        yield Finding(
            STATCAT_SECTION, subject, f"stemmed with {found['stemmer']}, the manifest says {want.get('stemmer')}"
        )
    table = index.table_for(lang)
    rows = table.count_rows() if table is not None else 0
    if rows != want.get("rows"):
        yield Finding(STATCAT_SECTION, subject, f"{rows} rows, the manifest says {want.get('rows')}")


def check_catalog(root: Path, manifest: Manifest, reg: EditionRegistry, profile: Profile) -> list[Finding]:
    """The ``statcat`` check, an extension of `sdmxlib.edition.check_edition`.

    Pass it as ``check_edition(path, at=..., extensions={STATCAT_SECTION:
    check_catalog})``. It checks that:

    - the ``statcat`` schema is in the edition's store at the version this build
      reads, and the manifest has a ``statcat`` section in a format this
      build reads;
    - the catalog holds the datasets the section counts, and every dataset
      linked to a dataflow names one the registry holds;
    - ``search/`` is present exactly when the section declares an index;
      ``index.json`` records a version this build reads, the one the section
      declares; the index has the declared languages, each stemmed as
      declared and holding the declared rows, and no table that
      ``index.json`` does not name;
    - the store's annotations of the types the index read are the ones it was
      built from, so an overlay folded in since shows up as a stale index.

    Args:
        root: The edition directory.
        manifest: Its manifest.
        reg: sdmxlib's registry over the edition.
        profile: The `check_edition` profile that runs it. Every profile runs
            the same checks.

    Returns:
        Everything found wrong with the catalog's part of the edition.
    """
    from statcatlib.store.schema import first_row  # noqa: PLC0415
    from statcatlib.store.store import annotation_fingerprint, statcat_schema_mismatch  # noqa: PLC0415

    del profile
    conn = reg.connection
    rows = conn.execute(
        """SELECT count(*) FROM information_schema.tables WHERE table_schema = 'statcat' AND table_name = 'dataset'"""
    ).fetchall()
    has_schema = first_row(rows)
    if not has_schema or not has_schema[0]:
        return [Finding(STATCAT_SECTION, "statcat", "no statcat schema: not a statcat catalog")]
    if (stored := statcat_schema_mismatch(conn)) is not None:
        detail = f"statcat schema version {stored}, not the one this build reads: rebuild the catalog"
        return [Finding(STATCAT_SECTION, "statcat._meta", detail)]
    section = manifest.extensions.get(STATCAT_SECTION)
    if section is None:
        detail = "no statcat section: seal with extensions={'statcat': catalog_section(cat)}"
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, detail)]
    if section.get("format") != STATCAT_SECTION_FORMAT:
        detail = f"statcat section format {section.get('format')!r}, this build reads {STATCAT_SECTION_FORMAT}"
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, detail)]
    findings = [*_check_catalog_rows(conn, section), *_check_search(root, section.get("search"))]
    search = section.get("search")
    if isinstance(search, Mapping):
        findings += _check_index_annotations(conn, search.get("annotations"), annotation_fingerprint)
    return findings


def overlay_affects_search(overlay: Overlay, types: Collection[str] | None = None) -> tuple[OverlayEntry, ...]:
    """The entries of *overlay* that change what the search index should hold.

    An overlay applied at serve time never reaches statcatlib's index, and one
    folded into an edition needs ``build_index()`` before search reflects it.
    Either way, an entry acting on an annotation type the index reads is a
    change search will not see on its own. Call this where overlays are
    loaded, to log or refuse them. Static: it needs no edition.

    Args:
        overlay: The overlay document.
        types: The annotation types the index reads. ``None`` means
            `SEARCH_ANNOTATION_TYPES`, what `Catalog.build_index` reads by
            default; pass ``cat.index_annotations["types"]`` for an index
            built otherwise.

    Returns:
        Those entries, in document order; empty, and so falsy, when there are none.
    """
    from statcatlib.store.store import SEARCH_ANNOTATION_TYPES  # noqa: PLC0415

    wanted = set(SEARCH_ANNOTATION_TYPES if types is None else types)
    return tuple(entry for entry in overlay.entries if entry.type in wanted)

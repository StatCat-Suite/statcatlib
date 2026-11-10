"""The ``statcat`` section of an sdmxlib edition manifest, and its check.

sdmxlib owns ``manifest.json`` (`sdmxlib.edition`): the edition id, a digest
per file, and the checker. A statcat edition is an sdmxlib edition plus the
``statcat`` schema and ``search/``, so statcatlib adds what only it can
describe: a ``statcat`` section in the manifest, written by `catalog_section`
when `WorkingCopy.seal` seals, and `check_catalog`, which checks that the
catalog and its search index are what that section says (GH-3). Run
`check_catalog` as an extension of `sdmxlib.edition.check_edition`.

It also holds the catalog description's JSON form: `description_to_json` and
`description_from_json` convert a `CatalogDescription`'s catalog-level fields
for the Inventory root's ``extensions.statcat``, which `CatalogAdmin.describe`
writes and `Catalog.description` reads.

The module keeps its name from the 0.11 bundle format; it describes editions.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from sdmxlib import InternationalString
from sdmxlib.edition import MANIFEST_NAME, Finding, Manifest, Overlay, OverlayEntry

from statcatlib.model.agents import Agent
from statcatlib.model.catalog import CatalogDescription
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.skos import ConceptSchemeRef

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import duckdb
    from sdmxlib import EditionRegistry
    from sdmxlib.edition import Lifecycle, Profile
    from sdmxlib.inventory import Kind

    from statcatlib.store.lance import LanceIndex, LanguageLayout
    from statcatlib.store.store import _Store

__all__ = [
    "PRODUCT_KINDS",
    "ROOT_SECTION_FORMAT",
    "STATCAT_SECTION",
    "STATCAT_SECTION_FORMAT",
    "catalog_section",
    "check_catalog",
    "check_catalog_no_index",
    "description_from_json",
    "description_from_root",
    "description_to_json",
    "overlay_affects_search",
    "structure_content",
]

#: The manifest's extension section statcatlib owns.
STATCAT_SECTION = "statcat"

#: Version of the ``statcat`` section. Bumped when its fields change meaning.
#: 2 added ``search.annotations``, the annotations the index was built from.
#: 3 added ``kind``, the product's kind (Phase 2a).
STATCAT_SECTION_FORMAT = 3

#: The product kinds format 3 names. A ``structure`` product's editions hold
#: full artefacts and no dataflow, binding or partial (`structure_content`), so
#: `check_catalog` refuses each of those, and a manifest binding, in one.
PRODUCT_KINDS: tuple[str, ...] = ("data", "structure")

#: The values the section's optional ``lifecycle`` takes (statcatlib 0.17, design D3).
_LIFECYCLES: tuple[str, ...] = ("final", "revisable")

_SEARCH_DIRNAME = "search"
_INDEX_MANIFEST = "index.json"


def _has_index(search_dir: Path) -> bool:
    """Whether ``search/`` holds anything."""
    return search_dir.is_dir() and any(search_dir.iterdir())


def _search_section(store: _Store, search_dir: Path) -> dict[str, object] | None:
    """The ``search`` part of the section.

    Args:
        store: The working copy's store, open.
        search_dir: Its ``search/`` directory.

    Returns:
        The index format version, embedding model and per-language layout,
        or ``None`` when no index is built. An index over an edition with no
        rows has no languages.

    Raises:
        ValueError: ``search/`` holds files and no ``index.json``.
    """
    if not _has_index(search_dir):
        return None
    if not (search_dir / _INDEX_MANIFEST).is_file():
        msg = f"{search_dir} holds a search index with no {_INDEX_MANIFEST}: rebuild it with build_index() first"
        raise ValueError(msg)
    layout = store.index_layout
    index_manifest = json.loads((search_dir / _INDEX_MANIFEST).read_text())
    return {
        "index_version": index_manifest.get("version"),
        "embedding_model": store.embedding_model_name,
        "languages": {lang: {"rows": entry["rows"], "stemmer": entry["stemmer"]} for lang, entry in layout.items()},
        "annotations": store.index_annotations,
    }


def catalog_section(store: _Store, *, kind: Kind = "data", lifecycle: Lifecycle) -> dict[str, object]:
    """Describe a working copy's edition, as its manifest's ``statcat`` section (format 3).

    The section records the product's kind and lifecycle, the number of catalog datasets and, when a search index
    is built, its index format version, embedding model, each language's row count and stemmer, and the
    annotations it was built from. `WorkingCopy.seal`
    computes it after rebuilding the index and before closing the store, and
    passes it to `sdmxlib.edition.seal` as ``extensions={"statcat": section}``.

    Args:
        store: The working copy's store, open.
        kind: The product's kind, ``data`` or ``structure``.
        lifecycle: The product's lifecycle, ``final`` or ``revisable``: the
            one the seal compacts by, which the release job's fold records
            for a product the Inventory does not record yet.

    Returns:
        The section, ready for ``seal(extensions=...)``.
    """
    from statcatlib.store.schema import first_row  # noqa: PLC0415

    rows = store._cursor().execute("SELECT count(*) FROM statcat.dataset").fetchall()
    row = first_row(rows)
    search_dir = store._search_dir
    return {
        "format": STATCAT_SECTION_FORMAT,
        "kind": kind,
        "lifecycle": lifecycle,
        "statcatlib_version": metadata.version("statcatlib"),
        "datasets": row[0] if row else 0,
        "search": _search_section(store, search_dir) if search_dir is not None else None,
    }


def structure_content(conn: duckdb.DuckDBPyConnection) -> list[Finding]:
    """What a structure product's edition may not hold: each dataflow, binding and partial in its store.

    A structure product holds full artefacts only. `check_catalog` runs this
    on a sealed structure edition, and `WorkingCopy.seal` on a structure
    working copy before anything is sealed.

    Args:
        conn: A connection whose current catalog is the edition's, so
            ``sdmx.artefact`` names its table.

    Returns:
        One finding per such artefact, by URN.
    """
    rows = conn.execute(
        """
        SELECT urn, artefact_type, is_partial FROM sdmx.artefact
        WHERE artefact_type IN ('Dataflow', 'DataflowBinding') OR is_partial
        ORDER BY urn
        """
    ).fetchall()
    findings: list[Finding] = []
    for urn, artefact_type, is_partial in rows:
        held = f"a partial {artefact_type}" if is_partial else f"a {artefact_type}"
        detail = f"{held}, which a structure product does not hold: it holds full artefacts only"
        findings.append(Finding(STATCAT_SECTION, urn, detail))
    return findings


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
    try:
        index = LanceIndex(search_dir)
    except ImportError:
        yield Finding(STATCAT_SECTION, _SEARCH_DIRNAME, "cannot check the search index without statcatlib[search]")
        return
    if (declared_version := declared.get("index_version")) != index.version:
        detail = f"the manifest declares index_version {declared_version}, {_INDEX_MANIFEST} records {index.version}"
        yield Finding(STATCAT_SECTION, MANIFEST_NAME, detail)
    yield from _check_layout(index, languages)


def _check_layout(index: LanceIndex, languages: Mapping[str, object]) -> Iterator[Finding]:
    """The index's tables: each declared language as declared, and none ``index.json`` does not name."""
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
    - the section's ``kind`` is one of `PRODUCT_KINDS`, and a ``structure``
      edition binds no dataflow to observations and holds no dataflow,
      binding or partial (`structure_content`);
    - the section's ``lifecycle``, when present, is ``final`` or
      ``revisable``;
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
    del profile
    return _section_findings(root, manifest, reg, index=True)


def check_catalog_no_index(root: Path, manifest: Manifest, reg: EditionRegistry, profile: Profile) -> list[Finding]:
    """`check_catalog` without its search index check, for an install with no `lancedb`.

    A ``statcatlib[blob]`` install holds no `lancedb`, so it cannot open the
    index. The ``files`` check of `sdmxlib.edition.check_edition` still covers the
    index's bytes by digest.

    Args:
        root: The edition directory.
        manifest: Its manifest.
        reg: sdmxlib's registry over the edition.
        profile: The `check_edition` profile that runs it. Every profile runs
            the same checks.

    Returns:
        Everything found wrong with the rest of the catalog's part of the edition.
    """
    del profile
    return _section_findings(root, manifest, reg, index=False)


def _section_findings(root: Path, manifest: Manifest, reg: EditionRegistry, *, index: bool) -> list[Finding]:
    """The ``statcat`` checks, with the opening of the search index when *index* is true."""
    from statcatlib.store.schema import first_row  # noqa: PLC0415
    from statcatlib.store.store import annotation_fingerprint, statcat_schema_mismatch  # noqa: PLC0415

    conn = reg.connection
    rows = conn.execute(
        """
        SELECT count(*) FROM information_schema.tables
        WHERE table_catalog = current_database() AND table_schema = 'statcat' AND table_name = 'dataset'
        """
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
    kind = section.get("kind")
    if kind not in PRODUCT_KINDS:
        detail = f"statcat section kind {kind!r}, not one of {', '.join(PRODUCT_KINDS)}"
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, detail)]
    lifecycle = section.get("lifecycle")
    if lifecycle is not None and lifecycle not in _LIFECYCLES:
        detail = f"statcat section lifecycle {lifecycle!r}, not one of {', '.join(_LIFECYCLES)}"
        return [Finding(STATCAT_SECTION, MANIFEST_NAME, detail)]
    findings: list[Finding] = []
    if kind == "structure":
        findings += [
            Finding(STATCAT_SECTION, binding.dataflow, "a structure product's edition binds a dataflow to observations")
            for binding in manifest.bindings
        ]
        findings += structure_content(conn)
    findings += _check_catalog_rows(conn, section)
    if index:
        findings += _check_search(root, section.get("search"))
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


# -- the catalog description, in the Inventory root's ``extensions.statcat`` --

#: The version of the root's ``extensions.statcat`` section that `CatalogAdmin.describe` writes.
ROOT_SECTION_FORMAT = "3"


def _text_map(value: object, key: str) -> InternationalString:
    """A JSON object of language tag to text, as an `InternationalString`."""
    if not isinstance(value, Mapping):
        msg = f"the catalog description's {key} is {value!r}; it must be an object of language tag to text"
        raise TypeError(msg)
    return InternationalString.of({str(lang): str(text) for lang, text in value.items()})


def _text_map_or_none(value: object, key: str) -> InternationalString | None:
    return None if value is None else _text_map(value, key)


def _str_or_none(value: object, key: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    msg = f"the catalog description's {key} is {value!r}; it must be a string"
    raise TypeError(msg)


def _required_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"the catalog description's {key} is {value!r}; it must be a string"
        raise TypeError(msg)
    return value


def _object(value: object, key: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"the catalog description's {key} is {value!r}; it must be an object"
        raise TypeError(msg)
    return {str(k): v for k, v in value.items()}


def _objects(value: object, key: str) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        msg = f"the catalog description's {key} is {value!r}; it must be a list"
        raise TypeError(msg)
    return [_object(item, key) for item in value]


def _instant(value: object, key: str) -> datetime | None:
    text = _str_or_none(value, key)
    return None if text is None else datetime.fromisoformat(text)


def _agent_from_json(data: Mapping[str, object]) -> Agent:
    return Agent(
        name=_text_map(data.get("name"), "publisher.name"),
        id=_str_or_none(data.get("id"), "publisher.id"),
        type=_str_or_none(data.get("type"), "publisher.type"),
        homepage=_str_or_none(data.get("homepage"), "publisher.homepage"),
    )


def description_to_json(description: CatalogDescription) -> dict[str, object]:
    """The catalog-level fields of *description*, as the JSON the Inventory root's ``extensions.statcat`` holds.

    The fields are ``identifier``, ``title``, ``description``, ``publisher``,
    ``homepage``, ``language``, ``licence``, ``theme_taxonomy``, ``issued`` and
    ``modified``. Multilingual text becomes an object of language tag to text,
    and an instant an ISO 8601 string. ``datasets``, ``services``, ``records``,
    ``spatial_coverage`` and ``temporal_coverage`` are left out: the datasets
    and services come from the live editions, and nothing stores them twice.

    Args:
        description: The description.

    Returns:
        A JSON object, which `description_from_json` reads back.
    """
    publisher = description.publisher
    licence = description.licence
    return {
        "identifier": description.identifier,
        "title": description.title.as_dict(),
        "description": description.description.as_dict(),
        "publisher": {
            "name": publisher.name.as_dict(),
            "id": publisher.id,
            "type": publisher.type,
            "homepage": publisher.homepage,
        },
        "homepage": description.homepage,
        "language": list(description.language),
        "licence": None
        if licence is None
        else {
            "id": licence.id,
            "label": None if licence.label is None else licence.label.as_dict(),
            "uri": licence.uri,
        },
        "theme_taxonomy": [{"uri": ref.uri, "label": ref.label.as_dict()} for ref in description.theme_taxonomy],
        "issued": None if description.issued is None else description.issued.isoformat(),
        "modified": None if description.modified is None else description.modified.isoformat(),
    }


def description_from_json(data: Mapping[str, object]) -> CatalogDescription:
    """The `CatalogDescription` that `description_to_json` wrote.

    Keys it does not know are ignored, so a section a later statcatlib extends
    still reads. The fields `description_to_json` leaves out take their
    defaults.

    Args:
        data: The ``description`` object of the root's ``extensions.statcat``.

    Returns:
        The description.

    Raises:
        TypeError: A field has the wrong JSON type, or ``identifier``,
            ``title``, ``description`` or ``publisher`` is missing.
    """
    licence = data.get("licence")
    licence_doc = None
    if licence is not None:
        fields = _object(licence, "licence")
        licence_doc = LicenceDocument(
            id=_required_str(fields, "id"),
            label=_text_map_or_none(fields.get("label"), "licence.label"),
            uri=_str_or_none(fields.get("uri"), "licence.uri"),
        )
    language = data.get("language")
    if language is not None and not isinstance(language, list):
        msg = f"the catalog description's language is {language!r}; it must be a list of language tags"
        raise TypeError(msg)
    return CatalogDescription(
        identifier=_required_str(data, "identifier"),
        title=_text_map(data.get("title"), "title"),
        description=_text_map(data.get("description"), "description"),
        publisher=_agent_from_json(_object(data.get("publisher"), "publisher")),
        homepage=_str_or_none(data.get("homepage"), "homepage"),
        language=[str(tag) for tag in language or ()],
        licence=licence_doc,
        theme_taxonomy=[
            ConceptSchemeRef(
                uri=_required_str(ref, "uri"),
                label=_text_map_or_none(ref.get("label"), "theme_taxonomy.label") or InternationalString(),
            )
            for ref in _objects(data.get("theme_taxonomy"), "theme_taxonomy")
        ],
        issued=_instant(data.get("issued"), "issued"),
        modified=_instant(data.get("modified"), "modified"),
    )


def description_from_root(extensions: Mapping[str, Mapping[str, object]]) -> CatalogDescription | None:
    """The catalog description an Inventory root's *extensions* hold, or ``None`` when it holds none.

    Args:
        extensions: The root's ``extensions``.

    Returns:
        The description in ``extensions.statcat.description``, read with
        `description_from_json`.
    """
    section = extensions.get(STATCAT_SECTION)
    found = None if section is None else section.get("description")
    return None if found is None else description_from_json(_object(found, "section"))

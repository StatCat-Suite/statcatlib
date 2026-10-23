"""`docs/bundle-format.md` restates what statcatlib writes, so it can fall behind it.

Modelled on sdmxlib's `tests/unit/test_bundle_spec.py`. The page's field tables
are compared with the `TypedDict`s that type `search/index.json` and
`embeddings.json`, and with the `statcat` section of a bundle
`synthetic_catalog` really publishes, since that section has no type of its own.
The column table, the schema's table list, the format numbers and the example
section are held to the code the same way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NotRequired, get_origin, get_type_hints

import duckdb
import pytest
from sdmxlib.testing import Dim, Shape

from statcatlib.bundle import STATCAT_SECTION, STATCAT_SECTION_FORMAT
from statcatlib.store import lance, schema
from statcatlib.store.store import _EMBEDDINGS_META_VERSION, SEARCH_ANNOTATION_TYPES, _EmbeddingsSidecar
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_SPEC = Path(__file__).resolve().parent.parent / "docs" / "bundle-format.md"

#: A heading naming a definition, then its table's rows up to the next blank line.
_TABLE = re.compile(
    r"^#{3,4} `(\w+)`\n\n(?:(?:[^|\n].*\n)+\n)?"  # the heading, then any paragraph
    r"\| field \| presence \| meaning \|\n\| --- \| --- \| --- \|\n((?:\|.*\n)+)",  # the table
    re.MULTILINE,
)
_ROW = re.compile(r"^\| `(\w+)` \| (required|optional) \|", re.MULTILINE)
_COLUMNS = re.compile(r"^#### Search table columns\n\n(?:.*\n)*?((?:\|.*\n)+)", re.MULTILINE)
_COLUMN_ROW = re.compile(r"^\| `(\w+)` \| ([^|]+?) \|", re.MULTILINE)

#: The documented names of definitions whose Python type is private or absent.
_TYPED = {
    "IndexManifest": lance._Manifest,
    "LanguageLayout": lance.LanguageLayout,
    "IndexAnnotations": lance.IndexAnnotations,
    "EmbeddingsSidecar": _EmbeddingsSidecar,
}


def _text() -> str:
    return _SPEC.read_text(encoding="utf-8")


def _tables(text: str) -> dict[str, dict[str, str]]:
    """Each documented definition's fields, by name, to ``required`` or ``optional``."""
    return {name: dict(_ROW.findall(rows)) for name, rows in _TABLE.findall(text)}


def _typed_fields() -> dict[str, dict[str, str]]:
    """Each TypedDict's fields, required unless marked `NotRequired`.

    Read from the resolved hints: `lance.py` postpones its annotations, so
    `__required_keys__` cannot see a `NotRequired` and calls every key required.
    """
    found: dict[str, dict[str, str]] = {}
    for name, typed in _TYPED.items():
        hints = get_type_hints(typed, include_extras=True)
        found[name] = {f: "optional" if get_origin(h) is NotRequired else "required" for f, h in hints.items()}
    return found


@pytest.fixture(scope="module")
def section(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """The ``statcat`` section of a bundle statcatlib really publishes."""
    shape = Shape(dimensions=(Dim(codes=30, depth=2), Dim(codes=4)), observations=300, languages=("en", "fr"))
    manifest = synthetic_catalog(tmp_path_factory.mktemp("spec") / "bundle", shape, seed=3)
    return manifest.extensions[STATCAT_SECTION]


def _section_fields(section: dict[str, object]) -> dict[str, dict[str, str]]:
    """The section's three untyped levels, as `_tables` reads the page. Every key is always written."""
    search = section["search"]
    assert isinstance(search, dict)
    languages = search["languages"]
    assert isinstance(languages, dict)
    one_language = next(iter(languages.values()))
    return {
        "StatcatSection": dict.fromkeys(section, "required"),
        "SearchSection": dict.fromkeys(search, "required"),
        "SearchLanguage": dict.fromkeys(one_language, "required"),
    }


def test_the_spec_documents_every_definition_field_by_field(section: dict[str, object]) -> None:
    documented = _tables(_text())
    written = {**_typed_fields(), **_section_fields(section)}
    assert len(written) == 7, "four TypedDicts and the section's three levels"
    assert documented == written, "docs/bundle-format.md's field tables differ from what statcatlib writes"


def test_the_table_reader_sees_a_missing_or_mislabelled_field(section: dict[str, object]) -> None:
    """Discrimination: a page that drops a field, or calls an optional one required, must not read as equal."""
    text = _text()
    written = {**_typed_fields(), **_section_fields(section)}
    dropped = text.replace("| `generation` | required |", "| `generatoin` | required |", 1)
    relabelled = text.replace("| `annotations` | optional |", "| `annotations` | required |", 1)
    assert dropped != text
    assert relabelled != text
    assert _tables(dropped) != written
    assert _tables(relabelled) != written


def test_the_column_table_is_the_declared_schema() -> None:
    match = _COLUMNS.search(_text())
    assert match is not None, "the Search table columns table is missing"
    documented = dict(_COLUMN_ROW.findall(match.group(1)))
    declared = dict((*lance._BASE_COLUMNS, *lance._EXTENSION_COLUMNS))
    embedding = documented.pop(lance._EMBEDDING_COLUMN)
    assert documented == declared
    assert "float32" in embedding


def test_the_spec_lists_the_statcat_schema_tables() -> None:
    conn = duckdb.connect()
    schema.create_schema(conn)
    created = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'"
        ).fetchall()
    }
    paragraph = _text().split("## 2. The catalog schema", 1)[1].split("\n\n", 2)[1]
    assert set(re.findall(r"`(\w+)`", paragraph)) - {"statcat", "MAP", "VARCHAR"} == created


def test_the_spec_names_the_formats_this_build_writes() -> None:
    text = _text()
    assert f"**statcat section format {STATCAT_SECTION_FORMAT}**" in text
    assert f"**search index manifest version {lance._MANIFEST_VERSION}**" in text
    assert f"**embeddings sidecar version {_EMBEDDINGS_META_VERSION}**" in text
    assert f"The section format: `{STATCAT_SECTION_FORMAT}` for this page." in text
    assert f"The manifest version: `{lance._MANIFEST_VERSION}` for this page." in text
    assert f"The sidecar version: `{_EMBEDDINGS_META_VERSION}` for this page." in text
    assert f'`schema_version`, `"{schema.SCHEMA_VERSION}"` for this page' in text


def test_the_spec_names_every_annotation_type_the_index_reads() -> None:
    text = _text().split("## 5. Annotations and the index", 1)[1]
    for annotation_type in SEARCH_ANNOTATION_TYPES:
        assert f"- `{annotation_type}`" in text


def test_the_example_section_has_the_written_shape(section: dict[str, object]) -> None:
    """The example's keys at every level are the ones `publish_bundle` writes."""
    example = re.search(r"the section is:\n\n```json\n(.*?)```", _text(), re.DOTALL)
    assert example is not None
    documented = json.loads(example.group(1))

    def shape(value: object) -> object:
        if isinstance(value, dict):
            return {k: shape(v) for k, v in value.items()}
        return type(value).__name__

    assert shape(documented) == shape(json.loads(json.dumps(section)))

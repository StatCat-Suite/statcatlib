"""The spatial facet key: SQL against its Python reference (``docs/design/label-lookup.md``, LL-D5)."""

from __future__ import annotations

import hashlib

import duckdb
import pytest

from statcatlib._lang_sql import spatial_key, spatial_key_sql

pytestmark = pytest.mark.unit

_CORPUS: list[dict[str, str | None] | None] = [
    None,
    {},
    {"en": None},
    {"en": ""},
    {"en": "Ontario"},
    {"en": "Ontario", "fr": "Ontario"},
    {"fr-ca": "x", "fr": "y", "en-t-fr": "z", "en": "w"},
    {"en": "a/b", "fr": 'quote " and \\ backslash'},
    {"en": "line" + chr(0x2028) + "sep", "fr": "control \x01 \x1f char"},
    {"en": "astral \U0001f600", "fr": "Québec", "und": None},
    {"en": "=3:x", "fr": "en=1:a"},
]


@pytest.mark.parametrize("label", _CORPUS)
def test_the_sql_matches_the_python_reference(label: dict[str, str | None] | None) -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE dataset_spatial (uri VARCHAR, label MAP(VARCHAR, VARCHAR))")
    con.execute("INSERT INTO dataset_spatial VALUES (NULL, ?)", [label])
    row = con.execute(f"SELECT {spatial_key_sql('ds')} FROM dataset_spatial ds").fetchone()  # noqa: S608
    assert row is not None
    assert row[0] == spatial_key(label)


def test_the_pinned_key() -> None:
    label = {"fr": 'Québec "Nord"', "en": "Quebec"}
    expected = "sha256:0bbd403bf0802df8910df15c8eea34b9ea0ea348b151a90831839a4ec6aa2cb8"
    assert spatial_key(label) == expected
    body = 'en=6:Quebecfr=14:Québec "Nord"'
    assert expected == "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def test_a_uri_is_its_own_key() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE dataset_spatial (uri VARCHAR, label MAP(VARCHAR, VARCHAR))")
    con.execute("INSERT INTO dataset_spatial VALUES ('urn:geo:x', MAP {'en': 'X'})")
    row = con.execute(f"SELECT {spatial_key_sql('ds')} FROM dataset_spatial ds").fetchone()  # noqa: S608
    assert row == ("urn:geo:x",)


@pytest.mark.parametrize("label", [None, {}, {"en": None}])
def test_no_text_gives_no_key(label: dict[str, str | None] | None) -> None:
    assert spatial_key(label) is None

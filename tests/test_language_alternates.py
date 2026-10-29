"""Tests for `Catalog.add_language_alternates` and `Catalog.language_alternates` (GH-84)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import statcatlib as sc

pytestmark = pytest.mark.unit

_PORTAL = "https://open.canada.ca/data"


@pytest.fixture
def store() -> sc.Catalog:
    return sc.Catalog().open()


def _pairs(store: sc.Catalog) -> list[tuple[object, object, object]]:
    return [(r.primary_tag, r.alternate_tag, r.source) for r in store.language_alternates()]


def test_empty_catalog_has_no_pairs(store: sc.Catalog):
    assert store.language_alternates() == []


def test_pairs_recorded_with_source(store: sc.Catalog):
    store.add_language_alternates({"fr": ["fr-t-en"], "en": ["en-t-fr"]}, source=_PORTAL)
    assert _pairs(store) == [("en", "en-t-fr", _PORTAL), ("fr", "fr-t-en", _PORTAL)]


def test_rows_support_attribute_access(store: sc.Catalog):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    (row,) = store.language_alternates()
    assert isinstance(row, sc.Row)
    assert row.alternate_tag == "en-t-fr"


def test_recorded_pair_keeps_its_first_source(store: sc.Catalog):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    store.add_language_alternates({"en": ["en-t-fr", "en-t-de"]}, source="caller")
    assert _pairs(store) == [("en", "en-t-de", "caller"), ("en", "en-t-fr", _PORTAL)]


@pytest.mark.parametrize(
    ("pairs", "match"),
    [
        ({"en": ["en"]}, "alternate of itself"),
        ({"en": [""]}, "non-empty"),
        ({" ": ["en-t-fr"]}, "non-empty"),
        ({"en": ["en-t-fr"], "fr": ["en-t-fr"]}, "already an alternate of 'en'"),
        ({"en": ["en-t-fr"], "en-t-fr": ["x-t-y"]}, "both a primary and an alternate"),
    ],
)
def test_invalid_pairs_refused(store: sc.Catalog, pairs: dict[str, list[str]], match: str):
    with pytest.raises(ValueError, match=match):
        store.add_language_alternates(pairs, source=_PORTAL)
    assert store.language_alternates() == []


def test_string_of_alternates_refused(store: sc.Catalog):
    """Untyped input, such as a portal's JSON, can hold a bare string.

    The type checker refuses one in typed code. At run time it would otherwise
    be read as one tag per character.
    """
    pairs = json.loads('{"en": "en-t-fr"}')
    with pytest.raises(TypeError, match="sequence of tags"):
        store.add_language_alternates(pairs, source=_PORTAL)


def test_conflict_with_recorded_pair_refused(store: sc.Catalog):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    with pytest.raises(ValueError, match="already an alternate of 'en', not 'fr'"):
        store.add_language_alternates({"fr": ["en-t-fr"]}, source="caller")


def test_chain_through_recorded_pair_refused(store: sc.Catalog):
    """An alternate may not become a primary later: lookup stays one step."""
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    with pytest.raises(ValueError, match="both a primary and an alternate"):
        store.add_language_alternates({"en-t-fr": ["en-t-fr-x-old"]}, source="caller")
    assert _pairs(store) == [("en", "en-t-fr", _PORTAL)]


def test_pairs_persist_and_read_only_open_reads_them(tmp_path: Path):
    path = tmp_path / "catalog"
    with sc.Catalog(path) as store:
        store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    with sc.Catalog(path, read_only=True) as store:
        assert _pairs(store) == [("en", "en-t-fr", _PORTAL)]
        with pytest.raises(RuntimeError, match="add_language_alternates"):
            store.add_language_alternates({"fr": ["fr-t-en"]}, source=_PORTAL)

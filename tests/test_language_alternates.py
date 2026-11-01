"""Tests for `WorkingCopy.add_language_alternates` and `EditionView.language_alternates` (GH-84)."""

from __future__ import annotations

import json

import pytest
import sdmxlib

import statcatlib as sc
from tests.conftest import ScratchStore, open_scratch, seal_and_view

pytestmark = pytest.mark.unit

_PORTAL = "https://open.canada.ca/data"


@pytest.fixture
def store() -> ScratchStore:
    return open_scratch()


def _pairs(store: sc.EditionView) -> list[tuple[object, object, object]]:
    return [(r.primary_tag, r.alternate_tag, r.source) for r in store.language_alternates()]


def test_empty_catalog_has_no_pairs(store: ScratchStore):
    assert store.language_alternates() == []


def test_pairs_recorded_with_source(store: ScratchStore):
    store.add_language_alternates({"fr": ["fr-t-en"], "en": ["en-t-fr"]}, source=_PORTAL)
    assert _pairs(store) == [("en", "en-t-fr", _PORTAL), ("fr", "fr-t-en", _PORTAL)]


def test_rows_support_attribute_access(store: ScratchStore):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    (row,) = store.language_alternates()
    assert isinstance(row, sc.Row)
    assert row.alternate_tag == "en-t-fr"


def test_recorded_pair_keeps_its_first_source(store: ScratchStore):
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
def test_invalid_pairs_refused(store: ScratchStore, pairs: dict[str, list[str]], match: str):
    with pytest.raises(ValueError, match=match):
        store.add_language_alternates(pairs, source=_PORTAL)
    assert store.language_alternates() == []


def test_string_of_alternates_refused(store: ScratchStore):
    """Untyped input, such as a portal's JSON, can hold a bare string.

    The type checker refuses one in typed code. At run time it would otherwise
    be read as one tag per character.
    """
    pairs = json.loads('{"en": "en-t-fr"}')
    with pytest.raises(TypeError, match="sequence of tags"):
        store.add_language_alternates(pairs, source=_PORTAL)


def test_conflict_with_recorded_pair_refused(store: ScratchStore):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    with pytest.raises(ValueError, match="already an alternate of 'en', not 'fr'"):
        store.add_language_alternates({"fr": ["en-t-fr"]}, source="caller")


def test_chain_through_recorded_pair_refused(store: ScratchStore):
    """An alternate may not become a primary later: lookup stays one step."""
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    with pytest.raises(ValueError, match="both a primary and an alternate"):
        store.add_language_alternates({"en-t-fr": ["en-t-fr-x-old"]}, source="caller")
    assert _pairs(store) == [("en", "en-t-fr", _PORTAL)]


def test_tags_are_stored_normalised(store: ScratchStore):
    """Each tag goes through `sdmxlib.normalize_tag`, so a mixed-case tag is stored lowercase."""
    store.add_language_alternates({"FR-CA": ["fr-CA-t-EN"]}, source=_PORTAL)
    assert _pairs(store) == [("fr-ca", "fr-ca-t-en", _PORTAL)]


def test_case_variant_of_recorded_pair_is_the_same_pair(store: ScratchStore):
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    store.add_language_alternates({"EN": ["EN-T-FR"]}, source="caller")
    assert _pairs(store) == [("en", "en-t-fr", _PORTAL)]


@pytest.mark.parametrize("pairs", [{"en_US": ["en-t-fr"]}, {"en": ["en_t_fr"]}])
def test_malformed_tag_refused(store: ScratchStore, pairs: dict[str, list[str]]):
    with pytest.raises(sdmxlib.InvalidTagError, match="not a well-formed language tag"):
        store.add_language_alternates(pairs, source=_PORTAL)
    assert store.language_alternates() == []


def test_pairs_persist_and_a_sealed_edition_refuses_new_ones():
    store = open_scratch()
    store.add_language_alternates({"en": ["en-t-fr"]}, source=_PORTAL)
    view = seal_and_view(store)
    assert _pairs(view) == [("en", "en-t-fr", _PORTAL)]
    with pytest.raises(RuntimeError, match="sealed"):
        store.add_language_alternates({"fr": ["fr-t-en"]}, source=_PORTAL)

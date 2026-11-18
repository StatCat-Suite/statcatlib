"""The label lookup types: tag arguments, gap containers and the label errors."""

from __future__ import annotations

import pickle

import pytest
import sdmxlib

import statcatlib as sc
from statcatlib.labels import _as_langs

pytestmark = pytest.mark.unit


class TestAsLangs:
    def test_one_tag_is_normalised(self) -> None:
        assert _as_langs("fr-CA") == ("fr-ca",)

    def test_a_sequence_keeps_its_order_and_drops_duplicates_after_normalising(self) -> None:
        assert _as_langs(["FR", "en", "fr", "en-T-fr"]) == ("fr", "en", "en-t-fr")

    def test_an_empty_sequence_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one tag"):
            _as_langs(())

    def test_a_non_str_item_is_refused(self) -> None:
        with pytest.raises(TypeError):
            _as_langs(["en", 1])  # pyrefly: ignore[bad-argument-type]

    def test_a_non_sequence_is_refused(self) -> None:
        with pytest.raises(TypeError):
            _as_langs(1)  # pyrefly: ignore[bad-argument-type]

    def test_a_malformed_tag_is_refused(self) -> None:
        with pytest.raises(sdmxlib.InvalidTagError):
            _as_langs(("en", "fr_CA"))


class TestMissingTranslationError:
    def test_it_is_a_missing_tag_error_and_a_key_error(self) -> None:
        err = sc.MissingTranslationError("urn:x", "name", ("de", "it"), ("en", "fr"))
        assert isinstance(err, sdmxlib.MissingTagError)
        assert isinstance(err, KeyError)

    def test_its_message_names_the_entity_field_and_tags(self) -> None:
        err = sc.MissingTranslationError("urn:x", "name", ("de", "it"), ("en", "fr"))
        assert str(err) == "no text for 'de', 'it' in name of urn:x; held: 'en', 'fr'"
        assert err.entity == "urn:x"
        assert err.field == "name"
        assert err.langs == ("de", "it")
        assert err.held == ("en", "fr")

    def test_it_says_when_nothing_is_held(self) -> None:
        assert str(sc.MissingTranslationError("urn:x", "name", ("de",), ())).endswith("held: nothing")

    def test_it_pickles(self) -> None:
        err = sc.MissingTranslationError("urn:x", "name", ("de",), ("en",))
        back = pickle.loads(pickle.dumps(err))  # noqa: S301
        assert (back.entity, back.field, back.langs, back.held) == ("urn:x", "name", ("de",), ("en",))


class TestUnknownCodeError:
    def test_it_names_every_missing_pair(self) -> None:
        err = sc.UnknownCodeError([("urn:a", "XX"), ("urn:b", "YY")])
        assert isinstance(err, KeyError)
        assert not isinstance(err, sdmxlib.MissingTagError)
        assert err.missing == (("urn:a", "XX"), ("urn:b", "YY"))
        assert str(err) == "no code 'XX' in urn:a; no code 'YY' in urn:b"

    def test_it_pickles(self) -> None:
        err = sc.UnknownCodeError([("urn:a", "XX")])
        assert pickle.loads(pickle.dumps(err)).missing == (("urn:a", "XX"),)  # noqa: S301


class TestContainers:
    def test_labels_is_a_mapping_with_gaps(self) -> None:
        gap = sc.Gap(key="QC", entity="urn:x", field="name", langs=("de",))
        labels = sc.Labels([("ON", sdmxlib.LocalisedString("Ontario", "de")), ("QC", None)], [gap])
        assert list(labels) == ["ON", "QC"]
        assert labels["QC"] is None
        assert labels.gaps == (gap,)
        assert dict(labels.items())["ON"] == sdmxlib.LocalisedString("Ontario", "de")

    def test_row_and_value_lists_carry_gaps(self) -> None:
        gap = sc.Gap(key="d1", entity="d1", field="title", langs=("fr",))
        rows = sc.RowList([sc.Row({"title": None})], [gap])
        values = sc.ValueList([None], [gap])
        assert rows == [{"title": None}]
        assert rows.gaps == values.gaps == (gap,)
        assert type(rows[:1]) is list

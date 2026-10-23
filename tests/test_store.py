"""Tests for Catalog (DuckDB backend)."""

from typing import Any

import pytest
from sdmxlib import InternationalString

import statcatlib as sc
from tests.conftest import FIXTURE_DATASETS

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _identifier(hit: object) -> str | None:
    """The dataset identifier behind a search hit, or `None`.

    `search()` spans datasets, concepts and codes, so a hit's payload is
    not always a dataset. Returning `None` for the others keeps the
    assertions about *which* dataset came back, not about the kinds.
    """
    payload = getattr(hit, "payload", None)
    return payload.identifier if isinstance(payload, sc.BaseDataset) else None


def _untyped(value: object) -> Any:
    """Erase a value's static type on the way to a runtime guard.

    The `extras` rejection tests deliberately pass what the annotation
    forbids — that is what a runtime guard is for, and a type checker is
    right to object. Laundering here states the intent once, where it can be
    read, rather than scattering suppressions over six tests.
    """
    return value


class TestPut:
    def test_put_single(self):
        s = sc.Catalog().open()
        ds = sc.OpenDataset(identifier="X", title=_i("X"), description=_i("d"))
        s.put(ds)
        assert s.get(sc.BaseDataset, "X") is not None

    def test_put_list(self, store):
        assert store.get(sc.BaseDataset, "EXR") is not None
        assert store.get(sc.BaseDataset, "CO2") is not None

    def test_put_overwrites(self):
        s = sc.Catalog().open()
        ds1 = sc.OpenDataset(identifier="X", title=_i("Original"), description=_i("d"))
        ds2 = sc.OpenDataset(identifier="X", title=_i("Updated"), description=_i("d"))
        s.put(ds1)
        s.put(ds2)
        result = s.get(sc.BaseDataset, "X")
        assert result is not None
        assert result.title.get("en") == "Updated"


class TestExtras:
    """Round-trip and rejection for `extras`.

    None of this existed before, which is why a field advertised as
    ``dict[str, Any]`` stored ``str(value)`` for four minor releases
    without anyone noticing.
    """

    def _roundtrip(self, extras: dict[str, str]) -> dict[str, str]:
        s = sc.Catalog().open()
        s.put(sc.OpenDataset(identifier="X", title=_i("X"), description=_i("d"), extras=extras))
        ds = s.get(sc.BaseDataset, "X")
        assert ds is not None
        return ds.extras

    def test_strings_round_trip_exactly(self):
        extras = {"source": "manual", "run": "7", "note": "a b c"}
        assert self._roundtrip(extras) == extras

    def test_empty_extras_round_trips_as_empty(self):
        assert self._roundtrip({}) == {}

    def test_strings_that_look_like_other_types_are_left_alone(self):
        """``"None"`` is a legitimate string and must survive as one.

        The old coercion made this value indistinguishable from a stored
        `None`; keeping it verbatim is what makes the two different again.
        """
        extras = {"a": "None", "b": "1", "c": "True", "d": "[1, 2]"}
        assert self._roundtrip(extras) == extras

    @pytest.mark.parametrize(
        "value",
        [1, 2.5, True, None, [1, 2], {"a": 1}],
        ids=["int", "float", "bool", "none", "list", "dict"],
    )
    def test_non_string_values_are_refused(self, value: object):
        """Each of these used to be silently stringified."""
        s = sc.Catalog().open()
        ds = sc.OpenDataset(identifier="X", title=_i("X"), description=_i("d"))
        ds.extras = _untyped({"k": value})
        with pytest.raises(TypeError, match=r"extras\['k'\] must be str"):
            s.put(ds)

    def test_non_string_keys_are_refused(self):
        s = sc.Catalog().open()
        ds = sc.OpenDataset(identifier="X", title=_i("X"), description=_i("d"))
        ds.extras = _untyped({1: "v"})
        with pytest.raises(TypeError, match="extras keys must be str"):
            s.put(ds)

    def test_error_names_the_key_and_the_type(self):
        """A large mapping is unhelpful to debug without both."""
        s = sc.Catalog().open()
        ds = sc.OpenDataset(identifier="X", title=_i("X"), description=_i("d"))
        ds.extras = _untyped({"ok": "yes", "bad": 3})
        with pytest.raises(TypeError) as exc:
            s.put(ds)
        assert "'bad'" in str(exc.value)
        assert "int" in str(exc.value)


class TestGet:
    def test_get_base_dataset(self, store):
        ds = store.get(sc.BaseDataset, "EXR")
        assert ds is not None
        assert ds.identifier == "EXR"

    def test_get_aggregate_dataset(self, store):
        ds = store.get(sc.AggregateDataset, "EXR")
        assert ds is not None
        assert isinstance(ds, sc.AggregateDataset)

    def test_get_open_as_aggregate_returns_none(self, store):
        result = store.get(sc.AggregateDataset, "CO2")
        assert result is None

    def test_get_aggregate_as_base_dataset(self, store):
        result = store.get(sc.BaseDataset, "EXR")
        assert result is not None

    def test_get_missing_returns_none(self, store):
        assert store.get(sc.BaseDataset, "DOES_NOT_EXIST") is None

    def test_get_roundtrip_preserves_fields(self, store):
        ds = store.get(sc.AggregateDataset, "EXR")
        assert ds is not None
        assert ds.num_series == 5
        assert ds.publisher is not None
        assert ds.publisher.name.get("en") == "ECB"
        assert ds.frequency is not None
        assert ds.frequency.id == "DAILY"
        assert ds.status == "current"

    def test_statistical_dataset_alias(self, store):
        """StatisticalDataset alias works for get()."""
        ds = store.get(sc.StatisticalDataset, "EXR")
        assert ds is not None
        assert isinstance(ds, sc.AggregateDataset)


class TestSearch:
    """`Catalog.search` over an indexed catalog.

    `search()` yields `Hit` wrappers, so the entity is on `.payload`.
    """

    def test_search_by_title(self, indexed_store: sc.Catalog):
        result = indexed_store.search("exchange rate")
        assert any(_identifier(hit) == "EXR" for hit in result.items)

    def test_search_case_insensitive(self, indexed_store: sc.Catalog):
        result = indexed_store.search("EXCHANGE RATE")
        assert any(_identifier(hit) == "EXR" for hit in result.items)

    def test_search_by_description(self, indexed_store: sc.Catalog):
        result = indexed_store.search("GDP growth rate")
        assert any(_identifier(hit) == "GDP" for hit in result.items)

    def test_search_returns_total(self, indexed_store: sc.Catalog):
        result = indexed_store.search("quarterly")
        assert result.total > 0

    def test_search_limit(self, indexed_store: sc.Catalog):
        result = indexed_store.search("rate", limit=2)
        assert len(result.items) <= 2

    def test_search_offset(self, indexed_store: sc.Catalog):
        all_results = indexed_store.search("monthly")
        offset_results = indexed_store.search("monthly", offset=1)
        assert offset_results.total == all_results.total
        if all_results.total > 1:
            assert _identifier(offset_results.items[0]) != _identifier(all_results.items[0])

    def test_search_no_results(self, indexed_store: sc.Catalog):
        result = indexed_store.search("xyzzy_nonexistent_term")
        assert result.items == []
        assert result.total == 0

    def test_search_returns_both_kinds(self, indexed_store: sc.Catalog):
        result = indexed_store.search("monthly")
        payloads = [hit.payload for hit in result.items]
        assert any(isinstance(p, sc.AggregateDataset) for p in payloads)
        assert any(isinstance(p, sc.OpenDataset) for p in payloads)

    def test_search_includes_open_dataset(self, indexed_store: sc.Catalog):
        result = indexed_store.search("labour force")
        assert any(_identifier(hit) == "LFS" for hit in result.items)


class TestFacets:
    def test_facets_publisher(self, store):
        facets = store.facets("publisher")
        assert "publisher" in facets
        assert facets["publisher"]["ECB"] >= 1
        assert facets["publisher"]["ESTAT"] >= 1

    def test_facets_frequency(self, store):
        facets = store.facets("frequency")
        assert "frequency" in facets
        assert "MONTHLY" in facets["frequency"]
        assert "ANNUAL" in facets["frequency"]

    def test_facets_theme(self, store):
        facets = store.facets("themes")
        assert "themes" in facets
        # Facets returns URIs for themes in the new SKOS model
        uris = set(facets["themes"].keys())
        assert any("economy" in uri for uri in uris)
        assert any("labour" in uri for uri in uris)

    def test_facets_multiple_fields(self, store):
        facets = store.facets("publisher", "frequency")
        assert "publisher" in facets
        assert "frequency" in facets

    def test_facets_counts_all_datasets(self, store):
        facets = store.facets("publisher")
        total = sum(facets["publisher"].values())
        assert total == len(FIXTURE_DATASETS)

    def test_facets_unknown_field_returns_empty(self, store):
        facets = store.facets("version")
        assert facets["version"] == {}


class TestThemeSchemeIsolation:
    """Concepts from different schemes remain isolated (URI-based identity)."""

    _STATCAN = "urn:test:statcan"
    _EUROVOC = "urn:test:eurovoc"

    def _build_store(self) -> sc.Catalog:
        s = sc.Catalog().open()
        statcan = sc.ConceptScheme(
            uri=self._STATCAN,
            label=InternationalString(en="StatCan"),
            concepts={"13": sc.Concept(notation="13", label=InternationalString(en="Labour"))},
        )
        eurovoc = sc.ConceptScheme(
            uri=self._EUROVOC,
            label=InternationalString(en="EuroVoc"),
            concepts={"13": sc.Concept(notation="13", label=InternationalString(en="Energy"))},
        )
        s.put(statcan)
        s.put(eurovoc)
        ds1 = sc.OpenDataset(
            identifier="DS1",
            title=InternationalString(en="Dataset 1"),
            description=InternationalString(en="Test"),
            themes=[sc.ConceptRef(uri=f"{self._STATCAN}/13", notation="13", scheme_uri=self._STATCAN)],
        )
        ds2 = sc.OpenDataset(
            identifier="DS2",
            title=InternationalString(en="Dataset 2"),
            description=InternationalString(en="Test"),
            themes=[sc.ConceptRef(uri=f"{self._EUROVOC}/13", notation="13", scheme_uri=self._EUROVOC)],
        )
        s.put(ds1)
        s.put(ds2)
        return s

    def test_same_notation_different_scheme_stored_separately(self) -> None:
        s = self._build_store()
        # Both concepts are stored with distinct URIs
        rows = s.connection.execute(
            "SELECT uri, in_scheme, notation FROM statcat.concept ORDER BY in_scheme"
        ).fetchall()
        assert len(rows) == 2
        uris = {r[0] for r in rows}
        assert f"{self._EUROVOC}/13" in uris
        assert f"{self._STATCAN}/13" in uris

    def test_concept_labels_per_scheme(self) -> None:
        s = self._build_store()
        statcan_labels = s.concept_labels(scheme_uri=self._STATCAN)
        assert statcan_labels == {f"{self._STATCAN}/13": "Labour"}
        eurovoc_labels = s.concept_labels(scheme_uri=self._EUROVOC)
        assert eurovoc_labels == {f"{self._EUROVOC}/13": "Energy"}

    def test_facets_with_different_schemes(self) -> None:
        """Themes from different schemes appear in facets as distinct URIs."""
        s = self._build_store()
        facets = s.facets("themes")
        theme_counts = facets["themes"]
        assert f"{self._STATCAN}/13" in theme_counts
        assert f"{self._EUROVOC}/13" in theme_counts


class TestFilter:
    def test_filter_by_publisher(self, store):
        result = store.query(sc.BaseDataset, publisher="ECB").execute()
        assert all(ds.publisher is not None and ds.publisher.name.get("en") == "ECB" for ds in result.items)
        assert result.total >= 1

    def test_filter_by_status(self, store):
        result = store.query(sc.BaseDataset, status="archived").execute()
        assert all(ds.status == "archived" for ds in result.items)
        assert result.total == 2

    def test_filter_by_frequency(self, store):
        result = store.query(sc.BaseDataset, frequency="DAILY").execute()
        assert all(ds.frequency is not None and ds.frequency.id == "DAILY" for ds in result.items)
        assert result.total == 2

    def test_filter_narrows_to_aggregate(self, store):
        result = store.query(sc.AggregateDataset, publisher="ECB").execute()
        assert all(isinstance(ds, sc.AggregateDataset) for ds in result.items)

    def test_filter_no_match(self, store):
        result = store.query(sc.BaseDataset, publisher="NONEXISTENT").execute()
        assert result.items == []
        assert result.total == 0


class TestTransaction:
    def test_transaction_commits(self):
        s = sc.Catalog().open()
        with s.transaction():
            s.put(sc.OpenDataset(identifier="TX1", title=_i("T1"), description=_i("d")))
            s.put(sc.OpenDataset(identifier="TX2", title=_i("T2"), description=_i("d")))
        assert s.get(sc.OpenDataset, "TX1") is not None
        assert s.get(sc.OpenDataset, "TX2") is not None

    def test_transaction_rolls_back(self):
        s = sc.Catalog().open()
        s.put(sc.OpenDataset(identifier="BEFORE", title=_i("Before"), description=_i("d")))
        with pytest.raises(RuntimeError, match="boom"):  # noqa: PT012, SIM117
            with s.transaction():
                s.put(sc.OpenDataset(identifier="FAIL", title=_i("Fail"), description=_i("d")))
                msg = "boom"
                raise RuntimeError(msg)
        assert s.get(sc.OpenDataset, "BEFORE") is not None
        assert s.get(sc.OpenDataset, "FAIL") is None

    def test_transaction_with_bulk_load(self, tmp_path):
        s = sc.Catalog(str(tmp_path / "catalog")).open()
        with s.bulk_load(), s.transaction():
            s.put(sc.OpenDataset(identifier="BL1", title=_i("BL1"), description=_i("d")))
            s.put(sc.OpenDataset(identifier="BL2", title=_i("BL2"), description=_i("d")))
        assert s.get(sc.OpenDataset, "BL1") is not None
        assert s.get(sc.OpenDataset, "BL2") is not None

    def test_bulk_load_reentrant(self, tmp_path):
        """Nested bulk_load() is a no-op — only the outermost checkpoints."""
        s = sc.Catalog(str(tmp_path / "catalog")).open()
        with s.bulk_load():
            s.put(sc.OpenDataset(identifier="OUTER", title=_i("Outer"), description=_i("d")))
            with s.bulk_load():
                s.put(sc.OpenDataset(identifier="INNER", title=_i("Inner"), description=_i("d")))
            # inner exit should NOT have reset WAL or checkpointed
            s.put(sc.OpenDataset(identifier="AFTER", title=_i("After"), description=_i("d")))
        assert s.get(sc.OpenDataset, "OUTER") is not None
        assert s.get(sc.OpenDataset, "INNER") is not None
        assert s.get(sc.OpenDataset, "AFTER") is not None


class TestEncoderNarrowing:
    """`as_vector` / `as_vectors` are the only place an encoder's output is trusted.

    `EmbeddingModel.encode` returns `object`, deliberately: a real encoder's
    declared return is wide enough to include ``dict[str, Tensor]``, so
    promising `Iterable[float]` in the Protocol made no actual encoder satisfy
    it. The narrowing lives here instead, two functions wide.
    """

    def test_accepts_an_ndarray(self):
        np = pytest.importorskip("numpy")

        from statcatlib.store.store import as_vector, as_vectors

        assert as_vector(np.array([1.0, 2.0])) == [1.0, 2.0]
        assert as_vectors(np.array([[1.0, 2.0], [3.0, 4.0]])) == [[1.0, 2.0], [3.0, 4.0]]

    def test_accepts_plain_sequences(self):
        from statcatlib.store.store import as_vector

        assert as_vector([1, 2]) == [1.0, 2.0]
        assert as_vector((1.5,)) == [1.5]

    @pytest.mark.parametrize("bad", ["abc", b"abc", 5, None], ids=["str", "bytes", "int", "none"])
    def test_rejects_non_iterables_and_strings(self, bad):
        from statcatlib.store.store import as_vector

        with pytest.raises(TypeError, match="expected an iterable of floats"):
            as_vector(bad)

    def test_rejects_a_mapping(self):
        """The live case: `encode` may return ``dict[str, Tensor]``.

        A dict is iterable — over its keys — so without an explicit guard this
        reaches ``float("some_key")`` and raises ValueError naming nothing
        useful.
        """
        from statcatlib.store.store import as_vector

        with pytest.raises(TypeError, match="expected an iterable of floats"):
            as_vector({"sentence_embedding": [1.0, 2.0]})

    def test_reports_non_numbers_as_a_type_error(self):
        from statcatlib.store.store import as_vector

        with pytest.raises(TypeError, match="of non-numbers"):
            as_vector(["x", "y"])

"""Tests for QueryBuilder chaining and terminal methods."""

import pytest

import statcatlib as sc

pytestmark = pytest.mark.unit


class TestChaining:
    def test_filter_chaining(self, store):
        result = store.query(sc.BaseDataset).filter(publisher="ECB").filter(status="current").execute()
        assert all(ds.publisher is not None and ds.publisher.name.get("en") == "ECB" for ds in result.items)
        assert all(ds.status == "current" for ds in result.items)

    def test_filter_and_search(self, indexed_store: sc.Catalog):
        """Filter and BM25 search compose (#117).

        Uses the indexed fixture: `QueryBuilder.search` needs a built index,
        which the in-memory `store` cannot have. Unlike `Catalog.search`,
        this yields the entities themselves, not `Hit` wrappers.
        """
        result = indexed_store.query(sc.BaseDataset).filter(publisher="ECB").search("exchange").execute()
        assert any(ds.identifier == "EXR" for ds in result.items)

    def test_sort_by_identifier(self, store):
        result = store.query(sc.BaseDataset).sort_by("identifier").execute()
        ids = [ds.identifier for ds in result.items]
        assert ids == sorted(ids)

    def test_sort_desc(self, store):
        result = store.query(sc.BaseDataset).sort_by("identifier", desc=True).execute()
        ids = [ds.identifier for ds in result.items]
        assert ids == sorted(ids, reverse=True)

    def test_sort_by_title(self, store):
        result = store.query(sc.BaseDataset).sort_by("title", lang="en").execute()
        titles = [ds.title.get("en") for ds in result.items]
        assert titles == sorted(titles, key=lambda t: t or "")

    def test_sort_by_title_desc(self, store):
        result = store.query(sc.BaseDataset).sort_by("title", lang="en", desc=True).execute()
        titles = [ds.title.get("en") for ds in result.items]
        assert titles == sorted(titles, key=lambda t: t or "", reverse=True)

    def test_sort_by_title_requires_lang(self, store):
        with pytest.raises(ValueError, match="lang is required"):
            store.query(sc.BaseDataset).sort_by("title").execute()

    def test_limit(self, store):
        result = store.query(sc.BaseDataset).limit(3).execute()
        assert len(result.items) == 3

    def test_offset(self, store):
        all_result = store.query(sc.BaseDataset).sort_by("identifier").execute()
        offset_result = store.query(sc.BaseDataset).sort_by("identifier").offset(2).execute()
        assert offset_result.total == all_result.total
        assert offset_result.items[0].identifier == all_result.items[2].identifier

    def test_limit_and_offset(self, store):
        result = store.query(sc.BaseDataset).sort_by("identifier").limit(2).offset(1).execute()
        assert len(result.items) == 2

    def test_with_facets(self, store):
        result = store.query(sc.BaseDataset).filter(status="current").with_facets("publisher", "frequency").execute()
        assert "publisher" in result.facets
        assert "frequency" in result.facets
        # Facets should only count over the filtered set (current only)
        assert "ECB" in result.facets["publisher"]

    def test_facets_reflect_filtered_set(self, store):
        result_all = store.query(sc.BaseDataset).with_facets("publisher").execute()
        result_ecb = store.query(sc.BaseDataset).filter(publisher="ECB").with_facets("publisher").execute()
        total_all = sum(result_all.facets["publisher"].values())
        total_ecb = sum(result_ecb.facets["publisher"].values())
        assert total_ecb < total_all


class TestTerminalMethods:
    def test_all(self, store):
        items = store.query(sc.Dataset, publisher="ECB").all()
        assert isinstance(items, list)
        assert all(ds.publisher is not None and ds.publisher.name.get("en") == "ECB" for ds in items)

    def test_first_returns_one(self, store):
        ds = store.query(sc.Dataset, publisher="ECB").first()
        assert ds is not None
        assert ds.publisher is not None
        assert ds.publisher.name.get("en") == "ECB"

    def test_first_returns_none_on_empty(self, store):
        ds = store.query(sc.Dataset, publisher="NOBODY").first()
        assert ds is None

    def test_one_success(self, store):
        ds = store.query(sc.Dataset, publisher="ECB").filter(identifier="EXR").one()
        assert ds.identifier == "EXR"

    def test_one_raises_on_no_results(self, store):
        with pytest.raises(KeyError):
            store.query(sc.Dataset, publisher="NOBODY").one()

    def test_one_raises_on_multiple_results(self, store):
        with pytest.raises(ValueError, match="Expected exactly one"):
            store.query(sc.Dataset, publisher="ECB").one()

    def test_count(self, store):
        n = store.query(sc.Dataset, publisher="ECB").count()
        assert n >= 1
        assert n == store.query(sc.Dataset, publisher="ECB").execute().total

    def test_count_zero(self, store):
        n = store.query(sc.Dataset, publisher="NOBODY").count()
        assert n == 0


class TestSearchResult:
    def test_result_attributes(self, store):
        result = store.query(sc.BaseDataset).execute()
        assert hasattr(result, "items")
        assert hasattr(result, "total")
        assert hasattr(result, "facets")
        assert hasattr(result, "offset")
        assert result.facets == {}
        assert result.offset == 0

    def test_total_matches_all_items(self, store):
        result = store.query(sc.BaseDataset).execute()
        assert isinstance(result.total, int)
        assert result.total == 12  # all fixture datasets


class TestSelectProjection:
    def test_select_all_returns_dicts(self, store):
        rows = store.query(sc.BaseDataset).select("identifier", "status").rows()
        assert isinstance(rows, list)
        assert len(rows) > 0
        assert isinstance(rows[0], dict)
        assert "identifier" in rows[0]
        assert "status" in rows[0]

    def test_select_with_map_column(self, store):
        rows = store.query(sc.BaseDataset).select("identifier", "title", lang="en").rows()
        assert len(rows) > 0
        assert "title" in rows[0]
        assert isinstance(rows[0]["title"], str)

    def test_select_first_returns_dict(self, store):
        row = store.query(sc.BaseDataset).select("identifier", "status").row()
        assert row is not None
        assert isinstance(row, dict)
        assert "identifier" in row

    def test_select_first_returns_none_when_empty(self, store):
        row = store.query(sc.BaseDataset).filter(publisher="NOBODY").select("identifier").row()
        assert row is None

    def test_select_row_returns_dict(self, store):
        row = store.query(sc.BaseDataset).filter(identifier="EXR").select("identifier", "status").row()
        assert row is not None
        assert isinstance(row, dict)
        assert row["identifier"] == "EXR"

    def test_select_respects_filter(self, store):
        rows = store.query(sc.BaseDataset).filter(publisher="ECB").select("identifier", "status").rows()
        # All ECB datasets from fixture
        ids = {r["identifier"] for r in rows}
        assert "EXR" in ids
        assert "LFS" not in ids  # StatCan

    def test_select_respects_limit(self, store):
        rows = store.query(sc.BaseDataset).limit(3).select("identifier").rows()
        assert len(rows) == 3

    def test_select_respects_sort(self, store):
        rows = store.query(sc.BaseDataset).sort_by("title", lang="en").select("identifier", "title", lang="en").rows()
        titles = [r["title"] for r in rows]
        assert titles == sorted(titles, key=lambda t: t or "")

    def test_select_map_requires_lang(self, store):
        with pytest.raises(ValueError, match="lang is required"):
            store.query(sc.BaseDataset).select("identifier", "title")

    def test_select_invalid_column(self, store):
        with pytest.raises(ValueError, match="Invalid select column"):
            store.query(sc.BaseDataset).select("nonexistent")

    def test_select_returns_row_instances(self, store):
        rows = store.query(sc.BaseDataset).select("identifier", "status").rows()
        assert all(isinstance(r, sc.Row) for r in rows)

    def test_row_attribute_access(self, store):
        row = store.query(sc.BaseDataset).filter(identifier="EXR").select("identifier", "status").row()
        assert row is not None
        assert isinstance(row, sc.Row)
        assert row.identifier == "EXR"
        assert row["identifier"] == "EXR"  # dict-style still works

    def test_row_is_dict_subclass(self, store):
        row = store.query(sc.BaseDataset).select("identifier", "status").row()
        assert isinstance(row, dict)

    def test_row_missing_attr_raises_attribute_error(self, store):
        row = store.query(sc.BaseDataset).select("identifier").row()
        assert row is not None
        with pytest.raises(AttributeError, match="nonexistent"):
            _ = row.nonexistent


class TestScalarsProjection:
    def test_scalars_all_returns_flat_list(self, store):
        ids = store.query(sc.BaseDataset).scalars("identifier").values()
        assert isinstance(ids, list)
        assert len(ids) > 0
        assert isinstance(ids[0], str)

    def test_scalars_with_filter(self, store):
        ids = store.query(sc.BaseDataset).filter(publisher="ECB").scalars("identifier").values()
        assert "EXR" in ids
        assert "LFS" not in ids

    def test_scalars_first_returns_value(self, store):
        val = store.query(sc.BaseDataset).scalars("identifier").value()
        assert val is not None
        assert isinstance(val, str)

    def test_scalars_first_none_when_empty(self, store):
        val = store.query(sc.BaseDataset).filter(publisher="NOBODY").scalars("identifier").value()
        assert val is None

    def test_scalars_map_column(self, store):
        titles = store.query(sc.BaseDataset).scalars("title", lang="en").values()
        assert len(titles) > 0
        assert all(isinstance(t, str) for t in titles)

    def test_scalars_map_requires_lang(self, store):
        with pytest.raises(ValueError, match="lang is required"):
            store.query(sc.BaseDataset).scalars("title")

    def test_scalars_invalid_column(self, store):
        with pytest.raises(ValueError, match="Invalid scalar column"):
            store.query(sc.BaseDataset).scalars("nonexistent")


class TestExists:
    def test_exists_true(self, store):
        assert store.query(sc.BaseDataset).filter(publisher="ECB").exists() is True

    def test_exists_false(self, store):
        assert store.query(sc.BaseDataset).filter(publisher="NOBODY").exists() is False

    def test_exists_with_kind(self, store):
        assert store.query(sc.AggregateDataset).exists() is True

    def test_exists_empty_store(self):
        s = sc.Catalog().open()
        assert s.query(sc.BaseDataset).exists() is False


class TestInclude:
    def test_include_publisher_loads_publisher(self, store):
        items = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").all()
        assert len(items) > 0
        assert all(ds.publisher is not None for ds in items)
        assert all(ds.publisher.name.get("en") == "ECB" for ds in items)

    def test_include_skips_undeclared_relationships(self, store):
        items = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").all()
        # themes was not included — should be UNLOADED (not silently empty)
        assert all(sc.is_unloaded(ds.themes) for ds in items)

    def test_include_accumulates_across_calls(self, store):
        items = (
            store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").include("themes", "keywords").all()
        )
        assert all(ds.publisher is not None for ds in items)
        # themes and keywords are now loaded (may be empty for some datasets, but not unloaded)
        assert all(isinstance(ds.themes, list) for ds in items)
        assert all(isinstance(ds.keywords, list) for ds in items)

    def test_include_multiple_in_one_call(self, store):
        items = store.query(sc.BaseDataset).include("publisher", "themes", "keywords").all()
        assert len(items) > 0
        assert all(isinstance(ds.themes, list) for ds in items)
        assert all(isinstance(ds.keywords, list) for ds in items)

    def test_full_loads_all_relationships(self, store):
        items = store.query(sc.BaseDataset).filter(publisher="ECB").full().all()
        assert len(items) > 0
        # full() should behave identically to no include() call
        assert all(ds.publisher is not None for ds in items)

    def test_full_after_include_resets_to_all(self, store):
        # include() narrows; full() afterwards reverts to loading everything
        items = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").full().all()
        assert len(items) > 0
        assert all(ds.publisher is not None for ds in items)

    def test_include_unknown_relationship_raises(self, store):
        with pytest.raises(ValueError, match="Unknown relationship"):
            store.query(sc.BaseDataset).include("not_a_real_field")

    def test_unloaded_field_raises_not_loaded_error_on_attr_access(self, store):
        ds = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").first()
        assert ds is not None
        # Accessing .themes itself returns UNLOADED (no raise yet).
        # Using it raises NotLoadedError.
        with pytest.raises(sc.NotLoadedError, match="themes"):
            _ = ds.themes[0]  # indexing triggers the error

    def test_unloaded_list_field_raises_on_iteration(self, store):
        ds = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").first()
        assert ds is not None
        with pytest.raises(sc.NotLoadedError, match="keywords"):
            list(ds.keywords)

    def test_unloaded_list_field_raises_on_len(self, store):
        ds = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").first()
        assert ds is not None
        with pytest.raises(sc.NotLoadedError, match="distributions"):
            len(ds.distributions)

    def test_null_fk_stays_none_when_not_included(self, store):
        # A dataset with no publisher (FK null in the row) should return None
        # even when "publisher" is not in include_rels — we know from the
        # main row that there is no publisher.
        ds = store.query(sc.BaseDataset).include("themes").first()
        assert ds is not None
        # If publisher is None it means the dataset genuinely has no publisher
        # (not UNLOADED). If it has one, it will be UNLOADED (falsy).
        if ds.publisher is not None:
            assert sc.is_unloaded(ds.publisher)

    def test_is_unloaded_helper(self, store):
        ds = store.query(sc.BaseDataset).filter(publisher="ECB").include("publisher").first()
        assert ds is not None
        assert not sc.is_unloaded(ds.publisher)
        assert sc.is_unloaded(ds.themes)

    def test_get_always_full_no_unloaded(self, store):
        ds = store.get(sc.BaseDataset, "EXR")
        assert ds is not None
        assert not sc.is_unloaded(ds.themes)
        assert not sc.is_unloaded(ds.publisher)
        assert not sc.is_unloaded(ds.distributions)

    def test_include_is_immutable(self, store):
        # Calling .include() on a builder must not affect the original.
        # Without include(), the default full load populates publisher.
        # With include() on a derived builder, the original still loads publisher.
        base = store.query(sc.BaseDataset).filter(publisher="ECB")
        narrowed = base.include("keywords")  # themes excluded on narrowed
        base_items = base.all()
        narrowed_items = narrowed.all()
        # base still loads publisher (full load), narrowed also loads via include
        assert all(ds.publisher is not None for ds in base_items)
        # the two builders are independent — same results but different load paths
        assert {ds.identifier for ds in base_items} == {ds.identifier for ds in narrowed_items}

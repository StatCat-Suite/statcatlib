"""Tests for dataset lineage and provenance tracking.

Covers add_lineage(), store.dataset().lineage_records(),
store.dataset().upstream(), store.dataset().downstream().
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sdmxlib import InternationalString

import statcatlib as sc

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _ds(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=_i(identifier),
        description=_i("desc"),
    )


def _store_with_chain() -> sc.Catalog:
    """Store with a four-node lineage chain: RAW → CLEAN → DERIVED → REPORT.

    RAW is the root (no lineage).
    CLEAN was derived from RAW.
    DERIVED was derived from CLEAN.
    REPORT was derived from DERIVED.
    """
    store = sc.Catalog().open()
    for ds_id in ["RAW", "CLEAN", "DERIVED", "REPORT"]:
        store.put(_ds(ds_id))

    store.add_lineage("RAW", "CLEAN", "derived_from", activity_type="aggregation")
    store.add_lineage("CLEAN", "DERIVED", "derived_from", activity_type="aggregation")
    store.add_lineage("DERIVED", "REPORT", "derived_from", activity_type="filter")
    return store


# ---------------------------------------------------------------------------
# add_lineage
# ---------------------------------------------------------------------------


class TestAddLineage:
    def test_minimal(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SOURCE", "DS1", "derived_from")
        rows = store.dataset("DS1").lineage_records()
        assert len(rows) == 1
        assert rows[0]["source_id"] == "SOURCE"
        assert rows[0]["target_id"] == "DS1"
        assert rows[0]["relationship"] == "derived_from"

    def test_all_fields(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        at = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        store.add_lineage(
            "SRC",
            "DS1",
            "aggregated_from",
            activity_label="Monthly tabulation",
            activity_type="aggregation",
            agent_id="statcan",
            timestamp=at,
            confidence="extracted",
            notes="Extracted from source documentation",
        )
        rows = store.dataset("DS1").lineage_records()
        assert len(rows) == 1
        r = rows[0]
        assert r["activity_label"] == "Monthly tabulation"
        assert r["activity_type"] == "aggregation"
        assert r["agent_id"] == "statcan"
        assert r["confidence"] == "extracted"
        assert r["notes"] == "Extracted from source documentation"

    def test_default_confidence_is_asserted(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC", "DS1", "derived_from")
        rows = store.dataset("DS1").lineage_records()
        assert rows[0]["confidence"] == "asserted"

    def test_idempotent_upsert(self) -> None:
        """Re-recording the same (source, target, relationship) triple upserts."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC", "DS1", "derived_from", notes="v1")
        store.add_lineage("SRC", "DS1", "derived_from", notes="v2")
        rows = store.dataset("DS1").lineage_records()
        assert len(rows) == 1
        assert rows[0]["notes"] == "v2"

    def test_multiple_sources_same_target(self) -> None:
        """A target may have multiple source edges (data fusion)."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC1", "DS1", "derived_from")
        store.add_lineage("SRC2", "DS1", "derived_from")
        rows = store.dataset("DS1").lineage_records(role="target")
        assert len(rows) == 2
        sources = {r["source_id"] for r in rows}
        assert sources == {"SRC1", "SRC2"}

    def test_different_relationship_types_are_separate_rows(self) -> None:
        """Same source+target but different relationship creates a new row."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC", "DS1", "derived_from")
        store.add_lineage("SRC", "DS1", "revision_of")
        rows = store.dataset("DS1").lineage_records()
        assert len(rows) == 2
        rels = {r["relationship"] for r in rows}
        assert rels == {"derived_from", "revision_of"}

    def test_all_relationship_types_accepted(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        for rel in sc.Catalog.LINEAGE_RELATIONSHIPS:
            store.add_lineage("SRC", "DS1", rel)
        rows = store.dataset("DS1").lineage_records()
        stored_rels = {r["relationship"] for r in rows}
        assert stored_rels == set(sc.Catalog.LINEAGE_RELATIONSHIPS)


# ---------------------------------------------------------------------------
# lineage_records
# ---------------------------------------------------------------------------


class TestLineageRecords:
    def test_empty_for_new_dataset(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        assert store.dataset("DS1").lineage_records() == []

    def test_empty_for_nonexistent_dataset(self) -> None:
        store = sc.Catalog().open()
        assert store.dataset("NONEXISTENT").lineage_records() == []

    def test_returns_dicts_with_expected_keys(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC", "DS1", "derived_from")
        rows = store.dataset("DS1").lineage_records()
        r = rows[0]
        assert isinstance(r, dict)
        for key in (
            "id",
            "source_id",
            "target_id",
            "relationship",
            "activity_label",
            "activity_type",
            "agent_id",
            "timestamp",
            "confidence",
            "notes",
        ):
            assert key in r

    def test_role_source(self) -> None:
        """role='source' returns rows where the dataset is the upstream input."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("DS1", "CHILD", "derived_from")
        store.add_lineage("PARENT", "DS1", "derived_from")

        rows = store.dataset("DS1").lineage_records(role="source")
        assert all(r["source_id"] == "DS1" for r in rows)
        assert len(rows) == 1

    def test_role_target(self) -> None:
        """role='target' returns rows where the dataset is the downstream output."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("DS1", "CHILD", "derived_from")
        store.add_lineage("PARENT", "DS1", "derived_from")

        rows = store.dataset("DS1").lineage_records(role="target")
        assert all(r["target_id"] == "DS1" for r in rows)
        assert len(rows) == 1

    def test_role_either_returns_both(self) -> None:
        """Default role='either' returns rows where dataset is source OR target."""
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("DS1", "CHILD", "derived_from")
        store.add_lineage("PARENT", "DS1", "derived_from")

        rows = store.dataset("DS1").lineage_records()
        assert len(rows) == 2

    def test_filter_by_relationship(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC", "DS1", "derived_from")
        store.add_lineage("SRC2", "DS1", "revision_of")

        rows = store.dataset("DS1").lineage_records(relationship="derived_from")
        assert all(r["relationship"] == "derived_from" for r in rows)
        assert len(rows) == 1

    def test_filter_by_confidence(self) -> None:
        store = sc.Catalog().open()
        store.put(_ds("DS1"))
        store.add_lineage("SRC1", "DS1", "derived_from", confidence="asserted")
        store.add_lineage("SRC2", "DS1", "revision_of", confidence="extracted")

        rows = store.dataset("DS1").lineage_records(confidence="asserted")
        assert all(r["confidence"] == "asserted" for r in rows)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# upstream
# ---------------------------------------------------------------------------


class TestUpstream:
    def test_direct_parent(self) -> None:
        store = _store_with_chain()
        result = store.dataset("CLEAN").upstream().collect()
        ids = {ds.identifier for ds in result}
        assert "RAW" in ids

    def test_transitive_ancestors(self) -> None:
        store = _store_with_chain()
        result = store.dataset("REPORT").upstream().collect()
        ids = {ds.identifier for ds in result}
        assert "DERIVED" in ids
        assert "CLEAN" in ids
        assert "RAW" in ids

    def test_self_excluded(self) -> None:
        store = _store_with_chain()
        result = store.dataset("REPORT").upstream().collect()
        ids = {ds.identifier for ds in result}
        assert "REPORT" not in ids

    def test_root_has_no_upstream(self) -> None:
        store = _store_with_chain()
        result = store.dataset("RAW").upstream().collect()
        assert result == []

    def test_nonexistent_returns_empty(self) -> None:
        store = _store_with_chain()
        result = store.dataset("NONEXISTENT").upstream().collect()
        assert result == []

    def test_returns_base_dataset_instances(self) -> None:
        store = _store_with_chain()
        result = store.dataset("DERIVED").upstream().collect()
        assert all(isinstance(ds, sc.BaseDataset) for ds in result)

    def test_source_not_in_catalog_excluded(self) -> None:
        """If a source_id isn't in the datasets table, it's silently skipped."""
        store = sc.Catalog().open()
        store.put(_ds("CHILD"))
        store.add_lineage("GHOST_PARENT", "CHILD", "derived_from")
        result = store.dataset("CHILD").upstream().collect()
        ids = {ds.identifier for ds in result}
        assert "GHOST_PARENT" not in ids

    def test_relationship_filter(self) -> None:
        """upstream(relationship=...) only follows edges of that type."""
        store = sc.Catalog().open()
        for ds_id in ["BASE", "ADJUSTED", "PUBLISHED"]:
            store.put(_ds(ds_id))
        store.add_lineage("BASE", "ADJUSTED", "adjusted_from")
        store.add_lineage("ADJUSTED", "PUBLISHED", "derived_from")

        # Only follow derived_from — stops at ADJUSTED
        result = store.dataset("PUBLISHED").upstream(relationship="derived_from").collect()
        ids = {ds.identifier for ds in result}
        assert "ADJUSTED" in ids
        assert "BASE" not in ids

    def test_depth_bound(self) -> None:
        """depth=1 returns only the direct parent."""
        store = _store_with_chain()
        result = store.dataset("REPORT").upstream(depth=1).collect()
        ids = {ds.identifier for ds in result}
        assert "DERIVED" in ids
        assert "CLEAN" not in ids
        assert "RAW" not in ids

    def test_diamond_no_duplicates(self) -> None:
        """Diamond: ROOT → A, ROOT → B, MERGED ← A, MERGED ← B.
        upstream(MERGED) returns each ancestor only once.
        """
        store = sc.Catalog().open()
        for ds_id in ["ROOT", "A", "B", "MERGED"]:
            store.put(_ds(ds_id))
        store.add_lineage("ROOT", "A", "derived_from")
        store.add_lineage("ROOT", "B", "derived_from")
        store.add_lineage("A", "MERGED", "derived_from")
        store.add_lineage("B", "MERGED", "derived_from")

        result = store.dataset("MERGED").upstream().collect()
        ids = [ds.identifier for ds in result]
        assert len(ids) == len(set(ids))
        assert set(ids) == {"A", "B", "ROOT"}


# ---------------------------------------------------------------------------
# downstream
# ---------------------------------------------------------------------------


class TestDownstream:
    def test_direct_child(self) -> None:
        store = _store_with_chain()
        result = store.dataset("CLEAN").downstream().collect()
        ids = {ds.identifier for ds in result}
        assert "DERIVED" in ids

    def test_transitive_descendants(self) -> None:
        store = _store_with_chain()
        result = store.dataset("RAW").downstream().collect()
        ids = {ds.identifier for ds in result}
        assert "CLEAN" in ids
        assert "DERIVED" in ids
        assert "REPORT" in ids

    def test_self_excluded(self) -> None:
        store = _store_with_chain()
        result = store.dataset("RAW").downstream().collect()
        ids = {ds.identifier for ds in result}
        assert "RAW" not in ids

    def test_leaf_has_no_downstream(self) -> None:
        store = _store_with_chain()
        result = store.dataset("REPORT").downstream().collect()
        assert result == []

    def test_nonexistent_returns_empty(self) -> None:
        store = _store_with_chain()
        result = store.dataset("NONEXISTENT").downstream().collect()
        assert result == []

    def test_returns_base_dataset_instances(self) -> None:
        store = _store_with_chain()
        result = store.dataset("CLEAN").downstream().collect()
        assert all(isinstance(ds, sc.BaseDataset) for ds in result)

    def test_branching_downstream(self) -> None:
        """A source with two children should return both downstream."""
        store = sc.Catalog().open()
        for ds_id in ["ROOT", "BRANCH_A", "BRANCH_B"]:
            store.put(_ds(ds_id))
        store.add_lineage("ROOT", "BRANCH_A", "derived_from")
        store.add_lineage("ROOT", "BRANCH_B", "derived_from")

        result = store.dataset("ROOT").downstream().collect()
        ids = {ds.identifier for ds in result}
        assert "BRANCH_A" in ids
        assert "BRANCH_B" in ids

    def test_relationship_filter(self) -> None:
        """downstream(relationship=...) only follows edges of that type."""
        store = sc.Catalog().open()
        for ds_id in ["BASE", "ADJUSTED", "PUBLISHED"]:
            store.put(_ds(ds_id))
        store.add_lineage("BASE", "ADJUSTED", "adjusted_from")
        store.add_lineage("BASE", "PUBLISHED", "derived_from")

        result = store.dataset("BASE").downstream(relationship="derived_from").collect()
        ids = {ds.identifier for ds in result}
        assert "PUBLISHED" in ids
        assert "ADJUSTED" not in ids

    def test_depth_bound(self) -> None:
        """depth=1 returns only the direct child."""
        store = _store_with_chain()
        result = store.dataset("RAW").downstream(depth=1).collect()
        ids = {ds.identifier for ds in result}
        assert "CLEAN" in ids
        assert "DERIVED" not in ids
        assert "REPORT" not in ids

    def test_target_not_in_catalog_excluded(self) -> None:
        """If a target_id isn't in the datasets table, it's silently skipped."""
        store = sc.Catalog().open()
        store.put(_ds("PARENT"))
        store.add_lineage("PARENT", "GHOST_CHILD", "derived_from")
        result = store.dataset("PARENT").downstream().collect()
        ids = {ds.identifier for ds in result}
        assert "GHOST_CHILD" not in ids


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestLineageConstants:
    def test_relationship_vocabulary(self) -> None:
        expected = {
            "derived_from",
            "revision_of",
            "supplement_to",
            "feeds_into",
            "adjusted_from",
            "aggregated_from",
            "uses_classification",
            "replaces",
        }
        assert set(sc.Catalog.LINEAGE_RELATIONSHIPS) == expected

    def test_confidence_vocabulary(self) -> None:
        assert set(sc.Catalog.LINEAGE_CONFIDENCE) == {"asserted", "inferred", "extracted"}

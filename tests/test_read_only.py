"""Tests for ``Catalog(..., read_only=True)`` — multi-worker safe opens.

Issue #88: opening a folder catalog read-write takes an exclusive DuckDB
file lock, which prevents running multiple uvicorn workers against the
same bundle. ``read_only=True`` opens the DuckDB connection with
``read_only=True``, ATTACHes the DuckLake sidecar ``READ_ONLY``, and
disables every write path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sdmxlib import InternationalString

import statcatlib as sc
from tests.conftest import untyped

pytestmark = pytest.mark.unit


def _i(en: str) -> InternationalString:
    return InternationalString(en=en)


def _seed(path: Path) -> None:
    """Build a small folder-mode catalog at *path*."""
    with sc.Catalog(path).open() as cat:
        cat.put(
            sc.OpenDataset(
                identifier="DS-1",
                title=_i("First"),
                description=_i("First dataset"),
                publisher=sc.Agent(id="PUB", name=_i("Pub")),
                status="current",
            )
        )
        cat.put(
            sc.OpenDataset(
                identifier="DS-2",
                title=_i("Second"),
                description=_i("Second dataset"),
                publisher=sc.Agent(id="PUB", name=_i("Pub")),
                status="current",
            )
        )


class TestReadOnlyOpen:
    def test_query_works_read_only(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cat"
        _seed(bundle)
        with sc.Catalog(bundle, read_only=True).open() as cat:
            ds = cat.get(sc.OpenDataset, "DS-1")
            assert ds is not None
            assert ds.identifier == "DS-1"
            rows = cat.query(sc.OpenDataset).execute().items
            assert {d.identifier for d in rows} == {"DS-1", "DS-2"}

    def test_no_exclusive_lock(self, tmp_path: Path) -> None:
        """Issue #88: read_only=True must not take the DuckDB exclusive lock.

        Repro is cross-process (uvicorn workers), but the underlying
        symptom is observable in-process by opening a second raw DuckDB
        connection to the same ``meta.duckdb`` — read-write open takes
        an exclusive lock that fails the second connect; read-only opens
        coexist freely.
        """
        import duckdb

        bundle = tmp_path / "cat"
        _seed(bundle)
        with sc.Catalog(bundle, read_only=True).open():
            # Second concurrent read-only opener succeeds — no lock contention.
            second = duckdb.connect(str(bundle / "meta.duckdb"), read_only=True)
            try:
                second.execute("SELECT 1").fetchone()
            finally:
                second.close()


class TestReadOnlyWriteGuards:
    def test_put_raises(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cat"
        _seed(bundle)
        with sc.Catalog(bundle, read_only=True).open() as cat, pytest.raises(RuntimeError, match=r"read_only"):
            cat.put(
                sc.OpenDataset(
                    identifier="DS-NEW",
                    title=_i("New"),
                    description=_i("nope"),
                    publisher=sc.Agent(id="PUB", name=_i("Pub")),
                    status="current",
                )
            )

    def test_bulk_load_raises(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cat"
        _seed(bundle)
        with (
            sc.Catalog(bundle, read_only=True).open() as cat,
            pytest.raises(RuntimeError, match=r"read_only"),
            cat.bulk_load(),
        ):
            pass

    def test_build_index_raises(self, tmp_path: Path) -> None:
        bundle = tmp_path / "cat"
        _seed(bundle)
        with sc.Catalog(bundle, read_only=True).open() as cat, pytest.raises(RuntimeError, match=r"read_only"):
            cat.build_index()


class TestReadOnlyValidation:
    def test_memory_uri_rejected(self) -> None:
        with pytest.raises(ValueError, match="in-memory"):
            sc.Catalog(":memory:", read_only=True)

    def test_embedding_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="embedding_model"):
            sc.Catalog("/tmp/whatever", embedding_model=untyped(object()), read_only=True)  # noqa: S108

    def test_missing_bundle_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"meta\.duckdb"):
            sc.Catalog(tmp_path / "nope", read_only=True).open()

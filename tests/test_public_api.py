"""Smoke tests for the public statcatlib API surface.

Verifies that everything a user needs is importable from ``import statcatlib as sc``
and that ``statcatlib.query`` is accessible as the filter DSL module.
"""

from __future__ import annotations

import annotationlib
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest
from obstore.store import MemoryStore

import statcatlib as sc
from tests.conftest import concept_of, open_scratch, scratch_store

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Import smoke test — everything reachable from top-level
# ---------------------------------------------------------------------------


class TestImports:
    def test_import_statcatlib(self) -> None:
        import statcatlib  # noqa: F401

    def test_import_as_cl(self) -> None:
        import statcatlib as sc  # noqa: F401

    def test_query_submodule_accessible(self) -> None:
        import statcatlib as sc

        assert hasattr(sc, "query")
        assert callable(sc.query.has_code)

    def test_valid_include_rels_is_public(self) -> None:
        """Documented as the list of valid `.include()` names, so it must be reachable."""
        import statcatlib as sc

        assert "VALID_INCLUDE_RELS" in sc.__all__
        assert "publisher" in sc.VALID_INCLUDE_RELS
        # every name it advertises is accepted by .include()
        with scratch_store() as store:
            store.query(sc.BaseDataset).include(*sc.VALID_INCLUDE_RELS)
        assert callable(sc.query.has_dimensions)
        assert callable(sc.query.distribution)
        assert callable(sc.query.has_service)


# ---------------------------------------------------------------------------
# Domain model types
# ---------------------------------------------------------------------------


class TestDomainModelExports:
    def test_base_dataset(self) -> None:
        import statcatlib as sc

        assert sc.BaseDataset is not None

    def test_dataset_union(self) -> None:
        import statcatlib as sc

        assert sc.Dataset is not None

    def test_dataset_variants(self) -> None:
        import statcatlib as sc

        assert sc.AggregateDataset is not None
        assert sc.OpenDataset is not None
        assert sc.MicrodataDataset is not None
        assert sc.GeospatialDataset is not None
        assert sc.PublicationDataset is not None

    def test_statistical_dataset_alias(self) -> None:
        """StatisticalDataset backward-compat alias must point to AggregateDataset."""
        import statcatlib as sc

        assert sc.StatisticalDataset is sc.AggregateDataset

    def test_variable(self) -> None:
        import statcatlib as sc

        assert sc.Variable is not None

    def test_agents(self) -> None:
        import statcatlib as sc

        assert sc.Agent is not None
        assert sc.ContactPoint is not None

    def test_concept(self) -> None:
        import statcatlib as sc

        assert sc.Concept is not None

    def test_temporal(self) -> None:
        import statcatlib as sc

        assert sc.TemporalCoverage is not None
        assert sc.Frequency is not None

    def test_spatial(self) -> None:
        import statcatlib as sc

        assert sc.SpatialCoverage is not None

    def test_licence(self) -> None:
        import statcatlib as sc

        assert sc.LicenceDocument is not None

    def test_quality(self) -> None:
        import statcatlib as sc

        assert sc.QualityAnnotation is not None
        assert sc.ProvenanceStatement is not None

    def test_classification(self) -> None:
        import statcatlib as sc

        assert sc.Standard is not None

    def test_distribution(self) -> None:
        import statcatlib as sc

        assert sc.Distribution is not None
        assert sc.DataService is not None

    def test_catalog(self) -> None:
        import statcatlib as sc

        assert sc.Catalog is not None
        assert sc.CatalogRecord is not None

    def test_international_string(self) -> None:
        import statcatlib as sc

        assert sc.InternationalString is not None
        s = sc.InternationalString(en="hello", fr="bonjour")
        assert s.as_dict().get("en") == "hello"
        assert s.as_dict().get("fr") == "bonjour"


# ---------------------------------------------------------------------------
# Store exports
# ---------------------------------------------------------------------------


class TestStoreExports:
    def test_catalog_model(self) -> None:
        import statcatlib as sc

        for name in ("Catalog", "Product", "Edition", "EditionView", "WorkingCopy", "checks"):
            assert name in sc.__all__, name
            assert getattr(sc, name) is not None

    def test_object_storage(self) -> None:
        import statcatlib as sc

        for name in ("BlobLocation", "TokenIdentity"):
            assert name in sc.__all__, name
            assert getattr(sc, name) is not None

    def test_upload(self) -> None:
        import statcatlib as sc

        for name in ("upload_edition", "UploadReport", "UploadCheckError"):
            assert name in sc.__all__, name
            assert getattr(sc, name) is not None

    def test_core_imports_without_the_blob_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`obstore` belongs to the ``blob`` extra, so importing statcatlib must not need it."""
        for name in [n for n in sys.modules if n == "statcatlib" or n.startswith("statcatlib.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "obstore", None)
        monkeypatch.setitem(sys.modules, "obstore.exceptions", None)

        reimported = importlib.import_module("statcatlib")

        assert reimported.BlobLocation is not None

    def test_a_blob_call_without_the_extra_names_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from statcatlib.store.blob import _ObjectOps

        monkeypatch.setitem(sys.modules, "obstore", None)
        monkeypatch.setitem(sys.modules, "obstore.exceptions", None)

        with pytest.raises(ImportError, match=r"uv add 'statcatlib\[blob\]'.*pip install 'statcatlib\[blob\]'"):
            _ObjectOps(MemoryStore()).read("catalog.json")

    @pytest.mark.parametrize(
        "route",
        [
            lambda ops, storage, root: ops.read("a"),
            lambda ops, storage, root: ops.put("a", b""),
            lambda ops, storage, root: ops.head("a"),
            lambda ops, storage, root: ops.delete("a"),
            lambda ops, storage, root: storage.read("a"),
            lambda ops, storage, root: storage.create("a", b""),
            lambda ops, storage, root: storage.write_leaf("a", b""),
            lambda ops, storage, root: storage.advance_current(root, b""),
        ],
        ids=["read", "put", "head", "delete", "storage-read", "create", "write-leaf", "advance-current"],
    )
    def test_every_route_names_the_missing_extra(self, monkeypatch: pytest.MonkeyPatch, route) -> None:
        from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, _ObjectOps

        store = MemoryStore()
        storage = BlobInventoryStorage(BlobLocation("az://c/p/", store), actor="t")
        monkeypatch.setitem(sys.modules, "obstore", None)
        monkeypatch.setitem(sys.modules, "obstore.exceptions", None)

        with pytest.raises(ImportError, match=r"uv add 'statcatlib\[blob\]'.*pip install 'statcatlib\[blob\]'"):
            route(_ObjectOps(store), storage, SimpleNamespace(seq=1, restore_epoch=0))

    @pytest.mark.parametrize(
        "route",
        [
            lambda storage, root: storage.create("a", b""),
            lambda storage, root: storage._move_current(root, b""),
        ],
        ids=["create", "move-current"],
    )
    def test_a_route_with_except_clauses_loads_the_extra_itself(self, monkeypatch: pytest.MonkeyPatch, route) -> None:
        """`create` and `_move_current` name `obstore.exceptions` in `except` clauses, past an injected `_ObjectOps`."""
        from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, _ObjectOps

        class Refuses(_ObjectOps):
            """Reaches no store and raises, so only the storage's own load can name the extra."""

            @override
            def read(self, path: str) -> tuple[bytes, str | None] | None:
                return None

            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                raise OSError(path)

        store = MemoryStore()
        storage = BlobInventoryStorage(BlobLocation("az://c/p/", store, ops=Refuses(store)), actor="t")
        monkeypatch.setitem(sys.modules, "obstore", None)
        monkeypatch.setitem(sys.modules, "obstore.exceptions", None)

        with pytest.raises(ImportError, match=r"statcatlib\[blob\]"):
            route(storage, SimpleNamespace(seq=1, restore_epoch=0))

    def test_errors(self) -> None:
        import statcatlib as sc

        names = (
            "InventoryConflictError",
            "InventoryContentionError",
            "InventoryRuleError",
            "InventoryWriteRefusedError",
            "NonFastForwardError",
            "CrossProductRuleError",
            "WorkingCopyExistsError",
            "WorkingCopyClosedError",
            "SealCheckError",
            "EditionCheckError",
            "JournalError",
            "NoLiveEditionError",
        )
        for name in names:
            assert name in sc.__all__, name
            assert issubclass(getattr(sc, name), Exception)
        assert issubclass(sc.NonFastForwardError, sc.InventoryRuleError)
        assert issubclass(sc.CrossProductRuleError, sc.InventoryRuleError)

    def test_query_builder(self) -> None:
        import statcatlib as sc

        assert sc.QueryBuilder is not None

    def test_search_result(self) -> None:
        import statcatlib as sc

        assert sc.SearchResult is not None


# ---------------------------------------------------------------------------
# Source function exports
# ---------------------------------------------------------------------------


class TestSourceExports:
    def test_ingest_sdmx(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_sdmx)

    def test_ingest_sdmx_registry(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_sdmx_registry)

    def test_dataset_from_dataflow(self) -> None:
        import statcatlib as sc

        assert callable(sc.dataset_from_dataflow)

    def test_ingest_ckan(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_ckan)

    def test_ingest_data(self) -> None:
        import statcatlib as sc

        assert callable(sc.ingest_data)


# ---------------------------------------------------------------------------
# Constructibility smoke test
# ---------------------------------------------------------------------------


class TestConstruct:
    def test_open_dataset(self) -> None:
        import statcatlib as sc

        ds = sc.OpenDataset(
            identifier="test-1",
            title=sc.InternationalString(en="Test"),
            description=sc.InternationalString(en="Desc"),
        )
        assert ds.identifier == "test-1"
        assert ds.kind == "open"

    def test_aggregate_dataset(self) -> None:
        import statcatlib as sc

        ds = sc.AggregateDataset(
            identifier="test-2",
            title=sc.InternationalString(en="Test"),
            description=sc.InternationalString(en="Desc"),
        )
        assert ds.identifier == "test-2"
        assert ds.kind == "aggregate"

    def test_catalog_store_round_trip(self) -> None:
        import statcatlib as sc

        store = open_scratch()
        ds = sc.OpenDataset(
            identifier="rt-1",
            title=sc.InternationalString(en="Round Trip"),
            description=sc.InternationalString(en="Desc"),
        )
        store.put(ds)
        retrieved = store.get(sc.OpenDataset, "rt-1")
        assert retrieved is not None
        assert retrieved.identifier == "rt-1"

    def test_query_filter_end_to_end(self) -> None:
        from sdmxlib.model.codelist import Code, Codelist
        from sdmxlib.model.dataflow import Dataflow
        from sdmxlib.model.datastructure import DataStructure, Dimension
        from sdmxlib.model.in_memory_registry import InMemoryRegistry
        from sdmxlib.model.istring import InternationalString as IStr

        import statcatlib as sc
        import statcatlib.query as q

        cl = Codelist(
            id="CL_GEO",
            maintainer="TEST",
            name=IStr(en="Geography"),
            codes=[Code(id="CA", name=IStr(en="Canada")), Code(id="US", name=IStr(en="USA"))],
        )
        dsd = DataStructure(
            id="DSD_FILTER",
            maintainer="TEST",
            name=IStr(en="Filter DSD"),
            dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=cl)],
        )
        flow = Dataflow(
            id="DF_FILTER",
            maintainer="TEST",
            name=IStr(en="Filterable"),
            description=IStr(en="Desc"),
            structure=dsd,
        )
        registry = InMemoryRegistry()
        for obj in [cl, dsd, flow]:
            registry.add(obj)

        store = open_scratch()
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.AggregateDataset).filter(q.has_code("GEO", "CA")).execute()
        assert result.total == 1


# ---------------------------------------------------------------------------
# RFC D4, D13, D16: the public objects reach neither the operational object
# nor an embargoed edition
# ---------------------------------------------------------------------------


def _seal_twice(root: Path) -> None:
    """Seal two editions of product ``p``: the first is superseded, the second live."""
    with sc.Catalog.create(root) as cat:
        for title in ("First", "Second"):
            wc = cat.product("p").working_copy()
            wc.put(
                sc.OpenDataset(
                    identifier="d", title=sc.InternationalString(en=title), description=sc.InternationalString(en=title)
                )
            )
            wc.seal()


def _public_values(obj: object) -> list[tuple[str, object]]:
    """Each public attribute of *obj* that reads without raising, with its value."""
    found: list[tuple[str, object]] = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            found.append((name, getattr(obj, name)))
        except AttributeError, NotImplementedError, RuntimeError:
            continue
    return found


class TestNoOperationalReach:
    """RFC D4, D13, D16: no attribute of Catalog, Product or EditionView reaches a CatalogAdmin or a non-live edition.

    `tests/test_catalog_admin.py` runs the same check over a catalog that holds
    embargoed editions. Each public attribute's value, and
    each public method's return annotation, is checked for it; the editions a
    public object lists are checked for any state but ``live``.
    """

    def test_no_attribute_returns_an_operational_object(self, tmp_path: Path) -> None:
        _seal_twice(tmp_path / "cat")
        with sc.Catalog.open(tmp_path / "cat") as cat:
            product = cat.product("p")
            view = product.view()
            for owner in (cat, product, view):
                for name, value in _public_values(owner):
                    where = f"{type(owner).__name__}.{name}"
                    assert "Admin" not in type(value).__name__, where
                    if callable(value) and hasattr(value, "__annotations__"):
                        returns = annotationlib.get_annotations(value, format=annotationlib.Format.STRING)
                        assert "Admin" not in returns.get("return", ""), where

    def test_the_public_catalog_lists_the_live_edition_only(self, tmp_path: Path) -> None:
        _seal_twice(tmp_path / "cat")
        with sc.Catalog.open(tmp_path / "cat") as cat:
            editions = cat.product("p").editions()
            manifest = cat.product("p").view().registry.manifest
            assert manifest is not None
            assert [e.edition_id for e in editions] == [manifest.edition_id]
            assert cat.products() == ["p"]

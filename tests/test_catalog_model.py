"""The public model: Catalog, Product, WorkingCopy and Edition (RFC D2, D3, D15, D19; design P4).

`Catalog.create` and `Catalog.open` replace ``Catalog(path)``; a product's
`WorkingCopy` is its one unsealed edition, and a seal records the edition in
the Inventory, live with embargo off. `Catalog.seal` seals several products
and records them in one Inventory version (design item 27).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sdmxlib.edition
from sdmxlib.edition import Finding, ManifestError
from sdmxlib.inventory import FullInventory, local_fetch, rollback
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
from statcatlib.store.inventory import Change, InventoryWriter
from statcatlib.store.store import IncompatibleCatalogError

pytestmark = pytest.mark.unit


def _dataset(identifier: str, title: str = "T") -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=sc.InternationalString(en=title), description=sc.InternationalString(en=title)
    )


def _seal(cat: sc.Catalog, product: str, *identifiers: str, lifecycle: str = "revisable") -> sc.Edition:
    wc = cat.product(product).working_copy(lifecycle="final" if lifecycle == "final" else "revisable")
    wc.put([_dataset(i) for i in identifiers])
    return wc.seal()


def _full(root: Path) -> FullInventory:
    return FullInventory(local_fetch(root)).refresh()


class TestOpen:
    def test_two_products_are_listed(self, tmp_path: Path) -> None:
        """RFC D2."""
        with sc.Catalog.create(tmp_path / "cat") as cat:
            _seal(cat, "a", "d1")
            _seal(cat, "b", "d2")
        with sc.Catalog.open(tmp_path / "cat") as cat:
            assert cat.products() == ["a", "b"]
            view = cat.product("a").view()
            assert isinstance(view, sc.EditionView)
            assert view.get(sc.OpenDataset, "d1") is not None
            assert cat.product("b").view().get(sc.OpenDataset, "d2") is not None

    def test_a_catalog_with_no_seal_has_no_products(self, tmp_path: Path) -> None:
        sc.Catalog.create(tmp_path / "cat").close()
        with sc.Catalog.open(tmp_path / "cat") as cat:
            assert cat.products() == []
            with pytest.raises(sc.NoLiveEditionError, match=r"no live edition.*CatalogAdmin\.editions"):
                cat.product("a").view()
            assert cat.product("a").editions() == []

    def test_a_product_id_names_a_directory(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat") as cat:
            for bad in ("", "../x", "a/b", ".hidden"):
                with pytest.raises(ValueError, match="not a product id"):
                    cat.product(bad)


class TestSeal:
    def test_the_first_seal_writes_version_1_with_the_edition_live(self, tmp_path: Path) -> None:
        """RFC D3: `create` writes no Inventory version; the first seal writes version 1."""
        with sc.Catalog.create(tmp_path / "cat") as cat:
            assert not (tmp_path / "cat" / "inventory").exists()
            edition = _seal(cat, "p", "d1")
        full = _full(tmp_path / "cat")
        assert full.seq == 1
        assert full.root.embargo is False
        (entry,) = full.entries("p")
        assert (entry.edition_id, entry.state, entry.previous) == (edition.edition_id, "live", None)
        assert entry.manifest_sha256 == edition.manifest_sha256
        assert full.leaf("p").kind == "data"

    def test_a_second_seal_supersedes_the_first(self, tmp_path: Path) -> None:
        """RFC D3: the edition built on the live one replaces it in one version."""
        with sc.Catalog.create(tmp_path / "cat") as cat:
            first = _seal(cat, "p", "d1")
            second = _seal(cat, "p", "d1", "d2")
        assert second.previous == first.edition_id
        full = _full(tmp_path / "cat")
        assert full.seq == 2
        assert {e.edition_id: e.state for e in full.entries("p")} == {
            first.edition_id: "superseded",
            second.edition_id: "live",
        }

    def test_an_open_catalog_sees_the_new_edition_after_refresh(self, tmp_path: Path) -> None:
        """RFC D3."""
        with sc.Catalog.create(tmp_path / "cat") as writer:
            _seal(writer, "p", "d1")
            with sc.Catalog.open(tmp_path / "cat") as reader:
                old = reader.product("p").view()
                second = _seal(writer, "p", "d1", "d2")
                assert reader.product("p").editions()[0].edition_id != second.edition_id
                reader.refresh()
                assert reader.product("p").editions()[0].edition_id == second.edition_id
                assert reader.product("p").view().get(sc.OpenDataset, "d2") is not None
                # A view stays on its edition.
                assert old.get(sc.OpenDataset, "d2") is None

    def test_editions_reads_previous_from_the_manifest(self, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "cat") as cat:
            first = _seal(cat, "p", "d1")
            second = _seal(cat, "p", "d1")
            cat.refresh()
            assert cat.product("p").editions() == [second]
            assert second.previous == first.edition_id

    def test_a_working_copy_reads_its_own_writes(self, wc: sc.WorkingCopy) -> None:
        view = wc.view()
        wc.put(_dataset("d1"))
        assert view.get(sc.OpenDataset, "d1") is not None


class TestWorkingCopyLifecycle:
    def test_calls_after_seal_raise(self, catalog: sc.Catalog) -> None:
        """RFC D15."""
        wc = catalog.product("p").working_copy()
        view = wc.view()
        wc.seal()
        for call in (
            lambda: wc.put(_dataset("x")),
            wc.view,
            lambda: wc.registry,
            wc.seal,
            wc.discard,
            lambda: view.query(sc.BaseDataset).count(),
            lambda: view.index_layout,
        ):
            with pytest.raises(sc.WorkingCopyClosedError, match="sealed"):
                call()

    def test_calls_after_discard_raise(self, catalog: sc.Catalog, tmp_path: Path) -> None:
        """RFC D15: discard removes the unsealed edition."""
        wc = catalog.product("p").working_copy()
        edition_dir = tmp_path / "catalog" / "products" / "p" / wc.edition_id
        assert edition_dir.is_dir()
        view = wc.view()
        wc.discard()
        assert not edition_dir.exists()
        with pytest.raises(sc.WorkingCopyClosedError, match="discarded"):
            wc.put(_dataset("x"))
        with pytest.raises(sc.WorkingCopyClosedError, match="discarded"):
            view.get(sc.OpenDataset, "x")
        catalog.product("p").working_copy().close()

    def test_a_second_working_copy_is_refused(self, catalog: sc.Catalog) -> None:
        """RFC D15: the error names the unsealed edition, its start, resume and discard."""
        wc = catalog.product("p").working_copy()
        wc.close()
        with pytest.raises(sc.WorkingCopyExistsError, match=rf"{wc.edition_id}.*resume=True.*discard"):
            catalog.product("p").working_copy()

    def test_resume_reopens_the_unsealed_edition(self, catalog: sc.Catalog) -> None:
        wc = catalog.product("p").working_copy()
        wc.put(_dataset("d1"))
        wc.close()
        resumed = catalog.product("p").working_copy(resume=True)
        assert resumed.edition_id == wc.edition_id
        assert resumed.view().get(sc.OpenDataset, "d1") is not None
        edition = resumed.seal()
        assert edition.edition_id == wc.edition_id

    def test_resume_without_an_unsealed_edition_raises(self, catalog: sc.Catalog) -> None:
        with pytest.raises(FileNotFoundError, match="no unsealed edition"):
            catalog.product("p").working_copy(resume=True)

    def test_a_lifecycle_change_is_refused_before_build(self, catalog: sc.Catalog, tmp_path: Path) -> None:
        _seal(catalog, "p", "d1")
        before = sorted(p.name for p in (tmp_path / "catalog" / "products" / "p").iterdir())
        with pytest.raises(sc.InventoryRuleError, match="revisable product") as caught:
            catalog.product("p").working_copy(lifecycle="final")
        assert caught.value.rule == "lifecycle"
        assert caught.value.product == "p"
        assert sorted(p.name for p in (tmp_path / "catalog" / "products" / "p").iterdir()) == before

    def test_a_final_product_is_recorded_final(self, catalog: sc.Catalog, tmp_path: Path) -> None:
        _seal(catalog, "p", "d1", lifecycle="final")
        assert _full(tmp_path / "catalog").leaf("p").lifecycle == "final"


class TestReadOnlyAndCreate:
    def test_open_is_read_only(self, tmp_path: Path) -> None:
        """P4: only a catalog from `create` writes."""
        sc.Catalog.create(tmp_path / "cat").close()
        with sc.Catalog.open(tmp_path / "cat") as cat:
            assert not cat.writable
            with pytest.raises(RuntimeError, match="read-only"):
                cat.product("p").working_copy()

    def test_create_exist_ok_reopens_for_writing(self, tmp_path: Path) -> None:
        """P4."""
        with sc.Catalog.create(tmp_path / "cat") as cat:
            _seal(cat, "p", "d1")
        with sc.Catalog.create(tmp_path / "cat", exist_ok=True) as cat:
            assert cat.writable
            assert cat.products() == ["p"]
            _seal(cat, "p", "d2")

    def test_create_refuses_a_filesystem_without_hard_links(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_links(src: object, dst: object) -> None:
            del src, dst
            raise OSError(45, "Operation not supported")

        monkeypatch.setattr(os, "link", no_links)
        with pytest.raises(IncompatibleCatalogError, match="does not support hard links"):
            sc.Catalog.create(tmp_path / "cat")
        assert not (tmp_path / "cat" / "catalog.json").exists()
        assert list((tmp_path / "cat").iterdir()) == []


class TestMovedCalls:
    @pytest.mark.parametrize("uri", [":memory:", "some/folder"])
    def test_catalog_constructor_names_the_temporary_catalog_helper(self, uri: str) -> None:
        """RFC D19."""
        with pytest.raises(TypeError, match=r"statcatlib\.testing\.temporary_catalog"):
            sc.Catalog(uri)

    @pytest.mark.parametrize("name", ["query", "get", "codes", "datasets", "code_map", "themes"])
    def test_a_moved_method_names_its_replacement(self, catalog: sc.Catalog, name: str) -> None:
        with pytest.raises(AttributeError, match=rf"product\(id\)\.view\(\)\.{name}"):
            getattr(catalog, name)

    def test_an_unknown_attribute_is_a_plain_attribute_error(self, catalog: sc.Catalog) -> None:
        with pytest.raises(AttributeError, match="has no attribute 'nonsense'"):
            _ = catalog.nonsense

    def test_search_and_facets_across_products_are_not_supported(self, catalog: sc.Catalog) -> None:
        with pytest.raises(NotImplementedError, match="across products"):
            catalog.search("x")
        with pytest.raises(NotImplementedError, match="across products"):
            catalog.facets("publisher")

    def test_product_rollback_names_catalog_admin(self, catalog: sc.Catalog) -> None:
        with pytest.raises(AttributeError, match=r"CatalogAdmin\.open\(path\)\.rollback"):
            catalog.product("p").rollback()

    @pytest.mark.parametrize("cls", [sc.Product, sc.EditionView, sc.WorkingCopy])
    def test_the_model_classes_are_not_constructed_directly(self, cls: type) -> None:
        with pytest.raises(TypeError):
            cls()


def _no_bad_datasets(view: sc.EditionView) -> Iterator[Finding]:
    """A seal check that refuses any dataset whose identifier starts with ``bad``."""
    for ds in view.query(sc.OpenDataset).collect():
        if ds.identifier.startswith("bad"):
            yield Finding("test", ds.identifier, "a bad dataset")


def _working_copy(cat: sc.Catalog, product: str, *identifiers: str) -> sc.WorkingCopy:
    wc = cat.product(product).working_copy()
    wc.put([_dataset(i) for i in identifiers])
    return wc


def _journal(root: Path, product: str) -> list[str]:
    folder = root / "products" / product / "journal"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class Killed(BaseException):
    """The process died here."""


class TestBatchSeal:
    def test_a_batch_records_every_product_in_one_version(self, tmp_path: Path) -> None:
        """Design G4b: N products, N leaves, one root."""
        root = tmp_path / "cat"
        with sc.Catalog.create(root) as cat:
            batch = [_working_copy(cat, p, f"{p}1") for p in ("a", "b", "c")]
            outcomes = cat.seal(batch)
        assert outcomes.refused == {}
        assert sorted(outcomes.sealed) == ["a", "b", "c"]
        full = _full(root)
        assert full.seq == 1
        for product, edition in outcomes.sealed.items():
            (entry,) = full.entries(product)
            assert (entry.edition_id, entry.state) == (edition.edition_id, "live")
            assert entry.manifest_sha256 == edition.manifest_sha256
            assert _journal(root, product) == []

    def test_a_refused_product_is_left_out_and_the_rest_recorded(self, tmp_path: Path) -> None:
        """A check finding refuses one product; the others are recorded, and its working copy stays open."""
        root = tmp_path / "cat"
        with sc.Catalog.create(root) as cat:
            good = _working_copy(cat, "a", "a1")
            bad = _working_copy(cat, "b", "bad1")
            outcomes = cat.seal([good, bad], checks=[_no_bad_datasets])
            assert sorted(outcomes.sealed) == ["a"]
            assert isinstance(outcomes.refused["b"], sc.SealCheckError)
            assert bad.view().get(sc.OpenDataset, "bad1") is not None
            bad.put(_dataset("b1"))
            cat.refresh()
            assert cat.products() == ["a"]
            assert cat.seal([bad]).sealed["b"].product_id == "b"
        assert _full(root).seq == 2

    def test_a_non_fast_forward_product_is_refused_before_any_seal(self, tmp_path: Path) -> None:
        root = tmp_path / "cat"
        with sc.Catalog.create(root) as cat:
            _seal(cat, "b", "b1")
            _seal(cat, "b", "b2")
            stale = cat.product("b").working_copy()
            writer = InventoryWriter(root)

            def back(full: FullInventory | None) -> Change:
                assert full is not None
                live = full.entries("b")
                current = next(e.edition_id for e in live if e.state == "live")
                leaf = rollback(full.leaf("b"), schema_version=SCHEMA_VERSION, if_live=current, now=datetime.now(UTC))
                return Change(leaves={"b": leaf})

            writer.commit(back, kind="rollback", reason="test")
            fresh = _working_copy(cat, "a", "a1")
            outcomes = cat.seal([stale, fresh])
            assert isinstance(outcomes.refused["b"], sc.NonFastForwardError)
            assert sorted(outcomes.sealed) == ["a"]
            stale.discard()

    def test_a_product_refused_at_record_is_left_unrecorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A rollback lands between the pre-check and the record: that product's sealed edition stays unrecorded."""
        root = tmp_path / "cat"
        real_seal = sdmxlib.edition.seal
        writer = InventoryWriter(root)

        with sc.Catalog.create(root) as cat:
            first_b = _seal(cat, "b", "b1")
            _seal(cat, "b", "b2")
            a = _working_copy(cat, "a", "a1")
            b = _working_copy(cat, "b", "b3")
            b_edition = b.edition_id

            def roll_back_b(path: Path, **kwargs: Any) -> object:
                manifest = real_seal(path, **kwargs)
                if kwargs.get("product_id") == "b":

                    def back(full: FullInventory | None) -> Change:
                        assert full is not None
                        current = next(e.edition_id for e in full.entries("b") if e.state == "live")
                        leaf = rollback(
                            full.leaf("b"), schema_version=SCHEMA_VERSION, if_live=current, now=datetime.now(UTC)
                        )
                        return Change(leaves={"b": leaf})

                    writer.commit(back, kind="rollback", reason="test")
                return manifest

            monkeypatch.setattr("sdmxlib.edition.seal", roll_back_b)
            with caplog.at_level(logging.WARNING):
                outcomes = cat.seal([a, b])
        assert sorted(outcomes.sealed) == ["a"]
        assert isinstance(outcomes.refused["b"], sc.NonFastForwardError)
        assert "not recorded" in caplog.text
        assert (root / "products" / "b" / b_edition / "manifest.json").is_file()
        assert _journal(root, "b") == []
        full = _full(root)
        assert b_edition not in {e.edition_id for e in full.entries("b")}
        live = next(e for e in full.entries("b") if e.state == "live")
        assert live.edition_id == first_b.edition_id

    def test_a_crash_during_the_batch_records_its_products_across_versions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash note: killed before the batch's commit, each product is recorded by its next working copy."""
        root = tmp_path / "cat"
        with sc.Catalog.create(root) as cat:
            batch = [_working_copy(cat, p, f"{p}1") for p in ("a", "b")]
            editions = [wc.edition_id for wc in batch]

            def killed(*args: object, **kwargs: object) -> object:
                raise Killed

            with monkeypatch.context() as mp:
                mp.setattr(InventoryWriter, "commit", killed)
                with pytest.raises(Killed):
                    cat.seal(batch)
        assert not (root / "inventory").exists()
        assert _journal(root, "a") == [f"{editions[0]}.json"]

        with sc.Catalog.create(root, exist_ok=True) as cat:
            cat.product("a").working_copy().discard()
            assert _full(root).seq == 1
            cat.product("b").working_copy().discard()
        full = _full(root)
        assert full.seq == 2
        assert [e.edition_id for e in full.entries("a")] == [editions[0]]
        assert [e.edition_id for e in full.entries("b")] == [editions[1]]

    def test_a_manifest_refusal_closes_only_that_working_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "cat"
        real_seal = sdmxlib.edition.seal

        def refuse_b(path: Path, **kwargs: Any) -> object:
            if kwargs.get("product_id") == "b":
                msg = "refused for the test"
                raise ManifestError(msg)
            return real_seal(path, **kwargs)

        with sc.Catalog.create(root) as cat:
            batch = [_working_copy(cat, p, f"{p}1") for p in ("a", "b")]
            monkeypatch.setattr("sdmxlib.edition.seal", refuse_b)
            outcomes = cat.seal(batch)
            monkeypatch.undo()
            assert sorted(outcomes.sealed) == ["a"]
            assert isinstance(outcomes.refused["b"], ManifestError)
            with pytest.raises(sc.WorkingCopyClosedError, match="resume=True"):
                batch[1].view()
            assert cat.seal([cat.product("b").working_copy(resume=True)]).sealed["b"].product_id == "b"

    def test_an_empty_batch_writes_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "cat"
        with sc.Catalog.create(root) as cat:
            assert cat.seal([]) == sc.SealOutcomes(sealed={}, refused={})
        assert not (root / "inventory").exists()

    def test_two_working_copies_of_one_product_are_refused(self, catalog: sc.Catalog) -> None:
        wc = catalog.product("p").working_copy()
        with pytest.raises(ValueError, match="two working copies of p"):
            catalog.seal([wc, wc])
        wc.discard()

    def test_a_working_copy_of_another_catalog_is_refused(self, catalog: sc.Catalog, tmp_path: Path) -> None:
        with sc.Catalog.create(tmp_path / "other") as other:
            wc = other.product("p").working_copy()
            with pytest.raises(ValueError, match="belongs to the catalog"):
                catalog.seal([wc])
            wc.discard()

    def test_a_closed_working_copy_is_refused(self, catalog: sc.Catalog) -> None:
        wc = catalog.product("p").working_copy()
        wc.close()
        with pytest.raises(sc.WorkingCopyClosedError):
            catalog.seal([wc])

    def test_a_read_only_catalog_cannot_seal(self, tmp_path: Path) -> None:
        sc.Catalog.create(tmp_path / "cat").close()
        with sc.Catalog.open(tmp_path / "cat") as cat, pytest.raises(RuntimeError, match="read-only"):
            cat.seal([])

"""`EmbargoReader` over a local catalog (Phase 5 D12, Part 5).

A local catalog with embargo on records each seal ``embargoed``, so the reader's
views are built from sealed editions on disk, with no store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.embargo import EmbargoReader
from statcatlib.store.inventory import InventoryRuleError

pytestmark = pytest.mark.integration


def _seal(root: Path, identifier: str, product: str = "p") -> sc.Edition:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(product).working_copy()
        wc.put(
            [
                sc.OpenDataset(
                    identifier=identifier,
                    title=sc.InternationalString(en=identifier),
                    description=sc.InternationalString(en=identifier),
                )
            ]
        )
        return wc.seal()


def _tree(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "cat"


@pytest.fixture
def embargoed(root: Path) -> tuple[CatalogAdmin, sc.Edition, sc.Edition]:
    """Product p: A live, and B built on it and sealed under embargo."""
    a = _seal(root, "a")
    admin = CatalogAdmin.open(root)
    admin.set_embargo(on=True, reason="embargo the next release")
    b = _seal(root, "b")
    return admin, a, b


class TestALocalCatalog:
    def test_an_embargoed_edition_opens_through_it(self, root, embargoed) -> None:
        _admin, a, b = embargoed
        reader = EmbargoReader.open(root)
        try:
            listed = reader.editions()
            assert [(e.product_id, e.edition_id, e.state) for e in listed] == [("p", b.edition_id, "embargoed")]
            assert listed[0].previous == a.edition_id
            # B is built on A, so it holds A's dataset and its own.
            held = reader.view("p", b.edition_id)
            assert {d.identifier for d in held.query(sc.OpenDataset).collect()} == {"a", "b"}
            live = reader.view("p", a.edition_id)
            assert {d.identifier for d in live.query(sc.OpenDataset).collect()} == {"a"}
        finally:
            reader.close()

        with sc.Catalog.open(root) as cat:
            assert {d.identifier for d in cat.product("p").view().query(sc.OpenDataset).collect()} == {"a"}

    def test_it_writes_nothing_to_the_catalog(self, root, embargoed) -> None:
        _admin, _a, b = embargoed
        before = _tree(root)
        reader = EmbargoReader.open(root)
        reader.view("p", b.edition_id).query(sc.OpenDataset).collect()
        reader.close()
        assert _tree(root) == before

    def test_editions_filters_by_state(self, root, embargoed) -> None:
        admin, a, b = embargoed
        reader = EmbargoReader.open(root)
        try:
            assert [e.edition_id for e in reader.editions(states=("live",))] == [a.edition_id]
            assert [e.edition_id for e in reader.editions(states=("embargoed", "live"))] == [
                a.edition_id,
                b.edition_id,
            ]
            admin.cancel("p", b.edition_id, reason="withdraw before release")
            assert reader.editions() != []
            reader.refresh()
            assert reader.editions() == []
            assert [e.edition_id for e in reader.editions(states=["withdrawn"])] == [b.edition_id]
        finally:
            reader.close()

    @pytest.mark.parametrize("states", ["embargoed", ("embargoed", "retired")], ids=["a-string", "unknown-state"])
    def test_an_unknown_state_is_refused(self, root, embargoed, states) -> None:
        del embargoed
        reader = EmbargoReader.open(root)
        try:
            with pytest.raises(ValueError, match="states takes a collection"):
                reader.editions(states=states)
        finally:
            reader.close()


class TestRefusals:
    def test_a_withdrawn_or_superseded_edition_raises(self, root, embargoed) -> None:
        admin, a, b = embargoed
        admin.release("p", b.edition_id, reason="release now")
        c = _seal(root, "c")
        admin.cancel("p", c.edition_id, reason="withdraw before release")
        reader = EmbargoReader.open(root)
        try:
            with pytest.raises(ValueError, match="is superseded"):
                reader.view("p", a.edition_id)
            with pytest.raises(ValueError, match="is withdrawn"):
                reader.view("p", c.edition_id)
        finally:
            reader.close()

    def test_an_unknown_product_or_edition_raises_the_unknown_rule(self, root, embargoed) -> None:
        _admin, a, _b = embargoed
        reader = EmbargoReader.open(root)
        try:
            with pytest.raises(InventoryRuleError, match="no product q") as product:
                reader.view("q", a.edition_id)
            assert product.value.rule == "unknown"
            with pytest.raises(InventoryRuleError, match="no edition") as edition:
                reader.view("p", "0190a000-0000-7000-8000-000000000000")
            assert (edition.value.rule, edition.value.edition) == ("unknown", "0190a000-0000-7000-8000-000000000000")
        finally:
            reader.close()

    def test_a_catalog_with_no_version_lists_nothing(self, root) -> None:
        sc.Catalog.create(root).close()
        reader = EmbargoReader.open(root)
        try:
            assert reader.editions(states=("embargoed", "live", "superseded", "withdrawn")) == []
            with pytest.raises(InventoryRuleError, match="no product p"):
                reader.view("p", "0190a000-0000-7000-8000-000000000000")
        finally:
            reader.close()

    def test_it_is_built_only_by_open(self) -> None:
        with pytest.raises(TypeError, match=r"EmbargoReader\.open"):
            EmbargoReader()

    def test_a_uri_string_names_blob_location(self) -> None:
        with pytest.raises(ValueError, match=r"EmbargoReader\.open\('az://c/p/'\).*BlobLocation"):
            EmbargoReader.open("az://c/p/")

    def test_a_closed_reader_and_its_views_refuse(self, root, embargoed) -> None:
        _admin, _a, b = embargoed
        reader = EmbargoReader.open(root)
        view = reader.view("p", b.edition_id)
        reader.close()
        reader.close()
        with pytest.raises(RuntimeError, match="is closed"):
            reader.editions()
        with pytest.raises(RuntimeError, match="is closed"):
            view.query(sc.OpenDataset).collect()

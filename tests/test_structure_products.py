"""Structure products: the product kind, `fill_structures`, the content check and the publish check (GH-51, S1).

A structure product holds full artefacts only. Its working copy is filled by
selection from a source registry, its seal refuses a dataflow, a binding or a
partial before anything is sealed, and its sealed edition is checked at
``publish`` against the catalog's other structure products before the
Inventory records it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sdmxlib as sl
from sdmxlib.edition import Finding, read_manifest
from sdmxlib.inventory import FullInventory, local_fetch

import statcatlib as sc
from statcatlib import diagnostics
from statcatlib.store import working_copy as working_copy_module
from statcatlib.store.inventory import InventoryRuleError
from tests.conftest import GEO, GEO_DSD, GEO_FLOW, GEO_HIERARCHY, NAICS, structure_source

pytestmark = pytest.mark.integration

GEO_PRODUCT = "structure-geo"
CLASSIFICATIONS = "structure-classifications"
CONCEPTS_PRODUCT = "structure-concepts"


class Killed(BaseException):
    """The process died here."""


@pytest.fixture
def source(tmp_path: Path) -> Iterator[sl.EditionRegistry]:
    """The scratch registry standing for an FMR's structures."""
    reg = structure_source(tmp_path / "source")
    yield reg
    reg.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "catalog"


def _full(root: Path) -> FullInventory:
    return FullInventory(local_fetch(root)).refresh()


def _structure_wc(cat: sc.Catalog, product_id: str, source: sl.EditionRegistry) -> sc.WorkingCopy:
    """A structure working copy filled as design CUJ 1 fills each of the three."""
    wc = cat.product(product_id).working_copy(kind="structure")
    if product_id == GEO_PRODUCT:
        wc.fill_structures(source, families=[sl.Codelist, sl.Hierarchy], agency="T", ids=["CL_GEO", "HCL_*"])
    elif product_id == CLASSIFICATIONS:
        wc.fill_structures(source, artefacts=[NAICS.urn])
    else:
        wc.fill_structures(source, families=[sl.ConceptScheme])
    return wc


def _seal_structure(cat: sc.Catalog, product_id: str, source: sl.EditionRegistry) -> sc.Edition:
    return _structure_wc(cat, product_id, source).seal()


def _editions(root: Path, product_id: str) -> list[str]:
    folder = root / "products" / product_id
    return sorted(p.name for p in folder.iterdir() if p.name not in {"data", "journal"}) if folder.is_dir() else []


def _journal(root: Path, product_id: str) -> list[str]:
    folder = root / "products" / product_id / "journal"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _section_kind(root: Path, edition: sc.Edition) -> object:
    manifest = read_manifest(root / "products" / edition.product_id / edition.edition_id)
    return manifest.extensions["statcat"]["kind"]


class TestFillStructures:
    def test_a_structure_product_seals_with_its_kind_in_the_leaf_and_the_section(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            wc = cat.product(GEO_PRODUCT).working_copy(kind="structure")
            assert wc.kind == "structure"
            by_family = wc.fill_structures(source, families=[sl.Codelist], agency="T", ids=["CL_GEO"])
            by_pattern = wc.fill_structures(source, families=[sl.Hierarchy], ids=["HCL_*"])
            by_urn = wc.fill_structures(source, artefacts=[str(NAICS.urn)])
            edition = wc.seal()

        assert by_family == (str(GEO.urn),)
        assert by_pattern == (str(GEO_HIERARCHY.urn),)
        assert by_urn == (str(NAICS.urn),)
        manifest = read_manifest(root / "products" / GEO_PRODUCT / edition.edition_id)
        assert sorted(a.urn for a in manifest.artefacts) == sorted(map(str, (GEO.urn, GEO_HIERARCHY.urn, NAICS.urn)))
        assert manifest.bindings == ()
        assert _section_kind(root, edition) == "structure"
        assert _full(root).leaf(GEO_PRODUCT).kind == "structure"

    def test_a_later_working_copy_takes_the_recorded_kind(self, root: Path, source: sl.EditionRegistry) -> None:
        with sc.Catalog.create(root) as cat:
            _seal_structure(cat, GEO_PRODUCT, source)
            wc = cat.product(GEO_PRODUCT).working_copy()
            assert wc.kind == "structure"
            edition = wc.seal()

        assert _section_kind(root, edition) == "structure"
        assert [e.state for e in _full(root).entries(GEO_PRODUCT)] == ["superseded", "live"]

    def test_a_data_product_is_data_by_default_and_refuses_fill_structures(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            wc = cat.product("table").working_copy()
            assert wc.kind == "data"
            with pytest.raises(TypeError, match=r"working_copy\(kind='structure'\)"):
                wc.fill_structures(source, families=[sl.Codelist])
            edition = wc.seal()

        assert _section_kind(root, edition) == "data"
        assert _full(root).leaf("table").kind == "data"


class TestKindRefusals:
    @pytest.mark.parametrize(("recorded", "asked"), [("data", "structure"), ("structure", "data")])
    def test_a_product_reopened_with_the_other_kind_is_refused_before_the_build(
        self, root: Path, source: sl.EditionRegistry, recorded: str, asked: Any
    ) -> None:
        with sc.Catalog.create(root) as cat:
            if recorded == "structure":
                wc = cat.product("p").working_copy(kind="structure")
                wc.fill_structures(source, artefacts=[GEO.urn])
                wc.seal()
            else:
                cat.product("p").working_copy().seal()
            before = _editions(root, "p")

            with pytest.raises(InventoryRuleError, match="a product keeps the kind") as caught:
                cat.product("p").working_copy(kind=asked)

        assert caught.value.rule == "kind"
        assert caught.value.product == "p"
        assert _editions(root, "p") == before, "nothing is built"

    def test_a_resumed_structure_working_copy_opened_as_data_is_refused(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            product = cat.product(GEO_PRODUCT)
            wc = product.working_copy(kind="structure")
            wc.fill_structures(source, artefacts=[GEO.urn])
            wc.close()

            with pytest.raises(InventoryRuleError, match="was started as a structure product") as caught:
                product.working_copy(kind="data", resume=True)
            assert caught.value.rule == "kind"
            assert caught.value.edition == wc.edition_id

            resumed = product.working_copy(resume=True)
            assert resumed.kind == "structure"
            edition = resumed.seal()

        assert _section_kind(root, edition) == "structure"


def _with_dataflow(wc: sc.WorkingCopy) -> None:
    wc.registry.add_all([GEO_DSD, GEO_FLOW])


def _with_binding(wc: sc.WorkingCopy) -> None:
    wc.registry.add(GEO_DSD)
    wc.registry.add(
        sl.DataflowBinding.from_dsd(
            GEO_DSD, dataflow=sl.Ref[sl.Dataflow].unresolved(GEO_FLOW.urn), schema="t", table="geo"
        )
    )


def _with_partial(wc: sc.WorkingCopy) -> None:
    wc.registry.add(
        sl.Codelist(
            id="CL_PART",
            maintainer="T",
            version="1.0",
            name={"en": "part"},
            is_partial=True,
            codes=[sl.Code(id="A", name={"en": "A"})],
        )
    )


class TestStructureContent:
    @pytest.mark.parametrize(
        ("add", "held"),
        [
            (_with_dataflow, "a Dataflow"),
            (_with_binding, "a DataflowBinding"),
            (_with_partial, "a partial Codelist"),
        ],
    )
    def test_a_structure_working_copy_holding_what_it_may_not_stays_open(
        self, root: Path, source: sl.EditionRegistry, add: Callable[[sc.WorkingCopy], None], held: str
    ) -> None:
        with sc.Catalog.create(root) as cat:
            wc = cat.product(GEO_PRODUCT).working_copy(kind="structure")
            wc.fill_structures(source, artefacts=[GEO.urn])
            add(wc)

            with pytest.raises(sc.SealCheckError, match=f"{held}, which a structure product does not hold"):
                wc.seal()

            assert wc.view().registry.get(GEO.urn) is not None, "the working copy stays open"
            assert not (root / "products" / GEO_PRODUCT / wc.edition_id / "manifest.json").exists()
            wc.discard()


class TestPublishCheck:
    def test_a_reference_into_another_live_structure_product_resolves(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            _seal_structure(cat, GEO_PRODUCT, source)
            _seal_structure(cat, CLASSIFICATIONS, source)
            edition = _seal_structure(cat, CONCEPTS_PRODUCT, source)

        assert [e.edition_id for e in _full(root).entries(CONCEPTS_PRODUCT)] == [edition.edition_id]

    def test_a_reference_no_structure_product_holds_leaves_the_edition_unrecorded(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            _seal_structure(cat, GEO_PRODUCT, source)
            wc = _structure_wc(cat, CONCEPTS_PRODUCT, source)

            with pytest.raises(sc.EditionCheckError, match="references") as caught:
                wc.seal()

        assert caught.value.product == CONCEPTS_PRODUCT
        assert caught.value.edition == wc.edition_id
        assert any(f.check == "references" and str(NAICS.urn) in str(f) for f in caught.value.findings)
        assert CONCEPTS_PRODUCT not in _full(root).products()
        assert _journal(root, CONCEPTS_PRODUCT) == []
        (item,) = diagnostics.doctor(root).about("unrecorded edition")
        assert wc.edition_id in item.detail

    def test_the_check_is_given_each_other_structure_products_live_edition(
        self, root: Path, source: sl.EditionRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        given: list[object] = []
        real = working_copy_module.check_edition

        def recording(path: Path, **kwargs: Any) -> Any:
            given.append(kwargs.get("structures"))
            return real(path, **kwargs)

        monkeypatch.setattr(working_copy_module, "check_edition", recording)
        with sc.Catalog.create(root) as cat:
            first = _seal_structure(cat, GEO_PRODUCT, source)
            cat.product("table").working_copy().seal()
            second = _seal_structure(cat, CLASSIFICATIONS, source)

        assert given == [None, [root / "products" / GEO_PRODUCT / first.edition_id]], (
            "a data product with no partial is not checked; an empty list is passed as None"
        )
        assert second.product_id == CLASSIFICATIONS


def _naics_check(view: sc.EditionView) -> list[Finding]:
    """A seal check that finds something in the edition holding CL_NAICS."""
    rows = view._cursor().execute("SELECT urn FROM sdmx.artefact WHERE urn = $urn", {"urn": str(NAICS.urn)}).fetchall()
    return [Finding("test", urn, "refused by the test") for (urn,) in rows]


class TestBatch:
    def test_three_structure_products_are_recorded_in_one_version(self, root: Path, source: sl.EditionRegistry) -> None:
        with sc.Catalog.create(root) as cat:
            batch = [_structure_wc(cat, p, source) for p in (CONCEPTS_PRODUCT, GEO_PRODUCT, CLASSIFICATIONS)]
            outcomes = cat.seal(batch)

        assert outcomes.refused == {}
        assert sorted(outcomes.sealed) == sorted([CONCEPTS_PRODUCT, GEO_PRODUCT, CLASSIFICATIONS])
        full = _full(root)
        assert full.seq == 1
        assert {p: full.leaf(p).kind for p in full.products()} == dict.fromkeys(outcomes.sealed, "structure")

    def test_a_refused_structure_product_drops_the_one_whose_references_it_held(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            batch = [_structure_wc(cat, p, source) for p in (CONCEPTS_PRODUCT, GEO_PRODUCT, CLASSIFICATIONS)]
            outcomes = cat.seal(batch, checks=[_naics_check])

        assert list(outcomes.sealed) == [GEO_PRODUCT]
        assert isinstance(outcomes.refused[CLASSIFICATIONS], sc.SealCheckError)
        concepts = outcomes.refused[CONCEPTS_PRODUCT]
        assert isinstance(concepts, sc.EditionCheckError)
        assert str(NAICS.urn) in str(concepts)
        assert _full(root).products() == [GEO_PRODUCT]

    def test_a_structure_edition_refused_at_record_drops_those_given_it(
        self, root: Path, source: sl.EditionRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with sc.Catalog.create(root) as cat:
            batch = [_structure_wc(cat, p, source) for p in (CONCEPTS_PRODUCT, GEO_PRODUCT, CLASSIFICATIONS)]
            geo = batch[1]
            refusal = InventoryRuleError("refused by the test", rule="state", product=GEO_PRODUCT, edition=None)

            def refuse(*_args: object) -> Any:
                raise refusal

            monkeypatch.setattr(geo, "_next_leaf", refuse)
            outcomes = cat.seal(batch)

        assert outcomes.sealed == {}
        assert outcomes.refused[GEO_PRODUCT] is refusal
        for product_id in (CONCEPTS_PRODUCT, CLASSIFICATIONS):
            dropped = outcomes.refused[product_id]
            assert isinstance(dropped, sc.EditionCheckError)
            assert f"{GEO_PRODUCT}/{geo.edition_id}" in str(dropped)
        assert not (root / "CURRENT").exists(), "no version is written"

    def test_a_data_product_seals_after_the_structure_products_of_its_batch(
        self, root: Path, source: sl.EditionRegistry
    ) -> None:
        with sc.Catalog.create(root) as cat:
            table = cat.product("table").working_copy()
            batch = [table, *(_structure_wc(cat, p, source) for p in (GEO_PRODUCT, CLASSIFICATIONS))]
            outcomes = cat.seal(batch)

        assert list(outcomes.sealed) == [GEO_PRODUCT, CLASSIFICATIONS, "table"]
        assert _full(root).leaf("table").kind == "data"


class TestJournalRepair:
    def _crash_before_the_check(self, root: Path, source: sl.EditionRegistry, monkeypatch: pytest.MonkeyPatch) -> str:
        """Seal structure-concepts and kill the writer after sdmxlib's seal, before the publish check."""

        def killed(*_args: object, **_kwargs: object) -> None:
            raise Killed

        with monkeypatch.context() as mp, sc.Catalog.create(root, exist_ok=True) as cat:
            mp.setattr(working_copy_module, "publish_check", killed)
            wc = _structure_wc(cat, CONCEPTS_PRODUCT, source)
            with pytest.raises(Killed):
                wc.seal()
        assert _journal(root, CONCEPTS_PRODUCT) == [f"{wc.edition_id}.json"]
        return wc.edition_id

    def test_the_next_working_copy_checks_and_records_it_as_a_structure_edition(
        self, root: Path, source: sl.EditionRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with sc.Catalog.create(root) as cat:
            _seal_structure(cat, GEO_PRODUCT, source)
            _seal_structure(cat, CLASSIFICATIONS, source)
        edition_id = self._crash_before_the_check(root, source, monkeypatch)
        checked: list[Path] = []
        real = working_copy_module.publish_check

        def recording(edition_dir: Path, **kwargs: Any) -> None:
            checked.append(edition_dir)
            real(edition_dir, **kwargs)

        monkeypatch.setattr(working_copy_module, "publish_check", recording)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product(CONCEPTS_PRODUCT).working_copy()
            assert wc.kind == "structure"
            assert wc.previous == edition_id
            wc.discard()

        assert checked == [root / "products" / CONCEPTS_PRODUCT / edition_id]
        leaf = _full(root).leaf(CONCEPTS_PRODUCT)
        assert leaf.kind == "structure"
        assert [e.edition_id for e in leaf.editions] == [edition_id]
        assert _journal(root, CONCEPTS_PRODUCT) == []

    def test_an_unresolved_reference_clears_the_entry_with_a_warning(
        self, root: Path, source: sl.EditionRegistry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with sc.Catalog.create(root) as cat:
            _seal_structure(cat, GEO_PRODUCT, source)
        edition_id = self._crash_before_the_check(root, source, monkeypatch)

        with caplog.at_level(logging.WARNING), sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product(CONCEPTS_PRODUCT).working_copy(kind="structure")
            assert wc.previous is None
            wc.discard()

        assert f"the journalled edition {edition_id} cannot be recorded" in caplog.text
        assert "publish check" in caplog.text
        assert CONCEPTS_PRODUCT not in _full(root).products()
        assert _journal(root, CONCEPTS_PRODUCT) == []

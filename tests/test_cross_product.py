"""The cross-product rules through seals, releases and rollbacks (GH-51, S3; specification §6.5 items 4, 7, 8).

Every catalog here is generated with the CODR generator: three structure
products and data products cut from them. A test edits a structure (a code's
name in ``CL_GEO``, the name of ``CS_CODR``) to make two copies of one URN,
and checks where the Inventory writer, the seal's pre-check and
`CatalogAdmin` refuse them, and what stays stale.
`tests/test_cross_product_rules.py` checks the rules as pure functions.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Generator, Sequence
from pathlib import Path

import attrs
import pytest
import sdmxlib as sl
from sdmxlib.edition import read_manifest
from sdmxlib.inventory import FullInventory, local_fetch

import statcatlib as sc
from statcatlib import catalog as catalog_module
from statcatlib import diagnostics, testing
from statcatlib.admin import CatalogAdmin
from statcatlib.store import cross_product
from statcatlib.store.cross_product import Refusal, manifest_records
from statcatlib.testing import STRUCTURE_PRODUCTS, CodrShape, codr_product, codr_structures

pytestmark = pytest.mark.integration

#: Small enough to seal in about a second, large enough that every codelist is cut.
SHAPE = CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20), observations=200)
SEED = 3
TABLE = "table-a"
GEO, CLASSIFICATIONS, CONCEPTS = STRUCTURE_PRODUCTS
GEO_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_GEO(1.0)"
UNIT_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_UNIT(1.0)"
CLASS_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_CLASS_1(1.0)"
CS_URN = "urn:sdmx:org.sdmx.infomodel.conceptscheme.ConceptScheme=CODR:CS_CODR(1.0)"
DSD_URN = "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=CODR:DSD_shared_1(1.0)"
REASON = "release the edit"


class Killed(BaseException):
    """The process died here."""


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A catalog holding the three CODR structure products and `TABLE`, all live, sealed once for the module."""
    root = tmp_path_factory.mktemp("codr") / "catalog"
    with sc.Catalog.create(root) as cat:
        codr_product(cat, TABLE, seed=SEED, shape=SHAPE)
    return root


@pytest.fixture
def root(template: Path, tmp_path: Path) -> Path:
    """A copy of `template` for one test."""
    copy = tmp_path / "catalog"
    shutil.copytree(template, copy)
    return copy


def _full(root: Path) -> FullInventory:
    return FullInventory(local_fetch(root)).refresh()


def _states(root: Path, product_id: str) -> dict[str, str]:
    return {e.edition_id: e.state for e in _full(root).entries(product_id)}


def _live(root: Path, product_id: str) -> str:
    (edition_id,) = [e for e, state in _states(root, product_id).items() if state == "live"]
    return edition_id


def _current(root: Path) -> bytes:
    return (root / "CURRENT").read_bytes()


def _edited(*, geo: bool = False, concepts: bool = False) -> testing._Structures:
    """The CODR structures of `SEED`, with a code added to ``CL_GEO`` and ``CS_CODR`` renamed, as asked.

    The edit adds a code, so every partial cut from the old ``CL_GEO`` stays
    a subset of the new one.
    """
    structures = testing._structures(SEED, SHAPE)
    if geo:
        added = sl.Code(id="GXTRA", name={"en": "added", "fr": "ajouté"})
        geography = attrs.evolve(structures.geography, codes=[*structures.geography.codes, added])
        structures = attrs.evolve(structures, geography=geography)
    if concepts:
        structures = attrs.evolve(
            structures, concepts=attrs.evolve(structures.concepts, name={"en": "edited", "fr": "modifié"})
        )
    return structures


def _held(structures: testing._Structures) -> list[sl.MaintainableArtefact]:
    return [a for artefacts in structures.by_product().values() for a in artefacts]


def _structure_wc(
    cat: sc.Catalog,
    product_id: str,
    structures: testing._Structures,
    extra: Sequence[sl.MaintainableArtefact] = (),
) -> sc.WorkingCopy:
    """A working copy of structure product *product_id* holding its artefacts in *structures*, and *extra*."""
    wc = cat.product(product_id).working_copy(kind="structure")
    with testing._scratch(_held(structures)) as scratch:
        urns = [a.urn for a in [*structures.by_product().get(product_id, []), *extra]]
        wc.fill_structures(scratch, artefacts=urns)
    return wc


@contextlib.contextmanager
def _source(
    structures: testing._Structures, table: tuple[sl.DataStructure, sl.Dataflow]
) -> Generator[sl.LocalRegistry]:
    with testing._scratch([*_held(structures), *table]) as scratch:
        yield scratch


def _table_wc(
    cat: sc.Catalog,
    product_id: str,
    structures: testing._Structures,
    *,
    table: tuple[sl.DataStructure, sl.Dataflow] | None = None,
) -> sc.WorkingCopy:
    """A working copy of data product *product_id* with one table cut from *structures*."""
    dsd, flow = table or testing._table(product_id, 1, structures)
    wc = cat.product(product_id).working_copy()
    rows = {str(flow.urn): testing._observations(product_id, 1, SEED, SHAPE, structures)}
    with _source(structures, (dsd, flow)) as scratch:
        sc.ingest_dataflows(wc, scratch, [flow.urn], observations=rows)
    return wc


def _set_embargo(root: Path, *, on: bool) -> None:
    CatalogAdmin.open(root).set_embargo(on=on, reason="test")


class TestItem4:
    def test_a_full_copy_that_differs_from_its_structure_home_is_refused_before_the_seal(self, root: Path) -> None:
        before = _current(root)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, "table-b", _edited(concepts=True))
            with pytest.raises(sc.CrossProductRuleError) as caught:
                wc.seal()
            assert not (root / "products" / "table-b" / wc.edition_id / "manifest.json").exists()
            wc.discard()

        refusal = caught.value
        assert (refusal.rule, refusal.item, refusal.urn) == ("full-artefacts", 4, CS_URN)
        assert refusal.editions == (("table-b", wc.edition_id), (CONCEPTS, _live(root, CONCEPTS)))
        assert _current(root) == before, "no version is written"

    def test_a_structure_edit_is_accepted_and_the_data_edition_stays_live_and_serving(self, root: Path) -> None:
        table = _live(root, TABLE)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            _structure_wc(cat, GEO, _edited(geo=True)).seal()
            cat.refresh()
            assert cat.product(TABLE).view().query(sc.AggregateDataset).count() == 1

        assert _live(root, TABLE) == table
        (stale,) = diagnostics.verify_catalog(root).stale
        assert (stale.product_id, stale.edition_id, stale.state, stale.urn) == (TABLE, table, "live", GEO_URN)
        assert (stale.home, stale.home_live) == (GEO, _live(root, GEO))

    def test_a_cut_codelist_with_no_structure_home_is_refused_before_the_seal(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        structures = _edited()
        with sc.Catalog.create(root) as cat:
            _structure_wc(cat, GEO, structures).seal()
            wc = _table_wc(cat, TABLE, structures)
            with pytest.raises(sc.CrossProductRuleError, match="has no structure home") as caught:
                wc.seal()
            wc.discard()

        assert (caught.value.rule, caught.value.urn) == ("full-artefacts", CLASS_URN)
        assert caught.value.editions == ((TABLE, wc.edition_id),)


class TestNoHome:
    """A DSD that two data products hold, with no structure product holding it (A4)."""

    def _shared(self, root: Path) -> tuple[sl.DataStructure, sl.Dataflow]:
        """Seal products ``shared-a`` and ``shared-b`` with one table, ``DSD_shared_1``; return the table."""
        table = testing._table("shared", 1, _edited())
        with sc.Catalog.create(root, exist_ok=True) as cat:
            outcomes = cat.seal([_table_wc(cat, p, _edited(), table=table) for p in ("shared-a", "shared-b")])
        assert outcomes.refused == {}
        return table

    def _changed(self, table: tuple[sl.DataStructure, sl.Dataflow]) -> tuple[sl.DataStructure, sl.Dataflow]:
        dsd, flow = table
        return attrs.evolve(dsd, name={"en": "changed", "fr": "changé"}), flow

    def test_a_changed_copy_sealed_alone_is_refused(self, root: Path) -> None:
        changed = self._changed(self._shared(root))
        before = _current(root)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, "shared-a", _edited(), table=changed)
            with pytest.raises(sc.CrossProductRuleError, match="no structure home") as caught:
                wc.seal()
            wc.discard()

        assert (caught.value.rule, caught.value.urn) == ("full-artefacts", DSD_URN)
        assert caught.value.editions == (("shared-a", wc.edition_id), ("shared-b", _live(root, "shared-b")))
        assert _current(root) == before

    def test_a_changed_copy_is_recorded_embargoed_and_its_release_alone_is_refused(self, root: Path) -> None:
        changed = self._changed(self._shared(root))
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            edition = _table_wc(cat, "shared-a", _edited(), table=changed).seal()

        assert _states(root, "shared-a")[edition.edition_id] == "embargoed"
        with pytest.raises(sc.CrossProductRuleError) as caught:
            CatalogAdmin.open(root).release("shared-a", edition.edition_id, reason=REASON)
        assert (caught.value.rule, caught.value.urn) == ("full-artefacts", DSD_URN)

    def test_every_holder_sealed_in_one_batch_is_accepted(self, root: Path) -> None:
        changed = self._changed(self._shared(root))
        with sc.Catalog.create(root, exist_ok=True) as cat:
            outcomes = cat.seal([_table_wc(cat, p, _edited(), table=changed) for p in ("shared-a", "shared-b")])

        assert outcomes.refused == {}
        assert sorted(outcomes.sealed) == ["shared-a", "shared-b"]


class TestItem7:
    def test_a_second_home_is_refused_by_the_pre_check_and_the_working_copy_stays_open(self, root: Path) -> None:
        unit = testing._structures(SEED, SHAPE).unit
        before = _current(root)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _structure_wc(cat, GEO, _edited(), extra=[unit])
            with pytest.raises(sc.CrossProductRuleError, match="at most one structure home") as caught:
                wc.seal()
            assert wc.view().registry is not None, "the working copy stays open"
            wc.discard()

        assert (caught.value.rule, caught.value.item, caught.value.urn) == ("structure-home", 7, UNIT_URN)
        assert caught.value.editions == ((GEO, wc.edition_id), (CLASSIFICATIONS, _live(root, CLASSIFICATIONS)))
        assert _current(root) == before

    def test_a_second_home_landing_between_the_pre_check_and_the_commit_is_refused_at_the_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "catalog"
        structures = _edited()
        with sc.Catalog.create(root) as cat:
            first = _structure_wc(cat, CLASSIFICATIONS, structures)
            second = _structure_wc(cat, "structure-units", structures, extra=[structures.unit])
            real = second._seal_edition

            def racing() -> object:
                sealed = real()
                first.seal()  # another writer records the first home now
                return sealed

            monkeypatch.setattr(second, "_seal_edition", racing)
            with pytest.raises(sc.CrossProductRuleError) as caught:
                second.seal()

        assert caught.value.rule == "structure-home"
        assert "structure-units" not in _full(root).products()
        assert (root / "products" / "structure-units" / second.edition_id / "manifest.json").exists()
        (item,) = diagnostics.doctor(root).about("unrecorded edition")
        assert second.edition_id in item.detail

    def test_under_embargo_a_second_home_is_recorded_and_its_release_refused(self, root: Path) -> None:
        unit = testing._structures(SEED, SHAPE).unit
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            edition = _structure_wc(cat, GEO, _edited(), extra=[unit]).seal()

        assert _states(root, GEO)[edition.edition_id] == "embargoed"
        with pytest.raises(sc.CrossProductRuleError) as caught:
            CatalogAdmin.open(root).release(GEO, edition.edition_id, reason=REASON)
        assert (caught.value.rule, caught.value.urn) == ("structure-home", UNIT_URN)


class TestItem8:
    def test_cuj_3_the_table_is_released_after_its_structure_edition(self, root: Path) -> None:
        old_table = _live(root, TABLE)
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            geo = _structure_wc(cat, GEO, _edited(geo=True)).seal()
            table = _table_wc(cat, TABLE, _edited(geo=True)).seal()
        assert diagnostics.verify_catalog(root).stale == (), "each table edition matches a home edition"
        admin = CatalogAdmin.open(root)
        before = _current(root)

        with pytest.raises(sc.CrossProductRuleError) as caught:
            admin.release(TABLE, table.edition_id, reason=REASON)
        assert (caught.value.rule, caught.value.item, caught.value.urn) == ("publish-order", 8, GEO_URN)
        assert caught.value.editions == ((TABLE, table.edition_id), (GEO, geo.edition_id))
        assert _current(root) == before

        admin.release(GEO, geo.edition_id, reason=REASON)
        (stale,) = diagnostics.verify_catalog(root).stale
        assert (stale.edition_id, stale.state) == (old_table, "live")

        admin.release(TABLE, table.edition_id, reason=REASON)
        assert _live(root, TABLE) == table.edition_id
        assert diagnostics.verify_catalog(root).stale == ()

    def test_with_embargo_off_the_seal_is_refused_before_sdmxlibs_seal(self, root: Path) -> None:
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            _structure_wc(cat, GEO, _edited(geo=True)).seal()
        _set_embargo(root, on=False)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, TABLE, _edited(geo=True))
            with pytest.raises(sc.CrossProductRuleError, match="item 8") as caught:
                wc.seal()
            assert not (root / "products" / TABLE / wc.edition_id / "manifest.json").exists()
            wc.discard()
        assert (caught.value.rule, caught.value.urn) == ("publish-order", GEO_URN)

    def test_a_full_artefact_matching_only_an_embargoed_home_edition_waits_for_it(self, root: Path) -> None:
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            _structure_wc(cat, CONCEPTS, _edited(concepts=True)).seal()
        _set_embargo(root, on=False)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, TABLE, _edited(concepts=True))
            with pytest.raises(sc.CrossProductRuleError) as caught:
                wc.seal()
            wc.discard()
        assert (caught.value.rule, caught.value.urn) == ("publish-order", CS_URN)


class TestStale:
    def test_an_embargoed_table_cut_from_the_old_structure_is_refused_once_the_edit_is_live(self, root: Path) -> None:
        _set_embargo(root, on=True)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            table = _table_wc(cat, TABLE, _edited()).seal()
            geo = _structure_wc(cat, GEO, _edited(geo=True)).seal()
        assert diagnostics.verify_catalog(root).stale == ()
        admin = CatalogAdmin.open(root)
        admin.release(GEO, geo.edition_id, reason=REASON)

        stale = {(s.edition_id, s.state) for s in diagnostics.verify_catalog(root).stale}
        assert (table.edition_id, "embargoed") in stale
        items = diagnostics.doctor(root).about("stale data editions")
        levels = {item.detail.split(" ", 1)[0]: item.level for item in items}
        assert levels[f"{TABLE}/{table.edition_id}"] == "warning"
        assert levels[f"{TABLE}/{_live(root, TABLE)}"] == "info"
        with pytest.raises(sc.CrossProductRuleError) as caught:
            admin.release(TABLE, table.edition_id, reason=REASON)
        assert (caught.value.rule, caught.value.urn) == ("full-artefacts", GEO_URN)


class TestBatch:
    def test_a_structure_edit_and_a_table_cut_from_it_go_live_in_one_version(self, root: Path) -> None:
        seq = _full(root).seq
        with sc.Catalog.create(root, exist_ok=True) as cat:
            outcomes = cat.seal([_table_wc(cat, TABLE, _edited(geo=True)), _structure_wc(cat, GEO, _edited(geo=True))])

        assert outcomes.refused == {}
        assert _full(root).seq == seq + 1
        assert diagnostics.verify_catalog(root).stale == ()

    def test_a_structure_edition_breaking_item_7_drops_the_table_cut_from_it(self, root: Path) -> None:
        unit = testing._structures(SEED, SHAPE).unit
        before = _current(root)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            geo = _structure_wc(cat, GEO, _edited(geo=True), extra=[unit])
            table = _table_wc(cat, TABLE, _edited(geo=True))
            outcomes = cat.seal([table, geo])
            geo.discard()
            table.discard()

        assert outcomes.sealed == {}
        geo_error, table_error = outcomes.refused[GEO], outcomes.refused[TABLE]
        assert isinstance(geo_error, sc.CrossProductRuleError)
        assert geo_error.rule == "structure-home"
        assert isinstance(table_error, sc.CrossProductRuleError)
        assert (table_error.rule, table_error.urn) == ("full-artefacts", GEO_URN)
        assert _current(root) == before

    def test_a_crashed_batch_is_recorded_in_one_version_by_the_tables_next_working_copy(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def killed(*_args: object, **_kwargs: object) -> None:
            raise Killed

        seq = _full(root).seq
        with monkeypatch.context() as mp, sc.Catalog.create(root, exist_ok=True) as cat:
            mp.setattr(catalog_module, "_record_batch", killed)
            geo = _structure_wc(cat, GEO, _edited(geo=True))
            table = _table_wc(cat, TABLE, _edited(geo=True))
            with pytest.raises(Killed):
                cat.seal([geo, table])
        assert _full(root).seq == seq

        with sc.Catalog.create(root, exist_ok=True) as cat:
            cat.product(TABLE).working_copy().discard()

        assert _full(root).seq == seq + 1
        assert _live(root, GEO) == geo.edition_id
        assert _live(root, TABLE) == table.edition_id
        assert not list((root / "products" / GEO / "journal").glob("*.json"))


def test_a_rollback_to_an_edition_cut_from_a_superseded_structure_edition_is_refused(root: Path) -> None:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        outcomes = cat.seal([_structure_wc(cat, GEO, _edited(geo=True)), _table_wc(cat, TABLE, _edited(geo=True))])
    assert outcomes.refused == {}
    before = _current(root)

    with pytest.raises(sc.CrossProductRuleError) as caught:
        CatalogAdmin.open(root).rollback(TABLE, reason="back to the first table")

    assert (caught.value.rule, caught.value.urn) == ("full-artefacts", GEO_URN)
    assert _current(root) == before


def test_the_pre_checks_fingerprints_are_the_sealed_manifests(root: Path) -> None:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = _table_wc(cat, "table-b", _edited())
        planned = wc._own_artefacts()
        edition = wc.seal()

    sealed = manifest_records(read_manifest(root / "products" / "table-b" / edition.edition_id))
    assert planned == sealed
    assert any(record.partial_of is not None for record in planned.values())


class TestSabotage:
    """With `cross_product.refusals` refusing every product, each path that records an edition refuses."""

    @pytest.fixture(autouse=True)
    def refuse_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def everything(_full: object, leaves: dict[str, object], _artefacts: object) -> dict[str, Refusal]:
            return {
                p: Refusal(p, "e", "full-artefacts", 4, "urn:x", ((p, "e"),), "refused by the test") for p in leaves
            }

        monkeypatch.setattr(cross_product, "refusals", everything)

    def test_working_copy_seal_refuses(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, "table-b", _edited())
            with pytest.raises(sc.CrossProductRuleError, match="refused by the test"):
                wc.seal()
            wc.discard()

    def test_catalog_seal_refuses(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _table_wc(cat, "table-b", _edited())
            outcomes = cat.seal([wc])
            wc.discard()
        assert isinstance(outcomes.refused["table-b"], sc.CrossProductRuleError)

    def test_release_refuses(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_embargo(root, on=True)
        with monkeypatch.context() as mp, sc.Catalog.create(root, exist_ok=True) as cat:
            mp.setattr(cross_product, "refusals", lambda *_args: {})
            edition = codr_product(cat, "table-b", seed=SEED, shape=SHAPE)
        with pytest.raises(sc.CrossProductRuleError, match="refused by the test"):
            CatalogAdmin.open(root).release("table-b", edition.edition_id, reason=REASON)


def test_codr_structures_seal_with_no_refusal(tmp_path: Path) -> None:
    with sc.Catalog.create(tmp_path / "catalog") as cat:
        assert sorted(codr_structures(cat, seed=SEED, shape=SHAPE)) == sorted(STRUCTURE_PRODUCTS)

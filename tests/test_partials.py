"""Partials in data products: `fill_dataflows`, ``statcat._meta.partial_of`` and the publish check (GH-51, S2).

A data product's working copy is filled with `WorkingCopy.fill_dataflows`
(or `ingest_dataflows`), which cuts each codelist to the codes the product
uses and records the full codelist's fingerprint in ``statcat._meta``. The
seal passes that map to sdmxlib as ``partial_of`` and checks every partial
against the catalog's structure editions before the Inventory records the
edition.
"""

from __future__ import annotations

import contextlib
import json
import logging
import multiprocessing
import shutil
from collections.abc import Generator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import attrs
import pytest
import sdmxlib as sl
import sdmxlib.edition as sdmx_edition
from sdmxlib.edition import ArtefactEntry, Manifest, ManifestError, check_edition, read_manifest
from sdmxlib.inventory import FullInventory, local_fetch

import statcatlib as sc
from statcatlib import diagnostics, testing
from statcatlib.store import working_copy as working_copy_module
from statcatlib.testing import STRUCTURE_PRODUCTS, CodrShape, codr_product, codr_structures
from tests.conftest import untyped

pytestmark = pytest.mark.integration

#: Small enough to seal in about a second, large enough that every codelist is cut.
SHAPE = CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20), observations=200)
SEED = 3
TABLE = "table-a"
GEO_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_GEO(1.0)"
CLASS_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_CLASS_1(1.0)"


class Killed(BaseException):
    """The process died here."""


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A catalog holding the three CODR structure products, sealed once for the module."""
    root = tmp_path_factory.mktemp("codr") / "catalog"
    with sc.Catalog.create(root) as cat:
        codr_structures(cat, seed=SEED, shape=SHAPE)
    return root


@pytest.fixture
def root(template: Path, tmp_path: Path) -> Path:
    """A copy of `template` for one test."""
    copy = tmp_path / "catalog"
    shutil.copytree(template, copy)
    return copy


def _full(root: Path) -> FullInventory:
    return FullInventory(local_fetch(root)).refresh()


def _manifest(root: Path, product_id: str, edition_id: str) -> Manifest:
    return read_manifest(root / "products" / product_id / edition_id)


def _live_dir(root: Path, product_id: str) -> Path:
    (entry,) = [e for e in _full(root).entries(product_id) if e.state == "live"]
    return root / "products" / product_id / entry.edition_id


def _partials(manifest: Manifest) -> dict[str, ArtefactEntry]:
    return {a.urn: a for a in manifest.artefacts if a.partial}


def _homes(root: Path) -> dict[str, str]:
    """Each full artefact's fingerprint in the live structure editions, by URN."""
    found: dict[str, str] = {}
    for product_id in STRUCTURE_PRODUCTS:
        found |= {a.urn: a.fingerprint for a in read_manifest(_live_dir(root, product_id)).artefacts}
    return found


@contextlib.contextmanager
def _source(product_id: str = TABLE, tables: int = 1) -> Generator[tuple[sl.LocalRegistry, list[sl.Dataflow]]]:
    """A scratch registry holding the CODR structures and *product_id*'s tables, with its dataflows."""
    structures = testing._structures(SEED, SHAPE)
    built = [testing._table(product_id, n, structures) for n in range(1, tables + 1)]
    held = [a for artefacts in structures.by_product().values() for a in artefacts]
    with testing._scratch([*held, *(a for pair in built for a in pair)]) as scratch:
        yield scratch, [flow for _, flow in built]


def _rows(flow: sl.Dataflow, product_id: str = TABLE, n: int = 1) -> dict[str, Any]:
    """Table *n* of *product_id*'s rows, keyed by *flow*'s URN."""
    return {str(flow.urn): testing._observations(product_id, n, SEED, SHAPE, testing._structures(SEED, SHAPE))}


def _resume_and_seal(root: str, product_id: str) -> str:
    """In a process of its own: resume *product_id*'s working copy, seal it, and return the edition id."""
    with sc.Catalog.create(root, exist_ok=True) as cat:
        return cat.product(product_id).working_copy(resume=True).seal().edition_id


def _filled(cat: sc.Catalog, *, rows_of: str = TABLE) -> sc.WorkingCopy:
    """A working copy of `TABLE` filled with its one table, the rows drawn as *rows_of*'s."""
    wc = cat.product(TABLE).working_copy()
    with _source() as (scratch, (flow,)):
        sc.ingest_dataflows(wc, scratch, [flow.urn], observations=_rows(flow, rows_of))
    return wc


class TestPartialOf:
    def test_each_partial_records_its_structure_homes_fingerprint_and_the_check_gets_all_three(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        given: list[object] = []
        real = working_copy_module.check_edition

        def recording(path: Path, **kwargs: Any) -> Any:
            given.append(kwargs.get("structures"))
            return real(path, **kwargs)

        monkeypatch.setattr(working_copy_module, "check_edition", recording)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            edition = codr_product(cat, TABLE, seed=SEED, shape=SHAPE)

        partials = _partials(_manifest(root, TABLE, edition.edition_id))
        homes = _homes(root)
        assert sorted(partials) == sorted(
            [
                GEO_URN,
                CLASS_URN,
                "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_CLASS_2(1.0)",
                "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_UNIT(1.0)",
                "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SDMX:CL_OBS_STATUS(1.0)",
            ]
        )
        assert {urn: entry.partial_of for urn, entry in partials.items()} == {urn: homes[urn] for urn in partials}
        (structures,) = given
        assert isinstance(structures, list)
        assert sorted(structures) == sorted(_live_dir(root, p) for p in STRUCTURE_PRODUCTS)
        assert [e.state for e in _full(root).entries(TABLE)] == ["live"]

    def test_a_partial_is_resolved_in_the_second_structure_product(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            edition = codr_product(cat, TABLE, seed=SEED, shape=SHAPE)
        edition_dir = root / "products" / TABLE / edition.edition_id
        geo, classifications = (_live_dir(root, p) for p in STRUCTURE_PRODUCTS[:2])

        both = check_edition(edition_dir, at="publish", structures=[geo, classifications])
        geography_only = check_edition(edition_dir, at="publish", structures=[geo])

        assert both.findings == ()
        assert CLASS_URN in {f.subject for f in geography_only.findings}
        assert GEO_URN not in {f.subject for f in geography_only.findings}

    def test_a_working_copy_resumed_and_sealed_in_another_process_records_every_partial_of(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _filled(cat)
            cut = working_copy_module.recorded_cuts(wc.view()._cursor())
            wc.close()
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
            edition_id = pool.submit(_resume_and_seal, str(root), TABLE).result()

        partials = _partials(_manifest(root, TABLE, edition_id))
        assert {urn: entry.partial_of for urn, entry in partials.items()} == cut
        assert set(cut) == set(partials)
        assert [e.state for e in _full(root).entries(TABLE)] == ["live"]

    def test_a_seal_sdmxlib_refused_keeps_the_map_for_the_resumed_seal(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refused(*_args: object, **_kwargs: object) -> None:
            msg = "refused by the test"
            raise ManifestError(msg)

        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _filled(cat)
            cut = working_copy_module.recorded_cuts(wc.view()._cursor())
            with monkeypatch.context() as mp:
                mp.setattr(sdmx_edition, "seal", refused)
                with pytest.raises(ManifestError, match="refused by the test"):
                    wc.seal()
            edition = cat.product(TABLE).working_copy(resume=True).seal()

        partials = _partials(_manifest(root, TABLE, edition.edition_id))
        assert {urn: entry.partial_of for urn, entry in partials.items()} == cut


class TestRepublish:
    def test_a_republish_with_new_observations_cuts_again_and_keeps_every_partial_of(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            first = codr_product(cat, TABLE, seed=SEED, shape=SHAPE)
            wc = _filled(cat, rows_of="other-rows")
            second = wc.seal()

        before = _partials(_manifest(root, TABLE, first.edition_id))
        after = _partials(_manifest(root, TABLE, second.edition_id))
        homes = _homes(root)
        assert set(after) == set(before)
        assert {urn: entry.partial_of for urn, entry in after.items()} == {urn: homes[urn] for urn in after}
        assert after[GEO_URN].fingerprint != before[GEO_URN].fingerprint, "the geography was cut to other codes"

    def test_a_republish_that_fills_nothing_keeps_its_partials_cut(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            first = codr_product(cat, TABLE, seed=SEED, shape=SHAPE)
            wc = cat.product(TABLE).working_copy()
            assert working_copy_module.recorded_cuts(wc.view()._cursor()) == {}, "a new edition starts with no map"
            second = wc.seal()

        assert _partials(_manifest(root, TABLE, second.edition_id)) == _partials(
            _manifest(root, TABLE, first.edition_id)
        )
        assert [e.state for e in _full(root).entries(TABLE)] == ["superseded", "live"]

    def test_a_fill_without_partials_replaces_each_partial_and_records_no_partial_of(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _filled(cat)
            with _source() as (scratch, (flow,)):
                assert wc.fill_dataflows(scratch, [str(flow.urn)], partials=False) == {}
            assert GEO_URN in working_copy_module.recorded_cuts(wc.view()._cursor())
            edition = wc.seal()

        manifest = _manifest(root, TABLE, edition.edition_id)
        assert _partials(manifest) == {}
        assert GEO_URN in {a.urn for a in manifest.artefacts}


class TestRefusals:
    def test_a_partial_with_a_code_its_full_codelist_lacks_fails_the_publish_check(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = _filled(cat)
            partial = wc.registry.get(sl.Codelist.parse_urn(GEO_URN), eager=True)
            assert isinstance(partial, sl.Codelist)
            assert partial.is_partial
            extra = sl.Code(id="XTRA", name={"en": "not in the full codelist"})
            wc.registry.replace(partial.urn, attrs.evolve(partial, codes=[*partial.codes, extra]))

            with pytest.raises(sc.EditionCheckError, match="XTRA") as caught:
                wc.seal()

        assert caught.value.product == TABLE
        assert any(f.check == "partials" and f.subject == GEO_URN for f in caught.value.findings)
        assert TABLE not in _full(root).products()
        (item,) = diagnostics.doctor(root).about("unrecorded edition")
        assert wc.edition_id in item.detail

    def test_a_partial_with_no_structure_edition_in_the_catalog_fails_the_publish_check(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        with sc.Catalog.create(root) as cat:
            wc = _filled(cat)
            with pytest.raises(sc.EditionCheckError, match="no live or embargoed structure edition") as caught:
                wc.seal()

        assert {f.subject for f in caught.value.findings if f.check == "partials"} >= {GEO_URN, CLASS_URN}
        assert not (root / "CURRENT").exists(), "nothing is recorded"

    def test_a_fill_that_stopped_before_its_record_refuses_the_seal_until_one_completes(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def killed(*_args: object, **_kwargs: object) -> None:
            raise Killed

        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product(TABLE).working_copy()
            with _source() as (scratch, (flow,)):
                with monkeypatch.context() as mp:
                    mp.setattr(sdmx_edition, "fill_edition", killed)
                    with pytest.raises(Killed):
                        wc.fill_dataflows(scratch, [flow.urn], observations=_rows(flow))

                with pytest.raises(sc.SealCheckError, match="fill_in_progress"):
                    wc.seal()

                wc.fill_dataflows(scratch, [flow.urn], observations=_rows(flow))
            edition = wc.seal()

        assert GEO_URN in _partials(_manifest(root, TABLE, edition.edition_id))

    def test_a_partial_cut_again_outside_fill_dataflows_refuses_the_seal(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            codr_product(cat, TABLE, seed=SEED, shape=SHAPE)
            wc = cat.product(TABLE).working_copy()
            with _source() as (scratch, (flow,)):
                sdmx_edition.fill_edition(wc.registry, scratch, [flow.urn], observations=_rows(flow, "other-rows"))

            with pytest.raises(sc.SealCheckError, match="cut again outside fill_dataflows") as caught:
                wc.seal()

            assert GEO_URN in {f.subject for f in caught.value.findings}
            assert not (root / "products" / TABLE / wc.edition_id / "manifest.json").exists()
            wc.discard()

    def test_fill_dataflows_refuses_a_structure_product_and_ingest_dataflows_a_view(self, root: Path) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product(STRUCTURE_PRODUCTS[0]).working_copy()
            with _source() as (scratch, (flow,)):
                with pytest.raises(TypeError, match="fill_dataflows fills a data product"):
                    wc.fill_dataflows(scratch, [flow.urn])
                with pytest.raises(TypeError, match="takes a WorkingCopy"):
                    sc.ingest_dataflows(untyped(wc.view()), scratch, [flow.urn])
            wc.discard()


class TestBatchAndRepair:
    def test_structure_products_and_a_table_cut_from_them_are_recorded_in_one_version(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        structures = testing._structures(SEED, SHAPE)
        with sc.Catalog.create(root) as cat, _source() as (scratch, (flow,)):
            batch: list[sc.WorkingCopy] = []
            for product_id, artefacts in structures.by_product().items():
                structure = cat.product(product_id).working_copy(kind="structure")
                structure.fill_structures(scratch, artefacts=[a.urn for a in artefacts])
                batch.append(structure)
            table = cat.product(TABLE).working_copy()
            sc.ingest_dataflows(table, scratch, [flow.urn], observations=_rows(flow))
            outcomes = cat.seal([table, *batch])

        assert outcomes.refused == {}
        assert sorted(outcomes.sealed) == sorted([*STRUCTURE_PRODUCTS, TABLE])
        assert _full(root).seq == 1
        partials = _partials(_manifest(root, TABLE, outcomes.sealed[TABLE].edition_id))
        homes = _homes(root)
        assert {urn: e.partial_of for urn, e in partials.items()} == {urn: homes[urn] for urn in partials}

    def test_the_journal_repair_checks_a_data_edition_with_partials_and_records_it(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def killed(*_args: object, **_kwargs: object) -> None:
            raise Killed

        with monkeypatch.context() as mp, sc.Catalog.create(root, exist_ok=True) as cat:
            mp.setattr(working_copy_module, "publish_check", killed)
            wc = _filled(cat)
            with pytest.raises(Killed):
                wc.seal()
        checked: list[tuple[Path, tuple[str, ...]]] = []
        real = working_copy_module.publish_check

        def recording(edition_dir: Path, **kwargs: Any) -> None:
            checked.append((edition_dir, tuple(kwargs.get("partials", ()))))
            real(edition_dir, **kwargs)

        monkeypatch.setattr(working_copy_module, "publish_check", recording)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            cat.product(TABLE).working_copy().discard()

        ((edition_dir, partials),) = checked
        assert edition_dir == root / "products" / TABLE / wc.edition_id
        assert GEO_URN in partials
        assert [e.edition_id for e in _full(root).entries(TABLE)] == [wc.edition_id]

    def test_the_journal_repair_leaves_a_data_edition_whose_partials_fail_unrecorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "catalog"

        def killed(*_args: object, **_kwargs: object) -> None:
            raise Killed

        with monkeypatch.context() as mp, sc.Catalog.create(root) as cat:
            mp.setattr(working_copy_module, "publish_check", killed)
            wc = _filled(cat)
            with pytest.raises(Killed):
                wc.seal()

        with caplog.at_level(logging.WARNING), sc.Catalog.create(root, exist_ok=True) as cat:
            cat.product(TABLE).working_copy().discard()

        assert f"the journalled edition {wc.edition_id} cannot be recorded" in caplog.text
        assert "no live or embargoed structure edition" in caplog.text
        assert not (root / "CURRENT").exists()


def test_the_map_is_json_in_meta(root: Path) -> None:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = _filled(cat)
        rows = wc.view()._cursor().execute("SELECT value FROM _meta WHERE key = 'partial_of'").fetchall()
        wc.discard()
    ((text,),) = rows
    assert GEO_URN in json.loads(text)

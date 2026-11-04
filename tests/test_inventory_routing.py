"""The routing fields each recorded leaf entry carries (Inventory routing fields, GH-98).

A seal and the journal repair record ``dataflows`` and ``pull_bytes`` for every
edition and ``artefacts`` for a structure edition, from the edition's manifest.
A rollback and a reinstate keep them, and ``statcat verify-catalog`` detects an
edited field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import msgspec
import polars as pl
import pytest
import sdmxlib
import sdmxlib.edition
from sdmxlib import InternationalString, Ref
from sdmxlib.edition import read_manifest
from sdmxlib.inventory import FullInventory, LeafEntry, local_fetch, manifest_records, reinstate, retire, routing_fields
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib import diagnostics, testing
from statcatlib.cli import app
from statcatlib.store.inventory import Change, InventoryWriter
from statcatlib.testing import STRUCTURE_PRODUCTS, CodrShape
from tests.conftest import concept_of

pytestmark = pytest.mark.integration

runner = CliRunner()

_SHAPE = CodrShape(geography_codes=6, geography_depth=2, classification_codes=(3, 3, 3), observations=4)


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=InternationalString(en=identifier), description=InternationalString(en="d")
    )


def _bound(wc: sc.WorkingCopy, flow_id: str = "DF_C") -> str:
    """Write a dataflow and one bound observation; return the dataflow's URN."""
    scheme = sdmxlib.ConceptScheme(
        id="CS_TEST",
        maintainer="TEST",
        name=InternationalString(en="Concepts"),
        concepts=[sdmxlib.Concept(id="FREQ", name=InternationalString(en="Frequency"))],
    )
    cl = sdmxlib.Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=InternationalString(en="Frequency"),
        codes=[sdmxlib.Code(id="A", name=InternationalString(en="Annual"))],
    )
    dsd = sdmxlib.DataStructure(
        id="DSD_C",
        maintainer="TEST",
        name=InternationalString(en="DSD"),
        dimensions=[sdmxlib.Dimension(id="FREQ", concept=concept_of("FREQ"), representation=cl)],
    )
    flow = sdmxlib.Dataflow(id=flow_id, maintainer="TEST", name=InternationalString(en="Flow"), structure=dsd)
    reg = wc.registry
    reg.add_all([scheme, cl, dsd, flow])
    reg.write_observations(schema="obs", table="df_c", source=pl.DataFrame({"FREQ": ["A"]}))
    reg.add(sdmxlib.DataflowBinding.from_dsd(dsd, dataflow=Ref.to(flow), schema="obs", table="df_c"))
    return f"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=TEST:{flow_id}(1.0)"


def _entries(root: Path, product: str) -> dict[str, LeafEntry]:
    return {e.edition_id: e for e in FullInventory(local_fetch(root)).refresh().entries(product)}


def _manifest(root: Path, product: str, edition_id: str) -> sdmxlib.edition.Manifest:
    return read_manifest(root / "products" / product / edition_id)


@pytest.fixture
def data_catalog(tmp_path: Path) -> tuple[Path, str, str]:
    """A catalog with one bound data edition: its path, its edition id and its dataflow's URN."""
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy()
        urn = _bound(wc)
        wc.put(_dataset("d1"))
        edition = wc.seal()
    return root, edition.edition_id, urn


@pytest.fixture
def structure_catalog(tmp_path: Path) -> tuple[Path, str]:
    """A catalog with the CODR structure products sealed in a batch and one table; its path and a table's id."""
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        testing.codr_structures(cat, seed=1, shape=_SHAPE)
        edition = testing.codr_product(cat, "table-a", seed=1, shape=_SHAPE)
    return root, edition.edition_id


class TestRecorded:
    def test_a_data_edition_records_both_routing_fields_and_no_artefacts(
        self, data_catalog: tuple[Path, str, str]
    ) -> None:
        root, edition_id, urn = data_catalog
        entry = _entries(root, "p")[edition_id]
        expected = routing_fields(_manifest(root, "p", edition_id))
        assert entry.has_routing
        assert entry.dataflows == expected.dataflows == (urn,)
        assert entry.pull_bytes == expected.pull_bytes
        assert expected.pull_bytes > 0
        assert entry.artefacts is None

    def test_a_structure_edition_records_its_artefacts_and_no_dataflow(
        self, structure_catalog: tuple[Path, str]
    ) -> None:
        root, _ = structure_catalog
        for product in STRUCTURE_PRODUCTS:
            (entry,) = _entries(root, product).values()
            manifest = _manifest(root, product, entry.edition_id)
            assert entry.dataflows == ()
            assert entry.pull_bytes == routing_fields(manifest).pull_bytes
            assert entry.artefacts == manifest_records(manifest)
            assert entry.artefacts

    def test_a_batched_data_edition_records_no_artefacts(self, structure_catalog: tuple[Path, str]) -> None:
        root, edition_id = structure_catalog
        entry = _entries(root, "table-a")[edition_id]
        assert entry.has_routing
        assert isinstance(entry.dataflows, tuple)
        assert len(entry.dataflows) == 1
        assert entry.artefacts is None

    def test_a_journal_repair_records_them(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A seal killed before its record: the next working copy records the edition with its routing fields."""

        class Killed(BaseException):
            """The process died here."""

        root = tmp_path / "catalog"
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy()
            urn = _bound(wc)
            wc.put(_dataset("d1"))
            edition_id = wc.edition_id

            def killed(_change: object, **_kwargs: object) -> None:
                raise Killed

            with monkeypatch.context() as patched:
                patched.setattr(wc._writer, "commit", killed)
                with pytest.raises(Killed):
                    wc.seal()
        with sc.Catalog.create(root, exist_ok=True) as cat:
            cat.product("p").working_copy().discard()
        entry = _entries(root, "p")[edition_id]
        assert entry.dataflows == (urn,)
        assert entry.pull_bytes == routing_fields(_manifest(root, "p", edition_id)).pull_bytes
        assert entry.artefacts is None


class TestKept:
    def test_a_rolled_back_entry_keeps_them(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy()
            _bound(wc)
            first = wc.seal()
            wc = cat.product("p").working_copy()
            wc.put(_dataset("d2"))
            second = wc.seal()
        before = _entries(root, "p")
        sc.CatalogAdmin.open(root).rollback("p", reason="the new figures are wrong")
        after = _entries(root, "p")
        assert after[first.edition_id].state == "live"
        assert after[second.edition_id].state == "withdrawn"
        for edition_id in (first.edition_id, second.edition_id):
            assert after[edition_id].dataflows == before[edition_id].dataflows
            assert after[edition_id].pull_bytes == before[edition_id].pull_bytes
            assert after[edition_id].has_routing

    def test_a_reinstated_entry_keeps_them(self, tmp_path: Path) -> None:
        """statcatlib's admin has no reinstate yet (GH-98), so the writer commits sdmxlib's rule directly."""
        root = tmp_path / "catalog"
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy()
            _bound(wc)
            first = wc.seal()
            wc = cat.product("p").working_copy()
            wc.put(_dataset("d2"))
            wc.seal()
        recorded = _entries(root, "p")[first.edition_id]
        writer = InventoryWriter(root)
        now = datetime.now(UTC)

        def retiring(full: FullInventory | None) -> Change:
            assert full is not None
            leaf, stones = retire(full.leaf("p"), [first.edition_id], now=now)
            return Change(leaves={"p": leaf}, retired=stones)

        writer.commit(retiring, kind="retire", reason="old")
        assert first.edition_id not in _entries(root, "p")
        fields = routing_fields(_manifest(root, "p", first.edition_id))

        def reinstating(full: FullInventory | None) -> Change:
            assert full is not None
            leaf, stones = reinstate(
                full.leaf("p"),
                full.retired,
                edition_id=first.edition_id,
                previous=recorded.previous,
                manifest_sha256=recorded.manifest_sha256,
                schema_version=recorded.schema_version,
                now=now,
                dataflows=fields.dataflows,
                pull_bytes=fields.pull_bytes,
            )
            return Change(leaves={"p": leaf}, retired=stones)

        writer.commit(reinstating, kind="reinstate", reason="back")
        entry = _entries(root, "p")[first.edition_id]
        assert entry.state == "superseded"
        assert (entry.dataflows, entry.pull_bytes) == (recorded.dataflows, recorded.pull_bytes)


class TestVerify:
    def _edit_entry(self, root: Path, product: str, edition_id: str, **changes: object) -> str:
        """Commit a leaf whose entry has *changes* in it; return the new leaf's path."""
        writer = InventoryWriter(root)
        full = writer.full()
        assert full is not None
        leaf = full.leaf(product)
        edited = msgspec.structs.replace(
            leaf,
            editions=tuple(
                msgspec.structs.replace(e, **changes) if e.edition_id == edition_id else e for e in leaf.editions
            ),
        )
        return writer.try_commit(full.root, {product: edited}, kind="correct", reason="an edit").products[product].leaf

    def test_a_clean_catalog_has_no_finding(self, data_catalog: tuple[Path, str, str]) -> None:
        root, _, _ = data_catalog
        report = diagnostics.verify_catalog(root)
        assert report.ok, report.findings

    def test_an_edited_field_is_a_finding_and_the_command_exits_1(self, data_catalog: tuple[Path, str, str]) -> None:
        root, edition_id, _ = data_catalog
        leaf = self._edit_entry(root, "p", edition_id, pull_bytes=1)
        (finding,) = diagnostics.verify_catalog(root).findings
        assert finding.file == leaf
        assert "pull_bytes 1" in finding.detail
        result = runner.invoke(app, ["verify-catalog", str(root)])
        assert result.exit_code == 1, result.output
        assert f"FAIL {leaf}" in result.output

    def test_edited_dataflows_are_a_finding(self, data_catalog: tuple[Path, str, str]) -> None:
        root, edition_id, _ = data_catalog
        self._edit_entry(root, "p", edition_id, dataflows=("urn:other",))
        (finding,) = diagnostics.verify_catalog(root).findings
        assert "dataflows" in finding.detail

    def test_an_entry_without_the_fields_is_skipped(self, data_catalog: tuple[Path, str, str]) -> None:
        root, edition_id, _ = data_catalog
        self._edit_entry(root, "p", edition_id, dataflows=msgspec.UNSET, pull_bytes=msgspec.UNSET)
        report = diagnostics.verify_catalog(root)
        assert report.ok, report.findings

    def test_edited_artefacts_are_a_finding(self, structure_catalog: tuple[Path, str]) -> None:
        root, _ = structure_catalog
        product = STRUCTURE_PRODUCTS[0]
        (entry,) = _entries(root, product).values()
        assert entry.artefacts is not None
        self._edit_entry(root, product, entry.edition_id, artefacts={})
        report = diagnostics.verify_catalog(root)
        assert any("artefacts differ" in f.detail for f in report.findings), report.findings

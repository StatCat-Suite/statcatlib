"""The cross-product rules as pure functions: `cross_product.refusals` over built leaves (GH-51, S3).

Each test builds an Inventory version with sdmxlib's rules (`record`) and
answers every edition's artefacts from a dict, so the rules of specification
§6.5 items 4, 7 and 8 are checked without a catalog on disk.
`tests/test_cross_product.py` drives the same rules through seals and
releases. `TestRecordsFromLeaves` checks where the rules take each entry's
records from: the incoming entry's manifest, and every other entry's leaf
entry when it is filled (Phase 5 D6, GH-125 part 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sdmxlib.inventory import FullInventory, Kind, Leaf, PartialRecord, record, release
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
from statcatlib import testing
from statcatlib.store import cross_product
from statcatlib.store.cross_product import Refusal
from statcatlib.store.inventory import InventoryWriter
from statcatlib.testing import CodrShape

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
GEO = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_GEO(1.0)"
CS = "urn:sdmx:org.sdmx.infomodel.conceptscheme.ConceptScheme=CODR:CS_CODR(1.0)"
DSD = "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=CODR:DSD_SHARED(1.0)"


def _edition(n: int) -> str:
    return f"01a18e00-0000-7000-8000-{n:012d}"


class Catalog:
    """Builds leaves with sdmxlib's `record`, writes them with `InventoryWriter`, and holds each edition's artefacts.

    The writer reads artefacts from `held`, so no edition needs files on disk.
    """

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.held: dict[str, dict[str, PartialRecord]] = {}
        self.writer = InventoryWriter(root, artefacts=self.read)
        self.writer.write_catalog_json()
        self._n = 0

    @property
    def leaves(self) -> dict[str, Leaf]:
        full = self.writer.full()
        return {} if full is None else dict(full.leaves())

    def seal(
        self,
        product: str,
        kind: Kind,
        artefacts: Mapping[str, PartialRecord],
        *,
        embargo: bool = False,
        filled: bool = False,
    ) -> tuple[str, Leaf]:
        """The product's next leaf with a new edition recorded, *filled* with *artefacts*; nothing is written."""
        self._n += 1
        edition = _edition(self._n)
        leaf = self.leaves.get(product)
        live = None if leaf is None else next((e.edition_id for e in leaf.editions if e.state == "live"), None)
        nxt = record(
            leaf,
            product_id=product,
            kind=kind,
            lifecycle="revisable",
            edition_id=edition,
            previous=live,
            manifest_sha256="0" * 64,
            schema_version=SCHEMA_VERSION,
            embargo=embargo,
            now=_NOW,
            artefacts=artefacts if filled else None,
        )
        self.held[edition] = dict(artefacts)
        return edition, nxt

    def keep(self, product: str, leaf: Leaf) -> None:
        """Write *leaf* as the product's leaf in the next version, with no rule applied."""
        self.writer.try_commit(self.writer.head(), {product: leaf}, kind="seal", reason="test")

    def full(self) -> FullInventory | None:
        return self.writer.full()

    def refusals(self, change: Mapping[str, Leaf]) -> dict[str, Refusal]:
        """The rules over *change* written on the kept leaves."""
        return cross_product.refusals(self.full(), change, self.read)

    def read(self, _product: str, edition: str, _sha: str) -> Mapping[str, PartialRecord]:
        return self.held[edition]


def full(fingerprint: str) -> PartialRecord:
    return PartialRecord(fingerprint=fingerprint)


def cut(partial_of: str) -> PartialRecord:
    return PartialRecord(fingerprint="cut-" + partial_of, partial_of=partial_of)


@pytest.fixture
def cat(tmp_path: Path) -> Catalog:
    """A catalog whose structure-geo holds CL_GEO at fingerprint ``g1``, live."""
    c = Catalog(tmp_path / "cat")
    _, leaf = c.seal("structure-geo", "structure", {GEO: full("g1")})
    c.keep("structure-geo", leaf)
    return c


class TestItem7:
    def test_a_second_home_entering_live_is_refused(self, cat: Catalog) -> None:
        edition, leaf = cat.seal("structure-classifications", "structure", {GEO: full("g1")})

        (refusal,) = cat.refusals({"structure-classifications": leaf}).values()

        assert (refusal.rule, refusal.item, refusal.urn) == ("structure-home", 7, GEO)
        assert refusal.editions[0] == ("structure-classifications", edition)
        assert refusal.editions[1][0] == "structure-geo"
        assert "at most one structure home" in refusal.message

    def test_a_second_home_recorded_embargoed_is_accepted(self, cat: Catalog) -> None:
        _, leaf = cat.seal("structure-classifications", "structure", {GEO: full("g1")}, embargo=True)
        assert cat.refusals({"structure-classifications": leaf}) == {}

    def test_a_structure_edit_of_the_home_itself_is_accepted(self, cat: Catalog) -> None:
        _, leaf = cat.seal("structure-geo", "structure", {GEO: full("g2")})
        assert cat.refusals({"structure-geo": leaf}) == {}


class TestItem4:
    def test_a_partial_cut_from_the_home_is_accepted(self, cat: Catalog) -> None:
        _, leaf = cat.seal("table-a", "data", {GEO: cut("g1")})
        assert cat.refusals({"table-a": leaf}) == {}

    def test_a_partial_cut_from_another_copy_is_refused(self, cat: Catalog) -> None:
        edition, leaf = cat.seal("table-a", "data", {GEO: cut("g0")})

        refusal = cat.refusals({"table-a": leaf})["table-a"]

        assert (refusal.rule, refusal.item) == ("full-artefacts", 4)
        assert refusal.editions[0] == ("table-a", edition)
        assert refusal.editions[1][0] == "structure-geo"

    def test_a_cut_codelist_with_no_structure_home_is_refused(self, tmp_path: Path) -> None:
        c = Catalog(tmp_path / "cat")
        edition, leaf = c.seal("table-a", "data", {GEO: cut("g1")})

        refusal = c.refusals({"table-a": leaf})["table-a"]

        assert refusal.rule == "full-artefacts"
        assert refusal.editions == (("table-a", edition),)
        assert "has no structure home" in refusal.message

    def test_a_full_copy_that_differs_from_its_home_is_refused(self, cat: Catalog) -> None:
        _, leaf = cat.seal("table-a", "data", {GEO: full("g0")})
        assert cat.refusals({"table-a": leaf})["table-a"].rule == "full-artefacts"

    def test_a_structure_edit_is_accepted_while_a_data_edition_holds_the_old_copy(self, cat: Catalog) -> None:
        _, table = cat.seal("table-a", "data", {GEO: cut("g1")})
        cat.keep("table-a", table)
        _, edit = cat.seal("structure-geo", "structure", {GEO: full("g2")})

        assert cat.refusals({"structure-geo": edit}) == {}
        cat.keep("structure-geo", edit)
        (stale,) = cross_product.stale_editions(cat.full(), cat.read)
        assert (stale.product_id, stale.urn, stale.fingerprint, stale.home) == ("table-a", GEO, "g1", "structure-geo")
        assert stale.home_live == edit.editions[-1].edition_id


class TestNoHome:
    def _shared(self, root: Path) -> Catalog:
        """Two data products holding DSD_SHARED at ``d1``, live."""
        c = Catalog(root)
        for product in ("table-a", "table-b"):
            _, leaf = c.seal(product, "data", {DSD: full("d1")})
            c.keep(product, leaf)
        return c

    def test_a_changed_copy_entering_live_alone_is_refused(self, tmp_path: Path) -> None:
        c = self._shared(tmp_path / "cat")
        edition, leaf = c.seal("table-a", "data", {DSD: full("d2")})

        refusal = c.refusals({"table-a": leaf})["table-a"]

        assert (refusal.rule, refusal.item, refusal.urn) == ("full-artefacts", 4, DSD)
        assert refusal.editions[0] == ("table-a", edition)
        assert refusal.editions[1][0] == "table-b"
        assert "Catalog.seal" in refusal.message

    def test_a_changed_copy_recorded_embargoed_is_accepted(self, tmp_path: Path) -> None:
        c = self._shared(tmp_path / "cat")
        _, leaf = c.seal("table-a", "data", {DSD: full("d2")}, embargo=True)
        assert c.refusals({"table-a": leaf}) == {}

    def test_its_release_alone_is_refused(self, tmp_path: Path) -> None:
        c = self._shared(tmp_path / "cat")
        edition, leaf = c.seal("table-a", "data", {DSD: full("d2")}, embargo=True)
        c.keep("table-a", leaf)

        released = release(leaf, edition, now=_NOW, explicit=True)

        assert c.refusals({"table-a": released})["table-a"].rule == "full-artefacts"

    def test_every_holder_entering_live_in_one_version_is_accepted(self, tmp_path: Path) -> None:
        c = self._shared(tmp_path / "cat")
        _, a = c.seal("table-a", "data", {DSD: full("d2")})
        _, b = c.seal("table-b", "data", {DSD: full("d2")})
        assert c.refusals({"table-a": a, "table-b": b}) == {}


class TestItem8:
    def _embargoed_edit(self, cat: Catalog) -> str:
        """structure-geo's edit to ``g2``, recorded embargoed."""
        edition, leaf = cat.seal("structure-geo", "structure", {GEO: full("g2")}, embargo=True)
        cat.keep("structure-geo", leaf)
        return edition

    def test_a_partial_cut_from_an_embargoed_home_edition_waits_for_it(self, cat: Catalog) -> None:
        home = self._embargoed_edit(cat)
        edition, leaf = cat.seal("table-a", "data", {GEO: cut("g2")})

        refusal = cat.refusals({"table-a": leaf})["table-a"]

        assert (refusal.rule, refusal.item) == ("publish-order", 8)
        assert refusal.editions == (("table-a", edition), ("structure-geo", home))

    def test_a_full_artefact_matching_an_embargoed_home_edition_waits_for_it(self, cat: Catalog) -> None:
        self._embargoed_edit(cat)
        _, leaf = cat.seal("table-a", "data", {GEO: full("g2")})
        assert cat.refusals({"table-a": leaf})["table-a"].rule == "publish-order"

    def test_recorded_embargoed_it_is_accepted_and_is_not_stale(self, cat: Catalog) -> None:
        self._embargoed_edit(cat)
        _, leaf = cat.seal("table-a", "data", {GEO: cut("g2")}, embargo=True)

        assert cat.refusals({"table-a": leaf}) == {}
        cat.keep("table-a", leaf)
        assert cross_product.stale_editions(cat.full(), cat.read) == []

    def test_a_stale_embargoed_edition_is_refused_at_its_release(self, cat: Catalog) -> None:
        edition, table = cat.seal("table-a", "data", {GEO: cut("g1")}, embargo=True)
        cat.keep("table-a", table)
        _, edit = cat.seal("structure-geo", "structure", {GEO: full("g2")})
        cat.keep("structure-geo", edit)
        (stale,) = cross_product.stale_editions(cat.full(), cat.read)
        assert (stale.edition_id, stale.state) == (edition, "embargoed")

        released = release(table, edition, now=_NOW, explicit=True)

        assert cat.refusals({"table-a": released})["table-a"].rule == "full-artefacts"


def test_a_version_with_no_incoming_entry_reads_no_artefact(cat: Catalog) -> None:
    def unread(*_args: str) -> Mapping[str, PartialRecord]:
        raise AssertionError

    assert cross_product.refusals(cat.full(), {}, unread) == {}


def test_cited_structures_name_the_edition_a_partial_was_cut_from(cat: Catalog) -> None:
    (first,) = [e.edition_id for e in cat.leaves["structure-geo"].editions]
    _, table = cat.seal("table-a", "data", {GEO: cut("g1")})
    cat.keep("table-a", table)
    _, edit = cat.seal("structure-geo", "structure", {GEO: full("g2")})
    cat.keep("structure-geo", edit)

    assert cross_product.cited_structures(cat.full(), cat.read) == {("structure-geo", first)}


class TestHomelessPartials:
    def test_a_partial_cut_from_the_live_home_is_not_listed(self, cat: Catalog) -> None:
        _, table = cat.seal("table-a", "data", {GEO: cut("g1")})
        cat.keep("table-a", table)
        assert cross_product.homeless_partials(cat.full(), cat.read) == []

    def test_a_live_and_an_embargoed_partial_are_listed_once_the_home_drops_the_urn(self, cat: Catalog) -> None:
        live, table = cat.seal("table-a", "data", {GEO: cut("g1")})
        cat.keep("table-a", table)
        embargoed, table = cat.seal("table-a", "data", {GEO: cut("g1")}, embargo=True)
        cat.keep("table-a", table)
        _, edit = cat.seal("structure-geo", "structure", {CS: full("c1")})
        assert cat.refusals({"structure-geo": edit}) == {}
        cat.keep("structure-geo", edit)

        found = cross_product.homeless_partials(cat.full(), cat.read)

        assert [(h.edition_id, h.state, h.urn, h.partial_of) for h in found] == [
            (live, "live", GEO, "g1"),
            (embargoed, "embargoed", GEO, "g1"),
        ]
        assert "its release is refused" in str(found[1])
        assert cross_product.stale_editions(cat.full(), cat.read) == [], "a URN with no home is not stale"
        released = release(table, embargoed, now=_NOW, explicit=True)
        assert "has no structure home" in cat.refusals({"table-a": released})["table-a"].message


class TestRecordsFromLeaves:
    """The rules read the incoming manifest, no manifest of another product's filled entry, and an unfilled one's."""

    @pytest.fixture
    def mixed(self, tmp_path: Path) -> Catalog:
        """structure-geo filled, table-b live and unfilled (recorded before Phase 5), table-c live and filled."""
        c = Catalog(tmp_path / "cat")
        _, geo = c.seal("structure-geo", "structure", {GEO: full("g1")}, filled=True)
        c.keep("structure-geo", geo)
        _, unfilled = c.seal("table-b", "data", {GEO: cut("g1"), DSD: full("d1")})
        c.keep("table-b", unfilled)
        _, filled = c.seal("table-c", "data", {GEO: cut("g1"), DSD: full("d1")}, filled=True)
        c.keep("table-c", filled)
        return c

    def test_a_record_reads_the_incoming_manifest_and_the_unfilled_entrys(self, mixed: Catalog) -> None:
        incoming, leaf = mixed.seal("table-a", "data", {GEO: cut("g1"), DSD: full("d1")}, filled=True)
        unfilled = mixed.leaves["table-b"].editions[0].edition_id
        reads: list[tuple[str, str]] = []

        def recording(product: str, edition: str, sha: str) -> Mapping[str, PartialRecord]:
            reads.append((product, edition))
            return mixed.read(product, edition, sha)

        assert cross_product.refusals(mixed.full(), {"table-a": leaf}, recording) == {}

        assert set(reads) == {("table-a", incoming), ("table-b", unfilled)}

    def test_an_incoming_entrys_records_come_from_its_manifest(self, mixed: Catalog) -> None:
        """The leaf says the partial was cut from g1, and the manifest the reader checks says g0."""
        incoming, leaf = mixed.seal("table-a", "data", {GEO: cut("g1")}, filled=True)
        mixed.held[incoming] = {GEO: cut("g0")}

        refusal = mixed.refusals({"table-a": leaf})["table-a"]

        assert (refusal.rule, refusal.urn) == ("full-artefacts", GEO)

    def test_the_derived_lists_read_no_manifest_of_a_filled_entry(self, cat: Catalog) -> None:
        geo = cat.leaves["structure-geo"]
        filled = record(
            geo,
            product_id="structure-geo",
            kind="structure",
            lifecycle="revisable",
            edition_id=_edition(99),
            previous=geo.editions[0].edition_id,
            manifest_sha256="0" * 64,
            schema_version=SCHEMA_VERSION,
            embargo=False,
            now=_NOW,
            artefacts={GEO: full("g2")},
        )
        cat.keep("structure-geo", filled)
        _, table = cat.seal("table-a", "data", {GEO: cut("g1")}, filled=True)
        cat.keep("table-a", table)
        reads: list[str] = []

        def recording(product: str, edition: str, sha: str) -> Mapping[str, PartialRecord]:
            reads.append(edition)
            return cat.read(product, edition, sha)

        assert [s.urn for s in cross_product.stale_editions(cat.full(), recording)] == [GEO]
        assert cross_product.homeless_partials(cat.full(), recording) == []
        superseded = geo.editions[0].edition_id
        assert cross_product.cited_structures(cat.full(), recording) == {("structure-geo", superseded)}
        assert set(reads) == {superseded}, "only the structure entry recorded before Phase 5 is read"


_SHAPE = CodrShape(geography_codes=6, geography_depth=2, classification_codes=(3, 3, 3), observations=4)


def test_a_seal_reads_no_manifest_of_another_products_filled_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every entry of a catalog sealed from Phase 5 on is filled, so a seal reads its own manifest alone."""
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        testing.codr_structures(cat, seed=1, shape=_SHAPE)
        testing.codr_product(cat, "table-a", seed=1, shape=_SHAPE)
        reads: list[str] = []
        real = cross_product.ManifestArtefacts.__call__

        def recording(
            self: cross_product.ManifestArtefacts, product: str, edition: str, sha: str
        ) -> Mapping[str, PartialRecord]:
            reads.append(product)
            return real(self, product, edition, sha)

        monkeypatch.setattr(cross_product.ManifestArtefacts, "__call__", recording)
        edition = testing.codr_product(cat, "table-b", seed=1, shape=_SHAPE)

    assert edition.product_id == "table-b"
    assert reads, "the commit reads the incoming manifest"
    assert set(reads) == {"table-b"}

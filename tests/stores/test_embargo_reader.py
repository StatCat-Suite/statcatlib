"""`EmbargoReader` over a blob catalog on Azurite (Phase 5 D12, Part 5; Goal 8).

Each store and DuckDB secret names Azurite on the loopback address, with its
well-known key. Part 3's fold is not built, so a fixture seals editions in a local
catalog and records each with `record_uploaded`, as the fold will: under embargo,
each is recorded ``embargoed``. The reader's location carries a `ReadingOps`, so
any write to the store fails the test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import attrs
import pytest
import sdmxlib as sl
from sdmxlib.edition import Report

import statcatlib as sc
from statcatlib import catalog as catalog_module
from statcatlib.admin import CatalogAdmin
from statcatlib.model import InternationalString
from statcatlib.store.blob import BlobLocation
from statcatlib.store.inventory import InventoryWriter
from tests.conftest import sealed_data_edition
from tests.stores import _stores

pytestmark = pytest.mark.stores

_FLOW = sl.Dataflow.urn_for(agency="T", id="DF_F", version="1.0")
_RELEASE = datetime(2099, 1, 1, tzinfo=UTC)


@attrs.frozen
class Sealed:
    """A local catalog: product ``a`` with two editions, the second built on the first, and product ``b``."""

    root: Path
    first: Path
    second: Path
    other: Path


@attrs.frozen
class Placed:
    """The blob catalog: ``a``'s first edition live, and its second sealed under embargo."""

    live: str
    held: str


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> Sealed:
    root = tmp_path_factory.mktemp("embargo") / "catalog"
    first = sealed_data_edition(root, product_id="a")
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("a").working_copy()
        wc.put(sc.OpenDataset(identifier="d2", title=InternationalString(en="Two"), description=InternationalString()))
        second = wc.seal()
    other = sealed_data_edition(root, product_id="b")
    return Sealed(root, first, first.parent / second.edition_id, other)


@pytest.fixture
def placed(writer: InventoryWriter, location: BlobLocation, sealed: Sealed) -> Placed:
    live = _stores.record_uploaded(writer, location, sealed.first)
    writer.try_commit(writer.head(), {}, embargo=True, kind="embargo-change", reason="embargo on")
    held = _stores.record_uploaded(writer, location, sealed.second, release_at=_RELEASE)
    return Placed(live.edition_id, held.edition_id)


@pytest.fixture
def reading(location: BlobLocation, azurite: _stores.Running, tmp_path: Path) -> BlobLocation:
    return _stores.reader(location, azurite, tmp_path / "preview-cache")


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch) -> list[Report]:
    """Each report the shared view's `check_on_open` returned."""
    made: list[Report] = []
    real = catalog_module.check_on_open

    def spy(*args: Any, **kwargs: Any) -> Report:
        report = real(*args, **kwargs)
        made.append(report)
        return report

    monkeypatch.setattr(catalog_module, "check_on_open", spy)
    return made


def _reads(location: BlobLocation) -> _stores.ReadingOps:
    assert isinstance(location.ops, _stores.ReadingOps)
    return location.ops


def _identifiers(view: sc.EditionView) -> set[str]:
    return {d.identifier for d in view.query(sc.OpenDataset).collect()}


class TestViews:
    def test_an_embargoed_and_a_live_edition_read_their_rows(self, reading, placed, sealed, reports) -> None:
        with sc.Catalog.open(sealed.root) as local_cat:
            local_rows = local_cat.product("a").view().registry.obs(_FLOW).frame()

        reader = sc.EmbargoReader.open(reading)
        try:
            assert [(e.product_id, e.edition_id) for e in reader.editions()] == [("a", placed.held)]
            held = reader.view("a", placed.held)
            live = reader.view("a", placed.live)
            assert _identifiers(held) == {"d1", "d2"}
            assert _identifiers(live) == {"d1"}
            held_rows = held.registry.obs(_FLOW).frame()
            assert held_rows.height > 0
            assert held_rows.equals(local_rows)
            assert live.registry.obs(_FLOW).frame().equals(local_rows)
            # A second view of an edition already checked runs `open`, from the reader's own stamps.
            reader.view("a", placed.held)
        finally:
            reader.close()

        assert [report.at for report in reports] == ["receive", "receive", "open"]
        assert all(report.ok for report in reports)
        assert reading.cache_dir is not None
        (cache_root,) = reading.cache_dir.iterdir()
        assert sorted(child.name for child in cache_root.iterdir()) == ["embargo-verified", "products"]
        assert not list(reading.cache_dir.rglob("*.parquet"))

    def test_a_withdrawn_edition_raises_before_any_read_under_it(self, location, reading, placed) -> None:
        admin = CatalogAdmin.open(location, actor="tests", clock=lambda: datetime.now(UTC))
        admin.cancel("a", placed.held, reason="withdraw before release")
        ops = _reads(reading)

        reader = sc.EmbargoReader.open(reading)
        try:
            assert reader.editions() == []
            assert [e.edition_id for e in reader.editions(states=("withdrawn",))] == [placed.held]
            with pytest.raises(ValueError, match="is withdrawn"):
                reader.view("a", placed.held)
        finally:
            reader.close()

        assert ops.under(f"products/a/{placed.held}") == []
        assert reading.cache_dir is not None
        assert not reading.cache_dir.exists() or not list(reading.cache_dir.rglob(placed.held))

    def test_refresh_sees_a_new_fold(self, writer, location, reading, placed, sealed) -> None:
        reader = sc.EmbargoReader.open(reading)
        try:
            assert [e.product_id for e in reader.editions()] == ["a"]
            # Part 3's fold is not built: `record_uploaded` records the upload as the fold will.
            other = _stores.record_uploaded(writer, location, sealed.other, release_at=_RELEASE)
            assert [e.product_id for e in reader.editions()] == ["a"]
            with pytest.raises(sc.InventoryRuleError, match="no product b"):
                reader.view("b", other.edition_id)

            assert reader.refresh() is reader
            listed = reader.editions()
            assert [(e.product_id, e.edition_id, e.state) for e in listed] == [
                ("a", placed.held, "embargoed"),
                ("b", other.edition_id, "embargoed"),
            ]
            assert listed[1].release_at == _RELEASE
            assert _identifiers(reader.view("b", other.edition_id)) == {"d1"}
        finally:
            reader.close()

    def test_the_public_catalog_on_its_own_cache_sees_none_of_it(self, reading, placed, tmp_path) -> None:
        reader = sc.EmbargoReader.open(reading)
        try:
            reader.view("a", placed.held)
        finally:
            reader.close()

        public = attrs.evolve(reading, cache_dir=tmp_path / "public-cache")
        with sc.Catalog.open(public) as cat:
            assert cat.products() == ["a"]
            assert _identifiers(cat.product("a").view()) == {"d1"}
        assert public.cache_dir is not None
        assert not list(public.cache_dir.rglob(placed.held))

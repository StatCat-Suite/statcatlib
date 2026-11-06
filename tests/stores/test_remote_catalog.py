"""`Catalog.open(BlobLocation)` against Azurite: the read path through `obstore` and DuckDB (Phase 4 Part 3, D6).

Each store and DuckDB secret names Azurite on the loopback address, with its
well-known key. Fixtures seal editions in a local catalog and place them with
`record_uploaded`, since Phase 4 has no fold.
"""

from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import attrs
import pytest
import sdmxlib as sl
from sdmxlib.edition import MANIFEST_NAME, Report, read_manifest
from sdmxlib.inventory import CURRENT_NAME, root_path, sha256_hex

import statcatlib as sc
from statcatlib import catalog as catalog_module
from statcatlib.bundle import (
    ROOT_SECTION_FORMAT,
    STATCAT_SECTION,
    check_catalog,
    check_catalog_no_index,
    description_to_json,
)
from statcatlib.model import InternationalString
from statcatlib.model.agents import Agent
from statcatlib.store.blob import BlobLocation, _ObjectOps
from statcatlib.store.store import IncompatibleCatalogError
from tests.conftest import sealed_data_edition, sealed_referring_edition
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


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> Sealed:
    root = tmp_path_factory.mktemp("remote") / "catalog"
    first = sealed_data_edition(root, product_id="a")
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("a").working_copy()
        wc.put(sc.OpenDataset(identifier="d2", title=InternationalString(en="Two"), description=InternationalString()))
        second = wc.seal()
    other = sealed_data_edition(root, product_id="b")
    return Sealed(root, first, first.parent / second.edition_id, other)


@pytest.fixture
def reading(location: BlobLocation, azurite: _stores.Running, tmp_path: Path) -> BlobLocation:
    return _stores.reader(location, azurite, tmp_path / "cache")


def _reads(location: BlobLocation) -> _stores.ReadingOps:
    assert isinstance(location.ops, _stores.ReadingOps)
    return location.ops


def _cache_files(location: BlobLocation) -> list[Path]:
    assert location.cache_dir is not None
    return [path for path in location.cache_dir.rglob("*") if path.is_file()]


def _edition_files(location: BlobLocation) -> list[Path]:
    """The cached edition files, without the receive stamps."""
    return [path for path in _cache_files(location) if "verified" not in path.parts]


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch) -> list[Report]:
    """Each report `Product.view`'s `check_on_open` returned."""
    made: list[Report] = []
    real = catalog_module.check_on_open

    def spy(*args: Any, **kwargs: Any) -> Report:
        report = real(*args, **kwargs)
        made.append(report)
        return report

    monkeypatch.setattr(catalog_module, "check_on_open", spy)
    return made


class TestAWrongManifest:
    def test_it_is_refused_after_reading_only_that_manifest(self, writer, location, reading, sealed) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)
        path = f"products/a/{edition.edition_id}/{MANIFEST_NAME}"
        plain = _ObjectOps(location.store)
        got = plain.read(path)
        assert got is not None
        plain.delete(path)
        plain.put(path, got[0] + b"\n")
        ops = _reads(reading)

        with sc.Catalog.open(reading) as cat:
            ops.reads.clear()
            with pytest.raises(IncompatibleCatalogError, match="SHA-256"):
                cat.product("a").view()
            assert ops.under(f"products/a/{edition.edition_id}/") == [path]
            assert ops.under("products/a/data") == []

        assert _cache_files(reading) == []


class TestTheEmbargo:
    @pytest.fixture
    def placed(self, writer, location, sealed) -> tuple[str, str, str]:
        """Live ``a`` at its first edition; embargoed: ``a``'s second edition and ``b``'s only one."""
        live = _stores.record_uploaded(writer, location, sealed.first)
        head = writer.head()
        writer.try_commit(head, {}, embargo=True, kind="embargo-change", reason="embargo on")
        held = _stores.record_uploaded(writer, location, sealed.second, release_at=_RELEASE)
        other = _stores.record_uploaded(writer, location, sealed.other, release_at=_RELEASE)
        full = writer.full()
        assert full is not None
        assert full.entries("b")[0].state == "embargoed"
        assert {e.edition_id: e.state for e in full.entries("a")} == {
            live.edition_id: "live",
            held.edition_id: "embargoed",
        }
        return live.edition_id, held.edition_id, other.edition_id

    def test_public_apis_never_return_an_embargoed_entry(self, reading, placed) -> None:
        live, held, other = placed

        with sc.Catalog.open(reading) as cat:
            assert cat.products() == ["a"]
            editions = cat.product("a").editions()
            assert [e.edition_id for e in editions] == [live]
            assert held not in repr(editions)
            assert cat.product("b").editions() == []
            with pytest.raises(sc.NoLiveEditionError):
                cat.product("b").view()
            view = cat.product("a").view()
            assert {d.identifier for d in view.query(sc.OpenDataset).collect()} == {"d1"}
            assert other not in repr(view)

    def test_no_read_under_an_embargoed_edition(self, reading, placed) -> None:
        live, held, _other = placed

        with sc.Catalog.open(reading) as cat:
            cat.products()
            cat.product("a").view()
            cat.product("a").editions()
            with pytest.raises(sc.NoLiveEditionError):
                cat.product("b").view()

        ops = _reads(reading)
        assert ops.under(f"products/a/{live}/")
        assert ops.under(f"products/a/{held}") == []
        assert ops.under("products/b") == []

    def test_cache_dir_holds_no_embargoed_edition_or_leaf_bytes(self, reading, placed, sealed) -> None:
        live, held, other = placed

        with sc.Catalog.open(reading) as cat:
            cat.products()
            cat.product("a").view()

        assert reading.cache_dir is not None
        names = {part for path in reading.cache_dir.rglob("*") for part in path.parts}
        assert live in names
        assert held not in names
        assert other not in names
        assert "b" not in names
        assert "inventory" not in names
        for path in _cache_files(reading):
            data = path.read_bytes()
            assert other.encode() not in data, path
            # The first edition's own files cannot name a later edition, and no leaf reaches the disk.
            assert held.encode() not in data, path


class TestTheReadPath:
    def test_rows_through_a_blob_view_equal_the_local_read(self, writer, location, reading, sealed) -> None:
        _stores.record_uploaded(writer, location, sealed.first)
        with sc.Catalog.open(sealed.root) as local_cat:
            # The local catalog's live edition of ``a`` is its second; its observations are the first's.
            local = local_cat.product("a").view()
            local_rows = local.registry.obs(_FLOW).frame()

        with sc.Catalog.open(reading) as cat:
            view = cat.product("a").view()
            rows = view.registry.obs(_FLOW).frame()
            found = {d.identifier for d in view.query(sc.OpenDataset).collect()}

        assert rows.height > 0
        assert rows.equals(local_rows)
        assert found == {"d1"}

    def test_no_data_file_reaches_the_cache(self, writer, location, reading, sealed) -> None:
        _stores.record_uploaded(writer, location, sealed.first)

        with sc.Catalog.open(reading) as cat:
            cat.product("a").view().registry.obs(_FLOW).frame()

        names = {path.suffix for path in _cache_files(reading)}
        assert ".parquet" not in names
        assert reading.cache_dir is not None
        assert not list(reading.cache_dir.rglob("products/a/data"))

    def test_nothing_on_the_wire_writes_to_the_catalog(self, writer, location, azurite, sealed, tmp_path) -> None:
        _stores.record_uploaded(writer, location, sealed.first)
        prefix = location.uri.removeprefix(f"az://{_stores.CONTAINER}/").rstrip("/")

        with _stores.recording_proxy(azurite) as proxy:
            through = _stores.location(azurite, prefix, endpoint=proxy.endpoint)
            opened = attrs.evolve(through, secrets=(_stores.secret(azurite),), cache_dir=tmp_path / "cache")
            with sc.Catalog.open(opened) as cat:
                cat.product("a").view()

        assert proxy.requests
        assert {request.method for request in proxy.requests} <= {"GET", "HEAD"}

    def test_a_uri_with_no_trailing_slash_reads_the_same_rows(self, writer, location, reading, sealed) -> None:
        _stores.record_uploaded(writer, location, sealed.first)
        bare = attrs.evolve(reading, uri=reading.uri.rstrip("/"))

        with sc.Catalog.open(bare) as cat:
            assert cat.product("a").view().registry.obs(_FLOW).frame().height > 0


@pytest.fixture(scope="module")
def referring(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sealed_referring_edition(tmp_path_factory.mktemp("structure") / "catalog")


class TestAStructureProduct:
    def test_external_references_open_and_the_deferred_stamp_lets_the_next_view_run_open(
        self, writer, location, reading, referring, reports
    ) -> None:
        edition = _stores.record_uploaded(writer, location, referring)

        with sc.Catalog.open(reading) as cat:
            cat.product(edition.product_id).view()
        with sc.Catalog.open(reading) as cat:
            cat.product(edition.product_id).view()

        assert [report.at for report in reports] == ["receive", "open"]
        assert all(report.ok for report in reports)
        assert "references" not in reports[0].checked

    def test_a_data_edition_runs_the_reference_check(self, writer, location, reading, sealed, reports) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)

        with sc.Catalog.open(reading) as cat:
            cat.product(edition.product_id).view()

        assert "references" in reports[0].checked


class TestWithNoLancedb:
    def test_a_view_of_an_edition_with_search_opens_through_the_no_index_check(
        self, writer, location, reading, sealed, monkeypatch
    ) -> None:
        _stores.record_uploaded(writer, location, sealed.first)
        monkeypatch.setitem(sys.modules, "lancedb", None)
        checks: list[object] = []
        real = catalog_module.check_on_open

        def spy(*args: Any, **kwargs: Any) -> Report:
            checks.append(kwargs["extensions"][STATCAT_SECTION])
            return real(*args, **kwargs)

        monkeypatch.setattr(catalog_module, "check_on_open", spy)

        with sc.Catalog.open(reading) as cat:
            view = cat.product("a").view()
            assert {d.identifier for d in view.query(sc.OpenDataset).collect()} == {"d1"}

        assert checks == [check_catalog_no_index]
        assert checks[0] is not check_catalog
        cached = read_manifest(next(p.parent for p in _cache_files(reading) if p.name == MANIFEST_NAME))
        assert any(entry.path.startswith("search/") for entry in cached.files)


class TestCaching:
    def test_the_second_view_pulls_nothing_and_runs_open(self, writer, location, reading, sealed, reports) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)
        ops = _reads(reading)

        with sc.Catalog.open(reading) as cat:
            cat.product("a").view()
            first = ops.under(f"products/a/{edition.edition_id}/")
            ops.reads.clear()
            cat.product("a").view()
            within = list(ops.reads)
        with sc.Catalog.open(reading) as cat:
            ops.reads.clear()
            cat.product("a").view()

        assert first[0] == f"products/a/{edition.edition_id}/{MANIFEST_NAME}"
        assert len(first) > 1
        assert within == []
        assert ops.under(f"products/a/{edition.edition_id}/") == []
        assert [report.at for report in reports] == ["receive", "open", "open"]
        assert all(report.ok for report in reports)

    def test_a_cached_view_reads_current_a_root_a_probe_and_one_leaf(self, writer, location, reading, sealed) -> None:
        _stores.record_uploaded(writer, location, sealed.first)
        with sc.Catalog.open(reading) as cat:
            cat.product("a").view()
        ops = _reads(reading)
        ops.reads.clear()

        with sc.Catalog.open(reading) as cat:
            ops.reads.clear()
            cat.refresh()
            refreshed = list(ops.reads)
            ops.reads.clear()
            cat.product("a").view()

        assert refreshed == [CURRENT_NAME, root_path(1), root_path(2)]
        assert [path.split("/")[1] for path in ops.reads] == ["leaves"]

    def test_a_cached_edition_with_another_manifest_names_the_directory_to_delete(
        self, writer, location, reading, sealed
    ) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)
        with sc.Catalog.open(reading) as cat:
            cat.product("a").view()
        manifests = [p for p in _cache_files(reading) if p.name == MANIFEST_NAME]
        assert len(manifests) == 1
        manifests[0].write_bytes(manifests[0].read_bytes() + b"\n")

        with (
            sc.Catalog.open(reading) as cat,
            pytest.raises(IncompatibleCatalogError, match="delete the directory") as e,
        ):
            cat.product("a").view()

        assert edition.edition_id in str(e.value)


class TestThroughTheStore:
    def test_catalog_id_is_read_once_from_the_store(self, writer, reading) -> None:
        ops = _reads(reading)

        with sc.Catalog.open(reading) as cat:
            ops.reads.clear()
            assert cat.catalog_id == writer.catalog_document().catalog_id
            assert ops.reads == []
            assert reading.cache_dir is not None
            assert cat.writable is False

    def test_refresh_sees_a_new_version_and_a_view_stays_on_its_edition(
        self, writer, location, reading, sealed
    ) -> None:
        first = _stores.record_uploaded(writer, location, sealed.first)

        with sc.Catalog.open(reading) as cat:
            assert cat.products() == ["a"]
            view = cat.product("a").view()
            second = _stores.record_uploaded(writer, location, sealed.second)
            assert cat.product("a").editions()[0].edition_id == first.edition_id
            cat.refresh()
            assert cat.product("a").editions()[0].edition_id == second.edition_id
            assert {d.identifier for d in view.query(sc.OpenDataset).collect()} == {"d1"}
            newer = cat.product("a").view()
            assert {d.identifier for d in newer.query(sc.OpenDataset).collect()} == {"d1", "d2"}

    def test_description_reads_the_root_through_the_store(self, writer, reading) -> None:
        described = sc.CatalogDescription(
            identifier="stats",
            title=InternationalString(en="Statistics"),
            description=InternationalString(en="All of them"),
            publisher=Agent(id="agency", name=InternationalString(en="An agency")),
        )
        section = {"format_version": ROOT_SECTION_FORMAT, "description": description_to_json(described)}

        with sc.Catalog.open(reading) as cat:
            assert cat.description is None
            writer.try_commit(writer.head(), {}, extensions={STATCAT_SECTION: section}, kind="describe", reason="x")
            cat.refresh()
            ops = _reads(reading)
            ops.reads.clear()
            assert cat.description == described
            assert ops.reads == [root_path(1)]

    def test_editions_returns_previous_pulls_nothing_and_writes_no_file(
        self, writer, location, reading, sealed, tmp_path, monkeypatch
    ) -> None:
        first = _stores.record_uploaded(writer, location, sealed.first)
        second = _stores.record_uploaded(writer, location, sealed.second)
        ops = _reads(reading)

        def no_temporary(*_: object, **__: object) -> None:
            pytest.fail("editions() made a temporary file")

        monkeypatch.setattr("tempfile.mkstemp", no_temporary)
        monkeypatch.setattr("tempfile.mkdtemp", no_temporary)
        with sc.Catalog.open(reading) as cat:
            ops.reads.clear()
            (edition,) = cat.product("a").editions()

        assert edition.edition_id == second.edition_id
        assert edition.previous == first.edition_id
        assert edition.manifest_sha256 == second.manifest_sha256
        assert ops.under("products/") == [f"products/a/{second.edition_id}/{MANIFEST_NAME}"]
        assert reading.cache_dir is not None
        assert not reading.cache_dir.exists()

    def test_editions_refuses_a_manifest_the_inventory_did_not_record(self, writer, location, reading, sealed) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)
        path = f"products/a/{edition.edition_id}/{MANIFEST_NAME}"
        plain = _ObjectOps(location.store)
        plain.delete(path)
        plain.put(path, b"{}")

        with sc.Catalog.open(reading) as cat, pytest.raises(IncompatibleCatalogError, match="SHA-256"):
            cat.product("a").editions()

    def test_a_missing_catalog_json_raises(self, new_location: Callable[[], BlobLocation], tmp_path) -> None:
        empty = attrs.evolve(new_location(), cache_dir=tmp_path / "cache")

        with pytest.raises(FileNotFoundError, match=r"catalog\.json"):
            sc.Catalog.open(empty)


class TestTheRenameRace:
    def test_two_processes_leave_one_copy(self, writer, location, azurite, sealed, tmp_path) -> None:
        edition = _stores.record_uploaded(writer, location, sealed.first)
        raw = (sealed.first / MANIFEST_NAME).read_bytes()
        assert sha256_hex(raw) == edition.manifest_sha256
        dest = tmp_path / "cache" / "cat" / "products" / "a" / edition.edition_id
        prefix = location.uri.removeprefix(f"az://{_stores.CONTAINER}/").rstrip("/")
        spawn = multiprocessing.get_context("spawn")
        barrier = spawn.Barrier(2)
        args = (azurite, prefix, ("a", edition.edition_id), raw, dest, barrier)
        workers = [spawn.Process(target=_stores.pull_at_the_rename, args=args) for _ in range(2)]

        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(180)

        assert [worker.exitcode for worker in workers] == [0, 0]
        assert (dest / MANIFEST_NAME).read_bytes() == raw
        assert (dest / "metadata.duckdb").is_file()
        assert [path.name for path in dest.parent.iterdir()] == [edition.edition_id]

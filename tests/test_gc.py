"""`collect_garbage` over a `MemoryStore` with a storage clock: every rule of Phase 5 D10 and its safety rules.

Each catalog's objects are written at `GcCatalog.real`, and each run moves the
storage clock: at ``real + 40d`` every object is 40 days old, so ``data/`` files
are past the 24 h grace period and registrations past the 35-day window.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import msgspec
import pytest
from sdmxlib.inventory import (
    CATALOG_NAME,
    HISTORY_NAME,
    CatalogHistory,
    History,
    decode_catalog,
    encode,
    root_path,
)

from statcatlib.gc import GcStoppedError, _delete
from statcatlib.jobs import _EVENT_ATTRIBUTE, JobsConfigError
from tests._gc import GcCatalog, edition_id, policy

pytestmark = pytest.mark.unit

_DAY = timedelta(days=1)


@pytest.fixture
def cat() -> GcCatalog:
    return GcCatalog()


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, object]]:
    """The events named *name* the run logged, in order."""
    found = [getattr(record, _EVENT_ATTRIBUTE, None) for record in caplog.records]
    return [event for event in found if isinstance(event, dict) and event.get("event") == name]


def _detail(event: dict[str, object]) -> dict[str, object]:
    detail = event["detail"]
    assert isinstance(detail, dict)
    return detail


def _deleted(cat: GcCatalog) -> list[str]:
    return cat.ops.called("delete")


def _two_editions(cat: GcCatalog) -> tuple[str, str]:
    """Product ``p``: edition A recorded at ``real - 60d``, B on it at ``real - 50d``, A retired at ``real - 40d``.

    A lists ``old.parquet`` and ``shared.parquet``; B lists ``shared.parquet`` and ``new.parquet``.
    Two later versions follow, so with ``keep_roots = 2`` the roots read hold no entry of A.
    """
    real = cat.real
    a, b = edition_id(1, real - 60 * _DAY), edition_id(2, real - 50 * _DAY)
    cat.upload("p", a, data=["old.parquet", "shared.parquet"])
    cat.upload("p", b, data=["shared.parquet", "new.parquet"])
    cat.record("p", a, real - 60 * _DAY)
    cat.record("p", b, real - 50 * _DAY)
    cat.retire("p", a, real - 40 * _DAY)
    cat.touch(real - 39 * _DAY)
    cat.touch(real - 38 * _DAY)
    return a, b


class TestTheRules:
    def test_a_retired_edition_and_its_unreferenced_files_go_and_the_live_ones_stay(self, cat: GcCatalog) -> None:
        a, b = _two_editions(cat)
        cat.put("products/p/data/orphan.parquet")

        report = cat.gc(cat.real + 2 * _DAY)

        assert set(report.deleted) == {
            f"products/p/{a}/manifest.json",
            f"products/p/{a}/metadata.duckdb",
            "products/p/data/old.parquet",
            "products/p/data/orphan.parquet",
        }
        assert {d.reason for d in report.planned if d.path.startswith(f"products/p/{a}/")} == {"retired"}
        assert cat.paths() == {
            f"products/p/{b}/manifest.json",
            f"products/p/{b}/metadata.duckdb",
            "products/p/data/shared.parquet",
            "products/p/data/new.parquet",
        }
        assert report.failed == {}
        assert report.failed_closed == {}

    def test_a_second_run_deletes_nothing(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat.put("products/p/data/orphan.parquet")
        cat.gc(cat.real + 2 * _DAY)
        cat.ops.calls.clear()

        report = cat.gc(cat.real + 2 * _DAY)

        assert report.planned == ()
        assert _deleted(cat) == []

    def test_a_retired_directory_loses_its_manifest_first(self, cat: GcCatalog) -> None:
        a, _ = _two_editions(cat)
        cat.put(f"products/p/{a}/search/index.json")

        cat.gc(cat.real + 2 * _DAY)

        own = [path for path in _deleted(cat) if path.startswith(f"products/p/{a}/")]
        assert own[0] == f"products/p/{a}/manifest.json"
        assert set(own[1:]) == {f"products/p/{a}/metadata.duckdb", f"products/p/{a}/search/index.json"}

    def test_a_failed_manifest_deletion_leaves_the_rest_of_the_directory(self, cat: GcCatalog) -> None:
        a, _ = _two_editions(cat)
        cat.ops.fail.add(f"products/p/{a}/manifest.json")

        report = cat.gc(cat.real + 2 * _DAY)

        assert set(report.failed) == {f"products/p/{a}/manifest.json"}
        assert cat.has(f"products/p/{a}/metadata.duckdb")
        assert f"products/p/{a}/metadata.duckdb" not in _deleted(cat)

    def test_a_later_run_finishes_a_partly_deleted_directory(self, cat: GcCatalog) -> None:
        a, _ = _two_editions(cat)
        cat._plain.ops.delete(f"products/p/{a}/manifest.json")

        report = cat.gc(cat.real + 2 * _DAY)

        assert f"products/p/{a}/metadata.duckdb" in report.deleted

    def test_a_tombstone_within_the_window_keeps_its_directory_and_files(self, cat: GcCatalog) -> None:
        a, _ = _two_editions(cat)

        # At real + 2d the cutoff is real - 33d: A's tombstone (real - 40d) is older. At real - 10d it is not.
        report = cat.gc(cat.real - 10 * _DAY, policy(upload_grace=timedelta(seconds=1)))

        assert not [path for path in report.deleted if a in path or "old.parquet" in path]
        assert cat.has(f"products/p/{a}/manifest.json")
        assert cat.has("products/p/data/old.parquet")

    def test_a_tombstone_in_the_window_with_no_entry_read_fails_its_product_closed(self, cat: GcCatalog) -> None:
        """Expiry took the roots that record A, so GC keeps A by its tombstone, and cannot verify its manifest."""
        real = cat.real
        a, b = edition_id(1, real - 60 * _DAY), edition_id(2, real - 50 * _DAY)
        cat.upload("p", a, data=["old.parquet"])
        cat.upload("p", b, data=["new.parquet"])
        cat.record("p", a, real - 60 * _DAY)
        cat.record("p", b, real - 50 * _DAY)
        cat.retire("p", a, real + 10 * _DAY)
        cat.touch(real + 11 * _DAY)
        _opt_in(cat, expired_through=2)
        for seq in (1, 2):
            cat._plain.ops.delete(root_path(seq))

        report = cat.gc(real + 40 * _DAY)

        assert a in report.failed_closed["p"]
        assert report.deleted == ()
        assert cat.has("products/p/data/old.parquet")
        assert cat.has(f"products/p/{a}/manifest.json")

    def test_a_data_file_inside_the_grace_period_stays(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat.put("products/p/data/racing.parquet")

        report = cat.gc(cat.real + timedelta(hours=23))

        assert "products/p/data/racing.parquet" not in report.deleted
        assert cat.has("products/p/data/racing.parquet")


class TestKeepRoots:
    def test_a_catalog_with_fewer_roots_in_the_window_keeps_what_the_newest_ten_name(self, cat: GcCatalog) -> None:
        """Every root is older than the cutoff, so GC reads the newest ``keep_roots`` of them.

        A is in roots 1 and 2 only; B, retired at version 5, is in roots 2 to 4. With
        the default ten roots read, B's entry is in a root read, and B stays.
        """
        real = cat.real
        a, b, c = (edition_id(n, real - (90 - n) * _DAY) for n in (1, 2, 3))
        for edition in (a, b, c):
            cat.upload("p", edition, data=[f"{edition}.parquet"])
        cat.record("p", a, real - 90 * _DAY)
        cat.record("p", b, real - 89 * _DAY)
        cat.retire("p", a, real - 88 * _DAY)
        cat.record("p", c, real - 87 * _DAY)
        cat.retire("p", b, real - 86 * _DAY)
        for n in range(8):
            cat.touch(real - (85 - n) * _DAY)
        # 13 versions: the newest ten are 4 to 13. Root 4 records B superseded.

        ten = cat.gc(cat.real + 2 * _DAY, policy(keep_roots=10), dry_run=True)
        two = cat.gc(cat.real + 2 * _DAY, policy(keep_roots=2), dry_run=True)

        assert {d.edition for d in ten.planned if d.reason == "retired"} == {f"p/{a}"}
        assert {d.edition for d in two.planned if d.reason == "retired"} == {f"p/{a}", f"p/{b}"}
        assert f"products/p/data/{b}.parquet" not in {d.path for d in ten.planned}
        assert f"products/p/data/{b}.parquet" in {d.path for d in two.planned}

    def test_the_walk_reads_back_to_the_newest_root_before_the_cutoff(self, cat: GcCatalog) -> None:
        real = cat.real
        a, b = edition_id(1, real - 60 * _DAY), edition_id(2, real - 50 * _DAY)
        cat.upload("p", a, data=["a.parquet"])
        cat.upload("p", b, data=["b.parquet"])
        cat.record("p", a, real - 60 * _DAY)
        cat.record("p", b, real - 50 * _DAY)
        cat.retire("p", a, real - 40 * _DAY)
        for n in range(5):
            cat.touch(real - (20 - n) * _DAY)
        # The cutoff is real - 33d. Roots 4 to 8 are inside the window, root 3 (the retire) is
        # the newest before it, and root 2, which records A, is not read.

        report = cat.gc(cat.real + 2 * _DAY, policy(keep_roots=2))

        assert root_path(3) in cat.ops.called("read")
        assert root_path(2) not in cat.ops.called("read")
        assert f"products/p/{a}/manifest.json" in report.deleted


def _opt_in(cat: GcCatalog, expired_through: int) -> None:
    """Rewrite ``catalog.json`` as 2.0 and write ``inventory/HISTORY``, as expiry leaves them."""
    stored = cat._plain.ops.read(CATALOG_NAME)
    assert stored is not None
    catalog = decode_catalog(stored[0])
    opted = msgspec.structs.replace(catalog, format_version="2.0", history=CatalogHistory(since_seq=1))
    cat.put(CATALOG_NAME, encode(opted))
    history = History(
        format_version="1.0",
        catalog_id=catalog.catalog_id,
        restore_epoch=0,
        expired_through=expired_through,
        newest_segment=None,
        newest_segment_sha256=None,
        cleaned_through=None,
    )
    cat.put(HISTORY_NAME, encode(history))


class TestStops:
    def test_a_missing_root_above_expired_through_deletes_nothing(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat.put("products/p/data/orphan.parquet")
        _opt_in(cat, expired_through=1)
        cat._plain.ops.delete(root_path(4))

        with pytest.raises(
            GcStoppedError, match=r"inventory/000000000004\.json is missing, above expired_through \(1\)"
        ):
            cat.gc(cat.real + 2 * _DAY, policy(keep_roots=10))

        assert _deleted(cat) == []
        assert cat.has("products/p/data/orphan.parquet")

    def test_the_walk_stops_at_expired_through(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        _opt_in(cat, expired_through=3)
        for seq in (1, 2, 3):
            cat._plain.ops.delete(root_path(seq))

        report = cat.gc(cat.real + 2 * _DAY, policy(keep_roots=10))

        assert report.failed == {}
        assert not {root_path(seq) for seq in (1, 2, 3)} & set(cat.ops.called("read"))

    def test_a_missing_root_on_a_catalog_that_has_not_opted_in_deletes_nothing(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat._plain.ops.delete(root_path(3))

        with pytest.raises(GcStoppedError, match="above expired_through"):
            cat.gc(cat.real + 2 * _DAY, policy(keep_roots=10))

        assert _deleted(cat) == []

    def test_a_missing_leaf_deletes_nothing(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        stored = cat._plain.ops.read(root_path(4))
        assert stored is not None
        root = msgspec.json.decode(stored[0])
        cat._plain.ops.delete(root["products"]["p"]["leaf"])
        cat.touch(cat.real - 37 * _DAY)
        cat.touch(cat.real - 36 * _DAY)

        with pytest.raises(GcStoppedError, match="is missing"):
            cat.gc(cat.real + 2 * _DAY, policy(keep_roots=10))

        assert _deleted(cat) == []

    def test_a_root_the_reader_skips_deletes_nothing(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat.put(root_path(6), b"{}")

        with pytest.raises(GcStoppedError, match="the reader skipped"):
            cat.gc(cat.real + 2 * _DAY)

        assert _deleted(cat) == []

    def test_a_catalog_with_no_catalog_json_deletes_nothing(self, cat: GcCatalog) -> None:
        cat._plain.ops.delete(CATALOG_NAME)
        cat.put("products/p/data/orphan.parquet")

        with pytest.raises(GcStoppedError, match=r"there is no catalog\.json"):
            cat.gc(cat.real + 2 * _DAY)

        assert _deleted(cat) == []

    def test_backup_retention_longer_than_the_gc_window_refuses_to_start(self, cat: GcCatalog) -> None:
        _two_editions(cat)

        with pytest.raises(JobsConfigError, match=r"backup \(40d\) must not exceed gc_window \(35d\)"):
            cat.gc(cat.real + 2 * _DAY, policy(backup=timedelta(days=40)))

        assert cat.ops.calls == []

    def test_gc_refuses_to_delete_outside_products_and_pending(self, cat: GcCatalog) -> None:
        for path in (root_path(1), "history/0/x.jsonl", CATALOG_NAME, "CURRENT"):
            with pytest.raises(ValueError, match="products/ and pending/ only"):
                _delete(cat.location, path)

        assert _deleted(cat) == []


class TestFailClosed:
    @pytest.mark.parametrize("problem", ["missing", "another edition"])
    def test_a_registration_whose_manifest_cannot_be_verified_keeps_the_products_data(
        self, cat: GcCatalog, caplog: pytest.LogCaptureFixture, problem: str
    ) -> None:
        e = edition_id(7, cat.real)
        if problem == "missing":
            cat.upload("p", e, data=["listed.parquet"], manifest=False)
        else:
            cat.upload("p", e, data=["listed.parquet"], named=("p", edition_id(8, cat.real)))
        cat.register("p", e)
        cat.put("products/p/data/orphan.parquet")
        cat.put("products/q/data/orphan.parquet")
        cat.touch(cat.real)

        with caplog.at_level(logging.INFO, logger="statcatlib.gc"):
            report = cat.gc(cat.real + 2 * _DAY)

        assert report.deleted == ("products/q/data/orphan.parquet",)
        assert not [path for path in _deleted(cat) if path.startswith("products/p/")]
        assert cat.has("products/p/data/orphan.parquet")
        assert e in report.failed_closed["p"]
        [event] = _events(caplog, "gc.failed_closed")
        assert (event["product"], event["edition"], event["severity"]) == ("p", e, "warning")

    def test_a_kept_edition_whose_manifest_differs_from_the_inventory_keeps_the_products_data(
        self, cat: GcCatalog
    ) -> None:
        _two_editions(cat)
        cat.put("products/p/data/orphan.parquet")
        b = cat.leaves["p"].editions[-1].edition_id
        cat.put(f"products/p/{b}/manifest.json", b"{}")

        report = cat.gc(cat.real + 2 * _DAY)

        assert "SHA-256" in report.failed_closed["p"]
        assert not [path for path in report.deleted if "/data/" in path]

    def test_an_unreadable_registration_keeps_the_products_data(self, cat: GcCatalog) -> None:
        e = edition_id(7, cat.real)
        cat.upload("p", e, data=["listed.parquet"])
        cat.put(f"pending/p/{e}.json", b'{"edition_id": "x"}')
        cat.touch(cat.real)

        report = cat.gc(cat.real + 2 * _DAY)

        assert "cannot be read" in report.failed_closed["p"]
        assert report.deleted == ()


class TestRegistrations:
    def test_a_registered_editions_files_stay_and_without_its_registration_they_go(self, cat: GcCatalog) -> None:
        e = edition_id(7, cat.real)
        cat.upload("p", e, data=["listed.parquet"])
        path = cat.register("p", e)
        cat.touch(cat.real)

        kept = cat.gc(cat.real + 2 * _DAY)

        assert kept.planned == ()
        cat._plain.ops.delete(path)
        gone = cat.gc(cat.real + 2 * _DAY)
        assert gone.deleted == ("products/p/data/listed.parquet",)
        assert gone.unregistered == (f"p/{e}",)
        assert cat.has(f"products/p/{e}/manifest.json")

    def test_folded_registrations_past_the_window_go_and_newer_ones_stay(self, cat: GcCatalog) -> None:
        a, b = _two_editions(cat)
        folded = [cat.register("p", a), cat.register("p", b)]

        inside = cat.gc(cat.real + 2 * _DAY)
        past = cat.gc(cat.real + 40 * _DAY)

        assert not set(folded) & {d.path for d in inside.planned}
        assert {d.path: d.reason for d in past.planned if d.path.startswith("pending/")} == dict.fromkeys(
            folded, "folded"
        )
        assert not set(folded) & cat.paths()

    def test_a_refused_registration_stays_until_the_tombstone_window(self, cat: GcCatalog) -> None:
        fresh = edition_id(7, cat.real - 10 * _DAY)
        old = edition_id(8, cat.real - 401 * _DAY)
        cat.upload("p", fresh, data=["fresh.parquet"])
        cat.upload("p", old, data=["old.parquet"])
        registrations = {cat.register("p", fresh), cat.register("p", old)}
        cat.touch(cat.real)

        report = cat.gc(cat.real + 40 * _DAY)

        assert {d.path: d.reason for d in report.planned if d.path.startswith("pending/")} == {
            f"pending/p/{old}.json": "expired-registration"
        }
        assert f"pending/p/{fresh}.json" in cat.paths() & registrations
        # The expired registration protects no file; the other one protects its own.
        assert "products/p/data/old.parquet" in report.deleted
        assert cat.has("products/p/data/fresh.parquet")
        assert report.unregistered == (f"p/{old}",)


class TestDiscard:
    def test_an_unregistered_edition_is_reported_and_kept_with_or_without_a_manifest(
        self, cat: GcCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        with_manifest, without = edition_id(1, cat.real), edition_id(2, cat.real)
        cat.upload("p", with_manifest)
        cat.upload("p", without, manifest=False)
        cat.touch(cat.real)

        with caplog.at_level(logging.INFO, logger="statcatlib.gc"):
            report = cat.gc(cat.real + 2 * _DAY)

        assert report.unregistered == tuple(sorted((f"p/{with_manifest}", f"p/{without}")))
        assert report.planned == ()
        found = {event["edition"]: _detail(event)["manifest"] for event in _events(caplog, "gc.unregistered")}
        assert found == {with_manifest: True, without: False}
        assert cat.has(f"products/p/{without}/metadata.duckdb")

    def test_discard_deletes_the_registration_then_the_manifest_then_the_directory(self, cat: GcCatalog) -> None:
        e = edition_id(7, cat.real)
        cat.upload("p", e, data=["only.parquet"], own=("metadata.duckdb", "search/index.json"))
        registration = cat.register("p", e)
        cat.touch(cat.real)

        report = cat.gc(cat.real + 2 * _DAY, discard=[f"p/{e}"])

        own = [path for path in _deleted(cat) if e in path]
        assert own[:2] == [registration, f"products/p/{e}/manifest.json"]
        assert set(own[2:]) == {f"products/p/{e}/metadata.duckdb", f"products/p/{e}/search/index.json"}
        # Its data file falls to the normal rule in the same run, as no manifest lists it now.
        assert "products/p/data/only.parquet" in report.deleted
        assert {d.reason for d in report.planned if d.edition == f"p/{e}"} == {"discarded"}
        assert cat.paths() == set()

    def test_a_failed_registration_deletion_leaves_the_edition_whole(self, cat: GcCatalog) -> None:
        e = edition_id(7, cat.real)
        cat.upload("p", e)
        registration = cat.register("p", e)
        cat.touch(cat.real)
        cat.ops.fail.add(registration)

        report = cat.gc(cat.real + 2 * _DAY, discard=[f"p/{e}"])

        assert set(report.failed) == {registration}
        assert _deleted(cat) == [registration]
        assert cat.has(f"products/p/{e}/manifest.json")

    @pytest.mark.parametrize("which", ["live", "superseded", "tombstoned"])
    def test_discard_of_an_edition_the_inventory_records_refuses_and_deletes_nothing(
        self, cat: GcCatalog, which: str
    ) -> None:
        a, b = _two_editions(cat)
        c = edition_id(3, cat.real - 37 * _DAY)
        cat.upload("p", c)
        cat.record("p", c, cat.real - 37 * _DAY)
        named = {"live": c, "superseded": b, "tombstoned": a}[which]

        with pytest.raises(ValueError, match="statcat admin cancel"):
            cat.gc(cat.real + 2 * _DAY, discard=[f"p/{named}"])

        assert _deleted(cat) == []

    @pytest.mark.parametrize("item", ["p", "p/e/x", "/e", "p/.."])
    def test_a_discard_that_names_no_edition_is_refused(self, cat: GcCatalog, item: str) -> None:
        with pytest.raises(ValueError, match="<product>/<edition>"):
            cat.gc(cat.real, discard=[item])
        assert cat.ops.calls == []


class TestDryRunAndForecast:
    def test_a_dry_run_logs_the_plan_and_deletes_nothing(
        self, cat: GcCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        a, _ = _two_editions(cat)
        before = cat.paths()

        with caplog.at_level(logging.INFO, logger="statcatlib.gc"):
            report = cat.gc(cat.real + 2 * _DAY, dry_run=True)

        assert report.dry_run
        assert report.deleted == ()
        assert _deleted(cat) == []
        assert cat.paths() == before
        lines = _events(caplog, "gc.planned")
        assert [line["path"] for line in lines] == [d.path for d in report.planned]
        assert lines[0] == {
            "event": "gc.planned",
            "path": "products/p/data/old.parquet",
            "reason": "unreferenced",
            "edition": None,
        }
        assert {"event": "gc.planned", "path": f"products/p/{a}/manifest.json", "reason": "retired"} | {
            "edition": f"p/{a}"
        } in lines

    def test_a_forecast_deletes_the_intersection_and_reports_each_sides_extra(
        self, cat: GcCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        a, _ = _two_editions(cat)
        plan = cat.gc(cat.real + 2 * _DAY, dry_run=True)
        forecast = {d.path for d in plan.planned} - {"products/p/data/old.parquet"}
        forecast.add("products/p/data/gone-meanwhile.parquet")

        with caplog.at_level(logging.INFO, logger="statcatlib.gc"):
            report = cat.gc(cat.real + 2 * _DAY, forecast=forecast)

        assert report.deviations == {
            "products/p/data/gone-meanwhile.parquet": "forecast",
            "products/p/data/old.parquet": "plan",
        }
        assert set(report.deleted) == {f"products/p/{a}/manifest.json", f"products/p/{a}/metadata.duckdb"}
        assert cat.has("products/p/data/old.parquet")
        assert {_detail(event)["path"] for event in _events(caplog, "gc.forecast_deviation")} == set(report.deviations)

    def test_a_forecast_without_a_directorys_manifest_keeps_the_whole_directory(self, cat: GcCatalog) -> None:
        a, _ = _two_editions(cat)

        report = cat.gc(cat.real + 2 * _DAY, forecast={f"products/p/{a}/metadata.duckdb"})

        assert report.deleted == ()
        assert cat.has(f"products/p/{a}/metadata.duckdb")

    def test_nothing_under_the_inventory_or_history_is_touched(self, cat: GcCatalog) -> None:
        _two_editions(cat)
        cat.put("history/0/000000000001-000000000002-x.jsonl")
        cat.put("products/p/data/orphan.parquet")
        cat.register("p", cat.leaves["p"].editions[-1].edition_id)

        cat.gc(cat.real + 40 * _DAY)

        assert cat.ops.called("put") == []
        assert cat.ops.called("delete")
        assert all(path.startswith(("products/", "pending/")) for path in _deleted(cat))
        assert sorted(cat.ops.called("list")) == ["pending", "products"]
        assert not [path for path in cat.ops.called("read") if path.startswith("history/")]
        assert cat.has("history/0/000000000001-000000000002-x.jsonl")

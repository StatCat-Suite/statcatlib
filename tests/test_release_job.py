"""`ReleaseJob.run` over a `MemoryStore`: its steps, their isolation, the access check and retirement (D1, D8, D9).

The listing is a `FakeListing`, except where `AzureReaderListing` and the storage
clock are answered by `pytest-httpx`, which fails any request it was given no
answer for. No test here reaches Azure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import attrs
import msgspec
import pytest
from obstore.store import AzureStore
from pytest_httpx import HTTPXMock
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    decode_catalog,
    decode_current,
    decode_root,
    encode,
    root_path,
    sha256_hex,
)

from statcatlib.access import AzureReaderListing
from statcatlib.jobs import _EVENT_ATTRIBUTE, JobsConfig, JobsConfigError
from statcatlib.release import ReleaseJob, ReleaseReport
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, BlobStoreError
from statcatlib.store.inventory import InventoryWriteRefusedError
from tests._fakes import ACCOUNT, SENTINEL, grant
from tests._release import PODS, RELEASE_JOB, URI, ReleaseCatalog, artefact, config

pytestmark = pytest.mark.unit

_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=T:CL_GEO(1.0)"
_ENDPOINT = "https://acct.blob.core.windows.net"
_ASSIGNMENTS = (
    f"https://management.azure.com{ACCOUNT}/blobServices/default/containers/catalogs/providers/"
    "Microsoft.Authorization/roleAssignments?%24filter=atScope%28%29&api-version=2022-04-01"
)


@pytest.fixture(autouse=True)
def _offline(httpx_mock: HTTPXMock) -> None:
    """`pytest-httpx` answers every HTTP request in this module, and fails one it was given no answer for."""
    del httpx_mock


@pytest.fixture
def cat() -> ReleaseCatalog:
    return ReleaseCatalog()


def _events(caplog: pytest.LogCaptureFixture, name: str | None = None) -> list[dict[str, Any]]:
    """The events the runs logged, in order; those named *name* when given."""
    found = [getattr(record, _EVENT_ATTRIBUTE, None) for record in caplog.records]
    return [event for event in found if isinstance(event, dict) and name in {None, event.get("event")}]


def _rules(report: ReleaseReport) -> dict[str, str | None]:
    return {f"{r.product_id}/{r.edition_id}": r.rule for r in report.refused}


def _azure_location(cat: ReleaseCatalog) -> BlobLocation:
    """The catalog behind an `AzureStore` that sends no request: every store call goes to the `MemoryStore`."""
    offline = AzureStore("catalogs", prefix="stc", account_name="acct", skip_signature=True)
    return BlobLocation(URI, offline, ops=cat.ops)


class TestStart:
    def test_a_clock_ahead_of_the_storage_clock_stops_the_run_before_any_write(
        self, cat: ReleaseCatalog, httpx_mock: HTTPXMock
    ) -> None:
        cat.upload("p")
        behind = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)
        httpx_mock.add_response(method="HEAD", url=_ENDPOINT, headers={"Date": behind})
        location = _azure_location(cat)
        job = ReleaseJob(location, config(), rest=cat.rest, listing=cat.listing)

        with pytest.raises(InventoryWriteRefusedError, match="ahead of the storage clock"):
            job.run()

        assert cat.ops.called("put") == []
        assert cat.listing.calls == 0
        assert not cat.writer.has_version()

    def test_the_storage_clock_is_the_time_each_version_records(
        self, cat: ReleaseCatalog, httpx_mock: HTTPXMock
    ) -> None:
        cat.upload("p")
        ahead = (datetime.now(UTC) + timedelta(minutes=3)).replace(microsecond=0)
        httpx_mock.add_response(
            method="HEAD", url=_ENDPOINT, headers={"Date": format_datetime(ahead, usegmt=True)}, is_reusable=True
        )
        location = _azure_location(cat)
        storage = BlobInventoryStorage(location, actor="release")

        report = ReleaseJob(location, config(), rest=cat.rest, listing=cat.listing, storage=storage).run()

        assert report.versions == (1,)
        root = cat.full().root
        assert root.created_at == ahead
        assert cat.full().entries("p")[0].state_changed_at == ahead

    def test_an_unreadable_storage_clock_stops_the_run(self, cat: ReleaseCatalog, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="HEAD", url=_ENDPOINT)
        job = ReleaseJob(_azure_location(cat), config(), rest=cat.rest, listing=cat.listing)

        with pytest.raises(InventoryWriteRefusedError, match="no readable Date header"):
            job.run()

    def test_an_opted_in_catalog_is_refused_naming_part_6(self, cat: ReleaseCatalog) -> None:
        cat.upload("p")
        document = msgspec.json.decode(cat.read(CATALOG_NAME))
        document |= {"format_version": "2.0", "history": {"since_seq": 1}}
        cat.put(CATALOG_NAME, msgspec.json.encode(document))
        assert decode_catalog(cat.read(CATALOG_NAME)).opted_in
        cat.ops.calls.clear()

        with pytest.raises(InventoryWriteRefusedError, match=r"opted in to expiry.*Part 6"):
            cat.run()

        assert cat.ops.called("put") == []
        assert cat.listing.calls == 0

    def test_a_store_with_no_catalog_raises(self, cat: ReleaseCatalog) -> None:
        cat.delete(CATALOG_NAME)

        with pytest.raises(FileNotFoundError, match=r"catalog\.json"):
            cat.run()

    def test_a_configuration_with_no_access_section_is_refused(self, cat: ReleaseCatalog) -> None:
        jobs = attrs.evolve(config(), access=None)

        with pytest.raises(JobsConfigError, match=r"\[access\] is missing"):
            cat.job(jobs)

    def test_an_access_section_for_another_container_is_refused(self, cat: ReleaseCatalog) -> None:
        base = config()
        assert base.access is not None
        jobs = attrs.evolve(base, access=attrs.evolve(base.access, container="other"))

        with pytest.raises(JobsConfigError, match=r"access\.container is 'other'"):
            ReleaseJob(_azure_location(cat), jobs, rest=cat.rest, listing=cat.listing)

    def test_an_access_section_for_another_account_is_refused(self, cat: ReleaseCatalog) -> None:
        base = config()
        assert base.access is not None
        other = attrs.evolve(base.access, storage_account=ACCOUNT.replace("acct", "other"))

        with pytest.raises(JobsConfigError, match="names the account 'other'"):
            ReleaseJob(_azure_location(cat), attrs.evolve(base, access=other), rest=cat.rest, listing=cat.listing)

    def test_the_account_is_compared_without_regard_to_case(self, cat: ReleaseCatalog) -> None:
        base = config()
        assert base.access is not None
        jobs = attrs.evolve(base, access=attrs.evolve(base.access, storage_account=ACCOUNT.replace("acct", "ACCT")))

        assert ReleaseJob(_azure_location(cat), jobs, rest=cat.rest, listing=cat.listing) is not None


class TestAdvance:
    def _lag(self, cat: ReleaseCatalog) -> None:
        """Two versions, with ``CURRENT`` naming the first."""
        cat.live("p")
        assert cat.seq() == 2
        first = cat.read(root_path(1))
        cat.put(CURRENT_NAME, encode(Current(seq=1, root_sha256=sha256_hex(first), restore_epoch=0)))

    def test_a_lagging_current_is_advanced_and_logged(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._lag(cat)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        assert decode_current(cat.read(CURRENT_NAME)).seq == 2
        assert report.versions == ()
        [behind] = _events(caplog, "current.behind")
        assert set(behind) == {"event", "severity", "at", "catalog", "detail"}
        assert (behind["severity"], behind["detail"]) == ("warning", {"seq": 2, "advanced": True})

    def test_a_dry_run_writes_nothing_and_leaves_current_where_it_is(self, cat: ReleaseCatalog) -> None:
        self._lag(cat)
        first = cat.states("p")
        second = cat.upload("p", previous=next(iter(first)), release_at=cat.now)
        bad = cat.upload("bad", previous=cat.new_id())
        cat.ops.calls.clear()
        cat.listing.calls = 0

        report = cat.run(dry_run=True)

        assert report.dry_run
        assert report.folded == (f"p/{second}",)
        assert _rules(report) == {f"bad/{bad}": "chain"}
        assert report.versions == ()
        assert cat.ops.called("put") == []
        assert cat.ops.called("delete") == []
        assert decode_current(cat.read(CURRENT_NAME)).seq == 1
        assert cat.seq() == 2
        assert cat.listing.calls == 1

    def test_a_dry_run_plans_the_release_and_the_retirement_on_the_head(self, cat: ReleaseCatalog) -> None:
        first = cat.live("p")
        second = cat.upload("p", previous=first, release_at=cat.now + _HOUR)
        cat.run()
        cat.run(at=cat.now + 2 * _HOUR)
        third = cat.upload("p", previous=second, release_at=cat.now + _HOUR)
        cat.run()
        seq = cat.seq()

        report = cat.run(at=cat.now + 91 * _DAY, dry_run=True)

        assert report.released == (f"p/{third}",)
        assert report.retired == (f"p/{first}",)
        assert cat.seq() == seq
        assert cat.states("p") == {first: "superseded", second: "live", third: "embargoed"}


class TestAccess:
    def _due(self, cat: ReleaseCatalog) -> tuple[str, str]:
        """An embargoed edition that is due at `ReleaseCatalog.now`, and a registration the fold refuses."""
        at = cat.now + _HOUR
        due = cat.upload("due", release_at=at)
        cat.run()
        cat.now = at + timedelta(seconds=5)
        return due, cat.upload("bad", previous=cat.new_id())

    def _skipped(self, cat: ReleaseCatalog, due: str, bad: str, report: ReleaseReport, new: str) -> None:
        """The release was skipped, and the fold ran and its refusals are recorded."""
        assert report.released == ()
        assert "access check failed" in report.skipped["release"]
        assert not report.access.passed
        assert cat.states("due") == {due: "embargoed"}
        assert report.folded == (f"new/{new}",)
        assert cat.states("new") == {new: "embargoed"}
        assert _rules(report) == {f"bad/{bad}": "chain"}
        listed = cat.record()["refused"]
        assert isinstance(listed, list)
        assert [item["edition_id"] for item in listed] == [bad]
        assert report.failed == {}

    def test_a_principal_outside_the_allowlist_skips_the_release(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        due, bad = self._due(cat)
        new = cat.upload("new")
        outsider = grant("oid-analyst", principal_type="User")
        cat.listing.grants = (grant(RELEASE_JOB), grant(PODS), outsider)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        self._skipped(cat, due, bad, report, new)
        assert report.access.outsiders == (outsider,)
        [mismatch] = _events(caplog, "access.mismatch")
        assert set(mismatch) == {"event", "severity", "at", "catalog", "reason", "detail"}
        assert mismatch["severity"] == "error"
        assert mismatch["detail"] == {"outsiders": [attrs.asdict(outsider)]}
        assert "oid-analyst" in mismatch["reason"]
        assert _events(caplog, "access.unchecked") == []
        [overdue] = _events(caplog, "release.overdue")
        assert overdue["edition"] == due

    def test_a_group_principal_not_on_the_list_skips_the_release(self, cat: ReleaseCatalog) -> None:
        due, bad = self._due(cat)
        new = cat.upload("new")
        group = grant("oid-analysts-group", principal_type="Group")
        cat.listing.grants = (grant(RELEASE_JOB), group)

        report = cat.run()

        self._skipped(cat, due, bad, report, new)
        assert report.access.outsiders == (group,)
        assert report.access.unused == (PODS,)

    def test_a_listing_error_skips_the_release(self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture) -> None:
        due, bad = self._due(cat)
        new = cat.upload("new")
        cat.listing.error = "the role assignments: ConnectTimeout"

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        self._skipped(cat, due, bad, report, new)
        assert report.access.errors == ("the role assignments: ConnectTimeout",)
        [unchecked] = _events(caplog, "access.unchecked")
        assert unchecked["detail"] == {"errors": ["the role assignments: ConnectTimeout"], "missing_sentinels": []}
        assert _events(caplog, "access.mismatch") == []

    def test_a_403_from_resource_manager_skips_the_release(self, cat: ReleaseCatalog, httpx_mock: HTTPXMock) -> None:
        due, bad = self._due(cat)
        new = cat.upload("new")
        httpx_mock.add_response(url=_ASSIGNMENTS, status_code=403)
        real = AzureReaderListing(ACCOUNT, "catalogs", arm_token=lambda: "arm-token")
        job = ReleaseJob(
            cat.location, config(), rest=cat.rest, listing=real, storage=cat.storage("release", plain=False)
        )

        report = job.run()

        self._skipped(cat, due, bad, report, new)
        assert "HTTP 403" in report.access.errors[0]

    def test_a_missing_sentinel_skips_the_release(self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture) -> None:
        due, bad = self._due(cat)
        new = cat.upload("new")
        cat.listing.sentinels = ()

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        self._skipped(cat, due, bad, report, new)
        assert report.access.missing_sentinels == (SENTINEL,)
        [unchecked] = _events(caplog, "access.unchecked")
        assert unchecked["detail"] == {"errors": [], "missing_sentinels": [SENTINEL]}

    def test_a_passed_check_releases_and_reports_unused_principals(self, cat: ReleaseCatalog) -> None:
        due, _ = self._due(cat)
        cat.listing.grants = (grant(RELEASE_JOB),)

        report = cat.run()

        assert report.access.passed
        assert report.access.unused == (PODS,)
        assert report.released == (f"due/{due}",)
        assert report.skipped == {}

    def test_retirement_still_runs_under_a_failed_check(self, cat: ReleaseCatalog) -> None:
        first = cat.live("p")
        cat.live("p", previous=first)
        cat.listing.error = "no listing"

        report = cat.run(at=cat.now + 91 * _DAY)

        assert report.retired == (f"p/{first}",)

    def test_an_error_that_is_no_listing_error_stops_the_run(self, cat: ReleaseCatalog) -> None:
        class Broken:
            def readers(self) -> Any:
                msg = "no credential"
                raise PermissionError(msg)

        job = ReleaseJob(cat.location, config(), rest=cat.rest, listing=Broken(), storage=cat.storage("release"))

        with pytest.raises(PermissionError, match="no credential"):
            job.run()


class TestOneJobTwoRuns:
    def test_a_second_run_of_one_job_reads_the_store_afresh(self, cat: ReleaseCatalog) -> None:
        job = cat.job()
        first = cat.upload("p")
        assert job.run().folded == (f"p/{first}",)
        second = cat.upload("q")

        report = job.run()

        assert report.folded == (f"q/{second}",)


class TestUnprepared:
    def test_a_due_registration_is_folded_and_released_in_one_run_in_two_versions(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        due = cat.upload("p", release_at=cat.now)
        later = cat.upload("q", release_at=cat.now + _DAY)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        assert report.folded == (f"p/{due}", f"q/{later}")
        assert report.released == (f"p/{due}",)
        assert report.versions == (1, 2)
        assert [decode_root_kind(cat, seq) for seq in (1, 2)] == ["seal", "release"]
        [unprepared] = _events(caplog, "release.unprepared")
        assert set(unprepared) == {"event", "severity", "at", "catalog", "product", "edition"}
        assert (unprepared["product"], unprepared["edition"], unprepared["severity"]) == ("p", due, "warning")

    def test_an_edition_folded_by_an_earlier_run_is_released_with_no_such_event(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        cat.upload("p", release_at=cat.now + _HOUR)
        cat.run()

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run(at=cat.now + 2 * _HOUR)

        assert len(report.released) == 1
        assert _events(caplog) == []


def decode_root_kind(cat: ReleaseCatalog, seq: int) -> str:
    """The ``event.kind`` of version *seq*."""
    return decode_root(cat.read(root_path(seq))).event.kind


class TestIsolation:
    def test_a_failed_fold_leaves_the_release_and_the_retirement_to_run(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = cat.live("p")
        second = cat.live("p", previous=first)
        # The pre-flight run, an hour earlier, is inside the retire window; this run is past it.
        at = cat.now + 90 * _DAY + timedelta(minutes=30)
        third = cat.upload("p", cat.new_id(at - _DAY), previous=second, release_at=at)
        standing = cat.upload("bad", cat.new_id(at - _DAY), previous=cat.new_id())
        cat.run(at=at - _HOUR)
        since = cat.now
        cat.upload("new", cat.new_id(at - _DAY))
        real = cat.ops.list

        def failing(prefix: str) -> Any:
            if prefix == "pending":
                msg = "list pending: ServerBusy"
                raise BlobStoreError(msg, path=prefix)
            return real(prefix)

        monkeypatch.setattr(cat.ops, "list", failing)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run(at=at + timedelta(seconds=5))

        assert set(report.failed) == {"fold"}
        assert "ServerBusy" in report.failed["fold"]
        assert report.folded == ()
        assert report.released == (f"p/{third}",)
        assert report.retired == (f"p/{first}",)
        assert cat.states("new") == {}
        [failed] = _events(caplog, "step.failed")
        assert set(failed) == {"event", "severity", "at", "catalog", "detail"}
        assert failed["severity"] == "error"
        assert failed["detail"] == {"step": "fold", "error": report.failed["fold"]}
        # The fold judged nothing, so the record keeps the head's fold refusals and their `since`.
        listed = cat.record()["refused"]
        assert isinstance(listed, list)
        assert [(item["edition_id"], item["since"]) for item in listed] == [
            (standing, since.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        ]
        assert report.refused == ()

    def test_a_failed_release_leaves_the_retirement_to_run(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = cat.live("p")
        second = cat.live("p", previous=first)
        # The pre-flight run, an hour earlier, is inside the retire window; this run is past it.
        at = cat.now + 90 * _DAY + timedelta(minutes=30)
        cat.upload("p", cat.new_id(at - _DAY), previous=second, release_at=at)
        cat.run(at=at - _HOUR)
        job = cat.job()
        original = job._writer.try_commit

        def refusing(*args: Any, **kwargs: Any) -> Any:
            if kwargs["kind"] == "release":
                msg = "write the root: ServerBusy"
                raise BlobStoreError(msg, path="inventory/")
            return original(*args, **kwargs)

        monkeypatch.setattr(job._writer, "try_commit", refusing)
        cat.now = at + timedelta(seconds=5)

        report = job.run()

        assert set(report.failed) == {"release"}
        assert report.released == ()
        assert report.retired == (f"p/{first}",)

    def test_a_current_that_cannot_move_fails_its_step_alone(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat.live("p")
        first = cat.read(root_path(1))
        cat.put(CURRENT_NAME, encode(Current(seq=1, root_sha256=sha256_hex(first), restore_epoch=0)))
        new = cat.upload("new")
        job = cat.job()
        monkeypatch.setattr(job._storage, "advance_current", lambda _root, _data: None)

        report = job.run()

        assert set(report.failed) == {"advance"}
        assert "did not move" in report.failed["advance"]
        assert report.folded == (f"new/{new}",)


class TestRetire:
    def test_entries_older_than_retire_after_are_tombstoned_in_one_retire_version(self, cat: ReleaseCatalog) -> None:
        first = cat.live("p")
        second = cat.live("p", previous=first)
        cancelled = cat.upload("q")
        cat.run()
        cat.admin().cancel("q", cancelled, reason="tests")
        superseded_at = cat.now

        early = cat.run(at=superseded_at + 89 * _DAY)
        assert (early.retired, early.versions) == ((), ())

        report = cat.run(at=superseded_at + 91 * _DAY)

        assert report.retired == (f"p/{first}", f"q/{cancelled}")
        assert len(report.versions) == 1
        root = cat.full().root
        assert root.event.kind == "retire"
        assert root.event.reason == "retire entries superseded or withdrawn for more than 90d"
        assert {(stone.product_id, stone.edition_id, stone.retired_at) for stone in root.retired} == {
            ("p", first, cat.now),
            ("q", cancelled, cat.now),
        }
        assert cat.states("p") == {second: "live"}
        assert cat.run().versions == ()

    def test_a_structure_edition_a_live_data_edition_cites_is_kept(self, cat: ReleaseCatalog) -> None:
        home = cat.live("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        cat.live("data", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])
        edit = cat.live("home", previous=home, kind="structure", artefacts=[artefact(_CODELIST, "b" * 64)])
        spare = cat.live("spare")
        cat.live("spare", previous=spare)

        report = cat.run(at=cat.now + 91 * _DAY)

        assert report.retired == (f"spare/{spare}",)
        assert cat.states("home") == {home: "superseded", edit: "live"}

    def test_the_window_comes_from_the_configuration(self, cat: ReleaseCatalog) -> None:
        first = cat.live("p")
        cat.live("p", previous=first)
        jobs: JobsConfig = config(retire_after=timedelta(days=2))

        assert cat.run(at=cat.now + _DAY, jobs=jobs).retired == ()
        assert cat.run(at=cat.now + 2 * _DAY, jobs=jobs).retired == (f"p/{first}",)

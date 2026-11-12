"""The release job's release step and its refusal record over a `MemoryStore` (Phase 5 D4 and D5)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import msgspec
import pytest
from pytest_httpx import HTTPXMock

from statcatlib.jobs import _EVENT_ATTRIBUTE
from statcatlib.release import (
    MAX_REFUSALS,
    ReleaseRefusal,
    ReleaseReport,
    current_record,
    current_refusals,
    drop_until_stable,
    refusal_record,
)
from tests._fakes import grant
from tests._release import PODS, RELEASE_JOB, ReleaseCatalog, artefact

pytestmark = pytest.mark.unit

_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=T:CL_GEO(1.0)"
_DSD = "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=T:DSD_SHARED(1.0)"


@pytest.fixture(autouse=True)
def _offline(httpx_mock: HTTPXMock) -> None:
    """`pytest-httpx` answers every HTTP request in this module, and fails one it was given no answer for."""
    del httpx_mock


@pytest.fixture
def cat() -> ReleaseCatalog:
    return ReleaseCatalog()


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """The events named *name* the runs logged, in order."""
    found = [getattr(record, _EVENT_ATTRIBUTE, None) for record in caplog.records]
    return [event for event in found if isinstance(event, dict) and event.get("event") == name]


def _rules(report: ReleaseReport) -> dict[str, tuple[str, str | None]]:
    """Each refused edition's step and rule, by ``<product>/<edition>``."""
    return {f"{r.product_id}/{r.edition_id}": (r.step, r.rule) for r in report.refused}


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _recorded(cat: ReleaseCatalog) -> list[dict[str, Any]]:
    """The refusals the head's record holds, as JSON objects."""
    listed = cat.record()["refused"]
    assert isinstance(listed, list)
    return listed


class TestRelease:
    def test_every_due_entry_goes_live_in_one_release_version(self, cat: ReleaseCatalog) -> None:
        at = cat.now + _HOUR
        editions = {name: cat.upload(name, release_at=at) for name in ("a", "b", "c")}
        later = cat.upload("later", release_at=at + _DAY)
        unscheduled = cat.upload("unscheduled")
        assert cat.run().versions == (1,)

        report = cat.run(at=at + timedelta(seconds=5))

        assert report.released == tuple(f"{name}/{edition}" for name, edition in editions.items())
        assert report.versions == (2,)
        root = cat.full().root
        assert (root.event.kind, root.event.reason) == ("release", f"release_due at {cat.now:%Y-%m-%dT%H:%M:%SZ}")
        assert root.created_at == cat.now
        assert all(cat.states(name) == {edition: "live"} for name, edition in editions.items())
        assert cat.entry("a", editions["a"]).state_changed_at == cat.now
        assert cat.states("later") == {later: "embargoed"}
        assert cat.states("unscheduled") == {unscheduled: "embargoed"}

    def test_a_release_supersedes_the_live_edition_it_was_built_on(self, cat: ReleaseCatalog) -> None:
        first = cat.live("p")
        second = cat.upload("p", previous=first, release_at=cat.now + _HOUR)
        cat.run()

        report = cat.run(at=cat.now + 2 * _HOUR)

        assert report.released == (f"p/{second}",)
        assert cat.states("p") == {first: "superseded", second: "live"}

    def test_a_second_run_writes_nothing(self, cat: ReleaseCatalog) -> None:
        cat.live("p")
        cat.ops.calls.clear()

        report = cat.run()

        assert (report.released, report.versions) == ((), ())
        assert cat.ops.called("put") == []

    def test_a_non_fast_forward_edition_is_refused_naming_both_and_the_rest_go_live(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = cat.live("p")
        second = cat.live("p", previous=first)
        at = cat.now + _HOUR
        third = cat.upload("p", previous=second, release_at=at)
        other = cat.upload("q", release_at=at)
        cat.run()
        cat.admin().rollback("p", reason="jdoe: a bad release")
        assert cat.states("p") == {first: "live", second: "withdrawn", third: "embargoed"}

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run(at=at + timedelta(seconds=5))

        assert report.released == (f"q/{other}",)
        assert _rules(report) == {f"p/{third}": ("release", None)}
        reason = report.refused[0].reason
        assert second in reason
        assert first in reason
        assert cat.states("p")[third] == "embargoed"
        assert report.failed == {}
        [refused] = _events(caplog, "release.refused")
        assert (refused["product"], refused["edition"], refused["rule"]) == ("p", third, None)
        assert refused["reason"] == reason
        [overdue] = _events(caplog, "release.overdue")
        assert (overdue["product"], overdue["edition"], overdue["detail"]) == ("p", third, {"release_at": _stamp(at)})
        assert _recorded(cat) == [
            {
                "product_id": "p",
                "edition_id": third,
                "step": "release",
                "rule": None,
                "reason": reason,
                "since": _stamp(cat.now),
            }
        ]

    def test_a_released_entry_whose_manifest_changed_after_the_fold_is_dropped_with_manifest(
        self, cat: ReleaseCatalog
    ) -> None:
        at = cat.now + _HOUR
        changed = cat.upload("changed", release_at=at)
        good = cat.upload("good", release_at=at)
        cat.run()
        cat.put(cat.manifest_path("changed", changed), b'{"format_version": "3.0"}')

        report = cat.run(at=at + timedelta(seconds=5))

        assert report.released == (f"good/{good}",)
        assert _rules(report) == {f"changed/{changed}": ("release", "manifest")}
        assert "SHA-256" in report.refused[0].reason
        assert cat.states("changed") == {changed: "embargoed"}

    def test_a_removed_manifest_drops_the_release_with_manifest(self, cat: ReleaseCatalog) -> None:
        at = cat.now + _HOUR
        gone = cat.upload("gone", release_at=at)
        cat.run()
        cat.delete(cat.manifest_path("gone", gone))

        report = cat.run(at=at + timedelta(seconds=5))

        assert _rules(report) == {f"gone/{gone}": ("release", "manifest")}
        assert report.released == ()


class TestPublishOrder:
    def _edited(self, cat: ReleaseCatalog, *, structure_at: datetime, data_at: datetime) -> tuple[str, str]:
        """A structure edition that edits the codelist, and a data edition cut from the edit, both folded."""
        home = cat.live("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        data = cat.live("data", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])
        edit = cat.upload(
            "home", previous=home, kind="structure", artefacts=[artefact(_CODELIST, "b" * 64)], release_at=structure_at
        )
        cut = cat.upload(
            "data", previous=data, artefacts=[artefact(_CODELIST, "2" * 64, partial_of="b" * 64)], release_at=data_at
        )
        report = cat.run()
        assert report.folded == (f"data/{cut}", f"home/{edit}")
        return edit, cut

    def test_a_data_edition_whose_structure_edition_is_not_due_stays_embargoed(self, cat: ReleaseCatalog) -> None:
        early, late = cat.now + _HOUR, cat.now + 2 * _HOUR
        edit, cut = self._edited(cat, structure_at=late, data_at=early)

        report = cat.run(at=early + timedelta(seconds=5))

        assert report.released == ()
        assert _rules(report) == {f"data/{cut}": ("release", "publish-order")}
        assert edit in report.refused[0].reason
        assert cat.states("data")[cut] == "embargoed"

        both = cat.run(at=late + timedelta(seconds=5))

        assert both.released == (f"data/{cut}", f"home/{edit}")
        assert both.refused == ()
        assert len(both.versions) == 1
        assert _recorded(cat) == []

    def test_both_due_release_together_in_one_version(self, cat: ReleaseCatalog) -> None:
        at = cat.now + _HOUR
        edit, cut = self._edited(cat, structure_at=at, data_at=at)

        report = cat.run(at=at + timedelta(seconds=5))

        assert report.released == (f"data/{cut}", f"home/{edit}")
        assert len(report.versions) == 1

    def test_products_sharing_a_changed_artefact_with_no_home_release_together_or_not_at_all(
        self, cat: ReleaseCatalog
    ) -> None:
        one = cat.live("one", artefacts=[artefact(_DSD, "a" * 64)])
        two = cat.live("two", artefacts=[artefact(_DSD, "a" * 64)])
        at = cat.now + _HOUR
        changed = cat.upload("one", previous=one, artefacts=[artefact(_DSD, "b" * 64)], release_at=at)
        cat.run()

        alone = cat.run(at=at + timedelta(seconds=5))

        assert _rules(alone) == {f"one/{changed}": ("release", "full-artefacts")}
        partner = cat.upload("two", previous=two, artefacts=[artefact(_DSD, "b" * 64)], release_at=cat.now)

        together = cat.run(at=cat.now + _HOUR)

        assert together.released == (f"one/{changed}", f"two/{partner}")
        assert together.refused == ()


class TestHeld:
    def test_an_embargo_turned_on_during_the_commit_turns_the_retry_into_a_held_run(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        at = cat.now + _HOUR
        edition = cat.upload("p", release_at=at)
        cat.run()
        cat.now = at + timedelta(seconds=5)
        job = cat.job()
        original = job._writer.try_commit
        raced: list[str] = []

        def racing(*args: Any, **kwargs: Any) -> Any:
            if not raced:
                raced.append(kwargs["kind"])
                cat.admin().set_embargo(on=True, reason="jdoe: stop releases")
            return original(*args, **kwargs)

        monkeypatch.setattr(job._writer, "try_commit", racing)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = job.run()

        assert raced == ["release"]
        assert report.released == ()
        assert report.versions == ()
        assert cat.states("p") == {edition: "embargoed"}
        assert cat.full().root.event.kind == "embargo-change"
        assert "embargo is on" in report.skipped["release"]
        [held] = _events(caplog, "release.held")
        assert (held["severity"], held["detail"]) == ("warning", {"due": 1})
        [overdue] = _events(caplog, "release.overdue")
        assert overdue["edition"] == edition

    def test_embargo_on_holds_every_automatic_release_and_an_explicit_one_still_releases(
        self, cat: ReleaseCatalog
    ) -> None:
        at = cat.now + _HOUR
        edition = cat.upload("p", release_at=at)
        cat.run()
        cat.admin().set_embargo(on=True, reason="jdoe: stop releases")

        report = cat.run(at=at + _HOUR)

        assert (report.released, report.versions) == ((), ())
        assert cat.states("p") == {edition: "embargoed"}

        cat.admin().release("p", edition, reason="jdoe: release by hand")
        assert cat.states("p") == {edition: "live"}

        cat.admin().set_embargo(on=False, reason="jdoe: resume")
        again = cat.upload("p", previous=edition, release_at=cat.now)
        assert cat.run(at=cat.now + _HOUR).released == (f"p/{again}",)

    def test_the_fold_still_records_embargoed_under_a_hold(self, cat: ReleaseCatalog) -> None:
        cat.live("earlier")
        cat.admin().set_embargo(on=True, reason="jdoe: stop releases")
        edition = cat.upload("p", release_at=cat.now)

        report = cat.run()

        assert report.folded == (f"p/{edition}",)
        assert report.released == ()
        assert cat.states("p") == {edition: "embargoed"}


class TestTheRefusalRecord:
    def test_it_is_written_with_a_release_in_the_same_version(self, cat: ReleaseCatalog) -> None:
        at = cat.now + _HOUR
        good = cat.upload("good", release_at=at)
        cat.run()
        bad = cat.upload("bad", previous=cat.new_id())

        report = cat.run(at=at + timedelta(seconds=5))

        assert report.released == (f"good/{good}",)
        assert report.versions == (2,)
        root = cat.full().root
        assert root.event.kind == "release"
        assert [item["edition_id"] for item in _recorded(cat)] == [bad]

    def test_it_is_written_alone_with_kind_release_and_empty_changes(self, cat: ReleaseCatalog) -> None:
        cat.live("earlier")
        bad = cat.upload("bad", previous=cat.new_id())

        report = cat.run()

        assert len(report.versions) == 1
        root = cat.full().root
        assert (root.event.kind, root.changes) == ("release", ())
        assert cat.record() == {
            "refused": [
                {
                    "product_id": "bad",
                    "edition_id": bad,
                    "step": "fold",
                    "rule": "chain",
                    "reason": report.refused[0].reason,
                    "since": _stamp(cat.now),
                }
            ],
            "omitted": 0,
            "epoch_started_at": None,
            "restore_epoch": 0,
        }
        assert cat.full().root.extensions["statcat"]["format_version"] == "3"

    def test_a_rerun_with_the_same_refusals_writes_nothing_and_since_is_carried(self, cat: ReleaseCatalog) -> None:
        first_run = cat.now
        bad = cat.upload("bad", previous=cat.new_id())
        cat.run()
        seq = cat.seq()

        rerun = cat.run(at=first_run + _HOUR)

        assert rerun.versions == ()
        assert cat.seq() == seq
        assert [r.since for r in rerun.refused] == [first_run]

        worse = cat.upload("worse", previous=cat.new_id())
        third = cat.run(at=first_run + 2 * _HOUR)

        assert len(third.versions) == 1
        since = {r.edition_id: r.since for r in current_refusals(cat.full())}
        assert since == {bad: first_run, worse: first_run + 2 * _HOUR}

    def test_a_refusal_that_ends_leaves_the_record(self, cat: ReleaseCatalog) -> None:
        missing = cat.new_id()
        bad = cat.upload("p", previous=missing)
        cat.run()
        assert len(_recorded(cat)) == 1
        cat.delete(f"pending/p/{bad}.json")

        report = cat.run()

        assert report.refused == ()
        assert len(report.versions) == 1
        assert _recorded(cat) == []
        assert cat.run().versions == ()

    def test_it_is_written_under_a_failed_access_check_and_the_release_is_skipped(self, cat: ReleaseCatalog) -> None:
        at = cat.now + _HOUR
        due = cat.upload("due", release_at=at)
        cat.run()
        bad = cat.upload("bad", previous=cat.new_id())
        cat.listing.grants = (grant(RELEASE_JOB), grant(PODS), grant("oid-analyst"))

        report = cat.run(at=at + _HOUR)

        assert report.released == ()
        assert "access check failed" in report.skipped["release"]
        assert cat.states("due") == {due: "embargoed"}
        assert cat.full().root.event.kind == "release"
        assert [item["edition_id"] for item in _recorded(cat)] == [bad]

    def test_the_description_and_other_sections_are_kept(self, cat: ReleaseCatalog) -> None:
        cat.live("earlier")
        head = cat.writer.head()
        assert head is not None
        extensions = {"statcat": {"format_version": "3", "description": {"identifier": "c"}}, "other": {"k": 1}}
        cat.writer.try_commit(head, {}, extensions=extensions, kind="describe", reason="tests")
        cat.upload("bad", previous=cat.new_id())

        cat.run()

        held = cat.full().root.extensions
        assert held["other"] == {"k": 1}
        assert held["statcat"]["description"] == {"identifier": "c"}
        assert set(held["statcat"]) == {"format_version", "description", "release"}

    def test_the_cap_keeps_the_oldest_since_and_counts_the_rest(self, cat: ReleaseCatalog) -> None:
        first_run = cat.now
        standing = cat.upload("zzz", previous=cat.new_id())
        cat.run()
        later = first_run + _DAY
        fresh = [ReleaseRefusal(f"p{n:04d}", "e", "fold", "chain", "refused", later) for n in range(MAX_REFUSALS + 5)]
        old = current_refusals(cat.full())[0]
        assert old.edition_id == standing

        extensions = refusal_record(cat.full(), [*fresh, old], later)

        record = extensions["statcat"]["release"]
        assert isinstance(record, dict)
        assert record["omitted"] == 6
        kept = record["refused"]
        assert len(kept) == MAX_REFUSALS
        assert kept[-1]["product_id"] == "zzz"
        assert kept[-1]["since"] == _stamp(first_run)
        assert [item["product_id"] for item in kept[:-1]] == [f"p{n:04d}" for n in range(MAX_REFUSALS - 1)]

    def test_no_refusal_on_a_head_with_no_record_changes_nothing(self, cat: ReleaseCatalog) -> None:
        cat.live("p")

        assert refusal_record(cat.full(), [], cat.now) == current_record(cat.full())
        assert "release" not in cat.full().root.extensions.get("statcat", {})
        assert refusal_record(None, [], cat.now) == {}

    def test_a_record_this_build_cannot_read_is_replaced(self, cat: ReleaseCatalog) -> None:
        cat.live("earlier")
        head = cat.writer.head()
        assert head is not None
        broken = {"statcat": {"format_version": "3", "release": {"refused": [{"product_id": 7}, "x"], "omitted": "?"}}}
        cat.writer.try_commit(head, {}, extensions=broken, kind="release", reason="tests")
        assert current_refusals(cat.full()) == ()

        report = cat.run()

        assert len(report.versions) == 1
        assert cat.record() == {"refused": [], "omitted": 0, "epoch_started_at": None, "restore_epoch": 0}


class TestDropUntilStable:
    def test_a_dropped_edition_takes_the_one_accepted_with_it(self, cat: ReleaseCatalog) -> None:
        home = cat.upload("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        data = cat.upload("data", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])
        other = cat.upload("other")
        cat.run()
        full = cat.full()
        leaves = dict(full.leaves())
        cat.delete(cat.manifest_path("home", home))
        planned = {
            name: msgspec.structs.replace(
                leaf, editions=tuple(msgspec.structs.replace(e, state="live", release_at=None) for e in leaf.editions)
            )
            for name, leaf in leaves.items()
        }

        kept, dropped = drop_until_stable(cat.job()._writer, full, planned)

        assert sorted(kept) == ["other"]
        assert (dropped["home"].edition_id, dropped["home"].rule) == (home, "manifest")
        # With home dropped, its edition stays embargoed in the head, so the data edition would go live first.
        assert (dropped["data"].edition_id, dropped["data"].rule) == (data, "publish-order")
        assert other in {entry.edition_id for entry in kept["other"].editions}

    def test_an_unreadable_manifest_of_another_products_unfilled_entry_fails_the_step(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        home = cat.upload("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        cat.run()
        leaf = cat.full().leaf("home")
        unfilled = msgspec.structs.replace(
            leaf, editions=tuple(msgspec.structs.replace(entry, artefacts=None) for entry in leaf.editions)
        )
        head = cat.writer.head()
        assert head is not None
        cat.writer.try_commit(head, {"home": unfilled}, kind="correct", reason="tests: an entry from before 0.17")
        cat.delete(cat.manifest_path("home", home))
        cat.upload("data", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        assert "ManifestError" in report.failed["fold"]
        assert cat.states("data") == {}
        [failed] = _events(caplog, "step.failed")
        assert (failed["severity"], failed["detail"]["step"]) == ("error", "fold")
        assert "release" not in report.failed
        assert "retire" not in report.failed

"""The release job's fold over a `MemoryStore` with a storage clock: every rule of Phase 5 D2 and D3.

Each edition is a manifest with one binding and its listed files
(`tests/_release.py`), so no reference check runs; the stores tier runs that
check on real editions (`tests/stores/test_release_job.py`).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import msgspec
import pytest
from pytest_httpx import HTTPXMock
from sdmxlib.inventory import CATALOG_NAME, PartialRecord, decode_catalog, encode, root_path

from statcatlib.jobs import _EVENT_ATTRIBUTE
from statcatlib.release import ReleaseJob, ReleaseReport
from statcatlib.store.blob import BlobStoreError
from tests._release import ReleaseCatalog, artefact

pytestmark = pytest.mark.unit

_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=T:CL_GEO(1.0)"


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


def _rules(report: ReleaseReport) -> dict[str, str | None]:
    """Each refused edition's rule, by ``<product>/<edition>``."""
    return {f"{r.product_id}/{r.edition_id}": r.rule for r in report.refused}


class TestRecording:
    def test_a_registration_is_recorded_embargoed_in_one_seal_version_with_its_routing_fields(
        self, cat: ReleaseCatalog
    ) -> None:
        release_at = cat.now + _HOUR
        records = [artefact(_CODELIST, "a" * 64)]
        a = cat.upload("a", release_at=release_at, artefacts=records)
        b = cat.upload("b", lifecycle="final")

        report = cat.run()

        assert report.folded == (f"a/{a}", f"b/{b}")
        assert report.versions == (1,)
        assert report.refused == ()
        root = cat.full().root
        assert (root.seq, root.event.kind, root.event.reason) == (1, "seal", "fold of pending/ registrations")
        assert root.event.actor == "release"
        entry = cat.entry("a", a)
        assert (entry.state, entry.release_at, entry.previous) == ("embargoed", release_at, None)
        assert entry.manifest_sha256 == cat.manifest_sha("a", a)
        assert entry.artefacts == {_CODELIST: PartialRecord(fingerprint="a" * 64)}
        assert entry.dataflows == ("urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=T:DF_A(1.0)",)
        assert isinstance(entry.pull_bytes, int)
        assert entry.pull_bytes > 0
        assert cat.entry("b", b).release_at is None
        assert (cat.full().leaf("a").lifecycle, cat.full().leaf("b").lifecycle) == ("revisable", "final")

    def test_a_second_run_reads_no_recorded_registration_and_writes_nothing(self, cat: ReleaseCatalog) -> None:
        a = cat.upload("a")
        cat.run()
        cat.ops.calls.clear()

        report = cat.run()

        assert (report.folded, report.versions) == ((), ())
        assert cat.ops.called("put") == []
        assert f"pending/a/{a}.json" not in cat.ops.called("read")
        assert cat.ops.called("list") == ["pending"]

    def test_the_fold_writes_and_deletes_nothing_under_pending_or_products(self, cat: ReleaseCatalog) -> None:
        cat.upload("a")

        cat.run()

        written = cat.ops.called("put")
        assert written
        assert all(path == "CURRENT" or path.startswith("inventory/") for path in written)
        assert cat.ops.called("delete") == []

    def test_an_object_under_pending_that_is_no_registration_is_left_alone(self, cat: ReleaseCatalog) -> None:
        cat.put("pending/README.txt")
        cat.put("pending/a/b/c.json")

        report = cat.run()

        assert (report.folded, report.refused, report.versions) == ((), (), ())

    def test_a_tombstoned_edition_counts_as_recorded(self, cat: ReleaseCatalog) -> None:
        a = cat.upload("a")
        cat.run()
        cat.admin().cancel("a", a, reason="tests")
        cat.run(at=cat.now + 91 * _DAY)
        assert cat.states("a") == {}

        report = cat.run()

        assert (report.folded, report.refused, report.versions) == ((), (), ())


class TestRefusals:
    def test_each_bad_registration_is_refused_and_the_rest_are_recorded(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        good = cat.upload("good")
        missing = cat.upload("missing-file")
        cat.delete(f"products/missing-file/data/{missing}.parquet")
        form = cat.upload("form", canonical_form="other-form")
        chain = cat.upload("chain", previous=cat.new_id())
        document = cat.upload("document", register=False)
        cat.put(f"pending/document/{document}.json", b'{"edition_id": "x"}')
        no_manifest = cat.new_id()
        cat.register("no-manifest", no_manifest)
        renamed = cat.upload("renamed")
        cat.put(cat.manifest_path("renamed", renamed), cat.read(cat.manifest_path("good", good)))
        fault = cat.upload("fault")
        cat.rest.fail.add(f"products/fault/{fault}/metadata.duckdb")
        no_kind = cat.upload("no-kind", kind=None)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        assert report.folded == (f"good/{good}",)
        assert _rules(report) == {
            f"missing-file/{missing}": "files",
            f"form/{form}": "canonical-form",
            f"chain/{chain}": "chain",
            f"document/{document}": "pending-document",
            f"no-manifest/{no_manifest}": "manifest",
            f"renamed/{renamed}": "manifest",
            f"fault/{fault}": "store",
            f"no-kind/{no_kind}": "kind",
        }
        assert report.failed == {}
        assert sorted(cat.full().products()) == ["good"]
        refused = _events(caplog, "fold.refused")
        assert len(refused) == 8
        files = next(event for event in refused if event["rule"] == "files")
        assert set(files) == {"event", "severity", "at", "catalog", "product", "edition", "rule", "reason"}
        assert (files["severity"], files["catalog"], files["product"]) == (
            "warning",
            "az://catalogs/stc/",
            "missing-file",
        )
        assert "missing from the store" in files["reason"]

    def test_an_edition_with_no_binding_that_lists_data_files_is_refused_naming_the_secret(
        self, cat: ReleaseCatalog
    ) -> None:
        unbound = cat.upload("p", bound=False)
        bound = cat.upload("q")

        report = cat.run()

        assert _rules(report) == {f"p/{unbound}": "references"}
        assert "StoreSecret" in report.refused[0].reason
        assert report.folded == (f"q/{bound}",)

    def test_another_kind_than_the_leafs_is_refused(self, cat: ReleaseCatalog) -> None:
        cat.live("p")
        other = cat.upload("p", kind="structure", bound=False)

        report = cat.run()

        assert _rules(report) == {f"p/{other}": "kind"}

    def test_one_older_than_the_tombstone_window_is_refused(self, cat: ReleaseCatalog) -> None:
        old = cat.upload("p", cat.new_id(cat.real - 401 * _DAY))
        inside = cat.upload("q", cat.new_id(cat.real - 399 * _DAY))

        report = cat.run()

        assert _rules(report) == {f"p/{old}": "too-old"}
        assert report.folded == (f"q/{inside}",)
        assert f"products/p/{old}/manifest.json" not in cat.ops.called("read")

    def test_a_refused_registration_is_refused_again_and_read_once_a_run(self, cat: ReleaseCatalog) -> None:
        chain = cat.upload("chain", previous=cat.new_id())
        cat.run()
        cat.ops.calls.clear()

        report = cat.run()

        assert _rules(report) == {f"chain/{chain}": "chain"}
        assert report.versions == ()
        assert cat.ops.called("read").count(f"pending/chain/{chain}.json") == 1


class TestLifecycle:
    def test_a_new_product_with_no_lifecycle_is_refused_naming_the_fix(self, cat: ReleaseCatalog) -> None:
        e = cat.upload("p", lifecycle=None)

        report = cat.run()

        assert _rules(report) == {f"p/{e}": "lifecycle"}
        assert "statcatlib 0.17" in report.refused[0].reason

    def test_a_recorded_product_keeps_the_leafs_and_refuses_another(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p", lifecycle="final")
        absent = cat.upload("p", previous=live, lifecycle=None)
        cat.run()
        assert cat.states("p")[absent] == "embargoed"
        assert cat.full().leaf("p").lifecycle == "final"
        cat.admin().cancel("p", absent, reason="tests")

        other = cat.upload("p", previous=live, lifecycle="revisable")
        report = cat.run()

        assert _rules(report) == {f"p/{other}": "lifecycle"}
        assert cat.full().leaf("p").lifecycle == "final"


class TestFinal:
    def test_different_content_is_refused_against_the_live_edition(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p", lifecycle="final")
        same = cat.upload("same", lifecycle="final")
        changed = cat.upload("p", previous=live, lifecycle="final", content="changed")

        report = cat.run()

        assert _rules(report) == {f"p/{changed}": "final"}
        assert live in report.refused[0].reason
        assert report.folded == (f"same/{same}",)

    def test_equal_content_is_recorded(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p", lifecycle="final")
        again = cat.upload("p", previous=live, lifecycle="final")

        report = cat.run()

        assert report.folded == (f"p/{again}",)

    def test_previous_null_at_a_new_schema_is_compared_with_the_live_edition_at_any_schema(
        self, cat: ReleaseCatalog
    ) -> None:
        cat.live("p", lifecycle="final")
        changed = cat.upload("p", lifecycle="final", content="changed", schema_version="9.99")
        cat.run()
        equal = cat.upload("p", lifecycle="final", schema_version="9.98")

        report = cat.run()

        assert _rules(report) == {f"p/{changed}": "final"}
        assert report.folded == (f"p/{equal}",)

    def test_a_tombstoned_previous_with_no_live_edition_is_refused(self, cat: ReleaseCatalog) -> None:
        first = cat.upload("p", lifecycle="final")
        cat.run()
        cat.admin().cancel("p", first, reason="tests")
        cat.run(at=cat.now + 91 * _DAY)
        assert cat.states("p") == {}
        built_on_it = cat.upload("p", cat.new_id(cat.now), previous=first, lifecycle="final")

        report = cat.run()

        assert _rules(report) == {f"p/{built_on_it}": "final"}
        assert "retired" in report.refused[0].reason

    def test_a_revisable_product_takes_changed_content(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        changed = cat.upload("p", previous=live, content="changed")

        assert cat.run().folded == (f"p/{changed}",)


class TestForks:
    def test_two_registrations_on_one_live_edition_and_schema_are_both_refused(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        b1 = cat.upload("p", previous=live)
        b2 = cat.upload("p", previous=live)
        other = cat.upload("q")

        report = cat.run()

        assert _rules(report) == {f"p/{b1}": "pending-fork", f"p/{b2}": "pending-fork"}
        assert b2 in report.refused[0].reason
        assert b1 in report.refused[1].reason
        assert report.folded == (f"q/{other}",)
        assert cat.states("p") == {live: "live"}

    def test_a_fork_with_a_failed_third_registration_counts_only_the_two(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        b1 = cat.upload("p", previous=live)
        b2 = cat.upload("p", previous=live)
        b3 = cat.upload("p", previous=live, canonical_form="other-form")

        report = cat.run()

        assert _rules(report) == {
            f"p/{b1}": "pending-fork",
            f"p/{b2}": "pending-fork",
            f"p/{b3}": "canonical-form",
        }
        assert b3 not in report.refused[0].reason

    def test_a_registration_whose_twin_failed_its_own_check_is_recorded(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        good = cat.upload("p", previous=live)
        bad = cat.upload("p", previous=live, canonical_form="other-form")

        report = cat.run()

        assert report.folded == (f"p/{good}",)
        assert _rules(report) == {f"p/{bad}": "canonical-form"}

    def test_another_schema_is_no_fork(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        b1 = cat.upload("p", previous=live)
        b2 = cat.upload("p", previous=live, schema_version="9.99")

        report = cat.run()

        assert report.folded == (f"p/{b1}", f"p/{b2}")

    def test_after_one_is_discarded_the_next_run_folds_the_other(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        b1 = cat.upload("p", previous=live)
        b2 = cat.upload("p", previous=live)
        cat.run()
        cat.delete(f"pending/p/{b1}.json")

        report = cat.run()

        assert report.folded == (f"p/{b2}",)
        assert report.refused == ()
        assert cat.record()["refused"] == []


class TestSupersedePending:
    def test_a_correction_beside_an_embargoed_edition_withdraws_it(self, cat: ReleaseCatalog) -> None:
        live = cat.live("p")
        first = cat.upload("p", previous=live, release_at=cat.now + _DAY)
        cat.run()
        correction = cat.upload("p", previous=live, release_at=cat.now + _DAY)

        report = cat.run()

        assert report.folded == (f"p/{correction}",)
        assert cat.states("p") == {live: "live", first: "withdrawn", correction: "embargoed"}
        assert cat.full().root.event.kind == "seal"

    def test_dropping_the_correction_keeps_the_first_embargoed(self, cat: ReleaseCatalog) -> None:
        cat.live("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        live = cat.live("p", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])
        cut = [artefact(_CODELIST, "1" * 64, partial_of="a" * 64)]
        first = cat.upload("p", previous=live, artefacts=cut)
        cat.run()
        stale = [artefact(_CODELIST, "2" * 64, partial_of="b" * 64)]
        correction = cat.upload("p", previous=live, artefacts=stale)

        report = cat.run()

        assert _rules(report) == {f"p/{correction}": "full-artefacts"}
        assert cat.states("p") == {live: "live", first: "embargoed"}


class TestCrossProduct:
    def test_a_refused_product_is_dropped_and_the_rest_are_recorded_with_their_artefacts(
        self, cat: ReleaseCatalog
    ) -> None:
        home = cat.upload("home", kind="structure", artefacts=[artefact(_CODELIST, "a" * 64)])
        good = cat.upload("good", artefacts=[artefact(_CODELIST, "1" * 64, partial_of="a" * 64)])
        stale = cat.upload("stale", artefacts=[artefact(_CODELIST, "2" * 64, partial_of="b" * 64)])

        report = cat.run()

        assert report.folded == (f"good/{good}", f"home/{home}")
        assert _rules(report) == {f"stale/{stale}": "full-artefacts"}
        assert "matches no live or embargoed edition" in report.refused[0].reason
        assert cat.entry("good", good).artefacts == {
            _CODELIST: PartialRecord(fingerprint="1" * 64, partial_of="a" * 64)
        }
        assert cat.entry("home", home).artefacts == {_CODELIST: PartialRecord(fingerprint="a" * 64)}
        assert sorted(cat.full().products()) == ["good", "home"]

    def test_a_manifest_changed_between_the_folds_read_and_the_commit_is_refused(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = cat.upload("good")
        changed = cat.upload("changed")
        job = cat.job()
        real = job._writer.artefacts

        def reader(product_id: str, edition: str, sha: str) -> Any:
            if product_id == "changed":
                cat.put(cat.manifest_path("changed", changed), b"{}")
            return real(product_id, edition, sha)

        monkeypatch.setattr(job._writer, "_artefacts", reader)

        report = job.run()

        assert report.folded == (f"good/{good}",)
        assert _rules(report) == {f"changed/{changed}": "manifest"}

    def test_a_store_fault_on_an_incoming_manifest_refuses_that_registration(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = cat.upload("good")
        fault = cat.upload("fault")
        job = cat.job()
        real = job._writer.artefacts

        def reader(product_id: str, edition: str, sha: str) -> Any:
            if product_id == "fault":
                msg = "read the manifest: refused by the test"
                raise BlobStoreError(msg, path=cat.manifest_path("fault", fault))
            return real(product_id, edition, sha)

        monkeypatch.setattr(job._writer, "_artefacts", reader)

        report = job.run()

        assert report.folded == (f"good/{good}",)
        assert _rules(report) == {f"fault/{fault}": "store"}
        assert report.failed == {}


class TestPastAndRestored:
    def test_a_release_at_before_the_registration_was_written_is_recorded_with_none(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        late = cat.upload("late", release_at=cat.real - _HOUR)
        due = cat.upload("due", release_at=cat.now)

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run()

        assert report.folded == (f"due/{due}", f"late/{late}")
        assert report.released == (f"due/{due}",)
        assert cat.entry("late", late).release_at is None
        [event] = _events(caplog, "fold.release_at_past")
        assert (event["product"], event["edition"], event["severity"]) == ("late", late, "warning")
        assert set(event["detail"]) == {"release_at", "last_modified"}

        later = cat.run(at=cat.now + 30 * _DAY)

        assert later.released == ()
        assert cat.states("late") == {late: "embargoed"}

    def _restore(self, cat: ReleaseCatalog) -> None:
        """Bump ``catalog.json``'s restore epoch and write the ``restore`` version, at the storage clock."""
        catalog = decode_catalog(cat.read(CATALOG_NAME))
        cat.put(CATALOG_NAME, encode(msgspec.structs.replace(catalog, restore_epoch=catalog.restore_epoch + 1)))
        cat.writer.try_commit(cat.writer.head(), {}, kind="restore", reason="tests restore")

    def test_a_registration_older_than_the_restore_epochs_first_root_is_held(
        self, cat: ReleaseCatalog, caplog: pytest.LogCaptureFixture
    ) -> None:
        cat.live("earlier")
        rearmed = cat.upload("p", release_at=cat.now + _HOUR)
        self._restore(cat)
        started = cat.full().root.created_at

        with caplog.at_level(logging.INFO, logger="statcatlib.release"):
            report = cat.run(at=cat.now + 2 * _HOUR)

        assert report.folded == (f"p/{rearmed}",)
        assert report.released == ()
        assert cat.entry("p", rearmed).release_at is None
        [event] = _events(caplog, "fold.restored")
        assert (event["product"], event["edition"]) == ("p", rearmed)
        assert event["detail"]["epoch_started_at"] is not None
        record = cat.record()
        assert record["restore_epoch"] == 1
        assert record["epoch_started_at"] == started.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        cat.upload("q", release_at=cat.now + _HOUR)
        cat.ops.calls.clear()
        cat.run()

        assert root_path(1) not in cat.ops.called("read")
        assert cat.run(at=cat.now + 30 * _DAY).released == ()

    def test_a_registration_written_after_the_epoch_began_keeps_its_release_time(self, cat: ReleaseCatalog) -> None:
        cat.now = cat.real - 2 * _HOUR
        self._restore(cat)
        release_at = cat.real + _HOUR
        after = cat.upload("p", release_at=release_at)

        report = cat.run(at=cat.real + 2 * _HOUR)

        assert report.folded == (f"p/{after}",)
        assert report.released == (f"p/{after}",)
        assert cat.record()["restore_epoch"] == 1

    def test_epoch_0_walks_no_root_and_records_no_epoch(self, cat: ReleaseCatalog) -> None:
        cat.live("earlier")
        bad = cat.upload("bad", previous=cat.new_id())
        cat.ops.calls.clear()

        cat.run()

        assert bad in json.dumps(cat.record())
        assert (cat.record()["restore_epoch"], cat.record()["epoch_started_at"]) == (0, None)
        assert root_path(1) not in cat.ops.called("read")


class TestALostRace:
    def test_the_recording_is_planned_again_on_the_new_head_with_the_reads_kept(
        self, cat: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = cat.live("p")
        b = cat.upload("p", previous=live)
        other = cat.upload("q")
        cat.ops.calls.clear()
        job: ReleaseJob = cat.job()
        original = job._writer.try_commit
        raced: list[int] = []

        def racing(*args: Any, **kwargs: Any) -> Any:
            if not raced:
                raced.append(1)
                cat.admin().set_embargo(on=True, reason="tests: a competing version")
            return original(*args, **kwargs)

        monkeypatch.setattr(job._writer, "try_commit", racing)

        report = job.run()

        assert report.folded == (f"p/{b}", f"q/{other}")
        assert cat.states("p")[b] == "embargoed"
        reads = cat.ops.called("read")
        assert reads.count(f"pending/p/{b}.json") == 1
        assert reads.count(cat.manifest_path("p", b)) == 2
        assert cat.ops.called("list") == ["pending"]
        assert cat.full().seq == report.versions[0]

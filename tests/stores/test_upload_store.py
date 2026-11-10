"""`statcat upload`'s steps against Azurite, through `_upload` and the account-key digest read (Phase 4 Part 2).

Azurite keeps the MD5 of a single-shot Put, as Azure documents, so these tests
check the digest path end to end. None of them reaches real Azure, and none
exercises the bearer token, which `tests/test_azure_blob_rest.py` covers with
`pytest-httpx` and the real-account checklist proves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sdmxlib.edition import MANIFEST_NAME, content_md5, read_manifest
from sdmxlib.edition._write import digests_of
from sdmxlib.inventory import Tombstone, TransitionError, manifest_records

from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryWriter
from statcatlib.upload import UploadCheckError, UploadReport, _store_path, _upload
from tests.conftest import sealed_data_edition, sealed_referring_edition
from tests.stores import _stores

pytestmark = pytest.mark.stores

_RELEASE = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)


class Killed(BaseException):
    """The process died here."""


@pytest.fixture(scope="module")
def edition_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A sealed data edition with a binding, a Parquet file and a search index."""
    return sealed_data_edition(tmp_path_factory.mktemp("upload") / "catalog")


@pytest.fixture
def recorded(azurite: _stores.Running) -> BlobLocation:
    """A catalog at a fresh prefix, with a `RecordingOps`."""
    return _stores.recording_location(azurite, uuid.uuid4().hex)


@pytest.fixture
def rest(recorded: BlobLocation) -> Iterator[_stores.KeyedRest]:
    with _stores.KeyedRest(recorded) as client:
        yield client


def _ops(location: BlobLocation) -> _stores.RecordingOps:
    assert isinstance(location.ops, _stores.RecordingOps)
    return location.ops


def _run(edition_dir: Path, location: BlobLocation, rest: _stores.KeyedRest, **kwargs) -> UploadReport:
    return _upload(edition_dir, location, rest=rest, **kwargs)


def _pending(edition_dir: Path) -> str:
    manifest = read_manifest(edition_dir)
    return f"pending/{manifest.product_id}/{manifest.edition_id}.json"


def _absent(location: BlobLocation, path: str) -> bool:
    """Whether the store holds nothing at *path*, read past `RecordingOps`' refusal."""
    return BlobLocation(location.uri, location.store).ops.read(path) is None


class TestWhatAnUploadTouches:
    def test_nothing_under_the_inventory_is_read_or_written(self, edition_dir, recorded, rest) -> None:
        report = _run(edition_dir, recorded, rest, release_at=_RELEASE)

        ops = _ops(recorded)
        assert report.registered
        assert ops.writes[-1] == _pending(edition_dir)
        assert {path.split("/")[0] for path in ops.writes} == {"products", "pending"}
        # RecordingOps fails the test on any call there; this states the outcome in the store.
        for path in ("catalog.json", "CURRENT", "inventory/1.json"):
            assert _absent(recorded, path)

    def test_no_request_on_the_wire_names_the_inventory(self, edition_dir, azurite: _stores.Running) -> None:
        """The proxy sees what `RecordingOps` cannot: the digest client's requests as well as the store's."""
        with _stores.recording_proxy(azurite) as proxy:
            location = _stores.location(azurite, uuid.uuid4().hex, endpoint=proxy.endpoint)
            with _stores.KeyedRest(location) as rest:
                _upload(edition_dir, location, rest=rest, release_at=_RELEASE)

        paths = [request.path.partition("?")[0] for request in proxy.requests]
        assert any(path.endswith(MANIFEST_NAME) for path in paths)
        assert any("/pending/" in path for path in paths)
        named = [path for path in paths if "/inventory" in path or "CURRENT" in path or "catalog.json" in path]
        assert named == []

    def test_a_second_upload_writes_nothing_again(self, edition_dir, recorded, rest) -> None:
        first = _run(edition_dir, recorded, rest)
        ops = _ops(recorded)
        held = ops.read(_pending(edition_dir))
        ops.writes.clear()

        second = _run(edition_dir, recorded, rest)

        assert ops.writes == []
        assert second.registered is False
        assert second.sent == ()
        assert set(second.kept) == set(first.sent)
        assert ops.read(_pending(edition_dir)) == held

    def test_a_rerun_with_the_same_release_time_stays_registered(self, edition_dir, recorded, rest) -> None:
        _run(edition_dir, recorded, rest, release_at=_RELEASE)

        assert _run(edition_dir, recorded, rest, release_at=_RELEASE).registered is False


class TestACrashAndARerun:
    @pytest.mark.parametrize(
        "kill_at",
        [
            lambda edition, path: path.startswith("products/p/data/"),
            lambda edition, path: f"/{edition.name}/" in path and not path.endswith(MANIFEST_NAME),
            lambda edition, path: path.endswith(MANIFEST_NAME),
            lambda edition, path: path.startswith("pending/"),
        ],
        ids=["data-file", "edition-file", "manifest", "pending"],
    )
    def test_a_rerun_sends_only_what_is_missing(self, edition_dir, recorded, rest, kill_at: Callable) -> None:
        ops = _ops(recorded)

        def die(path: str) -> None:
            if kill_at(edition_dir, path):
                raise Killed

        ops.before_put = die
        with pytest.raises(Killed):
            _run(edition_dir, recorded, rest)
        written = list(ops.writes)

        ops.before_put = lambda _path: None
        report = _run(edition_dir, recorded, rest)

        assert report.registered
        assert set(report.kept) == set(written)
        assert not set(report.sent) & set(written)
        assert len(report.sent) + len(report.kept) == len(read_manifest(edition_dir).files) + 1
        assert ops.read(_pending(edition_dir)) is not None


class TestOrderAndDigests:
    def test_the_manifest_is_the_last_edition_file_written(self, edition_dir, recorded, rest) -> None:
        _run(edition_dir, recorded, rest)

        manifest = read_manifest(edition_dir)
        writes = _ops(recorded).writes
        manifest_path = f"products/{manifest.product_id}/{manifest.edition_id}/{MANIFEST_NAME}"
        assert writes[-2:] == [manifest_path, _pending(edition_dir)]
        data = [i for i, path in enumerate(writes) if path.startswith("products/p/data/")]
        own = [i for i, path in enumerate(writes[:-2]) if f"/{manifest.edition_id}/" in path]
        assert data
        assert own
        assert max(data) < min(own)

    @pytest.mark.parametrize("root", ["data", "edition"])
    def test_a_corrupt_blob_fails_before_pending(self, edition_dir, recorded, rest, root: str) -> None:
        manifest = read_manifest(edition_dir)
        entry = next(e for e in manifest.files if e.root == root)
        path = _store_path(manifest, entry)
        _ops(recorded).put(path, b"\0" * entry.size, mode="create")

        with pytest.raises(UploadCheckError) as caught:
            _run(edition_dir, recorded, rest)

        assert any(f.check == "remote" and "md5" in f.detail for f in caught.value.findings)
        assert _absent(recorded, _pending(edition_dir))

    def test_a_file_lost_after_its_step_gives_a_remote_finding(self, edition_dir, recorded, rest) -> None:
        ops = _ops(recorded)
        manifest = read_manifest(edition_dir)
        lost = next(e for e in manifest.files if e.root == "data")

        def lose(path: str) -> None:
            if path.endswith(MANIFEST_NAME):
                ops.delete(_store_path(manifest, lost))

        ops.before_put = lose

        with pytest.raises(UploadCheckError) as caught:
            _run(edition_dir, recorded, rest)

        details = [f for f in caught.value.findings if f.check == "remote"]
        assert [lost.path in f.subject for f in details] == [True]
        assert "missing from the store" in details[0].detail
        assert _absent(recorded, _pending(edition_dir))

    def test_azure_keeps_the_manifests_md5(self, edition_dir, recorded, rest) -> None:
        _run(edition_dir, recorded, rest)

        manifest = read_manifest(edition_dir)
        for entry in manifest.files:
            remote = rest.head(_store_path(manifest, entry))
            assert remote is not None
            assert remote.size == entry.size
            assert remote.digests["md5"] == content_md5(entry)

    def test_an_absent_blob_is_none(self, rest) -> None:
        assert rest.head("products/nothing/here") is None


class TestStructureProduct:
    def test_external_references_upload_and_register(self, tmp_path: Path, recorded, rest) -> None:
        edition = sealed_referring_edition(tmp_path / "catalog")
        manifest = read_manifest(edition)
        assert manifest.bindings == ()

        report = _run(edition, recorded, rest)

        assert report.registered
        assert not _absent(recorded, _pending(edition))


def _with_big_data_file(edition_dir: Path, size: int) -> Path:
    """List a *size*-byte file under ``data/`` in the edition's manifest."""
    big = edition_dir.parent / "data" / "big" / "big.bin"
    big.parent.mkdir(parents=True)
    with big.open("wb") as handle:
        handle.write(os.urandom(size))
    path = edition_dir / MANIFEST_NAME
    raw = json.loads(path.read_bytes())
    raw["files"].append(
        {
            "path": "big/big.bin",
            "size": size,
            "digests": digests_of(big),
            "root": "data",
            "tier": "remote",
        }
    )
    path.write_text(json.dumps(raw))
    return big


class TestSinglePut:
    def test_a_64_mib_data_file_is_one_put(self, tmp_path: Path, azurite: _stores.Running) -> None:
        edition = sealed_data_edition(tmp_path / "catalog")
        size = 64 * 1024 * 1024
        big = _with_big_data_file(edition, size)
        md5 = base64.b64encode(hashlib.md5(big.read_bytes(), usedforsecurity=False).digest()).decode()

        with _stores.recording_proxy(azurite) as proxy:
            location = _stores.location(azurite, uuid.uuid4().hex, endpoint=proxy.endpoint)
            with _stores.KeyedRest(location) as rest:
                _upload(edition, location, rest=rest)
                remote = rest.head("products/p/data/big/big.bin")

        (put,) = proxy.puts("big.bin")
        assert put.size == size
        assert put.headers["if-none-match"] == "*"
        assert put.headers["x-ms-blob-type"] == "BlockBlob"
        assert not [r for r in proxy.requests if "comp=block" in r.path]
        assert remote is not None
        assert remote.digests["md5"] == md5


class TestRecordUploaded:
    @pytest.fixture
    def writer(self, recorded: BlobLocation) -> InventoryWriter:
        # A separate location's ops: `RecordingOps` refuses the Inventory's paths, and only the upload runs under it.
        plain = BlobLocation(recorded.uri, recorded.store)
        made = InventoryWriter(BlobInventoryStorage(plain, actor="tests", clock=lambda: datetime.now(UTC)))
        made.write_catalog_json()
        return made

    def test_a_catalog_with_embargo_off_records_it_live(self, writer, recorded, edition_dir) -> None:
        edition = _stores.record_uploaded(writer, recorded, edition_dir)

        full = writer.full()
        assert full is not None
        (entry,) = full.entries(edition.product_id)
        assert entry.edition_id == edition.edition_id
        assert entry.state == "live"
        assert entry.manifest_sha256 == edition.manifest_sha256
        assert entry.pull_bytes is not None

    def test_a_data_and_a_structure_edition_record_their_artefacts_and_lifecycle(
        self, writer, recorded, edition_dir, tmp_path
    ) -> None:
        """GH-125 part 4 and Phase 5 D3: the fold's stand-in fills artefacts and takes the section's lifecycle."""
        structure_dir = sealed_referring_edition(tmp_path / "catalog")
        for one in (edition_dir, structure_dir):
            edition = _stores.record_uploaded(writer, recorded, one)

            full = writer.full()
            assert full is not None
            (entry,) = full.entries(edition.product_id)
            manifest = read_manifest(one)
            assert entry.artefacts == manifest_records(manifest)
            assert entry.artefacts
            assert full.leaf(edition.product_id).lifecycle == manifest.extensions["statcat"]["lifecycle"]
        assert full.leaf("structure-concepts").kind == "structure"

    def test_a_catalog_with_embargo_on_records_it_embargoed(self, writer, recorded, edition_dir) -> None:
        writer.try_commit(None, {}, embargo=True, kind="seal", reason="embargo on")

        edition = _stores.record_uploaded(writer, recorded, edition_dir, release_at=_RELEASE)

        full = writer.full()
        assert full is not None
        (entry,) = full.entries(edition.product_id)
        assert entry.state == "embargoed"
        assert entry.release_at == _RELEASE

    def test_a_tombstoned_edition_is_refused(self, writer, recorded, edition_dir) -> None:
        manifest = read_manifest(edition_dir)
        stone = Tombstone(product_id=manifest.product_id, edition_id=manifest.edition_id, retired_at=_RELEASE)
        writer.try_commit(None, {}, retired=[stone], kind="seal", reason="retired")

        with pytest.raises(TransitionError, match="tombstoned"):
            _stores.record_uploaded(writer, recorded, edition_dir)

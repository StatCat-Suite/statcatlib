"""`upload_edition` and `_upload` over `MemoryStore` and a fake digest client (Phase 4 Part 2, D7, D2)."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, override

import httpx
import pytest
from obstore.exceptions import AlreadyExistsError, PreconditionError
from obstore.store import AzureStore, MemoryStore, S3Store
from pytest_httpx import HTTPXMock
from sdmxlib.edition import MANIFEST_NAME, ManifestError, RemoteFile, check_edition, read_manifest

from statcatlib import upload as upload_module
from statcatlib.bundle import STATCAT_SECTION, check_catalog, check_catalog_no_index
from statcatlib.store.blob import BlobLocation, BlobStoreError, TokenIdentity, _ObjectOps, _statcat_check
from statcatlib.upload import (
    MAX_PUT_BYTES,
    UploadCheckError,
    _pending_document,
    _store_path,
    _upload,
    upload_edition,
)
from tests.conftest import sealed_data_edition, sealed_referring_edition
from tests.stores._stores import RecordingOps

pytestmark = pytest.mark.unit

_RELEASE = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
_URI = "az://catalogs/stc/"


class MemoryRest:
    """The digest read over a `MemoryStore`: the size and the MD5 Azure would keep, and the paths asked."""

    def __init__(self, store: MemoryStore) -> None:
        self._ops = _ObjectOps(store)
        self.asked: list[str] = []

    def head(self, path: str) -> RemoteFile | None:
        self.asked.append(path)
        got = self._ops.read(path)
        if got is None:
            return None
        md5 = hashlib.md5(got[0], usedforsecurity=False).digest()
        return RemoteFile(len(got[0]), {"md5": base64.b64encode(md5).decode()})


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sealed_data_edition(tmp_path_factory.mktemp("upload") / "catalog")


@pytest.fixture
def edition_dir(sealed: Path, tmp_path: Path) -> Path:
    """A copy of the sealed edition that a test may change, with the shared ``data/``."""
    product = tmp_path / "copy" / "products" / "p"
    shutil.copytree(sealed.parent, product)
    return product / sealed.name


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def ops(store: MemoryStore) -> RecordingOps:
    return RecordingOps(store)


@pytest.fixture
def location(store: MemoryStore, ops: RecordingOps) -> BlobLocation:
    return BlobLocation(_URI, store, ops=ops)


@pytest.fixture
def rest(store: MemoryStore) -> MemoryRest:
    return MemoryRest(store)


def _pending(edition_dir: Path) -> str:
    return f"pending/p/{edition_dir.name}.json"


def _edit_manifest(edition_dir: Path, change: Callable[[dict], None]) -> None:
    path = edition_dir / MANIFEST_NAME
    raw = json.loads(path.read_bytes())
    change(raw)
    path.write_text(json.dumps(raw))


class TestThePendingDocument:
    def test_its_bytes_are_compact_with_two_keys_in_order(self) -> None:
        assert _pending_document("e1", _RELEASE) == b'{"edition_id":"e1","release_at":"2026-11-05T13:30:00.000Z"}'

    def test_no_release_writes_null(self) -> None:
        assert _pending_document("e1", None) == b'{"edition_id":"e1","release_at":null}'

    def test_an_offset_becomes_utc_and_equal_instants_give_equal_bytes(self) -> None:
        east = _RELEASE.astimezone(timezone(timedelta(hours=5)))

        assert _pending_document("e1", east) == _pending_document("e1", _RELEASE)

    def test_a_time_keeps_milliseconds(self) -> None:
        moment = datetime(2026, 11, 5, 13, 30, 0, 123456, tzinfo=UTC)

        assert b'"2026-11-05T13:30:00.123Z"' in _pending_document("e1", moment)

    def test_the_upload_writes_it(self, edition_dir, location, rest, ops) -> None:
        _upload(edition_dir, location, release_at=_RELEASE, rest=rest)

        got = ops.read(_pending(edition_dir))
        assert got is not None
        assert got[0] == _pending_document(edition_dir.name, _RELEASE)


class TestARerun:
    def test_another_release_time_raises_naming_both(self, edition_dir, location, rest, ops) -> None:
        _upload(edition_dir, location, release_at=_RELEASE, rest=rest)
        later = _RELEASE + timedelta(days=1)

        with pytest.raises(UploadCheckError) as caught:
            _upload(edition_dir, location, release_at=later, rest=rest)

        (finding,) = caught.value.findings
        assert finding.subject == _pending(edition_dir)
        assert "2026-11-05T13:30:00.000Z" in finding.detail
        assert "2026-11-06T13:30:00.000Z" in finding.detail
        got = ops.read(_pending(edition_dir))
        assert got is not None
        assert got[0] == _pending_document(edition_dir.name, _RELEASE)

    def test_the_same_release_time_reports_no_registration_and_sends_nothing(self, edition_dir, location, rest) -> None:
        first = _upload(edition_dir, location, release_at=_RELEASE, rest=rest)
        second = _upload(edition_dir, location, release_at=_RELEASE, rest=rest)

        assert first.registered
        assert not second.registered
        assert second.sent == ()
        assert set(second.kept) == set(first.sent)
        assert first.sent[-1].endswith(MANIFEST_NAME)
        assert first.product_id == "p"
        assert first.edition_id == edition_dir.name


class TestRefusals:
    def test_an_unsealed_directory(self, tmp_path: Path, location, rest, ops) -> None:
        with pytest.raises(ManifestError, match="not a sealed edition"):
            _upload(tmp_path, location, rest=rest)

        assert ops.calls == []

    def test_a_differing_manifest_in_the_store(self, edition_dir, location, rest, ops) -> None:
        path = f"products/p/{edition_dir.name}/{MANIFEST_NAME}"
        ops.put(path, b"{}", mode="create")
        ops.writes.clear()

        with pytest.raises(UploadCheckError, match="never rewritten"):
            _upload(edition_dir, location, rest=rest)

        assert not [p for p in ops.writes if p.startswith("pending/")]

    def test_an_oversize_file_before_anything_is_sent(self, edition_dir, location, rest, ops) -> None:
        def grow(raw: dict) -> None:
            raw["files"][0]["size"] = MAX_PUT_BYTES + 1

        _edit_manifest(edition_dir, grow)
        name = read_manifest(edition_dir).files[0].path

        with pytest.raises(UploadCheckError) as caught:
            _upload(edition_dir, location, rest=rest)

        assert [f.subject for f in caught.value.findings] == [name]
        assert ops.calls == []

    def test_the_limit_is_put_blobs(self) -> None:
        assert MAX_PUT_BYTES == 5_000 * 1024 * 1024

    @pytest.mark.parametrize("root", ["data", "edition"])
    def test_a_rotted_local_file_stays_out_of_the_store(self, edition_dir, location, rest, ops, root: str) -> None:
        manifest = read_manifest(edition_dir)
        entry = next(e for e in manifest.files if e.root == root)
        local = upload_module._local_file(edition_dir, manifest, entry)
        local.write_bytes(b"\0" * entry.size)

        with pytest.raises(UploadCheckError, match="sha256") as caught:
            _upload(edition_dir, location, rest=rest)

        assert [f.subject for f in caught.value.findings] == [entry.path]
        assert _store_path(manifest, entry) not in ops.writes
        assert not [p for p in ops.writes if p.startswith("pending/")]

    def test_a_missing_local_file_the_store_lacks(self, edition_dir, location, rest) -> None:
        manifest = read_manifest(edition_dir)
        entry = next(e for e in manifest.files if e.root == "data")
        upload_module._local_file(edition_dir, manifest, entry).unlink()

        with pytest.raises(UploadCheckError, match="cannot be read"):
            _upload(edition_dir, location, rest=rest)

    @pytest.mark.parametrize("root", ["data", "edition"])
    @pytest.mark.parametrize(
        "value", ["../../../inventory/1.json", "/etc/passwd", "a//b"], ids=["dot-dot", "absolute", "empty-segment"]
    )
    def test_a_path_leaving_its_directory_writes_nothing(self, edition_dir, location, rest, ops, value, root) -> None:
        def aim(raw: dict) -> None:
            next(entry for entry in raw["files"] if entry["root"] == root)["path"] = value

        _edit_manifest(edition_dir, aim)

        with pytest.raises(UploadCheckError, match="outside its directory"):
            _upload(edition_dir, location, rest=rest)

        assert ops.calls == []

    def test_a_data_path_leaving_the_product_writes_nothing(self, edition_dir, location, rest, ops) -> None:
        def aim(raw: dict) -> None:
            raw["data_path"] = "../../../inventory/"

        _edit_manifest(edition_dir, aim)

        with pytest.raises(ManifestError):
            _upload(edition_dir, location, rest=rest)

        assert ops.calls == []

    def test_a_product_id_that_is_a_path_writes_nothing(self, edition_dir, location, rest, ops) -> None:
        def aim(raw: dict) -> None:
            raw["product_id"] = "../inventory"

        _edit_manifest(edition_dir, aim)

        with pytest.raises(UploadCheckError, match="product_id"):
            _upload(edition_dir, location, rest=rest)

        assert ops.calls == []

    def test_a_naive_release_time(self, edition_dir, location, rest, ops) -> None:
        with pytest.raises(ValueError, match="time zone"):
            _upload(edition_dir, location, release_at=datetime(2026, 11, 5), rest=rest)

        assert ops.calls == []

    def test_an_s3_store_names_w3(self, edition_dir) -> None:
        store = S3Store("bucket", region="us-east-1", skip_signature=True)

        with pytest.raises(TypeError, match=r"W3.*S3Store"):
            upload_edition(edition_dir, BlobLocation("s3://bucket/", store))

    def test_a_memory_store_names_w3(self, edition_dir, location) -> None:
        with pytest.raises(TypeError, match="W3"):
            upload_edition(edition_dir, location)

    def test_no_token_identity_writes_nothing(self, edition_dir, ops) -> None:
        store = AzureStore("catalogs", prefix="stc", account_name="acct", account_key="a2V5")

        with pytest.raises(ValueError, match="TokenIdentity"):
            upload_edition(edition_dir, BlobLocation(_URI, store, ops=ops))

        assert ops.calls == []

    def test_no_account_in_the_config_writes_nothing(self, edition_dir, ops, monkeypatch) -> None:
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "from-the-environment")
        identity = TokenIdentity(lambda: {"token": "t", "expires_at": None})
        store = AzureStore("catalogs", credential_provider=identity)

        with pytest.raises(ValueError, match="account_name"):
            upload_edition(edition_dir, BlobLocation(_URI, store, ops=ops))

        assert ops.calls == []


class TestStoreConditions:
    def test_a_create_that_finds_the_file_counts_it_as_kept(self, edition_dir, location, rest, ops) -> None:
        """A retried create can raise `AlreadyExistsError` against its own earlier success (F13)."""
        manifest = read_manifest(edition_dir)
        entry = next(e for e in manifest.files if e.root == "data")
        target = _store_path(manifest, entry)

        def lose(path: str) -> None:
            if path == target:
                local = upload_module._local_file(edition_dir, manifest, entry).read_bytes()
                _ObjectOps(location.store).put(path, local, mode="create")

        ops.before_put = lose

        report = _upload(edition_dir, location, rest=rest)

        assert target in report.kept
        assert target not in report.sent
        assert report.registered

    def test_a_precondition_error_on_a_create_is_a_store_fault(self, edition_dir, store, rest) -> None:
        class Refusing(_ObjectOps):
            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                raise PreconditionError(path)

        with pytest.raises(BlobStoreError, match="PreconditionError"):
            _upload(edition_dir, BlobLocation(_URI, store, ops=Refusing(store)), rest=rest)

    def test_a_lost_pending_entry_after_a_lost_create_raises(self, edition_dir, store, rest) -> None:
        class Vanishing(_ObjectOps):
            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                if path.startswith("pending/"):
                    raise AlreadyExistsError(path)
                return super().put(path, data, **kwargs)

        with pytest.raises(BlobStoreError, match="now absent"):
            _upload(edition_dir, BlobLocation(_URI, store, ops=Vanishing(store)), rest=rest)

    def test_each_file_is_put_with_create_and_one_read(self, edition_dir, store, rest) -> None:
        modes: list[object] = []

        class Modes(_ObjectOps):
            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                modes.append((kwargs["mode"], kwargs.get("use_multipart", False)))
                return super().put(path, data, **kwargs)

        _upload(edition_dir, BlobLocation(_URI, store, ops=Modes(store)), rest=rest)

        assert set(modes) == {("create", False)}


class TestVerification:
    def test_every_listed_file_is_asked_once(self, edition_dir, location, rest) -> None:
        _upload(edition_dir, location, rest=rest)

        manifest = read_manifest(edition_dir)
        assert sorted(rest.asked) == sorted(_store_path(manifest, entry) for entry in manifest.files)

    def test_a_blob_with_no_kept_digest_is_a_finding(self, edition_dir, location, store, ops) -> None:
        """A block upload keeps no MD5 (F7), and `verify_remote` reports a file it cannot compare."""

        class NoDigest(MemoryRest):
            @override
            def head(self, path: str) -> RemoteFile | None:
                found = super().head(path)
                return None if found is None else RemoteFile(found.size)

        with pytest.raises(UploadCheckError, match="no digest"):
            _upload(edition_dir, location, rest=NoDigest(store))

        assert not [p for p in ops.writes if p.startswith("pending/")]

    def test_a_differing_uploaded_manifest_is_a_finding(self, edition_dir, store, rest) -> None:
        class Rotted(_ObjectOps):
            @override
            def read(self, path: str) -> tuple[bytes, str | None] | None:
                return (b"rot", None) if path.endswith(MANIFEST_NAME) else super().read(path)

        ops = Rotted(store)
        with pytest.raises(UploadCheckError) as caught:
            _upload(edition_dir, BlobLocation(_URI, store, ops=ops), rest=rest)

        assert [f.check for f in caught.value.findings] == ["remote"]
        assert ops.read(_pending(edition_dir)) is None

    def test_a_data_edition_with_bindings_checks_its_references(self, edition_dir, location, rest, monkeypatch) -> None:
        seen = _capture_check(monkeypatch)

        _upload(edition_dir, location, rest=rest)

        assert read_manifest(edition_dir).bindings
        assert seen["references"] is True
        assert seen["at"] == "receive"

    def test_an_edition_with_no_bindings_defers_its_references(self, tmp_path, location, rest, monkeypatch) -> None:
        edition = sealed_referring_edition(tmp_path / "catalog")
        seen = _capture_check(monkeypatch)

        report = _upload(edition, location, rest=rest)

        assert read_manifest(edition).bindings == ()
        assert seen["references"] is False
        assert report.registered
        assert any(f.check == "references" for f in check_edition(edition, at="receive").findings)


def _capture_check(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def spy(path: Path, **kwargs: Any) -> object:
        seen.update(kwargs)
        return check_edition(path, **kwargs)

    monkeypatch.setattr(upload_module, "check_edition", spy)
    return seen


class TestUploadEdition:
    def test_the_bearer_path_end_to_end_over_a_stubbed_azure(
        self, edition_dir, store, ops, httpx_mock: HTTPXMock
    ) -> None:
        """`upload_edition` builds the real client; the bytes go to a `MemoryStore`, the digests to `pytest-httpx`."""
        base = "https://acct.blob.core.windows.net/catalogs/stc/"
        memory = MemoryRest(store)

        def answer(request):
            found = memory.head(str(request.url).removeprefix(base))
            assert request.headers["authorization"] == "Bearer the-token"
            if found is None:
                return httpx.Response(404)
            return httpx.Response(200, headers={"Content-Length": str(found.size), "Content-MD5": found.digests["md5"]})

        httpx_mock.add_callback(answer, method="HEAD", is_reusable=True)
        identity = TokenIdentity(lambda: {"token": "the-token", "expires_at": None})
        azure = AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=identity)

        report = upload_edition(edition_dir, BlobLocation(_URI, azure, ops=ops), release_at=_RELEASE)

        assert report.registered
        assert ops.writes[-1] == _pending(edition_dir)


class TestWithNoLancedb:
    """A ``statcatlib[blob]`` install holds no `lancedb`, and `check_catalog` opens the search index (D2)."""

    def test_the_check_is_chosen_by_what_is_importable(self, monkeypatch) -> None:
        assert _statcat_check() is check_catalog

        monkeypatch.setitem(sys.modules, "lancedb", None)

        assert _statcat_check() is check_catalog_no_index

    def test_check_search_gives_a_finding_and_no_crash(self, edition_dir, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "lancedb", None)

        report = check_edition(edition_dir, at="receive", extensions={STATCAT_SECTION: check_catalog})

        assert [f.subject for f in report.findings if f.check == STATCAT_SECTION] == ["search"]

    def test_an_upload_skips_the_index_check_and_registers(self, edition_dir, location, rest, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "lancedb", None)

        assert _upload(edition_dir, location, rest=rest).registered

    def test_the_no_index_check_runs_the_sections_other_checks(self, edition_dir) -> None:
        def miscount(raw: dict) -> None:
            raw["extensions"]["statcat"]["datasets"] = 99

        _edit_manifest(edition_dir, miscount)

        report = check_edition(edition_dir, at="receive", extensions={STATCAT_SECTION: check_catalog_no_index})

        assert any("statcat.dataset" in f.subject for f in report.findings)

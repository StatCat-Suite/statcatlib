"""`BlobInventoryStorage` over `MemoryStore`: the leaf rule, ``CURRENT``, unknown outcomes, the clock, the actor."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, override

import httpx
import msgspec
import obstore
import pytest
import sdmxlib as sl
from obstore.exceptions import (
    AlreadyExistsError,
    BaseError,
    GenericError,
    InvalidPathError,
    NotSupportedError,
    PermissionDeniedError,
    PreconditionError,
    UnauthenticatedError,
)
from obstore.store import AzureStore, MemoryStore, S3Store
from pytest_httpx import HTTPXMock
from sdmxlib.edition import MANIFEST_NAME, ManifestError
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    Leaf,
    Root,
    decode_current,
    encode,
    leaf_ref,
    record,
    root_path,
    sha256_hex,
)
from sdmxlib.inventory.conformance import SCHEMA_VERSION

import statcatlib as sc
from statcatlib.store.blob import (
    MAX_CLOCK_SKEW,
    BlobInventoryStorage,
    BlobLocation,
    BlobStoreError,
    TokenIdentity,
    _endpoint,
    _ObjectOps,
)
from statcatlib.store.inventory import (
    MAX_RACES,
    Change,
    InventoryCommitUnknownError,
    InventoryConflictError,
    InventoryStorage,
    InventoryWriter,
    LocalInventoryStorage,
    current_is_older,
)

if TYPE_CHECKING:
    from obstore import ObjectMeta

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
_URI = "az://catalogs/stc/"


def _edition(n: int) -> str:
    return f"01a18e00-0000-7000-8000-{n:012d}"


def _leaf(product: str, n: int, *, embargo: bool = False) -> Leaf:
    return record(
        None,
        product_id=product,
        kind="data",
        lifecycle="revisable",
        edition_id=_edition(n),
        previous=None,
        manifest_sha256="0" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=embargo,
        now=_NOW,
        release_at=_NOW if embargo else None,
    )


class RecordingOps(_ObjectOps):
    """Records every read and write, and lets a test fail a write to one path."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store)
        self.reads: list[str] = []
        self.puts: list[str] = []
        self.heads: list[str] = []
        self.before_put: Callable[[str], None] = lambda _path: None

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self.reads.append(path)
        return super().read(path)

    @override
    def head(self, path: str) -> ObjectMeta | None:
        self.heads.append(path)
        return super().head(path)

    @override
    def put(self, path: str, data: bytes, **kwargs) -> str | None:
        self.puts.append(path)
        self.before_put(path)
        return super().put(path, data, **kwargs)


@pytest.fixture
def ops() -> RecordingOps:
    return RecordingOps(MemoryStore())


@pytest.fixture
def storage(ops: RecordingOps) -> BlobInventoryStorage:
    return BlobInventoryStorage(BlobLocation(_URI, ops._store, ops=ops), actor="tests", clock=lambda: _NOW)


@pytest.fixture
def writer(storage: BlobInventoryStorage) -> InventoryWriter:
    made = InventoryWriter(storage)
    made.write_catalog_json()
    return made


def _faulty_put(monkeypatch: pytest.MonkeyPatch, *faults: BaseError) -> list[int]:
    """Make `obstore.put` raise each of *faults* in turn, then behave. Returns a list holding one item per call."""
    remaining = list(faults)
    real = obstore.put
    calls: list[int] = []

    def put(*args, **kwargs):
        calls.append(1)
        if remaining:
            raise remaining.pop(0)
        return real(*args, **kwargs)

    monkeypatch.setattr(obstore, "put", put)
    return calls


class TestLeafRule:
    def test_a_one_product_version_at_50_products_makes_one_leaf_put(
        self, writer: InventoryWriter, ops: RecordingOps
    ) -> None:
        first = writer.try_commit(None, {f"p{n}": _leaf(f"p{n}", n) for n in range(50)}, kind="seal", reason="first")
        ops.puts.clear()

        root = writer.try_commit(first, {"p7": _leaf("p7", 100)}, kind="seal", reason="one product")

        changed = root.products["p7"].leaf
        assert ops.puts == [changed, root_path(2), CURRENT_NAME]

    def test_a_carried_leaf_gets_no_put(self, writer: InventoryWriter, ops: RecordingOps) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1), "b": _leaf("b", 2)}, kind="seal", reason="first")
        ops.puts.clear()

        root = writer.try_commit(first, {}, embargo=True, kind="embargo-change", reason="carry both")

        assert root.products == first.products
        assert ops.puts == [root_path(2), CURRENT_NAME]

    def test_a_present_changed_leaf_is_put_again_with_no_existence_check(
        self, writer: InventoryWriter, ops: RecordingOps
    ) -> None:
        leaf = _leaf("a", 1)
        first = writer.try_commit(None, {"a": leaf}, kind="seal", reason="first")
        ops.puts.clear()
        ops.reads.clear()

        writer.try_commit(first, {"a": leaf}, kind="seal", reason="the same bytes")

        name = leaf_ref(leaf)[1].leaf
        assert ops.puts.count(name) == 1
        assert name not in ops.reads
        assert ops.heads == []

    def test_a_leaf_write_never_uses_a_mode_or_multipart(
        self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict] = []
        real = obstore.put

        def put(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(obstore, "put", put)
        storage.write_leaf("inventory/leaves/x.json", b"{}")
        assert seen == [{"mode": None, "use_multipart": False}]

    def test_a_root_write_is_a_create(self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[dict] = []
        real = obstore.put

        def put(*args, **kwargs):
            seen.append(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(obstore, "put", put)
        storage.create(root_path(1), b"{}")
        assert seen == [{"mode": "create", "use_multipart": False}]


def _root_with(root: Root, **changes: object) -> Root:
    return msgspec.structs.replace(root, **changes)


def _held(storage: BlobInventoryStorage) -> Current:
    raw = storage.read(CURRENT_NAME)
    assert raw is not None
    return decode_current(raw)


class TestCurrent:
    def test_the_rule(self, writer: InventoryWriter) -> None:
        root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        sha = "0" * 64
        assert current_is_older(None, root)
        assert current_is_older(Current(seq=root.seq - 1, root_sha256=sha, restore_epoch=0), root) is True
        assert current_is_older(Current(seq=root.seq, root_sha256=sha, restore_epoch=0), root) is False
        assert current_is_older(Current(seq=root.seq + 1, root_sha256=sha, restore_epoch=0), root) is False
        assert current_is_older(Current(seq=root.seq + 1, root_sha256=sha, restore_epoch=1), root) is True

    def test_it_moves_only_forward(
        self, writer: InventoryWriter, storage: BlobInventoryStorage, ops: RecordingOps
    ) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        second = writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="second")
        ops.puts.clear()

        storage.advance_current(first, b"the first root")

        assert ops.puts == []
        assert _held(storage).seq == second.seq

    def test_a_recorded_restores_lower_seq_replaces_it(
        self, writer: InventoryWriter, storage: BlobInventoryStorage
    ) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="second")

        storage.advance_current(_root_with(first, restore_epoch=1), b"the restored root")

        held = _held(storage)
        assert (held.seq, held.restore_epoch) == (first.seq, 1)
        assert held.root_sha256 == sha256_hex(b"the restored root")

    def test_a_malformed_one_is_replaced(
        self, writer: InventoryWriter, storage: BlobInventoryStorage, ops: RecordingOps
    ) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        ops.put(CURRENT_NAME, b"not json")

        storage.advance_current(first, b"the root")

        assert _held(storage).seq == first.seq

    def test_already_exists_on_a_first_version_retries(
        self, writer: InventoryWriter, storage: BlobInventoryStorage, ops: RecordingOps
    ) -> None:
        def competing(path: str) -> None:
            if path == CURRENT_NAME and ops.puts.count(CURRENT_NAME) == 1:
                msg = "another writer created CURRENT"
                raise AlreadyExistsError(msg)

        ops.before_put = competing
        root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert ops.puts.count(CURRENT_NAME) == 2
        assert _held(storage).seq == root.seq

    def test_a_lost_condition_rereads_and_stops_at_a_newer_version(
        self, writer: InventoryWriter, storage: BlobInventoryStorage, ops: RecordingOps
    ) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        second = writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="second")
        ops.put(CURRENT_NAME, encode(Current(seq=first.seq, root_sha256="0" * 64, restore_epoch=0)))
        newer = encode(Current(seq=second.seq + 5, root_sha256="0" * 64, restore_epoch=0))
        state = {"raced": False}

        def competing(path: str) -> None:
            if path == CURRENT_NAME and not state["raced"]:
                state["raced"] = True
                obstore.put(ops._store, CURRENT_NAME, newer)
                msg = "another writer moved CURRENT"
                raise PreconditionError(msg)

        ops.before_put = competing
        ops.puts.clear()

        storage.advance_current(second, b"the second root")

        assert ops.puts == [CURRENT_NAME]
        assert _held(storage).seq == second.seq + 5


class TestFailedAdvance:
    def test_a_store_fault_logs_a_warning_and_returns_the_root(
        self,
        writer: InventoryWriter,
        ops: RecordingOps,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def refuse(path: str) -> None:
            if path == CURRENT_NAME:
                msg = "write CURRENT: denied"
                raise BlobStoreError(msg, path=path)

        ops.before_put = refuse

        with caplog.at_level(logging.WARNING, logger="statcatlib.store.blob"):
            root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert root.seq == 1
        assert "CURRENT did not move" in caplog.text
        head = writer.head()
        assert head is not None
        assert head.seq == 1

    def test_lost_races_log_a_warning_and_return_the_root(
        self, writer: InventoryWriter, ops: RecordingOps, caplog: pytest.LogCaptureFixture
    ) -> None:
        def lose(path: str) -> None:
            if path == CURRENT_NAME:
                msg = "always"
                raise AlreadyExistsError(msg)

        ops.before_put = lose

        with caplog.at_level(logging.WARNING, logger="statcatlib.store.blob"):
            root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert root.seq == 1
        assert ops.puts.count(CURRENT_NAME) == MAX_RACES
        assert f"lost {MAX_RACES} races" in caplog.text


class TestCreateOutcome:
    @pytest.mark.parametrize("fault", [AlreadyExistsError("taken"), GenericError("timeout")], ids=["exists", "generic"])
    def test_equal_bytes_mean_this_writer_won(
        self, storage: BlobInventoryStorage, ops: RecordingOps, monkeypatch: pytest.MonkeyPatch, fault: BaseError
    ) -> None:
        ops.put("inventory/x.json", b"mine")
        calls = _faulty_put(monkeypatch, fault)

        storage.create("inventory/x.json", b"mine")

        assert len(calls) == 1

    @pytest.mark.parametrize("fault", [AlreadyExistsError("taken"), GenericError("timeout")], ids=["exists", "generic"])
    def test_other_bytes_raise_file_exists(
        self, storage: BlobInventoryStorage, ops: RecordingOps, monkeypatch: pytest.MonkeyPatch, fault: BaseError
    ) -> None:
        ops.put("inventory/x.json", b"theirs")
        _faulty_put(monkeypatch, fault)

        with pytest.raises(FileExistsError, match="other bytes"):
            storage.create("inventory/x.json", b"mine")

    def test_other_bytes_become_a_conflict_in_try_commit(self, writer: InventoryWriter, ops: RecordingOps) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="winner")

        with pytest.raises(InventoryConflictError, match=r"version 2"):
            writer.try_commit(first, {"c": _leaf("c", 3)}, kind="seal", reason="loser")

    def test_a_real_second_create_with_other_bytes_conflicts(self, storage: BlobInventoryStorage) -> None:
        storage.create("inventory/x.json", b"first")
        with pytest.raises(FileExistsError):
            storage.create("inventory/x.json", b"second")
        assert storage.read("inventory/x.json") == b"first"

    def test_twin_roots_both_succeed(self, storage: BlobInventoryStorage) -> None:
        storage.create("inventory/x.json", b"twin")
        storage.create("inventory/x.json", b"twin")
        assert storage.read("inventory/x.json") == b"twin"

    @pytest.mark.parametrize("fault", [AlreadyExistsError("taken"), GenericError("timeout")], ids=["exists", "generic"])
    def test_an_absent_name_is_tried_once_more(
        self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch, fault: BaseError
    ) -> None:
        calls = _faulty_put(monkeypatch, fault)

        storage.create("inventory/x.json", b"mine")

        assert len(calls) == 2
        assert storage.read("inventory/x.json") == b"mine"

    def test_still_failing_leaves_the_outcome_unknown(
        self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _faulty_put(monkeypatch, GenericError("timeout"), GenericError("timeout"))

        with pytest.raises(InventoryCommitUnknownError, match=r"inventory/000000000003\.json"):
            storage.create(root_path(3), b"mine")

        assert len(calls) == 2

    def test_an_unknown_read_counts_as_nothing_found(
        self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def get(*_args, **_kwargs):
            msg = "timeout"
            raise GenericError(msg)

        _faulty_put(monkeypatch, GenericError("timeout"), GenericError("timeout"))
        monkeypatch.setattr(obstore, "get", get)

        with pytest.raises(InventoryCommitUnknownError):
            storage.create("inventory/x.json", b"mine")

    def test_a_definite_read_fault_raises(self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch) -> None:
        def get(*_args, **_kwargs):
            msg = "denied"
            raise PermissionDeniedError(msg)

        _faulty_put(monkeypatch, GenericError("timeout"))
        monkeypatch.setattr(obstore, "get", get)

        with pytest.raises(BlobStoreError, match="denied"):
            storage.create("inventory/x.json", b"mine")

    @pytest.mark.parametrize(
        "fault",
        [
            PermissionDeniedError("definite"),
            UnauthenticatedError("definite"),
            NotSupportedError("definite"),
            InvalidPathError("definite"),
            PreconditionError("definite"),
        ],
        ids=lambda f: type(f).__name__,
    )
    def test_a_definite_refusal_raises_at_once(
        self, storage: BlobInventoryStorage, monkeypatch: pytest.MonkeyPatch, fault: BaseError
    ) -> None:
        calls = _faulty_put(monkeypatch, fault)

        with pytest.raises(BlobStoreError, match=r"inventory/x\.json") as raised:
            storage.create("inventory/x.json", b"mine")

        assert len(calls) == 1
        assert raised.value.path == "inventory/x.json"
        assert raised.value.outcome_unknown is False
        assert raised.value.__cause__ is fault


class TestObjectOps:
    def test_an_absent_object_is_none(self, ops: RecordingOps) -> None:
        assert ops.read("nothing") is None
        assert ops.head("nothing") is None

    def test_read_returns_the_bytes_and_e_tag(self, ops: RecordingOps) -> None:
        e_tag = ops.put("a", b"bytes")
        assert ops.read("a") == (b"bytes", e_tag)
        meta = ops.head("a")
        assert meta is not None
        assert meta["size"] == 5

    def test_delete(self, ops: RecordingOps) -> None:
        ops.put("a", b"bytes")
        ops.delete("a")
        assert ops.read("a") is None

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("get", lambda o: o.read("a")),
            ("head", lambda o: o.head("a")),
            ("delete", lambda o: o.delete("a")),
            ("put", lambda o: o.put("a", b"")),
        ],
        ids=["read", "head", "delete", "put"],
    )
    def test_a_fault_names_the_path_and_chains(
        self, ops: RecordingOps, monkeypatch: pytest.MonkeyPatch, name: str, call: Callable[[_ObjectOps], object]
    ) -> None:
        fault = GenericError("timeout")

        def fails(*_args, **_kwargs):
            raise fault

        monkeypatch.setattr(obstore, name, fails)

        with pytest.raises(BlobStoreError, match="a: GenericError") as raised:
            call(_ObjectOps(ops._store))

        assert raised.value.outcome_unknown is True
        assert raised.value.__cause__ is fault

    @pytest.mark.parametrize("fault", [AlreadyExistsError("x"), PreconditionError("x")], ids=lambda f: type(f).__name__)
    def test_a_lost_condition_reaches_the_caller(
        self, ops: RecordingOps, monkeypatch: pytest.MonkeyPatch, fault: BaseError
    ) -> None:
        _faulty_put(monkeypatch, fault)
        with pytest.raises(type(fault)):
            _ObjectOps(ops._store).put("a", b"")


def _azure(**kwargs) -> AzureStore:
    return AzureStore("catalogs", prefix="stc", account_name="acct", **kwargs)


def _on_azure_clock(httpx_mock: HTTPXMock, **headers: str) -> BlobInventoryStorage:
    httpx_mock.add_response(method="HEAD", url="https://acct.blob.core.windows.net", status_code=400, headers=headers)
    return BlobInventoryStorage(BlobLocation(_URI, _azure()), actor="tests")


def _http_date(when: datetime) -> str:
    return format_datetime(when.astimezone(UTC), usegmt=True)


class TestClock:
    def test_a_clock_within_the_bound_takes_the_storage_time(self, httpx_mock: HTTPXMock) -> None:
        sent = datetime.now(UTC).replace(microsecond=0)
        storage = _on_azure_clock(httpx_mock, Date=_http_date(sent))
        assert storage.now() == sent

    def test_this_machine_ahead_over_the_bound_refuses_and_names_both_times(self, httpx_mock: HTTPXMock) -> None:
        sent = datetime.now(UTC).replace(microsecond=0) - MAX_CLOCK_SKEW - timedelta(seconds=5)
        storage = _on_azure_clock(httpx_mock, Date=_http_date(sent))
        with pytest.raises(sc.InventoryWriteRefusedError, match=sent.isoformat().replace("+", r"\+")) as raised:
            storage.now()
        assert "this machine's clock reads" in str(raised.value)

    def test_this_machine_behind_passes(self, httpx_mock: HTTPXMock) -> None:
        sent = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
        storage = _on_azure_clock(httpx_mock, Date=_http_date(sent))
        assert storage.now() == sent

    def test_a_missing_date_refuses(self, httpx_mock: HTTPXMock) -> None:
        storage = _on_azure_clock(httpx_mock)
        with pytest.raises(sc.InventoryWriteRefusedError, match="no readable Date"):
            storage.now()

    @pytest.mark.parametrize("value", ["yesterday", "Thu, 05 Nov 2026 12:00:00 -0000"])
    def test_a_malformed_date_refuses(self, httpx_mock: HTTPXMock, value: str) -> None:
        storage = _on_azure_clock(httpx_mock, Date=value)
        with pytest.raises(sc.InventoryWriteRefusedError, match="no readable Date"):
            storage.now()

    def test_a_failed_request_refuses(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ConnectError("no route"))
        storage = BlobInventoryStorage(BlobLocation(_URI, _azure()), actor="tests")
        with pytest.raises(sc.InventoryWriteRefusedError, match="no route"):
            storage.now()

    def test_a_memory_store_with_no_clock_refuses(self) -> None:
        storage = BlobInventoryStorage(BlobLocation(_URI, MemoryStore()), actor="tests")
        with pytest.raises(sc.InventoryWriteRefusedError, match=r"clock="):
            storage.now()

    def test_a_given_clock_sends_nothing(self) -> None:
        storage = BlobInventoryStorage(BlobLocation(_URI, _azure()), actor="tests", clock=lambda: _NOW)
        assert storage.now() == _NOW

    def test_a_try_commit_sends_one_head(self, httpx_mock: HTTPXMock) -> None:
        sent = datetime.now(UTC).replace(microsecond=0)
        storage = BlobInventoryStorage(BlobLocation(_URI, _azure(), ops=_ObjectOps(MemoryStore())), actor="tests")
        writer = InventoryWriter(storage)
        writer.write_catalog_json()
        httpx_mock.add_response(method="HEAD", headers={"Date": _http_date(sent)})

        root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert len(httpx_mock.get_requests()) == 1
        assert root.created_at == sent

    def test_a_clock_refusal_writes_nothing(self, httpx_mock: HTTPXMock) -> None:
        ops = RecordingOps(MemoryStore())
        storage = BlobInventoryStorage(BlobLocation(_URI, _azure(), ops=ops), actor="tests")
        writer = InventoryWriter(storage)
        writer.write_catalog_json()
        ops.puts.clear()
        sent = datetime.now(UTC) - timedelta(hours=1)
        httpx_mock.add_response(method="HEAD", headers={"Date": _http_date(sent)})

        with pytest.raises(sc.InventoryWriteRefusedError):
            writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert ops.puts == []


def _jwt(claims: dict[str, object]) -> str:
    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


class _Provider:
    def __init__(self, token: str) -> None:
        self.calls = 0
        self.token = token

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        return {"token": self.token, "expires_at": None}


class TestTokenIdentity:
    def test_it_returns_the_credential_unchanged_and_keeps_the_oid(self) -> None:
        credential = {"token": _jwt({"oid": "object-id"}), "expires_at": _NOW}
        identity = TokenIdentity(lambda: credential)
        assert identity() is credential
        assert identity.object_id() == "object-id"

    def test_object_id_fetches_a_token_when_none_has_been_fetched(self) -> None:
        provider = _Provider(_jwt({"oid": "object-id"}))
        identity = TokenIdentity(provider)
        assert provider.calls == 0
        assert identity.object_id() == "object-id"
        assert identity.object_id() == "object-id"
        assert provider.calls == 1

    def test_token_asks_the_provider_on_each_use(self) -> None:
        provider = _Provider("opaque")
        identity = TokenIdentity(provider)
        assert identity.token() == "opaque"
        assert identity.token() == "opaque"
        assert provider.calls == 2

    @pytest.mark.parametrize(
        "token",
        ["opaque", "a.b", "a.!!!.c", _jwt({"sub": "x"}), _jwt({"oid": ""}), _jwt({"oid": 7}), "a.W10.c"],
        ids=["opaque", "two-parts", "bad-base64", "no-oid", "empty-oid", "number-oid", "array-payload"],
    )
    def test_a_token_with_no_usable_oid_has_no_object_id(self, token: str) -> None:
        assert TokenIdentity(_Provider(token)).object_id() is None

    @pytest.mark.parametrize(
        "returned",
        [{"access_key": "k"}, {"sas_token": "s"}, {"token": "t"}, {"token": 7, "expires_at": None}, "token"],
        ids=["key", "sas", "no-expiry", "number", "string"],
    )
    def test_another_shape_raises(self, returned: object) -> None:
        with pytest.raises(TypeError, match="synchronous provider"):
            TokenIdentity(lambda: returned)()

    def test_an_async_provider_raises(self) -> None:
        async def provider() -> dict[str, object]:
            return {"token": "t", "expires_at": None}

        with pytest.raises(TypeError, match="coroutine"):
            TokenIdentity(provider)()

    def test_the_repr_holds_no_token(self) -> None:
        identity = TokenIdentity(_Provider("secret-token"))
        identity()
        assert "secret-token" not in repr(identity)

    def test_the_store_returns_the_same_identity_and_calls_nothing(self) -> None:
        provider = _Provider(_jwt({"oid": "object-id"}))
        identity = TokenIdentity(provider)
        store = AzureStore("catalogs", prefix="stc", account_name="acct", credential_provider=identity)
        assert store.credential_provider is identity
        assert provider.calls == 0


def _with_identity(provider: _Provider, *, actor: str | None = None) -> BlobInventoryStorage:
    store = AzureStore("catalogs", account_name="acct", credential_provider=TokenIdentity(provider))
    return BlobInventoryStorage(
        BlobLocation(_URI, store, ops=_ObjectOps(MemoryStore())), actor=actor, clock=lambda: _NOW
    )


class TestActor:
    def test_the_oid_is_recorded(self) -> None:
        writer = InventoryWriter(_with_identity(_Provider(_jwt({"oid": "object-id"}))))
        writer.write_catalog_json()
        root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        assert root.event.actor == "object-id"

    def test_the_oid_outranks_a_given_actor(self) -> None:
        storage = _with_identity(_Provider(_jwt({"oid": "object-id"})), actor="given")
        assert storage.actor() == "object-id"

    def test_a_token_with_no_oid_falls_through_to_the_given_actor(self) -> None:
        assert _with_identity(_Provider("opaque"), actor="given").actor() == "given"

    def test_a_given_actor_is_recorded_verbatim(self, writer: InventoryWriter) -> None:
        root = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        assert root.event.actor == "tests"

    def test_it_resolves_once(self) -> None:
        provider = _Provider(_jwt({"oid": "object-id"}))
        storage = _with_identity(provider)
        assert storage.actor() == storage.actor()
        assert provider.calls == 1

    def test_no_actor_refuses_with_no_write_recorded(self) -> None:
        ops = RecordingOps(MemoryStore())
        storage = BlobInventoryStorage(BlobLocation(_URI, ops._store, ops=ops), clock=lambda: _NOW)
        writer = InventoryWriter(storage)
        writer.write_catalog_json()
        ops.puts.clear()

        with pytest.raises(sc.InventoryWriteRefusedError, match=r"TokenIdentity.*actor="):
            writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")

        assert ops.puts == []


class TestCommit:
    """The writer's default reader on blob reads each incoming manifest through the storage (Phase 5 D6)."""

    def test_an_incoming_entry_with_no_manifest_raises_manifest_error_and_writes_nothing(
        self, writer: InventoryWriter, ops: RecordingOps
    ) -> None:
        writer.try_commit(None, {"a": _leaf("a", 1), "b": _leaf("b", 2)}, kind="seal", reason="first")
        ops.reads.clear()
        ops.puts.clear()

        with pytest.raises(ManifestError, match=r"products/c/.*/manifest\.json is absent"):
            writer.commit(lambda _: Change(leaves={"c": _leaf("c", 3)}), kind="seal", reason="incoming")

        assert f"products/c/{_edition(3)}/{MANIFEST_NAME}" in ops.reads
        assert ops.puts == []

    def test_an_embargoed_entry_is_incoming_too(self, writer: InventoryWriter) -> None:
        with pytest.raises(ManifestError, match="is absent"):
            writer.commit(lambda _: Change(leaves={"c": _leaf("c", 3, embargo=True)}), kind="seal", reason="incoming")

    def test_a_manifest_the_entry_does_not_record_raises_manifest_error(
        self, writer: InventoryWriter, ops: RecordingOps
    ) -> None:
        ops.put(f"products/c/{_edition(3)}/{MANIFEST_NAME}", b"{}")

        with pytest.raises(ManifestError, match=r"Inventory records 0{64}"):
            writer.commit(lambda _: Change(leaves={"c": _leaf("c", 3)}), kind="seal", reason="incoming")

        assert not writer.has_version()

    def test_no_incoming_entry_commits(self, writer: InventoryWriter) -> None:
        writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        root = writer.commit(lambda _: Change(leaves={}, embargo=True), kind="embargo-change", reason="embargo on")
        assert root.seq == 2
        assert root.embargo

    def test_given_artefacts_run_the_rules(self, storage: BlobInventoryStorage) -> None:
        writer = InventoryWriter(storage, artefacts=lambda *_: {})
        writer.write_catalog_json()
        root = writer.commit(lambda _: Change(leaves={"a": _leaf("a", 1)}), kind="seal", reason="first")
        assert root.seq == 1


class TestEndpoint:
    def test_the_account_url_on_a_store_built_as_the_cli_builds_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
        store = AzureStore.from_url("az://catalogs/stc/deep", account_name="acct")
        assert store.prefix == "stc/deep"
        assert _endpoint(store) == "https://acct.blob.core.windows.net"

    def test_a_given_endpoint_wins(self) -> None:
        store = AzureStore(
            "catalogs", account_name="acct", endpoint="http://127.0.0.1:1/acct", client_options={"allow_http": True}
        )
        assert _endpoint(store) == "http://127.0.0.1:1/acct"

    def test_an_azure_store_with_the_account_only_in_the_environment_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
        store = AzureStore.from_url("az://catalogs/stc")
        with pytest.raises(ValueError, match="account_name"):
            _endpoint(store)
        with pytest.raises(ValueError, match="account_name"):
            BlobInventoryStorage(BlobLocation(_URI, store), actor="tests").now()

    def test_s3s_default(self) -> None:
        assert _endpoint(S3Store("bucket", region="eu-west-1")) == "https://s3.eu-west-1.amazonaws.com"

    def test_an_s3_endpoint_wins(self) -> None:
        store = S3Store("bucket", region="r", endpoint="http://127.0.0.1:2", client_options={"allow_http": True})
        assert _endpoint(store) == "http://127.0.0.1:2"

    def test_an_s3_store_with_no_region_raises(self) -> None:
        with pytest.raises(ValueError, match="region"):
            _endpoint(S3Store("bucket"))

    def test_a_memory_store_has_none(self) -> None:
        with pytest.raises(ValueError, match="no endpoint"):
            _endpoint(MemoryStore())


class TestSeam:
    def test_both_storages_satisfy_the_protocol(self, storage: BlobInventoryStorage, tmp_path) -> None:
        assert isinstance(storage, InventoryStorage)
        assert isinstance(LocalInventoryStorage(tmp_path), InventoryStorage)

    def test_a_path_builds_a_local_storage(self, tmp_path) -> None:
        writer = InventoryWriter(tmp_path)
        assert isinstance(writer.storage, LocalInventoryStorage)
        assert writer.root == tmp_path
        assert writer.storage.location == str(tmp_path)

    def test_root_raises_on_blob(self, writer: InventoryWriter) -> None:
        with pytest.raises(TypeError, match=_URI):
            _ = writer.root

    def test_the_storage_is_reachable(self, storage: BlobInventoryStorage) -> None:
        assert InventoryWriter(storage).storage is storage

    def test_the_location_is_the_uri(self, storage: BlobInventoryStorage) -> None:
        assert storage.location == _URI

    def test_write_catalog_json_twice_with_one_id_raises(self, storage: BlobInventoryStorage) -> None:
        writer = InventoryWriter(storage)
        document = writer.write_catalog_json(catalog_id="01a18e00-0000-7000-8000-000000000001")
        with pytest.raises(FileExistsError, match=CATALOG_NAME):
            writer.write_catalog_json(catalog_id=document.catalog_id)

    def test_a_missing_catalog_json_raises_file_not_found(self, storage: BlobInventoryStorage) -> None:
        writer = InventoryWriter(storage)
        with pytest.raises(FileNotFoundError, match=CATALOG_NAME):
            writer.catalog_document()
        with pytest.raises(FileNotFoundError, match=CATALOG_NAME):
            writer.check_writable(None)

    def test_a_removed_base_is_a_conflict(self, writer: InventoryWriter, ops: RecordingOps) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        ops.delete(root_path(1))
        with pytest.raises(InventoryConflictError, match="no longer exists"):
            writer.try_commit(first, {"b": _leaf("b", 2)}, kind="seal", reason="second")

    def test_history_fields_read_through_the_storage(self, writer: InventoryWriter, ops: RecordingOps) -> None:
        first = writer.try_commit(None, {"a": _leaf("a", 1)}, kind="seal", reason="first")
        second = writer.try_commit(first, {"a": _leaf("a", 2)}, kind="seal", reason="second")
        raw = ops.read(root_path(1))
        assert raw is not None
        assert second.previous_root_sha256 == sha256_hex(raw[0])
        assert ops.reads.count(first.products["a"].leaf) >= 1

    def test_the_location_keeps_secrets_out_of_its_repr(self) -> None:
        location = BlobLocation(
            _URI, MemoryStore(), secrets=(sl.StoreSecret("azure", options={"connection_string": "hunter2"}),)
        )
        assert "hunter2" not in repr(location)

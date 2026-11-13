"""``statcat admin create``: a new catalog at an ``az://`` URL, its refusals and its exit codes (GH-159).

``az://`` builds an `AzureStore` that sends no request: every store call goes to
a `MemoryStore` through a `RecordingOps`. The command reads no storage clock and
asks for no token. `pytest-httpx` is active in every test, so a request to any
real host fails the test.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, override

import attrs
import msgspec
import pytest
from azure.core.exceptions import ClientAuthenticationError
from obstore.store import AzureStore, MemoryStore
from sdmxlib.inventory import CATALOG_NAME, decode_catalog, encode
from typer.testing import CliRunner

from statcatlib.cli import app
from statcatlib.store.blob import BlobLocation, BlobStoreError, _ObjectOps
from tests._gc import RecordingOps

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_httpx import HTTPXMock
    from typer.testing import Result

pytestmark = pytest.mark.unit

runner = CliRunner()

#: `statcat upload`'s refused variables, which ``admin create`` refuses too.
_REFUSED = ("AZURE_STORAGE_ACCOUNT_KEY", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_ENDPOINT")

_EDITION = "01a18e00-0000-7000-8000-000000000001"

#: The ``catalog_id`` of the run that wins the race.
_RIVAL = "01a18e00-0000-7000-8000-00000000000f"


class _Credential:
    """Stands for `DefaultAzureCredential`; the store the test builds never asks it."""

    def get_token(self, *_scopes: str, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("no token may be asked for")


@attrs.define
class _Remote:
    """What ``az://catalogs/stc`` reaches: the objects, the calls the command made, and how it built its store."""

    store: MemoryStore
    ops: RecordingOps
    built: dict[str, Any] = attrs.field(factory=dict)

    def put(self, path: str, data: bytes = b"x") -> None:
        """Write *path* past `ops`."""
        _ObjectOps(self.store).put(path, data)

    def paths(self) -> list[str]:
        """Every object the store holds."""
        return sorted(meta["path"] for meta in _ObjectOps(self.store).list(""))

    def catalog_id(self) -> str:
        got = _ObjectOps(self.store).read(CATALOG_NAME)
        assert got is not None
        return decode_catalog(got[0]).catalog_id


@pytest.fixture(autouse=True)
def _offline(httpx_mock: HTTPXMock) -> None:
    """`pytest-httpx` answers every HTTP request in this module, and fails one it was given no answer for."""
    del httpx_mock


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> _Remote:
    """An empty prefix that ``az://catalogs/stc`` reaches."""
    refused = {name.lower() for name in _REFUSED}
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in refused]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)
    store = MemoryStore()
    made = _Remote(store, RecordingOps(store))
    offline = AzureStore("catalogs", prefix="stc", account_name="acct", skip_signature=True)

    def from_url(url: str, **kwargs: object) -> AzureStore:
        made.built.update(kwargs, url=url)
        return offline

    monkeypatch.setattr("obstore.store.from_url", from_url)
    monkeypatch.setattr("statcatlib.cli.BlobLocation", lambda uri, store: BlobLocation(uri, store, ops=made.ops))
    return made


def _create(*args: str) -> Result:
    return runner.invoke(app, ["admin", "create", *args])


class TestCreate:
    def test_it_writes_catalog_json_and_prints_the_catalog_id(self, remote: _Remote) -> None:
        result = _create("az://catalogs/stc")

        assert result.exit_code == 0, result.output
        assert remote.paths() == [CATALOG_NAME]
        catalog_id = remote.catalog_id()
        assert uuid.UUID(catalog_id).version == 7
        assert result.stdout == f"{catalog_id}\n"
        assert f"Created the catalog az://catalogs/stc/ with catalog_id {catalog_id}." in result.stderr
        assert remote.ops.called("put") == [CATALOG_NAME]

    def test_the_store_is_built_as_the_other_admin_commands_build_it(self, remote: _Remote) -> None:
        result = _create("az://catalogs/stc/")

        assert result.exit_code == 0, result.output
        assert remote.built["url"] == "az://catalogs/stc"
        assert remote.built["account_name"] == "acct"
        assert remote.built["client_options"] == {"timeout": "5m"}
        assert "credential_provider" in remote.built

    def test_the_help_renders(self) -> None:
        result = _create("--help")
        assert result.exit_code == 0, result.output
        assert "AZ_URL" in result.output
        assert "create" in runner.invoke(app, ["admin", "--help"]).output


class TestExit1:
    def test_a_second_run_names_the_catalog_id_and_writes_nothing(self, remote: _Remote) -> None:
        assert _create("az://catalogs/stc").exit_code == 0
        catalog_id = remote.catalog_id()
        remote.ops.calls.clear()

        result = _create("az://catalogs/stc")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "ERROR az://catalogs/stc/ is a catalog already" in result.stderr
        assert catalog_id in result.stderr
        assert "Traceback" not in result.output
        assert remote.ops.called("put") == []
        assert remote.catalog_id() == catalog_id

    @pytest.mark.parametrize(
        "path",
        [
            "inventory/roots/00000000000000000001.json",
            f"products/p/{_EDITION}/manifest.json",
            f"pending/p/{_EDITION}.json",
        ],
        ids=["inventory", "products", "pending"],
    )
    def test_a_prefix_holding_a_catalogs_objects_and_no_catalog_json_is_refused(
        self, remote: _Remote, path: str
    ) -> None:
        remote.put(path)

        result = _create("az://catalogs/stc")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert f"ERROR az://catalogs/stc/ holds {path} and no catalog.json" in result.stderr
        assert remote.ops.called("put") == []
        assert remote.paths() == [path]

    def test_a_store_fault_exits_1(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch) -> None:
        def busy(path: str) -> Any:
            msg = f"read {path}: ServerBusy"
            raise BlobStoreError(msg, path=path)

        monkeypatch.setattr(remote.ops, "read", busy)

        result = _create("az://catalogs/stc")

        assert result.exit_code == 1
        assert "ERROR read catalog.json: ServerBusy" in result.stderr
        assert remote.paths() == []

    def test_a_credential_error_exits_1(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_credential(_path: str) -> Any:
            raise ClientAuthenticationError(message="DefaultAzureCredential failed to retrieve a token")

        monkeypatch.setattr(remote.ops, "read", no_credential)

        result = _create("az://catalogs/stc")

        assert result.exit_code == 1
        assert "ERROR DefaultAzureCredential failed" in result.stderr
        assert remote.paths() == []

    def test_a_create_that_loses_the_race_names_the_winner(
        self, remote: _Remote, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Racing(RecordingOps):
            """Another run's ``catalog.json`` lands just before this run's Put."""

            @override
            def put(self, path: str, data: bytes, **kwargs) -> str | None:
                if not self.called("put"):
                    rival = msgspec.structs.replace(decode_catalog(data), catalog_id=_RIVAL)
                    remote.put(CATALOG_NAME, encode(rival))
                return super().put(path, data, **kwargs)

        monkeypatch.setattr(remote, "ops", Racing(remote.store))

        result = _create("az://catalogs/stc")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert f"with catalog_id {_RIVAL}" in result.stderr
        assert remote.catalog_id() == _RIVAL


class TestExit2:
    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refused_variable_exits_2(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        monkeypatch.setenv(name, "")

        result = _create("az://catalogs/stc")

        assert result.exit_code == 2
        assert name in result.output
        assert "admin create writes with its own credential" in result.output
        assert remote.built == {}
        assert remote.ops.calls == []

    def test_a_refused_variable_in_another_case_exits_2(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_storage_sas_token", "")

        result = _create("az://catalogs/stc")

        assert result.exit_code == 2
        assert "AZURE_storage_sas_token" in result.output
        assert remote.ops.calls == []

    def test_a_directory_exits_2_and_stays_empty(self, remote: _Remote, tmp_path: Path) -> None:
        result = _create(str(tmp_path))

        assert result.exit_code == 2
        assert "Catalog.create(path) creates a local catalog" in result.output
        assert list(tmp_path.iterdir()) == []
        assert remote.ops.calls == []

    def test_an_s3_url_exits_2(self, remote: _Remote) -> None:
        result = _create("s3://bucket/stc")

        assert result.exit_code == 2
        assert "W3" in result.output
        assert remote.ops.calls == []

    def test_a_url_with_a_query_exits_2(self, remote: _Remote) -> None:
        result = _create("az://catalogs/stc?x=1")

        assert result.exit_code == 2
        assert "expected az://<container>[/<prefix>]" in result.output

    def test_no_account_exits_2(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME")

        result = _create("az://catalogs/stc")

        assert result.exit_code == 2
        assert "AZURE_STORAGE_ACCOUNT_NAME" in result.output
        assert remote.ops.calls == []

    def test_a_store_other_than_azure_exits_2(self, remote: _Remote, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: MemoryStore())

        result = _create("az://catalogs/stc")

        assert result.exit_code == 2
        assert "admin create supports AzureStore only" in result.output
        assert remote.ops.calls == []

    def test_no_url_is_a_usage_error(self, remote: _Remote) -> None:
        assert _create().exit_code == 2
        assert remote.ops.calls == []

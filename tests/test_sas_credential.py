"""``STATCAT_UNSAFE_AZURE_SAS``: a publisher signs with a container SAS instead of `DefaultAzureCredential`."""

from __future__ import annotations

import http.server
import logging
import os
import threading
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import obstore
import pytest
from obstore.exceptions import GenericError
from obstore.store import AzureStore
from pytest_httpx import HTTPXMock
from sdmxlib.edition import RemoteFile
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib import cli
from statcatlib.cli import app
from statcatlib.store.blob import (
    AzureBlobRest,
    BlobInventoryStorage,
    BlobLocation,
    BlobStoreError,
    TokenIdentity,
    _store_faults,
)
from statcatlib.upload import UploadReport, upload_edition

pytestmark = pytest.mark.unit

runner = CliRunner()

_SAS = "sp=racwl&se=2027-01-01T00:00:00Z&spr=https&sv=2026-02-06&sr=c&sig=abc%2Bdef%3D"
_URL = "https://acct.blob.core.windows.net/catalogs/stc/products/p/e/manifest.json"


def _sas_location(sas: str = _SAS) -> BlobLocation:
    return BlobLocation("az://catalogs/stc/", AzureStore("catalogs", prefix="stc", account_name="acct", sas_key=sas))


class TestTheDigestRead:
    @pytest.mark.parametrize("given", [_SAS, f"?{_SAS}"])
    def test_it_signs_with_the_sas_query_and_sends_no_bearer(self, httpx_mock: HTTPXMock, given: str) -> None:
        httpx_mock.add_response(
            method="HEAD", url=f"{_URL}?{_SAS}", headers={"Content-Length": "12", "Content-MD5": "abc="}
        )

        with AzureBlobRest(_sas_location(given)) as rest:
            found = rest.head("products/p/e/manifest.json")

        assert found == RemoteFile(12, {"md5": "abc="})
        request = httpx_mock.get_request()
        assert request is not None
        assert "authorization" not in request.headers
        assert request.headers["x-ms-version"] == "2023-11-03"

    def test_a_store_with_neither_a_token_nor_a_sas_is_refused(self) -> None:
        location = BlobLocation("az://catalogs/", AzureStore("catalogs", account_name="acct", skip_signature=True))

        with AzureBlobRest(location) as rest, pytest.raises(ValueError, match="sas_key"):
            rest.head("catalog.json")

    def test_upload_refuses_an_unsigned_store_before_any_request(self, tmp_path: Path) -> None:
        location = BlobLocation("az://catalogs/", AzureStore("catalogs", account_name="acct", skip_signature=True))

        with pytest.raises(ValueError, match=r"TokenIdentity.*or a sas_key"):
            upload_edition(tmp_path, location)

    def test_a_sas_store_names_no_actor_so_the_writer_must(self) -> None:
        assert BlobInventoryStorage(_sas_location(), actor="publisher").actor() == "publisher"
        with pytest.raises(sc.InventoryWriteRefusedError, match="actor="):
            BlobInventoryStorage(_sas_location()).actor()


class _Forbidden(http.server.BaseHTTPRequestHandler):
    """Answer every request 403, with Azure's ``AuthenticationFailed`` body."""

    def __getattr__(self, name: str) -> Callable[[], None]:
        # BaseHTTPRequestHandler dispatches each method to `do_<METHOD>`.
        if name.startswith("do_"):
            return self._refuse
        raise AttributeError(name)

    def _refuse(self) -> None:
        body = b'<?xml version="1.0"?><Error><Code>AuthenticationFailed</Code></Error>'
        self.send_response(403)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


@pytest.fixture
def forbidden() -> Iterator[str]:
    """The endpoint of a loopback server that refuses every request with 403."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Forbidden)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _sas_store(endpoint: str) -> AzureStore:
    return AzureStore(
        "catalogs",
        account_name="acct",
        sas_key=_SAS,
        endpoint=endpoint,
        client_options={"allow_http": True},
        retry_config={"max_retries": 0, "retry_timeout": timedelta(seconds=2)},
    )


_REQUESTS: list[tuple[str, Callable[[AzureStore], object]]] = [
    ("read", lambda store: obstore.get(store, "catalog.json").bytes()),
    ("read the properties of", lambda store: obstore.head(store, "catalog.json")),
    ("create", lambda store: obstore.put(store, "catalog.json", b"{}", mode="create")),
    ("list", lambda store: obstore.list(store, "inventory/").collect()),
]


class TestTheSignatureStaysOutOfMessages:
    """No message or log line carries the SAS signature (``sig=abc%2Bdef%3D``)."""

    def test_the_digest_reads_log_line_redacts_it(
        self, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        httpx_mock.add_response(method="HEAD", url=f"{_URL}?{_SAS}", headers={"Content-Length": "1"})

        with caplog.at_level(logging.INFO, logger="httpx"), AzureBlobRest(_sas_location()) as rest:
            rest.head("products/p/e/manifest.json")

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "HTTP Request: HEAD" in logged
        assert "sig=REDACTED" in logged
        assert "abc%2Bdef" not in logged

    def test_a_refused_digest_read_names_the_status_alone(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(method="HEAD", url=f"{_URL}?{_SAS}", status_code=403)

        with AzureBlobRest(_sas_location()) as rest, pytest.raises(BlobStoreError) as raised:
            rest.head("products/p/e/manifest.json")

        assert str(raised.value) == "read the properties of products/p/e/manifest.json: HTTP 403"

    @pytest.mark.parametrize(("action", "call"), _REQUESTS)
    def test_obstore_redacts_the_url_of_a_refused_request(
        self, forbidden: str, action: str, call: Callable[[AzureStore], object]
    ) -> None:
        # obstore 0.12.1 writes REDACTED in place of the URL; this pins it across an obstore bump.
        with pytest.raises(BlobStoreError) as raised, _store_faults(action, "catalog.json"):
            call(_sas_store(forbidden))

        assert "403" in str(raised.value)
        assert "REDACTED" in str(raised.value)
        assert "abc%2Bdef" not in str(raised.value)

    def test_obstore_redacts_the_url_of_an_unreachable_store(self) -> None:
        with pytest.raises(BlobStoreError) as raised, _store_faults("read", "catalog.json"):
            obstore.head(_sas_store("http://127.0.0.1:9"), "catalog.json")

        assert "REDACTED" in str(raised.value)
        assert "abc%2Bdef" not in str(raised.value)

    def test_obstore_never_quotes_a_sas_it_cannot_parse(self) -> None:
        with pytest.raises(GenericError, match="Missing component in SAS query pair") as raised:
            AzureStore("catalogs", account_name="acct", sas_key="sigabc%2Bdef")

        assert "abc%2Bdef" not in str(raised.value)


class _Untouched:
    """Stands for `DefaultAzureCredential`, which a SAS run never builds."""

    def __init__(self) -> None:
        pytest.fail("DefaultAzureCredential was built under STATCAT_UNSAFE_AZURE_SAS")


@pytest.fixture
def sas_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in [n for n in os.environ if n.startswith(("AZURE_STORAGE_SAS", "STATCAT_ACTOR", "STATCAT_REFUSE_SAS"))]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setenv("STATCAT_UNSAFE_AZURE_SAS", f"?{_SAS}")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Untouched)
    return monkeypatch


class TestTheCommands:
    def test_upload_builds_a_sas_store_and_says_it_has_no_identity(self, tmp_path: Path, sas_env) -> None:
        calls: list[BlobLocation] = []

        def fake(_dir: Path, location: BlobLocation, *, release_at: object = None) -> UploadReport:
            calls.append(location)
            return UploadReport("e1", "p", ("a",), (), registered=True)

        sas_env.setattr(cli, "upload_edition", fake)

        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs/stc"])

        assert result.exit_code == 0, result.output
        (location,) = calls
        assert location.store.config["sas_key"] == _SAS
        assert not isinstance(getattr(location.store, "credential_provider", None), TokenIdentity)
        assert "identity none: signed with STATCAT_UNSAFE_AZURE_SAS" in result.output
        assert "sig=" not in result.output

    def test_upload_needs_no_actor(self, tmp_path: Path, sas_env) -> None:
        sas_env.setattr(cli, "upload_edition", lambda *_a, **_k: UploadReport("e", "p", (), (), registered=True))

        assert runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"]).exit_code == 0

    def test_an_inventory_write_under_a_sas_needs_an_actor(self, sas_env) -> None:
        result = runner.invoke(app, ["advance-current", "az://catalogs/stc"])

        assert result.exit_code == 2
        assert "STATCAT_ACTOR" in result.output

    def test_the_actor_reaches_the_admin(self, sas_env) -> None:
        sas_env.setenv("STATCAT_ACTOR", "publisher")
        seen: list[object] = []

        def fake_open(_target: object, *, actor: str | None = None) -> SimpleNamespace:
            seen.append(actor)
            return SimpleNamespace(advance_current=lambda: None)

        sas_env.setattr(sc.CatalogAdmin, "open", fake_open)

        result = runner.invoke(app, ["advance-current", "az://catalogs/stc"])

        assert result.exit_code == 0, result.output
        assert seen == ["sas:publisher"]

    def test_a_catalog_directory_takes_no_actor_even_with_a_sas_set(self, tmp_path: Path, sas_env) -> None:
        seen: list[object] = []

        def fake_open(_target: object, *, actor: str | None = None) -> SimpleNamespace:
            seen.append(actor)
            return SimpleNamespace(advance_current=lambda: None)

        sas_env.setattr(sc.CatalogAdmin, "open", fake_open)

        assert runner.invoke(app, ["advance-current", str(tmp_path)]).exit_code == 0
        assert seen == [None]

    def test_a_sas_run_has_no_credential_for_the_access_listing(self, sas_env) -> None:
        location, credential = cli._azure_clients("az://catalogs/", "acct", command="release", timeout="5m")

        assert credential is None
        assert location.store.config["sas_key"] == _SAS


class TestTheGuards:
    def test_every_sas_run_warns_on_standard_error(self, tmp_path: Path, sas_env) -> None:
        sas_env.setattr(cli, "upload_edition", lambda *_a, **_k: UploadReport("e", "p", (), (), registered=True))

        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"])

        assert result.exit_code == 0, result.output
        assert "WARNING STATCAT_UNSAFE_AZURE_SAS" in result.stderr
        assert "Anyone holding it can write this catalog" in result.stderr
        assert "WARNING" not in result.stdout

    def test_a_token_run_prints_no_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STATCAT_UNSAFE_AZURE_SAS", raising=False)
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
        monkeypatch.setattr(
            "azure.identity.DefaultAzureCredential",
            lambda: SimpleNamespace(get_token=lambda *_a, **_k: pytest.fail("no token is asked for")),
        )
        monkeypatch.setattr(cli, "upload_edition", lambda *_a, **_k: UploadReport("e", "p", (), (), registered=True))
        monkeypatch.setattr(cli, "_token_identity", lambda _store: None)

        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"])

        assert "WARNING" not in result.output

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    @pytest.mark.parametrize("args", [["upload", ".", "az://catalogs"], ["advance-current", "az://catalogs"]])
    def test_the_kill_switch_refuses_a_sas_before_any_request(
        self, sas_env, value: str, args: list[str], tmp_path: Path
    ) -> None:
        sas_env.setenv("STATCAT_ACTOR", "publisher")
        sas_env.setenv("STATCAT_REFUSE_SAS", value)
        sas_env.setattr(cli, "upload_edition", lambda *_a, **_k: pytest.fail("no upload under a refused SAS"))
        sas_env.setattr(sc.CatalogAdmin, "open", lambda *_a, **_k: pytest.fail("no catalog under a refused SAS"))
        sas_env.chdir(tmp_path)

        result = runner.invoke(app, args)

        assert result.exit_code == 2
        assert "STATCAT_REFUSE_SAS refuses" in result.output

    @pytest.mark.parametrize("value", ["", "0", "false", "No"])
    def test_an_off_kill_switch_lets_the_sas_through(self, tmp_path: Path, sas_env, value: str) -> None:
        sas_env.setenv("STATCAT_REFUSE_SAS", value)
        sas_env.setattr(cli, "upload_edition", lambda *_a, **_k: UploadReport("e", "p", (), (), registered=True))

        assert runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"]).exit_code == 0

    def test_the_kill_switch_leaves_a_token_run_alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("STATCAT_UNSAFE_AZURE_SAS", raising=False)
        monkeypatch.setenv("STATCAT_REFUSE_SAS", "1")

        assert cli._sas("upload") is None

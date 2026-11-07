"""``statcat upload``: its arguments, its environment, its exit codes (Phase 4 Part 2, D7)."""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ClientAuthenticationError
from pytest_httpx import HTTPXMock
from sdmxlib.edition import Finding, ManifestError
from typer.testing import CliRunner

from statcatlib import cli
from statcatlib.cli import app
from statcatlib.store.blob import BlobLocation, BlobStoreError, TokenIdentity
from statcatlib.upload import UploadCheckError, UploadReport

pytestmark = pytest.mark.unit

runner = CliRunner()

_OID = "11111111-2222-3333-4444-555555555555"

#: Spelled out, so dropping a name from the command's collection fails a test.
_REFUSED = (
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_ACCESS_KEY",
    "AZURE_STORAGE_MASTER_KEY",
    "AZURE_STORAGE_SAS_KEY",
    "AZURE_STORAGE_SAS_TOKEN",
    "AZURE_STORAGE_TOKEN",
    "AZURE_STORAGE_ENDPOINT",
    "AZURE_ENDPOINT",
    "AZURE_STORAGE_USE_EMULATOR",
    "AZURE_USE_FABRIC_ENDPOINT",
    "AZURE_SKIP_SIGNATURE",
    "AZURE_STORAGE_ENCRYPTION_KEY",
)

#: Spellings `obstore` reads as a refused key: ``AZURE_``, then any case.
_MIXED_CASE = ("AZURE_storage_endpoint", "AZURE_Endpoint", "AZURE_Storage_Use_Emulator", "AZURE_skip_signature")


def _jwt(claims: dict[str, str]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


class _Credential:
    """Stands for `DefaultAzureCredential`: a token with a hand-made ``oid``."""

    def get_token(self, *_scopes: str, **_kwargs: object) -> SimpleNamespace:
        expires = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
        return SimpleNamespace(token=_jwt({"oid": _OID}), expires_on=expires)


class _NoCredential:
    def get_token(self, *_scopes: str, **_kwargs: object) -> SimpleNamespace:
        msg = "no credential in this environment"
        raise ClientAuthenticationError(msg)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """An account and a stand-in credential, with no refused variable set in any spelling."""
    refused = {name.lower() for name in _REFUSED}
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in refused]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)
    return monkeypatch


@pytest.fixture
def captured(env: pytest.MonkeyPatch) -> list[tuple[Path, BlobLocation, datetime | None]]:
    """Replaces `upload_edition` with a recorder that reports one sent and one kept file."""
    calls: list[tuple[Path, BlobLocation, datetime | None]] = []

    def fake(edition_dir: Path, location: BlobLocation, *, release_at: datetime | None = None) -> UploadReport:
        calls.append((edition_dir, location, release_at))
        return UploadReport("e1", "p", ("a",), ("b",), registered=True)

    env.setattr(cli, "upload_edition", fake)
    return calls


def _raising(env: pytest.MonkeyPatch, exc: Exception) -> None:
    def fail(*_args: object, **_kwargs: object) -> UploadReport:
        raise exc

    env.setattr(cli, "upload_edition", fail)


class TestSuccess:
    def test_it_builds_the_location_as_the_design_says(self, tmp_path: Path, captured) -> None:
        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs/stc", "--release-at", "2026-11-05T13:30Z"])

        assert result.exit_code == 0, result.output
        ((edition_dir, location, release_at),) = captured
        assert edition_dir == tmp_path
        assert release_at == datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
        assert location.uri == "az://catalogs/stc/"
        store = location.store
        assert isinstance(store.credential_provider, TokenIdentity)
        assert store.client_options["timeout"] == "1h"
        assert store.prefix == "stc"
        assert store.config["account_name"] == "acct"
        assert store.config["container_name"] == "catalogs"

    def test_it_prints_the_oid_the_counts_and_the_registration(self, tmp_path: Path, captured) -> None:
        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"])

        assert result.exit_code == 0, result.output
        assert _OID in result.output
        assert "1 sent, 1 kept" in result.output
        assert "registered in pending/" in result.output

    def test_an_already_registered_edition_exits_0_and_says_so(self, tmp_path: Path, env) -> None:
        env.setattr(cli, "upload_edition", lambda *_a, **_k: UploadReport("e", "p", (), ("a",), registered=False))

        result = runner.invoke(app, ["upload", str(tmp_path), "az://catalogs"])

        assert result.exit_code == 0
        assert "already registered" in result.output

    @pytest.mark.parametrize(
        ("given", "uri", "prefix"),
        [
            ("az://c", "az://c/", None),
            ("az://c/", "az://c/", None),
            ("az://c/p/q", "az://c/p/q/", "p/q"),
            ("az://c/p/", "az://c/p/", "p"),
        ],
    )
    def test_the_url_grammar(self, tmp_path: Path, captured, given: str, uri: str, prefix: str | None) -> None:
        result = runner.invoke(app, ["upload", str(tmp_path), given])

        assert result.exit_code == 0, result.output
        ((_, location, _),) = captured
        assert location.uri == uri
        assert location.store.prefix == prefix

    def test_no_release_time_passes_none(self, tmp_path: Path, captured) -> None:
        runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert captured[0][2] is None

    def test_an_offset_is_kept_aware(self, tmp_path: Path, captured) -> None:
        runner.invoke(app, ["upload", str(tmp_path), "az://c", "--release-at", "2026-11-05T08:30:00-05:00"])

        assert captured[0][2] == datetime(2026, 11, 5, 13, 30, tzinfo=UTC)

    def test_the_help_renders(self) -> None:
        """Rich reads ``[/...]`` in an option's help as a closing tag, and a stray one raises `MarkupError`."""
        result = runner.invoke(app, ["upload", "--help"])

        assert result.exit_code == 0, result.output
        assert "EDITION_DIR" in result.output
        assert "AZ_URL" in result.output

    def test_the_app_shows_no_locals_in_a_traceback(self) -> None:
        assert app.pretty_exceptions_show_locals is False

    @pytest.mark.parametrize("name", ["azure_storage_endpoint", "azure_skip_signature", "Azure_Endpoint"])
    def test_a_name_obstore_ignores_is_accepted(self, tmp_path: Path, env, captured, name: str) -> None:
        """`from_env` reads only names that start with ``AZURE_`` in capitals."""
        env.setenv(name, "https://elsewhere.example")

        result = runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert result.exit_code == 0, result.output
        assert len(captured) == 1


class TestExit1:
    def test_findings_print_as_fail_lines(self, tmp_path: Path, env) -> None:
        _raising(env, UploadCheckError([Finding("remote", "data/a.parquet", "missing from the store")]))

        result = runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert result.exit_code == 1
        assert "FAIL data/a.parquet: missing from the store" in result.output

    @pytest.mark.parametrize(
        "exc",
        [
            ManifestError("not a sealed edition"),
            BlobStoreError("read x: HTTP 403", path="x"),
            ClientAuthenticationError("no credential"),
        ],
        ids=["manifest", "store", "credential"],
    )
    def test_each_failure_prints_as_an_error_with_no_traceback(self, tmp_path: Path, env, exc: Exception) -> None:
        _raising(env, exc)

        result = runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert result.exit_code == 1
        assert result.output.startswith(f"ERROR {exc}") or f"ERROR {exc}" in result.output
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)

    def test_a_real_unsealed_directory_is_a_manifest_error(self, tmp_path: Path, env) -> None:
        result = runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert result.exit_code == 1
        assert "ERROR" in result.output
        assert "not a sealed edition" in result.output

    def test_a_credential_that_cannot_answer_exits_1(self, tmp_path: Path, env, captured) -> None:
        env.setattr("azure.identity.DefaultAzureCredential", _NoCredential)

        result = runner.invoke(app, ["upload", str(tmp_path), "az://c"])

        assert result.exit_code == 1
        assert "no credential in this environment" in result.output
        assert captured == []


class TestExit2:
    def _refused(self, tmp_path: Path, *args: str) -> str:
        result = runner.invoke(app, ["upload", str(tmp_path), *args])
        assert result.exit_code == 2, result.output
        return result.output

    def test_a_naive_time(self, tmp_path: Path, captured) -> None:
        output = self._refused(tmp_path, "az://c", "--release-at", "2026-11-05T13:30:00")

        assert "no time zone" in output
        assert captured == []

    def test_an_unparseable_time(self, tmp_path: Path, captured) -> None:
        assert "RFC 3339" in self._refused(tmp_path, "az://c", "--release-at", "soon")

    @pytest.mark.parametrize(
        "url",
        [
            "catalogs",
            "https://acct.blob.core.windows.net/c",
            "az://",
            "az:///p",
            "az://c//p",
            "az://c/../p",
            "az://c?x=1",
        ],
    )
    def test_a_bad_url(self, tmp_path: Path, captured, url: str) -> None:
        assert "az://<container>" in self._refused(tmp_path, url)
        assert captured == []

    def test_an_s3_url_names_w3(self, tmp_path: Path, captured) -> None:
        assert "W3" in self._refused(tmp_path, "s3://bucket/p")

    def test_no_account(self, tmp_path: Path, env, captured) -> None:
        env.delenv("AZURE_STORAGE_ACCOUNT_NAME")

        assert "AZURE_STORAGE_ACCOUNT_NAME" in self._refused(tmp_path, "az://c")

    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refused_variable(self, tmp_path: Path, env, captured, name: str) -> None:
        env.setenv(name, "secret-value")

        output = self._refused(tmp_path, "az://c")

        assert name in output
        assert "secret-value" not in output
        assert captured == []

    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refused_variable_set_empty_counts(self, tmp_path: Path, env, captured, name: str) -> None:
        env.setenv(name, "")

        assert name in self._refused(tmp_path, "az://c")
        assert captured == []

    @pytest.mark.parametrize(
        "name", ["AZURE_STORAGE_USE_EMULATOR", "AZURE_USE_FABRIC_ENDPOINT", "AZURE_SKIP_SIGNATURE"]
    )
    def test_a_switch_set_to_false_counts(self, tmp_path: Path, env, captured, name: str) -> None:
        """Refusal is by presence, so ``false`` exits 2, where a value obstore cannot parse raised a traceback."""
        env.setenv(name, "false")

        assert name in self._refused(tmp_path, "az://c")
        assert captured == []

    @pytest.mark.parametrize("value", ["https://elsewhere.example", ""], ids=["set", "empty"])
    @pytest.mark.parametrize("name", _MIXED_CASE)
    def test_a_mixed_case_spelling_is_refused_as_spelled(
        self, tmp_path: Path, env, captured, httpx_mock: HTTPXMock, name: str, value: str
    ) -> None:
        def untouched(*_args: object, **_kwargs: object) -> None:
            pytest.fail("the refusal comes first")

        env.setattr("azure.identity.DefaultAzureCredential", untouched)
        env.setattr("obstore.store.from_url", untouched)
        env.setenv(name, value)

        output = self._refused(tmp_path, "az://c")

        assert name in output
        assert name.upper() not in output
        assert captured == []
        assert httpx_mock.get_requests() == []

    def test_the_message_names_only_the_variables_that_are_set(self, tmp_path: Path, env, captured) -> None:
        env.setenv("AZURE_ENDPOINT", "https://elsewhere.example")

        output = self._refused(tmp_path, "az://c")

        assert "AZURE_ENDPOINT" in output
        assert "AZURE_STORAGE_TOKEN" not in output
        assert "elsewhere.example" not in output

    @pytest.mark.parametrize("value", ["secret-value", ""], ids=["set", "empty"])
    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refusal_builds_no_credential_or_store_and_sends_nothing(
        self, tmp_path: Path, env, captured, httpx_mock: HTTPXMock, name: str, value: str
    ) -> None:
        def untouched(*_args: object, **_kwargs: object) -> None:
            pytest.fail("the refusal comes first")

        env.setattr("azure.identity.DefaultAzureCredential", untouched)
        env.setattr("obstore.auth.azure.AzureCredentialProvider", untouched)
        env.setattr("obstore.store.from_url", untouched)
        env.setenv(name, value)

        self._refused(tmp_path, "az://c")

        assert captured == []
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("module", ["azure.identity", "obstore.auth.azure", "obstore.store"])
    def test_a_missing_extra(self, tmp_path: Path, env, captured, module: str) -> None:
        env.setitem(sys.modules, module, None)

        output = self._refused(tmp_path, "az://c")

        assert "uv add 'statcatlib[blob]'" in output
        assert "pip install 'statcatlib[blob]'" in output
        assert captured == []

    def test_an_import_error_from_the_upload(self, tmp_path: Path, env) -> None:
        _raising(env, ImportError("object storage needs the blob extra"))

        assert "blob extra" in self._refused(tmp_path, "az://c")

    def test_a_missing_directory(self, tmp_path: Path, captured) -> None:
        result = runner.invoke(app, ["upload", str(tmp_path / "nothing"), "az://c"])

        assert result.exit_code == 2

"""``statcat release``: its exit codes, the refused variables, and standard output holding the events alone (D1, D15).

``az://`` builds an `AzureStore` that sends no request: every store call goes to
the `MemoryStore` of a `ReleaseCatalog`, the listing and the digest read are the
fakes of `tests/_fakes.py`, and the storage clock is the test's. `pytest-httpx`
is active in every test, so a request to any real host fails the test.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING, Any, Self

import attrs
import msgspec
import pytest
from azure.core.exceptions import ClientAuthenticationError
from obstore.store import AzureStore, MemoryStore
from pytest_httpx import HTTPXMock
from sdmxlib.inventory import CATALOG_NAME
from typer.testing import CliRunner

from statcatlib.access import ARM_SCOPE
from statcatlib.cli import app
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, BlobStoreError
from tests._fakes import ACCOUNT, SENTINEL, RecordingRest, grant
from tests._release import PODS, RELEASE_JOB, ReleaseCatalog

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

pytestmark = pytest.mark.unit

runner = CliRunner()

_HOUR = timedelta(hours=1)

_CONFIG = f"""\
format_version = 1

[retention]
backup = "30d"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
retire_after = "90d"
pod_staleness = "5m"

[access]
storage_account = "{ACCOUNT}"
container = "catalogs"
allowlist = [
  {{ object_id = "{RELEASE_JOB}", name = "release job" }},
  {{ object_id = "{PODS}", name = "pods" }},
]
sentinels = ["{SENTINEL}"]
scopes = ["/subscriptions/sub"]
"""

_REFUSED = ("AZURE_STORAGE_ACCOUNT_KEY", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_USE_EMULATOR")


@attrs.frozen
class _Token:
    token: str


#: The scopes each `_Credential` was asked a token for.
_ASKED: list[tuple[str, ...]] = []


class _Credential:
    """Stands for `DefaultAzureCredential`: it hands out one token and records the scopes asked in `_ASKED`."""

    def get_token(self, *scopes: str, **_kwargs: object) -> _Token:
        _ASKED.append(scopes)
        return _Token("arm-token")


class _Rest(RecordingRest):
    """`RecordingRest` as the context manager `AzureBlobRest` is."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@attrs.define
class _Built:
    """What the command built its listing with."""

    arguments: list[tuple[tuple[Any, ...], dict[str, Any]]] = attrs.field(factory=list)


@pytest.fixture(autouse=True)
def _offline(httpx_mock: HTTPXMock) -> None:
    """`pytest-httpx` answers every HTTP request in this module, and fails one it was given no answer for."""
    del httpx_mock


@pytest.fixture
def built() -> _Built:
    return _Built()


@pytest.fixture
def unclocked(monkeypatch: pytest.MonkeyPatch, built: _Built) -> ReleaseCatalog:
    """A catalog ``az://catalogs/stc`` reaches, whose storage clock is the ``HEAD`` that `pytest-httpx` answers."""
    refused = {name.lower() for name in _REFUSED}
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in refused]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    _ASKED.clear()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)
    made = ReleaseCatalog()
    offline = AzureStore("catalogs", prefix="stc", account_name="acct", skip_signature=True)
    monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: offline)
    monkeypatch.setattr("statcatlib.cli.BlobLocation", lambda uri, store: BlobLocation(uri, store, ops=made.ops))
    monkeypatch.setattr(BlobInventoryStorage, "actor", lambda _self: "release-oid")

    def listing(*args: Any, **kwargs: Any) -> Any:
        built.arguments.append((args, kwargs))
        return made.listing

    monkeypatch.setattr("statcatlib.cli.AzureReaderListing", listing)
    monkeypatch.setattr("statcatlib.cli.AzureBlobRest", lambda _location: _Rest(made.store))
    return made


@pytest.fixture
def cat(unclocked: ReleaseCatalog, monkeypatch: pytest.MonkeyPatch) -> ReleaseCatalog:
    """The catalog of `unclocked`, with the storage clock at `ReleaseCatalog.now`."""
    monkeypatch.setattr(BlobInventoryStorage, "now", lambda _self: unclocked.now)
    return unclocked


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.toml"
    path.write_text(_CONFIG)
    return path


def _release(*args: str) -> Result:
    return runner.invoke(app, ["release", *args])


def _lines(result: Result) -> list[dict[str, Any]]:
    """Standard output, each line parsed: it holds JSON lines and nothing else."""
    return [json.loads(line) for line in result.stdout.splitlines()]


class TestARun:
    def test_standard_output_holds_the_events_alone_and_standard_error_the_summary(
        self, cat: ReleaseCatalog, config: Path
    ) -> None:
        folded = cat.upload("p", release_at=cat.now + _HOUR)
        bad = cat.upload("bad", previous=cat.new_id())

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        lines = _lines(result)
        assert [line["event"] for line in lines] == ["jobs.config", "fold.refused"]
        first = lines[0]
        assert first["catalog"] == "az://catalogs/stc/"
        assert first["detail"] == {"path": str(config), "sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
        refused = lines[1]
        assert (refused["product"], refused["edition"], refused["rule"]) == ("bad", bad, "chain")
        assert f"REFUSED bad/{bad} chain: " in result.stderr
        assert "Release folded 1 registration(s), released 0 edition(s) and retired 0; 1 refused" in result.stderr
        assert "Inventory version(s) written: 1, 2." in result.stderr
        assert cat.states("p") == {folded: "embargoed"}
        assert cat.full().root.event.actor == "release-oid"

    def test_the_listing_takes_the_configurations_scope_and_a_resource_manager_token(
        self, cat: ReleaseCatalog, config: Path, built: _Built
    ) -> None:
        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        [(args, kwargs)] = built.arguments
        assert args == (ACCOUNT, "catalogs")
        assert kwargs["scopes"] == ("/subscriptions/sub",)
        assert kwargs["arm_token"]() == "arm-token"
        assert _ASKED == [(ARM_SCOPE,)]

    def test_a_release_refusal_with_no_rule_prints_a_dash(self, cat: ReleaseCatalog, config: Path) -> None:
        first = cat.live("p")
        second = cat.live("p", previous=first)
        at = cat.now + _HOUR
        third = cat.upload("p", previous=second, release_at=at)
        cat.run()
        cat.admin().rollback("p", reason="tests")
        cat.now = at + timedelta(seconds=5)

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        assert f"REFUSED p/{third} -: " in result.stderr
        events = [line["event"] for line in _lines(result)]
        assert events == ["jobs.config", "release.refused", "release.overdue"]
        assert _lines(result)[1]["rule"] is None

    def test_a_failed_access_check_is_reported_and_leaves_exit_0(self, cat: ReleaseCatalog, config: Path) -> None:
        due = cat.upload("p", release_at=cat.now)
        cat.listing.grants = (grant(RELEASE_JOB), grant("oid-analyst", principal_type="User"))

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        assert "SKIPPED release: the access check failed" in result.stderr
        assert "OUTSIDER oid-analyst (User) holds Storage Blob Data Reader" in result.stderr
        assert f"UNUSED {PODS} (pods)" in result.stderr
        assert "access.mismatch" in [line["event"] for line in _lines(result)]
        assert cat.states("p") == {due: "embargoed"}

    def test_a_dry_run_writes_nothing(self, cat: ReleaseCatalog, config: Path) -> None:
        cat.upload("p")
        cat.ops.calls.clear()

        result = _release("az://catalogs/stc", "--config", str(config), "--dry-run")

        assert result.exit_code == 0, result.output
        assert "Release would fold 1 registration(s)" in result.stderr
        assert "This dry run wrote nothing." in result.stderr
        assert cat.ops.called("put") == []
        assert not cat.writer.has_version()


class TestExit1:
    def test_a_failed_step_exits_1_and_the_other_steps_ran(
        self, cat: ReleaseCatalog, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        due = cat.upload("p", release_at=cat.now + _HOUR)
        cat.run()
        cat.now += 2 * _HOUR

        def failing(_prefix: str) -> Any:
            msg = "list pending: ServerBusy"
            raise BlobStoreError(msg, path="pending")

        monkeypatch.setattr(cat.ops, "list", failing)

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "ERROR fold: BlobStoreError: list pending: ServerBusy" in result.stderr
        [failed] = [line for line in _lines(result) if line["event"] == "step.failed"]
        assert (failed["severity"], failed["detail"]["step"]) == ("error", "fold")
        assert cat.states("p") == {due: "live"}

    def test_a_refused_configuration_exits_1_before_any_request(self, cat: ReleaseCatalog, config: Path) -> None:
        config.write_text(_CONFIG.replace('backup = "30d"', 'backup = "40d"'))

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "backup (40d) must not exceed gc_window (35d)" in result.stderr
        assert result.stdout == ""
        assert cat.ops.calls == []

    def test_a_configuration_with_no_access_section_exits_1(self, cat: ReleaseCatalog, config: Path) -> None:
        config.write_text(_CONFIG.partition("[access]")[0])

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "[access] is missing" in result.stderr
        assert result.stdout == ""
        assert cat.ops.calls == []

    def test_an_access_section_for_another_container_exits_1(self, cat: ReleaseCatalog, config: Path) -> None:
        config.write_text(_CONFIG.replace('container = "catalogs"', 'container = "other"'))

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "access.container is 'other'" in result.stderr
        assert cat.ops.calls == []

    def test_an_opted_in_catalog_exits_1_with_step_failed(self, cat: ReleaseCatalog, config: Path) -> None:
        cat.upload("p")
        document = msgspec.json.decode(cat.read(CATALOG_NAME))
        document |= {"format_version": "2.0", "history": {"since_seq": 1}}
        cat.put(CATALOG_NAME, msgspec.json.encode(document))

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "ERROR" in result.stderr
        assert "Part 6" in result.stderr
        failed = _lines(result)[-1]
        assert (failed["event"], failed["detail"]["step"]) == ("step.failed", "start")
        assert cat.ops.called("put") == []

    def test_a_clock_ahead_of_the_storage_clock_exits_1(
        self, unclocked: ReleaseCatalog, config: Path, httpx_mock: HTTPXMock
    ) -> None:
        unclocked.upload("p")
        behind = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)
        httpx_mock.add_response(method="HEAD", url="https://acct.blob.core.windows.net", headers={"Date": behind})

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "ahead of the storage clock" in result.stderr
        assert [line["event"] for line in _lines(result)] == ["step.failed"]
        assert _lines(result)[0]["at"] is None
        assert unclocked.ops.called("put") == []
        assert len(httpx_mock.get_requests()) == 1

    def test_a_credential_error_exits_1(
        self, cat: ReleaseCatalog, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cat.upload("p")

        def no_credential() -> Any:
            raise ClientAuthenticationError(message="DefaultAzureCredential failed to retrieve a token")

        monkeypatch.setattr(cat.listing, "readers", no_credential)

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "ERROR DefaultAzureCredential failed" in result.stderr
        assert not cat.writer.has_version()


class TestExit2:
    def test_an_s3_url_exits_2(self, cat: ReleaseCatalog, config: Path) -> None:
        result = _release("s3://bucket/stc", "--config", str(config))
        assert result.exit_code == 2
        assert "W3" in result.output

    def test_a_store_other_than_azure_exits_2(
        self, cat: ReleaseCatalog, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: MemoryStore())

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 2
        assert "release supports AzureStore only" in result.output
        assert "W3" in result.output

    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refused_variable_exits_2(
        self, cat: ReleaseCatalog, config: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, "")

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 2
        assert name in result.output
        assert "release writes with its own credential" in result.output
        assert cat.ops.calls == []

    def test_no_account_exits_2(self, cat: ReleaseCatalog, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME")

        result = _release("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 2
        assert "AZURE_STORAGE_ACCOUNT_NAME" in result.output

    def test_no_config_is_a_usage_error(self, cat: ReleaseCatalog) -> None:
        assert _release("az://catalogs/stc").exit_code == 2

    def test_the_help_renders(self) -> None:
        result = _release("--help")
        assert result.exit_code == 0, result.output
        assert "--dry-run" in result.output
        assert "--config" in result.output

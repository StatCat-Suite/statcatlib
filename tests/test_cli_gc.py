"""``statcat gc``: its exit codes, standard output holding the events alone, and the deletion log's fields (D10, D15).

``az://`` builds an `AzureStore` that sends no request: every store call goes to
the `MemoryStore` of a `GcCatalog`, and the storage clock is the test's.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from obstore.store import AzureStore, MemoryStore
from sdmxlib.inventory import root_path
from typer.testing import CliRunner

from statcatlib.cli import app
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from tests._gc import GcCatalog, edition_id

if TYPE_CHECKING:
    from pathlib import Path

    from typer.testing import Result

pytestmark = pytest.mark.unit

runner = CliRunner()

_DAY = timedelta(days=1)

_CONFIG = """\
format_version = 1

[retention]
backup = "{backup}"
gc_window = "35d"
tombstone_window = "400d"
inventory_window = "36d"
keep_roots = 2
retire_after = "90d"
pod_staleness = "5m"
"""

#: The keys of a deletion log line, and of a failed deletion's.
_LINE = {"event", "path", "reason", "edition"}

_REFUSED = ("AZURE_STORAGE_ACCOUNT_KEY", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_USE_EMULATOR")


class _Credential:
    """Stands for `DefaultAzureCredential`; nothing asks it for a token."""

    def get_token(self, *_scopes: str, **_kwargs: object) -> object:
        pytest.fail("no token may be asked for")


@pytest.fixture
def cat(monkeypatch: pytest.MonkeyPatch) -> GcCatalog:
    """A catalog ``az://catalogs/stc`` reaches: A retired 40 days ago, B live, an orphan; the clock at ``real + 2d``."""
    refused = {name.lower() for name in _REFUSED}
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in refused]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)
    built = GcCatalog()
    offline = AzureStore("catalogs", prefix="stc", account_name="acct", skip_signature=True)
    monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: offline)
    monkeypatch.setattr("statcatlib.cli.BlobLocation", lambda uri, store: BlobLocation(uri, store, ops=built.ops))
    monkeypatch.setattr(BlobInventoryStorage, "now", lambda _self: built.real + 2 * _DAY)
    real = built.real
    a, b = edition_id(1, real - 60 * _DAY), edition_id(2, real - 50 * _DAY)
    built.upload("p", a, data=["old.parquet"])
    built.upload("p", b, data=["new.parquet"])
    built.record("p", a, real - 60 * _DAY)
    built.record("p", b, real - 50 * _DAY)
    built.retire("p", a, real - 40 * _DAY)
    built.touch(real - 39 * _DAY)
    built.put("products/p/data/orphan.parquet")
    return built


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.toml"
    path.write_text(_CONFIG.format(backup="30d"))
    return path


def _gc(*args: str) -> Result:
    return runner.invoke(app, ["gc", *args])


def _lines(result: Result) -> list[dict[str, object]]:
    """Standard output, each line parsed: it holds JSON lines and nothing else."""
    return [json.loads(line) for line in result.stdout.splitlines()]


class TestARun:
    def test_standard_output_holds_the_events_and_the_deletion_log(self, cat: GcCatalog, config: Path) -> None:
        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        lines = _lines(result)
        first = lines[0]
        assert first["event"] == "jobs.config"
        assert first["catalog"] == "az://catalogs/stc/"
        assert first["detail"] == {"path": str(config), "sha256": hashlib.sha256(config.read_bytes()).hexdigest()}
        planned = [line for line in lines if line["event"] == "gc.planned"]
        deleted = [line for line in lines if line["event"] == "gc.deleted"]
        assert {line["path"] for line in planned} == {line["path"] for line in deleted}
        assert len(planned) == 4
        assert all(set(line) == _LINE for line in planned + deleted)
        orphan = next(line for line in deleted if line["path"] == "products/p/data/orphan.parquet")
        assert orphan == {
            "event": "gc.deleted",
            "path": "products/p/data/orphan.parquet",
            "reason": "unreferenced",
            "edition": None,
        }
        assert {line["reason"] for line in planned if str(line["path"]).endswith("manifest.json")} == {"retired"}
        assert "GC planned 4 deletion(s) and deleted 4; 0 failed" in result.stderr
        assert "products/p/data/new.parquet" in cat.paths()

    def test_a_failed_deletion_exits_1_and_logs_its_error(self, cat: GcCatalog, config: Path) -> None:
        cat.ops.fail.add("products/p/data/orphan.parquet")

        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        [failed] = [line for line in _lines(result) if line["event"] == "gc.delete_failed"]
        assert set(failed) == _LINE | {"error"}
        assert "refused by the test" in str(failed["error"])
        assert "FAIL products/p/data/orphan.parquet" in result.stderr

    def test_a_dry_run_is_a_forecast_for_the_next_run(self, cat: GcCatalog, config: Path, tmp_path: Path) -> None:
        dry = _gc("az://catalogs/stc", "--config", str(config), "--dry-run")
        assert dry.exit_code == 0, dry.output
        assert cat.ops.called("delete") == []
        assert "This dry run deleted nothing." in dry.stderr
        plan = tmp_path / "plan.jsonl"
        kept = [line for line in dry.stdout.splitlines() if "orphan.parquet" not in line]
        plan.write_text("\n".join(kept) + "\n")

        result = _gc("az://catalogs/stc", "--config", str(config), "--forecast", str(plan))

        assert result.exit_code == 0, result.output
        [deviation] = [line for line in _lines(result) if line["event"] == "gc.forecast_deviation"]
        assert (deviation["severity"], deviation["detail"]) == (
            "warning",
            {"path": "products/p/data/orphan.parquet", "in": "plan"},
        )
        assert "products/p/data/orphan.parquet" in cat.paths()
        assert "DEVIATION products/p/data/orphan.parquet" in result.stderr

    def test_failed_closed_products_and_unregistered_editions_leave_exit_0(self, cat: GcCatalog, config: Path) -> None:
        e = edition_id(9, cat.real)
        cat.upload("q", e, data=["q.parquet"])
        cat.put("products/r/" + edition_id(10, cat.real) + "/metadata.duckdb")
        cat.put(f"pending/q/{e}.json", b"{}")

        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 0, result.output
        events = {line["event"] for line in _lines(result)}
        assert {"gc.failed_closed", "gc.unregistered"} <= events
        assert "FAILED-CLOSED q:" in result.stderr
        assert "UNREGISTERED r/" in result.stderr


class TestExit1:
    def test_a_refused_configuration_exits_1_before_any_request(
        self, cat: GcCatalog, config: Path, tmp_path: Path
    ) -> None:
        config.write_text(_CONFIG.format(backup="40d"))

        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        assert "ERROR" in result.stderr
        assert "backup (40d) must not exceed gc_window (35d)" in result.stderr
        assert result.stdout == ""
        assert cat.ops.calls == []

    def test_a_stopped_run_exits_1_with_step_failed(self, cat: GcCatalog, config: Path) -> None:
        cat._plain.ops.delete(root_path(3))

        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 1
        failed = _lines(result)[-1]
        assert (failed["event"], failed["severity"]) == ("step.failed", "error")
        assert "deleted nothing" in str(failed["detail"])
        assert cat.ops.called("delete") == []


class TestExit2:
    def test_discard_of_a_recorded_edition_exits_2(self, cat: GcCatalog, config: Path) -> None:
        recorded = cat.leaves["p"].editions[0].edition_id

        result = _gc("az://catalogs/stc", "--config", str(config), "--discard", f"p/{recorded}")

        assert result.exit_code == 2
        assert "statcat admin cancel" in result.output
        assert cat.ops.called("delete") == []

    @pytest.mark.parametrize("args", [("--discard", "p"), ("--forecast", "/nonexistent/plan.jsonl")], ids=str)
    def test_a_bad_option_exits_2(self, cat: GcCatalog, config: Path, args: tuple[str, str]) -> None:
        result = _gc("az://catalogs/stc", "--config", str(config), *args)

        assert result.exit_code == 2
        assert cat.ops.called("delete") == []

    def test_a_forecast_that_is_not_json_lines_exits_2(self, cat: GcCatalog, config: Path, tmp_path: Path) -> None:
        plan = tmp_path / "plan.txt"
        plan.write_text("delete products/p/data/orphan.parquet\n")

        result = _gc("az://catalogs/stc", "--config", str(config), "--forecast", str(plan))

        assert result.exit_code == 2
        assert "line 1" in result.output

    def test_an_s3_url_exits_2(self, cat: GcCatalog, config: Path) -> None:
        result = _gc("s3://bucket/stc", "--config", str(config))
        assert result.exit_code == 2
        assert "W3" in result.output

    def test_a_store_other_than_azure_exits_2(
        self, cat: GcCatalog, config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: MemoryStore())

        result = _gc("az://catalogs/stc", "--config", str(config))

        assert result.exit_code == 2
        assert "AzureStore only" in result.output
        assert "W3" in result.output

    def test_a_refused_variable_exits_2(self, cat: GcCatalog, config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_KEY", "")
        result = _gc("az://catalogs/stc", "--config", str(config))
        assert result.exit_code == 2
        assert "AZURE_STORAGE_ACCOUNT_KEY" in result.output

    def test_the_help_renders(self) -> None:
        result = _gc("--help")
        assert result.exit_code == 0, result.output
        assert "--discard" in result.output

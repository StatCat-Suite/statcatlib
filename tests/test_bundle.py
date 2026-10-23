"""Unit tests for portable bundle support."""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import shutil
import stat
from pathlib import Path

import duckdb
import pytest

import statcatlib as sc
from statcatlib.bundle import (
    At,
    Auto,
    BundleDataPathError,
    Colocated,
    Embedded,
    inspect_bundle,
    read_current_data_path,
)

pytestmark = pytest.mark.unit


def _make_bundle(path: Path) -> Path:
    """Build a minimal statcat bundle and return its directory."""
    with sc.Catalog(path).open() as store:
        store.put(
            sc.OpenDataset(
                identifier="ds-1",
                title=sc.InternationalString(en="Test"),
                description=sc.InternationalString(en="Test dataset"),
            )
        )
    return path


def _make_bundle_with_data(path: Path) -> Path:
    """A bundle whose lake references real parquet files.

    The data-file count is what separates "moved bundle, nothing to lose"
    from "moved bundle, the stamp is the only pointer to real data", so a
    few tests need a lake that is not empty. Small writes get inlined into
    the catalog rather than written out, hence the row count.
    """
    bundle = _make_bundle(path)
    conn = duckdb.connect()
    conn.execute("INSTALL ducklake; LOAD ducklake;")
    conn.execute(f"ATTACH 'ducklake:{bundle / 'lake.ducklake'}' AS lake (DATA_PATH '{bundle / 'parquet'}/')")
    conn.execute("CREATE TABLE lake.obs AS SELECT range AS i FROM range(10000)")
    conn.close()
    return bundle


def _embedded_data_path(bundle: Path) -> str:
    return read_current_data_path(bundle / "lake.ducklake")


class TestNormalizeLakeDataPath:
    def test_idempotent_when_already_matching(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        # default DATA_PATH is <bundle>/parquet/
        target = f"{bundle / 'parquet'}/"
        mtime_before = (bundle / "lake.ducklake").stat().st_mtime_ns

        result = sc.normalize_lake_data_path(bundle / "lake.ducklake", target)

        assert result == bundle / "lake.ducklake"
        # No write — mtime preserved.
        assert (bundle / "lake.ducklake").stat().st_mtime_ns == mtime_before
        assert _embedded_data_path(bundle) == target

    def test_idempotent_accepts_target_without_trailing_slash(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        target_no_slash = str(bundle / "parquet")
        mtime_before = (bundle / "lake.ducklake").stat().st_mtime_ns

        sc.normalize_lake_data_path(bundle / "lake.ducklake", target_no_slash)

        assert (bundle / "lake.ducklake").stat().st_mtime_ns == mtime_before

    def test_rewrites_on_mismatch(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        new_target = f"{tmp_path / 'elsewhere' / 'parquet'}/"

        sc.normalize_lake_data_path(bundle / "lake.ducklake", new_target)

        assert _embedded_data_path(bundle) == new_target

    def test_rewrites_to_abfss(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        abfss = "abfss://container@acct.dfs.core.windows.net/prefix/parquet/"

        sc.normalize_lake_data_path(bundle / "lake.ducklake", abfss)

        assert _embedded_data_path(bundle) == abfss

    def test_second_call_after_rewrite_is_zero_write(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        new_target = f"{tmp_path / 'elsewhere' / 'parquet'}/"

        sc.normalize_lake_data_path(bundle / "lake.ducklake", new_target)
        mtime_after_first = (bundle / "lake.ducklake").stat().st_mtime_ns

        sc.normalize_lake_data_path(bundle / "lake.ducklake", new_target)

        assert (bundle / "lake.ducklake").stat().st_mtime_ns == mtime_after_first

    def test_scratch_fallback_on_readonly_lake(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        lake = bundle / "lake.ducklake"
        new_target = f"{tmp_path / 'elsewhere' / 'parquet'}/"

        # Make the lake file read-only and try to rewrite.
        original_mode = lake.stat().st_mode
        lake.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            scratch = tmp_path / "scratch"
            result = sc.normalize_lake_data_path(lake, new_target, scratch_dir=scratch)
        finally:
            lake.chmod(original_mode)

        assert result == scratch / "lake.ducklake"
        assert read_current_data_path(result) == new_target
        # Original untouched.
        assert _embedded_data_path(bundle) != new_target


def _open_bundle_ro_worker(bundle_str: str) -> str:
    """Subprocess body for the concurrent read-only open test."""
    with sc.open_bundle(bundle_str, data=Embedded(), access_mode="read_only") as store:
        ds = store.get(sc.BaseDataset, "ds-1")
        return ds.identifier if ds is not None else "missing"


class TestOpenBundle:
    def test_default_opens(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        with sc.open_bundle(bundle) as store:
            ds = store.get(sc.BaseDataset, "ds-1")
            assert ds is not None
            assert ds.identifier == "ds-1"

    def test_default_open_in_place_is_zero_write(self, tmp_path: Path) -> None:
        """The steady state takes no write lock — what lets N pods share a mount."""
        bundle = _make_bundle_with_data(tmp_path / "cat")
        mtime_before = (bundle / "lake.ducklake").stat().st_mtime_ns

        with sc.open_bundle(bundle):
            pass

        assert (bundle / "lake.ducklake").stat().st_mtime_ns == mtime_before

    def test_moved_bundle_is_repointed(self, tmp_path: Path) -> None:
        """A bundle moved on disk self-heals: co-location is the contract."""
        original = _make_bundle_with_data(tmp_path / "cat")
        moved = tmp_path / "moved"
        original.rename(moved)

        with sc.open_bundle(moved) as _:
            pass

        assert _embedded_data_path(moved) == f"{moved / 'parquet'}/"

    def test_embedded_with_readonly(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        with sc.open_bundle(bundle, data=Embedded(), access_mode="read_only") as store:
            ds = store.get(sc.BaseDataset, "ds-1")
            assert ds is not None

    def test_readonly_default_no_longer_raises(self, tmp_path: Path) -> None:
        """The old API refused ``normalize=True`` + read-only; now it just never writes."""
        bundle = _make_bundle(tmp_path / "cat")
        with sc.open_bundle(bundle, access_mode="read_only") as store:
            assert store.get(sc.BaseDataset, "ds-1") is not None

    def test_readonly_never_writes_even_when_moved(self, tmp_path: Path) -> None:
        original = _make_bundle_with_data(tmp_path / "cat")
        moved = tmp_path / "moved"
        original.rename(moved)
        stamp_before = _embedded_data_path(moved)

        with contextlib.suppress(Exception), sc.open_bundle(moved, access_mode="read_only"):
            pass

        assert _embedded_data_path(moved) == stamp_before

    def test_concurrent_readonly_opens(self, tmp_path: Path) -> None:
        """N parallel processes can attach the same bundle read-only."""
        bundle = _make_bundle(tmp_path / "cat")
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{bundle / 'parquet'}/")

        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=4) as pool:
            results = pool.map(_open_bundle_ro_worker, [str(bundle)] * 4)
        assert results == ["ds-1"] * 4


class TestNoStrayParquetDir:
    """Opening creates no ``parquet/``: an empty one would make a split bundle look co-located."""

    def test_open_does_not_create_parquet_dir(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        assert not (bundle / "parquet").exists()

    def test_ducklake_creates_it_on_first_write(self, tmp_path: Path) -> None:
        """Dropping the mkdir loses nothing — DuckLake makes its own."""
        bundle = _make_bundle_with_data(tmp_path / "cat")
        assert (bundle / "parquet").is_dir()


class TestCLI:
    def test_rebind_command(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        moved = tmp_path / "moved"
        bundle.rename(moved)

        result = CliRunner().invoke(app, ["bundle", "rebind", str(moved)])
        assert result.exit_code == 0, result.output
        assert _embedded_data_path(moved) == f"{moved / 'parquet'}/"

    def test_rebind_to_remote(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"

        result = CliRunner().invoke(app, ["bundle", "rebind", str(bundle), "--to", remote])
        assert result.exit_code == 0, result.output
        assert _embedded_data_path(bundle) == remote

    def test_rebind_refuses_remote_to_local(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        result = CliRunner().invoke(app, ["bundle", "rebind", str(bundle)])
        assert result.exit_code == 1
        assert _embedded_data_path(bundle) == remote

        forced = CliRunner().invoke(app, ["bundle", "rebind", str(bundle), "--force"])
        assert forced.exit_code == 0, forced.output
        assert _embedded_data_path(bundle) == f"{bundle / 'parquet'}/"

    def test_explain_reports_split_layout(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        result = CliRunner().invoke(app, ["bundle", "explain", str(bundle)])
        assert result.exit_code == 0, result.output
        assert "split" in result.output
        assert _embedded_data_path(bundle) == remote

    def test_explain_exits_1_when_ambiguous(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle_with_data(tmp_path / "cat")
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{tmp_path / 'gone'}/")
        (bundle / "parquet").rename(tmp_path / "stashed")

        result = CliRunner().invoke(app, ["bundle", "explain", str(bundle)])
        assert result.exit_code == 1
        assert "ambiguous" in result.output

    def test_attach_cmd_command(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        result = CliRunner().invoke(app, ["bundle", "attach-cmd", str(bundle)])
        assert result.exit_code == 0, result.output
        assert "ATTACH 'ducklake:" in result.output
        assert "DATA_PATH" in result.output

    def test_attach_cmd_read_only_flag(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        bundle = _make_bundle(tmp_path / "cat")
        result = CliRunner().invoke(app, ["bundle", "attach-cmd", str(bundle), "--read-only"])
        assert result.exit_code == 0
        assert "READ_ONLY" in result.output


class TestNotADuckLake:
    def test_raises_when_metadata_table_missing(self, tmp_path: Path) -> None:
        fake = tmp_path / "fake.ducklake"
        conn = duckdb.connect(str(fake))
        conn.close()
        # The exact DuckDB error type for "table missing" varies across
        # versions — we just want to confirm we don't silently succeed.
        with pytest.raises((duckdb.Error, RuntimeError)):
            sc.normalize_lake_data_path(fake, str(tmp_path / "parquet/"))


class TestReadCurrentDataPath:
    """`read_current_data_path`, the public probe for the stamp."""

    def test_accepts_the_bundle_directory(self, tmp_path: Path) -> None:
        """Callers should not have to know the lake is called lake.ducklake."""
        bundle = _make_bundle(tmp_path / "cat")
        assert read_current_data_path(bundle) == f"{bundle / 'parquet'}/"

    def test_accepts_the_lake_file(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        assert read_current_data_path(bundle / "lake.ducklake") == f"{bundle / 'parquet'}/"

    def test_returns_a_remote_uri_verbatim(self, tmp_path: Path) -> None:
        """A remote path must come back untouched — not resolved or localised."""
        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)
        assert read_current_data_path(bundle) == remote

    def test_missing_bundle_names_what_it_looked_for(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"lake\.ducklake"):
            read_current_data_path(tmp_path / "not-a-bundle")


class TestAutoPolicy:
    """`Auto`, the default, reads the stamp and dispatches on it."""

    def test_remote_stamp_is_honoured_not_overwritten(self, tmp_path: Path) -> None:
        """The inversion of ``test_the_default_would_have_overwritten_it``.

        Overwriting the stamp here would destroy the only record of where a
        split bundle's data lives. The default leaves it alone.
        """
        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        with contextlib.suppress(Exception), sc.open_bundle(bundle):
            pass

        assert read_current_data_path(bundle) == remote

    def test_resolvable_local_stamp_outside_the_bundle_is_honoured(self, tmp_path: Path) -> None:
        """Resolve-first covers what scheme-sniffing would miss.

        Parquet on a local mount that is not ``<bundle>/parquet/``.
        """
        bundle = _make_bundle_with_data(tmp_path / "cat")
        mount = tmp_path / "mnt"
        mount.mkdir()
        elsewhere = mount / "parquet"
        (bundle / "parquet").rename(elsewhere)
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{elsewhere}/")

        with sc.open_bundle(bundle):
            pass

        assert read_current_data_path(bundle) == f"{elsewhere}/"

    def test_ambiguous_layout_raises(self, tmp_path: Path) -> None:
        """Neither guess is safe, so it refuses instead of picking one."""
        bundle = _make_bundle_with_data(tmp_path / "cat")
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{tmp_path / 'gone'}/")
        (bundle / "parquet").rename(tmp_path / "stashed")

        with pytest.raises(BundleDataPathError, match="cannot determine"), sc.open_bundle(bundle):
            pass

    def test_empty_lake_is_repointed_rather_than_refused(self, tmp_path: Path) -> None:
        """Nothing to lose: a stamp addressing no data files is safe to rewrite."""
        bundle = _make_bundle(tmp_path / "cat")
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{tmp_path / 'gone'}/")

        with sc.open_bundle(bundle):
            pass

        assert read_current_data_path(bundle) == f"{bundle / 'parquet'}/"


class TestExplicitPolicies:
    def test_embedded_leaves_the_stamp_alone(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        with contextlib.suppress(Exception), sc.open_bundle(bundle, data=Embedded()):
            pass

        assert read_current_data_path(bundle) == remote

    def test_colocated_rewrites_a_remote_stamp_only_when_asked(self, tmp_path: Path) -> None:
        """``Colocated`` is the explicit form of the old destructive default."""
        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        with pytest.raises(BundleDataPathError, match="refusing"), sc.open_bundle(bundle, data=Colocated()):
            pass

    def test_at_does_not_persist_by_default(self, tmp_path: Path) -> None:
        """Pointing one open somewhere is not re-stamping the bundle."""
        bundle = _make_bundle(tmp_path / "cat")
        stamp_before = read_current_data_path(bundle)

        with contextlib.suppress(Exception), sc.open_bundle(bundle, data=At(tmp_path / "elsewhere")):
            pass

        assert read_current_data_path(bundle) == stamp_before

    def test_at_persist_true_rewrites(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        target = tmp_path / "elsewhere"

        with contextlib.suppress(Exception), sc.open_bundle(bundle, data=At(target, persist=True)):
            pass

        assert read_current_data_path(bundle) == f"{target}/"


class TestInspectBundle:
    """The discriminator as an inspectable value, not hidden branching."""

    def test_reports_a_colocated_bundle(self, tmp_path: Path) -> None:
        bundle = _make_bundle_with_data(tmp_path / "cat")
        info = inspect_bundle(bundle)

        assert info.is_remote is False
        assert info.stamp_resolves is True
        assert info.colocated_exists is True
        assert info.data_files > 0
        assert info.target is None
        assert info.will_write is False

    def test_reports_a_split_bundle(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        remote = "abfss://acct.dfs.core.windows.net/container/prefix/"
        sc.normalize_lake_data_path(bundle / "lake.ducklake", remote)

        info = inspect_bundle(bundle)

        assert info.is_remote is True
        assert info.stamp_resolves is None
        assert info.target is None
        assert info.will_write is False
        assert "split" in info.verdict

    def test_reports_the_ambiguous_case_without_raising(self, tmp_path: Path) -> None:
        """`open_bundle` raises here; inspection reports, so ops can look first."""
        bundle = _make_bundle_with_data(tmp_path / "cat")
        sc.normalize_lake_data_path(bundle / "lake.ducklake", f"{tmp_path / 'gone'}/")
        (bundle / "parquet").rename(tmp_path / "stashed")

        info = inspect_bundle(bundle)

        assert info.target is None
        assert info.will_write is False
        assert info.verdict.startswith("ambiguous")

    def test_accepts_the_bundle_directory_or_the_lake_file(self, tmp_path: Path) -> None:
        bundle = _make_bundle(tmp_path / "cat")
        assert inspect_bundle(bundle).stamp == inspect_bundle(bundle / "lake.ducklake").stamp

    def test_auto_is_the_default_policy(self) -> None:
        import inspect as _inspect

        default = _inspect.signature(sc.open_bundle).parameters["data"].default
        assert isinstance(default, Auto)


def _obs_sum(store: sc.Catalog) -> int:
    """Read observations for real — a stale stamp answers ``count(*)`` correctly."""
    row = store.connection.execute("SELECT sum(i) FROM lake.obs").fetchone()
    assert row is not None
    return int(row[0])


_OBS_SUM = sum(range(10000))


class TestExplicitPathOverridesStamp:
    """An explicit DATA_PATH need not restate the stamp."""

    def test_at_absolute_spelling_of_a_relative_stamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundle = _make_bundle_with_data(tmp_path / "cat")
        monkeypatch.chdir(tmp_path)
        sc.normalize_lake_data_path(bundle / "lake.ducklake", "cat/parquet/")

        with sc.open_bundle(bundle, data=At(bundle / "parquet"), access_mode="read_only") as store:
            assert _obs_sum(store) == _OBS_SUM

        assert read_current_data_path(bundle) == "cat/parquet/"

    def test_at_reads_its_own_path_not_the_stamp(self, tmp_path: Path) -> None:
        """The split-deploy case: the path is genuinely elsewhere, and is what gets read."""
        original = _make_bundle_with_data(tmp_path / "b1")
        copy = tmp_path / "b2"
        shutil.copytree(original, copy)
        # The copy's stamp still names b1; removing b1's data proves the read is b2's.
        shutil.rmtree(original / "parquet")

        with sc.open_bundle(copy, data=At(copy / "parquet"), access_mode="read_only") as store:
            assert _obs_sum(store) == _OBS_SUM

    def test_at_read_write_does_not_restamp(self, tmp_path: Path) -> None:
        original = _make_bundle_with_data(tmp_path / "b1")
        copy = tmp_path / "b2"
        shutil.copytree(original, copy)
        stamp_before = read_current_data_path(copy)

        with sc.open_bundle(copy, data=At(copy / "parquet")) as store:
            assert _obs_sum(store) == _OBS_SUM

        assert read_current_data_path(copy) == stamp_before

    @pytest.mark.parametrize("policy", [Auto(), Colocated()], ids=["auto", "colocated"])
    def test_moved_bundle_opens_read_only(self, tmp_path: Path, policy: Auto | Colocated) -> None:
        """Read-only drops the rewrite; the override still makes the open correct."""
        original = _make_bundle_with_data(tmp_path / "cat")
        moved = tmp_path / "moved"
        original.rename(moved)
        stamp_before = read_current_data_path(moved)

        with sc.open_bundle(moved, data=policy, access_mode="read_only") as store:
            assert _obs_sum(store) == _OBS_SUM

        assert read_current_data_path(moved) == stamp_before

    def test_default_catalog_still_checks_the_stamp(self, tmp_path: Path) -> None:
        """No explicit path, no override: a mismatch still fails at ATTACH."""
        original = _make_bundle_with_data(tmp_path / "b1")
        copy = tmp_path / "b2"
        shutil.copytree(original, copy)

        with pytest.raises(duckdb.Error, match="does not match"), sc.Catalog(copy).open():
            pass

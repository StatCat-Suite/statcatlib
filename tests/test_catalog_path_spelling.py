"""A folder catalog opens by any spelling of its path, and reads its parquet from any directory (GH-23).

Every check reads observations (``sum``), never ``count(*)``: DuckLake answers
a count from its catalog without opening a parquet file, so a count passes
even when the data cannot be read.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl
import pytest

import statcatlib as sc
from statcatlib.bundle import normalize_lake_data_path, read_current_data_path

pytestmark = pytest.mark.unit

#: Enough rows that DuckLake writes a parquet file rather than inlining them.
_ROWS = 100


def _make(path: str | Path) -> None:
    with sc.Catalog(path) as cat:
        cat.data.write_observations(
            schema="t", table="obs", source=pl.DataFrame({"k": [str(i) for i in range(_ROWS)], "v": [1.0] * _ROWS})
        )


def _total(path: str | Path, *, read_only: bool = False, data_path: sc.Embedded | None = None) -> float:
    with sc.Catalog(path, read_only=read_only, data_path=data_path) as cat:
        row = cat.connection.execute("SELECT sum(v) FROM lake.t.obs").fetchone()
    assert row is not None
    return float(row[0])


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A working directory to create catalogs in, and another to open them from."""
    work, elsewhere = tmp_path / "work", tmp_path / "elsewhere"
    work.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(work)
    return work, elsewhere


def test_a_relative_path_stamps_an_absolute_data_path(dirs: tuple[Path, Path]) -> None:
    work, _ = dirs
    _make("cat")
    assert (work / "cat" / "parquet").is_dir()
    assert read_current_data_path(work / "cat") == f"{work / 'cat' / 'parquet'}/"


@pytest.mark.parametrize("mode", ["read-only", "read-write"])
@pytest.mark.parametrize(("created", "opened"), [("relative", "absolute"), ("absolute", "relative")])
def test_any_spelling_opens(dirs: tuple[Path, Path], created: str, opened: str, mode: str) -> None:
    work, _ = dirs
    _make("cat" if created == "relative" else work / "cat")
    assert _total("cat" if opened == "relative" else work / "cat", read_only=mode == "read-only") == _ROWS


def test_a_symlinked_spelling_opens_and_leaves_the_stamp(dirs: tuple[Path, Path]) -> None:
    work, _ = dirs
    _make(work / "cat")
    (work / "link").symlink_to(work / "cat")
    stamp = read_current_data_path(work / "cat")
    assert _total(work / "link") == _ROWS
    assert read_current_data_path(work / "cat") == stamp


class TestCatalogsStampedRelative:
    """Catalogs ``Catalog("relative")`` created before GH-23, whose stamp DuckLake resolves against the cwd."""

    @pytest.fixture
    def legacy(self, dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
        work, elsewhere = dirs
        _make(work / "cat")
        normalize_lake_data_path(work / "cat" / "lake.ducklake", "cat/parquet/")
        monkeypatch.chdir(elsewhere)
        return work / "cat"

    def test_read_only_reads_from_another_directory_and_writes_nothing(self, legacy: Path) -> None:
        assert _total(legacy, read_only=True) == _ROWS
        assert read_current_data_path(legacy) == "cat/parquet/"

    def test_read_write_repairs_the_stamp(self, legacy: Path) -> None:
        assert _total(legacy) == _ROWS
        assert read_current_data_path(legacy) == f"{legacy / 'parquet'}/"

    def test_embedded_still_honours_the_stamp_as_it_is(self, legacy: Path) -> None:
        """`Embedded` means "trust the stamp", so it fails here; the default open is what repairs."""
        with pytest.raises(duckdb.IOException, match="Cannot open file"):
            _total(legacy, read_only=True, data_path=sc.Embedded())


class TestSplitBundlesStillFailAtAttach:
    """A stamp naming another directory is not reconciled: a default open must not read an empty local one."""

    def test_an_absolute_stamp_elsewhere(self, dirs: tuple[Path, Path]) -> None:
        work, elsewhere = dirs
        _make(work / "cat")
        normalize_lake_data_path(work / "cat" / "lake.ducklake", f"{elsewhere / 'parquet'}/")
        with pytest.raises(duckdb.Error, match="does not match existing data path"):
            _total(work / "cat", read_only=True)

    def test_a_relative_stamp_naming_another_bundle(self, dirs: tuple[Path, Path]) -> None:
        work, _ = dirs
        _make(work / "cat")
        normalize_lake_data_path(work / "cat" / "lake.ducklake", "other/parquet/")
        with pytest.raises(duckdb.Error, match="does not match existing data path"):
            _total(work / "cat", read_only=True)

"""Legacy layouts are refused, and the file set is unchanged (RFC D10, CUJ 4).

`Catalog.open` and `Catalog.create` refuse a directory holding a statcatlib
0.11 catalog (a root-level ``meta.duckdb``), a published sdmxlib 0.72 bundle
(``bundle.manifest.json``), or files and no ``catalog.json``. They write
nothing first, and the error names the re-ingest steps in
``docs/upgrading.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import statcatlib as sc
from statcatlib.store.store import IncompatibleCatalogError

pytestmark = pytest.mark.unit

_LAYOUTS: dict[str, dict[str, bytes]] = {
    "statcatlib 0.11 catalog": {"meta.duckdb": b"DUCK", "lake.ducklake": b"", "search/index.json": b"{}"},
    "sdmxlib 0.72 bundle": {"bundle.manifest.json": b"{}", "meta.duckdb": b"DUCK"},
    "bundle without meta.duckdb": {"bundle.manifest.json": b"{}"},
    "unrelated files": {"notes.txt": b"hello"},
}

_OPENS: dict[str, Callable[[Path], object]] = {
    "open": sc.Catalog.open,
    "create": sc.Catalog.create,
    "create(exist_ok=True)": lambda path: sc.Catalog.create(path, exist_ok=True),
}


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("layout", list(_LAYOUTS))
@pytest.mark.parametrize("opener", list(_OPENS))
def test_a_legacy_layout_is_refused_and_left_unchanged(tmp_path: Path, layout: str, opener: str) -> None:
    root = tmp_path / "old"
    for name, data in _LAYOUTS[layout].items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    before = _snapshot(root)
    names_before = sorted(p.name for p in root.rglob("*"))
    with pytest.raises(IncompatibleCatalogError, match=r"changed nothing here.*docs/upgrading\.md"):
        _OPENS[opener](root)
    assert _snapshot(root) == before
    assert sorted(p.name for p in root.rglob("*")) == names_before


def test_the_message_names_what_was_found(tmp_path: Path) -> None:
    (tmp_path / "meta.duckdb").write_bytes(b"DUCK")
    with pytest.raises(IncompatibleCatalogError, match=r"statcatlib 0\.11 catalog"):
        sc.Catalog.open(tmp_path)


def test_hidden_files_alone_do_not_make_a_legacy_layout(tmp_path: Path) -> None:
    """A ``.DS_Store`` in an empty directory is not the user's data."""
    (tmp_path / ".DS_Store").write_bytes(b"")
    with sc.Catalog.create(tmp_path) as cat:
        assert cat.writable
    assert (tmp_path / "catalog.json").is_file()

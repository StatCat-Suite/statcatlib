"""The open-time guard against a search index whose manifest version this build does not read (GH-40).

`_MANIFEST_VERSION` is bumped only when an existing field of ``index.json``
changes meaning, so an index at a higher version would be served wrongly.
`Catalog.open()` refuses it, and `check_bundle` reports it.

Another version is simulated by rewriting ``search/index.json``, which is the
only thing the guard reads.
"""

import json
from pathlib import Path

import duckdb
import pytest
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.bundle import check_bundle
from statcatlib.store import lance
from statcatlib.store.lance import LanceIndex
from statcatlib.store.store import IncompatibleCatalogError
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_NEWER = lance._MANIFEST_VERSION + 1


def _set_index_version(bundle: Path, version: object) -> None:
    path = bundle / "search" / "index.json"
    manifest = json.loads(path.read_text())
    if version is None:
        del manifest["version"]
    else:
        manifest["version"] = version
    path.write_text(json.dumps(manifest))


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    synthetic_catalog(path, Shape(dimensions=(Dim(codes=4),), observations=10, languages=("en",)))
    return path


@pytest.fixture
def newer_bundle(bundle: Path) -> Path:
    _set_index_version(bundle, _NEWER)
    return bundle


class TestReadable:
    def test_a_built_index_records_this_builds_version(self, bundle: Path) -> None:
        assert LanceIndex(bundle / "search").version == lance._MANIFEST_VERSION
        assert LanceIndex.unreadable_version(bundle / "search") is None

    def test_this_builds_version_opens_and_searches(self, bundle: Path) -> None:
        with sc.Catalog(bundle, read_only=True) as cat:
            assert cat.index_layout

    def test_version_1_is_still_read(self, bundle: Path) -> None:
        _set_index_version(bundle, 1)
        with sc.Catalog(bundle, read_only=True) as cat:
            assert cat.index_layout

    def test_no_index_has_nothing_to_check(self, tmp_path: Path) -> None:
        assert LanceIndex.unreadable_version(tmp_path / "search") is None


class TestGuard:
    def test_read_write_open_raises(self, newer_bundle: Path) -> None:
        expected = rf"index.json records version {_NEWER}, written by statcatlib .*build_index\(\)"
        with pytest.raises(IncompatibleCatalogError, match=expected):
            sc.Catalog(newer_bundle).open()

    def test_read_only_open_raises(self, newer_bundle: Path) -> None:
        with pytest.raises(IncompatibleCatalogError, match=f"version {_NEWER}"):
            sc.Catalog(newer_bundle, read_only=True).open()

    @pytest.mark.parametrize(
        ("version", "recorded"), [(None, "no version"), (True, "version True"), ("2", "version 2")]
    )
    def test_a_missing_or_malformed_version_is_refused(self, bundle: Path, version: object, recorded: str) -> None:
        _set_index_version(bundle, version)
        with pytest.raises(IncompatibleCatalogError, match=f"index.json records {recorded},"):
            sc.Catalog(bundle).open()

    def test_refused_before_any_ddl(self, newer_bundle: Path) -> None:
        """A statcat table dropped from the catalog is still absent after the refusal."""
        with duckdb.connect(str(newer_bundle / "meta.duckdb")) as conn:
            conn.execute("DROP TABLE statcat.author")
        with pytest.raises(IncompatibleCatalogError):
            sc.Catalog(newer_bundle).open()
        with duckdb.connect(str(newer_bundle / "meta.duckdb"), read_only=True) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'statcat'"
                ).fetchall()
            }
        assert "author" not in tables

    def test_refusal_releases_the_connection(self, newer_bundle: Path) -> None:
        with pytest.raises(IncompatibleCatalogError):
            sc.Catalog(newer_bundle).open()
        with duckdb.connect(str(newer_bundle / "meta.duckdb")) as conn:
            assert conn.execute("SELECT count(*) FROM statcat.dataset").fetchone() is not None


def _statcat_findings(bundle: Path) -> list[str]:
    """The ``statcat`` findings only: rewriting ``index.json`` also fails sdmxlib's ``files`` check."""
    return [str(f) for f in check_bundle(bundle, references=False).findings if str(f).startswith("[statcat]")]


def test_check_bundle_reports_a_newer_version_and_skips_the_search_rows(newer_bundle: Path) -> None:
    findings = _statcat_findings(newer_bundle)
    assert len(findings) == 1
    assert findings[0].startswith(f"[statcat] search/index.json: index.json records version {_NEWER}, written by")
    assert findings[0].endswith(
        "which this build does not read: check it with that release, or rebuild with build_index()"
    )


def test_check_bundle_reports_an_index_version_the_section_does_not_declare(bundle: Path) -> None:
    _set_index_version(bundle, 1)
    detail = f"the manifest declares index_version {lance._MANIFEST_VERSION}, index.json records 1"
    assert _statcat_findings(bundle) == [f"[statcat] bundle.manifest.json: {detail}"]

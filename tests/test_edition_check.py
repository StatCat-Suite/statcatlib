"""Changing a sealed edition fails ``check_edition(at="open")`` (RFC D9).

A seal records every file's size and digests in the manifest. The ``open``
profile checks sizes, which is the check a reader can afford at every open.
"""

from __future__ import annotations

from pathlib import Path

import msgspec
import pytest
import sdmxlib
from sdmxlib.edition import check_edition, read_manifest

import statcatlib as sc
from statcatlib.bundle import STATCAT_SECTION, check_catalog

pytestmark = pytest.mark.unit


@pytest.fixture
def edition_dir(catalog: sc.Catalog, tmp_path: Path) -> Path:
    wc = catalog.product("p").working_copy()
    wc.put(
        sc.OpenDataset(
            identifier="d1", title=sc.InternationalString(en="One"), description=sc.InternationalString(en="One")
        )
    )
    edition = wc.seal()
    return tmp_path / "catalog" / "products" / "p" / edition.edition_id


def test_a_sealed_edition_passes(edition_dir: Path) -> None:
    report = check_edition(edition_dir, at="open", extensions={STATCAT_SECTION: check_catalog})
    assert report.ok, [str(f) for f in report.findings]


def test_a_changed_file_fails(edition_dir: Path) -> None:
    meta = edition_dir / "metadata.duckdb"
    with meta.open("ab") as handle:
        handle.write(b"\0" * 16)
    report = check_edition(edition_dir, at="open")
    assert not report.ok
    assert any("metadata.duckdb" in str(f) for f in report.findings)


def test_an_added_file_fails_the_publish_profile(edition_dir: Path) -> None:
    """``open`` checks the listed files' sizes; ``publish`` also finds a file the manifest does not list."""
    (edition_dir / "extra.txt").write_text("not sealed")
    assert check_edition(edition_dir, at="open").ok
    report = check_edition(edition_dir, at="publish")
    assert not report.ok
    assert any("extra.txt" in str(f) for f in report.findings)


def test_an_edition_with_no_rows_passes_the_publish_profile(catalog: sc.Catalog, tmp_path: Path) -> None:
    """The seal records the annotation fingerprint for an index with no languages, so check_catalog passes."""
    edition = catalog.product("empty").working_copy().seal()
    edition_dir = tmp_path / "catalog" / "products" / "empty" / edition.edition_id
    report = check_edition(edition_dir, at="publish", extensions={STATCAT_SECTION: check_catalog})
    assert report.ok, [str(f) for f in report.findings]
    search = read_manifest(edition_dir).extensions[STATCAT_SECTION]["search"]
    assert isinstance(search, dict)
    assert search["languages"] == {}
    assert isinstance(search["annotations"], dict)
    assert search["annotations"]["fingerprint"]


def test_a_search_section_with_no_fingerprint_is_refused_with_no_languages(catalog: sc.Catalog, tmp_path: Path) -> None:
    """check_catalog requires the fingerprint whatever the index holds."""
    edition = catalog.product("empty").working_copy().seal()
    edition_dir = tmp_path / "catalog" / "products" / "empty" / edition.edition_id
    manifest = read_manifest(edition_dir)
    section = dict(manifest.extensions[STATCAT_SECTION])
    search = section["search"]
    assert isinstance(search, dict)
    section["search"] = {key: value for key, value in search.items() if key != "annotations"}
    stripped = msgspec.structs.replace(manifest, extensions={**manifest.extensions, STATCAT_SECTION: section})
    with sdmxlib.LocalRegistry.edition(edition_dir) as reg:
        findings = check_catalog(edition_dir, stripped, reg, "publish")
    assert [(f.subject, f.detail) for f in findings] == [
        ("search annotations", "the index records no annotation fingerprint: rebuild with build_index()")
    ]

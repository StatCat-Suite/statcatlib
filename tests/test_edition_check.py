"""Changing a sealed edition fails ``check_edition(at="open")`` (RFC D9).

A seal records every file's size and digests in the manifest. The ``open``
profile checks sizes, which is the check a reader can afford at every open.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sdmxlib.edition import check_edition

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
    """Its index has no languages and records no annotation fingerprint, and nothing in it can go stale."""
    edition = catalog.product("empty").working_copy().seal()
    edition_dir = tmp_path / "catalog" / "products" / "empty" / edition.edition_id
    report = check_edition(edition_dir, at="publish", extensions={STATCAT_SECTION: check_catalog})
    assert report.ok, [str(f) for f in report.findings]

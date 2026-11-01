"""The catalog format page states the local embargo boundary (RFC D5, D18).

On local disk statcatlib's API enforces embargo and file permissions do not,
so the boundary is a documented limit: this test keeps the statement on the
page, with the locations statcatlib does not support.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PAGE = Path(__file__).resolve().parents[1] / "docs" / "catalog-format.md"


@pytest.fixture(scope="module")
def page() -> str:
    return " ".join(_PAGE.read_text().split())


def test_the_page_has_an_embargo_section(page: str) -> None:
    assert "### 1.1 Embargo on local disk" in page


def test_the_api_enforces_embargo_and_file_permissions_do_not(page: str) -> None:
    assert "On local disk, statcatlib's API enforces embargo, and file permissions do not." in page


def test_readers_of_the_files_see_embargoed_editions(page: str) -> None:
    assert re.search(r"can read an embargoed edition's files, and so can its backups and copies", page)


def test_the_actor_is_unauthenticated(page: str) -> None:
    assert "taken from the process and unauthenticated" in page


def test_unsupported_locations_are_named(page: str) -> None:
    for location in ("Dropbox", "OneDrive", "iCloud Drive", "Google Drive", "network shares"):
        assert location in page, location

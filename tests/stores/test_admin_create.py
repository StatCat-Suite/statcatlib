"""``statcat admin create`` against Azurite: create, upload and release with no other setup (GH-159).

The command builds its store from the environment, so each test replaces
`obstore.store.from_url` with the Azurite store of `_stores.location`, which
names the loopback address alone, and `DefaultAzureCredential` with a class that
fails the test when asked for a token. The upload and the release job are the
tier's helpers, on the test's clock.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sdmxlib.inventory import CATALOG_NAME, FullInventory, decode_catalog, decode_root, root_path
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.cli import _REFUSED_KEYS, app
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, _ObjectOps
from tests.conftest import sealed_data_edition
from tests.stores import _stores

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from typer.testing import Result

pytestmark = pytest.mark.stores

runner = CliRunner()


class _Credential:
    """Stands for `DefaultAzureCredential`; Azurite's store signs with its key and never asks it."""

    def get_token(self, *_scopes: str, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("no token may be asked for")


@pytest.fixture
def create(monkeypatch: pytest.MonkeyPatch) -> Callable[[BlobLocation], Result]:
    """Runs ``statcat admin create`` on a location's URL, with the store the command builds replaced by Azurite's."""
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in _REFUSED_KEYS]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", _stores.ACCOUNT)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)

    def run(location: BlobLocation) -> Result:
        monkeypatch.setattr("obstore.store.from_url", lambda _url, **_kwargs: location.store)
        return runner.invoke(app, ["admin", "create", location.uri])

    return run


@pytest.fixture(scope="module")
def edition_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One data edition, sealed in a local catalog."""
    return sealed_data_edition(tmp_path_factory.mktemp("create") / "catalog")


def _catalog_bytes(location: BlobLocation) -> bytes:
    got = _ObjectOps(location.store).read(CATALOG_NAME)
    assert got is not None
    return got[0]


def _paths(location: BlobLocation) -> list[str]:
    return sorted(meta["path"] for meta in _ObjectOps(location.store).list(""))


class TestCreateThenUploadThenRelease:
    def test_an_upload_and_two_release_runs_need_no_other_setup(
        self,
        location: BlobLocation,
        create: Callable[[BlobLocation], Result],
        edition_dir: Path,
        azurite: _stores.Running,
        tmp_path: Path,
    ) -> None:
        result = create(location)

        assert result.exit_code == 0, result.output
        assert _paths(location) == [CATALOG_NAME]
        created = _catalog_bytes(location)
        document = decode_catalog(created)
        assert result.stdout == f"{document.catalog_id}\n"
        assert document.restore_epoch == 0

        now = datetime.now(UTC).replace(microsecond=0)
        release_at = now + timedelta(minutes=10)
        edition = _stores.upload(location, edition_dir, release_at=release_at)

        # The first run folds the registration into version 1, which records the embargo off.
        with _stores.release_job(location, clock=lambda: now) as job:
            fold = job.run()
        assert (fold.failed, fold.refused, fold.released) == ({}, (), ())
        assert fold.folded == (f"p/{edition.edition_id}",)
        storage = BlobInventoryStorage(location)
        first = storage.read(root_path(1))
        assert first is not None
        assert (decode_root(first).event.kind, decode_root(first).embargo) == ("seal", False)

        # A run after `release_at` releases the edition: the embargo holds nothing.
        with _stores.release_job(location, clock=lambda: release_at + timedelta(seconds=5)) as job:
            release = job.run()
        assert (release.failed, release.refused) == ({}, ())
        assert release.released == (f"p/{edition.edition_id}",)
        assert "release" not in release.skipped
        full = FullInventory(storage.read).refresh()
        assert _catalog_bytes(location) == created
        assert [entry.state for entry in full.entries("p")] == ["live"]
        with sc.Catalog.open(_stores.reader(location, azurite, tmp_path / "cache")) as cat:
            assert cat.products() == ["p"]


class TestRefusals:
    def test_a_second_run_exits_1_names_the_catalog_id_and_writes_nothing(
        self, location: BlobLocation, create: Callable[[BlobLocation], Result]
    ) -> None:
        assert create(location).exit_code == 0
        before = _catalog_bytes(location)

        result = create(location)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert decode_catalog(before).catalog_id in result.stderr
        assert _catalog_bytes(location) == before
        assert _paths(location) == [CATALOG_NAME]

    def test_a_prefix_with_objects_under_products_and_no_catalog_json_is_refused(
        self, location: BlobLocation, create: Callable[[BlobLocation], Result], edition_dir: Path
    ) -> None:
        # An upload leaves objects under `products/` and `pending/`, as a catalog that lost its `catalog.json` holds.
        _stores.upload(location, edition_dir)
        before = _paths(location)

        result = create(location)

        assert result.exit_code == 1
        assert "and no catalog.json" in result.stderr
        assert "products/p/" in result.stderr
        assert _paths(location) == before

    def test_of_several_creates_at_one_time_one_succeeds(self, location: BlobLocation) -> None:
        runs = 6
        start = threading.Barrier(runs)
        created: list[str] = []
        refused: list[str] = []

        def one() -> None:
            start.wait()
            try:
                created.append(CatalogAdmin.create(location).catalog_id)
            except FileExistsError as exc:
                refused.append(str(exc))

        threads = [threading.Thread(target=one) for _ in range(runs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(created) == 1
        assert len(refused) == runs - 1
        assert decode_catalog(_catalog_bytes(location)).catalog_id == created[0]
        assert all(created[0] in message for message in refused)

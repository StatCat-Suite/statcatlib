"""``statcat advance-current`` and ``statcat admin``: arguments, local and ``az://`` catalogs, exit codes (D11)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from obstore.store import MemoryStore
from sdmxlib.inventory import (
    CURRENT_NAME,
    Current,
    FullInventory,
    Leaf,
    encode,
    local_fetch,
    record,
    root_path,
    sha256_hex,
)
from sdmxlib.storage.schema import SCHEMA_VERSION
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.cli import app
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryWriter

pytestmark = pytest.mark.unit

runner = CliRunner()

_NOW = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
_EARLIER = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)

#: `statcat upload`'s refused variables, which ``az://`` admin commands refuse too.
_REFUSED = ("AZURE_STORAGE_ACCOUNT_KEY", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_USE_EMULATOR")


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=sc.InternationalString(en=identifier),
        description=sc.InternationalString(en=identifier),
    )


def _seal(root: Path, identifier: str) -> sc.Edition:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put([_dataset(identifier)])
        return wc.seal()


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _states(root: Path) -> dict[str, str]:
    return {e.edition_id: e.state for e in CatalogAdmin.open(root).editions("p")}


@pytest.fixture
def local(tmp_path: Path) -> tuple[Path, sc.Edition, sc.Edition]:
    """A catalog with a live edition A of product p, and B built on it and sealed under embargo."""
    root = tmp_path / "cat"
    a = _seal(root, "a")
    CatalogAdmin.open(root).set_embargo(on=True, reason="embargo the next release")
    b = _seal(root, "b")
    return root, a, b


class TestLocal:
    def test_editions_lists_every_state(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, a, b = local

        code, output = _invoke("admin", "editions", str(root), "p")

        assert code == 0, output
        assert f"{a.edition_id} live previous=-" in output
        assert f"{b.edition_id} embargoed previous={a.edition_id}" in output

    def test_editions_of_an_unrecorded_product_exits_1(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, _, _ = local
        code, output = _invoke("admin", "editions", str(root), "nothing")
        assert code == 1
        assert "ERROR the catalog records no product nothing" in output

    def test_release_writes_one_version(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, a, b = local
        before = FullInventory(local_fetch(root)).refresh().seq

        code, output = _invoke("admin", "release", str(root), "p", b.edition_id, "--reason", "jdoe: on time")

        assert code == 0, output
        assert f"Inventory version {before + 1} written: released p/{b.edition_id}." in output
        head = FullInventory(local_fetch(root)).refresh().root
        assert (head.seq, head.event.kind, head.event.reason) == (before + 1, "release", "jdoe: on time")
        assert _states(root) == {a.edition_id: "superseded", b.edition_id: "live"}

    def test_cancel_then_embargo_off_then_rollback(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, a, b = local

        assert _invoke("admin", "cancel", str(root), "p", b.edition_id, "--reason", "jdoe: wrong base")[0] == 0
        assert _states(root)[b.edition_id] == "withdrawn"
        code, output = _invoke("admin", "embargo", str(root), "off", "--reason", "jdoe: releases resume")
        assert code == 0, output
        assert "embargo off" in output
        assert not CatalogAdmin.open(root).embargo

        c = _seal(root, "c")
        code, output = _invoke("admin", "rollback", str(root), "p", "--if-live", c.edition_id, "--reason", "jdoe: bad")
        assert code == 0, output
        assert _states(root) == {a.edition_id: "live", b.edition_id: "withdrawn", c.edition_id: "withdrawn"}

    def test_a_refused_change_exits_1_with_no_traceback(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, a, _ = local

        code, output = _invoke("admin", "cancel", str(root), "p", a.edition_id, "--reason", "jdoe: no")

        assert code == 1
        assert output.startswith("ERROR ")
        assert "Traceback" not in output

    def test_an_embargo_already_set_exits_1(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, _, _ = local
        code, output = _invoke("admin", "embargo", str(root), "on", "--reason", "jdoe: again")
        assert code == 1
        assert "already on" in output

    def test_a_directory_that_is_no_catalog_exits_1(self, tmp_path: Path) -> None:
        code, output = _invoke("admin", "editions", str(tmp_path), "p")
        assert code == 1
        assert "is not a catalog" in output

    def test_advance_current_moves_a_lagging_current(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, _, _ = local
        head = FullInventory(local_fetch(root)).refresh().root
        first = (root / root_path(1)).read_bytes()
        (root / CURRENT_NAME).write_bytes(encode(Current(seq=1, root_sha256=sha256_hex(first), restore_epoch=0)))

        code, output = _invoke("advance-current", str(root))
        assert code == 0, output
        assert f"CURRENT moved to Inventory version {head.seq}." in output

        code, output = _invoke("advance-current", str(root))
        assert code == 0, output
        assert "already" in output


class TestUsage:
    @pytest.mark.parametrize(
        "args",
        [
            ("admin", "release", "{root}", "p", "e"),
            ("admin", "cancel", "{root}", "p", "e"),
            ("admin", "rollback", "{root}", "p"),
            ("admin", "embargo", "{root}", "on"),
        ],
        ids=["release", "cancel", "rollback", "embargo"],
    )
    def test_a_missing_reason_exits_2_and_writes_nothing(
        self, local: tuple[Path, sc.Edition, sc.Edition], args: tuple[str, ...]
    ) -> None:
        root, _, _ = local
        before = FullInventory(local_fetch(root)).refresh().seq

        code, output = _invoke(*(arg.format(root=root) for arg in args))

        assert code == 2
        assert "--reason" in output
        assert FullInventory(local_fetch(root)).refresh().seq == before

    def test_a_blank_reason_exits_2_and_writes_nothing(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, _, b = local
        before = FullInventory(local_fetch(root)).refresh().seq

        code, output = _invoke("admin", "release", str(root), "p", b.edition_id, "--reason", "  ")

        assert code == 2
        assert "--reason is blank" in output
        assert FullInventory(local_fetch(root)).refresh().seq == before

    def test_an_embargo_setting_other_than_on_or_off_exits_2(self, local: tuple[Path, sc.Edition, sc.Edition]) -> None:
        root, _, _ = local
        assert _invoke("admin", "embargo", str(root), "maybe", "--reason", "r")[0] == 2

    def test_the_help_renders(self) -> None:
        for args in (("admin", "--help"), ("advance-current", "--help"), ("admin", "rollback", "--help")):
            code, output = _invoke(*args)
            assert code == 0, output


# -- az:// -------------------------------------------------------------------


class _Credential:
    """Stands for `DefaultAzureCredential`; the store the test builds never asks it."""

    def get_token(self, *_scopes: str, **_kwargs: object) -> SimpleNamespace:
        pytest.fail("no token may be asked for")


def _leaf(product: str, n: int, *, embargo: bool) -> Leaf:
    return record(
        None,
        product_id=product,
        kind="data",
        lifecycle="revisable",
        edition_id=f"01a18e00-0000-7000-8000-{n:012d}",
        previous=None,
        manifest_sha256="0" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=embargo,
        now=_EARLIER,
        release_at=_NOW if embargo else None,
    )


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> tuple[MemoryStore, dict[str, object]]:
    """``az://`` builds a `MemoryStore` holding a catalog with an embargoed edition; the storage clock is `_NOW`."""
    refused = {name.lower() for name in _REFUSED}
    for name in [name for name in os.environ if name.startswith("AZURE_") and name.lower() in refused]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "acct")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _Credential)
    store = MemoryStore()
    built: dict[str, object] = {}

    def from_url(url: str, **kwargs: object) -> MemoryStore:
        built.update(kwargs, url=url)
        return store

    monkeypatch.setattr("obstore.store.from_url", from_url)
    monkeypatch.setattr(BlobInventoryStorage, "now", lambda _self: _NOW)
    monkeypatch.setattr(BlobInventoryStorage, "actor", lambda _self: "release-job-oid")
    writer = InventoryWriter(BlobInventoryStorage(BlobLocation("az://catalogs/stc/", store)))
    writer.write_catalog_json()
    writer.try_commit(None, {"p": _leaf("p", 1, embargo=True)}, now=_EARLIER, kind="seal", reason="fold")
    return store, built


class TestAzure:
    def test_editions_reads_the_catalog_the_url_names(self, remote: tuple[MemoryStore, dict[str, object]]) -> None:
        _, built = remote

        code, output = _invoke("admin", "editions", "az://catalogs/stc", "p")

        assert code == 0, output
        assert "01a18e00-0000-7000-8000-000000000001 embargoed" in output
        assert built["url"] == "az://catalogs/stc"
        assert built["account_name"] == "acct"
        assert built["client_options"] == {"timeout": "5m"}

    def test_a_change_records_the_identitys_actor_and_the_storage_clock(
        self, remote: tuple[MemoryStore, dict[str, object]]
    ) -> None:
        store, _ = remote

        code, output = _invoke(
            "admin", "cancel", "az://catalogs/stc", "p", "01a18e00-0000-7000-8000-000000000001", "--reason", "jdoe: x"
        )

        assert code == 0, output
        assert "Inventory version 2 written" in output
        full = FullInventory(BlobInventoryStorage(BlobLocation("az://catalogs/stc/", store)).read).refresh()
        assert (full.root.event.actor, full.root.event.reason) == ("release-job-oid", "jdoe: x")
        assert full.root.created_at == _NOW
        assert full.entries("p")[0].state == "withdrawn"

    def test_embargo_on_and_advance_current(self, remote: tuple[MemoryStore, dict[str, object]]) -> None:
        assert _invoke("admin", "embargo", "az://catalogs/stc", "on", "--reason", "jdoe: hold")[0] == 0
        code, output = _invoke("advance-current", "az://catalogs/stc")
        assert code == 0, output
        assert "already" in output

    def test_a_release_whose_manifest_is_absent_exits_1(self, remote: tuple[MemoryStore, dict[str, object]]) -> None:
        code, output = _invoke(
            "admin", "release", "az://catalogs/stc", "p", "01a18e00-0000-7000-8000-000000000001", "--reason", "jdoe"
        )
        assert code == 1
        assert output.startswith("ERROR ")
        assert "manifest" in output

    def test_a_storage_clock_refusal_exits_1(
        self, remote: tuple[MemoryStore, dict[str, object]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def ahead(_self: BlobInventoryStorage) -> datetime:
            msg = "az://catalogs/stc/: this machine's clock reads more than 30 s ahead of the storage clock"
            raise sc.InventoryWriteRefusedError(msg)

        monkeypatch.setattr(BlobInventoryStorage, "now", ahead)

        code, output = _invoke("admin", "embargo", "az://catalogs/stc", "on", "--reason", "jdoe")

        assert code == 1
        assert "ERROR az://catalogs/stc/: this machine's clock" in output

    @pytest.mark.parametrize("name", _REFUSED)
    def test_a_refused_variable_exits_2(
        self, remote: tuple[MemoryStore, dict[str, object]], monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, "")
        code, output = _invoke("admin", "editions", "az://catalogs/stc", "p")
        assert code == 2
        assert name in output

    def test_an_s3_url_exits_2(self, remote: tuple[MemoryStore, dict[str, object]]) -> None:
        code, output = _invoke("advance-current", "s3://bucket/stc")
        assert code == 2
        assert "W3" in output

    def test_no_account_exits_2(
        self, remote: tuple[MemoryStore, dict[str, object]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME")
        code, output = _invoke("admin", "editions", "az://catalogs/stc", "p")
        assert code == 2
        assert "AZURE_STORAGE_ACCOUNT_NAME" in output

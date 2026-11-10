"""`Catalog.open(BlobLocation)` refusals, `pull_edition` and hostile ids, over `MemoryStore` (Phase 4 Part 3, D6).

Nothing here starts DuckDB on a store path: `tests/stores/test_remote_catalog.py`
runs the read path against Azurite. Every test stops at a refusal, or inside
`pull_edition`, which reads only through `BlobLocation.ops`.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

import pytest
import sdmxlib as sl
from obstore.store import MemoryStore
from sdmxlib.edition import MANIFEST_NAME, decode_manifest, read_manifest
from sdmxlib.inventory import CATALOG_NAME, encode, record, sha256_hex
from sdmxlib.inventory.conformance import SCHEMA_VERSION

import statcatlib as sc
from statcatlib import catalog as catalog_module
from statcatlib.store import blob as blob_module
from statcatlib.store.blob import (
    BlobInventoryStorage,
    BlobLocation,
    _cached_edition,
    _is_safe_path,
    _ObjectOps,
    pull_edition,
)
from statcatlib.store.inventory import InventoryWriter
from statcatlib.store.store import IncompatibleCatalogError
from tests.conftest import sealed_data_edition, untyped

pytestmark = pytest.mark.unit

_URI = "az://catalogs/stc/"
_NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
_EDITION = "019a0000-0000-7000-8000-000000000001"


class ReadOps(_ObjectOps):
    """Records each read and each write."""

    def __init__(self, store: MemoryStore) -> None:
        super().__init__(store)
        self.reads: list[str] = []
        self.writes: list[str] = []

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self.reads.append(path)
        return super().read(path)

    @override
    def put(self, path: str, data: bytes, **kwargs) -> str | None:
        self.writes.append(path)
        return super().put(path, data, **kwargs)


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sealed_data_edition(tmp_path_factory.mktemp("open") / "catalog")


@pytest.fixture
def ops() -> ReadOps:
    return ReadOps(MemoryStore())


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def location(ops: ReadOps, cache: Path) -> BlobLocation:
    return BlobLocation(_URI, ops._store, cache_dir=cache, ops=ops)


@pytest.fixture
def dest(cache: Path, sealed: Path) -> Path:
    return cache / "cat" / "products" / "p" / sealed.name


def _writer(location: BlobLocation) -> InventoryWriter:
    return InventoryWriter(BlobInventoryStorage(location, actor="tests", clock=lambda: _NOW))


def _place(location: BlobLocation, edition_dir: Path, raw: bytes | None = None) -> bytes:
    """Put the edition's own files in the store, and its manifest, or *raw* in its place; return the manifest."""
    prefix = f"products/p/{edition_dir.name}"
    manifest = read_manifest(edition_dir)
    plain = _ObjectOps(location.store)
    for entry in manifest.files:
        if entry.root == "edition":
            plain.put(f"{prefix}/{entry.path}", (edition_dir / entry.path).read_bytes())
    stored = raw if raw is not None else (edition_dir / MANIFEST_NAME).read_bytes()
    plain.put(f"{prefix}/{MANIFEST_NAME}", stored)
    return stored


def _edited(edition_dir: Path, change: Callable[[dict[str, Any]], None]) -> bytes:
    raw = json.loads((edition_dir / MANIFEST_NAME).read_bytes())
    change(raw)
    return json.dumps(raw).encode()


def _pull(location: BlobLocation, sealed: Path, raw: bytes, dest: Path) -> Path:
    return pull_edition(location, "p", sealed.name, raw, dest)


def _staged(dest: Path) -> list[Path]:
    return list(dest.parent.glob(".staged-*")) if dest.parent.exists() else []


def _commit_edition(location: BlobLocation, edition_id: str, *, product: str = "p", sha: str = "0" * 64) -> None:
    writer = _writer(location)
    if writer.has_version() is False:
        writer.write_catalog_json()
    leaf = record(
        None,
        product_id=product,
        kind="data",
        lifecycle="revisable",
        edition_id=edition_id,
        previous=None,
        manifest_sha256=sha,
        schema_version=SCHEMA_VERSION,
        embargo=False,
        now=_NOW,
    )
    writer.try_commit(None, {product: leaf}, kind="seal", reason="test")


class TestOpenRefusals:
    @pytest.mark.parametrize("uri", ["az://catalogs/stc", "abfss://c@a.dfs.core.windows.net/x", "s3://bucket/x"])
    def test_a_string_uri_raises_naming_blob_location(self, uri: str) -> None:
        with pytest.raises(ValueError, match="BlobLocation"):
            sc.Catalog.open(uri)

    def test_a_string_uri_raises_with_azure_variables_set(self, monkeypatch) -> None:
        for name in ("AZURE_STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT_KEY", "AZURE_STORAGE_CONNECTION_STRING"):
            monkeypatch.setenv(name, "from-the-environment")

        with pytest.raises(ValueError, match="BlobLocation"):
            sc.Catalog.open("az://catalogs/stc")

    def test_the_read_modules_never_read_the_environment(self) -> None:
        for module in (catalog_module, blob_module):
            source = inspect.getsource(module)
            for name in ("os.environ", "getenv", "from_env", "from_url", "DefaultAzureCredential"):
                assert name not in source, f"{module.__name__} names {name}"

    def test_no_cache_dir_raises(self, ops: ReadOps) -> None:
        with pytest.raises(ValueError, match="cache_dir"):
            sc.Catalog.open(BlobLocation(_URI, ops._store, ops=ops))
        assert ops.reads == []

    def test_create_refuses_a_blob_location(self, location: BlobLocation) -> None:
        with pytest.raises(TypeError, match=r"Catalog\.open"):
            sc.Catalog.create(untyped(location))

    def test_an_admin_opens_a_blob_location_and_reads_its_catalog_json(self, location: BlobLocation) -> None:
        with pytest.raises(FileNotFoundError, match=CATALOG_NAME):
            sc.CatalogAdmin.open(location)
        _writer(location).write_catalog_json()
        assert repr(sc.CatalogAdmin.open(location)) == f"<CatalogAdmin {location.uri}>"

    def test_a_missing_catalog_json_raises(self, location: BlobLocation) -> None:
        with pytest.raises(FileNotFoundError, match=CATALOG_NAME):
            sc.Catalog.open(location)

    @pytest.mark.parametrize("catalog_id", ["../../escape", "/absolute", "..", "a/b", ".hidden", ""])
    def test_a_hostile_catalog_id_cannot_name_the_cache_root(self, location: BlobLocation, catalog_id: str) -> None:
        raw = json.loads(encode(_writer(location).write_catalog_json()))
        raw["catalog_id"] = catalog_id
        location.ops.delete(CATALOG_NAME)
        location.ops.put(CATALOG_NAME, json.dumps(raw).encode())

        with pytest.raises(IncompatibleCatalogError, match="catalog_id"):
            sc.Catalog.open(location)


class TestOpenReads:
    def test_an_open_writes_nothing_and_makes_no_cache_directory(self, location: BlobLocation, ops, cache) -> None:
        _writer(location).write_catalog_json()
        ops.writes.clear()

        with sc.Catalog.open(location) as cat:
            assert cat.products() == []
            assert cat.description is None

        assert ops.writes == []
        assert not cache.exists()

    def test_a_catalog_from_open_shows_its_uri_and_refuses_writes(self, location: BlobLocation) -> None:
        _writer(location).write_catalog_json()

        cat = sc.Catalog.open(location)
        assert _URI in repr(cat)
        assert _URI in repr(cat.product("p"))
        assert cat.writable is False
        with pytest.raises(RuntimeError, match="object storage"):
            cat.product("p").working_copy()
        cat.close()
        with pytest.raises(RuntimeError, match=_URI):
            cat.products()

    def test_a_product_with_no_live_edition_names_no_admin_on_blob(self, location: BlobLocation) -> None:
        _writer(location).write_catalog_json()

        with sc.Catalog.open(location) as cat:
            assert cat.product("p").editions() == []
            with pytest.raises(sc.NoLiveEditionError) as caught:
                cat.product("p").view()
        assert "CatalogAdmin" not in str(caught.value)

    def test_a_data_edition_with_no_secrets_is_refused_before_any_engine(
        self, location: BlobLocation, sealed: Path, monkeypatch
    ) -> None:
        """DuckDB gets no chance to look for a credential of its own."""
        raw = _place(location, sealed)
        _commit_edition(location, sealed.name, sha=sha256_hex(raw))

        def no_engine(*_: object, **__: object) -> None:
            pytest.fail("the view built an engine")

        monkeypatch.setattr(sl, "EditionEngine", no_engine)
        with sc.Catalog.open(location) as cat, pytest.raises(ValueError, match="secrets"):
            cat.product("p").view()


class TestPullEdition:
    def test_it_copies_the_local_tiers_and_the_manifest(self, location, sealed, dest) -> None:
        raw = _place(location, sealed)

        assert _pull(location, sealed, raw, dest) == dest

        manifest = read_manifest(dest)
        own = {e.path for e in manifest.files if e.root == "edition" and e.tier in {"required", "lazy"}}
        on_disk = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
        assert on_disk == {*own, MANIFEST_NAME}
        assert "metadata.duckdb" in own
        assert any(path.startswith("search/") for path in own)
        assert any(e.root == "data" for e in manifest.files)
        assert not (dest.parent / "data").exists()
        assert (dest / MANIFEST_NAME).read_bytes() == raw
        assert _staged(dest) == []

    def test_a_remote_tier_file_and_a_data_file_at_a_local_tier_stay_in_the_store(self, location, sealed, dest, ops):
        def retier(raw: dict[str, Any]) -> None:
            for entry in raw["files"]:
                if entry["root"] == "data":
                    entry["tier"] = "required"
                elif entry["path"] == "metadata.duckdb":
                    entry["tier"] = "remote"

        raw = _place(location, sealed, _edited(sealed, retier))

        _pull(location, sealed, raw, dest)

        assert not (dest / "metadata.duckdb").exists()
        assert not any(path.startswith("products/p/data/") or path.endswith("metadata.duckdb") for path in ops.reads)

    @pytest.mark.parametrize(
        "path",
        ["/etc/escape", "../escape", "a/../../escape", "a//b", "", "a/", "./a", "a\\b", "..\\escape", "a\0b", ".."],
    )
    def test_an_unsafe_path_is_refused_before_any_read(self, location, sealed, dest, ops, path: str) -> None:
        def poison(raw: dict[str, Any]) -> None:
            raw["files"][-1]["path"] = path

        raw = _place(location, sealed, _edited(sealed, poison))
        ops.reads.clear()

        with pytest.raises(IncompatibleCatalogError, match="unsafe path"):
            _pull(location, sealed, raw, dest)

        assert ops.reads == []
        assert not dest.exists()
        assert not dest.parent.exists()

    def test_a_listed_manifest_is_refused(self, location, sealed, dest) -> None:
        def poison(raw: dict[str, Any]) -> None:
            entry = next(e for e in raw["files"] if e["root"] == "edition")
            entry["path"] = MANIFEST_NAME

        raw = _place(location, sealed, _edited(sealed, poison))

        with pytest.raises(IncompatibleCatalogError, match="unsafe path"):
            _pull(location, sealed, raw, dest)

    @pytest.mark.parametrize("data_path", ["/abs/data/", "az://elsewhere/data/", "../../other/data/", "../../../x/"])
    def test_a_data_path_leaving_the_product_is_refused(self, location, sealed, dest, data_path: str) -> None:
        def poison(raw: dict[str, Any]) -> None:
            raw["data_path"] = data_path

        raw = _place(location, sealed, _edited(sealed, poison))

        with pytest.raises(IncompatibleCatalogError, match="data_path"):
            _pull(location, sealed, raw, dest)
        assert not dest.exists()

    def test_a_path_listed_under_another_file_is_refused(self, location, sealed, dest) -> None:
        def poison(raw: dict[str, Any]) -> None:
            first, second = [e for e in raw["files"] if e["root"] == "edition" and e["tier"] != "remote"][:2]
            second["path"] = f"{first['path']}/under"

        raw = _edited(sealed, poison)
        manifest = decode_manifest(raw, "test")
        plain = _ObjectOps(location.store)
        for entry in manifest.files:
            if entry.root == "edition":
                source = sealed / entry.path
                data = source.read_bytes() if source.exists() else None
                if data is None:
                    original = next(e for e in read_manifest(sealed).files if e.digests == entry.digests)
                    data = (sealed / original.path).read_bytes()
                plain.put(f"products/p/{sealed.name}/{entry.path}", data)

        with pytest.raises(IncompatibleCatalogError, match="over another listed path"):
            _pull(location, sealed, raw, dest)
        assert not dest.exists()
        assert _staged(dest) == []

    @pytest.mark.parametrize(
        ("product_id", "edition_id"),
        [
            ("../escape", _EDITION),
            ("/abs", _EDITION),
            ("p", "../escape"),
            ("p", "/abs"),
            ("p", "a/b"),
            ("p", ".staged"),
        ],
    )
    def test_a_hostile_id_is_refused_before_anything_is_decoded(self, location, ops, dest, product_id, edition_id):
        with pytest.raises(IncompatibleCatalogError, match="cannot name a directory"):
            pull_edition(location, product_id, edition_id, b"not a manifest", dest)
        assert ops.reads == []
        assert not dest.parent.exists()

    def test_a_manifest_naming_another_edition_or_product_is_refused(self, location, sealed, dest, ops) -> None:
        raw = _place(location, sealed)
        ops.reads.clear()

        with pytest.raises(IncompatibleCatalogError, match="names"):
            pull_edition(location, "other", sealed.name, raw, dest)
        with pytest.raises(IncompatibleCatalogError, match="names"):
            pull_edition(location, "p", _EDITION, raw, dest)

        assert ops.reads == []

    def test_an_unreadable_manifest_is_refused(self, location, sealed, dest) -> None:
        with pytest.raises(IncompatibleCatalogError, match="cannot read"):
            _pull(location, sealed, b"{}", dest)

    def test_a_digest_mismatch_leaves_nothing(self, location, sealed, dest) -> None:
        raw = _place(location, sealed)
        path = f"products/p/{sealed.name}/metadata.duckdb"
        size = (sealed / "metadata.duckdb").stat().st_size
        _ObjectOps(location.store).put(path, b"x" * size)

        with pytest.raises(IncompatibleCatalogError, match=r"metadata\.duckdb"):
            _pull(location, sealed, raw, dest)

        assert not dest.exists()
        assert _staged(dest) == []

    def test_a_size_mismatch_leaves_nothing(self, location, sealed, dest) -> None:
        raw = _place(location, sealed)
        _ObjectOps(location.store).put(f"products/p/{sealed.name}/metadata.duckdb", b"short")

        with pytest.raises(IncompatibleCatalogError, match=r"metadata\.duckdb"):
            _pull(location, sealed, raw, dest)
        assert not dest.exists()

    def test_a_missing_file_leaves_nothing(self, location, sealed, dest) -> None:
        raw = _place(location, sealed)
        location.ops.delete(f"products/p/{sealed.name}/metadata.duckdb")

        with pytest.raises(IncompatibleCatalogError, match="is missing"):
            _pull(location, sealed, raw, dest)
        assert not dest.exists()
        assert _staged(dest) == []

    def test_a_stale_staged_directory_is_swept_and_a_fresh_one_stays(self, location, sealed, dest) -> None:
        raw = _place(location, sealed)
        dest.parent.mkdir(parents=True)
        stale = dest.parent / ".staged-crashed"
        (stale / "nested").mkdir(parents=True)
        (stale / "nested" / "file").write_bytes(b"left")
        old = time.time() - 2 * 86_400
        os.utime(stale, (old, old))
        fresh = dest.parent / ".staged-running"
        fresh.mkdir()

        _pull(location, sealed, raw, dest)

        assert not stale.exists()
        assert fresh.exists()

    def test_every_file_and_directory_is_fsynced_before_the_rename(self, location, sealed, dest, monkeypatch) -> None:
        raw = _place(location, sealed)
        synced: set[int] = set()
        real_fsync = os.fsync
        renamed: list[Path] = []
        real_publish = blob_module._publish

        def fsync(fd: int) -> None:
            synced.add(os.fstat(fd).st_ino)
            real_fsync(fd)

        def publish(staged: Path, target: Path, manifest_bytes: bytes) -> None:
            everything = [staged, *staged.rglob("*")]
            assert (staged / MANIFEST_NAME).read_bytes() == manifest_bytes
            assert any(p.is_dir() for p in everything[1:]), "the edition has a search directory"
            assert {p.stat().st_ino for p in everything} <= synced
            assert not target.exists()
            renamed.append(staged)
            real_publish(staged, target, manifest_bytes)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(blob_module, "_publish", publish)

        _pull(location, sealed, raw, dest)

        assert len(renamed) == 1
        assert dest.parent.stat().st_ino in synced

    def test_the_manifest_is_written_after_every_listed_file(self, location, sealed, dest, monkeypatch) -> None:
        raw = _place(location, sealed)
        written: list[str] = []
        real = blob_module._write_synced

        def spy(path: Path, data: bytes) -> None:
            written.append(path.name)
            real(path, data)

        monkeypatch.setattr(blob_module, "_write_synced", spy)

        _pull(location, sealed, raw, dest)

        assert written[-1] == MANIFEST_NAME
        assert written.count(MANIFEST_NAME) == 1
        assert len(written) > 1

    def test_a_loser_of_the_rename_uses_the_winners_copy(self, location, sealed, dest, monkeypatch) -> None:
        raw = _place(location, sealed)
        real = blob_module._publish

        def another_process_wins(staged: Path, target: Path, manifest_bytes: bytes) -> None:
            target.mkdir(parents=True)
            (target / MANIFEST_NAME).write_bytes(manifest_bytes)
            real(staged, target, manifest_bytes)

        monkeypatch.setattr(blob_module, "_publish", another_process_wins)

        assert _pull(location, sealed, raw, dest) == dest

        assert not (dest / "metadata.duckdb").exists(), "the winner's copy stays as it was"
        assert _staged(dest) == []

    def test_a_winner_with_another_manifest_is_refused(self, location, sealed, dest, monkeypatch) -> None:
        raw = _place(location, sealed)
        real = blob_module._publish

        def another_process_wins(staged: Path, target: Path, manifest_bytes: bytes) -> None:
            target.mkdir(parents=True)
            (target / MANIFEST_NAME).write_bytes(b"other")
            real(staged, target, manifest_bytes)

        monkeypatch.setattr(blob_module, "_publish", another_process_wins)

        with pytest.raises(IncompatibleCatalogError, match="delete the directory"):
            _pull(location, sealed, raw, dest)
        assert _staged(dest) == []


class TestCachedEdition:
    def _root(self, cache: Path) -> Path:
        return cache / "cat"

    def test_absent_it_pulls(self, location, sealed, cache, dest, ops) -> None:
        raw = _place(location, sealed)
        ops.reads.clear()

        found, manifest = _cached_edition(location, self._root(cache), "p", sealed.name, sha256_hex(raw))

        assert found == dest
        assert manifest.edition_id == sealed.name
        assert ops.reads[0] == f"products/p/{sealed.name}/{MANIFEST_NAME}"
        assert len(ops.reads) > 1

    def test_held_it_reads_nothing(self, location, sealed, cache, dest, ops) -> None:
        raw = _place(location, sealed)
        _pull(location, sealed, raw, dest)
        ops.reads.clear()

        found, _ = _cached_edition(location, self._root(cache), "p", sealed.name, sha256_hex(raw))

        assert found == dest
        assert ops.reads == []

    def test_a_cached_sha_mismatch_names_the_directory_to_delete(self, location, sealed, cache, dest, ops) -> None:
        raw = _place(location, sealed)
        _pull(location, sealed, raw, dest)
        ops.reads.clear()

        with pytest.raises(IncompatibleCatalogError, match="delete the directory") as caught:
            _cached_edition(location, self._root(cache), "p", sealed.name, "0" * 64)

        assert str(dest) in str(caught.value)
        assert ops.reads == []

    def test_a_cached_directory_with_no_manifest_names_the_directory_to_delete(self, location, sealed, cache, dest):
        dest.mkdir(parents=True)

        with pytest.raises(IncompatibleCatalogError, match="delete the directory") as caught:
            _cached_edition(location, self._root(cache), "p", sealed.name, "0" * 64)

        assert str(dest) in str(caught.value)

    def test_a_wrong_remote_manifest_stops_at_the_manifest(self, location, sealed, cache, dest, ops) -> None:
        _place(location, sealed)
        ops.reads.clear()

        with pytest.raises(IncompatibleCatalogError, match="nothing else of the edition was read"):
            _cached_edition(location, self._root(cache), "p", sealed.name, "0" * 64)

        assert ops.reads == [f"products/p/{sealed.name}/{MANIFEST_NAME}"]
        assert not dest.parent.exists()

    def test_a_missing_remote_manifest_is_refused(self, location, sealed, cache) -> None:
        with pytest.raises(IncompatibleCatalogError, match="is missing"):
            _cached_edition(location, self._root(cache), "p", sealed.name, "0" * 64)


_HOSTILE = ["../../../escape", "/absolute", "..", ".", "a/b", "a\\b", ".staged-x", "x\ny"]


class TestHostileInventory:
    @pytest.mark.parametrize("edition_id", _HOSTILE)
    def test_a_hostile_edition_id_in_a_leaf_stays_out_of_the_filesystem(
        self, location, ops, cache, tmp_path, edition_id: str
    ) -> None:
        _commit_edition(location, edition_id)
        ops.reads.clear()
        before = set(tmp_path.rglob("*"))

        with sc.Catalog.open(location) as cat:
            product = cat.product("p")
            with pytest.raises(IncompatibleCatalogError, match="cannot name a directory"):
                product.view()
            with pytest.raises(IncompatibleCatalogError, match="cannot name a directory"):
                product.editions()

        assert not [path for path in ops.reads if path.startswith("products/")]
        assert set(tmp_path.rglob("*")) == before
        assert not (tmp_path.parent / "escape").exists()

    @pytest.mark.parametrize("product_id", ["../../escape", "/absolute", "a/b", ".staged-x"])
    def test_a_hostile_product_id_in_a_root_is_listed_and_cannot_be_opened(
        self, location, ops, tmp_path, product_id: str
    ) -> None:
        _commit_edition(location, _EDITION, product=product_id)
        ops.reads.clear()
        before = set(tmp_path.rglob("*"))

        with sc.Catalog.open(location) as cat:
            assert cat.products() == [product_id]
            with pytest.raises(ValueError, match="is not a product id"):
                cat.product(product_id)

        assert not [path for path in ops.reads if path.startswith("products/")]
        assert set(tmp_path.rglob("*")) == before

    def test_a_manifest_recorded_for_another_edition_is_refused_after_one_read(self, location, sealed, ops) -> None:
        raw = _place(location, sealed)
        _ObjectOps(location.store).put(f"products/p/{_EDITION}/{MANIFEST_NAME}", raw)
        _commit_edition(location, _EDITION, sha=sha256_hex(raw))
        ops.reads.clear()

        with sc.Catalog.open(location) as cat, pytest.raises(IncompatibleCatalogError, match="names"):
            cat.product("p").view()

        assert [path for path in ops.reads if path.startswith("products/")] == [
            f"products/p/{_EDITION}/{MANIFEST_NAME}"
        ]


class TestSafePaths:
    @pytest.mark.parametrize("path", ["metadata.duckdb", "search/index.json", "a/b/c.lance", "year=2020/x.parquet"])
    def test_a_plain_path_passes(self, path: str) -> None:
        assert _is_safe_path(path)

"""`collect_garbage` against Azurite (Phase 5 Part 4): the unreferenced files, the grace period, registrations.

Every edition here is sealed by statcatlib and uploaded by `_upload` over the
account-key digest read, so the manifests, data files and registrations are the
real ones. Inventory versions use this machine's clock; GC's storage clock is
set ahead of it, and for the grace period it is measured from Azurite's own
``Last-Modified``, the clock that stamps the files.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import attrs
import pytest
from sdmxlib.edition import MANIFEST_NAME, decode_manifest, edition_files, read_manifest, verify_remote
from sdmxlib.inventory import (
    decode_leaf,
    decode_root,
    retire,
    rollback,
    sha256_hex,
)
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
from statcatlib.gc import GcReport, collect_garbage
from statcatlib.jobs import RetentionPolicy, decode_registration
from statcatlib.model import InternationalString
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.upload import _lookup, _upload
from tests.conftest import sealed_data_edition
from tests.stores import _stores

if TYPE_CHECKING:
    from pathlib import Path

    from sdmxlib.inventory import Root

    from statcatlib.store.inventory import InventoryWriter

pytestmark = pytest.mark.stores

_DAY = timedelta(days=1)

_ALL_TIERS = ("required", "lazy", "remote")


def _policy(*, gc_window: timedelta = timedelta(days=35)) -> RetentionPolicy:
    return RetentionPolicy(
        backup=min(gc_window, timedelta(days=30)),
        gc_window=gc_window,
        tombstone_window=timedelta(days=400),
        inventory_window=gc_window + timedelta(days=1),
        retire_after=timedelta(days=90),
        pod_staleness=timedelta(minutes=5),
        keep_roots=2,
    )


def _gc(location: BlobLocation, now: datetime, policy: RetentionPolicy | None = None) -> GcReport:
    storage = BlobInventoryStorage(location, actor="gc", clock=lambda: now)
    return collect_garbage(location, policy or _policy(), storage=storage)


@attrs.frozen
class _Sealed:
    """Product ``p``: A, and B built on it; product ``q``: one edition; product ``u``: one edition."""

    a: Path
    b: Path
    q: Path
    u: Path


@pytest.fixture(scope="module")
def sealed(tmp_path_factory: pytest.TempPathFactory) -> _Sealed:
    root = tmp_path_factory.mktemp("gc") / "catalog"
    a = sealed_data_edition(root, product_id="p")
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put(sc.OpenDataset(identifier="d2", title=InternationalString(en="Two"), description=InternationalString()))
        b = a.parent / wc.seal().edition_id
    q = sealed_data_edition(root, product_id="q")
    u = sealed_data_edition(root, product_id="u")
    return _Sealed(a, b, q, u)


def _upload_only(location: BlobLocation, edition_dir: Path) -> None:
    with _stores.KeyedRest(location) as rest:
        _upload(edition_dir, location, rest=rest)


def _commit_retire(writer: InventoryWriter, product_id: str, edition_id: str) -> Root:
    full = writer.full()
    assert full is not None
    leaf, stones = retire(full.leaf(product_id), [edition_id], now=writer.storage.now())
    return writer.try_commit(
        full.root, {product_id: leaf}, retired=(*full.root.retired, *stones), kind="retire", reason="t"
    )


def _touch(writer: InventoryWriter) -> Root:
    full = writer.full()
    assert full is not None
    return writer.try_commit(full.root, {}, embargo=not full.embargo, kind="embargo-change", reason="t")


def _objects(location: BlobLocation) -> dict[str, datetime]:
    plain = BlobLocation(location.uri, location.store)
    return {meta["path"]: meta["last_modified"] for p in ("products", "pending") for meta in plain.ops.list(p)}


def _read(location: BlobLocation, path: str) -> bytes:
    got = location.ops.read(path)
    assert got is not None, path
    return got[0]


type _Key = tuple[str, str]


def _read_back(
    plain: BlobLocation, cutoff: datetime, keep_roots: int
) -> tuple[set[_Key], dict[_Key, datetime], set[_Key]]:
    """From a listing of every root: the kept editions, every tombstone's time, and what the head records."""
    listed = [m["path"] for m in plain.ops.list("inventory") if m["path"].count("/") == 1]
    roots = sorted((decode_root(_read(plain, path)) for path in listed), key=lambda root: -root.seq)
    read: list[Root] = []
    for root in roots:
        read.append(root)
        if len(read) >= keep_roots and any(r.created_at < cutoff for r in read):
            break
    leaves = {ref.leaf: decode_leaf(_read(plain, ref.leaf)) for root in read for ref in root.products.values()}
    entries = {
        (p, e.edition_id) for root in read for p, ref in root.products.items() for e in leaves[ref.leaf].editions
    }
    stones: dict[_Key, datetime] = {}
    for root in read:
        for stone in root.retired:
            stones.setdefault((stone.product_id, stone.edition_id), stone.retired_at)
    kept = entries | {key for key, at in stones.items() if at >= cutoff}
    head = read[0]
    recorded = {(p, e.edition_id) for p, ref in head.products.items() for e in leaves[ref.leaf].editions}
    recorded |= {(t.product_id, t.edition_id) for t in head.retired}
    return kept, stones, recorded


def _expected(location: BlobLocation, now: datetime, policy: RetentionPolicy) -> set[str]:
    """What GC must delete, computed apart from `statcatlib.gc`: from a listing of every root and the spec's rules."""
    cutoff = now - policy.gc_window
    plain = BlobLocation(location.uri, location.store)
    kept, stones, recorded = _read_back(plain, cutoff, policy.keep_roots)
    objects = _objects(location)
    registered: set[_Key] = set()
    expected: set[str] = set()
    for path, modified in objects.items():
        if path.startswith("pending/"):
            reg = decode_registration(path, _read(plain, path))
            key = (reg.product_id, reg.edition_id)
            if key in recorded and modified < cutoff:
                expected.add(path)
            elif key not in recorded:
                registered.add(key)
    listed: set[str] = set()
    for product_id, edition_id in kept | registered:
        manifest = decode_manifest(_read(plain, f"products/{product_id}/{edition_id}/{MANIFEST_NAME}"), "test")
        listed |= {f"products/{product_id}/{f}" for f in edition_files(manifest)}
    for path, modified in objects.items():
        parts = path.split("/")
        if parts[0] != "products":
            continue
        key = (parts[1], parts[2])
        unreferenced = parts[2] == "data" and path not in listed and modified < now - policy.upload_grace
        retired = key in stones and key not in kept and stones[key] < cutoff
        if unreferenced or retired:
            expected.add(path)
    return expected


class TestReachability:
    def test_gc_deletes_exactly_the_unreferenced_files_and_a_second_run_nothing(
        self, writer: InventoryWriter, location: BlobLocation, sealed: _Sealed
    ) -> None:
        a = _stores.record_uploaded(writer, location, sealed.a)
        b = _stores.record_uploaded(writer, location, sealed.b)
        _commit_retire(writer, "p", a.edition_id)
        _touch(writer)
        _upload_only(location, sealed.q)  # registered, not folded
        _upload_only(location, sealed.u)
        u = read_manifest(sealed.u)
        location.ops.delete(f"pending/u/{u.edition_id}.json")  # uploaded, never registered
        location.ops.put("products/p/data/orphan.parquet", b"orphan")
        now = datetime.now(UTC) + 40 * _DAY
        expected = _expected(location, now, _policy())
        before = set(_objects(location))

        report = _gc(location, now)

        assert set(report.deleted) == expected
        assert set(_objects(location)) == before - expected
        assert {f"products/p/{a.edition_id}/{MANIFEST_NAME}", "products/p/data/orphan.parquet"} <= expected
        assert f"pending/p/{b.edition_id}.json" in expected  # folded, past the window
        assert report.unregistered == (f"u/{u.edition_id}",)
        assert report.failed == {}
        assert report.failed_closed == {}
        for edition_dir in (sealed.b, sealed.q):
            manifest = read_manifest(edition_dir)
            with _stores.KeyedRest(location) as rest:
                assert verify_remote(manifest, _lookup(rest, manifest), tiers=_ALL_TIERS) == []

        again = _gc(location, now)

        assert again.planned == ()


def _azurite_now(location: BlobLocation, name: str) -> datetime:
    """The ``Last-Modified`` Azurite stamps on a new object: its own clock, the one GC's grace period reads."""
    location.ops.put(name, b"probe")
    meta = location.ops.head(name)
    assert meta is not None
    return meta["last_modified"]


class TestGracePeriodAndRollback:
    def test_a_racing_uploads_files_and_a_rolled_back_to_editions_files_stay(
        self, writer: InventoryWriter, location: BlobLocation, sealed: _Sealed
    ) -> None:
        a = _stores.record_uploaded(writer, location, sealed.a)
        b = _stores.record_uploaded(writer, location, sealed.b)
        full = writer.full()
        assert full is not None
        leaf = rollback(full.leaf("p"), schema_version=SCHEMA_VERSION, if_live=b.edition_id, now=writer.storage.now())
        writer.try_commit(full.root, {"p": leaf}, kind="rollback", reason="t")
        _commit_retire(writer, "p", b.edition_id)
        _touch(writer)
        location.ops.put("products/p/data/stale.parquet", b"stale")
        time.sleep(1.1)  # Azurite stamps Last-Modified to the second.
        reference = _azurite_now(location, "products/p/data/probe.parquet")
        time.sleep(1.1)
        location.ops.put("products/p/data/racing.parquet", b"racing")  # an upload still sending its data files
        policy = _policy(gc_window=timedelta(hours=1))

        report = _gc(location, reference + policy.upload_grace, policy)

        assert "products/p/data/stale.parquet" in report.deleted
        assert "products/p/data/racing.parquet" not in report.deleted
        assert location.ops.head("products/p/data/racing.parquet") is not None
        assert f"products/p/{b.edition_id}/{MANIFEST_NAME}" in report.deleted
        manifest = read_manifest(sealed.a)
        with _stores.KeyedRest(location) as rest:
            assert verify_remote(manifest, _lookup(rest, manifest), tiers=_ALL_TIERS) == []
        assert sha256_hex(_read(location, f"products/p/{a.edition_id}/{MANIFEST_NAME}")) == a.manifest_sha256


class TestRegisteredBeforeTheFold:
    def test_a_registered_editions_files_stay_and_the_fold_then_verifies(
        self, writer: InventoryWriter, location: BlobLocation, sealed: _Sealed
    ) -> None:
        _touch_first(writer)
        _upload_only(location, sealed.q)
        now = datetime.now(UTC) + 2 * _DAY

        report = _gc(location, now)

        assert report.planned == ()
        with _stores.KeyedRest(location) as rest:
            again = _upload(sealed.q, location, rest=rest)  # checks every file against the store
        assert again.sent == ()
        assert not again.registered
        recorded = _stores.record_uploaded(writer, location, sealed.q)
        head = writer.full()
        assert head is not None
        assert [e.edition_id for e in head.entries("q")] == [recorded.edition_id]

    def test_with_no_registration_the_same_files_are_deleted(
        self, writer: InventoryWriter, location: BlobLocation, sealed: _Sealed
    ) -> None:
        _touch_first(writer)
        _upload_only(location, sealed.q)
        manifest = read_manifest(sealed.q)
        location.ops.delete(f"pending/q/{manifest.edition_id}.json")
        data = {f"products/q/{f}" for f in edition_files(manifest) if f.startswith("data/")}

        report = _gc(location, datetime.now(UTC) + 2 * _DAY)

        assert data
        assert set(report.deleted) == data
        assert report.unregistered == (f"q/{manifest.edition_id}",)
        assert location.ops.head(f"products/q/{manifest.edition_id}/{MANIFEST_NAME}") is not None


def _touch_first(writer: InventoryWriter) -> None:
    """Write the catalog's first version, which records nothing: GC reads a head."""
    writer.try_commit(None, {}, kind="embargo-change", embargo=False, reason="t")

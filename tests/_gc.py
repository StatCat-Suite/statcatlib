"""A blob catalog over a `MemoryStore` for the GC tests: editions, registrations and versions at chosen times.

`MemoryStore` stamps each object with this machine's time, so a test moves GC's
storage clock instead: `GcCatalog.real` is when the catalog's objects were
written, and a run at ``real + 40d`` sees every object as 40 days old. Each
version's ``created_at`` and each tombstone's ``retired_at`` is the time the test
gives, so roots and tombstones sit anywhere against the window.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override

import msgspec
from obstore.store import MemoryStore
from sdmxlib.edition import CANONICAL_FORM, DATA_PATH, MANIFEST_NAME, EditionFile, Manifest, Producer
from sdmxlib.inventory import Leaf, Root, Tombstone, record, retire, sha256_hex
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.gc import GcReport, collect_garbage
from statcatlib.jobs import RetentionPolicy, _pending_document
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, BlobStoreError, _ObjectOps
from statcatlib.store.inventory import InventoryWriter

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from obstore import ObjectMeta
    from obstore.store import ObjectStore
    from sdmxlib.inventory import EventKind

URI = "az://catalogs/stc/"

#: Every digest a manifest entry requires; GC reads none of them.
_DIGESTS: dict[str, str] = {"sha256": "0" * 64, "md5": "0" * 32, "crc32c": "0" * 8}


def policy(
    *,
    backup: timedelta = timedelta(days=30),
    keep_roots: int = 2,
    upload_grace: timedelta = timedelta(hours=24),
) -> RetentionPolicy:
    """The design's retention values, with ``keep_roots = 2`` unless given."""
    return RetentionPolicy(
        backup=backup,
        gc_window=timedelta(days=35),
        tombstone_window=timedelta(days=400),
        inventory_window=timedelta(days=36),
        retire_after=timedelta(days=90),
        pod_staleness=timedelta(minutes=5),
        keep_roots=keep_roots,
        upload_grace=upload_grace,
    )


def edition_id(n: int, at: datetime) -> str:
    """A canonical UUIDv7 carrying the time *at*, told apart by *n*."""
    ms = f"{int(at.timestamp() * 1000):012x}"
    return str(uuid.UUID(f"{ms[:8]}-{ms[8:]}-7000-8000-{n:012x}"))


class RecordingOps(_ObjectOps):
    """Records every store call in order, and fails each delete of a path in `fail`."""

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def called(self, call: str) -> list[str]:
        """The paths of each call named *call*, in order."""
        return [path for name, path in self.calls if name == call]

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self.calls.append(("read", path))
        return super().read(path)

    @override
    def head(self, path: str) -> ObjectMeta | None:
        self.calls.append(("head", path))
        return super().head(path)

    @override
    def put(self, path: str, data: bytes, **kwargs) -> str | None:
        self.calls.append(("put", path))
        return super().put(path, data, **kwargs)

    @override
    def delete(self, path: str) -> None:
        self.calls.append(("delete", path))
        if path in self.fail:
            msg = f"delete {path}: refused by the test"
            raise BlobStoreError(msg, path=path)
        super().delete(path)

    @override
    def list(self, prefix: str) -> Iterator[ObjectMeta]:
        self.calls.append(("list", prefix))
        return super().list(prefix)


class GcCatalog:
    """A catalog in a `MemoryStore`: GC reads through `ops`, and the test writes around it."""

    def __init__(self) -> None:
        self.store = MemoryStore()
        self.ops = RecordingOps(self.store)
        self.location = BlobLocation(URI, self.store, ops=self.ops)
        self._plain = BlobLocation(URI, self.store)
        self.real = datetime.now(UTC)
        self.writer = InventoryWriter(BlobInventoryStorage(self._plain, actor="tests", clock=lambda: self.real))
        self.writer.write_catalog_json()
        self.head: Root | None = None
        self.leaves: dict[str, Leaf] = {}
        self.retired: list[Tombstone] = []
        self.embargo = False
        self.shas: dict[tuple[str, str], str] = {}

    # -- the store ----------------------------------------------------------------

    def put(self, path: str, data: bytes = b"x") -> None:
        """Write *path* past `ops`, so GC's record holds GC's calls alone."""
        self._plain.ops.put(path, data)

    def has(self, path: str) -> bool:
        """Whether the store holds *path*."""
        return self._plain.ops.head(path) is not None

    def paths(self) -> set[str]:
        """Every object under ``products/`` and ``pending/``."""
        return {meta["path"] for prefix in ("products", "pending") for meta in self._plain.ops.list(prefix)}

    def upload(
        self,
        product_id: str,
        edition: str,
        *,
        data: Iterable[str] = (),
        own: Iterable[str] = ("metadata.duckdb",),
        named: tuple[str, str] | None = None,
        manifest: bool = True,
    ) -> str:
        """Place an edition's files: each ``data/`` file, each of its own, then its manifest; the manifest's SHA-256.

        *named* writes a manifest naming another product and edition, and
        ``manifest=False`` writes none, as an upload that died does.
        """
        files = [EditionFile(path=f, size=1, digests=_DIGESTS, root="data", tier="required") for f in data]
        for name in data:
            self.put(f"products/{product_id}/data/{name}")
        for name in own:
            self.put(f"products/{product_id}/{edition}/{name}")
            files.append(EditionFile(path=name, size=1, digests=_DIGESTS, root="edition", tier="required"))
        product, ident = named or (product_id, edition)
        raw = msgspec.json.encode(
            Manifest(
                format_version="3.0",
                product_id=product,
                edition_id=ident,
                previous=None,
                created_at=self.real,
                producer=Producer(name="statcatlib", version="0.17.0"),
                sdmxlib_version="0.88.0",
                schema_version=SCHEMA_VERSION,
                duckdb_version="v1.5.0",
                duckdb_storage_version=69,
                ducklake_version=None,
                ducklake_spec_version=None,
                data_path=DATA_PATH,
                files=tuple(files),
                bindings=(),
                artefacts=(),
                canonical_form=CANONICAL_FORM,
            )
        )
        if manifest:
            self.put(f"products/{product_id}/{edition}/{MANIFEST_NAME}", raw)
        self.shas[product_id, edition] = sha256_hex(raw)
        return sha256_hex(raw)

    def register(self, product_id: str, edition: str) -> str:
        """Write the edition's ``pending/`` registration; its path."""
        path = f"pending/{product_id}/{edition}.json"
        self.put(path, _pending_document(edition, None))
        return path

    # -- the Inventory ------------------------------------------------------------

    def _commit(self, leaves: dict[str, Leaf], at: datetime, kind: EventKind, *, embargo: bool | None = None) -> Root:
        root = self.writer.try_commit(
            self.head, leaves, now=at, kind=kind, reason="test", retired=tuple(self.retired), embargo=embargo
        )
        self.head = root
        self.leaves |= leaves
        return root

    def record(self, product_id: str, edition: str, at: datetime, *, sha: str | None = None) -> Root:
        """Record *edition* live in one version at *at*, superseding the product's live edition."""
        held = self.leaves.get(product_id)
        previous = next((e.edition_id for e in held.editions if e.state == "live"), None) if held else None
        leaf = record(
            held,
            product_id=product_id,
            kind="data",
            lifecycle="revisable",
            edition_id=edition,
            previous=previous,
            manifest_sha256=sha or self.shas.get((product_id, edition), "0" * 64),
            schema_version=SCHEMA_VERSION,
            embargo=False,
            now=at,
            retired={t.edition_id for t in self.retired if t.product_id == product_id},
        )
        return self._commit({product_id: leaf}, at, "seal")

    def retire(self, product_id: str, edition: str, at: datetime) -> Root:
        """Retire the superseded *edition* in one version at *at*, its tombstone retired at *at*."""
        leaf, stones = retire(self.leaves[product_id], [edition], now=at)
        self.retired += stones
        return self._commit({product_id: leaf}, at, "retire")

    def touch(self, at: datetime) -> Root:
        """A version at *at* that changes no product: an embargo change."""
        self.embargo = not self.embargo
        return self._commit({}, at, "embargo-change", embargo=self.embargo)

    # -- GC -----------------------------------------------------------------------

    def gc(
        self,
        now: datetime,
        retention: RetentionPolicy | None = None,
        *,
        dry_run: bool = False,
        forecast: Iterable[str] | None = None,
        discard: Iterable[str] = (),
    ) -> GcReport:
        """Run GC through `ops` with the storage clock at *now*."""
        storage = BlobInventoryStorage(self.location, actor="gc", clock=lambda: now)
        return collect_garbage(
            self.location,
            retention or policy(),
            storage=storage,
            dry_run=dry_run,
            forecast=forecast,
            discard=discard,
        )

"""A blob catalog over a `MemoryStore` for the release job's tests: uploads, registrations and a storage clock.

`MemoryStore` stamps each object with this machine's time, so a test moves the
job's storage clock instead: `ReleaseCatalog.real` is when the catalog was made,
and `ReleaseCatalog.now` is the time every run and every commit reads. An
uploaded edition is a manifest and its listed files, with no DuckDB store behind
them. Every manifest carries one binding unless told otherwise, so the fold runs
no reference check, which needs a real edition (`tests/stores/test_release_job.py`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import msgspec
from obstore.store import MemoryStore
from sdmxlib.edition import CANONICAL_FORM, DATA_PATH, MANIFEST_NAME, ArtefactEntry, EditionFile, Manifest, Producer
from sdmxlib.inventory import FullInventory, sha256_hex
from sdmxlib.storage._manifest import BindingCount
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.admin import CatalogAdmin
from statcatlib.jobs import AccessPolicy, ExpiryPolicy, JobsConfig, Principal, RetentionPolicy, _pending_document
from statcatlib.release import ReleaseJob, ReleaseReport
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryWriter
from tests._fakes import ACCOUNT, SENTINEL, FakeListing, RecordingRest, grant
from tests._gc import RecordingOps, edition_id

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sdmxlib.edition import Lifecycle
    from sdmxlib.inventory import Kind, LeafEntry, State

URI = "az://catalogs/stc/"

#: The object ids on the allowlist: the release job and the pods.
RELEASE_JOB, PODS = "oid-release", "oid-pods"

_DAY = timedelta(days=1)


def retention(*, retire_after: timedelta = timedelta(days=90)) -> RetentionPolicy:
    """The design's retention values."""
    return RetentionPolicy(
        backup=timedelta(days=30),
        gc_window=timedelta(days=35),
        tombstone_window=timedelta(days=400),
        inventory_window=timedelta(days=36),
        retire_after=retire_after,
        pod_staleness=timedelta(minutes=5),
    )


def config(*, retire_after: timedelta = timedelta(days=90), sentinels: Iterable[str] = (SENTINEL,)) -> JobsConfig:
    """A jobs configuration whose allowlist names the release job and the pods."""
    access = AccessPolicy(
        storage_account=ACCOUNT,
        container="catalogs",
        allowlist=(Principal(RELEASE_JOB, "release job"), Principal(PODS, "pods")),
        sentinels=tuple(sentinels),
    )
    return JobsConfig(
        retention=retention(retire_after=retire_after), access=access, expiry=ExpiryPolicy(), sha256="0" * 64
    )


def artefact(urn: str, fingerprint: str, *, partial_of: str | None = None) -> ArtefactEntry:
    """A manifest artefact: full, or a partial cut from the full artefact with fingerprint *partial_of*."""
    return ArtefactEntry(
        urn=urn, stability="semantic", partial=partial_of is not None, fingerprint=fingerprint, partial_of=partial_of
    )


class ReleaseCatalog:
    """A catalog in a `MemoryStore`: the job reads and writes through `ops`, and the test writes around it."""

    def __init__(self) -> None:
        self.store = MemoryStore()
        self.ops = RecordingOps(self.store)
        self.location = BlobLocation(URI, self.store, ops=self.ops)
        self._plain = BlobLocation(URI, self.store)
        # To the second, so a time the test derives from it survives a registration's millisecond form.
        self.real = datetime.now(UTC).replace(microsecond=0)
        self.now = self.real + timedelta(minutes=1)
        self.rest = RecordingRest(self.store)
        self.listing = FakeListing([grant(RELEASE_JOB), grant(PODS)])
        self.writer = InventoryWriter(self.storage("tests"))
        self.writer.write_catalog_json()
        self._n = 0

    # -- the store ----------------------------------------------------------------

    def storage(self, actor: str, *, plain: bool = True) -> BlobInventoryStorage:
        """A storage on the test's clock; *plain* writes past `ops`, so `ops` records the job's calls alone."""
        location = self._plain if plain else self.location
        return BlobInventoryStorage(location, actor=actor, clock=lambda: self.now)

    def put(self, path: str, data: bytes = b"x") -> None:
        """Write *path* past `ops`."""
        self._plain.ops.put(path, data)

    def delete(self, path: str) -> None:
        """Delete *path* past `ops`."""
        self._plain.ops.delete(path)

    def read(self, path: str) -> bytes:
        """The bytes at *path*, read past `ops`."""
        got = self._plain.ops.read(path)
        assert got is not None, path
        return got[0]

    def has(self, path: str) -> bool:
        """Whether the store holds *path*."""
        return self._plain.ops.head(path) is not None

    def new_id(self, at: datetime | None = None) -> str:
        """A fresh edition id carrying the time *at*, or `real`; later calls give higher ids at one time."""
        self._n += 1
        return edition_id(self._n, at or self.real)

    def upload(
        self,
        product_id: str,
        edition: str | None = None,
        *,
        previous: str | None = None,
        kind: Kind | None = "data",
        lifecycle: Lifecycle | None = "revisable",
        release_at: datetime | None = None,
        register: bool = True,
        bound: bool = True,
        artefacts: Iterable[ArtefactEntry] = (),
        content: str = "c",
        schema_version: str = SCHEMA_VERSION,
        canonical_form: str = CANONICAL_FORM,
    ) -> str:
        """Place an edition as ``statcat upload`` leaves it, and register it; its edition id.

        The edition lists one ``data/`` file and one of its own. *content*
        names its content fingerprint, *bound* gives it one binding, and
        ``kind=None`` or ``lifecycle=None`` leaves the key out of its ``statcat``
        section.
        """
        edition = edition or self.new_id()
        files: list[EditionFile] = []
        for root, name in (("data", f"{edition}.parquet"), ("edition", "metadata.duckdb")):
            body = f"{product_id}/{edition}/{name}".encode()
            where = (
                f"products/{product_id}/data/{name}" if root == "data" else f"products/{product_id}/{edition}/{name}"
            )
            self.put(where, body)
            digests = {
                "sha256": hashlib.sha256(body).hexdigest(),
                "md5": hashlib.md5(body, usedforsecurity=False).hexdigest(),
                "crc32c": "0" * 8,
            }
            files.append(EditionFile(path=name, size=len(body), digests=digests, root=root, tier="required"))
        section: dict[str, object] = {"format": 3}
        if kind is not None:
            section["kind"] = kind
        if lifecycle is not None:
            section["lifecycle"] = lifecycle
        flow = f"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=T:DF_{product_id.upper()}(1.0)"
        binding = BindingCount(
            dataflow=flow, observations=1, structural_version="1", fingerprint="b" * 64, structure_fingerprint="s" * 64
        )
        manifest = Manifest(
            format_version="3.0",
            product_id=product_id,
            edition_id=edition,
            previous=previous,
            created_at=self.real,
            producer=Producer(name="statcatlib", version="0.17.0"),
            sdmxlib_version="0.88.0",
            schema_version=schema_version,
            duckdb_version="v1.5.0",
            duckdb_storage_version=69,
            ducklake_version=None,
            ducklake_spec_version=None,
            data_path=DATA_PATH,
            files=tuple(files),
            bindings=(binding,) if bound else (),
            artefacts=tuple(artefacts),
            canonical_form=canonical_form,
            content_fingerprint=hashlib.sha256(content.encode()).hexdigest(),
            extensions={"statcat": section},
        )
        self.put(self.manifest_path(product_id, edition), msgspec.json.encode(manifest))
        if register:
            self.register(product_id, edition, release_at)
        return edition

    @staticmethod
    def manifest_path(product_id: str, edition: str) -> str:
        return f"products/{product_id}/{edition}/{MANIFEST_NAME}"

    def manifest_sha(self, product_id: str, edition: str) -> str:
        return sha256_hex(self.read(self.manifest_path(product_id, edition)))

    def register(self, product_id: str, edition: str, release_at: datetime | None = None) -> str:
        """Write the edition's ``pending/`` registration; its path."""
        path = f"pending/{product_id}/{edition}.json"
        self.put(path, _pending_document(edition, release_at))
        return path

    # -- the Inventory ------------------------------------------------------------

    def full(self) -> FullInventory:
        """The head, read past `ops`."""
        return FullInventory(self.storage("tests").read).refresh()

    def seq(self) -> int:
        """The newest version's ``seq``, or 0 before the first."""
        return self.full().seq if self.writer.has_version() else 0

    def entry(self, product_id: str, edition: str) -> LeafEntry:
        """The edition's entry in the head."""
        return next(e for e in self.full().entries(product_id) if e.edition_id == edition)

    def states(self, product_id: str) -> dict[str, State]:
        """Each recorded edition's state, by edition, for *product_id*."""
        full = self.full()
        if product_id not in full.products():
            return {}
        return {entry.edition_id: entry.state for entry in full.entries(product_id)}

    def admin(self) -> CatalogAdmin:
        """A `CatalogAdmin` over the catalog, on the test's clock."""
        return CatalogAdmin.open(self._plain, actor="operator", clock=lambda: self.now)

    def record(self) -> dict[str, object]:
        """The head's ``extensions.statcat.release``."""
        found = self.full().root.extensions.get("statcat", {}).get("release")
        assert isinstance(found, dict)
        return {str(key): value for key, value in found.items()}

    # -- the job ------------------------------------------------------------------

    def job(self, jobs: JobsConfig | None = None) -> ReleaseJob:
        """A release job through `ops`, with the fakes and the test's clock."""
        return ReleaseJob(
            self.location,
            jobs or config(),
            rest=self.rest,
            listing=self.listing,
            storage=self.storage("release", plain=False),
        )

    def run(
        self, *, at: datetime | None = None, jobs: JobsConfig | None = None, dry_run: bool = False
    ) -> ReleaseReport:
        """One run of a new job, with the storage clock at *at* when given."""
        if at is not None:
            self.now = at
        return self.job(jobs).run(dry_run=dry_run)

    def live(
        self,
        product_id: str,
        *,
        previous: str | None = None,
        kind: Kind = "data",
        lifecycle: Lifecycle = "revisable",
        artefacts: Iterable[ArtefactEntry] = (),
        content: str = "c",
    ) -> str:
        """Upload an edition of *product_id* with a release time already come, fold it and release it; its id."""
        edition = self.upload(
            product_id,
            previous=previous,
            kind=kind,
            lifecycle=lifecycle,
            artefacts=artefacts,
            content=content,
            release_at=self.now,
        )
        report = self.run()
        assert f"{product_id}/{edition}" in report.released, report
        return edition

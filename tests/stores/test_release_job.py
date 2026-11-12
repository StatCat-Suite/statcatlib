"""The release job against Azurite: the 200-table release, the reference check, a crash, a re-run (Phase 5 Part 3).

Each store names Azurite on the loopback address, with its well-known key. The
digest read is `KeyedRest`, and the access listing is a `FakeListing`, since
Azurite has no role assignment. Editions are sealed in a local catalog and
uploaded as ``statcat upload`` uploads them, so each fold reads real manifests
and files, and the reference check runs `check_edition` on a pulled edition.

The job's storage clock is the test's, a `Clock` the test moves, so no test
depends on the container's clock: every registration is written at about this
machine's time, and each run reads the time the test set.
"""

from __future__ import annotations

import contextlib
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, override

import attrs
import msgspec
import pytest
import sdmxlib as sl
from sdmxlib.inventory import (
    CATALOG_NAME,
    CURRENT_NAME,
    Current,
    FullInventory,
    decode_catalog,
    decode_root,
    encode,
    root_path,
    sha256_hex,
)

import statcatlib as sc
from statcatlib.admin import CatalogAdmin
from statcatlib.gc import collect_garbage
from statcatlib.model import InternationalString
from statcatlib.release import ReleaseReport
from statcatlib.store.blob import BlobInventoryStorage, BlobLocation, _ObjectOps
from statcatlib.store.inventory import InventoryWriter
from statcatlib.testing import corpus_product_id
from tests._gc import RecordingOps
from tests.conftest import sealed_data_edition, sealed_referring_edition
from tests.stores import _stores

if TYPE_CHECKING:
    from obstore.store import ObjectStore

pytestmark = pytest.mark.stores

_MINUTE = timedelta(minutes=1)


@attrs.define
class Clock:
    """The job's storage clock: `now` is what every run reads."""

    now: datetime = attrs.field(factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _run(location: BlobLocation, clock: Clock, *, dry_run: bool = False) -> ReleaseReport:
    with _stores.release_job(location, clock=clock) as job:
        return job.run(dry_run=dry_run)


def _full(location: BlobLocation) -> FullInventory:
    return FullInventory(BlobInventoryStorage(location).read).refresh()


def _states(location: BlobLocation) -> dict[str, list[str]]:
    """Each product's entry states, in edition order."""
    return {product: [entry.state for entry in leaf.editions] for product, leaf in _full(location).leaves().items()}


@pytest.fixture
def catalog(writer: InventoryWriter, location: BlobLocation) -> BlobLocation:
    """A new catalog in Azurite, with ``catalog.json`` and no version."""
    del writer
    return location


class PoolOps(RecordingOps):
    """Records every store call, and the most reads that ran at one time."""

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self._lock = threading.Lock()
        self._active = 0
        self.most = 0

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        with self._lock:
            self._active += 1
            self.most = max(self.most, self._active)
        try:
            # Long enough that reads sent from a pool overlap whatever the emulator's latency.
            time.sleep(0.02)
            return super().read(path)
        finally:
            with self._lock:
                self._active -= 1


@pytest.fixture(scope="module")
def mix(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Path]]:
    """The 200-table mix and its structure products, sealed once in a local catalog."""
    root = tmp_path_factory.mktemp("mix") / "catalog"
    yield _stores.seal_mix(root)
    shutil.rmtree(root, ignore_errors=True)


#: The mix's data products, each to its number of tables: 200 in all.
_TABLES = {
    corpus_product_id(group, index): group.tables for group in _stores.RELEASE_MIX for index in range(group.products)
}

#: The grouped product beside the mix whose release the test has refused.
_HELD = corpus_product_id(_stores.HELD_BACK, 0)


def _preview(catalog: BlobLocation, azurite: _stores.Running, cache: Path) -> set[str]:
    """The stub pre-materialisation: open every embargoed edition through `EmbargoReader`; the products opened."""
    with contextlib.closing(sc.EmbargoReader.open(_stores.reader(catalog, azurite, cache))) as reader:
        embargoed = reader.editions()
        for entry in embargoed:
            view = reader.view(entry.product_id, entry.edition_id)
            if entry.product_id in _TABLES:
                assert view.query(sc.AggregateDataset).count() == _TABLES[entry.product_id]
        return {entry.product_id for entry in embargoed}


def _change_manifest(catalog: BlobLocation, product_id: str, edition_dir: Path) -> None:
    """Replace the stored manifest of *product_id*'s edition with other bytes, as a late overwrite would."""
    ops = _ObjectOps(catalog.store)
    path = f"products/{product_id}/{edition_dir.name}/manifest.json"
    got = ops.read(path)
    assert got is not None
    ops.delete(path)
    ops.put(path, got[0] + b"\n")


def _live_tables(catalog: BlobLocation) -> list[str]:
    """The dataflow of every table a live entry routes to."""
    return [
        flow
        for leaf in _full(catalog).leaves().values()
        for entry in leaf.editions
        if entry.state == "live" and isinstance(entry.dataflows, tuple)
        for flow in entry.dataflows
    ]


def _counts(ops: _ObjectOps) -> dict[str, int]:
    """The requests one run sent through *ops*, a `PoolOps`."""
    assert isinstance(ops, PoolOps)
    reads = ops.called("read")
    leaves = [read for read in reads if read.startswith("inventory/leaves/")]
    manifests = [read for read in reads if read.endswith("/manifest.json")]
    return {
        "leaf reads": len(leaves),
        "distinct leaves": len(set(leaves)),
        "manifest reads": len(manifests),
        "distinct manifests": len(set(manifests)),
        "most reads at one time": ops.most,
        "puts": len(ops.called("put")),
        "deletes": len(ops.called("delete")),
    }


class TestThe200TableRelease:
    def test_one_seal_version_a_preview_of_each_embargoed_edition_then_one_release_version(
        self,
        catalog: BlobLocation,
        clock: Clock,
        mix: dict[str, Path],
        azurite: _stores.Running,
        tmp_path: Path,
        record_property: Callable[[str, object], None],
    ) -> None:
        assert sum(_TABLES.values()) == 200
        release_at = clock.now + 10 * _MINUTE
        for edition_dir in mix.values():
            _stores.upload(catalog, edition_dir, release_at=release_at)

        # 08:00, the pre-flight run: every upload is folded in one `seal` version, and nothing is due.
        folding = attrs.evolve(catalog, ops=PoolOps(catalog.store))
        fold = _run(folding, clock)

        assert (fold.refused, fold.failed, fold.released) == ((), {}, ())
        assert len(fold.folded) == len(mix) == 25
        assert fold.versions == (1,)
        assert _full(catalog).root.event.kind == "seal"
        assert _states(catalog) == {product: ["embargoed"] for product in mix}

        # 08:05, the stub pre-materialisation; the public catalog still holds nothing.
        assert _preview(catalog, azurite, tmp_path / "preview-cache") == set(mix)
        public = _stores.reader(catalog, azurite, tmp_path / "public-cache")
        with sc.Catalog.open(public) as cat:
            assert cat.products() == []

        # A grouped product's manifest changes after the fold, so its release is refused (spec §6.5 item 2).
        _change_manifest(catalog, _HELD, mix[_HELD])

        # 08:30:05, the release run: one `release` version makes all 200 tables live.
        clock.now = release_at + timedelta(seconds=5)
        releasing = attrs.evolve(catalog, ops=PoolOps(catalog.store))
        release = _run(releasing, clock)

        assert (release.failed, release.folded) == ({}, ())
        assert release.versions == (2,)
        assert _full(catalog).root.event.kind == "release"
        assert [(r.product_id, r.step, r.rule) for r in release.refused] == [(_HELD, "release", "manifest")]
        assert _states(catalog) == {product: ["embargoed" if product == _HELD else "live"] for product in mix}
        tables = _live_tables(catalog)
        assert len(tables) == len(set(tables)) == 200
        with sc.Catalog.open(public) as cat:
            assert set(cat.products()) == set(mix) - {_HELD}

        # The request counts of the two runs, recorded with the test's properties.
        folded, released = _counts(folding.ops), _counts(releasing.ops)
        for step, counts in (("pre-flight run", folded), ("release run", released)):
            for name, count in counts.items():
                record_property(f"{step}: {name}", count)
        # The pre-flight run folds onto no head, then its release and retire steps each read the 25 new leaves.
        assert (folded["leaf reads"], folded["distinct leaves"]) == (50, 25)
        # Each registered manifest is read once by the fold's checks and once by the writer's rules.
        assert (folded["manifest reads"], folded["distinct manifests"]) == (50, 25)
        # 25 leaves, the root and CURRENT.
        assert (folded["puts"], folded["deletes"]) == (27, 0)
        # The release run reads the 25 leaves in each of its three steps, on the reader's pool, and the commit
        # reads the 24 changed leaves of the base version for the change list. The retire step reads the new
        # head, whose 24 released products have new leaves.
        assert (released["leaf reads"], released["distinct leaves"]) == (3 * 25 + 24, 25 + 24)
        assert released["most reads at one time"] > 1
        # Each incoming manifest is read once, the changed one included (spec §6.5 item 2).
        assert (released["manifest reads"], released["distinct manifests"]) == (25, 25)
        # 24 leaves, the root and CURRENT.
        assert (released["puts"], released["deletes"]) == (26, 0)

        # A second run against the unchanged catalog writes nothing.
        again = attrs.evolve(catalog, ops=PoolOps(catalog.store))
        rerun = _run(again, clock)

        assert (rerun.folded, rerun.released, rerun.retired, rerun.versions) == ((), (), (), ())
        assert [(r.product_id, r.rule) for r in rerun.refused] == [(_HELD, "manifest")]
        assert (_counts(again.ops)["puts"], _counts(again.ops)["deletes"]) == (0, 0)
        assert _full(catalog).seq == 2


@pytest.fixture(scope="module")
def structures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Three structure products, where ``structure-concepts`` refers to codelists the other two hold."""
    concepts = sealed_referring_edition(tmp_path_factory.mktemp("structures") / "catalog")
    products = concepts.parent.parent
    found = {"structure-concepts": concepts}
    for name in ("structure-geo", "structure-classifications"):
        [edition_dir] = [child for child in (products / name).iterdir() if (child / "manifest.json").is_file()]
        found[name] = edition_dir
    return found


class TestTheReferenceCheck:
    def test_a_reference_that_resolves_in_no_structure_product_is_refused(
        self, catalog: BlobLocation, clock: Clock, structures: dict[str, Path]
    ) -> None:
        concepts = _stores.upload(catalog, structures["structure-concepts"])

        report = _run(catalog, clock)

        assert report.folded == ()
        [refusal] = report.refused
        assert (refusal.product_id, refusal.edition_id, refusal.rule) == (
            "structure-concepts",
            concepts.edition_id,
            "references",
        )
        assert "CL_GEO" in refusal.reason
        assert "no structure edition" in refusal.reason
        assert report.failed == {}

    def test_one_that_resolves_in_another_structure_product_is_recorded(
        self, catalog: BlobLocation, clock: Clock, structures: dict[str, Path]
    ) -> None:
        for name in ("structure-geo", "structure-classifications"):
            _stores.upload(catalog, structures[name])
        assert len(_run(catalog, clock).folded) == 2
        concepts = _stores.upload(catalog, structures["structure-concepts"])

        report = _run(catalog, clock)

        assert report.refused == ()
        assert report.folded == (f"structure-concepts/{concepts.edition_id}",)
        assert _states(catalog)["structure-concepts"] == ["embargoed"]

    def test_the_folds_own_structure_editions_resolve_each_other(
        self, catalog: BlobLocation, clock: Clock, structures: dict[str, Path]
    ) -> None:
        for edition_dir in structures.values():
            _stores.upload(catalog, edition_dir)

        report = _run(catalog, clock)

        assert report.refused == ()
        assert len(report.folded) == 3
        assert len(report.versions) == 1

    def test_a_reference_into_a_refused_fold_edition_is_refused_with_it(
        self, catalog: BlobLocation, clock: Clock, structures: dict[str, Path]
    ) -> None:
        geo = _stores.upload(catalog, structures["structure-geo"])
        _stores.upload(catalog, structures["structure-classifications"])
        _stores.upload(catalog, structures["structure-concepts"])
        ops = _ObjectOps(catalog.store)
        ops.delete(f"products/structure-geo/{geo.edition_id}/metadata.duckdb")

        report = _run(catalog, clock)

        rules = {r.product_id: r.rule for r in report.refused}
        assert rules == {"structure-geo": "files", "structure-concepts": "references"}
        assert report.folded == (f"structure-classifications/{_states_edition(catalog, 'structure-classifications')}",)


def _states_edition(location: BlobLocation, product_id: str) -> str:
    [entry] = _full(location).entries(product_id)
    return entry.edition_id


@attrs.frozen
class Chain:
    """A local catalog: product ``a`` with edition ``first``, and two editions both built on it."""

    first: Path
    second: Path
    twin: Path


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> Chain:
    root = tmp_path_factory.mktemp("chain") / "catalog"
    first = sealed_data_edition(root, product_id="a")
    copy = root.parent / "copy"
    shutil.copytree(root, copy)
    made: list[Path] = []
    for where, identifier in ((root, "d2"), (copy, "d3")):
        with sc.Catalog.create(where, exist_ok=True) as cat:
            wc = cat.product("a").working_copy()
            title = InternationalString(en=identifier)
            wc.put(sc.OpenDataset(identifier=identifier, title=title, description=InternationalString()))
            edition = wc.seal()
        made.append(where / "products" / "a" / edition.edition_id)
    return Chain(first, made[0], made[1])


def _flow_rows(location: BlobLocation, azurite: _stores.Running, cache: Path) -> int:
    flow = sl.Dataflow.urn_for(agency="T", id="DF_F", version="1.0")
    with sc.Catalog.open(_stores.reader(location, azurite, cache)) as cat:
        return cat.product("a").view().registry.obs(flow).frame().height


class TestACrash:
    def test_a_crash_before_the_release_root_leaves_every_entry_embargoed_and_the_next_run_releases(
        self,
        catalog: BlobLocation,
        clock: Clock,
        chain: Chain,
        azurite: _stores.Running,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release_at = clock.now + 10 * _MINUTE
        first = _stores.upload(catalog, chain.first, release_at=release_at)
        assert _run(catalog, clock).folded == (f"a/{first.edition_id}",)
        clock.now = release_at + timedelta(seconds=5)

        class CrashedError(Exception):
            """The process died."""

        with _stores.release_job(catalog, clock=clock) as job:
            real = job._storage.create

            def dying(path: str, data: bytes) -> None:
                if path == root_path(2):
                    raise CrashedError
                real(path, data)

            monkeypatch.setattr(job._storage, "create", dying)
            with pytest.raises(CrashedError):
                job.run()

        assert _full(catalog).seq == 1
        assert _states(catalog) == {"a": ["embargoed"]}

        report = _run(catalog, clock)

        assert report.released == (f"a/{first.edition_id}",)
        assert _states(catalog) == {"a": ["live"]}
        assert _flow_rows(catalog, azurite, tmp_path / "cache") > 0


class TestAForkAndADiscard:
    def test_after_a_refused_fork_a_discard_of_one_lets_the_next_run_fold_the_other(
        self, catalog: BlobLocation, clock: Clock, chain: Chain
    ) -> None:
        live = _stores.upload(catalog, chain.first, release_at=clock.now + _MINUTE)
        clock.now += 2 * _MINUTE
        assert _run(catalog, clock).released == (f"a/{live.edition_id}",)
        b1 = _stores.upload(catalog, chain.second)
        b2 = _stores.upload(catalog, chain.twin)

        refused = _run(catalog, clock)

        assert {(r.edition_id, r.rule) for r in refused.refused} == {
            (b1.edition_id, "pending-fork"),
            (b2.edition_id, "pending-fork"),
        }
        assert _states(catalog) == {"a": ["live"]}

        storage = BlobInventoryStorage(catalog, actor="gc", clock=clock)
        policy = _stores.jobs_config().retention
        gc = collect_garbage(catalog, policy, storage=storage, discard=[f"a/{b1.edition_id}"])
        assert f"pending/a/{b1.edition_id}.json" in gc.deleted
        assert gc.failed == {}

        report = _run(catalog, clock)

        assert report.folded == (f"a/{b2.edition_id}",)
        assert report.refused == ()
        entries = {entry.edition_id: entry.state for entry in _full(catalog).entries("a")}
        assert entries == {live.edition_id: "live", b2.edition_id: "embargoed"}


class TestARestore:
    def test_after_a_restore_to_s_nothing_cancelled_after_s_goes_live(
        self, catalog: BlobLocation, clock: Clock, chain: Chain
    ) -> None:
        # The job's clock runs an hour ahead, so every registration is older than the restore's root whatever
        # the container's clock reads.
        clock.now += timedelta(hours=1)
        live = _stores.upload(catalog, chain.first, release_at=clock.now - 30 * _MINUTE)
        assert _run(catalog, clock).released == (f"a/{live.edition_id}",)
        restore_point = _full(catalog).seq
        release_at = clock.now + 10 * _MINUTE
        cancelled = _stores.upload(catalog, chain.second, release_at=release_at)
        assert _run(catalog, clock).folded == (f"a/{cancelled.edition_id}",)
        admin = CatalogAdmin.open(catalog, actor="operator", clock=clock)
        admin.cancel("a", cancelled.edition_id, reason="jdoe: wrong data")
        assert _states(catalog) == {"a": ["live", "withdrawn"]}

        self._restore_to(catalog, restore_point, clock)

        assert _states(catalog) == {"a": ["live"]}
        clock.now = release_at + timedelta(seconds=5)

        report = _run(catalog, clock)

        # The registration is still in `pending/`, so the fold records the edition again, with no release time.
        assert report.folded == (f"a/{cancelled.edition_id}",)
        assert report.released == ()
        entries = {entry.edition_id: (entry.state, entry.release_at) for entry in _full(catalog).entries("a")}
        assert entries == {live.edition_id: ("live", None), cancelled.edition_id: ("embargoed", None)}
        clock.now += timedelta(days=30)
        assert _run(catalog, clock).released == ()

    @staticmethod
    def _restore_to(location: BlobLocation, seq: int, clock: Clock) -> None:
        """Lose the versions after *seq*, bump ``catalog.json``'s epoch, and record the restore, as a restore does."""
        ops = _ObjectOps(location.store)
        storage = BlobInventoryStorage(location, actor="restore", clock=clock)
        later = seq + 1
        while ops.head(root_path(later)) is not None:
            ops.delete(root_path(later))
            later += 1
        kept = storage.read(root_path(seq))
        raw = storage.read(CATALOG_NAME)
        assert kept is not None
        assert raw is not None
        base = decode_root(kept)
        ops.put(CURRENT_NAME, encode(Current(seq=seq, root_sha256=sha256_hex(kept), restore_epoch=base.restore_epoch)))
        catalog = decode_catalog(raw)
        ops.put(CATALOG_NAME, encode(msgspec.structs.replace(catalog, restore_epoch=catalog.restore_epoch + 1)))
        InventoryWriter(storage).try_commit(base, {}, kind="restore", reason="tests: restore")

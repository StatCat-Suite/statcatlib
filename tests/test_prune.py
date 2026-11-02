"""`CatalogAdmin.prune` and ``statcat prune``: the narrow laptop prune (Phase 2a S4, RFC D32, decision P7).

Prune retires superseded and withdrawn editions older than one window,
deletes the directories of editions tombstoned for longer than the window,
and deletes ``data/`` files no manifest on disk lists once they are older
than the window. It skips a product with a working copy or a seal journal
entry, deletes no Inventory root or leaf, and deletes nothing in a dry run.

The tests move prune's clock forward by patching ``statcatlib.admin.datetime``,
so an edition sealed a moment ago is days old to prune; a file stays newer
than the window by having its mtime set past the patched clock.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import sdmxlib
import sdmxlib.edition
from sdmxlib import InternationalString, Ref
from sdmxlib.inventory import FullInventory, Root, local_fetch
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib.admin import CatalogAdmin, PruneReport
from statcatlib.cli import app
from tests.conftest import concept_of

pytestmark = pytest.mark.unit

runner = CliRunner()

DAY = timedelta(days=1)
REASON = "reclaim disk"


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=InternationalString(en=identifier), description=InternationalString(en="d")
    )


def _observations(wc: sc.WorkingCopy) -> None:
    """Write a dataflow, its structures and one bound observation: a Parquet file under ``data/``."""
    scheme = sdmxlib.ConceptScheme(
        id="CS_TEST",
        maintainer="TEST",
        name=InternationalString(en="Concepts"),
        concepts=[sdmxlib.Concept(id="FREQ", name=InternationalString(en="Frequency"))],
    )
    cl = sdmxlib.Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=InternationalString(en="Frequency"),
        codes=[sdmxlib.Code(id="A", name=InternationalString(en="Annual"))],
    )
    dsd = sdmxlib.DataStructure(
        id="DSD_C",
        maintainer="TEST",
        name=InternationalString(en="DSD"),
        dimensions=[sdmxlib.Dimension(id="FREQ", concept=concept_of("FREQ"), representation=cl)],
    )
    flow = sdmxlib.Dataflow(id="DF_C", maintainer="TEST", name=InternationalString(en="Flow"), structure=dsd)
    reg = wc.registry
    reg.add_all([scheme, cl, dsd, flow])
    reg.write_observations(schema="obs", table="df_c", source=pl.DataFrame({"FREQ": ["A"]}))
    reg.add(sdmxlib.DataflowBinding.from_dsd(dsd, dataflow=Ref.to(flow), schema="obs", table="df_c"))


def _seal(root: Path, product: str, identifier: str, *, observations: bool = False) -> str:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(product).working_copy()
        if observations:
            _observations(wc)
        wc.put(_dataset(identifier))
        return wc.seal().edition_id


class _Ahead:
    """Stands in for `datetime` in `statcatlib.admin`, whose only call is ``datetime.now(UTC)``."""

    def __init__(self, ahead: timedelta) -> None:
        self.ahead = ahead

    def now(self, tz: tzinfo | None = None) -> datetime:
        return datetime.now(tz) + self.ahead


def _clock(monkeypatch: pytest.MonkeyPatch, ahead: timedelta) -> None:
    """Run prune's clock *ahead* of the real one."""
    monkeypatch.setattr("statcatlib.admin.datetime", _Ahead(ahead))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file and directory under *root*, with its size and mtime."""
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def _head(root: Path) -> FullInventory:
    return FullInventory(local_fetch(root)).refresh()


def _states(root: Path, product: str) -> dict[str, str]:
    return {e.edition_id: e.state for e in CatalogAdmin.open(root).editions(product)}


class Fixture:
    """The catalog the tests prune, and the names in it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        # Product p: A (observations) superseded by B, which carries A's data files.
        self.a = _seal(root, "p", "a", observations=True)
        self.b = _seal(root, "p", "b")
        self.orphan = root / "products" / "p" / "data" / "orphan.parquet"
        self.orphan.write_bytes(b"left by a discarded working copy")
        # Product w: an old superseded edition, a stale working copy, an orphan data file.
        self.w_old = _seal(root, "w", "w1")
        self.w_live = _seal(root, "w", "w2")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            stale = cat.product("w").working_copy()
            self.w_stale = stale.edition_id
            stale.close()
        self.w_orphan = root / "products" / "w" / "data" / "orphan.parquet"
        self.w_orphan.parent.mkdir(parents=True, exist_ok=True)
        self.w_orphan.write_bytes(b"w")
        # Product j: an old superseded edition and a seal journal entry.
        self.j_old = _seal(root, "j", "j1")
        self.j_live = _seal(root, "j", "j2")
        journal = root / "products" / "j" / "journal"
        journal.mkdir(exist_ok=True)
        (journal / "01a18e00-0000-7000-8000-000000000042.json").write_bytes(b"")

    @property
    def p_data(self) -> list[Path]:
        """Product p's data files that B's manifest lists."""
        product = self.root / "products" / "p"
        files = sdmxlib.edition.edition_files(sdmxlib.edition.read_manifest(product / self.b))
        return [product / f for f in files if f.startswith("data/")]


@pytest.fixture
def cat(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path / "cat")


def _prune(root: Path, *, apply: bool, ahead: timedelta, monkeypatch: pytest.MonkeyPatch) -> PruneReport:
    _clock(monkeypatch, ahead)
    return CatalogAdmin.open(root).prune(older_than=DAY, reason=REASON, dry_run=not apply)


# -- arguments ---------------------------------------------------------------


@pytest.mark.parametrize("window", [timedelta(0), -DAY])
def test_the_window_must_be_positive(cat: Fixture, window: timedelta) -> None:
    with pytest.raises(ValueError, match="positive window"):
        CatalogAdmin.open(cat.root).prune(older_than=window, reason=REASON)


def test_a_reason_is_required(cat: Fixture) -> None:
    with pytest.raises(ValueError, match="needs a reason"):
        CatalogAdmin.open(cat.root).prune(older_than=DAY, reason=" ")


# -- a dry run ---------------------------------------------------------------


def test_a_dry_run_deletes_nothing(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)  # tombstone A
    cat.orphan.write_bytes(b"another orphan")
    before = _snapshot(cat.root)
    seq = _head(cat.root).root.seq

    report = _prune(cat.root, apply=False, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert _snapshot(cat.root) == before
    assert _head(cat.root).root.seq == seq
    assert report.dry_run
    assert report.seq is None
    assert report.deleted_dirs == (f"products/p/{cat.a}",)
    assert "products/p/data/orphan.parquet" in report.deleted_files
    assert report.failed == {}


def test_prune_is_a_dry_run_by_default(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _snapshot(cat.root)
    _clock(monkeypatch, 10 * DAY)
    report = CatalogAdmin.open(cat.root).prune(older_than=DAY, reason=REASON)
    assert report.dry_run
    assert report.retired == (f"p/{cat.a}",)
    assert _snapshot(cat.root) == before
    assert _states(cat.root, "p")[cat.a] == "superseded"


# -- skipped products --------------------------------------------------------


def test_a_product_with_a_working_copy_or_a_journal_entry_is_skipped_and_reported(
    cat: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)
    report = _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert set(report.skipped_products) == {"w", "j"}
    assert cat.w_stale in report.skipped_products["w"]
    assert "working copy" in report.skipped_products["w"]
    assert "journal" in report.skipped_products["j"]
    assert _states(cat.root, "w")[cat.w_old] == "superseded"
    assert _states(cat.root, "j")[cat.j_old] == "superseded"
    assert cat.w_orphan.is_file()
    assert (cat.root / "products" / "w" / cat.w_stale).is_dir()
    assert not any(path.startswith(("products/w/", "products/j/")) for path in report.deleted_files)


# -- what is deleted, and what is kept ---------------------------------------


def test_tombstoned_directories_and_unreferenced_data_files_past_the_window_are_deleted(
    cat: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)

    assert first.retired == (f"p/{cat.a}",)
    assert first.seq == _head(cat.root).root.seq
    assert _head(cat.root).root.event.kind == "retire"
    assert _head(cat.root).root.event.reason == REASON
    assert cat.a not in _states(cat.root, "p")
    assert [(t.product_id, t.edition_id) for t in _head(cat.root).retired] == [("p", cat.a)]
    # The tombstone is new, so A's directory waits for a later run.
    assert first.deleted_dirs == ()
    assert (cat.root / "products" / "p" / cat.a).is_dir()
    assert first.deleted_files == ("products/p/data/orphan.parquet",)
    assert not cat.orphan.exists()

    second = _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert second.retired == ()
    assert second.seq is None
    assert second.deleted_dirs == (f"products/p/{cat.a}",)
    assert not (cat.root / "products" / "p" / cat.a).exists()
    # B's manifest lists the data files A wrote, so they stay.
    assert cat.p_data
    assert all(f.is_file() for f in cat.p_data)
    assert second.failed == {}
    # The tombstone is kept.
    assert [(t.product_id, t.edition_id) for t in _head(cat.root).retired] == [("p", cat.a)]


def test_a_data_file_newer_than_the_window_survives(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    ahead = 10 * DAY
    newer = (datetime.now(UTC) + ahead).timestamp()
    os.utime(cat.orphan, (newer, newer))

    report = _prune(cat.root, apply=True, ahead=ahead, monkeypatch=monkeypatch)

    assert cat.orphan.is_file()
    assert report.deleted_files == ()


def test_live_and_embargoed_editions_are_kept(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = CatalogAdmin.open(cat.root)
    admin.set_embargo(on=True, reason="hold")
    embargoed = _seal(cat.root, "p", "c")

    _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)
    _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert _states(cat.root, "p") == {cat.b: "live", embargoed: "embargoed"}
    assert (cat.root / "products" / "p" / cat.b / "manifest.json").is_file()
    assert (cat.root / "products" / "p" / embargoed / "manifest.json").is_file()
    with sc.Catalog.open(cat.root) as opened:
        assert opened.product("p").view().get(sc.OpenDataset, "b") is not None


def test_no_root_or_leaf_is_deleted(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = cat.root / "inventory"
    before = {p.relative_to(inventory).as_posix() for p in inventory.rglob("*.json")}

    _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)
    _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    after = {p.relative_to(inventory).as_posix() for p in inventory.rglob("*.json")}
    assert before < after
    assert (cat.root / "catalog.json").is_file()
    assert (cat.root / "CURRENT").is_file()


def test_a_lost_race_replans_against_the_new_head(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback lands while prune commits: A is live again on the new head, so prune retires nothing of p."""
    admin = CatalogAdmin.open(cat.root)
    writer = admin._writer
    original = writer.try_commit
    calls: list[int] = []

    def raced(*args: Any, **kwargs: Any) -> Root:
        if not calls:
            calls.append(1)
            CatalogAdmin.open(cat.root).rollback("p", reason="the new figures are wrong")
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "try_commit", raced)
    _clock(monkeypatch, 10 * DAY)
    report = admin.prune(older_than=DAY, reason=REASON, dry_run=False)

    assert report.retired == ()
    assert report.seq is None
    assert _states(cat.root, "p") == {cat.a: "live", cat.b: "withdrawn"}


# -- failures ----------------------------------------------------------------


def test_a_failing_deletion_is_recorded_and_the_run_continues(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)
    cat.orphan.write_bytes(b"another orphan")
    old = datetime.now(UTC).timestamp()
    os.utime(cat.orphan, (old, old))

    def locked(path: Path) -> None:
        msg = f"the process cannot access {path}: another process has it attached"
        raise PermissionError(msg)

    with monkeypatch.context() as mp:
        mp.setattr("statcatlib.admin.shutil.rmtree", locked)
        report = _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=mp)

    edition = f"products/p/{cat.a}"
    assert list(report.failed) == [edition]
    assert "another process" in report.failed[edition]
    assert report.deleted_dirs == ()
    # The run went on to step 3.
    assert report.deleted_files == ("products/p/data/orphan.parquet",)
    assert not cat.orphan.exists()
    assert (cat.root / edition).is_dir()

    retried = _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert retried.deleted_dirs == (edition,)
    assert retried.failed == {}
    assert not (cat.root / edition).exists()


def test_a_partly_deleted_edition_directory_is_retried_and_not_taken_for_a_working_copy(
    cat: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prune(cat.root, apply=True, ahead=10 * DAY, monkeypatch=monkeypatch)
    edition = cat.root / "products" / "p" / cat.a
    (edition / "manifest.json").unlink()

    report = _prune(cat.root, apply=True, ahead=20 * DAY, monkeypatch=monkeypatch)

    assert "p" not in report.skipped_products
    assert report.deleted_dirs == (f"products/p/{cat.a}",)
    assert not edition.exists()


def test_a_catalog_with_no_inventory_version_has_nothing_to_prune(tmp_path: Path) -> None:
    sc.Catalog.create(tmp_path / "empty").close()
    report = CatalogAdmin.open(tmp_path / "empty").prune(older_than=DAY, reason=REASON, dry_run=False)
    assert report == PruneReport(
        retired=(),
        deleted_dirs=(),
        deleted_files=(),
        skipped_products={},
        failed={},
        root=(tmp_path / "empty").absolute(),
        dry_run=False,
        seq=None,
    )


# -- statcat prune -----------------------------------------------------------


def _cli(root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["prune", str(root), *args])
    return result.exit_code, result.output


def test_the_cli_is_a_dry_run_by_default(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, 10 * DAY)
    before = _snapshot(cat.root)

    code, out = _cli(cat.root, "--older-than", "1", "--reason", REASON)

    assert code == 0, out
    assert _snapshot(cat.root) == before
    assert f"retire p/{cat.a}" in out
    assert "delete products/p/data/orphan.parquet" in out
    assert "skip w: working copy" in out
    assert "This dry run changed nothing." in out


def test_the_cli_prunes_with_apply(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, 10 * DAY)

    code, out = _cli(cat.root, "--older-than", "1", "--reason", REASON, "--apply")

    assert code == 0, out
    assert "Prune retired 1 edition(s), deleted 0 edition directories and 1 data file(s)" in out
    assert cat.a not in _states(cat.root, "p")
    assert not cat.orphan.exists()


def test_the_cli_requires_a_reason(cat: Fixture) -> None:
    before = _snapshot(cat.root)
    code, out = _cli(cat.root, "--older-than", "1", "--apply")
    assert code == 2
    assert "--reason" in out
    assert _snapshot(cat.root) == before


def test_the_cli_exits_1_when_a_deletion_fails(cat: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    _clock(monkeypatch, 10 * DAY)
    _cli(cat.root, "--older-than", "1", "--reason", REASON, "--apply")
    _clock(monkeypatch, 20 * DAY)

    def locked(path: Path) -> None:
        msg = f"cannot delete {path}"
        raise PermissionError(msg)

    monkeypatch.setattr("statcatlib.admin.shutil.rmtree", locked)
    code, out = _cli(cat.root, "--older-than", "1", "--reason", REASON, "--apply")

    assert code == 1, out
    assert f"FAIL products/p/{cat.a}: cannot delete" in out
    assert "the next run tries them again" in out


def test_the_cli_refuses_a_directory_that_is_no_catalog(tmp_path: Path) -> None:
    code, out = _cli(tmp_path, "--older-than", "1", "--reason", REASON)
    assert code == 1
    assert "Not a statcatlib catalog" in out

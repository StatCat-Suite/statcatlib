"""A seal killed at each step, then repaired by the journal rule (RFC §6, spec §6.7, CUJ 3).

Each test kills the writer at one named step of `WorkingCopy.seal` by raising
an exception no code catches, the way a killed process stops. A new catalog
object stands for the next process: its `Product.working_copy` settles the
product's journal first. Afterwards exactly one edition is live, nothing is
recorded twice, and the live edition's ``previous`` is the one expected.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import sdmxlib
import sdmxlib.edition
from sdmxlib import InternationalString, Ref
from sdmxlib.edition import JournalEntry, ManifestError
from sdmxlib.inventory import FullInventory, LeafEntry, local_fetch, record, rollback
from sdmxlib.storage.schema import SCHEMA_VERSION

import statcatlib as sc
from statcatlib.store import inventory as inventory_module
from statcatlib.store.inventory import Change, InventoryWriter
from statcatlib.store.working_copy import record_journalled
from tests.conftest import concept_of

pytestmark = pytest.mark.integration


class Killed(BaseException):
    """The process died here."""


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=sc.InternationalString(en=identifier), description=sc.InternationalString(en="d")
    )


def _seal(root: Path, *identifiers: str) -> sc.Edition:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put([_dataset(i) for i in identifiers])
        return wc.seal()


def _entries(root: Path) -> list[LeafEntry]:
    return list(FullInventory(local_fetch(root)).refresh().entries("p"))


def _journal(root: Path) -> list[str]:
    folder = root / "products" / "p" / "journal"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _after(target: Callable[..., Any], *, when: Callable[..., bool] = lambda *a, **k: True) -> Callable[..., Any]:
    """*target*, killed right after it returns, when *when* holds for its arguments."""

    def killed(*args: object, **kwargs: object) -> Any:
        result = target(*args, **kwargs)
        if when(*args, **kwargs):
            raise Killed
        return result

    return killed


def _is_root(path: Path, _data: bytes) -> bool:
    return path.parent.name == "inventory"


#: Each named seal step, and how to kill the writer right after it.
_STEPS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "after the journal": lambda mp: mp.setattr(
        "sdmxlib.edition._write.write_journal", _after(sdmxlib.edition._write.write_journal)
    ),
    "after compaction": lambda mp: mp.setattr(
        "sdmxlib.edition._write.compact_for_publication", _after(sdmxlib.edition._write.compact_for_publication)
    ),
    "after the manifest": lambda mp: mp.setattr("sdmxlib.edition.seal", _after(sdmxlib.edition.seal)),
    "after the leaves": lambda mp: mp.setattr(InventoryWriter, "_write_leaf", _after(InventoryWriter._write_leaf)),
    "after the root": lambda mp: mp.setattr(
        inventory_module, "create_exclusive", _after(inventory_module.create_exclusive, when=_is_root)
    ),
    "after CURRENT": lambda mp: mp.setattr(
        InventoryWriter, "_advance_current", _after(InventoryWriter._advance_current)
    ),
    "before the journal is cleared": lambda mp: mp.setattr("sdmxlib.edition.clear_journal", _after(lambda *a: None)),
}

#: The steps after which the edition has no manifest: it stays an unsealed working copy.
_BEFORE_THE_MANIFEST = {"after the journal", "after compaction"}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A catalog whose product ``p`` has one live edition, A."""
    path = tmp_path / "cat"
    _seal(path, "a")
    return path


@pytest.mark.parametrize("step", list(_STEPS))
def test_a_kill_at_each_step_is_repaired(step: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (a,) = _entries(root)
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put(_dataset("b"))
        b = wc.edition_id
        with monkeypatch.context() as mp:
            _STEPS[step](mp)
            with pytest.raises(Killed):
                wc.seal()

    with sc.Catalog.create(root, exist_ok=True) as cat:
        if step in _BEFORE_THE_MANIFEST:
            with pytest.raises(sc.WorkingCopyExistsError, match=b):
                cat.product("p").working_copy()
            cat.product("p").working_copy(resume=True).discard()
            expected_live, expected_previous = a.edition_id, None
        else:
            expected_live, expected_previous = b, a.edition_id
        nxt = cat.product("p").working_copy()
        assert nxt.previous == expected_live
        nxt.close()

    entries = _entries(root)
    live = [e for e in entries if e.state == "live"]
    assert [e.edition_id for e in live] == [expected_live]
    assert live[0].previous == expected_previous
    assert len({e.edition_id for e in entries}) == len(entries)
    assert _journal(root) == []


def test_a_late_non_fast_forward_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback lands between the pre-check and the record; the sealed edition stays unrecorded."""
    root = tmp_path / "cat"
    a = _seal(root, "a")
    b = _seal(root, "a", "b")
    writer = InventoryWriter(root)

    def roll_back(path: Path, **kwargs: Any) -> object:
        manifest = real_seal(path, **kwargs)

        def change(full: FullInventory | None) -> Change:
            assert full is not None
            leaf = rollback(full.leaf("p"), schema_version=SCHEMA_VERSION, if_live=b.edition_id, now=datetime.now(UTC))
            return Change(leaves={"p": leaf})

        writer.commit(change, kind="rollback", reason="test")
        return manifest

    real_seal = sdmxlib.edition.seal
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        assert wc.previous == b.edition_id
        c = wc.edition_id
        monkeypatch.setattr("sdmxlib.edition.seal", roll_back)
        with pytest.raises(sc.NonFastForwardError, match=b.edition_id) as caught:
            wc.seal()
        monkeypatch.undo()
        assert caught.value.rule == "fast-forward"
        assert (root / "products" / "p" / c / "manifest.json").is_file()
        assert _journal(root) == []
        nxt = cat.product("p").working_copy()
        assert nxt.previous == a.edition_id
        nxt.close()
    states = {e.edition_id: e.state for e in _entries(root)}
    assert states == {a.edition_id: "live", b.edition_id: "withdrawn"}


def test_a_seal_on_a_base_no_longer_live_is_refused_before_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "cat"
    a = _seal(root, "a")
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("p").working_copy()
        wc.put(_dataset("b"))
        # The edition this writer records has no manifest on disk, so its rules read no artefacts.
        writer = InventoryWriter(root, artefacts=lambda *_args: {})

        def supersede(full: FullInventory | None) -> Change:
            assert full is not None
            leaf = record(
                full.leaf("p"),
                product_id="p",
                kind="data",
                lifecycle="revisable",
                edition_id="01a18e00-0000-7000-8000-000000000099",
                previous=a.edition_id,
                manifest_sha256="0" * 64,
                schema_version=SCHEMA_VERSION,
                embargo=False,
                now=datetime.now(UTC),
            )
            return Change(leaves={"p": leaf})

        # Another writer records an edition over A while this working copy is open.
        writer.commit(supersede, kind="seal", reason="another writer")
        with pytest.raises(sc.NonFastForwardError, match=r"discard\(\).*working_copy\(\)"):
            wc.seal()
        assert not (root / "products" / "p" / wc.edition_id / "manifest.json").exists()
        assert _journal(root) == []
        wc.discard()


def _coded(wc: sc.WorkingCopy, code: str) -> None:
    """Write a dataflow with one coded column holding *code*, and bind it."""
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
    reg.add_all([cl, dsd, flow])
    reg.write_observations(schema="obs", table="df_c", source=pl.DataFrame({"FREQ": [code]}))
    if reg.binding_for(flow) is None:
        reg.add(sdmxlib.DataflowBinding.from_dsd(dsd, dataflow=Ref.to(flow), schema="obs", table="df_c"))


def test_an_out_of_codelist_value_leaves_no_journal_entry(catalog: sc.Catalog, tmp_path: Path) -> None:
    """sdmxlib refuses the seal before its journal entry; the caller resumes, fixes and seals."""
    wc = catalog.product("p").working_copy()
    _coded(wc, "Q")
    with pytest.raises(ManifestError, match="Q"):
        wc.seal()
    assert _journal(tmp_path / "catalog") == []
    with pytest.raises(sc.WorkingCopyClosedError, match=r"resume=True"):
        wc.view()
    resumed = catalog.product("p").working_copy(resume=True)
    _coded(resumed, "A")
    edition = resumed.seal()
    assert edition.edition_id == wc.edition_id
    assert [e.state for e in _entries(tmp_path / "catalog")] == ["live"]


def test_a_duplicate_with_the_same_digest_is_settled() -> None:
    """Another writer recorded the journalled edition meanwhile: the repair counts it as recorded."""
    now = datetime.now(UTC)
    edition = "01a18e00-0000-7000-8000-000000000001"
    leaf = record(
        None,
        product_id="p",
        kind="data",
        lifecycle="revisable",
        edition_id=edition,
        previous=None,
        manifest_sha256="a" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=False,
        now=now,
    )
    entry = JournalEntry(product_id="p", edition_id=edition, previous=None, sealed_at=now)
    common: dict[str, Any] = {"product_id": "p", "lifecycle": "revisable", "embargo": False, "retired": (), "now": now}

    same, settled, refused = record_journalled(leaf, [(entry, "a" * 64, SCHEMA_VERSION, "data")], **common)
    assert (same, settled, refused) == (None, [edition], {})

    other, settled, refused = record_journalled(leaf, [(entry, "b" * 64, SCHEMA_VERSION, "data")], **common)
    assert other is None
    assert settled == []
    assert refused[edition].rule == "duplicate"


def test_an_empty_journal_entry_raises_journal_error(root: Path) -> None:
    journal = root / "products" / "p" / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "01a18e00-0000-7000-8000-000000000042.json").write_bytes(b"")
    with sc.Catalog.create(root, exist_ok=True) as cat, pytest.raises(sc.JournalError, match="statcat doctor"):
        cat.product("p").working_copy()

"""``statcat verify-catalog`` and ``statcat doctor`` (Phase 2a S4, items 36 and 37; CUJ 5).

`verify-catalog` exits 0 on a clean catalog, 1 on any finding, naming the
file, and 2 on a catalog it cannot read. `doctor` writes nothing and reports
what support needs: a synced-folder path, a working copy with its start time,
journal entries (empty ones included), a sealed edition no Inventory version
names, and a file a writer staged in ``inventory/`` and left behind.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import attrs
import polars as pl
import pytest
import sdmxlib
import sdmxlib.edition
from sdmxlib import InternationalString, Ref
from sdmxlib.inventory import Current, encode, sha256_hex
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib import diagnostics, testing
from statcatlib.cli import app
from statcatlib.testing import STRUCTURE_PRODUCTS, CodrShape
from tests.conftest import concept_of

pytestmark = pytest.mark.unit

runner = CliRunner()


class Killed(BaseException):
    """The process died here."""


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=InternationalString(en=identifier), description=InternationalString(en="d")
    )


def _observations(wc: sc.WorkingCopy) -> None:
    """Write a dataflow, its structures and one observation, bound: a Parquet file under ``data/``."""
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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A catalog with product ``p`` (datasets and observations) and product ``empty`` (no rows), both live."""
    path = tmp_path / "cat"
    with sc.Catalog.create(path) as cat:
        wc = cat.product("p").working_copy()
        _observations(wc)
        wc.put(_dataset("d1"))
        wc.seal()
        cat.product("empty").working_copy().seal()
    return path


def _edition_dir(root: Path, product: str = "p") -> Path:
    with sc.Catalog.open(root) as cat:
        (edition,) = cat.product(product).editions()
    return root / "products" / product / edition.edition_id


def _verify(root: Path) -> tuple[int, str]:
    result = runner.invoke(app, ["verify-catalog", str(root)])
    return result.exit_code, result.output


# -- verify-catalog ----------------------------------------------------------


def test_a_clean_catalog_exits_0(root: Path) -> None:
    code, output = _verify(root)
    assert code == 0, output
    assert "2 edition(s) checked, 0 problem(s)" in output


def test_a_catalog_with_no_inventory_version_exits_0(tmp_path: Path) -> None:
    sc.Catalog.create(tmp_path / "cat").close()
    code, output = _verify(tmp_path / "cat")
    assert code == 0, output
    assert "nothing to verify" in output


def test_a_corrupted_edition_file_exits_1_and_names_it(root: Path) -> None:
    edition_dir = _edition_dir(root)
    with (edition_dir / "metadata.duckdb").open("ab") as handle:
        handle.write(b"\0" * 16)
    code, output = _verify(root)
    assert code == 1, output
    assert f"FAIL {(edition_dir / 'metadata.duckdb').relative_to(root).as_posix()}:" in output


def test_a_corrupted_data_parquet_file_exits_1_and_names_it(root: Path) -> None:
    (parquet,) = (root / "products" / "p" / "data").rglob("*.parquet")
    with parquet.open("r+b") as handle:
        handle.seek(10)
        handle.write(b"XXXX")
    code, output = _verify(root)
    assert code == 1, output
    assert f"FAIL {parquet.relative_to(root).as_posix()}: contents differ" in output


def test_a_changed_manifest_exits_1_and_names_it(root: Path) -> None:
    manifest = _edition_dir(root) / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    code, output = _verify(root)
    assert code == 1, output
    assert f"FAIL {manifest.relative_to(root).as_posix()}: SHA-256" in output


def test_a_corrupted_leaf_exits_1_and_names_it(root: Path) -> None:
    leaf = next((root / "inventory" / "leaves").iterdir())
    leaf.write_bytes(leaf.read_bytes() + b" ")
    code, output = _verify(root)
    assert code == 1, output
    assert f"FAIL inventory/leaves/{leaf.name}:" in output


def test_a_skipped_root_exits_1_and_names_it(root: Path) -> None:
    """A newer root that does not validate: readers keep the older one, and verify-catalog names the bad one."""
    (root / "inventory" / "000000000003.json").write_text("{}")
    code, output = _verify(root)
    assert code == 1, output
    assert "FAIL inventory/000000000003.json:" in output


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda root: (root / "catalog.json").unlink(), id="no catalog.json"),
        pytest.param(lambda root: (root / "catalog.json").write_text("not json"), id="malformed catalog.json"),
        pytest.param(
            lambda root: [p.write_text("{}") for p in (root / "inventory").glob("*.json")], id="no valid root"
        ),
        pytest.param(lambda root: (root / "meta.duckdb").write_bytes(b""), id="legacy layout"),
    ],
)
def test_an_unreadable_catalog_exits_2(root: Path, damage: object) -> None:
    assert callable(damage)
    damage(root)
    code, output = _verify(root)
    assert code == 2, output
    assert "Cannot read the catalog" in output


def test_a_missing_directory_exits_2(tmp_path: Path) -> None:
    code, output = _verify(tmp_path / "nowhere")
    assert code == 2, output


# -- doctor ------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file and directory under *root*, with its size and mtime."""
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


@pytest.fixture
def broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """CUJ 5: a catalog in a Dropbox folder, left by a crash.

    Product ``p`` has a live edition and a second edition killed after its
    manifest: sealed, journalled and unrecorded. Product ``q`` has a stale
    working copy. Product ``r`` has an empty journal entry, and ``inventory/``
    holds a staged file a writer left.

    Returns:
        The catalog, the unrecorded edition, and the stale working copy.
    """
    root = tmp_path / "Dropbox" / "stats"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy()
        wc.put(_dataset("a"))
        wc.seal()
    real_seal = sdmxlib.edition.seal

    def killed(*args: Any, **kwargs: Any) -> object:
        real_seal(*args, **kwargs)
        raise Killed

    with sc.Catalog.create(root, exist_ok=True) as cat:
        # q's working copy starts first: a later start would repair p's
        # crashed seal, since the journal repair is catalog-wide.
        stale = cat.product("q").working_copy()
        stale_id = stale.edition_id
        stale.close()
        wc = cat.product("p").working_copy()
        wc.put(_dataset("b"))
        unrecorded = wc.edition_id
        with monkeypatch.context() as mp:
            mp.setattr("sdmxlib.edition.seal", killed)
            with pytest.raises(Killed):
                wc.seal()
    journal = root / "products" / "r" / "journal"
    journal.mkdir(parents=True)
    (journal / "01a18e00-0000-7000-8000-000000000042.json").write_bytes(b"")
    (root / "inventory" / ".000000000002.json.123.abc.tmp").write_bytes(b"{")
    return root, unrecorded, stale_id


def test_doctor_writes_nothing(broken: tuple[Path, str, str]) -> None:
    root, _, _ = broken
    before = _snapshot(root)
    runner.invoke(app, ["doctor", str(root)])
    assert _snapshot(root) == before


def test_doctor_reports_cuj_5(broken: tuple[Path, str, str]) -> None:
    root, unrecorded, stale = broken
    result = runner.invoke(app, ["doctor", str(root)])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "WARNING synced folder:" in out
    assert "Dropbox" in out
    assert f"working copy: q: unsealed edition {stale}, started 20" in out
    assert f"journal: products/p/journal/{unrecorded}.json: a seal of {unrecorded} started at" in out
    assert "journal: products/r/journal/01a18e00-0000-7000-8000-000000000042.json is empty" in out
    assert f"unrecorded edition: p: sealed edition {unrecorded} is named by no Inventory version" in out
    assert "leftover file: inventory/.000000000002.json.123.abc.tmp" in out


def test_doctor_reports_the_working_copy_start_time(broken: tuple[Path, str, str]) -> None:
    root, _, stale = broken
    (info,) = [e for e in sdmxlib.edition.list_editions(root / "products" / "q") if e.edition_id == stale]
    (item,) = diagnostics.doctor(root).about("working copy")
    assert info.created_at.isoformat(timespec="seconds") in item.detail
    assert item.level == "info"


def test_doctor_on_a_clean_catalog_exits_0(root: Path) -> None:
    result = runner.invoke(app, ["doctor", str(root)])
    assert result.exit_code == 0, result.output
    assert "ok layout:" in result.output
    assert "ok CURRENT: names version 2, the newest valid version" in result.output
    assert "No warnings." in result.output


def test_doctor_reports_a_lagging_current(root: Path) -> None:
    """Two writers racing can leave ``CURRENT`` one version behind; readers probe forward, so it is no warning."""
    first = (root / "inventory" / "000000000001.json").read_bytes()
    (root / "CURRENT").write_bytes(encode(Current(seq=1, root_sha256=sha256_hex(first), restore_epoch=0)))
    (item,) = diagnostics.doctor(root).about("CURRENT")
    assert item.level == "info"
    assert "names version 1, and probing found version 2" in item.detail


def test_doctor_reports_a_bad_root(root: Path) -> None:
    (root / "inventory" / "000000000003.json").write_text("{}")
    (bad,) = diagnostics.doctor(root).about("inventory")
    assert bad.level == "warning"
    assert "inventory/000000000003.json" in bad.detail


def test_doctor_reports_a_legacy_layout(tmp_path: Path) -> None:
    (tmp_path / "meta.duckdb").write_bytes(b"")
    report = diagnostics.doctor(tmp_path)
    assert report.layout == "legacy"
    assert "0.11" in report.items[0].detail


@pytest.mark.parametrize("make", [False, True], ids=["missing", "empty"])
def test_doctor_reports_a_directory_that_is_no_catalog(tmp_path: Path, *, make: bool) -> None:
    path = tmp_path / "x"
    if make:
        path.mkdir()
    report = diagnostics.doctor(path)
    assert report.layout == "unknown"
    assert report.warnings


def test_doctor_on_a_catalog_with_no_inventory_version(tmp_path: Path) -> None:
    sc.Catalog.create(tmp_path / "cat").close()
    report = diagnostics.doctor(tmp_path / "cat")
    assert report.layout == "catalog"
    assert not report.warnings
    assert "no Inventory version yet" in report.about("inventory")[0].detail


@pytest.mark.parametrize(
    ("parts", "client"),
    [
        (("Dropbox", "stats"), "Dropbox"),
        (("OneDrive - Example", "stats"), "OneDrive"),
        (("Library", "CloudStorage", "GoogleDrive-a@example.com", "My Drive"), "Google Drive"),
        (("Library", "Mobile Documents", "com~apple~CloudDocs", "stats"), "iCloud Drive"),
        (("stats",), None),
    ],
)
def test_synced_folder(tmp_path: Path, parts: tuple[str, ...], client: str | None) -> None:
    assert diagnostics.synced_folder(tmp_path.joinpath(*parts)) == client


# -- structure products and stale data editions (GH-51, S3) --------------------

_SHAPE = CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20), observations=200)
_SEED = 3
_GEO_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_GEO(1.0)"
_UNIT_URN = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_UNIT(1.0)"


class _Ahead:
    """Stands in for `datetime` in `statcatlib.admin`, whose only call is ``datetime.now(UTC)``."""

    def __init__(self, ahead: timedelta) -> None:
        self.ahead = ahead

    def now(self, tz: tzinfo | None = None) -> datetime:
        return datetime.now(tz) + self.ahead


def _seal_structure(
    cat: sc.Catalog,
    product_id: str,
    artefacts: list[sdmxlib.MaintainableArtefact],
    drop: list[sdmxlib.MaintainableArtefact] | None = None,
) -> str:
    """Seal a new edition of structure product *product_id* holding *artefacts*, without *drop*."""
    held = [a for found in testing._structures(_SEED, _SHAPE).by_product().values() for a in found]
    wc = cat.product(product_id).working_copy(kind="structure")
    with testing._scratch([*held, *artefacts]) as scratch:
        wc.fill_structures(scratch, artefacts=[a.urn for a in artefacts])
    for artefact in drop or []:
        wc.registry.drop(artefact.urn)
    return wc.seal().edition_id


@pytest.fixture
def edited(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A generated catalog whose ``structure-geo`` was edited after table ``table-a`` was cut from it.

    Returns:
        The catalog, the stale table edition, the superseded ``structure-geo``
        edition it was cut from, and the live one.
    """
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        table = testing.codr_product(cat, "table-a", seed=_SEED, shape=_SHAPE).edition_id
        cat.refresh()
        (old,) = [e.edition_id for e in cat.product(STRUCTURE_PRODUCTS[0]).editions()]
        structures = testing._structures(_SEED, _SHAPE)
        added = sdmxlib.Code(id="GXTRA", name={"en": "added", "fr": "ajouté"})
        geography = attrs.evolve(structures.geography, codes=[*structures.geography.codes, added])
        new = _seal_structure(cat, STRUCTURE_PRODUCTS[0], [geography, structures.hierarchy])
    return root, table, old, new


def test_verify_lists_a_stale_data_edition_and_finds_nothing(edited: tuple[Path, str, str, str]) -> None:
    root, table, old, new = edited
    report = diagnostics.verify_catalog(root)

    assert report.ok, report.findings
    (stale,) = report.stale
    assert (stale.product_id, stale.edition_id, stale.state, stale.urn) == ("table-a", table, "live", _GEO_URN)
    manifest = sdmxlib.edition.read_manifest(root / "products" / STRUCTURE_PRODUCTS[0] / old)
    assert stale.fingerprint == next(a.fingerprint for a in manifest.artefacts if a.urn == _GEO_URN)
    assert (stale.home, stale.home_live) == (STRUCTURE_PRODUCTS[0], new)

    code, out = _verify(root)
    assert code == 0, out
    assert f"STALE table-a/{table} (live) holds {_GEO_URN}" in out
    assert f"(live edition: {new}); republish the product" in out


def test_doctor_reports_a_stale_data_edition(edited: tuple[Path, str, str, str]) -> None:
    root, table, _, _ = edited
    (item,) = diagnostics.doctor(root).about("stale data editions")
    assert item.level == "warning"
    assert f"table-a/{table} (live)" in item.detail
    assert "republish the product" in item.detail


def test_prune_keeps_the_structure_edition_a_stale_table_was_cut_from(
    edited: tuple[Path, str, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, old, _ = edited
    monkeypatch.setattr("statcatlib.admin.datetime", _Ahead(timedelta(days=30)))

    report = sc.CatalogAdmin.open(root).prune(older_than=timedelta(days=1), reason="reclaim disk", dry_run=False)

    assert f"{STRUCTURE_PRODUCTS[0]}/{old}" not in report.retired
    assert diagnostics.verify_catalog(root).ok


def test_a_structure_reference_only_a_superseded_edition_resolves_is_a_finding(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        testing.codr_structures(cat, seed=_SEED, shape=_SHAPE)
        structures = testing._structures(_SEED, _SHAPE)
        _seal_structure(
            cat, STRUCTURE_PRODUCTS[1], [*structures.classifications, structures.status], drop=[structures.unit]
        )
        cat.refresh()
        (concepts,) = [e.edition_id for e in cat.product(STRUCTURE_PRODUCTS[2]).editions()]

    report = diagnostics.verify_catalog(root)

    assert not report.ok
    assert any(
        f.file == f"products/{STRUCTURE_PRODUCTS[2]}/{concepts}" and _UNIT_URN in f.detail for f in report.findings
    ), report.findings

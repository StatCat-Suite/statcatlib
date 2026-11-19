"""Search index version 3 opens for everything but search (``docs/design/label-lookup.md``, LL-D15).

The catalog under test was sealed by statcatlib 0.20.0 (`tools/make_v3_fixture.py`):
``sealed`` has one live edition with a version 3 index whose rows carry
vectors; ``open`` has a live edition and an unsealed working copy indexed
under 0.20.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from sdmxlib.edition import check_edition, read_manifest

import statcatlib as sc
import statcatlib.query as q
from statcatlib import diagnostics
from statcatlib._lang_sql import spatial_key
from statcatlib.bundle import STATCAT_SECTION, check_catalog
from statcatlib.store.lance import LanceIndex

pytestmark = pytest.mark.unit

pytest.importorskip("lancedb", reason="the search index needs statcatlib[search]")

_ARCHIVE = Path(__file__).parent / "fixtures" / "v3_catalog.tar.xz"
_MODEL_NAME = "stub-encoder"


class _StubEncoder:
    """The fixture's encoder: deterministic unit vectors from the SHA-256 of the text."""

    DIM = 8

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        if isinstance(text, list):
            self.calls += len(text)
            return [self._one(t) for t in text]
        self.calls += 1
        return self._one(text)

    def _one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        v = [b / 255.0 + 0.01 for b in digest[: self.DIM]]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    with tarfile.open(_ARCHIVE) as tar:
        tar.extractall(tmp_path, filter="data")
    return tmp_path / "v3_catalog"


def _edition_dir(root: Path, product: str) -> Path:
    with sc.Catalog.open(root) as cat:
        (edition,) = cat.product(product).editions()
    return root / "products" / product / edition.edition_id


def _index_version(edition_dir: Path) -> int:
    version = json.loads((edition_dir / "search" / "index.json").read_text())["version"]
    assert isinstance(version, int)
    return version


def test_the_fixture_is_version_3(root: Path) -> None:
    edition_dir = _edition_dir(root, "sealed")
    assert _index_version(edition_dir) == 3
    assert read_manifest(edition_dir).extensions[STATCAT_SECTION]["search"]["index_version"] == 3  # pyrefly: ignore[bad-index]


_MODES = ("bm25", "vector", "hybrid")


def _find(scope: str, text: str, mode: str) -> Callable[[sc.EditionView], object]:
    """A call of *scope*'s ``find`` for *text* in *mode*."""

    def call(view: sc.EditionView) -> object:
        return getattr(view, scope).find(text, lang="en", mode=mode)

    return call


_SEARCHES: dict[str, Callable[[sc.EditionView], object]] = {
    **{
        f"{scope}.find {mode}": _find(scope, text, mode)
        for scope, text in (
            ("datasets", "rainfall"),
            ("codes", "Quebec"),
            ("concepts", "Rainfall"),
            ("reference_metadata", "station"),
        )
        for mode in _MODES
    },
    "search": lambda v: v.search("rainfall", lang="en"),
    "QueryBuilder.search": lambda v: v.query(sc.OpenDataset).search("rainfall", lang="en").collect(),
    "QueryBuilder.hybrid_search": lambda v: v.query(sc.OpenDataset).hybrid_search("rainfall", lang="en").collect(),
    "filtered search matching no row": lambda v: (
        v.query(sc.OpenDataset).filter(q.title_contains("no such title", lang="en")).search("x", lang="en").collect()
    ),
}


@pytest.mark.parametrize("call", list(_SEARCHES), ids=list(_SEARCHES))
def test_every_search_entry_point_refuses_version_3(root: Path, call: str) -> None:
    with sc.Catalog.open(root) as cat:
        view = cat.product("sealed").view(embedding_model=_StubEncoder())
        with pytest.raises(sc.IncompatibleCatalogError, match=r"format 3.*build_index"):
            _SEARCHES[call](view)


def test_table_for_refuses_version_3(root: Path) -> None:
    index = LanceIndex(_edition_dir(root, "sealed") / "search")
    with pytest.raises(sc.IncompatibleCatalogError):
        index.table_for("en")
    assert set(index.describe()) == {"en", "fr", "en_t_fr"}
    assert index.table_for_key("en") is not None


def test_a_view_opens_and_answers_everything_but_search(root: Path) -> None:
    with sc.Catalog.open(root) as cat:
        view = cat.product("sealed").view()
        assert sorted(d.identifier for d in view.query(sc.BaseDataset).collect()) == ["V3:DF_RAIN(1.0)", "rain"]
        titles = view.query(sc.OpenDataset).select("title", lang=("en", "en-t-fr")).rows()
        assert titles == [{"title": "Rainfall totals", "title__lang": "en-t-fr"}]
        assert view.index_layout
        # Labels and facets.
        assert view.agent_labels(lang="fr")["STC"] == sc.LocalisedString("Statistique Canada", "fr")
        spatial = view.facets("spatial")["spatial"]
        assert set(spatial) == {"https://example.org/region/24", spatial_key({"en": "Ontario", "fr": "Ontario"})}
        labels = view.spatial_labels(lang=("en", "fr"))
        assert labels["https://example.org/region/24"] == sc.LocalisedString("Québec", "fr")
        assert dict(view.code_map("V3:DF_RAIN(1.0)", lang="fr")["GEO"]) == {
            "ON": sc.LocalisedString("Ontario", "fr"),
            "QC": sc.LocalisedString("Québec", "fr"),
        }
        geo = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=V3:CL_GEO(1.0)"
        assert view.codelist(geo).batch_labels(["QC"], lang="en-t-fr")["QC"] == sc.LocalisedString(
            "Quebec province", "en-t-fr"
        )
        assert view.query(sc.OpenDataset).filter(q.has_spatial("Québec", lang="fr")).count() == 1


def test_the_embargo_reader_opens_version_3(root: Path) -> None:
    reader = sc.EmbargoReader.open(root)
    try:
        (entry,) = [e for e in reader.editions(states=("live",)) if e.product_id == "sealed"]
        view = reader.view("sealed", entry.edition_id)
        assert [d.identifier for d in view.query(sc.OpenDataset).collect()] == ["rain"]
        assert view.agent_labels(lang="en")["STC"] == sc.LocalisedString("Statistics Canada", "en")
    finally:
        reader.close()


def test_the_receive_check_accepts_version_3(root: Path) -> None:
    """The release fold and upload both run `check_edition` at ``receive`` with `check_catalog`."""
    report = check_edition(_edition_dir(root, "sealed"), at="receive", extensions={STATCAT_SECTION: check_catalog})
    assert report.ok, report.findings


def test_verify_catalog_accepts_version_3(root: Path) -> None:
    report = diagnostics.verify_catalog(root)
    assert report.ok, report.findings


def test_a_working_copy_over_version_3_reuses_its_vectors_and_seals_version_4(root: Path) -> None:
    """A re-index carries every dataset, code and concept vector and moves ``en-t-fr`` rows into ``en``."""
    model = _StubEncoder()
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("sealed").working_copy(embedding_model=model)
        wc.add_language_alternates({"en": ["en-t-fr"]}, source="test")
        wc.build_index(embedding_model=model, model_name=_MODEL_NAME, embed_codes=True, include_reference_metadata=True)
        # Every row 0.20 indexed reuses its vector. The one row 0.20 did not
        # write is encoded: QC's ``en-t-fr`` code label, which 0.20 skipped
        # because the dataflow has no ``en-t-fr`` title.
        assert model.calls == 1
        view = wc.view()
        assert set(view.index_layout) == {"en", "fr"}
        [hit] = view.datasets.find("totals", lang="en").items
        assert hit.lang == "en-t-fr"
        codes = view.codes.find("province", lang="en").items
        assert sc.LocalisedString("Quebec province", "en-t-fr") in {h.payload.code_label for h in codes}
        concepts = view.concepts.find("amount", lang="en", mode="vector").items
        assert concepts
        assert any(h.lang == "en-t-fr" for h in view.concepts.find("amount", lang="en").items)
        edition = wc.seal()
    edition_dir = root / "products" / "sealed" / edition.edition_id
    assert _index_version(edition_dir) == 4
    manifest = json.loads((edition_dir / "search" / "index.json").read_text())
    assert manifest["language_pairs"] == {"en-t-fr": "en"}
    assert read_manifest(edition_dir).extensions[STATCAT_SECTION]["search"]["index_version"] == 4  # pyrefly: ignore[bad-index]


def test_a_resumed_working_copy_indexed_under_0_20_seals_version_4(root: Path) -> None:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product("open").working_copy(resume=True, embedding_model=_StubEncoder())
        edition = wc.seal()
    assert _index_version(root / "products" / "open" / edition.edition_id) == 4


def test_a_rebuild_with_no_rows_names_no_version_3_table(root: Path, tmp_path: Path) -> None:
    edition_dir = _edition_dir(root, "sealed")
    search = tmp_path / "search"
    search.mkdir()
    shutil.copytree(edition_dir / "search", search, dirs_exist_ok=True)
    index = LanceIndex(search, edition_id="01a1f0c6-0000-7000-8000-000000000009")
    index.add_bulk([], pairs={})
    manifest = json.loads((search / "index.json").read_text())
    assert manifest["version"] == 4
    assert manifest["languages"] == {}

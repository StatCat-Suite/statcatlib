"""The edition view's read path: one engine, ``USE`` per cursor, nothing written (RFC §10).

A catalog attaches every edition it views into one DuckDB engine. Each view's
cursor runs ``USE "<catalog>".statcat``, so unqualified names, two-part names
(``statcat.dataset``, ``sdmx.code``) and every query built on them resolve
inside that view's own edition, even with another edition attached beside it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sdmxlib as sl
from sdmxlib import InternationalString
from sdmxlib.model.in_memory_registry import InMemoryRegistry

import statcatlib as sc
from statcatlib.store import lance
from statcatlib.store.store import SEARCH_ANNOTATION_TYPES, IncompatibleCatalogError, annotation_fingerprint
from tests.conftest import concept_of

pytestmark = pytest.mark.unit

_CODELIST = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
_DATASET = "TEST:DF_GEO(1.0)"


def _registry(label: str, *, excluded: bool) -> InMemoryRegistry:
    """One dataflow over codelist CL_GEO, whose code ``X`` is called *label*."""
    annotations = [sl.Annotation(type=sc.SEARCH_EXCLUDE_ANNOTATION_TYPE, title="x")] if excluded else []
    cl = sl.Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geography"),
        codes=[sl.Code(id="X", name=InternationalString(en=label), annotations=annotations)],
    )
    dsd = sl.DataStructure(
        id="DSD_GEO",
        maintainer="TEST",
        name=InternationalString(en="DSD"),
        dimensions=[sl.Dimension(id="GEO", concept=concept_of("GEO"), representation=cl)],
    )
    flow = sl.Dataflow(id="DF_GEO", maintainer="TEST", name=InternationalString(en=f"Flow {label}"), structure=dsd)
    registry = InMemoryRegistry()
    for artefact in (cl, dsd, flow):
        registry.add(artefact)
    return registry


@pytest.fixture
def two(tmp_path: Path) -> Iterator[tuple[sc.EditionView, sc.EditionView]]:
    """Views of two products' editions, in one catalog and so one engine: the same URNs, other labels."""
    root = tmp_path / "cat"
    with sc.Catalog.create(root) as cat:
        for product, label, excluded in (("north", "Nord", False), ("south", "Sud", True)):
            wc = cat.product(product).working_copy()
            sc.ingest_sdmx(wc, _registry(label, excluded=excluded))
            wc.put(
                sc.OpenDataset(
                    identifier=f"only-{product}",
                    title=InternationalString(en=product),
                    description=InternationalString(en=product),
                )
            )
            wc.seal()
    with sc.Catalog.open(root) as cat:
        yield cat.product("north").view(), cat.product("south").view()


def _one(view: sc.EditionView, sql: str) -> list[tuple[object, ...]]:
    return view._cursor().execute(sql).fetchall()


class TestNameResolution:
    def test_both_editions_share_one_engine(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        north, south = two
        assert north.registry.catalog_name != south.registry.catalog_name
        assert north._store._registry._engine is south._store._registry._engine

    def test_statcat_names_resolve_to_each_edition(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        for view, product in zip(two, ("north", "south"), strict=True):
            expected = [(_DATASET,), (f"only-{product}",)]
            assert _one(view, "SELECT identifier FROM statcat.dataset ORDER BY 1") == expected
            assert _one(view, "SELECT identifier FROM dataset ORDER BY 1") == expected

    def test_sdmx_names_resolve_to_each_edition(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        north, south = two
        assert _one(north, "SELECT name['en'] FROM sdmx.code") == [("Nord",)]
        assert _one(south, "SELECT name['en'] FROM sdmx.code") == [("Sud",)]

    def test_a_scope_resolves_to_each_edition(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        north, south = two
        assert north.codelists[_CODELIST].label("en") == "Geography"
        assert north.codelists[_CODELIST].batch_labels(["X"], lang="en") == {"X": "Nord"}
        assert south.codelists[_CODELIST].batch_labels(["X"], lang="en") == {"X": "Sud"}
        assert north.codelists.batch_labels([(_CODELIST, "X")], lang="en") == {(_CODELIST, "X"): "Nord"}

    def test_code_map_resolves_to_each_edition(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        north, south = two
        assert north.code_map(_DATASET) == {"GEO": {"X": "Nord"}}
        assert south.code_map(_DATASET) == {"GEO": {"X": "Sud"}}

    def test_annotation_fingerprint_resolves_to_each_edition(self, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        north, south = two
        assert annotation_fingerprint(north._cursor(), SEARCH_ANNOTATION_TYPES) != annotation_fingerprint(
            south._cursor(), SEARCH_ANNOTATION_TYPES
        )
        recorded = south.index_annotations
        assert recorded is not None
        assert annotation_fingerprint(south._cursor(), recorded["types"]) == recorded["fingerprint"]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


class TestReadPath:
    def test_open_writes_no_bytes(self, tmp_path: Path, two: tuple[sc.EditionView, sc.EditionView]) -> None:
        """Opening, viewing, querying and searching leave every file as it was."""
        del two
        root = tmp_path / "cat"
        before = _snapshot(root)
        with sc.Catalog.open(root) as cat:
            cat.refresh()
            for product in cat.products():
                view = cat.product(product).view()
                view.query(sc.BaseDataset).collect()
                view.facets("publisher")
                view.code_map(_DATASET)
                if view.index_layout:
                    view.search("north")
                cat.product(product).editions()
        assert _snapshot(root) == before

    def test_an_edition_with_no_search_rows_answers_with_no_hits(self, catalog: sc.Catalog) -> None:
        pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
        catalog.product("empty").working_copy().seal()
        catalog.refresh()
        view = catalog.product("empty").view()
        assert view.index_layout == {}
        assert view.search("anything").items == []

    def test_a_changed_manifest_is_refused(self, tmp_path: Path, catalog: sc.Catalog) -> None:
        edition = catalog.product("p").working_copy().seal()
        manifest = tmp_path / "catalog" / "products" / "p" / edition.edition_id / "manifest.json"
        document = json.loads(manifest.read_text())
        document["producer_fields"] = {"tampered": True}
        manifest.write_text(json.dumps(document))
        catalog.refresh()
        with pytest.raises(IncompatibleCatalogError, match=rf"{edition.edition_id}.*statcat verify-catalog"):
            catalog.product("p").view()

    def test_editions_returns_previous(self, catalog: sc.Catalog) -> None:
        first = catalog.product("p").working_copy().seal()
        second = catalog.product("p").working_copy().seal()
        catalog.refresh()
        assert catalog.product("p").editions() == [second]
        assert second.previous == first.edition_id


class TestSearchAcrossEditions:
    def test_the_old_view_still_searches_after_a_new_seal(self, catalog: sc.Catalog) -> None:
        """GH-47: B's seal drops its carried copy of A's tables, never A's own."""
        pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
        wc = catalog.product("p").working_copy()
        wc.put(
            sc.OpenDataset(
                identifier="a", title=InternationalString(en="Aardvark"), description=InternationalString(en="A")
            )
        )
        a = wc.seal()
        catalog.refresh()
        old = catalog.product("p").view()
        wc = catalog.product("p").working_copy()
        wc.put(
            sc.OpenDataset(
                identifier="b", title=InternationalString(en="Badger"), description=InternationalString(en="B")
            )
        )
        b = wc.seal()
        assert [d.identifier for d in old.query(sc.BaseDataset).search("aardvark").collect()] == ["a"]
        assert old.query(sc.BaseDataset).search("badger").collect() == []
        catalog.refresh()
        new = catalog.product("p").view()
        found = new.query(sc.BaseDataset).search("aardvark badger").collect()
        assert {d.identifier for d in found} == {"a", "b"}
        old_tables = {layout["table"] for layout in old.index_layout.values()}
        new_tables = {layout["table"] for layout in new.index_layout.values()}
        assert all(t.endswith(a.edition_id.replace("-", "")) for t in old_tables)
        assert all(t.endswith(b.edition_id.replace("-", "")) for t in new_tables)

    def test_table_names_carry_the_language_alias_and_the_edition(self, catalog: sc.Catalog) -> None:
        pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
        title = InternationalString(**{"en": "Rain", "fr": "Pluie", "fr-CA": "Pluie", "zh-Hant": "雨"})
        wc = catalog.product("p").working_copy()
        wc.put(sc.OpenDataset(identifier="r", title=title, description=title))
        edition = wc.seal()
        catalog.refresh()
        suffix = edition.edition_id.replace("-", "")
        layout = catalog.product("p").view().index_layout
        assert {lang: entry["table"] for lang, entry in layout.items()} == {
            "en": f"search_en__{suffix}",
            "fr": f"search_fr__{suffix}",
            "fr_ca": f"search_fr_ca__{suffix}",
            "zh_hant": f"search_zh_hant__{suffix}",
        }
        assert lance._table_name("fr-CA", edition.edition_id) == f"search_fr_ca__{suffix}"

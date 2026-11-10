"""statcatlib's part of sdmxlib's edition contract: the ``statcat`` section and its checks (GH-3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import msgspec
import pytest
import sdmxlib as sl
from sdmxlib.edition import (
    OVERLAY_FORMAT,
    Manifest,
    Overlay,
    OverlayAnnotation,
    OverlayEntry,
    check_edition,
    read_manifest,
)
from sdmxlib.testing import Dim, Shape, synthetic_edition

import statcatlib as sc
from statcatlib.bundle import STATCAT_SECTION, STATCAT_SECTION_FORMAT, check_catalog, overlay_affects_search
from statcatlib.store.store import annotation_fingerprint
from statcatlib.testing import synthetic_catalog
from tests.conftest import GEO, GEO_HIERARCHY, structure_source

pytestmark = pytest.mark.unit

_SHAPE = Shape(dimensions=(Dim(codes=30, depth=2), Dim(codes=4)), observations=300, languages=("en", "fr"))


def _edition_dir(root: Path, edition: sc.Edition) -> Path:
    return root / "products" / edition.product_id / edition.edition_id


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A sealed synthetic edition, with observations and a search index; its directory."""
    pytest.importorskip("lancedb", reason="the seal builds the index with statcatlib[search]")
    root = tmp_path_factory.mktemp("synthetic") / "catalog"
    return _edition_dir(root, synthetic_catalog(root, _SHAPE, seed=3))


@pytest.fixture(scope="module")
def no_observations(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A sealed edition holding catalog datasets and no observations; its directory."""
    root = tmp_path_factory.mktemp("datasets") / "catalog"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy()
        wc.put(
            sc.OpenDataset(
                identifier="d", title=sc.InternationalString(en="D"), description=sc.InternationalString(en="D")
            )
        )
        edition = wc.seal()
    return _edition_dir(root, edition)


_PARTIAL = sl.Codelist(
    id="CL_PART",
    maintainer="T",
    version="1.0",
    name={"en": "part"},
    is_partial=True,
    codes=[sl.Code(id="A", name={"en": "A"})],
)


@pytest.fixture(scope="module")
def structure_edition(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A sealed structure product's edition, holding a codelist and its hierarchy; its directory."""
    tmp = tmp_path_factory.mktemp("structure")
    root = tmp / "catalog"
    with structure_source(tmp / "source") as source, sc.Catalog.create(root) as cat:
        wc = cat.product("structure-geo").working_copy(kind="structure")
        wc.fill_structures(source, artefacts=[GEO.urn, GEO_HIERARCHY.urn])
        edition = wc.seal()
    return _edition_dir(root, edition)


@pytest.fixture(scope="module")
def partial_edition(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A sealed data edition holding one partial codelist and no dataflow; its directory."""
    root = tmp_path_factory.mktemp("partial") / "catalog"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy()
        wc.registry.add(_PARTIAL)
        edition = wc.seal()
    return _edition_dir(root, edition)


def _check(edition_dir: Path, manifest: Manifest) -> list[str]:
    """Run `check_catalog` against *edition_dir* as if *manifest* were its manifest."""
    with sl.LocalRegistry.edition(edition_dir) as reg:
        return [str(f) for f in check_catalog(edition_dir, manifest, reg, "open")]


def _with_section(manifest: Manifest, **changes: object) -> Manifest:
    section = {**manifest.extensions[STATCAT_SECTION], **changes}
    return msgspec.structs.replace(manifest, extensions={**manifest.extensions, STATCAT_SECTION: section})


class TestCheckCatalog:
    def test_a_sealed_synthetic_edition_passes_every_check(self, synthetic: Path) -> None:
        report = check_edition(synthetic, at="publish", extensions={STATCAT_SECTION: check_catalog})

        assert report.ok, [str(f) for f in report.findings]
        assert report.checked[-1] == STATCAT_SECTION
        section = read_manifest(synthetic).extensions[STATCAT_SECTION]
        assert section["datasets"] == 1
        search = section["search"]
        assert isinstance(search, dict)
        assert set(search["languages"]) == {"en", "fr"}

    def test_the_section_is_format_3_of_a_data_product(self, synthetic: Path) -> None:
        section = read_manifest(synthetic).extensions[STATCAT_SECTION]
        assert section["format"] == STATCAT_SECTION_FORMAT == 3
        assert section["kind"] == "data"

    def test_the_section_records_the_search_annotation_types(self, synthetic: Path) -> None:
        """GH-48: a server reads the types from the section; the default build reads `SEARCH_ANNOTATION_TYPES`."""
        search = read_manifest(synthetic).extensions[STATCAT_SECTION]["search"]
        assert isinstance(search, dict)
        annotations = search["annotations"]
        assert isinstance(annotations, dict)
        assert sorted(annotations["types"]) == sorted(sc.SEARCH_ANNOTATION_TYPES)

    def test_another_format_is_refused(self, no_observations: Path) -> None:
        manifest = _with_section(read_manifest(no_observations), format=2)
        assert _check(no_observations, manifest) == [
            f"[{STATCAT_SECTION}] manifest.json: statcat section format 2, this build reads 3"
        ]

    def test_an_unknown_kind_is_refused(self, no_observations: Path) -> None:
        manifest = _with_section(read_manifest(no_observations), kind="dataset")
        (finding,) = _check(no_observations, manifest)
        assert "kind 'dataset'" in finding

    def test_the_section_records_the_products_lifecycle(self, synthetic: Path, structure_edition: Path) -> None:
        """Phase 5 D3: the fold records a new product with the section's lifecycle."""
        assert read_manifest(synthetic).extensions[STATCAT_SECTION]["lifecycle"] == "revisable"
        assert read_manifest(structure_edition).extensions[STATCAT_SECTION]["lifecycle"] == "revisable"

    def test_a_final_products_section_records_final(self, tmp_path: Path) -> None:
        root = tmp_path / "catalog"
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy(lifecycle="final")
            wc.put(
                sc.OpenDataset(
                    identifier="d", title=sc.InternationalString(en="D"), description=sc.InternationalString()
                )
            )
            edition = wc.seal()
        assert read_manifest(_edition_dir(root, edition)).extensions[STATCAT_SECTION]["lifecycle"] == "final"

    def test_another_lifecycle_is_refused(self, no_observations: Path) -> None:
        manifest = _with_section(read_manifest(no_observations), lifecycle="live")
        assert _check(no_observations, manifest) == [
            f"[{STATCAT_SECTION}] manifest.json: statcat section lifecycle 'live', not one of final, revisable"
        ]

    def test_a_section_with_no_lifecycle_is_accepted(self, no_observations: Path) -> None:
        """An edition sealed before statcatlib 0.17 has none; the section format stays 3."""
        section = dict(read_manifest(no_observations).extensions[STATCAT_SECTION])
        del section["lifecycle"]
        manifest = read_manifest(no_observations)
        manifest = msgspec.structs.replace(manifest, extensions={**manifest.extensions, STATCAT_SECTION: section})
        assert _check(no_observations, manifest) == []

    def test_a_structure_kind_without_bindings_is_accepted(self, no_observations: Path) -> None:
        """Format 3 reserves ``structure`` for structure products (Phase 3)."""
        manifest = read_manifest(no_observations)
        assert manifest.bindings == ()
        assert _check(no_observations, _with_section(manifest, kind="structure")) == []

    def test_a_structure_kind_with_bindings_is_refused(self, synthetic: Path) -> None:
        manifest = read_manifest(synthetic)
        assert manifest.bindings
        findings = _check(synthetic, _with_section(manifest, kind="structure"))
        bound = [
            f"[{STATCAT_SECTION}] {b.dataflow}: a structure product's edition binds a dataflow to observations"
            for b in manifest.bindings
        ]
        assert findings[: len(bound)] == bound
        held = "which a structure product does not hold: it holds full artefacts only"
        assert [f.split(": ", 1)[1] for f in findings[len(bound) :]] == [
            f"a DataflowBinding, {held}",
            f"a Dataflow, {held}",
        ], "by URN: the binding's package sorts first"

    def test_a_sealed_structure_edition_passes_every_check(self, structure_edition: Path) -> None:
        assert read_manifest(structure_edition).extensions[STATCAT_SECTION]["kind"] == "structure"
        report = check_edition(structure_edition, at="publish", extensions={STATCAT_SECTION: check_catalog})

        assert report.ok, [str(f) for f in report.findings]

    def test_a_structure_kind_holding_a_partial_is_refused(self, partial_edition: Path) -> None:
        manifest = _with_section(read_manifest(partial_edition), kind="structure")

        detail = "a partial Codelist, which a structure product does not hold: it holds full artefacts only"
        assert _check(partial_edition, manifest) == [f"[{STATCAT_SECTION}] {_PARTIAL.urn}: {detail}"]


class TestAnnotationFingerprint:
    @pytest.fixture
    def conn(self, tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        reg = synthetic_edition(tmp_path / "product", _SHAPE, seed=3, seal=False)
        yield reg.connection
        reg.close()

    def _add(self, conn: duckdb.DuckDBPyConnection, annotation_type: str, title: str | None = "t") -> int:
        row = conn.execute(
            """
            INSERT INTO sdmx.annotation
                (id, owner_urn, owner_artefact_urn, ordinal, annotation_id, title, annotation_type, value)
            SELECT coalesce(max(id), 0) + 1, 'urn:x', 'urn:x', 0, NULL, $title, $type, NULL FROM sdmx.annotation
            RETURNING id
            """,
            {"title": title, "type": annotation_type},
        ).fetchone()
        assert row is not None
        return int(row[0])

    def test_changes_with_an_annotation_of_a_covered_type(self, conn: duckdb.DuckDBPyConnection) -> None:
        before = annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES)
        self._add(conn, sc.SEARCH_EXCLUDE_ANNOTATION_TYPE)
        assert annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES) != before

    def test_ignores_other_types(self, conn: duckdb.DuckDBPyConnection) -> None:
        before = annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES)
        self._add(conn, "LAYOUT_ROW")
        assert annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES) == before

    def test_changes_with_localised_text(self, conn: duckdb.DuckDBPyConnection) -> None:
        ident = self._add(conn, "skos:altLabel")
        conn.execute("""INSERT INTO sdmx.annotation_text VALUES ($id, 'en', 'one')""", {"id": ident})
        before = annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES)
        conn.execute("""UPDATE sdmx.annotation_text SET text = 'two' WHERE annotation_id = $id""", {"id": ident})
        assert annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES) != before

    def test_a_null_title_is_not_an_empty_one(self, conn: duckdb.DuckDBPyConnection) -> None:
        ident = self._add(conn, sc.SEARCH_EXCLUDE_ANNOTATION_TYPE, title=None)
        with_null = annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES)
        conn.execute("""UPDATE sdmx.annotation SET title = '' WHERE id = $id""", {"id": ident})
        assert annotation_fingerprint(conn, sc.SEARCH_ANNOTATION_TYPES) != with_null

    def test_no_types_is_the_digest_of_nothing(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._add(conn, sc.SEARCH_EXCLUDE_ANNOTATION_TYPE)
        assert annotation_fingerprint(conn, []) == annotation_fingerprint(duckdb.connect(), sc.SEARCH_ANNOTATION_TYPES)


def _overlay(annotation_type: str, selector: str = "Code") -> Overlay:
    return Overlay(
        format_version=OVERLAY_FORMAT,
        id=f"set-{annotation_type}",
        entries=(
            OverlayEntry(
                selector=selector,
                op="set",
                type=annotation_type,
                annotation=OverlayAnnotation(title="from an overlay"),
            ),
        ),
    )


class TestOverlayAffectsSearch:
    def test_names_the_entries_on_index_annotation_types(self) -> None:
        overlay = Overlay(
            format_version=OVERLAY_FORMAT,
            id="mixed",
            entries=(
                OverlayEntry(selector="*", op="remove", type="LAYOUT_ROW"),
                OverlayEntry(selector="Code", op="remove", type=sc.SEARCH_EXCLUDE_ANNOTATION_TYPE),
                OverlayEntry(selector="Code", op="remove", type="skos:altLabel"),
            ),
        )
        affecting = overlay_affects_search(overlay)
        assert [e.type for e in affecting] == [sc.SEARCH_EXCLUDE_ANNOTATION_TYPE, "skos:altLabel"]

    def test_display_only_overlay_is_falsy(self) -> None:
        assert not overlay_affects_search(_overlay("LAYOUT_ROW"))

    def test_types_of_an_index_built_otherwise(self) -> None:
        assert overlay_affects_search(_overlay("CUSTOM_CONTEXT"), types=["CUSTOM_CONTEXT"])
        assert not overlay_affects_search(_overlay(sc.SEARCH_EXCLUDE_ANNOTATION_TYPE), types=["CUSTOM_CONTEXT"])

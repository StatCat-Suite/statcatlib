"""statcatlib's part of sdmxlib's edition contract: the ``statcat`` section and its checks (GH-3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from sdmxlib.edition import (
    OVERLAY_FORMAT,
    Overlay,
    OverlayAnnotation,
    OverlayEntry,
    Producer,
    check_edition,
    seal,
)
from sdmxlib.testing import Dim, Shape

import statcatlib as sc
from statcatlib.bundle import STATCAT_SECTION, catalog_section, check_catalog, overlay_affects_search
from statcatlib.store.store import annotation_fingerprint
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_SHAPE = Shape(dimensions=(Dim(codes=30, depth=2), Dim(codes=4)), observations=300, languages=("en", "fr"))
_PRODUCER = Producer(name="tests", version="0")


class TestCheckCatalog:
    def test_a_sealed_synthetic_catalog_passes_every_check(self, tmp_path: Path) -> None:
        rec = synthetic_catalog(tmp_path / "product", _SHAPE, seed=3)
        section = catalog_section(rec.cat)
        rec.cat.close()
        rec.reg.close()
        seal(rec.edition_dir, product_id="synthetic", producer=_PRODUCER, extensions={STATCAT_SECTION: section})

        report = check_edition(rec.edition_dir, at="publish", extensions={STATCAT_SECTION: check_catalog})

        assert report.ok, [str(f) for f in report.findings]
        assert report.checked[-1] == STATCAT_SECTION
        assert section["datasets"] == 1
        search = section["search"]
        assert isinstance(search, dict)
        assert set(search["languages"]) == {"en", "fr"}


class TestAnnotationFingerprint:
    @pytest.fixture
    def conn(self, tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        rec = synthetic_catalog(tmp_path / "product", _SHAPE, seed=3, index=False)
        yield rec.reg.connection
        rec.cat.close()
        rec.reg.close()

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

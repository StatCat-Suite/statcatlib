"""statcatlib's part of sdmxlib's bundle contract: the ``statcat`` section and its checks (GH-3)."""

from __future__ import annotations

import json
import shutil
import warnings
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from sdmxlib.bundle import (
    MANIFEST_NAME,
    OVERLAY_FORMAT,
    Overlay,
    OverlayAnnotation,
    OverlayEntry,
    Producer,
    fold_overlays,
    write_manifest,
)
from sdmxlib.testing import Dim, Shape, synthetic_bundle

import statcatlib as sc
from statcatlib.bundle import STATCAT_SECTION, Embedded, check_bundle, overlay_affects_search, publish_bundle
from statcatlib.store.store import annotation_fingerprint
from statcatlib.testing import synthetic_catalog

pytestmark = pytest.mark.unit

_SHAPE = Shape(dimensions=(Dim(codes=30, depth=2), Dim(codes=4)), observations=300, languages=("en", "fr"))
_PRODUCER = Producer(name="tests", version="0")


@pytest.fixture(scope="module")
def _published(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("published") / "bundle"
    synthetic_catalog(path, _SHAPE, seed=3)
    return path


@pytest.fixture
def bundle(_published: Path, tmp_path: Path) -> Path:
    """A published statcat bundle of its own, safe to change."""
    return Path(shutil.copytree(_published, tmp_path / "bundle"))


def _findings(path: Path, **kwargs: bool) -> list[str]:
    return [str(f) for f in check_bundle(path, references=False, **kwargs).findings]


def _edit_section(path: Path, change: dict[str, object]) -> None:
    """Rewrite the manifest's statcat section in place. The manifest is not in its own inventory."""
    manifest = path / MANIFEST_NAME
    data = json.loads(manifest.read_text())
    data["extensions"][STATCAT_SECTION].update(change)
    manifest.write_text(json.dumps(data))


class TestPublish:
    def test_synthetic_catalog_passes_every_check(self, bundle: Path) -> None:
        report = check_bundle(bundle)
        assert report.ok, [str(f) for f in report.findings]
        assert report.checked[-1] == STATCAT_SECTION
        assert "references" in report.checked

    def test_section_describes_the_catalog_and_its_index(self, bundle: Path) -> None:
        section = json.loads((bundle / MANIFEST_NAME).read_text())["extensions"][STATCAT_SECTION]
        assert section["format"] == 2
        assert section["datasets"] == 1
        search = section["search"]
        assert set(search["languages"]) == {"en", "fr"}
        assert search["languages"]["fr"]["stemmer"] == "French"
        assert search["languages"]["en"]["rows"] > 0
        assert search["embedding_model"] is None

    def test_republishing_mints_a_new_token(self, bundle: Path) -> None:
        before = json.loads((bundle / MANIFEST_NAME).read_text())["token"]
        after = publish_bundle(bundle, bundle_id="synthetic", producer=_PRODUCER)
        assert after.token != before
        assert check_bundle(bundle, references=False).ok

    def test_refuses_a_statcat_section_from_the_caller(self, bundle: Path) -> None:
        with pytest.raises(ValueError, match="written by publish_bundle"):
            publish_bundle(bundle, bundle_id="x", producer=_PRODUCER, extensions={STATCAT_SECTION: {}})

    def test_other_extensions_are_kept(self, bundle: Path) -> None:
        manifest = publish_bundle(bundle, bundle_id="x", producer=_PRODUCER, extensions={"other": {"k": 1}})
        assert manifest.extensions["other"] == {"k": 1}
        assert STATCAT_SECTION in manifest.extensions


class TestServingLeavesTheBundleIntact:
    """Opening and searching a published bundle must not change a file the manifest lists."""

    def test_read_only_search(self, bundle: Path) -> None:
        with sc.open_bundle(bundle, access_mode="read_only") as cat:
            assert cat.search("synthetic").items
        assert _findings(bundle) == []

    def test_read_write_open_and_search(self, bundle: Path) -> None:
        with sc.open_bundle(bundle) as cat:
            assert cat.search("synthetic").items
        assert _findings(bundle) == []


class TestCatalogChecks:
    def test_dataset_count_and_index_rows_are_checked(self, bundle: Path) -> None:
        _edit_section(bundle, {"datasets": 5})
        manifest = json.loads((bundle / MANIFEST_NAME).read_text())
        manifest["extensions"][STATCAT_SECTION]["search"]["languages"]["en"]["rows"] = 1
        manifest["extensions"][STATCAT_SECTION]["search"]["languages"]["fr"]["stemmer"] = None
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest))

        found = _findings(bundle)
        assert "[statcat] statcat.dataset: 1 datasets, the manifest says 5" in found
        assert any(f.startswith("[statcat] search [en]: ") and "the manifest says 1" in f for f in found)
        assert "[statcat] search [fr]: stemmed with French, the manifest says None" in found

    def test_language_declared_but_not_indexed(self, bundle: Path) -> None:
        manifest = json.loads((bundle / MANIFEST_NAME).read_text())
        manifest["extensions"][STATCAT_SECTION]["search"]["languages"]["de"] = {"rows": 3, "stemmer": "German"}
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest))
        assert _findings(bundle) == ["[statcat] search [de]: declared in the manifest, absent from the index"]

    def test_undeclared_search_index(self, tmp_path: Path) -> None:
        path = tmp_path / "bundle"
        synthetic_catalog(path, _SHAPE, index=False)
        with sc.Catalog(path, data_path=Embedded()) as cat:
            cat.build_index()
        found = _findings(path)
        assert "[statcat] search: a search index the manifest does not declare" in found

    def test_dataset_linking_a_flow_the_registry_lacks(self, bundle: Path) -> None:
        missing = "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=NOPE:DF_NOPE(1.0)"
        with sc.Catalog(bundle, data_path=Embedded()) as cat:
            cat.put(
                sc.AggregateDataset(
                    identifier="NOPE:DF_NOPE(1.0)",
                    title=sc.InternationalString(en="Dangling"),
                    description=sc.InternationalString(en="Links a flow that is not stored"),
                    sdmx_dataflow_urn=missing,
                )
            )
            cat.build_index()
        publish_bundle(bundle, bundle_id="synthetic", producer=_PRODUCER)
        assert _findings(bundle) == [f"[statcat] NOPE:DF_NOPE(1.0): links {missing}, which the registry does not hold"]

    def test_manifest_without_a_statcat_section(self, bundle: Path) -> None:
        write_manifest(bundle, bundle_id="synthetic", producer=_PRODUCER)
        assert _findings(bundle) == [
            f"[statcat] {MANIFEST_NAME}: no statcat section: publish with statcatlib's publish_bundle"
        ]

    def test_unknown_section_format(self, bundle: Path) -> None:
        _edit_section(bundle, {"format": 99})
        assert _findings(bundle) == [f"[statcat] {MANIFEST_NAME}: statcat section format 99, this build reads 2"]

    def test_plain_sdmxlib_bundle_is_not_a_catalog(self, tmp_path: Path) -> None:
        path = tmp_path / "bundle"
        synthetic_bundle(path, _SHAPE)
        assert _findings(path) == ["[statcat] meta.duckdb: no statcat schema: not a statcat catalog"]


class TestCLI:
    def test_check_passes(self, bundle: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        result = CliRunner().invoke(app, ["bundle", "check", str(bundle), "--no-references"])
        assert result.exit_code == 0, result.output
        assert "checked manifest, files, lake, bindings, statcat" in result.output

    def test_check_lists_findings_and_exits_1(self, bundle: Path) -> None:
        from typer.testing import CliRunner

        from statcatlib.cli import app

        (bundle / "search" / "stray.txt").write_text("not published")
        result = CliRunner().invoke(app, ["bundle", "check", str(bundle), "--no-references"])
        assert result.exit_code == 1
        assert "[files] search/stray.txt: in the bundle but not in the manifest" in result.output


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


class TestIndexAnnotations:
    """An overlay folded into the store is detected as a stale index, never rebuilt (GH-3)."""

    def test_build_index_records_what_it_read(self, bundle: Path) -> None:
        with sc.Catalog(bundle, read_only=True, data_path=Embedded()) as cat:
            recorded = cat.index_annotations
        assert recorded is not None
        assert recorded["types"] == sorted(sc.SEARCH_ANNOTATION_TYPES)
        section = json.loads((bundle / MANIFEST_NAME).read_text())["extensions"][STATCAT_SECTION]
        assert section["search"]["annotations"] == recorded

    def test_folded_search_overlay_warns_on_open_and_fails_the_check(self, bundle: Path) -> None:
        fold_overlays(bundle, [_overlay(sc.SEARCH_EXCLUDE_ANNOTATION_TYPE)])

        with pytest.warns(UserWarning, match=r"annotations of type .* have changed"):
            sc.Catalog(bundle, read_only=True, data_path=Embedded()).open().close()
        # It keeps firing until the index is rebuilt: the open itself fixes nothing.
        with pytest.warns(UserWarning, match=r"annotations of type .* have changed"):
            sc.Catalog(bundle, read_only=True, data_path=Embedded()).open().close()
        found = _findings(bundle)
        assert len(found) == 1
        assert found[0].startswith("[statcat] search annotations: annotations of type ")

    def test_rebuilding_the_index_clears_it(self, bundle: Path) -> None:
        fold_overlays(bundle, [_overlay(sc.SEARCH_EXCLUDE_ANNOTATION_TYPE)])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with sc.Catalog(bundle, data_path=Embedded()) as cat:
                cat.build_index()
        publish_bundle(bundle, bundle_id="synthetic", producer=_PRODUCER)

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            sc.Catalog(bundle, read_only=True, data_path=Embedded()).open().close()
        assert _findings(bundle) == []

    def test_folded_display_overlay_changes_nothing_for_search(self, bundle: Path) -> None:
        fold_overlays(bundle, [_overlay("LAYOUT_ROW")])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            sc.Catalog(bundle, read_only=True, data_path=Embedded()).open().close()
        assert _findings(bundle) == []

    def test_index_without_a_fingerprint_is_a_finding_not_a_warning(self, bundle: Path) -> None:
        index_json = bundle / "search" / "index.json"
        data = json.loads(index_json.read_text())
        del data["annotations"]
        index_json.write_text(json.dumps(data))
        manifest = json.loads((bundle / MANIFEST_NAME).read_text())
        manifest["extensions"][STATCAT_SECTION]["search"]["annotations"] = None
        (bundle / MANIFEST_NAME).write_text(json.dumps(manifest))

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            sc.Catalog(bundle, read_only=True, data_path=Embedded()).open().close()
        found = [f for f in _findings(bundle) if not f.startswith("[files]")]
        assert found == [
            "[statcat] search annotations: the index records no annotation fingerprint: rebuild with build_index()"
        ]


class TestAnnotationFingerprint:
    @pytest.fixture
    def conn(self, bundle: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(str(bundle / "meta.duckdb"))
        yield conn
        conn.close()

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

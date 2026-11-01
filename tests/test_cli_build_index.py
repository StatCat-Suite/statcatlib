"""Tests for the ``statcat build-index`` CLI, focused on the
``--codelist-policy`` flag.

The library-level guarantees (policy bypasses the size cap, forces
``embedding=None``, etc.) are covered by
``test_codelist_policy_and_concepts.py``. This file only verifies the
CLI parsing layer, the catalog and working-copy checks, and that the
parsed policies flow through to ``WorkingCopy.build_index``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib import cli as cli_mod
from statcatlib.cli import _parse_codelist_policy, _parse_reference_metadata_blend, app
from tests.conftest import FIXTURE_DATASETS

runner = CliRunner()

pytestmark = pytest.mark.unit


class TestParseReferenceMetadataBlend:
    def test_path_only_uses_default_max_chars(self) -> None:
        b = _parse_reference_metadata_blend("FOOTNOTE_TEXT")
        assert b == sc.ReferenceMetadataBlend(attribute_path="FOOTNOTE_TEXT")

    def test_path_with_max_chars(self) -> None:
        b = _parse_reference_metadata_blend("FOOTNOTE_TEXT:240")
        assert b.attribute_path == "FOOTNOTE_TEXT"
        assert b.max_chars == 240

    def test_whitespace_stripped(self) -> None:
        b = _parse_reference_metadata_blend("  FOOTNOTE_TEXT : 120 ")
        assert b.attribute_path == "FOOTNOTE_TEXT"
        assert b.max_chars == 120

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_reference_metadata_blend(":240")

    def test_non_integer_max_chars_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_reference_metadata_blend("FOOTNOTE_TEXT:lots")

    def test_non_positive_max_chars_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_reference_metadata_blend("FOOTNOTE_TEXT:0")


class TestParseCodelistPolicy:
    def test_single_urn(self) -> None:
        p = _parse_codelist_policy("STC:CL_GEO(1.0)=fts_only")
        assert p == sc.CodelistIndexPolicy(urns=("STC:CL_GEO(1.0)",), mode="fts_only")

    def test_multi_urn_comma_separated(self) -> None:
        p = _parse_codelist_policy("A:CL_X(1.0),B:CL_Y(2.0)=fts_only")
        assert p.urns == ("A:CL_X(1.0)", "B:CL_Y(2.0)")
        assert p.mode == "fts_only"

    def test_whitespace_around_urns_is_stripped(self) -> None:
        p = _parse_codelist_policy("  A , B  = fts_only ")
        assert p.urns == ("A", "B")
        assert p.mode == "fts_only"

    def test_missing_equals_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_codelist_policy("STC:CL_GEO(1.0)")

    def test_empty_urns_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_codelist_policy("=fts_only")

    def test_empty_mode_rejected(self) -> None:
        with pytest.raises(typer.BadParameter):
            _parse_codelist_policy("STC:CL_GEO(1.0)=")

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(typer.BadParameter) as exc:
            _parse_codelist_policy("STC:CL_GEO(1.0)=vector_only")
        assert "fts_only" in str(exc.value)


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    """A catalog whose product ``p`` has an unsealed working copy holding the fixture datasets."""
    path = tmp_path / "catalog"
    cat = sc.Catalog.create(path)
    wc = cat.product("p").working_copy()
    wc.put(FIXTURE_DATASETS)
    wc.close()
    cat.close()
    return path


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The keyword arguments the CLI passes to ``WorkingCopy.build_index``.

    The encoder loader and ``WorkingCopy.build_index`` are stubbed, so the
    test runs the CLI's real catalog and working-copy handling without
    loading a model or building an index.
    """
    sink: dict[str, Any] = {}  # a kwargs sink: the values are whatever build_index took

    def _build_index(_self: sc.WorkingCopy, **kwargs: Any) -> int:
        sink.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "_load_encoder", lambda model, device: object())
    monkeypatch.setattr(sc.WorkingCopy, "build_index", _build_index)
    return sink


class TestBuildIndexCli:
    def test_unknown_mode_exits_with_parameter_error(self, catalog_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "build-index",
                str(catalog_path),
                "p",
                "--codelist-policy",
                "X=does_not_exist",
            ],
        )
        assert result.exit_code != 0
        assert "fts_only" in (result.output + (result.stderr or ""))

    def test_a_directory_without_catalog_json_exits_naming_catalog_create(self, tmp_path: Path) -> None:
        folder = tmp_path / "not_a_catalog"
        folder.mkdir()
        result = runner.invoke(app, ["build-index", str(folder), "p"])
        assert result.exit_code == 1
        assert "Catalog.create" in result.output
        assert list(folder.iterdir()) == []

    def test_a_product_without_a_working_copy_exits_1(self, catalog_path: Path, captured: dict[str, Any]) -> None:
        result = runner.invoke(app, ["build-index", str(catalog_path), "q"])
        assert result.exit_code == 1
        assert "no unsealed edition" in result.output
        assert captured == {}

    def test_the_working_copy_stays_unsealed(self, catalog_path: Path, captured: dict[str, Any]) -> None:
        result = runner.invoke(app, ["build-index", str(catalog_path), "p"])
        assert result.exit_code == 0, result.output
        with sc.Catalog.create(catalog_path, exist_ok=True) as cat:
            wc = cat.product("p").working_copy(resume=True)
            assert wc.view().query(sc.BaseDataset).execute().total == len(FIXTURE_DATASETS)
            wc.close()

    def test_policies_flow_through_to_build_index(self, catalog_path: Path, captured: dict[str, Any]) -> None:
        """Parsed policies reach ``WorkingCopy.build_index``."""
        result = runner.invoke(
            app,
            [
                "build-index",
                str(catalog_path),
                "p",
                "--codelist-policy",
                "A:CL_X(1.0)=fts_only",
                "--codelist-policy",
                "B:CL_Y(1.0),C:CL_Z(1.0)=fts_only",
            ],
        )
        assert result.exit_code == 0, result.output
        policies = captured.get("codelist_policies")
        assert policies is not None
        assert len(policies) == 2
        assert policies[0].urns == ("A:CL_X(1.0)",)
        assert policies[1].urns == ("B:CL_Y(1.0)", "C:CL_Z(1.0)")
        assert all(p.mode == "fts_only" for p in policies)

    def test_no_policy_flag_yields_empty_list(self, catalog_path: Path, captured: dict[str, Any]) -> None:
        result = runner.invoke(app, ["build-index", str(catalog_path), "p"])
        assert result.exit_code == 0, result.output
        assert captured.get("codelist_policies") == []
        assert captured.get("reference_metadata_blends") == []

    def test_blends_flow_through_to_build_index(self, catalog_path: Path, captured: dict[str, Any]) -> None:
        """Parsed reference-metadata blends reach ``WorkingCopy.build_index``."""
        result = runner.invoke(
            app,
            [
                "build-index",
                str(catalog_path),
                "p",
                "--reference-metadata-blend",
                "FOOTNOTE_TEXT:240",
            ],
        )
        assert result.exit_code == 0, result.output
        blends = captured.get("reference_metadata_blends")
        assert blends is not None
        assert len(blends) == 1
        assert blends[0].attribute_path == "FOOTNOTE_TEXT"
        assert blends[0].max_chars == 240

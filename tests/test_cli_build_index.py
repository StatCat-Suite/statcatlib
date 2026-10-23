"""Tests for the ``statcat build-index`` CLI, focused on the
``--codelist-policy`` flag.

The library-level guarantees (policy bypasses the size cap, forces
``embedding=None``, etc.) are covered by
``test_codelist_policy_and_concepts.py``. This file only verifies the
CLI parsing layer and that the parsed policies flow through to
``Catalog.build_index``.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer
from typer.testing import CliRunner

import statcatlib as sc
from statcatlib.cli import _parse_codelist_policy, _parse_reference_metadata_blend, app

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


class TestBuildIndexCli:
    def test_unknown_mode_exits_with_parameter_error(self, tmp_path) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "meta.duckdb").write_bytes(b"")

        result = runner.invoke(
            app,
            [
                "build-index",
                str(bundle),
                "--codelist-policy",
                "X=does_not_exist",
            ],
        )
        assert result.exit_code != 0
        assert "fts_only" in (result.output + (result.stderr or ""))

    def test_policies_flow_through_to_build_index(self, tmp_path, monkeypatch) -> None:
        """Parsed policies must reach ``Catalog.build_index``.

        We stub the encoder loader and the store's ``build_index`` to
        capture kwargs without running the real pipeline.
        """
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "meta.duckdb").write_bytes(b"")

        captured: dict[str, Any] = {}  # a kwargs sink: the values are whatever build_index took

        from statcatlib import cli as cli_mod

        monkeypatch.setattr(cli_mod, "_load_encoder", lambda model, device: object())

        class _FakeStore:
            embedding_metadata: object = None

            def build_index(self, **kwargs):
                captured.update(kwargs)
                return 0

        class _FakeCatalog:
            def __init__(self, _path: str) -> None:
                pass

            def open(self) -> _FakeStore:
                return _FakeStore()

        monkeypatch.setattr(cli_mod.sc, "Catalog", _FakeCatalog)

        result = runner.invoke(
            app,
            [
                "build-index",
                str(bundle),
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

    def test_no_policy_flag_yields_empty_list(self, tmp_path, monkeypatch) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "meta.duckdb").write_bytes(b"")

        captured: dict[str, Any] = {}  # a kwargs sink: the values are whatever build_index took
        from statcatlib import cli as cli_mod

        monkeypatch.setattr(cli_mod, "_load_encoder", lambda model, device: object())

        class _FakeStore:
            embedding_metadata: object = None

            def build_index(self, **kwargs):
                captured.update(kwargs)
                return 0

        class _FakeCatalog:
            def __init__(self, _path: str) -> None:
                pass

            def open(self) -> _FakeStore:
                return _FakeStore()

        monkeypatch.setattr(cli_mod.sc, "Catalog", _FakeCatalog)

        result = runner.invoke(app, ["build-index", str(bundle)])
        assert result.exit_code == 0, result.output
        assert captured.get("codelist_policies") == []
        assert captured.get("reference_metadata_blends") == []

    def test_blends_flow_through_to_build_index(self, tmp_path, monkeypatch) -> None:
        """Parsed reference-metadata blends must reach ``Catalog.build_index``."""
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "meta.duckdb").write_bytes(b"")

        captured: dict[str, Any] = {}  # a kwargs sink: the values are whatever build_index took
        from statcatlib import cli as cli_mod

        monkeypatch.setattr(cli_mod, "_load_encoder", lambda model, device: object())

        class _FakeStore:
            embedding_metadata: object = None

            def build_index(self, **kwargs):
                captured.update(kwargs)
                return 0

        class _FakeCatalog:
            def __init__(self, _path: str) -> None:
                pass

            def open(self) -> _FakeStore:
                return _FakeStore()

        monkeypatch.setattr(cli_mod.sc, "Catalog", _FakeCatalog)

        result = runner.invoke(
            app,
            [
                "build-index",
                str(bundle),
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

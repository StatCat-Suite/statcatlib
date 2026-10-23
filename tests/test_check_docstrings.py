"""Tests for `tools/check_docstrings.py`, the docstring convention gate (GH-9).

Ported from sdmxlib's `tests/unit/test_check_docstrings.py`. A gate breaks
**open**: a check that finds nothing reports the same green whether the tree is
clean or the check is broken. So each arm is shown to catch its own violation,
and the NumPy arm is shown to leave a prose heading alone.

sdmxlib's pydoclint arm shipped reading stdout, where pydoclint writes nothing,
and reported a clean tree on a file it had just flagged. Here the pydoclint arm
runs pydoclint on a real finding, which fails if the parser reads the wrong
stream.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TOOL = ROOT / "tools" / "check_docstrings.py"
_spec = importlib.util.spec_from_file_location("check_docstrings", _TOOL)
assert _spec is not None
assert _spec.loader is not None
check_docstrings = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docstrings)

pytestmark = pytest.mark.unit


def _tree(tmp_path: Path, body: str) -> Path:
    """A throwaway root holding one module under `TARGET`, for the arms to scan."""
    target = tmp_path / check_docstrings.TARGET
    target.mkdir(parents=True)
    (target / "probe.py").write_text(body, encoding="utf-8")
    return tmp_path


# ── arm 1: Sphinx / RST markup ────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [":class:`Catalog`", ":meth:`get`", ":func:`parse`", ":exc:`ValueError`", ":py:attr:`id`", ":ref:`guide`"],
)
def test_a_sphinx_role_is_found(tmp_path: Path, role: str) -> None:
    """The list is the dialect: `:ref:` and the `py:` spellings count too."""
    found = check_docstrings.rst_markup(_tree(tmp_path, f'"""Summary naming {role} the RST way."""\n'))
    assert len(found) == 1
    assert role in found[0][2]


def test_an_rst_literal_block_is_found(tmp_path: Path) -> None:
    body = '"""Summary.\n\n    Example::\n\n        x = 1\n    """\n'
    found = check_docstrings.rst_markup(_tree(tmp_path, body))
    assert [f[2] for f in found] == ["RST literal block: Example::"]


def test_a_google_example_section_is_not_markup(tmp_path: Path) -> None:
    """One colon is the Google spelling and stays legal."""
    body = '"""Summary.\n\n    Example:\n        x = 1\n    """\n'
    assert check_docstrings.rst_markup(_tree(tmp_path, body)) == []


# ── arm 2: NumPy sections, and the false positive it must avoid ───────────


@pytest.mark.parametrize("section", ["Parameters", "Returns", "Raises", "Attributes", "See Also", "Notes"])
def test_a_numpy_section_is_found(tmp_path: Path, section: str) -> None:
    body = f'"""Summary.\n\n    {section}\n    {"-" * len(section)}\n    x : int\n        A thing.\n    """\n'
    found = check_docstrings.numpy_sections(_tree(tmp_path, body))
    assert len(found) == 1, f"{section} underlined is a NumPy section"
    assert section in found[0][2]


def test_an_underlined_section_with_a_colon_is_found(tmp_path: Path) -> None:
    """The old recipe matched `Returns:` underlined too; the port keeps that."""
    body = '"""Summary.\n\n    Returns:\n    --------\n    int\n    """\n'
    assert len(check_docstrings.numpy_sections(_tree(tmp_path, body))) == 1


def test_a_prose_heading_is_not_a_numpy_section(tmp_path: Path) -> None:
    """A module docstring may head prose with an RST rule; that is not a NumPy section."""
    heading = "Why the catalog is the materialized graph"
    body = f'"""Summary.\n\n{heading}\n{"-" * len(heading)}\nBecause.\n"""\n'
    assert check_docstrings.numpy_sections(_tree(tmp_path, body)) == []


def test_a_google_section_is_not_a_numpy_section(tmp_path: Path) -> None:
    body = '"""Summary.\n\n    Returns:\n        A thing.\n    """\n'
    assert check_docstrings.numpy_sections(_tree(tmp_path, body)) == []


def test_a_section_name_on_the_last_line_does_not_run_off_the_end(tmp_path: Path) -> None:
    """The arm looks one line ahead; a section name as the final line must not raise."""
    assert check_docstrings.numpy_sections(_tree(tmp_path, "# Returns\nReturns")) == []


# ── arm 3: the argument section's name ────────────────────────────────────


@pytest.mark.parametrize("name", ["Parameters", "Arguments", "Params"])
def test_an_argument_section_not_named_args_is_found(tmp_path: Path, name: str) -> None:
    body = f'def f(x: int) -> None:\n    """Summary.\n\n    {name}:\n        x: A thing.\n    """\n'
    found = check_docstrings.argument_section_names(_tree(tmp_path, body))
    assert len(found) == 1
    assert name in found[0][2]


def test_args_is_the_google_spelling(tmp_path: Path) -> None:
    body = 'def f(x: int) -> None:\n    """Summary.\n\n    Args:\n        x: A thing.\n    """\n'
    assert check_docstrings.argument_section_names(_tree(tmp_path, body)) == []


# ── arm 4: pydoclint, run for real ────────────────────────────────────────


def test_pydoclint_reports_an_undocumented_argument(tmp_path: Path) -> None:
    """The sabotage case: a real finding must come back as a finding."""
    body = 'def f(x: int, y: int) -> None:\n    """Summary.\n\n    Args:\n        x: A thing.\n    """\n'
    found = check_docstrings.pydoclint_findings(_tree(tmp_path, body))
    assert found, "pydoclint flagged `y` as undocumented, and the arm must say so"
    assert all(f[0].endswith("probe.py") for f in found)
    assert any("DOC101" in f[2] or "DOC103" in f[2] for f in found)


def test_pydoclint_passes_a_google_docstring(tmp_path: Path) -> None:
    body = (
        "def f(x: int) -> int:\n"
        '    """Summary.\n\n    Args:\n        x: A thing.\n\n    Returns:\n        It.\n    """\n'
        "    return x\n"
    )
    assert check_docstrings.pydoclint_findings(_tree(tmp_path, body)) == []


# ── the arms are wired in ─────────────────────────────────────────────────


def test_every_arm_is_registered() -> None:
    """`ARMS` is what `main` iterates, so an arm left out of it never runs."""
    registered = {arm for _, arm in check_docstrings.ARMS}
    assert registered == {
        check_docstrings.rst_markup,
        check_docstrings.numpy_sections,
        check_docstrings.argument_section_names,
        check_docstrings.pydoclint_findings,
    }


def test_the_repo_is_clean() -> None:
    """The gate starts at zero; a finding here is a real docstring to fix."""
    findings = [(arm.__name__, f) for _, arm in check_docstrings.ARMS for f in arm(ROOT)]
    assert findings == []

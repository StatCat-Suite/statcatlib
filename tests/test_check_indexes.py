"""Tests for `tools/check_indexes.py` — the guard that only public PyPI is named.

This gate fails open, like every gate: a scanner that stops matching reports a
clean tree forever, and nobody investigates a passing check. So the cases below
come in pairs — a leak that must be caught, and the prose that must not be
mistaken for one.

The false-positive half is not hypothetical. The first version of the pattern
allowed a single dash, and found five "index directives" in this repository:
four sentences about a DuckDB ART-index and one about a `source-index` mapping.
A guard that cries wolf on English gets switched off, which is the same outcome
as not having it.

Note what is deliberately absent: no test names the private mirror this exists
to keep out. Writing that hostname into the suite would leak it exactly as
committing it to `pyproject.toml` would, and the allow-list design means a
made-up host exercises the identical code path.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_TOOL = ROOT / "tools" / "check_indexes.py"
_spec = importlib.util.spec_from_file_location("check_indexes", _TOOL)
assert _spec is not None
assert _spec.loader is not None
check_indexes = importlib.util.module_from_spec(_spec)
# Registered before exec: `@dataclass` resolves annotations through
# `sys.modules[cls.__module__]`, which is None for a module loaded by path
# alone — collection fails with a bare AttributeError out of dataclasses.
sys.modules["check_indexes"] = check_indexes
_spec.loader.exec_module(check_indexes)
scan_text, survey = check_indexes.scan_text, check_indexes.survey
is_public, structural_findings = check_indexes.is_public, check_indexes.structural_findings
SELF_EXEMPT = check_indexes.SELF_EXEMPT

pytestmark = pytest.mark.unit


def test_a_private_index_url_is_caught() -> None:
    text = 'index-url = "https://packages.example.invalid/api/pypi/simple"\n'
    findings = scan_text("pyproject.toml", text)
    assert len(findings) == 1
    assert "packages.example.invalid" in findings[0].reason


def test_public_pypi_is_allowed() -> None:
    assert scan_text("pyproject.toml", 'index-url = "https://pypi.org/simple"\n') == []
    assert scan_text("ci.yml", "pip install --index-url https://pypi.org/simple uv\n") == []


def test_every_directive_spelling_is_recognised() -> None:
    """Each spelling a Python toolchain accepts, pointed at the same private host."""
    lines = [
        "pip install --extra-index-url https://mirror.example.invalid/simple statcatlib",
        "pip install -i https://mirror.example.invalid/simple statcatlib",
        "  extra-index-url = https://mirror.example.invalid/simple",
        "  trusted-host = mirror.example.invalid",
        'PIP_INDEX_URL: "https://mirror.example.invalid/simple"',
        "UV_DEFAULT_INDEX=https://mirror.example.invalid/simple",
        "  publish-url = https://mirror.example.invalid/upload",
        "  find-links = https://mirror.example.invalid/wheels",
    ]
    for line in lines:
        assert scan_text("f", line + "\n"), f"missed: {line}"


def test_uv_index_table_is_read_across_lines() -> None:
    """uv puts the url on a later line than the table header."""
    private = '[[tool.uv.index]]\nname = "internal"\nurl = "https://mirror.example.invalid/simple"\n'
    assert len(scan_text("pyproject.toml", private)) == 1

    public = '[[tool.uv.index]]\nname = "pypi"\nurl = "https://pypi.org/simple"\n'
    assert scan_text("pyproject.toml", public) == []


def test_the_table_scan_stops_at_the_next_header() -> None:
    """A url in an unrelated table below must not be attributed to the index."""
    text = (
        '[[tool.uv.index]]\nurl = "https://pypi.org/simple"\n\n[tool.other]\nurl = "https://mirror.example.invalid"\n'
    )
    assert scan_text("pyproject.toml", text) == []


def test_a_directive_with_no_readable_host_is_a_finding() -> None:
    """Unverifiable is not the same as absent — a guard that shrugs is not a guard."""
    findings = scan_text("ci.yml", "  index-url = ${SECRET_INDEX}\n")
    assert len(findings) == 1
    assert "cannot be verified" in findings[0].reason


@pytest.mark.parametrize(
    "prose",
    [
        "    absent. Avoiding ``OR REPLACE`` sidesteps a DuckDB ART-index/heap",
        "    regex and substring-index qualifiers we do not model.",
        "    f\"{'regex' if isinstance(m, RegexValueMap) else 'source-index'} mapping has no \"",
        "    # the DuckDB ART-index desync of #70. Named here so a future reader does",
        "    print(f'HTML report: htmlcov/index.html')",
    ],
)
def test_prose_about_indexes_is_not_a_finding(prose: str) -> None:
    """The five real lines that the single-dash pattern flagged, plus a path."""
    assert scan_text("src/x.py", prose + "\n") == []


def test_public_host_list_is_exact() -> None:
    """A lookalike domain must not pass — the check is equality, not suffix."""
    assert is_public("pypi.org")
    assert is_public("PyPI.org")  # host comparison folds case
    assert not is_public("pypi.org.example.invalid")
    assert not is_public("notpypi.org")


def test_the_repository_itself_is_clean() -> None:
    """The gate's own subject. Fails loudly here before it fails in CI.

    This passed once while both of the guard's files were untracked, which is
    exactly the state in which `git ls-files` hides them: the first run after
    they were committed reported eleven findings, all of them the fixtures
    below. Hence `SELF_EXEMPT`, and hence the next two tests.
    """
    assert survey(ROOT) == []


def test_only_the_guards_own_files_are_exempt() -> None:
    """The blind spot is two files, and it stays two files."""
    assert set(SELF_EXEMPT) == {"tests/test_check_indexes.py", "tools/check_indexes.py"}


def test_the_exempt_paths_exist() -> None:
    """An exemption for a moved file is a silent hole, not a stale string."""
    for rel in SELF_EXEMPT:
        assert (ROOT / rel).is_file(), rel


def test_the_exemption_does_not_reach_scan_text() -> None:
    """Exempting happens in `survey`; the scanner itself stays honest."""
    leak = 'index-url = "https://mirror.example.invalid/simple"\n'
    assert len(scan_text("tools/check_indexes.py", leak)) == 1


@pytest.mark.parametrize("name", [".netrc", ".pypirc", "pip.conf", "pip.ini", "uv.lock"])
def test_a_credential_bearing_file_is_caught_by_name(name: str) -> None:
    """These leak a mirror without ever naming a directive."""
    findings = structural_findings([f"config/{name}"], lock_ignored=True)
    assert [f.path for f in findings] == [f"config/{name}"]
    assert "must not be tracked" in findings[0].reason


def test_an_ordinary_file_is_not() -> None:
    assert structural_findings(["src/statcatlib/model/dataset.py"], lock_ignored=True) == []


def test_an_unignored_lockfile_is_caught() -> None:
    """Catches the state before the commit, not only after it."""
    assert any("uv.lock is not ignored" in f.reason for f in structural_findings([], lock_ignored=False))
    assert structural_findings([], lock_ignored=True) == []

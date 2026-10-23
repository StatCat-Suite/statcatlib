"""Tests for `tools/check_prose.py`, the writing-style gate (GH-28).

Ported from sdmxlib's `tests/unit/test_check_prose.py`. A gate that finds
nothing reports the same green whether the tree is clean or the gate is broken,
so each check is shown catching its own violation, and each exemption is shown
to be no wider than it says.

Each sample carries a `|` inside its phrase, removed at runtime: a literal
banned phrase in this file would be a finding in it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_TOOL = ROOT / "tools" / "check_prose.py"
_spec = importlib.util.spec_from_file_location("check_prose", _TOOL)
assert _spec is not None
assert _spec.loader is not None
check_prose = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_prose)

pytestmark = pytest.mark.unit

#: One sample sentence per banned phrase, in `PHRASES` order.
_SAMPLES = tuple(
    s.replace("|", "")
    for s in (
        "Here's| the plan",
        "here is| the thing",
        "Why this| matters",
        "the whole| point",
        "The real| issue: x",
        "the short| version",
        "TL;|DR",
        "the part| nobody",
        "nobody| mentions",
        "less than| you'd think",
    )
)


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_every_phrase_has_a_sample_that_it_matches() -> None:
    """The samples cover the list exactly: one per pattern, each matching it."""
    assert len(_SAMPLES) == len(check_prose.PHRASES)
    for pattern, sample in zip(check_prose.PHRASES, _SAMPLES, strict=True):
        assert re.search(pattern, sample, re.IGNORECASE), (pattern, sample)


def test_a_question_heading_is_found(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"docs/page.md": "# Title\n\n## Why a registry?\n\nText.\n"})
    assert check_prose.find(root) == ["docs/page.md:3: heading is a question: 'Why a registry?'"]


def test_a_heading_with_an_attribute_list_is_read_without_it(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"README.md": "## Is it fast? {#fast}\n"})
    assert check_prose.find(root) == ["README.md:1: heading is a question: 'Is it fast?'"]


def test_a_question_in_a_code_fence_or_body_is_not_a_heading(tmp_path: Path) -> None:
    """The example-query comments in `docs/tutorial.md` sit in fences like this one."""
    root = _tree(tmp_path, {"docs/page.md": "```python\n# which one?\n```\n\nDoes it stream? Yes.\n"})
    assert check_prose.find(root) == []


@pytest.mark.parametrize("sample", _SAMPLES)
def test_a_phrase_is_found_in_markdown_and_in_code(tmp_path: Path, sample: str) -> None:
    root = _tree(tmp_path, {"docs/page.md": f"Some text. {sample} more.\n", "src/m.py": f"# {sample}\n"})
    assert [f.split(":", 2)[:2] for f in check_prose.find(root)] == [["docs/page.md", "1"], ["src/m.py", "1"]]


@pytest.mark.parametrize("rel", ["docs/api/store.md", "README.md", "llms.txt"])
def test_every_markdown_target_is_read(tmp_path: Path, rel: str) -> None:
    root = _tree(tmp_path, {rel: "## A question?\n"})
    assert [f.split(":", 1)[0] for f in check_prose.find(root)] == [rel]


@pytest.mark.parametrize("rel", ["tests/t.py", "tools/x.py", "justfile", "pyproject.toml", "ruff.toml", "pyrefly.toml"])
def test_every_code_target_is_read(tmp_path: Path, rel: str) -> None:
    root = _tree(tmp_path, {rel: f"# {_SAMPLES[3]}\n"})
    assert [f.split(":", 1)[0] for f in check_prose.find(root)] == [rel]


def test_the_rules_section_of_claude_md_is_exempt_and_nothing_else(tmp_path: Path) -> None:
    """The section quoting the banned phrases is skipped; the next one is read."""
    text = (
        f"## Before\n\n{_SAMPLES[0]} one.\n\n"
        f"## Writing style\n\n{_SAMPLES[1]} two.\n\n## Is this a question?\n\n"
        f"{_SAMPLES[2]} three.\n"
    )
    root = _tree(tmp_path, {"CLAUDE.md": text})
    assert [f.split(":", 2)[1] for f in check_prose.find(root)] == ["3", "9", "11"]


def test_the_changelog_and_design_docs_are_not_read(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"CHANGELOG.md": f"## Old?\n\n{_SAMPLES[3]}\n", "design/spec.md": "## Old?\n"})
    assert check_prose.find(root) == []


def test_the_tree_is_clean() -> None:
    assert check_prose.find(ROOT) == []

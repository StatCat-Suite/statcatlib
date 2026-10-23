"""Fail when a docstring drifts from plain Google style (GH-9).

Ported from sdmxlib's ``tools/check_docstrings.py``, its four checks ("arms")
and their tests, which replaced an inline justfile recipe here. The recipe had
no test that it could fail. sdmxlib found its pydoclint arm reporting a clean
tree on a file it had just flagged, and only by sabotaging the arm. The tests
in ``tests/test_check_docstrings.py`` are that sabotage, kept.

## Why this exists, when `ruff.toml` already says `convention = "google"`

Ruff's pydocstyle rules check a docstring's **structure** (summary line, blank
lines, punctuation). They never read its markup or reject a NumPy section
header. With that line declared and nothing checking it, 98 Sphinx roles and
13 NumPy-sectioned docstrings accumulated here while the project was pinax.

## The four arms

1. **No Sphinx/RST markup:** cross-reference roles (``:class:`X` ``, every role
   in the dialect, not only the ones seen so far) and ``Example::`` literal
   blocks. A grep, because banning a markup dialect is house policy, and
   pydoclint correctly parses roles as ordinary prose.
2. **No NumPy section underlines**, matched as a section *name* followed by a
   rule. A rule under anything else is a prose heading in a module docstring,
   which is fine and common.
3. **No `Parameters:` / `Arguments:` / `Params:` headings.** Google spells the
   section ``Args:``. pydoclint parses the others well enough that DOC003 stays
   quiet, which is how three survived a sweep that relied on it. statcatlib's
   own arm.
4. **pydoclint, in full**, with the settings in ``pyproject.toml``'s
   ``[tool.pydoclint]``. sdmxlib consults only DOC003, because its tree carries
   600-odd other findings. statcatlib cleared all of its own, so every
   code counts here: a docstring that is not Google (DOC003), and one that
   disagrees with its signature (missing args, undocumented raises, a return
   section with no return).

## Usage

    uv run python tools/check_docstrings.py           # report
    uv run python tools/check_docstrings.py --check   # exit 1 if any
"""

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

#: The published surface. Tests carry docstrings that are prose for a reader,
#: and the convention this enforces is about API documentation.
TARGET = "src/statcatlib"

#: The repo root, whose ``pyproject.toml`` holds pydoclint's settings. Passed
#: to pydoclint explicitly, so a run over another root uses the same ones.
ROOT = Path(__file__).resolve().parent.parent

#: Sphinx/RST cross-reference roles. The list is the dialect, not the subset
#: seen so far: a guard that only bans what already appeared bans nothing.
_ROLE_NAMES = (
    "class",
    "meth",
    "func",
    "mod",
    "attr",
    "obj",
    "exc",
    "data",
    "ref",
    "doc",
    "term",
    "const",
    "keyword",
    "envvar",
    "option",
)
ROLE_RE = re.compile(rf":(?:py:)?(?:{'|'.join(_ROLE_NAMES)}):`")

#: ``Example::`` and friends, the RST literal-block marker. Google style spells
#: it as an ``Example:`` section with an indented body.
LITERAL_BLOCK_RE = re.compile(r"^\s*(Example|Examples|Usage|Note|Warning)::\s*$")

#: NumPy section names. A line holding one of these *followed by a rule* is a
#: NumPy section; a rule under anything else is a prose heading.
_SECTIONS = (
    "Parameters",
    "Other Parameters",
    "Returns",
    "Yields",
    "Receives",
    "Raises",
    "Warns",
    "Warnings",
    "See Also",
    "Notes",
    "References",
    "Examples",
    "Attributes",
    "Methods",
)
SECTION_RE = re.compile(rf"^\s*(?:{'|'.join(_SECTIONS)}):?\s*$")
RULE_RE = re.compile(r"^\s*[-=~^]{3,}\s*$")

#: The argument section under a name other than Google's ``Args:``.
NOT_ARGS_RE = re.compile(r"^\s*(Parameters|Arguments|Params):\s*$")

#: One pydoclint finding: ``    12: DOC101: Function `f`: ...``.
PYDOCLINT_FINDING_RE = re.compile(r"^\s*(\d+):\s*(DOC\d+:.*)$")

type Finding = tuple[str, int, str]


def _python_files(root: Path) -> list[Path]:
    target = root / TARGET
    if not target.is_dir():
        return []
    return sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)


def _lines(root: Path) -> list[tuple[str, list[str]]]:
    return [(str(p.relative_to(root)), p.read_text(encoding="utf-8").splitlines()) for p in _python_files(root)]


def rst_markup(root: Path) -> list[Finding]:
    """Every Sphinx role and RST literal-block marker under `TARGET`."""
    found: list[Finding] = []
    for path, lines in _lines(root):
        for number, line in enumerate(lines, start=1):
            if ROLE_RE.search(line):
                found.append((path, number, f"Sphinx role: {line.strip()}"))
            elif LITERAL_BLOCK_RE.match(line):
                found.append((path, number, f"RST literal block: {line.strip()}"))
    return found


def numpy_sections(root: Path) -> list[Finding]:
    """Every NumPy section header: a section *name* underlined by a rule.

    Deliberately not every underlined line. A module docstring may head its
    prose with an RST rule, and that is a good Google-style docstring; a
    rule-only match calls it a NumPy section.
    """
    found: list[Finding] = []
    for path, lines in _lines(root):
        for number, line in enumerate(lines[:-1], start=1):
            if SECTION_RE.match(line) and RULE_RE.match(lines[number]):
                found.append((path, number, f"NumPy section: {line.strip()!r} underlined"))
    return found


def argument_section_names(root: Path) -> list[Finding]:
    """Every argument section spelled other than ``Args:``."""
    found: list[Finding] = []
    for path, lines in _lines(root):
        for number, line in enumerate(lines, start=1):
            if NOT_ARGS_RE.match(line):
                found.append((path, number, f"Google spells this section 'Args:': {line.strip()}"))
    return found


def pydoclint_findings(root: Path) -> list[Finding]:
    """Every pydoclint finding under `TARGET`, with the repo's settings.

    pydoclint exits 1 whenever it reports anything, so only another exit code
    counts as a failure to run.
    """
    if not (root / TARGET).is_dir():
        return []
    uv = shutil.which("uv")
    if uv is None:
        sys.stderr.write("check_docstrings: uv is not on PATH\n")
        raise SystemExit(2)
    # S603 asks for a literal argument list; the executable is a resolved path
    # and the arguments are this module's own constants.
    proc = subprocess.run(  # noqa: S603
        [uv, "run", "pydoclint", f"--config={ROOT / 'pyproject.toml'}", "--quiet", TARGET],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        sys.stderr.write(f"pydoclint failed to run:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}\n")
        raise SystemExit(2)

    # stderr, and stdout too. pydoclint writes its findings to stderr and
    # leaves stdout empty, so a parser reading stdout alone reports a clean
    # tree forever. sdmxlib's arm shipped that way and was caught only by
    # sabotage; `test_check_docstrings.py` runs pydoclint on a real finding.
    found: list[Finding] = []
    current = ""
    for raw in (proc.stderr + proc.stdout).splitlines():
        line = raw.rstrip()
        if line.endswith(".py"):
            current = line
        elif match := PYDOCLINT_FINDING_RE.match(line):
            found.append((current, int(match.group(1)), match.group(2)))
    return found


ARMS: tuple[tuple[str, Callable[[Path], list[Finding]]], ...] = (
    ("Sphinx/RST markup", rst_markup),
    ("NumPy section headers", numpy_sections),
    ("argument section named other than Args:", argument_section_names),
    ("pydoclint", pydoclint_findings),
)


def main() -> int:
    """Run every arm over the repo and report; with ``--check``, exit 1 on any finding.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description="Docstring convention gate (GH-9)")
    parser.add_argument("--check", action="store_true", help="exit 1 when any finding is present")
    args = parser.parse_args()

    out = sys.stdout
    total = 0
    for label, arm in ARMS:
        found = arm(ROOT)
        total += len(found)
        if not found:
            out.write(f"  ok    {label}\n")
            continue
        out.write(f"  FAIL  {label} — {len(found)}\n")
        for file, line, message in found:
            out.write(f"          {file}:{line}  {message}\n")

    if not total:
        out.write(f"{TARGET}/: docstrings are plain Google style\n")
        return 0
    if args.check:
        out.write(f"\n{total} finding(s). CLAUDE.md's convention is pure Google-style docstrings. See GH-9.\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

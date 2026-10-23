"""Fail when the repo configures a package index that is not public PyPI.

## Why this exists

A contributor's machine may resolve Python packages through a private
mirror. That is fine for a developer's environment and wrong for this
repository: an index host, a mirror path, or a token URL committed here would
publish that mirror's address to every consumer of a public library, and would
break the build for everyone outside that network.

`.gitignore` already keeps `uv.lock` out for exactly this reason — a lockfile
records whichever index resolved it. That comment is a note to a human. This
is the check.

## The shape of the rule, and why it is an allow-list

The obvious guard greps for the mirror's hostname. That guard cannot be
written here, because writing it *is* the leak — the pattern would name in the
gate the thing the gate exists to keep out, and it would be committed,
searchable and permanent.

So the rule is inverted: an index directive may point at a **public** index and
nothing else. The allow-list below is short, public knowledge, and complete;
anything else is a finding whether it belongs to a company, a cloud, or a
neighbour. A new private mirror needs no update here to be caught.

## What counts as a finding

1. **An index directive naming a non-public host.** `index-url`,
   `extra-index-url`, `find-links`, `trusted-host`, `publish-url`, the
   `UV_INDEX*` / `PIP_*_INDEX_URL` environment variables, and uv's
   `[[tool.uv.index]]` tables. A directive that *assigns* an unreadable value
   (`index-url = ${SECRET}`) is a finding too: a guard that stays quiet when
   it cannot tell is not a guard. A line that merely mentions a directive
   without assigning one is not — see `directive_value` for where that line
   is drawn, and why it had to be.
2. **A tracked file that carries index credentials by nature** — `uv.lock`,
   `.netrc`, `.pypirc`, `pip.conf`, `pip.ini`. These leak a mirror without
   ever spelling out a directive.
3. **`uv.lock` not being ignored.** Rule 2 catches it once committed; this
   catches the state where the next `git add` would commit it.

Every tracked text file is scanned, not just the config ones. A README that
tells a contributor to install from a private mirror leaks the same host as a
`pyproject.toml` that resolves from it. The two exceptions are this file and
its test — see `SELF_EXEMPT` for why a scanner cannot scan its own fixtures.

## Usage

    uv run python tools/check_indexes.py           # report
    uv run python tools/check_indexes.py --check    # exit 1 if any finding
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Public package indexes, and the CDN a public resolve pulls artefacts from.
# Everything absent from this set is a finding — that is the whole design, so
# additions belong in review, not in a hurry.
PUBLIC_INDEX_HOSTS = frozenset(
    {
        "pypi.org",
        "www.pypi.org",
        "files.pythonhosted.org",
        "test.pypi.org",
        "upload.pypi.org",
    }
)

# Files whose very presence in the tree records an index or its credentials.
CREDENTIAL_BEARING_FILES = frozenset(
    {
        ".netrc",
        ".pypirc",
        "pip.conf",
        "pip.ini",
        "uv.lock",
    }
)

# An index directive in any of the spellings a Python toolchain accepts: CLI
# flags, TOML/INI keys, and environment variables.
#
# The flag forms require both dashes. A single-dash alternative matched
# `-index` inside prose about a DuckDB ART-index and a `source-index` mapping,
# which is the failure mode a text scan has to be held to: five findings, all
# of them English.
_DIRECTIVE = re.compile(
    r"(?ix)"
    r"(?P<name>"
    r"-- (?: index-url | extra-index-url | find-links | trusted-host | publish-url | default-index | index )\b"
    r"| (?<![\w-]) -i \s+ (?= https?:// )"
    r"| \b (?: index[-_]url | extra[-_]index[-_]url | find[-_]links | trusted[-_]host | publish[-_]url ) \b"
    r"| \b (?: UV_INDEX | UV_INDEX_URL | UV_DEFAULT_INDEX | UV_EXTRA_INDEX_URL | UV_PUBLISH_URL )\w* \b"
    r"| \b (?: PIP_INDEX_URL | PIP_EXTRA_INDEX_URL | PIP_TRUSTED_HOST ) \b"
    r")"
)

# uv's index tables put the url on a later line: `[[tool.uv.index]]` then
# `url = "..."`. Track the table and read the url out of its body.
_INDEX_TABLE = re.compile(r"^\s*\[\[\s*tool\.uv\.index\s*\]\]")
_TABLE_HEADER = re.compile(r"^\s*\[")
_URL_KEY = re.compile(r"""^\s*url\s*=\s*["'](?P<url>[^"']+)["']""")

_URL = re.compile(r"https?://(?P<host>[A-Za-z0-9._-]+)")

# What follows a directive decides whether the line *configures* an index or
# merely mentions one. `index-url = <something>` configures; "an `index-url`,
# or a stray `.netrc`" is a sentence. Requiring an assignment separator is
# what keeps the guard off English — including this file's own prose, which
# the first version flagged twice.
_ASSIGNED = re.compile(r"""^\s*[=:]\s*["']?(?P<value>[^\s"',]+)""")
_FLAG_VALUE = re.compile(r"""^\s+["']?(?P<value>[^\s"',]+)""")
_VARIABLE = re.compile(r"^[$%]")
_COMMENT = re.compile(r"^\s*(#|//|--\s)")

# The guard's own files. They must contain non-public examples to be testable
# at all — a scanner tested only against public hosts is a scanner nobody has
# watched fail — so scanning them reports the fixtures as leaks.
#
# This exemption was not foreseen; it was earned. The gate was verified while
# both files were still untracked, so `git ls-files` never handed them over,
# and the first run after they were committed produced eleven findings, every
# one of them test data. That is the ordinary shape of a linter exempting its
# own fixtures, and the cost is bounded: two files, whose entire content is
# reviewed by the tests below them.
SELF_EXEMPT = frozenset(
    {
        "tools/check_indexes.py",
        "tests/test_check_indexes.py",
    }
)

_MAX_BYTES = 4_000_000


@dataclass(frozen=True)
class Finding:
    """One reason the tree fails the check."""

    path: str
    line: int
    reason: str
    excerpt: str

    def render(self) -> str:
        """Format as `path:line: reason` with the offending text beneath."""
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.reason}\n    {self.excerpt.strip()}"


def is_public(host: str) -> bool:
    """Whether `host` is a public package index we allow to be named."""
    return host.lower() in PUBLIC_INDEX_HOSTS


def hosts_in(text: str) -> list[str]:
    """Every host named by a full URL in `text`."""
    return [m.group("host") for m in _URL.finditer(text)]


def directive_value(tail: str, *, flag: bool) -> str | None:
    """The value a directive assigns, or None when the line only mentions it.

    Args:
        tail: The rest of the line, starting immediately after the directive.
        flag: Whether the directive was the command-line form. Only a flag
            takes its value as the next whitespace-separated word; requiring
            that of a key or an environment variable read the "to" in
            "unset UV_PUBLISH_URL to publish to PyPI" as a hostname.

    Returns:
        The assigned token — a URL, a dotted hostname, or a variable
        reference — or None when nothing index-shaped is assigned, which is
        what prose looks like.

    Note:
        A single-label value (`--trusted-host mirror`, no dot) reads as an
        ordinary word and is not reported. Requiring a dot is what keeps the
        scan off English; a private host named without one escapes this
        check, and `uv.lock` staying ignored is the backstop.
    """
    patterns = (_ASSIGNED, _FLAG_VALUE) if flag else (_ASSIGNED,)
    value: str | None = None
    for pattern in patterns:
        match = pattern.match(tail)
        if match:
            value = match.group("value")
            break
    if value is None and tail.startswith(("http://", "https://")):
        value = tail.split(maxsplit=1)[0]
    if value is None:
        return None
    looks_indexed = "://" in value or "." in value or _VARIABLE.match(value)
    return value if looks_indexed else None


def scan_text(path: str, text: str) -> list[Finding]:
    """Report every index directive in `text` that does not name a public index.

    Args:
        path: Repo-relative path, used only to label findings.
        text: The file's contents.

    Returns:
        One `Finding` per offending line, in file order.
    """
    findings: list[Finding] = []
    in_index_table = False

    for number, line in enumerate(text.splitlines(), start=1):
        if _INDEX_TABLE.match(line):
            in_index_table = True
            continue
        if in_index_table:
            if _TABLE_HEADER.match(line):
                in_index_table = False
            else:
                findings.extend(_index_table_finding(path, number, line))
                continue
        findings.extend(_directive_finding(path, number, line))

    return findings


def _index_table_finding(path: str, number: int, line: str) -> list[Finding]:
    """A `url = …` line inside a uv index table that names a non-public index."""
    url_match = _URL_KEY.match(line)
    if not url_match:
        return []
    hosts = hosts_in(url_match.group("url"))
    if hosts and all(is_public(h) for h in hosts):
        return []
    return [Finding(path, number, "uv index table names a non-public index", line)]


def _directive_finding(path: str, number: int, line: str) -> list[Finding]:
    """An index directive on `line` that names a non-public host, or none it can read."""
    match = _DIRECTIVE.search(line)
    if not match:
        return []

    # A non-public URL is a finding wherever it appears, prose included:
    # "install from https://mirror…/simple" leaks the same host a config
    # key would.
    private = [h for h in hosts_in(line) if not is_public(h)]
    if private:
        return [Finding(path, number, f"index directive names a non-public host: {', '.join(private)}", line)]

    if _COMMENT.match(line):
        return []

    value = directive_value(line[match.end() :], flag=match.group("name").startswith("-"))
    if value is None or hosts_in(value):
        # Nothing assigned (prose), or a public URL already cleared above.
        return []
    if _VARIABLE.match(value):
        return [Finding(path, number, "index directive with no readable host — cannot be verified", line)]
    if not is_public(value):
        return [Finding(path, number, f"index directive names a non-public host: {value}", line)]
    return []


def _git(root: Path, *args: str, check: bool) -> subprocess.CompletedProcess[str]:
    """Run git in *root*, with git resolved from PATH to a full path."""
    git = shutil.which("git")
    if git is None:
        sys.stderr.write("check_indexes: git is not on PATH\n")
        raise SystemExit(2)
    # S603 asks for a literal argument list; the executable is a resolved path
    # and the arguments are this module's own constants.
    return subprocess.run([git, *args], cwd=root, capture_output=True, text=True, check=check)  # noqa: S603


def tracked_files(root: Path) -> list[str]:
    """Repo-relative paths of every tracked file, via `git ls-files`."""
    out = _git(root, "ls-files", "-z", check=True)
    return [p for p in out.stdout.split("\0") if p]


def _read_text(path: Path) -> str | None:
    """File contents, or None when it is binary or too large to scan."""
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _lock_is_ignored(root: Path) -> bool:
    """Whether `uv.lock` would be refused by `.gitignore`."""
    return _git(root, "check-ignore", "-q", "uv.lock", check=False).returncode == 0


def structural_findings(paths: Sequence[str], *, lock_ignored: bool) -> list[Finding]:
    """The two findings that come from the tree's shape rather than its text.

    Split out from `survey` so it can be tested without building a git
    repository per case: the inputs are a path list and a boolean, and a test
    that has to `git init` to check a naming rule is a test measuring git.

    Args:
        paths: Repo-relative paths of every tracked file.
        lock_ignored: Whether `.gitignore` would refuse `uv.lock`.

    Returns:
        A `Finding` per tracked credential-bearing file, plus one if the
        lockfile is committable.
    """
    findings = [
        Finding(rel, 0, "file records an index or its credentials and must not be tracked", rel)
        for rel in paths
        if Path(rel).name in CREDENTIAL_BEARING_FILES
    ]
    if not lock_ignored:
        findings.append(
            Finding(
                ".gitignore",
                0,
                "uv.lock is not ignored — a resolve would commit whichever index produced it",
                "uv.lock",
            )
        )
    return findings


def survey(root: Path) -> list[Finding]:
    """Every finding across the tracked tree, plus the two structural checks."""
    paths = tracked_files(root)
    findings = structural_findings(paths, lock_ignored=_lock_is_ignored(root))

    for rel in paths:
        if rel in SELF_EXEMPT:
            continue
        text = _read_text(root / rel)
        if text is not None:
            findings.extend(scan_text(rel, text))

    return findings


def main() -> int:
    """Survey the tracked files and report; with ``--check``, exit 1 on any finding.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Exit 1 if anything was found.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    findings = survey(root)
    out = sys.stdout

    if not findings:
        out.write(f"index check: clean — {len(tracked_files(root))} tracked files, no non-public index named\n")
        return 0

    out.write(f"index check: {len(findings)} finding(s)\n\n")
    for finding in findings:
        out.write(f"{finding.render()}\n")
    out.write(
        "\nOnly public indexes may be named in this repository "
        f"({', '.join(sorted(PUBLIC_INDEX_HOSTS))}). A private mirror belongs "
        "in your local environment — uv reads UV_INDEX / UV_DEFAULT_INDEX from "
        "the shell, and nothing there is committed.\n"
    )
    return 1 if args.check else 0


if __name__ == "__main__":
    sys.exit(main())

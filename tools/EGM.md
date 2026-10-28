# tools

## Overview

`tools/` holds four stdlib-only Python scripts that `just ci` runs as gates.
Each script prints its findings and exits 0 by default, and exits 1 on any
finding under `--check`. The justfile runs each one as
`uv run python tools/<script> --check`, and the `check` recipe runs all four
with `lint`, `checkfmt` and `typecheck`.

## Interface

- `check_docstrings.py` (GH-9, recipe `check-docstrings`): scans
  `src/statcatlib/**/*.py` for Sphinx roles, RST literal blocks (`Example::`,
  `Note::`, …), NumPy section rules, and `Parameters:` / `Arguments:` /
  `Params:` headers, then runs `uv run pydoclint` with the `pyproject.toml`
  config and reports its findings. Functions the tests import: `rst_markup`,
  `numpy_sections`, `argument_section_names`, `pydoclint_findings`, `ARMS`.
- `check_indexes.py` (recipe `check-indexes`): scans git-tracked files for
  package-index directives (`--index-url`, `UV_INDEX*`, `PIP_INDEX_URL`,
  `[[tool.uv.index]]` `url=`, …) that name a host outside the public PyPI
  allow-list or use an uncheckable variable. It skips files over
  `_MAX_BYTES` (4,000,000), files with a NUL byte in the first 8192 bytes,
  and files that raise `OSError` on read. Also flags tracked `.netrc`,
  `.pypirc`, `pip.conf`, `pip.ini` or `uv.lock`, and a `uv.lock` that is not
  gitignored. Functions the tests import: `is_public`, `hosts_in`,
  `directive_value`, `scan_text`, `tracked_files`, `structural_findings`,
  `survey`, `Finding`, `CREDENTIAL_BEARING_FILES`, `SELF_EXEMPT`.
- `check_prose.py` (GH-28, recipe `check-prose`): flags Markdown headings ending
  in `?` and the 10 banned phrase regexes in `PHRASES`. Reads `docs/`,
  `README.md`, `CLAUDE.md`, `llms.txt`, and the `.py` / `.toml` files under
  `src/`, `tests/`, `tools/` plus `justfile`, `pyproject.toml`, `ruff.toml` and
  `pyrefly.toml`. Markdown lines inside code fences are skipped by both
  checks. Public names: `find`, `MARKDOWN`, `CODE`, `PHRASES`.
- `count_any.py` (recipe `check-any`): counts whole-word `Any` in `src/**/*.py`,
  excluding comments, docstrings and import lines, and including uses of
  module-level aliases built from `Any`. Compares the total with
  `any_baseline.txt`. Flags: `--check`, `--write`; with both, `--write` wins
  and the run exits 0. Public functions: `alias_names`, `count_file`,
  `survey`.
- `any_baseline.txt`: the `Any` ratchet baseline (currently `7`) after a
  3-line comment header.

## Invariants

- Every script resolves the repo root as `Path(__file__).resolve().parent.parent`,
  so results do not depend on the working directory.
- `check_docstrings.py` and `check_indexes.py` exit 2 when their external tool
  cannot run (`uv` missing or pydoclint exiting other than 0 or 1; `git`
  missing). A failing `git ls-files` (run with `check=True`) ends
  `check_indexes.py` with a traceback.
- `count_any.py --check` is a two-way ratchet: the total must equal the
  baseline. A fall also fails until `--write` records the new count, and a
  missing baseline fails with an error on stderr. The module docstring and
  the `--check` help still describe a one-way check ("fail if above
  baseline").
- `check_indexes.py` uses an allow-list (`PUBLIC_INDEX_HOSTS`: `pypi.org`,
  `www.pypi.org`, `files.pythonhosted.org`, `test.pypi.org`, `upload.pypi.org`)
  and never names a private host. A directive counts only when a value is
  assigned to it, and a single-label value with no dot (`--trusted-host
  mirror`) is not reported. It reports a non-public URL on a directive line
  even inside a comment.
- `check_prose.py` exempts itself and the `## Writing style` section of
  `CLAUDE.md`, which quotes the banned phrases. `CHANGELOG.md` and `design/`
  are left out as the historical record.
- `check_indexes.py` exempts itself and `tests/test_check_indexes.py`.
- `check_docstrings.py` parses pydoclint's stderr together with stdout, since
  pydoclint writes findings to stderr.

## Side Effects

- `count_any.py --write` overwrites `tools/any_baseline.txt`. No justfile recipe
  passes `--write`. No other script writes a file.
- `check_docstrings.py` runs `uv run pydoclint` as a subprocess, which may sync
  the uv environment.
- `check_indexes.py` runs `git ls-files -z` and `git check-ignore -q uv.lock`,
  both read-only.
- Findings go to stdout; tool-failure messages and `count_any.py --check` errors
  go to stderr.

## Verification

- Run: `just check` (all four scripts), or `just ci` for the full gate.

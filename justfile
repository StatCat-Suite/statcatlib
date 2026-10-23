# statcatlib development tasks

# A gitignored `.env` sets per-machine variables: `UV_PUBLISH_TOKEN` for
# `release-5-pypi`, chiefly. Loaded, never printed.
set dotenv-load

default:
    @just --list

# ── Dev ───────────────────────────────────────────────────────────────────────

[group('dev')]
lint:
    uv run ruff check src tests tools

# pyrefly at `preset = "all"` minus a documented deny-list (pyrefly.toml),
# the only type checker (GH-7).
# Type-check src and tests with pyrefly
[group('dev')]
typecheck:
    uv run pyrefly check

[group('dev')]
checkfmt:
    uv run ruff format --check src tests tools

# Four arms, in tools/check_docstrings.py and tested in
# tests/test_check_docstrings.py (GH-9): no Sphinx/RST markup, no NumPy
# section underlines, `Args:` as the only argument-section name, and pydoclint
# in full with [tool.pydoclint]'s settings.
# Guard: docstrings are plain Google style
[group('dev')]
check-docstrings:
    uv run python tools/check_docstrings.py --check

# Fail on the two Writing style rules a pattern can check exactly (GH-28): a
# Markdown heading that is a question, and a fixed list of colon reveals and
# hook clauses. The rest of the rules need a reader; see CLAUDE.md.
[group('dev')]
check-prose:
    uv run python tools/check_prose.py --check

# Guard: `Any` in src/ must not grow. ANN401 cannot see inside a
# generic argument, so this ratchet is what guards `dict[str, Any]`.
[group('dev')]
check-any:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run python tools/count_any.py --check

# Only public PyPI may be named in this repository. A contributor's machine
# may resolve through a private package mirror, and a lockfile, an
# `index-url`, or a stray `.netrc` would publish that mirror's address to
# every consumer of a public library. The guard is
# an allow-list rather than a search for the mirror's hostname: writing that
# hostname into the gate would itself be the leak. `uv.lock` is gitignored
# for the same reason — statcatlib is a library, consumers resolve pyproject.toml.
[group('dev')]
check-indexes:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run python tools/check_indexes.py --check

# index guard + lint + format + docstrings + Any-ratchet + type check
[group('dev')]
check: check-indexes lint checkfmt check-docstrings check-prose check-any typecheck

# Install the pre-push hook that runs `just ci` (hosted CI only builds docs)
[group('dev')]
hooks-install:
    #!/usr/bin/env bash
    set -euo pipefail
    git config core.hooksPath .githooks
    echo "core.hooksPath -> .githooks"
    echo "'just ci' now runs on every 'git push'. Bypass with 'git push --no-verify'."

# Format code
[group('dev')]
fmt:
    uv run ruff format src tests tools
    uv run ruff check --fix src tests tools

# ── Test ──────────────────────────────────────────────────────────────────────

# Run unit tests
[group('test')]
test:
    uv run pytest -m unit

# Run the gate's tests with coverage (unit + integration, as `just ci` selects).
# Fails under the `fail_under` floor in pyproject.toml.
[group('test')]
coverage:
    uv run pytest -m "unit or integration" --cov --cov-report=term-missing --cov-report=xml --cov-report=html
    @echo "HTML report: htmlcov/index.html"

# Run integration tests (in-process DuckDB + sdmxlib, no network)
[group('test')]
test-integration:
    uv run pytest -m integration

# Run live tests against real SDMX REST endpoints (requires network)
[group('test')]
test-live:
    uv run pytest -m live

# Full CI gate: index guard + lint + format + typecheck + unit and
# integration tests + docs. Enforced locally by the pre-push hook — see
# `just hooks-install`. Only `live` tests, which need the network, stay out.
[group('test')]
ci: check test test-integration docs-build

# ── Docs ──────────────────────────────────────────────────────────────────────

# Serve MkDocs dev server (hot-reload)
[group('docs')]
docs:
    uv run mkdocs serve

# Build static MkDocs site. --strict so broken links and pages missing from
# the nav fail the build: this is the docs check, now that hosted CI no longer
# rebuilds the site on every merge request.
[group('docs')]
docs-build:
    uv run mkdocs build --strict

# ── Release ───────────────────────────────────────────────────────────────────

# Step 1 — Preview next version and unreleased changelog (no files changed)
[group('release')]
release-1-preview:
    @echo "Next version: $(git-cliff --bumped-version)"
    @echo ""
    @git-cliff --bump --unreleased

# Step 2 — Start release/vX from origin/main and write CHANGELOG.md (pass VERSION to override)
[group('release')]
release-2-changelog VERSION='':
    #!/usr/bin/env bash
    # By default git-cliff determines the next version from commit messages;
    # pre-1.0, a "!" breaking change bumps the minor (cliff.toml). Pass
    # VERSION to pin anyway:
    #   just release-2-changelog              # auto
    #   just release-2-changelog 0.9.0        # pin
    #
    # Prepend the unreleased section, never regenerate the file: GitHub's
    # history starts at a single "statcatlib 0.8.0" commit, so every entry
    # before it exists only in CHANGELOG.md.
    set -euo pipefail
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "error: the working tree has changes; commit or stash them first" >&2
        exit 1
    fi
    git fetch origin --tags
    git switch --detach origin/main
    OVERRIDE="{{ VERSION }}"
    if [[ -n "${OVERRIDE}" ]]; then
        NEXT="v${OVERRIDE#v}"
        git switch -c "release/${NEXT}"
        echo "Writing changelog pinned to ${NEXT}..."
        git-cliff --unreleased --tag "${NEXT}" --prepend CHANGELOG.md
    else
        NEXT=$(git-cliff --bumped-version)
        git switch -c "release/${NEXT}"
        echo "Writing changelog for ${NEXT}..."
        git-cliff --unreleased --bump --prepend CHANGELOG.md
    fi
    echo ""
    echo "Review CHANGELOG.md, then run: just release-3-pr"

# Step 3 — Bump the version, commit, push release/vX and open its PR
[group('release')]
release-3-pr:
    #!/usr/bin/env bash
    # The version comes from the branch release-2-changelog started, so a
    # pinned version is given once.
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
        echo "error: not on a release/vX branch (run just release-2-changelog)" >&2
        exit 1
    fi
    VER="${BRANCH#release/}"
    uv version "${VER#v}"
    git add CHANGELOG.md pyproject.toml
    git commit -m "chore(release): ${VER}"
    git push -u origin "${BRANCH}"
    NOTES=$(just release-notes "${VER}")
    gh pr create --base main --head "${BRANCH}" --title "chore(release): ${VER}" --body "${NOTES}"
    echo "Run just ci on this branch; when it is green, run: just release-4-merge"

# Step 4 — Merge the release PR, tag its merge commit, mint the GitHub release
[group('release')]
release-4-merge:
    #!/usr/bin/env bash
    # Run from the release/vX branch, only after a green `just ci` on it:
    # hosted CI runs no tests, so nothing else stands between this and main.
    set -euo pipefail
    BRANCH=$(git branch --show-current)
    if [[ "${BRANCH}" != release/v* ]]; then
        echo "error: not on a release/vX branch" >&2
        exit 1
    fi
    VER="${BRANCH#release/}"
    if [[ "$(gh pr view "${BRANCH}" --json state --jq .state)" != MERGED ]]; then
        gh pr merge "${BRANCH}" --merge
    fi
    # `gh pr merge` has exited 0 without merging; trust the state, not the exit.
    STATE=$(gh pr view "${BRANCH}" --json state --jq .state)
    if [[ "${STATE}" != MERGED ]]; then
        echo "error: the ${BRANCH} PR is ${STATE}, not merged" >&2
        exit 1
    fi
    MERGE=$(gh pr view "${BRANCH}" --json mergeCommit --jq .mergeCommit.oid)
    git fetch origin
    git switch main
    git merge --ff-only origin/main
    git tag -a "${VER}" -m "${VER}" "${MERGE}"
    git push origin "${VER}"
    NOTES=$(just release-notes "${VER}")
    gh release create "${VER}" --verify-tag --title "${VER}" --notes "${NOTES}"
    git branch -D "${BRANCH}"
    # The repo deletes a merged PR's branch itself ("delete branch on merge"),
    # so the remote one is usually gone already. Deleting it regardless failed
    # this step after the tag and release were made (0.10.0).
    if git ls-remote --exit-code --heads origin "${BRANCH}" >/dev/null; then
        git push origin --delete "${BRANCH}"
    fi
    git switch --detach "${VER}"
    echo "Released ${VER} on GitHub; checked out at the tag"
    echo "Next: just release-5-pypi"

# Steps 3 and 4 take their text from here, not from git-cliff's regeneration
# from commits, which would drop what was written by hand after step 2.
# Print a version's CHANGELOG.md section, as edited
[group('release')]
release-notes VERSION:
    #!/usr/bin/env bash
    set -euo pipefail
    VER="{{ VERSION }}"
    NOTES=$(awk -v head="## [${VER#v}]" 'index($0, head) == 1 {f = 1; next} /^## \[/ {f = 0} f' CHANGELOG.md | sed '/./,$!d')
    if [[ -z "${NOTES}" ]]; then
        echo "error: CHANGELOG.md has no section for ${VER#v}" >&2
        exit 1
    fi
    printf '%s\n' "${NOTES}"

# Step 5 — Build and publish to PyPI (UV_PUBLISH_TOKEN, from the environment or .env)
[group('release')]
release-5-pypi:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
        echo "error: UV_PUBLISH_TOKEN is not set" >&2
        exit 1
    fi
    # Build from a checked-out tag, never from a branch that has moved past it.
    if ! VER=$(git describe --tags --exact-match HEAD 2>/dev/null); then
        echo "error: HEAD is not a release tag; run git switch --detach vX.Y.Z" >&2
        exit 1
    fi
    # `uv publish` defaults to public PyPI, but a shell configured for a
    # private mirror redirects it silently. Check the target by allow-list,
    # for the same reason `just check-indexes` does.
    ALLOWED="upload.pypi.org test.pypi.org"
    TARGET="${UV_PUBLISH_URL:-https://upload.pypi.org/legacy/}"
    HOST=$(printf '%s' "${TARGET}" | sed -E 's#^[a-z]+://([^/]+).*#\1#')
    if ! grep -qw -- "${HOST}" <<< "${ALLOWED}"; then
        echo "error: publish target is not a public index (${HOST})" >&2
        echo "       unset UV_PUBLISH_URL to publish to PyPI." >&2
        exit 1
    fi
    rm -rf dist
    uv build
    uv publish
    echo "Published ${VER} to PyPI"

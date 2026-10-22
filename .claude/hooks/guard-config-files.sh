#!/usr/bin/env bash
# PreToolUse/Bash guard for the two edit-guarded checker configs.
#
# permissions.ask only covers the file-editing tools; a Bash command can
# rewrite ruff.toml or pyrefly.toml without matching any permission rule.
# This closes that path for the mutation shapes a shell command can take.
#
# Read-only mentions (cat, grep, sed -n, git diff) pass through silently.
set -euo pipefail

cmd=$(jq -r '.tool_input.command // ""')

GUARDED='(ruff|pyrefly)\.toml'
grep -qE "$GUARDED" <<<"$cmd" || exit 0

# Anything that can write: redirection into the file, in-place editors,
# file movers, patch application, or an interpreter that could write it.
MUTATES='(>>?[[:space:]]*"?'"'"'?\.?/?(ruff|pyrefly)\.toml'
MUTATES+='|sed[[:space:]]+[^|;&]*-i'
MUTATES+='|\b(tee|mv|cp|rm|truncate|dd|patch|install|ln|ed)\b'
MUTATES+='|git[[:space:]]+(apply|checkout|restore|reset|stash)'
MUTATES+='|\b(python|python3|uv|uvx|perl|awk)\b'
MUTATES+='|<<)'
grep -qE "$MUTATES" <<<"$cmd" || exit 0

jq -nc '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: "This command can modify ruff.toml or pyrefly.toml. Project rule: the checker configs are never changed to make an error go away. Fix the underlying defect, or ask for an explicit sign-off on the config change."
  }
}'

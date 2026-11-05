#!/usr/bin/env bash
# Claude Code hook (PostToolUse on Edit|Write): format and lint a Python file right after
# an AI tool changed it, so generated code always follows the project style.
#
# Input: the tool call as JSON on stdin. Exit 0 = fine. Exit 2 = ruff found problems it
# cannot fix itself; the message goes back to the agent, which then fixes them.
set -euo pipefail

file=$(jq -r '.tool_input.file_path // empty')
[[ "$file" == *.py ]] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}"
# Lint fixes first (they can remove lines, such as an unused import), then formatting.
problems=$(uv run --quiet ruff check --fix --quiet "$file" 2>&1) || failed=1
uv run --quiet ruff format --quiet "$file"
if [[ -n "${failed:-}" ]]; then
  echo "ruff found problems it cannot fix automatically in $file:" >&2
  echo "$problems" >&2
  exit 2
fi

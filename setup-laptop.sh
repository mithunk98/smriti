#!/usr/bin/env bash
# Connects this Mac/Linux laptop's Claude Code to Smriti (Windows: use setup-laptop.ps1).
# Run with:  bash setup-laptop.sh   and paste your existing Smriti token when asked.
set -euo pipefail

URL="https://smriti-k3zk.onrender.com/mcp"

if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code is not installed (or not on PATH). Install it with:"
  echo "  curl -fsSL https://claude.ai/install.sh | bash"
  echo "then open a new Terminal window and run this script again."
  exit 1
fi

read -rsp "Paste your Smriti token (hidden while you type/paste), then press Enter: " TOKEN
echo
if [ -z "$TOKEN" ]; then
  echo "No token entered. Use the same token as your other laptop (it is SMRITI_TOKEN on Render)."
  exit 1
fi

# Replace any earlier smriti entry. Output is hidden because it echoes the token.
claude mcp remove smriti --scope user >/dev/null 2>&1 || true
if ! claude mcp add --transport http --scope user smriti "$URL" --header "Authorization: Bearer $TOKEN" >/dev/null 2>&1; then
  echo "Could not add smriti to Claude Code. Check that 'claude --version' works."
  exit 1
fi
echo "Smriti added to Claude Code."

mkdir -p "$HOME/.claude"
MD="$HOME/.claude/CLAUDE.md"
touch "$MD"

# Each block is added only if its marker is missing, so re-running is safe.
add_block() {
  if grep -q "$1" "$MD"; then
    echo "CLAUDE.md already has: $1"
  else
    printf '%s\n' "$2" >> "$MD"
    echo "Added to CLAUDE.md: $1"
  fi
}

add_block "Smriti (my personal memory)" '
## Smriti (my personal memory)
- Before answering questions about my life, plans, decisions or preferences, call `recall`.
- When I share a decision, deadline, goal or important fact, save it with `remember` (add short tags).
- When I say "weekly review", call `recent` with days=7 and summarize what I did vs. what I planned.'

add_block "list_tasks" '
## Smriti tasks
- When I mention something I have to do, add it with `add_task` (convert dates like "next Friday" to YYYY-MM-DD).
- When I ask what to do, plan my day or week, or start a work session, call `list_tasks` and point out anything overdue or due soon.
- When I say I finished something, mark it with `complete_task`.'

add_block "update_memory" '- If a saved memory is wrong or outdated, fix it with `update_memory` or delete it with `forget` (ask me first if unsure) instead of saving a correction.'

add_block "morning_brief" '- When I say "brief me" or ask to plan my day, call `morning_brief`.'

echo
echo "Done. Check the connection with: claude mcp list"

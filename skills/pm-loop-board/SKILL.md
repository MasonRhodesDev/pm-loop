---
name: pm-loop-board
description: Show the pm-loop board (open PRs with checks/verdict/next action, owner asks, pending events) and the cost ledger. Read-only. Use when asked "where are we", "what's blocked", or "what did the loop cost".
user-invocable: true
allowed-tools: Bash(pm *)
effort: low
---
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm events poll >/dev/null 2>&1; "${CLAUDE_PLUGIN_ROOT}"/bin/pm board --live`

## Cost ledger (headless ticks and lane reports)
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm ledger`

Answer from the tables above; do not run anything else.

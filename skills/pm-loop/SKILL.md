---
name: pm-loop
description: Run one PM tick of the pm-loop workflow interactively — board, queue and doctrine injected; decide, act with `pm` scripts and the gh-bot MCP tools, stop. Use when asked to "run the PM loop", "process the queue", or "what should merge".
user-invocable: true
argument-hint: "[owner/repo ...] [--force]"
allowed-tools: Bash(pm *), Bash(gh pr *), Bash(gh issue *), Bash(gh run *), Bash(git *), Agent, mcp__plugin_pm-loop_gh-bot__*
effort: medium
---
# pm-loop tick

Arguments: `$ARGUMENTS` (optional repos to restrict to; `--force` runs even with an empty queue).

## Effective config
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm config roles`

## Board (this tick already polled GitHub just now, below, so it never trusts a stale snapshot)
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm events poll >/dev/null 2>&1; "${CLAUDE_PLUGIN_ROOT}"/bin/pm board`

## Doctrine for this tick
!`"${CLAUDE_PLUGIN_ROOT}"/bin/pm brief pm | sed -n '/## Doctrine/,/## Open PRs/p' | head -n -1`

## How to act
- `pm premerge <repo> <pr>` then `pm merge <repo> <pr> --subject "..." --body "..."` for MERGE CANDIDATE rows.
- `pm classify <repo> <pr>` → `pm brief reviewer <repo> <pr>` → spawn a **fresh** `reviewer-light` or `reviewer-adversarial` agent with that brief as its prompt. Never resume an old reviewer.
- `pm brief dev <repo> <issue>` → spawn a `dev-lane` agent with the brief. One agent per issue.
- After each merge: `pm record <repo> <pr>` — entry, factcheck, and a comment on the merged PR in one step; it refuses to post if factcheck fails. The record goes with the change; no STATUS PR is opened whose only change is a record.
- If a gh-bot tool errors, use the equivalent `mason-agent` tool or `gh-agent` on the CLI instead — never fall back to plain `gh` for a write.
- Owner decisions: open/label `needs-owner` with a numbered CTA. Then stop.
- When nothing is actionable, say "nothing actionable" and stop. The poll above already ran once for this tick — never poll again, wait, or self-schedule; the next tick is the operator re-running this skill (by hand, or via Claude Code's own `/loop`) or the timer, if installed.
- Finish with `pm events clear` so processed events are not replayed.

# pm-loop

Event-driven, script-first PM loop for GitHub repos worked by Claude Code agents. A Claude Code plugin.

- `pm` CLI: poll/queue, board, classify, review-needed, premerge, merge, status-entry, factcheck, watch, brief, run, ledger, llm, git-env
- skills: `/pm-loop` (one interactive tick), `/pm-loop-board`, `/pm-loop-tune`
- agents: `dev-lane`, `reviewer-light`, `reviewer-adversarial`, `spike` (model/effort/turn caps; no nested agents)
- hooks: lane guard (no sub-agents, no background jobs, no CI polling), per-role tool-call cap, stall check
- MCP server `gh-bot`: every GitHub write as a GitHub App with a role line; merge gated by CLEAR+green
- deploy: systemd timer for the headless tick; Nomad job for a local llama.cpp model (prose only)

See `docs/DESIGN.md` for what each part replaces and how it is used.

## Quick start
```
cp config/config.example.toml ~/.config/pm-loop/config.toml   # bot app id/key, repos, models, llm endpoint
export PATH=$PWD/bin:$PATH
pm events poll && pm board            # seed the poller, see the board
pm premerge OWNER/REPO 123            # checklist
pm run --dry --force                  # the brief one headless tick would send
python3 -m unittest tests/test_pmloop.py
```
Enable the plugin: `/plugin marketplace add <this repo>` → `/plugin install pm-loop@mason-tools`. Headless: `deploy/systemd/*` (`systemctl --user enable --now pm-loop.timer`).

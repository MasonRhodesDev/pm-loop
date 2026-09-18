# pm-loop

Event-driven, script-first PM loop for GitHub repos worked by Claude Code agents. A Claude Code plugin.

- `pm` CLI: poll/queue, board, classify, review-needed, premerge, merge, status-entry, record, factcheck, watch, brief, run, ledger, llm, git-env
- skills: `/pm-loop` (one interactive tick), `/pm-loop-board`, `/pm-loop-tune`
- agents: `dev-lane`, `reviewer-light`, `reviewer-adversarial`, `spike` (model/effort/turn caps; no nested agents)
- hooks: lane guard (no sub-agents, no background jobs, no CI polling), per-role tool-call cap, stall check
- MCP server `gh-bot`: every GitHub write as a GitHub App with a role line; merge gated by CLEAR+green
- deploy: systemd timer for the headless tick, optional webhook receiver to keep the queue fresh between runs; Nomad job for a local llama.cpp model (prose only)

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
Enable the plugin: `/plugin marketplace add <this repo>` → `/plugin install pm-loop@mason-tools`. Two supported ways to trigger a tick, no bespoke poller needed: unattended via `deploy/systemd/*` (`systemctl --user enable --now pm-loop.timer`), or interactively by running `/pm-loop` yourself — by hand, or on a cadence with Claude Code's own `/loop` (e.g. `/loop 10m /pm-loop`). `pm events serve` (the webhook receiver) only keeps the queue fresh between runs; it never triggers a tick on its own.

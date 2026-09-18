# pm-loop — design

The Sep 2026 post-mortem (`~/.claude/reports/token-postmortem-2026-09-16/`) showed a $5.8K list-equivalent
loop where 70% of spend was context re-reads: a resident 500K-token PM session woken 1,574 times by
notifications and polls, agents resumed with their whole history, and ~10.9K tool calls that only asked
GitHub a question. pm-loop keeps the process (adversarial review, CLEAR-plus-green merges, STATUS
reconciliation, owner asks) and moves every mechanical part out of the model.

## Principle

**A model is invoked only with a small brief when there is something to decide.** Everything else —
noticing change, waiting for CI, checking a PR against the merge rules, writing the STATUS record,
verifying citations, choosing a review tier, capping an agent — is a script with an exit code.

## Parts, and how each is developed and used

| # | Post-mortem line it attacks | Part | Development | Use |
|---|---|---|---|---|
| 1 | 5,509 PM calls × 500K re-read ($1,421) | **Stateless PM tick** — `pm run`: `pm events poll` → queue → `pm board` + doctrine → `claude -p --max-turns --output-format json` with the plugin loaded; cost goes to the ledger, events are marked processed. `run.tick()` always polls first, whatever invoked it, so a tick never trusts a stale board. | `pmloop/run.py`, `brief.pm()`; systemd timer `deploy/systemd/pm-loop.timer` (2 min). | Unattended: timer. Interactive: `/pm-loop` (same brief, injected with `!` shell blocks) — run by hand, or on a cadence via Claude Code's own `/loop` (e.g. `/loop 10m /pm-loop`). Either way pm-loop never schedules its own wakeup; `pm events serve` (webhook) only keeps the queue fresh in between, it does not trigger a tick. |
| 2 | 2,900 CI-watch turns | **Watcher as a process** — `pm watch pr <repo> <n>` / `pm watch run <repo> <id>`: bounded wall clock, one queue event, exit code. Lanes may call it once; the poller sees check conclusions anyway. | `pmloop/watch.py`. | Lanes (`pm watch … --timeout 1800`); the PM never calls it. |
| 3 | 983 gh reads + 118 merge turns + conflict rounds | **Scripted checklist + merge** — `pm premerge` (open/base/tip==remote/verdict-at-tip/checks/mergeable/role line/closing keywords/trailers) → `pm merge` re-runs the checklist itself and refuses (no PUT) unless it reports `ok`, so a stale `pm premerge` run can't be used to skip the gate; a `mergeStateStatus` of `UNKNOWN` is polled a bounded number of times (`merge.mergeable_poll_attempts`/`_delay_s`) before either row decides, never taken as a hard fail or a pass; `BLOCKED` gets the same bounded retry only when review + checks already read green elsewhere in the checklist (issue #20 — GitHub's cached mergeability can lag reality that way too), so a real block (missing review, admin restriction) still fails outright. `--force-premerge-ok` overrides a failing checklist for a human only and is logged to the ledger. | `pmloop/premerge.py`, mirrors the MCP `merge` tool's rule. | PM: `pm premerge` then `pm merge`. Never `gh pr merge`, never `--force-premerge-ok` from a script. |
| 4 | STATUS + fact-check lanes ($466, ~3,250 gh calls) | **Record from facts, posted where the change is** — `pm status-entry` renders a record from `gh pr view --json` + check-runs (+ `git show --numstat`); the heading prefers the squash commit's subject over the PR title, which can be edited after merge. One prose sentence from the local model, template fallback. **Fact-check by script** — `pm factcheck <repo> <file>` resolves every `#N`, `` `sha` ``, run id and `"quoted string"` (against `--quotes-from` sources). `pm record <repo> <pr>` runs entry → factcheck → comment on the merged PR in one step and refuses to post if factcheck is not ok — no STATUS-only PR is opened; the PR list is the board (owner ruling, issue #13). | `pmloop/status.py`, `pmloop/factcheck.py`, `pmloop/llm.py`. | PM after each merge: `pm record` — no STATUS lane, no fact-check lane, no STATUS PR. |
| 5 | 262 reviewers ($994), re-reviews of merge commits | **Review routing** — `pm classify` (path classes → tier `none/light/adversarial`, `adversarial_paths` force), `pm review-needed` (verdict at tip? only merge commits since?). Two agent definitions with model/effort/maxTurns/`disallowedTools: Agent`. | `pmloop/classify.py`, `pmloop/review.py`, `agents/reviewer-*.md`. | PM: classify → `pm brief reviewer` → spawn a fresh reviewer. Docs-only PRs get no reviewer. |
| 6 | 274 resumed agents = 78% of lane spend | **Fresh agents with briefs** — `pm brief dev|reviewer` produces a self-contained brief carrying the prior verdict URL and the diff since it. Resuming is a config choice, not the default. | `pmloop/brief.py`. | Every round is a new agent; the record (PR comments) is the memory. |
| 7 | 129 sub-subagents, 197 lane background jobs | **Hooks** — `lane-guard.py` denies `Agent`, background Bash and CI polling loops inside any subagent; `tool-cap.py` enforces `roles.<role>.max_tool_calls` per agent; `stall-check.py` flags "waiting" at SubagentStop. | `hooks/hooks.json` (PreToolUse, SubagentStop). | Automatic while the plugin is enabled; knobs in `[lanes]` and `[roles]`. |
| 8 | 41% of output was thinking; Opus on everything | **Effort/model per role** — `roles.*` in config; agent frontmatter `model`/`effort`/`maxTurns`; `dev.escalate_paths` names where Opus is worth it. | `config.py` defaults + `/pm-loop-tune`. | Tune per repo with `.pm-loop.toml`; measure with `pm ledger`. |
| 9 | (owner) local LLM on the lab, not the desktop | **llama.cpp server on Nomad** — `deploy/nomad/llm.ts` (HouseJob, node-3, CPU, Qwen2.5-7B Q4). Prose only. | Home-lab PR adds `.infra/llm.ts`; CI/CD deploys. | `llm.endpoint` in config; `pm llm "…"` to check. |

## Lanes are processes, not turns

A headless tick ends when the PM stops, and any Agent it spawned dies with it (seen on the first trial: a lane cut off mid-work).
So in headless mode the PM dispatches `pm lane dev|fix|reviewer <repo> <n>`: a detached `claude -p` under a transient systemd
unit with the role's model, effort, `--max-turns` and `--max-budget-usd`, the agent definition as system prompt, and the brief on stdin.
When it exits, the wrapper records the ledger line and appends a `lane_done` event, so the next tick sees the result.
Interactively (`/pm-loop`) the same briefs go to foreground Agent calls. First live lane: `pm lane fix diarch 548` — $3.18,
16 minutes, fixes pushed to the PR's own branch as the bot, reply posted, six tests added.

## Data flow

```
GitHub ──(poll every 2 min │ webhook)──> queue.jsonl ──> pm run ──> claude -p (brief ≈ 2–4K tokens)
                                                             │            │
                                             ledger.jsonl <──┘            ├─> pm premerge/merge, pm status-entry, pm factcheck
                                                                          └─> Agent(dev-lane | reviewer-* | spike) with `pm brief …`
```

Events: `pr_opened`, `pr_updated`, `pr_closed`, `checks_done`, `review_verdict`, `issue_labeled`, `comment`, `main_ci`, `watch_timeout`.
State: `~/.local/state/pm-loop/{queue.jsonl,processed.txt,poll-state.json,ledger.jsonl,toolcaps/}`.

## Tunables (all in TOML, layered: defaults < user < repo < env)

`roles.<role>.{model,effort,max_turns,max_tool_calls}` · `review.{classes,tier_by_class,adversarial_paths,required_verdict,rereview_ignore_merges}` ·
`merge.{method,required_check,base,forbid_trailers,closing_keywords_only_in,mergeable_poll_attempts,mergeable_poll_delay_s}` · `status.{file,prose,max_prose_words,forbid_patterns}` ·
`llm.{endpoint,model,timeout_s,max_tokens,fallback}` · `poll.interval_s` · `watch.{timeout_s,fail_fast}` · `labels.{needs_owner,no_review}` · `lanes.{deny_subagents,deny_background}` · `claude.{bin,extra_args}`.

## Distribution

The repo is a Claude Code plugin **and** a one-plugin marketplace (`.claude-plugin/marketplace.json`, name `mason-tools`).
Install: `/plugin marketplace add MasonRhodesDev/pm-loop` then `/plugin install pm-loop@mason-tools`. Private repos work over SSH.
The parts are separable: `pmloop/` + `bin/pm` (no dependencies beyond Python 3.11, `gh`, `openssl`, `curl`), `mcp/` (uv project: the GitHub-App MCP server),
`hooks/`, `agents/`, `skills/`, `deploy/`. A repo opts in with `.pm-loop.toml`; a user opts in with `~/.config/pm-loop/config.toml`.

## Expected effect (from the post-mortem numbers)

PM −85% (500K → ~3K resident tokens per tick, ticks only on events), STATUS/fact-check −95% (scripts), review −50%
(docs-only skipped, light tier first, no merge-commit re-reviews, call caps), dev −30% (no sub-agents, no polling, Sonnet default).
`pm ledger` is how the claim gets checked.

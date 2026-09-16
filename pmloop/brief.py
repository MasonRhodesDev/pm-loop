"""Briefs for fresh agents. Each brief is small, self-contained, and carries the role config so the agent
knows its model/effort/caps. Reviewer briefs include the prior verdict and the diff since it, so a new
agent replaces a resumed one."""
from __future__ import annotations
import subprocess
from . import gh, events, classify, review

RULES = """## Lane rules (enforced by hooks where possible)
- Post every GitHub record through the `pm-loop` MCP tools (role line added for you). Never plain `gh` for writes.
- Never spawn sub-agents. Never start background jobs; run measurements in the foreground with a bounded timeout.
- Never poll CI in a loop. Use `pm watch pr <repo> <n>` once (bounded) if you must wait; prefer finishing your turn.
- Commit as the bot: `eval "$(pm git-env <role>)"` before `git commit` so author and committer are both the app.
- PR bodies: role line first; write `Closes #N` only for a real closure, otherwise "for #N".
- Quote people exactly or say "paraphrase". No personal e-mail addresses on any readable surface.
- End your turn with a report, never with "waiting for …".
"""

def dev(repo: str, issue: int, cfg: dict, extra: str = "") -> str:
    role = cfg["roles"]["dev"]
    j = gh.run(["issue", "view", str(issue), "-R", repo, "--json", "title,body,labels,comments"])
    return f"""You are a dev lane for {repo} issue #{issue}. role: dev (#{issue}) · model: {role['model']} · effort: {role['effort']} · max tool calls: {role['max_tool_calls']}.

{RULES}
## Task
Implement the issue below on a branch `dev/{issue}-<slug>` from `{cfg['merge']['base']}`, with a failing-then-passing test for every property you claim.
Open ONE PR with the MCP `open_pr` tool (body: role line, what/why, evidence, "for #{issue}" or "Closes #{issue}").
Do not merge. Do not review yourself. Report: PR URL, tip SHA, tests run, anything out of scope you noticed (as a list, not fixes).
{extra}
## Issue (verbatim JSON from gh)
{j}
"""

def reviewer(repo: str, number: int, cfg: dict, tier: str | None = None, checkout: str | None = None) -> str:
    t = tier or classify.for_pr(repo, number, cfg)["tier"]
    rk = "review_adversarial" if t == "adversarial" else "review_light"; role = cfg["roles"][rk]
    need = review.needed(repo, number, cfg, checkout)
    pr = gh.pr_view(repo, number, "title,body,headRefOid,files,url")
    prior = ""
    if need.get("reviewed"):
        prior = f"\n## Prior verdict\n{need.get('prior') or need.get('verdict')} at `{need['reviewed'][:7]}` — {need.get('url')}\nReview ONLY the change since then: `git diff {need['reviewed'][:7]}...{need['tip'][:7]}` (diffstat: {need.get('diffstat')}). Re-verify the prior findings are still addressed.\n"
    stance = ("Adversarial: assume the PR is wrong; try to break each claimed property with a concrete input; run the tests; "
              "read the diff, not the description.") if t == "adversarial" else \
             ("Light: confirm the diff matches the description, tests exist for the claims and pass, no forbidden trailers/secrets; "
              "do not redesign.")
    return f"""You are a reviewer for {repo} PR #{number} — {pr['title']}. role: test · model: {role['model']} · effort: {role['effort']} · max tool calls: {role['max_tool_calls']}. Tier: {t}.

{RULES}
## Stance
{stance}
{prior}
## Deliverable
Post exactly one verdict with the MCP `review` tool: verdict CLEAR or BLOCKED, tip `{pr['headRefOid']}`, body = numbered findings with file:line and the command that demonstrates each. A finding without a reproduction is a note, not a block.
Files: {len(pr['files'])}. PR: {pr['url']}
"""

def pm(cfg: dict, board_md: str, status_tail: str, extra: str = "") -> str:  # extra: appended after the doctrine
    role = cfg["roles"]["pm"]
    return f"""You are the PM for one tick of the loop. role: pm · model: {role['model']} · effort: {role['effort']}. You have NO memory of previous ticks; everything you need is below. Decide, act with the `pm` scripts and MCP tools, and stop. Max tool calls: {role['max_tool_calls']}.

## Doctrine
- Merge only when `pm premerge` reports ok (reviewer {cfg['review']['required_verdict']} at tip AND checks green). Never merge on your own judgment. Use `pm merge`.
- Never poll or wait. If nothing is actionable, say so and stop; the next tick comes from events.
- Reviews: `pm classify <repo> <pr>` decides the tier; `pm brief reviewer <repo> <pr>` builds the brief; spawn a FRESH reviewer agent (`reviewer-light` / `reviewer-adversarial`) — never resume one.
- Dev work: `pm brief dev <repo> <issue>`; spawn a `dev-lane` agent. A BLOCKED verdict goes back to a dev lane with the verdict URL.
- After a merge: `pm status-entry <repo> <pr>` writes the STATUS entry; `pm factcheck` verifies it; open the STATUS PR with the MCP tool (label: no-review) — no STATUS lane, no fact-check lane.
- Owner decisions: never decide; label `{cfg['labels']['needs_owner']}` with a short numbered CTA ("Reply with a number").
- Everything stays private. No attribution trailers. No polling. No sub-agents of sub-agents.
{extra}
{board_md}
## STATUS tail
{status_tail}
"""

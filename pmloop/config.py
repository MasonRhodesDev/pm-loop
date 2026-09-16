"""Layered TOML config: plugin defaults < ~/.config/pm-loop/config.toml < <repo>/.pm-loop.toml < env.

Every knob the loop uses is here so the whole workflow is tunable without touching code.
"""
from __future__ import annotations
import os, tomllib, copy
from pathlib import Path

DEFAULTS: dict = {
    "repos": [],                      # ["owner/name", ...] watched by the poller
    "state_dir": "~/.local/state/pm-loop",
    "bot": {                          # GitHub App used for every write (comments, PRs, merges)
        "app_id": 0, "installation_id": 0, "key_path": "~/.config/pm-loop/app.pem", "slug": "",
    },
    "poll": {"interval_s": 120, "lookback_min": 30},
    "watch": {"timeout_s": 2400, "fail_fast": True},
    "roles": {                        # model/effort/turn caps per role; the role line carries model+effort
        "pm":         {"model": "sonnet", "effort": "medium", "max_turns": 40, "max_tool_calls": 60, "max_budget_usd": 2.0},
        "dev":        {"model": "sonnet", "effort": "high",   "max_turns": 200, "max_tool_calls": 400, "max_budget_usd": 4.0,
                       "escalate_model": "opus", "escalate_paths": []},
        "review_light":       {"model": "sonnet", "effort": "low",  "max_turns": 40,  "max_tool_calls": 60,  "max_budget_usd": 1.0},
        "review_adversarial": {"model": "opus",   "effort": "high", "max_turns": 120, "max_tool_calls": 200, "max_budget_usd": 5.0},
        "spike":      {"model": "sonnet", "effort": "high", "max_turns": 150, "max_tool_calls": 300, "max_budget_usd": 3.0},
    },
    "review": {
        "required_verdict": "CLEAR",
        "classes": {                  # path globs -> class; first match wins, order matters
            "docs":  ["*.md", "docs/**", "LICENSE"],
            "ci":    [".github/**"],
            "tests": ["**/*_test.go", "**/*.test.*", "**/testdata/**", "**/test/**"],
        },
        "tier_by_class": {"docs": "none", "ci": "light", "tests": "light", "code": "light"},
        "adversarial_paths": [],      # globs that force the adversarial tier (engine/, store/, auth/ ...)
        "rereview_ignore_merges": True,
        "bot_authors": {"dependabot[bot]": "light", "renovate[bot]": "light"},   # tier for bot-authored PRs (none|light|adversarial)
    },
    "merge": {
        "method": "squash", "required_check": "", "base": "main",
        "forbid_trailers": ["Co-Authored-By:", "Claude-Session:", "Generated with"],
        "closing_keywords_only_in": "Closes #",   # bodies may close issues only with this exact form
        "queue": True,                            # sequence merges through the merge queue file
        "dry_run": False,                         # true = `pm merge` only reports what it would merge (first live runs)
    },
    "status": {
        "file": "STATUS.md", "prose": True, "max_prose_words": 60,
        "forbid_patterns": ["@gmail.com", "@users.noreply.github.com"],
    },
    "llm": {                          # local model for prose only; OpenAI-compatible chat endpoint
        "endpoint": "", "model": "", "timeout_s": 60, "max_tokens": 200, "fallback": "template",
    },
    "labels": {"needs_owner": "needs-owner", "no_review": "no-review"},
    "lanes": {"deny_subagents": True, "deny_background": True, "guard_all_subagents": False},
    "claude": {"bin": "claude", "extra_args": [],
               "allowed_tools": ["Bash(pm *)", "Bash(gh pr *)", "Bash(gh issue *)", "Bash(gh run *)", "Bash(gh api *)", "Bash(git *)", "Agent", "mcp__plugin_pm-loop_gh-bot__*"],
               "lane_tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "mcp__plugin_pm-loop_gh-bot__*", "mcp__mason-agent__*"],
               "plan_only_tools": ["Bash(pm board*)", "Bash(pm classify*)", "Bash(pm review-needed*)", "Bash(pm premerge*)", "Bash(pm events pending*)", "Bash(pm ledger*)", "Bash(gh pr view*)", "Bash(gh issue view*)"]},
}

def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out

def _read(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text())
    except FileNotFoundError:
        return {}

def load(repo_dir: str | os.PathLike | None = None) -> dict:
    cfg = _merge(DEFAULTS, _read(Path(os.environ.get("PM_LOOP_CONFIG", "~/.config/pm-loop/config.toml")).expanduser()))
    if repo_dir:
        cfg = _merge(cfg, _read(Path(repo_dir) / ".pm-loop.toml"))
    for k, v in os.environ.items():           # PM_LOOP__llm__endpoint=... overrides one leaf
        if k.startswith("PM_LOOP__"):
            node = cfg; parts = k[len("PM_LOOP__"):].lower().split("__")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = v
    cfg["state_dir"] = str(Path(cfg["state_dir"]).expanduser())
    cfg["bot"]["key_path"] = str(Path(cfg["bot"]["key_path"]).expanduser())
    return cfg

def state_dir(cfg: dict) -> Path:
    p = Path(cfg["state_dir"]); p.mkdir(parents=True, exist_ok=True); return p

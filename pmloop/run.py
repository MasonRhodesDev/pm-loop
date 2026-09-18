"""One PM tick, headless: build the brief from the queue + board, run `claude -p`, record cost, mark events done."""
from __future__ import annotations
import json, subprocess, os, shlex
from pathlib import Path
from . import queue, board, brief, ledger, config, events

def tick(cfg: dict, checkout_dirs: dict[str, str] | None = None, dry: bool = False, force: bool = False, plan_only: bool = False) -> dict:
    events.poll(cfg)  # safe every tick (de-dupes against poll-state.json); a caller that skipped
                       # its own `pm events poll` (or only runs the webhook receiver) must not hand
                       # the brief a stale board.
    ev = queue.pending(cfg)
    if not ev and not force:
        return {"ran": False, "reason": "queue empty"}
    b = board.build(cfg); md = board.render(b)
    tail = ""
    for repo, d in (checkout_dirs or {}).items():
        p = Path(d) / cfg["status"]["file"]
        if p.exists():
            tail += f"\n### {repo}\n" + "\n".join(p.read_text().splitlines()[-25:]) + "\n"
    extra = ""
    if plan_only:
        extra = "\n## PLAN-ONLY TICK\nDo not spawn agents, merge, post, or write anything. Using only the read commands you are allowed, list what you WOULD do per PR (one line each) and stop.\n"
    prompt = brief.pm(cfg, md, tail or "(no checkout given; use `gh` to read STATUS if needed)", extra, headless=True)
    role = cfg["roles"]["pm"]; plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    tools = cfg["claude"]["plan_only_tools"] if plan_only else cfg["claude"]["allowed_tools"]
    cmd = [cfg["claude"]["bin"], "-p", "--model", role["model"], "--effort", role["effort"], "--max-turns", str(role["max_turns"]),
           "--output-format", "json", "--plugin-dir", plugin_root, "--allowedTools", ",".join(tools), *cfg["claude"].get("extra_args", [])]
    if role.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(role["max_budget_usd"])]
    if dry:
        return {"ran": False, "dry": True, "cmd": " ".join(shlex.quote(c) for c in cmd), "prompt": prompt, "events": len(ev)}
    env = {**os.environ, "PM_LOOP_TICK": "1", "PATH": plugin_root + "/bin:" + os.environ.get("PATH", "")}
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=3600, env=env)
    out = {}
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = {"raw": p.stdout[-4000:]}
    ledger.record(cfg, "pm", role["model"], out.get("usage") or {}, out.get("total_cost_usd"), f"tick events={len(ev)}" + (" plan-only" if plan_only else ""))
    if not plan_only:
        queue.mark_processed(cfg, ev)
    (config.state_dir(cfg) / "last-tick.json").write_text(json.dumps({"rc": p.returncode, "result": out.get("result"), "cost": out.get("total_cost_usd"), "events": ev}, indent=1))
    return {"ran": True, "rc": p.returncode, "cost_usd": out.get("total_cost_usd"), "result": (out.get("result") or "")[:2000], "events": len(ev)}

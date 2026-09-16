"""Detached headless lanes: each dev/fix/reviewer run is its own `claude -p` process with its own model, effort,
turn cap and dollar cap. It survives the PM tick that started it; when it exits, a queue event `lane_done`
carries its cost and last message, so the next tick sees the result without anyone waiting."""
from __future__ import annotations
import json, os, subprocess, sys, time, shutil, shlex
from pathlib import Path
from . import brief, config, queue, ledger, classify

AGENT = {"dev": "dev-lane", "fix": "dev-lane", "reviewer": None, "spike": "spike"}

def start(kind: str, repo: str, number: int, cfg: dict, checkout: str | None = None, tier: str | None = None, extra: str = "") -> dict:
    if kind == "reviewer":
        t = tier or classify.for_pr(repo, number, cfg)["tier"]
        if t == "none":
            return {"started": False, "reason": "tier none: no reviewer needed"}
        role_key = "review_adversarial" if t == "adversarial" else "review_light"; agent = "reviewer-adversarial" if t == "adversarial" else "reviewer-light"
        text = brief.reviewer(repo, number, cfg, t, checkout)
    else:
        role_key = "dev" if kind in ("dev", "fix") else kind; agent = AGENT[kind]
        text = brief.dev(repo, number, cfg, extra) if kind == "dev" else brief.fix(repo, number, cfg) if kind == "fix" else brief.dev(repo, number, cfg, extra)
    role = cfg["roles"][role_key]
    lid = f"{kind}-{repo.split('/')[-1]}-{number}-{int(time.time())}"
    d = config.state_dir(cfg) / "lanes"; d.mkdir(exist_ok=True)
    (d / f"{lid}.prompt.md").write_text(text)
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    cmd = [cfg["claude"]["bin"], "-p", "--model", role["model"], "--effort", role["effort"], "--max-turns", str(role["max_turns"]),
           "--output-format", "json", "--plugin-dir", plugin_root, "--allowedTools", ",".join(cfg["claude"]["lane_tools"]),
           "--append-system-prompt", f"You are the pm-loop agent `{agent}`: {Path(plugin_root, 'agents', agent + '.md').read_text().split('---')[-1].strip()}"]
    if role.get("max_budget_usd"):
        cmd += ["--max-budget-usd", str(role["max_budget_usd"])]
    wrapper = [sys.executable, "-m", "pmloop.lane", "--wrap", lid, kind, repo, str(number), role_key, checkout or ""]
    env = {**os.environ, "PM_LOOP_LANE_CMD": json.dumps(cmd), "PM_LOOP_AGENT_TYPE": agent, "PYTHONPATH": plugin_root, "PATH": plugin_root + "/bin:" + os.environ.get("PATH", "")}
    log = open(d / f"{lid}.log", "ab")
    if shutil.which("systemd-run"):
        p = subprocess.Popen(["systemd-run", "--user", "--collect", "--quiet", "--unit", f"pm-loop-{lid}", "--working-directory", checkout or os.getcwd(),
                              *[f"--setenv={k}={v}" for k, v in env.items() if k.startswith("PM_LOOP") or k in ("PYTHONPATH", "PATH", "HOME")], *wrapper], stdout=log, stderr=log)
        p.wait(); pid = None
    else:
        p = subprocess.Popen(wrapper, cwd=checkout or None, env=env, stdout=log, stderr=log, start_new_session=True); pid = p.pid
    (d / f"{lid}.json").write_text(json.dumps({"id": lid, "kind": kind, "repo": repo, "number": number, "role": role_key, "model": role["model"], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pid": pid, "status": "running"}))
    return {"started": True, "lane": lid, "agent": agent, "model": role["model"], "budget_usd": role.get("max_budget_usd"), "log": str(d / f"{lid}.log")}

def _wrap(lid: str, kind: str, repo: str, number: int, role_key: str, checkout: str) -> None:
    cfg = config.load(checkout or None); d = config.state_dir(cfg) / "lanes"
    cmd = json.loads(os.environ["PM_LOOP_LANE_CMD"]); prompt = (d / f"{lid}.prompt.md").read_text()
    t0 = time.time(); p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=checkout or None)
    try: out = json.loads(p.stdout)
    except json.JSONDecodeError: out = {"raw": p.stdout[-3000:], "stderr": p.stderr[-2000:]}
    meta = json.loads((d / f"{lid}.json").read_text()); meta.update(status="done", rc=p.returncode, cost_usd=out.get("total_cost_usd"), seconds=int(time.time() - t0), result=(out.get("result") or out.get("raw") or "")[-3000:])
    (d / f"{lid}.json").write_text(json.dumps(meta, indent=1))
    ledger.record(cfg, role_key, meta["model"], out.get("usage") or {}, out.get("total_cost_usd"), f"lane {lid}")
    queue.append(cfg, [{"kind": "lane_done", "repo": repo, "number": number, "lane": lid, "sha": "", "result": "ok" if p.returncode == 0 else f"rc={p.returncode}", "cost_usd": out.get("total_cost_usd"), "summary": meta["result"][-600:]}])

def status(cfg: dict) -> list[dict]:
    d = config.state_dir(cfg) / "lanes"
    return sorted([json.loads(f.read_text()) for f in d.glob("*.json")], key=lambda m: m["started"]) if d.exists() else []

if __name__ == "__main__":
    if sys.argv[1:2] == ["--wrap"]:
        _wrap(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), sys.argv[6], sys.argv[7])

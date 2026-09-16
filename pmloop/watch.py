"""One bounded wait for CI, as a process — never as LLM turns. Emits a queue event and an exit code."""
from __future__ import annotations
import subprocess, time, json
from . import gh, queue

def pr(repo: str, number: int, cfg: dict, timeout_s: int | None = None) -> dict:
    t = timeout_s or cfg["watch"]["timeout_s"]; deadline = time.time() + t
    sha = gh.pr_view(repo, number, "headRefOid")["headRefOid"]
    req = cfg["merge"].get("required_check", "")
    # gh's own watcher does the waiting; we only need the final state and a bounded wall clock
    args = ["pr", "checks", str(number), "-R", repo, "--watch", "-i", "30"]
    if cfg["watch"].get("fail_fast"):
        args.append("--fail-fast")
    try:
        subprocess.run(["gh", *args], capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        pass
    while True:
        state, bad = gh.checks_state(repo, sha, req)
        if state != "pending" or time.time() > deadline:
            break
        time.sleep(30)
    ev = {"kind": "checks_done" if state != "pending" else "watch_timeout", "repo": repo, "number": number, "sha": sha, "result": state, "bad": bad[:5]}
    queue.append(cfg, [ev])
    return ev

def run(repo: str, run_id: int, cfg: dict, timeout_s: int | None = None) -> dict:
    t = timeout_s or cfg["watch"]["timeout_s"]
    try:
        subprocess.run(["gh", "run", "watch", str(run_id), "-R", repo, "-i", "30", "--exit-status"], capture_output=True, text=True, timeout=t)
    except subprocess.TimeoutExpired:
        pass
    j = json.loads(gh.run(["run", "view", str(run_id), "-R", repo, "--json", "status,conclusion,headSha"]))
    ev = {"kind": "main_ci" if j["status"] == "completed" else "watch_timeout", "repo": repo, "run": run_id, "sha": j["headSha"], "result": j.get("conclusion") or "pending"}
    queue.append(cfg, [ev])
    return ev

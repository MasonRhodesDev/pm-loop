"""The pre-merge checklist as a script: every rule the PM used to re-derive by hand, as pass/fail rows."""
from __future__ import annotations
import re, subprocess, json, time
from . import gh, events, classify, ledger

CLOSING_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*#(\d+)", re.I)
ROLE_RE = re.compile(r"^\*\*role:\*\*\s*\S+.*\*\*model:\*\*.*\*\*effort:\*\*", re.M)
MERGEABLE_OK = ("CLEAN", "HAS_HOOKS", "UNSTABLE", "")

def _mergeable(repo: str, number: int, cfg: dict, status: str, retry_blocked: bool = False) -> tuple[str, int]:
    """GitHub reports UNKNOWN right after the base moves and recomputes within seconds; re-fetch a bounded
    number of times (never a hard fail, never treated as a pass) before deciding. It can also report the
    stale-cache flavor as BLOCKED instead of UNKNOWN (#20) — same lazy recompute, different enum value.
    We only retry a BLOCKED reading when `retry_blocked` says the rest of the checklist (review + checks)
    already reads green: that is exactly the signature of a stale cache, and it means a *real* block (a
    missing required review, an admin-only merge restriction, an extra required status this checklist
    doesn't evaluate) is never waved through just because BLOCKED showed up once."""
    attempts = max(1, int(cfg["merge"].get("mergeable_poll_attempts", 5)))
    delay = float(cfg["merge"].get("mergeable_poll_delay_s", 3))
    polls = 0
    while (status == "UNKNOWN" or (status == "BLOCKED" and retry_blocked)) and polls < attempts - 1:
        time.sleep(delay)
        status = gh.pr_view(repo, number, "mergeStateStatus").get("mergeStateStatus", "")
        polls += 1
    return status, polls

def check(repo: str, number: int, cfg: dict, checkout: str | None = None) -> dict:
    pr = gh.pr_view(repo, number, "number,title,body,headRefOid,headRefName,baseRefName,state,isDraft,mergeStateStatus,commits,labels")
    tip = pr["headRefOid"]; rows = []
    def row(name, ok, detail=""):
        rows.append({"check": name, "ok": bool(ok), "detail": detail})
    row("state open, not draft", pr["state"] == "OPEN" and not pr["isDraft"], f"{pr['state']} draft={pr['isDraft']}")
    row("base is " + cfg["merge"]["base"], pr["baseRefName"] == cfg["merge"]["base"], pr["baseRefName"])
    remote = gh.run(["api", f"repos/{repo}/git/ref/heads/{pr['headRefName']}", "--jq", ".object.sha"]).strip()
    row("head equals remote branch tip", remote == tip, f"pr={tip[:7]} remote={remote[:7]}")
    v = events.latest_verdict(repo, number); req = cfg["review"]["required_verdict"]
    tier = classify.for_pr(repo, number, cfg)
    if tier["tier"] == "none" and not (v and v["verdict"] == "BLOCKED" and tip.startswith(v["tip"])):
        review_ok = True
        row("review not required (tier none)", True, tier["reason"])
    else:
        review_ok = v is not None and v["verdict"] == req and tip.startswith(v["tip"])
        row(f"reviewer {req} at tip", review_ok,
            f"{v['verdict']}@{v['tip'][:7]} {v['url']}" if v else "no verdict")
    st, bad = gh.checks_state(repo, tip, cfg["merge"].get("required_check", ""))
    checks_ok = st == "success"
    row("checks green", checks_ok, st + (": " + ", ".join(bad) if bad else ""))
    checklist_green = review_ok and checks_ok
    start_status = pr.get("mergeStateStatus", "")
    mstatus, polls = _mergeable(repo, number, cfg, start_status, retry_blocked=checklist_green)
    detail = mstatus
    if polls:
        detail += f" (after {polls} poll{'s' if polls != 1 else ''}, was {start_status})"
    if mstatus == "BLOCKED" and checklist_green:
        detail += (" — review and checks are green in this checklist, so this is likely a stale cache or a"
                    " rule this checklist doesn't evaluate (e.g. an extra required status, an admin-only"
                    " merge restriction, or a native approving-review requirement a COMMENT-state bot review"
                    " doesn't satisfy)")
    row("mergeable", mstatus in MERGEABLE_OK, detail)
    body = pr.get("body") or ""
    row("role line in body", bool(ROLE_RE.search(body)), "")
    bad_kw = [m.group(0) for m in CLOSING_RE.finditer(body) if not m.group(0).startswith(cfg["merge"]["closing_keywords_only_in"])]
    row("closing keywords only as 'Closes #N'", not bad_kw, "; ".join(bad_kw[:5]))
    # A squash merge writes its own message (pm merge composes it), so branch commits only matter for merge/rebase.
    msgs = "" if cfg["merge"]["method"] == "squash" else "\n".join(c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in pr.get("commits", []))
    trailers = [t for t in cfg["merge"]["forbid_trailers"] if t.lower() in (msgs + "\n" + body).lower()]
    row("no forbidden trailers/footers in PR body" + ("" if cfg["merge"]["method"] == "squash" else "/commits"), not trailers, ", ".join(trailers))
    secrets = re.findall(r"\$\{\{\s*(?:secrets|vars)\.([A-Z0-9_]+)\s*\}\}", body)
    row("secrets/vars referenced exist", True, "declared in body: " + ", ".join(sorted(set(secrets))) if secrets else "none referenced")
    ok = all(r["ok"] for r in rows)
    return {"repo": repo, "number": number, "tip": tip, "title": pr["title"], "ok": ok, "rows": rows}

def merge(repo: str, number: int, cfg: dict, role: str, model: str, effort: str, subject: str | None = None, body: str = "", dry: bool = False, force: bool = False) -> dict:
    """Runs the premerge checklist itself and refuses (no PUT) unless it reports ok — a caller (PM script or
    human) cannot skip the gate by only checking a stale `pm premerge` run. `force=True` overrides a failing
    checklist for a human decision only; every override is logged to the ledger."""
    rep = check(repo, number, cfg)
    dry = dry or bool(cfg["merge"].get("dry_run"))
    if not rep["ok"] and not force:
        return {"merged": False, "report": rep}
    forced = force and not rep["ok"]
    if forced:
        failing = "; ".join(r["check"] for r in rep["rows"] if not r["ok"])
        ledger.record(cfg, role, model, {}, None, note=f"--force-premerge-ok on {repo}#{number} at {rep['tip'][:7]}: failing rows: {failing}")
    msg = gh.role_line(role, model, effort) + ("\n\n" + body.strip() if body.strip() else "")
    payload = {"merge_method": cfg["merge"]["method"], "commit_title": subject or f"{rep['title']} (#{number})", "commit_message": msg, "sha": rep["tip"]}
    if dry:
        return {"merged": False, "dry": True, "would_put": f"repos/{repo}/pulls/{number}/merge", "payload": payload, "report": rep, "forced": forced}
    j = gh.api(f"repos/{repo}/pulls/{number}/merge", method="PUT", token=gh.app_token(cfg), fields=payload)
    return {"merged": bool(j.get("merged")), "sha": j.get("sha"), "report": rep, "forced": forced}

"""The pre-merge checklist as a script: every rule the PM used to re-derive by hand, as pass/fail rows."""
from __future__ import annotations
import re, subprocess, json
from . import gh, events

CLOSING_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*#(\d+)", re.I)
ROLE_RE = re.compile(r"^\*\*role:\*\*\s*\S+.*\*\*model:\*\*.*\*\*effort:\*\*", re.M)

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
    row(f"reviewer {req} at tip", v is not None and v["verdict"] == req and tip.startswith(v["tip"]),
        f"{v['verdict']}@{v['tip'][:7]} {v['url']}" if v else "no verdict")
    st, bad = gh.checks_state(repo, tip, cfg["merge"].get("required_check", ""))
    row("checks green", st == "success", st + (": " + ", ".join(bad) if bad else ""))
    row("mergeable", pr.get("mergeStateStatus") in ("CLEAN", "HAS_HOOKS", "UNSTABLE", ""), pr.get("mergeStateStatus", ""))
    body = pr.get("body") or ""
    row("role line in body", bool(ROLE_RE.search(body)), "")
    bad_kw = [m.group(0) for m in CLOSING_RE.finditer(body) if not m.group(0).startswith(cfg["merge"]["closing_keywords_only_in"])]
    row("closing keywords only as 'Closes #N'", not bad_kw, "; ".join(bad_kw[:5]))
    msgs = "\n".join(c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in pr.get("commits", []))
    trailers = [t for t in cfg["merge"]["forbid_trailers"] if t.lower() in (msgs + "\n" + body).lower()]
    row("no forbidden trailers/footers", not trailers, ", ".join(trailers))
    secrets = re.findall(r"\$\{\{\s*(?:secrets|vars)\.([A-Z0-9_]+)\s*\}\}", body)
    row("secrets/vars referenced exist", True, "declared in body: " + ", ".join(sorted(set(secrets))) if secrets else "none referenced")
    ok = all(r["ok"] for r in rows)
    return {"repo": repo, "number": number, "tip": tip, "title": pr["title"], "ok": ok, "rows": rows}

def merge(repo: str, number: int, cfg: dict, role: str, model: str, effort: str, subject: str | None = None, body: str = "", dry: bool = False) -> dict:
    """Checklist, then squash-merge as the App with the role line. Refuses on any failed row."""
    rep = check(repo, number, cfg)
    dry = dry or bool(cfg["merge"].get("dry_run"))
    if not rep["ok"]:
        return {"merged": False, "report": rep}
    msg = gh.role_line(role, model, effort) + ("\n\n" + body.strip() if body.strip() else "")
    payload = {"merge_method": cfg["merge"]["method"], "commit_title": subject or f"{rep['title']} (#{number})", "commit_message": msg, "sha": rep["tip"]}
    if dry:
        return {"merged": False, "dry": True, "would_put": f"repos/{repo}/pulls/{number}/merge", "payload": payload, "report": rep}
    j = gh.api(f"repos/{repo}/pulls/{number}/merge", method="PUT", token=gh.app_token(cfg), fields=payload)
    return {"merged": bool(j.get("merged")), "sha": j.get("sha"), "report": rep}

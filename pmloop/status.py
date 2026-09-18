"""STATUS.md entries from facts, not from an agent re-deriving them.

The record part is generated verbatim from `gh` JSON and `git show --numstat`; the optional prose
sentence comes from the local model (or a template when it is down). Every fact the prose may cite is
in the record, so `pm factcheck` can verify the entry mechanically.
"""
from __future__ import annotations
import json, re, subprocess, time
from . import gh, llm, factcheck

def facts(repo: str, number: int, checkout: str | None = None) -> dict:
    pr = gh.pr_view(repo, number, "number,title,body,headRefOid,headRefName,baseRefName,state,mergedAt,mergeCommit,url,files,closingIssuesReferences,author,labels,additions,deletions")
    f = {"repo": repo, "number": number, "title": pr["title"], "url": pr["url"], "branch": pr["headRefName"], "base": pr["baseRefName"],
         "state": pr["state"], "merged_at": pr.get("mergedAt"), "merge_sha": (pr.get("mergeCommit") or {}).get("oid"),
         "head": pr["headRefOid"], "files": len(pr["files"]), "additions": pr.get("additions"), "deletions": pr.get("deletions"),
         "closes": [i["number"] for i in pr.get("closingIssuesReferences") or []], "labels": [l["name"] for l in pr["labels"]],
         "paths": [x["path"] for x in pr["files"]][:40], "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    m = re.search(r"\*\*role:\*\*\s*([^·\n]+?)\s*·\s*\*\*model:\*\*\s*([^·\n]+?)\s*·\s*\*\*effort:\*\*\s*(\S+)", pr.get("body") or "")
    f["role"] = {"role": m.group(1).strip(), "model": m.group(2).strip(), "effort": m.group(3).strip()} if m else None
    sha = f["merge_sha"] or f["head"]
    if sha:
        runs = gh.check_runs(repo, sha)
        concl = {}
        for r in runs:
            concl[r["conclusion"] or r["status"]] = concl.get(r["conclusion"] or r["status"], 0) + 1
        st, bad = gh.checks_state(repo, sha)
        f["checks"] = {"sha": sha, "state": st, "bad": bad, "conclusions": concl}
    if checkout and f["merge_sha"]:
        p = subprocess.run(["git", "-C", checkout, "show", "--numstat", "--format=%H %s", f["merge_sha"]], capture_output=True, text=True)
        if p.returncode == 0:
            f["numstat"] = p.stdout.strip().splitlines()[:60]
    if f["merge_sha"]:
        # A merged PR's title can be edited afterwards (a rework retitled in prose only, still naming a
        # withdrawn design — seen once). The squash commit's subject is fixed at merge time by `pm merge`
        # and is what actually landed, so it is the more trustworthy heading. Fetched from the GitHub API
        # (not the local checkout) so this holds even when the checkout hasn't fetched the merge yet.
        try:
            msg = gh.run(["api", f"repos/{repo}/commits/{f['merge_sha']}", "--jq", ".commit.message"])
            subject = re.sub(r"\s*\(#\d+\)\s*$", "", (msg.strip().splitlines() or [""])[0])
            if subject:
                f["squash_subject"] = subject
        except gh.GhError:
            pass
    return f

def _forbidden(text: str, cfg: dict) -> str | None:
    return next((pat for pat in cfg["status"]["forbid_patterns"] if pat in text), None)

def render(f: dict, cfg: dict) -> str:
    when = f["merged_at"] or f["checked_at"]
    heading = f.get("squash_subject") or f["title"]
    lines = [f"### {when} — #{f['number']} {heading}", ""]
    lines.append(f"- **PR:** {f['url']} · branch `{f['branch']}` → `{f['base']}` · state {f['state']}")
    if f["merge_sha"]:
        lines.append(f"- **merged:** `{f['merge_sha'][:7]}` at {f['merged_at']}")
    lines.append(f"- **head at merge/check:** `{f['head'][:7]}` · files {f['files']} · +{f['additions']}/−{f['deletions']}")
    if f.get("checks"):
        c = f["checks"]; raw = ", ".join(f"{k} ×{v}" for k, v in sorted(c.get("conclusions", {}).items()))
        lines.append(f"- **check-runs on `{c['sha'][:7]}`:** {raw or c['state']}" + (" — not green: " + ", ".join(c["bad"][:5]) if c["bad"] else ""))
    if f["closes"]:
        lines.append("- **for issues:** " + ", ".join(f"#{n}" for n in f["closes"]))
    if f.get("role"):
        r = f["role"]; lines.append(f"- **lane:** role {r['role']} · model {r['model']} · effort {r['effort']}")
    lines.append(f"- **verified:** {f['checked_at']} via `gh pr view`/`check-runs`")
    if cfg["status"].get("prose"):
        prose = summarize(f, cfg)
        if prose:
            lines += ["", prose]
    return "\n".join(lines) + "\n"

def summarize(f: dict, cfg: dict) -> str:
    system = ("You write one factual sentence for a project status log. Use only the facts given. "
              "Do not invent numbers, names, or outcomes. No praise, no speculation. Max %d words." % cfg["status"]["max_prose_words"])
    user = json.dumps({k: f[k] for k in ("number", "title", "paths", "closes", "state", "checks") if k in f})
    out = llm.complete(cfg, system, user)
    if out and len(out.split()) <= cfg["status"]["max_prose_words"] * 1.5 and "\n" not in out.strip():
        return out.strip()
    top = ", ".join(sorted({p.split("/")[0] for p in f["paths"]})[:4])
    return f"#{f['number']} changed {f['files']} file(s) under {top}" + (f" for {', '.join('#%d' % n for n in f['closes'])}" if f["closes"] else "") + "."

def entry(repo: str, number: int, cfg: dict, checkout: str | None = None) -> str:
    text = render(facts(repo, number, checkout), cfg)
    bad = _forbidden(text, cfg)
    if bad:
        raise ValueError(f"entry contains forbidden pattern {bad!r}")
    return text

def record(repo: str, number: int, cfg: dict, checkout: str | None, role: str, model: str, effort: str, dry: bool = False) -> dict:
    """status-entry + factcheck + comment in one step: the record goes with the change (a comment on the
    merged PR) instead of a separate STATUS-only PR. Refuses to post if the PR is not MERGED, if the
    entry carries a forbidden pattern, or if factcheck does not report ok."""
    f = facts(repo, number, checkout)
    if f["state"] != "MERGED":
        return {"ok": False, "posted": False, "reason": f"PR state is {f['state']}, not MERGED"}
    text = render(f, cfg)
    bad = _forbidden(text, cfg)
    if bad:
        return {"ok": False, "posted": False, "reason": f"entry contains forbidden pattern {bad!r}"}
    fc = factcheck.check(text, repo, cfg)
    if not fc["ok"] or dry:
        return {"ok": fc["ok"], "posted": False, "factcheck": fc, "entry": text}
    body = gh.role_line(role, model, effort) + "\n\n" + text
    j = gh.api(f"repos/{repo}/issues/{number}/comments", method="POST", token=gh.app_token(cfg), fields={"body": body})
    return {"ok": True, "posted": True, "url": j["html_url"], "id": j["id"], "factcheck": fc, "entry": text}

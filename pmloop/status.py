"""STATUS.md entries from facts, not from an agent re-deriving them.

The record part is generated verbatim from `gh` JSON and `git show --numstat`; the optional prose
sentence comes from the local model (or a template when it is down). Every fact the prose may cite is
in the record, so `pm factcheck` can verify the entry mechanically.
"""
from __future__ import annotations
import json, re, subprocess, time
from . import gh, llm

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
    return f

def render(f: dict, cfg: dict) -> str:
    when = f["merged_at"] or f["checked_at"]
    lines = [f"### {when} — #{f['number']} {f['title']}", ""]
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
    for pat in cfg["status"]["forbid_patterns"]:
        if pat in text:
            raise ValueError(f"entry contains forbidden pattern {pat!r}")
    return text

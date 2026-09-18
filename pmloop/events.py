"""Turn GitHub state changes into queue events, by polling (default) or by webhook delivery.

Event kinds: pr_opened, pr_updated (new head), pr_closed, checks_done (success|failure for a head),
review_verdict (CLEAR/BLOCKED/... comment at a tip), issue_labeled (needs-owner), main_ci (push CI on main),
comment (owner replied). Each event is small; the PM brief is built from the table, not the payload.
"""
from __future__ import annotations
import re, json, hmac, hashlib, time
from . import gh, queue, config

# Both record shapes in use: the MCP `review` tool ("## CLEAR — tip `sha`") and hand-posted comments ("**Verdict: CLEAR at sha**")
VERDICT_RE = re.compile(r"(?:^##\s*|\*\*Verdict:\s*)(CLEAR|BLOCKED|VERIFIED|DISPUTED)\s*(?:[—-]+\s*tip\s*|at\s*)`?([0-9a-f]{7,40})`?", re.M | re.I)
# The doctrine's non-closing reference form ("for #N", per the lane rules) plus every closing-keyword
# spelling: anywhere in the body, not anchored to line start, so a linked issue is found however the
# PR body phrases it. Kept here (rather than in premerge.py, which imports this module) so both
# premerge._owner_row and board.build can share one extraction + one lookup without a circular import
# (premerge -> events is fine; events -> premerge would not be).
LINKED_ISSUE_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|for)\s*:?\s*#(\d+)", re.I)

def linked_issues(body: str) -> list[int]:
    return sorted({int(n) for n in LINKED_ISSUE_RE.findall(body)})

def linked_needs_owner(repo: str, body: str, cfg: dict) -> dict:
    """Every issue the PR body links (via linked_issues), looked up and checked for `needs-owner`.
    Shared by premerge._owner_row (fails the merge checklist so `pm merge` refuses) and
    board.build (labels the PR's `next` action) so the board's displayed status never disagrees
    with what premerge will actually decide (#30) -- one regex, one API-lookup loop, two callers
    that each interpret the result for their own presentation.

    Returns {"nums": [...], "checked": [...], "flagged": [...], "errs": [...]}: `checked`/`flagged`
    are "#N title" strings for issues that were successfully looked up (flagged: also carry
    needs-owner); `errs` are "#N: <exception>" strings for references the API couldn't resolve --
    an unverifiable reference is not the same as a verified 'not owner-held' (same stance
    premerge._owner_row already takes: fail closed, never silently pass)."""
    nums = linked_issues(body)
    needs_owner = cfg["labels"]["needs_owner"]; flagged, checked, errs = [], [], []
    for n in nums:
        try:
            j = gh.api(f"repos/{repo}/issues/{n}") or {}
        except Exception as e:
            errs.append(f"#{n}: {e}"); continue
        checked.append(f"#{n} {j.get('title', '')}")
        if needs_owner in {l.get("name") for l in j.get("labels", [])}:
            flagged.append(f"#{n} {j.get('title', '')}")
    return {"nums": nums, "checked": checked, "flagged": flagged, "errs": errs}

def snapshot(repo: str, cfg: dict) -> dict:
    """Current observable state of a repo: open PRs (head, checks), verdicts, needs-owner issues, last main run."""
    prs = json.loads(gh.run(["pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                             "--json", "number,title,body,headRefOid,headRefName,baseRefName,isDraft,labels,updatedAt,mergeStateStatus"]))
    out = {"prs": {}, "issues": {}, "main": {}}
    req = config.required_check(cfg, repo)
    for pr in prs:
        n = str(pr["number"]); sha = pr["headRefOid"]
        state, bad = gh.checks_state(repo, sha, req)
        verdict = latest_verdict(repo, pr["number"])
        out["prs"][n] = {"sha": sha, "checks": state, "bad": bad[:5], "verdict": verdict, "title": pr["title"],
                         "branch": pr["headRefName"], "draft": pr["isDraft"], "labels": [l["name"] for l in pr["labels"]],
                         "body": pr.get("body") or "", "merge_state": pr.get("mergeStateStatus", ""), "updated": pr["updatedAt"]}
    issues = json.loads(gh.run(["issue", "list", "-R", repo, "--label", cfg["labels"]["needs_owner"], "--state", "open", "--limit", "50", "--json", "number,title,updatedAt,comments"]))
    for i in issues:
        out["issues"][str(i["number"])] = {"title": i["title"], "updated": i["updatedAt"], "comments": len(i.get("comments") or [])}
    runs = json.loads(gh.run(["run", "list", "-R", repo, "--branch", cfg["merge"]["base"], "--limit", "1", "--json", "databaseId,headSha,status,conclusion,name"]))
    if runs:
        r = runs[0]; out["main"] = {"run": r["databaseId"], "sha": r["headSha"], "status": r["status"], "conclusion": r["conclusion"], "name": r["name"]}
    return out

def latest_verdict(repo: str, number: int) -> dict | None:
    """Newest review-verdict record on the PR (from reviews, then issue comments)."""
    best = None
    try:
        for rv in gh.api_list(f"repos/{repo}/pulls/{number}/reviews?per_page=100"):
            m = VERDICT_RE.search(rv.get("body") or "")
            if m and (best is None or rv["submitted_at"] > best["at"]):
                # The review's own commit_id (the commit it was actually submitted against) is
                # authoritative for the tip — a hand-typed SHA in the body prose can have a typo
                # (issue #4) even when the review is correctly attached to HEAD. Fall back to the
                # body-parsed SHA only if commit_id is missing.
                tip = rv.get("commit_id") or m.group(2)
                best = {"verdict": m.group(1), "tip": tip, "at": rv["submitted_at"], "url": rv["html_url"], "by": rv["user"]["login"]}
        for c in gh.api_list(f"repos/{repo}/issues/{number}/comments?per_page=100"):
            m = VERDICT_RE.search(c.get("body") or "")
            if m and (best is None or c["created_at"] > best["at"]):
                best = {"verdict": m.group(1), "tip": m.group(2), "at": c["created_at"], "url": c["html_url"], "by": c["user"]["login"]}
    except gh.GhError:
        pass
    return best

def diff_events(repo: str, old: dict, new: dict) -> list[dict]:
    ev = []
    op, np_ = old.get("prs", {}), new["prs"]
    for n, pr in np_.items():
        o = op.get(n)
        if o is None:
            ev.append({"kind": "pr_opened", "repo": repo, "number": int(n), "sha": pr["sha"], "title": pr["title"]})
        elif o["sha"] != pr["sha"]:
            ev.append({"kind": "pr_updated", "repo": repo, "number": int(n), "sha": pr["sha"], "prev": o["sha"]})
        if pr["checks"] in ("success", "failure") and (o is None or o["sha"] != pr["sha"] or o["checks"] != pr["checks"]):
            ev.append({"kind": "checks_done", "repo": repo, "number": int(n), "sha": pr["sha"], "result": pr["checks"], "bad": pr["bad"]})
        v = pr["verdict"]
        if v and (o is None or (o.get("verdict") or {}).get("at") != v["at"]):
            ev.append({"kind": "review_verdict", "repo": repo, "number": int(n), "sha": v["tip"], "verdict": v["verdict"], "url": v["url"]})
    for n, o in op.items():
        if n not in np_:
            ev.append({"kind": "pr_closed", "repo": repo, "number": int(n), "sha": o["sha"]})
    oi, ni = old.get("issues", {}), new["issues"]
    for n, i in ni.items():
        o = oi.get(n)
        if o is None:
            ev.append({"kind": "issue_labeled", "repo": repo, "number": int(n), "title": i["title"]})
        elif i["comments"] > o["comments"]:
            ev.append({"kind": "comment", "repo": repo, "number": int(n), "title": i["title"]})
    om, nm = old.get("main", {}), new["main"]
    if nm and nm.get("status") == "completed" and (om.get("run") != nm.get("run") or om.get("conclusion") != nm.get("conclusion")):
        ev.append({"kind": "main_ci", "repo": repo, "sha": nm["sha"], "run": nm["run"], "result": nm["conclusion"]})
    return ev

def poll(cfg: dict, repos: list[str] | None = None) -> list[dict]:
    state = queue.load_state(cfg); all_ev = []
    for repo in repos or cfg["repos"]:
        new = snapshot(repo, cfg)
        old = state.get(repo)
        if old is not None:                      # first poll only seeds the baseline
            all_ev += diff_events(repo, old, new)
        state[repo] = new
    queue.save_state(cfg, state)
    if all_ev:
        queue.append(cfg, all_ev)
    return all_ev

def from_webhook(cfg: dict, headers: dict, body: bytes, secret: str) -> list[dict]:
    """Verify X-Hub-Signature-256 and map a delivery to events. Used by `pm events serve`."""
    sig = headers.get("x-hub-signature-256", "")
    if not hmac.compare_digest(sig, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()):
        raise PermissionError("bad signature")
    ev_name = headers.get("x-github-event", ""); p = json.loads(body); repo = p.get("repository", {}).get("full_name", "")
    ev = []
    if ev_name == "pull_request":
        a = p["action"]; pr = p["pull_request"]
        kind = {"opened": "pr_opened", "synchronize": "pr_updated", "closed": "pr_closed", "reopened": "pr_opened"}.get(a)
        if kind:
            ev.append({"kind": kind, "repo": repo, "number": pr["number"], "sha": pr["head"]["sha"], "title": pr["title"]})
    elif ev_name == "check_suite" and p["action"] == "completed":
        cs = p["check_suite"]
        for pr in cs.get("pull_requests", []):
            ev.append({"kind": "checks_done", "repo": repo, "number": pr["number"], "sha": cs["head_sha"], "result": "success" if cs["conclusion"] in ("success", "neutral", "skipped") else "failure", "bad": []})
        if not cs.get("pull_requests") and cs.get("head_branch") == cfg["merge"]["base"]:
            ev.append({"kind": "main_ci", "repo": repo, "sha": cs["head_sha"], "run": cs["id"], "result": cs["conclusion"]})
    elif ev_name in ("pull_request_review", "issue_comment") and p["action"] in ("submitted", "created"):
        body_txt = (p.get("review") or p.get("comment") or {}).get("body") or ""
        m = VERDICT_RE.search(body_txt)
        num = (p.get("pull_request") or p.get("issue") or {}).get("number")
        if m and num:
            # A pull_request_review payload's review object carries commit_id (the commit it was
            # actually submitted against) same as the REST reviews list — authoritative over a
            # hand-typed SHA in the body. issue_comment has no such field, so its comment stays
            # prose-parsed only.
            sha = (p.get("review") or {}).get("commit_id") or m.group(2)
            ev.append({"kind": "review_verdict", "repo": repo, "number": num, "sha": sha, "verdict": m.group(1), "url": (p.get("review") or p.get("comment"))["html_url"]})
        elif num and p.get("issue") and p["sender"]["type"] != "Bot":
            ev.append({"kind": "comment", "repo": repo, "number": num, "title": p["issue"]["title"]})
    elif ev_name == "issues" and p["action"] == "labeled" and p["label"]["name"] == cfg["labels"]["needs_owner"]:
        ev.append({"kind": "issue_labeled", "repo": repo, "number": p["issue"]["number"], "title": p["issue"]["title"]})
    if ev:
        queue.append(cfg, ev)
    return ev

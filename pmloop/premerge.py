from __future__ import annotations
import re, subprocess, json, time
from . import gh, events, classify, ledger, config

CLOSING_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*#(\d+)", re.I)
ROLE_RE = re.compile(r"^\*\*role:\*\*\s*\S+.*\*\*model:\*\*.*\*\*effort:\*\*", re.M)
MERGEABLE_OK = ("CLEAN", "HAS_HOOKS", "UNSTABLE", "")
SECVAR_RE = re.compile(r"(?<![\w.])(secrets|vars)\.([A-Za-z0-9_]+)")
EXPR_BLOCK_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
EXEMPT_SECRETS = {"GITHUB_TOKEN"}

def _expr_secvar_refs(text: str) -> list[tuple[str, str]]:
    prose = CODE_SPAN_RE.sub("", text)
    return [ref for block in EXPR_BLOCK_RE.findall(prose) for ref in SECVAR_RE.findall(block)]

def _closing_kw_violations(body: str, only_in: str) -> list[str]:
    bad = []
    for line in body.splitlines():
        stripped = line.lstrip()
        for m in CLOSING_RE.finditer(stripped):
            if m.start() != 0:
                continue
            if not m.group(0).startswith(only_in):
                bad.append(m.group(0))
    return bad

def _forbidden_trailer_matches(text: str, patterns: list[str]) -> list[tuple[str, str]]:
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        candidate = stripped[1:].lstrip() if stripped.startswith("\U0001F916") else stripped
        for pat in patterns:
            if candidate.lower().startswith(pat.lower()):
                hits.append((pat, stripped[:120]))
                break
    return hits

_linked_issues = events.linked_issues

def _author_login(pr: dict) -> str:
    return (pr.get("author") or {}).get("login", "")

def _is_bot_author(pr: dict) -> bool:
    author = pr.get("author") or {}
    if "is_bot" in author:
        return bool(author["is_bot"])
    login = author.get("login", "")
    return login.endswith("[bot]") or login.startswith("app/")

def _is_app_login(login: str, cfg: dict) -> bool:
    slug = cfg["bot"]["slug"].removesuffix("[bot]")
    return bool(slug) and login in (slug, f"{slug}[bot]", f"app/{slug}")

def _role_line_row(pr: dict, cfg: dict) -> tuple[str, bool, str]:
    author = _author_login(pr)
    if cfg["bot"]["slug"] and _is_bot_author(pr) and not _is_app_login(author, cfg):
        return "role line in body", True, f"skipped: author is bot {author}, not the app"
    return "role line in body", bool(ROLE_RE.search(pr.get("body") or "")), ""

def _owner_row(repo: str, body: str, cfg: dict) -> tuple[str, bool, str]:
    r = events.linked_needs_owner(repo, body, cfg)
    if not r["nums"]:
        return "linked issue not needs-owner", True, "no linked issues referenced"
    if r["errs"]:
        return "linked issue not needs-owner", False, "could not verify: " + "; ".join(r["errs"])
    if r["flagged"]:
        return "linked issue not needs-owner", False, "; ".join(r["flagged"])
    return "linked issue not needs-owner", True, f"checked: {', '.join(n.split()[0] for n in r['checked'])}"

def _mergeable(repo: str, number: int, cfg: dict, status: str, retry_blocked: bool = False) -> tuple[str, int]:
    attempts = max(1, int(cfg["merge"].get("mergeable_poll_attempts", 5)))
    delay = float(cfg["merge"].get("mergeable_poll_delay_s", 3))
    polls = 0
    while (status == "UNKNOWN" or (status == "BLOCKED" and retry_blocked)) and polls < attempts - 1:
        time.sleep(delay)
        status = gh.pr_view(repo, number, "mergeStateStatus").get("mergeStateStatus", "")
        polls += 1
    return status, polls

def _added_lines(patch: str) -> str:
    return "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))

def _secvar_row(repo: str, number: int, body: str) -> tuple[str, bool, str]:
    gh_diff = "\n".join(_added_lines(f.get("patch") or "") for f in gh.pr_files(repo, number)
                         if f.get("filename", "").startswith(".github/"))
    refs = SECVAR_RE.findall(gh_diff) + _expr_secvar_refs(body)
    sec_refs = sorted({n.upper() for kind, n in refs if kind == "secrets"} - EXEMPT_SECRETS)
    var_refs = sorted({n.upper() for kind, n in refs if kind == "vars"})
    if not sec_refs and not var_refs:
        return "secrets/vars referenced exist", True, "none referenced in body or .github/**"
    configured_secrets, configured_vars, err = gh.configured_secrets_and_vars(repo)
    if err:
        return "secrets/vars referenced exist", False, err
    cs = {s.upper() for s in configured_secrets}; cv = {v.upper() for v in configured_vars}
    missing = [f"secrets.{s}" for s in sec_refs if s not in cs] + [f"vars.{v}" for v in var_refs if v not in cv]
    checked = ", ".join(f"secrets.{s}" for s in sec_refs) + (", " if sec_refs and var_refs else "") + ", ".join(f"vars.{v}" for v in var_refs)
    detail = f"missing: {', '.join(missing)}" if missing else f"checked: {checked}"
    return "secrets/vars referenced exist", not missing, detail

def check(repo: str, number: int, cfg: dict, checkout: str | None = None) -> dict:
    pr = gh.pr_view(repo, number, "number,title,body,headRefOid,headRefName,baseRefName,state,isDraft,mergeStateStatus,commits,labels,author")
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
    st, bad = gh.checks_state(repo, tip, config.required_check(cfg, repo))
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
    row(*_owner_row(repo, body, cfg))
    row(*_role_line_row(pr, cfg))
    bad_kw = _closing_kw_violations(body, cfg["merge"]["closing_keywords_only_in"])
    row("closing keywords only as 'Closes #N'", not bad_kw, "; ".join(bad_kw[:5]))
    checks_commits = cfg["merge"]["method"] != "squash"
    hits = _forbidden_trailer_matches(body, cfg["merge"]["forbid_trailers"])
    if checks_commits:
        msgs = "\n".join(c.get("messageHeadline", "") + "\n" + c.get("messageBody", "") for c in pr.get("commits", []))
        hits += _forbidden_trailer_matches(msgs, cfg["merge"]["forbid_trailers"])
    scope = "checked body + commit messages" if checks_commits else \
        "checked body only, not commit messages (squash merge: pm merge composes its own commit message, so branch commits can't leak into it)"
    detail = scope + ("; matched: " + "; ".join(f'{pat} → "{line}"' for pat, line in hits) if hits else "; no matches")
    row("no forbidden trailers/footers in PR body" + ("/commits" if checks_commits else ""), not hits, detail)
    row(*_secvar_row(repo, number, body))
    ok = all(r["ok"] for r in rows)
    return {"repo": repo, "number": number, "tip": tip, "title": pr["title"], "ok": ok, "rows": rows}

def merge(repo: str, number: int, cfg: dict, role: str, model: str, effort: str, subject: str | None = None, body: str = "", dry: bool = False, force: bool = False) -> dict:
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

"""The pre-merge checklist as a script: every rule the PM used to re-derive by hand, as pass/fail rows."""
from __future__ import annotations
import re, subprocess, json, time
from . import gh, events, classify, ledger, config

CLOSING_RE = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b\s*:?\s*#(\d+)", re.I)
# The doctrine's non-closing reference form ("for #N", per the lane rules) plus every closing-keyword
# spelling: anywhere in the body, not anchored to line start -- unlike CLOSING_RE (which only cares
# about a *malformed closing line*), this just wants every issue number the PR body points at, however
# it's phrased, so the owner-held check below never misses one because it landed mid-sentence.
LINKED_ISSUE_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|for)\s*:?\s*#(\d+)", re.I)
ROLE_RE = re.compile(r"^\*\*role:\*\*\s*\S+.*\*\*model:\*\*.*\*\*effort:\*\*", re.M)
MERGEABLE_OK = ("CLEAN", "HAS_HOOKS", "UNSTABLE", "")
# Negative lookbehind keeps a dotted prefix from sneaking a match in (`foo.vars.X`, `myvars.X`) and,
# together with requiring the literal `secrets.`/`vars.` prefix, means `github.*` context refs
# (`github.token`, `github.repository`, ...) never match at all -- they aren't secrets/vars.
SECVAR_RE = re.compile(r"(?<![\w.])(secrets|vars)\.([A-Za-z0-9_]+)")
# The PR body is free-form prose, not workflow YAML -- a description that merely *mentions* a secret
# name (documenting a bug, quoting a test name, "secrets.A || secrets.B" as an example) must not be
# treated as a real reference. Only the actual GitHub Actions expression form `${{ secrets.NAME }}`
# counts there (this is also the original, pre-#6 regex: `\$\{\{\s*(?:secrets|vars)\.([A-Z0-9_]+)\s*\}\}`),
# so we pull out `${{ ... }}` blocks and only scan inside those. Markdown code formatting -- an inline
# `` `...` `` span or a ``` fenced block -- is the PR author quoting something (an existing diff line, a
# test assertion, an example) rather than declaring a reference the PR itself introduces, so it's
# stripped first; the authoritative "this PR reads secret X" signal is the .github/** diff scan below,
# and the body scan is only a fallback for a raw `${{ }}` written directly into the description. That
# diff scan is real YAML/workflow content, so it can keep matching the bare `secrets.NAME`/`vars.NAME`
# form without requiring the `${{ }}` wrapper.
EXPR_BLOCK_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.S)
EXEMPT_SECRETS = {"GITHUB_TOKEN"}

def _expr_secvar_refs(text: str) -> list[tuple[str, str]]:
    """secrets.X/vars.X references, but only inside an actual `${{ ... }}` expression and outside any
    markdown code quoting -- see SECVAR_RE and the comment above CODE_SPAN_RE."""
    prose = CODE_SPAN_RE.sub("", text)
    return [ref for block in EXPR_BLOCK_RE.findall(prose) for ref in SECVAR_RE.findall(block)]

def _closing_kw_violations(body: str, only_in: str) -> list[str]:
    """pm-loop's own doctrine wants issue-closing prose written only as the canonical `Closes #N`
    form. But CLOSING_RE matches `close[sd]?|fix(e[sd])?|resolve[sd]?` anywhere via `finditer`, so a
    casual mid-sentence mention like "merged 361d59d, closes #568" (narrating a past merge, not
    intending to close anything right now) used to get flagged right alongside a genuine but
    malformed closing line like a bare `fixes #12` (#21). The same false-positive shape #11 hit on
    the trailer/footer check: a match only counts as an intended closing *line* when it starts its
    own line (after stripping leading whitespace) -- exactly the anchor `_forbidden_trailer_matches`
    uses below. A match in the canonical form is never a violation regardless of position (that's
    the whole point of allowing it); only a non-canonical match that also starts its own line is
    flagged as a genuinely malformed closing line."""
    bad = []
    for line in body.splitlines():
        stripped = line.lstrip()
        for m in CLOSING_RE.finditer(stripped):
            if m.start() != 0:
                continue  # not at the start of its (whitespace-stripped) line: mid-sentence prose, not an intended closing line
            if not m.group(0).startswith(only_in):
                bad.append(m.group(0))
    return bad

def _forbidden_trailer_matches(text: str, patterns: list[str]) -> list[tuple[str, str]]:
    """Real trailers/footers appear as their own line: a git trailer is `Key: value` at line start
    (trailer convention), and the Claude Code footer is `Generated with [Claude Code]` at line start,
    optionally preceded by the (robot emoji) prefix. Anchoring to line-start (after stripping leading
    whitespace and an optional emoji prefix) keeps prose that merely *contains* the phrase -- "regenerated
    with", "co-authored the design" mid-sentence, or a backticked example inside a longer sentence -- from
    matching: only a line that actually *begins* with the forbidden text counts. Returns (pattern, line)
    pairs so the row detail can show the real offending line, not just which pattern fired."""
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

def _linked_issues(body: str) -> list[int]:
    return sorted({int(n) for n in LINKED_ISSUE_RE.findall(body)})

def _author_login(pr: dict) -> str:
    return (pr.get("author") or {}).get("login", "")

def _is_bot_author(pr: dict) -> bool:
    """True for a bot- or GitHub-App-authored PR. `gh pr view --json author` reports an `is_bot` flag
    directly (confirmed against a live Dependabot PR: `{"login": "app/dependabot", "is_bot": true}`) --
    that's the authoritative signal. The login-pattern check (`<name>[bot]` for a bot account, `app/<slug>`
    for a GitHub App actor -- the same two shapes `classify.for_pr` matches against `bot_authors`) is only
    a fallback for a `gh` version that doesn't emit `is_bot`."""
    author = pr.get("author") or {}
    if "is_bot" in author:
        return bool(author["is_bot"])
    login = author.get("login", "")
    return login.endswith("[bot]") or login.startswith("app/")

def _is_app_login(login: str, cfg: dict) -> bool:
    slug = cfg["bot"]["slug"].removesuffix("[bot]")
    return bool(slug) and login in (slug, f"{slug}[bot]", f"app/{slug}")

def _role_line_row(pr: dict, cfg: dict) -> tuple[str, bool, str]:
    """`pm merge` composes the squash commit's own subject/body -- including the role line -- at merge
    time (see `merge()` below); it never copies the PR body verbatim. So this row's job is really "will
    the eventual commit carry a role line", which is guaranteed for the app's own dev-lane PRs (the
    `open_pr` tool prepends the role line itself) but is never true of a non-app bot's PR body (Dependabot,
    Renovate, ...) since those bots have no idea pm-loop's doctrine exists. Checking a non-app bot's raw
    PR body for a role line it was never going to write is checking the wrong artifact entirely (#2) --
    skip the row instead of failing every such PR after review+checks already went green. Only skips
    when `bot.slug` is actually configured -- with no slug there's no way to tell "some other bot" from
    "the app itself", and this checklist fails a row rather than silently waving it through on an
    unconfigured/unverifiable signal (same stance `_owner_row` takes on an unresolvable reference)."""
    author = _author_login(pr)
    if cfg["bot"]["slug"] and _is_bot_author(pr) and not _is_app_login(author, cfg):
        return "role line in body", True, f"skipped: author is bot {author}, not the app"
    return "role line in body", bool(ROLE_RE.search(pr.get("body") or "")), ""

def _owner_row(repo: str, body: str, cfg: dict) -> tuple[str, bool, str]:
    """pm-loop's doctrine is 'never decide owner questions' -- a PR whose linked issue carries
    `needs-owner` must never read as a MERGE CANDIDATE just because its own checklist rows (review,
    checks, mergeable) are green. References a number the API can't find (a typo, or a number that
    turned out to be a PR not an issue) fail the row rather than silently passing -- an unverifiable
    reference is not the same as a verified 'not owner-held', and the whole point of this row is to
    never wave an owner question through."""
    nums = _linked_issues(body)
    if not nums:
        return "linked issue not needs-owner", True, "no linked issues referenced"
    needs_owner = cfg["labels"]["needs_owner"]; flagged, checked, errs = [], [], []
    for n in nums:
        try:
            j = gh.api(f"repos/{repo}/issues/{n}") or {}
        except Exception as e:
            errs.append(f"#{n}: {e}"); continue
        checked.append(f"#{n}")
        if needs_owner in {l.get("name") for l in j.get("labels", [])}:
            flagged.append(f"#{n} {j.get('title', '')}")
    if errs:
        return "linked issue not needs-owner", False, "could not verify: " + "; ".join(errs)
    if flagged:
        return "linked issue not needs-owner", False, "; ".join(flagged)
    return "linked issue not needs-owner", True, f"checked: {', '.join(checked)}"

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

def _added_lines(patch: str) -> str:
    """Only lines a diff *adds* (not context, not removed) -- a workflow that deletes a secret
    reference should never be flagged for no longer referencing it."""
    return "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))

def _secvar_row(repo: str, number: int, body: str) -> tuple[str, bool, str]:
    """The 'secret must exist before a PR that reads it merges' row (#6): scans both the PR body (only
    real `${{ secrets.X }}`/`${{ vars.X }}` expressions, not bare prose mentions -- see EXPR_BLOCK_RE)
    and every added line under `.github/**` in the PR's diff (bare `secrets.X`/`vars.X` too, since that's
    actual workflow content) for references, then checks each name against the repo's, org's, and every
    environment's configured secrets/variables. `GITHUB_TOKEN` and any `github.*` context reference are
    exempt -- they aren't secrets/vars at all."""
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
    # A squash merge writes its own message (pm merge composes it), so branch commits only matter for merge/rebase.
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

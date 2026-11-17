from __future__ import annotations
import re, json, sys, difflib
from . import gh

PR_RE = re.compile(r"(?<![\w/])#(\d{1,6})\b")
SHA_RE = re.compile(r"`([0-9a-f]{7,40})`")
RUN_RE = re.compile(r"(?<!issuecomment-)(?<!pullrequestreview-)(?<!discussion_r)"
                     r"(?<!commitcomment-)(?<!event-)(?<!/job/)\b(\d{9,12})\b")
QUOTE_RE = re.compile(r'"([^"\n]{12,200})"')

def added_only(base_text: str, new_text: str) -> str:
    diff = difflib.unified_diff(base_text.splitlines(), new_text.splitlines(), lineterm="")
    return "\n".join(l[1:] for l in diff if l.startswith("+") and not l.startswith("+++"))

def check(text: str, repo: str, cfg: dict, quote_sources: list[str] | None = None, base_text: str | None = None) -> dict:
    if base_text is not None:
        text = added_only(base_text, text)
    findings = []
    prs = sorted({int(n) for n in PR_RE.findall(text)})
    shas = sorted(set(SHA_RE.findall(text)))
    runs = sorted(set(RUN_RE.findall(text)))
    quotes = QUOTE_RE.findall(text) if quote_sources else []
    print(f"factcheck: {len(prs)} ref(s), {len(shas)} sha(s), {len(runs)} run id(s), {len(quotes)} quote(s) to verify",
          file=sys.stderr)
    for n in prs:
        try:
            j = json.loads(gh.run(["api", f"repos/{repo}/issues/{n}", "--jq", "{title: .title, state: .state, pr: (.pull_request != null)}"]))
            findings.append({"kind": "ref", "value": f"#{n}", "ok": True, "detail": f"{'PR' if j['pr'] else 'issue'} {j['state']}: {j['title'][:60]}"})
        except gh.GhError:
            findings.append({"kind": "ref", "value": f"#{n}", "ok": False, "detail": "not found"})
    for sha in shas:
        try:
            gh.run(["api", f"repos/{repo}/commits/{sha}", "--jq", ".sha"])
            findings.append({"kind": "sha", "value": sha[:7], "ok": True, "detail": ""})
        except gh.GhError:
            findings.append({"kind": "sha", "value": sha[:7], "ok": False, "detail": "unknown commit"})
    for run in runs:
        try:
            j = json.loads(gh.run(["api", f"repos/{repo}/actions/runs/{run}", "--jq", "{s:.status,c:.conclusion,h:.head_sha}"]))
            findings.append({"kind": "run", "value": run, "ok": True, "detail": f"{j['s']}/{j['c']} on {j['h'][:7]}"})
        except gh.GhError:
            findings.append({"kind": "run", "value": run, "ok": False, "detail": "no such workflow run"})
    if quote_sources:
        corpus = "\n".join(quote_sources)
        for q in quotes:
            findings.append({"kind": "quote", "value": q[:60], "ok": q in corpus, "detail": "" if q in corpus else "not found verbatim in sources"})
    return {"ok": all(f["ok"] for f in findings), "checked": len(findings), "findings": findings}

"""`pm` — every procedural piece of the loop as a subcommand. JSON out by default so agents and scripts can parse it."""
from __future__ import annotations
import argparse, json, os, sys, shlex
from . import config, events, queue, classify, review, premerge, status, factcheck, watch, board, brief, run, ledger, llm, gh, lane

def main(argv=None):
    ap = argparse.ArgumentParser(prog="pm", description=__doc__)
    ap.add_argument("--repo-dir", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), help="checkout used for .pm-loop.toml and git commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("config", help="print the effective config"); s.add_argument("key", nargs="?")
    s = sub.add_parser("events", help="poll GitHub (or serve webhooks) and append queue events"); s.add_argument("mode", choices=["poll", "serve", "pending", "clear"]); s.add_argument("--repo", action="append"); s.add_argument("--port", type=int, default=8787); s.add_argument("--secret-env", default="PM_LOOP_WEBHOOK_SECRET")
    s = sub.add_parser("board", help="open PRs / owner asks / queue as markdown"); s.add_argument("--json", action="store_true"); s.add_argument("--live", action="store_true")
    s = sub.add_parser("classify", help="review tier for a PR"); s.add_argument("repo"); s.add_argument("pr", type=int)
    s = sub.add_parser("review-needed", help="does the PR need a (re-)review?"); s.add_argument("repo"); s.add_argument("pr", type=int)
    s = sub.add_parser("premerge", help="pre-merge checklist"); s.add_argument("repo"); s.add_argument("pr", type=int)
    s = sub.add_parser("merge", help="checklist then squash-merge as the app"); s.add_argument("repo"); s.add_argument("pr", type=int); s.add_argument("--subject"); s.add_argument("--body", default=""); s.add_argument("--model", default=None); s.add_argument("--effort", default=None); s.add_argument("--dry", action="store_true", help="checklist + the exact merge payload, no PUT")
    s = sub.add_parser("status-entry", help="STATUS.md entry from facts"); s.add_argument("repo"); s.add_argument("pr", type=int); s.add_argument("--json", action="store_true")
    s = sub.add_parser("factcheck", help="verify every #N / sha / run id / quote in a file"); s.add_argument("repo"); s.add_argument("file"); s.add_argument("--quotes-from", action="append", default=[])
    s = sub.add_parser("watch", help="bounded wait for CI (one process, one event)"); s.add_argument("what", choices=["pr", "run"]); s.add_argument("repo"); s.add_argument("id", type=int); s.add_argument("--timeout", type=int)
    s = sub.add_parser("brief", help="brief for a fresh agent"); s.add_argument("role", choices=["dev", "fix", "reviewer", "pm"]); s.add_argument("repo", nargs="?"); s.add_argument("number", nargs="?", type=int); s.add_argument("--tier")
    s = sub.add_parser("lane", help="start a detached headless lane (own process, own budget) or list lanes"); s.add_argument("kind", choices=["dev", "fix", "reviewer", "spike", "list"]); s.add_argument("repo", nargs="?"); s.add_argument("number", nargs="?", type=int); s.add_argument("--tier"); s.add_argument("--extra", default="")
    s = sub.add_parser("run", help="one headless PM tick if the queue is non-empty"); s.add_argument("--dry", action="store_true", help="print the command and brief only"); s.add_argument("--plan-only", action="store_true", help="real claude -p, read-only tools, no actions"); s.add_argument("--force", action="store_true"); s.add_argument("--checkout", action="append", default=[], help="owner/repo=/path")
    s = sub.add_parser("ledger", help="cost summary from the ledger"); s.add_argument("--since", default="")
    s = sub.add_parser("llm", help="local model check / one-off prompt"); s.add_argument("text", nargs="?")
    s = sub.add_parser("git-env", help="export lines so git commits as the bot"); s.add_argument("role")
    a = ap.parse_args(argv); cfg = config.load(a.repo_dir)
    def out(x): print(json.dumps(x, indent=1, default=str))
    if a.cmd == "config":
        out(cfg[a.key] if a.key else cfg)
    elif a.cmd == "events":
        if a.mode == "poll": out({"events": events.poll(cfg, a.repo)})
        elif a.mode == "pending": out(queue.pending(cfg))
        elif a.mode == "clear": ev = queue.pending(cfg); queue.mark_processed(cfg, ev); out({"cleared": len(ev)})
        else: _serve(cfg, a.port, os.environ.get(a.secret_env, ""))
    elif a.cmd == "board":
        b = board.build(cfg, use_state=not a.live); print(json.dumps(b, indent=1) if a.json else board.render(b))
    elif a.cmd == "classify": out(classify.for_pr(a.repo, a.pr, cfg))
    elif a.cmd == "review-needed": out(review.needed(a.repo, a.pr, cfg, a.repo_dir))
    elif a.cmd == "premerge":
        r = premerge.check(a.repo, a.pr, cfg, a.repo_dir); out(r); sys.exit(0 if r["ok"] else 1)
    elif a.cmd == "merge":
        role = cfg["roles"]["pm"]; r = premerge.merge(a.repo, a.pr, cfg, "pm", a.model or role["model"], a.effort or role["effort"], a.subject, a.body, dry=a.dry); out(r); sys.exit(0 if (r["merged"] or a.dry) else 1)
    elif a.cmd == "status-entry":
        print(json.dumps(status.facts(a.repo, a.pr, a.repo_dir), indent=1) if a.json else status.entry(a.repo, a.pr, cfg, a.repo_dir))
    elif a.cmd == "factcheck":
        srcs = [open(p).read() for p in a.quotes_from]; r = factcheck.check(open(a.file).read(), a.repo, cfg, srcs or None); out(r); sys.exit(0 if r["ok"] else 1)
    elif a.cmd == "watch":
        r = watch.pr(a.repo, a.id, cfg, a.timeout) if a.what == "pr" else watch.run(a.repo, a.id, cfg, a.timeout); out(r); sys.exit(0 if r["result"] == "success" else 1)
    elif a.cmd == "brief":
        if a.role == "dev": print(brief.dev(a.repo, a.number, cfg))
        elif a.role == "fix": print(brief.fix(a.repo, a.number, cfg))
        elif a.role == "reviewer": print(brief.reviewer(a.repo, a.number, cfg, a.tier, a.repo_dir))
        else: b = board.build(cfg); print(brief.pm(cfg, board.render(b), ""))
    elif a.cmd == "run":
        co = dict(x.split("=", 1) for x in a.checkout); r = run.tick(cfg, co, a.dry, a.force, a.plan_only)
        if a.dry and r.get("dry"): print(r["cmd"]); print("---"); print(r["prompt"])
        else: out(r)
    elif a.cmd == "lane":
        out(lane.status(cfg) if a.kind == "list" else lane.start(a.kind, a.repo, a.number, cfg, a.repo_dir, a.tier, a.extra))
    elif a.cmd == "ledger": out(ledger.summary(cfg, a.since))
    elif a.cmd == "llm":
        out({"healthy": llm.healthy(cfg), "reply": llm.complete(cfg, "Answer briefly.", a.text) if a.text else None})
    elif a.cmd == "git-env":
        bot = cfg["bot"]; name = f"{bot['slug']}[bot] ({a.role})"; email = f"{bot['app_id']}+{bot['slug']}[bot]@users.noreply.github.com"
        helper = gh.credential_helper(cfg)   # 0700 script that prints the cached token to git only; nothing secret on stdout
        for k, v in {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                     "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": helper}.items():
            print(f"export {k}={shlex.quote(v)}")

def _serve(cfg, port, secret):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    if not secret:
        sys.exit("webhook secret env var is empty")
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                ev = events.from_webhook(cfg, {k.lower(): v for k, v in self.headers.items()}, body, secret); code = 202
            except PermissionError:
                ev, code = [], 401
            self.send_response(code); self.end_headers(); self.wfile.write(json.dumps({"events": len(ev)}).encode())
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    HTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()

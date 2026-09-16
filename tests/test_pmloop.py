import json, os, tempfile, unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmloop import config, classify, events, queue, premerge, factcheck, status

class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); os.environ["PM_LOOP__state_dir"] = self.tmp; os.environ["PM_LOOP_CONFIG"] = "/nonexistent"
        self.cfg = config.load()

    def test_config_layers(self):
        d = tempfile.mkdtemp(); Path(d, ".pm-loop.toml").write_text('[roles.pm]\nmodel="opus"\n')
        c = config.load(d); self.assertEqual(c["roles"]["pm"]["model"], "opus"); self.assertEqual(c["roles"]["dev"]["model"], "sonnet")

    def test_classify_tiers(self):
        c = self.cfg
        self.assertEqual(classify.tier_for(["README.md", "docs/a.md"], c)["tier"], "none")
        self.assertEqual(classify.tier_for([".github/workflows/ci.yml"], c)["tier"], "light")
        c["review"]["adversarial_paths"] = ["engine/**"]
        r = classify.tier_for(["engine/x.go", "README.md"], c); self.assertEqual(r["tier"], "adversarial"); self.assertEqual(r["forced_by"], ["engine/x.go"])
        self.assertEqual(classify.tier_for(["x.md"], c, labels=["no-review"])["tier"], "none")

    def test_queue_roundtrip(self):
        queue.append(self.cfg, [{"kind": "pr_opened", "repo": "o/r", "number": 1, "sha": "abc1234"}])
        p = queue.pending(self.cfg); self.assertEqual(len(p), 1)
        queue.mark_processed(self.cfg, p); self.assertEqual(queue.pending(self.cfg), [])

    def test_diff_events(self):
        old = {"prs": {"1": {"sha": "a"*40, "checks": "pending", "verdict": None, "title": "t", "labels": []}}, "issues": {}, "main": {}}
        new = {"prs": {"1": {"sha": "b"*40, "checks": "success", "verdict": {"verdict": "CLEAR", "tip": "b"*7, "at": "2026", "url": "u"}, "title": "t", "labels": [], "bad": []},
                       "2": {"sha": "c"*40, "checks": "pending", "verdict": None, "title": "n", "labels": [], "bad": []}}, "issues": {"9": {"title": "ask", "updated": "", "comments": 0}},
               "main": {"run": 5, "sha": "d"*40, "status": "completed", "conclusion": "failure"}}
        kinds = sorted(e["kind"] for e in events.diff_events("o/r", old, new))
        self.assertEqual(kinds, ["checks_done", "issue_labeled", "main_ci", "pr_opened", "pr_updated", "review_verdict"])

    def test_verdict_regex(self):
        m = events.VERDICT_RE.search("**role:** test\n\n## CLEAR — tip `deadbeef1`\n\nfine"); self.assertEqual(m.group(1), "CLEAR"); self.assertEqual(m.group(2), "deadbeef1")
        m = events.VERDICT_RE.search("**role:** test · **model:** Claude Sonnet 5\n\n**Verdict: BLOCKED at 576efd2**\n\nTwo"); self.assertEqual((m.group(1), m.group(2)), ("BLOCKED", "576efd2"))

    def test_closing_keywords(self):
        bad = [m.group(0) for m in premerge.CLOSING_RE.finditer("fixes #12 and Closes #13, resolve #500's") if not m.group(0).startswith("Closes #")]
        self.assertEqual(bad, ["fixes #12", "resolve #500"])

    def test_webhook_signature(self):
        import hmac, hashlib
        body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {"number": 3, "head": {"sha": "e"*40}, "title": "x"}}).encode()
        sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
        ev = events.from_webhook(self.cfg, {"x-hub-signature-256": sig, "x-github-event": "pull_request"}, body, "s")
        self.assertEqual(ev[0]["kind"], "pr_opened")
        with self.assertRaises(PermissionError):
            events.from_webhook(self.cfg, {"x-hub-signature-256": "sha256=00", "x-github-event": "pull_request"}, body, "s")

    def test_status_render_template_prose(self):
        f = {"number": 5, "title": "T", "url": "u", "branch": "b", "base": "main", "state": "MERGED", "merged_at": "2026-09-16T00:00:00Z", "merge_sha": "f"*40,
             "head": "e"*40, "files": 2, "additions": 3, "deletions": 1, "closes": [4], "labels": [], "paths": ["engine/a.go", "engine/b.go"], "checked_at": "now", "role": None, "checks": {"sha": "f"*40, "state": "success", "bad": []}}
        txt = status.render(f, self.cfg); self.assertIn("#5 changed 2 file(s) under engine for #4.", txt); self.assertIn("**merged:** `fffffff`", txt)

    def test_factcheck_quotes_only(self):
        r = factcheck.check('he said "exactly these words here"', "o/r", self.cfg, ["... exactly these words here ..."])
        self.assertTrue(r["ok"]); r = factcheck.check('"not in the sources at all"', "o/r", self.cfg, ["x"]); self.assertFalse(r["ok"])

if __name__ == "__main__":
    unittest.main()

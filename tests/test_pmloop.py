import json, os, tempfile, unittest, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmloop import config, classify, events, queue, premerge, factcheck, status, ledger

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

    def test_latest_verdict_prefers_newer_comment_over_older_review(self):
        """A BLOCKED verdict posted as a plain issue-comment fallback (e.g. because the review
        endpoint 422'd, issue #12) must still win over an older CLEAR review, so premerge/board
        don't read a stale CLEAR at tip just because the newest verdict landed as a comment."""
        def fake_api_list(path, **kw):
            if "reviews" in path:
                return [{"body": "**role:** test\n\n## CLEAR — tip `aaaaaaa`", "submitted_at": "2026-01-01T00:00:00Z",
                          "html_url": "u1", "user": {"login": "bot"}}]
            if "comments" in path:
                return [{"body": "**role:** test\n\n## BLOCKED — tip `bbbbbbb`", "created_at": "2026-01-02T00:00:00Z",
                          "html_url": "u2", "user": {"login": "bot"}}]
            return []
        with patch("pmloop.gh.api_list", side_effect=fake_api_list):
            v = events.latest_verdict("o/r", 1)
        self.assertEqual(v["verdict"], "BLOCKED"); self.assertEqual(v["tip"], "bbbbbbb")

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

    def test_status_render_uses_squash_subject_over_stale_title(self):
        """A PR retitled in prose after a rework still names the withdrawn design in `title`; the
        squash commit subject (fixed at merge time) is what the heading should show instead (#13)."""
        f = {"number": 8, "title": "Add the old widget design", "url": "u", "branch": "b", "base": "main",
             "state": "MERGED", "merged_at": "2026-09-17T00:00:00Z", "merge_sha": "a" * 40, "head": "a" * 40,
             "files": 1, "additions": 1, "deletions": 1, "closes": [], "labels": [], "paths": ["x.py"],
             "checked_at": "now", "role": None, "checks": {"sha": "a" * 40, "state": "success", "bad": []},
             "squash_subject": "Rework the widget entirely"}
        txt = status.render(f, self.cfg)
        self.assertEqual(txt.splitlines()[0], "### 2026-09-17T00:00:00Z — #8 Rework the widget entirely")
        self.assertNotIn("Add the old widget design", txt)

    def test_status_facts_takes_squash_subject_from_merge_commit_api_stripping_pr_suffix(self):
        """The subject comes from the GitHub API (not the local checkout, which may not have fetched the
        merge yet) and drops the trailing ` (#N)` that `pm merge` appends when no --subject is given."""
        pr = {"number": 8, "title": "Add the old widget design", "url": "u", "headRefOid": "a" * 40, "headRefName": "b",
              "baseRefName": "main", "state": "MERGED", "mergedAt": "t", "mergeCommit": {"oid": "a" * 40},
              "files": [], "closingIssuesReferences": [], "labels": [], "author": {}, "additions": 0, "deletions": 0, "body": ""}
        def fake_run(args, **kw):
            if "commits/" in args[1]:
                return "Rework the widget entirely (#8)\n\nlonger commit body\n"
            return "a" * 40 + "\n"
        with patch("pmloop.gh.pr_view", return_value=pr), patch("pmloop.gh.check_runs", return_value=[]), \
             patch("pmloop.gh.checks_state", return_value=("success", [])), \
             patch("pmloop.gh.run", side_effect=fake_run):
            f = status.facts("o/r", 8)
        self.assertEqual(f["squash_subject"], "Rework the widget entirely")

    def _merged_pr_facts(self):
        return {"number": 5, "title": "T", "url": "u", "branch": "b", "base": "main", "state": "MERGED",
                "merged_at": "2026-09-18T00:00:00Z", "merge_sha": "f" * 40, "head": "e" * 40, "files": 2,
                "additions": 3, "deletions": 1, "closes": [4], "labels": [], "paths": ["engine/a.go"],
                "checked_at": "now", "role": None, "checks": {"sha": "f" * 40, "state": "success", "bad": []}}

    def test_record_refuses_to_post_when_pr_is_not_merged(self):
        """`pm record` posts a record on the MERGED PR, never on an open one."""
        with patch("pmloop.status.facts", return_value={"state": "OPEN"}), patch("pmloop.gh.api") as api:
            r = status.record("o/r", 5, self.cfg, None, "docs", "sonnet", "medium")
        self.assertFalse(r["ok"]); self.assertFalse(r["posted"]); api.assert_not_called()

    def test_record_refuses_to_post_when_factcheck_fails(self):
        with patch("pmloop.status.facts", return_value=self._merged_pr_facts()), \
             patch("pmloop.factcheck.check", return_value={"ok": False, "checked": 1,
                    "findings": [{"kind": "ref", "value": "#5", "ok": False, "detail": "not found"}]}), \
             patch("pmloop.gh.api") as api:
            r = status.record("o/r", 5, self.cfg, None, "docs", "sonnet", "medium")
        self.assertFalse(r["ok"]); self.assertFalse(r["posted"]); api.assert_not_called()

    def test_record_posts_fact_checked_comment_with_role_line_when_ok(self):
        with patch("pmloop.status.facts", return_value=self._merged_pr_facts()), \
             patch("pmloop.factcheck.check", return_value={"ok": True, "checked": 0, "findings": []}), \
             patch("pmloop.gh.app_token", return_value="tok"), \
             patch("pmloop.gh.api", return_value={"html_url": "u2", "id": 99}) as api:
            r = status.record("o/r", 5, self.cfg, None, "docs", "sonnet", "medium")
        self.assertTrue(r["ok"]); self.assertTrue(r["posted"]); self.assertEqual(r["url"], "u2")
        args, kwargs = api.call_args
        self.assertEqual(args[0], "repos/o/r/issues/5/comments")
        self.assertTrue(kwargs["fields"]["body"].startswith("**role:** docs"))

    def test_record_dry_run_checks_but_does_not_post(self):
        with patch("pmloop.status.facts", return_value=self._merged_pr_facts()), \
             patch("pmloop.factcheck.check", return_value={"ok": True, "checked": 0, "findings": []}), \
             patch("pmloop.gh.api") as api:
            r = status.record("o/r", 5, self.cfg, None, "docs", "sonnet", "medium", dry=True)
        self.assertTrue(r["ok"]); self.assertFalse(r["posted"]); api.assert_not_called()

    def test_factcheck_quotes_only(self):
        r = factcheck.check('he said "exactly these words here"', "o/r", self.cfg, ["... exactly these words here ..."])
        self.assertTrue(r["ok"]); r = factcheck.check('"not in the sources at all"', "o/r", self.cfg, ["x"]); self.assertFalse(r["ok"])

    def _base_pr(self, tip):
        return {"number": 42, "title": "T", "body": "**role:** dev · **model:** sonnet · **effort:** high",
                "headRefOid": tip, "headRefName": "b", "baseRefName": "main", "state": "OPEN", "isDraft": False,
                "commits": [], "labels": []}

    def test_premerge_mergeable_unknown_polls_then_passes(self):
        """mergeStateStatus UNKNOWN (base just moved) is retried, not taken as a hard fail."""
        tip = "a" * 40
        calls = {"n": 0}
        def fake_pr_view(repo, number, fields=""):
            calls["n"] += 1
            if calls["n"] == 1:
                d = dict(self._base_pr(tip)); d["mergeStateStatus"] = "UNKNOWN"; return d
            return {"mergeStateStatus": "UNKNOWN" if calls["n"] < 3 else "CLEAN"}
        self.cfg["merge"]["mergeable_poll_attempts"] = 5; self.cfg["merge"]["mergeable_poll_delay_s"] = 0
        with patch("pmloop.gh.pr_view", side_effect=fake_pr_view), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertTrue(mrow["ok"], mrow); self.assertIn("CLEAN", mrow["detail"])
        self.assertTrue(rep["ok"], rep["rows"])
        self.assertEqual(calls["n"], 3)   # 1 initial view + 2 polls before it resolved

    def test_premerge_mergeable_unknown_gives_up_after_bounded_retries(self):
        """Still UNKNOWN after the bound: decide (fail), don't retry forever and don't wave it through."""
        tip = "b" * 40
        calls = {"n": 0}
        base = self._base_pr(tip); base["mergeStateStatus"] = "UNKNOWN"
        def fake_pr_view(repo, number, fields=""):
            calls["n"] += 1
            return dict(base) if calls["n"] == 1 else {"mergeStateStatus": "UNKNOWN"}
        self.cfg["merge"]["mergeable_poll_attempts"] = 3; self.cfg["merge"]["mergeable_poll_delay_s"] = 0
        with patch("pmloop.gh.pr_view", side_effect=fake_pr_view), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertFalse(mrow["ok"]); self.assertIn("UNKNOWN", mrow["detail"])
        self.assertFalse(rep["ok"])
        self.assertEqual(calls["n"], 3)   # 1 initial + (attempts - 1) = 2 polls, then it stops

    def test_premerge_mergeable_blocked_retries_when_rest_of_checklist_green_then_passes(self):
        """#20: mergeStateStatus BLOCKED is GitHub's other stale-cache flavor (same root cause as #8's
        UNKNOWN) when review is CLEAR at tip and checks are green — retry it too, bounded, and let it pass
        once the cache catches up."""
        tip = "d" * 40
        calls = {"n": 0}
        base = self._base_pr(tip); base["mergeStateStatus"] = "BLOCKED"
        def fake_pr_view(repo, number, fields=""):
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(base)
            return {"mergeStateStatus": "BLOCKED" if calls["n"] < 3 else "CLEAN"}
        self.cfg["merge"]["mergeable_poll_attempts"] = 5; self.cfg["merge"]["mergeable_poll_delay_s"] = 0
        with patch("pmloop.gh.pr_view", side_effect=fake_pr_view), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertTrue(mrow["ok"], mrow); self.assertIn("CLEAN", mrow["detail"]); self.assertIn("was BLOCKED", mrow["detail"])
        self.assertTrue(rep["ok"], rep["rows"])
        self.assertEqual(calls["n"], 3)   # 1 initial + 2 polls before it resolved, same bound as UNKNOWN

    def test_premerge_mergeable_blocked_not_retried_when_checks_are_not_actually_green(self):
        """A real block (here: checks not green) must fail outright, not get the stale-cache retry — proves
        BLOCKED isn't waved through unconditionally, only when the rest of the checklist already says ok."""
        tip = "e" * 40
        calls = {"n": 0}
        base = self._base_pr(tip); base["mergeStateStatus"] = "BLOCKED"
        def fake_pr_view(repo, number, fields=""):
            calls["n"] += 1
            return dict(base) if calls["n"] == 1 else {"mergeStateStatus": "BLOCKED"}
        self.cfg["merge"]["mergeable_poll_attempts"] = 5; self.cfg["merge"]["mergeable_poll_delay_s"] = 0
        with patch("pmloop.gh.pr_view", side_effect=fake_pr_view), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("pending", ["ci=in_progress/None"])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertFalse(mrow["ok"]); self.assertEqual(mrow["detail"], "BLOCKED")
        self.assertFalse(rep["ok"])
        self.assertEqual(calls["n"], 1)   # no poll attempted: checks aren't green, so this isn't the stale-cache case

    def test_premerge_mergeable_blocked_gives_up_after_bounded_retries_even_when_checklist_green(self):
        """Still BLOCKED after the bound despite a green checklist: decide (fail), don't retry forever, and
        say so — this is a real block this checklist can't see (e.g. a rule it doesn't evaluate)."""
        tip = "f" * 40
        calls = {"n": 0}
        base = self._base_pr(tip); base["mergeStateStatus"] = "BLOCKED"
        def fake_pr_view(repo, number, fields=""):
            calls["n"] += 1
            return dict(base) if calls["n"] == 1 else {"mergeStateStatus": "BLOCKED"}
        self.cfg["merge"]["mergeable_poll_attempts"] = 3; self.cfg["merge"]["mergeable_poll_delay_s"] = 0
        with patch("pmloop.gh.pr_view", side_effect=fake_pr_view), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertFalse(mrow["ok"]); self.assertIn("BLOCKED", mrow["detail"]); self.assertIn("stale cache", mrow["detail"])
        self.assertFalse(rep["ok"])
        self.assertEqual(calls["n"], 3)   # 1 initial + (attempts - 1) = 2 polls, then it stops

    def test_merge_refuses_without_force_when_premerge_not_ok(self):
        fail_rep = {"repo": "o/r", "number": 7, "tip": "c" * 40, "title": "T", "ok": False,
                    "rows": [{"check": "checks green", "ok": False, "detail": "pending"}]}
        with patch("pmloop.premerge.check", return_value=fail_rep), patch("pmloop.gh.api") as api:
            r = premerge.merge("o/r", 7, self.cfg, "pm", "sonnet", "medium")
        self.assertFalse(r["merged"]); api.assert_not_called()

    def test_merge_force_premerge_ok_overrides_and_logs_ledger(self):
        """--force-premerge-ok is a human override: it proceeds despite a failing row, and it is logged."""
        fail_rep = {"repo": "o/r", "number": 7, "tip": "c" * 40, "title": "T", "ok": False,
                    "rows": [{"check": "checks green", "ok": False, "detail": "pending"}]}
        with patch("pmloop.premerge.check", return_value=fail_rep), \
             patch("pmloop.gh.app_token", return_value="tok"), \
             patch("pmloop.gh.api", return_value={"merged": True, "sha": "c" * 40}) as api:
            r = premerge.merge("o/r", 7, self.cfg, "pm", "sonnet", "medium", force=True)
        self.assertTrue(r["merged"]); self.assertTrue(r["forced"]); api.assert_called_once()
        lines = (Path(self.cfg["state_dir"]) / "ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertIn("force-premerge-ok", entry["note"]); self.assertIn("checks green", entry["note"])

if __name__ == "__main__":
    unittest.main()

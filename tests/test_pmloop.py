import json, os, tempfile, unittest, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmloop import config, classify, events, queue, premerge, factcheck, status, ledger, brief

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

    def test_latest_verdict_uses_review_commit_id_over_typoed_body_sha(self):
        """Issue #4: a reviewer posted CLEAR with the review attached to the real tip
        (commit_id on the review object matches HEAD) but retyped the SHA in the body prose
        and got one character wrong. The review's own commit_id is authoritative — the body
        SHA must not override it. On unfixed code this returns the typo'd body SHA
        ('cccccc0', one char off from the real 'cccccc1')."""
        def fake_api_list(path, **kw):
            if "reviews" in path:
                return [{"body": "**role:** test\n\n## CLEAR — tip `cccccc0`",  # typo: real tip is cccccc1
                          "commit_id": "cccccc1", "submitted_at": "2026-01-03T00:00:00Z",
                          "html_url": "u3", "user": {"login": "bot"}}]
            if "comments" in path:
                return []
            return []
        with patch("pmloop.gh.api_list", side_effect=fake_api_list):
            v = events.latest_verdict("o/r", 1)
        self.assertEqual(v["verdict"], "CLEAR"); self.assertEqual(v["tip"], "cccccc1")

    def test_latest_verdict_falls_back_to_body_sha_when_review_has_no_commit_id(self):
        """Defensive: if a review object somehow lacks commit_id, fall back to the body-parsed
        SHA rather than losing the tip entirely."""
        def fake_api_list(path, **kw):
            if "reviews" in path:
                return [{"body": "**role:** test\n\n## CLEAR — tip `ddddddd`",
                          "submitted_at": "2026-01-04T00:00:00Z", "html_url": "u4", "user": {"login": "bot"}}]
            return []
        with patch("pmloop.gh.api_list", side_effect=fake_api_list):
            v = events.latest_verdict("o/r", 1)
        self.assertEqual(v["tip"], "ddddddd")

    def test_reviewer_brief_tells_agent_to_paste_tip_verbatim(self):
        """Issue #4 suggestion 2: the reviewer brief must tell the agent to paste the tip
        verbatim rather than retype it, since a retyped SHA can silently typo one character."""
        pr = {"number": 7, "title": "T", "body": "b", "headRefOid": "a" * 40, "baseRefName": "main",
              "labels": [], "files": [{"path": "x.py"}], "url": "https://example/pr/7"}
        with patch("pmloop.gh.pr_view", return_value=pr), \
             patch("pmloop.events.latest_verdict", return_value=None):
            text = brief.reviewer("o/r", 7, self.cfg, tier="light")
        self.assertIn("verbatim", text.lower())
        self.assertIn("never retype it", text.lower())

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

    def test_webhook_review_verdict_uses_commit_id_over_typoed_body_sha(self):
        """Same defect, same file (issue #4): a pull_request_review webhook delivery carries
        commit_id on the review object too — it must win over a hand-typed SHA in the body."""
        import hmac, hashlib
        payload = {"action": "submitted", "repository": {"full_name": "o/r"},
                   "pull_request": {"number": 9},
                   "review": {"body": "## CLEAR — tip `eeeeeee`", "commit_id": "fffffff", "html_url": "u"}}
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
        ev = events.from_webhook(self.cfg, {"x-hub-signature-256": sig, "x-github-event": "pull_request_review"}, body, "s")
        self.assertEqual(ev[0]["kind"], "review_verdict"); self.assertEqual(ev[0]["sha"], "fffffff")

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
             patch("pmloop.gh.pr_files", return_value=[]), \
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
             patch("pmloop.gh.pr_files", return_value=[]), \
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
             patch("pmloop.gh.pr_files", return_value=[]), \
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
             patch("pmloop.gh.pr_files", return_value=[]), \
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
             patch("pmloop.gh.pr_files", return_value=[]), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        mrow = next(r for r in rep["rows"] if r["check"] == "mergeable")
        self.assertFalse(mrow["ok"]); self.assertIn("BLOCKED", mrow["detail"]); self.assertIn("stale cache", mrow["detail"])
        self.assertFalse(rep["ok"])
        self.assertEqual(calls["n"], 3)   # 1 initial + (attempts - 1) = 2 polls, then it stops

    def _secvar_pr(self, extra_body=""):
        return {"number": 9, "title": "T", "body": "**role:** dev · **model:** sonnet · **effort:** high" + extra_body,
                "headRefOid": "a" * 40, "headRefName": "b", "baseRefName": "main", "state": "OPEN", "isDraft": False,
                "commits": [], "labels": [], "mergeStateStatus": "CLEAN"}

    def _run_secvar(self, files, secrets=(), variables=(), org_secrets=(), org_vars=(), env_names=(), env_secrets=None, env_vars=None, body=""):
        env_secrets = env_secrets or {}; env_vars = env_vars or {}
        pr = self._secvar_pr(body)
        def fake_api_list(path, **kw):
            key = kw.get("key")
            if path.startswith("repos/o/r/actions/secrets"): return {"secrets": [{"name": n} for n in secrets]}[key]
            if path.startswith("repos/o/r/actions/variables"): return {"variables": [{"name": n} for n in variables]}[key]
            if path.startswith("repos/o/r/actions/organization-secrets"): return {"secrets": [{"name": n} for n in org_secrets]}[key]
            if path.startswith("repos/o/r/actions/organization-variables"): return {"variables": [{"name": n} for n in org_vars]}[key]
            if path.startswith("repos/o/r/environments") and "/secrets" not in path and "/variables" not in path:
                return {"environments": [{"name": n} for n in env_names]}[key]
            for env, names in env_secrets.items():
                if path.startswith(f"repos/o/r/environments/{env}/secrets"): return {"secrets": [{"name": n} for n in names]}[key]
            for env, names in env_vars.items():
                if path.startswith(f"repos/o/r/environments/{env}/variables"): return {"variables": [{"name": n} for n in names]}[key]
            raise AssertionError(f"unexpected api_list path: {path}")
        with patch("pmloop.gh.pr_view", return_value=pr), \
             patch("pmloop.gh.run", return_value="a" * 40 + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])), \
             patch("pmloop.gh.pr_files", return_value=files), \
             patch("pmloop.gh.api_list", side_effect=fake_api_list):
            rep = premerge.check("o/r", 9, self.cfg)
        return next(r for r in rep["rows"] if r["check"] == "secrets/vars referenced exist")

    def test_secvar_missing_secrets_and_vars_from_workflow_diff_fail_and_are_named(self):
        """Reproduces #6: a workflow reads three secrets/vars that don't exist anywhere; only the pending
        checks row used to fail. This row must now fail too and name all three."""
        files = [{"filename": ".github/workflows/ci.yml", "patch":
                  "@@ -1,2 +1,4 @@\n+  KEY: ${{ secrets.X_KEY }}\n+  SECRET: ${{ secrets.X_SECRET }}\n+  V: ${{ vars.X }}\n"}]
        row = self._run_secvar(files)
        self.assertFalse(row["ok"])
        self.assertIn("secrets.X_KEY", row["detail"]); self.assertIn("secrets.X_SECRET", row["detail"]); self.assertIn("vars.X", row["detail"])

    def test_secvar_present_in_repo_listings_passes(self):
        files = [{"filename": ".github/workflows/ci.yml", "patch": "@@ -1,1 +1,2 @@\n+  K: ${{ secrets.X_KEY }}\n"}]
        row = self._run_secvar(files, secrets=["X_KEY"])
        self.assertTrue(row["ok"], row)

    def test_secvar_present_only_in_environment_listing_passes(self):
        """Covers the environment scope and the `key=` path of `api_list` for the first time."""
        files = [{"filename": ".github/workflows/deploy.yml", "patch": "@@ -1,1 +1,2 @@\n+  K: ${{ secrets.DEPLOY_KEY }}\n"}]
        row = self._run_secvar(files, env_names=["prod"], env_secrets={"prod": ["DEPLOY_KEY"]})
        self.assertTrue(row["ok"], row)

    def test_secvar_github_token_and_github_context_are_exempt(self):
        files = [{"filename": ".github/workflows/ci.yml", "patch":
                  "@@ -1,1 +1,3 @@\n+  T: ${{ secrets.GITHUB_TOKEN }}\n+  R: ${{ github.repository }}\n"}]
        row = self._run_secvar_no_api(files)
        self.assertTrue(row["ok"], row); self.assertIn("none referenced", row["detail"])

    def _run_secvar_no_api(self, files, body=""):
        pr = self._secvar_pr(body)
        with patch("pmloop.gh.pr_view", return_value=pr), \
             patch("pmloop.gh.run", return_value="a" * 40 + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.checks_state", return_value=("success", [])), \
             patch("pmloop.gh.pr_files", return_value=files), \
             patch("pmloop.gh.api_list") as api_list:
            rep = premerge.check("o/r", 9, self.cfg)
            api_list.assert_not_called()
        return next(r for r in rep["rows"] if r["check"] == "secrets/vars referenced exist")

    def test_secvar_reference_outside_github_dir_is_not_scanned(self):
        files = [{"filename": "README.md", "patch": "@@ -1,1 +1,2 @@\n+  ${{ secrets.FOO }}\n"}]
        row = self._run_secvar_no_api(files)
        self.assertTrue(row["ok"], row); self.assertIn("none referenced", row["detail"])

    def test_secvar_removed_line_is_not_flagged(self):
        files = [{"filename": ".github/workflows/ci.yml", "patch": "@@ -1,2 +1,1 @@\n-  K: ${{ secrets.OLD }}\n"}]
        row = self._run_secvar_no_api(files)
        self.assertTrue(row["ok"], row); self.assertIn("none referenced", row["detail"])

    def test_secvar_multiple_refs_on_one_line_both_caught(self):
        files = [{"filename": ".github/workflows/ci.yml", "patch":
                  "@@ -1,1 +1,2 @@\n+  if: ${{ secrets.A || secrets.B }}\n"}]
        row = self._run_secvar(files, secrets=["A"])
        self.assertFalse(row["ok"]); self.assertIn("secrets.B", row["detail"]); self.assertNotIn("secrets.A", row["detail"])

    def test_secvar_declared_in_body_only_is_still_checked(self):
        """Issue asks to diff-scan .github/**, not to drop the pre-existing body scan."""
        row = self._run_secvar([], body="\n\nreads ${{ secrets.BODY_ONLY }}")
        self.assertFalse(row["ok"]); self.assertIn("secrets.BODY_ONLY", row["detail"])

    def test_secvar_bare_mention_in_body_prose_is_not_flagged(self):
        """A PR body that merely *talks about* a secret name (documenting a bug, quoting a test, an
        example like "secrets.A || secrets.B", or quoting an actual `${{ }}` snippet in backticks as an
        example) must not be treated as a real reference. Regression for the false positive #24 itself
        hit: its own body prose named fixture/example secrets, some inline-code-quoted, that don't exist."""
        body_extra = ("\n\nreads secrets.X_KEY, secrets.X_SECRET and vars.X\n"
                      "test_secvar_removed_line_is_not_flagged -- `-  ${{ secrets.OLD }}`\n"
                      "e.g. `${{ secrets.A || secrets.B }}`")
        row = self._run_secvar_no_api([], body=body_extra)
        self.assertTrue(row["ok"], row); self.assertIn("none referenced", row["detail"])

    def test_secvar_real_expression_in_body_is_still_flagged(self):
        """A PR body containing an actual `${{ secrets.FOO }}` expression written directly into the
        prose (not quoted as markdown code) is still a real reference and must still be checked."""
        row = self._run_secvar([], body="\n\nthe new step adds ${{ secrets.FOO }} to the job env")
        self.assertFalse(row["ok"]); self.assertIn("secrets.FOO", row["detail"])

    def test_secvar_code_quoted_expression_in_body_is_not_flagged(self):
        """The same expression, but quoted as inline markdown code (as a PR description would when
        citing an existing line rather than declaring a new one), is not a reference."""
        row = self._run_secvar_no_api([], body="\n\nsee `${{ secrets.QUOTED }}` in the old workflow")
        self.assertTrue(row["ok"], row); self.assertIn("none referenced", row["detail"])

    def _run_trailer_row(self, body="", commits=None, method="squash"):
        tip = "a" * 40
        pr = self._base_pr(tip)
        pr["body"] = pr["body"] + body
        pr["commits"] = commits or []
        self.cfg["merge"]["method"] = method
        with patch("pmloop.gh.pr_view", return_value=pr), \
             patch("pmloop.gh.run", return_value=tip + "\n"), \
             patch("pmloop.classify.for_pr", return_value={"tier": "none", "reason": "t"}), \
             patch("pmloop.events.latest_verdict", return_value=None), \
             patch("pmloop.gh.pr_files", return_value=[]), \
             patch("pmloop.gh.checks_state", return_value=("success", [])):
            rep = premerge.check("o/r", 42, self.cfg)
        return next(r for r in rep["rows"] if r["check"].startswith("no forbidden trailers"))

    def test_trailer_regenerated_with_prose_in_commit_message_is_not_flagged(self):
        """Reproduces #11: a commit line like 'notices/... regenerated with the latest license list'
        must not trip the forbidden 'Generated with' footer pattern -- it's prose, not a footer."""
        commits = [{"messageHeadline": "notices/THIRD-PARTY-NOTICES.txt regenerated with the latest license list",
                    "messageBody": ""}]
        row = self._run_trailer_row(commits=commits, method="merge")
        self.assertTrue(row["ok"], row)

    def test_trailer_bare_mention_mid_sentence_in_body_is_not_flagged(self):
        """A PR body that talks about the checker (e.g. documenting this very fix) and mentions
        'generated with' or 'co-authored-by' mid-sentence, not as its own line, must not be flagged --
        otherwise this fix's own PR body would trip the check it introduces (the #6 self-referential trap)."""
        body_extra = ("\n\nThis fixes premerge so text that merely contains 'generated with' as part of a "
                      "longer sentence, such as when something was regenerated with a tool, no longer matches.")
        row = self._run_trailer_row(body=body_extra)
        self.assertTrue(row["ok"], row)

    def test_trailer_backticked_mid_sentence_mention_is_not_flagged(self):
        """Same trap, phrased as an inline-code example: a line that only starts with 'The fix ...' and
        merely mentions the pattern in backticks mid-sentence is not a real footer."""
        body_extra = "\n\nThe fix checks for a line starting with `Co-Authored-By:` at line start."
        row = self._run_trailer_row(body=body_extra)
        self.assertTrue(row["ok"], row)

    def test_trailer_detail_shows_matched_line_not_just_pattern_name(self):
        """The row detail must name the actual offending line, not just which pattern matched, so a
        false positive is obvious to a human reading it."""
        body_extra = "\n\nCo-Authored-By: Claude <noreply@example.com>"
        row = self._run_trailer_row(body=body_extra)
        self.assertFalse(row["ok"])
        self.assertIn("Co-Authored-By: Claude <noreply@example.com>", row["detail"])

    def test_trailer_squash_merge_does_not_scan_commit_messages_and_says_so(self):
        """A squash merge uses pm merge's own composed subject/body, so a forbidden trailer sitting only
        in a branch commit message can't leak into the merge -- it must not be scanned, and the row detail
        must say so."""
        commits = [{"messageHeadline": "x", "messageBody": "Co-Authored-By: Claude <noreply@example.com>"}]
        row = self._run_trailer_row(commits=commits, method="squash")
        self.assertTrue(row["ok"], row)
        self.assertIn("commit", row["detail"].lower())

    def test_trailer_nonsquash_merge_scans_commit_messages_and_says_so(self):
        """merge_method != squash: branch commit messages become part of the real merge, so they must be
        scanned, the offending line reported, and the detail must say commit messages were checked."""
        commits = [{"messageHeadline": "x", "messageBody": "Co-Authored-By: Claude <noreply@example.com>"}]
        row = self._run_trailer_row(commits=commits, method="merge")
        self.assertFalse(row["ok"])
        self.assertIn("Co-Authored-By: Claude <noreply@example.com>", row["detail"])
        self.assertIn("commit", row["detail"].lower())

    def test_trailer_line_start_co_authored_by_is_flagged(self):
        row = self._run_trailer_row(body="\n\nCo-Authored-By: Claude <noreply@example.com>")
        self.assertFalse(row["ok"])

    def test_trailer_lowercase_co_authored_by_is_still_flagged(self):
        """Git trailer keys are case-insensitive; keep that behavior."""
        row = self._run_trailer_row(body="\n\nco-authored-by: someone <x@example.com>")
        self.assertFalse(row["ok"])

    def test_trailer_emoji_generated_with_footer_is_flagged(self):
        row = self._run_trailer_row(body="\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
        self.assertFalse(row["ok"])
        self.assertIn("Generated with", row["detail"])

    def test_trailer_bare_generated_with_footer_is_flagged(self):
        row = self._run_trailer_row(body="\n\nGenerated with [Claude Code](https://claude.com/claude-code)")
        self.assertFalse(row["ok"])

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

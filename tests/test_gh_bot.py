"""Tests for the gh-bot MCP server (mcp/gh_bot/server.py).

Config, key and state paths are resolved fresh on every call (never cached at import), so
we point PM_LOOP_CONFIG at a throwaway fixture toml before importing, but each test is free
to change PM_LOOP_CONFIG (or the files it points at) again afterwards, in the same process,
and expects the server to notice — no reimport required. See PathResolutionTest for the
issue #18 regression coverage (stale import-time globals after a plugin-cache rebuild).
"""
from __future__ import annotations
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))            # for pmloop.events
sys.path.insert(0, str(REPO_ROOT / "mcp"))    # for gh_bot.server


def _load_server():
    """Import gh_bot.server against a fixture config, isolated from any real pm-loop config."""
    tmp = tempfile.mkdtemp()
    cfg_path = Path(tmp, "config.toml")
    cfg_path.write_text(
        '[bot]\napp_id = 1\ninstallation_id = 1\nkey_path = "/nonexistent-key.pem"\nslug = "testbot"\n'
    )
    os.environ["PM_LOOP_CONFIG"] = str(cfg_path)
    sys.modules.pop("gh_bot.server", None)
    sys.modules.pop("gh_bot", None)
    import gh_bot.server as server
    return server


class ReviewToolTest(unittest.TestCase):
    def setUp(self):
        self.server = _load_server()

    def _fake_client(self, head: str):
        """A _client() stand-in reporting `head` as the PR's current head sha from GET /pulls/{n}."""
        fake_response = MagicMock()
        fake_response.json.return_value = {"html_url": "https://example/1", "id": 1}
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.get.return_value.json.return_value = {"head": {"sha": head}}
        fake_client.get.return_value.raise_for_status.return_value = None
        fake_client.post.return_value = fake_response
        return fake_client

    def _post_review(self, verdict: str, tip: str = "deadbeef1", head: str = "deadbeef1" + "0" * 31) -> dict:
        """Call review() with _client mocked out; return the JSON payload it tried to POST.
        `head` is what GET /pulls/{n} reports as the PR's current head sha (review() must
        check tip against this before posting)."""
        fake_client = self._fake_client(head)
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.review(
                repo="o/r", number=1, role="test", model="m", effort="low",
                verdict=verdict, tip=tip, body="because reasons",
            )
        _, kwargs = fake_client.post.call_args
        return kwargs["json"]

    def test_blocked_posts_comment_not_request_changes(self):
        """BLOCKED must never be posted as REQUEST_CHANGES: GitHub 422s that when the review
        author is also the PR author, which is always true for this app. This is the bug from
        issue #12 — on unfixed code this assertion fails with event == 'REQUEST_CHANGES'."""
        payload = self._post_review("BLOCKED")
        self.assertEqual(payload["event"], "COMMENT")

    def test_clear_also_posts_comment(self):
        payload = self._post_review("CLEAR")
        self.assertEqual(payload["event"], "COMMENT")

    def test_blocked_body_is_readable_by_the_verdict_read_path(self):
        """The state field carries no verdict information (it's always COMMENT now), so the
        read path (pmloop.events.latest_verdict) must still recover BLOCKED and the tip from
        the body text alone. Proves the write and read paths agree."""
        from pmloop.events import VERDICT_RE
        payload = self._post_review("BLOCKED")
        m = VERDICT_RE.search(payload["body"])
        self.assertIsNotNone(m)
        self.assertEqual((m.group(1), m.group(2)), ("BLOCKED", "deadbeef1"))

    def test_review_refuses_tip_that_does_not_match_pr_head(self):
        """Issue #4 suggestion 2: a reviewer that mistypes the tip argument must find out
        immediately, not have it silently posted and fail much later in `pm premerge`.
        On unfixed code this call succeeds and posts instead of raising."""
        fake_client = self._fake_client(head="ffffffff" + "0" * 32)
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.review(
                    repo="o/r", number=1, role="test", model="m", effort="low",
                    verdict="CLEAR", tip="deadbeef1", body="because reasons",
                )
        fake_client.post.assert_not_called()  # must refuse BEFORE posting, not after

    def test_review_refuses_empty_or_non_hex_tip_before_even_checking_head(self):
        """The refusal must be a real mismatch check, not just `head.startswith(tip)` (which is
        trivially true for tip="" or any too-short hex prefix) — an empty/garbage tip must be
        rejected outright, since it would otherwise post an unparseable verdict body."""
        fake_client = self._fake_client(head="deadbeef1" + "0" * 31)
        for bad_tip in ("", "not-hex-at-all", "zzzzzzz"):
            with patch.object(self.server, "_client", return_value=fake_client):
                with self.assertRaises(ValueError):
                    self.server.review(
                        repo="o/r", number=1, role="test", model="m", effort="low",
                        verdict="CLEAR", tip=bad_tip, body="because reasons",
                    )
        fake_client.post.assert_not_called()

    def test_review_accepts_tip_that_is_a_prefix_of_pr_head(self):
        """A short (abbreviated) tip that IS a genuine prefix of the real head must still be
        accepted — the check is a mismatch check, not an exact-length check."""
        payload = self._post_review("CLEAR", tip="deadbeef1", head="deadbeef1" + "2" * 31)
        self.assertEqual(payload["event"], "COMMENT")


def _reimport_server():
    sys.modules.pop("gh_bot.server", None)
    sys.modules.pop("gh_bot", None)
    import gh_bot.server as server
    return server


class PathResolutionTest(unittest.TestCase):
    """Issue #18: mid-session, every gh-bot tool started failing with a bare
    `[Errno 2] No such file or directory` after the plugin cache was rebuilt under a
    running server process, because config/key paths were cached in module globals at
    import time. These tests exercise the fix: fresh-per-call resolution, an error message
    that names the actual missing path, and one retry-after-fresh-resolve before raising.
    """

    def _write_cfg(self, path: Path, *, app_id: int, installation_id: int, key_path: Path, slug: str) -> None:
        path.write_text(
            f'[bot]\napp_id = {app_id}\ninstallation_id = {installation_id}\n'
            f'key_path = "{key_path}"\nslug = "{slug}"\n'
        )

    def _mocked_client(self):
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.get.return_value.json.return_value = {"repositories": []}
        return fake_client

    def test_config_change_is_picked_up_without_reimport(self):
        """Property: config is resolved fresh on every call, not cached at import. On
        unfixed code (APP_ID/APP_SLUG set once as module globals at import) the second
        whoami() below would still report config A's values."""
        tmp = tempfile.mkdtemp()
        cfg_a = Path(tmp, "config-a.toml")
        self._write_cfg(cfg_a, app_id=1, installation_id=1, key_path=Path(tmp, "unused-a.pem"), slug="bot-a")
        os.environ["PM_LOOP_CONFIG"] = str(cfg_a)
        server = _reimport_server()

        with patch.object(server, "_client", return_value=self._mocked_client()):
            who_a = server.whoami()
        self.assertEqual(who_a["app_id"], 1)
        self.assertEqual(who_a["app"], "bot-a")

        # Config changes underneath the running process (e.g. a plugin-cache rebuild) —
        # no reimport here.
        cfg_b = Path(tmp, "config-b.toml")
        self._write_cfg(cfg_b, app_id=2, installation_id=2, key_path=Path(tmp, "unused-b.pem"), slug="bot-b")
        os.environ["PM_LOOP_CONFIG"] = str(cfg_b)

        with patch.object(server, "_client", return_value=self._mocked_client()):
            who_b = server.whoami()
        self.assertEqual(who_b["app_id"], 2)
        self.assertEqual(who_b["app"], "bot-b")

    def test_import_survives_missing_config_file(self):
        """Property: nothing is read at import time. On unfixed code, `import gh_bot.server`
        itself raises FileNotFoundError here (module-level tomllib.loads at import), so this
        test cannot even reach the assertions below."""
        missing_cfg = Path(tempfile.mkdtemp(), "does-not-exist.toml")
        os.environ["PM_LOOP_CONFIG"] = str(missing_cfg)
        server = _reimport_server()  # must not raise

        with self.assertRaises(OSError) as ctx:
            server.whoami()
        self.assertIn(str(missing_cfg), str(ctx.exception))

    def test_key_read_failure_names_the_path(self):
        """Property: a file-operation error says *which* file is missing (config vs. key)
        and names its path, not a bare Errno. Path.read_text() already puts the path in
        str(exc) on unfixed code, so the discriminating part is the "GitHub App private
        key" description — on unfixed code that text does not appear anywhere."""
        tmp = tempfile.mkdtemp()
        missing_key = Path(tmp, "definitely-missing-key.pem")
        cfg = Path(tmp, "config.toml")
        self._write_cfg(cfg, app_id=1, installation_id=1, key_path=missing_key, slug="bot")
        os.environ["PM_LOOP_CONFIG"] = str(cfg)
        server = _reimport_server()

        with self.assertRaises(OSError) as ctx:
            server.whoami()
        self.assertIn(str(missing_key), str(ctx.exception))
        self.assertIn("GitHub App private key", str(ctx.exception))

    def test_moved_key_and_config_healed_without_restart(self):
        """Property: fresh-per-call resolution heals a rebuilt plugin cache that relocates
        the key file and rewrites the config, with no reimport / session restart. On unfixed
        code, _installation_token() still reads the stale module-level KEY_PATH (now deleted)
        and raises FileNotFoundError instead of minting a token."""
        tmp = tempfile.mkdtemp()
        key_a = Path(tmp, "key-a.pem"); key_a.write_text("stale-key")
        cfg_a = Path(tmp, "config-a.toml")
        self._write_cfg(cfg_a, app_id=1, installation_id=1, key_path=key_a, slug="bot-a")
        os.environ["PM_LOOP_CONFIG"] = str(cfg_a)
        server = _reimport_server()

        # Simulate a plugin-cache rebuild: the key moves and the config is rewritten to
        # point at the new location, in the same running process (no reimport below).
        key_b = Path(tmp, "rebuilt-key.pem"); key_b.write_text("fresh-key")
        key_a.unlink()
        cfg_b = Path(tmp, "config-b.toml")
        self._write_cfg(cfg_b, app_id=2, installation_id=2, key_path=key_b, slug="bot-b")
        os.environ["PM_LOOP_CONFIG"] = str(cfg_b)

        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"token": "tok-b", "expires_at": "2999-01-01T00:00:00Z"}
        with patch.object(server, "jwt") as mock_jwt, \
             patch.object(server.httpx, "post", return_value=fake_response) as mock_post:
            mock_jwt.encode.return_value = "signed-assertion"
            token = server._installation_token()

        self.assertEqual(token, "tok-b")
        self.assertEqual(mock_jwt.encode.call_args[0][1], "fresh-key")
        self.assertIn("/installations/2/", mock_post.call_args[0][0])

    def test_transient_oserror_on_key_read_heals_within_one_call(self):
        """Property: on OSError, retry once after a fresh re-resolve before raising, so a
        cache rebuild that briefly removes/recreates a file heals mid-call. On unfixed code
        there is no retry, so the transient failure below propagates immediately."""
        tmp = tempfile.mkdtemp()
        key_path = Path(tmp, "key.pem"); key_path.write_text("real-key-content")
        cfg = Path(tmp, "config.toml")
        self._write_cfg(cfg, app_id=1, installation_id=1, key_path=key_path, slug="bot")
        os.environ["PM_LOOP_CONFIG"] = str(cfg)
        server = _reimport_server()

        real_read_text = Path.read_text
        calls = {"n": 0}

        def flaky_read_text(self, *a, **kw):
            if self == key_path:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError(2, "No such file or directory", str(self))
            return real_read_text(self, *a, **kw)

        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"token": "tok", "expires_at": "2999-01-01T00:00:00Z"}
        with patch.object(Path, "read_text", flaky_read_text), \
             patch.object(server, "jwt") as mock_jwt, \
             patch.object(server.httpx, "post", return_value=fake_response):
            mock_jwt.encode.return_value = "signed"
            token = server._installation_token()

        self.assertEqual(token, "tok")
        self.assertEqual(calls["n"], 2)  # first read failed transiently, retry healed it


class BodyFileTest(unittest.TestCase):
    """Issue #10 suggestion 1: comment/open_issue/open_pr accept body_file as an alternative
    to body, read server-side, so a long/generated body never has to pass through a tool
    argument (and a caller's `$(cat ...)` meant for shell expansion never gets posted
    literally, since there's now a real way to hand over a file)."""

    def setUp(self):
        self.server = _load_server()

    def _fake_client(self):
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "html_url": "https://example/1", "id": 1, "number": 1, "head": {"sha": "abc123"},
        }
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.post.return_value = fake_response
        fake_client.patch.return_value = fake_response
        return fake_client

    def test_comment_body_file_is_read_and_posted(self):
        tmp = tempfile.mkdtemp()
        body_path = Path(tmp, "body.md")
        body_path.write_text("hello from file")
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.comment(repo="o/r", number=1, role="dev", model="m", effort="low", body_file=str(body_path))
        _, kwargs = fake_client.post.call_args
        self.assertIn("hello from file", kwargs["json"]["body"])

    def test_comment_requires_exactly_one_of_body_or_body_file(self):
        """Neither given, or both given, must error clearly before any request is made."""
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.comment(repo="o/r", number=1, role="dev", model="m", effort="low")
            with self.assertRaises(ValueError):
                self.server.comment(repo="o/r", number=1, role="dev", model="m", effort="low", body="x", body_file="y")
        fake_client.post.assert_not_called()

    def test_open_issue_body_file_missing_path_errors_with_path_named(self):
        missing = str(Path(tempfile.mkdtemp(), "does-not-exist.md"))
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError) as ctx:
                self.server.open_issue(repo="o/r", role="dev", model="m", effort="low", title="t", body_file=missing)
        self.assertIn(missing, str(ctx.exception))
        fake_client.post.assert_not_called()

    def test_open_pr_body_file_unreadable_path_errors(self):
        """A directory can't be read as a file (IsADirectoryError) — must surface as a clear
        ValueError, not an uncaught OSError, and must not post."""
        dir_path = tempfile.mkdtemp()
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.open_pr(repo="o/r", role="dev", model="m", effort="low", head="h", title="t", body_file=dir_path)
        fake_client.post.assert_not_called()

    def test_open_pr_inline_body_still_works(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.open_pr(repo="o/r", role="dev", model="m", effort="low", head="h", title="t", body="inline body")
        _, kwargs = fake_client.post.call_args
        self.assertIn("inline body", kwargs["json"]["body"])


class EditToolsTest(unittest.TestCase):
    """Issue #10 suggestion 2: edit_pr / edit_issue let a lane correct a record it already
    posted (title/body/labels), with the same role-line handling as open_pr/open_issue."""

    def setUp(self):
        self.server = _load_server()

    def _fake_client(self):
        fake_response = MagicMock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"html_url": "https://example/1", "number": 1, "head": {"sha": "abc123"}}
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.patch.return_value = fake_response
        return fake_client

    def test_edit_pr_updates_title_and_body_with_role_line(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            result = self.server.edit_pr(repo="o/r", number=1, role="dev", model="m", effort="low", title="new title", body="new body")
        args, kwargs = fake_client.patch.call_args
        self.assertEqual(args[0], "/repos/o/r/pulls/1")
        self.assertEqual(kwargs["json"]["title"], "new title")
        self.assertIn("new body", kwargs["json"]["body"])
        self.assertIn("role:", kwargs["json"]["body"])
        self.assertEqual(result["head_sha"], "abc123")

    def test_edit_pr_title_only_does_not_touch_body(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.edit_pr(repo="o/r", number=1, role="dev", model="m", effort="low", title="new title")
        _, kwargs = fake_client.patch.call_args
        self.assertNotIn("body", kwargs["json"])

    def test_edit_pr_body_file_is_read(self):
        tmp = tempfile.mkdtemp()
        body_path = Path(tmp, "body.md")
        body_path.write_text("body from file")
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.edit_pr(repo="o/r", number=1, role="dev", model="m", effort="low", body_file=str(body_path))
        _, kwargs = fake_client.patch.call_args
        self.assertIn("body from file", kwargs["json"]["body"])

    def test_edit_pr_requires_at_least_one_field(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.edit_pr(repo="o/r", number=1, role="dev", model="m", effort="low")
        fake_client.patch.assert_not_called()

    def test_edit_pr_rejects_both_body_and_body_file(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.edit_pr(repo="o/r", number=1, role="dev", model="m", effort="low", body="x", body_file="y")
        fake_client.patch.assert_not_called()

    def test_edit_issue_updates_title_body_and_labels(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.edit_issue(repo="o/r", number=1, role="dev", model="m", effort="low", title="t2", body="b2", labels=["bug"])
        args, kwargs = fake_client.patch.call_args
        self.assertEqual(args[0], "/repos/o/r/issues/1")
        self.assertEqual(kwargs["json"]["title"], "t2")
        self.assertIn("b2", kwargs["json"]["body"])
        self.assertEqual(kwargs["json"]["labels"], ["bug"])

    def test_edit_issue_empty_labels_list_clears_labels_not_rejected(self):
        """labels=[] means 'clear all labels', a legitimate edit — must not be treated as
        'nothing to edit' just because the list is falsy."""
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.edit_issue(repo="o/r", number=1, role="dev", model="m", effort="low", labels=[])
        _, kwargs = fake_client.patch.call_args
        self.assertEqual(kwargs["json"]["labels"], [])

    def test_edit_issue_requires_at_least_one_field(self):
        fake_client = self._fake_client()
        with patch.object(self.server, "_client", return_value=fake_client):
            with self.assertRaises(ValueError):
                self.server.edit_issue(repo="o/r", number=1, role="dev", model="m", effort="low")
        fake_client.patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

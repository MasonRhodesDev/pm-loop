"""Tests for the gh-bot MCP server's `review` tool (mcp/gh_bot/server.py).

The server reads its GitHub App config at import time (app id, installation id, key path),
so we point PM_LOOP_CONFIG at a throwaway fixture toml *before* importing it. The key file
itself is never read at import time (only its path is stored), so a nonexistent path is fine.
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

    def _post_review(self, verdict: str) -> dict:
        """Call review() with _client mocked out; return the JSON payload it tried to POST."""
        fake_response = MagicMock()
        fake_response.json.return_value = {"html_url": "https://example/1", "id": 1}
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = False
        fake_client.post.return_value = fake_response
        with patch.object(self.server, "_client", return_value=fake_client):
            self.server.review(
                repo="o/r", number=1, role="test", model="m", effort="low",
                verdict=verdict, tip="deadbeef1", body="because reasons",
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


if __name__ == "__main__":
    unittest.main()

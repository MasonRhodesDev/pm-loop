"""gh-bot: post to GitHub as a GitHub App (pm-loop). Adapted from mason-agent.

Every record-producing tool requires `role`, `model` and `effort`, and writes the
role line itself as the first line of the body, so nothing can post without it.
Tokens are minted from the app's private key and cached until near expiry.
The token never leaves this process.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

import httpx
import jwt
from mcp.server.fastmcp import FastMCP

import tomllib
API = "https://api.github.com"

# Config, key and state paths are resolved fresh on every call below (never cached at
# import), so a plugin-cache rebuild that moves them under a long-running server process
# is picked up on the very next tool call instead of requiring a session restart.


def _read_path_text(resolve_path, describe: str) -> str:
    """Read the text at resolve_path(), re-resolving from scratch and retrying once on
    OSError before giving up. resolve_path is called fresh on each attempt (it re-reads
    env vars and, for derived paths, re-parses the config file), so a file that moved or
    was briefly absent because a plugin cache was mid-rebuild heals on the retry without
    needing a session restart. If both attempts fail, the error names the actual path
    that was tried, instead of a bare `[Errno 2] No such file or directory`.
    """
    last_err: OSError | None = None
    last_path = None
    for _attempt in range(2):
        try:
            path = resolve_path()
            last_path = path
            return path.read_text()
        except OSError as e:
            last_err = e
    if last_path is None:
        # resolve_path() itself failed both times (e.g. the config file that a derived
        # path depends on) — that error already names its own path, so don't wrap it.
        raise last_err
    raise OSError(f"gh-bot: {describe} not found at {last_path} ({last_err})") from last_err


def _cfg() -> dict:
    """Load the pm-loop config fresh from disk (never cached), so PM_LOOP_CONFIG or the
    file it points at can change without restarting this process."""
    def resolve() -> Path:
        return Path(os.path.expanduser(os.environ.get("PM_LOOP_CONFIG", "~/.config/pm-loop/config.toml")))
    text = _read_path_text(resolve, "pm-loop config")
    return tomllib.loads(text).get("bot", {})


def _app_id() -> int:
    return int(_cfg()["app_id"])


def _installation_id() -> int:
    return int(_cfg()["installation_id"])


def _key_path() -> Path:
    return Path(os.path.expanduser(_cfg()["key_path"]))


def _app_slug() -> str:
    return _cfg()["slug"]


def _read_key() -> str:
    """Read the GitHub App private key, re-resolving its path (via a fresh config read)
    on every call and retrying once on OSError before raising."""
    return _read_path_text(_key_path, "GitHub App private key")


def _state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("PM_LOOP_STATE_DIR", "~/.local/state/pm-loop")))


Role = Literal["pm", "dev", "test", "architecture", "docs"]
Effort = Literal["low", "medium", "high"]
Verdict = Literal["CLEAR", "BLOCKED", "VERIFIED", "DISPUTED"]

mcp = FastMCP(
    "gh-bot",
    instructions=(
        "Post to GitHub as the configured GitHub App. Every tool that leaves a record takes "
        "role, model and effort and prepends the role line itself. Use role=pm for "
        "coordination and merges, dev for implementation, test for reviews and "
        "measurements, architecture for owner-decision write-ups, docs for STATUS reconciles."
    ),
)

_token: dict = {"value": None, "exp": 0.0}


def _installation_token() -> str:
    if _token["value"] and _token["exp"] - time.time() > 300:
        return _token["value"]
    now = int(time.time())
    assertion = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(_app_id())},
        _read_key(),
        algorithm="RS256",
    )
    r = httpx.post(
        f"{API}/app/installations/{_installation_id()}/access_tokens",
        headers={"Authorization": f"Bearer {assertion}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    _token["value"] = body["token"]
    _token["exp"] = time.mktime(time.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    return _token["value"]


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={
            "Authorization": f"Bearer {_installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=60,
    )


def role_line(role: Role, model: str, effort: Effort, extra: str | None = None) -> str:
    line = f"**role:** {role} · **model:** {model} · **effort:** {effort}"
    if extra:
        line += f" · {extra}"
    return line


def _with_line(body: str, role: Role, model: str, effort: Effort, extra: str | None = None) -> str:
    return f"{role_line(role, model, effort, extra)}\n\n{body.strip()}\n"


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError("repo must be owner/name")
    return owner, name


@mcp.tool()
def whoami() -> dict:
    """Return the app identity this server posts as, and the repos it is installed on."""
    with _client() as c:
        repos = c.get("/installation/repositories").json().get("repositories", [])
    slug = _app_slug()
    return {
        "app": slug,
        "app_id": _app_id(),
        "byline": f"{slug}[bot]",
        "repos": [r["full_name"] for r in repos],
        "roles": list(Role.__args__),
    }


@mcp.tool()
def comment(repo: str, number: int, role: Role, model: str, effort: Effort, body: str) -> dict:
    """Post a comment on an issue or PR as the app, with the role line prepended."""
    _split_repo(repo)
    with _client() as c:
        r = c.post(f"/repos/{repo}/issues/{number}/comments", json={"body": _with_line(body, role, model, effort)})
        r.raise_for_status()
        j = r.json()
    return {"url": j["html_url"], "id": j["id"]}


@mcp.tool()
def edit_comment(repo: str, comment_id: int, role: Role, model: str, effort: Effort, body: str) -> dict:
    """Replace the body of a comment by id (never 'edit last'), keeping the role line."""
    with _client() as c:
        r = c.patch(f"/repos/{repo}/issues/comments/{comment_id}", json={"body": _with_line(body, role, model, effort)})
        r.raise_for_status()
        j = r.json()
    return {"url": j["html_url"], "id": j["id"]}


@mcp.tool()
def open_issue(repo: str, role: Role, model: str, effort: Effort, title: str, body: str, labels: list[str] | None = None) -> dict:
    """Open an issue as the app, with the role line prepended to the body."""
    payload = {"title": title, "body": _with_line(body, role, model, effort)}
    if labels:
        payload["labels"] = labels
    with _client() as c:
        r = c.post(f"/repos/{repo}/issues", json=payload)
        r.raise_for_status()
        j = r.json()
    return {"url": j["html_url"], "number": j["number"]}


@mcp.tool()
def open_pr(repo: str, role: Role, model: str, effort: Effort, head: str, title: str, body: str, base: str = "main", draft: bool = False) -> dict:
    """Open a pull request as the app, with the role line prepended to the body."""
    with _client() as c:
        r = c.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": _with_line(body, role, model, effort), "draft": draft},
        )
        r.raise_for_status()
        j = r.json()
    return {"url": j["html_url"], "number": j["number"], "head_sha": j["head"]["sha"]}


@mcp.tool()
def review(repo: str, number: int, role: Role, model: str, effort: Effort, verdict: Verdict, tip: str, body: str) -> dict:
    """Post a review verdict (CLEAR/BLOCKED/VERIFIED/DISPUTED) naming the reviewed tip, as a PR review.

    Always posts a COMMENT-state review, never APPROVE or REQUEST_CHANGES: this app opens the
    PRs it reviews, and GitHub 422s a REQUEST_CHANGES (or APPROVE) review from the PR's own
    author. The verdict lives in the body ("## VERDICT — tip `sha`"), not the review `state`,
    and the read path (pmloop.events.latest_verdict) parses that body text on every review
    regardless of state, so a COMMENT-state review carries a BLOCKED verdict just as reliably
    and stays on the PR's reviews list (unlike the plain-comment fallback).
    """
    event = "COMMENT"
    text = f"## {verdict} — tip `{tip}`\n\n{body.strip()}"
    with _client() as c:
        r = c.post(
            f"/repos/{repo}/pulls/{number}/reviews",
            json={"event": event, "body": _with_line(text, role, model, effort, f"**verdict:** {verdict}")},
        )
        r.raise_for_status()
        j = r.json()
    return {"url": j["html_url"], "id": j["id"], "verdict": verdict, "tip": tip}


@mcp.tool()
def merge(repo: str, number: int, role: Role, model: str, effort: Effort, subject: str, body: str, expected_head: str, method: Literal["squash", "merge", "rebase"] = "squash") -> dict:
    """Merge a PR as the app. Refuses unless role is pm, the PR head equals expected_head, and
    the combined check state for that head is success (the CLEAR-plus-green rule).
    """
    if role != "pm":
        raise ValueError("only role=pm merges")
    with _client() as c:
        pr = c.get(f"/repos/{repo}/pulls/{number}").json()
        head = pr["head"]["sha"]
        if not head.startswith(expected_head):
            raise ValueError(f"PR head is {head[:7]}, expected {expected_head}")
        runs = c.get(f"/repos/{repo}/commits/{head}/check-runs", params={"per_page": 100}).json().get("check_runs", [])
        bad = [f"{x['name']}={x['status']}/{x['conclusion']}" for x in runs if x["status"] != "completed" or x["conclusion"] not in ("success", "skipped", "neutral")]
        if bad:
            raise ValueError("checks not green: " + ", ".join(bad))
        r = c.put(
            f"/repos/{repo}/pulls/{number}/merge",
            json={"merge_method": method, "commit_title": subject, "commit_message": _with_line(body, role, model, effort), "sha": head},
        )
        r.raise_for_status()
        j = r.json()
    return {"merged": j["merged"], "sha": j["sha"], "head": head}


@mcp.tool()
def git_env(role: Role) -> dict:
    """Environment for git so commits are authored by the app and pushes authenticate as it.
    The token never appears in the result: git reads it through a 0700 credential-helper file."""
    _installation_token()
    state = _state_dir(); state.mkdir(parents=True, exist_ok=True)
    cache = state / ".token-cache.json"; helper = state / "git-credential-gh-bot"
    fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"token": _token["value"], "exp": _token["exp"]}, fh)
    fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/sh\n[ \"$1\" = get ] || exit 0\necho username=x-access-token\n"
                 f"printf 'password=%s\\n' \"$(python3 -c 'import json;print(json.load(open(\"{cache}\"))[\"token\"])')\"\n")
    slug = _app_slug()
    name = f"{slug}[bot] ({role})"
    email = f"{_app_id()}+{slug}[bot]@users.noreply.github.com"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": str(helper), "expires_in_s": int(_token["exp"] - time.time())}


@mcp.tool()
def push_branch(worktree: str, branch: str, role: Role, force: bool = False) -> dict:
    """Push a branch from a local worktree to origin as the app (HTTPS, token never printed)."""
    env = {k: str(v) for k, v in git_env(role).items() if k != "expires_in_s"}
    cmd = ["git", "-C", worktree, "push", "-u", "origin", f"HEAD:refs/heads/{branch}"]
    if force:
        cmd.insert(3, "--force-with-lease")
    p = subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True)
    return {"ok": p.returncode == 0, "stderr": p.stderr.strip()[-2000:]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

"""Thin `gh` wrappers. Reads use the user's gh auth; writes use the GitHub App token (never printed)."""
from __future__ import annotations
import json, os, subprocess, time, base64, hashlib
from pathlib import Path

class GhError(RuntimeError):
    pass

def run(args: list[str], *, token: str | None = None, check: bool = True, input: str | None = None, timeout: int = 120) -> str:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    p = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, input=input, timeout=timeout)
    if check and p.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}… rc={p.returncode}: {p.stderr.strip()[-500:]}")
    return p.stdout

def api(path: str, *, method: str = "GET", fields: dict | None = None, token: str | None = None, raw: bool = False):
    args = ["api", path, "-X", method, "-H", "Accept: application/vnd.github+json"]
    inp = None
    if fields is not None:
        args += ["--input", "-"]; inp = json.dumps(fields)
    out = run(args, token=token, input=inp)
    if raw:
        return out
    return json.loads(out) if out.strip() else None

def api_list(path: str, *, token: str | None = None, key: str | None = None) -> list:
    """GET with --paginate; concatenated pages are merged (arrays, or objects under `key`)."""
    out = run(["api", path, "--paginate", "-H", "Accept: application/vnd.github+json"], token=token)
    dec = json.JSONDecoder(); i = 0; s = out.strip(); items = []
    while i < len(s):
        obj, i = dec.raw_decode(s, i)
        if key is not None:
            obj = obj.get(key, [])
        items.extend(obj if isinstance(obj, list) else [obj])
        while i < len(s) and s[i] .isspace():
            i += 1
    return items

def pr_view(repo: str, number: int, fields: str = "number,title,body,headRefOid,headRefName,baseRefName,state,mergedAt,mergeStateStatus,isDraft,labels,files,author,url,commits") -> dict:
    return json.loads(run(["pr", "view", str(number), "-R", repo, "--json", fields]))

def check_runs(repo: str, sha: str) -> list[dict]:
    return api_list(f"repos/{repo}/commits/{sha}/check-runs?per_page=100", key="check_runs")

def pr_files(repo: str, number: int) -> list[dict]:
    """Files changed in the PR via the files API (paginated), so each entry carries a unified-diff
    `patch` (GitHub omits it for binary/oversized files) alongside the filename."""
    return api_list(f"repos/{repo}/pulls/{number}/files?per_page=100")

def _names(path: str, key: str) -> set[str]:
    return {i["name"] for i in api_list(path, key=key)}

def configured_secrets_and_vars(repo: str) -> tuple[set[str], set[str], str]:
    """Names of secrets/vars visible to this repo: repo-level (required -- a listing failure here is
    surfaced, not swallowed, since silently treating it as empty would report every referenced secret
    as 'missing'), plus org secrets/vars shared with this repo and every configured environment's
    secrets/vars (both best-effort: the token may not be able to see them, and that alone isn't a
    reason to fail the row). Listing secrets only ever returns names, never values -- that's all
    existence-checking needs."""
    try:
        secrets = _names(f"repos/{repo}/actions/secrets?per_page=100", "secrets")
        variables = _names(f"repos/{repo}/actions/variables?per_page=100", "variables")
    except Exception as e:
        return set(), set(), f"could not list repo secrets/vars: {e}"
    def best_effort(path: str, key: str) -> set[str]:
        try:
            return _names(path, key)
        except Exception:
            return set()
    secrets |= best_effort(f"repos/{repo}/actions/organization-secrets?per_page=100", "secrets")
    variables |= best_effort(f"repos/{repo}/actions/organization-variables?per_page=100", "variables")
    for env in best_effort(f"repos/{repo}/environments?per_page=100", "environments"):
        secrets |= best_effort(f"repos/{repo}/environments/{env}/secrets?per_page=100", "secrets")
        variables |= best_effort(f"repos/{repo}/environments/{env}/variables?per_page=100", "variables")
    return secrets, variables, ""

def checks_state(repo: str, sha: str, required: str = "") -> tuple[str, list[str]]:
    """('success'|'pending'|'failure', [bad runs]) — mirrors the merge tool's rule; `required` narrows to one check name."""
    runs = check_runs(repo, sha)
    if required:
        runs = [r for r in runs if r["name"] == required]
        if not runs:
            return "pending", [f"{required}=missing"]
    bad = [f"{r['name']}={r['status']}/{r['conclusion']}" for r in runs
           if r["status"] != "completed" or r["conclusion"] not in ("success", "skipped", "neutral")]
    if any(r["status"] != "completed" for r in runs):
        return "pending", bad
    return ("failure" if bad else "success"), bad

# ---- GitHub App token (same scheme as gh-agent: RS256 JWT via openssl, cached until 5 min before expiry) ----
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def app_token(cfg: dict) -> str:
    bot = cfg["bot"]
    if not bot.get("app_id"):
        raise GhError("bot.app_id not configured; writes need the GitHub App")
    cache = Path(cfg["state_dir"]) / ".token-cache.json"
    try:
        c = json.loads(cache.read_text())
        if c["exp"] > time.time() + 300:
            return c["token"]
    except Exception:
        pass
    now = int(time.time())
    hdr = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    pl = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(bot["app_id"])}).encode())
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", bot["key_path"]], input=f"{hdr}.{pl}".encode(), capture_output=True, check=True).stdout
    jwt = f"{hdr}.{pl}.{_b64url(sig)}"
    p = subprocess.run(["curl", "-sf", "-X", "POST", "-H", f"Authorization: Bearer {jwt}", "-H", "Accept: application/vnd.github+json",
                        f"https://api.github.com/app/installations/{bot['installation_id']}/access_tokens"], capture_output=True, text=True)
    if p.returncode != 0:
        raise GhError("could not mint an installation token (check app_id/installation_id/key_path)")
    j = json.loads(p.stdout)
    exp = time.mktime(time.strptime(j["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"token": j["token"], "exp": exp}, fh)
    return j["token"]

def credential_helper(cfg: dict) -> str:
    """Path of a 0700 helper that hands git the cached App token. Mints/refreshes the cache first."""
    app_token(cfg)
    cache = Path(cfg["state_dir"]) / ".token-cache.json"; helper = Path(cfg["state_dir"]) / "git-credential-pm-loop"
    body = "#!/bin/sh\n# git credential helper for pm-loop: token comes from the App-token cache, never from argv/env\n" \
           f"[ \"$1\" = get ] || exit 0\necho username=x-access-token\nprintf 'password=%s\\n' \"$(python3 -c 'import json;print(json.load(open(\"{cache}\"))[\"token\"])')\"\n"
    fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return str(helper)

def role_line(role: str, model: str, effort: str, extra: str = "") -> str:
    line = f"**role:** {role} · **model:** {model} · **effort:** {effort}"
    return line + (f" · {extra}" if extra else "")

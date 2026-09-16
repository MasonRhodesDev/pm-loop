"""Local model client (OpenAI-compatible chat endpoint, e.g. llama.cpp server on the home-lab).
Used ONLY for prose: a STATUS sentence, a PR-body summary, a CI-log triage line. Never for verdicts."""
from __future__ import annotations
import json, urllib.request, urllib.error

def complete(cfg: dict, system: str, user: str, max_tokens: int | None = None) -> str | None:
    llm = cfg["llm"]
    if not llm.get("endpoint"):
        return None
    req = urllib.request.Request(llm["endpoint"].rstrip("/") + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({"model": llm.get("model") or "default", "temperature": 0.2, "max_tokens": max_tokens or llm["max_tokens"],
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode())
    try:
        with urllib.request.urlopen(req, timeout=llm["timeout_s"]) as r:
            j = json.load(r)
        return j["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, OSError):
        return None

def healthy(cfg: dict) -> bool:
    llm = cfg["llm"]
    if not llm.get("endpoint"):
        return False
    try:
        with urllib.request.urlopen(llm["endpoint"].rstrip("/") + "/v1/models", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False

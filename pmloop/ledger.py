"""Token/cost ledger: every headless run and every lane report appends one line; `pm ledger` sums it."""
from __future__ import annotations
import json, time, collections
from .config import state_dir

def record(cfg: dict, role: str, model: str, usage: dict, cost_usd: float | None, note: str = "") -> None:
    with open(state_dir(cfg) / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "role": role, "model": model,
                             "usage": usage, "cost_usd": cost_usd, "note": note}) + "\n")

def summary(cfg: dict, since: str = "") -> dict:
    p = state_dir(cfg) / "ledger.jsonl"; by = collections.defaultdict(lambda: {"runs": 0, "cost_usd": 0.0, "in": 0, "out": 0, "cache_read": 0})
    if not p.exists():
        return {}
    for line in p.read_text().splitlines():
        r = json.loads(line)
        if since and r["ts"] < since:
            continue
        k = f"{r['role']}/{r['model']}"; u = r.get("usage") or {}
        by[k]["runs"] += 1; by[k]["cost_usd"] += r.get("cost_usd") or 0
        by[k]["in"] += u.get("input_tokens", 0); by[k]["out"] += u.get("output_tokens", 0); by[k]["cache_read"] += u.get("cache_read_input_tokens", 0)
    return dict(by)

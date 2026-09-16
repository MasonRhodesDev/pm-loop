"""Append-only event queue + poller state. One JSONL line per event; `pm run` consumes unprocessed lines."""
from __future__ import annotations
import json, time, fcntl
from pathlib import Path
from .config import state_dir

def _paths(cfg):
    d = state_dir(cfg); return d / "queue.jsonl", d / "processed.txt", d / "poll-state.json"

def append(cfg: dict, events: list[dict]) -> int:
    q, _, _ = _paths(cfg)
    with open(q, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        for e in events:
            e.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            e.setdefault("id", f"{e['ts']}-{e['kind']}-{e.get('repo','')}-{e.get('number','')}-{e.get('sha','')[:7]}")
            fh.write(json.dumps(e, sort_keys=True) + "\n")
    return len(events)

def pending(cfg: dict) -> list[dict]:
    q, done, _ = _paths(cfg)
    seen = set(done.read_text().split()) if done.exists() else set()
    out, ids = [], set()
    if q.exists():
        for line in q.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e["id"] in seen or e["id"] in ids:
                continue
            ids.add(e["id"]); out.append(e)
    return out

def mark_processed(cfg: dict, events: list[dict]) -> None:
    _, done, _ = _paths(cfg)
    with open(done, "a") as fh:
        fh.write("".join(e["id"] + "\n" for e in events))

def load_state(cfg: dict) -> dict:
    _, _, st = _paths(cfg)
    return json.loads(st.read_text()) if st.exists() else {}

def save_state(cfg: dict, state: dict) -> None:
    _, _, st = _paths(cfg)
    tmp = st.with_suffix(".tmp"); tmp.write_text(json.dumps(state, indent=1, sort_keys=True)); tmp.replace(st)

"""Is a (re-)review needed? Compare the tip the last verdict named with the current tip, ignoring merges from base."""
from __future__ import annotations
import subprocess
from . import gh, events

def needed(repo: str, number: int, cfg: dict, checkout: str | None = None) -> dict:
    pr = gh.pr_view(repo, number, "headRefOid,baseRefName,labels,files")
    tip = pr["headRefOid"]; v = events.latest_verdict(repo, number)
    if v is None:
        return {"needed": True, "reason": "no verdict yet", "tip": tip, "reviewed": None}
    if tip.startswith(v["tip"]) or v["tip"].startswith(tip[: len(v["tip"])]):
        return {"needed": False, "reason": f"verdict {v['verdict']} is at tip", "tip": tip, "reviewed": v["tip"], "verdict": v["verdict"], "url": v["url"]}
    changed = None
    if checkout:
        # non-merge commits between reviewed tip and current tip; a pure merge-from-base adds none
        args = ["git", "-C", checkout, "log", "--format=%H", f"{v['tip']}..{tip}"]
        if cfg["review"].get("rereview_ignore_merges", True):
            args.insert(3, "--no-merges")
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode == 0:
            commits = [c for c in p.stdout.split() if c]
            if not commits:
                return {"needed": False, "reason": "only merge commits since reviewed tip", "tip": tip, "reviewed": v["tip"], "verdict": v["verdict"], "url": v["url"]}
            d = subprocess.run(["git", "-C", checkout, "diff", "--stat", f"{v['tip']}...{tip}"], capture_output=True, text=True).stdout
            changed = d.strip().splitlines()[-1] if d.strip() else ""
    return {"needed": True, "reason": "new non-merge commits since reviewed tip", "tip": tip, "reviewed": v["tip"], "prior": v["verdict"], "url": v["url"], "diffstat": changed}

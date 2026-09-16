"""Classify a PR's changed files into classes and pick the review tier — a script decides, not a reviewer."""
from __future__ import annotations
import fnmatch
from . import gh

def _match(path: str, globs: list[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "")) or (g.endswith("/**") and path.startswith(g[:-3] + "/")):
            return True
    return False

def classes_for(files: list[str], cfg: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in files:
        cls = "code"
        for name, globs in cfg["review"]["classes"].items():
            if _match(f, globs):
                cls = name; break
        out.setdefault(cls, []).append(f)
    return out

TIERS = ["none", "light", "adversarial"]

def tier_for(files: list[str], cfg: dict, labels: list[str] = ()) -> dict:
    cls = classes_for(files, cfg)
    if cfg["labels"]["no_review"] in labels:
        return {"tier": "none", "reason": f"label {cfg['labels']['no_review']}", "classes": cls}
    tiers = [cfg["review"]["tier_by_class"].get(c, "light") for c in cls]
    tier = max(tiers, key=TIERS.index) if tiers else "none"
    forced = [f for f in files if _match(f, cfg["review"]["adversarial_paths"])]
    if forced:
        tier = "adversarial"
    return {"tier": tier, "classes": cls, "forced_by": forced[:10], "reason": "adversarial_paths" if forced else "tier_by_class"}

def for_pr(repo: str, number: int, cfg: dict) -> dict:
    pr = gh.pr_view(repo, number, "number,files,labels,headRefOid,author")
    files = [f["path"] for f in pr["files"]]
    r = tier_for(files, cfg, [l["name"] for l in pr["labels"]])
    author = (pr.get("author") or {}).get("login", "")          # gh reports GitHub Apps as "app/<slug>"
    for login, tier in cfg["review"].get("bot_authors", {}).items():
        slug = login.replace("[bot]", "")
        if author in (login, slug, f"app/{slug}"):
            r.update(tier=tier, reason=f"bot author {login}")
    r.update(repo=repo, number=number, sha=pr["headRefOid"], files=len(files))
    return r

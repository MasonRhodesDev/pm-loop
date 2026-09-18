"""The PM's whole world in ~2–4K tokens: open PRs with checks/verdict/tier/next action, owner asks, queue."""
from __future__ import annotations
import json
from . import gh, queue, classify, events

def build(cfg: dict, repos: list[str] | None = None, use_state: bool = True) -> dict:
    state = queue.load_state(cfg) if use_state else {}
    rows, asks = [], []
    for repo in repos or cfg["repos"]:
        snap = state.get(repo) or events.snapshot(repo, cfg)
        for n, pr in snap["prs"].items():
            v = pr.get("verdict") or {}
            at_tip = bool(v) and pr["sha"].startswith(v.get("tip", "x"))
            if pr["draft"]:
                nxt = "draft: wait"
            elif cfg["labels"]["needs_owner"] in pr["labels"]:
                nxt = "held: needs-owner"
            elif (hold := events.linked_needs_owner(repo, pr.get("body") or "", cfg))["flagged"] or hold["errs"]:
                # Same priority as the direct-label check above: a PR whose linked issue (`Closes #N`/
                # `for #N`, per premerge._owner_row) is owner-held, or whose reference couldn't even be
                # verified, must never read as further along (needs review / MERGE CANDIDATE) than a
                # PR with its own needs-owner label would -- #30. An unverifiable reference fails closed
                # here too, same stance premerge._owner_row takes: it's not the same as a verified
                # "not owner-held".
                held_num = (hold["flagged"] or hold["errs"])[0].split()[0].rstrip(":")
                nxt = f"held: needs-owner (linked {held_num})" if hold["flagged"] else f"held: linked {held_num} could not be verified"
            elif not v:
                nxt = "needs review (pm classify → brief reviewer)"
            elif v["verdict"] == "BLOCKED" and at_tip:
                nxt = "blocked: brief dev lane with the verdict"
            elif not at_tip:
                nxt = "verdict stale: pm review-needed"
            elif pr["checks"] == "pending":
                nxt = "CLEAR at tip, CI pending: nothing to do"
            elif pr["checks"] == "failure":
                nxt = "CLEAR at tip, CI red: brief dev lane"
            else:
                nxt = "MERGE CANDIDATE: pm merge"
            rows.append({"repo": repo, "pr": int(n), "title": pr["title"][:60], "sha": pr["sha"][:7], "checks": pr["checks"],
                         "verdict": (v.get("verdict", "-") + ("" if at_tip or not v else "(stale)")), "labels": pr["labels"], "next": nxt})
        for n, i in snap["issues"].items():
            asks.append({"repo": repo, "issue": int(n), "title": i["title"][:70], "comments": i["comments"]})
    return {"prs": rows, "owner_asks": asks, "queue": queue.pending(cfg)}

def render(b: dict) -> str:
    out = ["## Open PRs", "| repo | PR | title | tip | checks | verdict | next |", "|---|---|---|---|---|---|---|"]
    for r in b["prs"]:
        out.append(f"| {r['repo'].split('/')[-1]} | #{r['pr']} | {r['title']} | {r['sha']} | {r['checks']} | {r['verdict']} | {r['next']} |")
    if not b["prs"]:
        out.append("| – | – | no open PRs | | | | |")
    out += ["", "## Owner asks (needs-owner)"] + ([f"- {a['repo'].split('/')[-1]}#{a['issue']} {a['title']} ({a['comments']} comments)" for a in b["owner_asks"]] or ["- none"])
    out += ["", f"## Queue ({len(b['queue'])} events)"] + [f"- {e['kind']} {e.get('repo','').split('/')[-1]}#{e.get('number', e.get('run',''))} {e.get('result', e.get('verdict',''))} {e.get('sha','')[:7]}" for e in b["queue"][:40]]
    return "\n".join(out) + "\n"

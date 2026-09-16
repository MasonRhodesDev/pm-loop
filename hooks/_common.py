import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pmloop import config  # noqa: E402

def read():
    return json.load(sys.stdin)

def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    sys.exit(0)

PM_AGENTS = {"dev-lane": "dev", "reviewer-light": "review_light", "reviewer-adversarial": "review_adversarial", "spike": "spike"}

def in_subagent(h):
    return bool(h.get("agent_id") or h.get("agent_type"))

def pm_role(h):
    """Role this hook applies to: one of pm-loop's agents, the headless PM tick (PM_LOOP_TICK=1), else None."""
    t = h.get("agent_type") or ""
    if t in PM_AGENTS:
        return PM_AGENTS[t]
    if os.environ.get("PM_LOOP_TICK") and not in_subagent(h):
        return "pm"
    return None

def cfg(h):
    return config.load(h.get("cwd"))

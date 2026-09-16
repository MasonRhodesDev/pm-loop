#!/usr/bin/env python3
"""Per-agent tool-call cap. The cap comes from roles.<role>.max_tool_calls; the role is inferred from agent_type
(dev-lane→dev, reviewer-light→review_light, reviewer-adversarial→review_adversarial, spike→spike, else pm)."""
import sys, json
from pathlib import Path
from _common import read, deny, cfg, pm_role
h = read(); c = cfg(h)
role = pm_role(h)
if role is None:          # not a pm-loop agent and not the headless tick: never cap the user's own session
    sys.exit(0)
key = h.get("agent_id") or h.get("session_id") or "main"
cap = int(c["roles"].get(role, {}).get("max_tool_calls", 0) or 0)
d = Path(c["state_dir"]) / "toolcaps"; d.mkdir(parents=True, exist_ok=True); f = d / f"{key}.count"
n = int(f.read_text() or 0) + 1 if f.exists() else 1
f.write_text(str(n))
if cap and n > cap:
    deny(f"tool-call cap reached for role {role} ({cap}). Write your final report now: what is done, what is not, the exact next command.")
sys.exit(0)

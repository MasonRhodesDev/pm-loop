#!/usr/bin/env python3
"""SubagentStop: flag a lane that ended its turn 'waiting' — nothing will ever notify it (lanes-end-turns-waiting)."""
import json, re, sys
h = json.load(sys.stdin)
txt = json.dumps(h.get("last_assistant_message") or h.get("result") or h)[-4000:]
if re.search(r"\b(waiting (for|on)|will (continue|resume) (once|when)|awaiting)\b", txt, re.I):
    print(json.dumps({"systemMessage": "pm-loop: this agent ended its turn waiting on something. Nothing notifies it; resume it with a bounded foreground command or spawn a fresh agent."}))
sys.exit(0)

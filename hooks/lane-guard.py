#!/usr/bin/env python3
"""Inside any subagent: no nested agents, no background Bash, no CI polling loops, no bulk
Docker teardown (docker ps -aq / docker system prune / docker rm -f $(...)), no sudo systemctl."""
import re, sys, os
from _common import read, deny, in_subagent, cfg, pm_role
h = read(); c = cfg(h)["lanes"]; tool = h.get("tool_name"); inp = h.get("tool_input") or {}
# Applies to pm-loop's own lanes; every other subagent only if lanes.guard_all_subagents = true
if (in_subagent(h) or os.environ.get("PM_LOOP_AGENT_TYPE")) and (pm_role(h) or c.get("guard_all_subagents")):
    if tool == "Agent" and c.get("deny_subagents", True):
        deny("lane rule: lanes never spawn sub-agents (a fork redoes the task and opens duplicate PRs). Report the need in your final message.")
    if tool == "Bash":
        if inp.get("run_in_background") and c.get("deny_background", True):
            deny("lane rule: no background jobs; run it in the foreground with a bounded `timeout`.")
        cmd = inp.get("command", "")
        if re.search(r"(while|until)\b.*\bgh (pr checks|run (view|list))", cmd, re.S) or re.search(r"\bgh (pr checks|run watch)\b.*--watch", cmd) and not re.search(r"\btimeout\s+\d", cmd):
            deny("lane rule: never poll CI from a turn; use `pm watch pr <owner/repo> <n> --timeout N` once, or finish your turn.")
        if c.get("deny_bulk_docker", True):
            if re.search(r"\bdocker\s+ps\s+-(aq|qa)\b", cmd):
                deny("lane rule: never run `docker ps -aq` (a bulk listing that feeds wiping containers you did not create). Name every container you create with the issue number and remove only those, by name.")
            if re.search(r"\bdocker\s+system\s+prune\b", cmd):
                deny("lane rule: never run `docker system prune`; it removes resources you did not create. Remove only your own named containers/networks/volumes.")
            if re.search(r"\bdocker\s+rm\s+-f\b[^;&|\n]*\$\(", cmd):
                deny("lane rule: never run `docker rm -f` with a `$(...)` command substitution; that's a bulk removal of every matching container. Remove only the specific containers you created, by name.")
        if c.get("deny_sudo_systemctl", True) and re.search(r"\bsudo\b.*\bsystemctl\b", cmd, re.S):
            deny("lane rule: never start/stop/restart system services with `sudo systemctl` on the host. If a daemon isn't running, report it and stop.")
sys.exit(0)

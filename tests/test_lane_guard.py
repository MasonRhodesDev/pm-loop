"""Tests for hooks/lane-guard.py's PreToolUse deny logic (issue #5).

The hook is invoked by Claude Code as a subprocess (JSON on stdin, a deny decision as JSON
on stdout), so we exercise it the same way: craft a hook-input payload, run the script, and
check the permissionDecision it emits. Every test also asserts a clean exit (returncode 0,
no stderr) so a hook traceback can't masquerade as "allowed".
"""
from __future__ import annotations
import json, os, subprocess, sys, unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "lane-guard.py"


def run_hook(command: str, tool: str = "Bash", agent_type: str = "dev-lane", tool_input_extra: dict | None = None):
    payload = {"agent_type": agent_type, "tool_name": tool, "tool_input": {"command": command, **(tool_input_extra or {})}}
    env = {k: v for k, v in os.environ.items() if not k.startswith("PM_LOOP")}
    env["PM_LOOP_CONFIG"] = "/nonexistent-pm-loop-config.toml"  # force defaults, isolated from any real config
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=10
    )


def denied(proc: subprocess.CompletedProcess) -> bool:
    assert proc.returncode == 0, f"hook exited {proc.returncode}, stderr: {proc.stderr}"
    assert proc.stderr == "", f"hook wrote to stderr: {proc.stderr}"
    out = proc.stdout.strip()
    if not out:
        return False
    data = json.loads(out)
    return data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class BulkDockerTest(unittest.TestCase):
    def test_docker_ps_aq_denied(self):
        self.assertTrue(denied(run_hook("docker ps -aq")))

    def test_docker_ps_aq_with_further_args_denied(self):
        self.assertTrue(denied(run_hook("docker stop $(docker ps -aq)")))

    def test_docker_system_prune_denied(self):
        self.assertTrue(denied(run_hook("docker system prune -f")))

    def test_docker_rm_f_command_substitution_denied(self):
        # independent of the `docker ps -aq` rule: a different subshell still feeds a bulk rm -f
        self.assertTrue(denied(run_hook("docker rm -f $(cat /tmp/leftover-ids.txt)")))

    def test_docker_rm_f_named_container_allowed(self):
        self.assertFalse(denied(run_hook("docker rm -f my-container-5")))

    def test_docker_run_allowed(self):
        self.assertFalse(denied(run_hook("docker run --name test-5 myimage")))

    def test_docker_build_allowed(self):
        self.assertFalse(denied(run_hook("docker build -t test-5 .")))

    def test_docker_stop_named_container_allowed(self):
        self.assertFalse(denied(run_hook("docker stop test-5 && docker rm test-5")))

    def test_docker_rm_f_then_unrelated_substitution_allowed(self):
        # the $( is not feeding the rm -f -- must not be a bulk removal
        self.assertFalse(denied(run_hook("docker rm -f test-5\nTS=$(date +%s)\necho $TS")))


class SudoSystemctlTest(unittest.TestCase):
    def test_sudo_systemctl_start_denied(self):
        self.assertTrue(denied(run_hook("sudo -n systemctl start docker")))

    def test_sudo_systemctl_restart_denied(self):
        self.assertTrue(denied(run_hook("sudo systemctl restart myservice")))

    def test_sudo_other_command_allowed(self):
        self.assertFalse(denied(run_hook("sudo apt-get update")))

    def test_systemctl_without_sudo_allowed(self):
        self.assertFalse(denied(run_hook("systemctl --user status myservice")))


class GuardScopeTest(unittest.TestCase):
    """The new checks only apply inside a pm-loop lane, same gate as the existing rules."""

    def test_outside_lane_agent_not_denied(self):
        self.assertFalse(denied(run_hook("docker ps -aq", agent_type="")))


if __name__ == "__main__":
    unittest.main()

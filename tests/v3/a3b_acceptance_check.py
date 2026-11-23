"""A3b acceptance (orchestrator-owned; the implementer must not edit this file).

PR map A3 (split), A3b: the shared gate treats the v3 daemon exactly like
daemon2. Agents are refused (DENY, never asked) the owner operations that
start, install, restart or stop it, any changing systemctl verb on its unit
`jarvis-agent`, and kills that reach it: by a pattern matching its command
line at the word `agentd`, or fed its PID from its port (8410) or a
listing. Reads stay allowed. `jarvis hud` becomes an owner op too, because
A4 makes it switch daemons (it will stop jarvis-agent).

Parity is the test: every template's verdict for the v3 values must equal its
verdict for the daemon2 values, for every template, both ways.

Checks are in-process against `protected_state.owner_op`; nothing is executed.
Run: PYTHONPATH=<worktree> HOME=$(mktemp -d) python tests/v3/a3b_acceptance_check.py
"""
from __future__ import annotations

import unittest

TEMPLATES = [
    # kills by pattern at the daemon's word
    "pkill -f {W}", "pkill -9 -f 'jarvis {W}'", "pkill -f 'python -m jarvis {W}'", "killall -r {W}",
    "kill $(pgrep -f {W})", "kill -9 `pgrep -f {W}`", "pgrep -f {W} | xargs kill",
    "ps aux | grep {W} | awk '{{print $2}}' | xargs kill -9",
    "grep -l {W} /proc/*/cmdline | cut -d/ -f3 | xargs kill",
    "nohup pkill -f {W}", "timeout 5 pkill -f {W}", "sh -c 'pkill -f {W}'",
    # the unit
    "systemctl --user stop {U}", "systemctl --user restart {U}.service", "systemctl --user kill {U}",
    "systemctl --user disable --now {U}", "systemctl --user mask {U}", "systemctl --user try-restart {U}",
    "systemctl --user stop 'jarvis-*'", "busctl --user call org.freedesktop.systemd1 /org/freedesktop/systemd1 "
    "org.freedesktop.systemd1.Manager StopUnit ss {U}.service replace",
    "kill $(systemctl --user show -p MainPID --value {U})",
    # its port
    "fuser -k {P}/tcp", "kill $(lsof -t -i:{P})", "kill -9 $(lsof -ti tcp:{P})",
    # reads and unrelated lines stay allowed
    "pgrep -f {W}", "ps aux | grep {W}", "systemctl --user status {U}", "systemctl --user is-active {U}",
    "journalctl --user -u {U} -n 50", "lsof -i:{P}", "pkill -f jarvis", "pkill python", "kill 12345",
]
V2 = dict(W="daemon2", U="jarvis-daemon2", P="8405")
V3 = dict(W="agentd", U="jarvis-agent", P="8410")

OWNER = [
    "jarvis agent start", "jarvis agent install", "jarvis agent restart", "jarvis agent stop",
    "python -m jarvis agent start", "python3 -m jarvis agent install", "nohup jarvis agent start",
    "uv run jarvis agent restart", "sh -c 'jarvis agent start'", "echo start | xargs jarvis agent",
    "jarvis hud", "jarvis hud open", "jarvis hud --no-window", "python -m jarvis hud",
]
READS = ["jarvis agent status", "jarvis agent", "jarvis agentd --help", "jarvis bench agent"]


class Parity(unittest.TestCase):
    def test_v3_twins_match_daemon2(self):
        from jarvis import protected_state as ps
        mismatches = []
        for t in TEMPLATES:
            a, b = t.format(**V2), t.format(**V3)
            if (ps.owner_op(a) is None) != (ps.owner_op(b) is None):
                mismatches.append((t, ps.owner_op(a) is not None, ps.owner_op(b) is not None))
        self.assertEqual(mismatches, [], "template, daemon2 denied, v3 denied")

    def test_v3_kills_and_unit_changes_are_denied(self):
        from jarvis import protected_state as ps
        for t in ("pkill -f {W}", "systemctl --user stop {U}", "fuser -k {P}/tcp", "kill $(lsof -t -i:{P})"):
            self.assertIsNotNone(ps.owner_op(t.format(**V3)), t)

    def test_reads_stay_allowed(self):
        from jarvis import protected_state as ps
        for t in ("pgrep -f {W}", "systemctl --user status {U}", "journalctl --user -u {U} -n 50"):
            self.assertIsNone(ps.owner_op(t.format(**V3)), t)


class OwnerOps(unittest.TestCase):
    def test_switch_install_restart_stop_are_owner_only(self):
        from jarvis import protected_state as ps
        for cmd in OWNER:
            self.assertIsNotNone(ps.owner_op(cmd), cmd)

    def test_reads_are_not(self):
        from jarvis import protected_state as ps
        for cmd in READS:
            self.assertIsNone(ps.owner_op(cmd), cmd)

    def test_existing_owner_ops_unchanged(self):
        from jarvis import protected_state as ps
        for cmd in ("jarvis restart", "jarvis daemon2 restart", "jarvis hud install", "jarvis hud rebuild"):
            self.assertIsNotNone(ps.owner_op(cmd), cmd)
        self.assertIsNone(ps.owner_op("jarvis daemon2"))      # running a daemon is not an op (parity)
        self.assertIsNone(ps.owner_op("jarvis agentd"))       # same for v3

    def test_rules_decide_denies_too(self):
        # v1's static verdict and v2's layer 1 both start from rules.decide.
        from jarvis import rules
        for cmd in ("jarvis agent start", "pkill -f agentd", "systemctl --user stop jarvis-agent"):
            verdict = rules.decide(cmd)
            self.assertEqual(verdict.decision, "deny", (cmd, verdict))


if __name__ == "__main__":
    unittest.main()

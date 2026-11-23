"""A4 acceptance (orchestrator-owned; the implementer must not edit this file).

PR map A4: one daemon at a time (owner decision A-2). `jarvis agent start`
switches to v3, plain `jarvis hud` switches back to v2, `jarvis agent install`
writes the v3 user unit. Everything goes through an injectable `Env`, so these
tests never touch systemd, sockets or real time.

Interface under test (jarvis/v3/switch.py):
  V2_UNIT = "jarvis-daemon2"; V3_UNIT = "jarvis-agent"
  @dataclass class Env:
      systemctl: Callable[..., CompletedProcess]   # called with the args after `systemctl --user`
      v2_status: Callable[[], tuple[dict | None, str | None]]  # owner_ops.read_status semantics;
                                                   # (None, owner_ops.REFUSED) when nothing listens
      v3_status: Callable[[], dict | None]          # /v3/status payload, or None when nothing answers
      port_free: Callable[[int], bool]
      clock: Callable[[], float]
      sleep: Callable[[float], None]
      out: Callable[[str], None]
  to_v3(env, *, force=False, wait=30.0) -> int     # 0 switched/already, 1 failed, 3 refused (busy)
  to_v2(env, *, wait=30.0) -> int                  # 0 switched/already, 1 failed
  install_unit(env, *, unit_path: Path, python: str, repo: Path) -> int   # 0 written or unchanged

Run: PYTHONPATH=<worktree> HOME=$(mktemp -d) python tests/v3/a4_acceptance_check.py
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

REFUSED = None  # filled from owner_ops in setUp


class World:
    """A fake user systemd with two units, two status endpoints and a clock."""

    def __init__(self, v2_active=True, v3_active=False, v2_busy=False, v3_boots=True, v2_boots=True,
                 v3_installed=True):
        from jarvis.v2 import owner_ops
        self.REFUSED = owner_ops.REFUSED
        self.active = {"jarvis-daemon2": v2_active, "jarvis-agent": v3_active}
        self.enabled = {"jarvis-daemon2": v2_active, "jarvis-agent": v3_active}
        self.v2_busy, self.v3_boots, self.v2_boots, self.v3_installed = v2_busy, v3_boots, v2_boots, v3_installed
        self.calls, self.lines, self.now = [], [], 1000.0
        self.pending = {}  # unit -> clock time its state change completes

    # -- systemctl ------------------------------------------------------------
    def systemctl(self, *args):
        self.calls.append(list(args))
        verb, rest = args[0], [a for a in args[1:] if not a.startswith("-")]
        now_flag = "--now" in args
        for unit in rest:
            unit = unit.removesuffix(".service")
            if unit == "jarvis-agent" and not self.v3_installed and verb in ("start", "enable", "restart"):
                return subprocess.CompletedProcess(args, 5, "", "Unit jarvis-agent.service not found.")
            if verb == "stop":
                self.pending[unit] = (self.now + 2.0, False)
            elif verb in ("start", "restart"):
                self.pending[unit] = (self.now + 2.0, True)
            elif verb == "enable":
                self.enabled[unit] = True
                if now_flag:
                    self.pending[unit] = (self.now + 2.0, True)
            elif verb == "disable":
                self.enabled[unit] = False
                if now_flag:
                    self.pending[unit] = (self.now + 2.0, False)
        if verb in ("is-active",):
            unit = rest[0].removesuffix(".service")
            return subprocess.CompletedProcess(args, 0 if self._active(unit) else 3,
                                               "active\n" if self._active(unit) else "inactive\n", "")
        if verb == "is-enabled":
            unit = rest[0].removesuffix(".service")
            return subprocess.CompletedProcess(args, 0 if self.enabled[unit] else 1,
                                               "enabled\n" if self.enabled[unit] else "disabled\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def _active(self, unit):
        when = self.pending.get(unit)
        if when and self.now >= when[0]:
            want = when[1]
            if unit == "jarvis-agent" and want and not self.v3_boots:
                want = False
            if unit == "jarvis-daemon2" and want and not self.v2_boots:
                want = False
            self.active[unit] = want
            del self.pending[unit]
        return self.active[unit]

    # -- the rest of Env --------------------------------------------------------
    def v2_status(self):
        if not self._active("jarvis-daemon2"):
            return None, self.REFUSED
        return ({"version": 2, "turns_running": 1 if self.v2_busy else 0, "approvals_pending": 0}, None)

    def v3_status(self):
        if not self._active("jarvis-agent"):
            return None
        return {"service": "jarvis-agentd", "version": "v3"}

    def port_free(self, port):
        return not self._active({8405: "jarvis-daemon2", 8410: "jarvis-agent"}.get(port, ""))

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds

    def env(self):
        from jarvis.v3 import switch
        return switch.Env(systemctl=self.systemctl, v2_status=self.v2_status, v3_status=self.v3_status,
                          port_free=self.port_free, clock=self.clock, sleep=self.sleep, out=self.lines.append)

    def verbs(self):
        return [c for c in self.calls if c[0] not in ("is-active", "is-enabled", "show", "status")]


class ToV3(unittest.TestCase):
    def test_switch_order_and_waits(self):
        from jarvis.v3 import switch
        w = World()
        self.assertEqual(switch.to_v3(w.env()), 0, w.lines)
        verbs = w.verbs()
        stop = next(i for i, c in enumerate(verbs) if c[0] == "stop" and "jarvis-daemon2" in " ".join(c))
        start = next(i for i, c in enumerate(verbs)
                     if c[0] in ("enable", "start") and "jarvis-agent" in " ".join(c))
        self.assertLess(stop, start, verbs)
        self.assertFalse(w.enabled["jarvis-daemon2"], "the switch moves the enable away from v2")
        self.assertTrue(w.enabled["jarvis-agent"], "and onto v3, so a reboot keeps the choice")
        self.assertTrue(w.active["jarvis-agent"]) ; self.assertFalse(w.active["jarvis-daemon2"])
        self.assertGreater(w.now, 1000.0, "it waited for the real state, on the injected clock")
        text = "\n".join(w.lines)
        self.assertIn("jarvis-daemon2", text) ; self.assertIn("jarvis-agent", text)

    def test_v3_started_only_after_v2_is_down_and_port_free(self):
        from jarvis.v3 import switch
        w = World()
        seen = {}
        original = w.systemctl

        def watch(*args):
            if args[0] in ("enable", "start") and "jarvis-agent" in " ".join(args):
                seen["v2_down"] = not w._active("jarvis-daemon2") and w.port_free(8405)
            return original(*args)
        w.systemctl = watch
        self.assertEqual(switch.to_v3(w.env()), 0, w.lines)
        self.assertTrue(seen.get("v2_down"), "v3 must not start while v2 still holds its ports")

    def test_busy_v2_refuses_without_force(self):
        from jarvis.v3 import switch
        w = World(v2_busy=True)
        self.assertEqual(switch.to_v3(w.env()), 3, w.lines)
        self.assertEqual(w.verbs(), [], "nothing was stopped")
        self.assertTrue(any("--force" in line for line in w.lines))
        w2 = World(v2_busy=True)
        self.assertEqual(switch.to_v3(w2.env(), force=True), 0, w2.lines)

    def test_unknown_v2_state_refuses_without_force(self):
        from jarvis.v3 import switch
        w = World()
        w.v2_status = lambda: (None, "/status answered HTTP 500")
        self.assertEqual(switch.to_v3(w.env()), 3, w.lines)
        self.assertEqual(w.verbs(), [])

    def test_v3_never_comes_up(self):
        from jarvis.v3 import switch
        w = World(v3_boots=False)
        self.assertEqual(switch.to_v3(w.env(), wait=10.0), 1, w.lines)
        self.assertLessEqual(w.now - 1000.0, 10.0 + 5.0, "the deadline is honoured")
        text = "\n".join(w.lines)
        self.assertIn("journalctl --user -u jarvis-agent", text)
        self.assertIn("jarvis hud", text, "it says how to go back to v2")

    def test_unit_not_installed(self):
        from jarvis.v3 import switch
        w = World(v3_installed=False)
        self.assertEqual(switch.to_v3(w.env()), 1, w.lines)
        self.assertIn("jarvis agent install", "\n".join(w.lines))
        self.assertTrue(w.active["jarvis-daemon2"] or w.enabled["jarvis-daemon2"] or
                        any("jarvis hud" in l for l in w.lines),
                        "never leaves the owner with neither daemon and no way back")

    def test_already_on_v3_is_a_no_op(self):
        from jarvis.v3 import switch
        w = World(v2_active=False, v3_active=True)
        self.assertEqual(switch.to_v3(w.env()), 0, w.lines)
        self.assertEqual([c for c in w.verbs() if c[0] in ("stop", "disable")], [])


class ToV2(unittest.TestCase):
    def test_switch_back(self):
        from jarvis.v3 import switch
        w = World(v2_active=False, v3_active=True)
        self.assertEqual(switch.to_v2(w.env()), 0, w.lines)
        verbs = w.verbs()
        stop = next(i for i, c in enumerate(verbs) if c[0] in ("stop", "disable") and "jarvis-agent" in " ".join(c))
        start = next(i for i, c in enumerate(verbs)
                     if c[0] in ("enable", "start") and "jarvis-daemon2" in " ".join(c))
        self.assertLess(stop, start, verbs)
        self.assertFalse(w.enabled["jarvis-agent"]) ; self.assertTrue(w.enabled["jarvis-daemon2"])
        self.assertTrue(w.active["jarvis-daemon2"]) ; self.assertFalse(w.active["jarvis-agent"])

    def test_already_on_v2(self):
        from jarvis.v3 import switch
        w = World()
        self.assertEqual(switch.to_v2(w.env()), 0, w.lines)
        self.assertEqual([c for c in w.verbs() if c[0] == "stop"], [])

    def test_v2_never_comes_up(self):
        from jarvis.v3 import switch
        w = World(v2_active=False, v3_active=True, v2_boots=False)
        self.assertEqual(switch.to_v2(w.env(), wait=10.0), 1, w.lines)
        self.assertIn("journalctl --user -u jarvis-daemon2", "\n".join(w.lines))


class Install(unittest.TestCase):
    def test_writes_unit_reloads_and_does_not_enable(self):
        from jarvis.v3 import switch
        w = World()
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "systemd" / "user" / "jarvis-agent.service"
            repo = Path(tmp) / "repo"
            self.assertEqual(switch.install_unit(w.env(), unit_path=unit, python=f"{repo}/.venv/bin/python",
                                                 repo=repo), 0, w.lines)
            text = unit.read_text()
            self.assertIn(f"ExecStart={repo}/.venv/bin/python -m jarvis agentd", text)
            self.assertIn("Restart=on-failure", text)
            self.assertIn(f"WorkingDirectory={repo}", text)
            self.assertIn("WantedBy=default.target", text)
            self.assertIn(["daemon-reload"], w.calls)
            self.assertFalse(any(c[0] in ("enable", "start") for c in w.calls), "install never starts or enables")
            # idempotent
            before = unit.read_bytes()
            w.calls.clear()
            self.assertEqual(switch.install_unit(w.env(), unit_path=unit, python=f"{repo}/.venv/bin/python",
                                                 repo=repo), 0)
            self.assertEqual(unit.read_bytes(), before)
            self.assertTrue(any("unchanged" in line.lower() for line in w.lines))
            # a different existing file is backed up, not lost
            unit.write_text("[Service]\nExecStart=/bin/false\n")
            self.assertEqual(switch.install_unit(w.env(), unit_path=unit, python=f"{repo}/.venv/bin/python",
                                                 repo=repo), 0)
            backups = list(unit.parent.glob("jarvis-agent.service.bak*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("/bin/false", backups[0].read_text())


class Cli(unittest.TestCase):
    def test_agent_has_no_value_taking_options(self):
        # The owner-op detector reads `jarvis agent … start` by its words; an option value before
        # the action (`--wait 30 start`) would hide it (same rule as `jarvis hud`).
        import argparse
        from jarvis.v3 import cli
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cli.add_commands(sub)
        agent = sub.choices["agent"]
        for action in agent._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    for opt in subparser._actions:
                        if opt.option_strings and opt.nargs != 0 and not isinstance(opt, argparse._HelpAction):
                            self.fail(f"jarvis agent {name} {opt.option_strings} takes a value")
            elif action.option_strings and action.nargs != 0 and not isinstance(action, argparse._HelpAction):
                self.fail(f"jarvis agent {action.option_strings} takes a value")
        names = set(next(a for a in agent._actions if isinstance(a, argparse._SubParsersAction)).choices)
        self.assertLessEqual({"status", "start", "install", "stop", "restart"}, names)

    def test_gate_still_refuses_the_new_verbs(self):
        from jarvis import protected_state as ps
        for cmd in ("jarvis agent start", "jarvis agent start --force", "jarvis agent install",
                    "jarvis agent stop", "jarvis agent restart", "jarvis hud"):
            self.assertIsNotNone(ps.owner_op(cmd), cmd)
        self.assertIsNone(ps.owner_op("jarvis agent status"))


if __name__ == "__main__":
    unittest.main()

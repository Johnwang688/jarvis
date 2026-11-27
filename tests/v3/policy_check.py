"""Focused checks for I1 using only temporary owner state."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from shlex import quote
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from jarvis import config, permissions as v1, protected_state, rules, runtime, tools
from jarvis.tools import files, shell
from jarvis.v2 import permissions
from jarvis.v3.policy import Policy, set_rule


class PolicyCheck(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.home / "policy.json"
        self.policy = Policy(self.path)
        repo = Path(__file__).resolve().parents[2]
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"], "PYTHONPATH": str(repo)}

    def write(self, *rules, **fields):
        self.path.write_text(json.dumps({"version": 1, "rules": rules, **fields}))
        self.path.chmod(0o600)

    def decision(self, kind, **facts):
        return self.policy.explain({"kind": kind, "tainted": False, **facts})

    def test_every_hard_floor_beats_a_permissive_rule(self):
        cases = [
            ("credential.read", {}, "never"), ("credential.write", {}, "never"),
            ("policy.write", {}, "never"), ("gate.change", {}, "never"),
            ("school.submit", {}, "never"), ("data.delete_others", {}, "never"),
            ("mail.send", {}, "other-people"), ("mail.reply", {}, "other-people"),
            ("mail.forward", {}, "other-people"), ("calendar.invite", {"attendees": 0}, "other-people"),
            ("calendar.respond", {"attendees": 0}, "other-people"), ("share.doc", {}, "other-people"),
            ("calendar.block", {"attendees": 1}, "other-people"),
            ("money.spend", {}, "money"), ("vault.update", {"money": True}, "money"),
            ("research.start", {"tainted": True}, "tainted"),
            ("calendar.block_move", {"attendees": 0, "owner_event": True}, "owner-event"),
            ("calendar.event_change", {"attendees": 0, "owner_event": True}, "owner-event"),
            ("calendar.event_delete", {"attendees": 0, "owner_event": True}, "owner-event"),
            ("garden.water", {}, "unknown-kind"),
        ]
        self.write(*({"kind": kind, "level": "L0"} for kind, _, _ in cases), default_level="L0")
        for kind, facts, reason in cases:
            with self.subTest(kind=kind, reason=reason):
                self.assertEqual(self.decision(kind, **facts),
                                 {"level": "L3" if reason == "never" else "L2", "why": f"hard:{reason}"})

    def test_facts_require_exact_types(self):
        class FalseLike:
            def __bool__(self):
                raise AssertionError("Unexpected truthiness test")

            def __eq__(self, other):
                raise AssertionError("Unexpected equality test")

        self.write({"kind": "calendar.block_move", "level": "L0"},
                   {"kind": "thread.fix_test", "level": "L0", "when": {"verified": 0}})
        for bad in (0.0, "0", None, [], FalseLike()):
            with self.subTest(type=type(bad)):
                self.assertEqual(self.decision("vault.update", tainted=bad)["level"], "L2")
                self.assertEqual(self.decision("mail.draft", money=bad)["level"], "L2")
                self.assertEqual(self.decision("calendar.block", attendees=bad)["level"], "L2")
                self.assertEqual(self.decision("calendar.block_move", attendees=0, owner_event=bad)["level"], "L2")
                self.assertEqual(self.decision("thread.fix_test", verified=bad),
                                 {"level": "L1", "why": "default:thread.fix_test"})
        for bad in (False, True):
            self.assertEqual(self.decision("calendar.block", attendees=bad)["level"], "L2")
            self.assertEqual(self.decision("thread.fix_test", verified=bad),
                             {"level": "L1", "why": "default:thread.fix_test"})
        self.assertEqual(self.decision("vault.update", tainted=0)["level"], "L2")
        self.assertEqual(self.decision("mail.draft", money=0)["level"], "L2")
        self.assertEqual(self.decision("calendar.block_move", attendees=0, owner_event=0)["level"], "L2")
        self.assertEqual(self.decision("calendar.block_move", attendees=0)["level"], "L2")
        self.assertEqual(self.decision("thread.fix_test"), {"level": "L1", "why": "default:thread.fix_test"})
        self.assertEqual(self.decision("thread.fix_test", verified=0)["level"], "L0")
        self.assertEqual(self.decision("thread.fix_test", verified=1)["level"], "L1")

    def test_kinds_are_canonical(self):
        for kind in ("Mail.draft", "mail.Draft", " mail.draft", "mail.draft ", "mail.draft\n",
                     "mail.ｄraft", "maıl.draft", "mail.dra\u200bft", "mail.draft.extra"):
            with self.subTest(kind=repr(kind)):
                self.assertEqual(self.decision(kind)["level"], "L2")
                with self.assertRaises(ValueError):
                    set_rule(self.path, kind, "L0")
                self.write({"kind": kind, "level": "L0"})
                self.assertEqual(self.decision("mail.draft")["why"], "malformed-file")

    def test_ordered_rules_and_duplicate_json_keys(self):
        first = {"kind": "mail.draft", "level": "L1", "when": {"local": True}}
        last = {"kind": "mail.draft", "level": "L3"}
        self.write(first, first, {**first, "level": "L0"}, last)
        self.assertEqual(self.decision("mail.draft", local=True), {"level": "L1", "why": "file:0"})
        self.assertEqual(self.decision("mail.draft", local=False), {"level": "L3", "why": "file:3"})
        set_rule(self.path, "mail.draft", "L2", {"local": True})
        self.assertEqual(json.loads(self.path.read_bytes())["rules"], [{**first, "level": "L2"}, last])
        self.path.write_text('{"version":1,"rules":[{"kind":"mail.draft","level":"L3","level":"L0"}]}')
        self.assertEqual(self.decision("mail.draft"), {"level": "L2", "why": "malformed-file"})

    def test_undecidable_rule_can_only_raise_the_level(self):
        kind = "vault.update"
        conditional = {"kind": kind, "level": "L1", "when": {"sensitive": True, "label": "private"}}
        self.write(conditional)
        self.assertEqual(self.decision(kind, sensitive="true", label="private")["level"], "L2")
        self.assertEqual(self.decision(kind, sensitive="true", label="public")["level"], "L0")
        self.assertEqual(self.decision(kind, sensitive="true")["level"], "L0")
        self.write(conditional, {**conditional, "level": "L3"}, {"kind": kind, "level": "L1"})
        self.assertEqual(self.decision(kind, sensitive=1, label="private"), {"level": "L3", "why": "file:1"})
        self.write(conditional, {"kind": kind, "level": "L3"})
        self.assertEqual(self.decision(kind, sensitive=1, label="private"), {"level": "L3", "why": "file:1"})

    def test_file_replaced_between_calls(self):
        self.assertEqual(self.decision("mail.draft")["level"], "L0")
        self.write({"kind": "mail.draft", "level": "L1"})
        self.assertEqual(self.decision("mail.draft")["level"], "L1")
        other = self.home / "replacement.json"
        set_rule(other, "mail.draft", "L3")
        os.replace(other, self.path)
        self.assertEqual(self.decision("mail.draft")["level"], "L3")

    def test_parent_links_and_hard_links_fail_closed(self):
        self.write()
        link = self.home / "alias.json"
        os.link(self.path, link)
        self.assertEqual(self.decision("mail.draft")["why"], "malformed-file")
        link.unlink()
        link.symlink_to(self.home, target_is_directory=True)
        self.assertEqual(Policy(link / "policy.json").explain({"kind": "mail.draft", "tainted": False})["why"],
                         "malformed-file")

    def test_file_replaced_by_directory_or_fifo_does_not_block(self):
        self.write()
        self.assertEqual(self.decision("mail.draft")["level"], "L0")
        self.path.unlink()
        for make, remove in ((self.path.mkdir, self.path.rmdir),
                             (lambda: os.mkfifo(self.path, 0o600), self.path.unlink)):
            make()
            result = subprocess.run(
                [sys.executable, "-c",
                 "import sys; from jarvis.v3.policy import Policy, set_rule\n"
                 "path = sys.argv[1]\n"
                 "print(Policy(path).explain({'kind': 'mail.draft', 'tainted': False})['why'])\n"
                 "try: set_rule(path, 'mail.draft', 'L2')\n"
                 "except ValueError: print('write refused')",
                 str(self.path)], env=self.env, capture_output=True, text=True, timeout=5, check=True,
            )
            self.assertEqual(result.stdout.splitlines(), ["malformed-file", "write refused"])
            remove()
            self.write()
            self.path.unlink()

    def test_concurrent_writes_keep_every_rule(self):
        selectors = range(16)
        ready = Barrier(len(selectors), timeout=5)

        def write(selector):
            ready.wait()
            set_rule(self.path, "mail.draft", "L1", {"selector": selector})

        with ThreadPoolExecutor(max_workers=len(selectors)) as executor:
            list(executor.map(write, selectors))
        rules = json.loads(self.path.read_bytes())["rules"]
        self.assertEqual(len(rules), len(selectors))
        self.assertEqual({rule["when"]["selector"] for rule in rules}, set(selectors))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_cli_under_temporary_home(self):
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "jarvis", "policy", *args],
                                  env=self.env, text=True, capture_output=True, timeout=15)

        show = cli("show")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("mail.draft  L0  [default:mail.draft]", show.stdout)
        self.assertIn("mail.send  L2  [default:mail.send]  [hard:other-people floor L2]", show.stdout)
        self.assertIn("policy.write  L3  [hard:never]", show.stdout)
        location = self.home / ".local/share/jarvis/v3/policy.json"
        self.assertFalse(location.exists())
        change = cli("set", "mail.draft", "L1")
        self.assertEqual(change.returncode, 0, change.stderr)
        self.assertIn("Effective level L0 -> L1", change.stdout)
        self.assertIn("mail.draft  L1  [file:0]", cli("show").stdout)
        denied = cli("set", "mail.send", "L0")
        self.assertEqual(denied.returncode, 1, denied.stdout)
        self.assertIn("hard policy rule", denied.stderr)
        self.assertEqual(json.loads(location.read_bytes())["rules"][0]["kind"], "mail.draft")

    def test_existing_gate_refuses_agent_file_writes(self):
        with patch.dict(os.environ, {"HOME": str(self.home)}), patch.object(config, "V3_POLICY_FILE", self.path):
            set_rule(config.V3_POLICY_FILE, "mail.draft", "L2")
            self.assertTrue(files._self_protected(config.V3_POLICY_FILE))
            self.assertTrue(permissions.denied_file(str(config.V3_POLICY_FILE)))

    def test_cli_auto_round_trip_and_safe_file_defaults(self):
        def auto(setting):
            return subprocess.run([sys.executable, "-m", "jarvis", "policy", "auto", setting],
                                  env=self.env, text=True, capture_output=True, timeout=15)

        self.env["JARVIS_POLICY_FILE"] = str(self.path)
        self.assertEqual(auto("status").stdout.strip(), "Automatic running is off.")
        self.assertFalse(self.path.exists())
        self.write({"kind": "vault.update", "level": "L1"})
        self.assertEqual(auto("status").stdout.strip(), "Automatic running is on.")
        for setting in ("off", "on", "off"):
            result = auto(setting)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, auto("status").stdout)
            self.assertIs(json.loads(self.path.read_text())["auto"], setting == "on")
            self.assertEqual(self.decision("vault.update"), {"level": "L1", "why": "file:0"})
        set_rule(self.path, "vault.update", "L0")
        self.assertFalse(self.policy.auto)
        self.write(auto="on")
        before = self.path.read_bytes()
        self.assertFalse(self.policy.auto)
        self.assertEqual(auto("status").stdout.strip(), "Automatic running is off.")
        self.assertEqual(auto("on").returncode, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_policy_writer_is_denied_through_the_shared_gate(self):
        command = "jarvis policy set mail.file L2"
        launcher = quote(str(Path(sys.executable).with_name("jarvis")))
        commands = (
            command, f"uv run {command}", "python -m jarvis policy set mail.file L2",
            f"{launcher} policy set mail.file L2", f"env EXAMPLE=1 {command}",
            f"nice -n 5 {command}", f"timeout 5 {command}", f"bash -c {quote(command)}",
            "python -c 'import jarvis.v3.policy as p; p.set_rule(None, \"mail.file\", \"L2\")'",
            "python -c 'from jarvis.v3.cli import policy_set as change; change(args)'",
            "python -c 'import subprocess; subprocess.run([\"jarvis\", \"policy\", \"set\", \"mail.file\", \"L2\"])'",
            "python -c 'import sys, jarvis.__main__; sys.argv = [\"jarvis\", \"policy\", \"set\", \"mail.file\", \"L2\"]; "
            "jarvis.__main__.main()'",
            "jarvis policy auto on", "jarvis policy auto off",
            "uv run jarvis policy auto on", "python -m jarvis policy auto on",
            f"{launcher} policy auto off", "env EXAMPLE=1 jarvis policy auto on",
            "python -c 'from jarvis.v3.policy import set_auto; set_auto(None, True)'",
            "python -c 'from jarvis.v3.policy import _edit_file; _edit_file(None)'",
            "python -c 'from jarvis.v3.cli import policy_auto as change; change(args)'",
            "python -c 'import subprocess; subprocess.run([\"jarvis\", \"policy\", \"auto\", \"on\"])'",
            "python -c 'import sys, jarvis.__main__; sys.argv = [\"jarvis\", \"policy\", \"auto\", \"off\"]; "
            "jarvis.__main__.main()'",
        )
        allowlist = self.home / "allowlist.json"
        allowlist.write_text(json.dumps([
            {"tool": "run_command", "prefix": stem} for stem in ("jarvis", "uv", "python", "bash")
        ]))
        ask = Mock(return_value=True)
        with patch.object(config, "ALLOWLIST_PATH", allowlist), patch.object(shell, "_run") as run, \
                runtime.tools_bound({"run_command"}):
            self.assertTrue(v1.allows("run_command", {"command": "jarvis policy show"}))
            for line in commands:
                with self.subTest(line=line):
                    args = {"command": line, "reason": "policy gate test"}
                    self.assertIn("owner-only policy writer", protected_state.owner_op(line) or "")
                    self.assertEqual(v1.static_verdict(line).decision, rules.DENY)
                    self.assertIn("owner-only policy writer", permissions.denied_command(line) or "")
                    self.assertFalse(v1.allows("run_command", args))
                    for mode in ("ask", "all"):
                        with patch.object(v1, "_mode", mode):
                            result = tools.dispatch("run_command", json.dumps(args), approve=v1.gate(ask))
                            self.assertIn("Refused", result.text)
            ask.assert_not_called()
            run.assert_not_called()

    def test_policy_writer_neighbours_keep_their_verdicts(self):
        for line, expected in (
            ("jarvis policy show", rules.ASK), ("jarvis policy", rules.ASK),
            ("jarvis --help", rules.ASK), ("jarvis policy set --help", rules.ASK),
            ("jarvis policy auto status", rules.ASK), ("jarvis policy auto on --help", rules.ASK),
            ("cat policy", rules.ALLOW), ("python src/v3/policy.py", rules.ALLOW),
            ("echo jarvis policy set", rules.ALLOW),
            ("python -c 'from jarvis.v3.policy import set_rule; print(\"set_rule(None)\")'", rules.ASK),
            ("python -c 'import subprocess; subprocess.run(\"jarvis policy set mail.file L2\")'", rules.ASK),
            ("python -c 'import sys; sys.argv = [\"jarvis\", \"policy\", \"set\", \"mail.file\", \"L2\"]; "
             "print(sys.argv)'", rules.ASK),
            ("nohup " * 13 + "echo policy set; jarvis restart", rules.DENY),
        ):
            with self.subTest(line=line):
                self.assertEqual(v1.static_verdict(line).decision, expected)

    def test_cli_set_replaces_only_the_unconditional_rule_and_lists_each_rule(self):
        location = self.home / ".local/share/jarvis/v3/policy.json"
        location.parent.mkdir(parents=True)
        school = {"kind": "mail.file", "level": "L2", "when": {"label": "school"}}
        local = {"kind": "mail.file", "level": "L3", "when": {"private": True}}
        other = {"kind": "mail.label", "level": "L1"}
        location.write_text(json.dumps({"version": 1, "rules": [
            {"kind": "mail.file", "level": "L0"}, school, other, local,
        ]}))
        location.chmod(0o600)
        change = subprocess.run([sys.executable, "-m", "jarvis", "policy", "set", "mail.file", "L1"],
                                env=self.env, text=True, capture_output=True, timeout=15, check=True)
        self.assertEqual(change.stdout.splitlines()[1:], [
            'mail.file  L2  when {"label": "school"}', 'mail.file  L3  when {"private": true}', 'mail.file  L1',
        ])
        self.assertEqual(json.loads(location.read_bytes())["rules"], [
            school, other, local, {"kind": "mail.file", "level": "L1", "when": {}},
        ])


if __name__ == "__main__":
    unittest.main()

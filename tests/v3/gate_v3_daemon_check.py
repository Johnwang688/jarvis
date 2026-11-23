"""Exercise the shared owner-operation gate on strings. No command is executed."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, protected_state, rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rules_check as v1
from v2 import permissions_check as v2


# Most twins use the new unit, port and op word verbatim. A regex that names
# only part of the word needs the analogous fragment, and a numeric brace
# sequence over daemon2's final digit becomes a letter sequence for agentd's unit.
SPELLINGS = (
    ("jarvis-daemon{1..3}", "jarvis-agen{s..u}"),
    ("jarvis-daemon{2,}", "jarvis-agen{t,}"),
    ("jarvis-daem*", "jarvis-age*"),
    ("jarvis_2ddaemon2", "jarvis_2dagent"),
    ("jarvis-daemon2", "jarvis-agent"),
    ("daemon2 --force restart", "agent --force restart"),
    ("daemon2 restart", "agent restart"),
    ("daemon2", "agentd"),
    ("daemo[n]2", "agen[t]d"),
    ("daemo.{0,1}2", "agen.{0,1}d"),
    ("d{1,}aemon2", "a{1,}gentd"),
    ("daemon.", "agent."),
    ("daemon{2}", "agen{2}"),
    ("jarvis d'", "jarvis a'"),
    ("n2$", "td$"),
    ("jarvis.*.*.*d", "jarvis.*.*.*ag"),
    ("python.*.*.*.*2", "python.*.*.*.*td"),
    ("jarvis.*.*.*.*n", "jarvis.*.*.*.*nt"),
    ("8405", "8410"),
)


def twin(command: str) -> str:
    for old, new in SPELLINGS:
        command = command.replace(old, new)
    return command


DENIES = (
    *v1.OWNER_OPS_DENY, *v1.REVIEW_48_DENY, *v1.REVIEW_48_2_DENY, *v1.REVIEW_48_DELTA_DENY,
    *v1.RUNNER_DECIDE_DENY, *v2.REVIEW_48_V2_DENY, *v2.REVIEW_48_2_V2_DENY, *v2.REVIEW_48_DELTA_V2_DENY,
    "systemctl --user stop 'jarvis-daem*'",
    "U=jarvis-daemon2; systemctl --user show -p MainPID \"$U\" | xargs kill",
    "systemctl --user status jarvis-daemon2 | xargs kill",
    "ash -c 'jarvis restart'", "tcsh -c 'systemctl --user restart jarvis-daemon2'",
    "pkill -f '.*daemon2'",
    "pkill -f 'daemon2['",
    "kill $(pgrep -f 'daemon2[')",
)
ORDINARY = (
    *(line for line in v1.OWNER_OPS_ORDINARY if line not in ("jarvis hud", "jarvis hud --no-window")),
    *v1.REVIEW_48_ORDINARY, *v1.REVIEW_48_2_ORDINARY, *v1.REVIEW_48_DELTA_ORDINARY,
    *v1.REVIEW_48_DELTA_MAIN, *v2.REVIEW_48_V2_ORDINARY, *v2.REVIEW_48_2_V2_ORDINARY,
    *v2.REVIEW_48_DELTA_V2_ORDINARY,
    "systemctl --user stop jarvis-daemon2-other", "fuser -k 8411/tcp",
)
COSTLY = (*v1.REVIEW_48_DELTA_ASK, *v2.REVIEW_48_DELTA_V2_ASK)
# Explicitly empty or runtime-provided patterns/units and daemon-wide operations
# can reach either daemon. The legacy reason is retained in those cases.
AMBIGUOUS = ("n2$", "echo jarvis-daemon2 | xargs", "echo restart jarvis-daemon2 | xargs")


class GateParity(unittest.TestCase):
    def test_daemon_twins(self):
        # The existing cost fixtures pin this checkout path for matching work.
        with patch.object(config, "REPO_ROOT", Path("/home/u/projects/Jarvis")):
            for commands, expected in ((DENIES, rules.DENY), (ORDINARY, None), (COSTLY, rules.ASK)):
                for command in dict.fromkeys(commands):
                    other = twin(command)
                    with self.subTest(v2=command, v3=other):
                        old, new = rules.decide(command), rules.decide(other)
                        if expected is None:
                            self.assertNotEqual(old.decision, rules.DENY)
                        else:
                            self.assertEqual(old.decision, expected, old.reason)
                        self.assertEqual(new.decision, old.decision, new.reason)
                        if (command != other and "v2 daemon" in old.reason
                                and not any(marker in command for marker in AMBIGUOUS)):
                            self.assertIn("v3 daemon", new.reason)
                            self.assertIn("jarvis agentd", new.reason)

    def test_case_insensitive_pattern(self):
        for word, version in (("DAEMON2", "v2"), ("AGENTD", "v3")):
            with self.subTest(word=word):
                why = protected_state.owner_op(f"pkill -if {word}")
                self.assertIn(f"{version} daemon", why or "")

    def test_grep_filters_must_select_the_same_daemon(self):
        for command in ("ps aux | grep daemon2 | grep agentd | xargs kill",
                        "ss -ltnp | grep :8405 | grep :8410 | xargs kill"):
            with self.subTest(command=command):
                self.assertIsNone(protected_state.owner_op(command))

    def test_owner_operations(self):
        for verb in ("start", "install", "restart", "stop"):
            for command in (
                    f"jarvis agent {verb}", f"python -m jarvis agent {verb}",
                    f"uv run jarvis agent {verb}", f"sh -c 'jarvis agent {verb}'",
                    f"python -c \"import os; os.system('jarvis agent {verb}')\""):
                with self.subTest(command=command):
                    why = protected_state.owner_op(command)
                    self.assertIn("v3 daemon", why or "")
                    self.assertIn("decision A-2", why or "")
        for command in ("jarvis hud", "jarvis hud open", "jarvis hud --no-window",
                        "env A=1 nohup python -m jarvis hud", "uv run jarvis hud",
                        "sh -c 'jarvis hud open'", "echo arg | xargs jarvis hud open"):
            with self.subTest(command=command):
                self.assertEqual(rules.decide(command).decision, rules.DENY)
                self.assertIn("decision A-2", protected_state.owner_op(command) or "")
        for verb in ("install", "rebuild"):
            self.assertIn("decision C-1", protected_state.owner_op(f"jarvis hud {verb}") or "")
        self.assertEqual(rules.decide("echo open | xargs jarvis hud").decision, rules.DENY)
        for command in ("jarvis agent status", "jarvis agent", "jarvis agentd", "jarvis bench agent"):
            with self.subTest(command=command):
                self.assertIsNone(protected_state.owner_op(command))
                self.assertNotEqual(rules.decide(command).decision, rules.DENY)

    def test_v3_port_uses_shared_default_and_configured_ports(self):
        with patch.object(config, "V3_API_PORT", 18410):
            for port in (8410, 18410):
                for command in (f"fuser -k {port}/tcp", f"kill $(lsof -ti :{port})",
                                f"ss -ltnp | grep :{port} | xargs kill"):
                    with self.subTest(command=command):
                        self.assertIn("v3 daemon", protected_state.owner_op(command) or "")


if __name__ == "__main__":
    unittest.main()

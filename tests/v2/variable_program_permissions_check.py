"""GB-2b review backstop and literal-program parity under v1 and v2 AUTO."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from jarvis import permissions as v1_permissions, rules
from jarvis.v2 import permissions as v2_permissions
from jarvis.v2.model import PermissionProfile as PP, Role
from jarvis.v2.provider import Brief, Decision


class Asker:
    human_backed = True

    def __init__(self, decision=Decision.DENY):
        self.decision, self.seen = decision, []

    def ask(self, request):
        self.seen.append(request)
        return self.decision


def context(cwd, profile=PP.AUTO):
    return v2_permissions.PermitContext(
        thread_id="abcd1234", brief=Brief(role=Role.IMPLEMENTER, cwd=str(cwd), profile=profile),
        provider="claude")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def auto(self, command):
        with patch.object(v2_permissions, "log_decision") as log:
            asker = Asker()
            decision = v2_permissions.build_permit(context(self.root), asker)(
                "Bash", {"command": command})
            return decision, log.call_args.args[0].layer, len(asker.seen)


class ResolvedProgramKeepsGeneralRules(Base):
    # (variable form, literal form): the literal is refused at layer 1 or
    # always asked; the variable form must never reach the AUTO reviewer.
    VEXCOM_VIA_ESCALATION = (
        ('V=sudo; "$V" ./v*.exe --user', 'sudo ./v*.exe --user'),
        ('V=pkexec; "$V" ./v*.exe --user', 'pkexec ./v*.exe --user'),
        ('V=su; "$V" -c ./v*.exe', 'su -c ./v*.exe'),
        ('V=doas; "$V" ./v*.exe', 'doas ./v*.exe'),
    )
    OTHER_DENIES = (
        ('V=secret-tool; "$V" lookup service x', 'secret-tool lookup service x'),
        ('V=python3; "$V" -m keyring get a b', 'python3 -m keyring get a b'),
        ('V=busctl; "$V" --user call org.freedesktop.secrets /x a b',
         'busctl --user call org.freedesktop.secrets /x a b'),
        ('V=rm; "$V" -rf /', 'rm -rf /'),
        ('V=dd; "$V" if=/dev/zero of=/dev/sda', 'dd if=/dev/zero of=/dev/sda'),
        ('V=shutdown; "$V" now', 'shutdown now'),
    )
    ALWAYS_ASK = (
        ('V=gh; "$V" auth token', 'gh auth token'),
    )

    def check(self, pairs):
        for variable, literal in pairs:
            with self.subTest(variable):
                lit = self.auto(literal)
                self.assertNotEqual(lit[1], v2_permissions.L_REVIEWER, literal)
                got = self.auto(variable)
                self.assertNotEqual(
                    got[1], v2_permissions.L_REVIEWER,
                    f"{variable!r} reached the AUTO reviewer ({got}); literal gives {lit}")

    def test_vexcom_behind_an_escalation_stem_held_in_a_variable(self):
        self.check(self.VEXCOM_VIA_ESCALATION)

    def test_layer_one_denials_behind_a_variable_program(self):
        self.check(self.OTHER_DENIES)

    def test_always_ask_rules_behind_a_variable_program(self):
        self.check(self.ALWAYS_ASK)


class Rule6StillHolds(Base):
    """Standing coverage keeps the unresolved reading."""

    def test_allowlist_entry_gains_nothing(self):
        command = 'V=/opt/build-cli; "$V" build'
        with patch.object(v1_permissions, "load_allowlist", return_value=[
                {"tool": "run_command", "prefix": "V=/opt/build-cli"},
                {"tool": "run_command", "prefix": "$V"},
                {"tool": "run_command", "prefix": "/opt/build-cli"}]):
            self.assertFalse(v1_permissions.allows("run_command", {"command": command}))

    def test_gate_state_write_never_reaches_the_reviewer(self):
        # The unresolved reading asks. Once cp is known this is a certain
        # write to the allowlist, so the literal refusal takes precedence.
        line = 'V=cp; "$V" /tmp/x ~/.config/jarvis/allowlist.json'
        self.assertEqual(v2_permissions._shell_always_ask((line,), self.root)[0], "gate-state")
        self.assertEqual(self.auto(line), (Decision.DENY, v2_permissions.L_DENY, 0))
        self.assertEqual(v1_permissions.static_verdict(line, self.root).decision, rules.DENY)

    def test_new_owner_question_uses_original_text_without_always(self):
        line = 'V=gh; "$V" auth token'
        asker = Asker()
        with patch.object(v2_permissions, "log_decision"):
            v2_permissions.build_permit(context(self.root), asker)("Bash", {"command": line})
        request, = asker.seen
        self.assertEqual(request.command, line)
        self.assertEqual(request.args, {"command": line})
        self.assertFalse(request.allowlistable)


class LiteralProgramParity(Base):
    CASES = (*ResolvedProgramKeepsGeneralRules.VEXCOM_VIA_ESCALATION,
             *ResolvedProgramKeepsGeneralRules.OTHER_DENIES,
             *ResolvedProgramKeepsGeneralRules.ALWAYS_ASK,
             ('V=git; "$V" push --force origin main', 'git push --force origin main'),
             ('V=gh; "$V" pr merge 45', 'gh pr merge 45'),
             ('V=npx; "$V" vercel --prod', 'npx vercel --prod'),
             ('V=chmod; "$V" -R a+w /', 'chmod -R a+w /'),
             ('V=jarvis; "$V" restart', 'jarvis restart'),
             ('V=cp; "$V" /tmp/x .env', 'cp /tmp/x .env'),
             ('V=gh; if true; then "$V" auth token; fi', 'if true; then gh auth token; fi'),
             ('V=gh; echo "$(echo test)"; "$V" auth token',
              'echo "$(echo test)"; gh auth token'),
             ('V=gh; cat <<\'EOF\'\n"$V" auth token\nEOF\n"$V" auth token',
              'cat <<\'EOF\'\n"$V" auth token\nEOF\ngh auth token'),
             ('false && V=build; $V gh auth token', 'gh auth token'))

    def test_literal_parity_in_auto(self):
        rank = {v2_permissions.L_DENY: 2, v2_permissions.L_ALWAYS_ASK: 1}
        for variable, literal in self.CASES:
            with self.subTest(variable):
                literal_rank = rank.get(self.auto(literal)[1], 0)
                self.assertGreater(literal_rank, 0, literal)
                self.assertGreaterEqual(rank.get(self.auto(variable)[1], 0), literal_rank)

    def test_literal_parity_in_v1_decide(self):
        rank = {rules.DENY: 2, rules.ASK: 1, rules.ALLOW: 0}
        for variable, literal in self.CASES:
            with self.subTest(variable):
                # Some v2 always-ask rules, such as vercel-prod, are v2-only.
                literal_rank = rank[rules.decide(literal).decision]
                self.assertGreaterEqual(rank[rules.decide(variable).decision], literal_rank)

    def test_hash_stops_proving_the_variable_program(self):
        line = 'V=build; hash -p "$P" build; $V'
        self.assertIn("cannot be told", rules.vex_uncoverable(line) or "")
        self.assertEqual(self.auto(line), (Decision.DENY, v2_permissions.L_ALWAYS_ASK, 1))

    def test_rewrite_only_changes_proven_program_words(self):
        line = ("V='/opt/build cli'; printf '%s' \"$V\" \"$(echo text)\"; "
                "cat <<'EOF'\n\"$V\" body\nEOF\n"
                "W=other \"${V}\" compile; \"$\\\nV\" check")
        expected = ("V='/opt/build cli'; printf '%s' \"$V\" \"$(echo text)\"; "
                    "cat <<'EOF'\n\"$V\" body\nEOF\n"
                    "W=other '/opt/build cli' compile; '/opt/build cli' check")
        self.assertEqual(rules.literal_program_readings(line), (expected,))

    def test_failed_rewrite_keeps_the_vex_question(self):
        line = 'V=/opt/build-cli; "$V" rewrite-error'
        with patch.object(rules, "_literal_readings", side_effect=ValueError):
            self.assertIn("cannot be told", rules.vex_uncoverable(line) or "")
            self.assertEqual(self.auto(line), (Decision.DENY, v2_permissions.L_ALWAYS_ASK, 1))

    def test_failed_extra_check_keeps_the_vex_question(self):
        line = 'V=/opt/build-cli; "$V" judge-error'
        judge = v2_permissions.always_ask_match

        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                def fail_on_extra(*args, **kwargs):
                    if not kwargs.get("command_readings"):
                        return judge(*args, **kwargs)
                    if exhausted:
                        return "too-complex", rules.RUNNERS_TOO_COMPLEX
                    raise ValueError

                with (patch.object(v2_permissions, "always_ask_match", side_effect=fail_on_extra),
                      patch.object(v2_permissions, "log_decision") as log):
                    asker = Asker()
                    v2_permissions.build_permit(context(self.root), asker)("Bash", {"command": line})
                    self.assertIn("cannot be told", log.call_args.args[0].reason)
                    self.assertEqual(len(asker.seen), 1)

    def test_failed_v1_check_keeps_the_vex_question(self):
        line = 'V=/opt/build-cli; "$V" judge-error'
        judge = rules._decide

        def fail_on_extra(text, **kwargs):
            if text != line:
                raise ValueError
            return judge(text, **kwargs)

        with patch.object(rules, "_decide", side_effect=fail_on_extra):
            verdict = rules.decide(line)
        self.assertEqual(verdict.decision, rules.ASK)
        self.assertIn("cannot be told", verdict.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)

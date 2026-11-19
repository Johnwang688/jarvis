"""GB-1 detector regressions. Command strings are judged in-process, never run."""
from __future__ import annotations

import atexit
import json
import os
import tempfile
import unittest
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="heredoc-layer2-home-")
_HOME_ENV = patch.dict(os.environ, {"HOME": _HOME.name})
_HOME_ENV.start()
atexit.register(_HOME.cleanup)
atexit.register(_HOME_ENV.stop)

from jarvis import permissions as v1, protected_state, rules, runtime, tools  # noqa: E402
from jarvis.v2 import merge_gate, permissions as v2  # noqa: E402


def rule_of(command):
    hit = v2.always_ask_match("Bash", {"command": command})
    return hit[0] if hit else None


# One body per rule isolates the verdict even when an earlier rule could win.
# The gate-state example uses a non-allowlist path because that one can ASK.
MARKERS = {
    "gate-state": "echo demo > ~/.config/jarvis/models.json",
    "git-push-force": "git push --force origin topic",
    "git-push-protected": "git push origin main",
    "credentials-path": "cat ~/.ssh/id_rsa",
    "credentials-inner": "sh -c $(true)",
}

# The routes from the HD-0 review. Use placeholders so the same routes are
# tested against every rule, with the appropriate Makefile recipe indentation.
EXECUTORS = {
    "group piped to sh": "{ cat <<'EOF'; } | sh\n<BODY>\nEOF",
    "redirect to process substitution": "cat <<'EOF' > >(sh)\n<BODY>\nEOF",
    "tee to process substitution": "tee >(sh) <<'EOF'\n<BODY>\nEOF",
    "function": "cat() { sh; }; cat <<'EOF'\n<BODY>\nEOF",
    "alias": "shopt -s expand_aliases\nalias cat=sh\ncat <<'EOF'\n<BODY>\nEOF",
    "path to cat": "./cat <<'EOF'\n<BODY>\nEOF",
    "PATH change": "PATH=/tmp/other:$PATH cat <<'EOF'\n<BODY>\nEOF",
    "write then run": "cat > scratch.sh <<'EOF'\n<BODY>\nEOF\nsh scratch.sh",
    "interpreter reading stdin": (
        "python3 -c 'import os,sys; os.system(sys.stdin.read())' <<'EOF'\n<BODY>\nEOF"
    ),
    "make": "cat > Makefile <<'EOF'\nall:\n\t<BODY>\nEOF\nmake",
    "awk system": (
        "cat > scratch.sh <<'EOF'\n<BODY>\nEOF\n"
        """awk 'BEGIN{while((getline l < "scratch.sh")>0) system(l)}'"""
    ),
    "at -f": "cat > scratch.sh <<'EOF'\n<BODY>\nEOF\nat -f scratch.sh now",
    "taskset dash": "cat > scratch.sh <<'EOF'\n<BODY>\nEOF\ntaskset 1 dash scratch.sh",
    "program read from file": (
        "cat > scratch.sh <<'EOF'\n<BODY>\nEOF\nwhile read -r l; do $l; done < scratch.sh"
    ),
}


class HeredocLayer2(unittest.TestCase):
    def assert_rule(self, command, rule):
        if rule == "gate-state":
            touch = protected_state.command_touch(command)
            self.assertIsNotNone(touch)
            self.assertEqual(touch.name, "models.json")
            self.assertFalse(protected_state.denies(touch))
        else:
            self.assertEqual(rule_of(command), rule)

    def test_quoted_scripts_use_separate_gate_state_checks(self):
        for command, reason in (
            ("sed -i 's|x|$(echo ~/.config/jarvis/models.json)|' src/x.hpp",
             "in a substitution in a line that writes"),
            ("""node -e 'console.log("models.json", 1 > 0)'""", "node source that names models.json"),
            ("""python3 -c 'print("models.json", 1 > 0)'""", "python3 source that names models.json"),
        ):
            with self.subTest(command=command):
                self.assertEqual(rules.segments(command), [command])
                self.assertEqual(rules.redirections(command), [])
                touch = protected_state.command_touch(command)
                self.assertIsNotNone(touch)
                self.assertIn(reason, touch.why)
                self.assertFalse(touch.certain)

    def test_data_bodies_skip_each_shell_rule(self):
        for rule, line in MARKERS.items():
            with self.subTest(rule=rule):
                command = f"cat > notes.txt <<'EOF'\n{line}\nEOF"
                self.assertIsNone(rule_of(command))
                self.assertIsNone(protected_state.command_touch(command))

    def test_execution_routes_keep_each_rule(self):
        for route, template in EXECUTORS.items():
            for rule, line in MARKERS.items():
                with self.subTest(route=route, rule=rule):
                    self.assert_rule(template.replace("<BODY>", line), rule)

    def test_inner_shell_reading_skips_data_and_keeps_unreadable_wrappers(self):
        data = "cat > notes.txt <<'EOF'\nsh -c $(true)\nEOF"
        self.assertEqual(v2._inner_segments([data]), [])
        self.assertIsNone(v2._inner_segments([data + "\nsh -c $(true)"]))
        # The existing inner-word parser declines multiline -c arguments.
        self.assertEqual(rule_of(f'sh -c "{data}"'), "credentials-inner")

    def test_real_actions_remain_visible(self):
        for rule, line in MARKERS.items():
            with self.subTest(rule=rule):
                self.assert_rule(line, rule)
                self.assert_rule(line + "; cat <<'EOF'\nordinary data\nEOF", rule)
                self.assert_rule("cat <<'EOF'\nordinary data\nEOF\n" + line, rule)

    def test_operator_redirect_is_still_refused_in_mode_all(self):
        command = "cat > ~/.config/jarvis/allowlist.json <<'EOF'\n[]\nEOF"
        touch = protected_state.command_touch(command)
        self.assertTrue(protected_state.denies(touch))
        self.assertIsNotNone(v2.denied_command(command))
        self.assertEqual(v1.static_verdict(command).decision, rules.DENY)
        previous_mode = v1.mode()
        self.addCleanup(v1.set_mode, previous_mode)
        v1.set_mode("all")
        with runtime.tools_bound({"run_command"}), patch(
            "jarvis.tools.shell._run", side_effect=AssertionError("must never run")
        ) as run:
            approve = v1.gate(lambda *_: self.fail("must never ask"))
            out = tools.dispatch("run_command", json.dumps({"command": command, "reason": "test"}),
                                 approve=approve)
            self.assertIn("Refused", out.text)
            self.assertIn("allowlist.json", out.text)
            run.assert_not_called()

    def test_ansi_c_decoding_cannot_hide_outer_redirects(self):
        data = "cat <<'EOF'"
        for command in (
            data + " > ~/.config/jarvis/allowlist.json\n  $'\nEOF",
            data + "\n  $'\nEOF\necho x > ~/.config/jarvis/allowlist.json",
            data + r" > ~/.config/jarvis/$'allow\x6cist.json'" + "\n  $'\nEOF",
        ):
            with self.subTest(command=command):
                self.assertTrue(protected_state.denies(protected_state.command_touch(command)))
                self.assertIsNotNone(v2.denied_command(command))
                self.assertEqual(v1.static_verdict(command).decision, rules.DENY)

    def test_real_env_redirect_and_interpreter_write_are_refused(self):
        for command in (
            "cat > .env.production <<'EOF'\nsetting=test\nEOF",
            "python3 - <<'EOF'\nfrom pathlib import Path\n"
            "Path('~/.config/jarvis/allowlist.json').expanduser().write_text('[]')\nEOF",
        ):
            with self.subTest(command=command):
                self.assertTrue(protected_state.denies(protected_state.command_touch(command)))
                self.assertIsNotNone(v2.denied_command(command))
                self.assertEqual(v1.static_verdict(command).decision, rules.DENY)

    def test_file_edit_and_commit_message_shapes(self):
        python = (
            "cd repo && python3 - <<'EOF'\n"
            "from pathlib import Path\n"
            "Path('src/x.js').write_text(\"\"\"\n"
            "/* multiplication: a * b */\n"
            "const bigger = (a, b) => a > b;\n"
            "// Document git push --force for the owner.\n"
            "\"\"\")\nEOF"
        )
        cpp = ("cat > src/x.hpp <<'EOF'\n/* choose the bigger value */\n"
               "static Foo* pick(Foo* a, Foo* b) { return a > b ? a : b; }\nEOF")
        message = "git commit -q -F - <<'EOF'\nDocument the comparison\n\n- Add the C++ example.\nEOF"
        for command in (python, cpp, message):
            with self.subTest(command=command):
                self.assertIsNone(rule_of(command))
                self.assertIsNone(protected_state.command_touch(command))
                self.assertIsNone(rules.vex_uncoverable(command))
        self.assertIsNone(merge_gate.judge(message))
        # The merge gate's source backstop deliberately applies to Python.
        self.assertEqual(merge_gate.judge(python).rule, "git-push-force")
        # Git can run hooks. Its stdin does not meet the HD-0 program guard.
        self.assertEqual(rule_of(message.replace("- Add the C++ example.", MARKERS["git-push-force"])),
                         "git-push-force")


if __name__ == "__main__":
    unittest.main()

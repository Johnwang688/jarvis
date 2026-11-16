"""`credentials-read` and here-document bodies (HD-0, 2026-11-16). Free,
hermetic, no live daemon.

A Claude sub-agent's read-only log check was carded `always-ask rule
credentials-read`: its `python3 - <<'EOF'` body held the Python line `pass`,
and layer 2 split the raw line, so `pass` was judged as the password store
(`_CREDENTIAL_READ_STEMS`). Now `credentials-read` reads a line with its data
bodies set aside (`permissions._heredoc_data_reading`): a body fed to a
`HEREDOC_DATA_CONSUMERS` program or a Python, not piped on, nothing live in
it. Anything else keeps the raw reading, so every line asked on main is
still asked. The owner deferred the same change for the other layer-2 rules
(docs/plans/2026-11-16-heredoc-bodies-plan.md §10).
"""
from __future__ import annotations

import atexit
import os
import tempfile
import unittest
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="heredoc-credread-home-")
_HOME_ENV = patch.dict(os.environ, {"HOME": _HOME.name})
_HOME_ENV.start()
atexit.register(_HOME.cleanup)
atexit.register(_HOME_ENV.stop)

from jarvis import rules  # noqa: E402
from jarvis.v2 import permissions  # noqa: E402

# The reported line, its worktree path shortened.
REPORTED = (
    'cd /tmp/wt && grep -n "lx:" build-sim/b1.log | head; '
    'grep -n "TIMED OUT" build-sim/stage4/run.log | wc -l; python3 - <<\'EOF\'\n'
    "import csv,re\nfor f in ['build-sim/s1.log']:\n    pass\nEOF\n"
    'grep -c "TIMED OUT" build-sim/s1.log'
)

# Data bodies: no longer asked by credentials-read.
DATA = [
    REPORTED,
    "python3 - <<'EOF'\nfor f in []:\n    pass\nEOF",
    "python3 <<'PY'\nclass A:\n    pass\nPY",
    "/usr/bin/python3.14 - <<'EOF'\npass\nEOF",
    "python3 - <<'EOF'\ngit config --list\nEOF",
    "python3 - <<'EOF'\ngit credential fill\nEOF",
    "cat > notes.txt <<'EOF'\npass\nEOF",
    "cat <<'EOF' > notes.txt\npass show github\nEOF",
    "cat >> notes.txt <<-'EOF'\n\tpass\n\tEOF",
    "tee notes.txt <<'EOF' >/dev/null\npass\nEOF",
    "node - <<'EOF'\nconst pass = 1; pass\nEOF",
    "wc -l <<'EOF'\npass\nEOF",
    "cat <<EOF > notes.txt\npass\nEOF",                       # unquoted, nothing live
    "cat > a.txt <<'A'\npass\nA\ncat > b.txt <<'B'\npass\nB",   # two, both data
    "cat <<'EOF' > x.sh\necho $(pass show x) `pass`\nEOF",    # quoted: literal text
    "cat <<'EOF' || true\npass\nEOF",                         # `||` is not a pipe
]

# Asked on main and still asked, by the same rule.
STILL_ASKED = {
    "pass show github": "credentials-read",
    "bash <<'EOF'\npass show github\nEOF": "credentials-read",
    "sh <<EOF\npass show github\nEOF": "credentials-read",
    "zsh <<'EOF'\npass show x\nEOF": "credentials-read",
    "bash -s <<'EOF'\ngit credential fill\nEOF": "credentials-read",
    "env bash <<'EOF'\npass show x\nEOF": "credentials-read",
    "uv run bash <<'EOF'\npass show x\nEOF": "credentials-read",
    "source /dev/stdin <<'EOF'\npass show x\nEOF": "credentials-read",
    ". /dev/stdin <<'EOF'\npass show x\nEOF": "credentials-read",
    "ssh host <<'EOF'\npass show x\nEOF": "credentials-read",
    "xargs -I{} sh -c {} <<'EOF'\npass show x\nEOF": "credentials-read",
    "$RUN <<'EOF'\npass show x\nEOF": "credentials-read",
    # a data program, but its output goes on
    "cat <<'EOF' | sh\npass show x\nEOF": "credentials-read",
    "cat <<'EOF' | bash -s\npass show x\nEOF": "credentials-read",
    "cat <<'EOF' |& sh\npass show x\nEOF": "credentials-read",
    "cat <<'EOF' | tee f | sh\npass show x\nEOF": "credentials-read",
    # one body data, one fed to a shell: the whole line keeps the raw reading
    "cat > a.txt <<'A'\nhi\nA\nbash <<'B'\npass show x\nB": "credentials-read",
    # an unquoted body with something live in it
    "cat <<EOF\n$(true)\npass show x\nEOF": "credentials-read",
    "cat <<EOF\n`true`\npass show x\nEOF": "credentials-read",
    # a data body beside a real read
    "cat > n.txt <<'EOF'\nhi\nEOF\npass show github": "credentials-read",
    "python3 - <<'EOF'\npass\nEOF\ngit credential fill": "credentials-read",
    # a body with no terminator runs to the end, as in bash
    "bash <<'EOF'\npass show x": "credentials-read",
    # other credential rules are untouched
    "eval \"$(cat <<'EOF'\npass show x\nEOF\n)\"": "credentials-inner",
    "bash <<'EOF'\ncat ~/.ssh/id_rsa\nEOF": "credentials-path",
}


def rule_of(command: str) -> str | None:
    match = permissions.always_ask_match("Bash", {"command": command})
    return match[0] if match else None


class HeredocCredentialsRead(unittest.TestCase):
    def test_data_bodies_are_not_credential_reads(self):
        for command in DATA:
            with self.subTest(command=command):
                self.assertNotEqual(rule_of(command), "credentials-read")
                self.assertIsNotNone(permissions._heredoc_data_reading(command))

    def test_reported_line_is_not_asked(self):
        self.assertIsNone(rule_of(REPORTED))

    def test_shell_fed_and_unsure_bodies_are_still_asked(self):
        for command, rule in STILL_ASKED.items():
            with self.subTest(command=command):
                self.assertEqual(rule_of(command), rule)

    def test_multi_field_reading_still_judges_each_field(self):
        # `command_field_checks` (#211): a body left open in one field must
        # not hide the next field's credential read.
        for args in ({"command": "cat <<EOF", "script": "pass show github"},
                     {"command": "python3 - <<'EOF'", "script": "git credential fill"}):
            with self.subTest(args=args):
                match = permissions.always_ask_match("Bash", args)
                self.assertEqual(match[0] if match else None, "credentials-read")

    def test_an_unsure_inlining_keeps_the_raw_reading(self):
        def unsure(text, keep=True, bodies=None, unsure=None):
            raise rules.TooComplex
        with patch.object(rules, "_inline_heredocs", unsure):
            self.assertIsNone(permissions._heredoc_data_reading(REPORTED))
            self.assertEqual(rule_of(REPORTED), "credentials-read")

        def flagged(text, keep=True, bodies=None, unsure=None):
            unsure.append("flagged")
            return text.replace("<<", "<<<", 1)
        with patch.object(rules, "_inline_heredocs", flagged):
            self.assertIsNone(permissions._heredoc_data_reading(REPORTED))

    def test_the_reading_refuses_what_it_cannot_place(self):
        # The reading's own contract. Today the verdict is the same either
        # way (layer 2 reads no substitution or here-string at all: the
        # plan's gap G), so the always-ask outcome cannot show these.
        for command in (
                # a body opened inside a substitution: not placed at segment level
                "echo \"$(bash <<'EOF'\npass show x\nEOF\n)\"",
                # a here-string that opens single-quoted but has something live after
                "cat > n <<'EOF'\nhi\nEOF\ncat <<< 'a'\"$(pass show x)\"",
                "cat > n <<'EOF'\nhi\nEOF\ncat <<< \"$(pass show x)\"",
                # a here-string written as one, fed to a shell
                "cat > n <<'EOF'\nhi\nEOF\nbash <<< 'pass show x'"):
            with self.subTest(command=command):
                self.assertIsNone(permissions._heredoc_data_reading(command))

    def test_the_table_lists_no_program_that_runs_its_input(self):
        runners = rules.SHELLS | {"source", ".", "eval", "xargs", "ssh", "env", "uv", "npx",
                                  "sudo", "su", "exec", "nohup", "timeout", "parallel"}
        self.assertFalse(permissions.HEREDOC_DATA_CONSUMERS & runners)

    def test_widening_the_table_to_a_shell_is_caught(self):
        # Mutation evidence inside the suite: a table holding `bash` would
        # let a shell-fed body through, and this check would see it.
        wide = permissions.HEREDOC_DATA_CONSUMERS | {"bash", "sh"}
        with patch.object(permissions, "HEREDOC_DATA_CONSUMERS", wide):
            self.assertIsNone(rule_of("bash <<'EOF'\npass show github\nEOF"))


if __name__ == "__main__":
    unittest.main(verbosity=1)

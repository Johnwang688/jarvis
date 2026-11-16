"""`credentials-read` and here-document bodies (HD-0, 2026-11-16). Free,
hermetic, no live daemon.

A Claude sub-agent's read-only log check was carded `always-ask rule
credentials-read`: its `python3 - <<'EOF'` body held the Python line `pass`,
and layer 2 split the raw line, so `pass` was judged as the password store
(`_CREDENTIAL_READ_STEMS`). Now `credentials-read` reads a line with its data
bodies set aside (`permissions._heredoc_data_reading`), but only on a line
where every program is inert (`HEREDOC_INERT_PROGRAMS`) or is the
`python3 -`/`node -` a body is the program of, and nothing live, grouped or
renamed is on it. Anything else keeps the raw reading, so every line asked
on main is still asked. The owner deferred the same change for the other layer-2 rules
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
    "python3.14 -u - <<'EOF'\npass\nEOF",
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
    "cat <<'EOF' | tee out.txt | wc -l\npass\nEOF",           # piped to inert programs
    "mkdir -p d && cd d && cat > n.txt <<'EOF'\npass\nEOF\nls; pwd",
    "grep -rn --include='*.py' -E 'a|b' . <<'EOF'\npass\nEOF",  # ordinary grep options
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
    # The review of #396: every route below takes a body to a shell.
    # a compound command piped on from its closing word
    "{ cat <<'EOF'; } | sh\npass show github\nEOF": "credentials-read",
    "{ cat <<'EOF'\npass show github\nEOF\n} | sh": "credentials-read",
    "{ cat; } <<'EOF' | sh\npass show github\nEOF": "credentials-read",
    "( cat <<'EOF' ) | sh\npass show github\nEOF": "credentials-read",
    "if true; then cat <<'EOF'; fi | sh\npass show github\nEOF": "credentials-read",
    "for i in 1; do cat <<'EOF'; done | bash\npass show github\nEOF": "credentials-read",
    "case x in x) cat <<'EOF';; esac | sh\npass show github\nEOF": "credentials-read",
    # output into a shell through a process substitution or an fd
    "cat <<'EOF' > >(sh)\npass show github\nEOF": "credentials-read",
    "tee >(sh) <<'EOF'\npass show github\nEOF": "credentials-read",
    "tee -a >(sh) <<'EOF'\npass show github\nEOF": "credentials-read",
    "exec 3> >(sh)\ncat <<'EOF' >&3\npass show github\nEOF": "credentials-read",
    "exec > >(sh)\ncat <<'EOF'\npass show github\nEOF": "credentials-read",
    "exec 4> >(bash); cat <<'EOF' >/dev/fd/4\npass show github\nEOF": "credentials-read",
    "coproc sh\ncat <<'EOF' >&${COPROC[1]}\npass show github\nEOF": "credentials-read",
    # a name that is not the program it says
    "cat() { sh; }; cat <<'EOF'\npass show github\nEOF": "credentials-read",
    "function grep { bash; }\ngrep x <<'EOF'\npass show github\nEOF": "credentials-read",
    "shopt -s expand_aliases\nalias cat=sh\ncat <<'EOF'\npass show github\nEOF": "credentials-read",
    "ln -sf /bin/sh ./cat; ./cat <<'EOF'\npass show github\nEOF": "credentials-read",
    "/tmp/evil/cat <<'EOF'\npass show github\nEOF": "credentials-read",
    "PATH=/tmp/evil:$PATH cat <<'EOF'\npass show github\nEOF": "credentials-read",
    "export PATH=/tmp/evil\ncat <<'EOF'\npass show github\nEOF": "credentials-read",
    # written, then run on the same line
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\nsh /tmp/x.sh": "credentials-read",
    "tee /tmp/x.sh <<'EOF'\npass show github\nEOF\nsource /tmp/x.sh": "credentials-read",
    "cat > x.sh <<'EOF'\npass show github\nEOF\nchmod +x x.sh; ./x.sh": "credentials-read",
    # an interpreter told to run its stdin
    "python3 -c 'import os,sys; os.system(sys.stdin.read())' <<'EOF'\npass show github\nEOF":
        "credentials-read",
    "node -e 'require(\"child_process\").execSync(require(\"fs\").readFileSync(0))' <<'EOF'\n"
    "pass show github\nEOF": "credentials-read",
    # The second review of #396: executors no deny-list held. W writes the
    # body to a file; the line then runs it without naming a shell.
    "cat > Makefile <<'EOF'\nall:\n\tpass show github\nEOF\nmake": "credentials-read",
    "tee /tmp/mk >/dev/null <<'EOF'\nall:\n\tpass show github\nEOF\nmake -f /tmp/mk":
        "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\ntaskset 1 dash /tmp/x.sh": "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\nstrace -f sh /tmp/x.sh": "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\nunshare -r sh /tmp/x.sh": "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\ndocker exec c sh /tmp/x.sh": "credentials-read",
    "cat <<'EOF' > /tmp/l\npass show github\nEOF\nwhile read -r l; do $l; done < /tmp/l":
        "credentials-read",
    "cat <<'EOF' > /tmp/l\npass show github\nEOF\nread -r x < /tmp/l; $x": "credentials-read",
    "cat <<'EOF' > /tmp/l\npass show github\nEOF\nprintf -v c sh; $c /tmp/l": "credentials-read",
    "cat <<'EOF' > /tmp/l\npass show github\nEOF\n$'\\x73h' /tmp/l": "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\n"
    "awk 'BEGIN{while((getline l < \"/tmp/x.sh\")>0) system(l)}'": "credentials-read",
    "cat > /tmp/x.sh <<'EOF'\npass show github\nEOF\nat -f /tmp/x.sh now": "credentials-read",
    "cat > .git/hooks/pre-commit <<'EOF'\npass show github\nEOF\ngit commit -m x": "credentials-read",
    "python3 x.py <<'EOF'\npass show github\nEOF": "credentials-read",
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

        real = rules._inline_heredocs
        clean = "cat > notes.txt <<'EOF'\npass\nEOF"
        self.assertIsNotNone(permissions._heredoc_data_reading(clean))

        def flagged(text, keep=True, bodies=None, unsure=None):
            out = real(text, keep=keep, bodies=bodies, unsure=unsure)
            unsure.append("flagged")
            return out
        with patch.object(rules, "_inline_heredocs", flagged):
            self.assertIsNone(permissions._heredoc_data_reading(clean))

    def test_each_guard_alone(self):
        # One line per guard that only that guard refuses (the others would
        # let it through), so removing any one of them fails here. The
        # outcome a card shows can be the same either way: these pin the
        # reading's own contract.
        for guard, command in (
                ("syntax", "cat <<'EOF' > a.txt; { true; }\nhi\nEOF"),
                ("live in double quotes", "cat <<'EOF' \"$(true)\"\nhi\nEOF"),
                ("PATH=", "PATH=/tmp/evil cat <<'EOF'\nhi\nEOF"),
                ("compound closer", "if true; then cat <<'EOF'; fi\nhi\nEOF"),
                ("program not inert", "cat <<'EOF' > a.txt\nhi\nEOF\nsort a.txt"),
                ("program by path", "cat <<'EOF' > a.txt\nhi\nEOF\n./a.txt"),
                ("python without a body", "cat <<'EOF' > a.txt\nhi\nEOF\npython3 -"),
                ("python -c", "python3 -c 'print(1)' <<'EOF'\nhi\nEOF"),
                ("node -e", "node -e '1' <<'EOF'\nhi\nEOF"),
                ("fd duplication", "cat <<'EOF' >&2\nhi\nEOF"),
                ("fd path redirect", "cat <<'EOF' >/dev/fd/4\nhi\nEOF"),
                ("fd path argument", "tee /dev/fd/4 <<'EOF'\nhi\nEOF"),
                ("unplaced here-string", "cat > n <<'EOF'\nhi\nEOF\necho 'a <<< b'"),
                ("ugrep filter", "grep --filter='*:sh' x <<'EOF'\nhi\nEOF"),
                ("ugrep pager", "cat > a <<'EOF'\nhi\nEOF\ngrep --pager=sh x a"),
                ("ugrep config", "grep --config=/tmp/c x <<'EOF'\nhi\nEOF"),
                ("ugrep ---", "grep --- x <<'EOF'\nhi\nEOF"),
                ("ugrep query", "grep -nQ x <<'EOF'\nhi\nEOF"),
                ("rg-style pre", "grep --pre=sh x <<'EOF'\nhi\nEOF")):
            with self.subTest(guard=guard):
                self.assertIsNone(permissions._heredoc_data_reading(command))

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
        runners = rules.SHELLS | {"source", ".", "eval", "exec", "xargs", "ssh", "env", "uv", "npx",
                                  "sudo", "su", "nohup", "timeout", "parallel", "make", "awk",
                                  "sed", "find", "sort", "at", "batch", "git", "taskset", "strace",
                                  "python3", "node", "perl"}
        self.assertFalse(permissions.HEREDOC_INERT_PROGRAMS & runners)

    def test_a_program_added_to_the_list_is_what_lets_a_line_through(self):
        # The list is the only gate for the line's other programs: with
        # `make` on it, the Makefile route opens (and without it, closes).
        line = "cat > Makefile <<'EOF'\nall:\n\tpass show github\nEOF\nmake"
        self.assertEqual(rule_of(line), "credentials-read")
        wide = permissions.HEREDOC_INERT_PROGRAMS | {"make"}
        with patch.object(permissions, "HEREDOC_INERT_PROGRAMS", wide):
            self.assertIsNone(rule_of(line))


if __name__ == "__main__":
    unittest.main(verbosity=1)

"""PR #388, re-check 3: Unicode casefold and raw-name call-site probes.

Hermetic: round one's isolated imports, temporary HOME/project, fake contents.
Only this new file is changed. The accepted option-tail cost, file -f lists,
NTFS streams, and recursive folder reads are outside this review.

Run under the requested MemoryMax=3G/MemorySwapMax=0 systemd scope with
PYTHONPATH=. and /home/johnw/projects/Jarvis/.venv/bin/python.
Add --mutant-lower to substitute lower() for casefold() in raw_names_in,
in memory only; a failing suite kills this single mutant.
"""
from __future__ import annotations

import json
import shlex
import sys
import unittest
from unittest.mock import patch

import name_protection_verifier_check as verifier
from jarvis import runtime
from jarvis.tools import dispatch, secrets
from jarvis.v2 import permissions


NAMES = sorted(secrets.PROTECTED_NAMES | secrets.EXACT_NAMES)
FIXED_NAMES = [name for name in NAMES if not name.startswith(".env")]
EQUIVALENTS = (
    ("ſpotify_token.json", "spotify_token.json"),
    ("onshape_Keys.json", "onshape_keys.json"),
    ("jarviſ_puſh_ſubſcriptionſ.json", "jarvis_push_subscriptions.json"),
    ("JARVIS_PUSH_KEY.JSON", "jarvis_push_key.json"),
    (".GIT-CREDENTIALſ", ".git-credentials"),
    ("dIſcord_token.jſon", "discord_token.json"),
    ("onshape_Keyſ.jſon", "onshape_keys.json"),
)
SHIFTS = ("ß", "ﬁ", "İ", "ßﬁİ")


def inline_read(path, shift="ßﬁİ"):
    # Strings contain no spaces: the interpreter-string parser does not
    # mistake these Python file reads for nested shell command lines.
    code = f"label={shift!r};print(open({str(path)!r}).read());label={shift!r}"
    return shlex.join([sys.executable, "-c", code])


class CasefoldReview(unittest.TestCase):
    setUp = verifier.Counterexamples.setUp

    def test_length_changes_on_both_sides_do_not_lose_raw_matches(self):
        for shift in SHIFTS:
            self.assertGreater(len(shift.casefold()), len(shift))
            for spelling, canonical in [(name.upper(), name) for name in NAMES] + list(EQUIVALENTS):
                with self.subTest(shift=shift, spelling=spelling):
                    self.assertTrue(secrets.is_credential_name(spelling))
                    # Delimiters stay adjacent to the protected name even
                    # when text before and after it expands during folding.
                    text = f"label='{shift}';open('/{shift}/{spelling}');label='{shift}'"
                    self.assertEqual(secrets.raw_names_in(text), canonical)
                    self.assertIsNotNone(secrets.protected_in_command("python -c " + shlex.quote(text)))

    def test_folded_letters_do_not_create_matches_inside_longer_names(self):
        for name in FIXED_NAMES + [spelling for spelling, _ in EQUIVALENTS]:
            # ß and ﬁ expand to ASCII name characters at the boundary.
            # İ on the right begins with ASCII i after folding. On the
            # left its combining dot is outside the documented ASCII
            # boundary class, so use an intervening ASCII letter there.
            for ordinary in ("x" + name, name + "x", "ß" + name, name + "ß",
                             "ﬁ" + name, name + "ﬁ", "İx" + name, name + "İ",
                             "notes." + name, name + ".example", "_" + name,
                             name + "-notes"):
                with self.subTest(ordinary=ordinary):
                    self.assertFalse(secrets.is_credential_name(ordinary))
                    command = inline_read(self.project / ordinary)
                    self.assertIsNone(secrets.raw_names_in(command))
                    self.assertIsNone(secrets.protected_in_command(command))

    def test_non_equivalent_dotted_and_dotless_i_stay_ordinary(self):
        for name in ("dİscord_token.json", "dIſcord_token.jİon", ".pypırc",
                     "ſpotİfy_token.json", "onshape_Keyſ.jıon"):
            with self.subTest(name=name):
                self.assertFalse(secrets.is_credential_name(name))
                self.assertIsNone(secrets.raw_names_in(inline_read(self.project / name)))
                self.assertIsNone(secrets.protected_in_command(inline_read(self.project / name)))

    def test_raw_only_inline_reads_cannot_print_fake_credentials(self):
        shifted = self.project / "ßﬁİ"
        shifted.mkdir()
        for spelling, canonical in EQUIVALENTS:
            path = shifted / spelling
            path.write_text(verifier.MARKER + "\n")
            command = inline_read(path)
            with self.subTest(spelling=spelling, layer="raw guard"):
                # Prove this exercises the raw fallback rather than an
                # earlier token or nested-command check hiding a failure.
                self.assertIsNone(secrets._judge_line(command, False, 0))
                self.assertEqual(secrets.raw_names_in(command), canonical)
                self.assertEqual(secrets.protected_in_command(command), canonical)
            with self.subTest(spelling=spelling, layer="approved dispatch"):
                with runtime.tools_bound({"run_command"}):
                    out = dispatch("run_command", json.dumps({
                        "command": command, "reason": "hermetic fake credential fixture",
                    }), approve=lambda *_: True).text
                self.assertNotIn(verifier.MARKER, out, out)
                self.assertIn("protected", out, out)

    def test_parsed_command_paths_keep_casefold_protection_without_raw_fallback(self):
        # There are no direct _RAW_NAMES consumers outside raw_names_in.
        # Earlier protected_in_command paths use the whole-name rule and
        # must keep the same equivalences without relying on the raw pass.
        with patch.object(secrets, "raw_names_in", return_value=None):
            for spelling, _ in EQUIVALENTS:
                path = self.project / "ßﬁİ" / spelling
                for command in ("cat " + shlex.quote(str(path)),
                                "sh -c " + shlex.quote("cat " + shlex.quote(str(path))),
                                "cat<" + shlex.quote(str(path)),
                                "cat " + str(path).replace("token", "to\\\nken")):
                    with self.subTest(command=command):
                        self.assertIsNotNone(secrets.protected_in_command(command))

    def test_v2_grep_glob_and_path_reach_folded_raw_scan(self):
        for tool in ("Grep", "grep_files"):
            for spelling, _ in EQUIVALENTS:
                # A protected component followed by an ordinary leaf forces
                # path's raw check: the final leaf is not protected itself.
                args_by_key = {
                    "path": {"path": str(self.project / "ßﬁİ" / spelling / "plain.txt")},
                    "glob": {"path": str(self.project), "glob": f"ßﬁİ/**/{spelling}"},
                }
                for key, args in args_by_key.items():
                    with self.subTest(tool=tool, key=key, spelling=spelling):
                        with patch.object(secrets, "raw_names_in", wraps=secrets.raw_names_in) as raw:
                            verdict = permissions.denied_read(tool, args, str(self.project))
                        raw.assert_any_call(args[key])
                        self.assertIsNotNone(verdict)

    def test_v2_and_readonly_allow_ordinary_expanding_names(self):
        for name in ("ßſpotify_token.json", "ſpotify_token.jsonﬁ",
                     "ﬁonshape_Keys.json", "onshape_Keys.jsonİ",
                     "dİscord_token.json", ".pypırc", "notes.GIT-CREDENTIALſ.txt"):
            path = self.project / name
            path.write_text(verifier.MARKER + "\n")
            with self.subTest(name=name):
                self.assertFalse(secrets.is_credential_name(name))
                for command in ("cat " + shlex.quote(str(path)),
                                "grep VERIFIER " + shlex.quote(str(path))):
                    self.assertIsNone(secrets.protected_in_command(command))
                    self.assertIn(verifier.MARKER, verifier.call("run_readonly", command=command))
                for args in ({"path": str(path)},
                             {"path": str(self.project), "glob": f"ßﬁİ/**/{name}"}):
                    self.assertIsNone(permissions.denied_read("Grep", args, str(self.project)))

    def test_v2_content_patterns_and_names_only_globs_do_not_gain_raw_refusals(self):
        for spelling, _ in EQUIVALENTS:
            for tool, args in (
                ("Grep", {"path": str(self.project), "pattern": "ßﬁİ" + spelling}),
                ("Glob", {"path": str(self.project), "pattern": f"ßﬁİ/**/{spelling}"}),
            ):
                with self.subTest(tool=tool, spelling=spelling):
                    self.assertIsNone(permissions.denied_read(tool, args, str(self.project)))

    def test_ordinary_commands_with_length_changing_unicode_still_run(self):
        (self.project / "readme.txt").write_text("ß ﬁ İ\n")
        for command in ("ls -la", "date -u", "cat readme.txt",
                        "grep 'ß' readme.txt", "grep 'ﬁ' readme.txt", "grep 'İ' readme.txt"):
            with self.subTest(command=command):
                self.assertIsNone(secrets.protected_in_command(command))
                self.assertIn("[exit 0]", verifier.call("run_readonly", command=command))


def lower_mutant(text):
    """One mutation: raw matching uses lower(), losing true long-s folds."""
    folded = text.lower()
    for name, pattern in secrets._RAW_NAMES:
        if pattern.search(folded):
            return name
    return secrets.env_credential_in_text(text)


if __name__ == "__main__":
    if "--mutant-lower" in sys.argv:
        sys.argv.remove("--mutant-lower")
        with patch.object(secrets, "raw_names_in", lower_mutant):
            unittest.main(verbosity=2)
    else:
        unittest.main(verbosity=2)

"""`terminal_read`'s pure half (WP-F; decisions W-2): the renderer
(`jarvis/v2/terminal_text.py`), the credential formats
(`jarvis/credential_patterns.py`) and the guard (`jarvis/v2/terminal_guard.py`).

Free, no terminal, no daemon, no network. **Every placeholder credential is
built by concatenation at run time** (`P(...)`), so no contiguous token shaped
like a real one sits in this file: they are shapes, never values.

Written to fail against `main` before WP-F: none of the three modules existed.
"""
from __future__ import annotations

import base64
import random
import sys
from pathlib import Path
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import credential_patterns as CP  # noqa: E402
from jarvis.v2 import terminal_guard as G, terminal_text as TT  # noqa: E402
from jarvis.v2.terminals import CommandSpan  # noqa: E402


def P(*parts: str) -> str:
    """A placeholder, assembled here so the source never holds it whole."""
    return "".join(parts)


HEX40 = "0123456789abcdef" * 2 + "01234567"
UUID = "123e4567-e89b-12d3-a456-426614174000"


def texts(data: bytes, **kw) -> list[str]:
    return [line.text for line in TT.render(data, **kw).lines]


def chunked(data: bytes, cuts, **kw) -> list[str]:
    renderer = TT.Renderer(**kw)
    last = 0
    for cut in list(cuts) + [len(data)]:
        renderer.feed(data[last:cut])
        last = cut
    return [line.text for line in renderer.finish().lines]


# -- the renderer -----------------------------------------------------------------------

STREAM = (b"$ ls\r\n"
          b"\x1b[1;31mred\x1b[0m and \x1b[38;2;1;2;3mtrue colour\x1b[m\r\n"   # CSI SGR
          b"\x1b]0;a title\x07after osc bel\r\n"                              # OSC, BEL
          b"\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ text\r\n"                # OSC, ST
          b"\x1bPq#0;2;0;0;0~-\x1b\\after dcs\r\n"                            # DCS (sixel)
          b"\x1b_apc payload\x1b\\\x1b^pm payload\x1b\\\x1bXsos\x1b\\after strings\r\n"
          b"\x1b(B\x1b7\x1b8\x1b=\x1b>single escapes\r\n"
          b"\xc2\x9b31mc1 csi\xc2\x9b0m \xc2\x9d0;t\x07c1 osc\r\n"            # C1 forms
          b"caf\xc3\xa9 \xe2\x82\xac \xf0\x9f\x98\x80 utf-8\r\n"
          b"$ ")
EXPECTED = ["$ ls", "red and true colour", "after osc bel", "link text", "after dcs",
            "after strings", "single escapes", "c1 csi c1 osc", "café € \U0001F600 utf-8", "$"]


class Strip(unittest.TestCase):
    def test_every_kind_of_sequence_is_stripped(self):
        self.assertEqual(texts(STREAM), EXPECTED)

    def test_split_at_every_byte_and_every_two_chunk_cut(self):
        self.assertEqual(chunked(STREAM, range(1, len(STREAM))), EXPECTED)
        for cut in range(1, len(STREAM)):
            with self.subTest(cut=cut):
                self.assertEqual(chunked(STREAM, [cut]), EXPECTED)

    def test_aborted_sequences(self):
        # CAN aborts a CSI; an ESC inside an OSC ends it and starts a new
        # sequence; a C1 control inside an OSC aborts it; ST alone is nothing.
        data = (b"a\x1b[12\x18b\r\n"
                b"\x1b]0;title\x1b[31mc\r\n"
                b"\x1b]0;title\xc2\x9b0md\r\n"
                b"\xc2\x9ce\r\n")
        self.assertEqual(texts(data), ["ab", "c", "d", "e"])

    def test_an_unterminated_string_swallows_only_itself(self):
        self.assertEqual(texts(b"before\r\n\x1b]0;never ends\r\nstill osc"), ["before"])
        # An unfinished CSI longer than any real one is not carried for ever.
        data = b"x\x1b[" + b"1;" * 300 + b"\r\ny"
        self.assertEqual(texts(data)[-1], "y")

    def test_controls_are_not_drawn(self):
        self.assertEqual(texts(b"a\x07b\x00c\x7fd"), ["abcd"])


class AltScreen(unittest.TestCase):
    def test_the_alternate_screen_is_never_read(self):
        for enter, leave in ((b"\x1b[?1049h", b"\x1b[?1049l"), (b"\x1b[?1047h", b"\x1b[?1047l"),
                             (b"\x1b[?47h", b"\x1b[?47l"), (b"\x1b[?25;1049h", b"\x1b[?1049;25l"),
                             (b"\xc2\x9b?1049h", b"\xc2\x9b?1049l")):
            with self.subTest(enter=enter):
                data = b"$ vim f\r\n" + enter + b"ALT-SCREEN-TEXT\r\nmore alt" + leave + b"$ done\r\n"
                self.assertEqual(texts(data), ["$ vim f", "$ done"])

    def test_entered_and_never_left(self):
        data = b"$ top\r\n\x1b[?1049h\x1b[HALT-ONLY\r\n" + b"row\r\n" * 50
        self.assertEqual(texts(data), ["$ top"])

    def test_split_at_every_byte(self):
        data = b"keep\r\n\x1b[?1049hhidden\x1b[?1049lkept\r\n"
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.assertEqual(chunked(data, [cut]), ["keep", "kept"])

    def test_a_ring_that_starts_inside_the_alternate_screen(self):
        self.assertEqual(texts(b"still alt\r\n\x1b[?1049lnormal\r\n", alt=True), ["normal"])
        self.assertEqual(texts(b"all alt\r\nmore\r\n", alt=True), [])

    def test_the_tracker_agrees_with_the_renderer(self):
        """A differential: random streams of alternate-screen switches, look-
        alikes and the sequences that could hide one, fed in random pieces."""
        pieces = [b"text ", b"\r\n", b"\x1b[?1049h", b"\x1b[?1049l", b"\x1b[?47h", b"\x1b[?1047l",
                  b"\xc2\x9b?1049h", b"\xc2\x9b?1049l", b"\x1b]0;\x1b[?1049h", b"\x1b]0;t\x07",
                  b"\x1bP", b"\x1b\\", b"\x18", b"\x1b[?10", b"49h", b"\x1b[?1049$h", b"\x1b[?2004h",
                  b"\x1b[?1049;2004l", b"\x1b", b"[", b"\xc2", b"\x9b", b"?", b"1049", b"h", b"l"]
        rng = random.Random(7)
        for _ in range(2000):
            stream = b"".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
            renderer, tracker = TT.Renderer(), TT.AltTracker()
            i = 0
            while i < len(stream):
                j = i + rng.randint(1, 5)
                renderer.feed(stream[i:j])
                tracker.feed(stream[i:j])
                i = j
            self.assertEqual(tracker.alt, renderer.alt, stream)
            self.assertEqual(TT.AltTracker().at(stream), renderer.alt, stream)


class Overwrites(unittest.TestCase):
    def test_carriage_returns_collapse_a_progress_bar(self):
        data = b"get  10%\rget  50%\rget 100%\r\ndone\r\n"
        self.assertEqual(texts(data), ["get 100%", "done"])

    def test_backspace_overwrites(self):
        self.assertEqual(texts(b"$ printenx\b \bv\r\n"), ["$ printenv"])
        self.assertEqual(texts(b"abc\b\bX\r\n"), ["aXc"])

    def test_tabs_and_erase_in_line(self):
        self.assertEqual(texts(b"a\tb\r\n"), ["a       b"])
        self.assertEqual(texts(b"hello world\r\x1b[K\r\nkeep\x1b[2K\r\nend"), ["", "", "end"])
        self.assertEqual(texts(b"hello world\x1b[6D\x1b[0K\r\n"), ["hello"])

    def test_cursor_up_redraws_lines_above(self):
        # docker-style: two progress rows rewritten in place.
        data = (b"layer a: 10%\r\nlayer b: 10%\r\n"
                b"\x1b[2A\rlayer a: done\x1b[K\r\n"
                b"\x1b[1B\x1b[1A\rlayer b: done\x1b[K\r\n$ ")
        self.assertEqual(texts(data), ["layer a: done", "layer b: done", "$"])

    def test_column_and_character_edits(self):
        self.assertEqual(texts(b"abcdef\x1b[3G\x1b[2P\r\n"), ["abef"])
        self.assertEqual(texts(b"abcdef\x1b[3G\x1b[2@\r\n"), ["ab  cdef"])
        self.assertEqual(texts(b"abcdef\x1b[3G\x1b[2X\r\n"), ["ab  ef"])
        self.assertEqual(texts(b"abc\x1b7XY\x1b8Z\r\n"), ["abcZY"])

    def test_clear_drops_what_was_on_screen_but_ink_remembers(self):
        data = b"$ cat .env\r\nsome output\r\n\x1b[H\x1b[2J\x1b[3J$ ls\r\na b\r\n$ "
        rendered = TT.render(data, rows=24)
        self.assertEqual([l.text for l in rendered.lines], ["$ ls", "a b", "$"])
        self.assertIn("$ cat .env", [l.text for l in rendered.ink])

    def test_utf8_split_inside_a_character(self):
        data = "naïve → 世界\r\n".encode()
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                self.assertEqual(chunked(data, [cut]), ["naïve → 世界"])

    def test_offsets_name_the_bytes_that_drew_each_line(self):
        data = b"one\r\n\x1b[31mtwo\x1b[0m\r\nthree"
        lines = TT.render(data, start=1000).lines
        # A line starts at the newline that made it and ends with its last drawn byte.
        self.assertEqual([(l.first, l.last) for l in lines], [(1000, 1003), (1004, 1013), (1018, 1024)])
        self.assertEqual(data[lines[1].last - 1000 - 3:lines[1].last - 1000], b"two")


# -- credential formats ---------------------------------------------------------------

FORMATS = {
    "openrouter": P("sk-", "or-v1-", "0123456789abcdef" * 4),
    "sk-provider": P("sk-", "proj-", "Abc123" * 8),
    "github": P("gh", "p_", "A1b2C3d4" * 4, "E5f6"),
    "github-pat": P("github", "_pat_", "11ABCDEFG0" * 3),
    "aws-key-id": P("AK", "IA", "ABCDEFGH23456789"),
    "slack": P("xo", "xb-", "1234567890-abcdefghij"),
    "google-api": P("AI", "za", "Sy" + "A1b2C3d4e5" * 3 + "F6g"),
    "stripe": P("sk", "_live_", "a1B2c3D4" * 3),
    "private-key": P("-----BEGIN ", "OPENSSH PRIVATE KEY", "-----"),
    "jwt": P("ey", "JhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiIxMjMifQ", ".", "c2lnbmF0dXJlc2ln"),
    "huggingface": P("hf", "_", "AbCdEfGhIj" * 3 + "1234"),
    "npm": P("npm", "_", "A1b2C3d4E5" * 3 + "F6g7H8"),
    "gitlab": P("glpat", "-", "A1b2C3d4E5f6G7h8I9j0"),
}
NOT_CREDENTIALS = [
    HEX40, UUID, P("sk-", "short"), P("sk-", "learn-something-very-long-with-words"),
    P("AK", "IA", "ABCDEFGH2345678"), P("gh", "p_", "A1b2" * 8), "eyJhbGciOi but not a token",
    "-----BEGIN PUBLIC KEY-----", "-----BEGIN CERTIFICATE-----", "task-1234567890abcdefghij",
]


class Formats(unittest.TestCase):
    def test_each_format_is_found_alone_and_in_a_line(self):
        for name, value in FORMATS.items():
            with self.subTest(name=name):
                self.assertEqual(CP.find(value), name if name != "sk-provider" else "sk-provider")
                self.assertTrue(CP.holds_credential(f"export X={value} # set"))
                self.assertTrue(CP.holds_credential(f'{{"k": "{value}"}}'))

    def test_shapes_that_are_not_credentials(self):
        for text in NOT_CREDENTIALS:
            with self.subTest(text=text):
                self.assertIsNone(CP.find(text))

    def test_the_list_is_one_tuple_of_compiled_patterns(self):
        self.assertTrue(all(hasattr(p, "search") for _, p in CP.PATTERNS))
        named = {name for name, _ in CP.PATTERNS}
        self.assertTrue(set(FORMATS) <= named, set(FORMATS) - named)


# -- rule 1: exact values ---------------------------------------------------------------

class Values(unittest.TestCase):
    VALUE = P("placeholder", "-Value-", "9f8e7d6c5b4a")

    def test_the_value_and_its_encodings(self):
        forms = G.value_forms([self.VALUE, "short", "", None])
        self.assertTrue(G.holds_value(f"KEY={self.VALUE}", forms))
        self.assertTrue(G.holds_value(quote(self.VALUE, safe=""), forms) or quote(self.VALUE) == self.VALUE)
        self.assertFalse(G.holds_value("short and nothing else", forms))
        for prefix in (b"", b"K", b"KE", b"KEY", b"KEY=", b"OPENROUTER_API_KEY="):
            for encode in (base64.b64encode, base64.urlsafe_b64encode):
                with self.subTest(prefix=prefix, encode=encode.__name__):
                    blob = encode(prefix + self.VALUE.encode() + b"\n").decode()
                    self.assertTrue(G.holds_value(blob, forms), blob)

    def test_url_encoding(self):
        value = P("p@ss/", "word+", "with space")
        forms = G.value_forms([value])
        self.assertTrue(G.holds_value("?q=" + quote(value, safe=""), forms))


# -- rule 3: secret-printing commands -------------------------------------------------

PRINTERS = [
    "env", "printenv", "printenv HOME", "set", "export", "export -p", "declare -p", "typeset",
    "env | grep KEY", "  env", "\tprintenv", "sudo -k env", "sudo -k printenv | sort",
    "sudo -u root printenv", "FOO=1 BAR=2 printenv", "/usr/bin/env", "/usr/bin/printenv PATH",
    "env -i FOO=1", "env -u X", "nohup env", "timeout 5 cat .env", "ls | env", "true && env",
    "(env)", "{ env; }", "cat .env", "less .env.local", "head -5 .env", "tail -f google_token.json",
    "bat .env", "cat ./config/.env", "cat ~/.ssh/id_ed25519", "cat ~/.aws/credentials",
    "cat /proc/self/environ", "strings /proc/1/environ", "grep KEY .env", "cat .env*",
    "gh auth token", "gh auth status -t", "gh auth status --show-token", "anything --show-token",
    "vercel env pull", "vercel env pull .env.local", "aws configure get aws_secret_access_key",
    "aws configure export-credentials", "git credential fill", "git -C repo credential fill",
    "echo $GITHUB_TOKEN", 'printf "%s" "${OPENAI_API_KEY}"', "bash -c 'cat .env'",
    'sh -lc "printenv"', "ssh host cat .env", "ssh -p 22 host printenv", "docker exec c printenv",
    "env -S 'cat .env'", "eval printenv", "kubectl config view --raw", "gcloud auth print-access-token",
    "(sleep 5; cat .env) &", "sudo env", "doas -u root env",
]
NOT_PRINTERS = [
    "env FOO=1 ls", "env -i bash", "ls -la", "cat README.md", "cat .env.example", "export FOO=bar",
    "set -e", "echo hello", "grep -r token src", "git status", "gh pr list", "aws s3 ls",
    "ENV FOO=bar", "echo $HOME", "sudo -k", "sudo apt update", "vim notes.txt", "",
    "declare -x FOO=bar", "git log --oneline", "make test 2>&1", "find . -name env",
    "ls | xargs grep env", "docker build -t env .",
]


class Printers(unittest.TestCase):
    def test_each_shape_prints(self):
        for line in PRINTERS:
            with self.subTest(line=line):
                self.assertTrue(G.prints_secrets(line), line)

    def test_ordinary_lines_do_not(self):
        for line in NOT_PRINTERS:
            with self.subTest(line=line):
                self.assertFalse(G.prints_secrets(line), line)

    def test_the_list_lives_in_one_place(self):
        self.assertEqual(set(G.SECRET_PRINTERS), {"environment", "credential files", "tokens", "variables"})
        self.assertIn("cat", G.READERS)
        self.assertTrue(any(words == ("auth", "token") for _, words in G.SUBCOMMANDS))

    def test_backgrounds(self):
        for line, expected in (("env &", True), ("(sleep 5; env) &", True), ("nohup env", True),
                               ("make 2>&1", False), ("a && b", False), ("cat x &> y", False),
                               ("echo 'a & b'", False)):
            with self.subTest(line=line):
                self.assertEqual(G.backgrounds(line), expected)

    def test_shown_on_a_line_of_terminal_text(self):
        for line in ("john@host:~/p$ cat .env", "❯ printenv", "➜  proj git:(main) ✗ export -p",
                     "(venv) $ env | grep KEY", "root@box:/# set", "fake$ env", "remote$ printenv",
                     "> cat ~/.aws/credentials", "Run printenv to see them", "PS1$ gh auth token"):
            with self.subTest(line=line):
                self.assertTrue(G.shows_printer(line), line)
        for line in ("env", "set", "ENV FOO=bar", "the environment is set", "export default App;",
                     "venv", "a > b", "$ ls -la", "drwxr-xr-x  env", "commit " + HEX40):
            with self.subTest(line=line):
                self.assertFalse(G.shows_printer(line), line)


# -- the heuristic ---------------------------------------------------------------------

TOKEN = "a1B2c3D4e5F6g7H8i9J0k1L2"


class Heuristic(unittest.TestCase):
    def test_a_keyword_with_a_token_after_it(self):
        for line in (f"OPENROUTER_API_KEY={TOKEN}", f"token: {TOKEN}", f'"api_key": "{TOKEN}"',
                     f"password {TOKEN}", f"DB_PASSWD='{TOKEN}'", f"Authorization: Bearer {TOKEN}",
                     f"Authorization: Basic {TOKEN}==", f"private_key_id = {TOKEN}",
                     f"client secret: `{TOKEN}`", f"--auth={TOKEN}", f"credential  =>  {TOKEN}",
                     f"pwd {TOKEN}", f"token: {HEX40}", f"api_key={UUID}", "password=CorrectHorseBatteryStaple"):
            with self.subTest(line=line):
                self.assertTrue(G.withhold(line), line)

    def test_hashes_uuids_and_lookalikes_alone_stay(self):
        for line in (f"commit {HEX40}", f"id {UUID}", f"{HEX40}  refs/heads/main", "monkeypatch_something_long_name",
                     "token: aaaaaaaaaaaaaaaaaaaaaaaa", "key file: /home/john/.ssh/known_hosts_backup",
                     "at com.example.auth.AuthenticationFilter.doFilterInternal(Auth.java:42)",
                     "secret_name: this_is_a_long_identifier", "PWD=/home/john/projects/jarvis-thing",
                     f"the key is far away from {'x' * 45} {TOKEN}", "ordinary text, nothing special: " + TOKEN):
            with self.subTest(line=line):
                self.assertFalse(G.withhold(line), line)

    def test_entropy(self):
        self.assertEqual(G.entropy("aaaa"), 0.0)
        self.assertGreater(G.entropy(HEX40), G.ENTROPY_FLOOR)


# -- a whole read ---------------------------------------------------------------------

def span(data: bytes, command: str, echo: bytes, output: bytes, **kw) -> CommandSpan:
    """The span for `command` whose echo line and output sit in `data`."""
    prompt = data.index(echo)
    start = prompt + len(echo)
    end = data.index(output, start) + len(output) if output else start
    return CommandSpan(command, start=start, end=kw.get("end", end), exit_code=0, prompt=prompt)


class Judge(unittest.TestCase):
    def read(self, data, **kw):
        return G.judge(data, 0, **kw)

    def test_a_plain_read(self):
        verdict = self.read(b"$ make\r\nbuilt 3 targets\r\n$ ")
        self.assertFalse(verdict.refused)
        self.assertEqual(verdict.text, "$ make\nbuilt 3 targets\n$")
        self.assertEqual(verdict.covered, 3)

    def test_each_refusal_family_refuses_the_whole_read_and_says_nothing_else(self):
        value = P("placeholder", "-Value-", "9f8e7d6c5b4a")
        cases = {
            "value": (f"$ run\r\nconnected with {value}\r\n".encode(), {"values": [value]}),
            "base64": (b"$ run\r\n" + base64.b64encode(b"X=" + value.encode()) + b"\r\n", {"values": [value]}),
            "format": (f"$ run\r\nusing {FORMATS['github']}\r\n".encode(), {}),
            "pem": (f"$ run\r\n{FORMATS['private-key']}\r\nMIIE\r\n".encode(), {}),
            "command": (b"$ printenv\r\nHOME=/home/x\r\n$ ", {}),
        }
        for name, (data, kw) in cases.items():
            with self.subTest(name=name):
                verdict = self.read(data, **kw)
                self.assertTrue(verdict.refused)
                self.assertEqual(verdict.text, "")
        self.assertEqual(G.REFUSAL, "possible credential in this output")

    def test_heuristic_lines_are_withheld_and_counted(self):
        data = f"$ run\r\ntoken: {TOKEN}\r\nok\r\nsecret = {HEX40}\r\ncommit {HEX40}\r\n".encode()
        verdict = self.read(data)
        self.assertFalse(verdict.refused)
        self.assertEqual(verdict.withheld, 2)
        self.assertEqual(verdict.text, f"$ run\nok\ncommit {HEX40}")

    def test_lines_size_and_caps(self):
        data = b"".join(b"line %d\r\n" % i for i in range(3000))
        self.assertEqual(self.read(data).covered, 200)
        self.assertEqual(self.read(data, lines=5).text.split("\n")[0], "line 2995")
        self.assertEqual(self.read(data, lines=99999).covered, G.MAX_LINES)
        long = self.read(b"x" * 10_000 + b"\r\n")
        self.assertEqual((len(long.text), long.shortened), (G.LINE_CAP, 1))
        wide = self.read(b"".join(b"%04d" % i + b"y" * 1990 + b"\r\n" for i in range(100)), lines=100)
        self.assertLessEqual(len(wide.text), G.MAX_CHARS)
        self.assertGreater(wide.cut, 0)
        self.assertTrue(wide.text.endswith("y"))                      # the newest lines are kept

    def test_the_alternate_screen_is_never_read_or_judged(self):
        data = b"$ less f\r\n\x1b[?1049hOPENROUTER_API_KEY=" + TOKEN.encode() + b"\x1b[?1049l$ "
        self.assertEqual(self.read(data).text, "$ less f\n$")
        self.assertEqual(self.read(b"inside\r\n\x1b[?1049lout\r\n", alt=True).text, "out")

    def test_invisible_characters_cannot_split_a_credential_past_the_rules(self):
        key = FORMATS["github"]
        hidden = key[:6] + "​" + key[6:]
        self.assertTrue(self.read(f"$ x\r\n{hidden}\r\n".encode()).refused)

    # -- rule 3 in detail ---------------------------------------------------------------

    def test_a_span_refuses_even_when_its_command_line_is_not_on_screen(self):
        data = b"HOME=/home/x\r\nSHELL=/bin/bash\r\n$ "
        printer = CommandSpan(" printenv", start=0, end=data.index(b"$ "), exit_code=0, prompt=None)
        self.assertTrue(self.read(data, spans=(printer,), integrated=True).refused)
        self.assertFalse(self.read(data).refused)

    def test_a_backgrounded_printer_taints_what_follows(self):
        data = b"$ (sleep 1; env) &\r\n[1] 42\r\n$ echo hi\r\nhi\r\nHOME=/x\r\n$ "
        bg = span(data, "(sleep 1; env) &", b"$ (sleep 1; env) &\r\n", b"[1] 42\r\n")
        echo = span(data, "echo hi", b"$ echo hi\r\n", b"hi\r\nHOME=/x\r\n")
        self.assertTrue(self.read(data, lines=2, spans=(bg, echo), integrated=True).refused)

    def test_forged_text_is_refused_whatever_the_spans_say(self):
        # The real span is an innocent command; what it printed shows a
        # secret printer's prompt line (a nested shell, or a forgery).
        data = b"$ ./tool\r\nremote$ printenv\r\nSOME_VAR=x\r\nremote$ cat .env\r\nA=b\r\n$ "
        tool = span(data, "./tool", b"$ ./tool\r\n", b"A=b\r\n")
        for integrated in (False, True):
            with self.subTest(integrated=integrated):
                self.assertTrue(self.read(data, spans=(tool,), integrated=integrated).refused)

    def test_the_text_fallback_reaches_above_the_read(self):
        # The window holds only the tail of env's output; its command line is
        # just above it. No spans (a shell without the startup file).
        data = b"$ env\r\n" + b"".join(b"VAR%d=v\r\n" % i for i in range(300))
        self.assertTrue(self.read(data, lines=50).refused)
        # LOOKBACK bytes of output later, it is out of reach again.
        later = data + b"".join(b"build line %06d\r\n" % i for i in range(G.LOOKBACK // 18 + 10))
        self.assertFalse(self.read(later, lines=50).refused)

    def test_signed_marks_narrow_the_lookback(self):
        data = b"$ env\r\nX=1\r\n$ echo hi\r\nhi\r\n$ "
        env = span(data, "env", b"$ env\r\n", b"X=1\r\n")
        echo = span(data, "echo hi", b"$ echo hi\r\n", b"hi\r\n")
        # A read of the last two lines starts after env's span ended: proven clean.
        self.assertFalse(self.read(data, lines=2, spans=(env, echo), integrated=True).refused)
        # Without the marks, env's line is in the lookback: refused.
        self.assertTrue(self.read(data, lines=2).refused)
        self.assertTrue(self.read(data, lines=2, spans=(env, echo), integrated=False).refused)
        # A read that covers env's output is refused by its span.
        self.assertTrue(self.read(data, lines=4, spans=(env, echo), integrated=True).refused)

    def test_a_span_record_below_spans_from_never_narrows(self):
        """Bytes before `spans_from` lost their spans to the cap: no record
        there may vouch for anything, so the full lookback applies."""
        data = b"$ env\r\nX=1\r\n$ echo hi\r\nhi\r\n$ "
        env = span(data, "env", b"$ env\r\n", b"X=1\r\n")
        echo = span(data, "echo hi", b"$ echo hi\r\n", b"hi\r\n")
        self.assertTrue(self.read(data, lines=2, spans=(env, echo), integrated=True,
                                  spans_from=len(data) - 2).refused)
        self.assertEqual(G.scan_start(100_000, (echo,), spans_from=200_000, integrated=True),
                         100_000 - G.LOOKBACK)

    def test_a_cleared_command_line_still_counts(self):
        data = b"$ cat .env\r\nA=b\r\n\x1b[H\x1b[2J\x1b[3J$ "
        self.assertTrue(self.read(data, lines=5).refused)


if __name__ == "__main__":
    unittest.main(verbosity=1)

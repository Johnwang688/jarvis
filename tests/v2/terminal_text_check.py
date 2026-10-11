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
import time
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
        renderer = TT.Renderer()
        renderer.feed(b"x\x1b[")
        for _ in range(100):
            renderer.feed(b"1;" * 50)
        self.assertLessEqual(len(renderer._carry), TT.CARRY_CAP)
        renderer.feed(b"\x1b[0m\r\ny")
        self.assertEqual([l.text for l in renderer.finish().lines][-1], "y")

    def test_c0_inside_a_sequence_is_executed_and_the_sequence_goes_on(self):
        """As xterm does (2026-10-10 review): CR/LF inside a CSI move the
        cursor and the CSI still completes; DEL is ignored; CAN aborts."""
        self.assertEqual(texts(b"one\x1b[\r\n2Ktwo"), ["one", "two"])
        self.assertEqual(texts(b"abc\x1b[1\x7fDX\r\n"), ["abX"])
        self.assertEqual(texts(b"a\x1b(\r\nBb"), ["a", "b"])
        self.assertEqual(texts(b"a\x1b\r\n7b"), ["a", "b"])
        self.assertEqual(texts(b"a\x1b[12\x18b"), ["ab"])
        # The alternate screen with controls inside its switch: still never read.
        for switch in (b"\x1b[?10\r49h", b"\x1b\r[?1049h", b"\x1b[\x00?1049\x7fh", b"\xc2\x9b?1\n049h"):
            with self.subTest(switch=switch):
                seen = texts(b"keep\r\n" + switch + b"HIDDEN-TEXT\x1b[?1049l\r\nback")
                self.assertEqual((seen[0], seen[-1]), ("keep", "back"))
                self.assertNotIn("HIDDEN", "".join(seen))

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
                  b"\x1b[?1049;2004l", b"\x1b", b"[", b"\xc2", b"\x9b", b"?", b"1049", b"h", b"l",
                  b"\r", b"\n", b"\x00", b"\x7f", b"\x1a", b"\x1b[?10\r49h", b"\x1b\n[?1049h",
                  b"\x1b[\x00?47\x7fl"]
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
    "private-key-end": P("-----END ", "RSA PRIVATE KEY", "-----"),
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
    # The compound shapes the 2026-10-10 review found (each False before).
    "if [ -f .env ]; then cat .env; fi", 'for f in .env; do cat "$f"; done',
    'while read l; do echo "$l"; done < .env', "< .env cat", "<.env cat", "true; then env; fi",
    "do env; done", "elif env", "!env", "! env", "coproc env", "case x in x) env;; esac",
    "x=env; $x", "$(echo env)", '"$(which env)"', "echo $(cat .env)", "echo `cat .env`",
    "diff <(cat .env) x", "e''nv", "find . -name .env -exec cat {} \\;",
    "find . -name '.env*' -exec grep KEY {} +", "ls .env* | xargs cat", "xargs -a .env echo",
    "git show HEAD:.env", "git diff .env.production", "python3 -c 'print(open(\".env\").read())'",
    "cat .env.production", "less ~/app/.env.staging.local", "ps eww", "ps axe",
    "terraform output -raw private_key", "systemctl show-environment", "docker inspect c",
    "aws secretsmanager get-secret-value --secret-id x", "time env", "{ cat .env; }",
    "f() { cat .env; }; f",
    # 2026-10-11 review (finding 4): copies, archives and decompressors onto
    # the terminal, and whole-environment dumps (each False on 3393daf).
    "cp .env /dev/stdout", "install -m 600 .env.production /dev/stderr", "curl -s file://$PWD/.env",
    "curl file:///home/u/.aws/credentials", "tar -xOf b.tar .env", "tar xOf b.tar .env",
    "tar --extract --to-stdout -f b.tar .env.local", "unzip -p a.zip .env", "7z e -so a.7z .env.production",
    "zcat .env.gz", "gzip -dc .env.gz", "zcat ~/.ssh/id_rsa.gz", "xzcat .env.production.xz",
    "node -p process.env", "node -e 'console.log(JSON.stringify(process.env))'",
    "python3 -c 'import os; print(os.environ)'", "python3 -Ic 'import os; print(dict(os.environ))'",
    "python3 -c 'import os; print(os.environ[\"API_KEY\"])'", "python3 -c 'import os; print(os.environ.copy())'",
    "ruby -e 'p ENV'", "perl -e 'print %ENV'", "perl -le 'print $ENV{GITHUB_TOKEN}'",
    "php -r 'print_r(getenv());'", "php -i", "deno eval 'console.log(Deno.env.toObject())'", "bun -p process.env",
    "pwsh -c 'Get-ChildItem env:'", "pwsh -EncodedCommand ZQBuAHYA", "tmux show-environment", "tmux showenv -g",
    "node --env-file=.env -p process.env.DATABASE_URL", "node --env-file=.env -e 'console.log(process.env.PORT)'",
    "python3 .env", "node .env.production", "python -m dotenv list", "python -m base64 .env",
    "perl -pe 1 .env", "node -e \"require('fs').readFileSync('.env')\"",
    "docker inspect c --format '{{.Config.Env}}'", "docker inspect c --format '{{json .}}'",
    "docker inspect -f '{{.}}' c", "docker container inspect c", "podman inspect c --format json",
    "docker compose config", "docker-compose config", "dotenv list", "sort < .env",
    "nohup " * 9 + "env", "sudo " * 9 + "cat .env",
]
NOT_PRINTERS = [
    "env FOO=1 ls", "env -i bash", "ls -la", "cat README.md", "cat .env.example", "export FOO=bar",
    "set -e", "echo hello", "grep -r token src", "git status", "gh pr list", "aws s3 ls",
    "ENV FOO=bar", "echo $HOME", "sudo -k", "sudo apt update", "vim notes.txt", "",
    "declare -x FOO=bar", "git log --oneline", "make test 2>&1", "find . -name env",
    "ls | xargs grep env", "docker build -t env .", "cat .env.example", "cat .env.sample",
    "cat .env.template", "ls -la", "ps aux", "ps -o pid,user", "for f in *.py; do cat \"$f\"; done",
    "if [ -f x ]; then make; fi", "x=ls; $x", "$(npm bin)/eslint .", "echo $(date)",
    # 2026-10-11 review (finding 1): ordinary commands that were refused —
    # an --env-file loads the file, a counter does not print it, and a format
    # naming a safe field prints no environment (each True on 3393daf but
    # the last four).
    "node --env-file=.env server.js", "node --env-file .env server.js", "deno run --env-file=.env main.ts",
    "bun --env-file=.env server.ts", "bun run --env-file=.env.production dev",
    "node --env-file=.env server.js | jq", "node --env-file=.env --watch server.js 2>&1 | tee log",
    "python -m uvicorn app:app --env-file .env", "wc -l < .env", "sha256sum < .env.production",
    "docker inspect c --format '{{.State.Status}}'", "docker inspect -f '{{.NetworkSettings.IPAddress}}' c",
    "ls -la .env*", "git log -p -- .env.example", "docker compose --env-file .env.production up -d",
    "node -e 'console.log(process.env.PORT)'", "python3 manage.py runserver", "tar -tvf b.tar .env",
    "cp .env .env.bak", "set -a; source .env; set +a", "npx dotenv -e .env -- next dev",
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

    # 2026-10-11 review (finding 1): lines a dev server, a log or a commit
    # message prints, refused on 3393daf — a reader's or an interpreter's
    # name in prose is not a command, and an --env-file loads the file.
    ORDINARY_LINES = (
        "Add tail call before loading .env.production",
        "Using python 3.12 with settings from .env",
        "[nodemon] starting `node --env-file=.env server.js`",
        "$ node --env-file=.env server.js",
        "john@host:~/app$ node --env-file .env server.js",
        "$ wc -l < .env",
        "$ docker inspect web --format '{{.State.Status}}'",
        # Must keep passing (they did on 3393daf too).
        "[dotenv@17.2.0] injecting env (3) from .env -- tip: encrypt with dotenvx",
        "  - Environments: .env.local, .env",
        "◇ injected env (2) from .env",
        "$ cat .env.example", "$ ls -la .env*", "$ git log -p -- .env.example",
        "$ docker compose --env-file .env.production up -d",
    )

    def test_ordinary_lines_naming_a_credential_file(self):
        for line in self.ORDINARY_LINES:
            with self.subTest(line=line):
                self.assertFalse(G.shows_printer(line), line)

    def test_a_prompt_made_of_words_still_counts(self):
        for line in ("john@host ~/p cat .env", "[proj] cat .env", "(venv) ~/app tail -n 5 .env.production",
                     "(venv) python3 -c 'import os; print(os.environ)'", "$ cp .env /dev/stdout",
                     "$ node -p process.env", "❯ tmux show-environment"):
            with self.subTest(line=line):
                self.assertTrue(G.shows_printer(line), line)

    def test_a_glyph_alone_is_not_a_command_word(self):
        # `_worth_parsing` is the cheap look before a parse: a prompt's glyph
        # is never a command (its glyph candidate reads what follows).
        for candidate in ("$ wc -l < .env", "# cat x", "1 john john 4096 Oct 11 notes.txt"):
            with self.subTest(candidate=candidate):
                self.assertFalse(G._worth_parsing(candidate))
        self.assertTrue(G._worth_parsing("cat .env"))


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


# -- the 2026-10-10 review: width, private keys, the split switch, speed -----------------

class Width(unittest.TestCase):
    """The screen has the terminal's width: a staircase, a wild column and a
    long line are bounded, and wrap the way xterm does."""

    def test_bare_line_feeds_draw_a_staircase_clamped_at_the_margin(self):
        self.assertEqual(texts(b"ab\ncd\nef", cols=80), ["ab", "  cd", "    ef"])
        self.assertEqual(texts(b"\x1b[78Gab\ncd", cols=80), ["                                     "
                                                             "                                        ab",
                                                             " " * 79 + "c" + "d"])

    def test_text_wraps_at_the_margin_into_one_logical_line(self):
        self.assertEqual(texts(b"x" * 100 + b"\r\nnext", cols=40), ["x" * 100, "next"])
        key = FORMATS["github"]
        self.assertTrue(G.judge(f"$ x\r\n{key}\r\n".encode(), 0, cols=10).refused)

    def test_wild_cursor_numbers_are_clamped(self):
        for data in (b"\x1b[999999999Gx", b"\x1b[1;999999999Hx", b"ab\x1b[1D\x1b[999999999@",
                     b"ab\x1b[999999999X", b"\x1b[999999999C\x1b[999999999Dx", b"\x1b[999999999S" * 1000,
                     b"\x1b[999999999L" * 1000):
            with self.subTest(data=data[:24]):
                start = time.perf_counter()
                rendered = TT.render(data, cols=80, rows=24)
                self.assertLess(time.perf_counter() - start, 0.5)
                self.assertTrue(all(len(line.text) <= 80 for line in rendered.lines))
                self.assertLessEqual(len(rendered.lines), TT.MAX_ROWS + 1000)

    def test_the_rows_kept_are_capped_like_scrollback(self):
        rendered = TT.render(b"x\r\n" * 60_000 + b"last", cols=80)
        self.assertLessEqual(len(rendered.lines), TT.MAX_ROWS + 1000)
        self.assertEqual(rendered.lines[-1].text, "last")


PEM_FORMS = ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY", "OPENSSH PRIVATE KEY",
             "ENCRYPTED PRIVATE KEY", "PGP PRIVATE KEY BLOCK"]
BODY = "TUlJRXZRSUJBREFOQmdrcWhraUc5dzBCQVFFRkFBU0NCS2N3Z2dTakFnRUFBb0lCQVFD"[:64]   # filler, not a key


class PrivateKeys(unittest.TestCase):
    """HIGH (2026-10-10 review): the body of a key came back when the read
    started below its BEGIN line."""

    def block(self, form, end=True):
        lines = [P("-----BEGIN ", form, "-----")] + [BODY] * 8
        if end:
            lines.append(P("-----END ", form, "-----"))
        return lines

    def test_a_window_starting_at_any_line_of_the_block_is_refused(self):
        for form in PEM_FORMS:
            block = self.block(form)
            data = ("$ openssl genpkey\r\n" + "\r\n".join(block) + "\r\n$ ").encode()
            for top in range(len(block)):
                with self.subTest(form=form, top=top):
                    verdict = G.judge(data, 0, lines=len(block) - top + 1)
                    self.assertTrue(verdict.refused)
                    self.assertNotIn(BODY, verdict.text)

    def test_a_block_with_no_end_is_refused_from_any_line(self):
        block = self.block("RSA PRIVATE KEY", end=False)
        data = ("$ head -5 key\r\n" + "\r\n".join(block) + "\r\n").encode()
        for top in range(1, len(block)):
            with self.subTest(top=top):
                self.assertTrue(G.judge(data, 0, lines=len(block) - top).refused)

    def test_a_closed_block_above_does_not_refuse_what_follows(self):
        data = ("\r\n".join(self.block("EC PRIVATE KEY")) + "\r\n$ ls\r\na b c\r\n$ ").encode()
        self.assertFalse(G.judge(data, 0, lines=3).refused)

    def test_the_footer_alone_is_a_format(self):
        for form in PEM_FORMS:
            self.assertTrue(CP.holds_credential(P("-----END ", form, "-----")), form)


class AltSplit(unittest.TestCase):
    """MEDIUM (2026-10-10 review): a ring whose cut split the switch into the
    alternate screen read that screen. Through the real `Terminal.history()`,
    at every byte of each switch form."""

    def test_terminals_read_hands_the_lead_over(self):
        """LOW (2026-10-11 review, finding 3): the same split, through
        `Terminals.read` — handing `history.data` from `history.start` (no
        `lead`) read the alternate screen."""
        from jarvis.v2 import terminals as T
        body = b"\x1b[H" + b"ALT-SCREEN-ONLY-TEXT " * 40
        switch = b"\x1b[?1049h"
        for split in range(1, len(switch)):
            with self.subTest(split=split):
                head = b"normal line\r\n" + b"x" * 100
                chunks = [head + switch[:split], switch[split:] + body]
                term = T.Terminal("0000000f", shell="/bin/sh", folder="/", project_id=None,
                                  label="~", cols=80, rows=24, nonce="0" * 32)
                terminals = T.Terminals()
                try:
                    term._ring = T.Ring(cap=sum(map(len, chunks)) - len(head) - split, on_drop=term._alt.feed)
                    for chunk in chunks:
                        term._output(chunk)
                    terminals._terminals[term.id] = term
                    read = terminals.read(term.id, lines=50)
                    self.assertIsNone(read.refused)
                    self.assertNotIn("ALT-SCREEN", read.text)
                finally:
                    term.finish()

    def test_a_split_switch_is_parsed_whole(self):
        from jarvis.v2 import terminals as T
        body = b"\x1b[H" + b"ALT-SCREEN-ONLY-TEXT " * 40
        for switch in (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h", b"\xc2\x9b?1049h",
                       b"\x1b[?10\r49h", b"\x1b\n[?1049h", b"\x1b[\x00?1049\x7fh"):
            for split in range(len(switch) + 1):
                with self.subTest(switch=switch, split=split):
                    head = b"normal line\r\n" + b"x" * 100
                    chunks = [head + switch[:split], switch[split:] + body]
                    term = T.Terminal("0000000e", shell="/bin/sh", folder="/", project_id=None,
                                      label="~", cols=80, rows=24, nonce="0" * 32)
                    try:
                        term._ring = T.Ring(cap=sum(map(len, chunks)) - len(head) - split,
                                            on_drop=term._alt.feed)
                        for chunk in chunks:
                            term._output(chunk)
                        h = term.history()
                        verdict = G.judge(h.lead + h.data, h.start - len(h.lead), alt=h.alt)
                        self.assertNotIn("ALT-SCREEN", verdict.text)
                    finally:
                        term.finish()


MiB = 1 << 20


def _fill(unit: bytes) -> bytes:
    return (unit * (MiB // len(unit) + 1))[:MiB]


def _dense(lines: int, per_line: int) -> bytes:
    """Lines of distinct `$ echo $vN | sort ;` candidates: every one worth parsing."""
    n = iter(range(10 ** 9))
    return b"".join(b"".join(b"$ echo $v%d | sort ; " % next(n) for _ in range(per_line)) + b"\r\n"
                    for _ in range(lines))


def _redrawn(times: int) -> bytes:
    """A long wrapped line of candidates, one of its rows revisited and
    changed `times` times: an ink entry per visit, each a little different."""
    parts = [_dense(1, 60)[:1000] + b"\r\n"]
    for i in range(times):
        parts.append(b"\x1b[A\x1b[5G%d\x1b[%dG%c\x1b[B\r" % (i % 10, 10 + i % 900, 65 + i % 26))
    return b"".join(parts)


class Speed(unittest.TestCase):
    """MEDIUM (2026-10-10 review): a ring of adversarial shapes must render
    and be judged in bounded time — the old renderer had no width and two
    guard scans were quadratic (minutes, gigabytes). Each case is a full
    1 MiB ring read at the maximum window."""

    BUDGET_S = 2.0

    def cases(self):
        b64 = (BODY * 6)[:300].encode()
        return {
            "minified lines: prompt glyphs, keywords, trigger words":
                _fill(b'if(a> b){export const time=c.env;let set=new Set();cat(x)}else{d=e> f} $ cat x # key: '
                      + b"Zx9Qp2Lm8Rt4Vb6Nc1Hs7Kd3 " * 4 + b"\r\n"),
            "one minified line, no newline":
                _fill(b'if(a> b){export const key=c.env;let set=new Set();cat .env.x}else{d=e> f}'),
            "staircase (raw-mode line feeds)": _fill(b"\x1b[70Gx\n"),
            "staircase of ordinary lines": _fill(b"line 000001 some ordinary output here\n"),
            "long base64-ish runs beside keywords": _fill(b"token=" + b64 + b" key: " + b64 + b"\r\n"),
            "many = and :": _fill(b"a=b:c=d:key=e:token=f:pwd=g:" * 30 + b"\r\n"),
            "keyword then spaces": _fill(b"key" + b" " * 61),
            "prompt lines with commands": _fill(b"user@host:~/p$ cat file.txt | grep -v env > out\r\nline\r\n"),
            "cursor-up redraws": _fill(b"\x1b[3A\r\x1b[Klayer a: 10% token=abc\r\n\x1b[Klayer b\r\n\x1b[Kc\r\n"),
            "substitutions everywhere": _fill(b"$ $(echo $x) `y` <(z) $HOME ${A} " * 10 + b"\r\n"),
            "glyph soup": _fill("> set > env > cat > $ # % ❯ \r\n".encode()),
            "a runner with thousands of words": _fill(b"$ watch " + b"a " * 4000 + b"\r\n"),
            "backspace rewrites": _fill(b"A" * 2000 + b"\x08x" * 2000),
            # 2026-10-11 review (finding 2): 18.6 s and 85.7 s on 3393daf.
            "a window dense with distinct candidates": _dense(1000, 30),
            "one row redrawn and erased": _redrawn(7600),
            "one line of globs past the scan": _fill(b"ls src/*.py .env.example "),
        }

    def test_every_adversarial_ring_is_judged_in_bounded_time(self):
        timings = {}
        for name, data in self.cases().items():
            with self.subTest(case=name):
                G.prints_secrets.cache_clear()
                start = time.perf_counter()
                G.judge(data, 0, lines=G.MAX_LINES)
                timings[name] = time.perf_counter() - start
                self.assertLess(timings[name], self.BUDGET_S, name)
        print("\n  speed: " + ", ".join(f"{k.split(':')[0]} {v:.2f}s" for k, v in timings.items()))

    def test_the_command_cache_is_bounded_by_size(self):
        G.prints_secrets.cache_clear()
        for i in range(4000):
            G.prints_secrets(f"cat file{i}.txt " + "x" * 2000)
        self.assertLessEqual(G._cache_size, G.CACHE_BYTES)

    def test_the_command_cache_is_bounded_by_count(self):
        # LOW (2026-10-11 review, finding 5): 8-character keys under a 4 MiB
        # bound were ~524k entries (~+47 MiB).
        G.prints_secrets.cache_clear()
        for i in range(G.CACHE_ENTRIES + 5000):
            G.prints_secrets(f"ls f{i}")
        self.assertLessEqual(len(G._cache), G.CACHE_ENTRIES)

    def test_long_lines_of_candidates_are_judged_in_time_and_not_refused(self):
        """LOW (2026-10-11 review, finding 3): what makes these lines cheap is
        CANDIDATE_CAP, CANDIDATES_PER_LINE and SCAN_LINE_CAP; without any of
        them the same lines spend the parse budget (and are refused) or run
        for seconds. Eight 32 KiB lines of distinct candidates, none a
        printer."""
        data = b"".join(b"".join(b"$ echo $x%d_%d | sort ; " % (line, k) for k in range(1400)) + b"\r\n"
                        for line in range(8))
        G.prints_secrets.cache_clear()
        start = time.perf_counter()
        verdict = G.judge(data, 0, lines=G.MAX_LINES, cols=200)
        self.assertLess(time.perf_counter() - start, self.BUDGET_S)
        self.assertFalse(verdict.refused)

    def test_a_long_command_is_parsed_only_to_the_cut(self):
        # Without COMMAND_CUT one 68 KiB command takes seconds to parse.
        G.prints_secrets.cache_clear()
        start = time.perf_counter()
        self.assertFalse(G.prints_secrets("echo $x | sort ; " * 4000))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_a_redrawn_row_is_judged_once_per_text(self):
        # LOW (2026-10-11 review, finding 2): one judgement per distinct text.
        calls = []
        real = G.shows_printer

        def counting(text, budget=None):
            calls.append(text)
            return real(text, budget)

        ink = [TT.Line("$ echo $v | sort ; make", 0, 10)] * 500 + [TT.Line("$ echo done", 11, 20)] * 500
        G.shows_printer = counting
        try:
            self.assertFalse(G.rule3(0, 100, [], ink, (), 0, False))
        finally:
            G.shows_printer = real
        self.assertEqual(sorted(calls), ["$ echo $v | sort ; make", "$ echo done"])

    def test_a_dense_window_is_refused_not_slow(self):
        # Past PARSE_BUDGET the read is refused: an adversarial screen buys a
        # refusal, never minutes.
        G.prints_secrets.cache_clear()
        start = time.perf_counter()
        self.assertTrue(G.judge(_dense(1000, 30), 0, lines=G.MAX_LINES).refused)
        self.assertLess(time.perf_counter() - start, self.BUDGET_S)


class OrdinaryReads(unittest.TestCase):
    """MEDIUM (2026-10-11 review, finding 1): the headline read — a dev
    server's log — was refused for as long as `node --env-file=.env` ran (the
    span covered the whole window), and a commit message or a nodemon banner
    naming `.env` after a reader's or an interpreter's name refused any
    window holding it. Each refused on 3393daf."""

    def server(self, command: str, banner: bytes = b"") -> tuple[bytes, list]:
        echo = f"john@host:~/app$ {command}\r\n".encode()
        log = b"".join(b"GET /api/items/%d 200 3ms\r\n" % i for i in range(120))
        data = echo + banner + log
        running = span(data, command, echo, b"", end=None)
        return data, [running]

    def test_a_dev_server_started_with_an_env_file_is_readable(self):
        for command in ("node --env-file=.env server.js", "node --env-file .env --watch server.js",
                        "deno run -A --env-file=.env main.ts", "bun --env-file=.env.local run dev",
                        "python -m uvicorn app:app --reload --env-file .env"):
            with self.subTest(command=command):
                data, spans = self.server(command)
                verdict = G.judge(data, 0, lines=50, spans=spans, integrated=True)
                self.assertFalse(verdict.refused, command)
                self.assertIn("GET /api/items/119 200 3ms", verdict.text)

    def test_banners_and_commit_messages_naming_a_credential_file(self):
        banners = (b"[nodemon] starting `node --env-file=.env server.js`\r\n",
                   b"[dotenv@17.2.0] injecting env (3) from .env -- tip: encrypt with dotenvx\r\n",
                   b"   - Environments: .env.local, .env\r\n",
                   b"abc1234 Add tail call before loading .env.production\r\n",
                   b"def5678 Using python 3.12 with settings from .env\r\n")
        for banner in banners:
            with self.subTest(banner=banner):
                data, spans = self.server("npm run dev", banner)
                self.assertFalse(G.judge(data, 0, lines=50, spans=spans, integrated=True).refused)
                self.assertFalse(G.judge(data, 0, lines=50).refused)       # the text half alone

    def test_counting_and_inspecting_a_safe_field(self):
        for line in (b"$ wc -l < .env\r\n4\r\n$ ", b"$ docker inspect web --format '{{.State.Status}}'\r\nrunning\r\n$ "):
            with self.subTest(line=line):
                self.assertFalse(G.judge(line, 0).refused)

    def test_the_printers_are_still_refused(self):
        for line in (b"$ node -p process.env\r\n{ HOME: '/home/x' }\r\n$ ", b"$ cp .env /dev/stdout\r\nX=1\r\n$ ",
                     b"$ zcat .env.gz\r\nX=1\r\n$ ", b"$ tmux show-environment\r\nX=1\r\n$ ",
                     b"$ docker inspect web\r\n[ {} ]\r\n$ ", b"$ f() { cat .env; }; f\r\nX=1\r\n$ "):
            with self.subTest(line=line):
                self.assertTrue(G.judge(line, 0).refused)


class UnseenParts(unittest.TestCase):
    """LOW (2026-10-11 review, finding 4): a guard that cannot see is not a
    reason to read — for length as it was for depth. Past each bound on what
    rule 3 parses, the part never parsed refuses the read when it could hold
    a printer, and only then. Each printer here read on 3393daf."""

    PAD = "echo aaaa ; " * 60                   # 720 characters: past CANDIDATE_CAP

    def test_a_command_past_the_cut(self):
        self.assertTrue(G.prints_secrets("echo " + "a" * 5000 + " ; env"))
        self.assertTrue(G.prints_secrets("true " + "x " * 2500 + "; echo $GITHUB_TOKEN"))
        self.assertTrue(G.prints_secrets("echo " + "a" * 5000 + " ; e''nv"))     # as the shell reads it
        self.assertFalse(G.prints_secrets("echo " + "a" * 5000 + " ; make"))     # long, not refused

    def test_a_runner_past_its_tails(self):
        self.assertTrue(G.prints_secrets("ssh h " + "-o x " * 30 + "cat .env"))
        self.assertTrue(G.prints_secrets("ssh h " + "-o x " * 30 + "env"))
        self.assertFalse(G.prints_secrets("ssh h " + "-o x " * 30 + "uptime"))

    def test_wrappers_past_the_unwrap(self):
        self.assertTrue(G.prints_secrets("nohup " * 9 + "env"))
        self.assertTrue(G.prints_secrets("timeout 5 " * 9 + "cat .env"))
        self.assertFalse(G.prints_secrets("nohup " * 3 + "make"))

    def test_a_candidate_past_its_cap(self):
        for tail in ("env", "echo $GITHUB_TOKEN", "node -p process.env", "tmux show-environment", "cat .e*",
                     "e''nv", 'pr""intenv'):
            with self.subTest(tail=tail):
                self.assertTrue(G.shows_printer("$ " + self.PAD + tail))
        for tail in ("make", "ls -la", "echo done"):
            with self.subTest(tail=tail):
                self.assertFalse(G.shows_printer("$ " + self.PAD + tail))

    def test_candidates_past_the_per_line_cap(self):
        self.assertTrue(G.shows_printer("$ ls " * 17 + "$ env"))
        self.assertTrue(G.shows_printer("- ok " * 20 + "$ echo $API_TOKEN"))
        self.assertFalse(G.shows_printer("$ ls " * 40))
        self.assertFalse(G.shows_printer("- ok " * 40 + "- done"))

    def test_a_line_past_the_scan(self):
        self.assertTrue(G.shows_printer("x" * 20_000 + " ; env"))
        self.assertTrue(G.shows_printer("$ " + "x" * 20_000 + " ; cat .env"))
        self.assertTrue(G.shows_printer("x" * 20_000 + ' ; pr""intenv'))
        self.assertFalse(G.shows_printer("x" * 20_000 + " ; make"))
        self.assertFalse(G.shows_printer("const e=" + "a.b?c:d[0]*2;" * 2000))  # minified: no hazard

    def test_a_span_command_past_its_cap(self):
        from urllib.parse import quote as q
        from jarvis.v2 import terminals as T
        nonce = "0" * 32
        for command, printer in (("echo " + "a" * 5000 + " ; env", True),
                                 ("echo " + "a" * 5000 + " ; make", False)):
            with self.subTest(printer=printer):
                marks = T.Marks(nonce)
                echo = b"$ run\r\n"
                mark = f"\x1b]133;C;cmdline_url={q(command, safe='')};jarvis={nonce}\x07".encode()
                data = echo + mark + b"".join(b"line %d\r\n" % i for i in range(50))
                marks.feed(data)
                self.assertEqual(len(marks.spans), 1)
                self.assertLessEqual(len(marks.spans[0].command), T.COMMAND_CAP)
                verdict = G.judge(data, 0, lines=20, spans=list(marks.spans), integrated=True)
                self.assertEqual(verdict.refused, printer)


class FolderValues(unittest.TestCase):
    def test_env_variants_are_credential_files_here_but_templates_are_not(self):
        for name in (".env.production", ".env.staging.local", ".env.test", "dir/.env.prod"):
            self.assertTrue(G.protected_name(name), name)
        for name in (".env.example", ".env.sample", ".env.template", ".envrc", "my.env"):
            self.assertFalse(G.protected_name(name), name)

    def test_a_url_with_a_password_is_withheld(self):
        self.assertTrue(G.withhold(P("DATABASE_URL=postgres://app:", "Zx9Qp2Lm8", "@db:5432/app")))
        self.assertFalse(G.withhold("see https://example.com/docs for more"))
        self.assertFalse(G.withhold("postgres://app:****@db/app"))


if __name__ == "__main__":
    unittest.main(verbosity=1)

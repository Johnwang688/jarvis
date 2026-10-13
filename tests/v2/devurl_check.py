"""Dev servers announced in HUD terminals, the pure half (BR-8a; Browser plan
amendment 1 §D.1, §H): `jarvis/v2/devurl.py`, and the owner's switch that
gates it, `jarvis/v2/browser/settings.py` (`browser.json`).

Free and hermetic: no terminal, no daemon, no network. Every path is a temp
one; HOME is a temp folder for the whole run, so nothing here can read or
write the owner's `~/.config/jarvis`. The wiring (the frame on the attached
socket, nothing on the bus or in a log) is `terminal_check.py`'s `DevUrls`.

Written to fail against `main` before BR-8a: neither module existed.
"""
from __future__ import annotations

import collections
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# A temp HOME before `jarvis.config` is imported, so every path it derives
# (the allowlist, models.json, browser.json …) is a temp one.
_HOME = tempfile.mkdtemp(prefix="devurl-home-")
os.environ["HOME"] = _HOME
for _name in [n for n in os.environ if n.startswith("JARVIS_")]:
    del os.environ[_name]

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import devurl as D  # noqa: E402
from jarvis.v2.browser import settings as S  # noqa: E402

NONE: frozenset[int] = frozenset()


def sgr(*codes: int) -> bytes:
    return b"\x1b[" + b";".join(str(c).encode() for c in codes) + b"m"


# What each tool prints (from their own source and output), one per `SHAPES`
# row, and the URL the window should get. Escape codes where the tool uses
# them: Vite colours the port apart from the host.
VITE = (b"\x1b[2J\x1b[3J\x1b[H\r\n  " + sgr(32) + sgr(1) + b"VITE" + sgr(22) + b" v5.4.0" + sgr(39)
        + b"  " + sgr(2) + b"ready in " + sgr(0) + sgr(1) + b"312" + sgr(22) + sgr(2) + sgr(0)
        + b" ms" + sgr(22) + b"\r\n\r\n  " + sgr(32) + "➜".encode() + sgr(39) + b"  "
        + sgr(1) + b"Local" + sgr(22) + b":   " + sgr(36) + b"http://localhost:" + sgr(1) + b"5173"
        + sgr(22) + b"/" + sgr(39) + b"\r\n  " + sgr(32) + "➜".encode() + sgr(39) + b"  "
        + sgr(1) + b"Network" + sgr(22) + b": " + sgr(2) + b"use " + sgr(22) + sgr(1) + b"--host"
        + sgr(22) + sgr(2) + b" to expose" + sgr(22) + b"\r\n")
BANNERS: list[tuple[str, bytes, str]] = [
    ("local", VITE, "http://localhost:5173/"),
    ("local", b"   \xe2\x96\xb2 Next.js 15.0.3\r\n   - Local:        http://localhost:3000\r\n"
              b"   - Network:      http://192.168.1.20:3000\r\n", "http://localhost:3000/"),
    ("local", b"Compiled successfully!\r\n\r\nYou can now view app in the browser.\r\n\r\n"
              b"  Local:            http://localhost:3000\r\n  On Your Network:  http://10.0.0.4:3000\r\n",
     "http://localhost:3000/"),
    ("local", " astro  v4.16.0 ready in 210 ms\r\n\r\n┃ Local    http://localhost:4321/\r\n"
              "┃ Network  use --host to expose\r\n".encode(), "http://localhost:4321/"),
    ("local", "│   Storybook 8.3 for react-vite started   │\r\n│   Local:            "
              "http://localhost:6006/   │\r\n".encode(), "http://localhost:6006/"),
    ("local", b"\r\n  You can now view your Streamlit app in your browser.\r\n\r\n"
              b"  Local URL: http://localhost:8501\r\n  Network URL: http://10.0.0.4:8501\r\n",
     "http://localhost:8501/"),
    ("local", "   ┌──┐\r\n   │   Serving!   │\r\n"
              "   │   - Local:    http://localhost:3000       │\r\n".encode(),
     "http://localhost:3000/"),
    ("next (older)", b"ready - started server on 0.0.0.0:3000, url: http://localhost:3000\r\n",
     "http://localhost:3000/"),
    ("angular", b"** Angular Live Development Server is listening on localhost:4200, open your "
                b"browser on http://localhost:4200/ **\r\n", "http://localhost:4200/"),
    ("webpack-dev-server", b"<i> [webpack-dev-server] Project is running at:\r\n"
                           b"<i> [webpack-dev-server] Loopback: http://localhost:8080/\r\n",
     "http://localhost:8080/"),
    ("running at", b"Server running at http://localhost:1234\r\n", "http://localhost:1234/"),
    ("python -m http.server", b"Serving HTTP on 0.0.0.0 port 8000 (http://0.0.0.0:8000/) ...\r\n",
     "http://localhost:8000/"),
    ("python -m http.server", b"Serving HTTP on :: port 8001 (http://[::]:8001/) ...\r\n",
     "http://localhost:8001/"),
    ("running on", b" * Serving Flask app 'app'\r\n * Debug mode: on\r\n"
                   b" * Running on http://127.0.0.1:5000\r\nPress CTRL+C to quit\r\n",
     "http://localhost:5000/"),
    ("running on", b"INFO:     Uvicorn running on " + sgr(1) + b"http://127.0.0.1:8000" + sgr(0)
                   + b" (Press CTRL+C to quit)\r\n", "http://localhost:8000/"),
    ("running on", b"* Running on local URL:  http://127.0.0.1:7860\r\n", "http://localhost:7860/"),
    ("django", b"Django version 5.1, using settings 'site.settings'\r\n"
               b"Starting development server at http://127.0.0.1:8000/\r\nQuit the server with CONTROL-C.\r\n",
     "http://localhost:8000/"),
    ("listening on", b"* Listening on http://127.0.0.1:3000\r\n* Listening on http://[::1]:3000\r\n",
     "http://localhost:3000/"),
    ("hugo", b"Web Server is available at http://localhost:1313/ (bind address 127.0.0.1)\r\n",
     "http://localhost:1313/"),
    ("jekyll", b"    Server address: http://127.0.0.1:4000/\r\n  Server running... press ctrl-c to stop.\r\n",
     "http://localhost:4000/"),
    ("php -S", b"[Mon Oct 12 10:00:00 2026] PHP 8.3.6 Development Server (http://localhost:8080) started\r\n",
     "http://localhost:8080/"),
    ("expo", "› Metro waiting on exp://10.0.0.4:8081\r\n› Web is waiting on "
             "http://localhost:8081\r\n".encode(), "http://localhost:8081/"),
    ("serving on", b"INFO    -  [10:00:00] Serving on http://127.0.0.1:8000/\r\n", "http://localhost:8000/"),
    ("eleventy", b"[11ty] Server at http://localhost:8080/\r\n", "http://localhost:8080/"),
    ("jupyter", b"[I 2026-10-12 10:00:00.000 ServerApp] Jupyter Server 2.14.2 is running at:\r\n"
                b"[I 2026-10-12 10:00:00.000 ServerApp] http://localhost:8888/tree?token=abc0123456789def\r\n"
                b"[I 2026-10-12 10:00:00.000 ServerApp]     http://127.0.0.1:8888/tree?token=abc0123456789def\r\n",
     "http://localhost:8888/tree"),
]


def run(sniffer: D.DevUrlSniffer, chunks) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, tuple):            # a mark, between chunks
            found += sniffer.mark(chunk[0])
        else:
            found += sniffer.feed(chunk)
    return found


def sent(chunks) -> list[str]:
    """What a terminal would send: found, then admitted (a second apart, so
    only de-duplication decides). Puma's two lines are one server."""
    now = [0.0]

    def clock():
        now[0] += D.RATE_GAP_S
        return now[0]
    sniffer = D.DevUrlSniffer(lambda: NONE, clock=clock)
    return [url for url in run(sniffer, chunks) if admit(sniffer, url)]


def admit(sniffer: D.DevUrlSniffer, url: str) -> bool:
    """What the terminal does with a URL and a socket that takes the frame."""
    if not sniffer.may_send(url):
        return False
    sniffer.sent(url)
    return True


def split_every_byte(data: bytes) -> list[bytes]:
    return [data[i:i + 1] for i in range(len(data))]


class Shapes(unittest.TestCase):
    def test_every_row_of_the_table_gives_one_cleaned_url(self):
        names = {name for name, _ in D.SHAPES}
        covered = set()
        for name, banner, url in BANNERS:
            with self.subTest(banner=banner[:50]):
                self.assertEqual(sent([banner]), [url])
                # And the row that found it is the one the table says.
                lines = [line for line in banner.replace(b"\r", b"").split(b"\n") if b"://" in line]
                plain = [D._SGR.sub(b"", line).decode() for line in lines]
                hit = next(n for line in plain for n, shape in D.SHAPES
                           if (m := shape.search(line)) and D.clean(m.group("url")))
                self.assertEqual(hit, name)
                covered.add(name)
        self.assertEqual(covered, names, "every row of SHAPES has a banner here")

    def test_split_anywhere_with_escapes_found_once(self):
        rng = random.Random(7)
        for name, banner, url in BANNERS:
            with self.subTest(banner=banner[:40]):
                self.assertEqual(sent(split_every_byte(banner)), [url])
                for _ in range(20):
                    cuts = sorted(rng.sample(range(1, len(banner)), min(4, len(banner) - 1)))
                    chunks = [banner[a:b] for a, b in zip([0, *cuts], [*cuts, len(banner)])]
                    self.assertEqual(sent(chunks), [url])

    def test_escapes_in_every_form(self):
        cases = [
            # Colour around the port, in the 8-bit C1 form.
            (b"  Local:   http://localhost:\xc2\x9b1m5174\xc2\x9b22m/\r\n", "http://localhost:5174/"),
            # A C0 control inside a CSI is executed and the CSI goes on.
            (b"  Local:   http://localhost:\x1b[1\x07m5175/\r\n", "http://localhost:5175/"),
            # Cursor forward is a gap; a title (OSC) and a hyperlink's target draw nothing.
            (b"Local:\x1b[3Chttp://localhost:5176/\r\n", "http://localhost:5176/"),
            (b"\x1b]0;Local: http://localhost:9999/\x07  Local: http://localhost:5177/\r\n",
             "http://localhost:5177/"),
            (b"  Local: \x1b]8;;http://localhost:9998/\x1b\\http://localhost:5178/\x1b]8;;\x1b\\\r\n",
             "http://localhost:5178/"),
            # A progress line overwritten by CR, then the banner.
            (b"building 40%\rbuilding 90%\r  Local: http://localhost:5179/\n", "http://localhost:5179/"),
        ]
        for data, url in cases:
            with self.subTest(data=data):
                self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [data]), [url])
                self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), split_every_byte(data)), [url])

    def test_what_is_drawn_over_is_not_read(self):
        for data in (
            b"  Local: http://localhost:51\b\b73/\r\n",          # backspaced
            b"  Local: http://localhost:5180/\x1b[4D9999\r\n",     # cursor back, overwritten
            b"  Local: http://localhost:5181/\x1b[2P\r\n",         # characters deleted
            b"\x1b]0;  Local: http://localhost:5182/\x07\r\n",     # a title only
            b"\x1b[?1049h  Local: http://localhost:5183/\r\n\x1b[?1049l",   # the alternate screen
        ):
            with self.subTest(data=data):
                self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [data]), [])

    def test_the_alternate_screen_ends_with_a_prompt_or_its_exit(self):
        sniffer = D.DevUrlSniffer(lambda: NONE)
        self.assertEqual(run(sniffer, [b"\x1b[?1049h", (b"A",), (b"C",),
                                       b"  Local: http://localhost:5184/\r\n"]), ["http://localhost:5184/"])
        sniffer = D.DevUrlSniffer(lambda: NONE)
        self.assertEqual(run(sniffer, [b"\x1b[?1049hvim\x1b[?1049l\r\n  Local: http://localhost:5185/\n"]),
                         ["http://localhost:5185/"])


class Cleaning(unittest.TestCase):
    def test_a_jupyter_token_and_any_fragment_never_travel(self):
        self.assertEqual(D.clean("http://localhost:8888/tree?token=abc123#x"), "http://localhost:8888/tree")
        self.assertEqual(D.clean("http://localhost:8888/lab/?token=abc"), "http://localhost:8888/lab/")
        self.assertEqual(D.clean("http://localhost:3000/#/route"), "http://localhost:3000/")
        found = run(D.DevUrlSniffer(lambda: NONE), [b"     or http://127.0.0.1:8888/?token=abc0123456789\r\n"])
        self.assertEqual(found, ["http://localhost:8888/"])
        self.assertNotIn("token", json.dumps(found))

    def test_only_loopback_with_an_explicit_port(self):
        good = {
            "http://localhost:3000": "http://localhost:3000/",
            "https://LOCALHOST:3443/app": "https://localhost:3443/app",
            # Every loopback host is sent as `localhost`, the port and path kept
            # (review of PR #70: the window can frame no other loopback name).
            "http://127.0.0.1:5000/": "http://localhost:5000/",
            "http://127.4.5.6:5001/a/b": "http://localhost:5001/a/b",
            "http://127.255.255.254:5002": "http://localhost:5002/",
            "http://[::1]:3000/": "http://localhost:3000/",
            "https://[0:0:0:0:0:0:0:1]:3443/x": "https://localhost:3443/x",
            "http://0.0.0.0:8000/": "http://localhost:8000/",
            "http://[::]:8000/": "http://localhost:8000/",
            "http://localhost:3000/café": "http://localhost:3000/caf%C3%A9",
            "http://localhost:3000/).": "http://localhost:3000/",
        }
        for raw, want in good.items():
            with self.subTest(raw=raw):
                self.assertEqual(D.clean(raw), want)
        for raw in ("http://192.168.1.5:5173/", "http://10.0.0.4:3000", "http://example.com:3000/",
                    "http://foo.localhost:3000/", "http://127.1:3000/", "http://2130706433:3000/",
                    "http://[::ffff:127.0.0.1]:3000/", "http://localhost/", "http://localhost:/",
                    "http://localhost:0/", "http://localhost:70000/", "http://user:pw@localhost:3000/",
                    "http://user@localhost:3000/", "ftp://localhost:21/", "file:///etc/passwd",
                    "exp://localhost:8081", "javascript://localhost:3000/"):
            with self.subTest(raw=raw):
                self.assertIsNone(D.clean(raw))

    def test_jarvis_ports_are_never_announced(self):
        refused = set(D.jarvis_ports())
        self.assertTrue({8402, 8403, 8404, 8405, 8406} <= refused)
        with patch.object(config, "FACE_PORT", 18402), patch.object(config, "WORKSHOP_PORT", 18403):
            moved = D.jarvis_ports()
        self.assertTrue({18402, 18403, 8402, 8403} <= moved, "configured and default")
        for port in sorted(refused):
            for host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0"):
                line = f"  Local: http://{host}:{port}/".encode() + b"\r\n"
                with self.subTest(port=port, host=host):
                    self.assertEqual(run(D.DevUrlSniffer(), [line]), [])
        # And whatever the caller adds (the daemon's listeners as bound).
        sniffer = D.DevUrlSniffer(lambda: {31337})
        self.assertEqual(run(sniffer, [b"  Local: http://localhost:31337/\r\n"]), [])
        self.assertEqual(run(sniffer, [b"  Local: http://localhost:31338/\r\n"]), ["http://localhost:31338/"])

    def test_what_is_not_an_announcement_is_not_read(self):
        for line in (b"  Network: http://192.168.1.5:5173/", b"curl http://localhost:3000",
                     b"see http://localhost:3000 for details", b"  Local: http://example.com:3000/",
                     b"GET http://localhost:3000/api 200", b"localhost:3000 is up"):
            with self.subTest(line=line):
                self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [line + b"\r\n"]), [])


class Marks(unittest.TestCase):
    """Only a command's output: the prompt and the typed line are skipped."""

    def test_the_typed_line_between_b_and_c_is_never_read(self):
        typed = b"curl http://localhost:3000 # Local: http://localhost:3001/\r\n"
        output = b"  Local: http://localhost:5190/\r\n"
        session = [(b"A",), b"fake$ ", (b"B",), typed, (b"C",), output, (b"D",),
                   (b"A",), b"fake$ ", (b"B",), b"echo  Local: http://localhost:3002/\r\n", (b"C",),
                   b"Local: http://localhost:5191/\r\n", (b"D",), (b"A",), b"fake$ "]
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), session),
                         ["http://localhost:5190/", "http://localhost:5191/"])

    def test_a_line_split_across_chunks_is_found_once(self):
        data = b"  Local: http://localhost:5192/\r\n"
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                chunks = [(b"A",), b"$ ", (b"B",), b"npm run dev\r\n", (b"C",), data[:cut], data[cut:]]
                self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), chunks), ["http://localhost:5192/"])

    def test_bash_skips_from_b_to_c_even_across_lines(self):
        # Once a C mark has come, B..C is the typed line whatever it holds (a
        # continuation line, readline moving down a wrapped line).
        sniffer = D.DevUrlSniffer(lambda: NONE)
        run(sniffer, [(b"A",), (b"B",), b"true\r\n", (b"C",), (b"D",)])
        self.assertEqual(sniffer.mode, D.READ)
        self.assertEqual(run(sniffer, [(b"A",), b"$ ", (b"B",)]), [])
        self.assertEqual(sniffer.mode, D.SKIP)
        self.assertEqual(run(sniffer, [b"echo x \\\r\n> Local: http://localhost:3003/\r\n"]), [])

    def test_a_shell_that_marks_no_command_skips_only_the_typed_line(self):
        # The POSIX startup file marks A and B, never C: after B the typed
        # line runs to its line feed, and what follows is the command's.
        sniffer = D.DevUrlSniffer(lambda: NONE)
        found = run(sniffer, [(b"A",), b"$ ", (b"B",), b"echo Local: http://localhost:3004/\r\n",
                              b"Local: http://localhost:5193/\r\n", (b"A",), b"$ ", (b"B",)])
        self.assertEqual(found, ["http://localhost:5193/"])
        self.assertEqual(sniffer.mode, D.SKIP_LINE)

    def test_no_marks_at_all_reads_everything(self):
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [b"$ npm run dev\r\n  Local: http://localhost:5194/\r\n"]),
                         ["http://localhost:5194/"])

    def test_output_with_no_newline_before_the_prompt_still_counts(self):
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE),
                             [(b"C",), b"Local: http://localhost:5195/", (b"D",), (b"A",)]),
                         ["http://localhost:5195/"])

    def test_paused_output_is_not_read_and_the_line_it_resumes_in_is_dropped(self):
        # The terminal feeds nothing while auto-open is off; it pauses the
        # sniffer instead. Fed again mid-line, that line was not seen whole.
        sniffer = D.DevUrlSniffer(lambda: NONE)
        run(sniffer, [b"  Local: http://local"])
        sniffer.pause()
        self.assertEqual(run(sniffer, [b"ocal: http://localhost:5197/\r\n  Local: http://localhost:5198/\r\n"]),
                         ["http://localhost:5198/"])
        self.assertEqual(sniffer.held(), 0)


class Bounds(unittest.TestCase):
    def assert_small(self, sniffer: D.DevUrlSniffer):
        self.assertLessEqual(len(sniffer._line), D.LINE_CAP)
        self.assertLessEqual(len(sniffer._carry), D.CARRY_CAP)
        self.assertLessEqual(sniffer.held(), D.LINE_CAP + D.CARRY_CAP)
        self.assertLessEqual(len(sniffer._recent), D.RECENT)
        self.assertLessEqual(len(sniffer._sent), D.RATE_MAX)
        sizes = {k: len(v) for k, v in vars(sniffer).items()
                 if isinstance(v, (bytes, bytearray, list, dict, set, frozenset, collections.deque))}
        self.assertEqual(set(sizes), {"_line", "_carry", "_recent", "_sent"}, sizes)

    def test_ten_megabytes_leave_at_most_a_line_and_four_urls(self):
        rng = random.Random(3)
        sniffer = D.DevUrlSniffer(lambda: NONE)
        pieces = [b"x" * 3000, b"\x1b[" + b"1;" * 200, b"\x1b]0;" + b"t" * 5000, b"\r\n",
                  b"\x1b[31merror\x1b[0m: something\r\n", os.urandom(512), b"Local: http://localhost:",
                  b"\x1bP" + b"d" * 700 + b"\x1b\\", b"\xc2", b"\x9b?1049h", b"\x1b[?1049l"]
        fed = 0
        while fed < 10_000_000:
            chunk = b"".join(rng.choice(pieces) for _ in range(40))
            sniffer.feed(chunk)
            fed += len(chunk)
            self.assert_small(sniffer)
        # One endless line: dropped whole, never kept.
        sniffer = D.DevUrlSniffer(lambda: NONE)
        for _ in range(160):
            sniffer.feed(b"Local: http://localhost:5199/" + b"y" * 65_000)
            self.assert_small(sniffer)
        self.assertEqual(sniffer.feed(b"\r\n"), [])

    def test_a_line_longer_than_the_cap_is_dropped_whole(self):
        long_line = b"  Local: http://localhost:5200/" + b" " * D.LINE_CAP + b"\r\n"
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [long_line]), [])
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), [b"a\r\n" + long_line + b"b\r\n"]), [])
        self.assertEqual(run(D.DevUrlSniffer(lambda: NONE), split_every_byte(long_line)), [])

    def test_a_flood_is_cheap(self):
        colour = b"\x1b[32mok\x1b[0m test \x1b[1mname\x1b[22m passed in \x1b[33m3ms\x1b[0m\r\n" * 120_000
        lines = b"line of output with no address in it at all\r\n" * 220_000
        # PR #70 review: URL-dense output (another host's, or this machine's
        # access log) and a flood of real announcements.
        network = b"  Network: http://192.168.1.20:5173/assets/app.js 200 OK\r\n" * 160_000
        access = b"GET http://localhost:3000/api/items?page=2 200 12ms\r\n" * 180_000
        local = b"".join(b"  Local:   http://localhost:%d/\r\n" % (1024 + i) for i in range(280_000))
        # Twice what xterm draws (~14 MB/s), best of two runs each.
        for data in (colour, lines, os.urandom(4_000_000), network, access, local):
            best = 0.0
            for _ in range(2):
                sniffer = D.DevUrlSniffer(lambda: NONE)
                started = time.perf_counter()
                for i in range(0, len(data), 65536):
                    sniffer.feed(data[i:i + 65536])
                best = max(best, len(data) / (1 << 20) / (time.perf_counter() - started))
            with self.subTest(flood=data[:30]):
                self.assertGreater(best, 28.0, f"{best:.1f} MB/s")

    def test_a_chunk_matches_at_most_its_budget(self):
        lines = b"".join(b"  Local:   http://localhost:%d/\r\n" % (2000 + i) for i in range(200))
        calls = []
        real = D.find_url

        def counting(text, refused=()):
            calls.append(text)
            return real(text, refused)
        with patch.object(D, "find_url", counting):
            found = D.DevUrlSniffer(lambda: NONE).feed(lines)
        self.assertEqual(len(calls), D.MATCH_BUDGET)
        self.assertEqual(found[0], "http://localhost:2000/")
        # Each chunk (and each mark) starts with a fresh budget.
        sniffer = D.DevUrlSniffer(lambda: NONE)
        sniffer.feed(lines)
        self.assertEqual(sniffer.feed(b"  Local: http://localhost:2999/\r\n"), ["http://localhost:2999/"])

    def test_other_urls_never_spend_the_budget(self):
        # The cheap tests keep lines that cannot announce a loopback server
        # (another host's URLs, a loopback access log) from the table, so an
        # announcement among them in one chunk is still found.
        noise = (b"  Network: http://192.168.1.20:5173/assets/app.js 200 OK\r\n" * 40
                 + b"GET http://localhost:3000/api/items?page=2 200 12ms\r\n" * 40)
        found = D.DevUrlSniffer(lambda: NONE).feed(b"\r\n" + noise + b"  Local:   http://localhost:5173/\r\n"
                                                    + noise)
        self.assertEqual(found, ["http://localhost:5173/"])


def unfiltered(line: str, refused=()) -> str | None:
    """`find_url` without its two cheap tests: the table alone."""
    for _, shape in D.SHAPES:
        match = shape.search(line)
        if match is not None:
            url = D.clean(match.group("url"), refused)
            if url is not None:
                return url
    return None


def corpus(count: int, seed: int = 11) -> list[str]:
    """Lines shaped like announcements, near misses and noise, in every case
    and the spellings IGNORECASE folds (ſ, ı, İ, the Kelvin sign)."""
    rng = random.Random(seed)
    prefixes = ["", "  ", "➜  ", "- ", "┃ ", "[I 2026-10-12 ServerApp] ", "INFO:     ", " * ",
                "│   ", "     or ", "GET ", "2026-10-12 ", "é "]
    labels = ["Local:", "LOCAL:", "Local URL:", "local", "Locaͥl:", "Local ", "Running on",
              "Uvicorn running on", "Running on local URL:", "RUNNING ON", "Listening on",
              "Liſtening on", "Lıstening on", "LİSTENING ON", "Loopback:", "LOOPBACK:",
              "open your browser on", "Server running at", "is running at:", "Project is running at",
              "Serving HTTP on 0.0.0.0 port 8000", "Starting development server at",
              "PHP 8.3 Development Server", "is available at", "Server address:", "Web is waiting on",
              "Serving on", "[11ty] Server at", "started server on 0.0.0.0:3000, url:", "Network:",
              "url:", "see", "", "KEY local:"]
    seps = [" ", "  ", "\t", " ", ": ", ""]
    urls = ["http://localhost:3000/", "http://LOCALHOST:3001", "http://LocalHost:3004/a",
            "http://127.0.0.1:5000", "http://127.9.9.9:5001/x", "http://[::1]:3002/", "http://[::]:8000/",
            "(http://0.0.0.0:8000/)", "(http://localhost:8080)", "http://192.168.1.2:5173/",
            "http://example.com:80/", "http://localhoſt:3000", "http://[0:0:0:0:0:0:0:1]:3003/",
            "http://localhost:8888/tree?token=x", "http://127.0.0.1:8888/?token=y",
            "http://localhost:8888/lab", "http://localhost:8402/", "https://localhost:3443/app",
            "http://localhost/", "http://[::ffff:127.0.0.1]:3005/", "http://foo.localhost:3006/",
            "exp://10.0.0.4:8081", "http://0.0.0.0:3007"]
    suffixes = ["", "/", " (Press CTRL+C to quit)", ")", " started", "  │", " ** ", ".", " "]
    lines = []
    for _ in range(count):
        lines.append(rng.choice(prefixes) + rng.choice(labels) + rng.choice(seps) + rng.choice(urls)
                     + rng.choice(suffixes))
    return lines


class Prefilter(unittest.TestCase):
    """PR #70 review: two cheap tests run before the table, and neither may
    change an answer."""

    def test_the_cheap_tests_never_change_an_answer(self):
        refused = set(D.jarvis_ports())
        lines = corpus(20_000)
        for _, banner, _ in BANNERS:
            lines += [D._SGR.sub(b"", line).decode() for line in banner.split(b"\r\n")]
        hits = 0
        for line in lines:
            want = unfiltered(line, refused)
            with self.subTest(line=line):
                self.assertEqual(D.find_url(line, refused), want)
                if want is not None:
                    hits += 1
                    self.assertTrue(D._has_host(line.encode()), "the bytes host test")
                    self.assertTrue(D._has_word(line), "the literal word test")
                    self.assertIsNotNone(D._HOSTS.search(line))
                    self.assertIsNotNone(D._WORDS.search(line))
        self.assertGreater(hits, 2000, "the corpus finds URLs, not only misses")

    def test_the_literal_word_test_is_the_regex_or_wider(self):
        # `_has_word` stands in for `_WORDS` before decoding is paid for: true
        # wherever the regex matches, in every folded spelling.
        for line in corpus(20_000, seed=12):
            if D._WORDS.search(line):
                with self.subTest(line=line):
                    self.assertTrue(D._has_word(line))
        for spelled in ("LIſTENING", "lıstening", "LİSTENING", "KEY lOcAl:",
                        "Local　http", "SERVING http", "loOpBack"):
            if D._WORDS.search(spelled):
                self.assertTrue(D._has_word(spelled), spelled)

    def test_the_sniffer_finds_what_the_table_finds(self):
        # Through `feed`, chunks of 20 lines (within the match budget), so the
        # bulk tests over a chunk's run of lines are exercised too.
        refused = set(D.jarvis_ports())
        lines = corpus(6_000, seed=13)
        for start in range(0, len(lines), 20):
            group = lines[start:start + 20]
            want = list(dict.fromkeys(u for u in (unfiltered(x, refused) for x in group) if u))
            data = "\r\n".join(group).encode() + b"\r\n"
            with self.subTest(start=start):
                self.assertEqual(D.DevUrlSniffer(lambda: refused).feed(b"\r\n" + data), want)

    def test_every_loopback_host_is_sent_as_localhost(self):
        sniffer = D.DevUrlSniffer(lambda: NONE)
        found = run(sniffer, [b"* Listening on http://[::1]:3100/a\r\n",
                              b"* Listening on http://127.0.0.2:3101/b?x=1\r\n",
                              b" * Running on http://127.0.0.1:3102\r\n",
                              b"Serving HTTP on :: port 3103 (http://[::]:3103/) ...\r\n"])
        self.assertEqual(found, ["http://localhost:3100/a", "http://localhost:3101/b",
                                 "http://localhost:3102/", "http://localhost:3103/"])
        # So a server Puma announces twice uses one frame, and the window can
        # frame what it gets.
        self.assertEqual(sent([b"* Listening on http://127.0.0.1:3000\r\n* Listening on http://[::1]:3000\r\n"]),
                         ["http://localhost:3000/"])


class Admit(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.sniffer = D.DevUrlSniffer(lambda: NONE, clock=lambda: self.now)

    def test_one_frame_per_origin_loopback_hosts_folded(self):
        self.assertTrue(admit(self.sniffer, "http://127.0.0.1:3000/"))
        self.now += 10
        for again in ("http://127.0.0.1:3000/", "http://[::1]:3000/", "http://localhost:3000/other"):
            self.assertFalse(admit(self.sniffer, again), again)
        self.assertTrue(admit(self.sniffer, "https://localhost:3000/"), "another scheme is another origin")

    def test_rate_limited_per_terminal(self):
        self.assertTrue(admit(self.sniffer, "http://localhost:4001/"))
        self.assertFalse(admit(self.sniffer, "http://localhost:4002/"), "within RATE_GAP_S")
        self.now += D.RATE_GAP_S
        self.assertTrue(admit(self.sniffer, "http://localhost:4002/"))
        for port in (4003, 4004):
            self.now += D.RATE_GAP_S
            self.assertTrue(admit(self.sniffer, f"http://localhost:{port}/"))
        self.now += D.RATE_GAP_S
        self.assertFalse(admit(self.sniffer, "http://localhost:4005/"), f"{D.RATE_MAX} a window")
        self.now = 1000.0 + D.RATE_WINDOW_S
        self.assertTrue(admit(self.sniffer, "http://localhost:4005/"))

    def test_the_last_four_are_remembered(self):
        for port in (4101, 4102, 4103, 4104, 4105):
            self.now += D.RATE_WINDOW_S
            self.assertTrue(admit(self.sniffer, f"http://localhost:{port}/"))
        self.now += D.RATE_WINDOW_S
        self.assertTrue(admit(self.sniffer, "http://localhost:4101/"), "the oldest was let go")
        self.assertFalse(admit(self.sniffer, "http://localhost:4105/"))

    def test_found_is_not_sent_until_admitted(self):
        found = self.sniffer.feed(b"  Local: http://localhost:4201/\r\n")
        self.assertEqual(found, ["http://localhost:4201/"])
        self.assertEqual(len(self.sniffer._recent), 0, "finding remembers nothing")
        self.assertEqual(self.sniffer.feed(b"  Local: http://localhost:4201/\r\n"), found)


class Settings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = 50.0
        self.path = self.root / ".config" / "jarvis" / "browser.json"

    def settings(self) -> S.BrowserSettings:
        return S.BrowserSettings(self.path, clock=lambda: self.now)

    def test_on_by_default_and_memory_only_without_a_file(self):
        memory = S.BrowserSettings()
        self.assertIs(memory.auto_open(), True)
        self.assertEqual(memory.snapshot(), dict(S.DEFAULTS))
        self.assertEqual(memory.update(auto_open=False),
                         {**S.DEFAULTS, "auto_open": False})
        self.assertIs(memory.auto_open(), False)
        self.assertIs(memory.value("auto_open"), False)
        with self.assertRaises(KeyError):
            memory.value("unknown_setting")
        self.assertIsNone(memory.path)
        self.assertIs(self.settings().auto_open(), True)              # no file yet
        self.assertFalse(self.path.exists())

    def test_saved_owner_only_keeping_what_it_does_not_know(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"auto_open": True, "lan": True, "sound": False}))
        settings = self.settings()
        self.assertEqual(settings.update(auto_open=False),
                         {**S.DEFAULTS, "lan": True, "auto_open": False, "sound": False})
        self.assertEqual(json.loads(self.path.read_text()), {"auto_open": False, "lan": True, "sound": False})
        self.assertEqual(settings.update(local_servers=True)["local_servers"], True)
        self.assertEqual(json.loads(self.path.read_text()),
                         {"auto_open": False, "lan": True, "local_servers": True, "sound": False})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["browser.json"], "no temp left")
        for bad in ({"auto_open": "no"}, {"auto_open": 1}, {"other": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settings.update(**bad)

    def test_a_hand_edit_lands_within_a_second(self):
        settings = self.settings()
        self.assertIs(settings.auto_open(), True)
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"auto_open": false}')
        self.assertIs(settings.auto_open(), True, "not looked at again yet")
        self.now += S.RECHECK_S
        self.assertIs(settings.auto_open(), False)
        self.path.unlink()
        self.now += S.RECHECK_S
        self.assertIs(settings.auto_open(), True, "gone: the default")

    def test_the_gates_reads_recheck_within_a_second(self):
        # BR-1a's gate asks `lan` and `local_servers` on every connection:
        # memory between `stat`s, a hand edit within RECHECK_S, no read of
        # the file when it has not changed.
        settings = self.settings()
        self.assertEqual((settings.value("lan"), settings.value("local_servers")), (False, False))
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"lan": true, "local_servers": true}')
        self.assertFalse(settings.value("lan"), "not looked at again yet")
        self.now += S.RECHECK_S
        reads = []
        real = S._read
        with patch.object(S, "_read", lambda path: reads.append(path) or real(path)):
            self.assertTrue(settings.value("lan"))
            self.assertTrue(settings.value("local_servers"))
            for _ in range(1000):
                settings.value("lan")
            self.now += S.RECHECK_S
            settings.value("lan")
        self.assertEqual(len(reads), 1, "read once; then only a stat a second, the file unchanged")

    def test_one_reader_one_writer(self):
        # The reconciliation with BR-1a (#69): `browser.json` is opened only by
        # `settings.py`. Its path is defined once (a merge left two, and the
        # later won) and named nowhere else but the attach in daemon2's
        # `main()` and the protected-state list; no other module defines a
        # reader or a writer of it.
        import ast
        repo = Path(__file__).resolve().parents[2]
        tree = ast.parse((repo / "jarvis" / "config.py").read_text())
        defined = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
                   and any(isinstance(t, ast.Name) and t.id == "BROWSER_SETTINGS_PATH"
                           for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
        self.assertEqual(len(defined), 1, "BROWSER_SETTINGS_PATH defined once")
        allowed = {"jarvis/config.py", "jarvis/v2/daemon.py", "jarvis/tools/files.py",
                   "jarvis/v2/browser/settings.py"}
        named = sorted(path.relative_to(repo).as_posix() for path in (repo / "jarvis").rglob("*.py")
                       if "BROWSER_SETTINGS_PATH" in path.read_text(encoding="utf-8"))
        self.assertEqual(set(named) - allowed, set(), "only these name the settings file")
        daemon = (repo / "jarvis" / "v2" / "daemon.py").read_text()
        self.assertEqual(daemon.count("BROWSER_SETTINGS_PATH"), 1, "the attach in main()")
        self.assertIn("browser_settings.attach(config.BROWSER_SETTINGS_PATH)", daemon)
        for path in (repo / "jarvis" / "v2" / "browser").glob("*.py"):
            if path.name == "settings.py":
                continue
            text = path.read_text(encoding="utf-8")
            for word in ("def load_settings", "def save_settings", "browser.json\"", "SETTINGS_DEFAULTS"):
                self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_what_cannot_be_read_is_the_default(self):
        self.path.parent.mkdir(parents=True)
        target = self.root / "elsewhere.json"
        target.write_text('{"auto_open": false}')
        for setup in (lambda: self.path.write_text("not json"),
                      lambda: self.path.write_text('{"auto_open": "false"}'),
                      lambda: self.path.write_text('[false]'),
                      lambda: self.path.write_text('{"auto_open": false, "pad": "' + "x" * S.FILE_CAP + '"}'),
                      lambda: self.path.symlink_to(target),
                      lambda: os.mkfifo(self.path)):
            with self.subTest(setup=setup):
                if self.path.exists() or self.path.is_symlink():
                    self.path.unlink()
                setup()
                started = time.monotonic()
                self.assertIs(self.settings().auto_open(), True)
                self.assertLess(time.monotonic() - started, 2.0, "a FIFO never hangs the read")
                self.path.unlink()

    def test_an_empty_setting_names_no_folder(self):
        # PR #70 review: `JARVIS_BROWSER_SETTINGS=""` gave `Path("")`, which is
        # `.` — the working folder in the gate's protected state. Read at
        # import, so a fresh interpreter answers.
        import subprocess
        repo = Path(__file__).resolve().parents[2]
        probe = ("from jarvis import config; from jarvis.tools import files; "
                 "print(config.BROWSER_SETTINGS_PATH); "
                 "print(any(str(p) in ('.', '') for p in files._protected_state_paths()))")
        for value in ("", None):
            env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
            env.update(HOME=str(self.root), PYTHONPATH=str(repo))
            if value is not None:
                env["JARVIS_BROWSER_SETTINGS"] = value
            out = subprocess.run([sys.executable, "-c", probe], env=env, cwd=self.root,
                                 capture_output=True, text=True, timeout=60)
            with self.subTest(value=value):
                self.assertEqual(out.returncode, 0, out.stderr[-500:])
                path, dot = out.stdout.split()
                self.assertEqual(path, str(self.root / ".config" / "jarvis" / "browser.json"))
                self.assertEqual(dot, "False")

    def test_browser_json_is_gate_state(self):
        from jarvis import permissions, rules
        from jarvis.tools import files
        home = self.root / "home"
        allow = home / ".config" / "jarvis" / "allowlist.json"
        allow.parent.mkdir(parents=True)
        target = allow.parent / "browser.json"
        with patch.dict(os.environ, {"HOME": str(home)}), \
                patch.object(config, "ALLOWLIST_PATH", allow), \
                patch.object(config, "BROWSER_SETTINGS_PATH", self.root / "moved" / "browser.json"):
            protected = files._protected_state()
            self.assertIn(target.resolve(), protected, "by name beside the allowlist")
            self.assertIn((self.root / "moved" / "browser.json").resolve(), protected, "where config puts it")
            refusal = files.write_file(str(target), '{"auto_open": false}')
            self.assertFalse(target.exists(), refusal)
            verdict = permissions.static_verdict(f"cp /tmp/x {target}")
            self.assertNotEqual(verdict.decision, rules.ALLOW, verdict)


if __name__ == "__main__":
    unittest.main(verbosity=2)

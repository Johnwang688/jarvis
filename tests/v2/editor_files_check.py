"""The editor's files (ED-2): the content precondition, the change poll, the
one classifier, the owner-only routes, and the owner's `.env*` rule.

Free and hermetic: a real daemon on three ephemeral loopback ports (never
8402, 8403 or 8405), every config path and HOME temporary, and no real
`.env*` file is ever read — the env files here are made in the temp HOME with
fake values. The browser half uses real Chromium against that daemon, because
a same-origin GET carries no `Origin` and only a real browser sends the fetch
metadata `projects.is_owner` reads.

What it pins, each written to fail on b04d6e8 (ED-1's main):

  - **the name rule** (`secrets.is_env_credential`): every listed name, case
    variants, a trailing dot or space, `.example` names open, `.env.sample`
    and `.env.template` credential, `.envrc` and `myapp.env` not; a symlink
    named `.env.example` pointing at `.env`, a symlink named `.env` pointing
    anywhere, a chain through a `.env` hop, a loop; `/mnt/c` paths;
  - **the kinds** (`editor_policy`): withheld (a key in `.ssh`, Claude's and
    Codex's login files, git's credential store, a token bundle), the gate's
    state readable but read-only even inside `~/.config/jarvis`, the safety
    layer read-only, an env file the owner's, not-text read-only;
  - **the precondition**: a mismatched etag is a 409 carrying the current
    etag and leaves the file byte-identical; **a rewrite that restores the
    old mtime with `os.utime` is still a 409** (the mtime guard passed it);
    a `touch` alone is not a conflict; `null` creates and is refused over an
    existing file; a gone file answers `etag: null`; a Latin-1 file is
    read-only and its PUT refused, its bytes unchanged; modes kept, a new env
    file 0600;
  - **the poll** (`POST …/files/stat`): its cap, gone and withheld rows, a
    file hashed again only when its stat moved — and a same-size rewrite
    `utime`d back is still seen;
  - **owner-only**: every editor file route refuses the API listener and a
    request with no Origin and no same-origin fetch metadata; an env file's
    content never reaches the API listener, the bus, a log line, a diff, the
    preview origin or an `@path` attachment.

Run: PYTHONPATH=. .venv/bin/python tests/v2/editor_files_check.py
"""
from __future__ import annotations

import hashlib
import http.client
import http.server
import io
import json
import logging
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import editor_policy as EP, hud_api as H  # noqa: E402
from jarvis.v2.daemon import Daemon  # noqa: E402
from jarvis.v2.model import ProviderName as PN  # noqa: E402
from jarvis.v2.provider import Decision, Event, EventKind as K, SessionHandle, Usage  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402

LIVE_PORTS = {8402, 8403, 8405}
LIVE = re.compile(r"^[a-z]+://[^/]*:(8402|8403|8405)(/|$)")
# A fake credential, made up for these tests: it must never come out anywhere
# but the owner's own read.
FAKE_SECRET = "sk-fake-ed2-0123456789abcdef"

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the venv has it
    sync_playwright = None

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))


def tag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


class Fake:
    def __init__(self, name):
        self.name = name

    def health(self):
        return True, "fake"

    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, self.name, "fake:" + thread.id, SimpleNamespace(brief=brief))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        yield Event(K.TEXT, handle.thread_id, {"text": "ok"})
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})

    def interrupt(self, handle):
        pass

    def close(self, handle):
        pass


# ---------------------------------------------------------------------------
# the name rule
# ---------------------------------------------------------------------------

CREDENTIAL = (".env", ".env.local", ".env.production", ".env.development.local", ".env.prod.bak",
              ".env.staging", ".env.sample", ".env.template", ".env.", ".env.example.bak",
              ".ENV", ".Env.Production", ".ENV.LOCAL", ".env ", ".env.local.",
              ".env.example.", ".env.\nx")
OPEN = (".env.example", ".env.local.example", ".ENV.EXAMPLE", ".env.Example", ".envrc", "myapp.env",
        "env", ".environment", "dotenv", "x.env.production", ".env-local", "", "example.env.example",
        ".env\nx")


class EnvNameRule(unittest.TestCase):
    def test_names(self):
        for name in CREDENTIAL:
            with self.subTest(name=name):
                self.assertTrue(secrets.is_env_credential_name(name))
                self.assertTrue(secrets.is_env_credential(f"/tmp/nowhere-ed2/{name}"))
        for name in OPEN:
            with self.subTest(name=name):
                self.assertFalse(secrets.is_env_credential_name(name))
                if name:
                    self.assertFalse(secrets.is_env_credential(f"/tmp/nowhere-ed2/{name}"))

    def test_any_depth_and_mnt_c(self):
        for path in ("/mnt/c/Users/owner/OneDrive/Desktop/override/.ENV.Production",
                     "/mnt/c/Users/owner/app/.env", "/mnt/d/x/y/z/.Env.Local", "a/b/c/.env.staging",
                     Path("/mnt/c/Users/owner/.env.development.local")):
            with self.subTest(path=path):
                self.assertTrue(secrets.is_env_credential(path))
        for path in ("/mnt/c/Users/owner/app/.env.example", "/mnt/c/Users/owner/app/.Env.Local.Example",
                     "/mnt/c/Users/owner/.env/config.py", "/home/x/.env/bin/python"):
            with self.subTest(path=path):
                self.assertFalse(secrets.is_env_credential(path))

    def test_symlinks_both_ways(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".env").write_text("A=fake\n")
            (d / "notes.txt").write_text("hello\n")
            (d / ".env.example").symlink_to(d / ".env")                 # named open, reaches .env
            (d / "plain.example").symlink_to(".env")                     # relative target
            (d / "alias").symlink_to(d / "plain.example")                # a chain to .env
            (d / "sub").mkdir()
            (d / "sub" / ".env.local").symlink_to(d / "notes.txt")       # named .env.local, reaches notes
            (d / "hop").symlink_to(d / "sub" / ".env.local")             # a hop named .env.local
            (d / "ok.example").symlink_to(d / "notes.txt")
            (d / ".env.dev.example").symlink_to(d / "notes.txt")
            (d / "dangling.example").symlink_to(d / "missing")
            (d / "loop1").symlink_to(d / "loop2")
            (d / "loop2").symlink_to(d / "loop1")
            for name in (".env.example", "plain.example", "alias", "sub/.env.local", "hop", "loop1"):
                with self.subTest(credential=name):
                    self.assertTrue(secrets.is_env_credential(d / name))
                    self.assertTrue(secrets.is_env_credential(str(d / name)))
            for name in ("notes.txt", "ok.example", ".env.dev.example", "dangling.example"):
                with self.subTest(open=name):
                    self.assertFalse(secrets.is_env_credential(d / name))

    def test_not_a_path(self):
        for value in (None, 3, object()):
            with self.subTest(value=value):
                self.assertFalse(secrets.is_env_credential(value))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# a temp world: HOME, the gate's state, a repo, a daemon
# ---------------------------------------------------------------------------

class World(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = home = self.root / "home"
        (home / ".config" / "jarvis").mkdir(parents=True)
        env = patch.dict(os.environ, {"HOME": str(home), "XDG_DATA_HOME": str(self.root / "xdg"),
                                      "JARVIS_CLAUDE_CREDENTIALS": str(home / ".claude" / ".credentials.json")})
        env.start()
        self.addCleanup(env.stop)
        for key in ("CODEX_HOME", "XDG_CONFIG_HOME"):
            if key in os.environ:
                drop = patch.dict(os.environ)
                drop.start()
                self.addCleanup(drop.stop)
                os.environ.pop(key)
        cj = home / ".config" / "jarvis"
        self.repo = self.root / "repo"
        (self.repo / "jarvis" / "tools").mkdir(parents=True)
        (self.repo / "hud" / "dist").mkdir(parents=True)
        for name, value in dict(V2_DATA_DIR=self.root / "data", ALLOWLIST_PATH=cj / "allowlist.json",
                                MODELS_PATH=cj / "models.json", PROVIDER_DEFAULTS_PATH=cj / "provider_defaults.json",
                                DISCORD_GUILD_PATH=cj / "discord_guild.json", ROUTING_PATH=cj / "routing.json",
                                CODEX_CATALOG_PATH=cj / "codex-models.json", REPO_ROOT=self.repo,
                                HUD_DIST=self.repo / "hud" / "dist", V2_ALWAYS_ASK=self.root / "always.json",
                                DISCORD_TOKEN_PATH=cj / "discord_token.json").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        # The world: ordinary files, credentials of every kind, the gate's state.
        (home / "notes.txt").write_text("hello\n")
        (home / "crlf.txt").write_bytes(b"a\r\nb\r\n")
        (home / "mixed.txt").write_bytes(b"a\r\nb\nc\n")
        (home / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
        (home / "nul.bin").write_bytes(b"ab\x00cd")
        (home / "bom.txt").write_bytes(b"\xef\xbb\xbfhi\n")
        (home / ".ssh").mkdir()
        (home / ".ssh" / "id_test").write_text("-----BEGIN FAKE KEY-----\n")
        (home / ".ssh" / ".env").write_text("X=in-a-credential-folder\n")
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text('{"token": "fake"}')
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text('{"token": "fake"}')
        (home / ".git-credentials").write_text("https://u:fake@example.invalid\n")
        (home / ".gitconfig").write_text("[user]\n\tname = x\n")
        (home / "google_token.json").write_text('{"refresh_token": "fake"}')
        (cj / "allowlist.json").write_text("[]\n")
        (cj / "discord_token.json").write_text('{"bot_token": "fake"}')
        (home / "app").mkdir()
        (home / "app" / ".env.production").write_text(f"API_KEY={FAKE_SECRET}\n")
        (home / "app" / ".env.example").write_text("API_KEY=\n")
        (home / "app" / "main.py").write_text("print('hi')\n")
        (self.repo / "jarvis" / "tools" / "secrets.py").write_text("# guard\n")
        (self.repo / "hud" / "dist" / "index.html").write_text("<title>x</title>")

    def classify(self, path: Path) -> EP.Verdict:
        raw = path if path.is_absolute() else self.home / path
        verdict = EP.Policy().classify(raw, raw.resolve())
        if verdict.kind in EP.EDITABLE and raw.exists():
            verdict = EP.classify_bytes(verdict, raw.read_bytes())
        return verdict


class Kinds(World):
    def test_each_kind(self):
        cj = self.home / ".config" / "jarvis"
        want = {
            self.home / "notes.txt": EP.TEXT,
            self.home / ".ssh" / "id_test": EP.WITHHELD,
            self.home / ".ssh" / ".env": EP.WITHHELD,
            self.home / ".claude" / ".credentials.json": EP.WITHHELD,
            self.home / ".codex" / "auth.json": EP.WITHHELD,
            self.home / ".git-credentials": EP.WITHHELD,
            self.home / "google_token.json": EP.WITHHELD,
            cj / "discord_token.json": EP.WITHHELD,
            cj / "allowlist.json": EP.GATE,
            cj / "models.json": EP.GATE,          # not there yet: still the gate's
            self.home / ".gitconfig": EP.SAFETY,
            self.repo / "jarvis" / "tools" / "secrets.py": EP.SAFETY,
            self.repo / "hud" / "dist" / "index.html": EP.SAFETY,
            self.repo / "hud" / "dist" / ".env": EP.SAFETY,
            self.home / "app" / ".env.production": EP.ENV,
            self.home / "app" / ".env": EP.ENV,   # not there yet
            self.home / "app" / ".env.example": EP.TEXT,
            self.home / "latin1.txt": EP.BINARY,
            self.home / "nul.bin": EP.BINARY,
            self.home / "bom.txt": EP.TEXT,
        }
        for path, kind in want.items():
            with self.subTest(path=str(path.relative_to(self.root))):
                self.assertEqual(self.classify(path).kind, kind)

    def test_a_symlink_is_judged_where_it_goes(self):
        (self.home / "link-to-key").symlink_to(self.home / ".ssh" / "id_test")
        (self.home / "link-to-allow").symlink_to(self.home / ".config" / "jarvis" / "allowlist.json")
        (self.home / "app" / "env.example").symlink_to(self.home / "app" / ".env.production")
        self.assertEqual(self.classify(self.home / "link-to-key").kind, EP.WITHHELD)
        self.assertEqual(self.classify(self.home / "link-to-allow").kind, EP.GATE)
        self.assertEqual(self.classify(self.home / "app" / "env.example").kind, EP.ENV)

    def test_reasons_and_editability(self):
        self.assertTrue(EP.Verdict(EP.TEXT).editable and EP.Verdict(EP.ENV).editable)
        for kind in (EP.GATE, EP.WITHHELD, EP.SAFETY, EP.BINARY):
            self.assertFalse(EP.Verdict(kind).editable)
        self.assertIn("Jarvis can't read", EP.REASONS[EP.ENV])
        self.assertIsNone(self.classify(self.home / "notes.txt").reason)

    def test_etag_eol_text(self):
        self.assertEqual(EP.etag(b"hello\n"), tag(b"hello\n"))
        self.assertEqual(len(EP.etag(b"")), 32)
        self.assertEqual([EP.eol_of(t) for t in ("a\nb", "a\r\nb", "a\rb", "a\r\nb\n", "ab")],
                         ["LF", "CRLF", "CR", "mixed", None])
        self.assertTrue(EP.is_text("é".encode()))
        self.assertFalse(EP.is_text(b"\xe9"))
        self.assertFalse(EP.is_text(b"a\x00b"))
        self.assertTrue(EP.is_text(b"a" * EP.NUL_PROBE + b"\x00"))  # past the probe: UTF-8 still


class Daemonised(World):
    def setUp(self):
        super().setUp()
        self.daemon = Daemon(Stores(), {name: Fake(name) for name in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        ports = {self.daemon.port, self.daemon.face_port, self.daemon.workshop_port}
        self.assertFalse(ports & LIVE_PORTS, "a test daemon must never hold a live port")
        self.face = self.daemon.face_port
        self.project = self.daemon.stores.projects.create("Home", str(self.home))
        self.url = f"/projects/{self.project.id}"
        self.events = self.daemon.bus.subscribe()
        H._ETAGS.clear()

    def raw(self, method, path, headers=None, *, port=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.face, timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else None
            hdrs = dict(headers or {})
            if data is not None:
                hdrs["Content-Type"] = "application/json"
            conn.request(method, path, data, hdrs)
            response = conn.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
        finally:
            conn.close()

    def owner(self, method, path, body=None, status=200):
        got, payload = self.raw(method, path, {"Origin": f"http://127.0.0.1:{self.face}"}, body=body)
        self.assertEqual(got, status, str(payload)[:300])
        return payload

    def read(self, name, status=200):
        return self.owner("GET", f"{self.url}/file?" + urlencode({"path": name}), status=status)

    def save(self, name, content, status=200, **pre):
        return self.owner("PUT", f"{self.url}/file", dict(path=name, content=content, **pre), status=status)

    def stat(self, paths, status=200):
        return self.owner("POST", f"{self.url}/files/stat", {"paths": paths}, status=status)

    def bus_text(self) -> str:
        out = []
        while True:
            try:
                out.append(json.dumps(self.events.get_nowait()))
            except queue.Empty:
                return "\n".join(out)


class Precondition(Daemonised):
    def test_read_says_etag_abs_and_what_the_editor_may_do(self):
        got = self.read("notes.txt")
        self.assertEqual(got["etag"], tag(b"hello\n"))
        self.assertEqual(got["abs"], str((self.home / "notes.txt").resolve()))
        self.assertEqual((got["kind"], got["readonly"], got["encoding"], got["eol"], got["content"]),
                         ("text", None, "utf-8", "LF", "hello\n"))
        self.assertEqual(self.read("crlf.txt")["eol"], "CRLF")
        self.assertEqual(self.read("mixed.txt")["eol"], "mixed")
        self.assertEqual(self.read("bom.txt")["encoding"], "utf-8-bom")
        self.assertEqual(self.read("bom.txt")["content"], "\ufeffhi\n")

    def test_a_mismatched_etag_is_409_and_the_file_is_untouched(self):
        path = self.home / "notes.txt"
        first = self.read("notes.txt")["etag"]
        path.write_text("an agent wrote this\n")
        got = self.save("notes.txt", "mine", expected_etag=first, status=409)
        self.assertEqual(got["etag"], tag(b"an agent wrote this\n"))
        self.assertEqual(path.read_bytes(), b"an agent wrote this\n")
        # Keep mine: against the etag the conflict showed.
        saved = self.save("notes.txt", "mine\n", expected_etag=got["etag"])
        self.assertEqual(saved["etag"], tag(b"mine\n"))
        self.assertEqual(path.read_bytes(), b"mine\n")

    def test_a_rewrite_that_restores_the_mtime_is_still_a_conflict(self):
        path = self.home / "notes.txt"
        seen = self.read("notes.txt")
        st = path.stat()
        path.write_text("HELLO\n")  # same size
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(path.stat().st_mtime_ns, seen["mtime_ns"], "setup: the mtime is back")
        got = self.save("notes.txt", "clobber", expected_etag=seen["etag"], status=409)
        self.assertEqual(got["etag"], tag(b"HELLO\n"))
        self.assertEqual(path.read_text(), "HELLO\n")
        # (The mtime guard is fooled, which is why it is going.)
        self.save("notes.txt", "clobbered", expected_mtime=seen["mtime"])
        self.assertEqual(path.read_text(), "clobbered")

    def test_a_touch_alone_is_not_a_conflict(self):
        path = self.home / "notes.txt"
        seen = self.read("notes.txt")
        os.utime(path, (time.time() + 50, time.time() + 50))
        self.save("notes.txt", "fine\n", expected_etag=seen["etag"])
        self.assertEqual(path.read_text(), "fine\n")

    def test_null_creates_and_is_refused_over_a_file(self):
        got = self.save("app/new.txt", "fresh\n", expected_etag=None)
        self.assertEqual(got["etag"], tag(b"fresh\n"))
        self.assertEqual((self.home / "app" / "new.txt").read_text(), "fresh\n")
        self.assertEqual((self.home / "app" / "new.txt").stat().st_mode & 0o777, 0o644)
        got = self.save("app/new.txt", "again", expected_etag=None, status=409)
        self.assertEqual(got["etag"], tag(b"fresh\n"))
        self.assertEqual((self.home / "app" / "new.txt").read_text(), "fresh\n")
        # A file gone since it was read: 409 with etag null; recreate with null.
        seen = self.read("app/new.txt")
        (self.home / "app" / "new.txt").unlink()
        got = self.save("app/new.txt", "x", expected_etag=seen["etag"], status=409)
        self.assertIsNone(got["etag"])
        self.assertFalse((self.home / "app" / "new.txt").exists())
        self.save("app/new.txt", "back\n", expected_etag=None)
        self.assertEqual((self.home / "app" / "new.txt").read_text(), "back\n")

    def test_the_body_is_exact(self):
        for body in ({"path": "notes.txt", "content": "x"},
                     {"path": "notes.txt", "content": "x", "expected_etag": None, "expected_mtime": None},
                     {"path": "notes.txt", "content": "x", "expected_etag": "ABC"},
                     {"path": "notes.txt", "content": "x", "expected_etag": 7},
                     {"path": "notes.txt", "content": "x", "expected_etag": tag(b"") + "0"},
                     {"path": "notes.txt", "content": 4, "expected_etag": None},
                     {"path": "notes.txt", "content": "x", "expected_etag": None, "force": True}):
            with self.subTest(body=body):
                self.owner("PUT", f"{self.url}/file", body, status=400)
        self.assertEqual((self.home / "notes.txt").read_text(), "hello\n")
        self.owner("PUT", f"{self.url}/file", dict(path="notes.txt", content="x" * (H.FILE_CAP + 1),
                                                    expected_etag=None), status=413)

    def test_modes_are_kept_and_no_temp_is_left(self):
        path = self.home / "notes.txt"
        path.chmod(0o640)
        self.save("notes.txt", "kept\n", expected_etag=self.read("notes.txt")["etag"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.home.glob(".*.tmp")))

    def test_not_text_is_read_only_and_never_overwritten(self):
        for name in ("latin1.txt", "nul.bin"):
            with self.subTest(name=name):
                before = (self.home / name).read_bytes()
                got = self.read(name)
                self.assertEqual(got["kind"], "binary")
                self.assertTrue(got["readonly"])
                self.save(name, "x", expected_etag=got["etag"], status=403)
                self.assertEqual((self.home / name).read_bytes(), before)


class Kinds403(Daemonised):
    def test_withheld_has_no_content_and_cannot_be_written(self):
        for name in (".ssh/id_test", ".claude/.credentials.json", ".codex/auth.json", ".git-credentials",
                     "google_token.json", ".config/jarvis/discord_token.json"):
            with self.subTest(name=name):
                before = (self.home / name).read_bytes()
                got = self.read(name)
                self.assertEqual((got["kind"], got["protected"]), ("withheld", True))
                self.assertNotIn("content", got)
                self.assertNotIn("etag", got)
                self.save(name, "x", expected_etag=tag(before), status=403)
                self.assertEqual((self.home / name).read_bytes(), before)
        rows = self.stat([".ssh/id_test", "notes.txt"])["files"]
        self.assertEqual(rows[".ssh/id_test"], {"kind": "withheld"})

    def test_nothing_else_serves_a_withheld_file(self):
        # The preview origin (no owner check, by design) and an `@path`
        # attachment used to read a key out of the Inbox's `.ssh`.
        status, _ = self.raw("GET", f"/p/{self.project.id}/.ssh/id_test", port=self.daemon.workshop_port)
        self.assertEqual(status, 403)
        message = H.assemble_turn(self.project, dict(text="look at @.ssh/id_test"))
        self.assertNotIn("FAKE KEY", message.text)
        self.assertIn("not attached", message.text)

    def test_the_gate_is_readable_and_read_only(self):
        got = self.read(".config/jarvis/allowlist.json")
        self.assertEqual((got["kind"], got["content"]), ("gate", "[]\n"))
        self.save(".config/jarvis/allowlist.json", '[{"tool": "run_command"}]', expected_etag=got["etag"],
                  status=403)
        self.assertEqual((self.home / ".config" / "jarvis" / "allowlist.json").read_text(), "[]\n")

    def test_the_safety_layer_is_read_only(self):
        repo = self.daemon.stores.projects.create("Repo", str(self.repo))
        url = f"/projects/{repo.id}/file"
        got = self.owner("GET", url + "?" + urlencode({"path": "jarvis/tools/secrets.py"}))
        self.assertEqual(got["kind"], "safety")
        self.owner("PUT", url, dict(path="jarvis/tools/secrets.py", content="x", expected_etag=got["etag"]),
                   status=403)
        self.owner("PUT", url, dict(path="hud/dist/index.html", content="x",
                                    expected_etag=tag(b"<title>x</title>")), status=403)
        self.owner("PUT", url, dict(path="hud/dist/.env", content="X=1", expected_etag=None), status=403)
        self.assertFalse((self.repo / "hud" / "dist" / ".env").exists())

    def test_the_tree_withholds_credential_folders(self):
        rows = {r["name"]: r for r in self.owner("GET", f"{self.url}/tree?depth=3")["entries"]}
        self.assertEqual(rows[".ssh"].get("policy"), "withheld")
        self.assertNotIn(".ssh/id_test", rows)
        self.assertNotIn(".ssh/.env", rows)
        self.assertEqual(rows["app/.env.production"].get("policy"), "env")
        self.assertNotIn("policy", rows["app/.env.example"])
        self.assertEqual(rows["google_token.json"].get("policy"), "withheld")
        # Inside ~/.config/jarvis only the gate's own state is listed.
        self.assertIn(".config/jarvis/allowlist.json", rows)
        self.assertNotIn(".config/jarvis/discord_token.json", rows)
        inside = self.owner("GET", f"{self.url}/tree?path=.config/jarvis&depth=1")["entries"]
        self.assertEqual([r["name"] for r in inside], ["allowlist.json"])
        self.owner("GET", f"{self.url}/tree?path=.ssh&depth=1", status=403)


class Poll(Daemonised):
    def test_cap_gone_and_errors(self):
        self.stat([f"f{i}" for i in range(H.STAT_CAP + 1)], status=413)
        self.stat("notes.txt", status=400)
        rows = self.stat(["notes.txt", "missing.txt", "../outside", "app"])["files"]
        self.assertEqual(rows["notes.txt"]["etag"], tag(b"hello\n"))
        self.assertEqual(rows["notes.txt"]["kind"], "text")
        self.assertEqual(rows["missing.txt"], {"gone": True})
        self.assertEqual(rows["../outside"], {"error": 403})
        self.assertEqual(rows["app"], {"gone": True})

    def test_hashed_again_only_when_the_stat_moves(self):
        calls = []
        real = EP.etag

        def counting(data):
            calls.append(len(data))
            return real(data)
        with patch.object(H.editor_policy, "etag", counting):
            self.stat(["notes.txt"])
            self.stat(["notes.txt"])
            self.assertEqual(len(calls), 1, "a second poll of an unmoved file re-hashed it")
            path = self.home / "notes.txt"
            st = path.stat()
            path.write_text("HELLO\n")
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            rows = self.stat(["notes.txt"])["files"]
            self.assertEqual(rows["notes.txt"]["etag"], tag(b"HELLO\n"),
                             "a same-size rewrite with its mtime restored was not seen")
            os.utime(path, (time.time() + 60, time.time() + 60))
            self.assertEqual(self.stat(["notes.txt"])["files"]["notes.txt"]["etag"], tag(b"HELLO\n"))


class OwnerOnly(Daemonised):
    def routes(self):
        return [("GET", f"{self.url}/tree?depth=1", None),
                ("GET", f"{self.url}/file?path=notes.txt", None),
                ("GET", f"{self.url}/file?path=app%2F.env.production", None),
                ("PUT", f"{self.url}/file", {"path": "notes.txt", "content": "x", "expected_etag": None}),
                ("POST", f"{self.url}/files/stat", {"paths": ["notes.txt"]})]

    def test_the_api_listener_and_a_bare_request_are_refused(self):
        api = self.daemon.port
        for method, path, body in self.routes():
            for port, headers in ((api, {}), (api, {"Origin": f"http://127.0.0.1:{api}"}),
                                  (api, {"Sec-Fetch-Site": "same-origin"}),
                                  (self.face, {}), (self.face, {"Sec-Fetch-Site": "cross-site"}),
                                  (self.face, {"Origin": "null"}),
                                  (self.face, {"Origin": f"http://127.0.0.1:{self.daemon.workshop_port}"})):
                with self.subTest(method=method, path=path, port=port, headers=headers):
                    status, payload = self.raw(method, path, headers, port=port, body=body)
                    self.assertEqual(status, 403, str(payload)[:200])
                    self.assertNotIn(FAKE_SECRET, json.dumps(payload) if isinstance(payload, dict) else str(payload))
        self.assertEqual((self.home / "notes.txt").read_text(), "hello\n")

    def test_a_same_origin_read_is_the_owners(self):
        status, payload = self.raw("GET", f"{self.url}/file?path=notes.txt", {"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((status, payload["content"]), (200, "hello\n"))


class EnvFiles(Daemonised):
    def test_the_owner_opens_and_saves_an_env_file(self):
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old_level)))
        path = self.home / "app" / ".env.production"
        path.chmod(0o640)
        got = self.read("app/.env.production")
        self.assertEqual((got["kind"], got["content"]), ("env", f"API_KEY={FAKE_SECRET}\n"))
        self.assertIn("Jarvis can't read", got["readonly"])
        new = f"API_KEY={FAKE_SECRET}-2\n"
        saved = self.save("app/.env.production", new, expected_etag=got["etag"])
        self.assertEqual(path.read_text(), new)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640, "the file's mode was not kept")
        self.save("app/.env.production", "stale", expected_etag=got["etag"], status=409)
        self.assertEqual(path.read_text(), new)
        self.assertEqual(self.stat(["app/.env.production"])["files"]["app/.env.production"]["etag"],
                         saved["etag"])
        created = self.save("app/.env.staging", f"K={FAKE_SECRET}\n", expected_etag=None)
        self.assertEqual((self.home / "app" / ".env.staging").stat().st_mode & 0o777, 0o600)
        self.assertTrue(created["etag"])
        # .env and .env.local too: the owner's now, no longer withheld.
        (self.home / "app" / ".env").write_text(f"K={FAKE_SECRET}\n")
        self.assertEqual(self.read("app/.env")["kind"], "env")
        time.sleep(0.2)
        self.assertNotIn(FAKE_SECRET, self.bus_text(), "an env file's content reached the bus")
        self.assertNotIn(FAKE_SECRET, logs.getvalue(), "an env file's content reached a log line")
        self.assertFalse(list((self.root / "data").rglob("*.jsonl")) and any(
            FAKE_SECRET in p.read_text(errors="replace") for p in (self.root / "data").rglob("*")
            if p.is_file()), "an env file's content reached the daemon's data folder")

    def test_the_api_listener_never_serves_it(self):
        api = self.daemon.port
        for headers in ({}, {"Origin": f"http://127.0.0.1:{api}"}, {"Sec-Fetch-Site": "same-origin"}):
            with self.subTest(headers=headers):
                status, payload = self.raw("GET", f"{self.url}/file?path=app%2F.env.production", headers, port=api)
                self.assertEqual(status, 403)
                self.assertNotIn(FAKE_SECRET, json.dumps(payload))
        status, _ = self.raw("PUT", f"{self.url}/file", {}, port=api,
                             body={"path": "app/.env.production", "content": "X=1", "expected_etag": None})
        self.assertEqual(status, 403)
        self.assertEqual((self.home / "app" / ".env.production").read_text(), f"API_KEY={FAKE_SECRET}\n")

    def test_nothing_else_carries_it(self):
        # Not as an @path attachment, not as a dropped file, not on the
        # preview origin: only the owner's editor reads it.
        message = H.assemble_turn(self.project, dict(text="look at @app/.env.production and @app/.env.example"))
        self.assertNotIn(FAKE_SECRET, message.text)
        self.assertIn("not attached", message.text)
        import base64
        dropped = H.assemble_turn(self.project, dict(text="x", attachments=[dict(
            name="C:\\Users\\o\\.env.production", mime="text/plain",
            data_b64=base64.b64encode(f"K={FAKE_SECRET}".encode()).decode())]))
        self.assertNotIn(FAKE_SECRET, dropped.text)
        status, payload = self.raw("GET", f"/p/{self.project.id}/app/.env.production",
                                   port=self.daemon.workshop_port)
        self.assertEqual(status, 403)
        self.assertNotIn(FAKE_SECRET.encode(), payload if isinstance(payload, bytes) else json.dumps(payload).encode())


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = b"<!doctype html><title>elsewhere</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "Playwright is not installed: the browser half did not run")
class InChromium(Daemonised):
    """The owner's env file through a real browser's own headers."""

    def setUp(self):
        super().setUp()
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.ctx = self.browser.new_context()
        self.addCleanup(self.ctx.close)
        self.live_hits: list[str] = []

        def refuse(route):
            self.live_hits.append(route.request.url)
            route.abort()
        self.ctx.route(LIVE, refuse)
        self.addCleanup(lambda: self.assertEqual(self.live_hits, [], "a request reached a live port"))
        self.page = self.ctx.new_page()

    def test_the_huds_page_opens_and_saves_it_and_another_origin_cannot(self):
        self.page.goto(f"http://127.0.0.1:{self.face}/status")
        url = f"{self.url}/file"
        got = self.page.evaluate("""async (url) => {
          const r = await fetch(url + '?path=' + encodeURIComponent('app/.env.production'));
          const body = await r.json();
          const w = await fetch(url, {method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: 'app/.env.production', content: body.content + 'B=2\\n',
                                  expected_etag: body.etag})});
          return {status: r.status, kind: body.kind, content: body.content, put: w.status};
        }""", url)
        self.assertEqual((got["status"], got["kind"], got["put"]), (200, "env", 200), str(got))
        self.assertEqual(got["content"], f"API_KEY={FAKE_SECRET}\n")
        self.assertEqual((self.home / "app" / ".env.production").read_text(), f"API_KEY={FAKE_SECRET}\nB=2\n")
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
        other.daemon_threads = True
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.page.goto(f"http://127.0.0.1:{other.server_address[1]}/")
        served = []
        real = H.read_for_editor

        def recording(*a, **kw):
            served.append(a[1])
            return real(*a, **kw)
        with patch.object(H, "read_for_editor", recording):
            out = self.page.evaluate("""async (url) => {
              const out = {};
              try { const r = await fetch(url); out.cors = {status: r.status, body: await r.text()}; }
              catch (e) { out.cors = {error: e.name}; }
              try { const r = await fetch(url, {mode: 'no-cors'});
                    out.opaque = {type: r.type, body: await r.text()}; }
              catch (e) { out.opaque = {error: e.name}; }
              return out;
            }""", f"http://127.0.0.1:{self.face}{url}?path=app%2F.env.production")
        self.assertEqual(out["cors"], {"error": "TypeError"}, str(out))
        self.assertEqual(out["opaque"], {"type": "opaque", "body": ""}, str(out))
        self.assertEqual(served, [], "the daemon read the env file for another origin")


if __name__ == "__main__":
    unittest.main(verbosity=2)

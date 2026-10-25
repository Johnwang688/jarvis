"""The phone's read API and event stream (phone surface design §5, §6, work
package P-3): `jarvis/v2/remote_api.py` behind `remote_auth.api_route` on the
remote listener.

Free and hermetic, on the same harness as tests/v2/remote_auth_check.py: a
real daemon with its remote listener on an ephemeral loopback port (never
8402, 8403, 8405 or 8408), temporary HOME, data and HUD build, a passkey
paired and signed in with the software authenticator against the real
`webauthn` verification. The HUD's own routes are read on the same daemon's
HUD listener as the owner, so "the phone gets the HUD's projection minus the
documented fields" is compared against what the HUD really sends.

What it pins (P-3's acceptance items):

  - every read route needs a session (401 without one, after logout, after a
    revoke), refuses every method but GET (405, and 403 first without the
    phone's Origin), and needs the exact Host (400);
  - each answer equals the HUD projection's minus exactly `DROPPED`;
  - a private approval row carries only its private projection, never
    `owner_json`;
  - the transcript is `chat_reads`' projection, row for row; the log pages
    with `after`, by rows and by bytes, and cuts an over-long row;
  - the diff's file and byte caps, with the "open it at the desk" marker;
  - the stream needs a session at connect, ends on logout, on a revoke and
    on expiry, carries only the allowed kinds and no text, and displaces its
    oldest past the cap;
  - no route leaks a credential planted in a thread log, a title, a brief or
    a question;
  - `projects.is_owner` stays false, and the module never asks it;
  - the agent-path DENY covers the new routes.

Run: PYTHONPATH=. .venv/bin/python tests/v2/remote_api_check.py
"""
from __future__ import annotations

import http.client
import importlib.util
import inspect
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("remote_auth_check", Path(__file__).with_name("remote_auth_check.py"))
rac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rac)

from jarvis import config, rules  # noqa: E402
from jarvis.v2 import chat_reads, projects, remote_api, remote_auth, worktrees  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.model import OpenQuestion, ProviderName as PN, Report, Role, Spec, TaskState  # noqa: E402

HOST, ORIGIN, raw = rac.HOST, rac.ORIGIN, rac.raw
# A GitHub-shaped token: the scrub's patterns withhold it. Planted wherever
# a model's words reach a route; it must never come back.
TOKEN = "ghp_" + "Zq7" * 12
AGENT = "SENTINEL-private-detail-5c1e"
# Credential-shaped samples, assembled at run time from pieces (the repo's
# convention, tests/v2/inline_credentials_check.py): no literal a secret
# scanner matches is committed, and the scrub still faces the same shapes.
# All are fake or public documentation samples.
PEM_BODY = "MII" + "EowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"
_KEY_HEAD, _KEY_FOOT = "-----BEGIN " + "RSA PRIVATE KEY-----", "-----END " + "RSA PRIVATE KEY-----"
PEM = _KEY_HEAD + "\n" + PEM_BODY + "\n" + _KEY_FOOT + "\n"
SAMPLES = {
    "AWS_SECRET": "wJa" + "lrXUtnFEMI/K7MDENG/bPxRfi" + "CYEXAMPLEKEY",
    "AWS_SESSION": "IQo" + "Jb3JpZ2luX2VjEN7//////////wEaCXVzLWVhc3QtMSJH",
    "HEX_TOKEN": "abcdef01" + "23456789" + "abcdef01" + "23456789",
    "HEX_KEY": "9f8e7d6c" + "5b4a3928" + "1706f5e4" + "d3c2b1a0",
    "HEX_SECRET": "abcdef01" + "23456789" + "abcdef",
    "GOOGLE_SECRET": "GOC" + "SPX-" + "abcDEF123" + "ghiJKL456",
    "PW_LONG": "hunt" + "er2" + "hunt" + "er2" + "xyzzy",
    "PW_TROUB": "Tr0u" + "b4dor&3",
    "PW_DASH": "s3cr" + "3t-pass",
    "PW_MYSQL": "S3cr" + "etPass",
    "PW_BANG": "Hunt" + "er2!",
    "PW_PG": "S3cr" + "et",
    "PW_DOT": "s3cr" + "3t",
    "PW_SHORT": "hunt" + "er2",
    "HEX_KEY_HEAD": "9f8e7d6c" + "5b4a3928",
    "AWS_SECRET_HEAD": "wJa" + "lrXUtnFEMI",
    "AWS_SESSION_HEAD": "IQo" + "Jb3JpZ2lu",
    "PW_HORSE": "correct" + "horse" + "battery" + "staple",
    "PW_WORDS": " ".join(("correct", "horse", "battery", "staple")),
    "PW_SUMMER": "Sum" + "mer",
    "PW_LETMEIN": "let" + "me" + "in",
    # The labels too, beside a sample value: a scanner matches the label and
    # the assignment, whatever the value is.
    "K_PGPASSWORD": "PG" + "PASS" + "WORD",
    "K_DBPASSWORD": "DB_" + "PASS" + "WORD",
    "K_PASSPHRASE": "pass" + "phrase",
    "K_PASSWORD": "pass" + "word",
    "K_PWD": "p" + "wd",
}


def _fill(text: str) -> str:
    """A line with each @NAME@ marker replaced by its sample."""
    for name, value in SAMPLES.items():
        text = text.replace("@" + name + "@", value)
    return text

# The review's line sets (PR #308, labelled_probe.py and fp_compare.py), with
# main's own `chat_reads.scrub` and `redact.scrub_text` output for each (taken
# on origin/main aad6e566): the shared paths must stay exactly that.
LABELLED_KEEP = [
    'def login(username: str, password: str) -> bool:',
    'api_key: Optional[str] = None',
    'api_key: str | None = None',
    'password: SecretStr',
    'client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])',
    'client = Client(api_key=settings.API_KEY)',
    "api_key=os.getenv('API_KEY')",
    "password=request.form['password']",
    'password = self.password',
    'access_token = response.json()["access_token"]',
    'client_secret=CLIENT_SECRET,',
    'const apiKey = process.env.API_KEY;',
    'api_key: process.env.STRIPE_KEY,',
    'password: ${DB_PASSWORD}',
    'password: "{{ vault_db_password }}"',
    'password: <your-password>',
    'password: ********',
    "| password | string | the user's password |",
    'Set `password:` to your database password.',
    'password: required, at least 12 characters',
    'api_key: see the dashboard',
    'private_key: ~/.ssh/id_ed25519',
    'auth_token: !vault |',
    'refresh_token=refresh_token,',
    'password=hashed_password',
    'password_hash = bcrypt.hashpw(password.encode(), salt)',
    'if password == confirm_password:',
    "secret_key: env('DJANGO_SECRET_KEY')",
    'api-key: x-api-key header',
    'password: str = Field(..., min_length=8)',
    'self.access_token = access_token',
    'passwd: /etc/passwd entries',
    'password=getpass.getpass()',
]
# fp_compare.py's code lines the first labelled rule altered: every one kept.
LABELLED_CODE = [
    'api_key: Optional[str] = None',
    'client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])',
    'client = Client(api_key=settings.API_KEY)',
    "api_key=os.getenv('API_KEY')",
    "password=request.form['password']",
    'password = self.password',
    'access_token = response.json()["access_token"]',
    'client_secret=CLIENT_SECRET,',
    'const apiKey = process.env.API_KEY;',
    'api_key: process.env.STRIPE_KEY,',
    'password: ********',
    'private_key: ~/.ssh/id_ed25519',
    'auth_token: !vault |',
    'refresh_token=refresh_token,',
    'password=hashed_password',
    'api-key: x-api-key header',
    'self.access_token = access_token',
    'passwd: /etc/passwd entries',
    'password=getpass.getpass()',
]
_LABELLED_PROBE_WITHHOLD = [
    'aws_secret_access_key = @AWS_SECRET@',
    '@K_PASSWORD@: @PW_SHORT@',
    '@K_PASSWORD@: @PW_LONG@',
    '@K_PASSWORD@=@PW_TROUB@',
    '"@K_PASSWORD@": "@PW_DASH@"',
    '@K_DBPASSWORD@: @PW_HORSE@',
    'api_key: @HEX_KEY@',
    'client_secret: @GOOGLE_SECRET@',
    '@K_PASSPHRASE@: @PW_WORDS@',
    '@K_PASSWORD@: @PW_SUMMER@',
    '@K_PASSWORD@: @PW_LETMEIN@',
    'secret: @HEX_SECRET@',
    'token: @HEX_TOKEN@',
    '@K_PWD@=@PW_BANG@',
    'mysql -u root -p@PW_MYSQL@ db',
    '@K_PGPASSWORD@=@PW_PG@ psql',
    '@K_PASSWORD@ - @PW_SHORT@',
]
_MAIN_SCRUB = {  # line: (chat_reads.scrub, redact.scrub_text) on main
    'def login(username: str, password: str) -> bool:': ('def login(username: str, password: str) -> bool:', 'def login(username: str, password: str) -> bool:'),
    'api_key: Optional[str] = None': ('api_key: Optional[str] = None', 'api_key: Optional[str] = None'),
    'api_key: str | None = None': ('api_key: str | None = None', 'api_key: str | None = None'),
    'password: SecretStr': ('password: SecretStr', 'password: SecretStr'),
    'client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])': ('client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])', 'client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])'),
    'client = Client(api_key=settings.API_KEY)': ('client = Client(api_key=[redacted])', 'client = Client(api_key=settings.API_KEY)'),
    "api_key=os.getenv('API_KEY')": ("api_key=[redacted]('API_KEY')", "api_key=os.getenv('API_KEY')"),
    "password=request.form['password']": ("password=request.form['password']", "password=request.form['password']"),
    'password = self.password': ('password = self.password', 'password = self.password'),
    'access_token = response.json()["access_token"]': ('access_token = response.json()["access_token"]', 'access_token = response.json()["access_token"]'),
    'client_secret=CLIENT_SECRET,': ('client_secret=[redacted]', 'client_secret=CLIENT_SECRET,'),
    'const apiKey = process.env.API_KEY;': ('const apiKey = process.env.API_KEY;', 'const apiKey = process.env.API_KEY;'),
    'api_key: process.env.STRIPE_KEY,': ('api_key: process.env.STRIPE_KEY,', 'api_key: process.env.STRIPE_KEY,'),
    'password: ${DB_PASSWORD}': ('password: ${DB_PASSWORD}', 'password: ${DB_PASSWORD}'),
    'password: "{{ vault_db_password }}"': ('password: "{{ vault_db_password }}"', 'password: "{{ vault_db_password }}"'),
    'password: <your-password>': ('password: <your-password>', 'password: <your-password>'),
    'password: ********': ('password: ********', 'password: ********'),
    "| password | string | the user's password |": ("| password | string | the user's password |", "| password | string | the user's password |"),
    'Set `password:` to your database password.': ('Set `password:` to your database password.', 'Set `password:` to your database password.'),
    'password: required, at least 12 characters': ('password: required, at least 12 characters', 'password: required, at least 12 characters'),
    'api_key: see the dashboard': ('api_key: see the dashboard', 'api_key: see the dashboard'),
    'private_key: ~/.ssh/id_ed25519': ('private_key: ~/.ssh/id_ed25519', 'private_key: ~/.ssh/id_ed25519'),
    'auth_token: !vault |': ('auth_token: !vault |', 'auth_token: !vault |'),
    'refresh_token=refresh_token,': ('refresh_token=[redacted]', 'refresh_token=refresh_token,'),
    'password=hashed_password': ('password=[redacted]', 'password=hashed_password'),
    'password_hash = bcrypt.hashpw(password.encode(), salt)': ('password_hash = bcrypt.hashpw(password.encode(), salt)', 'password_hash = bcrypt.hashpw(password.encode(), salt)'),
    'if password == confirm_password:': ('if password == confirm_password:', 'if password == confirm_password:'),
    "secret_key: env('DJANGO_SECRET_KEY')": ("secret_key: env('DJANGO_SECRET_KEY')", "secret_key: env('DJANGO_SECRET_KEY')"),
    'api-key: x-api-key header': ('api-key: x-api-key header', 'api-key: x-api-key header'),
    'password: str = Field(..., min_length=8)': ('password: str = Field(..., min_length=8)', 'password: str = Field(..., min_length=8)'),
    'self.access_token = access_token': ('self.access_token = access_token', 'self.access_token = access_token'),
    'passwd: /etc/passwd entries': ('passwd: /etc/passwd entries', 'passwd: /etc/passwd entries'),
    'password=getpass.getpass()': ('password=[redacted]()', 'password=getpass.getpass()'),
    'aws_secret_access_key = @AWS_SECRET@': ('aws_secret_access_key = @AWS_SECRET@', 'aws_secret_access_key = @AWS_SECRET@'),
    '@K_PASSWORD@: @PW_SHORT@': ('@K_PASSWORD@: @PW_SHORT@', '@K_PASSWORD@: @PW_SHORT@'),
    '@K_PASSWORD@: @PW_LONG@': ('@K_PASSWORD@: @PW_LONG@', '@K_PASSWORD@: @PW_LONG@'),
    '@K_PASSWORD@=@PW_TROUB@': ('@K_PASSWORD@=[redacted]&3', '@K_PASSWORD@=@PW_TROUB@'),
    '"@K_PASSWORD@": "@PW_DASH@"': ('"@K_PASSWORD@": "[redacted]"', '"@K_PASSWORD@": "@PW_DASH@"'),
    '@K_DBPASSWORD@: @PW_HORSE@': ('@K_DBPASSWORD@: @PW_HORSE@', '@K_DBPASSWORD@: @PW_HORSE@'),
    'api_key: @HEX_KEY@': ('api_key: @HEX_KEY@', 'api_key: @HEX_KEY@'),
    'client_secret: @GOOGLE_SECRET@': ('client_secret: @GOOGLE_SECRET@', 'client_secret: @GOOGLE_SECRET@'),
    '@K_PASSPHRASE@: @PW_WORDS@': ('@K_PASSPHRASE@: @PW_WORDS@', '@K_PASSPHRASE@: @PW_WORDS@'),
    '@K_PASSWORD@: @PW_SUMMER@': ('@K_PASSWORD@: @PW_SUMMER@', '@K_PASSWORD@: @PW_SUMMER@'),
    '@K_PASSWORD@: @PW_LETMEIN@': ('@K_PASSWORD@: @PW_LETMEIN@', '@K_PASSWORD@: @PW_LETMEIN@'),
    'secret: @HEX_SECRET@': ('secret: @HEX_SECRET@', 'secret: @HEX_SECRET@'),
    'token: @HEX_TOKEN@': ('token: @HEX_TOKEN@', 'token: @HEX_TOKEN@'),
    '@K_PWD@=@PW_BANG@': ('@K_PWD@=[redacted]', '@K_PWD@=@PW_BANG@'),
    'mysql -u root -p@PW_MYSQL@ db': ('mysql -u root -p[redacted] db', 'mysql -u root -p@PW_MYSQL@ db'),
    '@K_PGPASSWORD@=@PW_PG@ psql': ('@K_PGPASSWORD@=[redacted] psql', '@K_PGPASSWORD@=@PW_PG@ psql'),
    '@K_PASSWORD@ - @PW_SHORT@': ('@K_PASSWORD@ - @PW_SHORT@', '@K_PASSWORD@ - @PW_SHORT@'),
}
LABELLED_PROBE_WITHHOLD = [_fill(t) for t in _LABELLED_PROBE_WITHHOLD]
MAIN_SCRUB = {_fill(k): (_fill(a), _fill(b)) for k, (a, b) in _MAIN_SCRUB.items()}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def approval(req_id, *, private=False, thread_id=None):
    r = ApprovalRequest(tool="Bash", args={"command": "ls -la"}, command="ls -la", reason="list",
                        provider="claude", thread_id=thread_id, origin='task 0000beef "build"',
                        hud_only=private, discord_channel_id="123456789012345678",
                        browser={"url": "https://" + AGENT, "secret": AGENT} if private else None)
    if private:
        r.args, r.command, r.reason = {"url": AGENT}, AGENT, AGENT
    r.req_id, r.code, r.asked_at, r.timeout_s = req_id, "abcd", "2026-10-25T10:00:00+00:00", 600.0
    return r


class Api(rac.Remote):
    """A signed-in phone, a project with chats, tasks and a task worktree."""

    def setUp(self):
        super().setUp()
        repo = self.root / "project"
        repo.mkdir()
        (repo / "app.py").write_text("def f(n):\n    return n\n")
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
        self.repo = repo
        stores = self.d.stores
        self.stores = stores
        self.project = stores.projects.create("Calc", str(repo), extra_dirs=[str(self.root)])
        self.other = stores.projects.create("Archived one", str(repo))
        self.chat = self.thread("Fix the parser " + TOKEN)
        self.second = self.thread("Second chat")
        self.append(self.chat, {"kind": "user", "data": {"typed": "hello, my key is " + TOKEN,
                                                         "text": "INLINED-FILE " + TOKEN, "via": "hud",
                                                         "message_id": "m1"}})
        self.append(self.chat, {"kind": "tool_started", "data": {"name": "Bash", "args": {"command": "cat .env"}}})
        self.append(self.chat, {"kind": "tool_finished", "data": {"name": "Bash", "summary": "TERMINAL-OUTPUT"}})
        self.append(self.chat, {"kind": "text", "data": {"text": "**Done.** Here it is: " + TOKEN}})
        self.append(self.chat, {"kind": "notice", "data": {"text": "a system line"}})
        task = stores.tasks.create(self.project.id, "Build the thing " + TOKEN)
        self.task = worktrees.ensure(task, self.project, stores)
        live = stores.tasks.get(self.task.id)
        live.plan = ["step one", "step two " + TOKEN]
        live.report = Report(done=["did a thing"], next="ship it")
        live.spec = Spec(goal="a goal", questions=[OpenQuestion("which db? " + TOKEN, blocking=True,
                                                                options=["pg", "sqlite"])])
        stores.tasks.save(live)
        stores.tasks.transition(self.task.id, TaskState.CLARIFYING)
        self.task = stores.tasks.get(self.task.id)
        hidden = stores.tasks.create(self.other.id, "hidden task")
        stores.tasks.save(hidden)
        self.hidden_task = hidden
        self.hidden_chat = self.thread("hidden chat", project=self.other)
        self.other.archived = "2026-10-25T09:00:00+00:00"
        stores.projects.save(self.other)
        self.a = self.pair()
        self.cookie = self.signed_in(self.a)

    def thread(self, title, *, project=None):
        t = self.stores.threads.create((project or self.project).id, Role.CHAT, PN.CLAUDE, cwd=str(self.repo))
        # Each one newer than the last, by its own stamp (never two in the same second).
        self._made = getattr(self, "_made", 0) + 1
        t.updated = f"2026-10-25T0{self._made}:00:00+00:00"
        self.stores.threads.save(t)
        self.stores.threads.set_title(t.id, title, "owner")
        return self.stores.threads.get(t.id)

    def append(self, thread, record):
        record = {"at": "2026-10-25T10:00:00+00:00", "thread_id": thread.id, **record}
        self.stores.threads._append(thread.id, "log.jsonl", record)

    def get(self, path, *, cookie=True, host=HOST):
        return self.phone("GET", path, cookie=self.cookie if cookie is True else cookie, host=host)

    def ok(self, path):
        status, _, body = self.get(path)
        self.assertEqual(status, 200, (path, body))
        return body

    def owner(self, path):
        status, body = self.hud("GET", path)
        self.assertEqual(status, 200, (path, body))
        return body

    def reads(self):
        t, k = self.chat.id, self.task.id
        return ["/m/api/inbox", "/m/api/projects", "/m/api/threads", f"/m/api/threads?project={self.project.id}",
                f"/m/api/threads/{t}/log", f"/m/api/threads/{t}/log?after=1", "/m/api/tasks",
                f"/m/api/tasks/{k}", f"/m/api/tasks/{k}/diff"]


# -- the session, the method and the Host -------------------------------------------


class Gate(Api):
    def test_every_route_needs_a_session(self):
        for path in self.reads() + ["/m/api/events"]:
            for cookie in (None, "__Host-jarvis_m=" + "A" * 43, "jarvis_m=" + self.cookie.split("=", 1)[1]):
                status, headers, body = self.get(path, cookie=cookie)
                self.assertEqual((status, body), (401, {"error": "unauthorized"}), (path, cookie))
                self.assertNotIn("Set-Cookie", headers)
        for path in self.reads():
            self.assertEqual(self.get(path)[0], 200, path)
        # Logged out, then a fresh session revoked at the desk: 401 again.
        status, _, _ = self.phone("POST", "/m/api/logout", {}, cookie=self.cookie)
        self.assertEqual(status, 200)
        for path in self.reads():
            self.assertEqual(self.get(path)[0], 401, ("after logout", path))
        self.cookie = self.signed_in(self.a)
        self.assertEqual(self.get("/m/api/inbox")[0], 200)
        key = self.auth.status()["passkeys"][0]["key"]
        self.assertTrue(self.auth.revoke(key))
        for path in self.reads():
            self.assertEqual(self.get(path)[0], 401, ("after revoke", path))
        print(f"ok  {len(self.reads()) + 1} read routes: 401 without a session, after logout and after a revoke")

    def test_only_get(self):
        for path in self.reads() + ["/m/api/events"]:
            for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                status, headers, body = self.phone(method, path, {}, cookie=self.cookie)
                self.assertEqual((status, body), (405, {"error": "method not allowed"}), (method, path))
                self.assertEqual(headers["Allow"], "GET")
                # Without the phone's Origin the listener refuses first (403).
                status, _, _ = self.phone(method, path, {}, cookie=self.cookie, origin=None)
                self.assertEqual(status, 403, (method, path))
            status, _, _ = self.phone("HEAD", path, cookie=self.cookie)
            self.assertEqual(status, 405, ("HEAD", path))
        for path in ("/m/api/inbox/x", "/m/api/inbox/appr:x/answer", "/m/api/threads/x", "/m/api/tasks/x/y",
                     "/m/api/projects/" + self.project.id, "/m/api/ws", "/m/api/", "/m/api/status",
                     f"/m/api/tasks/{self.task.id}/diff/file", f"/m/api/tasks/{self.task.id}/journal",
                     f"/m/api/threads/{self.chat.id}/transcript", "/m/api/approvals"):
            self.assertEqual(self.get(path)[0], 404, path)
            self.assertEqual(self.get(path, cookie=None)[0], 401, path)
        print("ok  reads are GET only (405, 403 first without Origin); anything else 404 signed in, 401 not")

    def test_exact_host(self):
        for path in self.reads():
            for host in ("localhost", HOST + ":443", "127.0.0.1", HOST.upper(), None):
                status, _, body = self.get(path, host=host)
                self.assertEqual(status, 400, (path, host, body))
        print("ok  reads need exactly the configured Host (400)")

    def test_bad_ids_and_queries(self):
        for path in ("/m/api/threads/zzzzzzzz/log", "/m/api/threads/../log", "/m/api/tasks/0000000g",
                     "/m/api/tasks/00000000", "/m/api/threads/00000000/log",
                     f"/m/api/threads/{self.hidden_chat.id}/log", f"/m/api/tasks/{self.hidden_task.id}",
                     f"/m/api/tasks/{self.hidden_task.id}/diff", f"/m/api/threads?project={self.other.id}",
                     "/m/api/threads?project=nope"):
            self.assertEqual(self.get(path)[0], 404, path)
        t = self.chat.id
        for path in (f"/m/api/threads/{t}/log?after=-1", f"/m/api/threads/{t}/log?after=x",
                     f"/m/api/threads/{t}/log?after=1&after=2", f"/m/api/threads/{t}/log?before=1",
                     "/m/api/inbox?x=1", f"/m/api/threads/{t}/log?after=1234567890",
                     "/m/api/threads?project=a&project=b"):
            self.assertEqual(self.get(path)[0], 400, path)
        print("ok  unknown, malformed and archived ids 404; unknown or repeated query fields 400")


# -- the projections -------------------------------------------------------------------


class Projections(Api):
    def test_projects_equal_the_huds_minus_dropped(self):
        hud = self.owner("/projects")
        phone = self.ok("/m/api/projects")
        self.assertFalse(phone["more"])
        self.assertEqual([p["id"] for p in phone["projects"]], [p["id"] for p in hud])
        self.assertNotIn(self.other.id, [p["id"] for p in phone["projects"]])
        for mine, theirs in zip(phone["projects"], hud):
            self.assertEqual(set(theirs) - set(mine), set(remote_api.DROPPED["project"]))
            self.assertEqual(mine, {k: v for k, v in theirs.items() if k in mine})
        self.assertNotIn(str(self.repo), json.dumps(phone))
        print("ok  projects: the HUD's minus", sorted(remote_api.DROPPED["project"]))

    def test_threads_equal_the_huds_minus_dropped(self):
        hud = {t["id"]: t for t in self.owner(f"/threads?project={self.project.id}")}
        phone = self.ok(f"/m/api/threads?project={self.project.id}")
        self.assertEqual({t["id"] for t in phone["threads"]}, set(hud))
        self.assertEqual([t["id"] for t in phone["threads"]][:1], [self.second.id])   # newest first
        for mine in phone["threads"]:
            theirs = hud[mine["id"]]
            self.assertLessEqual(set(theirs) - set(mine), set(remote_api.DROPPED["thread"]),
                                 "a field the HUD sends is neither kept nor documented as dropped")
            for key, value in mine.items():
                if key == "title":
                    self.assertEqual(value, chat_reads.scrub(theirs["title"]))
                else:
                    self.assertEqual(value, theirs[key], key)
        everything = self.ok("/m/api/threads")
        self.assertNotIn(self.hidden_chat.id, {t["id"] for t in everything["threads"]})
        text = json.dumps(phone)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(str(self.repo), text)           # no cwd
        print("ok  threads: the HUD's minus", sorted(remote_api.DROPPED["thread"]), "; titles scrubbed")

    def test_tasks_equal_the_huds_minus_dropped(self):
        hud = {t["id"]: t for t in self.owner("/tasks")}
        phone = self.ok("/m/api/tasks")
        self.assertEqual({t["id"] for t in phone["tasks"]}, {self.task.id})   # the archived project's task hidden
        mine, theirs = phone["tasks"][0], hud[self.task.id]
        self.assertEqual(set(theirs) - set(mine),
                         set(remote_api.DROPPED["task"]) | set(remote_api.DROPPED["task_list"]))
        for key, value in mine.items():
            want = theirs[key]
            if key == "status":
                want = {k: v for k, v in want.items() if k not in remote_api.DROPPED["task_status"]}
            self.assertEqual(value, chat_reads.scrub(want) if isinstance(want, str) else want, key)
        one = self.ok(f"/m/api/tasks/{self.task.id}")["task"]
        theirs = self.owner(f"/tasks/{self.task.id}")
        self.assertEqual(set(theirs) - set(one), set(remote_api.DROPPED["task"]))
        self.assertEqual(one["plan"], ["step one", chat_reads.scrub("step two " + TOKEN)])
        self.assertEqual(one["report"]["done"], ["did a thing"])
        self.assertEqual(one["state"], "clarifying")
        for key in ("id", "project_id", "state", "created", "updated", "branch", "thread_ids", "ceilings"):
            self.assertEqual(one[key], theirs[key], key)
        text = json.dumps([phone, one])
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(str(self.repo), text)           # no worktree, no root
        print("ok  tasks: the list and the task are the HUD's minus the documented fields, text scrubbed")

    def test_inbox_equals_the_huds_minus_dropped(self):
        public, private = approval("req-public", thread_id=self.chat.id), approval("req-private", private=True)
        with patch.object(self.d.approvals, "pending", lambda: [public, private]):
            hud = {r["id"]: r for r in self.owner("/inbox")}
            phone = self.ok("/m/api/inbox")
        self.assertFalse(phone["more"])
        rows = {r["id"]: r for r in phone["rows"]}
        self.assertEqual(set(rows), set(hud))
        self.assertIn(f"tq:{self.task.id}:0", rows)
        for row_id, mine in rows.items():
            theirs = hud[row_id]
            # Exact key sets against the constants: a field inbox.rows (or a
            # card's to_json) gains later fails here until it is decided.
            self.assertEqual(set(theirs), set(remote_api.ROW_FIELDS), row_id)
            self.assertEqual(set(mine), set(remote_api.ROW_FIELDS), row_id)
            for key in remote_api.ROW_FIELDS:
                if key != "detail":
                    want = theirs[key]
                    self.assertEqual(mine[key], chat_reads.scrub(want) if isinstance(want, str)
                                     and key in ("title", "blocks", "origin") else want, (row_id, key))
            kind = theirs["kind"]
            if kind == "approval":
                self.assertEqual(set(theirs["detail"]) - set(remote_api.APPROVAL_DETAIL_FIELDS),
                                 set(remote_api.APPROVAL_DETAIL_DROPPED) & set(theirs["detail"]), row_id)
                self.assertEqual(set(mine["detail"]),
                                 set(theirs["detail"]) & set(remote_api.APPROVAL_DETAIL_FIELDS), row_id)
            else:
                self.assertEqual(set(theirs["detail"]), set(remote_api.DETAIL_FIELDS[kind]), row_id)
                self.assertEqual(set(mine["detail"]), set(remote_api.DETAIL_FIELDS[kind]), row_id)
        self.assertEqual(set(public.to_json()) - set(remote_api.APPROVAL_DETAIL_FIELDS),
                         set(remote_api.APPROVAL_DETAIL_DROPPED))
        self.assertLessEqual(set(private.to_json()), set(remote_api.APPROVAL_DETAIL_FIELDS) | {"code"})
        question = rows[f"tq:{self.task.id}:0"]["detail"]["text"]
        self.assertEqual(question, chat_reads.scrub("which db? " + TOKEN))
        self.assertNotIn(TOKEN, json.dumps(phone))
        print("ok  inbox: ROW_FIELDS and each kind's detail fields exactly; approvals drop",
              sorted(remote_api.DROPPED["approval_detail"]), "; every string scrubbed")

    def test_approval_detail_is_scrubbed_too(self):
        # The review's three leaks (#308): a private key's body in an argument
        # (card_args misses it), a token in origin and in relayed_from.title
        # (only clean_line'd), each through GET /m/api/inbox.
        req = ApprovalRequest(tool="Write", args={"file_path": "/tmp/deploy.pem", "content": PEM},
                              reason="write key", provider="claude", thread_id=self.chat.id,
                              origin='task 0000beef "' + TOKEN + '"')
        req.relayed_from = {"title": "Fix " + TOKEN, "provider": "claude", "thread_id": self.second.id,
                            "kind": "started", "extra": AGENT}
        req.req_id, req.code, req.asked_at, req.timeout_s = "req-x", "wxyz", "2026-10-25T10:00:00+00:00", 600.0
        with patch.object(self.d.approvals, "pending", lambda: [req]):
            hud = next(r for r in self.owner("/inbox") if r["kind"] == "approval")
            body = self.ok("/m/api/inbox")
        row = next(r for r in body["rows"] if r["kind"] == "approval")
        self.assertEqual(hud["detail"]["args"]["file_path"], "/tmp/deploy.pem")
        text = json.dumps(row)
        self.assertNotIn(PEM_BODY, text)
        self.assertNotIn(TOKEN, row["origin"])                       # the row's own origin (M1)
        self.assertNotIn(TOKEN, json.dumps(row["detail"]["origin"]))
        self.assertNotIn(TOKEN, json.dumps(row["detail"]["relayed_from"]))
        self.assertEqual(set(row["detail"]["relayed_from"]), set(remote_api.RELAYED_FIELDS))
        self.assertNotIn(AGENT, text)
        self.assertEqual(row["detail"]["args"]["file_path"], "/tmp/deploy.pem")
        print("ok  an approval's detail is scrubbed on the phone: a PEM body in an argument, a token in "
              "origin and in relayed_from.title are withheld")

    def test_private_approval_is_its_private_projection(self):
        private = approval("req-private", private=True)
        with patch.object(self.d.approvals, "pending", lambda: [private]):
            phone = self.ok("/m/api/inbox")
        row = next(r for r in phone["rows"] if r["kind"] == "approval")
        allowed = set(private.to_json()) - set(remote_api.APPROVAL_DETAIL_DROPPED)
        self.assertEqual(set(row["detail"]), allowed)
        self.assertTrue(row["detail"]["hud_only"])
        for key in ("args", "command", "reason", "browser", "origin", "headline", "task_id"):
            self.assertNotIn(key, row["detail"])
        self.assertEqual((row["origin"], row["project_id"], row["task_id"]), ("", None, None))
        self.assertNotIn(AGENT, json.dumps(phone))
        self.assertNotIn("owner_json", inspect.getsource(remote_api).replace("no `owner_json`", ""))
        print("ok  a private approval row: only its private projection (no owner_json, no Browser detail)")

    def row_of(self, kind):
        return {"id": f"{kind}:abc", "kind": kind, "rank": 8, "title": "Denied: " + TOKEN,
                "blocks": "b " + TOKEN, "project_id": None, "thread_id": self.chat.id, "task_id": None,
                "asked_at": "2026-10-25T10:00:00+00:00", "expires_at": None, "origin": "o " + TOKEN,
                "cwd": "/home/owner/secret-project",
                "detail": {"req_id": "r1", "tool": "Bash", "origin": "o " + TOKEN, "resolution": "timeout",
                           "at": "2026-10-25T10:00:00+00:00", "text": "words " + TOKEN,
                           "cwd": "/home/owner/secret-project", "nested": {"text": AGENT}, "list": [AGENT],
                           "n": 3}}

    def test_a_row_of_an_unknown_kind_is_served_generically(self):
        # The review's case: a token in the title and a cwd path, in a kind
        # P-3 does not know (and in A3's missed rows, now a known kind).
        from jarvis.v2 import inbox
        for kind, fields in (("later", {"req_id", "tool", "origin", "resolution", "at", "text"}),
                             ("missed", set(remote_api.DETAIL_FIELDS["missed"]) - {"thread_id", "task_id"})):
            with patch.object(inbox, "rows", lambda daemon, kind=kind: [self.row_of(kind)]):
                phone = self.ok("/m/api/inbox")
            (row,) = phone["rows"]
            self.assertEqual(set(row), set(remote_api.ROW_FIELDS), kind)
            self.assertEqual(set(row["detail"]), fields, kind)
            text = json.dumps(phone)
            for leak in (TOKEN, "/home/owner/secret-project", AGENT):
                self.assertNotIn(leak, text, kind)
            self.assertEqual(row["title"], chat_reads.scrub("Denied: " + TOKEN))
        self.assertEqual(row["detail"]["origin"], chat_reads.scrub("o " + TOKEN))
        print("ok  an unknown kind and A3's missed rows: ROW_FIELDS only, allowlisted scalar detail, "
              "title, blocks and origin scrubbed")

    def test_a_real_missed_row_from_a3(self):
        from jarvis.v2 import inbox
        req = approval("req-gone", thread_id=self.chat.id)
        req.origin = 'task 0000beef "' + TOKEN + '"'
        entry = inbox.missed_entry(req, "timeout", "2026-10-25T10:00:00+00:00")
        self.assertEqual(set(entry), set(remote_api.DETAIL_FIELDS["missed"]))
        row = remote_api.inbox_row({"id": "missed:req-gone", "kind": "missed", "rank": 8, "title": "t",
                                    "blocks": "b", "project_id": None, "thread_id": self.chat.id,
                                    "task_id": None, "asked_at": entry["at"], "expires_at": None,
                                    "origin": entry["origin"], "detail": entry})
        self.assertEqual(set(row["detail"]), set(entry))
        self.assertNotIn(TOKEN, json.dumps(row))
        print("ok  A3's missed_entry fields are exactly DETAIL_FIELDS['missed'], scrubbed")


class Review308(Api):
    """The independent review's probes on PR #308, kept as regressions."""

    def test_task_status_is_allowlisted_and_scrubbed(self):
        live = self.stores.tasks.get(self.task.id)
        live.status.last_file = str(Path(self.task.worktree) / "app.py")
        live.status.open_question = "which key? " + TOKEN
        live.status.last_tool = "Bash " + TOKEN
        self.stores.tasks.save(live)
        hud = self.owner(f"/tasks/{self.task.id}")
        one = self.ok(f"/m/api/tasks/{self.task.id}")["task"]
        listed = next(t for t in self.ok("/m/api/tasks")["tasks"] if t["id"] == self.task.id)
        for status in (one["status"], listed["status"]):
            self.assertEqual(set(hud["status"]) - set(status), set(remote_api.DROPPED["task_status"]))
            self.assertLessEqual(set(status), set(remote_api.STATUS_FIELDS))
            self.assertEqual(status["open_question"], chat_reads.scrub("which key? " + TOKEN))   # M5
        text = json.dumps([one, listed])
        self.assertNotIn(self.task.worktree, text)
        self.assertNotIn(TOKEN, text)
        print("ok  a task's status: STATUS_FIELDS only (no last_file path), scrubbed")

    def test_after_takes_ascii_digits_only(self):
        for q in ("%C2%B2", "%D9%A3", "%EF%BC%91", "1%C2%B2", "+1", "%201"):
            status, _, body = self.get(f"/m/api/threads/{self.chat.id}/log?after={q}")
            self.assertEqual((status, body), (400, {"error": "bad request"}), q)
        print("ok  after: ASCII digits only (a superscript two or an Arabic-Indic three is 400, not 500)")

    def test_a_revoke_during_a_read_gets_401(self):
        import threading
        real = self.stores.threads.read_log
        reading, release = threading.Event(), threading.Event()

        def slow(thread_id):
            reading.set()
            release.wait(5)
            return real(thread_id)

        out = {}
        with patch.object(self.stores.threads, "read_log", slow):
            worker = threading.Thread(target=lambda: out.update(r=self.get(f"/m/api/threads/{self.chat.id}/log")))
            worker.start()
            self.assertTrue(reading.wait(5))
            self.assertTrue(self.auth.revoke(self.auth.status()["passkeys"][0]["key"]))
            release.set()
            worker.join(10)
        status, _, body = out["r"]
        self.assertEqual((status, body), (401, {"error": "unauthorized"}))
        print("ok  a revoke while a read is in flight: the read answers 401, not the data")

    def test_a_thread_archived_on_its_own_is_not_served(self):
        archived = self.stores.threads.get(self.second.id)
        archived.archived = "2026-10-25T09:30:00+00:00"
        self.stores.threads.save(archived)
        self.assertIsNone(self.stores.projects.get(self.project.id).archived)
        self.assertEqual(self.get(f"/m/api/threads/{self.second.id}/log")[0], 404)       # M2
        listed = {t["id"] for t in self.ok(f"/m/api/threads?project={self.project.id}")["threads"]}
        self.assertNotIn(self.second.id, listed)
        self.assertIn(self.chat.id, listed)
        print("ok  a thread archived on its own, in a live project: not listed, its log 404")

    def test_aws_secret_and_password_shapes_are_scrubbed(self):
        self.append(self.second, {"kind": "text", "data": {
            "text": _fill("aws_secret_access_key = @AWS_SECRET@ and @K_PASSWORD@: @PW_LONG@")}})
        self.append(self.second, {"kind": "text", "data": {"text": "password: reset it in settings"}})
        body = self.ok(f"/m/api/threads/{self.second.id}/log")
        text = json.dumps(body)
        self.assertNotIn(SAMPLES["AWS_SECRET_HEAD"], text)
        self.assertNotIn(SAMPLES["PW_LONG"], text)
        self.assertEqual(body["rows"][-1]["text"], "password: reset it in settings")
        print("ok  the ~/.aws/credentials form and password: <x> are withheld; prose after a label is kept")

    def test_no_stream_events_for_an_archived_projects_threads(self):
        s = Stream(self.port, self.cookie)
        self.addCleanup(s.close)
        s._until(b": connected\n\n", 5)
        bus = self.d.bus
        bus.publish({"kind": "text", "thread_id": self.hidden_chat.id, "project_id": self.other.id, "data": {}})
        bus.publish({"kind": "text", "thread_id": self.hidden_chat.id, "data": {}})          # no project id
        bus.publish({"kind": "task_status_changed", "task_id": self.hidden_task.id, "data": {}})
        bus.publish({"kind": "text", "thread_id": self.chat.id, "project_id": self.project.id, "data": {}})
        items = [i for i in s.items(2, timeout=3) if i["kind"] != "inbox_changed"]
        rest = s.body(0.6)
        self.assertEqual(items[0]["data"]["thread_id"], self.chat.id)
        self.assertNotIn(self.hidden_chat.id.encode(), rest)
        self.assertNotIn(b'"task_changed"', rest)
        # Restored: its events come again.
        other = self.stores.projects.get(self.other.id)
        other.archived = None
        self.stores.projects.save(other)
        bus.publish({"kind": "project_restored", "project_id": self.other.id, "data": {}})
        bus.publish({"kind": "text", "thread_id": self.hidden_chat.id, "project_id": self.other.id, "data": {}})
        found = [i for i in s.items(2, timeout=3) if i["kind"] == "thread_changed"]
        self.assertEqual(found[-1]["data"]["thread_id"], self.hidden_chat.id)
        print("ok  stream: nothing for an archived project's threads or tasks; restored, they come again")


class LabelledValues(unittest.TestCase):
    """`remote_api.labelled_values` (review of PR #308, and its delta review):
    a value after a credential's label is withheld **on the phone only**; the
    shared scrub (`redact.scrub_text`, `chat_reads.scrub`, which feed agents
    through chat relays and git tool output) is exactly main's."""

    MUST_WITHHOLD = [(_fill(a), _fill(b)) for a, b in [
        ("aws_secret_access_key = @AWS_SECRET@", "@AWS_SECRET_HEAD@"),
        ("@K_PASSWORD@: @PW_SHORT@", "@PW_SHORT@"),
        ("@K_PASSWORD@: @PW_LONG@", "@PW_LONG@"),
        ('"@K_PASSWORD@": "@PW_DASH@"', "@PW_DASH@"),
        ("client_secret: @GOOGLE_SECRET@", "@GOOGLE_SECRET@"),
        ("api_key: @HEX_KEY@", "@HEX_KEY_HEAD@"),
        ("AWS_SESSION_TOKEN: @AWS_SESSION@", "@AWS_SESSION_HEAD@"),
        ("@K_PWD@=@PW_BANG@", "@PW_BANG@"),
        ("the login is admin, @K_PASSWORD@: @PW_DOT@.", "@PW_DOT@"),
    ]]

    def test_the_shared_scrub_is_mains(self):
        from jarvis.v2 import redact
        self.assertFalse(hasattr(redact, "labelled_values"))
        for line, (reads, shared) in MAIN_SCRUB.items():
            self.assertEqual(chat_reads.scrub(line), reads, line)
            self.assertEqual(redact.scrub_text(line), shared, line)
        print(f"ok  the shared scrub is main's on all {len(MAIN_SCRUB)} review lines "
              "(redact.scrub_text, chat_reads.scrub)")

    def test_code_and_prose_after_a_label_are_kept_on_the_phone(self):
        self.assertLessEqual(set(LABELLED_CODE), set(LABELLED_KEEP))
        for line in LABELLED_KEEP:
            self.assertEqual(remote_api.labelled_values(line), line, line)
            self.assertEqual(remote_api._scrub(line), MAIN_SCRUB[line][0], line)
        for line in ("password: reset it in settings", "Password: required", "password: <your password>",
                     "password=$PASSWORD", "password: xxxxxxxxxx", "the api_key: see the README",
                     "a password manager stores it", "passwords: none"):
            self.assertEqual(remote_api.labelled_values(line), line, line)
        print(f"ok  phone: all {len(LABELLED_KEEP)} code, config and prose lines kept "
              f"(the {len(LABELLED_CODE)} the first rule altered among them)")

    def test_values_after_a_label_are_withheld_on_the_phone(self):
        for line, secret in self.MUST_WITHHOLD:
            out = remote_api._scrub(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("[redacted]", out, line)
        self.assertEqual(remote_api.labelled_values(_fill("@K_PASSWORD@: @PW_DOT@.")), _fill("@K_PASSWORD@: [redacted]."))
        caught = [line for line in LABELLED_PROBE_WITHHOLD if remote_api._scrub(line) != line]
        print(f"ok  phone: the must-withhold shapes withheld; {len(caught)} of the probe's "
              f"{len(LABELLED_PROBE_WITHHOLD)} secret lines altered (plain words after a label, and "
              "unlabelled `secret:`/`token:`, are deliberately kept)")


# -- the transcript ---------------------------------------------------------------------


class Log(Api):
    def projection(self, thread):
        records = [r if isinstance(r, dict) else {} for r in self.stores.threads.read_log(thread.id)]
        return records, chat_reads.projected(records, thread.provider)

    def test_rows_are_chat_reads_projection(self):
        body = self.ok(f"/m/api/threads/{self.chat.id}/log")
        records, rows = self.projection(self.chat)
        self.assertEqual([(r["seq"], r["role"], r["text"], r["at"], tuple(r["marks"])) for r in body["rows"]],
                         [(i, row.role, row.text, row.at, row.marks) for i, row in rows])
        self.assertEqual([row for _, row in rows], chat_reads.transcript(records, self.chat.provider))
        self.assertEqual(body["next"], len(records))
        self.assertFalse(body["older"] or body["more"])
        self.assertEqual(body["thread"]["id"], self.chat.id)
        roles = [r["role"] for r in body["rows"]]
        self.assertEqual(roles, ["owner", "Claude", "system"])
        text = json.dumps(body)
        for leak in (TOKEN, "INLINED-FILE", "TERMINAL-OUTPUT", "cat .env"):
            self.assertNotIn(leak, text)
        self.assertIn("**Done.**", text)           # markdown arrives as text; the phone renders it
        print("ok  the transcript is chat_reads' projection row for row: no tool output, inlined files or tokens")

    def test_after_pages_and_caps(self):
        for i in range(250):
            self.append(self.second, {"kind": "text", "data": {"text": f"reply {i}"}})
            if i % 50 == 0:
                self.append(self.second, {"kind": "tool_started", "data": {"name": "Read"}})
        path = f"/m/api/threads/{self.second.id}/log"
        records, rows = self.projection(self.second)
        tail = self.ok(path)
        self.assertTrue(tail["older"])
        self.assertEqual(len(tail["rows"]), remote_api.LOG_TAIL)
        self.assertEqual([r["seq"] for r in tail["rows"]], [i for i, _ in rows[-remote_api.LOG_TAIL:]])
        self.assertEqual(tail["next"], len(records))
        self.assertEqual(self.ok(f"{path}?after={tail['next']}")["rows"], [])
        # Paging from the start: at most LOG_PAGE rows each, and every row once.
        seen, cursor, pages = [], 0, 0
        while True:
            page = self.ok(f"{path}?after={cursor}")
            pages += 1
            self.assertLessEqual(len(page["rows"]), remote_api.LOG_PAGE)
            seen += [r["seq"] for r in page["rows"]]
            cursor = page["next"]
            if not page["more"]:
                break
        self.assertEqual(seen, [i for i, _ in rows])
        self.assertEqual(pages, 2)
        # New records after the cursor are exactly what the next call returns.
        self.append(self.second, {"kind": "text", "data": {"text": "the newest"}})
        fresh = self.ok(f"{path}?after={cursor}")
        self.assertEqual([r["text"] for r in fresh["rows"]], ["the newest"])
        # The byte cap ends a page early, and the cursor resumes after it.
        with patch.object(remote_api, "LOG_BYTES", 2000):
            small = self.ok(f"{path}?after=0")
        self.assertTrue(small["more"])
        self.assertLess(len(small["rows"]), 20)
        self.assertEqual(small["next"], small["rows"][-1]["seq"] + 1)
        # A row past ROW_CHARS is cut and says so.
        self.append(self.second, {"kind": "text", "data": {"text": "x" * (remote_api.ROW_CHARS + 500)}})
        last = self.ok(f"{path}?after={fresh['next']}")["rows"][-1]
        self.assertEqual(last["cut"], remote_api.ROW_CHARS + 500)
        self.assertEqual(len(last["text"]), remote_api.ROW_CHARS)
        self.assertTrue(last["text"].endswith(remote_api.ROW_CUT))
        print(f"ok  log: tail {remote_api.LOG_TAIL}, pages of {remote_api.LOG_PAGE} by `after`, "
              f"a byte cap, rows cut at {remote_api.ROW_CHARS}")


# -- the diff ---------------------------------------------------------------------------


class Diff(Api):
    def setUp(self):
        super().setUp()
        wt = Path(self.task.worktree)
        (wt / "app.py").write_text("def f(n):\n    return n + 1\n")
        (wt / "new.txt").write_text("new\n")
        (wt / "big.txt").write_text("".join(f"line {i}\n" for i in range(4000)))
        (wt / ".env").write_text("SECRET_TOKEN=" + TOKEN + "\n")
        (wt / "notes.md").write_text("token " + TOKEN + "\n")

    def test_equals_the_diff_tab(self):
        (Path(self.task.worktree) / "notes.md").unlink()
        hud = self.owner(f"/tasks/{self.task.id}/diff")
        phone = self.ok(f"/m/api/tasks/{self.task.id}/diff")
        self.assertFalse(phone["truncated"])
        self.assertNotIn("note", phone)
        self.assertEqual([{k: v for k, v in f.items() if k != "patch"} for f in phone["files"]], hud["files"])
        self.assertEqual("".join(f["patch"] for f in phone["files"]), hud["patch"])
        self.assertEqual((phone["base"], phone["head"]), (hud["base"], hud["head"]))
        self.assertEqual({f["path"] for f in phone["files"]}, {"app.py", "new.txt", "big.txt"})
        print("ok  diff: the Diff tab's files and patches, file by file; .env skipped")

    def test_a_token_in_a_changed_file_is_withheld(self):
        phone = self.ok(f"/m/api/tasks/{self.task.id}/diff")
        notes = next(f for f in phone["files"] if f["path"] == "notes.md")
        self.assertNotIn(TOKEN, json.dumps(phone))
        self.assertTrue(notes["patch"].endswith("\n"))
        self.assertIn("+token ", notes["patch"])
        print("ok  diff: a token in a changed file is withheld from the phone (the transcript's scrub too)")

    def test_caps(self):
        with patch.object(remote_api, "DIFF_FILE_BYTES", 4096):
            phone = self.ok(f"/m/api/tasks/{self.task.id}/diff")
        big = next(f for f in phone["files"] if f["path"] == "big.txt")
        self.assertEqual((big["patch"], big["too_large"]), (None, True))
        self.assertTrue(phone["truncated"])
        self.assertEqual(phone["note"], remote_api.TOO_LARGE)
        self.assertIn("open it at the desk", remote_api.TOO_LARGE)
        self.assertTrue(all(f["patch"] for f in phone["files"] if f["path"] != "big.txt"))
        with patch.object(remote_api, "DIFF_FILES", 2):
            phone = self.ok(f"/m/api/tasks/{self.task.id}/diff")
        self.assertLessEqual(len(phone["files"]), 2)
        self.assertTrue(phone["truncated"])
        with patch.object(remote_api, "DIFF_BYTES", 300):
            phone = self.ok(f"/m/api/tasks/{self.task.id}/diff")
        self.assertTrue(phone["truncated"])
        self.assertLessEqual(sum(len((f["patch"] or "").encode()) for f in phone["files"]), 300)
        print("ok  diff caps: per file, in all and by files, with the 'open it at the desk' marker")

    def test_no_worktree(self):
        bare = self.stores.tasks.create(self.project.id, "not started")
        self.stores.tasks.save(bare)
        status, _, body = self.get(f"/m/api/tasks/{bare.id}/diff")
        self.assertEqual((status, body), (409, {"error": "task has no git worktree"}))
        print("ok  a task without a worktree: 409, as the Diff tab")


# -- the event stream -------------------------------------------------------------------


class Stream:
    """A raw SSE client: the headers, then `data:` items as they come."""

    def __init__(self, port, cookie, *, host=HOST):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        headers = f"GET /m/api/events HTTP/1.1\r\nHost: {host}\r\n"
        if cookie:
            headers += f"Cookie: {cookie}\r\n"
        self.sock.sendall((headers + "\r\n").encode())
        self.buffer = b""
        self.closed = False
        head = self._until(b"\r\n\r\n", 5)
        self.status = int(head.split(b" ", 2)[1])
        self.head = head.decode("latin-1")

    def _until(self, mark, timeout):
        end = time.monotonic() + timeout
        while mark not in self.buffer:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError(mark)
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise TimeoutError(mark) from None
            if not chunk:
                self.closed = True
                raise EOFError(self.buffer)
            self.buffer += chunk
        cut = self.buffer.index(mark) + len(mark)
        out, self.buffer = self.buffer[:cut], self.buffer[cut:]
        return out

    def items(self, n, timeout=5):
        out = []
        while len(out) < n:
            block = self._until(b"\n\n", timeout).decode()
            for line in block.splitlines():
                if line.startswith("data: "):
                    out.append(json.loads(line[6:]))
        return out

    def body(self, timeout=1.0):
        """Everything until `timeout` passes with nothing new, or the end."""
        out = b""
        self.sock.settimeout(timeout)
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.closed = True
                    break
                out += chunk
        except socket.timeout:
            pass
        return self.buffer + out

    def ends_within(self, seconds) -> bool:
        end = time.monotonic() + seconds
        self.sock.settimeout(0.2)
        while time.monotonic() < end:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return True
            if not chunk:
                self.closed = True
                return True
            self.buffer += chunk
        return False

    def close(self):
        self.sock.close()


class Events(Api):
    def stream(self, cookie=True, **kw):
        s = Stream(self.port, self.cookie if cookie is True else cookie, **kw)
        self.addCleanup(s.close)
        return s

    def opened(self):
        s = self.stream()
        self.assertEqual(s.status, 200, s.head)
        self.assertIn("text/event-stream", s.head)
        self.assertIn("Cache-Control: no-store", s.head)
        self.assertIn("frame-ancestors 'none'", s.head)
        s._until(b": connected\n\n", 5)
        return s

    def test_needs_a_session_at_connect(self):
        for cookie in (None, "__Host-jarvis_m=" + "B" * 43):
            s = self.stream(cookie)
            self.assertEqual(s.status, 401, s.head)
        s = self.stream(host="localhost")
        self.assertEqual(s.status, 400)
        self.assertEqual(self.opened().status, 200)
        print("ok  stream: 401 without a session at connect, 400 with a wrong Host")

    def test_carries_only_the_allowed_kinds_and_no_text(self):
        s = self.opened()
        bus = self.d.bus
        t, p, k = self.chat.id, self.project.id, self.task.id
        bus.publish({"kind": "text_delta", "thread_id": t, "project_id": p, "data": {"text": AGENT}})
        bus.publish({"kind": "tool_started", "thread_id": t, "project_id": p, "data": {"args": AGENT}})
        bus.publish({"kind": "thinking", "thread_id": t, "data": {"text": AGENT}})
        bus.publish({"kind": "usage", "thread_id": t, "data": {"input": 1}})
        bus.publish({"kind": "mute", "data": {"muted": True}})
        bus.publish({"kind": "text", "thread_id": t, "project_id": p, "data": {"text": AGENT}})
        bus.publish({"kind": "inbox_changed", "data": {"ids": ["appr:r1"], "reason": "approval_requested",
                                                        "extra": AGENT}})
        bus.publish({"kind": "task_status_changed", "task_id": k, "project_id": p, "data": {"brief": AGENT}})
        bus.publish({"kind": "thread_updated", "thread_id": t, "project_id": p,
                     "data": {"title": AGENT, "changed": ["title"]}})
        items = s.items(4)
        self.assertEqual(items[0], {"kind": "thread_changed",
                                    "data": {"thread_id": t, "project_id": p, "reason": "text"}})
        self.assertEqual(items[1], {"kind": "inbox_changed",
                                    "data": {"ids": ["appr:r1"], "reason": "approval_requested"}})
        self.assertEqual(items[2], {"kind": "task_changed",
                                    "data": {"task_id": k, "project_id": p, "reason": "task_status_changed"}})
        # task_status_changed is also an inbox source: the inbox observer's own
        # inbox_changed may come before the thread_updated; collect the rest.
        rest = items[3:] + s.items(1, timeout=3)
        kinds = {i["kind"] for i in items + rest}
        self.assertLessEqual(kinds, {"thread_changed", "inbox_changed", "task_changed", "resync"})
        self.assertIn({"kind": "thread_changed", "data": {"thread_id": t, "project_id": p,
                                                          "reason": "thread_updated"}}, items + rest)
        tail = s.body(0.6)
        self.assertNotIn(AGENT.encode(), tail)
        self.assertNotIn(b"text_delta", tail)
        print("ok  stream: inbox_changed {ids, reason}, thread_changed and task_changed ids only; "
              "deltas, tools, thinking, usage and every text dropped")

    def test_record_shapes(self):
        self.assertIsNone(remote_api.stream_record({"kind": "text", "thread_id": "../x"}))
        self.assertIsNone(remote_api.stream_record({"kind": "approval_requested", "data": {"command": AGENT}}))
        self.assertIsNone(remote_api.stream_record("text"))
        self.assertEqual(remote_api.stream_record({"kind": "inbox_changed",
                                                   "data": {"ids": ["ok:1", "bad id <b>", 7], "reason": "x y"}}),
                         {"kind": "inbox_changed", "data": {"ids": ["ok:1"], "reason": ""}})
        for kind in remote_api.STREAM_KINDS - {"shutdown", "inbox_changed"}:
            shaped = remote_api.stream_record({"kind": kind, "thread_id": self.chat.id,
                                               "task_id": self.task.id, "data": {"text": AGENT}})
            self.assertIsNotNone(shaped, kind)
            self.assertNotIn(AGENT, json.dumps(shaped))
        print("ok  stream records: ids validated, every kind's text dropped")

    def test_ends_on_logout(self):
        s = self.opened()
        status, _, _ = self.phone("POST", "/m/api/logout", {}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertTrue(s.ends_within(3), "the stream outlived its logout")
        print("ok  stream: ends on logout")

    def test_ends_on_revoke(self):
        s = self.opened()
        other = self.stream()               # the same session, a second stream
        other._until(b": connected\n\n", 5)
        key = self.auth.status()["passkeys"][0]["key"]
        self.assertTrue(self.auth.revoke(key))
        self.assertTrue(s.ends_within(3), "the stream outlived a revoke")
        self.assertTrue(other.ends_within(3))
        # An event published after the revoke never reaches it.
        self.d.bus.publish({"kind": "inbox_changed", "data": {"ids": ["appr:late"], "reason": "x"}})
        self.assertNotIn(b"appr:late", s.buffer)
        print("ok  stream: a revoke at the desk ends every stream of that passkey at once")

    def test_ends_on_expiry_and_is_not_activity(self):
        s = self.opened()
        before = next(iter(self.auth._sessions.values()))["last_seen"]
        self.wall.now += 120
        s.body(1.5)
        self.assertEqual(next(iter(self.auth._sessions.values()))["last_seen"], before,
                         "an open stream touched the session")
        self.wall.now += remote_auth.IDLE_S
        self.assertTrue(s.ends_within(3), "the stream outlived its session's idle expiry")
        self.cookie = self.signed_in(self.a)
        s = self.opened()
        self.wall.now += remote_auth.ABSOLUTE_S
        self.assertTrue(s.ends_within(3), "the stream outlived its session's absolute end")
        print("ok  stream: ends on idle and absolute expiry, and never refreshes the session itself")

    def test_oldest_displaced_past_the_cap(self):
        with patch.object(remote_api, "MAX_STREAMS", 2):
            first, second = self.opened(), self.opened()
            third = self.opened()
            self.assertTrue(first.ends_within(3))
            self.assertFalse(second.ends_within(0.5))
            self.assertFalse(third.ends_within(0.2))
        print("ok  stream: past MAX_STREAMS the oldest is ended")

    def test_daemon_stop_ends_it(self):
        s = self.opened()
        self.d.stop()
        self.assertTrue(s.ends_within(5))
        print("ok  stream: the daemon's shutdown ends it")


# -- the boundary -------------------------------------------------------------------------


class Boundary(Api):
    def test_is_owner_still_false(self):
        server = self.auth.server
        handler = rac.OwnerTest.handler(self, server, "GET", [("Host", HOST), ("Cookie", self.cookie)])
        self.assertTrue(remote_auth.is_remote_owner(handler))
        self.assertFalse(projects.is_owner(handler, self.d))
        asked = []
        real = projects.is_owner

        def spy(handler, daemon):
            asked.append(handler.server)
            return real(handler, daemon)

        with patch.object(projects, "is_owner", spy):
            for path in self.reads():
                self.ok(path)
        self.assertFalse([s for s in asked if getattr(s, "remote_only", False)])
        source = inspect.getsource(remote_api)
        for word in ("is_owner(", "owner_only(", "owner_json(", "owner_details(", "resolve_approval",
                     ".answer(", "deliver(", "publish("):
            self.assertNotIn(word, source, word)
        print("ok  is_owner stays false on the remote listener and remote_api never asks it, answers or sends")

    def test_agent_path_deny_covers_the_new_routes(self):
        port = config.REMOTE_PORT
        for path in self.reads() + ["/m/api/events"]:
            for line in (f"curl http://127.0.0.1:{port}{path}", f"curl -N http://localhost:{port}{path}",
                         f"curl https://box.tail1234.ts.net{path}", f"wget -qO- http://127.0.0.1:8402{path}",
                         f"python3 -c \"import urllib.request as u; u.urlopen('http://[::1]:{port}{path}')\""):
                self.assertIsNotNone(rules.remote_reading(line), line)
                verdict = rules.decide(line)
                self.assertEqual((verdict.decision, verdict.reason), (rules.DENY, rules.REMOTE_DENIED), line)
        print("ok  the agent-path DENY covers every new /m/api route, on loopback and the ts.net name")

    def test_self_protected(self):
        from jarvis.tools.files import SELF_PROTECTED
        self.assertIn("jarvis/v2/remote_api.py", SELF_PROTECTED)
        print("ok  remote_api.py is SELF_PROTECTED")

    def test_api_listener_and_hud_never_serve_it(self):
        for port in (self.d.port, self.d.face_port):
            for path in self.reads() + ["/m/api/events"]:
                status, _, _ = raw(port, "GET", path, [("Host", f"127.0.0.1:{port}"), ("Cookie", self.cookie)])
                self.assertEqual(status, 404, (port, path))
        print("ok  the API and HUD listeners answer the phone's reads with 404, cookie or not")


if __name__ == "__main__":
    unittest.main(verbosity=1)

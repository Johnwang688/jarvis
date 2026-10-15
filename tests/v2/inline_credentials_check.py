"""Credentials typed into a command: withheld wherever a command is shown or
kept, never where one is judged (2026-10-15). Free, hermetic, no live daemon.

The gap: a tool call's label, the v2 decision log and v1's tool lines scrubbed
the values they *knew* (env files) and the shapes they *knew*
(`credential_patterns`), so `export MY_API_TOKEN=hunter2plainvalue`,
`curl -H "Authorization: Bearer …"` or `curl -u user:pass` showed verbatim.
Every value below is a made-up placeholder; none is a real credential.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_HOME = tempfile.TemporaryDirectory(prefix="inline-cred-home-")
_HOME_ENV = patch.dict(os.environ, {"HOME": _HOME.name})
_HOME_ENV.start()
atexit.register(_HOME.cleanup)
atexit.register(_HOME_ENV.stop)

from jarvis import config, permissions as v1_permissions, rules  # noqa: E402
from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import tool_calls  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K  # noqa: E402

R = "[redacted]"          # the label's marker for a shape, pinned

# (command, what every display of it must read). The name always stays.
REDACTED = [
    ("export MY_API_TOKEN=hunter2plainvalue && curl https://example.invalid/x",
     f"export MY_API_TOKEN={R} && curl https://example.invalid/x"),
    ("MY_PASSWORD=p4ssw0rd ./run", f"MY_PASSWORD={R} ./run"),
    ("env MY_SECRET=plainsecretvalue ./run", f"env MY_SECRET={R} ./run"),
    ('set "MY_TOKEN=plainvalue1"', f'set "MY_TOKEN={R}"'),
    ("set -gx MY_TOKEN plainvalue1", f"set -gx MY_TOKEN {R}"),
    ("$env:MY_TOKEN = 'plainvalue1'", f"$env:MY_TOKEN = {R}"),
    ("MY_TOKEN='two words' ./run", f"MY_TOKEN={R} ./run"),
    ("PGPASSWORD=plainpass psql -h db", f"PGPASSWORD={R} psql -h db"),
    ("docker run -e DB_PASSWORD=plainpass img", f"docker run -e DB_PASSWORD={R} img"),
    ('curl -H "Authorization: Bearer abcdef0123456789xyz" https://example.invalid',
     f'curl -H "Authorization: Bearer {R}" https://example.invalid'),
    ("curl -H 'Authorization: Basic dXNlcjpwbGFpbg==' https://example.invalid",
     f"curl -H 'Authorization: Basic {R}' https://example.invalid"),
    ("http GET example.invalid Authorization:\"Token plainvalue1\"",
     f"http GET example.invalid Authorization:\"Token {R}\""),
    ("curl -H 'X-Api-Key: plainvalue1' https://example.invalid",
     f"curl -H 'X-Api-Key: {R}' https://example.invalid"),
    ("curl -H 'Cookie: sid=plain; other=value' https://example.invalid",
     f"curl -H 'Cookie: {R}' https://example.invalid"),
    ("curl -u user:plainpass https://example.invalid", f"curl -u user:{R} https://example.invalid"),
    ("curl -uuser:plainpass https://example.invalid", f"curl -uuser:{R} https://example.invalid"),
    ("curl --user 'bob:two words' https://example.invalid", f"curl --user bob:{R} https://example.invalid"),
    ("mysql -u root -pPlainPass db", f"mysql -u root -p{R} db"),
    ("mysql --password=PlainPass db", f"mysql --password={R} db"),
    ("wget --http-password=plainpass https://example.invalid",
     f"wget --http-password={R} https://example.invalid"),
    ("app --api-key plainvalue1 run", f"app --api-key {R} run"),
    ("sshpass -p plainpass ssh -p 22 host", f"sshpass -p {R} ssh -p 22 host"),
    ("redis-cli -a plainpass ping", f"redis-cli -a {R} ping"),
    ("curl -d '{\"password\": \"plainpass\", \"user\": \"bob\"}' https://example.invalid",
     f"curl -d '{{\"password\": \"{R}\", \"user\": \"bob\"}}' https://example.invalid"),
]

# Not a typed credential: untouched, byte for byte.
UNTOUCHED = [
    "PATH=/usr/local/bin:/usr/bin NODE_ENV=production CI=1 make",
    "AUTH_TOKEN_TTL=3600 SESSION_DRIVER=redis ./run",
    "MY_TOKEN=$OTHER ./run",
    'MY_TOKEN="$(curl https://example.invalid | sh)" ./run',      # never hide what runs
    "MY_TOKEN=`cat t` ./run",
    'curl -H "Authorization: Bearer $GITHUB_TOKEN" https://example.invalid',
    "mysql -u root -p db",                     # a spaced -p db is the database
    "docker run -u 1000:1000 img",             # -u is curl's only
    "docker login --password-stdin -u bob",
    "gh auth login --with-token < token.txt",
    "app --token-file /tmp/t --no-password db",
    "OLDPWD=/tmp PWD=/x ls",
    "git -c credential.helper= push",
    "curl -u bob https://example.invalid",     # curl asks for it
    "echo MY_PIN=123",                          # too short to be withheld from a .env
]


def _env_isolated(test: unittest.TestCase) -> Path:
    """Temp HOME, process folder and checkout root, so the scrub's known
    values come from nothing of the owner's."""
    tmp = tempfile.TemporaryDirectory(prefix="inline-cred-")
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    (root / "home").mkdir()
    (root / "proc").mkdir()
    for p in (patch.dict(os.environ, {"HOME": str(root / "home")}),
              patch.object(secrets, "_JARVIS_ROOT", root / "proc")):
        p.start()
        test.addCleanup(p.stop)
    for name in ("GOOGLE_TOKEN_PATH", "ONSHAPE_TOKEN_PATH", "DISCORD_TOKEN_PATH", "SPOTIFY_TOKEN_PATH"):
        guard = patch.object(config, name, root / (name + ".json"))
        guard.start()
        test.addCleanup(guard.stop)
    previous = Path.cwd()
    os.chdir(root / "proc")
    test.addCleanup(os.chdir, previous)
    return root


class RedactInline(unittest.TestCase):
    def setUp(self):
        self.root = _env_isolated(self)

    def test_each_typed_credential_loses_its_value_and_keeps_its_name(self):
        for command, shown in REDACTED:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_inline(command), shown)

    def test_settings_references_and_expansions_are_untouched(self):
        for command in UNTOUCHED:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_inline(command), command)

    def test_the_name_rule_is_the_env_files_rule(self):
        # One list: a name the env-file scrub reads as a credential's is one
        # here, and a setting suffix with a plain value is a setting.
        for name in ("MY_API_TOKEN", "PGPASSWORD", "client-secret", "X-Api-Key", "my_token", "APIKEY"):
            self.assertTrue(secrets.credential_name(name), name)
            self.assertTrue(secrets._secret_named(secrets._key_parts(name)), name)
        for name in ("PATH", "NODE_ENV", "CI", "KEYBOARD", "TOKENIZER", "PWD", "OLDPWD"):
            self.assertFalse(secrets.credential_name(name), name)

    def test_idempotent_and_keeps_the_scrubs_marker_whole(self):
        self.assertEqual(secrets.INLINE_REDACTED, R)
        for command, shown in REDACTED:
            self.assertEqual(secrets.redact_inline(shown), shown)
        known = "MY_TOKEN=" + secrets.REDACTED + " ./run"
        self.assertEqual(secrets.redact_inline(known), known)

    def test_stays_fast_on_hostile_input(self):
        import time
        for text in ('MY_TOKEN="' * 410, "a=" * 2048, "--token " * 512, "Authorization: " * 300,
                     "-H x: " * 700, "curl;" * 820, "set -x " * 600, "MY_TOKEN=" + "\\" * 4000):
            start = time.perf_counter()
            secrets.redact_inline(text[:secrets.DISPLAY_SCAN])
            self.assertLess(time.perf_counter() - start, 0.5, text[:20])


class Labels(unittest.TestCase):
    """The shared label: the parent's rows, a Claude child's Bash rows and a
    Codex child's shell rows, and history derived from the log."""

    def setUp(self):
        self.root = _env_isolated(self)
        self.cwd = str(self.root / "project")
        os.mkdir(self.cwd)

    def test_label_withholds_each_typed_value(self):
        for command, shown in REDACTED:
            for tool, key in (("Bash", "command"), ("shell", "command"), ("shell", "cmd")):
                with self.subTest(command=command, tool=tool, key=key):
                    self.assertEqual(tool_calls.label(tool, {key: command}, self.cwd), shown[:119] + (
                        "…" if len(shown) > 120 else ""))

    def test_label_leaves_settings(self):
        from jarvis.v2.approvals import clean_line
        for command in UNTOUCHED:
            with self.subTest(command=command):
                # Backticks go in the one-line cleaning, after the judgment.
                self.assertEqual(tool_calls.label("Bash", {"command": command}, self.cwd),
                                 clean_line(command, 120))

    def test_child_rows_and_history(self):
        from jarvis.v2.subagents import SubagentRecords
        for command, shown in REDACTED:
            for sid, tool in (("cl:toolu_a", "Bash"), ("cx:item_a", "shell")):
                event = Event(K.SUBAGENT_EVENT, "t", {"sid": sid, "event": {"kind": "tool_started", "data": {
                    "call_id": "c1", "name": tool, "args": {"command": command}}}})
                with self.subTest(command=command, sid=sid):
                    self.assertEqual(SubagentRecords.prepare(event, self.cwd)["data"]["label"], shown)
            rows = [{"kind": "tool_started", "turn_id": "t", "data": {
                "call_id": "c1", "name": "Bash", "args": {"command": command}}}]
            with self.subTest(command=command, history=True):
                self.assertEqual(tool_calls.transcript_tools(rows, self.cwd, None)[0]["label"], shown)


class Gate(unittest.TestCase):
    """The judge reads the command as typed; only the log is redacted, and an
    approval card still carries the whole command."""

    def setUp(self):
        self.root = _env_isolated(self)
        cfg = self.root / "home" / ".config" / "jarvis"
        cfg.mkdir(parents=True)
        for name, value in {"ALLOWLIST_PATH": cfg / "allowlist.json", "MODELS_PATH": cfg / "models.json",
                            "PROVIDER_DEFAULTS_PATH": cfg / "provider_defaults.json",
                            "V2_DATA_DIR": self.root / "v2data", "V2_ALWAYS_ASK": cfg / "always-ask.json",
                            "DISCORD_GUILD_PATH": cfg / "discord_guild.json",
                            "ROUTING_PATH": cfg / "routing.json"}.items():
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def commands(self):
        return [c for c, _ in REDACTED] + UNTOUCHED + ["cat .env", "sudo rm -rf /", "ls -la"]

    def permit_results(self):
        from jarvis.v2.model import PermissionProfile as Profile, Role
        from jarvis.v2.permissions import PermitContext, build_permit
        from jarvis.v2.provider import Brief, Decision

        class Asker:
            human_backed = True

            def __init__(self):
                self.seen = []

            def ask(self, request):
                self.seen.append(request)
                return Decision.ALLOW

        out = []
        for profile in (Profile.ASK, Profile.AUTO, Profile.STRICT):
            brief = Brief(role=Role.IMPLEMENTER, cwd=str(self.root / "proc"), profile=profile)
            asker = Asker()
            permit = build_permit(PermitContext(thread_id="abcd1234", brief=brief, provider="claude"), asker)
            for command in self.commands():
                seen = len(asker.seen)
                decision = permit("Bash", {"command": command}, brief)
                asked = asker.seen[seen].command if len(asker.seen) > seen else None
                out.append((profile.value, command, decision, asked))
        path = config.V2_DATA_DIR / "decisions.jsonl"
        log = [json.loads(x) for x in path.read_text().splitlines()]
        path.unlink()
        return out, log

    def test_decisions_are_the_same_with_and_without_redaction(self):
        with_redaction, log = self.permit_results()
        with patch.object(secrets, "redact_inline", lambda text: text):
            without, _ = self.permit_results()
        self.assertEqual([r[:3] for r in with_redaction], [r[:3] for r in without])
        # The card is asked with the command exactly as typed.
        asked = [r for r in with_redaction if r[3] is not None]
        self.assertTrue(asked)
        for _, command, _, card in asked:
            self.assertEqual(card, command)
        # The durable log keeps the redacted line.
        logged = {entry["command"] for entry in log if entry.get("command")}
        for command, shown in REDACTED:
            self.assertNotIn(command, logged)
            self.assertIn(shown, logged)
        self.assertIn("PATH=/usr/local/bin:/usr/bin NODE_ENV=production CI=1 make", logged)

    def test_judges_never_read_through_the_redaction(self):
        def boom(*_a, **_k):
            raise AssertionError("a judge read through the display redaction")

        before = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision)
                  for c in self.commands()]
        from jarvis.v2 import permissions as v2_permissions
        v2_before = [v2_permissions.denied_command(c, str(self.root / "proc")) for c in self.commands()]
        with patch.object(secrets, "redact_inline", boom), patch.object(secrets, "for_display", boom):
            after = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision)
                     for c in self.commands()]
            v2_after = [v2_permissions.denied_command(c, str(self.root / "proc")) for c in self.commands()]
        self.assertEqual(before, after)
        self.assertEqual(v2_before, v2_after)


class V1Lines(unittest.TestCase):
    """v1's tool lines: the chat REPL, the face, a task's and a workflow's
    log, a goal's journal. They showed the raw JSON arguments unscrubbed."""

    def setUp(self):
        self.root = _env_isolated(self)

    def raw(self, command):
        return json.dumps({"command": command, "timeout": 30})

    def test_tool_args_for_display(self):
        for command, shown in REDACTED:
            with self.subTest(command=command):
                got = json.loads(secrets.tool_args_for_display(self.raw(command)))
                self.assertEqual(got, {"command": shown, "timeout": 30})
        for command in UNTOUCHED:
            got = json.loads(secrets.tool_args_for_display(self.raw(command)))
            self.assertEqual(got["command"], command)
        self.assertEqual(secrets.tool_args_for_display("MY_TOKEN=plainvalue1 x"), f"MY_TOKEN={R} x")
        with patch.object(secrets, "scrub", side_effect=RuntimeError):
            self.assertEqual(secrets.tool_args_for_display(self.raw("ls")), "[arguments withheld]")

    def fake_agent(self, raw):
        done = threading.Event()

        class Agent:
            def __init__(self, **kw):
                self.on_event = kw["on_event"]

            def run_turn(self, _text):
                self.on_event("tool_start", ("run_command", raw))
                done.set()
                return SimpleNamespace(text="", cost_usd=0.0, steps=1, cancelled=False)

        return Agent, done

    def wait_finished(self, item):
        import time
        deadline = time.monotonic() + 30
        while item.status == "running" and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertNotEqual(item.status, "running")

    def test_task_and_workflow_logs(self):
        from jarvis import tasks, workflows
        command, shown = REDACTED[0]
        for module in (tasks, workflows):
            Agent, done = self.fake_agent(self.raw(command))
            with patch("jarvis.agent.Agent", Agent), patch.object(workflows.agent_mod, "Agent", Agent):
                item = module.start("probe", name="probe")
                self.assertTrue(done.wait(30))
                self.wait_finished(item)
            log = "\n".join(item.log)
            with self.subTest(module=module.__name__):
                self.assertNotIn("hunter2plainvalue", log)
                self.assertIn("MY_API_TOKEN=" + R, log)

    def test_repl_face_and_goal_journal(self):
        import jarvis.__main__ as cli
        from jarvis.face import server
        from jarvis.goalrunner import GoalRunner
        command, _ = REDACTED[1]
        raw = self.raw(command)

        printed = []
        with patch.object(cli.agent_mod, "Agent", lambda **kw: SimpleNamespace(**kw)), \
                patch.object(cli.console, "print", lambda text, *a, **k: printed.append(text)):
            cli._make_agent(None).on_event("tool_start", ("run_command", raw))
        self.assertNotIn("p4ssw0rd", printed[0])
        self.assertIn("MY_PASSWORD=\\" + R, printed[0])   # escaped for rich markup

        sent = []
        with patch.object(server, "broadcast", lambda kind, data: sent.append((kind, data))):
            server._agent_event("tool_start", ("run_command", raw))
        self.assertEqual(sent[0][0], "tool")
        self.assertNotIn("p4ssw0rd", sent[0][1]["args"])
        self.assertIn("MY_PASSWORD=" + R, sent[0][1]["args"])

        journal = []
        goal = SimpleNamespace(journal=lambda kind, **f: journal.append(f))
        runner = SimpleNamespace(current=goal, _last_activity="", _check_live_spend=lambda _d: None,
                                 _maybe_update=lambda _g: None)
        GoalRunner._on_event(runner, "tool_start", ("run_command", raw))
        self.assertNotIn("p4ssw0rd", journal[0]["args"] + runner._last_activity)
        self.assertIn("MY_PASSWORD=" + R, journal[0]["args"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

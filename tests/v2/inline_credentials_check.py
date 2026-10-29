"""Credentials typed into a command: withheld wherever a command is shown or
kept, never where one is judged (2026-10-15). Free, hermetic, no live daemon.

The gap: a tool call's label, the v2 decision log and v1's tool lines scrubbed
the values they *knew* (env files) and the shapes they *knew*
(`credential_patterns`), so `export MY_API_TOKEN=hunter2plainvalue`,
`curl -H "Authorization: Bearer …"` or `curl -u user:pass` showed verbatim.
Since the owner's decision of 2026-10-15 approval cards and `log.jsonl` take
the same pass, and a withheld value must never conceal code that runs
(`NEVER_HIDDEN`). Every value below is a made-up placeholder; none is a real
credential.
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
     f"curl -H 'Cookie: {R}; other=value' https://example.invalid"),
    ("curl -u user:plainpass https://example.invalid", f"curl -u user:{R} https://example.invalid"),
    ("curl -uuser:plainpass https://example.invalid", f"curl -uuser:{R} https://example.invalid"),
    ("curl --user 'bob:plainpass' https://example.invalid", f"curl --user bob:{R} https://example.invalid"),
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
    # A value with a blank, operator, expansion, glob or leading `~` is never
    # withheld (delta review of #205): the accepted cost of a quoting model
    # that can be wrong.
    "MY_TOKEN='two words' ./run",
    "curl --user 'bob:two words' https://example.invalid",
    "MY_PASSWORD='a;b' ./run",
    "MY_TOKEN=~/plain ./run",
    "TOKEN=$'plain\\nvalue1' ./run",
    "API_KEY=abcd* ls",
    "rm -rf API_KEY=abcd{,/../../..}",
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
    "app --token-file /tmp/t --no-password production_db",
    "app --password --interactive run",       # an option is never the value
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


# -- review of #205 -----------------------------------------------------------

URL_CREDENTIALS = [
    ("git clone https://plaintok123@github.com/a/b", "git clone https://github.com/a/b"),
    ("git push https://x-access-token:plaintok123@github.com/a/b.git", "git push https://github.com/a/b.git"),
    ("curl https://user:plainpw123@host/x?page=2", "curl https://host/x?page=2"),
    ("psql postgresql://u:plainpw123@h/db", "psql postgresql://h/db"),
]

# Readability: none of these holds a typed credential. Byte for byte.
FALSE_POSITIVES = [
    "docker run --name mongo -p 27017:27017 mongo",
    "kubectl port-forward svc/mysql -p3306",
    "sshpass -f pwfile ssh -p 2222 host",
    "docker run --name redis-cli -a x img",
    "curl -H 'Authorization: Bearer \"$TOKEN\"' https://example.invalid",
    "sed -i 's/TOKEN=.*/TOKEN=X/' settings.example",
    "sed -i 's/API_TOKEN=abcd/API_TOKEN=efgh/' settings.example",
    "app --key ./client-key.pem",
    "PASSWORD_FILE=/run/secrets/x ./run",
    "app --token-file=/tmp/t",
    "PASSWORD_FILE=secret.txt app --token-file=token.txt",
    "curl -b cookies.txt https://example.invalid",
    "SESSION_DRIVER=redis COOKIE_SECURE=true ./run",
    # A program to run is not a secret.
    "GIT_ASKPASS=evilhelper git push",
    "SSH_ASKPASS=askhelper ssh host",
    "SUDO_ASKPASS=askhelper sudo -A true",
    "BORG_PASSCOMMAND=getpassword borg list",
    "CREDENTIAL_HELPER=storehelper git fetch",
    "aws configure set credential_process getcredentials",
]

# Common lines whose values rule 3 must not show.
STILL_REDACTED = [
    ("export MY_TOKEN=plainvalue1 && find . -name x", f"export MY_TOKEN={R} && find . -name x"),
    ("export MY_TOKEN=plainvalue1 && curl -H \"Authorization: Bearer $MY_TOKEN\" https://example.invalid",
     f"export MY_TOKEN={R} && curl -H \"Authorization: Bearer $MY_TOKEN\" https://example.invalid"),
    ("env MY_SECRET=plainsecretvalue sudo ./run", f"env MY_SECRET={R} sudo ./run"),
    ("export MY_TOKEN=plainvalue1 && curl -H \"Authorization: Bearer $MY_TOKEN\" -o /tmp/out https://x",
     f"export MY_TOKEN={R} && curl -H \"Authorization: Bearer $MY_TOKEN\" -o /tmp/out https://x"),
]

CHEAP_CATCHES = [
    ("TOKEN=$'plainvalue1'", f"TOKEN={R}"),
    ("docker login -u bob -p plainpass registry", f"docker login -u bob -p {R} registry"),
    ("podman login -p plainpass registry", f"podman login -p {R} registry"),
    ("curl -b 'session=plainv' https://example.invalid", f"curl -b {R} https://example.invalid"),
    ("curl --cookie session=plainv https://example.invalid", f"curl --cookie {R} https://example.invalid"),
    ("gh secret set X --body plainvalue", f"gh secret set X --body {R}"),
    ("setx API_KEY plainvalue", f"setx API_KEY {R}"),
    ("npm config set //registry.npmjs.org/:_authToken plainvalue",
     f"npm config set //registry.npmjs.org/:_authToken {R}"),
    ("SESSION_ID=a1b2c3d4e5 ./run", f"SESSION_ID={R} ./run"),
    ("sudo MYSQL_HOME=/x mysql -u root -pPlainPass db", f"sudo MYSQL_HOME=/x mysql -u root -p{R} db"),
]


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self.root = _env_isolated(self)

    def test_url_userinfo_leaves_every_display_and_log(self):
        from jarvis.v2.permissions import _safe_command
        for command, shown in URL_CREDENTIALS:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)
                self.assertEqual(secrets.for_display(command), shown)
                self.assertEqual(_safe_command(command), shown)
                self.assertNotIn("plain", secrets.tool_args_for_display(json.dumps({"command": command})))
                # The label keeps its own rule: the query goes too.
                self.assertEqual(tool_calls.label("Bash", {"command": command}, ""),
                                 shown.replace("?page=2", ""))

    def test_false_positives_stay_byte_identical(self):
        from jarvis.v2.approvals import clean_line
        for command in FALSE_POSITIVES:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_inline(command), command)
                self.assertEqual(secrets.redact_command(command), command)
                self.assertEqual(tool_calls.label("Bash", {"command": command}, ""), clean_line(command, 120))

    def test_known_value_inside_a_url(self):
        # URLs go first, as in the label: the scrub's marker inside a URL
        # would break it apart.
        (self.root / "proc" / ".env").write_text("DB_PASSWORD=knownsecretvalue9\n")
        for shown in (secrets.for_display("psql postgresql://u:knownsecretvalue9@h/db"),
                      secrets.redact_command("psql postgresql://u:knownsecretvalue9@h/db")):
            self.assertEqual(shown, "psql postgresql://h/db")

    def test_common_lines_stay_redacted(self):
        for command, shown in STILL_REDACTED:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)

    def test_cheap_catches(self):
        for command, shown in CHEAP_CATCHES:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)

    def test_setting_names_follow_the_env_rule(self):
        for name in ("SESSION_ID", "AUTH", "COOKIE_VALUE", "X-Auth-Token"):
            self.assertTrue(secrets.credential_name(name), name)
        self.assertEqual(secrets.redact_inline("SESSION_DRIVER=redis AUTH_TYPE=basic ./x"),
                         "SESSION_DRIVER=redis AUTH_TYPE=basic ./x")

    def test_decision_log_reason_and_json_keys(self):
        from jarvis.v2.permissions import ApprovalRecord, log_decision
        path = self.root / "decisions.jsonl"
        log_decision(ApprovalRecord(tool="Bash", args_digest="d", layer="ask", decision="allow",
                                    reason="rule x matched: MY_TOKEN=plainvalue1 ./run",
                                    command="MY_TOKEN=plainvalue1 ./run"), path)
        entry = json.loads(path.read_text())
        self.assertEqual(entry["reason"], f"rule x matched: MY_TOKEN={R} ./run")
        self.assertNotIn("plainvalue1", path.read_text())
        got = json.loads(secrets.tool_args_for_display(json.dumps({"password": "plainpass", "pin": 48291357,
                                                                    "user": "bob"})))
        self.assertEqual(got, {"password": R, "pin": R, "user": "bob"})
        self.assertEqual(secrets.redact_value({"password": "plainpass", "n": [{"api_key": "plainvalue1"}], "ok": True}),
                         {"password": R, "n": [{"api_key": R}], "ok": True})


# -- the owner's decision, 2026-10-15: cards and raw logs ------------------------

# Each line runs the value or hides structure; the card must show what runs.
# (command, a value the line may run is marked, what must show). A value with
# a blank is never withheld at all, so rule 3's cases use one without.
NEVER_HIDDEN = [
    ("X_TOKEN='rmrf~'; eval $X_TOKEN", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; eval \"$X_TOKEN\"", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; bash -c \"$X_TOKEN\"", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; zsh -lc \"$X_TOKEN\"", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; $X_TOKEN", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; ${X_TOKEN}", True, "rmrf~"),
    ("export X_TOKEN='rmrf~' && sudo $X_TOKEN", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; source <(echo $X_TOKEN)", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; . <(echo $X_TOKEN)", True, "rmrf~"),
    ("X_TOKEN='import os; os.system(\"rm -rf ~\")'; python3 -c \"$X_TOKEN\"", False),
    ("X_TOKEN=rmrfhome python3 -c 'import os; os.system(os.environ[\"X_TOKEN\"])'", True, "rmrfhome"),
    ("X_TOKEN='rmrf~'; perl -e \"system('$X_TOKEN')\"", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; echo \"$X_TOKEN\" | sh", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; echo \"$X_TOKEN\" | /bin/bash", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; echo $($X_TOKEN)", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; echo `$X_TOKEN`", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; eval '$'X_TOKEN", True, "rmrf~"),
    ("R=X_TOKEN; X_TOKEN='rmrf~'; eval ${!R}", True, "rmrf~"),
    ("X_TOKEN='rmrf~' bash -c '$X_TOKEN'", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; bash <<< \"$X_TOKEN\"", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; printf %s \"$X_TOKEN\" > /tmp/a.sh; sh /tmp/a.sh", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; printf %s \"$X_TOKEN\" | xargs -I{} sh -c {}", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; printf %s \"$X_TOKEN\" | tee run.sh", True, "rmrf~"),
    ("X_TOKEN='rmrf~'; cmd /c %X_TOKEN%", True, "rmrf~"),
    ("X_TOKEN='rmrf~' python3 -c 'import os; os.system(os.environ[\"X_TOKEN\"])'", True, "rmrf~"),
    # Quoting: what looks like a value is not one in the line's own parse.
    ("echo 'MY_TOKEN=\"abc' ; rm -rf ~ ; echo '\"'", False),
    ("echo \"MY_TOKEN='$(rm -rf ~)'\"", False),
    ("curl https://h/x?a;rm -rf ~", False),
    ("curl https://u:p@h/x?a&rm -rf ~", False),
    ("curl -H Authorization:abc;rm -rf ~", False),
    ("curl -H Cookie:a=1;rm -rf ~", False),
    ("curl -u 'u:a' ; rm -rf ~ ; echo ':b' https://x", False),
    ("MY_TOKEN=abc;rm -rf ~", False),
    # Delta review of #205: a `'` in a comment or a here-document desynced
    # the quoting model, and the value swallowed a command.
    ("# it's fine\necho 'x API_KEY='abcd; rm -rf ~; echo 'y' # it's", False),
    ("cat <<'EOF' >/dev/null\nit's\nEOF\necho 'x API_KEY='abcd; rm -rf ~; echo 'y'", False),
    ("# it's\necho '{\"password\": \"'abc; rm -rf ~; '\"}' # '", False),
    ("echo \"$(echo 'a\"b')\" API_KEY='abcd;rm -rf ~'", False),
    ("echo `echo #'` ; API_KEY='x;rm -rf ~' ok", False),
    ("rm -rf API_KEY=abcd{,/../../..}", False, "{,/../../..}"),
    ("API_KEY=abcd* ls", False, "abcd*"),
    # Second delta review: a malformed URL was withheld whole past the
    # character rule, hiding a brace expansion.
    ("ls http://u:p@[a]/{x,y}", False, "{x,y}"),
    ("ls http://u:p@[a]/*", False, "[a]/*"),
    # Rule 3 additions.
    ("X_TOKEN='rmrf~'; trap \"$X_TOKEN\" EXIT", True, "rmrf~"),
    ("X_TOKEN='rmrf'; watch \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; ssh host \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; autossh host \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; su -c \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; runuser -c \"$X_TOKEN\" bob", True, "rmrf"),
    ("X_TOKEN='rmrf'; script -c \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; parallel ::: \"$X_TOKEN\"", True, "rmrf"),
    ("X_TOKEN='rmrf'; find /tmp -exec $X_TOKEN {} +", True, "rmrf"),
    ("X_TOKEN='rmrf'; . ./env.sh", True, "rmrf"),
    ("X_TOKEN='rmrf'; printf %s \"$X_TOKEN\" | tee a.sh; ./a.sh", True, "rmrf"),
    ("X_TOKEN='rmrf'; echo \"$X_TOKEN\" > a.sh; /tmp/a.sh", True, "rmrf"),
    ("alias X_TOKEN='rmrf'; X_TOKEN", True, "rmrf"),
    ("$env:X_TOKEN='rmrf'; & $env:X_TOKEN", True, "rmrf"),
    ("$env:X_TOKEN='rmrf'; Start-Process $env:X_TOKEN", True, "rmrf"),
]


class Cards(unittest.TestCase):
    def setUp(self):
        self.root = _env_isolated(self)
        cfg = self.root / "home" / ".config" / "jarvis"
        cfg.mkdir(parents=True)
        p = patch.object(config, "ALLOWLIST_PATH", cfg / "allowlist.json")
        p.start()
        self.addCleanup(p.stop)

    def test_a_withheld_value_never_hides_code_that_runs(self):
        from jarvis.v2.approvals import ApprovalRequest, clean_line
        for command, marked, *shows in NEVER_HIDDEN:
            shows = shows[0] if shows else "rm -rf ~"
            with self.subTest(command=command):
                card = secrets.redact_command(command)
                self.assertIn(shows, card)
                self.assertEqual(card.count(secrets.RUN_MARKER), 1 if marked else 0)
                if marked:
                    self.assertTrue(card.startswith(command.split("=", 1)[0]))
                self.assertNotIn(R + R, card)
                shown = ApprovalRequest(tool="Bash", args={"command": command}, command=command).to_json()
                self.assertEqual(shown["command"], card)
                self.assertEqual(shown["args"]["command"], card)
                self.assertEqual(secrets.redact_value({"command": command}), {"command": card})
                if not marked:
                    # Nothing withheld at all when the line only looks like
                    # values; and the label (which also drops a URL's
                    # query) never hides structure either.
                    self.assertIn(clean_line(shows, 120), tool_calls.label("Bash", {"command": command}, ""))

    def test_structure_is_never_withheld(self):
        # Rule 1, directly: a value's span with an unquoted operator, blank,
        # `$` or backtick, or one that ends in other quoting, is kept.
        mask = secrets._Mask("echo 'A=\"b' ; rm ; echo '\"'")
        self.assertFalse(mask.safe(8, 25))
        mask = secrets._Mask("A='b c' x")
        self.assertTrue(mask.safe(2, 7, value=False))      # quoting alone: fine
        self.assertFalse(mask.safe(2, 7))                   # a value with a blank: never
        self.assertFalse(mask.safe(2, 9, value=False))
        self.assertTrue(secrets._Mask("A='bc' x").safe(2, 6))
        mask = secrets._Mask('echo "A=$(rm)"')
        self.assertFalse(mask.safe(8, 13))
        # No active character, but the span ends inside other quoting.
        self.assertFalse(secrets._Mask("a'b'c").safe(1, 3))
        self.assertTrue(secrets._Mask("a'b'c").safe(1, 4))
        # The character rule holds even where the quoting looks right.
        self.assertFalse(secrets._Mask("A='b;c'").safe(2, 7))
        self.assertFalse(secrets._Mask("A=~b").safe(2, 4))
        self.assertTrue(secrets._Mask("A=b;c").safe(2, 3))
        # Fail closed: a comment to its line's end, everything after a
        # here-document, a backtick, or a `$(` inside double quotes.
        text = "# it's x\nA=bcd"
        mask = secrets._Mask(text)
        self.assertFalse(mask.safe(2, 4))
        self.assertTrue(mask.safe(text.index("bcd"), len(text)))
        for text in ("cat <<E\nx\nE\nA=bcd", "echo `x` A=bcd", 'echo "$(x)" A=bcd'):
            self.assertFalse(secrets._Mask(text).safe(text.index("bcd"), len(text)), text)
        text = "echo a#b A=bcd"                 # not a word-start `#`
        self.assertTrue(secrets._Mask(text).safe(text.index("bcd"), len(text)))

    def request(self, command):
        from jarvis.v2.approvals import ApprovalRequest
        return ApprovalRequest(tool="Bash", args={"command": command, "description": "MY_TOKEN=plainvalue1"},
                               command=command, reason=f"always-ask: {command}",
                               headline="MY_TOKEN=plainvalue1")

    def test_card_shows_redacted_request_keeps_typed(self):
        command = "MY_TOKEN=plainvalue1 curl -H 'Authorization: Bearer plainbearer9' https://example.invalid"
        shown = f"MY_TOKEN={R} curl -H 'Authorization: Bearer {R}' https://example.invalid"
        request = self.request(command)
        for card in (request.to_json(), request.owner_json()):
            self.assertEqual(card["command"], shown)
            self.assertEqual(card["args"]["command"], shown)
            self.assertEqual(card["reason"], "always-ask: " + shown)
            self.assertEqual(card["headline"], f"MY_TOKEN={R}")
            self.assertNotIn("plain", json.dumps(card))
        self.assertEqual(request.command, command)
        self.assertEqual(request.args["command"], command)

    def test_the_escape_hatch_card_shows_what_bash_will_run(self):
        # The hatch asks with this card, then runs the raw command with
        # `bash -c` (hatch.py): the card must show every command in it.
        from jarvis.v2.approvals import ApprovalRequest
        for command, _marked, *shows in NEVER_HIDDEN:
            shows = shows[0] if shows else "rm -rf ~"
            card = ApprovalRequest(tool="Bash", args={"command": command}, command=command,
                                   allowlistable=False, layer="escape-hatch").to_json()
            self.assertIn(shows, card["command"], command)

    def test_card_projection_is_computed_once(self):
        request = self.request("MY_TOKEN=plainvalue1 ./run")
        with patch.object(secrets, "redact_command", wraps=secrets.redact_command) as judge:
            first = request.to_json()
            for _ in range(5):
                self.assertEqual(request.to_json(), first)
                request.owner_json()
        self.assertEqual(judge.call_count, 5)     # command, two args, reason, headline

    def test_card_redaction_failure_shows_the_command(self):
        command = "MY_TOKEN=plainvalue1 ./run"
        with patch.object(secrets, "redact_command", side_effect=RuntimeError), \
                patch.object(secrets, "redact_value", side_effect=RuntimeError):
            card = self.request(command).to_json()
        self.assertEqual(card["command"], command)

    def test_broker_publishes_the_card_and_always_mints_from_the_typed_line(self):
        from jarvis.v2.approvals import PendingApprovals
        from jarvis.v2.provider import Decision
        published = []
        broker = PendingApprovals(on_request=lambda r: published.append(r.to_json()))
        command = "MY_TOKEN=plainvalue1 npm test"
        request = self.request(command)
        result = []
        worker = threading.Thread(target=lambda: result.append(broker.ask(request)))
        worker.start()
        try:
            for _ in range(3000):
                if published:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(published[0]["command"], f"MY_TOKEN={R} npm test")
            self.assertEqual(broker.pending()[0].command, command)
            minted = []
            real_add = v1_permissions.add_allow
            with patch.object(v1_permissions, "add_allow",
                              side_effect=lambda tool, args: minted.append(args) or real_add(tool, args)):
                entry = broker.resolve(request.req_id, Decision.ALLOW, always=True)
            self.assertEqual(minted, [{"command": command}])
        finally:
            worker.join(10)
        self.assertEqual(result, [Decision.ALLOW])
        self.assertEqual(entry, v1_permissions.entry_for("run_command", {"command": command}))
        self.assertNotIn("redacted", config.ALLOWLIST_PATH.read_text())

    def test_discord_dm_shows_the_card(self):
        from jarvis.v2.approvals import PendingApprovals
        from jarvis.v2.discord.gateway import DiscordRouter
        broker = PendingApprovals()
        command = "MY_TOKEN=plainvalue1 npm test"
        request = self.request(command)
        worker = threading.Thread(target=lambda: broker.ask(request))
        worker.start()
        try:
            for _ in range(3000):
                if broker.pending():
                    break
                threading.Event().wait(0.01)
            router = object.__new__(DiscordRouter)
            router._lock = threading.Lock()
            router.approvals, router.mirror, router.daemon = broker, None, None
            router._approval_channels, router._approval_messages = {request.req_id: "dm"}, {}
            bodies = []
            router._announce = lambda *_: None
            router._owner_dm = lambda: "dm"
            router._ask_in = lambda req_id, channel, body, code, **_: bodies.append(body) or ("dm", "m1")
            router._save_posts = lambda: None
            router._is_dm = lambda _c: True
            router._post_approval(request.to_json())
        finally:
            broker.resolve(request.req_id, "deny")
            worker.join(10)
        text = bodies[0] if isinstance(bodies[0], str) else json.dumps(bodies[0], default=str)
        self.assertNotIn("plainvalue1", text)
        self.assertIn("MY_TOKEN=" + R, text)
        self.assertIn("`/always ", text)            # judged on the typed request
        # Not pending any more (answered meanwhile): never "always" from the card.
        bodies.clear()
        router._approval_channels = {request.req_id: "dm"}
        router._post_approval(request.to_json())
        late = bodies[0] if isinstance(bodies[0], str) else json.dumps(bodies[0], default=str)
        self.assertNotIn("`/always ", late)

    def test_v1_cards(self):
        from jarvis.discord_approvals import _describe
        from jarvis.face.approvals import _for_display
        args = {"command": "MY_TOKEN=plainvalue1 ./run", "password": "plainpass"}
        self.assertEqual(_for_display(args), {"command": f"MY_TOKEN={R} ./run", "password": R})
        described = _describe(SimpleNamespace(args=args))
        self.assertNotIn("plain", described)
        self.assertIn(f"MY_TOKEN={R} ./run", described)
        for command, _marked, *shows in NEVER_HIDDEN:
            shows = shows[0] if shows else "rm -rf ~"
            run = {"command": command}
            self.assertIn(shows, _for_display(run)["command"], command)
            self.assertIn(shows, _describe(SimpleNamespace(args=run)), command)


class RawLogs(unittest.TestCase):
    """`log.jsonl` keeps typed credentials withheld; the bus keeps the event
    as it came (the hatch runs a declined command from it)."""

    def setUp(self):
        import hud_backend_check as backend
        backend.Backend.setUp(self)
        self.backend = backend

    def thread(self):
        return self.backend.Backend.thread(self)

    def events_all(self):
        return self.backend.Backend.events_all(self)

    def test_log_is_redacted_and_the_bus_is_not(self):
        thread = self.thread()
        session = self.daemon._sessions[thread.id]
        session.turn_id = "turn"
        command = "MY_TOKEN=plainvalue1 curl -u bob:plainpw https://x-access-token:plaintok@h/x"
        shown = f"MY_TOKEN={R} curl -u bob:{R} https://h/x"
        events = [
            Event(K.TOOL_STARTED, thread.id, {"call_id": "c1", "name": "Bash", "args": {"command": command}}),
            Event(K.TOOL_FINISHED, thread.id, {"call_id": "c1", "name": "Bash", "ok": True, "outcome": "ok",
                                               "summary": "done", "args": {"command": command}}),
            Event(K.APPROVAL_REQUESTED, thread.id, {"req_id": "g1", "tool": "Bash", "args": {"command": command},
                                                    "command": command}),
            Event(K.REVIEWER_DECLINED, thread.id, {"tool": "Bash", "args": {"command": command},
                                                   "command": command, "reason": "declined: MY_TOKEN=plainvalue1"}),
        ]
        for event in events:
            self.daemon._record(session, event)
        log = self.stores.threads.read_log(thread.id)
        logged = [r for r in log if r.get("kind") in ("tool_started", "tool_finished", "gate_requested",
                                                     "reviewer_declined")]
        self.assertEqual(len(logged), 4)
        text = json.dumps(logged)
        self.assertNotIn("plain", text)
        for row in logged:
            self.assertEqual(row["data"]["args"]["command"], shown)
        self.assertEqual(logged[2]["data"]["command"], shown)
        self.assertEqual(logged[3]["data"]["reason"], f"declined: MY_TOKEN={R}")
        self.assertEqual(logged[0]["data"]["label"], f"MY_TOKEN={R} curl -u bob:{R} https://h/x")
        bus = {r["kind"]: r for r in self.events_all()}
        self.assertEqual(bus["reviewer_declined"]["data"]["command"], command)
        self.assertEqual(bus["tool_started"]["data"]["args"]["command"], command)
        # History reads the redacted record and stays whole.
        from jarvis.v2.tool_calls import transcript_tools
        rows = transcript_tools(log, None, None)
        self.assertEqual(next(iter(rows.values()))["label"], logged[0]["data"]["label"])

    def test_resolution_record_shows_the_card(self):
        from jarvis.v2.approvals import ApprovalRequest
        from jarvis.v2.provider import Decision
        request = ApprovalRequest(tool="Bash", args={"command": "MY_TOKEN=plainvalue1 ./x"},
                                  command="MY_TOKEN=plainvalue1 ./x", thread_id="t", req_id="r", code="abcd")
        self.daemon._approval_resolved(request, Decision.DENY, "deny")
        resolved = [r for r in self.events_all() if r["kind"] == "approval_resolved"][-1]
        self.assertEqual(resolved["data"]["command"], f"MY_TOKEN={R} ./x")
        self.assertEqual(request.command, "MY_TOKEN=plainvalue1 ./x")

    def test_browser_actions_keep_their_empty_args(self):
        thread = self.thread()
        session = self.daemon._sessions[thread.id]
        session.turn_id = "turn"
        self.daemon._record(session, Event(K.TOOL_STARTED, thread.id, {
            "call_id": "b1", "name": "hud_browser_act", "args": {"text": "MY_TOKEN=plainvalue1 private page"}}))
        row = self.stores.threads.read_log(thread.id)[-1]
        self.assertEqual(row["data"]["args"], {})

    def test_a_failed_redaction_withholds_the_field(self):
        thread = self.thread()
        session = self.daemon._sessions[thread.id]
        session.turn_id = "turn"
        with patch.object(secrets, "redact_value", side_effect=RuntimeError):
            self.daemon._record(session, Event(K.TOOL_STARTED, thread.id, {
                "call_id": "c2", "name": "Bash", "args": {"command": "MY_TOKEN=plainvalue1 ./x"}}))
        row = self.stores.threads.read_log(thread.id)[-1]
        self.assertEqual(row["data"]["args"], "[withheld: could not be judged]")



# -- SEC-6 (owner, 2026-10-19): a private key's whole block -------------------
#
# `redact_command` (cards, logs) and `for_display` (v1's tool lines) applied
# `credential_patterns.PATTERNS`, which know only a key's header and footer;
# the base64 body between them showed (and on a card, rule 0 kept even the
# header: it holds a blank). Both now take `redact.private_key_blocks` first.
# On a card that is still bound by rules 1-3: a block goes only where it sits
# inside one quoting with nothing active, holds nothing but key material, and
# the line runs no text. Every body is seeded random base64 built here.

import base64 as _b64  # noqa: E402
import random as _random  # noqa: E402

_RNG = _random.Random(20261019)


def _body(lines: int = 4, width: int = 64) -> list[str]:
    text = _b64.b64encode(_RNG.randbytes(width * 3 // 4 * lines + 7)).decode()
    return [text[i:i + width] for i in range(0, len(text), width)]


def _head(label: str = "PRIVATE KEY") -> str:
    return "-----BEGIN " + label + "-----"


def _foot(label: str = "PRIVATE KEY") -> str:
    return "-----END " + label + "-----"


def _block(label: str = "PRIVATE KEY", sep: str = "\n", extra: tuple = ()) -> str:
    return sep.join([_head(label), *extra, *_body(), _foot(label)])


_LABELS = ("PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY",
           "ENCRYPTED PRIVATE KEY", "PGP PRIVATE KEY BLOCK")
_LEGACY = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF", "")


def _key_cards():
    """(command, what its card and log must read)."""
    cases = []
    for label in _LABELS:
        block = _block(label)
        cases.append((f"printf '%s\\n' '{block}' > key.pem", "printf '%s\\n' '[redacted]' > key.pem"))
    block = _block("RSA PRIVATE KEY", extra=_LEGACY)
    cases.append((f"echo '{block}' | tee key.pem", "echo '[redacted]' | tee key.pem"))
    block = _block(sep="\\n")
    cases += [
        # JSON's and ANSI-C's `\n`, and double quotes.
        (f"curl -d '{{\"name\": \"deploy\", \"key\": \"{block}\\n\"}}' https://example.invalid/keys",
         "curl -d '{\"name\": \"deploy\", \"key\": \"[redacted]\\n\"}' https://example.invalid/keys"),
        (f"printf %s $'{block}' | ssh-add -", "printf %s $'[redacted]' | ssh-add -"),
        (f"gh secret set DEPLOY --body \"{_block()}\"", "gh secret set DEPLOY --body \"[redacted]\""),
        # An unterminated block: to its quote's end, never past it.
        (f"printf %s '{_head()}\n" + "\n".join(_body()) + "' > key.pem", "printf %s '[redacted]' > key.pem"),
        # A footer-only tail: the key material above the footer, in its quote.
        ("printf %s '" + "\n".join(_body()) + f"\n{_foot()}' > key.pem", "printf %s '[redacted]' > key.pem"),
        # Two blocks in two quotes stay two.
        (f"echo '{_block()}' > a.pem && echo '{_block('EC PRIVATE KEY')}' > b.pem",
         "echo '[redacted]' > a.pem && echo '[redacted]' > b.pem"),
    ]
    return cases


def _key_lines():
    """(tool line, what `for_display` must read)."""
    before, after = "Here is the key you asked about:\n", "\nCan you tell me why ssh refuses it?"
    cases = [(before + _block(label) + after, before + R + after) for label in _LABELS]
    cases += [
        (before + _block("RSA PRIVATE KEY", extra=_LEGACY) + after, before + R + after),
        (before + _head() + "\n" + "\n".join(_body()), before + R),           # unterminated
        ("\n".join(_body()) + "\n" + _foot() + after, R + after),             # a footer-only tail
        ("echo '" + _block() + "' > key.pem", "echo '" + R + "' > key.pem"),
    ]
    return cases


def _public_material():
    cert = "\n".join(["-----BEGIN CERTIFICATE-----", *_body(), "-----END CERTIFICATE-----"])
    pub = "\n".join(["-----BEGIN PUBLIC KEY-----", *_body(), "-----END PUBLIC KEY-----"])
    ssh = "ssh-ed25519 " + _body(1)[0] + " me@laptop"
    pgp = "\n".join(["-----BEGIN PGP PUBLIC KEY BLOCK-----", "", *_body(), "-----END PGP PUBLIC KEY BLOCK-----"])
    return [cert, pub, ssh, pgp, f"echo '{cert}' > ca.pem", f"echo '{pub}' >> keys.pem",
            f"echo '{ssh}' >> ~/.ssh/authorized_keys"]


def _key_never_hidden():
    """(command, what its card must still show[, the card when not the
    command as typed]): rules 1-3 hold for a block."""
    body = "\n".join(_body())
    block = f"{_head()}\n{body}\n{_foot()}"
    return [
        # Rule 1: a header and a footer in two quotes never take what is
        # between them; an unquoted block and a here-document stay whole.
        # (A header alone in its quote is an unterminated block; the footer
        # alone has no key material above it, so only the per-pattern pass
        # sees it, and rule 0 keeps it.)
        (f"echo '{_head()}'; rm -rf ~; echo '{_foot()}'", "rm -rf ~",
         f"echo '[redacted]'; rm -rf ~; echo '{_foot()}'"),
        (f"{_head()}\n{body}\n{_foot()}", body),
        (f"cat > key.pem <<'EOF'\n{block}\nEOF", body),
        (f"echo '{_head()}\nrm -rf ~\n{_foot()}' > x", "rm -rf ~"),        # not key material
        (f"echo \"$KEYTAIL\n{body}\n{_foot()}\"", "$KEYTAIL"),          # an expansion's name stays
        # Rule 3: a line that runs text shows it.
        (f"bash -c '{_head()}\n{body}\n{_foot()}'", body),
        (f"X='{block}'; eval \"$X\"", body),
        (f"K='{block}'; echo \"$K\" > k.pem", body),
        (f"printf %s '{block}' > a.sh; sh a.sh", body),
        (f"printf %s '{block}' > a.sh; ./a.sh", body),
        (f"printf %s '{block}' | python3", body),
    ]


class PrivateKeyBlocks(unittest.TestCase):
    def setUp(self):
        self.root = _env_isolated(self)

    def test_a_card_and_a_log_hide_the_whole_block(self):
        from jarvis.v2.approvals import ApprovalRequest
        from jarvis.v2.permissions import ApprovalRecord, _safe_command, log_decision
        for command, shown in _key_cards():
            with self.subTest(command=command[:60]):
                self.assertEqual(secrets.redact_command(command), shown)
                self.assertEqual(secrets.redact_command(shown), shown)              # idempotent
                self.assertEqual(secrets.redact_value({"command": command, "n": [command]}),
                                 {"command": shown, "n": [shown]})
                self.assertEqual(_safe_command(command), shown)
                request = ApprovalRequest(tool="Bash", args={"command": command}, command=command)
                self.assertEqual(request.to_json()["command"], shown)
                self.assertEqual(request.to_json()["args"]["command"], shown)
                self.assertEqual(request.command, command)                         # judged as typed
        path = self.root / "decisions.jsonl"
        command, shown = _key_cards()[0]
        log_decision(ApprovalRecord(tool="Bash", args_digest="d", layer="ask", decision="allow",
                                    reason=f"rule x: {command}", command=command), path)
        entry = json.loads(path.read_text())
        self.assertEqual((entry["command"], entry["reason"]), (shown, f"rule x: {shown}"))

    def test_a_v1_tool_line_hides_the_whole_block(self):
        for line, shown in _key_lines():
            with self.subTest(line=line[:60]):
                self.assertEqual(secrets.for_display(line), shown)
                got = json.loads(secrets.tool_args_for_display(json.dumps({"path": "k.pem", "content": line})))
                self.assertEqual(got, {"path": "k.pem", "content": shown})
                self.assertEqual(secrets.tool_args_for_display(line), shown)        # not JSON: judged whole

    def test_public_keys_and_certificates_are_unchanged(self):
        for text in _public_material():
            with self.subTest(text=text[:40]):
                self.assertEqual(secrets.redact_command(text), text)
                self.assertEqual(secrets.for_display(text), text)

    def test_a_block_never_hides_code_that_runs(self):
        from jarvis.v2.approvals import ApprovalRequest
        for command, shows, *card in _key_never_hidden():
            with self.subTest(command=command[:60]):
                card = card[0] if card else command      # as before SEC-6: nothing withheld
                self.assertEqual(secrets.redact_command(command), card)
                self.assertIn(shows, card)
                hatch = ApprovalRequest(tool="Bash", args={"command": command}, command=command,
                                        allowlistable=False, layer="escape-hatch").to_json()
                self.assertIn(shows, hatch["command"])

    def test_stays_linear_on_hostile_key_text(self):
        # About 1 MiB each, in a child with an active deadline (a hang cannot
        # be interrupted from its own thread). Measured 2026-10-19: at most
        # 0.9 CPU-seconds a case for `redact_command`, 0.002 for `for_display`;
        # the old `redact_command` took 0.6 on the unquoted case.
        import subprocess
        import sys
        script = """
import sys, time
from jarvis.tools import secrets
H, F = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
line = "QUJD" * 16 + "\\n"
cases = [("'" + H + "' ") * 30000,
         ("'" + H + "\\nQUJD\\n" + F + "' ") * 20000,
         "echo '" + H + "\\n" + line * 16000 + F + "' > k",
         "echo '" + H + "\\n" + line * 16000,
         ("'QUJD\\n" + F + "' ") * 30000,
         (H + "\\n" + line) * 16000,
         "'" + H + "$" * 1000000 + "'"]
worst = 0.0
for text in cases:
    start = time.process_time()
    secrets.redact_command(text)
    secrets.for_display(text)
    worst = max(worst, time.process_time() - start)
print(worst)
"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=240, env=env, check=True)
        self.assertLess(float(done.stdout.strip()), 5.0, done.stdout)

    def test_judges_never_read_the_new_pass(self):
        def boom(*_a, **_k):
            raise AssertionError("a judge read through the display redaction")

        commands = [c for c, _ in _key_cards()] + [c for c, *_ in _key_never_hidden()]
        before = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision) for c in commands]
        with patch.object(secrets, "redact_command", boom), patch.object(secrets, "for_display", boom), \
                patch.object(secrets, "redact_value", boom):
            after = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision) for c in commands]
        self.assertEqual(before, after)


# -- review of #330, LOW 5: the same family, tighten only --------------------

FAMILY = [
    ("htpasswd -b .htpasswd admin adminpass99", f"htpasswd -b .htpasswd admin {R}"),
    ("htpasswd -nb admin adminpass99", f"htpasswd -nb admin {R}"),
    ("htpasswd -bc .htpasswd admin adminpass99", f"htpasswd -bc .htpasswd admin {R}"),
    ("htpasswd -cb .htpasswd admin adminpass99", f"htpasswd -cb .htpasswd admin {R}"),
    ("htpasswd -b -c .htpasswd admin adminpass99", f"htpasswd -b -c .htpasswd admin {R}"),
    ("htpasswd -B -b .htpasswd admin adminpass99", f"htpasswd -B -b .htpasswd admin {R}"),
    ("htpasswd -nbB admin adminpass99", f"htpasswd -nbB admin {R}"),
    ("smbclient //srv/share -U alice%plainpass9", f"smbclient //srv/share -U alice%{R}"),
    ("smbclient -U alice%plainpass9 //srv/share", f"smbclient -U alice%{R} //srv/share"),
    ("rpcclient --user=alice%plainpass9 srv", f"rpcclient --user=alice%{R} srv"),
    ("net ads join -U admin%plainpass9", f"net ads join -U admin%{R}"),
    ("lftp -u alice,plainpass9 ftp://h", f"lftp -u alice,{R} ftp://h"),
    ("sqlcmd -S srv -U sa -P plainpass9", f"sqlcmd -S srv -U sa -P {R}"),
    ("ldapsearch -x -D cn=admin -w plainpass9 -b dc=x", f"ldapsearch -x -D cn=admin -w {R} -b dc=x"),
    ("openssl pkcs12 -passin pass:plainpass9 -in a.p12", f"openssl pkcs12 -passin pass:{R} -in a.p12"),
    ("openssl enc -aes256 -pass pass:plainpass9 -in f", f"openssl enc -aes256 -pass pass:{R} -in f"),
    ("zip -P plainpass9 out.zip f", f"zip -P {R} out.zip f"),
    ("unzip -P plainpass9 out.zip", f"unzip -P {R} out.zip"),
    ("7z a -pplainpass9 out.7z f", f"7z a -p{R} out.7z f"),
    ("xfreerdp /u:alice /p:plainpass9 /v:host", f"xfreerdp /u:alice /p:{R} /v:host"),
    ("ssh-keygen -N plainpass9 -f k", f"ssh-keygen -N {R} -f k"),
    ("keytool -list -storepass plainpass9", f"keytool -list -storepass {R}"),
    # Already covered before #330 (kept here so the family is pinned whole).
    ("sshpass -p plainpass9 ssh admin@10.0.0.5 uptime", f"sshpass -p {R} ssh admin@10.0.0.5 uptime"),
    ("mysql -uroot -pplainpass9 db", f"mysql -uroot -p{R} db"),
    ("mysql -u root --password=plainpass9 db", f"mysql -u root --password={R} db"),
    ("PGPASSWORD=plainpass9 psql -h db", f"PGPASSWORD={R} psql -h db"),
    ("curl -u alice:plainpass9 https://x", f"curl -u alice:{R} https://x"),
    ("some-tool --password plainpass9", f"some-tool --password {R}"),
]

FAMILY_UNTOUCHED = [
    "htpasswd -c .htpasswd admin",              # no -b: htpasswd asks
    "htpasswd -D .htpasswd admin",
    "htpasswd -n admin",
    "smbclient -L //srv -N",
    "smbclient -U alice //srv/share",           # no %: it asks
    "lftp -u alice ftp://h",
    "openssl pkcs12 -passin env:P12_PASS -in a.p12",
    "openssl pkcs12 -passin file:/run/p -in a.p12",
    "openssl pkcs12 -passin stdin -in a.p12",
    "7z a -p out.7z f",                         # a spaced -p asks
    "ssh-keygen -N '' -f k",
    "ssh-keygen -l -f k.pub",
    "zip -r out.zip dir",
    "ls -P plainpass9",                         # not the program
]


class Family(unittest.TestCase):
    def setUp(self):
        self.root = _env_isolated(self)

    def test_each_is_withheld_on_the_card_the_log_and_v1_lines(self):
        for command, shown in FAMILY:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)
                self.assertEqual(secrets.redact_inline(command), shown)
                self.assertNotIn("plainpass9", secrets.for_display(command))
                self.assertNotIn("adminpass99", secrets.for_display(command))
                self.assertFalse(secrets.holds_back_credential(command), "nothing left once withheld")

    def test_the_rest_is_byte_identical(self):
        for command in FAMILY_UNTOUCHED:
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), command)

    def test_the_never_hidden_rule_stands_and_is_reported(self):
        # A value holding an operator stays on the card (delta review of
        # #205): tighten only. `holds_back_credential` reports it, so a path
        # that sends the card off the machine sends nothing.
        for command in ("sshpass -p 'Tr0ub4dor&3' ssh admin@10.0.0.5 uptime",
                        "PGPASSWORD='Tr0ub&4dor3' psql", "curl -u 'alice:Tr0ub&4dor3' x",
                        "smbclient -U 'alice%Tr0ub&4dor3' //s/x",
                        # A separator inside the quotes splits the display's
                        # segment; the check reads the quoting.
                        "sshpass -p 'Tr0ub;4dor3' ssh a@h", "curl -u 'a:Tr0ub|4dor3' x",
                        "htpasswd -b f admin 'adm|npass99'"):
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), command)
                self.assertTrue(secrets.holds_back_credential(command))
                self.assertTrue(secrets.holds_back_credential({"command": command}))
        # A credential-named value with a glob is kept on the card the same
        # way, and held back from a model too (fail closed: no summary).
        self.assertTrue(secrets.holds_back_credential("API_KEY=abcd* ls"))
        plain = [c for c in UNTOUCHED if not any(m in c for m in ("two words", "a;b", "*", "{"))]
        for command in plain + FAMILY_UNTOUCHED + ["make deploy", "rm -rf build && make deploy", "echo 'a & b'",
                                                         "echo 'a|b' | grep a", "git log --format='%h|%s' | head"]:
            with self.subTest(command=command):
                self.assertFalse(secrets.holds_back_credential(command))
        # The switch it uses never leaks out of it.
        self.assertFalse(secrets._LOOSE_VALUES.get())
        self.assertEqual(secrets.redact_command("PGPASSWORD='Tr0ub&4dor3' psql"), "PGPASSWORD='Tr0ub&4dor3' psql")

    def test_every_occurrence_is_withheld(self):
        # Delta review of #330, NEW-2: only the first match per program and
        # segment was withheld.
        for command, shown in (
                ("ssh-keygen -p -P 0ldPass99 -N N3wPass99 -f k", f"ssh-keygen -p -P {R} -N {R} -f k"),
                ("openssl pkcs12 -in a -passin pass:AAApass1 -passout pass:BBBpass2",
                 f"openssl pkcs12 -in a -passin pass:{R} -passout pass:{R}"),
                ("zip -P aaapass1 -P bbbpass2 o.zip f", f"zip -P {R} -P {R} o.zip f"),
                ("ldapsearch -w aaapass1 -w bbbpass2", f"ldapsearch -w {R} -w {R}"),
                ("curl -u a:p1xxxx -u b:p2xxxx https://x", f"curl -u a:{R} -u b:{R} https://x")):
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)
                self.assertFalse(secrets.holds_back_credential(command))

    def test_a_glued_value_in_quotes_is_the_rest_of_the_word(self):
        for command, shown in (("7z a '-pTr0ub4dor' o.7z f", f"7z a '-p{R}' o.7z f"),
                               ('7z a "-pTr0ub4dor" o.7z f', f'7z a "-p{R}" o.7z f'),
                               ("mysql '-pTr0ub4dor' db", f"mysql '-p{R}' db")):
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)
        # A blank in it: the never-hidden rule keeps the value on the card
        # (its first run is withheld, as before); the check reports it, so
        # nothing of it leaves the machine.
        command = "7z a '-pTr0ub 4dor' o.7z f"
        self.assertNotIn("Tr0ub", secrets.redact_command(command))
        self.assertTrue(secrets.holds_back_credential(command))
        self.assertEqual(secrets.redact_command('mysql "-p$PW" db'), 'mysql "-p$PW" db', "an expansion is no value")

    def test_a_string_another_shell_runs_is_read(self):
        # Delta review of #330, NEW-1: `bash -c "..."` was one quoted word.
        held = ['bash -c "sshpass -p \'Tr0ub&4dor3\' ssh h"',               # the reviewer's probe
                'bash -c "sshpass -p Tr0ub4dor3 ssh h"',
                "bash -c \"sh -c 'sshpass -p Tr0ub4dor3 ssh h'\"",          # nested twice
                "/bin/bash -lc 'PGPASSWORD=x9x9x9x9 psql'",
                "su -c 'htpasswd -b f admin adminpass99' root",
                "eval 'mysql -pS3cretPw db'",
                "bash -c $'sshpass -p x\\x20ssh h'",                       # unreadable
                'bash -c "unterminated',                                      # unreadable
                "sh -c \"bash -c 'zsh -c \\\"dash -c ls\\\"'\"",     # past the depth bound
                "bash -c '" + "x" * (secrets._NESTED_MAX + 1) + "'"]         # past the size bound
        for command in held:
            with self.subTest(command=command[:80]):
                self.assertTrue(secrets.holds_back_credential(command))
        for command in ('bash -lc "cd /srv && make deploy"', 'bash -c "echo $HOME | wc"', 'eval "$(ssh-agent -s)"',
                        "sudo sh -c 'echo hi > /etc/x'", "bash -c \"sh -c 'ls -la'\"", "echo 'bash -c x'; ls",
                        "sh -c \"bash -c 'zsh -c \\\"ls\\\"'\""):
            with self.subTest(command=command):
                self.assertFalse(secrets.holds_back_credential(command), "summaries stay for ordinary nesting")

    def test_a_command_line_handed_on_is_read(self):
        # Final review of #330: a remote shell, a container's exec, a
        # terminal or job runner hands a command line on.
        held = ["ssh host 'mysql -pS3cretPw99 db'",
                "ssh host \"sshpass -p 'Tr0ub&4dor3' ssh h2\"",
                "ssh -p 2222 -i ~/.ssh/k -o StrictHostKeyChecking=no host 'PGPASSWORD=x9x9x9x9 psql'",
                "kubectl exec pod -- mysql -pS3cretPw99",
                "kubectl exec -it pod -c app -- sh -c 'mysql -pS3cretPw99'",
                "docker exec -it -u root db mysql -pS3cretPw99",
                "watch -n 5 'mysql -pS3cretPw99 -e status'",
                "script -q -c 'mysql -pS3cretPw99' /dev/null",
                "tmux new -d -s x 'mysql -pS3cretPw99'",
                "tmux send-keys -t x 'mysql -pS3cretPw99' Enter",
                "parallel 'mysql -pS3cretPw99 -e {}' ::: a b",
                "mosh host -- mysql -pS3cretPw99",
                "autossh -M 0 host 'mysql -pS3cretPw99'",
                "ssh host $'mysql -pS3cretPw99\\x20db'",                    # unreadable
                "ssh h \"ssh h2 \\\"ssh h3 'ssh h4 ls'\\\"\""]        # past the depth bound
        for command in held:
            with self.subTest(command=command):
                self.assertTrue(secrets.holds_back_credential(command))
        for command in ("ssh host 'ls -la'", "ssh host ls > out.txt", "ssh -o StrictHostKeyChecking=no host uptime",
                        "kubectl exec pod -- ls", "docker exec web ls /app", "watch -n 1 'kubectl get pods'",
                        "tmux new -s dev", "parallel gzip ::: a b", "grep ssh notes.txt", "docker run -p 8080:80 nginx"):
            with self.subTest(command=command):
                self.assertFalse(secrets.holds_back_credential(command), "summaries stay for ordinary lines")

    def test_an_exec_argv_tail_is_redacted_on_the_card(self):
        # Tighten only: the tail is an argv the container runs as it is.
        for command, shown in (
                ("kubectl exec pod -- mysql -pS3cretPw99", f"kubectl exec pod -- mysql -p{R}"),
                ("kubectl exec -it pod -c app -- mysql -uroot -pS3cretPw99 db",
                 f"kubectl exec -it pod -c app -- mysql -uroot -p{R} db"),
                ("docker exec -it -u root db mysql -pS3cretPw99", f"docker exec -it -u root db mysql -p{R}"),
                ("podman exec db sshpass -p S3cretPw99 ssh h", f"podman exec db sshpass -p {R} ssh h")):
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), shown)
        for command in ("kubectl exec pod -- ls", "docker exec web ls /app", "docker run -p 8080:80 nginx",
                        "kubectl exec pod -- sh -c 'mysql -pS3cretPw99'",     # a string a shell parses: as typed
                        "docker run --rm img mysql -pS3cretPw99"):            # `run`: the image cannot be placed
            with self.subTest(command=command):
                self.assertEqual(secrets.redact_command(command), command)
        # ...so the summary is held back for it instead.
        self.assertTrue(secrets.holds_back_credential("docker run --rm img mysql -pS3cretPw99"))

    def test_judges_never_read_it(self):
        def boom(*_a, **_k):
            raise AssertionError("a judge read through the display redaction")
        commands = [c for c, _ in FAMILY]
        before = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision) for c in commands]
        with patch.object(secrets, "redact_command", boom), patch.object(secrets, "holds_back_credential", boom):
            after = [(rules.decide(c).decision, v1_permissions.static_verdict(c).decision) for c in commands]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=1)

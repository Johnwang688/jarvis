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
    "app --key ./client-key.pem",
    "PASSWORD_FILE=/run/secrets/x ./run",
    "app --token-file=/tmp/t",
    "curl -b cookies.txt https://example.invalid",
    "SESSION_DRIVER=redis COOKIE_SECURE=true ./run",
]

CHEAP_CATCHES = [
    ("TOKEN=$'plain\\nvalue1'", f"TOKEN={R}"),
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
NEVER_HIDDEN = [
    ("X_TOKEN='rm -rf ~'; eval $X_TOKEN", True),
    ("X_TOKEN='rm -rf ~'; eval \"$X_TOKEN\"", True),
    ("X_TOKEN='rm -rf ~'; bash -c \"$X_TOKEN\"", True),
    ("X_TOKEN='rm -rf ~'; zsh -lc \"$X_TOKEN\"", True),
    ("X_TOKEN='rm -rf ~'; $X_TOKEN", True),
    ("X_TOKEN='rm -rf ~'; ${X_TOKEN}", True),
    ("export X_TOKEN='rm -rf ~' && sudo $X_TOKEN", True),
    ("X_TOKEN='rm -rf ~'; source <(echo $X_TOKEN)", True),
    ("X_TOKEN='rm -rf ~'; . <(echo $X_TOKEN)", True),
    ("X_TOKEN='import os; os.system(\"rm -rf ~\")'; python3 -c \"$X_TOKEN\"", True),
    ("X_TOKEN='rm -rf ~'; perl -e \"system('$X_TOKEN')\"", True),
    ("X_TOKEN='rm -rf ~'; echo \"$X_TOKEN\" | sh", True),
    ("X_TOKEN='rm -rf ~'; echo \"$X_TOKEN\" | /bin/bash", True),
    ("X_TOKEN='rm -rf ~'; echo $($X_TOKEN)", True),
    ("X_TOKEN='rm -rf ~'; echo `$X_TOKEN`", True),
    ("X_TOKEN='rm -rf ~'; eval '$'X_TOKEN", True),
    ("R=X_TOKEN; X_TOKEN='rm -rf ~'; eval ${!R}", True),
    ("X_TOKEN='rm -rf ~' bash -c '$X_TOKEN'", True),
    ("X_TOKEN='rm -rf ~'; bash <<< \"$X_TOKEN\"", True),
    ("X_TOKEN='rm -rf ~'; printf %s \"$X_TOKEN\" > /tmp/a.sh; sh /tmp/a.sh", True),
    ("X_TOKEN='rm -rf ~'; printf %s \"$X_TOKEN\" | xargs -I{} sh -c {}", True),
    ("X_TOKEN='rm -rf ~'; printf %s \"$X_TOKEN\" | tee run.sh", True),
    ("X_TOKEN='rm -rf ~'; cmd /c %X_TOKEN%", True),
    # Quoting: what looks like a value is not one in the line's own parse.
    ("echo 'MY_TOKEN=\"abc' ; rm -rf ~ ; echo '\"'", False),
    ("echo \"MY_TOKEN='$(rm -rf ~)'\"", False),
    ("curl https://h/x?a;rm -rf ~", False),
    ("curl https://u:p@h/x?a&rm -rf ~", False),
    ("curl -H Authorization:abc;rm -rf ~", False),
    ("curl -H Cookie:a=1;rm -rf ~", False),
    ("curl -u 'u:a' ; rm -rf ~ ; echo ':b' https://x", False),
    ("MY_TOKEN=abc;rm -rf ~", False),
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
        for command, marked in NEVER_HIDDEN:
            with self.subTest(command=command):
                card = secrets.redact_command(command)
                self.assertIn("rm -rf ~", card)
                if marked:
                    self.assertIn(secrets.RUN_MARKER, card)
                    self.assertTrue(card.startswith(command.split("=", 1)[0]))
                self.assertNotIn(R + R, card)

    def test_structure_is_never_withheld(self):
        # Rule 1, directly: a value's span with an unquoted operator, blank,
        # `$` or backtick, or one that ends in other quoting, is kept.
        mask = secrets._Mask("echo 'A=\"b' ; rm ; echo '\"'")
        self.assertFalse(mask.safe(8, 25))
        mask = secrets._Mask("A='b c' x")
        self.assertTrue(mask.safe(2, 7))
        self.assertFalse(mask.safe(2, 9))
        mask = secrets._Mask('echo "A=$(rm)"')
        self.assertFalse(mask.safe(8, 13))

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
            entry = broker.resolve(request.req_id, Decision.ALLOW, always=True)
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
            router.approvals, router.mirror = broker, None
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
        self.assertIn("always", text.lower())       # judged on the typed request

    def test_v1_cards(self):
        from jarvis.discord_approvals import _describe
        from jarvis.face.approvals import _for_display
        args = {"command": "MY_TOKEN=plainvalue1 ./run", "password": "plainpass"}
        self.assertEqual(_for_display(args), {"command": f"MY_TOKEN={R} ./run", "password": R})
        described = _describe(SimpleNamespace(args=args))
        self.assertNotIn("plain", described)
        self.assertIn(f"MY_TOKEN={R} ./run", described)
        run = {"command": "X_TOKEN='rm -rf ~'; eval $X_TOKEN"}
        self.assertIn("rm -rf ~", _for_display(run)["command"])
        self.assertIn("rm -rf ~", _describe(SimpleNamespace(args=run)))


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


if __name__ == "__main__":
    unittest.main(verbosity=1)

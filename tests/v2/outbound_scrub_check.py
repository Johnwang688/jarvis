"""Free checks: SEC-3, outbound surfaces get the whole scrub (2026-10-18).

Plan: docs/plans/2026-10-18-security-fix-specs.md, "SEC-3". Text that leaves the
machine (the Discord task reporter, the Discord chat mirror, the Diff tab's
Quick review, the chat git tools' output) goes through `redact.scrub_text`: the
credential-file values *and* the provider key shapes (`ghp_…`, `sk-…`) and the
JWT/bearer shapes. Text that cannot be scrubbed is withheld, never sent as is.

These are written to FAIL on main before the fix (the older surfaces call
`secrets.scrub` alone, which knows only Jarvis's own credential values).

Fakes only: a fake Discord transport (`httpx.request` raises on any call), a
scripted provider, temporary stores and config. Every key-shaped string is built
at run time, so no real-looking key is committed. "The scrub raises" patches
`jarvis.tools.secrets.scrub`, which `scrub_text` calls first: that works before
and after the fix.

Cases (the card's acceptance list):
  c1  key shapes in text: the posted payload / returned text holds none
  c2  the scrub raises: the withheld marker goes out, never the raw text
  c3  diff_review._clean: withheld on a raise, never raises
  c4  chat_git._scrub strips userinfo of any URL scheme
  c5  the render caps hold when a short value becomes a longer marker
  c6  mutation: each call site has a case of its own (see the names)

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/outbound_scrub_check.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402

from jarvis import config  # noqa: E402
from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import chat_git, diff_review as R  # noqa: E402
from jarvis.v2.bus import EventBus  # noqa: E402
from jarvis.v2.discord import DiscordRest, Reporter  # noqa: E402
from jarvis.v2.discord import mirror as M  # noqa: E402
from jarvis.v2.model import OpenQuestion, Report, TaskState, to_json  # noqa: E402
from jarvis.v2.stores import Stores  # noqa: E402
from tests.v2 import discord_mirror_check as MC  # noqa: E402
from tests.v2 import discord_render_check as RC  # noqa: E402

MARKER = "[output withheld: it could not be scrubbed]"
PLAIN = "PLAINSENTINEL"             # ordinary words: proof the raw text was withheld


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def ghp() -> str:
    return "ghp_" + "A" * 36


def sk() -> str:
    return "sk-" + "B1" * 15


def jwt() -> str:
    return ".".join((_b64(b'{"alg":"HS256","typ":"JWT"}'),
                     _b64(b'{"sub":"user-12345","name":"somebody"}'),
                     _b64(b"signature-bytes-0123456789")))


def keys() -> list[str]:
    return [ghp(), sk(), jwt()]


def raising(*_args, **_kwargs):
    raise RuntimeError("the scrub is broken")


def scrub_raises():
    """`secrets.scrub` raises: what `scrub_text` calls first, before and after."""
    return patch.object(secrets, "scrub", side_effect=raising)


def absent(test, needle, text, where):
    """`needle` is not in `text`; a failure names the place, not the whole text."""
    if needle in text:
        i = text.index(needle)
        test.fail(f"{where}: {needle[:12]!r}… left the machine, in …{text[max(0, i - 40):i + 60]!r}…")


def no_keys(test, text, where):
    for key in keys():
        absent(test, key, text, f"{where}: a key-shaped value")
    absent(test, "ghp_AAAA", text, where)


# --------------------------------------------------------------------------
# the Discord task reporter

class ReporterChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token_path = Path(self.tmp.name) / "discord_token.json"
        token_path.write_text(json.dumps({"bot_token": RC.TOKEN, "owner_id": "owner"}))
        for name, value in (("DISCORD_TOKEN_PATH", token_path),
                            ("DISCORD_GUILD_PATH", Path(self.tmp.name) / "discord_guild.json")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch.object(httpx, "request", side_effect=AssertionError("live network"))
        network.start()
        self.addCleanup(network.stop)
        self.fake = RC.FakeTransport()
        self.rest = DiscordRest(self.fake)
        self.addCleanup(self.rest.close)
        pace = patch("jarvis.v2.discord.reporter.CREATE_INTERVAL", 0.0)
        pace.start()
        self.addCleanup(pace.stop)

    # -- helpers (the render check's, copied: its class carries other tests) --

    def reporter(self, channel="project-channel"):
        self.stores = Stores(Path(self.tmp.name) / "stores")
        self.project = self.stores.projects.create("Jarvis", "/tmp", discord_channel_id=channel)
        self.task = self.stores.tasks.create(self.project.id, "Implement task")
        self.stores.tasks.save(self.task)
        self.bus = EventBus()
        self.rep = Reporter(self.stores, self.rest, self.bus, dm_channel=lambda: RC.DM,
                            reconcile=True)
        self.addCleanup(self.rep.close)
        self.drained()

    def publish(self, task):
        self.bus.publish({"kind": "task_status_changed", "task_id": task.id,
                          "project_id": task.project_id, "data": to_json(task)})

    def move(self, *phases):
        task = self.task
        for phase in phases:
            task = self.stores.tasks.transition(task.id, phase)
            self.publish(task)
        self.task = task
        return task

    def drained(self):
        def idle():
            rep = self.rep
            due = (rep._reconcile_at is not None and not rep._closing
                   and rep._reconcile_at <= rep._clock() + 0.2 and not rep.breaker_open())
            return rep.subscription.unfinished_tasks == 0 and not rep.reconciling and not due
        RC.wait_for(idle)
        import time
        time.sleep(0.01)
        RC.wait_for(idle)

    def run_task(self, *, brief, question, last_file, done, verified):
        """A task through to DONE with the given text in every reporter field."""
        self.reporter()
        task = self.stores.tasks.get(self.task.id)
        task.brief = brief
        self.stores.tasks.save(task)
        task = self.move(TaskState.CLARIFYING)
        q = OpenQuestion(question, True, options=[question, "no"])
        task.spec.questions = [q]
        task.status.open_question = q.text
        task.status.last_file = last_file
        self.stores.tasks.save(task)
        self.publish(task)
        self.drained()
        self.task = self.stores.tasks.get(self.task.id)
        self.task.spec.questions[0].answer = "no"
        self.task.status.open_question = None
        self.task.report = Report(done=[done], verified=verified)
        self.stores.tasks.save(self.task)
        self.move(TaskState.PLANNED, TaskState.RUNNING, TaskState.VERIFYING, TaskState.DONE)
        self.drained()
        self.rep.close(flush=True)
        return repr(self.fake.calls)

    def posts(self):
        return [c for c in self.fake.calls if c["method"] == "POST"
                and c["path"].endswith("/messages")]

    # -- c1 -----------------------------------------------------------------

    def test_c1_reporter_posts_no_key_shaped_value(self):
        """c1/reporter: a ghp_, an sk- and a JWT that are in no Jarvis file reach
        no thread name, report, question, embed or attachment."""
        ghp_key, sk_key, token = keys()
        wire = self.run_task(brief=f"deploy with {ghp_key}",
                             question=f"Use {token}?",
                             last_file=f"/tmp/{sk_key}",
                             done=f"used {ghp_key} and {token} " + "x" * 3000,
                             verified=f"checked {sk_key}")
        no_keys(self, wire, "reporter")
        self.assertTrue("redacted" in wire, "nothing was redacted")
        self.assertTrue(any(c.get("files") for c in self.posts()),
                        "the report overflow was attached (and so scrubbed too)")

    # -- c2 -----------------------------------------------------------------

    def test_c2_reporter_withholds_everything_when_the_scrub_raises(self):
        """c2/reporter: with the scrub broken the withheld marker goes out and
        none of the raw text does."""
        with scrub_raises():
            wire = self.run_task(brief=f"{PLAIN}-brief {ghp()}",
                                 question=f"{PLAIN}-question?",
                                 last_file=f"/tmp/{PLAIN}-file",
                                 done=f"{PLAIN}-done {jwt()}",
                                 verified=f"{PLAIN}-verified")
        absent(self, PLAIN, wire, "raw text")
        no_keys(self, wire, "reporter")
        self.assertTrue(MARKER in wire, "the withheld marker is missing")

    # -- c5 -----------------------------------------------------------------

    def test_c5_reporter_render_caps_hold_when_a_short_value_grows(self):
        """c5/reporter: a known 8-character value becomes the 53-character
        redaction; the 400 / 1500 / 1024 / 6000 caps are applied after that."""
        short = "abcd1234"
        with patch.object(secrets, "secret_values", return_value=[short]):
            repeated = lambda n: (short + " ") * n                        # noqa: E731
            wire = self.run_task(brief=repeated(40),                         # 360 raw, > 400 scrubbed
                                 question=repeated(110),                     # 990 raw, > 1024 scrubbed
                                 last_file=repeated(100),
                                 done=repeated(160),                         # 1440 raw, > 1500 scrubbed
                                 verified=repeated(20))
        absent(self, short, wire, "known value")
        contents = [json.loads(json.dumps(RC.payload(c))).get("content") or ""
                    for c in self.posts()]
        started = [t for t in contents if t.startswith("Started task")]
        self.assertTrue(started, contents)
        self.assertTrue(all(len(t) <= 400 for t in started), [len(t) for t in started])
        reports = [t for t in contents if "\nDONE" in t]
        self.assertTrue(reports, [t[:20] for t in contents])
        bodies = [t.split("\n", 1)[1] for t in reports]       # after "Done: task …" line
        self.assertTrue(all(len(b) <= 1500 for b in bodies), [len(b) for b in bodies])
        self.assertTrue(all(len(t) <= 2000 for t in contents))
        embeds = [e for c in self.posts() for e in (RC.payload(c).get("embeds") or [])]
        self.assertTrue(embeds)
        for embed in embeds:
            for field in embed["fields"]:
                self.assertLessEqual(len(field["value"]), 1024, field["name"])
            total = len(embed["title"]) + sum(len(f["name"]) + len(f["value"])
                                              for f in embed["fields"])
            self.assertLessEqual(total, 6000)


# --------------------------------------------------------------------------
# the Discord chat mirror

class MirrorChecks(MC.Harness):
    def posts_for(self, chat, text, *, spoken=False, via="hud", turn="turn-1"):
        """`_turn_posts` for one owner message and one reply, as the worker calls it."""
        log = [{"kind": "user", "turn_id": turn,
                "data": {"text": "hello", "typed": "hello", "via": via, "spoken": spoken,
                         "discord_channel_id": "9"}}]
        segment = [{"kind": "text", "turn_id": turn, "data": {"text": text}},
                   {"kind": "turn_finished", "turn_id": turn, "data": {"stop": "end"}}]
        return self.mirror._turn_posts(chat.id, "9", log, segment, turn, 3, False)

    # -- c1: the call sites -----------------------------------------------------

    def test_c1_mirror_name_has_no_key_shaped_value(self):
        """c1/mirror/chat_name (mirror.py:123): a title or an excerpt."""
        chat = self.open_chat()
        for key in keys():
            titled = self.stores.threads.get(chat.id)
            titled.title = f"notes {key}"
            no_keys(self, M.chat_name(titled, ""), "mirror title")
            titled.title = ""
            no_keys(self, M.chat_name(titled, f"use {key} now"), "mirror excerpt")

    def test_c1_mirror_not_sent_line_has_no_key_shaped_value(self):
        """c1/mirror/user_line why-line (mirror.py:154): a dropped Discord message."""
        for key in keys():
            line = M.user_line({"via": "discord", "typed": f"deploy with {key}"}, dropped=True)
            self.assertTrue(line and line.startswith("Not sent"), line)
            no_keys(self, line, "mirror not-sent line")

    def test_c1_mirror_system_head_has_no_key_shaped_value(self):
        """c1/mirror/user_line system head (mirror.py:164)."""
        for key in keys():
            line = M.user_line({"via": "system", "typed": f"[owner ran: echo {key}]\nmore"})
            self.assertTrue(line and line.startswith("· system:"), line)
            no_keys(self, line, "mirror system head")

    def test_c1_mirror_hud_lines_have_no_key_shaped_value(self):
        """c1/mirror/user_line joined lines (mirror.py:181): the HUD line, the
        peer request, a steer, an attachment name."""
        for key in keys():
            for data in ({"via": "hud", "typed": f"my key is {key}"},
                         {"via": "hud", "typed": "x", "steer": True, "attachments": [f"{key}.md"]},
                         {"via": "peer", "typed": f"please use {key}"}):
                line = M.user_line(data)
                self.assertTrue(line, data)
                no_keys(self, line, f"mirror user line {data['via']}")

    def test_c1_mirror_reply_chunks_have_no_key_shaped_value(self):
        """c1/mirror/reply (mirror.py:1099): what Jarvis said."""
        chat = self.open_chat()
        reply = f"the key is {ghp()}, the token {jwt()} and {sk()} ok"
        posts = self.posts_for(chat, reply)
        texts = [p.content or "" for p in posts]
        self.assertTrue(any("the key is" in t for t in texts), texts)
        no_keys(self, "\n".join(texts), "mirror reply")

    def test_c1_mirror_spoken_text_has_no_key_shaped_value(self):
        """c1/mirror/speech (mirror.py:1116): a value redacted from the text must
        not come back as audio."""
        chat = self.open_chat()
        reply = f"say the key {ghp()} and {jwt()} and {sk()}"
        posts = self.posts_for(chat, reply, spoken=True, via="discord")
        speak = [p for p in posts if p.action == "speak"]
        self.assertTrue(speak, "a voice message's reply is spoken")
        no_keys(self, "\n".join(p.content or "" for p in posts), "mirror speech")

    def test_c1_mirror_end_to_end_posts_no_key_shaped_value(self):
        """c1/mirror/wire: the real flow — a thread named from, and a HUD line and
        a reply carrying, key shapes — puts none on the wire."""
        self.provider.default = [("text", f"your key {ghp()} and {jwt()} and {sk()}")]
        chat = self.open_chat()
        self.hud_send(chat, f"{ghp()} {jwt()} {sk()}")
        self.place(chat.id)
        self.idle(chat.id)
        self.settled(chat.id)
        wire = json.dumps(self.transport.calls)
        no_keys(self, wire, "mirror wire")
        self.assertTrue("redacted" in wire, "nothing was redacted")
        self.assertEqual(self.transport.violations, [])

    # -- c2: a scrub that raises ---------------------------------------------------

    def test_c2_mirror_name_withheld_when_the_scrub_raises(self):
        """c2/mirror/chat_name."""
        chat = self.open_chat()
        titled = self.stores.threads.get(chat.id)
        titled.title = f"{PLAIN}-title"
        with scrub_raises():
            name = M.chat_name(titled, "")
            titled.title = ""
            excerpt = M.chat_name(titled, f"{PLAIN}-excerpt")
        for text in (name, excerpt):
            absent(self, PLAIN, text, "raw text")
            self.assertTrue(MARKER in text, "the withheld marker is missing")
            self.assertLessEqual(len(text), M.NAME_MAX)

    def test_c2_mirror_not_sent_line_withheld_when_the_scrub_raises(self):
        """c2/mirror/user_line why-line."""
        with scrub_raises():
            line = M.user_line({"via": "discord", "typed": f"{PLAIN}-typed"}, dropped=True)
        self.assertTrue(line)
        absent(self, PLAIN, line, "raw text")
        self.assertTrue(MARKER in line, "the withheld marker is missing")

    def test_c2_mirror_system_head_withheld_when_the_scrub_raises(self):
        """c2/mirror/user_line system head."""
        with scrub_raises():
            line = M.user_line({"via": "system", "typed": f"[owner ran: {PLAIN}-cmd]"})
        self.assertTrue(line)
        absent(self, PLAIN, line, "raw text")
        self.assertTrue(MARKER in line, "the withheld marker is missing")

    def test_c2_mirror_hud_line_withheld_when_the_scrub_raises(self):
        """c2/mirror/user_line joined lines."""
        with scrub_raises():
            line = M.user_line({"via": "hud", "typed": f"{PLAIN}-typed",
                                "attachments": [f"{PLAIN}.md"]})
        self.assertTrue(line)
        absent(self, PLAIN, line, "raw text")
        self.assertTrue(MARKER in line, "the withheld marker is missing")

    def test_c2_mirror_reply_withheld_when_the_scrub_raises(self):
        """c2/mirror/reply: the marker is posted and the raw reply never."""
        chat = self.open_chat()
        with scrub_raises():
            posts = self.posts_for(chat, f"{PLAIN}-reply {ghp()}")
        texts = [p.content or "" for p in posts]
        absent(self, PLAIN, "\n".join(texts), "raw text")
        no_keys(self, "\n".join(texts), "mirror reply")
        self.assertTrue(any(MARKER in t for t in texts), texts)

    def test_c2_mirror_speech_withheld_when_the_scrub_raises(self):
        """c2/mirror/speech: nothing raw is spoken. Nothing spoken, or only the
        marker, are both acceptable."""
        chat = self.open_chat()
        with scrub_raises():
            posts = self.posts_for(chat, f"{PLAIN}-reply {ghp()}", spoken=True, via="discord")
        for post in posts:
            absent(self, PLAIN, post.content or "", "raw text")
            no_keys(self, post.content or "", f"mirror {post.action}")
        for post in posts:
            if post.action == "speak":
                self.assertTrue(MARKER in (post.content or ""), "the withheld marker is missing")

    # -- c5: caps ------------------------------------------------------------------

    def test_c5_mirror_caps_hold_when_a_short_value_grows(self):
        """c5/mirror: an 8-character known value becomes a 53-character
        redaction; the name (100) and every post (2000) are cut after that."""
        short = "abcd1234"
        chat = self.open_chat()
        with patch.object(secrets, "secret_values", return_value=[short]):
            titled = self.stores.threads.get(chat.id)
            titled.title = (short + " ") * 11                          # 99 raw
            name = M.chat_name(titled, "")
            line = M.user_line({"via": "hud", "typed": (short + " ") * 300})
            chunks = [p.content or "" for p in
                      self.posts_for(chat, (short + " ") * 210)]     # 1890 raw
        self.assertLessEqual(len(name), M.NAME_MAX)
        self.assertNotIn(short, name)
        self.assertNotIn(short, line)
        self.assertTrue(chunks)
        self.assertTrue(all(len(t) <= M.MAX_CHARS for t in chunks), [len(t) for t in chunks])
        absent(self, short, "\n".join(chunks), "known value")


# --------------------------------------------------------------------------
# the Diff tab's Quick review

class DiffReviewChecks(unittest.TestCase):
    def test_c1_diff_review_clean_has_no_key_shaped_value(self):
        """c1/diff_review._clean: a finding's text, path and summary."""
        for key in keys():
            text = R._clean(f"leaks {key} here", 400)
            no_keys(self, text, "diff_review._clean")
            self.assertIn("leaks", text)
        answer = {"summary": f"found {ghp()}", "findings": [
            {"severity": "bug", "file": f"x/{sk()}.py", "line": 3, "why": f"token {jwt()} in code"}]}
        result = R.parse("```json\n" + json.dumps(answer) + "\n```", ["x.py"])
        no_keys(self, json.dumps(result), "diff_review.parse")

    def test_c1_diff_review_clean_redacts_a_key_split_by_invisible_characters(self):
        """c1/diff_review._clean: a zero-width or bidi character inside a key
        must not hide it from the scrub, only for `strip_invisible` to join it
        back together on the way out (invisible characters go first)."""
        for key in keys():
            for mark in ("\u200b", "\u2060", "\u202e", "\ufeff"):
                split = key[:6] + mark + key[6:]
                text = R._clean(f"leaks {split} here", 400)
                no_keys(self, text, f"diff_review._clean with {mark!r} inside the key")

    def test_c3_diff_review_clean_withholds_and_never_raises(self):
        """c3: with the scrub raising, `_clean` returns the withheld result."""
        with scrub_raises():
            text = R._clean(f"{PLAIN} {ghp()}", 400)
        absent(self, PLAIN, text, "raw text")
        no_keys(self, text, "diff_review._clean")
        self.assertTrue(MARKER in text, "the withheld marker is missing")

    def test_c3_diff_review_parse_withholds_and_never_raises(self):
        """c3: the whole answer reader survives a broken scrub, and shows no raw text."""
        answer = {"summary": f"{PLAIN}-summary", "findings": [
            {"severity": "bug", "file": "x.py", "line": 3, "why": f"{PLAIN}-why {ghp()}"}]}
        with scrub_raises():
            result = R.parse("```json\n" + json.dumps(answer) + "\n```", ["x.py"])
        self.assertIsNotNone(result)
        absent(self, PLAIN, json.dumps(result), "raw text")
        no_keys(self, json.dumps(result), "diff_review.parse")
        self.assertTrue(MARKER in json.dumps(result), "the withheld marker is missing")

    def test_c5_diff_review_caps_hold_when_a_short_value_grows(self):
        """c5/diff_review: the cap is applied after the scrub."""
        short = "abcd1234"
        with patch.object(secrets, "secret_values", return_value=[short]):
            text = R._clean((short + " ") * 100, R.WHY_CAP)
        self.assertNotIn(short, text)
        self.assertLessEqual(len(text), R.WHY_CAP)


# --------------------------------------------------------------------------
# the chat git tools' output

class ChatGitChecks(unittest.TestCase):
    def test_c4_chat_git_scrub_strips_userinfo_of_any_scheme(self):
        """c4: `postgresql://u:pw@h/db` — the gitops regex knows only http(s)."""
        password = "hunter2pw"
        for scheme in ("postgresql", "ssh", "ftp", "mysql", "redis", "amqps", "https", "http"):
            text = chat_git._scrub(f"fatal: {scheme}://user:{password}@db.example/prod")
            self.assertNotIn(password, text, scheme)
            self.assertIn("db.example", text, scheme)
        text = chat_git._scrub(f"fatal: postgresql://u:{password}@h/db")
        self.assertNotIn(password, text)
        # A token as the whole userinfo, and several URLs in one output.
        text = chat_git._scrub(f"a ssh://{password}@h/x b ftp://u:{password}@h2/y")
        self.assertNotIn(password, text)

    def test_c1_chat_git_scrub_has_no_key_shaped_value(self):
        """c1/chat_git: the key shapes (already through `scrub_text` — kept so
        switching the userinfo strip cannot drop that layer)."""
        text = chat_git._scrub(f"remote: {ghp()} {jwt()} {sk()}")
        no_keys(self, text, "chat_git._scrub")

    def test_c2_chat_git_scrub_withholds_when_the_scrub_raises(self):
        """c2/chat_git: kept so the userinfo step cannot let a raise through."""
        with scrub_raises():
            text = chat_git._scrub(f"{PLAIN} postgresql://u:pw@h/db")
        self.assertEqual(text, MARKER)

    def test_c5_chat_git_output_cap_holds(self):
        """c5/chat_git: `_render` cuts after scrubbing."""
        short = "abcd1234"
        with patch.object(secrets, "secret_values", return_value=[short]):
            text = chat_git._render(0, (short + " ") * 2000, "")
        self.assertNotIn(short, text)
        self.assertLessEqual(len(text), chat_git.OUTPUT_CAP + len("\n[truncated]"))


if __name__ == "__main__":
    unittest.TestCase.shortDescription = lambda self: None      # print each case's own name
    unittest.main(verbosity=2)

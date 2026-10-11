"""`terminal_read` end to end (WP-F; decisions W-2): a real PTY, the scripted
fake shell, the daemon's own terminals, the tool through `dispatch()`, and the
fast path's holder rule.

Free and hermetic, on `terminal_check.Base`: three ephemeral loopback ports
(never 8402/8403/8405), `JARVIS_TERMINAL_SHELL` at `terminal_fake/sh`, a temp
HOME and temp config paths, the v1 session store in a temp dir. Placeholder
credentials are assembled at run time and never printed by this suite.

The fake shell's `hex HEX` writes raw bytes, so an escape sequence or a
credential-shaped placeholder reaches the ring without ever appearing in the
echoed command line — what the rules see is what a real program would print.

Written to fail against `main` before WP-F: `terminal_read` did not exist.
"""
from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from terminal_check import Base, eventually  # noqa: E402

from jarvis import config, llm, runtime, tools  # noqa: E402
from jarvis.v2 import terminal_guard as G, terminals as T  # noqa: E402
from jarvis.v2.model import ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, BriefRefused, Decision, UserMessage  # noqa: E402
from jarvis.v2.providers import fastpath  # noqa: E402
from jarvis.v2.providers.fastpath import FastPathProvider  # noqa: E402
from jarvis.v2.tools import terminal_read as TR  # noqa: E402

REFUSED = "Refused: " + G.REFUSAL
NL = b"\n"


def P(*parts: str) -> str:
    return "".join(parts)


def hexed(data: bytes | str) -> str:
    return (data.encode() if isinstance(data, str) else data).hex()


def read(terminal, lines=200, *, desk=True, depth=0):
    """The tool, through `dispatch()` (so its scrub runs), in a context bound
    the way the fast path binds an owner's HUD turn — or not at all."""
    ctx = contextvars.copy_context()
    if desk is not None:
        ctx.run(runtime.bind, desk={"present": desk}, depth=depth)
    return ctx.run(tools.dispatch, "terminal_read",
                   json.dumps({"terminal": terminal, "lines": lines})).text


class ReadBase(Base):
    def setUp(self):
        super().setUp()
        self.events = self.daemon.bus.subscribe()

    def reads(self):
        out = []
        while True:
            try:
                record = self.events.get_nowait()
            except Exception:
                return out
            if record.get("kind") == "terminal_read":
                out.append(record)

    def run_line(self, client, line, wait_for):
        client.type(line)
        client.wait_text(wait_for)

    def settle(self, tid):
        """Wait for the shell's prompt after the last command (the D mark)."""
        terminal = self.term(tid)
        eventually(lambda: terminal.history().spans and terminal.history().spans[-1].end is not None,
                   what="the last command's end")


# -- what a read returns, and what it announces ---------------------------------------

class Reads(ReadBase):
    def test_a_read_is_fenced_labelled_and_announced(self):
        tid, client = self.session()
        marker = "read-marker-" + secrets.token_hex(4)
        self.run_line(client, f"echo {marker}", f"\n{marker}")
        self.settle(tid)
        self.reads()
        text = read(tid)
        title = self.term(tid).title
        self.assertTrue(text.startswith("[untrusted web content "), text[:120])
        self.assertIn(f"from HUD terminal {title} (id {tid})", text.splitlines()[0])
        self.assertIn(marker, text)
        self.assertIn("[end of web content ", text)
        records = self.reads()
        self.assertEqual(len(records), 1, records)
        self.assertEqual(set(records[0]), {"kind", "data"})
        self.assertEqual(set(records[0]["data"]), {"terminal_id", "lines", "at", "refused"})
        self.assertEqual((records[0]["data"]["terminal_id"], records[0]["data"]["refused"]), (tid, False))
        self.assertGreater(records[0]["data"]["lines"], 0)
        self.assertNotIn(marker, json.dumps(records))

    def test_a_terminal_is_named_as_the_hud_shows_it(self):
        tid, client = self.session()
        self.run_line(client, "echo named", "\nnamed")
        title, label = self.term(tid).title, self.term(tid).label
        for spec in (tid, title, title.upper(), label, "1", ""):
            with self.subTest(spec=spec):
                self.assertIn("named", read(spec))
        missing = read("/etc/passwd")
        self.assertTrue(missing.startswith("Error: no terminal is called /etc/passwd"), missing)
        self.assertIn(f"1. {title} (id {tid})", missing)
        self.open_terminal()                                       # a second, same title
        self.assertIn("more than one terminal", read(title))
        self.assertTrue(read("").startswith("Error: no terminal is called nothing"))
        self.assertIn("named", read(tid))
        self.assertEqual(read("9"), read("9"))                     # out of range: an error, no event
        self.assertTrue(read("9").startswith("Error:"))

    def test_only_at_the_desk(self):
        tid, client = self.session()
        self.run_line(client, "echo desk", "\ndesk")
        self.reads()
        for kw in ({"desk": None}, {"desk": False}, {"desk": True, "depth": 1}):
            with self.subTest(**kw):
                self.assertEqual(read(tid, **kw), TR.NOT_AT_DESK)
        self.assertEqual(self.reads(), [], "a refused caller reads no terminal and announces nothing")
        self.assertIn("desk", read(tid))

    def test_the_owners_switch_refuses_by_name(self):
        tid, client = self.session()
        self.run_line(client, "echo switch", "\nswitch")
        self.owner("PATCH", f"/terminals/{tid}", {"readable": False})
        self.reads()
        text = read(tid)
        self.assertEqual(text, f"Refused: {T.READ_OFF} ({self.term(tid).title})")
        self.assertNotIn("switch", text.replace(T.READ_OFF, ""))
        records = self.reads()
        self.assertEqual([(r["data"]["refused"], r["data"]["lines"]) for r in records], [(True, 0)])
        self.owner("PATCH", f"/terminals/{tid}", {"readable": True})
        self.assertIn("switch", read(tid))


# -- the three refusal families --------------------------------------------------------

class Refusals(ReadBase):
    def refused(self, tid):
        self.reads()
        text = read(tid)
        self.assertEqual(text, REFUSED)
        records = self.reads()
        self.assertEqual([r["data"]["refused"] for r in records], [True])
        return text

    def test_exact_known_values_and_their_encodings(self):
        # `+` and `/` so the URL-encoded form holds no plain copy of the value.
        value = P("placeholder+", secrets.token_hex(12), "/z")
        (self.work / ".env").write_text(f"PLACEHOLDER_SERVICE_TOKEN={value}\n")
        encoded = quote(value, safe="")
        self.assertNotIn(value, encoded)
        for shown in (value.encode(), base64.b64encode(b"T=" + value.encode()), encoded.encode()):
            with self.subTest(shown=shown[:6]):
                tid, client = self.session()
                self.run_line(client, "hex " + hexed(b"out: " + shown + NL), "\nout: ")
                self.settle(tid)
                self.assertNotIn(value, self.refused(tid))
                self.owner("DELETE", f"/terminals/{tid}")

    def test_a_credential_format(self):
        key = P("gh", "p_", "A1b2C3d4" * 4, "E5f6")
        tid, client = self.session()
        self.run_line(client, f"hex {hexed('using ' + key + chr(10))}", "\nusing ")
        self.settle(tid)
        self.refused(tid)

    def test_a_secret_printing_command_by_its_span(self):
        for line in ("printenv", "cat .env", "  sudo -k env"):
            with self.subTest(line=line):
                tid, client = self.session()
                self.run_line(client, line, "command not found")
                self.settle(tid)
                self.assertEqual(self.term(tid).history().spans[-1].command, line)
                self.refused(tid)
                self.owner("DELETE", f"/terminals/{tid}")

    def test_forged_marks_cannot_launder_printenv(self):
        """A program prints marks without the nonce, claiming an innocent
        command, then a nested shell's prompt line and env output. The span the
        backend keeps is the real one (`hex …`); the text fallback refuses."""
        forged = (b"\x1b]133;C;cmdline_url=echo%20safe\x1b\\" + b"remote$ printenv\n"
                  + b"SERVICE_HOST=example\n" + b"\x1b]133;D;0\x1b\\")
        tid, client = self.session()
        self.run_line(client, f"hex {hexed(forged)}", "SERVICE_HOST")
        self.settle(tid)
        spans = self.term(tid).history().spans
        self.assertTrue(spans[-1].command.startswith("hex "), spans[-1].command)
        self.assertFalse(any(G.prints_secrets(s.command) for s in spans))
        self.refused(tid)

    def test_output_before_spans_from_is_still_covered(self):
        with patch.object(T, "SPAN_CAP", 3):
            tid, client = self.session()
            self.run_line(client, "hex " + hexed(b"remote$ printenv" + NL + b"A=1" + NL), "\nA=1")
            for word in ("one", "two", "three", "four"):
                self.run_line(client, f"echo {word}", f"\n{word}")
            self.settle(tid)
            history = self.term(tid).history()
            forged_at = history.start + history.data.index(b"remote$ printenv")
            self.assertGreater(history.spans_from, forged_at, "the forged line's span was evicted")
            self.assertTrue(all(s.start > forged_at for s in history.spans))
            self.refused(tid)


# -- the alternate screen --------------------------------------------------------------

class Alternate(ReadBase):
    def test_the_alternate_screen_is_never_read(self):
        tid, client = self.session()
        payload = b"\x1b[?1049h" + b"ALT-SCREEN-MARKER\n" + b"\x1b[?1049l" + b"normal-again\n"
        self.run_line(client, f"hex {hexed(payload)}", "normal-again")
        self.settle(tid)
        text = read(tid)
        self.assertIn("normal-again", text)
        self.assertNotIn("ALT-SCREEN-MARKER", text)

    def test_entered_and_never_left(self):
        tid, client = self.session()
        self.run_line(client, "echo before-alt", "\nbefore-alt")
        self.run_line(client, "hex " + hexed(b"\x1b[?1049hINSIDE-ALT" + NL), "INSIDE-ALT")
        self.run_line(client, "echo typed-in-alt", "\ntyped-in-alt")
        text = read(tid)
        self.assertIn("before-alt", text)
        for hidden in ("INSIDE-ALT", "\ntyped-in-alt"):
            self.assertNotIn(hidden, text)

    def test_the_state_survives_the_ring_dropping_its_start(self):
        """vim left open while more than a ring's worth is drawn: the switch
        into the alternate screen is long gone from the ring, and still the
        read shows nothing from inside it (`AltTracker`)."""
        tid, client = self.session()
        self.run_line(client, "hex " + hexed(b"\x1b[?1049h"), "\x1b[?1049hfake$ ")
        client.type("flood 100000")
        terminal = self.term(tid)
        eventually(lambda: terminal.history().start > 0 and b"line 099999" in terminal.history().data,
                   timeout=30, what="a flood past the ring inside the alternate screen")
        self.assertNotIn(b"\x1b[?1049h", terminal.history().data)
        self.assertTrue(terminal.history().alt)
        text = read(tid)
        self.assertNotIn("line 0", text)
        self.assertIn("[the terminal has shown nothing yet]", text)


# -- who holds it ------------------------------------------------------------------------

class Holders(unittest.TestCase):
    """Foreground chat only (W-2): the fast path, for an owner's chat turn at
    the HUD. Not workflows, sub-agents, goals, tasks, v1 surfaces, Discord —
    nor jarvis-mcp, which cannot tell a chat from a task worker."""

    def test_the_toolsets(self):
        from jarvis import agents, goalrunner, tasks, workflows
        from jarvis.tools import subagent
        from jarvis.v2 import mcp
        self.assertIn("terminal_read", fastpath.FAST_TOOLS)
        self.assertEqual(fastpath.DESK_TOOLS, frozenset({"terminal_read"}))
        self.assertIn("terminal_read", tools.EXPLICIT_ONLY)
        self.assertFalse(tools.REGISTRY["terminal_read"].dangerous)
        absent = {
            "tools.default_names() (v1 chat, face, daemon)": tools.default_names(),
            "workflows.SAFE_TOOLS": workflows.SAFE_TOOLS,
            "subagent.SUBAGENT_TOOLS": subagent.SUBAGENT_TOOLS,
            "goalrunner.goal_tool_names()": goalrunner.goal_tool_names(),
            "tasks.task_tool_names()": tasks.task_tool_names(),
            "mcp.MCP_TOOLS": mcp.MCP_TOOLS,
            "mcp.available_tools()": mcp.available_tools(),
        }
        for name, kind in agents.TYPES.items():
            absent[f"sub-agent type {name}"] = kind.tools
        for where, names in absent.items():
            with self.subTest(where=where):
                self.assertNotIn("terminal_read", set(names))

    def test_the_fast_path_offers_it_to_an_owners_chat_only(self):
        provider = FastPathProvider()
        chat = Brief(role=Role.CHAT, cwd="/tmp")
        self.assertIn("terminal_read", provider._toolset(chat))
        for brief in (Brief(role=Role.CHAT, cwd="/tmp", task_id="t1"),
                      Brief(role=Role.ORCHESTRATOR, cwd="/tmp"),
                      Brief(role=Role.IMPLEMENTER, cwd="/tmp")):
            with self.subTest(role=brief.role, task=brief.task_id):
                self.assertNotIn("terminal_read", provider._toolset(brief))
                with self.assertRaises(BriefRefused):
                    provider._toolset(Brief(role=brief.role, cwd="/tmp", task_id=brief.task_id,
                                            allowed_tools=["terminal_read", "get_datetime"]))

    def test_who_is_at_the_desk(self):
        chat = Brief(role=Role.CHAT, cwd="/tmp")
        task = Brief(role=Role.CHAT, cwd="/tmp", task_id="t1")
        at = fastpath._at_desk
        self.assertTrue(at(chat, UserMessage("hi", via="hud", desk=True)))
        self.assertTrue(at(chat, UserMessage("hi", desk=True)))           # via None: the HUD's
        for message in (UserMessage("hi", via="hud"),                     # not the window's own
                        UserMessage("hi", via="discord", desk=True),
                        UserMessage("hi", via="dm", desk=True),
                        UserMessage("hi", origin="owner-ran", desk=True),
                        UserMessage("hi", origin="system", via="system", desk=True)):
            with self.subTest(via=message.via, origin=message.origin):
                self.assertFalse(at(chat, message))
        self.assertFalse(at(task, UserMessage("hi", via="hud", desk=True)))


def _reply(text="", calls=None):
    message: dict = {"content": text or None}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function",
                                  "function": {"name": n, "arguments": a}}
                                 for i, (n, a) in enumerate(calls)]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop",
                     model="stub", latency_s=0.0, cost_usd=0.0)


class FastPathTurns(ReadBase):
    """A real fast-path turn reading a real terminal, with `llm.chat` scripted."""

    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp(dir=self.root))
        for name, value in dict(SESSIONS_DIR=tmp / "sessions", SKILLS_DIR=tmp / "skills",
                                SPILL_DIR=tmp / "spill", MODEL_CACHE_PATH=tmp / "catalog.json",
                                AVATAR_ENV="jarvis").items():
            guard = patch.object(config, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        (tmp / "skills").mkdir()

    @contextlib.contextmanager
    def scripted(self, tid, seen):
        real = llm.chat

        def chat(model, messages, tools=None, on_delta=None, **kw):
            if tools is None:
                return _reply("a title")
            names = [t["function"]["name"] for t in tools]
            seen.append({"names": names, "messages": list(messages)})
            if len(seen) == 1:
                return _reply(calls=[("terminal_read", json.dumps({"terminal": tid, "lines": 20}))])
            return _reply("done")
        llm.chat = chat
        try:
            yield
        finally:
            llm.chat = real

    def turn(self, message, tid):
        provider = FastPathProvider()
        thread = Thread(id="t" + secrets.token_hex(3), project_id=self.project.id, role=Role.CHAT,
                        provider=ProviderName.FAST)
        handle = provider.start(thread, Brief(role=Role.CHAT, cwd=str(self.work)),
                                lambda *_: Decision.ALLOW)
        seen: list = []
        with self.scripted(tid, seen):
            list(provider.send(handle, message))
        provider.close(handle)
        result = next(m["content"] for m in seen[-1]["messages"] if m.get("role") == "tool")
        return seen[0]["names"], result

    def test_a_hud_turn_reads_the_terminal(self):
        tid, client = self.session()
        self.run_line(client, "echo turn-marker", "\nturn-marker")
        names, result = self.turn(UserMessage("look at my terminal", via="hud", desk=True), tid)
        self.assertIn("terminal_read", names)
        self.assertIn("turn-marker", result)
        self.assertTrue(result.startswith("[untrusted web content "))

    def test_a_discord_turn_neither_holds_it_nor_can_call_it(self):
        tid, client = self.session()
        self.run_line(client, "echo turn-marker", "\nturn-marker")
        self.reads()
        for message in (UserMessage("look", via="discord"), UserMessage("look", via="hud")):
            with self.subTest(via=message.via, desk=message.desk):
                names, result = self.turn(message, tid)
                self.assertNotIn("terminal_read", names)
                self.assertEqual(result, TR.NOT_AT_DESK)
        self.assertEqual(self.reads(), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)

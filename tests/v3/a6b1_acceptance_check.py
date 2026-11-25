"""Orchestrator-owned acceptance tests for A6b-1 (implementers: do not edit; if one is wrong, stop and say why).

Contract: the v3 chat backend: v1's Agent loop, unchanged, with a fixed read-only toolset, behind owner routes that poll.
  jarvis/v3/chat.py        light imports only (never jarvis.agent, jarvis.llm, jarvis.models or jarvis.tools at import)
    ChatService(root, *, completion=None, clock=time.time, feed_root=None)    root: <data_dir>/chats; one chat thread per daemon
      snapshot() -> {"state": "idle"|"running"|"stopping"|"error", "session_id", "seq", "draft", "tools": [...], "history": [{"role", "text", "t"}] (last 50)}
      send(text) -> {"state", "steered": bool}   idle or error: starts a turn in a background thread; running: the text is steering
      stop() -> {"state"}                        idempotent
      events(after) -> {"state", "seq", "gap": bool, "events": [{"seq", "t", "kind", ...}]}
           kinds: user, steer, tool_start {name}, tool_end {name, ok, summary <= 200 chars}, reply {text}, error, stopped
      new_session() -> {"session_id"}            raises ChatBusy unless idle
      close(grace_s=5.0) -> bool                 stops a running turn and joins it; nothing is written after it returns
    MAX_TEXT = 16000
  jarvis/v3/fastpath.py    heavy; imported on the first send
    V3_TOOLS: frozenset     exactly: get_datetime, vault_list, vault_read, vault_search, index_search, skill_view, skill_names,
                            calendar_list, calendar_free, brief_post. Every one read-only or post-only; none dangerous; none asks.
    register_tools()        registers the v3 wrappers in jarvis.tools.REGISTRY under those names, each in tools.EXPLICIT_ONLY
                            (get_datetime is v1's own); idempotent; raises if a name is already registered by another module
    deny_all(tool, args) -> False     the approver bound for every turn; never human-backed
  Routes (owner-only, A6a's rules): GET /v3/owner/chat -> snapshot · POST /v3/owner/chat/send {"text"} -> 202 ·
    POST /v3/owner/chat/stop {} -> 200 · POST /v3/owner/chat/events {"after": int >= 0} -> 200 · POST /v3/owner/chat/new {} -> 200 or 409
    Bad bodies are 400; a text over MAX_TEXT is 400. Daemon(..., chat=None) builds ChatService(<data_dir>/chats) cheaply and closes it first on stop().
  Transcripts: <data_dir>/chats/<session id>/ (0700) in v1's session format; the working-context block is never saved; a new
  ChatService on the same root resumes the newest session. The calendar tools are offered only when config.GOOGLE_TOKEN_PATH is a file.
  The chat holds no vault write, no skill change, no calendar write, no web, no shell, no file tool.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config

REPO = Path(__file__).resolve().parents[2]
EXPECTED = {"get_datetime", "vault_list", "vault_read", "vault_search", "index_search", "skill_view", "skill_names", "calendar_list", "calendar_free", "brief_post"}


def reply(text="", calls=None):
    from jarvis import llm
    message = {"content": text or None}
    if calls:
        message["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop", model="stub", latency_s=0.0)


class Model:
    """A scripted model. Each step is a reply, an exception to raise, or a callable(messages) -> reply."""

    def __init__(self, *steps):
        self.steps, self.seen, self.gate = list(steps), [], None

    def __call__(self, *args, **kw):
        messages = kw.get("messages") if "messages" in kw else next(a for a in args if isinstance(a, list))
        self.seen.append({"messages": json.loads(json.dumps(messages, default=str)), "tools": sorted(t["function"]["name"] for t in (kw.get("tools") or []))})
        if self.gate is not None:
            self.gate.wait(20)
        step = self.steps.pop(0) if self.steps else reply("done")
        if isinstance(step, BaseException):
            raise step
        return step(messages) if callable(step) else step


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.v3 = self.tmp / "v3"
        env = {"JARVIS_V3_DIR": str(self.v3), "JARVIS_VAULT": str(self.v3 / "vault"), "JARVIS_INDEX_DB": str(self.v3 / "index.sqlite"), "JARVIS_AUTO_TITLE": "0"}
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch.object(config, "V3_DIR", self.v3))
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.tmp / "no-token.json"))
        from jarvis import llm, sessions
        self.enterContext(patch.object(sessions, "AUTO_TITLE", False))
        self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("the real model must never be called in tests")))
        from jarvis.v3 import chat
        self.chat = chat

    def service(self, model, **kw):
        svc = self.chat.ChatService(self.v3 / "chats", completion=model, feed_root=self.v3 / "feed", **kw)
        self.addCleanup(svc.close)
        return svc

    def wait_idle(self, svc, states=("idle", "error"), timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = svc.snapshot()["state"]
            if state in states:
                return state
            time.sleep(0.02)
        self.fail("the turn did not finish: " + svc.snapshot()["state"])

    def kinds(self, svc):
        return [e["kind"] for e in svc.events(0)["events"]]


class Turns(Base):
    def test_a_turn_with_a_tool(self):
        model = Model(reply(calls=[("vault_list", {})]), reply("Your vault is empty."))
        svc = self.service(model)
        out = svc.send("What do you know about me?")
        self.assertEqual((out["state"], out["steered"]), ("running", False))
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(self.kinds(svc), ["user", "tool_start", "tool_end", "reply"])
        events = svc.events(0)["events"]
        self.assertEqual((events[1]["name"], events[2]["ok"]), ("vault_list", True))
        self.assertLessEqual(len(events[2]["summary"]), 200)
        self.assertEqual(events[3]["text"], "Your vault is empty.")
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))
        self.assertEqual(svc.events(events[1]["seq"])["events"][0]["kind"], "tool_end")
        snap = svc.snapshot()
        self.assertEqual([(m["role"], m["text"]) for m in snap["history"]], [("user", "What do you know about me?"), ("assistant", "Your vault is empty.")])
        self.assertEqual(set(model.seen[0]["tools"]), EXPECTED - {"calendar_list", "calendar_free"})   # no Google grant in this test
        self.assertEqual(set(snap["tools"]), EXPECTED - {"calendar_list", "calendar_free"})

    def test_tools_outside_the_set_are_refused_as_text_and_nothing_asks(self):
        from jarvis.v3 import fastpath
        asked = []
        model = Model(reply(calls=[("write_file", {"path": "/tmp/x", "content": "x"}), ("run_command", {"command": "id"}), ("memory_add", {"folder": "facts", "name": "x", "description": "d", "body": "b", "source": "owner"})]),
                      reply("I cannot do that here."))
        with patch.object(fastpath, "deny_all", side_effect=lambda tool, args: (asked.append(tool), False)[1]):
            svc = self.service(model)
            svc.send("Write a file and remember this.")
            self.assertEqual(self.wait_idle(svc), "idle")
        results = [m["content"] for m in model.seen[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(results), 3)
        self.assertTrue(all("not available" in text or "nknown tool" in text or text.startswith("Error") for text in results), results)
        self.assertEqual(asked, [])
        self.assertFalse((self.tmp / "x").exists())
        self.assertFalse((self.v3 / "vault").exists() and any((self.v3 / "vault").rglob("*.md")))
        self.assertEqual(fastpath.V3_TOOLS, frozenset(EXPECTED))
        self.assertFalse(fastpath.deny_all("anything", {}))
        self.assertFalse(getattr(fastpath.deny_all, "jarvis_human_backed", False))

    def test_a_send_while_running_is_steering(self):
        model = Model(reply(calls=[("get_datetime", {})]), reply("first answer"))
        model.gate = threading.Event()
        svc = self.service(model)
        svc.send("Start.")
        while not model.seen:
            time.sleep(0.01)
        out = svc.send("Also check Friday.")
        self.assertTrue(out["steered"])
        model.gate.set()
        self.wait_idle(svc)
        self.assertIn("steer", self.kinds(svc))
        told = json.dumps([call["messages"] for call in model.seen[1:]])
        self.assertIn("Also check Friday.", told)

    def test_stop_ends_the_turn_and_keeps_the_transcript_whole(self):
        model = Model(reply(calls=[("get_datetime", {})]), reply("never reached"))
        model.gate = threading.Event()
        svc = self.service(model)
        svc.send("Start.")
        while not model.seen:
            time.sleep(0.01)
        self.assertIn(svc.stop()["state"], ("stopping", "idle"))
        model.gate.set()
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertIn("stopped", self.kinds(svc))
        self.assertEqual(svc.stop()["state"], "idle")
        svc.close()
        ok = Model(reply("after the stop"))
        svc2 = self.service(ok)
        svc2.send("Again.")
        self.wait_idle(svc2)
        for message in ok.seen[0]["messages"]:                    # every tool call still has its result (invariant 1)
            for call in message.get("tool_calls") or []:
                self.assertTrue(any(m.get("tool_call_id") == call["id"] for m in ok.seen[0]["messages"]), call)

    def test_a_model_error_is_recoverable(self):
        svc = self.service(Model(RuntimeError("provider said no: private detail"), reply("second try worked")))
        svc.send("Hello.")
        self.assertEqual(self.wait_idle(svc), "error")
        self.assertIn("error", self.kinds(svc))
        self.assertNotIn("private detail", json.dumps(svc.events(0)) + json.dumps(svc.snapshot()))
        self.assertEqual(svc.send("Hello again.")["steered"], False)
        self.assertEqual(self.wait_idle(svc), "idle")
        self.assertEqual(svc.snapshot()["history"][-1]["text"], "second try worked")

    def test_restart_resumes_and_the_context_block_is_never_saved(self):
        from jarvis import agent
        svc = self.service(Model(reply("The first answer.")))
        svc.send("Remember the word walrus.")
        self.wait_idle(svc)
        sid = svc.snapshot()["session_id"]
        svc.close()
        saved = "".join(p.read_text() for p in (self.v3 / "chats").rglob("*") if p.is_file())
        self.assertIn("walrus", saved)
        self.assertNotIn(agent.CONTEXT_BLOCK_PREFIX[:40], saved)
        self.assertEqual(oct((self.v3 / "chats").stat().st_mode & 0o777), "0o700")
        again = Model(reply("Walrus."))
        svc2 = self.service(again)
        self.assertEqual(svc2.snapshot()["session_id"], sid)
        self.assertEqual(len(svc2.snapshot()["history"]), 2)
        svc2.send("What was the word?")
        self.wait_idle(svc2)
        self.assertIn("walrus", json.dumps(again.seen[0]["messages"]))
        fresh = svc2.new_session()["session_id"]
        self.assertNotEqual(fresh, sid)
        self.assertEqual(svc2.snapshot()["history"], [])

    def test_close_stops_a_running_turn(self):
        model = Model(reply(calls=[("get_datetime", {})]), reply("late"))
        model.gate = threading.Event()
        svc = self.service(model)
        svc.send("Start.")
        while not model.seen:
            time.sleep(0.01)
        threading.Timer(0.3, model.gate.set).start()
        begun = time.monotonic()
        self.assertTrue(svc.close(grace_s=5.0))
        self.assertLess(time.monotonic() - begun, 6)
        files = {p: p.stat().st_mtime_ns for p in (self.v3 / "chats").rglob("*") if p.is_file()}
        time.sleep(0.5)
        self.assertEqual(files, {p: p.stat().st_mtime_ns for p in (self.v3 / "chats").rglob("*") if p.is_file()})


class Registry(unittest.TestCase):
    def test_v1_and_v2_toolsets_are_unchanged_and_agentd_stays_light(self):
        code = ("import sys, json\nfrom jarvis import tools\nbefore = sorted(tools.default_names())\n"
                "from jarvis.v3 import fastpath\nfastpath.register_tools(); fastpath.register_tools()\n"
                "after = sorted(tools.default_names())\n"
                "print(json.dumps({'same': before == after, 'explicit': sorted(fastpath.V3_TOOLS - {'get_datetime'} - set(tools.EXPLICIT_ONLY)),"
                " 'dangerous': sorted(n for n in fastpath.V3_TOOLS if tools.REGISTRY[n].dangerous), 'missing': sorted(fastpath.V3_TOOLS - set(tools.REGISTRY))}))")
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(REPO), "HOME": home, "PATH": os.environ.get("PATH", "")})
            self.assertEqual(out.returncode, 0, out.stderr[-600:])
            self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), {"same": True, "explicit": [], "dangerous": [], "missing": []})
            light = ("import sys\nfrom jarvis.v3 import daemon, chat\nfrom pathlib import Path\n"
                     "chat.ChatService(Path(sys.argv[1])).snapshot()\n"
                     "print(sorted(m for m in sys.modules if m in ('jarvis.agent', 'jarvis.llm', 'jarvis.models', 'jarvis.tools', 'jarvis.v3.fastpath', 'httpx', 'openai')))")
            out = subprocess.run([sys.executable, "-c", light, home + "/chats"], capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(REPO), "HOME": home, "PATH": os.environ.get("PATH", "")})
            self.assertEqual((out.returncode, out.stdout.strip()), (0, "[]"), out.stderr[-600:])

    def test_a_name_clash_is_refused(self):
        code = ("from jarvis import tools\nfrom typing import Annotated\n"
                "@tools.tool\ndef vault_list(folder: Annotated[str, 'x'] = '') -> str:\n    'someone else'\n    return ''\n"
                "from jarvis.v3 import fastpath\n"
                "try:\n    fastpath.register_tools()\n    print('registered')\nexcept Exception as exc:\n    print('refused', type(exc).__name__)")
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env={"PYTHONPATH": str(REPO), "HOME": home, "PATH": os.environ.get("PATH", "")})
        self.assertTrue(out.stdout.strip().startswith("refused"), out.stdout + out.stderr[-400:])


class Routes(Base):
    def setUp(self):
        super().setUp()
        from jarvis.v3 import daemon
        self.model = Model(reply("Hello from the window."))
        self.svc = self.service(self.model)
        self.d = daemon.Daemon(port=0, data_dir=self.v3, hud_dir=self.tmp / "hud3", jobs=[], chat=self.svc)
        self.d.start()
        self.addCleanup(self.d.stop)
        self.host = f"127.0.0.1:{self.d.port}"

    def call(self, method, path, body=None, owner=True):
        headers = {"Host": self.host}
        if owner:
            headers.update({"Origin": f"http://{self.host}"} if method == "POST" else {"Sec-Fetch-Site": "same-origin"})
        data = None
        if method == "POST":
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        conn = http.client.HTTPConnection("127.0.0.1", self.d.port, timeout=10)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp.status, (json.loads(text) if text else None)

    def test_send_poll_and_read(self):
        self.assertEqual(self.call("GET", "/v3/owner/chat")[1]["state"], "idle")
        status, out = self.call("POST", "/v3/owner/chat/send", {"text": "Hi."})
        self.assertEqual((status, out["steered"]), (202, False))
        self.wait_idle(self.svc)
        status, out = self.call("POST", "/v3/owner/chat/events", {"after": 0})
        self.assertEqual((status, [e["kind"] for e in out["events"]]), (200, ["user", "reply"]))
        self.assertEqual(self.call("GET", "/v3/owner/chat")[1]["history"][-1]["text"], "Hello from the window.")
        self.assertEqual(self.call("POST", "/v3/owner/chat/stop", {})[0], 200)
        self.assertEqual(self.call("POST", "/v3/owner/chat/new", {})[0], 200)

    def test_refusals(self):
        self.assertEqual(self.call("GET", "/v3/owner/chat", owner=False)[0], 403)
        self.assertEqual(self.call("POST", "/v3/owner/chat/send", {"text": "Hi."}, owner=False)[0], 403)
        for body in ({}, {"text": ""}, {"text": 5}, {"text": "x" * 16001}, {"text": "hi", "extra": 1}, {"text": "a\x00b"}):
            self.assertEqual(self.call("POST", "/v3/owner/chat/send", body)[0], 400, body)
        for body in ({}, {"after": -1}, {"after": "0"}, {"after": True}, {"after": 0, "x": 1}):
            self.assertEqual(self.call("POST", "/v3/owner/chat/events", body)[0], 400, body)
        self.assertEqual(self.model.seen, [])
        self.model.gate = threading.Event()
        self.call("POST", "/v3/owner/chat/send", {"text": "Hi."})
        while not self.model.seen:
            time.sleep(0.01)
        self.assertEqual(self.call("POST", "/v3/owner/chat/new", {})[0], 409)
        self.model.gate.set()
        self.wait_idle(self.svc)


if __name__ == "__main__":
    unittest.main()

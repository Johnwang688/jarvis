"""CU-3b with fake providers and a synthetic Windows bridge on ephemeral listeners."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from queue import Queue
import threading
import tomllib
import unittest
from unittest.mock import Mock, patch

import mcp_session_check as fixture
from jarvis import config
from jarvis.tools import files
from jarvis.v2 import mcp_session, router
from jarvis.v2.daemon import Daemon
from jarvis.v2.desktop_driver import DRIVER_BRIEF, REFUSED
from jarvis.v2.model import Role
from jarvis.v2.provider import Decision, UserMessage
from jarvis.v2.providers import codex_config
from jarvis.v2.tools import hud_window


class Checks(fixture.DaemonHarness):
    def setUp(self):
        flag = patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"})
        flag.start()
        self.addCleanup(flag.stop)
        super().setUp()

    def make_daemon(self, stores=None, providers=None):
        daemon = Daemon(stores or self.stores, providers or {
            fixture.CLAUDE: self.claude, fixture.CODEX: self.codex, fixture.FAST: self.fast,
        }, None, 0, face_port=0, workshop_port=0, remote_host=None)
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def owner(self, method, path, body=None, *, daemon=None, port=None, origin=True, headers=None):
        daemon = daemon or self.d
        own = {"Origin": f"http://127.0.0.1:{daemon.face_port}"} if origin else {}
        own.update(headers or {})
        return fixture.http(port or daemon.face_port, method, path, body=body, headers=own)

    @staticmethod
    def path(chat):
        return f"/threads/{chat.id}/desktop-driving"

    def toggle(self, chat, on=True, **kwargs):
        return self.owner("PATCH", self.path(chat), {"on": on}, **kwargs)

    def catalog(self, chat):
        token = fixture.token_of(self.run_brief(chat))
        status, response = fixture.http(self.d.port, "GET", "/mcp/tools", token=token)
        self.assertEqual(status, 200)
        return {tool["name"] for tool in response["tools"]}

    def hold_turn(self, chat, message=None):
        started, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)

        def send(_handle, _message):
            started.set()
            released.wait(5)

        self.fake(chat.provider).script[chat.id] = send
        self.d.send(chat.id, message or UserMessage("look", desk=True))
        self.assertTrue(started.wait(5))
        return released

    def test_owner_gate_and_strict_bodies(self):
        chat = self.chat()
        for path, method, body in ((self.path(chat), "PATCH", {"on": True}),
                                   (self.path(chat) + "/stop", "POST", {}),
                                   ("/desktop-driving", "GET", None)):
            with self.subTest(path=path):
                self.assertEqual(self.owner(method, path, body, origin=False)[0], 403)
                self.assertEqual(self.owner(method, path, body, port=self.d.port,
                                            headers={"Origin": f"http://127.0.0.1:{self.d.port}"})[0], 403)
                self.assertIn(self.owner(method, path, body, port=self.d.workshop_port)[0], (403, 404))
                self.assertEqual(self.owner(method, path, body, headers={"Origin": "http://evil.invalid"})[0], 403)
                status, _ = fixture.http(self.d.face_port, method, path, body=body, headers={
                    "Host": f"example.invalid:{self.d.face_port}",
                    "Origin": f"http://example.invalid:{self.d.face_port}",
                })
                self.assertEqual(status, 403)
        for invalid in (1, "true", None, []):
            self.assertEqual(self.toggle(chat, invalid)[0], 400)
        self.assertEqual(self.owner("GET", "/desktop-driving"),
                         (200, {"enabled": True, "thread_id": None, "badge_showing": False}))
        self.assertEqual(self.owner("GET", "/desktop-driving", origin=False,
                                    headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_concurrent_enable_has_one_winner(self):
        chats = (self.chat(), self.chat(fixture.CODEX))
        ready = threading.Barrier(len(chats))

        def enable(chat):
            ready.wait(timeout=5)
            return self.toggle(chat)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enable, chats))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(chat for chat, (status, _) in zip(chats, results) if status == 200)
        self.assertEqual(self.d.desktop_driving_state()["thread_id"], winner.id)

    def test_exclusive_switch_and_mcp_catalog(self):
        first, second, fast = self.chat(), self.chat(fixture.CODEX), self.chat(fixture.FAST)
        names = hud_window.HUD_WINDOW_TOOLS
        original_choice = (first.model, first.effort, first.speed)
        for chat in (first, second):
            self.assertFalse(self.catalog(chat) & names)
        self.assertEqual(self.toggle(fast)[0], 409)
        self.assertEqual(self.toggle(first),
                         (200, {"enabled": True, "thread_id": first.id, "badge_showing": False}))
        self.assertEqual(self.catalog(first) & names, names)
        self.assertFalse(self.catalog(second) & names)
        status, error = self.toggle(second)
        self.assertEqual(status, 409)
        self.assertIn("Another chat is driving the desktop", error["error"])
        self.assertEqual(self.toggle(second, False)[1]["thread_id"], first.id)
        off_desk = self.hold_turn(first, UserMessage("from phone", via="phone"))
        self.assertFalse(self.catalog(first) & names)
        off_desk.set()
        self.wait_idle(first.id)
        self.assertIsNone(self.toggle(first, False)[1]["thread_id"])
        self.assertEqual(self.toggle(second)[1]["thread_id"], second.id)
        self.assertFalse(self.catalog(first) & names)
        self.assertEqual(self.catalog(second) & names, names)
        saved = self.stores.threads.get(first.id)
        self.assertEqual((saved.model, saved.effort, saved.speed), original_choice)

    def test_off_after_restart_and_master_off_refuses_on(self):
        chat = self.chat()
        self.assertEqual(self.toggle(chat)[0], 200)
        self.d.stop()
        restarted = self.make_daemon()
        self.assertEqual(self.owner("GET", "/desktop-driving", daemon=restarted)[1],
                         {"enabled": True, "thread_id": None, "badge_showing": False})
        with patch.dict(os.environ):
            del os.environ["JARVIS_DESKTOP_DRIVE"]
            # A dotenv file in a test fixture is unable to opt in the daemon.
            (self.root / ".env").write_text("JARVIS_DESKTOP_DRIVE=1\n")
            with patch.object(config, "REPO_ROOT", self.root):
                config._load_dotenv()
            self.assertFalse(config.desktop_drive())
            disabled = self.make_daemon()
            self.assertEqual(self.toggle(chat, daemon=disabled)[0], 409)
            self.assertEqual(self.owner("GET", "/desktop-driving", daemon=disabled)[1],
                             {"enabled": False, "thread_id": None, "badge_showing": False})
        self.assertEqual(self.toggle(chat, daemon=disabled)[0], 409)

    def test_non_driver_calls_all_refused_before_any_side_effect(self):
        first, second = self.chat(), self.chat(fixture.CODEX)
        self.toggle(first)
        self.hold_turn(second)
        token = fixture.token_of(self.run_brief(second))
        with patch.object(hud_window.SESSION, "request") as request, \
                patch.object(self.d.approvals, "ask") as ask, \
                patch.object(hud_window, "_append_audit") as audit:
            for name in sorted(hud_window.HUD_WINDOW_TOOLS):
                with self.subTest(name=name):
                    self.assertEqual(self.call(token, name, {"unexpected": "invalid"}),
                                     (200, {"text": REFUSED, "isError": True}))
            request.assert_not_called()
            ask.assert_not_called()
            audit.assert_not_called()

    def test_pending_approval_cannot_run_even_after_reenable(self):
        chat = self.chat()
        self.toggle(chat)
        self.hold_turn(chat)
        token = fixture.token_of(self.run_brief(chat))
        prompts, release = Queue(), threading.Event()
        wid, ref = "0000000000000001", "1234567890abcdef-e1"
        responses = {
            "windows": {"windows": [{"window": wid, "app": "Catalog.exe", "title": "Fixture", "access": "drivable"}]},
            "window_snapshot": {"snapshot": ref, "refs": {ref: {"name": "Send", "role": "button"}}},
        }

        def answer(request, *, cancelled):
            prompts.put((request, cancelled()))
            self.assertTrue(release.wait(5))
            prompts.put(cancelled())
            return Decision.ALLOW

        with patch.object(hud_window.SESSION, "request", side_effect=lambda op, **_: responses[op]) as request, \
                patch.object(self.d.approvals, "ask", side_effect=answer), \
                ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(self.call(token, "hud_window_snapshot", {"window": wid})[1]["text"], ref)
            pending = pool.submit(self.call, token, "hud_window_click",
                                  {"window": wid, "ref": ref, "purpose": "Send the fixture", "risky": "yes"})
            try:
                approval, cancelled = prompts.get(timeout=5)
                self.assertEqual(approval.thread_id, chat.id)
                self.assertFalse(cancelled)
                self.assertIsNone(self.toggle(chat, False)[1]["thread_id"])
                self.assertEqual(self.toggle(chat)[0], 200)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5), (200, {"text": REFUSED, "isError": True}))
            self.assertTrue(prompts.get(timeout=5))
            self.assertEqual([call.args[0] for call in request.call_args_list], ["windows", "window_snapshot"])
        self.assertFalse((config.V2_DATA_DIR / "desktop_actions.jsonl").exists())

    def test_stop_uses_normal_interrupt_and_idle_is_noop(self):
        first, second = self.chat(), self.chat(fixture.CODEX)
        with patch.object(self.d, "interrupt", wraps=self.d.interrupt) as interrupt:
            self.d.stop_desktop_driving("fixture idle")
            self.toggle(first)
            release = self.hold_turn(first)
            session = self.d._sessions[first.id]
            with patch.object(self.claude, "interrupt", side_effect=lambda _handle: release.set()) as native:
                self.assertEqual(self.owner("POST", self.path(second) + "/stop", {})[1]["thread_id"], first.id)
                interrupt.assert_not_called()
                self.assertIsNone(self.owner("POST", self.path(first) + "/stop", {})[1]["thread_id"])
                interrupt.assert_called_once_with(first.id, expected_turn_id=session.turn_id)
                native.assert_called_with(session.handle)
                self.assertTrue(session.cancelled.is_set())
                self.wait_idle(first.id)
            self.d.stop_desktop_driving("fixture idle again")
            self.toggle(second)
            self.d.stop_desktop_driving("fixture badge")
            self.assertIsNone(self.d.desktop_driving_state()["thread_id"])
            self.assertEqual(interrupt.call_count, 1)

    def test_brief_once_per_switch_on_and_stays_out_of_hud_log(self):
        chat = self.chat()
        sent = []
        self.claude.script[chat.id] = lambda _h, message: sent.append(message.text)

        def send(text, **kwargs):
            self.d.send(chat.id, UserMessage(text, **kwargs))
            self.wait_idle(chat.id)

        send("before", desk=True)
        self.toggle(chat)
        send("from phone", via="phone")
        send("first", desk=True)
        self.toggle(chat)
        send("second", desk=True)
        self.toggle(chat, False)
        send("off", desk=True)
        self.toggle(chat)
        send("third", desk=True)
        self.assertEqual(sent, ["before", "from phone", DRIVER_BRIEF + "\n\nfirst",
                                # Lead: the brief now goes with every owner desk turn while driving.
                                DRIVER_BRIEF + "\n\nsecond", "off",
                                DRIVER_BRIEF + "\n\nthird"])
        logged = [row["data"]["text"] for row in self.stores.threads.read_log(chat.id) if row["kind"] == "user"]
        self.assertEqual(logged, ["before", "from phone", "first", "second", "off", "third"])
        self.assertTrue(files._self_protected(config.REPO_ROOT / "jarvis/v2/desktop_driver.py"))

    def test_archive_revokes_and_restore_does_not_enable(self):
        chat = self.chat()
        self.toggle(chat)
        self.assertEqual(self.owner("POST", f"/threads/{chat.id}/archive", {})[0], 200)
        self.assertIsNone(self.d.desktop_driving_state()["thread_id"])
        self.assertEqual(self.toggle(chat)[0], 409)
        self.assertEqual(self.owner("POST", f"/threads/{chat.id}/restore", {})[0], 200)
        self.assertIsNone(self.d.desktop_driving_state()["thread_id"])
        self.assertEqual(self.toggle(chat)[0], 200)
        project = f"/projects/{chat.project_id}"
        impact = self.owner("GET", project + "/impact")[1]
        self.assertEqual(self.owner("POST", project + "/archive?expect=" + impact["token"], {})[0], 200)
        self.assertIsNone(self.d.desktop_driving_state()["thread_id"])
        self.assertEqual(self.toggle(chat)[0], 409)

    def test_native_catalog_notification_through_relay(self):
        chat = self.chat(fixture.CODEX)
        entry = fixture.entry_of(self.run_brief(chat))
        self.assertEqual(entry["env"][mcp_session.WATCH_WINDOWS_VARIABLE], "1")
        features = tomllib.loads(codex_config.config_text(self.run_brief(chat)))["features"]
        self.assertIs(features["mcp_tool_list_changed"], True)
        relay = mcp_session.Relay(fixture.token_of(self.run_brief(chat)), self.d.port, watch_windows=True)
        inputs, outputs = Queue(), Queue()

        class Sink(io.StringIO):
            def write(self, data):
                outputs.put(json.loads(data))

        def command(method, **params):
            inputs.put(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
            return outputs.get(timeout=5)["result"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            server = pool.submit(mcp_session.serve, relay, iter(inputs.get, None), Sink())
            try:
                self.assertEqual(command("initialize", protocolVersion="2025-06-18")["capabilities"],
                                 {"tools": {"listChanged": True}})
                for on in (False, True, False):
                    if on or self.d.desktop_driving_state()["thread_id"]:
                        self.assertEqual(self.toggle(chat, on)[0], 200)
                        self.assertEqual(outputs.get(timeout=5)["method"], "notifications/tools/list_changed")
                    names = {tool["name"] for tool in command("tools/list")["tools"]}
                    self.assertEqual(names & hud_window.HUD_WINDOW_TOOLS, hud_window.HUD_WINDOW_TOOLS if on else set())
            finally:
                inputs.put(None)
            server.result(timeout=5)

    def test_driver_chat_uses_existing_pinned_model_settings_without_enabling(self):
        model = "gpt-6.2-sol"
        body = {"project_id": self.project.id, "role": Role.CHAT.value, "provider": "codex",
                "brief": {"model": model, "effort": "medium", "speed": "ultrafast"}}
        with patch.dict(router.CLI_MODELS["codex"], {model: {"efforts": ("medium",),
                        "speeds": (("ultrafast", "Ultrafast"),)}}):
            status, created = self.owner("POST", "/threads", body)
        self.assertEqual(status, 201, created)
        self.assertEqual((created["model"], created["effort"], created["speed"], created["speed_model"]),
                         (model, "medium", "ultrafast", model))
        self.assertIsNone(self.d.desktop_driving_state()["thread_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Independent CU-3b contract checks with fake providers and ephemeral listeners."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
import http.client
import json
import os
from pathlib import Path
from queue import Queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Ignore repository dotenv files on first import. Tests of the loader below
# point REPO_ROOT at a fixture and call the real loader without this patch.
_exists = Path.exists
with patch.object(Path, "exists", lambda path: path.name != ".env" and _exists(path)):
    import mcp_session_check as fixture

from jarvis import config, tools
from jarvis.v2.mcp_catalog import MCP_TOOLS
from jarvis.v2 import mcp_session, router
from jarvis.v2.daemon import Daemon
from jarvis.v2.desk import SelectedVoiceMessage
from jarvis.v2.desktop_driver import REFUSED
from jarvis.v2.provider import Decision, Event, EventKind, UserMessage
from jarvis.v2.providers import claude, codex
from jarvis.v2.stores import Stores
from jarvis.v2.tools import hud_window

NAMES = hud_window.HUD_WINDOW_TOOLS
WINDOW = "0000000000000001"
REF = "1234567890abcdef-e1"
CLICK = {"window": WINDOW, "ref": REF, "purpose": "Click the fixture button", "risky": "yes"}
HOST = "fixture.test.ts.net"
BRIEF = (
    '[Desktop driving instructions]\n'
    'List windows first with hud_window_list. Take a snapshot before acting and act by ref. '
    'Give every acting call a purpose. Set risky to "yes" for anything that sends, submits, '
    'pays, deletes, installs or changes system settings, and whenever unsure. '
    'Terminals are see-only here. Use the terminal tool. '
    'A refusal is final. Do not look for another route to the same protected window. '
    'If a step is refused twice, stop and tell the owner.'
)


class Review(fixture.DaemonHarness):
    def setUp(self):
        master = patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"})
        master.start()
        self.addCleanup(master.stop)
        super().setUp()

    def new_daemon(self, stores):
        return Daemon(stores, {p: self.fake(p) for p in (fixture.CLAUDE, fixture.CODEX, fixture.FAST)},
                      None, 0, face_port=0, workshop_port=0, remote_port=0, remote_host=HOST)

    def make_daemon(self):
        self.stack.enter_context(patch.object(config, "voice_link_key", return_value=self.root / "no-pair"))
        daemon = self.new_daemon(self.stores)
        # The synthetic phone listener needs no external security probes.
        self.stack.enter_context(patch.object(daemon.secure, "start"))
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def hud(self, method, path, body=None):
        return fixture.http(self.d.face_port, method, path, body=body,
                            headers={"Origin": f"http://127.0.0.1:{self.d.face_port}"})

    @staticmethod
    def strict_request(port, method, path, host, origin, body):
        # A single explicit Host is required to reach the phone route table.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, json.dumps(body), {
                "Host": host, "Origin": origin, "Content-Type": "application/json",
            })
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def phone(self, method, path, body):
        return self.strict_request(self.d.remote_port, method, path, HOST, f"https://{HOST}", body)

    @staticmethod
    def path(chat):
        return f"/threads/{chat.id}/desktop-driving"

    def driving(self, chat, on=True):
        return self.hud("PATCH", self.path(chat), {"on": on})

    def driver(self):
        return self.d.desktop_driving_state()["thread_id"]

    def token(self, chat):
        return fixture.token_of(self.run_brief(chat))

    def catalog(self, chat):
        status, body = fixture.http(self.d.port, "GET", "/mcp/tools", token=self.token(chat))
        self.assertEqual(status, 200, body)
        return {row["name"] for row in body["tools"]} & NAMES

    def mcp(self, chat, name, arguments, context=None):
        body = {"name": name, "arguments": arguments}
        if context is not None:
            body["codex_context"] = context
        return fixture.http(self.d.port, "POST", "/mcp/call", token=self.token(chat), body=body)

    def hold(self, chat):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def send(_handle, _message):
            entered.set()
            release.wait(10)

        self.fake(chat.provider).script[chat.id] = send
        self.d.send(chat.id, UserMessage("fixture", desk=True))
        self.assertTrue(entered.wait(5))
        return release

    @contextmanager
    def native_codex(self, chat):
        handle = self.d._sessions[chat.id].handle
        original = handle.native
        native = SimpleNamespace(thread_id="native-thread", turn_id="native-turn", mcp_calls=set(),
                                 closed=threading.Event(), cancelled=threading.Event(),
                                 mutex=threading.RLock(), sending=threading.Lock())
        handle.native = native
        provider = object.__new__(codex.CodexProvider)
        try:
            with native.sending, patch.object(self.codex, "admit_foreground_mcp",
                                              provider.admit_foreground_mcp, create=True):
                yield native
        finally:
            handle.native = original

    @staticmethod
    def windows(op, **_fields):
        if op == "windows":
            return {"windows": [{"window": WINDOW, "app": "Catalog.exe", "title": "Fixture",
                                 "access": "drivable"}]}
        if op == "window_snapshot":
            return {"snapshot": REF, "refs": {REF: {"name": "Send", "role": "button"}}}
        raise AssertionError(f"Unexpected bridge call: {op}")

    def test_a_catalog_requires_both_and_fresh_daemon_has_no_grant(self):
        chats = [self.chat(fixture.CLAUDE), self.chat(fixture.CODEX)]
        saved = asdict(self.stores.threads.get(chats[0].id))
        self.assertEqual([self.catalog(chat) for chat in chats], [set(), set()])
        self.assertEqual(self.driving(chats[0])[0], 200)
        self.assertEqual([self.catalog(chat) for chat in chats], [NAMES, set()])
        self.assertEqual(asdict(self.stores.threads.get(chats[0].id)), saved)
        cold = self.new_daemon(Stores())
        self.assertEqual(cold.desktop_driving_state(), {"enabled": True, "thread_id": None})

        with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "0"}):
            self.assertEqual([self.catalog(chat) for chat in chats], [set(), set()])
            disabled_at_launch = self.new_daemon(self.stores)
        self.assertEqual(disabled_at_launch.desktop_driving_state(), {"enabled": False, "thread_id": None})

    # Lead: known edge case E20 (plan section 11).
    @unittest.expectedFailure
    def test_a_relay_invalidates_last_catalog_after_missed_toggle_pair(self):
        chat = self.chat(fixture.CODEX)
        relay = mcp_session.Relay(self.token(chat), self.d.port, watch_windows=True)
        request = relay._request
        watching, release, observed = threading.Event(), threading.Event(), threading.Event()
        inputs, outputs = Queue(), Queue()

        def exchange(method, path, body, timeout, *, lifetime=None):
            if not path.startswith("/mcp/window-tools"):
                return request(method, path, body, timeout, lifetime=lifetime)
            if watching.is_set():
                # The watcher has processed its previous reply. Hold the
                # next poll so the assertion sees exactly that outcome.
                observed.set()
                lifetime.cancelled.wait(5)
                return None
            watching.set()
            release.wait(5)
            return request(method, path, body, timeout, lifetime=lifetime)

        class Sink:
            def write(self, data):
                outputs.put(json.loads(data))

            def flush(self):
                pass

        def list_tools():
            inputs.put(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
            rows = outputs.get(timeout=5)["result"]["tools"]
            return {row["name"] for row in rows} & NAMES

        with patch.object(relay, "_request", side_effect=exchange), \
                patch.object(self.d.desktop_driver.changed, "wait", return_value=False), \
                ThreadPoolExecutor(max_workers=1) as pool:
            serving = pool.submit(mcp_session.serve, relay, iter(inputs.get, None), Sink())
            try:
                self.assertEqual(list_tools(), set())
                self.assertTrue(watching.wait(5))
                self.assertEqual(self.driving(chat)[0], 200)
                self.assertEqual(list_tools(), NAMES)
                self.assertEqual(self.driving(chat, False)[0], 200)
                release.set()
                self.assertTrue(observed.wait(5))
                self.assertFalse(outputs.empty(), "the client last received an on catalog and got no invalidation")
                self.assertEqual(outputs.get_nowait()["method"], "notifications/tools/list_changed")
            finally:
                release.set()
                inputs.put(None)
                serving.result(timeout=5)

    def test_b_dotenv_cannot_enable_the_master(self):
        with patch.dict(os.environ), patch.object(config, "REPO_ROOT", self.root):
            del os.environ["JARVIS_DESKTOP_DRIVE"]
            (self.root / ".env").write_text("JARVIS_DESKTOP_DRIVE=1\nCU3B_FIXTURE=loaded\n")
            config._load_dotenv()
            self.assertFalse(config.desktop_drive())
            self.assertEqual(os.environ["CU3B_FIXTURE"], "loaded")

    def test_b_h_phone_and_nonowner_requests_cannot_toggle_either_switch(self):
        chat = self.chat()
        paths = ((self.path(chat), {"on": True}), ("/browser/settings", {"agents_may_drive": True}))
        face, api, workshop = self.d.face_port, self.d.port, self.d.workshop_port
        for path, body in paths:
            for port, headers, expected in (
                (face, {}, {403}),
                (face, {"Origin": "null"}, {403}),
                (face, {"Origin": "http://untrusted.test"}, {403}),
                (api, {"Origin": f"http://127.0.0.1:{api}"}, {403}),
                (workshop, {"Origin": f"http://127.0.0.1:{workshop}"}, {403, 404}),
            ):
                with self.subTest(path=path, port=port, headers=headers):
                    status, _ = fixture.http(port, "PATCH", path, body=body, headers=headers)
                    self.assertIn(status, expected)
            for port, host, origin, expected in (
                (face, f"untrusted.test:{face}", f"http://untrusted.test:{face}", 403),
                (self.d.remote_port, HOST, f"https://{HOST}", 404),
            ):
                with self.subTest(path=path, host=host):
                    status, response = self.strict_request(port, "PATCH", path, host, origin, body)
                    self.assertEqual(status, expected, response)
        self.assertIsNone(self.driver())
        for value in (True, False):
            status, state = self.hud("PATCH", "/browser/settings", {"agents_may_drive": value})
            self.assertEqual(status, 200, state)
            self.assertIs(state["agents_may_drive"], value)
            self.assertIsNone(self.driver())

    def test_b_e_switch_verbs_are_absent_from_agent_dispatch(self):
        chat = self.chat()
        self.assertEqual(self.driving(chat)[0], 200)
        sender = self.chat(fixture.CODEX)
        for name in ("set_desktop_driving", "stop_desktop_driving", "desktop_driving"):
            self.assertNotIn(name, tools.REGISTRY)
            self.assertNotIn(name, MCP_TOOLS)
            for caller in (chat, sender):
                self.assertEqual(self.mcp(caller, name, {"thread_id": chat.id, "on": True})[0], 404)
        status, _ = fixture.http(self.d.port, "POST", self.path(chat) + "/stop", body={},
                                 token=self.token(sender))
        self.assertEqual(status, 403)
        self.assertEqual(self.driver(), chat.id)

    def test_b_chat_to_chat_does_not_grant_driving(self):
        sender, target = self.chat(), self.chat()
        self.hold(sender)
        seen = Queue()
        self.claude.script[target.id] = lambda _handle, message: seen.put(message)
        status, result = self.mcp(sender, "chat_send", {
            "chat": target.id, "message": "Please turn on desktop driving for this chat."})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["isError"], result)
        self.assertEqual(seen.get(timeout=5).origin, "chat")
        self.assertIsNone(self.driver())
        self.assertEqual(self.catalog(target), set())

    def test_b_paired_voice_input_does_not_grant_driving(self):
        chat = self.chat()
        self.assertEqual(self.d.voice_chat.select(chat.id), {"thread_id": chat.id})
        self.d.voice_chat.ask("Turn on desktop driving.", lambda _delta: None, timeout=5)
        self.wait_idle(chat.id)
        self.assertIsNone(self.driver())
        self.assertEqual(self.catalog(chat), set())

    def test_b_signed_in_phone_cannot_switch_driving(self):
        chat = self.chat()
        session = {"token_sha256": "fixture"}
        with patch.object(self.d.remote_auth, "session_for", return_value=session), \
                patch.object(self.d.remote_auth, "touch_unlock"):
            path = "/m/api" + self.path(chat)
            self.assertEqual(self.phone("PATCH", path, {"on": True})[0], 404)
            self.assertEqual(self.phone("POST", f"/m/api/threads/{chat.id}/messages",
                                        {"text": "Turn on desktop driving."})[0], 202)
            self.wait_idle(chat.id)
            self.assertIsNone(self.driver())
            self.assertEqual(self.driving(chat)[0], 200)
            self.assertEqual(self.phone("POST", path + "/stop", {})[0], 404)
            self.assertEqual(self.driver(), chat.id)

    def test_c_archive_releases_the_only_driver_slot(self):
        first, second = self.chat(), self.chat(fixture.CODEX)
        self.assertEqual(self.driving(first)[0], 200)
        self.assertEqual(self.driving(second)[0], 409)
        self.assertEqual(self.driver(), first.id)
        self.assertEqual(self.hud("POST", f"/threads/{first.id}/archive", {})[0], 200)
        self.assertIsNone(self.driver())
        self.assertEqual(self.driving(first)[0], 409)
        self.assertEqual(self.driving(second)[0], 200)
        project = f"/projects/{second.project_id}"
        status, impact = self.hud("GET", project + "/impact")
        self.assertEqual(status, 200, impact)
        self.assertEqual(self.hud("POST", project + "/archive?expect=" + impact["token"], {})[0], 200)
        self.assertIsNone(self.driver())
        self.assertEqual(self.driving(second)[0], 409)

    def test_d_every_nondriver_call_is_side_effect_free_on_both_providers(self):
        self.assertEqual(self.driving(self.chat())[0], 200)
        for provider in (fixture.CLAUDE, fixture.CODEX):
            chat = self.chat(provider)
            release = self.hold(chat)
            with patch.object(hud_window.SESSION, "request") as request, \
                    patch.object(self.d.approvals, "ask") as ask, \
                    patch.object(hud_window, "_append_audit") as audit:
                for name in sorted(NAMES):
                    with self.subTest(provider=provider, name=name):
                        self.assertEqual(self.mcp(chat, name, {"bad": 1}),
                                         (200, {"text": REFUSED, "isError": True}))
                request.assert_not_called()
                ask.assert_not_called()
                audit.assert_not_called()
            release.set()
            self.wait_idle(chat.id)

    def test_d_claude_hook_denies_child_and_revoked_parent_before_permit(self):
        chat = self.chat()
        self.driving(chat)
        provider = object.__new__(claude.ClaudeProvider)
        session = SimpleNamespace(brief=self.run_brief(chat), permit=self.claude.opened[-1].permit,
                                  denied_tools=set(), child_denied=OrderedDict(), mutex=threading.Lock())
        hook = provider._pre_tool_hook(session)

        with patch.object(provider, "_decide") as decide:
            for agent in (True, False):
                if not agent:
                    self.driving(chat, False)
                for name in sorted(NAMES):
                    metadata = {"tool_name": "mcp__jarvis__" + name}
                    if agent:
                        metadata["agent_id"] = "fixture-child"
                    result = asyncio.run(hook(metadata, name, None))["hookSpecificOutput"]
                    self.assertEqual(result["permissionDecision"], "deny", result)
            decide.assert_not_called()

    def test_d_codex_child_denied_and_native_parent_can_list(self):
        chat = self.chat(fixture.CODEX)
        self.driving(chat)
        self.hold(chat)
        relay = mcp_session.Relay(self.token(chat), self.d.port)

        def call(native, name, child):
            meta = {"thread_id": native.thread_id, "turn_id": native.turn_id}
            if child:
                meta["parent_thread_id"] = "fixture-parent"
            response = mcp_session.handle(relay, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": {}, "_meta": {
                    "threadId": native.thread_id, "callId": name, "x-codex-turn-metadata": meta,
                }}})
            self.assertIn("result", response)
            return response["result"]

        with self.native_codex(chat) as native, \
                patch.object(hud_window.SESSION, "request", side_effect=self.windows) as request, \
                patch.object(self.d.approvals, "ask") as ask, \
                patch.object(hud_window, "_append_audit") as audit:
            for name in sorted(NAMES):
                result = call(native, name, True)
                self.assertTrue(result["isError"], result)
                self.assertIn("foreground", result["content"][0]["text"])
            request.assert_not_called()
            ask.assert_not_called()
            audit.assert_not_called()
            result = call(native, "hud_window_list", False)
            self.assertFalse(result["isError"], result)
            self.assertIn("Catalog.exe", result["content"][0]["text"])
            request.assert_called_once()

    def test_d_pending_approval_is_dead_after_off_reenable_or_transfer(self):
        self.check_pending_revocation(self.chat(), self.chat())
        codex_chat, other = self.chat(fixture.CODEX), self.chat()
        with self.native_codex(codex_chat) as native:
            self.check_pending_revocation(codex_chat, other, native)

    def check_pending_revocation(self, first, second, native=None):
        self.hold(first)

        def call(name, arguments, call_id):
            context = None if native is None else {
                "thread_id": native.thread_id, "turn_id": native.turn_id,
                "subagent": False, "call_id": call_id,
            }
            return self.mcp(first, name, arguments, context)

        for transition in ("off", "reenable", "transfer"):
            with self.subTest(provider=first.provider, transition=transition):
                self.assertEqual(self.driving(first)[0], 200)
                waiting, answer = Queue(), threading.Event()

                def approve(request, *, cancelled):
                    waiting.put((request.thread_id, cancelled()))
                    answer.wait(5)
                    waiting.put(cancelled())
                    return Decision.ALLOW

                with patch.object(hud_window.SESSION, "request", side_effect=self.windows) as request, \
                        patch.object(self.d.approvals, "ask", side_effect=approve), \
                        patch.object(hud_window, "_append_audit") as audit, \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(call("hud_window_snapshot", {"window": WINDOW}, transition + "-look")[1]["text"], REF)
                    pending = pool.submit(call, "hud_window_click", CLICK, transition + "-click")
                    try:
                        thread_id, cancelled = waiting.get(timeout=5)
                        self.assertEqual(thread_id, first.id)
                        self.assertFalse(cancelled)
                        self.assertEqual(self.driving(first, False)[0], 200)
                        if transition != "off":
                            self.assertEqual(self.driving(first if transition == "reenable" else second)[0], 200)
                    finally:
                        answer.set()
                    self.assertEqual(pending.result(timeout=5), (200, {"text": REFUSED, "isError": True}))
                    self.assertTrue(waiting.get(timeout=5))
                    self.assertNotIn("window_click", [call.args[0] for call in request.call_args_list])
                    audit.assert_not_called()
                self.driving(first, False)
                self.driving(second, False)

    def test_d_driver_cannot_use_previous_drivers_refs(self):
        first, second = self.chat(), self.chat()
        self.hold(first)
        self.hold(second)
        self.driving(first)
        with patch.object(hud_window.SESSION, "request", side_effect=self.windows) as request:
            self.assertEqual(self.mcp(first, "hud_window_snapshot", {"window": WINDOW})[1]["text"], REF)
            self.driving(first, False)
            self.driving(second)
            request.reset_mock()
            status, result = self.mcp(second, "hud_window_click", CLICK)
            self.assertEqual(status, 200, result)
            self.assertTrue(result["isError"], result)
            self.assertIn("fresh snapshot", result["text"])
            request.assert_not_called()

    def test_e_both_stop_entries_revoke_before_interrupting_and_are_idle_safe(self):
        for route in (True, False):
            with self.subTest(route=route):
                chat = self.chat()
                self.driving(chat)
                release = self.hold(chat)
                observed = []

                def interrupted(_handle):
                    observed.append(self.driver())
                    release.set()

                def stop():
                    if route:
                        self.assertEqual(self.hud("POST", self.path(chat) + "/stop", {}),
                                         (200, {"enabled": True, "thread_id": None}))
                    else:
                        self.d.stop_desktop_driving("review")

                with patch.object(self.claude, "interrupt", side_effect=interrupted), \
                        patch.object(self.d, "interrupt", wraps=self.d.interrupt) as interrupt:
                    stop()
                    self.wait_idle(chat.id)
                    stop()
                    interrupt.assert_called_once()
                self.assertTrue(observed)
                self.assertEqual(set(observed), {None})

    def test_f_brief_is_fixed_once_and_never_goes_to_other_or_voice_turns(self):
        owner, other = self.chat(), self.chat()
        self.driving(owner)
        sent = []
        for chat in (owner, other):
            self.claude.script[chat.id] = lambda handle, message: sent.append((handle.thread_id, message.text))

        def send(chat, message):
            self.d.send(chat.id, message)
            self.wait_idle(chat.id)

        send(owner, SelectedVoiceMessage("voice fixture"))
        send(owner, UserMessage("phone fixture", via="phone"))
        send(other, UserMessage("other fixture", desk=True))
        send(owner, UserMessage("owner fixture", desk=True))
        self.driving(owner)
        send(owner, UserMessage("next fixture", desk=True))
        self.driving(owner, False)
        self.driving(owner)
        send(owner, UserMessage("second grant fixture", desk=True))
        self.assertEqual(sent, [
            (owner.id, "voice fixture"), (owner.id, "phone fixture"), (other.id, "other fixture"),
            (owner.id, BRIEF + "\n\nowner fixture"), (owner.id, BRIEF + "\n\nnext fixture"),  # lead: every owner desk turn while driving
            (owner.id, BRIEF + "\n\nsecond grant fixture"),
        ])

    def test_f_provider_refusal_before_delivery_preserves_the_pending_brief(self):
        chat = self.chat()
        self.driving(chat)

        def reject_before_delivery(handle, _message):
            yield Event(EventKind.ERROR, handle.thread_id, {"message": "turn start rejected", "fatal": False})

        with patch.object(self.claude, "send", side_effect=reject_before_delivery):
            self.d.send(chat.id, UserMessage("rejected at turn start", desk=True))
            self.wait_idle(chat.id)
        received = []
        self.claude.script[chat.id] = lambda _handle, message: received.append(message.text)
        self.d.send(chat.id, UserMessage("accepted turn", desk=True))
        self.wait_idle(chat.id)
        self.assertEqual(received, [BRIEF + "\n\naccepted turn"])

    # Lead: known edge case E21 (plan section 11).
    @unittest.expectedFailure
    def test_g_preset_creates_pinned_chat_with_builtin_catalog(self):
        # Exact payload of api.newDesktopDriverChat. The builtin catalog is
        # the normal state when the account catalog has not been captured.
        body = {"project_id": self.project.id, "role": "chat", "provider": "codex",
                "brief": {"model": "gpt-6.2-sol", "effort": "medium", "speed": "ultrafast"}}
        with patch.dict(router.CLI_MODELS, {"codex": dict(router.CODEX_FALLBACK)}):
            status, chat = self.hud("POST", "/threads", body)
        self.assertEqual(status, 201, chat)
        self.assertEqual((chat["provider"], chat["model"], chat["effort"], chat["speed"]),
                         ("codex", "gpt-6.2-sol", "medium", "ultrafast"))
        self.assertIsNone(self.driver())

    def test_h_fast_catalog_excludes_windows_even_when_explicitly_requested(self):
        from jarvis.v2 import bridge
        self.assertFalse({row["name"] for row in bridge.listing("fast", window_tools=True)} & NAMES)


if __name__ == "__main__":
    unittest.main(verbosity=2)

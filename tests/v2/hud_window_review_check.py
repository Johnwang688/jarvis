"""Independent Windows HUD contract checks. All transport and owner state is synthetic."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextvars
import json
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


def forbid_live_io(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        if Path(os.fsdecode(args[0])).name.startswith(".env"):
            raise AssertionError("the review must never open an environment file")
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"}:
        raise AssertionError("the review must never use a socket or start a process")


sys.addaudithook(forbid_live_io)

from jarvis import config, runtime, tools
from jarvis.desktop import DesktopError
from jarvis.tools import files, secrets
from jarvis.v2 import bridge, inbox, remote_answer
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.browser import drive
from jarvis.v2.browser.drive_state import DriveTurn
from jarvis.v2.daemon import APIError, Daemon
from jarvis.v2.hud_sensitive import Reports
from jarvis.v2.model import ProviderName, Role
from jarvis.v2.provider import Decision
from jarvis.v2.providers import claude, codex, fastpath
from jarvis.v2.stores import Stores
from jarvis.v2.tools import hud_browser_act, hud_desktop, hud_window as window


PRIMARY = "a79f0b3d521ee301"
SECONDARY = "a79f0b3d521ee302"
EDIT = "66d40c77b6153189-e4"
QUEUE = "66d40c77b6153189-e8"
NEW = "d77b61531896640c-e4"
EDITOR = {"name": "Filter by author", "role": "editable"}
BUTTON = {"name": "Send invitation", "role": "button"}
PURPOSE = "Find the requested author."
TYPED = "Synthetic violet orchard 73"
INTERNAL = "SYNTHETIC_TRANSPORT_DETAIL"
COMPLETED = "Action completed. Take a fresh snapshot to see the result."
ACTING = {"window_click", "window_key", "window_scroll", "window_type"}
NAMES = {"hud_" + op for op in ACTING | {"window_list", "window_snapshot", "window_screenshot"}}


def isolate(case, target, name, *args, **kwargs):
    guard = patch.object(target, name, *args, **kwargs)
    case.addCleanup(guard.stop)
    return guard.start()


def fake_session(provider=ProviderName.CODEX):
    chat = NS(id="review-chat", project_id="review-project", role=Role.CHAT,
              task_id=None, provider=provider)
    session = NS(thread=chat, brief=NS(role=Role.CHAT, task_id=None), worker=object(),
                 turn_id="turn-a", caller_key=object(), handle=object(), closing=False,
                 retired=False, lost=False, desk=NS(slot={"present": True, "images": True}),
                 cancelled=threading.Event(), permit=Mock(return_value=Decision.ALLOW),
                 provider=NS(admit_foreground_mcp=Mock(return_value=lambda: True)),
                 drive_turn=NS(closed=False))
    caller = NS(thread_id=chat.id, provider=provider.value, session=session)
    return session, caller


class Review(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ["HOME"])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.logfile = self.root / "desktop_actions.jsonl"
        isolate(self, config, "V2_DATA_DIR", self.root)
        isolate(self, secrets, "secret_values", return_value=[])
        env = patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.daemon = object.__new__(Daemon)
        self.daemon._lock = threading.RLock()
        self.daemon._stopping = False
        from jarvis.v2.desktop_driver import DesktopDriver
        self.daemon.desktop_driver = DesktopDriver(self.daemon._lock)
        self.daemon.desktop_driver.set("review-chat", True)
        self.daemon.hud_sensitive_reports = Reports()
        self.addCleanup(self.daemon.hud_sensitive_reports.close)
        self.service = window.WindowDrive(self.daemon)
        self.service.install()
        self.addCleanup(self.service.stop)
        self.daemon.approvals = PendingApprovals(timeout_s=0.05)
        self.calls = []
        self.cards = []
        self.responses = {}
        self.stopped = False
        isolate(self, window.SESSION, "request", self.exchange)

    def drive_chat(self, thread_id):
        self.daemon.desktop_driver.clear()
        self.daemon.desktop_driver.set(thread_id, True)

    def exchange(self, op, **kw):
        self.calls.append((op, kw))
        if op in self.responses:
            response = self.responses[op]
            return response(kw) if callable(response) else response
        if op == "windows":
            return {"windows": [
                {"window": PRIMARY, "app": "Catalog.exe", "title": "Mystery Chat", "access": "drivable"},
                {"window": SECONDARY, "app": "Shelf.exe", "title": "Unrelated", "access": "drivable"},
            ]}
        if op == "window_snapshot":
            ref = EDIT if kw["window"] == PRIMARY else NEW
            return {"snapshot": f"Controls {ref} and {QUEUE}", "refs": {ref: EDITOR, QUEUE: BUTTON}}
        if op == "window_screenshot":
            return {"image_b64": "c3ludGhldGlj"}
        if op == "window_probe":
            return {**EDITOR, "rect": [10, 20, 50, 60]}
        return {"internal": INTERNAL}

    def invoke(self, name, args=None, *, thread="review-chat", turn="turn-a",
               provider="codex", desk=True, paired=False, depth=0):
        def run():
            runtime.bind(caller={"thread_id": thread, "turn_id": turn, "provider": provider},
                         desk={"present": desk, "images": True, "hud_desktop": paired},
                         depth=depth, should_stop=lambda: self.stopped, tool_names=NAMES)
            return tools.dispatch("hud_window_" + name, json.dumps({} if args is None else args))
        return contextvars.Context().run(run)

    def act(self, op, *, on=PRIMARY, via=None, **args):
        return self.invoke(op, {"window": on, "purpose": PURPOSE, "risky": "no", **args},
                           **(via or {})).text

    def snapshot(self, on=PRIMARY, **via):
        return self.invoke("snapshot", {"window": on}, **via).text

    def actions(self):
        return [(op, fields) for op, fields in self.calls if op in ACTING]

    def audit(self):
        return [json.loads(line) for line in self.logfile.read_text().splitlines()] if self.logfile.exists() else []

    def broker(self, present):
        def record(request):
            self.cards.append(request)
            present(request)
        self.daemon.approvals = PendingApprovals(timeout_s=0.05, on_request=record)
        return self.daemon.approvals

    def test_01_flag_flip_after_listing_and_inside_approval(self):
        for provider in ProviderName:
            names = {row["name"] for row in bridge.listing(provider, window_tools=True)}
            self.assertEqual(names & NAMES, NAMES if provider != ProviderName.FAST else set())
        listed = self.snapshot()
        self.assertIn(QUEUE, listed)
        def disable_and_approve(req):
            del os.environ["JARVIS_DESKTOP_DRIVE"]
            self.daemon.approvals.resolve(req.req_id, Decision.ALLOW, owner_verified=True)

        self.broker(disable_and_approve)
        self.assertIn("Refused:", self.act("click", ref=QUEUE))
        for provider in ProviderName:
            self.assertFalse(NAMES & {row["name"] for row in bridge.listing(provider)})
        count = len(self.calls)
        self.assertIn("off", self.act("click", ref=QUEUE))
        self.assertIn("off", window.hud_window_list())
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.actions(), [])

    def test_02_context_and_session_gates(self):
        for via in ({"depth": 2}, {"turn": None}, {"desk": False, "paired": True},
                    {"provider": "fast", "paired": True}, {"provider": "voice"}):
            with self.subTest(via=via):
                self.assertIn("Refused:", self.invoke("list", **via).text)
                self.assertIn("Refused:", self.act("key", keys=["Tab"], via=via))
        self.stopped = True
        self.assertIn("Refused:", self.act("key", keys=["Tab"]))
        self.stopped = False
        session, caller = fake_session()
        self.daemon._sessions = {caller.thread_id: session}
        args = {"window": PRIMARY, "purpose": PURPOSE, "risky": "no", "keys": ["Tab"]}
        for obj, attr, value in ((session.thread, "task_id", "job"), (session.brief, "task_id", "job"),
                                 (session.thread, "role", Role.REVIEWER), (session, "worker", None)):
            with self.subTest(field=attr), patch.object(obj, attr, value):
                if attr == "worker":
                    self.assertTrue(bridge.call(self.daemon, session, caller, "hud_window_key", args)["isError"])
                else:
                    with self.assertRaises(APIError):
                        bridge.call(self.daemon, session, caller, "hud_window_key", args)
        with patch.object(session.provider, "admit_foreground_mcp", return_value=None):
            self.assertTrue(bridge.call(self.daemon, session, caller, "hud_window_list", {})["isError"])
        self.assertTrue(bridge.call(self.daemon, session, caller, "hud_window_list", {},
                                    request_alive=lambda: False)["isError"])
        self.assertEqual(self.calls, [])
        result = bridge.call(self.daemon, session, caller, "hud_window_key", args)
        self.assertFalse(result["isError"], result["text"])

    def test_02_claude_subagent_is_denied_and_legacy_admission_is_unchanged(self):
        provider = object.__new__(claude.ClaudeProvider)
        session = NS(brief=NS(role=Role.CHAT, task_id=None, mcp_servers={"jarvis": {}}),
                     denied_tools=set(), child_denied=OrderedDict(), mutex=threading.Lock(),
                     permit=NS(desktop_driver_grant=lambda: self.daemon.desktop_driver.grant("review-chat")))
        hook = provider._pre_tool_hook(session)

        def run(tool, **metadata):
            outcome = asyncio.run(hook({"tool_name": "mcp__jarvis__" + tool, **metadata}, "call-id", None))
            return outcome["hookSpecificOutput"]["permissionDecision"]

        with patch.object(provider, "_decide", return_value=Decision.ALLOW) as permit, \
             patch.object(drive, "admission_for_tool", return_value=False) as browser_admission:
            self.assertEqual(run("hud_window_snapshot", agent_id="worker"), "deny")
            permit.assert_not_called()
            self.assertEqual(run("hud_window_snapshot"), "allow")
            browser_admission.assert_not_called()
            permit.reset_mock()
            self.daemon.desktop_driver.clear()
            self.assertEqual(run("hud_window_snapshot"), "deny")
            permit.assert_not_called()
            self.assertEqual(run("hud_browser_act"), "deny")
            browser_admission.assert_called_once()
            self.assertEqual(run("hud_desktop_type"), "allow")

    def test_03_invalid_shapes_fail_before_any_transport(self):
        cases = [
            ("click", {"on": PRIMARY.upper(), "x": 0, "y": 0}),
            ("click", {"on": [PRIMARY], "x": 0, "y": 0}),
            ("click", {"on": PRIMARY + "\n", "x": 0, "y": 0}),
            ("click", {"ref": None, "x": 0, "y": 0}),
            ("click", {"x": 0, "y": 0.0}),
            ("scroll", {"amount": True, "x": 0, "y": 0}),
            ("scroll", {"amount": -100, "ref": EDIT, "x": 0, "y": 0}),
            ("scroll", {"amount": -100, "y": 0}),
            ("key", {"keys": [{}, "tab"]}),
            ("key", {"keys": []}),
            ("key", {"keys": ["ALT", "Alt"]}),
            ("key", {"keys": ["F24", "win"]}),
            ("key", {"keys": ["K"]}),
            ("type", {"ref": EDIT, "text": ["value"]}),
            ("type", {"ref": EDIT, "text": "a" * 2001}),
            ("type", {"ref": EDIT, "text": "a\u2029b"}),
            ("type", {"ref": EDIT, "text": "a\x7fb"}),
            ("type", {"ref": EDIT, "text": "a\ud800b"}),
            ("type", {"ref": EDIT, "text": "a", "submit": "false"}),
        ]
        for op, args in cases:
            with self.subTest(op=op, args={**args, **({"text": "omitted from failure label"} if "text" in args else {})}):
                self.assertTrue(self.act(op, **args).startswith("Refused:"))
        for op, args in (("click", {"x": 0, "y": 0}), ("scroll", {"amount": 1, "x": 0, "y": 0}),
                         ("key", {"keys": ["tab"]}), ("type", {"ref": EDIT, "text": TYPED})):
            for field in ("purpose", "risky"):
                required = {"window": PRIMARY, "purpose": PURPOSE, "risky": "no", **args}
                del required[field]
                self.assertIn(field, self.invoke(op, required).text)
        self.assertEqual(self.calls, [])
        self.assertEqual([row["outcome"] for row in self.audit()], ["refused"] * (len(cases) + 8))

    def test_03_key_aliases_and_text_limit(self):
        self.snapshot()
        self.assertEqual(self.act("key", keys=[" Control ", "A"]), COMPLETED)
        self.assertEqual(self.actions()[-1][1]["keys"], ["ctrl", "a"])
        self.assertEqual(self.act("type", ref=EDIT, text="é" * 2000), COMPLETED)
        self.assertEqual(len(self.actions()[-1][1]["text"]), 2000)
        self.assertEqual(self.act("scroll", amount=-100, x=0, y=0), COMPLETED)

    def test_03_known_ref_cannot_be_combined_with_coordinates(self):
        self.snapshot()
        count = len(self.calls)
        for op, args in (("click", {}), ("scroll", {"amount": 1})):
            with self.subTest(op=op):
                self.assertIn("Refused:", self.act(op, ref=EDIT, x=0, y=0, **args))
        self.assertEqual(len(self.calls), count)

    def test_04_interleaving_preserves_only_each_windows_latest_refs(self):
        self.snapshot()
        self.snapshot(SECONDARY)
        self.drive_chat("another-chat")
        self.snapshot(SECONDARY, thread="another-chat")
        self.assertIn("fresh snapshot", self.act("click", ref=EDIT, via={"thread": "another-chat"}))
        self.drive_chat("review-chat")
        self.assertIn("Refused:", self.act("click", on=SECONDARY, ref=EDIT))
        self.assertIn("Refused:", self.act("click", ref="66d40c77b6153189-e5"))
        self.assertIn("Refused:", self.act("click", ref=EDIT, name="Fabricated", role="button"))
        self.assertEqual(self.act("click", ref=EDIT), COMPLETED)
        self.responses["window_snapshot"] = {"snapshot": NEW, "refs": {NEW: EDITOR}}
        self.snapshot()
        self.assertIn("Refused:", self.act("click", ref=EDIT))
        self.assertEqual(self.act("click", ref=NEW), COMPLETED)
        self.drive_chat("another-chat")
        self.assertEqual(self.act("click", ref=NEW, on=SECONDARY, via={"thread": "another-chat"}), COMPLETED)
        for op, fields in self.actions():
            self.assertEqual(fields["expect"], EDITOR, op)

    def test_04_failed_snapshot_does_not_publish_unseen_refs(self):
        self.snapshot()
        self.responses["window_snapshot"] = {"refs": {NEW: EDITOR}}
        error = self.snapshot()
        self.assertTrue(error.startswith(("Error:", "Refused:")), error)
        result = self.act("click", ref=NEW)
        self.assertEqual([op for op, _ in self.actions()], [],
                         f"snapshot returned {error!r}, then its unseen ref returned {result!r}")

    def test_04_probe_fields_and_changed_type_target(self):
        self.snapshot()
        for response in ({}, {"name": "Filter by author"}, {"name": None, "role": "editable"},
                         {"name": "Different field", "role": "editable"}, ["not a control"]):
            self.responses["window_probe"] = response
            with self.subTest(response=response):
                self.assertIn("Refused:", self.act("type", ref=EDIT, text=TYPED))
                self.assertEqual(self.actions(), [])
        self.responses["window_probe"] = {"name": "", "role": "button", "unexpected": INTERNAL}
        self.assertEqual(self.act("scroll", x=0, y=0, amount=2), COMPLETED)
        self.assertEqual(self.actions()[-1][1]["expect"], {"name": "", "role": "button"})
        self.responses["window_probe"] = {**BUTTON, "extra": INTERNAL}
        broker = self.broker(lambda req: broker.resolve(req.req_id, "deny"))
        self.assertIn("declined", self.act("key", keys=["Space"]))
        self.assertEqual(self.calls[-1][0], "window_probe")
        self.assertIs(self.calls[-1][1]["focus"], True)
        self.assertIn("Send invitation", self.cards[-1].headline)

    def test_05_refuse_then_retry_never_sends_or_caches_a_grant(self):
        self.snapshot()
        before = len(self.calls)
        for _ in range(2):
            self.assertIn("invalid:risky", self.act("click", ref=QUEUE, risky=[]))
        self.assertEqual(len(self.calls), before)
        answers = iter(("deny", "allow", "deny"))

        def present(req):
            self.assertEqual(len(self.actions()), 0 if len(self.cards) < 3 else 1)
            self.assertEqual(req.headline, 'Click "Send invitation" in Catalog.exe (send)')
            self.assertIn(PURPOSE, req.reason)
            self.assertFalse(req.to_json()["allowlistable"])
            self.assertFalse(req.grantable)
            self.assertIsNone(broker.always_rule(req.req_id)[0])
            self.assertIsNone(broker.resolve(req.req_id, next(answers), always=True, owner_verified=True))

        broker = self.broker(present)
        for expected in ("declined", COMPLETED, "declined"):
            self.assertIn(expected, self.act("click", ref=QUEUE))
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual([r["outcome"] for r in self.audit()],
                         ["refused", "refused", "asked-declined", "asked-approved", "asked-declined"])

    def test_05_revalidate_after_cancel_and_with_unbacked_approver(self):
        self.snapshot()
        unbacked = NS(human_backed=False, ask=Mock(return_value=Decision.ALLOW))
        self.daemon.approvals = unbacked
        self.assertIn("Refused:", self.act("click", ref=QUEUE))
        unbacked.ask.assert_not_called()

        def stop(req):
            self.stopped = True
            broker.resolve(req.req_id, "allow", owner_verified=True)

        broker = self.broker(stop)
        self.assertIn("Refused:", self.act("click", ref=QUEUE))
        self.assertEqual(self.actions(), [])

    def test_05_snapshot_replaced_after_audit_reservation(self):
        self.snapshot()
        append = window._append_audit

        def newer(record, outcome):
            result = append(record, outcome)
            self.responses["window_snapshot"] = {"snapshot": NEW, "refs": {NEW: EDITOR}}
            self.assertEqual(self.snapshot(), NEW)
            return result

        with patch.object(window, "_append_audit", newer):
            self.assertIn("Refused:", self.act("click", ref=EDIT))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.audit()[0]["outcome"], "refused")

    def test_06_every_transport_request_has_scope_pairing_and_liveness(self):
        via = {"thread": "scope-review"}
        self.drive_chat(via["thread"])

        def checked_reply(fields):
            self.assertTrue(fields["current"]())
            self.stopped = True
            self.assertFalse(fields["current"]())
            self.stopped = False
            return {"clicked": True}

        self.responses["window_click"] = checked_reply
        self.snapshot(**via)
        shot = self.invoke("screenshot", {"window": PRIMARY}, **via)
        self.assertEqual(shot.mime, "image/png")
        self.assertEqual(self.act("click", via=via, x=0, y=0), COMPLETED)
        self.assertEqual(self.act("type", via=via, ref=EDIT, text=TYPED), COMPLETED)
        self.assertEqual(self.act("key", via=via, keys=["tab"]), COMPLETED)
        self.assertEqual(self.act("scroll", via=via, ref=EDIT, amount=1), COMPLETED)
        self.assertEqual([op for op, _ in self.calls],
                         ["windows", "window_snapshot", "window_screenshot", "window_probe",
                          "window_click", "window_probe", "window_type", "window_probe",
                          "window_key", "window_scroll"])
        for op, fields in self.calls:
            with self.subTest(op=op):
                self.assertEqual(fields["scope"], via["thread"])
                self.assertIs(fields["require_pair"], True)
                self.assertIsNone(fields["hud_sensitive"])
                self.assertTrue(callable(fields["current"]))

    def test_07_success_results_ignore_bridge_payload(self):
        self.snapshot()
        for op, args in (("click", {"ref": EDIT}), ("scroll", {"ref": EDIT, "amount": 1}),
                         ("type", {"ref": EDIT, "text": TYPED}), ("key", {"keys": ["tab"]})):
            self.assertEqual(self.act(op, **args), COMPLETED)

    def test_07_acting_failure_does_not_expose_bridge_exception_text(self):
        self.snapshot()
        for message in (INTERNAL, "Refused: " + INTERNAL):
            with self.subTest(message=message), patch.dict(self.responses, {"window_click": Mock(
                    side_effect=DesktopError(message))}):
                result = self.act("click", ref=EDIT)
                self.assertTrue(result.startswith(("Error:", "Refused:")))
                self.assertNotIn(INTERNAL, result)
        self.assertEqual([r["outcome"] for r in self.audit()], ["bridge-refused"] * 2)

    def test_08_reserve_one_private_line_before_dispatch_and_fail_closed(self):
        self.snapshot()
        self.logfile.touch(mode=0o644)

        def action(fields):
            self.assertEqual(stat.S_IMODE(self.logfile.stat().st_mode), 0o600)
            row, = self.audit()
            # Lead's change 2026-11-19: while the input is being sent the row
            # says "pending"; it becomes "run" only once the bridge answered.
            self.assertEqual(row["outcome"], "pending")
            self.assertNotIn(TYPED, self.logfile.read_text())
            self.assertEqual(fields["text"], TYPED)
            return {"typed": True}

        self.responses["window_type"] = action
        self.assertEqual(self.act("type", ref=EDIT, text=TYPED), COMPLETED)
        before = self.logfile.read_bytes()
        with patch.object(window.os, "fsync", side_effect=OSError("fixture disk failure")):
            self.assertIn("log could not be written", self.act("type", ref=EDIT, text=TYPED))
        self.assertEqual(self.logfile.read_bytes(), before)
        self.assertEqual(len(self.actions()), 1)

    def test_08_probe_exception_always_audited(self):
        self.snapshot()
        self.responses["window_probe"] = Mock(side_effect=RuntimeError(INTERNAL))
        result = self.act("type", ref=EDIT, text=TYPED)
        self.assertEqual(self.actions(), [])
        # Lead's change 2026-11-19: an unexpected error is "failed", not a bridge refusal.
        self.assertEqual([r["outcome"] for r in self.audit()], ["failed"], result)

    def test_08_failed_bridge_and_failed_audit_update_cannot_claim_run(self):
        self.snapshot()
        self.responses["window_click"] = Mock(side_effect=DesktopError("refused by fixture"))
        with patch.object(window.os, "pwrite", side_effect=OSError("fixture disk failure")):
            result = self.act("click", ref=EDIT)
        self.assertIn("log could not be written", result)
        self.assertNotIn("run", [r["outcome"] for r in self.audit()],
                         "the reserved row still claims run after the bridge refused")

    def test_08_card_and_audit_do_not_echo_typed_text_from_accessible_name(self):
        reflected = {"name": "Filter: " + TYPED, "role": "editable"}
        self.responses["window_snapshot"] = {"snapshot": EDIT, "refs": {EDIT: reflected}}
        self.responses["window_probe"] = reflected
        self.snapshot()
        broker = self.broker(lambda req: broker.resolve(req.req_id, "deny"))
        self.assertIn("declined", self.act("type", ref=EDIT, text=TYPED, risky="yes",
                                          purpose="Look up " + TYPED))
        card, = self.cards
        for target, value in (("card", json.dumps(card.to_json())), ("audit", self.logfile.read_text())):
            with self.subTest(target=target):
                self.assertNotIn(TYPED, value)

    def test_09_catalog_and_file_protection_match_existing_policies(self):
        self.assertEqual(NAMES & tools.EXPLICIT_ONLY, NAMES)
        self.assertEqual(NAMES & tools.PRIVATE_RESULT, NAMES - {"hud_window_list"})
        self.assertFalse(NAMES & set(tools.default_names()))
        self.assertTrue(files._self_protected(Path(window.__file__)))
        new, old = self.logfile, self.root / "decisions.jsonl"
        for path in (new, old):
            path.write_text("{}\n")
        old_alias = self.root / "old-alias"
        new_alias = self.root / "new-alias"
        old_alias.symlink_to(old)
        new_alias.symlink_to(new)
        for path in (old, old_alias, new, new_alias):
            with self.subTest(path=path.name):
                self.assertIn("own records", files.write_file(str(path), "replacement"))
                self.assertEqual(path.read_text(), "{}\n")

    def test_09_provider_events_and_durable_log_omit_arguments(self):
        args = {"text": TYPED, "purpose": PURPOSE}
        cl = object.__new__(claude.ClaudeProvider)
        cx = object.__new__(codex.CodexProvider)
        self.daemon.stores = Stores(self.root)
        self.daemon.chat_relay = NS(progress_text=lambda *_: None)
        self.daemon.voice_chat = NS(speaks=lambda *_: False)
        self.daemon.bus = NS(publish=Mock())
        for tool in ("hud_desktop_type", "hud_window_type"):
            name = "mcp__jarvis__" + tool
            with self.subTest(tool=tool):
                thread = self.daemon.stores.threads.create("project", Role.CHAT, ProviderName.CODEX)
                session = NS(thread_id=thread.id, denied_tools=set(), denial_without_id=False,
                             mutex=threading.Lock(), pending={}, emit=Mock())
                seen = {}
                events = cl._blocks(session, NS(content=[claude.ToolUseBlock("call-a", name, args)]), seen)
                events += cl._tool_result(session, claude.ToolResultBlock(
                    "call-a", claude.CLASSIFIER_DENIAL_PREFIX + " review", True), seen=seen)
                with patch.object(cl, "_race", return_value=Decision.DENY):
                    asyncio.run(cl._decide(session, name, args))
                self.assertEqual(session.emit.call_args_list[0].kwargs["args"], {})
                self.assertIsNone(session.emit.call_args_list[0].kwargs["command"])
                native = NS(thread_id=thread.id, turn_id="turn-a", mcp_items={})
                item = {"id": "call-a", "server": "jarvis", "tool": tool,
                        "arguments": args, "type": "mcpToolCall"}
                events += list(cx._mcp_item(NS(thread_id=native.thread_id, native=native), "item/started",
                                            {"threadId": native.thread_id, "turnId": native.turn_id}, item))
                self.assertEqual(cx._child_tool(item), (tool, {}))
                event_queue = queue.Queue()
                fastpath._TurnState(native.thread_id, event_queue).on_event("tool_start", (tool, json.dumps(args)))
                events.append(event_queue.get_nowait())
                daemon_session = NS(thread=thread, brief=NS(cwd=None), retired=False, turn_id="turn-a",
                                    self_started=False, compacting=False, relay_mark=None)
                for event in events:
                    self.assertNotIn(TYPED, json.dumps(event.data))
                    if "args" in event.data:
                        self.assertEqual(event.data["args"], {})
                    self.daemon._record(daemon_session, event)
                replay = self.daemon.stores.threads.read_log(thread.id)
                self.assertEqual(len(replay), len(events))
                self.assertNotIn(TYPED, json.dumps(replay))

    def phone_answer(self, req, *, verified, decision="allow"):
        """Stub authentication only; the phone handler, inbox and broker run in-process."""
        self.daemon.secure = NS(check=Mock(return_value={"ok": verified}))
        handler = NS(remote_auth=NS(touch_unlock=Mock()))
        ticket = NS(mutating=Mock(), returned=Mock())
        row = "appr:" + req.req_id
        body = {"decision": decision}
        if decision != "deny":
            body["op"] = "synthetic assertion"
        with patch("jarvis.v2.remote_auth.parse_op", return_value=object()), \
             patch("jarvis.v2.remote_auth.gated", side_effect=lambda *a: a[-1](ticket)), \
             patch.object(inbox, "rows", return_value=[{"id": row, "detail": req.to_json()}]):
            return remote_answer.answer(handler, self.daemon, object(), "/fixture/answer", row, body)

    def test_05_verified_phone_can_supply_the_required_owner_yes(self):
        # The lead changed the expectation on 2026-11-19.
        self.snapshot()
        for verified in (False, True):
            with self.subTest(verified=verified):
                attempts = []

                def present(req):
                    with self.assertRaises(ValueError):
                        broker.resolve_code(req.code, "allow")
                    attempts.append(self.phone_answer(req, verified=verified))

                broker = self.broker(present)
                result = self.act("click", ref=QUEUE)
                self.assertEqual(len(attempts), 1, attempts)
                self.assertEqual(attempts[0][0] == 200, verified, attempts)
                if verified:
                    self.assertEqual(result, COMPLETED)
                    self.assertEqual([op for op, _ in self.actions()], ["window_click"])
                else:
                    self.assertIn("declined", result)
                    self.assertEqual(self.actions(), [])

    def test_10_ordinary_phone_answers_still_use_both_authorities_and_deny(self):
        for verified, decision in ((False, "allow"), (True, "allow"), (False, "deny")):
            with self.subTest(verified=verified, decision=decision):
                replies = []
                broker = self.broker(lambda req: replies.append(
                    self.phone_answer(req, verified=verified, decision=decision)))
                req = ApprovalRequest("Bash", args={"command": "git status"}, command="git status")
                self.assertIs(broker.ask(req), Decision(decision))
                (status, data, _), = replies
                self.assertEqual((status, data["decision"]), (200, decision))
                if decision == "allow":
                    self.assertEqual(data["authority"], "hud" if verified else "limited")

    def test_10_ordinary_allowlist_and_quiet_settlement_are_unchanged(self):
        entry = {"tool": "Bash", "rule": "git status"}
        broker = self.broker(lambda req: broker.resolve(req.req_id, "allow", always=True))
        req = ApprovalRequest("Bash", args={"command": "git status"}, command="git status")
        with patch("jarvis.permissions.add_allow", return_value=entry) as persist:
            self.assertIs(broker.ask(req), Decision.ALLOW)
        persist.assert_called_once()
        self.assertEqual(broker.decisions[-1]["resolution"], "approved-always")
        for settlement, expected in (("granted", Decision.ALLOW), ("deferred", Decision.DENY)):
            with self.subTest(settlement=settlement):
                presenter = Mock()
                broker = self.broker(presenter)
                quiet = Mock()
                quiet.settle.return_value = (settlement, "fixture")
                quiet.granted.return_value = True
                broker.quiet_desk = quiet
                req = ApprovalRequest("Bash", args={"command": "git status"})
                self.assertIs(broker.ask(req), expected)
                self.assertEqual(req.outcome, (settlement, "fixture"))
                getattr(quiet, settlement).assert_called_once_with(req)
                presenter.assert_not_called()

    def test_10_public_summary_and_private_browser_card_are_unchanged(self):
        summary = self.daemon.approval_summaries = NS(begin=Mock(return_value=("ready", "Lists changes.")))
        self.daemon.stores = Mock()
        self.daemon.stores.threads.get.return_value = None
        events = []
        self.daemon.bus = NS(publish=events.append)
        ordinary = ApprovalRequest("Bash", args={"command": "git status"}, thread_id="thread")
        card = object.__new__(drive.BrowserDrive)._card(
            NS(action="click", target="e3", text="", file=""),
            NS(context_flagged=False, url="https://example.test/item", target=NS(name="Next"), rect=None),
            NS(snapshot=NS(tab_id="tab", slot=1, page_identity="page", document_generation=2),
               turn=NS(thread_id="thread", provider="codex")),
            "action", "ordinary")
        attempts = []

        def present(req):
            self.daemon._approval_requested(req)
            if req is card:
                with self.assertRaises(ValueError):
                    broker.resolve(req.req_id, "allow")
                attempts.extend(self.phone_answer(req, verified=verified)[0] for verified in (False, True))
            else:
                broker.resolve(req.req_id, "allow")

        broker = self.broker(present)
        broker.describe = self.daemon._describe_approval
        self.assertIs(broker.ask(ordinary), Decision.ALLOW)
        self.assertIs(broker.ask(card), Decision.ALLOW)
        self.assertEqual(attempts, [409, 200])
        self.assertEqual([event["kind"] for event in events],
                         ["approval_requested", "approval_summary", "approval_requested"])
        self.assertEqual(events[0]["data"]["summary_state"], "pending")
        self.assertEqual(events[1]["data"]["summary"], "Lists changes.")
        self.assertTrue(events[2]["data"]["hud_only"])
        self.assertNotIn("headline", events[2]["data"])
        summary.begin.assert_called_once()

    def test_10_legacy_desktop_bridge_and_browser_admission_are_unchanged(self):
        session, caller = fake_session()
        self.daemon._sessions = {caller.thread_id: session}
        session.desk.slot["hud_desktop"] = True
        session.desk.slot["present"] = False
        self.responses["type"] = {"typed": True}
        args = {"app": "Settings", "ref": EDIT, "text": TYPED}
        with patch("jarvis.v2.callers.authenticate", return_value=(caller, session)), \
             patch.object(bridge, "_api_listener", return_value=True):
            status, reply = bridge.route(NS(command="GET"), self.daemon, ["mcp", "tools"], {})
        self.assertEqual(status, 200)
        names = {row["name"] for row in reply["tools"]}
        self.assertFalse(NAMES & names)
        self.assertIn("hud_browser_act", names)
        self.assertLessEqual(hud_desktop.HUD_DESKTOP_TOOLS, names)
        self.assertEqual(bridge.call(self.daemon, session, caller, "hud_desktop_type", args)["text"], COMPLETED)
        session.permit.assert_called_once_with("hud_desktop_type", args, None, dangerous_tool=True)
        self.assertEqual(self.calls[-1][0], "type")
        self.assertNotIn("hud_sensitive", self.calls[-1][1])
        self.assertTrue(bridge.call(self.daemon, session, caller, "hud_desktop_type", args,
                                    request_alive=lambda: False)["isError"])
        session.desk.slot["present"] = True
        with patch.object(drive, "admission_for_tool", return_value=False):
            self.assertIn(drive.OFF, bridge.call(self.daemon, session, caller, "hud_browser_act", {})["text"])
        with patch.object(drive, "admission_for_tool", return_value=True), \
             patch.object(hud_browser_act, "act_for_tool", return_value=NS(outcome="acted", text="fixture")) as act:
            session.drive_turn = None
            self.assertTrue(bridge.call(self.daemon, session, caller, "hud_browser_act", {})["isError"])
            act.assert_not_called()
            session.drive_turn = DriveTurn(caller.thread_id, session.turn_id, False, "", "", provider="codex")
            self.assertTrue(bridge.call(self.daemon, session, caller, "hud_browser_act", {},
                                        request_alive=lambda: False)["isError"])
            act.assert_not_called()
            self.assertEqual(bridge.call(self.daemon, session, caller, "hud_browser_act",
                                         {"action": "click"})["text"], "Acted: fixture")
            act.assert_called_once()
            self.assertTrue(session.drive_turn.attempted)

    def test_10_daemon_lifecycle_preserves_browser_services_and_ordinary_cards(self):
        # No listener binds and the test owns an isolated store separate from the configured root.
        self.service.stop()
        daemon = Daemon(Stores(self.root / "lifecycle"), {}, None, 0, remote_host=None)

        def listener(address, handler):
            fixture = Mock(server_address=address)
            fixture.RequestHandlerClass = handler
            return fixture

        with patch("jarvis.v2.daemon.ThreadingHTTPServer", side_effect=listener), \
             patch.object(daemon.devservers, "start"), patch.object(daemon.devservers, "stop"), \
             patch.object(daemon.working, "start"), patch.object(daemon.schedules, "start"), \
             patch("jarvis.v2.hud_api.connect_controls"), \
             patch.object(daemon.browser_drive, "install", wraps=daemon.browser_drive.install) as install_drive, \
             patch.object(daemon.browser_look, "install", wraps=daemon.browser_look.install) as install_look, \
             patch.object(daemon.browser_drive, "stop", wraps=daemon.browser_drive.stop) as stop_drive, \
             patch.object(daemon.browser_look, "stop", wraps=daemon.browser_look.stop) as stop_look:
            try:
                daemon.start()
                self.assertIs(window._service, daemon.window_drive)
                self.assertIs(drive._installed, daemon.browser_drive)
                install_drive.assert_called_once()
                install_look.assert_called_once()
                request = ApprovalRequest("Bash", args={"command": "git status"})
                with patch.object(daemon.approvals, "_on_request", side_effect=lambda req:
                                  daemon.resolve_approval(req.req_id, "allow")):
                    self.assertIs(daemon.approvals.ask(request), Decision.ALLOW)
            finally:
                daemon.stop()
            self.assertIsNone(window._service)
            self.assertIsNone(drive._installed)
            stop_drive.assert_called_once()
            stop_look.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

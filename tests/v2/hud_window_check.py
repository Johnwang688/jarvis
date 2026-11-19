"""Owner HUD window gating with synthetic controls and no desktop connection."""
import contextvars
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config, runtime, tools
from jarvis.desktop import DesktopError, DesktopSession
from jarvis.v2 import bridge, desktop_risk as risk, session_tools
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.daemon import APIError
from jarvis.v2.mcp_catalog import MCP_TOOLS
from jarvis.v2.model import ProviderName, Role
from jarvis.v2.permissions import denied_file
from jarvis.v2.provider import Decision
from jarvis.v2.providers import claude, codex
from jarvis.v2.tools import hud_window as hud

WINDOW = "0000000000000001"
OTHER = "0000000000000002"
SAFE = "0123456789abcdef-e1"
RISKY = "0123456789abcdef-e2"
FIELD = "0123456789abcdef-e3"
FRESH = "fedcba9876543210-e1"
PURPOSE = "Open the requested item."
SECRET = "synthetic-private-text"
IMAGE = "iVBORw0KGgo="
OPEN = {"name": "Open", "role": "button"}
SEND = {"name": "Send now", "role": "button"}
SEARCH = {"name": "Search files", "role": "editable"}
CHAT = {"name": "Write a message", "role": "editable"}
NAMES = {
    "hud_window_list", "hud_window_snapshot", "hud_window_screenshot", "hud_window_click",
    "hud_window_type", "hud_window_key", "hud_window_scroll",
}


class Approvals:
    human_backed = True

    def __init__(self):
        self.answer = Decision.ALLOW
        self.cards = []
        self.during = lambda: None

    def ask(self, request, *, cancelled):
        self.cards.append(request)
        assert not cancelled()
        self.during()
        return self.answer


class Checks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ["HOME"])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "desktop_actions.jsonl"
        for guard in (patch.object(config, "V2_DATA_DIR", self.root),
                      patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "1"}),
                      patch.object(hud.SESSION, "request", self.request)):
            guard.start()
            self.addCleanup(guard.stop)
        self.approvals = Approvals()
        self.daemon = SimpleNamespace(approvals=self.approvals)
        self.service = hud.WindowDrive(self.daemon)
        self.service.install()
        self.addCleanup(self.service.stop)
        self.requests = []
        self.focus, self.point, self.field = CHAT, SEND, SEARCH
        self.refs = {WINDOW: {SAFE: OPEN, RISKY: SEND, FIELD: SEARCH}, OTHER: {FRESH: OPEN}}
        self.bridge_error = None
        self.stopped = False

    def request(self, op, *, scope, require_pair, current, hud_sensitive, **fields):
        assert scope in ("first", "second") and require_pair is True and hud_sensitive is None
        assert callable(current) and current()
        self.requests.append((op, scope, fields))
        if op == "windows":
            return {"windows": [
                {"window": WINDOW, "title": "Untrusted title", "app": "explorer.exe", "access": "drivable"},
                {"window": OTHER, "title": "System", "app": "system.exe", "access": "see_only"},
            ]}
        if op == "window_snapshot":
            return {"snapshot": "Controls " + " ".join(self.refs[fields["window"]]),
                    "refs": self.refs[fields["window"]]}
        if op == "window_screenshot":
            return {"image_b64": IMAGE, "bytes": 12, "marks": 0, "size": [20, 30]}
        if op == "window_probe":
            return self.focus if fields.get("focus") else self.field if "ref" in fields else self.point
        # Inspect the actual file before returning from the mutation.
        assert self.log()[-1]["outcome"] == "pending"
        if self.bridge_error is not None:
            raise DesktopError(self.bridge_error)
        return {"internal": "do not return bridge internals"}

    def call(self, name, *, thread="first", desk=True, stopped=False, depth=0,
             provider="claude", images=True, **args):
        def run():
            runtime.bind(caller={"thread_id": thread, "turn_id": "fixture", "provider": provider},
                         desk={"present": desk, "hud_desktop": True, "images": images},
                         depth=depth, should_stop=lambda: stopped or self.stopped, tool_names=NAMES)
            return tools.dispatch("hud_window_" + name, json.dumps(args))
        return contextvars.Context().run(run)

    def action(self, name="click", *, purpose=PURPOSE, risky="no", window=WINDOW, **args):
        return self.call(name, window=window, purpose=purpose, risky=risky, **args).text

    def snap(self, **kwargs):
        return self.call("snapshot", window=WINDOW, **kwargs).text

    def log(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_catalog_and_dark_flag(self):
        self.assertEqual(NAMES, hud.HUD_WINDOW_TOOLS)
        self.assertFalse(NAMES & MCP_TOOLS)
        self.assertFalse(NAMES & set(tools.default_names()))
        self.assertEqual(NAMES & tools.EXPLICIT_ONLY, NAMES)
        self.assertEqual(NAMES & tools.PRIVATE_RESULT, NAMES - {"hud_window_list"})
        os.environ.pop("JARVIS_DESKTOP_DRIVE")
        for setting in ("", "0", "true", " 1", "1"):
            with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": setting}):
                self.assertEqual(config.desktop_drive(), setting == "1")
        for provider in ("claude", "codex", "fast"):
            self.assertFalse(NAMES & {row["name"] for row in bridge.listing(provider)})
        for name in NAMES:
            self.assertEqual(self.call(name.removeprefix("hud_window_")).text
                             if name not in ("hud_window_snapshot", "hud_window_screenshot")
                             else self.call(name.removeprefix("hud_window_"), window=WINDOW).text, hud.OFF)
        self.assertEqual(hud.hud_window_click(WINDOW, PURPOSE, "no", ref=SAFE), hud.OFF)
        self.assertFalse(self.requests)
        os.environ["JARVIS_DESKTOP_DRIVE"] = "1"
        self.assertFalse(NAMES & {row["name"] for row in bridge.listing("fast")})
        for provider in ("claude", "codex"):
            rows = {row["name"]: row for row in bridge.listing(provider)}
            self.assertLessEqual(NAMES, rows.keys())
            self.assertFalse(NAMES & {row["name"] for row in bridge.listing(provider, window_tools=False)})
            for name in NAMES:
                entry = session_tools.offered(provider, name)
                self.assertTrue(entry.foreground_only and entry.desk_only)
                self.assertEqual(entry.read_only, name == "hud_window_list")
                self.assertEqual(entry.owner_gated, name != "hud_window_list")
                self.assertFalse(tools.REGISTRY[name].dangerous)
                if entry.owner_gated and name not in ("hud_window_snapshot", "hud_window_screenshot"):
                    schema = rows[name]["inputSchema"]
                    self.assertLessEqual({"purpose", "risky"}, set(schema["required"]))
                    self.assertEqual(schema["additionalProperties"], False)

    def test_requires_current_foreground_owner_desk_and_installed_service(self):
        for args in ({"desk": False}, {"stopped": True}, {"depth": 1}, {"provider": "fast"}):
            with self.subTest(args=args):
                self.assertEqual(self.call("list", **args).text, hud.UNAVAILABLE)
                self.assertEqual(self.action(ref=SAFE, **args), hud.UNAVAILABLE)
        self.service.stop()
        self.assertEqual(self.action(ref=SAFE), hud.UNAVAILABLE)
        self.assertEqual([row["outcome"] for row in self.log()], ["refused"] * 5)
        self.assertFalse(self.requests)

    def test_look_and_safe_click(self):
        self.assertEqual(self.call("list").text, f"{WINDOW}\texplorer.exe\tUntrusted title\tdrivable\n"
                                                f"{OTHER}\tsystem.exe\tSystem\tsee-only")
        self.assertEqual(self.snap(), f"Controls {SAFE} {RISKY} {FIELD}")
        screen = self.call("screenshot", window=WINDOW)
        self.assertEqual((screen.image_b64, screen.mime), (IMAGE, "image/png"))
        self.assertEqual(self.action(ref=SAFE, provider="codex"), hud.COMPLETED)
        self.assertEqual(self.requests, [
            ("windows", "first", {}), ("window_snapshot", "first", {"window": WINDOW}),
            ("window_screenshot", "first", {"window": WINDOW}),
            ("window_click", "first", {"window": WINDOW, "ref": SAFE, "expect": OPEN}),
        ])
        self.assertFalse(self.approvals.cards)
        row, = self.log()
        self.assertEqual(row, {
            "at": row["at"], "thread_id": "first", "tool": "hud_window_click", "app": "explorer.exe",
            "name": "Open", "role": "button", "keys": [], "purpose": PURPOSE, "decision": "run", "group": "",
            "reasons": [], "outcome": "run",
        })
        self.assertTrue(row["at"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(denied_file(str(self.path)))
        self.assertTrue(denied_file(hud.__file__))
        self.assertTrue(self.call("screenshot", window=WINDOW, images=False).text.startswith("Refused:"))
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn(IMAGE, self.path.read_text())

    def test_risky_ref_uses_code_headline_and_one_time_human_answer(self):
        self.snap()
        action = risk.Action("click", "explorer.exe", **SEND, purpose=PURPOSE, risky="no")
        self.approvals.during = lambda: self.assertEqual([op for op, _, _ in self.requests],
                                                        ["windows", "window_snapshot"])
        self.assertEqual(self.action(ref=RISKY), hud.COMPLETED)
        card, = self.approvals.cards
        self.assertEqual(card.headline, risk.statement(action, risk.decide(action)))
        self.assertEqual(card.reason, 'Approve this one desktop action? Jarvis says: "' + PURPOSE + '"')
        self.assertEqual(card.args, {"window": WINDOW, "ref": RISKY, "purpose": PURPOSE, "risky": "no"})
        self.assertTrue(card.require_hud_answer)
        self.assertFalse(card.allowlistable or card.grantable)
        self.assertEqual(self.requests[-1], ("window_click", "first",
                                            {"window": WINDOW, "ref": RISKY, "expect": SEND}))
        self.assertEqual(self.log()[-1]["outcome"], "asked-approved")
        self.assertEqual(self.log()[-1]["group"], "send")

    def test_refs_cannot_be_invented_moved_or_reused(self):
        self.snap()
        for kwargs in ({"ref": SAFE, "thread": "second"}, {"ref": SAFE, "window": OTHER},
                       {"ref": "0123456789abcdef-e99"}, {"ref": SAFE, "label": "Send"}):
            with self.subTest(kwargs=kwargs):
                self.assertTrue(self.action(**kwargs).startswith("Refused:"))
                self.assertEqual(len(self.requests), 2)
        self.refs[WINDOW] = {FRESH: OPEN}
        self.snap()
        self.assertIn("fresh snapshot", self.action(ref=SAFE))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.action(ref=FRESH), hud.COMPLETED)
        self.assertEqual([row["outcome"] for row in self.log()], ["refused"] * 5 + ["run"])

    def test_point_probes_and_scroll(self):
        self.call("screenshot", window=WINDOW)
        self.assertEqual(self.action(x=7, y=8), hud.COMPLETED)
        self.assertEqual(self.requests[-2:], [
            ("window_probe", "first", {"window": WINDOW, "x": 7, "y": 8}),
            ("window_click", "first", {"window": WINDOW, "x": 7, "y": 8, "expect": SEND}),
        ])
        self.assertEqual(len(self.approvals.cards), 1)
        self.assertEqual(self.action("scroll", x=7, y=8, amount=-4), hud.COMPLETED)
        self.assertEqual(self.requests[-1], ("window_scroll", "first",
                                            {"window": WINDOW, "x": 7, "y": 8, "amount": -4, "expect": SEND}))
        self.assertEqual(len(self.approvals.cards), 1)
        self.assertEqual(self.action("scroll", x=7, y=8, amount=1, risky="unsure"), hud.COMPLETED)
        self.assertEqual(len(self.approvals.cards), 2)
        self.assertEqual(self.log()[-1]["reasons"], ["declared:unsure"])

    def test_click_and_scroll_require_exactly_one_selector(self):
        self.snap()
        for kind, args in (("click", {}), ("scroll", {"amount": 1})):
            for selector in ({}, {"ref": SAFE, "x": 0, "y": 0}):
                with self.subTest(kind=kind, selector=selector):
                    self.assertIn("either a snapshot ref or both screenshot coordinates",
                                  self.action(kind, **args, **selector))
        self.assertEqual([op for op, _, _ in self.requests], ["windows", "window_snapshot"])

    def test_malformed_snapshots_clear_old_and_new_refs(self):
        for reply in ({"refs": {FRESH: OPEN}}, {"snapshot": "", "refs": {FRESH: OPEN}},
                      {"snapshot": 42, "refs": {FRESH: OPEN}}, {"snapshot": FRESH},
                      {"snapshot": FRESH, "refs": [FRESH]},
                      {"snapshot": FRESH, "refs": {FRESH: {"name": "Open"}}},
                      {"snapshot": FRESH, "refs": {FRESH: {"name": 1, "role": "button"}}},
                      {"snapshot": FRESH, "refs": {FRESH: {"name": "Open", "role": 1}}},
                      {"snapshot": FRESH, "refs": {FRESH: OPEN, "e2": OPEN}}):
            with self.subTest(reply=reply):
                self.snap()
                self.call("snapshot", window=OTHER)
                with patch.object(self.service, "request", return_value=reply):
                    self.assertTrue(self.snap().startswith(("Refused:", "Error:")))
                before = len(self.requests)
                for ref in (SAFE, FRESH):
                    self.assertEqual(self.action(ref=ref), f"Refused: {hud.FRESH_SNAPSHOT}")
                self.assertEqual(len(self.requests), before)
                self.assertEqual(self.action(ref=FRESH, window=OTHER), hud.COMPLETED)

    def test_key_focus_enter_and_canonical_names(self):
        self.assertEqual(self.action("key", keys=[" Return "]), hud.COMPLETED)
        self.assertEqual(self.requests[-2:], [
            ("window_probe", "first", {"window": WINDOW, "focus": True}),
            ("window_key", "first", {"window": WINDOW, "keys": ["enter"], "expect": CHAT}),
        ])
        self.assertEqual(len(self.approvals.cards), 1)
        self.focus = SEARCH
        self.assertEqual(self.action("key", keys=["enter"]), hud.COMPLETED)
        self.assertEqual(len(self.approvals.cards), 1)
        self.assertEqual(self.log()[-1]["keys"], ["enter"])
        self.assertEqual(self.log()[-1]["outcome"], "run")

    def test_type_probes_field_and_keeps_text_out_of_card_and_log(self):
        self.snap()
        self.assertEqual(self.action("type", ref=FIELD, text=SECRET), hud.COMPLETED)
        self.assertFalse(self.approvals.cards)
        self.assertEqual(self.action("type", ref=FIELD, text=SECRET, submit=True,
                                     risky="unsure", purpose=f'Enter "{SECRET}".'), hud.COMPLETED)
        self.assertEqual(self.requests[-2:], [
            ("window_probe", "first", {"window": WINDOW, "ref": FIELD}),
            ("window_type", "first", {"window": WINDOW, "ref": FIELD, "text": SECRET,
                                     "submit": True, "expect": SEARCH}),
        ])
        card, = self.approvals.cards
        self.assertEqual(card.args["text"], len(SECRET))
        self.assertEqual(card.args["purpose"], 'Enter "[typed text withheld]".')
        self.assertNotIn(SECRET, repr(card))
        self.assertNotIn(SECRET, self.path.read_text())
        self.field = CHAT
        self.assertIn("fresh snapshot", self.action("type", ref=FIELD, text=SECRET))
        self.assertEqual(self.requests[-1][0], "window_probe")

    def test_type_withholds_reflected_names_but_classifies_the_real_control(self):
        self.field = {"name": f"Pay {SECRET.upper()}", "role": "editable"}
        self.refs[WINDOW][FIELD] = self.field
        self.snap()
        self.service._apps["first"][WINDOW] = f"{SECRET.upper()}.exe"
        self.assertEqual(self.action("type", ref=FIELD, submit=True, text=f" {SECRET} "), hud.COMPLETED)
        card, = self.approvals.cards
        self.assertIn("a field showing the typed text", card.headline)
        self.assertIn("an app showing the typed text", card.headline)
        self.assertNotIn(SECRET, repr(card).casefold())
        self.assertNotIn(SECRET, self.path.read_text().casefold())
        self.assertEqual(self.log()[-1]["group"], "pay")
        self.assertEqual(self.requests[-1][2]["expect"], self.field)

        self.assertEqual(self.action("type", ref=FIELD, risky="yes", text="a"), hud.COMPLETED)
        self.assertIn(self.field["name"], self.approvals.cards[-1].headline)
        self.assertEqual(self.log()[-1]["name"], self.field["name"])

    def test_claude_event_omits_window_type_arguments(self):
        provider = object.__new__(claude.ClaudeProvider)
        name = "mcp__jarvis__hud_window_type"
        message = SimpleNamespace(content=[claude.ToolUseBlock("call", name, {"text": SECRET})])
        event, = provider._blocks(SimpleNamespace(thread_id="first"), message, {})
        self.assertEqual(event.data["name"], name)
        self.assertEqual(event.data["args"], {})

    def test_codex_event_omits_window_type_arguments(self):
        provider = object.__new__(codex.CodexProvider)
        native = SimpleNamespace(thread_id="first", turn_id="fixture", mcp_items={})
        handle = SimpleNamespace(thread_id="first", native=native)
        item = {"id": "call", "server": "jarvis", "tool": "hud_window_type", "arguments": {"text": SECRET}}
        event, = provider._mcp_item(handle, "item/started", {"threadId": "first", "turnId": "fixture"}, item)
        self.assertEqual(event.data["name"], "hud_window_type")
        self.assertEqual(event.data["args"], {})

    def test_arguments_refused_before_any_bridge_request(self):
        invalid = [
            ("click", {"window": "bad", "ref": SAFE}), ("click", {"ref": SAFE, "x": 2, "y": 4}),
            ("click", {"x": 2}), ("click", {"x": True, "y": 0}), ("click", {"x": -2, "y": 0}),
            ("click", {"x": 1, "y": 1, "purpose": " "}), ("click", {"x": 1, "y": 1, "purpose": 3}),
            ("click", {"x": 1, "y": 1, "purpose": "Open\nnow"}),
            ("click", {"x": 1, "y": 1, "purpose": "x" * 301}),
            ("click", {"x": 1, "y": 1, "risky": "maybe"}),
            ("key", {"keys": ["wat"]}), ("key", {"keys": ["win"]}),
            ("key", {"keys": ["control", "ctrl", "c"]}), ("key", {"keys": "enter"}),
            ("scroll", {"ref": SAFE, "amount": 101}), ("scroll", {"ref": SAFE, "amount": 0}),
        ]
        for name, args in invalid:
            with self.subTest(name=name, args=args):
                self.assertTrue(self.action(name, **args).startswith("Refused:"))
        for required in ("purpose", "risky"):
            args = {"window": WINDOW, "ref": SAFE, "purpose": PURPOSE, "risky": "no"}
            del args[required]
            self.assertIn(f"Refused: Missing required argument(s): {required}", self.call("click", **args).text)
        self.assertFalse(self.requests or self.approvals.cards)
        self.assertEqual([row["outcome"] for row in self.log()], ["refused"] * (len(invalid) + 2))
        self.assertNotIn(SECRET, self.path.read_text())

    def test_invalid_text_refused_for_known_field_before_probing(self):
        self.snap()
        for text in (SECRET + "\n", "x" * 2001, "a\x85", "a\u202e", 10):
            with self.subTest(text=text):
                self.assertIn("Use submit or hud_window_key", self.action("type", ref=FIELD, text=text))
        self.assertIn("boolean submit flag", self.action("type", ref=FIELD, text="a", submit=1))
        self.assertEqual([op for op, _, _ in self.requests], ["windows", "window_snapshot"])
        self.assertEqual([row["outcome"] for row in self.log()], ["refused"] * 6)
        self.assertNotIn(SECRET, self.path.read_text())

    def test_decline_unbacked_and_cancelled(self):
        self.snap()
        for value in (False, 1):
            self.approvals.human_backed = value
            self.assertIn("needs the owner at the HUD", self.action(ref=RISKY))
        self.assertFalse(self.approvals.cards)
        self.approvals.human_backed = True
        self.approvals.answer = Decision.DENY
        self.assertIn("The user declined", self.action(ref=RISKY))
        self.approvals.answer = Decision.ALLOW

        def stop():
            self.stopped = True

        self.approvals.during = stop
        self.assertTrue(self.action(ref=RISKY).startswith("Refused:"))
        self.assertEqual(len(self.approvals.cards), 2)
        self.assertEqual([op for op, _, _ in self.requests], ["windows", "window_snapshot"])
        self.assertEqual([row["outcome"] for row in self.log()],
                         ["refused", "refused", "asked-declined", "refused"])

    def test_replaced_ref_during_approval(self):
        self.snap()

        def refresh():
            self.refs[WINDOW] = {FRESH: OPEN}
            self.snap()

        self.approvals.during = refresh
        self.assertTrue(self.action(ref=RISKY).startswith("Refused:"))
        self.assertEqual([op for op, _, _ in self.requests], ["windows", "window_snapshot", "window_snapshot"])

    def test_log_failure_and_bridge_refusal(self):
        self.snap()
        self.path.mkdir()
        self.assertEqual(self.action(ref=SAFE), hud.LOG_REFUSED)
        self.assertEqual(len(self.requests), 2)
        self.path.rmdir()
        self.bridge_error = "Refused: that window is see-only. " + "x" * 200
        self.assertEqual(self.action(ref=SAFE), hud.BRIDGE_REFUSED)
        self.assertEqual(self.action(ref=RISKY), hud.BRIDGE_REFUSED)
        self.assertEqual([row["outcome"] for row in self.log()], ["bridge-refused"] * 2)
        self.assertEqual([row["outcome_detail"] for row in self.log()], [self.bridge_error[:200]] * 2)
        self.assertEqual(len(self.requests), 4)

    def test_disconnected_list_and_action_use_fixed_text(self):
        disconnected = DesktopSession(0)
        with patch.object(disconnected, "start"), patch.object(disconnected, "wait_for_bridge"), \
             patch.object(hud, "SESSION", disconnected):
            expected = "Error: The Windows bridge is not connected."
            self.assertEqual(self.call("list").text, expected)
            self.assertEqual(self.action("key", keys=["tab"]), expected)
        self.assertEqual(self.log()[-1]["outcome"], "bridge-refused")

    def test_audit_fsyncs_pending_before_dispatch_and_run_after(self):
        self.snap()
        synced = []
        fsync = os.fsync

        def sync(fd):
            synced.append(self.log()[-1]["outcome"])
            fsync(fd)

        def observe(op, **kwargs):
            self.assertEqual(synced, ["pending"])
            return self.request(op, **kwargs)

        with patch.object(hud.os, "fsync", side_effect=sync), patch.object(hud.SESSION, "request", observe):
            self.assertEqual(self.action(ref=SAFE), hud.COMPLETED)
        self.assertEqual(synced, ["pending", "run"])

    def test_failed_audit_update_retains_pending(self):
        self.snap()
        fsync = os.fsync
        pwrite = os.pwrite

        def fail_update_sync(fd):
            if self.log()[-1]["outcome"] != "pending":
                raise OSError("fixture update sync failure")
            fsync(fd)

        for method, failure in (("fsync", fail_update_sync),
                                ("pwrite", lambda fd, data, offset: pwrite(fd, data[:12], offset))):
            with self.subTest(method=method), patch.object(hud.os, method, side_effect=failure):
                self.assertIn("outcome could not be recorded", self.action(ref=SAFE))
                self.assertEqual(self.log()[-1]["outcome"], "pending")
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(len(self.log()), 2)
        self.assertEqual([op for op, _, _ in self.requests].count("window_click"), 2)

    def test_unexpected_probe_and_dispatch_exceptions_are_audited(self):
        self.snap()
        for expected_rows, op in enumerate(("window_probe", "window_type"), start=1):
            def exchange(request, **kwargs):
                if request == op:
                    raise RuntimeError(f"fixture failure with {SECRET}")
                return self.request(request, **kwargs)

            with self.subTest(op=op), patch.object(hud.SESSION, "request", exchange):
                self.assertEqual(self.action("type", ref=FIELD, text=SECRET),
                                 "Error: The action could not be completed.")
                rows = self.log() if self.path.exists() else []
                self.assertEqual(len(rows), expected_rows)
                self.assertEqual(rows[-1]["outcome"], "failed")
                self.assertIn("fixture failure with", rows[-1]["outcome_detail"])
        self.assertNotIn(SECRET, self.path.read_text())

    def test_daemon_entry_delegates_only_from_live_owner_hud(self):
        self.snap()
        session = SimpleNamespace(
            thread=SimpleNamespace(id="first", project_id="project", role=Role.CHAT,
                                   task_id=None, provider=ProviderName.CODEX),
            brief=SimpleNamespace(role=Role.CHAT, task_id=None), closing=False, retired=False, lost=False,
            worker=object(), turn_id="fixture", caller_key=object(), handle=object(),
            desk=SimpleNamespace(slot={"present": True, "images": True}),
            cancelled=threading.Event(), permit=Mock(return_value=Decision.ALLOW),
            provider=SimpleNamespace(admit_foreground_mcp=Mock(return_value=lambda: True)),
        )
        caller = SimpleNamespace(thread_id="first", provider="codex", session=session)
        self.daemon._lock = threading.RLock()
        self.daemon._sessions = {"first": session}
        self.daemon._stopping = False
        args = {"window": WINDOW, "ref": SAFE, "purpose": PURPOSE, "risky": "no"}

        def call(**options):
            return bridge.call(self.daemon, session, caller, "hud_window_click", args, **options)

        self.assertEqual(call(), {"text": hud.COMPLETED, "isError": False})
        session.permit.assert_called_once_with("hud_window_click", args, None)
        self.assertFalse(self.approvals.cards)
        session.desk.slot["present"] = False
        self.assertEqual(call()["text"], hud.UNAVAILABLE)
        session.desk.slot["present"] = True
        self.assertEqual(call(request_alive=lambda: False)["text"], hud.UNAVAILABLE)
        session.provider.admit_foreground_mcp.return_value = None
        self.assertIn("foreground", call()["text"])
        with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": "0"}):
            with self.assertRaisesRegex(APIError, "not offered"):
                call()
        self.assertEqual([op for op, _, _ in self.requests], ["windows", "window_snapshot", "window_click"])

    def test_audit_cuts_label_without_changing_bridge_expect(self):
        label = {"name": "x" * 150, "role": "button"}
        self.refs[WINDOW][SAFE] = label
        self.snap()
        self.assertEqual(self.action(ref=SAFE), hud.COMPLETED)
        self.assertEqual(self.requests[-1][2]["expect"], label)
        self.assertEqual(self.log()[-1]["name"], "x" * 120)

    def test_standard_card_can_only_be_allowed_by_verified_owner(self):
        self.snap()
        rejected = []

        def present(request):
            public = request.to_json()
            self.assertEqual(public["tool"], "hud_window_click")
            self.assertEqual(public["headline"], 'Click "Send now" in explorer.exe (send)')
            self.assertIn("Jarvis says", public["reason"])
            self.assertFalse(public["allowlistable"])
            with self.assertRaisesRegex(ValueError, "requires an answer in the owner HUD"):
                broker.resolve(request.req_id, Decision.ALLOW)
            rejected.append(True)
            self.assertIsNone(broker.resolve(request.req_id, Decision.ALLOW, always=True,
                                             owner_verified=True))

        broker = PendingApprovals(timeout_s=1, on_request=present)
        quiet = Mock()
        quiet.settle.return_value = ("granted", "fixture grant")
        broker.quiet_desk = quiet
        self.daemon.approvals = broker
        self.assertEqual(self.action(ref=RISKY), hud.COMPLETED)
        self.assertEqual(rejected, [True])
        quiet.settle.assert_not_called()

    def test_private_browser_card_can_only_be_allowed_by_verified_owner(self):
        attempts = []

        def present(request):
            for verified in (False, 1):
                with self.assertRaises(ValueError):
                    broker.resolve(request.req_id, Decision.ALLOW, owner_verified=verified)
                attempts.append(verified)
            broker.resolve(request.req_id, Decision.ALLOW, owner_verified=True)

        broker = PendingApprovals(timeout_s=1, on_request=present)
        card = ApprovalRequest("hud_browser_act", hud_only=True)
        self.assertIs(broker.ask(card), Decision.ALLOW)
        self.assertEqual(attempts, [False, 1])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Checks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("hud-window: ALL CHECKS PASSED")

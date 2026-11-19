"""Fix-round checks with synthetic bridge responses and an isolated audit file."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import threading
import unittest
from unittest.mock import Mock, patch

import hud_window_review_check as fixture
from jarvis.desktop import DesktopError
from jarvis.v2 import approvals
from jarvis.v2.approvals import ApprovalRequest
from jarvis.v2.provider import Decision
from jarvis.v2.tools import hud_window as window


EDIT, NEW, PRIMARY, SECONDARY = fixture.EDIT, fixture.NEW, fixture.PRIMARY, fixture.SECONDARY
EDITOR, PURPOSE = fixture.EDITOR, fixture.PURPOSE
COMPLETE = "Action completed. Take a fresh snapshot to see the result."
OUTCOME_ERROR = (
    "Error: The action outcome could not be recorded because the desktop action log could not be written."
)
FIELD_PLACEHOLDER = "a field showing the typed text"
APP_PLACEHOLDER = "an app showing the typed text"


class Review2(fixture.Review):
    def prepare_type(self, name=EDITOR["name"], app="Catalog.exe"):
        control = {"name": name, "role": "editable"}
        self.responses["windows"] = {"windows": [
            {"window": PRIMARY, "app": app, "title": "Fixture", "access": "drivable"},
        ]}
        self.assertIn(PRIMARY, self.invoke("list").text)
        self.responses["window_snapshot"] = {"snapshot": f"[{EDIT}]", "refs": {EDIT: control}}
        self.responses["window_probe"] = control
        self.assertIn(EDIT, self.snapshot())

    def typed_card(self, text, purpose=PURPOSE):
        broker = self.broker(lambda req: broker.resolve(req.req_id, Decision.DENY))
        self.assertIn("declined", self.act("type", ref=EDIT, text=text, purpose=purpose, risky="yes"))
        return self.cards[-1].to_json(), self.audit()[-1]

    def test_verified_answer_must_be_exact_true_and_match_request(self):
        self.assertIn(fixture.QUEUE, self.snapshot())
        attempts = []

        def present(req):
            if req.tool == "Bash":
                broker.resolve(req.req_id, Decision.ALLOW, owner_verified=True)
                attempts.append("other request approved")
                return
            for flag in (False, None, 1, "true", object()):
                try:
                    broker.resolve(req.req_id, Decision.ALLOW, owner_verified=flag)
                except ValueError:
                    attempts.append("unverified rejected")
            other = ApprovalRequest("Bash", args={"command": "git status"})
            if broker.ask(other) is Decision.ALLOW:
                attempts.append("other ask returned allow")
                try:
                    broker.resolve(other.req_id, Decision.ALLOW, owner_verified=True)
                except ValueError:
                    attempts.append("old id rejected")

        broker = self.broker(present)
        self.assertIn("declined", self.act("click", ref=fixture.QUEUE))
        self.assertEqual(attempts, ["unverified rejected"] * 5 + [
            "other request approved", "other ask returned allow", "old id rejected",
        ])
        self.assertEqual(self.actions(), [])
        self.assertEqual([row["outcome"] for row in self.audit()], ["asked-declined"])

    def test_bad_snapshot_clears_only_its_refs_and_good_reply_recovers(self):
        bridge_ref = "abcdef1234567890-e1"
        bad_refs = {
            "nested role": {bridge_ref: {"name": "Open", "role": ["button"]}},
            "nested name": {bridge_ref: {"name": {"value": "Open"}, "role": "button"}},
            "extra field": {bridge_ref: {**EDITOR, "rect": [0, 1, 2, 3]}},
            "case collision": {bridge_ref: EDITOR, bridge_ref.upper(): EDITOR},
        }
        self.assertIn(NEW, self.snapshot(SECONDARY))
        self.drive_chat("other-chat")
        self.assertIn(EDIT, self.snapshot(thread="other-chat"))
        self.drive_chat("review-chat")
        good_reply = {"snapshot": f"[{EDIT}]", "refs": {EDIT: EDITOR}}
        for kind, refs in bad_refs.items():
            with self.subTest(kind=kind):
                self.responses["window_snapshot"] = good_reply
                self.assertIn(EDIT, self.snapshot())
                self.responses["window_snapshot"] = {"snapshot": "Controls", "refs": refs}
                self.assertTrue(self.snapshot().startswith("Refused:"))
                actions = len(self.actions())
                for ref in (EDIT, bridge_ref):
                    self.assertIn("fresh snapshot", self.act("click", ref=ref))
                self.assertEqual(len(self.actions()), actions)
                self.assertEqual(self.act("click", on=SECONDARY, ref=NEW), COMPLETE)
                self.drive_chat("other-chat")
                self.assertEqual(self.act("click", ref=EDIT, via={"thread": "other-chat"}), COMPLETE)
                self.drive_chat("review-chat")
                self.responses["window_snapshot"] = good_reply
                self.assertIn(EDIT, self.snapshot())
                self.assertEqual(self.act("click", ref=EDIT), COMPLETE)

    def test_impossibly_large_snapshot_cannot_authorize_refs(self):
        # op_window_snapshot calls uiatree.snapshot with its default 150-element cap.
        self.assertIn(EDIT, self.snapshot())
        refs = {f"abcdef1234567890-e{number}": EDITOR for number in range(1, 4097)}
        self.responses["window_snapshot"] = {"snapshot": "Large reply", "refs": refs}
        result = self.snapshot()
        old = self.act("click", ref=EDIT)
        fresh = self.act("click", ref="abcdef1234567890-e4096")
        self.assertEqual((result.startswith("Refused:"), old.startswith("Refused:"),
                          fresh.startswith("Refused:"), len(self.actions())),
                         (True, True, True, 0), (result, old, fresh))

    def test_name_threshold_case_spacing_special_characters_and_app(self):
        for text, name, app in (
            ("Qz", "Qz field", "Qz.exe"),
            ("Select Client", "Select Client", "Catalog.exe"),
            ("  Mixed Case  ", "mIxEd CaSe", "MIXED CASE.exe"),
            ('A.[x]"\\B', 'Field a.[X]"\\b', 'a.[X]"\\b.exe'),
            ("App Secret", "Search", "App Secret.exe"),
        ):
            with self.subTest(text=text):
                self.prepare_type(name, app)
                token = text.strip().casefold()
                redact_name = len(token) >= 3 and token in name.casefold()
                redact_app = len(token) >= 3 and token in app.casefold()
                card, row = self.typed_card(text, f'Lookup {text.strip()} or "{text.strip()}".')
                self.assertEqual(row["name"], FIELD_PLACEHOLDER if redact_name else name)
                self.assertEqual(row["app"], APP_PLACEHOLDER if redact_app else app)
                self.assertIn(FIELD_PLACEHOLDER if redact_name else name, card["headline"])
                self.assertIn(APP_PLACEHOLDER if redact_app else app, card["headline"])
                self.assertNotIn(token, card["args"]["purpose"].casefold())
                self.assertNotIn(token, row["purpose"].casefold())
                self.assertEqual(card["args"]["text"], len(text))

    def test_risk_uses_unredacted_control_name(self):
        self.prepare_type("Search customer")
        self.assertEqual(self.act("type", ref=EDIT, text="Search customer", submit=True), COMPLETE)
        self.assertEqual(self.cards, [])
        row, = self.audit()
        self.assertEqual((row["name"], row["outcome"]), (FIELD_PLACEHOLDER, "run"))

    def test_typed_text_does_not_reappear_after_headline_normalization(self):
        self.prepare_type("Filter  Violet", "Violet \N{ZERO WIDTH SPACE}Orchard.exe")
        for text in ("Filter Violet", "Violet Orchard"):
            with self.subTest(text=text):
                card, _ = self.typed_card(text)
                self.assertNotIn(text.casefold(), card["headline"].casefold())

    def test_purpose_and_bridge_detail_redact_short_quoted_case_variant(self):
        self.prepare_type()
        text = "Qz"
        purpose = 'Look up "qZ".'
        card, _ = self.typed_card(text, purpose)
        self.responses["window_type"] = Mock(side_effect=DesktopError('Unable to enter "qZ".'))
        self.act("type", ref=EDIT, text=text, purpose=purpose)
        _, row = self.audit()
        self.assertEqual([value for value in (card["reason"], card["args"]["purpose"],
                                             row["purpose"], row["outcome_detail"])
                          if "qz" in value.casefold()], [])

    # Known edge case left open (owner's rule of 2026-11-19, plan section 11):
    # needs two failures at once, an outside change to the log file, or typed
    # text that happens to equal a ref or role name.
    @unittest.expectedFailure
    def test_typed_identifier_and_role_do_not_reach_card_or_audit(self):
        self.prepare_type()
        for text, where in ((EDIT, "card args"), ("editable", "log row")):
            with self.subTest(where=where):
                card, row = self.typed_card(text)
                actual = card["args"] if where == "card args" else row
                self.assertNotIn(text, json.dumps(actual).casefold())

    def test_fixed_bridge_sentences_for_every_acting_tool(self):
        self.assertIn(EDIT, self.snapshot())
        raw = "Refused: internal fixture detail\n" + "\U0001F6A7" * 230
        for op, args in (("click", {"ref": EDIT}), ("type", {"ref": EDIT, "text": "Synthetic input"}),
                         ("key", {"keys": ["Tab"]}), ("scroll", {"ref": EDIT, "amount": 1})):
            for error, sentence, outcome in (
                (DesktopError(raw), "Refused: The window refused that action. Take a fresh snapshot and try again.",
                 "bridge-refused"),
                (RuntimeError(raw), "Error: The action could not be completed.", "failed"),
            ):
                with self.subTest(op=op, error=type(error).__name__):
                    self.responses[f"window_{op}"] = Mock(side_effect=error)
                    count = len(self.audit())
                    self.assertEqual(self.act(op, **args), sentence)
                    row, = self.audit()[count:]
                    self.assertEqual((row["outcome"], row["outcome_detail"]), (outcome, raw[:200]))
                    self.assertEqual(len(self.logfile.read_bytes().splitlines()), count + 1)

    def test_read_only_tools_can_return_bridge_refusal(self):
        self.assertIn(EDIT, self.snapshot())
        raw = "Refused: synthetic read detail"
        for tool, op in (("list", "windows"), ("snapshot", "window_snapshot"),
                         ("screenshot", "window_screenshot")):
            with self.subTest(tool=tool):
                self.responses[op] = Mock(side_effect=DesktopError(raw))
                args = {} if tool == "list" else {"window": PRIMARY}
                self.assertEqual(self.invoke(tool, args).text, raw)
        self.assertEqual(self.audit(), [])

    def test_retry_and_two_interleaved_chats_keep_separate_rows(self):
        other = {"thread": "other-chat"}
        self.assertIn(EDIT, self.snapshot())
        self.drive_chat("other-chat")
        self.assertIn(EDIT, self.snapshot(**other))
        self.drive_chat("review-chat")
        entered, release = threading.Event(), threading.Event()

        def bridge(fields):
            if fields["scope"] == "review-chat" and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release first action")
                raise DesktopError("Refused: first attempt only")
            return {"clicked": True}

        self.responses["window_click"] = bridge
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.act, "click", ref=EDIT)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual([row["outcome"] for row in self.audit()], ["pending"])
                self.drive_chat("other-chat")
                self.assertEqual(self.act("click", ref=EDIT, via=other), COMPLETE)
                self.assertEqual([row["outcome"] for row in self.audit()], ["pending", "run"])
            finally:
                release.set()
            self.assertIn("window refused", first.result(timeout=5))
        self.drive_chat("review-chat")
        self.assertEqual(self.act("click", ref=EDIT), COMPLETE)
        self.assertEqual([(row["thread_id"], row["outcome"]) for row in self.audit()], [
            ("review-chat", "bridge-refused"), ("other-chat", "run"), ("review-chat", "run"),
        ])

    def test_failed_update_after_owner_approval_leaves_pending(self):
        self.assertIn(fixture.QUEUE, self.snapshot())
        broker = self.broker(lambda req: broker.resolve(req.req_id, Decision.ALLOW, owner_verified=True))
        real_sync = os.fsync

        def failing_sync(fd):
            if self.audit()[-1]["outcome"] == "asked-approved":
                raise OSError("fixture final fsync failure")
            real_sync(fd)

        with patch.object(os, "fsync", side_effect=failing_sync):
            self.assertEqual(self.act("click", ref=fixture.QUEUE), OUTCOME_ERROR)
        row, = self.audit()
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(stat.S_IMODE(self.logfile.stat().st_mode), 0o600)
        self.assertEqual(self.act("click", ref=fixture.QUEUE), COMPLETE)
        self.assertEqual([row["outcome"] for row in self.audit()], ["pending", "asked-approved"])
        self.assertEqual(len(self.actions()), 2)

    def test_short_reservation_write_prevents_dispatch(self):
        self.assertIn(EDIT, self.snapshot())
        self.assertEqual(self.act("click", ref=EDIT), COMPLETE)
        before = self.logfile.read_bytes()
        real_write = os.write
        with patch.object(os, "write", side_effect=lambda fd, data: real_write(fd, data[:20])):
            self.assertEqual(self.act("click", ref=EDIT),
                             "Refused: the desktop action log could not be written.")
        self.assertEqual(self.logfile.read_bytes(), before)
        self.assertEqual(len(self.actions()), 1)

    # Known edge case left open (owner's rule of 2026-11-19, plan section 11):
    # needs two failures at once, an outside change to the log file, or typed
    # text that happens to equal a ref or role name.
    @unittest.expectedFailure
    def test_failed_update_and_failed_rollback_never_claim_completion(self):
        self.assertIn(EDIT, self.snapshot())
        real_sync, real_write = os.fsync, os.write

        def fail_sync(fd):
            if self.audit()[-1]["outcome"] == "run":
                raise OSError("fixture final fsync failure")
            real_sync(fd)

        def fail_rollback(fd, data):
            if data.startswith(b'"pending"'):
                raise OSError("fixture rollback write failure")
            return real_write(fd, data)

        with patch.object(os, "fsync", side_effect=fail_sync), patch.object(os, "write", side_effect=fail_rollback):
            self.assertEqual(self.act("click", ref=EDIT), OUTCOME_ERROR)
        self.assertEqual([row["outcome"] for row in self.audit()], ["pending"])

    # Known edge case left open (owner's rule of 2026-11-19, plan section 11):
    # needs two failures at once, an outside change to the log file, or typed
    # text that happens to equal a ref or role name.
    @unittest.expectedFailure
    def test_log_mode_change_between_writes(self):
        self.assertIn(EDIT, self.snapshot())

        def change_mode(_):
            self.logfile.chmod(0o644)
            return {"clicked": True}

        self.responses["window_click"] = change_mode
        result = self.act("click", ref=EDIT)
        self.assertIn(result, (COMPLETE, OUTCOME_ERROR))
        self.assertEqual(stat.S_IMODE(self.logfile.stat().st_mode), 0o600)

    # Known edge case left open (owner's rule of 2026-11-19, plan section 11):
    # needs two failures at once, an outside change to the log file, or typed
    # text that happens to equal a ref or role name.
    @unittest.expectedFailure
    def test_log_path_replaced_by_symlink_between_writes(self):
        self.assertIn(EDIT, self.snapshot())
        original, target = self.root / "original.jsonl", self.root / "target.jsonl"
        target.write_text("fixture target\n")

        def replace_path(_):
            self.logfile.rename(original)
            self.logfile.symlink_to(target)
            return {"clicked": True}

        self.responses["window_click"] = replace_path
        result = self.act("click", ref=EDIT)
        self.assertEqual(target.read_text(), "fixture target\n")
        self.assertEqual((result, json.loads(original.read_text())["outcome"]), (OUTCOME_ERROR, "pending"))

    def test_interruptions_propagate_and_leave_dispatched_actions_pending(self):
        self.assertIn(EDIT, self.snapshot())
        for error in (KeyboardInterrupt, SystemExit):
            with self.subTest(error=error.__name__):
                count = len(self.audit())
                self.responses["window_probe"] = Mock(side_effect=error("fixture probe"))
                with self.assertRaises(error):
                    self.act("key", keys=["Tab"])
                self.assertEqual(len(self.audit()), count)
                self.responses.pop("window_probe")
                self.responses["window_key"] = Mock(side_effect=error("fixture dispatch"))
                with self.assertRaises(error):
                    self.act("key", keys=["Tab"])
                row, = self.audit()[count:]
                self.assertEqual(row["outcome"], "pending")

    def test_probe_and_approval_exceptions_leave_one_failed_row_each(self):
        self.assertIn(EDIT, self.snapshot())
        self.responses["window_probe"] = Mock(side_effect=RuntimeError("fixture probe"))
        for risky, message in (("no", "fixture probe"), ("yes", "fixture ask")):
            with self.subTest(stage=message):
                if risky == "yes":
                    self.responses.pop("window_probe")
                count = len(self.audit())
                with patch.object(self.daemon.approvals, "ask", side_effect=RuntimeError("fixture ask")):
                    self.assertEqual(self.act("key", keys=["Tab"], risky=risky),
                                     "Error: The action could not be completed.")
                row, = self.audit()[count:]
                self.assertEqual((row["outcome"], row["outcome_detail"]), ("failed", message))
        self.assertEqual(self.actions(), [])

    def test_approval_legacy_quiet_settlement_and_resolution_branches(self):
        for settlement, recorded, expected in (("granted", True, Decision.ALLOW),
                                                ("granted", False, Decision.DENY),
                                                ("deferred", True, Decision.DENY)):
            with self.subTest(settlement=settlement, recorded=recorded):
                present = Mock()
                broker = self.broker(present)
                desk = broker.quiet_desk = Mock()
                desk.settle.return_value = (settlement, "fixture")
                desk.granted.return_value = recorded
                request = ApprovalRequest("Bash", args={"command": "git status"})
                self.assertIs(broker.ask(request), expected)
                getattr(desk, settlement).assert_called_once_with(request)
                present.assert_not_called()
        for private in (False, True):
            with self.subTest(private=private):
                rejected = []

                def present(req):
                    if private:
                        try:
                            broker.resolve(req.req_id, Decision.ALLOW, owner_verified=1)
                        except ValueError:
                            rejected.append(1)
                    broker.resolve(req.req_id, Decision.ALLOW, always=True, owner_verified=private)

                broker = self.broker(present)
                request = ApprovalRequest("Bash", args={"command": "git status"}, hud_only=private)
                with patch.object(approvals.permissions, "add_allow", return_value={"rule": "fixture"}) as add:
                    self.assertIs(broker.ask(request), Decision.ALLOW)
                self.assertEqual(add.call_count, 0 if private else 1)
                self.assertEqual(rejected, [1] if private else [])

    def test_approval_legacy_positional_summary_fields(self):
        # Positional suffix on main was owner_initiated, summary, summary_state, where.
        args = (
            "Bash", {"command": "git status"}, None, "", "", None, None, None, "", True, "",
            None, None, "", "", "", False, None, None, "", None, None, True, False,
            "Show the changes.", "ready", {"project": "Fixture"},
        )
        request = ApprovalRequest(*args)
        self.assertEqual((request.require_hud_answer, request.summary, request.summary_state, request.where),
                         (False, "Show the changes.", "ready", {"project": "Fixture"}))


def load_tests(loader, _standard_tests, _pattern):
    """Reuse the first review's fixture while running only this round's checks."""
    return unittest.TestSuite(Review2(name) for name in loader.getTestCaseNames(Review2)
                              if name in Review2.__dict__)


if __name__ == "__main__":
    unittest.main()

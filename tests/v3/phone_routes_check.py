"""Exercise the phone's data routes over its isolated loopback listener."""

from dataclasses import replace
import importlib.util
import json
import os
import sys
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from a6b1_acceptance_check import Model, reply
import phone_auth_check as auth_check
from phone_listener_check import HOST, request

from jarvis import config, llm, sessions
from jarvis.v3 import owner_routes, phone_routes
from jarvis.v3.approvals import ApprovalBroker
from jarvis.v3.chat import MAX_INBOX, ChatService
from jarvis.v3.daemon import Daemon
from jarvis.v3.work import Ledger, Registry, WorkError


DATA_ROUTES = (
    ("GET", "status"), ("GET", "feed"), ("POST", "feed/answer"), ("POST", "feed/feedback"),
    ("GET", "chat"), ("POST", "chat/events"), ("POST", "chat/send"), ("POST", "chat/stop"),
    ("POST", "chat/new"), ("GET", "approvals"), ("POST", "approvals/answer"), ("GET", "work"),
)
BRIEF_FIELDS = {
    "id", "kind", "rank", "due", "ask", "why_now", "recommend", "options", "default", "context",
    "state", "feedback",
}
STATUS_FIELDS = {"service", "version", "pid", "uptime_s", "jobs"}
LOCKED = (423, {"error": "locked", "unlock": True})
UNAUTHORIZED = (401, {"error": "unauthorized"})
APPROVAL_ANSWER = "approvals/answer"


class PhoneRoutesCheck(unittest.TestCase):
    call = auth_check.PhoneAuthCheck.call
    ok = auth_check.PhoneAuthCheck.ok
    pair = auth_check.PhoneAuthCheck.pair
    sign_in = auth_check.PhoneAuthCheck.sign_in
    unlock = auth_check.PhoneAuthCheck.unlock
    me = auth_check.PhoneAuthCheck.me

    def setUp(self):
        auth_check.PhoneAuthCheck.setUp(self)
        self.data = self.root / "data"
        self.enterContext(patch.dict(os.environ, {"JARVIS_VAULT": str(self.data / "vault"),
                                                  "JARVIS_INDEX_DB": str(self.data / "index.sqlite")}))
        self.enterContext(patch.object(config, "V3_DIR", self.data))
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.root / "no-token.json"))
        self.enterContext(patch.object(sessions, "AUTO_TITLE", False))
        self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("Real model call")))

    def start(self, model=None):
        self.broker = ApprovalBroker(clock=lambda: 1000, log=self.data / "approvals.jsonl")
        self.addCleanup(self.broker.shutdown)
        self.chat = ChatService(self.data / "chats", completion=Model() if model is None else model,
                                approvals=self.broker)
        self.registry = Registry(self.data / "work/projects.json")
        self.ledger = Ledger(self.data / "work/threads.jsonl")
        self.daemon = Daemon(port=0, phone_host=HOST, data_dir=self.data, hud_dir=self.root / "hud",
                             jobs=(), chat=self.chat, work=lambda: (self.registry, self.ledger))
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.server = self.daemon._phone_server
        self.auth = self.server.auth
        self.auth.mono = lambda: self.clock.now
        self.feed = self.server.services.feed()
        self.pair()
        self.cookie = self.sign_in().split(";", 1)[0]

    def api(self, route, *, method="POST", body=None):
        status, _, payload = self.call(route, method=method, body=body, cookie=self.cookie)
        return status, payload

    def window(self, method, path, *, body=None, cookie=None):
        host = f"127.0.0.1:{self.daemon.port}"
        headers = {"Sec-Fetch-Site": "same-origin"}
        if cookie is not None:
            headers["Cookie"] = cookie
        status, _, payload = request(self.daemon.port, method, path, host=host,
                                     origin=f"http://{host}", headers=headers, body=body)
        return status, json.loads(payload)

    def open_approval(self):
        results = []
        approved = []

        def ask():
            results.append(self.broker.ask(
                "fixture_tool", {}, {"title": "Fixture card", "rows": [{"label": "Folder", "value": "fixture"}]},
                session_id="fixture-session", turn_id="fixture-turn", verdict={"level": "ask", "why": "Fixture"},
                on_approved=lambda *receipt: approved.append(receipt),
            ))

        thread = Thread(target=ask, daemon=True)
        thread.start()

        def cleanup():
            self.broker.cancel()
            thread.join(3)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 3
        while not (pending := self.broker.pending()):
            self.assertLess(time.monotonic(), deadline, "No pending approval")
            time.sleep(0.01)
        (card,) = pending
        return {key: card[key] for key in ("id", "digest")}, thread, results, approved

    def post_brief(self):
        return self.feed.post({
            "kind": "decision", "rank": 4, "ask": "Fixture brief", "why_now": "Fixture reason",
            "recommend": {"label": "Fixture recommendation", "because": "Fixture rationale"},
            "options": [{"label": "Fixture option"}], "default": {"action": "none", "at": None},
            "context": [{"label": "Fixture context", "ref": "https://phone.example.invalid/example"}],
            "attachment": {"type": "draft", "ref": "fixture-attachment"},
            "proposal_id": "fixture-proposal",
        })["id"]

    def test_01_exact_table_and_import_time_window_route_lookup(self):
        table = phone_routes.PHONE_ROUTES
        self.assertEqual(set(table), {(method, f"/m/api/{name}") for method, name in DATA_ROUTES})
        self.assertEqual([key for key, route in table.items() if route.owner_key is None],
                         [("GET", "/m/api/status")])
        for (method, path), route in table.items():
            if route.owner_key is None:
                self.assertIsNone(route.owner_route)
            else:
                key = (method, path.replace("/m/api/", "/v3/owner/", 1))
                self.assertEqual(route.owner_key, key)
                self.assertIs(route.owner_route, owner_routes.ROUTES[key])

        name = "jarvis.v3._fixture_phone_routes"
        spec = importlib.util.spec_from_file_location(name, phone_routes.__file__)
        module = importlib.util.module_from_spec(spec)
        key = ("GET", "/v3/owner/work")
        with patch.dict(sys.modules, {name: module}), patch.dict(owner_routes.ROUTES):
            del owner_routes.ROUTES[key]
            with self.assertRaises(KeyError) as failure:
                spec.loader.exec_module(module)
            self.assertEqual(failure.exception.args, (key,))

    def test_02_locked_approve_keeps_the_request_pending(self):
        self.start()
        body, _, _, _ = self.open_approval()
        before = self.broker.pending()
        with patch.object(self.broker, "answer", wraps=self.broker.answer) as answer:
            self.assertEqual(self.api(APPROVAL_ANSWER, body={**body, "decision": "approve"}), LOCKED)
            answer.assert_not_called()
        self.assertEqual(self.broker.pending(), before)

    def test_03_unlocked_approve_slides_and_answers_once(self):
        self.start()
        body, thread, results, approved = self.open_approval()
        self.unlock(self.cookie)
        self.clock.now += 120
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3480)
        body["decision"] = "approve"
        self.assertEqual(self.api(APPROVAL_ANSWER, body=body), (200, {"id": body["id"], "state": "approved"}))
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3600)
        self.assertEqual(self.api(APPROVAL_ANSWER, body=body),
                         (409, {"error": "Approval conflicts with the pending request."}))
        thread.join(3)
        self.assertEqual(results, [True])
        self.assertEqual(approved, [(body["id"], body["digest"])])
        self.assertEqual(self.broker.pending(), [])
        log = (self.data / "approvals.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(row)["state"] for row in log], ["pending", "approved"])

    def test_04_deny_works_while_locked(self):
        self.start()
        body, thread, results, approved = self.open_approval()
        self.assertEqual(self.api(APPROVAL_ANSWER, body={**body, "decision": "deny"}),
                         (200, {"id": body["id"], "state": "denied"}))
        thread.join(3)
        self.assertEqual(results, [False])
        self.assertEqual(approved, [])
        self.assertEqual(self.me(self.cookie)["unlock_s"], 0)

    def test_05_unknown_or_malformed_approval_needs_unlock(self):
        self.start()
        body, _, _, _ = self.open_approval()
        before = self.broker.pending()
        invalid = (
            {**body, "decision": "other"}, {**body, "decision": "Deny"}, {**body, "decision": ["deny"]},
            body, b"{", b"[]", b'{"decision":"deny","decision":"approve"}',
            {"decision": "deny"}, {**body, "decision": "deny", "extra": "fixture"},
        )
        with patch.object(self.broker, "answer", wraps=self.broker.answer) as answer:
            for malformed in invalid:
                with self.subTest(body=malformed):
                    self.assertEqual(self.api(APPROVAL_ANSWER, body=malformed), LOCKED)
            self.assertEqual(self.broker.pending(), before)
            self.unlock(self.cookie)
            for malformed in invalid:
                with self.subTest(unlocked_body=malformed):
                    self.assertEqual(self.api(APPROVAL_ANSWER, body=malformed)[0], 400)
            answer.assert_not_called()
        self.assertEqual(self.broker.pending(), before)

    def test_06_locked_feed_approve_and_edit_leave_the_brief_open(self):
        self.start()
        bid = self.post_brief()
        before = self.feed.get(bid)
        with patch.object(self.feed, "answer", wraps=self.feed.answer) as answer:
            for action in ({"action": "approve"}, {"action": "edit", "text": "Fixture edit"},
                           {"action": "Not_now"}, {}):
                with self.subTest(action=action):
                    self.assertEqual(self.api("feed/answer", body={"id": bid, **action}), LOCKED)
            answer.assert_not_called()
        self.assertEqual(self.feed.get(bid), before)

    def test_07_not_now_and_other_work_while_locked(self):
        self.start()
        for body, expected in (({"action": "not_now"}, "dismissed"),
                               ({"action": "other", "text": "Fixture answer"}, "other")):
            with self.subTest(action=body["action"]):
                bid = self.post_brief()
                status, payload = self.api("feed/answer", body={"id": bid, **body})
                self.assertEqual(status, 200)
                brief = self.feed.get(bid)
                self.assertEqual(brief["state"], expected)
                self.assertEqual(payload, {"brief": {key: brief[key] for key in BRIEF_FIELDS}})

    def test_08_status_only_returns_phone_fields(self):
        self.start()
        window_status, original = self.window("GET", "/v3/status")
        self.assertEqual(window_status, 200)
        self.assertTrue({"data_dir", "phone", "started_at"} <= original.keys())
        self.assertEqual(original["data_dir"], str(self.data))
        self.assertEqual(original["phone"], {"bound": True, "host_configured": True})
        status, payload = self.api("status", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(payload.keys(), STATUS_FIELDS)
        self.assertGreaterEqual(payload["uptime_s"], original["uptime_s"])
        for key in STATUS_FIELDS - {"uptime_s"}:
            self.assertEqual(payload[key], original[key])

        dropped = original.keys() - STATUS_FIELDS
        markers = {key: f"fixture-status-{key}" for key in dropped}
        sample = {**original, **markers, "jobs": {"fixture-job": {"failures": 0}}}
        with patch.object(self.server, "services", replace(self.server.services, status=lambda: sample)):
            self.assertEqual(self.api("status", method="GET"), (200, {k: sample[k] for k in STATUS_FIELDS}))

    def test_09_open_and_recent_feed_briefs_match_the_panel(self):
        self.start()
        open_id, recent_id = self.post_brief(), self.post_brief()
        self.feed.answer(recent_id, "other", text="Fixture answer")
        raw = self.feed.snapshot()
        self.assertEqual([row["id"] for row in raw["briefs"]], [open_id])
        self.assertEqual([row["id"] for row in raw["recent"]], [recent_id])
        expected = {key: [{field: row[field] for field in BRIEF_FIELDS} for row in rows]
                    for key, rows in raw.items()}
        self.assertEqual(self.api("feed", method="GET"), (200, expected))
        for rows in raw.values():
            for row in rows:
                for field in row.keys() - BRIEF_FIELDS:
                    row[field] = f"fixture-dropped-{field}"
        with patch.object(self.feed, "snapshot", return_value=raw):
            self.assertEqual(self.api("feed", method="GET"), (200, expected))

    def test_10_running_turn_receives_only_desk_steering(self):
        entered, release = Event(), Event()

        def first(_messages):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture did not release the first completion")
            return reply(calls=[("get_datetime", {})])

        model = Model(first, reply("Fixture first reply"), reply("Fixture following reply"))
        self.start(model)
        self.addCleanup(release.set)
        self.assertEqual(self.window("POST", "/v3/owner/chat/send", body={"text": "Fixture first turn"}),
                         (202, {"state": "running", "steered": False}))
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.api("chat/send", body={"text": "Fixture phone text"}),
                         (202, {"state": "running", "steered": False}))
        self.assertEqual(self.window("POST", "/v3/owner/chat/send", body={"text": "Fixture desk steering"}),
                         (202, {"state": "running", "steered": True}))
        release.set()
        deadline = time.monotonic() + 5
        while (state := self.chat.snapshot()["state"]) == "running":
            self.assertLess(time.monotonic(), deadline, "Chat did not finish")
            time.sleep(0.01)
        self.assertEqual(state, "idle")
        self.assertEqual(len(model.seen), 3)
        self.assertNotIn("Fixture phone text", json.dumps(model.seen[:2]))
        self.assertIn({"role": "user", "content": "[owner steering] Fixture desk steering"},
                      model.seen[1]["messages"])
        self.assertIn({"role": "user", "content": "Fixture phone text"}, model.seen[2]["messages"])

    def test_11_reads_preserve_the_unlock_deadline(self):
        self.start()
        reads = (("status", "GET", None), ("feed", "GET", None), ("chat", "GET", None),
                 ("chat/events", "POST", {"after": 0}), ("approvals", "GET", None), ("work", "GET", None))
        self.unlock(self.cookie)
        self.clock.now += 123
        for route, method, body in reads:
            with self.subTest(route=route):
                self.assertEqual(self.api(route, method=method, body=body)[0], 200)
                self.assertEqual(self.me(self.cookie)["unlock_s"], 3477)

    def test_12_session_write_slides_only_an_open_window(self):
        self.start()
        bid = self.post_brief()
        body = {"id": bid, "tag": "too_long"}
        status, payload = self.api("feed/feedback", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["brief"].keys(), BRIEF_FIELDS)
        self.assertEqual(payload["brief"]["feedback"], ["too_long"])
        self.assertEqual(self.me(self.cookie)["unlock_s"], 0)
        self.unlock(self.cookie)
        self.clock.now += 100
        self.assertEqual(self.api("feed/feedback", body={**body, "tag": "missing_context"})[0], 200)
        self.assertEqual(self.me(self.cookie)["unlock_s"], 3600)
        self.assertEqual(self.feed.get(bid)["feedback"], ["too_long", "missing_context"])

    def test_13_unknown_paths_require_a_session_then_return_not_found(self):
        self.start()
        for route, method in (("missing", "GET"), ("whoami", "GET"), ("ping", "POST"),
                              ("learned/pin", "POST"), ("status", "POST")):
            with self.subTest(route=route, method=method):
                status, _, payload = self.call(route, method=method)
                self.assertEqual((status, payload), UNAUTHORIZED)
                self.assertEqual(self.api(route, method=method), (404, {"error": "not found"}))

    def test_expiry_between_two_approval_requests(self):
        self.start()
        first, _, _, _ = self.open_approval()
        self.unlock(self.cookie)
        self.clock.now += 3599
        self.assertEqual(self.api(APPROVAL_ANSWER, body={**first, "decision": "approve"})[0], 200)
        second, _, _, _ = self.open_approval()
        self.clock.now += 3601
        before = self.broker.pending()
        self.assertEqual(self.api(APPROVAL_ANSWER, body={**second, "decision": "approve"}), LOCKED)
        self.assertEqual(self.broker.pending(), before)

    def test_every_data_route_refuses_missing_and_foreign_sessions(self):
        self.start()
        writer = auth_check.remote_auth.RemoteAuth(self.root / "foreign-remote", HOST)
        code = urlsplit(writer.open_window()["url"]).fragment
        writer.pair_verify(code, auth_check.credential(writer.pair_options(code)))
        token, _ = writer.login_verify(auth_check.credential(writer.login_options()))
        foreign = f"__Host-jarvis_m={token}"
        self.assertIsNotNone(writer.session_for([foreign]))
        for method, route in DATA_ROUTES:
            for cookie in (None, foreign):
                with self.subTest(method=method, route=route, foreign=cookie is not None):
                    status, _, payload = self.call(route, method=method, cookie=cookie,
                                                   body=b"{" if method == "POST" else None)
                    self.assertEqual((status, payload), UNAUTHORIZED)

    def test_data_dispatch_checks_session_before_decoding(self):
        self.start()
        original = phone_routes.data_route
        with (patch("jarvis.v3.phone_routes.phone_auth.session_for", side_effect=[{}, None]),
              patch("jarvis.v3.phone_routes._decode_route_body") as decode,
              patch.object(phone_routes, "data_route", wraps=original) as dispatch):
            self.assertEqual(self.api(APPROVAL_ANSWER, body=b"{"), UNAUTHORIZED)
            dispatch.assert_called_once()
            decode.assert_not_called()

    def test_window_refuses_every_phone_data_route(self):
        self.start()
        self.unlock(self.cookie)
        for method, route in DATA_ROUTES:
            with self.subTest(method=method, route=route):
                status, _ = self.window(method, "/m/api/" + route,
                                        body={} if method == "POST" else None, cookie=self.cookie)
                self.assertEqual(status, 405 if method == "POST" else 404)

    def test_work_and_approval_cards_match_the_window(self):
        self.start()
        self.open_approval()
        self.assertEqual(self.api("approvals", method="GET"), self.window("GET", "/v3/owner/approvals"))
        self.assertEqual(len(self.api("approvals", method="GET")[1]["approvals"]), 1)
        folder = self.root / "fixture-checkout"
        folder.mkdir()
        self.registry.add("fixture", folder, provider="fixture-provider", model="fixture-model")
        self.ledger.record_start(thread_id="fixture-thread", run_id="fixture-run", project="fixture",
                                 title="Fixture thread", branch=None, brief_sha256="a" * 64, at=10)
        status, work = self.api("work", method="GET")
        self.assertEqual((status, work), self.window("GET", "/v3/owner/work"))
        self.assertEqual(status, 200)
        self.assertEqual(work["projects"], [{"name": "fixture"}])
        self.assertEqual([row["thread_id"] for row in work["threads"]], ["fixture-thread"])
        with patch.object(self.ledger, "threads", side_effect=WorkError("fixture error detail")):
            self.assertEqual(self.api("work", method="GET"), (503, {"error": "Service Unavailable"}))

    def test_chat_actions_and_route_error_responses(self):
        entered, release = Event(), Event()

        def hold(_messages):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture did not release the completion")
            return reply("Fixture reply")

        self.start(Model(hold))
        self.addCleanup(release.set)
        self.assertEqual(self.api("chat/send", body={"text": None}), (400, {"error": "Invalid chat message."}))
        self.assertEqual(self.api("chat/events", body={"after": -1}),
                         (400, {"error": "Expected a nonnegative event sequence number."}))
        self.assertEqual(self.api("feed/feedback", body={"id": "0" * 32, "tag": "too_long"}),
                         (404, {"error": "Brief not found."}))
        status, new_chat = self.api("chat/new", body={})
        self.assertEqual(status, 200)
        self.assertEqual(self.api("chat", method="GET")[1]["session_id"], new_chat["session_id"])
        self.assertEqual(self.api("chat/send", body={"text": "Fixture question"})[0], 202)
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.api("chat/new", body={}), (409, {"error": "Chat is busy."}))
        for index in range(MAX_INBOX):
            self.assertEqual(self.api("chat/send", body={"text": f"Fixture waiting {index}"})[0], 202)
        self.assertEqual(self.api("chat/send", body={"text": "Fixture overflow"}),
                         (429, {"error": "The chat steering queue is full."}))
        self.assertEqual(self.api("chat/stop", body={}), (200, {"state": "stopping"}))


if __name__ == "__main__":
    unittest.main()

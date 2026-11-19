"""HUD region counts and the owner route, with a fake bridge and ephemeral listeners."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.browser import BrowserPolicy
from jarvis.tools import web
from jarvis.v2 import hud_sensitive
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.browser import policy
from jarvis.v2.daemon import _handler
from jarvis.v2.tools import hud_window
from windows.desktopguard import HUD_REGION_NAME

ROUTE = "/hud/sensitive"
CLIENT = "a" * 32
NOW = 100.0


class CountChecks(unittest.TestCase):
    def test_report_age_and_distinct_clients(self):
        count = hud_sensitive.count
        self.assertIsNone(count({}, 0, NOW))
        self.assertIsNone(count({CLIENT: (1, 0, NOW - 5.0)}, 0, NOW))
        self.assertIsNone(count({CLIENT: (1, 0, NOW + 0.1)}, 0, NOW))
        reports = {CLIENT: (1, 2, NOW - 4.999), "other": (0, 0, NOW)}
        self.assertIsNone(count(reports, 0, NOW))
        reports["other"] = (0, 0, NOW - 5.0)
        self.assertEqual(count(reports, 0, NOW), 3)

    def test_both_counts_are_bounded_nonnegative_integers(self):
        for invalid in (-1, 65, True, False, None, 1.0, "1"):
            for cards, editors in ((invalid, 1), (1, invalid)):
                with self.subTest(cards=cards, editors=editors):
                    self.assertIsNone(hud_sensitive.count({CLIENT: (cards, editors, NOW)}, 0, NOW))
        self.assertEqual(hud_sensitive.count({CLIENT: (64, 64, NOW)}, 0, NOW), 128)

    def test_pending_approval_requires_a_card_but_not_one_per_request(self):
        count = hud_sensitive.count
        self.assertEqual(count({CLIENT: (0, 0, NOW)}, 0, NOW), 0)
        self.assertEqual(count({CLIENT: (0, 2, NOW)}, 0, NOW), 2)
        self.assertIsNone(count({CLIENT: (0, 2, NOW)}, 1, NOW))
        self.assertEqual(count({CLIENT: (1, 2, NOW)}, 3, NOW), 3)

    def test_expiry_drops_only_old_clients_without_another_report(self):
        reports = hud_sensitive.Reports()
        self.addCleanup(reports.close)
        with (patch.object(hud_sensitive.time, "monotonic", return_value=NOW) as clock,
              patch.object(hud_sensitive.threading, "Timer") as timer):
            reports.record(CLIENT, 1, 0)
            clock.return_value += 59.0
            reports.record("other", 2, 1)
            timer.assert_called_once_with(60.0, reports._expire)
            clock.return_value += 1.0
            reports._expire()
            self.assertEqual(set(reports._reports), {"other"})
            self.assertEqual(reports.count(0), 3)
            timer.assert_called_with(59.0, reports._expire)


class WireChecks(unittest.TestCase):
    def setUp(self):
        self.ready = threading.Event()
        approvals = PendingApprovals(on_request=lambda _: self.ready.set())
        reports = hud_sensitive.Reports()
        self.addCleanup(reports.close)
        self.addCleanup(approvals.shutdown)
        self.daemon = SimpleNamespace(stores=None, approvals=approvals, hud_sensitive_reports=reports)
        self.face = self.listen()
        self.daemon.face_port = self.face
        self.api = self.listen()

    def listen(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.daemon))
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        self.assertNotIn(port, range(8402, 8410))
        worker = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        return port

    def post(self, port, *, origin=None, host=None, **fields):
        body = {"client_id": CLIENT, "cards": 0, "editors": 0} | fields
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("POST", ROUTE, json.dumps(body), headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_reports_require_owner_listener_host_and_origin(self):
        origin = f"http://127.0.0.1:{self.face}"
        self.assertEqual(self.post(self.api, origin=f"http://127.0.0.1:{self.api}"), 403)
        self.assertEqual(self.post(self.api, origin=origin), 403)
        self.assertEqual(self.post(self.face), 403)
        self.assertEqual(self.post(self.face, origin="null"), 403)
        self.assertEqual(self.post(self.face, origin="http://foreign.invalid"), 403)
        host = f"foreign.invalid:{self.face}"
        self.assertEqual(self.post(self.face, host=host, origin=f"http://{host}"), 403)
        self.assertIsNone(self.daemon.hud_sensitive_reports.count(0))
        self.assertEqual(self.post(self.face, origin=origin), 200)
        self.assertEqual(self.daemon.hud_sensitive_reports.count(0), 0)
        self.assertEqual(self.post(self.face, origin=origin, cards=True), 400)
        self.assertIsNone(self.daemon.hud_sensitive_reports.count(0))

    def test_bridge_gets_computed_value_using_the_brokers_pending_set(self):
        origin = f"http://127.0.0.1:{self.face}"
        self.assertEqual(self.post(self.face, origin=origin), 200)
        service = hud_window.WindowDrive(self.daemon)
        with patch.object(hud_window.SESSION, "request", return_value={}) as request:
            def sent():
                service.request("windows", "fixture", current=lambda: True)
                return request.call_args.kwargs["hud_sensitive"]

            self.assertEqual(sent(), 0)
            broker = self.daemon.approvals
            worker = threading.Thread(target=broker.ask, args=(ApprovalRequest("fixture"),), daemon=True)
            worker.start()
            try:
                self.assertTrue(self.ready.wait(3))
                self.assertIsNone(sent())
                self.assertEqual(self.post(self.face, origin=origin, cards=1, editors=2), 200)
                self.assertEqual(sent(), 3)
                self.assertEqual(self.post(self.face, origin=origin, client_id="b" * 32), 200)
                self.assertIsNone(sent())
            finally:
                broker.shutdown()
                worker.join(3)


class BoundaryChecks(unittest.TestCase):
    def test_agent_browser_and_fetch_refuse_the_control_port_before_io(self):
        for port in (config.FACE_PORT, config.DAEMON_PORT):
            url = f"http://127.0.0.1:{port}{ROUTE}"
            with self.subTest(port=port), patch.object(web.httpx, "Client") as client:
                self.assertIn("own control plane", web.fetch_page(url))
                client.assert_not_called()
                self.assertFalse(BrowserPolicy().allows(url))
                with self.assertRaises(policy.URLRefused) as refused:
                    policy.judge_url(url)
                self.assertEqual(refused.exception.reason, policy.JARVIS_PORT)

    def test_hud_source_uses_the_bridge_label(self):
        source = Path(__file__).resolve().parents[2] / "hud/src/lib/hudSensitive.ts"
        self.assertIn(json.dumps(HUD_REGION_NAME), source.read_text())


if __name__ == "__main__":
    unittest.main()

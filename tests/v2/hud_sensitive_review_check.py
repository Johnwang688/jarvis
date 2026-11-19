"""Independent CU-3a checks. Browser tests use the built HUD and only fixture data."""

import json
import http.client
from http.server import ThreadingHTTPServer
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from jarvis.v2 import hud_sensitive
from jarvis.v2.daemon import _handler
import hud_sensitive_check as existing
from tests.face.hud_v2_check import FAKE_RECOGNIZER, guard_live, until
from tests.face.hud_v2_drive_check import private_card
from tests.face.hud_v2_editor_check import TWO, WIDE
from tests.face.hud_v2_inbox_check import defer_row
from tests.face.hud_v2_layout_check import _approval, _pane, _set_storage, _set_ws
from tests.face.hud_v2_mock import DIST, MockDaemon
from tests.face.hud_v2_mock_inbox import state as inbox_state
from tests.face.hud_v2_review_check import report as morning_report
from desktop_guard_check import FakeDesktop
from windows import desktopguard as dg, windowpolicy as wp


REPORT = "/hud/sensitive"
NAME = dg.HUD_REGION_NAME
GROUP = f'[role="group"][aria-label={json.dumps(NAME)}]'
APPROVAL_BUTTONS = (
    '[data-testid="approval-allow"], [data-testid="approval-deny"], [data-testid="approval-always"], '
    '[data-testid="inbox-deferred-retry"], [data-testid="inbox-deferred-leave"]'
)
ENV = ".env.production"


class HudChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (DIST / "index.html").is_file():
            raise RuntimeError("Build the HUD with npm run build before running this check.")
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.mock = MockDaemon(0).start()
        self.addCleanup(self.mock.stop)
        self.assertNotIn(self.mock.port, range(8402, 8410))
        self.base = f"http://127.0.0.1:{self.mock.port}"
        self.ctx = self.browser.new_context(
            viewport={"width": WIDE[0], "height": WIDE[1]}, service_workers="block")
        self.live_requests = []
        self.addCleanup(lambda: self.assertEqual(self.live_requests, []))
        self.addCleanup(self.ctx.close)
        guard_live(self.ctx, self.live_requests.append, ports=range(8402, 8410))
        self.ctx.add_init_script(FAKE_RECOGNIZER)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(5000)

    def boot(self):
        self.page.goto(self.base + "/")
        expect(self.page.get_by_test_id("project-p1")).to_be_attached()
        _set_storage(self.page, zoom="100", layout="{}")
        _set_ws(self.page, TWO)
        before = self.mock.sse_connections()
        self.page.reload()
        expect(self.page.get_by_test_id("project-p1")).to_be_attached()
        self.assertTrue(self.mock.await_reconnect(before))

    def open_file(self, name):
        self.page.locator(_pane(2, f'[data-testid="file-{name}"]')).click()
        lines = self.page.locator(_pane(2, '[data-testid="editor"] .view-lines'))
        expect(lines).to_be_visible()
        return lines

    def named_groups(self):
        return self.page.get_by_role("group", name=NAME, exact=True)

    def assert_buttons_inside_groups(self):
        buttons = self.page.locator(APPROVAL_BUTTONS)
        self.assertGreater(buttons.count(), 0)
        outside = buttons.evaluate_all(
            "(buttons, selector) => buttons.filter(b => !b.closest(selector)).map(b => b.innerText)",
            GROUP)
        self.assertEqual(outside, [], "desktop approval buttons outside named groups")

    def test_a_full_cards_including_browser_and_queue_have_one_complete_group(self):
        self.boot()
        public, detail = private_card("reviewbrow01", "0123abcd")
        self.ctx.route(f"**/browser/approvals/{public['req_id']}",
                       lambda route: route.fulfill(json=detail))
        _approval(self.mock, "reviewordinary", "fixture ordinary command")
        _approval(self.mock, "reviewqueued", "fixture queued command")
        queue = self.page.get_by_test_id("approval-queue")
        expect(queue).to_contain_text("fixture queued command")
        expect(queue.locator(APPROVAL_BUTTONS)).to_have_count(0)
        for request_id, expected_text in (
            ("reviewordinary", "fixture ordinary command"),
            ("reviewqueued", "fixture queued command"),
            (public["req_id"], "PRIVATE_CARD_TEXT"),
        ):
            with self.subTest(request=request_id):
                if request_id == public["req_id"]:
                    self.mock.emit("approval_requested", public)
                card = self.page.locator(f'[data-testid="approval-card"][data-req="{request_id}"]')
                expect(card).to_contain_text(expected_text)
                expect(self.named_groups()).to_have_count(1)
                expect(card.and_(self.named_groups())).to_have_count(1)
                expect(card.get_by_test_id("approval-allow")).to_have_count(1)
                expect(card.get_by_test_id("approval-deny")).to_have_count(1)
                expect(self.page.locator(f"{GROUP} {GROUP}")).to_have_count(0)
                self.assert_buttons_inside_groups()
                card.get_by_test_id("approval-deny").click()
        expect(self.page.get_by_test_id("approval-card")).to_have_count(0)

    def test_a_credential_pane_has_one_complete_group(self):
        fixture_text = "HUD_REVIEW_FIXTURE=local-test-value"
        self.mock.disk_write(ENV, fixture_text + "\n")
        self.boot()
        lines = self.open_file(ENV)
        expect(lines).to_contain_text(fixture_text)
        group = self.named_groups()
        expect(group).to_have_count(1)
        self.assertTrue(lines.evaluate("(node, selector) => !!node.closest(selector)", GROUP))
        copies = self.page.locator(".view-lines").filter(has_text=fixture_text)
        expect(copies).to_have_count(1)
        expect(group.get_by_test_id("editor-lock")).to_have_count(1)
        expect(group.get_by_test_id("file-save")).to_have_count(1)
        expect(self.page.locator(f"{GROUP} {GROUP}")).to_have_count(0)

    def test_a_deferred_inbox_approval_buttons_are_inside_a_named_group(self):
        inbox_state(self.mock)["rows"] = [defer_row(command="fixture command")]
        self.boot()
        self.page.get_by_test_id("working-needs-line").click()
        retry = self.page.get_by_test_id("inbox-deferred-retry")
        expect(retry).to_be_enabled()
        expect(self.page.get_by_test_id("inbox-deferred-leave")).to_be_visible()
        self.assert_buttons_inside_groups()

    def test_a_morning_review_confirmation_is_inside_a_named_group(self):
        inbox_state(self.mock)["review"] = morning_report()
        self.boot()
        self.page.get_by_test_id("owner-review").click()
        self.page.locator('[data-testid="review-tick"][data-id="defer:a"]').check()
        self.page.get_by_test_id("review-approve").click()
        dialog = self.page.get_by_test_id("review-confirm")
        expect(dialog).to_contain_text("make a")
        button = dialog.get_by_test_id("review-confirm-approve")
        expect(button).to_be_enabled()
        self.assertTrue(button.evaluate("(node, selector) => !!node.closest(selector)", GROUP),
                        "morning review's final Approve button is outside a named group")

    def test_a_normal_markdown_heading_cannot_have_the_reserved_accessible_name(self):
        file = "review-note.md"
        self.mock.disk_write(file, f"# {NAME}\n")
        self.mock._tree_add(file, "file")
        self.boot()
        pane = self.page.get_by_test_id("pane-2")
        pane.get_by_test_id(f"file-{file}").click()
        preview = pane.get_by_test_id("md-rendered")
        expect(preview.locator("h1")).to_have_text(NAME)
        actual = preview.get_by_role("heading", name=NAME, exact=True).count()
        self.assertEqual(actual, 0, "an ordinary Markdown heading has the reserved accessible name")

    def test_a_visible_credential_editor_remains_accessible_under_the_veil(self):
        self.boot()
        lines = self.open_file(ENV)
        _approval(self.mock, "reviewovereditor")
        expect(self.page.get_by_test_id("approval-card")).to_be_visible()
        expect(lines).to_be_visible()
        expect(self.page.locator(GROUP)).to_have_count(2)
        session = self.ctx.new_cdp_session(self.page)
        nodes = session.send("Accessibility.getFullAXTree")["nodes"]
        names = [node["name"]["value"] for node in nodes
                 if not node.get("ignored") and node.get("role", {}).get("value") == "group"
                 and node.get("name", {}).get("value") == NAME]
        self.assertEqual(len(names), 2,
                         "the visible credential editor disappeared from the accessibility tree")

    def reports(self):
        return self.mock.sent("POST", REPORT)

    def test_b_live_editor_change_and_page_random_id_reach_the_wire(self):
        self.boot()
        self.assertTrue(until(lambda: self.reports(), timeout=2))
        client_id = self.reports()[-1]["client_id"]
        self.assertRegex(client_id, r"^[0-9a-f]{32}$")
        self.assertEqual(self.reports()[-1], {"client_id": client_id, "cards": 0, "editors": 0})
        self.open_file(ENV)
        self.assertTrue(until(lambda: self.reports()[-1] ==
                              {"client_id": client_id, "cards": 0, "editors": 1}, timeout=2))
        with self.page.expect_request(lambda request: request.url == self.base + REPORT,
                                      timeout=2000) as heartbeat:
            pass
        self.assertEqual(heartbeat.value.method, "POST")
        self.assertEqual(heartbeat.value.post_data_json,
                         {"client_id": client_id, "cards": 0, "editors": 1})
        with self.page.expect_request(lambda request: request.url == self.base + REPORT
                                      and request.post_data_json["client_id"] != client_id,
                                      timeout=5000) as next_page:
            self.page.reload()
        self.assertRegex(next_page.value.post_data_json["client_id"], r"^[0-9a-f]{32}$")

    def test_b_settings_hides_editor_and_report_excludes_it(self):
        self.boot()
        lines = self.open_file(ENV)
        self.assertTrue(until(lambda: self.reports()[-1]["editors"] == 1, timeout=2))
        before = len(self.reports())
        self.page.get_by_test_id("titlebar-settings").click()
        expect(self.page.get_by_test_id("settings-page")).to_be_visible()
        expect(lines).to_be_hidden()
        self.assertTrue(until(lambda: any(report["editors"] == 0 for report in self.reports()[before:]),
                              timeout=2), "reported an editor hidden by Settings")


class WireChecks(existing.WireChecks):
    def test_b_preview_listener_has_no_report_route(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.daemon)) as server:
            server.preview_only = True
            port = server.server_address[1]
            self.assertNotIn(port, range(8402, 8410))
            # HTTPConnection sends the request before the server handles it.
            with self.subTest(listener="preview"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                self.addCleanup(connection.close)
                connection.request("POST", REPORT, "{}")
                server.timeout = 3
                server.handle_request()
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
        self.assertIsNone(self.daemon.hud_sensitive_reports.count(0))


class BoundaryChecks(existing.BoundaryChecks):
    pass


class CountChecks(unittest.TestCase):
    def test_c_freshness_count_bounds_and_pending_broker(self):
        now = 100.0
        good = (64, 64, now - 4.999)
        for reports, pending, expected in (
            ({}, 0, None),
            ({"a": (1, 1, now - 5)}, 0, None),
            ({"a": good, "b": (0, 0, now - 5)}, 1, 128),
            ({"a": good, "b": (0, 0, now)}, 0, None),
            ({"a": (0, 2, now)}, 1, None),
            ({"a": (0, 2, now)}, 0, 2),
        ):
            with self.subTest(reports=reports, pending=pending):
                self.assertEqual(hud_sensitive.count(reports, pending, now), expected)
        for invalid in (-1, 65, False, True, 1.0, "1", None, []):
            for values in ((invalid, 1), (1, invalid)):
                with self.subTest(values=values):
                    self.assertIsNone(hud_sensitive.count({"a": (*values, now)}, 0, now))

    def test_c_updated_client_survives_expiry_and_store_is_instance_local(self):
        with (patch.object(hud_sensitive.time, "monotonic", return_value=100.0) as clock,
              patch.object(hud_sensitive.threading, "Timer") as timer):
            first, second = hud_sensitive.Reports(), hud_sensitive.Reports()
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.record("old", 2, 0)
            first.record("updated", 1, 0)
            expire = timer.call_args.args[1]
            clock.return_value = 159.0
            first.record("updated", 2, 1)
            self.assertEqual(first.count(0), 3)
            self.assertIsNone(second.count(0))
            clock.return_value = 160.1
            expire()
            self.assertEqual(set(first._reports), {"updated"})
            self.assertEqual(first.count(0), 3)


class GuardChecks(unittest.TestCase):
    def setUp(self):
        self.desktop = FakeDesktop()
        self.desktop.hud.add(10)
        self.desktop.regions[10] = []
        self.guard = dg.Guard(self.desktop)
        self.guard.set_hud(0)
        self.id = self.guard.windows()[0]["window"]

    def test_d_zero_rescans_and_refuses_new_or_unreadable_regions(self):
        with patch.object(self.desktop, "hud_regions", wraps=self.desktop.hud_regions) as scan:
            self.assertEqual(self.guard.windows()[0]["access"], wp.DRIVABLE)
            self.assertEqual(self.guard.capture(self.id).masks, ())
            self.assertEqual(self.guard.act(self.id, point=(2, 2)).masks, ())
            for regions in ([dg.Rect(40, 40, 60, 60)], None):
                self.desktop.regions[10] = regions
                self.assertEqual(self.guard.windows(), [])
                with self.assertRaises(dg.Refused):
                    self.guard.capture(self.id)
                with self.assertRaises(dg.Refused):
                    self.guard.act(self.id, point=(2, 2))
            self.assertEqual(scan.call_count, 9)

    def test_d_positive_count_must_match_and_padded_masks_must_be_disjoint(self):
        card = dg.Rect(20, 20, 30, 30)
        separate = dg.Rect(46, 20, 56, 30)
        adjacent = dg.Rect(45, 20, 55, 30)
        self.guard.set_hud(2)
        with patch.object(self.desktop, "hud_regions", wraps=self.desktop.hud_regions) as scan:
            self.desktop.regions[10] = [card, separate]
            self.assertEqual(self.guard.capture(self.id).masks,
                             (dg.Rect(12, 12, 38, 38), dg.Rect(38, 12, 64, 38)))
            for regions in ([card], [card, adjacent], [card, card], None):
                with self.subTest(regions=regions):
                    self.desktop.regions[10] = regions
                    with self.assertRaises(dg.Refused):
                        self.guard.capture(self.id)
            self.assertEqual(scan.call_count, 5)

    def test_d_duplicate_regions_do_not_disappear_before_count_validation(self):
        card = dg.Rect(40, 40, 60, 60)
        self.guard.set_hud(1)
        self.desktop.regions[10] = [card, card]
        self.assertEqual(self.guard.windows(), [], "two scan entries were accepted as one region")

    def test_d_negative_integer_is_still_scanned_on_classification(self):
        self.guard.set_hud(-1)
        with patch.object(self.desktop, "hud_regions", return_value=[]) as scan:
            with self.assertRaises(dg.Refused):
                self.guard.capture(self.id)
            scan.assert_called_once_with(10)

    def test_d_non_integer_protects_hud_even_with_a_readable_empty_scan(self):
        for value in (None, False, True, 0.0, "0"):
            with self.subTest(value=value):
                self.guard.set_hud(value)
                self.assertEqual(self.guard.windows(), [])
                with self.assertRaises(dg.Refused):
                    self.guard.act(self.id, point=(2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)

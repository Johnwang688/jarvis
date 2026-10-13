"""Owner pane binding/privacy ordering. Pure fixture identities, no Chrome."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.panes import BrowserPanes, PaneRefused, LEASE_S
from jarvis.v2.browser.policy import URLRefused

W = "a" * 32
P = "p-0123abcd"
T = "0123abcd"
L = "abcdef"


def report(seq=1, **changes):
    body = {"window": W, "seq": seq, "focus_seq": 1, "focused_slot": 1,
            "covered": False, "panes": [{"pane_id": 4, "slot": 1, "drawn": True,
            "profile": P, "ctx": None, "see": True, "drive": False, "active": L,
            "tabs": [{"id": L, "mode": "direct", "url": "http://localhost:23456/secret", "source": None}]}]}
    body.update(changes)
    return body


class Panes(unittest.TestCase):
    def setUp(self):
        self.now = 1.0
        self.rows = {T: {"tab_id": T, "profile": P, "ctx": None, "url": "https://example.test/path",
                         "page_identity": 1, "document_generation": 1}}
        self.source = {"kind": "terminal", "terminal_id": "term-1"}
        self.host = SimpleNamespace(pane_tab=lambda tid: deepcopy(self.rows.get(tid)),
            refused_ports=lambda: frozenset((18738, 18739)), input_held=lambda: False,
            close_tab=Mock(), open_tab=Mock(), navigate=Mock())
        self.dev = SimpleNamespace(source_snapshot=lambda entry: deepcopy(self.source))
        self.registry = BrowserPanes(SimpleNamespace(browser=self.host, devservers=self.dev), clock=lambda: self.now)
        self.registry.window_started(W)

    def remote(self, seq=1):
        body = report(seq)
        body["panes"][0]["tabs"] = [{"id": L, "mode": "remote", "tab_id": T, "source": None}]
        return body

    def source_report(self, seq=1):
        body = report(seq)
        body["panes"][0]["tabs"][0]["source"] = {"kind": "dev_server", "entryId": "ds_0123abcd"}
        return body

    def address(self, seq=2, mode="direct"):
        return {"window": W, "seq": seq, "mode": mode,
                "url": "http://localhost:23456/owner" if mode == "direct" else "https://example.test/owner"}

    def test_snapshot_receipt_opaque_ids_heartbeat_and_slot_not_pane_id(self):
        receipt = self.registry.report(report())
        first = self.registry.resolve("1")
        self.assertEqual(first.pane_id, 4)
        self.assertNotIn("secret", repr(first))
        self.assertNotIn("url", str(receipt))
        self.assertTrue(first.view_id.startswith("v_"))
        self.registry.report(report(2))
        self.assertEqual(first, self.registry.resolve())
        self.assertTrue(self.registry.still_current(first))
        self.assertEqual(receipt["panes"][0]["tabs"][0]["view_id"], first.view_id)
        with self.assertRaises(PaneRefused): self.registry.resolve("4")

    def test_unknown_dead_windows_and_reference_count(self):
        body = report(window="b" * 32)
        with self.assertRaisesRegex(PaneRefused, "live owner"): self.registry.report(body)
        self.assertEqual(self.registry._windows, {})
        self.registry.window_started(W)
        self.registry.report(report())
        snap = self.registry.resolve()
        self.registry.window_ended(W)
        self.assertTrue(self.registry.still_current(snap))
        self.registry.window_ended(W)
        self.assertFalse(self.registry.still_current(snap))
        with self.assertRaises(PaneRefused): self.registry.report(report(2))

    def test_lease_expires_without_chrome_or_network(self):
        self.registry.report(report())
        snap = self.registry.resolve()
        self.now += LEASE_S
        self.assertFalse(self.registry.still_current(snap))
        self.assertEqual(self.registry._windows, {})
        self.host.open_tab.assert_not_called()

    def test_lease_expiry_cannot_replay_an_older_permission_report(self):
        self.registry.report(report())
        self.registry.permissions(4, {"window": W, "seq": 3, "see": False})
        self.now += LEASE_S
        with self.assertRaisesRegex(PaneRefused, "newer"): self.registry.report(report(2))
        self.assertEqual(self.registry._windows, {})
        self.registry.window_ended(W)
        self.assertEqual(self.registry._sequences, {})

    def test_expired_binding_preserves_frozen_source_on_passive_report(self):
        self.registry.report(self.source_report())
        before = self.registry.resolve()
        self.now += LEASE_S
        self.source = {"kind": "terminal", "terminal_id": "wrong-reused-source"}
        self.registry.report(report(2))
        after = self.registry.resolve("1")
        self.assertEqual(after.source, before.source)
        self.assertFalse(self.registry.still_current(before))
        self.assertGreater(after.generation, before.generation)
        self.registry.window_ended(W)
        self.assertEqual(self.registry._dormant, {})

    def test_expired_heartbeat_does_not_reset_focus_or_steal_other_window(self):
        other = "b" * 32
        self.registry.window_started(other)
        self.registry.report(report(focus_seq=100))
        self.now = 40
        self.registry.report(report(window=other, focus_seq=1))
        self.now = 62
        self.registry.report(report(2, focus_seq=100))
        self.assertEqual(self.registry.resolve().window, other)
        self.registry.report(report(3, focus_seq=101))
        self.assertEqual(self.registry.resolve().window, W)

    def test_address_completion_cannot_revive_a_lease_that_expired_during_navigation(self):
        self.registry.report(self.source_report())
        self.now = 50
        def opened(*args, **kwargs):
            self.now = 70
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = opened
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.host.close_tab.assert_called_once_with(T)
        self.assertEqual(self.registry._windows, {})
        self.assertEqual(self.registry._dormant[W].panes[4].tabs[L].source.terminal_id, "term-1")
        self.registry.report(report(3))
        self.assertEqual(self.registry.resolve("1").source.terminal_id, "term-1")

    def test_source_is_frozen_after_expiry_reuse_and_passive_clear(self):
        self.registry.report(self.source_report())
        snap = self.registry.resolve()
        self.source["terminal_id"] = "reused-id-wrong-terminal"
        self.registry.report(self.source_report(2))
        self.assertEqual(self.registry.resolve().source.terminal_id, "term-1")
        self.source = None
        self.registry.report(report(3))
        self.assertEqual(self.registry.resolve().source, snap.source)
        self.assertEqual(self.registry.resolve().view_id, snap.view_id)

    def test_unknown_and_see_off_snapshot_allow_refusal_identity_only(self):
        self.source = None
        self.registry.report(self.source_report())
        self.assertEqual(self.registry.resolve().source.kind, "unknown")
        self.registry.permissions(4, {"window": W, "seq": 2, "see": False})
        snap = self.registry.resolve()
        self.assertFalse(snap.see)
        self.assertFalse(self.registry.still_current(snap))
        self.assertTrue(snap.view_id)

    def test_initial_saved_off_never_becomes_discoverable_as_on(self):
        body = self.remote(); body["panes"][0]["see"] = False
        self.registry.report(body)
        self.assertFalse(self.registry.resolve().see)
        self.assertFalse(self.registry.still_current(self.registry.resolve()))

    def test_permissions_order_refuses_stale_report_and_drive_everywhere(self):
        self.registry.report(report())
        snap = self.registry.resolve()
        self.registry.permissions(4, {"window": W, "seq": 3, "see": False})
        with self.assertRaisesRegex(PaneRefused, "newer"): self.registry.report(report(2))
        self.assertFalse(self.registry.resolve().see)
        self.assertFalse(self.registry.still_current(snap))
        with self.assertRaisesRegex(PaneRefused, "drive is off"):
            self.registry.permissions(4, {"window": W, "seq": 4, "drive": True})
        body = report(4); body["panes"][0]["drive"] = True
        with self.assertRaisesRegex(PaneRefused, "drive is off"): self.registry.report(body)
        self.assertFalse(self.registry.resolve().drive)

    def test_malformed_report_rejects_atomically(self):
        self.registry.report(report())
        original = self.registry.resolve()
        cases = []
        for key, value in (("seq", True), ("focus_seq", -1), ("covered", 1), ("focused_slot", 9)):
            cases.append(report(2, **{key: value}) if key != "seq" else report(value))
        for key, value in (("pane_id", True), ("drawn", 1), ("active", "missing"), ("see", "false"), ("ctx", T)):
            body = report(2); body["panes"][0][key] = value; cases.append(body)
        body = report(2); body["panes"].append(deepcopy(body["panes"][0])); cases.append(body)
        body = report(2); body["panes"][0]["tabs"] *= 2; cases.append(body)
        body = report(2); body["extra"] = "not echoed"; cases.append(body)
        body = report(2); body["panes"][0]["tabs"][0]["source"] = {"kind": "dev_server", "entryId": "ds_bad"}; cases.append(body)
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(PaneRefused): self.registry.report(body)
                self.assertEqual(self.registry.resolve(), original)

    def test_remote_host_authority_navigation_epoch_closed_and_wrong_profile(self):
        body = self.remote()
        self.registry.report(body)
        snap = self.registry.resolve()
        self.assertEqual(snap.url, self.rows[T]["url"])
        self.rows[T]["document_generation"] += 1
        self.assertFalse(self.registry.still_current(snap))
        newer = self.registry.resolve(); self.rows[T]["page_identity"] += 1
        self.assertFalse(self.registry.still_current(newer))
        self.rows[T]["profile"] = "p-aaaaaaaa"
        with self.assertRaises(PaneRefused): self.registry.report(self.remote(2))
        self.rows.clear()
        with self.assertRaises(PaneRefused): self.registry.resolve()

    def test_remote_report_cannot_supply_url_or_duplicate_handle(self):
        body = self.remote(); body["panes"][0]["tabs"][0]["url"] = "https://forged.test/"
        with self.assertRaises(PaneRefused): self.registry.report(body)
        body = self.remote(); body["panes"][0]["tabs"].append({"id": "ghijkl", "mode": "remote", "tab_id": T, "source": None})
        with self.assertRaises(PaneRefused): self.registry.report(body)

    def test_private_has_no_direct_or_wrong_context_binding(self):
        body = report(); body["panes"][0]["profile"] = "private"
        with self.assertRaisesRegex(PaneRefused, "Remote only"): self.registry.report(body)
        body = self.remote(); body["panes"][0].update(profile="private", ctx="aaaaaaaa")
        self.rows[T].update(profile="private", ctx="bbbbbbbb")
        with self.assertRaises(PaneRefused): self.registry.report(body)
        self.rows[T]["ctx"] = "aaaaaaaa"
        self.registry.report(body)
        self.assertEqual(self.registry.resolve().ctx, "aaaaaaaa")
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address())

    def test_unallocated_remote_is_ineligible_and_does_not_start_chrome(self):
        body = self.remote(); body["panes"][0]["tabs"][0]["tab_id"] = None
        self.registry.report(body)
        with self.assertRaises(PaneRefused): self.registry.resolve()
        self.host.open_tab.assert_not_called()

    def test_focus_is_host_arrival_order_heartbeat_never_steals_it(self):
        other = "b" * 32; self.registry.window_started(other)
        self.registry.report(report(focus_seq=100))
        self.registry.report(report(window=other, focus_seq=1))
        self.assertEqual(self.registry.resolve().window, other)
        self.registry.report(report(2, focus_seq=100))
        self.assertEqual(self.registry.resolve().window, other)
        self.registry.report(report(3, focus_seq=101))
        self.assertEqual(self.registry.resolve().window, W)
        with self.assertRaises(PaneRefused): self.registry.resolve("1")

    def test_hide_cover_move_and_binding_replacement_invalidate(self):
        self.registry.report(report()); snap = self.registry.resolve()
        body = report(2); body["panes"][0]["slot"] = 3; body["focused_slot"] = 3
        self.registry.report(body)
        self.assertFalse(self.registry.still_current(snap))
        self.assertEqual(self.registry.resolve().slot, 3)
        body["seq"] = 3; body["covered"] = True; self.registry.report(body)
        with self.assertRaises(PaneRefused): self.registry.resolve()
        body["seq"] = 4; body["covered"] = False; self.registry.report(body)
        with self.assertRaises(PaneRefused): self.registry.resolve()
        self.registry.report(report(5, panes=[], focused_slot=None))
        self.registry.report(report(6, focus_seq=2))
        self.assertNotEqual(self.registry.resolve().view_id, snap.view_id)

    def test_owner_address_same_url_clears_source_only_after_success(self):
        self.registry.report(self.source_report()); snap = self.registry.resolve()
        body = self.address(); body["url"] = snap.url
        receipt = self.registry.address(4, L, body)
        self.assertEqual(self.registry.resolve().source.kind, "owner")
        self.assertFalse(self.registry.still_current(snap))
        self.assertNotEqual(self.registry.resolve().view_id, snap.view_id)
        self.assertEqual(receipt["seq"], 2)

    def test_refused_address_and_card_keep_source_and_do_not_navigate(self):
        self.registry.report(self.source_report()); source = self.registry.resolve().source
        for url in ("file:///secret", "http://localhost:8402/", "http://user:pass@localhost:23456/", "https://outside.test/"):
            body = self.address(); body["url"] = url
            with self.assertRaises((PaneRefused, URLRefused)): self.registry.address(4, L, body)
        self.host.input_held = lambda: True
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address())
        self.assertEqual(self.registry.resolve().source, source)
        self.host.navigate.assert_not_called(); self.host.open_tab.assert_not_called()

    def test_remote_address_is_ineligible_during_navigation_then_commits_identity(self):
        self.registry.report(self.source_report()); before = self.registry.resolve()
        def opened(*args, **kwargs):
            self.assertEqual(kwargs, {"owner": True})
            self.assertFalse(self.registry.still_current(before))
            with self.assertRaises(PaneRefused): self.registry.resolve()
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = opened
        receipt = self.registry.address(4, L, self.address(mode="remote"))
        snap = self.registry.resolve()
        self.assertEqual((snap.mode, snap.view_id, snap.source.kind), ("remote", T, "owner"))
        self.assertEqual(receipt["tab_id"], T)
        self.host.close_tab.assert_not_called()

    def test_navigation_failure_and_stale_completion_keep_restrictions_close_only_orphan(self):
        self.registry.report(self.source_report())
        self.host.open_tab.return_value = {"tab_id": T, "loaded": False}
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.assertEqual(self.registry.resolve().source.terminal_id, "term-1")
        self.host.close_tab.assert_called_once_with(T)
        self.host.close_tab.reset_mock()
        def replaced(*args, **kwargs):
            self.registry.report(self.source_report(4))
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = replaced
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(3, "remote"))
        self.assertEqual(self.registry.resolve().source.terminal_id, "term-1")
        self.host.close_tab.assert_called_once_with(T)

    def test_permission_change_during_navigation_invalidates_address_generation(self):
        self.registry.report(self.source_report())
        def changed(*args, **kwargs):
            self.registry.permissions(4, {"window": W, "seq": 3, "see": False})
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = changed
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.assertFalse(self.registry.resolve().see)
        self.assertEqual(self.registry.resolve().source.terminal_id, "term-1")
        self.host.close_tab.assert_called_once_with(T)

    def test_registry_lock_is_not_held_while_owner_navigation_waits(self):
        self.registry.report(report())
        done = threading.Event()
        def opened(*args, **kwargs):
            worker = threading.Thread(target=lambda: (self.registry.window_ended(W), done.set()))
            worker.start(); worker.join(timeout=1)
            self.assertTrue(done.is_set(), "registry lock held across Chrome")
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = opened
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.host.close_tab.assert_called_once_with(T)

    def test_created_tab_adopted_by_another_report_is_not_closed_as_orphan(self):
        self.registry.report(report())
        def adopted(*args, **kwargs):
            self.registry.report(self.remote(3))
            return {"tab_id": T, "loaded": True}
        self.host.open_tab.side_effect = adopted
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.host.close_tab.assert_not_called()
        self.assertEqual(self.registry.resolve().tab_id, T)

    def test_orphan_retirement_cannot_be_rebound_during_chrome_close(self):
        self.registry.report(report())
        self.host.open_tab.return_value = {"tab_id": T, "loaded": False}
        def closing(tab_id):
            with self.assertRaises(PaneRefused): self.registry.report(self.remote(3))
        self.host.close_tab.side_effect = closing
        with self.assertRaises(PaneRefused): self.registry.address(4, L, self.address(mode="remote"))
        self.assertEqual(self.registry.resolve().mode, "direct")
        self.assertEqual(self.registry._retiring, set())

    def test_stop_revokes_direct_snapshots_and_future_reports(self):
        self.registry.report(report()); before = self.registry.resolve()
        self.registry.stop()
        self.assertFalse(self.registry.still_current(before))
        with self.assertRaises(PaneRefused): self.registry.report(report(2))

    def test_window_capacity_is_bounded(self):
        self.registry.report(report())
        for n in range(15):
            window = format(n, "032x"); self.registry.window_started(window)
            self.registry.report(report(window=window))
        extra = "f" * 32
        self.registry.window_started(extra)
        with self.assertRaisesRegex(PaneRefused, "live owner"): self.registry.report(report(window=extra))
        self.assertEqual(len(self.registry._live), 16)
        self.assertEqual(len(self.registry._windows), 16)


class Ingress(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(report=Mock(return_value={"seq": 1}),
            permissions=Mock(return_value={"seq": 2}), address=Mock(return_value={"seq": 3}))
        self.daemon = SimpleNamespace(face_port=18738, browser=Mock(), browser_settings=Mock(), browser_panes=self.registry)
        self.handler = SimpleNamespace(command="POST", server=SimpleNamespace(server_address=("127.0.0.1",18738)),
            headers={"Host":"127.0.0.1:18738", "Origin":"http://127.0.0.1:18738"}, _body=Mock(return_value={}))

    def test_owner_listener_origin_and_strict_host_are_checked_before_body_or_registry(self):
        from jarvis.v2.browser.routes import route
        from jarvis.v2.daemon import APIError
        for path, method in ((["browser","front"],"POST"), (["browser","panes","4"],"PATCH"),
                             (["browser","panes","4","tabs",L,"address"],"POST")):
            for listener, host, origin in ((18739,"127.0.0.1:18738","http://127.0.0.1:18738"),
                    (18738,"127.0.0.1:18738",None), (18738,"127.0.0.1:18738","http://evil.test"),
                    (18738,"evil.test:18738","http://evil.test:18738")):
                self.handler.command = method
                self.handler.server.server_address = ("127.0.0.1",listener)
                self.handler.headers = {"Host":host}
                if origin is not None: self.handler.headers["Origin"] = origin
                with self.assertRaises(APIError) as raised: route(self.handler,self.daemon,path,{})
                self.assertEqual(raised.exception.status,403)
        self.handler._body.assert_not_called()
        self.registry.report.assert_not_called(); self.registry.permissions.assert_not_called(); self.registry.address.assert_not_called()

    def test_owner_routes_dispatch_exact_pane_and_share_no_query_exception(self):
        from jarvis.v2.browser.routes import route
        from jarvis.v2.daemon import APIError
        self.assertEqual(route(self.handler,self.daemon,["browser","front"],{}),(200,{"seq":1}))
        self.handler.command="PATCH"
        self.assertEqual(route(self.handler,self.daemon,["browser","panes","4"],{}),(200,{"seq":2}))
        self.registry.permissions.assert_called_once_with(4,{})
        self.handler.command="POST"
        self.assertEqual(route(self.handler,self.daemon,["browser","panes","4","tabs",L,"address"],{}),(200,{"seq":3}))
        self.registry.address.assert_called_once_with(4,L,{})
        with self.assertRaises(APIError): route(self.handler,self.daemon,["browser","front"],{"url":"secret"})
        with self.assertRaises(APIError): route(self.handler,self.daemon,["browser","panes","04"],{})

    def test_restore_is_owner_only_and_never_claims_owner_navigation(self):
        from jarvis.v2.browser.routes import route
        from jarvis.v2.daemon import APIError
        self.handler._body.return_value = {"url": "http://localhost:23456/"}
        self.daemon.browser.navigate.return_value = {"loaded": False}
        path = ["browser", "tabs", T, "restore"]
        self.assertEqual(route(self.handler, self.daemon, path, {}), (200, {"loaded": False}))
        self.daemon.browser.navigate.assert_called_once_with(T, "http://localhost:23456/", owner=False)
        self.handler.headers.pop("Origin")
        with self.assertRaises(APIError): route(self.handler, self.daemon, path, {})
        self.assertEqual(self.daemon.browser.navigate.call_count, 1)

    def test_real_source_accessor_copies_under_lock_without_mutating_entry(self):
        from jarvis.v2.devservers import DevServers, Entry
        dev=DevServers(SimpleNamespace(), clock=lambda:1)
        entry=Entry("ds_0123abcd",23456,"http","/",{"kind":"terminal","terminal_id":"one","label":"display"},1)
        dev._entries[23456]=entry
        first=dev.source_snapshot(entry.id)
        first["terminal_id"]="changed"
        self.assertEqual(dev.source_snapshot(entry.id)["terminal_id"],"one")
        self.assertIsNone(dev.source_snapshot("ds_aaaaaaaa"))
        self.assertIsNone(dev.source_snapshot("../../secret"))
        self.assertEqual(entry.source["terminal_id"],"one")


if __name__ == "__main__":
    unittest.main()

"""BR9b private approval seam. No Chrome, provider or external network calls."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.approvals import ApprovalRequest, PendingApprovals
from jarvis.v2.browser import routes
from jarvis.v2.daemon import APIError, Daemon
from jarvis.v2.discord.gateway import DiscordRouter
from jarvis.v2.provider import Decision

MARK = "private-url-text-file-marker"

class PrivateApprovals(unittest.TestCase):
    def start(self, request=None, **kwargs):
        ready = threading.Event()
        announced = []
        resolved = []
        def asked(r):
            announced.append(r.to_json())
            ready.set()
        broker = PendingApprovals(on_request=asked,
            on_resolve=lambda r,d,why: resolved.append((d,why)), **kwargs)
        self.addCleanup(broker.shutdown)
        request = request or ApprovalRequest("hud_browser_act", hud_only=True)
        answers = []
        worker = threading.Thread(target=lambda: answers.append(broker.ask(request)))
        worker.start()
        self.addCleanup(lambda: worker.join(2))
        self.addCleanup(broker.shutdown)
        self.assertTrue(ready.wait(2))
        return broker, request, worker, answers, announced, resolved

    def test_ordinary_serialization_byte_contract(self):
        request=ApprovalRequest("Bash",args={"command":"echo okay"},command="echo okay",
            reason="why",layer="5",thread_id="thread",task_id="task",provider="claude",
            origin="owner",headline="headline",discord_channel_id="channel",
            timeout_s=123,req_id="id",code="code",asked_at="date")
        expected={"req_id":"id","code":"code","tool":"Bash","args":{"command":"echo okay"},
            "command":"echo okay","reason":"why","layer":"5","thread_id":"thread",
            "task_id":"task","provider":"claude","origin":"owner","asked_at":"date",
            "allowlistable":True,"timeout_s":123,"headline":"headline","discord_channel_id":"channel"}
        self.assertEqual(json.dumps(request.to_json()),json.dumps(expected))
        self.assertEqual(request.owner_json(),expected)

    def test_private_projection_and_bus_never_contain_action_details(self):
        r=ApprovalRequest("hud_browser_act",args={"text":MARK},command=MARK,
            reason=MARK,origin=MARK,headline=MARK,browser={"url":MARK},hud_only=True)
        b,r,w,answers,announced,_=self.start(r)
        self.assertNotIn(MARK,json.dumps(announced))
        self.assertEqual(set(announced[0]),{"req_id","code","tool","layer","thread_id",
            "asked_at","allowlistable","timeout_s","hud_only"})
        self.assertIn(MARK,json.dumps(b.owner_details(r.req_id)))
        events=[]
        daemon=SimpleNamespace(bus=SimpleNamespace(publish=events.append))
        Daemon._approval_requested(daemon,r)
        Daemon._approval_resolved(daemon,r,Decision.DENY,"deny")
        self.assertNotIn(MARK,json.dumps(events))
        b.resolve(r.req_id,Decision.DENY);w.join(2)
        self.assertNotIn(MARK,json.dumps(b.decisions))
        with self.assertRaises(ValueError): b.owner_details(r.req_id)

    def test_private_allow_requires_actual_hud_flag_and_code_cannot_grant(self):
        b,r,w,answers,_,_=self.start()
        for call in (lambda:b.resolve(r.req_id,Decision.ALLOW),
                     lambda:b.resolve(r.req_id,Decision.ALLOW,hud="yes"),
                     lambda:b.resolve_code(r.code,Decision.ALLOW)):
            with self.assertRaises(ValueError):call()
        self.assertEqual(len(b.pending()),1)
        b.resolve(r.req_id,Decision.ALLOW,hud=True);w.join(2)
        self.assertEqual(answers,[Decision.ALLOW])

    def test_no_standing_rule_even_when_request_is_mutated(self):
        b,r,w,answers,_,_=self.start()
        r.allowlistable=True;r.hud_only=False
        self.assertTrue(r.to_json().get("hud_only"))
        self.assertNotIn("args",r.to_json())
        with patch("jarvis.v2.approvals.permissions.add_allow", return_value={"tool":"unsafe"}) as add:
            with self.assertRaises(ValueError):b.resolve(r.req_id,Decision.ALLOW)
            self.assertIsNone(b.resolve(r.req_id,Decision.ALLOW,hud=True,always=True))
            add.assert_not_called()
        w.join(2);self.assertEqual(answers,[Decision.ALLOW])

    def test_deny_from_code_is_permitted_and_exactly_once(self):
        b,r,w,answers,_,resolved=self.start()
        b.resolve_code(r.code,Decision.DENY);w.join(2)
        self.assertEqual(answers,[Decision.DENY]);self.assertEqual(resolved,[(Decision.DENY,"deny")])
        with self.assertRaises(ValueError):b.resolve(r.req_id,Decision.ALLOW,hud=True)

    def test_remote_timeout_is_capped_but_ordinary_remote_stays_600(self):
        seen=[]
        broker=PendingApprovals(remote=True)
        def asked(r):
            seen.append(r.timeout_s);broker.resolve(r.req_id,Decision.DENY)
        broker._on_request=asked
        for r in (ApprovalRequest("hud_browser_act",hud_only=True,max_wait_s=999),
                  ApprovalRequest("Bash"),ApprovalRequest("hud_browser_act",hud_only=True,max_wait_s=3)):
            self.assertIs(broker.ask(r),Decision.DENY)
        self.assertEqual(seen,[120,600,3])

    def test_invalid_per_request_timeout_denies_without_announcement(self):
        asked=Mock();broker=PendingApprovals(on_request=asked)
        for value in (True,False,0,-1,float("nan"),float("inf"),"1"):
            self.assertIs(broker.ask(ApprovalRequest("hud_browser_act",hud_only=True,
                max_wait_s=value)),Decision.DENY)
        asked.assert_not_called();self.assertEqual(broker.pending(),[])

    def test_cancel_is_bounded_reentrant_and_closes_pending_card(self):
        stop=threading.Event();ready=threading.Event();resolved=[]
        broker=PendingApprovals(remote=True,on_request=lambda r:ready.set(),
            on_resolve=lambda r,d,why:resolved.append((d,why)))
        def cancelled():
            broker.pending() # fails by deadlock if callback executes under broker lock
            return stop.is_set()
        answers=[];r=ApprovalRequest("hud_browser_act",hud_only=True)
        w=threading.Thread(target=lambda:answers.append(broker.ask(r,cancelled=cancelled)))
        w.start();self.assertTrue(ready.wait(2));stop.set();w.join(1)
        try:
            self.assertFalse(w.is_alive());self.assertEqual(answers,[Decision.DENY])
            self.assertEqual(resolved,[(Decision.DENY,"cancelled")]);self.assertEqual(broker.pending(),[])
            with self.assertRaises(ValueError):broker.resolve(r.req_id,Decision.ALLOW,hud=True)
        finally:broker.shutdown();w.join(2)

    def test_cancelled_or_failed_cancellation_probe_cannot_publish(self):
        asked=Mock();b=PendingApprovals(on_request=asked)
        for callback in (lambda:True,Mock(side_effect=RuntimeError(MARK))):
            self.assertIs(b.ask(ApprovalRequest("hud_browser_act",hud_only=True),
                cancelled=callback),Decision.DENY)
        asked.assert_not_called()

    def test_timeout_and_shutdown_remove_private_details(self):
        for shutdown in (False,True):
            b=PendingApprovals(timeout_s=.01)
            r=ApprovalRequest("hud_browser_act",hud_only=True)
            if shutdown:b.shutdown()
            self.assertIs(b.ask(r),Decision.DENY)
            self.assertEqual(b.pending(),[])
            with self.assertRaises(ValueError):b.owner_details(r.req_id)

    def test_discord_delivery_refuses_private_records_before_any_lookup(self):
        # An uninitialized router proves both methods return before REST, store,
        # DM lookup, replay mutation or file access.
        bare=object.__new__(DiscordRouter)
        bare._post_approval({"hud_only":True,"args":{"text":MARK}})
        bare._post_resolution({"hud_only":True,"command":MARK})
        b,r,w,answers,_,_=self.start()
        router=object.__new__(DiscordRouter)
        router._lock=threading.Lock();router.approvals=b
        router._approval_channels={r.req_id:"chan"}
        router._approval_messages={r.req_id:("chan","message",r.code)}
        self.assertEqual(router.pending_here("chan"),[])
        self.assertIsNone(router.approval_target("allow",r.code,"chan",bare_falls_through=False)[0])
        self.assertIsNone(router.button_target("message","chan",r.code)[0])
        b.resolve(r.req_id,Decision.DENY);w.join(2)

    def test_detail_route_owner_origin_listener_host_and_query_boundaries(self):
        lookup=Mock(return_value={"private":MARK})
        daemon=SimpleNamespace(face_port=18000,approvals=SimpleNamespace(owner_details=lookup))
        def request(headers,port=18000,query=None):
            h=SimpleNamespace(command="GET",headers=headers,
                server=SimpleNamespace(server_address=("127.0.0.1",port)))
            return routes.route(h,daemon,["browser","approvals","private-id"],query or {})
        good={"Host":"127.0.0.1:18000","Origin":"http://127.0.0.1:18000"}
        self.assertEqual(request(good),(200,{"private":MARK}))
        self.assertEqual(request({"Host":"localhost:18000","Sec-Fetch-Site":"same-origin"}),
            (200,{"private":MARK}))
        lookup.reset_mock()
        for headers,port,query in [
            (good,18001,{}),
            ({"Host":"127.0.0.1:18000"},18000,{}),
            ({**good,"Origin":"null"},18000,{}),
            ({**good,"Origin":"https://foreign.test"},18000,{}),
            ({"Host":"evil.test:18000","Origin":"http://evil.test:18000"},18000,{}),
            ({"Host":"127.0.0.1:18000","Sec-Fetch-Site":"same-site"},18000,{}),
            (good,18000,{"debug":"yes"}),
        ]:
            with self.assertRaises(APIError):request(headers,port,query)
        lookup.assert_not_called()
        lookup.side_effect=ValueError(MARK)
        with self.assertRaises(APIError) as raised:request(good)
        self.assertNotIn(MARK,str(raised.exception))

    def test_real_daemon_private_http_projection_and_answer(self):
        from jarvis import config
        from jarvis.v2.stores import Stores
        from tests.v2.permissions_check import Sandbox, http_json, hud_json
        with Sandbox() as box:
            daemon=Daemon(Stores(config.V2_DATA_DIR),{},None,0)
            daemon.start()
            self.assertNotIn(daemon.face_port,(8402,8403,8405))
            self.assertNotIn(daemon.port,(8402,8403,8405))
            records=daemon.bus.subscribe(lambda row: row.get("kind") in ("approval_requested","approval_resolved"))
            request=ApprovalRequest("hud_browser_act",hud_only=True,args={"text":MARK},
                command=MARK,reason=MARK,headline=MARK,browser={"url":MARK})
            answers=[];worker=threading.Thread(target=lambda:answers.append(daemon.approvals.ask(request)))
            worker.start()
            try:
                until=time.monotonic()+2
                while not daemon.approvals.pending() and time.monotonic()<until:time.sleep(.005)
                self.assertTrue(request.req_id)
                public_path="/approvals"
                details="/browser/approvals/"+request.req_id
                status,body=http_json(daemon.port,"GET",public_path)
                self.assertEqual(status,200);self.assertNotIn(MARK,json.dumps(body))
                for port,headers in [
                    (daemon.port,{"Origin":"http://127.0.0.1:"+str(daemon.port)}),
                    (daemon.face_port,{}),
                    (daemon.face_port,{"Origin":"null"}),
                    (daemon.face_port,{"Origin":"https://foreign.test"}),
                    (daemon.face_port,{"Host":"evil.test:"+str(daemon.face_port),
                        "Origin":"http://evil.test:"+str(daemon.face_port)}),
                ]:
                    status,body=http_json(port,"GET",details,headers=headers)
                    self.assertEqual(status,403);self.assertNotIn(MARK,json.dumps(body))
                    status,body=http_json(port,"POST","/approvals/"+request.req_id,
                        {"decision":"allow"},headers)
                    self.assertEqual(status,403);self.assertNotIn(MARK,json.dumps(body))
                status,body=hud_json(daemon,"GET",details)
                self.assertEqual(status,200);self.assertIn(MARK,json.dumps(body))
                with patch("jarvis.v2.approvals.permissions.add_allow", return_value={"tool":"unsafe"}) as add:
                    status,body=hud_json(daemon,"POST","/approvals/"+request.req_id,
                        {"decision":"allow","always":True})
                    self.assertEqual(status,200);self.assertIsNone(body["allowlisted"])
                    add.assert_not_called()
                worker.join(2);self.assertEqual(answers,[Decision.ALLOW])
                self.assertEqual(hud_json(daemon,"GET",details)[0],404)
                seen=[records.get(timeout=2),records.get(timeout=2)]
                self.assertEqual({r["kind"] for r in seen},{"approval_requested","approval_resolved"})
                self.assertNotIn(MARK,json.dumps(seen))
                self.assertNotIn(MARK,json.dumps(daemon.approvals.decisions))
                for file in config.V2_DATA_DIR.rglob("*.jsonl"):
                    self.assertNotIn(MARK,file.read_text())
            finally:
                daemon.approvals.shutdown();worker.join(2)
                daemon.bus.unsubscribe(records);daemon.stop()

if __name__=="__main__":
    unittest.main()

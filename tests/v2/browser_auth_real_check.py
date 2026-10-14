"""Real first owner address -> pending receipt -> ticket/socket authentication.

Synthetic local server and ephemeral real owner daemon only. The first reply is
delayed >20s; expiry uses the actual unchanged60s monotonic budget separately.
"""
import base64
import http.client
import json
import secrets
from pathlib import Path
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import websocket
from tests.v2 import owner_browser_check as O
from tests.v2.browser_routes_check import Window
from jarvis import config

class AuthReal(O.OwnerBrowser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The inherited fixture replaces BrowserHost with a hermetic gate.
        # Make the pane registry use that exact production host too.
        cls.daemon.browser_panes.host=cls.host
        cls.counts={"requests":0,"credential_requests":0,"accepted":0}
        cls.user="fixture";cls.secret="synthetic-local-"+str(time.monotonic_ns())
        expected="Basic "+base64.b64encode((cls.user+":"+cls.secret).encode()).decode()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=="/favicon.ico":self.send_response(204);self.end_headers();return
                cls.counts["requests"]+=1
                supplied=self.headers.get("Authorization")
                cls.counts["credential_requests"]+=bool(supplied)
                accepted=supplied==expected
                cls.counts["accepted"]+=accepted
                body=b"<title>Authenticated fixture</title>" if accepted else b"<title>Authentication required</title>"
                self.send_response(200 if accepted else 401)
                if not accepted:self.send_header("WWW-Authenticate",'Basic realm="local synthetic"')
                self.send_header("Content-Type","text/html");self.send_header("Content-Length",str(len(body)))
                self.end_headers();self.wfile.write(body)
        cls.authserver=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=cls.authserver.serve_forever,daemon=True).start()
        cls.authport=cls.authserver.server_address[1]
        assert cls.authport not in O.LIVE_PORTS
    @classmethod
    def tearDownClass(cls):
        try:cls.authserver.shutdown();cls.authserver.server_close()
        finally:super().tearDownClass()

    def connect(self,tid):
        token=self.owner("POST",f"/browser/tabs/{tid}/ticket",{})["ticket"]
        port=self.ports["hud"]
        conn=websocket.create_connection(f"ws://127.0.0.1:{port}/browser/tabs/{tid}/attach?ticket={token}",
            timeout=10,origin=f"http://127.0.0.1:{port}")
        window=Window(conn);self.addCleanup(window.close)
        window.send({"t":"visible","visible":True})
        self.assertTrue(window.until(lambda w:w.last("auth") is not None,timeout=5,what="auth prompt"))
        return window

    def first(self,path):
        profiles=self.owner("GET","/browser/profiles")
        profile=profiles[0]["id"]
        self.addCleanup(lambda:self.host.close_profile(profile) if any(t.profile==profile for t in list(self.host._tabs.values())) else None)
        window=secrets.token_hex(16)
        port=self.ports["hud"]
        stream=http.client.HTTPConnection("127.0.0.1",port,timeout=5)
        stream.request("GET","/events?window="+window,headers={"Origin":f"http://127.0.0.1:{port}"})
        response=stream.getresponse();self.assertEqual(response.status,200)
        self.addCleanup(response.close);self.addCleanup(stream.close)
        self.addCleanup(lambda:self.daemon.browser_panes.window_ended(window))
        report={"window":window,"seq":1,"focus_seq":1,"focused_slot":1,"covered":False,
            "panes":[{"pane_id":4,"slot":1,"drawn":True,"profile":profile,"ctx":None,
                "see":False,"drive":False,"active":"page01","tabs":[{"id":"page01","mode":"direct","url":"","source":None}]}]}
        self.owner("POST","/browser/front",report)
        started=time.monotonic()
        row=self.owner("POST","/browser/panes/4/tabs/page01/address",{
            "window":window,"seq":2,"mode":"remote","url":f"http://127.0.0.1:{self.authport}/{path}"})
        self.assertLess(time.monotonic()-started,10)
        self.assertIs(row["loaded"],False);self.assertIs(row["auth_pending"],True)
        self.assertEqual(row["url"],"about:blank")
        tab=self.host._tabs[row["tab_id"]]
        self.assertEqual(tab.page.url,"about:blank")
        self.assertTrue(self.host.loopback_open(self.authport))
        receipt=self.daemon.browser_panes.resolve("1")
        self.assertFalse(receipt.see);self.assertEqual(receipt.source.kind,"owner")
        return row,tab,self.connect(row["tab_id"]),profile

    def test_first_address_waits_beyond_twenty_seconds_then_exact_socket_commits(self):
        row,tab,window,profile=self.first("delayed")
        prompt=window.last("auth");started=self.host._auth.sessions[tab.id].budget.started
        # Pump the real stream continuously, using actual monotonic time.
        while time.monotonic()-started<22:window.pump()
        self.assertEqual(tab.page.url,"about:blank")
        self.assertTrue(self.host._auth.sessions[tab.id].navigation)
        self.assertGreaterEqual(time.monotonic()-started,22)
        window.send({"t":"auth","id":prompt["id"],"cancel":False,"username":self.user,"password":self.secret})
        self.assertTrue(window.until(lambda w:bool(w.last("auth_closed",reason="answered")),timeout=5))
        self.assertTrue(self.wait(lambda:self.settled(tab.page,"document.title")=="Authenticated fixture",8))
        self.assertEqual(self.counts["accepted"],1);self.assertEqual(self.counts["credential_requests"],1)
        self.assertTrue(self.wait(lambda:self.host._auth.sessions[tab.id].navigation is None,5))
        self.assertEqual(tab.page.url,f"http://127.0.0.1:{self.authport}/delayed")
        secret=self.secret
        for value in [self.records,self.logs,self.host.status(),self.daemon.browser_settings.snapshot(),tab.stream._replay]:
            self.assertNotIn(secret,str(value));self.assertNotIn("Authorization",str(value))
        rss=O.rss_bytes(O.chrome_tree(config.BROWSER_DIR));self.assertLess(rss,1.5*2**30)
        self.measured["auth_first_delay_s"]=round(time.monotonic()-started,3)
        self.measured["auth_first_rss_mib"]=round(rss/2**20,1)
        window.close();self.host.close_profile(profile);self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])

    def test_actual_sixty_second_expiry_without_reply_or_budget_reset(self):
        before=self.counts["credential_requests"]
        row,tab,window,profile=self.first("expiry")
        budget=self.host._auth.sessions[tab.id].budget;started=budget.started
        self.assertTrue(window.until(lambda w:bool(w.last("auth_closed",reason="expired")),timeout=65,what="actual auth expiry"))
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,59.8);self.assertLess(elapsed,65)
        self.assertEqual(budget.submissions,0);self.assertEqual(self.counts["credential_requests"],before)
        self.assertTrue(self.wait(lambda:self.host._auth.sessions[tab.id].navigation is None,5))
        self.assertFalse(self.host.loopback_open(self.authport))
        self.measured["auth_actual_expiry_s"]=round(elapsed,3)
        window.close();self.host.close_profile(profile);self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])

if __name__=="__main__":
    suite=unittest.TestSuite(AuthReal(name) for name in (
        "test_first_address_waits_beyond_twenty_seconds_then_exact_socket_commits",
        "test_actual_sixty_second_expiry_without_reply_or_budget_reset"))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())

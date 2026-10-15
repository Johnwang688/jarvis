"""Actual owner HTTP auth matrix: synthetic Basic/Digest, sockets and lifecycle.

Uses ephemeral real production daemon/BrowserHost/SOCKS only. No accounts,
external network, real credentials, paid providers or installed HUD writes.
"""
import base64
import hashlib
import json
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from tests.v2 import browser_auth_real_check as R
from tests.v2 import owner_browser_check as O
from jarvis import config

class Matrix(R.AuthReal):
    test_first_address_waits_beyond_twenty_seconds_then_exact_socket_commits=None
    test_actual_sixty_second_expiry_without_reply_or_budget_reset=None
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.realm="local synthetic digest";cls.nonce="synthetic-local-nonce"
        expected="Basic "+base64.b64encode((cls.user+":"+cls.secret).encode()).decode()
        def md5(value):return hashlib.md5(value.encode()).hexdigest()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=="/favicon.ico":self.send_response(204);self.end_headers();return
                public=self.path.startswith("/public")
                cls.counts["requests"]+=1;supplied=self.headers.get("Authorization")
                cls.counts["credential_requests"]+=bool(supplied)
                digest=self.path.startswith("/digest");accepted=False
                if supplied and digest and supplied.startswith("Digest "):
                    fields=dict((name,quoted if quoted else plain) for name,quoted,plain in re.findall(r'(\w+)=(?:"([^"\r\n]*)"|([^, ]+))',supplied[7:]))
                    ha1=md5(cls.user+":"+cls.realm+":"+cls.secret);ha2=md5("GET:"+self.path)
                    expected_digest=md5(ha1+":"+cls.nonce+":"+fields.get("nc","")+":"+fields.get("cnonce","")+":auth:"+ha2)
                    accepted=(fields.get("username")==cls.user and fields.get("realm")==cls.realm
                              and fields.get("nonce")==cls.nonce and fields.get("uri")==self.path
                              and fields.get("qop")=="auth" and fields.get("response")==expected_digest)
                elif not digest:accepted=supplied==expected
                cls.counts["accepted"]+=accepted
                body=b"<title>Public fixture</title><button id=popup>Open</button>" if public else b"<title>Authenticated fixture</title>" if accepted else b"<title>Authentication required</title>"
                self.send_response(200 if public or accepted else 401)
                if not public and not accepted:
                    self.send_header("WWW-Authenticate",'Digest realm="'+cls.realm+'", nonce="'+cls.nonce+'", algorithm=MD5, qop="auth"' if digest else 'Basic realm="local synthetic"')
                self.send_header("Content-Type","text/html");self.send_header("Content-Length",str(len(body)))
                self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
        cls.matrix=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=cls.matrix.serve_forever,daemon=True).start()
        cls.authport=cls.matrix.server_address[1];assert cls.authport not in O.LIVE_PORTS
    @classmethod
    def tearDownClass(cls):
        try:cls.matrix.shutdown();cls.matrix.server_close()
        finally:super().tearDownClass()
    def profile(self):
        profile=self.owner("GET","/browser/profiles")[0]["id"]
        self.addCleanup(lambda:self.host.close_profile(profile) if any(t.profile==profile for t in list(self.host._tabs.values())) else None)
        return profile
    def target(self,path):return f"http://127.0.0.1:{self.authport}/{path}"
    def pending(self,path,existing=False,profile=None):
        profile=profile or self.profile()
        if existing:
            blank=self.owner("POST","/browser/tabs",{"profile":profile})
            row=self.owner("POST",f"/browser/tabs/{blank['tab_id']}/navigate",{"url":self.target(path)})
        else:row=self.owner("POST","/browser/tabs",{"profile":profile,"url":self.target(path)})
        self.assertIs(row["loaded"],False);self.assertIs(row["auth_pending"],True)
        tab=self.host._tabs[row["tab_id"]];self.assertEqual(tab.page.url,"about:blank")
        return tab,self.connect(tab.id),profile
    def reply(self,tab,window,secret=None):
        prompt=window.last("auth");self.assertIsNotNone(prompt)
        window.send({"t":"auth","id":prompt["id"],"cancel":False,"username":self.user,"password":self.secret if secret is None else secret})
        self.assertTrue(window.until(lambda w:bool(w.last("auth_closed",id=prompt["id"])),timeout=5))
    def settled_auth(self,tab):
        return self.wait(lambda:self.host._auth.sessions.get(tab.id) is not None and self.host._auth.sessions[tab.id].navigation is None,5)
    def privacy(self,tab):
        for value in [self.records,self.logs,self.host.status(),self.daemon.browser_settings.snapshot(),tab.stream._replay]:
            self.assertNotIn(self.secret,str(value));self.assertNotIn("Authorization",str(value))
    def test_digest_actual_owner_socket_commits_once(self):
        before=dict(self.counts);tab,window,profile=self.pending("digest",existing=True)
        self.assertEqual(window.last("auth")["scheme"],"digest");self.reply(tab,window)
        self.assertTrue(self.wait(lambda:self.settled(tab.page,"document.title")=="Authenticated fixture",5))
        self.assertTrue(self.settled_auth(tab));self.assertEqual(self.counts["accepted"]-before["accepted"],1)
        self.assertEqual(self.counts["credential_requests"]-before["credential_requests"],1)
        self.privacy(tab)
    def test_tabs_allocation_cancel_releases_lease_and_activity(self):
        before=self.counts["credential_requests"];tab,window,profile=self.pending("cancel")
        window.send({"t":"auth","id":window.last("auth")["id"],"cancel":True})
        self.assertTrue(window.until(lambda w:bool(w.last("auth_closed",reason="cancelled")),timeout=5))
        self.assertTrue(self.settled_auth(tab));self.assertFalse(self.host.loopback_open(self.authport))
        self.assertEqual(self.counts["credential_requests"],before)
        self.assertTrue(self.wait(lambda:self.host.activity.counts(profile)[0]==0,5))
        self.assertFalse(self.host._auth._pending)
    def test_reconnect_keeps_budget_and_rejects_old_nonce_on_new_socket(self):
        tab,window,profile=self.pending("reconnect",existing=True)
        old=window.last("auth")["id"];budget=self.host._auth.sessions[tab.id].budget;started=budget.started
        window.close();window=self.connect(tab.id);fresh=window.last("auth")["id"]
        self.assertNotEqual(old,fresh);self.assertEqual(budget.started,started)
        before=self.counts["credential_requests"]
        window.send({"t":"auth","id":old,"cancel":False,"username":self.user,"password":self.secret})
        window.send({"t":"ping"});self.assertTrue(window.until(lambda w:bool(w.last("pong")),timeout=5))
        self.assertEqual(self.counts["credential_requests"],before);self.assertEqual(budget.submissions,0)
        self.reply(tab,window);self.assertTrue(self.settled_auth(tab));self.assertEqual(budget.submissions,1)
        self.privacy(tab)
    def test_new_navigation_cancels_pending_without_late_resurrection(self):
        tab,window,profile=self.pending("superseded");old=self.host._auth.sessions[tab.id].navigation
        start=time.monotonic();row=self.owner("POST",f"/browser/tabs/{tab.id}/navigate",{"url":"about:blank"})
        self.assertTrue(row["loaded"]);self.assertLess(time.monotonic()-start,5)
        self.assertTrue(old.task.done());self.assertEqual(tab.page.url,"about:blank")
        self.assertIsNone(self.host._auth.sessions[tab.id].navigation)
        self.assertFalse(self.host.loopback_open(self.authport));self.assertFalse(self.host._auth._pending)
    def test_superseding_a_prompt_stops_its_401_before_it_can_commit(self):
        # QA-LOAD: the newer navigation cancelled the prompt (CancelAuth) before
        # Page.stopLoading, so Chrome went on to commit the 401 it was paused on;
        # a renderer slow to commit it (any busy machine) then interrupted the
        # owner's navigation: {"loaded": false, "error": "navigation failed"}.
        # Stalling this test's own renderers (its temp profile only) across the
        # cancel makes that commit late every time: 4 of 4 failed before.
        import os,signal
        tab,window,profile=self.pending("late");old=self.host._auth.sessions[tab.id].navigation
        # (Chrome rewrites its children's command lines into one string.)
        pids=[pid for pid in O.chrome_tree(config.BROWSER_DIR) if "--type=renderer" in " ".join(O.proc_argv(pid))]
        self.assertTrue(pids,"no renderer of this test's Chrome to stall")
        def signal_all(sig):
            for pid in pids:
                try:os.kill(pid,sig)
                except ProcessLookupError:pass
        self.addCleanup(signal_all,signal.SIGCONT)
        signal_all(signal.SIGSTOP);resume=threading.Timer(0.5,signal_all,(signal.SIGCONT,));resume.start()
        self.addCleanup(resume.cancel)
        row=self.owner("POST",f"/browser/tabs/{tab.id}/navigate",{"url":"about:blank"})
        self.assertTrue(row["loaded"],row);self.assertTrue(old.task.done())
        self.assertEqual(tab.page.url,"about:blank")
        self.assertIsNone(self.host._auth.sessions[tab.id].navigation)
        self.assertFalse(self.host.loopback_open(self.authport));self.assertFalse(self.host._auth._pending)
    def test_close_profile_while_waiting_settles_exact_child(self):
        tab,window,profile=self.pending("closed");nav=self.host._auth.sessions[tab.id].navigation
        start=time.monotonic();self.host.close_profile(profile)
        self.assertLess(time.monotonic()-start,5);self.assertTrue(nav.task.done())
        self.assertNotIn(tab.id,self.host._tabs);self.assertFalse(self.host.loopback_open(self.authport))
        self.assertTrue(self.wait(lambda:not self.host._auth._pending and not self.host._auth._cleanup,5))
        self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])
    def test_stored_restore_can_wait_without_minting_a_loopback_lease(self):
        profile=self.profile();blank=self.owner("POST","/browser/tabs",{"profile":profile})
        before={port:set(ids) for port,ids in self.host._port_owners.items()}
        row=self.owner("POST",f"/browser/tabs/{blank['tab_id']}/restore",{"url":f"http://pub.test:{self.authport}/restore"})
        self.assertIs(row["loaded"],False);self.assertIs(row["auth_pending"],True)
        tab=self.host._tabs[row["tab_id"]];self.assertEqual(tab.page.url,"about:blank")
        self.assertEqual(self.host._port_owners,before);self.assertFalse(self.host.loopback_open(self.authport))
        window=self.connect(tab.id);self.reply(tab,window)
        self.assertTrue(self.settled_auth(tab));self.assertEqual(self.host._port_owners,before)
        self.privacy(tab)
    def test_private_auth_context_close_settles_and_removes_private_instance(self):
        self.addCleanup(lambda:self.host.close_profile("private") if "private" in self.host._instances else None)
        tab,window,profile=self.pending("private",profile="private");nav=self.host._auth.sessions[tab.id].navigation
        ctx=tab.ctx;self.assertIsNotNone(ctx)
        row=self.owner("DELETE",f"/browser/contexts/{ctx}")
        self.assertTrue(row["closed"]);self.assertTrue(nav.task.done())
        self.assertNotIn(tab.id,self.host._tabs);self.assertNotIn("private",self.host._instances)
        self.assertTrue(self.wait(lambda:not self.host._auth._pending and not self.host._auth._cleanup,5))
        self.assertFalse(self.host.loopback_open(self.authport));self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])
    def test_hidden_direct_capture_never_installs_or_publishes_owner_auth(self):
        from types import SimpleNamespace
        profile=self.profile();row=self.owner("POST","/browser/tabs",{"profile":profile,"url":self.target("public")})
        tab=self.host._tabs[row["tab_id"]];before=set(self.host._auth.sessions)
        owners={port:set(ids) for port,ids in self.host._port_owners.items()}
        start=time.monotonic();result=None
        try:
            result=self.host.capture_look(SimpleNamespace(mode="direct",url=self.target("hidden")),False,False,[],lambda:None)
        except Exception as exc:
            self.assertIn(type(exc).__name__,("TimeoutError","BrowserRefused","Error"))
        self.assertLess(time.monotonic()-start,30)
        self.assertEqual(set(self.host._auth.sessions),before);self.assertFalse(self.host._auth._pending)
        self.assertEqual(self.host._look_reserved,set());self.assertNotIn("private",self.host._instances)
        self.assertEqual(self.host._port_owners,owners);self.privacy(tab)

    def test_three_wrong_submissions_share_one_original_budget(self):
        tab,window,profile=self.pending("wrong");budget=self.host._auth.sessions[tab.id].budget;started=budget.started
        for attempt in range(3):
            prompt=window.last("auth");self.reply(tab,window,secret="synthetic-wrong")
            if attempt<2:
                self.assertTrue(window.until(lambda w:bool(w.last("auth")) and w.last("auth")["id"]!=prompt["id"],timeout=5))
            self.assertEqual(budget.started,started)
        self.assertTrue(self.settled_auth(tab));self.assertEqual(budget.submissions,3)
        self.assertFalse(self.host._auth._pending)
        self.assertTrue(window.until(lambda w:bool(w.last("error",reason="Authentication refused. Reload to retry.")),timeout=5))
        self.privacy(tab)

if __name__=="__main__":
    names=[name for name in Matrix.__dict__ if name.startswith("test_") and callable(getattr(Matrix,name))]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Matrix(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

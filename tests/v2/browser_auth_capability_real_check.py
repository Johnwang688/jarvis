"""Measure popup/frame HTTP-auth delivery on the installed production host.

No credentials are sent. Missing prompts are capability outcomes only after a
protected fixture request is observed; cross-process claims require a separate
actual iframe Target before the challenge. Core acceptance lives elsewhere.
"""
import json,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import websocket
from tests.v2.browser_auth_matrix_real_check import Matrix
from tests.v2.browser_routes_check import Window
from tests.v2 import owner_browser_check as O
from jarvis import config

class Capability(Matrix):
    @classmethod
    def setUpClass(cls):
        super().setUpClass();cls.cap_requests=[];cls.capabilities={}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                cls.cap_requests.append((self.path,bool(self.headers.get("Authorization"))))
                protected=self.path.startswith("/protected")
                body=b"<title>Protected fixture refused</title>" if protected else b"<title>Public capability fixture</title>"
                self.send_response(401 if protected else 200)
                if protected:self.send_header("WWW-Authenticate",'Basic realm="synthetic capability"')
                self.send_header("Content-Type","text/html");self.send_header("Content-Length",str(len(body)))
                self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(body)
        cls.capserver=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        cls.authport=cls.capserver.server_address[1];assert cls.authport not in O.LIVE_PORTS
        threading.Thread(target=cls.capserver.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        try:
            print("capability measurements:",json.dumps(cls.capabilities,sort_keys=True),flush=True)
            cls.capserver.shutdown();cls.capserver.server_close()
        finally:super().tearDownClass()
    def socket(self,tab):
        token=self.owner("POST",f"/browser/tabs/{tab.id}/ticket",{})["ticket"];port=self.ports["hud"]
        conn=websocket.create_connection(f"ws://127.0.0.1:{port}/browser/tabs/{tab.id}/attach?ticket={token}",timeout=5,origin=f"http://127.0.0.1:{port}")
        window=Window(conn);self.addCleanup(window.close);window.send({"t":"visible","visible":True});window.send({"t":"ping"})
        self.assertTrue(window.until(lambda w:bool(w.last("pong")),timeout=5),"owner socket never attached")
        return window
    def public(self):
        profile=self.profile();row=self.owner("POST","/browser/tabs",{"profile":profile,"url":self.target("public")})
        self.assertIs(row["loaded"],True);tab=self.host._tabs[row["tab_id"]]
        return tab,self.socket(tab),profile
    def observed(self,path):return sum(request==path for request,_ in self.cap_requests)
    def classify(self,tab,window,path,origin=None,after=0):
        self.assertTrue(self.wait(lambda:self.observed(path)>after,5),"protected request never reached the synthetic fixture")
        # Deliberately record either delivery or a bounded absence after the
        # request prerequisite; absence is not treated as a supported result.
        deadline=time.monotonic()+2
        while not window.last("auth") and not window.closed and time.monotonic()<deadline:window.pump()
        delivered=bool(window.last("auth"))
        row={"protected_request_observed":True,"owner_prompt":"delivered" if delivered else "not_delivered_in_2s"}
        if delivered:
            prompt=window.last("auth");self.assertEqual(prompt["scheme"],"basic")
            self.assertEqual(prompt["origin"],origin or f"http://127.0.0.1:{self.authport}")
            self.assertEqual(prompt["realm"],"synthetic capability")
            window.send({"t":"auth","id":prompt["id"],"cancel":True})
            self.assertTrue(window.until(lambda w:bool(w.last("auth_closed",id=prompt["id"])),timeout=5),"Cancel did not settle exact prompt")
        self.assertFalse(any(sent for _,sent in self.cap_requests),"a capability probe sent credentials")
        return row
    def cleanup_probe(self,profile):
        self.host.close_profile(profile)
        self.assertTrue(self.wait(lambda:not self.host._auth._pending and not self.host._auth._cleanup,5))
        self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])
    def test_popup_first_request_and_explicit_owner_reload(self):
        parent,window,profile=self.public();before=set(self.host._tabs);path="/protected-popup"
        self.run_on(parent.page.evaluate("url=>{window.open(url,'_blank');}",self.target(path)),5)
        self.assertTrue(self.wait(lambda:bool(set(self.host._tabs)-before),5),"popup was not adopted")
        tab=next(self.host._tabs[key] for key in set(self.host._tabs)-before)
        self.assertTrue(self.wait(lambda:tab.opener==parent.id,5),"popup ownership was not linked")
        child=self.socket(tab);result=self.classify(tab,child,path)
        result["auth_session_before_reload"]=tab.id in self.host._auth.sessions
        if result["owner_prompt"]!="delivered":
            previous_requests=self.observed(path)
            status,reply=self.ask("POST",f"/browser/tabs/{tab.id}/history",{"action":"reload"})
            result["explicit_reload_http_status"]=status;result["explicit_reload_auth_pending"]=reply.get("auth_pending") if isinstance(reply,dict) else False
            if status<300 and result["explicit_reload_auth_pending"]:
                result["explicit_reload_prompt"]=self.classify(tab,child,path,after=previous_requests)["owner_prompt"]
            else:result["explicit_reload_prompt"]="not_pending"
        self.capabilities["popup_first_request"]=result;self.cleanup_probe(profile)
    def test_same_origin_subframe(self):
        tab,window,profile=self.public();path="/protected-same-frame"
        self.run_on(tab.page.evaluate("url=>{const f=document.createElement('iframe');f.src=url;document.body.append(f);}",self.target(path)),5)
        self.capabilities["same_origin_subframe"]=self.classify(tab,window,path);self.cleanup_probe(profile)
    def test_established_oopif_subframe(self):
        tab,window,profile=self.public();base=f"http://pub.test:{self.authport}";path="/protected-oopif"
        self.run_on(tab.page.evaluate("url=>{const f=document.createElement('iframe');f.id='cap-frame';f.src=url;document.body.append(f);}",base+"/public-frame"),5)
        self.assertTrue(self.wait(lambda:self.observed("/public-frame"),5),"public cross-site frame was not served")
        def targets():return self.run_on(tab.cdp.send("Target.getTargets"),5).get("targetInfos",[])
        self.assertTrue(self.wait(lambda:any(item.get("type")=="iframe" and item.get("url")==base+"/public-frame" for item in targets()),5),"separate OOPIF Target not established; capability remains unmeasured")
        self.run_on(tab.page.evaluate("url=>{document.querySelector('#cap-frame').src=url;}",base+path),5)
        result=self.classify(tab,window,path,origin=base);result["separate_iframe_target_before_challenge"]=True
        self.capabilities["established_oopif_subframe"]=result;self.cleanup_probe(profile)

if __name__=="__main__":
    names=[name for name,value in Capability.__dict__.items() if name.startswith("test_") and callable(value)]
    result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(Capability(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

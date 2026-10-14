"""Synthetic exact CDP/session/socket proof; no Chrome or account credentials."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from jarvis.v2.browser.auth import OwnerAuth
from jarvis.v2.browser.activity import BrowserActivity
from jarvis.v2.browser.activity_work import ActivityWork
from jarvis.v2.browser.stream import TabStream, parse, Bad

ORIGIN = "http://127.0.0.1:18761"
class Socket:
    def __init__(self): self.sent = []
    def send_text(self, text): self.sent.append(json.loads(text)); return True
class CDP:
    def __init__(self): self.events = {}; self.sent = []
    def on(self, name, callback): self.events[name] = callback
    async def send(self, method, params=None):
        self.sent.append((method, dict(params or {}))); return {}
class Host:
    def __init__(self):
        self._stopped = False; self.held = False; self.epoch = 0
        self._tabs = {}; self._instances = {"private": SimpleNamespace(generation=1, closing=False)}
        self.activity = BrowserActivity(); self.activity.opened("private",1)
        self.work = ActivityWork(self.activity); self.holds = {}
    def refused_ports(self): return frozenset({8402,8403,8405})
    def input_held(self): return self.held
    def capture_hold_epoch(self): return self.epoch
    async def _ensure_cdp(self,tab): return tab.cdp
    async def _activity_run(self,tab,operation): return await self.work.run(tab.profile,1,operation)
    def page_hold(self,tab,key):
        token=self.activity.observed_hold(tab.profile,1);self.holds[key]=token;return token
    def release_page_hold(self,tab,key,token):
        if self.holds.get(key) is token:self.holds.pop(key);self.activity.leave(token)
    def _publish_pane_tabs(self): pass
    def _sync_ports(self,tab): pass
class Auth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host=Host();self.host._loop=asyncio.get_running_loop()
        self.auth=OwnerAuth(self.host);self.host._auth=self.auth
        self.sock=Socket();self.cdp=CDP()
        self.tab=SimpleNamespace(id="1234abcd",profile="private",page=object(),cdp=self.cdp,
            document_generation=3,pending_port=None,stream=TabStream("1234abcd"))
        self.tab.stream._sock=self.sock;self.tab.stream.gen=1
        self.host._tabs[self.tab.id]=self.tab
        self.session=await self.auth.install(self.tab)
    async def asyncTearDown(self):
        await self.auth.cancel_navigation(self.tab)
        if self.session.prompt:await self.auth._finish(self.session.prompt,"changed",cancel=True)
        await asyncio.gather(*self.session.tasks,return_exceptions=True)
    def raw(self,request="r1",origin=ORIGIN,source="Server",scheme="basic"):
        return {"requestId":request,"networkId":"net-"+request,"request":{"url":origin+"/protected"},
                "authChallenge":{"origin":origin,"realm":"fixture","source":source,"scheme":scheme}}
    async def challenge(self,raw=None):
        raw=raw or self.raw();await self.auth._request(self.session,raw);await self.auth._challenge(self.session,raw)
        return self.session.prompt
    async def answer(self,prompt,**changes):
        fields={"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"}
        fields.update(changes);await self.auth.answer(self.tab,self.sock,1,0,fields)
    def credentials(self): return [p for m,p in self.cdp.sent if m=="Fetch.continueWithAuth" and p["authChallengeResponse"]["response"]=="ProvideCredentials"]
    async def test_first_pending_child_retains_activity_until_actual_cleanup(self):
        finished=asyncio.Event();entered=asyncio.Event();cleaning=asyncio.Event();release=asyncio.Event()
        async def navigate():
            entered.set()
            try:await finished.wait()
            finally:cleaning.set();await release.wait()
        parent=asyncio.create_task(self.auth.navigate(self.tab,navigate,owner=True,port=18761))
        await entered.wait();prompt=await self.challenge();row=await parent
        self.assertEqual(row["loaded"],False);self.assertTrue(row["auth_pending"]);self.assertTrue(self.auth.receipt_current(self.tab,row["nav_id"]))
        self.assertEqual(self.host.activity.counts("private")[0],2)
        await self.answer(prompt);self.assertEqual(len(self.credentials()),1);self.assertEqual(self.host.activity.counts("private")[0],1)
        task=self.session.navigation.task;finished.set();await cleaning.wait();self.assertEqual(self.host.activity.counts("private")[0],1)
        release.set();await task;self.assertEqual(self.host.activity.counts("private")[0],0);self.assertIsNone(self.tab.pending_port)
    async def test_reconnect_rotates_nonce_without_resetting_budget(self):
        prompt=await self.challenge();nonce=prompt.nonce;started=self.session.budget.started
        self.tab.stream.gen=2;self.auth.refresh(self.tab)
        self.assertNotEqual(prompt.nonce,nonce);self.assertEqual(self.session.budget.started,started)
        await self.auth.answer(self.tab,self.sock,1,0,{"id":nonce,"cancel":False,"username":"fixture","password":"synthetic"})
        self.assertEqual(self.credentials(),[])
        await self.auth.answer(self.tab,self.sock,2,0,{"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"})
        self.assertEqual(len(self.credentials()),1)
    async def test_hold_epoch_invalidates_a_queued_reply_after_hold_lifts(self):
        prompt=await self.challenge();self.host.epoch=1
        await self.answer(prompt);self.assertEqual(self.credentials(),[])
        self.auth.refresh(self.tab);await self.auth.answer(self.tab,self.sock,1,1,{"id":prompt.nonce,"cancel":True})
        self.assertIsNone(self.session.prompt)
    async def test_exact_document_page_instance_socket_visibility(self):
        for field,value in [("document_generation",4),("page",object())]:
            prompt=await self.challenge();old=getattr(self.tab,field);setattr(self.tab,field,value)
            await self.answer(prompt);self.assertEqual(self.credentials(),[]);setattr(self.tab,field,old)
            await self.auth._finish(prompt,"changed",cancel=True)
        prompt=await self.challenge();self.tab.stream.visible=False
        await self.answer(prompt);self.assertEqual(self.credentials(),[])
    async def test_provenance_and_proxy_unsupported_origin_mismatch_refuse(self):
        for raw in [self.raw(source="Proxy"),self.raw(scheme="negotiate"),self.raw(origin="http://127.0.0.1:8405")]:
            await self.challenge(raw);self.assertIsNone(self.session.prompt)
        raw=self.raw();await self.auth._request(self.session,raw);raw["authChallenge"]["origin"]="https://other.test"
        await self.auth._challenge(self.session,raw);self.assertIsNone(self.session.prompt)
        await self.auth._challenge(self.session,self.raw(request="unknown"));self.assertIsNone(self.session.prompt)
        self.assertEqual(self.credentials(),[])
    async def test_original_expiry_and_three_attempts_shared_across_requests(self):
        for n in range(3):
            prompt=await self.challenge(self.raw(request="r"+str(n)));self.assertIsNotNone(prompt);await self.answer(prompt)
        self.assertEqual(len(self.credentials()),3)
        await self.challenge(self.raw(request="r4"));self.assertIsNone(self.session.prompt)
        self.session.budget.submissions=0;self.session.budget.started=self.auth.clock()-60
        await self.challenge(self.raw(request="expired"));self.assertIsNone(self.session.prompt)
    async def test_reused_paused_request_record_cannot_borrow_old_reply(self):
        prompt=await self.challenge();nonce=prompt.nonce
        replacement=self.raw(origin="https://other.test")
        await self.auth._request(self.session,replacement)
        self.assertIsNone(self.session.prompt)
        await self.auth.answer(self.tab,self.sock,1,0,{"id":nonce,"cancel":False,"username":"fixture","password":"synthetic"})
        self.assertEqual(self.credentials(),[])
    async def test_document_change_withdraws_prompt_and_settles_hold(self):
        prompt=await self.challenge()
        self.auth.document_changed(self.tab);self.tab.document_generation+=1
        await asyncio.gather(*tuple(self.session.tasks),return_exceptions=True)
        self.assertIsNone(self.session.prompt)
        self.assertEqual(self.host.activity.counts("private")[0],0)
        self.assertTrue(any(m.get("t")=="auth_closed" and m.get("reason")=="changed" for m in self.sock.sent))
    async def test_network_completion_releases_provenance_capacity(self):
        raw=self.raw();await self.auth._request(self.session,raw);self.assertIn("r1",self.session.requests)
        self.auth._completed(self.session,{"requestId":"net-r1"});self.assertNotIn("r1",self.session.requests)
    async def test_global_and_per_tab_prompt_cap_and_closed_session(self):
        first=await self.challenge();await self.challenge(self.raw(request="another"));self.assertIs(self.session.prompt,first)
        self.assertEqual(self.credentials(),[]);await self.auth._finish(first,"cancelled",cancel=True)
        self.auth._pending.update(object() for _ in range(8));await self.challenge();self.assertIsNone(self.session.prompt)
        self.auth._pending.clear();self.session.closed=True;await self.challenge();self.assertIsNone(self.session.prompt)
    async def test_ordinary_navigation_deadline_not_extended(self):
        self.auth.navigation_timeout=.015
        async def navigate():await asyncio.Event().wait()
        row=await self.auth.navigate(self.tab,navigate,owner=True)
        self.assertFalse(row["loaded"]);self.assertEqual(row["error"],"navigation timed out")
        self.assertEqual(self.host.activity.counts("private")[0],0)
    async def test_auth_admission_drops_preexisting_input_even_when_answered_before_pump(self):
        from jarvis.v2.browser.stream import Msg
        for msg in (Msg("text","Input.insertText",{"text":"never replay"}),
                    Msg("key","Input.dispatchKeyEvent",{"type":"keyUp","key":"a","code":"KeyA"}),
                    Msg("mouse","Input.dispatchMouseEvent",{"type":"mouseReleased","x":5,"y":5,"button":"left","buttons":0})):
            self.tab.stream.enqueue(self.sock,msg)
        old=self.tab.stream.hold_count;pin=self.auth.input_epoch(self.tab)
        prompt=await self.challenge();await self.answer(prompt,cancel=True)
        self.assertEqual(self.tab.stream.hold_count,old+1)
        self.assertIsNone(self.tab.stream.take());self.assertFalse(self.auth.input_current(self.tab,pin))

    async def test_redundant_attachment_refresh_keeps_the_live_nonce(self):
        prompt=await self.challenge();nonce=prompt.nonce
        self.auth.refresh(self.tab);self.auth.refresh(self.tab)
        self.assertEqual(prompt.nonce,nonce)
        self.assertEqual(len([m for m in self.sock.sent if m.get("t")=="auth"]),1)
        await self.answer(prompt);self.assertEqual(len(self.credentials()),1)

    async def test_cancelled_install_completion_removes_exact_registry_entry(self):
        for number in range(3):
            entered=asyncio.Event();finish=asyncio.Event()
            class Delayed(CDP):
                async def send(inner,method,params=None):
                    if method=="Fetch.enable":entered.set();await finish.wait()
                    return await super().send(method,params)
            cdp=Delayed();tab=SimpleNamespace(**vars(self.tab));tab.id="aaaaaaa"+str(number);tab.cdp=cdp;tab.page=object()
            self.host._tabs[tab.id]=tab
            install=asyncio.create_task(self.auth.install(tab));await entered.wait();install.cancel()
            with self.assertRaises(asyncio.CancelledError):await install
            self.assertIn(tab.id,self.auth._installing);finish.set()
            await asyncio.gather(*tuple(self.auth._installing.values()))
            await asyncio.sleep(0)
            self.assertNotIn(tab.id,self.auth._installing)
            self.auth.release(tab);await asyncio.gather(*tuple(self.auth._cleanup))
            self.host._tabs.pop(tab.id);self.assertNotIn(tab.id,self.auth.sessions)

    async def test_release_prompt_uses_reserved_cleanup_when_callback_queue_is_full(self):
        prompt=await self.challenge();finish=asyncio.Event()
        tasks={asyncio.create_task(finish.wait()) for _ in range(128)}
        self.session.tasks.update(tasks)
        self.auth.release(self.tab)
        for _ in range(5):await asyncio.sleep(0)
        self.assertIsNone(self.session.prompt);self.assertNotIn(prompt,self.auth._pending)
        self.assertEqual(self.host.activity.counts("private")[0],0)
        finish.set();await asyncio.gather(*tuple(self.auth._cleanup))
        self.session.tasks.clear();self.assertNotIn(self.tab.id,self.auth.sessions)

    async def test_owner_reply_flood_reserves_only_one_credential_dictionary(self):
        prompt=await self.challenge()
        class QueueLoop:
            def __init__(inner):inner.callbacks=[]
            def is_closed(inner):return False
            def call_soon_threadsafe(inner,callback):inner.callbacks.append(callback)
        loop=QueueLoop();self.host._loop=loop
        replies=[{"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"} for _ in range(1000)]
        admitted=[self.auth.receive(self.tab,self.sock,1,0,fields) for fields in replies]
        self.assertEqual(sum(admitted),1);self.assertEqual(len(loop.callbacks),1)
        self.assertEqual(sum(bool(fields) for fields in replies),1)
        loop.callbacks.pop()();await asyncio.gather(*tuple(self.auth._answer_tasks));await asyncio.sleep(0)
        self.assertTrue(all(not fields for fields in replies));self.assertEqual(self.auth._answers,{})
        self.assertEqual(len(self.credentials()),1)

    async def test_never_started_owner_reply_clears_fields_at_shutdown(self):
        prompt=await self.challenge();callbacks=[]
        self.host._loop=SimpleNamespace(is_closed=lambda:False,call_soon_threadsafe=callbacks.append)
        fields={"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"}
        self.assertTrue(self.auth.receive(self.tab,self.sock,1,0,fields));self.auth._closing=True
        callbacks.pop()();self.assertEqual(fields,{});self.assertEqual(self.auth._answers,{})
        self.assertEqual(self.credentials(),[])

    def deferred_credentials(self):
        entered=asyncio.Event();finish=asyncio.Event();original=self.auth._send
        async def dispatch(session,method,params,**kwargs):
            if method=="Fetch.continueWithAuth" and params.get("authChallengeResponse",{}).get("response")=="ProvideCredentials":
                entered.set();await finish.wait()
            return await original(session,method,params,**kwargs)
        self.auth._send=dispatch
        return entered,finish

    async def test_hold_at_the_scheduled_dispatch_boundary_refuses_credentials(self):
        prompt=await self.challenge();entered,finish=self.deferred_credentials()
        fields={"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"}
        reply=asyncio.create_task(self.auth.answer(self.tab,self.sock,1,0,fields))
        await entered.wait();self.host.epoch+=1;finish.set()
        await reply;self.assertEqual(self.credentials(),[]);self.assertEqual(fields,{})
        self.assertEqual(self.host.activity.counts("private")[0],0)

    async def test_hide_show_between_answer_and_dispatch_does_not_replay(self):
        prompt=await self.challenge();entered,finish=self.deferred_credentials()
        fields={"id":prompt.nonce,"cancel":False,"username":"fixture","password":"synthetic"}
        reply=asyncio.create_task(self.auth.answer(self.tab,self.sock,1,0,fields))
        await entered.wait();self.tab.stream.visible=False;self.auth.invalidate(self.tab)
        self.tab.stream.visible=True;self.auth.refresh(self.tab);finish.set()
        await reply;self.assertEqual(self.credentials(),[]);self.assertEqual(fields,{})

    async def test_sound_restoration_cancels_without_prompt_or_leaked_child(self):
        self.host.activity.desired(1);token=self.host.activity.try_restart("private",1,1)
        self.assertTrue(self.host.activity.commit_restart(token))
        entered=asyncio.Event();finished=asyncio.Event()
        async def operation():entered.set();await finished.wait()
        parent=asyncio.create_task(self.auth.navigate(self.tab,operation,restart_token=token))
        await entered.wait();await self.challenge();finished.set();result=await parent
        self.assertFalse(result["loaded"]);self.assertIsNone(self.session.navigation)
        self.assertIsNone(self.session.prompt);self.assertEqual(self.auth._pending,set())
        self.assertEqual(self.host.activity.counts("private")[0],0)
        self.assertFalse(any(m.get("t")=="auth" for m in self.sock.sent))
        self.host.activity.finish_restart(token)

    async def test_request_record_capacity_is_fixed_and_overflow_request_is_failed(self):
        for number in range(129):await self.auth._request(self.session,self.raw(request="r"+str(number)))
        self.assertEqual(len(self.session.requests),128)
        self.assertTrue(any(m=="Fetch.failRequest" and p["requestId"]=="r128" for m,p in self.cdp.sent))

    def test_only_bounded_provenance_survives_event_sanitization(self):
        raw=self.raw();raw["request"]["headers"]={"Authorization":"synthetic-sensitive-marker"}
        raw["extra"]="synthetic-sensitive-marker";bounded=self.auth._event(raw)
        self.assertNotIn("synthetic-sensitive-marker",json.dumps(bounded))
        self.assertNotIn("headers",bounded["request"])
        raw["request"]["url"]="x"*2049;self.assertIsNone(self.auth._event(raw)["request"]["url"])

    def test_socket_strict_fields_and_no_control_credentials(self):
        good={"t":"auth","id":"a"*32,"cancel":False,"username":"fixture","password":"synthetic"}
        self.assertEqual(parse(json.dumps(good).encode()).t,"auth")
        for change in [{"extra":True},{"password":"bad\n"},{"password":"x"*4097},{"id":"no"}]:
            with self.assertRaises(Bad):parse(json.dumps({**good,**change}).encode())
        with self.assertRaises(Bad):parse(json.dumps({**good,"cancel":True}).encode())

if __name__=="__main__":unittest.main()

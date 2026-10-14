"""Finite owner metadata mailbox tests: no Chrome, owner files or listeners."""
from pathlib import Path
import asyncio
import json
import sys
import threading
import unittest
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import owner_tools as O
from jarvis.v2.browser.activity import BrowserActivity
from jarvis.v2.browser.activity_work import ActivityWork
from jarvis.v2.browser.stream import TabStream


class Parser(unittest.TestCase):
    def test_strict_find_and_query_bounds(self):
        valid={"t":"find","seq":1,"query":"ordinary 😀 text","backward":False}
        self.assertEqual(O.parse_message(valid),valid)
        self.assertTrue(O.valid_query("😀"*256))
        for query in ("", "x"*257, "\n", "\t", "\0", "\u007f", "\u0085", "\u2028", "\u202e", "\ud800"):
            with self.subTest(query=repr(query)),self.assertRaises(O.OwnerBad):
                O.parse_message({**valid,"query":query})
        for changed in ({**valid,"seq":True},{**valid,"seq":0},{**valid,"seq":2**31},
                        {**valid,"backward":1},{**valid,"script":"evil"},{k:v for k,v in valid.items() if k!="backward"}):
            with self.assertRaises(O.OwnerBad):O.parse_message(changed)

    def test_cursor_numeric_and_css_vocabulary_bounds(self):
        value={"t":"cursor","seq":1,"x":1.5,"y":0}
        self.assertEqual(O.parse_message(value),value)
        for x in (True,float("nan"),float("inf"),-1,4097,"1"):
            with self.assertRaises(O.OwnerBad):O.parse_message({**value,"x":x})
        for t in ("find_cancel","cursor_cancel"):
            self.assertEqual(O.parse_message({"t":t,"seq":3}),{"t":t,"seq":3})
            with self.assertRaises(O.OwnerBad):O.parse_message({"t":t,"seq":3,"x":0})
        for cursor in O.CURSORS:self.assertEqual(O.cursor_keyword(cursor),cursor)
        for cursor in ("auto","none","url(secret),pointer","copy","inherit",None,{},23):
            self.assertEqual(O.cursor_keyword(cursor),"default")


class Socket:
    def __init__(self):self.sent=[]
    def send_text(self,value):self.sent.append(json.loads(value))


class Host:
    def __init__(self):
        self._stopped=False;self._loop=asyncio.get_running_loop();self._capture_hold_epoch=0
        self.held=False;self._auth=NS(blocked=lambda tab:False)
        self.instance=NS(generation=7,closing=False,restoring=False)
        self._instances={"profile":self.instance};self.activity=BrowserActivity()
        self.activity.opened("profile",7);self.work=ActivityWork(self.activity)
        self.tab=NS(id="1234abcd",profile="profile",page=object(),page_identity=4,document_generation=3,pane_ready=True,retired=False,dialogs={},chooser=None)
        self.sock=Socket();self.tab.stream=TabStream(self.tab.id,held=lambda:self.held,wake=lambda:None)
        self.tab.stream._sock=self.sock;self.tab.stream.viewport=(500,400,1)
        self._tabs={self.tab.id:self.tab};self.calls=[];self.started=asyncio.Event();self.finish=None
        self.on_call=None;self.result=True;self.inflight=0;self.peak=0
    def input_held(self):return self.held
    async def _activity_run(self,tab,operation):return await self.work.run(tab.profile,7,operation)
    async def _ensure_cdp(self,tab):return NS(send=self.send)
    async def send(self,method,params=None):
        self.calls.append((method,params))
        if self.on_call:self.on_call(method)
        if method=="Page.getFrameTree":return{"frameTree":{"frame":{"id":"top"}}}
        if method=="Page.createIsolatedWorld":return{"executionContextId":123}
        self.inflight+=1;self.peak=max(self.peak,self.inflight);self.started.set()
        try:
            if self.finish is not None:await self.finish.wait()
            return{"result":{"value":self.result}}
        finally:self.inflight-=1


class Mailbox(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.host=Host();self.tools=O.OwnerTools(self.host)
    async def asyncTearDown(self):
        self.host._stopped=True
        if self.host.finish is not None:self.host.finish.set()
        for _ in range(8):await asyncio.sleep(0)
        await self.host.work.settle()
        workers=[s.worker for s in self.tools._slots.values() if s.worker is not None]
        if workers:await asyncio.gather(*workers,return_exceptions=True)
    def receive(self,t="find",seq=1,**fields):
        value={"t":t,"seq":seq,**({"query":"PRIVATE_QUERY_CANARY","backward":False} if t=="find" else {"x":10,"y":20} if t=="cursor" else {}),**fields}
        self.tools.receive(self.host.tab,self.host.sock,O.parse_message(value))
    async def drain(self):
        for _ in range(12):await asyncio.sleep(0)
    async def test_fixed_isolated_find_and_boolean_only_reply(self):
        self.receive();await self.drain()
        self.assertEqual(self.host.sock.sent,[{"t":"find","seq":1,"available":True,"matched":True}])
        methods=[method for method,_ in self.host.calls]
        self.assertEqual(methods,["Page.getFrameTree","Page.createIsolatedWorld","Runtime.evaluate"])
        expression=self.host.calls[-1][1];self.assertEqual(expression["timeout"],500)
        self.assertEqual(expression["contextId"],123);self.assertTrue(expression["returnByValue"])
        self.assertIn("window.find",expression["expression"])
        self.assertIn("false, backward, true, false, false, false",expression["expression"])
        self.assertNotIn("PRIVATE_QUERY_CANARY",repr(self.tools._slots))
        self.assertNotIn("PRIVATE_QUERY_CANARY",json.dumps(self.host.sock.sent))
    async def test_cancel_or_edit_drops_result_permanently(self):
        self.host.finish=asyncio.Event();self.receive();await self.host.started.wait()
        self.receive("find_cancel",2);self.host.finish.set();await self.drain()
        self.assertEqual(self.host.sock.sent,[])
    async def test_each_exact_identity_guard_blocks_stale_result(self):
        mutations=[
            lambda:setattr(self.host.tab,"page",object()),
            lambda:setattr(self.host.tab,"page_identity",8),
            lambda:setattr(self.host.tab,"document_generation",8),
            lambda:setattr(self.host.instance,"generation",8),
            lambda:setattr(self.host.instance,"closing",True),
            lambda:setattr(self.host.instance,"restoring",True),
            lambda:setattr(self.host.tab,"retired",True),
            lambda:setattr(self.host.tab,"pane_ready",False),
            lambda:setattr(self.host.tab.stream,"visible",False),
            lambda:setattr(self.host.tab.stream,"gen",8),
            lambda:setattr(self.host.tab.stream,"hold_count",8),
            lambda:setattr(self.host,"_capture_hold_epoch",8),
            lambda:setattr(self.host.tab.stream,"_sock",Socket()),
            lambda:setattr(self.host,"held",True),
            lambda:setattr(self.host,"_auth",NS(blocked=lambda tab:True)),
            lambda:self.host.tab.dialogs.update(one=object()),
            lambda:setattr(self.host.tab,"chooser",object()),
            lambda:self.host._tabs.clear(),
            lambda:self.host._instances.clear(),
        ]
        for mutate in mutations:
            self.host=Host();self.tools=O.OwnerTools(self.host);self.host.finish=asyncio.Event()
            self.receive();await self.host.started.wait();mutate();self.host.finish.set();await self.drain()
            self.assertEqual(self.host.sock.sent,[])
            self.assertEqual(self.host.activity.counts("profile")[0],0)
    async def test_guard_rechecked_between_each_protocol_send(self):
        for method in ("Page.getFrameTree","Page.createIsolatedWorld"):
            self.host=Host();self.tools=O.OwnerTools(self.host)
            def mutate(name):
                if name==method:self.host.tab.document_generation+=1
            self.host.on_call=mutate;self.receive();await self.drain()
            self.assertNotIn("Runtime.evaluate",[name for name,_ in self.host.calls])
            self.assertEqual(self.host.sock.sent,[])
    async def test_replaced_query_does_not_execute_after_queue_delay(self):
        self.receive();self.receive("find_cancel",2);await self.drain()
        self.assertEqual(self.host.calls,[])
    async def test_unavailable_find_never_returns_page_string(self):
        self.host.result="PAGE_SECRET_CANARY";self.receive();await self.drain()
        self.assertEqual(self.host.sock.sent,[{"t":"find","seq":1,"available":False,"matched":False}])
    async def test_no_current_socket_or_attention_starts_no_work(self):
        for change in (lambda:setattr(self.host,"held",True),lambda:setattr(self.host.tab.stream,"visible",False),
                       lambda:setattr(self.host.tab.stream,"_sock",Socket()),lambda:setattr(self.host.tab,"chooser",object())):
            self.host=Host();self.tools=O.OwnerTools(self.host);change();self.receive();await self.drain()
            self.assertEqual(self.host.calls,[])
    async def test_latest_cursor_coalesces_one_inflight_and_does_not_take_over(self):
        self.host.finish=asyncio.Event();self.host.result="pointer";self.receive("cursor",1);await self.host.started.wait()
        for seq in range(2,101):self.receive("cursor",seq,x=seq)
        self.assertEqual(len(self.tools._slots),1)
        slot=self.tools._slots[self.host.tab.id]
        self.assertEqual(slot.cursor.fields["x"],100);self.assertEqual(self.host.tab.stream.owner_input_serial,0)
        self.host.finish.set()
        await asyncio.sleep(.12);await self.drain()
        self.assertEqual(self.host.peak,1)
        self.assertEqual(len([m for m,_ in self.host.calls if m=="Runtime.evaluate"]),2)
        self.assertEqual(self.host.sock.sent,[{"t":"cursor","seq":100,"cursor":"pointer"}])
    async def test_custom_cursor_is_default_and_viewport_point_is_checked(self):
        self.host.result="url(https://secret.test),none";self.receive("cursor",1);await self.drain()
        self.assertEqual(self.host.sock.sent,[{"t":"cursor","seq":1,"cursor":"default"}])
        self.receive("cursor",2,x=500);await self.drain()
        self.assertEqual(len(self.host.calls),3)
    async def test_host_deadline_keeps_actual_activity_and_serializes_reconnect(self):
        self.host.finish=asyncio.Event()
        from unittest.mock import patch
        with patch.object(O,"RESPONSE_WAIT_S",.005):
            self.receive();await self.host.started.wait();await asyncio.sleep(.02)
            self.assertEqual(self.host.sock.sent,[{"t":"find","seq":1,"available":False,"matched":False}])
            self.assertEqual(self.host.activity.counts("profile")[0],1)
            self.tools.invalidate(self.host.tab,attachment=True)
            old=self.host.sock;self.host.sock=Socket();self.host.tab.stream._sock=self.host.sock;self.host.tab.stream.gen+=1
            self.receive("find",1);await self.drain();self.assertEqual(self.host.inflight,1)
            self.assertEqual(len([m for m,_ in self.host.calls if m=="Runtime.evaluate"]),1)
            self.host.finish.set();await self.drain()
            self.assertEqual(self.host.peak,1);self.assertEqual(self.host.activity.counts("profile")[0],0)
            self.assertEqual(self.host.sock.sent,[{"t":"find","seq":1,"available":True,"matched":True}])
            self.assertEqual(len(old.sent),1)
    async def test_hide_then_show_and_hold_then_resolve_never_replays(self):
        for hold in (False,True):
            self.host=Host();self.tools=O.OwnerTools(self.host);self.host.finish=asyncio.Event()
            self.receive();await self.host.started.wait()
            if hold:self.host.tab.stream.hold_through()
            else:self.host.tab.stream.visible=False
            self.tools.invalidate(self.host.tab)
            self.host.tab.stream.visible=True;self.host.finish.set();await self.drain()
            self.assertFalse(any(m.get("t")=="find" for m in self.host.sock.sent))
    async def test_cancellation_after_deadline_does_not_cancel_actual_child(self):
        from unittest.mock import patch
        self.host.finish=asyncio.Event()
        with patch.object(O,"RESPONSE_WAIT_S",.005):
            self.receive();await self.host.started.wait();await asyncio.sleep(.02)
            slot=self.tools._slots[self.host.tab.id];actual=slot.operation
            slot.worker.cancel();await self.drain()
            self.assertFalse(actual.cancelled());self.assertFalse(actual.cancelling())
            self.assertEqual(self.host.activity.counts("profile")[0],1)
            self.receive("find",2);await self.drain();self.assertEqual(self.host.inflight,1)
            self.host.finish.set();await self.drain();self.assertEqual(self.host.peak,1)

    async def test_closed_idle_and_scheduled_slots_are_bounded(self):
        for scheduled in (False,True):
            for _ in range(100):
                self.host=Host();self.tools.host=self.host
                self.receive()
                if not scheduled:await self.drain()
                self.tools.invalidate(self.host.tab,closed=True)
                self.host._tabs.clear();await self.drain()
                self.assertEqual(self.tools._slots,{})
        self.assertEqual(self.tools._slots,{})

    async def test_waiter_cancellation_cannot_free_busy_slot(self):
        self.host.finish=asyncio.Event();self.receive();await self.host.started.wait()
        slot=self.tools._slots[self.host.tab.id];slot.worker.cancel();await self.drain()
        self.assertEqual(self.host.activity.counts("profile")[0],1)
        self.receive("find",2);await self.drain();self.assertEqual(self.host.inflight,1)
        self.host.finish.set();await self.drain();self.assertEqual(self.host.peak,1)

if __name__=="__main__":unittest.main()

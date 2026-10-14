"""Document-commit input retirement; synthetic host only, no Chrome/listeners."""
from pathlib import Path
import asyncio,json,sys,tempfile,unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H
from jarvis.v2.browser.stream import TabStream,Msg

class DocumentInput(unittest.TestCase):
 def test_main_document_commit_retires_old_input_without_releasing_into_new_page(self):
  with tempfile.TemporaryDirectory(prefix="owner-document-input-") as temp,patch.object(config,"BROWSER_DIR",Path(temp)/"browser"):
   host=H.BrowserHost();profile="p-12345678";page=NS()
   instance=H._Instance(profile,Path(temp)/profile,Mock(),Mock(),generation=1)
   host._instances[profile]=instance;host.activity.opened(profile,1)
   sent=[];sock=NS(send_text=lambda raw:sent.append(json.loads(raw)))
   stream=TabStream("1234abcd",held=lambda:False,wake=lambda:None);stream._sock=sock
   tab=H._Tab("1234abcd",profile,None,page,stream=stream,page_identity=1,document_generation=3,pane_ready=True)
   host._tabs[tab.id]=tab
   tab.keys_pressed["Space"]=stream.hold_count;tab.pressed["left"]=stream.hold_count
   stream.enqueue(sock,Msg("key","Input.dispatchKeyEvent",{"type":"keyUp","code":"Space","key":" "}))
   stream.enqueue(sock,Msg("mouse","Input.dispatchMouseEvent",{"type":"mouseReleased","button":"left","buttons":0,"x":10,"y":20}))
   before=stream.hold_count
   host._landed(tab,NS(parent_frame=None,url="about:blank"))
   self.assertEqual(tab.document_generation,4)
   self.assertFalse(tab.keys_pressed);self.assertFalse(tab.pressed)
   self.assertGreater(stream.hold_count,before);self.assertIsNone(stream.take())
   self.assertIn({"t":"owner_reset"},sent)
   self.assertIsNone(tab.input_task,"document retirement must send no synthetic input")
 def test_subframe_and_retired_callbacks_do_not_clear_current_input(self):
  with tempfile.TemporaryDirectory(prefix="owner-document-input-") as temp,patch.object(config,"BROWSER_DIR",Path(temp)/"browser"):
   host=H.BrowserHost();tab=H._Tab("1234abcd","p-12345678",None,NS(),keys_pressed={"Space":1},pressed={"left":1})
   host._tabs[tab.id]=tab
   host._landed(tab,NS(parent_frame=object(),url="about:blank"))
   self.assertEqual(tab.keys_pressed,{"Space":1});self.assertEqual(tab.pressed,{"left":1})
   host._tabs[tab.id]=H._Tab(tab.id,tab.profile,None,NS(),keys_pressed={"KeyA":2},pressed={"middle":2})
   host._landed(tab,NS(parent_frame=None,url="about:blank"))
   self.assertEqual(host._tabs[tab.id].keys_pressed,{"KeyA":2});self.assertEqual(host._tabs[tab.id].pressed,{"middle":2})

class DispatchBarriers(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix="owner-dispatch-document-");self.addCleanup(self.temp.cleanup)
  self.patch=patch.object(config,"BROWSER_DIR",Path(self.temp.name)/"browser");self.patch.start();self.addCleanup(self.patch.stop)
  self.host=H.BrowserHost();self.host._loop=asyncio.get_running_loop();profile="p-12345678"
  self.instance=H._Instance(profile,Path(self.temp.name)/profile,Mock(),Mock(),generation=1)
  self.host._instances[profile]=self.instance;self.host.activity.opened(profile,1)
  self.stream=TabStream("1234abcd",held=lambda:False,wake=lambda:None)
  self.tab=H._Tab("1234abcd",profile,None,NS(),stream=self.stream,page_identity=1,document_generation=3,pane_ready=True)
  self.host._tabs[self.tab.id]=self.tab;self.sent=[]
  async def send(method,params):self.sent.append((method,params))
  self.cdp=NS(send=send);self.host._ensure_cdp=AsyncMock(return_value=self.cdp)
 async def asyncTearDown(self):
  self.host._loop=None;self.host._stopped=True
  await self.host._activity_work.settle()
 def landed(self):self.host._landed(self.tab,NS(parent_frame=None,url="about:blank"))
 async def test_document_commit_during_ensure_never_dispatches_old_event(self):
  entered=asyncio.Event();finish=asyncio.Event()
  async def ensure(tab):entered.set();await finish.wait();return self.cdp
  self.host._ensure_cdp=ensure
  task=asyncio.create_task(self.host._deliver(self.tab,"Input.dispatchKeyEvent",{"type":"rawKeyDown","code":"Space","key":" "}))
  await entered.wait();self.landed();finish.set();await task
  self.assertEqual(self.sent,[]);self.assertFalse(self.tab.keys_pressed)
 async def test_document_commit_after_actual_child_admission_drops_old_input_and_new_intent_works(self):
  entered=asyncio.Event();finish=asyncio.Event();original=self.host._activity_run;calls=0
  async def activity(tab,operation):
   nonlocal calls
   calls+=1
   if calls!=2:return await original(tab,operation)
   async def held_child():
    entered.set();await finish.wait();return await operation()
   return await original(tab,held_child)
  self.host._activity_run=activity
  waiting=asyncio.create_task(self.host._deliver(self.tab,"Input.dispatchKeyEvent",{"type":"rawKeyDown","code":"Space","key":" "}))
  await entered.wait();self.assertGreater(self.host.activity.counts(self.tab.profile)[0],0)
  self.landed();finish.set();await waiting
  self.assertEqual(self.sent,[]);self.assertFalse(self.tab.keys_pressed or self.tab.pressed)
  self.host._activity_run=original
  await self.host._deliver(self.tab,"Input.dispatchKeyEvent",{"type":"rawKeyDown","code":"KeyA","key":"a"})
  await self.host._deliver(self.tab,"Input.dispatchKeyEvent",{"type":"keyUp","code":"KeyA","key":"a"})
  self.assertEqual([params["code"] for _,params in self.sent],["KeyA","KeyA"])
  self.assertFalse(self.tab.keys_pressed or self.tab.pressed)

if __name__=="__main__":unittest.main()


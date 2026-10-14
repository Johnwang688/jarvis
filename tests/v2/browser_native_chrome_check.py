"""Native parent driving over real stdio relay and real isolated fixture Chrome."""
from __future__ import annotations
import asyncio,json,os,subprocess,sys,threading,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import browser_mcp_chrome_check as B
import browser_native_cancel_check as X
import mcp_session_check as F
from jarvis.v2.browser import drive as D,drive_state as S,act_review as A,act_rules as R

class NativeChrome(B.RelayChrome):
 def setUp(self):
  super().setUp()
  self.html("<input aria-label='Find' type='search'><script>window.events=[];document.querySelector('input').oninput=e=>events.push(e.target.value.length)</script>")
  self.snap=replace(self.snap,drive=True)
  self.service=D.BrowserDrive(self.daemon,enabled=lambda:True)
  self.previous_drive=self.daemon.browser_drive
  self.daemon.browser_drive=self.service
  self.addCleanup(setattr,self.daemon,"browser_drive",self.previous_drive)
  original=self.host.owner_state
  self.owner_patch=patch.object(self.host,'owner_state',side_effect=lambda tab:{**original(tab),'shown':True,'visible':True})
  self.owner_patch.start();self.addCleanup(self.owner_patch.stop)
  self.daemon.browser_panes.after_navigation=lambda snap:replace(snap,url=self.pg.url,document_generation=self.tab.document_generation) if self.valid else None
  self.installed=patch.object(D,'_installed',self.service);self.installed.start();self.addCleanup(self.installed.stop)
  self.review=patch.object(A,'judge',return_value=A.ActVerdict('allow',R.ORDINARY,'fixture','model'));self.review.start();self.addCleanup(self.review.stop)
  got=self.relay();self.assertFalse(got['isError']);self.assertIn('e1',got['content'][0]['text'])
 def meta(self,identity):
  return {'threadId':'native-parent','callId':identity,'x-codex-turn-metadata':{'thread_id':'native-parent','turn_id':'native-turn'}}
 def test_native_real_relay_types_and_driver_reports_actual_provider(self):
  got,err=F.run_relay(self.entry,[('hud_browser_act',{'action':'type','pane':'2','target':'e1','text':'fixture query'})],cwd=self.root,call_meta=self.meta('native-act-success'))
  self.assertEqual(err,'');self.assertFalse(got[10]['result']['isError'],got)
  self.assertEqual(self.run_on(self.pg.locator('input').input_value()),'fixture query')
  self.assertEqual(self.tab.stream.driver[1],'codex')
 def test_native_cancel_mid_input_keeps_exact_activity_until_real_settlement(self):
  entered=threading.Event();release=threading.Event();settled=threading.Event();sent=[];out=[]
  cdp=self.run_on(self.host._ensure_cdp(self.tab));original=cdp.send
  async def intercept(method,params=None):
   result=await original(method,params)
   if method=='Input.insertText':
    sent.append(params['text']);entered.set();await asyncio.to_thread(release.wait,5)
   return result
  actual=self.service.act
  def act(action):
   try:
    result=actual(action);out.append(result);return result
   finally:settled.set()
  env={'PATH':'/usr/bin:/bin','HOME':str(self.root),**self.entry['env']}
  proc=subprocess.Popen([self.entry['command'],*self.entry['args']],cwd=self.root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  def send(message):proc.stdin.write(json.dumps(message)+'\n');proc.stdin.flush()
  with patch.object(cdp,'send',side_effect=intercept),patch.object(self.service,'act',side_effect=act):
   try:
    send({'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'hud_browser_act','arguments':{'action':'type','pane':'2','target':'e1','text':'z'*100},'_meta':self.meta('native-cancel-mid-input')}})
    self.assertTrue(entered.wait(4))
    send({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':7}})
    # Ping proves the cancellation notification has been processed in order.
    send({'jsonrpc':'2.0','id':9,'method':'ping','params':{}})
    import select
    self.assertTrue(select.select([proc.stdout],[],[],3)[0]);self.assertEqual(json.loads(proc.stdout.readline())['id'],9)
    self.assertGreater(self.host.activity.counts(self.tab.profile)[0],0)
    other=replace(self.session.drive_turn,thread_id='other-native-fixture')
    with self.assertRaises(S.DriveStateRefused):self.service.state.reserve(other,self.snap.tab_id)
    self.assertFalse(settled.is_set());release.set();self.assertTrue(settled.wait(5))
    self.assertEqual(out[0].outcome,'stopped',out);self.assertIn('32 characters',out[0].text)
    self.assertEqual(sent,['z'*32]);self.assertEqual(self.run_on(self.pg.locator('input').input_value()),'z'*32)
    self.assertEqual(self.host.activity.counts(self.tab.profile)[0],0)
    self.assertFalse(self.session.drive_turn.closed);self.assertFalse(self.session.cancelled.is_set())
   finally:
    release.set();proc.stdin.close()
    try:proc.wait(3)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(3)
    self.assertEqual(proc.stderr.read(),'');proc.stdout.close();proc.stderr.close()

def load_tests(loader,tests,pattern):
 return unittest.TestSuite(NativeChrome(name) for name in loader.getTestCaseNames(NativeChrome) if name.startswith('test_native_'))

if __name__=='__main__':unittest.main(verbosity=2)

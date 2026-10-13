"""Independent BR6 acceptance: production Chrome + BrowserLook + stdio relay.

Only synthetic local pages; the fixture proxy refuses all external networking.
No native provider/account is used by this deterministic suite.
"""
from __future__ import annotations
import base64,json,threading,time,unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import hud_browser_view_check as B
import mcp_session_check as F
import codex_session_mcp_check as C
from jarvis.v2.model import ProviderName as N,Role,PermissionProfile
from jarvis.v2.provider import Brief,UserMessage
from jarvis.v2.providers import codex
from jarvis.v2 import bridge
from jarvis.v2.browser import look as L
from jarvis.v2.tools import hud_browser_view as BV

VISIBLE="BR6_SYNTHETIC_PUBLIC_ARTICLE"
PRIVATE="BR6_SYNTHETIC_FORM_ONLY"
HTML="""<title>Synthetic visual fixture</title><style>body{margin:0;background:white}</style>
<p style="position:absolute;left:10px;top:10px">BR6_SYNTHETIC_PUBLIC_ARTICLE</p>
<canvas width="160" height="160" style="position:absolute;left:40px;top:90px"></canvas>
<input style="position:absolute;left:260px;top:70px;width:240px;height:40px" value="BR6_SYNTHETIC_FORM_ONLY">
<iframe srcdoc="<body style='margin:0;background:red'>BR6_SYNTHETIC_IFRAME_ONLY</body>" style="position:absolute;left:260px;top:160px;width:240px;height:70px"></iframe>
<script>let c=document.querySelector('canvas').getContext('2d');c.fillStyle='#ff00aa';c.beginPath();c.arc(80,80,60,0,Math.PI*2);c.fill();
window.changes=[];window.events=[];new MutationObserver(x=>changes.push(...x)).observe(document,{subtree:true,attributes:true,childList:true,characterData:true});
for(let e of ['input','change','click','focus'])document.addEventListener(e,()=>events.push(e),true);
</script>"""

class RelayChrome(B.Real):
 def setUp(self):
  super().setUp()
  self.html(HTML);self.run_on(self.pg.wait_for_timeout(50))
  self.fake=F.FakeCli(N.CODEX)
  self.identity=C.native_handle();native=codex.CodexProvider()
  self.fake.admit_foreground_mcp=lambda handle,ctx:native.admit_foreground_mcp(self.identity,ctx)
  self.daemon.providers[N.CODEX]=self.fake
  self.daemon.permit_factory=self.daemon._permit
  project_dir=self.root/"relay-project";project_dir.mkdir(exist_ok=True)
  self.project=self.daemon.stores.projects.create("Synthetic",str(project_dir),profile=PermissionProfile.AUTO)
  self.thread=self.daemon.open_thread(self.project.id,Role.CHAT,N.CODEX,Brief(Role.CHAT,str(project_dir),model="gpt-6.1-sol",effort="low"))
  self.release=threading.Event();self.entered=threading.Event()
  def held(handle,message):self.entered.set();self.release.wait(45)
  self.fake.script[self.thread.id]=held
  self.daemon.send(self.thread.id,UserMessage("synthetic",desk=True,via="hud"))
  self.assertTrue(self.entered.wait(3))
  self.session=self.daemon._sessions[self.thread.id]
  self.entry=F.entry_of(self.fake.opened[-1].brief)
  self.addCleanup(self.end_turn)
  self.serial=0
 def end_turn(self):
  self.release.set()
  self.daemon.close_thread(self.thread.id)
 def relay(self,**meta_change):
  self.serial+=1
  meta={"threadId":"native-parent","callId":"relay-"+str(self.serial),"x-codex-turn-metadata":{"thread_id":"native-parent","turn_id":"native-turn"}}
  meta.update(meta_change)
  got,err=F.run_relay(self.entry,[("hud_browser_view",{"pane":"2"})],cwd=self.root,call_meta=meta)
  self.assertEqual(err,"")
  self.assertIn(10,got)
  return got[10]["result"]
 def test_br6_image_mask_and_no_page_mutation(self):
  self.assertTrue(self.session.desk.slot["images"])
  before=self.run_on(self.pg.evaluate("[changes.length,events.length,scrollX,scrollY,document.activeElement.tagName]"))
  answer=self.relay()
  self.assertFalse(answer["isError"],answer)
  text=answer["content"][0]["text"]
  self.assertIn(VISIBLE,text)
  self.assertNotIn(PRIVATE,text);self.assertNotIn("BR6_SYNTHETIC_IFRAME_ONLY",text)
  self.assertNotIn("ff00aa",text);self.assertNotIn("circle",text.lower())
  block=answer["content"][1];self.assertEqual(block["type"],"image")
  self.assertEqual(block["mimeType"],"image/png")
  from PIL import Image
  with Image.open(BytesIO(base64.b64decode(block["data"]))) as image:
   self.assertEqual(image.getpixel((120,170)),(255,0,170))
   self.assertEqual(image.getpixel((300,90)),(24,26,29))
   self.assertEqual(image.getpixel((300,190)),(24,26,29))
  after=self.run_on(self.pg.evaluate("[changes.length,events.length,scrollX,scrollY,document.activeElement.tagName]"))
  self.assertEqual(after,before)
  serialized=json.dumps(self.records)+json.dumps(self.logs)
  self.assertNotIn(VISIBLE,serialized);self.assertNotIn(PRIVATE,serialized)
 def test_br6_child_cannot_capture_and_unknown_model_is_text_only(self):
  with patch.object(self.host,"capture_look",wraps=self.host.capture_look) as capture:
   answer=self.relay(threadId="child")
   self.assertTrue(answer["isError"]);capture.assert_not_called()
  self.session.applied=("unknown-model","low")
  self.daemon._desk_images(self.session,self.session.worker,self.session.turn_id,self.session.desk.slot)
  answer=self.relay()
  self.assertFalse(answer["isError"])
  self.assertIn(VISIBLE,answer["content"][0]["text"])
  self.assertEqual([x["type"] for x in answer["content"]],["text"])
 def test_br6_postcapture_revocation_withholds_every_block(self):
  original=self.host.capture_look
  def revoke(*args,**kwargs):
   result=original(*args,**kwargs)
   self.session.desk.reserve()
   return result
  with patch.object(self.host,"capture_look",side_effect=revoke):
   answer=self.relay()
  self.assertTrue(answer["isError"])
  self.assertEqual([x["type"] for x in answer["content"]],["text"])
  self.assertNotIn(VISIBLE,json.dumps(answer))
  self.assertNotIn(PRIVATE,json.dumps(answer))
  self.assertTrue(any(e.get("kind")=="browser_viewed" and e["data"]["refused"] for e in self.records))

def load_tests(loader,tests,pattern):
 return unittest.TestSuite(RelayChrome(name) for name in loader.getTestCaseNames(RelayChrome) if name.startswith("test_br6_"))

if __name__=="__main__":unittest.main(verbosity=2)

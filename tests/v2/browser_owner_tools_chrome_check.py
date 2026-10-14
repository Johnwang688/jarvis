"""Production Chrome + actual owner ticket/socket, blocked network, ephemeral ports.

Run only the named fixture below; imported BrowserStream's broader suite is not
selected. No real profile, account, paid model or live Jarvis port is used.
"""
from pathlib import Path
import asyncio
import json
import sys
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.v2 import browser_routes_check as B
from jarvis.v2.browser import owner_tools as O
from jarvis import config
from tests.v2.owner_browser_check import rss_bytes

QUERY="FIND_PRIVATE_CANARY"
HTML="""<!doctype html><style>body{margin:0}p{margin-top:600px}#pointer{position:fixed;top:0;left:0;width:100px;height:100px;cursor:pointer}#bad{position:fixed;top:0;left:100px;width:100px;height:100px;cursor:none}iframe{position:fixed;top:0;left:200px;width:100px;height:100px}</style>
<div id=pointer>pointer</div><div id=bad>bad</div><p id=one>first FIND_PRIVATE_CANARY</p><p id=two>second FIND_PRIVATE_CANARY</p><p hidden>HIDDEN_CANARY</p><iframe srcdoc='<p>FRAME_CANARY</p>'></iframe>
<script>window.find=()=>{throw Error('page world override');};window.mutations=0;window.selectionEvents=0;new MutationObserver(x=>mutations+=x.length).observe(document.documentElement,{attributes:true,childList:true,subtree:true,characterData:true});document.addEventListener('selectionchange',()=>selectionEvents++);</script>"""

class OwnerChrome(B.BrowserStream):
 def test_owner_tools(self):
  tid=self.owner("POST","/browser/tabs",{"url":self.url("/input")})["tab_id"]
  window=self.attached(tid);tab=self.host._tabs[tid]
  self.run_on(tab.page.set_content(HTML))
  self.wait(lambda:tab.pane_ready)
  window.send({"t":"resize","width":800,"height":700});window.settled()
  peaks=[rss_bytes(B.chrome_tree(config.BROWSER_DIR))]
  anchors=[]
  for seq,backward in [(1,False),(2,False),(3,False),(4,True)]:
   window.send({"t":"find","seq":seq,"query":QUERY,"backward":backward})
   result=window.wait_for("find",seq=seq)
   self.assertEqual(result,{"t":"find","seq":seq,"available":True,"matched":True})
   anchors.append(self.js(tid,"getSelection().anchorNode.parentElement.id"))
  self.assertEqual(anchors,["one","two","one","two"])
  peaks.append(rss_bytes(B.chrome_tree(config.BROWSER_DIR)))
  for seq,query in [(5,"MISSING_CANARY"),(6,"HIDDEN_CANARY"),(7,"FRAME_CANARY")]:
   window.send({"t":"find","seq":seq,"query":query,"backward":False})
   self.assertEqual(window.wait_for("find",seq=seq),{"t":"find","seq":seq,"available":True,"matched":False})
  self.assertEqual(self.js(tid,"mutations"),0)
  self.assertGreater(self.js(tid,"selectionEvents"),0)
  before=tab.stream.owner_input_serial
  for seq,x,cursor in [(8,50,"pointer"),(9,150,"default"),(10,250,"default")]:
   window.send({"t":"cursor","seq":seq,"x":x,"y":50})
   self.assertEqual(window.wait_for("cursor",seq=seq),{"t":"cursor","seq":seq,"cursor":cursor})
  self.assertEqual(tab.stream.owner_input_serial,before,"cursor metadata must not take over")

  # Open shadows are followed; frames deliberately fall back to default.
  self.run_on(tab.page.evaluate("() => {const d=document.createElement('div');d.style='position:fixed;top:110px;left:0;width:100px;height:100px';d.attachShadow({mode:'open'}).innerHTML='<span style=\"display:block;width:100px;height:100px;cursor:text\">shadow</span>';document.body.append(d);}"))
  window.send({"t":"cursor","seq":11,"x":50,"y":150})
  self.assertEqual(window.wait_for("cursor",seq=11)["cursor"],"text")
  # Pin a blocked evaluate, replace the receiving query, prove one actual child.
  entered=asyncio.Event();finish=asyncio.Event();calls=[];inflight=[0,0]
  original=tab.cdp.send
  async def send(method,params=None):
   if method=="Runtime.evaluate" and params.get("contextId"):
    calls.append(params);inflight[0]+=1;inflight[1]=max(inflight)
    entered.set()
    try:await finish.wait()
    finally:inflight[0]-=1
   return await original(method,params)
  tab.cdp.send=send
  try:
   window.send({"t":"cursor","seq":12,"x":50,"y":50});self.run_on(entered.wait())
   for seq in range(13,113):window.send({"t":"cursor","seq":seq,"x":150,"y":50})
   window.settled();self.assertEqual(len(calls),1)
   self.host._loop.call_soon_threadsafe(finish.set)
   self.assertEqual(window.wait_for("cursor",seq=112)["cursor"],"default")
   self.assertEqual(inflight[1],1);self.assertEqual(len(calls),2)
  finally:
   self.host._loop.call_soon_threadsafe(finish.set);tab.cdp.send=original

  # A same-URL document navigation withdraws the UI generation, even if its
  # location string is unchanged; old responses must not be replayed.
  old_doc=tab.document_generation
  self.run_on(tab.page.reload())
  self.wait(lambda:tab.document_generation>old_doc)
  window.wait_for("owner_reset")
  with self.card():
   start=len(window.messages)
   window.send({"t":"find","seq":113,"query":QUERY,"backward":False});window.settled()
   self.assertFalse(any(m.get("t")=="find" and m.get("seq")==113 for m in window.messages[start:]))
  window.send({"t":"ping"});window.wait_for("pong")
  self.assertNotIn(QUERY,json.dumps(self.records));self.assertNotIn(QUERY,"\n".join(self.logs))
  peaks.append(rss_bytes(B.chrome_tree(config.BROWSER_DIR)))
  self.assertLess(max(peaks),1.5*2**30)
  print("owned Chrome sampled summed RSS MiB:",[round(p/2**20,1) for p in peaks])
  self.owner("DELETE",f"/browser/tabs/{tid}")
  self.wait(lambda:not self.host._owner_tools._slots,what="closed owner slots pruned")

if __name__=="__main__":
 result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([OwnerChrome("test_owner_tools")]))
 raise SystemExit(not result.wasSuccessful())

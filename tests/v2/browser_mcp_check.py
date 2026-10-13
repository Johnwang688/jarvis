"""BR6 native-session Browser reads; temp daemon, scripted providers, no account IO."""
from __future__ import annotations
import base64,json,os,sys,threading,unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import codex_session_mcp_check as C
import terminal_read_mcp_check as T
import hud_browser_view_check as B
from jarvis import runtime,tools,untrusted
from jarvis.v2 import bridge,session_tools,router,mcp_session
from jarvis.v2.browser import look as L
from jarvis.v2.providers import claude,codex
from jarvis.v2.tools import hud_browser_view as BV
setUpModule=T.setUpModule
tearDownModule=T.tearDownModule
MARKER="BR6_PRIVATE_PAGE_CANARY"

def png():
 from PIL import Image
 buf=BytesIO();Image.new("RGB",(20,10),"blue").save(buf,format="PNG")
 return base64.b64encode(buf.getvalue()).decode()

class CatalogAndSize(unittest.TestCase):
 def test_exact_session_flags_default_and_external_exclusion(self):
  for provider in ("claude","codex"):
   entry=session_tools.offered(provider,"hud_browser_view")
   self.assertTrue(entry.read_only and entry.foreground_only and entry.desk_only)
   spec=next(row for row in bridge.listing(provider) if row["name"]=="hud_browser_view")
   self.assertEqual(spec["annotations"],{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False})
   self.assertEqual(set(spec["inputSchema"]["properties"]),{"pane","whole_page"})
  from jarvis.v2 import mcp
  self.assertNotIn("hud_browser_view",mcp.MCP_TOOLS)
  self.assertNotIn("hud_browser_view",tools.default_names())
  for provider in ("unknown","fast"):
   self.assertIsNone(session_tools.offered(provider,"hud_browser_view"))
 def test_real_high_entropy_png_omits_only_image(self):
  from PIL import Image
  buf=BytesIO();Image.frombytes("RGB",(1568,1568),os.urandom(1568*1568*3)).save(buf,format="PNG")
  image=L.masked_image(base64.b64encode(buf.getvalue()).decode(),B.raw(width=1568,height=1568))
  self.assertGreater(len(image),mcp_session.MAX_RESPONSE)
  result=L.presented(B.raw(lines=[MARKER]),image,fresh=False,vision=True,withheld=False)
  self.assertEqual(result.text,MARKER);self.assertIsNone(result.image)
  self.assertIn("image omitted because it exceeds the transport size limit",result.notes)
  self.assertFalse(any("page changed" in note for note in result.notes))
 def test_at_limit_serialized_body_keeps_margin_and_over_limit_keeps_text(self):
  scan=B.raw(lines=["\U0001f642"*5000],title="\U0001f642"*120,url="https://example.invalid/"+"\U0001f642"*2048,
             refs=[{"ref":"e"+str(i),"tag":"button","label":"\U0001f642"*120} for i in range(200)])
  image="A"*L.IMAGE_B64_CAP
  result=L.presented(scan,image,fresh=False,vision=True,withheld=False)
  body={"text":untrusted.fence(result.text,result.source,notes=list(result.notes)),"isError":False,
        "image":{"data":result.image,"mimeType":"image/png"}}
  self.assertIs(result.image,image)
  self.assertLess(len(json.dumps(body,ensure_ascii=True).encode()),mcp_session.MAX_RESPONSE)
  omitted=L.presented(scan,image+"A",fresh=False,vision=True,withheld=False)
  self.assertIsNone(omitted.image);self.assertEqual(omitted.text,result.text)
  normal=L.presented(B.raw(),png(),fresh=False,vision=True,withheld=False)
  self.assertIsNotNone(normal.image)
 def test_exact_vision_boolean_no_unknown_model_or_alias_guess(self):
  with patch.dict(router.CLI_MODELS,{"codex":{"known":{"vision":True},"false":{"vision":False},"truthy":{"vision":1}}}):
   self.assertTrue(router.cli_images("codex","known"))
   for model in (None,"false","truthy","missing","known-alias"):
    self.assertFalse(router.cli_images("codex",model))
   self.assertFalse(router.cli_images("claude","known"))

class BrowserBridge(C.ForegroundBridge):
 def setUp(self):
  super().setUp()
  self.session=self.d._sessions[self.thread.id]
  self.snapshot=B.snapshot();self.valid=True;self.seen=[];self.notes=[];self.serial=0
  self.scan=B.raw(lines=[MARKER],raw=[MARKER]);self.image=png()
  def capture(snapshot,whole,vision,forms,guard):
   guard();self.seen.append((whole,vision));return self.scan,self.image if vision else None,False
  self.host=SimpleNamespace(input_held=lambda:False,capture_look=capture)
  self.browser_daemon=SimpleNamespace(browser=self.host,terminals=SimpleNamespace(readable_source=lambda _:True),
      browser_panes=SimpleNamespace(resolve=lambda _:self.snapshot,still_current=lambda s:self.valid and s==self.snapshot),
      bus=SimpleNamespace(publish=self.notes.append))
  self.service=L.BrowserLook(self.browser_daemon)
  self.stack.enter_context(patch.object(BV,"look_for_tool",self.service.read))
  self.stack.enter_context(patch.object(L.secrets,"secret_values",return_value=[]))
  self.pin("gpt-6.1-sol")
 def pin(self,model):
  self.session.applied=(model,"low")
  self.d._desk_images(self.session,self.session.worker,self.session.turn_id,self.session.desk.slot)
 def view(self,ctx=True):
  self.serial+=1
  context=C.context(call_id="browser-"+str(self.serial)) if ctx is True else ctx
  return C.F.http(self.d.port,"POST","/mcp/call",token=self.token,
    body={"name":"hud_browser_view","arguments":{"pane":"2"},"codex_context":context})[1]
 def test_parent_text_image_and_exact_read_note(self):
  got=self.view();self.assertIn(MARKER,got["text"])
  self.assertEqual(got["image"],{"data":self.image,"mimeType":"image/png"})
  self.assertEqual(self.seen,[(False,True)])
  self.assertEqual(set(self.notes[0]["data"]),{"tab_id","at","refused"})
  self.assertNotIn(MARKER,json.dumps(self.notes))
 def test_child_missing_and_replayed_identity_never_capture(self):
  for context in (None,C.context(thread_id="child"),C.context(subagent=True)):
   self.assertTrue(self.view(context)["isError"])
  self.assertEqual(self.seen,[])
  ctx=C.context(call_id="exact-once")
  self.assertFalse(self.view(ctx)["isError"])
  self.assertTrue(self.view(ctx)["isError"]);self.assertEqual(len(self.seen),1)
 def test_unknown_applied_model_text_only_and_catalog_change_stays_pinned(self):
  self.pin("unknown-model");got=self.view()
  self.assertNotIn("image",got);self.assertIn("not known to accept images",got["text"])
  self.pin("gpt-6.1-sol")
  with patch.dict(router.CLI_MODELS,{"codex":{}}):
   self.assertIn("image",self.view())
 def test_requested_model_does_not_override_actual_applied_model(self):
  self.session.thread.model="gpt-6.1-sol";self.pin("unknown-surviving-model")
  self.assertNotIn("image",self.view())
 def test_refused_model_switch_pins_surviving_applied_model(self):
  old="gpt-6.1-sol";wanted="gpt-6-sol"
  self.session.applied=(old,"low");self.session.applied_record=(old,"low")
  self.session.thread.model=wanted;self.session.thread.effort="low"
  from jarvis.v2.provider import BriefRefused
  with patch.object(self.codex,"set_model",create=True,side_effect=BriefRefused("synthetic refusal")),patch.dict(router.CLI_MODELS,{"codex":{old:{"vision":False,"efforts":("low",)},wanted:{"vision":True,"efforts":("low",)}}}):
   self.d._apply_choice(self.session)
   self.assertEqual(self.session.applied,(old,"low"))
   self.d._desk_images(self.session,self.session.worker,self.session.turn_id,self.session.desk.slot)
   self.assertNotIn("image",self.view())
 def test_slot_replacement_during_capability_lookup_is_not_granted_images(self):
  slot=self.session.desk.slot;turn=self.session.turn_id
  def capability(*args):
   self.session.desk.begin(self.session.brief,T.HUD,"next");return True
  with patch.object(router,"cli_images",side_effect=capability):
   self.d._desk_images(self.session,self.session.worker,turn,slot)
  self.assertFalse(self.session.desk.slot["images"])
 def test_late_model_binding_cannot_enable_successor_slot_or_revoked_presence(self):
  old=self.session.desk.slot;turn=self.session.turn_id;worker=self.session.worker
  self.session.desk.reserve();self.pin("gpt-6.1-sol")
  self.assertFalse(old["present"]);self.assertTrue(self.view()["isError"])
  new=self.session.desk.begin(self.session.brief,T.HUD,"synthetic-successor")
  self.d._desk_images(self.session,worker,turn,old)
  self.assertFalse(new["images"])
 def test_see_source_hidden_covered_and_changed_refuse_before_capture(self):
  original=self.snapshot
  cases=[replace(original,see=False),replace(original,drawn=False),replace(original,covered=True),
         replace(original,source=B.ResolvedSource("unknown"))]
  for snap in cases:
   self.snapshot=snap;self.assertTrue(self.view()["isError"])
  self.snapshot=replace(original,source=B.ResolvedSource("terminal",terminal_id="fixture"))
  self.browser_daemon.terminals.readable_source=lambda _:False
  self.assertTrue(self.view()["isError"]);self.assertEqual(self.seen,[])
 def test_guard_rejects_whole_text_and_image(self):
  self.scan=B.raw(lines=[MARKER],raw=[MARKER])
  with patch.object(L.secrets,"secret_values",return_value=[MARKER]):got=self.view()
  self.assertTrue(got["isError"]);self.assertNotIn("image",got);self.assertNotIn(MARKER,got["text"])
 def test_revocation_after_capture_withholds_text_and_image(self):
  for stop in (lambda:self.session.desk.reserve(),lambda:setattr(self,"valid",False),
               lambda:self.native.native.closed.set()):
   old=self.host.capture_look
   def capture(*args):
    answer=old(*args);stop();return answer
   with patch.object(self.host,"capture_look",capture):got=self.view()
   self.assertTrue(got["isError"]);self.assertNotIn("image",got);self.assertNotIn(MARKER,got["text"])
   self.valid=True;self.native.native.closed.clear()
   self.session.desk.pending.clear();self.session.desk.carry.clear()
   self.session.desk.begin(self.session.brief,T.HUD,self.session.turn_id);self.pin("gpt-6.1-sol")
 def test_real_relay_preserves_native_image_block(self):
  entry=C.F.entry_of(self.run_brief(self.thread))
  meta={"threadId":"native-parent","callId":"relay-browser","x-codex-turn-metadata":{"thread_id":"native-parent","turn_id":"native-turn"}}
  got,err=C.F.run_relay(entry,[("hud_browser_view",{"pane":"2"})],cwd=self.root,call_meta=meta)
  content=got[10]["result"]["content"]
  self.assertEqual(content[1],{"type":"image","data":self.image,"mimeType":"image/png"})
  self.assertIn(MARKER,content[0]["text"]);self.assertNotIn(MARKER,err)

class ClaudeBoundary(C.F.DaemonHarness):
 held=T.LifecycleChecks.held
 hook=T.LifecycleChecks.hook
 finish=T.LifecycleChecks.finish
 def test_exact_browser_child_hook_refuses_without_card_or_capture(self):
  thread,session,release=self.held()
  with patch.object(BV,"look_for_tool") as look:
   decision=self.hook(thread,"hud_browser_view",agent_id="synthetic-child")
   self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"],"deny")
   look.assert_not_called();self.assertEqual(self.d.approvals.pending(),[])
  self.assertEqual(self.hook(thread,"hud_browser_view")["hookSpecificOutput"]["permissionDecision"],"allow")
 def test_claude_real_relay_image_and_offdesk_refusal(self):
  for message,allowed in ((T.HUD,True),(T.REMOTE,False)):
   thread,session,release=self.held(message)
   session.applied=("claude-sonnet-5-5","low")
   self.d._desk_images(session,session.worker,session.turn_id,session.desk.slot)
   with patch.object(BV,"look_for_tool",return_value=L.LookResult(text=MARKER,source="fixture",image=png())) as look:
    responses,err=C.F.run_relay(C.F.entry_of(self.run_brief(thread)),[("hud_browser_view",{"pane":"2"})],cwd=self.root)
   result=responses[10]["result"];self.assertEqual(result["isError"],not allowed)
   self.assertEqual(look.call_count,int(allowed))
   self.assertEqual(any(row["type"]=="image" for row in result["content"]),allowed)
   self.assertNotIn(MARKER,err);self.finish(thread,release)

class PrivateEvents(unittest.TestCase):
 def test_claude_success_error_denial_keep_fixed_browser_summary(self):
  provider=claude.ClaudeProvider()
  for name in ("hud_browser_view","mcp__jarvis__hud_browser_view"):
   for text,error,denied,want in ((MARKER,False,False,"looked at a page"),(MARKER,True,False,"refused"),(MARKER,False,True,"refused"),("Refused: "+MARKER,True,False,"refused")):
    session=SimpleNamespace(thread_id="chat",tools_seen={"c":(name,{"pane":"2"})},denied_tools={"c"} if denied else set(),denial_without_id=False)
    got=provider._tool_result(session,claude.ToolResultBlock(tool_use_id="c",content=text,is_error=error))
    self.assertEqual(got[-1].data["summary"],want);self.assertNotIn(MARKER,json.dumps([e.data for e in got]))
  session=SimpleNamespace(thread_id="chat",tools_seen={"c":("mcp__jarvis__hud_browser_view",{})},denied_tools=set(),denial_without_id=False)
  events=provider._tool_result(session,claude.ToolResultBlock(tool_use_id="c",content=claude.CLASSIFIER_DENIAL_PREFIX+" reason: "+MARKER,is_error=True))
  self.assertEqual(events[0].data["reason"],"refused");self.assertNotIn(MARKER,json.dumps([e.data for e in events]))
  self.assertEqual(provider._tool_summary("mcp__foreign__hud_browser_view",MARKER),MARKER)
 def test_codex_browser_result_error_review_and_interrupt_are_private(self):
  provider=codex.CodexProvider();h=C.native_handle()
  def emit(i,text,status="completed"):
   item={"id":i,"type":"mcpToolCall","server":"jarvis","tool":"hud_browser_view","arguments":{"pane":"2"},"status":status,
         "result":{"content":[{"type":"text","text":text},{"type":"image","data":MARKER}],"structuredContent":{"page":MARKER}}}
   return list(provider._notification(h,"item/completed",{"threadId":"native-parent","turnId":"native-turn","item":item}))
  for i,text,status,want in (("ok",MARKER,"completed","looked at a page"),("no","Refused: "+MARKER,"completed","refused"),("error",MARKER,"failed","refused")):
   events=emit(i,text,status);self.assertEqual(events[-1].data["summary"],want);self.assertNotIn(MARKER,json.dumps([e.data for e in events]))
  review={"threadId":"native-parent","turnId":"native-turn","targetItemId":None,"action":{"type":"mcpToolCall","server":"jarvis","toolName":"hud_browser_view","toolTitle":MARKER},"review":{"status":"denied","rationale":MARKER}}
  events=list(provider._notification(h,"item/autoApprovalReview/completed",review))
  self.assertEqual(events[0].data["reason"],"refused");self.assertNotIn(MARKER,json.dumps([e.data for e in events]))
  h.native.mcp_items["pending"]=("jarvis","hud_browser_view","hud_browser_view",False)
  self.assertEqual(list(provider._mcp_unfinished(h))[0].data["summary"],"refused")

if __name__=="__main__":unittest.main(verbosity=2)

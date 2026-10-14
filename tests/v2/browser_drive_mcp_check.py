"""Native Browser action delegation: synthetic providers and real daemon/relay gates."""
from __future__ import annotations
import asyncio,json,os,select,subprocess,sys,threading,unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import mcp_session_check as F
import terminal_read_mcp_check as T
import codex_session_mcp_check as C
import browser_drive_gate_check as G
import browser_drive_downloads_check as DD
import browser_panes_check as P
from jarvis import runtime,tools
from jarvis.v2 import bridge,session_tools
from jarvis.v2.browser import drive as D,drive_state as S,look as L,act_review as A,act_rules as R
from jarvis.v2.browser.host import BrowserHost
from jarvis.v2.browser.panes import BrowserPanes
from jarvis.v2.permissions import PermitContext,build_permit
from jarvis.v2.provider import Brief,Decision,Event,EventKind as K,UserMessage,SteerRefused
from jarvis.v2.model import PermissionProfile,Role
from jarvis.v2.approvals import PendingApprovals,ApprovalRequest
from jarvis.v2.providers import claude,codex
from jarvis.v2.tools import hud_browser_view as BV
setUpModule=T.setUpModule
tearDownModule=T.tearDownModule
ACT="hud_browser_act"
SECRET="PRIVATE_BROWSER_ACTION_CANARY"

class Catalog(unittest.TestCase):
 def test_exact_owner_action_hints_scope_and_timeout(self):
  for provider in ("claude","codex"):
   entry=session_tools.offered(provider,ACT)
   self.assertTrue(entry.owner_gated and entry.foreground_only and entry.desk_only)
   self.assertFalse(entry.read_only)
   spec=next(x for x in bridge.listing(provider) if x["name"]==ACT)
   self.assertEqual(spec["annotations"],{"readOnlyHint":False,"destructiveHint":True,"openWorldHint":True})
  from jarvis.v2.mcp_catalog import MCP_TOOLS
  self.assertNotIn(ACT,MCP_TOOLS);self.assertNotIn(ACT,tools.default_names())
  self.assertNotIn(ACT,tools.PARALLEL_SAFE)
  config=bridge.server_entry("codex",12345,"placeholder")
  self.assertEqual(config["tool_timeout_sec"],900)
  self.assertEqual(config["tools"][ACT],{"approval_mode":"approve"})
  with self.assertRaises(ValueError):session_tools.SessionTool(ACT,read_only=True,owner_gated=True)
 def test_owner_gate_registration_refuses_inconsistent_or_dangerous_entry(self):
  for flags in ({"read_only":True},{"foreground_only":False},{"desk_only":False}):
   values={"foreground_only":True,"desk_only":True,"owner_gated":True,**flags}
   with self.assertRaises(ValueError):session_tools.SessionTool("invalid",**values)
  for name in ("not_registered_native_fixture",F.SEND):
   with self.assertRaises(ValueError):session_tools.register(session_tools.SessionTool(name,foreground_only=True,desk_only=True,owner_gated=True))

 def test_binding_is_nonretiring_exact_provider_and_resets(self):
  before=S.current_turn()
  for provider in ("fast","claude","codex"):
   turn=S.DriveTurn("thread","turn",False,"/tmp","owner",provider=provider)
   with runtime.turn_scope(),S.bind_turn(turn):
    runtime.bind_caller("thread","project",provider,"turn")
    self.assertIs(S.current_turn(),turn)
    runtime.bind_caller("thread","project","foreign","turn");self.assertIsNone(S.current_turn())
   self.assertFalse(turn.closed);self.assertIs(S.current_turn(),before)
  with runtime.turn_scope(),S.bind_turn(replace(turn,provider="foreign")):
   runtime.bind_caller("thread","project","foreign","turn");self.assertIsNone(S.current_turn())
 def test_no_service_or_shipping_off_denies_even_environment(self):
  for service in (None,D.BrowserDrive(Mock()),SimpleNamespace(closed=False,enabled=lambda:1)):
   with patch.object(D,"_installed",service),patch.dict("os.environ",{"JARVIS_AGENTS_MAY_DRIVE":"1"}):
    self.assertFalse(D.admission_for_tool())

class Delegation(unittest.TestCase):
 def permit(self,provider="claude",rule=True,**kw):
  brief=Brief(Role.CHAT,"/tmp",mcp_servers={"jarvis":{}},profile=PermissionProfile.AUTO)
  asker=Mock(human_backed=True);asker.ask.return_value=Decision.ALLOW
  permit=build_permit(PermitContext("chat",brief,provider=provider,**kw),asker)
  name=ACT if provider=="codex" else "mcp__jarvis__"+ACT
  return permit,asker,name
 def test_named_rule_private_projection_then_delegate_without_human_flag(self):
  for provider in ("claude","codex"):
   permit,asker,name=self.permit(provider)
   with patch("jarvis.v2.permissions.always_ask_match",return_value=("private-rule",SECRET)),patch("jarvis.v2.permissions.log_decision") as log:
    self.assertIs(permit(name,{"text":SECRET,"file":"/private/"+SECRET}),Decision.ALLOW)
   self.assertTrue(asker.ask.called)
   request=asker.ask.call_args.args[0]
   self.assertEqual(request.tool,ACT);self.assertEqual(request.layer,"always-ask")
   self.assertEqual(request.browser,{"kind":"delegation","tool":ACT})
   self.assertEqual(request.args,{});self.assertIsNone(request.command)
   self.assertTrue(request.private);self.assertFalse(request.allowlistable)
   self.assertEqual(request.max_wait_s,120)
   self.assertNotIn(SECRET,json.dumps(request.owner_json())+json.dumps(request.to_json())+str(log.call_args))
   self.assertFalse(getattr(permit,"jarvis_human_backed",False))
   with patch("jarvis.v2.permissions.always_ask_match",return_value=None),patch("jarvis.v2.permissions.log_decision") as log:
    self.assertIs(permit(name,{}),Decision.ALLOW)
   self.assertEqual(log.call_args.args[0].layer,"jarvis-gated")
 def test_alias_matching_receives_original_arguments(self):
  for provider in ("claude","codex"):
   permit,asker,name=self.permit(provider);seen=[]
   def match(tool,args,*_):
    seen.append((tool,args));return ("alias","fixed") if len(seen)==2 else None
   with patch("jarvis.v2.permissions.always_ask_match",side_effect=match),patch("jarvis.v2.permissions.log_decision"):
    self.assertIs(permit(name,{"text":SECRET}),Decision.ALLOW)
   self.assertEqual({x[0] for x in seen},{ACT,"mcp__jarvis__"+ACT})
   self.assertTrue(all(x[1]=={"text":SECRET} for x in seen))
 def test_private_broker_hud_only_no_always_cancel_and_timeout(self):
  broker=PendingApprovals(timeout_s=0.05)
  stop=threading.Event();seen=[]
  broker._on_request=lambda request:seen.append(request)
  permit,unused,name=self.permit(delegation_cancelled=lambda:stop.is_set)
  brief=Brief(Role.CHAT,"/tmp",mcp_servers={"jarvis":{}})
  permit=build_permit(PermitContext("chat",brief,provider="claude",delegation_cancelled=lambda:stop.is_set),broker)
  for cancel in (False,True):
   out=[];seen.clear();stop.clear()
   with patch("jarvis.v2.permissions.always_ask_match",return_value=("rule","fixed")),patch("jarvis.v2.permissions.log_decision"):
    worker=threading.Thread(target=lambda:out.append(permit(name,{"text":SECRET})));worker.start()
    T.eventually(lambda:bool(seen));req=seen[0]
    with self.assertRaises(ValueError):broker.resolve(req.req_id,"allow",hud=False)
    if cancel:stop.set()
    worker.join(2)
   self.assertFalse(worker.is_alive());self.assertEqual(out,[Decision.DENY]);self.assertEqual(broker.pending(),[])

 def test_delegation_hud_allow_cannot_create_always_rule(self):
  broker=PendingApprovals(timeout_s=1)
  broker._on_request=lambda req:broker.resolve(req.req_id,"allow",always=True,hud=True)
  brief=Brief(Role.CHAT,"/tmp",mcp_servers={"jarvis":{}})
  permit=build_permit(PermitContext("chat",brief,provider="claude"),broker)
  with patch("jarvis.v2.permissions.always_ask_match",return_value=("rule","fixed")),patch("jarvis.v2.approvals.permissions.add_allow") as add:
   self.assertIs(permit("mcp__jarvis__"+ACT,{"text":SECRET}),Decision.ALLOW)
   add.assert_not_called()
  self.assertEqual(broker.pending(),[])

class NativeClaude(F.DaemonHarness):
 held=T.LifecycleChecks.held
 hook=T.LifecycleChecks.hook
 finish=T.LifecycleChecks.finish
 def test_off_and_child_never_enter_permission_or_dispatch(self):
  thread,session,release=self.held()
  with patch.object(D,"_installed",None),patch.object(session,"permit") as permit,patch.object(tools,"dispatch") as dispatch:
   for extra in ({},{"agent_id":"child"}):
    decision=self.hook(thread,ACT,**extra)
    self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"],"deny")
   result=self.call(F.token_of(self.run_brief(thread)),ACT,{"action":"click","target":"e1"})[1]
   self.assertTrue(result["isError"]);self.assertIn(D.OFF,result["text"])
   permit.assert_not_called();dispatch.assert_not_called();self.assertEqual(self.d.approvals.pending(),[])
 def test_native_turn_lifecycle_stop_replacement_and_offdesk(self):
  thread,session,release=self.held();turn=session.drive_turn
  self.assertEqual((turn.provider,turn.owner_words,turn.turn_id),("claude",T.HUD.text,session.turn_id))
  cancelled=self.d._delegation_cancelled(thread.id);self.assertFalse(cancelled())
  self.d.interrupt(thread.id);self.assertTrue(turn.closed);self.assertIsNone(session.drive_turn);self.assertTrue(cancelled())
  self.finish(thread,release)
  _,session,release=self.held(T.REMOTE,thread)
  self.assertIsNot(session.drive_turn,turn);self.assertTrue(cancelled());self.assertFalse(session.desk.slot["present"])
  next_turn=session.drive_turn;self.finish(thread,release);self.assertTrue(next_turn.closed)
 def test_enabled_hook_delegates_explicitly_but_strict_refuses(self):
  thread,session,release=self.held()
  with patch.object(D,"_installed",SimpleNamespace(closed=False,enabled=lambda:True,state=S.DriveState())):
   self.assertEqual(self.hook(thread,ACT)["hookSpecificOutput"]["permissionDecision"],"allow")
   self.finish(thread,release)
   strict=replace(session.brief,profile=PermissionProfile.STRICT)
   permit=build_permit(PermitContext(thread.id,strict,provider="claude"),self.d.approvals)
   self.assertIs(permit("mcp__jarvis__"+ACT,{}),Decision.DENY)
 def test_delegation_wait_stops_with_exact_session_not_successor(self):
  thread,session,release=self.held();asked=threading.Event();out=[]
  self.d.approvals._on_request=lambda request:asked.set()
  with patch.object(D,"_installed",SimpleNamespace(closed=False,enabled=lambda:True,state=S.DriveState())),patch("jarvis.v2.permissions.always_ask_match",return_value=("rule","fixed")):
   worker=threading.Thread(target=lambda:out.append(self.hook(thread,ACT)));worker.start()
   self.assertTrue(asked.wait(2));self.d.interrupt(thread.id);worker.join(2)
  self.assertFalse(worker.is_alive());self.assertEqual(out[0]["hookSpecificOutput"]["permissionDecision"],"deny")
  self.assertEqual(self.d.approvals.pending(),[])
 def test_only_latest_accepted_desk_steer_changes_bounded_owner_words(self):
  self.claude.steer=lambda h,m:None
  thread,session,release=self.held(UserMessage("ORIGINAL "+"x"*600,desk=True,via="hud"))
  turn=session.drive_turn;self.assertEqual(len(turn.owner_words),500)
  self.assertEqual(self.d.deliver(thread.id,UserMessage("FIRST_DESK",desk=True,via="hud"))["status"],"steered")
  self.assertIn("FIRST_DESK",turn.owner_words)
  self.assertEqual(self.d.deliver(thread.id,UserMessage("LATEST_DESK "+"y"*600,desk=True,via="hud"))["status"],"steered")
  self.assertIn("ORIGINAL",turn.owner_words);self.assertIn("LATEST_DESK",turn.owner_words)
  self.assertNotIn("FIRST_DESK",turn.owner_words);self.assertLessEqual(len(turn.owner_words),500)
  before=turn.owner_words
  def refused(h,m):raise SteerRefused("fixture",before_write=True)
  self.claude.steer=refused
  self.assertEqual(self.d.deliver(thread.id,UserMessage("REFUSED_WORDS",desk=True,via="hud"))["status"],"queued")
  self.assertEqual(turn.owner_words,before);self.d.interrupt(thread.id)

 def test_offdesk_text_never_enters_native_classifier_owner_words(self):
  self.claude.steer=lambda h,m:None
  thread,session,release=self.held(T.REMOTE);turn=session.drive_turn
  self.assertEqual(turn.owner_words,"")
  self.d.deliver(thread.id,UserMessage("OFFDESK_HUD_STEER",desk=True,via="hud"))
  self.assertEqual(turn.owner_words,"");self.d.interrupt(thread.id)

 def test_real_stdio_cancel_withdraws_only_its_card_and_preserves_turn(self):
  thread,session,release=self.held();turn=session.drive_turn
  asked=threading.Event();settled=threading.Event();seen=[]
  self.d.approvals._on_request=lambda req:asked.set()
  def act(action):
   if action.text=="second":return D.ActResult("acted","fixture")
   stop=runtime.should_stop()
   request=ApprovalRequest(ACT,args={},hud_only=True,browser={"kind":"delegation","tool":ACT},max_wait_s=120)
   answer=self.d.approvals.ask(request,cancelled=stop)
   seen.append(answer);settled.set();return D.ActResult("stopped","fixture")
  service=SimpleNamespace(closed=False,enabled=lambda:True,act=act)
  entry=F.entry_of(self.run_brief(thread));env={"PATH":"/usr/bin:/bin","HOME":str(self.root),**entry["env"]}
  with patch.object(D,"_installed",service):
   proc=subprocess.Popen([entry["command"],*entry["args"]],cwd=self.root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
   def send(value):proc.stdin.write(json.dumps(value)+"\n");proc.stdin.flush()
   try:
    send({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":ACT,"arguments":{"action":"press","text":"Tab"}}})
    self.assertTrue(asked.wait(2))
    send({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}})
    self.assertTrue(settled.wait(3));self.assertEqual(seen,[Decision.DENY]);self.assertEqual(self.d.approvals.pending(),[])
    self.assertIs(session.drive_turn,turn);self.assertFalse(turn.closed);self.assertFalse(session.cancelled.is_set())
    send({"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":ACT,"arguments":{"action":"type","target":"e1","text":"second"}}})
    self.assertTrue(select.select([proc.stdout],[],[],3)[0]);got=json.loads(proc.stdout.readline())
    self.assertEqual(got["id"],8);self.assertFalse(got["result"]["isError"])
   finally:
    proc.stdin.close()
    try:proc.wait(3)
    except subprocess.TimeoutExpired:proc.kill();proc.wait(3)
    self.assertEqual(proc.stderr.read(),"");proc.stdout.close();proc.stderr.close()

 def test_view_and_act_share_exact_turn_across_real_relay_calls(self):
  thread,session,release=self.held();turn=session.drive_turn;seen=[]
  service=D.BrowserDrive(SimpleNamespace(),enabled=lambda:True)
  snapshot=G.pane()
  def view(*args):
   seen.append(S.current_turn())
   return L.LookResult(text="fixture",source="fixture",delivered=lambda:service.publish_look(snapshot,{"serial":"a"*32,"refs":[{"ref":"e1"}]}))
  def act(action):
   seen.append(S.current_turn());cap=service.state.capability(turn,snapshot,"e1")
   return D.ActResult("acted","synthetic action")
  service.act=act
  with patch.object(D,"_installed",service),patch.object(BV,"look_for_tool",side_effect=view):
   view_result,err=F.run_relay(F.entry_of(self.run_brief(thread)),[("hud_browser_view",{})],cwd=self.root)
   self.assertFalse(view_result[10]["result"]["isError"])
   results,err=F.run_relay(F.entry_of(self.run_brief(thread)),[(ACT,{"action":"click","target":"e1"})],cwd=self.root)
  self.assertEqual(seen,[turn,turn]);self.assertFalse(turn.closed)
  self.assertFalse(results[10]["result"]["isError"],results[10]["result"]);self.assertIsNone(S.current_turn())
  self.finish(thread,release);self.assertTrue(turn.closed)

 def test_stop_retires_refs_but_keeps_actual_inflight_reservation(self):
  thread,session,release=self.held();turn=session.drive_turn;service=D.BrowserDrive(Mock(),enabled=lambda:True)
  snap=G.pane();service.state.publish(turn,snap,"a"*32,["e1"]);lease=service.state.reserve(turn,snap.tab_id)
  with patch.object(D,"_installed",service):self.d.interrupt(thread.id)
  self.assertTrue(turn.closed);self.assertNotIn(turn.key,service.state.refs)
  other=replace(turn,thread_id="other",closed=False)
  with self.assertRaises(S.DriveStateRefused):service.state.reserve(other,snap.tab_id)
  service.state.finish(lease);service.state.reserve(other,snap.tab_id)
 def test_nonhud_steer_revokes_current_and_one_successor_without_reusing_refs(self):
  self.claude.steer=lambda h,m:None
  thread,session,release=self.held();first=session.drive_turn
  self.assertEqual(self.d.deliver(thread.id,T.REMOTE)["status"],"steered")
  self.assertTrue(self.d._delegation_cancelled(thread.id)())
  with patch.object(D,"_installed",SimpleNamespace(closed=False,enabled=lambda:True,state=S.DriveState(),act=Mock())) as service:
   result=self.call(F.token_of(self.run_brief(thread)),ACT,{"action":"click"})[1]
   self.assertTrue(result["isError"]);service.act.assert_not_called()
  self.finish(thread,release);self.assertTrue(first.closed)
  _,session,release=self.held(T.HUD,thread);second=session.drive_turn
  self.assertIsNot(second,first);self.assertFalse(session.desk.slot["present"])
  self.finish(thread,release)
  _,session,release=self.held(T.HUD,thread);self.assertTrue(session.desk.slot["present"])
 def test_provider_error_retires_before_the_provider_finishes(self):
  thread=self.chat();entered=threading.Event();release=threading.Event();captured=[]
  def send(handle,message):
   yield Event(K.TURN_STARTED,thread.id)
   captured.append(self.d._sessions[thread.id].drive_turn)
   yield Event(K.ERROR,thread.id,{"message":"synthetic failure","fatal":False})
   entered.set();release.wait(3)
   yield Event(K.TURN_FINISHED,thread.id,{"stop":"error"})
  with patch.object(self.claude,"send",side_effect=send):
   self.d.send(thread.id,T.HUD);self.assertTrue(entered.wait(2))
   self.assertTrue(captured[0].closed);self.assertIsNone(self.d._sessions[thread.id].drive_turn)
   release.set();self.wait_idle(thread.id)

 def test_error_close_and_shutdown_retire_native_turn(self):
  thread,session,release=self.held();turn=session.drive_turn
  release.set();self.d.close_thread(thread.id);self.assertTrue(turn.closed)
  thread,session,release=self.held();turn=session.drive_turn
  release.set();self.d.stop();self.assertTrue(turn.closed)
 def test_daemon_backstop_redacts_exact_native_names_before_label_and_bus(self):
  thread,session,release=self.held();self.drain()
  with patch("jarvis.v2.tool_calls.label",side_effect=AssertionError("private labels must not be derived")):
   for name in (ACT,"mcp__jarvis__"+ACT):
    for kind,data in ((K.TOOL_STARTED,{"name":name,"call_id":"private","args":{"text":SECRET}}),
                      (K.TOOL_FINISHED,{"name":name,"call_id":"private","summary":SECRET,"ok":False}),
                      (K.REVIEWER_DECLINED,{"tool":name,"args":{"text":SECRET},"command":SECRET,"reason":SECRET}),
                      (K.APPROVAL_REQUESTED,{"tool":name,"req_id":"private-gate","args":{"text":SECRET},"command":SECRET})):
     self.d._record(session,Event(kind,thread.id,data))
  rows=self.drain();self.assertNotIn(SECRET,json.dumps(rows))
  self.assertNotIn(SECRET,self.d.stores.threads.path(thread.id).with_name("log.jsonl").read_text())
  started=[x for x in rows if x.get("kind")==K.TOOL_STARTED.value]
  self.assertEqual(len(started),2);self.assertTrue(all(x["data"]["label"]=="Browser action" for x in started))

class NativeCodex(C.ForegroundBridge):
 def test_off_parent_and_invalid_child_no_permission_or_io(self):
  session=self.d._sessions[self.thread.id]
  with patch.object(D,"_installed",None),patch.object(session,"permit") as permit,patch.object(tools,"dispatch") as dispatch:
   for context in (None,C.context(thread_id="child"),C.context()):
    _,result=F.http(self.d.port,"POST","/mcp/call",token=self.token,body={"name":ACT,"arguments":{"action":"click"},"codex_context":context})
    self.assertTrue(result["isError"])
   permit.assert_not_called();dispatch.assert_not_called()
 def test_enabled_rpc_uses_native_identity_and_stopped_is_error(self):
  session=self.d._sessions[self.thread.id];seen=[]
  service=SimpleNamespace(closed=False,enabled=lambda:True,act=lambda action:(seen.append(S.current_turn()),D.ActResult("stopped",SECRET))[1])
  with patch.object(D,"_installed",service):
   for n in range(2):
    _,result=F.http(self.d.port,"POST","/mcp/call",token=self.token,body={"name":ACT,"arguments":{"action":"click"},"codex_context":C.context(call_id="native-"+str(n))})
    self.assertTrue(result["isError"]);self.assertTrue(result["text"].startswith("Stopped:"))
  self.assertEqual(seen,[session.drive_turn]*2);self.assertEqual(seen[0].provider,"codex")
  self.assertFalse(seen[0].closed)

 def test_drive_turn_replacement_during_native_permit_prevents_dispatch(self):
  session=self.d._sessions[self.thread.id];old=session.drive_turn
  def permit(*args,**kw):session.drive_turn=replace(old);return Decision.ALLOW
  with patch.object(D,"_installed",SimpleNamespace(closed=False,enabled=lambda:True)),patch.object(session,"permit",side_effect=permit),patch.object(tools,"dispatch",return_value=tools.ToolResult("Acted: fixture")) as dispatch:
   _,result=F.http(self.d.port,"POST","/mcp/call",token=self.token,body={"name":ACT,"arguments":{"action":"click"},"codex_context":C.context()})
   self.assertTrue(result["isError"]);dispatch.assert_not_called()

class Attribution(unittest.TestCase):
 setUp=G.Gate.setUp
 action=G.Gate.action
 def test_cards_audit_and_driver_provider_derive_only_from_bound_turn(self):
  self.judge.return_value=A.ActVerdict("ask",R.ORDINARY,"unused","model")
  for provider in ("fast","claude","codex"):
   self.turn.provider=provider;runtime.bind_caller(self.turn.thread_id,"project",provider,self.turn.turn_id)
   self.service.state.publish(self.turn,self.snap,"a"*32,["e1"])
   self.assertEqual(self.action("click").outcome,"acted")
   self.assertEqual(self.approvals.ask.call_args.args[0].provider,provider)
   self.assertTrue(all(x.kwargs["provider"]==provider for x in self.host.drive_notice.call_args_list[-2:]))
   self.assertEqual(self.audit.call_args.args[0].provider,provider)
   tab=SimpleNamespace(stream=Mock(),page_identity=1,document_generation=1);host=SimpleNamespace(require_tab=lambda _:tab)
   BrowserHost.drive_notice(host,"tab","thread","act","click","running",self.prepared,"",provider=provider)
   tab.stream.agent_driver.assert_called_once_with("thread",provider)
  with self.assertRaises(ValueError):BrowserHost.drive_notice(host,"tab","thread","act","click","running",self.prepared,"",provider="foreign")
 def test_real_registry_and_broker_distinguish_own_card_from_settings(self):
  self.judge.return_value=A.ActVerdict("ask",R.ORDINARY,"unused","model")
  host=self.host;host.pane_tab.side_effect=lambda _:dict(tab_id=P.T,profile=P.P,ctx=None,url=self.snap.url,page_identity=1,document_generation=1)
  host.input_held.return_value=False
  registry=BrowserPanes(SimpleNamespace(browser=host,devservers=SimpleNamespace(source_snapshot=lambda _:None)))
  registry.window_started(P.W)
  body=P.report();body["panes"][0]["tabs"]=[{"id":P.L,"mode":"remote","tab_id":P.T,"source":None}]
  registry.report(body)
  original=registry._snapshot
  # Test-only drive bit; real ownership/source/visibility/generation code stays live.
  registry._snapshot=lambda *args:replace(original(*args),drive=True)
  self.daemon.browser_panes=registry;self.snap=registry.resolve("1")
  for change in ("own","settings","other","takeover"):
   body["seq"]+=1;body["focus_seq"]+=1;body["covered"]=False;registry.report(body)
   self.snap=registry.resolve("1");self.service.state.publish(self.turn,self.snap,"a"*32,["e1"])
   self.state.update(held=False,hold_count=0,input_serial=0)
   seen=[];broker=PendingApprovals(timeout_s=1);self.daemon.approvals=broker
   checked=threading.Event();other_ready=threading.Event();cancel_other=threading.Event();workers=[]
   original_cancelled=self.service._cancelled
   def cancelled(guard,request):
    result=original_cancelled(guard,request)
    if change=="own" and request in broker.pending():
     self.assertFalse(result);checked.set()
    return result
   def asked(request):
    if request.tool=="other":other_ready.set();return
    self.state.update(held=True,hold_count=1);seen.append(request)
    if change=="settings":
     body["seq"]+=1;body["covered"]=True;registry.report(body)
    if change=="takeover":self.state["input_serial"]+=1
    if change=="other":
     other=threading.Thread(target=lambda:broker.ask(ApprovalRequest("other"),cancelled=cancel_other.is_set))
     workers.append(other);other.start();self.assertTrue(other_ready.wait(1))
    if change=="own":
     self.assertTrue(registry.still_current(self.snap))
     def answer():
      if checked.wait(1):
       self.state["held"]=False;broker.resolve(request.req_id,"allow",hud=True)
     worker=threading.Thread(target=answer);workers.append(worker);worker.start()
   broker._on_request=asked
   with patch.object(self.service,"_cancelled",side_effect=cancelled):result=self.action("click")
   cancel_other.set()
   for worker in workers:worker.join(2);self.assertFalse(worker.is_alive())
   if change=="own":self.assertTrue(checked.is_set())
   self.assertEqual(result.outcome,"acted" if change=="own" else "refused",(change,result.text,len(seen)))
   self.assertEqual(len(seen),1,(change,result.text));self.assertEqual(self.host.execute_action.call_count,1);broker.shutdown()

class DownloadAttribution(unittest.TestCase):
 setUp=DD.Downloads.setUp
 saved=DD.Downloads.saved
 def test_native_download_card_and_audit_keep_exact_provider(self):
  for provider in ("fast","claude","codex"):
   self.guard.turn.provider=provider
   token,row=self.saved();self.manager.review(token)
   self.assertEqual(self.approvals.ask.call_args.args[0].provider,provider)
   self.assertEqual(self.audit.call_args.args[0].provider,provider)
  self.manager.stop()

class Privacy(unittest.TestCase):
 def test_claude_gate_events_hide_args_while_permit_race_receives_original(self):
  provider=claude.ClaudeProvider()
  for name in (ACT,"mcp__jarvis__"+ACT,"mcp__foreign__"+ACT):
   emitted=[];captured=[]
   session=SimpleNamespace(mutex=threading.Lock(),pending={},emit=lambda kind,**data:emitted.append((kind,data)))
   async def race(s,pending,tool,args):captured.append(args);return Decision.DENY
   with patch.object(provider,"_race",side_effect=race):
    self.assertIs(asyncio.run(provider._decide(session,name,{"text":SECRET})),Decision.DENY)
   self.assertEqual(captured,[{"text":SECRET}])
   if name.startswith("mcp__foreign__"):
    self.assertEqual(emitted[0][1]["args"],{"text":SECRET})
   else:
    self.assertEqual(emitted[0][1]["args"],{});self.assertIsNone(emitted[0][1]["command"])
    self.assertNotIn(SECRET,str(emitted))
   self.assertEqual(session.pending,{})
 def test_claude_start_result_reviewer_and_foreign_exactness(self):
  p=claude.ClaudeProvider()
  for name in (ACT,"mcp__jarvis__"+ACT):
   s=SimpleNamespace(thread_id="chat",tools_seen={},denied_tools=set(),denial_without_id=False)
   msg=claude.AssistantMessage(content=[claude.ToolUseBlock(id="c",name=name,input={"text":SECRET})],model="fixture")
   events=p._translate(s,msg)
   self.assertEqual(events[0].data["args"],{})
   for text,error,want in (("Acted: "+SECRET,False,"acted"),("Stopped: "+SECRET,True,"stopped"),("interrupted "+SECRET,True,"refused"),("The user declined "+SECRET,True,"refused"),(claude.CLASSIFIER_DENIAL_PREFIX+" reason: "+SECRET,True,"refused")):
    got=p._tool_result(s,claude.ToolResultBlock(tool_use_id="c",content=text,is_error=error))
    self.assertEqual(got[-1].data["summary"],want);self.assertNotIn(SECRET,json.dumps([e.data for e in got]))
  self.assertEqual(p._tool_summary("mcp__foreign__"+ACT,SECRET),SECRET)
 def test_codex_start_result_unmatched_review_and_unfinished(self):
  p=codex.CodexProvider();h=C.native_handle()
  for i,text in enumerate(("Acted: "+SECRET,"Stopped: "+SECRET)):
   item={"id":str(i),"type":"mcpToolCall","server":"jarvis","tool":ACT,"arguments":{"text":SECRET},"status":"completed","result":{"content":[{"type":"text","text":text}]}}
   events=list(p._notification(h,"item/completed",{"threadId":"native-parent","turnId":"native-turn","item":item}))
   self.assertEqual(events[0].data["args"],{});self.assertNotIn(SECRET,json.dumps([e.data for e in events]))
   self.assertEqual(events[-1].data["ok"],i==0)
  params={"threadId":"native-parent","turnId":"native-turn","targetItemId":None,"action":{"type":"mcpToolCall","server":"jarvis","toolName":ACT,"arguments":{"text":SECRET}},"review":{"status":"denied","rationale":SECRET}}
  got=list(p._notification(h,"item/autoApprovalReview/completed",params))
  self.assertEqual(got[0].data,{"tool":ACT,"args":{},"command":None,"reason":"refused"})
  item={"id":"unfinished","type":"mcpToolCall","server":"jarvis","tool":ACT,"arguments":{"text":SECRET},"status":"inProgress"}
  started=list(p._notification(h,"item/started",{"threadId":"native-parent","turnId":"native-turn","item":item}))
  ended=list(p._mcp_unfinished(h))
  self.assertEqual(started[0].data["args"],{});self.assertEqual(ended[0].data["summary"],"stopped")
  self.assertNotIn(SECRET,json.dumps([e.data for e in started+ended]))

if __name__=="__main__":unittest.main()

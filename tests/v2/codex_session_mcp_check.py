"""Codex session MCP: exact delegation, native foreground identity, private events."""
from __future__ import annotations
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import mcp_session_check as F
from jarvis import tools
from jarvis.v2 import mcp_session, session_tools, bridge
from jarvis.v2.model import PermissionProfile as P, ProviderName as N, Role
from jarvis.v2.provider import Decision as D, EventKind as K, SessionHandle
from jarvis.v2.permissions import PermitContext, build_permit
from jarvis.v2.providers import codex, codex_config
from jarvis.v2.providers.codex_rpc import RpcError
setUpModule=F.setUpModule
tearDownModule=F.tearDownModule


def native_handle():
    native=SimpleNamespace(thread_id="native-parent",turn_id="native-turn",mcp_calls=set(),mcp_items={},items={},
                           mutex=threading.RLock(),closed=threading.Event(),cancelled=threading.Event(),sending=threading.Lock())
    native.sending.acquire()
    return SessionHandle("chat",N.CODEX,"codex:native-parent",native)


def context(**kw):
    return {"thread_id":"native-parent","turn_id":"native-turn","call_id":"call-1","subagent":False,**kw}


class Identity(unittest.TestCase):
    def setUp(self):
        self.provider=codex.CodexProvider();self.h=native_handle()

    def test_parent_ticket_is_current_then_revoked_by_turn_or_handle(self):
        ticket=self.provider.admit_foreground_mcp(self.h,context())
        self.assertTrue(ticket())
        self.h.native.turn_id="successor"
        self.assertFalse(ticket())
        self.h.native.turn_id="native-turn"
        self.h.native=native_handle().native
        self.assertFalse(ticket())

    def test_child_missing_malformed_markers_and_untrusted_first_call_deny(self):
        for value in (None,{},context(thread_id="child"),context(turn_id="old"),context(subagent=True),
                      context(subagent=0),context(call_id=""),context(call_id="x"*129),{**context(),"extra":1}):
            with self.subTest(value=value):self.assertIsNone(self.provider.admit_foreground_mcp(self.h,value))
        self.h.native.turn_id=None
        self.assertIsNone(self.provider.admit_foreground_mcp(self.h,context()))
        self.assertIsNone(self.h.native.turn_id)

    def test_concurrent_replay_is_one_shot_and_cap_does_not_reset_on_completion(self):
        answers=[]
        workers=[threading.Thread(target=lambda:answers.append(self.provider.admit_foreground_mcp(self.h,context()))) for _ in range(12)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertEqual(sum(callable(x) for x in answers),1)
        self.h.native.mcp_calls.update(str(i) for i in range(4096))
        self.assertIsNone(self.provider.admit_foreground_mcp(self.h,context(call_id="fresh")))
        self.h.native.mcp_items.clear()
        self.assertIsNone(self.provider.admit_foreground_mcp(self.h,context(call_id="fresh")))

    def test_stop_close_and_inactive_send_revoke(self):
        for field in ("closed","cancelled","sending"):
            h=native_handle();ticket=self.provider.admit_foreground_mcp(h,context())
            getattr(h.native,field).release() if field=="sending" else getattr(h.native,field).set()
            self.assertFalse(ticket())
            self.assertIsNone(self.provider.admit_foreground_mcp(h,context(call_id="new")))

    def test_outer_context_never_comes_from_arguments(self):
        meta={"threadId":"native-parent","callId":"call-1","x-codex-turn-metadata":{"thread_id":"native-parent","turn_id":"native-turn"}}
        params={"_meta":meta,"arguments":{"_meta":{"threadId":"forged"},"threadId":"forged"}}
        self.assertEqual(mcp_session._codex_context(params),context())
        self.assertIsNone(mcp_session._codex_context({"arguments":{"_meta":meta}}))
        self.assertIsNone(mcp_session._codex_context({"_meta":{**meta,"threadId":"conflict"}}))
        for marker in ("parent_thread_id","subagent_kind","thread_source"):
            child={**meta,"x-codex-turn-metadata":{**meta["x-codex-turn-metadata"],marker:None}}
            self.assertIs(mcp_session._codex_context({"_meta":child})["subagent"],True)


class Events(unittest.TestCase):
    def setUp(self):self.provider=codex.CodexProvider();self.h=native_handle()
    def item(self,**kw):
        return {"id":"call","type":"mcpToolCall","server":"jarvis","tool":"terminal_read","arguments":{"lines":3},
                "status":"completed","result":{"content":[{"type":"text","text":"HUD terminal fixture, its last 3 line(s)\nPRIVATE_MARKER"}]},**kw}
    def emit(self,item=None,method="item/completed",**kw):
        return list(self.provider._notification(self.h,method,{"threadId":"native-parent","turnId":"native-turn","item":item or self.item(),**kw}))
    def test_terminal_fixed_summary_and_duplicate_lifecycle(self):
        events=self.emit(method="item/started")+self.emit()+self.emit()
        self.assertEqual([x.kind for x in events],[K.TOOL_STARTED,K.TOOL_FINISHED])
        self.assertEqual(events[-1].data["summary"],"read 3 lines")
        self.assertNotIn("PRIVATE_MARKER",json.dumps([x.data for x in events]))
        self.assertEqual(self.h.native.items,{})
        self.assertNotIn("PRIVATE_MARKER",str(self.h.native.mcp_items))
    def test_refusal_transport_error_and_metadata_never_copy_text(self):
        cases=[("Refused: PRIVATE_MARKER","completed",None,"refused","denied"),
               ("The user declined PRIVATE_MARKER","completed",None,"refused","denied"),
               ("Error: PRIVATE_MARKER","completed",None,"error","failed"),
               ("PRIVATE_MARKER","failed",{"message":"PRIVATE_MARKER"},"error","failed")]
        for i,(text,status,error,summary,outcome) in enumerate(cases):
            row=self.item(id=str(i),status=status,error=error,result={"content":[{"type":"text","text":text}],"structuredContent":{"secret":"PRIVATE_MARKER"},"_meta":{"private":"PRIVATE_MARKER"}})
            got=self.emit(row)[-1]
            self.assertEqual((got.data["summary"],got.data["outcome"]),(summary,outcome))
            self.assertNotIn("PRIVATE_MARKER",json.dumps(got.data))
    def test_malformed_terminal_results_report_fixed_error(self):
        for index, result in enumerate((None, {}, {"content": None}, {"content": "wrong"},
                                        {"content": []}, {"content": [{"type": "image", "data": "PRIVATE_MARKER"}]},
                                        {"content": [{"type": "text", "text": 17}]})):
            got = self.emit(self.item(id="malformed-" + str(index), result=result))[-1]
            self.assertEqual((got.data["summary"], got.data["outcome"]), ("error", "failed"))
            self.assertNotIn("PRIVATE_MARKER", json.dumps(got.data))
    def test_stale_wrong_thread_and_identity_change(self):
        self.assertEqual(self.emit(turnId="old"),[])
        self.assertEqual(self.emit(threadId="child"),[])
        self.emit(method="item/started")
        with self.assertRaises(RpcError):self.emit(self.item(tool="memory_read"))
    def test_unfinished_pair_is_fixed_and_finished_once(self):
        self.emit(method="item/started")
        got=list(self.provider._mcp_unfinished(self.h))
        self.assertEqual(got[0].data["summary"],"error")
        self.assertEqual(list(self.provider._mcp_unfinished(self.h)),[])
        self.assertEqual(self.emit(),[])
    def test_ordinary_results_keep_short_summary_and_foreign_name_is_distinct(self):
        text="ordinary"*50
        got=self.emit(self.item(tool="memory_read",result={"content":[{"type":"text","text":text}]}))[-1]
        self.assertEqual(got.data["summary"],text[:200])
        got=self.emit(self.item(id="other",server="another"))
        self.assertEqual(got[-1].data["name"],"mcp__another__terminal_read")
    def test_reviewer_refusal_cannot_copy_terminal_rationale(self):
        self.emit(method="item/started")
        events=list(self.provider._notification(self.h,"item/autoApprovalReview/completed",{
            "threadId":"native-parent","turnId":"native-turn",
            "targetItemId":"call","review":{"status":"denied","rationale":"PRIVATE_MARKER"}}))
        self.assertEqual(events[0].data["reason"],"refused")
        self.assertNotIn("PRIVATE_MARKER",json.dumps(events[0].data))


    def test_early_unmatched_mcp_reviews_never_copy_private_fields(self):
        for target in (None, "unknown"):
            for action in ({"type": "mcpToolCall", "server": "jarvis", "toolName": "terminal_read",
                            "toolTitle": "PRIVATE_MARKER"},
                           {"type": "mcpToolCall", "server": "PRIVATE_MARKER", "toolName": "PRIVATE_MARKER"},
                           {"type": "mcpToolCall"}):
                params = {"threadId": "native-parent", "turnId": "native-turn", "targetItemId": target,
                          "action": action, "review": {"status": "denied", "rationale": "PRIVATE_MARKER"}}
                events = list(self.provider._notification(self.h, "item/autoApprovalReview/completed", params))
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].data["reason"], "refused")
                self.assertEqual(events[0].data["args"], {})
                self.assertNotIn("PRIVATE_MARKER", json.dumps(events[0].data))
                for field, value in (("threadId", "child"), ("turnId", "old"), ("turnId", None)):
                    self.assertEqual(list(self.provider._notification(self.h,
                        "item/autoApprovalReview/completed", {**params, field: value})), [])
        self.emit(method="item/started")
        for action in (None, [], "malformed"):
            params = {"threadId": "native-parent", "turnId": "native-turn", "targetItemId": "call",
                      "action": action, "review": {"status": "denied", "rationale": "PRIVATE_MARKER"}}
            event = list(self.provider._notification(self.h, "item/autoApprovalReview/completed", params))[0]
            self.assertEqual(event.data["tool"], "terminal_read")
            self.assertNotIn("PRIVATE_MARKER", json.dumps(event.data))

    def test_ordinary_command_review_keeps_its_existing_explanation(self):
        params = {"threadId": "native-parent", "turnId": "native-turn", "targetItemId": None,
                  "action": {"type": "command", "command": "echo synthetic", "cwd": "/tmp"},
                  "review": {"status": "denied", "rationale": "Ordinary command reason"}}
        event = list(self.provider._notification(self.h, "item/autoApprovalReview/completed", params))[0]
        self.assertEqual(event.data["reason"], "Ordinary command reason")
        self.assertEqual(event.data["command"], "echo synthetic")



class Gates(F.GateChecks):
    # Reuse only fixture helpers; inherited Claude tests also verify parity.
    def test_codex_delegates_exact_read_and_write_but_obeys_strict_and_named_rules(self):
        for profile in (P.ASK,P.AUTO,P.STRICT):
            for name in (F.READ,F.SEND):
                brief=self.brief(profile);asker=F.Asker(D.DENY)
                permit=build_permit(PermitContext("abcd1234",brief,provider="codex"),asker)
                self.assertEqual(permit(name,{}),D.DENY if profile==P.STRICT else D.ALLOW)
                self.assertEqual(asker.seen,[])
        for rule in (F.READ,session_tools.CLAUDE_PREFIX+F.READ):
            asker=F.Asker(D.DENY);brief=self.brief(P.AUTO,always_ask=[rule])
            self.assertEqual(build_permit(PermitContext("abcd1234",brief,provider="codex"),asker)(F.READ,{}),D.DENY)
            self.assertEqual(len(asker.seen),1)
    def test_codex_delegation_requires_provider_chat_server_and_canonical_name(self):
        for brief,provider,name in [(self.brief(P.ASK),"claude",F.READ),(self.brief(P.ASK,role=Role.IMPLEMENTER,task_id="task"),"codex",F.READ),
                                   (self.brief(P.ASK,task_id="task"),"codex",F.READ),(self.brief(P.ASK,mcp_servers={}),"codex",F.READ),
                                   (self.brief(P.ASK),"codex","mcp__another__"+F.READ)]:
            asker=F.Asker(D.DENY)
            self.assertEqual(build_permit(PermitContext("abcd1234",brief,provider=provider),asker)(name,{}),D.DENY)

    def test_codex_dangerous_execution_is_one_owner_gate_never_always(self):
        for profile in (P.AUTO, P.ASK, P.STRICT):
            for decision in (D.ALLOW, D.DENY):
                asker = F.Asker(decision)
                brief = self.brief(profile, always_ask=[session_tools.CLAUDE_PREFIX + F.SEND])
                permit = build_permit(PermitContext("abcd1234", brief, provider="codex"), asker)
                actual = permit(F.SEND, {"to": "fixture"}, None, dangerous_tool=True)
                self.assertEqual(actual, D.DENY if profile == P.STRICT else decision)
                self.assertEqual(len(asker.seen), 0 if profile == P.STRICT else 1)
                if asker.seen:
                    self.assertFalse(asker.seen[0].allowlistable)
        brief = self.brief(P.AUTO)
        asker = F.Asker(D.ALLOW)
        permit = build_permit(PermitContext("abcd1234", brief, provider="codex"), asker)
        target = str(self.root / "creds" / ".ssh")
        for name, dangerous in ((F.READ, False), (F.SEND, True)):
            self.assertEqual(permit(name, {}, None, grants=[("read", target)],
                                    dangerous_tool=dangerous), D.DENY)
        self.assertEqual(asker.seen, [])


class BridgeGate(F.DaemonHarness):
    def test_non_dangerous_codex_preflight_denial_does_not_dispatch(self):
        thread=self.chat(N.CODEX);session=self.d._sessions[thread.id]
        session.permit=lambda *a,**kw:D.DENY
        status,body=self.call(F.token_of(self.run_brief(thread)))
        self.assertEqual(status,200);self.assertTrue(body["isError"]);self.assertEqual(F.CALLS,[])
    def test_owner_yes_after_session_replacement_does_not_dispatch(self):
        thread=self.chat(N.CODEX);session=self.d._sessions[thread.id]
        def permit(*a,**kw):
            session.cancelled.set();return D.ALLOW
        session.permit=permit
        _,body=self.call(F.token_of(self.run_brief(thread)))
        self.assertTrue(body["isError"]);self.assertEqual(F.CALLS,[])


class NativeConfig(F.CodexProviderChecks):
    def test_measured_mcp_readback_refuses_missing_changed_and_disabled(self):
        expected=self.brief.mcp_servers["jarvis"]
        for wrong in (None,{}, {**expected,"enabled":False}, {**expected,"tools":{}},
                      {**expected,"env":{}}, {**expected,"disabled_tools":[F.READ]}):
            with self.subTest(wrong=wrong):
                self.brain.script['config_override']={'mcp_servers':{} if wrong is None else {'jarvis':wrong}}
                with self.assertRaises(F.BriefRefused):self.start(thread_id='mcpbad'+str(len(self.brain.rpcs)))
        self.brain.script.clear()
    def test_start_and_resume_override_exact_current_session_server(self):
        h=self.start(thread_id='mcpoverride')
        self.assertEqual(self.brain.calls('thread/start')[-1]['params']['config']['mcp_servers.jarvis'],self.brief.mcp_servers['jarvis'])
        list(self.provider.send(h,F.UserMessage('go')))
        self.provider.close(h)
        thread=F.Thread('mcpoverride','p',Role.CHAT,N.CODEX,provider_session_id=h.provider_session_id)
        resumed=self.provider.resume(thread,self.brief,lambda *a,**kw:D.ALLOW)
        self.handles.append(resumed)
        self.assertEqual(self.brain.calls('thread/resume')[-1]['params']['config']['mcp_servers.jarvis'],self.brief.mcp_servers['jarvis'])


class ForegroundBridge(F.DaemonHarness):
    def setUp(self):
        super().setUp()
        from jarvis.v2.tools import terminal_read as TR
        self.TR=TR;self.reads=[]
        self.stack.enter_context(patch.dict(session_tools.SESSION_TOOLS, {'terminal_read':session_tools.SessionTool('terminal_read',read_only=True,foreground_only=True,desk_only=True)}))
        self.native=native_handle();provider=codex.CodexProvider()
        self.codex.admit_foreground_mcp=lambda handle,ctx:provider.admit_foreground_mcp(self.native,ctx)
        self.thread=self.chat(N.CODEX);self.release=threading.Event();self.entered=threading.Event()
        def run(handle,message):self.entered.set();self.release.wait(8)
        self.codex.script[self.thread.id]=run
        self.d.send(self.thread.id,F.UserMessage('go',desk=True,via='hud'))
        self.assertTrue(self.entered.wait(2));self.addCleanup(self.release.set)
        self.token=F.token_of(self.run_brief(self.thread))
        from jarvis.v2.terminals import ToolRead
        def read(*args):self.reads.append(args);return ToolRead('t','fixture',text='PRIVATE_MARKER',lines=1)
        self.stack.enter_context(patch.object(TR,'read_for_tool',side_effect=read))
    def terminal(self,ctx=None):
        return F.http(self.d.port,'POST','/mcp/call',token=self.token,body={'name':'terminal_read','arguments':{'terminal':'fixture'},'codex_context':ctx})[1]
    def test_only_exact_parent_reads_and_replay_child_missing_do_not(self):
        self.assertTrue(self.terminal()['isError'])
        self.assertTrue(self.terminal(context(thread_id='child'))['isError'])
        self.assertTrue(self.terminal(context(subagent=True))['isError'])
        self.assertEqual(self.reads,[])
        self.assertIn('PRIVATE_MARKER',self.terminal(context())['text'])
        self.assertTrue(self.terminal(context())['isError'])
        self.assertEqual(len(self.reads),1)
    def test_native_turn_advancing_during_permission_wait_cannot_read(self):
        entered=threading.Event();release=threading.Event();got=[]
        def permit(*a,**kw):entered.set();release.wait(3);return D.ALLOW
        self.d._sessions[self.thread.id].permit=permit
        worker=threading.Thread(target=lambda:got.append(self.terminal(context())))
        worker.start();self.assertTrue(entered.wait(2));self.native.native.turn_id='successor';release.set();worker.join(3)
        self.assertTrue(got[0]['isError']);self.assertEqual(self.reads,[])
    def test_native_turn_advancing_during_guarded_read_withholds_result(self):
        from jarvis.v2.terminals import ToolRead
        def read(*args):self.native.native.turn_id='successor';return ToolRead('t','fixture',text='PRIVATE_MARKER',lines=1)
        with patch.object(self.TR,'read_for_tool',side_effect=read):got=self.terminal(context())
        self.assertTrue(got['isError']);self.assertNotIn('PRIVATE_MARKER',got['text'])
    def test_chat_git_tools_are_codex_foreground_only_at_the_bridge(self):
        """CG-0: the git tools reach the gate only from the exact foreground
        Codex turn; a sub-agent's or a stale call is refused before the
        owner is asked, and the owner's gate is the dangerous-tool one."""
        from jarvis.v2.tools import chat_git_tools  # noqa: F401 — registers the tools
        # The module setup empties the catalog; put back exactly the production flags.
        self.stack.enter_context(patch.dict(session_tools.SESSION_TOOLS, {
            n:session_tools.SessionTool(n,providers=frozenset({'codex'}),foreground_only=True)
            for n in session_tools.CHAT_GIT_TOOLS}))
        calls=[]
        def permit(*a,**kw):calls.append((a,kw));return D.DENY
        self.d._sessions[self.thread.id].permit=permit
        def push(ctx):
            return F.http(self.d.port,'POST','/mcp/call',token=self.token,body={'name':'chat_git_push','arguments':{'worktree':'/nonexistent'},'codex_context':ctx})[1]
        for ctx in (None,context(thread_id='child'),context(subagent=True)):
            body=push(ctx)
            self.assertTrue(body['isError']);self.assertIn('foreground chat turn',body['text'])
        self.assertEqual(calls,[])
        body=push(context())
        self.assertTrue(body['isError']);self.assertTrue(body['text'].startswith('The user declined'))
        self.assertEqual(len(calls),1);self.assertIs(calls[0][1].get('dangerous_tool'),True)
        self.assertEqual(calls[0][0][0],'chat_git_push')
        listed=F.http(self.d.port,'GET','/mcp/tools',token=self.token)[1]
        names={t['name'] for t in listed['tools']} if isinstance(listed,dict) else {t['name'] for t in listed}
        self.assertTrue({'chat_git_push','chat_pr_create','chat_pr_merge','chat_pr_close','chat_git_fetch'}<=names)
    def test_child_nonforeground_tool_stays_available(self):
        _,body=F.http(self.d.port,'POST','/mcp/call',token=self.token,body={'name':F.READ,'arguments':{'note':'child'},'codex_context':context(thread_id='child',subagent=True)})
        self.assertIn('PROBE-MARKER child',body['text'])


# A real relay process and fake-shell PTY exercise the whole guarded read path;
# the native provider identity verifier is real, while the model loop is fake.
import terminal_read_check as R
from terminal_check import eventually


class RealTerminal(R.ReadBase):
    def test_codex_relay_uses_native_admission_and_real_guarded_pty(self):
        self.enterContext(patch.dict(session_tools.SESSION_TOOLS, {
            "terminal_read": session_tools.SessionTool(
                "terminal_read", read_only=True, foreground_only=True, desk_only=True)}))
        fake = F.FakeCli(N.CODEX)
        native = native_handle()
        provider = codex.CodexProvider()
        fake.admit_foreground_mcp = lambda handle, ctx: provider.admit_foreground_mcp(native, ctx)
        self.daemon.providers[N.CODEX] = fake
        self.daemon.permit_factory = self.daemon._permit
        thread = self.daemon.open_thread(self.project.id, Role.CHAT, N.CODEX, {})
        tid, client = self.session()
        marker = "WP3_PRIVATE_PTY_MARKER"
        self.run_line(client, "echo " + marker, "\n" + marker)
        self.settle(tid)
        self.reads()
        results = []
        serial = 0

        def action(handle, message):
            nonlocal serial
            serial += 1
            meta = {"threadId": "native-parent", "callId": "pty-call-" + str(serial),
                    "x-codex-turn-metadata": {"thread_id": "native-parent", "turn_id": "native-turn"}}
            got, err = F.run_relay(F.entry_of(fake.opened[-1].brief),
                [("terminal_read", {"terminal": tid})], cwd=self.root, call_meta=meta)
            results.append(got[10]["result"]["content"][0]["text"])
            self.assertNotIn(marker, err)

        fake.script[thread.id] = action

        def send():
            self.daemon.send(thread.id, F.UserMessage("read", desk=True, via="hud"))
            eventually(lambda: self.daemon._sessions[thread.id].worker is None)
            self.assertTrue(results)
            return results.pop()

        self.assertIn(marker, send())
        events = self.reads()
        self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]["data"]), {"terminal_id", "lines", "at", "refused"})
        self.assertFalse(events[0]["data"]["refused"])
        self.assertNotIn(marker, json.dumps(events))
        self.owner("PATCH", f"/terminals/{tid}", {"readable": False})
        self.assertTrue(send().startswith("Refused: the owner"))
        self.assertTrue(self.reads()[0]["data"]["refused"])
        self.owner("PATCH", f"/terminals/{tid}", {"readable": True})
        self.run_line(client, "cat .env", "command not found")
        self.settle(tid)
        self.assertTrue(send().startswith("Refused:"))
        self.assertTrue(self.reads()[0]["data"]["refused"])
        self.assertNotIn(marker, json.dumps(self.stores.threads.read_log(thread.id)))


if __name__=="__main__":unittest.main(verbosity=2)

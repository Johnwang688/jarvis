"""BR9b actual gate with scripted IO/reviewer/broker, zero real providers."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import runtime
from jarvis.v2.browser import drive as D, act_rules as R, act_review as A
from jarvis.v2.browser.drive_state import turn_scope
from jarvis.v2.browser.drive_scan import prepare_result
from jarvis.v2.provider import Decision
from tests.v2.browser_drive_state_check import pane,turn
from tests.v2.browser_drive_scan_check import row

class Gate(unittest.TestCase):
    def setUp(self):
        self.saved=runtime.caller();self.turn=turn();self.snap=pane();self.desk={"present":True,"images":False}
        self.runtime=runtime.turn_scope(desk=self.desk);self.runtime.__enter__()
        self.addCleanup(self.runtime.__exit__,None,None,None)
        runtime.bind_caller(self.turn.thread_id,"project","fast",self.turn.turn_id)
        self.addCleanup(runtime._CALLER.set,self.saved)
        self.state={"paused":False,"shown":True,"visible":True,"held":False,"owner_quiet_s":None,
                    "input_serial":0,"attachment_generation":1,"hold_count":0,"depressed":False,"input_busy":False}
        self.host=Mock()
        self.host.owner_state.side_effect=lambda _:dict(self.state)
        self.host.refused_ports.return_value={8402,8403,8405}
        self.host.loopback_open.return_value=True
        self.prepared=prepare_result(row(),"e1",())
        self.host.prepare_action.side_effect=lambda *a,**k:self.prepared
        self.host.execute_action.return_value=D.ActResult("acted","Done")
        self.registry=Mock();self.registry.resolve.return_value=self.snap
        self.registry.still_current.return_value=True
        self.registry.after_navigation.return_value=self.snap
        self.approvals=Mock(human_backed=True);self.approvals.pending.return_value=[]
        self.approvals.ask.return_value=Decision.ALLOW
        self.daemon=SimpleNamespace(browser=self.host,browser_panes=self.registry,approvals=self.approvals,
            terminals=Mock(),bus=Mock())
        self.daemon.terminals.readable_source.return_value=True
        self.service=D.BrowserDrive(self.daemon,enabled=lambda:True)
        self.scope=turn_scope(self.turn,self.service.state.retire);self.scope.__enter__()
        self.addCleanup(self.scope.__exit__,None,None,None)
        self.service.state.publish(self.turn,self.snap,"a"*32,["e1"])
        self.secrets=patch.object(D.secrets,"secret_values",return_value=[]);self.secrets.start();self.addCleanup(self.secrets.stop)
        self.judge=patch.object(A,"judge",return_value=A.ActVerdict("allow",R.ORDINARY,"page-model-marker","model")).start()
        self.addCleanup(patch.stopall)
        self.audit=patch("jarvis.v2.permissions.log_decision").start()

    def action(self,action="type",**kw):
        return self.service.act(D.Action(action,target=kw.pop("target","e1" if action in ("type","click","upload","select") else ""),text=kw.pop("text","query" if action=="type" else ""),**kw))

    def refused_without_io(self):
        result=self.action();self.assertEqual(result.outcome,"refused")
        self.host.prepare_action.assert_not_called();self.host.execute_action.assert_not_called()
        self.judge.assert_not_called();self.approvals.ask.assert_not_called()
        return result

    def test_host_activity_spans_prepare_review_execute_and_final_notice(self):
        from jarvis.v2.browser.activity import BrowserActivity
        gate=BrowserActivity();gate.opened(self.snap.profile,1);gate.desired(1)
        self.state['owner_quiet_s']=5.0
        self.host.begin_activity.side_effect=lambda *args:gate.enter(self.snap.profile,1)
        self.host.end_activity.side_effect=gate.leave
        stages=[]
        def current(stage,value):
            self.assertEqual(gate.counts(self.snap.profile)[0],1)
            self.assertIsNone(gate.try_restart(self.snap.profile,1,1))
            stages.append(stage);return value
        self.host.prepare_action.side_effect=lambda *args,**kwargs:current('prepare',self.prepared)
        self.judge.side_effect=lambda *args:current('review',A.ActVerdict('allow',R.ORDINARY,'fixed','model'))
        self.host.execute_action.side_effect=lambda *args,**kwargs:current('execute',D.ActResult('acted','Done'))
        self.host.wait_action_downloads.side_effect=lambda *args:current('downloads',None)
        self.host.drive_finished.side_effect=lambda *args:current('finished',None)
        self.assertEqual(self.action('click').outcome,'acted')
        self.assertEqual(stages,['prepare','review','prepare','execute','downloads','finished'])
        self.assertEqual(gate.counts(self.snap.profile)[0],0)
        self.host.begin_activity.assert_called_once_with(self.snap.tab_id,self.snap.page_identity)
        self.host.end_activity.assert_called_once()

    def test_failed_driver_reservation_releases_parent_and_runs_no_page_io(self):
        from jarvis.v2.browser.activity import BrowserActivity
        from jarvis.v2.browser.drive_state import DriveStateRefused
        gate=BrowserActivity();gate.opened(self.snap.profile,1);gate.desired(1)
        self.state['owner_quiet_s']=5.0
        self.host.begin_activity.side_effect=lambda *args:gate.enter(self.snap.profile,1)
        self.host.end_activity.side_effect=gate.leave
        with patch.object(self.service.state,'reserve',side_effect=DriveStateRefused('synthetic busy')):
            self.refused_without_io()
        self.assertEqual(gate.counts(self.snap.profile)[0],0)
        self.host.end_activity.assert_called_once()
    def test_shipping_off_ignores_environment_and_has_zero_io(self):
        service=D.BrowserDrive(Mock())
        with patch.dict("os.environ",{"JARVIS_AGENTS_MAY_DRIVE":"1","JARVIS_BROWSER_DRIVE":"true"}):
            self.assertEqual(service.act(D.Action("type",target="e1",text="x")).text,"Refused: "+D.OFF)
        service.daemon.assert_not_called();self.assertEqual(service.daemon.mock_calls,[])

    def test_strict_desk_depth_caller_and_live_turn_fail_before_page(self):
        for change in ("strict","desk","depth","caller","stop"):
            with self.subTest(change=change):
                self.turn.strict=change=="strict";self.desk["present"]=change!="desk"
                runtime.bind(depth=1 if change=="depth" else 0,should_stop=lambda:change=="stop")
                runtime.bind_caller("wrong" if change=="caller" else self.turn.thread_id,"project","fast",self.turn.turn_id)
                self.refused_without_io()

    def test_missing_or_stale_ref_never_prepares_or_reviews(self):
        for target in ("e2","e999"):
            self.assertEqual(self.action(target=target).outcome,"refused")
        self.service.state.publish(self.turn,replace(self.snap,document_generation=2),"b"*32,["e1"])
        self.refused_without_io()

    def test_permission_private_direct_source_drawn_and_current_gates(self):
        from jarvis.v2.browser.panes import ResolvedSource
        for snap in (replace(self.snap,see=False),replace(self.snap,drive=False),
                     replace(self.snap,mode="direct"),replace(self.snap,covered=True),
                     replace(self.snap,drawn=False),replace(self.snap,source=ResolvedSource("unknown")),
                     replace(self.snap,source=ResolvedSource("terminal",terminal_id="t"))):
            with self.subTest(snapshot=snap):
                self.registry.resolve.return_value=snap
                self.daemon.terminals.readable_source.return_value=False
                self.refused_without_io()
        self.registry.resolve.return_value=self.snap;self.registry.still_current.return_value=False
        self.refused_without_io()

    def test_source_readability_is_checked_even_with_a_current_valid_capability(self):
        from jarvis.v2.browser.panes import ResolvedSource
        self.snap=replace(self.snap,source=ResolvedSource('terminal',terminal_id='source-terminal'))
        self.registry.resolve.return_value=self.snap
        self.service.state.publish(self.turn,self.snap,'a'*32,['e1'])
        self.daemon.terminals.readable_source.return_value=False
        self.refused_without_io()

    def test_unreferenced_navigation_still_requires_drive_permission_and_visibility(self):
        for snap in (replace(self.snap,drive=False),replace(self.snap,see=False),replace(self.snap,mode='direct'),
                     replace(self.snap,drawn=False),replace(self.snap,covered=True)):
            self.registry.resolve.return_value=snap
            with self.subTest(snapshot=snap):
                self.assertEqual(self.action('navigate',text=self.snap.url).outcome,'refused')
                self.host.prepare_action.assert_not_called();self.judge.assert_not_called()

    def test_owner_quiet_exact_boundary_pause_socket_and_cards(self):
        for key,value in (("paused",True),("shown",False),("visible",False),("owner_quiet_s",4.999),
                          ("owner_quiet_s",float("nan")),("input_serial",None),("held",True),("depressed",True),("input_busy",True)):
            old=self.state[key];self.state[key]=value
            with self.subTest(key=key):self.refused_without_io()
            self.state[key]=old
        self.approvals.pending.return_value=[object()];self.refused_without_io()
        self.approvals.pending.return_value=[];self.state["owner_quiet_s"]=5.0
        self.assertEqual(self.action().outcome,"acted")

    def test_h1_password_refuses_before_reviewer_and_input(self):
        self.prepared=replace(self.prepared,target=R.Element(tag="input",type="password"))
        self.assertIn(R.R_SENSITIVE,self.action().text)
        self.judge.assert_not_called();self.approvals.ask.assert_not_called();self.host.execute_action.assert_not_called()

    def test_allow_search_calls_no_model_and_charges_actual_action(self):
        self.assertEqual(self.action().outcome,"acted")
        self.judge.assert_not_called();self.approvals.ask.assert_not_called()
        self.assertEqual(len(self.service.state.hourly[self.snap.tab_id]),1)
        self.assertEqual(self.host.prepare_action.call_count,2)

    def test_owner_takeover_or_ref_replacement_during_review_stops(self):
        for mutate in (lambda:self.state.update(input_serial=1),lambda:self.state.update(attachment_generation=2),
                       lambda:self.service.state.publish(self.turn,self.snap,"b"*32,["e1"])):
            self.state.update(input_serial=0,attachment_generation=1)
            self.service.state.publish(self.turn,self.snap,"a"*32,["e1"])
            self.judge.side_effect=lambda *a,**k:(mutate(),A.ActVerdict("allow",R.ORDINARY,"unused","model"))[1]
            self.assertEqual(self.action("click").outcome,"refused")
            self.host.execute_action.assert_not_called()

    def test_sensitive_floor_never_lowered_and_card_is_exact_private_command(self):
        self.prepared=replace(self.prepared,target=R.Element(tag="button",name="Sign in",submits=True),
                              form=R.Form(fields=(R.Element(tag="input",type="password"),)))
        self.assertEqual(self.action("click").outcome,"acted")
        request=self.approvals.ask.call_args.args[0]
        self.assertTrue(request.hud_only);self.assertFalse(request.allowlistable);self.assertEqual(request.max_wait_s,120)
        self.assertEqual(json.loads(request.command)["target"],"e1")
        self.assertEqual(request.browser["page_identity"],self.snap.page_identity)
        self.assertNotIn("page-model-marker",json.dumps(request.owner_json()))
        self.assertNotIn("Sign in",json.dumps(request.to_json()))
        self.assertEqual(self.judge.call_args.args[1],self.turn.owner_words)

    def test_card_denial_and_changed_post_yes_fingerprint_never_execute(self):
        self.judge.return_value=A.ActVerdict("ask",R.ORDINARY,"unused","model")
        self.approvals.ask.return_value=Decision.DENY
        result=self.action("click");self.assertEqual(result.outcome,"asked")
        self.assertEqual(self.daemon.bus.publish.call_args.args[0]["data"]["outcome"],"asked")
        self.approvals.ask.return_value=Decision.ALLOW
        original=self.prepared
        def change(*a,**k):self.prepared=replace(original,fingerprint="changed");return Decision.ALLOW
        self.approvals.ask.side_effect=change
        self.assertEqual(self.action("click").outcome,"refused")
        self.host.execute_action.assert_not_called();self.assertEqual(self.approvals.ask.call_count,2)

    def test_own_card_hold_allowed_but_other_card_or_owner_input_cancels(self):
        self.judge.return_value=A.ActVerdict("ask",R.ORDINARY,"unused","model")
        def ask(request,*,cancelled):
            self.approvals.pending.return_value=[request];self.state.update(held=True,hold_count=1)
            self.assertFalse(cancelled())
            self.approvals.pending.return_value=[request,object()];self.assertTrue(cancelled())
            self.approvals.pending.return_value=[request];self.state.update(input_serial=1);self.assertTrue(cancelled())
            self.approvals.pending.return_value=[];self.state.update(held=False)
            return Decision.ALLOW
        self.approvals.ask.side_effect=ask
        self.assertEqual(self.action("click").outcome,"refused");self.host.execute_action.assert_not_called()

    def test_budget_and_audit_remain_bounded_and_content_free(self):
        self.judge.return_value=A.ActVerdict("deny",R.INJECTION,"PRIVATE-MODEL-REASON","model")
        for _ in range(40):self.assertEqual(self.action("click").outcome,"refused")
        self.assertEqual(self.judge.call_count,40)
        self.assertEqual(self.action("click").outcome,"refused");self.assertEqual(self.judge.call_count,40)
        records=[call.args[0].to_json() for call in self.audit.call_args_list]
        self.assertTrue(records);self.assertTrue(all(len(r["args_digest"])==64 for r in records))
        text=json.dumps(records)+str(self.daemon.bus.mock_calls)
        for forbidden in ("PRIVATE-MODEL-REASON","fixture.test","Search","query"):
            self.assertNotIn(forbidden,text)
        self.assertTrue(all(set(call.args[0]["data"])=={"tab_id","action_id","at","outcome"} for call in self.daemon.bus.publish.call_args_list))

    def test_navigate_internal_and_unleased_control_urls_no_prepare(self):
        self.host.loopback_open.return_value=False
        for url in ("chrome://settings/people","http://127.0.0.1:8402/","http://localhost:19741/"):
            self.assertEqual(self.action("navigate",text=url).outcome,"refused")
        self.host.prepare_action.assert_not_called();self.judge.assert_not_called()

if __name__=="__main__":unittest.main()

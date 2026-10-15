"""BR9b fast-path transcript, private records, and delivered-ref boundaries."""
from types import SimpleNamespace
import json
import queue
import threading
import unittest
from unittest.mock import Mock,patch
from jarvis import runtime,tools
from jarvis.v2.provider import Event,EventKind,UserMessage
from jarvis.v2.browser.drive_state import current_turn,DriveTurn,turn_scope
from jarvis.v2.browser.drive import ActResult
from jarvis.v2.browser.look import LookResult
from jarvis.v2.providers import fastpath
from jarvis.v2.tools import hud_browser_act as action_tool,hud_browser_view as view_tool
from tests.v2 import fastpath_check as H

class Integration(unittest.TestCase):
    def setUp(self):
        scope=runtime.turn_scope(desk={'present':True,'images':False});scope.__enter__()
        self.addCleanup(scope.__exit__,None,None,None)
        runtime.bind(depth=0,should_stop=lambda:False)

    def test_exact_explicit_tool_exception_and_private_outcome_vocabulary(self):
        name='hud_browser_act'
        self.assertEqual(fastpath.DRIVE_TOOLS,frozenset({name}))
        self.assertIn(name,fastpath.FAST_TOOLS);self.assertIn(name,fastpath.DESK_TOOLS)
        self.assertIn(name,tools.EXPLICIT_ONLY);self.assertIn(name,tools.PRIVATE_RESULT)
        self.assertNotIn(name,tools.default_names());self.assertNotIn(name,tools.PARALLEL_SAFE)
        self.assertFalse(tools.REGISTRY[name].dangerous)
        self.assertEqual(fastpath.FORBIDDEN_PREFIXES,('browser_','desktop_','cad_','gmail_','discord_'))
        for text,expected in [('Acted: PRIVATE','acted'),('Asked: PRIVATE','asked'),('Stopped: PRIVATE','stopped'),('Refused: PRIVATE','refused'),('unknown PRIVATE','refused')]:
            self.assertEqual(tools.private_summary(name,text),expected)

    def test_tool_desk_gate_and_production_off_before_host(self):
        self.assertIn('Agents may drive is off',action_tool.hud_browser_act('click',target='e1'))
        with patch.object(action_tool,'act_for_tool') as act:
            runtime.bind(desk={'present':False})
            self.assertTrue(action_tool.hud_browser_act('click',target='e1').startswith('Refused:'))
            act.assert_not_called()

    def test_prompt_renarrows_each_desk_transition_from_pristine(self):
        provider=H.FastPathProvider();handle=provider.start(H.thread('prompt-turn'),H.brief(system_append="\n\nPinned caller context"),H.allow_all)
        self.addCleanup(provider.close,handle)
        native=handle.native
        for desk in (False,True,False,True):
            provider._turn_tools(native,desk)
            held={s['function']['name'] for s in native.agent.tool_specs}
            self.assertEqual('hud_browser_act' in held,desk)
            self.assertEqual('On a desk turn, `hud_browser_act`' in native.agent._base_system,desk)
            self.assertTrue(native.agent._base_system.endswith('Pinned caller context'))
            self.assertEqual(native.agent._base_system,H.config.narrow_prompt(native.pristine_system,held))

    def test_actual_worker_scope_exhaustion_has_no_handoff_and_consistent_transcript(self):
        # The ending states the turn's own budget: thirty on a desk turn holding
        # the edit tools (2026-10-14, decision F-4), eight on one without them.
        for kw,steps,word in (({},fastpath.FAST_EDIT_MAX_STEPS,'thirty'),
                              ({'allowed_tools':sorted(fastpath.FAST_TOOLS)},fastpath.FAST_MAX_STEPS,'eight')):
          with self.subTest(steps=steps):
            provider=H.FastPathProvider();handle=provider.start(H.thread('drive-turn'),H.brief(**kw),H.allow_all)
            self.addCleanup(provider.close,handle)
            runtime.bind_caller('drive-turn','p1','fast','trusted-turn')
            calls=[];turns=[]
            def act(**kwargs):
                turn=current_turn();self.assertIsNotNone(turn)
                self.assertEqual(turn.key,('drive-turn','trusted-turn'))
                self.assertEqual(turn.owner_words,'original owner request')
                self.assertEqual(turn.project_root,H.CWD)
                turns.append(turn);turn.attempted=True;turn.pane='2'
                return 'Stopped: scripted action'
            def fake(model,messages,tools=None,**kw):
                self.assertIsNotNone(tools,'driving exhaustion must not call the handoff model')
                calls.append(1)
                return H.reply('',[('hud_browser_act',json.dumps({'action':'type','target':'e1','text':'PRIVATE-TYPED-MARKER'}))])
            with patch.object(tools.REGISTRY['hud_browser_act'],'func',side_effect=act),H.scripted(fake):
                events=list(provider.send(handle,UserMessage(text='original owner request',desk=True)))
            self.assertEqual(len(calls),steps)
            final=H.one(events,EventKind.TEXT).data['text']
            self.assertEqual(final,f"Stopped after {word} steps in Browser pane 2. Say 'go on' to continue.")
            self.assertEqual(handle.native.agent.messages[-1]['content'],final)
            self.assertIsNone(H.one(events,EventKind.TURN_FINISHED).data['proposal'])
            self.assertTrue(all(t.closed for t in turns));self.assertIsNone(current_turn())
            self.assertIsNone(handle.native.agent.exhaustion_message)
            starts=[e for e in events if e.kind==EventKind.TOOL_STARTED]
            self.assertTrue(starts);self.assertTrue(all(e.data['args']=={} for e in starts))
            self.assertNotIn('PRIVATE-TYPED-MARKER',json.dumps([e.data for e in events]))
            # The chat's own model transcript still receives the original argument.
            self.assertIn('PRIVATE-TYPED-MARKER',json.dumps(handle.native.agent.messages))

    def test_explicit_unrelated_task_proposal_is_preserved(self):
        turn=SimpleNamespace(stopped_early=True,text='unused')
        self.assertEqual(fastpath._proposal({'brief':'explicit unrelated task'},turn,driving=True),{'brief':'explicit unrelated task'})
        self.assertIsNone(fastpath._proposal({},turn,driving=True))

    def test_failed_worker_restores_drive_scope_and_ending(self):
        provider=H.FastPathProvider();handle=provider.start(H.thread('fail-drive'),H.brief(),H.allow_all)
        self.addCleanup(provider.close,handle);runtime.bind_caller('fail-drive','p1','fast','failed-turn')
        seen=[]
        def boom(*a,**k):seen.append(current_turn());raise RuntimeError('synthetic failure')
        with patch.object(handle.native.agent,'run_turn',side_effect=boom):
            events=list(provider.send(handle,UserMessage(text='owner',desk=True)))
        self.assertTrue(seen[0].closed);self.assertIsNone(current_turn());self.assertIsNone(handle.native.agent.exhaustion_message)
        self.assertEqual(H.one(events,EventKind.ERROR).data['fatal'],True)

    def test_look_receipt_runs_only_after_successful_tool_presentation(self):
        delivered=Mock();result=LookResult(text='safe page',source='Browser',delivered=delivered)
        with patch.object(view_tool,'look_for_tool',return_value=result):
            self.assertIn('safe page',str(view_tool.hud_browser_view()))
        delivered.assert_called_once();delivered.reset_mock()
        def leave(*a):runtime.bind(desk={'present':False});return result
        with patch.object(view_tool,'look_for_tool',side_effect=leave):
            self.assertTrue(str(view_tool.hud_browser_view()).startswith('Refused:'))
        delivered.assert_not_called()

    def test_daemon_reinforces_private_arguments_results_and_labels(self):
        from jarvis.v2.daemon import Daemon
        rows=[];daemon=object.__new__(Daemon);daemon._lock=threading.RLock()
        daemon.stores=SimpleNamespace(threads=SimpleNamespace(_append=lambda *args:rows.append(args[-1])))
        daemon.bus=Mock()
        session=SimpleNamespace(retired=False,thread=H.thread('privacy'),turn_id='turn',brief=H.brief())
        marker='PRIVATE-URL-TEXT-PATH-MARKER'
        for kind,data in [(EventKind.TOOL_STARTED,{'name':'hud_browser_act','call_id':'call','args':{'text':marker},'label':marker}),
            (EventKind.TOOL_FINISHED,{'name':'hud_browser_act','call_id':'call','ok':False,'summary':marker,'extra':marker})]:
            Daemon._record(daemon,session,Event(kind,'privacy',data))
        encoded=json.dumps(rows)
        self.assertNotIn(marker,encoded);self.assertEqual(rows[0]['data']['args'],{})
        self.assertEqual(rows[0]['data']['label'],'Browser action')
        self.assertEqual(rows[1]['data']['summary'],'refused')
        self.assertEqual(daemon.bus.publish.call_count,2)

if __name__=='__main__':unittest.main()

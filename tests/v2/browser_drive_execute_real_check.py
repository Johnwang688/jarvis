"""Real CDP BR9b action execution; enabled policy only in isolated fixtures."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import asyncio
import threading
import unittest
from jarvis import runtime
from jarvis.v2.provider import Decision
from jarvis.v2.browser import drive as D, act_review as A, act_rules as R
from jarvis.v2.browser.drive_state import DriveTurn,turn_scope
from tests.v2 import hud_browser_view_check as V

class Real(V.Real):
    def setUp(self):
        super().setUp();self.desk["images"]=False
        self.snap=replace(self.snap,drive=True)
        self.turn=DriveTurn("drive-fixture","drive-turn",False,str(self.root / "project"),"Test the local fixture controls")
        self.saved=runtime.caller();runtime.bind_caller(self.turn.thread_id,"fixture","fast",self.turn.turn_id)
        self.addCleanup(runtime._CALLER.set,self.saved)
        runtime.bind(should_stop=lambda:False,depth=0)
        self.service=D.BrowserDrive(self.daemon,enabled=lambda:True)
        scope=turn_scope(self.turn,self.service.state.retire);scope.__enter__();self.addCleanup(scope.__exit__,None,None,None)
        self.daemon._sessions[self.turn.thread_id]=SimpleNamespace(retired=False,turn_id=self.turn.turn_id,cancelled=threading.Event())
        self.addCleanup(self.daemon._sessions.pop,self.turn.thread_id,None)
        original_owner=self.host.owner_state
        # These fixtures test real CDP and real owner stamps; the lease is an
        # explicitly injected fixture, as production drive remains OFF. Socket
        # owner takeover has a separate existing real HTTP/WebSocket regression.
        self.owner_patch=patch.object(self.host,"owner_state",side_effect=lambda tab:{**original_owner(tab),"shown":True,"visible":True})
        self.owner_patch.start();self.addCleanup(self.owner_patch.stop)
        self.daemon.browser_panes.after_navigation=lambda snap:replace(snap,url=self.pg.url,document_generation=self.tab.document_generation) if self.valid else None
        self.review_patch=patch.object(A,"judge",return_value=A.ActVerdict("allow",R.ORDINARY,"fixture","model"))
        self.judge=self.review_patch.start();self.addCleanup(self.review_patch.stop)

    def choose(self,label):
        scan,_,_=self.host.capture_look(self.snap,False,False,(),lambda:None)
        self.service.publish_look(self.snap,scan)
        return next(row["ref"] for row in scan["refs"] if row["label"]==label)

    def act(self,kind,label="",text=""):
        target=self.choose(label) if label else ""
        return self.service.act(D.Action(kind,"2",target,text))

    def test_click_select_scroll_and_value_replacement(self):
        self.html("""<input aria-label='Find' type='search' value='previous'><button>Go</button>
        <select aria-label='Choice'><option>A</option><option>B</option></select><div style='height:2400px'></div>
        <script>window.events=[];document.querySelector('button').onclick=()=>events.push('clicked');</script>""")
        result=self.act("click","Go");self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.run_on(self.pg.evaluate("events")),["clicked"])
        result=self.act("type","Find","replacement"*7);self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.run_on(self.pg.locator("input").input_value()),"replacement"*7)
        result=self.act("select","Choice","B");self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.run_on(self.pg.locator("select").input_value()),"B")
        result=self.act("scroll",text="down");self.assertEqual(result.outcome,"acted",result)
        self.assertGreater(self.run_on(self.pg.evaluate("scrollY")),0)

    def test_focus_handler_redirect_cannot_receive_typed_text(self):
        self.html("""<input id='find' aria-label='Find' type='search'><input id='secret' type='password'>
        <script>document.querySelector('#find').onfocus=()=>document.querySelector('#secret').focus();</script>""")
        self.assertTrue(self.run_on(self.pg.locator("#find").evaluate("e=>typeof e.onfocus==='function'")))
        result=self.act("type","Find","never-in-password")
        self.assertEqual(result.outcome,"stopped",result)
        self.assertIn("0 characters",result.text)
        self.assertEqual(self.run_on(self.pg.locator("#secret").input_value()),"")
        self.assertEqual(self.run_on(self.pg.locator("#find").input_value()),"")

    def test_password_type_change_after_first_chunk_stops_remaining_text(self):
        self.html("""<input id='find' aria-label='Find' type='search'>
        <script>document.querySelector('#find').oninput=e=>{e.target.type='password'};</script>""")
        self.assertTrue(self.run_on(self.pg.locator("#find").evaluate("e=>typeof e.oninput==='function'")))
        result=self.act("type","Find","x"*90)
        self.assertEqual(result.outcome,"stopped",result);self.assertIn("32 characters",result.text)
        self.assertEqual(self.run_on(self.pg.locator("input").input_value()),"x"*32)

    def test_owner_input_between_chunks_stops_without_following_events(self):
        self.html("<input aria-label='Find' type='search'>")
        cdp=self.run_on(self.host._ensure_cdp(self.tab));original=cdp.send;sent=[]
        async def intercept(method,params=None):
            result=await original(method,params)
            if method=="Input.insertText":
                sent.append(params["text"]);self.tab.stream.owner_input()
            return result
        with patch.object(cdp,"send",side_effect=intercept):result=self.act("type","Find","y"*100)
        self.assertEqual(result.outcome,"stopped",result);self.assertEqual(sent,["y"*32])
        self.assertTrue(self.tab.stream.paused)

    def test_overlay_added_after_review_prevents_all_click_input(self):
        self.html("<button style='position:absolute;left:30px;top:30px;width:150px;height:50px'>Go</button><script>window.clicks=0;document.querySelector('button').onclick=()=>clicks++;</script>")
        def judge(*args,**kwargs):
            self.run_on(self.pg.evaluate("()=>{const o=document.createElement('div');o.style='position:fixed;inset:0;background:red;z-index:99';document.body.append(o)}"))
            return A.ActVerdict("allow",R.ORDINARY,"fixture","model")
        self.judge.side_effect=judge
        result=self.act("click","Go");self.assertIn(result.outcome,("refused","stopped"))
        self.assertEqual(self.run_on(self.pg.evaluate("clicks")),0)

    def test_open_shadow_control_can_receive_exact_typed_value(self):
        self.html("""<div id='root'></div><script>root.attachShadow({mode:'open'}).innerHTML='<input aria-label="Find" type="search">';</script>""")
        result=self.act("type","Find","shadow query")
        self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.run_on(self.pg.locator("input").input_value()),"shadow query")

    def test_press_tab_allows_ordinary_focus_advance_but_stops_at_sensitive_field(self):
        self.html("<input id='first' aria-label='First' type='search'><input id='next' aria-label='Next' type='search'>")
        self.run_on(self.pg.locator("#first").focus())
        result=self.act("press",text="Tab");self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.run_on(self.pg.evaluate("document.activeElement.id")),"next")
        self.html("<input id='first' aria-label='First' type='search'><input id='next' type='password'>")
        self.run_on(self.pg.locator("#first").focus())
        # Initial sensitive form adjacency needs an owner card. Script exactly
        # one answer; the newly focused sensitive control gets no keyUp/review.
        with patch.object(self.daemon.approvals,"ask",return_value=Decision.ALLOW):
            result=self.act("press",text="Tab")
        self.assertEqual(result.outcome,"stopped",result)
        self.assertEqual(self.run_on(self.pg.locator("#next").input_value()),"")

    def test_identical_focused_control_replacement_during_review_is_not_same_target(self):
        self.html("<input aria-label='Same'><input aria-label='Same'><script>window.keys=0;document.addEventListener('keydown',()=>keys++);</script>")
        self.run_on(self.pg.locator("input").first.focus())
        def change(*args,**kwargs):
            self.run_on(self.pg.locator("input").last.focus())
            return A.ActVerdict("allow",R.ORDINARY,"fixture","model")
        self.judge.side_effect=change
        result=self.act("press",text="Enter")
        self.assertEqual(result.outcome,"refused",result)
        self.assertEqual(self.run_on(self.pg.evaluate("keys")),0)

    def test_navigation_uses_existing_lease_and_binds_fresh_document(self):
        self.html("<p>Before navigation</p>")
        owners={port:set(ids) for port,ids in self.host._port_owners.items()}
        result=self.act("navigate",text=self.url+"?agent-navigation=1")
        self.assertEqual(result.outcome,"acted",result)
        self.assertEqual(self.host._port_owners,owners)
        capability=self.service.state.refs[self.turn.key]
        self.assertGreater(capability.snapshot.document_generation,self.snap.document_generation)
        self.assertEqual(capability.snapshot.page_identity,self.snap.page_identity)
        self.assertEqual(capability.snapshot.url,self.pg.url)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(Real(name) for name in sorted(Real.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

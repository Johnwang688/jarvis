"""Real CDP BR9b action execution; enabled policy only in isolated fixtures."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import asyncio
import json
import threading
import unittest
from jarvis import runtime
from jarvis.v2.provider import Decision
from jarvis.v2.browser import drive as D, act_review as A, act_rules as R, drive_execute as E
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
        self.daemon.browser_drive=self.service
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

    def test_real_upload_file_lives_after_source_removal_and_submits_exact_bytes(self):
        from pathlib import Path
        from jarvis import config
        project=Path(self.turn.project_root);project.mkdir(exist_ok=True)
        original=project/'owner-report.txt';original.write_bytes(b'approved synthetic report')
        self.html("<form><input name='report' type='file' aria-label='Choose file'></form>")
        target=self.choose('Choose file')
        with patch.object(self.daemon.approvals,'ask',return_value=Decision.ALLOW):
            result=self.service.act(D.Action('upload','2',target,file='owner-report.txt'))
        self.assertEqual(result.outcome,'acted',result)
        self.assertEqual(self.run_on(self.pg.locator('input').evaluate('async e=>await e.files[0].text()')),'approved synthetic report')
        self.assertFalse((config.BROWSER_DIR/'drive-staging').exists())
        original.unlink()
        self.assertEqual(self.run_on(self.pg.locator('input').evaluate('async e=>await e.files[0].text()')),'approved synthetic report')
        endpoint=self.url.rsplit('/',1)[0]+'/uploaded-report';posted=[]
        async def receive(route):
            posted.append(route.request.post_data_buffer);await route.fulfill(status=200,body='received')
        self.run_on(self.pg.route(endpoint,receive))
        self.assertEqual(self.run_on(self.pg.evaluate("async url=>{const r=await fetch(url,{method:'POST',body:new FormData(document.querySelector('form'))});return r.status}",endpoint)),200)
        self.assertEqual(len(posted),1);self.assertIn(b'approved synthetic report',posted[0])
        self.assertIn(b'filename="owner-report.txt"',posted[0])

    def test_select_input_sensitization_or_replacement_stops_before_change(self):
        for mutation in ("e.target.autocomplete='cc-number'","window.original=e.target;e.target.replaceWith(e.target.cloneNode(true))"):
            with self.subTest(mutation=mutation):
                self.html("<select aria-label='Choice'><option>A</option><option>B</option></select><script>window.inputs=0;window.changes=0;document.addEventListener('change',()=>changes++);document.querySelector('select').oninput=e=>{inputs++;"+mutation+"};</script>")
                result=self.act('select','Choice','B')
                self.assertEqual(result.outcome,'stopped',result);self.assertIn('updating',result.text)
                self.assertEqual(self.run_on(self.pg.evaluate('[inputs,changes]')),[1,0])
                if 'replaceWith' in mutation:
                    self.assertEqual(self.run_on(self.pg.evaluate('original.value')), 'B')
                    self.assertEqual(self.run_on(self.pg.locator('select').input_value()),'A')
                else:self.assertEqual(self.run_on(self.pg.locator('select').input_value()),'B')

    def test_click_release_never_hits_overlay_added_by_mousedown(self):
        self.html("<button>Go</button><script>window.downs=0;window.foreignUps=0;document.querySelector('button').onmousedown=()=>{downs++;const o=document.createElement('div');o.style='position:fixed;inset:0;z-index:99';o.onmouseup=()=>foreignUps++;document.body.append(o)};</script>")
        result=self.act('click','Go')
        self.assertEqual(result.outcome,'stopped',result)
        self.assertEqual(self.run_on(self.pg.evaluate('[downs,foreignUps]')),[1,0])

    def test_upload_buffer_source_changes_and_each_target_phase_stops_safely(self):
        from pathlib import Path
        from jarvis import config
        project=Path(self.turn.project_root);project.mkdir(exist_ok=True)
        source=project/'report.txt';body=b'approved bytes across chunk boundaries'*(E.UPLOAD_CHUNK_BYTES//20)
        cdp=self.run_on(self.host._ensure_cdp(self.tab));original=cdp.send
        declaration='function(args){return ('+E.UPLOAD_JS+')(args)}'
        for change_at in ('source','begin','chunk','assign','input'):
            with self.subTest(phase=change_at):
                source.write_bytes(body)
                self.html("<input type='file' aria-label='Choose file'><script>window.events=[];document.addEventListener('input',()=>events.push('input'));document.addEventListener('change',()=>events.push('change'));</script>")
                target=self.choose('Choose file');phases=[];changed=False
                async def intercept(method,params=None):
                    nonlocal changed
                    result=await original(method,params)
                    if method=='Runtime.callFunctionOn' and (params or {}).get('functionDeclaration')==declaration:
                        args=params['arguments'][0]['value'];phases.append(args['phase'])
                        self.assertLessEqual(len(json.dumps(params)),len(E.UPLOAD_JS)+1048576+2000)
                        if not changed and (args['phase']==change_at or change_at=='source'):
                            changed=True
                            if change_at=='source':source.write_bytes(b'mutated after immutable handoff')
                            else:await self.pg.evaluate("()=>{const e=document.querySelector('input');e.replaceWith(e.cloneNode(true))}")
                    return result
                with patch.object(cdp,'send',side_effect=intercept),patch.object(self.daemon.approvals,'ask',return_value=Decision.ALLOW):
                    result=self.service.act(D.Action('upload','2',target,file='report.txt'))
                self.assertTrue(changed,'phase boundary must actually be reached')
                self.assertFalse((config.BROWSER_DIR/'drive-staging').exists())
                if change_at=='source':
                    self.assertEqual(result.outcome,'acted',result)
                    self.assertEqual(self.run_on(self.pg.locator('input').evaluate('async e=>await e.files[0].text()')),body.decode())
                    self.assertGreater(phases.count('chunk'),1)
                else:
                    self.assertEqual(result.outcome,'stopped',result)
                    self.assertEqual(phases[-1],change_at)
                    self.assertEqual(self.run_on(self.pg.evaluate('events')),['input'] if change_at=='input' else [])
                    if change_at in ('assign','input'):self.assertIn('selecting the approved file',result.text)

    def test_upload_input_handler_cannot_trigger_change_after_sensitization(self):
        from pathlib import Path
        project=Path(self.turn.project_root);project.mkdir(exist_ok=True)
        (project/'report.txt').write_bytes(b'approved')
        self.html("<input type='file' aria-label='Choose file'><script>window.events=[];document.querySelector('input').oninput=e=>{events.push('input');e.target.autocomplete='cc-number'};document.addEventListener('change',()=>events.push('change'));</script>")
        target=self.choose('Choose file')
        with patch.object(self.daemon.approvals,'ask',return_value=Decision.ALLOW):
            result=self.service.act(D.Action('upload','2',target,file='report.txt'))
        self.assertEqual(result.outcome,'stopped',result);self.assertIn('selecting',result.text)
        self.assertEqual(self.run_on(self.pg.evaluate('events')),['input'])


    def test_upload_full_50_mib_cap_uses_bounded_transport(self):
        from pathlib import Path
        project=Path(self.turn.project_root);project.mkdir(exist_ok=True)
        source=project/'large.txt'
        block=b'a'*(1024*1024)
        with source.open('wb') as output:
            for _ in range(50):output.write(block)
        self.html("<input type='file' aria-label='Choose file'>")
        target=self.choose('Choose file')
        with patch.object(self.daemon.approvals,'ask',return_value=Decision.ALLOW):
            result=self.service.act(D.Action('upload','2',target,file='large.txt'))
        self.assertEqual(result.outcome,'acted',result);source.unlink()
        result=self.run_on(self.pg.locator('input').evaluate("async e=>{const f=e.files[0];return [f.size,await f.slice(0,8).text(),await f.slice(-8).text()]}"))
        self.assertEqual(result,[R.MAX_UPLOAD_BYTES,'aaaaaaaa','aaaaaaaa'])


    def test_control_port_spellings_and_unleased_navigation_have_zero_accepts(self):
        self.html('<p>Control-port refusal fixture</p>')
        before=self.stand_in_accepts();unleased=self.closed.accepts
        owners={port:set(ids) for port,ids in self.host._port_owners.items()}
        for port in (listener.port for listener in self.stand_ins.values()):
            for host in ('127.0.0.1','localhost','[::1]','foo.localhost','127.1','2130706433','[::ffff:127.0.0.1]'):
                with self.subTest(host=host,port=port):
                    result=self.act('navigate',text=f'http://{host}:{port}/blocked')
                    self.assertEqual(result.outcome,'refused',result)
        result=self.act('navigate',text=f'http://127.0.0.1:{self.closed.port}/unleased')
        self.assertEqual(result.outcome,'refused',result)
        self.assertEqual(self.stand_in_accepts(),before);self.assertEqual(self.closed.accepts,unleased)
        self.assertEqual(self.host._port_owners,owners);self.judge.assert_not_called()

    def test_password_focus_has_no_model_card_or_key_input(self):
        self.html("<input type='password'><script>window.keys=0;document.addEventListener('keydown',()=>keys++);</script>")
        self.run_on(self.pg.locator('input').focus())
        with patch.object(self.daemon.approvals,'ask') as ask:
            result=self.act('press',text='Backspace')
        self.assertEqual(result.outcome,'refused',result)
        self.assertEqual(self.run_on(self.pg.evaluate('keys')),0)
        self.judge.assert_not_called();ask.assert_not_called()

    def test_approval_epoch_between_chunks_stops_without_another_event(self):
        self.html("<input aria-label='Find' type='search'>")
        cdp=self.run_on(self.host._ensure_cdp(self.tab));original=cdp.send;sent=[]
        async def intercept(method,params=None):
            result=await original(method,params)
            if method=='Input.insertText':
                sent.append(params['text'])
                self.daemon.bus.publish({'kind':'approval_requested','data':{'req_id':'other-card'}})
            return result
        with patch.object(cdp,'send',side_effect=intercept):result=self.act('type','Find','z'*100)
        self.assertEqual(result.outcome,'stopped',result);self.assertEqual(sent,['z'*32])

    def test_real_download_mime_attribution_private_card_and_exact_release(self):
        from jarvis.v2.browser import files
        import os
        endpoint=self.url.rsplit('/',1)[0]+'/driver-download'
        async def serve(route):
            await route.fulfill(status=200,headers={"Content-Type":"application/x-shellscript",
                "Content-Disposition":'attachment; filename="notes.txt"'},body=b'echo synthetic fixture')
        self.run_on(self.pg.route(endpoint,serve))
        self.html("<button onclick=\"location.href='"+endpoint+"'\">Download fixture</button>")
        requests=[]
        def yes(request,**kw):requests.append(request);return Decision.ALLOW
        with patch.object(self.daemon.approvals,'ask',side_effect=yes):
            result=self.act('click','Download fixture')
        self.assertEqual(result.outcome,'acted',result)
        self.assertTrue(any(request.browser['category']==R.EXECUTABLE_DOWNLOAD for request in requests),
                        [(request.browser or {}).get('category') for request in requests])
        rows=self.host.downloads()['downloads'];self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]['held']);self.assertIn('not run',result.text)
        fd,_name,size=files.open_download(rows[0]['id'],authorize=self.service.downloads.authorized)
        try:self.assertEqual(os.read(fd,size),b'echo synthetic fixture')
        finally:os.close(fd)
        # A fresh manager has no release; MIME-only executable remains held.
        from jarvis.v2.browser.drive_downloads import Downloads
        with self.assertRaises(files.FileRefused):files.open_download(rows[0]['id'],authorize=Downloads(self.service).authorized)

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

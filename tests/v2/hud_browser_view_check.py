"""BR5 disclosures: local fixtures, dummy credentials and one Chrome only.

Run under the required3G/0swap scope. Registry integration tests are added on
BR3b's final implementation; these immutable doubles pin the agreed seam.
"""
from __future__ import annotations
import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sys
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config,llm,runtime,tools
from jarvis.v2.browser import host as H, look as L, look_capture as C, look_scan as S
from jarvis.v2.browser.panes import BrowserPanes,PaneSnapshot,ResolvedSource,PaneRefused
from jarvis.v2.providers import fastpath as F
from jarvis.v2.tools.hud_browser_view import hud_browser_view
from tests.v2 import owner_browser_check as O

MARKER="BR5-fixture-7JfLmP8vTq2AsX9z"

def snapshot(**changes):
    data=dict(window="a"*32,pane_id=5,slot=2,local_tab_id="aaaaaa",mode="remote",tab_id="abcdef01",
              view_id="abcdef01",profile="private",ctx="1234abcd",url="http://localhost:4567/",
              see=True,drive=False,drawn=True,covered=False,generation=1,page_identity=1,
              document_generation=1,source=ResolvedSource("owner"))
    data.update(changes);return PaneSnapshot(**data)


def raw(**changes):
    data=dict(url="http://localhost:4567/",title="Fixture",lines=["Hello"],refs=[],masks=[],
              raw=["http://localhost:4567/","Fixture","Hello"],width=200,height=100,incomplete=False)
    data.update(changes);return data


class Pure(unittest.TestCase):
    def test_tool_boundary_and_fixed_summaries(self):
        self.assertIn("hud_browser_view",F.FAST_TOOLS & F.DESK_TOOLS & tools.EXPLICIT_ONLY & tools.PRIVATE_RESULT)
        self.assertNotIn("hud_browser_view",tools.PARALLEL_SAFE)
        self.assertNotIn("hud_browser_view",tools.default_names())
        self.assertEqual(F.FORBIDDEN_PREFIXES,("browser_","desktop_","cad_","gmail_","discord_"))
        for text in (MARKER,"https://host/"+MARKER,"[untrusted "+MARKER):
            self.assertEqual(F._summary("hud_browser_view",text),"looked at a page")
        for text in ("Error: "+MARKER,"Refused: "+MARKER,"Stopped: "+MARKER):
            self.assertEqual(F._summary("hud_browser_view",text),"refused")
        self.assertEqual(F._summary("terminal_read","its last 234 line(s) "+MARKER),"read 234 lines")
        self.assertEqual(F._summary("terminal_read","The user declined"),"error")

    def test_desk_capability_restored_even_on_failure_and_nested_turn(self):
        self.assertFalse(runtime.desk_images())
        slot={"present":True,"images":True}
        with self.assertRaises(ValueError),runtime.turn_scope(desk=slot):
            self.assertTrue(runtime.desk_images())
            with runtime.turn_scope():self.assertTrue(runtime.desk_images())
            slot["present"]=False;self.assertFalse(runtime.desk_images())
            raise ValueError("fixture")
        self.assertFalse(runtime.at_desk());self.assertFalse(runtime.desk_images())
        with runtime.turn_scope(desk={"present":True}):self.assertFalse(runtime.desk_images())

    def test_offdesk_depth_and_argument_refusals_never_reach_service(self):
        with patch("jarvis.v2.tools.hud_browser_view.look_for_tool") as look:
            self.assertTrue(hud_browser_view().text.startswith("Refused:"))
            with runtime.turn_scope(desk={"present":True}):
                runtime.bind(depth=1)
                self.assertTrue(hud_browser_view().text.startswith("Refused:"))
            with runtime.turn_scope(desk={"present":True}):
                runtime.bind(depth=0)
                for pane in ("abcdef01","0","9"," 2",2,True):self.assertTrue(hud_browser_view(pane).text.startswith("Refused:"))
                self.assertTrue(hud_browser_view("",1).text.startswith("Refused:"))
            look.assert_not_called()

    def test_provider_pins_vision_and_offdesk_surfaces_cannot_dispatch(self):
        from jarvis.v2.model import Role
        from jarvis.v2.provider import Brief, BriefRefused, UserMessage
        from jarvis import agents, goalrunner, tasks, workflows
        from jarvis.tools import subagent
        from jarvis.v2 import mcp
        provider=F.FastPathProvider()
        for names in (tools.default_names(),workflows.SAFE_TOOLS,subagent.SUBAGENT_TOOLS,
                      goalrunner.goal_tool_names(),tasks.task_tool_names(),mcp.MCP_TOOLS,mcp.available_tools(),
                      *(kind.tools for kind in agents.TYPES.values())):
            self.assertNotIn("hud_browser_view",names)
        for role,task in ((Role.CHAT,"task"),(Role.IMPLEMENTER,None),(Role.ORCHESTRATOR,None)):
            brief=Brief(role=role,cwd="/tmp",task_id=task)
            self.assertNotIn("hud_browser_view",provider._toolset(brief))
            with self.assertRaises(BriefRefused):provider._toolset(replace(brief,allowed_tools=["hud_browser_view"]))
        cases=[(UserMessage("look",via="hud",desk=True),True,True),
               (UserMessage("look",via="hud",desk=True),False,True),
               (UserMessage("look",via="hud",desk=True),None,True),
               (UserMessage("look",via="discord",desk=True),True,False),
               (UserMessage("look",via="dm",desk=True),True,False),
               (UserMessage("look",origin="owner-ran",desk=True),True,False),
               (UserMessage("look",origin="system",via="system",desk=True),True,False)]
        for message,vision,allowed in cases:
            captured=[]
            agent=SimpleNamespace(model="old",_base_tool_names=[],tool_specs=[])
            def run_turn(*args,**kwargs):
                runtime.bind(tool_names=agent._base_tool_names)
                captured.append((runtime.at_desk(),runtime.desk_images(),tools.dispatch("hud_browser_view","{}").text))
                return SimpleNamespace(cost_usd=0,cancelled=False,stopped_early=False,text="done")
            agent.run_turn=run_turn
            native=SimpleNamespace(agent=agent,model="fixture-pinned",effort=None,stop=threading.Event(),
                pristine_system=F.config.SYSTEM_PROMPT+F.FAST_PROMPT,
                brief=Brief(role=Role.CHAT,cwd="/tmp"),tool_names=["hud_browser_view"],project_id="p",
                inbox_lock=threading.Lock(),inbox=[],leftover=[],accepting=False,cost_usd=0,turns=0,workdir=None)
            with patch.object(F.models,"tier",return_value="different-global"),patch.object(F.models,"cached_info",return_value=None if vision is None else SimpleNamespace(vision=vision)) as info,patch("jarvis.v2.tools.hud_browser_view.look_for_tool",return_value=L.LookResult(text="Dispatch marker",source="Fixture")) as look:
                list(provider._run(SimpleNamespace(thread_id="thread"),native,message))
                info.assert_called_once_with("fixture-pinned")
                self.assertEqual(look.call_count,int(allowed))
                self.assertEqual(captured[0][:2],(allowed,bool(vision) and allowed))
                self.assertEqual("Dispatch marker" in captured[0][2],allowed)

    def test_raw_guard_before_truncation_and_heuristic(self):
        forms=L.terminal_guard.value_forms([MARKER])
        for text in (MARKER,"x"*5000+MARKER,base64.b64encode(MARKER.encode()).decode()):
            with self.assertRaisesRegex(L.LookRefused,"credential"):L.guarded(raw(raw=[text]),forms)
        with self.assertRaises(L.LookRefused):L.guarded(raw(incomplete=True),[])
        with self.assertRaises(L.LookRefused):L.guarded(raw(raw=["x"*(S.RAW_CAP+1)]),[])
        self.assertFalse(L.guarded(raw(),forms))
        self.assertTrue(L.guarded(raw(raw=["TOKEN=abcdef0123456789ABCDEFfedcba987654"]),[]))

    def test_image_masks_and_downscale_are_real_pixels(self):
        from PIL import Image
        image=Image.new("RGB",(2000,1000),"white");buf=BytesIO();image.save(buf,format="PNG")
        result=L.masked_image(base64.b64encode(buf.getvalue()).decode(),raw(width=2000,height=1000,masks=[[20,20,500,200]]))
        with Image.open(BytesIO(base64.b64decode(result))) as masked:
            self.assertEqual(masked.size,(1568,784));self.assertEqual(masked.getpixel((80,80)),(24,26,29))
            self.assertEqual(masked.getpixel((1000,500)),(255,255,255))
        with self.assertRaises(L.LookRefused):L.masked_image(base64.b64encode(buf.getvalue()).decode(),raw(width=100,height=100))

    def test_provenance_and_changed_guards_have_no_url_or_label(self):
        current=snapshot(url="http://localhost:4567/"+MARKER,source=ResolvedSource("terminal",terminal_id="1234"))
        registry=SimpleNamespace(resolve=lambda _:current,still_current=lambda _:True)
        records=[];host=SimpleNamespace(input_held=lambda:False,capture_look=Mock())
        daemon=SimpleNamespace(browser_panes=registry,browser=host,terminals=SimpleNamespace(readable_source=lambda _:False),bus=SimpleNamespace(publish=records.append))
        service=L.BrowserLook(daemon)
        with runtime.turn_scope(desk={"present":True}),patch.object(L.secrets,"secret_values",return_value=[]):
            result=service.read("2",False)
            self.assertIn("terminal",result.refused);host.capture_look.assert_not_called()
            current=replace(current,source=ResolvedSource("unknown"))
            self.assertIn("source",service.read("2",False).refused)
            current=replace(current,source=ResolvedSource("owner"),see=False)
            self.assertIn("turned off",service.read("2",False).refused)
        self.assertNotIn(MARKER,json.dumps(records)+str(result))
        self.assertTrue(all(set(row["data"])=={"tab_id","at","refused"} for row in records))

    def test_direct_has_no_way_to_grant_a_port(self):
        host=SimpleNamespace(refused_ports=lambda:frozenset(),loopback_open=lambda _:False)
        with self.assertRaisesRegex(L.LookRefused,"owner-granted"):C.direct_url(host,"http://localhost:4567/")
        for url in ("http://public.example/","http://192.168.1.2/","http://localhost:8405/","chrome://settings/people"):
            with self.assertRaises((L.LookRefused,ValueError)):C.direct_url(host,url)

    def test_source_and_tool_have_no_input_or_dom_mutation_entry(self):
        source=Path(S.__file__).read_text()
        for forbidden in (".value", "defaultValue", "setAttribute(", "appendChild(", ".focus(", ".scrollTo("):
            self.assertNotIn(forbidden,source)
        source=Path(sys.modules[hud_browser_view.__module__].__file__).read_text()
        self.assertIn("from ..browser.look import look_for_tool",source)
        for forbidden in ("import BrowserHost", "import drive", "import browser", "SESSION", "navigate(", "Input."):
            self.assertNotIn(forbidden,source)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancel_during_cdp_cleanup_waits_for_private_retirement(self):
        entered=asyncio.Event();cleanup_entered=asyncio.Event();cleanup_release=asyncio.Event()
        retire_entered=asyncio.Event();retire_release=asyncio.Event()
        contexts={"owner-ctx":object(),"look-fixture":object()}
        instance=SimpleNamespace(private_contexts=contexts)
        host=SimpleNamespace(_look_lock=None,_look_tasks=set(),_look_reserved={"look-fixture"},
            _launching={},_instances={H.PRIVATE:instance},_closing_instances={},
            refused_ports=lambda:frozenset(),loopback_open=lambda _:True)
        async def retire(ctx):
            retire_entered.set();await retire_release.wait();contexts.pop(ctx)
        host._retire_context_locked=retire
        async def send(method,params=None):
            if method=="Page.getFrameTree":return {"frameTree":{"frame":{"id":"frame","loaderId":"loader","url":"http://localhost:4567/"}}}
            if method=="Page.createIsolatedWorld":return {"executionContextId":7}
            if method=="Runtime.evaluate":
                self.assertIn("delete globalThis.__jarvisLook",params["expression"])
                cleanup_entered.set();await cleanup_release.wait()
            return {}
        cdp=SimpleNamespace(send=send)
        page=SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp)))
        async def scan(*args):entered.set();await asyncio.Event().wait()
        with patch.object(C,"_hidden",new=AsyncMock(return_value=("look-fixture",page))),patch.object(C,"_scan",new=scan):
            task=asyncio.create_task(C.capture(host,snapshot(mode="direct"),False,False,[],lambda:None,time.monotonic()+5))
            await entered.wait();task.cancel();await cleanup_entered.wait();task.cancel()
            await asyncio.sleep(0);self.assertFalse(task.done());self.assertIn("look-fixture",contexts)
            cleanup_release.set();await retire_entered.wait();task.cancel()
            await asyncio.sleep(0);self.assertFalse(task.done());retire_release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(set(contexts),{"owner-ctx"});self.assertEqual(host._look_reserved,set())
        self.assertEqual(host._look_tasks,set())

    async def test_failed_retirement_is_retryable_and_blocks_new_hidden_admission(self):
        contexts={"owner-ctx":object(),"look-fixture":object()}
        instance=SimpleNamespace(private_contexts=contexts)
        host=SimpleNamespace(_launching={},_instances={H.PRIVATE:instance},_closing_instances={},
            _look_reserved={"look-fixture"},_retire_context_locked=AsyncMock(side_effect=RuntimeError("fixture")),
            _instance=AsyncMock())
        for action in (C._retire_hidden(host,"look-fixture"),C._hidden(host,"http://localhost:4567/")):
            with self.assertRaises(RuntimeError):await action
            self.assertEqual(set(contexts),{"owner-ctx","look-fixture"})
            self.assertEqual(host._look_reserved,{"look-fixture"})
        host._instance.assert_not_called()
        async def retire(ctx):contexts.pop(ctx)
        host._retire_context_locked=retire
        await C._retire_hidden(host,"look-fixture")
        self.assertEqual(set(contexts),{"owner-ctx"});self.assertEqual(host._look_reserved,set())

    async def test_empty_engine_failure_retains_reservation_until_close_succeeds(self):
        instance=SimpleNamespace(private_contexts={})
        host=SimpleNamespace(_launching={},_instances={H.PRIVATE:instance},_closing_instances={},
            _look_reserved={"look-fixture"},_tabs={},_close_instance=AsyncMock(side_effect=RuntimeError("fixture")))
        with self.assertRaises(RuntimeError):await C._retire_hidden(host,"look-fixture")
        self.assertEqual(host._look_reserved,{"look-fixture"})
        host._close_instance=AsyncMock()
        await C._retire_hidden(host,"look-fixture")
        host._close_instance.assert_awaited_once_with(H.PRIVATE)
        self.assertEqual(host._look_reserved,set())

    async def test_cancel_during_hidden_allocation_keeps_reservation_until_registered_cleanup(self):
        entered=asyncio.Event();release=asyncio.Event();abandoned=[]
        host=SimpleNamespace(_look_lock=None,_look_tasks=set(),_look_reserved=set())
        async def hidden(*args):
            host._look_reserved.add("look-fixture");entered.set()
            try:
                await release.wait()
            finally:
                if not release.is_set():abandoned.append(True)
            return "look-fixture",object()
        async def retire(*args):host._look_reserved.remove("look-fixture")
        with patch.object(C,"_hidden",new=hidden),patch.object(C,"_retire_hidden",new=retire):
            task=asyncio.create_task(C.capture(host,snapshot(mode="direct"),False,False,[],lambda:None,time.monotonic()+5))
            await entered.wait();task.cancel();task.cancel()
            for _ in range(5):await asyncio.sleep(0)
            self.assertEqual(abandoned,[],"allocation must finish registering before cleanup")
            self.assertFalse(task.done());self.assertEqual(host._look_reserved,{"look-fixture"})
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(host._look_reserved,set());self.assertEqual(host._look_tasks,set())

    async def test_hidden_reservation_counts_against_owner_adoption_and_new_looks(self):
        host=H.BrowserHost();host._tabs={str(i):SimpleNamespace(page=object()) for i in range(3)}
        host._look_reserved={"look-fixture"}
        page=object()
        instance=H._Instance(H.PRIVATE,Path('/synthetic/private'),Mock(),Mock(),generation=1)
        host._instances[H.PRIVATE]=instance
        with patch.object(config,"BROWSER_TAB_CAP",4),patch.object(host,"_refuse_popup",new=AsyncMock()) as refuse,patch.object(H,"_Tab",side_effect=AssertionError("adoption attempted beyond reserved cap")):
            self.assertIsNone(host._adopt(instance,page,None))
            await asyncio.sleep(0);refuse.assert_awaited_once_with(page)
        host=SimpleNamespace(_launching={},_look_reserved=set(),_closing_contexts=set(),_tabs={"a":object()},
            _tab_cap=lambda:1,_instance=AsyncMock())
        with self.assertRaisesRegex(L.LookRefused,"limit"):
            await C._hidden(host,"http://localhost:4567/")
        host._instance.assert_not_called()

    async def test_remote_stale_host_identity_refuses_before_cdp(self):
        tab=SimpleNamespace(page_identity=42,document_generation=7,page=object())
        host=SimpleNamespace(_look_lock=None,_look_tasks=set(),_tabs={"abcdef01":tab},_ensure_cdp=AsyncMock())
        for snap in (snapshot(page_identity=41,document_generation=7),snapshot(page_identity=42,document_generation=6)):
            with self.assertRaisesRegex(L.LookRefused,"changed"):
                await C.capture(host,snap,False,False,[],lambda:None,time.monotonic()+5)
        host._ensure_cdp.assert_not_called();self.assertEqual(host._look_tasks,set())


class Real(O.OwnerBrowser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass();config.BROWSER_TAB_CAP=4
        cls.llm_guard=patch.object(llm,"chat",side_effect=AssertionError("unscripted model call"));cls.llm_guard.start()
        cls.secret_guard=patch.object(L.secrets,"secret_values",return_value=[MARKER]);cls.secret_guard.start()

    @classmethod
    def tearDownClass(cls):
        cls.secret_guard.stop();cls.llm_guard.stop();super().tearDownClass()

    def setUp(self):
        for profile in list(self.host._instances):self.host.close_profile(profile)
        self.url=f"http://127.0.0.1:{self.fx}/{O.MARK}/page"
        row=self.host.open_tab(profile="private",url=self.url,owner=True)
        self.tab=self.host._tabs[row["tab_id"]];self.pg=self.tab.page
        self.run_on(self.pg.wait_for_load_state("load"))
        self.snap=snapshot(tab_id=self.tab.id,view_id=self.tab.id,ctx=self.tab.ctx,url=self.url,page_identity=self.tab.page_identity,document_generation=self.tab.document_generation)
        self.instance_generation=self.host.pane_tab(self.tab.id)["instance_generation"]
        self.assertIs(type(self.instance_generation),int)
        self.valid=True
        original_registry=self.daemon.browser_panes
        self.addCleanup(setattr,self.daemon,"browser_panes",original_registry)
        self.daemon.browser_panes=SimpleNamespace(resolve=lambda pane:self.snap,
            still_current=lambda snap:self.valid and snap==self.snap and self.pg.url==self.url
                and (snap.mode=="direct" or self.host.pane_tab(snap.tab_id)==dict(
                    tab_id=snap.tab_id,profile=snap.profile,ctx=snap.ctx,url=snap.url,
                    page_identity=snap.page_identity,document_generation=snap.document_generation,
                    instance_generation=self.instance_generation)))
        self.service=self.daemon.browser_look
        self.desk={"present":True,"images":True}
        self.scope=runtime.turn_scope(desk=self.desk);self.scope.__enter__();self.addCleanup(self.scope.__exit__,None,None,None)
        runtime.bind(depth=0)

    def tearDown(self):
        rss=O.rss_bytes(O.chrome_tree(config.BROWSER_DIR));self.measured[self._testMethodName+"_rss_mb"]=round(rss/2**20,1)
        self.assertLess(rss,1.5*2**30)
        for profile in list(self.host._instances):self.host.close_profile(profile)
        self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])

    def html(self,html):self.run_on(self.pg.set_content(html))
    def read(self,whole=False):return self.service.read("2",whole)

    def test_01_isolated_scan_refs_and_no_owner_page_mutation(self):
        self.html("""<title>Browser fixture</title><p>Visible article</p><button>Ordinary action</button>
        <p style='opacity:0'>HIDDEN-MARK</p><button style='opacity:0'>Hidden action</button>
        <button style='position:absolute;left:10px;top:200px;width:150px;height:40px'>Covered action</button>
        <div style='position:absolute;left:0;top:190px;width:300px;height:80px;background:white;z-index:10'></div>
        <div style='margin-top:1500px'>Beyond viewport</div>
        <script>window.changes=[];new MutationObserver(x=>changes.push(...x)).observe(document,{subtree:true,attributes:true,childList:true,characterData:true});
        window.getComputedStyle=()=>{throw new Error('main-world trap')};</script>""")
        result=self.read();self.assertFalse(result.refused,result.refused)
        self.assertIn("Visible article",result.text);self.assertIn("Ordinary action",result.text)
        for hidden in ("HIDDEN-MARK","Hidden action","Beyond viewport"):self.assertNotIn(hidden,result.text)
        self.assertNotIn("Covered action",result.text.split("INTERACTIVE",1)[1])
        self.assertIsNotNone(result.image)
        self.assertEqual(self.run_on(self.pg.evaluate("[changes.length,scrollX,scrollY,typeof window.__jarvisLook]")),[0,0,0,"undefined"])
        result=self.read(True);self.assertIn("Beyond viewport",result.text)

    def test_02_forms_never_read_and_images_masked(self):
        self.html(f"<title>Form fixture</title><p>Public text</p><input style='position:absolute;left:20px;top:50px;width:250px;height:40px' value='{MARKER}'><textarea>{MARKER}</textarea><select><option>{MARKER}</option></select><div contenteditable>{MARKER}</div>")
        result=self.read();self.assertFalse(result.refused,result.refused);self.assertNotIn(MARKER,result.text+result.source)
        from PIL import Image
        with Image.open(BytesIO(base64.b64decode(result.image))) as image:self.assertEqual(image.getpixel((80,70)),(24,26,29))

    def test_03_credentials_in_raw_tail_title_url_and_page_refuse_everything(self):
        for html in (f"<p>{MARKER}</p>",f"<title>{'x'*5000}{MARKER}</title><p>Public</p>",f"<button aria-label='{'x'*5000}{MARKER}'>Go</button>",f"<p>{'x'*5000}{MARKER}</p>"):
            self.html(html);result=self.read();self.assertIn("credential",result.refused);self.assertIsNone(result.image);self.assertEqual(result.text,"")

    def test_04_heuristic_omits_image_and_nonvision_never_captures(self):
        self.html("<p>Public paragraph</p><p>TOKEN=abcdef0123456789ABCDEFfedcba987654</p>")
        result=self.read();self.assertFalse(result.refused,result.refused);self.assertIsNone(result.image);self.assertNotIn("abcdef",result.text)
        self.html("<p>Public paragraph</p>");self.desk["images"]=False
        result=self.read();self.assertFalse(result.refused,result.refused);self.assertIsNone(result.image);self.assertIn("no image",str(result.notes))

    def test_05_direct_fresh_read_preserves_owner_private_context_and_lease(self):
        self.html("<p>Owner-only visible content</p>")
        self.snap=replace(self.snap,mode="direct",tab_id=None,view_id="view-direct-fixture",page_identity=None,document_generation=None)
        owners={port:set(ids) for port,ids in self.host._port_owners.items()}
        result=self.read();self.assertFalse(result.refused,result.refused)
        self.assertIn("fresh load",str(result.notes));self.assertNotIn("Owner-only visible content",result.text)
        self.assertEqual(self.host._port_owners,owners);self.assertEqual(self.host._look_reserved,set())
        self.assertEqual(set(self.host._instances[H.PRIVATE].private_contexts),{self.tab.ctx})
        self.assertFalse(self.pg.is_closed())
        self.assertEqual(len(self.host._instances),1)

    def test_06_owner_off_or_steer_mid_capture_discards_result(self):
        self.html("<p>Private page marker</p>")
        original=C._scan
        async def scan(*a,**kw):
            result=await original(*a,**kw);self.desk["present"]=False;return result
        with patch.object(C,"_scan",side_effect=scan):
            result=self.read();self.assertTrue(result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)
        self.desk["present"]=True
        async def stale(*a,**kw):
            result=await original(*a,**kw);self.valid=False;return result
        with patch.object(C,"_scan",side_effect=stale):self.assertTrue(self.read().refused)

    def test_07_budget_refuses_without_image(self):
        self.html("<p>"+"x"*(S.RAW_CAP+1)+"</p>")
        result=self.read();self.assertIn("too large",result.refused);self.assertIsNone(result.image)

    def test_08_no_page_content_in_bus_or_logs(self):
        marker="BR5-private-visible-fixture"
        self.html(f"<title>{marker}</title><p>{marker}</p>");result=self.read()
        self.assertFalse(result.refused,result.refused);self.assertIn(marker,result.text)
        events=[row for row in self.records if row.get("kind")=="browser_viewed"]
        self.assertTrue(events);self.assertTrue(all(set(row["data"])=={"tab_id","at","refused"} for row in events))
        self.assertNotIn(marker,json.dumps(self.records)+"\n".join(self.logs))

    def test_09_same_url_reload_during_capture_discards_without_front_report(self):
        self.html("<p>Before reload</p>")
        original=C._scan;previous=self.snap.document_generation
        async def reload(*args,**kwargs):
            result=await original(*args,**kwargs)
            await self.pg.reload(wait_until="load")
            return result
        with patch.object(C,"_scan",new=reload):result=self.read()
        self.assertIn("changed",result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)
        self.assertGreater(self.host.pane_tab(self.tab.id)["document_generation"],previous)
        self.assertEqual(self.pg.url,self.snap.url)

    def test_10_terminal_readability_revoked_during_scan_discards(self):
        self.snap=replace(self.snap,source=ResolvedSource("terminal",terminal_id="fixture-terminal"))
        readable=[True];original=C._scan
        async def revoke(*args,**kwargs):
            result=await original(*args,**kwargs);readable[0]=False;return result
        with patch.object(self.daemon.terminals,"readable_source",side_effect=lambda _:readable[0]),patch.object(C,"_scan",new=revoke):
            result=self.read()
        self.assertIn("terminal",result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)

    def test_11_direct_lease_revocation_discards_and_retires_hidden_context(self):
        self.snap=replace(self.snap,mode="direct",tab_id=None,view_id="view-direct-fixture",page_identity=None,document_generation=None)
        original=C._scan
        async def revoke(*args,**kwargs):
            result=await original(*args,**kwargs)
            self.tab.loopback_port=None;self.tab.pending_port=None;self.host._sync_ports(self.tab)
            return result
        with patch.object(C,"_scan",new=revoke):result=self.read()
        self.assertTrue(result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)
        self.assertFalse(self.host.loopback_open(self.fx));self.assertEqual(self.host._look_reserved,set())
        self.assertEqual(set(self.host._instances[H.PRIVATE].private_contexts),{self.tab.ctx})

    def test_12_ref_node_budgets_and_inaccessible_frame_masks(self):
        self.html("<div style='display:flex;flex-wrap:wrap'>"+"<button style='width:20px;height:20px'>x</button>"*(S.REF_CAP+1)+"</div>")
        self.assertIn("too large",self.read().refused)
        self.html("<div>"+"<span></span>"*(S.NODE_CAP+1)+"</div>")
        self.assertIn("too large",self.read().refused)
        self.html(f"<iframe style='position:absolute;left:20px;top:50px;width:250px;height:100px' srcdoc='<p>{MARKER}</p>'></iframe><p>Outside frame</p>")
        result=self.read();self.assertFalse(result.refused,result.refused);self.assertNotIn(MARKER,result.text)
        from PIL import Image
        with Image.open(BytesIO(base64.b64decode(result.image))) as image:self.assertEqual(image.getpixel((80,70)),(24,26,29))

    def test_13_input_hold_and_close_during_capture_discard(self):
        original=C._scan
        async def hold(*args,**kwargs):
            result=await original(*args,**kwargs);self.valid=False;return result
        with patch.object(self.host,"input_held",side_effect=lambda:not self.valid),patch.object(C,"_scan",new=hold):
            self.assertTrue(self.read().refused)
        self.valid=True
        async def close(*args,**kwargs):
            result=await original(*args,**kwargs);await self.pg.close();return result
        with patch.object(C,"_scan",new=close):
            result=self.read();self.assertTrue(result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)

    def test_14_open_shadow_host_visibility_and_direct_text(self):
        self.desk["images"]=False
        for style in ("opacity:0", "filter:opacity(0)", "clip-path:inset(50%)",
                      "position:absolute;clip:rect(0,0,0,0)", "display:none"):
            with self.subTest(style=style):
                self.html("""<p>Public paragraph</p><div id='hidden'></div><div id='nested'></div><div id='visible'></div>
                <script>(()=>{
                hidden.style.cssText=STYLE;
                hidden.attachShadow({mode:'open'}).innerHTML='HIDDEN-DIRECT<p>HIDDEN-CHILD</p>';
                const outer=nested.attachShadow({mode:'open'});
                outer.innerHTML='<div id="wrapper"><div id="inner"></div></div>';
                outer.querySelector('#wrapper').style.cssText=STYLE;
                outer.querySelector('#inner').attachShadow({mode:'open'}).innerHTML='HIDDEN-NESTED-DIRECT<p>HIDDEN-NESTED-CHILD</p>';
                visible.attachShadow({mode:'open'}).innerHTML='VISIBLE-DIRECT<p>VISIBLE-CHILD</p><div id="inner"></div>';
                visible.shadowRoot.querySelector('#inner').attachShadow({mode:'open'}).innerHTML='VISIBLE-NESTED-DIRECT<p>VISIBLE-NESTED-CHILD</p>';
                })();</script>""".replace("STYLE",json.dumps(style)))
                result=self.read();self.assertFalse(result.refused,result.refused)
                for text in ("VISIBLE-DIRECT", "VISIBLE-CHILD", "VISIBLE-NESTED-DIRECT", "VISIBLE-NESTED-CHILD"):
                    self.assertIn(text,result.text)
                for text in ("HIDDEN-DIRECT", "HIDDEN-CHILD", "HIDDEN-NESTED-DIRECT", "HIDDEN-NESTED-CHILD"):
                    self.assertNotIn(text,result.text)


class RegistryReal(Real):
    """The actual registry and actual Chrome, without a front-report double."""
    def setUp(self):
        super().setUp()
        self.now=1.0
        self.registry=BrowserPanes(self.daemon,clock=lambda:self.now)
        self.daemon.browser_panes=self.registry
        self.window="a"*32;self.registry.window_started(self.window)
        self.body={"window":self.window,"seq":1,"focus_seq":1,"focused_slot":2,"covered":False,
            "panes":[{"pane_id":5,"slot":2,"drawn":True,"profile":"private","ctx":self.tab.ctx,
                "see":True,"drive":False,"active":"aaaaaa", "tabs":[{"id":"aaaaaa","mode":"remote",
                    "tab_id":self.tab.id,"source":None}]}]}

    def test_14_real_registry_resolves_displayed_slot_and_event_identity(self):
        self.html("<p>Registry capture fixture</p>")
        for pid,slot in ((1,1),(2,3)):
            self.body["panes"].append({"pane_id":pid,"slot":slot,"drawn":True,"profile":"private",
                "ctx":None,"see":True,"drive":False,"active":None,"tabs":[]})
        receipt=self.registry.report(self.body)
        result=self.read();self.assertFalse(result.refused,result.refused)
        self.assertEqual(self.registry.resolve("2").pane_id,5)
        self.assertIn("Registry capture fixture",result.text)
        events=[r["data"] for r in self.records if r.get("kind")=="browser_viewed"]
        self.assertEqual(events[-1]["tab_id"],self.tab.id)
        self.assertFalse(events[-1]["refused"])
        for selector in ("5",self.tab.id):self.assertTrue(self.service.read(selector,False).refused)
        self.assertIn(self.tab.id,json.dumps(receipt))

    def test_15_real_registry_off_source_and_window_expiry_fail_closed(self):
        self.body["panes"][0]["see"]=False
        self.registry.report(self.body)
        with patch.object(self.host,"capture_look",wraps=self.host.capture_look) as capture:
            self.assertEqual(self.read().refused,"the owner has turned off 'Jarvis can see' for pane 2; ask them, or use Send to chat.")
            self.registry.permissions(5,{"window":self.window,"seq":2,"see":True})
            self.now+=61
            self.assertTrue(self.read().refused)
            capture.assert_not_called()
        self.body["seq"]=3;self.body["panes"][0]["see"]=True
        self.body["panes"][0]["tabs"][0]["source"]={"kind":"unknown"}
        self.registry.report(self.body)
        with patch.object(self.host,"capture_look",wraps=self.host.capture_look) as capture:
            self.assertIn("source",self.read().refused);capture.assert_not_called()

    def test_16_real_registry_source_cannot_be_laundered_by_passive_report(self):
        tab=self.body["panes"][0]["tabs"][0]
        tab["source"]={"kind":"dev_server","entryId":"ds_0123abcd"}
        with patch.object(self.daemon.devservers,"source_snapshot",return_value={"kind":"terminal","terminal_id":"fixture-terminal"}):
            self.registry.report(self.body)
        tab["source"]=None;self.body["seq"]=2;self.registry.report(self.body)
        self.assertEqual(self.registry.resolve("2").source.terminal_id,"fixture-terminal")
        with patch.object(self.daemon.terminals,"readable_source",return_value=False),patch.object(self.host,"capture_look",wraps=self.host.capture_look) as capture:
            self.assertIn("terminal",self.read().refused);capture.assert_not_called()

    def test_17_real_registry_permission_or_window_end_during_scan_discards(self):
        self.registry.report(self.body);original=C._scan
        async def off(*args,**kwargs):
            result=await original(*args,**kwargs)
            self.registry.permissions(5,{"window":self.window,"seq":2,"see":False})
            return result
        with patch.object(C,"_scan",new=off):
            result=self.read();self.assertTrue(result.refused);self.assertEqual(result.text,"");self.assertIsNone(result.image)
        self.registry.permissions(5,{"window":self.window,"seq":3,"see":True})
        async def end(*args,**kwargs):
            result=await original(*args,**kwargs);self.registry.window_ended(self.window);return result
        with patch.object(C,"_scan",new=end):self.assertTrue(self.read().refused)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(cls(name) for cls in (Pure,Lifecycle,Real,RegistryReal) for name in sorted(cls.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

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
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config,llm,runtime,tools
from jarvis.v2.browser import host as H, look as L, look_capture as C, look_scan as S
from jarvis.v2.browser.panes import PaneSnapshot,ResolvedSource,PaneRefused
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
        self.snap=snapshot(tab_id=self.tab.id,view_id=self.tab.id,ctx=self.tab.ctx,url=self.url,page_identity=id(self.pg))
        self.valid=True
        self.daemon.browser_panes=SimpleNamespace(resolve=lambda pane:self.snap,
            still_current=lambda snap:self.valid and snap==self.snap and self.pg.url==self.url)
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


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(cls(name) for cls in (Pure,Real) for name in sorted(cls.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

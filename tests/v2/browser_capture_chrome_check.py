"""Owner captures against real isolated Chrome; synthetic fixture pages only."""
from __future__ import annotations
import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import captures as C, look_capture as LC
from tests.v2.hud_browser_view_check import RegistryReal, MARKER


class RealCaptures(RegistryReal):
    def setUp(self):
        super().setUp()
        self.body["panes"][0]["see"]=False
        self.body["panes"][0]["tabs"][0]["source"]={"kind":"unknown"}
        self.registry.report(self.body)
        self.daemon.browser_captures=C.Captures()
        self.query=dict(window=self.window,pane="5",local_tab="aaaaaa",view=self.tab.id,whole="0")

    def capture(self,whole=False):
        return C.capture_page(self.daemon,self.tab.id,{**self.query,"whole":"1" if whole else "0"})

    def test_owner_still_matches_visible_text_masks_and_keeps_immutable_reference(self):
        from PIL import Image
        self.html(f"""<title>BR4 owner fixture</title><p>Visible chosen text</p>
        <input style='position:absolute;left:20px;top:50px;width:250px;height:40px' value='{MARKER}'>
        <div id='shadow'></div><div style='margin-top:1500px'>Whole-page chosen text</div>
        <script>shadow.attachShadow({{mode:'open'}}).textContent='Open shadow chosen text';</script>""")
        owners=deepcopy(self.host._port_owners)
        result=self.capture()
        text=self.daemon.browser_captures.resolve(result["capture_id"],owner=True)
        self.assertIn("Visible chosen text",text);self.assertIn("Open shadow chosen text",text)
        self.assertNotIn("Whole-page chosen text",text);self.assertNotIn(MARKER,text)
        self.assertFalse(result["whole"])
        with Image.open(BytesIO(base64.b64decode(result["image"]["data_b64"]))) as image:
            self.assertEqual(image.size,(result["image"]["width"],result["image"]["height"]))
            k=image.width/1280   # page CSS px → image px (RASTER_SCALE, then the edge cap)
            self.assertEqual(image.getpixel((round(80*k),round(70*k))),(24,26,29))
            # The far corner: covered only by a mask scaled with the image.
            self.assertEqual(image.getpixel((round(265*k),round(85*k))),(24,26,29))
        whole=self.capture(True)
        self.assertTrue(whole["whole"])
        self.assertIn("Whole-page chosen text",self.daemon.browser_captures.resolve(whole["capture_id"],owner=True))
        self.html("<title>Changed after capture</title><p>Later page words</p>")
        retained=self.daemon.browser_captures.resolve(result["capture_id"],owner=True)
        self.assertIn("Visible chosen text",retained);self.assertNotIn("Later page words",retained)
        self.assertEqual(self.host._port_owners,owners)
        snap=self.registry.resolve("2")
        self.assertFalse(snap.see);self.assertEqual(snap.source.kind,"unknown")
        self.assertFalse(self.registry.still_current(snap))
        self.assertNotIn("Visible chosen text",json.dumps(self.records)+"\n".join(self.logs))

    def test_credentials_and_heuristic_refuse_image_and_text_together(self):
        for html in (f"<p>{MARKER}</p>",f"<title>{'x'*5000}{MARKER}</title><p>Safe</p>",
                     "<p>Public</p><p>TOKEN=abcdef0123456789ABCDEFfedcba987654</p>"):
            with self.subTest(kind=html[:20]):
                self.html(html)
                with self.assertRaisesRegex(C.CaptureRefused,"credential"):self.capture()
                self.assertEqual(self.daemon.browser_captures._rows,{})

    def test_navigation_permission_cover_and_closed_window_during_scan_discard(self):
        for change in ("reload","permission","covered","window"):
            with self.subTest(change=change):
                self.html("<p>Before owner capture race</p>")
                self.body["seq"]+=1;self.body["covered"]=False;self.body["panes"][0]["see"]=False
                self.registry.report(self.body)
                original=LC._scan;done=False
                async def scan(*a,**kw):
                    nonlocal done
                    result=await original(*a,**kw)
                    if not done:
                        done=True
                        if change=="reload":await self.pg.reload(wait_until="load")
                        elif change=="permission":
                            self.body["seq"]+=1
                            self.registry.permissions(5,{"window":self.window,"seq":self.body["seq"],"see":True})
                        elif change=="covered":
                            self.body["seq"]+=1;self.body["covered"]=True;self.registry.report(self.body)
                        else:self.registry.window_ended(self.window)
                    return result
                with patch.object(LC,"_scan",new=scan),self.assertRaises(C.CaptureRefused):self.capture()
                self.assertEqual(self.daemon.browser_captures._rows,{})

    def test_transient_approval_card_invalidates_capture_even_after_it_is_gone(self):
        self.html("<p>Before transient card</p>")
        epoch=self.host.capture_hold_epoch();original=LC._scan
        async def scan(*a,**kw):
            result=await original(*a,**kw)
            self.host._observe({"kind":"approval_requested"})
            return result
        with patch.object(LC,"_scan",new=scan),self.assertRaisesRegex(C.CaptureRefused,"approval"):
            self.capture()
        self.assertFalse(self.host.input_held());self.assertGreater(self.host.capture_hold_epoch(),epoch)
        self.assertEqual(self.daemon.browser_captures._rows,{})

    def test_owner_edge_is_1920_and_does_not_change_agent_edge(self):
        self.run_on(self.pg.set_viewport_size({"width":2400,"height":1200}))
        self.html("<p>Large owner viewport</p>")
        result=self.capture()
        self.assertEqual((result["image"]["width"],result["image"]["height"]),(1920,960))
        self.assertLess(len(base64.b64decode(result["image"]["data_b64"])),4*1024*1024)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(RealCaptures(name) for name in sorted(RealCaptures.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

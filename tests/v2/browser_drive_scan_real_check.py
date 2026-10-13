"""Actual isolated-world action facts; no provider or agent input is invoked."""
import asyncio
import json
import secrets
import unittest
from tests.v2 import hud_browser_view_check as V
from jarvis.v2.browser import drive_scan as D, look_scan as S, look_capture as C
from jarvis.v2.browser.look import LookRefused
from jarvis.v2 import terminal_guard

class Real(V.Real):
    def setUp(self):
        super().setUp()
        self.desk["images"]=False

    async def _evaluate(self, expression, args, world):
        cdp=await self.host._ensure_cdp(self.tab)
        result=await cdp.send("Runtime.evaluate",{
            "expression":"("+expression+")("+json.dumps(args)+")",
            "contextId":world,"returnByValue":True,"silent":True,"timeout":5000})
        if result.get("exceptionDetails"):
            raise AssertionError("isolated scan JavaScript failed: "+str(result["exceptionDetails"]))
        return result["result"]["value"]

    async def _prepare(self, action="click", label="Go", forms=(), between=None):
        cdp=await self.host._ensure_cdp(self.tab)
        frame=await C._frame(cdp)
        world=(await cdp.send("Page.createIsolatedWorld",{
            "frameId":frame[0],"worldName":"jarvis-look","grantUniveralAccess":False}))["executionContextId"]
        args={"url":self.pg.url,"serial":secrets.token_hex(16),"whole":False,"replace":True,
              "nodeCap":S.NODE_CAP,"rawCap":S.RAW_CAP,"refCap":S.REF_CAP}
        look=await self._evaluate(S.SCAN_JS,args,world)
        if look.get("incomplete"):raise LookRefused("incomplete fixture look")
        target=next((row["ref"] for row in look["refs"] if row["label"]==label),"")
        if label:self.assertTrue(target,"the successful look must expose the chosen visible target")
        await cdp.send("Runtime.evaluate",{"expression":"globalThis.__jarvisLook.observer.disconnect()","contextId":world})
        if between:await between()
        args.update(replace=False,target=target,action=action)
        raw=await self._evaluate(D.PREPARE_JS,args,world)
        return D.prepare_result(raw,target,forms),raw

    def prepare(self, *args, **kwargs):return self.run_on(self._prepare(*args,**kwargs))

    def test_form_association_and_labels_never_control_values(self):
        self.html("""<title>Form facts</title><form id='ordinary' action='/submit' method='post'>
        <label for='find'>Search</label><input id='find' name='find' type='search' value='VALUE-SHOULD-NOT-READ'>
        <button>Go</button></form><input form='ordinary' type='password' name='password' value='PASSWORD-SHOULD-NOT-READ'>
        <textarea form='ordinary'>TEXTAREA-SHOULD-NOT-READ</textarea>""")
        prepared,raw=self.prepare()
        self.assertTrue(prepared.target.submits)
        self.assertEqual(prepared.form.method,"post")
        self.assertTrue(any(field.type=="password" for field in prepared.form.fields))
        self.assertTrue(any(field.tag=="textarea" for field in prepared.form.fields))
        self.assertEqual(next(field.label for field in prepared.form.fields if field.type=="search"),"Search")
        for marker in ("VALUE-SHOULD-NOT-READ","PASSWORD-SHOULD-NOT-READ","TEXTAREA-SHOULD-NOT-READ"):
            self.assertNotIn(marker,json.dumps(raw))

    def test_attribute_secret_tail_is_checked_full_before_fingerprint(self):
        marker="synthetic-attribute-secret-abcdefghijklmnop"
        self.html("<button name='"+"x"*10000+marker+"'>Go</button>")
        with self.assertRaisesRegex(LookRefused,"credential"):
            self.prepare(forms=terminal_guard.value_forms([marker]))

    def test_focus_facts_and_cross_origin_frame_refusal(self):
        self.html("<input aria-label='Find' type='password'><iframe src='about:blank'></iframe>")
        self.run_on(self.pg.locator("input").focus())
        prepared,_=self.prepare(action="press",label="")
        self.assertEqual(prepared.target.type,"password");self.assertTrue(prepared.focused_target)
        self.run_on(self.pg.locator("iframe").evaluate("e=>e.focus()"))
        with self.assertRaises(LookRefused):self.prepare(action="press",label="")

    def test_removed_target_and_changed_type_are_not_original_facts(self):
        self.html("<input aria-label='Go' type='search'>")
        async def sensitive():await self.pg.locator("input").evaluate("e=>e.type='password'")
        prepared,_=self.prepare(action="type",between=sensitive)
        self.assertEqual(prepared.target.type,"password")
        self.html("<button>Go</button>")
        async def remove():await self.pg.locator("button").evaluate("e=>e.remove()")
        with self.assertRaises(LookRefused):self.prepare(between=remove)

    def test_select_labels_not_values_and_hidden_form_field_is_kept(self):
        self.html("""<form><select aria-label='Go'><option value='PRIVATE-OPTION-VALUE'>First label</option>
        <optgroup disabled><option value='PRIVATE-SECOND-VALUE'>Second label</option></optgroup></select>
        <input type='password' hidden></form>""")
        prepared,raw=self.prepare(action="select")
        self.assertEqual(prepared.options,((0,"First label",False),(1,"Second label",True)))
        self.assertTrue(any(field.type=="password" for field in prepared.form.fields))
        self.assertNotIn("PRIVATE-OPTION-VALUE",json.dumps(raw));self.assertNotIn("PRIVATE-SECOND-VALUE",json.dumps(raw))

    def test_open_shadow_target_and_composed_hidden_ancestors(self):
        self.html("""<div id='visible'></div><div id='hidden' style='opacity:0'></div>
        <script>visible.attachShadow({mode:'open'}).innerHTML='<div><button>Go</button></div>';
        hidden.attachShadow({mode:'open'}).innerHTML='<button>Hidden</button>';</script>""")
        prepared,raw=self.prepare()
        self.assertEqual(prepared.target.name,"Go")
        self.assertNotIn("Hidden",json.dumps(raw))
        self.run_on(self.pg.locator("#visible").evaluate("e=>e.style.opacity='0'"))
        with self.assertRaises(AssertionError):self.prepare()


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(Real(name) for name in sorted(Real.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

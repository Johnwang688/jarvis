"""BR9b action fact conversion: guards run before any presentation truncation."""
from dataclasses import asdict
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.act_rules import Element
from jarvis.v2.browser.drive_scan import prepare_result
from jarvis.v2.browser.look import LookRefused
from jarvis.v2 import terminal_guard

def row():
    target=asdict(Element(tag="input",type="search",attr_id="find",name="Search"))
    return {"url":"https://fixture.test/","title":"Fixture","raw":["https://fixture.test/","Fixture","Search"],
        "target":target,"form":{"method":"get","action":"https://fixture.test/","fields":[target],"frame_origins":[]},
        "rect":[10,20,110,45],"options":[],"context":"Search the fixture","serial":"a"*32,
        "focusedTarget":False,"incomplete":False}

class Conversion(unittest.TestCase):
    def test_full_attributes_and_form_survive_conversion(self):
        raw=row();raw["target"]["autocomplete"]="one-time-code"
        raw["form"]["fields"].append(asdict(Element(tag="input",type="password",attr_id="external")))
        prepared=prepare_result(raw,"e1",())
        self.assertEqual(prepared.target.autocomplete,"one-time-code")
        self.assertEqual(prepared.form.fields[-1].attr_id,"external")
        self.assertEqual(prepared.rect,(10,20,110,45))
        self.assertFalse(prepared.focused_target)

    def test_full_attribute_tail_changes_fingerprint(self):
        raw=row();raw["target"]["placeholder"]="x"*10000+"original"
        first=prepare_result(raw,"e1",())
        raw["target"]["placeholder"]="x"*10000+"changed"
        self.assertNotEqual(prepare_result(raw,"e1",()).fingerprint,first.fingerprint)

    def test_known_secret_at_long_raw_tail_refuses_before_presentation(self):
        raw=row();secret="synthetic-secret-tail-"+("z"*24)
        raw["raw"].append("x"*20000+secret)
        with self.assertRaises(LookRefused):
            prepare_result(raw,"e1",terminal_guard.value_forms([secret]))

    def test_keyword_withheld_context_retains_ask_floor_flag(self):
        raw=row();raw["raw"].append("TOKEN=abcdef0123456789ABCDEFfedcba987654")
        self.assertTrue(prepare_result(raw,"e1",()).context_flagged)

    def test_incomplete_changed_frame_and_invalid_geometry_refuse(self):
        for key,value in (("incomplete",True),("changed",True),("refused","focused-frame"),
                          ("rect",[0,0,float("nan"),20]),("rect",[2,2,1,1]),
                          ("focusedTarget","yes"),("serial",None)):
            raw=row();raw[key]=value
            with self.subTest(key=key),self.assertRaises(LookRefused):prepare_result(raw,"e1",())

    def test_bad_fields_values_and_oversized_lists_refuse(self):
        for field,value in (("value","owner text"),("maxlength",True),("editable",1)):
            raw=row();raw["target"][field]=value
            with self.subTest(field=field),self.assertRaises(LookRefused):prepare_result(raw,"e1",())
        raw=row();raw["form"]["fields"]=[raw["target"]]*501
        with self.assertRaises(LookRefused):prepare_result(raw,"e1",())

    def test_context_and_options_fingerprint_but_geometry_and_expected_focus_do_not(self):
        raw=row();original=prepare_result(raw,"e1",())
        raw["focusedTarget"]=True;raw["rect"]=[11,21,111,46]
        self.assertEqual(prepare_result(raw,"e1",()).fingerprint,original.fingerprint)
        raw["context"]+=" changed"
        self.assertNotEqual(prepare_result(raw,"e1",()).fingerprint,original.fingerprint)
        raw=row();raw["options"]=[{"index":0,"label":"first","disabled":False}]
        first=prepare_result(raw,"e1",())
        raw["options"][0]["disabled"]=True
        self.assertNotEqual(prepare_result(raw,"e1",()).fingerprint,first.fingerprint)

if __name__=="__main__":unittest.main()


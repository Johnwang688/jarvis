"""BR9b same-binding navigation reconciliation; no production drive permission."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.v2 import browser_panes_check as base
from jarvis.v2.browser.panes import LEASE_S, ResolvedSource

class NavigationBinding(unittest.TestCase):
    def setUp(self):
        self.f=base.Panes();self.f.setUp()
        self.r=self.f.registry
        # Enabled fixture only: production report/permissions/snapshot still
        # never accept or create drive=True.
        self.original=self.r._snapshot
        self.r._snapshot=lambda *args:replace(self.original(*args),drive=True)
        self.r.report(self.f.remote())
        self.before=self.r.resolve("1")

    def test_same_document_and_same_url_advance_allowed_but_strict_look_revoked(self):
        self.assertEqual(self.r.after_navigation(self.before),self.before)
        self.f.rows[base.T]["document_generation"]+=1
        after=self.r.after_navigation(self.before)
        self.assertIsNotNone(after)
        self.assertEqual(after.document_generation,self.before.document_generation+1)
        self.assertFalse(self.r.still_current(self.before))
        self.assertTrue(self.r.still_current(after))

    def test_url_advance_requires_document_advance(self):
        self.f.rows[base.T]["url"]="https://example.test/landed"
        self.assertIsNone(self.r.after_navigation(self.before))
        self.f.rows[base.T]["document_generation"]+=1
        self.assertEqual(self.r.after_navigation(self.before).url,"https://example.test/landed")

    def test_same_url_new_page_and_lowered_generation_refuse(self):
        for field,value in (("page_identity",2),("document_generation",0)):
            old=self.f.rows[base.T][field];self.f.rows[base.T][field]=value
            self.assertIsNone(self.r.after_navigation(self.before))
            self.f.rows[base.T][field]=old

    def test_every_remaining_snapshot_identity_is_pinned(self):
        for field,value in {
            "window":"b"*32,"pane_id":3,"slot":2,"local_tab_id":"bbbbbb",
            "tab_id":"87654321","view_id":"87654321","profile":"p-99999999",
            "ctx":"changed","mode":"direct","generation":900,"see":False,
            "drive":False,"drawn":False,"covered":True,
            "source":ResolvedSource("unknown"),
        }.items():
            with self.subTest(field=field):
                self.assertIsNone(self.r.after_navigation(replace(self.before,**{field:value})))

    def test_actual_permission_visibility_source_and_window_changes_refuse(self):
        state=self.r._windows[base.W];pane=state.panes[4];tab=pane.tabs[base.L]
        for obj,field,value in ((pane,"see",False),(pane,"drawn",False),
                               (state,"covered",True),(tab,"source",ResolvedSource("unknown"))):
            old=getattr(obj,field);object.__setattr__(obj,field,value)
            with self.subTest(field=field):self.assertIsNone(self.r.after_navigation(self.before))
            object.__setattr__(obj,field,old)
        self.r.window_ended(base.W)
        self.assertIsNone(self.r.after_navigation(self.before))

    def test_lease_expiry_and_disappeared_host_tab_refuse(self):
        self.f.now+=LEASE_S+1
        self.assertIsNone(self.r.after_navigation(self.before))
        self.f.now=1
        self.r.report(self.f.remote(2));current=self.r.resolve("1")
        del self.f.rows[base.T]
        self.assertIsNone(self.r.after_navigation(current))

    def test_production_snapshot_is_off_and_cannot_rebind(self):
        self.r._snapshot=self.original
        current=self.r.resolve("1")
        self.assertFalse(current.drive)
        self.assertIsNone(self.r.after_navigation(current))
        self.assertIsNone(self.r.after_navigation(self.before))
        self.assertIsNone(self.r.after_navigation(None))

if __name__=="__main__": unittest.main()


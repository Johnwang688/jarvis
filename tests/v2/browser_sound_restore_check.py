"""Pure replay admission/lease tests; no Chrome, network or private URL copies."""
from dataclasses import replace, FrozenInstanceError
from pathlib import Path
import sys, unittest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser.sound_restore import describe,target,BLANK

class Restore(unittest.TestCase):
    def tab(self,url='https://example.test/synthetic'):
        return SimpleNamespace(id='1234abcd',profile='p-12345678',page_identity=9,opener='abcd1234',
            pane_ready=True,signin=False,loading=False,pending_port=None,loopback_port=None,
            location=(url,'Synthetic title'),page=SimpleNamespace(url=url))
    def describe(self,tab,owns=lambda *_:False,local=False):
        return describe(tab,refused=frozenset({18402}),owns_port=owns,local_servers=local)
    def test_exact_accepted_public_page_and_identity_preserved(self):
        row=self.describe(self.tab());self.assertEqual(row.url,'https://example.test/synthetic')
        self.assertEqual((row.tab_id,row.old_page,row.opener),('1234abcd',9,'abcd1234'))
        self.assertFalse(row.blanked);self.assertIsNone(row.port)
    def test_private_never_collected(self):
        tab=self.tab();tab.profile='private'
        with self.assertRaisesRegex(ValueError,'Private'):self.describe(tab)
    def test_loading_unaccepted_pending_and_privileged_pages_are_inert(self):
        for attr,value in [('loading',True),('pane_ready',False),('pending_port',33333),('signin',True)]:
            tab=self.tab();setattr(tab,attr,value)
            with self.subTest(attr=attr):self.assertEqual(self.describe(tab).url,BLANK)
        for url in ['chrome://settings/syncSetup','chrome-error://chromewebdata/','blob:https://example.test/id','file:///synthetic','about:blank?stale','http://127.0.0.1:18402/synthetic']:
            with self.subTest(url=url):self.assertTrue(self.describe(self.tab(url)).blanked)
        tab=self.tab();tab.page.url='https://other.test/'
        self.assertEqual(self.describe(tab).url,BLANK)
    def test_only_established_exact_tab_port_is_transferred(self):
        tab=self.tab('http://127.0.0.1:33333/synthetic');tab.loopback_port=33333
        row=self.describe(tab,lambda port,tid:port==33333 and tid==tab.id)
        self.assertEqual((row.url,row.port),(tab.page.url,33333))
        self.assertEqual(self.describe(tab).url,BLANK)
        self.assertEqual(self.describe(tab,lambda *_:True,local=False).port,33333)
        tab.pending_port=33333
        self.assertEqual(self.describe(tab,lambda *_:True).url,BLANK)
    def test_unleased_global_override_is_not_converted_to_tab_lease(self):
        tab=self.tab('http://127.0.0.1:33333/synthetic')
        row=self.describe(tab,local=True);self.assertIsNone(row.port);self.assertEqual(row.url,tab.page.url)
        self.assertEqual(target(row,refused=frozenset(),local_servers=False),(BLANK,None,True))
    def test_current_control_port_rejudged_and_existing_lease_not_broadened(self):
        tab=self.tab('http://127.0.0.1:33333/synthetic');tab.loopback_port=33333
        row=self.describe(tab,lambda *_:True)
        self.assertEqual(target(row,refused=frozenset({33333}),local_servers=True),(BLANK,None,True))
        self.assertEqual(target(row,refused=frozenset(),local_servers=False),(tab.page.url,33333,False))
        with self.assertRaises(FrozenInstanceError):row.url='http://127.0.0.1:33334/synthetic'
        row=replace(row,url='http://127.0.0.1:33334/synthetic')
        self.assertEqual(target(row,refused=frozenset(),local_servers=False),(BLANK,None,True))

if __name__=='__main__':unittest.main()

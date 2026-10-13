"""Exact download release, ambiguous provenance, restart and byte-swap guards."""
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import Mock,patch
from jarvis import config
from jarvis.v2.browser import drive_downloads as D,files
from jarvis.v2.browser.host import secure_dir
from jarvis.v2.provider import Decision
from tests.v2.browser_drive_state_check import pane,turn

class Downloads(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='browser-drive-downloads-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.source=self.root/'source';self.source.write_bytes(b'MZdummy-executable')
        patcher=patch.object(config,'BROWSER_DIR',self.root/'browser');patcher.start();self.addCleanup(patcher.stop)
        secure_dir(secure_dir(config.BROWSER_DIR)/'downloads')
        self.approvals=Mock(human_backed=True);self.approvals.ask.return_value=Decision.ALLOW
        self.service=SimpleNamespace(daemon=SimpleNamespace(approvals=self.approvals,browser=Mock()))
        self.manager=D.Downloads(self.service)
        self.guard=Mock(return_value=(pane(),{'hold_count':0}));self.guard.snapshot=pane();self.guard.turn=turn();self.guard.hold_count=0
        self.audit=patch('jarvis.v2.permissions.log_decision').start();self.addCleanup(patch.stopall)
        self.number=0

    def saved(self,*,mime='application/x-msdownload',body=None,name='run.exe',response=True):
        if body is not None:self.source.write_bytes(body)
        self.number+=1;identity=f'{self.number:08x}'
        token=self.manager.begin('tab-'+str(self.number),'a'*32,self.guard,Mock())
        if response:self.manager.response(token,{'response':{'url':'https://test.invalid/download','mimeType':mime}})
        item=self.manager.started(token.tab_id,'https://test.invalid/download')
        row=files.save(str(self.source),'work',name,identity,agent_held=True)
        self.manager.completed(item,row)
        return token,row

    def open(self,row):return files.open_download(row['id'],authorize=self.manager.authorized)

    def test_exact_owner_yes_releases_sealed_bytes_never_executes_or_persists_digest(self):
        token,row=self.saved();notices=self.manager.review(token)
        self.assertIn('not run',' '.join(notices));self.approvals.ask.assert_called_once()
        fd,name,size=self.open(row)
        try:
            self.assertEqual(os.read(fd,size),b'MZdummy-executable')
            with self.assertRaises(OSError):os.write(fd,b'change')
        finally:os.close(fd)
        saved=self.manager.releases[row['id']]
        self.assertEqual((Path(saved.path).parent/files.HELD_MARKER).read_bytes(),b'held\n')
        self.assertNotIn(saved.digest.hex(),repr(saved))
        self.assertEqual(sorted(p.name for p in Path(saved.path).parent.iterdir()),['.agent-held','run.exe'])
        request=self.approvals.ask.call_args.args[0]
        self.assertEqual(request.browser['action'],'release_download');self.assertIsNone(request.browser['rect'])
        self.assertNotIn('run.exe',str(request.to_json()));self.assertFalse(request.allowlistable)
        self.assertEqual(request.max_wait_s,120);self.assertEqual(self.audit.call_args.args[0].reason,'executable_download:owner')

    def test_restart_and_replacement_revoke_exact_release(self):
        token,row=self.saved();self.manager.review(token)
        fresh=D.Downloads(self.service)
        with self.assertRaises(files.FileRefused):files.open_download(row['id'],authorize=fresh.authorized)
        saved=self.manager.releases[row['id']];Path(saved.path).write_bytes(b'MZother-executable')
        with self.assertRaises(files.FileRefused):self.open(row)
        self.assertNotIn(row['id'],self.manager.releases)

    def test_owner_cannot_read_partially_saved_mime_only_agent_file(self):
        self.source.write_bytes(b'ordinary-looking executable bytes')
        original=files.os.write;observed=[]
        def write(fd,data):
            count=original(fd,data)
            path=os.path.realpath('/proc/self/fd/'+str(fd))
            if path.endswith('/notes.txt'):
                rows=files.listing();self.assertEqual(len(rows),1);self.assertTrue(rows[0]['held'])
                with self.assertRaises(files.FileRefused):files.open_download(rows[0]['id'])
                observed.append(True)
            return count
        with patch.object(files.os,'write',side_effect=write):
            files.save(str(self.source),'work','notes.txt','aabbccdd',agent_held=True)
        self.assertTrue(observed,'fixture must inspect while the output descriptor is open')

    def test_mutable_disk_after_open_cannot_change_sealed_response(self):
        token,row=self.saved();self.manager.review(token);fd,_name,size=self.open(row)
        try:
            Path(self.manager.releases[row['id']].path).write_bytes(b'evil-later-content')
            self.assertEqual(os.read(fd,size),b'MZdummy-executable')
        finally:os.close(fd)

    def test_same_metadata_changed_bytes_still_revoke(self):
        token,row=self.saved();self.manager.review(token);saved=self.manager.releases[row['id']]
        Path(saved.path).write_bytes(b'MZother-executable')
        original_lstat=files.os.lstat;original_fstat=files.os.fstat
        def lstat(path,*args,**kwargs):
            return saved.info if str(path)==saved.path else original_lstat(path,*args,**kwargs)
        def fstat(fd):
            return saved.info if os.path.realpath('/proc/self/fd/'+str(fd))==saved.path else original_fstat(fd)
        # Directory stats remain real, so only the content digest can catch
        # this exact-size rewrite within a simulated coarse timestamp tick.
        with patch.object(files.os,'lstat',side_effect=lstat),patch.object(files.os,'fstat',side_effect=fstat):
            self.assertIsNone(self.manager.authorized(row['id'],Path(saved.path),saved.info))

    def test_deny_deletes_only_saved_file(self):
        token,row=self.saved();self.approvals.ask.return_value=Decision.DENY
        unrelated=self.root/'owner.txt';unrelated.write_text('preserve')
        notices=self.manager.review(token)
        self.assertIn('declined and deleted',' '.join(notices));self.assertIsNone(files._find(row['id']))
        self.assertEqual(unrelated.read_text(),'preserve');self.assertEqual(self.manager.releases,{})

    def test_replacement_during_card_is_retained_held(self):
        token,row=self.saved();found=files._find(row['id']);target=files._entry(found[1])
        def deny(*a,**k):
            other=target.parent/'replacement';other.write_bytes(b'not-approved-file');os.replace(other,target);return Decision.DENY
        self.approvals.ask.side_effect=deny;notices=self.manager.review(token)
        self.assertEqual(target.read_bytes(),b'not-approved-file');self.assertIn('cleanup is unfinished',' '.join(notices))
        with self.assertRaises(files.FileRefused):self.open(row)

    def test_stop_after_yes_deletes_file_and_cannot_grant_release(self):
        token,row=self.saved()
        def yes(*a,**k):self.guard.side_effect=RuntimeError('stopped');return Decision.ALLOW
        self.approvals.ask.side_effect=yes;self.manager.review(token)
        self.assertIsNone(files._find(row['id']));self.assertEqual(self.manager.releases,{})

    def test_missing_mime_and_response_overflow_stay_held_without_card(self):
        for mode in ('missing','overflow','duplicate'):
            token,row=self.saved(response=mode!='missing')
            if mode=='overflow':
                for n in range(9):self.manager.response(token,{'response':{'url':f'https://test.invalid/{n}','mimeType':'text/plain'}})
            if mode=='duplicate':self.manager.response(token,{'response':{'url':'https://test.invalid/download','mimeType':'text/plain'}})
            self.manager.review(token);self.manager.end(token)
            self.assertTrue(files._find(row['id']));self.approvals.ask.assert_not_called()
            with self.assertRaises(files.FileRefused):self.open(row)
            self.assertLessEqual(len(token.responses),8)

    def test_mime_only_executable_held_across_restart(self):
        token,row=self.saved(mime='application/x-shellscript',body=b'plain script content',name='notes.txt')
        self.assertTrue(row['held']);self.manager.review(token);self.approvals.ask.assert_called_once()
        self.manager.stop()
        with self.assertRaises(files.FileRefused):self.open(row)
        self.assertTrue(files.listing()[0]['held'])

    def test_ordinary_attributed_file_needs_no_card_but_remains_exact_bytes(self):
        token,row=self.saved(mime='text/plain',body=b'ordinary text',name='notes.txt')
        self.manager.review(token);self.approvals.ask.assert_not_called()
        fd,_name,size=self.open(row)
        try:self.assertEqual(os.read(fd,size),b'ordinary text')
        finally:os.close(fd)

    def test_late_and_overflow_downloads_keep_tombstone_until_tab_closure(self):
        token,row=self.saved()
        for _ in range(10):self.assertIsNotNone(self.manager.started(token.tab_id,'https://test.invalid/download'))
        self.assertEqual(len(token.downloads),4);self.assertTrue(token.ambiguous)
        self.manager.end(token)
        late=self.manager.started(token.tab_id,'https://test.invalid/late')
        self.assertIsNotNone(late);self.assertIsNone(late.token)
        self.assertIsNone(self.manager.started('untouched-owner-tab','https://test.invalid/owner'))
        self.manager.closed_tab(token.tab_id)
        self.assertIsNone(self.manager.started(token.tab_id,'https://test.invalid/new-owner-tab'))

    def test_malformed_hold_marker_never_serves_or_deletes_its_target(self):
        token,row=self.saved(mime='application/x-shellscript',body=b'plain',name='notes.txt')
        target=files._entry(files._find(row['id'])[1]);marker=target.parent/files.HELD_MARKER
        outside=self.root/'unrelated';outside.write_bytes(b'preserve')
        marker.unlink();marker.symlink_to(outside)
        with self.assertRaises(files.FileRefused):self.open(row)
        self.approvals.ask.return_value=Decision.DENY
        notices=self.manager.review(token)
        self.assertEqual(outside.read_bytes(),b'preserve');self.assertTrue(marker.is_symlink())
        self.assertIn('cleanup is unfinished',' '.join(notices));self.assertEqual(self.manager.releases,{})


    def test_large_release_stays_held_without_unbounded_read(self):
        token,row=self.saved();target=files._entry(files._find(row['id'])[1])
        with target.open('wb') as output:output.truncate(D.RELEASE_CAP+1)
        self.manager.review(token);self.approvals.ask.assert_not_called()
        with self.assertRaises(files.FileRefused):self.open(row)

if __name__=='__main__':unittest.main()

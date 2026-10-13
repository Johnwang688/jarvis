"""Exact-file Browser upload races and private-copy lifecycle."""
from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from jarvis import config
from jarvis.v2.browser import drive_files as F,files
from jarvis.v2.browser.look import LookRefused

class Uploads(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='browser-upload-drive-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.file=self.project/'report.txt';self.file.write_bytes(b'owner-approved-file')
        self.browser=self.root/'browser';patcher=patch.object(config,'BROWSER_DIR',self.browser)
        patcher.start();self.addCleanup(patcher.stop)

    def test_exact_bytes_are_immutable_private_and_need_no_disk_copy(self):
        approved=F.judge_upload('report.txt',str(self.project))
        loaded=F.load_upload(approved,'report.txt',str(self.project),lambda:None)
        self.assertIsInstance(loaded.data,bytes);self.assertEqual(loaded.data,b'owner-approved-file')
        self.assertEqual(loaded.name,'report.txt');self.assertNotIn('owner-approved',repr(loaded))
        self.file.unlink()
        self.assertEqual(loaded.data,b'owner-approved-file')
        self.assertFalse(self.browser.exists())

    def test_post_yes_replacement_same_inode_rewrite_and_hardlink_refuse(self):
        for mode in ('replace','rewrite','hardlink','symlink'):
            self.file.write_bytes(b'owner-approved-file');approved=F.judge_upload('report.txt',str(self.project))
            other=self.project/'other.txt';other.write_bytes(b'other-private-file')
            if mode=='replace':os.replace(other,self.file)
            elif mode=='rewrite':self.file.write_bytes(b'changed-during-copy')
            elif mode=='hardlink':os.link(self.file,self.project/'alias.txt')
            else:self.file.unlink();self.file.symlink_to(other)
            with self.subTest(mode=mode),self.assertRaises(LookRefused):
                F.load_upload(approved,'report.txt',str(self.project),lambda:None)
            self.assertFalse((self.browser/'drive-staging').exists())
            self.file.unlink()
            alias=self.project/'alias.txt'
            if alias.exists():alias.unlink()

    def test_file_change_during_read_is_refused_by_shared_descriptor_check(self):
        approved=F.judge_upload('report.txt',str(self.project));original=files.os.read;changed=False
        def read(fd,n):
            nonlocal changed
            out=original(fd,n)
            if out and not changed and os.path.realpath(f"/proc/self/fd/{fd}")==str(self.file):
                changed=True;self.file.write_bytes(b'changed-during-copy')
            return out
        with patch.object(files.os,'read',side_effect=read),self.assertRaises(LookRefused):
            F.load_upload(approved,'report.txt',str(self.project),lambda:None)
        self.assertTrue(changed, 'the concurrent write fixture must actually modify the opened source')
        self.assertFalse(self.browser.exists())

    def test_same_metadata_different_bytes_cannot_use_earlier_approval(self):
        approved=F.judge_upload('report.txt',str(self.project))
        self.file.write_bytes(b'changed-during-copy')
        # Model a coarse timestamp tick deterministically; all stat fields
        # presented to the checker remain the originally approved fields.
        original=F.upload_refusal
        def judge(*args,**kwargs):
            result=original(*args,**kwargs)
            if kwargs.get('judged'):kwargs['judged'][:]=[approved.info]
            return result
        with patch.object(F,'upload_refusal',side_effect=judge),patch.object(files.os,'fstat',return_value=approved.info):
            with self.assertRaisesRegex(LookRefused,'contents changed'):
                F.load_upload(approved,'report.txt',str(self.project),lambda:None)
        self.assertFalse(self.browser.exists())

    def test_approved_digest_binds_the_returned_buffer_and_never_appears_in_repr(self):
        approved=F.judge_upload('report.txt',str(self.project))
        self.assertNotIn(approved.digest.hex(),repr(approved))
        loaded=F.load_upload(approved,'report.txt',str(self.project),lambda:None)
        import hashlib
        self.assertEqual(hashlib.sha256(loaded.data).digest(),approved.digest)

    def test_stop_after_read_creates_no_private_copy(self):
        approved=F.judge_upload('report.txt',str(self.project));calls=0
        def stopped():
            nonlocal calls
            calls+=1
            if calls==2:raise LookRefused('stopped')
        with self.assertRaises(LookRefused):
            F.load_upload(approved,'report.txt',str(self.project),stopped)
        self.assertFalse(self.browser.exists())

    def test_hidden_and_outside_project_sources_refuse_before_copy(self):
        hidden=self.project/'.private';hidden.write_text('dummy')
        outside=self.root/'outside.txt';outside.write_text('dummy')
        for name in ('.private','../outside.txt'):
            with self.assertRaises(LookRefused):F.judge_upload(name,str(self.project))
        self.assertFalse(self.browser.exists())

    def test_staging_parent_or_ancestor_swap_cannot_redirect_an_upload_write(self):
        for swap_ancestor in (False,True):
            with self.subTest(ancestor=swap_ancestor):
                outside=self.root/('outside-'+str(swap_ancestor));outside.mkdir()
                parent=self.root/('state-'+str(swap_ancestor));parent.mkdir()
                browser=parent/'browser';browser.mkdir();(browser/'drive-staging').mkdir()
                approved=F.judge_upload('report.txt',str(self.project));calls=0
                def current():
                    nonlocal calls
                    calls+=1
                    if calls==2:
                        target=parent if swap_ancestor else browser/'drive-staging'
                        target.rename(target.with_name(target.name+'-original'));target.symlink_to(outside,target_is_directory=True)
                with patch.object(config,'BROWSER_DIR',browser):
                    loaded=F.load_upload(approved,'report.txt',str(self.project),current)
                self.assertEqual(loaded.data,b'owner-approved-file')
                self.assertEqual(list(outside.iterdir()),[])

    def test_startup_sweep_only_private_single_file_folders(self):
        from jarvis.v2.browser.host import secure_dir
        root=secure_dir(secure_dir(self.browser)/'drive-staging')
        owned=secure_dir(root/('a'*32));(owned/'report.txt').write_bytes(b'crash-leftover');(owned/'report.txt').chmod(0o600)
        unknown=secure_dir(root/('b'*32));(unknown/'link').symlink_to(self.file)
        multiple=secure_dir(root/('c'*32));(multiple/'one').write_text('1');(multiple/'two').write_text('2')
        self.assertFalse(F.sweep_staging())
        self.assertFalse(owned.exists());self.assertTrue((unknown/'link').is_symlink())
        self.assertTrue((multiple/'one').exists());self.assertEqual(self.file.read_bytes(),b'owner-approved-file')

    def test_sweep_refuses_ancestor_symlinks_and_swaps_after_open(self):
        from jarvis.v2.browser.host import secure_dir
        for during in (False,True):
            with self.subTest(during=during):
                parent=secure_dir(self.root/('state-'+str(during)));browser=secure_dir(parent/'browser')
                root=secure_dir(browser/'drive-staging');folder=secure_dir(root/('a'*32))
                target=folder/'report.txt';target.write_bytes(b'preserve-on-race');target.chmod(0o600)
                outside=secure_dir(self.root/('elsewhere-'+str(during)))
                outside_browser=secure_dir(outside/'browser');outside_root=secure_dir(outside_browser/'drive-staging')
                outside_folder=secure_dir(outside_root/('a'*32));foreign=outside_folder/'report.txt'
                foreign.write_bytes(b'foreign');foreign.chmod(0o600)
                old=parent.with_name(parent.name+'-old');changed=False
                def swap():
                    nonlocal changed
                    if not changed:parent.rename(old);parent.symlink_to(outside,target_is_directory=True);changed=True
                original=F.os.scandir
                def scan(fd):
                    result=original(fd)
                    if during and isinstance(fd,int):swap()
                    return result
                if not during:swap()
                with patch.object(config,'BROWSER_DIR',browser),patch.object(F.os,'scandir',side_effect=scan):
                    self.assertFalse(F.sweep_staging())
                self.assertEqual(foreign.read_bytes(),b'foreign')
                self.assertEqual((old/'browser/drive-staging'/('a'*32)/'report.txt').read_bytes(),b'preserve-on-race')


    def test_missing_already_pinned_ancestor_is_not_reported_clean(self):
        from jarvis.v2.browser.host import secure_dir
        parent=secure_dir(self.root/'state');browser=secure_dir(parent/'browser')
        root=secure_dir(browser/'drive-staging');folder=secure_dir(root/('a'*32))
        (folder/'report.txt').write_bytes(b'keep');(folder/'report.txt').chmod(0o600)
        renamed=parent.with_name('old-state');original=F.os.fstat;changed=False
        def fstat(fd):
            nonlocal changed
            info=original(fd)
            if not changed and os.path.realpath('/proc/self/fd/'+str(fd))==str(root):
                parent.rename(renamed);changed=True
            return info
        with patch.object(config,'BROWSER_DIR',browser),patch.object(F.os,'fstat',side_effect=fstat):
            self.assertFalse(F.sweep_staging())
        self.assertTrue(changed)
        self.assertEqual((renamed/'browser/drive-staging'/('a'*32)/'report.txt').read_bytes(),b'keep')

if __name__=='__main__':unittest.main()

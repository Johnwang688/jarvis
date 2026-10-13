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

    def test_exact_bytes_are_private_and_removed_after_success_or_failure(self):
        for fail in (False,True):
            approved=F.judge_upload('report.txt',str(self.project))
            try:
                with F.stage_upload(approved,'report.txt',str(self.project),lambda:None) as name:
                    staged=Path(name)
                    self.assertNotEqual(staged,self.file);self.assertEqual(staged.read_bytes(),b'owner-approved-file')
                    self.assertEqual(stat.S_IMODE(staged.stat().st_mode),0o600)
                    self.assertEqual(stat.S_IMODE(staged.parent.stat().st_mode),0o700)
                    if fail:raise RuntimeError('scripted stop')
            except RuntimeError:
                self.assertTrue(fail)
            self.assertFalse(staged.exists());self.assertFalse(staged.parent.exists())
            self.assertEqual(self.file.read_bytes(),b'owner-approved-file')

    def test_post_yes_replacement_same_inode_rewrite_and_hardlink_refuse(self):
        for mode in ('replace','rewrite','hardlink','symlink'):
            self.file.write_bytes(b'owner-approved-file');approved=F.judge_upload('report.txt',str(self.project))
            other=self.project/'other.txt';other.write_bytes(b'other-private-file')
            if mode=='replace':os.replace(other,self.file)
            elif mode=='rewrite':self.file.write_bytes(b'changed-during-copy')
            elif mode=='hardlink':os.link(self.file,self.project/'alias.txt')
            else:self.file.unlink();self.file.symlink_to(other)
            with self.subTest(mode=mode),self.assertRaises(LookRefused):
                with F.stage_upload(approved,'report.txt',str(self.project),lambda:None):self.fail('unsafe upload reached Chrome')
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
            with F.stage_upload(approved,'report.txt',str(self.project),lambda:None):self.fail('changed bytes used')
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
                with F.stage_upload(approved,'report.txt',str(self.project),lambda:None):self.fail('reused content approval')
        self.assertFalse(self.browser.exists())

    def test_approved_digest_binds_the_staged_buffer_and_never_appears_in_repr(self):
        approved=F.judge_upload('report.txt',str(self.project))
        self.assertNotIn(approved.digest.hex(),repr(approved))
        with F.stage_upload(approved,'report.txt',str(self.project),lambda:None) as name:
            import hashlib
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).digest(),approved.digest)

    def test_stop_after_read_creates_no_private_copy(self):
        approved=F.judge_upload('report.txt',str(self.project));calls=0
        def stopped():
            nonlocal calls
            calls+=1
            if calls==2:raise LookRefused('stopped')
        with self.assertRaises(LookRefused):
            with F.stage_upload(approved,'report.txt',str(self.project),stopped):self.fail('stopped upload used')
        self.assertFalse(self.browser.exists())

    def test_hidden_and_outside_project_sources_refuse_before_copy(self):
        hidden=self.project/'.private';hidden.write_text('dummy')
        outside=self.root/'outside.txt';outside.write_text('dummy')
        for name in ('.private','../outside.txt'):
            with self.assertRaises(LookRefused):F.judge_upload(name,str(self.project))
        self.assertFalse(self.browser.exists())

    def test_replaced_staging_file_is_retained_as_unfinished_cleanup(self):
        approved=F.judge_upload('report.txt',str(self.project))
        with self.assertRaisesRegex(LookRefused,'cleanup'):
            with F.stage_upload(approved,'report.txt',str(self.project),lambda:None) as name:
                staged=Path(name);other=staged.parent/'other';other.write_bytes(b'not-the-staged-inode');os.replace(other,staged)
        self.assertEqual(staged.read_bytes(),b'not-the-staged-inode')

    def test_startup_sweep_only_private_single_file_folders(self):
        from jarvis.v2.browser.host import secure_dir
        root=secure_dir(secure_dir(self.browser)/'drive-staging')
        owned=secure_dir(root/('a'*32));(owned/'report.txt').write_bytes(b'crash-leftover');(owned/'report.txt').chmod(0o600)
        unknown=secure_dir(root/('b'*32));(unknown/'link').symlink_to(self.file)
        multiple=secure_dir(root/('c'*32));(multiple/'one').write_text('1');(multiple/'two').write_text('2')
        self.assertFalse(F.sweep_staging())
        self.assertFalse(owned.exists());self.assertTrue((unknown/'link').is_symlink())
        self.assertTrue((multiple/'one').exists());self.assertEqual(self.file.read_bytes(),b'owner-approved-file')

    def test_cancel_during_private_copy_removes_only_staging(self):
        approved=F.judge_upload('report.txt',str(self.project));calls=0
        def current():
            nonlocal calls
            calls+=1
            if calls==3:raise LookRefused('stopped while copying')
        with self.assertRaisesRegex(LookRefused,'stopped'):
            with F.stage_upload(approved,'report.txt',str(self.project),current):self.fail('copy continued after stop')
        self.assertEqual(list((self.browser/'drive-staging').iterdir()),[])
        self.assertEqual(self.file.read_bytes(),b'owner-approved-file')

if __name__=='__main__':unittest.main()

"""Refused page attention cannot outlive its exact-instance cleanup hold."""
from pathlib import Path
import asyncio, sys, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import activity as A, attention_cleanup as C, host as H
from tests.v2 import browser_host_activity_check as fixtures

class Cleanup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.Host.setUp(self);self.releases=[]
    async def asyncTearDown(self):
        for event in self.releases:event.set()
        await self.host._attention_cleanup.settle()
        await fixtures.Host.asyncTearDown(self)
    def batch(self):return self.host._attention_cleanup._batches[self.profile]
    async def test_cap_refusal_dismisses_without_publishing_and_blocks_new_generation(self):
        entered=asyncio.Event();finish=asyncio.Event();self.releases.append(finish)
        async def dismiss():entered.set();await finish.wait()
        dialog=SimpleNamespace(dismiss=dismiss)
        with patch.object(A,'MAX_ACTIVITIES',0):self.host._dialog(self.tab,dialog)
        self.assertIn(self.profile,self.host._attention_cleanup._batches)
        await entered.wait();self.assertEqual(self.tab.dialogs,{})
        self.host.activity.desired(2)
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,2))
        task=self.batch().task;finish.set();await task
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)
        self.assertIsNotNone(self.host.activity.try_restart(self.profile,1,2))
    async def test_refused_download_is_canceled_deleted_never_attributed(self):
        download=SimpleNamespace(cancel=AsyncMock(),delete=AsyncMock())
        with patch.object(A,'MAX_ACTIVITIES',0):self.host._start_download(self.tab,download)
        task=self.batch().task;await task
        download.cancel.assert_awaited_once();download.delete.assert_awaited_once()
        self.assertEqual(self.host._download_tasks,{})
    async def test_cancel_keeps_hold_through_actual_child_finally(self):
        entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event();self.releases.append(finish)
        async def dismiss():
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait()
        self.host._close_instance=AsyncMock()
        with patch.object(A,'MAX_ACTIVITIES',0):self.host._dialog(self.tab,SimpleNamespace(dismiss=dismiss))
        await entered.wait();task=self.batch().task;task.cancel();await cleaning.wait()
        self.host.activity.desired(2);self.assertFalse(task.done())
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,2))
        finish.set();await task
        self.host._close_instance.assert_awaited_once_with(self.profile)
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)
    async def test_queue_cap_retires_exact_old_instance_without_unbounded_tasks(self):
        self.host._close_instance=AsyncMock()
        with patch.object(A,'MAX_ACTIVITIES',0),patch.object(C,'CAP',2):
            for _ in range(4):self.host._dialog(self.tab,SimpleNamespace(dismiss=AsyncMock()))
            batch=self.batch();self.assertLessEqual(len(batch.queue),2)
            self.assertTrue(self.instance.closing);self.assertIsNone(self.host.pane_tab(self.tab.id))
            await batch.task
        self.host._close_instance.assert_awaited_once_with(self.profile)
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)
    async def test_queued_old_cleanup_cannot_retire_same_profile_replacement(self):
        self.host._close_instance=AsyncMock()
        lock=self.host._launching.setdefault(self.profile,asyncio.Lock());await lock.acquire()
        with patch.object(A,'MAX_ACTIVITIES',0),patch.object(C,'CAP',0):
            self.host._dialog(self.tab,SimpleNamespace(dismiss=AsyncMock()))
        batch=self.batch();await asyncio.sleep(0)
        replacement=H._Instance(self.profile,self.instance.folder,None,None,generation=2)
        self.host._instances[self.profile]=replacement;lock.release();await batch.task
        self.host._close_instance.assert_not_called();self.assertFalse(replacement.closing)
    async def test_shutdown_before_batch_first_step_releases_only_after_retirement(self):
        self.host._close_instance=AsyncMock()
        with patch.object(A,'MAX_ACTIVITIES',0):self.host._dialog(self.tab,SimpleNamespace(dismiss=AsyncMock()))
        for event in self.releases:event.set()
        await self.host._attention_cleanup.settle()
        self.host._close_instance.assert_awaited_once_with(self.profile)
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)
        self.assertEqual(self.host._attention_cleanup._batches,{})

    async def test_committed_restart_retains_sole_old_context_teardown_ownership(self):
        restart=self.host.activity.try_restart(self.profile,1,1)
        self.assertTrue(self.host.activity.commit_restart(restart))
        dialog=SimpleNamespace(dismiss=AsyncMock());download=SimpleNamespace(cancel=AsyncMock(),delete=AsyncMock())
        self.host._dialog(self.tab,dialog);self.host._start_download(self.tab,download)
        await asyncio.sleep(0)
        self.assertEqual(self.host._attention_cleanup._batches,{})
        self.assertEqual(self.host.activity.counts(self.profile),(0,True,True))
        dialog.dismiss.assert_not_called();download.cancel.assert_not_called()

if __name__=='__main__':unittest.main()
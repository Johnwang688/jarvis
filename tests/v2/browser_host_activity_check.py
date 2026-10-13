"""BR10 host integration barriers with synthetic pages/CDP; never Chrome."""
from pathlib import Path
import asyncio, sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, drive_execute
from jarvis.v2.browser.activity import ActivityRefused
from jarvis.v2.browser.stream import TabStream


class Host(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='sound-host-activity-');self.addCleanup(tmp.cleanup)
        guard=patch.object(config,'BROWSER_DIR',Path(tmp.name)/'browser');guard.start();self.addCleanup(guard.stop)
        self.host=H.BrowserHost();self.profile='p-12345678'
        self.instance=H._Instance(self.profile,Path(tmp.name)/self.profile,Mock(),Mock(),generation=1)
        self.host._instances[self.profile]=self.instance;self.host.activity.opened(self.profile,1);self.host.activity.desired(1)
        self.page=SimpleNamespace(url='https://example.test/',goto=AsyncMock(),title=AsyncMock(return_value='Synthetic'),main_frame=object())
        self.tab=H._Tab('1234abcd',self.profile,None,self.page,page_identity=1,document_generation=1,pane_ready=True,
            location=(self.page.url,'Synthetic'),stream=TabStream('1234abcd',held=lambda:False,wake=lambda:None),
            cdp_lock=asyncio.Lock(),cast_lock=asyncio.Lock(),wakeup=asyncio.Event())
        self.host._tabs[self.tab.id]=self.tab;self.host._publish_snapshot()

    async def asyncTearDown(self):
        self.host._loop=None
        for tab in self.host._tabs.values():
            for timer in tab.timers.values():timer.cancel()
        await self.host._activity_work.settle()

    async def test_navigation_admits_before_profile_lock_and_retains_canceled_cleanup(self):
        lock=self.host._launching.setdefault(self.profile,asyncio.Lock());await lock.acquire()
        entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event()
        async def goto(*args,**kwargs):
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait()
        self.page.goto=goto
        task=asyncio.create_task(self.host._navigate(self.tab.id,'https://example.test/next'))
        await asyncio.sleep(0);self.assertEqual(self.host.activity.counts(self.profile)[0],1)
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        lock.release();await entered.wait();task.cancel();await cleaning.wait()
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        finish.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)

    async def test_prepare_timeout_does_not_release_its_actual_child(self):
        entered=asyncio.Event();cleaning=asyncio.Event();finish=asyncio.Event()
        async def prepare(*args,**kwargs):
            try:entered.set();await asyncio.Event().wait()
            finally:cleaning.set();await finish.wait()
        self.host._loop=asyncio.get_running_loop();original=self.host._call
        self.host._call=lambda coro,timeout:original(coro,.03)
        with patch.object(drive_execute,'prepare',prepare):
            waiting=asyncio.create_task(asyncio.to_thread(self.host.prepare_action,SimpleNamespace(tab_id=self.tab.id),None,None,None,None,lambda:None))
            await entered.wait()
            with self.assertRaises(H.BrowserRefused):await waiting
            await cleaning.wait()
            self.assertEqual(self.host.activity.counts(self.profile)[0],1)
            self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
            finish.set();await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertEqual(self.host.activity.counts(self.profile)[0],0)

    async def test_parent_snapshot_admission_and_queued_child_fail_closed(self):
        parent=self.host.begin_activity(self.tab.id,1)
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        self.host.end_activity(parent)
        restart=self.host.activity.try_restart(self.profile,1,1)
        with self.assertRaises(H.BrowserRefused):await self.host._navigate(self.tab.id,self.page.url)
        self.page.goto.assert_not_called()
        self.assertTrue(self.host.activity.commit_restart(restart))
        with self.assertRaises(H.BrowserRefused):self.host.begin_activity(self.tab.id,2)

    async def test_owner_input_child_outlives_the_short_delivery_wait(self):
        entered=asyncio.Event();finish=asyncio.Event()
        async def send(*args,**kwargs):entered.set();await finish.wait()
        self.tab.cdp=SimpleNamespace(send=send)
        with patch.object(H,'DELIVER_WAIT_S',.001):
            await self.host._deliver(self.tab,'Input.insertText',{'text':'synthetic'})
        await entered.wait();self.assertFalse(self.tab.input_task.done())
        self.assertEqual(self.host.activity.counts(self.profile)[0],1)
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        finish.set();await self.tab.input_task
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)

    async def test_attention_hold_cancels_pending_restart_and_pins_answer_cleanup(self):
        restart=self.host.activity.try_restart(self.profile,1,1)
        token=self.host.page_hold(self.tab,'auth')
        self.assertIsNotNone(token);self.assertFalse(self.host.activity.commit_restart(restart))
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        self.host.release_page_hold(self.tab,'auth',object())
        self.assertEqual(self.host.activity.counts(self.profile)[0],1)
        self.host.release_page_hold(self.tab,'auth',token)
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)

    async def test_old_same_id_callbacks_cannot_touch_current_ports_metadata_or_downloads(self):
        old=self.tab;old.loopback_port=33333;self.host._sync_ports(old)
        new=H._Tab(old.id,self.profile,None,self.page,page_identity=2,document_generation=1,pane_ready=True,
            location=(self.page.url,'New'),stream=Mock(),loopback_port=33333)
        self.host._tabs[old.id]=new;self.host._daemon=SimpleNamespace(browser_drive=Mock(),face_port=18001,port=18002,workshop_port=18003)
        self.host._dropped(old);self.host._landed(old,SimpleNamespace(parent_frame=None,url='about:blank'))
        self.host._located(old,url='https://stale.test/');self.host._start_download(old,Mock())
        old.loopback_port=None;self.host._sync_ports(old)
        self.assertIs(self.host._tabs[old.id],new);self.assertTrue(self.host.loopback_open(33333))
        self.assertEqual(new.location,(self.page.url,'New'));self.assertEqual(new.document_generation,1)
        self.host._daemon.browser_drive.assert_not_called()
        self.assertEqual(self.host._daemon.browser_drive.mock_calls,[])
        self.assertEqual(new.stream.mock_calls,[])
        self.assertIsNone(self.host.page_hold(old,'auth'))

    async def test_tab_release_does_not_free_actual_download_cleanup_hold(self):
        token=self.host.page_hold(self.tab,'download:1234abcd')
        self.host._release(self.tab,'closed')
        self.assertEqual(self.host.activity.counts(self.profile)[0],1)
        self.assertIsNone(self.host.activity.try_restart(self.profile,1,1))
        self.host.release_page_hold(self.tab,'download:1234abcd',token)
        self.assertEqual(self.host.activity.counts(self.profile)[0],0)
    async def test_only_current_main_frame_request_marks_restore_as_loading(self):
        request=SimpleNamespace(is_navigation_request=lambda:True,frame=self.page.main_frame)
        self.host._loading(self.tab,request);self.assertTrue(self.tab.loading)
        self.host._titled=AsyncMock();self.host._loaded(self.tab);await asyncio.sleep(0)
        self.assertFalse(self.tab.loading)
        self.host._loading(self.tab,SimpleNamespace(is_navigation_request=lambda:True,frame=object()))
        self.assertFalse(self.tab.loading)
        self.host._tabs.pop(self.tab.id)
        self.host._loading(self.tab,request);self.assertFalse(self.tab.loading)

    async def test_popup_adoption_cannot_register_a_retired_instance(self):
        stale=H._Instance(self.profile,Path('/synthetic'),Mock(),Mock(),generation=0)
        page=Mock();self.host._refuse_popup=AsyncMock()
        self.assertIsNone(self.host._adopt(stale,page,None));await asyncio.sleep(0)
        self.host._refuse_popup.assert_awaited_once_with(page)
        self.assertEqual(list(self.host._tabs),[self.tab.id])

if __name__=='__main__':unittest.main()
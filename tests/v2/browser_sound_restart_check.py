"""Deterministic sound replacement with synthetic pages, broker and Pulse only."""
from pathlib import Path
import asyncio, sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, audio as A
from jarvis.v2.browser.sound import BrowserSound
from jarvis.v2.browser.sound_restore import Restore

class Page:
    def __init__(self,url='about:blank'):
        self.url=url;self.handlers={};self.closed=False;self.gotos=[]
        self.main_frame=SimpleNamespace(parent_frame=None,url=url)
    def on(self,name,callback):self.handlers[name]=callback
    async def opener(self):return None
    async def title(self):return 'Synthetic'
    async def close(self):
        self.closed=True
        if 'close' in self.handlers:self.handlers['close'](self)
    async def goto(self,url,**kwargs):
        self.gotos.append(url);self.url=url;self.main_frame.url=url
        if 'framenavigated' in self.handlers:self.handlers['framenavigated'](self.main_frame)
    async def set_content(self,*args,**kwargs):pass

class Context:
    def __init__(self):self.pages=[];self.callback=None;self.closed=False;self.gate=None;self.entered=None
    async def new_page(self):
        page=Page();self.pages.append(page)
        if self.callback:self.callback(page)
        return page
    async def close(self):
        if self.entered:self.entered.set()
        if self.gate:await self.gate.wait()
        for page in self.pages:await page.close()
        self.closed=True

class Restart(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        p=patch.object(config,'BROWSER_DIR',Path(self.tmp.name));p.start();self.addCleanup(p.stop)
        self.host=H.BrowserHost();self.profile='p-12345678'
        self.host._sound=BrowserSound(self.host,probe=lambda:A.Availability(True,False));self.sound=self.host._sound
        self.host._daemon=SimpleNamespace(approvals=SimpleNamespace(run_if_idle=lambda f:f(),pending=lambda:[]),
            bus=Mock(),face_port=18001,port=18002,workshop_port=18003)
        self.context=Context();self.old=H._Instance(self.profile,Path(self.tmp.name)/self.profile,self.context,Mock(),generation=1)
        self.host._instances[self.profile]=self.old;self.host._next_instance_generation=1
        self.host.activity.opened(self.profile,1)
        self.host._settings.update(sound=True);self.sound.sync();self.generation=self.sound.preference()[1]
        self.launches=[]
        async def launch(profile):
            context=Context();instance=H._Instance(profile,self.old.folder,context,Mock(),audio=A.InstanceAudio(self.sound.preference()[0]))
            context.callback=lambda page:self.host._adopt(instance,page,None)
            self.launches.append(instance);return instance
        self.host._launch=AsyncMock(side_effect=launch)
        self.kill=patch.object(H,'stop_pids',return_value=0);self.kill.start();self.addCleanup(self.kill.stop)
        self.pids=patch.object(H,'chrome_pids',return_value=[]);self.pids.start();self.addCleanup(self.pids.stop)
    async def asyncTearDown(self):
        self.host._stopped=True;await self.sound.settle()
        for i in self.host._instances.values():
            if i.idle:i.idle.cancel()
        await self.host._activity_work.settle()
        await asyncio.sleep(0)
    def tab(self,id='1234abcd',url='https://example.test/synthetic',lease=None):
        page=Page(url);self.context.pages.append(page)
        tab=self.host._adopt(self.old,page,None)
        self.host._tabs.pop(tab.id);tab.id=id;tab.stream.tab_id=id;self.host._tabs[id]=tab
        tab.loopback_port=lease;self.host._sync_ports(tab);self.host._next_page_identity=max(1,self.host._next_page_identity)
        return tab
    async def run_restart(self):await self.sound.restarts._run(self.old,self.generation)
    async def test_exact_tab_fresh_page_and_established_lease_survive_without_owner_navigation(self):
        old=self.tab(url='http://127.0.0.1:33333/synthetic',lease=33333)
        await asyncio.sleep(0);await asyncio.sleep(0)
        before=old.page_identity;stale_ticket=self.host.tickets.mint(old.id,old.page_identity);await self.run_restart()
        new=self.host._tabs[old.id]
        self.assertIsNot(new,old);self.assertIsNot(new.page,old.page);self.assertGreater(new.page_identity,before)
        self.assertEqual(new.loopback_port,33333);self.assertTrue(self.host.loopback_open(33333));self.assertIsNone(new.pending_port)
        self.assertFalse(self.host.tickets.redeem(stale_ticket,old.id,old.page_identity))
        self.assertEqual(new.page.gotos,['http://127.0.0.1:33333/synthetic'])
        self.assertEqual(self.sound.restarts.descriptors,{})
        fresh_ticket=self.host.tickets.mint(new.id,new.page_identity)
        self.host._dropped(old);self.host._release(old,'stale');self.host._landed(old,SimpleNamespace(parent_frame=None,url='about:blank'))
        self.assertIs(self.host._tabs[old.id],new);self.assertTrue(self.host.loopback_open(33333))
        self.assertTrue(self.host.tickets.redeem(fresh_ticket,new.id,new.page_identity))
        self.assertNotIn('synthetic',repr(self.host.status()));self.assertNotIn('33333',repr(self.host.status()))
    async def test_broker_already_pending_blocks_before_any_shutdown(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.host._daemon.approvals.run_if_idle=lambda f:False
        await self.run_restart();self.assertIs(self.host._instances[self.profile],self.old)
        self.assertFalse(self.context.closed);self.host._launch.assert_not_awaited();self.assertEqual(self.sound.restarts.descriptors,{})
        self.assertEqual(self.host.activity.counts(self.profile),(0,False,False))
    async def test_owner_close_during_shutdown_cancels_before_profile_lock_wait(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.context.gate=asyncio.Event();self.context.entered=asyncio.Event()
        restart=asyncio.create_task(self.run_restart());await self.context.entered.wait()
        closed=asyncio.create_task(self.host._close_tab(old.id));await asyncio.sleep(0)
        self.assertNotIn(old.id,self.sound.restarts.descriptors)
        self.context.gate.set();await restart;self.assertTrue((await closed)['closed']);self.assertNotIn(old.id,self.host._tabs)
    async def test_second_page_failure_keeps_every_logical_tab_once_and_retry_is_explicit(self):
        first=self.tab();second=self.tab('abcd1234','https://example.test/second')
        await asyncio.sleep(0);await asyncio.sleep(0)
        real=self.host._goto
        async def goto(tab,*args,**kwargs):
            if tab.id==second.id:return {'loaded':False}
            return await real(tab,*args,**kwargs)
        self.host._goto=goto;await self.run_restart()
        self.assertEqual(set(self.sound.restarts.descriptors),{first.id,second.id});self.assertEqual(self.host._tabs,{})
        self.assertEqual(self.sound.instance_status(self.host._instances[self.profile])['phase'],'failed')
        self.sound.restarts.consider(self.host._instances[self.profile],self.generation);await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,1)
        self.host._goto=real;await self.sound.retry(first.id)
        await asyncio.sleep(0)
        task=self.sound.restarts.tasks.get(self.profile)
        if task:await task
        self.assertEqual(set(self.host._tabs),{first.id,second.id});self.assertEqual(self.host._launch.await_count,2)
    async def test_new_card_after_commit_holds_input_but_structural_restore_completes(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        def commit(f):
            result=f();self.host._daemon.approvals.pending=lambda:[object()];return result
        self.host._daemon.approvals.run_if_idle=commit
        await self.run_restart();self.assertIn(old.id,self.host._tabs);self.assertTrue(self.host.input_held())
        self.assertEqual(self.sound.restarts.descriptors,{})
    async def test_private_and_privileged_signin_are_never_replayed(self):
        tab=self.tab();tab.signin=True;await asyncio.sleep(0);await asyncio.sleep(0)
        await self.run_restart();self.host._launch.assert_not_awaited()
        self.assertEqual(self.sound.restarts.descriptors,{})
        self.old.profile='private';self.sound.restarts.consider(self.old,self.generation)
        self.assertEqual(self.sound.restarts.tasks,{})
    async def test_unsettled_mute_and_owned_chrome_cleanup_block_overlap(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        epoch=self.old.audio.begin_mute();await self.run_restart();self.host._launch.assert_not_awaited()
        self.old.audio.finish_mute(epoch)
        with patch.object(H,'chrome_pids',return_value=[12345]):await self.run_restart()
        self.host._launch.assert_not_awaited();self.assertIn(old.id,self.sound.restarts.descriptors)
        self.assertEqual(self.sound.instance_status(self.host._instances[self.profile])['phase'],'failed')
    async def test_popup_during_restore_is_refused_and_cannot_consume_a_logical_id(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        real=self.host._goto;popups=[]
        async def goto(tab,*args,**kwargs):
            instance=self.host._instances[self.profile]
            popup=Page('https://popup.test/');instance.context.pages.append(popup);popups.append(popup)
            self.assertIsNone(self.host._adopt(instance,popup,None))
            return await real(tab,*args,**kwargs)
        self.host._goto=goto;await self.run_restart()
        self.assertEqual(set(self.host._tabs),{old.id});self.assertTrue(popups[0].closed)
        self.assertIsNot(self.host._tabs[old.id].page,popups[0])

    async def test_forbidden_redirect_is_inert_and_loses_transferred_port(self):
        old=self.tab(url='http://127.0.0.1:33333/synthetic',lease=33333)
        await asyncio.sleep(0);await asyncio.sleep(0)
        real=self.host._goto
        async def goto(tab,*args,**kwargs):
            await real(tab,*args,**kwargs)
            tab.page.url='http://127.0.0.1:18001/'
            tab.page.main_frame.url=tab.page.url
            self.host._landed(tab,tab.page.main_frame)
            return {'loaded':True}
        self.host._goto=goto;await self.run_restart()
        new=self.host._tabs[old.id];self.assertEqual(new.page.url,'about:blank')
        self.assertFalse(self.host.loopback_open(33333));self.assertTrue(new.pane_ready)

    async def test_off_then_on_while_mute_native_work_is_pending_replaces_tainted_instance(self):
        import threading
        old=self.tab();self.old.audio=A.InstanceAudio(True);self.old.audio.on_settled(self.sound.wake)
        self.host._settings.update(sound=False);self.sound.sync();off=self.sound.preference()[1]
        await asyncio.sleep(0);await asyncio.sleep(0)
        entered=threading.Event();release=threading.Event()
        def mute(owner,state,current):
            epoch=state.begin_mute();entered.set();release.wait(2);state.finish_mute(epoch)
            return A.MuteResult(1,True)
        self.sound.pulse=SimpleNamespace(mute_owned=mute)
        with patch.object(A.OwnedChrome,'pin',return_value=object()),patch.object(H,'chrome_pids',side_effect=lambda _:[] if self.context.closed else [12345]):
            self.sound._mute(self.old,off,A.Availability(True,True))
            while not entered.is_set():await asyncio.sleep(0)
            self.host._settings.update(sound=True);self.sound.sync();on=self.sound.preference()[1]
            await self.sound.restarts._run(self.old,on);self.host._launch.assert_not_awaited()
            release.set();task=self.sound._mute_tasks[self.profile];await task;await asyncio.sleep(0)
            restart=self.sound.restarts.tasks.get(self.profile)
            if restart:await restart
        self.assertEqual(self.host._launch.await_count,1)
        self.assertTrue(self.host._instances[self.profile].audio.synchronized(True))
        self.assertIsNot(self.host._instances[self.profile],self.old);self.assertIn(old.id,self.host._tabs)

    async def test_stream_ticket_identity_mismatch_is_spent_before_any_upgrade(self):
        ticket=self.host.tickets.mint('1234abcd',1)
        self.assertFalse(self.host.tickets.redeem(ticket,'1234abcd',2))
        self.assertFalse(self.host.tickets.redeem(ticket,'1234abcd',1))

    async def test_exact_restart_navigation_capability_rejects_forged_and_owner_flags(self):
        tab=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        with self.assertRaises(H.BrowserRefused):await self.host._goto(tab,tab.page.url,restart_token=object())
        token=self.host.activity.try_restart(self.profile,1,self.generation);self.assertTrue(self.host.activity.commit_restart(token))
        with self.assertRaises(H.BrowserRefused):await self.host._goto(tab,tab.page.url,owner=True,restart_token=token)
        self.assertEqual(tab.page.gotos,[])

if __name__=='__main__':unittest.main()

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
        self.sound._requests.add(self.profile);self.sound.sync(explicit=True);self.generation=self.sound.preference()[1]
        self.launches=[]
        async def launch(profile, *, restart_token=None):
            context=Context();instance=H._Instance(profile,self.old.folder,context,Mock(),audio=A.InstanceAudio(self.sound.preference(profile)[0]))
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
    async def failed_toggle(self, initially_on, *, fail_again=False):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.old.audio=A.InstanceAudio(not initially_on)
        if not initially_on:self.sound._requests.discard(self.profile)
        real=self.host._goto
        self.host._goto=AsyncMock(return_value={'loaded':False})
        await self.run_restart()
        self.assertEqual(self.host._launch.await_count,1)
        placeholder=self.host._instances[self.profile]
        self.assertTrue(placeholder.closing)
        self.assertEqual(set(self.sound.restarts.descriptors),{old.id})
        self.host._loop=asyncio.get_running_loop()
        self.host._daemon.approvals.run_if_idle=lambda f:False
        await self.sound.change(old.id,not initially_on)
        self.assertEqual(self.sound.instance_status(placeholder)['phase'],'failed')
        await asyncio.sleep(0);await asyncio.sleep(0);await asyncio.sleep(0)
        task=self.sound.restarts.tasks.get(self.profile)
        if task:await task
        await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,1)
        self.assertEqual(self.sound.instance_status(placeholder)['phase'],'failed')
        self.assertEqual(self.host._tabs,{})
        self.host._daemon.approvals.run_if_idle=lambda f:f()
        if not fail_again:self.host._goto=real
        self.sound._reconcile()
        task=self.sound.restarts.tasks.get(self.profile)
        if task:await task
        await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,2)
        if fail_again:
            self.assertEqual(self.sound.instance_status(self.host._instances[self.profile])['phase'],'failed')
            self.assertEqual(set(self.sound.restarts.descriptors),{old.id})
        else:
            current=self.host._instances[self.profile]
            self.assertFalse(current.closing)
            self.assertTrue(current.audio.synchronized(not initially_on))
            self.assertEqual(self.sound.instance_status(current)['phase'],'ready')
            self.assertEqual(set(self.host._tabs),{old.id})
            self.assertEqual(self.sound.restarts.descriptors,{})
        self.sound._reconcile();await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,2)

    async def test_failed_on_owner_off_restores_quiet_after_busy_barrier(self):
        await self.failed_toggle(True)

    async def test_failed_off_owner_on_restores_audible_after_busy_barrier(self):
        await self.failed_toggle(False)

    async def test_failed_toggle_attempt_stays_failed_without_automatic_retry(self):
        await self.failed_toggle(True,fail_again=True)

    async def test_new_owner_generation_during_failed_restore_survives_exact_replacement(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.host._loop=asyncio.get_running_loop()
        entered=asyncio.Event();release=asyncio.Event();real=self.host._goto
        calls=0
        async def goto(tab,*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:
                entered.set();await release.wait();return {'loaded':False}
            return await real(tab,*args,**kwargs)
        self.host._goto=goto
        self.sound.restarts.consider(self.old,self.generation)
        await entered.wait()
        replacement=self.host._instances[self.profile]
        await self.sound.change(old.id,False)
        marker=self.sound._recoveries[self.profile]
        self.assertIs(marker[0],replacement)
        self.assertEqual(marker[1],replacement.generation)
        first=self.sound.restarts.tasks[self.profile]
        release.set();await first
        for _ in range(20):
            await asyncio.sleep(0)
            task=self.sound.restarts.tasks.get(self.profile)
            if task is not None:await task
            if old.id in self.host._tabs:break
        current=self.host._instances[self.profile]
        self.assertEqual(self.host._launch.await_count,2)
        self.assertFalse(current.closing)
        self.assertTrue(current.audio.synchronized(False))
        self.assertEqual(set(self.host._tabs),{old.id})
        self.assertEqual(self.sound._recoveries,{})
        self.assertNotIn(self.profile,self.sound.restarts.failed)

    async def test_other_profile_owner_gesture_cannot_retry_failed_profile(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.host._goto=AsyncMock(return_value={'loaded':False})
        await self.run_restart()
        placeholder=self.host._instances[self.profile]
        other='p-87654321'
        self.host._instances[other]=H._Instance(other,self.old.folder,Context(),Mock(),generation=100)
        other_tab=SimpleNamespace(id='other-id',profile=other)
        self.host._tabs[other_tab.id]=other_tab
        self.sound.publish=Mock()
        await self.sound.change(other_tab.id,False)
        self.sound._reconcile();await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,1)
        self.assertEqual(self.sound.instance_status(placeholder)['phase'],'failed')
        self.assertEqual(set(self.sound.restarts.descriptors),{old.id})

    async def test_stale_instance_recovery_marker_cannot_retry_failed_profile(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.host._goto=AsyncMock(return_value={'loaded':False})
        await self.run_restart()
        placeholder=self.host._instances[self.profile]
        self.sound._recoveries[self.profile]=(placeholder,placeholder.generation-1,self.generation+1)
        self.sound.sync(explicit=True)
        self.sound._reconcile();await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(self.host._launch.await_count,1)
        self.assertEqual(self.sound.instance_status(placeholder)['phase'],'failed')

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

    async def test_revocation_blocks_new_allocation_and_popup_adoption_until_quiet_launch(self):
        for last_close in (False,True):
            with self.subTest(last_tab_close=last_close):
                # Freeze reconcile to expose the launch-mode revocation gap.
                self.host._tabs.clear();self.sound._requests.add(self.profile)
                self.old.closing=False;self.old.audio=A.InstanceAudio(True)
                self.host._profiles.require=Mock()
                old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
                if last_close:await self.host._close_tab(old.id)
                else:await self.sound.change(old.id,False)
                self.assertTrue(self.old.audio.applied);self.assertFalse(self.sound.preference(self.profile)[0])
                before=list(self.context.pages)
                with self.assertRaises(H.BrowserRefused) as refused:
                    await self.host._open_tab_locked(self.profile,None,None,owner=False,signin=False)
                self.assertEqual(refused.exception.status,409);self.assertEqual(self.context.pages,before)
                with self.assertRaises(H.BrowserRefused):
                    await self.host._open_page_locked(self.old,None,None,owner=False,signin=False)
                popup=Page('https://popup.test/');self.context.pages.append(popup)
                self.assertIsNone(self.host._adopt(self.old,popup,None))
                self.assertFalse(any(t.page is popup for t in self.host._tabs.values()))
                await asyncio.gather(*self.old.popup_cleanup);await asyncio.sleep(0)
                self.assertTrue(popup.closed)
                # Native stream mute does not change immutable launch mode.
                epoch=self.old.audio.begin_mute();self.old.audio.finish_mute(epoch)
                with self.assertRaises(H.BrowserRefused):
                    await self.host._open_tab_locked(self.profile,None,None,owner=False,signin=False)
                # The structurally quiet replacement can allocate again.
                self.old.audio=A.InstanceAudio(False)
                row=await self.host._open_tab_locked(self.profile,None,None,owner=False,signin=False)
                self.assertIn(row['tab_id'],self.host._tabs)

    async def test_off_during_new_page_wait_refuses_returned_page(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        self.old.audio=A.InstanceAudio(True);self.host._profiles.require=Mock()
        entered=asyncio.Event();release=asyncio.Event();created=[]
        real=self.context.new_page
        async def blocked():
            entered.set();await release.wait()
            page=await real();created.append(page);return page
        self.context.new_page=blocked
        job=asyncio.create_task(self.host._open_tab(self.profile,None,None))
        await entered.wait();await self.sound.change(old.id,False);release.set()
        with self.assertRaises(H.BrowserRefused) as refused:await job
        self.assertEqual(refused.exception.status,409)
        self.assertEqual(set(self.host._tabs),{old.id})
        await asyncio.gather(*self.old.popup_cleanup);await asyncio.sleep(0)
        self.assertTrue(created[0].closed)
        self.assertFalse(any(t.page is created[0] for t in self.host._tabs.values()))

    async def test_popup_flood_while_new_page_waits_is_bounded_and_fails_replacement(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        entered=asyncio.Event();release=asyncio.Event();popups=[]
        original=self.host._launch.side_effect
        async def launch(*args,**kwargs):
            instance=await original(*args,**kwargs)
            real=instance.context.new_page
            async def blocked():
                entered.set();await release.wait();return await real()
            instance.context.new_page=blocked
            return instance
        self.host._launch.side_effect=launch
        restart=asyncio.create_task(self.run_restart());await entered.wait()
        instance=self.launches[-1];cap=self.host._tab_cap()
        for _ in range(cap*4):
            page=Page('https://popup.test/');instance.context.pages.append(page);popups.append(page)
            self.assertIsNone(self.host._adopt(instance,page,None))
        self.assertTrue(instance.closing)
        self.assertLessEqual(len(instance.unadopted)+len(instance.refused_pages),cap)
        self.assertLessEqual(len(instance.popup_cleanup),cap)
        self.assertEqual(self.host._tabs,{})
        release.set();await restart
        self.assertTrue(instance.context.closed);self.assertTrue(all(p.closed for p in popups))
        self.assertEqual(instance.unadopted,[]);self.assertEqual(self.host._tabs,{})
        self.assertEqual(set(self.sound.restarts.descriptors),{old.id})
        self.assertEqual(self.sound.instance_status(self.host._instances[self.profile])['phase'],'failed')

    async def test_restore_navigation_cannot_publish_or_attach_a_partial_page(self):
        old=self.tab();await asyncio.sleep(0);await asyncio.sleep(0)
        real=self.host._goto;admissions=[]
        self.host._running=lambda:True
        async def goto(tab,*args,**kwargs):
            instance=self.host._instances[self.profile]
            self.host._publish_pane_tabs();published=self.host.pane_tab(tab.id)
            ticket_refused=False
            try:self.host.ticket(tab.id)
            except H.BrowserRefused as refused:ticket_refused=refused.status==409
            sock=Mock()
            with patch.object(H,'_serve_stream') as serve:
                self.host.serve_socket(tab.id,sock,expected_tab=tab)
                admissions.append((instance.restoring,published is None,ticket_refused,
                                   sock.close.call_count==1,serve.called))
            return await real(tab,*args,**kwargs)
        self.host._goto=goto;await self.run_restart()
        self.assertEqual(admissions,[(True,True,True,True,False)])
        new=self.host._tabs[old.id]
        self.assertFalse(self.host._instances[self.profile].restoring)
        self.assertTrue(new.pane_ready);self.assertEqual(new.page.gotos,['https://example.test/synthetic'])
        self.assertIsNotNone(self.host.pane_tab(old.id));self.assertIn('ticket',self.host.ticket(old.id))

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
            self.host._settings.update(sound=True);self.sound.sync();await self.sound.change(old.id,True);on=self.sound.preference()[1]
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

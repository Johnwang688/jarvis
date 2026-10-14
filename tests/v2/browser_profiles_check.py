"""BR-1c profile metadata and lifecycle; no external network or credentials.

Run each suite in a 3G/0swap systemd scope. Real Chrome uses existing blocked
network fixtures, static pages, tab cap4, and one instance at a time; pure
barrier tests exercise concurrent launch caps without extra Chrome processes.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config, llm
from jarvis.v2.browser import host as H, profiles as P
from tests.v2 import owner_browser_check as O


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="profiles-store-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "browser"
        guard = patch.object(config, "BROWSER_DIR", self.root)
        guard.start(); self.addCleanup(guard.stop)
        self.store = P.ProfileStore()

    def test_crud_paths_caps_and_empty_last_profile(self):
        self.assertEqual(self.store.list(), [])
        main = self.store.list(create=True)[0]
        other = self.store.create("../x")
        self.assertRegex(other["id"], r"^p-[0-9a-f]{8}$")
        self.assertFalse((self.root.parent / "x").exists())
        self.assertEqual(self.store.rename(other["id"], "Other")["label"], "Other")
        for n in range(6): self.store.create(f"Profile {n}")
        with self.assertRaises(H.BrowserRefused): self.store.create("Ninth")
        self.assertEqual(len(self.store.list()), 8)
        for row in self.store.list(): self.store.delete(row["id"])
        self.assertEqual(self.store.list(create=True), [])
        self.assertNotEqual(self.store.create("Main")["id"], main["id"])
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "profiles.json").stat().st_mode), 0o600)
        self.assertIn(str(self.root), config.credential_dirs())

    def test_labels_and_ids_are_validated_without_echo(self):
        self.store.list(create=True)
        for label in (None, True, "", " ", " private ", "PRIVATE", "a\nb", "a\u202eb", "e\u0301", "x"*33):
            with self.subTest(label=repr(label)), self.assertRaises(H.BrowserRefused):
                self.store.create(label)
        self.store.create("Work")
        with self.assertRaises(H.BrowserRefused): self.store.create("work")
        with self.assertRaises(H.BrowserRefused): self.store.rename("../../x", "Safe")

    def test_metadata_corruption_permissions_and_symlinks_fail_closed(self):
        row = self.store.list(create=True)[0]
        path = self.root / "profiles.json"
        valid = path.read_bytes()
        for bad in (b"no", b"{}", b"[" * 2000 + b"]" * 2000, json.dumps([row,row]).encode(), json.dumps([{**row,"id":"../../x"}]).encode()):
            path.write_bytes(bad)
            with self.assertRaises(H.BrowserRefused): self.store.list()
        path.write_bytes(valid);path.chmod(0o644)
        with self.assertRaises(H.BrowserRefused): self.store.list()
        path.chmod(0o600);path.unlink();path.symlink_to(self.root.parent / "elsewhere")
        with self.assertRaises(H.BrowserRefused): self.store.list(create=True)
        path.unlink();path.write_bytes(valid);path.chmod(0o600)
        (self.root / "profiles").rmdir();(self.root / "profiles").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(H.BrowserRefused): self.store.create("Never")

    def test_parallel_create_cannot_exceed_cap_or_lose_rows(self):
        self.store.list(create=True)
        outcomes=[]
        def create(n):
            try: outcomes.append(self.store.create(f"Label {n}"))
            except H.BrowserRefused: pass
        threads=[threading.Thread(target=create,args=(n,)) for n in range(12)]
        for t in threads:t.start()
        for t in threads:t.join(3)
        self.assertEqual(len(outcomes),7)
        self.assertEqual(len(self.store.list()),8)


class Lifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="profiles-life-");self.addCleanup(self.tmp.cleanup)
        guard=patch.object(config,"BROWSER_DIR",Path(self.tmp.name)/"browser")
        guard.start();self.addCleanup(guard.stop)
        self.host=H.BrowserHost()
        self.ids=[self.host.list_profiles()[0]["id"]]+[self.host.create_profile(f"Profile {n}")["id"] for n in range(4)]

    async def test_inflight_reservations_refuse_fourth_and_allow_private(self):
        entered=asyncio.Queue();release=asyncio.Event()
        async def launch(profile):
            await entered.put(profile);await release.wait()
            return H._Instance(profile,config.BROWSER_DIR/profile,SimpleNamespace(),SimpleNamespace())
        self.host._launch=launch
        tasks=[asyncio.create_task(self.host._instance(profile)) for profile in self.ids[:3]]
        for _ in tasks:await entered.get()
        with self.assertRaises(H.BrowserRefused):await asyncio.wait_for(self.host._instance(self.ids[3]),1)
        private=asyncio.create_task(self.host._instance(H.PRIVATE));self.assertEqual(await entered.get(),H.PRIVATE)
        release.set();await asyncio.gather(*tasks,private)
        self.assertEqual(len(self.host._instances),4)
        self.assertEqual(self.host._launch_reserved,set())

    async def test_failed_launch_releases_reservation(self):
        self.host._launch=AsyncMock(side_effect=H.BrowserRefused(409,"fake failed launch"))
        with self.assertRaises(H.BrowserRefused):await self.host._instance(self.ids[0])
        self.assertEqual(self.host._launch_reserved,set())

    async def test_delete_waits_for_download_before_wiping(self):
        profile=self.ids[0];entered=asyncio.Event();release=asyncio.Event();wiped=[]
        async def download():entered.set();await release.wait()
        task=asyncio.create_task(download());await entered.wait()
        self.host._download_tasks[task]=SimpleNamespace(profile=profile,ctx=None)
        self.host._wipe_profile=lambda value:wiped.append(value)
        deleting=asyncio.create_task(self.host._manage_profile_async(profile,True))
        await asyncio.sleep(0)
        self.assertEqual(wiped,[]);self.assertFalse(deleting.done())
        release.set();await deleting
        self.assertEqual(wiped,[profile])
        with self.assertRaises(H.BrowserRefused):self.host._profiles.require(profile)

    async def test_open_queued_behind_delete_cannot_recreate_profile(self):
        profile=self.ids[0];entered=threading.Event();release=threading.Event()
        def wipe(value):entered.set();release.wait(3)
        self.host._wipe_profile=wipe
        self.host._launch=AsyncMock()
        deleting=asyncio.create_task(self.host._manage_profile_async(profile,True))
        self.assertTrue(await asyncio.to_thread(entered.wait,3))
        opening=asyncio.create_task(self.host._open_tab(profile,None,None))
        await asyncio.sleep(0);self.assertFalse(opening.done())
        release.set();await deleting
        with self.assertRaises(H.BrowserRefused):await opening
        self.host._launch.assert_not_awaited()

    async def test_cancelled_delete_keeps_lock_until_worker_finishes(self):
        profile=self.ids[0];entered=threading.Event();release=threading.Event()
        def wipe(value):entered.set();release.wait(3)
        self.host._wipe_profile=wipe
        self.host._launch=AsyncMock()
        deleting=asyncio.create_task(self.host._manage_profile_async(profile,True))
        self.assertTrue(await asyncio.to_thread(entered.wait,3))
        deleting.cancel();await asyncio.sleep(0)
        deleting.cancel();await asyncio.sleep(0)
        opening=asyncio.create_task(self.host._open_tab(profile,None,None))
        await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertFalse(opening.done(), "a timed-out wipe still owns its lifecycle lock")
        release.set()
        with self.assertRaises(asyncio.CancelledError):await deleting
        with self.assertRaises(H.BrowserRefused):await opening
        self.host._launch.assert_not_awaited()

    async def test_last_private_tab_waits_for_context_and_download_cleanup(self):
        ctx="abcdefab";release=asyncio.Event()
        folder=H.secure_dir(config.BROWSER_DIR/"private"/"run-test")
        incoming=H.secure_dir(config.BROWSER_DIR/"downloads"/H.PRIVATE/ctx)
        (incoming/"fixture").write_text("temporary")
        instance=H._Instance(H.PRIVATE,folder,SimpleNamespace(close=AsyncMock()),None)
        instance.private_contexts[ctx]=SimpleNamespace(close=AsyncMock())
        self.host._instances[H.PRIVATE]=instance
        tab=H._Tab("abcdefab",H.PRIVATE,ctx,SimpleNamespace(close=AsyncMock()))
        self.host._tabs[tab.id]=tab
        download=asyncio.create_task(release.wait())
        self.host._download_tasks[download]=tab
        with patch.object(H,"stop_pids",return_value=[]):
            closing=asyncio.create_task(self.host._close_tab(tab.id))
            for _ in range(8):await asyncio.sleep(0)
            self.assertFalse(closing.done(), "DELETE must not report cleanup while a writer remains")
            release.set();await closing
        self.assertFalse(incoming.exists());self.assertFalse(folder.exists())
        self.assertNotIn(H.PRIVATE,self.host._instances)

    def private_fixture(self):
        ctx="abcdefab"
        folder=H.secure_dir(config.BROWSER_DIR/"private"/"run-test")
        incoming=H.secure_dir(config.BROWSER_DIR/"downloads"/H.PRIVATE/ctx)
        (incoming/"fixture").write_text("temporary")
        context=SimpleNamespace(close=AsyncMock())
        instance=H._Instance(H.PRIVATE,folder,SimpleNamespace(close=AsyncMock()),None)
        instance.private_contexts[ctx]=context
        self.host._instances[H.PRIVATE]=instance
        return ctx,instance,context,incoming

    async def test_context_failure_retains_identity_and_retries_every_stage(self):
        for phase in ("context", "download", "directory", "engine"):
            with self.subTest(phase=phase):
                ctx,instance,context,incoming=self.private_fixture()
                real_remove=H.shutil.rmtree
                failed=False
                async def settle(*args):
                    nonlocal failed
                    if phase=="download" and not failed:
                        failed=True;raise H.BrowserRefused(409,"fixture writer pending")
                def remove(path,*args,**kw):
                    nonlocal failed
                    target=incoming if phase=="directory" else instance.folder
                    if phase in ("directory","engine") and Path(path)==target and not failed:
                        failed=True;raise OSError("fixture failure")
                    return real_remove(path,*args,**kw)
                if phase=="context":context.close.side_effect=[OSError("fixture failure"),None]
                with patch.object(H,"stop_pids",return_value=[]),patch.object(H.shutil,"rmtree",side_effect=remove),patch.object(self.host,"_settle_downloads",side_effect=settle):
                    with self.assertRaises((OSError,H.BrowserRefused)):await self.host._close_context(ctx)
                    self.assertIs(instance.private_contexts[ctx],context)
                    self.assertIn(ctx,self.host._closing_contexts)
                    for wanted in (ctx,None):
                        with self.assertRaises(H.BrowserRefused) as refused:
                            await self.host._open_tab(H.PRIVATE,wanted,None)
                        self.assertEqual(refused.exception.status,409)
                    self.assertEqual(await self.host._close_context(ctx),{"ctx":ctx,"closed":True})
                self.assertNotIn(ctx,self.host._closing_contexts)
                self.assertFalse(incoming.exists());self.assertFalse(instance.folder.exists())

    async def test_context_cancellation_keeps_retryable_state(self):
        ctx,instance,context,incoming=self.private_fixture()
        entered=asyncio.Event()
        async def closing():entered.set();await asyncio.Event().wait()
        context.close.side_effect=closing
        deleting=asyncio.create_task(self.host._close_context(ctx))
        await entered.wait();deleting.cancel()
        with self.assertRaises(asyncio.CancelledError):await deleting
        self.assertIs(instance.private_contexts[ctx],context)
        self.assertIn(ctx,self.host._closing_contexts)
        context.close=AsyncMock()
        with patch.object(H,"stop_pids",return_value=[]):await self.host._close_context(ctx)
        self.assertFalse(incoming.exists());self.assertNotIn(ctx,self.host._closing_contexts)

    async def test_failed_natural_retirement_preserves_other_private_context(self):
        ctx,instance,context,incoming=self.private_fixture()
        context.close.side_effect=[OSError("fixture failure"),None]
        other="01234567";other_context=SimpleNamespace(close=AsyncMock())
        instance.private_contexts[other]=other_context
        tab=H._Tab("abcdefab",H.PRIVATE,ctx,SimpleNamespace())
        healthy=H._Tab("01234567",H.PRIVATE,other,SimpleNamespace())
        self.host._tabs.update({tab.id:tab,healthy.id:healthy})
        self.host._dropped(tab)
        with self.assertRaises(OSError):await self.host._retiring_contexts[ctx]
        await asyncio.sleep(0)
        self.assertNotIn(ctx,self.host._retiring_contexts)
        self.assertIs(instance.private_contexts[ctx],context)
        self.assertIn(ctx,self.host._closing_contexts)
        self.assertIs(self.host._tabs[healthy.id],healthy)
        self.assertIs(instance.private_contexts[other],other_context)
        self.assertEqual(await self.host._close_context(ctx),{"ctx":ctx,"closed":True})
        self.assertFalse(incoming.exists());self.assertIs(self.host._instances[H.PRIVATE],instance)
        self.assertIs(self.host._tabs[healthy.id],healthy)
        other_context.close.assert_not_awaited()

    async def test_private_cleanup_failure_is_retryable_and_reported(self):
        folder=config.BROWSER_DIR/"private"/"run-test"
        folder.mkdir(parents=True,mode=0o700)
        instance=H._Instance(H.PRIVATE,folder,SimpleNamespace(close=AsyncMock()),None)
        self.host._instances[H.PRIVATE]=instance
        with patch.object(H,"stop_pids",return_value=[]),patch.object(H.shutil,"rmtree",side_effect=OSError):
            with self.assertRaises(H.BrowserRefused):await self.host._close_instance(H.PRIVATE)
        self.assertIs(self.host._closing_instances[H.PRIVATE],instance)
        with patch.object(H,"stop_pids",return_value=[]):await self.host._close_instance(H.PRIVATE)
        self.assertFalse(folder.exists());self.assertNotIn(H.PRIVATE,self.host._closing_instances)

    async def test_lease_release_notifies_gate_only_after_last_owner(self):
        self.host._gate=SimpleNamespace(revoke_closed_loopback=Mock())
        a=SimpleNamespace(id="a",loopback_port=33333,pending_port=None)
        b=SimpleNamespace(id="b",loopback_port=33333,pending_port=None)
        self.host._tabs.update(a=a,b=b)
        self.host._sync_ports(a);self.host._sync_ports(b)
        a.loopback_port=None;self.host._sync_ports(a)
        self.assertTrue(self.host.loopback_open(33333))
        self.host._gate.revoke_closed_loopback.assert_not_called()
        b.loopback_port=None;self.host._sync_ports(b)
        self.assertFalse(self.host.loopback_open(33333))
        self.host._gate.revoke_closed_loopback.assert_called_once()

    async def test_cancelled_navigation_revokes_pending_port(self):
        entered=asyncio.Event();stopped=asyncio.Event()
        async def goto(*args,**kw):
            entered.set()
            try:await asyncio.Event().wait()
            finally:stopped.set()
        profile=self.ids[0]
        cdp=SimpleNamespace(on=Mock(),send=AsyncMock(return_value={}))
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp))
        page=SimpleNamespace(goto=goto,url="about:blank",context=context)
        instance=H._Instance(profile,config.BROWSER_DIR/profile,context,None,generation=1)
        tab=H._Tab("1234abcd",profile,None,page,cdp_lock=asyncio.Lock(),page_identity=1)
        self.host._instances[profile]=instance;self.host._tabs[tab.id]=tab
        self.host.activity.opened(profile,instance.generation)
        going=asyncio.create_task(self.host._goto(tab,"http://127.0.0.1:33333",owner=True))
        starting=asyncio.create_task(entered.wait())
        try:
            done,_=await asyncio.wait({going,starting},timeout=2,return_when=asyncio.FIRST_COMPLETED)
            self.assertTrue(done,"navigation neither entered goto nor settled within the fixture deadline")
            if going in done:
                self.fail("navigation settled before goto: "+repr(going.result()))
            self.assertTrue(starting.done());self.assertTrue(self.host.loopback_open(33333))
            context.new_cdp_session.assert_awaited_once_with(page)
            cdp.send.assert_any_await("Fetch.enable",{"handleAuthRequests":True})
            going.cancel()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(going,2)
            self.assertTrue(stopped.is_set(),"actual navigation child did not settle")
            self.assertIsNone(tab.pending_port);self.assertFalse(self.host.loopback_open(33333))
            self.assertEqual(self.host.activity.counts(profile)[0],0)
            self.assertFalse(self.host._auth._pending)
        finally:
            for task in (going,starting):
                if not task.done():task.cancel()
            await asyncio.wait_for(asyncio.gather(going,starting,return_exceptions=True),2)
            await asyncio.wait_for(self.host._auth.settle(),2)

    async def test_admission_failure_does_not_open_a_port(self):
        self.host._tabs={str(n):SimpleNamespace(profile=self.ids[0]) for n in range(4)}
        with patch.object(config,"BROWSER_TAB_CAP",4),self.assertRaises(H.BrowserRefused):
            await self.host._open_tab(self.ids[0],None,"http://127.0.0.1:33333",owner=True)
        self.assertFalse(self.host.loopback_open(33333))

    async def test_idle_timer_is_cancelled_when_same_instance_reused(self):
        profile=self.ids[0]
        instance=H._Instance(profile,config.BROWSER_DIR/profile,SimpleNamespace(),SimpleNamespace())
        self.host._instances[profile]=instance
        closed=[]
        async def close(value):closed.append(value);self.host._instances.pop(value,None)
        self.host._close_instance=close
        with patch.object(H,"PROFILE_IDLE_S",0):
            self.host._idle_if_empty(instance)
            await self.host._instance(profile)
            await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertEqual(closed,[])
            self.host._idle_if_empty(instance)
            for _ in range(4):await asyncio.sleep(0)
            self.assertEqual(closed,[profile])
        self.assertEqual(H.PROFILE_IDLE_S,300)


class Real(O.OwnerBrowser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.llm_guard=patch.object(llm,"chat",side_effect=AssertionError("unscripted model call"))
        cls.llm_guard.start()
        config.BROWSER_TAB_CAP=4

    @classmethod
    def tearDownClass(cls):
        cls.llm_guard.stop()
        super().tearDownClass()

    def setUp(self):
        for profile in list(self.host._instances):self.host.close_profile(profile)
        for row in self.host.list_profiles():self.host.delete_profile(row["id"])
        self.main=self.host.create_profile("Main")["id"]
        self.url=f"http://127.0.0.1:{self.fx}/{O.MARK}/page"

    def tearDown(self):
        pids=O.chrome_tree(config.BROWSER_DIR)
        rss=O.rss_bytes(pids)
        self.measured[f"rss_{self._testMethodName}"]=round(rss/2**20,1)
        self.assertLess(rss,1.5*2**30,"Chrome RSS sum exceeded 1.5 GiB")
        for profile in list(self.host._instances):self.host.close_profile(profile)
        self.assertEqual(O.chrome_tree(config.BROWSER_DIR),[])

    def open(self,profile=None,**extra):
        return self.owner("POST","/browser/tabs",{"profile":profile or self.main,"url":self.url,**extra})

    def test_01_routes_metadata_and_boundaries(self):
        self.assertEqual(self.host.status()["instances"],[])
        row=self.owner("POST","/browser/profiles",{"label":"../x"})
        identity=row["id"]
        self.assertFalse((config.BROWSER_DIR.parent/"x").exists())
        self.assertEqual(self.owner("PATCH",f"/browser/profiles/{identity}",{"label":"Work"})["label"],"Work")
        routes=[("GET","/browser/profiles",None),("POST","/browser/profiles",{"label":"Nope"}),
                ("PATCH",f"/browser/profiles/{identity}",{"label":"Nope"}),
                ("DELETE",f"/browser/profiles/{identity}",None),
                ("POST",f"/browser/profiles/{identity}/forget",{}),
                ("POST",f"/browser/profiles/{identity}/signin",{}),
                ("DELETE","/browser/contexts/deadbeef",None)]
        for method,path,body in routes:
            with self.subTest(path=path,method=method):
                self.assertEqual(self.ask(method,path,body,listener="api")[0],403)
                self.assertEqual(self.ask(method,path,body,headers={"Host":"attacker.test"})[0],403)
                if method!="GET":self.assertEqual(self.ask(method,path,body,origin=False)[0],403)
        self.owner("DELETE",f"/browser/profiles/{identity}")
        self.assertEqual(self.host.status()["instances"],[])

    def test_02_profile_cookie_isolation_and_persistence(self):
        other=self.host.create_profile("Other")["id"]
        row=self.open();page=self.page(row["tab_id"])
        self.run_on(page.context.add_cookies([{"url":self.url,"name":"profile-cookie","value":"A","expires":time.time()+3600}]))
        self.host.close_profile(self.main)
        row=self.open(other)
        self.assertFalse(any(c["name"]=="profile-cookie" for c in self.run_on(self.page(row["tab_id"]).context.cookies())))
        self.host.close_profile(other)
        row=self.open()
        self.assertTrue(any(c["name"]=="profile-cookie" and c["value"]=="A" for c in self.run_on(self.page(row["tab_id"]).context.cookies())))
        self.measured["webdriver"]=self.run_on(self.page(row["tab_id"]).evaluate("navigator.webdriver"))

    def test_03_private_contexts_and_cleanup(self):
        a=self.open("private");b=self.open("private")
        self.assertNotEqual(a["ctx"],b["ctx"])
        context=self.page(a["tab_id"]).context
        self.run_on(context.add_cookies([{"url":self.url,"name":"private-cookie","value":"A"}]))
        self.assertFalse(any(c["name"]=="private-cookie" for c in self.run_on(self.page(b["tab_id"]).context.cookies())))
        self.owner("DELETE",f"/browser/contexts/{a['ctx']}")
        self.assertIn(b["tab_id"],self.host._tabs)
        self.owner("DELETE",f"/browser/contexts/{b['ctx']}")
        self.assertTrue(self.wait(lambda:H.PRIVATE not in self.host._instances,5))
        self.assertFalse(list((config.BROWSER_DIR/"private").glob("run-*")))
        self.assertFalse((config.BROWSER_DIR/"downloads"/"private").exists())
        c=self.open("private")
        self.assertFalse(any(cookie["name"]=="private-cookie" for cookie in self.run_on(self.page(c["tab_id"]).context.cookies())))

    def test_04_forget_removes_only_target_data(self):
        other=self.host.create_profile("Keep")["id"]
        for profile in (self.main,other):
            for parent in ("profiles","downloads"):
                folder=H.secure_dir(config.BROWSER_DIR/parent/profile)
                (folder/"sentinel").write_text("fixture")
        self.open()
        self.owner("POST",f"/browser/profiles/{self.main}/forget",{})
        for parent in ("profiles","downloads"):
            self.assertFalse((config.BROWSER_DIR/parent/self.main/"sentinel").exists())
            self.assertTrue((config.BROWSER_DIR/parent/other/"sentinel").exists())
        self.assertEqual(self.host._profiles.require(self.main)["label"],"Main")
        self.assertFalse(self.host.loopback_open(self.fx))

    def test_05_loopback_last_owner_tab_leaves(self):
        a=self.open();b=self.open()
        self.assertTrue(self.host.loopback_open(self.fx))
        self.owner("DELETE",f"/browser/tabs/{a['tab_id']}")
        self.assertTrue(self.host.loopback_open(self.fx))
        self.owner("POST",f"/browser/tabs/{b['tab_id']}/navigate",{"url":"about:blank"})
        self.assertFalse(self.host.loopback_open(self.fx))
        row=self.host.open_tab(url=self.url,owner=False)
        self.assertFalse(row["loaded"])
        self.assertFalse(self.host.loopback_open(self.fx))

    def test_06_exact_signin_entry_and_generic_refusal(self):
        row=self.owner("POST",f"/browser/profiles/{self.main}/signin",{})
        page=self.page(row["tab_id"])
        self.assertTrue(self.wait(lambda:page.url=="chrome://settings/people",5))
        self.run_on(page.wait_for_load_state("load"))
        self.assertEqual(self.run_on(page.title()),"Settings - You and Google")
        for url in ("chrome://settings/people","chrome://settings/syncSetup","chrome://settings/passwords",
                    "chrome://settings/people?x=1","chrome://settings/people#x","chrome://version"):
            self.assertEqual(self.ask("POST",f"/browser/tabs/{row['tab_id']}/navigate",{"url":url})[0],400)
        self.assertEqual(self.ask("POST","/browser/profiles/private/signin",{})[0],400)
        self.assertNotIn("--disable-sync",H.launch_args(Path("/tmp/fixture"),1234,private=False))
        self.assertEqual(self.host.status()["sync"]["password_store"],"basic")

    def test_06b_internal_capability_ends_on_leave_and_refuses_other_pages(self):
        row=self.owner("POST",f"/browser/profiles/{self.main}/signin",{})
        page=self.page(row["tab_id"])
        self.assertTrue(self.wait(lambda:page.url=="chrome://settings/people",5))
        try:self.run_on(page.goto("chrome://version",wait_until="commit"))
        except H.BrowserRefused:pass
        self.assertTrue(self.wait(lambda:page.url=="about:blank",5))
        self.assertFalse(self.host._tabs[row["tab_id"]].signin)
        try:self.run_on(page.goto(H.SIGNIN_URL,wait_until="commit"))
        except H.BrowserRefused:pass
        self.assertTrue(self.wait(lambda:page.url=="about:blank",5))

    def test_06c_page_navigation_revokes_without_creating_a_lease(self):
        row=self.open();page=self.page(row["tab_id"])
        self.run_on(page.goto("about:blank"))
        self.assertFalse(self.host.loopback_open(self.fx))
        try:self.run_on(page.goto(self.url,wait_until="commit"))
        except H.BrowserRefused:pass
        self.assertFalse(self.host.loopback_open(self.fx))
        self.assertTrue(self.wait(lambda:page.url=="chrome-error://chromewebdata/",5))

    def test_07_holds_and_wipe_failure_are_reported(self):
        self.open()
        with patch.object(self.host,"input_held",return_value=True):
            for method,path,body in (("DELETE",f"/browser/profiles/{self.main}",None),
                    ("POST",f"/browser/profiles/{self.main}/forget",{}),
                    ("POST",f"/browser/profiles/{self.main}/signin",{}),
                    ("POST","/browser/profiles",{"label":"Held"})):
                self.assertEqual(self.ask(method,path,body)[0],409)
        with patch.object(H.shutil,"rmtree",side_effect=OSError("fixture denial")):
            self.assertEqual(self.ask("DELETE",f"/browser/profiles/{self.main}")[0],409)
        self.assertEqual(self.host._profiles.require(self.main)["id"],self.main)


def load_tests(loader,tests,pattern):
    # Inherit fixture helpers, not the earlier ordered suite's 28 test methods.
    return unittest.TestSuite(cls(name) for cls in (Store,Lifecycle,Real)
                              for name in sorted(cls.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

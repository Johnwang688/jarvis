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
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock, Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis import config, llm
from jarvis.v2.browser import gate as G, host as H, profiles as P
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


class SigninAdoption(unittest.IsolatedAsyncioTestCase):
    """`_signin_popup`'s checks one at a time, on doubles: each is required."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="profiles-signin-");self.addCleanup(self.tmp.cleanup)
        guard=patch.object(config,"BROWSER_DIR",Path(self.tmp.name)/"browser")
        guard.start();self.addCleanup(guard.stop)
        self.host=H.BrowserHost()
        self.url="https://accounts.google.com/signin/chrome/sync?ssp=1&continue=https%3A%2F%2Fwww.google.com%2F"
        # Google's 302 for Chrome's request (FakeGaia does the same).
        self.landed="https://accounts.google.com/v3/signin/identifier?ssp=1&flowName=GlifDesktopChromeSync"

    def tab(self,tid,*,opener=None,entries=None,**fields):
        sent=[]
        cdp=SimpleNamespace(send=AsyncMock(return_value={"currentIndex":0,"entries":entries if entries is not None
                            else [{"url":self.landed,"userTypedURL":self.url,"transitionType":"auto_bookmark"}]}),
                            detach=AsyncMock())
        page=SimpleNamespace(opener=AsyncMock(return_value=opener))
        page.context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=cdp))
        tab=H._Tab(id=tid,profile="p-11111111",ctx=None,page=page,
                   stream=SimpleNamespace(send=lambda m:sent.append(m),attached=lambda:None),**fields)
        tab.sent=sent;self.host._tabs[tid]=tab
        return tab

    def pair(self,**parent):
        now=asyncio.get_running_loop().time()
        fields=dict(signin=True,signin_press=now);fields.update(parent)
        return self.tab("aaaaaaaa",**fields),self.tab("bbbbbbbb",unsolicited=True)

    async def popup(self,signin,tab,landed=None):
        await self.host._signin_popup(tab,landed or self.landed)
        return tab.opener==signin.id and signin.sent==[{"t":"popup","tab_id":tab.id}]

    async def test_all_conditions_adopt_once(self):
        signin,tab=self.pair()
        self.assertTrue(await self.popup(signin,tab))
        self.assertTrue(signin.signin_adopted)
        again=self.tab("cccccccc",unsolicited=True)
        await self.host._signin_popup(again,self.landed)
        self.assertIsNone(again.opener)

    async def test_unredirected_and_unreachable_requests_adopt(self):
        for name,landed,entry in (
                ("no redirect",self.url,{"url":self.url,"userTypedURL":"","transitionType":"auto_bookmark"}),
                ("Google unreachable",H.SIGNIN_ERROR,{"url":self.url,"userTypedURL":"","transitionType":"auto_bookmark"})):
            with self.subTest(name):
                self.host._tabs.clear()
                signin,_=self.pair();tab=self.tab("bbbbbbbb",unsolicited=True,entries=[entry])
                self.assertTrue(await self.popup(signin,tab,landed))

    async def test_each_condition_is_required(self):
        now=asyncio.get_running_loop().time()
        cases={"a page's pop-up (opener)":({},dict(opener=object())),
               "a page's open (link transition)":({},dict(entries=[{"url":self.url,"transitionType":"link"}])),
               "more history":({},dict(entries=[{"url":self.url,"transitionType":"auto_bookmark"}]*2)),
               "another request":({},dict(entries=[{"url":self.landed,"transitionType":"auto_bookmark",
                    "userTypedURL":"https://accounts.google.com/o/oauth2/auth?client_id=x"}])),
               "a request on a look-alike host":({},dict(entries=[{"url":self.landed,"transitionType":"auto_bookmark",
                    "userTypedURL":self.url.replace("google.com","google.com.evil")}])),
               "redirected off Google's origin":({},dict(entries=[{"url":"https://evil.test/signin",
                    "userTypedURL":self.url,"transitionType":"auto_bookmark"}])),
               "redirected to another port":({},dict(entries=[{"url":self.landed.replace(".com/",".com:444/"),
                    "userTypedURL":self.url,"transitionType":"auto_bookmark"}])),
               "a page's open behind the error page":({},dict(entries=[{"url":"https://accounts.google.com/o/oauth2/auth",
                    "userTypedURL":"","transitionType":"auto_bookmark"}],landed=H.SIGNIN_ERROR)),
               "the capability ended":(dict(signin=False),{}),
               "no owner press":(dict(signin_press=None),{}),
               "a stale press":(dict(signin_press=now-H.SIGNIN_ADOPT_S-1),{}),
               "already adopted":(dict(signin_adopted=True),{}),
               "another profile":(dict(profile="p-22222222"),{})}
        for name,(parent,child) in cases.items():
            with self.subTest(name):
                self.host._tabs.clear()
                fields=dict(signin=True,signin_press=now);fields.update(parent)
                profile=fields.pop("profile","p-11111111")
                signin=self.tab("aaaaaaaa",**fields);signin.profile=profile
                child=dict(child);landed=child.pop("landed",None)
                tab=self.tab("bbbbbbbb",unsolicited=True,**child)
                self.assertFalse(await self.popup(signin,tab,landed))
                self.assertIsNone(tab.opener);self.assertEqual(signin.sent,[])

    async def test_an_opener_named_meanwhile_wins(self):
        signin,tab=self.pair()
        async def late():
            tab.opener="cccccccc";return None    # `_link_opener` answered first
        tab.page.opener=late
        self.assertFalse(await self.popup(signin,tab))
        self.assertEqual(tab.opener,"cccccccc")

    def test_exact_address_only(self):
        good="https://accounts.google.com/signin/chrome/sync"
        self.assertTrue(H.BrowserHost._signin_address(good+"?ssp=1&continue=x"))
        for url in (good.replace("accounts.google.com","accounts.google.com.evil"),
                    good.replace("accounts.google.com","evil.accounts.google.com"),
                    good.replace("accounts.google.com","accounts.google.com:444"),
                    good.replace("accounts.google.com","x@accounts.google.com"),
                    good.replace("https:","http:"),good+"/x",good+"x",
                    "https://accounts.google.com/o/oauth2/auth?client_id=x","https://accounts.google.com/",
                    "chrome://settings/people","not a url"):
            with self.subTest(url=url):self.assertFalse(H.BrowserHost._signin_address(url))

    def test_landing_on_googles_origin_or_the_error_page_only(self):
        for url in (self.landed,self.url,"https://accounts.google.com/",H.SIGNIN_ERROR):
            with self.subTest(url=url):self.assertTrue(H.BrowserHost._signin_landing(url))
        for url in ("https://accounts.google.com.evil/","https://evil.accounts.google.com/",
                    "https://accounts.google.com:444/","https://x@accounts.google.com/",
                    "http://accounts.google.com/","chrome-error://other/","chrome://settings/people",
                    "about:blank",None):
            with self.subTest(url=url):self.assertFalse(H.BrowserHost._signin_landing(url))


class FakeGaia:
    """A local TLS stand-in for `accounts.google.com`: the gate's resolver
    answers the suite's public address for that one name and the connector
    carries port 443 to this server, so Chrome's own sign-in tab lands on
    the real URL and nothing leaves loopback. Chrome is told to ignore
    certificate errors only in the suite that uses it."""

    def __init__(self, root: Path):
        cert, key = root / "gaia-cert.pem", root / "gaia-key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-subj", "/CN=accounts.google.com",
                        "-addext", "subjectAltName=DNS:accounts.google.com"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hits = self.hits = []
        self.headers = []
        gaia = self
        self.delay = 0.0

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                gaia.headers.append(dict(self.headers))
                time.sleep(gaia.delay)
                path, _, query = self.path.partition("?")
                if path == "/signin/chrome/sync":
                    # As accounts.google.com answers it (PR #217 delta review):
                    # with Chrome's account-consistency header, a 302 to the
                    # identifier page; without it, nothing.
                    if "X-Chrome-ID-Consistency-Request" not in self.headers:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self.send_response(302)
                    self.send_header("Location", "/v3/signin/identifier?" + query
                                     + "&flowName=GlifDesktopChromeSync")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = f"<!doctype html><title>{O.MARK} gaia</title><p>fixture sign-in".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def patches(self, host):
        port = self.server.server_address[1]
        resolve, connect = host._gate._resolver, host._gate._connector

        def resolver(name, p):
            return [O.PUB] if name == "accounts.google.com" else resolve(name, p)

        async def connector(address, p):
            if address == O.PUB and p == 443:
                return await G.default_connect("127.0.0.1", port)
            return await connect(address, p)
        args = H.launch_args
        return [patch.object(host._gate, "_resolver", resolver),
                patch.object(host._gate, "_connector", connector),
                patch.object(H, "launch_args", lambda *a, **k: args(*a, **k) + ["--ignore-certificate-errors"])]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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

    def test_06d_signin_route_beside_an_open_tab_is_never_replaced(self):
        # The live shape (2026-10-15): the profile's Chrome already holds an
        # owner tab, so the sign-in page is a new page and its own
        # about:blank lands first. The route must still end on Chrome's page.
        first=self.open()
        guarded=self.host._guarded;mark=len(self.logs)
        row=self.owner("POST",f"/browser/profiles/{self.main}/signin",{})
        page=self.page(row["tab_id"])
        self.assertTrue(self.wait(lambda:page.url=="chrome://settings/people",5))
        self.run_on(page.wait_for_load_state("load"))
        self.assertEqual(self.run_on(page.title()),"Settings - You and Google")
        self.assertTrue(self.host._tabs[row["tab_id"]].signin)
        self.assertEqual(self.host._guarded,guarded)
        self.assertFalse([l for l in self.logs[mark:] if "landing guard replaced" in l])
        self.owner("DELETE",f"/browser/tabs/{first['tab_id']}")   # the suite's RSS bound
        # Any other settings page, even one the people page links to, is not
        # part of the capability: replaced, and the capability ends.
        self.run_on(page.get_by_text("Customize profile",exact=True).first.click(timeout=5000))
        self.assertTrue(self.wait(lambda:page.url=="about:blank",5))
        self.assertFalse(self.host._tabs[row["tab_id"]].signin)
        self.assertEqual(self.host._guarded,guarded+1)

    # -- Chrome's own Google sign-in tab (2026-10-15 review: MED 1) -------------

    def gaia(self):
        """FakeGaia behind the gate, and a record of every window message."""
        gaia=FakeGaia(Path(self.tmp.name));self.addCleanup(gaia.close)
        self.host._ensure_started()           # the gate exists once the host runs
        for p in gaia.patches(self.host):p.start();self.addCleanup(p.stop)
        self.record()
        return gaia

    def record(self):
        self.sent=[];send=H.TabStream.send
        def record(stream,message,**kw):
            self.sent.append((stream,message));return send(stream,message,**kw)
        guard=patch.object(H.TabStream,"send",record);guard.start();self.addCleanup(guard.stop)

    def signin_tab(self):
        row=self.owner("POST",f"/browser/profiles/{self.main}/signin",{})
        tab=self.host._tabs[row["tab_id"]]
        self.assertTrue(self.wait(lambda:tab.page.url=="chrome://settings/people",5))
        self.run_on(tab.page.wait_for_load_state("load"))
        return tab

    def owner_click(self,tab,locator):
        """A click by the owner's own path: `_deliver`, as the window's
        stream pump sends it (driving never goes through it)."""
        box=self.run_on(locator.bounding_box())
        point={"x":box["x"]+box["width"]/2,"y":box["y"]+box["height"]/2,"modifiers":0,"clickCount":1,
               "pointerType":"mouse"}
        for kind,buttons in (("mousePressed",1),("mouseReleased",0)):
            self.run_on(self.host._deliver(tab,"Input.dispatchMouseEvent",
                                           {"type":kind,"button":"left","buttons":buttons,**point}))

    def new_tab(self,act,landed=lambda t:t.page.url.startswith("https://accounts.google.com/")):
        before=set(self.host._tabs);act()
        self.assertTrue(self.wait(lambda:len(set(self.host._tabs)-before)==1,5))
        tab=self.host._tabs[next(iter(set(self.host._tabs)-before))]
        self.assertTrue(self.wait(lambda:landed(tab),5),tab.page.url)
        return tab

    def unshown(self,tab,why):
        time.sleep(0.7)                       # longer than the check's own CDP round trip
        self.assertIsNone(tab.opener,why)
        self.assertFalse([m for _,m in self.sent if m.get("t")=="popup" and m.get("tab_id")==tab.id],why)
        self.owner("DELETE",f"/browser/tabs/{tab.id}")

    def chrome_button(self,signin):
        return signin.page.get_by_role("button",name="Sign in to Chrome").first

    def test_06e_chromes_own_signin_tab_opens_beside_the_signin_tab(self):
        gaia=self.gaia()
        offered=[];link=self.host._link_signin
        def spy(tab,url):offered.append(tab.id);return link(tab,url)
        guard=patch.object(self.host,"_link_signin",spy);guard.start();self.addCleanup(guard.stop)
        signin=self.signin_tab()
        self.assertNotIn(signin.id,offered)   # Jarvis's own page, from its first landing
        tab=self.new_tab(lambda:self.owner_click(signin,self.chrome_button(signin)))
        self.assertTrue(self.wait(lambda:tab.opener==signin.id,5))
        self.assertIn(tab.id,offered)
        # Chrome's request, with its account-consistency header, and
        # Google's 302 (as FakeGaia answers it) to the identifier page.
        self.assertTrue(tab.page.url.startswith("https://accounts.google.com/v3/signin/identifier?ssp=1&"),tab.page.url)
        self.assertIn("flowName=GlifDesktopChromeSync",tab.page.url)
        first=next(n for n,path in enumerate(gaia.hits) if path.startswith("/signin/chrome/sync?"))
        self.assertIn("X-Chrome-ID-Consistency-Request",gaia.headers[first])
        self.assertIn((signin.stream,{"t":"popup","tab_id":tab.id}),self.sent)
        self.assertEqual({r["tab_id"]:r["opener"] for r in self.owner("GET","/browser/tabs")},
                         {signin.id:None,tab.id:signin.id})
        self.assertTrue(signin.signin_adopted)
        self.assertFalse(tab.signin)          # Google's page holds no internal capability
        self.assertEqual(signin.page.url,"chrome://settings/people")
        try:self.run_on(tab.page.goto("chrome://settings/people",wait_until="commit"))
        except Exception:pass
        self.assertTrue(self.wait(lambda:tab.page.url=="about:blank",5))
        self.owner("DELETE",f"/browser/tabs/{tab.id}")
        # At most once per sign-in capability: the owner's second press opens
        # Chrome's tab again, unshown.
        again=self.new_tab(lambda:self.owner_click(signin,self.chrome_button(signin)))
        self.unshown(again,"a second adoption")
        # Jarvis's own tab at the exact address is never taken for Chrome's.
        self.owner_click(signin,signin.page.get_by_text("You and Google").first)
        own=self.new_tab(lambda:self.owner("POST","/browser/tabs",{"profile":self.main,
            "url":"https://accounts.google.com/signin/chrome/sync?ssp=1&own=1"}))
        self.assertNotIn(own.id,offered)
        self.unshown(own,"Jarvis's own tab")

    def test_06f_pages_cannot_forge_chromes_signin_tab(self):
        # The owner has just pressed in the sign-in tab, so only the page's
        # origin of the open stands between it and the pane (measured: none
        # of these fire Page.windowOpen, and none has an opener).
        self.gaia();signin=self.signin_tab()
        other=self.page(self.open()["tab_id"])
        url="https://accounts.google.com/signin/chrome/sync?ssp=1&continue=https%3A%2F%2Fwww.google.com%2F"
        self.run_on(other.set_content(f'<a id=a href="{url}">a</a><button id=s onclick="'
            "document.getElementById('a').dispatchEvent(new MouseEvent('click',{ctrlKey:true,bubbles:true,cancelable:true}))"
            '">s</button>'))
        shapes={"Ctrl-click":lambda:self.run_on(other.click("#a",modifiers=["Control"])),
                "middle-click":lambda:self.run_on(other.click("#a",button="middle")),
                "Shift-click":lambda:self.run_on(other.click("#a",modifiers=["Shift"])),
                # a driven click (CDP input, as driving sends) on a page that turns it into one
                "driven click, scripted Ctrl-click":lambda:self.run_on(other.click("#s")),
                "scripted Ctrl-click, no input":lambda:self.run_on(other.evaluate(
                    "()=>document.getElementById('a').dispatchEvent(new MouseEvent('click',{ctrlKey:true,bubbles:true,cancelable:true}))"))}
        for name,act in shapes.items():
            with self.subTest(shape=name):
                self.owner_click(signin,signin.page.get_by_text("You and Google").first)
                self.unshown(self.new_tab(act),name)
        self.assertFalse(signin.signin_adopted)
        self.owner("DELETE",f"/browser/tabs/{next(t.id for t in self.host._tabs.values() if t.page is other)}")

    def deliver(self,tab,method,params):
        self.run_on(self.host._deliver(tab,method,{"modifiers":0,**params}))

    def test_06g_only_soon_after_the_owners_press_and_while_signin_lasts(self):
        gaia=self.gaia();signin=self.signin_tab()
        # Only a press opens the window: a hover, a wheel or a key's release
        # by the owner does not.
        key={"code":"ShiftLeft","key":"Shift","windowsVirtualKeyCode":16}
        self.deliver(signin,"Input.dispatchMouseEvent",{"type":"mouseMoved","x":20,"y":20,"button":"none","buttons":0})
        self.deliver(signin,"Input.dispatchMouseEvent",{"type":"mouseWheel","x":20,"y":20,"deltaX":0,"deltaY":40})
        self.deliver(signin,"Input.dispatchKeyEvent",{"type":"keyUp",**key})
        self.deliver(signin,"Input.dispatchMouseEvent",{"type":"mouseReleased","x":20,"y":20,"button":"left","buttons":0,"clickCount":1})
        self.assertIsNone(signin.signin_press)
        self.unshown(self.new_tab(lambda:self.run_on(self.chrome_button(signin).click())),"no press, only input")
        self.deliver(signin,"Input.dispatchKeyEvent",{"type":"rawKeyDown",**key})
        self.assertIsNotNone(signin.signin_press)
        self.deliver(signin,"Input.dispatchKeyEvent",{"type":"keyUp",**key})
        signin.signin_press=None
        # Chrome's own tab without a recent owner press (pressed long ago,
        # or a driven click) stays unshown.
        with patch.object(H,"SIGNIN_ADOPT_S",0.3):
            self.owner_click(signin,signin.page.get_by_text("You and Google").first)
            time.sleep(0.6)
            self.unshown(self.new_tab(lambda:self.run_on(self.chrome_button(signin).click())),"a stale press")
        self.assertFalse(signin.signin_adopted)
        # The capability ended before Google's page landed: unshown.
        gaia.delay=2.0
        tab=self.new_tab(lambda:self.owner_click(signin,self.chrome_button(signin)),
                         landed=lambda t:True)
        self.owner("POST",f"/browser/tabs/{signin.id}/navigate",{"url":"about:blank"})
        self.assertFalse(signin.signin)
        self.assertTrue(self.wait(lambda:tab.page.url.startswith("https://accounts.google.com/"),6),tab.page.url)
        self.unshown(tab,"an ended capability")

    def test_06h_google_unreachable_shows_chromes_error_page_beside_signin(self):
        self.record()                         # no FakeGaia: the suite's gate reaches no Google
        signin=self.signin_tab()
        tab=self.new_tab(lambda:self.owner_click(signin,self.chrome_button(signin)),
                         landed=lambda t:t.page.url==H.SIGNIN_ERROR)
        self.assertTrue(self.wait(lambda:tab.opener==signin.id,5))
        self.assertIn((signin.stream,{"t":"popup","tab_id":tab.id}),self.sent)
        self.assertEqual(self.run_on(tab.page.title()),"accounts.google.com")
        self.owner("DELETE",f"/browser/tabs/{tab.id}")   # the suite's RSS bound

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
    return unittest.TestSuite(cls(name) for cls in (Store,Lifecycle,SigninAdoption,Real)
                              for name in sorted(cls.__dict__) if name.startswith("test_"))

if __name__=="__main__":unittest.main(verbosity=2)

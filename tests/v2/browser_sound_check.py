"""Memory-only owner sound grants and exact replacement launch authorization."""
from pathlib import Path
import asyncio, sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, audio as A
from jarvis.v2.browser.sound import BrowserSound
from jarvis.v2.browser.settings import BrowserSettings

class Sound(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host=H.BrowserHost()
        self.sound=BrowserSound(self.host,probe=lambda:A.Availability(True,False))
        self.host._sound=self.sound
        self.profile="p-12345678"
        self.host._daemon=SimpleNamespace(approvals=SimpleNamespace(pending=lambda:[]),bus=Mock())
        self.instance=H._Instance(self.profile,Path("/synthetic/profile"),Mock(),Mock(),generation=1)
        self.host._instances[self.profile]=self.instance
        self.host.activity.opened(self.profile,1)
        self.tab=SimpleNamespace(id="1234abcd",profile=self.profile)
        self.host._tabs[self.tab.id]=self.tab
        # publish reads actual host tab metadata; this isolated fixture pins only grants.
        self.sound.publish=Mock()
    async def asyncTearDown(self):
        self.host._stopped=True
        await self.sound.settle()
    async def test_discovery_and_master_enable_never_grant_sound_or_start_chrome(self):
        self.host._ensure_started=Mock()
        self.assertTrue(self.sound.status()["desired"])
        self.assertFalse(self.sound.preference(self.profile)[0])
        self.host._settings.update(sound=False);self.sound.sync()
        self.host._settings.update(sound=True);self.host.settings_changed(sound_requested=True)
        self.assertFalse(self.sound.preference(self.profile)[0])
        self.host._ensure_started.assert_not_called()
        self.assertIsNone(self.host._thread)
    async def test_cold_launch_muted_even_with_owner_grant_in_memory(self):
        await self.sound.change(self.tab.id,True)
        result=self.sound.launch(["--mute-audio","--sandbox"],{"PULSE_SERVER":"ambient"},profile=self.profile)
        self.assertFalse(result.applied);self.assertIn("--mute-audio",result.args)
        self.assertNotIn("PULSE_SERVER",result.env)
    async def test_only_exact_committed_current_generation_replacement_is_audible(self):
        await self.sound.change(self.tab.id,True)
        generation=self.sound.preference()[1]
        token=self.host.activity.try_restart(self.profile,1,generation)
        self.assertFalse(self.sound.launch(["--mute-audio"],{},profile=self.profile,restart_token=token).applied)
        self.assertTrue(self.host.activity.commit_restart(token))
        result=self.sound.launch(["--mute-audio","--sandbox"],{"FONTCONFIG_FILE":"synthetic"},profile=self.profile,restart_token=token)
        self.assertTrue(result.applied);self.assertNotIn("--mute-audio",result.args)
        self.assertEqual(result.env,{"FONTCONFIG_FILE":"synthetic","PULSE_SERVER":A.PULSE_SERVER})
        self.assertFalse(self.sound.launch(["--mute-audio"],{},profile="p-87654321",restart_token=token).applied)
        await self.sound.change(self.tab.id,True)
        self.assertFalse(self.sound.launch(["--mute-audio"],{},profile=self.profile,restart_token=token).applied)
        await self.sound.change(self.tab.id,False)
        self.assertFalse(self.sound.launch(["--mute-audio"],{},profile=self.profile,restart_token=token).applied)
    async def test_master_off_clears_pending_gesture_and_on_does_not_restore_it(self):
        await self.sound.change(self.tab.id,True)
        self.host._settings.update(sound=False);self.sound.sync()
        self.assertEqual(self.sound._requests,set())
        with self.assertRaises(H.BrowserRefused):await self.sound.change(self.tab.id,True)
        self.host._settings.update(sound=True);self.sound.sync()
        self.assertFalse(self.sound.preference(self.profile)[0])
    async def test_profile_grant_inheritance_and_clear_are_explicit(self):
        await self.sound.change(self.tab.id,True)
        sibling=SimpleNamespace(profile=self.profile,generation=1,audio=A.InstanceAudio(True))
        self.assertTrue(self.sound.instance_status(sibling)["requested"])
        other=SimpleNamespace(profile="p-87654321",generation=2,audio=A.InstanceAudio(False))
        self.assertFalse(self.sound.instance_status(other)["requested"])
        self.sound.clear(self.profile)
        self.assertFalse(self.sound.preference(self.profile)[0])
        fresh=BrowserSound(self.host,probe=lambda:A.Availability(True,False))
        self.assertFalse(fresh.preference(self.profile)[0])
    async def test_private_cards_and_bad_shapes_cannot_create_a_grant(self):
        for bad in (None,1,"true",{},[]):
            with self.subTest(bad=bad),self.assertRaises(H.BrowserRefused):
                await self.sound.change(self.tab.id,bad)
        self.host._daemon.approvals.pending=lambda:[object()]
        with self.assertRaises(H.BrowserRefused):await self.sound.change(self.tab.id,True)
        self.host._daemon.approvals.pending=lambda:[]
        self.tab.profile="private"
        self.host._instances["private"]=SimpleNamespace(profile="private")
        with self.assertRaisesRegex(H.BrowserRefused,"Close Private"):await self.sound.change(self.tab.id,True)
        self.assertEqual(self.sound._requests,set())
    async def test_unavailable_and_tainted_instances_never_claim_ready_on(self):
        await self.sound.change(self.tab.id,True)
        self.sound.probe=lambda:A.Availability(False,False);self.sound.sync(explicit=True)
        self.assertEqual(self.sound.instance_status(self.instance)["reason"],"unavailable")
        self.instance.audio=A.InstanceAudio(True)
        attempt=self.instance.audio.begin_mute();self.instance.audio.finish_mute(attempt)
        self.assertEqual(self.sound.instance_status(self.instance)["phase"],"pending")
    async def test_last_tab_close_cancels_pending_unmute_before_waiting_for_lifecycle_lock(self):
        # Exact close during replacement has no live tab, only a bounded descriptor.
        self.sound._requests.add(self.profile)
        self.host._tabs.clear()
        self.sound.restarts.descriptors[self.tab.id]=SimpleNamespace(tab_id=self.tab.id,profile=self.profile)
        lock=asyncio.Lock();await lock.acquire();self.host._launching[self.profile]=lock
        close=asyncio.create_task(self.host._close_tab(self.tab.id));await asyncio.sleep(0)
        self.assertFalse(self.sound.preference(self.profile)[0])
        lock.release();self.assertTrue((await close)["closed"])
    async def test_actual_cold_host_launch_preserves_fonts_and_stays_muted(self):
        with tempfile.TemporaryDirectory(prefix="sound-cold-") as td:
            host=H.BrowserHost();host._sound=BrowserSound(host,probe=lambda:A.Availability(True,False))
            host._settings.update(sound=True);host._gate=SimpleNamespace(port=19000)
            context=SimpleNamespace(browser=SimpleNamespace(version="synthetic"),on=Mock())
            launch=AsyncMock(return_value=context);host._pw=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            # The gate check (B-LOGIN-W) needs a real Chrome and gate; this one is fake.
            with patch.object(H.BrowserHost,"_check_gate",AsyncMock(return_value=True)),patch.object(config,"BROWSER_DIR",Path(td)),patch.object(H,"stop_pids",return_value=0),patch.object(H,"font_environment",return_value={"FONTCONFIG_FILE":"synthetic","PULSE_SERVER":"ambient"}):
                # _launch reads the profile's password-store row (password_store.py).
                host._profiles._write([{"id":self.profile,"label":"Main","created":"2026-10-15T00:00:00+00:00"}])
                instance=await host._launch(self.profile)
            kwargs=launch.call_args.kwargs
            self.assertFalse(instance.audio.applied);self.assertIn("--mute-audio",kwargs["args"])
            self.assertEqual(kwargs["env"],{"FONTCONFIG_FILE":"synthetic"})
            self.assertTrue(kwargs["chromium_sandbox"]);self.assertTrue(kwargs["headless"])
    async def test_malformed_master_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"browser.json"
            self.assertTrue(BrowserSettings(path).value("sound"))
            for raw in ('{"sound":"true"}','bad','[]'):
                path.write_text(raw)
                self.assertFalse(BrowserSettings(path).value("sound"))
            path.write_text("{}");self.assertTrue(BrowserSettings(path).value("sound"))

class OwnerRoute(unittest.TestCase):
    def setUp(self):
        from jarvis.v2.browser import routes
        from jarvis.v2.daemon import APIError
        self.route,self.APIError=routes.route,APIError
        self.port=18341
        self.host=SimpleNamespace(set_sound=Mock(return_value={"on":True}))
        self.daemon=SimpleNamespace(face_port=self.port,browser=self.host,browser_settings=BrowserSettings())
    def handler(self, *, port=None, host=None, origin=None, body=None):
        target=host or f"127.0.0.1:{self.port}"
        headers={"Host":target,"Origin":origin or "http://"+target}
        return SimpleNamespace(command="POST",headers=headers,
            server=SimpleNamespace(server_address=("127.0.0.1",port or self.port)),_body=Mock(return_value=body or {"on":True}))
    def test_api_foreign_origin_and_bad_host_refused_before_body_or_engine(self):
        for handler in (self.handler(port=self.port+1),self.handler(origin="https://foreign.invalid"),self.handler(host="foreign.invalid:18341")):
            with self.subTest(headers=handler.headers),self.assertRaises(self.APIError) as caught:
                self.route(handler,self.daemon,["browser","tabs","1234abcd","sound"],{})
            self.assertEqual(caught.exception.status,403)
            handler._body.assert_not_called();self.host.set_sound.assert_not_called()
    def test_explicit_owner_boolean_body_and_unknown_fields_are_validated(self):
        handler=self.handler()
        self.assertEqual(self.route(handler,self.daemon,["browser","tabs","1234abcd","sound"],{}),(200,{"on":True}))
        self.host.set_sound.assert_called_once_with("1234abcd",True)
        self.host.set_sound.reset_mock()
        for body in ({"on":True,"unexpected":True},{"other":False}):
            with self.assertRaises(self.APIError):
                self.route(self.handler(body=body),self.daemon,["browser","tabs","1234abcd","sound"],{})
        self.host.set_sound.assert_not_called()

if __name__=="__main__":unittest.main()

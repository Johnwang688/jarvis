"""Sound coordination: synthetic launch/status and ordered preference changes."""
from pathlib import Path
import asyncio, sys, tempfile, threading, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from jarvis import config
from jarvis.v2.browser import host as H, audio as A
from jarvis.v2.browser.sound import BrowserSound

class Sound(unittest.TestCase):
    def setUp(self):
        self.host=H.BrowserHost()
        self.sound=BrowserSound(self.host,probe=lambda:A.Availability(True,False))
        self.host._sound=self.sound
    def test_discovery_and_explicit_settings_never_launch_a_cold_browser(self):
        self.host._ensure_started=Mock()
        self.assertEqual(self.sound.status(),{'desired':False,'generation':0,'available':True,'reason':None})
        self.host._settings.update(sound=True);self.host.settings_changed(sound_requested=True)
        self.assertEqual(self.sound.status()['generation'],1)
        self.assertTrue(self.sound.status()['desired']);self.assertIsNone(self.host._thread)
        self.host._ensure_started.assert_not_called()
    def test_unavailable_retry_keeps_desired_distinct_from_launch_mode(self):
        self.sound.probe=lambda:A.Availability(False,False)
        self.host._settings.update(sound=True);self.sound.sync(explicit=True)
        self.assertEqual(self.sound.status()['reason'],'unavailable')
        launch=self.sound.launch(['--mute-audio','--sandbox'],{'PULSE_SERVER':'untrusted'})
        self.assertFalse(launch.applied);self.assertNotIn('PULSE_SERVER',launch.env)
        self.sound.probe=lambda:A.Availability(True,False);self.sound.sync(explicit=True)
        self.assertIsNone(self.sound.status()['reason']);self.assertEqual(self.sound.status()['generation'],2)
        self.assertTrue(self.sound.launch(['--mute-audio'],{}).applied)
    def test_private_and_tainted_instance_never_claim_new_setting_applied(self):
        self.host._settings.update(sound=True);self.sound.sync()
        row=SimpleNamespace(profile='private',generation=3,audio=A.InstanceAudio(False))
        self.assertEqual(self.sound.instance_status(row),{'applied':False,'generation':3,'phase':'pending','reason':'private'})
        row.profile='p-12345678';row.audio=A.InstanceAudio(True)
        self.assertEqual(self.sound.instance_status(row)['phase'],'ready')
        attempt=row.audio.begin_mute();row.audio.finish_mute(attempt)
        self.assertEqual(self.sound.instance_status(row)['phase'],'pending')
    def test_overlapping_refresh_cannot_overwrite_the_newer_preference(self):
        entered=threading.Event();release=threading.Event();first=True
        def probe():
            nonlocal first
            if first:first=False;entered.set();self.assertTrue(release.wait(2))
            return A.Availability(True,True)
        self.sound.probe=probe;self.host._settings.update(sound=True)
        a=threading.Thread(target=lambda:self.sound.sync(explicit=True));a.start();self.assertTrue(entered.wait(2))
        self.host._settings.update(sound=False)
        b=threading.Thread(target=lambda:self.sound.sync(explicit=True));b.start();release.set();a.join(2);b.join(2)
        self.assertFalse(a.is_alive());self.assertFalse(b.is_alive())
        self.assertEqual(self.sound.status()['desired'],False);self.assertEqual(self.sound.status()['generation'],2)

class Launch(unittest.IsolatedAsyncioTestCase):
    async def test_actual_host_launch_composes_fonts_and_curated_sound_environment(self):
        with tempfile.TemporaryDirectory(prefix='sound-cold-') as td:
            host=H.BrowserHost();host._sound=BrowserSound(host,probe=lambda:A.Availability(True,False))
            host._settings.update(sound=True);host._gate=SimpleNamespace(port=19000)
            context=SimpleNamespace(browser=SimpleNamespace(version='synthetic'),on=Mock())
            launch=AsyncMock(return_value=context);host._pw=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
            with patch.object(config,'BROWSER_DIR',Path(td)),patch.object(H,'stop_pids',return_value=0),patch.object(H,'font_environment',return_value={'FONTCONFIG_FILE':'synthetic','PULSE_SERVER':'ambient'}):
                instance=await host._launch('p-12345678')
            kwargs=launch.call_args.kwargs
            self.assertTrue(instance.audio.applied);self.assertNotIn('--mute-audio',kwargs['args'])
            self.assertEqual(kwargs['env'],{'FONTCONFIG_FILE':'synthetic','PULSE_SERVER':A.PULSE_SERVER})
            self.assertTrue(kwargs['chromium_sandbox']);self.assertTrue(kwargs['headless'])

if __name__=='__main__':unittest.main()
"""Rules-only cleanup over the authenticated link and owner-only HUD routes."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('voice_link_existing', Path(__file__).with_name('voice_link_check.py'))
existing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(existing)
from jarvis import stt_settings
from jarvis.v2 import voice_link
from windows import voicewire as wire


class Cleanup(existing.Base):
    def owner(self, method, path, body=None, *, port=None, origin=True, status=200):
        port = self.d.face_port if port is None else port
        headers = [('Host', f'127.0.0.1:{port}')]
        if origin:
            headers.append(('Origin', f'http://127.0.0.1:{self.d.face_port}'))
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers.append(('Content-Type', 'application/json'))
        code, _, data = existing.rac.raw(port, method, path, headers, payload)
        self.assertEqual(code, status, data)
        return json.loads(data)

    def test_rules_off_categories_counts_and_unknown_scratch(self):
        self.start()
        c = self.client()
        text = 'um hello there friend'
        self.assertEqual(c.clean(text, 'other'), {'text': 'Hello there friend.', 'pass': 'rules'})
        self.assertEqual(c.clean(text, 'terminal'), {'text': 'hello there friend', 'pass': 'rules'})
        for odd in ['unknown', None, [], {}]:
            self.assertEqual(c.clean(text, odd), {'text': 'hello there friend', 'pass': 'rules'})
        self.assertEqual(self.owner('GET', '/stt/settings')['dictation_cleanup'], 'rules')
        shown = self.owner('POST', '/stt/settings', {'dictation_cleanup': 'off'})
        self.assertEqual(shown['dictation_cleanup'], 'off')
        self.assertEqual(c.clean(text, 'other'), {'text': text, 'pass': 'off'})
        self.assertEqual(json.loads(self.settings.read_text())['dictation_cleanup'], 'off')
        self.owner('POST', '/stt/settings', {'dictation_cleanup': 'rules'})
        c.scratch('rules')
        c.scratch('off')
        c.scratch('unknown')
        counts = self.owner('GET', '/stt/cleanup')
        self.assertEqual(counts, {'rules': {'typed': 6, 'scratched': 1},
                                  'off': {'typed': 1, 'scratched': 1},
                                  'raw': {'typed': 0, 'scratched': 0},
                                  'model': {'typed': 0, 'scratched': 0}})
        counts['rules']['typed'] = 99
        self.assertEqual(self.link.cleanup_counts()['rules']['typed'], 6)
        self.assertEqual(voice_link.VoiceLink(self.d, port=0).cleanup_counts()['rules']['typed'], 0)

    def test_guard_raw_fallback_counted(self):
        self.start()
        with patch.object(voice_link.dictation_rules, 'clean', return_value='invented words'):
            self.assertEqual(self.client().clean('keep my words', 'other'),
                             {'text': 'keep my words', 'pass': 'raw'})
        self.assertEqual(self.link.cleanup_counts()['raw']['typed'], 1)

    def test_caps_identity_error_shape_and_no_text_in_errors(self):
        self.start()
        c = self.client()
        for body in [{'op': 'clean', 'text': 'x' * 2001}, {'op': 'clean', 'text': 3},
                     {'op': 'scratch', 'pass': []}]:
            with self.assertRaises(wire.LinkError) as got:
                c._request(body)
            self.assertEqual(str(got.exception), voice_link.BAD_REQUEST)
        wire.send_json(c._stream, {'op': 'clean', 'text': 'private words', 'id': 'bad'})
        self.assertEqual(wire.read_json(c._stream), {'id': None, 'ok': False, 'error': voice_link.BAD_REQUEST})
        with patch.object(voice_link.dictation_rules, 'clean', side_effect=OSError('private words')):
            with self.assertRaises(wire.LinkError) as got:
                c.clean('private words', 'other')
            self.assertNotIn('private words', str(got.exception))
        self.assertTrue(all(row['typed'] == 0 for row in self.link.cleanup_counts().values()))

    def test_settings_and_diagnostics_owner_only_and_read_only(self):
        self.start()
        for path in ['/stt/settings', '/stt/cleanup']:
            self.owner('GET', path, port=self.d.port, status=403)
            self.owner('GET', path, origin=False, status=403)
        self.owner('POST', '/stt/cleanup', {}, status=405)
        self.owner('GET', '/stt/cleanup?extra=1', status=400)
        for value in ['model', None, 3, {}, []]:
            self.owner('POST', '/stt/settings', {'dictation_cleanup': value}, status=400)
        self.assertFalse(self.settings.exists())

    def test_cleanup_settings_preserve_existing_switches_and_fail_closed(self):
        self.assertEqual(stt_settings.load_cleanup(), 'rules')
        stt_settings.update(backend='local', cloud_fallback=False)
        stt_settings.update(dictation_cleanup='off')
        self.assertEqual(stt_settings.load()['backend'], 'local')
        self.assertFalse(stt_settings.load()['cloud_fallback'])
        self.settings.write_text('{broken')
        self.assertEqual(stt_settings.load_cleanup(), 'off')
        stt_settings.update(backend='local')
        self.assertEqual(stt_settings.load_cleanup(), 'off')
        self.assertFalse(stt_settings.load()['cloud_fallback'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

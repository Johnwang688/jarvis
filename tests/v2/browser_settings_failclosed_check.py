"""Shared Browser settings safety defaults, malformed repair and preservation."""
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from jarvis.v2.browser import settings as S

class Closed(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"browser.json"
    def test_missing_file_and_key_allow_master_without_creating_state(self):
        self.assertTrue(S.BrowserSettings(self.path).value("sound"));self.assertFalse(self.path.exists())
        self.path.write_text('{"future":{"enabled":true}}')
        state=S.BrowserSettings(self.path);self.assertTrue(state.value("sound"))
        state.update(lan=True)
        self.assertEqual(json.loads(self.path.read_text()),{"future":{"enabled":True},"lan":True})
    def test_first_attach_stat_permission_error_fails_closed(self):
        with patch.object(S.os,"stat",side_effect=PermissionError):
            self.assertFalse(S.BrowserSettings(self.path).value("sound"))
    def test_corrupt_file_repair_never_enables_omitted_sound(self):
        for raw in ("bad","[]","null"):
            with self.subTest(raw=raw):
                self.path.write_text(raw);state=S.BrowserSettings(self.path)
                self.assertFalse(state.value("sound"));self.assertFalse(state.update(auto_open=False)["sound"])
                self.assertIs(json.loads(self.path.read_text())["sound"],False)
                self.assertTrue(state.update(sound=True)["sound"])
    def test_bad_type_symlink_and_oversize_fail_closed(self):
        self.path.write_text('{"sound":1}')
        self.assertFalse(S.BrowserSettings(self.path).value("sound"))
        self.path.unlink();target=self.path.with_name("target");target.write_text("{}");self.path.symlink_to(target)
        self.assertFalse(S.BrowserSettings(self.path).value("sound"))
        self.path.unlink();self.path.write_text(" "* (S.FILE_CAP+1))
        self.assertFalse(S.BrowserSettings(self.path).value("sound"))
    def test_permission_failure_after_on_clears_master(self):
        self.path.write_text('{"sound":true}');state=S.BrowserSettings(self.path)
        self.assertTrue(state.value("sound"))
        with patch.object(S.os,"stat",side_effect=PermissionError):
            self.assertFalse(state.snapshot()["sound"])

if __name__=="__main__":unittest.main()

"""Owner switch fail-closed cases, isolated from real settings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jarvis.v2.browser import settings

class Settings(unittest.TestCase):
    def test_missing_valid_and_malformed_repair(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"browser.json";s=settings.BrowserSettings(p)
            self.assertTrue(s.value("agents_may_drive"))
            for raw in ('{}','{"other":42}','{"agents_may_drive":true}'):
                p.write_text(raw);self.assertTrue(s.snapshot()["agents_may_drive"])
            for raw in ('garbage','[]','null','{"agents_may_drive":1}','{"agents_may_drive":"true"}'):
                p.write_text(raw);self.assertFalse(s.snapshot()["agents_may_drive"])
            p.write_text('garbage');s.update(auto_open=False)
            self.assertFalse(s.value("agents_may_drive"))
            self.assertFalse(json.loads(p.read_text())["agents_may_drive"])
            s.update(agents_may_drive=True);self.assertTrue(s.value("agents_may_drive"))
    def test_unreadable_stat_on_first_attach_and_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"browser.json"
            with patch.object(settings.os,"stat",side_effect=PermissionError):
                s=settings.BrowserSettings(p);self.assertFalse(s.value("agents_may_drive"))
                self.assertFalse(s.snapshot()["agents_may_drive"])
            self.assertTrue(s.snapshot()["agents_may_drive"])
            with patch.object(settings.os,"stat",side_effect=PermissionError):
                self.assertFalse(s.snapshot()["agents_may_drive"])
    def test_symlink_directory_oversized_and_open_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"browser.json";target=Path(d)/"target";target.write_text('{}')
            p.symlink_to(target);self.assertFalse(settings.BrowserSettings(p).value("agents_may_drive"))
            p.unlink();p.mkdir();self.assertFalse(settings.BrowserSettings(p).value("agents_may_drive"))
            p.rmdir();p.write_text(' '*(settings.FILE_CAP+1));self.assertFalse(settings.BrowserSettings(p).value("agents_may_drive"))
            p.write_text('{}')
            with patch.object(settings.os,"open",side_effect=PermissionError):
                self.assertFalse(settings.BrowserSettings(p).value("agents_may_drive"))

if __name__=="__main__":unittest.main()

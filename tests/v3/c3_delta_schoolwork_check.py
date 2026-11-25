import unittest
from unittest.mock import Mock, patch

from c3_acceptance_check import Base, FakeSw
from jarvis.v3.ingest.schoolwork import SchoolworkIngester


class SchoolworkDelta(Base):
    @patch("jarvis.tools.secrets.secret_values", return_value=[])
    def test_session_cookie_in_cli_fields_is_withheld(self, _secrets):
        value = "fake-session-cookie-48e6b2d1"
        header = f"Cookie: sessionid={value}"
        cli = FakeSw()
        cli.review["undated"][0].update(title=f"Example worksheet {header}", description=header)
        post = Mock()
        runner = self.runner(SchoolworkIngester(cli, post=post, signals_dir=self.sig))

        self.assertIsNone(runner.tick()["schoolwork"]["error"])
        post.assert_not_called()
        snapshot = self.items("schoolwork")
        self.assertIn("4", snapshot)
        self.assertTrue(self.index.discover("worksheet"))
        index_bytes = b"".join(path.read_bytes() for path in (self.tmp / "v3").glob("index.sqlite*"))
        for name, contents in (("snapshot", repr(snapshot).encode()), ("index", index_bytes)):
            with self.subTest(surface=name):
                self.assertFalse(value.encode() in contents, "The fake session cookie was persisted")


if __name__ == "__main__":
    unittest.main()

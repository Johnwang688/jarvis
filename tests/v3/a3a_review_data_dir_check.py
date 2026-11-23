"""(A3a verifier r1, orchestrator-owned; the fixer must not edit.) An existing symlink must never change another directory's permissions."""

from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis.v3.daemon import Daemon


class DataDirCheck(unittest.TestCase):
    def test_start_preserves_permissions_on_symlink_target(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            shared = root / "shared"
            shared.mkdir()
            shared.chmod(0o750)
            data = root / "v3"
            data.symlink_to(shared, target_is_directory=True)
            daemon = Daemon(port=0, data_dir=data)

            with patch("jarvis.v3.daemon.ThreadingHTTPServer"), patch(
                "jarvis.v3.daemon.threading.Thread"
            ):
                try:
                    daemon.start()
                except (OSError, RuntimeError, ValueError):
                    pass
                finally:
                    daemon.stop()

            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o750)


if __name__ == "__main__":
    unittest.main()

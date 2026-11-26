from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.tools import files
from jarvis.v3.voice import gpu_voice
from jarvis.v3.voice.engines import EngineUnavailable


class LaunchTrustCheck(unittest.TestCase):
    def test_worker_script_is_protected_from_agent_file_writes(self):
        self.assertTrue(files._self_protected(gpu_voice._WORKER))

    def test_world_writable_ancestor_invalidates_python(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        provisioning_root = root / "voice-gpu"
        python = provisioning_root / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.touch(mode=0o700)
        (python.parent.parent / gpu_voice._INSTALLED).touch()
        provisioning_root.chmod(0o777)
        with patch.object(config, "V3_VOICE_GPU_PYTHON", python):
            with self.assertRaises(EngineUnavailable):
                gpu_voice.KokoroWorkerVoice(data_dir=root)

    def test_writable_symlink_target_parent_invalidates_python(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        python = root / "owner" / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        (python.parent.parent / gpu_voice._INSTALLED).touch()
        other = root / "shared"
        other.mkdir()
        target = other / "python"
        target.touch(mode=0o700)
        python.symlink_to(target)
        other.chmod(0o777)
        with self.assertRaises(EngineUnavailable):
            gpu_voice.worker_python(python)


if __name__ == "__main__":
    unittest.main()

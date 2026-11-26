from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.v3.voice import gpu_voice, installation
from jarvis.v3.voice.engines import EngineUnavailable


class SetupIntegrityCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.python = self.root / "voice-gpu" / "venv" / "bin" / "python"
        self.enterContext(patch.object(config, "V3_DIR", self.root))

    def create_interpreter_only(self):
        self.python.parent.mkdir(mode=0o700, parents=True)
        self.python.touch(mode=0o700)

    def test_interpreter_without_completed_install_is_unavailable(self):
        self.create_interpreter_only()
        with self.assertRaises(EngineUnavailable):
            gpu_voice.worker_python(self.python)

    def test_phonemizer_dependency_resolution_requires_a_pin(self):
        # phonemizer-fork 3.3.2 requires dlinfo without a version constraint.
        def run(argv, **_kwargs):
            if argv[1:2] == ["venv"]:
                self.create_interpreter_only()

        with patch.object(installation.shutil, "which", side_effect=lambda name: f"/fake/{name}"), \
                patch.object(installation.subprocess, "run", side_effect=run) as invoked, \
                patch("builtins.print"):
            installation._setup_gpu()
        installs = [call.args[0] for call in invoked.call_args_list if "install" in call.args[0]]
        self.assertTrue(installs)
        argv = next(argv for argv in installs if "phonemizer-fork==3.3.2" in argv)
        self.assertTrue(
            "--require-hashes" in argv
            or any(arg.startswith("dlinfo==") for command in installs for arg in command),
            "The installer resolves dlinfo without an exact pin.",
        )


if __name__ == "__main__":
    unittest.main()

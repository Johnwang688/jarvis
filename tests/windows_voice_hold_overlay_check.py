"""Right Ctrl during a pointer click, using real Windows Tk on a private desktop.

Run with the Windows voice venv. No actual hotkeys, foreground changes, audio
devices or audio output. Optional: --old-agent <source.py> verifies the regression.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import threading
import time
import types
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def isolated_desktop():
    user32 = ctypes.windll.user32
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    desktop = user32.CreateDesktopW("JarvisFixture" + uuid.uuid4().hex, None, None, 0, 0x1ff, None)
    if not desktop or not user32.SetThreadDesktop(desktop):
        raise ctypes.WinError()


def main():
    isolated_desktop()
    from windows import voice_agent as agent_module
    baseline = len(sys.argv) == 3 and sys.argv[1] == "--old-agent"
    if baseline:
        old = types.ModuleType("windows.old_voice_agent")
        old.__package__ = "windows"
        old.__file__ = sys.argv[2]
        exec(compile(Path(old.__file__).read_text(encoding="utf-8"), old.__file__, "exec"), old.__dict__)
        agent_module = old
    agent = agent_module.Agent("127.0.0.1", 0, None)
    agent.desktop_key = None  # never register a real desktop shortcut
    agent.online.set()
    agent.player = Mock()
    agent.wake = Mock()
    agent.wake.tap.return_value = True  # virtual source; never open any audio device
    keys = {0xA3, 0x11}
    agent.user32 = types.SimpleNamespace(GetAsyncKeyState=lambda code: 0x8000 if code in keys else 0)
    def pump(duration):
        end = time.monotonic() + duration
        while time.monotonic() < end:
            agent.root.update()
            time.sleep(0.005)
        shown = ctypes.windll.user32.IsWindowVisible(agent.overlay.hwnd)
        return agent.overlay.display.state, bool(shown), agent.recording.is_set()
    checks = []
    with patch.dict(sys.modules, {"sounddevice": types.ModuleType("sounddevice")}):
        worker = threading.Thread(target=agent.poll, daemon=True)
        worker.start()
        try:
            checks.append(("held", pump(0.43)))
            keys.add(0x01)   # transient primary mouse button; Right Ctrl remains physically down
            checks.append(("held+click", pump(0.16)))
            keys.discard(0x01)
            checks.append(("still held", pump(5.25)))
            keys.clear()
            checks.append(("released", pump(0.12)))
        finally:
            agent.done.set()
            worker.join(2)
            agent.root.destroy()
    for step, measured in checks:
        print(step, *measured)
    assert checks[0][1] == ("listening", True, True)
    expected = ("hidden", False, False) if baseline else ("listening", True, True)
    assert checks[1][1] == expected and checks[2][1] == expected
    assert not checks[3][1][2]


if __name__ == "__main__":
    main()

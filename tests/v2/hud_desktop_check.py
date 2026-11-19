"""Selected-chat desktop gates, scoped refs and actual paired transport with synthetic data.
Pass --windows on the owner machine to ping the real Windows bridge on a private ephemeral port.
"""
import contextvars
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config, desktop, runtime, tools
from jarvis.v2 import bridge, session_tools
from jarvis.v2.permissions import denied_file
from jarvis.v2.mcp_catalog import MCP_TOOLS
from jarvis.v2.desk import DeskState, SelectedVoiceMessage
from jarvis.v2.model import Role
from jarvis.v2.provider import Brief, UserMessage
from jarvis.v2.tools import hud_desktop as hud
from windows import desktopwire as dw, voicewire as wire

NAMES = hud.HUD_DESKTOP_TOOLS


def call(name, args=None, *, voice=False, desk=False, depth=0, allow=False, stopped=False):
    approvals = []
    def run():
        runtime.bind(depth=depth, caller={"thread_id": "fixture", "turn_id": "turn"},
                     desk={"present": desk, "hud_desktop": voice}, tool_names=NAMES,
                     should_stop=lambda: stopped)
        def permit(entry, fields):
            approvals.append((entry.name, fields))
            return allow
        return tools.dispatch(name, json.dumps(args or {}), approve=permit)
    return contextvars.Context().run(run).text, approvals


def packet_peer(port, key, seen):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock, sock.makefile("rwb") as stream:
        dw.prove_windows(stream, key)
        while True:
            try:
                req = wire.read_json(stream)
            except (wire.LinkError, OSError):
                return
            seen.append(req)
            wire.send_json(stream, {"id": req["id"], "ok": True, "result": {
                "name": "Google Chrome", "snapshot": "[0badbeef00000001-e1] button"}})


def main():
    assert denied_file(str(Path(config.V2_DATA_DIR).expanduser() / "voice-target.json"))
    assert "jarvis/v2/voice_chat.py" in tools.files.SELF_PROTECTED
    state = DeskState()
    chat = Brief(Role.CHAT, "/fixture")
    actual = SelectedVoiceMessage("question")
    plain = UserMessage("question", origin="owner", via="voice", spoken=True, desk=False)
    for brief, message in ((chat, plain), (Brief(Role.REVIEWER, "/fixture"), actual),
                           (Brief(Role.CHAT, "/fixture", task_id="task"), actual),
                           (chat, SelectedVoiceMessage("question", origin="guest"))):
        assert not state.begin(brief, message, "turn")["hud_desktop"]
    slot = state.begin(chat, actual, "paired-turn")
    assert slot["hud_desktop"] and not slot["present"]
    state.reserve()
    assert not slot["hud_desktop"]
    for provider in ("claude", "codex"):
        listed = {row["name"]: row for row in bridge.listing(provider)}
        assert NAMES <= listed.keys()
        for name in NAMES:
            item = session_tools.offered(provider, name)
            assert item.foreground_only and item.read_only == (name == "hud_desktop_status")
    for enabled in ("0", "1"):
        with patch.dict(os.environ, {"JARVIS_DESKTOP_DRIVE": enabled}):
            for provider in ("claude", "codex"):
                listed = {row["name"] for row in bridge.listing(provider)}
                assert {name for name in listed if name.startswith("hud_desktop_")} == NAMES
                assert bool(listed & session_tools.HUD_WINDOW_TOOLS) == (enabled == "1")
    assert NAMES <= tools.EXPLICIT_ONLY and not NAMES & MCP_TOOLS
    assert all(tools.REGISTRY[n].dangerous for n in NAMES - {"hud_desktop_status"})
    assert call("hud_desktop_status")[0].startswith("Refused:")
    assert call("hud_desktop_status", voice=True, depth=1)[0].startswith("Refused:")
    assert call("hud_desktop_status", desk=True, stopped=True)[0].startswith("Refused:")

    with TemporaryDirectory() as td:
        p = Path(td) / "pair"
        p.write_bytes(b"ab" * 32)
        p.chmod(0o600)
        session = desktop.DesktopSession(0, key_path=p)
        session.start()
        port = session._server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as sock, sock.makefile("rwb") as stream:
            wire.send_json(stream, {"hello": "jarvis-desktop-bridge", "version": "2", "nonce": "0" * 64})
            wire.read_json(stream)
            wire.send_json(stream, {"proof": "0" * 64})
            assert not stream.read(1)
        assert not session.connected
        seen = []
        peer = threading.Thread(target=packet_peer, args=(port, dw.private_key(p), seen), daemon=True)
        peer.start()
        assert session.wait_for_bridge(3)
        previous, hud.SESSION = hud.SESSION, session
        try:
            assert "connected" in call("hud_desktop_status", voice=True)[0]
            assert seen[-1]["op"] == "ping" and seen[-1]["scope"] == "fixture"
            args = {"app": "Chrome"}
            refused, approvals = call("hud_desktop_open", args, desk=True)
            assert "declined" in refused and approvals == [("hud_desktop_open", args)]
            assert seen[-1]["op"] == "ping"
            ok, approvals = call("hud_desktop_open", args, voice=True, allow=True)
            assert "Google Chrome" in ok and approvals == [("hud_desktop_open", args)]
            assert seen[-1]["op"] == "launch_app" and seen[-1]["app"] == "chrome"
            ok, _ = call("hud_desktop_snapshot", {"app": "settings"}, desk=True, allow=True)
            assert "0badbeef" in ok and seen[-1]["op"] == "snapshot"
            assert call("hud_desktop_click", {"app": "settings", "ref": "e1"}, voice=True, allow=True)[0].startswith("Refused:")
            assert call("hud_desktop_key", {"app": "settings", "key": "{Win}r"}, voice=True, allow=True)[0].startswith("Refused:")
            assert call("hud_desktop_key", {"app": "chrome", "key": "enter"}, voice=True, allow=True)[0].startswith("Refused:")
            assert call("hud_desktop_open", {"app": "PowerShell (Administrator)"}, voice=True, allow=True)[0].startswith("Refused:")
            assert seen[-1]["op"] == "snapshot"
        finally:
            hud.SESSION = previous
            session.stop()
            peer.join(3)
    print("PASS: provider catalogs, scopes, permission, paired transport, safe app and ref boundaries.")


def windows_smoke():
    from jarvis.desktop_setup import _win_path
    session = desktop.DesktopSession(0)
    session.start()
    port = session._server.getsockname()[1]
    prefix = Path(subprocess.check_output(["wslpath", "-u", config.DESKTOP_BRIDGE_DIR], text=True).strip())
    proc = subprocess.Popen([str(prefix / "venv/Scripts/python.exe"), _win_path(ROOT / "windows/bridge.py"),
                             "127.0.0.1", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert session.wait_for_bridge(15), "Windows peer did not pair"
        info = session.request("ping", require_pair=True, scope="fixture", timeout=4)
        assert info.get("python")
        print(f"PASS: real non-admin Windows desktop bridge paired; Python {info['python']}, read-only ping.")
    finally:
        session.stop()
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
    if "--windows" in sys.argv:
        windows_smoke()

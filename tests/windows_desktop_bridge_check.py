"""Run with the installed Windows bridge Python. Fake controls; never take foreground."""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "windows"))
import bridge


class FakeBackend:
    name = "uia"
    root = "root"

    def children(self, node):
        return ["x"] if node == "root" else []

    def describe(self, node):
        return {"role": "button", "name": "Fixture", "value": "", "checked": None, "selected": False,
                "enabled": True, "offscreen": False, "interactive": True, "rect": (0, 0, 5, 5)}

    def click(self, node):
        assert node == "x"
        return "test"


def main():
    entry = {"backend": FakeBackend(), "hwnd": 1}
    with patch.object(bridge, "current", return_value=entry), patch.object(bridge, "refresh_backend"):
        def take(scope):
            text = bridge.op_snapshot({"app": "settings", "scope": scope})["snapshot"]
            return text.split("]", 1)[0][1:]
        ref = take("chat-a")
        other = take("chat-b")
        for scope, bad in (("chat-a", ref), ("chat-b", ref), ("chat-a", other)):
            try:
                bridge.op_click({"app": "settings", "scope": scope, "ref": bad, "settle": 0})
            except RuntimeError:
                pass
            else:
                raise AssertionError("A stale or another chat's ref was accepted.")
        assert bridge.op_click({"app": "settings", "scope": "chat-b", "ref": other, "settle": 0})["via"] == "test"
        try:
            bridge.op_type({"app": "settings", "scope": "chat-b", "ref": other, "text": "x"})
        except RuntimeError as exc:
            assert "text field" in str(exc)
        else:
            raise AssertionError("A button was typed into.")
    with patch.object(bridge, "attach", side_effect=RuntimeError) as called:
        try:
            bridge.op_open({"spec": {"name": "settings", "launch": {"kind": "exe", "target": "bogus"}}})
        except RuntimeError:
            pass
        assert called.call_args.args[0]["launch"] == {"kind": "uri", "target": "ms-settings:"}
    user = Mock()
    user.IsIconic.return_value = False
    user.GetForegroundWindow.return_value = 999  # owner switched after the raise
    with patch.object(bridge, "user32", user), patch.object(bridge, "_force_foreground", return_value=True), patch.object(bridge.time, "sleep"):
        try:
            bridge.activate(100, maximize=False, require_foreground=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Foreground lost while settling.")
    user.WindowFromPoint.return_value = 700
    user.GetAncestor.return_value = 999  # unrelated overlay even when target is foreground
    with patch.object(bridge, "user32", user), patch.object(bridge, "_require_foreground"), patch.object(bridge.auto, "Click") as click:
        try:
            bridge._click_control(100, 101, (0, 0, 50, 50))
        except RuntimeError:
            pass
        else:
            raise AssertionError("Covering window received a click.")
        click.assert_not_called()
        user.GetAncestor.return_value = 101
        bridge._click_control(100, 101, (0, 0, 50, 50))
        click.assert_called_once_with(25, 25, waitTime=0)
    field_backend = Mock(hwnd=100)
    user.GetForegroundWindow.return_value = 100
    field_backend.focused.return_value = False
    with patch.object(bridge, "user32", user), patch.object(bridge, "_require_foreground"), patch.object(bridge.time, "sleep"):
        try:
            bridge._focus_field(field_backend, "field")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Keystrokes permitted after focus moved to a different field.")
        field_backend.focused.return_value = True
        bridge._focus_field(field_backend, "field")
    fake = Mock()
    fake.apps = [{"name": "Google Chrome Beta", "id": "wrong"}, {"name": "Google Chrome", "id": "expected"},
                 {"name": "Discord", "id": "discord"}, {"name": "Paint", "id": "one"}, {"name": "Paint", "id": "two"}]
    with patch.object(bridge, "_app_actions", fake):
        assert bridge.op_launch_app({"app": "chrome"})["name"] == "Google Chrome"
        assert fake.launcher.call_args.args[0] == ["explorer.exe", "shell:AppsFolder\\expected"]
        assert bridge.op_launch_app({"app": "Discord"})["name"] == "Discord"
        for bad in ("Paint", "Googl", "C:/Windows/System32/cmd.exe", "PowerShell (Administrator)", "Discord --argument"):
            try:
                bridge.op_launch_app({"app": bad})
            except ValueError:
                pass
            else:
                raise AssertionError(f"Invalid or ambiguous launch: {bad}")
        assert fake.launcher.call_count == 2
    print("PASS: Windows scoped fresh refs, pinned specs, text field check and exact launch.")


if __name__ == "__main__":
    main()

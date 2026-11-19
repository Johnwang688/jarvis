"""Drive the production Tk overlay on a fresh non-input Windows desktop."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock


def check():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from windows import voiceflow, voiceui

    u = ctypes.windll.user32
    u.GetThreadDesktop.argtypes = [w.DWORD]
    u.GetThreadDesktop.restype = w.HANDLE
    u.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    u.CreateDesktopW.restype = w.HANDLE
    u.SetThreadDesktop.argtypes = [w.HANDLE]
    u.CloseDesktop.argtypes = [w.HANDLE]
    old = u.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
    desk = u.CreateDesktopW(f"JarvisReviewOverlay{os.getpid()}", None, None, 0, 0x10000000, None)
    assert desk
    try:
        assert u.SetThreadDesktop(desk)
        import tkinter as tk
        with TemporaryDirectory(prefix="jarvis-overlay-review-") as home:
            os.environ["HOME"] = home
            root = tk.Tk(className="JarvisReviewOverlay")
            root.withdraw()
            try:
                root.tk.call("tk", "scaling", 2.0)  # 144 pixels per inch
                overlay = voiceui.Overlay(root)
                client, typer, actions, player = (Mock() for _ in range(4))
                client.session = "paired"
                flow = voiceflow.Flow(client, typer, actions, overlay, player)
                cases = [("Personal", "Work")] + [tuple(f"Client Workspace {idx:02d}"
                         for idx in range(1, count + 1)) for count in (2, 12, 20)]
                cases.append(tuple(f"Client Workspace {idx:02d} " + "W" * 60 for idx in range(24)))
                for labels in cases:
                    actions.chrome_profile_options.return_value = labels
                    flow._offer_profile_choice(None, client.session, "hotkey", lambda: True)
                    overlay._tick()
                    root.update_idletasks()
                    box = overlay.canvas.bbox(overlay.label)
                    canvas_height = overlay.canvas.winfo_height()
                    assert box[3] <= canvas_height, (len(labels), box, canvas_height)
                    if overlay.profile_list is not None and overlay.profile_list.winfo_ismapped():
                        content = overlay.profile_text
                        assert content.itemcget(overlay.profile_names, "text") == ", ".join(labels)
                        assert "profile name or cancel" in overlay.canvas.itemcget(overlay.label, "text")
                        assert content.yview()[1] < 1
                        content.yview_moveto(1)
                        root.update_idletasks()
                        assert content.yview()[1] == 1, content.yview()
                    else:
                        assert overlay.canvas.itemcget(overlay.label, "text") == overlay.display.text
                    print(f"{len(labels)} names: text ends at {box[3]}, canvas height {canvas_height}")
                overlay.set_state("done")
                overlay._tick()
                root.update_idletasks()
                if overlay.profile_list is not None:
                    assert not overlay.profile_list.winfo_ismapped()
            finally:
                root.destroy()
    finally:
        u.SetThreadDesktop(old)
        u.CloseDesktop(desk)


if __name__ == "__main__":
    if sys.argv[1:] == ["--run"] and os.name == "nt":
        check()
    else:
        print("Use Windows Python with --run to check the hidden-desktop overlay.")

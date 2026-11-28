"""Owner-run check that the Windows bridge can click and scroll a real window.

This one ACTS: it opens Notepad on a throwaway text file, opens and closes one
of Notepad's menus, clicks in its text area and turns the mouse wheel there.
It touches no other window, presses no key and types nothing. Run it yourself
from WSL, at the desk, and keep your hands off the mouse and keyboard while it
runs (about 30 seconds):

    PYTHONPATH=$PWD .venv/bin/python tests/v3/window_act_smoke.py

It starts its own Windows bridge on a private port, so it needs no daemon and
does not use the daemon's bridge port. It prints program names, roles, counts
and sizes, never window titles, snapshot text or pixels. Each step prints PASS
or the bridge's own refusal and the run goes on, so one failure does not hide
the others. Notepad is left open; close it yourself.
"""
from base64 import b64decode
from io import BytesIO
from pathlib import Path
from random import Random
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config, desktop  # noqa: E402

NOTEPAD = "/mnt/c/Windows/System32/notepad.exe"
MENUS = ("view", "edit", "file")
# A blinking caret or a hovered button changes well under this share of the
# pixels; an open menu or a scrolled page of text changes far more.
CHANGED = 0.002
LINES = 400
CHECK_FILE = "scroll-check"
HANDS_OFF = 4
SETTLE = 0.6
WORDS = ("scroll", "check", "window", "bridge", "wheel", "notch", "page", "line", "text")


class Report:
    def __init__(self, out=print):
        self.out, self.failed = out, []

    def step(self, name, call):
        try:
            result = call()
        except Exception as exc:  # the point is to see every failure
            self.failed.append(name)
            self.out(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            return None
        self.out(f"PASS  {name}")
        return result

    def verdict(self, name, ok, detail):
        if not ok:
            self.failed.append(name)
        self.out(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def _pixels(shot, *, text_area):
    import numpy
    from PIL import Image

    image = Image.open(BytesIO(b64decode(shot["image_b64"]))).convert("L")
    if text_area:
        # The title, status bar and a scrollbar that appears while scrolling sit at the edges.
        width, height = image.size
        image = image.crop((width // 20, height // 5, width * 4 // 5, height * 9 // 10))
    return numpy.asarray(image, dtype=numpy.int16)


def changed_fraction(before, after, *, text_area=False):
    """Share of pixels that differ between two screenshot replies."""
    first, second = (_pixels(shot, text_area=text_area) for shot in (before, after))
    if first.shape != second.shape:
        return 1.0
    return float((first != second).mean())


def content_shift(before, after):
    """How far the text moved between two screenshots, in pixels.

    Positive: the text moved up, so the page scrolled down. Negative: the
    reverse. 0: nothing moved. None: the picture changed in some other way.
    """
    import numpy

    if changed_fraction(before, after, text_area=True) <= CHANGED:
        return 0
    first, second = (_pixels(shot, text_area=True)[:, ::4] for shot in (before, after))
    if first.shape != second.shape:
        return None  # the window changed size between the two pictures
    height = first.shape[0]
    still = numpy.abs(second - first).mean()
    best, shift = still, 0
    for move in range(1, height * 6 // 10):
        for signed, score in (
                (move, numpy.abs(second[:height - move] - first[move:]).mean()),
                (-move, numpy.abs(second[move:] - first[:height - move]).mean())):
            if score < best:
                best, shift = score, signed
    return shift if shift and best < still / 2 else None


def pick_window(before, after, app="notepad.exe", showing=CHECK_FILE):
    """The window of `app` whose title names the check file, a new window first.

    Notepad may open the file as a tab in a window that was already open, so an
    earlier window counts too, but only while it shows this file. A Notepad
    window showing anything else is never picked.
    """
    known = {row["window"] for row in before}
    ours = [row for row in after
            if (row.get("app") or "").casefold() == app and row.get("access") == "drivable"
            and showing in (row.get("title") or "").casefold()]
    return next((row for row in ours if row["window"] not in known), ours[0] if ours else None)


def _describe(shift):
    if shift is None:
        return "the picture changed but not as a clean scroll"
    if shift == 0:
        return "nothing moved"
    return f"the page scrolled {'down' if shift > 0 else 'up'} by {abs(shift)} px"


def drive(ask, window, report):
    """Click and scroll inside one window. `ask(op, **fields)` reaches the bridge."""
    out = report.out

    def shot(name):
        time.sleep(SETTLE)  # let a menu or a smooth scroll finish
        return report.step(f"window_screenshot ({name})",
                           lambda: ask("window_screenshot", window=window))

    snap = report.step("window_snapshot", lambda: ask("window_snapshot", window=window))
    refs = (snap or {}).get("refs") or {}
    out(f"      {len(refs)} refs, roles: {sorted({info['role'] for info in refs.values()})}")
    start = shot("start")
    if start is None:
        return
    x, y = start["size"][0] // 2, start["size"][1] // 2
    out(f"      image size {start['size']}; using its centre as the point")

    # A click by ref: open one of Notepad's menus and see it appear.
    by_name = {info["name"].casefold(): (ref, info) for ref, info in refs.items()
               if info["role"] == "menuitem"}
    menu = next((by_name[name] for name in MENUS if name in by_name), None)
    opened = None
    if menu is None:
        out("SKIP  no File, Edit or View menu found; the click by ref is skipped")
    elif report.step("window_click by ref (a menu)", lambda: ask(
            "window_click", window=window, ref=menu[0], expect=menu[1])) is not None:
        opened = shot("menu open")
        if opened is not None:
            share = changed_fraction(start, opened)
            report.verdict("the click by ref opened the menu", share > CHANGED,
                           f"{share:.2%} of the window changed")
            if share <= CHANGED:
                opened = None

    # A click by point: the text area. It also closes the menu.
    under = report.step("window_probe by point", lambda: ask("window_probe", window=window, x=x, y=y))
    if under is None:
        return
    expect = {"name": under["name"], "role": under["role"]}
    out(f"      control under the point: role {under['role']}")
    clicked = report.step("window_click by point (text area)", lambda: ask(
        "window_click", window=window, x=x, y=y, expect=expect))
    before = shot("before scrolling")
    if clicked is not None and opened is not None and before is not None:
        share = changed_fraction(opened, before)
        report.verdict("the click by point closed the menu", share > CHANGED,
                       f"{share:.2%} of the window changed")
    report.step("window_probe focus (Notepad should be in front)",
                lambda: ask("window_probe", window=window, focus=True))

    # The wheel, both ways. Measured by how far the text moved, so it does not
    # matter where in the file Notepad opened.
    moves = []
    for amount in (-3, 3):
        if before is None or report.step(f"window_scroll by point (amount {amount:+d})", lambda: ask(
                "window_scroll", window=window, amount=amount, x=x, y=y, expect=expect)) is None:
            break
        after = shot(f"after amount {amount:+d}")
        if after is None:
            break
        shift = content_shift(before, after)
        out(f"      amount {amount:+d}: {_describe(shift)}")
        moves.append((amount, shift))
        before = after
    if not moves:
        return
    measured = [(amount, shift) for amount, shift in moves if shift]
    report.verdict("the wheel moved the page", bool(measured),
                   "; ".join(f"amount {amount:+d}: {_describe(shift)}" for amount, shift in moves))
    if measured:
        standard = [(amount < 0) == (shift > 0) for amount, shift in measured]
        report.verdict(
            "a negative amount scrolls down and a positive one up", all(standard),
            "as the tool describes" if all(standard) else
            "REVERSED: a negative amount scrolled up" if not any(standard) else "mixed results")
    if moves[0][1]:
        # The first turn moved the page, so the opposite turn has room to move it back.
        back = moves[1][1] if len(moves) == 2 else None
        report.verdict("the opposite turn moved the page back",
                       bool(back) and (back > 0) != (moves[0][1] > 0),
                       _describe(back) if len(moves) == 2 else "the second turn did not run")


def _bridge_dir():
    return Path(subprocess.check_output(
        ["wslpath", "-u", config.DESKTOP_BRIDGE_DIR], text=True).strip())


def _scroll_file(prefix):
    folder = prefix / "smoke"
    folder.mkdir(exist_ok=True)
    path = folder / f"{CHECK_FILE}.txt"
    # Lines of uneven length, so a scrolled page lines up with the earlier one at one offset only.
    rng = Random(7)
    path.write_text("".join(
        f"line {n:04d}  {' '.join(rng.choice(WORDS) for _ in range(rng.randint(1, 14)))}\r\n"
        for n in range(1, LINES + 1)))
    return path


def main():
    from jarvis.desktop_setup import _win_path

    prefix = _bridge_dir()
    session = desktop.DesktopSession(0)
    session.start()
    port = session._server.getsockname()[1]
    log_path = Path(tempfile.gettempdir()) / "window_act_smoke_bridge.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [str(prefix / "venv/Scripts/python.exe"), _win_path(str(ROOT / "windows/bridge.py")),
         "127.0.0.1", str(port)], stdout=log, stderr=subprocess.STDOUT)
    report = Report()

    def ask(op, **fields):
        return session.request(op, require_pair=True, scope="act-smoke", timeout=30,
                               hud_sensitive=None, **fields)

    try:
        if not session.wait_for_bridge(20):
            print(f"FAIL  the Windows bridge did not connect and pair. Its output: {log_path}")
            return 1
        before = report.step("windows (before Notepad)", lambda: ask("windows"))
        if before is None:
            return 1
        before = before["windows"]
        programs = sorted({(row.get("app") or "?").casefold() for row in before})
        print(f"      {len(before)} windows listed; programs: {', '.join(programs)}")
        subprocess.Popen([NOTEPAD, _win_path(str(_scroll_file(prefix)))],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target, deadline = None, time.monotonic() + 15
        while target is None and time.monotonic() < deadline:
            time.sleep(0.5)
            listed = report.step("windows (looking for Notepad)", lambda: ask("windows"))
            if listed is None:
                return 1
            target = pick_window(before, listed["windows"])
        if target is None:
            print("FAIL  no drivable Notepad window showing the check file appeared within 15 seconds")
            return 1
        print(f"PASS  Notepad window found. Hands off the mouse and keyboard; starting in {HANDS_OFF} s.")
        time.sleep(HANDS_OFF)
        drive(ask, target["window"], report)
    finally:
        session.stop()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        print(f"Bridge output saved to {log_path}")
    if report.failed:
        print(f"\n{len(report.failed)} step(s) failed:")
        for name in report.failed:
            print(f"  - {name}")
        return 1
    print("\nAll steps passed: the bridge clicked and scrolled a real window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

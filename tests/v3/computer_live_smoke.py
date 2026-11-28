"""Owner-run check of `jarvis computer` on a real Windows desktop, without the daemon.

This one ACTS, through the same code the daemon runs (jarvis/v3/computer.py):
it opens Notepad on a throwaway text file, takes pictures of the front window,
clicks in Notepad, turns the wheel there, opens one of its menus, then opens
Character Map and brings Notepad back to the front. It clicks nothing outside
that Notepad window, presses no key and types nothing. Run it yourself from
WSL, at the desk, hands off the mouse and keyboard (about a minute):

    PYTHONPATH=$PWD .venv/bin/python tests/v3/computer_live_smoke.py

It starts its own Windows bridge on a private port and builds its own driving
permission in this process: the installed daemon is not contacted, restarted or
changed. It prints program names, roles, counts and sizes, never window titles
or pixels. Notepad and Character Map are left open; close them yourself.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

import window_act_smoke as base  # noqa: E402
from jarvis import desktop  # noqa: E402
from jarvis.v3.computer import ComputerDrive, ComputerError  # noqa: E402

CHARMAP = "/mnt/c/Windows/System32/charmap.exe"
RAW_SCOPE = "computer-live-smoke"


def is_check_window(reply):
    """Whether a screenshot or window row is the Notepad showing the check file."""
    return ((reply.get("app") or "").casefold() == "notepad.exe"
            and base.CHECK_FILE in (reply.get("title") or "").casefold())


def changed_box(before, after):
    """The rectangle (left, top, right, bottom) holding every pixel that differs, or None."""
    import numpy

    first, second = (base._pixels(shot, text_area=False) for shot in (before, after))
    if first.shape != second.shape:
        return None
    rows, columns = numpy.nonzero(first != second)
    if not len(rows):
        return None
    return int(columns.min()), int(rows.min()), int(columns.max()) + 1, int(rows.max()) + 1


def find_menu(probe, size):
    """Scan the top of the window for a File, Edit or View menu button. Returns (x, y) or None."""
    for y in range(20, min(size[1], 130), 12):
        for x in range(10, min(size[0], 420), 14):
            try:
                under = probe(x, y)
            except desktop.DesktopError:
                continue
            if under.get("role") == "menuitem" and (under.get("name") or "").casefold() in base.MENUS:
                return x, y
    return None


class WrongWindow(Exception):
    """The front window is not the check's Notepad, so the run must not act any further."""


def check(drive, code, ask, report, open_program, records):
    """The whole run. `ask(op, **fields)` reaches the bridge directly, for read-only looks."""
    try:
        _check(drive, code, ask, report, open_program, records)
    except WrongWindow:
        report.out("      Stopping here: nothing is clicked in any other window.")
    finally:
        drive.stop("check finished")


def _check(drive, code, ask, report, open_program, records):
    out = report.out

    def look(name):
        """A new screenshot. Every click and wheel turn after it lands in the window it shows,
        so anything but the check's Notepad ends the run before the next action."""
        time.sleep(base.SETTLE)
        shot = report.step(f"screenshot ({name})", lambda: drive.screenshot(code))
        if shot is not None and not is_check_window(shot):
            report.verdict(f"the front window is still the check's Notepad ({name})", False,
                           f"program {shot['app'] or '?'}")
            raise WrongWindow
        return shot

    def numbered():
        rows = report.step("windows", lambda: drive.windows(code))
        if rows is None:
            return None, None
        return rows, next((row for row in rows if is_check_window(row)), None)

    # 1. Notepad comes up and is the front window.
    open_program("notepad")
    row, deadline = None, time.monotonic() + 15
    while row is None and time.monotonic() < deadline:
        time.sleep(0.5)
        rows, row = numbered()
        if rows is None:
            return
    if row is None:
        report.verdict("Notepad showing the check file is listed", False, "it did not appear within 15 seconds")
        return
    out(f"      {len(rows)} windows listed; Notepad is number {row['n']}, "
        f"{'in front' if row['front'] else 'not in front'}")
    out(f"      Hands off the mouse and keyboard; starting in {base.HANDS_OFF} s.")
    time.sleep(base.HANDS_OFF)
    if not row["front"] and report.step("front (Notepad was not in front)",
                                        lambda: drive.front(code, row["n"])) is None:
        return
    start = look("start")
    if start is None:
        return
    out(f"      the front window is the check's Notepad, size {start['size']}")
    x, y = start["size"][0] // 2, start["size"][1] // 2

    # 2. Click and wheel, through the driving code.
    clicked = report.step("click (text area)", lambda: drive.click(code, x, y))
    if clicked is not None:
        out(f"      clicked a control of role {clicked['control']['role']}")
    before = look("before scrolling")
    moves = []
    for amount in (-3, 3):
        if before is None or report.step(f"scroll {amount:+d}", lambda: drive.scroll(code, x, y, amount)) is None:
            break
        after = look(f"after scroll {amount:+d}")
        if after is None:
            break
        moves.append((amount, base.content_shift(before, after)))
        out(f"      amount {amount:+d}: {base._describe(moves[-1][1])}")
        before = after
    measured = [(amount, shift) for amount, shift in moves if shift]
    report.verdict("the wheel moved the page the way the command says",
                   bool(measured) and all((amount < 0) == (shift > 0) for amount, shift in measured),
                   "; ".join(f"{amount:+d}: {base._describe(shift)}" for amount, shift in moves) or "no turn ran")

    # 3. A menu: open it by point, and see whether its items can be reached by point.
    listed = report.step("windows (direct, for the menu search)", lambda: ask("windows"))
    target = next((item for item in (listed or {}).get("windows", []) if is_check_window(item)), None)
    if target is not None and report.step("screenshot (direct)", lambda: ask(
            "window_screenshot", window=target["window"])) is not None:
        spot = find_menu(lambda px, py: ask("window_probe", window=target["window"], x=px, y=py), start["size"])
        closed = look("menu closed")
        if spot is None or closed is None:
            out("SKIP  no File, Edit or View menu button found by point; the menu steps are skipped")
        elif report.step("click (a menu button)", lambda: drive.click(code, *spot)) is not None:
            opened = look("menu open")
            box = changed_box(closed, opened) if opened is not None else None
            report.verdict("the click opened the menu", box is not None and (box[3] - box[1]) > 60,
                           f"changed area {box}" if box else "nothing changed")
            if box is not None and (box[3] - box[1]) > 60:
                item = ((box[0] + box[2]) // 2, box[1] + max(40, (box[3] - box[1]) // 6))
                # Read-only: what the bridge says is under a point inside the open menu.
                try:
                    ask("window_screenshot", window=target["window"])
                    under = ask("window_probe", window=target["window"], x=item[0], y=item[1])
                    out(f"INFO  a point inside the open menu can be reached: role {under.get('role')}")
                except desktop.DesktopError as exc:
                    out(f"INFO  a point inside the open menu cannot be reached: {exc}")
            if look("before closing the menu") is not None:
                report.step("click (text area, closes the menu)", lambda: drive.click(code, x, y))

    # 4. Another window takes the front; bring Notepad back.
    open_program("charmap")
    away, deadline = False, time.monotonic() + 10
    while not away and time.monotonic() < deadline:
        time.sleep(0.5)
        rows, row = numbered()
        if rows is None or row is None:
            break
        away = not row["front"]
    if not away:
        out("SKIP  Character Map did not take the front; bringing a window forward is not tested")
    elif report.step("front (bring Notepad back)", lambda: drive.front(code, row["n"])) is not None:
        try:
            drive.click(code, x, y)
            report.verdict("a click needs a new screenshot after switching windows", False, "the click went through")
        except ComputerError:
            report.verdict("a click needs a new screenshot after switching windows", True, "refused")
        if look("after bringing Notepad forward") is not None:
            report.verdict("Notepad is in front again", True, "the new screenshot shows it")

    # 5. The action log, and stopping.
    states = [record.get("state") for record in records]
    report.verdict("every action was logged before and after",
                   states.count("start") > 0 and states.count("start") == len(states) - states.count("start"),
                   f"{states.count('start')} actions, outcomes: "
                   f"{sorted(set(states) - {'start'})}")
    drive.stop("check finished")
    try:
        drive.screenshot(code)
        report.verdict("after stop the code no longer works", False, "a screenshot was returned")
    except ComputerError:
        report.verdict("after stop the code no longer works", True, "refused")


def main():
    from jarvis.desktop_setup import _win_path

    prefix = base._bridge_dir()
    session = desktop.DesktopSession(0)
    session.start()
    log_path = Path(tempfile.gettempdir()) / "computer_live_smoke_bridge.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [str(prefix / "venv/Scripts/python.exe"), _win_path(str(ROOT / "windows/bridge.py")),
         "127.0.0.1", str(session.bound_port)], stdout=log, stderr=subprocess.STDOUT)
    report, records = base.Report(), []
    drive = ComputerDrive(session, enabled=lambda: True, log=records.append)

    def ask(op, **fields):
        return session.request(op, require_pair=True, scope=RAW_SCOPE, timeout=30, hud_sensitive=None, **fields)

    def open_program(name):
        command = ([base.NOTEPAD, _win_path(str(base._scroll_file(prefix)))] if name == "notepad" else [CHARMAP])
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        if not session.wait_for_bridge(20):
            print(f"FAIL  the Windows bridge did not connect and pair. Its output: {log_path}")
            return 1
        check(drive, drive.start(), ask, report, open_program, records)
    finally:
        drive.stop("check ended")
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
    print("\nAll steps passed: screenshots, clicks, the wheel and switching windows work through the driving code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

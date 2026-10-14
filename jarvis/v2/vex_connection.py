"""Shared, demand-driven V5 USB presence. No identifiers or raw output leave this module.

Schema/VID/PID evidence: docs/plans/2026-10-13-vex-download-device-status.md.
A successful enumeration proves USB presence only, never radio/download readiness.
"""
import json
import os
import re
import threading
import time

MIN_INTERVAL = 5.0
PROBE_TIMEOUT = 2.0
OUTPUT_CAP = 64 * 1024


def usb_presence(data: bytes) -> tuple[bool, bool]:
    """Validate the whole snapshot before any absence can be confirmed."""
    if len(data) > OUTPUT_CAP:
        raise ValueError("USB response too large")
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate USB field")
            obj[key] = value
        return obj
    # Under a sized PTY, Windows' console wraps the JSON in its own control
    # sequences (mode toggles, the window title, cursor show). Only those are
    # removed; anything else that is not the JSON still refuses.
    from .vex import _ANSI
    raw = json.loads(_ANSI.sub("", data.decode("utf-8")), object_pairs_hook=unique)
    if not isinstance(raw, dict) or not isinstance(raw.get("usb"), dict):
        raise ValueError("USB response not recognized")
    usb = raw["usb"]
    if not all(isinstance(usb.get(key), list) for key in ("vex", "dfu")):
        raise ValueError("USB lists missing")
    rows = usb["vex"] + usb["dfu"]
    if len(rows) > 64:
        raise ValueError("too many USB records")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("USB record not recognized")
        ids = []
        for key in ("vid", "pid"):
            value = row.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{4}", value):
                raise ValueError("USB identity not recognized")
            ids.append(int(value, 16))
        if ids[0] != 0x2888 or ids[1] not in (0x0501, 0x0503):
            raise ValueError("USB identity not supported")
        seen.add(ids[1])
    return 0x0501 in seen, 0x0503 in seen


class Connection:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.running = False
        self.next_at = 0.0
        self.failures = 0
        self.value = {"brain": None, "controller": None, "checked_at": None,
                      "state": "unknown", "message": "USB devices have not been checked.", "events": []}

    def want(self):
        # Reserve synchronously before returning: concurrent windows cannot queue probes.
        from .vex import VexError, load_config
        with self.lock:
            with self.host._lock:
                busy = self.host._device
                stopped = self.host._stopped
                waiting = self.host._device_waiters
            if busy and busy["kind"] != "connection":
                return self.view("busy", "USB check paused while a device action owns the port.")
            if stopped:
                return self.view("error", "Jarvis is stopping.")
            if not self.running and time.monotonic() >= self.next_at and not waiting:
                try:
                    cfg = load_config()
                    release, bind = self.host._reserve_device("connection")
                except VexError:
                    self.fail("USB check unavailable; check tooling or the active device action.")
                else:
                    self.running = True
                    self.value.update(state="checking", message="Checking USB devices…")
                    try:
                        threading.Thread(target=self.run, args=(cfg, release, bind), daemon=True,
                                         name="vex-usb-presence").start()
                    except RuntimeError:
                        release()
                        self.running = False
                        self.fail("USB check worker could not start; last confirmed state retained.")
            return self.view()

    def view(self, state=None, message=None):
        return {**self.value, "events": [dict(e) for e in self.value["events"]],
                **({"state": state, "message": message} if state else {})}

    def fail(self, message):
        self.failures = min(self.failures + 1, 5)
        self.next_at = time.monotonic() + MIN_INTERVAL * (2 ** (self.failures - 1))
        self.value.update(state="error", message=message)

    def run(self, cfg, release, bind):
        from .vex import _utcnow
        done = threading.Event()
        timed_out = threading.Event()

        def ended(terminal):
            # Only actual PTY settlement frees the device, including after timeout.
            try:
                if timed_out.is_set():
                    raise ValueError("USB check timed out; last confirmed state retained.")
                if terminal.exit_code != 0:
                    raise ValueError("USB tool failed; last confirmed state retained.")
                history = terminal.history()
                if history.start != 0:
                    raise ValueError("USB output was truncated")
                brain, controller = usb_presence(history.data)
                at = _utcnow()
                with self.lock:
                    events = self.value["events"]
                    for key, value in (("brain", brain), ("controller", controller)):
                        if self.value[key] is None or self.value[key] != value:
                            events.append({"device": key, "connected": value, "at": at})
                    self.value.update(brain=brain, controller=controller, checked_at=at,
                                      state="ready", message="USB check complete.", events=events[-32:])
                    self.failures = 0
                    self.next_at = time.monotonic() + MIN_INTERVAL
            except Exception:  # Untrusted output or an unavailable ring never changes confirmed state.
                with self.lock:
                    self.fail("USB check failed or returned an unsupported response; last confirmed state retained.")
            finally:
                release()
                done.set()

        terminal = None
        try:
            terminal = self.host._start([cfg.vexcom, "--usb"], cfg=cfg, tool="vexcom",
                folder=os.path.dirname(cfg.vexcom), label="VEX USB", program="vex-connection",
                title="VEX USB check", on_exit=ended)
            bind(terminal.id)
            self.host._announce("connection")
            if not done.wait(PROBE_TIMEOUT):
                timed_out.set()
                self.host._stop_program(terminal)
        except Exception:
            if terminal is None:
                release()
                with self.lock:
                    self.fail("USB check could not start; check tooling configuration.")
            else:
                timed_out.set()
                self.host._stop_program(terminal)
        finally:
            if terminal is not None:
                # One worker per daemon remains responsible until actual EOF. Never
                # close from on_exit: fast exit can run under create_program's lock.
                # A delayed callback retains its holder, so no new native read overlaps.
                done.wait()
                try:
                    self.host.daemon.terminals.close(terminal.id, grace=0)
                except Exception:
                    pass
            with self.lock:
                self.running = False

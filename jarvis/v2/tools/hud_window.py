"""Drive paired Windows windows from a current owner HUD chat."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Annotated
from unicodedata import category

from jarvis import config, runtime, untrusted
from jarvis.desktop import DesktopError, SESSION
from jarvis.tools import EXPLICIT_ONLY, PRIVATE_RESULT, ToolResult, tool
from windows.desktopguard import KEY_NAMES
from windows.windowpolicy import DRIVABLE, SEE_ONLY

from .. import desktop_risk as risk
from ..approvals import ApprovalRequest
from ..desktop_driver import REFUSED as OFF
from ..model import utcnow
from ..provider import Decision
from . import hud_desktop

HUD_WINDOW_TOOLS = frozenset({
    "hud_window_list", "hud_window_snapshot", "hud_window_screenshot",
    "hud_window_click", "hud_window_type", "hud_window_key", "hud_window_scroll",
})
UNAVAILABLE = "Refused: Windows window tools require a current owner HUD or paired voice chat turn."
FRESH_SNAPSHOT = "Take a fresh snapshot of this window in this chat."
COMPLETED = "Action completed. Take a fresh snapshot to see the result."
LOG_REFUSED = "Refused: the desktop action log could not be written."
LOG_OUTCOME_FAILED = "Error: The action outcome could not be recorded because the desktop action log could not be written."
BRIDGE_REFUSED = "Refused: The window refused that action. Take a fresh snapshot and try again."
BRIDGE_DISCONNECTED = "Error: The Windows bridge is not connected."
ACTION_FAILED = "Error: The action could not be completed."
_WINDOW_ID = re.compile(r"[0-9a-f]{16}")
_WINDOW_REF = re.compile(r"[0-9a-f]{16}-e[1-9][0-9]*")
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_DETAIL_LIMIT = 200
_TYPED_WITHHELD = "[typed text withheld]"
_service = None
_bound_grant = ContextVar("desktop_window_grant", default=None)

WindowId = Annotated[str, "Window id from hud_window_list"]
Purpose = Annotated[str, "One sentence describing why you are taking this action, up to 300 characters"]
Risky = Annotated[str, 'Your risk declaration: "yes", "no" or "unsure"']
Ref = Annotated[str, "Control ref from this chat's latest snapshot of this window"]
X = Annotated[int, "X in pixels from this chat's latest screenshot of this window"]
Y = Annotated[int, "Y in pixels from this chat's latest screenshot of this window"]


class Refused(Exception):
    pass


@contextmanager
def bind_grant(grant):
    binding = _bound_grant.set(grant)
    try:
        yield
    finally:
        _bound_grant.reset(binding)


def _window_id(window):
    return type(window) is str and _WINDOW_ID.fullmatch(window) is not None


def _control(info):
    if not isinstance(info, dict) or any(type(info.get(field)) is not str for field in ("name", "role")):
        raise Refused("The bridge did not identify the control safely.")
    return {"name": info["name"], "role": info["role"]}


def _selector(ref, x, y):
    if type(ref) is str and type(x) is int and type(y) is int:
        if ref and (x, y) == (-1, -1):
            return {"ref": ref}
        if not ref and x >= 0 and y >= 0:
            return {"x": x, "y": y}
    raise Refused("Provide either a snapshot ref or both screenshot coordinates.")


def _check(window, kind, fields):
    if not _window_id(window):
        raise Refused("Use a window id from hud_window_list.")
    if kind in ("click", "scroll"):
        selector = _selector(fields.pop("ref"), fields.pop("x"), fields.pop("y"))
        fields.update(selector)
    if kind == "scroll" and (type(fields["amount"]) is not int or not 1 <= abs(fields["amount"]) <= 100):
        raise Refused("Use a signed scroll amount of 1 to 100 wheel detents.")
    if kind == "type":
        text = fields["text"]
        if (type(text) is not str or len(text) > 2000
                or any(category(ch) in _HIDDEN for ch in text)):
            raise Refused("Use plain text up to 2000 characters without control characters. "
                          "Use submit or hud_window_key to press Enter.")
        if type(fields["submit"]) is not bool:
            raise Refused("Use a boolean submit flag.")
        if type(fields["ref"]) is not str or not fields["ref"]:
            raise Refused(FRESH_SNAPSHOT)
    if kind == "key":
        raw = fields["keys"]
        keys = risk.canonical_keys(tuple(raw)) if type(raw) is list else None
        if keys is None or not set(keys) <= KEY_NAMES:
            raise Refused("Use distinct key names supported by both the risk rules and Windows bridge.")
        fields["keys"] = list(keys)


def _audit_outcome(outcome, detail=""):
    suffix = json.dumps(outcome)
    if detail:
        suffix += ', "outcome_detail": ' + json.dumps(detail)
    return suffix.encode()


# Ensure enough room for 200 characters escaped as JSON surrogate pairs.
_OUTCOME_WIDTH = len(_audit_outcome("bridge-refused", "\U00010000" * _DETAIL_LIMIT))


def _append_audit(record, outcome):
    path = Path(config.V2_DATA_DIR) / "desktop_actions.jsonl"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OSError("unsafe desktop action log")
        os.fchmod(fd, 0o600)
        # Reserve a complete line before sending input, with room for the result.
        prefix = (json.dumps(record)[:-1] + ', "outcome": ').encode()
        suffix = _audit_outcome(outcome)
        if outcome == "pending":
            suffix = suffix.ljust(_OUTCOME_WIDTH)
        line = prefix + suffix + b"}\n"
        fcntl.flock(fd, fcntl.LOCK_EX)
        offset = os.lseek(fd, 0, os.SEEK_END)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("short desktop action log write")
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, offset)
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return fd, offset + len(prefix)
    except BaseException:
        os.close(fd)
        raise


def _update_audit(fd, offset, outcome, detail):
    suffix = _audit_outcome(outcome, detail).ljust(_OUTCOME_WIDTH)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        if os.pwrite(fd, suffix, offset) != len(suffix):
            raise OSError("short desktop action log update")
        os.fsync(fd)
    except OSError:
        pending = _audit_outcome("pending").ljust(_OUTCOME_WIDTH)
        os.lseek(fd, offset, os.SEEK_SET)
        while pending:
            written = os.write(fd, pending)
            if not written:
                raise OSError("short desktop action log rollback")
            pending = pending[written:]
        os.fsync(fd)
        raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _squash(value):
    """Text as the card would show it: invisible characters gone, runs of
    blank space one space, case folded. A label that only looks different
    from the typed text ("my  secret", a zero-width character inside it)
    compares equal."""
    visible = "".join(ch for ch in value if category(ch) not in ("Cf", "Cc", "Cs", "Mn", "Me"))
    return " ".join(visible.split()).casefold()


# The bridge shows at most 150 elements; a table far past that is not its reply.
_MAX_REFS = 1000


def _withhold_text(value, text, placeholder):
    if text and _squash(text) and _squash(text) in _squash(value):
        return placeholder
    return value


def _without_typed_text(value, text):
    """Free text (the assistant's purpose, a bridge error) with the typed
    text taken out, whatever its case. One character is not removed: that
    would strike a letter from every sentence."""
    if type(text) is str and len(text.strip()) >= 2:
        for needle in {text, text.strip()}:
            value = re.sub(re.escape(needle), _TYPED_WITHHELD, value, flags=re.IGNORECASE)
        stripped = text.strip()
        if len(stripped) >= 3:
            value = _withhold_text(value, stripped, _TYPED_WITHHELD)
    return value


def _reason_text(purpose, text):
    if type(purpose) is not str or any(category(ch) in _HIDDEN for ch in purpose):
        return ""
    return _without_typed_text(purpose, text)[:300]


def _disconnected(detail):
    return detail.startswith((
        "the Windows desktop bridge is not running.",
        "the desktop bridge disconnected.",
        "the desktop bridge closed the connection.",
        "lost the desktop bridge mid-request (",
    ))


def _action_failure(exc, text):
    try:
        detail = str(exc)
    except Exception:
        detail = type(exc).__name__
    safe_detail = _without_typed_text(detail, text)[:_DETAIL_LIMIT]
    if isinstance(exc, DesktopError):
        return "bridge-refused", BRIDGE_DISCONNECTED if _disconnected(detail) else BRIDGE_REFUSED, safe_detail
    return "failed", ACTION_FAILED, safe_detail


def _refusal(exc):
    if isinstance(exc, Refused):
        return f"Refused: {exc}"
    if isinstance(exc, DesktopError):
        detail = str(exc)
        return detail if detail.startswith("Refused:") else f"Error: {detail}"
    return "Error: the Windows bridge did not complete the request safely."


class WindowDrive:
    def __init__(self, daemon):
        self.daemon = daemon
        self._lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._refs = {}
        self._apps = {}

    def install(self):
        global _service
        _service = self

    def stop(self):
        global _service
        if _service is self:
            _service = None

    def current(self):
        return (_service is self and hud_desktop.current()
                and runtime.caller().get("provider") in ("claude", "codex"))

    def grant(self):
        grant = self.daemon.desktop_driver.grant((runtime.caller() or {}).get("thread_id"))
        expected = _bound_grant.get()
        return grant if expected is None or grant is expected else None

    def request(self, op, thread, *, current, acting=False, **fields):
        if not current():
            raise Refused("The owner chat turn is no longer current. Inspect the window again.")
        sensitive = self.daemon.hud_sensitive_reports.count(len(self.daemon.approvals.pending()))
        result = SESSION.request(op, require_pair=True, current=current, scope=thread,
                                 hud_sensitive=sensitive, **fields)
        if not acting and not current():
            raise Refused("The owner chat turn or snapshot is no longer current.")
        return result

    def windows(self, thread, current):
        rows = self.request("windows", thread, current=current)["windows"]
        apps = {}
        lines = []
        for row in rows:
            window, app, title, access = (row[key] for key in ("window", "app", "title", "access"))
            if not (_window_id(window) and type(app) is str and type(title) is str
                    and access in (DRIVABLE, SEE_ONLY)):
                raise Refused("The bridge did not identify the listed windows safely.")
            apps[window] = app
            lines.append(f"{window}\t{untrusted.one_line(app, 120)}\t"
                         f"{untrusted.one_line(title, 300)}\t{access.replace('_', '-')}")
        with self._lock:
            self._apps[thread] = apps
        return "\n".join(lines) or "No windows are available."

    def app(self, thread, window, current):
        with self._lock:
            app = self._apps.get(thread, {}).get(window)
        if app is None:
            self.windows(thread, current)
            with self._lock:
                app = self._apps.get(thread, {}).get(window)
        if app is None:
            raise Refused("Unknown window. List windows first.")
        return app

    def snapshot(self, thread, window, current):
        with self._snapshot_lock:
            with self._lock:
                self._refs.pop((thread, window), None)
            self.app(thread, window, current)
            result = self.request("window_snapshot", thread, current=current, window=window)
            if (not isinstance(result, dict) or type(result.get("snapshot")) is not str
                    or not result["snapshot"].strip() or not isinstance(result.get("refs"), dict)):
                raise Refused("The bridge did not provide a valid window snapshot.")
            if len(result["refs"]) > _MAX_REFS:
                raise Refused("The bridge did not provide valid snapshot refs.")
            refs = {}
            for ref, info in result["refs"].items():
                if (type(ref) is not str or not _WINDOW_REF.fullmatch(ref)
                        or not isinstance(info, dict) or info.keys() != {"name", "role"}):
                    raise Refused("The bridge did not provide valid snapshot refs.")
                refs[ref] = _control(info)
            with self._lock:
                self._refs[thread, window] = refs
            return result["snapshot"]

    def read(self, op, window=None):
        grant = self.grant()
        if grant is None:
            return OFF
        if not self.current():
            return UNAVAILABLE
        thread = runtime.caller()["thread_id"]

        def current():
            return self.grant() is grant and self.current()

        try:
            if op == "windows":
                return self.windows(thread, current)
            if not _window_id(window):
                raise Refused("Use a window id from hud_window_list.")
            if op == "window_snapshot":
                return self.snapshot(thread, window, current)
            if not runtime.hud_desktop_images():
                raise Refused("This chat's model does not support screenshots.")
            result = self.request(op, thread, current=current, window=window)
            return ToolResult("Windows window screenshot. Coordinates are in screenshot pixels.",
                              result["image_b64"], "image/png")
        except (Refused, DesktopError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            if self.grant() is not grant:
                return OFF
            if op == "windows" and isinstance(exc, DesktopError) and _disconnected(str(exc)):
                return BRIDGE_DISCONNECTED
            return _refusal(exc)

    def prepare(self, kind, window, thread, purpose, risky, fields, record, grant):
        _check(window, kind, fields)
        keys = tuple(fields.get("keys", ()))
        submit = fields.get("submit", False)
        record["keys"] = keys
        # Validate the declaration through the same rules, before bridge reads.
        declaration = risk.Action(kind, "", keys=keys, submit=submit, purpose=purpose, risky=risky)
        verdict = risk.decide(declaration)
        if verdict.decision == risk.REFUSE:
            _record_verdict(record, verdict)
            raise Refused(", ".join(verdict.reasons) + ".")

        ref = fields.get("ref")
        refs = None
        if ref:
            with self._lock:
                refs = self._refs.get((thread, window))
                expected = refs.get(ref) if refs is not None else None
            if expected is None:
                raise Refused(FRESH_SNAPSHOT)

        def current():
            if self.grant() is not grant or not self.current():
                return False
            if refs is None:
                return True
            with self._lock:
                return self._refs.get((thread, window)) is refs

        app = self.app(thread, window, current)
        text = fields.get("text", "").strip()
        sensitive = text if len(text) >= 3 else ""
        record["app"] = _withhold_text(app, sensitive, "an app showing the typed text")
        if kind == "key":
            expected = _control(self.request("window_probe", thread, current=current, window=window, focus=True))
        elif not ref:
            expected = _control(self.request("window_probe", thread, current=current, window=window,
                                             x=fields["x"], y=fields["y"]))
        elif kind == "type":
            live = _control(self.request("window_probe", thread, current=current, window=window, ref=ref))
            if live != expected:
                raise Refused(FRESH_SNAPSHOT)
            if live["role"] not in ("editable", "combobox"):
                raise Refused("Use a ref for an editable field.")
            expected = live

        action = risk.Action(kind, app, **expected, keys=keys, submit=submit, purpose=purpose, risky=risky)
        safe_name = _withhold_text(action.name, sensitive, "a field showing the typed text")
        record.update(name=safe_name[:120], role=action.role)
        verdict = risk.decide(action)
        _record_verdict(record, verdict)
        if verdict.decision == risk.REFUSE:
            raise Refused(", ".join(verdict.reasons) + ".")
        if not current():
            raise Refused("The chat turn or snapshot changed. Inspect the window again.")

        if verdict.decision == risk.ASK:
            approvals = self.daemon.approvals
            if getattr(approvals, "human_backed", False) is not True:
                raise Refused("This desktop action needs the owner at the HUD.")
            args = {"window": window, **fields, "purpose": record["purpose"], "risky": risky}
            if "text" in args:
                args["text"] = len(args["text"])
            request = ApprovalRequest(
                record["tool"], args=args,
                reason="Approve this one desktop action? Jarvis says: " + json.dumps(record["purpose"], ensure_ascii=False),
                headline=risk.statement(replace(action, app=record["app"], name=safe_name), verdict),
                layer="desktop-gate",
                thread_id=thread, provider=runtime.caller()["provider"], allowlistable=False,
                max_wait_s=120, grantable=False, owner_initiated=True, require_hud_answer=True,
            )
            answer = approvals.ask(request, cancelled=lambda: not current())
            if not current():
                raise Refused("The chat turn or snapshot changed. Inspect the window again.")
            if answer is not Decision.ALLOW:
                return "asked-declined", "The user declined to run this. Ask what they would like instead."
            record["asked"] = True

        return "pending", lambda: self.request("window_" + kind, thread, current=current,
                                               acting=True, window=window, expect=expected, **fields)


def _record_verdict(record, verdict):
    record.update(decision=verdict.decision, group=verdict.group, reasons=verdict.reasons)


def _read(op, window=None):
    service = _service
    return service.read(op, window) if service is not None else OFF


def invalid_call(name, arguments, missing, unexpected):
    if name not in HUD_WINDOW_TOOLS or name in {
        "hud_window_list", "hud_window_snapshot", "hud_window_screenshot",
    }:
        return None
    if missing:
        fault = "Missing required argument(s): " + ", ".join(missing) + "."
    elif unexpected:
        fault = "Use only the tool's declared arguments."
    else:
        return None
    return _act(name.removeprefix("hud_window_"), None, arguments.get("purpose"),
                arguments.get("risky"), fault=fault, text=arguments.get("text"))


def _act(kind, window, purpose, risky, *, fault=None, **fields):
    service = _service
    grant = service.grant() if service is not None else None
    if grant is None:
        return OFF
    who = runtime.caller() or {}
    thread = who.get("thread_id")
    record = {"at": utcnow(), "thread_id": thread, "tool": "hud_window_" + kind,
              "app": "", "name": "", "role": "", "keys": [],
              "purpose": _reason_text(purpose, fields.get("text")),
              "decision": risk.REFUSE, "group": "", "reasons": ()}
    outcome = "refused"
    text = fields.get("text")
    detail = ""
    try:
        if not service.current():
            record["reasons"] = ("unavailable",)
            result = UNAVAILABLE
        elif fault is not None:
            record["reasons"] = (fault,)
            result = f"Refused: {fault}"
        else:
            outcome, result = service.prepare(kind, window, thread, purpose, risky, fields, record, grant)
    except Refused as exc:
        if not record["reasons"]:
            record["reasons"] = (str(exc),)
        result = _refusal(exc)
    except Exception as exc:
        outcome, result, detail = _action_failure(exc, text)

    if service.grant() is not grant:
        return OFF
    try:
        if detail:
            record["outcome_detail"] = detail
        fd, offset = _append_audit(record, outcome)
    except OSError:
        return LOG_REFUSED
    try:
        if not callable(result):
            return result
        try:
            result()
            outcome, result = "asked-approved" if record.get("asked") else "run", COMPLETED
        except Refused as exc:
            outcome, result = "refused", OFF if service.grant() is not grant else _refusal(exc)
        except Exception as exc:
            if (isinstance(exc, DesktopError) and str(exc) == "The owner chat turn is no longer current."
                    and service.grant() is not grant):
                outcome, result = "refused", OFF
            else:
                outcome, result, detail = _action_failure(exc, text)
        try:
            _update_audit(fd, offset, outcome, detail)
        except OSError:
            return LOG_OUTCOME_FAILED
        return result
    finally:
        os.close(fd)


@tool
def hud_window_list() -> str:
    """List allowed Windows windows with their ids, apps, titles and access."""
    return _read("windows")


@tool
def hud_window_snapshot(window: WindowId) -> str:
    """Read a Windows window's accessible controls and refresh its refs for this chat."""
    return _read("window_snapshot", window)


@tool
def hud_window_screenshot(window: WindowId) -> ToolResult | str:
    """Look at a Windows window and get screenshot pixel coordinates."""
    return _read("window_screenshot", window)


@tool
def hud_window_click(window: WindowId, purpose: Purpose, risky: Risky,
                     ref: Ref = "", x: X = -1, y: Y = -1) -> str:
    """Click a Windows control by ref or screenshot coordinates."""
    return _act("click", window, purpose, risky, ref=ref, x=x, y=y)


@tool
def hud_window_type(window: WindowId, ref: Ref,
                    text: Annotated[str, "Plain text to replace the field contents, up to 2000 characters"],
                    purpose: Purpose, risky: Risky,
                    submit: Annotated[bool, "Press Enter after typing"] = False) -> str:
    """Replace text in a Windows field found in the current snapshot."""
    return _act("type", window, purpose, risky, ref=ref, text=text, submit=submit)


@tool
def hud_window_key(window: WindowId, keys: Annotated[list[str], "Names of the keys to press together"],
                   purpose: Purpose, risky: Risky) -> str:
    """Press a chord in the focused control of a Windows window."""
    return _act("key", window, purpose, risky, keys=keys)


@tool
def hud_window_scroll(window: WindowId, amount: Annotated[int, "Signed wheel detents from -100 to 100"],
                      purpose: Purpose, risky: Risky, ref: Ref = "", x: X = -1, y: Y = -1) -> str:
    """Scroll over a Windows control by ref or screenshot coordinates."""
    return _act("scroll", window, purpose, risky, amount=amount, ref=ref, x=x, y=y)


EXPLICIT_ONLY.update(HUD_WINDOW_TOOLS)
PRIVATE_RESULT.update(HUD_WINDOW_TOOLS - {"hud_window_list"})

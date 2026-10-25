"""§4.1 of the phone design, "the network stays secured" (work package P-4).

The phone answers with HUD authority (`owner_verified`, PH-7, owner decision
O-3: Always, and the HUD-only Browser cards) only while **every** one of
these holds. Each is a check here, by name:

  loopback      the remote listener's socket is bound to 127.0.0.1 only, on
                the port the daemon says (read from the socket itself, never
                from the configuration);
  serve         `tailscale serve status --json` (the Windows CLI, read-only)
                holds exactly one handler: `<host>:443` path `/`, proxying to
                `http://127.0.0.1:<port>`, TCP 443 with HTTPS and nothing
                else, and no Funnel for any port;
  certificate   the certificate is the tailnet's own: `tailscale status
                --json` says the backend runs, this machine's MagicDNS name
                is the configured host, the host is under the tailnet's
                `.ts.net` suffix and in `CertDomains` (the names Tailscale
                issues certificates for; `tailscale serve` terminates HTTPS
                with no other), and serve's 443 is HTTPS. Before an answer,
                the request's `Host` and `Origin` are exactly that name too;
  passkey       the passkeys file reads, and a paired, unrevoked passkey
                exists (registration and every assertion require user
                verification, `remote_auth.REQUIRE_UV`); before an answer,
                the session's own passkey is that. The answer's assertion is
                fresh and user-verified (`RemoteAuth.answer_verify`);
  listeners     nothing else listens on the port: in WSL, `ss` lists exactly
                one listener on it, 127.0.0.1, whose socket inode is the
                listener's own; on Windows, `Get-NetTCPConnection` (through
                `powershell.exe`, read-only) lists only WSL's `wslrelay` on
                loopback, or nothing.

**When it runs.** At start-up and every `INTERVAL_S` (5 minutes), from a
thread of its own; and **again, in full, before every verified answer and
every private card's detail** (`check`), never from a cache. The last result
is kept only to show it (`view`: the phone's `/m/api/me`, the desk's
`/status`), never to authorize, so a failure is never masked by an older
pass; and any check that fails replaces what is shown at once.

**Fail closed.** Each external read is a fixed argv (no shell), read-only,
bounded in time (`PROBE_TIMEOUT_S`) and in output (`OUTPUT_CAP`). A
timeout, an error, a non-zero exit, output that does not parse or a shape
not listed above is a failure of that check ("limited"). Nothing is read
until `attach_system()` (daemon2's `main()` only), so an embedded or a test
daemon runs no Windows command and stays limited unless a test injects
`run` or a check's `overrides`.

**Limited** is Discord-level authority on the phone (O-3): Allow and Deny on
ordinary cards, no Always, and a HUD-only card says "answer at the desk".
Authority comes back when the checks pass again; no setting widens it. The
result names the first check that failed and a fixed reason of this module's
own words: never a command's output, a name or an address.

SELF_PROTECTED: it decides when the phone has the HUD's authority. Tests:
tests/v2/remote_answer_check.py.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import socket
import subprocess
import threading
import time

LOG = logging.getLogger(__name__)

CHECKS = ("loopback", "serve", "certificate", "passkey", "listeners")
# What the phone and the desk say after "limited: ".
LABELS = {
    "loopback": "the phone's listener is not on loopback only",
    "serve": "tailscale serve is not the only path, or Funnel is on",
    "certificate": "the name or certificate is not the tailnet's own",
    "passkey": "no paired passkey that requires verification",
    "listeners": "something else listens on the phone's port",
    "pending": "not checked yet",
}
INTERVAL_S = 300.0
PROBE_TIMEOUT_S = 8.0
OUTPUT_CAP = 1024 * 1024

TAILSCALE = "/mnt/c/Program Files/Tailscale/tailscale.exe"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WINDOWS_RELAY = "wslrelay"
LOOPBACK = "127.0.0.1"


class ReadFailed(Exception):
    """An external read that did not give an answer: its reason is fixed."""


def run_command(argv: list[str], timeout: float) -> str:
    """One read-only command, no shell, within `timeout`: its standard
    output, or ReadFailed (timeout, a non-zero exit, too much output)."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        raise ReadFailed("timed out") from None
    except OSError:
        raise ReadFailed("could not run") from None
    if done.returncode != 0:
        raise ReadFailed("failed")
    if len(done.stdout) > OUTPUT_CAP:
        raise ReadFailed("too much output")
    return done.stdout.decode("utf-8", "replace")


def windows_listeners_argv(port: int) -> list[str]:
    script = (f"Get-NetTCPConnection -State Listen -LocalPort {int(port)} -ErrorAction SilentlyContinue |"
              " ForEach-Object { $_.LocalAddress + ' ' + (Get-Process -Id $_.OwningProcess).ProcessName }")
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script]


def reads(port: int) -> dict[str, list[str]]:
    """The external reads one check makes, by name: fixed argv each."""
    return {
        "serve": [TAILSCALE, "serve", "status", "--json"],
        "status": [TAILSCALE, "status", "--json"],
        "ss": ["ss", "-H", "-l", "-t", "-n", "-e", "sport", "=", f":{int(port)}"],
        "windows": windows_listeners_argv(port),
    }


class _Fail(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _json(text) -> dict:
    if isinstance(text, ReadFailed):
        raise _Fail(f"tailscale could not be read ({text})")
    try:
        value = json.loads(text or "{}")
    except ValueError:
        raise _Fail("tailscale's answer is not JSON") from None
    if not isinstance(value, dict):
        raise _Fail("tailscale's answer is not an object")
    return value


# -- the checks: each raises _Fail with a fixed reason ------------------------------


def _server(daemon):
    auth = getattr(daemon, "remote_auth", None)
    return getattr(auth, "server", None)


def check_loopback(daemon, ctx) -> None:
    server = _server(daemon)
    port = getattr(daemon, "remote_port", None)
    if server is None or not isinstance(port, int):
        raise _Fail("the listener is not running")
    try:
        sock = server.socket
        name = sock.getsockname()
        family = sock.family
    except (OSError, AttributeError):
        raise _Fail("the listener's socket cannot be read") from None
    if family != socket.AF_INET or not isinstance(name, tuple) or name[:2] != (LOOPBACK, port):
        raise _Fail("the listener is bound beyond loopback")
    if tuple(server.server_address[:2]) != (LOOPBACK, port):
        raise _Fail("the listener is bound beyond loopback")


def _serve_config(ctx) -> dict:
    if "serve_config" not in ctx:
        ctx["serve_config"] = _json(ctx.get("serve"))
    return ctx["serve_config"]


def check_serve(daemon, ctx) -> None:
    host = (daemon.remote_host or "").lower()
    port = daemon.remote_port
    cfg = _serve_config(ctx)
    for key, value in cfg.items():
        if key not in ("TCP", "Web", "AllowFunnel") and value:
            raise _Fail("serve holds an entry of another kind")
    funnel = cfg.get("AllowFunnel") or {}
    if not isinstance(funnel, dict) or any(funnel.values()):
        raise _Fail("Funnel is on")
    web = cfg.get("Web") or {}
    tcp = cfg.get("TCP") or {}
    if not isinstance(web, dict) or not isinstance(tcp, dict):
        raise _Fail("serve's configuration has an unknown shape")
    handlers = []
    for hostport, site in web.items():
        found = site.get("Handlers") if isinstance(site, dict) else None
        if not isinstance(found, dict) or set(site) - {"Handlers"}:
            raise _Fail("serve's configuration has an unknown shape")
        for path, handler in found.items():
            if not isinstance(handler, dict) or set(handler) != {"Proxy"}:
                raise _Fail("serve has a handler that is not a proxy")
            handlers.append((str(hostport).lower(), path, handler["Proxy"]))
    if len(handlers) != 1:
        raise _Fail("serve has no handler" if not handlers else "serve has more than one handler")
    if handlers[0] != (f"{host}:443", "/", f"http://{LOOPBACK}:{port}"):
        raise _Fail("serve's handler points elsewhere")
    if tcp != {"443": {"HTTPS": True}}:
        raise _Fail("serve listens on more than HTTPS 443")


def check_certificate(daemon, ctx) -> None:
    host = (daemon.remote_host or "").lower()
    status = _json(ctx.get("status"))
    me = status.get("Self") if isinstance(status.get("Self"), dict) else {}
    suffix = status.get("MagicDNSSuffix")
    domains = status.get("CertDomains")
    name = me.get("DNSName")
    if status.get("BackendState") != "Running":
        raise _Fail("Tailscale is not running")
    if not isinstance(name, str) or name.rstrip(".").lower() != host or not host:
        raise _Fail("the configured name is not this machine's tailnet name")
    if (not isinstance(suffix, str) or not suffix.lower().endswith(".ts.net")
            or not host.endswith("." + suffix.lower())):
        raise _Fail("the configured name is not under the tailnet's ts.net name")
    if not isinstance(domains, list) or host not in [d.lower() for d in domains if isinstance(d, str)]:
        raise _Fail("Tailscale issues no certificate for the configured name")
    tcp = _serve_config(ctx).get("TCP") or {}
    if not isinstance(tcp, dict) or not isinstance(tcp.get("443"), dict) or tcp["443"].get("HTTPS") is not True:
        raise _Fail("serve does not terminate HTTPS with the tailnet's certificate")
    handler = ctx.get("handler")
    if handler is not None:
        from .remote_auth import _headers
        if _headers(handler, "Host") != [daemon.remote_host]:
            raise _Fail("the request's Host is not the tailnet name")
        origin = _headers(handler, "Origin")
        exact = ["https://" + daemon.remote_host]
        # A write carries exactly the phone's Origin (§4 item 4); a read may
        # carry none, but never another.
        if (origin != exact if getattr(handler, "command", None) not in ("GET", "HEAD")
                else origin not in (None, [], exact)):
            raise _Fail("the request's Origin is not the tailnet name")


def check_passkey(daemon, ctx) -> None:
    auth = getattr(daemon, "remote_auth", None)
    if auth is None:
        raise _Fail("no passkeys")
    ok, reason = auth.passkey_ready(ctx.get("session"))
    if not ok:
        raise _Fail(reason)


def _own_inode(daemon) -> int | None:
    server = _server(daemon)
    try:
        return os.fstat(server.socket.fileno()).st_ino
    except (OSError, AttributeError, ValueError):
        return None


def check_listeners(daemon, ctx) -> None:
    port = daemon.remote_port
    text = ctx.get("ss")
    if isinstance(text, ReadFailed) or text is None:
        raise _Fail(f"WSL's listeners could not be read ({text})")
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise _Fail("more than one listener on the port in WSL" if lines else "WSL lists no listener on the port")
    fields = lines[0]
    if len(fields) < 4 or fields[3] != f"{LOOPBACK}:{port}":
        raise _Fail("a WSL listener on the port is not on loopback")
    inode = next((f[4:] for f in fields if f.startswith("ino:")), None)
    own = _own_inode(daemon)
    if inode is None or own is None or inode != str(own):
        raise _Fail("the WSL listener on the port is not this daemon's")
    rows = ctx.get("windows")
    if isinstance(rows, ReadFailed) or rows is None:
        raise _Fail(f"Windows' listeners could not be read ({rows})")
    for row in (r.strip() for r in rows.splitlines()):
        if not row:
            continue
        parts = row.split()
        if len(parts) != 2 or parts[0] not in (LOOPBACK, "::1") or parts[1].lower() != WINDOWS_RELAY:
            raise _Fail("something else listens on the port on Windows")


CHECK_FUNCS = {"loopback": check_loopback, "serve": check_serve, "certificate": check_certificate,
               "passkey": check_passkey, "listeners": check_listeners}
# The external reads each check needs (none: it reads the daemon only).
NEEDS = {"serve": ("serve",), "certificate": ("serve", "status"), "listeners": ("ss", "windows")}


class SecureMonitor:
    """The five checks for one daemon. `run(argv, timeout)` reads one
    external command (None until `attach_system`: every check that needs
    one then fails, "not attached"). `overrides` replaces a check by name
    (tests: `fn(daemon, ctx)` raising or returning). `wall` stamps results."""

    def __init__(self, daemon, *, run=None, wall=time.time):
        self.daemon = daemon
        self.run = run
        self.wall = wall
        self.overrides: dict = {}
        self._lock = threading.Lock()        # one check at a time
        self._state = threading.Lock()       # the shown result
        self._last = {"ok": False, "failed": "pending", "reason": LABELS["pending"], "at": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.runs = 0                        # how many checks ran (tests)

    def attach_system(self) -> None:
        """The real reads (daemon2's `main()` only)."""
        self.run = run_command

    # -- one check -------------------------------------------------------------------

    def _gather(self, needed: set[str]) -> dict:
        port = getattr(self.daemon, "remote_port", None)
        out: dict = {}
        if not needed:
            return out
        if self.run is None or not isinstance(port, int):
            return {name: ReadFailed("not attached") for name in needed}
        argvs = reads(port)

        def one(name):
            try:
                return name, self.run(argvs[name], PROBE_TIMEOUT_S)
            except ReadFailed as exc:
                return name, exc
            except Exception:  # noqa: BLE001 — anything else is a failed read too
                return name, ReadFailed("failed")

        with ThreadPoolExecutor(max_workers=len(needed), thread_name_prefix="jarvis-secure") as pool:
            for name, value in pool.map(one, sorted(needed)):
                out[name] = value
        return out

    def check(self, *, session: dict | None = None, handler=None) -> dict:
        """Run every check now, in full (never a cached answer): {ok,
        failed, reason, label, failures, at}. `session` and `handler` add the
        answer's own conditions (its passkey, its Host and Origin)."""
        with self._lock:
            self.runs += 1
            needed = {r for name in CHECKS if name not in self.overrides for r in NEEDS.get(name, ())}
            ctx = self._gather(needed)
            ctx["session"], ctx["handler"] = session, handler
            failures: list[tuple[str, str]] = []
            for name in CHECKS:
                fn = self.overrides.get(name) or CHECK_FUNCS[name]
                try:
                    fn(self.daemon, ctx)
                except _Fail as exc:
                    failures.append((name, exc.reason))
                except Exception as exc:  # noqa: BLE001 — a check that breaks fails
                    failures.append((name, f"the check failed ({type(exc).__name__})"))
            result = {"ok": not failures, "failed": failures[0][0] if failures else None,
                      "reason": failures[0][1] if failures else "",
                      "failures": [name for name, _ in failures], "at": self.wall()}
            self._show(result)
            return dict(result)

    def _show(self, result: dict) -> None:
        """Keep the newest result to show: a failure replaces a pass at once,
        and a pass (all five held) replaces a failure."""
        with self._state:
            before = self._last
            self._last = dict(result)
        if before.get("ok") != result["ok"] or before.get("failed") != result["failed"]:
            if result["ok"]:
                LOG.info("phone: every §4.1 check passes; the phone answers with the HUD's authority")
            else:
                LOG.warning("phone: limited (%s: %s); the phone answers with Discord's authority",
                            result["failed"], result["reason"])

    def view(self) -> dict:
        """The last result, to show (never to authorize): {ok, failed,
        reason, label, at}."""
        with self._state:
            last = dict(self._last)
        failed = last.get("failed")
        return {"ok": bool(last.get("ok")), "failed": failed, "reason": last.get("reason") or "",
                "label": LABELS.get(failed, "") if failed else "", "at": last.get("at")}

    # -- the schedule ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.check()
                except Exception:  # noqa: BLE001 — never kills the loop
                    LOG.exception("phone: a §4.1 check broke")
                self._stop.wait(INTERVAL_S)

        self._thread = threading.Thread(target=loop, name="jarvis-secure", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(PROBE_TIMEOUT_S + 1)

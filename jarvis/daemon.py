"""`jarvis daemon` — the headless always-on service (away-agent phase 1).

Runs the pieces of Jarvis that must outlive a terminal and a window: the
Discord gateway (the owner's away-from-desk channel) and an approval broker
whose only surface is that channel — every dangerous-tool question goes to
the owner's DMs, and every failure path denies, exactly as at the desk.

What it deliberately does NOT run: the HUD, the workshop, the whiteboard,
voice pre-warm (a voice note loads the TTS model lazily on first use), and
any browser window. The face remains "daemon + window": when a daemon is
running, `jarvis face` skips the gateway and detaches its remote approval
channel, because DM answers arrive at *this* process's broker — a face-made
DM could never be answered and would only time out.

Single-instance rule: the health endpoint on DAEMON_PORT doubles as the
lock. It is read-only (GET /status), serves loopback only, and exposes no
approve/resolve route — nothing on it can decide anything, so it is not a
control plane the agent could reach.

Provisioning is human-only, per the project rule: `jarvis daemon install`
prints the systemd user unit and the commands; it writes nothing.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config


class DaemonError(RuntimeError):
    """Refusal to start, with a message meant for the owner's eyes."""


# ---- probes (import-light: face/server.py calls is_running at startup) ------


def is_running(port: int | None = None) -> bool:
    """True if a jarvis daemon is serving its health endpoint on `port`."""
    import http.client

    try:
        conn = http.client.HTTPConnection("localhost", port or config.DAEMON_PORT, timeout=2)
        conn.request("GET", "/status")
        body = conn.getresponse().read()
        conn.close()
        return bool(json.loads(body).get("jarvis-daemon"))
    except Exception:
        return False


def face_is_serving(port: int | None = None) -> bool:
    """True if a face server owns FACE_PORT (same probe the face itself uses)."""
    import http.client

    try:
        conn = http.client.HTTPConnection("localhost", port or config.FACE_PORT, timeout=2)
        conn.request("GET", "/config")
        body = conn.getresponse().read()
        conn.close()
        return "stt" in json.loads(body)
    except Exception:
        return False


# ---- the daemon --------------------------------------------------------------


class Daemon:
    """Gateway + remote-only approvals + a health endpoint, and nothing else.

    `listener_factory` and `channel` are injection points for the free test
    suite (no websocket, no Discord REST); production callers take the
    defaults. `port=0` binds an ephemeral port (tests), and `self.port` is
    rewritten to whatever was actually bound.
    """

    def __init__(
        self,
        port: int | None = None,
        listener_factory=None,
        channel=None,
        announce=print,
    ):
        self.port = config.DAEMON_PORT if port is None else port
        self._listener_factory = listener_factory
        self._channel = channel
        self._announce = announce
        self.broker = None
        self.responder = None
        self.listener = None
        self.runner = None
        self._health: ThreadingHTTPServer | None = None
        self.started_at: float | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if not config.DISCORD_TOKEN_PATH.exists():
            raise DaemonError(
                "Discord is not connected, and Discord is the daemon's only "
                "surface — there would be nobody to talk to and nowhere to "
                "send approval questions. Run `jarvis auth discord` first."
            )
        if face_is_serving():
            raise DaemonError(
                "a `jarvis face` is running and already serves the Discord "
                "gateway. Stop it first — or just keep using it: the daemon "
                "only exists for when no face is up."
            )
        # Bind the health endpoint first: it is the single-instance lock, so
        # losing the race must happen before anything else starts.
        try:
            self._health = _health_server(self)
        except OSError as exc:
            if exc.errno != 98:  # EADDRINUSE
                raise
            if is_running(self.port):
                raise DaemonError(
                    f"another jarvis daemon is already running on port {self.port}."
                ) from exc
            raise DaemonError(
                f"port {self.port} is taken by something that is not a "
                "jarvis daemon (JARVIS_DAEMON_PORT overrides)."
            ) from exc
        self.port = self._health.server_address[1]

        from . import discord_approvals, permissions
        from .face.approvals import ApprovalBroker

        channel = self._channel or discord_approvals.DiscordApprovals(
            announce=self._announce
        )
        # viewers=0 forever: there is no window here, so every question goes
        # out over the remote channel or denies as nowhere-to-ask. ALWAYS
        # replies write the same persistent allowlist the desk surfaces use.
        self.broker = ApprovalBroker(
            broadcast=lambda kind, data: None,
            viewers=lambda: 0,
            on_always=permissions.add_allow,
            announce=self._announce,
            remote=channel,
        )
        if hasattr(channel, "bind"):
            channel.bind(self.broker)

        from . import discord_agent

        self.responder = discord_agent.DiscordResponder(
            broker=self.broker,
            channel=channel,
            on_note=lambda text: self._announce(f"[daemon] {text}"),
        )

        # The goal runner: background goals worked in slices, gated by the
        # same remote broker, progress DMed to the owner. Its DM verbs
        # (goal:/steer:/goal status/goal cancel) are parsed in _route ahead
        # of the conversation agent — typed messages only, so a mishearing
        # can never steer or cancel a goal.
        from . import goalrunner

        self.runner = goalrunner.GoalRunner(
            approver=permissions.gate(self.broker.approver(remote=True)),
            announce=self._announce,
        )
        self.runner.start()

        if self._listener_factory is not None:
            self.listener = self._listener_factory(self._route)
        else:
            from . import discord_gateway

            self.listener = discord_gateway.GatewayListener(
                run_turn=self._route, announce=self._announce
            )
        self.listener.start()
        self.started_at = time.time()
        self._announce(
            f"jarvis daemon up — status on http://localhost:{self.port}/status"
        )

    def _route(self, text: str, channel_id: str, spoken: bool = False) -> str:
        """Goal verbs first (typed only), then the conversation agent."""
        if not spoken and self.runner is not None:
            handled = self.runner.handle_dm(text)
            if handled is not None:
                return handled
        return self.responder.turn(text, channel_id, spoken)

    def stop(self) -> None:
        """Mirror of the face's shutdown: never leave a waiter blocked.

        Broker first, runner second — a goal slice blocked on an approval DM
        is released by deny_all, which is what lets the runner's join finish
        inside its timeout."""
        if self.broker is not None:
            self.broker.deny_all("shutdown")
        if self.runner is not None:
            self.runner.stop()
        if self.listener is not None:
            self.listener.stop()
        if self._health is not None:
            self._health.shutdown()
            self._health.server_close()
            self._health = None
        # A browser left running EPIPEs the Node driver when the process dies
        # under it; stop() is a safe no-op if no turn ever browsed.
        try:
            from .browser import SESSION

            SESSION.stop(trace_name="daemon")
        except Exception:
            pass  # shutdown must finish even if Playwright never installed

    def status(self) -> dict:
        listener = self.listener
        payload = {
            "jarvis-daemon": True,
            "pid": os.getpid(),
            "started": self.started_at,
            "identified": bool(getattr(listener, "bot_id", "")),
            "pending_approvals": self.broker.pending_count if self.broker else 0,
            "model": config.TIERS["orchestrator"],
        }
        runner = self.runner
        if runner is not None:
            goal = runner.current
            payload["goal"] = (
                {
                    "id": goal.id,
                    "status": goal.status,
                    "slices": goal.slices,
                    "spent_usd": round(goal.spent_usd, 4),
                }
                if goal is not None
                else None
            )
        return payload


def _health_server(daemon: Daemon) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/status":
                self.send_error(404)
                return
            body = json.dumps(daemon.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", daemon.port), Handler)
    threading.Thread(target=server.serve_forever, name="daemon-health", daemon=True).start()
    return server


# ---- entry points -------------------------------------------------------------


def run() -> int:
    daemon = Daemon()
    try:
        daemon.start()
    except DaemonError as exc:
        print(f"jarvis daemon: {exc}")
        return 1

    stop = threading.Event()
    # systemd stops units with SIGTERM; treat it exactly like Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            stop.wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


UNIT_NAME = "jarvis-daemon.service"

# %h is the home directory; WorkingDirectory matters because the agent's file
# and shell tools resolve relative paths against the process CWD, and
# systemd's default is / — a relative write from a daemon turn should land
# somewhere the owner would look, not in the filesystem root.
UNIT_TEMPLATE = f"""[Unit]
Description=Jarvis daemon (Discord gateway + remote approvals)
After=network-online.target

[Service]
ExecStart=%h/.local/bin/jarvis daemon
WorkingDirectory=%h
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install() -> int:
    """Print the unit and the steps. Writes nothing — provisioning is human-only."""
    unit_path = f"~/.config/systemd/user/{UNIT_NAME}"
    print(f"# 1. Save this as {unit_path}:\n")
    print(UNIT_TEMPLATE)
    print("# 2. Enable it (no sudo needed for --user units):")
    print("systemctl --user daemon-reload")
    print(f"systemctl --user enable --now {UNIT_NAME}")
    print()
    print("# 3. Keep it alive after you close every terminal:")
    print("loginctl enable-linger $USER   # may prompt for authentication")
    print()
    print("# 4. WSL keeps the VM alive only while something runs. Belt and")
    print("#    braces on the Windows side (pick either or both):")
    print("#    - Task Scheduler, at logon:  wsl.exe -d Ubuntu --exec sleep infinity")
    print('#    - %UserProfile%\\.wslconfig:  [wsl2] then vmIdleTimeout=-1')
    print("#    And away-mode means the PC's power plan must not sleep.")
    print()
    print("# 5. Verify:")
    print(f"systemctl --user status {UNIT_NAME}")
    print(f"curl -s localhost:{config.DAEMON_PORT}/status")
    return 0

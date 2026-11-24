"""Command-line entry points for the v3 daemon."""

import argparse
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import signal
import time

from jarvis import config
from . import switch
from .daemon import Daemon, SERVICE_NAME


def run_daemon(_args) -> int:
    daemon = Daemon()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        daemon.start()
        while not stopping:
            time.sleep(0.1)
    finally:
        daemon.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def status(_args) -> int:
    port = config.V3_API_PORT
    address = f"127.0.0.1:{port}"
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    response = None
    try:
        connection.request("GET", "/v3/status", headers={"Host": address})
        response = connection.getresponse()
        if response.status == 200:
            payload = json.loads(response.read())
            if (isinstance(payload, dict) and payload.get("service") == SERVICE_NAME
                    and payload.get("version") == "v3"):
                print(f"v3 daemon running on {address}")
                return 0
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    if response is None:
        print(f"v3 daemon not running on {address}")
    else:
        print(f"Something else answers on {address}")
    return 1


def _open_v3_hud(env: switch.Env) -> None:
    """A5 hook for opening the installed v3 HUD."""
    env.out("no v3 HUD yet")


def start(args) -> int:
    env = switch.default_env()
    result = switch.to_v3(env, force=args.force)
    if result == 0:
        _open_v3_hud(env)
    return result


def stop(_args) -> int:
    return switch.stop_v3(switch.default_env())


def restart(_args) -> int:
    return switch.restart_v3(switch.default_env())


def install(_args) -> int:
    repo = Path(config.REPO_ROOT)
    return switch.install_unit(
        switch.default_env(),
        unit_path=Path.home() / ".config/systemd/user/jarvis-agent.service",
        python=str(repo / ".venv/bin/python"),
        repo=repo,
    )


def add_commands(sub):
    agentd = sub.add_parser("agentd", help="run the v3 daemon in the foreground")
    agentd.set_defaults(func=run_daemon)

    agent = sub.add_parser("agent", help="v3 daemon commands")
    agent_sub = agent.add_subparsers(dest="action", required=True)
    agent_status = agent_sub.add_parser("status", help="show v3 daemon status")
    agent_status.set_defaults(func=status)
    # The owner-op gate recognizes the action word. Options must take no values.
    agent_start = agent_sub.add_parser("start", help="switch to the v3 daemon")
    agent_start.add_argument("--force", action="store_true", help="switch even if v2 is busy")
    agent_start.set_defaults(func=start)
    agent_sub.add_parser("stop", help="stop the v3 daemon").set_defaults(func=stop)
    agent_sub.add_parser("restart", help="restart the v3 daemon").set_defaults(func=restart)
    agent_sub.add_parser("install", help="install the v3 systemd user unit").set_defaults(func=install)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your personal agent.")
    add_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(exc)
        return 1

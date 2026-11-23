"""Command-line entry points for the v3 daemon."""

from http.client import HTTPConnection, HTTPException
import json
import signal
import time

from jarvis import config
from .daemon import Daemon


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
    running = False
    try:
        connection.request("GET", "/v3/status", headers={"Host": address})
        response = connection.getresponse()
        if response.status == 200:
            payload = json.loads(response.read())
            running = isinstance(payload, dict) and payload.get("version") == "v3"
    except (OSError, HTTPException, ValueError):
        pass
    finally:
        connection.close()
    if running:
        print(f"v3 daemon running on {address}")
        return 0
    print(f"v3 daemon not running on {address}")
    return 1

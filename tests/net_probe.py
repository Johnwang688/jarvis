"""Network/TTS latency probe — run this WHILE voice feels slow.

Stages every fresh connection (DNS / TCP / TLS / HTTP) against OpenRouter
plus a control host, so an intermittent stall gets attributed instead of
guessed at:

  slow DNS everywhere        -> resolver (WSL NAT / Tailscale DNS interception)
  slow TCP/TLS everywhere    -> network path / wifi
  slow HTTP only, one host   -> that service's edge or backend
  clean here but voice slow  -> the TTS provider or OpenRouter's audio proxy;
                                compare with --tts

Free without --tts; --tts makes 3 real synthesis calls (~$0.0001).

Run:  .venv/bin/python tests/net_probe.py [--tts]
"""

from __future__ import annotations

import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def staged(host: str, rounds: int = 6, gap: float = 4.0) -> None:
    print(f"--- {host}")
    for _ in range(rounds):
        try:
            t0 = time.monotonic()
            addr = socket.getaddrinfo(host, 443, socket.AF_INET)[0][4]
            dns = (time.monotonic() - t0) * 1000
            t0 = time.monotonic()
            sock = socket.create_connection(addr, timeout=30)
            tcp = (time.monotonic() - t0) * 1000
            t0 = time.monotonic()
            tls_sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host
            )
            tls = (time.monotonic() - t0) * 1000
            t0 = time.monotonic()
            tls_sock.sendall(
                f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            )
            tls_sock.recv(256)
            http = (time.monotonic() - t0) * 1000
            tls_sock.close()
            total = dns + tcp + tls + http
            flag = "  <-- SLOW" if total > 1500 else ""
            print(
                f"  dns {dns:6.0f}  tcp {tcp:6.0f}  tls {tls:6.0f}  "
                f"http {http:6.0f}  = {total:6.0f} ms{flag}"
            )
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
        time.sleep(gap)


def tts_probe() -> None:
    from jarvis import config, voice

    print(f"--- wired TTS (model {config.TTS_MODEL}, pin {config.TTS_PROVIDER or 'none'})")
    for i in range(3):
        t0 = time.monotonic()
        voice.tts("Right away, sir.")
        print(f"  tts call {i + 1}: {(time.monotonic() - t0) * 1000:6.0f} ms")
        time.sleep(2)


def main() -> int:
    staged("openrouter.ai")
    staged("www.google.com", rounds=3, gap=2)
    if "--tts" in sys.argv:
        tts_probe()
    return 0


if __name__ == "__main__":
    sys.exit(main())

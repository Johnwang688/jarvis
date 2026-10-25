"""Spike P-0 (a) for the phone surface (docs/plans/2026-10-24-phone-surface-design.md §10).

Read-only checks that the planned path works on this PC, run while
`p0_phone.py` is listening on 127.0.0.1:8406 and `tailscale serve` points at it:

  1. in WSL, 8406 is bound to loopback only (§4.1 condition 1, and 5 for WSL);
  2. Windows reaches the WSL listener at 127.0.0.1:8406 (WSL2 localhost forwarding);
  3. `tailscale serve status --json`, read from WSL through the Windows CLI, has exactly one
     HTTPS handler for this machine's name, proxying to 127.0.0.1:8406, and no Funnel (condition 2);
  4. Windows fetches https://<name>/health with a certificate it trusts (condition 3);
  5. on Windows, nothing but WSL's relay listens on 8406 (condition 5 for Windows).

Changes nothing. The owner's one-time setup, from Windows:

    tailscale serve --bg --https=443 http://127.0.0.1:8406

and to undo it: `tailscale serve --https=443 off` (or `tailscale serve reset`).
Exit status 0 only if every check passes.
"""
from __future__ import annotations

import json
import subprocess
import sys

TAILSCALE = "/mnt/c/Program Files/Tailscale/tailscale.exe"
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
PORT = 8406
TARGET = f"http://127.0.0.1:{PORT}"


def run(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ps(script: str) -> subprocess.CompletedProcess:
    return run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], timeout=60)


def name() -> str:
    return json.loads(run([TAILSCALE, "status", "--json"]).stdout)["Self"]["DNSName"].rstrip(".")


def check_wsl_bind() -> tuple[bool, str]:
    out = run(["ss", "-ltnH", f"sport = :{PORT}"]).stdout.split()
    locals_ = [w for w in out if w.endswith(f":{PORT}")]
    if not locals_:
        return False, "nothing listening on 8406 in WSL (start p0_phone.py)"
    bad = [a for a in locals_ if not a.startswith(("127.0.0.1:", "[::1]:"))]
    return (not bad), f"WSL listeners: {locals_}"


def check_windows_reaches_wsl() -> tuple[bool, str]:
    r = ps(f"try {{ (Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 {TARGET}/health).Content }}"
           " catch { 'ERR ' + $_.Exception.Message }")
    text = r.stdout.strip()
    return text.startswith("{") and '"ok": true' in text, f"Windows → {TARGET}/health: {text[:160]}"


def check_serve(host: str) -> tuple[bool, str]:
    r = run([TAILSCALE, "serve", "status", "--json"])
    if r.returncode != 0:
        return False, f"serve status failed: {r.stderr.strip()[:160]}"
    try:
        cfg = json.loads(r.stdout or "{}")
    except ValueError:
        return False, "serve status is not JSON"
    funnel = {k: v for k, v in (cfg.get("AllowFunnel") or {}).items() if v}
    web = cfg.get("Web") or {}
    handlers = [(hp, path, h.get("Proxy")) for hp, site in web.items()
                for path, h in (site.get("Handlers") or {}).items()]
    tcp = cfg.get("TCP") or {}
    ok = (not funnel
          and handlers == [(f"{host}:443", "/", TARGET)]
          and set(tcp) == {"443"} and tcp["443"].get("HTTPS") is True)
    return ok, f"handlers={handlers} tcp={sorted(tcp)} funnel={sorted(funnel) or 'off'}"


def check_https(host: str) -> tuple[bool, str]:
    r = ps(f"try {{ $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 15 https://{host}/health;"
           " 'HTTP ' + $r.StatusCode + ' ' + $r.Content } catch { 'ERR ' + $_.Exception.Message }")
    text = r.stdout.strip()
    return text.startswith("HTTP 200"), f"Windows → https://{host}/health: {text[:160]}"


def check_windows_listeners() -> tuple[bool, str]:
    r = ps(f"Get-NetTCPConnection -State Listen -LocalPort {PORT} -ErrorAction SilentlyContinue |"
           " ForEach-Object { $_.LocalAddress + ' ' + (Get-Process -Id $_.OwningProcess).ProcessName }")
    rows = [line.strip() for line in r.stdout.splitlines() if line.strip()]
    bad = [row for row in rows if not row.endswith("wslrelay") or not row.startswith(("127.0.0.1", "::1"))]
    return (not bad), f"Windows listeners on {PORT}: {rows or 'none'}"


def main() -> int:
    host = name()
    checks = [
        ("1 WSL bind is loopback only", check_wsl_bind),
        ("2 Windows reaches the WSL listener", check_windows_reaches_wsl),
        ("3 tailscale serve: one handler, no Funnel", lambda: check_serve(host)),
        ("4 HTTPS with a trusted tailnet certificate", lambda: check_https(host)),
        ("5 Windows: only WSL's relay on 8406", check_windows_listeners),
    ]
    failed = 0
    for label, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:  # a spike reports, it does not crash
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}\n      {detail}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed (machine {host})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

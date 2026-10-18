"""SEC-1 acceptance tests: a *name* whose first label is `127` is a DNS name.

Task card: `docs/plans/2026-10-18-security-fix-specs.md`, section SEC-1.
Only *literal* addresses in 127.0.0.0/8 count as this machine. A name such
as `127.rebind.example` or `127.0.0.1.nip.io` is resolved like any other
and refused when it answers with loopback or a private address. Literal
spellings (`127.1`, `0x7f000001`, `2130706433`, `::ffff:127.0.0.1`,
`localhost.`) stay owner-local with no DNS lookup.

Cases (each prints its own name, `ok`/`FAIL`):

1.  `is_loopback` over literals, `*.localhost` and the `127.`-prefixed names.
2.  `is_lan_host("127.rebind.example")` resolves, and refuses private and
    loopback answers.
3.  `is_lan_host` on literal spellings: False, with no lookup.
4.  `web._refusal` and a `fetch_page` redirect hop to `127.rebind.example`.
5.  `127.rebind.example:8402` (resolving to 127.0.0.1) is still refused by
    `web._refusal` and by v1's `*` policy, though `is_face_origin` no longer
    claims it (the card's behaviour change).
6.  v1's `*` allowlist refuses `127.rebind.example`.
7.  The v2 gate: `tests/v2/loopback_names_gate_check.py` (run by case 8 too).
8.  The neighbouring suites still pass (subprocesses; `--skip-suites` skips
    them, `--only-suites` runs only them). They inherit this environment:
    do not point HOME at a temporary directory for this file, or Playwright
    cannot find its Chromium and `tests/browser/policy_check.py` fails
    (each suite makes its own temporary state).

Hermetic: `socket.getaddrinfo` is replaced for every case, and `httpx.get`
is replaced wherever `fetch_page` could reach the network. Nothing touches
8402/8403/8405 or the owner's state.

Run:
    PYTHONPATH=. .venv/bin/python tests/loopback_names_check.py
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from jarvis import config, runtime, tools  # noqa: E402
from jarvis.browser import BrowserPolicy  # noqa: E402
from jarvis.tools import web  # noqa: E402

FAILED: list[str] = []
PASSED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"ok    {name}")
    else:
        FAILED.append(name)
        print(f"FAIL  {name}" + (f"  -- {detail}" if detail else ""))


class FakeDNS:
    """`socket.getaddrinfo` from a table. A name in `table` answers with its
    address list; any other host answers with `default` (a list) when it is
    set, else raises `gaierror`. Every call is recorded."""

    def __init__(self, table=None, default=None):
        self.table = dict(table or {})
        self.default = default
        self.calls: list[str] = []

    def __call__(self, host, *args, **kwargs):
        self.calls.append(host)
        answer = self.table.get(host, self.default)
        if answer is None:
            raise socket.gaierror(socket.EAI_NONAME, f"unresolvable in test: {host}")
        out = []
        for address in answer:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
            out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return out


@contextlib.contextmanager
def dns(table=None, default=None):
    fake = FakeDNS(table, default)
    real = socket.getaddrinfo
    socket.getaddrinfo = fake
    try:
        yield fake
    finally:
        socket.getaddrinfo = real


@contextlib.contextmanager
def no_fetch():
    """`httpx.get` that fails the case if anything is requested."""
    calls: list[str] = []

    def must_not_fetch(url, *args, **kwargs):
        calls.append(str(url))
        raise httpx.ConnectError("the test transport refuses every request")

    real = httpx.get
    httpx.get = must_not_fetch
    try:
        yield calls
    finally:
        httpx.get = real


def _safe(fn, *args):
    """fn(*args), or the exception's repr (so a raise is a FAIL, not a crash)."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        return f"raised {exc!r}"


# -- case 1 -----------------------------------------------------------------------------

def case1_is_loopback() -> None:
    with dns(default=["127.0.0.1"]) as fake:
        for host in ("127.0.0.1", "127.5.6.7", "[::1]", "localhost", "foo.localhost",
                     "0.0.0.0", ""):
            got = _safe(config.is_loopback, host)
            check(f"case1_is_loopback_true[{host!r}]", got is True, f"got {got!r}")
        for host in ("127.rebind.example", "127.0.0.1.nip.io", "127."):
            got = _safe(config.is_loopback, host)
            check(f"case1_is_loopback_false[{host!r}]", got is False, f"got {got!r}")
    check("case1_is_loopback_never_resolves", fake.calls == [], f"getaddrinfo({fake.calls})")


# -- case 2 -----------------------------------------------------------------------------

def case2_is_lan_host_resolves_127_names() -> None:
    for answer in ("192.168.1.10", "127.0.0.1"):
        with dns({"127.rebind.example": [answer]}) as fake:
            got = _safe(config.is_lan_host, "127.rebind.example")
        check(f"case2_is_lan_host_127_rebind_to_{answer}_is_lan", got is True, f"got {got!r}")
        check(f"case2_is_lan_host_127_rebind_to_{answer}_was_resolved",
              "127.rebind.example" in fake.calls, f"getaddrinfo calls: {fake.calls}")
    # The nip.io shape: the whole dotted quad is a prefix of a DNS name.
    with dns({"127.0.0.1.nip.io": ["127.0.0.1"]}) as fake:
        got = _safe(config.is_lan_host, "127.0.0.1.nip.io")
    check("case2_is_lan_host_127_0_0_1_nip_io_is_lan", got is True, f"got {got!r}")
    check("case2_is_lan_host_127_0_0_1_nip_io_was_resolved",
          "127.0.0.1.nip.io" in fake.calls, f"getaddrinfo calls: {fake.calls}")


# -- case 3 -----------------------------------------------------------------------------

def case3_literal_spellings_stay_local_without_dns() -> None:
    # The resolver answers loopback for anything (what libc does for these),
    # so a version that looks them up calls them LAN and fails both halves.
    for host in ("127.1", "0x7f000001", "2130706433", "::ffff:127.0.0.1", "localhost."):
        with dns(default=["127.0.0.1"]) as fake:
            got = _safe(config.is_lan_host, host)
        check(f"case3_is_lan_host_false[{host!r}]", got is False, f"got {got!r}")
        check(f"case3_is_lan_host_no_dns[{host!r}]", fake.calls == [],
              f"getaddrinfo calls: {fake.calls}")


# -- case 4 -----------------------------------------------------------------------------

def case4_fetch_page_refuses_127_name_to_lan() -> None:
    table = {"127.rebind.example": ["192.168.1.10"], "public.example": ["93.184.216.34"]}
    with dns(table) as fake, no_fetch() as fetched:
        got = _safe(web._refusal, "http://127.rebind.example/")
        check("case4_refusal_127_rebind_to_lan_is_lan", got == "lan", f"got {got!r}")
        check("case4_refusal_127_rebind_was_resolved", "127.rebind.example" in fake.calls,
              f"getaddrinfo calls: {fake.calls}")
        out = tools.dispatch("fetch_page", json.dumps({"url": "http://127.rebind.example/"}))
        check("case4_fetch_page_typed_127_rebind_refused",
              "private network" in out.text and fetched == [],
              f"out={out.text[:160]!r} requested={fetched}")

    # A public page that redirects to the name: the hop is judged before it
    # is requested, so the transport sees exactly one request.
    requested: list[str] = []

    def transport(url, *args, **kwargs):
        requested.append(str(url))
        if len(requested) == 1:
            return types.SimpleNamespace(
                url=httpx.URL(str(url)), status_code=302,
                headers={"location": "http://127.rebind.example/admin"}, text="")
        return types.SimpleNamespace(
            url=httpx.URL(str(url)), status_code=200,
            headers={"content-type": "text/html"},
            text="<html><body>router admin</body></html>")

    real = httpx.get
    httpx.get = transport
    try:
        with dns(table):
            out = tools.dispatch("fetch_page", json.dumps({"url": "http://public.example/"}))
    finally:
        httpx.get = real
    check("case4_fetch_page_redirect_hop_to_127_rebind_refused",
          "redirected to" in out.text and "blocked" in out.text, f"out={out.text[:160]!r}")
    check("case4_fetch_page_redirect_hop_never_requested",
          requested == ["http://public.example/"], f"requested={requested}")


# -- case 5 -----------------------------------------------------------------------------

def case5_control_port_by_127_name_still_refused() -> None:
    url = f"http://127.rebind.example:{config.FACE_PORT}/"
    with dns({"127.rebind.example": ["127.0.0.1"]}), no_fetch() as fetched:
        got = _safe(web._refusal, url)
        check("case5_refusal_127_rebind_control_port_not_none", got is not None, f"got {got!r}")
        out = tools.dispatch("fetch_page", json.dumps({"url": url}))
        check("case5_fetch_page_127_rebind_control_port_not_requested",
              out.text.startswith("Error:") and fetched == [],
              f"out={out.text[:160]!r} requested={fetched}")
        allowed = _safe(BrowserPolicy(allowed_hosts=["*"]).allows, url)
        check("case5_v1_star_policy_127_rebind_control_port_refused", allowed is False,
              f"got {allowed!r}")
        allowed = _safe(BrowserPolicy().allows, url)
        check("case5_v1_default_policy_127_rebind_control_port_refused", allowed is False,
              f"got {allowed!r}")
        # The card's behaviour change: the name is no longer a spelling of
        # this machine, so the control-port test does not claim it (the LAN
        # check above is what refuses it now).
        face = _safe(config.is_face_origin, url)
        check("case5_is_face_origin_127_rebind_is_false", face is False, f"got {face!r}")
    # Unchanged: every literal spelling on a control port is still the
    # control plane, with no lookup.
    with dns(default=["127.0.0.1"]) as fake:
        for host in ("127.0.0.1", "127.5.6.7", "127.1", "0x7f000001", "2130706433",
                     "[::ffff:127.0.0.1]", "[::1]", "localhost", "localhost.", "foo.localhost"):
            got = _safe(config.is_face_origin, f"http://{host}:{config.FACE_PORT}/")
            check(f"case5_is_face_origin_literal_true[{host!r}]", got is True, f"got {got!r}")
    check("case5_is_face_origin_literals_never_resolve", fake.calls == [],
          f"getaddrinfo calls: {fake.calls}")


# -- case 6 -----------------------------------------------------------------------------

def case6_v1_star_allowlist_refuses_127_names() -> None:
    for answer in ("192.168.1.10", "127.0.0.1"):
        with dns({"127.rebind.example": [answer], "127.0.0.1.nip.io": [answer]}):
            for host in ("127.rebind.example", "127.0.0.1.nip.io"):
                for label, policy in (("star", BrowserPolicy(allowed_hosts=["*"])),
                                      ("default", BrowserPolicy())):
                    got = _safe(policy.allows, f"http://{host}/")
                    check(f"case6_v1_{label}_policy_refuses[{host}->{answer}]", got is False,
                          f"got {got!r}")
    # Unchanged: a public name still passes '*', and the literal loopback the
    # default allowlist names still passes.
    with dns({"public.example": ["93.184.216.34"]}):
        got = _safe(BrowserPolicy(allowed_hosts=["*"]).allows, "http://public.example/")
        check("case6_v1_star_policy_public_name_allowed", got is True, f"got {got!r}")
        got = _safe(BrowserPolicy().allows, "http://127.0.0.1:8000/")
        check("case6_v1_default_policy_127_0_0_1_dev_port_allowed", got is True, f"got {got!r}")


# -- cases 7 and 8 ------------------------------------------------------------------------

SUITES = [
    "tests/v2/loopback_names_gate_check.py",  # case 7
    "tests/browser/policy_check.py",
    "tests/v2/owner_browser_check.py",
    "tests/v2/devurl_check.py",
    "tests/v2/terminal_check.py",
]


def _summary(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    tail = [line for line in lines[-6:] if line.startswith(("Ran ", "OK", "FAILED", "ok", "PASS",
                                                               "FAIL", "all ", "ALL "))]
    return " | ".join(tail) or (lines[-1] if lines else "(no output)")


def case8_neighbouring_suites() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    for suite in SUITES:
        name = ("case7_v2_gate_suite" if "loopback_names_gate" in suite
                else f"case8_suite_passes[{suite}]")
        try:
            done = subprocess.run([sys.executable, str(ROOT / suite)], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            check(name, False, "timed out after 30 minutes")
            continue
        output = done.stdout + done.stderr
        check(name, done.returncode == 0, f"exit {done.returncode}: {_summary(output)}")
        if done.returncode == 0:
            print(f"      {_summary(output)}")
        else:
            # Name the failing cases, so the reader need not rerun the suite.
            for line in output.splitlines():
                if line.startswith(("FAIL:", "ERROR:")) or " ... FAIL" in line or " ... ERROR" in line:
                    print(f"      {line}")


def main(argv: list[str]) -> int:
    only_suites = "--only-suites" in argv
    if not only_suites:
        for case in (case1_is_loopback, case2_is_lan_host_resolves_127_names,
                     case3_literal_spellings_stay_local_without_dns,
                     case4_fetch_page_refuses_127_name_to_lan,
                     case5_control_port_by_127_name_still_refused,
                     case6_v1_star_allowlist_refuses_127_names):
            try:
                case()
            except Exception as exc:  # noqa: BLE001
                check(f"{case.__name__}_crashed", False, repr(exc))
    if "--skip-suites" not in argv:
        case8_neighbouring_suites()
    print()
    print(f"loopback_names_check: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    # dispatch() refuses with no toolset bound; this suite calls only fetch_page.
    with runtime.tools_bound({"fetch_page"}):
        sys.exit(main(sys.argv[1:]))

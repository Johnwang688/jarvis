"""The step between pasta and bwrap in a networked cell (stdlib only, run with `python -I`).

    systemd-run --scope ... -- pasta <PASTA_FLAGS> -- python -I netcell.py <policy json> -- bwrap ...

pasta has just made a new user and network namespace, brought up its tap
interface and started this as root *of that namespace* (the host's uid, mapped
to 0). Before anything an agent controls runs, this:

1. Installs the egress policy as `ip rule`s (it holds CAP_NET_ADMIN over the
   namespace; the agent, in bwrap's nested user namespace, never does):
   DNS only to pasta's forwarding address on port 53, the blocked ranges
   (private, link-local, CGNAT, multicast, reserved and every address the host
   owns) prohibited, TCP to the allowed ports (443) through, and everything
   else prohibited. IPv6 is prohibited outright.
2. Turns IPv6 off on every interface but `lo`, so a resolver never hands out
   an AAAA answer the cell could only fail on.
3. Reads the rules back and refuses to go on unless every one is in place.
4. Re-creates the credential pipes. pasta closes every inherited descriptor
   but 0-2, so the trimmed logins arrive on stdin first (`<length>\\n` then a
   JSON object of fd -> base64), are written into fresh pipes at the fd
   numbers bwrap's `--ro-bind-data` arguments name, and the rest of stdin is
   left untouched for the agent.
5. Execs bwrap. Any failure before that exits 125 and runs nothing.

It never reads a file, never opens a socket and never prints a credential.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import subprocess
import sys

IP = "/usr/sbin/ip"
IPV6_CONF = "/proc/sys/net/ipv6/conf"   # the namespace's own (procfs follows the netns)
EXIT_REFUSED = 125
MAX_HEADER = 1 << 20


class Refused(RuntimeError):
    pass


def rules(policy: dict) -> list[dict]:
    """The IPv4 rules for a policy, in order. Each is {pref, dst, ipproto, dport, do}
    where `do` is "main" (look up the main table) or "prohibit"."""
    dns = str(ipaddress.IPv4Address(policy["dns"]))
    out = [{"pref": 90, "dst": f"{dns}/32", "ipproto": "udp", "dport": 53, "do": "main"},
           {"pref": 91, "dst": f"{dns}/32", "ipproto": "tcp", "dport": 53, "do": "main"},
           {"pref": 100, "dst": f"{dns}/32", "do": "prohibit"}]
    for block in policy["block"]:
        out.append({"pref": 100, "dst": str(ipaddress.IPv4Network(block, strict=False)), "do": "prohibit"})
    for port in policy["allow_tcp"]:
        port = int(port)
        if not 0 < port < 65536:
            raise Refused(f"bad port {port}")
        out.append({"pref": 110, "ipproto": "tcp", "dport": port, "do": "main"})
    out.append({"pref": 120, "do": "prohibit"})
    return out


def _argv(rule: dict) -> list[str]:
    a = [IP, "-4", "rule", "add", "pref", str(rule["pref"])]
    if rule.get("dst"):
        a += ["to", rule["dst"]]
    if rule.get("ipproto"):
        a += ["ipproto", rule["ipproto"], "dport", str(rule["dport"])]
    a += ["lookup", "main"] if rule["do"] == "main" else ["prohibit"]
    return a


def _key(rule: dict) -> tuple:
    dst = rule.get("dst") or ""
    if dst:
        dst = str(ipaddress.IPv4Network(dst, strict=False))
    return (int(rule["pref"]), dst, rule.get("ipproto") or "", int(rule.get("dport") or 0), rule["do"])


def _installed(family: str) -> set[tuple]:
    done = subprocess.run([IP, "-j", family, "rule", "show"], capture_output=True, text=True, timeout=10)
    if done.returncode:
        raise Refused(f"cannot read back the {family} rules: {done.stderr.strip()[:200]}")
    seen = set()
    for r in json.loads(done.stdout or "[]"):
        dst = r.get("dst") or ""
        if dst and dst != "all":
            dst = f"{dst}/{r.get('dstlen', 32 if family == '-4' else 128)}"
            dst = str(ipaddress.ip_network(dst, strict=False))
        elif dst == "all":
            dst = ""
        do = "prohibit" if r.get("action") == "prohibit" else r.get("table", "")
        seen.add((int(r.get("priority", -1)), dst, r.get("ipproto") or "", int(r.get("dport") or 0), do))
    return seen


def install(policy: dict) -> None:
    want = rules(policy)
    for rule in want:
        done = subprocess.run(_argv(rule), capture_output=True, text=True, timeout=10)
        if done.returncode:
            raise Refused(f"ip rule failed ({' '.join(_argv(rule)[2:])}): {done.stderr.strip()[:200]}")
    done = subprocess.run([IP, "-6", "rule", "add", "pref", "100", "prohibit"], capture_output=True, text=True, timeout=10)
    if done.returncode and "not supported" not in done.stderr.lower():
        raise Refused(f"ip -6 rule failed: {done.stderr.strip()[:200]}")
    conf = IPV6_CONF
    if os.path.isdir(conf):
        for name in sorted(os.listdir(conf)):
            if name in ("all", "lo"):
                continue
            with open(os.path.join(conf, name, "disable_ipv6"), "w") as handle:
                handle.write("1")
    verify(policy)


def verify(policy: dict) -> None:
    """Every wanted rule is installed, and no interface but lo carries IPv6."""
    seen = _installed("-4")
    missing = [r for r in rules(policy) if _key(r) not in seen]
    if missing:
        raise Refused(f"{len(missing)} egress rule(s) are not in place, e.g. {missing[0]}")
    conf = IPV6_CONF
    if os.path.isdir(conf):
        if (100, "", "", 0, "prohibit") not in _installed("-6"):
            raise Refused("the IPv6 prohibit rule is not in place")
        for name in sorted(os.listdir(conf)):
            if name in ("all", "lo"):
                continue
            with open(os.path.join(conf, name, "disable_ipv6")) as handle:
                if handle.read().strip() != "1":
                    raise Refused(f"IPv6 is still on for {name}")


def _read_exact(n: int) -> bytes:
    """Exactly n bytes from fd 0, unbuffered, so the rest stays for the agent."""
    out = b""
    while len(out) < n:
        chunk = os.read(0, n - len(out))
        if not chunk:
            raise Refused("stdin ended inside the credential header")
        out += chunk
    return out


def read_credentials() -> dict[int, bytes]:
    line = b""
    while not line.endswith(b"\n"):
        if len(line) > 16:
            raise Refused("bad credential header")
        c = os.read(0, 1)
        if not c:
            raise Refused("stdin ended before the credential header")
        line += c
    size = int(line.strip())
    if not 0 <= size <= MAX_HEADER:
        raise Refused("bad credential header size")
    doc = json.loads(_read_exact(size) or b"{}")
    return {int(fd): base64.b64decode(data) for fd, data in doc.items()}


def place_credentials(creds: dict[int, bytes]) -> None:
    for fd, data in creds.items():
        if fd < 10:
            raise Refused(f"credential fd {fd} is reserved")
        r, w = os.pipe()
        try:
            os.write(w, data)
        finally:
            os.close(w)
        if r != fd:
            os.dup2(r, fd)
            os.close(r)
        os.set_inheritable(fd, True)


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 4 or argv[2] != "--":
            raise Refused("usage: netcell.py <policy json> -- bwrap ...")
        policy, command = json.loads(argv[1]), argv[3:]
        install(policy)
        place_credentials(read_credentials())
    except (Refused, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"netcell: refused, nothing was started: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return EXIT_REFUSED
    os.execv(command[0], command)
    return EXIT_REFUSED                                # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv))

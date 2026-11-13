"""The step between pasta and bwrap in a networked cell (stdlib only, run with `python -I`).

    systemd-run --scope ... -- pasta <PASTA_FLAGS> -- python -I netcell.py <policy json> -- bwrap ...

pasta has just made a new user and network namespace, brought up its tap
interface and started this as root *of that namespace* (the host's uid, mapped
to 0). Before anything an agent controls runs, this:

1. Installs the egress policy as `ip rule`s (it holds CAP_NET_ADMIN over the
   namespace; the agent, in bwrap's nested user namespace, never does):
   DNS only to pasta's forwarding address on port 53, the blocked ranges
   (private, link-local, CGNAT, multicast, reserved and every address the host
   owns) prohibited, TCP to the allowed ports (443) through (to any public
   address, or, when the policy carries `allow_dst`, only to those IPv4
   CIDRs), and everything else prohibited. IPv6 is prohibited outright.
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


MIN_PREFIX = 16        # the broadest `allow_dst` entry: a /15 or shorter is refused
MAX_ALLOW_ADDRESSES = 1 << 16   # every `allow_dst` entry together: at most one /16's worth

# GitHub's published IPv4 ranges (https://api.github.com/meta, fetched 2026-11-13,
# unauthenticated: the `web`, `api`, `git`, `packages`, `pages` and `importer`
# lists, collapsed with ipaddress.collapse_addresses). An `allow_dst` entry that
# overlaps any of them is refused: an egress allowlist exists to keep a public
# repo's merged fix out of reach, and GitHub is where that fix lives. Refresh
# from the same URL when GitHub publishes new ranges.
GITHUB_V4 = (
    "4.149.173.80/28", "4.158.254.48/28", "4.159.108.160/28", "4.159.197.192/28", "4.163.8.144/28",
    "4.186.157.192/28", "4.187.247.160/28", "4.195.217.80/28", "4.196.32.112/28", "4.207.215.240/28",
    "4.208.26.193/32", "4.208.26.196/31", "4.208.26.198/32", "4.208.26.200/32", "4.209.111.112/28",
    "4.209.112.0/28", "4.225.11.194/32", "4.225.11.196/32", "4.225.11.199/32", "4.225.11.200/31",
    "4.225.187.240/28", "4.225.189.240/28", "4.228.31.144/31", "4.228.31.149/32", "4.228.31.150/32",
    "4.228.31.152/32", "4.234.31.32/28", "4.234.31.128/28", "4.234.47.112/28", "4.236.95.112/28",
    "4.237.22.32/32", "4.237.22.34/32", "4.237.22.36/32", "4.237.22.38/32", "4.237.22.40/32",
    "4.249.131.163/32", "4.249.131.164/32", "4.249.131.166/31", "20.6.7.16/28", "20.26.58.32/28",
    "20.26.68.64/28", "20.26.156.210/31", "20.26.156.213/32", "20.26.156.214/31", "20.27.163.208/28",
    "20.27.177.113/32", "20.27.177.116/30", "20.29.134.17/32", "20.29.134.18/31", "20.29.134.22/31",
    "20.51.87.64/28", "20.87.245.0/31", "20.87.245.2/32", "20.87.245.4/32", "20.87.245.6/32",
    "20.169.109.160/28", "20.172.24.160/28", "20.175.192.146/31", "20.175.192.149/32",
    "20.175.192.150/32", "20.199.39.227/32", "20.199.39.228/32", "20.199.39.231/32",
    "20.199.39.232/32", "20.200.245.241/32", "20.200.245.244/31", "20.200.245.247/32",
    "20.200.245.248/32", "20.205.243.160/31", "20.205.243.164/32", "20.205.243.166/32",
    "20.205.243.168/32", "20.207.73.81/32", "20.207.73.82/31", "20.207.73.85/32", "20.207.73.86/32",
    "20.208.89.80/28", "20.217.135.0/31", "20.217.135.4/31", "20.233.83.145/32", "20.233.83.146/31",
    "20.233.83.148/31", "20.247.166.64/28", "20.250.119.67/32", "20.250.119.68/32", "20.250.119.72/31",
    "40.121.125.32/28", "40.121.125.128/26", "40.121.125.192/27", "48.200.82.112/28",
    "48.202.10.80/28", "48.202.91.176/28", "48.202.248.34/32", "48.202.248.38/31", "48.202.248.40/32",
    "48.204.201.2/32", "48.204.201.5/32", "48.204.201.6/32", "48.204.201.9/32", "52.0.228.224/32",
    "52.22.155.48/32", "52.23.85.212/32", "57.155.223.240/28", "57.166.80.144/28", "74.161.79.192/28",
    "135.235.149.144/28", "140.82.112.0/20", "143.55.64.0/20", "172.130.18.192/28",
    "172.130.45.112/28", "172.130.153.16/28", "172.130.201.80/28", "172.139.64.112/28",
    "172.182.252.130/32", "172.182.252.133/32", "172.182.252.135/32", "172.182.252.136/31",
    "172.184.222.112/28", "172.194.155.208/28", "172.194.218.192/28", "185.199.108.0/22",
    "192.30.252.0/22",
)


def allow_networks(policy: dict) -> list[ipaddress.IPv4Network] | None:
    """The policy's `allow_dst` as normalised, de-duplicated IPv4 networks, or
    None when the policy sets none (TCP to the allowed ports, any destination).
    A set list must be a nonempty list of IPv4 CIDR strings, none broader than
    /MIN_PREFIX, together covering at most MAX_ALLOW_ADDRESSES (their union), and
    none overlapping a blocked range, the DNS forwarder or GitHub's published
    ranges: anything else is refused, so a mistake can never widen the egress or
    silently close it entirely."""
    if policy.get("allow_dst") is None:
        return None
    raw = policy["allow_dst"]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise Refused("allow_dst must be a nonempty list of IPv4 CIDRs")
    nets: list[ipaddress.IPv4Network] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise Refused(f"allow_dst entry {entry!r} is not a string")
        try:
            net = ipaddress.IPv4Network(entry, strict=False)
        except ValueError as exc:
            raise Refused(f"allow_dst entry {entry!r} is not an IPv4 CIDR: {exc}")
        if net.prefixlen < MIN_PREFIX:
            raise Refused(f"allow_dst {net} is broader than /{MIN_PREFIX}: an allowlist names "
                          f"the provider's own ranges, not a slice of the internet")
        if net not in nets:
            nets.append(net)
    total = sum(n.num_addresses for n in ipaddress.collapse_addresses(nets))
    if total > MAX_ALLOW_ADDRESSES:
        raise Refused(f"allow_dst covers {total} addresses in all, more than {MAX_ALLOW_ADDRESSES} (one /16): "
                      f"many narrow entries are as broad as one wide one")
    github = [ipaddress.IPv4Network(g) for g in GITHUB_V4]
    for net in nets:
        hit = next((g for g in github if net.overlaps(g)), None)
        if hit is not None:
            raise Refused(f"allow_dst {net} overlaps GitHub's published range {hit} (where a public fix lives)")
    blocked = [ipaddress.IPv4Network(f"{ipaddress.IPv4Address(policy['dns'])}/32")]
    blocked += [ipaddress.IPv4Network(b, strict=False) for b in policy["block"]]
    for net in nets:
        for block in blocked:
            if net.overlaps(block):
                raise Refused(f"allow_dst {net} overlaps the blocked range {block}")
    return nets


def rules(policy: dict) -> list[dict]:
    """The IPv4 rules for a policy, in order. Each is {pref, dst, ipproto, dport, do}
    where `do` is "main" (look up the main table) or "prohibit". Without
    `allow_dst`, TCP to the allowed ports is open to any destination; with it,
    one rule per (CIDR, port) and nothing else lets TCP out."""
    dns = str(ipaddress.IPv4Address(policy["dns"]))
    allowed = allow_networks(policy)
    out = [{"pref": 90, "dst": f"{dns}/32", "ipproto": "udp", "dport": 53, "do": "main"},
           {"pref": 91, "dst": f"{dns}/32", "ipproto": "tcp", "dport": 53, "do": "main"},
           {"pref": 100, "dst": f"{dns}/32", "do": "prohibit"}]
    for block in policy["block"]:
        out.append({"pref": 100, "dst": str(ipaddress.IPv4Network(block, strict=False)), "do": "prohibit"})
    for net in (allowed if allowed is not None else [None]):
        for port in policy["allow_tcp"]:
            port = int(port)
            if not 0 < port < 65536:
                raise Refused(f"bad port {port}")
            rule = {"pref": 110, "ipproto": "tcp", "dport": port, "do": "main"}
            if net is not None:
                rule["dst"] = str(net)
            out.append(rule)
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


# The kernel's own rules in a fresh namespace, which the read-back expects beside ours.
DEFAULTS_V4 = ((0, "", "", 0, "local"), (32766, "", "", 0, "main"), (32767, "", "", 0, "default"))
DEFAULTS_V6 = ((0, "", "", 0, "local"), (32766, "", "", 0, "main"))
V6_PROHIBIT = (100, "", "", 0, "prohibit")


def _installed(family: str) -> list[tuple]:
    done = subprocess.run([IP, "-j", family, "rule", "show"], capture_output=True, text=True, timeout=10)
    if done.returncode:
        raise Refused(f"cannot read back the {family} rules: {done.stderr.strip()[:200]}")
    seen = []
    for r in json.loads(done.stdout or "[]"):
        dst = r.get("dst") or ""
        if dst and dst != "all":
            dst = f"{dst}/{r.get('dstlen', 32 if family == '-4' else 128)}"
            dst = str(ipaddress.ip_network(dst, strict=False))
        elif dst == "all":
            dst = ""
        do = "prohibit" if r.get("action") == "prohibit" else r.get("table", "")
        if set(r) - {"priority", "src", "dst", "dstlen", "ipproto", "dport", "table", "action"} or r.get("src", "all") != "all":
            do = f"unexpected:{json.dumps(r, sort_keys=True)}"        # a selector we never set: never matches
        seen.append((int(r.get("priority", -1)), dst, r.get("ipproto") or "", int(r.get("dport") or 0), do))
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
    """The rules are exactly ours plus the kernel's defaults (nothing extra,
    nothing duplicated, nothing ahead of ours), and no interface but lo
    carries IPv6."""
    want = sorted([_key(r) for r in rules(policy)] + list(DEFAULTS_V4))
    seen = sorted(_installed("-4"))
    if seen != want:
        extra = [r for r in seen if r not in want]
        missing = [r for r in want if r not in seen]
        raise Refused(f"the IPv4 rules are not exactly the policy: missing {missing[:2]}, extra {extra[:2]}")
    conf = IPV6_CONF
    if os.path.isdir(conf):
        if sorted(_installed("-6")) != sorted([V6_PROHIBIT, *DEFAULTS_V6]):
            raise Refused("the IPv6 rules are not exactly one prohibit plus the kernel's defaults")
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
    return {int(fd): base64.b64decode(data, validate=True) for fd, data in doc.items()}


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

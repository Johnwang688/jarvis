"""Free checks for `jarvis.v2.devlisten` (AO-1a; plan
`docs/plans/2026-10-12-hud-browser-plan-amendment-2.md` §B, §D.4 and §H's
AO-1a table): the `/proc/net` rows parser, the port and address classes, the
facts readers, the origin-tag finder, `attribute()`, the probe parser and
`arbitrate()`.

Everything runs over fixture `/proc` trees in a temp dir. The one exception
is a read-only parse of the real `/proc/net/tcp`/`tcp6` (and this process's
own cgroup and port range) to hold the parser to the real format; no other
process's `environ` is ever read from the real `/proc`. Work is counted, not
timed. A fake secret in a fixture `environ` must appear in no return value,
exception, log record or counter.

Run: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/devlisten_check.py
"""
from __future__ import annotations

import builtins
import dataclasses
import logging
import os
import re
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jarvis import config  # noqa: E402
from jarvis.v2 import devlisten as dl  # noqa: E402
from jarvis.v2.model import ProviderName  # noqa: E402

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(("ok   " if condition else "FAIL ") + label)
    if not condition:
        _failures.append(label)


# A fake credential: it must never surface anywhere the module hands back.
SECRET = b"sk-or-v1-FAKE" + b"0123456789abcdef" * 4
T1 = "a" * 8 + "0123456789abcdef" + "b" * 8          # 32 lowercase hex
T2 = "c" * 32
T3 = "d" * 32
T4 = "e" * 32
UID = os.getuid()
TCK = 100


# -- every path the module touches ------------------------------------------------

TOUCHED: list[str] = []
_REAL: dict[str, object] = {}


def _monitor_install(fixture_root: str) -> None:
    module = dl.__file__

    def wrap(name, real):
        def inner(*a, **k):
            path = a[0] if a else k.get("path", k.get("file"))
            # Only what the module itself touches (not this suite's fixture writes).
            if (isinstance(path, (str, bytes, os.PathLike))
                    and sys._getframe(1).f_code.co_filename == module):
                text = os.fsdecode(os.fspath(path))
                if text.startswith(fixture_root) or text.startswith("/proc"):
                    TOUCHED.append(text)
            return real(*a, **k)
        return inner

    for name in ("open", "readlink", "listdir", "stat", "lstat", "scandir"):
        _REAL[name] = getattr(os, name)
        setattr(os, name, wrap(name, _REAL[name]))
    _REAL["builtins.open"] = builtins.open
    builtins.open = wrap("builtins.open", builtins.open)


def _monitor_remove() -> None:
    for name, real in _REAL.items():
        if name == "builtins.open":
            builtins.open = real
        else:
            setattr(os, name, real)


# -- fixture /proc trees ----------------------------------------------------------

HEADER4 = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
           "retrnsmt   uid  timeout inode\n")
HEADER6 = ("  sl  local_address                         remote_address"
           "                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")


def hex4(ip: str) -> str:
    return "%08X" % int.from_bytes(socket.inet_aton(ip), sys.byteorder)


def hex6(ip: str) -> str:
    raw = socket.inet_pton(socket.AF_INET6, ip)
    return "".join("%08X" % int.from_bytes(raw[i:i + 4], sys.byteorder) for i in range(0, 16, 4))


def line(sl: int, local: str, port: int, inode: int, state: str = "0A", uid: int = UID) -> str:
    rem = "0" * len(local)
    return (f"{sl:4d}: {local}:{port:04X} {rem}:0000 {state} 00000000:00000000 00:00000000 "
            f"00000000 {uid:5d}        0 {inode} 1 0000000000000000 100 0 0 10 0\n")


def stat_text(pid: int, comm: str, state: str, ppid: int, sid: int, start: int) -> str:
    rest = [state, ppid, pid, sid, 0, -1, 4194304, 0, 0, 0, 0, 0, 0, 0, 0, 20, 0, 1, 0, start]
    rest += [0] * 32
    return f"{pid} ({comm}) " + " ".join(str(x) for x in rest) + "\n"


class Fake:
    """A fixture `/proc` (and cgroup root) in a temp dir."""

    def __init__(self, base: Path):
        self.base = base
        self.proc = base / "proc"
        self.cg = base / "cg"
        (self.proc / "net").mkdir(parents=True)
        (self.proc / "self").mkdir()
        self.cg.mkdir()
        self.net([], [])

    def net(self, rows4, rows6) -> None:
        (self.proc / "net" / "tcp").write_text(HEADER4 + "".join(rows4))
        (self.proc / "net" / "tcp6").write_text(HEADER6 + "".join(rows6))

    def process(self, pid: int, *, ppid: int = 1, sid: int | None = None, start: int,
                state: str = "S", comm: str = "python3", environ: bytes | None = b"",
                fds=(), cwd: str = "/tmp") -> Path:
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(stat_text(pid, comm, state, ppid, pid if sid is None else sid, start))
        if environ is not None:
            (d / "environ").write_bytes(environ)
        # Present so a read would succeed: the monitor proves none happens.
        (d / "cmdline").write_bytes(b"SENTINEL-CMDLINE\0")
        if not (d / "cwd").is_symlink():
            os.symlink(cwd, d / "cwd")
        fd_dir = d / "fd"
        fd_dir.mkdir(exist_ok=True)
        for n, target in enumerate(fds, start=3):
            os.symlink(target, fd_dir / str(n))
        return d

    def restart(self, pid: int, start: int) -> None:
        text = (self.proc / str(pid) / "stat").read_text().split()
        text[21] = str(start)
        (self.proc / str(pid) / "stat").write_text(" ".join(text) + "\n")

    def cgroup(self, path: str, pids) -> None:
        d = self.cg / path.strip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))
        (self.proc / "self" / "cgroup").write_text(f"0::{path}\n")

    def reader(self) -> dl.ProcReader:
        return dl.ProcReader(self.proc, self.cg, clk_tck=TCK)


def chain(fake: Fake, base: int, depth: int, *, top_ppid: int = 1, start0: int = 1000,
          environ: bytes = b"") -> None:
    """Pids `base` (the top, parent `top_ppid`) to `base + depth` (the deepest),
    each its own session leader and one tick younger than its parent."""
    for i in range(depth + 1):
        fake.process(base + i, ppid=(base + i - 1) if i else top_ppid, start=start0 + i, environ=environ)


def env(*entries: bytes) -> bytes:
    return b"".join(e + b"\0" for e in entries)


def tagged(tag: str) -> bytes:
    return env(b"PATH=/usr/bin", b"OPENROUTER_API_KEY=" + SECRET,
               b"JARVIS_ORIGIN_TAG=" + tag.encode(), b"LANG=\xff\xfe\x80")


KNOWN_PORTS = dl.port_rules(hud_port=18402, api_port=18405, workshop_port=18403,
                            gate_port=18409, bound=(18410,))


def world(**over):
    """Sessions: th-1 (tag T1) with a running Claude turn from 1000 s; th-2
    (tag T2, task tk-2) with no turn. Known: Jarvis's pids 100 and 101, one
    terminal led by 600 (start 60000 ticks)."""
    sessions = dl.Sessions()
    sessions.open(T1, "th-1", at=999.0)
    sessions.open(T2, "th-2", "tk-2", at=999.0)
    sessions.turn_started("th-1", "claude", at=1000.0)
    known = dl.Known(ports=over.get("ports", KNOWN_PORTS), uid=over.get("uid", UID),
                     jarvis_pids=frozenset({100, 101, 102}),
                     terminal_leaders=frozenset({(600, 60000)}))
    return sessions, known


def listener(port: int = 5173, inode: int = 777001, *, family: int = 4, bind: str = dl.LOOPBACK,
             first: float = 1002.0, last: float = 1005.0, mapped: bool = False) -> dl.Listener:
    return dl.Listener(dl.Row(family, bind, port, inode, UID, mapped), first, last)


PAGE = dl.Probe(None, 2, True, True, True)


class Counting:
    def __init__(self, reader):
        self.reader, self.calls = reader, 0

    def __call__(self, facts):
        self.calls += 1
        return self.reader.tag(facts)


# -- the rows parser --------------------------------------------------------------

def rows_checks(fake: Fake) -> None:
    print("\n-- /proc/net rows")
    # The real format, little-endian words (as the kernel printed them here).
    real4 = ("   1: 0100007F:20D7 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000"
             "        0 194389 1 0000000000000000 100 0 0 10 0\n").encode()
    real6 = ("   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A "
             "00000000:00000000 00:00000000 00000000  1000        0 55501 1 0000000000000000 100 0 0 10 0\n"
             "   1: 0000000000000000FFFF00000100007F:1F91 00000000000000000000000000000000:0000 0A "
             "00000000:00000000 00:00000000 00000000  1000        0 55502 1 0000000000000000 100 0 0 10 0\n"
             ).encode()
    if sys.byteorder == "little":
        r4 = dl.parse_rows(real4, 4)
        check(r4 == [dl.Row(4, dl.LOOPBACK, 8407, 194389, 1000)],
              "a real tcp line: 0100007F:20D7 is 127.0.0.1:8407, LISTEN, inode 194389")
        r6 = dl.parse_rows(real6, 6)
        check([(r.bind, r.port, r.mapped) for r in r6]
              == [(dl.LOOPBACK, 8080, False), (dl.LOOPBACK, 8081, True)],
              "real tcp6 words: ...01000000 is ::1 and ...FFFF00000100007F is ::ffff:127.0.0.1")
    else:
        print("note: not little-endian; the literal kernel lines are skipped")

    rows4 = [
        line(0, hex4("127.0.0.1"), 5173, 11),
        line(1, hex4("0.0.0.0"), 3000, 12),
        line(2, hex4("127.0.0.5"), 4000, 13),
        line(3, hex4("192.168.1.20"), 4001, 14),
        line(4, hex4("172.17.0.1"), 4002, 15),
        line(5, hex4("10.0.0.3"), 4003, 16),
        line(6, hex4("127.0.0.1"), 5174, 17, state="01"),      # ESTABLISHED
        line(7, hex4("127.0.0.1"), 5175, 18, state="06"),      # TIME_WAIT
        line(8, hex4("127.0.0.1"), 5176, 19, state="07"),      # CLOSE
    ]
    rows6 = [
        line(0, hex6("::1"), 8000, 21),
        line(1, hex6("::"), 8001, 22),
        line(2, hex6("::ffff:127.0.0.1"), 8002, 23),
        line(3, hex6("::ffff:127.0.0.5"), 8003, 24),
        line(4, hex6("::ffff:0.0.0.0"), 8004, 25),
        line(5, hex6("::ffff:192.168.1.2"), 8005, 26),
        line(6, hex6("fe80::1"), 8006, 27),
        line(7, hex6("2001:db8::1"), 8007, 28),
        line(8, hex6("::1"), 8008, 29, state="01"),
    ]
    fake.net(rows4, rows6)
    reader = fake.reader()
    rows = {(r.family, r.port): r for r in reader.net_rows()}
    check(set(rows) == {(4, 5173), (4, 3000), (4, 4000), (6, 8000), (6, 8001), (6, 8002), (6, 8003)},
          "only LISTEN rows on 127/8, 0.0.0.0, ::1, :: and ::ffff:127/8 are kept")
    check(rows[(4, 5173)].bind == dl.LOOPBACK and rows[(4, 3000)].bind == dl.WILDCARD
          and rows[(6, 8000)].bind == dl.LOOPBACK and rows[(6, 8001)].bind == dl.WILDCARD
          and rows[(6, 8002)].bind == dl.LOOPBACK and rows[(6, 8002)].mapped,
          "127.0.0.1, 0.0.0.0, ::1, :: and ::ffff:127.0.0.1 are loopback or wildcard")
    check(rows[(4, 4000)].bind == dl.ODD_LOOPBACK and rows[(6, 8003)].bind == dl.ODD_LOOPBACK,
          "127.0.0.5 and ::ffff:127.0.0.5 are odd_loopback")
    check(not {(4, 5174), (4, 5175), (4, 5176), (6, 8008)} & set(rows),
          "ESTABLISHED, TIME_WAIT and CLOSE rows are not counted")
    check(not {(4, 4001), (4, 4002), (4, 4003), (6, 8004), (6, 8005), (6, 8006), (6, 8007)} & set(rows),
          "192.168, 172.x, 10.x, ::ffff:0.0.0.0, mapped LAN, fe80:: and global binds are skipped")
    check(rows[(4, 5173)].inode == 11 and rows[(4, 5173)].uid == UID, "inode and uid are read")
    check(reader.work.files == 2 and reader.work.lines == 2 + 18 and reader.work.rows == 14,
          f"two files; every line looked at, only the 14 LISTEN lines split (work {reader.work.as_dict()})")

    junk = (HEADER4 + "garbage\n" + "   9: 0100007F 00000000:0000 0A x\n"
            + "  10: 0100007G:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 99 1\n"
            + "  11: 0100007F:1F9 00000000:0000 0A 0:0 0:0 0 1000 0 99 1\n"
            + "  12: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 9x9 1\n"
            + "  13: 0100007F:0000 00000000:0000 0A 0:0 0:0 0 1000 0 99 1\n"
            + "  14: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 0 1\n"
            + "  15: 0x00007F:1F90 00000000:0000 0A 0:0 0:0 0 1000 0 99 1\n").encode()
    try:
        out = dl.parse_rows(junk, 4)
        check(out == [], "malformed lines (short, bad hex, bad port, bad inode, port 0, inode 0) are skipped")
    except Exception as exc:  # noqa: BLE001
        check(False, f"malformed lines raised {type(exc).__name__}")

    # The real tables, read-only: the parser keeps exactly the LISTEN lines it
    # should, by an independent count.
    real = dl.ProcReader()
    try:
        rows_real = real.net_rows()
        raw = b""
        for name in ("tcp", "tcp6"):
            try:
                with open(f"/proc/net/{name}", "rb") as fh:
                    raw += fh.read()
            except OSError:
                pass
        listen = sum(1 for ln in raw.split(b"\n") if len(ln.split()) > 3 and ln.split()[3] == b"0A")
        check(real.work.rows == listen and all(0 < r.port < 65536 for r in rows_real),
              f"the real /proc/net tables parse: {real.work.rows} LISTEN rows split, {len(rows_real)} kept")
        check(real.own_cgroup() is not None and real.own_cgroup().startswith("/"),
              "this process's own cgroup reads as a v2 path")
        low, high = real.ephemeral()
        check(1 <= low <= high <= 65535, "the real ephemeral range reads")
    except Exception as exc:  # noqa: BLE001
        check(False, f"reading the real tables raised {type(exc).__name__}")


# -- ports -------------------------------------------------------------------------

def port_checks(fake: Fake) -> None:
    print("\n-- ports: rules 1 and 2 before anything else")
    ports = KNOWN_PORTS
    jarvis = set(config.control_ports()) | {8403, 18402, 18403, 18405, 18409, 18410}
    check(jarvis <= ports.jarvis,
          "refused_ports, the workshop (default and this daemon's), the HUD and API ports, "
          "the gate's port and the daemon's bound ports are Jarvis's")
    check(ports.service == {9222, 9229, 5678, 5432, 3306, 6379, 27017}, "the seven service ports")
    shapes = [(4, dl.LOOPBACK, False), (4, dl.WILDCARD, False), (6, dl.LOOPBACK, False),
              (6, dl.WILDCARD, False), (6, dl.LOOPBACK, True)]
    sessions, known = world()
    reader = fake.reader()
    calls = Counting(reader)
    fails = []
    for port in sorted(jarvis | set(ports.service)):
        want = dl.JARVIS_PORT if port in jarvis else dl.SERVICE_PORT
        for family, bind, mapped in shapes:
            row = dl.Row(family, bind, port, 5, UID, mapped)
            if dl.cheap_refusal(row, ports) != want:
                fails.append((port, family, bind))
            # Facts that would pass every other rule, and none at all (rule 3):
            # the port still decides first, and nothing is read.
            for facts in ([], [_passing_facts()]):
                v = attribute(dl.Listener(row, 1002.0, 1005.0), facts, sessions, 1006.0, known,
                                 tag_of=calls, probe=PAGE)
                if v.refusal != want:
                    fails.append((port, family, bind, "attribute", v.refusal))
            if dl.probe_address(row, ports) is not None:
                fails.append((port, family, bind, "probe address"))
    check(not fails, f"Jarvis's and the service ports are refused in every family, first ({fails[:3]})")
    check(calls.calls == 0 and reader.work.environ_reads == 0 and reader.work.files == 0,
          "a refused port reads no environ and no file")
    odd = dl.Row(4, dl.ODD_LOOPBACK, 5173, 5, UID)
    v = attribute(dl.Listener(odd, 1002.0, 1005.0), [_passing_facts()], sessions, 1006.0, known,
                     tag_of=calls, probe=PAGE)
    check(v.refusal == dl.ODD_LOOPBACK and calls.calls == 0, "a 127.0.0.5 bind is odd_loopback, unread")

    print("\n-- the ephemeral range")
    rng = fake.proc / "sys" / "net" / "ipv4"
    rng.mkdir(parents=True, exist_ok=True)
    (rng / "ip_local_port_range").write_text("20000\t20100\n")
    low, high = fake.reader().ephemeral()
    custom = dl.PortRules(frozenset(), ephemeral=(low, high))
    check((low, high) == (20000, 20100), "the range is read from the file")
    check(all(dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, p, 5, UID), custom) == dl.EPHEMERAL
              for p in (20000, 20050, 20100))
          and all(dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, p, 5, UID), custom) is None
                  for p in (19999, 20101, 40000)),
          "its ends are inclusive, and a port outside it passes (40000 is not ephemeral here)")
    (rng / "ip_local_port_range").unlink()
    check(fake.reader().ephemeral() == (32768, 60999), "no file: the 32768-60999 fallback")
    for bad in (b"abc", b"5 3", b"0 10", b"1 70000", b"1", b""):
        if dl.parse_ephemeral(bad) != (32768, 60999):
            check(False, f"a malformed range {bad!r} falls back")
            break
    else:
        check(True, "a malformed range falls back")
    fall = dl.PortRules(frozenset())
    check(dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, 32768, 5, UID), fall) == dl.EPHEMERAL
          and dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, 60999, 5, UID), fall) == dl.EPHEMERAL
          and dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, 32767, 5, UID), fall) is None
          and dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, 61000, 5, UID), fall) is None,
          "the fallback's ends are inclusive")



def attribute(*args, **kwargs):
    """`dl.attribute` with a complete fd scan unless a check says otherwise:
    `scan_complete` is required, so a caller that forgets the scan's answer fails."""
    kwargs.setdefault("scan_complete", True)
    return dl.attribute(*args, **kwargs)

def _passing_facts() -> dl.PidFacts:
    return dl.PidFacts(4242, 100100, 1001.0, 1, 4242, UID, 100100, ())


# -- attribution -------------------------------------------------------------------

def attribution_checks(fake: Fake) -> None:
    print("\n-- attribute(): the tag is required, there is no folder rule")
    sessions, known = world()
    fake.process(4242, start=100100, environ=tagged(T1), fds=["socket:[777001]"], cwd="/tmp")
    reader = fake.reader()
    facts = reader.facts(4242)
    check(facts is not None and facts.started_at == 1001.0 and facts.uid == UID
          and facts.sid == 4242 and facts.leader_start == 100100,
          "facts: start in seconds since boot, uid, session and leader start")
    v = attribute(listener(), [facts], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.thread_id == "th-1" and v.task_id is None and v.pid == 4242,
          "a tagged process in th-1's turn is attributed to th-1")

    # The tag decides, wherever the process works: its cwd and its parent's are /tmp.
    fake.process(4300, ppid=1, start=100050, cwd="/tmp")
    fake.process(4301, ppid=4300, sid=4300, start=100150, environ=tagged(T1), cwd="/tmp")
    f = reader.facts(4301)
    v = attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.thread_id == "th-1" and f.ancestors == ((4300, 100050),),
          "no folder rule: a tagged process working in /tmp, its parent too, is attributed")

    for pid, name, environ in (
            (4400, "an empty environment (env -i)", b""),
            (4401, "an environment without the tag", env(b"PATH=/usr/bin", b"OPENROUTER_API_KEY=" + SECRET))):
        fake.process(pid, start=100100, environ=environ, cwd="/home/project")
        f = reader.facts(pid)
        v = attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
        check(v.refusal == dl.UNTAGGED, f"tag required: {name} is untagged whatever its folder and turn")

    print("\n-- rule 7: the turn window")
    cases = [
        ("a process started before the turn (990 s), bound inside it", 99000, 1002.0, dl.NO_TURN),
        ("a process started 1.5 s before the turn", 99850, 1002.0, None),
        ("a process started 2 s before the turn (the slack's end)", 99800, 1002.0, None),
    ]
    for label, start, first, want in cases:
        f = dataclasses.replace(_passing_facts(), start=start, started_at=start / TCK, leader_start=start)
        v = attribute(listener(first=first, last=first + 3), [f], sessions, first + 4, known,
                         tag_of=lambda _f: T1.encode(), probe=PAGE)
        check((v.refusal == want) if want else v.attributed, label)
    ended, _k = world()
    ended.turn_ended("th-1", at=1100.0)
    f = _passing_facts()
    v = attribute(listener(first=1700.0, last=1703.0), [f], ended, 1704.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.attributed, "first seen at end + 600 s: inside the grace")
    v = attribute(listener(first=1701.0, last=1704.0), [f], ended, 1705.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "first seen at end + 601 s: no_turn")
    v = attribute(listener(first=999.0, last=1003.0), [f], sessions, 1004.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "first seen before the turn started: no_turn")
    fast = dl.Sessions()
    fast.open(T3, "th-3", at=999.0)
    check(fast.turn_started("th-3", "fast", at=1000.0) is False
          and fast.turn_started("th-3", ProviderName.FAST, at=1000.0) is False,
          "a fast-path turn opens no window")
    v = attribute(listener(), [f], fast, 1006.0, known, tag_of=lambda _f: T3.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "a tagged process during a fast-path turn is no_turn")
    codex = dl.Sessions()
    codex.open(T3, "th-3", at=999.0)
    check(codex.turn_started("th-3", ProviderName.CODEX, at=1000.0) is True, "a Codex turn opens one")
    v = attribute(listener(), [f], codex, 1006.0, known, tag_of=lambda _f: T3.encode(), probe=PAGE)
    check(v.attributed and v.thread_id == "th-3", "a tagged process in a Codex turn is attributed")

    print("\n-- rule 9: the tag must name a live session whose window holds it")
    v = attribute(listener(), [f], sessions, 1006.0, known, tag_of=lambda _f: T2.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "a tag naming another thread (th-2, no turn) is stale_tag")
    v = attribute(listener(), [f], sessions, 1006.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "a tag naming no session is stale_tag")
    closing, _k = world()
    closing.open(T4, "th-1", at=999.0)
    closing.close(T4, at=1001.0)
    v = attribute(listener(), [f], closing, 1500.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.attributed, "a closed session inside its grace still attributes")
    # Rule 9 is judged at the first sighting (closed at 1001; the grace ends at 1601).
    v = attribute(listener(first=1601.0, last=1604.0), [f], closing, 1605.0, known,
                     tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.attributed, "first seen exactly at the close + 600 s: the session is still inside its grace")
    v = attribute(listener(first=1602.0, last=1605.0), [f], closing, 1606.0, known,
                     tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "first seen after the session's grace: stale_tag")
    pend = listener(first=1002.0, last=1004.0)
    v1 = attribute(pend, [f], closing, 1005.0, known, tag_of=lambda _f: T4.encode())
    v2 = attribute(pend, [f], closing, 1700.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v1.pending == "probe" and v2.attributed,
          "a verdict waiting on its probe does not turn stale_tag as the clock passes the grace (judged at first_seen)")
    two = dl.Sessions()
    two.open(T2, "th-2", "tk-2", at=999.0)
    two.turn_started("th-2", "codex", at=1000.0)
    v = attribute(listener(), [f], two, 1006.0, known, tag_of=lambda _f: T2.encode(), probe=PAGE)
    check(v.attributed and (v.thread_id, v.task_id) == ("th-2", "tk-2"), "a task's session carries its task id")

    print("\n-- rules 3-6")
    v = attribute(listener(), [], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.OUTSIDE, "no owning pid in the daemon's cgroup: outside")
    v = attribute(listener(), [_passing_facts()], sessions, 1006.0, dataclasses.replace(known, uid=UID + 1),
                     tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.UID, "another uid: uid")
    before = reader.work.kinds["environ"]
    for pid in (100, 101, 102):
        fake.process(pid, start=100100, environ=tagged(T1))
        v = attribute(listener(), [reader.facts(pid)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
        check(v.refusal == dl.JARVIS, f"pid {pid} (the daemon, a provider root, Chrome) is jarvis")
    check(reader.work.kinds["environ"] == before, "Jarvis's pids are refused before their environ is read")

    # Terminal 600 (start 60000 ticks).
    fake.process(600, start=60000, comm="bash")
    fake.process(4500, sid=600, start=100100, environ=tagged(T1))
    v = attribute(listener(), [reader.facts(4500)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a process in a live terminal's session is terminal")
    fake.process(4501, ppid=600, sid=4501, start=100100, environ=tagged(T1))
    v = attribute(listener(), [reader.facts(4501)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a setsid child of a terminal leader is terminal")
    fake.process(4502, sid=650, start=100100, environ=tagged(T1))
    known_650 = dataclasses.replace(known, terminal_leaders=frozenset({(650, 65000)}))
    v = attribute(listener(), [reader.facts(4502)], sessions, 1006.0, known_650, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a terminal session whose leader exited still counts")
    fake.process(700, start=99000, comm="sh")                  # number 700 reused
    fake.process(4503, sid=700, start=100100, environ=tagged(T1))
    known_700 = dataclasses.replace(known, terminal_leaders=frozenset({(700, 50000)}))
    f503 = reader.facts(4503)
    v = attribute(listener(), [f503], sessions, 1006.0, known_700, tag_of=reader.tag, probe=PAGE)
    check(f503.leader_start == 99000 and v.attributed,
          "a reused session number with another leader start is not a terminal")
    fake.process(4504, ppid=700, sid=4504, start=100100, environ=tagged(T1))
    v = attribute(listener(), [reader.facts(4504)], sessions, 1006.0, known_700, tag_of=reader.tag, probe=PAGE)
    check(v.attributed, "an ancestor whose number was reused is not the terminal's leader")

    print("\n-- several pids on one inode")
    fake.process(4600, start=100100, environ=tagged(T1))
    fake.process(4601, start=100200, environ=tagged(T1))
    fs = [reader.facts(4601), reader.facts(4600)]
    v = attribute(listener(), fs, sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4600, "the earliest-started passing pid owns it")
    fake.process(4602, start=100300, environ=env(b"A=1"))      # untagged, later
    v = attribute(listener(), [reader.facts(4602), reader.facts(4600)], sessions, 1006.0, known,
                     tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4600, "a later pid with no tag is no obstacle: the earliest owns it")
    fake.process(4603, start=99000, environ=tagged(T1))         # started before the turn: no_turn
    v = attribute(listener(), [reader.facts(4603), reader.facts(4600)], sessions, 1006.0, known,
                     tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4600, "a holder outside the turn window is passed over (no Jarvis or terminal)")

    print("\n-- a socket shared with a Jarvis or terminal holder fails closed")
    fake.process(102, start=90000, environ=tagged(T1))         # Jarvis's own, started first
    fake.process(4900, ppid=102, sid=4900, start=100100, environ=tagged(T1))    # a tagged child
    jarvis_first = [reader.facts(102), reader.facts(4900)]
    calls = Counting(reader)
    v = attribute(listener(), jarvis_first, sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.JARVIS and not v.attributed and calls.calls == 0,
          "a Jarvis pid first and a tagged child sharing the socket: jarvis, and no environ read")
    v = attribute(listener(), list(reversed(jarvis_first)), sessions, 1006.0, known,
                     tag_of=calls, probe=PAGE)
    check(v.refusal == dl.JARVIS, "the order the facts arrive in does not matter")
    fake.process(101, start=100400, environ=tagged(T1))        # Jarvis's own, started AFTER the agent
    v = attribute(listener(), [reader.facts(4900), reader.facts(101)], sessions, 1006.0, known,
                     tag_of=calls, probe=PAGE)
    check(v.refusal == dl.JARVIS and calls.calls == 0,
          "a Jarvis pid that started after the tagged holder still refuses the listener")
    fake.process(600, start=60000, comm="bash")
    fake.process(4910, sid=600, start=100050, environ=env(b"A=1"))             # a terminal's process, first
    fake.process(4911, sid=4911, start=100200, environ=tagged(T1))             # an agent's tagged process
    term_first = [reader.facts(4910), reader.facts(4911)]
    v = attribute(listener(), term_first, sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.TERMINAL and not v.attributed and calls.calls == 0,
          "a terminal's process first and a tagged agent process sharing the socket: terminal")
    v = attribute(listener(), list(reversed(term_first)), sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a terminal holder refuses in either order")
    fake.process(4912, ppid=600, sid=4912, start=100500, environ=env(b"A=1"))  # a setsid child of the leader, later
    v = attribute(listener(), [reader.facts(4600), reader.facts(4912)], sessions, 1006.0, known,
                     tag_of=calls, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a terminal leader's child that started after the owner still refuses it")
    v = attribute(listener(), [reader.facts(4600), reader.facts(102), reader.facts(4910)], sessions, 1006.0,
                     known, tag_of=calls, probe=PAGE)
    check(v.refusal in (dl.JARVIS, dl.TERMINAL), "with both kinds of holder, one of the two classes")
    other_uid = dataclasses.replace(reader.facts(4600), pid=4605, uid=UID + 1)
    v = attribute(listener(), [other_uid, reader.facts(4601)], sessions, 1006.0, known,
                     tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4601, "a holder of another uid is not Jarvis's or a terminal's: it is passed over")
    calls = Counting(reader)
    v = attribute(listener(), [None, reader.facts(4600)], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.UNREADABLE and not v.attributed and calls.calls == 0,
          "an unreadable co-holder beside a tagged agent refuses the listener (it could be a terminal's)")
    try:
        dl.attribute(listener(), [reader.facts(4600)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
        check(False, "scan_complete is required")
    except TypeError:
        check(True, "scan_complete is required: a caller that forgets the fd scan's answer fails")
    v = attribute(listener(), [None, reader.facts(102)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.JARVIS, "with none passing, the earliest readable pid's refusal is reported")
    v = attribute(listener(), [None], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.UNREADABLE, "a lone unreadable holder is unreadable")

    print("\n-- rules 10 and 11 wait")
    v = attribute(listener(first=1002.0, last=1003.5), [facts], sessions, 1004.0, known, tag_of=reader.tag)
    check(v.outcome == "pending" and v.pending == "life", "seen for under 2 s: pending life")
    v = attribute(listener(first=1002.0, last=1004.0), [facts], sessions, 1005.0, known, tag_of=reader.tag)
    check(v.outcome == "pending" and v.pending == "probe", "two sightings 2 s apart, not yet probed: pending probe")
    for refusal in (dl.NOT_HTTP, dl.NOT_PAGE, dl.NO_FRAME):
        v = attribute(listener(), [facts], sessions, 1006.0, known, tag_of=reader.tag,
                         probe=dl.Probe(refusal))
        check(v.refusal == refusal, f"a probe answering {refusal} refuses")


def fail_closed_checks(fake: Fake) -> None:
    print("\n-- facts fail closed")
    sessions, known = world()
    reader = fake.reader()
    euid_root = os.geteuid() == 0
    fake.process(4700, start=100100, environ=tagged(T1))
    f = reader.facts(4700)
    os.chmod(fake.proc / "4700" / "environ", 0)
    try:
        if euid_root:
            print("note: running as root, EACCES cannot be produced")
        else:
            v = attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
            check(v.refusal == dl.UNREADABLE and not v.attributed, "an EACCES environ is unreadable, never a match")
    finally:
        os.chmod(fake.proc / "4700" / "environ", 0o600)
    fake.process(4701, start=100100, environ=tagged(T1))
    os.chmod(fake.proc / "4701" / "stat", 0)
    try:
        if not euid_root:
            f2 = reader.facts(4701)
            v = attribute(listener(), [f2], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
            check(f2 is None and v.refusal == dl.UNREADABLE, "an EACCES stat gives no facts: unreadable")
    finally:
        os.chmod(fake.proc / "4701" / "stat", 0o600)
    fake.process(4720, start=100100, fds=["socket:[555]"])
    os.chmod(fake.proc / "4720" / "fd", 0)
    try:
        if not euid_root:
            check(reader.socket_owners([4720], [555]) == {},
                  "an fd folder that cannot be listed holds nothing (EACCES is not a match)")
    finally:
        os.chmod(fake.proc / "4720" / "fd", 0o700)
    check(reader.socket_owners([4720, 99999], [555]) == {555: [4720]},
          "readable again it is found, and a vanished pid is skipped")
    check(reader.facts(99999) is None, "a vanished pid has no facts")
    fake.process(4702, start=100100, environ=tagged(T1))
    f3 = reader.facts(4702)
    fake.restart(4702, 100900)                                 # the pid now names another process
    v = attribute(listener(), [f3], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.UNREADABLE, "a start time changed before the environ read: unreadable")
    fake.process(4703, start=100100, environ=tagged(T1))
    f4 = reader.facts(4703)
    import shutil
    shutil.rmtree(fake.proc / "4703")
    check(reader.tag(f4) == dl.UNREADABLE, "a pid gone before the environ read: unreadable")
    fake.process(4704, ppid=4705, start=100100, environ=tagged(T1))
    check(reader.facts(4704) is None, "a parent that vanished mid-walk: no facts")
    fake.process(4706, start=100500)
    fake.process(4707, ppid=4706, start=100100, environ=tagged(T1))
    check(reader.facts(4707) is None, "a parent younger than its child (a reused number): no facts")
    fake.process(4708, start=100100, state="Z")
    check(reader.facts(4708) is None, "a zombie has no facts")
    # The ancestor walk is bounded and stops at its stop set.
    check(dl.ANCESTRY_MAX == 64, "the ancestor walk is 64 deep")
    for i in range(40):
        fake.process(5000 + i, ppid=(5000 + i - 1) if i else 1, sid=5000, start=1000 + i)
    reader.work = dl.Work()
    mid = reader.facts(5039)
    check(mid is not None and len(mid.ancestors) == 39 and not mid.truncated
          and reader.work.stat_reads <= 39 + 2,
          f"a 39-deep chain is read to pid 1 ({reader.work.stat_reads} stat reads), not truncated")
    stopped = reader.facts(5039, stop=frozenset({5036}))
    check(stopped is not None and [p for p, _ in stopped.ancestors] == [5038, 5037]
          and not stopped.truncated,
          "the walk stops at a stop pid (the subreaper, the daemon, a provider root)")
    chain(fake, 6000, 64, top_ppid=1)                    # 6064 has exactly 64 ancestors, then pid 1
    reader.work = dl.Work()
    exact = reader.facts(6064)
    check(exact is not None and len(exact.ancestors) == 64 and not exact.truncated
          and reader.work.stat_reads == 64 + 1,
          f"exactly 64 ancestors up to pid 1: all read, not truncated ({reader.work.stat_reads} stat reads)")
    chain(fake, 6100, 65, top_ppid=1)
    reader.work = dl.Work()
    over = reader.facts(6165)
    check(over is not None and len(over.ancestors) == 64 and over.truncated
          and reader.work.stat_reads <= 64 + 2,
          f"65 ancestors: 64 are read, no more ({reader.work.stat_reads} stat reads), and the facts say truncated")
    chain(fake, 6200, 80, top_ppid=1)
    reader.work = dl.Work()
    far = reader.facts(6280)
    check(far is not None and far.truncated and len(far.ancestors) == 64 and reader.work.stat_reads <= 64 + 2,
          "an 80-deep chain is read 64 deep and is truncated")
    # A stop pid at the very limit ends the walk there: not truncated.
    at_limit = reader.facts(6165, stop=frozenset({6100}))
    check(at_limit is not None and not at_limit.truncated and len(at_limit.ancestors) == 64,
          "a stop pid right where the limit falls ends the walk: not truncated")
    check(exact.truncated is False and far.truncated is True and reader.facts(5039).truncated is False,
          "`truncated` is a plain flag on the facts")

    print("\n-- rule 6 reaches a terminal 25 levels up, and fails closed past the limit")
    # Every process of a chain is its own session leader (a daemoniser's habit), so
    # only the ancestor walk can find the terminal's leader at the top.
    chain(fake, 7000, 25, start0=100000, environ=tagged(T1))
    chain(fake, 7100, 70, start0=100000, environ=tagged(T1))
    sessions, known = world()
    calls = Counting(reader)
    known25 = dataclasses.replace(known, terminal_leaders=known.terminal_leaders | {(7000, 100000)})
    f25 = reader.facts(7025)
    v = attribute(listener(), [f25], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(len(f25.ancestors) == 25 and v.attributed,
          "control: 25 levels below a process that is not a terminal's, a tagged server is attributed")
    v = attribute(listener(), [f25], sessions, 1006.0, known25, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.TERMINAL and not v.attributed,
          "a process 25 levels below a terminal's leader is refused as terminal's (16 levels used to stop short)")
    f70 = reader.facts(7170)
    calls = Counting(reader)
    v = attribute(listener(), [f70], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(f70.truncated and v.refusal == dl.TERMINAL and not v.attributed and calls.calls == 0,
          "a process 70 levels deep, however it is tagged, is refused (fail closed) with no environ read")
    v = attribute(listener(), [f70, reader.facts(7025)], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.TERMINAL,
          "a truncated holder refuses the listener even when another holder would pass")
    fake.process(5100, ppid=5101, start=1000)
    fake.process(5101, ppid=5100, start=1000)
    loop = reader.facts(5100)
    check(loop is not None and len(loop.ancestors) == 1, "a ppid loop ends the walk")


# -- the tag finder ---------------------------------------------------------------

def tag_checks(fake: Fake, records: list) -> None:
    print("\n-- the origin tag finder")
    t = T1.encode()

    def find(raw: bytes, complete: bool = True):
        buf = bytearray(raw)
        return dl.find_tag(buf, len(raw), complete)

    seen: list[str] = []
    try:
        cases = {
            "at offset 0": (env(b"JARVIS_ORIGIN_TAG=" + t, b"A=1"), t),
            "after a NUL": (tagged(T1), t),
            "XJARVIS_ORIGIN_TAG= (no preceding NUL)": (env(b"XJARVIS_ORIGIN_TAG=" + t), None),
            "inside another value": (env(b"FOO=JARVIS_ORIGIN_TAG=" + t), None),
            "31 bytes": (env(b"OPENROUTER_API_KEY=" + SECRET, b"JARVIS_ORIGIN_TAG=" + t[:31]), None),
            "33 bytes": (env(b"OPENROUTER_API_KEY=" + SECRET, b"JARVIS_ORIGIN_TAG=" + t + b"0"), None),
            "uppercase hex": (env(b"OPENROUTER_API_KEY=" + SECRET, b"JARVIS_ORIGIN_TAG=" + t.upper()), None),
            "not hex": (env(b"OPENROUTER_API_KEY=" + SECRET, b"JARVIS_ORIGIN_TAG=" + b"z" * 32), None),
            "the secret as the value": (env(b"JARVIS_ORIGIN_TAG=" + SECRET[:32]), None),
            "the first of two": (env(b"JARVIS_ORIGIN_TAG=" + t, b"JARVIS_ORIGIN_TAG=" + T2.encode()), t),
            "a malformed first and a valid second: the first wins, as getenv does":
                (env(b"JARVIS_ORIGIN_TAG=zz", b"JARVIS_ORIGIN_TAG=" + t), None),
            "an empty first and a valid second": (env(b"JARVIS_ORIGIN_TAG=", b"JARVIS_ORIGIN_TAG=" + t), None),
        }
        for label, (raw, want) in cases.items():
            got = find(raw)
            seen.append(repr(got))
            check(got == want, f"find_tag: {label}")
        check(find(b"A=1\0JARVIS_ORIGIN_TAG=" + t, True) == t
              and find(b"A=1\0JARVIS_ORIGIN_TAG=" + t, False) is None,
              "a value ending the buffer counts only when the buffer holds the whole environment")
    except Exception as exc:  # noqa: BLE001
        seen.append(f"{exc!r} {exc}")
        check(False, f"find_tag raised {type(exc).__name__}")

    sessions, known = world()
    reader = fake.reader()
    fake.process(4800, start=100100, environ=tagged(T1))
    f = reader.facts(4800)
    got = reader.tag(f)
    seen.append(repr(got))
    check(got == t and isinstance(got, bytes) and len(got) == 32,
          "beside a fake API key, only the 32 tag bytes come back")
    fake.process(4801, start=100100, environ=env(b"OPENROUTER_API_KEY=" + SECRET, b"LANG=\xff\xfe"))
    got = reader.tag(reader.facts(4801))
    seen.append(repr(got))
    check(got == dl.UNTAGGED, "no tag beside the fake key: untagged (and nothing raised)")
    fake.process(4802, start=100100, environ=env(b"OPENROUTER_API_KEY=" + SECRET, b"JARVIS_ORIGIN_TAG=" + b"Z" * 32))
    got = reader.tag(reader.facts(4802))
    seen.append(repr(got))
    check(got == dl.UNTAGGED, "a malformed tag beside the fake key: untagged (and nothing raised)")

    # A 1 MiB environment: only the first 128 KiB is ever read.
    pad = b"PAD=" + b"x" * (200 * 1024) + b"\0"
    big = env(b"OPENROUTER_API_KEY=" + SECRET) + pad + env(b"JARVIS_ORIGIN_TAG=" + t) + b"Q=" + b"y" * (900 * 1024)
    fake.process(4803, start=100100, environ=big)
    reader.work = dl.Work()
    got = reader.tag(reader.facts(4803))
    seen.append(repr(got))
    check(got == dl.UNTAGGED and reader.work.environ_bytes == dl.ENVIRON_CAP == 128 * 1024,
          f"a 1 MiB environment is read only to 128 KiB ({reader.work.environ_bytes} bytes); a tag past it is not seen")
    early = env(b"JARVIS_ORIGIN_TAG=" + t, b"OPENROUTER_API_KEY=" + SECRET) + b"Q=" + b"y" * (1024 * 1024)
    fake.process(4804, start=100100, environ=early)
    reader.work = dl.Work()
    got = reader.tag(reader.facts(4804))
    check(got == t and reader.work.environ_bytes <= dl.ENVIRON_CAP, "a tag inside the first 128 KiB is found")

    print("\n-- environ is read only after rules 1-7 pass, only for the owning pid")
    reader = fake.reader()
    fake.process(4810, start=100100, environ=tagged(T1))
    fake.process(4811, start=99000, environ=tagged(T1))        # started before the turn
    fake.process(4812, sid=600, start=100100, environ=tagged(T1))
    fake.process(600, start=60000, comm="bash")
    good = reader.facts(4810)
    refusals = [
        (listener(port=8405), [good], known),                                  # 1 jarvis_port
        (listener(port=9229), [good], known),                                  # 2 service_port
        (listener(port=40000), [good], known),                                 # 2 ephemeral
        (listener(bind=dl.ODD_LOOPBACK), [good], known),                       # 2 odd_loopback
        (listener(), [], known),                                               # 3 outside
        (listener(), [good], dataclasses.replace(known, uid=UID + 1)),         # 4 uid
        (listener(), [dataclasses.replace(good, pid=100)], known),             # 5 jarvis
        (listener(), [reader.facts(4812)], known),                             # 6 terminal
        (listener(), [reader.facts(4811)], known),                             # 7 no_turn
        (listener(), [None], known),                                           # unreadable facts
    ]
    out = [attribute(lst, fs, sessions, 1006.0, k, tag_of=reader.tag, probe=PAGE).refusal
           for lst, fs, k in refusals]
    check(out == [dl.JARVIS_PORT, dl.SERVICE_PORT, dl.EPHEMERAL, dl.ODD_LOOPBACK, dl.OUTSIDE, dl.UID,
                  dl.JARVIS, dl.TERMINAL, dl.NO_TURN, dl.UNREADABLE] and reader.work.environ_reads == 0,
          f"rules 1-7 refuse with no environ read ({reader.work.environ_reads} reads; {out})")
    v = attribute(listener(), [reader.facts(4811), good], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and reader.work.environ_reads == 1 and reader.work.kinds["environ"] == 1,
          "a passing pid: exactly one environ read, the owning pid's (not the earlier refused one's)")
    for _ in range(3):
        attribute(listener(), [good], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(reader.work.environ_reads == 1, "judging it again on later polls reads nothing more")

    print("\n-- the fake secret goes nowhere")
    texts = list(seen)
    texts += [repr(reader.work), repr(reader.work.as_dict()), repr(reader._tags), repr(v)]
    texts += [r.getMessage() + repr(r.args) + str(r.exc_info) + str(getattr(r, "exc_text", "")) for r in records]
    leaked = [x for x in texts if SECRET.decode() in x or SECRET[:20].decode() in x]
    check(not leaked, "the fake key is in no return value, exception, counter or log record")
    check(not [r for r in records if r.name.startswith("jarvis.v2.devlisten")],
          "the module logs nothing")


# -- the tag cache, the buffer and the read cap --------------------------------------

class OpenFails:
    """`os.open` raising `errno_` for a path ending `suffix`, `times` times,
    then behaving. Installed over whatever `os.open` is now."""

    def __init__(self, errno_: int, suffix: str = "/environ", times: int = 1):
        self.errno_, self.suffix, self.left = errno_, suffix, times
        self.real = None

    def __enter__(self):
        self.real = os.open
        outer = self

        def opener(path, *a, **k):
            if outer.left > 0 and isinstance(path, str) and path.endswith(outer.suffix):
                outer.left -= 1
                raise OSError(outer.errno_, os.strerror(outer.errno_))
            return outer.real(path, *a, **k)
        os.open = opener
        return self

    def __exit__(self, *exc):
        os.open = self.real


def tag_cache_checks(fake: Fake) -> None:
    import errno
    t = T1.encode()
    reader = fake.reader()

    print("\n-- the tag cache: only a found tag or `untagged` is remembered, keyed on (pid, start)")
    fake.process(8000, start=100100, environ=tagged(T1))
    f = reader.facts(8000)
    with OpenFails(errno.EMFILE):
        first = reader.tag(f)
    check(first == dl.UNREADABLE and reader.work.environ_reads == 1 and not reader._tags,
          "EMFILE on the environ open: unreadable, and nothing is cached")
    again = reader.tag(f)
    check(again == t and reader.work.environ_reads == 2 and len(reader._tags) == 1,
          "the next poll reads again and finds the tag (a failure is not remembered)")
    reader.tag(f)
    check(reader.work.environ_reads == 2, "a found tag is cached: no third read")

    fake.process(8001, start=100100, environ=tagged(T1))
    f = reader.facts(8001)
    reads = reader.work.environ_reads
    with OpenFails(errno.EACCES):
        first = reader.tag(f)
    check(first == dl.UNREADABLE and reader.tag(f) == t and reader.work.environ_reads == reads + 2,
          "EACCES once and then readable: unreadable, then the tag")
    if os.geteuid() != 0:
        fake.process(8002, start=100100, environ=tagged(T1))
        f = reader.facts(8002)
        os.chmod(fake.proc / "8002" / "environ", 0)
        try:
            first = reader.tag(f)
        finally:
            os.chmod(fake.proc / "8002" / "environ", 0o600)
        check(first == dl.UNREADABLE and reader.tag(f) == t,
              "a real EACCES environ, made readable later, is read on the next poll")
    else:
        print("note: running as root, a real EACCES cannot be produced")

    fake.process(8003, start=100100, environ=tagged(T1))
    f = reader.facts(8003)
    fake.restart(8003, 100900)                      # the number now names another process
    first = reader.tag(f)
    fake.restart(8003, 100100)
    check(first == dl.UNREADABLE and reader.tag(f) == t,
          "a start time that moved mid-read: unreadable, not cached; read afresh when it matches")

    fake.process(8004, start=100100, environ=env(b"A=1"))
    f = reader.facts(8004)
    reads = reader.work.environ_reads
    check(reader.tag(f) == dl.UNTAGGED, "an environment without the tag is untagged")
    (fake.proc / "8004" / "environ").write_bytes(tagged(T1))
    check(reader.tag(f) == dl.UNTAGGED and reader.work.environ_reads == reads + 1,
          "`untagged` is cached for the (pid, start)")

    # The stated limit: exec keeps the pid and its start time, so a cached tag outlives it.
    fake.process(8005, start=100100, environ=tagged(T1))
    f = reader.facts(8005)
    check(reader.tag(f) == t, "a tagged process is read")
    (fake.proc / "8005" / "environ").write_bytes(env(b"A=1"))      # what an exec of a cleaned env shows
    check(reader.tag(f) == t,
          "the limit, stated: a cached tag outlives an exec of the same pid (same start time)")

    # The key includes the start: a pid number reused later does not inherit the tag.
    fake.process(8010, start=100100, environ=tagged(T1))
    fa = reader.facts(8010)
    check(reader.tag(fa) == t, "the first process of pid 8010 is tagged")
    reads = reader.work.environ_reads
    fake.process(8010, start=100800, environ=env(b"A=1"))           # the number is reused
    fb = reader.facts(8010)
    got = reader.tag(fb)
    check(got == dl.UNTAGGED and reader.work.environ_reads == reads + 1,
          "a reused pid with a new start time reads afresh and does not inherit the dead process's tag")
    check(reader.tag(fa) == t, "and the old (pid, start) entry is still its own")

    print("\n-- the cache is bounded")
    small = fake.reader()
    for i in range(dl.TAG_CACHE + 10):
        fake.process(8100 + i, start=100100 + i, environ=env(b"A=1"))
        small.tag(small.facts(8100 + i))
    check(len(small._tags) == dl.TAG_CACHE, "at most TAG_CACHE (pid, start) entries are kept")

    print("\n-- the buffer is zeroed after the read, and the read is capped")
    fake.process(8200, start=100100, environ=tagged(T1))
    r = fake.reader()
    f = r.facts(8200)
    held: list = []
    real_fill = r._fill

    def spy(path, buf):
        out = real_fill(path, buf)
        held.append((buf, SECRET in bytes(buf[:out[0]])))
        return out
    r._fill = spy
    check(r.tag(f) == t, "the spied read finds the tag")
    buf, had_secret = held[0]
    check(had_secret and len(buf) == dl.ENVIRON_CAP and bytes(buf) == bytes(len(buf)),
          "the 128 KiB buffer held the (fake) key while read, and is all zero after the function returns")

    held.clear()
    fake.process(8201, start=100100, environ=tagged(T1))
    f = r.facts(8201)
    fake.restart(8201, 100999)                                       # the start check fails after the read
    check(r.tag(f) == dl.UNREADABLE and held and held[0][1] and bytes(held[0][0]) == bytes(len(held[0][0])),
          "and zeroed on the unreadable path too")

    held.clear()
    fake.process(8202, start=100100, environ=tagged(T1))
    f = r.facts(8202)

    def spy_raises(path, buf):
        out = real_fill(path, buf)
        held.append((buf, SECRET in bytes(buf[:out[0]])))
        raise OSError(5, "I/O error")                                # the read dies after filling the buffer
    r._fill = spy_raises
    check(r.tag(f) == dl.UNREADABLE and held and held[0][1] and bytes(held[0][0]) == bytes(len(held[0][0])),
          "and zeroed when the read fails after filling it")
    r._fill = real_fill

    path = fake.proc / "8300"
    fake.process(8300, start=100100, environ=b"x" * 64)
    for size, want in ((64, (64, False)), (63, (63, True)), (100, (64, False)), (0, (0, True)), (65, (64, False))):
        (path / "environ").write_bytes(b"x" * size)
        got = fake.reader()._fill(str(path / "environ"), bytearray(64))
        if got != want:
            check(False, f"_fill of a {size}-byte file into 64 bytes returned {got}, wanted {want}")
            break
    else:
        check(True, "_fill reports `complete` only when it hit the end of the file before the cap")

    cap = dl.ENVIRON_CAP
    head = b"P=" + b"z" * (cap - 2 - 1 - len(dl.TAG_NAME) - dl.TAG_LEN) + b"\0" + dl.TAG_NAME + t
    check(len(head) == cap, "(the fixture environment is exactly 128 KiB, the tag value last)")
    fake.process(8301, start=100100, environ=head)
    check(r.tag(r.facts(8301)) == dl.UNTAGGED,
          "an environment exactly at the cap, tag last with no NUL: not trusted (the value may go on)")
    fake.process(8302, start=100100, environ=head + b"0")
    check(r.tag(r.facts(8302)) == dl.UNTAGGED,
          "a tag whose 33rd digit lies past the cap is not read as a 32-digit tag")
    fake.process(8303, start=100100, environ=head[:-len(t)] + t + b"\0")
    check(r.tag(r.facts(8303)) == dl.UNTAGGED,
          "(a tag that ends exactly where the cap does, its NUL beyond it, is likewise not seen)")
    fake.process(8304, start=100100, environ=head[:-1][-(cap - 1):])
    check(len(head[:-1]) == cap - 1 and r.tag(r.facts(8304)) == dl.UNTAGGED,
          "a 31-digit tag value at the end of a short environment is not a tag")
    short = b"A=1\0" + dl.TAG_NAME + t
    fake.process(8305, start=100100, environ=short)
    check(r.tag(r.facts(8305)) == t, "the same tag at the end of an environment read whole is found")

    print("\n-- environ is opened without following a symlink")
    real_file = fake.proc / "8400-target"
    real_file.write_bytes(tagged(T1))
    fake.process(8400, start=100100, environ=None)
    os.symlink(real_file, fake.proc / "8400" / "environ")
    f = r.facts(8400)
    check(r.tag(f) == dl.UNREADABLE and not any(k == (8400, 100100) for k in r._tags),
          "an environ that is a symlink is unreadable (O_NOFOLLOW), and not cached")


# -- the fd scan's budget -----------------------------------------------------------------

def fd_budget_checks(base: Path) -> None:
    print("\n-- the fd scan is bounded: FD_SCAN_MAX readlinks, then it stops and attributes nothing")
    check(dl.FD_SCAN_MAX == 50_000, "the cap is 50,000 readlinks a scan")
    fake = Fake(base / "fdcap")
    big, tail = 9000, 9001
    d = fake.proc / str(big)
    (d / "fd").mkdir(parents=True)
    (d / "stat").write_text(stat_text(big, "node", "S", 1, big, 100100))
    for n in range(dl.FD_SCAN_MAX + 25):
        os.symlink("/dev/null", d / "fd" / str(n))
    fake.process(tail, start=100200, fds=["socket:[777001]"], environ=tagged(T1))
    reader = fake.reader()
    out = reader.socket_owners([big, tail], [777001])
    w = reader.work
    check(w.fd_readlinks == dl.FD_SCAN_MAX and w.fd_lists == 1,
          f"a pid with more fds than the cap costs at most the cap ({w.fd_readlinks} readlinks, {w.fd_lists} listing)")
    check(out == {} and out.complete is False and out.skipped == 2 and w.fd_skipped == 2
          and w.as_dict()["fd_skipped"] == 2,
          "the scan says it is incomplete, the skipped pids are counted, and the socket in the unread pid is not found")
    sessions, known = world()
    calls = Counting(reader)
    facts = [reader.facts(tail)]
    v = attribute(listener(), facts, sessions, 1006.0, known, tag_of=calls, probe=PAGE,
                     scan_complete=out.complete)
    check(v.outcome == "pending" and v.pending == "scan" and not v.attributed and calls.calls == 0,
          "an incomplete scan attributes nothing this poll: pending (scan), no environ read")
    v = attribute(listener(), facts, sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.attributed, "(the same facts from a complete scan do attribute)")
    v = attribute(listener(port=8405), facts, sessions, 1006.0, known, tag_of=calls, probe=PAGE,
                     scan_complete=False)
    check(v.refusal == dl.JARVIS_PORT, "a refused port is still refused on an incomplete scan, not left pending")
    v = attribute(listener(), [], sessions, 1006.0, known, tag_of=calls, probe=PAGE, scan_complete=False)
    check(v.pending == "scan", "no holder found on an incomplete scan is pending, not `outside`")

    old = dl.FD_SCAN_MAX
    dl.FD_SCAN_MAX = 5
    try:
        small = Fake(base / "fdsmall")
        for pid, nfds in ((9100, 5), (9101, 6), (9102, 1)):
            small.process(pid, start=100100, fds=["/dev/null"] * (nfds - 1) + ["socket:[555]"])
        r = small.reader()
        check(r.socket_owners([9100], [555]).complete and r.work.fd_readlinks == 5,
              "exactly the cap of fds in one pid: complete")
        r = small.reader()
        got = r.socket_owners([9101], [555])
        check(not got.complete and r.work.fd_readlinks == 5 and got.skipped == 1,
              "one fd over the cap: incomplete after exactly the cap's readlinks")
        r = small.reader()
        got = r.socket_owners([9102, 9102, 9102], [555])
        check(got.complete and got == {555: [9102]} and r.work.fd_readlinks == 3,
              "small pids within the budget in total: complete")
        r = small.reader()
        got = r.socket_owners([9100, 9102], [555])
        check(not got.complete and got == {555: [9100]} and got.skipped == 1 and r.work.fd_skipped == 1,
              "the budget is for the scan, not each pid: the second pid is left unread")
    finally:
        dl.FD_SCAN_MAX = old


# -- rule 7: a window's two conditions hold together ------------------------------------------

def window_checks() -> None:
    print("\n-- rule 7: first sighting and process start must fall in the same window")
    s = dl.Sessions()
    s.open(T1, "th-1", at=0.0)
    s.turn_started("th-1", "claude", at=1000.0)
    s.turn_ended("th-1", at=1100.0)                  # window 1: 1000..1100, grace to 1700
    s.turn_started("th-1", "claude", at=2000.0)      # window 2: 2000.., still running
    check(s.holding(2100.0, 1500.0) == frozenset(),
          "a process started in window 1's grace, first seen in window 2: not held by either")
    check(s.holding(2100.0, 1999.0) == {"th-1"}, "started within the slack of window 2: held")
    check(s.holding(1650.0, 1500.0) == {"th-1"}, "started in window 1 and seen in its grace: held")
    check(s.holding(1650.0, 990.0) == frozenset(), "started before window 1 and seen in it: not held")
    _unused, known = world()
    sessions = dl.Sessions()
    sessions.open(T1, "th-1", at=999.0)
    sessions.turn_started("th-1", "claude", at=1000.0)
    sessions.turn_ended("th-1", at=1100.0)
    sessions.turn_started("th-1", "claude", at=2000.0)
    f = dataclasses.replace(_passing_facts(), start=150000, started_at=1500.0, leader_start=150000)
    v = attribute(listener(first=2100.0, last=2103.0), [f], sessions, 2104.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "attribute(): that process is no_turn")
    v = attribute(listener(first=1650.0, last=1653.0), [f], sessions, 1654.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.attributed, "attribute(): the same process seen inside window 1's grace is attributed")


# -- small guards -------------------------------------------------------------------------------

def nit_checks() -> None:
    print("\n-- the socket owner's uid, an unknown bind class, and the tag out of reprs")
    ports = KNOWN_PORTS
    mine = dl.Row(4, dl.LOOPBACK, 5173, 1, UID)
    other = dl.Row(4, dl.LOOPBACK, 5173, 1, UID + 1)
    check(dl.cheap_refusal(mine, ports, UID) is None and dl.cheap_refusal(other, ports, UID) == dl.UID
          and dl.cheap_refusal(other, ports) is None,
          "a socket owned by another uid is refused as uid (the daemon's uid is passed in; none passed: no check)")
    check(dl.cheap_refusal(dl.Row(4, dl.LOOPBACK, 8405, 1, UID + 1), ports, UID) == dl.JARVIS_PORT,
          "the port rules still come first")
    check(dl.cheap_refusal(dl.Row(4, "weird", 5173, 1, UID), ports, UID) == dl.ODD_LOOPBACK
          and dl.cheap_refusal(dl.Row(4, "", 5173, 1, UID), ports, UID) == dl.ODD_LOOPBACK
          and dl.cheap_refusal(dl.Row(4, dl.WILDCARD, 5173, 1, UID), ports, UID) is None,
          "an unknown bind class is refused; the wildcard and loopback are not")
    sessions, known = world()
    calls = Counting(dl.ProcReader())
    row = dl.Listener(other, 1002.0, 1005.0)
    v = attribute(row, [_passing_facts()], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    v2 = attribute(row, [], sessions, 1006.0, known, tag_of=calls, probe=PAGE)
    check(v.refusal == dl.UID and v2.refusal == dl.UID and calls.calls == 0,
          "attribute(): another uid's socket is uid before rule 3 and before any process is judged")
    check(dl.probe_address(other, ports, UID) is None and dl.probe_address(mine, ports, UID) == ("127.0.0.1", 5173)
          and dl.probe_address(other, ports) == ("127.0.0.1", 5173),
          "probe_address passes the uid through when it is given")

    s = dl.Sessions()
    s.open(T1, "th-1", at=0.0)
    session = s.owner(T1.encode(), 1.0)
    check(T1 not in repr(session) and "th-1" in repr(session),
          "a Session's repr does not carry its tag")


def arbitrate_malformed_checks() -> None:
    print("\n-- arbitrate drops a malformed claim")
    C = dl.Claim
    good = C("wa", False, 100, 0.5)
    bad = [
        C("wx", "yes", 100, 0.0),            # shows: not a bool
        C("wx", 1, 100, 0.0),
        C("wx", True, 100.5, 0.0),           # focused_at: a float
        C("wx", True, "100", 0.0),
        C("wx", True, True, 0.0),            # focused_at: a bool
        C("wx", True, -1, 0.0),              # focused_at out of range
        C("wx", True, 2**53, 0.0),
        C("wx", True, 100, float("nan")),    # at: not finite
        C("wx", True, 100, float("inf")),
        C("wx", True, 100, "0.0"),
        C("wx", True, 100, None),
        C(7, True, 100, 0.0),                # window: not a string
        C(None, True, 100, 0.0),
        C("", True, 100, 0.0),
        C("w" * 200, True, 100, 0.0),
        {"window": "wx", "shows": True, "focused_at": 1, "at": 0.0},
        None,
        "claim",
    ]
    try:
        out = dl.arbitrate([*bad, good])
        check(out is good, "every malformed claim is dropped; the good one wins, none raises")
        check(dl.arbitrate(bad) is None, "only malformed claims: no winner, no error")
        late = C("wl", True, 5, 0.7)
        check(dl.arbitrate([C("wx", True, "100", 0.0), good, late]) is late,
              "a malformed claim does not open the 600 ms window (the good claim at 0.5 s does, so 0.7 s is in)")
        check(dl.arbitrate([C("wa", False, 0, 0.0), C("wb", False, 2**53 - 1, 0.1)]).window == "wb"
              and dl.arbitrate([C("w" * 128, True, 1, 0)]).window == "w" * 128,
              "the bounds are inclusive: focused_at 2**53-1, a 128-character window, an int `at`")
    except Exception as exc:  # noqa: BLE001
        check(False, f"arbitrate raised {type(exc).__name__}: {exc}")


# -- the probe -----------------------------------------------------------------------

def probe_checks() -> None:
    print("\n-- the probe's request and address")
    req = dl.probe_request(5173)
    check(req == (b"GET / HTTP/1.0\r\nHost: localhost:5173\r\nUser-Agent: Jarvis-devwatch\r\n"
                  b"Accept: text/html\r\nConnection: close\r\n\r\n"),
          "one GET / with Host, User-Agent, Accept and Connection: close; no cookie, credential or body")
    ports = KNOWN_PORTS
    check(dl.probe_address(dl.Row(4, dl.LOOPBACK, 5173, 1, UID), ports) == ("127.0.0.1", 5173)
          and dl.probe_address(dl.Row(4, dl.WILDCARD, 5173, 1, UID), ports) == ("127.0.0.1", 5173)
          and dl.probe_address(dl.Row(6, dl.LOOPBACK, 5173, 1, UID), ports) == ("::1", 5173)
          and dl.probe_address(dl.Row(6, dl.WILDCARD, 5173, 1, UID), ports) == ("::1", 5173)
          and dl.probe_address(dl.Row(6, dl.LOOPBACK, 5173, 1, UID, True), ports) == ("127.0.0.1", 5173),
          "127.0.0.1 for IPv4, wildcard and mapped binds; ::1 for ::1 and ::")
    check(dl.probe_address(dl.Row(4, dl.ODD_LOOPBACK, 5173, 1, UID), ports) is None
          and dl.probe_address(dl.Row(4, dl.LOOPBACK, 8405, 1, UID), ports) is None,
          "never an odd loopback or a refused port")

    print("\n-- the probe parser")

    def resp(status: str, headers=(), body: bytes = b"") -> bytes:
        head = f"HTTP/1.1 {status}\r\n" + "".join(f"{h}\r\n" for h in headers) + "\r\n"
        return head.encode() + body

    page = b"<!DOCTYPE html><html><head><title>x</title></head><body>hi</body></html>"
    cases = [
        ("a 302 passes", resp("302 Found", ["Location: /login"]), None),
        ("a 301 with no headers passes", resp("301 Moved Permanently"), None),
        ("a 200 text/html passes", resp("200 OK", ["Content-Type: text/html"], b"plain"), None),
        ("a 200 text/html; charset=utf-8 passes", resp("200 OK", ["content-type: Text/HTML; charset=utf-8"]), None),
        ("a 200 with no type and markup passes", resp("200 OK", [], page), None),
        ("a 200 with no type and <body> passes", resp("200 OK", [], b"<BODY>"), None),
        ("a 200 with no type and no markup is not_page", resp("200 OK", [], b"hello"), dl.NOT_PAGE),
        ("a 200 application/json is not_page", resp("200 OK", ["Content-Type: application/json"],
                                                    b'{"html": "<html>"}'), dl.NOT_PAGE),
        ("a 404 text/html with markup passes", resp("404 Not Found", ["Content-Type: text/html"], page), None),
        ("a 404 text/html without markup is not_page",
         resp("404 Not Found", ["Content-Type: text/html"], b"Cannot GET /"), dl.NOT_PAGE),
        ("a 404 with no type and markup is not_page", resp("404 Not Found", [], page), dl.NOT_PAGE),
        ("a 404 application/json is not_page", resp("404 Not Found", ["Content-Type: application/json"],
                                                   b'{"error":"nope"}'), dl.NOT_PAGE),
        ("a 500 text/plain is not_page", resp("500 Internal Server Error", ["Content-Type: text/plain"], page),
         dl.NOT_PAGE),
        ("X-Frame-Options: DENY is no_frame", resp("200 OK", ["Content-Type: text/html", "X-Frame-Options: DENY"]),
         dl.NO_FRAME),
        ("x-frame-options: sameorigin is no_frame",
         resp("200 OK", ["Content-Type: text/html", "x-frame-options: sameorigin"]), dl.NO_FRAME),
        ("frame-ancestors 'none' is no_frame",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src 'self'; frame-ancestors 'none'"]),
         dl.NO_FRAME),
        ("frame-ancestors 'self' is no_frame",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: FRAME-ANCESTORS 'self'"]), dl.NO_FRAME),
        ("a second CSP header refusing is no_frame",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src *",
                         "Content-Security-Policy: frame-ancestors 'self'"]), dl.NO_FRAME),
        ("a 302 with X-Frame-Options: DENY is no_frame",
         resp("302 Found", ["Location: /", "X-Frame-Options: DENY"]), dl.NO_FRAME),
        ("frame-ancestors * passes", resp("200 OK", ["Content-Type: text/html",
                                                     "Content-Security-Policy: frame-ancestors *"]), None),
        ("frame-ancestors http://localhost:* passes",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: frame-ancestors 'self' http://localhost:*"]),
         None),
        ("a CSP without frame-ancestors passes",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src 'self'"]), None),
        ("a report-only CSP is not enforced",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy-Report-Only: frame-ancestors 'none'"]),
         None),
        ("garbage is not_http", b"hello world\r\n\r\n", dl.NOT_HTTP),
        ("an SSH banner is not_http", b"SSH-2.0-OpenSSH_9.6\r\n", dl.NOT_HTTP),
        ("headers past 4 KiB are not_http",
         resp("200 OK", ["Content-Type: text/html", "X-Pad: " + "a" * 5000], page), dl.NOT_HTTP),
        # -- policies and lists are read whole (they were survivors of the review's guard removals)
        ("a comma-joined CSP whose second policy refuses is no_frame",
         resp("200 OK", ["Content-Type: text/html",
                         "Content-Security-Policy: frame-ancestors *, frame-ancestors 'none'"]), dl.NO_FRAME),
        ("a comma-joined CSP whose first policy refuses is no_frame",
         resp("200 OK", ["Content-Type: text/html",
                         "Content-Security-Policy: frame-ancestors 'self', default-src 'self'"]), dl.NO_FRAME),
        ("a comma-joined CSP that allows framing in every policy passes",
         resp("200 OK", ["Content-Type: text/html",
                         "Content-Security-Policy: frame-ancestors *, default-src 'self'"]), None),
        ("X-Frame-Options: ALLOWALL, DENY is no_frame",
         resp("200 OK", ["Content-Type: text/html", "X-Frame-Options: ALLOWALL, DENY"]), dl.NO_FRAME),
        ("X-Frame-Options: SAMEORIGIN, SAMEORIGIN is no_frame",
         resp("200 OK", ["Content-Type: text/html", "X-Frame-Options: SAMEORIGIN, SAMEORIGIN"]), dl.NO_FRAME),
        ("two Content-Type headers: the last one (text/html) wins and passes",
         resp("200 OK", ["Content-Type: text/plain", "Content-Type: text/html"], b"plain"), None),
        ("two Content-Type headers: the last one (application/json) wins and is not_page",
         resp("200 OK", ["Content-Type: text/html", "Content-Type: application/json"], page), dl.NOT_PAGE),
        # -- where the headers end
        ("LF headers end at their own blank line, not at a later CRLF one in the body",
         b'HTTP/1.1 200 OK\nContent-Type: application/json\n\n{"a":1}\r\nContent-Type: text/html\r\n\r\n',
         dl.NOT_PAGE),
        ("a body that quotes X-Frame-Options after LF headers is not a header",
         b"HTTP/1.1 200 OK\nContent-Type: text/html\n\n<html>docs\r\nX-Frame-Options: DENY\r\n\r\n", None),
        ("CRLF headers end at their own blank line, not at an earlier bare LF pair in the body",
         b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX-Frame-Options: DENY\r\n\r\n<html>\n\nx", dl.NO_FRAME),
        ("LF-only headers still end at the first blank line",
         b"HTTP/1.1 200 OK\nContent-Type: text/html\nX-Frame-Options: DENY\n\n<html>", dl.NO_FRAME),
        # -- obs-fold (RFC 7230 §3.2.4): a continuation line joins the previous header with one space
        ("a CSP folded onto a second line (a space) is read whole",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src 'self';",
                         " frame-ancestors 'none'"]), dl.NO_FRAME),
        ("a CSP folded with a tab",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src 'self';",
                         "\tframe-ancestors 'none'"]), dl.NO_FRAME),
        ("an X-Frame-Options value folded onto the next line",
         resp("200 OK", ["Content-Type: text/html", "X-Frame-Options:", " DENY"]), dl.NO_FRAME),
        ("a fold on LF-only headers",
         b"HTTP/1.1 200 OK\nContent-Type: text/html\nContent-Security-Policy: default-src 'self';\n"
         b"  frame-ancestors 'none'\n\n<html>", dl.NO_FRAME),
        ("a fold of a harmless policy still passes",
         resp("200 OK", ["Content-Type: text/html", "Content-Security-Policy: default-src 'self';",
                         " img-src *"]), None),
        ("a folded Content-Type is joined (text/ + html is not text/html)",
         resp("200 OK", ["Content-Type: text/", " html"], b"plain"), dl.NOT_PAGE),
        ("a fold with no header before it is dropped, not raised on",
         resp("200 OK", [" orphan", "Content-Type: text/html"], b"plain"), None),
        # -- markup needs a tag name that ends
        ("<header> is not <head>: an untyped 200 is not_page",
         resp("200 OK", [], b"<header>Site</header>"), dl.NOT_PAGE),
        ("<htmlx> is not <html>",
         resp("200 OK", [], b"<htmlx>"), dl.NOT_PAGE),
        ("<bodyguard> and <heading> are not markup either",
         resp("200 OK", [], b"<bodyguard></bodyguard><heading>"), dl.NOT_PAGE),
        ("a 404 text/html holding only <header> is not_page",
         resp("404 Not Found", ["Content-Type: text/html"], b"<header>Cannot GET /</header>"), dl.NOT_PAGE),
        ("<html lang=en> is markup", resp("200 OK", [], b"<html lang=en>"), None),
        ("<BODY then a newline (whitespace after the name) is markup",
         resp("200 OK", [], b"<BODY\n>"), None),
        ("<head/> is markup", resp("200 OK", [], b"<head/>"), None),
        ("<!DOCTYPE HTML> is markup", resp("200 OK", [], b"<!DOCTYPE HTML>"), None),
        ("<!doctype htmlx> is not markup", resp("200 OK", [], b"<!doctype htmlx>"), dl.NOT_PAGE),
    ]
    for label, data, want in cases:
        try:
            p = dl.parse_response(data)
            check(p.refusal == want, label)
        except Exception as exc:  # noqa: BLE001
            check(False, f"{label}: raised {type(exc).__name__}")
    silent = dl.parse_response(b"")
    alert = dl.parse_response(b"\x15\x03\x01\x00\x02\x02\x50")
    check(silent.refusal == dl.NOT_HTTP and silent.retry_tls and alert.retry_tls,
          "a silent close or a TLS alert asks for one TLS probe")
    check(not dl.parse_response(b"garbage\r\n\r\n").retry_tls, "garbage does not")
    secret_body = resp("200 OK", ["Content-Type: text/html", "Set-Cookie: s=SECRETCOOKIE"], b"<html>SECRETBODY")
    p = dl.parse_response(secret_body)
    check(p.refusal is None and "SECRET" not in repr(p)
          and all(f.type in ("str | None", "int", "bool") for f in dataclasses.fields(dl.Probe)),
          "only the status class and booleans survive the probe")


# -- arbitrate -----------------------------------------------------------------------

def arbitrate_checks() -> None:
    print("\n-- arbitrate")
    C = dl.Claim
    a, b = C("wa", False, 200, 0.0), C("wb", True, 100, 0.3)
    check(dl.arbitrate([a, b]) is b, "the earliest shows claim beats a newer focused_at")
    a, b, c = C("wa", False, 100, 0.0), C("wb", False, 300, 0.1), C("wc", False, 200, 0.2)
    check(dl.arbitrate([c, a, b]) is b, "else the newest focused_at (claims in any order)")
    a, b = C("wa", False, 300, 0.0), C("wb", False, 300, 0.1)
    check(dl.arbitrate([b, a]) is a, "ties go to the earliest claim")
    a, b = C("wa", True, 0, 0.0), C("wb", True, 999, 0.2)
    check(dl.arbitrate([a, b]) is a, "of two shows claims, the earliest")
    a, b = C("wa", False, 100, 0.0), C("wb", True, 500, 0.7)
    check(dl.arbitrate([a, b]) is a, "a claim after the 600 ms decision loses, even with shows")
    a, b = C("wa", False, 100, 0.0), C("wb", True, 500, 0.6)
    check(dl.arbitrate([a, b]) is b, "a claim at exactly 600 ms is still in")
    a, a2 = C("wa", False, 100, 0.0), C("wa", True, 900, 0.1)
    check(dl.arbitrate([a, a2]) is a, "a window's second claim does not count")
    check(dl.arbitrate([]) is None, "no claims, no winner")


# -- sessions --------------------------------------------------------------------------

def sessions_checks() -> None:
    print("\n-- sessions")
    s = dl.Sessions()
    for bad in ("A" * 32, "a" * 31, "g" * 32, b"a" * 33):
        try:
            s.open(bad, "th", at=0.0)
            check(False, f"a bad tag {bad!r} is refused")
            break
        except ValueError as exc:
            if "a" * 31 in str(exc):
                check(False, "the refusal does not repeat the tag")
                break
    else:
        check(True, "a tag is exactly 32 lowercase hex characters")
    s.open(T1, "th-1", at=0.0)
    check(s.owner(T1.encode(), 1.0) is not None, "an open session's tag names it")
    check(not s.live(5.0), "no turn: not live (the watcher sleeps)")
    s.turn_started("th-1", "claude", at=10.0)
    check(s.live(11.0), "a running turn is live")
    s.turn_ended("th-1", at=20.0)
    check(s.live(620.0) and not s.live(621.0), "live for the grace after the turn")
    s.close(T1, at=30.0)
    s.prune(1000.0)
    check(s.owner(T1.encode(), 1000.0) is None and s.holding(15.0, 10.0) == frozenset(),
          "prune drops a closed session and an old window")
    s2 = dl.Sessions()
    s2.turn_started("th-1", "claude", at=10.0)
    s2.turn_started("th-1", "claude", at=50.0)               # a missed end
    check(s2.holding(30.0, 10.0) == {"th-1"} and s2.holding(60.0, 49.0) == {"th-1"},
          "a second start without an end closes the first window there")
    for i in range(dl.WINDOWS_MAX + 50):
        s2.turn_started(f"t{i}", "codex", at=100.0 + i)
        s2.turn_ended(f"t{i}", at=100.5 + i)
    check(len(s2._windows) <= dl.WINDOWS_MAX, "the windows are capped")


# -- work counts -----------------------------------------------------------------------

def work_checks(base: Path) -> None:
    print("\n-- work counts, not wall time")
    fake = Fake(base / "work")
    pids = list(range(10000, 11000))
    rows4 = [line(i, hex4("127.0.0.1"), 3000 + i, 900000 + i) for i in range(500)]
    rows4 += [line(500 + i, hex4("127.0.0.1"), 5000 + i, 950000 + i, state="01") for i in range(300)]
    rows6 = [line(i, hex6("::1"), 7000 + i, 990000 + i) for i in range(50)]
    fake.net(rows4, rows6)
    for pid in pids:
        d = fake.proc / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "stat").write_text(stat_text(pid, "sleep", "S", 1, pid, 100000))
        os.symlink("/dev/null", d / "fd" / "0")
        os.symlink("pipe:[1]", d / "fd" / "1")
        os.symlink(f"socket:[{pid}]", d / "fd" / "2")
    os.symlink("socket:[900007]", fake.proc / "10500" / "fd" / "3")
    fake.cgroup("/user.slice/app.slice/jarvis-daemon2.service", pids)
    reader = fake.reader()
    rows = reader.net_rows()
    w = reader.work
    check(len(rows) == 550 and w.files == 2 and w.rows == 550 and w.lines == 852,
          f"a steady poll: 2 files, 852 lines looked at, 550 LISTEN rows parsed ({w.as_dict()})")
    cg = reader.own_cgroup()
    procs = reader.cgroup_pids(cg)
    check(cg == "/user.slice/app.slice/jarvis-daemon2.service" and len(procs) == 1000 and w.files == 4,
          "the daemon's cgroup and its 1,000 pids: two more files")
    check(reader.socket_owners(procs, []) == {} and w.fd_lists == 0 and w.fd_readlinks == 0,
          "no candidate inode: no fd scan")
    owners = reader.socket_owners(procs, [900007])
    check(owners == {900007: [10500]} and w.fd_lists == 1000 and w.fd_readlinks == 3001,
          f"one candidate: one fd scan, readlinks equal to the cgroup's fds ({w.fd_readlinks})")
    check(reader.cgroup_pids("/../etc") == [] and reader.cgroup_pids("relative") == [],
          "a cgroup path that could leave the root reads nothing")
    w.files = 0
    f = reader.facts(10500)
    check(f is not None and w.files == 1 and w.environ_reads == 0,
          "an orphan's facts: one stat read, no environ")


# -- the import pin ---------------------------------------------------------------------

def pin_checks() -> None:
    print("\n-- who may import it")
    allowed = {"jarvis/v2/devlisten.py", "jarvis/v2/devservers.py", "jarvis/v2/daemon.py",
               "jarvis/v2/hud_api.py", "jarvis/v2/terminals.py"}
    pattern = re.compile(r"\bdevlisten\b")
    offenders = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(errors="replace")
        if pattern.search(text) and rel not in allowed:
            offenders.append(rel)
    check(not offenders, f"only the daemon, hud_api, terminals and devservers may name devlisten ({offenders})")


def touched_checks(fixture_root: str) -> None:
    print("\n-- what was opened")
    bad = [p for p in TOUCHED if p.rsplit("/", 1)[-1] in ("cmdline", "cwd", "exe", "maps", "mem")]
    check(not bad, f"no cmdline, cwd or exe was read, under the fixture or the real /proc ({bad[:3]})")
    real_environ = [p for p in TOUCHED if p.startswith("/proc") and p.endswith("environ")]
    check(not real_environ, "no real /proc/<pid>/environ was read")
    check(any(p.endswith("/environ") for p in TOUCHED if p.startswith(fixture_root)),
          "the fixture environs were read (the monitor sees reads)")


def main() -> int:
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    keep = Keep(logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.addHandler(keep)
    old_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    with tempfile.TemporaryDirectory(prefix="devlisten-") as tmp:
        base = Path(tmp)
        _monitor_install(tmp)
        try:
            rows_checks(Fake(base / "rows"))
            port_checks(Fake(base / "ports"))
            attribution_checks(Fake(base / "attr"))
            fail_closed_checks(Fake(base / "closed"))
            tag_checks(Fake(base / "tags"), records)
            tag_cache_checks(Fake(base / "tagcache"))
            fd_budget_checks(base)
            window_checks()
            nit_checks()
            probe_checks()
            arbitrate_checks()
            arbitrate_malformed_checks()
            sessions_checks()
            work_checks(base)
            pin_checks()
        finally:
            _monitor_remove()
            root_logger.removeHandler(keep)
            root_logger.setLevel(old_level)
        touched_checks(tmp)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        return 1
    print("all devlisten checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
                     terminals=frozenset({(600, 60000)}))
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
                v = dl.attribute(dl.Listener(row, 1002.0, 1005.0), facts, sessions, 1006.0, known,
                                 tag_of=calls, probe=PAGE)
                if v.refusal != want:
                    fails.append((port, family, bind, "attribute", v.refusal))
            if dl.probe_address(row, ports) is not None:
                fails.append((port, family, bind, "probe address"))
    check(not fails, f"Jarvis's and the service ports are refused in every family, first ({fails[:3]})")
    check(calls.calls == 0 and reader.work.environ_reads == 0 and reader.work.files == 0,
          "a refused port reads no environ and no file")
    odd = dl.Row(4, dl.ODD_LOOPBACK, 5173, 5, UID)
    v = dl.attribute(dl.Listener(odd, 1002.0, 1005.0), [_passing_facts()], sessions, 1006.0, known,
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
    v = dl.attribute(listener(), [facts], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.thread_id == "th-1" and v.task_id is None and v.pid == 4242,
          "a tagged process in th-1's turn is attributed to th-1")

    # The tag decides, wherever the process works: its cwd and its parent's are /tmp.
    fake.process(4300, ppid=1, start=100050, cwd="/tmp")
    fake.process(4301, ppid=4300, sid=4300, start=100150, environ=tagged(T1), cwd="/tmp")
    f = reader.facts(4301)
    v = dl.attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.thread_id == "th-1" and f.ancestors == ((4300, 100050),),
          "no folder rule: a tagged process working in /tmp, its parent too, is attributed")

    for pid, name, environ in (
            (4400, "an empty environment (env -i)", b""),
            (4401, "an environment without the tag", env(b"PATH=/usr/bin", b"OPENROUTER_API_KEY=" + SECRET))):
        fake.process(pid, start=100100, environ=environ, cwd="/home/project")
        f = reader.facts(pid)
        v = dl.attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
        check(v.refusal == dl.UNTAGGED, f"tag required: {name} is untagged whatever its folder and turn")

    print("\n-- rule 7: the turn window")
    cases = [
        ("a process started before the turn (990 s), bound inside it", 99000, 1002.0, dl.NO_TURN),
        ("a process started 1.5 s before the turn", 99850, 1002.0, None),
        ("a process started 2 s before the turn (the slack's end)", 99800, 1002.0, None),
    ]
    for label, start, first, want in cases:
        f = dataclasses.replace(_passing_facts(), start=start, started_at=start / TCK, leader_start=start)
        v = dl.attribute(listener(first=first, last=first + 3), [f], sessions, first + 4, known,
                         tag_of=lambda _f: T1.encode(), probe=PAGE)
        check((v.refusal == want) if want else v.attributed, label)
    ended, _k = world()
    ended.turn_ended("th-1", at=1100.0)
    f = _passing_facts()
    v = dl.attribute(listener(first=1700.0, last=1703.0), [f], ended, 1704.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.attributed, "first seen at end + 600 s: inside the grace")
    v = dl.attribute(listener(first=1701.0, last=1704.0), [f], ended, 1705.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "first seen at end + 601 s: no_turn")
    v = dl.attribute(listener(first=999.0, last=1003.0), [f], sessions, 1004.0, known,
                     tag_of=lambda _f: T1.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "first seen before the turn started: no_turn")
    fast = dl.Sessions()
    fast.open(T3, "th-3", at=999.0)
    check(fast.turn_started("th-3", "fast", at=1000.0) is False
          and fast.turn_started("th-3", ProviderName.FAST, at=1000.0) is False,
          "a fast-path turn opens no window")
    v = dl.attribute(listener(), [f], fast, 1006.0, known, tag_of=lambda _f: T3.encode(), probe=PAGE)
    check(v.refusal == dl.NO_TURN, "a tagged process during a fast-path turn is no_turn")
    codex = dl.Sessions()
    codex.open(T3, "th-3", at=999.0)
    check(codex.turn_started("th-3", ProviderName.CODEX, at=1000.0) is True, "a Codex turn opens one")
    v = dl.attribute(listener(), [f], codex, 1006.0, known, tag_of=lambda _f: T3.encode(), probe=PAGE)
    check(v.attributed and v.thread_id == "th-3", "a tagged process in a Codex turn is attributed")

    print("\n-- rule 9: the tag must name a live session whose window holds it")
    v = dl.attribute(listener(), [f], sessions, 1006.0, known, tag_of=lambda _f: T2.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "a tag naming another thread (th-2, no turn) is stale_tag")
    v = dl.attribute(listener(), [f], sessions, 1006.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "a tag naming no session is stale_tag")
    closing, _k = world()
    closing.open(T4, "th-1", at=999.0)
    closing.close(T4, at=1001.0)
    v = dl.attribute(listener(), [f], closing, 1500.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.attributed, "a closed session inside its grace still attributes")
    v = dl.attribute(listener(), [f], closing, 1602.0, known, tag_of=lambda _f: T4.encode(), probe=PAGE)
    check(v.refusal == dl.STALE_TAG, "a closed session past its grace is stale_tag")
    two = dl.Sessions()
    two.open(T2, "th-2", "tk-2", at=999.0)
    two.turn_started("th-2", "codex", at=1000.0)
    v = dl.attribute(listener(), [f], two, 1006.0, known, tag_of=lambda _f: T2.encode(), probe=PAGE)
    check(v.attributed and (v.thread_id, v.task_id) == ("th-2", "tk-2"), "a task's session carries its task id")

    print("\n-- rules 3-6")
    v = dl.attribute(listener(), [], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.OUTSIDE, "no owning pid in the daemon's cgroup: outside")
    v = dl.attribute(listener(), [_passing_facts()], sessions, 1006.0, dataclasses.replace(known, uid=UID + 1),
                     tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.UID, "another uid: uid")
    before = reader.work.kinds["environ"]
    for pid in (100, 101, 102):
        fake.process(pid, start=100100, environ=tagged(T1))
        v = dl.attribute(listener(), [reader.facts(pid)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
        check(v.refusal == dl.JARVIS, f"pid {pid} (the daemon, a provider root, Chrome) is jarvis")
    check(reader.work.kinds["environ"] == before, "Jarvis's pids are refused before their environ is read")

    # Terminal 600 (start 60000 ticks).
    fake.process(600, start=60000, comm="bash")
    fake.process(4500, sid=600, start=100100, environ=tagged(T1))
    v = dl.attribute(listener(), [reader.facts(4500)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a process in a live terminal's session is terminal")
    fake.process(4501, ppid=600, sid=4501, start=100100, environ=tagged(T1))
    v = dl.attribute(listener(), [reader.facts(4501)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a setsid child of a terminal leader is terminal")
    fake.process(4502, sid=650, start=100100, environ=tagged(T1))
    known_650 = dataclasses.replace(known, terminals=frozenset({(650, 65000)}))
    v = dl.attribute(listener(), [reader.facts(4502)], sessions, 1006.0, known_650, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.TERMINAL, "a terminal session whose leader exited still counts")
    fake.process(700, start=99000, comm="sh")                  # number 700 reused
    fake.process(4503, sid=700, start=100100, environ=tagged(T1))
    known_700 = dataclasses.replace(known, terminals=frozenset({(700, 50000)}))
    f503 = reader.facts(4503)
    v = dl.attribute(listener(), [f503], sessions, 1006.0, known_700, tag_of=reader.tag, probe=PAGE)
    check(f503.leader_start == 99000 and v.attributed,
          "a reused session number with another leader start is not a terminal")
    fake.process(4504, ppid=700, sid=4504, start=100100, environ=tagged(T1))
    v = dl.attribute(listener(), [reader.facts(4504)], sessions, 1006.0, known_700, tag_of=reader.tag, probe=PAGE)
    check(v.attributed, "an ancestor whose number was reused is not the terminal's leader")

    print("\n-- several pids on one inode")
    fake.process(4600, start=100100, environ=tagged(T1))
    fake.process(4601, start=100200, environ=tagged(T1))
    fs = [reader.facts(4601), reader.facts(4600)]
    v = dl.attribute(listener(), fs, sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4600, "the earliest-started passing pid owns it")
    fake.process(102, start=90000, environ=tagged(T1))         # Jarvis's own, started first
    v = dl.attribute(listener(), [reader.facts(102), reader.facts(4601)], sessions, 1006.0, known,
                     tag_of=reader.tag, probe=PAGE)
    check(v.attributed and v.pid == 4601, "a refused earlier pid is passed over")
    v = dl.attribute(listener(), [None, reader.facts(102)], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.refusal == dl.JARVIS, "with none passing, the earliest readable pid's refusal is reported")

    print("\n-- rules 10 and 11 wait")
    v = dl.attribute(listener(first=1002.0, last=1003.5), [facts], sessions, 1004.0, known, tag_of=reader.tag)
    check(v.outcome == "pending" and v.pending == "life", "seen for under 2 s: pending life")
    v = dl.attribute(listener(first=1002.0, last=1004.0), [facts], sessions, 1005.0, known, tag_of=reader.tag)
    check(v.outcome == "pending" and v.pending == "probe", "two sightings 2 s apart, not yet probed: pending probe")
    for refusal in (dl.NOT_HTTP, dl.NOT_PAGE, dl.NO_FRAME):
        v = dl.attribute(listener(), [facts], sessions, 1006.0, known, tag_of=reader.tag,
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
            v = dl.attribute(listener(), [f], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
            check(v.refusal == dl.UNREADABLE and not v.attributed, "an EACCES environ is unreadable, never a match")
    finally:
        os.chmod(fake.proc / "4700" / "environ", 0o600)
    fake.process(4701, start=100100, environ=tagged(T1))
    os.chmod(fake.proc / "4701" / "stat", 0)
    try:
        if not euid_root:
            f2 = reader.facts(4701)
            v = dl.attribute(listener(), [f2], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
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
    v = dl.attribute(listener(), [f3], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
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
    for i in range(40):
        fake.process(5000 + i, ppid=(5000 + i - 1) if i else 1, sid=5000, start=1000 + i)
    reader.work = dl.Work()
    deep = reader.facts(5039)
    check(deep is not None and len(deep.ancestors) == dl.ANCESTRY_MAX
          and reader.work.stat_reads <= dl.ANCESTRY_MAX + 2,
          f"the walk reads at most {dl.ANCESTRY_MAX} ancestors ({reader.work.stat_reads} stat reads)")
    stopped = reader.facts(5039, stop=frozenset({5036}))
    check(stopped is not None and [p for p, _ in stopped.ancestors] == [5038, 5037],
          "the walk stops at a stop pid (the subreaper, the daemon, a provider root)")
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
    out = [dl.attribute(lst, fs, sessions, 1006.0, k, tag_of=reader.tag, probe=PAGE).refusal
           for lst, fs, k in refusals]
    check(out == [dl.JARVIS_PORT, dl.SERVICE_PORT, dl.EPHEMERAL, dl.ODD_LOOPBACK, dl.OUTSIDE, dl.UID,
                  dl.JARVIS, dl.TERMINAL, dl.NO_TURN, dl.UNREADABLE] and reader.work.environ_reads == 0,
          f"rules 1-7 refuse with no environ read ({reader.work.environ_reads} reads; {out})")
    v = dl.attribute(listener(), [reader.facts(4811), good], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(v.attributed and reader.work.environ_reads == 1 and reader.work.kinds["environ"] == 1,
          "a passing pid: exactly one environ read, the owning pid's (not the earlier refused one's)")
    for _ in range(3):
        dl.attribute(listener(), [good], sessions, 1006.0, known, tag_of=reader.tag, probe=PAGE)
    check(reader.work.environ_reads == 1, "judging it again on later polls reads nothing more")

    print("\n-- the fake secret goes nowhere")
    texts = list(seen)
    texts += [repr(reader.work), repr(reader.work.as_dict()), repr(reader._tags), repr(v)]
    texts += [r.getMessage() + repr(r.args) + str(r.exc_info) + str(getattr(r, "exc_text", "")) for r in records]
    leaked = [x for x in texts if SECRET.decode() in x or SECRET[:20].decode() in x]
    check(not leaked, "the fake key is in no return value, exception, counter or log record")
    check(not [r for r in records if r.name.startswith("jarvis.v2.devlisten")],
          "the module logs nothing")


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
            probe_checks()
            arbitrate_checks()
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

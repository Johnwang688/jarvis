"""Which local dev servers an agent started, and for which chat or task
(AO-1a; plan `docs/plans/2026-10-12-hud-browser-plan-amendment-2.md` §B and
§D.4, review decisions R-2 and R-7; the decisions file wins).

Pure: no thread, no network I/O, no clock, no logging. Every file it reads
sits under an injectable `/proc` root (and an injectable cgroup root) and is
counted in `Work`, so a test can hold the work to a number rather than a
time. AO-1b's watcher (`devservers.py`) wires it to the daemon; nothing here
is wired yet.

The pieces, in the order a poll uses them:

* **`parse_rows`** reads `/proc/net/tcp` and `tcp6`: LISTEN rows only (a
  cheap `b" 0A "` test before any split), addresses in the kernel's native
  word order, and only binds on `127.0.0.1`, `0.0.0.0`, `::1`, `::` and
  `::ffff:127.0.0.1`. Another 127/8 address is kept as `odd_loopback` so it
  is counted and refused; a LAN or `eth0` bind is dropped.
* **`cheap_refusal`** (rules 1 and 2) refuses Jarvis's ports, the service
  and debugger ports and the ephemeral range before anything else is read,
  then a socket owned by another uid (the daemon's uid is passed in).
* **`ProcReader`** reads the facts: the daemon's own cgroup, its
  `cgroup.procs`, the `socket:[inode]` links of those pids' fds (at most
  `FD_SCAN_MAX` readlinks a scan; past it the scan says it is incomplete and
  nothing is attributed from it), and `/proc/<pid>/stat` for the owner and up
  to `ANCESTRY_MAX` ancestors (a chain still going past that is `truncated`,
  and rule 6 refuses it). **It never reads `cwd`, `cmdline` or `exe`.** Every
  failed read fails closed.
* **The origin tag** (rule 8, R-2): `/proc/<pid>/environ` of the owning pid
  only, after rules 1-7 pass, opened `O_RDONLY | O_NONBLOCK`, at most
  `ENVIRON_CAP` bytes into a buffer that is searched as raw bytes for
  `JARVIS_ORIGIN_TAG=` at an entry's start and zeroed before the function
  returns. **It is never decoded, logged, formatted, put into an exception or
  returned**: what comes back is the 32 tag bytes or a refusal class. A found
  tag and "no tag" are remembered per `(pid, start)`; a failed read never is.
* **`Sessions`** holds the live tags and the Claude and Codex turn windows.
* **`attribute`** applies rules 1-11 in order and answers a thread, a
  refusal class, or "not yet" (rule 10's two sightings, rule 11's probe, an
  incomplete fd scan). **A socket several processes hold fails closed:** if
  any holder is Jarvis's own or a terminal's, the listener is refused as that,
  whatever another holder's tag says; only without such a holder does the
  earliest-started passing pid own it. Rules 7 and 9 are judged at the
  listener's first sighting, so a verdict waiting on its probe cannot change.
* **`parse_response`** judges what the probe read: a page that may be framed
  or a refusal class. Nothing of the response is kept but booleans.
* **`arbitrate`** picks the one window that acts on a server (§D.4).

There is no folder rule (R-2): the tag is the attribution, so an agent's
`cd /tmp && python3 -m http.server 5000` is attributed to its chat. The cost,
stated: a child started with a cleared environment (`env -i`) is missed.

Limits, stated. The tag is not a secret and not a boundary (any same-uid
program can copy it). **A cached tag outlives an `exec` of the same pid:**
the cache is keyed on `(pid, start)`, and `exec` keeps the start time, so a
process that execs a program with another environment is still judged by the
environment it was first read with. A pid reused by another process has
another start time and is read afresh.
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from jarvis.v2.browser import policy

# -- constants -------------------------------------------------------------------

# Rule 2 (R-7): Chrome DevTools, the Node inspector, debugpy and four databases.
SERVICE_PORTS = frozenset({9222, 9229, 5678, 5432, 3306, 6379, 27017})
# The kernel's default ephemeral range, used when the file cannot be read.
EPHEMERAL_FALLBACK = (32768, 60999)

GRACE_S = 600.0            # a turn's window stays open this long after it ends
START_SLACK_S = 2.0        # a process may start this long before its turn
MIN_LIFE_S = 2.0           # rule 10: two sightings at least this far apart
ANCESTRY_MAX = 64          # ancestors read for the terminal check; a longer chain is refused
FD_SCAN_MAX = 50_000       # fd readlinks in one scan; past it nothing is attributed this poll
ENVIRON_CAP = 128 * 1024   # bytes of /proc/<pid>/environ ever read
TAG_NAME = b"JARVIS_ORIGIN_TAG="
TAG_LEN = 32
PROVIDERS = frozenset({"claude", "codex"})   # the fast path cannot start a process
WINDOWS_MAX = 512          # turn windows kept at most
PRUNE_MARGIN_S = 120.0     # a window outlives its grace by this, for late re-judging
TAG_CACHE = 256            # (pid, start) -> tag results remembered

# The probe (§B.7). AO-1b's watcher connects and reads; the rules are here.
PROBE_CONNECT_S = 1.0
PROBE_RESPONSE_S = 20.0
PROBE_READ_MAX = 4096
PROBE_BUDGET_S = 30.0
PROBE_RETRY_S = (1, 2, 4, 8)
PROBES_IN_FLIGHT = 2

ARBITRATE_MS = 600         # §D.4: claims are collected this long from the first
WINDOW_ID_MAX = 128        # longest window nonce a claim may carry
FOCUSED_AT_MAX = 2**53 - 1  # epoch ms from a browser: a JavaScript safe integer

_NET_CAP = 64 * 1024 * 1024
_SMALL_CAP = 64 * 1024
_PROCS_CAP = 8 * 1024 * 1024

# Bind classes.
LOOPBACK = "loopback"
WILDCARD = "wildcard"

# Refusal classes (§B.5). Counted on `/status`, never logged per listener.
JARVIS_PORT = policy.JARVIS_PORT        # rule 1
SERVICE_PORT = "service_port"           # rule 2
EPHEMERAL = "ephemeral"
ODD_LOOPBACK = "odd_loopback"
OUTSIDE = "outside"                     # rule 3
UID = "uid"                             # rule 4
JARVIS = "jarvis"                       # rule 5
TERMINAL = "terminal"                   # rule 6
NO_TURN = "no_turn"                     # rule 7
UNTAGGED = "untagged"                   # rule 8
UNREADABLE = "unreadable"               # rule 8, and any fact that cannot be read
STALE_TAG = "stale_tag"                 # rule 9
BRIEF = "brief"                         # rule 10 (the watcher counts it)
NOT_HTTP = "not_http"                   # rule 11
NOT_PAGE = "not_page"
NO_FRAME = "no_frame"
REFUSALS = (JARVIS_PORT, SERVICE_PORT, EPHEMERAL, ODD_LOOPBACK, OUTSIDE, UID, JARVIS,
            TERMINAL, NO_TURN, UNTAGGED, UNREADABLE, STALE_TAG, BRIEF, NOT_HTTP, NOT_PAGE,
            NO_FRAME)

_STATE_LISTEN = b"0A"
_LISTEN_MARK = b" 0A "
_HEX = re.compile(rb"[0-9A-Fa-f]+")
_DIGITS = re.compile(rb"[0-9]+")
_TAG = re.compile(rb"[0-9a-f]{%d}" % TAG_LEN)

_V4_LOOPBACK = bytes((127, 0, 0, 1))
_V4_ANY = bytes(4)
_V6_LOOPBACK = bytes(15) + b"\x01"
_V6_ANY = bytes(16)
_V4_MAPPED = bytes(10) + b"\xff\xff"


# -- work ------------------------------------------------------------------------

@dataclass
class Work:
    """What the reads cost, counted (R-7: work, not wall time). Counts only:
    `/status` → `dev_watch.work` may carry `as_dict()` as it is."""

    files: int = 0            # files opened and read
    lines: int = 0            # /proc/net lines looked at
    rows: int = 0             # LISTEN rows split and parsed
    stat_reads: int = 0       # /proc/<pid>/stat reads (part of `files`)
    uid_reads: int = 0        # stat() of /proc/<pid>
    fd_lists: int = 0         # /proc/<pid>/fd listings
    fd_readlinks: int = 0
    fd_skipped: int = 0       # pids an fd scan left unread because it hit FD_SCAN_MAX
    environ_reads: int = 0
    environ_bytes: int = 0
    kinds: Counter = field(default_factory=Counter)   # what was opened, by entry name

    def as_dict(self) -> dict:
        return {"files": self.files, "lines": self.lines, "rows": self.rows,
                "stat_reads": self.stat_reads, "uid_reads": self.uid_reads,
                "fd_lists": self.fd_lists, "fd_readlinks": self.fd_readlinks,
                "fd_skipped": self.fd_skipped,
                "environ_reads": self.environ_reads, "environ_bytes": self.environ_bytes}


# -- /proc/net rows ----------------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One LISTEN row worth judging. `bind` is `loopback`, `wildcard` or
    `odd_loopback`; `mapped` marks an IPv4-mapped bind in a v6 socket."""

    family: int
    bind: str
    port: int
    inode: int
    uid: int
    mapped: bool = False


def _address(text: bytes, words: int) -> bytes | None:
    """The kernel prints an address as 32-bit words, each in the host's own
    byte order (`%08X` of the in-memory u32). Back to network-order bytes."""
    if len(text) != 8 * words or not _HEX.fullmatch(text):
        return None
    return b"".join(int(text[i:i + 8], 16).to_bytes(4, sys.byteorder)
                    for i in range(0, 8 * words, 8))


def _v4_class(addr: bytes) -> str | None:
    if addr == _V4_LOOPBACK:
        return LOOPBACK
    if addr == _V4_ANY:
        return WILDCARD
    if addr[0] == 127:
        return ODD_LOOPBACK
    return None


def bind_class(family: int, text: bytes) -> tuple[str, bool] | None:
    """(bind class, mapped) for a row's local address, or None for an address
    the HUD cannot reach (a LAN or `eth0` bind, `::ffff:0.0.0.0`, …)."""
    if family == 4:
        addr = _address(text, 1)
        return None if addr is None or _v4_class(addr) is None else (_v4_class(addr), False)
    addr = _address(text, 4)
    if addr is None:
        return None
    if addr == _V6_LOOPBACK:
        return LOOPBACK, False
    if addr == _V6_ANY:
        return WILDCARD, False
    if addr[:12] == _V4_MAPPED:
        inner = _v4_class(addr[12:])
        if inner in (LOOPBACK, ODD_LOOPBACK):     # ::ffff:0.0.0.0 is not a wildcard
            return inner, True
    return None


def parse_rows(data: bytes, family: int, work: Work | None = None) -> list[Row]:
    """The LISTEN rows of one `/proc/net/tcp` (family 4) or `tcp6` (6) text
    worth judging. Malformed lines are skipped, never raised on."""
    rows: list[Row] = []
    for line in data.split(b"\n"):
        if not line:
            continue
        if work is not None:
            work.lines += 1
        if _LISTEN_MARK not in line:              # cheap, before any split
            continue
        fields = line.split()
        if len(fields) < 10 or fields[3] != _STATE_LISTEN:
            continue
        if work is not None:
            work.rows += 1
        addr, sep, port = fields[1].rpartition(b":")
        if not sep or len(port) != 4 or not _HEX.fullmatch(port):
            continue
        if not _DIGITS.fullmatch(fields[7]) or not _DIGITS.fullmatch(fields[9]):
            continue
        port_n, inode = int(port, 16), int(fields[9])
        if port_n == 0 or inode == 0:
            continue
        cls = bind_class(family, addr)
        if cls is None:
            continue
        rows.append(Row(family, cls[0], port_n, inode, int(fields[7]), cls[1]))
    return rows


# -- ports (rules 1 and 2) ----------------------------------------------------------

@dataclass(frozen=True)
class PortRules:
    """`jarvis`: every port of Jarvis's own (rule 1). `service` and
    `ephemeral` (inclusive ends): rule 2."""

    jarvis: frozenset[int]
    service: frozenset[int] = SERVICE_PORTS
    ephemeral: tuple[int, int] = EPHEMERAL_FALLBACK


def port_rules(*, hud_port: int | None = None, api_port: int | None = None,
               workshop_port: int | None = None, gate_port: int | None = None,
               bound=(), ephemeral: tuple[int, int] = EPHEMERAL_FALLBACK,
               service: frozenset[int] = SERVICE_PORTS) -> PortRules:
    """Rule 1's list: `policy.refused_ports` (BR-0's control ports, the
    daemon's reported HUD and API ports, the default workshop port), the
    Browser gate's port, and every port this daemon has bound — its own
    workshop included, which `refused_ports` leaves out for the Browser's
    sake."""
    jarvis = set(policy.refused_ports(hud_port=hud_port, api_port=api_port,
                                      workshop_port=workshop_port))
    jarvis.update(p for p in (hud_port, api_port, workshop_port, gate_port, *bound) if p)
    return PortRules(frozenset(jarvis), frozenset(service), ephemeral)


def parse_ephemeral(text: bytes) -> tuple[int, int]:
    """`ip_local_port_range`'s two numbers, or the kernel default."""
    parts = text.split()
    if len(parts) == 2 and all(_DIGITS.fullmatch(p) for p in parts):
        low, high = int(parts[0]), int(parts[1])
        if 1 <= low <= high <= 65535:
            return low, high
    return EPHEMERAL_FALLBACK


def cheap_refusal(row: Row, ports: PortRules, uid: int | None = None) -> str | None:
    """Rules 1 and 2, then the socket owner's uid (rule 4's class), all before
    anything about a process is read. A bind class that is neither loopback
    nor wildcard is refused as `odd_loopback` (`parse_rows` makes no other).
    `uid` is the daemon's; the caller passes it in (None skips the check)."""
    if row.port in ports.jarvis:
        return JARVIS_PORT
    if row.port in ports.service:
        return SERVICE_PORT
    low, high = ports.ephemeral
    if low <= row.port <= high:
        return EPHEMERAL
    if row.bind not in (LOOPBACK, WILDCARD):
        return ODD_LOOPBACK
    if uid is not None and row.uid != uid:
        return UID
    return None


# -- process facts ------------------------------------------------------------------

@dataclass(frozen=True)
class Stat:
    state: bytes
    ppid: int
    sid: int
    start: int          # clock ticks since boot


@dataclass(frozen=True)
class PidFacts:
    """What attribution needs about one pid holding a listener. `start` is in
    ticks, `started_at` in seconds since boot (CLOCK_BOOTTIME's scale).
    `leader_start` is the start of the process now numbered `sid`, or None
    when there is none. `ancestors` is (pid, start) from the parent up, at
    most `ANCESTRY_MAX`; `truncated` says the chain was still going past it,
    so rule 6 cannot rule a terminal out."""

    pid: int
    start: int
    started_at: float
    ppid: int
    sid: int
    uid: int
    leader_start: int | None
    ancestors: tuple[tuple[int, int], ...]
    truncated: bool = False


def find_tag(buf, length: int, complete: bool) -> bytes | None:
    """The 32 tag bytes of the first `JARVIS_ORIGIN_TAG=` entry in
    `buf[:length]`, or None. The name must start an entry (offset 0 or right
    after a NUL), and its value must be exactly 32 lowercase hex bytes ending
    at a NUL, or at the end of a buffer that holds the whole environment
    (`complete`). Only the tag is ever copied out of `buf`."""
    if buf.startswith(TAG_NAME, 0, length):
        start = len(TAG_NAME)
    else:
        at = buf.find(b"\0" + TAG_NAME, 0, length)
        if at < 0:
            return None
        start = at + 1 + len(TAG_NAME)
    end = buf.find(b"\0", start, length)
    if end < 0:
        if not complete:
            return None
        end = length
    if end - start != TAG_LEN or not _TAG.fullmatch(buf, start, end):
        return None
    return bytes(buf[start:end])


class Owners(dict):
    """`socket_owners`' answer: inode → pids, as a dict, plus whether the scan
    finished. `complete` False means the fd budget ran out and `skipped` pids
    were not (fully) read; `attribute(scan_complete=False)` then answers
    pending for every listener."""

    complete: bool = True
    skipped: int = 0


class ProcReader:
    """Every read AO-1 makes, over an injectable `/proc` root and cgroup
    root, each counted in `work`. Reads fail closed: a missing, unreadable or
    changed entry is None (or `UNREADABLE`), never a guess."""

    def __init__(self, root: str = "/proc", cgroup_root: str = "/sys/fs/cgroup",
                 work: Work | None = None, clk_tck: int | None = None):
        self.root = os.fspath(root).rstrip("/") or "/"
        self.cgroup_root = os.fspath(cgroup_root).rstrip("/") or "/"
        self.work = work if work is not None else Work()
        self.clk_tck = clk_tck or os.sysconf("SC_CLK_TCK")
        self._tags: OrderedDict[tuple[int, int], bytes | str] = OrderedDict()

    # The one way a file is read (environ has its own, below).
    def _read(self, path: str, kind: str, cap: int) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            self.work.files += 1
            self.work.kinds[kind] += 1
            parts, size = [], 0
            while size < cap:
                chunk = os.read(fd, min(65536, cap - size))
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
            return b"".join(parts)
        finally:
            os.close(fd)

    def _pid_path(self, pid: int, entry: str) -> str:
        return f"{self.root}/{int(pid)}/{entry}"

    # -- rows --------------------------------------------------------------------

    def net_rows(self) -> list[Row]:
        """Both tables, read whole, once. A missing `tcp6` (IPv6 off) is
        simply no rows."""
        rows: list[Row] = []
        for name, family in (("tcp", 4), ("tcp6", 6)):
            try:
                data = self._read(f"{self.root}/net/{name}", f"net/{name}", _NET_CAP)
            except OSError:
                continue
            rows.extend(parse_rows(data, family, self.work))
        return rows

    def ephemeral(self) -> tuple[int, int]:
        try:
            text = self._read(f"{self.root}/sys/net/ipv4/ip_local_port_range",
                              "ip_local_port_range", 256)
        except OSError:
            return EPHEMERAL_FALLBACK
        return parse_ephemeral(text)

    # -- the daemon's cgroup ---------------------------------------------------------

    def own_cgroup(self, pid: int | str = "self") -> str | None:
        """The cgroup v2 path of `pid` (the daemon itself by default)."""
        try:
            text = self._read(f"{self.root}/{pid}/cgroup", "cgroup", _SMALL_CAP)
        except OSError:
            return None
        for line in text.split(b"\n"):
            if line.startswith(b"0::"):
                return _cgroup_path(line[3:])
        return None

    def cgroup_pids(self, cgroup: str) -> list[int]:
        path = _cgroup_path(cgroup.encode() if isinstance(cgroup, str) else cgroup)
        if path is None:
            return []
        try:
            text = self._read(f"{self.cgroup_root}{path.rstrip('/')}/cgroup.procs",
                              "cgroup.procs", _PROCS_CAP)
        except OSError:
            return []
        return [int(p) for p in text.split() if _DIGITS.fullmatch(p) and int(p) > 0]

    def socket_owners(self, pids, inodes) -> Owners:
        """inode → the pids (among `pids`) holding it, from their fd links.
        Nothing is read when no inode is wanted. At most `FD_SCAN_MAX`
        readlinks are made in one scan, a pid's fd folder walked lazily so a
        process with a million fds costs no more than the cap: past it the
        scan stops, counts the pids it left unread (`work.fd_skipped`) and
        answers with `complete` False — a partial answer, not to attribute
        from (a holder that is Jarvis's own or a terminal's may be in the
        part not read)."""
        want = {f"socket:[{int(i)}]": int(i) for i in inodes}
        found = Owners()
        if not want:
            return found
        pids = list(pids)
        budget = FD_SCAN_MAX
        for n, pid in enumerate(pids):
            fd_dir = self._pid_path(pid, "fd")
            tripped = False
            try:
                scan = os.scandir(fd_dir)
            except OSError:
                continue
            self.work.fd_lists += 1
            try:
                with scan:
                    for entry in scan:
                        if budget <= 0:
                            tripped = True
                            break
                        budget -= 1
                        self.work.fd_readlinks += 1
                        try:
                            target = os.readlink(f"{fd_dir}/{entry.name}")
                        except OSError:
                            continue
                        inode = want.get(target)
                        if inode is not None and pid not in found.setdefault(inode, []):
                            found[inode].append(pid)
            except OSError:
                continue                       # the process went away mid-listing
            if tripped:
                found.complete = False
                found.skipped = len(pids) - n
                self.work.fd_skipped += found.skipped
                break
        return found

    # -- facts -------------------------------------------------------------------------

    def stat(self, pid: int) -> Stat | None:
        try:
            data = self._read(self._pid_path(pid, "stat"), "stat", 4096)
        except (OSError, ValueError):
            return None
        finally:
            self.work.stat_reads += 1
        fields = data[data.rfind(b")") + 2:].split()
        try:
            return Stat(fields[0], int(fields[1]), int(fields[3]), int(fields[19]))
        except (IndexError, ValueError):
            return None

    def facts(self, pid: int, stop=frozenset()) -> PidFacts | None:
        """The owning pid's facts, or None when any of them cannot be read
        (fail closed). The ancestor walk reads only `stat`, at most
        `ANCESTRY_MAX` of them, and stops at pid 1 or any pid in `stop` (the
        subreaper, the daemon, the provider roots). A chain still going when
        the limit is reached is marked `truncated`: nothing above it was
        looked at, so rule 6 refuses the process."""
        st = self.stat(pid)
        if st is None or st.state in (b"Z", b"X"):
            return None
        try:
            self.work.uid_reads += 1
            uid = os.stat(f"{self.root}/{int(pid)}", follow_symlinks=False).st_uid
        except OSError:
            return None
        ancestors: list[tuple[int, int]] = []
        seen = {int(pid)}
        child_start, ppid = st.start, st.ppid
        for _ in range(ANCESTRY_MAX):
            if ppid <= 1 or ppid in stop or ppid in seen:
                break
            parent = self.stat(ppid)
            if parent is None or parent.start > child_start:
                return None                       # vanished, or the number was reused
            ancestors.append((ppid, parent.start))
            seen.add(ppid)
            child_start, ppid = parent.start, parent.ppid
        truncated = not (ppid <= 1 or ppid in stop or ppid in seen)   # the loop ran out
        if st.sid == pid:
            leader_start = st.start
        elif st.sid > 0:
            leader = self.stat(st.sid)
            leader_start = leader.start if leader is not None else None
        else:
            leader_start = None
        return PidFacts(int(pid), st.start, st.start / self.clk_tck, st.ppid, st.sid, uid,
                        leader_start, tuple(ancestors), truncated)

    # -- the origin tag (rule 8) ---------------------------------------------------------

    def tag(self, facts: PidFacts) -> bytes | str:
        """The process's origin tag (32 bytes), or `UNTAGGED` / `UNREADABLE`.
        A found tag and `UNTAGGED` are read once per (pid, start); a failed
        read (`UNREADABLE`: EMFILE, EACCES, a start time that moved) is never
        remembered, so the next poll reads again."""
        key = (facts.pid, facts.start)
        if key in self._tags:
            self._tags.move_to_end(key)
            return self._tags[key]
        result = self._read_tag(facts.pid, facts.start)
        if isinstance(result, bytes) or result == UNTAGGED:
            self._tags[key] = result
            while len(self._tags) > TAG_CACHE:
                self._tags.popitem(last=False)
        return result

    def _read_tag(self, pid: int, start: int) -> bytes | str:
        self.work.environ_reads += 1
        buf = bytearray(ENVIRON_CAP)
        try:
            got, complete = self._fill(self._pid_path(pid, "environ"), buf)
            self.work.environ_bytes += got
            tag = find_tag(buf, got, complete)
            again = self.stat(pid)                # the same process still?
            if again is None or again.start != start:
                return UNREADABLE
            return tag if tag is not None else UNTAGGED
        except Exception:  # noqa: BLE001 — nothing about the buffer leaves this function
            return UNREADABLE
        finally:
            buf[:] = bytes(len(buf))
            del buf

    def _fill(self, path: str, buf: bytearray) -> tuple[int, bool]:
        """Read at most `len(buf)` bytes into `buf`: (bytes read, whether that
        was the whole file)."""
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            self.work.files += 1
            self.work.kinds["environ"] += 1
            got = 0
            with memoryview(buf) as view:
                while got < len(buf):
                    n = os.readv(fd, [view[got:]])
                    if n == 0:
                        return got, True
                    got += n
            return got, False
        finally:
            os.close(fd)


def _cgroup_path(raw: bytes) -> str | None:
    """A cgroup v2 path as `/proc/<pid>/cgroup` prints it, or None for one
    that could leave the cgroup root."""
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text.startswith("/") or "\0" in text:
        return None
    if any(part in (".", "..") for part in text.split("/")):
        return None
    return text


# -- sessions and turn windows ----------------------------------------------------------

@dataclass
class Session:
    tag: bytes = field(repr=False)        # never printed, even by a debug dump
    thread_id: str
    task_id: str | None
    opened: float
    closed: float | None = None


@dataclass
class _Window:
    thread_id: str
    start: float
    end: float | None = None


def _tag_bytes(tag) -> bytes:
    raw = tag.encode("ascii") if isinstance(tag, str) else bytes(tag)
    if not _TAG.fullmatch(raw):
        raise ValueError("an origin tag is 32 lowercase hex characters")
    return raw


class Sessions:
    """The live origin tags (one per Claude or Codex session) and the turn
    windows of Claude and Codex turns. Times are CLOCK_BOOTTIME seconds,
    passed in. A tag maps to its thread while the session lives and for
    `GRACE_S` after it closes; a turn's window runs from its start to its end
    plus `GRACE_S`."""

    def __init__(self, *, grace: float = GRACE_S, slack: float = START_SLACK_S):
        self.grace = grace
        self.slack = slack
        self._sessions: dict[bytes, Session] = {}
        self._windows: list[_Window] = []
        self._running: dict[str, _Window] = {}

    def open(self, tag, thread_id: str, task_id: str | None = None, *, at: float) -> None:
        raw = _tag_bytes(tag)
        self._sessions[raw] = Session(raw, thread_id, task_id, at)

    def close(self, tag, *, at: float) -> None:
        session = self._sessions.get(_tag_bytes(tag))
        if session is not None and session.closed is None:
            session.closed = at

    def turn_started(self, thread_id: str, provider, *, at: float) -> bool:
        """Record a turn's start; only Claude and Codex turns open a window."""
        if str(getattr(provider, "value", provider)) not in PROVIDERS:
            return False
        if thread_id in self._running:
            self.turn_ended(thread_id, at=at)
        window = _Window(thread_id, at)
        self._windows.append(window)
        self._running[thread_id] = window
        del self._windows[:-WINDOWS_MAX]
        return True

    def turn_ended(self, thread_id: str, *, at: float) -> None:
        window = self._running.pop(thread_id, None)
        if window is not None:
            window.end = at

    def holding(self, first_seen: float, started_at: float) -> frozenset[str]:
        """Rule 7: the threads with a turn window that holds a listener first
        seen at `first_seen`, from a process started at `started_at`."""
        out = set()
        for w in self._windows:
            end = float("inf") if w.end is None else w.end + self.grace
            if w.start <= first_seen <= end and started_at >= w.start - self.slack:
                out.add(w.thread_id)
        return frozenset(out)

    def owner(self, tag: bytes, now: float) -> Session | None:
        """Rule 9: the live session a tag names, or None (unknown, or closed
        more than `GRACE_S` ago)."""
        session = self._sessions.get(tag)
        if session is None:
            return None
        if session.closed is not None and now > session.closed + self.grace:
            return None
        return session

    def live(self, now: float) -> bool:
        """Whether any turn runs or is inside its grace: the watcher polls."""
        return any(w.end is None or now <= w.end + self.grace for w in self._windows)

    def prune(self, now: float) -> None:
        keep = self.grace + PRUNE_MARGIN_S
        self._windows = [w for w in self._windows if w.end is None or now <= w.end + keep]
        self._sessions = {t: s for t, s in self._sessions.items()
                          if s.closed is None or now <= s.closed + self.grace}


# -- attribution (rules 1-11) -------------------------------------------------------------

@dataclass(frozen=True)
class Listener:
    """A row and when the watcher saw it: `first_seen` and `last_seen` are
    CLOCK_BOOTTIME seconds of the first and latest poll that showed it."""

    row: Row
    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class Known:
    """What the daemon knows for certain: rule 1-2's ports, its uid (rule 4),
    Jarvis's own pids (rule 5: the daemon, the provider roots, the Browser's
    Chrome and driver) and the live terminals (rule 6) as (leader pid, which
    is the session id, leader start in ticks; None when not recorded)."""

    ports: PortRules
    uid: int
    jarvis_pids: frozenset[int] = frozenset()
    terminal_leaders: frozenset[tuple[int, int | None]] = frozenset()


@dataclass(frozen=True)
class Probe:
    """What the probe's answer showed, as booleans: never the answer."""

    refusal: str | None
    status_class: int = 0
    html: bool = False
    markup: bool = False
    framable: bool = False
    retry_tls: bool = False


@dataclass(frozen=True)
class Verdict:
    """`attributed` (to `thread_id`/`task_id`, through owning `pid`),
    `refused` (a class), or `pending`: `scan` waits for an fd scan that
    finished (`FD_SCAN_MAX`), `life` for rule 10's second sighting, `probe`
    for rule 11's answer."""

    outcome: str
    refusal: str | None = None
    pending: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    pid: int | None = None
    start: int | None = None

    @property
    def attributed(self) -> bool:
        return self.outcome == "attributed"


def _refused(cls: str) -> Verdict:
    return Verdict("refused", refusal=cls)


def _terminal(f: PidFacts, leaders) -> bool:
    """Rule 6: the process is in a live terminal's session — the number `sid`
    still belongs to that terminal's leader, or to nobody (a session id is not
    reused while a member lives) — or an ancestor is a terminal's leader. An
    ancestor chain too long to read to its end (`truncated`) cannot rule a
    terminal out, so it counts: fail closed."""
    if f.truncated:
        return True
    for leader, leader_start in leaders:
        if f.sid == leader and (leader_start is None or f.leader_start is None
                                or f.leader_start == leader_start):
            return True
        if any(pid == leader and (leader_start is None or start == leader_start)
               for pid, start in f.ancestors):
            return True
    return False


def _identity_refusal(f: PidFacts | None, known: Known) -> str | None:
    """Rules 4-6 for one holding pid: who the process is."""
    if f is None:
        return UNREADABLE
    if f.uid != known.uid:
        return UID
    if f.pid in known.jarvis_pids:
        return JARVIS
    if _terminal(f, known.terminal_leaders):
        return TERMINAL
    return None


def attribute(listener: Listener, facts, sessions: Sessions, now: float, known: Known, *,
              tag_of, probe: Probe | None = None, scan_complete: bool = True) -> Verdict:
    """Rules 1-11 in order (§B.5). `facts` holds one entry per pid found
    holding the listener among the daemon cgroup's processes (None for a pid
    whose facts could not be read); empty is rule 3. `scan_complete` is the fd
    scan's (`Owners.complete`): False means holders may be missing, so nothing
    is attributed this poll (`pending`, "scan").

    **A socket several processes hold fails closed.** If any holder is refused
    as Jarvis's own (rule 5) or as a terminal's (rule 6), the listener is
    refused with that class, even when another holder carries a live tag: a
    shared socket is as much the terminal's or Jarvis's as the agent's. With
    no such holder the earliest-started pid passing rules 4-7 is the owning
    pid, and `tag_of(facts)` — its `environ`, read only now — is consulted for
    it alone. Rules 7 and 9 are judged at the listener's first sighting
    (`now` is the poll's clock and no rule reads it), so a verdict waiting on
    its probe cannot change meanwhile. Rules 10 and 11 answer `pending` until
    the watcher has a second sighting and a probe."""
    cheap = cheap_refusal(listener.row, known.ports, known.uid)   # rules 1, 2, and 4 for the socket
    if cheap is not None:
        return _refused(cheap)
    if not scan_complete:
        return Verdict("pending", pending="scan")
    if not facts:                                             # rule 3
        return _refused(OUTSIDE)
    order = sorted(facts, key=lambda f: (f is None, f.start if f else 0, f.pid if f else 0))
    first = shared = None
    owner, holding = None, frozenset()
    for f in order:                                           # rules 4-7, every holder
        refusal = _identity_refusal(f, known)
        if refusal is None and owner is None:
            holding = sessions.holding(listener.first_seen, f.started_at)    # rule 7
            if holding:
                owner = f
                continue
            refusal = NO_TURN
        if refusal is None:
            continue                                          # a later passing pid: not the owner
        first = first or refusal
        if refusal in (JARVIS, TERMINAL):
            shared = shared or refusal
    if shared is not None:
        return _refused(shared)
    if owner is None:
        return _refused(first)
    tag = tag_of(owner)                                       # rule 8
    if not isinstance(tag, bytes):
        return _refused(tag if tag in (UNTAGGED, UNREADABLE) else UNREADABLE)
    session = sessions.owner(tag, listener.first_seen)        # rule 9
    if session is None or session.thread_id not in holding:
        return _refused(STALE_TAG)
    if listener.last_seen - listener.first_seen < MIN_LIFE_S:  # rule 10
        return Verdict("pending", pending="life", pid=owner.pid, start=owner.start)
    if probe is None:                                         # rule 11
        return Verdict("pending", pending="probe", pid=owner.pid, start=owner.start)
    if probe.refusal is not None:
        return _refused(probe.refusal)
    return Verdict("attributed", thread_id=session.thread_id, task_id=session.task_id,
                   pid=owner.pid, start=owner.start)


# -- the probe (rule 11) -------------------------------------------------------------------

def probe_address(row: Row, ports: PortRules, uid: int | None = None) -> tuple[str, int] | None:
    """Where the probe connects, judged again just before `connect`:
    127.0.0.1 for an IPv4, wildcard or IPv4-mapped bind, ::1 for `::1` or
    `::`. Never a non-loopback address; None for a refused port (and, when
    the daemon's `uid` is passed, for another user's socket)."""
    if cheap_refusal(row, ports, uid) is not None or row.bind not in (LOOPBACK, WILDCARD):
        return None
    if row.family == 4 or row.mapped:
        return "127.0.0.1", row.port
    return "::1", row.port


def probe_request(port: int) -> bytes:
    """The one request the probe sends: no cookie, no credential, no body."""
    return (f"GET / HTTP/1.0\r\nHost: localhost:{int(port)}\r\n"
            "User-Agent: Jarvis-devwatch\r\nAccept: text/html\r\n"
            "Connection: close\r\n\r\n").encode("ascii")


_STATUS = re.compile(r"HTTP/\d(?:\.\d)? ([1-5]\d\d)(?: .*)?")
# A tag name must end: `<header` and `<htmlx` are not `<head` or `<html`.
_MARKUP = re.compile(rb"<(?:!doctype\s+html|html|head|body)(?=[\s>/])", re.IGNORECASE)
_FRAME_OK = frozenset({"*", "http://localhost:*"})


def _framable(headers: list[tuple[str, str]]) -> bool:
    """False when `X-Frame-Options` says DENY or SAMEORIGIN, or any enforced
    CSP's `frame-ancestors` names neither `*` nor `http://localhost:*`."""
    for name, value in headers:
        if name == "x-frame-options":
            if any(tok.strip().lower() in ("deny", "sameorigin") for tok in value.split(",")):
                return False
        elif name == "content-security-policy":
            for one in value.split(","):
                for directive in one.split(";"):
                    words = directive.split()
                    if words and words[0].lower() == "frame-ancestors":
                        if not {w.lower() for w in words[1:]} & _FRAME_OK:
                            return False
    return True


def parse_response(data: bytes) -> Probe:
    """Judge the first `PROBE_READ_MAX` bytes the probe read. A page is a
    3xx; a 2xx that is `text/html`, or untyped with markup in the body; any
    other status only `text/html` with markup. A page that refuses framing is
    `no_frame`. Nothing but status class and booleans survives."""
    data = bytes(data[:PROBE_READ_MAX])
    if not data or data[:1] == b"\x15":           # closed silent, or a TLS alert
        return Probe(NOT_HTTP, retry_tls=True)
    # The header block ends at the earlier blank line, CRLF or bare LF; a body
    # that happens to hold the other kind later is not part of it.
    ends = [(at, gap) for at, gap in ((data.find(b"\r\n\r\n"), 4), (data.find(b"\n\n"), 2))
            if at >= 0]
    if not ends:
        return Probe(NOT_HTTP)                    # garbage, or headers past the cap
    end, gap = min(ends)
    lines = data[:end].decode("latin-1").split("\n")
    status = _STATUS.fullmatch(lines[0].rstrip("\r"))
    if status is None:
        return Probe(NOT_HTTP)
    headers: list[tuple[str, str]] = []
    last = -1                                     # index of the header a fold continues
    for raw in lines[1:]:
        line = raw.rstrip("\r")
        if line[:1] in (" ", "\t"):               # obs-fold (RFC 7230 §3.2.4): joined with one space
            if last >= 0:
                name, value = headers[last]
                headers[last] = (name, (value + " " + line.strip()).strip())
            continue
        name, sep, value = line.partition(":")
        if sep and name:
            headers.append((name.strip().lower(), value.strip()))
            last = len(headers) - 1
        else:
            last = -1                             # a line with no colon: its folds go nowhere
    ctypes = [v for n, v in headers if n == "content-type"]
    ctype = ctypes[-1].split(";")[0].strip().lower() if ctypes else None
    html = ctype == "text/html"
    markup = _MARKUP.search(data, end + gap) is not None
    cls = int(status.group(1)) // 100
    framable = _framable(headers)
    if cls == 3:
        page = True
    elif cls == 2:
        page = html or (ctype is None and markup)
    else:
        page = html and markup
    if not page:
        refusal = NOT_PAGE
    elif not framable:
        refusal = NO_FRAME
    else:
        refusal = None
    return Probe(refusal, cls, html, markup, framable)


# -- the claim (§D.4) ----------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    """One window's claim: `window` its page nonce, `shows` whether a drawn
    pane shows the source, `focused_at` epoch ms of its last focus (0 for
    never), `at` when the claim arrived (seconds, any monotonic clock)."""

    window: str
    shows: bool
    focused_at: int
    at: float


def _valid_claim(c) -> bool:
    """A claim whose every field is of its type and in range. Claims come
    from windows over HTTP, so nothing is trusted to be shaped as declared."""
    return (isinstance(c, Claim)
            and isinstance(c.window, str) and 0 < len(c.window) <= WINDOW_ID_MAX
            and isinstance(c.shows, bool)
            and isinstance(c.focused_at, int) and not isinstance(c.focused_at, bool)
            and 0 <= c.focused_at <= FOCUSED_AT_MAX
            and isinstance(c.at, (int, float)) and not isinstance(c.at, bool)
            and math.isfinite(c.at))


def arbitrate(claims, window_ms: float = ARBITRATE_MS) -> Claim | None:
    """The one window that acts: among the claims that arrived within
    `window_ms` of the first, the earliest that shows the source; else the
    newest `focused_at`; ties to the earliest. A claim after that — or a
    window's second claim — loses. A malformed claim (a field of the wrong
    type or out of range, or not a `Claim`) is dropped before any of that,
    never raised on and never the one that opens the window."""
    ordered = sorted((c for c in claims if _valid_claim(c)), key=lambda c: c.at)
    if not ordered:
        return None
    cutoff = ordered[0].at + window_ms / 1000.0
    eligible, windows = [], set()
    for claim in ordered:
        if claim.at <= cutoff and claim.window not in windows:
            windows.add(claim.window)
            eligible.append(claim)
    for claim in eligible:
        if claim.shows:
            return claim
    best = eligible[0]
    for claim in eligible[1:]:
        if claim.focused_at > best.focused_at:
            best = claim
    return best

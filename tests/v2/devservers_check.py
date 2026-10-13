"""AO-1b-i: fixture proc trees, fixed free loopback ports, port-0 daemons.

Never reads a live environ. Fake CLIs record their own inherited tag, and
all HOME/config paths and child artifacts belong to this suite's temp tree.
Run each daemon/Chrome suite separately; this one launches no Chrome.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import asyncio
import http.client
import json
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import config
from jarvis.v2 import devlisten as dl, devservers as ds
from jarvis.v2.daemon import Daemon
from jarvis.v2.model import ProviderName as PN, Role, Thread
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage
from jarvis.v2.stores import Stores
from devlisten_check import Fake as ProcTree, line, hex4

W1, W2 = "a" * 32, "b" * 32
SECRET = "FAKE-ENV-SECRET-DO-NOT-LOG"


def eventually(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("fixture condition did not settle")


class Clock:
    now = 1000.0
    def __call__(self):
        return self.now


class FakeProvider:
    def health(self):
        return True, "fixture"
    def start(self, thread, brief, permit):
        return SessionHandle(thread.id, thread.provider, "fixture", SimpleNamespace(brief=brief))
    resume = start
    def usage(self, handle):
        return Usage()
    def send(self, handle, message):
        yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})
    def interrupt(self, handle):
        pass
    def close(self, handle):
        pass


class Server:
    """Count real accepts and capture the one request, without logging it."""
    def __init__(self, reply=b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html>fixture</html>",
                 *, silent=False, stream=False):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for port in range(20000, 30000):
            try:
                self.sock.bind(("127.0.0.1", port))
                break
            except OSError:
                continue
        else:
            raise AssertionError("no fixed fixture port free")
        self.port = port
        self.sock.listen()
        self.sock.settimeout(.05)
        self.reply, self.silent, self.stream = reply, silent, stream
        self.accepts = 0
        self.requests = []
        self.closed = threading.Event()
        self.clients = []
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
    def run(self):
        while not self.closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepts += 1
            self.clients.append(conn)
            threading.Thread(target=self.answer, args=(conn,), daemon=True).start()
    def answer(self, conn):
        try:
            conn.settimeout(2)
            self.requests.append(conn.recv(4096))
            if self.silent:
                self.closed.wait(3)
            else:
                conn.sendall(self.reply)
                if self.stream:
                    while not self.closed.is_set():
                        conn.sendall(b"x" * 4096)
        except OSError:
            pass
        finally:
            conn.close()
    def close(self):
        self.closed.set()
        self.sock.close()
        for c in self.clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        self.worker.join(1)


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="ao1bi-")))
        self.home = self.root / "home"
        self.home.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.root / "xdg"),
            "JARVIS_CLAUDE_CREDENTIALS": str(self.root / "none.json")}))
        for name in vars(config):
            if name.endswith("_PATH"):
                self.stack.enter_context(patch.object(config, name, self.root / (name.lower() + ".json")))
        for name in ("V2_DATA_DIR", "HUD_DIR", "WORKSHOP_DIR", "SETTINGS_DIR"):
            if hasattr(config, name):
                self.stack.enter_context(patch.object(config, name, self.root / name.lower()))
        self.stack.enter_context(patch.object(config, "REPO_ROOT", self.root))
        self.tree = ProcTree(self.root)
        (self.tree.proc / "self" / "cgroup").write_text("0::/fixture\n")
        self.tree.cgroup("/fixture", [])
        self.clock = Clock()
        self.daemon = Daemon(Stores(self.root / "data"), {p: FakeProvider() for p in PN},
                             lambda *_: lambda *_: Decision.DENY, 0)
        self.watch = ds.DevServers(self.daemon, proc_root=self.tree.proc,
                                   cgroup_root=self.tree.cg, clock=self.clock)
        self.daemon.devservers = self.watch
        # Most race/work tests drive individual polls. The lazy-thread check
        # below uses the real thread. This avoids concurrent fixture rewrites.
        self.auto_thread = self.stack.enter_context(patch.object(self.watch, "_ensure_thread"))
        self.records = []
        self.daemon.bus.observe(self.records.append)
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.project = self.daemon.stores.projects.create("fixture", str(self.root))
        self.thread = self.daemon.open_thread(self.project.id, Role.IMPLEMENTER, PN.CLAUDE,
                                              Brief(Role.IMPLEMENTER, str(self.root)))
        self.tag = self.daemon._sessions[self.thread.id].origin_tag
        self.watch.turn_started(self.thread.id, PN.CLAUDE)
        self.watch.poll()                             # baseline before server starts
        self.records.clear()
        self.logs = []
        class Capture(logging.Handler):
            def emit(_, record):
                self.logs.append(record.getMessage())
        capture = Capture()
        logging.getLogger().addHandler(capture)
        self.addCleanup(logging.getLogger().removeHandler, capture)
        self.log_path = self.daemon.stores.threads.path(self.thread.id).with_name("log.jsonl")
        self.before_log = self.log_path.read_bytes() if self.log_path.exists() else b""

    def server(self, **kwargs):
        s = Server(**kwargs)
        self.addCleanup(s.close)
        return s

    def add_listener(self, s, *, pid=700, inode=10, tag=True, start=100000, pids=None):
        fd_dir = self.tree.proc / str(pid) / "fd"
        if fd_dir.exists():
            for link in fd_dir.iterdir():
                link.unlink()
        self.tree.process(pid, start=start, environ=(b"OPENROUTER_API_KEY=" + SECRET.encode() + b"\0"
                          + (dl.TAG_NAME + self.tag.encode() + b"\0" if tag else b"")),
                          fds=[f"socket:[{inode}]"], cwd="/tmp")
        self.tree.cgroup("/fixture", [pid] if pids is None else pids)
        self.tree.net([line(0, hex4("127.0.0.1"), s.port, inode)], [])
        return pid

    def finish_probe(self):
        eventually(lambda: not self.watch._probes)
        self.watch.poll()

    def discover(self, s, **kwargs):
        self.add_listener(s, **kwargs)
        self.watch.poll()
        self.clock.now += 2
        self.watch.poll()
        self.finish_probe()
        return self.watch.listing()["servers"]

    def req(self, method, path="/dev-servers", body=None, *, port=None, origin="owner", headers=None):
        port = self.daemon.face_port if port is None else port
        h = {"Content-Type": "application/json"}
        if origin == "owner":
            h["Origin"] = f"http://127.0.0.1:{port}"
        elif origin is not None:
            h["Origin"] = origin
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, h)
            response = conn.getresponse()
            status, raw, hs = response.status, response.read(), dict(response.getheaders())
            self.assertEqual(hs.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", hs.get("Content-Security-Policy", ""))
            return status, json.loads(raw)
        finally:
            conn.close()

    def entry(self, *, window=None, terminal="fixture-terminal", port=23456):
        ident = self.watch.announce_terminal(terminal, f"http://localhost:{port}/fixture-path?drop=1", window=window)
        self.assertIsNotNone(ident)
        return ident

    def claim(self, ident, window=W1, shows=False, focus=0):
        return self.req("POST", f"/dev-servers/{ident}/claim",
                        {"window": window, "shows": shows, "focused_at": focus})


class Watch(Base):
    def test_fake_shell_terminal_tag_is_absent_and_its_server_is_excluded(self):
        with patch.object(config, "TERMINAL_SHELL", str(Path(__file__).resolve().parent / "terminal_fake" / "sh")), \
             patch.dict(os.environ, {"JARVIS_ORIGIN_TAG": self.tag}):
            terminal = self.daemon.terminals.create(str(self.root), project_id=self.project.id, label="fixture")
        self.addCleanup(self.daemon.terminals.close, terminal.id)
        terminal.queue_input(b"names\r")
        found = eventually(lambda: re.search(rb"NAMES (.*?) END", terminal._ring.snapshot()[1]))
        self.assertNotIn(b"JARVIS_ORIGIN_TAG", found.group(1).split())
        s = self.server()
        leader, start = terminal._proc.pid, terminal._start_time
        self.add_listener(s, start=start + 1)
        self.tree.process(leader, start=start)
        self.tree.process(700, start=start + 1, sid=leader, ppid=leader,
                          environ=dl.TAG_NAME + self.tag.encode() + b"\0")
        before = self.watch.reader.work.environ_reads
        self.watch.poll()
        self.clock.now += 2
        self.watch.poll()
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.reader.work.environ_reads, before)
        self.assertEqual(self.watch.status()["refused"].get("terminal"), 1)

    def test_scripted_agent_turn_starts_real_detached_server(self):
        reserve = self.server()
        port = reserve.port
        reserve.close()
        artifact = self.root / "server-facts.json"
        done = threading.Event()
        children = []
        self.addCleanup(done.set)
        def cleanup():
            done.set()
            for child in children:
                child.terminate()
                child.wait(timeout=3)
        self.addCleanup(cleanup)
        def send(handle, message):
            env = {**os.environ, "JARVIS_ORIGIN_TAG": handle.native.brief.origin_tag}
            child = subprocess.Popen(["nohup", sys.executable, "-B", str(Path(__file__).resolve()),
                                      "--listen", str(port), str(artifact)], env=env,
                                     cwd="/tmp", start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children.append(child)
            eventually(artifact.exists)
            done.wait(3)
            yield Event(K.TURN_FINISHED, handle.thread_id, {"stop": "end"})
        provider = self.daemon.providers[PN.CLAUDE]
        with patch.object(provider, "send", side_effect=send):
            th = self.daemon.open_thread(self.project.id, Role.IMPLEMENTER, PN.CLAUDE,
                                         Brief(Role.IMPLEMENTER, str(self.root)))
            self.daemon.send(th.id, UserMessage("start fixture"))
            eventually(artifact.exists)
            self.assertEqual(json.loads(artifact.read_text())["tag"], self.daemon._sessions[th.id].origin_tag)
            child = children[0]
            self.tree.process(child.pid, start=100000,
                              environ=dl.TAG_NAME + self.daemon._sessions[th.id].origin_tag.encode() + b"\0",
                              fds=["socket:[999]"])
            self.tree.cgroup("/fixture", [child.pid])
            self.tree.net([line(0, hex4("127.0.0.1"), port, 999)], [])
            self.watch.poll()
            self.clock.now += 2
            self.watch.poll()
            self.finish_probe()
            entry = self.watch.listing()["servers"][0]
            self.assertEqual(entry["source"]["thread_id"], th.id)
            self.assertEqual(entry["url"], f"http://localhost:{port}/")
            self.assertEqual(json.loads(artifact.read_text())["accepts"], 1)
            self.assertEqual(sum(r["kind"] == "dev_server" for r in self.records), 1)
            done.set()
            eventually(lambda: self.daemon._sessions[th.id].worker is None)

    def test_tagged_page_published_once_and_exact_bus_keys(self):
        s = self.server()
        entries = self.discover(s)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], f"http://localhost:{s.port}/")
        self.assertEqual(s.accepts, 1)
        self.assertEqual(s.requests, [dl.probe_request(s.port)])
        self.assertEqual(len(self.records), 1)
        self.assertEqual(set(self.records[0]), {"kind", "data"})
        self.assertEqual(set(self.records[0]["data"]), {"id", "state", "source", "at"})
        self.assertEqual(self.records[0]["data"]["source"], {
            "kind": "thread", "thread_id": self.thread.id, "task_id": None})
        self.assertEqual(self.records[0]["kind"], "dev_server")
        self.assertEqual(self.records[0]["data"]["state"], "up")
        for _ in range(3):
            self.watch.poll()
        self.assertEqual(len(self.records), 1)
        status, listing = self.req("GET")
        self.assertEqual((status, listing["servers"][0]["id"]), (200, entries[0]["id"]))
        self.assertEqual(self.claim(entries[0]["id"])[1], {"won": True, "url": entries[0]["url"]})

    def test_untagged_is_never_probed_or_published(self):
        s = self.server()
        self.assertEqual(self.discover(s, tag=False), [])
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.status()["refused"].get("untagged"), 1)

    def test_jarvis_terminal_and_shared_holders_are_refused(self):
        for mode in ("jarvis", "terminal", "shared", "unreadable"):
            with self.subTest(mode=mode):
                s = self.server()
                self.clock.now += 10
                self.add_listener(s, inode=s.port)
                if mode in ("jarvis", "terminal"):
                    known = ({700}, set(), None) if mode == "jarvis" else (set(), {(700, 100000)}, None)
                else:
                    known = ({701}, set(), None) if mode == "shared" else (set(), set(), None)
                    shared_fd = self.tree.proc / "701" / "fd" / "3"
                    if shared_fd.is_symlink():
                        shared_fd.unlink()
                    self.tree.process(701, start=100001, fds=[f"socket:[{s.port}]"],
                                      environ=b"", ppid=999 if mode == "unreadable" else 1)
                    self.tree.cgroup("/fixture", [700, 701])
                with patch.object(self.daemon, "_dev_known", return_value=known):
                    for _ in range(4):
                        self.watch.poll()
                        self.clock.now += 2
                self.assertEqual(s.accepts, 0)
                self.assertEqual(self.watch.listing()["servers"], [])

    def test_control_service_ephemeral_uid_and_odd_bind_no_accept(self):
        for mode in ("control", "workshop", "gate", "service", "ephemeral", "uid", "odd"):
            with self.subTest(mode=mode):
                s = self.server()
                self.add_listener(s, inode=s.port)
                self.clock.now += 10
                with ExitStack() as stack:
                    if mode == "control":
                        stack.enter_context(patch.object(config, "control_ports", return_value={s.port}))
                    if mode == "workshop":
                        stack.enter_context(patch.object(self.daemon, "workshop_port", s.port))
                    if mode == "gate":
                        stack.enter_context(patch.object(self.daemon, "_dev_known", return_value=(set(), set(), s.port)))
                    if mode == "service":
                        original = dl.port_rules
                        stack.enter_context(patch.object(dl, "port_rules", side_effect=lambda **kw: original(**kw, service=frozenset({s.port}))))
                    if mode == "ephemeral":
                        stack.enter_context(patch.object(self.watch, "_ephemeral", (s.port, s.port)))
                    if mode in ("uid", "odd"):
                        self.tree.net([line(0, hex4("127.0.0.5" if mode == "odd" else "127.0.0.1"),
                                             s.port, s.port, uid=os.getuid() + 1 if mode == "uid" else os.getuid())], [])
                    for _ in range(3):
                        self.watch.poll()
                        self.clock.now += 2
                self.assertEqual(s.accepts, 0)
                self.assertEqual(self.watch.listing()["servers"], [])

    def test_outside_grace_and_fast_turn_no_accept(self):
        s = self.server()
        self.add_listener(s, pids=[])
        for _ in range(4):
            self.watch.poll()
            self.clock.now += 2
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.status()["refused"].get("outside"), 1)
        self.watch.turn_ended(self.thread.id)
        self.clock.now += 601
        self.watch.turn_started("fast", PN.FAST)
        count = self.watch.status()["polls"]
        self.watch.poll()
        self.assertEqual(self.watch.status()["polls"], count)
        self.assertEqual(s.accepts, 0)

    def test_transient_missing_parent_is_retried(self):
        s = self.server()
        self.add_listener(s)
        self.tree.process(700, start=100000, ppid=699,
                          environ=dl.TAG_NAME + self.tag.encode() + b"\0")
        self.watch.poll()
        self.assertEqual(s.accepts, 0)
        self.tree.process(699, start=99999)
        self.clock.now += 2
        self.watch.poll()
        self.finish_probe()
        self.assertEqual(len(self.watch.listing()["servers"]), 1)

    def test_pid_reused_between_sightings(self):
        s = self.server()
        self.add_listener(s)
        self.watch.poll()
        self.tree.restart(700, 100100)
        self.clock.now += 2
        self.watch.poll()
        self.finish_probe()
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.listing()["servers"], [])

    def test_recheck_identity_and_inode_immediately_before_publish(self):
        for mode in ("reuse", "fd"):
            with self.subTest(mode=mode):
                s = self.server()
                self.clock.now += 10
                self.add_listener(s, inode=s.port)
                self.watch.poll()
                self.clock.now += 2
                self.watch.poll()
                eventually(lambda: not self.watch._probes)
                if mode == "reuse":
                    self.tree.restart(700, 100100)
                else:
                    (self.tree.proc / "700" / "fd" / "3").unlink()
                self.watch.poll()
                self.assertEqual(self.watch.listing()["servers"], [])
                self.assertEqual(self.records, [])

    def test_port_class_rechecked_between_cheap_refusal_and_connect(self):
        server = self.server()
        self.add_listener(server)
        self.watch.poll()  # passes the cheap refusal while this is a dev port
        candidate = self.watch._candidates[(10, server.port)]
        self.assertFalse(candidate.done)
        self.clock.now += 2
        exchange = self.watch._exchange
        def become_gate(c, tls=False):
            with patch.object(self.daemon.browser, "_gate", SimpleNamespace(port=server.port)):
                return exchange(c, tls=tls)
        with patch.object(self.watch, "_exchange", side_effect=become_gate):
            self.watch.poll()
            self.finish_probe()
        self.assertEqual(server.accepts, 0)
        self.assertEqual(self.watch.status()["work"]["probes"], 0)
        self.assertEqual(self.watch.status()["refused"].get(dl.JARVIS_PORT), 1)
        self.assertEqual(self.watch.listing()["servers"], [])

    def test_slow_proc_scan_does_not_block_hooks_status_or_claim_routes(self):
        server = self.server()
        self.add_listener(server)
        entered = threading.Event()
        scan = self.watch.reader.socket_owners
        def slow_scan(*args, **kwargs):
            entered.set()
            time.sleep(1)
            return scan(*args, **kwargs)
        ident = self.entry(window=W1)
        worker = threading.Thread(target=self.watch.poll)
        with patch.object(self.watch.reader, "socket_owners", side_effect=slow_scan):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                actions = [lambda: self.watch.turn_started(self.thread.id, PN.CLAUDE),
                           lambda: self.watch.turn_ended(self.thread.id), self.watch.status,
                           lambda: self.watch.session_opened("another"),
                           lambda: self.watch.session_closed(self.tag),
                           lambda: self.watch.act(ident, "renew", W1),
                           lambda: self.watch.claim(ident, W2, False, 0), self.watch.listing]
                for action in actions:
                    began = time.monotonic()
                    action()
                    self.assertLess(time.monotonic() - began, .05)
            finally:
                worker.join(4)
            self.assertFalse(worker.is_alive())

    def test_apply_rechecks_candidate_and_sessions_after_proc_scan(self):
        server = self.server()
        self.add_listener(server)
        self.watch.poll()
        self.clock.now += 2
        scan = self.watch._still_holds
        def replace_candidate(*args):
            result = scan(*args)
            with self.watch._lock:
                self.watch._candidates.clear()
            return result
        with patch.object(self.watch, "_still_holds", side_effect=replace_candidate):
            self.watch.poll()
        eventually(lambda: not self.watch._probes)
        self.assertEqual(server.accepts, 0)
        self.assertEqual(self.watch.listing()["servers"], [])
        self.watch.poll()
        self.clock.now += 2
        def remove_session(*args):
            result = scan(*args)
            with self.watch._lock:
                self.watch.sessions = dl.Sessions()
            return result
        with patch.object(self.watch, "_still_holds", side_effect=remove_session):
            self.watch.poll()
        eventually(lambda: not self.watch._probes)
        self.assertEqual(server.accepts, 0)
        self.assertEqual(self.watch.listing()["servers"], [])

    def test_page_and_framing_refusals_redirect_and_read_cap(self):
        target = self.server()
        replies = [b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{}",
                   b"HTTP/1.0 404 Nope\r\nContent-Type: text/html\r\n\r\nmissing",
                   b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nX-Frame-Options: DENY\r\n\r\n<html>",
                   b"HTTP/1.0 200 OK\r\nContent-Security-Policy: frame-ancestors 'none'\r\n\r\n<html>"]
        for reply in replies:
            s = self.server(reply=reply)
            self.clock.now += 10
            self.assertEqual(self.discover(s, inode=s.port), [])
            self.assertEqual(s.accepts, 1)
        s = self.server(reply=f"HTTP/1.0 302 Found\r\nLocation: http://localhost:{target.port}/private\r\n\r\n".encode())
        self.clock.now += 10
        self.assertEqual(len(self.discover(s, inode=s.port)), 1)
        self.assertEqual(target.accepts, 0)
        # Streaming non-pages close at 4 KiB instead of draining forever.
        s = self.server(reply=b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n", stream=True)
        self.clock.now += 10
        self.discover(s, inode=s.port)
        self.assertEqual(s.accepts, 1)
        self.assertNotIn(s.port, [e.port for e in self.watch._entries.values() if e.listening])

    def test_brief_listener_and_probe_concurrency_timeout_retry(self):
        s = self.server()
        self.add_listener(s)
        self.watch.poll()
        self.tree.net([], [])
        self.clock.now += 1
        self.watch.poll()
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.status()["refused"].get("brief"), 1)
        servers = [self.server(silent=True) for _ in range(3)]
        rows = []
        for index, server in enumerate(servers):
            self.tree.process(710 + index, start=100000, environ=dl.TAG_NAME + self.tag.encode() + b"\0",
                              fds=[f"socket:[{20 + index}]"])
            rows.append(line(index, hex4("127.0.0.1"), server.port, 20 + index))
        self.tree.cgroup("/fixture", [710, 711, 712])
        self.tree.net(rows, [])
        with patch.object(dl, "PROBE_RESPONSE_S", .1):
            self.watch.poll()
            self.clock.now += 2
            self.watch.poll()
            self.assertLessEqual(len(self.watch._probes), 2)
            eventually(lambda: not self.watch._probes)
            self.clock.now += 31
            self.watch.poll()
        self.assertEqual(self.watch.listing()["servers"], [])
        self.assertEqual(self.watch.status()["work"]["probes"], 2)

    def test_tls_fallback_on_silent_close_and_alert(self):
        # Exercise orchestration separately from the TLS wire test below.
        for first in (b"", b"\x15\x03\x03"):
            c = ds.Candidate(dl.Listener(dl.Row(4, dl.LOOPBACK, 23456, 1, os.getuid()), 1000, 1002))
            with patch.object(self.watch, "_exchange", side_effect=[dl.parse_response(first),
                             dl.parse_response(b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n")]) as exchange:
                self.watch._probe(c)
                self.assertEqual(exchange.call_count, 2)
                self.assertEqual(exchange.call_args.kwargs, {"tls": True})
                self.assertEqual(c.scheme, "https")

    def test_real_tls_probe_after_plain_close(self):
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        s = self.server()
        def answer(conn):
            try:
                conn.settimeout(1)
                if conn.recv(1, socket.MSG_PEEK) == b"G":
                    conn.recv(4096)
                    return                          # plaintext: silent close requests TLS
                with context.wrap_socket(conn, server_side=True) as tls:
                    s.requests.append(tls.recv(4096))
                    tls.sendall(s.reply)
            except OSError:
                pass
            finally:
                conn.close()
        s.answer = answer
        entries = self.discover(s)
        self.assertEqual(entries[0]["url"], f"https://localhost:{s.port}/")
        self.assertEqual(s.accepts, 2)
        self.assertEqual(s.requests, [dl.probe_request(s.port)])

    def test_probe_connect_retry_backoff_and_budget(self):
        s = self.server()
        self.add_listener(s)
        self.watch.poll()
        self.clock.now += 2
        with patch.object(self.watch, "_exchange", side_effect=ConnectionRefusedError) as exchange:
            for delay in (1, 2, 4, 8):
                self.watch.poll()
                eventually(lambda: not self.watch._probes)
                c = self.watch._candidates[(10, s.port)]
                self.assertEqual(c.next_probe, self.clock.now + delay)
                count = exchange.call_count
                self.watch.poll()
                self.assertEqual(exchange.call_count, count)
                self.clock.now += delay
            self.clock.now = 1031
            self.watch.poll()
            self.assertEqual(exchange.call_count, 4)
            self.assertTrue(c.done)
            self.watch.poll()
            self.assertEqual(exchange.call_count, 4)

    def test_probe_crossing_closed_session_grace_keeps_first_sighting(self):
        self.watch.turn_ended(self.thread.id)
        self.watch.session_closed(self.tag)
        self.clock.now = 1599
        s = self.server()
        self.add_listener(s)
        self.watch.poll()
        self.clock.now += 2
        self.watch.poll()
        self.finish_probe()
        self.assertEqual(len(self.watch.listing()["servers"]), 1)

    def test_browser_descendants_known_without_cmdline_or_environ(self):
        s = self.server()
        self.add_listener(s)
        self.tree.process(700, start=100000, ppid=699, environ=dl.TAG_NAME + self.tag.encode() + b"\0")
        self.tree.process(699, start=99999)
        before = self.watch.reader.work.environ_reads
        with patch.object(self.daemon, "_dev_browser_roots", return_value={699}):
            self.watch.poll()
            self.clock.now += 2
            self.watch.poll()
        self.assertEqual(s.accepts, 0)
        self.assertEqual(self.watch.reader.work.environ_reads, before)
        self.assertEqual(self.watch.status()["refused"].get("jarvis"), 1)

    def test_down_restart_keeps_id_first_source_and_dismissal(self):
        s = self.server()
        entry = self.discover(s)[0]
        self.req("POST", f"/dev-servers/{entry['id']}/dismiss", {"window": W2})
        self.tree.net([], [])
        self.watch.poll()
        self.assertFalse(any(r["data"]["state"] == "down" for r in self.records))
        self.watch.poll()
        self.assertEqual(self.records[-1]["data"]["state"], "down")
        self.clock.now += 10
        self.discover(s, inode=11)
        self.assertEqual(self.watch._entries[s.port].id, entry["id"])
        self.assertEqual(self.watch._entries[s.port].state, "dismissed")
        self.assertEqual(sum(r["data"]["state"] == "up" for r in self.records), 1)
        self.assertEqual(self.watch.announce_terminal("other", f"http://localhost:{s.port}/other"), entry["id"])
        self.assertEqual(self.watch._entries[s.port].source["kind"], "thread")

    def test_auto_open_off_no_work(self):
        self.daemon.browser_settings.update(auto_open=False)
        work = self.watch.reader.work.as_dict()
        s = self.server()
        self.add_listener(s)
        self.watch.turn_started(self.thread.id, PN.CODEX)
        self.watch.poll()
        self.assertEqual(self.watch.reader.work.as_dict(), work)
        self.assertEqual(s.accepts, 0)
        self.assertIsNone(self.watch._thread)
        self.assertIsNone(self.watch.announce_terminal("t", f"http://localhost:{s.port}/"))

    def test_work_counts_1000_processes_500_rows_steady_two_reads(self):
        pids = list(range(10000, 11000))
        for pid in pids:
            self.tree.process(pid, start=100000, fds=[f"socket:[{pid}]"])
        self.tree.cgroup("/fixture", pids)
        rows = [line(i, hex4("127.0.0.1"), 40000 + i, 10000 + i) for i in range(500)]
        self.tree.net(rows, [])
        self.watch.poll()
        before = self.watch.reader.work.as_dict()
        self.watch.poll()
        after = self.watch.reader.work.as_dict()
        self.assertEqual(after["files"] - before["files"], 2)
        self.assertEqual(after["fd_readlinks"] - before["fd_readlinks"], 0)
        self.assertEqual(after["environ_reads"] - before["environ_reads"], 0)
        self.tree.process(10000, start=100000, environ=dl.TAG_NAME + self.tag.encode() + b"\0")
        rows.append(line(501, hex4("127.0.0.1"), 23456, 10000))
        self.tree.net(rows, [])
        self.watch.poll()
        after_new = self.watch.reader.work.as_dict()
        self.assertLessEqual(after_new["fd_readlinks"] - after["fd_readlinks"], 1000)
        self.assertEqual(after_new["environ_reads"] - after["environ_reads"], 1)

    def test_status_counts_no_log_no_thread_log_no_secret(self):
        s = self.server()
        self.discover(s)
        status, data = self.req("GET", "/status", port=self.daemon.port)
        view = data["dev_watch"]
        self.assertEqual(set(view), {"active", "entries", "polls", "errors", "refused", "work"})
        self.assertEqual(view["errors"], {})
        self.assertEqual(set(view["work"]), {"rows", "fd_readlinks", "environ_reads", "probes"})
        self.assertEqual(view["entries"], 1)
        self.assertEqual(view["work"]["probes"], 1)
        self.assertEqual(view["work"]["environ_reads"], 1)
        # Invalid bodies and malicious path/query text must stay out of logs.
        marker = "private-title-and-path-marker"
        self.req("POST", f"/dev-servers/{marker}/claim?url=http://localhost:{s.port}/{marker}", {})
        self.assertNotIn(marker, "\n".join(self.logs))
        self.assertNotIn(str(s.port), "\n".join(self.logs))
        self.assertNotIn(SECRET, json.dumps([self.records, data, self.logs]))
        self.assertNotIn(self.tag, json.dumps([self.records, data, self.logs]))
        after = self.log_path.read_bytes() if self.log_path.exists() else b""
        self.assertEqual(after, self.before_log)
        q = self.daemon.bus.subscribe({"thread_id": self.thread.id})
        self.watch.announce_terminal("t", "http://localhost:23457/private")
        self.assertTrue(q.empty())
        self.daemon.bus.unsubscribe(q)
        from jarvis.v2.discord.mirror import KINDS
        self.assertNotIn("dev_server", KINDS)       # the actual mirror's subscription ignores it


class Routes(Base):
    def test_every_route_owner_only_api_foreign_null_missing_and_rebinding(self):
        ident = self.entry()
        paths = [("GET", "/dev-servers", None)] + [("POST", f"/dev-servers/{ident}/{verb}",
                 {"window": W1, **({"shows": False, "focused_at": 0} if verb == "claim" else {})})
                 for verb in ("claim", "renew", "placed", "dismiss")]
        for method, path, body in paths:
            with self.subTest(path=path):
                self.assertEqual(self.req(method, path, body, port=self.daemon.port)[0], 403)
                for origin in ("http://foreign.invalid", "null", None):
                    self.assertEqual(self.req(method, path, body, origin=origin)[0], 403)
                self.assertEqual(self.req(method, path, body, origin="http://evil.invalid",
                                 headers={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.req("GET", origin=None, headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.watch._entries[23456].state, "up")

    def test_claim_validation_precedes_arbitrate_and_all_fields(self):
        ident = self.entry()
        good = {"window": W1, "shows": True, "focused_at": 0}
        bad = [{}, [], {**good, "extra": 1}, {**good, "window": "bad"}, {**good, "window": 1},
               {**good, "shows": 1}, {**good, "shows": "yes"}, {**good, "focused_at": True},
               {**good, "focused_at": -1}, {**good, "focused_at": dl.FOCUSED_AT_MAX + 1},
               {**good, "focused_at": 1.5}, {**good, "focused_at": "x"}, {**good, "focused_at": None}]
        with patch.object(ds.dl, "arbitrate", wraps=dl.arbitrate) as arb:
            for body in bad:
                self.assertEqual(self.req("POST", f"/dev-servers/{ident}/claim", body)[0], 400)
            self.assertEqual(arb.call_count, 0)
            self.assertEqual(self.claim(ident)[0], 200)
            self.assertEqual(arb.call_count, 1)
        for verb in ("renew", "placed", "dismiss"):
            for body in ({}, {"window": "bad"}, {"window": W1, "extra": 1}):
                self.assertEqual(self.req("POST", f"/dev-servers/{ident}/{verb}", body)[0], 400)

    def test_claim_arbitration_routes_showing_window_beats_focus(self):
        ident = self.entry()
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.claim, ident, W1, False, 999999)
            time.sleep(.3)
            second = pool.submit(self.claim, ident, W2, True, 0)
            self.assertEqual(first.result(), (200, {"won": False}))
            self.assertTrue(second.result()[1]["won"])
        self.assertEqual(self.claim(ident, W1, True, dl.FOCUSED_AT_MAX)[1], {"won": False})
        self.assertEqual(self.req("GET", f"/dev-servers?window={W2}")[1]["servers"][0]["mine"], True)

    def test_claim_renew_placed_dismiss_cycle(self):
        ident = self.entry()
        self.assertTrue(self.claim(ident)[1]["won"])
        self.clock.now += 80
        self.assertEqual(self.req("POST", f"/dev-servers/{ident}/renew", {"window": W2})[0], 409)
        self.assertEqual(self.req("POST", f"/dev-servers/{ident}/renew", {"window": W1})[0], 200)
        self.clock.now += 80
        self.assertEqual(self.watch._entry(ident).state, "claimed")
        self.assertEqual(self.req("POST", f"/dev-servers/{ident}/placed", {"window": W1})[0], 200)
        self.clock.now += 100
        self.assertEqual(self.watch._entry(ident).state, "placed")
        self.assertEqual(self.claim(ident, W2)[1], {"won": False})
        self.assertEqual(self.req("POST", f"/dev-servers/{ident}/dismiss", {"window": W2})[0], 200)
        self.assertEqual(self.claim(ident)[0], 409)
        self.assertEqual(self.watch.listing(), {"servers": []})
        self.assertEqual([r["data"]["state"] for r in self.records], ["up", "claimed", "placed", "dismissed"])
        self.assertEqual(self.claim("ds_deadbeef")[0], 404)

    def test_unrenewed_lease_lapses_and_publishes_up(self):
        ident = self.entry()
        self.claim(ident)
        self.clock.now += 90
        self.assertEqual(self.req("GET")[1]["servers"][0]["state"], "up")
        self.assertEqual(self.records[-1]["data"]["state"], "up")
        self.assertTrue(self.claim(ident, W2)[1]["won"])

    def stream(self, window, port=None, origin=None):
        port = self.daemon.face_port if port is None else port
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        headers = "Sec-Fetch-Site: same-origin\r\n" if origin is None else f"Origin: {origin}\r\n"
        sock.sendall(f"GET /events?window={window} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n{headers}\r\n".encode())
        expected = b"403" if origin == "http://foreign.invalid" else b"200"
        self.assertIn(expected, sock.recv(4096))
        return sock

    def test_owner_sse_end_lapses_api_or_foreign_window_does_nothing(self):
        ident = self.entry()
        self.claim(ident)
        for kwargs in ({"port": self.daemon.port}, {"origin": "http://foreign.invalid"}):
            stream = self.stream(W1, **kwargs)
            stream.shutdown(socket.SHUT_RDWR)
            stream.close()
            time.sleep(.7)
            self.assertEqual(self.watch._entry(ident).state, "claimed")
        stream = self.stream(W1)
        stream.shutdown(socket.SHUT_RDWR)
        stream.close()
        eventually(lambda: self.watch._entry(ident).state == "up", timeout=3)
        self.assertEqual(self.records[-1]["data"]["state"], "up")

    def test_last_stream_only_lapses_its_window_claims(self):
        first = self.entry(window=W1)
        second = self.entry(window=W2, terminal="other-terminal", port=23457)
        streams = [self.stream(W1), self.stream(W1), self.stream(W2)]
        for stream in streams:
            self.addCleanup(stream.close)
        eventually(lambda: self.daemon._dev_streams == {W1: 2, W2: 1})
        streams[0].shutdown(socket.SHUT_RDWR)
        streams[0].close()
        eventually(lambda: self.daemon._dev_streams == {W1: 1, W2: 1})
        self.assertEqual(self.watch._entry(first).state, "claimed")
        self.assertEqual(self.watch._entry(second).state, "claimed")
        streams[1].shutdown(socket.SHUT_RDWR)
        streams[1].close()
        eventually(lambda: self.watch._entry(first).state == "up")
        self.assertEqual(self.watch._entry(second).state, "claimed")
        self.assertEqual(self.watch._entry(second).window, W2)
        streams[2].shutdown(socket.SHUT_RDWR)
        streams[2].close()
        eventually(lambda: self.watch._entry(second).state == "up")

    def test_terminal_registry_preclaim_path_rate_limit_capacity_and_liveness(self):
        for port in config.control_ports() | {self.daemon.workshop_port}:
            self.assertIsNone(self.watch.announce_terminal("refused", f"http://localhost:{port}/"))
        ident = self.entry(window=W1)
        self.assertEqual([r["data"]["state"] for r in self.records], ["claimed"])
        self.assertEqual(self.watch.listing(W1)["servers"][0]["url"], "http://localhost:23456/fixture-path")
        self.assertIsNone(self.watch.announce_terminal("fixture-terminal", "http://localhost:23457/"))
        for i in range(5):
            self.assertIsNotNone(self.watch.announce_terminal(str(i), f"http://localhost:{23500+i}/"))
        self.assertIsNone(self.watch.announce_terminal("rate", "http://localhost:23600/"))
        self.clock.now += 5
        self.watch.poll()
        self.assertTrue(self.watch._entry(ident).listening)
        self.watch.poll()
        self.assertFalse(self.watch._entry(ident).listening)
        self.assertEqual(self.claim(ident)[1]["code"], "gone")
        self.clock.now += 61
        self.watch.listing()
        self.assertEqual(self.watch.status()["entries"], 0)
        with patch.object(ds, "ENTRIES_MAX", 1):
            self.assertIsNotNone(self.watch.announce_terminal("cap1", "http://localhost:23601/"))
            self.assertIsNone(self.watch.announce_terminal("cap2", "http://localhost:23602/"))

    def test_terminal_delivery_first_source_wins_and_claims_are_not_replaced(self):
        receipt = object()
        ident = self.watch.announce_terminal("terminal", "http://localhost:23456/path",
                                             window=W1, delivery=receipt)
        entry = self.watch._entry(ident)
        for terminal, window in (("other", W1), ("terminal", W2), ("terminal", W1)):
            self.assertIsNone(self.watch.announce_terminal(terminal, "http://localhost:23456/changed",
                                                           window=window, delivery=object()))
        self.assertEqual((entry.window, entry.path, entry.state), (W1, "/path", "claimed"))
        self.assertEqual(len(self.records), 1)
        self.watch.act(ident, "placed", W1)
        self.assertIsNone(self.watch.announce_terminal("terminal", "http://localhost:23456/",
                                                       window=W1, delivery=object()))
        self.watch.act(ident, "dismiss", W1)
        self.assertIsNone(self.watch.announce_terminal("terminal", "http://localhost:23456/",
                                                       delivery=object()))
        self.assertEqual(entry.state, "dismissed")
        # An agent-owned port must not be claimed or redelivered by a terminal.
        with self.watch._lock:
            agent = self.watch._register(23457, "http", "/",
                                        {"kind": "thread", "thread_id": self.thread.id, "task_id": None},
                                        self.clock.now)
        self.assertIsNone(self.watch.announce_terminal("terminal", "http://localhost:23457/",
                                                       window=W1, delivery=object()))
        self.assertEqual((agent.state, agent.window, agent.source["kind"]), ("up", None, "thread"))

    def test_failed_terminal_delivery_cannot_release_a_newer_same_window_claim(self):
        old, new = object(), object()
        ident = self.watch.announce_terminal("terminal", "http://localhost:23456/", window=W1, delivery=old)
        self.watch.terminal_delivery_failed("wrong", ident, W1, old)
        self.watch.terminal_delivery_failed("terminal", ident, W2, old)
        self.assertEqual(self.watch._entry(ident).state, "claimed")
        self.watch.window_ended(W1)
        self.assertEqual(self.watch.announce_terminal("terminal", "http://localhost:23456/",
                                                      window=W1, delivery=new), ident)
        self.watch.terminal_delivery_failed("terminal", ident, W1, old)
        self.assertEqual(self.watch._entry(ident).state, "claimed")
        self.watch.terminal_delivery_failed("terminal", ident, W1, new)
        self.assertEqual(self.watch._entry(ident).state, "up")
        self.assertEqual(self.watch.announce_terminal("terminal", "http://localhost:23456/",
                                                      window=W1, delivery=new), ident)
        self.watch.act(ident, "placed", W1)
        self.watch.terminal_delivery_failed("terminal", ident, W1, new)
        self.assertEqual(self.watch._entry(ident).state, "placed")

    def test_terminal_restart_resets_startup_grace_and_retains_id_and_dismissal(self):
        ident = self.entry()
        self.clock.now += 5
        self.watch.poll()
        self.watch.poll()
        self.assertEqual(self.watch._entry(ident).state, "down")
        self.clock.now += 10
        self.assertEqual(self.watch.announce_terminal("fixture-terminal", "http://localhost:23456/"), ident)
        self.watch.poll()
        self.watch.poll()
        self.assertTrue(self.watch._entry(ident).listening, "new banner gets a fresh five seconds")
        self.assertEqual(sum(r["data"]["state"] == "up" for r in self.records), 1)
        self.watch.act(ident, "dismiss", W1)
        self.clock.now += 5
        self.watch.poll()
        self.watch.poll()
        self.assertIsNone(self.watch.announce_terminal("fixture-terminal", "http://localhost:23456/",
                                                       window=W1, delivery=object()))
        self.assertEqual(self.watch._entry(ident).state, "dismissed")



class Lifecycle(Base):
    def test_known_roots_from_real_provider_handle_shapes(self):
        session = self.daemon._sessions[self.thread.id]
        sdk_transport = SimpleNamespace(_process=SimpleNamespace(pid=750))
        session.handle.native = SimpleNamespace(client=SimpleNamespace(_transport=sdk_transport))
        self.assertIn(750, self.daemon._dev_known()[0])
        session.handle.native = SimpleNamespace(rpc=SimpleNamespace(process=SimpleNamespace(pid=751)))
        self.assertIn(751, self.daemon._dev_known()[0])
        self.daemon.browser._pw = SimpleNamespace(_connection=SimpleNamespace(
            _transport=SimpleNamespace(_proc=SimpleNamespace(pid=752))))
        self.assertIn(752, self.daemon._dev_known()[0])
        self.daemon.browser._pw = None

    def test_real_playwright_driver_root(self):
        from playwright.async_api import async_playwright
        async def check_driver():
            pw = await async_playwright().start()
            try:
                self.daemon.browser._pw = pw
                driver_pid = pw._impl_obj._connection._transport._proc.pid
                self.assertEqual(self.daemon._dev_browser_roots(), {driver_pid})
                self.assertIn(driver_pid, self.daemon._dev_known()[0])
            finally:
                self.daemon.browser._pw = None
                await pw.stop()
        asyncio.run(check_driver())

    def test_baseline_proc_read_does_not_block_turn_start(self):
        watch = ds.DevServers(self.daemon, proc_root=self.tree.proc,
                              cgroup_root=self.tree.cg, clock=self.clock)
        self.addCleanup(watch.stop)
        watch.session_opened("baseline")
        entered, release = threading.Event(), threading.Event()
        rows = watch.reader.net_rows
        def slow_rows():
            entered.set()
            release.wait(2)
            return rows()
        with patch.object(watch.reader, "net_rows", side_effect=slow_rows):
            try:
                began = time.monotonic()
                watch.turn_started("baseline", PN.CLAUDE)
                self.assertLess(time.monotonic() - began, .05)
                self.assertTrue(entered.wait(1))
                began = time.monotonic()
                watch.status()
                self.assertLess(time.monotonic() - began, .05)
            finally:
                release.set()
            eventually(lambda: watch.status()["polls"] > 0)

    def test_poll_exception_stops_without_respawn_and_turn_rearms(self):
        watch = ds.DevServers(self.daemon, proc_root=self.tree.proc,
                              cgroup_root=self.tree.cg, clock=self.clock)
        self.addCleanup(watch.stop)
        watch.session_opened("fault")
        starts, uncaught = [], []
        thread_factory = threading.Thread
        def counted(*args, **kwargs):
            if kwargs.get("name") == "jarvis-devwatch":
                starts.append(time.monotonic())
            return thread_factory(*args, **kwargs)
        with patch.object(watch.reader, "net_rows", side_effect=RuntimeError(SECRET)), \
             patch.object(ds.threading, "Thread", side_effect=counted), \
             patch.object(threading, "excepthook", side_effect=uncaught.append):
            watch.turn_started("fault", PN.CLAUDE)
            time.sleep(3)
            self.assertLessEqual(len(starts), 2)
            self.assertIsNone(watch._thread)
            self.assertEqual(watch.status()["errors"], {"RuntimeError": 1})
            self.assertFalse(watch.status()["active"])  # a faulted watcher is not active
            watch.announce_terminal("fault-terminal", "http://localhost:23458/")
            self.assertIsNone(watch._thread)  # registry work cannot re-arm a fault
            watch.turn_started("fault", PN.CLAUDE)
            eventually(lambda: watch.status()["errors"].get("RuntimeError") == 2)
            eventually(lambda: watch._thread is None)
        self.assertEqual(len(starts), 2)
        self.assertEqual(uncaught, [])
        self.assertEqual(self.logs, ["Dev watcher failed (RuntimeError)"])
        watch.settings_changed()
        eventually(lambda: watch.status()["polls"] > 0)
        self.assertTrue(watch._thread.is_alive())

    def check_faulting_hook(self, hook):
        session = self.daemon._sessions[self.thread.id]
        called = threading.Event()
        def fault(*args):
            if hook == "turn_started":
                self.assertIsNotNone(session.worker.ident)
            called.set()
            raise RuntimeError(SECRET)
        with patch.object(self.watch, hook, side_effect=fault), \
             patch.object(session.provider, "send", wraps=session.provider.send) as send, \
             patch.object(session.provider, "close", wraps=session.provider.close) as close:
            for _ in range(2):
                self.daemon.send(self.thread.id, UserMessage("fixture"))
                eventually(lambda: session.worker is None)
            self.assertEqual(send.call_count, 2)
            self.assertEqual([r["data"]["stop"] for r in self.records
                              if r["kind"] == "turn_finished"], ["end", "end"])
            self.daemon.close_thread(self.thread.id)
            close.assert_called_once()
            self.assertNotIn(self.thread.id, self.daemon._sessions)
            self.assertTrue(called.is_set())
        self.assertEqual(self.logs, ["Dev watcher hook failed (RuntimeError)"])
        # Every fault is counted in /status, not only the first one logged.
        self.assertGreaterEqual(self.watch.status()["errors"].get("hook:RuntimeError", 0),
                                2 if hook != "session_closed" else 1)

    def test_faulting_turn_started_cannot_wedge_worker(self):
        self.check_faulting_hook("turn_started")

    def test_faulting_turn_ended_cannot_skip_finally(self):
        self.check_faulting_hook("turn_ended")

    def test_faulting_session_closed_cannot_skip_provider_close(self):
        self.check_faulting_hook("session_closed")

    def test_settings_toggle_wakes_active_turn_and_no_work_when_off(self):
        # A constructed daemon opts in to fixture roots; the owner setting
        # route is the wake seam even when this turn started with auto_open off.
        daemon = Daemon(Stores(self.root / "settings-daemon"), {}, None, 0,
                        dev_proc_root=self.tree.proc, dev_cgroup_root=self.tree.cg)
        self.addCleanup(daemon.stop)
        daemon.start()
        daemon.browser_settings.update(auto_open=False)
        daemon.devservers.turn_started("active", PN.CLAUDE)
        self.assertEqual(daemon.devservers.reader.work.files, 0)
        self.assertIsNone(daemon.devservers._thread)
        daemon.browser_settings.update(auto_open=True)
        daemon._dev_settings_changed()
        eventually(lambda: daemon.devservers.status()["polls"] > 0)
        daemon.browser_settings.update(auto_open=False)
        daemon._dev_settings_changed()
        eventually(lambda: daemon.devservers._thread is None)
        before = daemon.devservers.reader.work.as_dict()
        daemon.devservers.poll()
        self.assertEqual(daemon.devservers.reader.work.as_dict(), before)

    def test_default_embedded_daemon_never_reads_proc(self):
        daemon = Daemon(Stores(self.root / "embedded"), {}, None, 0)
        self.addCleanup(daemon.stop)
        with patch.object(dl.ProcReader, "_read", side_effect=AssertionError("real proc read")):
            daemon.devservers.turn_started("test", PN.CLAUDE)
            daemon.devservers.poll()
        self.assertIsNone(daemon.devservers.reader)
        self.assertIsNone(daemon.devservers._thread)

    def test_terminal_only_entry_wakes_watch_without_http_probe_or_agent_session(self):
        watch = ds.DevServers(self.daemon, proc_root=self.tree.proc, cgroup_root=self.tree.cg, clock=self.clock)
        self.addCleanup(watch.stop)
        watch.start()
        self.assertIsNone(watch._thread)
        with patch.object(watch, "_exchange", side_effect=AssertionError("terminal must not be probed")):
            ident = watch.announce_terminal("alone", "http://localhost:23456/app/")
            eventually(lambda: watch.status()["polls"] > 0)
            self.assertEqual(watch._thread.name, "jarvis-devwatch")
            self.assertEqual(watch.cadence(), 5)
            self.assertEqual(watch.status()["work"]["probes"], 0)
            self.assertEqual(watch.reader.work.environ_reads, 0)
            self.clock.now += 5
            watch.poll()
            self.assertTrue(watch._entry(ident).listening)
            watch.poll()
            self.assertFalse(watch._entry(ident).listening)
            self.assertIsNone(watch.cadence())

    def test_terminal_liveness_counts_the_initial_baseline_poll(self):
        watch = ds.DevServers(self.daemon, proc_root=self.tree.proc, cgroup_root=self.tree.cg, clock=self.clock)
        self.addCleanup(watch.stop)
        with patch.object(watch, "_ensure_thread"):    # deterministic first poll at grace boundary
            ident = watch.announce_terminal("alone", "http://localhost:23456/")
        self.clock.now += 5
        watch.poll()
        self.assertEqual(watch._entry(ident).misses, 1)
        watch.poll()
        self.assertFalse(watch._entry(ident).listening)

    def test_lazy_watch_thread_baseline_off_and_cadence(self):
        s = self.server()
        self.add_listener(s)
        watch = ds.DevServers(self.daemon, proc_root=self.tree.proc, cgroup_root=self.tree.cg, clock=self.clock)
        self.addCleanup(watch.stop)
        watch.start()
        self.assertEqual(watch.reader.work.files, 0)
        self.assertIsNone(watch._thread)
        tag = watch.session_opened("live")
        watch.turn_started("live", PN.FAST)
        self.assertIsNone(watch._thread)
        watch.turn_started("live", PN.CLAUDE)
        self.assertEqual(watch._thread.name, "jarvis-devwatch")
        eventually(lambda: watch.status()["polls"] > 0)
        self.assertEqual(s.accepts, 0)              # existed in the baseline
        watch.turn_ended("live")
        self.clock.now += 121
        self.assertEqual(watch.cadence(), 5)
        self.clock.now += 480
        self.assertIsNone(watch.cadence())
        polls = watch.status()["polls"]
        watch.poll()
        self.assertEqual(watch.status()["polls"], polls)
        self.daemon.browser_settings.update(auto_open=False)
        watch.settings_changed()
        eventually(lambda: watch._thread is None)
        self.assertFalse(watch.status()["active"])

    def test_turn_wiring_run_copy_saved_brief_and_session_close(self):
        session = self.daemon._sessions[self.thread.id]
        saved = self.daemon.stores.threads.path(self.thread.id).with_name("brief.json")
        before = saved.read_bytes()
        self.assertNotIn(b"origin_tag", before)
        self.assertNotIn(self.tag.encode(), before)
        self.assertIsNone(session.brief.origin_tag)
        self.assertEqual(session.handle.native.brief.origin_tag, self.tag)
        self.assertNotIn(self.tag, repr(session.handle.native.brief))
        with patch.object(self.watch, "turn_started", wraps=self.watch.turn_started) as start, \
             patch.object(self.watch, "turn_ended", wraps=self.watch.turn_ended) as end:
            self.daemon.send(self.thread.id, UserMessage("fixture"))
            eventually(lambda: session.worker is None)
            self.assertEqual(start.call_args.args, (self.thread.id, PN.CLAUDE))
            self.assertEqual(end.call_args.args, (self.thread.id,))
        self.daemon.close_thread(self.thread.id)
        self.assertIsNotNone(self.watch.sessions.owner(self.tag.encode(), self.clock.now).closed)
        self.daemon.resume_thread(self.thread.id)
        next_tag = self.daemon._sessions[self.thread.id].origin_tag
        self.assertNotEqual(next_tag, self.tag)
        self.assertEqual(saved.read_bytes(), before)

    def test_stop_order_terminals_watch_bus(self):
        order = []
        with ExitStack() as stack:
            for obj, method, name in ((self.daemon.terminals, "finish_all", "terminals"),
                                      (self.watch, "stop", "watch"), (self.daemon.bus, "close", "bus")):
                original = getattr(obj, method)
                stack.enter_context(patch.object(obj, method, side_effect=lambda *a, f=original, n=name, **k:
                                               (order.append(n), f(*a, **k))[1]))
            self.daemon.stop()
        self.assertEqual(order, ["terminals", "watch", "bus"])

    def test_provider_env_inherited_by_setsided_nohup_children_not_argv(self):
        from jarvis.v2.providers import claude, codex, codex_config
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        from claude_provider_check import FakeClient
        from codex_provider_check import Brain, FakeRpc
        target = self.root / "claude-env.json"
        clients = []
        class Client(FakeClient):
            async def connect(client):
                await super().connect()
                clients.append(client)
                env = {**os.environ, **client.options.env}
                subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--env-parent", str(target)],
                               env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with patch.object(claude, "_client_factory", Client), patch.object(claude, "resolve_cli", return_value="/fake/claude"):
            provider = claude.ClaudeProvider()
            handle = provider.start(Thread("envclaude", "p", Role.IMPLEMENTER, PN.CLAUDE),
                                    Brief(Role.IMPLEMENTER, str(self.root), origin_tag=self.tag), lambda *_: Decision.DENY)
            try:
                options = clients[0].options
                self.assertEqual(options.env["JARVIS_ORIGIN_TAG"], self.tag)
                command = SubprocessCLITransport("", options)._build_command()
                self.assertNotIn(self.tag, " ".join(command))
                self.assertEqual(json.loads(target.read_text())["tag"], self.tag)
            finally:
                provider.close(handle)
        brain = Brain(self.root)
        auth = self.root / "owner-codex"
        auth.mkdir()
        (auth / "auth.json").write_text("FAKE AUTH")
        target = self.root / "codex-env.json"
        def rpc(argv, **kwargs):
            fake = FakeRpc(brain, argv, **kwargs)
            fake.argv = [sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--codex-peer",
                         *fake.argv[5:], str(target)]
            brain.rpcs.append(fake)
            return fake
        with patch.object(codex_config, "owner_home", return_value=auth), patch.object(codex, "RpcProcess", rpc), \
             patch.object(codex.CodexProvider, "_probe", return_value=("/fake/codex", "fixture")):
            provider = codex.CodexProvider()
            handle = provider.start(Thread("envcodex", "p", Role.IMPLEMENTER, PN.CODEX),
                                    Brief(Role.IMPLEMENTER, str(self.root), origin_tag=self.tag), lambda *_: Decision.DENY)
            try:
                self.assertEqual(brain.rpcs[0].env["JARVIS_ORIGIN_TAG"], self.tag)
                self.assertEqual(json.loads(target.read_text())["tag"], self.tag)
                self.assertNotIn(self.tag, " ".join(brain.rpcs[0].original_argv))
                self.assertNotIn(self.tag, " ".join(brain.rpcs[0].argv))
                self.assertNotIn(self.tag, (Path(brain.rpcs[0].env["CODEX_HOME"]) / "config.toml").read_text())
                self.assertIn('inherit = "all"', (Path(brain.rpcs[0].env["CODEX_HOME"]) / "config.toml").read_text())
            finally:
                provider.close(handle)


class NoTool(unittest.TestCase):
    def test_only_daemon_hud_api_terminals_and_implementation_reach_modules(self):
        allowed = {"jarvis/v2/daemon.py", "jarvis/v2/hud_api.py", "jarvis/v2/terminals.py",
                   "jarvis/v2/devservers.py", "jarvis/v2/devlisten.py"}
        needle = re.compile(r"(?:\bimport\s+[^\n]*\b(?:devservers|devlisten)\b|"
                            r"\b(?:devservers|devlisten)\s+import\b|\.devservers\b|['\"]/dev-servers)")
        offenders = [p.relative_to(REPO).as_posix() for p in (REPO / "jarvis").rglob("*.py")
                     if p.relative_to(REPO).as_posix() not in allowed and needle.search(p.read_text())]
        self.assertEqual(offenders, [])


def env_parent(target):
    # This fake CLI launches a detached/nohup child. It reads only its own
    # named variable, never /proc or any owner's environment file.
    subprocess.run(["nohup", sys.executable, "-B", str(Path(__file__).resolve()), "--env-child", target],
                   start_new_session=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--env-child":
        Path(sys.argv[2]).write_text(json.dumps({"tag": os.environ.get("JARVIS_ORIGIN_TAG")}))
    elif len(sys.argv) > 1 and sys.argv[1] == "--env-parent":
        env_parent(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "--codex-peer":
        from codex_provider_check import peer
        env_parent(sys.argv[5])
        peer(*sys.argv[2:5])
    elif len(sys.argv) > 1 and sys.argv[1] == "--listen":
        artifact = Path(sys.argv[3])
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", int(sys.argv[2])))
            server.listen()
            accepts = 0
            while True:
                artifact.write_text(json.dumps({"tag": os.environ.get("JARVIS_ORIGIN_TAG"), "accepts": accepts}))
                conn, _ = server.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<html>fixture</html>")
                    accepts += 1
    else:
        unittest.main(verbosity=2)

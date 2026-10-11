"""`jarvis restart` and `jarvis hud rebuild` (2026-10-11, `jarvis/v2/owner_ops.py`).

Free and hermetic. Nothing here restarts anything, installs into the
owner's HUD root, runs npm, or talks to 8402/8403/8405:

  - `systemctl` and `npm` are a fake behind `subprocess.run` (anything else
    still runs, and a real `systemctl`/`npm` reaching it fails the suite);
  - the daemon's `/status` is a fake whose uptime runs on a fake clock, so a
    restart, a slow boot, an old daemon's last answer and a timeout are all
    played on cue — except `fetch_status`'s own HTTP, which is tested
    against a server of its own on an ephemeral port;
  - every HTTP connection to 8402, 8403 or 8405 raises;
  - `--pull` runs real git against a temporary clone with a bare origin,
    HOME a temp dir;
  - `rebuild` installs into a temp `config.HUD_DIR` from a temp checkout,
    its build step a stub that writes a `dist/`, and the fake daemon reports
    what `hud_api.hud_served()` says — what the real `/status` would.

What it pins:
  restart — refused while a turn runs (exit 3, nothing restarted) unless
    --force; the unit must be installed and running the daemon; the restart
    is an argv list; the wait takes only a daemon younger than the restart,
    and times out (exit 4) with systemd's state and the journal command;
    systemctl missing, the user bus unreachable and a failed restart each
    say so (exit 1); the report names uptime, every provider and the HUD;
  rebuild — `npm ci` exactly when node_modules is missing, unmarked or older
    than package-lock.json (or --clean), before the build; a failed `npm ci`
    installs nothing; the clean-hud/ refusal and --allow-dirty pass through
    #44's installer; the new build is verified against /status (exit 4 when
    the daemon is down, serves another build, or runs in dev mode);
    --restart is refused before anything is built while a turn runs, and
    again before restarting; --pull refuses off main, on a detached HEAD and
    with dirty tracked files (never stashing), fast-forwards, refuses a
    diverged main without moving it, and names Python a pull changed;
  the CLI — every spelling dispatches with its options, stray options are a
    usage error, `jarvis daemon2` with no action still runs the daemon;
  no lever — only the CLI imports this module, and no tool is named for it.

Run:
    PYTHONPATH=. .venv/bin/python tests/v2/owner_ops_check.py
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config  # noqa: E402
from jarvis.v2 import hud_install, owner_ops as O  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
LIVE_PORTS = {8402, 8403, 8405}
REAL_RUN = subprocess.run
REAL_SYSTEMCTL_PATH = O._systemctl_path
REAL_NPM_PATH = hud_install.npm_path
REAL_GIT_HEAD = hud_install.git_head
_real_connect = http.client.HTTPConnection.connect


def _guarded_connect(self):
    if self.port in LIVE_PORTS:
        raise AssertionError(f"a test reached the live port {self.port}")
    return _real_connect(self)


http.client.HTTPConnection.connect = _guarded_connect


# -- fakes -----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0

    def sleep(self, seconds):
        self.slept += 1
        self.now += seconds


class Daemon:
    """The v2 daemon as `/status` shows it, on the fake clock."""

    def __init__(self, clock, *, up=True, turns=0, boot=0.6):
        self.clock = clock
        self.up = up
        self.turns = turns
        self.boot = boot                 # seconds a restarted daemon takes to answer
        self.started = clock.now - 5000
        self.down_until = None
        self.comes_back = True
        self.stale_answers = 0           # the old daemon answers this many more times
        self.hud = {"build": "20261011T000000000000Z-0123456789ab", "git_head": "a" * 40,
                    "state": "installed"}
        self.turns_after_restart = 0
        self.fetches = 0

    def restart(self):
        self.down_until = self.clock.now + self.boot
        self.old_started = self.started
        self.started = self.clock.now + self.boot
        self.turns = self.turns_after_restart

    def status(self, port=None, timeout=2.0):
        self.fetches += 1
        if self.down_until is not None and self.stale_answers > 0:
            self.stale_answers -= 1
            return self._body(self.old_started)
        if not self.up:
            return None
        if self.down_until is not None and (self.clock.now < self.down_until or not self.comes_back):
            return None
        return self._body(self.started)

    def _body(self, started):
        hud = self.hud() if callable(self.hud) else self.hud
        return {"jarvis-daemon": True, "version": 2, "uptime": self.clock.now - started,
                "providers": {"claude": {"ok": True, "reason": "claude 2.1.300"},
                              "codex": {"ok": False, "reason": "codex is not logged in\nrun codex login"},
                              "fast": {"ok": True, "reason": ""}},
                "threads_open": 1, "turns_running": self.turns,
                "hud_build": hud["build"], "hud_git_head": hud.get("git_head"),
                "hud_state": hud["state"]}


class Programs:
    """`subprocess.run` for systemctl and npm; everything else really runs."""

    def __init__(self, daemon=None, *, load="loaded", active="active", show_rc=0, show_err="",
                 restart_rc=0, restart_err="", npm_rc=0, npm_raises=None, systemctl_raises=None):
        self.daemon = daemon
        self.load, self.active = load, active
        self.show_rc, self.show_err = show_rc, show_err
        self.restart_rc, self.restart_err = restart_rc, restart_err
        self.npm_rc, self.npm_raises = npm_rc, npm_raises
        self.systemctl_raises = systemctl_raises
        self.active_after = None         # ActiveState once the restart was issued
        self.restarted = False
        self.calls: list[tuple[list, dict]] = []
        self.events: list[str] = []

    def run(self, argv, *args, **kwargs):
        name = os.path.basename(argv[0]) if isinstance(argv, list) and argv else None
        if name not in ("systemctl", "npm"):
            if isinstance(argv, list) and any(os.path.basename(str(a)) in ("systemctl", "npm")
                                              for a in argv[:1]):
                raise AssertionError(f"a real {argv[0]} was about to run")
            return REAL_RUN(argv, *args, **kwargs)
        assert isinstance(argv, list), "argv, never a string"
        assert not kwargs.get("shell"), "never through a shell"
        assert kwargs.get("timeout"), f"{name} runs with a timeout"
        assert kwargs.get("stdin") is subprocess.DEVNULL, f"{name} reads nothing from the terminal"
        self.calls.append((list(argv), dict(kwargs)))
        if name == "npm":
            self.events.append("npm " + " ".join(argv[1:]))
            if self.npm_raises:
                raise self.npm_raises
            return subprocess.CompletedProcess(argv, self.npm_rc)
        if self.systemctl_raises:
            raise self.systemctl_raises
        verb = argv[2]
        self.events.append("systemctl " + verb)
        if verb == "show":
            active = self.active_after if self.restarted and self.active_after else self.active
            out = f"LoadState={self.load}\nActiveState={active}\nSubState=running\n"
            return subprocess.CompletedProcess(argv, self.show_rc, out if not self.show_rc else "",
                                               self.show_err)
        if verb == "restart":
            self.restarted = True
            if self.restart_rc == 0 and self.daemon is not None:
                self.daemon.restart()
            return subprocess.CompletedProcess(argv, self.restart_rc, "", self.restart_err)
        raise AssertionError(f"unexpected systemctl verb {verb}")

    def verbs(self, program="systemctl"):
        return [argv[2] if program == "systemctl" else argv[1]
                for argv, _ in self.calls if os.path.basename(argv[0]) == program]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="owner-ops-"))
        self.clock = Clock()
        self.out: list[str] = []
        self.addCleanup(patch.stopall)
        patch.object(O, "_clock", lambda: self.clock.now).start()
        patch.object(O, "_sleep", self.clock.sleep).start()
        patch.object(O, "_systemctl_path", lambda: "/usr/bin/systemctl").start()
        patch.object(hud_install, "npm_path", lambda: "/usr/bin/npm").start()
        patch.object(hud_install, "git_head", lambda: "a" * 40).start()

    def world(self, daemon=None, **programs):
        self.daemon = daemon if daemon is not None else Daemon(self.clock)
        self.programs = Programs(self.daemon, **programs)
        patch.object(O, "fetch_status", self.daemon.status).start()
        patch.object(subprocess, "run", self.programs.run).start()
        return self.daemon, self.programs

    def said(self):
        return "\n".join(self.out)


# -- restart ---------------------------------------------------------------------------


class Restart(Base):
    def test_refused_while_a_turn_runs(self):
        _, programs = self.world(Daemon(self.clock, turns=1))
        self.assertEqual(O.restart(out=self.out.append), O.BUSY)
        self.assertNotIn("restart", programs.verbs(), "nothing was restarted")
        self.assertIn("1 turn is running", self.said())
        self.assertIn("--force", self.said())
        self.assertIn("Stop", self.said())

    def test_two_running_turns_read_as_plural(self):
        self.world(Daemon(self.clock, turns=2))
        self.assertEqual(O.restart(out=self.out.append), O.BUSY)
        self.assertIn("2 turns are running", self.said())

    def test_force_restarts_a_busy_daemon(self):
        _, programs = self.world(Daemon(self.clock, turns=1))
        self.assertEqual(O.restart(force=True, out=self.out.append), O.OK, self.said())
        self.assertIn("restart", programs.verbs())
        restart = [argv for argv, _ in programs.calls if argv[2] == "restart"][0]
        self.assertEqual(restart, ["/usr/bin/systemctl", "--user", "restart", "jarvis-daemon2"])
        self.assertIn("--force: 1 turn is running, and will be cut off.", self.said())

    def test_warns_about_terminals_and_reports(self):
        self.world()
        self.assertEqual(O.restart(out=self.out.append), O.OK, self.said())
        said = self.said()
        self.assertIn("open HUD terminals end", said)
        self.assertRegex(said, r"The daemon is up \(uptime \d+\.\d s\)")
        self.assertIn("claude", said)
        self.assertIn("codex      NOT OK — codex is not logged in run codex login", said)
        self.assertIn("HUD: build 20261011T000000000000Z-0123456789ab, installed from aaaaaaaaaaaa", said)
        self.assertIn("Press Ctrl+R in the HUD window", said)
        self.assertLess(said.index("open HUD terminals end"), said.index("The daemon is up"))

    def test_waits_through_a_slow_boot(self):
        self.world(Daemon(self.clock, boot=4.0))
        self.assertEqual(O.restart(out=self.out.append), O.OK, self.said())
        self.assertGreaterEqual(self.clock.slept, 10, "it polled while the daemon booted")
        self.assertLessEqual(self.clock.now - 1000.0, 4.0 + O.POLL + 0.01)

    def test_the_old_daemons_answer_is_never_taken(self):
        daemon, _ = self.world()
        daemon.stale_answers = 3          # the old process answers three more times
        self.assertEqual(O.restart(out=self.out.append), O.OK, self.said())
        self.assertGreaterEqual(daemon.fetches, 1 + 3 + 1)
        self.assertNotIn("uptime 50", self.said(), "the old daemon's uptime was never reported")

    def test_a_daemon_that_never_comes_back_times_out(self):
        daemon, programs = self.world()
        daemon.comes_back = False
        programs.active_after = "activating"
        code = O.restart(wait=5.0, out=self.out.append)
        self.assertEqual(code, O.UNVERIFIED)
        said = self.said()
        self.assertIn("within 5 s", said)
        self.assertIn("systemd says activating/running", said)
        self.assertIn("journalctl --user -u jarvis-daemon2 -n 50 --no-pager", said)
        self.assertLessEqual(self.clock.now - 1000.0, 5.0 + O.POLL + 0.01, "the wait is bounded")

    def test_systemctl_missing(self):
        self.world()
        patch.object(O, "_systemctl_path", REAL_SYSTEMCTL_PATH).start()
        with patch("jarvis.v2.owner_ops.shutil.which", return_value=None):
            self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        self.assertIn("systemctl is not on PATH", self.said())
        self.assertEqual(self.programs.calls, [])

    def test_a_windows_systemctl_is_never_used(self):
        with patch("jarvis.v2.owner_ops.shutil.which", return_value="/mnt/c/Windows/systemctl"):
            with self.assertRaisesRegex(O.OpsRefused, "not on PATH"):
                REAL_SYSTEMCTL_PATH()
        with patch("jarvis.v2.owner_ops.shutil.which", return_value="/usr/bin/systemctl"):
            self.assertEqual(REAL_SYSTEMCTL_PATH(), "/usr/bin/systemctl")

    def test_the_user_bus_is_unreachable(self):
        self.world(show_rc=1, show_err="Failed to connect to bus: No medium found")
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        self.assertIn("Failed to connect to bus", self.said())
        self.assertIn("systemd=true", self.said())
        self.assertNotIn("restart", self.programs.verbs())

    def test_the_unit_is_not_installed(self):
        _, programs = self.world(load="not-found", active="inactive")
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        said = self.said()
        self.assertIn("is not installed (LoadState=not-found)", said)
        self.assertIn(O.UNIT_FILE, said)
        self.assertIn("enable --now jarvis-daemon2", said)
        self.assertNotIn("restart", programs.verbs())

    def test_a_daemon_started_another_way_is_not_doubled(self):
        _, programs = self.world(active="inactive")
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        self.assertIn("started another way", self.said())
        self.assertIn("second daemon", self.said())
        self.assertNotIn("restart", programs.verbs())

    def test_a_failed_restart_says_where_to_look(self):
        _, programs = self.world(restart_rc=1, restart_err=(
            "Job for jarvis-daemon2.service failed because the control process exited with error code.\n"
            "See \"systemctl --user status jarvis-daemon2.service\" for details."))
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        said = self.said()
        self.assertIn("failed (exit 1): Job for jarvis-daemon2.service failed", said)
        self.assertIn("journalctl --user -u jarvis-daemon2", said)

    def test_a_systemctl_that_hangs(self):
        self.world(systemctl_raises=subprocess.TimeoutExpired(["systemctl"], 15))
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        self.assertIn("did not return within", self.said())

    def test_a_daemon_that_is_down_is_restarted_anyway(self):
        daemon, programs = self.world(Daemon(self.clock, up=False), active="failed")
        programs.restart_rc = 0
        orig = daemon.restart

        def back():
            orig()
            daemon.up = True
        daemon.restart = back
        self.assertEqual(O.restart(out=self.out.append), O.OK, self.said())
        self.assertIn("Nothing answers on 127.0.0.1", self.said())

    def test_something_else_on_the_port(self):
        daemon, programs = self.world()
        patch.object(O, "fetch_status", lambda **_: {"jarvis-daemon": True, "uptime": 3}).start()
        self.assertEqual(O.restart(out=self.out.append), O.FAILED)
        self.assertIn("something other than the v2 daemon", self.said())
        self.assertEqual(programs.calls, [])

    def test_a_hud_that_is_not_served_says_what_to_run(self):
        daemon, _ = self.world()
        daemon.hud = {"build": None, "git_head": None, "state": "not installed"}
        self.assertEqual(O.restart(out=self.out.append), O.OK)
        self.assertIn("HUD: not installed — nothing is served; run `jarvis hud rebuild`.", self.said())

    def test_a_hud_from_an_older_commit_is_pointed_out(self):
        self.world()
        with patch.object(hud_install, "git_head", lambda: "b" * 40):
            self.assertEqual(O.restart(out=self.out.append), O.OK)
        self.assertIn("the checkout is at bbbbbbbbbbbb", self.said())



class FetchStatus(unittest.TestCase):
    """The one real HTTP read, against a server of its own."""

    def serve(self, code, body):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_reads_status(self):
        port = self.serve(200, json.dumps({"version": 2, "uptime": 1.5}).encode())
        self.assertEqual(O.fetch_status(port), {"version": 2, "uptime": 1.5})
        with patch.object(config, "DAEMON_PORT", port):
            self.assertEqual(O.fetch_status(), {"version": 2, "uptime": 1.5}, "config's port by default")

    def test_anything_else_is_none(self):
        self.assertIsNone(O.fetch_status(self.serve(500, b"{}")))
        self.assertIsNone(O.fetch_status(self.serve(200, b"not json")))
        self.assertIsNone(O.fetch_status(self.serve(200, b"[1, 2]")))

    def test_nothing_listening_is_none(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertIsNone(O.fetch_status(port, timeout=0.5))

    def test_the_live_ports_are_guarded(self):
        with self.assertRaises(AssertionError):
            http.client.HTTPConnection("127.0.0.1", 8405, timeout=0.1).connect()


# -- npm ci ----------------------------------------------------------------------------


class NpmCi(Base):
    def hud(self, *, modules=True, marker=True, lock=True, lock_newer=False):
        hud = self.tmp / "hud"
        hud.mkdir(exist_ok=True)
        if lock:
            (hud / "package-lock.json").write_text("{}")
        if modules:
            (hud / "node_modules").mkdir(exist_ok=True)
            if marker:
                (hud / "node_modules" / ".package-lock.json").write_text("{}")
        if lock and modules and marker:
            older, newer = 1_700_000_000, 1_700_000_100
            os.utime(hud / "package-lock.json", (newer, newer) if lock_newer else (older, older))
            os.utime(hud / "node_modules" / ".package-lock.json",
                     (older, older) if lock_newer else (newer, newer))
        return hud

    def test_when_it_runs(self):
        self.assertEqual(O.npm_ci_reason(self.hud(modules=False)), "node_modules is missing")
        self.assertEqual(O.npm_ci_reason(self.hud(marker=False)), "node_modules has no .package-lock.json")

    def test_a_newer_lock_file_runs_it(self):
        self.assertEqual(O.npm_ci_reason(self.hud(lock_newer=True)),
                         "package-lock.json is newer than node_modules")

    def test_a_current_install_does_not(self):
        hud = self.hud()
        self.assertIsNone(O.npm_ci_reason(hud))
        self.assertEqual(O.npm_ci_reason(hud, clean=True), "--clean")

    def test_no_lock_file_leaves_it_to_the_build(self):
        hud = self.hud(lock=False)
        self.assertIsNone(O.npm_ci_reason(hud))

    def test_it_runs_npm_ci_in_hud(self):
        _, programs = self.world()
        hud = self.hud()
        O.npm_ci(hud)
        (argv, kwargs), = programs.calls
        self.assertEqual(argv, ["/usr/bin/npm", "ci"])
        self.assertEqual(kwargs["cwd"], str(hud))
        self.assertEqual(kwargs["timeout"], O.NPM_CI_TIMEOUT)

    def test_a_failed_npm_ci_refuses(self):
        self.world(npm_rc=1)
        with self.assertRaisesRegex(O.OpsRefused, r"`npm ci` failed \(exit 1\); nothing was installed"):
            O.npm_ci(self.hud())

    def test_a_hung_npm_ci_refuses(self):
        self.world(npm_raises=subprocess.TimeoutExpired(["npm"], 900))
        with self.assertRaisesRegex(O.OpsRefused, "did not finish within"):
            O.npm_ci(self.hud())

    def test_no_npm(self):
        patch.object(hud_install, "npm_path", REAL_NPM_PATH).start()
        with patch("jarvis.v2.hud_install.shutil.which", return_value=None):
            with self.assertRaisesRegex(O.OpsRefused, "npm is not on PATH"):
                O.npm_ci(self.hud())
        with patch("jarvis.v2.hud_install.shutil.which", return_value="/mnt/c/Program Files/nodejs/npm"):
            with self.assertRaisesRegex(O.OpsRefused, "never used"):
                O.npm_ci(self.hud())


# -- a temporary checkout with a bare origin ------------------------------------------


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


class Checkout(Base):
    def setUp(self):
        super().setUp()
        home = self.tmp / "home"
        home.mkdir()
        patch.dict(os.environ, {**GIT_ENV, "HOME": str(home)}).start()
        self.origin = self.tmp / "origin.git"
        self.seed = self.tmp / "seed"
        self.repo = self.tmp / "repo"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.seed))
        self.git(self.seed, "checkout", "-q", "-b", "main")
        self.write(self.seed, ".gitignore", "hud/dist/\nhud/node_modules/\n")
        self.write(self.seed, "jarvis/x.py", "X = 1\n")
        self.write(self.seed, "hud/package.json", '{"name": "hud"}\n')
        self.write(self.seed, "hud/package-lock.json", "{}\n")
        self.write(self.seed, "README.md", "hello\n")
        self.commit(self.seed, "seed")
        self.git(self.seed, "push", "-q", "origin", "main")
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        patch.object(config, "REPO_ROOT", self.repo).start()

    def git(self, where, *args):
        result = REAL_RUN(["git", "-C", str(where), *args], capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, (args, result.stderr))
        return result.stdout.strip()

    def write(self, where, rel, text):
        path = Path(where) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, where, message):
        self.git(where, "add", "-A")
        self.git(where, "commit", "-q", "-m", message)
        return self.git(where, "rev-parse", "HEAD")

    def upstream(self, rel="jarvis/x.py", text="X = 2\n"):
        """origin/main moves on by one commit."""
        self.write(self.seed, rel, text)
        head = self.commit(self.seed, f"change {rel}")
        self.git(self.seed, "push", "-q", "origin", "main")
        return head

    def head(self):
        return self.git(self.repo, "rev-parse", "HEAD")


class Pull(Checkout):
    def test_refused_off_main(self):
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        before = self.head()
        self.upstream()
        with self.assertRaisesRegex(O.OpsRefused, r"is on feature; switch to main yourself"):
            O.pull(self.out.append)
        self.assertEqual(self.head(), before)

    def test_refused_on_a_detached_head(self):
        self.git(self.repo, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(O.OpsRefused, "a detached HEAD"):
            O.pull(self.out.append)

    def test_refused_with_dirty_tracked_files_and_nothing_stashed(self):
        self.write(self.repo, "jarvis/x.py", "X = 'mine'\n")
        before = self.head()
        self.upstream()
        with self.assertRaisesRegex(O.OpsRefused, r"uncommitted changes \(jarvis/x.py\).*never stashes"):
            O.pull(self.out.append)
        self.assertEqual((self.repo / "jarvis/x.py").read_text(), "X = 'mine'\n", "the edit is untouched")
        self.assertEqual(self.git(self.repo, "stash", "list"), "", "nothing was stashed")
        self.assertEqual(self.head(), before)

    def test_untracked_files_do_not_stop_it(self):
        self.write(self.repo, "notes.txt", "scratch\n")
        new = self.upstream()
        O.pull(self.out.append)
        self.assertEqual(self.head(), new)
        self.assertTrue((self.repo / "notes.txt").exists())

    def test_fast_forwards_and_names_python_that_changed(self):
        before = self.head()
        new = self.upstream()
        self.assertEqual(O.pull(self.out.append), (before, new))
        self.assertEqual(self.head(), new)
        self.assertIn(f"Fast-forwarded main {before[:12]} → {new[:12]} (1 commit(s)).", self.said())
        self.assertEqual(O.python_changed(before, new), ["jarvis/x.py"])

    def test_a_hud_only_pull_changes_no_python(self):
        before = self.head()
        new = self.upstream("hud/package.json", '{"name": "hud", "v": 2}\n')
        O.pull(self.out.append)
        self.assertEqual(O.python_changed(before, new), [])

    def test_up_to_date(self):
        O.pull(self.out.append)
        self.assertIn("main is up to date with origin/main", self.said())

    def test_a_diverged_main_is_refused_and_not_moved(self):
        self.write(self.repo, "local.txt", "mine\n")
        mine = self.commit(self.repo, "local work")
        self.upstream()
        with self.assertRaisesRegex(O.OpsRefused, "cannot be fast-forwarded.*never merges"):
            O.pull(self.out.append)
        self.assertEqual(self.head(), mine, "the local commit is still HEAD")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "", "no merge in progress")


# -- rebuild ---------------------------------------------------------------------------


class Rebuild(Checkout):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "share" / "jarvis" / "hud"
        patch.object(config, "HUD_DIR", self.root).start()
        patch.object(config, "HUD_DIST", self.root).start()
        patch.object(config, "HUD_DEV", False).start()
        patch.object(config, "HUD_BUILD", self.repo / "hud" / "dist").start()
        patch.object(hud_install, "git_head", REAL_GIT_HEAD).start()
        self.builds = 0
        modules = self.repo / "hud" / "node_modules"
        modules.mkdir()
        (modules / ".package-lock.json").write_text("{}")
        os.utime(self.repo / "hud" / "package-lock.json", (1_700_000_000, 1_700_000_000))

    def build(self):
        self.builds += 1
        if hasattr(self, "programs"):
            self.programs.events.append("build")
        dist = self.repo / "hud" / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text(f"<!doctype html><title>J.A.R.V.I.S.</title>{self.builds}")
        (dist / "assets" / f"app-{self.builds}.js").write_text("console.log(1)")

    def served(self):
        from jarvis.v2.hud_api import hud_served
        return hud_served()

    def daemon(self, **kw):
        daemon = Daemon(self.clock, **kw)
        daemon.hud = self.served                 # what the real /status would say, per request
        return daemon

    def rebuild(self, **kw):
        return O.rebuild(out=self.out.append, build=self.build, **kw)

    def test_installs_and_verifies(self):
        _, programs = self.world(self.daemon())
        self.assertEqual(self.rebuild(), O.OK, self.said())
        built = hud_install.newest(self.root)
        self.assertIsNotNone(built)
        self.assertEqual(built.git_head, self.head())
        said = self.said()
        self.assertIn(f"Installed HUD build {built.id}", said)
        self.assertIn(f"The daemon serves {built.id} from {self.head()[:12]}, installed.", said)
        self.assertIn("Press Ctrl+R in the HUD window", said)
        self.assertEqual(programs.verbs("npm"), [], "node_modules was current: no npm ci")
        self.assertEqual(programs.verbs(), [], "no restart by default")

    def test_runs_npm_ci_first_when_the_lock_file_moved(self):
        _, programs = self.world(self.daemon())
        marker = (self.repo / "hud" / "node_modules" / ".package-lock.json").stat().st_mtime
        os.utime(self.repo / "hud" / "package-lock.json", (marker + 10, marker + 10))
        self.assertEqual(self.rebuild(), O.OK, self.said())
        self.assertEqual(programs.events[:2], ["npm ci", "build"])
        self.assertIn("package-lock.json is newer than node_modules", self.said())

    def test_clean_forces_npm_ci(self):
        _, programs = self.world(self.daemon())
        self.assertEqual(self.rebuild(clean=True), O.OK, self.said())
        self.assertEqual(programs.verbs("npm"), ["ci"])

    def test_a_failed_npm_ci_installs_nothing(self):
        self.world(self.daemon(), npm_rc=1)
        self.assertEqual(self.rebuild(clean=True), O.FAILED)
        self.assertEqual(self.builds, 0)
        self.assertEqual(hud_install.builds(self.root), [])
        self.assertIn("`npm ci` failed", self.said())

    def test_dirty_hud_is_refused_unless_allowed(self):
        self.world(self.daemon())
        self.write(self.repo, "hud/package.json", '{"name": "edited"}\n')
        self.assertEqual(self.rebuild(), O.FAILED)
        self.assertIn("refused: hud/ has uncommitted changes", self.said())
        self.assertEqual(self.builds, 0)
        self.out.clear()
        self.assertEqual(self.rebuild(allow_dirty=True), O.OK, self.said())
        self.assertTrue(hud_install.newest(self.root).dirty)
        self.assertIn("(with uncommitted hud/ changes)", self.said())

    def test_unverified_when_the_daemon_is_down(self):
        self.world(self.daemon(up=False))
        self.assertEqual(self.rebuild(), O.UNVERIFIED)
        self.assertIn("no v2 daemon answers", self.said())
        self.assertIn("systemctl --user start jarvis-daemon2", self.said())
        self.assertIsNotNone(hud_install.newest(self.root), "the install itself happened")

    def test_unverified_when_the_daemon_serves_another_build(self):
        daemon = self.daemon()
        daemon.hud = {"build": "20200101T000000000000Z-ffffffffffff", "git_head": "f" * 40,
                      "state": "installed"}
        self.world(daemon)
        self.assertEqual(self.rebuild(), O.UNVERIFIED)
        self.assertIn("JARVIS_HUD_DIR must agree", self.said())

    def test_unverified_in_dev_mode(self):
        daemon = self.daemon()
        daemon.hud = {"build": "dev", "git_head": None, "state": "dev"}
        self.world(daemon)
        self.assertEqual(self.rebuild(), O.UNVERIFIED)
        self.assertIn("JARVIS_HUD_DEV=1", self.said())

    def test_restart_is_refused_before_anything_is_built(self):
        _, programs = self.world(self.daemon(turns=1))
        self.assertEqual(self.rebuild(restart_after=True), O.BUSY)
        self.assertEqual(self.builds, 0)
        self.assertEqual(programs.calls, [])
        self.assertIn("--restart: 1 turn is running", self.said())

    def test_restart_after_the_install(self):
        _, programs = self.world(self.daemon())
        self.assertEqual(self.rebuild(restart_after=True), O.OK, self.said())
        self.assertEqual(programs.events, ["build", "systemctl show", "systemctl restart"])
        built = hud_install.newest(self.root)
        self.assertIn("The daemon is up", self.said())
        self.assertIn(f"The daemon serves {built.id}", self.said())

    def test_a_turn_that_started_during_the_build_stops_the_restart(self):
        daemon = self.daemon()
        _, programs = self.world(daemon)
        build = self.build

        def build_then_busy():
            build()
            daemon.turns = 1
        self.assertEqual(O.rebuild(out=self.out.append, build=build_then_busy, restart_after=True), O.BUSY)
        built = hud_install.newest(self.root)
        self.assertIn(f"installed {built.id}, but did not restart: 1 turn is running", self.said())
        self.assertNotIn("restart", programs.verbs())

    def test_force_restarts_through_a_turn(self):
        _, programs = self.world(self.daemon(turns=1))
        self.assertEqual(self.rebuild(restart_after=True, force=True), O.OK, self.said())
        self.assertIn("restart", programs.verbs())

    def test_a_refused_pull_builds_nothing(self):
        self.world(self.daemon())
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        self.assertEqual(self.rebuild(pull_first=True), O.FAILED)
        self.assertEqual(self.builds, 0)
        self.assertIn("jarvis hud rebuild: --pull fast-forwards main", self.said())

    def test_pull_then_build_names_python_to_restart_for(self):
        self.world(self.daemon())
        new = self.upstream()
        self.assertEqual(self.rebuild(pull_first=True), O.OK, self.said())
        self.assertEqual(self.head(), new)
        self.assertEqual(hud_install.newest(self.root).git_head, new)
        self.assertIn("Python changed in that pull (1 file)", self.said())
        self.assertIn("`jarvis restart`", self.said())

    def test_pull_with_restart_does_not_nag(self):
        self.world(self.daemon())
        self.upstream()
        self.assertEqual(self.rebuild(pull_first=True, restart_after=True), O.OK, self.said())
        self.assertNotIn("Python changed", self.said())


# -- the CLI ---------------------------------------------------------------------------


class Cli(Base):
    def main(self, *argv):
        from jarvis import __main__ as cli
        with patch.object(sys, "argv", ["jarvis", *argv]):
            return cli.main()

    def test_restart_spellings(self):
        with patch.object(O, "restart", return_value=7) as restart:
            self.assertEqual(self.main("restart"), 7)
            self.assertEqual(self.main("restart", "--force", "--wait", "12"), 7)
            self.assertEqual(self.main("daemon2", "restart"), 7)
            self.assertEqual(self.main("daemon2", "restart", "--force"), 7)
        self.assertEqual([c.kwargs for c in restart.call_args_list],
                         [{"force": False, "wait": O.WAIT}, {"force": True, "wait": 12.0},
                          {"force": False, "wait": O.WAIT}, {"force": True, "wait": O.WAIT}])

    def test_rebuild_spellings(self):
        with patch.object(O, "rebuild", return_value=5) as rebuild:
            self.assertEqual(self.main("hud", "rebuild"), 5)
            self.assertEqual(self.main("hud", "rebuild", "--pull", "--restart", "--clean",
                                       "--allow-dirty", "--force", "--wait", "45"), 5)
        self.assertEqual(rebuild.call_args_list[0].kwargs,
                         {"pull_first": False, "restart_after": False, "clean": False,
                          "allow_dirty": False, "force": False, "wait": O.WAIT})
        self.assertEqual(rebuild.call_args_list[1].kwargs,
                         {"pull_first": True, "restart_after": True, "clean": True,
                          "allow_dirty": True, "force": True, "wait": 45.0})

    def test_rebuild_options_are_only_for_rebuild(self):
        with patch.object(O, "rebuild") as rebuild, patch.object(hud_install, "main") as install:
            self.assertEqual(self.main("hud", "--pull"), 2)
            self.assertEqual(self.main("hud", "install", "--restart"), 2)
        rebuild.assert_not_called()
        install.assert_not_called()

    def test_daemon2_still_runs_the_daemon(self):
        from jarvis.v2 import daemon
        with patch.object(daemon, "main", return_value=0) as run, patch.object(O, "restart") as restart:
            self.assertEqual(self.main("daemon2"), 0)
            self.assertEqual(self.main("daemon2", "run"), 0)
            self.assertEqual(self.main("daemon2", "--force"), 2)
        self.assertEqual(run.call_count, 2)
        restart.assert_not_called()

    def test_wait_is_bounded(self):
        for bad in ("0", "-1", "601", "soon"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as caught:
                with patch("sys.stderr"):
                    self.main("restart", "--wait", bad)
            self.assertEqual(caught.exception.code, 2)

    def test_the_owners_cli_parses(self):
        env = {**os.environ, "HOME": str(self.tmp), "PYTHONPATH": str(REPO)}
        for argv, want in ((["restart", "--help"], "--force"), (["hud", "--help"], "rebuild"),
                           (["daemon2", "--help"], "restart")):
            out = REAL_RUN([sys.executable, "-m", "jarvis", *argv], cwd=str(self.tmp), env=env,
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertEqual(out.returncode, 0, out.stderr[-400:])
            self.assertIn(want, out.stdout)
            self.assertIn("Exit codes" if argv[0] != "hud" else "exit codes", out.stdout)


class NoLever(unittest.TestCase):
    def test_only_the_cli_imports_it(self):
        importers = []
        for path in sorted((REPO / "jarvis").rglob("*.py")):
            if path.name == "owner_ops.py":
                continue
            if re.search(r"import\s+owner_ops\b|\bowner_ops\s+import\b|import\s+[\w.]*\.owner_ops\b",
                         path.read_text()):
                importers.append(path.relative_to(REPO).as_posix())
        self.assertEqual(importers, ["jarvis/__main__.py"], importers)

    def test_no_registered_tool_reaches_it(self):
        from jarvis import tools
        from jarvis.v2.providers import fastpath
        words = ("restart", "rebuild", "owner_op", "systemctl")
        names = [n for n in list(tools.REGISTRY) + list(fastpath.FAST_TOOLS)
                 if any(w in n.lower() for w in words)]
        self.assertEqual(names, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)

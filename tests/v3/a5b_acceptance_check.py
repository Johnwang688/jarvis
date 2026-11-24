"""Orchestrator-owned acceptance tests for A5b: the v3 daemon serves the installed v3 HUD.

Implementers: do not edit; if one is wrong, stop and say why.

Contract
  config.HUD3_DIR    Path, env JARVIS_HUD3_DIR, default ~/.local/share/jarvis/hud3 (the install root)
  config.HUD3_BUILD  Path, REPO_ROOT/hud3/dist (what `npm run build` writes)
  jarvis.v3.daemon.Daemon(port, data_dir, hud_dir=None)   hud_dir defaults to config.HUD3_DIR, read at start
  jarvis.v3.hud
      install_hud(*, build=npm_build, allow_dirty=False) -> jarvis.v2.hud_install.Build
          build() first, then hud_install.install(config.HUD3_BUILD, config.HUD3_DIR);
          raises hud_install.InstallRefused with a sentence
      npm_build()        `npm ci --ignore-scripts` only when needed, then `npm run build`, in hud3/
      open_window(out)   -> int; face.server.launch_window("http://localhost:<V3_API_PORT>/")
  jarvis agent install   installs the HUD first, then the unit; a refused HUD leaves the unit alone
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import config, protected_state
from jarvis.tools import files
from jarvis.v2 import hud_install, permissions
from jarvis.v3 import cli, daemon, imports, switch

REPO = Path(__file__).resolve().parents[2]
INDEX = b'<!doctype html><title>J.A.R.V.I.S. Agent</title><script type="module" src="/assets/index-Ab3xY9_z.js"></script>'
FILES = {"index.html": INDEX, "assets/index-Ab3xY9_z.js": b"console.log(1)", "assets/index-Ab3xY9_z.css": b"body{}",
         "assets/font-x.woff2": b"wOF2", "icons/orb-16.png": b"\x89PNG", "manifest.webmanifest": b"{}",
         "build-manifest.json": b"{}", "odd.exe": b"MZ", "notes.txt": b"hi"}


def put(root, files=FILES):
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.addCleanup(self._unlock, tmp)
        self.home = Path(os.path.realpath(tmp.name))
        self.root = self.home / "share" / "hud3"
        self.built = put(self.home / "dist")

    def _unlock(self, tmp):
        for path in [self.home, *self.home.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o700)
        tmp.cleanup()

    def install(self, source=None):
        return hud_install.install(source or self.built, self.root)

    def start(self):
        d = daemon.Daemon(port=0, data_dir=self.home / "v3", hud_dir=self.root)
        d.start()
        self.addCleanup(d.stop)
        return d

    def get(self, d, path, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in {"Host": f"localhost:{d.port}", **(headers or {})}.items():
                c.putheader(k, v)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.headers, r.read()
        finally:
            c.close()


class Serving(Base):
    def test_the_installed_window_is_served(self):
        self.install()
        d = self.start()
        status, headers, body = self.get(d, "/")
        self.assertEqual((status, body), (200, INDEX))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        for path, kind in (("/assets/index-Ab3xY9_z.js", "text/javascript"), ("/assets/index-Ab3xY9_z.css", "text/css"),
                           ("/assets/font-x.woff2", "font/woff2"), ("/icons/orb-16.png", "image/png"),
                           ("/manifest.webmanifest", "application/manifest+json")):
            status, headers, body = self.get(d, path)
            self.assertEqual((status, body), (200, FILES[path[1:]]), path)
            self.assertEqual(headers["Content-Type"].split(";")[0], kind, path)

    def test_every_response_refuses_framing_and_other_origins_code(self):
        self.install()
        d = self.start()
        for path in ("/", "/assets/index-Ab3xY9_z.js", "/v3/status", "/nope"):
            _, headers, _ = self.get(d, path)
            self.assertEqual(headers.get_all("X-Frame-Options"), ["DENY"], path)
            self.assertEqual(headers.get_all("X-Content-Type-Options"), ["nosniff"], path)
            (csp,) = headers.get_all("Content-Security-Policy")
            rules = {p.split()[0]: p.split()[1:] for p in csp.split(";") if p.strip()}
            self.assertEqual(rules["frame-ancestors"], ["'none'"], path)
            self.assertEqual(rules.get("default-src"), ["'none'"], path)
        _, headers, _ = self.get(d, "/")
        rules = {p.split()[0]: p.split()[1:] for p in headers["Content-Security-Policy"].split(";") if p.strip()}
        self.assertEqual(rules["script-src"], ["'self'"])
        self.assertEqual(rules["connect-src"], ["'self'"])
        self.assertEqual(rules["style-src"], ["'self'", "'unsafe-inline'"])
        self.assertEqual(rules["img-src"], ["'self'", "data:"])
        self.assertEqual(rules["font-src"], ["'self'", "data:"])
        for name in ("worker-src", "manifest-src"):
            self.assertEqual(rules[name], ["'self'"], name)
        for name in ("base-uri", "form-action", "object-src"):
            self.assertEqual(rules[name], ["'none'"], name)
        self.assertNotIn("frame-src", {k for k, v in rules.items() if v != ["'none'"]})
        self.assertNotIn("*", headers["Content-Security-Policy"])

    def test_head_has_the_length_and_no_body(self):
        self.install()
        d = self.start()
        status, headers, body = self.get(d, "/", "HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, str(len(INDEX)), b""))

    def test_nothing_else_is_served(self):
        self.install()
        (self.home / "secret.txt").write_text("planted-text")
        d = self.start()
        build = next(p for p in self.root.iterdir() if p.is_dir())
        for path in ("/index.html/", "/manifest.json", "/build-manifest.json", "/odd.exe", "/notes.txt", "/assets/", "/assets",
                     "/assets/missing.js", "/../secret.txt", "/assets/../../../secret.txt", "/%2e%2e/secret.txt",
                     "/assets/%2e%2e/index.html", "/assets%2findex-Ab3xY9_z.js", "//index.html", "/./index.html", "/assets//index-Ab3xY9_z.js",
                     "/assets/..\\index.html", f"/{build.name}/index.html", "/index.html%00", "/v3", "/v3/", "/v3/status/", "/anything",
                     "/assets/index-Ab3xY9_z.js/", "/ASSETS/index-Ab3xY9_z.js"):
            status, _, body = self.get(d, path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"planted-text", body, path)
            self.assertNotIn(INDEX, body, path)

    def test_a_query_never_changes_the_file(self):
        self.install()
        d = self.start()
        self.assertEqual(self.get(d, "/?x=../../secret")[0::2], (200, INDEX))
        self.assertEqual(self.get(d, "/assets/index-Ab3xY9_z.js?v=1")[0], 200)

    def test_status_still_answers_exactly(self):
        self.install()
        d = self.start()
        status, _, body = self.get(d, "/v3/status")
        self.assertEqual((status, json.loads(body)["service"]), (200, "jarvis-agentd"))

    def test_a_tampered_file_is_not_served(self):
        self.install()
        d = self.start()
        build = next(p for p in self.root.iterdir() if p.is_dir())
        for rel in ("assets/index-Ab3xY9_z.js", "index.html"):
            path = build / rel
            path.parent.chmod(0o755)
            path.chmod(0o644)
            path.write_bytes(b"evil()")
            status, _, body = self.get(d, "/" if rel == "index.html" else "/" + rel)
            self.assertNotEqual(status, 200, rel)
            self.assertNotIn(b"evil", body)

    def test_a_linked_file_or_root_is_not_served(self):
        self.install()
        link = self.home / "link"
        link.symlink_to(self.root)
        d = daemon.Daemon(port=0, data_dir=self.home / "v3", hud_dir=link)
        d.start()
        self.addCleanup(d.stop)
        self.assertEqual(self.get(d, "/")[0], 503)
        self.assertEqual(self.get(d, "/assets/index-Ab3xY9_z.js")[0], 404)

    def test_no_install_says_how_and_status_still_works(self):
        d = self.start()
        status, _, body = self.get(d, "/")
        self.assertEqual(status, 503)
        self.assertIn("jarvis agent install", json.loads(body)["error"])
        self.assertEqual(self.get(d, "/v3/status")[0], 200)
        self.assertEqual(self.get(d, "/assets/index-Ab3xY9_z.js")[0], 404)

    def test_an_install_shows_without_a_restart_and_old_chunks_still_load(self):
        self.install()
        d = self.start()
        newer = put(self.home / "dist2", {"index.html": b"<!doctype html>new", "assets/index-new.js": b"2"})
        self.install(newer)
        self.assertEqual(self.get(d, "/")[2], b"<!doctype html>new")
        self.assertEqual(self.get(d, "/assets/index-Ab3xY9_z.js")[0], 200)
        self.assertEqual(self.get(d, "/icons/orb-16.png")[0], 404)     # only hashed assets outlive their build

    def test_host_and_origin_still_guard_the_window(self):
        self.install()
        d = self.start()
        self.assertEqual(self.get(d, "/", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.get(d, "/assets/index-Ab3xY9_z.js", headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.get(d, "/", "POST")[0], 405)

    def test_the_default_root_is_read_when_the_daemon_starts(self):
        self.install()
        with patch.object(config, "HUD3_DIR", self.root):
            d = daemon.Daemon(port=0, data_dir=self.home / "v3")
            d.start()
            self.addCleanup(d.stop)
            self.assertEqual(self.get(d, "/")[0], 200)


class Paths(unittest.TestCase):
    def test_config(self):
        code = "from jarvis import config; print(config.HUD3_DIR); print(config.HUD3_BUILD)"
        env = {**os.environ, "HOME": "/nonexistent-home", "PYTHONPATH": str(REPO)}
        env.pop("JARVIS_HUD3_DIR", None)
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["/nonexistent-home/.local/share/jarvis/hud3", str(REPO / "hud3" / "dist")])
        out = subprocess.run([sys.executable, "-c", code], env={**env, "JARVIS_HUD3_DIR": "/x/y"}, capture_output=True, text=True,
                             check=True).stdout.split()
        self.assertEqual(out[0], "/x/y")

    def test_v3_may_import_the_installer(self):
        self.assertIn("jarvis.v2.hud_install", imports.ALLOWED_V2)
        self.assertEqual(imports.violations(REPO / "jarvis" / "v3"), [])

    def test_importing_the_daemon_loads_no_v2_daemon_or_model(self):
        code = ("import sys; import jarvis.v3.daemon, jarvis.v3.cli; "
                "print([m for m in ('jarvis.llm','jarvis.agent','jarvis.v2.daemon','jarvis.v2.hud_api','jarvis.face.server') if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)},
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "[]")


class Protected(Base):
    """An agent must not be able to write what the v3 window runs."""

    def test_write_tools_refuse_the_install_root(self):
        self.install()
        with patch.object(config, "HUD3_DIR", self.root):
            for target in (self.root, self.root / "x.js", self.root / "20991231T000000000000Z-aaaaaaaaaaaa" / "index.html"):
                self.assertTrue(files._self_protected(target), target)
            link = self.home / "innocent"
            link.symlink_to(self.root)
            self.assertTrue(files._self_protected(link / "index.html"))
            self.assertFalse(files._self_protected(self.home / "other" / "x.js"))

    def test_the_v2_root_is_still_protected(self):
        with patch.object(config, "HUD3_DIR", self.root):
            self.assertTrue(files._self_protected(Path(config.HUD_DIR) / "x.js"))

    def test_an_unresolvable_root_refuses(self):
        with patch.object(files, "_hud_roots", side_effect=OSError("no")):
            self.assertTrue(files._hud_build(self.home / "x"))

    def test_v2_grants_refuse_it(self):
        self.install()
        with patch.object(config, "HUD3_DIR", self.root):
            self.assertTrue(permissions.denied_file(str(self.root / "x.js")))

    def test_shell_writes_into_it_are_never_allowed(self):
        from jarvis import permissions as P, rules
        with patch.object(config, "HUD3_DIR", self.root):
            for line in (f"cp /tmp/x.js {self.root}/x.js", f"echo 1 > {self.root}/index.html", f"mv /tmp/b {self.root}/b",
                         f"rsync -a /tmp/dist/ {self.root}/new/"):
                verdict = P.static_verdict(line)
                self.assertEqual(verdict.decision, rules.ASK, line)
                self.assertIn("installed HUD", verdict.reason)
            self.assertNotEqual(P.static_verdict(f"rm -rf {self.root}").decision, rules.ALLOW)
            self.assertEqual(P.static_verdict(f"cat {self.root}/index.html").decision, rules.ALLOW)

    def test_agents_cannot_run_the_installer(self):
        refused = (
            "jarvis agent install", "python -m jarvis agent install", "python -m jarvis.v3.hud", "python jarvis/v3/hud.py",
            'python -c "from jarvis.v3 import hud; hud.install_hud()"', 'python -c "from jarvis.v3.hud import install_hud as f; f()"',
            'python -c "import jarvis.v3.hud as h; h.npm_build()"', 'python -c "from jarvis.v3 import hud; hud.main()"',
            'python -c "from jarvis.v2 import hud_install as h; h.install(1, 2)"',
        )
        for line in refused:
            self.assertIsNotNone(protected_state.owner_op(line), line)
        for line in ("python hud.py", "python app/hud.py", "python -m hud", 'python -c "import hud; hud.main()"', "cat jarvis/v3/hud.py",
                     "git diff jarvis/v3/hud.py", 'python -c "from jarvis.v3 import hud; print(hud.__doc__)"',
                     "python -m pytest tests/v3", "python tests/v3/a5b_acceptance_check.py", "jarvis agent status"):
            self.assertIsNone(protected_state.owner_op(line), line)


class Install(Base):
    def setUp(self):
        super().setUp()
        from jarvis.v3 import hud
        self.hud = hud
        for name, value in (("HUD3_DIR", self.root), ("HUD3_BUILD", self.built)):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_it_builds_then_installs(self):
        seen = []
        built = self.hud.install_hud(build=lambda: seen.append("built"), allow_dirty=True)
        self.assertEqual(seen, ["built"])
        self.assertEqual(hud_install.newest(self.root).id, built.id)
        self.assertEqual(hud_install.read(built, "index.html"), INDEX)
        self.assertEqual(built.git_head, hud_install.load(self.root, built.id).git_head)

    def test_a_failed_build_installs_nothing(self):
        def fail():
            raise hud_install.InstallRefused("the build failed")
        with self.assertRaises(hud_install.InstallRefused):
            self.hud.install_hud(build=fail, allow_dirty=True)
        self.assertFalse(self.root.exists())

    def test_a_refused_root_is_refused_before_the_build(self):
        target = self.home / "real"
        target.mkdir()
        self.root.parent.mkdir(parents=True)
        self.root.symlink_to(target)
        with self.assertRaises(hud_install.InstallRefused):
            self.hud.install_hud(build=lambda: self.fail("built"), allow_dirty=True)

    def test_uncommitted_frontend_changes_are_refused_before_the_build(self):
        with patch.object(self.hud, "frontend_dirty", return_value=True):
            with self.assertRaises(hud_install.InstallRefused):
                self.hud.install_hud(build=lambda: self.fail("built"))
            self.hud.install_hud(build=lambda: None, allow_dirty=True)
        with patch.object(self.hud, "frontend_dirty", return_value=None):      # git cannot say
            with self.assertRaises(hud_install.InstallRefused):
                self.hud.install_hud(build=lambda: self.fail("built"))

    def test_frontend_dirty_counts_the_shared_source_too(self):
        repo = self.home / "repo"
        for path in ("hud3/src/main.tsx", "hud/src/lib/a.ts", "jarvis/x.py"):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text("1")
        git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                                        check=True, capture_output=True)
        git("init", "-q")
        git("add", ".")
        git("commit", "-qm", "x")
        with patch.object(config, "REPO_ROOT", repo):
            self.assertIs(self.hud.frontend_dirty(), False)
            (repo / "jarvis/x.py").write_text("2")
            self.assertIs(self.hud.frontend_dirty(), False)
            for path in ("hud3/src/main.tsx", "hud/src/lib/a.ts"):
                (repo / path).write_text("2")
                self.assertIs(self.hud.frontend_dirty(), True, path)
                git("checkout", "-q", "--", path)
            (repo / "hud3/src/new.ts").write_text("1")
            self.assertIs(self.hud.frontend_dirty(), True)
        with patch.object(config, "REPO_ROOT", self.home / "not-a-repo"):
            self.assertIsNone(self.hud.frontend_dirty())

    def test_npm_never_runs_package_scripts_and_skips_an_up_to_date_install(self):
        repo = self.home / "repo"
        hud3 = repo / "hud3"
        hud3.mkdir(parents=True)
        (hud3 / "package-lock.json").write_text("{}")
        runs = []

        def run(args, **kwargs):
            runs.append((list(args), kwargs))
            if "ci" in args:
                (hud3 / "node_modules").mkdir(exist_ok=True)
                (hud3 / "node_modules" / ".package-lock.json").write_text("{}")
            if "build" in args:
                self.built.mkdir(exist_ok=True)
                (self.built / "index.html").write_bytes(INDEX)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(config, "REPO_ROOT", repo), patch.object(hud_install, "npm_path", return_value="/usr/bin/npm"), \
                patch.object(self.hud.subprocess, "run", side_effect=run):
            self.hud.npm_build()
            self.assertEqual([r[0][1:] for r in runs], [["ci", "--ignore-scripts"], ["run", "build"]])
            self.assertTrue(all(Path(r[1]["cwd"]) == hud3 and r[1].get("timeout") for r in runs))
            runs.clear()
            self.hud.npm_build()
            self.assertEqual([r[0][1:] for r in runs], [["run", "build"]])
            runs.clear()
            os.utime(hud3 / "package-lock.json", (2 ** 31, 2 ** 31))
            self.hud.npm_build()
            self.assertEqual(runs[0][0][1:], ["ci", "--ignore-scripts"])

    def test_an_npm_failure_is_a_sentence(self):
        (self.home / "repo" / "hud3").mkdir(parents=True)
        with patch.object(config, "REPO_ROOT", self.home / "repo"), patch.object(hud_install, "npm_path", return_value="/usr/bin/npm"), \
                patch.object(self.hud.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="x")):
            with self.assertRaises(hud_install.InstallRefused):
                self.hud.npm_build()
        with patch.object(hud_install, "npm_path", side_effect=hud_install.InstallRefused("no npm")):
            with self.assertRaises(hud_install.InstallRefused):
                self.hud.npm_build()


class Command(unittest.TestCase):
    def setUp(self):
        from jarvis.v3 import hud
        self.hud = hud

    def test_install_puts_the_hud_first(self):
        order, lines = [], []
        env = SimpleNamespace(out=lines.append)
        built = hud_install.Build("b", Path("/x"), {}, "abc", False)
        with patch.object(switch, "default_env", return_value=env), \
                patch.object(self.hud, "install_hud", side_effect=lambda **k: order.append(("hud", k)) or built), \
                patch.object(switch, "install_unit", side_effect=lambda *a, **k: order.append(("unit", None)) or 0):
            self.assertEqual(cli.main(["agent", "install"]), 0)
            self.assertEqual([o[0] for o in order], ["hud", "unit"])
            self.assertFalse(order[0][1].get("allow_dirty", False))
            order.clear()
            self.assertEqual(cli.main(["agent", "install", "--allow-dirty"]), 0)
            self.assertTrue(order[0][1]["allow_dirty"])

    def test_a_refused_hud_leaves_the_unit_alone(self):
        lines = []
        with patch.object(switch, "default_env", return_value=SimpleNamespace(out=lines.append)), \
                patch.object(self.hud, "install_hud", side_effect=hud_install.InstallRefused("hud3 has uncommitted changes")), \
                patch.object(switch, "install_unit") as unit:
            self.assertNotEqual(cli.main(["agent", "install"]), 0)
            unit.assert_not_called()
        self.assertIn("uncommitted", " ".join(lines))

    def test_start_opens_the_window_only_after_a_good_switch(self):
        from jarvis.face import server
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "HUD3_DIR", Path(tmp) / "none"):
            lines = []
            env = SimpleNamespace(out=lines.append)
            with patch.object(switch, "default_env", return_value=env), patch.object(server, "launch_window") as launch:
                with patch.object(switch, "to_v3", return_value=3):
                    self.assertEqual(cli.main(["agent", "start"]), 3)
                launch.assert_not_called()
                with patch.object(switch, "to_v3", return_value=0):
                    self.assertEqual(cli.main(["agent", "start"]), 0)
                launch.assert_called_once_with(f"http://localhost:{config.V3_API_PORT}/")
                self.assertIn("jarvis agent install", " ".join(lines))     # nothing installed: say so, still open

    def test_a_window_that_cannot_open_does_not_undo_the_switch(self):
        from jarvis.face import server
        for error in (ValueError("profile"), OSError("no browser"), FileNotFoundError("x")):
            lines = []
            with patch.object(switch, "default_env", return_value=SimpleNamespace(out=lines.append)), \
                    patch.object(switch, "to_v3", return_value=0), patch.object(server, "launch_window", side_effect=error):
                self.assertEqual(cli.main(["agent", "start"]), 0)
            self.assertTrue(any("window" in line.lower() for line in lines), lines)

    def test_the_installed_v3_app_is_used_when_there_is_one(self):
        from jarvis.face import server
        with tempfile.TemporaryDirectory() as tmp:
            url = f"http://localhost:{config.V3_API_PORT}/"
            app_id = server.chrome_app_id(url)
            base = Path(tmp) / "Google" / "Chrome" / "User Data" / "Default" / "Web Applications" / "Manifest Resources"
            with patch.object(config, "WINDOWS_LOCALAPPDATA", tmp), patch.object(config, "HUD_CHROME_PROFILE", "Default"), \
                    patch.object(server, "find_browser", return_value=("/mnt/c/x/chrome.exe", True)), \
                    patch.object(server.subprocess, "Popen") as popen:
                server.launch_window(url)
                self.assertIn(f"--app={url}", popen.call_args.args[0])
                (base / app_id).mkdir(parents=True)
                server.launch_window(url)
                self.assertIn(f"--app-id={app_id}", popen.call_args.args[0])
                server.launch_window("http://localhost:1/")
                self.assertIn("--app=http://localhost:1/", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

"""PH1 listener acceptance tests using only ephemeral loopback sockets."""

from email.message import Message
from http.client import HTTPConnection, HTTPResponse
import json
import os
from pathlib import Path
import select
import socket
from subprocess import CompletedProcess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from jarvis import config
from jarvis.v3 import hud, owner_routes, phone, phone_auth, switch
from jarvis.v3.daemon import Daemon, _status_handler


HOST = "phone.example.invalid"
ORIGIN = "https://" + HOST
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)


def request(port, method, path, *, host=HOST, origin=None, headers=None, body=None):
    options = {"Host": host, **(headers or {})}
    if origin is not None:
        options["Origin"] = origin
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        options["Content-Type"] = "application/json"
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=options)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


class PhoneListenerCheck(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "JARVIS_REMOTE_HOST": ""}))
        self.enterContext(patch.object(config, "remote_dir", return_value=self.root / "remote"))
        self.hud = self.root / "hud"

    def start(self, **kwargs):
        daemon = Daemon(port=0, data_dir=self.root / "data", hud_dir=self.hud, jobs=(), **kwargs)
        daemon.start()
        self.addCleanup(daemon.stop)
        return daemon

    def window(self, daemon, path):
        return request(daemon.port, "GET", path, host=f"127.0.0.1:{daemon.port}")

    def raw_response(self, port, head, body=b""):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            if body:
                sock.sendall(head + body[:16])
                self.assertFalse(select.select([sock], [], [], 0.15)[0], "answer before body")
                sock.sendall(body[16:])
            else:
                sock.sendall(head)
            sock.shutdown(socket.SHUT_WR)
            with HTTPResponse(sock) as response:
                response.begin()
                payload = response.read()
                self.assertEqual(sock.recv(1), b"")
                return response.status, response.headers, payload

    def test_01_no_host(self):
        with patch.object(phone, "bind", wraps=phone.bind) as bind:
            daemon = self.start()
        bind.assert_not_called()
        self.assertIsNone(daemon.phone_port)
        self.assertEqual(json.loads(self.window(daemon, "/v3/status")[2])["phone"],
                         {"bound": False, "host_configured": False})

    def test_02_occupied_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with (patch.object(config, "remote_host", return_value=HOST),
                  self.assertLogs("jarvis.v3.phone", "WARNING") as logs):
                daemon = self.start(phone_port=occupied.getsockname()[1])
            self.assertIsNone(daemon.phone_port)
            status, _, data = self.window(daemon, "/v3/status")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data)["phone"], {"bound": False, "host_configured": True})
            self.assertEqual(len(logs.output), 1)
            self.assertNotIn(HOST, logs.output[0])

    def test_03_exact_host_before_dispatch(self):
        port = self.start(phone_host=HOST).phone_port
        with patch.object(phone_auth, "auth_route", wraps=phone_auth.auth_route) as route:
            for host in ("example.invalid", HOST.upper(), HOST + "."):
                with self.subTest(host=host):
                    self.assertEqual(request(port, "GET", "/m/api/me", host=host)[0], 400)
            duplicate = f"GET /m/api/me HTTP/1.1\r\nHost: {HOST}\r\nHost: {HOST}\r\n\r\n"
            folded = f"GET /m/api/me HTTP/1.1\r\nHost:\r\n {HOST}\r\n\r\n"
            for wire in (duplicate, folded):
                self.assertEqual(self.raw_response(port, wire.encode())[0], 400)
            route.assert_not_called()

    def test_04_exact_origin(self):
        port = self.start(phone_host=HOST).phone_port
        for origin in (None, f"http://{HOST}", ORIGIN + "/"):
            with self.subTest(origin=origin):
                self.assertEqual(request(port, "POST", "/m/api/logout", origin=origin)[0], 403)
        self.assertEqual(request(port, "POST", "/m/api/logout", origin=ORIGIN)[0], 401)
        self.assertEqual(request(port, "TRACE", "/elsewhere")[0], 403)

    def test_05_only_phone_paths_and_one_redirect(self):
        port = self.start(phone_host=HOST).phone_port
        status, headers, body = request(port, "GET", "/")
        self.assertEqual((status, headers["Location"], body), (302, "/m/", b""))
        for method, path in (("GET", "/elsewhere"), ("HEAD", "/"), ("POST", "/"),
                             ("GET", "//m/"), ("GET", "/?other"), ("GET", "/m/sw.js")):
            with self.subTest(method=method, path=path):
                self.assertEqual(request(port, method, path, origin=ORIGIN)[0], 404)

    def test_06_window_route_absent_from_phone(self):
        port = self.start(phone_host=HOST).phone_port
        self.assertEqual(request(port, "GET", "/v3/owner/feed")[0], 404)

    def test_07_phone_route_absent_from_window(self):
        daemon = self.start(phone_host=HOST)
        self.assertEqual(self.window(daemon, "/m/")[0], 404)

    def test_08_window_identity_rejected_before_owner_check(self):
        port = self.start(phone_host=HOST).phone_port
        with patch.object(owner_routes, "is_owner", wraps=owner_routes.is_owner) as owner:
            status, _, _ = request(port, "GET", "/v3/owner/feed",
                                   host=f"127.0.0.1:{port}", headers={"Sec-Fetch-Site": "same-origin"})
            self.assertEqual(status, 400)
            owner.assert_not_called()

    def test_09_owner_refuses_remote_server(self):
        daemon = self.start(phone_host=HOST)
        headers = Message()
        headers["Host"] = f"127.0.0.1:{daemon.phone_port}"
        headers["Sec-Fetch-Site"] = "same-origin"
        handler = SimpleNamespace(server=daemon._phone_server, headers=headers,
                                  folded_headers=set(), command="GET")
        self.assertFalse(owner_routes.is_owner(handler))
        with patch.object(handler.server, "remote_only", False):
            self.assertTrue(owner_routes.is_owner(handler))

    def test_10_body_consumed_before_401_or_403(self):
        port = self.start(phone_host=HOST).phone_port
        body = b"x" * (10 * 1024)
        for origin, expected in (("", 403), (f"Origin: {ORIGIN}\r\n", 401)):
            with self.subTest(expected=expected):
                head = (f"POST /m/api/logout HTTP/1.1\r\nHost: {HOST}\r\n"
                        f"{origin}Content-Length: {len(body)}\r\n\r\n").encode()
                self.assertEqual(self.raw_response(port, head, body)[0], expected)
        head = (f"POST /m/api/logout HTTP/1.1\r\nHost: {HOST}\r\n"
                f"Origin: {ORIGIN}\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
        chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        self.assertEqual(self.raw_response(port, head, chunked)[0], 401)

    def test_11_drains_oversize_before_refusal(self):
        port = self.start(phone_host=HOST).phone_port
        body = b"x" * (64 * 1024 + 1)
        head = (f"POST /m/api/logout HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        status, _, payload = self.raw_response(port, head, body)
        self.assertEqual((status, json.loads(payload)), (413, {"error": "request body too large"}))
        head = (f"POST /m/api/logout HTTP/1.1\r\nHost: {HOST}\r\n"
                f"Origin: {ORIGIN}\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
        chunked = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        self.assertEqual(self.raw_response(port, head, chunked)[0], 413)
        self.assertEqual((phone.BODY_CAP, phone.DRAIN_CAP, phone.BODY_DEADLINE_S),
                         (64 * 1024, 1024 * 1024, 10))

    def test_12_headers_include_malformed_and_missing_build(self):
        port = self.start(phone_host=HOST).phone_port
        malformed = self.raw_response(port, b"GET /m/ HTTP/invalid\r\n\r\n")
        ambiguous = self.raw_response(
            port, (f"POST /m/api/me HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
                   "Content-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
        self.assertEqual(json.loads(malformed[2]), {"error": "bad request"})
        self.assertEqual(json.loads(ambiguous[2]), {"error": "bad request"})
        for expected, (status, headers, _) in (
            (400, malformed), (400, ambiguous),
            (302, request(port, "GET", "/")), (503, request(port, "GET", "/m/")),
            (503, request(port, "GET", "/m/pair")), (401, request(port, "GET", "/m/api/me")),
            (400, request(port, "GET", "/m/", host="example.invalid")),
            (404, request(port, "GET", "/m/assets/missing.js")),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(status, expected)
                self.assertEqual(headers["Content-Security-Policy"], CSP)
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_13_only_phone_entry_assets(self):
        source = self.root / "dist"
        files = {
            "index.html": "window",
            "phone.html": '<script type="module" src="./assets/phone.js"></script>',
            "build-manifest.json": json.dumps({
                "index.html": {"file": "assets/window.js"},
                "phone.html": {"file": "assets/phone.js", "imports": ["shared"]},
                "shared": {"file": "assets/shared.js", "css": ["assets/shared.css"],
                           "dynamicImports": ["dynamic"]},
                "dynamic": {"file": "assets/dynamic.js", "assets": ["assets/phone.woff2"]},
            }),
            "icons/orb-192.png": "icon",
        }
        for name in ("phone.js", "shared.js", "shared.css", "phone.woff2", "dynamic.js", "window.js"):
            files[f"assets/{name}"] = name
        for name, content in files.items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        hud.hud_install.install(source, self.hud)
        port = self.start(phone_host=HOST).phone_port
        for path in ("/m/", "/m/pair"):
            status, _, data = request(port, "GET", path)
            self.assertEqual((status, data), (200, files["phone.html"].encode()))
        for name in ("phone.js", "shared.js", "shared.css", "phone.woff2", "dynamic.js"):
            with self.subTest(name=name):
                status, headers, body = request(port, "GET", f"/m/assets/{name}")
                self.assertEqual((status, body), (200, name.encode()))
                self.assertIn("immutable", headers["Cache-Control"])
        for path in ("/m/assets/window.js", "/m/assets/../index.html", "/m/phone.html"):
            self.assertEqual(request(port, "GET", path)[0], 404)
        status, headers, payload = request(port, "GET", "/m/manifest.webmanifest")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/manifest+json"))
        manifest = json.loads(payload)
        self.assertEqual([manifest[key] for key in ("id", "start_url", "scope")], ["/m/"] * 3)
        self.assertEqual(request(port, "GET", "/m/icons/orb-192.png")[2], b"icon")

    def test_14_stop_releases_port_and_detaches_auth_before_save(self):
        daemon = self.start(phone_host=HOST)
        port = daemon.phone_port
        self.assertEqual(json.loads(self.window(daemon, "/v3/status")[2])["phone"],
                         {"bound": True, "host_configured": True})
        auth = daemon._phone_server.auth
        original_save = auth.final_save

        def check_order():
            self.assertIsNone(auth.server)
            with socket.socket() as replacement:
                replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    replacement.bind(("127.0.0.1", port))
                except OSError as exc:
                    self.fail(f"phone port was still held: {exc}")
            self.assertEqual(self.window(daemon, "/v3/status")[0], 200)
            return original_save()

        with patch.object(auth, "final_save", side_effect=check_order) as save:
            daemon.stop()
            save.assert_called_once_with()
        self.assertIsNone(daemon.phone_port)

    def test_15_unit_carries_host_without_printing_value(self):
        folder = self.root / "units"
        folder.mkdir()
        source = folder / "jarvis-daemon2.service"
        target = folder / "jarvis-agent.service"
        lines = []
        systemctl = Mock(return_value=CompletedProcess([], 0, "", ""))
        env = SimpleNamespace(systemctl=systemctl, out=lines.append)

        def install():
            return switch.install_unit(env, unit_path=target, python="/home/user/python", repo=Path("/home/user"))

        self.assertEqual(install(), 0)
        self.assertNotIn("Environment=", target.read_text())
        self.assertTrue(any("drop-in" in line and "JARVIS_REMOTE_HOST=" in line for line in lines))
        source.write_text(f"[Service]\nEnvironment=JARVIS_REMOTE_HOST={HOST}\n")
        self.assertEqual(install(), 0)
        self.assertIn(f"Environment=JARVIS_REMOTE_HOST={HOST}\n", target.read_text())
        self.assertTrue(any("carried over" in line for line in lines))
        installed = target.read_bytes()
        drop_ins = source.with_name(source.name + ".d")
        drop_ins.mkdir()
        (drop_ins / "20-phone.conf").write_text(
            f'[Service]\nEnvironment=OTHER=fake-token "JARVIS_REMOTE_HOST={HOST}"\n')
        source.write_text("[Service]\nEnvironment=OTHER=fake-token\n")
        reloads = systemctl.call_count
        self.assertEqual(install(), 0)
        self.assertEqual(systemctl.call_count, reloads)
        self.assertEqual(target.read_bytes(), installed)
        target.unlink()
        self.assertEqual(install(), 0)
        self.assertEqual(target.read_bytes(), installed)
        (drop_ins / "30-phone.conf").write_text("[Service]\nEnvironment=\n")
        self.assertEqual(install(), 0)
        self.assertEqual(target.read_bytes(), installed)
        self.assertNotIn(HOST, "\n".join(lines))
        self.assertTrue(all(call.args == ("daemon-reload",) for call in systemctl.call_args_list))


if __name__ == "__main__":
    unittest.main()

"""Wire-level checks for the v3 owner route boundary."""

from http.client import HTTPResponse, parse_headers
import json
from pathlib import Path
import select
import socket
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from jarvis.v3 import owner_routes
from jarvis.v3.daemon import Daemon
from jarvis.v3.owner_routes import ROUTES, Route


PING = "/v3/owner/ping"
WHOAMI = "/v3/owner/whoami"


class OwnerRoutes(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.daemon = Daemon(port=0, data_dir=root / "data", hud_dir=root / "hud")
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.host = f"127.0.0.1:{self.daemon.port}"
        self.origin = f"http://{self.host}"
        self.handler = self.daemon._server.RequestHandlerClass

    def wire(self, method, path, headers, body=b""):
        lines = [f"{method} {path} HTTP/1.1", *headers, "", ""]
        return "\r\n".join(lines).encode("ascii") + body

    def request(self, method, path, headers, body=b""):
        with socket.create_connection(self.daemon.address, timeout=2) as client:
            client.sendall(self.wire(method, path, headers, body))
            with HTTPResponse(client, method=method) as response:
                response.begin()
                return response.status, response.headers, response.read()

    def post(self, body=b"{}", *, headers=None, path=PING):
        if headers is None:
            headers = [f"Host: {self.host}", f"Origin: {self.origin}",
                       "Content-Type: application/json", f"Content-Length: {len(body)}"]
        return self.request("POST", path, headers, body)

    def test_host_route_owner_type_length_and_body_in_order(self):
        host, origin = f"Host: {self.host}", f"Origin: {self.origin}"
        json_type = "Content-Type: application/json"
        bad_type = "Content-Type: text/plain"
        incomplete = "Content-Length: 2"
        with patch.object(self.handler, "_read_body", return_value=bytearray(b"{}")) as read:
            self.assertEqual(self.request("PUT", PING, ["Host: example.invalid", bad_type])[0], 421)
            with patch.object(owner_routes, "is_owner", wraps=owner_routes.is_owner) as identity:
                status, headers, _ = self.request("PUT", PING, [host, "Origin: null", bad_type])
                self.assertEqual((status, headers["Allow"]), (405, "POST"))
                status, headers, _ = self.post(b"", headers=[host, bad_type], path=PING + "/extra")
                self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"))
                identity.assert_not_called()
            self.assertEqual(self.post(b"", headers=[host, json_type, incomplete])[0], 403)
            read.assert_not_called()
            self.assertEqual(self.post(b"", headers=[host, bad_type])[0], 403)
            self.assertEqual(self.post(b"", headers=[host, incomplete])[0], 403)
            self.assertEqual(self.post(b"", headers=[host, json_type, incomplete,
                                                    "Sec-Fetch-Site: same-origin"])[0], 403)
            self.assertEqual(self.post(b"", headers=[host, origin, json_type, incomplete,
                                                    "Sec-Fetch-Site: none"])[0], 403)
            self.assertEqual(self.post(b"", headers=[host, origin, bad_type])[0], 415)
            self.assertEqual(self.post(b"", headers=[host, origin, json_type])[0], 411)
            self.assertEqual(self.post(b"", headers=[host, origin, json_type,
                                                    "Content-Length: 65537"])[0], 413)
        read.assert_not_called()

    def test_header_names_are_case_insensitive_and_values_are_exact(self):
        accepted = [f"hOsT: {self.host}", f"oRiGiN: {self.origin}",
                    "cOnTeNt-TyPe: application/json; charset=utf-8", "cOnTeNt-LeNgTh: 2"]
        self.assertEqual(self.post(headers=accepted)[0], 200)
        for index, value, status in (
            (0, f"Host: LOCALHOST:{self.daemon.port}", 421),
            (0, f"Host:\r\n\t{self.host}", 421),
            (1, f"Origin: HTTP://{self.host}", 403),
            (1, f"Origin: {self.origin}\r\n\t", 403),
            (1, f"Origin: {self.origin}/", 403),
            (2, "Content-Type: APPLICATION/JSON", 415),
            (2, "Content-Type: application/json;\r\n\tcharset=utf-8", 415),
            (3, "Content-Length:\r\n\t2", 400),
        ):
            with self.subTest(header=value):
                headers = accepted.copy()
                headers[index] = value
                self.assertEqual(self.post(headers=headers)[0], status)

    def test_duplicate_headers_are_refused_even_when_values_agree(self):
        accepted = [f"Host: {self.host}", f"Origin: {self.origin}",
                    "Content-Type: application/json", "Content-Length: 2"]
        for duplicate, status in (
            (f"hOsT: {self.host}", 421), (f"oRiGiN: {self.origin}", 403),
            ("cOnTeNt-TyPe: application/json", 415), ("cOnTeNt-LeNgTh: 2", 400),
        ):
            with self.subTest(header=duplicate):
                self.assertEqual(self.post(headers=[*accepted, duplicate])[0], status)

    def test_head_uses_the_get_owner_rule_and_omits_the_body(self):
        host = f"Host: {self.host}"
        headers = [host, "sEc-FeTcH-sItE: same-origin"]
        status, _, data = self.request("GET", WHOAMI, headers)
        self.assertEqual((status, json.loads(data)), (200, {"owner": True}))
        status, reply, body = self.request("HEAD", WHOAMI, headers)
        self.assertEqual((status, int(reply["Content-Length"]), body), (200, len(data), b""))
        for extra in ([], ["Sec-Fetch-Site: same-origin", "Upgrade: websocket"],
                      ["Sec-Fetch-Site: same-origin", "Origin: null"]):
            self.assertEqual(self.request("HEAD", WHOAMI, [host, *extra])[0::2], (403, b""))

    def test_absolute_targets_and_noncanonical_hosts_are_refused(self):
        self.assertEqual(self.post(path=self.origin + PING)[0], 405)
        headers = [f"Host: {self.host}", "Sec-Fetch-Site: same-origin"]
        self.assertEqual(self.request("GET", self.origin + WHOAMI, headers)[0], 404)
        for host in (f"[::1]:{self.daemon.port}", "127.0.0.1", "localhost"):
            self.assertEqual(self.request("GET", WHOAMI, [f"Host: {host}", headers[1]])[0], 421)

    def test_refused_request_closes_with_a_second_request_waiting(self):
        first = self.wire("POST", PING, [f"Host: {self.host}", "Connection: keep-alive",
                                        "Content-Type: application/json", "Content-Length: 2"], b"{}")
        second = self.wire("GET", WHOAMI, [f"Host: {self.host}", f"Origin: {self.origin}"])
        called = Mock(return_value=(200, {"owner": True}))
        with patch.dict(ROUTES, {("GET", WHOAMI): Route(called)}), \
                patch.object(self.handler, "protocol_version", "HTTP/1.1"), \
                socket.create_connection(self.daemon.address, timeout=2) as client:
            client.sendall(first + second)
            with client.makefile("rb") as stream:
                self.assertEqual(stream.readline().split()[1], b"403")
                reply = parse_headers(stream)
                self.assertEqual(reply["Connection"], "close")
                self.assertIn("error", json.loads(stream.read(int(reply["Content-Length"]))))
                self.assertEqual(stream.read(1), b"")
        called.assert_not_called()

    def test_stalled_and_dripping_bodies_hit_the_same_deadline(self):
        request = self.wire("POST", PING, [f"Host: {self.host}", f"Origin: {self.origin}",
                                          "Content-Type: application/json", "Content-Length: 128"])
        for drip in (False, True):
            with self.subTest(drip=drip), patch.object(self.handler, "timeout", 0.5), \
                    socket.create_connection(self.daemon.address, timeout=2) as client:
                client.sendall(request)
                deadline = time.monotonic() + 1.5
                while not select.select([client], [], [], 0.05)[0]:
                    self.assertLess(time.monotonic(), deadline, "body read outlived its deadline")
                    if drip:
                        client.sendall(b" ")
                with HTTPResponse(client, method="POST") as response:
                    response.begin()
                    self.assertEqual((response.status, response.headers["Connection"]), (408, "close"))

    def test_invalid_byte_encodings_and_deep_json_never_reach_the_handler(self):
        called = Mock(return_value=(200, {}))
        with patch.dict(ROUTES, {("POST", PING): Route(called)}):
            for body in (b"\xef\xbb\xbf{}", "{}".encode("utf-16"),
                         b'{"x":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}"):
                self.assertEqual(self.post(body)[0], 400)
            called.assert_not_called()
            self.assertEqual(self.post(b'{"x":[0]}')[0], 200)
            called.assert_called_once_with({"x": [0]})

    def test_non_json_handler_returns_a_plain_500(self):
        with patch.dict(ROUTES, {("POST", PING): Route(lambda body: (200, {"fake-detail": object()}))}):
            status, headers, body = self.post()
        self.assertEqual((status, headers["Content-Type"]), (500, "application/json"))
        self.assertEqual(json.loads(body), {"error": "Internal Server Error"})
        self.assertEqual(self.post()[0], 200)


if __name__ == "__main__":
    unittest.main()

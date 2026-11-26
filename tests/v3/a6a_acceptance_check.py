"""Orchestrator-owned acceptance tests for A6a (implementers: do not edit; if one is wrong, stop and say why).

Contract: the v3 window's owner routes. No action yet: two harmless routes prove the rules.
  jarvis/v3/owner_routes.py
    Route(handler, *, owner=True)                handler(body: dict | None) -> (status, json-able); owner routes are the default
    ROUTES: dict[(method, path), Route]          the ONLY routes besides GET/HEAD of /v3/status and the HUD's static files
       ("GET", "/v3/owner/whoami") -> 200 {"owner": true}
       ("POST", "/v3/owner/ping")  -> 200 {"ok": true}
    PUBLIC: frozenset of (method, path)          routes declared owner=False; empty in this PR
    is_owner(handler) -> bool                    v2's rule (`jarvis/v2/projects.py::is_owner`) on v3's one listener:
       an `Origin` equal to `http://<Host>` exactly; or a GET/HEAD with no Origin, no Upgrade, and `Sec-Fetch-Site: same-origin`.
       `Origin: null`, an empty Origin, two Origin headers, `Sec-Fetch-Site: cross-site|same-site|none` on a write: never the owner.
  Every request still passes the strict Host test first (421). A refused owner route is 403 and runs nothing.
  POST: only for a table route (else 404 or 405, nothing runs); `Content-Type: application/json` exactly (parameters allowed) else 415 or 400;
  `Content-Length` required (411), at most MAX_BODY = 64 KiB (413), no chunked bodies; the body must be a JSON object (400).
  PUT, PATCH, DELETE, OPTIONS, CONNECT, TRACE: 405 everywhere. Answers are JSON with the existing security headers; no CORS header is ever sent.
"""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.v3 import daemon, owner_routes
from jarvis.v3.owner_routes import ROUTES, Route


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.calls = []
        self.d = daemon.Daemon(port=0, data_dir=self.tmp / "v3", hud_dir=self.tmp / "hud3")
        self.d.start()
        self.addCleanup(self.d.stop)
        self.port = self.d.port
        self.host = f"127.0.0.1:{self.port}"

    def request(self, method, path, *, body=None, headers=None, host=None, raw_headers=()):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", self.host if host is None else host)
        for name, value in {**(headers or {})}.items():
            conn.putheader(name, value)
        for name, value in raw_headers:
            conn.putheader(name, value)
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        if data is not None and "Content-Length" not in (headers or {}) and "Transfer-Encoding" not in (headers or {}):
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        text = resp.read()
        conn.close()
        return resp, text

    def owner(self, **extra):
        return {"Origin": f"http://{self.host}", "Content-Type": "application/json", **extra}

    def ping(self, **kw):
        kw.setdefault("body", {})
        kw.setdefault("headers", self.owner())
        return self.request("POST", "/v3/owner/ping", **kw)

    def spy(self):
        """Count every handler run, whatever the route."""
        def wrap(route):
            def handler(body):
                self.calls.append(body)
                return route.handler(body)
            return Route(handler, owner=route.owner)
        patcher = patch.dict(ROUTES, {key: wrap(route) for key, route in ROUTES.items()})
        patcher.start()
        self.addCleanup(patcher.stop)


class Owner(Base):
    def test_the_window_can_ping(self):
        resp, text = self.ping()
        self.assertEqual((resp.status, json.loads(text)), (200, {"ok": True}))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        resp, text = self.ping(host=f"localhost:{self.port}", headers={"Origin": f"http://localhost:{self.port}", "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(resp.status, 200)

    def test_owner_reads(self):
        resp, text = self.request("GET", "/v3/owner/whoami", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual((resp.status, json.loads(text)), (200, {"owner": True}))
        resp, _ = self.request("GET", "/v3/owner/whoami", headers={"Origin": f"http://{self.host}"})
        self.assertEqual(resp.status, 200)
        for headers in ({}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "none"},
                        {"Sec-Fetch-Site": "same-origin", "Upgrade": "websocket"}, {"Origin": "null"}):
            resp, text = self.request("GET", "/v3/owner/whoami", headers=headers)
            self.assertEqual(resp.status, 403, headers)
            self.assertNotIn(b"true", text)
        resp, _ = self.request("GET", "/v3/status")                 # the status read stays open to local tools
        self.assertEqual(resp.status, 200)

    def test_not_the_owner(self):
        self.spy()
        other = self.port + 1
        cases = [{"Content-Type": "application/json"},                                              # no Origin: a tool, not the window
                 self.owner(Origin="null"), self.owner(Origin=""), self.owner(Origin=f"http://127.0.0.1:{other}"),
                 self.owner(Origin=f"http://localhost:{self.port}"),                                # another spelling than Host
                 self.owner(Origin=f"https://{self.host}"), self.owner(Origin=f"http://{self.host}/"),
                 self.owner(Origin=f"http://{self.host}.example.invalid"), self.owner(Origin="http://example.invalid"),
                 self.owner(**{"Sec-Fetch-Site": "cross-site"}), self.owner(**{"Sec-Fetch-Site": "same-site"})]
        for headers in cases:
            resp, _ = self.ping(headers=headers)
            self.assertEqual(resp.status, 403, headers)
        resp, _ = self.ping(headers={"Content-Type": "application/json"}, raw_headers=[("Origin", f"http://{self.host}"), ("Origin", "http://example.invalid")])
        self.assertEqual(resp.status, 403)
        resp, _ = self.ping(headers={"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"})   # the read rule never admits a write
        self.assertEqual(resp.status, 403)
        for host in ("example.invalid", f"127.0.0.1.nip.io:{self.port}", f"LOCALHOST:{self.port}", "127.0.0.1", ""):
            resp, _ = self.ping(host=host, headers={"Origin": f"http://{host}", "Content-Type": "application/json"})
            self.assertEqual(resp.status, 421, host)
        self.assertEqual(self.calls, [])


class Bodies(Base):
    def test_json_objects_only(self):
        self.spy()
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx", "text/json", ""):
            resp, _ = self.ping(headers=self.owner(**{"Content-Type": content_type}))
            self.assertIn(resp.status, (400, 415), content_type)
        headers = self.owner()
        del headers["Content-Type"]
        self.assertIn(self.ping(headers=headers)[0].status, (400, 415))
        for body in (b"", b"[]", b"3", b'"x"', b"null", b"{", b'{"a": 1} trailing', b"\xff\xfe", b'{"a": NaN}'):
            resp, _ = self.ping(body=body)
            self.assertEqual(resp.status, 400, body)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ping(body={"a": [1, {"b": None}]})[0].status, 200)
        self.assertEqual(self.calls, [{"a": [1, {"b": None}]}])

    def test_size_and_length(self):
        self.spy()
        big = b'{"a": "' + b"x" * (64 * 1024) + b'"}'
        self.assertEqual(self.ping(body=big)[0].status, 413)
        self.assertEqual(self.ping(body=b"{}", headers=self.owner(**{"Content-Length": str(10 ** 12)}))[0].status, 413)
        for bad in ("-1", "abc", "2, 2", ""):
            resp, _ = self.ping(body=b"{}", headers=self.owner(**{"Content-Length": bad}))
            self.assertIn(resp.status, (400, 411, 413), bad)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/v3/owner/ping", skip_host=True)
        for name, value in {"Host": self.host, **self.owner(), "Transfer-Encoding": "chunked"}.items():
            conn.putheader(name, value)
        conn.endheaders(b"2\r\n{}\r\n0\r\n\r\n")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertIn(resp.status, (400, 411, 501))
        self.assertEqual(self.calls, [])
        self.assertEqual(owner_routes.MAX_BODY, 64 * 1024)


class Table(Base):
    def test_the_table_is_the_whole_surface(self):
        self.assertLessEqual({
            ("GET", "/v3/owner/whoami"), ("POST", "/v3/owner/ping"), ("POST", "/v3/owner/proposal"),
        }, set(ROUTES))
        self.assertEqual({key for key in ROUTES if key[1].startswith("/v3/owner/voice")}, {
            ("GET", "/v3/owner/voice"), ("POST", "/v3/owner/voice/settings"),
            ("POST", "/v3/owner/voice/helper"),
        })
        self.assertTrue(all(method in ("GET", "POST") and path.startswith("/v3/owner/") for method, path in ROUTES))   # later PRs add routes; all stay under /v3/owner/
        self.assertEqual(owner_routes.PUBLIC, frozenset())
        self.assertTrue(all(route.owner for route in ROUTES.values()))
        self.assertTrue(Route(lambda body: (200, {})).owner)      # owner-only unless a route says otherwise

    def test_unlisted_routes_and_methods_are_refused(self):
        self.spy()
        for path in ("/v3/owner/ping/", "/v3/owner/ping?x=1", "/v3/owner/PING", "//v3/owner/ping", "/v3/owner/../owner/ping", "/v3/owner/ping%2f", "/v3/status",
                     "/v3/owner/whoami", "/", "/v3/owner", "/assets/x.js", "/v3/owner/ping#x"):
            resp, _ = self.request("POST", path, body={}, headers=self.owner())
            self.assertIn(resp.status, (404, 405), path)
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            for path in ("/v3/owner/ping", "/v3/status", "/"):
                resp, _ = self.request(method, path, body={}, headers={**self.owner(), "Access-Control-Request-Method": "POST"})
                self.assertEqual(resp.status, 405, (method, path))
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
                self.assertIsNone(resp.getheader("Access-Control-Allow-Methods"))
        self.assertEqual(self.request("GET", "/v3/owner/ping", headers={"Sec-Fetch-Site": "same-origin"})[0].status in (404, 405), True)
        self.assertEqual(self.calls, [])

    def test_a_new_route_is_owner_only_without_asking(self):
        ran = []
        with patch.dict(ROUTES, {("POST", "/v3/owner/new-thing"): Route(lambda body: (ran.append(body), (200, {"did": 1}))[1])}):
            resp, _ = self.request("POST", "/v3/owner/new-thing", body={}, headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 403)
            resp, text = self.request("POST", "/v3/owner/new-thing", body={"x": 1}, headers=self.owner())
            self.assertEqual((resp.status, json.loads(text)), (200, {"did": 1}))
        self.assertEqual(ran, [{"x": 1}])

    def test_a_handler_that_raises_is_a_plain_500(self):
        def boom(body):
            raise RuntimeError("private detail /home/user/secret")
        with patch.dict(ROUTES, {("POST", "/v3/owner/boom"): Route(boom)}):
            resp, text = self.request("POST", "/v3/owner/boom", body={}, headers=self.owner())
        self.assertEqual(resp.status, 500)
        self.assertNotIn(b"private", text)
        self.assertEqual(self.ping()[0].status, 200)               # the daemon is still up


if __name__ == "__main__":
    unittest.main()

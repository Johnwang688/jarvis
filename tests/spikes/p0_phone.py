"""Spike P-0 (b) and (c) for the phone surface (docs/plans/2026-10-24-phone-surface-design.md §10).

A throwaway server on 127.0.0.1:8408, reached from the owner's iPhone through
`tailscale serve` (spike (a), `p0_network.py`). The page, added to the home
screen, answers three questions on the real phone:

  (b) a passkey registered and asserted with user verification, once with
      Face ID and once with Face ID off (the passcode), verified by `webauthn`;
      an assertion bound to "allow" is refused when replayed as "always", and a
      used challenge is refused a second time (PH-4);
  (c) a content-free Web Push ({"count", "rank"} only, PH-8) reaches the
      home-screen app with the screen locked.

Not product code: no Jarvis module imports it and it shares no state with the
daemon. Its own state (VAPID key, credentials, push subscription, findings)
is in ~/.cache/jarvis-p0 (0700). Run it with a venv that has `webauthn` and
`pywebpush`:

    uv venv ~/.cache/c0-spike/.venv && uv pip install --python ~/.cache/c0-spike/.venv/bin/python webauthn pywebpush
    ~/.cache/c0-spike/.venv/bin/python tests/spikes/p0_phone.py

Findings go to ~/.cache/jarvis-p0/findings.jsonl, one line per event; the
result is written into the design document's §10 by hand.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import webauthn
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)
from py_vapid import Vapid
from pywebpush import WebPushException, webpush
from cryptography.hazmat.primitives import serialization

PORT = 8408
STATE = Path.home() / ".cache" / "jarvis-p0"
ICONS = Path(__file__).resolve().parents[2] / "hud" / "public" / "icons"
TAILSCALE = "/mnt/c/Program Files/Tailscale/tailscale.exe"
CHALLENGE_TTL_S = 60


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ts_name() -> str:
    """This machine's MagicDNS name, from Tailscale itself (never hardcoded)."""
    out = subprocess.run([TAILSCALE, "status", "--json"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["Self"]["DNSName"].rstrip(".")


NAME = ts_name()
ORIGIN = f"https://{NAME}"
_lock = threading.Lock()
_reg_challenges: dict[str, float] = {}           # b64url challenge -> expiry
_auth_challenges: dict[str, dict] = {}           # b64url challenge -> {answer, req_id, expires, used}


def finding(event: str, **data) -> None:
    line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **data}
    with _lock, open(STATE / "findings.jsonl", "a") as fh:
        fh.write(json.dumps(line) + "\n")
    print(f"[p0] {event} {json.dumps(data)}", flush=True)


def load(name: str, default):
    path = STATE / name
    return json.loads(path.read_text()) if path.exists() else default


def save(name: str, value) -> None:
    path = STATE / name
    path.write_text(json.dumps(value, indent=1))
    path.chmod(0o600)


def vapid() -> Vapid:
    pem = STATE / "vapid.pem"
    if not pem.exists():
        key = Vapid()
        key.generate_keys()
        key.save_key(str(pem))
        pem.chmod(0o600)
    return Vapid.from_file(str(pem))


def application_server_key() -> str:
    raw = vapid().public_key.public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)
    return b64url(raw)


def send_push(delay_s: float) -> None:
    time.sleep(delay_s)
    sub = load("push.json", None)
    if not sub:
        finding("push_send", ok=False, why="no subscription")
        return
    payload = json.dumps({"count": 1, "rank": 1})       # PH-8: content-free
    try:
        resp = webpush(subscription_info=sub, data=payload,
                       vapid_private_key=str(STATE / "vapid.pem"),
                       vapid_claims={"sub": ORIGIN}, ttl=120)
        finding("push_send", ok=True, status=resp.status_code, payload=payload, delay_s=delay_s)
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        finding("push_send", ok=False, status=status)   # never the endpoint URL


class Handler(BaseHTTPRequestHandler):
    server_version = "p0"

    def log_message(self, fmt, *args):  # quiet; findings carry what matters
        pass

    # -- plumbing ------------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value) -> None:
        self._send(code, json.dumps(value).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        # tailscale serve forwards the original Host; a local curl sends 127.0.0.1:8408.
        if host not in (NAME, f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._json(400, {"error": "untrusted Host"})
            return False
        return True

    # -- routes ----------------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(200, {"ok": True, "name": NAME})
        if path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/sw.js":
            return self._send(200, SW.encode(), "text/javascript",
                              {"Service-Worker-Allowed": "/"})
        if path == "/manifest.webmanifest":
            return self._send(200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png"):
            size = path[6:9]
            return self._send(200, (ICONS / f"orb-{size}.png").read_bytes(), "image/png")
        if path == "/push/key":
            return self._json(200, {"key": application_server_key()})
        if path == "/findings":
            text = (STATE / "findings.jsonl").read_text() if (STATE / "findings.jsonl").exists() else ""
            return self._send(200, text.encode(), "text/plain; charset=utf-8")
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        origin = self.headers.get("Origin")
        if origin != ORIGIN:
            finding("refused_origin", origin=origin, path=self.path)
            return self._json(403, {"error": "bad Origin"})
        try:
            body = self._body()
        except ValueError:
            return self._json(400, {"error": "bad JSON"})
        route = {
            "/log": self.r_log,
            "/reg/options": self.r_reg_options, "/reg/verify": self.r_reg_verify,
            "/auth/options": self.r_auth_options, "/auth/verify": self.r_auth_verify,
            "/push/subscribe": self.r_push_subscribe, "/push/send": self.r_push_send,
        }.get(self.path)
        if route is None:
            return self._json(404, {"error": "not found"})
        route(body)

    def r_log(self, body):
        # Prefixed: a client field named "event" would collide with finding()'s own
        # argument (the 2026-10-25 run lost every client line to that TypeError).
        finding("client", **{f"client_{k}": str(v)[:300] for k, v in body.items()},
                user_agent=self.headers.get("User-Agent", "")[:200])
        self._json(200, {"ok": True})

    def r_reg_options(self, body):
        opts = webauthn.generate_registration_options(
            rp_id=NAME, rp_name="Jarvis (P-0 spike)", user_name="owner",
            user_id=b"jarvis-owner-p0",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["id"] + "=="))
                                 for c in load("creds.json", [])])
        with _lock:
            _reg_challenges[b64url(opts.challenge)] = time.time() + CHALLENGE_TTL_S
        self._send(200, webauthn.options_to_json(opts).encode(), "application/json")

    def r_reg_verify(self, body):
        cred = body.get("credential") or {}
        try:
            client = json.loads(base64.urlsafe_b64decode(cred["response"]["clientDataJSON"] + "=="))
            chal = client["challenge"]
        except Exception:
            return self._json(400, {"error": "malformed credential"})
        with _lock:
            expiry = _reg_challenges.pop(chal, None)
        if expiry is None or expiry < time.time():
            finding("register", ok=False, why="unknown or expired challenge")
            return self._json(409, {"error": "unknown or expired challenge"})
        try:
            v = webauthn.verify_registration_response(
                credential=cred, expected_challenge=base64.urlsafe_b64decode(chal + "=="),
                expected_rp_id=NAME, expected_origin=ORIGIN, require_user_verification=True)
        except Exception as exc:
            finding("register", ok=False, why=str(exc)[:200])
            return self._json(400, {"error": str(exc)[:200]})
        creds = load("creds.json", [])
        creds.append({"id": b64url(v.credential_id), "public_key": b64url(v.credential_public_key),
                      "sign_count": v.sign_count})
        save("creds.json", creds)
        finding("register", ok=True, user_verified=v.user_verified, sign_count=v.sign_count,
                device_type=str(v.credential_device_type), backed_up=v.credential_backed_up,
                aaguid=v.aaguid)
        self._json(200, {"ok": True, "user_verified": v.user_verified})

    def r_auth_options(self, body):
        answer = body.get("answer")
        if answer not in ("allow", "always"):
            return self._json(400, {"error": "answer must be allow or always"})
        creds = load("creds.json", [])
        if not creds:
            return self._json(409, {"error": "register a passkey first"})
        req_id = secrets.token_urlsafe(9)                       # stands in for an approval id
        nonce = secrets.token_bytes(16)
        challenge = hashlib.sha256(req_id.encode() + b"\x00" + answer.encode() + b"\x00" + nonce).digest()
        opts = webauthn.generate_authentication_options(
            rp_id=NAME, challenge=challenge,
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(c["id"] + "=="))
                               for c in creds],
            user_verification=UserVerificationRequirement.REQUIRED)
        with _lock:
            _auth_challenges[b64url(challenge)] = {"answer": answer, "req_id": req_id,
                                                   "expires": time.time() + CHALLENGE_TTL_S,
                                                   "used": False}
        self._send(200, webauthn.options_to_json(opts).encode(), "application/json")

    def r_auth_verify(self, body):
        cred, claimed, method = body.get("credential") or {}, body.get("answer"), body.get("method", "")
        try:
            client = json.loads(base64.urlsafe_b64decode(cred["response"]["clientDataJSON"] + "=="))
            chal = client["challenge"]
        except Exception:
            return self._json(400, {"error": "malformed credential"})
        with _lock:
            pending = _auth_challenges.get(chal)
            why = None
            if pending is None:
                why = "unknown challenge"
            elif pending["used"]:
                why = "challenge already used"
            elif pending["expires"] < time.time():
                why = "challenge expired"
            elif pending["answer"] != claimed:
                why = f"assertion was for {pending['answer']!r}, not {claimed!r}"
            if why is None:
                pending["used"] = True                          # single-use, even if verify fails
        if why is not None:
            finding("assert", ok=False, why=why, claimed=claimed, method=method)
            return self._json(409, {"error": why})
        creds = load("creds.json", [])
        stored = next((c for c in creds if c["id"] == cred.get("id")), None)
        if stored is None:
            finding("assert", ok=False, why="unknown credential", method=method)
            return self._json(400, {"error": "unknown credential"})
        try:
            v = webauthn.verify_authentication_response(
                credential=cred, expected_challenge=base64.urlsafe_b64decode(chal + "=="),
                expected_rp_id=NAME, expected_origin=ORIGIN,
                credential_public_key=base64.urlsafe_b64decode(stored["public_key"] + "=="),
                credential_current_sign_count=stored["sign_count"], require_user_verification=True)
        except Exception as exc:
            finding("assert", ok=False, why=str(exc)[:200], method=method)
            return self._json(400, {"error": str(exc)[:200]})
        stored["sign_count"] = v.new_sign_count
        save("creds.json", creds)
        finding("assert", ok=True, answer=claimed, method=method, user_verified=v.user_verified,
                sign_count=v.new_sign_count, backed_up=v.credential_backed_up)
        self._json(200, {"ok": True, "user_verified": v.user_verified})

    def r_push_subscribe(self, body):
        sub = body.get("subscription") or {}
        if not isinstance(sub.get("endpoint"), str) or not sub.get("keys"):
            return self._json(400, {"error": "malformed subscription"})
        save("push.json", sub)
        host = sub["endpoint"].split("/")[2]                     # the push service, not the capability URL
        finding("push_subscribe", ok=True, push_service=host)
        self._json(200, {"ok": True, "push_service": host})

    def r_push_send(self, body):
        delay = max(0.0, min(float(body.get("delay_s") or 0), 120.0))
        threading.Thread(target=send_push, args=(delay,), daemon=True).start()
        self._json(200, {"ok": True, "delay_s": delay})


MANIFEST = {
    "name": "Jarvis P-0", "short_name": "Jarvis P-0", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#141413", "theme_color": "#141413",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
}

SW = """
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) {}
  const n = Number.isInteger(d.count) ? d.count : 1;
  e.waitUntil(self.registration.showNotification('Jarvis', {
    body: n + ' waiting' + (d.rank === 1 ? ' - an approval expires soon' : ''),
    icon: '/icon-192.png', tag: 'jarvis-inbox'
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.openWindow('/?from=push'));
});
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Jarvis P-0">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-192.png">
<title>Jarvis P-0</title>
<style>
  :root { --bg:#141413; --pane:#1a1a19; --raised:#222221; --text:#e8e6e1; --dim:#a19f99;
          --accent:#6fc3df; --amber:#e0a84a; --red:#e06c5a; --ok:#7fbf7f; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.45 -apple-system, system-ui, sans-serif;
         padding: max(16px, env(safe-area-inset-top)) 16px 32px; }
  h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:15px; margin:22px 0 8px; color:var(--dim);
       text-transform:uppercase; letter-spacing:.06em; }
  p { margin:6px 0; color:var(--dim); }
  button { display:block; width:100%; min-height:48px; margin:8px 0; border:1px solid #3a3a38; border-radius:8px;
           background:var(--raised); color:var(--text); font:inherit; }
  button:active { border-color:var(--accent); }
  .row { background:var(--pane); border:1px solid #2c2c2a; border-radius:8px; padding:10px 12px; margin:6px 0; }
  #log div { font:13px/1.4 ui-monospace, Menlo, monospace; padding:3px 0; border-bottom:1px solid #262624;
             white-space:pre-wrap; word-break:break-word; }
  .ok { color:var(--ok); } .bad { color:var(--red); } .warn { color:var(--amber); }
</style></head><body>
<h1>Jarvis P-0</h1>
<p>Phone-surface spike. Nothing here touches Jarvis.</p>
<div class="row" id="mode"></div>

<h2>1 · Passkey</h2>
<button id="reg">Create passkey</button>
<button id="faceid">Verify with Face ID (answer: allow)</button>
<p>Then turn Face ID off for this test (Settings → Face ID &amp; Passcode → "Use Face ID for" → Password
AutoFill / Passkeys off), come back, and:</p>
<button id="passcode">Verify with passcode (answer: allow)</button>
<button id="replay">Replay test (allow assertion sent as "always")</button>

<h2>2 · Notifications</h2>
<button id="sub">Allow notifications</button>
<button id="push">Send a test push in 20 s, then lock the phone</button>

<h2>Log</h2>
<div id="log"></div>

<script>
const $ = id => document.getElementById(id);
const b2u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const u2b = s => { s = s.replace(/-/g,'+').replace(/_/g,'/'); while (s.length % 4) s += '=';
                   return Uint8Array.from(atob(s), c => c.charCodeAt(0)); };
function log(text, cls) { const d = document.createElement('div'); d.textContent = text; if (cls) d.className = cls;
                          $('log').prepend(d); }
async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
  const j = await r.json().catch(() => ({}));
  return {status:r.status, body:j};
}
const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
$('mode').textContent = standalone ? 'Running as the home-screen app ✓'
  : 'Open in Safari → Share → Add to Home Screen, then use the icon. Push only works there.';
$('mode').className = 'row ' + (standalone ? 'ok' : 'warn');
post('/log', {event:'page_open', standalone, push: 'PushManager' in window,
              webauthn: !!window.PublicKeyCredential, from: location.search});

function credJSON(c) {
  const r = c.response, out = {id:c.id, rawId:b2u(c.rawId), type:c.type,
    clientExtensionResults:c.getClientExtensionResults(), authenticatorAttachment:c.authenticatorAttachment,
    response:{clientDataJSON:b2u(r.clientDataJSON)}};
  if (r.attestationObject) { out.response.attestationObject = b2u(r.attestationObject);
                             out.response.transports = r.getTransports ? r.getTransports() : []; }
  if (r.authenticatorData) { out.response.authenticatorData = b2u(r.authenticatorData);
                             out.response.signature = b2u(r.signature);
                             if (r.userHandle) out.response.userHandle = b2u(r.userHandle); }
  return out;
}

$('reg').onclick = async () => {
  try {
    const o = (await post('/reg/options')).body;
    o.challenge = u2b(o.challenge); o.user.id = u2b(o.user.id);
    (o.excludeCredentials||[]).forEach(c => c.id = u2b(c.id));
    const c = await navigator.credentials.create({publicKey:o});
    const r = await post('/reg/verify', {credential:credJSON(c)});
    log('register: ' + JSON.stringify(r.body), r.status === 200 ? 'ok' : 'bad');
  } catch (e) { log('register failed: ' + e.name + ': ' + e.message, 'bad'); post('/log', {event:'register_error', error:e.name+': '+e.message}); }
};

let lastAssertion = null;
async function verify(method, answer) {
  try {
    const o = (await post('/auth/options', {answer})).body;
    if (!o.challenge) { log('options: ' + JSON.stringify(o), 'bad'); return; }
    o.challenge = u2b(o.challenge); (o.allowCredentials||[]).forEach(c => c.id = u2b(c.id));
    const c = await navigator.credentials.get({publicKey:o});
    lastAssertion = credJSON(c);
    const r = await post('/auth/verify', {credential:lastAssertion, answer, method});
    log(method + ': ' + JSON.stringify(r.body), r.status === 200 ? 'ok' : 'bad');
  } catch (e) { log(method + ' failed: ' + e.name + ': ' + e.message, 'bad'); post('/log', {event:'assert_error', method, error:e.name+': '+e.message}); }
}
$('faceid').onclick = () => verify('faceid', 'allow');
$('passcode').onclick = () => verify('passcode', 'allow');
$('replay').onclick = async () => {
  if (!lastAssertion) { log('verify once first', 'warn'); return; }
  const a = await post('/auth/verify', {credential:lastAssertion, answer:'always', method:'replay-as-always'});
  log('replay as always → ' + a.status + ' ' + JSON.stringify(a.body), a.status === 409 ? 'ok' : 'bad');
  const b = await post('/auth/verify', {credential:lastAssertion, answer:'allow', method:'replay-same'});
  log('replay same → ' + b.status + ' ' + JSON.stringify(b.body), b.status === 409 ? 'ok' : 'bad');
};

$('sub').onclick = async () => {
  try {
    if (!standalone) log('not the home-screen app: iOS will refuse', 'warn');
    const reg = await navigator.serviceWorker.register('/sw.js', {scope:'/'});
    await navigator.serviceWorker.ready;
    const perm = await Notification.requestPermission();
    log('permission: ' + perm, perm === 'granted' ? 'ok' : 'bad');
    if (perm !== 'granted') return;
    const key = (await (await fetch('/push/key')).json()).key;
    const sub = await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:u2b(key)});
    const r = await post('/push/subscribe', {subscription:sub.toJSON()});
    log('subscribed: ' + JSON.stringify(r.body), r.status === 200 ? 'ok' : 'bad');
  } catch (e) { log('subscribe failed: ' + e.name + ': ' + e.message, 'bad'); post('/log', {event:'subscribe_error', error:e.name+': '+e.message}); }
};
$('push').onclick = async () => {
  const r = await post('/push/send', {delay_s:20});
  log('push queued: ' + JSON.stringify(r.body) + ' - lock the phone now', 'warn');
};
if (location.search.includes('from=push')) post('/log', {event:'opened_from_notification'});
</script></body></html>
"""


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    vapid()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    finding("server_start", name=NAME, origin=ORIGIN, port=PORT)
    print(f"P-0 spike on 127.0.0.1:{PORT}; open {ORIGIN}/ on the iPhone", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

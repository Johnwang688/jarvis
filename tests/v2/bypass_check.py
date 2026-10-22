"""The owner's Bypass switch (2026-10-21): the daemon, the owner-only route and
what they record. The provider half (the hook's explicit allow, a DENY staying
a deny, the decision-log line, the refusals) is `bypass_checks` in
tests/v2/claude_provider_check.py.

Free and hermetic: chat_relay_check's harness (temp config and V2_DATA_DIR,
every listener on port 0, never 8402/8403/8405 or the owner's state) with its
scripted fakes; the `claude` one takes the switch here, the others have none.

Run:
  systemd-run --user --scope -q -p MemoryMax=3G -p MemorySwapMax=0 -- env \
    PYTHONPATH=$PWD TMPDIR=/var/tmp OPENROUTER_API_KEY=sk-dummy-not-a-key \
    JARVIS_DISCORD_TOKEN=/nonexistent/discord_token.json \
    /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/bypass_check.py

TEST -> MUTATION
  RouteOwnerOnly.test_api_listener_is_refused_before_anything_is_read
      `owner_only` removed, or moved after the lookup or the body read
  RouteOwnerOnly.test_route_shape
      a body without `on`, a non-bool `on`, extra fields or a query accepted
  SourceGuard.test_only_the_owner_route_reaches_set_bypass
      a second caller of `Daemon.set_bypass` (a tool, the bridge, Discord)
  Switch.test_on_and_off_are_recorded_once
      no notice, a notice per repeat, no bus event, no decision-log line
  Switch.test_thread_json_reads_the_live_switch
      `bypass` missing from `thread_json`, or stored rather than live
  Refusals.*
      a provider with no classifier, a refusal sentence ignored, a task
      thread or archived chat switched on; turning off ever refused
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat_relay_check as CR  # noqa: E402  (its harness and Fake; not its tests)
import mcp_session_check as M  # noqa: E402  (helpers only)
from jarvis import config  # noqa: E402
from jarvis.v2 import daemon as daemon_mod  # noqa: E402
from jarvis.v2.model import ProviderName  # noqa: E402

CLAUDE, CODEX, FAST = ProviderName.CLAUDE, ProviderName.CODEX, ProviderName.FAST


class BypassFake(CR.Fake):
    """The provider's three Bypass methods, as `ClaudeProvider` has them.
    `refusal[tid]` is what `bypass_refusal` says."""

    def __init__(self, name):
        super().__init__(name)
        self.on, self.refusal, self.calls = set(), {}, []

    def bypass_refusal(self, h):
        return self.refusal.get(h.thread_id)

    def set_bypass(self, h, on):
        self.calls.append((h.thread_id, on))
        if on and self.refusal.get(h.thread_id):
            raise RuntimeError("refused")
        (self.on.add if on else self.on.discard)(h.thread_id)

    def bypassed(self, h):
        return h.thread_id in self.on


class Base(CR.Harness):
    def make_daemon(self):
        self.fakes = {FAST: CR.Fake(FAST), CLAUDE: BypassFake(CLAUDE), CODEX: CR.Fake(CODEX)}
        return super().make_daemon()

    def owner(self):
        return {"Origin": f"http://127.0.0.1:{self.d.face_port}"}

    def post(self, thread_id, body, *, query=""):
        return M.http(self.d.face_port, "POST", f"/threads/{thread_id}/bypass{query}",
                      body=body, headers=self.owner())

    def notices(self, thread):
        return [(r.get("data") or {}).get("text") for r in self.log(thread, "notice")]

    def decisions(self):
        path = Path(config.V2_DATA_DIR) / "decisions.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in rows if r.get("layer") == "owner-bypass"]


class RouteOwnerOnly(Base):
    def test_api_listener_is_refused_before_anything_is_read(self):
        t = self.chat(CLAUDE)
        path = f"/threads/{t.id}/bypass"
        for headers in (None, {"Origin": f"http://127.0.0.1:{self.d.port}"}, self.owner()):
            status, body = M.http(self.d.port, "POST", path, body={"on": True}, headers=headers)
            self.assertEqual(status, 403, f"the API listener is never the owner ({headers}): {body}")
        status, _ = M.http(self.d.port, "POST", path, raw=b"{not json")
        self.assertEqual(status, 403, "the owner check comes before the body is read")
        status, _ = M.http(self.d.port, "POST", f"/threads/{'0' * 8}/bypass", body={"on": True})
        self.assertEqual(status, 403, "the owner check comes before the thread is looked up")
        status, _ = M.http(self.d.face_port, "POST", path, body={"on": True})
        self.assertEqual(status, 403, "the HUD listener without the HUD's Origin is not the owner")
        self.assertEqual(self.fakes[CLAUDE].calls, [], "nothing was switched")
        self.assertEqual(self.notices(t), [])

    def test_route_shape(self):
        t = self.chat(CLAUDE)
        for body in ({}, {"on": "yes"}, {"on": 1}, {"on": True, "now": True}):
            status, _ = self.post(t.id, body)
            self.assertEqual(status, 400, f"refused: {body}")
        status, _ = self.post(t.id, {"on": True}, query="?x=1")
        self.assertEqual(status, 400, "a query is refused")
        status, _ = M.http(self.d.face_port, "GET", f"/threads/{t.id}/bypass", headers=self.owner())
        self.assertEqual(status, 405)
        self.assertEqual(self.fakes[CLAUDE].calls, [], "none of those switched anything")
        status, body = self.post(t.id, {"on": True})
        self.assertEqual((status, body), (200, {"bypass": True}))


def _py_files(*parts):
    out = []
    for part in parts:
        path = ROOT / part
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out.extend(sorted(path.rglob("*.py")))
        else:
            out.extend(sorted(path.parent.glob(path.name)))
    return out


class SourceGuard(unittest.TestCase):
    def test_only_the_owner_route_reaches_set_bypass(self):
        # Mutation: a second caller of `Daemon.set_bypass`. The daemon calls
        # the provider's own `set_bypass` through `switch(...)`, never by name.
        callers = sorted(str(p.relative_to(ROOT)) for p in _py_files("jarvis")
                         if re.search(r"\bdaemon\.set_bypass\(", p.read_text(encoding="utf-8")))
        self.assertEqual(callers, ["jarvis/v2/hud_api.py"])
        source = (ROOT / "jarvis/v2/hud_api.py").read_text(encoding="utf-8")
        at = source.find("daemon.set_bypass(")
        before = source[max(0, at - 1200):at]
        self.assertIn('"bypass"', before, "the call sits in the bypass route")
        self.assertIn("owner_only(handler, daemon)", before, "behind owner_only")
        # Not even by name, `getattr` included: only the route, the daemon and
        # the provider may mention it.
        named = sorted(str(p.relative_to(ROOT)) for p in _py_files("jarvis")
                       if "set_bypass" in p.read_text(encoding="utf-8"))
        self.assertEqual(named, ["jarvis/v2/daemon.py", "jarvis/v2/hud_api.py",
                                 "jarvis/v2/providers/claude.py"])

    def test_no_tool_bridge_mcp_or_discord_file_names_bypass(self):
        files = _py_files("jarvis/tools", "jarvis/v2/tools", "jarvis/v2/bridge.py",
                          "jarvis/v2/mcp*.py", "jarvis/v2/session_tools.py", "jarvis/v2/discord")
        self.assertTrue(files)
        hits = [str(p.relative_to(ROOT)) for p in files
                if re.search(r"set_bypass|/bypass\b", p.read_text(encoding="utf-8"))]
        self.assertEqual(hits, [])


class Switch(Base):
    def test_on_and_off_are_recorded_once(self):
        t = self.chat(CLAUDE)
        self.assertEqual(self.d.set_bypass(t.id, True), {"bypass": True})
        self.assertEqual(self.d.set_bypass(t.id, True), {"bypass": True})
        self.assertEqual(self.notices(t), [daemon_mod.BYPASS_ON], "one line, not one per click")
        changed = [e for e in self.bus("bypass_changed", t)]
        self.assertEqual([e["data"] for e in changed], [{"bypass": True}])
        self.assertEqual(self.d.set_bypass(t.id, False), {"bypass": False})
        self.assertEqual(self.notices(t), [daemon_mod.BYPASS_ON, daemon_mod.BYPASS_OFF])
        self.assertEqual([(r["tool"], r["decision"], r["thread_id"]) for r in self.decisions()],
                         [("bypass", "on", t.id), ("bypass", "off", t.id)])
        # The HUD transcript shows both as system lines.
        status, body = M.http(self.d.face_port, "GET", f"/threads/{t.id}/transcript",
                              headers=self.owner())
        self.assertEqual(status, 200, body)
        rows = body if isinstance(body, list) else body.get("messages", [])
        system = [m["text"] for m in rows if m.get("role") == "system"]
        self.assertEqual(system, [daemon_mod.BYPASS_ON, daemon_mod.BYPASS_OFF])

    def test_thread_json_reads_the_live_switch(self):
        t = self.chat(CLAUDE)
        self.assertIs(self.d.thread_json(self.stores.threads.get(t.id))["bypass"], False)
        self.d.set_bypass(t.id, True)
        self.assertIs(self.d.thread_json(self.stores.threads.get(t.id))["bypass"], True)
        self.assertNotIn("bypass", json.loads(self.stores.threads.path(t.id).read_text()),
                         "never stored on the thread record")
        f = self.chat(FAST)
        self.assertIs(self.d.thread_json(self.stores.threads.get(f.id))["bypass"], False)


class Refusals(Base):
    def test_a_provider_with_no_classifier_is_refused_and_off_still_works(self):
        for provider in (FAST, CODEX):
            t = self.chat(provider)
            status, body = self.post(t.id, {"on": True})
            self.assertEqual((status, (body or {}).get("error")),
                             (409, daemon_mod.BYPASS_UNSUPPORTED), provider)
            self.assertEqual(self.post(t.id, {"on": False}), (200, {"bypass": False}))
            self.assertEqual(self.notices(t), [])

    def test_the_providers_refusal_is_the_409(self):
        t = self.chat(CLAUDE)
        sentence = "This chat is on the ask profile, which has no classifier."
        self.fakes[CLAUDE].refusal[t.id] = sentence
        status, body = self.post(t.id, {"on": True})
        self.assertEqual((status, (body or {}).get("error")), (409, sentence))
        self.assertEqual(self.fakes[CLAUDE].calls, [], "the switch was never called")
        self.assertEqual(self.post(t.id, {"on": False}), (200, {"bypass": False}),
                         "turning off is never refused")

    def test_a_task_thread_is_refused(self):
        t = self.chat(CLAUDE)
        stored = self.stores.threads.get(t.id)
        stored.task_id = "a" * 8
        self.stores.threads.save(stored)
        with self.assertRaises(daemon_mod.APIError) as caught:
            self.d.set_bypass(t.id, True)
        self.assertEqual(str(caught.exception), daemon_mod.BYPASS_NOT_A_CHAT)
        self.assertEqual(self.fakes[CLAUDE].calls, [])

    def test_an_archived_chat_is_refused(self):
        t = self.chat(CLAUDE)
        self.d.close_thread(t.id)
        stored = self.stores.threads.get(t.id)
        stored.archived = "2026-10-21T00:00:00Z"
        self.stores.threads.save(stored)
        with self.assertRaises(daemon_mod.DaemonError):
            self.d.set_bypass(t.id, True)
        self.assertEqual(self.d.set_bypass(t.id, False), {"bypass": False},
                         "with no open session, off is a no-op that succeeds")
        self.assertEqual(self.fakes[CLAUDE].calls, [])

    def test_a_provider_missing_a_method_is_a_409_not_a_500(self):
        t = self.chat(CLAUDE)
        fake = self.fakes[CLAUDE]
        try:
            BypassFake.bypassed_saved = BypassFake.bypassed
            del BypassFake.bypassed
            status, body = self.post(t.id, {"on": True})
            self.assertEqual((status, (body or {}).get("error")),
                             (409, daemon_mod.BYPASS_UNSUPPORTED))
        finally:
            BypassFake.bypassed = BypassFake.bypassed_saved
        self.assertEqual(fake.calls, [])

    def test_a_session_closing_mid_switch_is_a_409(self):
        from jarvis.v2.provider import BriefRefused
        t = self.chat(CLAUDE)
        fake = self.fakes[CLAUDE]

        def closed(h, on):
            raise BriefRefused("This chat's Claude session is closed.")
        fake.set_bypass = closed
        status, body = self.post(t.id, {"on": True})
        self.assertEqual((status, (body or {}).get("error")),
                         (409, "This chat's Claude session is closed."))

    def test_a_non_bool_is_refused(self):
        t = self.chat(CLAUDE)
        with self.assertRaises(daemon_mod.APIError):
            self.d.set_bypass(t.id, "true")


if __name__ == "__main__":
    unittest.main(verbosity=2)

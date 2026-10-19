"""WP-2: Claude's authenticated relay, desk lifecycle and private terminal reads.

Fake providers, real ephemeral daemon listeners/relay and a fake-shell PTY;
all state is temporary. No installed CLI, account action or live daemon.
"""
from __future__ import annotations
import contextvars
import json
import logging
from pathlib import Path
import sys
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
import mcp_session_check as M
import terminal_read_check as R
from terminal_check import eventually
from jarvis import runtime, tools
from jarvis.v2 import bridge, session_tools, mcp
from jarvis.v2.desk import DeskState, desk_message
from jarvis.v2.mcp_catalog import MCP_TOOLS, MCP_READ_ONLY
from jarvis.v2.model import PermissionProfile, ProviderName, Role
from jarvis.v2.provider import Brief, Decision, SteerRefused, UserMessage
from jarvis.v2.tools import terminal_read as TR
from jarvis.v2.terminals import ToolRead
from jarvis.v2.providers import claude

HUD = UserMessage('owner words', desk=True, via='hud')
REMOTE = UserMessage('remote words', via='discord')
MARKER = 'WP2-PRIVATE-TERMINAL-MARKER'
CATALOG = dict(session_tools.SESSION_TOOLS)
CHAT_READS = ('chat_list', 'chat_summary', 'chat_search', 'chat_read')
# chat_send and chat_wait (chat-to-chat C4): pinned by tests/v2/chat_send_check.py.
CHAT_SENDS = ('chat_send', 'chat_wait')


def setUpModule():
    M.setUpModule()
    session_tools.SESSION_TOOLS.update(CATALOG)


def tearDownModule():
    M.tearDownModule()


class DeskChecks(unittest.TestCase):
    def test_exact_predicate_and_fastpath_alias(self):
        from jarvis.v2.providers.fastpath import _at_desk
        b = Brief(Role.CHAT, '/tmp')
        self.assertIs(_at_desk, desk_message)
        self.assertTrue(desk_message(b, HUD))
        self.assertTrue(desk_message(b, replace(HUD, via=None)))
        for change in ({'desk': False}, {'desk': 1}, {'origin': 'system'}, {'origin': 'owner-ran'},
                       {'via': 'discord'}, {'via': 'dm'}, {'via': 'peer'}, {'via': 'system'}):
            with self.subTest(change=change):
                self.assertFalse(desk_message(b, replace(HUD, **change)))
        for role in Role:
            if role != Role.CHAT:
                self.assertFalse(desk_message(replace(b, role=role), HUD))
        self.assertFalse(desk_message(replace(b, task_id='task'), HUD))

    def test_fresh_slots_end_revocation_and_one_turn_carry(self):
        s = DeskState(); b = Brief(Role.CHAT, '/tmp')
        old = s.begin(b, HUD, 'a'); self.assertTrue(old['present'])
        token = s.reserve(); s.settled(token); self.assertFalse(old['present'])
        second = s.begin(b, HUD, 'b'); self.assertFalse(second['present'])
        third = s.begin(b, HUD, 'c'); self.assertTrue(third['present'])
        self.assertIsNot(old, third)
        self.assertFalse(old['present']); self.assertFalse(second['present'])
        s.end(); self.assertFalse(third['present'])

    def test_two_reservations_resolve_independently_in_both_orders(self):
        b = Brief(Role.CHAT, '/tmp')
        for reverse in (False, True):
            s = DeskState(); s.begin(b, HUD, 'a')
            tokens = [s.reserve(), s.reserve()]
            if reverse: tokens.reverse()
            s.refused_before_write(tokens[0]); self.assertFalse(s.slot['present'])
            s.refused_before_write(tokens[1]); self.assertTrue(s.slot['present'])
            self.assertTrue(s.begin(b, HUD, 'b')['present'])

    def test_late_refusal_never_enables_successor_or_stopped_original(self):
        b = Brief(Role.CHAT, '/tmp')
        s = DeskState(); old = s.begin(b, HUD, 'a'); token = s.reserve()
        new = s.begin(b, HUD, 'b'); s.refused_before_write(token)
        self.assertFalse(old['present']); self.assertFalse(new['present'])
        self.assertTrue(s.begin(b, HUD, 'c')['present'])
        token = s.reserve(); s.end(); s.refused_before_write(token)
        self.assertFalse(s.slot['present'])
        self.assertTrue(s.begin(b, HUD, 'd')['present'])


class NativeSteerChecks(unittest.TestCase):
    def test_only_native_prewrite_paths_prove_refusal(self):
        provider = claude.ClaudeProvider()
        for native in (None, SimpleNamespace(closed=True),
                       SimpleNamespace(closed=False, mutex=threading.RLock(), accepting=False)):
            with self.assertRaises(SteerRefused) as caught:
                provider.steer(SimpleNamespace(native=native), REMOTE)
            self.assertTrue(caught.exception.before_write)
        native = SimpleNamespace(closed=False, mutex=threading.RLock(), accepting=True,
                                 steering={}, generation=1, loop=None,
                                 client=SimpleNamespace(query=lambda stream: None))
        with patch.object(claude, '_await', side_effect=TimeoutError('synthetic write timeout')):
            with self.assertRaises(SteerRefused) as caught:
                provider.steer(SimpleNamespace(native=native), REMOTE)
        self.assertFalse(caught.exception.before_write)
        self.assertEqual(native.steering, {})


class CatalogChecks(unittest.TestCase):
    def test_exact_shared_catalog_flags_and_availability(self):
        self.assertIs(mcp.MCP_TOOLS, MCP_TOOLS)
        chat_git = set(session_tools.CHAT_GIT_TOOLS)
        # The chat reads (chat-to-chat C1) are pinned by ChatReadCatalogChecks below,
        # chat_send and chat_wait (C4) by tests/v2/chat_send_check.py.
        self.assertEqual(set(CATALOG) - set(CHAT_READS) - set(CHAT_SENDS),
                         MCP_TOOLS | {'terminal_read', 'hud_browser_view', 'hud_browser_act'} | chat_git)
        self.assertEqual(len(MCP_TOOLS), 23)
        self.assertEqual(MCP_TOOLS - MCP_READ_ONLY, {
            'memory_write', 'memory_delete', 'gmail_send', 'discord_dm_owner',
            'spotify_play', 'spotify_pause', 'schedule_create', 'schedule_delete'})
        for name, entry in CATALOG.items():
            if name in CHAT_READS or name in CHAT_SENDS:
                continue
            if name in chat_git:
                # Codex chats' git tools (CG-0): Codex only, foreground only.
                self.assertEqual((entry.providers, entry.read_only, entry.foreground_only,
                                  entry.desk_only, entry.owner_gated),
                                 (frozenset({'codex'}), False, True, False, False))
                continue
            self.assertEqual(entry.providers, frozenset({'claude', 'codex'}))
            self.assertEqual(entry.read_only, name in MCP_READ_ONLY or name in ('terminal_read', 'hud_browser_view'))
            self.assertEqual(entry.foreground_only, name in ('terminal_read', 'hud_browser_view', 'hud_browser_act'))
            self.assertEqual(entry.desk_only, name in ('terminal_read', 'hud_browser_view', 'hud_browser_act'))
            self.assertEqual(entry.owner_gated, name == 'hud_browser_act')
        # Both chat providers share availability; terminal stays out of external MCP.
        with patch.dict(session_tools.SESSION_TOOLS, CATALOG, clear=True):
            self.assertEqual([t for t in session_tools.for_provider('codex') if t.name not in chat_git],
                             session_tools.for_provider('claude'))
            self.assertTrue(chat_git <= {t.name for t in session_tools.for_provider('codex')})
            self.assertNotIn('terminal_read', MCP_TOOLS)
            offered = session_tools.for_provider('claude')
            for entry in offered:
                self.assertIn(entry.name, tools.REGISTRY)
            with patch.object(tools, 'group_of', return_value=SimpleNamespace(is_available=lambda: False)):
                self.assertEqual(session_tools.for_provider('claude'), [])
            self.assertNotIn('terminal_read', tools.default_names())
            self.assertIn('terminal_read', tools.PRIVATE_RESULT)

    def test_external_mcp_approver_still_denies(self):
        self.assertFalse(mcp._approver(tools.REGISTRY[M.SEND], {'to': 'fake'}))



class ChatReadCatalogChecks(unittest.TestCase):
    """Chat-to-chat C1 (§4.2): the four reads are offered to Claude and Codex
    chats as read-only, not foreground- or desk-only, and stay out of the
    external MCP. Mutation: mark one not read-only (no readOnlyHint, no
    explicit allow), or add it to MCP_TOOLS."""

    def test_the_reads_are_read_only_session_tools_on_both_providers(self):
        for name in CHAT_READS:
            with self.subTest(tool=name):
                self.assertIn(name, CATALOG)
                entry = CATALOG[name]
                self.assertEqual((entry.providers, entry.read_only, entry.foreground_only,
                                  entry.desk_only, entry.owner_gated),
                                 (frozenset({'claude', 'codex'}), True, False, False, False))
                self.assertNotIn(name, MCP_TOOLS, 'the external jarvis mcp never lists them')
                self.assertIn(name, tools.PRIVATE_RESULT)
                self.assertNotIn(name, tools.default_names())
        with patch.dict(session_tools.SESSION_TOOLS, CATALOG, clear=True):
            for provider in ('claude', 'codex'):
                offered = {t.name for t in session_tools.for_provider(provider)}
                self.assertTrue(set(CHAT_READS) <= offered, (provider, sorted(offered)))
            self.assertTrue({session_tools.CLAUDE_PREFIX + n for n in CHAT_READS}
                            <= session_tools.read_only_claude_names())

    def test_the_listing_marks_them_read_only_with_the_registry_schema(self):
        with patch.dict(session_tools.SESSION_TOOLS, CATALOG, clear=True):
            for provider in ('claude', 'codex'):
                listed = {t['name']: t for t in bridge.listing(provider)}
                for name in CHAT_READS:
                    with self.subTest(provider=provider, tool=name):
                        self.assertIn(name, listed)
                        spec = tools.REGISTRY[name].spec()['function']
                        self.assertEqual(listed[name]['inputSchema'], spec['parameters'])
                        self.assertEqual(listed[name]['description'], spec['description'])
                        self.assertEqual(listed[name]['annotations'],
                                         {'readOnlyHint': True, 'destructiveHint': False,
                                          'openWorldHint': False})


class LifecycleChecks(M.DaemonHarness):
    def held(self, message=HUD, thread=None):
        thread = thread or self.chat()
        entered, release = threading.Event(), threading.Event()
        def action(h, msg):
            entered.set(); release.wait(8)
        self.claude.script[thread.id] = action
        self.d.send(thread.id, message)
        self.assertTrue(entered.wait(3))
        self.addCleanup(release.set)
        return thread, self.d._sessions[thread.id], release

    def terminal(self, thread):
        return self.call(M.token_of(self.run_brief(thread)), 'terminal_read', {'terminal': 'test'})[1]

    def finish(self, thread, release):
        release.set(); self.wait_idle(thread.id)

    def terminal_reader(self):
        return patch.object(TR, 'read_for_tool', return_value=ToolRead('test', 'safe', text=MARKER, lines=1))

    def test_only_owner_hud_http_surface_grants_claude_desk(self):
        thread = self.chat(); out = []
        def action(h, msg): out.append(self.terminal(thread)['text'])
        self.claude.script[thread.id] = action
        with self.terminal_reader():
            for port, origin, expected in ((self.d.face_port, self.d.face_port, True),
                                           (self.d.port, None, False),
                                           (self.d.port, self.d.port, False),
                                           (self.d.face_port, None, False)):
                headers = {'Origin': f'http://127.0.0.1:{origin}'} if origin else {}
                status, _ = M.http(port, 'POST', f'/threads/{thread.id}/send',
                                   body={'text': 'look'}, headers=headers)
                self.assertEqual(status, 202)
                self.wait_idle(thread.id)
                self.assertEqual(MARKER in out.pop(), expected)
            status, _ = M.http(self.d.port, 'POST', f'/threads/{thread.id}/send',
                               body={'text': 'forged', 'desk': True})
            if status == 202:
                self.wait_idle(thread.id)
                self.assertEqual(out.pop(), TR.NOT_AT_DESK)
            else:
                self.assertEqual(status, 400)

    def test_active_idle_offdesk_and_context_restore(self):
        t = self.chat()
        with self.terminal_reader() as reader:
            self.assertEqual(self.terminal(t)['text'], TR.NOT_AT_DESK)
            self.assertEqual(reader.call_count, 0)
            for msg, expected in ((HUD, True), (REMOTE, False), (UserMessage('api'), False)):
                t, s, release = self.held(msg, t)
                self.assertEqual(MARKER in self.terminal(t)['text'], expected)
                token = M.token_of(self.run_brief(t)); caller = self.d.callers.resolve(token)
                before = (runtime.caller(), runtime.at_desk(), runtime.depth(), runtime.parent_tools())
                bridge.call(self.d, s, caller, 'get_datetime', {})
                self.assertEqual((runtime.caller(), runtime.at_desk(), runtime.depth(), runtime.parent_tools()), before)
                old = s.desk.slot; self.finish(t, release)
                self.assertFalse(old['present'])
                self.assertEqual(self.terminal(t)['text'], TR.NOT_AT_DESK)

    def test_queued_remote_message_does_not_revoke_current_desk(self):
        t, s, release = self.held()
        s.questions.add('owner-question')
        self.assertEqual(self.d.deliver(t.id, REMOTE)['status'], 'queued')
        self.assertTrue(s.desk.slot['present']); self.assertFalse(s.desk.carry)
        with self.terminal_reader(): self.assertIn(MARKER, self.terminal(t)['text'])
        self.d.interrupt(t.id)

    def test_provider_without_steer_queues_without_revocation(self):
        t, s, release = self.held()
        self.assertEqual(self.d.deliver(t.id, REMOTE)['status'], 'queued')
        self.assertTrue(s.desk.slot['present']); self.assertFalse(s.desk.carry)
        self.d.interrupt(t.id)

    def test_accepted_nonhud_steer_revokes_current_and_next_only(self):
        self.claude.steer = lambda h, m: None
        t, s, release = self.held()
        self.assertEqual(self.d.deliver(t.id, REMOTE)['status'], 'steered')
        with self.terminal_reader() as reader:
            self.assertEqual(self.terminal(t)['text'], TR.NOT_AT_DESK); reader.assert_not_called()
        self.finish(t, release)
        t, s, release = self.held(HUD, t); self.assertFalse(s.desk.slot['present'])
        self.finish(t, release)
        t, s, release = self.held(HUD, t); self.assertTrue(s.desk.slot['present'])

    def test_hud_steer_cannot_grant_offdesk(self):
        self.claude.steer = lambda h, m: None
        t, s, release = self.held(REMOTE)
        self.d.deliver(t.id, HUD)
        self.assertFalse(s.desk.slot['present']); self.assertFalse(s.desk.carry)

    def test_only_proven_prewrite_refusal_restores_current(self):
        for failure, restored in ((SteerRefused('prewrite', before_write=True), True),
                                   (SteerRefused('ambiguous'), False), (TimeoutError('query'), False)):
            with self.subTest(kind=type(failure).__name__, restored=restored):
                t, s, release = self.held()
                def steer(h, m): raise failure
                self.claude.steer = steer
                self.assertEqual(self.d.deliver(t.id, REMOTE)['status'], 'queued')
                self.assertEqual(s.desk.slot['present'], restored)
                self.assertEqual(bool(s.desk.carry), not restored)
                self.d.interrupt(t.id); self.finish(t, release)

    def test_pending_delivery_carry_and_late_reply_never_enable_new_turn(self):
        for refuse in (False, True):
            with self.subTest(refuse=refuse):
                entered, answer = threading.Event(), threading.Event()
                def steer(h, m):
                    entered.set(); answer.wait(8)
                    if refuse: raise SteerRefused('prewrite', before_write=True)
                self.claude.steer = steer
                t, s, release = self.held(); old = s.desk.slot
                replies = []
                sender = threading.Thread(target=lambda: replies.append(self.d.deliver(t.id, REMOTE)), daemon=True)
                self.addCleanup(answer.set); sender.start(); self.assertTrue(entered.wait(3))
                self.assertFalse(old['present'])
                self.finish(t, release)
                t, s, release = self.held(HUD, t); new = s.desk.slot
                with self.terminal_reader() as reader:
                    self.assertEqual(self.terminal(t)['text'], TR.NOT_AT_DESK); reader.assert_not_called()
                answer.set(); sender.join(3); self.assertFalse(sender.is_alive())
                self.assertFalse(new['present']); self.assertFalse(old['present'])
                self.d.interrupt(t.id); self.finish(t, release)

    def test_pending_delivery_keeps_two_successors_offdesk_until_settled(self):
        entered, answer = threading.Event(), threading.Event()
        def steer(h, m):
            entered.set(); answer.wait(8)
        self.claude.steer = steer
        t, s, release = self.held()
        sender = threading.Thread(target=self.d.deliver, args=(t.id, REMOTE), daemon=True)
        self.addCleanup(answer.set); sender.start(); self.assertTrue(entered.wait(3))
        self.finish(t, release)
        for successor in range(2):
            t, s, release = self.held(HUD, t)
            with self.terminal_reader() as reader:
                self.assertEqual(self.terminal(t)['text'], TR.NOT_AT_DESK,
                                 f'pending delivery reached successor {successor+1}')
                reader.assert_not_called()
            self.finish(t, release)
        answer.set(); sender.join(3); self.assertFalse(sender.is_alive())
        t, s, release = self.held(HUD, t)
        self.assertTrue(s.desk.slot['present'], 'settled and consumed carry does not revoke forever')

    def test_two_pending_refusals_resolve_without_undoing_each_other(self):
        for reverse in (False, True):
            t, s, release = self.held()
            entered = [threading.Event(), threading.Event()]; answers = [threading.Event(), threading.Event()]
            def steer(h, m):
                i = int(m.text); entered[i].set(); answers[i].wait(8)
                raise SteerRefused('prewrite', before_write=True)
            self.claude.steer = steer
            senders = []
            for i in range(2):
                worker = threading.Thread(target=self.d.deliver, args=(t.id, replace(REMOTE, text=str(i))), daemon=True)
                senders.append(worker); self.addCleanup(answers[i].set)
                worker.start(); self.assertTrue(entered[i].wait(3))
            order = (1, 0) if reverse else (0, 1)
            answers[order[0]].set(); senders[order[0]].join(3)
            self.assertFalse(s.desk.slot['present'])
            answers[order[1]].set(); senders[order[1]].join(3)
            self.assertTrue(s.desk.slot['present']); self.assertFalse(s.desk.carry)
            self.d.interrupt(t.id); self.finish(t, release)

    def test_postread_recheck_withholds_marker_after_steer_or_stop(self):
        for stop in (False, True):
            t, s, release = self.held()
            self.claude.steer = lambda h, m: None
            entered, answer = threading.Event(), threading.Event(); out = []
            def reader(*args):
                entered.set(); answer.wait(5)
                return ToolRead('test', 'safe', text=MARKER, lines=1)
            with patch.object(TR, 'read_for_tool', reader):
                worker = threading.Thread(target=lambda: out.append(self.terminal(t)), daemon=True)
                worker.start(); self.assertTrue(entered.wait(3))
                if stop: self.d.interrupt(t.id)
                else: self.d.deliver(t.id, REMOTE)
                answer.set(); worker.join(3); self.assertFalse(worker.is_alive())
            self.assertEqual(out[0]['text'], TR.NOT_AT_DESK)
            self.assertNotIn(MARKER, json.dumps(self.drain()))
            self.finish(t, release)

    def test_stop_close_and_replacement_revoke_inflight_read(self):
        for action in ('stop', 'close', 'replace', 'turn'):
            with self.subTest(action=action):
                t, s, release = self.held(); entered, answer = threading.Event(), threading.Event(); out = []
                def reader(*args):
                    entered.set(); answer.wait(5)
                    return ToolRead('test', 'safe', text=MARKER, lines=1)
                with patch.object(TR, 'read_for_tool', reader):
                    worker = threading.Thread(target=lambda: out.append(self.terminal(t)), daemon=True)
                    worker.start(); self.assertTrue(entered.wait(3))
                    if action == 'stop': self.d.interrupt(t.id)
                    elif action == 'close':
                        with self.d._lock: s.closing = True; s.desk.end()
                    elif action == 'replace':
                        with self.d._lock: self.d._sessions.pop(t.id)
                    else:
                        with self.d._lock: s.turn_id = 'new-turn-identity'
                    answer.set(); worker.join(3)
                    if action == 'replace':
                        with self.d._lock: self.d._sessions[t.id] = s
                self.assertEqual(out[0]['text'], TR.NOT_AT_DESK)
                self.finish(t, release)

    def test_dangerous_approval_rechecks_stop_session_worker_and_turn(self):
        for action in ('stop', 'replace', 'worker', 'turn', 'closing', 'retired', 'lost'):
            with self.subTest(action=action):
                t, s, release = self.held(); out = []
                token = M.token_of(self.run_brief(t))
                before = len(M.CALLS)
                worker = threading.Thread(target=lambda: out.append(self.call(token, M.SEND, {'to': 'fake'})), daemon=True)
                worker.start()
                request = eventually(lambda: next((r for r in self.d.approvals.pending() if r.thread_id == t.id), None))
                saved_worker = s.worker
                with self.d._lock:
                    if action == 'stop': s.cancelled.set(); s.desk.end()
                    elif action == 'replace': self.d._sessions.pop(t.id)
                    elif action == 'worker': s.worker = object()
                    elif action == 'turn': s.turn_id = 'successor'
                    else: setattr(s, action, True)
                self.d.resolve_approval(request.req_id, Decision.ALLOW)
                worker.join(3)
                with self.d._lock:
                    self.d._sessions[t.id] = s; s.worker = saved_worker
                self.finish(t, release)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(M.CALLS), before)
                self.assertTrue(out[0][1]['isError'])

    def test_wrong_session_binding_is_rejected_before_dispatch(self):
        t, s, release = self.held(); other = self.chat()
        caller = self.d.callers.resolve(M.token_of(self.run_brief(other)))
        with patch.object(tools, 'dispatch', wraps=tools.dispatch) as dispatch:
            with self.assertRaises(M.daemon_mod.APIError) as got:
                bridge.call(self.d, s, caller, M.READ, {})
            self.assertEqual(got.exception.status, 403)
            dispatch.assert_not_called()

    def hook(self, thread, name, *, agent_id=None):
        import claude_provider_check as cl
        brief = self.run_brief(thread)
        with cl.fake():
            provider = claude.ClaudeProvider()
            handle = provider.start(thread, brief, self.claude.opened[-1].permit)
            try:
                data = {'tool_name': 'mcp__jarvis__'+name, 'tool_input': {'to': 'fake'},
                        'tool_use_id': 'hook', 'agent_id': agent_id}
                return claude._await(handle.native.loop,
                    provider._pre_tool_hook(handle.native)(data, 'hook', {}), 5)
            finally:
                provider.close(handle)

    def test_hook_delegation_then_bridge_is_exactly_one_dangerous_card(self):
        for profile in ('auto', 'ask'):
            for decision in (Decision.ALLOW, Decision.DENY):
                with self.subTest(profile=profile, decision=decision):
                    thread = self.d.open_thread(self.project.id, Role.CHAT, ProviderName.CLAUDE,
                        {'profile': profile, 'always_ask': ['mcp__jarvis__'+M.SEND]})
                    thread, session, release = self.held(HUD, thread)
                    self.drain(); before = len(M.CALLS)
                    hook = self.hook(thread, M.SEND)
                    self.assertEqual(hook['hookSpecificOutput']['permissionDecision'], 'allow')
                    self.assertEqual(self.d.approvals.pending(), [])
                    self.assertEqual(len(M.CALLS), before, 'delegation executes nothing')
                    seen = []; resolver = self.resolver(decision, seen, thread.id)
                    result = self.call(M.token_of(self.run_brief(thread)), M.SEND, {'to': 'fake'})
                    resolver.join(3); self.assertFalse(resolver.is_alive())
                    self.assertEqual(len(seen), 1); self.assertFalse(seen[0].allowlistable)
                    self.assertEqual(len(M.CALLS)-before, int(decision is Decision.ALLOW))
                    self.assertEqual(result[1]['isError'], decision is Decision.DENY)
                    self.assertEqual(sum(e['kind']=='approval_requested' for e in self.drain()), 1)
                    self.finish(thread, release)

    def test_strict_claude_chat_is_refused_before_a_token_or_provider(self):
        before = len(self.claude.opened)
        with self.assertRaises(M.daemon_mod.APIError) as got:
            self.d.open_thread(self.project.id, Role.CHAT, ProviderName.CLAUDE, {'profile': 'strict'})
        self.assertEqual(got.exception.status, 400)
        self.assertEqual(len(self.claude.opened), before)
        self.assertEqual(self.d.callers.live(), 0)

    def test_terminal_foreground_gate_but_d3_subagent_delegates(self):
        thread, session, release = self.held()
        self.assertEqual(self.hook(thread, 'terminal_read', agent_id='child')
                         ['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.hook(thread, 'get_datetime', agent_id='child')
                         ['hookSpecificOutput']['permissionDecision'], 'allow')
        self.assertEqual(self.hook(thread, 'memory_write', agent_id='child')
                         ['hookSpecificOutput']['permissionDecision'], 'allow')
        self.assertFalse(self.d.approvals.pending())

    def test_claude_hook_allows_the_chat_reads_explicitly_on_any_turn(self):
        """C1: a read-only session tool, so Claude's PreToolUse hook returns
        the explicit delegation allow, for the conversation and a sub-agent
        alike (not foreground-only), on and off the desk; nobody is asked."""
        for message in (HUD, REMOTE):
            thread, session, release = self.held(message)
            for name in CHAT_READS:
                for agent_id in (None, 'child'):
                    with self.subTest(via=message.via, tool=name, agent=agent_id):
                        out = self.hook(thread, name, agent_id=agent_id).get('hookSpecificOutput', {})
                        self.assertEqual(out.get('permissionDecision'), 'allow')
                        self.assertEqual(out.get('permissionDecisionReason'), 'a read-only Jarvis tool')
            self.assertFalse(self.d.approvals.pending())
            self.finish(thread, release)

    def test_offdesk_d3_read_and_nondangerous_write_have_one_dispatch(self):
        t, s, release = self.held(REMOTE)
        token = M.token_of(self.run_brief(t))
        for name, args in (('get_datetime', {}), ('memory_write', {'name': 'test', 'content': 'x'})):
            entry = tools.REGISTRY[name]
            with patch.object(entry, 'func', return_value='safe-probe') as func, \
                 patch.object(tools, 'dispatch', wraps=tools.dispatch) as dispatch:
                got = self.call(token, name, args)
                self.assertEqual(got[1]['text'], 'safe-probe'); func.assert_called_once(); dispatch.assert_called_once()
        self.assertFalse(self.d.approvals.pending())


class DelegationChecks(M.GateChecks):
    def test_the_layer_is_for_an_owners_chat_and_read_only_tools_only(self):
        with patch.dict(session_tools.SESSION_TOOLS, {M.READ: session_tools.SESSION_TOOLS[M.READ],
                                                     M.SEND: session_tools.SESSION_TOOLS[M.SEND]}, clear=True):
            super().test_the_layer_is_for_an_owners_chat_and_read_only_tools_only()

    def test_named_read_and_nondangerous_write_rules_apply_in_either_spelling(self):
        for name in ('get_datetime', 'memory_write'):
            for spelling in (name, 'mcp__jarvis__'+name):
                asker = M.Asker(Decision.DENY)
                brief = self.brief(PermissionProfile.AUTO, always_ask=[spelling])
                permit = M.build_permit(M.PermitContext('t', brief, provider='claude'), asker)
                self.assertEqual(permit('mcp__jarvis__'+name, {}, brief), Decision.DENY)
                self.assertEqual(len(asker.seen), 1)

    def test_strict_defense_refuses_delegation_and_dangerous_dispatch(self):
        brief = self.brief(PermissionProfile.STRICT)
        asker = M.Asker(Decision.ALLOW)
        permit = M.build_permit(M.PermitContext('t', brief, provider='claude'), asker)
        for name in (M.SEND, 'get_datetime', 'memory_write', 'terminal_read'):
            self.assertEqual(permit('mcp__jarvis__'+name, {}, brief), Decision.DENY)
        self.assertEqual(permit(M.SEND, {}, brief, dangerous_tool=True), Decision.DENY)
        self.assertEqual(asker.seen, [])

    def test_exact_server_offering_chat_and_provider_restrictions(self):
        for name, opts, provider in (
            ('mcp__other__get_datetime', {}, 'claude'),
            ('mcp__jarvis__get_datetime_extra', {}, 'claude'),
            ('mcp__jarvis__get_datetime', {'mcp_servers': {}}, 'claude'),
            ('mcp__jarvis__get_datetime', {'task_id': 'x'}, 'claude'),
            ('mcp__jarvis__get_datetime', {'role': Role.IMPLEMENTER}, 'claude'),
            ('mcp__jarvis__get_datetime', {}, 'codex'),
        ):
            with self.subTest(name=name, opts=opts, provider=provider):
                asker = M.Asker(Decision.DENY)
                brief = self.brief(PermissionProfile.ASK, **opts)
                permit = M.build_permit(M.PermitContext('t', brief, provider=provider), asker)
                self.assertEqual(permit(name, {}, brief), Decision.DENY)
                self.assertEqual(len(asker.seen), 1)

    def test_layer_one_beats_delegation_and_dangerous_approval(self):
        # Grant checking runs before either delegation path; nobody may approve
        # a request also reaching a protected credential directory.
        brief = self.brief(PermissionProfile.AUTO)
        asker = M.Asker(Decision.ALLOW)
        permit = M.build_permit(M.PermitContext('t', brief, provider='claude'), asker)
        target = str(self.root / 'creds' / '.ssh')
        for name, dangerous in (('mcp__jarvis__'+M.SEND, False), (M.SEND, True)):
            self.assertEqual(permit(name, {}, brief, grants=[('read', target)],
                                    dangerous_tool=dangerous), Decision.DENY)
        self.assertEqual(asker.seen, [])


class PrivacyChecks(unittest.TestCase):
    def test_exact_names_get_private_summaries_without_changing_ordinary_tools(self):
        provider = claude.ClaudeProvider()
        for name in ('terminal_read', 'mcp__jarvis__terminal_read'):
            for text, expected in ((MARKER, 'read'), ('Refused: '+MARKER, 'refused'),
                                   ('Error: '+MARKER, 'error'), ('its last 17 line(s) '+MARKER, 'read 17 lines')):
                block = claude.ToolResultBlock(tool_use_id='c', content=text, is_error=False)
                session = SimpleNamespace(tools_seen={'c': (name, {})}, denied_tools=set(),
                                          denial_without_id=False, thread_id='t')
                events = provider._tool_result(session, block)
                self.assertEqual(events[-1].data['summary'], expected)
                self.assertNotIn(MARKER, json.dumps([e.data for e in events]))
        for name in ('mcp__other__terminal_read', 'terminal_read_extra', 'Bash'):
            self.assertEqual(provider._tool_summary(name, MARKER*20), (MARKER*20)[:200])

    def test_chat_reads_get_the_fixed_private_summary_on_claude(self):
        """C1: `claude.py` reads tools.PRIVATE_RESULT, not a hard-coded list.
        Mutation: leave the list as ('terminal_read', 'hud_browser_view',
        'hud_browser_act')."""
        provider = claude.ClaudeProvider()
        fenced = '[untrusted chat text n1 from HUD chat aaaa0001 "x" (Jarvis, project p)]\n' + MARKER
        for canonical in CHAT_READS:
            for name in (canonical, 'mcp__jarvis__' + canonical):
                for text, expected in ((fenced, 'read'), ('Refused: ' + MARKER, 'refused'),
                                       ('Error: ' + MARKER, 'error')):
                    with self.subTest(name=name, expected=expected):
                        block = claude.ToolResultBlock(tool_use_id='c', content=text, is_error=False)
                        session = SimpleNamespace(tools_seen={'c': (name, {'chat': 'aaaa0001'})},
                                                  denied_tools=set(), denial_without_id=False,
                                                  thread_id='t')
                        events = provider._tool_result(session, block)
                        self.assertEqual(events[-1].data['summary'], expected)
                        self.assertNotIn(MARKER, json.dumps([e.data for e in events]))
        # Another server's tool of the same name keeps the ordinary summary.
        self.assertEqual(provider._tool_summary('mcp__other__chat_read', MARKER * 20), (MARKER * 20)[:200])

    def test_classifier_terminal_reason_never_contains_output(self):
        text = claude.CLASSIFIER_DENIAL_PREFIX + ' reason: ' + MARKER
        session = SimpleNamespace(tools_seen={'c': ('mcp__jarvis__terminal_read', {})},
                                  denied_tools=set(), denial_without_id=False, thread_id='t')
        events = claude.ClaudeProvider()._tool_result(session,
                    claude.ToolResultBlock(tool_use_id='c', content=text, is_error=True))
        self.assertNotIn(MARKER, json.dumps([e.data for e in events]))
        self.assertEqual(events[-1].data['summary'], 'refused')
        self.assertEqual(events[0].data['reason'], 'refused')
        session.tools_seen = {'c': ('terminal_read', {})}
        events = claude.ClaudeProvider()._tool_result(session,
                    claude.ToolResultBlock(tool_use_id='c', content=MARKER, is_error=True))
        self.assertEqual(events[-1].data['summary'], 'error')
        events = claude.ClaudeProvider()._tool_result(session,
                    claude.ToolResultBlock(tool_use_id='c', content='Refused: '+MARKER, is_error=True))
        self.assertEqual(events[-1].data['summary'], 'refused')


class RealTerminalChecks(R.ReadBase):
    def test_real_relay_reads_guarded_pty_and_events_remain_private(self):
        fake = M.FakeCli(ProviderName.CLAUDE)
        self.daemon.providers[ProviderName.CLAUDE] = fake
        thread = self.daemon.open_thread(self.project.id, Role.CHAT, ProviderName.CLAUDE, {})
        tid, client = self.session()
        self.run_line(client, 'echo '+MARKER, '\n'+MARKER); self.settle(tid); self.reads()
        out = []
        def action(h, msg):
            got, err = M.run_relay(M.entry_of(fake.opened[-1].brief), [('terminal_read', {'terminal': tid})], cwd=self.root)
            out.append(got[10]['result']['content'][0]['text']); self.assertNotIn(MARKER, err)
        fake.script[thread.id] = action
        def send():
            self.daemon.send(thread.id, HUD)
            eventually(lambda: self.daemon._sessions[thread.id].worker is None)
            self.assertTrue(out)
            return out.pop()
        self.assertIn(MARKER, send())
        events = self.reads(); self.assertEqual(len(events), 1)
        self.assertEqual(set(events[0]['data']), {'terminal_id', 'lines', 'at', 'refused'})
        self.assertFalse(events[0]['data']['refused']); self.assertNotIn(MARKER, json.dumps(events))
        self.owner('PATCH', f'/terminals/{tid}', {'readable': False})
        self.assertTrue(send().startswith('Refused: the owner')); self.assertTrue(self.reads()[0]['data']['refused'])
        self.owner('PATCH', f'/terminals/{tid}', {'readable': True})
        # A credential-printing command refuses even with harmless prior text.
        self.run_line(client, 'cat .env', 'command not found'); self.settle(tid)
        self.assertTrue(send().startswith('Refused:')); self.assertTrue(self.reads()[0]['data']['refused'])
        self.assertNotIn(MARKER, json.dumps(self.stores.threads.read_log(thread.id)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
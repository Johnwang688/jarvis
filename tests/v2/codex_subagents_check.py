"""SV-2: a Codex chat's child agents come out of the provider as their own,
and the daemon keeps them apart, as SV-1 did for Claude's.

Free and offline. Every scenario is a recording SV-0 made of Codex 0.161.0
(`fixtures/subagents/codex/*.jsonl`), played back through the real
`CodexProvider` over real pipes (`codex_provider_check`'s replaying peer:
only the executable is replaced). Where a child outlives the parent's turn
(`no_wait`, `parent_interrupt`, `steer_parent`), what Codex sent after the
parent's `turn/completed` is read by a second send, as it would be: nobody
reads between turns until SV-6. What is pinned, in the order it would hurt:

  - **a child's words are never the orchestrator's**: everything a known
    child's thread sends comes out as SUBAGENT/SUBAGENT_EVENT, and the
    parent's TEXT/TEXT_DELTA/THINKING/TOOL_*/USAGE carry nothing of it; its
    tokens never reach the session's accounting; its `turn/completed` never
    ends the parent's turn; a thread nobody announced is still dropped;
  - **through the daemon**: no child's word reaches `log.jsonl`, the
    transcript route, the Discord mirror's reply, titles or the bus (ids,
    status, times and counts only), and the ledger counts the parent alone;
  - **the lifecycle**: `cx:<threadId>`; started before any of its events;
    the status is the child's own turn's (done, failed with Codex's reason,
    stopped — by the orchestrator's `interrupt_agent` or a `turn/interrupt`);
    nested children carry depth and parent; children outlive the parent's
    turn and end with the session; a finished child given another turn is
    live again;
  - **privacy**: a tool is a name and what the daemon labels it from (a
    command, a patch's paths — never its diff, never `hud_browser_act`'s
    arguments) and an outcome; text is scrubbed; deltas, reasoning, plans
    and the child's brief are not passed on; a reviewer's decline is a name;
  - **bounds**: a child's open calls (`CHILD_CALL_LIMIT`), its record made
    and forgotten with SV-2a's `children` entry (`CHILD_LIMIT`).

Run: PYTHONPATH=. .venv/bin/python tests/v2/codex_subagents_check.py
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import copy
import functools
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import codex_provider_check as P  # noqa: E402
from editor_files_check import Daemonised  # noqa: E402
from jarvis import config  # noqa: E402
from jarvis.v2 import titles  # noqa: E402
from jarvis.v2.discord.mirror import ChatMirror  # noqa: E402
from jarvis.v2.model import ProviderName as PN, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, Decision, Event, EventKind as K, SessionHandle, Usage, UserMessage  # noqa: E402
from jarvis.v2.providers import codex, codex_config  # noqa: E402

try:  # absent before SV-1
    from jarvis.v2 import subagents as S  # noqa: E402
except ImportError:  # pragma: no cover
    S = None

FIX = P.FIXTURES
PARENT_KINDS = (K.TEXT, K.TEXT_DELTA, K.THINKING, K.TOOL_STARTED, K.TOOL_FINISHED,
                K.REVIEWER_DECLINED, K.PLAN_UPDATED, K.USAGE)
BUS_KEYS = {"sid", "op", "status", "depth", "background", "started_at", "entries",
            "task_id", "parent_sid", "ended_at", "declined"}
FAKE_SECRET = "sv2-FAKE-credential-value-0123456789"
UNANNOUNCED = "01a140cb-0000-7000-8000-unannounced0"

# recording: how many sends it takes (a second reads children that outlived
# the parent's turn)
SCENARIOS = {
    "one": 1, "parallel": 1, "nested": 1, "fail": 1, "child_interrupt": 1,
    "close_agent": 1, "approval": 1, "approval_accept": 1, "patch_approval_child": 1,
    "one_multi_agent_v2": 1, "no_wait": 2, "parent_interrupt": 2, "steer_parent": 2,
}


def rows(name):
    return [json.loads(line) for line in (FIX / f"{name}.jsonl").read_text().splitlines()]


def parent_thread(name):
    start = next(r["data"] for r in rows(name) if r["src"] == "rpc_out"
                 and r["data"].get("method") == "turn/start")
    return start["params"]["threadId"]


def children(name):
    """{child threadId: agentPath}, in the order they were announced."""
    out = {}
    for r in rows(name):
        d = r["data"]
        item = (d.get("params") or {}).get("item") if r["src"] == "rpc_in" else None
        if isinstance(item, dict) and item.get("type") == "subAgentActivity" and item.get("kind") == "started":
            out.setdefault(item["agentThreadId"], item["agentPath"])
    return out


def child_words(name):
    """What a child wrote or ran in the recording (its texts, commands and
    Codex's reason it failed) and the names the model gave its children."""
    parent = parent_thread(name)
    words, names = set(), set(children(name).values())
    for r in rows(name):
        d = r["data"]
        p = d.get("params") if r["src"] == "rpc_in" else None
        if not isinstance(p, dict) or p.get("threadId") in (None, parent):
            continue
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        if item.get("type") == "agentMessage" and item.get("text"):
            words.add(item["text"])
        if item.get("type") == "commandExecution" and item.get("command"):
            words.add(item["command"].split("'")[1] if "'" in item["command"] else item["command"])
        turn = p.get("turn") if isinstance(p.get("turn"), dict) else {}
        if isinstance(turn.get("error"), dict) and "synthetic" in str(turn["error"].get("message")):
            words.add("SV0 synthetic child failure")
    return words, {n.rsplit("/", 1)[-1] for n in names}


def turns_of(name):
    """The replaying peer's script: turn 1 is the recorded parent turn; a
    second send reads what Codex sent after it (children that outlived the
    turn), then a short parent turn of its own."""
    first = P.fixture_turn(name)
    n = SCENARIOS.get(name, 1)
    if n == 1:
        return [first]
    recorded = rows(name)
    start = next(r["data"] for r in recorded if r["src"] == "rpc_out" and r["data"].get("method") == "turn/start")
    thread = start["params"]["threadId"]
    turn = next(r["data"]["result"]["turn"]["id"] for r in recorded if r["src"] == "rpc_in"
                and r["data"].get("id") == start["id"])
    rest, after = [], False
    for r in recorded:
        d = r["data"]
        if r["src"] != "rpc_in" or "method" not in d:
            continue
        if after:
            rest.append(d)
        elif d["method"] == "turn/completed" and d["params"]["threadId"] == thread:
            after = True
    # The parent's leftovers keep its finished turn's id (turn-1 to the peer).
    rest = json.loads(json.dumps(rest).replace(thread, "$THREAD").replace(turn, "turn-1"))
    usage = next(m for m in reversed(first) if m.get("method") == "thread/tokenUsage/updated"
                 and m["params"]["threadId"] == "$THREAD")
    more = copy.deepcopy(usage)
    more["params"]["tokenUsage"]["total"] = {k: v * 2 for k, v in usage["params"]["tokenUsage"]["total"].items()}
    second = rest + [copy.deepcopy(first[0]), more,
                     {"method": "turn/completed", "params": {"threadId": "$THREAD",
                                                            "turn": {"id": "$TURN", "status": "completed"}}}]
    return [first, second]


class Run:
    """One replayed session: its turns' events, and the handle (closed)."""

    def __init__(self, turns, handle):
        self.turns, self.handle = turns, handle

    def of(self, kind):
        return [e for t in self.turns for e in t if e.kind == kind]

    def life(self, sid):
        return [e.data for e in self.of(K.SUBAGENT) if e.data["sid"] == sid]

    def inner(self, sid, kind=None):
        return [e.data["event"] for e in self.of(K.SUBAGENT_EVENT) if e.data["sid"] == sid
                and (kind is None or e.data["event"]["kind"] == kind)]

    def parent_side(self):
        return json.dumps([e.data for t in self.turns for e in t if e.kind in PARENT_KINDS])


def replay(name, *, edit=None, turns=None, permit=None, between=None) -> Run:
    """Play `name` through the real CodexProvider, as an owner chat. `edit`
    changes each turn's messages (turn index, messages); `between(provider,
    handle)` runs between sends."""
    script = turns if turns is not None else turns_of(name)
    if edit is not None:
        script = [edit(i, copy.deepcopy(t)) for i, t in enumerate(script)]
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="sv2-")))
        auth = root / "owner"
        (auth / "skills").mkdir(parents=True)
        (auth / "auth.json").write_text("FAKE AUTH: must never be read by provider")
        work = root / "work"
        work.mkdir()
        brain = P.Brain(root)
        stack.enter_context(patch.object(config, "V2_DATA_DIR", root / "data"))
        stack.enter_context(patch.object(config, "CHAT_WORKTREES_DIR", root / "chat-worktrees"))
        stack.enter_context(patch.object(codex_config, "owner_home", return_value=auth))
        stack.enter_context(patch.object(codex, "RpcProcess", brain.rpc))
        stack.enter_context(patch.object(codex.CodexProvider, "_probe", return_value=("/fake/codex", "fake")))
        brain.script.update(mode="replay", replays=script)
        provider = codex.CodexProvider()
        brief = Brief(Role.CHAT, str(work), model="fake-model", effort="high")
        h = provider.start(Thread(name[:20], "p", Role.CHAT, PN.CODEX), brief,
                           permit or (lambda *a, **k: Decision.DENY))
        out = []
        try:
            for i in range(len(script)):
                if i and between is not None:
                    between(provider, h)
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(lambda: list(provider.send(h, UserMessage(f"turn {i + 1}"))))
                    try:
                        out.append(future.result(timeout=30))
                    except TimeoutError:
                        provider.close(h)
                        raise AssertionError(f"the replayed {name} turn {i + 1} never finished")
        finally:
            run = Run(out, h)
            run.live = provider.live_subagents(h) if hasattr(provider, "live_subagents") else None
            provider.close(h)
        return run


@functools.cache
def played(name) -> Run:
    return replay(name)


def sid(thread_id):
    return "cx:" + thread_id


# --- the provider --------------------------------------------------------------


class ProseIsolation(unittest.TestCase):
    def test_no_childs_word_reaches_the_parents_events(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                run = played(name)
                words, names = child_words(name)
                self.assertTrue(words or names, name)
                side = run.parent_side()
                self.assertEqual([w for w in words | names if w in side], [], "a child's word as the parent's")
                said = json.dumps([e.data for e in run.of(K.SUBAGENT_EVENT)])
                # (parent_interrupt's child speaks only after the session that
                # ran it is gone: see the test below.)
                texts = {w for w in words if w.startswith("SV0 child")} if name != "parent_interrupt" else set()
                self.assertEqual([w for w in texts if w not in said], [], "the child's words are its own")
                self.assertTrue(run.of(K.SUBAGENT), name)

    def test_the_parent_keeps_its_own_turn_words_and_accounting(self):
        for name in ("one", "parallel", "nested", "fail", "child_interrupt", "approval"):
            with self.subTest(name=name):
                run = played(name)
                self.assertEqual(run.turns[-1][-1].kind, K.TURN_FINISHED)
                self.assertEqual(run.turns[-1][-1].data, {"stop": "end"})
                self.assertEqual([e.data for e in run.of(K.ERROR) if e.data.get("fatal")], [])
                self.assertEqual([e.data["text"] for e in run.of(K.TEXT)],
                                 [f"SV0 parent: {name} done at step 3."])
                # SV-0 finding 13: the parent's own requests, 100 input each
                # (its last recorded total); every child's tokens are its own.
                parent = parent_thread(name)
                totals = [r["data"]["params"]["tokenUsage"]["total"] for r in rows(name)
                          if r["src"] == "rpc_in" and r["data"].get("method") == "thread/tokenUsage/updated"
                          and r["data"]["params"]["threadId"] == parent]
                self.assertEqual(run.handle.native.accounting.total["inputTokens"], totals[-1]["inputTokens"])
                reported = [e.data["provider_reported"]["total"]["totalTokens"] for e in run.of(K.USAGE)
                            if "total" in e.data["provider_reported"]]
                self.assertEqual(reported, [t["totalTokens"] for t in totals])

    def test_a_childs_turn_completed_never_ends_the_parents_turn(self):
        run = played("child_interrupt")
        # The child's turn ends interrupted long before the parent's ends.
        finished = [i for i, e in enumerate(run.turns[0]) if e.kind == K.SUBAGENT
                    and e.data["op"] == "finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual([e.kind for e in run.turns[0]].count(K.TURN_FINISHED), 1)
        self.assertGreater(len(run.turns[0]) - 1, finished[0])

    def test_a_thread_nobody_announced_is_still_dropped(self):
        def foreign(i, msgs):
            # The child's whole traffic re-addressed to a thread nobody announced.
            child = next(iter(children("one")))
            return json.loads(json.dumps(msgs).replace(f'"threadId": "{child}"', f'"threadId": "{UNANNOUNCED}"'))
        run = replay("one", edit=foreign)
        self.assertEqual(run.of(K.SUBAGENT_EVENT), [])
        self.assertEqual([e.data["sid"] for e in run.of(K.SUBAGENT)], [sid(next(iter(children("one"))))])
        self.assertNotIn("SV0 child", run.parent_side())

        def announced_elsewhere(i, msgs):
            for m in msgs:
                item = (m.get("params") or {}).get("item") or {}
                if item.get("type") == "subAgentActivity":
                    m["params"]["threadId"] = UNANNOUNCED
            return msgs
        run = replay("one", edit=announced_elsewhere)
        self.assertEqual(run.of(K.SUBAGENT) + run.of(K.SUBAGENT_EVENT), [])
        self.assertNotIn("SV0 child", run.parent_side())


class Lifecycle(unittest.TestCase):
    def test_started_comes_first_and_once(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                run = played(name)
                seen = []
                for e in [e for t in run.turns for e in t]:
                    if e.kind in (K.SUBAGENT, K.SUBAGENT_EVENT):
                        if e.data["sid"] not in seen:
                            self.assertEqual((e.kind, e.data.get("op")), (K.SUBAGENT, "started"), e.data)
                            seen.append(e.data["sid"])
                self.assertEqual(seen, [sid(t) for t in children(name)])
                for s in seen:
                    self.assertEqual([d["op"] for d in run.life(s)].count("started"), 1, s)

    def test_the_end_each_recording_shows(self):
        want = {"one": ["done"], "parallel": ["done", "done"], "nested": ["done", "done"],
                "fail": ["failed"], "child_interrupt": ["stopped"], "close_agent": ["stopped"],
                "approval": ["done"], "approval_accept": ["done"], "patch_approval_child": ["done"],
                "one_multi_agent_v2": ["done"], "no_wait": ["done"], "steer_parent": ["done"]}
        for name, ends in want.items():
            with self.subTest(name=name):
                run = played(name)
                got = []
                for t in children(name):
                    finished = [d for d in run.life(sid(t)) if d["op"] == "finished"]
                    self.assertEqual(len(finished), 1, (t, run.life(sid(t))))
                    got.append(finished[0]["status"])
                    self.assertIsInstance(finished[0].get("duration_ms"), int)
                self.assertEqual(got, ends)
                self.assertEqual(run.live, set())

    def test_why_a_child_stopped_and_why_it_failed(self):
        stop = lambda name: [d for d in played(name).life(sid(next(iter(children(name)))))
                             if d["op"] == "finished"][0]
        self.assertEqual(stop("close_agent")["stop_reason"], "the orchestrator stopped it")
        self.assertEqual(stop("child_interrupt")["stop_reason"], "its turn was interrupted")
        self.assertNotIn("stop_reason", stop("one"))
        run = played("fail")
        failer = sid(next(iter(children("fail"))))
        result = run.inner(failer, "result")
        self.assertEqual(len(result), 1)
        self.assertIn("SV0 synthetic child failure", result[0]["data"]["text"])
        self.assertEqual(result[0]["data"]["status"], "failed")

    def test_after_the_owners_stop_the_next_send_ends_the_session_and_its_children(self):
        """`parent_interrupt`: the parent's turn ends interrupted while its
        child runs on. Codex's accounting for that turn is incomplete, so
        the next send closes the session (unchanged, codex.py) — the
        app-server, and the child, with it. The provider never says it
        finished; the daemon ends it with the session (`Routes`)."""
        run = played("parent_interrupt")
        child = sid(next(iter(children("parent_interrupt"))))
        self.assertEqual(run.turns[0][-1].data, {"stop": "interrupted"})
        self.assertEqual([d["status"] for d in run.life(child)], ["running", "running"])
        self.assertEqual([e.data.get("fatal") for e in run.turns[1]], [True])
        self.assertNotIn("SV0 child", json.dumps([e.data for t in run.turns for e in t]))

    def test_children_outlive_the_parents_turn(self):
        for name in ("no_wait", "steer_parent"):
            with self.subTest(name=name):
                run = played(name)
                child = sid(next(iter(children(name))))
                first = [e.data for e in run.turns[0] if e.kind == K.SUBAGENT and e.data["sid"] == child]
                self.assertEqual([d["status"] for d in first if d["op"] == "finished"], [])
                self.assertEqual(first[0]["status"], "running")
                self.assertEqual(run.turns[0][-1].data, {"stop": "end"})
                second = [e.data for e in run.turns[1] if e.kind == K.SUBAGENT and e.data["sid"] == child]
                self.assertEqual([d["status"] for d in second if d["op"] == "finished"], ["done"])
                self.assertIn({"kind": "text", "data": {"text": "SV0 child child1: finished."}},
                              run.inner(child))
                # The parent's next turn is its own: its words, its tokens.
                self.assertNotIn("SV0 child", run.parent_side())
                self.assertEqual(run.turns[1][-1].data, {"stop": "end"})

    def test_a_turn_end_leaves_a_live_child_live(self):
        seen = {}

        def after_first(provider, h):
            seen["live"] = provider.live_subagents(h)
        run = replay("no_wait", between=after_first)
        child = sid(next(iter(children("no_wait"))))
        self.assertEqual(seen["live"], {child})
        self.assertEqual(run.live, set())
        # A model switch rides the next turn/start: no child is ended by it.
        seen.clear()

        def switch(provider, h):
            provider.set_model(h, "other-model", None)
            seen["live"] = provider.live_subagents(h)
        replay("no_wait", between=switch)
        self.assertEqual(seen["live"], {child})

    def test_nesting_gives_depth_and_parent(self):
        run = played("nested")
        outer, inner = (sid(t) for t in children("nested"))
        started = {e.data["sid"]: e.data for e in run.of(K.SUBAGENT) if e.data["op"] == "started"}
        self.assertEqual((started[outer]["depth"], started[outer].get("parent_sid")), (1, None))
        self.assertEqual((started[inner]["depth"], started[inner]["parent_sid"]), (2, outer))
        self.assertEqual(started[inner]["name"], "/root/sv0_outer/sv0_inner")
        self.assertEqual(run.inner(inner, "tool_started")[0]["data"]["args"]["command"],
                         "/bin/bash -lc 'echo SV0-child2'")

    def test_tokens_and_tools_ride_the_lifecycle(self):
        run = played("one")
        child = sid(next(iter(children("one"))))
        life = run.life(child)
        self.assertEqual([d.get("tokens") for d in life if d.get("tokens")][-1], 220)
        last = life[-1]
        self.assertEqual((last["tool_uses"], last["last_tool"]), (1, "shell"))
        self.assertEqual(last["name"], "/root/sv0_alpha")
        self.assertIs(last["background"], False)

    def test_a_finished_child_given_another_turn_is_live_again(self):
        child = next(iter(children("one")))

        def again(i, msgs):
            at = next(k for k, m in enumerate(msgs) if m.get("method") == "turn/completed"
                      and m["params"]["threadId"] == child)
            msgs[at + 1:at + 1] = [
                {"method": "turn/started", "params": {"threadId": child, "turn": {"id": "again"}}},
                {"method": "item/completed", "params": {"threadId": child, "turnId": "again", "item": {
                    "type": "agentMessage", "id": "m2", "text": "SV0 child: second go."}}},
                {"method": "turn/completed", "params": {"threadId": child, "turn": {
                    "id": "again", "status": "completed", "durationMs": 5}}}]
            return msgs
        run = replay("one", edit=again)
        ops = [(d["op"], d["status"]) for d in run.life(sid(child)) if d["op"] != "progress"]
        self.assertEqual(ops, [("started", "running"), ("finished", "done"),
                               ("started", "running"), ("finished", "done")])
        self.assertEqual(run.inner(sid(child), "text")[-1]["data"]["text"], "SV0 child: second go.")
        # Another turn's end, or a second end, is not said twice.
        self.assertEqual(len([d for d in run.life(sid(child)) if d["op"] == "finished"]), 2)

    def test_a_closed_thread_stops_a_running_child(self):
        child = next(iter(children("child_interrupt")))

        def closed(i, msgs):
            at = next(k for k, m in enumerate(msgs) if m.get("method") == "turn/completed"
                      and m["params"]["threadId"] == child)
            msgs[at] = {"method": "thread/closed", "params": {"threadId": child}}
            return msgs
        run = replay("child_interrupt", edit=closed)
        end = [d for d in run.life(sid(child)) if d["op"] == "finished"]
        self.assertEqual([(d["status"], d["stop_reason"]) for d in end], [("stopped", "Codex closed it")])


class Privacy(unittest.TestCase):
    def test_a_tool_is_its_name_label_source_and_outcome(self):
        run = played("approval")
        child = sid(next(iter(children("approval"))))
        started = run.inner(child, "tool_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["data"]["name"], "shell")
        self.assertEqual(set(started[0]["data"]["args"]), {"command", "cwd"})
        finished = run.inner(child, "tool_finished")
        self.assertEqual([(f["data"]["outcome"], f["data"]["ok"]) for f in finished], [("denied", False)])
        self.assertNotIn("summary", json.dumps(finished))
        # A child's file change: its paths for the label, never its diff.
        run = played("patch_approval_child")
        child = sid(next(iter(children("patch_approval_child"))))
        started = run.inner(child, "tool_started")
        self.assertEqual(started[0]["data"]["name"], "apply_patch")
        self.assertEqual(set(started[0]["data"]["args"]), {"paths"})
        self.assertNotIn("diff", json.dumps([e.data for e in run.of(K.SUBAGENT_EVENT)]))
        # A command still running when its child was interrupted finishes later.
        run = played("child_interrupt")
        child = sid(next(iter(children("child_interrupt"))))
        kinds = [e.data.get("op") or e.data["event"]["kind"] for e in run.of(K.SUBAGENT) + run.of(K.SUBAGENT_EVENT)
                 if e.data["sid"] == child]
        self.assertIn("tool_finished", kinds)
        order = [e for e in run.turns[0] if e.kind in (K.SUBAGENT, K.SUBAGENT_EVENT)
                 and (e.data.get("op") == "finished" or e.data.get("event", {}).get("kind") == "tool_finished")]
        self.assertEqual([e.kind for e in order], [K.SUBAGENT, K.SUBAGENT_EVENT])

    def inject(self, name, extra, at_kind="turn/completed"):
        child = next(iter(children(name)))

        def edit(i, msgs):
            at = next(k for k, m in enumerate(msgs) if m.get("method") == at_kind
                      and m["params"]["threadId"] == child)
            msgs[at:at] = [json.loads(json.dumps(m).replace("$CHILD", child)) for m in extra]
            return msgs
        return replay(name, edit=edit), sid(child)

    def test_deltas_reasoning_plans_and_the_brief_are_not_passed_on(self):
        p = {"threadId": "$CHILD", "turnId": "t"}
        run, child = self.inject("one", [
            {"method": "item/agentMessage/delta", "params": {**p, "delta": "SV2 delta words"}},
            {"method": "item/reasoning/summaryTextDelta", "params": {**p, "delta": "SV2 thinking"}},
            {"method": "item/completed", "params": {**p, "item": {"type": "reasoning", "id": "r",
                                                                  "summary": ["SV2 reasoned"], "content": []}}},
            {"method": "turn/plan/updated", "params": {**p, "plan": [{"step": "SV2 plan", "status": "x"}]}},
            {"method": "item/completed", "params": {**p, "item": {"type": "userMessage", "id": "u",
                                                                  "content": [{"type": "text", "text": "SV2 brief"}]}}},
            {"method": "item/completed", "params": {**p, "item": {"type": "plan", "id": "pl", "text": "SV2 plan2"}}},
        ])
        everything = json.dumps([e.data for t in run.turns for e in t])
        self.assertNotIn("SV2", everything)

    def test_a_childs_text_is_scrubbed(self):
        from jarvis.tools import secrets
        real = secrets.scrub
        with patch.object(secrets, "scrub", side_effect=lambda t: real(t).replace(FAKE_SECRET, "[redacted]")):
            run, child = self.inject("one", [{"method": "item/completed", "params": {
                "threadId": "$CHILD", "turnId": "t", "item": {"type": "agentMessage", "id": "leak",
                                                              "text": f"token {FAKE_SECRET} here"}}}])
        texts = [e["data"]["text"] for e in run.inner(child, "text")]
        self.assertIn("token [redacted] here", texts)
        self.assertNotIn(FAKE_SECRET, json.dumps([e.data for t in run.turns for e in t]))
        # A scrub that fails withholds the text.
        with patch.object(secrets, "scrub", side_effect=RuntimeError("no")):
            run, child = self.inject("one", [{"method": "item/completed", "params": {
                "threadId": "$CHILD", "turnId": "t", "item": {"type": "agentMessage", "id": "leak",
                                                              "text": f"token {FAKE_SECRET} here"}}}])
        self.assertNotIn(FAKE_SECRET, json.dumps([e.data for t in run.turns for e in t]))

    def test_browser_act_arguments_never_ride_the_envelope(self):
        p = {"threadId": "$CHILD", "turnId": "t"}
        act = {"type": "mcpToolCall", "id": "act1", "server": "jarvis", "tool": "hud_browser_act",
               "arguments": {"instruction": "SV2 private page words"}, "status": "inProgress"}
        run, child = self.inject("one", [
            {"method": "item/started", "params": {**p, "item": act}},
            {"method": "item/completed", "params": {**p, "item": {**act, "status": "completed", "result": {
                "content": [{"type": "text", "text": "SV2 private result"}]}}}}])
        started = run.inner(child, "tool_started")[-1]["data"]
        self.assertEqual((started["name"], started["args"]), ("hud_browser_act", {}))
        self.assertNotIn("SV2 private", json.dumps([e.data for t in run.turns for e in t]))
        self.assertEqual(run.inner(child, "tool_finished")[-1]["data"]["outcome"], "ok")

    def test_a_reviewers_decline_is_a_name(self):
        run, child = self.inject("one", [{"method": "item/autoApprovalReview/completed", "params": {
            "threadId": "$CHILD", "turnId": "t", "targetItemId": "x",
            "review": {"status": "denied", "rationale": "SV2 private rationale"},
            "action": {"type": "command", "command": "SV2 private command"}}}])
        declined = run.inner(child, "reviewer_declined")
        self.assertEqual([d["data"] for d in declined], [{"tool": "shell"}])
        self.assertNotIn("SV2 private", json.dumps([e.data for t in run.turns for e in t]))
        self.assertEqual(run.of(K.REVIEWER_DECLINED), [])

    def test_malformed_child_traffic_is_dropped_never_fatal(self):
        p = {"threadId": "$CHILD", "turnId": "t"}
        run, child = self.inject("one", [
            {"method": "turn/started", "params": {"threadId": "$CHILD", "turn": "nope"}},
            {"method": "thread/tokenUsage/updated", "params": {**p, "tokenUsage": {"total": {"totalTokens": "x"}}}},
            {"method": "thread/tokenUsage/updated", "params": {**p, "tokenUsage": []}},
            {"method": "item/started", "params": {**p, "item": {"type": "commandExecution", "id": ["x"]}}},
            {"method": "item/completed", "params": {**p, "item": "nope"}},
            {"method": "item/completed", "params": {**p, "item": {"type": "agentMessage", "id": "m", "text": 7}}},
            {"method": "item/autoApprovalReview/completed", "params": {**p, "review": "denied"}},
            {"method": "model/rerouted", "params": {**p, "fromModel": "a", "toModel": "b"}},
        ])
        self.assertEqual(run.turns[0][-1].data, {"stop": "end"})
        self.assertEqual([e.data for e in run.of(K.ERROR) if e.data.get("fatal")], [])


class Bounds(unittest.TestCase):
    def test_the_bounds_are_these_numbers(self):
        self.assertEqual((codex.CHILD_LIMIT, codex.CHILD_CALL_LIMIT, codex.CHILD_TEXT_CHARS), (256, 256, 4000))

    def test_a_childs_open_calls_are_bounded(self):
        child = next(iter(children("one")))
        p = {"threadId": child, "turnId": "t"}

        def many(i, msgs):
            at = next(k for k, m in enumerate(msgs) if m.get("method") == "turn/completed"
                      and m["params"]["threadId"] == child)
            msgs[at:at] = [{"method": "item/started", "params": {**p, "item": {
                "type": "commandExecution", "id": f"c{k}", "command": "true", "cwd": "$CWD"}}}
                for k in range(12)]
            return msgs
        with patch.object(codex, "CHILD_CALL_LIMIT", 4):
            run = replay("one", edit=many)
            self.assertLessEqual(len(run.handle.native.child_views[child].calls), 4)
        self.assertEqual(run.life(sid(child))[-1]["tool_uses"], 13)

    def test_a_childs_record_goes_with_its_registration(self):
        """CHILD_LIMIT (SV-2a) forgets the oldest finished child; its record
        goes with it, and a child Codex never let in stays unknown, its words
        dropped, never the parent's."""
        old = "01a140cb-0000-7000-8000-0000000000old"

        def older(i, msgs):
            at = next(k for k, m in enumerate(msgs) if m.get("method") == "item/started"
                      and m["params"]["item"].get("type") == "subAgentActivity")
            announce = copy.deepcopy(msgs[at])
            announce["params"]["item"].update(id="call_spawn_old", agentThreadId=old, agentPath="/root/old")
            msgs[at:at] = [announce,
                           {"method": "turn/started", "params": {"threadId": old, "turn": {"id": "o"}}},
                           {"method": "turn/completed", "params": {"threadId": old, "turn": {
                               "id": "o", "status": "completed"}}}]
            return msgs
        with patch.object(codex, "CHILD_LIMIT", 1):
            run = replay("one", edit=older)
        self.assertEqual(list(run.handle.native.child_views), [next(iter(children("one")))])
        self.assertEqual(set(run.handle.native.child_views), set(run.handle.native.children))
        self.assertEqual({e.data["sid"] for e in run.of(K.SUBAGENT)}, {sid(old), sid(next(iter(children("one"))))})

    def test_a_sid_that_does_not_fit_is_hashed(self):
        self.assertEqual(codex.child_sid("01a140cb-0a0d-7292"), "cx:01a140cb-0a0d-7292")
        odd = codex.child_sid("a/b c")
        self.assertRegex(odd, r"^cx:h[0-9a-f]{32}$")
        self.assertTrue(S.valid_sid(odd))
        self.assertTrue(S.valid_sid(codex.child_sid("x" * 128)))
        self.assertTrue(S.valid_sid(codex.child_sid("x" * 129)))


# --- the daemon ----------------------------------------------------------------


class Replay:
    """A provider that hands the daemon what the real CodexProvider made of a
    recording, turn by turn."""

    def __init__(self):
        self.turns: list[list[Event]] = []
        self.closed = []

    def health(self):
        return True, "replay"

    def start(self, thread, brief, permit):
        from types import SimpleNamespace
        return SessionHandle(thread.id, PN.CODEX, "codex:replay-" + thread.id,
                             SimpleNamespace(brief=brief, closed=False))

    resume = start

    def usage(self, handle):
        return Usage()

    def send(self, handle, message):
        for event in self.turns.pop(0) if self.turns else []:
            yield Event(event.kind, handle.thread_id, copy.deepcopy(event.data))

    def interrupt(self, handle):
        pass

    def close(self, handle):
        self.closed.append(handle.thread_id)


class Daemon(Daemonised):
    @classmethod
    def setUpClass(cls):
        # Replayed before any daemon runs: the replay patches config paths.
        for name in SCENARIOS:
            played(name)

    def setUp(self):
        super().setUp()
        self.provider = Replay()
        self.daemon.providers[PN.CODEX] = self.provider
        self.everything = self.daemon.bus.subscribe()

    def open(self):
        self.chat = self.daemon.open_thread(self.project.id, Role.CHAT, PN.CODEX, {})
        self.bus()

    def play(self, name, turns=None):
        self.open()
        got = played(name).turns if turns is None else turns
        self.provider.turns = [list(t) for t in got]
        for n in range(len(got)):
            self.daemon.send(self.chat.id, UserMessage(text=f"turn {n + 1}"))
            self.idle()
        return got

    def idle(self):
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.daemon._lock:
                session = self.daemon._sessions.get(self.chat.id)
                if session is None or session.worker is None:
                    return
            time.sleep(0.01)
        self.fail("the turn did not finish")

    def bus(self):
        out = []
        while True:
            try:
                out.append(self.everything.get_nowait())
            except Exception:
                return out

    def log(self):
        return self.daemon.stores.threads.read_log(self.chat.id)

    def sub(self, rest=""):
        return f"/threads/{self.chat.id}/subagents{rest}"


# The broker's card for a child's call (and the gate question the daemon logs
# for it) is the owner's gate surface, and SV-2a put the child's command and
# label on it by design, as the orchestrator's own carry theirs: the one
# record allowed to name them.
CARD_KINDS = ("approval_requested", "approval_resolved", "gate_requested", "gate_resolved")


class Isolation(Daemon):
    def test_no_childs_word_reaches_the_log_the_bus_discord_or_titles(self):
        for name in SCENARIOS:
            with self.subTest(name=name):
                got = self.play(name)
                words, names = child_words(name)
                log = [r for r in self.log() if r.get("kind") not in CARD_KINDS]
                text = json.dumps(log)
                self.assertEqual([w for w in words | names if w in text], [], "a child's word in the log")
                self.assertFalse([r for r in log if str(r.get("kind", "")).startswith("subagent")])
                bus = [r for r in self.bus() if r.get("kind") not in CARD_KINDS]
                self.assertEqual([w for w in words | names if w in json.dumps(bus)], [], "... on the bus")
                records = [r for r in bus if r.get("kind") == "subagent"]
                self.assertTrue(records, name)
                for record in records:
                    self.assertLessEqual(set(record["data"]), BUS_KEYS, record)
                    self.assertTrue(record["data"]["sid"].startswith("cx:"))
                # The parent's reply on the transcript route is its own.
                status, payload = self.raw("GET", f"/threads/{self.chat.id}/transcript?tools=1", {},
                                           port=self.daemon.port)
                self.assertEqual(status, 200)
                said = [m["text"] for m in payload["messages"] if m.get("role") == "assistant"]
                self.assertEqual(said, [e.data["text"] for t in got for e in t if e.kind == K.TEXT])
                self.assertFalse([m for m in payload["messages"] if m.get("role") == "tool"],
                                 "no child's call as the parent's")
                # The Discord mirror's reply for each turn.
                mirror = ChatMirror.__new__(ChatMirror)
                mirror._lock, mirror._places, mirror._speak = threading.Lock(), {}, None
                mirror._dm_id = lambda: "dm"
                full = self.log()
                for turn in {r["turn_id"] for r in full if r.get("kind") == "user"}:
                    segment = [r for r in full if r.get("turn_id") == turn]
                    posts = mirror._turn_posts(self.chat.id, "target", full, segment, turn, len(full), False)
                    content = " ".join(p.content or "" for p in posts)
                    self.assertNotIn("SV0 child", content)
                    self.assertNotIn("shell", content, "a child's tool is not the parent's footer")
                self.assertEqual(titles.owner_text(self.daemon.stores, self.chat.id),
                                 "\n\n".join(f"turn {n + 1}" for n in range(len(got))))
                # Kept apart, in the thread's own record.
                kept = self.daemon.stores.threads.path(self.chat.id).with_name(S.FILE).read_text()
                for e in played(name).of(K.SUBAGENT_EVENT):
                    if e.data["event"]["kind"] == "text":
                        self.assertIn(e.data["event"]["data"]["text"], kept)

    def test_the_ledger_and_tokens_see_the_parents_usage_only(self):
        # Measured against the recording, not the events: the parent's own
        # usage notifications, and its last total (SV-0 finding 13).
        self.play("parallel")
        parent = parent_thread("parallel")
        totals = [r["data"]["params"]["tokenUsage"]["total"] for r in rows("parallel")
                  if r["src"] == "rpc_in" and r["data"].get("method") == "thread/tokenUsage/updated"
                  and r["data"]["params"]["threadId"] == parent]
        thread = self.daemon.stores.threads.get(self.chat.id)
        last = totals[-1]
        self.assertEqual(thread.tokens, max(0, last["inputTokens"] - last["cachedInputTokens"]) + last["outputTokens"])
        ledger = [r for r in self.daemon.router.ledger._rows if r.get("thread_id") == self.chat.id]
        # One row per parent usage notification, and one per account-wide
        # rate-limit update (which carries the parent's totals, unchanged).
        rates = [m for m in P.fixture_turn("parallel") if m.get("method") == "account/rateLimits/updated"]
        self.assertEqual(len(ledger), len(totals) + len(rates))


class Routes(Daemon):
    def test_the_owner_reads_codex_children(self):
        self.play("nested")
        outer, inner = (sid(t) for t in children("nested"))
        listed = {c["sid"]: c for c in self.owner("GET", self.sub())["subagents"]}
        self.assertEqual(set(listed), {outer, inner})
        self.assertEqual((listed[outer]["status"], listed[outer]["depth"], listed[outer]["name"]),
                         ("done", 1, "/root/sv0_outer"))
        self.assertEqual((listed[inner]["depth"], listed[inner]["parent_sid"], listed[inner]["tokens"]),
                         (2, outer, 220))
        self.assertTrue(listed[inner]["ended_at"])
        got = self.owner("GET", self.sub(f"/{inner}/transcript"))
        self.assertEqual([m["role"] for m in got["messages"]], ["tool", "assistant"])
        tool = got["messages"][0]
        self.assertEqual((tool["name"], tool["label"], tool["outcome"]),
                         ("shell", "/bin/bash -lc 'echo SV0-child2'", "ok"))
        self.assertEqual(got["messages"][1]["text"], "SV0 child child2: finished.")
        for key in ('"args"', '"summary"', '"command"', '"cwd"', '"key"'):
            self.assertNotIn(key, json.dumps(got))
        for port, headers in ((self.daemon.port, {}), (self.face, {}),
                              (self.face, {"Origin": "null"})):
            status, _ = self.raw("GET", self.sub(f"/{inner}/transcript"), headers, port=port)
            self.assertEqual(status, 403)

    def test_a_failed_child_shows_codexs_reason(self):
        self.play("fail")
        failer = sid(next(iter(children("fail"))))
        got = self.owner("GET", self.sub(f"/{failer}/transcript"))
        self.assertEqual(got["subagent"]["status"], "failed")
        self.assertEqual([m["role"] for m in got["messages"]], ["result"])
        self.assertIn("SV0 synthetic child failure", got["messages"][0]["text"])

    def test_a_child_outlives_its_turn_and_ends_with_the_session(self):
        got = played("no_wait").turns
        self.open()
        self.provider.turns = [list(got[0])]
        self.daemon.send(self.chat.id, UserMessage(text="one"))
        self.idle()
        child = sid(next(iter(children("no_wait"))))
        row = self.owner("GET", self.sub())["subagents"][0]
        self.assertEqual((row["sid"], row["status"]), (child, "running"))
        self.daemon.close_thread(self.chat.id)
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = self.owner("GET", self.sub())["subagents"][0]
            if row["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual((row["status"], row["stop_reason"]), ("stopped", S.GONE))

    def test_a_fatal_send_ends_the_children_with_the_session(self):
        self.play("parent_interrupt")
        child = sid(next(iter(children("parent_interrupt"))))
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            row = self.owner("GET", self.sub())["subagents"][0]
            if row["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual((row["sid"], row["status"], row["stop_reason"]), (child, "stopped", S.GONE))

    def test_a_child_that_outlived_its_turn_is_done_after_the_next(self):
        self.play("steer_parent")
        child = sid(next(iter(children("steer_parent"))))
        row = self.owner("GET", self.sub())["subagents"][0]
        self.assertEqual((row["sid"], row["status"]), (child, "done"))
        roles = [m["role"] for m in self.owner("GET", self.sub(f"/{child}/transcript"))["messages"]]
        self.assertEqual(roles, ["tool", "assistant"])


class Revival(unittest.TestCase):
    """The record's half of a Codex child given another turn: its provider's
    own `started`, from the session that ran it, makes an ended child live
    again; a live word from anywhere else stays stale (SV-1's rule)."""

    def setUp(self):
        from types import SimpleNamespace
        import shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="sv2-rec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        threads = SimpleNamespace(path=lambda tid: self.tmp / tid / "log.jsonl",
                                  get=lambda tid: SimpleNamespace(project_id="p"))
        (self.tmp / "t").mkdir()
        self.published = []
        self.rec = S.SubagentRecords(SimpleNamespace(threads=threads), self.published.append,
                                     lambda _tid: "k1")
        self.thread = SimpleNamespace(id="t")

    def put(self, key, **data):
        event = Event(K.SUBAGENT, "t", {"sid": "cx:abc", **data})
        self.rec.record(self.thread, "turn", key, S.SubagentRecords.prepare(event, None))
        return self.rec.list("t")[0]

    def test_started_from_the_same_session_revives_and_nothing_else_does(self):
        self.put("k1", op="started", status="running")
        done = self.put("k1", op="finished", status="done")
        self.assertEqual(done["status"], "done")
        self.assertIn("ended_at", done)
        self.assertEqual(self.put("k1", op="progress", status="running")["status"], "done")
        self.assertEqual(self.put("k2", op="started", status="running")["status"], "done")
        row = self.put("k1", op="started", status="running")
        self.assertEqual(row["status"], "running")
        self.assertNotIn("ended_at", row)
        self.assertEqual(self.put("k1", op="finished", status="done")["status"], "done")


def load_tests(loader, tests, pattern):
    """Only this module's tests (codex_provider_check's run on their own)."""
    suite = unittest.TestSuite()
    for cls in (ProseIsolation, Lifecycle, Privacy, Bounds, Isolation, Routes, Revival):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return suite


if __name__ == "__main__":
    unittest.main(verbosity=2)

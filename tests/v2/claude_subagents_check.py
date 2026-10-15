"""SV-1: Claude's native sub-agents come out of the provider as their own.

Free and offline. Every scenario is a recording SV-0 made of Claude Code
2.1.295 (`fixtures/subagents/claude/*.jsonl`), played back through the real
`ClaudeProvider` by `claude_replay.ReplayClient`: the SDK's own parser, the
provider's own hook. What is pinned, in the order it would hurt:

  - **a child's words are never the orchestrator's** (one, parallel,
    background, nested, failed, stopped): everything a tagged message says
    or does comes out as SUBAGENT_EVENT, and the parent's TEXT/TEXT_DELTA/
    THINKING/TOOL_* carry nothing of it — not in the turn that spawned it,
    not in a later turn a background child talks through;
  - **approval cards name the child, and nothing else changes**: the hook's
    `agent_id` → `task_id` → sid gives the label; `permit` is asked exactly
    what it would be asked for the orchestrator, with the label beside it
    only when it takes the keyword; the label is cleaned;
  - **the lifecycle**: started before any of the child's events; done,
    failed, stopped (`killed`) from the CLI; background children stay live
    past their turn; a top-level foreground child still live when its turn
    ends is stopped, inferred, and the CLI's own word replaces that; a long
    Bash task (`local_bash`) is no child; nesting gives depth and parent;
  - **`_safe` and the private summaries apply inside the envelope**;
  - **bounds**: children, their tool names and refused calls.

Run: PYTHONPATH=. .venv/bin/python tests/v2/claude_subagents_check.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_agent_sdk import AssistantMessage, StreamEvent, SystemMessage, TextBlock, ToolUseBlock  # noqa: E402
from claude_agent_sdk import ToolResultBlock  # noqa: E402
from claude_agent_sdk import UserMessage as SdkUserMessage  # noqa: E402

import claude_replay as R  # noqa: E402
from jarvis.v2.provider import Decision, EventKind as K  # noqa: E402
from jarvis.v2.providers import claude  # noqa: E402

FAKE_TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"
PARENT_KINDS = (K.TEXT, K.TEXT_DELTA, K.THINKING, K.TOOL_STARTED, K.TOOL_FINISHED,
                K.REVIEWER_DECLINED)


def child_strings(name: str) -> set[str]:
    """Every word a child wrote in the recording and every id of a child's
    own call: none may reach the parent's events."""
    out = set()
    for r in R.rows(name):
        d = r["data"]
        if r["src"] != "cli" or not d.get("parent_tool_use_id"):
            continue
        content = (d.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            # A tagged user message's text is the child's brief, which the
            # parent wrote in its own Agent call: not the child's words.
            if block.get("type") == "text" and block.get("text") and d.get("type") == "assistant":
                out.add(block["text"])
            if block.get("type") == "tool_use":
                out.add(block["id"])
            if block.get("type") == "tool_result":
                out.add(block["tool_use_id"])
    return out


def parent_side(turns) -> str:
    return json.dumps([e.data for t in turns for e in t if e.kind in PARENT_KINDS])


def of(turns, kind):
    return [e for t in turns for e in t if e.kind == kind]


def life(turns, sid):
    return [e.data for e in of(turns, K.SUBAGENT) if e.data["sid"] == sid]


SCENARIOS = {  # recording: (sends, the children it spawns)
    "one_fg": (1, ["cl:toolu_sv0_alpha"]),
    "explore_fg": (1, ["cl:toolu_sv0_scout"]),
    "parallel_fg": (1, ["cl:toolu_sv0_alpha", "cl:toolu_sv0_beta", "cl:toolu_sv0_gamma"]),
    "one_default": (2, ["cl:toolu_sv0_alpha"]),
    "parallel_default": (2, ["cl:toolu_sv0_alpha", "cl:toolu_sv0_beta"]),
    "background_unread": (2, ["cl:toolu_sv0_bgquiet"]),
    "nested": (1, ["cl:toolu_sv0_outer", "cl:toolu_sv0_inner"]),
    "fail_fg": (1, ["cl:toolu_sv0_failer"]),
    "stop_fg": (1, ["cl:toolu_sv0_sleeper"]),
    "stop_fg_parallel": (1, ["cl:toolu_sv0_sleeper", "cl:toolu_sv0_napper"]),
    "stop_bg": (2, ["cl:toolu_sv0_bgsleeper"]),
    "interrupt_fg": (2, ["cl:toolu_sv0_sleeper"]),
    "interrupt_bg": (2, ["cl:toolu_sv0_bgsleeper"]),
}


class ProseIsolation(unittest.TestCase):
    def test_no_childs_word_or_call_reaches_the_parents_events(self):
        for name, (turns, sids) in SCENARIOS.items():
            with self.subTest(name=name):
                got, *_ = R.run(name, turns)
                parent = parent_side(got)
                leaked = [s for s in child_strings(name) if s in parent]
                self.assertEqual(leaked, [], f"{name}: a child's words or calls on the parent")
                self.assertEqual(sorted({e.data["sid"] for e in of(got, K.SUBAGENT)}), sorted(sids))

    def test_the_parent_keeps_its_own_words_and_its_spawn_calls(self):
        got, *_ = R.run("parallel_fg")
        texts = [e.data["text"] for e in of(got, K.TEXT)]
        self.assertEqual(texts, ["Launching three.", "SV0 parent: parallel_fg step 1 done."])
        spawns = [e.data["call_id"] for e in of(got, K.TOOL_STARTED)]
        self.assertEqual(spawns, ["toolu_sv0_alpha", "toolu_sv0_beta", "toolu_sv0_gamma"])
        self.assertEqual(sorted(e.data["call_id"] for e in of(got, K.TOOL_FINISHED)), sorted(spawns))

    def test_a_background_childs_words_in_a_later_turn_are_still_its_own(self):
        got, *_ = R.run("one_default", 2)
        later = got[1]
        self.assertNotIn("SV0 child alpha: finished.",
                         json.dumps([e.data for e in later if e.kind in PARENT_KINDS]))
        inner = [e.data["event"] for e in later if e.kind == K.SUBAGENT_EVENT]
        self.assertIn({"kind": "text", "data": {"text": "SV0 child alpha: finished."}}, inner)

    def test_known_gap_an_injected_turns_words_land_in_the_next_send(self):
        # SV-6's problem, pinned so it is changed on purpose: the CLI's own
        # turn for a finished background child (origin task-notification) is
        # read by the next send and its words handed out as that turn's.
        got, *_ = R.run("one_default", 2)
        self.assertEqual([e.data["text"] for e in got[1] if e.kind == K.TEXT],
                         ["SV0 parent: one_default step 2 done."])

    def test_a_tagged_stream_event_yields_nothing(self):
        session = claude._Session(client=None, brief=R.brief(), permit=R.Permit(), thread_id="t1",
                                  loop=None, runner=None)
        provider = claude.ClaudeProvider()
        delta = {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": "child words"}}
        self.assertEqual(provider._translate(session, StreamEvent(
            uuid="u", session_id="s", event=delta, parent_tool_use_id="toolu_x")), [])
        mine = provider._translate(session, StreamEvent(uuid="u", session_id="s", event=delta))
        self.assertEqual([(e.kind, e.data) for e in mine], [(K.TEXT_DELTA, {"text": "child words"})])

    def test_any_tagged_message_is_a_childs_even_unannounced(self):
        # No task_started for it (a CLI that sends none, or one past the
        # bound): its words are still a child's, under a sid of its own.
        session = claude._Session(client=None, brief=R.brief(), permit=R.Permit(), thread_id="t1",
                                  loop=None, runner=None)
        provider = claude.ClaudeProvider()
        msg = AssistantMessage(content=[TextBlock(text="unannounced words")], model="m",
                               parent_tool_use_id="weird id/with spaces")
        got = provider._translate(session, msg)
        self.assertEqual([e.kind for e in got], [K.SUBAGENT, K.SUBAGENT_EVENT])
        sid = got[0].data["sid"]
        self.assertRegex(sid, r"^cl:h[0-9a-f]{32}$")
        self.assertEqual(got[1].data, {"sid": sid, "event": {"kind": "text", "data": {"text": "unannounced words"}}})


class Lifecycle(unittest.TestCase):
    def test_started_comes_before_any_of_the_childs_events(self):
        for name, (turns, sids) in SCENARIOS.items():
            got, *_ = R.run(name, turns)
            flat = [e for t in got for e in t]
            for sid in sids:
                with self.subTest(name=name, sid=sid):
                    first = next(i for i, e in enumerate(flat) if e.data.get("sid") == sid)
                    self.assertEqual((flat[first].kind, flat[first].data["op"]), (K.SUBAGENT, "started"))
                    ops = [d["op"] for d in life(got, sid)]
                    self.assertEqual(ops.count("started"), 1)

    def test_the_end_each_recording_shows(self):
        want = {
            "one_fg": {"cl:toolu_sv0_alpha": "done"},
            "parallel_fg": dict.fromkeys(["cl:toolu_sv0_alpha", "cl:toolu_sv0_beta", "cl:toolu_sv0_gamma"], "done"),
            "nested": {"cl:toolu_sv0_outer": "done", "cl:toolu_sv0_inner": "done"},
            "fail_fg": {"cl:toolu_sv0_failer": "failed"},
            "stop_fg": {"cl:toolu_sv0_sleeper": "stopped"},
            "stop_fg_parallel": {"cl:toolu_sv0_sleeper": "stopped", "cl:toolu_sv0_napper": "done"},
            "stop_bg": {"cl:toolu_sv0_bgsleeper": "stopped"},
            "interrupt_fg": {"cl:toolu_sv0_sleeper": "stopped"},
            "interrupt_bg": {"cl:toolu_sv0_bgsleeper": "stopped"},
            "one_default": {"cl:toolu_sv0_alpha": "done"},
            "parallel_default": {"cl:toolu_sv0_alpha": "done", "cl:toolu_sv0_beta": "done"},
        }
        for name, ends in want.items():
            got, *_ = R.run(name, SCENARIOS[name][0])
            for sid, status in ends.items():
                with self.subTest(name=name, sid=sid):
                    finished = [d for d in life(got, sid) if d["op"] == "finished"]
                    self.assertEqual([d["status"] for d in finished], [status])
                    self.assertNotIn("stop_reason", finished[0], "the CLI's word, not inferred")

    def test_a_failed_childs_error_and_a_done_childs_report_are_its_own(self):
        got, *_ = R.run("fail_fg")
        result = [e.data["event"] for e in of(got, K.SUBAGENT_EVENT) if e.data["event"]["kind"] == "result"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["data"]["status"], "failed")
        self.assertIn("API err", result[0]["data"]["text"])

    def test_background_children_outlive_their_turn(self):
        for name in ("one_default", "parallel_default", "stop_bg", "interrupt_bg", "background_unread"):
            with self.subTest(name=name):
                got, *_ = R.run(name, 2)
                first = [e.data for e in got[0] if e.kind == K.SUBAGENT]
                self.assertTrue(first)
                self.assertTrue(all(d["status"] == "backgrounded" and d["background"] for d in first))
                self.assertFalse([d for d in first if d["op"] == "finished"],
                                 "a background child was ended with its turn")

    def test_turn_end_stops_a_live_top_level_foreground_child_and_the_cli_overrules(self):
        def drop_ends(rs):
            # The child's own ending is lost from the stream: as far as the
            # provider knows it is still running when the turn ends.
            return [r for r in rs if not (r["src"] == "cli" and r["data"].get("subtype") in
                                          ("task_updated", "task_notification"))]
        got, provider, session, *_ = R.run("one_fg", edit=drop_ends)
        finished = [d for d in life(got, "cl:toolu_sv0_alpha") if d["op"] == "finished"]
        self.assertEqual([(d["status"], d.get("stop_reason")) for d in finished],
                         [("stopped", "the turn ended")])
        turn = got[0]
        at = next(i for i, e in enumerate(turn) if e.kind == K.SUBAGENT and e.data["op"] == "finished")
        self.assertLess(at, next(i for i, e in enumerate(turn) if e.kind == K.TURN_FINISHED))
        # The CLI's later word replaces the inference, once.
        late = SystemMessage(subtype="task_notification", data={
            "task_id": "a5fd86f028e49636f", "tool_use_id": "toolu_sv0_alpha", "status": "completed"})
        again = provider._translate(session, late)
        self.assertEqual([(e.data["op"], e.data["status"]) for e in again if e.kind == K.SUBAGENT],
                         [("finished", "done")])
        self.assertNotIn("stop_reason", again[0].data)
        self.assertFalse([e for e in provider._translate(session, late)
                          if e.kind == K.SUBAGENT and e.data["op"] == "finished"],
                         "a second word is not a second finish")

    def test_turn_end_leaves_nested_and_background_children_alone(self):
        def drop_ends(rs):
            return [r for r in rs if not (r["src"] == "cli" and r["data"].get("subtype") in
                                          ("task_updated", "task_notification"))]
        got, *_ = R.run("nested", edit=drop_ends)
        ended = {d["sid"] for d in of_data(got) if d["op"] == "finished"}
        self.assertEqual(ended, {"cl:toolu_sv0_outer"}, "only the top-level foreground child")
        got, *_ = R.run("one_default", 1, edit=drop_ends)
        self.assertFalse([d for d in of_data(got) if d["op"] == "finished"])

    def test_nesting_gives_depth_and_parent(self):
        got, *_ = R.run("nested")
        inner = life(got, "cl:toolu_sv0_inner")[0]
        self.assertEqual((inner["depth"], inner["parent_sid"]), (2, "cl:toolu_sv0_outer"))
        outer = life(got, "cl:toolu_sv0_outer")[0]
        self.assertEqual(outer["depth"], 1)
        self.assertNotIn("parent_sid", outer)
        # The inner spawn is the outer child's call, never the parent's.
        calls = [e.data["call_id"] for e in of(got, K.TOOL_STARTED)]
        self.assertEqual(calls, ["toolu_sv0_outer"])

    def test_a_long_bash_task_is_no_child(self):
        for name in ("stop_fg_parallel", "background_unread"):
            got, *_ = R.run(name, SCENARIOS[name][0])
            sids = {e.data["sid"] for e in of(got, K.SUBAGENT)}
            self.assertFalse([s for s in sids if "bash" in s], name)

    def test_usage_and_tool_ride_the_lifecycle_never_a_usage_event(self):
        got, *_ = R.run("one_fg")
        last = life(got, "cl:toolu_sv0_alpha")[-1]
        self.assertEqual((last["status"], last["tokens"], last["tool_uses"], last["last_tool"]),
                         ("done", 110, 1, "Bash"), "the notification's closing numbers")
        self.assertEqual(len(of(got, K.USAGE)), 1, "the parent's result only")

    def test_the_brief_rides_the_task_started_lifecycle_alone(self):
        """SV-3 (the owner's decision: the brief, collapsed, at the top of
        the pane): `task_started.prompt` goes to the record on that one
        lifecycle event, through `_safe`, and on no other event."""
        for name, (turns, sids) in SCENARIOS.items():
            with self.subTest(name=name):
                got, *_ = R.run(name, turns)
                prompts = {"cl:" + r["data"]["tool_use_id"]: r["data"]["prompt"] for r in R.rows(name)
                           if r["src"] == "cli" and r["data"].get("subtype") == "task_started"
                           and r["data"].get("task_type") == "local_agent"}
                for sid in sids:
                    carried = [d["brief"] for d in life(got, sid) if "brief" in d]
                    self.assertEqual(carried, [prompts[sid]], sid)
                # (A nested spawn's own Agent call carries it as arguments,
                # which the record reads for a label only and never keeps.)
                inner = json.dumps([e.data for e in of(got, K.SUBAGENT_EVENT)
                                    if e.data["event"]["kind"] != "tool_started"])
                for prompt in prompts.values():
                    self.assertNotIn(json.dumps(prompt)[1:-1], inner)

    def test_the_brief_is_scrubbed_and_bounded(self):
        def secret(rs):
            for r in rs:
                if r["src"] == "cli" and r["data"].get("subtype") == "task_started":
                    r["data"]["prompt"] = f"use {FAKE_TOKEN} " + "x" * 20000
            return rs
        got, *_ = R.run("one_fg", edit=secret)
        brief = next(d["brief"] for d in life(got, "cl:toolu_sv0_alpha") if "brief" in d)
        self.assertNotIn(FAKE_TOKEN, brief)
        self.assertLessEqual(len(brief), claude.CHILD_BRIEF_CHARS)
        # Scrubbed whole, then cut: a token across the cut leaves no part of
        # itself behind (review of #212, LOW-5).
        def straddle(rs):
            for r in rs:
                if r["src"] == "cli" and r["data"].get("subtype") == "task_started":
                    r["data"]["prompt"] = "y" * (claude.CHILD_BRIEF_CHARS - 13) + " " + FAKE_TOKEN
            return rs
        got, *_ = R.run("one_fg", edit=straddle)
        brief = next(d["brief"] for d in life(got, "cl:toolu_sv0_alpha") if "brief" in d)
        self.assertNotIn(FAKE_TOKEN[:12], brief)

    def test_stop_and_its_name_never_reach_the_parents_text(self):
        got, *_ = R.run("stop_fg")
        self.assertEqual([e.data["text"] for e in of(got, K.TEXT)], ["SV0 parent: stop_fg step 1 done."])


def of_data(turns):
    return [e.data for e in of(turns, K.SUBAGENT)]


class ApprovalLabels(unittest.TestCase):
    def test_a_childs_call_is_asked_exactly_as_the_parents_with_its_name_beside(self):
        for name, (turns, _sids) in SCENARIOS.items():
            with self.subTest(name=name):
                permit = R.Permit()
                got, _p, session, client, _ = R.run(name, turns, permit=permit)
                hooks = [(tool, agent) for tool, agent, _ in client.hooks]
                self.assertEqual(len(permit.calls), len(hooks))
                names = {r["data"]["task_id"]: r["data"]["description"] for r in R.rows(name)
                         if r["src"] == "cli" and r["data"].get("subtype") == "task_started"}
                for (tool, agent), (asked, _args, label) in zip(hooks, permit.calls):
                    self.assertEqual(asked, tool)
                    self.assertEqual(label, names[agent] if agent else None)
                requested = of(got, K.APPROVAL_REQUESTED)
                for event in requested:
                    if "subagent" in event.data:
                        self.assertTrue(event.data["subagent"].startswith("cl:"))

    def test_the_answer_is_the_permits_whatever_the_label(self):
        for answer in (Decision.ALLOW, Decision.DENY):
            permit = R.Permit(answer)
            _, _, _, client, _ = R.run("one_fg", permit=permit)
            child = [got for tool, agent, got in client.hooks if agent]
            self.assertEqual(len(child), 1)
            if answer is Decision.DENY:
                self.assertEqual(child[0]["hookSpecificOutput"]["permissionDecision"], "deny")
            else:
                self.assertEqual(child[0], {}, "ALLOW under auto still leaves the classifier on")

    def test_a_permit_without_the_keyword_is_asked_plainly_and_decides(self):
        asked = []

        def plain(tool, args, brief):
            asked.append(tool)
            return Decision.ALLOW
        _, _, _, client, _ = R.run("one_fg", permit=plain)
        self.assertEqual(asked, ["Agent", "Bash"])
        self.assertEqual([got for _t, agent, got in client.hooks if agent], [{}])

    def test_the_child_call_refused_by_the_hook_reads_denied_in_its_transcript(self):
        permit = R.Permit(Decision.DENY)
        got, *_ = R.run("one_fg", permit=permit)
        done = [e.data["event"]["data"] for e in of(got, K.SUBAGENT_EVENT)
                if e.data["event"]["kind"] == "tool_finished"]
        self.assertEqual([d["outcome"] for d in done], ["denied"])

    def test_an_unknown_agent_is_still_labelled_a_child(self):
        def unknown(rs):
            for r in rs:
                if r["src"] == "hook" and r["data"].get("agent_id"):
                    r["data"]["agent_id"] = "not-a-task-we-saw"
            return rs
        permit = R.Permit()
        R.run("one_fg", permit=permit, edit=unknown)
        self.assertEqual([label for _t, _a, label in permit.calls], [None, "unnamed"])

    def test_the_label_is_cleaned(self):
        for raw, want in (
            ("plain name", "plain name"),
            ("a · b", "a _ b"),
            ("x› ‹y", "x_ _y"),
            ("line\nbreak‮ and bidi", "line break_ and bidi"),
            ("`code` [link](x)", "code_ _link_x"),
            ("n" * 80, "n" * 39 + "…"),
            ("", "unnamed"),
            (None, "unnamed"),
            (f"key {FAKE_TOKEN}", "key _redacted"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(claude.child_label(raw), want)

    def test_a_childs_card_carries_its_sid_and_the_orchestrators_none(self):
        """SV-3: beside its label, a child's question names its sid
        (`agent_id` -> `task_id` -> the spawn's id), for the HUD's link from
        the card to the child's row; the orchestrator's names none."""
        for name, (turns, _sids) in SCENARIOS.items():
            with self.subTest(name=name):
                seen = []

                def permit(tool, args, brief=None, *, subagent=None, subagent_sid=None):
                    seen.append((subagent, subagent_sid))
                    return Decision.ALLOW
                got, _p, _session, client, _ = R.run(name, turns, permit=permit)
                spawns = {r["data"]["task_id"]: r["data"]["tool_use_id"] for r in R.rows(name)
                          if r["src"] == "cli" and r["data"].get("subtype") == "task_started"}
                agents = [agent for _t, agent, _ in client.hooks]
                self.assertEqual(len(seen), len(agents))
                for agent, (label, sid) in zip(agents, seen):
                    if agent:
                        self.assertEqual(sid, "cl:" + spawns[agent])
                    else:
                        self.assertEqual((label, sid), (None, None))
                # The logged gate question names the same sid.
                self.assertEqual([e.data.get("subagent") for e in of(got, K.APPROVAL_REQUESTED)],
                                 [sid for _label, sid in seen])

    def test_a_permit_taking_only_the_label_is_asked_without_the_sid(self):
        asked = []

        def permit(tool, args, brief=None, *, subagent=None):
            asked.append((tool, subagent))
            return Decision.ALLOW
        _, _, _, client, _ = R.run("one_fg", permit=permit)
        self.assertEqual(asked, [("Agent", None), ("Bash", "SV0 alpha probe")])
        self.assertEqual([got for _t, agent, got in client.hooks if agent], [{}])

    def test_the_label_never_reaches_args_or_the_command(self):
        permit = R.Permit()
        got, *_ = R.run("one_fg", permit=permit)
        for tool, args, label in permit.calls:
            self.assertNotIn("subagent", args)
            self.assertNotIn("SV0 alpha probe", json.dumps(args) if tool == "Bash" else "")


class Privacy(unittest.TestCase):
    def session(self):
        return claude._Session(client=None, brief=R.brief(), permit=R.Permit(), thread_id="t1",
                               loop=None, runner=None)

    def test_safe_applies_inside_the_envelope(self):
        provider, session = claude.ClaudeProvider(), self.session()
        msg = AssistantMessage(content=[TextBlock(text=f"my key is {FAKE_TOKEN}")], model="m",
                               parent_tool_use_id="toolu_child")
        got = provider._translate(session, msg)
        self.assertNotIn(FAKE_TOKEN, json.dumps([e.data for e in got]))
        self.assertIn("[redacted]", got[-1].data["event"]["data"]["text"])

    def test_private_summaries_apply_inside_the_envelope(self):
        provider, session = claude.ClaudeProvider(), self.session()
        marker = "PRIVATE-TERMINAL-TEXT-0123"
        use = AssistantMessage(content=[ToolUseBlock(id="toolu_read", name="mcp__jarvis__terminal_read",
                                                     input={"terminal": "x"})], model="m",
                               parent_tool_use_id="toolu_child")
        provider._translate(session, use)
        result = SdkUserMessage(content=[ToolResultBlock(tool_use_id="toolu_read",
                                                         content=f"its last 3 line(s) {marker}")],
                                parent_tool_use_id="toolu_child")
        got = provider._translate(session, result)
        inner = got[-1].data["event"]
        self.assertEqual((inner["kind"], inner["data"]["summary"]), ("tool_finished", "read 3 lines"))
        self.assertNotIn(marker, json.dumps([e.data for e in got]))

    def test_browser_act_arguments_are_empty_inside_the_envelope(self):
        provider, session = claude.ClaudeProvider(), self.session()
        use = AssistantMessage(content=[ToolUseBlock(id="toolu_act", name="mcp__jarvis__hud_browser_act",
                                                     input={"secret": "page text"})], model="m",
                               parent_tool_use_id="toolu_child")
        got = provider._translate(session, use)
        self.assertEqual(got[-1].data["event"]["data"]["args"], {})


class Bounds(unittest.TestCase):
    def session(self):
        return claude._Session(client=None, brief=R.brief(), permit=R.Permit(), thread_id="t1",
                               loop=None, runner=None)

    @staticmethod
    def started(n, background=True):
        return SystemMessage(subtype="task_started", data={
            "task_id": f"task{n}", "tool_use_id": f"toolu_{n}", "task_type": "local_agent",
            "description": f"child {n}", "is_backgrounded": background, "spawn_depth": 1})

    @staticmethod
    def ended(n):
        return SystemMessage(subtype="task_notification", data={
            "task_id": f"task{n}", "tool_use_id": f"toolu_{n}", "status": "completed"})

    def test_the_oldest_finished_child_makes_room(self):
        provider, session = claude.ClaudeProvider(), self.session()
        for n in range(claude.CHILD_LIMIT):
            provider._translate(session, self.started(n))
        provider._translate(session, self.ended(5))
        provider._translate(session, self.started(999))
        self.assertEqual(len(session.children), claude.CHILD_LIMIT)
        self.assertNotIn("cl:toolu_5", session.children)
        self.assertNotIn("task5", session.tasks)
        self.assertIn("cl:toolu_999", session.children)

    def test_with_every_child_live_a_newcomer_is_not_tracked_but_stays_a_child(self):
        provider, session = claude.ClaudeProvider(), self.session()
        for n in range(claude.CHILD_LIMIT):
            provider._translate(session, self.started(n))
        self.assertEqual(provider._translate(session, self.started(1000)), [])
        msg = AssistantMessage(content=[TextBlock(text="over the bound")], model="m",
                               parent_tool_use_id="toolu_1000")
        got = provider._translate(session, msg)
        self.assertEqual([e.kind for e in got], [K.SUBAGENT_EVENT])
        self.assertEqual(len(session.children), claude.CHILD_LIMIT)

    def test_child_tool_names_and_refusals_are_bounded(self):
        provider, session = claude.ClaudeProvider(), self.session()
        for n in range(claude.CHILD_TOOL_LIMIT + 50):
            provider._translate(session, AssistantMessage(
                content=[ToolUseBlock(id=f"c{n}", name="Bash", input={})], model="m",
                parent_tool_use_id="toolu_child"))
            claude._note_denied(session, f"d{n}", "agent")
        self.assertEqual(len(session.child_tools), claude.CHILD_TOOL_LIMIT)
        self.assertEqual(len(session.child_denied), claude.CHILD_TOOL_LIMIT)
        self.assertNotIn("c0", session.child_tools)

    def test_a_model_switch_stops_the_old_clis_children(self):
        from jarvis.v2.provider import UserMessage
        provider = claude.ClaudeProvider()
        with R.replaying("one_default"):
            handle = provider.start(R.thread(), R.brief(), R.Permit())
            try:
                list(provider.send(handle, UserMessage(text="go")))
                self.assertEqual(provider.live_subagents(handle), {"cl:toolu_sv0_alpha"})
                provider.set_model(handle, "claude-other", None)
                self.assertEqual(provider.live_subagents(handle), set())
                child = handle.native.children["cl:toolu_sv0_alpha"]
                self.assertEqual((child.status, child.stop_reason), ("stopped", "the session restarted"))
            finally:
                provider.close(handle)
        self.assertEqual(provider.live_subagents(handle), set(), "a closed session runs none")

    def test_a_turn_start_forgets_no_child(self):
        got, provider, session, *_ = R.run("one_default", 2)
        self.assertIn("cl:toolu_sv0_alpha", session.children)


if __name__ == "__main__":
    unittest.main(verbosity=1)

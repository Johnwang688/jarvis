"""SV-0: the recorded native sub-agent fixtures say what the findings say.

Free and offline: it only reads `tests/v2/fixtures/subagents/{claude,codex}`,
the recordings `record_claude.py` and `record_codex.py` made from the installed
Claude Code 2.1.295 and Codex 0.161.0 against a scripted local model
(docs/plans/2026-10-15-sv0-findings.md). Each test pins one finding SV-1/SV-2
build on, so a re-recording against a newer CLI that changes a shape fails
here, by name, instead of inside a provider test that replays it.
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

FIX = Path(__file__).resolve().parent / "fixtures" / "subagents"


def rows(kind: str, name: str) -> list[dict]:
    with (FIX / kind / f"{name}.jsonl").open() as f:
        return [json.loads(line) for line in f]


def cli(rs, type_=None, subtype=None):
    return [r for r in rs if r["src"] == "cli" and (type_ is None or r["data"].get("type") == type_)
            and (subtype is None or r["data"].get("subtype") == subtype)]


def harness(rs, action):
    return next(r for r in rs if r["src"] == "harness" and r["data"].get("action") == action)


def model(rs, role, agent, step):
    return next(r for r in rs if r["src"] == "model" and r["data"].get("role") == role
                and r["data"].get("agent") == agent and r["data"].get("step") == step)


CLAUDE = sorted(p.stem for p in (FIX / "claude").glob("*.jsonl"))
CODEX = sorted(p.stem for p in (FIX / "codex").glob("*.jsonl"))


class ClaudeFixtures(unittest.TestCase):
    def test_every_cli_row_parses_with_the_installed_sdk(self):
        from claude_agent_sdk._internal.message_parser import parse_message
        for name in CLAUDE:
            for r in cli(rows("claude", name)):
                parse_message(r["data"])  # raises on a shape the SDK cannot read

    def test_sub_agent_messages_are_tagged_and_carry_the_task_id(self):
        rs = rows("claude", "one_fg")
        started = cli(rs, "system", "task_started")[0]["data"]
        self.assertEqual(started["tool_use_id"], "toolu_sv0_alpha")
        self.assertFalse(started["is_backgrounded"])
        self.assertEqual(started["task_type"], "local_agent")
        tagged = [r["data"] for r in cli(rs) if r["data"].get("parent_tool_use_id")]
        self.assertTrue(tagged)
        for d in tagged:
            self.assertEqual(d["parent_tool_use_id"], "toolu_sv0_alpha")
            self.assertEqual(d["agent_id"], started["task_id"])

    def test_no_sub_agent_deltas_are_streamed(self):
        for name in CLAUDE:
            for r in cli(rows("claude", name), "stream_event"):
                self.assertIsNone(r["data"].get("parent_tool_use_id"), name)

    def test_agents_run_in_the_background_unless_told_otherwise(self):
        rs = rows("claude", "one_default")
        started = cli(rs, "system", "task_started")[0]
        self.assertTrue(started["data"]["is_backgrounded"])
        result = next(r for r in cli(rs, "user")
                      if (r["data"].get("tool_use_result") or {}).get("status") == "async_launched")
        first_child = next(r for r in cli(rs) if r["data"].get("parent_tool_use_id"))
        self.assertLess(result["seq"], first_child["seq"])
        results = cli(rs, "result")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1]["data"]["origin"]["kind"], "task-notification")

    def test_stop_task_stops_one_foreground_agent_and_the_turn_goes_on(self):
        rs = rows("claude", "stop_fg_parallel")
        stop = harness(rs, "stop_task")
        after = [r["data"] for r in rs if r["seq"] > stop["seq"] and r["src"] == "cli"]
        updated = next(d for d in after if d.get("subtype") == "task_updated")
        self.assertEqual(updated["patch"]["status"], "killed")
        note = next(d for d in after if d.get("subtype") == "task_notification")
        self.assertEqual((note["tool_use_id"], note["status"]), ("toolu_sv0_sleeper", "stopped"))
        sibling = [d for d in after if d.get("subtype") == "task_notification"
                   and d.get("tool_use_id") == "toolu_sv0_napper"]
        self.assertEqual([d["status"] for d in sibling], ["completed"])
        self.assertGreater(model(rs, "parent", "stop_fg_parallel", 1)["seq"], stop["seq"])
        self.assertEqual(cli(rs, "result")[-1]["data"]["subtype"], "success")

    def test_interrupt_kills_background_agents_between_turns(self):
        rs = rows("claude", "interrupt_bg")
        first_result = cli(rs, "result")[0]
        interrupt = harness(rs, "interrupt")
        self.assertLess(first_result["seq"], interrupt["seq"])
        note = next(r["data"] for r in cli(rs, "system", "task_notification") if r["seq"] > interrupt["seq"])
        self.assertEqual(note["status"], "stopped")

    def test_interrupting_a_foreground_agent_ends_the_turn_as_aborted(self):
        rs = rows("claude", "interrupt_fg")
        result = cli(rs, "result")[0]["data"]
        self.assertEqual((result["subtype"], result["terminal_reason"]), ("error_during_execution", "aborted_tools"))

    def test_nesting_is_allowed_but_only_depth_one_is_forwarded(self):
        rs = rows("claude", "nested")
        inner = next(r["data"] for r in cli(rs, "system", "task_started")
                     if r["data"]["tool_use_id"] == "toolu_sv0_inner")
        outer = next(r["data"] for r in cli(rs, "system", "task_started")
                     if r["data"]["tool_use_id"] == "toolu_sv0_outer")
        self.assertEqual((inner["spawn_depth"], inner["parent_task_id"]), (2, outer["task_id"]))
        self.assertFalse([r for r in cli(rs) if r["data"].get("parent_tool_use_id") == "toolu_sv0_inner"])
        self.assertTrue(model(rs, "child", "inner", 0))

    def test_a_failed_agent_reports_failed(self):
        rs = rows("claude", "fail_fg")
        note = cli(rs, "system", "task_notification")[0]["data"]
        self.assertEqual(note["status"], "failed")

    def test_a_sub_agents_long_bash_is_a_task_that_is_not_an_agent(self):
        rs = rows("claude", "steer_fg")
        bash = next(r["data"] for r in cli(rs, "system", "task_started")
                    if r["data"]["task_type"] == "local_bash")
        self.assertTrue(bash["owned_by_subagent"])
        self.assertEqual(bash["tool_use_id"], "toolu_sv0_napper_bash")

    def test_hook_agent_ids_are_known_task_ids_before_the_hook_fires(self):
        for name in CLAUDE:
            rs = rows("claude", name)
            for h in (r for r in rs if r["src"] == "hook" and r["data"].get("agent_id")):
                known = {r["data"]["task_id"] for r in cli(rs, "system", "task_started") if r["seq"] < h["seq"]}
                self.assertIn(h["data"]["agent_id"], known, name)

    def test_a_steer_waits_for_the_foreground_agent(self):
        rs = rows("claude", "steer_fg")
        agent_result = next(r for r in cli(rs, "user") if any(
            isinstance(p, dict) and p.get("tool_use_id") == "toolu_sv0_napper"
            for p in r["data"]["message"]["content"]))
        echo = next(r for r in cli(rs, "user") if r["data"].get("isReplay")
                    and "SV0_STEER" in json.dumps(r["data"]["message"]))
        self.assertLess(agent_result["seq"], echo["seq"])

    def test_an_unread_stream_stalls_a_background_agent(self):
        rs = rows("claude", "background_flood")
        stopped = harness(rs, "stop_reading")
        flood = model(rs, "parent", "background_flood", 2)
        late = model(rs, "child", "late", 1)
        # `sleep 4` ended about 4 s in; the child's next request waited for the reader.
        self.assertGreater(late["ms"] - stopped["ms"], 8000)
        self.assertLess(flood["seq"], late["seq"])


class CodexFixtures(unittest.TestCase):
    def parent(self, rs):
        return next(r["data"]["params"]["thread"]["id"] for r in rs
                    if r["src"] == "rpc_in" and r["data"].get("method") == "thread/started")

    def notes(self, rs, method=None):
        return [r for r in rs if r["src"] == "rpc_in" and "method" in r["data"] and "id" not in r["data"]
                and (method is None or r["data"]["method"] == method)]

    def activity(self, rs, thread):
        return [r for r in self.notes(rs, "item/completed")
                if r["data"]["params"]["threadId"] == thread
                and r["data"]["params"]["item"]["type"] == "subAgentActivity"]

    def test_child_notifications_reach_the_parents_connection(self):
        rs = rows("codex", "one")
        parent = self.parent(rs)
        child_methods = {r["data"]["method"] for r in self.notes(rs)
                         if (r["data"].get("params") or {}).get("threadId") not in (None, parent)}
        self.assertTrue({"turn/started", "item/started", "item/completed", "thread/tokenUsage/updated",
                         "turn/completed"} <= child_methods)

    def test_a_spawn_is_a_sub_agent_activity_item_not_a_collab_spawn(self):
        for name in CODEX:
            rs = rows("codex", name)
            for r in self.notes(rs):
                item = (r["data"].get("params") or {}).get("item") or {}
                if item.get("type") == "collabAgentToolCall":
                    self.assertEqual(item["tool"], "wait", name)
        rs = rows("codex", "one")
        parent = self.parent(rs)
        started = self.activity(rs, parent)[0]["data"]["params"]["item"]
        self.assertEqual((started["kind"], started["id"], started["agentPath"]),
                         ("started", "call_spawn_alpha", "/root/sv0_alpha"))
        # item/started (not item/completed) precedes the child's first notification.
        child = started["agentThreadId"]
        first_child = next(r for r in self.notes(rs) if r["data"]["params"].get("threadId") == child)
        announced = next(r for r in self.notes(rs, "item/started")
                         if r["data"]["params"]["item"].get("agentThreadId") == child)
        self.assertLess(announced["seq"], first_child["seq"])
        self.assertGreater(self.activity(rs, parent)[0]["seq"], first_child["seq"])

    def test_children_get_no_thread_started(self):
        for name in CODEX:
            rs = rows("codex", name)
            self.assertEqual(len(self.notes(rs, "thread/started")), 1, name)

    def test_a_childs_approval_request_reaches_the_parents_connection(self):
        for name, decision, status in (("approval", "decline", "declined"),
                                       ("approval_accept", "accept", "failed")):
            rs = rows("codex", name)
            parent = self.parent(rs)
            req = next(r["data"] for r in rs if r["src"] == "rpc_in"
                       and r["data"].get("method") == "item/commandExecution/requestApproval")
            self.assertNotEqual(req["params"]["threadId"], parent)
            done = next(r["data"]["params"]["item"] for r in self.notes(rs, "item/completed")
                        if r["data"]["params"]["item"].get("type") == "commandExecution")
            self.assertEqual(done["status"], status)  # accept ran it; / is not writable

    def test_turn_interrupt_on_a_child_ends_only_that_childs_turn(self):
        rs = rows("codex", "child_interrupt")
        parent = self.parent(rs)
        reply = next(r["data"] for r in rs if r["src"] == "rpc_in" and r["data"].get("id") == 4)
        self.assertEqual(reply["result"], {})
        done = {r["data"]["params"]["threadId"]: r["data"]["params"]["turn"]["status"]
                for r in self.notes(rs, "turn/completed")}
        self.assertEqual(done.pop(parent), "completed")
        self.assertEqual(list(done.values()), ["interrupted"])
        # The command it was running is not killed, and the parent hears nothing.
        late = [r for r in self.notes(rs, "item/completed")
                if r["data"]["params"]["item"].get("type") == "commandExecution"]
        self.assertEqual(late[0]["data"]["params"]["item"]["status"], "completed")
        self.assertEqual([r for r in self.activity(rs, parent)
                          if r["data"]["params"]["item"]["kind"] != "started"], [])

    def test_children_outlive_an_interrupted_parent_turn(self):
        rs = rows("codex", "parent_interrupt")
        parent = self.parent(rs)
        ends = [(r["data"]["params"]["threadId"] == parent, r["data"]["params"]["turn"]["status"], r["seq"])
                for r in self.notes(rs, "turn/completed")]
        self.assertEqual([e[:2] for e in ends], [(True, "interrupted"), (False, "completed")])
        late = self.activity(rs, parent)[-1]["data"]["params"]
        self.assertEqual(late["item"]["kind"], "completed")
        self.assertGreater(self.activity(rs, parent)[-1]["seq"], ends[0][2])

    def test_parent_token_totals_exclude_children(self):
        rs = rows("codex", "one")
        parent = self.parent(rs)
        parent_requests = sum(1 for r in rs if r["src"] == "model" and r["data"]["role"] == "parent")
        last = [r["data"]["params"]["tokenUsage"]["total"]["totalTokens"]
                for r in self.notes(rs, "thread/tokenUsage/updated") if r["data"]["params"]["threadId"] == parent][-1]
        self.assertEqual(last, 110 * parent_requests)

    def test_a_failed_child_is_seen_only_on_its_own_thread(self):
        rs = rows("codex", "fail")
        parent = self.parent(rs)
        failed = [r["data"]["params"] for r in self.notes(rs, "turn/completed")
                  if r["data"]["params"]["turn"]["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertNotEqual(failed[0]["threadId"], parent)
        self.assertEqual([r["data"]["params"]["item"]["kind"] for r in self.activity(rs, parent)], ["started"])

    def test_a_grandchild_is_announced_on_its_parents_thread(self):
        rs = rows("codex", "nested")
        parent = self.parent(rs)
        outer = self.activity(rs, parent)[0]["data"]["params"]["item"]["agentThreadId"]
        inner = self.activity(rs, outer)[0]["data"]["params"]["item"]
        self.assertEqual(inner["agentPath"], "/root/sv0_outer/sv0_inner")

    def test_the_model_side_interrupt_is_reported_as_interrupted(self):
        rs = rows("codex", "close_agent")
        parent = self.parent(rs)
        kinds = [r["data"]["params"]["item"]["kind"] for r in self.activity(rs, parent)]
        self.assertEqual(kinds, ["started", "interrupted"])


if __name__ == "__main__":
    unittest.main()

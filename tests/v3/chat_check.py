"""Offline regression checks for the v3 chat service and its stores."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jarvis import agent, config, context, llm, runtime
from jarvis.v3 import chat, fastpath, jobs
from jarvis.v3.feed import Feed
from jarvis.v3.index import Index
from jarvis.v3.ingest.transcripts import ingest
from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.vault import Vault


def reply(text=None, *calls):
    message = {"content": text}
    if calls:
        message["tool_calls"] = [
            {"id": f"call-{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ]
    return llm.Reply(message=message, finish_reason="tool_calls" if calls else "stop",
                     model="fake-chat", latency_s=0)


class Model:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.seen = []

    def __call__(self, model, messages, **kwargs):
        self.seen.append(json.loads(json.dumps(messages)))
        step = next(self.steps)
        return step(**kwargs) if callable(step) else step


def contents(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class ChatCheck(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = self.base / "data"
        self.root = self.data / "chats"
        self.token = self.base / "google_token.json"
        self.enterContext(patch.object(config, "V3_DIR", self.data))
        self.enterContext(patch.object(config, "GOOGLE_TOKEN_PATH", self.token))
        self.enterContext(patch.object(llm, "chat", side_effect=AssertionError("Unexpected real completion.")))
        self.enterContext(patch.dict(os.environ, {
            "JARVIS_VAULT": str(self.data / "vault"),
            "JARVIS_INDEX_DB": str(self.data / "index.sqlite"),
            "JARVIS_LEARNED_SKILLS": str(self.data / "skills"),
        }))

    def service(self, model, root=None):
        svc = chat.ChatService(self.root if root is None else root, completion=model)
        self.addCleanup(svc.close)
        return svc

    def wait_for(self, predicate):
        end = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= end:
                self.fail("Chat did not reach the expected state.")
            time.sleep(0.005)

    def finish(self, svc):
        self.wait_for(lambda: svc.events(0)["state"] in ("idle", "error"))
        self.assertEqual(svc.snapshot()["state"], "idle")

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        return entered, release

    def test_listener_overflow_discards_its_queue_and_preserves_other_listeners(self):
        entered, release = self.hold()
        slow_calls = []
        fast_calls = []
        received = threading.Condition()

        def slow_listener(event):
            slow_calls.append(event["kind"])
            entered.set()
            release.wait(5)

        def fast_listener(event):
            if event["kind"] not in ("user", "delta", "reply"):
                return
            with received:
                fast_calls.append(event["text"])
                received.notify_all()

        def stream(on_delta, **kwargs):
            if not entered.wait(5):
                raise TimeoutError("The fixture listener did not start.")
            for index in range(chat.LISTENER_QUEUE_LIMIT + 1):
                piece = f"{index} "
                on_delta(piece)
                with received:
                    if not received.wait_for(lambda: piece in fast_calls, timeout=5):
                        raise TimeoutError("The fixture listener did not receive a delta.")
            return reply("A fixture answer.")

        svc = self.service(Model(stream))
        svc.subscribe(slow_listener)
        svc.subscribe(fast_listener)
        try:
            with self.assertLogs("jarvis.v3.chat", level="WARNING") as output:
                svc.send("A fixture request.")
                self.finish(svc)
            self.assertEqual(output.records[0].getMessage(),
                             "A chat event listener overflowed and was removed.")
            with received:
                self.assertTrue(received.wait_for(lambda: fast_calls[-1] == "A fixture answer.", timeout=5))
                self.assertEqual(fast_calls, ["A fixture request.",
                                              *(f"{i} " for i in range(chat.LISTENER_QUEUE_LIMIT + 1)),
                                              "A fixture answer."])
        finally:
            release.set()
        self.assertEqual(slow_calls, ["user"])

    def test_brief_cap_resets_each_turn_and_posts_under_the_session(self):
        def posts(*labels):
            return reply(None, *(("brief_post", {"kind": "news", "ask": label}) for label in labels))

        svc = self.service(Model(posts("First brief", "Second brief"), posts("Third brief"),
                                 reply("Finished."), posts("Fourth brief"), reply("Finished again.")))
        svc.send("Post short test briefs.")
        self.finish(svc)
        errors = [event for event in svc.events(0)["events"]
                  if event["kind"] == "tool_end" and not event["ok"]]
        self.assertEqual(len(errors), 1)
        self.assertIn("Only two", errors[0]["summary"])
        svc.send("Post one more.")
        self.finish(svc)
        briefs = Feed(self.data / "feed").briefs()
        self.assertEqual({brief["ask"] for brief in briefs}, {"First brief", "Second brief", "Fourth brief"})
        self.assertTrue(all(brief["session"] == svc.snapshot()["session_id"] and brief["actor"] == "agent"
                            for brief in briefs))

    def test_vault_reads_leave_store_byte_identical(self):
        vault = Vault(self.data / "vault")
        vault.add("facts", "fixture", "Test entry", "The fixture is a telescope.", "Test")
        before = contents(vault.root)
        model = Model(reply(None, ("vault_list", {}), ("vault_read", {"name": "fixture"}),
                            ("vault_search", {"query": "telescope"})), reply("Done."))
        svc = self.service(model)
        svc.send("Read the fixture.")
        self.finish(svc)
        results = [m["content"] for m in model.seen[-1] if m["role"] == "tool"]
        self.assertEqual(len(results), 3)
        self.assertIn("fixture", results[0])
        self.assertTrue(all("telescope" in result for result in results[1:]))
        self.assertEqual(before, contents(vault.root))

    def test_chat_truncates_old_results_without_writing_shared_spills(self):
        calls = [("vault_read", {"name": "fixture"})] * 5
        model = Model(reply(None, *calls), reply("Done."))
        svc = self.service(model)
        spills = self.base / "shared-spills"
        with patch.object(MemoryTools, "memory_read", return_value="x" * 2500), \
             patch.object(config, "SPILL_DIR", spills):
            svc.send("Read long test results.")
            self.finish(svc)
        self.assertTrue(any(m.get("content", "").endswith(context.TRUNCATED)
                            for m in model.seen[-1] if m["role"] == "tool"))
        self.assertFalse(spills.exists())

    def test_calendar_is_conditional_and_lazy(self):
        # Isolate imports from other suites and v1's pre-existing integrations.
        code = """
import os
import sys
from pathlib import Path
from jarvis import config, llm
from jarvis.v3 import chat

root = Path(sys.argv[1])
os.umask(0o002)
config.GOOGLE_TOKEN_PATH = root / 'google_token.json'
seen = []
def fake(model, messages, **kwargs):
    seen.append({row['function']['name'] for row in kwargs['tools']})
    return llm.Reply(message={'content': 'Done.'}, finish_reason='stop', model='fake', latency_s=0)

svc = chat.ChatService(root / 'chats', completion=fake)
for state in ('missing', 'directory', 'file'):
    if state == 'directory':
        config.GOOGLE_TOKEN_PATH.mkdir()
    elif state == 'file':
        config.GOOGLE_TOKEN_PATH.rmdir()
        config.GOOGLE_TOKEN_PATH.write_text('fake-token')
    svc.send('Calendar test')
    svc._worker.join(10)
    assert svc.snapshot()['state'] == 'idle'
    expected = chat.CALENDAR_TOOLS if state == 'file' else set()
    assert seen[-1] & chat.CALENDAR_TOOLS == expected
    assert set(svc.snapshot()['tools']) & chat.CALENDAR_TOOLS == expected
    assert 'jarvis.v3.tools.calendar' not in sys.modules
    assert not any(name == 'google' or name.startswith('google.') for name in sys.modules)
assert svc.close()
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.base)], check=False,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        fastpath.register_tools()
        for name in chat.CALENDAR_TOOLS:
            fields = fastpath.tools.REGISTRY[name].schema["properties"]
            self.assertTrue(all(field.get("description") for field in fields.values()))

    def test_calendar_refuses_if_token_is_removed_mid_turn(self):
        self.token.write_text("fake-token", encoding="utf-8")

        def removed(**kwargs):
            self.token.unlink()
            return reply(None, ("calendar_list", {"start": "2030-01-01", "end": "2030-01-02"}))

        model = Model(removed, reply("Calendar is disconnected."))
        svc = self.service(model)
        svc.send("Read events.")
        self.finish(svc)
        self.assertIn("Google Calendar is not connected",
                      next(m["content"] for m in model.seen[-1] if m["role"] == "tool"))

    def test_leftover_steers_become_one_follow_up_turn(self):
        entered, release = self.hold()

        def first(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply("First answer.")

        model = Model(first, reply("Second answer."))
        svc = self.service(model)
        svc.send("First question.")
        self.assertTrue(entered.wait(5))
        self.assertTrue(svc.send("Second question.")["steered"])
        self.assertTrue(svc.send("Third question.")["steered"])
        release.set()
        self.finish(svc)
        self.assertEqual(len(model.seen), 2)
        self.assertIn({"role": "user", "content": "Second question.\n\nThird question."}, model.seen[1])
        events = svc.events(0)["events"]
        self.assertEqual([(e["kind"], e["text"]) for e in events if e["kind"] == "reply"],
                         [("reply", "First answer."), ("reply", "Second answer.")])
        followup = next(event for event in events if "inputs" in event)
        self.assertEqual(followup["inputs"], [
            {"seq": event["seq"], "text": event["text"]} for event in events if event["kind"] == "steer"
        ])
        snapshot = svc.snapshot()
        self.assertEqual(snapshot["replay"], {"history_prefix": 0, "events": events})
        self.assertEqual([row["t"] for row in snapshot["history"]],
                         [events[0]["t"], events[3]["t"], followup["t"], followup["t"], events[-1]["t"]])

    def test_event_history_is_scoped_to_the_selected_session(self):
        svc = self.service(Model(reply("Old answer."), reply("New answer.")))
        svc.send("Old question.")
        self.finish(svc)
        old = svc.snapshot()
        new_id = svc.new_session()["session_id"]
        self.assertNotEqual(new_id, old["session_id"])
        self.assertEqual(svc.events(old["seq"])["session_id"], new_id)
        self.assertEqual(svc.snapshot()["seq"], old["seq"])
        self.assertEqual(svc.snapshot()["replay"], {"history_prefix": 0, "events": []})
        svc.send("New question.")
        self.finish(svc)
        new = svc.events(0)
        self.assertEqual(new["session_id"], new_id)
        self.assertTrue(new["gap"])
        self.assertEqual([row["text"] for row in new["events"]], ["New question.", "New answer."])

    def test_replay_starts_at_a_whole_turn_after_event_gap(self):
        with patch.object(chat, "EVENT_LIMIT", 5):
            svc = self.service(Model(reply("Old answer."), reply(None, ("vault_list", {})), reply("New answer.")))
        svc.send("Old question.")
        self.finish(svc)
        svc.send("New question.")
        self.finish(svc)
        batch = svc.events(0)
        self.assertTrue(batch["gap"])
        self.assertEqual(batch["events"][0]["kind"], "reply")
        snapshot = svc.snapshot()
        self.assertEqual(snapshot["replay"], {"history_prefix": 2, "events": batch["events"][1:]})
        self.assertEqual([row["kind"] for row in snapshot["replay"]["events"]],
                         ["user", "tool_start", "tool_end", "reply"])

    def test_provider_error_drops_steers_before_a_retry(self):
        entered, release = self.hold()

        def fail(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture did not release completion")
            raise RuntimeError("Fake provider failed")

        model = Model(fail, reply("Recovered."))
        svc = self.service(model)
        svc.send("First question.")
        self.assertTrue(entered.wait(5))
        stale = "A steer queued during the failed request."
        self.assertTrue(svc.send(stale)["steered"])
        release.set()
        self.wait_for(lambda: svc.events(0)["state"] == "error")
        (error,) = [event for event in svc.events(0)["events"] if event["kind"] == "error"]
        self.assertEqual(error["dropped"], 1)
        (steer,) = [event for event in svc.events(0)["events"] if event["kind"] == "steer"]
        self.assertEqual(error["discarded"], [steer["seq"]])
        self.assertFalse(svc.send("Fresh question.")["steered"])
        self.finish(svc)
        users = [row["content"] for row in model.seen[-1] if row["role"] == "user"]
        self.assertIn("Fresh question.", users)
        self.assertNotIn(stale, "\n".join(users))
        self.assertNotIn(stale, [row["text"] for row in svc.snapshot()["history"]])

    def test_index_search_reads_committed_wal_during_ingest(self):
        writer = """
import sys
from jarvis.v3.index import Index

with Index(sys.argv[1]) as index:
    index.add("jarvis", "fixture", title="Committed title", ts=1,
              messages=[("user", "A seismograph measures motion.", 1)])
    with index.write_transaction() as db:
        db.execute("UPDATE docs SET title = 'Uncommitted title'")
        print("ready", flush=True)
        sys.stdin.readline()
"""
        path = self.data / "index.sqlite"
        with subprocess.Popen([sys.executable, "-c", writer, str(path)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as ingest_process:
            try:
                ready, _, _ = select.select([ingest_process.stdout], [], [], 5)
                self.assertTrue(ready, "The writer did not start its transaction.")
                self.assertEqual(ingest_process.stdout.readline().strip(), "ready")
                model = Model(reply(None, ("index_search", {"query": "seismograph"})),
                              reply("Found it."))
                svc = self.service(model)
                svc.send("Search the index.")
                self.finish(svc)
                result = next(row["content"] for row in model.seen[-1] if row["role"] == "tool")
                self.assertIn("A seismograph measures motion.", result)
                self.assertIn("Committed title", result)
                self.assertNotIn("Uncommitted title", result)
                self.assertTrue(path.with_name("index.sqlite-wal").is_file())
            finally:
                _, stderr = ingest_process.communicate(input="finish\n", timeout=5)
            self.assertEqual(ingest_process.returncode, 0, stderr)

    def test_step_wall_uses_v1_handoff_and_drops_the_context_block(self):
        steps = []

        def stuck(**kwargs):
            self.assertIs(runtime.approver(), fastpath.deny_all)
            steps.append(kwargs["tools"])
            return reply(None, ("get_datetime", {}))

        def handoff(**kwargs):
            self.assertFalse(kwargs.get("tools"))
            return reply("Handoff from the fake model.")

        model = Model(*([stuck] * config.FAST_STEPS), handoff)
        svc = self.service(model)
        svc.send("Try until the step budget.")
        self.finish(svc)
        self.assertEqual(len(steps), config.FAST_STEPS)
        self.assertIn(agent.HANDOFF_PROMPT, [m["content"] for m in model.seen[-1]])
        self.assertTrue(any(agent.working_context(messages) for messages in model.seen))
        self.assertIn("Handoff from the fake model.", svc.snapshot()["history"][-1]["text"])
        self.assertTrue(all(agent.CONTEXT_BLOCK_PREFIX.strip().encode() not in raw
                            for raw in contents(self.root).values()))

    def test_racing_sends_admit_one_worker_and_steer_the_other(self):
        entered, release = self.hold()
        gate = threading.Barrier(2, timeout=5)

        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply("First answer.")

        model = Model(blocked, reply("Follow-up answer."))
        svc = self.service(model)

        def send(text):
            gate.wait()
            return svc.send(text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(send, "Message one.")
            second = pool.submit(send, "Message two.")
            self.assertEqual({first.result(5)["steered"], second.result(5)["steered"]}, {False, True})
            self.assertTrue(entered.wait(5))
            self.assertEqual(len(model.seen), 1)
            release.set()
        self.finish(svc)
        self.assertIn(len(model.seen), (1, 2))
        delivered = json.dumps(model.seen)
        self.assertIn("Message one.", delivered)
        self.assertIn("Message two.", delivered)

    def test_event_ring_reports_overflow(self):
        with patch.object(chat, "EVENT_LIMIT", 3):
            svc = self.service(Model(reply("One."), reply("Two.")))
        for text in ("first", "second"):
            svc.send(text)
            self.finish(svc)
        events = svc.events(0)
        self.assertTrue(events["gap"])
        self.assertEqual([e["seq"] for e in events["events"]], [2, 3, 4])
        self.assertFalse(svc.events(1)["gap"])
        self.assertEqual(svc.events(4)["events"], [])

    def test_close_joins_a_tool_call(self):
        entered, release = self.hold()
        svc = self.service(Model(reply(None, ("vault_list", {}))))

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return "A fake vault result."

        with patch.object(MemoryTools, "memory_list", blocked), ThreadPoolExecutor(1) as pool:
            svc.send("List test entries.")
            self.assertTrue(entered.wait(5))
            closing = pool.submit(svc.close)
            try:
                self.wait_for(lambda: svc.events(0)["state"] == "stopping" or closing.done())
                self.assertFalse(closing.done())
            finally:
                release.set()
            self.assertTrue(closing.result(5))
        self.assertEqual([e["kind"] for e in svc.events(0)["events"]][-1], "stopped")

    def test_close_timeout_disables_late_writes(self):
        entered, release = self.hold()

        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply("This answer is too late.")

        svc = self.service(Model(blocked))
        svc.send("Wait for this reply.")
        self.assertTrue(entered.wait(5))
        try:
            self.assertFalse(svc.close(grace_s=0))
            before = contents(self.root)
        finally:
            release.set()
        self.assertTrue(svc.close())
        self.assertEqual(before, contents(self.root))

    def test_rejects_symlink_and_nonprivate_chats(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        self.root.symlink_to(target, target_is_directory=True)
        svc = self.service(Model())
        with self.assertRaises(chat.ChatStorageError):
            svc.send("Refuse the linked root.")
        self.assertEqual(list(target.iterdir()), [])
        self.root.unlink()
        self.root.mkdir(mode=0o755)
        with self.assertRaises(chat.ChatStorageError):
            svc.snapshot()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)

    def test_corrupt_session_is_preserved_and_replaced(self):
        svc = self.service(Model(reply("Before corruption.")))
        svc.send("Write a test turn.")
        self.finish(svc)
        folder = self.root / svc.snapshot()["session_id"]
        svc.close()
        (folder / "messages.json").write_text('{"not": "a transcript"}', encoding="utf-8")
        before = contents(folder)

        model = Model(reply("New session."))
        again = self.service(model)
        again.send("Start safely.")
        self.finish(again)
        self.assertNotEqual(again.snapshot()["session_id"], folder.name)
        self.assertEqual(contents(folder), before)
        self.assertNotIn("Write a test turn.", json.dumps(model.seen))

    def test_ingest_indexes_v3_chats_as_jarvis_and_idle_counts_activity(self):
        entered, release = self.hold()

        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError
            return reply("The fixture word is orrery.")

        svc = self.service(Model(blocked))
        svc.send("What is the fixture word?")
        self.assertTrue(entered.wait(5))
        try:
            self.assertEqual(svc.idle_seconds(), 0.0)
        finally:
            release.set()
        self.finish(svc)
        self.assertLess(jobs.idle_seconds(), 30)
        with Index(self.data / "index.sqlite") as index:
            stats = ingest(index, claude=self.base / "claude", codex=self.base / "codex",
                           jarvis=self.base / "v1")
            self.assertEqual(stats.files, 1)
            (hit,) = index.discover("orrery")
            self.assertEqual(hit["source"], "jarvis")

    def test_plain_compact_message_is_handled_by_v1(self):
        svc = self.service(Model())
        svc.send("/compact")
        self.finish(svc)
        self.assertEqual([m["text"] for m in svc.snapshot()["history"]],
                         ["/compact", "Compacted; saved 0 tokens."])

    def test_compact_a_long_chat_uses_the_injected_completion(self):
        model = Model(*[reply("An answer with some details to summarize.") for _ in range(12)],
                      reply("Test summary."))
        svc = self.service(model)
        for step in range(12):
            svc.send(f"Test message {step}.")
            self.finish(svc)
        svc.send("/compact")
        self.finish(svc)
        self.assertIn(agent.COMPACTION_PROMPT, model.seen[-1][0]["content"])
        self.assertIn(b"Test summary.", next(self.root.glob("*/messages.json")).read_bytes())


if __name__ == "__main__":
    unittest.main()

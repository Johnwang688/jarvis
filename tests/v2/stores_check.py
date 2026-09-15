"""Free store and migration checks; every filesystem write stays in a temp dir.

Run: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/johnw/projects/Jarvis/.venv/bin/python tests/v2/stores_check.py
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import config, context, sessions
from jarvis.v2 import model as m
from jarvis.v2 import migrate as migrate_mod
from jarvis.v2.migrate import migrate_v1_sessions
from jarvis.v2.stores import StoreError, Stores


def refused(call, contains: str = "") -> None:
    try:
        call()
    except StoreError as exc:
        assert contains in str(exc), str(exc)
    else:
        raise AssertionError("expected StoreError")


def chat(stores: Stores, **values) -> m.Thread:
    return stores.threads.create("01234567", m.Role.CHAT, m.ProviderName.FAST, **values)


def round_trip(root: Path) -> None:
    stores = Stores(root)
    project = stores.projects.create(
        "Workshop", root / "repo", profile=m.PermissionProfile.STRICT,
        routing=m.Routing(chains={"implementer": ["codex", "claude"]},
                          models={"implementer": {"codex": "model/high"}}, no_new_work=0.85),
        discord_channel_id="channel", extra_dirs=["/tmp"], always_ask=["publish"],
    )
    thread = stores.threads.create(
        project.id, m.Role.IMPLEMENTER, m.ProviderName.CODEX,
        provider_session_id="codex:remote", title="Unicode ✓", turns=2,
        cost_usd=0.75, tokens=1234, model="model", effort="high", migrated_from="old-thread",
    )
    task = stores.tasks.create(
        project.id, "Build it", spec=m.Spec(
            goal="Ship", deliverable="patch", acceptance=["green"], constraints=["local"],
            questions=[m.OpenQuestion("Where?", True, ["here"], answer="here"),
                       m.OpenQuestion("Color?", False, assumed="blue")]),
        plan=["build", "check"], status=m.Status(
            step=1, steps=2, started=m.utcnow(), elapsed_s=3.5, cost_usd=0.75, tokens=1234,
            last_tool="shell", last_file="a.py", open_question="Ready?",
            routing=[m.RoutingDecision(m.Role.IMPLEMENTER, m.ProviderName.CODEX, "project table")]),
        report=m.Report(done=["built"], changed=["a.py"], verified="suite", open=["review"],
                        next="merge", cost={"codex": 0.75}),
        thread_ids=[thread.id], worktree="/tmp/tree", branch="work", discord_thread_id="discord",
        profile=m.PermissionProfile.ASK, provider_override=m.ProviderName.CLAUDE,
        ceilings={"usd": 2.0}, migrated_from="old-goal",
    )
    thread.task_id = task.id
    stores.threads.save(thread)
    stores.tasks.save(task)
    fresh = Stores(root)
    for name, obj in (("projects", project), ("threads", thread), ("tasks", task)):
        store = getattr(fresh, name)
        assert store.get(obj.id) == obj
        assert re.fullmatch("[0-9a-f]{8}", obj.id)
        assert store.list(id=obj.id) == [obj]
        assert store.list(id="ffffffff") == []
        assert store.get("ffffffff") is None
    loaded = fresh.tasks.get(task.id)
    assert isinstance(loaded.spec.questions[0], m.OpenQuestion)
    assert isinstance(loaded.status.routing[0], m.RoutingDecision)
    assert isinstance(loaded.report, m.Report)
    loaded.plan.append("detached")
    assert fresh.tasks.get(task.id).plan == task.plan
    assert fresh.threads.list(project_id=project.id, provider=m.ProviderName.CODEX) == [thread]
    refused(lambda: fresh.tasks.list(typo="value"), "filters")
    print("ok  round-trip: all domain dataclasses, enums, usage fields, filters, detached reads")


def lazy_and_ids(root: Path) -> None:
    stores = Stores(root)
    with patch("jarvis.v2.stores.secrets.token_hex", side_effect=["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"]):
        first = chat(stores)
        second = chat(stores)
    task = stores.tasks.create("01234567", "unused")
    assert first.id != second.id
    assert not root.exists(), "bare create touched disk"
    assert stores.threads.list() == [] and stores.tasks.list() == []
    assert stores.threads.read_log(first.id) == [] and stores.tasks.read_journal(task.id) == []
    assert not root.exists()
    stores.threads.save(first)
    with patch("jarvis.v2.stores.secrets.token_hex", side_effect=[first.id, "cccccccc"]):
        assert chat(stores).id == "cccccccc"
    refused(lambda: chat(stores, id="dddddddd"), "minted")
    refused(lambda: stores.threads.get("../escape"), "Invalid")
    print("ok  lazy creation: no empty directories, ids collision checked in memory and on disk")


def inbox_and_atomic(root: Path) -> None:
    stores = Stores(root)
    inbox = stores.projects.inbox()
    assert inbox.inbox and inbox.name == "Inbox" and inbox.root == str(Path.home())
    assert Stores(root).projects.inbox() == inbox
    refused(lambda: stores.projects.create("Inbox", str(Path.home()), inbox=True), "already exists")
    refused(lambda: stores.projects.save(replace(inbox, inbox=False)), "cannot")
    refused(lambda: stores.projects.save(replace(inbox, name="Elsewhere")), "named Inbox")
    assert len(stores.projects.list(inbox=True)) == 1
    project = stores.projects.create("Old", str(root))
    path = stores.projects.path(project.id)
    original = path.read_bytes()
    project.name = "New"
    with patch("jarvis.v2.stores.os.replace", side_effect=OSError("simulated disk failure")):
        refused(lambda: stores.projects.save(project), str(path))
    assert path.read_bytes() == original
    stores.projects.save(project)
    assert stores.projects.get(project.id).name == "New"
    assert not list(root.rglob("*.tmp")), "atomic writer left temporary files"
    print("ok  inbox singleton; atomic replacement preserves old data on failure and cleans temps")


def transitions_and_delete(root: Path) -> None:
    stores = Stores(root)
    task = stores.tasks.create("01234567", "Walk the lifecycle")
    refused(lambda: stores.tasks.create(task.project_id, "skip", state=m.TaskState.DONE), "intake")
    refused(lambda: stores.tasks.save(replace(task, state=m.TaskState.DONE)), "transition")
    refused(lambda: stores.tasks.transition(task.id, m.TaskState.DONE), "intake -> done")
    refused(lambda: stores.tasks.delete(task.id), "terminal")
    previous = task.updated
    for state in (m.TaskState.CLARIFYING, m.TaskState.PLANNED, m.TaskState.RUNNING,
                  m.TaskState.VERIFYING, m.TaskState.RUNNING, m.TaskState.BLOCKED,
                  m.TaskState.RUNNING, m.TaskState.VERIFYING, m.TaskState.DONE):
        task = stores.tasks.transition(task.id, state, reason="test move")
        assert task.state == state and task.status.phase == state
        assert task.updated > previous
        previous = task.updated
    entries = stores.tasks.read_journal(task.id)
    assert len(entries) == 9 and all(e["reason"] == "test move" for e in entries)
    assert entries[0]["old_state"] == "intake" and entries[-1]["new_state"] == "done"
    refused(lambda: stores.tasks.transition(task.id, m.TaskState.RUNNING), "done -> running")
    stale = replace(task, state=m.TaskState.INTAKE)
    refused(lambda: stores.tasks.save(stale), "transition")
    stores.tasks.delete(task.id)
    assert stores.tasks.get(task.id) is None and not stores.tasks.path(task.id).parent.exists()
    for state in (m.TaskState.CANCELLED, m.TaskState.FAILED):
        task = stores.tasks.create("01234567", "Terminal")
        if state == m.TaskState.FAILED:
            for step in (m.TaskState.CLARIFYING, m.TaskState.BLOCKED):
                stores.tasks.transition(task.id, step)
            refused(lambda: stores.tasks.delete(task.id), "terminal")
        stores.tasks.transition(task.id, state)
        stores.tasks.delete(task.id)
    thread = chat(stores, task_id="11111111")
    refused(lambda: stores.threads.delete(thread.id), "task")
    standalone = chat(stores)
    stores.threads.save(standalone)
    linked = stores.tasks.create("01234567", "Reverse link", thread_ids=[standalone.id])
    refused(lambda: stores.threads.delete(standalone.id), "task")
    stores.tasks.save(linked)
    refused(lambda: stores.threads.delete(standalone.id), "task")
    stores.tasks.transition(linked.id, m.TaskState.CANCELLED)
    stores.tasks.delete(linked.id)
    stores.threads.delete(standalone.id)
    assert stores.threads.get(standalone.id) is None
    empty = chat(stores)
    stores.threads.delete(empty.id)
    refused(lambda: stores.projects.delete("01234567"), "cannot")
    print("ok  transitions: full lifecycle, retry/block paths, state bypass refused, delete rules")


def all_transition_edges(root: Path) -> None:
    stores = Stores(root)
    routes = {
        m.TaskState.INTAKE: [], m.TaskState.CLARIFYING: [m.TaskState.CLARIFYING],
        m.TaskState.PLANNED: [m.TaskState.CLARIFYING, m.TaskState.PLANNED],
        m.TaskState.RUNNING: [m.TaskState.CLARIFYING, m.TaskState.PLANNED, m.TaskState.RUNNING],
        m.TaskState.BLOCKED: [m.TaskState.CLARIFYING, m.TaskState.BLOCKED],
        m.TaskState.CANCELLED: [m.TaskState.CANCELLED],
        m.TaskState.FAILED: [m.TaskState.CLARIFYING, m.TaskState.BLOCKED, m.TaskState.FAILED],
    }
    routes[m.TaskState.VERIFYING] = routes[m.TaskState.RUNNING] + [m.TaskState.VERIFYING]
    routes[m.TaskState.DONE] = routes[m.TaskState.VERIFYING] + [m.TaskState.DONE]
    for source, route in routes.items():
        for destination in m.TaskState:
            task = stores.tasks.create("01234567", "Transition edge")
            for step in route:
                stores.tasks.transition(task.id, step)
            before = stores.tasks.get(task.id)
            journal = stores.tasks.read_journal(task.id)
            if destination in m.TRANSITIONS[source]:
                assert stores.tasks.transition(task.id, destination).state == destination
            else:
                refused(lambda: stores.tasks.transition(task.id, destination), "illegal")
                assert stores.tasks.get(task.id) == before
                assert stores.tasks.read_journal(task.id) == journal
    print("ok  transition matrix: all 81 state pairs, refusals leave task and journal unchanged")


def append_and_corruption(root: Path) -> None:
    stores = Stores(root)
    thread, task = chat(stores), stores.tasks.create("01234567", "Journal")
    for index in range(3):
        stores.threads.log(thread.id, "you", f"question {index}\n✓")
        stores.tasks.journal(task.id, "step", index=index, details={"ok": True})
    assert [entry["text"] for entry in stores.threads.read_log(thread.id)] == [f"question {i}\n✓" for i in range(3)]
    assert [entry["index"] for entry in stores.tasks.read_journal(task.id)] == [0, 1, 2]
    assert all(set(entry) == {"t", "role", "text"} for entry in stores.threads.read_log(thread.id))
    assert all(entry["event"] == "step" and entry["at"] for entry in stores.tasks.read_journal(task.id))
    assert Stores(root).tasks.get(task.id) == task
    path = stores.tasks.path(task.id)
    good = path.read_bytes()
    for bad in (b"{broken", b"[]", b"null", b'{}',
                json.dumps({**m.to_json(task), "spec": {"questions": [None]}}).encode(),
                json.dumps({**m.to_json(task), "status": []}).encode(),
                json.dumps({**m.to_json(task), "state": "bogus"}).encode(),
                json.dumps({**m.to_json(task), "id": "ffffffff"}).encode()):
        path.write_bytes(bad)
        refused(lambda: stores.tasks.get(task.id), str(path))
        refused(lambda: stores.tasks.list(), str(path))
    path.write_bytes(good)
    log = stores.threads.path(thread.id).with_name("log.jsonl")
    with log.open("a") as handle:
        handle.write("[]\n")
    refused(lambda: stores.threads.read_log(thread.id), str(log))
    print("ok  append/read order; malformed JSON, nested objects, enums and logs fail with paths")


def hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def migration(root: Path) -> None:
    fixture = root / "fixture"
    with patch.object(config, "SESSIONS_DIR", fixture), patch.object(sessions, "AUTO_TITLE", False):
        session = sessions.new("chat")
        messages = [{"role": "system", "content": "fresh system"},
                    {"role": "user", "content": "first saved message"},
                    {"role": "assistant", "content": "answer"}]
        session.record("first question ✓", SimpleNamespace(text="answer", cost_usd=0.0123), messages)
        session.record("second question", SimpleNamespace(text="reply", cost_usd=0.0045), messages)
        # Simulate an older transcript which still has live image content.
        messages = session.restore_messages()
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET_IMAGE"}},
            {"type": "input_image", "image_url": "SECRET_IMAGE"},
            {"type": "image", "source": {"data": "SECRET_IMAGE"}},
        ]})
        (session.path / "messages.json").write_text(json.dumps(messages), encoding="utf-8")
        # Preserve whitespace, CRLF and a missing final newline exactly.
        log_path = session.path / "log.jsonl"
        log_path.write_bytes(log_path.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
        meta = dict(session.meta)
    source = root / "v1-copy"
    shutil.copytree(fixture, source)
    bad = source / "aaa-malformed"
    bad.mkdir()
    (bad / "meta.json").write_text("{", encoding="utf-8")
    bad_transcript = source / "bbb-malformed-transcript"
    shutil.copytree(source / session.id, bad_transcript)
    (bad_transcript / "messages.json").write_text("{}", encoding="utf-8")
    before = hashes(source)
    stores = Stores(root / "v2")
    output = io.StringIO()
    with redirect_stdout(output):
        ids = migrate_v1_sessions(source, stores)
    assert len(ids) == 1 and output.getvalue().count("Warning:") == 2
    thread = stores.threads.get(ids[0])
    assert thread.project_id == stores.projects.inbox().id
    assert thread.role == m.Role.CHAT and thread.provider == m.ProviderName.FAST
    assert thread.provider_session_id is None and thread.migrated_from == session.id
    assert (thread.title, thread.turns, thread.cost_usd) == (meta["title"], meta["turns"], meta["cost_usd"])
    assert abs(datetime.fromisoformat(thread.created).timestamp() - meta["created"]) < 1e-6
    assert abs(datetime.fromisoformat(thread.updated).timestamp() - meta["updated"]) < 1e-6
    dest = stores.threads.path(thread.id).parent
    assert (dest / "log.jsonl").read_bytes() == (source / session.id / "log.jsonl").read_bytes()
    transcript = (dest / "v1_messages.json").read_text(encoding="utf-8")
    assert "SECRET_IMAGE" not in transcript and context.EVICTED_IMAGE in transcript
    assert json.loads(transcript)[0] == messages[0], "migration dropped first saved message"
    with redirect_stdout(output):
        assert migrate_v1_sessions(source, Stores(stores.root)) == []
    assert hashes(source) == before, "migration changed v1 bytes"
    assert len(stores.threads.list()) == 1
    assert not list(stores.root.rglob("*.tmp"))
    stores.threads.log(thread.id, "you", "after migration")
    entries = stores.threads.read_log(thread.id)
    assert len(entries) == 5 and entries[-1]["text"] == "after migration"
    refused(lambda: migrate_v1_sessions(source, Stores(source / "unsafe-output")), "outside")
    assert hashes(source) == before
    failed = Stores(root / "failed-import")
    write = migrate_mod._write_bytes

    def fail_transcript(path: Path, data: bytes) -> None:
        if path.name == "v1_messages.json":
            raise StoreError(f"{path}: simulated copy failure")
        write(path, data)

    with patch.object(migrate_mod, "_write_bytes", side_effect=fail_transcript), redirect_stdout(output):
        assert migrate_v1_sessions(source, failed) == []
    assert failed.threads.list() == []
    assert not list((failed.root / "threads").iterdir())
    with redirect_stdout(output):
        assert len(migrate_v1_sessions(source, failed)) == 1
    assert hashes(source) == before
    print("ok  migration: copied v1 fixture, metadata, verbatim log, images stripped, malformed skipped")
    print("ok  migration: rerun idempotent, source hashes unchanged, destination outside v1 enforced")
    print("ok  migration: failed copy leaves no partial thread and can be retried")


def main() -> int:
    for check in (round_trip, lazy_and_ids, inbox_and_atomic, transitions_and_delete,
                  all_transition_edges, append_and_corruption, migration):
        with tempfile.TemporaryDirectory() as tmp:
            check(Path(tmp) / "data")
    print("\nall v2 store checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

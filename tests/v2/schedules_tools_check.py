"""WP12c: real registry dispatch, loopback API and scheduler; no paid providers."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config, runtime, tools
from jarvis.v2.tools import schedules as schedule_tools
from jarvis.v2.daemon import Daemon
from jarvis.v2.model import Role, ProviderName, TaskState
from jarvis.v2.stores import Stores


class ScheduleTools(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        guard = patch.object(config, "V2_DATA_DIR", self.root / "data")
        guard.start()
        self.addCleanup(guard.stop)
        self.stores = Stores()
        self.project = self.stores.projects.create("Example", str(self.root))
        self.daemon = Daemon(self.stores, {}, None, 0)
        self.started = []

        class Runner:
            def start(_, task_id):
                self.started.append(task_id)
                self.stores.tasks.transition(task_id, TaskState.CLARIFYING)

        self.daemon.runner = Runner()
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        guard = patch.object(config, "DAEMON_PORT", self.daemon.port)
        guard.start()
        self.addCleanup(guard.stop)
        self.events = self.daemon.bus.subscribe()

    def dispatch(self, name, **arguments):
        return tools.dispatch(name, json.dumps(arguments),
                              approve=lambda *_: self.fail("schedule tools are not dangerous")).text

    def create(self, **kw):
        return json.loads(self.dispatch("schedule_create", **(dict(brief="Check the project", when="every 15 minutes") | kw)))

    def test_dispatch_crud_shared_store_events_and_scheduler(self):
        made = self.create(project="eXample")
        self.assertEqual(made["project_id"], self.project.id)
        self.assertEqual(made["every_s"], 900)
        # `describe` is computed on the way out and never stored.
        self.assertEqual(made["describe"], "every 15 minutes")
        stored = {k: v for k, v in made.items() if k != "describe"}
        self.assertEqual(self.daemon.schedules.get(made["id"]), stored)
        self.assertEqual(json.loads(self.dispatch("schedule_list")), [made])
        event = self.events.get(timeout=1)
        self.assertEqual(event["kind"], "schedule_created")
        self.assertEqual(event["data"], {"schedule_id": made["id"]})
        self.daemon.schedules.clock = lambda: datetime.fromisoformat(made["next_run_at"]).timestamp()
        self.daemon.schedules.tick()
        fired = self.daemon.schedules.get(made["id"])
        self.assertEqual(self.started, [fired["last_task_id"]])
        task = self.stores.tasks.get(fired["last_task_id"])
        self.assertEqual(task.project_id, self.project.id)
        self.assertEqual(task.brief, made["brief"])
        self.assertEqual(task.state, TaskState.CLARIFYING)
        events = [self.events.get(timeout=1) for _ in range(2)]
        self.assertEqual({e["kind"] for e in events}, {"task_created", "schedule_fired"})
        self.daemon.schedules.save({"brief": "Changed by HTTP's store method"}, made["id"])
        self.assertEqual(self.events.get(timeout=1)["kind"], "schedule_updated")
        self.assertEqual(json.loads(self.dispatch("schedule_delete", id=made["id"])), {"ok": True})
        self.assertEqual(self.events.get(timeout=1)["kind"], "schedule_deleted")
        self.assertEqual(json.loads(self.dispatch("schedule_list")), [])
        self.assertIsNotNone(self.stores.tasks.get(task.id))
        self.assertTrue(self.dispatch("schedule_delete", id=made["id"]).startswith("Error:"))

    def test_project_resolution_id_inbox_and_optional_runtime(self):
        self.assertEqual(self.create(project=self.project.id, when="mondays at 10")["cron"], "0 10 * * 1")
        self.assertTrue(self.stores.projects.get(self.create()["project_id"]).inbox)
        thread = self.stores.threads.create(self.project.id, Role.CHAT, ProviderName.FAST)
        self.stores.threads.save(thread)
        slot = ContextVar("test_thread", default=None)
        with patch.object(runtime, "thread_id", slot.get, create=True):
            token = slot.set(thread.id)
            try:
                self.assertEqual(self.create()["project_id"], self.project.id)
            finally:
                slot.reset(token)
            self.assertTrue(self.stores.projects.get(self.create()["project_id"]).inbox)
        with patch.object(runtime, "project_id", lambda: self.project.id, create=True):
            self.assertEqual(self.create()["project_id"], self.project.id)
        with patch.object(runtime, "thread_id", lambda: "deadbeef", create=True):
            self.assertIn("calling thread not found", self.dispatch("schedule_create", brief="x", when="every hour"))
        self.stores.projects.create("EXAMPLE", str(self.root))
        self.assertIn("ambiguous", self.dispatch("schedule_create", brief="x", when="every hour", project="example"))
        self.assertEqual(self.create(project=self.project.id)["project_id"], self.project.id)

    def test_refusals_do_not_create_schedules(self):
        for when in ("tomorrow morning", "every few hours", "weekdays at 25:00", "every 0 minutes"):
            result = self.dispatch("schedule_create", brief="x", when=when)
            self.assertTrue(result.startswith("Error:"), result)
            self.assertIn("Accepted forms:", result)
        for arguments in (dict(brief="", when="every hour"), dict(brief="x", when="every hour", project="missing"),
                          dict(brief="x", when="every hour", project=3)):
            self.assertTrue(self.dispatch("schedule_create", **arguments).startswith("Error:"))
        for bad in ("../projects", "", 3):
            self.assertIn("invalid schedule id", self.dispatch("schedule_delete", id=bad))
        self.assertEqual(json.loads(self.dispatch("schedule_list")), [])

    def test_schemas_and_toolset_validation_in_fresh_processes(self):
        names = {"schedule_create", "schedule_list", "schedule_delete"}
        for name in names:
            entry = tools.REGISTRY[name]
            self.assertFalse(entry.dangerous)
            self.assertFalse(entry.schema["additionalProperties"])
            for prop in entry.schema["properties"].values():
                self.assertEqual(prop["type"], "string")
                self.assertTrue(prop["description"])
        self.assertEqual(tools.REGISTRY["schedule_create"].schema["required"], ["brief", "when"])
        for module, toolset in (("providers.fastpath", "FAST_TOOLS"), ("mcp", "MCP_TOOLS")):
            code = (f"from jarvis.v2.{module} import {toolset}; "
                    f"from jarvis import tools; assert {names!r} <= {toolset}; "
                    f"assert {toolset} <= tools.REGISTRY.keys()")
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

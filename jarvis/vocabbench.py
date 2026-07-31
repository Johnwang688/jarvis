"""vocab-bench: a seeded browser drill as a second bench task family.

Unlike bench.py's canned tools, these tasks drive the *real* browser against a
local, deterministic vocabulary drill (vocabbench/index.html). The seed fixes
the question sequence, so every model faces identical work and runs are
directly comparable — including snapshot (text) vs vision runs of the same
seed.

Scoring is read from the page itself (window.__vocabResults) after the agent
finishes; the agent has no way to see or set it, since it has no JS-eval tool.

Runs are headless by default (it is a batch benchmark); set
JARVIS_BROWSER_HEADLESS=0 to watch a run on the desktop.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import agent as agent_mod
from . import config
from .bench import TaskResult
from .browser import SESSION

PAGE_DIR = config.REPO_ROOT / "vocabbench"
PORT = int(os.environ.get("VOCAB_PORT", "8321"))

SYSTEM = (
    "You are completing a browser-based vocabulary drill. Work steadily and "
    "accurately using the browser tools. The page re-renders after every "
    "action, so take a fresh snapshot or screenshot before acting on element "
    "refs."
)

SNAPSHOT_TOOLS = ["browser_goto", "browser_snapshot", "browser_click", "browser_type"]
# No browser_snapshot: perception is screenshots only, which is what makes the
# text-vs-vision comparison honest. Marked screenshots carry the refs.
VISION_TOOLS = ["browser_goto", "browser_screenshot", "browser_click", "browser_type"]


@dataclass
class VocabTask:
    name: str
    tests: str
    mode: str  # "snapshot" | "vision"
    seed: int = 42
    n: int = 6
    time_s: int = 420
    idle_s: int = 60
    max_steps: int = 40

    @property
    def url(self) -> str:
        return (
            f"http://localhost:{PORT}/?seed={self.seed}&n={self.n}"
            f"&time={self.time_s}&idle={self.idle_s}"
        )

    def prompt(self) -> str:
        base = (
            f"Open {self.url} and complete the vocabulary drill: click Start, "
            "answer every question, and continue until the results screen "
            "appears. Question types: multiple choice (click an option), "
            "fill-in-the-blank and spelling (type the answer), and matching "
            "(click a word, then click its definition, until all are paired). "
            "If an 'Are you still there?' overlay appears, click Resume and "
            "continue. Stop at the results screen and report your score."
        )
        if self.mode == "vision":
            base += (
                " Read the page from screenshots: each interactive element is "
                "labeled with a small yellow ref badge (e.g. e7). Use those "
                "refs with browser_click and browser_type, and take a fresh "
                "screenshot after every action — refs change when the page does."
            )
        return base


TASKS: list[VocabTask] = [
    VocabTask("vocab-snap", "full drill via text snapshots", mode="snapshot"),
    # Vision needs more steps: the seed-42 Luna run finished at 39.
    VocabTask("vocab-vision", "same seed, marked screenshots only", mode="vision", max_steps=60),
]

# Luna only by default: the owner routes work through it unless a comparison
# is explicitly requested (see CLAUDE.md model routing policy). Pass model ids
# on the command line to sweep others.
DEFAULT_ROSTER = [
    "openai/gpt-5.6-luna",
]


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT), partial(_QuietHandler, directory=str(PAGE_DIR))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_task(model: str, task: VocabTask) -> TaskResult:
    result = TaskResult(task=task.name, passed=False)
    server = _serve()

    # Batch default is headless; JARVIS_BROWSER_HEADLESS=0 forces a headed run.
    SESSION.stop()  # fresh context and action budget per run
    SESSION.policy.headless = os.environ.get("JARVIS_BROWSER_HEADLESS", "1") == "1"
    # Hermetic: a bench run must not wander onto the live web.
    SESSION.policy.allowed_hosts = ["localhost", "127.0.0.1"]

    tools = SNAPSHOT_TOOLS if task.mode == "snapshot" else VISION_TOOLS
    jarvis = agent_mod.Agent(
        model=model, system=SYSTEM, tool_names=tools, max_steps=task.max_steps
    )

    try:
        turn = jarvis.run_turn(task.prompt())
        result.text = turn.text
        result.cost_usd = turn.cost_usd
        result.latency_s = turn.latency_s
        result.calls = [(name, {}) for name, _ in turn.tool_calls]

        raw = SESSION.eval_js(
            "() => window.__vocabResults ? JSON.stringify(window.__vocabResults) : ''"
        )
        data = json.loads(raw) if raw else None
        if data is None:
            result.detail = f"no results after {turn.steps} steps"
            result.error = "never reached the results screen"
        else:
            result.passed = data["score"] == task.n and not data["timedOut"]
            result.detail = (
                f"{data['score']}/{task.n} correct · {data['idleEvents']} idle · "
                f"{turn.steps} steps"
                + (" · timed out" if data["timedOut"] else "")
            )
        if turn.stopped_early:
            result.error = (result.error + " · " if result.error else "") + "hit step cap"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        SESSION.stop(trace_name=f"{task.name}-{model.rsplit('/', 1)[-1]}")
        server.shutdown()
        server.server_close()  # shutdown() alone leaves the port bound

    return result

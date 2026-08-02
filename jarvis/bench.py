"""Tool-calling stress test for cheap models.

Cheap models rarely fail at writing prose — they fail at picking the right
tool, formatting arguments, chaining steps, and knowing when *not* to call a
tool. This runs the same battery against any set of models and reports pass
rate, cost, and latency, so a model earns real tasks instead of being trusted.

Tools here are fakes with canned results: no filesystem, no network, no cost
beyond tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import config, llm

Call = tuple[str, dict]

BENCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Get the current local date and time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from disk.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient address"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Delivery priority",
                    },
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar",
            "description": "Search calendar events in a date range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "ISO date, e.g. 2026-07-30"},
                    "end": {"type": "string", "description": "ISO date"},
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
        },
    },
]


def _canned(name: str, args: dict, attempt: int) -> str:
    """Fake tool results. `attempt` counts prior calls to the same tool."""
    if name == "get_datetime":
        return "Thursday, 30 July 2026 at 14:05:00 CDT"
    if name == "list_dir":
        return "notes/\nbudget-2026.csv  (2048B)\nreadme.md  (512B)"
    if name == "read_file":
        path = str(args.get("path", ""))
        # The recovery task: the obvious path fails once, with a hint.
        if path.endswith("budget.csv"):
            return "Error: /home/u/budget.csv does not exist. Did you mean budget-2026.csv?"
        if "budget-2026.csv" in path:
            return "month,spend\njan,420\nfeb,510\nmar,395"
        return "the quick brown fox"
    if name == "get_weather":
        return f"{args.get('city', '?')}: 24C, clear"
    if name == "send_email":
        return "queued"
    if name == "search_calendar":
        return "2026-08-02 CS class project demo\n2026-08-04 dentist"
    return "ok"


@dataclass
class Task:
    name: str
    prompt: str
    check: Callable[[list[Call], str], bool]
    tests: str
    max_steps: int = 6


def _names(calls: list[Call]) -> list[str]:
    return [c[0] for c in calls]


TASKS: list[Task] = [
    Task(
        name="abstain",
        prompt="What is the capital of France? Just tell me, don't look anything up.",
        tests="knows when NOT to call a tool",
        check=lambda calls, text: not calls and "paris" in text.lower(),
    ),
    Task(
        name="single",
        prompt="What time is it right now?",
        tests="one no-arg call",
        check=lambda calls, text: (
            _names(calls) == ["get_datetime"]
            and ("14:05" in text or "2:05" in text)  # models may reformat to 12h
        ),
    ),
    Task(
        name="args",
        prompt="Read the file at ~/notes/todo.md and tell me what it says.",
        tests="passes a string argument correctly",
        check=lambda calls, text: (
            _names(calls) == ["read_file"] and calls[0][1].get("path", "").endswith("todo.md")
        ),
    ),
    Task(
        name="decoy",
        prompt="Do I have anything on my calendar between 2026-08-01 and 2026-08-05?",
        tests="picks the right tool from six",
        check=lambda calls, text: (
            _names(calls) == ["search_calendar"] and "dentist" in text.lower()
        ),
    ),
    Task(
        name="parallel",
        prompt="Compare the weather in Austin and Seattle right now.",
        tests="two independent calls, ideally in one turn",
        check=lambda calls, text: (
            sorted(_names(calls)) == ["get_weather", "get_weather"]
            and {c[1].get("city", "").lower() for c in calls} == {"austin", "seattle"}
        ),
    ),
    Task(
        name="chain",
        prompt="Look in ~/docs, find the budget spreadsheet, and tell me what I spent in February.",
        tests="uses one tool's output as the next tool's input",
        check=lambda calls, text: (
            _names(calls)[:2] == ["list_dir", "read_file"] and "510" in text
        ),
    ),
    Task(
        name="recovery",
        prompt="Read ~/docs/budget.csv and tell me the January spend.",
        tests="recovers from a tool error instead of giving up",
        check=lambda calls, text: (
            len(calls) >= 2
            and _names(calls)[0] == "read_file"
            and any("budget-2026" in str(c[1].get("path", "")) for c in calls[1:])
            and "420" in text
        ),
    ),
    Task(
        name="enum",
        prompt=(
            "Send an urgent email to prof@tamu.edu with the subject 'Project extension' "
            "asking for two more days. Mark it high priority."
        ),
        tests="fills an enum field and multiple required args",
        check=lambda calls, text: (
            _names(calls) == ["send_email"]
            and calls[0][1].get("priority") == "high"
            and calls[0][1].get("to") == "prof@tamu.edu"
            and bool(calls[0][1].get("body"))
        ),
    ),
]


@dataclass
class TaskResult:
    task: str
    passed: bool
    calls: list[Call] = field(default_factory=list)
    text: str = ""
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str = ""
    detail: str = ""  # optional summary for the results table, e.g. "5/6 correct"
    # Per-check results, for families that grade several properties per task
    # (agentbench.Check). Empty for pass/fail families.
    checks: list = field(default_factory=list)


def run_task(model: str, task: Task) -> TaskResult:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant with tools. Use them when they are "
                "needed and answer directly when they are not."
            ),
        },
        {"role": "user", "content": task.prompt},
    ]
    calls: list[Call] = []
    seen: dict[str, int] = {}
    result = TaskResult(task=task.name, passed=False)

    try:
        for _ in range(task.max_steps):
            reply = llm.chat(model, messages, tools=BENCH_TOOLS, max_tokens=2048)
            result.cost_usd += reply.cost_usd
            result.latency_s += reply.latency_s

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.message.get("content"),
                    **({"tool_calls": reply.tool_calls} if reply.tool_calls else {}),
                }
            )

            if not reply.tool_calls:
                result.text = reply.text
                break

            for call in reply.tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments were not an object")
                except Exception as exc:
                    args = {}
                    result.error = f"bad tool arguments: {exc}"

                calls.append((name, args))
                attempt = seen.get(name, 0)
                seen[name] = attempt + 1

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": _canned(name, args, attempt),
                    }
                )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    result.calls = calls
    try:
        result.passed = bool(task.check(calls, result.text))
    except Exception:
        result.passed = False
    return result


def run_model(model: str, tasks: list[Task] | None = None) -> list[TaskResult]:
    return [run_task(model, t) for t in (tasks or TASKS)]


DEFAULT_ROSTER = [
    "openai/gpt-5.6-luna",
    "openai/gpt-oss-20b",
    "qwen/qwen3.7-flash",
    "google/gemma-3-12b-it",
    "meta-llama/llama-3.1-8b-instruct",
    "mistralai/mistral-nemo",
]

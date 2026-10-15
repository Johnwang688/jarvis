"""Replays the SV-0 Claude recordings (`fixtures/subagents/claude/*.jsonl`)
through the real `ClaudeProvider`, for the sub-agent checks (SV-1).

`ReplayClient` is everything `ClaudeSDKClient` is to the provider: each `cli`
row is parsed with the installed SDK's own `parse_message` (a hand-built
stand-in would keep passing after the SDK renamed a field), each recorded
PreToolUse `hook` row is run through the provider's real hook — `agent_id`
included, so the gate sees a sub-agent's call as it did live — and the
harness rows (the recorder's own `stop_task`, `interrupt`) are skipped: what
they caused is in the recording. A replayed user echo takes the uuid of the
query it echoes, in order, so the provider's turn ownership works as live.
Free and offline: no process, no network, no model.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from pathlib import Path
from unittest.mock import patch

from claude_agent_sdk._internal.message_parser import parse_message

from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread
from jarvis.v2.provider import Brief, Decision
from jarvis.v2.providers import claude

FIX = Path(__file__).resolve().parent / "fixtures" / "subagents" / "claude"
ECHO_WAIT_S = 3.0


def rows(name: str) -> list[dict]:
    with (FIX / f"{name}.jsonl").open() as f:
        return [json.loads(line) for line in f]


class ReplayClient:
    instances: list["ReplayClient"] = []

    def __init__(self, options, recorded: list[dict], *, edit=None):
        self.options = options
        self.rows = [copy.deepcopy(r) for r in recorded]
        if edit is not None:
            self.rows = edit(self.rows)
        self.pos = 0
        self.queries: list = []
        self.echo_uids: list[str] = []
        self.echoed = 0
        self.hooks: list[tuple] = []        # (tool_name, agent_id, hook result)
        self.interrupts = 0
        ReplayClient.instances.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def interrupt(self):
        self.interrupts += 1

    async def query(self, prompt, session_id: str = "default"):
        items = [m async for m in prompt] if hasattr(prompt, "__aiter__") else [prompt]
        self.queries.append(items)
        for item in items:
            if isinstance(item, dict) and item.get("uuid"):
                self.echo_uids.append(item["uuid"])

    async def receive_messages(self):
        # One cursor for the client: a reader the provider replaces after an
        # end of stream continues where the last one stopped, never replays.
        while self.pos < len(self.rows):
            r = self.rows[self.pos]
            self.pos += 1
            data = r["data"]
            if r["src"] == "hook":
                if data.get("hook_event_name") != "PreToolUse":
                    continue
                hook_input = {"hook_event_name": "PreToolUse", "tool_name": data["tool_name"],
                              "tool_input": data.get("tool_input") or {},
                              "tool_use_id": data.get("tool_use_id")}
                if data.get("agent_id"):
                    hook_input["agent_id"] = data["agent_id"]
                    hook_input["agent_type"] = data.get("agent_type")
                matcher = self.options.hooks["PreToolUse"][0]
                got = await matcher.hooks[0](hook_input, data.get("tool_use_id"), {"signal": None})
                self.hooks.append((data["tool_name"], data.get("agent_id"), got))
                continue
            if r["src"] != "cli":
                continue
            data = copy.deepcopy(data)
            if (data.get("type") == "user" and data.get("isReplay")
                    and not data.get("parent_tool_use_id")):
                waited = 0.0
                while self.echoed >= len(self.echo_uids) and waited < ECHO_WAIT_S:
                    await asyncio.sleep(0.01)
                    waited += 0.01
                if self.echoed < len(self.echo_uids):
                    data["uuid"] = self.echo_uids[self.echoed]
                    self.echoed += 1
            yield parse_message(data)

    async def receive_response(self):
        async for item in self.receive_messages():
            yield item


@contextlib.contextmanager
def replaying(name: str, *, edit=None):
    """`claude._client_factory` plays `name`; no CLI is ever looked for."""
    recorded = rows(name)
    ReplayClient.instances = []
    with patch.object(claude, "_client_factory",
                      lambda options: ReplayClient(options, recorded, edit=edit)), \
            patch.object(claude, "resolve_cli", lambda: None):
        yield


def thread(thread_id: str = "t1") -> Thread:
    return Thread(id=thread_id, project_id="p1", role=Role.CHAT,
                  provider=ProviderName.CLAUDE, provider_session_id=None)


def brief(**kw) -> Brief:
    kw.setdefault("role", Role.CHAT)
    kw.setdefault("cwd", "/tmp/jarvis-sv1")
    kw.setdefault("profile", PermissionProfile.AUTO)
    return Brief(**kw)


class Permit:
    """A permit that records every question (with any `subagent` label) and
    answers `answer`."""

    def __init__(self, answer=Decision.ALLOW):
        self.answer = answer
        self.calls: list[tuple] = []

    def __call__(self, tool, args, brief=None, *, subagent=None, **_kw):
        self.calls.append((tool, dict(args), subagent))
        return self.answer


def run(name: str, turns: int = 1, *, permit=None, edit=None, texts=None):
    """Play `name` through a real ClaudeProvider for `turns` sends. ->
    (per-turn event lists, the provider, the handle's native session,
    the replay client, the permit)."""
    permit = permit if permit is not None else Permit()
    provider = claude.ClaudeProvider()
    out = []
    with replaying(name, edit=edit):
        handle = provider.start(thread(), brief(), permit)
        try:
            for n in range(turns):
                text = (texts or [])[n] if texts and n < len(texts) else f"turn {n + 1}"
                from jarvis.v2.provider import UserMessage
                out.append(list(provider.send(handle, UserMessage(text=text))))
            client = ReplayClient.instances[-1]
            session = handle.native
        finally:
            provider.close(handle)
    return out, provider, session, client, permit

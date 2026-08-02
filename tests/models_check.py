"""Synthetic checks for runtime model switching. Free — no API, no network.

What must hold:
  1. the roster is the owner's list plus `default` (the model the process
     booted on) — without that, switching is one-way until a restart
  2. resolution accepts alias / full id / bare name, and refuses anything
     off-roster with a message that names the roster (the model reads it)
  3. a switch mutates *that agent only* — never config.TIERS, which would
     silently re-route compaction, delegate(), and future workflows
  4. capability gating off a faked catalog: a model that cannot call tools is
     refused; a text-only model evicts the images already in the transcript
     (the retroactive half of the vision problem)
  5. eviction on switch obeys invariant 1 — no message deleted, every
     tool_call_id still matched
  6. an unreachable catalog degrades to "switch anyway", never to "refuse"
  7. set_model is registered non-dangerous, and is NOT reachable from a
     background workflow agent
  8. a roster note reaches every surface
  9. an open-weight model's provider pin is set from the roster, replaced
     (never merged) on the next switch, and reaches the wire as an ordered
     order[] with allow_fallbacks off — a pin that can silently reroute off
     the allowlist is not a pin

Run:  .venv/bin/python tests/models_check.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import agent as agent_mod
from jarvis import config, context, llm, models, workflows
from jarvis.tools import modelctl, subagent
from jarvis.tools import REGISTRY

CATALOG = {
    "openai/gpt-5.6-luna": {"context_length": 1_050_000, "vision": True, "tools": True},
    "anthropic/claude-opus-5": {"context_length": 500_000, "vision": True, "tools": True},
    "x-ai/grok-4.5": {"context_length": 2_000_000, "vision": True, "tools": True},
    "moonshotai/kimi-k3": {"context_length": 256_000, "vision": False, "tools": True},
    "no/tools": {"context_length": 8000, "vision": False, "tools": False},
}


def fake_catalog(catalog):
    """Pin llm.catalog() for one check. {} stands in for 'unreachable'."""
    llm.catalog = lambda refresh=False: catalog  # type: ignore[assignment]


def roster_checks() -> None:
    entries = models.roster()
    assert list(entries)[0] == "default", "default must lead the menu"
    assert entries["default"]["id"] == config.TIERS["orchestrator"]
    for alias in config.SWITCHABLE:
        assert alias in entries, f"{alias} missing from the roster"
    print(f"ok  roster: {', '.join(entries)}")

    # A roster entry duplicating the default must not appear twice.
    saved = dict(config.SWITCHABLE)
    try:
        config.SWITCHABLE["dupe"] = {"id": config.TIERS["orchestrator"], "note": ""}
        ids = [e["id"] for e in models.roster().values()]
        assert len(ids) == len(set(ids)), f"duplicate ids in the menu: {ids}"
    finally:
        config.SWITCHABLE.clear()
        config.SWITCHABLE.update(saved)
    print("ok  roster: an alias for the default model does not double up")


def resolve_checks() -> None:
    for spelling in ("opus", "OPUS", " opus ", "anthropic/claude-opus-5", "claude-opus-5"):
        alias, entry = models.resolve(spelling)
        assert entry["id"] == "anthropic/claude-opus-5", f"{spelling!r} -> {entry}"
    print("ok  resolve: alias, full id, bare name, case and space insensitive")

    for bad in ("", "gpt-4", "anthropic/claude-opus-4", "../../etc/passwd"):
        try:
            models.resolve(bad)
        except ValueError as exc:
            assert "grok" in str(exc) or "Available" in str(exc), f"unhelpful message: {exc}"
        else:
            raise AssertionError(f"{bad!r} was accepted onto the roster")
    print("ok  resolve: off-roster names refused, with the roster in the message")


def switch_checks() -> None:
    fake_catalog(CATALOG)
    tiers_before = dict(config.TIERS)

    a = agent_mod.Agent()
    b = agent_mod.Agent()
    start = a.model

    report = a.set_model("grok")
    assert a.model == "x-ai/grok-4.5", a.model
    assert start in report and "x-ai/grok-4.5" in report, report
    assert b.model == start, "switching one agent moved another"
    assert config.TIERS == tiers_before, "config.TIERS was mutated — blast radius escaped"
    print("ok  switch: scoped to one agent; config.TIERS untouched")

    assert "Already running" in a.set_model("grok"), "re-switch should be a no-op"
    a.set_model("default")
    assert a.model == start, "default must return to the model the process booted on"
    print("ok  switch: same-model is a no-op; default returns to the boot model")


def provider_pin_checks() -> None:
    """An open-weight model's host is a routing choice, so it is pinned."""
    fake_catalog(CATALOG)
    a = agent_mod.Agent()
    assert a.providers == [], "a configured tier must not carry a pin"

    report = a.set_model("kimi")
    assert a.providers == config.SWITCHABLE["kimi"]["providers"], a.providers
    assert "moonshotai" in report and "together" in report, report

    # Switching away must *replace* the pin, not keep it: a host that serves
    # kimi does not necessarily serve opus at all.
    a.set_model("opus")
    assert a.providers == [], f"a stale pin followed the switch: {a.providers}"
    print("ok  pin: set from the roster entry, replaced (not merged) on switch")

    # And it has to reach the wire in the shape OpenRouter expects.
    import jarvis.llm as real_llm

    sent = {}

    class Capture:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(json or {})

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {
                        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                        "usage": {},
                    }

            return R()

    saved_client, real_llm._client = real_llm._client, Capture()
    # chat() builds an auth header before it posts; nothing leaves the process.
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-real")
    try:
        real_llm.chat("moonshotai/kimi-k3", [], providers=["moonshotai", "together"])
        assert sent["provider"] == {
            "order": ["moonshotai", "together"],
            "allow_fallbacks": False,
        }, sent.get("provider")

        sent.clear()
        real_llm.chat("openai/gpt-5.6-luna", [], providers=[])
        assert "provider" not in sent, "an unpinned model must route normally"
    finally:
        real_llm._client = saved_client
    print("ok  pin: reaches the wire as an ordered order[] with fallbacks off")


def capability_checks() -> None:
    fake_catalog(CATALOG)
    a = agent_mod.Agent()

    saved = dict(config.SWITCHABLE)
    try:
        config.SWITCHABLE["notools"] = {"id": "no/tools", "note": ""}
        try:
            a.set_model("notools")
        except ValueError as exc:
            assert "tool calling" in str(exc), exc
        else:
            raise AssertionError("a model that cannot call tools was accepted")
        assert a.model != "no/tools", "refused switch still mutated the agent"
    finally:
        config.SWITCHABLE.clear()
        config.SWITCHABLE.update(saved)
    print("ok  capability: a model without tool calling cannot run the loop")

    # The retroactive vision trap: images already in the transcript.
    a.messages += [
        {"role": "user", "content": "look at this"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "function": {"name": "cad_render", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "cad_render", "content": "rendered"},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        },
    ]
    before = len(a.messages)
    report = a.set_model("kimi")  # text-only in the fake catalog
    assert a.model == "moonshotai/kimi-k3", a.model
    assert "image" in report, report
    live = [
        p
        for m in a.messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if context._is_live_image(p)
    ]
    assert not live, "a text-only model was handed images that were already in context"
    print("ok  capability: switching to a text-only model evicts images already in context")

    # Invariant 1: eviction rewrites in place, never deletes.
    assert len(a.messages) == before, "eviction deleted a message"
    ids = {m["tool_call_id"] for m in a.messages if m.get("role") == "tool"}
    declared = {
        c["id"] for m in a.messages for c in (m.get("tool_calls") or [])
    }
    assert declared <= ids, f"orphaned tool_call ids: {declared - ids}"
    print("ok  invariant 1: no message deleted, every tool_call_id still matched")


def failsoft_checks() -> None:
    fake_catalog({})  # catalog unreachable
    a = agent_mod.Agent()
    report = a.set_model("kimi")
    assert a.model == "moonshotai/kimi-k3", "an unreachable catalog vetoed the owner's switch"
    # Assert the roster's own text, not a copy of it — the note is prose the
    # owner edits, and a test that hardcodes the wording just breaks on rewrite.
    assert config.SWITCHABLE["kimi"]["note"] in report, "the note must survive an unknown catalog"
    print("ok  fail-soft: an unreachable catalog does not block a switch")

    # And the real catalog() must swallow a dead transport rather than raise.
    import jarvis.llm as real_llm

    saved_client, real_llm._catalog = real_llm._client, None

    class Dead:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    real_llm._client = Dead()  # type: ignore[assignment]
    real_llm._catalog_failed_at = 0.0
    try:
        assert real_llm.catalog() == {}, "catalog() must fail soft"
        assert real_llm._catalog is None, "a failure must never be cached as data"

        # …but it must not be retried per lookup either: rendering the menu
        # asks about every roster entry, and one timeout each is a minute of
        # silence before the first line prints.
        tries = {"n": 0}

        class Counting(Dead):
            def get(self, *a, **k):
                tries["n"] += 1
                return super().get(*a, **k)

        real_llm._client = Counting()  # type: ignore[assignment]
        for _ in range(4):
            assert real_llm.catalog() == {}
        assert tries["n"] == 0, f"a dead catalog was re-dialled {tries['n']}x within the window"
    finally:
        real_llm._client = saved_client
        real_llm._catalog_failed_at = 0.0
    print("ok  fail-soft: catalog() fails soft, caches no data, and stops re-dialling")


def registration_checks() -> None:
    entry = REGISTRY["set_model"]
    assert not entry.dangerous, "set_model must not need an approval card"
    assert "list_models" in REGISTRY

    # Two toolsets must stay clear of it, for different reasons: a workflow
    # agent could re-point itself unwatched, and a sub-agent would re-point
    # its *parent* mid-turn — modelctl is bound to the orchestrator, not to
    # whoever calls the tool.
    for name in ("set_model", "list_models"):
        assert name not in workflows.SAFE_TOOLS, f"{name} reachable from a background workflow"
        assert name not in subagent.SUBAGENT_TOOLS, f"{name} reachable from a sub-agent"
    print("ok  registration: non-dangerous, out of reach of workflow and sub-agents")

    fake_catalog(CATALOG)
    seen = []
    modelctl._agent = None
    try:
        modelctl.switch("opus")
    except ValueError as exc:
        assert "nothing to switch" in str(exc), exc
    else:
        raise AssertionError("switching with no bound agent should fail")

    a = agent_mod.Agent()
    modelctl.bind(a)
    modelctl.on_change(seen.append)
    modelctl.switch("kimi")
    assert a.model == "moonshotai/kimi-k3"
    assert modelctl.current() == a.model
    assert seen and seen[-1]["model"] == a.model, "on_change did not fire"
    note = config.SWITCHABLE["kimi"]["note"]
    assert note in seen[-1]["report"], "the HUD is not told about the roster note"
    print("ok  modelctl: bind/switch/current/on_change, note carried to the window")


def main() -> int:
    saved_catalog = llm.catalog
    try:
        roster_checks()
        resolve_checks()
        switch_checks()
        provider_pin_checks()
        capability_checks()
        failsoft_checks()
        registration_checks()
    finally:
        llm.catalog = saved_catalog  # type: ignore[assignment]
        modelctl._agent = None
        modelctl._on_change = None
    print("\nall model-switching checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

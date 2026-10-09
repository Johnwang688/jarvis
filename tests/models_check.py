"""Synthetic checks for the model roster and picker. Free — no API calls.

Three layers, all offline: `llm.catalog` is replaced by a fixture so nothing
reaches OpenRouter, and both state files are pointed at a temp directory (a
suite that writes the owner's real roster is the `apt`-on-the-allowlist bug
again).

What is worth pinning here, in the order it matters:

  * **Eligibility is a refusal, not a sort.** A model that cannot call tools
    cannot run this loop, so it must be impossible to get one onto the roster
    — the failure it causes would surface a turn later as "the model just
    talks" with nothing pointing back at the picker.
  * **The selection reaches the tiers that were following the orchestrator**
    and leaves the ones that were not. A child on last month's model while the
    parent runs the new one is precisely the drift "every child is the
    orchestrator tier" exists to prevent.
  * **The catalog degrades toward showing something**, and says when it is
    stale rather than quietly serving an old list.

Run:  .venv/bin/python tests/models_check.py
"""

from __future__ import annotations

import http.client
import json
import queue
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, llm, models  # noqa: E402

PORT = 8447


def _entry(model_id, **kw):
    """One raw OpenRouter catalog entry, eligible unless told otherwise."""
    entry = {
        "id": model_id,
        "name": kw.get("name", model_id),
        "context_length": kw.get("context", 128000),
        "architecture": {
            "input_modalities": ["text"] + (["image"] if kw.get("vision") else []),
            "output_modalities": kw.get("outputs", ["text"]),
        },
        "pricing": {
            "prompt": kw.get("prompt", "0.000001"),
            "completion": kw.get("completion", "0.000004"),
        },
        "supported_parameters": kw.get("params", ["tools", "temperature"]),
        "description": kw.get("description", "a model"),
    }
    if kw.get("intelligence") is not None:
        entry["benchmarks"] = {
            "artificial_analysis": {
                "intelligence_index": kw["intelligence"],
                "agentic_index": kw.get("agentic", 40),
            }
        }
    if kw.get("reasoning"):
        entry["reasoning"] = kw["reasoning"]
    return entry


FIXTURE = [
    _entry("openai/gpt-5.6-luna", name="OpenAI: Luna", intelligence=58, vision=True,
           reasoning={"supported_efforts": ["max", "high", "medium", "low"]}),
    _entry("anthropic/claude-opus-5", name="Anthropic: Opus 5", intelligence=63.1,
           vision=True, prompt="0.000005", completion="0.000025"),
    _entry("tiny/free-model", name="Tiny: Free", prompt="0", completion="0"),
    _entry("mid/unrated-model", name="Mid: Unrated", prompt="0.0000005",
           completion="0.0000015"),
    # The three shapes that must never reach the roster.
    _entry("chat/no-tools", params=["temperature"]),          # cannot call tools
    _entry("draw/image-out", outputs=["image"]),               # not a text model
    _entry("openrouter/auto", prompt="-1", completion="-1"),   # a router, not a model
    _entry("anthropic/claude-opus-5:batch", intelligence=63.1),  # async endpoint
]


def _reset_catalog():
    models._cached, models._cached_at, models._stale_reason = [], 0.0, ""


def _serve(entries):
    """Point llm.catalog at a fixture; returns a call counter."""
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        if isinstance(entries, Exception):
            raise entries
        return list(entries)

    llm.catalog = fake
    return calls


def eligibility_checks() -> None:
    _reset_catalog()
    _serve(FIXTURE)
    catalog = models.catalog(refresh=True)
    ids = [m.id for m in catalog]

    for refused in ("chat/no-tools", "draw/image-out", "openrouter/auto",
                    "anthropic/claude-opus-5:batch"):
        assert refused not in ids, f"{refused} reached the catalog"
    assert ids[0] == "anthropic/claude-opus-5", ids  # best rated first
    assert ids[-1] == "tiny/free-model" or "mid/unrated-model" in ids[-2:], ids

    opus = next(m for m in catalog if m.id == "anthropic/claude-opus-5")
    # Dollars per *million* tokens: the raw payload is per token, and a picker
    # that showed 0.000005 would be unreadable.
    assert opus.prompt_usd == 5.0 and opus.completion_usd == 25.0, opus
    assert opus.intelligence == 63.1 and opus.vision is True and opus.free is False

    unrated = next(m for m in catalog if m.id == "mid/unrated-model")
    # Unrated is None, never 0 — a zero would sort and render as "measured
    # and terrible" instead of "not scored".
    assert unrated.intelligence is None, unrated
    assert unrated.describe()["intelligence"] is None
    assert next(m for m in catalog if m.id == "tiny/free-model").free is True
    print(f"ok  eligibility: {len(catalog)} eligible, 4 refused shapes, prices per M")


def cache_checks() -> None:
    _reset_catalog()
    calls = _serve(FIXTURE)
    models.catalog(refresh=True)
    models.catalog()
    models.catalog()
    assert calls["n"] == 1, f"catalog refetched {calls['n']} times"
    assert models.stale_reason() == ""

    # A dead network with a cache in hand serves the cache and says so.
    _serve(llm.LLMError("connection refused"))
    served = models.catalog(refresh=True)
    assert [m.id for m in served] == [m.id for m in models.catalog()], "stale copy lost"
    assert "connection refused" in models.stale_reason(), models.stale_reason()

    # A dead network with nothing cached is an error the picker must render,
    # not an empty list that reads as "OpenRouter has no models".
    _reset_catalog()
    config.MODEL_CACHE_PATH = config.MODEL_CACHE_PATH.parent / "absent.json"
    try:
        models.catalog(refresh=True)
    except LookupError as exc:
        assert "unreachable" in str(exc), exc
    else:
        raise AssertionError("an unreachable catalog with no cache did not raise")

    # A 200 that parses to nothing eligible is a shape change at the far end.
    # Keep what we had rather than blanking the picker.
    _reset_catalog()
    _serve(FIXTURE)
    models.catalog(refresh=True)
    _serve([_entry("chat/no-tools", params=[])])
    assert [m.id for m in models.catalog(refresh=True)][0] == "anthropic/claude-opus-5"
    assert "no eligible models" in models.stale_reason()
    print("ok  cache: fetched once, stale copy survives an outage, empty catalog refused")


def roster_checks() -> None:
    _reset_catalog()
    _serve(FIXTURE)
    default = config.TIERS["orchestrator"]

    # Nothing saved yet: the list opens on the model he is already running.
    assert models.roster().models == [default], models.roster()
    assert models.selected() == "" and models.tier("orchestrator") == default

    try:
        models.add("chat/no-tools")
    except models.NotEligible as exc:
        assert "tool calling" in str(exc), exc
    else:
        raise AssertionError("a model that cannot call tools was added to the roster")
    try:
        models.add("nope/invented")
    except models.NotEligible:
        pass
    else:
        raise AssertionError("an invented model id was added to the roster")

    models.add("anthropic/claude-opus-5")
    models.add("anthropic/claude-opus-5")  # idempotent
    models.add("tiny/free-model")
    assert models.roster().models == [default, "anthropic/claude-opus-5", "tiny/free-model"]
    assert json.loads(config.MODELS_PATH.read_text())["models"][-1] == "tiny/free-model"

    try:
        models.select("mid/unrated-model")  # eligible, but not on the list
    except LookupError:
        pass
    else:
        raise AssertionError("selected a model that is not on the roster")

    models.select("anthropic/claude-opus-5")
    assert models.selected() == "anthropic/claude-opus-5"
    assert models.tier("orchestrator") == "anthropic/claude-opus-5"

    # Removing the selected model must not leave the loop pointed at something
    # the picker no longer lists.
    models.remove("anthropic/claude-opus-5")
    assert models.selected() == "", models.roster()
    assert models.tier("orchestrator") == default
    try:
        models.remove("anthropic/claude-opus-5")
    except LookupError:
        pass
    else:
        raise AssertionError("removed a model that was not on the roster")

    models.add("anthropic/claude-opus-5")
    models.select("anthropic/claude-opus-5")
    models.select("")  # back to the configured default
    assert models.selected() == "" and models.tier("orchestrator") == default

    # A corrupt file must not take the loop down: fall back to the default.
    config.MODELS_PATH.write_text("{ not json")
    assert models.selected() == "" and models.tier("orchestrator") == default
    assert models.roster().models == [default]
    config.MODELS_PATH.unlink()
    print("ok  roster: seeded, add validated, remove clears a stale selection, corrupt file survived")


def _refused(fn, *args, needle: str = "") -> str:
    """Call fn, expecting RosterRefused; returns its sentence."""
    try:
        fn(*args)
    except models.RosterRefused as exc:
        assert needle in str(exc), exc
        return str(exc)
    raise AssertionError(f"{fn.__name__}{args} was not refused")


def roster_edit_checks() -> None:
    """The HUD can unpin any model — the configured default included — and
    choose the default, without ever leaving a default thread on a model the
    picker does not list (2026-10-08)."""
    _reset_catalog()
    _serve(FIXTURE)
    config.MODELS_PATH.unlink(missing_ok=True)
    default = config.TIERS["orchestrator"]
    opus, tiny, luna = "anthropic/claude-opus-5", "tiny/free-model", "openai/gpt-5.6-luna"

    # A plain unpin, and the selected model's unpin falling back to config.
    models.add(opus)
    models.add(tiny)
    models.remove(tiny)
    assert models.roster().models == [default, opus], models.roster()
    models.select(opus)
    models.remove(opus)
    assert models.selected() == "" and models.tier("orchestrator") == default
    assert models.describe()["default_source"] == "config"

    # The env model while it is the effective default (nothing chosen): refused.
    models.add(opus)
    _refused(models.remove, default, needle="choose another default first")
    assert default in models.roster().models

    # Choose another default, and the env model unpins like any other.
    models.select(opus)
    assert models.describe()["default_source"] == "hud"
    assert models.describe()["current"] == opus
    models.remove(default)
    assert models.roster().models == [opus], models.roster()
    saved = json.loads(config.MODELS_PATH.read_text())
    assert saved["removed_default"] == default, saved
    # ...and stays unpinned across a reload: _load used to put it straight back.
    assert models.roster().models == [opus] and models.tier("orchestrator") == opus
    assert [m["id"] for m in models.describe()["models"]] == [opus]

    # The roster is never empty: the last model cannot go.
    _refused(models.remove, opus, needle="only model")
    assert models.roster().models == [opus]
    # Nor can the selection go while the env model is unpinned — the loop
    # would fall back onto a model the picker no longer lists.
    models.add(tiny)
    _refused(models.remove, opus, needle="choose another default first")
    models.remove(tiny)

    # A hand-edited file that leaves nothing selected cannot hide the default.
    config.MODELS_PATH.write_text(json.dumps(
        {"models": [opus], "selected": "", "removed_default": default}))
    assert models.roster().models == [default, opus], models.roster()
    config.MODELS_PATH.write_text(json.dumps(
        {"models": [], "selected": "", "removed_default": default}))
    assert models.roster().models == [default], "an empty roster was loaded"

    # Reset to config default re-lists the env model and clears the flag.
    config.MODELS_PATH.write_text(json.dumps(
        {"models": [opus], "selected": opus, "removed_default": default}))
    models.select("")
    assert models.roster().models == [default, opus], models.roster()
    assert models.selected() == "" and models.tier("orchestrator") == default
    assert json.loads(config.MODELS_PATH.read_text())["removed_default"] == ""

    # Re-pinning the env model by hand clears the flag too.
    models.select(opus)
    models.remove(default)
    models.add(default)
    assert json.loads(config.MODELS_PATH.read_text())["removed_default"] == ""
    assert models.roster().models == [opus, default]

    # A flag naming a model that is no longer the configured default is
    # ignored, so a changed JARVIS_ORCHESTRATOR is seeded as before.
    config.MODELS_PATH.write_text(json.dumps(
        {"models": [opus], "selected": opus, "removed_default": "old/default"}))
    assert models.roster().models == [default, opus], models.roster()

    # An unpinned model's effort pin goes with it — out of the file, not just
    # filtered on the next read. Luna is the fixture model with a ladder.
    config.MODELS_PATH.unlink()
    models.add(opus)
    models.add(luna)
    models.select(opus)
    models.set_effort(luna, "low")
    assert json.loads(config.MODELS_PATH.read_text())["efforts"] == {luna: "low"}
    models.remove(luna)
    assert json.loads(config.MODELS_PATH.read_text())["efforts"] == {}
    models.select("")
    config.MODELS_PATH.unlink()
    print("ok  roster edits: any model unpins, the env model stays unpinned, "
          "the effective default is always listed, reset re-lists it")


ROSTER_WRITES = ("models.select(", "models.set_effort(", "models.remove(", "models.add(",
                 "models_mod.select(", "models_mod.set_effort(", "models_mod.remove(",
                 "models_mod.add(", "models._save(", "MODELS_PATH",
                 # The Claude/Codex chat default (2026-10-08): its write
                 # function, its saver, its file and its route.
                 "set_provider_default(", "_save_defaults(", "PROVIDER_DEFAULTS_PATH",
                 "provider_defaults", "/thread-models")


def no_tool_checks() -> None:
    """The agent has no tool for the model he thinks with (2026-08-22), and
    that now covers the roster too: unpinning the model a turn runs on, or
    choosing the default, is the owner's, in the window."""
    from jarvis import tools

    for name in tools.REGISTRY:
        assert not any(w in name for w in ("model", "roster", "default")), f"registered tool {name!r}"
    roots = [Path(tools.__file__).parent, Path(tools.__file__).parents[1] / "v2" / "tools"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for needle in ROSTER_WRITES:
                assert needle not in text, f"{path} reaches {needle}"
    print("ok  guard: no tool can change the roster, the default, an effort pin, "
          "or the Claude/Codex chat default")


def robustness_checks() -> None:
    """A non-UTF-8 file, an atomic save, one snapshot per listing, and the
    404 class that catches only "not on the roster" (review of PR #13)."""
    _reset_catalog()
    _serve(FIXTURE)
    default = config.TIERS["orchestrator"]
    opus = "anthropic/claude-opus-5"

    # Not UTF-8: degrades to the seed like any corrupt file. It used to raise
    # UnicodeDecodeError out of effort_for — i.e. out of every v1 turn.
    config.MODELS_PATH.write_bytes(b'{"models": ["\xff\xfe"], "selected": ""}')
    assert models.roster().models == [default]
    models.effort_for(default)
    assert [m["id"] for m in models.describe()["models"]] == [default]
    _refused(models.remove, default, needle="only model")
    models.add(opus)  # and a write replaces it cleanly
    assert json.loads(config.MODELS_PATH.read_text(encoding="utf-8"))["models"] == [default, opus]

    # Atomic, and the file keeps its mode: no temp file left behind.
    config.MODELS_PATH.chmod(0o640)
    models.select(opus)
    assert config.MODELS_PATH.stat().st_mode & 0o777 == 0o640, oct(config.MODELS_PATH.stat().st_mode)
    leftovers = [p.name for p in config.MODELS_PATH.parent.iterdir() if p.name.startswith(".models-")]
    assert not leftovers, leftovers
    config.MODELS_PATH.chmod(0o644)

    # One snapshot per describe(): a change landing between two reads must
    # not make `selected` and `current` disagree, nor list a removed model.
    snapshots = [models.Roster(models=[default, opus], selected=opus),
                 models.Roster(models=[default], selected="")]
    reads = {"n": 0}
    real_roster = models.roster

    def racing():
        reads["n"] += 1
        return snapshots[min(reads["n"] - 1, 1)]

    models.roster = racing
    try:
        payload = models.describe()
    finally:
        models.roster = real_roster
    assert reads["n"] == 1, f"describe read the roster {reads['n']} times"
    assert payload["selected"] == opus and payload["current"] == opus, payload
    assert [m["id"] for m in payload["models"]] == [default, opus], payload
    assert payload["default_source"] == "hud"

    # The 404 class is exactly "not on the roster".
    for call in (lambda: models.remove("never/listed"), lambda: models.select("never/listed"),
                 lambda: models.set_effort("never/listed", "low")):
        try:
            call()
        except models.NotOnRoster as exc:
            assert isinstance(exc, LookupError) and "not on the roster" in str(exc)
        else:
            raise AssertionError("a model off the roster was not refused")

    # The pure refusal rule the HUD mock imports agrees with remove().
    rule = models.removal_refusal
    assert rule([default], "", default, default) and "only model" in rule([default], "", default, default)
    assert "choose another default first" in rule([default, opus], "", default, default)
    assert rule([default, opus], opus, default, default) is None
    assert rule([default, opus], opus, default, opus) is None
    assert "choose another default first" in rule([opus, "tiny/free-model"], opus, default, opus)
    models.select("")
    config.MODELS_PATH.unlink()
    print("ok  robustness: non-UTF-8 file seeds, atomic save keeps its mode, "
          "describe reads one snapshot, 404 is NotOnRoster only")


def tier_checks() -> None:
    _reset_catalog()
    _serve(FIXTURE)
    default = config.TIERS["orchestrator"]
    models.add("anthropic/claude-opus-5")
    models.select("anthropic/claude-opus-5")

    followers = [name for name, value in config.TIERS.items() if value == default]
    assert {"orchestrator", "subagent", "compaction", "review"} <= set(followers), followers
    for name in followers:
        assert models.tier(name) == "anthropic/claude-opus-5", name
    for name in ("worker", "cheap"):
        # Pointed somewhere else on purpose, and they stay there.
        assert models.tier(name) == config.TIERS[name], name

    # A tier the owner pinned to a different model is not swept along.
    config.TIERS["subagent"] = "some/other-model"
    assert models.tier("subagent") == "some/other-model"
    config.TIERS["subagent"] = default

    # The whole point: an Agent built with no model= runs on the selection.
    from jarvis import agent as agent_mod

    assert agent_mod.Agent(tool_names=["read_file"]).model == "anthropic/claude-opus-5"
    models.select("")
    assert agent_mod.Agent(tool_names=["read_file"]).model == default
    print("ok  tiers: the selection moves the followers, leaves worker/cheap, reaches Agent()")


class _Body:
    """The smallest thing `llm.chat`'s non-streaming path will accept."""

    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "model": "test/model",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def effort_checks() -> None:
    """How hard the model is asked to think, and who gets asked."""
    # Its own cache file: this section serves a different fixture, and the
    # disk cache is shared and still fresh, so route_checks would otherwise
    # read this one's catalog instead of its own.
    shared_cache = config.MODEL_CACHE_PATH
    config.MODEL_CACHE_PATH = shared_cache.parent / "effort-cache.json"
    _reset_catalog()
    _serve([
        # Luna's real ladder, and a model that stops short of it.
        _entry("openai/gpt-5.6-luna", intelligence=58,
               reasoning={"supported_efforts": ["max", "xhigh", "high", "medium", "low"]}),
        _entry("cheap/short-ladder",
               reasoning={"supported_efforts": ["high", "medium", "low"]}),
        _entry("plain/no-reasoning"),
    ])
    models.catalog(refresh=True)

    config.REASONING_EFFORT = "max"
    assert models.effort_for("openai/gpt-5.6-luna") == "max"
    # Clamped down the ladder, because the ladder is not the same everywhere:
    # asking a model for a level it does not publish reads as though it did
    # something, and the request should say what it means.
    assert models.effort_for("cheap/short-ladder") == "high"
    # No reasoning control at all: send nothing rather than a no-op.
    assert models.effort_for("plain/no-reasoning") is None
    # Cold catalog is not a reason to think less hard — an unpublished effort
    # is accepted and clamped upstream (verified live), never refused.
    assert models.effort_for("never/heard-of-it") == "max"

    config.REASONING_EFFORT = "medium"
    assert models.effort_for("openai/gpt-5.6-luna") == "medium"
    for off in ("", "default"):
        config.REASONING_EFFORT = off
        assert models.effort_for("openai/gpt-5.6-luna") is None, off
    # A typo must not become a 400 on every request for the rest of the run.
    config.REASONING_EFFORT = "maximum"
    models._effort_warned = True  # the warning itself is not what is under test
    assert models.effort_for("openai/gpt-5.6-luna") is None
    config.REASONING_EFFORT = "max"
    print("ok  effort: clamped to the model's own ladder, absent when it has none")

    # A pin made about one model beats the global default, and clearing it
    # hands that model back — the way out of any choice made in the picker.
    models.add("cheap/short-ladder")
    models.add("plain/no-reasoning")
    assert models.effort_for("cheap/short-ladder") == "high"  # global, clamped
    models.set_effort("cheap/short-ladder", "low")
    assert models.effort_for("cheap/short-ladder") == "low"
    assert models.effort_for("openai/gpt-5.6-luna") == "max", "the pin leaked"
    assert json.loads(config.MODELS_PATH.read_text())["efforts"] == {"cheap/short-ladder": "low"}
    models.set_effort("cheap/short-ladder", "")
    assert models.effort_for("cheap/short-ladder") == "high"

    # A level this model does not offer is refused rather than clamped: the
    # picker would otherwise show a level nobody chose.
    models.set_effort("cheap/short-ladder", "high")
    try:
        models.set_effort("plain/no-reasoning", "high")
    except models.NotEligible:
        pass
    else:
        raise AssertionError("pinned an effort on a model with no reasoning control")
    for bad in ("max", "turbo"):
        try:
            models.set_effort("cheap/short-ladder", bad)
        except models.NotEligible:
            pass
        else:
            raise AssertionError(f"accepted {bad!r} on a model that does not offer it")
    assert models.effort_for("cheap/short-ladder") == "high", "a refusal changed the pin"
    try:
        models.set_effort("never/heard-of-it", "high")
    except LookupError:
        pass
    else:
        raise AssertionError("pinned effort on a model that is not on the roster")

    # An override for a removed model does not survive to haunt a re-add.
    models.remove("cheap/short-ladder")
    models.add("cheap/short-ladder")
    assert models.roster().efforts == {}, models.roster().efforts
    assert models.effort_for("cheap/short-ladder") == "high"  # back to the global

    # describe() carries both the setting and its consequence, which are not
    # the same number on a model whose ladder stops short of the default.
    models.set_effort("cheap/short-ladder", "low")
    rows = {m["id"]: m for m in models.describe()["models"]}
    assert rows["cheap/short-ladder"]["effort"] == "low"
    assert rows["cheap/short-ladder"]["effective_effort"] == "low"
    assert rows["openai/gpt-5.6-luna"]["effort"] == ""
    assert rows["openai/gpt-5.6-luna"]["effective_effort"] == "max"
    assert models.describe()["default_effort"] == "max"
    models.remove("cheap/short-ladder")
    print("ok  effort: a per-model pin beats the global, is refused off-ladder, "
          "and does not outlive the model")

    # And it reaches the wire only when a caller asks for it.
    sent: list[dict] = []
    real_post = llm._client.post

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Body()

    llm._client.post = fake_post
    # Non-streaming for the whole block: the streaming path goes through
    # _client.stream, which is *not* faked here — leaving it on would send
    # these checks to the real API.
    was_streaming, config.STREAM = config.STREAM, False
    try:
        llm.chat("openai/gpt-5.6-luna", [{"role": "user", "content": "hi"}], stream=False)
        assert "reasoning" not in sent[-1], sent[-1]
        llm.chat("openai/gpt-5.6-luna", [{"role": "user", "content": "hi"}],
                 stream=False, effort="max")
        assert sent[-1]["reasoning"] == {"effort": "max"}, sent[-1]

        # The loop asks; the cheap tier does not. A reasoning budget on bulk
        # text work is spend with nothing to show for it.
        from jarvis import agent as agent_mod

        before = len(sent)
        agent_mod.Agent(tool_names=["read_file"], max_steps=1).run_turn("hi")
        assert len(sent) > before, "the loop made no call to check"
        assert sent[-1]["reasoning"] == {"effort": "max"}, "the loop sent no effort"
        before = len(sent)
        agent_mod.delegate("hi", tier="cheap")
        assert len(sent) > before, "delegate made no call to check"
        assert "reasoning" not in sent[-1], "the cheap tier was asked to reason"
        print("ok  effort: the agent loop sends it, delegate('cheap') does not")
    finally:
        llm._client.post = real_post
        config.STREAM = was_streaming
        config.MODEL_CACHE_PATH = shared_cache
        _reset_catalog()


def byok_cost_checks() -> None:
    """A BYOK call costs real money and reports zero credits — count it anyway.

    Every dollar budget here is denominated in dollars: the goal runner parks
    a runaway goal on spend, the HUD shows session cost, benches print cost
    columns. Reading `usage.cost` alone makes all of them stop counting the
    moment a model routes to the owner's own provider key.
    """
    # Measured shapes, 2026-08-23. Credit-billed: the two fields are the same
    # number, so this must not be a sum. BYOK: credits 0, upstream real.
    assert llm._cost({"cost": 9e-06, "is_byok": False,
                      "cost_details": {"upstream_inference_cost": 9e-06}}) == 9e-06
    assert llm._cost({"cost": 0, "is_byok": True,
                      "cost_details": {"upstream_inference_cost": 0.0006264}}) == 0.0006264
    # Degradations: no details, no usage at all, junk.
    assert llm._cost({"cost": 0.002}) == 0.002
    assert llm._cost({}) == 0.0
    assert llm._cost({"cost": None, "cost_details": None}) == 0.0

    # And it reaches Reply.cost_usd, which is what every budget reads.
    real_post = llm._client.post

    class Body:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "model": "moonshotai/kimi-k3",
                "usage": {"prompt_tokens": 88, "completion_tokens": 50, "cost": 0,
                          "is_byok": True,
                          "cost_details": {"upstream_inference_cost": 0.001014}},
            }

    llm._client.post = lambda *a, **kw: Body()
    was_streaming, config.STREAM = config.STREAM, False
    try:
        reply = llm.chat("moonshotai/kimi-k3", [{"role": "user", "content": "hi"}])
        assert reply.cost_usd == 0.001014, reply.cost_usd
    finally:
        llm._client.post = real_post
        config.STREAM = was_streaming
    print("ok  byok: a call billed to the owner's own key still reports its cost")


def _request(method: str, path: str, payload=None, origin: str | None = None):
    conn = http.client.HTTPConnection("localhost", PORT, timeout=5)
    headers = {"Content-Type": "application/json"}
    headers["Origin"] = origin if origin is not None else f"http://localhost:{PORT}"
    conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, (json.loads(body) if body else {})


def route_checks() -> None:
    _reset_catalog()
    _serve(FIXTURE)
    default = config.TIERS["orchestrator"]
    config.MODELS_PATH.unlink(missing_ok=True)  # start from the seeded roster

    from jarvis.face import server

    srv = server.create_server(port=PORT)
    sse: queue.Queue = queue.Queue(maxsize=50)
    with server._subs_lock:
        server._subscribers.append(sse)
    try:
        status, data = _request("GET", "/models")
        assert status == 200 and data["selected"] == "" and data["current"] == default
        assert [m["id"] for m in data["models"]] == [default], data

        status, data = _request("GET", "/models/catalog")
        assert status == 200 and len(data["models"]) == 4, data
        assert data["roster"] == [default] and data["stale"] == ""

        agent = server._get_agent()
        assert agent.model == default

        assert _request("POST", "/models", {"add": "chat/no-tools"})[0] == 400
        assert _request("POST", "/models", {})[0] == 400
        assert _request("POST", "/models", {"add": "x", "remove": "y"})[0] == 400
        assert _request("POST", "/models", {"remove": "not/listed"})[0] == 404
        assert _request("POST", "/models", {"model": "x", "add": "y"})[0] == 400
        # An effort riding an add or a remove is refused, not dropped.
        assert _request("POST", "/models", {"add": "tiny/free-model", "effort": "low"})[0] == 400
        assert _request("POST", "/models", {"remove": default, "effort": "low"})[0] == 400
        assert "tiny/free-model" not in models.roster().models

        status, data = _request("POST", "/models", {"add": "anthropic/claude-opus-5"})
        assert status == 200 and "anthropic/claude-opus-5" in [m["id"] for m in data["models"]]
        assert json.loads(sse.get(timeout=2))["kind"] == "model"

        assert _request("POST", "/model", {"model": "mid/unrated-model"})[0] == 404
        status, data = _request("POST", "/model", {"model": "anthropic/claude-opus-5"})
        assert status == 200 and data["current"] == "anthropic/claude-opus-5", data
        assert json.loads(sse.get(timeout=2))["kind"] == "model"
        # The live agent moves with it — rebuilding would throw the
        # conversation away, and leaving it alone would mean the window and
        # the loop disagree about which model is answering.
        assert agent.model == "anthropic/claude-opus-5"
        assert server._get_agent() is agent, "the transcript was thrown away"
        assert _request("GET", "/config")[1]["llm"] == "anthropic/claude-opus-5"

        # Per-model effort rides the same route.
        status, data = _request("POST", "/models", {"model": default, "effort": "low"})
        assert status == 200
        row = next(m for m in data["models"] if m["id"] == default)
        assert row["effort"] == "low" and row["effective_effort"] == "low", row
        assert _request("POST", "/models", {"model": default, "effort": "turbo"})[0] == 400
        # opus carries no reasoning block in this fixture, so there is nothing
        # to pin — refused rather than stored and ignored.
        assert _request(
            "POST", "/models", {"model": "anthropic/claude-opus-5", "effort": "low"})[0] == 400
        _request("POST", "/models", {"model": default, "effort": ""})
        assert _request("POST", "/models", {"model": "not/listed", "effort": "low"})[0] == 404

        # Removing the selected model puts the live agent back on the default.
        _request("POST", "/models", {"remove": "anthropic/claude-opus-5"})
        assert agent.model == default and models.selected() == ""
        # The env model is the effective default now, so unpinning it is a
        # 409 with a sentence the picker can show, not a silent fallback.
        _request("POST", "/models", {"add": "tiny/free-model"})
        status, data = _request("POST", "/models", {"remove": default})
        assert status == 409 and "choose another default first" in data["error"], (status, data)

        for path, payload in (("/model", {"model": ""}), ("/models", {"add": "x"})):
            assert _request("POST", path, payload, origin="http://evil.example")[0] == 403, path
        print("ok  routes: /models lists, /model selects and moves the live agent, 400/403/404")
    finally:
        with server._subs_lock:
            if sse in server._subscribers:
                server._subscribers.remove(sse)
        srv.shutdown()
        srv.server_close()


def main() -> int:
    real_catalog = llm.catalog
    with tempfile.TemporaryDirectory() as tmp:
        config.MODELS_PATH = Path(tmp) / "models.json"
        config.MODEL_CACHE_PATH = Path(tmp) / "cache" / "models.json"
        config.ALLOWLIST_PATH = Path(tmp) / "allowlist.json"
        try:
            eligibility_checks()
            config.MODEL_CACHE_PATH = Path(tmp) / "cache" / "models.json"
            cache_checks()
            config.MODEL_CACHE_PATH = Path(tmp) / "cache" / "models.json"
            roster_checks()
            roster_edit_checks()
            robustness_checks()
            no_tool_checks()
            tier_checks()
            effort_checks()
            byok_cost_checks()
            route_checks()
        finally:
            llm.catalog = real_catalog
    print("\nall model checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

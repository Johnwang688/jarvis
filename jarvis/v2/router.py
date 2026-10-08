"""Deterministic message placement and provider admission (design §8).

Thread.provider is never edited. WP11 calls ready_to_start before admitting a
proposal and next_worker for each NEW thread; running threads finish pinned.
Brief lacks images in the fixed interface: resolve accepts message images and
MCP requirements as keyword context; a supplied Brief supplies profile/MCP.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import json
import re
import threading
import time

from jarvis import config
from .ledger import DEFAULT_ALLOWANCES, UsageLedger
from .model import (PermissionProfile, ProviderName, Role, RoutingDecision,
                    TaskState, to_json)
from .provider import Event
from .stores import ProjectArchived, StoreError, _write_bytes

PROPOSAL_GRACE_S = 60
HEALTH_CACHE_S = 60
ROLES = ("orchestrator", "implementer", "reviewer", "researcher")
CLI_PROVIDERS = ("claude", "codex")
EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
_CONFIG_LOCK = threading.RLock()

# The models each CLI provider can be asked for by name, with the efforts each
# one takes and whether it can see an image. One table, read by the
# capability filter below (vision), by the per-thread model check
# (`thread_model`) and by the HUD's model chip — so the router, the check and
# the list cannot disagree about what exists.
#
# Claude: Claude Code's own model ids; `--effort` is the SDK's `EffortLevel`
# (`providers/claude.py` `EFFORT_LEVELS`), and Haiku 4.5 has no effort control.
# Codex: the models the routing defaults and the vision filter already named;
# the app-server's ReasoningEffort is "a value advertised by the model", so
# the ladder here is the one the routing defaults use (high, xhigh) and its
# neighbours. Both lists are a statement of what Jarvis will ask for, not a
# live probe; a model missing here is refused by name rather than guessed at —
# by the chip, and by the routing table too: `load_routing` and `/route`
# accept a claude/codex model only from this table (`_cli_model`), plus
# `roster`, the routing table's own spelling of "the configured default".
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
CLI_MODELS: dict[str, dict[str, dict]] = {
    "claude": {
        "claude-opus-5-5": {"name": "Claude Opus 5.5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-sonnet-5-5": {"name": "Claude Sonnet 5.5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-fable-5-1": {"name": "Claude Fable 5.1", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-opus-5": {"name": "Claude Opus 5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-sonnet-5": {"name": "Claude Sonnet 5", "efforts": _CLAUDE_EFFORTS, "vision": True},
        "claude-haiku-4-5": {"name": "Claude Haiku 4.5", "efforts": (), "vision": True},
    },
    "codex": {
        "gpt-6-astra": {"name": "GPT-6 Astra", "efforts": _CODEX_EFFORTS, "vision": True},
        "gpt-5.6-sol": {"name": "GPT-5.6 Sol", "efforts": _CODEX_EFFORTS, "vision": True},
        "gpt-5.6-terra": {"name": "GPT-5.6 Terra", "efforts": _CODEX_EFFORTS, "vision": True},
        "gpt-5.6-luna": {"name": "GPT-5.6 Luna", "efforts": _CODEX_EFFORTS, "vision": True},
        "gpt-5.5": {"name": "GPT-5.5", "efforts": _CODEX_EFFORTS, "vision": True},
    },
}


@dataclass(frozen=True)
class Incoming:
    text: str
    surface: str = "cli"
    project_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    intake: bool = False
    spoken: bool = False


class Destination:
    pass


@dataclass(frozen=True)
class Verb(Destination):
    name: str
    argument: str = ""


@dataclass(frozen=True)
class NewTask(Destination):
    text: str


@dataclass(frozen=True)
class Steer(Destination):
    task_id: str


@dataclass(frozen=True)
class FastPath(Destination):
    pass


@dataclass(frozen=True)
class NeedsProject(Destination):
    brief: str
    reply: str = "Which project should this task belong to?"


def classify(incoming: Incoming) -> Destination:
    text = incoming.text.strip()
    # Voice cannot authorize even when it happens to sound like a command.
    if incoming.spoken:
        return Steer(incoming.task_id) if incoming.task_id else FastPath()
    match = re.fullmatch(r"(yes|no|always)\s+(\S+)|(status|projects|tasks)|"
                         r"(cancel)(?:\s+(\S+))?|(steer|redirect):\s*(.+)|"
                         r"(resume)\s+(\S+(?:\s+on\s+\S+)?)", text,
                         flags=re.I | re.S)
    if match:
        a, b, c, d, e, f, g, h, i = match.groups()
        return Verb((a or c or d or f or h).lower(), b or e or g or i or "")
    if incoming.intake or text.lower().startswith("task:"):
        return NewTask(text[5:].strip() if text.lower().startswith("task:") else text)
    if incoming.task_id:
        return Steer(incoming.task_id)
    return FastPath()


def defaults():
    return {"chains": {role: (["claude", "codex"] if role in ("orchestrator", "reviewer")
                              else ["codex", "claude"]) for role in ROLES},
            "models": {role: {"claude": "roster/default", "codex":
                               "gpt-6-astra/xhigh" if role in ("orchestrator", "reviewer")
                               else "gpt-5.6-sol/high"} for role in ROLES},
            "no_new_work": .85, "allowances": copy.deepcopy(DEFAULT_ALLOWANCES)}


def _role(role):
    if role not in ROLES:
        raise ValueError("role must be " + ", ".join(ROLES))
    return str(role.value if isinstance(role, Role) else role)


def _chain(chain):
    if not isinstance(chain, list) or not chain or any(p not in CLI_PROVIDERS for p in chain):
        raise ValueError("chain must be a nonempty list of claude/codex")
    if len(chain) != len(set(chain)):
        raise ValueError("chain contains duplicate providers")
    return chain


def _model(value):
    if not isinstance(value, str) or "/" not in value:
        raise ValueError("expected model/effort")
    model, effort = value.rsplit("/", 1)
    if not model.strip() or any(c.isspace() for c in model) or effort not in EFFORTS:
        raise ValueError("invalid model/effort")
    return model, None if effort == "default" else effort


def _cli_model(provider, value):
    """`_model`, for one CLI provider's routing entry: the model must be one
    `CLI_MODELS` names (or `roster`) and the effort one that model offers."""
    model, effort = _model(value)
    if model == "roster":
        return model, effort
    known = CLI_MODELS[provider]
    if model not in known:
        raise ValueError(f"{model} is not a {provider} model Jarvis knows "
                         f"(it knows {', '.join(known)})")
    ladder = known[model]["efforts"]
    if effort is not None and effort not in ladder:
        offered = f"it offers {', '.join(ladder)}" if ladder else "it has no effort control"
        raise ValueError(f"{model} does not offer effort {effort!r} ({offered}; or 'default')")
    return model, effort


def load_routing(path=None):
    path = path or config.ROUTING_PATH
    result = defaults()
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return result
    if not isinstance(saved, dict) or saved.keys() - result.keys():
        raise ValueError("unknown routing configuration fields")
    for key in ("chains", "models"):
        entries = saved.get(key, {})
        if not isinstance(entries, dict):
            raise ValueError(f"{key} must be an object")
        for role, value in entries.items():
            _role(role)
            if key == "chains":
                result[key][role] = _chain(value)
            else:
                if not isinstance(value, dict) or value.keys() - set(CLI_PROVIDERS):
                    raise ValueError("models must map claude/codex to model/effort")
                for provider, setting in value.items():
                    _cli_model(provider, setting)
                result[key][role].update(value)
    fraction = saved.get("no_new_work", result["no_new_work"])
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
        raise ValueError("no_new_work must be in (0, 1]")
    result["no_new_work"] = fraction
    allowances = saved.get("allowances", {})
    if not isinstance(allowances, dict) or allowances.keys() - DEFAULT_ALLOWANCES.keys():
        raise ValueError("invalid allowance providers")
    for provider, limits in allowances.items():
        metric = next(iter(DEFAULT_ALLOWANCES[provider]))
        if (not isinstance(limits, dict) or set(limits) != {metric} or
            isinstance(limits[metric], bool) or not isinstance(limits[metric], (int, float)) or
            not 0 < limits[metric] < float("inf")):
            raise ValueError(f"{provider} allowance needs a positive {metric}")
        result["allowances"][provider] = limits
    return result


class HealthCache:
    def __init__(self, clock=time.monotonic):
        self.clock, self._values, self._lock = clock, {}, threading.RLock()

    def check(self, provider):
        with self._lock:
            key = id(provider)
            previous = self._values.get(key)
            if previous and previous[0] is provider and self.clock() - previous[1] < HEALTH_CACHE_S:
                return previous[2]
            try:
                ok, reason = provider.health()
                result = bool(ok), str(reason)
            except Exception as exc:
                result = False, f"{type(exc).__name__}: {exc}"
            self._values[key] = (provider, self.clock(), result)
            return result


HEALTH = HealthCache()


class RoutingBlocked(RuntimeError):
    def __init__(self, role, drops):
        self.role, self.drops = role, drops
        super().__init__(f"7 nothing survived: {role} blocked; " +
                         "; ".join(f"{p}: {why}" for p, why in drops.items()))


# Optional role syntax: 'use codex for reviewer'. Persist it in the journal,
# because the fixed Task.provider_override holds only a task-wide override.
_USE = re.compile(r"\buse\s+(claude|codex)\b(?:\s+for\s+(orchestrator|implementer|reviewer|researcher)\b)?", re.I)


def apply_override(task, text, stores):
    def strip(match):
        provider, role = match.group(1).lower(), match.group(2)
        if role:
            stores.tasks.journal(task.id, "owner_override", role=role.lower(), provider=provider)
        else:
            task.provider_override = ProviderName(provider)
        return ""
    return re.sub(r"[ \t]{2,}", " ", _USE.sub(strip, text)).strip(" \t\n,;")


def model_settings(role, provider, project=None, settings=None):
    settings = settings or load_routing()
    value = (project.routing.models.get(role, {}).get(provider) if project else None)
    model, effort = _model(value or settings["models"][role][provider])
    if model == "roster":
        if provider in ("claude", getattr(provider, "value", None)) or str(provider) == "claude":
            # The v1 roster holds OpenRouter ids; Claude Code takes Anthropic
            # names. "roster/default" on Claude means "no model argument" — the
            # CLI's own configured default — and effort from routing.json only.
            # (Found live: the first e2e task handed deepseek to claude.)
            return None, (effort if effort and effort != "default" else None)
        from jarvis import models
        model = models.tier("orchestrator")
        effort = effort or models.effort_for(model)
    return model, effort


def resolve(role, task, project, ledger, providers, *, brief=None, images=None,
            mcp_servers=None, health_cache=None, settings=None, exclude=(), record=True):
    """Select and durably record a fresh role. No provider session is started.

    Owner overrides lead the configured chain, with normal fallback if dropped.
    Soft preferences cannot displace a surviving owner or explicit project lead.
    """
    role = _role(role)
    stores = ledger.stores
    settings = settings or load_routing()
    health_cache = health_cache or HEALTH
    if record:
        task.brief = apply_override(task, task.brief, stores)
        stores.tasks.save(task)
    overrides = [r for r in stores.tasks.read_journal(task.id)
                 if r["event"] == "owner_override" and r.get("role") == role]
    override = overrides[-1]["provider"] if overrides else task.provider_override
    project_chain = project.routing.chains.get(role)
    chain = list(_chain(project_chain) if project_chain else settings["chains"][role])
    reasons = ["1 owner override: " + (str(override.value if isinstance(override, ProviderName) else override) if override else "none"),
               "2 project table: " + (",".join(chain) if project_chain else "inherit"),
               "3 global defaults: " + ",".join(settings["chains"][role])]
    if override:
        if override not in CLI_PROVIDERS:
            raise ValueError("owner override must be claude or codex")
        chain = [override] + [p for p in chain if p != override]
    drops, survivors, states = {}, [], {}
    profile = task.profile or project.profile
    if brief is not None and profile != PermissionProfile.STRICT:
        profile = brief.profile
    images = images or getattr(brief, "images", [])
    required = mcp_servers if mcp_servers is not None else getattr(brief, "mcp_servers", {})
    for name in chain:
        name = ProviderName(name).value
        provider = providers.get(name)
        model, effort = model_settings(role, name, project, settings)
        problems = []
        if profile == PermissionProfile.STRICT and name != "codex":
            problems.append("4 capability filter: strict requires codex")
        if images:
            if name == "claude":
                from jarvis import models
                info = models.cached_info(model)
                capable = info is None or info.vision
            else:
                capable = bool(CLI_MODELS["codex"].get(model, {}).get("vision"))
            if not capable:
                problems.append(f"4 capability filter: model {model} is not known image-capable")
        # Both merged adapters accept explicit MCP configs. Future/fake adapters
        # may narrow that set using supported_mcp_servers; no probing/start().
        supported = getattr(provider, "supported_mcp_servers", None)
        if supported is not None:
            missing = set(required) - set(supported)
            if missing:
                problems.append("4 capability filter: missing MCP servers " + ",".join(sorted(missing)))
        if name in exclude:
            problems.append("5 availability filter: current pinned provider unavailable")
        if not problems:
            ok, health_reason = health_cache.check(provider) if provider else (False, "not in provider roster")
            ledger.set_health(name, ok, health_reason)
            state = ledger.state(name, no_new_work=project.routing.no_new_work if project.routing.no_new_work is not None else settings["no_new_work"], allowances=settings["allowances"])
            states[name] = state["state"]
            if state["state"] != "available":
                problems.append(f"5 availability filter: {state['state']} ({state['reason']})")
        if problems:
            drops[name] = "; ".join(problems)
        else:
            survivors.append(name)
    reasons += [f"{name}: {reason}" for name, reason in drops.items()]
    if not survivors:
        blocked = RoutingBlocked(role, drops)
        if record:
            stores.tasks.journal(task.id, "routing_blocked", milestone="blocked", role=role,
                                 reason="; ".join(reasons + ["6 fallback chain: empty", str(blocked)]), drops=drops)
        raise blocked
    reasons += ["4 capability filter: survivors satisfy requirements",
                "5 availability filter: survivors available", "6 fallback chain: " + ",".join(survivors)]
    chosen = survivors[0]
    protected = (override if override else project_chain[0] if project_chain else None)
    if chosen != protected:
        implementers = [d.provider.value for d in task.status.routing if d.role == Role.IMPLEMENTER]
        if role == "reviewer" and implementers:
            others = [p for p in survivors if p != implementers[-1]]
            if others:
                chosen = others[0]
                reasons.append("soft preference: reviewer differs from implementer")
        if "over_threshold" in states.values() and task.status.routing:
            prior = task.status.routing[-1].provider.value
            if prior in survivors:
                chosen = prior
                reasons.append("soft preference: one provider per task while another is over_threshold")
    reasons.append("selected " + chosen)
    decision = RoutingDecision(Role(role), ProviderName(chosen), "; ".join(reasons),
                               at=datetime.now(timezone.utc).isoformat())
    if record:
        task.status.routing.append(decision)
        stores.tasks.save(task)
        model, effort = model_settings(role, chosen, project, settings)
        stores.tasks.journal(task.id, "routing_decision", **to_json(decision), model=model, effort=effort)
    return decision


class Router:
    def __init__(self, stores, providers=None, ledger=None, *, clock=time.time, health_cache=None):
        self.stores = stores
        self.providers = providers or {}
        self.ledger = ledger or UsageLedger(stores, clock=clock)
        self.clock = clock
        self.health = health_cache or HealthCache()
        self._lock = threading.RLock()

    def on_event(self, event, incoming=None):
        """Runner/event-consumer hook: account usage and handle task proposals.

        Call synchronously from the durable event path: bounded EventBus queues
        may drop records and must not be the sole accounting source.
        """
        self.ledger.on_event(event)
        return self.on_turn_finished(event, incoming)

    def place(self, explicit=None, incoming=None):
        incoming = incoming or Incoming("")
        # An archived project places nothing (decisions B1), and names match
        # ignoring case and surrounding spaces, the rule the HUD numbers by.
        live = [p for p in self.stores.projects.list() if not p.archived]
        if explicit:
            wanted = explicit.strip().casefold()
            matches = [p for p in live if p.id == explicit or p.name.strip().casefold() == wanted]
            return matches[0] if len(matches) == 1 else None
        if incoming.thread_id:
            thread = self.stores.threads.get(incoming.thread_id)
            if thread:
                project = self.stores.projects.get(thread.project_id)
                if project and not project.inbox and not project.archived:
                    return project
        if incoming.project_id:
            project = self.stores.projects.get(incoming.project_id)
            if project and not project.inbox and not project.archived:
                return project
        matches = [p for p in live if p.discord_channel_id == incoming.surface]
        return matches[0] if len(matches) == 1 else None

    def on_turn_finished(self, event, incoming=None):
        event = to_json(event) if isinstance(event, Event) else event
        proposal = event.get("data", {}).get("proposal")
        if event.get("kind") != "turn_finished" or not proposal:
            return None
        thread_id = event.get("thread_id")
        thread = self.stores.threads.get(thread_id) if thread_id else None
        if thread is None or thread.provider != ProviderName.FAST:
            return None
        incoming = incoming or Incoming("", thread_id=thread_id, project_id=event.get("project_id"))
        text = proposal["brief"].strip()
        if not text:
            raise ValueError("proposal requires a brief")
        project = self.place(proposal.get("project"), incoming)
        if project is None:
            return NeedsProject(text)
        with self._lock:
            # TURN_FINISHED may be replayed by an event consumer after restart.
            turn_id = event.get("turn_id")
            if turn_id:
                for task in self.stores.tasks.list():
                    for row in self.stores.tasks.read_journal(task.id):
                        if row["event"] == "proposed_by_fastpath" and row.get("turn_id") == turn_id and row.get("thread_id") == thread_id:
                            return row["reply"]
            provider = proposal.get("provider")
            if provider and provider not in CLI_PROVIDERS:
                raise ValueError("proposal provider must be claude or codex")
            try:
                task = self.stores.tasks.create(project.id, text, provider_override=provider)
                task.brief = apply_override(task, text, self.stores)
                self.stores.tasks.save(task)
            except ProjectArchived:
                # `place` skips archived projects; this is one archived since.
                return (f"Project {project.name} was archived, so I opened nothing. "
                        "Restore it from the HUD's Archive view to work there again.")
            summary = " ".join(task.brief.split()).rstrip(".")
            reply = f"Opened task {task.id} in {project.name}: {summary}. It will ask if anything is unclear."
            self.stores.tasks.journal(task.id, "proposed_by_fastpath", brief=text,
                                     thread_id=thread_id, turn_id=turn_id, reply=reply,
                                     not_before=self.clock() + PROPOSAL_GRACE_S)
            return reply

    def ready_to_start(self, task):
        task = self.stores.tasks.get(task.id)
        if task is None:
            return False
        rows = self.stores.tasks.read_journal(task.id)
        deadlines = [r["not_before"] for r in rows if r["event"] == "proposed_by_fastpath"]
        return task.state == TaskState.INTAKE and (not deadlines or self.clock() >= max(deadlines))

    def cancel_proposal(self, task_id):
        with self._lock:
            task = self.stores.tasks.get(task_id)
            if task is None or task.state != TaskState.INTAKE:
                return False
            rows = self.stores.tasks.read_journal(task_id)
            if not any(r["event"] == "proposed_by_fastpath" and self.clock() < r["not_before"] for r in rows):
                return False
            self.stores.tasks.transition(task_id, TaskState.CANCELLED, reason="owner cancelled within proposal grace")
            return True

    def on_steer(self, task, text):
        text = apply_override(task, text, self.stores)
        self.stores.tasks.save(task)
        return text

    def next_worker(self, task, role, **context):
        with self._lock:
            return resolve(role, task, self.stores.projects.get(task.project_id), self.ledger,
                           self.providers, health_cache=self.health, **context)

    def orchestrator_unavailable(self, task, **context):
        threads = [self.stores.threads.get(i) for i in task.thread_ids]
        current = next((t.provider.value for t in threads if t and t.role == Role.ORCHESTRATOR), None)
        project = self.stores.projects.get(task.project_id)
        try:
            suggestion = resolve("orchestrator", task, project, self.ledger, self.providers,
                                 health_cache=self.health, exclude=(current,), record=False, **context)
            fallback = suggestion.provider.value
        except RoutingBlocked:
            fallback = None
        durable = {"spec": to_json(task.spec), "plan": list(task.plan), "status": to_json(task.status),
                   "worker_reports": [r for r in self.stores.tasks.read_journal(task.id)
                                      if r["event"] in ("worker_report", "report")],
                   "thread_ids": list(task.thread_ids), "report": to_json(task.report) if task.report else None}
        text = f"Task {task.id} blocked: orchestrator {current or 'unknown'} unavailable. "
        text += (f"To restart from spec, plan, status and worker reports, use resume {task.id} on {fallback}."
                 if fallback else "No fallback is available; resume when a provider recovers.")
        self.stores.tasks.journal(task.id, "orchestrator_unavailable", milestone="blocked", reason=text)
        return text, durable

    def view(self, project_id=None):
        settings = load_routing()
        project = self._project(project_id) if project_id else None
        effective = copy.deepcopy(settings)
        if project:
            effective["chains"].update({r: c for r, c in project.routing.chains.items() if c})
            for role, models in project.routing.models.items():
                effective["models"][role].update(models)
            if project.routing.no_new_work is not None:
                effective["no_new_work"] = project.routing.no_new_work
        effective["resolved_models"] = {role: {p: dict(zip(("model", "effort"), model_settings(role, p, project, settings)))
                                              for p in CLI_PROVIDERS} for role in ROLES}
        states = {}
        for name in ProviderName:
            provider = self.providers.get(name)
            ok, reason = self.health.check(provider) if provider else (False, "not in provider roster")
            self.ledger.set_health(name, ok, reason)
            states[name.value] = self.ledger.state(name, no_new_work=effective["no_new_work"], allowances=settings["allowances"])
        decisions = [{"task_id": t.id, **to_json(d)} for t in self.stores.tasks.list()
                     if not project_id or t.project_id == project_id for d in t.status.routing]
        return {"table": effective, "states": states, "decisions": sorted(decisions, key=lambda d: d["at"])[-10:]}

    def _project(self, project_id):
        try:
            project = self.stores.projects.get(project_id)
        except StoreError as exc:
            raise ValueError(str(exc)) from exc
        if project is None:
            raise ValueError("project not found")
        return project

    def configure(self, body):
        if not isinstance(body, dict) or body.keys() - {"action", "role", "chain", "provider", "model", "project"}:
            raise ValueError("unknown route configuration fields")
        if "project" in body and (not isinstance(body["project"], str) or not body["project"]):
            raise ValueError("project must be a nonempty project id")
        action, role = body.get("action"), _role(body.get("role"))
        if action == "set":
            if set(body) - {"action", "role", "chain", "project"}:
                raise ValueError("set accepts role, chain and optional project")
            chain = body.get("chain")
            chain = chain.split(",") if isinstance(chain, str) else chain
            _chain(chain)
        elif action == "models":
            if set(body) != {"action", "role", "provider", "model"} or body.get("provider") not in CLI_PROVIDERS:
                raise ValueError("models requires role, provider and model/effort")
            _cli_model(body["provider"], body["model"])
        else:
            raise ValueError("action must be set or models")
        with _CONFIG_LOCK:
            if body.get("project"):
                project = self._project(body["project"])
                project.routing.chains[role] = chain
                self.stores.projects.save(project)
            else:
                settings = load_routing()
                if action == "set":
                    settings["chains"][role] = chain
                else:
                    settings["models"][role][body["provider"]] = body["model"]
                _write_bytes(config.ROUTING_PATH, (json.dumps(settings, indent=2) + "\n").encode())
        return self.view(body.get("project"))


def daemon_router(daemon):
    """Lazy route service; construction has no provider subprocess or inference."""
    with daemon._lock:
        if not hasattr(daemon, "router"):
            daemon.router = Router(daemon.stores, daemon.providers)
        return daemon.router


def cli(args):
    """Keep the daemon as state owner; CLI speaks the same two JSON routes."""
    import http.client
    from urllib.parse import urlencode
    body = None
    if args.action:
        if args.action == "set" and args.model is not None:
            raise RuntimeError("route set accepts role and chain only")
        body = {"action": args.action, "role": args.role}
        if args.action == "set":
            body["chain"] = args.value
            if args.project:
                body["project"] = args.project
        else:
            body.update(provider=args.value, model=args.model)
            if args.project:
                raise RuntimeError("route models is global; --project is for route set")
    conn = http.client.HTTPConnection("127.0.0.1", config.DAEMON_PORT, timeout=30)
    try:
        path = "/route" + ("?" + urlencode({"project": args.project}) if body is None and args.project else "")
        conn.request("POST" if body else "GET", path, json.dumps(body) if body else None,
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(result.get("error", "route request failed"))
        print(json.dumps(result, indent=2))
        return 0
    except OSError as exc:
        raise RuntimeError(f"cannot reach v2 daemon: {exc}") from exc
    finally:
        conn.close()

"""Carry out the exact proposal bound to an owner's durable feed answer."""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import logging
import os
import stat
import threading
import time

from ._paths import open_directory


LOG = logging.getLogger(__name__)
_ACTIONS = "actions.jsonl"
_TERMINAL = {"done", "failed", "unknown", "dismissed", "edited", "expired"}
_ANSWERED = ("approved", "edited", "dismissed", "other", "done", "expired")
_TOOLS = {"thread.start": "work_start", "thread.send": "work_send",
          "calendar.block": "calendar_create", "vault.update": "vault_add"}
_NEWS = {
    "binding": "The approval does not match the stored proposal.",
    "stale": "The proposal approval is more than 24 hours old or its time is invalid.",
    "policy": "Current policy forbids this proposal.",
    "policy_changed": "The policy level increased. The action needs a new approval.",
    "unavailable": "This action is not available yet.",
    "failed": "The proposal could not be completed.",
    "unknown": "The action may or may not have run before the executor stopped.",
}


@dataclass(frozen=True)
class Outcome:
    proposal_id: str
    kind: str
    outcome: str
    result: str | None = None


class _Refused(Exception):
    """Only fixed reason codes cross from action validation to the feed."""


class _Stopped(Exception):
    """The executor stopped before admitting this action."""


class Worker:
    """The feed callback and sweep loop share an event, never action work."""

    def __init__(self, sweep, *, stop=None):
        self._event = threading.Event()
        self._stop = threading.Event() if stop is None else stop
        self._run_sweep = sweep

    def wake(self, _proposal_id=None) -> None:
        self._event.set()

    def run(self, *, interval=60):
        self.wake()
        while not self._stop.is_set():
            self._event.wait(interval)
            self._event.clear()
            if self._stop.is_set():
                break
            try:
                self._run_sweep()
            except Exception:
                LOG.warning("Proposal sweep could not finish.")


class Executor(Worker):
    def __init__(self, store, feed, work, policy, calendar_factory=None, vault=None, *,
                 clock=time.time, stop=None):
        super().__init__(self.sweep, stop=stop)
        self._store = store
        self._feed = feed
        self._work = work
        self._policy = policy
        self._clock = clock

    def _now(self):
        return datetime.fromtimestamp(self._clock(), UTC)

    def _feed_reader(self):
        from .feed import Feed

        # Approval binding reads the durable log afresh. The shared feed's
        # incremental projection assumes an earlier byte range never changes.
        return Feed(self._feed.root, lock_timeout=self._feed._lock_timeout)

    def _answers(self):
        feed = self._feed_reader()
        by_brief, by_proposal = {}, {}
        for state in _ANSWERED:
            for brief in feed.briefs(state):
                if brief["actor"] != "jarvis" or brief["proposal_id"] is None:
                    continue
                by_brief[brief["id"]] = brief
                pid, _, _ = brief["proposal_id"].partition(":")
                by_proposal.setdefault(pid, brief)
        return by_brief, by_proposal

    @staticmethod
    def _bound(proposal, brief):
        from .proposals import action_sha256

        if brief is None or proposal["brief_id"] != brief["id"] or brief["actor"] != "jarvis":
            return False
        digest = action_sha256(proposal)
        return (proposal["action_sha256"] == digest
                and brief["proposal_id"] == f"{proposal['id']}:{digest}")

    @staticmethod
    def _approved_by_owner(brief):
        return (brief is not None and brief["resolved_by"] == "owner"
                and brief["answer"] == {"action": "approve", "text": None})

    def _authorize(self, proposal, brief, approved_at):
        from .policy import LEVELS

        if not self._approved_by_owner(brief) or not self._bound(proposal, brief):
            raise _Refused("binding")
        if approved_at is None or not timedelta(0) <= self._now() - approved_at <= timedelta(hours=24):
            raise _Refused("stale")
        kind = proposal["kind"]
        facts = {"kind": kind, "tainted": proposal["tainted"]}
        if kind == "calendar.block":
            facts.update(attendees=0, owner_event=False)
        level = self._policy.explain(facts)["level"]
        if level not in LEVELS[:3] or proposal["level"] not in LEVELS[:3]:
            raise _Refused("policy")
        if level > proposal["level"]:
            raise _Refused("policy_changed")

    def _revision(self, proposal, text=None):
        from .proposals import Draft
        from .work import _message

        pid = proposal["id"]
        for revision in self._store.proposals():
            if revision["revision_of"] == pid:
                self._store.brief(revision["id"], self._feed)
                return revision["id"]

        action = proposal["action"]
        if action is not None:
            args = action["args"].copy()
            preview = action["preview"]
            if text is not None:
                field = "brief" if action["tool"] == "work_start" else "message"
                args[field] += f"\n\nOwner's changes (take precedence)\n\n{text}"
                _message(args[field])
            if action["tool"] == "work_start":
                preview = self._work.plan_start(**args)
            action = {**action, "args": args, "preview": preview}
        # A new thread proposal gets a fresh lifetime. Calendar replacements
        # retain their original action deadline.
        brief = proposal["brief"].copy()
        if proposal["kind"] != "calendar.block":
            brief.pop("default", None)
        draft = Draft(kind=proposal["kind"], level_proposed=proposal["level_proposed"],
                      basis=proposal["basis"], brief=brief, action=action,
                      dedupe_key=proposal["dedupe_key"], project=proposal["project"],
                      tainted=proposal["tainted"], revision_of=pid)
        revision = self._store.add(draft)
        if revision is None:
            raise _Refused("policy")
        self._store.brief(revision["id"], self._feed)
        return revision["id"]

    def _observe(self, proposal, brief, directory):
        from .work import WorkError

        pid = proposal["id"]
        default = proposal["brief"]["default"]
        deadline = (datetime.fromisoformat(default["at"]) if default is not None
                    else datetime.fromisoformat(proposal["created"]) + timedelta(hours=72))
        if self._now() > deadline or (brief is not None and brief["resolved_by"] in ("default", "expiry")):
            return self._finish(directory, proposal, "expired")
        if brief is None or brief["resolved_by"] != "owner":
            return None
        answer = brief["answer"]
        if answer["action"] == "approve":
            self._store.mark(pid, "approved")
        elif (answer["action"] == "edit" and proposal["action"] is not None
              and proposal["kind"] in ("thread.start", "thread.send")):
            try:
                if not self._bound(proposal, brief):
                    raise _Refused("binding")
                revision = self._revision(proposal, answer["text"])
            except _Refused as exc:
                return self._finish(directory, proposal, "edited", reason=str(exc))
            except WorkError:
                return self._finish(directory, proposal, "edited", reason="failed")
            return self._finish(directory, proposal, "edited", revision)
        else:
            return self._finish(directory, proposal, "dismissed")
        return None

    def _check_stop(self):
        if self._stop.is_set():
            raise _Stopped

    def _perform(self, proposal, brief, approved_at):
        action = proposal["action"]
        if action is None:
            return None

        def guard():
            current = self._store.get(proposal["id"])
            if (current is None or current["state"] != "running"
                    or current["action_sha256"] != proposal["action_sha256"]):
                raise _Refused("binding")
            self._authorize(current, self._feed_reader().get(brief["id"]), approved_at)
            self._check_stop()

        tool = action["tool"]
        if _TOOLS.get(proposal["kind"]) != tool:
            raise _Refused("binding")
        if tool == "work_start":
            return self._work.start(action["preview"], guard=guard).thread_id
        if tool == "work_send":
            return self._work.send(**action["args"], guard=guard).thread_id
        raise _Refused("unavailable")

    def _execute(self, proposal, brief, directory):
        pid = proposal["id"]
        approved_at = self._store.transition_at(pid)
        try:
            self._authorize(proposal, brief, approved_at)
        except _Refused as exc:
            reason = str(exc)
        except Exception:
            reason = "failed"
        else:
            reason = None

        # Even refusals pass through the durable claim. If shutdown wins
        # before action admission, release it under the same sweep lock.
        if self._stop.is_set() or not self._store.claim(pid):
            return None
        result = None
        try:
            self._check_stop()
            if reason is None:
                result = self._perform(proposal, brief, approved_at)
        except _Stopped:
            self._store.release_claim(pid)
            return None
        except _Refused as exc:
            reason = str(exc)
        except Exception:
            reason = "failed"
        if reason is not None:
            return self._finish(directory, proposal, "failed", reason=reason)
        return self._finish(directory, proposal, "done", result)

    def _recover(self, proposal, brief, directory):
        from .work import _message

        if proposal["kind"] == "thread.start" and proposal["action"] is not None:
            try:
                matches = []
                if self._approved_by_owner(brief) and self._bound(proposal, brief):
                    plan = proposal["action"]["preview"]
                    claim = self._store.transition_at(proposal["id"]).timestamp()
                    digest = hashlib.sha256(_message(plan["brief"])).hexdigest()
                    matches = [thread for thread in self._work.threads(plan["project"])
                               if thread.started_at > claim and thread.title == plan["title"]
                               and thread.brief_sha256 == digest]
            except Exception:
                matches = []
            if len(matches) == 1:
                return self._finish(directory, proposal, "done", matches[0].thread_id)
        return self._finish(directory, proposal, "unknown", reason="unknown")

    def _finish(self, directory, proposal, state, result=None, *, reason=None):
        pid = proposal["id"]
        recorded = {"ref": result, "reason": reason} if reason else result
        self._store.mark(pid, state, result=recorded)
        return self._publish(directory, {**proposal, "state": state, "result": recorded})

    def _publish(self, directory, proposal):
        from .feed import _private_file
        from .proposals import _append
        from .work import WorkError

        pid, kind, state = proposal["id"], proposal["kind"], proposal["state"]
        result = proposal["result"]
        if isinstance(result, dict):
            reason, result = result["reason"], result["ref"]
            if reason == "policy_changed":
                try:
                    result = self._revision(proposal)
                except _Refused:
                    reason = "policy"
                except WorkError:
                    reason = "failed"
            self._feed.post({"kind": "news", "rank": 3, "ask": _NEWS[reason],
                             "idempotency_key": f"proposal-failed:{pid}"}, actor="jarvis")
        outcome = Outcome(pid, kind if proposal["action"] is not None and kind in _TOOLS else "ask",
                          state, result)
        at = self._store.transition_at(pid).timestamp()
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT
        with _private_file(directory, _ACTIONS, flags) as fd:
            _append(directory, fd, {"at": at, "proposal_id": outcome.proposal_id,
                                    "kind": outcome.kind, "outcome": state, "result": result})
        return outcome

    @staticmethod
    def _rows(directory):
        from .feed import _private_file

        try:
            with _private_file(directory, _ACTIONS, os.O_RDONLY, readonly=True) as fd:
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    return [json.loads(line) for line in stream
                            if line.endswith(b"\n") and not line.endswith(b"!\n")]
        except FileNotFoundError:
            return []

    def _sweep(self, directory):
        proposals = self._store.proposals()
        if not proposals:
            return []
        pending = any(row["state"] in ("briefed", "approved", "running") for row in proposals)
        by_brief, by_proposal = self._answers() if pending else ({}, {})
        outcomes = []

        def process(operation, *args):
            try:
                outcome = operation(*args)
            except Exception:
                LOG.warning("A proposal outcome could not be recorded.")
            else:
                if outcome is not None:
                    outcomes.append(outcome)

        def answer(proposal):
            return by_brief.get(proposal["brief_id"]) or by_proposal.get(proposal["id"])

        logged = {row["proposal_id"] for row in self._rows(directory)}
        for proposal in proposals:
            state = proposal["state"]
            if state == "briefed":
                process(self._observe, proposal, answer(proposal), directory)
            elif state == "running":
                # The directory lock rules out a live executor owning this claim.
                process(self._recover, proposal, answer(proposal), directory)
            elif state in _TERMINAL and proposal["id"] not in logged:
                # A crash after the terminal transition can leave its news or
                # audit row unwritten. Republish without re-running the action.
                process(self._publish, directory, proposal)

        for proposal in self._store.proposals("approved"):
            if self._stop.is_set():
                break
            process(self._execute, proposal, answer(proposal), directory)
        return outcomes

    def sweep(self) -> list[Outcome]:
        try:
            with open_directory(self._store.root) as directory:
                info = os.fstat(directory)
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("The proposal directory must be private.")
                # Hold recovery and execution under the same cross-instance
                # lock. The proposal log's separate flock protects each claim.
                try:
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return []
                return self._sweep(directory)
        except FileNotFoundError:
            return []

    def activity_counts(self, since, until) -> dict:
        start = since.timestamp() if isinstance(since, datetime) else since
        end = until.timestamp() if isinstance(until, datetime) else until
        try:
            with open_directory(self._store.root) as directory:
                rows = self._rows(directory)
        except FileNotFoundError:
            return {}
        return dict(Counter(row["kind"] for row in rows if row["outcome"] == "done"
                            and row["kind"] in _TOOLS and start <= row["at"] < end))

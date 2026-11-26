"""Classify v3 actions. Proposals require approval at execution under H10.

Clean owner chat can run L0 and L1 automatically when enabled.
"""

from contextlib import contextmanager, suppress
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Literal, TypedDict
from uuid import uuid4

from jarvis import config
from .index import open_directory


Level = Literal["L0", "L1", "L2", "L3"]
LEVELS = ("L0", "L1", "L2", "L3")
NEVER = frozenset({
    "credential.read", "credential.write", "policy.write", "gate.change",
    "school.submit", "data.delete_others",
})
OTHER_PEOPLE = frozenset({
    "mail.send", "mail.reply", "mail.forward", "calendar.invite", "calendar.respond",
})
MONEY = frozenset({"money.spend"})
_CALENDAR_EDITS = frozenset({
    "calendar.block_move", "calendar.event_change", "calendar.event_delete",
})
DEFAULTS: dict[str, Level] = {
    "mail.file": "L0",
    "mail.label": "L0",
    "mail.archive": "L0",
    "mail.draft": "L0",
    "vault.update": "L0",
    "research.start": "L0",
    "investigation.readonly": "L0",
    "calendar.block": "L0",
    "calendar.block_move": "L1",
    "pr.draft_open": "L1",
    "thread.fix_test": "L1",
    "calendar.add_due_date": "L1",
    "mail.send": "L2",
    "mail.reply": "L2",
    "calendar.invite": "L2",
    "calendar.respond": "L2",
    "calendar.event_change": "L2",
    "calendar.event_delete": "L2",
    "vault.archive": "L2",
    "money.spend": "L2",
    "pr.merge": "L2",
    "thread.start": "L2",
    "thread.send": "L2",
    **dict.fromkeys(NEVER, "L3"),
}
OWNER_REQUEST: dict[str, Level] = {
    "thread.start": "L1", "thread.send": "L1", "calendar.event_change": "L1",
}
_KIND = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_FACT_TYPES = {"tainted": bool, "attendees": int, "owner_event": bool, "money": bool}
_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_BYTES = 256 * 1024
_MISSING = object()


class Decision(TypedDict):
    level: Level
    why: str


def _kind(value):
    return type(value) is str and _KIND.fullmatch(value) is not None


def _level(value):
    return type(value) is str and value in LEVELS


def _same_fact(actual, expected):
    return type(actual) is type(expected) and actual == expected


def _valid_when(when):
    if type(when) is not dict:
        return False
    for name, value in when.items():
        if type(name) is not str or not name or name == "kind":
            return False
        expected = _FACT_TYPES.get(name)
        if expected is not None and type(value) is not expected:
            return False
        if type(value) not in (bool, int, str, float, type(None)):
            return False
        if type(value) is float and not math.isfinite(value):
            return False
    return True


def _valid_rule(rule):
    return (type(rule) is dict and {"kind", "level"} <= rule.keys() <= {"kind", "level", "when"}
            and _kind(rule["kind"]) and _level(rule["level"]) and _valid_when(rule.get("when", {})))


def _valid_promotion(row):
    return (type(row) is dict and row.keys() == {"kind", "from", "to", "approved_by_owner_at"}
            and _kind(row["kind"]) and _level(row["from"]) and _level(row["to"])
            and type(row["approved_by_owner_at"]) is str and bool(row["approved_by_owner_at"]))


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate policy JSON key.")
    return result


def _empty_file():
    return {"version": 1, "rules": []}


def _policy_path(path):
    path = Path(config.V3_POLICY_FILE if path is None else path).expanduser()
    if not path.name or path.name == "..":
        raise ValueError("Policy path must name a file.")
    return path


def _read_file(directory, name):
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_BYTES):
            raise ValueError("Policy must be a private, regular file within the size limit.")
        raw = stream.read(_MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > _MAX_BYTES or info.st_ctime_ns != after.st_ctime_ns:
            raise ValueError("Policy file exceeded the size limit or changed during the read.")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if (type(data) is not dict or not data.keys() <= {"version", "rules", "default_level", "promotions", "auto"}
            or type(data.get("version")) is not int or data["version"] != 1
            or type(data.get("auto", True)) is not bool
            or type(data.get("rules")) is not list or not all(map(_valid_rule, data["rules"]))
            or not _level(data.get("default_level", "L2"))
            or type(data.get("promotions", [])) is not list
            or not all(map(_valid_promotion, data.get("promotions", [])))):
        raise ValueError("Invalid policy file.")
    return data


def _default_level(data):
    return max("L2", data.get("default_level", "L2"))


def _base_decision(data, kind, facts):
    if data is None:
        return Decision(level="L2", why="malformed-file")
    undecidable = None
    has_rule = False
    for index, rule in enumerate(data["rules"]):
        if rule["kind"] != kind:
            continue
        has_rule = True
        mistyped = False
        for name, expected in rule.get("when", {}).items():
            actual = facts.get(name, _MISSING)
            if actual is _MISSING:
                break
            if type(actual) is not type(expected):
                mistyped = True
            elif actual != expected:
                break
        else:
            candidate = Decision(level=rule["level"], why=f"file:{index}")
            if not mistyped:
                decision = candidate
                break
            if undecidable is None or candidate["level"] > undecidable["level"]:
                undecidable = candidate
    else:
        if not has_rule and facts.get("owner_request") is True and kind in OWNER_REQUEST:
            decision = Decision(level=OWNER_REQUEST[kind], why=f"owner-request:{kind}")
        else:
            decision = (Decision(level=DEFAULTS[kind], why=f"default:{kind}") if kind in DEFAULTS
                        else Decision(level=_default_level(data), why="default-level"))

    if undecidable is not None and undecidable["level"] > decision["level"]:
        return Decision(level=max("L2", undecidable["level"]), why=undecidable["why"])
    return decision


def _other_people(kind):
    return kind in OTHER_PEOPLE or kind.startswith("share.")


def _apply_hard_rules(decision, kind, facts):
    if kind in NEVER:
        return Decision(level="L3", why="hard:never")
    if decision["level"] >= "L2":
        return decision
    if facts.get("tainted") is not False:
        return Decision(level="L2", why="hard:tainted")
    if _other_people(kind) or (
        kind.startswith("calendar.") and not _same_fact(facts.get("attendees"), 0)
    ):
        return Decision(level="L2", why="hard:other-people")
    if kind in MONEY or facts.get("money", False) is not False:
        return Decision(level="L2", why="hard:money")
    if kind in _CALENDAR_EDITS and facts.get("owner_event") is not False:
        return Decision(level="L2", why="hard:owner-event")
    if kind not in DEFAULTS:
        return Decision(level="L2", why="hard:unknown-kind")
    return decision


def _decide(data, kind, facts):
    base = _base_decision(data, kind, facts)
    decision = _apply_hard_rules(base, kind, facts)
    if base["why"].startswith("owner-request:") and decision != base:
        # Preserve the ordinary verdict when a hard rule rules out the owner-request level.
        return _apply_hard_rules(Decision(level=DEFAULTS[kind], why=f"default:{kind}"), kind, facts)
    return decision


def _example_facts(kind):
    facts = {"kind": kind, "tainted": False, "money": False}
    if kind.startswith("calendar."):
        facts.update(attendees=0, owner_event=False)
    return facts


class Policy:
    def __init__(self, path=None):
        self.path = path

    def _snapshot(self):
        try:
            path = _policy_path(self.path)
            with open_directory(path.parent) as directory:
                return _read_file(directory, path.name)
        except FileNotFoundError:
            return {**_empty_file(), "auto": False}
        except Exception:
            return None

    @property
    def auto(self) -> bool:
        data = self._snapshot()
        return data is not None and data.get("auto", True)

    def level_for(self, proposal) -> Level:
        return self.explain(proposal)["level"]

    def explain(self, proposal) -> Decision:
        return self._explain(self._snapshot(), proposal)

    def explain_with_auto(self, proposal) -> tuple[Decision, bool]:
        data = self._snapshot()
        return self._explain(data, proposal), data is not None and data.get("auto", True)

    @staticmethod
    def _explain(data, proposal) -> Decision:
        kind = None
        try:
            # Keep caller objects out of hashing and comparisons with policy keys.
            if type(proposal) is dict:
                facts = {key: value for key, value in proposal.items() if type(key) is str}
                kind = facts.get("kind")
                if len(facts) == len(proposal) and _kind(kind):
                    return _decide(data, kind, facts)
        except Exception:
            pass
        if _kind(kind) and kind in NEVER:
            return Decision(level="L3", why="hard:never")
        return Decision(level="L2", why="malformed-file" if data is None else "unjudgeable")

    def rules_for(self, kind):
        """Yield configured rules for this kind in their evaluation order."""
        data = self._snapshot()
        if data is not None:
            yield from (rule for rule in data["rules"] if rule["kind"] == kind)

    def table(self):
        """Yield effective levels for ordinary proposals and each file condition."""
        data = self._snapshot()
        conditions = {kind: [] for kind in DEFAULTS.keys() | OTHER_PEOPLE}
        for rule in data["rules"] if data is not None else ():
            cases = conditions.setdefault(rule["kind"], [])
            if rule.get("when"):
                cases.append(rule["when"])
        for kind, cases in sorted(conditions.items()):
            facts = _example_facts(kind)
            yield kind, {}, _decide(data, kind, facts)
            for when in cases:
                yield kind, when, _decide(data, kind, {**facts, **when})
        yield "*", {}, (Decision(level="L2", why="malformed-file") if data is None
                        else Decision(level=_default_level(data), why="default-level"))


def _replace_file(directory, name, data):
    raw = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise ValueError("Policy file would exceed the size limit.")
    tmp = ".policy-" + uuid4().hex
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=directory)


@contextmanager
def _edit_file(path):
    path = _policy_path(path)
    with open_directory(path.parent, create=True) as directory:
        # The directory inode stays stable across replacements of the file.
        fcntl.flock(directory, fcntl.LOCK_EX)
        try:
            data = _read_file(directory, path.name)
        except FileNotFoundError:
            data = _empty_file()
        except (OSError, ValueError, RecursionError) as exc:
            raise ValueError("Policy file is malformed or unreadable.") from exc
        yield data
        _replace_file(directory, path.name, data)


def set_auto(path, enabled: bool):
    """Persist the owner's chat automation setting."""
    if type(enabled) is not bool:
        raise ValueError("Automatic running must be on or off.")
    with _edit_file(path) as data:
        data["auto"] = enabled


def set_rule(path, kind, level, when=None) -> tuple[Level, Level]:
    """Persist an owner rule and return the previous and new effective levels."""
    when = when.copy() if type(when) is dict else when
    when = {} if when is None else when
    rule = {"kind": kind, "level": level, "when": when}
    if not _valid_rule(rule):
        raise ValueError("Use a lowercase area.verb kind, a level from L0 to L3, and valid facts.")
    facts = {**_example_facts(kind), **when}
    if _apply_hard_rules(Decision(level=level, why=""), kind, facts)["level"] > level:
        raise ValueError("A hard policy rule requires a higher level.")
    with _edit_file(path) as data:
        previous = _decide(data, kind, facts)["level"]
        # Replace the same selector, preserving the order of the other rules.
        remaining = [row for row in data["rules"] if row["kind"] != kind
                     or row.get("when", {}).keys() != when.keys()
                     or not all(_same_fact(row["when"][key], value) for key, value in when.items())]
        # A catch-all must follow every conditional rule it could shadow.
        data["rules"] = [rule, *remaining] if when else [*remaining, rule]
        return previous, _decide(data, kind, facts)["level"]

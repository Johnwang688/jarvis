"""Turn due reviews into one small, session-scoped Claude job per tick.

The index and consumer must use the same provider folders for doc IDs to
match. Both default to ~/.claude/projects and ~/.codex/sessions.

Lexical taint over-taints any tool with "search" in its name and misses
untrusted text reached without a URL or matching tool name, such as a file
the session read. Both limitations are accepted. Under H4 a tainted skill
still goes live if the scanner allows it, and the digest lists it first.
L3 builds the digest from reviewer/log.jsonl and the skill ledger. K4 owns
protection of the reviewer's state folder. Both are outside this module.

Corrupt backlog state is renamed and kept, and the reviewer starts empty.
Journal delivery is acknowledged only after syncing the backlog. An interrupted
job can rerun until its outcome is durably saved. The stores guard duplicate writes.
Readiness requires the index cursor to cover the queue's counted byte offset
in the same transcript. A replaced transcript starts a fresh review. Appends
only move the readiness threshold when the queue delivers a newer review.
"""

from dataclasses import dataclass
import errno
import fcntl
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import stat
import sys
import time
from uuid import uuid4

from jarvis import config, untrusted

from . import claude_jobs
from ._paths import open_directory
from .brain_mcp import (
    MEMORY_REVIEW_TOOLS as MEMORY_TOOLS,
    PROFILE_ENV,
    REVIEW_DOC_ENV,
    REVIEW_SESSION_ENV,
    REVIEW_TAINTED_ENV,
    SKILL_REVIEW_TOOLS as SKILL_TOOLS,
)
from .consolidate import _atomic_write, _read_file
from .hooks.turn_end import transcript_reference
from .index import _document_id
from .ingest.transcripts import _fingerprint
from .review_queue import (
    Consumer,
    _NS_PER_SECOND,
    _REVIEW_KEYS,
    _STATE_NAME,
    _Unusable,
    _matches_snapshot,
    _open_transcript,
    _path,
    _snapshot,
    _state_name,
    _valid_review,
    _valid_snapshot,
    credential_folder_judge,
)
from .vault import Vault


JOB = "reviewer"
MODEL = "haiku"
MAX_AGE_S = 7 * 86400
RETRY_AFTER_S = 3600
MAX_ATTEMPTS = 3
SCAN_BYTES = 64 << 20
TAINT_WORDS = ("web", "fetch", "browser", "mail", "calendar", "http", "url", "search")
LOG_KEYS = ("ts", "kind", "provider", "session", "outcome", "tainted", "input_tokens", "output_tokens")
_BACKLOG = "backlog.json"
_LOG = "log.jsonl"
_PROMPTS = Path(__file__).with_name("prompts")
_TOOLSETS = {"memory": MEMORY_TOOLS, "skill": SKILL_TOOLS}
_DEFAULT_RUN_JOB = claude_jobs.run_job
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Inspection:
    tainted: bool
    own: bool


def _transcript_fields(provider, row):
    if provider == "codex":
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return None, ()
        tools = ()
        if row.get("type") == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            tools = (payload,)
        return payload.get("cwd"), tools
    message = row.get("message")
    tools = ()
    if row.get("type") == "assistant" and isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            tools = (block for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
    return row.get("cwd"), tools


def _tool_taints(tool):
    name = tool.get("name")
    if isinstance(name, str) and any(word in name.casefold() for word in TAINT_WORDS):
        return True
    arguments = json.dumps([tool.get("input"), tool.get("arguments")])
    return "http://" in arguments or "https://" in arguments


def inspect(provider, transcript, *, snapshot=None, file_state=None) -> Inspection | None:
    """Inspect an owner transcript without following links or reading credentials."""
    if provider not in ("claude", "codex"):
        return None
    try:
        transcript = os.fspath(transcript)
        if not transcript_reference(transcript):
            return None
        path = _path(transcript)
        with _open_transcript(path, credential_folder_judge()) as (stream, info):
            if file_state is not None and file_state != _state_name(provider, path, stream, info):
                return None
            if snapshot is not None and not _matches_snapshot(stream, info, snapshot):
                return None
            # Read the snapshot without buffering past the byte limit.
            content = stream.raw.read(min(info.st_size, SCAN_BYTES))
    except (OSError, TypeError, _Unusable):
        return None

    truncated = len(content) < info.st_size
    tainted, own = truncated, False
    root = Path(os.path.abspath(config.V3_DIR))
    for line in BytesIO(content):
        if truncated and not line.endswith(b"\n"):
            break
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            cwd, tools = _transcript_fields(provider, row)
            if isinstance(cwd, str) and cwd.startswith("/") and "\0" not in cwd:
                own |= Path(os.path.abspath(cwd)).is_relative_to(root)
            tainted = tainted or any(_tool_taints(tool) for tool in tools)
        except (ValueError, RecursionError):
            continue
        if own and tainted:
            break
    return Inspection(tainted, own)


def _valid_item(item):
    return isinstance(item, dict) and item.keys() == _REVIEW_KEYS and _valid_review(item)


def _time_value(value):
    return (type(value) is int and -(1 << 63) <= value < (1 << 63)
            or type(value) is float and math.isfinite(value))


def _valid_backlog(state):
    return (isinstance(state, dict) and state.keys() == {"pending", "retry_at"}
            and (state["retry_at"] is None or _time_value(state["retry_at"]))
            and isinstance(state["pending"], list)
            and all(isinstance(row, dict)
                    and {"review", "received", "attempts"} <= row.keys()
                    <= {"review", "received", "attempts", "snapshot", "file_state"}
                    and _valid_item(row["review"]) and _time_value(row["received"])
                    and type(row["attempts"]) is int and 0 <= row["attempts"] < MAX_ATTEMPTS
                    and (row.get("snapshot") is None or _valid_snapshot(row["snapshot"]))
                    and ("file_state" not in row or (isinstance(row["file_state"], str)
                                                      and _STATE_NAME.fullmatch(row["file_state"])))
                    for row in state["pending"]))


def _load(directory):
    empty = {"pending": [], "retry_at": None}
    try:
        with _read_file(directory, _BACKLOG) as stream:
            if stream is None:
                return empty
            state = json.load(stream)
        if _valid_backlog(state):
            return state
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
    except (ValueError, RecursionError):
        pass
    os.rename(_BACKLOG, f"backlog.corrupt-{uuid4().hex}.json", src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)
    _LOGGER.warning("Reviewer set aside a corrupt backlog and will continue with an empty one.")
    return empty


def _key(review):
    return review["kind"], review["provider"], review["transcript"]


def _capture_snapshot(review):
    # Legacy backlog rows and consumers that return bare reviews have no cursor.
    try:
        with _open_transcript(_path(review["transcript"]), credential_folder_judge()) as (stream, info):
            return _snapshot(stream, info, info.st_size)
    except (OSError, _Unusable):
        return None


def _same_transcript(row, snapshot, file_state):
    if row.get("file_state") is not None and file_state is not None and row["file_state"] != file_state:
        return False
    previous = row.get("snapshot")
    if previous is None or snapshot is None or previous[:2] != snapshot[:2]:
        return False
    if previous[2] == snapshot[2]:
        return previous == snapshot
    try:
        with _open_transcript(_path(row["review"]["transcript"]), credential_folder_judge()) as (stream, info):
            return _matches_snapshot(stream, info, previous)
    except (OSError, _Unusable):
        return False


def _merge(state, batch, now):
    # Dicts preserve arrival order, including after a wall-clock rollback.
    pending = {_key(row["review"]): row for row in state["pending"]}
    for item in batch:
        if _valid_review(item):
            review = {key: item[key] for key in _REVIEW_KEYS}
            key = _key(review)
            snapshot = item["snapshot"] if "snapshot" in item else _capture_snapshot(review)
            file_state = item.get("state")
            row = pending.get(key)
            if row is None or not _same_transcript(row, snapshot, file_state):
                pending.pop(key, None)
                row = pending[key] = {"received": now, "attempts": 0}
            row["review"] = review
            row["snapshot"] = snapshot
            if file_state is not None:
                row["file_state"] = file_state
    for row in pending.values():
        if "snapshot" not in row:
            row["snapshot"] = _capture_snapshot(row["review"])
    state["pending"] = list(pending.values())


def _session(review):
    return f"{review['provider']}:{review['session_id']}"


def _prompt(review, doc, tainted):
    kind = review["kind"]
    rules = (_PROMPTS / f"review_{kind}.md").read_text(encoding="utf-8").strip()
    fields = {key: review[key] for key in ("kind", "provider", "tool_steps", "user_turns")}
    fields.update(doc=doc, tainted=tainted)
    return rules + "\n\n" + untrusted.fence(json.dumps(fields), "review document reference")


def _require_private_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Reviewer needs an owner-owned directory with mode 0700.")


def _work_directory(directory, state_path):
    try:
        os.mkdir("work", mode=0o700, dir_fd=directory)
    except FileExistsError:
        pass
    work = state_path / "work"
    fd = os.open("work", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        _require_private_directory(fd)
        if os.listdir(fd) or not os.path.samestat(os.fstat(fd), work.stat(follow_symlinks=False)):
            raise ValueError("Reviewer needs an empty working directory.")
    finally:
        os.close(fd)
    return work


class Reviewer:
    def __init__(self, *, consumer, budget, index, run_job=_DEFAULT_RUN_JOB, state_dir=None, clock=time.time):
        self._consumer = consumer
        self._budget = budget
        self._index = index
        self._run_job = run_job
        self._state_path = Path(config.V3_DIR / JOB if state_dir is None else state_dir).absolute()
        self._clock = clock

    def _job_arguments(self, directory, review, doc, tainted):
        kind = review["kind"]
        env = {
            PROFILE_ENV: f"review-{kind}",
            REVIEW_DOC_ENV: doc,
            REVIEW_SESSION_ENV: _session(review),
            REVIEW_TAINTED_ENV: str(int(tainted)),
            "PYTHONPATH": str(config.REPO_ROOT),
            "JARVIS_V3_DIR": str(config.V3_DIR),
            "JARVIS_INDEX_DB": str(self._index.path),
            "JARVIS_VAULT": str(Vault().root),
        }
        servers = {"jarvis-brain": {"command": sys.executable,
                                    "args": ["-m", "jarvis", "brain", "mcp"], "env": env}}
        return {
            "model": MODEL,
            "tools": [f"mcp__jarvis-brain__{tool}" for tool in _TOOLSETS[kind]],
            "mcp_servers": servers,
            "cwd": _work_directory(directory, self._state_path),
            "timeout": 600,
            "review": True,
        }

    def _run_admitted(self, prompt, arguments) -> tuple[str, claude_jobs.JobResult | None]:
        if self._run_job is _DEFAULT_RUN_JOB:
            cli = claude_jobs.resolve_cli()
            if not cli or not Path(cli).is_file() or not os.access(cli, os.X_OK):
                return "error", claude_jobs.JobResult(JOB, MODEL, error="Claude CLI was not found.")
            arguments["cli"] = cli
        # The runner path is ready. Charge as we enter it, so an interruption
        # after it has run still leaves a durable start record.
        admission = self._budget.admit(JOB, start_model=MODEL)
        if not admission.ok:
            self._budget.skip(JOB, admission)
            return admission.outcome, None
        try:
            try:
                result = self._run_job(JOB, prompt, **arguments)
            except (OSError, RuntimeError, ValueError):
                result = claude_jobs.JobResult(JOB, MODEL)
            self._budget.record(admission, result)
        except BaseException:
            self._budget.release(admission)
            raise
        return ("ok" if result.ok else "error"), result

    def _poll(self, directory, state, now):
        def save(batch, received):
            _merge(state, batch, received)
            _atomic_write(directory, _BACKLOG, state)

        if isinstance(self._consumer, Consumer):
            self._consumer.handoff(lambda batch, received: save(batch, received / _NS_PER_SECOND))
        else:
            save(self._consumer.tick(), now)

    def _index_ready(self, doc, transcript, snapshot):
        if snapshot is None:
            return False
        dev, ino, required, sample = snapshot
        # The ingester commits the document and its byte cursor together.
        with self._index._transaction() as db:
            cursor = db.execute("""SELECT dev, ino, offset, digest FROM cursors
                                   JOIN docs ON docs.id = cursors.file_key
                                   WHERE file_key = ?""", (doc,)).fetchone()
        if cursor is None or (cursor[0], cursor[1]) != (dev, ino) or cursor[2] < required:
            return False
        try:
            with _open_transcript(_path(transcript), credential_folder_judge()) as (stream, info):
                return ((info.st_dev, info.st_ino) == (dev, ino) and cursor[2] <= info.st_size
                        and _fingerprint(stream, cursor[2]) == cursor[3]
                        and _fingerprint(stream, required).hex() == sample)
        except (OSError, _Unusable):
            return False

    def tick(self) -> list[dict]:
        """Persist L1's batch, then try at most one job under the state lock."""
        with open_directory(self._state_path, create=True) as directory:
            _require_private_directory(directory)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return []
            now = self._clock()
            state = _load(directory)
            self._poll(directory, state, now)
            return self._process(directory, state, now)

    def _process(self, directory, state, now):
        rows = []
        for pending in state["pending"][:]:
            review = pending["review"]
            inspected = result = None
            stop, drop = False, True
            if now - pending["received"] > MAX_AGE_S:
                outcome = "expired"
            elif (inspected := inspect(review["provider"], review["transcript"], snapshot=pending["snapshot"],
                                       file_state=pending.get("file_state"))) is None:
                outcome = "gone"
            elif inspected.own:
                outcome = "own-session"
            else:
                if state["retry_at"] is not None and now < state["retry_at"]:
                    continue
                doc = _document_id(review["provider"], review["transcript"])
                if not self._index_ready(doc, review["transcript"], pending["snapshot"]):
                    continue
                # Refuse unusable log storage before spending a budget slot.
                with _read_file(directory, _LOG):
                    pass
                prompt = _prompt(review, doc, inspected.tainted)
                arguments = self._job_arguments(directory, review, doc, inspected.tainted)
                stop = True
                outcome, result = self._run_admitted(prompt, arguments)
                if result is None:
                    drop = False
                    state["retry_at"] = now + RETRY_AFTER_S
                elif not result.ok:
                    pending["attempts"] += 1
                    drop = pending["attempts"] >= MAX_ATTEMPTS
            row = {
                "ts": now, "kind": review["kind"], "provider": review["provider"],
                "session": _session(review), "outcome": outcome,
                "tainted": None if inspected is None else inspected.tainted,
                "input_tokens": 0 if result is None else result.input_tokens,
                "output_tokens": 0 if result is None else result.output_tokens,
            }
            if drop:
                state["pending"].remove(pending)
            _atomic_write(directory, _BACKLOG, state)
            _atomic_write(directory, _LOG, row, append=True)
            rows.append(row)
            if stop:
                break
        return rows

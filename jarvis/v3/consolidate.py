"""Consolidate new index documents into the personal vault.

The checkpoint follows index insertion positions. A future-dated document is
eligible as soon as it is indexed.
At-least-once replay after a successful job can spend another budget slot.
"""

from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
import fcntl
from functools import partial
import json
import math
import os
from pathlib import Path
import re
from shutil import copyfileobj
import sqlite3
import stat
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from jarvis import config, untrusted
from jarvis.v2 import redact

from . import claude_jobs
from .brain_mcp import CONSOLIDATE_TOOLS as TOOLS, PROFILE_ENV
from .index import open_directory
from .provenance import owner_authored as _owner_authored, provenance
from .vault import Vault


JOB = "consolidate"
MODEL = "sonnet"
MAX_DOCS = 150
_CHICAGO = ZoneInfo("America/Chicago")
_PROMPT = Path(__file__).with_name("prompts") / "consolidate.md"
_STATE = "state.json"
_LOG = "log.jsonl"
_STORAGE_ERRORS = (OSError, ValueError, RuntimeError, OverflowError, sqlite3.Error)
_GENERATION = re.compile(r"[a-f0-9]{32}")
_DOC_ID = re.compile(r"[a-f0-9]{64}")
_DEFAULT_RUN_JOB = claude_jobs.run_job


@dataclass(frozen=True)
class Result:
    outcome: str
    docs: int = 0
    left_out: int = 0
    note: str = ""


@dataclass(frozen=True)
class _Checkpoint:
    generation: str
    position: int
    pending: list[str]
    initial: bool = False


def due(now, *, last_run) -> bool:
    local = datetime.fromtimestamp(now, _CHICAGO)
    if (local.hour, local.minute) < (3, 30):
        return False
    return last_run is None or datetime.fromtimestamp(last_run, _CHICAGO).date() < local.date()


def select(index, *, since, until):
    docs = index.documents_between(since=since, until=until)
    # Stable partition preserves the index's newest-first order within each group.
    docs.sort(key=_owner_authored, reverse=True)
    return docs[:MAX_DOCS], max(0, len(docs) - MAX_DOCS)


def build_prompt(docs) -> str:
    lines = []
    for doc in docs:
        fields = {
            "id": doc["id"],
            "source": untrusted.one_line(redact.for_display(provenance(doc)), cap=420),
            "title": untrusted.one_line(redact.for_display(doc.get("title") or ""), cap=240) or "(untitled)",
            "time": doc["ts"],
        }
        lines.append(json.dumps(fields, ensure_ascii=False, allow_nan=False))
    fenced = untrusted.fence("\n".join(lines), "consolidation document list")
    return _PROMPT.read_text(encoding="utf-8").strip() + "\n\n" + fenced


def _state_path(state_dir) -> Path:
    return Path(config.V3_DIR / JOB if state_dir is None else state_dir).absolute()


def _check_directory(fd: int) -> None:
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Consolidation needs an owner-owned directory with mode 0700.")


@contextmanager
def _read_file(directory: int, name: str):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        yield None
        return
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("Consolidation needs owner-owned regular files with mode 0600.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(fd)


def _read_state(directory: int):
    with _read_file(directory, _STATE) as stream:
        if stream is None:
            return None
        try:
            state = json.load(stream)
        except (ValueError, RecursionError):
            raise ValueError("Invalid consolidation state.") from None
    if isinstance(state, dict):
        if (state.keys() == {"since"} and type(state["since"]) in (int, float)
                and math.isfinite(state["since"])):
            # A time cutoff cannot identify which index entries were consumed. Replay once.
            return _Checkpoint("", 0, [])
        if (state.keys() == {"generation", "position", "pending", "initial"}
                and isinstance(state["generation"], str) and _GENERATION.fullmatch(state["generation"])
                and type(state["position"]) is int and 0 <= state["position"] <= 2**63 - 1
                and isinstance(state["pending"], list)
                and all(isinstance(doc, str) and _DOC_ID.fullmatch(doc) for doc in state["pending"])
                and type(state["initial"]) is bool):
            return _Checkpoint(**state)
    raise ValueError("Invalid consolidation state.")


def _atomic_write(directory: int, name: str, row: dict, *, append: bool = False) -> None:
    payload = (json.dumps(row, allow_nan=False) + "\n").encode()
    tmp = ".consolidate-" + uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(fd, 0o600)
            if append:
                with _read_file(directory, name) as previous:
                    if previous is not None:
                        copyfileobj(previous, stream)
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
        os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=directory)


def _window(index, now, checkpoint):
    generation, position, docs = index.document_updates(
        generation=checkpoint.generation if checkpoint else None,
        position=checkpoint.position if checkpoint else 0,
        pending=checkpoint.pending if checkpoint else (),
        initial_since=now - 86_400 if checkpoint is None else None,
    )
    bootstrap = None
    if checkpoint is None or checkpoint.generation != generation:
        # Freeze the bootstrap cohort so it survives a failed or skipped first job.
        bootstrap = _Checkpoint(generation, position, [doc["id"] for doc in docs], bool(docs))
    backlog = {}
    if bootstrap is None and not checkpoint.initial:
        backlog = {doc: rank for rank, doc in enumerate(checkpoint.pending)}

    def backlog_rank(doc):
        return backlog.get(doc["id"], len(backlog))

    docs.sort(key=lambda doc: (not _owner_authored(doc), backlog_rank(doc)))
    # Preserve older cohorts ahead of newly deferred docs, oldest first within the new cohort.
    remaining = sorted(docs[MAX_DOCS:], key=lambda doc: (backlog_rank(doc), doc["ts"], doc["id"]))
    next_checkpoint = _Checkpoint(generation, position, [doc["id"] for doc in remaining])
    return docs[:MAX_DOCS], next_checkpoint, bootstrap


def preview(index, *, now=None, state_dir=None) -> tuple[int, int]:
    """Return the selected document count and prompt length in characters."""
    now = time.time() if now is None else now
    try:
        with open_directory(_state_path(state_dir)) as directory:
            _check_directory(directory)
            checkpoint = _read_state(directory)
    except FileNotFoundError:
        checkpoint = None
    docs, _, _ = _window(index, now, checkpoint)
    return len(docs), len(build_prompt(docs))


def _work_directory(directory: int, path: Path) -> Path:
    try:
        os.mkdir("work", mode=0o700, dir_fd=directory)
    except FileExistsError:
        pass
    fd = os.open("work", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        _check_directory(fd)
        if os.listdir(fd) or not os.path.samestat(os.fstat(fd), path.stat(follow_symlinks=False)):
            raise ValueError("Consolidation needs an empty working directory.")
    finally:
        os.close(fd)
    return path


def _mcp_servers(index) -> dict:
    vault = Vault().root
    try:
        with open_directory(vault) as directory:
            if os.fstat(directory).st_uid != os.getuid():
                raise ValueError("Consolidation needs an owner-owned vault directory.")
    except FileNotFoundError:
        pass  # The vault creates a missing directory on its first write.
    env = {
        PROFILE_ENV: JOB,
        "PYTHONPATH": str(config.REPO_ROOT),
        "JARVIS_V3_DIR": str(config.V3_DIR),
        "JARVIS_INDEX_DB": str(index.path),
        "JARVIS_VAULT": str(vault),
    }
    return {"jarvis-brain": {"command": sys.executable,
                            "args": ["-m", "jarvis", "brain", "mcp"], "env": env}}


def _run_selected(directory, path, index, budget, run_job, docs, next_checkpoint):
    left_out = len(next_checkpoint.pending)
    cwd = _work_directory(directory, path / "work")
    prompt = build_prompt(docs)
    servers = _mcp_servers(index)
    tools = [f"mcp__jarvis-brain__{tool}" for tool in TOOLS]
    if run_job is _DEFAULT_RUN_JOB:
        cli = claude_jobs.resolve_cli()
        if not cli or not Path(cli).is_file() or not os.access(cli, os.X_OK):
            return Result("error", len(docs), left_out, "Claude CLI was not found.")
        run_job = partial(claude_jobs.run_job, cli=cli)
    admission = budget.admit(JOB, start_model=MODEL)
    if not admission.ok:
        note = admission.reason
        try:
            budget.skip(JOB, admission)
        except (OSError, RuntimeError):
            note += " The budget ledger could not record the skip."
        return Result(admission.outcome, len(docs), left_out, note)

    try:
        try:
            job = run_job(JOB, prompt, model=MODEL, tools=tools, mcp_servers=servers, cwd=cwd, timeout=1800)
        except (OSError, RuntimeError, ValueError):
            job = claude_jobs.JobResult(JOB, MODEL)
        budget.record(admission, job)
    except BaseException:
        budget.release(admission)
        raise

    if not job.ok or job.outcome != "ok":
        note = {"timeout": "Consolidation job timed out.", "cancelled": "Consolidation job was cancelled."}
        return Result("error", len(docs), left_out, note.get(job.outcome, "Consolidation job failed."))

    _atomic_write(directory, _STATE, asdict(next_checkpoint))
    note = f"{left_out} documents exceeded the selection cap." if left_out else ""
    return Result("ok", len(docs), left_out, note)


def run(*, index, budget, now=None, state_dir=None, run_job=_DEFAULT_RUN_JOB) -> Result:
    """Run one window under a nonblocking lock on its private directory."""
    now = time.time() if now is None else now
    path = _state_path(state_dir)
    try:
        with open_directory(path, create=True) as directory:
            _check_directory(directory)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return Result("nothing")
            docs, left_out = [], 0
            try:
                checkpoint = _read_state(directory)
                docs, next_checkpoint, bootstrap = _window(index, now, checkpoint)
                left_out = len(next_checkpoint.pending)
                if not docs and bootstrap is None:
                    return Result("nothing")
                with _read_file(directory, _LOG):
                    pass
                if bootstrap is not None:
                    _atomic_write(directory, _STATE, asdict(bootstrap))
                if not docs:
                    return Result("nothing")
                result = _run_selected(directory, path, index, budget, run_job, docs, next_checkpoint)
            except _STORAGE_ERRORS:
                result = Result("error", len(docs), left_out, "Consolidation could not complete safely.")
            try:
                _atomic_write(directory, _LOG, {"ts": now, "outcome": result.outcome, "docs": result.docs,
                                               "left_out": result.left_out, "note": result.note}, append=True)
            except _STORAGE_ERRORS:
                return Result(result.outcome, result.docs, result.left_out,
                              (result.note + " The consolidation log could not be written.").strip())
            return result
    except _STORAGE_ERRORS:
        return Result("error", note="Consolidation state directory is unavailable.")

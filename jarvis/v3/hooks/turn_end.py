"""Silent turn-end notification. This also runs as a plain script.

Publication is synchronous and bounded only by the filesystem.
"""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import select
import stat
import sys
import time


MAX_ITEMS = 500
MAX_PAYLOAD = 1 << 20
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def valid_session(value):
    return isinstance(value, str) and _SESSION_ID.fullmatch(value) is not None


def absolute_reference(value):
    if not (isinstance(value, str) and len(value) <= 4096 and value.startswith("/")
            and "\0" not in value and ".." not in value.split("/")):
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeEncodeError:
        return False


def transcript_reference(value):
    return absolute_reference(value) and not value.endswith(("/", "/."))


@contextmanager
def _queue_directory(path):
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in Path(path).parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("Expected a private queue directory")
        yield fd
    finally:
        os.close(fd)


def _publish(path, item, stamp, deadline):
    with _queue_directory(path) as directory:
        # Serialize the cap check and publication among producers. The
        # consumer uses its own lock and never holds up a hook.
        while True:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.001)
        with os.scandir(directory) as entries:
            items = 0
            for entry in entries:
                items += entry.name.endswith(".json")
                if items >= MAX_ITEMS or time.monotonic() >= deadline:
                    return
        if time.monotonic() >= deadline:
            return
        suffix = os.urandom(16).hex()
        temporary = f".turn-{suffix}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(fd, 0o600)
                stream.write(json.dumps(item).encode("ascii"))
            os.replace(temporary, f"{stamp:020d}-{suffix}.json",
                       src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _stdin_payload(stream, deadline):
    try:
        fd = stream.fileno()
    except (AttributeError, OSError):
        return stream.read(MAX_PAYLOAD + 1)
    raw = bytearray()
    while len(raw) <= MAX_PAYLOAD:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            return b""
        block = os.read(fd, min(65536, MAX_PAYLOAD + 1 - len(raw)))
        if not block:
            break
        raw.extend(block)
    return raw


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON keys.")
    return result


def _bad_constant(_value):
    raise ValueError("Invalid JSON constant.")


def _item(argv, stdin, deadline):
    provider = argv[1]
    if provider == "claude":
        raw = _stdin_payload(stdin, deadline)
    elif provider == "codex" and len(argv) >= 3:
        raw = argv[-1]
    else:
        return None
    if len(raw) > MAX_PAYLOAD or (isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_PAYLOAD):
        return None
    record = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    if not isinstance(record, dict):
        return None
    transcript = None
    if provider == "claude":
        if record.get("hook_event_name") != "Stop" or record.get("stop_hook_active", False) is not False:
            return None
        session = record.get("session_id")
        transcript = record.get("transcript_path")
        if not transcript_reference(transcript):
            return None
    else:
        if record.get("type") != "agent-turn-complete":
            return None
        session = record.get("thread-id")
    cwd = record.get("cwd")
    if not valid_session(session) or not absolute_reference(cwd):
        return None
    return {"provider": provider, "session_id": session, "transcript": transcript, "cwd": cwd}


def main(argv=None, stdin=None, env=None):
    try:
        argv = sys.argv if argv is None else argv
        stdin = sys.stdin if stdin is None else stdin
        env = os.environ if env is None else env
        if "JARVIS_JOB" in env or len(argv) < 2:
            return 0
        deadline = time.monotonic() + 0.040
        base = env.get("JARVIS_V3_DIR")
        home = env.get("HOME")
        if not base and home:
            base = os.path.join(home, ".local/share/jarvis/v3")
        elif base and base.startswith("~/") and absolute_reference(home):
            base = os.path.join(home, base[2:])
        if not absolute_reference(base) or Path(base) == Path("/"):
            return 0
        item = _item(argv, stdin, deadline)
        if item is not None and time.monotonic() < deadline:
            stamp = time.time_ns()
            item["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp // 10**9))
            item["ended_at"] += f".{stamp % 10**9:09d}Z"
            _publish(os.path.join(base, "review-queue"), item, stamp, deadline)
    except BaseException:
        # A hook failure must never affect the caller's session or emit text.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

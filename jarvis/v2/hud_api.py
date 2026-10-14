"""HUD routes mounted on the daemon, plus an isolated read-only preview origin.

Every response on the HUD and API listeners refuses to be framed
(`frame_headers`, WP-E); the preview origin is framed on purpose, and every
document it serves is sandboxed by its own CSP instead."""
from __future__ import annotations

import base64
import copy
import dataclasses
import difflib
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote, urlsplit

from jarvis import config
from jarvis.tools.secrets import is_env_credential, is_env_credential_name, is_protected, scrub
from jarvis.tools.search import SKIP_DIRS as SEARCH_SKIP_DIRS
from .ledger import UsageLedger
from .model import ProviderName, Role, to_json
from . import editor_policy
from .provider import UserMessage
from . import worktrees

SKIP_DIRS = SEARCH_SKIP_DIRS | {".jarvis"}
FILE_CAP = 2 * 1024 * 1024
PATCH_CAP = 1024 * 1024
ATTACH_CAP = 4 * 1024 * 1024
LOG = logging.getLogger(__name__)
# What the HUD listener serves is `config.HUD_DIST`, read per request (the
# write tools refuse the same folder, BR-0): the newest installed build
# (`hud_install`, decision C-1), or `hud/dist` itself under JARVIS_HUD_DEV=1.

# Claude Code's own `/usage` command. Undocumented, so a failure is "not
# reported", never a number invented from the ledger. The User-Agent has to
# look like the CLI: a bare client is the bucket this endpoint 429s. The
# version is the CLI in use (`providers/claude.py` `usage_agent_version`).
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_TTL_S = 300
# Codex's account models and quota: refreshed at most once per TTL (a failure
# waits the same), and only CODEX_METADATA_BUSY_S after a turn held the lock.
CODEX_METADATA_TTL_S = 300
CODEX_METADATA_BUSY_S = 30
CODEX_LIMIT_IDS_CAP = 16
_CLAUDE_WINDOWS = (("five_hour", "5h"), ("seven_day", "week"))


class ClaudeUsageUnavailable(Exception):
    """A 429 or a transport failure. The last good reading stays."""


def _claude_credentials_path() -> Path:
    from .providers.claude import _credentials_path
    return _credentials_path()


def _claude_access_token():
    """The login, for this one request. Never logged and never returned."""
    path = _claude_credentials_path()
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except ValueError:
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None
    return token


def fetch_claude_usage(token):
    """Live call. Tests replace this whole function, so a suite never dials out.

    The token is a header and nothing else: it is not interpolated into an
    exception, a log line, or the value this returns.
    """
    import httpx
    from .providers.claude import usage_agent_version
    try:
        response = httpx.get(
            CLAUDE_USAGE_URL,
            headers={
                "Authorization": "Bearer " + token,
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
                "User-Agent": "claude-code/" + usage_agent_version(),
            },
            timeout=8,
        )
    except httpx.HTTPError:
        raise ClaudeUsageUnavailable("network")
    if response.status_code == 429:
        raise ClaudeUsageUnavailable("429")
    if response.status_code == 401:
        return None
    if response.status_code != 200:
        raise ClaudeUsageUnavailable("http")
    try:
        return response.json()
    except ValueError:
        raise ClaudeUsageUnavailable("body")


def claude_windows(body):
    """`five_hour` and `seven_day` onto the quota shape. Opus-only and
    extra-usage fields are ignored so the row stays two bars.

    `utilization` is already a percent (responses look like 74.0, not 0.74).
    """
    if not isinstance(body, dict):
        return None
    windows = []
    for key, label in _CLAUDE_WINDOWS:
        window = body.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        if type(used) not in (int, float) or isinstance(used, bool):
            continue
        if not math.isfinite(used) or used < 0:
            continue
        resets = window.get("resets_at")
        if not isinstance(resets, str):
            resets = ""
        windows.append({"name": label, "used_percent": float(used), "resets_at": resets})
    return {"windows": windows} if windows else None


def fail(status, text):
    from .daemon import APIError
    raise APIError(status, text)


def scope(project, name, *, root=None):
    """Resolve both the lexical and actual target; URL input is never proxied."""
    if not isinstance(name, str) or "\x00" in name:
        fail(400, "invalid path")
    if "://" in name or "\\" in name or ".." in Path(name).parts:
        fail(403, "path outside project scope")
    roots = [Path(root or project.root).resolve()]
    if root is None:
        roots += [Path(p).expanduser().resolve() for p in project.extra_dirs]
    raw = Path(name).expanduser()
    raw = raw if raw.is_absolute() else roots[0] / raw
    try:
        resolved = raw.resolve()
    except (OSError, RuntimeError):
        fail(403, "path cannot be resolved safely")
    for candidate in (raw, resolved):
        relative = next((candidate.relative_to(r) for r in roots if candidate.is_relative_to(r)), None)
        if relative is None or any(p in SKIP_DIRS for p in relative.parts):
            fail(403, "path outside project scope or in an excluded directory")
    return raw, resolved


def protected(raw, resolved):
    """A credential file — a bundle by name (`secrets.PROTECTED_NAMES`), a
    credential env file (`secrets.is_env_credential`, the owner's `.env*`
    rule), or anything the editor withholds (a credential folder, a login
    file, a tool's credential store, v2's session carriers: `editor_policy`)
    — as written or where it resolves. What a diff, the preview origin and an
    `@path` attachment refuse. **No gate exemption here** (review of PR #49,
    LOW2): the gate's state stays readable in the editor's own routes only,
    which ask `editor_policy` directly and also let the owner open an env
    file (and only the owner: those routes are owner-only)."""
    return (is_protected(raw) or is_protected(resolved)
            or is_env_credential(raw) or is_env_credential(resolved)
            or editor_policy.Policy().withheld_anywhere(Path(raw), Path(resolved)))


def read_file(path, limit=FILE_CAP):
    if not path.is_file():
        fail(404, "file not found")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
        stat = os.fstat(handle.fileno())
    if len(data) > limit:
        fail(413, "file exceeds size limit")
    return data, stat


def tree(project, name, depth):
    """A directory's entries, `depth` levels down.

    A row the editor must treat specially says so before anything is read
    (`editor_policy.Policy.tree_flag`): `policy: "withheld"` (a credential
    file or folder — never opened, and a withheld folder is never walked) or
    `policy: "env"` (a credential env file: the owner's to edit, Jarvis's
    never to read). Inside a withheld folder only the gate's own state files
    are listed (E-8 keeps them readable), and the folders on the way to them."""
    if not 0 <= depth <= 20:
        fail(400, "depth must be between 0 and 20")
    raw_dir, directory = scope(project, name)
    if not directory.is_dir():
        fail(404, "directory not found")
    policy = editor_policy.Policy()
    entries = []

    def walk(parent, remaining, ancestors, only):
        if not remaining:
            return
        for child in sorted(parent.iterdir(), key=lambda p: p.name):
            if child.name in SKIP_DIRS:
                continue
            try:
                raw, actual = scope(project, str(child))
            except Exception as exc:
                from .daemon import APIError
                if isinstance(exc, APIError):
                    continue
                raise
            if not actual.exists():
                continue
            if only is not None and not any(actual == g or actual in g.parents for g in only):
                continue
            stat = actual.stat()
            is_dir = actual.is_dir()
            if not is_dir and not actual.is_file():
                continue
            flag = policy.tree_flag(raw, actual, is_dir=is_dir)
            row = dict(name=str(child.relative_to(directory)), kind="dir" if is_dir else "file",
                       size=stat.st_size, mtime=stat.st_mtime)
            inner = only
            if flag == editor_policy.WITHHELD:
                gates = policy.gate_inside(actual) if is_dir else set()
                if not gates:
                    row["policy"] = flag
                    entries.append(row)
                    continue
                inner = gates  # only the way to the gate's own files
            elif flag:
                row["policy"] = flag
            entries.append(row)
            if is_dir and actual not in ancestors:
                walk(child, remaining - 1, ancestors | {actual}, inner)
    top = None
    if policy.is_withheld(raw_dir, directory):
        top = policy.gate_inside(directory)
        if not top:
            fail(403, "a credential folder: its contents are never listed")
    walk(directory, depth, {directory}, top)
    return {"path": name, "entries": entries}


# -- the editor's files (ED-2, editor plan §2.4) ---------------------------------
#
# A read returns the bytes' `etag` (the first 128 bits of their SHA-256) and the
# file's absolute path (`abs`, where it resolves: one buffer per file on disk,
# however many projects reach it). A save says which bytes it was made against
# (`expected_etag`; null means "must not exist"), and the daemon hashes what is
# on disk now, under its lock, before it writes: a mismatch is a 409 carrying
# the current etag, and the file is left byte-identical. An mtime cannot do
# this — a rewrite that restores the old mtime (`os.utime`, or a same-second
# rewrite on the 9p `/mnt/c` mount) passed it, and a `touch` that changed
# nothing failed it. `expected_mtime` is still accepted for one release.

ETAG_RE = re.compile(r"[0-9a-f]{32}")
STAT_CAP = 100
_ETAGS: dict[str, tuple[tuple, str]] = {}
_ETAGS_LOCK = threading.Lock()
_ETAGS_MAX = 4096


def _stat_key(st) -> tuple:
    # More than (mtime_ns, size): the inode and ctime too, so a file replaced
    # by a rename, or rewritten and `utime`d back, is hashed again.
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _cached_etag(path: Path, st) -> str | None:
    """The etag of `path`, hashed again only when its stat moved."""
    key, name = _stat_key(st), str(path)
    with _ETAGS_LOCK:
        hit = _ETAGS.get(name)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with path.open("rb") as handle:
            data = handle.read(FILE_CAP + 1)
            now = _stat_key(os.fstat(handle.fileno()))
    except OSError:
        return None
    if len(data) > FILE_CAP:
        return None
    tag = editor_policy.etag(data)
    with _ETAGS_LOCK:
        if len(_ETAGS) >= _ETAGS_MAX:
            _ETAGS.clear()
        _ETAGS[name] = (now, tag)
    return tag


def _editor_target(project, name):
    raw, target = scope(project, name)
    policy = editor_policy.Policy()
    return raw, target, policy.classify(raw, target)


def read_for_editor(project, name):
    """`GET /projects/{id}/file`: the file, its etag and what the editor may do.

    `kind` is `editor_policy`'s; `editable` is whether the editor may save it
    (the HUD follows it, never the kind alone: an env file that is not UTF-8
    is `env` and not editable); `readonly` is the sentence the editor shows
    beside its lock (null when the file is editable and Jarvis may read it).
    A withheld file comes back with no content and no etag, ever."""
    raw, target, verdict = _editor_target(project, name)
    if verdict.withheld:
        return dict(path=name, abs=str(target), kind=verdict.kind, readonly=verdict.reason,
                    editable=False, protected=True)
    data, stat = read_file(target)
    verdict = editor_policy.classify_bytes(verdict, data)
    text = editor_policy.is_text(data)
    content = data.decode("utf-8", errors="replace")
    return dict(path=name, abs=str(target), content=content, etag=editor_policy.etag(data),
                mtime=stat.st_mtime, mtime_ns=stat.st_mtime_ns, size=stat.st_size,
                encoding=("utf-8-bom" if data.startswith(b"\xef\xbb\xbf") else "utf-8") if text else None,
                eol=editor_policy.eol_of(content) if text else None,
                kind=verdict.kind, editable=verdict.editable,
                readonly=verdict.reason if verdict.kind != editor_policy.TEXT else None,
                protected=False)


def _atomic_write(target: Path, data: bytes, mode: int, *, exclusive: bool) -> bool:
    """Write beside the file and rename over it: never half a file on disk.

    The temporary file is 0600 from its first byte and takes `mode` before the
    rename, so an env file is never world-readable for an instant. With
    `exclusive` the file must not exist: the new name is a hard link, which
    fails rather than replaces (a file created in between is kept, and False
    comes back)."""
    import tempfile
    target.parent.mkdir(parents=True, exist_ok=True)
    # Named after the file itself (`.env.production.<x>.tmp`), so a temp of
    # an env file is an env file by name too — credential to every rule, and
    # ignored by a `.env*` gitignore line (review of PR #49, LOW4).
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        if exclusive:
            try:
                os.link(tmp, target)
            except FileExistsError:
                return False
            except OSError:
                # A filesystem without hard links: the check under the lock
                # is all there is.
                if os.path.lexists(target):
                    return False
                os.replace(tmp, target)
        else:
            os.replace(tmp, target)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def save_for_editor(daemon, project, body):
    """`PUT /projects/{id}/file`: `{path, content, expected_etag}`.

    Refused (403) for every kind the editor may not write — withheld, the
    gate's state, the safety layer — and for a file on disk that is not text
    (the editor never overwrites bytes it could not show exactly); 409 with
    the current `etag` (null: the file is gone) when the bytes on disk are not
    the ones the save was made against. A file's mode is kept; a new file is
    0644, a new env file 0600. An unexpected error from the disk is a 500
    (permission denied, a 403), never the dispatcher's 409, which the HUD
    reads as a conflict."""
    from .daemon import _object
    body = _object(body, ("path", "content", "expected_etag", "expected_mtime"), ("path", "content"))
    has_etag, has_mtime = "expected_etag" in body, "expected_mtime" in body
    if has_etag == has_mtime:
        fail(400, "send exactly one of expected_etag (or null: the file must not exist) and expected_mtime")
    if not isinstance(body["path"], str) or not isinstance(body["content"], str):
        fail(400, "path and content must be strings")
    expected = body.get("expected_etag")
    if has_etag and expected is not None and not (isinstance(expected, str) and ETAG_RE.fullmatch(expected)):
        fail(400, "expected_etag must be 32 lowercase hex digits or null")
    expected_mtime = body.get("expected_mtime")
    if has_mtime and expected_mtime is not None and (
            type(expected_mtime) not in (int, float) or not math.isfinite(expected_mtime)):
        fail(400, "expected_mtime must be a number or null")
    try:
        data = body["content"].encode("utf-8")
    except UnicodeEncodeError:
        fail(400, "content is not valid Unicode text")
    if len(data) > FILE_CAP:
        fail(413, "file exceeds 2 MB")
    try:
        with daemon._lock:
            daemon._active()
            return _save_locked(project, body, data, has_etag, expected, expected_mtime)
    except PermissionError as exc:
        # The filesystem said no: not a conflict (a 409 is the conflict bar).
        fail(403, f"the file could not be written: permission denied ({type(exc).__name__})")
    except OSError as exc:
        # Anything else unexpected from the disk is the daemon's failure, not
        # a conflict the owner could resolve: 500, never the dispatcher's 409.
        fail(500, f"the file could not be written ({type(exc).__name__})")


def _save_locked(project, body, data, has_etag, expected, expected_mtime):
    from .permissions import denied_file
    raw, target, verdict = _editor_target(project, body["path"])
    if not verdict.editable:
        fail(403, "read-only here: " + (verdict.reason or "this file cannot be written from the editor"))
    if verdict.kind != editor_policy.ENV and (denied_file(str(raw)) or denied_file(str(target))):
        fail(403, "protected file cannot be written")
    if target.exists() and not target.is_file():
        fail(400, "target is not a regular file")
    current = st = None
    if target.exists():
        with target.open("rb") as handle:
            current = handle.read(FILE_CAP + 1)
            st = os.fstat(handle.fileno())
        if len(current) > FILE_CAP:
            fail(413, "file on disk exceeds 2 MB")
        if not editor_policy.is_text(current):
            fail(403, "read-only here: the file on disk is not UTF-8 text, and the editor never "
                      "rewrites bytes it could not show exactly")
    if has_etag:
        now = editor_policy.etag(current) if current is not None else None
        if now != expected:
            return 409, {"error": ("file is gone since it was read" if now is None
                                   else "file exists" if expected is None
                                   else "file changed on disk since it was read"),
                         "etag": now}
    else:
        if (st.st_mtime if st is not None else None) != expected_mtime:
            fail(409, "file changed since it was read")
    if st is not None:
        mode = st.st_mode & 0o7777
    else:
        mode = 0o600 if verdict.kind == editor_policy.ENV else 0o644
    if not _atomic_write(target, data, mode, exclusive=current is None):
        # Created by someone else between the check and the write.
        return 409, {"error": "file exists", "etag": _cached_etag(target, target.stat())}
    after = target.stat()
    tag = editor_policy.etag(data)
    with _ETAGS_LOCK:
        _ETAGS[str(target)] = (_stat_key(after), tag)
    return 200, {"etag": tag, "abs": str(target), "mtime": after.st_mtime,
                 "mtime_ns": after.st_mtime_ns, "size": after.st_size}


def stat_for_editor(project, body):
    """`POST /projects/{id}/files/stat {paths}`: where each open file stands.

    At most 100 paths. Per path: `{etag, mtime_ns, size, kind}`, or
    `{gone: true}`, or `{kind: "withheld"}`, or `{error: <status>}` for a path
    the project cannot reach; `etag` is null for a file over the editor's
    2 MB. A file is hashed again only when its stat moved — the HUD asks every
    2 s while it is visible, because inotify cannot see a write made from
    Windows on the 9p `/mnt/c` mount, where VEX projects live."""
    from .daemon import APIError, _object
    body = _object(body, ("paths",), ("paths",))
    paths = body["paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        fail(400, "paths must be a list of strings")
    if len(paths) > STAT_CAP:
        fail(413, f"at most {STAT_CAP} paths at once")
    policy = editor_policy.Policy()
    out = {}
    for name in dict.fromkeys(paths):
        try:
            raw, target = scope(project, name)
        except APIError as exc:
            out[name] = {"error": exc.status}
            continue
        verdict = policy.classify(raw, target)
        if verdict.withheld:
            out[name] = {"kind": verdict.kind}
            continue
        try:
            st = target.stat()
        except FileNotFoundError:
            out[name] = {"gone": True}
            continue
        except OSError:
            out[name] = {"error": 403}
            continue
        if not target.is_file():
            out[name] = {"gone": True}
            continue
        tag = _cached_etag(target, st) if st.st_size <= FILE_CAP else None
        out[name] = {"etag": tag, "mtime_ns": st.st_mtime_ns, "size": st.st_size, "kind": verdict.kind}
    return {"files": out}


def _picker_allowed(path):
    return (path.is_relative_to(Path.home().resolve())
            or bool(re.match(r"^/mnt/[a-zA-Z](?:/|$)", str(path))))


def directories(name):
    """List only directories whose lexical and resolved paths are in scope."""
    if not isinstance(name, str) or "\x00" in name:
        fail(400, "invalid path")
    raw = Path(name)
    if not raw.is_absolute() or ".." in raw.parts or not _picker_allowed(raw):
        fail(403, "directory outside allowed roots")
    try:
        directory = raw.resolve()
    except (OSError, RuntimeError):
        fail(403, "directory cannot be resolved safely")
    if not _picker_allowed(directory):
        fail(403, "directory outside allowed roots")
    if not directory.is_dir():
        fail(404, "directory not found")
    names = []
    for child in directory.iterdir():
        if child.name.startswith("."):
            continue
        try:
            actual = child.resolve()
            if _picker_allowed(actual) and actual.is_dir():
                names.append(child.name)
        except (OSError, RuntimeError):
            continue
    parent = directory.parent
    return {"path": str(directory), "parent": str(parent) if _picker_allowed(parent) else None,
            "dirs": sorted(names)}


def assemble_turn(project, body, *, paths: bool = True, owner: bool = False, captures=None):
    """v1 fences/notes/caps, with project-scoped @paths and credential names refused.

    `paths=False` (a message typed in Discord, PR C) takes the attachments
    under the same caps and refusals but never reads an `@path` from disk."""
    from .daemon import _object
    _object(body, ("text", "images", "attachments", "spoken"), ("text",))
    typed = body["text"]
    if not isinstance(typed, str):
        fail(400, "text must be a string")
    # Dictation (PR C): the Discord mirror shows it as "You (HUD, voice)".
    spoken = body.get("spoken", False)
    if not isinstance(spoken, bool):
        fail(400, "spoken must be a boolean")
    attachments, images = body.get("attachments", []), body.get("images", [])
    if not isinstance(attachments, list) or not isinstance(images, list):
        fail(400, "images and attachments must be lists")
    combined = []
    for img in images:
        _object(img, ("b64", "mime"), ("b64", "mime"))
        if not isinstance(img["mime"], str) or not img["mime"].startswith("image/"):
            fail(400, "image mime must be image/*")
        combined.append(dict(name="image", mime=img["mime"], data_b64=img["b64"], direct=True))
    for item in attachments:
        _object(item, ("name", "mime", "data_b64"), ("name", "mime", "data_b64"))
        combined.append(item)
    notes = []
    for token in (re.findall(r"(?:(?<=\s)|^)@(\S+)", typed) if paths else ()):
        name = token.rstrip(".,;:!?)\"'")
        try:
            raw, target = scope(project, name)
            if protected(raw, target):
                notes.append(f"[{name} holds live credentials — not attached]")
                continue
            if not target.is_file():
                if "/" in name:
                    notes.append(f"[{name} not found — not attached]")
                continue
            # Bound reads even for dropped attachments.
            data, _ = read_file(target, ATTACH_CAP)
            mime = mimetypes.guess_type(name)[0] or "text/plain"
            combined.append(dict(name=name, mime=mime, data_b64=base64.b64encode(data).decode()))
        except Exception as exc:
            from .daemon import APIError
            if not isinstance(exc, (APIError, OSError)):
                raise
            notes.append(f"[{name}: unavailable or outside scope — not attached]")
    result_images, blocks, names = [], [], []
    for item in combined[:8]:
        if any(not isinstance(item.get(k), str) for k in ("name", "mime", "data_b64")):
            fail(400, "attachment fields must be strings")
        # Opaque owner-minted reference, not file data despite the legacy
        # field name. Resolve before base64 decoding and canonicalize its
        # mirror name so page metadata never reaches Discord.
        from .browser.captures import MIME as CAPTURE_MIME, OWNER_ONLY, UNAVAILABLE
        if item["mime"] == CAPTURE_MIME:
            names.append("page text")
            blocks.append(captures.resolve(item["data_b64"], owner=owner)
                          if captures is not None else (UNAVAILABLE if owner is True else OWNER_ONLY))
            continue
        name, mime = (item["name"] or "attachment")[:200], item["mime"] or "text/plain"
        if (is_protected(item["name"].replace("\\", "/"))
                or is_env_credential_name(item["name"].replace("\\", "/").rsplit("/", 1)[-1])
                or editor_policy.credential_name(item["name"])):
            notes.append(f"[{name} holds live credentials — not attached]")
            continue
        try:
            data = base64.b64decode(item["data_b64"], validate=True)
        except ValueError:
            notes.append(f"[{name}: unreadable attachment data]")
            continue
        if len(data) > ATTACH_CAP:
            notes.append(f"[{name} is over 4MB — not attached]")
            continue
        if not item.get("direct"):
            names.append(name)
        if mime.startswith("image/"):
            result_images.append({"b64": base64.b64encode(data).decode(), "mime": mime})
            if not item.get("direct"):
                blocks.append(f"[attached image: {name}]")
        else:
            text = data.decode("utf-8", errors="replace")
            suffix = "\n[truncated]" if len(text) > 100_000 else ""
            blocks.append(f"[attached file: {name}]\n```\n{scrub(text[:100_000])}\n```{suffix}")
    if len(combined) > 8:
        notes.append(f"[{len(combined) - 8} attachment(s) over the 8-per-turn cap were dropped]")
    text = "\n\n".join(([typed.strip()] if typed.strip() else []) + blocks + notes)
    if not text:
        fail(400, "text or attachments required")
    # `typed` is the owner's own words: the Discord mirror shows those and the
    # attachment names, never what was inlined (PR C, plan §4.3).
    return UserMessage(text, result_images, via="hud", typed=typed.strip(),
                       attachments=names, spoken=spoken)


class HUDLedger(UsageLedger):
    """Keep sparse provider reports in ledger rows without changing its interface."""
    def __init__(self, *args, **kwargs):
        self.rate_limits = {}
        self._reported = None
        self._claude_quota = None
        self._claude_quota_at = 0.0
        # The Codex account metadata refresh (models + quota). `_codex_next`
        # is when the next one may start — None until the first (never a 0.0
        # compared against a monotonic clock, which could be under the TTL for
        # minutes after boot). One runs at a time, on its own thread.
        self.metadata_clock = time.monotonic
        self._codex_next: float | None = None
        self._codex_worker: threading.Thread | None = None
        # Set by `stop_codex` (daemon shutdown) under `_codex_gate`, which a
        # refresh also holds while it installs what it read: once stopped,
        # nothing is saved or swapped, however late the read returns.
        self._codex_stopped = threading.Event()
        self._codex_gate = threading.Lock()
        super().__init__(*args, **kwargs)

    def _merge_rate_limits(self, key, report):
        """Fold one sparse snapshot into the meters: a null never clears a
        known value, and a limit id not in this snapshot is kept."""
        previous = self.rate_limits.setdefault(key, {})
        for k, v in report.items():
            if v is not None:
                if isinstance(v, dict) and isinstance(previous.get(k), dict):
                    previous[k].update({a: b for a, b in v.items() if b is not None})
                else:
                    previous[k] = copy.deepcopy(v)

    def _apply(self, row):
        super()._apply(row)
        if isinstance(row.get("rate_limits"), dict):
            # Notifications are sparse; null metadata never clears known values.
            self._merge_rate_limits(row["rate_limits"].get("limitId") or "codex", row["rate_limits"])

    def _append(self, row):
        if self._reported is not None and row["provider"] == "codex":
            row["rate_limits"] = self._reported
        super()._append(row)

    def on_event(self, event, **kwargs):
        from .model import to_json
        from .provider import Event
        record = to_json(event) if isinstance(event, Event) else event
        with self._lock:
            self._reported = (record.get("data", {}).get("provider_reported") or {}).get("rate_limits")
            try:
                return super().on_event(record, **kwargs)
            finally:
                self._reported = None

    def quota(self, provider):
        if provider == "claude":
            return self._claude_subscription()
        if provider != "codex":
            return None
        windows = []
        with self._lock:
            for limit_id, report in self.rate_limits.items():
                for key in ("primary", "secondary"):
                    window = report.get(key)
                    if not isinstance(window, dict):
                        continue
                    used = window.get("usedPercent")
                    if type(used) not in (float, int) or not math.isfinite(used) or used < 0:
                        continue
                    minutes = window.get("windowDurationMins")
                    label = {300: "5h", 10080: "weekly"}.get(minutes, key)
                    if limit_id != "codex":
                        label = f"{report.get('limitName') or limit_id}: {label}"
                    windows.append(dict(name=label, used_percent=used, resets_at=window.get("resetsAt")))
        return {"windows": windows} if windows else None

    def refresh_codex(self, provider, on_change=None) -> bool:
        """Start a background refresh of Codex's models and quota when one is
        due; never wait for it. True when one was started.

        The caller — a HUD read — serves the snapshot it already has; a
        refresh that changed something calls `on_change` (the daemon publishes
        `codex_metadata`, and the HUD re-reads). At most one runs at a time.
        A success or a failure waits `CODEX_METADATA_TTL_S` before the next;
        a busy provider (a Codex turn holds its login lock — `MetadataBusy`)
        only `CODEX_METADATA_BUSY_S`. Nothing here starts a model turn.
        """
        read = getattr(provider, "account_metadata", None)
        if not callable(read) or self._codex_stopped.is_set():
            return False
        with self._lock:
            now = self.metadata_clock()
            if self._codex_next is not None and now < self._codex_next:
                return False
            if self._codex_worker is not None and self._codex_worker.is_alive():
                return False
            # Reserve the slot before the thread exists, so a second request
            # arriving now neither starts another nor finds nothing running.
            self._codex_next = now + CODEX_METADATA_TTL_S
            worker = threading.Thread(target=self._refresh_codex, args=(read, on_change),
                                      name="jarvis-codex-metadata", daemon=True)
            self._codex_worker = worker
        worker.start()
        return True

    def wait_codex_refresh(self, timeout: float | None = None) -> bool:
        """Block until the refresh in flight (if any) is done; False on
        timeout. For tests and shutdown — request handlers never wait."""
        worker = self._codex_worker
        if worker is not None:
            worker.join(timeout)
            return not worker.is_alive()
        return True

    def stop_codex(self, timeout: float | None = None) -> bool:
        """Daemon shutdown: no refresh starts, none in flight saves the
        catalog or swaps the table or the meters from here on, and the one in
        flight (if any) is waited for up to `timeout`. True when none is left
        running. The caller cancels the provider's app-server first
        (`CodexProvider.cancel_metadata`), so the wait is short."""
        with self._codex_gate:
            self._codex_stopped.set()
        return self.wait_codex_refresh(timeout)

    def _refresh_codex(self, read, on_change) -> None:
        from .providers.codex import MetadataBusy
        if self._codex_stopped.is_set():
            return
        try:
            metadata = read()
        except MetadataBusy:
            with self._lock:
                self._codex_next = self.metadata_clock() + CODEX_METADATA_BUSY_S
            return
        except Exception as exc:
            # The TTL set at the start throttles a failure too; the last good
            # catalog and meters stand.
            LOG.warning("Codex metadata refresh failed (%s)", type(exc).__name__)
            return
        if not isinstance(metadata, dict):
            LOG.warning("Codex metadata refresh failed (%s)", type(metadata).__name__)
            return
        changed = False
        with self._codex_gate:
            if self._codex_stopped.is_set():
                return          # the daemon stopped while this read ran
            # The two halves are independent: either may be None (it failed)
            # and the other still lands.
            rows = metadata.get("models")
            if rows is not None:
                try:
                    from .router import set_codex_models
                    set_codex_models(rows)          # installs it and saves it
                except (TypeError, ValueError) as exc:
                    LOG.warning("Codex model catalog refused (%s)", type(exc).__name__)
                else:
                    changed = True
            reports = _codex_rate_limits(metadata.get("rate_limits"))
            if reports:
                # Merged, never swapped: an empty or partial read keeps the
                # windows (and the limit ids turn notifications taught us).
                with self._lock:
                    for limit_id, report in reports.items():
                        self._merge_rate_limits(limit_id, report)
                changed = True
        if changed and on_change is not None:
            try:
                on_change()
            except Exception as exc:
                LOG.warning("Codex metadata notification failed (%s)", type(exc).__name__)

    def _claude_subscription(self):
        """The subscription windows, cached. A miss is null, never a guess.

        No credentials, a 401, or a body without the two windows is null.
        A 429 or a network error keeps the last good reading, because this
        runs on every `usage_updated` and the endpoint punishes polling.
        """
        now = time.monotonic()
        with self._lock:
            cached = self._claude_quota
            fresh = cached is not None and (now - self._claude_quota_at) < CLAUDE_USAGE_TTL_S
        if fresh:
            return cached
        token = _claude_access_token()
        if not token:
            with self._lock:
                self._claude_quota = None
                self._claude_quota_at = 0.0
            return None
        try:
            body = fetch_claude_usage(token)
        except ClaudeUsageUnavailable:
            return cached
        if body is None:
            parsed = None
        else:
            parsed = claude_windows(body)
        if not parsed:
            with self._lock:
                self._claude_quota = None
                self._claude_quota_at = 0.0
            return None
        with self._lock:
            self._claude_quota = parsed
            self._claude_quota_at = time.monotonic()
        return parsed


def usage(daemon):
    # Only the allowances and the no-new-work threshold: never the routing
    # table, so a table at odds with the Codex catalog cannot blank the
    # status column (PR #20 review).
    from .router import daemon_router, usage_settings
    router = daemon_router(daemon)
    settings = usage_settings()
    # Kicks off a background refresh when one is due; this read serves the
    # meters as they are now.
    _refresh_codex_metadata(daemon)
    result = {}
    for provider in ProviderName:
        instance = daemon.providers.get(provider)
        ok, reason = router.health.check(instance) if instance else (False, "provider not in roster")
        router.ledger.set_health(provider.value, ok, reason)
        state = router.ledger.state(provider.value, no_new_work=settings["no_new_work"],
                                    allowances=settings["allowances"])
        quota = router.ledger.quota(provider.value) if isinstance(router.ledger, HUDLedger) else None
        result[provider.value] = dict(state=state["state"], reason=state["reason"], today=state["totals"],
                                      allowance=settings["allowances"][provider.value], quota=quota)
    return {"providers": result}


def _codex_rate_limits(body) -> dict[str, dict] | None:
    """Normalize `account/rateLimits/read` into HUDLedger's keyed snapshots;
    None when it carries no report, so an empty read changes nothing. At most
    CODEX_LIMIT_IDS_CAP limit ids are taken from one read."""
    if not isinstance(body, dict):
        return None
    reports = {}
    by_id = body.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, report in list(by_id.items())[:CODEX_LIMIT_IDS_CAP]:
            if isinstance(limit_id, str) and limit_id and isinstance(report, dict):
                reports[limit_id] = copy.deepcopy(report)
                reports[limit_id].setdefault("limitId", limit_id)
    current = body.get("rateLimits")
    if isinstance(current, dict):
        limit_id = current.get("limitId") or "codex"
        if isinstance(limit_id, str) and limit_id and limit_id not in reports:
            reports[limit_id] = copy.deepcopy(current)
    return reports or None


def _refresh_codex_metadata(daemon) -> None:
    """Start a background Codex models/quota refresh if one is due. Never
    blocks: the handler answers from what it has, and a refresh that changed
    something publishes `codex_metadata` so the HUD re-reads."""
    from .router import daemon_router
    ledger = daemon_router(daemon).ledger
    if isinstance(ledger, HUDLedger):
        ledger.refresh_codex(daemon.providers.get(ProviderName.CODEX),
                             on_change=lambda: daemon.bus.publish(
                                 {"kind": "codex_metadata", "data": {"provider": "codex"}}))


def _scrub_source(text):
    # v1 scrub normalizes lines and removes the final newline; restore it so
    # the diff editor does not invent a missing-newline change.
    return scrub(text) + ("\n" if text.endswith("\n") else "")


def _git(root, *args):
    return worktrees._git(root, "--literal-pathspecs", *args)


def _diff_context(daemon, task):
    if not task.worktree or not Path(task.worktree).is_dir() or not task.branch:
        fail(409, "task has no git worktree")
    root = Path(task.worktree).resolve()
    base = worktrees._entry(task, daemon.stores).get("base_ref")
    if not base:
        fail(409, "task has no recorded base ref")
    ancestor = _git(root, "merge-base", base, "HEAD").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    project = daemon.require(daemon.stores.projects, task.project_id)
    return root, base, ancestor, head, project


def _diff_paths(root, ancestor):
    tokens = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-z", "-M", ancestor, "--").split("\0")
    rows = []
    while tokens and tokens[0]:
        status, old = tokens.pop(0), tokens.pop(0)
        name = tokens.pop(0) if status.startswith(("R", "C")) else old
        rows.append(("R" if status.startswith(("R", "C")) else status[0], old, name))
    rows += [("A", name, name) for name in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if name]
    return rows


def diff(daemon, task, name=None):
    root, base, ancestor, head, project = _diff_context(daemon, task)
    rows = _diff_paths(root, ancestor)
    def allowed(path):
        raw, actual = scope(project, path, root=root)
        if protected(raw, actual):
            fail(403, "protected file")
        return actual
    if name is not None:
        actual = allowed(name)
        raw, _ = scope(project, name, root=root)
        rel = str(raw.relative_to(root))
        old = next((old for _, old, new in rows if new == rel), rel)
        allowed(old)
        # Symlinks are displayed as link text, never followed into another file.
        exists = _git(root, "ls-tree", "-z", ancestor, "--", old)
        before = _git(root, "show", f"{ancestor}:{old}") if exists else ""
        raw = root / rel
        if raw.is_symlink():
            after = str(raw.readlink())
        elif actual.exists():
            data, _ = read_file(actual)
            after = data.decode("utf-8", errors="replace")
        else:
            after = ""
            if not exists:
                fail(404, "file not found in diff")
        return {"before": _scrub_source(before), "after": _scrub_source(after)}
    files, patches = [], []
    for status, old, path in rows:
        try:
            actual = allowed(path)
            allowed(old)
        except Exception as exc:
            from .daemon import APIError
            if isinstance(exc, APIError) and exc.status == 403:
                continue
            raise
        if status not in ("A", "M", "D", "R"):
            status = "M"
        patch = _git(root, "diff", "--no-ext-diff", "--no-textconv", "-M", ancestor, "--", old, path)
        nums = _git(root, "diff", "--no-ext-diff", "--no-textconv", "--numstat", ancestor, "--", old, path)
        additions = deletions = 0
        for line in nums.splitlines():
            a, d, _ = line.split("\t", 2)
            additions += int(a) if a.isdecimal() else 0
            deletions += int(d) if d.isdecimal() else 0
        if not patch and status == "A":
            if (root / path).is_symlink():
                data = str((root / path).readlink()).encode()
            else:
                data, _ = read_file(actual)
            lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
            additions = len(lines)
            patch = "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{path}"))
        files.append(dict(path=path, status=status, additions=additions, deletions=deletions))
        patches.append(patch)
    encoded = _scrub_source("".join(patches)).encode()
    result = dict(base=base, head=head, files=files, patch=encoded[:PATCH_CAP].decode("utf-8", errors="ignore"))
    if len(encoded) > PATCH_CAP:
        result["truncated"] = True
    return result


# -- framing (WP-E, HUD workspace plan §2.5 item 3) --------------------------------
#
# Nothing on the HUD listener (8402) or the API listener (8405) may be drawn
# inside a frame: not the HUD, not a JSON answer, not an error page. A Preview
# pane frames local pages on purpose, and a page in one could otherwise
# navigate its own frame to 8402 and become a working copy of the window that
# gates approvals, drawn inside that window. With these two headers the
# browser shows a blocked frame instead. Both, because `X-Frame-Options` is
# what an older engine reads and `frame-ancestors` is the standard one.
#
# The preview listener (8403) is framed by the HUD on purpose, so it gets
# neither — but every document it serves is **sandboxed by its own header**
# (`sandbox allow-scripts allow-forms`, the Preview frame's own flags), so a
# workshop page always runs with an opaque origin: inside the HUD (as it
# already did), in a top-level tab, and in a Preview frame whose page was let
# keep its origin and then navigated itself to the workshop (PR #31 review:
# that frame used to run with the real workshop origin, its storage and
# same-origin reads of other projects' served files).
#
# They are added in **one place**, the daemon handler's `end_headers`, so a
# route cannot forget them; the one response written by hand (the WebSocket
# `101`, `ws.upgrade`) asks `frame_headers` for them too. `GET /status`
# reports `frame_hardened: true` from the handler that adds them, and the HUD
# offers a Preview pane's "keep its own origin" switch only then (decisions
# W-4: that switch ships only once these headers are in force).
FRAME_ANCESTORS = "frame-ancestors 'none'"
WORKSHOP_SANDBOX = "sandbox allow-scripts allow-forms"
# Directives a response may never set for itself: they are this module's to
# decide, on every response (`report-*` would let a response send the policy's
# violations somewhere of its own choosing).
_RESERVED = ("frame-ancestors", "sandbox", "report-uri", "report-to")

# -- the HUD's Content-Security-Policy (editor plan §2.7, ED-4) ------------------------
#
# Sent on **every** response from the HUD and API listeners — the page, every
# `/assets/*` file (a worker takes its policy from its own script's response,
# not the page's), JSON, SSE, errors and the terminal socket's `101` — in the
# one header WP-E's frame rule already rides in. Only code bundled at build
# time runs in the window that gates approvals: no inline script, no script
# from another origin, no `eval`, no plugin, no `<base>`, no form posting
# anywhere. Why each line:
#   - `script-src 'self' 'wasm-unsafe-eval'`: the bundle only. **No
#     `'unsafe-eval'`**: no string becomes JavaScript (`eval`, `new
#     Function` and a string `setTimeout` stay refused).
#     `'wasm-unsafe-eval'` (ED-5) lets WebAssembly compile, and nothing else:
#     tree-sitter's runtime and grammars, which the syntax worker fetches
#     only from the map built into it (`hud/src/lib/syntax/assets.ts` refuses
#     any other URL before a request is made) and compiles from bytes. A
#     worker takes its policy from its own script's response, so the worker
#     needs it on `/assets/*` — and every response carries this one policy.
#     tests/face/hud_v2_syntax_check.py loads every grammar under it.
#   - `worker-src 'self'`: Monaco's `editor.worker` and the syntax worker
#     (ED-5), module workers Vite emits under `/assets/` (no `blob:` worker).
#   - `connect-src 'self'`: `fetch`, the `/events` stream, the `/stt` upload
#     and — CSP3, as Chromium implements it — same-origin `ws://` for the
#     terminal socket (proved headless by tests/face/hud_v2_csp_check.py).
#   - `style-src 'self' 'unsafe-inline'`: xterm (and Monaco) inject `<style>`
#     elements at run time, which only `'unsafe-inline'` admits. React's
#     `style={…}` is not the reason — it sets properties through the CSSOM,
#     which CSP does not govern. Styles cannot run code.
#   - `img-src 'self' data:`: the avatar `<img>` (`/avatar.svg`) and Monaco's
#     `data:` SVG squiggles. `font-src 'self' data:`: the codicon font.
#   - `media-src 'self'`: same-origin audio only (`/say`), should playback
#     ever come back; the microphone and the approval chime are Web Audio,
#     which no CSP directive governs.
#   - `manifest-src 'self'`: Chrome loads the HUD's install manifest locally.
#   - `frame-src`: the Preview pane's loopback frames, by the host names a
#     CSP can express (an IPv6 literal or another 127/8 address cannot be
#     named, so the Preview judge refuses those with a sentence). An
#     `http:` source also matches `https:` (CSP3). The judge already refuses
#     the daemon's own ports; `frame-ancestors 'none'` stops the frame from
#     becoming the HUD.
#   - `base-uri`, `form-action`, `object-src 'none'`: nothing to allow.
HUD_POLICY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("default-src", ("'none'",)),
    ("script-src", ("'self'", "'wasm-unsafe-eval'")),
    ("worker-src", ("'self'",)),
    ("connect-src", ("'self'",)),
    ("style-src", ("'self'", "'unsafe-inline'")),
    ("img-src", ("'self'", "data:")),
    ("font-src", ("'self'", "data:")),
    ("media-src", ("'self'",)),
    ("manifest-src", ("'self'",)),
    ("frame-src", ("http://localhost:*", "http://*.localhost:*", "http://127.0.0.1:*",
                   "http://0.0.0.0:*")),
    ("base-uri", ("'none'",)),
    ("form-action", ("'none'",)),
    ("object-src", ("'none'",)),
)
# The avatar SVG's own lockdown (`GET /avatar.svg`): nothing but its own
# inline styles, should the file ever be opened as a document.
AVATAR_CSP = "default-src 'none'; style-src 'unsafe-inline'"
_FETCH = frozenset({"default-src", "script-src", "worker-src", "connect-src", "style-src", "img-src",
                    "font-src", "media-src", "manifest-src", "frame-src", "object-src"})


def _own_directives(extra) -> list[str]:
    """A response's own CSP directives, checked.

    Refused with ValueError — a programming error, never input:
      - a comma: it ends one policy and starts another in the same header.
        `img-src 'self', frame-ancestors *` became the policies `img-src
        'self'` and `frame-ancestors *; frame-ancestors 'none'`, where the
        first `frame-ancestors` wins — and a browser that sees one ignores
        `X-Frame-Options`, so both frame headers fell at once (PR #31 review,
        shown in Chromium);
      - anything but printable ASCII (space to `~`): CR or LF splits the
        header, and a character `latin-1` cannot encode (U+2028) failed only
        inside `end_headers` — after `send_response`, so the error's own
        answer went out as a second status line (PR #31 re-review);
      - any directive this module reserves (`_RESERVED`)."""
    directives: list[str] = []
    for chunk in extra:
        if not chunk:
            continue
        if not isinstance(chunk, str) or any(c == "," or not " " <= c <= "~" for c in chunk):
            raise ValueError("a response's own CSP directives must be printable ASCII with no comma")
        for directive in chunk.split(";"):
            directive = directive.strip()
            if not directive:
                continue
            name = directive.split()[0].lower()
            if name in _RESERVED:
                raise ValueError(f"a response may not set the CSP directive {name!r}")
            directives.append(directive)
    return directives


def _source(token: str) -> str:
    # Keywords and schemes are case-insensitive; compare them folded.
    return token.lower() if token.startswith("'") or token.endswith(":") else token


def _narrowed(own: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    """`HUD_POLICY` with a response's own directives folded in — narrowing only.

    A response may restate a directive the policy sets, with a subset of its
    sources (`'none'` is the empty set, so it always narrows); anything else
    raises ValueError: a source the policy does not allow, a directive it
    does not set, the same directive twice. A response that states its own
    `default-src` (necessarily `'none'`) also drops every fetch directive of
    the policy it does not restate, so they fall back to its `'none'` — the
    avatar's `default-src 'none'; style-src 'unsafe-inline'` keeps exactly the
    lockdown it had before the policy existed."""
    base = dict(HUD_POLICY)
    stated: dict[str, tuple[str, ...]] = {}
    for directive in own:
        name, *sources = directive.split()
        name = name.lower()
        if name in stated:
            raise ValueError(f"a response states the CSP directive {name!r} twice")
        if name not in base:
            raise ValueError(f"a response may only narrow a directive the HUD policy sets, not {name!r}")
        wanted = tuple(_source(t) for t in sources)
        allowed = {_source(t) for t in base[name]} - {"'none'"}
        if not wanted or not (set(wanted) - {"'none'"} <= allowed) or ("'none'" in wanted and len(wanted) > 1):
            raise ValueError(f"a response's own {name!r} may only narrow the HUD policy's")
        stated[name] = wanted
    merged = []
    for name, sources in HUD_POLICY:
        if name in stated:
            merged.append((name, stated[name]))
        elif "default-src" in stated and name in _FETCH:
            continue
        else:
            merged.append((name, sources))
    return merged


def content_security_policy(*extra: str | None) -> str:
    """The one `Content-Security-Policy` the HUD and API listeners send.

    `HUD_POLICY` (ED-4) and `frame-ancestors 'none'` (WP-E), in one header; a
    response may add directives of its own (an avatar's SVG locks itself
    down further), checked by `_own_directives` (no comma, printable ASCII,
    nothing reserved) and by `_narrowed` (they may only narrow the policy).
    One builder, so no response ever sends a second, conflicting policy."""
    merged = _narrowed(_own_directives(extra))
    return "; ".join([*(f"{name} {' '.join(sources)}" for name, sources in merged), FRAME_ANCESTORS])


def workshop_security_policy(*extra: str | None) -> str:
    """The one `Content-Security-Policy` the preview listener sends: the
    sandbox (an opaque origin for every document it serves) and a response's
    own directives, checked the same way."""
    return "; ".join([*_own_directives(extra), WORKSHOP_SANDBOX])


def frame_headers(handler, csp: str | None = None) -> list[tuple[str, str]]:
    """The headers every response on `handler`'s listener carries.

    The HUD and API listeners: the CSP (with `csp`'s directives, if any) and
    `X-Frame-Options: DENY`. The preview listener: no frame rule (the HUD
    frames it), but one CSP that sandboxes every document it serves, with
    `csp`'s directives merged in."""
    if getattr(getattr(handler, "server", None), "preview_only", False):
        return [("Content-Security-Policy", workshop_security_policy(csp))]
    return [("Content-Security-Policy", content_security_policy(csp)), ("X-Frame-Options", "DENY")]


def binary(handler, data, mime, *, csp=None, status=200, headers=None):
    # Checked before a byte is written: a bad policy is one 400 (the
    # dispatcher's answer to a ValueError), never half a response.
    frame_headers(handler, csp)
    handler.send_response(status)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    # Folded into the one policy by the handler's `end_headers` (`frame_headers`).
    handler._csp = csp
    handler.end_headers()
    handler._streaming = True
    handler.wfile.write(data)
    return status, None


# -- the HUD's own files (decision C-1, ED-4) -------------------------------------------

_HUD_CAP = 64 * 1024 * 1024


def _hud_page(sentence: str) -> bytes:
    import html as _html
    return (b"<!doctype html><meta charset=\"utf-8\"><title>J.A.R.V.I.S.</title><p>"
            + _html.escape(sentence).encode() + b"</p>\n")


_NOT_INSTALLED = _hud_page("The HUD is not installed. Run `jarvis hud install` in a terminal, "
                           "then reload this window.")
_FAILED = _hud_page("The installed HUD failed verification: no installed build still matches "
                    "its manifest, so none is served. Run `jarvis hud install` in a terminal, "
                    "then reload this window. The daemon's log names what did not match.")


def hud_served() -> dict:
    """What `GET /` serves (review of PR #44, L1/L6), for `/status` and the
    daemon's start-up log: `build` (the id, "dev" under JARVIS_HUD_DEV=1, or
    None), `git_head` and `dirty` from that build's manifest, `state` —
    "installed", "fallback" (a newer build failed verification and an older
    one is served), "dev", "not installed", "failed verification" or
    "refused: …" — and `passed_over`, the newer builds that failed. Never
    raises."""
    from . import hud_install
    if config.HUD_DEV:
        return {"build": "dev", "git_head": None, "dirty": None, "state": "dev", "passed_over": []}
    built, state = hud_install.state(config.HUD_DIST)
    try:
        skipped = hud_install.passed_over(config.HUD_DIST, built) if state != "not installed" else []
    except Exception:     # noqa: BLE001
        skipped = []
    if built is None:
        return {"build": None, "git_head": None, "dirty": None, "state": state, "passed_over": skipped}
    return {"build": built.id, "git_head": built.git_head, "dirty": built.dirty, "state": state,
            "passed_over": skipped}


def hud_build_id() -> str | None:
    """Which HUD this daemon serves: a build id, "dev", or None."""
    return hud_served()["build"]


def hud_files(handler, parts):
    """`GET /`, `/assets/*`, `/manifest.webmanifest`, `/icons/*`: the HUD,
    from the newest intact installed build (`hud_install.state`).

    Under JARVIS_HUD_DEV=1 (the headless suites) it is `hud/dist` as it is,
    no link followed; otherwise only a file the build's manifest lists, still
    matching its size and SHA-256 (`hud_install.read`). With no intact build
    `GET /` is a 503 page that says which: nothing installed, or nothing that
    passed verification."""
    from . import hud_install
    name = "index.html" if parts == [""] else unquote("/".join(parts))
    if ".." in Path(name).parts or name.startswith("/") or "\\" in name or "\0" in name:
        fail(403, "asset outside HUD")
    if config.HUD_DEV:
        root = Path(config.HUD_DIST)
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or (root / name).is_symlink():
            fail(403, "asset outside HUD")
        if not path.is_file():
            if parts == [""]:
                return binary(handler, b"<!doctype html><title>J.A.R.V.I.S.</title>hud/dist is absent; "
                              b"build the HUD first.\n", "text/html; charset=utf-8", status=503)
            fail(404, "file not found")
        data, _ = read_file(path, _HUD_CAP)
    else:
        root = config.HUD_DIST
        if parts == [""]:
            built, state = hud_install.state(root)
            data = hud_install.read(built, "index.html") if built else None
            if data is None:
                page = (_NOT_INSTALLED if state == "not installed" else _FAILED
                        if state == "failed verification" or built is not None
                        else _hud_page(f"The HUD install folder was {state}"))
                return binary(handler, page, "text/html; charset=utf-8", status=503)
        else:
            data = hud_install.find(root, name)
            if data is None:
                fail(404, "file not found")
    mime = "application/manifest+json" if name.endswith(".webmanifest") else (
        mimetypes.guess_type(name)[0] or "application/octet-stream")
    return binary(handler, data, mime)


def check_origin(handler, *, preview=False):
    host = handler.headers.get("Host", "")
    port = handler.server.server_address[1]
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        fail(403, "untrusted Host")
    if not preview:
        origin = handler.headers.get("Origin")
        if origin and origin != f"http://{host}":
            fail(403, "cross-origin request refused")
        if handler.headers.get("Sec-Fetch-Site") == "cross-site":
            fail(403, "cross-site request refused")
        if handler.command in ("POST", "PUT", "PATCH") and urlsplit(handler.path).path != "/stt":
            # CLI clients omit Content-Type in older tests; browsers' simple
            # form/text types must never reach a mutation without preflight.
            content_type = handler.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type and content_type != "application/json":
                fail(400, "expected application/json")


PROTECTED_PAGE = "protected file"
# A hidden path is refused for what it is, not called a credential: a
# `.vitepress/dist/index.html` is an ordinary build, and "protected file" sent
# the owner looking for a secret that is not there (review of PR #49, NIT-1).
HIDDEN_PAGE = "hidden path: not served on the preview origin"


def _not_a_page(project, raw, target) -> str | None:
    """What the preview origin refuses beyond `protected` (review of PR #49,
    LOW3), as the refusal's sentence; None when it may be served. The preview
    listener has no owner check by design — the HUD frames it — and it answers
    any loopback client, v1's `fetch_page` included (loopback is allowed and
    8403 is not a control port), while the Inbox is rooted at `~`. So: no path
    with a hidden component inside the project (`.docker/config.json`,
    `.kube/config`, `.npmrc`, a tool's dotfolder), except `.well-known`; and
    nothing under Jarvis's own config and data folders (session carriers,
    transcripts), wherever the project is."""
    roots = [Path(project.root).resolve()] + [Path(p).expanduser().resolve() for p in project.extra_dirs]
    for path in (Path(raw), Path(target)):
        root = next((r for r in roots if path.is_relative_to(r)), None)
        if root is None:
            return PROTECTED_PAGE
        lexical = Path(os.path.abspath(path))
        if any(p == d or p.is_relative_to(d) for d in editor_policy.jarvis_dirs() for p in (lexical, path.resolve())):
            return PROTECTED_PAGE
        if editor_policy.hidden_component(path, root):
            return HIDDEN_PAGE
    return None


def preview_route(handler, daemon, parts, query):
    if handler.command != "GET" or len(parts) < 2 or parts[0] != "p":
        fail(404, "preview route not found")
    project = daemon.require(daemon.stores.projects, parts[1])
    name = unquote("/".join(parts[2:]))
    raw, target = scope(project, name)
    if target.is_dir():
        raw, target = scope(project, str(target / "index.html"))
    if protected(raw, target):
        fail(403, PROTECTED_PAGE)
    refused = _not_a_page(project, raw, target)
    if refused:
        fail(403, refused)
    data, _ = read_file(target, 32 * 1024 * 1024)
    return binary(handler, data, mimetypes.guess_type(target.name)[0] or "application/octet-stream")


SAY_MAX_CHARS = 2000


def say(handler, body):
    """`POST /say`: one chunk of speech as audio (V6).

    `voice`, when named, must be a voice `catalog()` lists: an unknown name
    used to fail inside Kokoro and then reach the cloud carrying that name.
    `local_only` (read-aloud sets it) never calls `llm.speech` -- on a local
    failure the answer is 503 and the chunk is skipped. `X-Speech-Ms` is the
    synthesis time, for the HUD's diagnostics.
    """
    import time
    from jarvis import voice
    text = str(body.get("text", "")).strip()
    if not text:
        fail(400, "no text to say")
    name = body.get("voice")
    if name is not None:
        if not isinstance(name, str):
            fail(400, "voice must be a string")
        name = name.strip() or None
    if name is not None and name not in {entry["name"] for entry in voice.catalog()}:
        fail(400, "unknown voice")
    local_only = body.get("local_only", False)
    if not isinstance(local_only, bool):
        fail(400, "local_only must be a boolean")
    started = time.monotonic()
    try:
        audio = voice.tts(text[:SAY_MAX_CHARS], voice=name, instructions=body.get("instructions"),
                          local_only=local_only)
    except voice.LocalTTSUnavailable as exc:
        fail(503, str(exc))
    elapsed = str(int((time.monotonic() - started) * 1000))
    return binary(handler, audio, "audio/wav" if audio.startswith(b"RIFF") else "audio/mpeg",
                  headers={"X-Speech-Ms": elapsed})


def stt_settings_route(handler, daemon, query):
    """`/stt/settings` (voice rework V13): the owner's speech-to-text switches.

        GET  -> {backend, cloud_fallback, error, local: {installed, reason, loaded, threads, ...}}
        POST {backend?: "cloud"|"local"|"auto", cloud_fallback?: bool}  (at least one)

    Owner-only, the HUD listener (like the other voice stores): an agent or a
    page must not be able to point the owner's audio at the network. A read never
    loads the model.
    """
    from jarvis import stt_local, stt_settings
    from .daemon import _object
    from .projects import owner_only

    owner_only(handler, daemon)
    _object(query, ())

    def view():
        return {**stt_settings.load(), "backends": list(stt_settings.BACKENDS), "local": stt_local.status()}

    if handler.command == "GET":
        return 200, view()
    if handler.command != "POST":
        return None
    body = _object(handler._body(), ("backend", "cloud_fallback"))
    if not body:
        fail(400, "expected backend and/or cloud_fallback")
    if "backend" in body and body["backend"] not in stt_settings.BACKENDS:
        fail(400, f"backend must be one of {', '.join(stt_settings.BACKENDS)}")
    if "cloud_fallback" in body and not isinstance(body["cloud_fallback"], bool):
        fail(400, "cloud_fallback must be a boolean")
    try:
        stt_settings.update(**body)
    except OSError as exc:
        fail(500, f"the setting could not be saved ({type(exc).__name__})")
    return 200, view()


def pickers(handler, daemon, parts, query):
    from jarvis import avatars, llm, models, voice
    from jarvis.tools import voicectl
    from .daemon import _object
    name = "/".join(parts)
    if handler.command == "GET":
        if name == "avatars":
            return 200, {"current": avatars.active().slug, "avatars": [a.describe() for a in avatars.available()]}
        if name == "avatar.svg":
            _object(query, ("slug",))
            av = avatars.load(query["slug"]) if query.get("slug") else avatars.active()
            art = avatars.svg(av) if av else None
            if not art:
                fail(404, "no avatar art")
            return binary(handler, art.encode(), "image/svg+xml", csp=AVATAR_CSP)
        if name == "voices":
            # `voice_out`: Jarvis's spoken replies, the v1 mute flag. The mic's
            # OFF no longer posts it (V6), so it moves only by `/mute` itself.
            return 200, {"current": voice.selected_voice(), "override": voice._voice_override,
                         "voices": voice.catalog(), "voice_out": not voicectl.is_muted()}
        if name == "models":
            return 200, models.describe()
        if name == "models/catalog":
            return 200, {"models": [m.describe() for m in models.catalog()],
                         "roster": models.roster().models, "stale": models.stale_reason()}
    if handler.command != "POST":
        return None
    if name == "stt":
        mime = handler.headers.get("Content-Type", "").split(";", 1)[0]
        if not mime.startswith("audio/"):
            fail(400, "expected audio/*")
        # V2: the client names each clip so a delayed reply can be placed in
        # capture order; both are echoed back verbatim (non-negative ints).
        _object(query, ("utterance_id", "seq", "budget_ms"))
        ids = {}
        for key, raw in query.items():
            if not (raw.isascii() and raw.isdigit() and len(raw) <= 15):
                fail(400, f"{key} must be a non-negative integer")
            ids[key] = int(raw)
        # The client's whole deadline for this request (ms). The model chain is
        # fitted inside it (voice.stt); absent, the fixed dictation bounds apply.
        budget_ms = ids.pop("budget_ms", None)
        budget = None if budget_ms is None else min(max(budget_ms, 1000), 600_000) / 1000
        data = handler._raw_body()
        if not data:
            fail(400, "audio is empty")
        try:
            # Dictation is bounded tighter than the library default (V-11).
            # The HUD is the caller that opts in to the local engine (V13); the
            # reply says which backend served the clip, for the diagnostics.
            got = voice.stt_result(data, mime=mime, timeout=config.STT_DICTATION_TIMEOUT_S,
                                   max_retries=config.STT_DICTATION_ATTEMPTS, budget=budget,
                                   prefer_local=True)
            served = {"backend": got.backend, "model": got.model}
            if got.fallback:
                served["fallback"] = got.fallback
            return 200, {"text": got.text, **served, **ids}
        except llm.LLMError as exc:
            # A provider outage is a sentence for the HUD, not a traceback in
            # the daemon log. STTUnavailable's message is model ids and HTTP
            # statuses only; any other LLMError may quote a response body, so
            # it is named by type.
            message = str(exc) if isinstance(exc, voice.STTUnavailable) else (
                f"speech-to-text failed ({type(exc).__name__})")
            # The owner's own switch forbade the only path left: a retry cannot fix it.
            fail(409 if isinstance(exc, voice.STTBlocked) else 502, message)  # logged as one warning line by _dispatch
    body = handler._body()
    # The keys each control reads, and nothing else. A body carrying some
    # other key used to be read as "the key is absent", which for /model
    # meant "back to the config default" and for /voice "clear the override"
    # — the HUD sent {id}, {name} and {mute} for weeks and every click quietly
    # undid itself (decisions A6). An unknown key is now a 400 that names it.
    keys = {"mute": ("muted",), "avatar": ("slug",), "voice": ("voice",), "model": ("model",),
            "models": ("add", "remove", "model", "effort"),
            "say": ("text", "voice", "instructions", "local_only")}.get(name)
    if keys is not None:
        _object(body, keys, () if name in ("models", "say") else keys)
    if name == "say":
        return say(handler, body)
    if name == "mute":
        if not isinstance(body["muted"], bool):
            fail(400, "muted must be a boolean")
        voicectl.set_muted(body["muted"])
        return 200, {"ok": True, "muted": voicectl.is_muted()}
    if name == "avatar":
        return 200, avatars.set_active(str(body.get("slug", ""))).describe()
    if name == "voice":
        payload = {"voice": voice.set_voice(str(body.get("voice", "")))}
        daemon.bus.publish({"kind": "voice", "data": payload})
        return 200, payload
    if name not in ("model", "models"):
        return None
    try:
        if name == "model":
            models.select(str(body.get("model", "")))
        else:
            add, remove, model = (str(body.get(k) or "").strip() for k in ("add", "remove", "model"))
            if sum(bool(v) for v in (add, remove, model)) != 1:
                fail(400, "expected exactly one of add, remove, or model with effort")
            if "effort" in body and not model:
                # {add, effort} / {remove, effort} used to drop the effort
                # silently; an effort is set with {model, effort} only.
                fail(400, "effort goes with {model, effort}, not with add or remove")
            if add:
                models.add(add)
            elif remove:
                models.remove(remove)
            else:
                models.set_effort(model, str(body.get("effort") or ""))
    # The picker shows a refusal in the backend's own words (an unpin that
    # would leave the default unlisted says "choose another default first"),
    # and the dispatcher only reflects an APIError's text — anything else
    # would arrive as "request failed (RosterRefused)".
    except models.RosterRefused as exc:
        fail(409, str(exc))
    except models.NotEligible as exc:
        fail(400, str(exc))
    except models.NotOnRoster as exc:
        # Only this: a bare LookupError would also catch a real bug's KeyError.
        fail(404, str(exc))
    payload = models.describe()
    daemon.bus.publish({"kind": "model", "data": payload})
    return 200, payload


def _with_describe(record):
    """The scheduler's own sentence, computed on the way out so the window
    does not grow a second cron reader. Not stored: `fire` saves the record
    it read, and a field added in place would be written back."""
    if not isinstance(record, dict):
        return record
    from .schedules import describe
    row = dict(record)
    try:
        row["describe"] = describe(row.get("cron"), row.get("every_s"))
    except (TypeError, ValueError):
        row["describe"] = ""
    return row


def discord_status(daemon) -> dict:
    """`GET /discord` (S1): is the surface up, and did the command sync work.

    Built from the surface's own status record, which holds states, counts
    and times only — never a token, an interaction token or a request path.
    PR A adds `reporter` (the update poster's health); B1 adds `guild`,
    `linker` and `permissions`; PR C adds `mirror` (the chat mirror's)."""
    surface = getattr(daemon, "discord", None)
    if surface is None:
        from .daemon import discord_connected
        from .discord import guild as _guild
        cfg = _guild.load()
        offline = {"guild": {"configured": cfg is not None,
                             "id": cfg.guild_id if cfg is not None else None},
                   "linker": None, "permissions": None, "mirror": None}
        failed = getattr(daemon, "discord_error", None)
        if isinstance(failed, str) and failed:
            # start_discord raised: say so, red, with the class and nothing else.
            failed = failed[:60]
            return {"connected": False,
                    "commands": {"state": "failed", "count": 0, "synced_at": None,
                                 "error": failed},
                    "reporter": {"state": "down",
                                 "reason": f"the Discord surface did not start ({failed})",
                                 "counters": {}, "dropped": 0, "last_error": None},
                    **offline}
        state = "pending" if discord_connected() else "off"
        return {"connected": False,
                "commands": {"state": state, "count": 0, "synced_at": None, "error": None},
                "reporter": None, **offline}
    status = surface.status()
    commands = status.get("commands") or {}
    return {"connected": bool(status.get("connected")),
            "commands": {key: commands.get(key) for key in
                         ("state", "count", "synced_at", "error")},
            "reporter": _reporter_status(status.get("reporter")),
            "mirror": _mirror_status(status.get("mirror")),
            **_linker_status(status)}


def _mirror_status(mirror) -> dict | None:
    """The chat mirror's health (PR C), field by field: a state, a reason
    sentence, the posts still waiting, and the last failure as operation,
    HTTP status, Discord code and time. Nothing else passes."""
    if not isinstance(mirror, dict):
        return None
    state = mirror.get("state")
    error = mirror.get("last_error")
    queued = mirror.get("queued")
    return {
        "state": state if state in ("ok", "degraded", "down") else "down",
        "reason": str(mirror.get("reason") or "")[:300],
        "queued": queued if isinstance(queued, int) and not isinstance(queued, bool) else 0,
        "last_error": ({"op": str(error.get("op") or "")[:40],
                        "status": error.get("status") if isinstance(error.get("status"), int) else None,
                        "code": error.get("code") if isinstance(error.get("code"), int) else None,
                        "at": error.get("at") if isinstance(error.get("at"), (int, float)) else None}
                       if isinstance(error, dict) else None),
    }


def _names(value, limit=20) -> list[str]:
    return [str(v)[:60] for v in value[:limit] if isinstance(v, str)] \
        if isinstance(value, list) else []


def _linker_status(status) -> dict:
    """B1's three fields, copied one by one like the reporter's: the guild
    (configured, id), the channel linker (state, reason, pending renames,
    open housekeeping asks) and the bot's server-wide permissions (missing,
    excess, administrator). A surface without a linker reads as unconfigured."""
    guild = status.get("guild") if isinstance(status.get("guild"), dict) else {}
    gid = guild.get("id")
    linker = status.get("linker")
    perms = status.get("permissions")
    return {
        "guild": {"configured": bool(guild.get("configured")),
                  "id": gid if isinstance(gid, str) and gid.isdigit() else None},
        "linker": ({"state": linker.get("state") if linker.get("state") in
                    ("ok", "degraded", "unconfigured") else "degraded",
                    "reason": str(linker.get("reason") or "")[:300],
                    "pending_renames": int(linker.get("pending_renames") or 0),
                    "pending_moves": int(linker.get("pending_moves") or 0),
                    "awaiting_approval": int(linker.get("awaiting_approval") or 0)}
                   if isinstance(linker, dict) else None),
        "permissions": ({"missing": _names(perms.get("missing")),
                         "excess": _names(perms.get("excess")),
                         "administrator": bool(perms.get("administrator")),
                         "checked_at": perms.get("checked_at")
                         if isinstance(perms.get("checked_at"), (int, float)) else None}
                        if isinstance(perms, dict) else None),
    }


def _reporter_status(reporter) -> dict | None:
    """The update poster's health (PR A), field by field: a state, a reason
    sentence, integer counters, the bus drop count and the last failure as
    operation, HTTP status, Discord code and time. Nothing else passes."""
    if not isinstance(reporter, dict):
        return None
    state = reporter.get("state")
    counters = reporter.get("counters") or {}
    error = reporter.get("last_error")
    return {
        "state": state if state in ("ok", "degraded", "down") else "down",
        "reason": str(reporter.get("reason") or "")[:300],
        "counters": {str(k): int(v) for k, v in counters.items()
                     if isinstance(v, int) and not isinstance(v, bool)},
        "dropped": int(reporter.get("dropped") or 0),
        "last_error": ({"op": str(error.get("op") or "")[:40],
                        "status": error.get("status") if isinstance(error.get("status"), int) else None,
                        "code": error.get("code") if isinstance(error.get("code"), int) else None,
                        "at": error.get("at") if isinstance(error.get("at"), (int, float)) else None}
                       if isinstance(error, dict) else None),
    }


def route(handler, daemon, parts, query):
    """Return None for the existing daemon routes; never consume their bodies."""
    from .daemon import _object, safe_list
    method, stores = handler.command, daemon.stores
    if method == "GET" and (parts in ([""], ["manifest.webmanifest"]) or parts[0] in ("assets", "icons")):
        return hud_files(handler, parts)
    if parts == ["stt", "settings"]:
        return stt_settings_route(handler, daemon, query)
    if "/".join(parts) in ("avatar.svg", "avatars", "avatar", "voices", "voice", "models", "models/catalog", "model", "mute", "say", "stt"):
        return pickers(handler, daemon, parts, query)
    if parts[0] == "speech":
        # Read-aloud's per-thread voices and mutes (V7): owner-only.
        from . import speech_voices as _speech_voices
        mounted = _speech_voices.route(handler, daemon, parts, query)
        if mounted is not None:
            return mounted
    if parts[0] == "terminals":
        # The owner's terminals (WP-C): owner-only, the HUD listener only,
        # and the attach socket also needs a single-use ticket.
        from . import terminals as _terminals
        return _terminals.route(handler, daemon, parts, query)
    if parts[0] == "dev-servers":
        from . import devservers as _devservers
        return _devservers.route(handler, daemon, parts, query)
    if parts[0] == "browser":
        # The owner's Browser (BR-1a): owner-only and a strict loopback Host
        # on every route, checked before anything else (browser/routes.py),
        # its settings included (BR-8a's `auto_open` beside the LAN switch).
        from .browser import routes as _browser
        return _browser.route(handler, daemon, parts, query)
    # The editor's git (ED-9): HEAD's text for the gutter and the explorer's
    # colours. The owner's, like every editor route (E-7), checked before
    # anything else — the project is not even looked up for anyone else.
    if len(parts) == 4 and parts[0] == "projects" and parts[2] == "git":
        from .projects import owner_only
        owner_only(handler, daemon)
        from . import editor_git
        if parts[3] == "base" and method == "GET":
            project = daemon.require(stores.projects, parts[1])
            _object(query, ("path",), ("path",))
            return 200, editor_git.base(project, query["path"])
        if parts[3] == "status" and method == "GET":
            project = daemon.require(stores.projects, parts[1])
            _object(query, ())
            return 200, editor_git.status(project)
    # VEX V5 (VX-1): build, download, the brain terminal, new projects.
    # Owner-only, the HUD listener only; mounted before `/projects/...` so
    # `/projects/{id}/vex...` reaches it.
    from . import vex as _vex
    mounted = _vex.route(handler, daemon, parts, query)
    if mounted is not None:
        return mounted
    # Archive, restore, permanent delete and the trash (decisions part B).
    from . import projects as _projects
    mounted = _projects.route(handler, daemon, parts, query)
    if mounted is not None:
        return mounted
    # Project channels: the light, link/create/unlink and backfill (B1).
    from .discord import routes as _discord_routes
    mounted = _discord_routes.route(handler, daemon, parts, query)
    if mounted is not None:
        return mounted
    if parts == ["activity"] and method == "GET":
        # The sidebar's dots: every thread and task that is not idle.
        _object(query, ())
        return 200, daemon.activity.snapshot()
    if parts == ["usage"] and method == "GET":
        _object(query, ())
        return 200, usage(daemon)
    if parts == ["discord"] and method == "GET":
        _object(query, ())
        return 200, discord_status(daemon)
    if parts == ["fs", "dirs"] and method == "GET":
        # No path means "start at home": the picker opens there (WP12c frontend).
        _object(query, ("path",))
        return 200, directories(query.get("path") or str(Path.home()))
    if parts[0] == "schedules":
        _object(query, ())
        schedules = daemon.schedules
        if parts == ["schedules", "preview"] and method == "POST":
            from .schedules import preview
            body = _object(handler._body(), ("cron", "every_s"))
            return 200, preview(**body, now=schedules.clock())
        if len(parts) == 1:
            if method == "GET":
                # An archived project's schedules are paused and hidden (B1).
                hidden = _projects.archived_project_ids(stores)
                return 200, [_with_describe(row) for row in schedules.list()
                             if row.get("project_id") not in hidden]
            if method == "POST":
                return 201, _with_describe(schedules.save(handler._body()))
        if len(parts) == 2:
            if method == "PATCH":
                return 200, _with_describe(schedules.save(handler._body(), parts[1]))
            if method == "DELETE":
                schedules.delete(parts[1])
                return 200, {"ok": True}
        if len(parts) == 3 and parts[2] == "run-now" and method == "POST":
            _object(handler._body(), ())
            return 200, _with_describe(schedules.fire(parts[1], manual=True))
    if len(parts) == 3 and parts[0] == "projects" and parts[2] in ("tree", "file", "platform"):
        project = daemon.require(stores.projects, parts[1])
        action = parts[2]
        if action == "platform" and method == "GET":
            windows = bool(re.match(r"^/mnt/[a-zA-Z]/", project.root + "/"))
            return 200, {"platform": "windows" if windows else "wsl",
                         "note": "Git on WSL's 9p-mounted Windows filesystem can be slower than on Linux." if windows else None}
        # The editor's file routes are the owner's, reads included (E-7): the
        # HUD listener and the HUD's own page, never the API listener every
        # tool's HTTP client uses. An env file's content (the owner's `.env*`
        # rule) is served by nothing else.
        if action == "tree" and method == "GET":
            from .projects import owner_only
            owner_only(handler, daemon)
            _object(query, ("path", "depth"))
            return 200, tree(project, query.get("path", ""), int(query.get("depth", "2")))
        if action == "file" and method == "GET":
            from .projects import owner_only
            owner_only(handler, daemon)
            _object(query, ("path",), ("path",))
            return 200, read_for_editor(project, query["path"])
        if action == "file" and method == "PUT":
            from .projects import owner_only
            owner_only(handler, daemon)
            _object(query, ())
            return save_for_editor(daemon, project, handler._body())
    # The editor's navigation (ED-7): quick open's list, find in files and the
    # explorer's file operations — the owner's, like every editor file route
    # (E-7), checked before the body is read (`editor_nav`).
    if (len(parts) == 3 and parts[0] == "projects"
            and (parts[2], method) in (("files", "GET"), ("search", "POST"), ("fs", "POST"))):
        project = daemon.require(stores.projects, parts[1])
        from .projects import owner_only
        owner_only(handler, daemon)
        _object(query, ())
        from . import editor_nav
        if parts[2] == "files":
            return 200, editor_nav.list_files(project)
        if parts[2] == "search":
            return 200, editor_nav.search(project, handler._body())
        return editor_nav.fs(daemon, project, handler._body())
    if (len(parts) == 4 and parts[0] == "projects" and parts[2:] == ["files", "stat"]
            and method == "POST"):
        project = daemon.require(stores.projects, parts[1])
        from .projects import owner_only
        owner_only(handler, daemon)
        _object(query, ())
        return 200, stat_for_editor(project, handler._body())
    if parts == ["thread-models"] and method == "GET":
        # What the input bar's provider/model/effort chips offer (decisions A2).
        from . import thread_model
        _object(query, ())
        _refresh_codex_metadata(daemon)
        return 200, thread_model.describe()
    if parts == ["thread-models"] and method == "POST":
        # The default Claude or Codex chat threads run on (2026-10-08): the
        # owner's "Set as default" / "Reset to built-in default" in the model
        # chip. Exact keys (A6); `model: ""` resets. It never touches
        # routing.json — role routing is Settings' — and no tool reaches it.
        from . import thread_model
        _object(query, ())
        body = _object(handler._body(), ("provider", "model", "effort"), ("provider", "model"))
        _refresh_codex_metadata(daemon)
        try:
            thread_model.set_provider_default(body["provider"], body["model"], body.get("effort"))
        except thread_model.ChoiceRefused as exc:
            fail(400, str(exc))
        payload = thread_model.describe()
        name = ProviderName(body["provider"]).value
        provider = payload["providers"][name]
        daemon.bus.publish({"kind": "model", "data": {
            "provider": name, "default": provider["default"],
            "default_effort": provider["default_effort"],
            "default_source": provider["default_source"]}})
        return 200, payload
    if len(parts) == 2 and parts[0] == "threads" and method == "PATCH":
        _object(query, ())
        body = _object(handler._body(), ("title", "project_id", "model", "effort"))
        kinds = [k for k, keys in (("rename", {"title"}), ("move", {"project_id"}),
                                   ("model", {"model", "effort"})) if body.keys() & keys]
        if len(kinds) != 1:
            fail(400, "rename, move and model change are separate requests" if kinds
                 else "missing fields: title, project_id, or model/effort")
        if kinds == ["rename"]:
            # A rename is its own request: `{title}` alone (decisions B4).
            return 200, _projects.rename_thread(daemon, parts[1], body["title"])
        if kinds == ["model"]:
            # A chat thread's model and effort, from its next message on.
            # Never its provider: a session cannot change provider.
            return 200, daemon.set_thread_model(parts[1], body)
        with daemon._lock:
            daemon._active()
            thread = daemon.require(stores.threads, parts[1])
            if thread.task_id is not None:
                fail(409, "task threads move with their task")
            if thread.role != Role.CHAT:
                fail(409, "only chat threads can move")
            target = daemon.require(stores.projects, body["project_id"])
            if thread.archived or _projects.is_archived(stores, thread.project_id):
                fail(409, "restore the thread before moving it")
            _projects.refuse_archived_project(target)
            previous = thread.project_id
            thread.project_id = target.id
            stores.threads.save(thread)
            # The live session writes its own Thread on usage/finish. Keep its
            # identity (also held by the provider) while updating its project.
            session = daemon._sessions.get(thread.id)
            if session is not None:
                session.thread.project_id = target.id
            daemon.bus.publish({"kind": "thread_moved", "thread_id": thread.id,
                                "project_id": target.id, "data": {
                                    "thread_id": thread.id, "from_project_id": previous,
                                    "to_project_id": target.id}})
            return 200, to_json(thread)
    if len(parts) == 3 and parts[0] == "threads" and parts[2] in ("transcript", "send"):
        thread = daemon.require(stores.threads, parts[1])
        if parts[2] == "send" and method == "POST":
            project = daemon.require(stores.projects, thread.project_id)
            # Authenticate before resolving an opaque page capture. API
            # and Discord sends can never use the owner's retained page text.
            from .projects import is_owner
            owner = is_owner(handler, daemon)
            message = assemble_turn(project, handler._body(), owner=owner,
                                    captures=daemon.browser_captures)
            # The owner is at the desk only when the window itself sent this
            # (the HUD's listener and Origin), never a script on the API one.
            if owner:
                message = dataclasses.replace(message, desk=True)
            # Never a dead end while a turn runs (2026-10-08): the message
            # starts a turn, is steered into the running one, or waits behind
            # it — `{"status": "started"|"steered"|"queued", "turn_id", ...}`.
            # Only a full queue (three waiting) or a task's running thread is
            # still 409.
            return 202, daemon.deliver(thread.id, message)
        if parts[2] == "transcript" and method == "GET":
            _object(query, ("tools",))
            if "tools" in query and query["tools"] != "1":
                fail(400, "tools must be 1")
            messages = []
            rows = stores.threads.read_log(thread.id)
            # What became of each owner message that reached a running turn
            # (2026-10-08): a steer the provider refused waits instead
            # (`steer_queued`); a waiting one ran (`queued_started`) or was
            # dropped. One still waiting must be in the live queue: after a
            # crash or restart nothing is, and it never ran.
            fate, requeued = {}, set()
            for row in rows:
                mid = (row.get("data") or {}).get("message_id")
                if not isinstance(mid, str):
                    continue
                if row.get("kind") in ("queued_started", "queued_dropped"):
                    fate[mid] = row["kind"]
                elif row.get("kind") == "steer_queued":
                    requeued.add(mid)
            with daemon._lock:
                live = {item.message_id for item in daemon._queues.get(thread.id) or ()}
                session = daemon._sessions.get(thread.id)
                running_turn = session.turn_id if session is not None and session.worker is not None else None
                cwd = session.brief.cwd if session is not None else None
            tools = {}
            if query.get("tools") == "1":
                from .tool_calls import transcript_tools
                if cwd is None:
                    try:
                        cwd = json.loads(stores.threads.path(thread.id).with_name("brief.json").read_text()).get("cwd")
                    except (OSError, ValueError, AttributeError):
                        pass
                tools = transcript_tools(rows, cwd, running_turn)
            for index, row in enumerate(rows):
                if index in tools:
                    messages.append(tools[index])
                    continue
                kind = row.get("kind")
                if kind not in (None, "text", "user", "model_set"):
                    continue
                data = row.get("data", row)
                text = data.get("text")
                if kind == "user" and isinstance(text, str) and isinstance(data.get("skill"), str):
                    # A `/skill` turn shows as the owner typed it (S1).
                    text = f"/skill {data['skill']} {text}".rstrip()
                if isinstance(text, str):
                    role = ("assistant" if kind == "text" else "user" if kind == "user"
                            else "system" if kind == "model_set" else row.get("role", "assistant"))
                    message = dict(role=role, text=text, at=row.get("at", row.get("t")))
                    # Opt-in clients reconcile an optimistic initial send by
                    # its acknowledged turn. Legacy response bytes stay fixed.
                    if query.get("tools") == "1" and isinstance(row.get("turn_id"), str):
                        message["turn_id"] = row["turn_id"]
                    if kind == "user" and data.get("via") in ("discord", "dm"):
                        # Typed in Discord (PR C): the HUD labels it "via Discord".
                        message["via"] = data["via"]
                    mid = data.get("message_id") if kind == "user" else None
                    if isinstance(mid, str):
                        message["message_id"] = mid
                        waited = bool(data.get("queued")) or mid in requeued
                        if fate.get(mid) == "queued_dropped":
                            message["mark"] = "not sent"
                        elif waited and mid not in fate:
                            message["mark"] = "queued" if mid in live else "not sent"
                        elif data.get("steer") and mid not in requeued:
                            message["mark"] = "steering"
                    messages.append(message)
            return 200, {"messages": messages}
    if len(parts) in (3, 4) and parts[0] == "tasks" and method == "GET":
        task = daemon.require(stores.tasks, parts[1])
        if parts[2:] == ["journal"]:
            _object(query, ("after",))
            after = int(query.get("after", "0"))
            if after < 0:
                fail(400, "after must be nonnegative")
            return 200, stores.tasks.read_journal(task.id)[after:]
        if parts[2:] == ["threads"]:
            _object(query, ())
            threads = {t.id: t for t in safe_list(stores.threads, task_id=task.id)}
            for thread_id in task.thread_ids:
                thread = stores.threads.get(thread_id)
                if thread and thread.project_id == task.project_id:
                    threads[thread_id] = thread
            with daemon._lock:
                return 200, [dict(thread_id=t.id, role=t.role.value, provider=t.provider.value, model=t.model,
                                  state="open" if t.id in daemon._sessions else "closed", turns=t.turns,
                                  cost_usd=t.cost_usd) for t in threads.values()]
        if parts[2:] == ["diff"]:
            _object(query, ())
            return 200, diff(daemon, task)
        if parts[2:] == ["diff", "file"]:
            _object(query, ("path",), ("path",))
            return 200, diff(daemon, task, query["path"])
    return None


def connect_controls(daemon):
    """The same bus also hears tool-originated mute and avatar changes."""
    import weakref
    from jarvis import avatars
    from jarvis.tools import voicectl
    reference = weakref.ref(daemon)
    def publish(kind, data):
        owner = reference()
        if owner and not owner._stopping:
            owner.bus.publish({"kind": kind, "data": data})
    voicectl.on_change(lambda muted: publish("mute", {"muted": muted}))
    avatars.on_change(lambda av: publish("avatar", av.describe()))

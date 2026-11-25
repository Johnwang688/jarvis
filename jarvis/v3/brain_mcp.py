"""Newline-delimited MCP over v3's bound brain toolset.

The review-memory profile uses the strict scan as an accident guard. Its
patches check for newly introduced directives in the resulting body and keep
the original source line.
"""

from copy import deepcopy
from functools import cached_property
from inspect import Parameter, getdoc, signature
import json
import os
import re
import sys
from threading import Thread
from typing import Annotated, Unpack, get_args, get_origin, get_type_hints
from uuid import uuid4

from jarvis.v2 import redact

from .hooks.turn_end import valid_session
from .index import Index
from .tools.memory import MemoryTools
from .tools.session_search import session_search
from .tools.skills import SkillTools
from .vault import Vault


_MEMORY_AND_SEARCH_TOOLS = (
    "memory_list", "memory_read", "memory_add", "memory_patch", "memory_archive",
    "memory_search", "session_search",
)
_COMMITMENT_TOOLS = ("commitment_upsert", "commitment_close")
CONSOLIDATE_TOOLS = (*_MEMORY_AND_SEARCH_TOOLS, *_COMMITMENT_TOOLS)
PROFILE = (*sorted(_MEMORY_AND_SEARCH_TOOLS), "skill_manage", "skill_view")
MEMORY_REVIEW_TOOLS = (
    "memory_list", "memory_read", "memory_search", "memory_add", "memory_patch", "session_search",
)
SKILL_REVIEW_TOOLS = ("skill_view", "skill_manage", "session_search")
PROFILE_ENV = "JARVIS_BRAIN_PROFILE"
REVIEW_DOC_ENV = "JARVIS_BRAIN_DOC"
REVIEW_SESSION_ENV = "JARVIS_BRAIN_SESSION"
REVIEW_TAINTED_ENV = "JARVIS_BRAIN_TAINTED"
_REVIEW_PROFILES = {"review-memory": MEMORY_REVIEW_TOOLS, "review-skill": SKILL_REVIEW_TOOLS}
_PROFILES = {None: PROFILE, "consolidate": CONSOLIDATE_TOOLS, **_REVIEW_PROFILES}
_DOC_ID = re.compile(r"[0-9a-f]{64}")
PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION, "2024-11-05"})
_MAX_LINE = 1024 * 1024
_RESULT_CHARS = 32_000
_JSON_TYPES = {str: "string", int: "integer", bool: "boolean"}
_PYTHON_TYPES = {value: key for key, value in _JSON_TYPES.items()}


def _schema(method, *, internal=()):
    properties = {}
    required = []

    def add(name, annotation, mandatory):
        if get_origin(annotation) is not Annotated:
            raise TypeError(f"Missing tool parameter description for {name}")
        kind, description = get_args(annotation)
        properties[name] = {"type": _JSON_TYPES[kind], "description": description}
        if mandatory:
            required.append(name)

    hints = get_type_hints(method, include_extras=True)
    for param in signature(method).parameters.values():
        if param.name == "self" or param.name in internal:
            continue
        hint = hints[param.name]
        if param.kind is Parameter.VAR_KEYWORD and get_origin(hint) is Unpack:
            (fields,) = get_args(hint)
            for name, field in get_type_hints(fields, include_extras=True).items():
                add(name, field, name in fields.__required_keys__)
        else:
            add(param.name, hint, param.default is Parameter.empty)
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


_METHODS = {
    name: getattr(MemoryTools, name) for name in _MEMORY_AND_SEARCH_TOOLS if name != "session_search"
}
_METHODS.update(session_search=session_search, skill_manage=SkillTools.skill_manage,
                skill_view=SkillTools.skill_view)
_SCHEMAS = {name: _schema(method, internal=("index",) if name == "session_search" else ())
            for name, method in _METHODS.items()}
_REVIEW_ADD_SCHEMA = deepcopy(_SCHEMAS["memory_add"])
_REVIEW_ADD_SCHEMA["required"].remove("source")
_REVIEW_ADD_SCHEMA["properties"]["source"]["description"] = "The server sets the session source."


def _result(text, *, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _valid(arguments, schema):
    return (isinstance(arguments, dict)
            and arguments.keys() >= set(schema["required"])
            and all(key in schema["properties"]
                    and type(value) is _PYTHON_TYPES[schema["properties"][key]["type"]]
                    for key, value in arguments.items()))


def _review_scope():
    doc = os.environ.get(REVIEW_DOC_ENV, "")
    session = os.environ.get(REVIEW_SESSION_ENV, "")
    provider, _, sid = session.partition(":")
    tainted = os.environ.get(REVIEW_TAINTED_ENV)
    if (not _DOC_ID.fullmatch(doc) or provider not in ("claude", "codex")
            or not valid_session(sid) or tainted not in ("0", "1")):
        raise ValueError("Invalid brain review scope.")
    return doc, session, tainted == "1"


class Brain:
    def __init__(self, *, vault=None, index=None, store=None, session=None, profile=None):
        self._tools = _PROFILES[profile]
        self._review = profile in _REVIEW_PROFILES
        self._read_only_search = self._review or profile == "consolidate"
        self._review_doc, self._tainted = None, False
        if self._review:
            self._review_doc, session, self._tainted = _review_scope()
        self._memory_source = f"reviewer:{session}" if profile == "review-memory" else None
        self._schemas = (_SCHEMAS if self._memory_source is None else
                         {**_SCHEMAS, "memory_add": _REVIEW_ADD_SCHEMA})
        # All readers must still treat vault entries as untrusted data.
        purpose = "consolidation-memory" if profile in ("consolidate", "review-memory") else "memory"
        self._vault = Vault() if vault is None else vault
        self._memory = MemoryTools(self._vault, write_purpose=purpose,
                                   review_patch=profile == "review-memory")
        self._index = index
        self._store = store
        self._session = uuid4().hex if session is None else session

    @cached_property
    def _commitment_definitions(self):
        # The daemon imports the consolidation schedule without serving MCP.
        from .tools.commitments import CommitmentTools

        tools = CommitmentTools(self._vault, self._index)
        methods = {name: getattr(tools, name) for name in _COMMITMENT_TOOLS}
        return {name: (method, _schema(method)) for name, method in methods.items()}

    def _definition(self, name):
        if name in _COMMITMENT_TOOLS:
            return self._commitment_definitions[name]
        return _METHODS[name], self._schemas[name]

    @cached_property
    def _skills(self):
        store = self._store
        if store is None:
            # The store shares v1 skill metadata parsing, which imports the
            # legacy registry. Defer it until a skill is actually requested.
            from .skill_store import SkillStore

            store = SkillStore()
        return SkillTools(store, session=self._session, actor="reviewer" if self._review else "foreground",
                          tainted=self._tainted)

    def _search(self, **arguments):
        with Index(self._index) as opened:
            return session_search(index=opened, **arguments)

    def list_tools(self):
        tools = []
        for name in self._tools:
            method, schema = self._definition(name)
            schema = deepcopy(schema)
            description = getdoc(method)
            if self._read_only_search and name == "session_search":
                schema["properties"]["mode"].update(enum=["read"], description='Use "read".')
                schema["required"].append("mode")
                description = "Read one page of an indexed document by ID."
                if self._review:
                    schema["properties"]["doc"].update(enum=[self._review_doc], description="ID of the session under review.")
                    schema["required"].append("doc")
            tools.append({"name": name, "description": description, "inputSchema": schema})
        return {"tools": tools}

    def call(self, name, arguments):
        if name not in self._tools:
            return _result("Error: Tool is outside the brain profile.", error=True)
        try:
            method, schema = self._definition(name)
            if not _valid(arguments, schema):
                return _result("Error: Arguments do not match the tool schema.", error=True)
            if self._read_only_search and name == "session_search" and arguments.get("mode") != "read":
                return _result('Error: This brain profile requires session_search mode "read".', error=True)
            if name == "session_search":
                if self._review and arguments.get("doc") != self._review_doc:
                    return _result("Error: This brain profile can read only the session under review.", error=True)
                text = self._search(**arguments)
            elif name in _COMMITMENT_TOOLS:
                text = method(**arguments)
            else:
                if name == "memory_add" and self._memory_source is not None:
                    arguments = {**arguments, "source": self._memory_source}
                target = self._memory if name.startswith("memory_") else self._skills
                text = getattr(target, name)(**arguments)
            clean = redact.for_display(text)[:_RESULT_CHARS]
            return _result(clean, error=text.startswith(("Error:", "Refused:")))
        except (Exception, SystemExit):      # a tool that exits ends its call, never the server
            return _result("Error: The brain tool could not complete safely.", error=True)


# Framing copied from jarvis.v2.mcp so importing it cannot load its tool catalog.
class RPCError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _route(method, params, brain):
    if method == "initialize":
        version = params.get("protocolVersion")
        if not isinstance(version, str):
            raise RPCError(-32602, "protocolVersion must be a string")
        return {"protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-brain", "version": "3.0"}}
    if method in {"notifications/initialized", "ping"}:
        return {}
    if method == "tools/list":
        return brain.list_tools()
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            raise RPCError(-32602, "tools/call requires a name")
        return brain.call(name, params.get("arguments", {}))
    raise RPCError(-32601, "Method not found")


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle(message, brain):
    if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and type(message["id"]) not in (str, int))):
        return _error(None, -32600, "Invalid Request")
    # Notifications must not execute tools.
    if "id" not in message:
        return None
    request_id = message["id"]
    try:
        params = message.get("params", {})
        if not isinstance(params, dict):
            raise RPCError(-32602, "params must be an object")
        return {"jsonrpc": "2.0", "id": request_id,
                "result": _route(message["method"], params, brain)}
    except RPCError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception:
        return _error(request_id, -32603, "Internal error")


def _reject_constant(_value):
    raise ValueError("Invalid JSON constant")


def serve(source, sink, brain):
    while line := source.readline(_MAX_LINE + 1):
        if len(line) > _MAX_LINE:
            while line and not line.endswith(b"\n"):
                line = source.readline(_MAX_LINE + 1)
            response = _error(None, -32700, "Request line too long")
        elif not line.endswith(b"\n"):
            response = _error(None, -32700, "Incomplete request line")
        else:
            try:
                message = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                response = _error(None, -32700, "Parse error")
            else:
                response = _handle(message, brain)
        if response is not None:
            data = json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n"
            try:
                sink.write(data)
                sink.flush()
            except BrokenPipeError:
                return


def _drain_chatter(read_fd, diagnostic_fd):
    # Client applications log stderr. Even a tool's unexpected print may
    # include private input or output, so forward a fixed notice only.
    with os.fdopen(read_fd, "rb", buffering=0) as stream:
        if stream.read(8192):
            try:
                os.write(diagnostic_fd, b"jarvis-brain suppressed unexpected tool output.\n")
            except OSError:
                pass
            while stream.read(8192):
                pass


def main():
    profile = os.environ.get(PROFILE_ENV)
    if profile not in _PROFILES:
        print("Error: Unknown brain profile.", file=sys.stderr)
        return 1
    stdout_fd, stderr_fd = sys.stdout.fileno(), sys.stderr.fileno()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        with (os.fdopen(os.dup(stdout_fd), "w", encoding="utf-8", buffering=1) as protocol,
              os.fdopen(os.dup(stderr_fd), "wb") as diagnostic):
            read_fd, write_fd = os.pipe()
            drain = Thread(target=_drain_chatter, args=(read_fd, diagnostic.fileno()), daemon=True)
            drain.start()
            try:
                os.dup2(write_fd, stderr_fd)
                os.dup2(stderr_fd, stdout_fd)
            finally:
                os.close(write_fd)
            try:
                serve(sys.stdin.buffer, protocol, Brain(profile=profile))
            finally:
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                finally:
                    os.dup2(protocol.fileno(), stdout_fd)
                    os.dup2(diagnostic.fileno(), stderr_fd)
                    drain.join(timeout=1)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception:
        return 1
    return 0

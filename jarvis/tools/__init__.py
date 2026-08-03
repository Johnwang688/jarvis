"""Tool registry.

A tool is a plain Python function. The `@tool` decorator derives its JSON
schema from type hints, so there is no schema to keep in sync by hand:

    @tool
    def read_file(path: Annotated[str, "Path to read"]) -> str:
        '''Read a UTF-8 text file.'''

`Annotated[T, "description"]` supplies the per-parameter description that the
model reads when deciding how to call the tool. Parameters with defaults are
optional; everything else is required.
"""

from __future__ import annotations

import inspect
import json
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable

from .secrets import scrub

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolResult:
    """What a tool hands back.

    A `tool` message in the OpenAI wire format can only hold a string, so a
    tool that produces an image returns it here and the agent loop attaches it
    as a separate user message. Tools that only produce text can just return a
    plain str.
    """

    text: str
    image_b64: str | None = None
    mime: str = "image/png"

    def __str__(self) -> str:
        return self.text


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]
    dangerous: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


REGISTRY: dict[str, Tool] = {}


def _json_type(annotation: Any) -> dict[str, Any]:
    origin = typing.get_origin(annotation)

    if origin is typing.Annotated:
        base, *meta = typing.get_args(annotation)
        node = _json_type(base)
        for item in meta:
            if isinstance(item, str):
                node["description"] = item
        return node

    # Optional[X] / X | None -> schema of X
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return {}

    if origin in (list, set, tuple):
        args = typing.get_args(annotation)
        node: dict[str, Any] = {"type": "array"}
        if args:
            node["items"] = _json_type(args[0])
        return node

    if origin is dict:
        return {"type": "object"}

    if isinstance(annotation, type) and issubclass(annotation, bool):
        return {"type": "boolean"}

    return {"type": _PY_TO_JSON.get(annotation, "string")}


def tool(func: Callable[..., Any] | None = None, *, dangerous: bool = False):
    """Register a function as a tool. `dangerous=True` requires user approval."""

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        hints = typing.get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)

        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, param in signature.parameters.items():
            properties[name] = _json_type(hints.get(name, str))
            if param.default is inspect.Parameter.empty:
                required.append(name)

        doc = inspect.getdoc(fn) or ""
        REGISTRY[fn.__name__] = Tool(
            name=fn.__name__,
            description=doc.strip(),
            schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            func=fn,
            dangerous=dangerous,
        )
        return fn

    return wrap(func) if func else wrap


def specs(names: list[str] | None = None) -> list[dict[str, Any]]:
    tools = REGISTRY.values() if names is None else [REGISTRY[n] for n in names]
    return [t.spec() for t in tools]


def dispatch(
    name: str, raw_arguments: str, approve: Callable[[Tool, dict], bool] | None = None
) -> ToolResult:
    """Execute a tool call and return its result.

    Failures come back as text rather than raised exceptions: the model sees
    them as a tool result and gets a chance to correct itself, which is the
    whole point of an agent loop.

    Every result is scrubbed of `.env` content on the way out. This is the last
    point before a string becomes a `tool` message, so it is the only place
    that catches a leak from a tool that never names the file — a recursive
    grep, a fetched page, a browser snapshot. See tools/secrets.py.
    """
    result = _dispatch(name, raw_arguments, approve)
    result.text = scrub(result.text)
    return result


def _dispatch(
    name: str, raw_arguments: str, approve: Callable[[Tool, dict], bool] | None = None
) -> ToolResult:
    entry = REGISTRY.get(name)
    if entry is None:
        return ToolResult(f"Error: no tool named {name!r}. Available: {', '.join(sorted(REGISTRY))}")

    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return ToolResult(
            f"Error: arguments were not valid JSON ({exc}). Received: {raw_arguments[:200]}"
        )

    if not isinstance(arguments, dict):
        return ToolResult(f"Error: arguments must be a JSON object, got {type(arguments).__name__}")

    missing = [k for k in entry.schema["required"] if k not in arguments]
    if missing:
        return ToolResult(f"Error: missing required argument(s): {', '.join(missing)}")

    if entry.dangerous and approve is not None and not approve(entry, arguments):
        return ToolResult("The user declined to run this. Ask what they would like instead.")

    try:
        result = entry.func(**arguments)
    except TypeError as exc:
        return ToolResult(f"Error: bad arguments for {name}: {exc}")
    except Exception as exc:  # surfaced to the model, not the user
        return ToolResult(f"Error: {type(exc).__name__}: {exc}")

    if isinstance(result, ToolResult):
        return result
    if isinstance(result, str):
        return ToolResult(result)
    return ToolResult(json.dumps(result, default=str, indent=2))


from . import (  # noqa: E402,F401  (registers the tools)
    browsing,
    clock,
    contextctl,
    desktop,
    files,
    gmail,
    drive,
    google_workspace,
    memory,
    onshape,
    plan,
    sessions,
    shell,
    skills,
    subagent,
    discord,

    voicectl,
    web,
    whiteboardctl,
    workflows,
)

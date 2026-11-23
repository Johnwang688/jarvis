"""Statically check imports across the v2/v3 package boundary.

An accident guard against a confused agent or a careless edit, not an
adversarial boundary (AGENTS.md: confinement is against a confused agent).
It reads source, never runs it. Stated limits (verifier r2, 2026-11-23):
runtime reflection; `__import__`/`import_module` with computed arguments, a
dotted `fromlist` entry, a `globals` dict naming `__package__`, or an aliased
builtin; and a v2 namespace folder sharing a plain module's name (a
`from jarvis.v2.<allowed> import x` there may be misread as a submodule).
"""

import ast
from dataclasses import dataclass
from pathlib import Path


ALLOWED_V2: dict[str, str] = {
    "jarvis.v2.owner_ops": "shared gate/ops: daemon2 status, busy refusal and systemctl helpers (A-2)",
    "jarvis.v2.redact": "Reuse credential redaction for text shown to owners and models.",
    "jarvis.v2.push": "Reuse encrypted owner Web Push with count and rank only.",
    "jarvis.v2.quiet": "Reuse quiet-hours settings and local-time window calculations.",
    "jarvis.v2.quiet_defer": "Reuse approval deferral and its morning review records.",
    "jarvis.v2.stores": "Reuse atomic disk writes for persisted records.",
    "jarvis.v2.mcp": "Reuse newline-delimited JSON-RPC framing for MCP over stdio.",
    "jarvis.v2.skills_link": "Reuse skill symlink management for Claude Code and Codex.",
    "jarvis.v2.remote_auth": "Reuse phone passkey pairing, verification and session handling.",
    "jarvis.v2.remote_secure": "Reuse remote listener and tailnet checks for owner authority.",
}

_UNRESOLVABLE = "<unresolvable>"
_JARVIS_DIR = Path(__file__).parent.parent


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    module: str


def _absolute(name: str, package: str, level: int) -> str:
    if level == 0:
        return name

    parts = package.split(".")
    if level > len(parts) or not all(part.isidentifier() for part in parts):
        return _UNRESOLVABLE

    base = ".".join(parts[: len(parts) - level + 1])
    return f"{base}.{name}" if name else base


def _from_target(
    base: str, member: str, boundary: str, *, require_module: bool = False
) -> str:
    if member == "*" or base == _UNRESOLVABLE:
        return base

    candidate = f"{base}.{member}"
    if not all(part.isidentifier() for part in candidate.split(".")):
        return base
    package_root = base in ("jarvis", boundary)
    if package_root and not require_module:
        return candidate
    if package_root or base.startswith(boundary + "."):
        # A from-import can name a symbol of the source module or a child
        # module. Use the source layout to identify nested modules.
        child = _JARVIS_DIR.joinpath(*candidate.split(".")[1:])
        if child.with_suffix(".py").is_file() or child.is_dir():
            return candidate
    return base


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_arg(node: ast.Call, position: int, name: str) -> ast.AST | None:
    if position < len(node.args):
        return node.args[position]
    return next((keyword.value for keyword in node.keywords if keyword.arg == name), None)


def _builtin_targets(node: ast.Call, target: str, package: str, boundary: str) -> list[str]:
    level = _call_arg(node, 4, "level")
    if level is not None and not (
        isinstance(level, ast.Constant) and isinstance(level.value, int) and level.value >= 0
    ):
        return [_UNRESOLVABLE]

    base = _absolute(target, package, 0 if level is None else level.value)
    fromlist = _call_arg(node, 3, "fromlist")
    if not isinstance(fromlist, (ast.List, ast.Tuple)):
        return [base]

    # Like a from-import, report children when they resolve and the base for
    # attributes. One call can spell the same target more than once.
    targets = []
    for item in fromlist.elts:
        member = _string(item)
        module = base if member is None else _from_target(
            base, member, boundary, require_module=True
        )
        targets.append(module)
    return list(dict.fromkeys(targets)) or [base]


def _import_targets(tree: ast.AST, package: str, boundary: str):
    nodes = list(ast.walk(tree))
    importlib_names = {"importlib"}
    import_module_names = {"import_module"}
    for node in nodes:
        if isinstance(node, ast.Import):
            importlib_names.update(
                alias.asname for alias in node.names if alias.name == "importlib" and alias.asname
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "importlib":
            import_module_names.update(
                alias.asname for alias in node.names if alias.name == "import_module" and alias.asname
            )

    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node.module or "", package, node.level)
            for alias in node.names:
                yield node.lineno, _from_target(base, alias.name, boundary)
        elif isinstance(node, ast.Call):
            match node.func:
                case ast.Attribute(value=ast.Name(id=owner), attr="import_module") if owner in importlib_names:
                    is_import_module = True
                case ast.Name(id=name) if name in import_module_names:
                    is_import_module = True
                case ast.Name(id="__import__"):
                    is_import_module = False
                case _:
                    continue

            target = _string(_call_arg(node, 0, "name"))
            if target is None:
                continue
            if is_import_module:
                name = target.lstrip(".")
                relative_package = _string(_call_arg(node, 1, "package")) or ""
                yield node.lineno, _absolute(name, relative_package, len(target) - len(name))
            else:
                for module in _builtin_targets(node, target, package, boundary):
                    yield node.lineno, module


def _python_files(directory: Path):
    if not directory.is_dir():
        return

    pending = [(directory, frozenset())]
    while pending:
        current, ancestors = pending.pop()
        stat = current.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in ancestors:
            continue
        # Check only ancestors so aliases get their own package and path.
        ancestors = ancestors | {identity}
        for child in current.iterdir():
            if child.is_dir():
                pending.append((child, ancestors))
            elif child.name.endswith(".py") and child.is_file():
                yield child


def _violations(
    directory: Path, package: str, boundary: str, allowed: dict[str, str]
) -> list[Violation]:
    found = []
    for path in _python_files(directory):
        file_package = ".".join((package, *path.relative_to(directory).parent.parts))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except (SyntaxError, UnicodeError, RecursionError) as exc:
            found.append(Violation(path, getattr(exc, "lineno", None) or 0, "<unparseable>"))
            continue

        for line, module in _import_targets(tree, file_package, boundary):
            if module == _UNRESOLVABLE or (
                (module == boundary or module.startswith(boundary + ".")) and module not in allowed
            ):
                found.append(Violation(path, line, module))
    return sorted(found, key=lambda violation: (violation.path, violation.line))


def violations(v3_dir: Path, package: str = "jarvis.v3") -> list[Violation]:
    """Find v3 imports of v2 modules outside the allowlist."""
    return _violations(v3_dir, package, "jarvis.v2", ALLOWED_V2)


def reverse_violations(v2_dir: Path, package: str = "jarvis.v2") -> list[Violation]:
    """Find v2 imports of v3."""
    return _violations(v2_dir, package, "jarvis.v3", {})

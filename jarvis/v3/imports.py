"""Statically check imports across the v2/v3 package boundary."""

import ast
from dataclasses import dataclass
from pathlib import Path


ALLOWED_V2: dict[str, str] = {
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


def _absolute(name: str, package: str) -> str:
    suffix = name.lstrip(".")
    level = len(name) - len(suffix)
    if level == 0:
        return name

    parts = package.split(".")
    if level > len(parts) or not all(part.isidentifier() for part in parts):
        return _UNRESOLVABLE

    base = ".".join(parts[: len(parts) - level + 1])
    return f"{base}.{suffix}" if suffix else base


def _from_target(base: str, member: str, boundary: str) -> str:
    if member == "*" or base == _UNRESOLVABLE:
        return base

    candidate = f"{base}.{member}"
    if base in ("jarvis", boundary):
        return candidate
    if base.startswith(boundary + "."):
        # A from-import can name a symbol of the source module or a child
        # module. Use the source layout to identify nested modules.
        child = _JARVIS_DIR.joinpath(*candidate.split(".")[1:])
        if child.with_suffix(".py").is_file() or (child / "__init__.py").is_file():
            return candidate
    return base


def _string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


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
            base = _absolute("." * node.level + (node.module or ""), package)
            for alias in node.names:
                yield node.lineno, _from_target(base, alias.name, boundary)
        elif isinstance(node, ast.Call) and node.args:
            match node.func:
                case ast.Attribute(value=ast.Name(id=owner), attr="import_module") if owner in importlib_names:
                    is_import_module = True
                case ast.Name(id=name) if name in import_module_names:
                    is_import_module = True
                case ast.Name(id="__import__"):
                    is_import_module = False
                case _:
                    continue

            target = _string(node.args[0])
            if target is None:
                continue
            relative_package = ""
            if target.startswith("."):
                package_arg = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "package"),
                    None,
                )
                if package_arg is None and is_import_module and len(node.args) >= 2:
                    package_arg = node.args[1]
                relative_package = _string(package_arg) or ""
            yield node.lineno, _absolute(target, relative_package)


def _violations(
    directory: Path, package: str, boundary: str, allowed: dict[str, str]
) -> list[Violation]:
    found = []
    for path in directory.rglob("*.py"):
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

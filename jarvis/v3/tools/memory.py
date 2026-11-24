"""Session-local memory tools for v3 agents."""

from functools import wraps
from threading import RLock

from jarvis.v3.vault import Vault, VaultError, entry_summary


_LIST_CHARS = 48_000
_SEARCH_CHARS = 24_000
_QUERY_CHARS = 1_000


def _tool(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except VaultError as exc:
                return f"Error: {exc}"
            except Exception:
                return "Error: Memory operation could not complete."
    return call


class MemoryTools:
    """Bind one instance per session. The optional index is reserved for vault FTS."""

    def __init__(self, vault: Vault, *, index=None):
        self._vault = vault
        self._reads = {}
        self._flags = []
        self._lock = RLock()

    def flags(self) -> list[dict[str, str]]:
        with self._lock:
            return [flag.copy() for flag in self._flags]

    @_tool
    def memory_list(self, folder: str = "") -> str:
        """List the current memory entries and their descriptions."""
        lines = [entry_summary(location, entry) for location, entry in self._vault.entries(folder)]
        return _bounded(lines, _LIST_CHARS) or "Memory is empty."

    @_tool
    def memory_read(self, name: str) -> str:
        """Read a memory entry and remember its revision for patching."""
        entry = self._vault.read(name)
        text = entry.markdown()
        self._reads[entry.location] = entry.revision
        return text

    @_tool
    def memory_add(self, folder: str, name: str, description: str, body: str, source: str) -> str:
        """Add a new memory entry with its provenance."""
        entry, flags = self._vault.add(folder, name, description, body, source)
        self._flags.extend(flags)
        return f"Added {entry.location.ref}."

    @_tool
    def memory_patch(self, name: str, old: str, new: str) -> str:
        """Replace one unique substring in a memory body read in this session."""
        entry, flags = self._vault.patch(name, old, new, self._reads)
        self._reads[entry.location] = entry.revision
        self._flags.extend(flags)
        return f"Patched {entry.location.ref}."

    @_tool
    def memory_archive(self, name: str, replaced_by: str = "") -> str:
        """Archive a memory entry and optionally identify its replacement."""
        entry, flags = self._vault.archive(name, replaced_by)
        self._reads.pop(entry.location, None)
        self._flags.extend(flags)
        return f"Archived {entry.location.ref}."

    @_tool
    def memory_search(self, query: str) -> str:
        """Find a substring across active vault entries."""
        if not isinstance(query, str) or not query.strip() or len(query) > _QUERY_CHARS or "\0" in query:
            raise VaultError("Supply a short, nonempty search query.")
        needle = query.strip().casefold()
        matches = []
        for location, entry in self._vault.entries():
            if entry is None:
                continue
            text = entry.markdown()
            if any(needle in value.casefold() for value in (text, location.ref, entry.description, entry.source)):
                try:
                    entry.scan_markdown(text)
                except VaultError:
                    continue
                matches.append(f"--- {location.ref} ---\n{text}")
        return _bounded(matches, _SEARCH_CHARS) or "No matching memories."


def _bounded(lines: list[str], cap: int) -> str:
    return "\n".join(lines)[:cap]

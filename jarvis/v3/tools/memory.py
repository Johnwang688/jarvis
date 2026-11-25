"""Session-local memory tools for v3 agents."""

from functools import wraps
from threading import RLock
from typing import Annotated

from jarvis.v3.content_scan import Purpose
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

    def __init__(self, vault: Vault, *, index=None, write_purpose: Purpose = "memory", review_patch: bool = False):
        self._vault = vault
        self._write_purpose = write_purpose
        self._review_patch = review_patch
        self._reads = {}
        self._flags = []
        self._lock = RLock()

    def flags(self) -> list[dict[str, str]]:
        with self._lock:
            return [flag.copy() for flag in self._flags]

    @_tool
    def memory_list(self, folder: Annotated[str, "Limit the list to this vault folder."] = "") -> str:
        """List the current memory entries and their descriptions."""
        lines = [entry_summary(location, entry) for location, entry in self._vault.entries(folder)]
        return _bounded(lines, _LIST_CHARS) or "Memory is empty."

    @_tool
    def memory_read(self, name: Annotated[str, "Entry name or folder/name reference."]) -> str:
        """Read a memory entry and remember its revision for patching."""
        entry = self._vault.read(name)
        text = entry.markdown()
        self._reads[entry.location] = entry.revision
        return text

    @_tool
    def memory_add(
        self,
        folder: Annotated[str, "Vault folder for the new entry."],
        name: Annotated[str, "Name of the new entry."],
        description: Annotated[str, "One-line description of the entry."],
        body: Annotated[str, "Markdown body to save."],
        source: Annotated[str, "Where the information came from."],
    ) -> str:
        """Add a new memory entry with its provenance."""
        entry, flags = self._vault.add(folder, name, description, body, source, purpose=self._write_purpose)
        self._flags.extend(flags)
        return f"Added {entry.location.ref}."

    @_tool
    def memory_patch(
        self,
        name: Annotated[str, "Reference to an entry read in this session."],
        old: Annotated[str, "Substring that must occur exactly once."],
        new: Annotated[str, "Text to replace that substring with."],
    ) -> str:
        """Replace one unique substring in a memory body read in this session."""
        entry, flags = self._vault.patch(name, old, new, self._reads, purpose=self._write_purpose,
                                        review_patch=self._review_patch)
        self._reads[entry.location] = entry.revision
        self._flags.extend(flags)
        return f"Patched {entry.location.ref}."

    @_tool
    def memory_archive(
        self,
        name: Annotated[str, "Entry name or folder/name reference."],
        replaced_by: Annotated[str, "Reference to the replacement entry, if any."] = "",
    ) -> str:
        """Archive a memory entry and optionally identify its replacement."""
        entry, flags = self._vault.archive(name, replaced_by)
        self._reads.pop(entry.location, None)
        self._flags.extend(flags)
        return f"Archived {entry.location.ref}."

    @_tool
    def memory_search(self, query: Annotated[str, "Nonempty substring to find."]) -> str:
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

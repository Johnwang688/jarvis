"""Search the private v3 session index for context."""

from itertools import groupby
from typing import Annotated

from jarvis import untrusted
from jarvis.v2 import redact
from jarvis.v3.index import Index


_OUTPUT_CHARS = 24_000


def _clean(text):
    return redact.for_model(untrusted.strip_invisible(text))


def _render(docs, mode):
    lines = []
    # Fit meaningful excerpts from each of the selected sessions in one reply.
    previews = sum(len(doc.get("messages", ())) for doc in docs)
    preview_chars = min(500, 18_000 // max(1, previews))
    next_page = None
    for doc in docs:
        title = " ".join(_clean(doc["title"]).split())[:240]
        source = _clean(doc["source"])
        lines.append(f"doc={doc['id']} [{source}] {title}")
        for seq, parts in groupby(doc.get("messages", ()), key=lambda row: row["seq"]):
            message = next(parts)
            complete = message["text"] + "".join(part["text"] for part in parts)
            text = _clean(complete)
            if text == complete:
                text = message.get("excerpt", text)
            if mode != "read" and len(text) > preview_chars:
                text = text[:preview_chars] + " …"
            role = _clean(message["role"])
            lines.append(f"#{seq} {role} {text}")
        next_page = doc.get("next_page")
    notes = (f"Next page {next_page}.",) if next_page is not None else ()
    return untrusted.fence("\n".join(lines), "session index", max_chars=_OUTPUT_CHARS, notes=notes)


def _search(index, query, mode, doc, around, limit):
    if mode == "discover":
        docs = index.discover(query, limit)
    elif mode == "recent":
        docs = index.recent(limit)
    elif mode == "scroll":
        result = index.scroll(doc, around, limit)
        docs = [result] if result else []
    else:
        result = index.read(doc, page=around)
        docs = [result] if result else []
    return _render(docs, mode) if docs else "No matching sessions."


def session_search(
    query: Annotated[str, "Search text in discover mode."] = "",
    mode: Annotated[str, "Choose discover, scroll, read, or recent."] = "discover",
    doc: Annotated[str, "Indexed document ID for scroll or read."] = "",
    around: Annotated[int, "Message sequence for scroll or page number for read."] = 0,
    limit: Annotated[int, "Maximum number of results to return."] = 5,
    *,
    index=None,
) -> str:
    """Discover, scroll, read or browse recent indexed sessions."""
    if mode not in ("discover", "scroll", "read", "recent"):
        return "Choose discover, scroll, read, or recent."
    try:
        if index is not None:
            return _search(index, query, mode, doc, around, limit)
        with Index() as opened:
            return _search(opened, query, mode, doc, around, limit)
    except Exception:
        return "Session search could not complete."

"""Indexed document provenance used by consolidation."""

import json
import re


_TRANSCRIPTS = frozenset({"claude", "codex", "jarvis", "t3", "transcript"})
_MESSAGE = re.compile(r"#([0-9]+)\Z")


def owner_authored(doc):
    if doc["source"] in _TRANSCRIPTS:
        return True
    try:
        meta = json.loads(doc["meta_json"] or "null")
    except (ValueError, RecursionError):
        return False
    return isinstance(meta, dict) and meta.get("from_owner") == 1


def provenance(doc):
    source, ref = doc["source"], doc["ref"]
    return f"transcript:{source}/{ref}" if source in _TRANSCRIPTS else f"{source}:{ref}"


def indexed_document(index, citation):
    """Resolve a citation with message chunks for sent mail or a cited turn."""
    source, colon, ref = citation.partition(":")
    if not colon or not source or not ref:
        return None
    if source == "transcript":
        source, slash, ref = ref.partition("/")
        if not slash or source not in _TRANSCRIPTS or not ref:
            return None
        # Prefer the full reference, since an indexed path can contain '#'.
        doc = index.document(source, ref)
        if doc is not None:
            return doc
        match = _MESSAGE.search(ref)
        if match and match.start() and (doc := index.document(source, ref[:match.start()])):
            return index.message(doc["id"], int(match[1]))
        return None
    if source in _TRANSCRIPTS:
        return None
    doc = index.document(source, ref)
    if source == "gmail" and doc is not None and owner_authored(doc):
        return index.message(doc["id"], 1) or doc
    return doc

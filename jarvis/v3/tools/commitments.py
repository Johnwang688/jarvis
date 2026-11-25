"""Typed consolidation tools for the commitments table."""

from functools import wraps
from typing import Annotated

from jarvis import untrusted
from jarvis.v3 import commitments, content_scan, vault
from jarvis.v3.ingest.gmail import owner_written_text
from jarvis.v3.index import Index
from jarvis.v3.provenance import indexed_document, owner_authored


_PURPOSE = "consolidation-memory"
_EVIDENCE_CAP = 300


def _tool(method):
    @wraps(method)
    def call(self, *args, **arguments):
        try:
            return method(self, *args, **arguments)
        except (commitments.CommitmentError, vault.VaultError) as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: The commitment operation could not complete safely."
    return call


def _scan_fields(*fields):
    for value in fields:
        if not content_scan.scan(value, purpose=_PURPOSE).ok:
            raise commitments.CommitmentError("Memory scanner blocked a commitment field.")


def _validate_evidence(evidence):
    if (not isinstance(evidence, str) or len(evidence) > _EVIDENCE_CAP
            or not untrusted.strip_invisible(evidence).strip()):
        raise commitments.CommitmentError(f"Evidence must be a verbatim quote of 1 to {_EVIDENCE_CAP} characters.")


def _check_owner_evidence(doc, direction, evidence):
    if direction != "owner-owes":
        return
    parts = doc.get("messages", ())
    text = "".join(part["text"] for part in parts)
    if doc["source"] == "gmail":
        text = owner_written_text(text) if owner_authored(doc) else ""
    elif not parts or parts[0]["role"] != "user":
        text = ""
    if evidence not in text:
        raise commitments.CommitmentError("Evidence must occur verbatim in the owner's sent mail above any quote or forward, or in the cited owner turn.")


def _check_cap(before, after):
    over = len(after) - vault.ENTRY_CAP
    if after != before and over > 0:
        ids = ", ".join(row.id for row in commitments.parse(before)) or "(none)"
        raise commitments.CommitmentError(f"Free {over} characters to fit commitments. Current rows: {ids}.")


class CommitmentTools:
    def __init__(self, vault, index):
        self._vault = vault
        self._index = index

    def _document(self, source):
        with Index(self._index) as index:
            doc = indexed_document(index, source)
        if doc is None:
            raise commitments.CommitmentError("Source must name an indexed document or an existing transcript turn.")
        return doc

    @_tool
    def commitment_upsert(
        self,
        who: Annotated[str, "The person owed, or the person who owes the commitment."],
        what: Annotated[str, "A short description of the promise."],
        direction: Annotated[str, "Use owner-owes or owed-to-owner."],
        source: Annotated[str, "Indexed provenance. For owner-owes, cite sent mail or append #<message number> for an owner turn."],
        evidence: Annotated[str, "Verbatim quote supporting the promise, up to 300 characters. For owner-owes, quote the owner's own words."],
        due: Annotated[str, "Due date in YYYY-MM-DD, or empty."] = "",
    ) -> str:
        """Record a commitment or return its existing id. Owner promises require a verbatim quote from the cited owner text."""
        who, what, due_date, direction, source = commitments.validate_upsert(who, what, due, direction, source)
        _validate_evidence(evidence)
        _scan_fields(who, what, source)
        _check_owner_evidence(self._document(source), direction, evidence)

        def transform(body):
            nonlocal result
            updated, row, created = commitments.upsert(
                body, who=who, what=what, due=due_date.isoformat() if due_date else "",
                direction=direction, source=source)
            _check_cap(body, updated)
            result = f"Added {row.id}." if created else f"Already recorded {row.id}."
            return updated

        result = ""
        self._vault.rewrite("commitments", transform, purpose=_PURPOSE)
        return result

    @_tool
    def commitment_close(
        self,
        id: Annotated[str, "The commitment row id."],
        state: Annotated[str, "Use done or dropped."],
        source: Annotated[str, "Indexed provenance for closing. For owner-owes, cite sent mail or append #<message number> for an owner turn."],
        evidence: Annotated[str, "Verbatim quote supporting closure, up to 300 characters. For owner-owes, quote the owner's own words."],
    ) -> str:
        """Close a commitment with cited evidence. Owner promises require a verbatim quote from the cited owner text."""
        id, state = commitments.validate_close(id, state)
        source = commitments.validate_source(source)
        _validate_evidence(evidence)
        _scan_fields(source)
        doc = self._document(source)

        def transform(body):
            updated, row = commitments.close(body, id, state)
            _check_owner_evidence(doc, row.direction, evidence)
            _check_cap(body, updated)
            return updated

        self._vault.rewrite("commitments", transform, purpose=_PURPOSE)
        return f"Marked {id} {state}."

"""Gmail tools: search, read, send.

Narrow and typed on purpose (see CLAUDE.md safety design): each is a
separately gateable action, and gmail_send is dangerous=True, so every
outbound email needs the owner's yes — a y/N prompt in the CLI, an
authorization card in the HUD.

Credentials: google_auth.access_token(). The token goes into an
Authorization header here and is never part of a tool result — the model
only sees Google's API responses. On a 401 the cached token is dropped and
the call retried once, so an expiry mid-conversation self-heals.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Annotated

import httpx

from .. import google_auth
from . import tool

API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """One authed API call, retried once on 401."""
    for attempt in (1, 2):
        response = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {google_auth.access_token()}"},
            timeout=30,
            **kwargs,
        )
        if response.status_code == 401 and attempt == 1:
            google_auth.invalidate()
            continue
        return response
    return response  # unreachable; keeps type-checkers calm


def _fail(response: httpx.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except Exception:
        message = response.text[:200]
    return f"Error: Gmail API returned {response.status_code}: {message}"


def _b64text(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace"
    )


def _extract_text(payload: dict) -> str:
    """Best-effort plain text from a message payload tree.

    Prefers text/plain; falls back to stripped text/html. Attachments are
    ignored — say so rather than pretending they were read.
    """
    body = payload.get("body", {}).get("data")
    if payload.get("mimeType") == "text/plain" and body:
        return _b64text(body)
    for part in payload.get("parts") or []:
        text = _extract_text(part)
        if text:
            return text
    if payload.get("mimeType") == "text/html" and body:
        from bs4 import BeautifulSoup

        return BeautifulSoup(_b64text(body), "html.parser").get_text("\n", strip=True)
    return ""


@tool
def gmail_search(
    query: Annotated[str, "Gmail query, e.g. 'is:unread', 'from:bob subject:invoice'"] = "in:inbox",
    max_results: Annotated[int, "How many messages, 1-25"] = 10,
) -> str:
    """Search the owner's mailbox. Returns id, sender, date, subject, snippet.

    Use gmail_read with an id for a full message body.
    """
    max_results = max(1, min(int(max_results), 25))
    try:
        listing = _request(
            "GET", f"{API}/messages", params={"q": query, "maxResults": max_results}
        )
        if listing.status_code != 200:
            return _fail(listing)
        refs = listing.json().get("messages", [])
        if not refs:
            return f"No messages match {query!r}."

        lines = []
        for ref in refs:
            detail = _request(
                "GET",
                f"{API}/messages/{ref['id']}",
                params={
                    "format": "metadata",
                    "metadataHeaders": ["From", "Subject", "Date"],
                },
            )
            if detail.status_code != 200:
                lines.append(f"- id {ref['id']} · (could not load headers)")
                continue
            message = detail.json()
            headers = {
                h["name"]: h["value"]
                for h in message.get("payload", {}).get("headers", [])
            }
            lines.append(
                f"- id {ref['id']} · {headers.get('From', '?')} · {headers.get('Date', '?')}\n"
                f"  {headers.get('Subject', '(no subject)')}\n"
                f"  {message.get('snippet', '').strip()}"
            )
        return "\n".join(lines)
    except (google_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool
def gmail_read(
    message_id: Annotated[str, "Message id from gmail_search"],
    max_chars: Annotated[int, "Cap on returned body length"] = 8000,
) -> str:
    """Read one email: headers plus the plain-text body (attachments ignored)."""
    try:
        response = _request(
            "GET", f"{API}/messages/{message_id}", params={"format": "full"}
        )
        if response.status_code != 200:
            return _fail(response)
        message = response.json()
        headers = {
            h["name"].lower(): h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        body = _extract_text(message.get("payload", {})) or "(no readable text body)"
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n\n[truncated at {max_chars} chars]"
        return (
            f"From: {headers.get('from', '?')}\n"
            f"To: {headers.get('to', '?')}\n"
            f"Date: {headers.get('date', '?')}\n"
            f"Subject: {headers.get('subject', '(no subject)')}\n\n{body}"
        )
    except (google_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"


@tool(dangerous=True)
def gmail_send(
    to: Annotated[str, "Recipient address(es), comma-separated"],
    subject: Annotated[str, "Subject line"],
    body: Annotated[str, "Plain-text message body"],
) -> str:
    """Send an email from the owner's Gmail account. Requires the owner's approval.

    Plain text only — no HTML, no attachments. The message is sent exactly as
    given, so show the owner the final wording before calling this.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        response = _request("POST", f"{API}/messages/send", json={"raw": raw})
        if response.status_code != 200:
            return _fail(response)
        return f"Sent to {to} — message id {response.json().get('id', '?')}."
    except (google_auth.AuthError, httpx.HTTPError) as exc:
        return f"Error: {exc}"

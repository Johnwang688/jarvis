"""Approval-gated creation and text editing for Google Docs and Slides."""

from __future__ import annotations

from typing import Annotated

import httpx

from .. import google_auth
from . import tool

DOCS = "https://docs.googleapis.com/v1"
SLIDES = "https://slides.googleapis.com/v1"


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {google_auth.access_token()}"}
        headers.update(kwargs.pop("headers", {}))
        response = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
        if response.status_code == 401 and attempt == 1:
            google_auth.invalidate()
            continue
        return response
    return response


def _json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except Exception:
        return {}


def _fail(response: httpx.Response) -> str:
    message = _json(response).get("error", {}).get("message", response.text[:300])
    return f"Google Workspace error ({response.status_code}): {message}"


@tool(dangerous=True)
def docs_create(
    title: Annotated[str, "Title of the new Google Doc"],
    content: Annotated[str, "Text to put in the new document"],
) -> str:
    """Create a Google Doc containing text. Requires owner approval."""
    created = _request("POST", f"{DOCS}/documents", json={"title": title})
    if created.status_code != 200:
        return _fail(created)
    document_id = _json(created).get("documentId", "")
    updated = _request(
        "POST", f"{DOCS}/documents/{document_id}:batchUpdate",
        json={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
    )
    if updated.status_code != 200:
        return _fail(updated)
    return f"Created Google Doc {title} ({document_id})."


@tool(dangerous=True)
def docs_replace_text(
    document_id: Annotated[str, "The Google Doc document ID"],
    content: Annotated[str, "Complete replacement text for the document body"],
) -> str:
    """Replace all body text in a Google Doc. Requires owner approval."""
    current = _request("GET", f"{DOCS}/documents/{document_id}")
    if current.status_code != 200:
        return _fail(current)
    body = _json(current).get("body", {}).get("content", [])
    end = body[-1].get("endIndex", 1) if body else 1
    requests = []
    if end > 2:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end - 1}}})
    requests.append({"insertText": {"location": {"index": 1}, "text": content}})
    updated = _request("POST", f"{DOCS}/documents/{document_id}:batchUpdate", json={"requests": requests})
    if updated.status_code != 200:
        return _fail(updated)
    return f"Replaced text in Google Doc {document_id}."


@tool(dangerous=True)
def slides_create(
    title: Annotated[str, "Title of the new Google Slides presentation"],
    content: Annotated[str, "Text for the first slide"],
) -> str:
    """Create a Google Slides presentation with a text box on its first slide. Requires owner approval."""
    created = _request("POST", f"{SLIDES}/presentations", json={"title": title})
    if created.status_code != 200:
        return _fail(created)
    presentation_id = _json(created).get("presentationId", "")
    presentation = _json(created)
    slides = presentation.get("slides", [])
    slide_id = slides[0].get("objectId") if slides else None
    if not slide_id:
        refreshed = _request("GET", f"{SLIDES}/presentations/{presentation_id}")
        slide_id = _json(refreshed).get("slides", [{}])[0].get("objectId")
    element_id = "jarvis_text_box"
    requests = [
        {"createShape": {"objectId": element_id, "shapeType": "TEXT_BOX", "elementProperties": {"pageObjectId": slide_id, "size": {"width": {"magnitude": 600, "unit": "PT"}, "height": {"magnitude": 300, "unit": "PT"}}, "transform": {"scaleX": 1, "scaleY": 1, "translateX": 60, "translateY": 60, "unit": "PT"}}}},
        {"insertText": {"objectId": element_id, "text": content}},
    ]
    updated = _request("POST", f"{SLIDES}/presentations/{presentation_id}:batchUpdate", json={"requests": requests})
    if updated.status_code != 200:
        return _fail(updated)
    return f"Created Google Slides presentation {title} ({presentation_id})."


@tool(dangerous=True)
def slides_replace_text(
    presentation_id: Annotated[str, "The Google Slides presentation ID"],
    content: Annotated[str, "Replacement text for text boxes in the presentation"],
) -> str:
    """Replace text in every text box in a Google Slides presentation. Requires owner approval."""
    presentation = _request("GET", f"{SLIDES}/presentations/{presentation_id}")
    if presentation.status_code != 200:
        return _fail(presentation)
    requests = []
    for slide in _json(presentation).get("slides", []):
        for element in slide.get("pageElements", []):
            shape = element.get("shape", {})
            if shape.get("text"):  # preserve the shape and replace its text
                object_id = element.get("objectId")
                text_elements = shape["text"].get("textElements", [])
                end = sum(len(e.get("textRun", {}).get("content", "")) for e in text_elements)
                if object_id and end:
                    requests.append({"deleteText": {"objectId": object_id, "textRange": {"type": "ALL"}}})
                    requests.append({"insertText": {"objectId": object_id, "text": content}})
    if not requests:
        return "No editable text boxes were found in that presentation."
    updated = _request("POST", f"{SLIDES}/presentations/{presentation_id}:batchUpdate", json={"requests": requests})
    if updated.status_code != 200:
        return _fail(updated)
    return f"Replaced text in Google Slides presentation {presentation_id}."

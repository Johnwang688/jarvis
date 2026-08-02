"""Read-only Google Drive tools: search files and read text documents."""

from __future__ import annotations

from typing import Annotated

import httpx

from .. import google_auth
from . import tool

API = "https://www.googleapis.com/drive/v3"


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """One authed API call, retried once on 401."""
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {google_auth.access_token()}"}
        headers.update(kwargs.pop("headers", {}))
        response = httpx.request(method, url, headers=headers, timeout=30, **kwargs)
        if response.status_code == 401 and attempt == 1:
            google_auth.invalidate()
            continue
        return response
    return response


def _fail(response: httpx.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except Exception:
        message = response.text[:300]
    return f"Google Drive error ({response.status_code}): {message}"


@tool
def drive_search(
    query: Annotated[str, "Drive search expression, such as name contains 'report'"],
    max_results: Annotated[int, "Maximum number of files to return, from 1 to 100"] = 20,
) -> str:
    """Search the owner's Google Drive files by name, type, or full-text query."""
    max_results = max(1, min(max_results, 100))
    response = _request(
        "GET",
        f"{API}/files",
        params={
            "q": f"trashed = false and ({query})",
            "pageSize": max_results,
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
        },
    )
    if response.status_code != 200:
        return _fail(response)
    files = response.json().get("files", [])
    if not files:
        return "No Drive files matched that query."
    return "\n".join(
        f"{item['id']} | {item.get('name', '(unnamed)')} | "
        f"{item.get('mimeType', '')} | modified {item.get('modifiedTime', '')}"
        for item in files
    )


@tool
def drive_read(
    file_id: Annotated[str, "The Google Drive file ID"],
) -> str:
    """Read the text of a Google document or downloadable text file from Drive."""
    metadata = _request("GET", f"{API}/files/{file_id}", params={"fields": "id,name,mimeType,size"})
    if metadata.status_code != 200:
        return _fail(metadata)
    item = metadata.json()
    mime = item.get("mimeType", "")
    if mime == "application/vnd.google-apps.document":
        response = _request(
            "GET", f"{API}/files/{file_id}/export",
            params={"mimeType": "text/plain"},
        )
    elif mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        response = _request("GET", f"{API}/files/{file_id}", params={"alt": "media"})
    else:
        return f"Cannot read {item.get('name', file_id)} as text (MIME type: {mime})."
    if response.status_code != 200:
        return _fail(response)
    return f"{item.get('name', file_id)}\n\n{response.text}"

@tool(dangerous=True)
def drive_create_text(
    name: Annotated[str, "Name for the new text file"],
    content: Annotated[str, "Text content to write into the new file"],
    parent_id: Annotated[str, "Optional Drive folder ID; leave empty for My Drive root"] = "",
) -> str:
    """Create a plain-text file in Google Drive. Requires owner approval."""
    metadata = {"name": name, "mimeType": "text/plain"}
    if parent_id:
        metadata["parents"] = [parent_id]
    body = f"--jarvis-boundary\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" \
        f"{__import__('json').dumps(metadata)}\r\n--jarvis-boundary\r\n" \
        "Content-Type: text/plain\r\n\r\n" + content + "\r\n--jarvis-boundary--\r\n"
    response = _request(
        "POST", f"{API}/files", params={"uploadType": "multipart", "fields": "id,name,mimeType"},
        content=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {google_auth.access_token()}",
            "Content-Type": "multipart/related; boundary=jarvis-boundary",
        },
    )
    if response.status_code not in (200, 201):
        return _fail(response)
    item = response.json()
    return f"Created Drive file {item.get('name', name)} ({item.get('id', '')})."


@tool(dangerous=True)
def drive_update_text(
    file_id: Annotated[str, "The Google Drive file ID to replace"],
    content: Annotated[str, "The complete replacement text content"],
) -> str:
    """Replace the contents of an existing plain-text Drive file. Requires owner approval."""
    response = _request(
        "PATCH", f"{API}/files/{file_id}",
        params={"uploadType": "media", "fields": "id,name,mimeType"},
        content=content.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {google_auth.access_token()}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    if response.status_code != 200:
        return _fail(response)
    item = response.json()
    return f"Updated Drive file {item.get('name', file_id)} ({item.get('id', file_id)})."

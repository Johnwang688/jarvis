"""Onshape auth + setup: API keys in, CAD tools out, the model never sees keys.

Same contract as google_auth (CLAUDE.md, safety design): the bundle at
`config.ONSHAPE_TOKEN_PATH` is written once by `jarvis auth onshape` — a CLI
subcommand, human-only, so the agent cannot initiate or widen its own access
— and read only here. tools/secrets.py makes the file unreadable through
every tool and scrubs its values out of tool output; the model only ever
sees Onshape API responses.

The bundle also pins where Jarvis may *write*: one sandbox document
(did/wid) chosen or created at setup. tools/onshape.py builds every write
URL from that pin and no CAD tool takes a document id for a write target,
so there is no argument the model could supply to reach another document.
Parts libraries are read-only sources, also listed here.

Unlike Google there is no OAuth dance: the owner creates an API key in
Onshape (account icon → My account → Developer → API keys) and pastes it
in. Deleting the key there kills Jarvis's access instantly. Auth on the
wire is HTTP Basic (access key as username, secret as password) — supported
by Onshape for exactly this kind of first-party use.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from . import config


class AuthError(RuntimeError):
    """Auth is missing or broken. Tools return the message as text."""


_DOC_URL = re.compile(r"documents/([0-9a-f]{24})(?:/w/([0-9a-f]{24}))?", re.IGNORECASE)


def parse_document_url(url: str) -> tuple[str, str | None]:
    """(documentId, workspaceId | None) from a cad.onshape.com document URL."""
    match = _DOC_URL.search(url.strip())
    if not match:
        raise ValueError(
            f"not an Onshape document URL: {url!r} "
            "(expected …/documents/<24-hex-id>/w/<24-hex-id>/…)"
        )
    return match.group(1), match.group(2)


def _load_bundle() -> dict:
    path = config.ONSHAPE_TOKEN_PATH
    if not path.exists():
        raise AuthError(
            "Onshape is not connected. The owner needs to run `jarvis auth onshape` "
            "once — tell them, and offer to explain the setup."
        )
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"the Onshape key bundle is unreadable ({exc}).") from exc
    missing = {"access_key", "secret_key", "sandbox"} - bundle.keys()
    if missing:
        raise AuthError(
            f"the Onshape key bundle is missing {sorted(missing)} — "
            "the owner should re-run `jarvis auth onshape --redo`."
        )
    return bundle


def auth() -> tuple[str, str]:
    """Basic-auth credentials for httpx. Goes into the auth parameter by tool
    code and is never part of a tool result."""
    bundle = _load_bundle()
    return bundle["access_key"], bundle["secret_key"]


def sandbox() -> dict:
    """The one document Jarvis may write CAD into: {did, wid, name, url}."""
    return _load_bundle()["sandbox"]


def libraries() -> list[dict]:
    """Read-only parts-library documents: [{did, name, url}, …]."""
    return _load_bundle().get("libraries", [])


# -- one-time setup (human-only, via `jarvis auth onshape`) -------------------


def _get(path: str, keys: tuple[str, str], **kwargs) -> httpx.Response:
    return httpx.get(config.ONSHAPE_API + path, auth=keys, timeout=30, **kwargs)


def _library_entries(raw: str, keys: tuple[str, str]) -> list[dict]:
    """Validate pasted library URLs against the live API; skip what fails."""
    entries: list[dict] = []
    for url in [u.strip() for u in raw.replace(",", " ").split() if u.strip()]:
        try:
            did, _ = parse_document_url(url)
        except ValueError as exc:
            print(f"  skipped: {exc}")
            continue
        response = _get(f"/documents/{did}", keys)
        if response.status_code != 200:
            print(f"  skipped {url}: HTTP {response.status_code} — is the document shared with you?")
            continue
        name = response.json().get("name", did)
        entries.append({"did": did, "name": name, "url": url})
        print(f"  library ok: {name}")
    return entries


def connect(redo: bool = False) -> int:
    """Interactive setup, or report status when already connected."""
    path = config.ONSHAPE_TOKEN_PATH

    if path.exists() and not redo:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        sb = bundle.get("sandbox", {})
        print(f"connected — key bundle at {path}")
        print(f"  sandbox: {sb.get('name', '?')}  {sb.get('url', '')}")
        for lib in bundle.get("libraries", []):
            print(f"  library: {lib.get('name', '?')}  {lib.get('url', '')}")
        print("re-run with --redo to change keys, sandbox, or libraries.")
        print("revoke access any time: cad.onshape.com → account icon → My account")
        print("→ Developer → API keys → delete the Jarvis key")
        return 0

    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    try:
        return _setup(path, old)
    except (EOFError, KeyboardInterrupt):
        print("\naborted — nothing written.")
        return 1


def _setup(path: Path, old: dict) -> int:
    from getpass import getpass

    print("Onshape setup. Create an API key in Onshape: cad.onshape.com → account")
    print("icon (top right) → My account → Developer → API keys → Create new API")
    print("key. Check the read + write document permissions. The secret is shown")
    print("only once on creation — paste both keys here.\n")

    prompt = "access key"
    if old.get("access_key"):
        prompt += " (Enter to keep the current one)"
    access = input(f"{prompt}: ").strip() or old.get("access_key", "")
    secret = (
        getpass("secret key (hidden; Enter to keep current): ").strip()
        or old.get("secret_key", "")
        if old.get("secret_key")
        else getpass("secret key (hidden): ").strip()
    )
    if not access or not secret:
        print("both keys are required.")
        return 1
    keys = (access, secret)

    response = _get("/users/sessioninfo", keys)
    if response.status_code != 200:
        print(f"Onshape rejected the keys (HTTP {response.status_code}). Check both and retry.")
        return 1
    print(f"authenticated as {response.json().get('name', '?')}\n")

    print("Jarvis writes CAD into exactly one sandbox document — nothing else.")
    raw = input(
        "sandbox document URL (Enter to create a new 'Jarvis CAD Sandbox'): "
    ).strip()
    if raw:
        try:
            did, wid = parse_document_url(raw)
        except ValueError as exc:
            print(exc)
            return 1
        response = _get(f"/documents/{did}", keys)
        if response.status_code != 200:
            print(f"cannot open that document (HTTP {response.status_code}).")
            return 1
        info = response.json()
        wid = wid or info.get("defaultWorkspace", {}).get("id")
        if not wid:
            print("could not determine a workspace id — paste the full …/w/… URL.")
            return 1
        sandbox_entry = {
            "did": did,
            "wid": wid,
            "name": info.get("name", "sandbox"),
            "url": f"https://cad.onshape.com/documents/{did}/w/{wid}",
        }
    else:
        create = httpx.post(
            config.ONSHAPE_API + "/documents",
            auth=keys,
            json={"name": "Jarvis CAD Sandbox", "isPublic": False},
            timeout=30,
        )
        if create.status_code != 200:
            # Free plans cannot create private documents.
            create = httpx.post(
                config.ONSHAPE_API + "/documents",
                auth=keys,
                json={"name": "Jarvis CAD Sandbox", "isPublic": True},
                timeout=30,
            )
            if create.status_code == 200:
                print("note: your plan only allows public documents — the sandbox is public.")
        if create.status_code != 200:
            print(f"could not create a document (HTTP {create.status_code}).")
            return 1
        info = create.json()
        did, wid = info["id"], info.get("defaultWorkspace", {}).get("id", "")
        sandbox_entry = {
            "did": did,
            "wid": wid,
            "name": info.get("name", "Jarvis CAD Sandbox"),
            "url": f"https://cad.onshape.com/documents/{did}/w/{wid}",
        }
    print(f"sandbox: {sandbox_entry['name']}  {sandbox_entry['url']}\n")

    print("Parts libraries are documents Jarvis reads parts from (e.g. the VEX V5")
    print("parts library). Paste document URLs, space- or comma-separated, or Enter")
    print("to keep/skip — you can re-run with --redo any time.")
    raw = input("library URLs: ").strip()
    libraries_entry = _library_entries(raw, keys) if raw else old.get("libraries", [])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(
        json.dumps(
            {
                "access_key": access,
                "secret_key": secret,
                "sandbox": sandbox_entry,
                "libraries": libraries_entry,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    print(f"\nconnected — key bundle written to {path} (mode 600).")
    print("Jarvis can now CAD. Try: jarvis ask \"what's in the CAD sandbox?\"")
    return 0

"""Build bounded, redacted text for the owner's approval card."""

from urllib.parse import urlsplit
from unicodedata import category

from jarvis import untrusted
from jarvis.tools import secrets
from jarvis.v2 import redact
from jarvis.v2.browser.policy import MAX_URL

from .work import DELEGATE_MODEL_LABEL, GENERAL_WORKSPACE, WorkError


CARD_TEXT_LIMIT = 1200
_TITLES = {
    "vault_add": "Add a vault entry",
    "vault_patch": "Patch a vault entry",
    "vault_archive": "Archive a vault entry",
    "calendar_create": "Create a calendar event",
    "calendar_update": "Update a calendar event",
    "calendar_delete": "Delete a calendar event",
    "work_start": "Start a coding thread",
    "delegate": "Hand off to Sol",
    "work_send": "Send to a coding thread",
    "app_open": "Open an app",
    "folder_open": "Open a folder",
    "link_open": "Open a link",
    "browser_open": "Open a browser address",
    "browser_read": "Read the browser page",
    "browser_follow": "Follow a link on the browser page",
    "browser_sign_in": "Sign in to a saved site",
    "browser_press": "Press a browser control",
    "browser_fill": "Fill a browser field",
}
_BROWSER_TAINT_NOTE = (
    "This chat has read outside text. Jarvis wants to open an address you did not supply in this turn."
)
_FULL_VAULT_ROWS = {"Body", "Old text", "New text"}
_TARGETS = {
    "vault_add": "Entry", "vault_patch": "Entry", "vault_archive": "Entry",
    "calendar_create": "Title", "calendar_update": "Event", "calendar_delete": "Event",
    "work_start": "Project", "work_send": "Thread", "delegate": "Project",
    "app_open": "App", "link_open": "URL",
}


def card_target(tool, card):
    if tool == "folder_open":
        project, folder = card["rows"]
        return f"{project['value']} · {folder['value']}"
    return next(row["value"] for row in card["rows"] if row["label"] == _TARGETS[tool])


def card_text(value, *, limit=CARD_TEXT_LIMIT):
    # Card text is display data. Scrub the whole value before taking a prefix
    # so the cut cannot leave a prefix of a credential visible.
    extra = redact.CardPass()
    hidden = extra.hide(value, command=False)
    shown = untrusted.strip_invisible(extra.show(secrets.redact_command(hidden), command=False))
    if limit is not None and len(shown) > limit:
        return shown[:limit - 1] + "…"
    return shown


def _calendar_rows(tool, preview):
    before, body = preview.get("before"), preview.get("body")
    if before is not None:
        yield "Event", before["id"]
        yield "Revision", before["etag"]
    for fields, prefix in ((before, "Current "), (body, "New " if before is not None else "")):
        if fields is None:
            continue
        for key, label in (("summary", "title"), ("start", "start"), ("end", "end"),
                           ("description", "description")):
            if key in fields:
                value = fields[key]
                if key in ("start", "end"):
                    value = value.get("dateTime") or value["date"]
                yield (prefix + label).capitalize(), value
    if tool == "calendar_create":
        if "reminders" in body:
            minutes = [str(row["minutes"]) for row in body["reminders"]["overrides"]]
            yield "Reminders", ", ".join(minutes) + " minutes before (popup)"
        else:
            yield "Reminders", "Calendar defaults"
        yield "Delivery", "No attendees, no emails sent"


def _vault_rows(tool, args, preview):
    yield "Entry", preview["ref"]
    yield "Revision", preview["revision"]
    if preview["flags"]:
        yield "Scanner flags", ", ".join(preview["flags"])
    if tool == "vault_add":
        yield "Description", args["description"]
        yield "Source", args["source"]
        yield "Body", args["body"]
    elif tool == "vault_patch":
        yield "Old text", args["old"]
        yield "New text", args["new"]
    elif preview["replacement"]:
        yield "Replacement", preview["replacement"]


def _work_rows(tool, args, preview):
    if tool == "work_start":
        yield "Project", preview["project"]
        yield "Folder", preview["path"]
        yield "Agent", f"{preview['provider']} / {preview['model']}"
        branch = preview["branch"]
        yield "Runs in", (f"A new worktree from {preview['base']} on branch {branch}"
                          if branch is not None else "The project folder")
        yield "Thread title", preview["title"]
        yield "Brief", preview["brief"]
    elif tool == "delegate":
        yield "Project", preview["project"] if preview["named"] else GENERAL_WORKSPACE
        yield "Folder", preview["path"]
        yield "Model", DELEGATE_MODEL_LABEL
        yield "Task", preview["task"]
    else:
        yield "Thread", preview["title"]
        yield "Thread id", preview["thread_id"]
        yield "In project", preview["project"]
        yield "Message", args["message"]


def _browser_action_rows(tool, args, preview):
    item = preview["item"]
    origin, destination = item["origin"], item["destination_origin"]
    other_origin = destination if destination != origin else ""
    yield "Site", origin
    if tool == "browser_fill":
        yield "Field", item["label"] or f"Item {args['number']}"
        yield "Text", args["text"]
        then = "submit the form" if args["submit"] else "the page receives this text; the form is not submitted"
        if args["submit"] and other_origin:
            then += f" to {other_origin}"
        yield "Then", then
    else:
        yield "Button", item["label"] or f"Item {item['target_number']}"
        if item["kind"] == "toggle":
            yield "Kind", "checkbox or radio"
        elif item["button_like"]:
            yield "Kind", "button"
        else:
            yield "Kind", "other control"
        if item["kind"] == "submit" and other_origin:
            yield "Sends to", other_origin
        if item["payment"]:
            yield "Reason", "This looks like a payment form."


def build_card(tool, args, preview):
    if tool.startswith("calendar_"):
        from .tools.calendar import DESCRIPTION_LIMIT

        rows = _calendar_rows(tool, preview)
        # Calendar validates proposed descriptions before building the preview.
        # Render all of the redacted text even if the credential markers add length.
        limits = {"Description": None, "New description": None, "Current description": DESCRIPTION_LIMIT}
    elif tool in ("work_start", "work_send", "delegate"):
        rows = _work_rows(tool, args, preview)
        limits = dict.fromkeys(("Brief", "Message", "Folder", "Task"))
    elif tool == "app_open":
        rows, limits = (("App", preview["app"]),), {}
    elif tool == "folder_open":
        rows = (("Project", preview["project"]), ("Folder", preview["path"]))
        limits = {"Folder": None}
    elif tool == "link_open":
        rows, limits = (("URL", preview["url"]),), {"URL": None}
    elif tool == "browser_open":
        rows = [("URL", preview["url"])]
        if preview["tainted"]:
            rows.append(("Reason", _BROWSER_TAINT_NOTE))
        limits = {"URL": None}
    elif tool == "browser_read":
        rows, limits = (), {}
    elif tool == "browser_follow" or (tool == "browser_press" and "link" in preview):
        number = preview.get("item", {}).get("target_number", args["number"])
        link = preview["link"]
        rows = [("Link number", str(number)), ("Address", link.address), ("From", link.source_origin)]
        limits = dict.fromkeys(("Address", "From"))
        if preview["saved_site"]:
            source = urlsplit(link.source_origin).hostname
            destination = urlsplit(link.destination_origin).hostname
            rows.extend([
                ("Destination", destination),
                ("Reason", f"A page on {source} links to {destination}, where Jarvis has a saved login."),
            ])
    elif tool in ("browser_press", "browser_fill"):
        rows, limits = _browser_action_rows(tool, args, preview), {"Text": None, "Field": 80, "Button": 80}
    elif tool == "browser_sign_in":
        rows, limits = (("Saved login", preview["name"]), ("Site", preview["origin"]),
                        ("Browser", preview["browser"])), {}
    else:
        rows = _vault_rows(tool, args, preview)
        limits = dict.fromkeys(_FULL_VAULT_ROWS)
    title = _TITLES[tool]
    if tool == "browser_press":
        if "link" in preview:
            title = _TITLES["browser_follow"]
        elif preview["item"]["kind"] == "submit":
            title = "Submit a browser form"
    title = card_text(title)
    if tool == "delegate" and card_text(preview["title"], limit=None) != preview["title"]:
        raise WorkError("The coding thread details cannot be shown safely.")
    shown_rows = []
    for label, value in rows:
        shown_label = card_text(label)
        if label == "Address" and (
            not 1 <= len(value) <= MAX_URL or any(category(char) in ("Cc", "Cs") for char in value)
        ):
            raise WorkError("The browser destination cannot be shown safely.")
        shown = card_text(value, limit=limits.get(label, CARD_TEXT_LIMIT))
        if tool in ("work_start", "delegate") and label not in ("Brief", "Task") and shown != value:
            raise WorkError("The coding thread details cannot be shown safely.")
        if tool == "browser_sign_in" and shown != value:
            raise WorkError("The saved login details cannot be shown safely.")
        if label in ("Address", "From") and shown != value:
            raise WorkError("The browser destination cannot be shown safely.")
        row = {"label": shown_label, "value": shown}
        if label in ("Address", "From") or (
            tool in ("browser_press", "browser_fill") and label in ("Field", "Button")
        ):
            row["source"] = "page"
        if tool in ("browser_press", "browser_fill") and shown != value:
            if label == "Text":
                raise WorkError("That text cannot be shown exactly on the approval card.")
            if label in ("Site", "Destination", "Sends to", "Then"):
                raise WorkError("The browser destination cannot be shown safely.")
        shown_rows.append(row)
    return {"title": title, "rows": shown_rows}

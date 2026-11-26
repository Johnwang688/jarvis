"""Build bounded, redacted text for the owner's approval card."""

from jarvis import untrusted
from jarvis.tools import secrets
from jarvis.v2 import redact

from .work import WorkError


CARD_TEXT_LIMIT = 1200
_TITLES = {
    "vault_add": "Add a vault entry",
    "vault_patch": "Patch a vault entry",
    "vault_archive": "Archive a vault entry",
    "calendar_create": "Create a calendar event",
    "calendar_update": "Update a calendar event",
    "calendar_delete": "Delete a calendar event",
    "work_start": "Start a coding thread",
    "work_send": "Send to a coding thread",
    "app_open": "Open an app",
    "folder_open": "Open a folder",
    "link_open": "Open a link",
}
_FULL_VAULT_ROWS = {"Body", "Old text", "New text"}
_TARGETS = {
    "vault_add": "Entry", "vault_patch": "Entry", "vault_archive": "Entry",
    "calendar_create": "Title", "calendar_update": "Event", "calendar_delete": "Event",
    "work_start": "Project", "work_send": "Thread",
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
    else:
        yield "Thread", preview["title"]
        yield "Thread id", preview["thread_id"]
        yield "In project", preview["project"]
        yield "Message", args["message"]


def build_card(tool, args, preview):
    if tool.startswith("calendar_"):
        from .tools.calendar import DESCRIPTION_LIMIT

        rows = _calendar_rows(tool, preview)
        # Calendar validates proposed descriptions before building the preview.
        # Render all of the redacted text even if the credential markers add length.
        limits = {"Description": None, "New description": None, "Current description": DESCRIPTION_LIMIT}
    elif tool in ("work_start", "work_send"):
        rows = _work_rows(tool, args, preview)
        limits = dict.fromkeys(("Brief", "Message", "Folder"))
    elif tool == "app_open":
        rows, limits = (("App", preview["app"]),), {}
    elif tool == "folder_open":
        rows = (("Project", preview["project"]), ("Folder", preview["path"]))
        limits = {"Folder": None}
    elif tool == "link_open":
        rows, limits = (("URL", preview["url"]),), {"URL": None}
    else:
        rows = _vault_rows(tool, args, preview)
        limits = dict.fromkeys(_FULL_VAULT_ROWS)
    title = card_text(_TITLES[tool])
    shown_rows = []
    for label, value in rows:
        shown_label = card_text(label)
        shown = card_text(value, limit=limits.get(label, CARD_TEXT_LIMIT))
        if tool == "work_start" and label != "Brief" and shown != value:
            raise WorkError("The coding thread details cannot be shown safely.")
        shown_rows.append({"label": shown_label, "value": shown})
    return {"title": title, "rows": shown_rows}

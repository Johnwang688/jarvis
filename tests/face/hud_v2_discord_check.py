"""Headless checks for B1: a project's Discord channel in the HUD.

Called from `hud_v2_check.main()` before the part-B checks (which archive and
delete projects). Driven against the mock (`hud_v2_mock_projects.py` answers
`GET|POST /projects/{id}/discord` and `POST /discord/backfill`).

What each check guards:
  - before `jarvis auth discord-guild` nothing can act: the dialog's buttons
    are off and say which command turns them on, and the New Project box is
    off with the same pointer;
  - once set up, the New Project box is **on by default** (decisions O1) and
    creating a project also creates its channel;
  - the pill shows the state and who made the link, an Unlink says the
    channel is **kept**, and a refused link is shown inline in the backend's
    words;
  - the Inbox gets no buttons — only setup changes #ungrouped;
  - the Discord panel offers "Create channels for N projects" once, and the
    archive confirmation says where the channel goes.
"""
from __future__ import annotations

import time


def _close(page, until):
    for _ in range(3):
        if page.locator('[data-testid="picker"], [data-testid="archive-confirm"]').count() == 0:
            return
        page.keyboard.press("Escape")
        time.sleep(0.15)


def _edit(page, until, pid):
    page.locator(f'[data-testid="project-menu-{pid}"]').click()
    until(lambda: page.locator('[data-testid="pmenu-edit"]').count() > 0)
    page.locator('[data-testid="pmenu-edit"]').click()
    until(lambda: page.locator('[data-testid="discord-pill"]').count() > 0)
    until(lambda: page.locator('[data-testid="discord-pill"]').get_attribute("data-state"))


def _set_guild(mock, configured: bool):
    mock.world["discord"]["guild"] = {"configured": configured,
                                      "id": "800000000000000001" if configured else None}
    mock.emit("discord_status", {"state": "ok"})


def discord_link_checks(page, mock, check, until, expand):
    w = mock.world
    print("\ndiscord: project channels (B1)")
    _close(page, until)
    pill = page.locator('[data-testid="discord-pill"]')

    # The New Project box, from the earlier newproject section: on by default
    # because the mock's server is set up, so the new project got a channel.
    made = [p for p in w["projects"] if p["name"] == "trading firm"]
    check("a project created with the box on also asked for its channel",
          bool(made) and {"action": "create"} in mock.posted(f"/projects/{made[0]['id']}/discord"),
          str(made[0]["id"] if made else None))

    # -- before setup ---------------------------------------------------------
    _set_guild(mock, False)
    panel = page.locator('[data-testid="discord"]')
    until(lambda: page.evaluate("!!window.__hud.state().discord && "
                                "!window.__hud.state().discord.guild.configured"))
    check("no backfill button before the server is set up",
          page.locator('[data-testid="discord-backfill"]').count() == 0)
    page.locator('[data-testid="new-project"]').click()
    page.wait_for_selector('[data-testid="project-discord-create"]')
    box = page.locator('[data-testid="project-discord-create"]')
    check("the New Project box is off and disabled before setup",
          box.is_disabled() and not box.is_checked())
    check("and names the setup command",
          "jarvis auth discord-guild" in page.locator('[data-testid="project-discord-setup"]').inner_text())
    _close(page, until)
    _edit(page, until, "p2")
    check("the pill says the server is not set up",
          pill.get_attribute("data-state") == "unconfigured"
          and "not set up" in pill.inner_text(), pill.inner_text())
    check("Create and Link are disabled",
          page.locator('[data-testid="discord-create"]').is_disabled()
          and page.locator('[data-testid="discord-link-open"]').is_disabled())
    check("and the dialog shows `jarvis auth discord-guild`",
          "jarvis auth discord-guild" in page.locator('[data-testid="discord-setup"]').inner_text())
    _close(page, until)

    # -- set up -----------------------------------------------------------------
    _set_guild(mock, True)
    until(lambda: page.evaluate("!!window.__hud.state().discord && "
                                "window.__hud.state().discord.guild.configured"))
    page.locator('[data-testid="new-project"]').click()
    page.wait_for_selector('[data-testid="project-discord-create"]')
    box = page.locator('[data-testid="project-discord-create"]')
    check("once set up, the New Project box is on by default",
          box.is_checked() and not box.is_disabled())
    _close(page, until)

    # Link a pasted id: a refusal inline, then the link, then unlink.
    _edit(page, until, "p2")
    check("an unlinked project's pill says so",
          pill.get_attribute("data-state") == "unlinked" and "no channel" in pill.inner_text())
    page.locator('[data-testid="discord-link-open"]').click()
    page.locator('[data-testid="discord-link-id"]').fill("not-an-id")
    page.locator('[data-testid="discord-link-submit"]').click()
    until(lambda: page.locator('[data-testid="discord-error"]').count() > 0)
    check("a refused link is shown inline, in the backend's words",
          "digits" in page.locator('[data-testid="discord-error"]').inner_text()
          and page.locator('[data-testid="project-dialog-edit"]').count() == 1)
    page.locator('[data-testid="discord-link-id"]').fill("830000000000000777")
    page.locator('[data-testid="discord-link-submit"]').click()
    until(lambda: pill.get_attribute("data-state") == "linked_ok")
    check("a pasted id links the channel",
          {"action": "link", "channel_id": "830000000000000777"} in mock.posted("/projects/p2/discord"))
    check("the pill says linked, and by whom",
          "linked by you" in page.locator('[data-testid="discord-origin"]').inner_text())
    unlink = page.locator('[data-testid="discord-unlink"]')
    check("Unlink says the channel is kept", "the channel is kept" in unlink.inner_text(),
          unlink.inner_text())
    unlink.click()
    until(lambda: pill.get_attribute("data-state") == "unlinked")
    check("Unlink unlinks", {"action": "unlink"} in mock.posted("/projects/p2/discord"))
    page.locator('[data-testid="discord-create"]').click()
    until(lambda: pill.get_attribute("data-state") == "linked_ok")
    check("Create makes one, created by Jarvis",
          "created by Jarvis" in page.locator('[data-testid="discord-origin"]').inner_text())
    _close(page, until)

    # Other states draw as words, and a pending rename is said out loud.
    w.setdefault("channel_views", {})["p2"] = {
        "state": "missing_permissions", "missing": ["Attach Files"], "rename_pending": True}
    _edit(page, until, "p2")
    until(lambda: pill.get_attribute("data-state") == "missing_permissions")
    check("a missing permission is named on the pill", "Attach Files" in pill.inner_text(),
          pill.inner_text())
    check("and a pending rename is shown",
          page.locator('[data-testid="discord-rename-pending"]').count() == 1)
    w["channel_views"]["p2"] = {"name": '<img src=x onerror="window.__pwnedPill=1">'}
    page.locator('[data-testid="discord-recheck"]').click()
    until(lambda: "<img" in pill.inner_text())
    check("a channel name renders as text, never markup",
          pill.locator("img").count() == 0 and not page.evaluate("window.__pwnedPill === 1"))
    w["channel_views"].pop("p2")
    _close(page, until)

    # The Inbox: #ungrouped, and no buttons.
    _edit(page, until, "p1")
    check("the Inbox gets no channel buttons",
          page.locator('[data-testid="discord-inbox-note"]').count() == 1
          and page.locator('[data-testid="discord-create"]').count() == 0
          and page.locator('[data-testid="discord-unlink"]').count() == 0)
    _close(page, until)

    # -- the one-time backfill ------------------------------------------------------
    for p in w["projects"]:
        if p["id"] == "p2":
            p["discord_channel_id"], p["discord_channel_origin"] = None, None
    mock.emit("project_updated", {"project_id": "p2", "changed": ["discord_channel_id"]},
              project_id="p2")
    button = page.locator('[data-testid="discord-backfill"]')
    until(lambda: button.count() > 0)
    targets = [p for p in w["projects"] if not p.get("inbox") and not p.get("archived")
               and not p.get("discord_channel_id")]
    check("the panel offers the backfill, counted",
          button.count() == 1 and f"Create channels for {len(targets)} project" in button.inner_text(),
          button.inner_text() if button.count() else "")
    button.click()
    until(lambda: mock.posted("/discord/backfill"))
    until(lambda: page.locator('[data-testid="discord-backfill-result"]').count() > 0)
    check("it calls the backfill route", bool(mock.posted("/discord/backfill")))
    check("and says what it made",
          "Created" in page.locator('[data-testid="discord-backfill-result"]').inner_text())
    until(lambda: page.locator('[data-testid="discord-backfill"]').count() == 0)
    check("then the button is gone: every project has a channel",
          page.locator('[data-testid="discord-backfill"]').count() == 0)

    # -- archiving says where the channel goes --------------------------------------
    page.locator('[data-testid="project-menu-p2"]').click()
    until(lambda: page.locator('[data-testid="pmenu-archive"]').count() > 0)
    page.locator('[data-testid="pmenu-archive"]').click()
    until(lambda: page.locator('[data-testid="archive-summary"]').count() > 0)
    summary = page.locator('[data-testid="archive-summary"]').inner_text()
    check("the archive confirmation says the channel moves to Jarvis Archive, kept",
          "Discord channel moves to the Jarvis Archive" in summary and "kept" in summary, summary)
    _close(page, until)

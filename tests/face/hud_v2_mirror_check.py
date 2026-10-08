"""Headless checks for PR C: a chat's place on Discord, shown in the HUD.

Called from `hud_v2_check.main()` right after the chat section, against the
mock. What each check guards:
  - a chat that is not on Discord yet has no header; once the mirror gives it
    a thread (`thread_updated` with `changed: ["surface"]`), the header reads
    "On Discord: #school › <name>" and links to that thread on discord.com;
  - a message the owner typed in Discord arrives over SSE (`user_message`,
    `via: "discord"`) and shows as theirs, labelled "via Discord";
  - a HUD message's own `user_message` is never drawn a second time;
  - the label survives a reload, from the transcript's `via`.
"""
from __future__ import annotations

URL = "https://discord.com/channels/100000000000000003/800000000000000001"


def mirror_checks(page, mock, check, until, base_reconnect):
    print("\nchats on Discord (PR C)")
    page.locator('[data-testid="tab-chat"]').click()
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: page.locator('[data-testid="log"] .msg').count() >= 2)
    check("a chat not yet on Discord has no Discord header",
          page.locator('[data-testid="chat-discord"]').count() == 0)

    thread = next(t for t in mock.world["threads"] if t["id"] == "t1")
    thread["surface"] = "discord:800000000000000001"
    thread["discord"] = {"kind": "thread", "channel": "school", "name": "desk chat", "url": URL}
    mock.emit("thread_updated", {"thread_id": "t1", "surface": thread["surface"],
                                 "changed": ["surface"]}, thread_id="t1", project_id="p1")
    until(lambda: page.locator('[data-testid="chat-discord"]').count() > 0)
    header = page.locator('[data-testid="chat-discord"]')
    check("its header names the channel and the thread",
          header.count() == 1 and header.inner_text().strip() == "On Discord: #school › desk chat",
          header.inner_text() if header.count() else "")
    link = header.locator("a")
    check("and links to the thread on discord.com, in a new tab",
          link.count() == 1 and link.get_attribute("href") == URL
          and link.get_attribute("target") == "_blank",
          link.get_attribute("href") if link.count() else "")

    before = page.locator('[data-testid="msg-user"]').count()
    mock.emit("user_message", {"text": "sent from my phone", "typed": "sent from my phone",
                               "via": "discord", "images": 0, "attachments": []},
              thread_id="t1", project_id="p1", turn_id="turn-d1")
    until(lambda: page.locator('[data-testid="msg-user"]').count() > before)
    last = page.locator('[data-testid="msg-user"]').last
    check("a message typed in Discord shows as the owner's",
          "sent from my phone" in last.inner_text(), last.inner_text())
    check("labelled via Discord", last.locator('[data-testid="via-discord"]').count() == 1)

    count = page.locator('[data-testid="msg-user"]').count()
    mock.emit("user_message", {"text": "typed here", "typed": "typed here", "via": "hud"},
              thread_id="t1", project_id="p1", turn_id="turn-h1")
    mock.emit("user_message", {"text": "another chat", "typed": "another chat",
                               "via": "discord"}, thread_id="t-other", project_id="p1")
    import time
    time.sleep(0.4)
    check("a HUD message, and another chat's message, are never drawn here",
          page.locator('[data-testid="msg-user"]').count() == count)

    mock.world["transcripts"]["t1"].append(
        {"role": "user", "text": "from the bus stop", "at": "2026-10-07T00:00:00+00:00",
         "via": "discord"})
    connections = mock.sse_connections()
    page.reload()
    base_reconnect(connections)
    page.wait_for_selector('[data-testid="sidebar"]')
    until(lambda: page.locator('[data-testid="thread-t1"]').count() > 0)
    page.locator('[data-testid="thread-t1"]').click()
    until(lambda: any("from the bus stop" in m.inner_text()
                      for m in page.locator('[data-testid="msg-user"]').all()))
    labelled = [m for m in page.locator('[data-testid="msg-user"]').all()
                if "from the bus stop" in m.inner_text()]
    check("the label comes back from the transcript after a reload",
          bool(labelled) and labelled[0].locator('[data-testid="via-discord"]').count() == 1)
    first = page.locator('[data-testid="msg-user"]').first
    check("and a HUD message carries none",
          first.locator('[data-testid="via-discord"]').count() == 0)

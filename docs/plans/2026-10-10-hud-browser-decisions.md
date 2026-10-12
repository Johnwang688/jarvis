# Decisions: the built-in Browser (2026-10-11)

These are the owner's answers to `2026-10-10-hud-browser-plan.md` §12. **Where this file and the plan disagree, this file
wins.** Several answers change the plan, and the plan amendment (§ "Plan changes" below) is written before the affected
packages are built.

## Answered by the owner

| # | Decision | Answer |
|---|---|---|
| B-1 | Engine | **Yes**: daemon-owned Chrome streamed into the pane; the iframe stays as Direct mode. |
| B-2 | Local dev URLs | **Direct**, as recommended. **Plus:** when a dev server starts in a HUD terminal, **automatically open it in a new Browser pane**. |
| B-3 | Which Chrome | **Google Chrome stable via apt.** The owner installs it; the lead gives the command. |
| B-4 | Logins and accounts | **Named profiles plus Private.** Each Browser pane picks a profile; panes on the same profile share logins; different profiles hold different accounts at once; a Private pane forgets everything on close. **Chrome-level sign-in, sync and autofill are on** (owner, 2026-10-11: "chrome sync should be on. keep the sign ins. it's ok for the agents to click and type in stuff with autofill since I'm watching them do it in the HUD"). An agent still never types a password, code or card number itself, and submitting a login or payment still asks (amendment 1 §C). Google may refuse sign-in from an automated Chrome (M4, M4b). |
| B-5 | LAN addresses | **Off by default, with a Settings switch.** |
| B-6 | Downloads | **Private folder plus "Save a copy"**, as recommended. |
| B-7 | What Jarvis can see | **Every HUD Browser pane, by default.** The owner can point him at a specific one ("look at pane 3"). The owner's own Windows Chrome stays private. The per-pane switch remains as a way to turn a pane off, but it defaults **on** for every site. |
| B-8 | Tool name | `hud_browser_view`. |
| B-9 | May agents drive the browser? | **Yes, agents may click and type**, behind a classifier that **denies logins and anything dangerous the owner did not approve**. Annotate and Send to chat work as planned. |
| B-10 | What Send to chat attaches | As recommended: the picture, URL, title and the text on screen. Whole-page text is an unticked option. |
| B-11 | Terminal links | Any http(s) link offers "Open in Browser" first. |
| B-12 | Sound | **Off by default until the owner turns it on.** There is a switch, so sound must be possible; M3 decides how. |
| B-13 | Tabs | As recommended: tabs per pane, pop-ups as tabs, 12 in all. |
| B-14 | Rename | **As soon as possible.** It no longer waits for G2. It lands right after WP-G1 merges, so it doesn't collide with G1's `workspace.ts` rewrite. |
| B-16 | Fonts | **Yes**, the Windows fonts, if M10's cost is acceptable. |
| B-15 | The v1 control-port gap (BR-0) | **Build it as a small PR** (owner, 2026-10-11). |
| — | Merging | Not answered separately. The lead treats Browser PRs as merge-on-review, like the HUD and editor work the owner already covered, and will stop if the owner says otherwise. |

## Lead's reading of B-9 (agents drive the browser)

This governs the plan amendment.
- **Hard refusals, not left to the classifier.** An agent never types into a password, one-time-code or payment-card field. It never navigates to Jarvis's own ports (the gate refuses them anyway). It never acts while an approval card is up.
- **The classifier** judges each agent action (navigate, click, type, submit, upload, download) against the page's URL and a fenced, untrusted description of the target.
  - Logging in or signing up, OAuth or consent screens, two-factor prompts, purchases, payments, sending or posting anything, deleting things, and changing account or security settings → an **approval card** naming the action. Deny or no answer means no.
  - Everything else (reading, scrolling, following links, searching, filling ordinary fields) runs.
  - Every failure path asks. It is a safety net, not a boundary (CLAUDE.md's `command_review` lesson).
- **The owner is always in charge.** A "Jarvis is driving" banner shows in the pane. Any input from the owner takes over at once and pauses the agent. Each agent action is listed in the pane.
- **Which turns may drive:** desk turns only, as with `terminal_read` and `hud_browser_view`, unless the owner says otherwise.

## Plan changes these answers require

1. **Profiles** (B-4): several persistent profiles, each its own user-data-dir and Chrome instance, plus non-persistent Private contexts. Profile pick, create and delete in the pane. "Sign out of everything" works per profile.
2. **Auto-open** (B-2): detect a dev server's URL announced in a HUD terminal. This happens in daemon memory, never written anywhere. Open it in a new Browser pane through G2's split. It needs G2.
3. **Seeing** (B-7): "Jarvis can see" defaults on for every pane, and the tool takes a pane number.
4. **Driving** (B-9): new driving tools behind the classifier, plus the approval-card path, the banner and takeover. This replaces the plan's "no driving" (§6 iii).
5. **Sound** (B-12): a switch, off by default.
6. **Rename** (B-14): BR-3a moves up to "right after G1".

## Amendment 1 (2026-10-11)

The plan changes above are worked out in `2026-10-11-hud-browser-plan-amendment-1.md` (driving, profiles, auto-open, seeing, sound, and the re-cut work packages). Its §J decisions BD-1 to BD-4, BD-6, BD-7, BD-9 and BD-11 are adopted as recommended. BD-5 (Chrome sign-in and sync) is answered: on. BD-8 waits for M3, and BD-10 for BR-1a's review.

**B-3 done (2026-10-11):** the owner installed Google Chrome stable through apt (`google-chrome --version` reports 155.0.8059.39).

## Amendment 2 (2026-10-12): dev servers from any source

**The owner, 2026-10-12:** "Can you have Jarvis/claude/codex auto open localhost servers when they are started? If there is 8 panels all taken already then have them open a new tab in a browser panel. If no browser panels are open then have it ask which panel to open in or if I don't want to open it"

The lead's decisions, worked out in `2026-10-12-hud-browser-plan-amendment-2.md` (its §0 has them in full):

- **L1, sources.**
  - The owner's HUD terminals, through BR-8a's banners.
  - Servers an agent starts (a chat or a task, on any provider), found as new loopback listeners. Each must be:
    - owned by the owner's uid;
    - working in that thread's folder (dropped at review: R-2 replaces it with the session's tag);
    - started during its turn or a grace period after it;
    - not a HUD terminal's, and not Jarvis's own;
    - answering HTTP on `/`.
  - Servers the owner starts outside Jarvis are not included.
- **L2, Direct only.** An auto-opened server uses WP-E's sandboxed frame and never gets keep-origin. **Auto-open never opens a port in the Remote browser's gate**, which reverses the note that BR-8b's auto-open counts as an owner-route navigation.
- **L3, placement.**
  1. A new pane beside the source, with `{focus:false}`.
  2. Else a new tab in a drawn Browser pane.
  3. Else a non-blocking prompt: "Open in pane [1]…[8] · Don't open". It expires when the server stops.
- **L4, tabs.** Browser panes get tabs in Direct mode, migrated from `browserUrl`, and BR-3b reuses the strip.
- **L5, one window.** The window showing the source acts. Otherwise the most recently focused window claims it through an owner-only route. With no window open, a quiet notice appears in the next window to load.
- **L6, the gentle rules.** Nothing happens under a card; it waits until the card goes. Focus and the mic never move. One entry per port; a rate limit; the `auto_open` switch covers every source.
- **L7, what leaves memory.** Only a loopback URL and the source's id. Never terminal text or tool output. R-5 narrows it further: no port on the bus.
- **L8, stated limits.** An agent can put a sandboxed page in front of the owner, and some servers are missed.

**Superseded:** amendment 1's §D.2 placement ("No room: the focused pane switches to Browser", the offer chip in the terminal's bar), BD-7, and the BR-8b rows of its §G and §H. AO-3 replaces BR-8b. BR-2's 30-second wait under a card becomes "until the card goes" (amendment 2, AD-14).

**Decided at the plan's review (the lead, 2026-10-12; amendment 2 §0.2):**
- **R-1, no frame takes the keyboard.** The reviewer reproduced it in Chrome 155: a framed page can take keyboard focus with no user activation. A Browser frame that gains focus without an owner gesture is unmounted at once ("This page took the keyboard · Reload").
  - An owner gesture is: the pointer over that frame; a Tab into it; or the owner's own load of that tab.
  - A hidden or background frame gaining focus is always theft. So is any frame under a card.
  - This covers **every** Browser frame, including the Direct pane on main today, and ships first in AO-2.
- **R-2, a tag is required.** An agent-started listener must carry its session's origin tag and be in the daemon's cgroup. An untagged one is not attributed, so L1's folder rule is dropped. The cost: a child started with `env -i` is missed.
- **R-3, blank kept panes.** Rule 1 may take a blank kept pane: a default spec, holding nothing. A kept pane holding anything is never taken.
- **R-4, the owner's two questions, decided by the lead.**
  - **Q-A1:** a server whose `/` is not a web page (JSON, an error page with no markup) does not open.
  - **Q-A2:** in a Browser pane the owner isn't using, a new tab comes to the front.

  The owner can overturn either.
- **R-5, no port on the bus.** A bus record carries `id`, `state`, `source` and time only. The URL comes only from owner-only routes.
- **R-6, the prompt.** It sits away from every input bar and ignores clicks for about 500 ms after it appears. A split waits while a pointer button is down.
- **R-7, the smaller items.**
  - `environ` is read only for the owning pid, after the cheap rules, as raw bytes never decoded, logged or raised with.
  - The probe needs markup in the body for a non-2xx/3xx answer.
  - Service ports are skipped (9222, 9229, 5678, 5432, 3306, 6379, 27017), and so are 127/8 binds other than 127.0.0.1.
  - The HUD's `opened` set is keyed by entry id.
  - The claim lease is renewed while a prompt or wait is live, and lapses when the window's SSE ends.
  - Each auto-opened tab shows a visible "Opened for … · localhost:N".
  - Tests count work rather than wall time, check an accept count of 0 for refused listeners, and cap live frames window-wide.
- **R-8, polling and packaging.**
  - Polling runs only during Claude or Codex turns, with a 600 s grace for slow binders.
  - AO-1b is split so the watcher doesn't wait on #70.
  - The owner is told that "Open in Browser" now opens a new tab.

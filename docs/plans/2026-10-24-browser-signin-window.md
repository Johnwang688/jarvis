# B-LOGIN-W: a Chrome window for the Chrome sign-in step (2026-10-24)

Owner request, 2026-10-24: "Plan a headed Chrome window for the Chrome sign-in
step (B-LOGIN), then build it." Lead: Claude. This un-queues the managed-account
part of B-LOGIN; the rest of the Browser feature freeze is unchanged.

## Why

After #299 Google accepts the sign-in, but the owner's school account
(a Workspace-managed account) then shows Chrome's
`ManagedProfileRequiredInterstitial`: "Your organization requires you to sign
into Chrome". Its Continue opens Chrome's own profile dialog, which is browser
UI. The Remote browser is headless and streams page frames only, so that
dialog is never drawn and cannot be pressed (browser.md already listed this as
unverified). Measured 2026-10-24: a headed Chrome started from a systemd user
service draws a window on the Windows desktop through WSLg, on Wayland
(absolute `WAYLAND_DISPLAY=/mnt/wslg/runtime-dir/wayland-0`) and on X11, in
under a second, with `navigator.webdriver` false and no `Headless` user agent.

## Decisions

1. **Owner-only route, one profile at a time.**
   `POST /browser/profiles/<id>/signin-window {}` opens the window;
   `DELETE /browser/profiles/<id>/signin-window` closes it. Same guards as the
   existing `signin` route: the HUD listener and Origin, an approval card
   waiting refuses (409, nothing reaches a page), a named profile only,
   never Private. No tool, agent, workflow or Discord path calls it.
2. **The profile leaves the Remote browser while the window is open.** Its
   headless Chrome is closed first (its tabs end, as when the profile is
   closed); the same profile folder is then launched headed. While the window
   is open, every Remote open of that profile answers 409 "This profile is
   open in a Chrome window for sign-in; close that window first". The window
   counts toward the three-profile cap.
3. **Nothing in the window is a Jarvis tab.** Its pages are never adopted: no
   stream, no ticket, no look, no capture, no agent driving, no Jarvis
   navigation. The owner types into Chrome directly; Jarvis never relays,
   sees or stores that input.
4. **The same launch, minus `--headless`.** Same curated flags (#299
   included), so the window keeps the SOCKS gate, `MAP * ~NOTFOUND`, no QUIC,
   WebRTC held to the proxy, extensions off, permission prompts denied, audio
   muted, Chrome's sandbox. Added: `--ozone-platform=wayland`. Its start page
   is `chrome://settings/syncSetup`. Downloads are refused
   (`accept_downloads=False`). The password store is chosen and observed
   exactly as for a headless launch (keyring when unlocked, sticky marker);
   a mismatch closes it and refuses.
5. **Wayland, not X11.** The owner types a Google password into this window.
   Any X11 client on the display can read X11 keystrokes; Wayland clients
   cannot read another client's input. The environment gains only
   `WAYLAND_DISPLAY` (the absolute WSLg socket path, so it works from the
   service's `/run/user` view, which lacks WSLg's link) and a private 0700
   `XDG_RUNTIME_DIR` inside the Browser folder. No `DISPLAY`. The socket must
   be a socket owned by us; otherwise 409 "No desktop display (WSLg) is
   available".
6. **The window ends** when the owner closes it (Chrome's context `close`),
   presses "Close Chrome window" in the HUD, the profile is forgotten or
   deleted, the daemon stops, or after **20 minutes** (a forgotten window
   would hold the profile). Ending stops its processes; the next Remote open
   relaunches the profile headless with the new sign-in.
7. **Focus.** The window takes the desktop's focus only because the owner
   pressed the button (the "no focus without the owner's gesture" rule).

## The gate check (new, every named-profile launch)

A managed account brings its organisation's Chrome policy. Chrome ranks
policy above command-line switches, so a `ProxySettings` (or PAC) policy
could take Chrome around the SOCKS gate. `MAP * ~NOTFOUND` would still stop
name lookups, but an IP literal (a LAN address) would go direct: the LAN
would open. So **after every named-profile launch, headless or headed**,
before any page is adopted, the daemon opens one page at
`http://gate-check-<random>.jarvis.invalid/` and waits up to 5 s for the gate
to see that exact name (the gate refuses it; nothing leaves). Not seen: the
Chrome is closed, its processes stopped, and the launch refused with 409
"This profile's Chrome settings bypass Jarvis's network gate (an
organization's proxy policy?); it was closed". The gate records only that a
pending check name arrived, never other hosts. Private has no account and no
policy and is not checked.

## HUD

Profile **Manage** panel: a "Sign in in a Chrome window" button beside "Sign
in to Chrome". While a window is open (`/browser` → `signin_windows`, read on
the panel's existing 3 s refresh): the line "Open in a Chrome window on your
desktop. Finish signing in there, then close it." and a "Close Chrome window"
button. Errors show as the panel's other 409s do.

## Tests

- Host with fakes: the route guards (held, Private, unknown profile, no
  display), headless instance closed first, Remote open refused while open,
  cap counting, close/forget/delete/stop/timeout ending it, pages never
  adopted, downloads off, launch arguments (no `--headless`, Wayland flag,
  env has only the absolute socket and the private runtime dir).
- Gate check with fakes: seen passes, unseen closes and refuses, only the
  pending name is recorded.
- Real Chrome (isolated, temp profile, mock gate, no network): a headless
  named launch passes the gate check; a launch whose proxy is pointed
  elsewhere fails it. A headed launch on a temporary profile opens and closes
  (skipped without WSLg).
- HUD unit tests for the panel states.

## Not verified by tests

The real school sign-in, the managed-profile dialog itself, and what the
school's policy sets. The owner does that once it is installed.

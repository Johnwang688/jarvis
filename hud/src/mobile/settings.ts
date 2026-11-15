// RD-2: display live state; actions remain in their existing server-backed flows.
import { el, tap } from "./views";
import { brandButton } from "./orb";
import { CODE_SVG, lockIcon, unlockIcon } from "./icons";

// Settings-only members of the same 1.75-stroke outline family as icons.ts.
const GLYPHS: Record<string, string[]> = {
  "m-menu-lastread": ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z", "M12 6v6l4 2"],
  "m-settings-security": ["M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Z", "m8 12 3 3 5-6"],
  "m-settings-quiet": ["M21 13a9 9 0 0 1-10-10 9 9 0 1 0 10 10Z"],
  "m-settings-session": ["M6 2h12v20H6V2Z", "M10 18h4"],
  notifications: ["M5 16h14l-2-3V8a5 5 0 0 0-10 0v5l-2 3Z", "M10 20h4"],
  signout: ["M9 3H3v18h6", "M8 12h13m-5-5 5 5-5 5"],
};
function glyph(id: string): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  CODE_SVG.add(svg);
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: "m-icon" })) svg.setAttribute(key, value);
  for (const d of GLYPHS[id]) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export interface SettingsState {
  connection: string;
  connectionState: string;
  tone: string;
  lastRead: string;
  security: string;
  limited: boolean;
  unlock: string;
  unlocked: boolean;
  lockNote: string;
  quiet: string;
}

function row(title: string, id: string, detail = ""): HTMLElement {
  const line = el("div", "m-settings-row");
  const text = el("div", "m-settings-text");
  text.append(el("span", "m-settings-label", title));
  const value = el("span", "m-settings-detail", detail);
  value.dataset.testid = id;
  text.append(value);
  line.append(text);
  if (GLYPHS[id]) line.prepend(glyph(id));
  if (id === "m-menu-lastread") line.classList.add("m-settings-inline");
  return line;
}

export function settingsView(state: SettingsState, notifications: HTMLElement,
  actions: { lock: () => void; signOut: () => void }): HTMLElement {
  const view = el("div", "view m-settings");
  view.dataset.testid = "m-settings";
  const heading = el("h1", "m-settings-title", "Settings");
  heading.tabIndex = -1;
  view.append(heading);
  const section = (title: string, ...rows: HTMLElement[]) => {
    const group = el("section", "m-settings-group");
    group.setAttribute("aria-label", title);
    const list = el("div", "m-settings-list");
    list.append(...rows);
    group.append(el("h2", "m-settings-heading", title), list);
    view.append(group);
  };
  section("Connection", row("Jarvis", "m-settings-connection"), row("Last read", "m-menu-lastread"),
    row("This connection", "m-settings-security"));
  const orb = brandButton().querySelector<HTMLElement>(".m-orb")!;
  orb.classList.add("m-settings-orb");
  view.querySelector(".m-settings-row")!.prepend(orb);
  const unlock = row("Unlock window", "m-settings-unlock");
  unlock.querySelector(".m-settings-label")!.remove();
  unlock.querySelector('[data-testid="m-settings-unlock"]')!.className = "m-settings-label";
  unlock.querySelector(".m-settings-text")!.append(el("span", "m-settings-detail",
    "Face ID unlocks Authorize, Always and Done for an hour."));
  unlock.append(tap("Lock now", actions.lock, "quiet", "m-lock-now"));
  const note = el("p", "m-notice m-settings-lock-note");
  note.dataset.testid = "m-settings-lock-note";
  note.setAttribute("role", "status");
  section("Unlock", unlock, note);
  const quiet = row("Quiet hours", "m-settings-quiet");
  const notify = el("div", "m-settings-notify");
  notify.append(glyph("notifications"), notifications);
  section("Notifications", notify, quiet);
  section("Session", row("Signed in", "m-settings-session", "This phone, with its passkey"));
  const signOut = tap("Sign out", actions.signOut, "m-settings-signout", "m-signout");
  signOut.prepend(glyph("signout"));
  view.querySelector('[aria-label="Session"] .m-settings-list')!.append(signOut);
  view.append(el("p", "m-settings-footnote", "Signing out ends this phone's session. Signing in again needs the passkey."));
  updateSettings(view, state);
  return view;
}

/** Change words in place: a stream event or clock tick must not remove a focused Settings control. */
export function updateSettings(view: HTMLElement, state: SettingsState): void {
  const set = (id: string, value: string) => {
    const node = view.querySelector<HTMLElement>(`[data-testid="${id}"]`);
    if (node && node.textContent !== value) node.textContent = value;
    return node;
  };
  const connection = set("m-settings-connection", state.connection);
  if (connection) connection.dataset.tone = state.tone;
  const orb = view.querySelector<HTMLElement>(".m-settings-orb");
  if (orb) orb.dataset.state = state.connectionState;
  set("m-menu-lastread", state.lastRead);
  const security = set("m-settings-security", state.security);
  if (security) security.dataset.tone = state.limited ? "err" : "ok";
  const unlock = set("m-settings-unlock", state.unlock);
  if (unlock) unlock.dataset.tone = state.unlocked ? "ok" : "muted";
  const row = unlock?.closest(".m-settings-row");
  if (row && row.getAttribute("data-unlocked") !== String(state.unlocked)) {
    row.setAttribute("data-unlocked", String(state.unlocked));
    row.querySelector(".m-icon")?.remove();
    row.prepend(state.unlocked ? unlockIcon() : lockIcon());
  }
  const note = set("m-settings-lock-note", state.lockNote);
  if (note) note.hidden = !state.lockNote;
  set("m-settings-quiet", state.quiet);
}

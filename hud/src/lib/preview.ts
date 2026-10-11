// What the Preview tab is allowed to iframe, and how.
//
// The HUD is the approval surface. An iframe that could reach `/approvals`
// would let an agent approve itself, which is the frontend half of v1's
// `is_face_origin()` rule — so the HUD's own origin is refused here whatever
// spelling it arrives in, and the backend refuses to proxy it as well. So is
// the daemon's API listener (WP-E): it serves the same HUD and the same API,
// and framing it is at best pointless.
//
// Chrome resolves `foo.localhost` to 127.0.0.1, so the loopback test is by
// hostname family rather than by string equality, exactly as v1's
// `is_face_origin` matches all of loopback including `*.localhost`.
//
// **Keeping its own origin (decisions W-4, WP-E).** A framed page normally
// runs with an opaque origin (`sandbox` without `allow-same-origin`), so a dev
// app's `localStorage`, cookies and IndexedDB throw. A Preview pane may let a
// local dev server's page keep its own origin — off by default, per pane, and
// only:
//   - for a loopback port that is none of the daemon's three (HUD, workshop,
//     API — as `/status` reports them, and the defaults, which are the
//     owner's live daemon) nor this window's own;
//   - once the daemon says every HUD and API response refuses to be framed
//     (`/status` `frame_hardened`). That is what makes it safe: scripts plus
//     same-origin amount to no sandbox only when the framed page is the
//     *parent's* origin. The port check refuses that up front, and
//     `frame-ancestors 'none'` stops a frame from navigating itself into the
//     HUD later. A page on another origin cannot touch the parent at all.
// The grant is stored as the **origin it was given for**, so loading a URL on
// another port, host or scheme turns it off by construction; and a stored
// grant is judged again on every load and dropped when it no longer passes.
// The frame never gets `allow-top-navigation`, popups (escaping or not) or
// downloads, with or without it.

export const FACE_PORT = 8402;
export const WORKSHOP_PORT = 8403;
export const API_PORT = 8405;

export interface PreviewVerdict {
  ok: boolean;
  url: string;
  reason: string;
}

/** The daemon's listeners as the serving daemon reported them, and this window's own port. */
export interface DaemonPorts {
  hud: number;
  api: number;
  workshop: number;
  /** The port this window was served from (null: unknown). */
  self: number | null;
  /** Every HUD and API response refuses to be framed (`/status` `frame_hardened`). */
  frameHardened: boolean;
}

/** What a window knows before `/status` answers: the defaults, and no frame hardening. */
export const DEFAULT_PORTS: DaemonPorts = {
  hud: FACE_PORT, api: API_PORT, workshop: WORKSHOP_PORT, self: null, frameHardened: false,
};

/** The sandbox every preview frame gets. Nothing else is ever added but `allow-same-origin`. */
export const SANDBOX = "allow-scripts allow-forms";

export function isLoopbackHost(host: string): boolean {
  const h = host.toLowerCase();
  if (h === "localhost" || h.endsWith(".localhost")) return true;
  if (h === "::1" || h === "[::1]") return true;
  if (h === "0.0.0.0") return true;
  return /^127\.\d+\.\d+\.\d+$/.test(h);
}

function portOf(v: unknown): number | null {
  return typeof v === "number" && Number.isInteger(v) && v > 0 && v < 65536 ? v : null;
}

/** The port a URL names, or its scheme's default. */
function urlPort(url: URL): number {
  return Number(url.port || (url.protocol === "https:" ? 443 : 80));
}

/** Ports a preview frame never loads: the HUD, the API (reported and default) and this window's own. */
function framelessPorts(ports: Partial<DaemonPorts>): Set<number> {
  const out = new Set<number>([FACE_PORT, API_PORT]);
  for (const p of [ports.hud, ports.api, ports.self]) if (typeof p === "number") out.add(p);
  return out;
}

/** Ports whose pages never keep their origin: the above, and the workshop (reported and default). */
function daemonPorts(ports: Partial<DaemonPorts>): Set<number> {
  const out = framelessPorts(ports);
  out.add(WORKSHOP_PORT);
  if (typeof ports.workshop === "number") out.add(ports.workshop);
  return out;
}

/**
 * Judge a URL for the preview pane.
 *
 * Allowed: the workshop origin (`http://127.0.0.1:8403/p/<project>/…`) and any
 * `http://localhost:<port>/…` dev server. Refused: the HUD's own origin, the
 * daemon's API listener (WP-E), this window's own port, and anything that is
 * not loopback http — a preview pane is for looking at what was just built on
 * this machine, not a browser. `ports` is what `/status` reported; the
 * defaults (8402, 8405) are refused whatever it says, since they are the
 * owner's live daemon.
 */
export function judgePreviewUrl(raw: string, ports: Partial<DaemonPorts> = {}): PreviewVerdict {
  const text = (raw || "").trim();
  if (!text) return { ok: false, url: "", reason: "Enter a preview URL." };
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return { ok: false, url: "", reason: "Not a URL." };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, url: "", reason: `Refused: ${url.protocol} is not a page.` };
  }
  // As a terminal link is (lib/terminal.ts judgeLink): a URL carrying a user
  // name or password is never loaded.
  if (url.username || url.password) {
    return { ok: false, url: "", reason: "Refused: a URL with a user name or password in it." };
  }
  if (!isLoopbackHost(url.hostname)) {
    return { ok: false, url: "", reason: "Refused: the preview pane only loads local servers." };
  }
  const port = urlPort(url);
  if (framelessPorts(ports).has(port)) {
    const api = port === API_PORT || port === ports.api;
    const hud = port === FACE_PORT || port === ports.hud || port === ports.self;
    return {
      ok: false,
      url: "",
      reason: hud || !api
        ? "Refused: that is the HUD's own origin. The window that gates approvals " +
          "is never loaded in a frame inside itself."
        : "Refused: that is Jarvis's API listener. Jarvis's own control plane is " +
          "never loaded in a frame.",
    };
  }
  return { ok: true, url: url.href, reason: "" };
}

/** The origin a URL loads under (scheme, host, port), or "" for something that is not one. */
export function originOf(raw: string): string {
  try {
    const u = new URL((raw || "").trim());
    return u.protocol === "http:" || u.protocol === "https:" ? u.origin : "";
  } catch {
    return "";
  }
}

export interface KeepVerdict {
  ok: boolean;
  /** The origin the page would keep (what a grant stores). */
  origin: string;
  reason: string;
}

/**
 * Whether a pane showing `url` may let it keep its own origin (W-4): a URL
 * the pane may load at all, on a loopback port that is none of the daemon's
 * three and not this window's own, served by a daemon whose every HUD and API
 * response refuses to be framed.
 */
export function judgeKeepOrigin(url: string, ports: DaemonPorts): KeepVerdict {
  if (!ports.frameHardened) {
    return { ok: false, origin: "", reason: "Jarvis's daemon does not refuse to be framed yet, so no page keeps its origin." };
  }
  const v = judgePreviewUrl(url, ports);
  if (!v.ok) return { ok: false, origin: "", reason: v.reason };
  const u = new URL(v.url);
  if (!isLoopbackHost(u.hostname)) {
    return { ok: false, origin: "", reason: "Only a local dev server's page may keep its origin." };
  }
  if (daemonPorts(ports).has(urlPort(u))) {
    return { ok: false, origin: "", reason: "A page on one of Jarvis's own ports never keeps its origin." };
  }
  return { ok: true, origin: u.origin, reason: "" };
}

/**
 * A stored grant, judged again against the URL the pane loads now: the
 * origin it names while that is still the URL's origin and still allowed,
 * else null — and the caller drops it.
 */
export function keptOrigin(stored: string | null | undefined, url: string, ports: DaemonPorts): string | null {
  if (!stored || !url) return null;
  const v = judgeKeepOrigin(url, ports);
  return v.ok && v.origin === stored ? stored : null;
}

/**
 * What may be stored with a pane, judged without `/status` (parse time): a
 * string that is exactly the stored URL's origin, on a loopback port that is
 * none of the default daemon ports. The ports `/status` reports are judged
 * when the pane loads (`keptOrigin`).
 */
export function storableKeepOrigin(stored: unknown, url: string | undefined): string | null {
  if (typeof stored !== "string" || !stored || stored.length > 200 || !url) return null;
  return keptOrigin(stored, url, { ...DEFAULT_PORTS, frameHardened: true });
}

/** The frame's sandbox: `allow-same-origin` only with a grant that still passes. */
export function sandboxFor(keepOrigin: boolean): string {
  return keepOrigin ? `${SANDBOX} allow-same-origin` : SANDBOX;
}

/**
 * The workshop port the serving daemon reported on `/status`.
 *
 * Only a daemon too old to report one falls back to the default: the HUD and
 * its daemon ship together, so that is a stale daemon, not a test. A test's
 * mock reports its own port, which is the point — a hard-coded 8403 sent the
 * HUD suite's fixture project to the owner's live daemon.
 */
export function workshopPortFrom(status: unknown): number {
  const port = (status as { workshop_port?: unknown } | null)?.workshop_port;
  return portOf(port) ?? WORKSHOP_PORT;
}

/** Everything the preview needs from `/status`; the defaults for what a daemon did not say. */
export function daemonPortsFrom(status: unknown, self: number | null = null): DaemonPorts {
  const s = status && typeof status === "object" ? (status as Record<string, unknown>) : {};
  return {
    hud: portOf(s.hud_port) ?? FACE_PORT,
    api: portOf(s.api_port) ?? API_PORT,
    workshop: workshopPortFrom(status),
    self: portOf(self),
    frameHardened: s.frame_hardened === true,
  };
}

/** The workshop URL for a file inside a project (docs/hud-api.md, Preview origin). */
export function workshopUrl(projectId: string, rel: string, port = WORKSHOP_PORT): string {
  const clean = rel.replace(/^\/+/, "");
  return `http://127.0.0.1:${port}/p/${encodeURIComponent(projectId)}/${clean}`;
}

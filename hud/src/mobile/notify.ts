// "Turn on notifications" on the phone (phone design §7, P-6).
//
// Web Push on the iPhone works only in the app opened from the Home Screen,
// and the browser asks for permission only from a tap. So the app shows one
// of these, under the Inbox:
//
//   unsupported  this browser has no Web Push;
//   install      iOS, not opened from the Home Screen: how to add it;
//   blocked      permission was refused: where to allow it again;
//   off          "Turn on" (a tap subscribes, then tells Jarvis);
//   on           "Turn off" (unsubscribes here, then tells Jarvis);
//   error        what failed, and the button again.
//
// The service worker (./sw.ts, /m/sw.js) is registered when the app starts;
// the subscribe call is the tap's first await, so iOS sees the gesture. A
// subscription this phone already holds is sent again at start, so a new
// session (after a sign-in) takes it over. The push carries a count only
// (jarvis/v2/push.py). Text here is code's own; nothing from a server is
// shown but a status word.

export type NotifyState = "unsupported" | "install" | "blocked" | "off" | "on" | "busy" | "error" | "unavailable";

export interface SubscriptionLike {
  endpoint: string;
  /** The key it was made for (`PushSubscription.options.applicationServerKey`). */
  options?: { applicationServerKey?: ArrayBuffer | null } | null;
  toJSON(): unknown;
  unsubscribe(): Promise<boolean>;
}

export interface RegistrationLike {
  pushManager: {
    getSubscription(): Promise<SubscriptionLike | null>;
    subscribe(options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }): Promise<SubscriptionLike>;
  };
}

/** What the browser offers (a fake in the tests). */
export interface PushEnv {
  supported(): boolean;
  /** iPhone or iPad (where push needs the Home Screen app). */
  ios(): boolean;
  /** Opened from the Home Screen (display-mode standalone). */
  standalone(): boolean;
  permission(): "default" | "granted" | "denied";
  register(): Promise<RegistrationLike>;
  /** The service worker asked the app to show the Inbox (a notification's tap). */
  onOpenInbox(fn: () => void): void;
}

export type Send = (method: "GET" | "POST" | "DELETE", path: string, body?: unknown) =>
  Promise<{ status: number; body: Record<string, unknown> } | null>;

export const PUSH_API = "/m/api/push";
export const SUBSCRIBE_API = "/m/api/push/subscribe";

export const browserEnv: PushEnv = {
  supported: () => typeof navigator !== "undefined" && "serviceWorker" in navigator
    && typeof window !== "undefined" && "PushManager" in window && "Notification" in window,
  ios: () => typeof navigator !== "undefined" && (/iPhone|iPad|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)),
  standalone: () => (typeof window !== "undefined" && window.matchMedia?.("(display-mode: standalone)").matches)
    || (typeof navigator !== "undefined" && (navigator as unknown as { standalone?: boolean }).standalone === true),
  permission: () => (typeof Notification !== "undefined" ? Notification.permission : "default"),
  register: () => navigator.serviceWorker.register("/m/sw.js", { scope: "/m/" }) as unknown as Promise<RegistrationLike>,
  onOpenInbox: (fn) => {
    navigator.serviceWorker?.addEventListener("message", (event: MessageEvent) => {
      const data = event.data as { kind?: unknown } | null;
      if (data && data.kind === "open-inbox") fn();
    });
  },
};

export const browserSend: Send = async (method, path, body) => {
  try {
    const init: RequestInit = { method, credentials: "same-origin", cache: "no-store" };
    if (method !== "GET") {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body ?? {});
    }
    const res = await fetch(path, init);
    let value: Record<string, unknown> = {};
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) value = parsed as Record<string, unknown>;
    } catch { /* the status says enough */ }
    return { status: res.status, body: value };
  } catch {
    return null;
  }
};

/** Whether a subscription was made for this key; true when the browser
 * does not say (undoing it on every start would then never stay on). */
export function sameKey(sub: SubscriptionLike, key: Uint8Array): boolean {
  const held = sub.options?.applicationServerKey;
  if (!held) return true;
  const bytes = new Uint8Array(held);
  return bytes.length === key.length && bytes.every((b, i) => b === key[i]);
}

/** A base64url string as bytes (the applicationServerKey). */
export function keyBytes(text: string): Uint8Array | null {
  if (!/^[A-Za-z0-9_-]{80,100}$/.test(text)) return null;
  const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4);
  let raw: string;
  try { raw = atob(b64); } catch { return null; }
  if (raw.length !== 65 || raw.charCodeAt(0) !== 4) return null;
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export const WORDS: Record<NotifyState, string> = {
  unsupported: "This browser cannot show notifications from Jarvis.",
  install: "Notifications work only in the Jarvis app on the Home Screen. In Safari, tap Share, then "
    + "Add to Home Screen; open Jarvis from there and turn them on.",
  blocked: "Notifications are blocked for Jarvis. Allow them in the iPhone's Settings, Notifications, Jarvis.",
  off: "Notifications off",
  on: "Notifications on",
  busy: "One moment…",
  error: "Notifications could not be turned on.",
  unavailable: "Notifications are not available on the PC right now. Try again later.",
};

/** The muted hint beside "on" (only): what a push says. */
export const HINT = "count only, never what";

export class Notifications {
  state: NotifyState = "off";
  detail = "";
  /** The desk's switch (`/phone/push/settings`): false means nothing is sent. */
  deskOn = true;
  private reg: RegistrationLike | null = null;
  private key: Uint8Array | null = null;

  constructor(private readonly env: PushEnv, private readonly send: Send, private readonly changed: () => void,
              openInbox: () => void) {
    if (!env.supported()) { this.state = env.ios() && !env.standalone() ? "install" : "unsupported"; return; }
    if (env.ios() && !env.standalone()) { this.state = "install"; return; }
    try { env.onOpenInbox(openInbox); } catch { /* the tap still opens the app */ }
  }

  /** At start: register the worker, read Jarvis's key and whether this
   * session is subscribed, and hand over a subscription this phone holds. */
  async sync(): Promise<void> {
    if (this.state === "unsupported" || this.state === "install") return;
    try {
      try {
        this.reg = await this.env.register();
      } catch {
        this.state = "error";
        this.detail = "This phone could not set notifications up. Close Jarvis and open it again.";
        return;
      }
      const status = await this.send("GET", PUSH_API);
      if (!status || status.status !== 200) {
        this.state = "unavailable";
        return;
      }
      this.deskOn = status.body.enabled !== false;
      this.key = typeof status.body.key === "string" ? keyBytes(status.body.key) : null;
      const held = await this.reg.pushManager.getSubscription();
      if (this.env.permission() === "denied") this.state = "blocked";
      else if (held && this.env.permission() === "granted") {
        if (!this.key || !sameKey(held, this.key)) {
          // Made for another key (Jarvis's key changed): it would never be
          // delivered. Undone here and at Jarvis; the owner turns it on again.
          try { await held.unsubscribe(); } catch { /* still tell Jarvis */ }
          await this.send("DELETE", SUBSCRIBE_API, { endpoint: held.endpoint });
          this.state = "off";
          this.detail = "Jarvis's notification key changed: turn notifications on again.";
        } else if (status.body.subscribed !== true) {
          const sent = await this.send("POST", SUBSCRIBE_API, { subscription: held.toJSON() });
          this.state = sent && sent.status === 200 ? "on" : "off";
        } else {
          this.state = "on";
        }
      } else {
        this.state = "off";
      }
    } catch {
      this.state = "off";
    } finally {
      this.changed();
    }
  }

  /** The tap. The subscribe call is the first await, so it runs in the gesture. */
  async turnOn(): Promise<void> {
    if (this.state === "busy" || !this.reg || !this.key) {
      if (!this.reg || !this.key) { this.state = "error"; this.detail = "Jarvis could not be reached."; this.changed(); }
      return;
    }
    const reg = this.reg;
    const key = this.key;
    this.state = "busy";
    this.detail = "";
    let sub: SubscriptionLike;
    try {
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    } catch (error) {
      const name = error && typeof error === "object" && "name" in error ? String((error as { name: unknown }).name) : "";
      this.state = name === "NotAllowedError" && this.env.permission() === "denied" ? "blocked" : "error";
      this.detail = this.state === "error" ? "The phone did not allow it." : "";
      this.changed();
      return;
    }
    this.changed();
    const sent = await this.send("POST", SUBSCRIBE_API, { subscription: sub.toJSON() });
    if (sent && sent.status === 200) {
      this.state = "on";
    } else {
      this.state = "error";
      const word = sent && typeof sent.body.error === "string" ? sent.body.error : "";
      this.detail = sent === null ? "Jarvis could not be reached."
        : sent.status === 503 ? "Notifications are not available on the PC right now."
        : word ? `Jarvis said: ${word}.` : `Jarvis refused it (${sent.status}).`;
      try { await sub.unsubscribe(); } catch { /* nothing more to undo */ }
    }
    this.changed();
  }

  async turnOff(): Promise<void> {
    if (this.state === "busy") return;
    this.state = "busy";
    this.changed();
    let endpoint: string | undefined;
    try {
      const held = this.reg ? await this.reg.pushManager.getSubscription() : null;
      if (held) { endpoint = held.endpoint; await held.unsubscribe(); }
    } catch { /* still tell Jarvis */ }
    await this.send("DELETE", SUBSCRIBE_API, endpoint ? { endpoint } : {});
    this.state = "off";
    this.changed();
  }

  /** The row under the Inbox: one compact line and, where it can act, a small
   * button at its right ("Turn on", "Turn off"). The longer states (install,
   * blocked, …) keep their whole sentence, in the same small type. */
  view(): HTMLElement {
    const box = document.createElement("section");
    box.className = "notify";
    box.dataset.testid = "m-notify";
    box.dataset.state = this.state;
    const line = document.createElement("p");
    line.className = "notify-line";
    line.textContent = WORDS[this.state] + (this.detail ? " " + this.detail : "");
    const hint = this.state === "on" && !this.deskOn ? "turned off at the desk for now"
      : this.state === "on" ? HINT : "";
    if (hint) {
      // One line: the hint gives way (an ellipsis) before the row grows a second line.
      if (!this.detail) line.classList.add("one-line");
      const small = document.createElement("span");
      small.className = "notify-hint";
      small.dataset.testid = "m-notify-hint";
      small.textContent = " · " + hint;
      line.append(small);
    }
    box.append(line);
    const action = this.state === "off" || this.state === "error" ? "Turn on"
      : this.state === "on" ? "Turn off" : "";
    if (action) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tap notify-act";
      b.dataset.testid = "m-notify-button";
      b.textContent = action;
      b.setAttribute("aria-label", action + " notifications");
      b.addEventListener("click", () => { void (this.state === "on" ? this.turnOff() : this.turnOn()); });
      box.append(b);
    }
    return box;
  }
}
